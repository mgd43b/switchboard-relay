"""FastMCP server exposing the switchboard-relay tools.

Two ways to run the same server:

* **stdio (default)** -- ``switchboard``. Every Claude Code session spawns its
  own stdio process; durability and cross-session delivery come from a shared
  SQLite database. ``wait()`` long-polls that database. This is the v1 default
  and satisfies all of switchboard's core goals. It cannot *push* -- a session's
  process has no handle to another session's process -- so recipients poll.

* **HTTP daemon** -- ``switchboard serve``. One process holds every session's
  connection, so ``send()`` can additionally emit a Channels notification
  (``notifications/claude/channel``) straight into a connected recipient, which
  a channel-subscribed session turns into a reactive turn ("turn injection").
  Push is on by default here and best-effort: if the recipient isn't a
  subscribed channel it simply falls back to polling. See the README's "Turn
  injection" section for the subscription recipe and its constraints.

Identity is bound per MCP connection: ``register(name, role)`` associates the
calling session with an address, and subsequent ``inbox()``/``wait()``/``send()``
calls resolve "who am I" from that connection. In stdio there is exactly one
connection per process; in the daemon there are many, keyed by the live
``ServerSession`` object.

Each server is bound to one *board* -- an isolated switchboard with its own
participant registry and mailboxes, backed by its own SQLite file. By default the
board is derived from the project (git repo), so sessions in different repos are
isolated and every worktree of one repo shares a board; ``$SWITCHBOARD_BOARD``
overrides it to any named (e.g. shared) board. See :mod:`switchboard_relay.board`.
"""

from __future__ import annotations

import argparse
import os
import secrets
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import anyio
from mcp.server.fastmcp import Context, FastMCP

from switchboard_relay.board import boards_dir, describe_target, legacy_db_path
from switchboard_relay.store import DEFAULT_TTL_SECONDS, Store

# How often wait() checks the mailbox, and how often it heartbeats the caller's
# liveness while parked. Poll is a balance between latency and DB churn.
_WAIT_POLL_SECONDS = 0.25
_WAIT_HEARTBEAT_SECONDS = 10.0

# The Claude Code Channels contract (research preview), verified against the docs
# (https://code.claude.com/docs/en/channels-reference) and the local `claude`
# build. A session that has subscribed to this server as a channel turns each
# such notification into its *next turn* -- the content is wrapped as
# ``<channel source="<server>" ...>content</channel>`` and injected as a meta
# prompt. That injected turn is what makes a recipient REACT to a message
# instead of waiting to poll. See the "Turn injection" section of the README.
_CHANNEL_METHOD = "notifications/claude/channel"

# The server-side capability that makes turn injection possible: a channel-aware
# client only registers a listener for ``_CHANNEL_METHOD`` if the server declared
# this experimental capability in its initialize response. Declared as ``{}``
# (its presence is the signal; there is no config). Harmless to advertise to a
# stock MCP client, which ignores experimental capabilities it doesn't know.
_CHANNEL_CAPABILITY = "claude/channel"


def _now() -> float:
    return time.time()


def _auto_name() -> str:
    """A short, unique-ish fallback address for a session that registers unnamed.

    The server can't observe a session's title, so rather than dead-end a bare
    "register me", we mint a usable handle (e.g. ``session-a3f9c1``). Store
    registration is an upsert keyed on the name, so a collision would silently
    merge two sessions onto one mailbox; 24 bits of entropy keeps that
    negligible for realistic multi-session use. A caller that wants a memorable
    name just passes one.
    """
    return f"session-{secrets.token_hex(3)}"


def _resolve_ttl() -> float:
    raw = os.environ.get("SWITCHBOARD_TTL")
    if not raw:
        return DEFAULT_TTL_SECONDS
    try:
        val = float(raw)
        return val if val > 0 else DEFAULT_TTL_SECONDS
    except ValueError:
        return DEFAULT_TTL_SECONDS


# Undelivered messages age out after this many seconds, pruned opportunistically
# (the same pattern as participant expiry). Overridable via $SWITCHBOARD_MSG_TTL;
# a value <= 0 disables age-out entirely.
DEFAULT_MSG_TTL_SECONDS = 7 * 86400.0

# Reject a send() whose body exceeds this many UTF-8 bytes -- a hygiene bound so a
# runaway sender can't bloat the store. Overridable via $SWITCHBOARD_MAX_BODY; a
# value <= 0 disables the cap.
DEFAULT_MAX_BODY_BYTES = 256 * 1024


def _resolve_msg_ttl() -> float:
    raw = os.environ.get("SWITCHBOARD_MSG_TTL")
    if raw is None or raw.strip() == "":
        return DEFAULT_MSG_TTL_SECONDS
    try:
        val = float(raw)
    except ValueError:
        return DEFAULT_MSG_TTL_SECONDS
    return val if val > 0 else 0.0  # 0 / negative -> age-out disabled


def _resolve_max_body() -> int:
    raw = os.environ.get("SWITCHBOARD_MAX_BODY")
    if raw is None or raw.strip() == "":
        return DEFAULT_MAX_BODY_BYTES
    try:
        # OverflowError guards against e.g. "1e309" -> float('inf') -> int(inf).
        val = int(float(raw))
    except (ValueError, OverflowError):
        return DEFAULT_MAX_BODY_BYTES
    return val if val > 0 else 0  # 0 / negative -> cap disabled


_PUSH_TRUE = ("1", "true", "yes", "on")
_PUSH_FALSE = ("0", "false", "no", "off")


def _resolve_push(*, daemon: bool) -> bool:
    """Whether to emit Channels push nudges (turn injection).

    ``$SWITCHBOARD_PUSH`` wins when set to a recognized truthy/falsy value.
    Otherwise the default is ``daemon`` -- push is ON in daemon mode (the only
    mode that can cross-push, and the whole reason ``serve`` exists) and OFF for
    per-session stdio (where there is never another session's connection to push
    to). Push is always best-effort: a recipient that isn't a subscribed channel
    simply doesn't react and falls back to durable poll, so defaulting it on in
    daemon mode costs nothing when the feature is unavailable.
    """
    raw = (os.environ.get("SWITCHBOARD_PUSH") or "").strip().lower()
    if raw in _PUSH_TRUE:
        return True
    if raw in _PUSH_FALSE:
        return False
    return daemon


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

    def __init__(
        self,
        store: Store,
        *,
        ttl: float = DEFAULT_TTL_SECONDS,
        board: str = "",
        msg_ttl: float = DEFAULT_MSG_TTL_SECONDS,
        max_body: int = DEFAULT_MAX_BODY_BYTES,
        daemon: bool = False,
    ):
        self.store = store
        self.ttl = ttl
        # The board this server is bound to. Purely informational here (isolation
        # is provided by pointing at a per-board database); surfaced in tool
        # responses so a session can see which switchboard it joined.
        self.board = (board or "").strip()
        # Hygiene bounds (see issue #17). msg_ttl ages out undelivered messages
        # opportunistically; max_body caps a single send. <= 0 disables either.
        self.msg_ttl = msg_ttl
        self.max_body = max_body
        # id(ctx.session) -> _Conn. Keyed by object identity because one
        # ServerSession maps to exactly one client connection for its lifetime.
        self._conns: dict[int, _Conn] = {}
        # Optional identity seeded from the environment so scripted worker
        # sessions can be launched pre-addressed (SWITCHBOARD_NAME/ROLE) without
        # an explicit register() call.
        self._default_name = (os.environ.get("SWITCHBOARD_NAME") or "").strip()
        self._default_role = (os.environ.get("SWITCHBOARD_ROLE") or "").strip()
        # Whether to emit Channels push nudges. Push only does anything in daemon
        # mode (a recipient connected to the same process) AND only when that
        # recipient is a Claude session subscribed to this server as a channel --
        # a stock MCP client just ignores the notification. It therefore defaults
        # ON in daemon mode (that is the entire point of `serve`) and OFF for
        # stdio, with $SWITCHBOARD_PUSH forcing either way. Tests set this
        # attribute directly. See _resolve_push().
        self._push_enabled = _resolve_push(daemon=daemon)

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

    def register(self, ctx: Context, name: str = "", role: str = "") -> dict:
        name = (name or "").strip()
        role = (role or "").strip()
        explicit_name = bool(name)
        assigned = False
        if not name:
            # No explicit name: fall back to a pre-seeded identity from the
            # environment. The tool description steers the caller to pass its
            # session title, but the server cannot observe that title -- so rather
            # than dead-end a bare "register me", mint a usable handle and say so.
            name = self._default_name
        if not name:
            name = _auto_name()
            assigned = True
        # Only inherit the env role when adopting an env/auto identity (i.e. no
        # explicit name was given). An explicit registration passes its role
        # through unchanged -- an empty role then lets Store.register() preserve
        # any existing role, so a bare re-register is a clean heartbeat that
        # never disturbs the role.
        if not role and not explicit_name:
            role = self._default_role
        p = self.store.register(name, role, now=_now())
        self._bind(ctx, p.name, p.role)
        payload = self._participants_payload()
        out = {
            "ok": True,
            "you": p.name,
            "role": p.role,
            "ttl_seconds": self.ttl,
            **({"board": self.board} if self.board else {}),
            "participants": payload,
        }
        if assigned:
            out["note"] = (
                f"No name was given, so I registered you as '{p.name}'. Pass "
                "name=... to choose your own address (e.g. your session title)."
            )
        hint = self._solo_hint(payload)
        if hint:
            out["hint"] = hint
        return out

    def participants(self, ctx: Context) -> dict:
        # A read is also a heartbeat for the caller, if registered.
        conn = self._conns.get(id(ctx.session))
        if conn is not None:
            self.store.touch(conn.name, now=_now())
        payload = self._participants_payload()
        out = {
            "participants": payload,
            "count": len(payload),
            **({"board": self.board} if self.board else {}),
        }
        hint = self._solo_hint(payload)
        if hint:
            out["hint"] = hint
        return out

    def send(self, ctx: Context, to: str, body: str, reply_to: Optional[int] = None) -> dict:
        conn = self._resolve(ctx)
        to = (to or "").strip()
        if not to:
            raise ValueError("`to` must be a non-empty address (a participant name or role).")
        self._check_body(body)
        now = _now()
        mid = self.store.send(to, body, sender=conn.name, reply_to=reply_to, now=now)
        self.store.touch(conn.name, now=now)
        out = {"id": mid, "to": to, "from": conn.name, "reply_to": reply_to}
        if not self._has_live_recipient(to):
            # Delivery is still durable -- this is a soft nudge that nobody is
            # currently reading `to`, which most often means a typo'd name/role.
            out["no_live_recipient"] = True
            out["warning"] = (
                f"No session is currently registered as '{to}'. The message is queued "
                "and will be delivered whenever one reads it, but if you expected an "
                "immediate reader, double-check the name/role with participants()."
            )
        return out

    async def _notify(
        self, ctx: Context, *, sender: str, to: str, mid: int, reply_to: Optional[int]
    ) -> bool:
        """Best-effort push nudge that makes any connected recipient(s) REACT.

        Returns True if at least one live delivery was handed to the transport.
        A channel-subscribed recipient turns this notification into its next turn
        (see ``_CHANNEL_METHOD``), so the ``content`` is written as an
        instruction: drain the durable inbox and handle what comes out.

        This is a *nudge*, not the message itself: the durable inbox row is the
        single source of truth, and the body is deliberately omitted. That keeps
        push consistent with drain-once -- when a role is addressed every
        connected member is nudged, but only the one that drains the row receives
        it; the others find an empty inbox, which the wording anticipates. (The
        one-round-trip optimization of inlining the body for a unique-name/single
        -reader target is intentionally declined: it would risk double-handling
        and split the source of truth. See the README.)

        No-op unless push is enabled (daemon mode).
        """
        if not self._push_enabled:
            return False
        targets = self._push_targets(to, exclude_session_id=id(ctx.session))
        if not targets:
            return False
        content = (
            f"A switchboard message just arrived for you from '{sender}' "
            f"(message id {mid}). React now: call inbox() to drain your mailbox, "
            "then read and act on what it returns -- if it's a question, reply "
            "with send(to, body, reply_to=<id>). This is an automatic nudge, not "
            "the message body; the message itself is in your durable inbox. If "
            "inbox() comes back empty, a peer on your role already handled it -- "
            "nothing to do."
        )
        # meta keys must be identifiers ([A-Za-z_][A-Za-z0-9_]*) or the client
        # silently drops them (they render as attributes on the <channel> tag).
        # No "source" key: the client already sets source="<server name>" on the
        # tag, so sending one would duplicate the attribute.
        meta = {
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
        self._check_body(body)
        now = _now()
        # Note whether anyone is live to answer *before* we send -- used to explain
        # a timeout below (offline recipient vs. a live one that just didn't reply).
        live_at_send = self._has_live_recipient(to)
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
                result = {
                    "you": conn.name,
                    "asked": to,
                    "question_id": qid,
                    "reply": None,
                    "timed_out": True,
                }
                if not live_at_send:
                    result["no_live_recipient"] = True
                    result["note"] = (
                        f"No session was registered as '{to}' when you asked, so no "
                        "reply is likely -- the name/role may be misspelled or that "
                        "session may be offline. Your question remains queued."
                    )
                return result
            await anyio.sleep(min(_WAIT_POLL_SECONDS, max(0.0, deadline - now)))

    async def broadcast(self, ctx: Context, body: str) -> dict:
        """Send ``body`` to every currently-live participant except the sender."""
        conn = self._resolve(ctx)
        self._check_body(body)
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

    def _check_body(self, body: Any) -> None:
        """Reject an over-large message body (a hygiene bound; see issue #17)."""
        if self.max_body > 0 and isinstance(body, str):
            n = len(body.encode("utf-8"))
            if n > self.max_body:
                raise ValueError(
                    f"Message body is {n} bytes, over the {self.max_body}-byte limit "
                    "(set $SWITCHBOARD_MAX_BODY to change it, or 0 to disable). Send a "
                    "shorter message, or a reference/path instead of inlining large content."
                )

    def _has_live_recipient(self, to: str) -> bool:
        """True if some live participant reads ``to`` (by its name or its role)."""
        now = _now()
        for p in self.store.participants(now=now, ttl=self.ttl):
            if p.name == to or (p.role and p.role == to):
                return True
        return False

    def _solo_hint(self, payload: list[dict]) -> Optional[str]:
        """A nudge shown when a session is alone on its board (nobody to talk to)."""
        if len(payload) != 1:
            return None
        where = f" (board '{self.board}')" if self.board else ""
        return (
            f"You're the only session here{where}. Open another Claude Code session on "
            "the same board -- the same repo, or one started with the same "
            "SWITCHBOARD_BOARD -- and register it to start talking."
        )

    def _prune_conns(self, live_names: set[str]) -> None:
        """Evict daemon registry entries whose participant is no longer live.

        The daemon keys ``_conns`` on the live ``ServerSession`` object, so a
        client that disconnects without calling ``unregister()`` would otherwise
        linger forever (issue #18). A connected session heartbeats on every tool
        call, keeping its participant live; a departed one expires from the store
        within the TTL and is swept here. Bounds the registry to (live sessions +
        at most one TTL window of stragglers) -- no unbounded leak.
        """
        stale = [sid for sid, c in self._conns.items() if c.name not in live_names]
        for sid in stale:
            del self._conns[sid]

    def _participants_payload(self) -> list[dict]:
        now = _now()
        # Opportunistic housekeeping so neither the participant table nor the
        # undelivered-message backlog grows unbounded. Message age-out mirrors
        # participant expiry: pruned during normal operation, never on a timer.
        self.store.prune_participants(now=now, ttl=self.ttl)
        if self.msg_ttl > 0:
            self.store.prune_messages(older_than=now - self.msg_ttl)
        live = self.store.participants(now=now, ttl=self.ttl)
        self._prune_conns({p.name for p in live})
        out = []
        for p in live:
            d = p.to_dict()
            d["idle_seconds"] = round(now - p.last_seen, 1)
            out.append(d)
        return out


# -- FastMCP wiring ---------------------------------------------------------

# Tool docstrings are what Claude reads to decide when/how to call each tool, so
# they double as the user-facing tool descriptions. Keep them action-oriented.


def _declare_channel_capability(mcp: FastMCP) -> None:
    """Advertise the ``claude/channel`` capability in the initialize response.

    Turn injection only happens if the recipient's client saw this experimental
    capability at connect time and registered a listener for ``_CHANNEL_METHOD``.
    FastMCP builds its InitializationOptions via the underlying low-level
    server's ``create_initialization_options()`` (both the stdio and
    streamable-http transports route through it and call it with no arguments),
    which takes an ``experimental_capabilities`` dict. We wrap that bound method
    to inject ours while preserving anything an explicit caller passes.
    """
    low = mcp._mcp_server  # the mcp.server.lowlevel.Server FastMCP wraps
    original = low.create_initialization_options

    def with_channel_capability(notification_options=None, experimental_capabilities=None):
        # Force our capability last so a caller can't override it to a non-{}
        # value (the client keys purely on its presence; the value must be {}).
        merged = {**(experimental_capabilities or {}), _CHANNEL_CAPABILITY: {}}
        return original(notification_options, merged)

    low.create_initialization_options = with_channel_capability  # type: ignore[assignment]


def build_server(
    store: Optional[Store] = None,
    *,
    ttl: Optional[float] = None,
    board: str = "",
    msg_ttl: Optional[float] = None,
    max_body: Optional[int] = None,
    daemon: bool = False,
) -> FastMCP:
    store = store if store is not None else Store()
    sb = Switchboard(
        store,
        ttl=ttl if ttl is not None else _resolve_ttl(),
        board=board,
        # Match the ttl pattern: resolve from the environment when not supplied,
        # so programmatic callers behave like the CLI.
        msg_ttl=msg_ttl if msg_ttl is not None else _resolve_msg_ttl(),
        max_body=max_body if max_body is not None else _resolve_max_body(),
        daemon=daemon,
    )

    mcp = FastMCP(
        "switchboard-relay",
        instructions=(
            "Shared message bus for independent Claude Code sessions. Call "
            "register() once to claim an address -- pass a name (e.g. "
            '"lead" or "worker:feature-x") or, if none is given, register under '
            "your session's title -- then send(to, body) to message another "
            "session and inbox()/wait() to receive. For a question that needs an "
            "answer, ask(to, body) sends and blocks for the reply in one call. "
            "Messages are durable: they wait in the recipient's mailbox until read. "
            "The switchboard is scoped to this project by default, so you only see "
            "sessions on the same board."
        ),
    )
    # Expose the wiring for tests and daemon introspection.
    mcp._switchboard_relay = sb  # type: ignore[attr-defined]

    # Declare the Channels capability so a subscribed recipient will inject our
    # push nudges as turns (see _declare_channel_capability / the README). Done
    # for every transport: harmless to advertise on stdio (which never pushes),
    # and it keeps the initialize response uniform.
    _declare_channel_capability(mcp)

    @mcp.tool()
    def register(name: str = "", role: str = "", ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        """Claim an address on the switchboard for this session.

        `name` is your address that others send to (e.g. "lead",
        "worker:feature-x"). It is optional: if the user did not give you a
        specific name, register under your current session's title (a short slug
        of it) so you are addressable by it -- and if you truly have no name to
        offer, one is assigned for you (a "session-..." handle, returned in
        `you`). `role` is an optional shared address for a group (e.g. "worker")
        -- a message sent to a role is delivered to any participant that reads
        with that role. Re-call to refresh your presence (a heartbeat) or change
        your role. Returns the `board` you joined and the current live
        participants (with a `hint` if you're the only one).
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
        not registered yet. Returns the new message `id`; if no live participant
        currently reads `to`, the result also carries `no_live_recipient` and a
        `warning` (most often a typo'd name/role).
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
        in the recipient's inbox); if nobody was even registered as `to` when you
        asked, the result also carries `no_live_recipient` so you can tell "wrong
        address" from "still thinking". Other messages in your inbox are untouched.
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
        help="Path to the SQLite database (raw override; wins over --board and "
        "$SWITCHBOARD_DB). Default: the current board's file.",
    )
    parser.add_argument(
        "--board",
        default=None,
        help="Switchboard/board to target (default: $SWITCHBOARD_BOARD or the "
        "current project). Each board is an isolated bus at "
        "~/.claude/switchboard/<board>.db.",
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
        help="Run as a shared HTTP daemon (streamable-http) so send() can push a live nudge "
        "into a connected session (the 'responsive lead' setup; needs Claude Code Channels). "
        "See the README.",
    )
    serve.add_argument("--host", default=os.environ.get("SWITCHBOARD_HOST", "127.0.0.1"))
    serve.add_argument("--port", type=int, default=int(os.environ.get("SWITCHBOARD_PORT", "8765")))

    # Human-facing inspection commands (read the DB directly; no MCP server).
    sub.add_parser(
        "doctor",
        help="One-shot 'why isn't this working?' diagnostics for the current board.",
    )
    sub.add_parser("boards", help="List local switchboards (boards) and their live participants.")
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
    """Open an existing store for inspection, or None if it does not exist yet.

    Returns None (creating nothing) when the database file is absent, so an
    inspection command never materializes a db on a system that has not started
    the server. When it does exist, it is opened with ``init_schema=False`` -- no
    WAL-mode switch, no CREATE TABLE -- so a read-only command (doctor,
    participants, tail) writes no schema state. (`prune` also uses this and then
    issues its DELETEs; the tables already exist.)
    """
    p = Path(str(db_path)).expanduser()
    if str(p) != ":memory:" and not p.exists():
        return None
    return Store(db_path, init_schema=False)


def _discover_boards() -> list[tuple[str, Path]]:
    """(board_name, db_path) for every local board file, plus a legacy DB if any.

    Sorted by name. Purely a read of the filesystem -- creates nothing.
    """
    found: dict[str, Path] = {}
    d = boards_dir()
    if d.is_dir():
        for f in sorted(d.glob("*.db")):
            found[f.stem] = f
    legacy = legacy_db_path()
    if legacy.exists():
        found.setdefault(legacy.stem, legacy)  # shown as "switchboard"
    return sorted(found.items())


def _cli_boards(ttl: float) -> int:
    boards = _discover_boards()
    if not boards:
        print("No switchboards yet.")
        return 0
    now = _now()
    for name, path in boards:
        # Every discovered path exists (we just globbed them), so opening is safe.
        live = len(Store(path).participants(now=now, ttl=ttl))
        print(f"{name}  ({live} live)  {path}")
    return 0


# Env vars worth surfacing in `doctor` -- everything that steers behavior.
_DOCTOR_ENV_VARS = (
    "SWITCHBOARD_DB",
    "SWITCHBOARD_BOARD",
    "SWITCHBOARD_TTL",
    "SWITCHBOARD_MSG_TTL",
    "SWITCHBOARD_MAX_BODY",
    "SWITCHBOARD_NAME",
    "SWITCHBOARD_ROLE",
    "SWITCHBOARD_PUSH",
)


def _count_queued(store: Store, live_addresses: set[str]) -> tuple[int, int]:
    """(total queued messages, queued to an address with no live reader).

    Pages through the whole pending backlog so the counts are exact even beyond
    one query window.
    """
    total = 0
    dead = 0
    last = 0
    while True:
        batch = store.pending_messages(since=last)
        if not batch:
            return total, dead
        for m in batch:
            total += 1
            if m.to not in live_addresses:
                dead += 1
        last = batch[-1].id


def _cli_doctor(target, ttl: float) -> int:
    """Print resolution, env, live peers, and queued counts, plus heuristic hints."""
    print("switchboard-relay doctor")
    print(f"  board:       {target.board}")
    print(f"  db:          {target.db_path}")
    print(f"  resolved by: {target.source}")

    print("  env:")
    for var in _DOCTOR_ENV_VARS:
        val = os.environ.get(var)
        print(f"    {var}={val}" if val not in (None, "") else f"    {var} (unset)")

    store = _open_store_ro(target.db_path)
    if store is None:
        print("  db file:     does not exist yet (nobody has registered on this board)")
        print(
            "\nhint: this board has no state yet. Register a session here, or you may be on a "
            "different board than you expect -- run `switchboard-relay boards` to compare."
        )
        return 0

    now = _now()
    parts = store.participants(now=now, ttl=ttl)
    print("  db file:     exists")
    print(f"  live participants: {len(parts)}")
    for p in parts:
        role = f" [{p.role}]" if p.role else ""
        print(f"    {p.name}{role}  idle={round(now - p.last_seen, 1)}s")

    live_addresses = {p.name for p in parts} | {p.role for p in parts if p.role}
    total_queued, dead_queued = _count_queued(store, live_addresses)
    print(f"  pending (undelivered) messages: {total_queued}")

    hints = []
    if len(parts) <= 1:
        who = "no live participants" if not parts else "only 1 live participant"
        hints.append(
            f"{who} on this board -- if a peer should be here, it is likely on a DIFFERENT board. "
            "Compare with `switchboard-relay boards`, or put both sessions on the same "
            "$SWITCHBOARD_BOARD."
        )
    if dead_queued:
        hints.append(
            f"{dead_queued} message(s) are queued to an address with no live reader -- likely a "
            "misspelled name/role in `to`, or the recipient isn't running. Check the `to` "
            "addresses (`switchboard-relay tail`) against the live list above."
        )
    if hints:
        print("\nhints:")
        for h in hints:
            print(f"  - {h}")
    else:
        print("\nlooks healthy: live peers are present and nothing is stuck to a dead address.")
    return 0


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

    ttl = args.ttl if args.ttl is not None else _resolve_ttl()

    # `boards` lists every local board and needs no single target.
    if args.cmd == "boards":
        return _cli_boards(ttl)

    # Everything else operates on one resolved board: --db > --board >
    # $SWITCHBOARD_DB > $SWITCHBOARD_BOARD > the current project.
    target = describe_target(db_arg=args.db, board_arg=args.board)
    db_path = target.db_path

    # Human-facing inspection commands read the DB directly -- no server, and no
    # side effect of creating the DB if it does not exist yet.
    if args.cmd == "doctor":
        return _cli_doctor(target, ttl)
    if args.cmd == "participants":
        return _cli_participants(db_path, ttl)
    if args.cmd == "tail":
        return _cli_tail(db_path, follow=args.follow, interval=args.interval)
    if args.cmd == "prune":
        return _cli_prune(db_path, ttl, args.older_than_days)

    store = Store(db_path)
    # Only the daemon can cross-push, so push (turn injection) defaults on there
    # and off for stdio; see _resolve_push().
    daemon = args.cmd == "serve"
    mcp = build_server(
        store,
        ttl=ttl,
        board=target.board,
        msg_ttl=_resolve_msg_ttl(),
        max_body=_resolve_max_body(),
        daemon=daemon,
    )

    if daemon:
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        push_on = mcp._switchboard_relay._push_enabled  # type: ignore[attr-defined]
        print(
            f"switchboard-relay daemon (streamable-http) on http://{args.host}:{args.port}"
            f"{mcp.settings.streamable_http_path}  board={target.board}  db={store.db_path}",
            file=sys.stderr,
        )
        # The push path is a Channels research preview: a recipient only reacts
        # to a send() if that session subscribed to switchboard as a channel.
        # Spell out the verified recipe so the daemon is self-documenting.
        print(
            "  turn injection (push): "
            + ("ENABLED" if push_on else "disabled (set SWITCHBOARD_PUSH=1 to enable)")
            + " -- for a session to react, launch it subscribed as a channel:\n"
            "    claude --dangerously-load-development-channels server:switchboard-relay\n"
            "  (needs Claude Code Channels; durable poll via wait()/inbox() is the "
            "fallback. See the README's 'Turn injection' section.)",
            file=sys.stderr,
        )
        mcp.run(transport="streamable-http")
    else:
        # Default: stdio, one process per Claude Code session.
        mcp.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
