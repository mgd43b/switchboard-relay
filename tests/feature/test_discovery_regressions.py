import time

import anyio
import pytest
from mcp.server.mcpserver.exceptions import ToolError
from test_server import FakeCtx, FakeSession

from switchboard_relay.server import Switchboard
from switchboard_relay.store import Store


def test_repeat_rename_collision_and_lease_loss(tmp_path):
    b = Switchboard(Store(tmp_path / "b.db"))
    a = FakeCtx(FakeSession())
    c = FakeCtx(FakeSession())
    first = b.register(a)["you"]
    assert b.register(a)["you"] == first
    b.register(a, "lead")
    with pytest.raises(ToolError, match="name_in_use"):
        b.register(c, "lead")
    with b.protocol.transaction() as conn:
        conn.execute("UPDATE relay_sessions SET expires=0")
    b.register(c, "lead")
    with pytest.raises(ToolError, match="ownership lost"):
        b.send(a, "lead", "bad")


async def test_failed_push_retries_and_cancel_keeps_future_mail(tmp_path):
    b = Switchboard(Store(tmp_path / "b.db"))
    a = FakeCtx(FakeSession())
    b.register(a, "lead")
    original = a.session._connection.notify

    async def fail(*args, **kwargs):
        raise RuntimeError("temporary failure")

    a.session._connection.notify = fail
    b.store.send("lead", "first", sender="peer", now=time.time())
    assert await b._watch_tick() == 0
    assert await b._watch_tick() == 0
    a.session._connection.notify = original
    b._watch_failures.clear()
    assert await b._watch_tick() == 1
    b.inbox(a)
    async with anyio.create_task_group() as tg:
        tg.start_soon(b.wait, a, 10)
        await anyio.sleep(0.01)
        with pytest.raises(ToolError, match="session_busy"):
            b.register(a, "new")
        tg.cancel_scope.cancel()
    b.store.send("lead", "after cancel", sender="peer", now=time.time())
    assert b.inbox(a)["messages"][0]["body"] == "after cancel"
    assert b._active_receivers == 0


def test_charter_cli_read_is_noncreating_and_transfer(tmp_path, monkeypatch, capsys):
    from switchboard_relay.server import main

    path = tmp_path / "cli.db"
    monkeypatch.setenv("SWITCHBOARD_DB", str(path))
    assert main(["charter"]) == 0
    assert not path.exists()
    store = Store(path)
    assert main(["charter"]) == 0
    p = Switchboard(store)
    a = FakeCtx(FakeSession())
    p.register(a, "owner")
    assert main(["charter"]) == 0
    assert "coordinator" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        main(["charter", "--coordinator", "owner"])
    assert main(["charter", "--coordinator", "owner", "--expected-revision", "1"]) == 0
    with pytest.raises(SystemExit):
        main(["--ttl", "nan"])
