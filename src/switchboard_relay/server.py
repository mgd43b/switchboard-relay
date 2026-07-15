"""FastMCP server exposing the switchboard-relay tools.

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
from pathlib import Path
from typing import Any, Optional

import anyio
from mcp.server.fastmcp import Context, FastMCP

from switchboard_relay.store import DEFAULT_TTL_SECONDS, Store, default_db_path

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


def _clamp_timeout(value: Any, *, default: float = 30.0, cap: float = 3600.0) -> float:
    """Coerce a caller-supplied timeout to a sane [0, cap] range."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        v = default
    return max(0.0, min(v, cap))


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

    async def _notify(
        self, ctx: Context, *, sender: str, to: str, mid: int, reply_to: Optional[int]
    ) -> bool:
        """Best-effort push nudge to any connected recipient(s) of ``to``.

        Returns True if at least one live delivery was made. This is a *nudge*,
        not the message itself: the durable inbox row is the single source of
        truth. Deliberately omitting the body keeps push consistent with
        drain-once semantics -- when a role is addressed every connected member
        is nudged but only the one that drains the row receives it; the others
        just find an empty inbox, which is harmless. Embedding the body here
        would let multiple role members act on a message only one of them owns.
        No-op unless push is enabled (daemon mode).
        """
        if not self._push_enabled:
            return False
        targets = self._push_targets(to, exclude_session_id=id(ctx.session))
        if not targets:
            return False
        content = (
            f"New switchboard message from {sender} (id {mid}). "
            f"Call inbox() (or wait()) to read it."
        )
        meta = {
            "source": "switchboard-relay",
            "msg_from": str(sender),
            "msg_to": str(to),
            "msg_id": str(mid),
        }
        if reply_to is not None:
            meta["reply_to"] = str(reply_to)
        delivered = False
        for t in targets:
            if await self._push(t, content=content, meta=meta):
                delivered = True
        return delivered

    async def send_async(
        self, ctx: Context, to: str, body: str, reply_to: Optional[int] = None
    ) -> dict:
        result = self.send(ctx, to, body, reply_to)
        result["delivered_live"] = await self._notify(
            ctx, sender=result["from"], to=result["to"], mid=result["id"], reply_to=reply_to
        )
        return result

    async def ask(self, ctx: Context, to: str, body: str, timeout_s: float = 30.0) -> dict:
        """Send a question and block until its reply arrives (or timeout).

        Sends ``body`` to ``to``, then waits for a message addressed back to us
        whose ``reply_to`` is this question's id -- draining only that reply and
        leaving any other inbox messages untouched.
        """
        conn = self._resolve(ctx)
        to = (to or "").strip()
        if not to:
            raise ValueError("`to` must be a non-empty address (a participant name or role).")
        now = _now()
        qid = self.store.send(to, body, sender=conn.name, now=now)
        self.store.touch(conn.name, now=now)
        await self._notify(ctx, sender=conn.name, to=to, mid=qid, reply_to=None)

        timeout_s = _clamp_timeout(timeout_s)
        deadline = _now() + timeout_s
        last_heartbeat = 0.0
        while True:
            now = _now()
            if now - last_heartbeat >= _WAIT_HEARTBEAT_SECONDS:
                self.store.touch(conn.name, now=now)
                last_heartbeat = now
            reply = self.store.take_reply(conn.name, conn.role, reply_to=qid)
            if reply is not None:
                self.store.touch(conn.name, now=_now())
                return {
                    "you": conn.name,
                    "asked": to,
                    "question_id": qid,
                    "reply": reply.to_dict(),
                    "timed_out": False,
                }
            if now >= deadline:
                self.store.touch(conn.name, now=now)
                return {
                    "you": conn.name,
                    "asked": to,
                    "question_id": qid,
                    "reply": None,
                    "timed_out": True,
                }
            await anyio.sleep(min(_WAIT_POLL_SECONDS, max(0.0, deadline - now)))

    async def broadcast(self, ctx: Context, body: str) -> dict:
        """Send ``body`` to every currently-live participant except the sender."""
        conn = self._resolve(ctx)
        now = _now()
        recipients = [
            p for p in self.store.participants(now=now, ttl=self.ttl) if p.name != conn.name
        ]
        delivered = []
        for p in recipients:
            mid = self.store.send(p.name, body, sender=conn.name, now=now)
            live = await self._notify(ctx, sender=conn.name, to=p.name, mid=mid, reply_to=None)
            delivered.append({"to": p.name, "id": mid, "delivered_live": live})
        self.store.touch(conn.name, now=_now())
        return {"from": conn.name, "delivered": delivered, "count": len(delivered)}

    def unregister(self, ctx: Context) -> dict:
        """Remove the caller from the participant registry (a clean leave)."""
        conn = self._conns.pop(id(ctx.session), None)
        name = conn.name if conn is not None else (self._default_name or None)
        if not name:
            return {"ok": True, "was_registered": False, "you": None}
        removed = self.store.unregister(name)
        return {"ok": True, "was_registered": removed, "you": name}

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
        timeout_s = _clamp_timeout(timeout_s)
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
        "switchboard-relay",
        instructions=(
            "Shared message bus for independent Claude Code sessions. Call "
            'register(name) once to claim an address (e.g. "lead" or '
            '"worker:feature-x"), then send(to, body) to message another '
            "session and inbox()/wait() to receive. For a question that needs an "
            "answer, ask(to, body) sends and blocks for the reply in one call. "
            "Messages are durable: they wait in the recipient's mailbox until read."
        ),
    )
    # Expose the wiring for tests and daemon introspection.
    mcp._switchboard_relay = sb  # type: ignore[attr-defined]

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

    @mcp.tool()
    async def ask(  # type: ignore[assignment]
        to: str, body: str, timeout_s: float = 30.0, ctx: Context = None
    ) -> dict[str, Any]:
        """Send a question to another participant and block until they reply.

        Sends `body` to `to`, then waits up to `timeout_s` seconds (capped at
        3600) for a reply threaded to this question, and returns it. This is the
        one-call request/response: a worker can `ask("lead", "...")` and get the
        answer back directly, as long as the responder replies with `reply_to`
        set to the returned `question_id`. The responder should address the reply
        to the question's `from` (your unique name) -- that is where ask() looks;
        replying to a shared role could let a role peer drain it first. On timeout
        returns `timed_out=true` and `reply=null` (the question still sits durably
        in the recipient's inbox). Other messages in your inbox are left untouched.
        """
        return await sb.ask(ctx, to, body, timeout_s)

    @mcp.tool()
    async def broadcast(body: str, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        """Send `body` to every currently-live participant except yourself.

        Delivers one durable message per live participant (those seen within the
        TTL window). Returns the per-recipient message ids. Recipients that are
        registered but idle past the TTL are skipped.
        """
        return await sb.broadcast(ctx, body)

    @mcp.tool()
    def unregister(ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        """Leave the switchboard: remove yourself from the participant registry.

        A courteous way to drop out so you stop showing in participants(). Your
        mailbox is preserved -- any unread messages remain and are delivered if
        you register under the same name again.
        """
        return sb.unregister(ctx)

    return mcp


# -- CLI --------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="switchboard-relay",
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

    # Human-facing inspection commands (read the DB directly; no MCP server).
    sub.add_parser("participants", help="Print live participants and exit.")
    tail = sub.add_parser("tail", help="Print queued (undelivered) messages and exit.")
    tail.add_argument("-f", "--follow", action="store_true", help="Keep polling for new messages.")
    tail.add_argument(
        "--interval", type=float, default=1.0, help="Follow poll interval in seconds (default 1)."
    )
    prune = sub.add_parser(
        "prune", help="Delete old undelivered messages and expired participants (housekeeping)."
    )
    prune.add_argument(
        "--older-than-days",
        type=float,
        default=7.0,
        help="Delete queued messages older than this many days (default 7).",
    )
    return parser


def _open_store_ro(db_path) -> Optional[Store]:
    """Open the store for a read-only peek, or None if it does not exist yet.

    Avoids materializing the database file (and its WAL sidecars) just to run an
    inspection command against a system that has never started the server.
    """
    p = Path(str(db_path)).expanduser()
    if str(p) != ":memory:" and not p.exists():
        return None
    return Store(db_path)


def _cli_participants(db_path, ttl: float) -> int:
    store = _open_store_ro(db_path)
    if store is None:
        print("No live participants.")
        return 0
    now = _now()
    parts = store.participants(now=now, ttl=ttl)
    if not parts:
        print("No live participants.")
        return 0
    for p in parts:
        role = f" [{p.role}]" if p.role else ""
        print(f"{p.name}{role}  idle={round(now - p.last_seen, 1)}s")
    return 0


def _print_pending(store: Store, since: int) -> int:
    """Print all queued messages with id > ``since``; return the new high-water id."""
    last = since
    while True:
        batch = store.pending_messages(since=last)
        if not batch:
            return last
        for m in batch:
            reply = f" (reply_to #{m.reply_to})" if m.reply_to is not None else ""
            print(f"#{m.id} {m.sender} -> {m.to}: {m.body}{reply}")
        last = batch[-1].id


def _cli_tail(db_path, *, follow: bool = False, interval: float = 1.0) -> int:
    store = _open_store_ro(db_path)
    if store is None:
        return 0
    last_id = _print_pending(store, 0)
    while follow:  # pragma: no cover - interactive follow loop (Ctrl-C to exit)
        time.sleep(max(0.1, interval))
        last_id = _print_pending(store, last_id)
    return 0


def _cli_prune(db_path, ttl: float, older_than_days: float) -> int:
    store = _open_store_ro(db_path)
    if store is None:
        print("Nothing to prune (no database).")
        return 0
    now = _now()
    msgs = store.prune_messages(older_than=now - older_than_days * 86400.0)
    parts = store.prune_participants(now=now, ttl=ttl)
    print(f"Pruned {msgs} old message(s) and {parts} expired participant(s).")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    if args.version:
        from switchboard_relay import __version__

        print(f"switchboard-relay {__version__}")
        return 0

    db_path = args.db or os.environ.get("SWITCHBOARD_DB") or default_db_path()
    ttl = args.ttl if args.ttl is not None else _resolve_ttl()

    # Human-facing inspection commands read the DB directly -- no server, and no
    # side effect of creating the DB if it does not exist yet.
    if args.cmd == "participants":
        return _cli_participants(db_path, ttl)
    if args.cmd == "tail":
        return _cli_tail(db_path, follow=args.follow, interval=args.interval)
    if args.cmd == "prune":
        return _cli_prune(db_path, ttl, args.older_than_days)

    store = Store(db_path)
    mcp = build_server(store, ttl=ttl)

    if args.cmd == "serve":
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        print(
            f"switchboard-relay daemon (streamable-http) on http://{args.host}:{args.port}"
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
