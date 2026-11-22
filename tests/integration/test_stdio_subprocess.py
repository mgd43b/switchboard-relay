"""Integration test over REAL stdio subprocesses sharing one SQLite database.

This is the truest reproduction of the deployment shape: two separate OS
processes (as two Codex, Claude Code, or mixed MCP sessions would be), each
running the installed ``switchboard-relay`` console script, talking only through
the shared DB file. It exercises the console entry point, the stdio transport,
and cross-process durability all at once.

Skipped automatically if the ``switchboard-relay`` console script is not on PATH
(e.g. the package was not installed, only imported).
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

import pytest
from mcp import Client, ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.subscriptions import ResourceUpdated


def _find_switchboard() -> str | None:
    # Prefer PATH, but also look next to the running interpreter so the test
    # runs inside a virtualenv that hasn't been PATH-activated (and in CI).
    candidate = Path(sys.executable).parent / "switchboard-relay"
    if candidate.exists():
        return str(candidate)
    return shutil.which("switchboard-relay")


SWITCHBOARD_BIN = _find_switchboard()

pytestmark = pytest.mark.skipif(
    SWITCHBOARD_BIN is None,
    reason="`switchboard-relay` console script not on PATH (install the package to run this test)",
)


def _params(db_path: str, **env: str) -> StdioServerParameters:
    return StdioServerParameters(
        command=SWITCHBOARD_BIN,
        args=[],
        env={**os.environ, "SWITCHBOARD_DB": db_path, **env},
    )


def _data(result):
    assert not result.is_error, result.content[0].text if result.content else result
    return result.structured_content


async def test_two_real_processes_round_trip(tmp_path):
    db = str(tmp_path / "switchboard.db")
    # AsyncExitStack lets the two ClientSessions consume the streams yielded by
    # the two stdio_client contexts without a data-dependent nested `with`.
    async with AsyncExitStack() as stack:
        lr, lw = await stack.enter_async_context(stdio_client(_params(db)))
        wr, ww = await stack.enter_async_context(stdio_client(_params(db)))
        lead = await stack.enter_async_context(ClientSession(lr, lw))
        worker = await stack.enter_async_context(ClientSession(wr, ww))
        await lead.initialize()
        await worker.initialize()

        assert (
            _data(await lead.call_tool("register", {"name": "lead", "role": "coordinator"}))["you"]
            == "lead"
        )
        assert (
            _data(await worker.call_tool("register", {"name": "worker:x", "role": "worker"}))["you"]
            == "worker:x"
        )

        # The two processes see each other only via the shared DB.
        parts = _data(await worker.call_tool("participants", {}))
        assert sorted(p["name"] for p in parts["participants"]) == ["lead", "worker:x"]

        q = _data(await worker.call_tool("send", {"to": "lead", "body": "what is 2+2?"}))
        got = _data(await lead.call_tool("inbox", {}))
        assert got["messages"][0]["body"] == "what is 2+2?"
        assert got["messages"][0]["from"] == "worker:x"

        _data(await lead.call_tool("send", {"to": "worker:x", "body": "4", "reply_to": q["id"]}))
        reply = _data(await worker.call_tool("wait", {"timeout_s": 5}))
        assert reply["messages"][0]["body"] == "4"
        assert reply["messages"][0]["reply_to"] == q["id"]


@asynccontextmanager
async def _participant(db: str, name: str, role: str = ""):
    async with (
        stdio_client(_params(db, SWITCHBOARD_DELIVERY_LEASE="1")) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        _data(await session.call_tool("register", {"name": name, "role": role}))
        yield session


async def test_unacknowledged_stdio_claim_survives_worker_disconnect(tmp_path):
    db = str(tmp_path / "switchboard.db")
    async with _participant(db, "lead") as lead:
        sent = _data(await lead.call_tool("send", {"to": "worker", "body": "recover me"}))
        mid = sent["id"]
        assert _data(await lead.call_tool("status", {"id": mid}))["status"] == "queued"
        async with _participant(db, "worker:one", "worker") as first:
            [claim] = _data(await first.call_tool("wait", {"timeout_s": 0}))["messages"]
            assert claim["id"] == mid
            assert _data(await lead.call_tool("status", {"id": mid}))["status"] == "leased"
            assert _data(await first.call_tool("status", {"id": mid}))["status"] == "unknown"
        # The first client's stdio process is gone and never acknowledged its claim.

        async with _participant(db, "worker:two", "worker") as second:
            [retry] = _data(await second.call_tool("wait", {"timeout_s": 5}))["messages"]
            assert retry["id"] == mid and retry["body"] == "recover me"
            assert retry["receipt"] != claim["receipt"]
            stale = _data(await second.call_tool("ack", {"id": mid, "receipt": claim["receipt"]}))
            assert stale["ok"] is False
            ack_args = {"id": mid, "receipt": retry["receipt"]}
            ack = _data(await second.call_tool("ack", ack_args))
            assert ack["ok"] is True
            assert (
                _data(await second.call_tool("ack", ack_args))["acknowledged_at"]
                == ack["acknowledged_at"]
            )

        status = _data(await lead.call_tool("status", {"id": mid}))
        assert status["status"] == "acknowledged"
        assert status["acknowledged_at"] == ack["acknowledged_at"]


async def test_nonblocking_threaded_replies_over_stdio(tmp_path):
    db = str(tmp_path / "switchboard.db")
    async with (
        _participant(db, "lead") as lead,
        _participant(db, "worker", "implementer") as worker,
    ):
        question = _data(await worker.call_tool("send", {"to": "lead", "body": "Which branch?"}))
        ticket = {"question_id": question["id"]}
        assert _data(await worker.call_tool("replies", ticket))["replies"] == []
        _data(await lead.call_tool("send", {"to": "worker", "body": "Unrelated"}))
        _data(
            await lead.call_tool(
                "send",
                {
                    "to": "implementer",
                    "body": "Role message",
                    "reply_to": question["id"],
                },
            )
        )
        answer = _data(
            await lead.call_tool(
                "send",
                {
                    "to": "worker",
                    "body": "main",
                    "reply_to": question["id"],
                },
            )
        )

        [reply] = _data(await worker.call_tool("replies", ticket))["replies"]
        assert reply["id"] == answer["id"] and reply["body"] == "main"
        assert _data(
            await worker.call_tool(
                "ack",
                {
                    "id": reply["id"],
                    "receipt": reply["receipt"],
                },
            )
        )["ok"]
        mailbox = _data(await worker.call_tool("inbox", {"peek": True}))["messages"]
        assert [message["body"] for message in mailbox] == ["Unrelated", "Role message"]


async def test_role_task_tools_and_history_survive_lead_restart(tmp_path):
    db = str(tmp_path / "switchboard.db")
    async with _participant(db, "lead") as lead:
        task = _data(
            await lead.call_tool(
                "task_submit",
                {
                    "role": "worker",
                    "scope": "Review the docs",
                    "owned_paths": ["README.md"],
                    "acceptance_criteria": ["Verify the examples"],
                    "expected_evidence": "Test output",
                },
            )
        )
        assert task["status"] == "queued"

    async with _participant(db, "worker:one", "worker") as worker:
        claim = _data(await worker.call_tool("task_claim", {}))["task"]
        assert claim["id"] == task["id"] and claim["expected_evidence"] == "Test output"
        change = {"id": task["id"], "receipt": claim["receipt"]}
        assert _data(
            await worker.call_tool(
                "task_update",
                {
                    **change,
                    "action": "completed",
                    "note": "Examples passed",
                },
            )
        )["ok"]
        assert _data(await worker.call_tool("task_claim", {}))["task"] is None

    async with _participant(db, "lead") as lead:
        status = _data(await lead.call_tool("task_status", {"id": task["id"]}))
        assert status["status"] == "completed"
        assert status["attempts"][0]["updates"][0]["note"] == "Examples passed"


async def test_stdio_inbox_subscription_is_scoped_and_notifies_on_redelivery(tmp_path):
    board = str(tmp_path / "switchboard.db")
    uri = "switchboard://inbox"
    async with _participant(board, "sender") as sender:
        async with Client(
            _params(board, SWITCHBOARD_INBOX_SUBSCRIPTIONS="1", SWITCHBOARD_DELIVERY_LEASE="1")
        ) as receiver:
            _data(await receiver.call_tool("register", {"name": "listener"}))
            async with receiver.listen(resource_subscriptions=[uri]) as events:
                initial = await receiver.read_resource(uri)
                assert json.loads(initial.contents[0].text) == {"available": False}
                _data(await sender.call_tool("send", {"to": "somebody-else", "body": "private"}))
                incoming = asyncio.create_task(anext(events))
                try:
                    done, _ = await asyncio.wait([incoming], timeout=1.25)
                    assert not done
                    sent = _data(
                        await sender.call_tool(
                            "send",
                            {
                                "to": "listener",
                                "body": "claim me",
                            },
                        )
                    )
                    assert await asyncio.wait_for(incoming, 5) == ResourceUpdated(uri=uri)
                finally:
                    if not incoming.done():
                        incoming.cancel()
                        await asyncio.gather(incoming, return_exceptions=True)

                visible = await receiver.read_resource(uri)
                assert json.loads(visible.contents[0].text) == {"available": True}
                [claim] = _data(await receiver.call_tool("inbox", {}))["messages"]
                assert claim["id"] == sent["id"]
                assert await asyncio.wait_for(anext(events), 5) == ResourceUpdated(uri=uri)

        # A fresh connection can recover pending mail through a normal tool call.
        async with _participant(board, "listener") as reconnected:
            [retry] = _data(await reconnected.call_tool("wait", {"timeout_s": 5}))["messages"]
            assert retry["id"] == claim["id"]
            assert retry["receipt"] != claim["receipt"]
