"""Integration tests: drive the switchboard-relay tools over a real MCP transport.

Two independent client sessions connect to one FastMCP server through the SDK's
in-memory client/server pipe, so each has its own MCP session (distinct
identity) while sharing the same durable store -- exactly the shape of two
Claude Code sessions talking through switchboard-relay. This is the wire-level proof
of the acceptance criteria (register -> send -> inbox -> reply, TTL expiry,
wait()).
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from mcp.shared.memory import create_connected_server_and_client_session as connect

from switchboard_relay.server import build_server
from switchboard_relay.store import Store


def make_board(tmp_path, *, ttl: float = 300.0):
    return build_server(Store(tmp_path / "switchboard.db"), ttl=ttl)


def data(result):
    """Return a tool call's structured payload, asserting it did not error."""
    assert not result.isError, _text(result)
    return result.structuredContent


def _text(result):
    return result.content[0].text if result.content else "<no content>"


@asynccontextmanager
async def sessions(mcp, n: int = 2):
    """Open `n` concurrent client sessions against one server."""
    if n == 2:
        async with connect(mcp) as a, connect(mcp) as b:
            yield a, b
    elif n == 3:
        async with connect(mcp) as a, connect(mcp) as b, connect(mcp) as c:
            yield a, b, c
    else:  # pragma: no cover
        raise ValueError(n)


# -- acceptance criterion 2: two-way lead/worker round trip -----------------


async def test_lead_worker_round_trip(tmp_path):
    mcp = make_board(tmp_path)
    async with sessions(mcp) as (a, b):
        assert (
            data(await a.call_tool("register", {"name": "lead", "role": "coordinator"}))["you"]
            == "lead"
        )
        assert (
            data(await b.call_tool("register", {"name": "worker:x", "role": "worker"}))["you"]
            == "worker:x"
        )

        sent = data(await b.call_tool("send", {"to": "lead", "body": "what is 2+2?"}))
        qid = sent["id"]
        assert isinstance(qid, int)
        assert sent["delivered_live"] is False  # stdio-style: no push

        got = data(await a.call_tool("inbox", {}))
        assert got["count"] == 1
        msg = got["messages"][0]
        assert msg["body"] == "what is 2+2?"
        assert msg["from"] == "worker:x"

        # Lead replies, threading on the question id.
        data(await a.call_tool("send", {"to": "worker:x", "body": "4", "reply_to": qid}))
        reply = data(await b.call_tool("inbox", {}))
        assert reply["count"] == 1
        assert reply["messages"][0]["body"] == "4"
        assert reply["messages"][0]["reply_to"] == qid


async def test_inbox_drains_by_default_peek_does_not(tmp_path):
    mcp = make_board(tmp_path)
    async with sessions(mcp) as (a, b):
        data(await a.call_tool("register", {"name": "lead"}))
        data(await b.call_tool("register", {"name": "worker"}))
        data(await b.call_tool("send", {"to": "lead", "body": "m1"}))

        assert data(await a.call_tool("inbox", {"peek": True}))["count"] == 1
        assert data(await a.call_tool("inbox", {"peek": True}))["count"] == 1  # still there
        assert data(await a.call_tool("inbox", {}))["count"] == 1  # drains
        assert data(await a.call_tool("inbox", {}))["count"] == 0  # gone


async def test_participants_lists_both(tmp_path):
    mcp = make_board(tmp_path)
    async with sessions(mcp) as (a, b):
        data(await a.call_tool("register", {"name": "lead", "role": "coordinator"}))
        data(await b.call_tool("register", {"name": "worker:1", "role": "worker"}))
        parts = data(await a.call_tool("participants", {}))
        names = {p["name"] for p in parts["participants"]}
        assert names == {"lead", "worker:1"}
        assert parts["count"] == 2


# -- acceptance criterion 4: TTL expiry, end to end -------------------------


async def test_participant_expires_after_ttl(tmp_path):
    # A short TTL keeps the test fast, but not so short that ordinary scheduling
    # jitter on a loaded CI runner can exceed it: participants() heartbeats the
    # caller and then queries liveness, and if the process is descheduled for
    # longer than the TTL *between* those two steps, the just-touched caller is
    # wrongly pruned. 1s comfortably clears that jitter while still expiring an
    # idle peer within the ~1.5s sleep below.
    mcp = make_board(tmp_path, ttl=1.0)
    async with sessions(mcp) as (ghost, watcher):
        data(await ghost.call_tool("register", {"name": "ghost"}))
        data(await watcher.call_tool("register", {"name": "watcher"}))
        # Both live immediately.
        names = {
            p["name"] for p in data(await watcher.call_tool("participants", {}))["participants"]
        }
        assert "ghost" in names

        # ghost stops heartbeating; watcher keeps active. After the TTL passes,
        # ghost drops out while watcher (which just called a tool) remains.
        await asyncio.sleep(1.5)
        parts = data(await watcher.call_tool("participants", {}))
        names = {p["name"] for p in parts["participants"]}
        assert "ghost" not in names
        assert "watcher" in names


# -- wait() -----------------------------------------------------------------


async def test_wait_times_out_cleanly(tmp_path):
    mcp = make_board(tmp_path)
    async with sessions(mcp) as (a, _b):
        data(await a.call_tool("register", {"name": "lead"}))
        out = data(await a.call_tool("wait", {"timeout_s": 0.3}))
        assert out["timed_out"] is True
        assert out["messages"] == []


async def test_wait_returns_message_on_arrival(tmp_path):
    mcp = make_board(tmp_path)
    async with sessions(mcp) as (a, b):
        data(await a.call_tool("register", {"name": "lead"}))
        data(await b.call_tool("register", {"name": "worker"}))

        async def deliver():
            await asyncio.sleep(0.2)
            await b.call_tool("send", {"to": "lead", "body": "async hello"})

        waiter = asyncio.create_task(a.call_tool("wait", {"timeout_s": 5.0}))
        deliverer = asyncio.create_task(deliver())
        result = await waiter
        await deliverer

        out = data(result)
        assert out["timed_out"] is False
        assert [m["body"] for m in out["messages"]] == ["async hello"]


# -- ask (request/response) -------------------------------------------------


async def test_ask_round_trip(tmp_path):
    mcp = make_board(tmp_path)
    async with sessions(mcp) as (worker, lead):
        data(await worker.call_tool("register", {"name": "worker:x", "role": "worker"}))
        data(await lead.call_tool("register", {"name": "lead"}))

        async def answer():
            got = data(await lead.call_tool("wait", {"timeout_s": 5.0}))
            q = got["messages"][0]
            await lead.call_tool("send", {"to": q["from"], "body": "42", "reply_to": q["id"]})

        answerer = asyncio.create_task(answer())
        result = data(
            await worker.call_tool("ask", {"to": "lead", "body": "the answer?", "timeout_s": 5.0})
        )
        await answerer

        assert result["timed_out"] is False
        assert result["reply"]["body"] == "42"
        assert result["reply"]["reply_to"] == result["question_id"]


async def test_broadcast_over_the_wire(tmp_path):
    mcp = make_board(tmp_path)
    async with sessions(mcp, 3) as (lead, w1, w2):
        data(await lead.call_tool("register", {"name": "lead"}))
        data(await w1.call_tool("register", {"name": "worker:1", "role": "worker"}))
        data(await w2.call_tool("register", {"name": "worker:2", "role": "worker"}))

        out = data(await lead.call_tool("broadcast", {"body": "standup in 5"}))
        assert out["count"] == 2
        assert [m["body"] for m in data(await w1.call_tool("inbox", {}))["messages"]] == [
            "standup in 5"
        ]
        assert [m["body"] for m in data(await w2.call_tool("inbox", {}))["messages"]] == [
            "standup in 5"
        ]


async def test_unregister_over_the_wire(tmp_path):
    mcp = make_board(tmp_path)
    async with sessions(mcp) as (a, b):
        data(await a.call_tool("register", {"name": "lead"}))
        data(await b.call_tool("register", {"name": "worker"}))
        out = data(await a.call_tool("unregister", {}))
        assert out["was_registered"] is True
        assert out["you"] == "lead"
        parts = data(await b.call_tool("participants", {}))
        assert "lead" not in {p["name"] for p in parts["participants"]}


# -- role addressing --------------------------------------------------------


async def test_role_addressed_message(tmp_path):
    mcp = make_board(tmp_path)
    async with sessions(mcp) as (lead, worker):
        data(await lead.call_tool("register", {"name": "lead"}))
        data(await worker.call_tool("register", {"name": "worker:7", "role": "worker"}))
        data(await lead.call_tool("send", {"to": "worker", "body": "any worker?"}))
        got = data(await worker.call_tool("inbox", {}))
        assert [m["body"] for m in got["messages"]] == ["any worker?"]


# -- errors -----------------------------------------------------------------


async def test_unregistered_session_gets_helpful_error(tmp_path):
    mcp = make_board(tmp_path)
    async with sessions(mcp) as (a, _b):
        res = await a.call_tool("inbox", {})
        assert res.isError
        assert "not registered" in _text(res).lower()


async def test_send_without_registration_errors(tmp_path):
    mcp = make_board(tmp_path)
    async with sessions(mcp) as (a, _b):
        res = await a.call_tool("send", {"to": "lead", "body": "hi"})
        assert res.isError


# -- daemon-mode push / turn injection --------------------------------------


async def test_daemon_push_over_the_wire_is_advertised_and_harmless(tmp_path):
    """The turn-injection path proven over a real MCP session.

    Three things at once, all end-to-end rather than through a fake Context:

    * the server advertises the ``claude/channel`` capability in the initialize
      handshake, which is what makes a real Claude Code client subscribe;
    * a daemon-mode ``send()`` to a connected recipient hands a Channels
      notification to the transport (``delivered_live`` True);
    * a *generic* MCP client (which, unlike Claude Code, never registered a
      handler for the custom method) simply drops the unknown notification --
      so push is best-effort and never breaks the session. The durable row is
      untouched and still drains normally.
    """
    mcp = build_server(Store(tmp_path / "switchboard.db"), ttl=300, daemon=True)
    async with sessions(mcp) as (lead, worker):
        # The capability the client sees on the wire (same field the SDK's own
        # capability checks read); its presence is what a channel client keys on.
        caps = lead._server_capabilities
        assert caps is not None and (caps.experimental or {}).get("claude/channel") == {}

        data(await lead.call_tool("register", {"name": "lead"}))
        data(await worker.call_tool("register", {"name": "worker"}))

        sent = data(await worker.call_tool("send", {"to": "lead", "body": "ping"}))
        assert sent["delivered_live"] is True  # pushed over the real transport

        # The unknown notification is dropped by the generic client, but the
        # session is unharmed and the message is still durably deliverable.
        await asyncio.sleep(0.2)  # let the notification propagate + be dropped
        got = data(await lead.call_tool("inbox", {}))
        assert [m["body"] for m in got["messages"]] == ["ping"]


async def test_stdio_lifespan_starts_self_watch(tmp_path, monkeypatch):
    """The daemon-free path: the FastMCP lifespan starts the self-watch loop.

    A stdio server with push enabled runs a background watcher (started by the
    server lifespan under real ``run()``) that polls the shared board and
    self-nudges its own client -- turn injection with no daemon. We spy on
    ``_watch_tick`` to prove the loop is actually wired up and running, without
    depending on the client understanding the custom channel notification.
    """
    mcp = build_server(Store(tmp_path / "switchboard.db"), ttl=300, daemon=False)
    sb = mcp._switchboard_relay
    sb._push_enabled = True  # equivalent to launching with SWITCHBOARD_PUSH=1

    ticks = 0
    original = sb._watch_tick

    async def spy(**kwargs):
        nonlocal ticks
        ticks += 1
        return await original(**kwargs)

    monkeypatch.setattr(sb, "_watch_tick", spy)

    async with sessions(mcp) as (a, _b):
        data(await a.call_tool("register", {"name": "lead"}))
        for _ in range(250):  # up to ~5s for the lifespan-started loop to tick
            if ticks:
                break
            await asyncio.sleep(0.02)

    assert ticks >= 1  # the lifespan actually started and ran the watcher


# -- durability across a "restart" ------------------------------------------


async def test_messages_persist_across_server_restart(tmp_path):
    # First server instance: queue a message for a name that never reads it.
    mcp1 = make_board(tmp_path)
    async with sessions(mcp1) as (a, _b):
        data(await a.call_tool("register", {"name": "sender"}))
        data(await a.call_tool("send", {"to": "lead", "body": "still here after restart"}))

    # Fresh server over the same DB file (simulating a daemon/CLI restart).
    mcp2 = make_board(tmp_path)
    async with sessions(mcp2) as (a, _b):
        data(await a.call_tool("register", {"name": "lead"}))
        got = data(await a.call_tool("inbox", {}))
        assert [m["body"] for m in got["messages"]] == ["still here after restart"]
