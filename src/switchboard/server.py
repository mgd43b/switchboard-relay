"""FastMCP server exposing the switchboard tools.

Two ways to run the same server:

* **stdio (default)** -- ``switchboard``. Every Claude Code session spawns its
  own stdio process; durability and cross-session delivery come from a shared
  SQLite database. ``wait()`` long-polls that database. This is the v1 default
  and satisfies all of switchboard's core goals. It cannot *push* -- a session's
  process has no handle to another session's process -- so recipients poll.

* **HTTP daemon (opt-in)** -- ``switchboard serve``. One process holds every
  session's connection, so ``send()`` can additionally emit a Channels
  notification (``notifications/claude/channel``) straight into a connected
  recipient. This is the experimental push path; see the README.

Identity is bound per MCP connection: ``register(name, role)`` associates the
calling session with an address, and subsequent ``inbox()``/``wait()``/``send()``
calls resolve "who am I" from that connection. In stdio there is exactly one
connection per process; in the daemon there are many, keyed by the live
``ServerSession`` object.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass
from typing import Any, Optional

import anyio
from mcp.server.fastmcp import Context, FastMCP

from switchboard.store import DEFAULT_TTL_SECONDS, Store, default_db_path

# How often wait() checks the mailbox, and how often it heartbeats the caller's
# liveness while parked. Poll is a balance between latency and DB churn.
_WAIT_POLL_SECONDS = 0.25
_WAIT_HEARTBEAT_SECONDS = 10.0

_CHANNEL_METHOD = "notifications/claude/channel"


def _now() -> float:
    return time.time()


def _resolve_ttl() -> float:
    raw = os.environ.get("SWITCHBOARD_TTL")
    if not raw:
        return DEFAULT_TTL_SECONDS
    try:
        val = float(raw)
        return val if val > 0 else DEFAULT_TTL_SECONDS
    except ValueError:
        return DEFAULT_TTL_SECONDS


@dataclass
class _Conn:
    """One connected participant: an address bound to a live MCP session."""

    name: str
    role: str
    session: Any  # mcp.server.session.ServerSession


class Switchboard:
    """Wires the durable :class:`~switchboard.store.Store` to MCP tools.

    Holds the in-process connection registry used for identity resolution and
    (in daemon mode) for pushing notifications to connected recipients. The
    durable state lives entirely in the store; this registry is soft state that
    is rebuilt as sessions register.
    """

    def __init__(self, store: Store, *, ttl: float = DEFAULT_TTL_SECONDS):
        self.store = store
        self.ttl = ttl
        # id(ctx.session) -> _Conn. Keyed by object identity because one
        # ServerSession maps to exactly one client connection for its lifetime.
        self._conns: dict[int, _Conn] = {}
        # Optional identity seeded from the environment so scripted worker
        # sessions can be launched pre-addressed (SWITCHBOARD_NAME/ROLE) without
        # an explicit register() call.
        self._default_name = (os.environ.get("SWITCHBOARD_NAME") or "").strip()
        self._default_role = (os.environ.get("SWITCHBOARD_ROLE") or "").strip()
        # Push is opt-in. It only does anything in daemon mode (a recipient
        # connected to the same process) AND only makes sense when recipients
        # are Claude sessions subscribed as a channel -- a stock MCP client
        # rejects the custom notification method. Off by default keeps the
        # common path pure durable-poll with no surprises.
        self._push_enabled = (os.environ.get("SWITCHBOARD_PUSH") or "").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        )

    # -- identity -----------------------------------------------------------

    def _bind(self, ctx: Context, name: str, role: str) -> _Conn:
        conn = _Conn(name=name, role=role, session=ctx.session)
        self._conns[id(ctx.session)] = conn
        return conn

    def _resolve(self, ctx: Context) -> _Conn:
        """Return the caller's bound identity, or raise a helpful error."""
        conn = self._conns.get(id(ctx.session))
        if conn is not None:
            return conn
        if self._default_name:
            # Lazily register the environment-seeded identity for this session.
            self.store.register(self._default_name, self._default_role, now=_now())
            return self._bind(ctx, self._default_name, self._default_role)
        raise ValueError(
            'This session is not registered. Call register(name="...", '
            'role="...") first so switchboard knows which mailbox is yours '
            "(or launch with $SWITCHBOARD_NAME set)."
        )

    # -- push (daemon mode) -------------------------------------------------

    def _push_targets(self, address: str, *, exclude_session_id: int) -> list[_Conn]:
        return [
            c
            for sid, c in self._conns.items()
            if sid != exclude_session_id and (c.name == address or (c.role and c.role == address))
        ]

    async def _push(self, conn: _Conn, *, content: str, meta: dict[str, str]) -> bool:
        """Best-effort Channels notification to a connected recipient.

        Returns True if the notification was handed to the transport. Never
        raises: push is an optimization layered on top of durable delivery, so
        any failure (old SDK, closed stream, recipient not a channel) simply
        falls back to the recipient polling inbox()/wait().
        """
        try:
            import mcp.types as types
            from pydantic import BaseModel, ConfigDict

            class _ChannelParams(BaseModel):
                model_config = ConfigDict(extra="allow")

            notification = types.Notification[_ChannelParams, str](
                method=_CHANNEL_METHOD,
                params=_ChannelParams.model_validate({"content": content, "meta": meta}),
            )
            # related_request_id is intentionally omitted: this notification
            # targets a *different* session than the current request, so it must
            # ride the recipient's standalone stream, not our request's stream.
            await conn.session.send_notification(notification)  # type: ignore[arg-type]
            return True
        except Exception:
            return False

    # -- tool implementations ----------------------------------------------

    def register(self, ctx: Context, name: str, role: str = "") -> dict:
        name = (name or "").strip()
        if not name:
            raise ValueError('name must be a non-empty string, e.g. "lead" or "worker:feature-x".')
        role = (role or "").strip()
        p = self.store.register(name, role, now=_now())
        self._bind(ctx, p.name, p.role)
        return {
            "ok": True,
            "you": p.name,
            "role": p.role,
            "ttl_seconds": self.ttl,
            "participants": self._participants_payload(),
        }

    def participants(self, ctx: Context) -> dict:
        # A read is also a heartbeat for the caller, if registered.
        conn = self._conns.get(id(ctx.session))
        if conn is not None:
            self.store.touch(conn.name, now=_now())
        payload = self._participants_payload()
        return {"participants": payload, "count": len(payload)}

    def send(self, ctx: Context, to: str, body: str, reply_to: Optional[int] = None) -> dict:
        conn = self._resolve(ctx)
        to = (to or "").strip()
        if not to:
            raise ValueError("`to` must be a non-empty address (a participant name or role).")
        now = _now()
        mid = self.store.send(to, body, sender=conn.name, reply_to=reply_to, now=now)
        self.store.touch(conn.name, now=now)
        return {"id": mid, "to": to, "from": conn.name, "reply_to": reply_to}

    async def send_async(
        self, ctx: Context, to: str, body: str, reply_to: Optional[int] = None
    ) -> dict:
        result = self.send(ctx, to, body, reply_to)
        # Best-effort push to any connected recipient (daemon mode only, opt-in).
        delivered_live = False
        targets = (
            self._push_targets(result["to"], exclude_session_id=id(ctx.session))
            if self._push_enabled
            else []
        )
        if targets:
            # A nudge, not the message itself: the durable inbox row is the
            # single source of truth. Deliberately omitting the body keeps push
            # consistent with drain-once semantics -- when a role is addressed,
            # every connected member is nudged but only the one that drains the
            # row actually receives it; the others just find an empty inbox,
            # which is harmless. Embedding the body here would let multiple
            # role members act on a message only one of them "owns".
            content = (
                f"New switchboard message from {result['from']} (id {result['id']}). "
                f"Call inbox() (or wait()) to read it."
            )
            meta = {
                "source": "switchboard",
                "msg_from": str(result["from"]),
                "msg_to": str(result["to"]),
                "msg_id": str(result["id"]),
            }
            if reply_to is not None:
                meta["reply_to"] = str(reply_to)
            for t in targets:
                if await self._push(t, content=content, meta=meta):
                    delivered_live = True
        result["delivered_live"] = delivered_live
        return result

    def inbox(self, ctx: Context, peek: bool = False, since: Optional[int] = None) -> dict:
        conn = self._resolve(ctx)
        self.store.touch(conn.name, now=_now())
        msgs = self.store.inbox(conn.name, conn.role, peek=peek, since=since)
        return {
            "you": conn.name,
            "messages": [m.to_dict() for m in msgs],
            "count": len(msgs),
            "peek": peek,
        }

    async def wait(self, ctx: Context, timeout_s: float = 30.0) -> dict:
        conn = self._resolve(ctx)
        try:
            timeout_s = float(timeout_s)
        except (TypeError, ValueError):
            timeout_s = 30.0
        timeout_s = max(0.0, min(timeout_s, 3600.0))

        deadline = _now() + timeout_s
        last_heartbeat = 0.0
        while True:
            now = _now()
            if now - last_heartbeat >= _WAIT_HEARTBEAT_SECONDS:
                self.store.touch(conn.name, now=now)
                last_heartbeat = now
            if self.store.has_messages(conn.name, conn.role):
                msgs = self.store.inbox(conn.name, conn.role)  # drain
                if msgs:
                    self.store.touch(conn.name, now=_now())
                    return {
                        "you": conn.name,
                        "messages": [m.to_dict() for m in msgs],
                        "count": len(msgs),
                        "timed_out": False,
                    }
            if now >= deadline:
                self.store.touch(conn.name, now=now)
                return {"you": conn.name, "messages": [], "count": 0, "timed_out": True}
            await anyio.sleep(min(_WAIT_POLL_SECONDS, max(0.0, deadline - now)))

    # -- helpers ------------------------------------------------------------

    def _participants_payload(self) -> list[dict]:
        now = _now()
        # Opportunistic housekeeping so the table does not grow unbounded.
        self.store.prune_participants(now=now, ttl=self.ttl)
        out = []
        for p in self.store.participants(now=now, ttl=self.ttl):
            d = p.to_dict()
            d["idle_seconds"] = round(now - p.last_seen, 1)
            out.append(d)
        return out


# -- FastMCP wiring ---------------------------------------------------------

# Tool docstrings are what Claude reads to decide when/how to call each tool, so
# they double as the user-facing tool descriptions. Keep them action-oriented.


def build_server(store: Optional[Store] = None, *, ttl: Optional[float] = None) -> FastMCP:
    store = store if store is not None else Store()
    sb = Switchboard(store, ttl=ttl if ttl is not None else _resolve_ttl())

    mcp = FastMCP(
        "switchboard",
        instructions=(
            "Shared message bus for independent Claude Code sessions. Call "
            'register(name) once to claim an address (e.g. "lead" or '
            '"worker:feature-x"), then send(to, body) to message another '
            "session and inbox()/wait() to receive. Messages are durable: they "
            "wait in the recipient's mailbox until read."
        ),
    )
    # Expose the wiring for tests and daemon introspection.
    mcp._switchboard = sb  # type: ignore[attr-defined]

    @mcp.tool()
    def register(name: str, role: str = "", ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        """Claim an address on the switchboard for this session.

        `name` is your address that others send to (e.g. "lead",
        "worker:feature-x"). `role` is an optional shared address for a group
        (e.g. "worker") -- a message sent to a role is delivered to any
        participant that reads with that role. Re-call to refresh your presence
        (a heartbeat) or change your role. Returns the current live participants.
        """
        return sb.register(ctx, name, role)

    @mcp.tool()
    def participants(ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        """List participants seen within the TTL window (name, role, idle_seconds)."""
        return sb.participants(ctx)

    @mcp.tool()
    async def send(  # type: ignore[assignment]
        to: str, body: str, reply_to: Optional[int] = None, ctx: Context = None
    ) -> dict[str, Any]:
        """Send a message to another participant's durable inbox.

        `to` is an address: a participant name or a role. `body` is the message
        text. Pass `reply_to` with a message id you received to thread a reply.
        Delivery is durable -- the message waits even if `to` is offline or has
        not registered yet. Returns the new message `id`.
        """
        return await sb.send_async(ctx, to, body, reply_to)

    @mcp.tool()
    def inbox(  # type: ignore[assignment]
        peek: bool = False, since: Optional[int] = None, ctx: Context = None
    ) -> dict[str, Any]:
        """Read messages addressed to you (by your name or role).

        By default this DRAINS your inbox: messages are returned once and then
        removed. Pass `peek=True` to read without removing. Pass `since=<id>` to
        only return messages newer than that id (useful with peek to poll).
        """
        return sb.inbox(ctx, peek=peek, since=since)

    @mcp.tool()
    async def wait(timeout_s: float = 30.0, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        """Block until a message arrives for you, then drain and return it.

        Long-polls your inbox for up to `timeout_s` seconds (capped at 3600).
        Returns as soon as any message is available, draining it like inbox().
        On timeout returns an empty list with `timed_out=true`. Ideal for a
        long-running "lead" session parked in a /loop waiting for questions.
        """
        return await sb.wait(ctx, timeout_s)

    return mcp


# -- CLI --------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="switchboard",
        description="A local MCP server for inter-session messaging between Claude Code sessions.",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="Path to the SQLite database (default: $SWITCHBOARD_DB or ~/.claude/switchboard.db).",
    )
    parser.add_argument(
        "--ttl",
        type=float,
        default=None,
        help="Participant liveness window in seconds (default: $SWITCHBOARD_TTL or 300).",
    )
    parser.add_argument("--version", action="store_true", help="Print version and exit.")

    sub = parser.add_subparsers(dest="cmd")
    serve = sub.add_parser(
        "serve",
        help="Run as a shared HTTP daemon (streamable-http) enabling push. Experimental.",
    )
    serve.add_argument("--host", default=os.environ.get("SWITCHBOARD_HOST", "127.0.0.1"))
    serve.add_argument("--port", type=int, default=int(os.environ.get("SWITCHBOARD_PORT", "8765")))
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    if args.version:
        from switchboard import __version__

        print(f"switchboard {__version__}")
        return 0

    db_path = args.db or os.environ.get("SWITCHBOARD_DB") or default_db_path()
    store = Store(db_path)
    ttl = args.ttl if args.ttl is not None else _resolve_ttl()
    mcp = build_server(store, ttl=ttl)

    if args.cmd == "serve":
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        print(
            f"switchboard daemon (streamable-http) on http://{args.host}:{args.port}"
            f"{mcp.settings.streamable_http_path}  db={store.db_path}",
            file=sys.stderr,
        )
        mcp.run(transport="streamable-http")
    else:
        # Default: stdio, one process per Claude Code session.
        mcp.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
