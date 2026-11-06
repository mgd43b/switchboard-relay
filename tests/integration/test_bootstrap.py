"""Real stdio tests for automatic join, both MCP protocol eras, and sticky opt-out."""

import os
import sys
from contextlib import AsyncExitStack

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


@pytest.mark.parametrize("modern", [False, True])
async def test_auto_join_charter_and_identity(tmp_path, modern):
    async with AsyncExitStack() as stack:
        clients = []
        for _ in range(2):
            params = StdioServerParameters(
                command=sys.executable,
                args=["-m", "switchboard_relay.server"],
                env={
                    **os.environ,
                    "SWITCHBOARD_DB": str(tmp_path / "b.db"),
                    "SWITCHBOARD_AUTO": "1",
                    "SWITCHBOARD_NAME": "",
                    "SWITCHBOARD_ROLE": "",
                },
            )
            r, w = await stack.enter_async_context(stdio_client(params))
            c = await stack.enter_async_context(ClientSession(r, w))
            await (c.discover() if modern else c.initialize())
            clients.append(c)
        a, b = clients

        async def call(c, name, args=None):
            result = await c.call_tool(name, args or {})
            assert not result.is_error, result
            if name not in {"host_standby", "codex_standby", "unregister"}:
                assert "context" in result.structured_content
                assert any("Switchboard context:" in getattr(x, "text", "") for x in result.content)
            return result.structured_content

        first = await call(a, "bootstrap")
        second = await call(b, "bootstrap")
        assert first["you"] != second["you"]
        assert len(second["peers"]) == 2
        assert (await call(a, "register"))["you"] == first["you"]
        assert (await call(a, "inbox"))["count"] == 0
        revision = await call(
            a, "set_charter", {"text": "test conventions", "expected_revision": 1}
        )
        assert revision["charter_revision"] == 2
        assert (await call(b, "charter"))["charter"] == "test conventions"
        await call(a, "send", {"to": second["you"], "body": "ping"})
        assert (await call(b, "wait", {"timeout_s": 1}))["messages"][0]["body"] == "ping"
        await call(a, "standby", {"enabled": False})
        assert (await call(a, "host_standby", {"timeout_s": 0})) == {"continue": True}
        await call(a, "unregister")
        result = await a.call_tool("inbox", {})
        assert result.is_error
        await call(a, "register", {"name": "back"})
        assert (await call(a, "bootstrap"))["you"] == "back"


def test_sigterm_cleans_session(tmp_path):
    import json
    import select
    import sqlite3
    import subprocess
    import time

    db = tmp_path / "signal.db"
    process = subprocess.Popen(
        [sys.executable, "-m", "switchboard_relay.server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={
            **os.environ,
            "SWITCHBOARD_DB": str(db),
            "SWITCHBOARD_AUTO": "1",
            "SWITCHBOARD_NAME": "",
        },
    )
    try:
        process.stdin.write(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {},
                        "clientInfo": {"name": "signal-test", "version": "1"},
                    },
                }
            )
            + "\n"
        )
        process.stdin.flush()
        assert select.select([process.stdout], [], [], 5)[0]
        assert json.loads(process.stdout.readline())["id"] == 1
        process.stdin.write(
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"
        )
        process.stdin.flush()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with sqlite3.connect(db) as c:
                if c.execute("SELECT count(*) FROM relay_sessions").fetchone()[0]:
                    break
            time.sleep(0.01)
        else:
            pytest.fail("session never joined")
        process.terminate()
        process.wait(timeout=5)
        with sqlite3.connect(db) as c:
            assert c.execute("SELECT count(*) FROM relay_sessions").fetchone()[0] == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        for stream in [process.stdin, process.stdout, process.stderr]:
            stream.close()


async def test_discovery_context_and_hooks_in_process(tmp_path):
    from test_integration import connect

    from switchboard_relay.server import build_server
    from switchboard_relay.store import Store

    server = build_server(Store(tmp_path / "b.db"), stdio=True, automatic=True)
    async with connect(server) as client:

        async def call(name, args=None):
            return await client.call_tool(name, args or {})

        result = await call("bootstrap")
        assert not result.is_error
        assert result.structured_content["context"]["charter_revision"] == 1
        again = await call("participants")
        assert again.structured_content["context"]["peer_count"] == 1
        assert "charter" not in again.structured_content["context"]
        idle = await call("host_standby", {"timeout_s": 0})
        assert idle.structured_content["decision"] == "block"
        assert "re-arm" in idle.structured_content["reason"]
        error = await call("set_charter", {"text": "new", "expected_revision": 0})
        assert error.is_error
        updated = await call("set_charter", {"text": "new", "expected_revision": 1})
        assert updated.structured_content["context"]["charter"] == "new"
        await call("standby", {"enabled": False})
        off = await call("host_standby", {"timeout_s": 0})
        assert off.structured_content == {"continue": True}
        await call("unregister")
        assert (await call("inbox")).is_error
