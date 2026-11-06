"""Concurrency and persistence contracts for discovery and board ownership."""

from concurrent.futures import ThreadPoolExecutor

import pytest

from switchboard_relay.protocol import Protocol
from switchboard_relay.store import Store


def test_charter_election_rename_failover_and_revisions(tmp_path):
    store = Store(tmp_path / "board.db")
    p = Protocol(store, lease=10)
    p.claim("a", "alice", "", now=0)
    p.claim("b", "bob", "", now=1)
    assert p.context("a", 2)["coordinator"] == "alice"
    assert not p.context("a", 2)["peers"][0]["ready"]
    assert p.context("a", 2, bootstrap=True)["peers"][0]["ready"]
    with pytest.raises(ValueError, match="coordinator_required"):
        p.update("b", "new", 1, 2)
    with pytest.raises(ValueError, match="revision_conflict"):
        p.update("a", "new", 0, 2)
    for text in ["", "x" * 8193]:
        with pytest.raises(ValueError, match="charter"):
            p.update("a", text, 1, 2)
    p.update("a", "new", 1, 2)
    store.send("alice", "pending", sender="bob", now=2)
    p.claim("a", "renamed", "", now=3)
    assert p.context("a", 3)["coordinator"] == "renamed"
    assert store.inbox("renamed")[0].body == "pending"
    assert "alice" not in [x.name for x in store.participants(now=3)]
    p.touch("b", 5, "configured")
    p.leave("a", 6)
    assert p.context("b", 6)["coordinator"] == "bob"
    assert p.context("b", 6)["charter"] == "new"
    p.claim("c", "carol", "", now=7)
    p.touch("c", 14)
    assert p.context("c", 16)["coordinator"] == "carol"
    with pytest.raises(ValueError, match="revision_conflict"):
        p.transfer("carol", 1, 16)
    with pytest.raises(ValueError, match="live native"):
        p.transfer("missing", 2, 16)
    assert p.transfer("carol", 2, 16)["coordinator"] == "carol"
    assert Protocol(store).context("c", 16)["charter"] == "new"
    p.leave("c", 17)
    assert p.context("", 100)["coordinator"] is None


def test_atomic_claims_and_legacy_collision(tmp_path):
    store = Store(tmp_path / "board.db")
    p = Protocol(store)

    def claim(i):
        try:
            p.claim(str(i), "lead", "", now=1)
            return True
        except ValueError:
            return False

    with ThreadPoolExecutor(4) as pool:
        assert sum(pool.map(claim, range(4))) == 1
    assert len(p.context("", 2)["peers"]) == 1
    store.register("legacy", now=1)
    with pytest.raises(ValueError, match="name_in_use"):
        p.claim("x", "legacy", "", now=2)
    assert p.claim("x", "legacy", "", now=2, explicit=False) != "legacy"
    assert p.claim("y", "lead", "", now=300) == "lead"
    assert store.pending_messages() == []


def test_default_refresh_and_transfer_generation(tmp_path, monkeypatch):
    store = Store(tmp_path / "b.db")
    p = Protocol(store)
    p.claim("a", "owner", "", now=0)
    with store._connect() as c:
        c.execute("UPDATE relay_charters SET text='older default'")
    monkeypatch.setattr("switchboard_relay.protocol.DEFAULT_CHARTER_VERSION", 2)
    p = Protocol(store)
    assert p.context("", 1)["charter_revision"] == 2
    monkeypatch.setattr("switchboard_relay.protocol.DEFAULT_CHARTER_VERSION", 1)
    monkeypatch.setattr("switchboard_relay.protocol.DEFAULT_CHARTER", "older build")
    assert Protocol(store).context("", 1)["charter_revision"] == 2
    with pytest.raises(ValueError, match="coordinator_conflict"):
        p.transfer("owner", 2, 1, expected_generation=-1)


def test_stale_owner_cannot_drain_or_take_reply(tmp_path):
    store = Store(tmp_path / "b.db")
    p = Protocol(store, lease=1)
    p.claim("a", "lead", "", now=0)
    p.claim("b", "lead", "", now=2)
    store.send("lead", "pending", sender="peer", reply_to=1, now=2)
    with pytest.raises(ValueError, match="ownership lost"):
        store.inbox("lead", owner_token="a")
    with pytest.raises(ValueError, match="ownership lost"):
        store.take_reply("lead", reply_to=1, owner_token="a")
    assert store.inbox("lead", owner_token="b")[0].body == "pending"


def test_simultaneous_schema_initialization(tmp_path):
    path = tmp_path / "fresh.db"

    def start(i):
        p = Protocol(Store(path))
        return p.claim(str(i), f"worker-{i}", "", now=1)

    with ThreadPoolExecutor(6) as pool:
        assert len(set(pool.map(start, range(12)))) == 12
    assert len(Protocol(Store(path)).context("", 2)["peers"]) == 12
