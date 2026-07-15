"""Server-layer unit tests.

These exercise the :class:`~switchboard.server.Switchboard` glue directly with a
lightweight fake Context, so they cover identity binding, the opt-in push path,
and role targeting without standing up the full MCP transport (that is covered
by test_integration.py).
"""

from __future__ import annotations

import pytest

from switchboard.server import _CHANNEL_METHOD, Switchboard
from switchboard.store import Store


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


def test_register_rejects_empty_name(board):
    with pytest.raises(ValueError, match="non-empty"):
        board.register(_ctx(), "   ")


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
    assert dumped["params"]["meta"]["msg_from"] == "lead"
    assert dumped["params"]["meta"]["msg_id"] == str(res["id"])
    # The push is a body-less nudge to check inbox, NOT the message content --
    # the durable row is the single source of truth (see role fan-out below).
    assert "inbox" in dumped["params"]["content"].lower()
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
