import time
import weakref

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


def test_token_survives_prune_while_connection_is_open(tmp_path):
    b = Switchboard(Store(tmp_path / "b.db"))
    a = FakeCtx(FakeSession())
    b.register(a, "lead")
    token = b._conns[b._sid(a)].token
    b._prune_conns(set())  # participant row lapsed; the connection is still open
    assert b.register(a, "lead")["you"] == "lead"
    assert b._conns[b._sid(a)].token == token


def test_recycled_connection_id_never_inherits_a_token(tmp_path):
    b = Switchboard(Store(tmp_path / "b.db"))
    a = FakeCtx(FakeSession())
    b.register(a, "lead")
    b._conns.pop(b._sid(a))
    b.store.send("lead", "for the old session", sender="peer", now=time.time())

    class Gone:
        pass

    # Simulate CPython reusing the departed connection's id() for a new one.
    old = b._tokens[b._sid(a)][1]
    b._tokens[b._sid(a)] = (weakref.ref(Gone()), old)
    with pytest.raises(ToolError, match="name_in_use"):
        b.register(a, "lead")  # a fresh token, so the old session's mail stays put
    assert b._tokens[b._sid(a)][1] != old
    assert b.store.has_messages("lead")


def test_prune_sweeps_tokens_of_closed_connections(tmp_path):
    b = Switchboard(Store(tmp_path / "b.db"))

    class Gone:
        pass

    b._tokens[1] = (weakref.ref(Gone()), "stale")
    b._prune_conns(set())
    assert 1 not in b._tokens


def test_unregister_drops_the_token(tmp_path):
    b = Switchboard(Store(tmp_path / "b.db"))
    a = FakeCtx(FakeSession())
    b.register(a, "lead")
    b.unregister(a)
    assert b._sid(a) not in b._tokens


def test_manual_keepalive_refreshes_within_the_lease(tmp_path):
    b = Switchboard(Store(tmp_path / "b.db"), ttl=0.3)  # lease 0.3s, keepalive 60s
    a = FakeCtx(FakeSession())
    b.register(a, "lead")
    c = FakeCtx(FakeSession())

    async def drive():
        async with anyio.create_task_group() as tg:
            tg.start_soon(b._watch_loop)
            await anyio.sleep(0.8)
            with pytest.raises(ToolError, match="name_in_use"):
                b.register(c, "lead")
            tg.cancel_scope.cancel()

    anyio.run(drive)
