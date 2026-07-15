"""Server-layer unit tests.

These exercise the :class:`~switchboard_relay.server.Switchboard` glue directly with a
lightweight fake Context, so they cover identity binding, the opt-in push path,
and role targeting without standing up the full MCP transport (that is covered
by test_integration.py).
"""

from __future__ import annotations

import re
import time

import pytest

from switchboard_relay.server import (
    _CHANNEL_CAPABILITY,
    _CHANNEL_METHOD,
    Switchboard,
    _resolve_push,
    build_server,
)
from switchboard_relay.store import Store

# The channel client only keeps meta keys that are plain identifiers; anything
# else is silently dropped before the <channel> tag is rendered. Mirror that
# rule here so a regression into e.g. a hyphenated key is caught in CI.
_META_KEY_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


class FakeSession:
    """Stand-in for an MCP ServerSession that records pushed notifications."""

    def __init__(self):
        self.sent: list = []

    async def send_notification(self, notification, related_request_id=None):
        self.sent.append(notification)


class FakeCtx:
    """Minimal Context: identity is keyed off `id(ctx.session)`."""

    def __init__(self, session: FakeSession):
        self.session = session


@pytest.fixture()
def board(tmp_path):
    return Switchboard(Store(tmp_path / "sb.db"), ttl=300)


def _ctx() -> FakeCtx:
    return FakeCtx(FakeSession())


# -- identity ---------------------------------------------------------------


def test_resolve_requires_registration(board):
    with pytest.raises(ValueError, match="not registered"):
        board.inbox(_ctx())


def test_register_auto_assigns_name_when_none_given(board):
    # A bare register() no longer dead-ends: the server can't see the session
    # title, so it mints a usable handle, says so, and binds the session to it.
    ctx = _ctx()
    out = board.register(ctx, "   ")
    assert re.fullmatch(r"session-[0-9a-f]{6}", out["you"])
    assert "session-" in out["note"]
    assert board.inbox(ctx)["you"] == out["you"]  # bound and usable


def test_register_explicit_name_has_no_auto_note(board):
    assert "note" not in board.register(_ctx(), "lead")


def test_register_explicit_heartbeat_not_overridden_by_env_role(tmp_path, monkeypatch):
    # $SWITCHBOARD_ROLE must not clobber an explicitly-named session's role on a
    # bare re-register: a nameless re-call would inherit it, but an explicit one
    # passes an empty role through so Store preserves the existing role.
    monkeypatch.setenv("SWITCHBOARD_ROLE", "worker")
    sb = Switchboard(Store(tmp_path / "sb.db"), ttl=300)
    ctx = _ctx()
    sb.register(ctx, "lead", "coordinator")  # explicit role
    out = sb.register(ctx, "lead")  # bare heartbeat; env role is set to "worker"
    assert out["role"] == "coordinator"  # preserved, NOT reset to the env role


def test_register_falls_back_to_env_name(tmp_path, monkeypatch):
    # A nameless register() adopts the environment-seeded identity when present --
    # that is not an "assigned" name, so there is no note.
    monkeypatch.setenv("SWITCHBOARD_NAME", "lead")
    monkeypatch.setenv("SWITCHBOARD_ROLE", "coordinator")
    sb = Switchboard(Store(tmp_path / "sb.db"), ttl=300)
    out = sb.register(_ctx())  # no name, no role
    assert out["you"] == "lead"
    assert out["role"] == "coordinator"
    assert "note" not in out


# -- solo hint --------------------------------------------------------------


def test_register_hints_when_alone(board):
    out = board.register(_ctx(), "lead")
    assert "only session" in out["hint"].lower()


def test_no_solo_hint_with_multiple_participants(board):
    board.register(_ctx(), "lead")
    out = board.register(_ctx(), "worker:1", "worker")
    assert "hint" not in out  # two live now, so no "you're alone" nudge


def test_participants_hints_when_alone(board):
    ctx = _ctx()
    board.register(ctx, "lead")
    assert "hint" in board.participants(ctx)


# -- unknown-recipient warning ---------------------------------------------


def test_send_warns_when_no_live_recipient(board):
    # Sender registers with an empty role, exercising the empty-role skip in the
    # liveness check; the target address has nobody reading it.
    ctx = _ctx()
    board.register(ctx, "lead")
    out = board.send(ctx, "nobody", "hello?")
    assert out["no_live_recipient"] is True
    assert "warning" in out


def test_send_no_warning_for_live_name(board):
    a, b = _ctx(), _ctx()
    board.register(a, "lead")
    board.register(b, "worker:x", "worker")
    assert "no_live_recipient" not in board.send(a, "worker:x", "hi")


def test_send_no_warning_for_live_role(board):
    a, b = _ctx(), _ctx()
    board.register(a, "lead")
    board.register(b, "worker:x", "worker")
    assert "no_live_recipient" not in board.send(a, "worker", "hi")  # matched by role


def test_ask_timeout_flags_unknown_recipient(board):
    ctx = _ctx()
    board.register(ctx, "worker:x", "worker")
    out = _run(board.ask, ctx, "leed", "help?", 0.05)  # nobody is "leed"
    assert out["timed_out"] is True
    assert out["no_live_recipient"] is True
    assert "note" in out


def test_ask_timeout_no_flag_when_recipient_live(board):
    w, lead = _ctx(), _ctx()
    board.register(w, "worker:x", "worker")
    board.register(lead, "lead")  # live, but does not reply within the timeout
    out = _run(board.ask, w, "lead", "hi", 0.05)
    assert out["timed_out"] is True
    assert "no_live_recipient" not in out


# -- hygiene bounds: body-size cap (#17) ------------------------------------


def _sb(tmp_path, **kw):
    return Switchboard(Store(tmp_path / "sb.db"), ttl=300, **kw)


def test_send_rejects_oversized_body(tmp_path):
    sb = _sb(tmp_path, max_body=10)
    ctx = _ctx()
    sb.register(ctx, "lead")
    with pytest.raises(ValueError, match="over the 10-byte limit"):
        sb.send(ctx, "worker", "x" * 11)


def test_send_allows_body_at_the_limit(tmp_path):
    sb = _sb(tmp_path, max_body=10)
    ctx = _ctx()
    sb.register(ctx, "lead")
    assert isinstance(sb.send(ctx, "worker", "x" * 10)["id"], int)


def test_body_cap_disabled_when_zero(tmp_path):
    sb = _sb(tmp_path, max_body=0)
    ctx = _ctx()
    sb.register(ctx, "lead")
    assert isinstance(sb.send(ctx, "worker", "x" * 5000)["id"], int)


def test_ask_and_broadcast_also_enforce_body_cap(tmp_path):
    sb = _sb(tmp_path, max_body=5)
    ctx = _ctx()
    sb.register(ctx, "lead", "worker")
    with pytest.raises(ValueError, match="byte limit"):
        _run(sb.ask, ctx, "peer", "toolong", 0.05)
    with pytest.raises(ValueError, match="byte limit"):
        _run(sb.broadcast, ctx, "toolong")


def test_check_body_ignores_non_string(tmp_path):
    # A non-string body bypasses the size check; Store.send enforces the type.
    sb = _sb(tmp_path, max_body=1)
    sb._check_body(12345)  # must not raise


# -- hygiene bounds: undelivered-message TTL (#17) --------------------------


def test_msg_ttl_ages_out_stale_queued_messages(tmp_path):
    sb = _sb(tmp_path, msg_ttl=1.0)
    now = time.time()
    sb.store.send("lead", "ancient", sender="w", now=now - 1000)  # older than msg_ttl
    sb.store.send("lead", "fresh", sender="w", now=now)  # recent
    sb.register(_ctx(), "someone")  # normal op triggers opportunistic prune
    assert [m.body for m in sb.store.inbox("lead", peek=True)] == ["fresh"]


def test_msg_ttl_zero_disables_ageout(tmp_path):
    sb = _sb(tmp_path, msg_ttl=0)
    sb.store.send("lead", "ancient", sender="w", now=time.time() - 10**9)
    sb.register(_ctx(), "someone")
    assert [m.body for m in sb.store.inbox("lead", peek=True)] == ["ancient"]


# -- daemon registry eviction (#18) -----------------------------------------


def test_conns_evicted_when_participant_expires(tmp_path):
    sb = Switchboard(Store(tmp_path / "sb.db"), ttl=0.02)
    ctx = _ctx()
    sb.register(ctx, "ghost")
    assert id(ctx.session) in sb._conns
    time.sleep(0.05)  # let ghost's participant expire past the tiny TTL
    sb.participants(_ctx())  # any participants()/register triggers the sweep
    assert id(ctx.session) not in sb._conns


def test_register_and_participants_report_board(tmp_path):
    sb = Switchboard(Store(tmp_path / "sb.db"), ttl=300, board="proj-abc123")
    reg = sb.register(_ctx(), "lead")
    assert reg["board"] == "proj-abc123"
    assert sb.participants(_ctx())["board"] == "proj-abc123"


def test_board_omitted_from_payload_when_unset(board):
    # The default (unlabeled) board adds no "board" key, so payloads stay lean.
    assert "board" not in board.register(_ctx(), "lead")
    assert "board" not in board.participants(_ctx())


def test_send_rejects_empty_recipient(board):
    ctx = _ctx()
    board.register(ctx, "lead")
    with pytest.raises(ValueError, match="non-empty address"):
        board.send(ctx, "", "body")


def test_participants_before_register_returns_empty(board):
    # participants() does not require registration; an unregistered caller just
    # sees the (empty) live list without heartbeating anything.
    out = board.participants(_ctx())
    assert out == {"participants": [], "count": 0}


# -- wait() -----------------------------------------------------------------


def test_wait_returns_immediately_when_message_present(board):
    ctx = _ctx()
    board.register(ctx, "worker:x", "worker")
    board.store.send("worker:x", "already here", sender="lead", now=1.0)
    # A non-numeric timeout falls back to the default, but a pending message
    # means wait() returns at once without any real blocking.
    out = _run(board.wait, ctx, "not-a-number")
    assert out["timed_out"] is False
    assert [m["body"] for m in out["messages"]] == ["already here"]


def test_wait_times_out_after_polling(board):
    ctx = _ctx()
    board.register(ctx, "lead")
    out = _run(board.wait, ctx, 0.05)  # no message: polls, then times out
    assert out["timed_out"] is True
    assert out["messages"] == []


# -- ask / broadcast / unregister ------------------------------------------


def test_ask_times_out_without_reply(board):
    ctx = _ctx()
    board.register(ctx, "worker:x", "worker")
    out = _run(board.ask, ctx, "lead", "are you there?", 0.05)
    assert out["timed_out"] is True
    assert out["reply"] is None
    # The question is still durably queued for the (absent) recipient.
    assert board.store.has_messages("lead")


async def test_ask_returns_reply_when_it_arrives(board):
    import anyio

    asker, responder = _ctx(), _ctx()
    board.register(asker, "worker:x", "worker")
    board.register(responder, "lead")

    async def respond():
        for _ in range(200):
            pending = board.store.pending_messages()
            q = next((m for m in pending if m.to == "lead"), None)
            if q is not None:
                board.send(responder, "worker:x", "the answer is 42", q.id)
                return
            await anyio.sleep(0.005)

    async with anyio.create_task_group() as tg:
        tg.start_soon(respond)
        out = await board.ask(asker, "lead", "what is the answer?", 5.0)

    assert out["timed_out"] is False
    assert out["reply"]["body"] == "the answer is 42"
    assert out["reply"]["reply_to"] == out["question_id"]


def test_ask_rejects_empty_recipient(board):
    ctx = _ctx()
    board.register(ctx, "worker")
    with pytest.raises(ValueError, match="non-empty address"):
        _run(board.ask, ctx, "", "q")


def test_push_enabled_but_no_connected_target_is_not_live(board):
    board._push_enabled = True
    a = _ctx()
    board.register(a, "lead")
    # Nobody is connected under "nobody-here", so push finds no target.
    res = _run(board.send_async, a, "nobody-here", "hi")
    assert res["delivered_live"] is False


def test_broadcast_reaches_all_live_except_sender(board):
    a, b, c = _ctx(), _ctx(), _ctx()
    board.register(a, "lead")
    board.register(b, "worker:1", "worker")
    board.register(c, "worker:2", "worker")

    out = _run(board.broadcast, a, "all hands")
    assert out["count"] == 2
    assert {d["to"] for d in out["delivered"]} == {"worker:1", "worker:2"}
    assert [m["body"] for m in board.inbox(b)["messages"]] == ["all hands"]
    assert [m["body"] for m in board.inbox(c)["messages"]] == ["all hands"]
    assert board.inbox(a)["messages"] == []  # sender does not receive its own broadcast


def test_unregister_removes_from_registry(board):
    ctx = _ctx()
    board.register(ctx, "lead")
    out = board.unregister(ctx)
    assert out == {"ok": True, "was_registered": True, "you": "lead"}
    # Gone from the live list, and identity no longer resolves.
    assert board.participants(_ctx())["participants"] == []
    with pytest.raises(ValueError, match="not registered"):
        board.inbox(ctx)


def test_unregister_when_never_registered(board):
    out = board.unregister(_ctx())
    assert out == {"ok": True, "was_registered": False, "you": None}


def test_unregister_uses_env_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("SWITCHBOARD_NAME", "worker:seed")
    sb = Switchboard(Store(tmp_path / "sb.db"), ttl=300)
    sb.store.register("worker:seed", "", now=1.0)
    # A session that never explicitly registered still unregisters its seeded id.
    out = sb.unregister(_ctx())
    assert out == {"ok": True, "was_registered": True, "you": "worker:seed"}


def test_wait_survives_drained_race(board, monkeypatch):
    # has_messages() says yes, but another reader drains the row before our
    # inbox() call, so it comes back empty. wait() must NOT falsely report a
    # message -- it falls through and (here, with a 0s deadline) times out.
    ctx = _ctx()
    board.register(ctx, "lead")
    monkeypatch.setattr(board.store, "has_messages", lambda *a, **k: True)
    monkeypatch.setattr(board.store, "inbox", lambda *a, **k: [])
    out = _run(board.wait, ctx, 0.0)
    assert out["timed_out"] is True
    assert out["messages"] == []


def test_register_binds_identity(board):
    ctx = _ctx()
    out = board.register(ctx, "lead", "coordinator")
    assert out["you"] == "lead"
    assert out["role"] == "coordinator"
    # Now inbox resolves without error.
    assert board.inbox(ctx)["you"] == "lead"


def test_distinct_sessions_have_distinct_identities(board):
    a, b = _ctx(), _ctx()
    board.register(a, "lead")
    board.register(b, "worker:x", "worker")
    board.send(a, "worker:x", "hi")
    # b drains its own mailbox; a's is empty.
    assert [m["body"] for m in board.inbox(b)["messages"]] == ["hi"]
    assert board.inbox(a)["messages"] == []


def test_env_seeded_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("SWITCHBOARD_NAME", "worker:seed")
    monkeypatch.setenv("SWITCHBOARD_ROLE", "worker")
    sb = Switchboard(Store(tmp_path / "sb.db"), ttl=300)
    ctx = _ctx()
    # No explicit register() -- identity comes from the environment.
    out = sb.inbox(ctx)
    assert out["you"] == "worker:seed"
    assert any(p["name"] == "worker:seed" for p in sb.participants(ctx)["participants"])


# -- send / participants ----------------------------------------------------


def test_send_returns_id_and_no_live_delivery_by_default(board):
    a, b = _ctx(), _ctx()
    board.register(a, "lead")
    board.register(b, "worker")

    import anyio

    res = anyio.run(board.send_async, a, "worker", "ping")
    assert isinstance(res["id"], int)
    assert res["from"] == "lead"
    assert res["delivered_live"] is False  # push disabled by default
    assert b.session.sent == []


def test_participants_lists_live_and_counts(board):
    a, b = _ctx(), _ctx()
    board.register(a, "lead", "coordinator")
    board.register(b, "worker:1", "worker")
    out = board.participants(a)
    names = {p["name"] for p in out["participants"]}
    assert names == {"lead", "worker:1"}
    assert out["count"] == 2
    assert all("idle_seconds" in p for p in out["participants"])


# -- opt-in push ------------------------------------------------------------


def _run(coro_fn, *args):
    import anyio

    return anyio.run(coro_fn, *args)


def test_push_delivers_to_connected_recipient_when_enabled(board):
    board._push_enabled = True
    a, b = _ctx(), _ctx()
    board.register(a, "lead")
    board.register(b, "worker:x", "worker")

    res = _run(board.send_async, a, "worker:x", "hello there")
    assert res["delivered_live"] is True
    assert len(b.session.sent) == 1

    dumped = b.session.sent[0].model_dump(by_alias=True, mode="json", exclude_none=True)
    assert dumped["method"] == _CHANNEL_METHOD
    meta = dumped["params"]["meta"]
    assert meta["msg_from"] == "lead"
    assert meta["msg_to"] == "worker:x"
    assert meta["msg_id"] == str(res["id"])
    # No "source" meta key: the client sets source="<server name>" on the tag
    # itself, so sending one would duplicate the attribute.
    assert "source" not in meta
    # Every meta key must be a bare identifier or the client drops it silently.
    assert all(_META_KEY_RE.match(k) for k in meta)
    # The nudge makes the recipient REACT, not just "you have mail": it tells it
    # to drain inbox() and act on / reply to what it gets.
    content = dumped["params"]["content"].lower()
    assert "inbox()" in content
    assert "react" in content or "act on" in content
    # It is a body-less nudge, NOT the message content -- the durable row is the
    # single source of truth (see the role fan-out drain-once test below).
    assert "hello there" not in dumped["params"]["content"]
    # The message body is delivered durably, drained once via inbox().
    assert [m["body"] for m in board.inbox(b)["messages"]] == ["hello there"]


def test_push_includes_reply_to_in_meta(board):
    board._push_enabled = True
    a, b = _ctx(), _ctx()
    board.register(a, "lead")
    board.register(b, "worker:x", "worker")
    res = _run(board.send_async, a, "worker:x", "re: your question", 41)
    dumped = b.session.sent[0].model_dump(by_alias=True, mode="json", exclude_none=True)
    assert dumped["params"]["meta"]["reply_to"] == "41"
    assert res["reply_to"] == 41


def test_push_by_role_targets_all_matching_and_excludes_sender(board):
    board._push_enabled = True
    lead = _ctx()
    w1, w2 = _ctx(), _ctx()
    board.register(lead, "lead")
    board.register(w1, "worker:1", "worker")
    board.register(w2, "worker:2", "worker")

    res = _run(board.send_async, lead, "worker", "all-hands")
    assert res["delivered_live"] is True
    assert len(w1.session.sent) == 1
    assert len(w2.session.sent) == 1
    assert lead.session.sent == []  # sender never pushed to itself


def test_push_by_role_nudges_all_but_durable_drain_yields_one_handler(board):
    # The drain-once guarantee under role addressing: a message sent to a role
    # with two connected members nudges BOTH (so whichever reacts first handles
    # it), but the single durable row is drained by exactly one -- the body is
    # never delivered to both. This is why the nudge omits the body.
    board._push_enabled = True
    lead = _ctx()
    w1, w2 = _ctx(), _ctx()
    board.register(lead, "lead")
    board.register(w1, "worker:1", "worker")
    board.register(w2, "worker:2", "worker")

    _run(board.send_async, lead, "worker", "who can take this?")
    assert len(w1.session.sent) == 1 and len(w2.session.sent) == 1  # both nudged

    # Both react by draining; exactly one gets the message, the other gets empty.
    drained = board.inbox(w1)["messages"] + board.inbox(w2)["messages"]
    assert [m["body"] for m in drained] == ["who can take this?"]


def test_push_never_raises_on_bad_session(board):
    board._push_enabled = True

    class Boom:
        async def send_notification(self, *a, **k):
            raise RuntimeError("stream closed")

    sender, recip = _ctx(), FakeCtx(Boom())
    board.register(sender, "lead")
    board.register(recip, "worker:x", "worker")
    # Must not raise even though the recipient's session errors on push.
    res = _run(board.send_async, sender, "worker:x", "hi")
    assert res["delivered_live"] is False
    # Durable delivery still succeeded.
    assert board.store.has_messages("worker:x", "worker")


# -- channel capability + push defaults -------------------------------------


def test_build_server_declares_channel_capability():
    # Turn injection only works if the server advertises the Channels capability
    # in its initialize response; both transports build options via this call.
    mcp = build_server(Store(":memory:"), ttl=300, board="cap")
    caps = mcp._mcp_server.create_initialization_options().capabilities
    # Assert the key is present and exactly {} rather than exact-matching the
    # whole dict, so this doesn't break if the SDK starts advertising other
    # experimental capabilities by default.
    assert (caps.experimental or {}).get(_CHANNEL_CAPABILITY) == {}
    # Declaring it must not clobber the real tools capability.
    assert caps.tools is not None
    # Our capability is forced last: a caller can't override it to a non-{}
    # value, but their other experimental capabilities are preserved.
    caps2 = mcp._mcp_server.create_initialization_options(
        experimental_capabilities={_CHANNEL_CAPABILITY: {"bogus": 1}, "other": {}}
    ).capabilities
    assert caps2.experimental[_CHANNEL_CAPABILITY] == {}
    assert caps2.experimental["other"] == {}


def test_push_defaults_on_in_daemon_mode_off_for_stdio(monkeypatch):
    monkeypatch.delenv("SWITCHBOARD_PUSH", raising=False)
    assert Switchboard(Store(":memory:"), daemon=True)._push_enabled is True
    assert Switchboard(Store(":memory:"), daemon=False)._push_enabled is False


def test_push_env_overrides_daemon_default(monkeypatch):
    monkeypatch.setenv("SWITCHBOARD_PUSH", "0")
    assert _resolve_push(daemon=True) is False  # explicit off beats daemon-on
    monkeypatch.setenv("SWITCHBOARD_PUSH", "on")
    assert _resolve_push(daemon=False) is True  # explicit on beats stdio-off


def test_push_meta_keys_are_identifiers(board):
    # Guard: every meta key we emit must survive the client's identifier filter,
    # including the reply_to key that only appears on threaded replies.
    board._push_enabled = True
    a, b = _ctx(), _ctx()
    board.register(a, "lead")
    board.register(b, "worker:x", "worker")
    _run(board.send_async, a, "worker:x", "threaded", 7)
    dumped = b.session.sent[0].model_dump(by_alias=True, mode="json", exclude_none=True)
    meta = dumped["params"]["meta"]
    assert "reply_to" in meta
    assert all(_META_KEY_RE.match(k) for k in meta)
