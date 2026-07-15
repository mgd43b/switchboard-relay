"""Integration test over REAL stdio subprocesses sharing one SQLite database.

This is the truest reproduction of the deployment shape: two separate OS
processes (as two Claude Code sessions would be), each running the installed
``switchboard-relay`` console script, talking only through the shared DB file. It
exercises the console entry point, the stdio transport, and cross-process
durability all at once.

Skipped automatically if the ``switchboard-relay`` console script is not on PATH
(e.g. the package was not installed, only imported).
"""

from __future__ import annotations

import os
import shutil
import sys
from contextlib import AsyncExitStack
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def _find_switchboard() -> str | None:
    # Prefer PATH, but also look next to the running interpreter so the test
    # runs inside a virtualenv that hasn't been PATH-activated (and in CI).
    found = shutil.which("switchboard-relay")
    if found:
        return found
    candidate = Path(sys.executable).parent / "switchboard-relay"
    return str(candidate) if candidate.exists() else None


SWITCHBOARD_BIN = _find_switchboard()

pytestmark = pytest.mark.skipif(
    SWITCHBOARD_BIN is None,
    reason="`switchboard-relay` console script not on PATH (install the package to run this test)",
)


def _params(db_path: str) -> StdioServerParameters:
    return StdioServerParameters(
        command=SWITCHBOARD_BIN,
        args=[],
        env={**os.environ, "SWITCHBOARD_DB": db_path},
    )


def _data(result):
    assert not result.isError, result.content[0].text if result.content else result
    return result.structuredContent


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
