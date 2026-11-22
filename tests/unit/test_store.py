"""Unit tests for the switchboard-relay SQLite store.

These exercise the pure store in isolation: register/heartbeat, send/inbox
drain semantics, role addressing, `since`, and TTL expiry. Time is injected via
`now` so the tests are deterministic (no sleeps, no wall clock).
"""

from __future__ import annotations

import contextlib
import sqlite3
import types

import pytest

import switchboard_relay.store as store_mod
from switchboard_relay.store import (
    DEFAULT_TTL_SECONDS,
    Store,
    _enable_wal,
    _with_lock_retry,
    default_db_path,
)


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "switchboard.db")
    yield s
    s.close()


# -- registration & participants -------------------------------------------


def test_register_creates_participant(store):
    p = store.register("lead", "coordinator", now=100.0)
    assert p.name == "lead"
    assert p.role == "coordinator"
    assert p.last_seen == 100.0
    assert p.registered_at == 100.0

    live = store.participants(now=100.0)
    assert [x.name for x in live] == ["lead"]


def test_register_refresh_updates_last_seen_keeps_registered_at(store):
    store.register("lead", "coordinator", now=100.0)
    p = store.register("lead", now=250.0)  # refresh without role
    assert p.last_seen == 250.0
    assert p.registered_at == 100.0  # preserved
    assert p.role == "coordinator"  # preserved when omitted


def test_register_can_change_role(store):
    store.register("w", "worker", now=1.0)
    p = store.register("w", "reviewer", now=2.0)
    assert p.role == "reviewer"


def test_register_rejects_empty_name(store):
    with pytest.raises(ValueError):
        store.register("   ", now=1.0)


def test_register_stores_and_preserves_ccd_session_id(store):
    p = store.register("lead", "coord", now=1.0, ccd_session_id="local_abc")
    assert p.ccd_session_id == "local_abc"
    # A bare refresh keeps the CCD id (like it keeps the role).
    p2 = store.register("lead", now=2.0)
    assert p2.ccd_session_id == "local_abc"
    # participants() surfaces it on the dataclass but hides it from to_dict().
    live = store.participants(now=2.0)[0]
    assert live.ccd_session_id == "local_abc"
    assert "ccd_session_id" not in live.to_dict()


def test_migrate_adds_new_columns_to_legacy_db(tmp_path):
    # A DB created by an older schema (no ccd_session_id, no last_active) must
    # gain both columns when reopened, without losing existing rows.
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(
        "CREATE TABLE participants (name TEXT PRIMARY KEY, role TEXT NOT NULL DEFAULT '', "
        "  last_seen REAL NOT NULL, registered_at REAL NOT NULL);"
        "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, recipient TEXT, "
        "  sender TEXT, body TEXT, reply_to INTEGER, created_at REAL);"
        "INSERT INTO participants VALUES ('old', '', 1.0, 1.0);"
    )
    conn.commit()
    conn.close()

    s = Store(path)  # opening runs the migration
    old = next(p for p in s.participants(now=1.0, ttl=1e9) if p.name == "old")
    assert old.ccd_session_id == ""  # existing row defaults cleanly
    # last_active is seeded from last_seen, not left at the ADD COLUMN default:
    # before the split the two were the same thing, and a legacy row parked at 0
    # would report as idle since the epoch.
    assert old.last_active == old.last_seen == 1.0
    s.register("old", now=2.0, ccd_session_id="local_xyz")  # and is now settable
    assert next(p for p in s.participants(now=2.0, ttl=1e9)).ccd_session_id == "local_xyz"


def test_register_strips_whitespace(store):
    p = store.register("  lead  ", "  boss  ", now=1.0)
    assert p.name == "lead"
    assert p.role == "boss"


def test_participants_orders_freshest_first(store):
    store.register("a", now=10.0)
    store.register("b", now=30.0)
    store.register("c", now=20.0)
    names = [p.name for p in store.participants(now=30.0)]
    assert names == ["b", "c", "a"]


# -- TTL expiry (acceptance criterion 4) -----------------------------------


def test_participant_drops_out_past_ttl(store):
    store.register("ghost", now=0.0)
    ttl = 300.0
    # Still live just inside the window.
    assert [p.name for p in store.participants(now=ttl, ttl=ttl)] == ["ghost"]
    # Past the window it disappears from the live list.
    assert store.participants(now=ttl + 1, ttl=ttl) == []


def test_default_ttl_applied_when_unspecified(store):
    store.register("x", now=0.0)
    assert store.participants(now=DEFAULT_TTL_SECONDS - 1) != []
    assert store.participants(now=DEFAULT_TTL_SECONDS + 1) == []


def test_default_ttl_survives_a_long_heads_down_stretch(store):
    """The default window is sized for a live peer that goes quiet, not for how
    fast a dead one disappears: a session running a build or waiting on CI makes
    no switchboard call for twenty minutes and must still count as live, because
    broadcast() skips non-live recipients outright and never queues to them."""
    store.register("busy", now=0.0)
    assert [p.name for p in store.participants(now=20 * 60.0)] == ["busy"]


def test_touch_refreshes_liveness(store):
    store.register("x", now=0.0)
    store.touch("x", now=290.0)
    # Without the touch this would be expired at now=299 (ttl 300 from t=0).
    assert [p.name for p in store.participants(now=299.0, ttl=300.0)] == ["x"]


def test_touch_unregistered_is_noop(store):
    store.touch("nobody", now=1.0)  # must not raise
    assert store.participants(now=1.0) == []


def test_touch_moves_both_clocks_keepalive_moves_only_liveness(store):
    # The two clocks answer different questions. last_seen is "is this session
    # still there", which the connection keepalive can vouch for; last_active is
    # "when did this peer last actually use switchboard", which only a real call
    # can move. Collapsing them would make idle_seconds read 0 for every
    # connected session, however long it had been silent.
    store.register("x", now=0.0)
    store.keepalive("x", now=1000.0)
    p = next(iter(store.participants(now=1000.0, ttl=1e9)))
    assert p.last_seen == 1000.0  # live: the connection is still open
    assert p.last_active == 0.0  # and honestly idle since t=0
    store.touch("x", now=2000.0)
    p = next(iter(store.participants(now=2000.0, ttl=1e9)))
    assert (p.last_seen, p.last_active) == (2000.0, 2000.0)


def test_keepalive_alone_keeps_a_participant_live_past_the_ttl(store):
    # The point of the whole exercise: a session that makes no switchboard call
    # for an hour stays live as long as its process is still connected.
    store.register("busy", now=0.0)
    assert store.participants(now=3600.0, ttl=1800.0) == []  # silence alone expires it
    store.keepalive("busy", now=3599.0)
    assert [p.name for p in store.participants(now=3600.0, ttl=1800.0)] == ["busy"]


def test_keepalive_unregistered_is_noop(store):
    store.keepalive("nobody", now=1.0)  # must not raise
    store.keepalive("", now=1.0)  # nor an empty name
    assert store.participants(now=1.0) == []


def test_prune_deletes_expired(store):
    store.register("old", now=0.0)
    store.register("new", now=1000.0)
    removed = store.prune_participants(now=1000.0, ttl=300.0)
    assert removed == 1
    assert [p.name for p in store.participants(now=1000.0)] == ["new"]


# -- send / inbox ----------------------------------------------------------


def test_send_and_claim_roundtrip(store):
    store.register("b", now=1.0)
    mid = store.send("b", "hello", sender="a", now=2.0)
    assert isinstance(mid, int) and mid > 0

    msgs = store.inbox("b", now=1000.0)
    assert len(msgs) == 1
    m = msgs[0].to_dict()
    assert m["id"] == mid
    assert m["to"] == "b"
    assert m["from"] == "a"
    assert m["body"] == "hello"
    assert m["reply_to"] is None

    # An active lease hides the message from a second read.
    assert store.inbox("b", now=1000.0) == []


def test_peek_does_not_claim(store):
    store.send("b", "hi", sender="a", now=1.0)
    assert len(store.inbox("b", peek=True, now=1000.0)) == 1
    assert len(store.inbox("b", peek=True, now=1000.0)) == 1  # still there
    assert len(store.inbox("b", now=1000.0)) == 1  # now claimed
    assert store.inbox("b", peek=True, now=1000.0) == []


def test_messages_ordered_oldest_first(store):
    store.send("b", "one", sender="a", now=1.0)
    store.send("b", "two", sender="a", now=2.0)
    store.send("b", "three", sender="a", now=3.0)
    bodies = [m.body for m in store.inbox("b", now=1000.0)]
    assert bodies == ["one", "two", "three"]


def test_reply_to_roundtrip(store):
    mid = store.send("b", "question", sender="a", now=1.0)
    reply_id = store.send("a", "answer", sender="b", reply_to=mid, now=2.0)
    replies = store.inbox("a", now=1000.0)
    assert len(replies) == 1
    assert replies[0].reply_to == mid
    assert reply_id == replies[0].id


def test_lead_worker_two_way(store):
    # Acceptance criterion 2, at the store layer.
    store.register("lead", "coordinator", now=1.0)
    store.register("worker:x", "worker", now=1.0)

    q = store.send("lead", "what is 2+2?", sender="worker:x", now=2.0)
    got = store.inbox("lead", now=1000.0)
    assert [m.body for m in got] == ["what is 2+2?"]

    store.send("worker:x", "4", sender="lead", reply_to=q, now=3.0)
    reply = store.inbox("worker:x", now=1000.0)
    assert [m.body for m in reply] == ["4"]
    assert reply[0].reply_to == q


def test_isolation_between_recipients(store):
    store.send("b", "for-b", sender="a", now=1.0)
    store.send("c", "for-c", sender="a", now=1.0)
    assert [m.body for m in store.inbox("b", now=1000.0)] == ["for-b"]
    assert [m.body for m in store.inbox("c", now=1000.0)] == ["for-c"]


def test_since_filters_older_messages(store):
    m1 = store.send("b", "old", sender="a", now=1.0)
    m2 = store.send("b", "new", sender="a", now=2.0)
    peeked = store.inbox("b", peek=True, since=m1, now=1000.0)
    assert [m.id for m in peeked] == [m2]


def test_send_to_durable_mailbox_before_register(store):
    # Sending to a name that has never registered is fine; it waits.
    store.send("future", "waiting for you", sender="a", now=1.0)
    assert [m.body for m in store.inbox("future", now=1000.0)] == ["waiting for you"]


def test_send_rejects_empty_recipient(store):
    with pytest.raises(ValueError):
        store.send("", "body", sender="a", now=1.0)


def test_delivery_lease_status_and_acknowledgment(store):
    from switchboard_relay.store import MESSAGE_STATUS_RETENTION_SECONDS

    mid = store.send("worker", "work", sender="lead", now=1.0)
    assert store.status(mid, sender="lead", now=2)["status"] == "queued"
    assert store.status(mid, sender="worker", now=2) is None
    [peeked] = store.inbox("one", "worker", now=2, peek=True)
    assert "receipt" not in peeked.to_dict()

    [first] = store.inbox("one", "worker", now=2, lease=3)
    assert first.lease_expires_at == 5
    assert store.status(mid, sender="lead", now=3) == {
        "id": mid,
        "to": "worker",
        "status": "leased",
        "created_at": 1.0,
        "claimed_at": 2.0,
        "lease_expires_at": 5.0,
    }
    assert not store.has_messages("two", "worker", now=3)
    assert store.ack(mid, first.receipt, name="two", clock=lambda: 3) is None
    assert store.ack(mid, first.receipt, name="one", clock=lambda: 5) is None
    assert store.status(mid, sender="lead", now=5)["status"] == "queued"

    [second] = store.inbox("one", "worker", now=5, lease=3)
    assert second.id == first.id and second.receipt != first.receipt
    assert store.ack(mid, first.receipt, name="one", clock=lambda: 6) is None
    assert store.ack(mid, second.receipt, name="one", clock=lambda: 6) == 6
    assert store.ack(mid, second.receipt, name="one", clock=lambda: 7) == 6
    assert store.inbox("two", "worker", now=9) == []
    assert store.status(mid, sender="lead", now=9) == {
        "id": mid,
        "to": "worker",
        "status": "acknowledged",
        "created_at": 1.0,
        "acknowledged_at": 6.0,
    }
    with store._connect() as conn:
        assert (
            conn.execute("SELECT body FROM message_deliveries WHERE id=?", (mid,)).fetchone()[0]
            is None
        )
    after_retention = 6 + MESSAGE_STATUS_RETENTION_SECONDS
    assert store.status(mid, sender="lead", now=after_retention) is None
    assert store.ack(mid, second.receipt, name="one", clock=lambda: after_retention) is None
    assert store.prune_messages(now=after_retention, max_age=None) == 0
    with store._connect() as conn:
        assert not conn.execute("SELECT 1 FROM message_deliveries").fetchone()


def test_ack_samples_clock_under_the_writer_lock(store):
    mid = store.send("worker", "job", sender="lead", now=1)
    [message] = store.inbox("worker", now=2, lease=3)

    def clock():
        with (
            contextlib.closing(sqlite3.connect(store.db_path, timeout=0)) as other,
            pytest.raises(sqlite3.OperationalError, match="locked"),
        ):
            other.execute("BEGIN IMMEDIATE")
        return 5

    assert store.ack(mid, message.receipt, name="worker", clock=clock) is None
    assert store.status(mid, sender="lead", now=5)["status"] == "queued"


def test_incremental_availability_catches_claims_committed_after_a_snapshot(store):
    from switchboard_relay.store import AvailabilityCursor

    mid = store.send("worker", "job", sender="lead", now=1)
    rows, cursor = store.newly_available(AvailabilityCursor(), name="worker", now=10, max_age=None)
    assert [row["id"] for row in rows] == [mid]
    # A claim with a deadline before the cursor became visible after its snapshot.
    store.inbox("worker", now=9, lease=0.5)
    rows, cursor = store.newly_available(cursor, name="worker", now=11, max_age=None)
    assert [(row["id"], row["retry"]) for row in rows] == [(mid, 1)]
    rows, _ = store.newly_available(cursor, name="worker", now=12, max_age=None)
    assert rows == []


def test_age_out_covers_queued_messages_and_active_claims(store):
    from switchboard_relay.store import MESSAGE_STATUS_RETENTION_SECONDS

    leased = store.send("worker", "leased", sender="lead", now=1)
    [claim] = store.inbox("worker", now=2, lease=100)
    queued = store.send("worker", "queued", sender="lead", now=2)
    assert store.status(leased, sender="lead", now=6, max_age=5)["expired_at"] == 6
    assert store.status(queued, sender="lead", now=7, max_age=5)["expired_at"] == 7
    assert store.ack(leased, claim.receipt, name="worker", clock=lambda: 7, max_age=5) is None
    assert store.inbox("worker", now=7, max_age=5) == []
    assert store.prune_messages(now=7, max_age=5) == 2
    assert store.status(leased, sender="lead", now=7)["status"] == "expired"
    assert store.status(queued, sender="lead", now=7)["expired_at"] == 7
    assert store.ack(leased, claim.receipt, name="worker", clock=lambda: 7) is None
    assert store.status(queued, sender="lead", now=7 + MESSAGE_STATUS_RETENTION_SECONDS) is None


def test_legacy_queue_can_be_read_and_cannot_remove_new_claims(tmp_path):
    path = tmp_path / "legacy.db"
    with contextlib.closing(sqlite3.connect(path)) as legacy, legacy:
        legacy.execute(
            """CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT, recipient TEXT NOT NULL,
                sender TEXT NOT NULL, body TEXT NOT NULL, reply_to INTEGER, created_at REAL NOT NULL
            )"""
        )
        mid = legacy.execute(
            "INSERT INTO messages (recipient,sender,body,created_at) VALUES ('worker','lead','old',1)"
        ).lastrowid

    from switchboard_relay.store import AvailabilityCursor

    legacy_store = Store(path, init_schema=False)
    assert legacy_store.pending_messages(now=2)[0].id == mid
    rows, _ = legacy_store.newly_available(AvailabilityCursor(), now=2, max_age=None)
    assert [row["id"] for row in rows] == [mid]
    store = Store(path)
    [claim] = store.inbox("worker", now=2, lease=2)
    with contextlib.closing(sqlite3.connect(path)) as legacy, legacy:
        assert legacy.execute("DELETE FROM messages WHERE recipient='worker'").rowcount == 0
    [redelivery] = store.inbox("worker", now=4)
    assert redelivery.id == mid and redelivery.receipt != claim.receipt
    assert store.ack(mid, redelivery.receipt, name="worker", clock=lambda: 5) == 5
    assert store.send("worker", "new", sender="lead", now=5) > mid


# -- unregister ------------------------------------------------------------


def test_unregister_removes_participant(store):
    store.register("lead", "coordinator", now=1.0)
    assert store.unregister("lead") is True
    assert store.participants(now=1.0) == []


def test_unregister_unknown_returns_false(store):
    assert store.unregister("ghost") is False
    assert store.unregister("") is False


def test_unregister_preserves_mailbox(store):
    store.register("lead", now=1.0)
    store.send("lead", "still here", sender="worker", now=2.0)
    store.unregister("lead")
    # Leaving does not discard undelivered messages.
    assert [m.body for m in store.inbox("lead", now=1000.0)] == ["still here"]


# -- take_reply (ask() primitive) ------------------------------------------


def test_take_reply_claims_only_the_matching_reply(store):
    q = store.send("worker", "question", sender="lead", now=1.0)
    # An unrelated message and the actual reply both land for worker.
    store.send("worker", "unrelated", sender="other", now=2.0)
    store.send("worker", "the answer", sender="lead", reply_to=q, now=3.0)

    reply = store.take_reply("worker", reply_to=q, now=1000.0)
    assert reply is not None
    assert reply.body == "the answer"
    assert reply.reply_to == q
    # The unrelated message is untouched.
    assert [m.body for m in store.inbox("worker", now=1000.0)] == ["question", "unrelated"]


def test_take_reply_recovers_the_same_reply_after_lease_expiry(store):
    mid = store.send("worker", "answer", sender="lead", reply_to=42, now=1)
    first = store.take_reply("worker", reply_to=42, now=2, lease=2)
    assert first is not None
    assert store.take_reply("worker", reply_to=42, now=3) is None
    retry = store.take_reply("worker", reply_to=42, now=4)
    assert retry is not None and retry.id == mid
    assert retry.receipt != first.receipt


def test_take_reply_returns_none_when_no_match(store):
    store.send("worker", "no reply here", sender="lead", now=1.0)
    assert store.take_reply("worker", reply_to=999, now=1000.0) is None


def test_take_reply_matches_role(store):
    q = 7
    store.send("worker", "answer", sender="lead", reply_to=q, now=1.0)
    reply = store.take_reply("worker:1", role="worker", reply_to=q, now=1000.0)
    assert reply is not None and reply.body == "answer"


def test_take_reply_oldest_first(store):
    store.send("w", "first", sender="a", reply_to=5, now=1.0)
    store.send("w", "second", sender="a", reply_to=5, now=2.0)
    assert store.take_reply("w", reply_to=5, now=1000.0).body == "first"
    assert store.take_reply("w", reply_to=5, now=1000.0).body == "second"


# -- pending_messages (CLI inspection) -------------------------------------


def test_pending_messages_lists_all_queued(store):
    store.send("a", "one", sender="x", now=1.0)
    store.send("b", "two", sender="y", now=2.0)
    pending = store.pending_messages(now=1000.0)
    assert [(m.to, m.body) for m in pending] == [("a", "one"), ("b", "two")]


def test_pending_messages_respects_limit(store):
    for i in range(5):
        store.send("a", f"m{i}", sender="x", now=float(i))
    assert len(store.pending_messages(limit=3, now=1000.0)) == 3


def test_prune_messages_removes_old_keeps_new(store):
    store.send("a", "old", sender="x", now=100.0)
    store.send("a", "new", sender="x", now=200.0)
    removed = store.prune_messages(now=250.0, max_age=100.0)
    assert removed == 1
    assert [m.body for m in store.inbox("a", peek=True, now=1000.0)] == ["new"]


def test_pending_messages_since_pages_through_backlog(store):
    ids = [store.send("a", f"m{i}", sender="x", now=float(i)) for i in range(5)]
    # Everything after the 2nd id.
    rest = store.pending_messages(since=ids[1], now=1000.0)
    assert [m.id for m in rest] == ids[2:]
    # since past the end -> empty.
    assert store.pending_messages(since=ids[-1], now=1000.0) == []


# -- role addressing -------------------------------------------------------


def test_inbox_matches_role_address(store):
    # A message addressed to the role "worker" is drained by a participant that
    # reads with that role.
    store.send("worker", "job for any worker", sender="lead", now=1.0)
    msgs = store.inbox("worker:1", role="worker", now=1000.0)
    assert [m.body for m in msgs] == ["job for any worker"]


def test_inbox_matches_both_name_and_role(store):
    store.send("worker:1", "personal", sender="lead", now=1.0)
    store.send("worker", "broadcast", sender="lead", now=2.0)
    msgs = store.inbox("worker:1", role="worker", now=1000.0)
    assert sorted(m.body for m in msgs) == ["broadcast", "personal"]


def test_role_message_not_seen_by_other_role(store):
    store.send("worker", "job", sender="lead", now=1.0)
    # A reviewer must not drain a worker-addressed message.
    assert store.inbox("reviewer:1", role="reviewer", now=1000.0) == []
    # And the worker still gets it.
    assert [m.body for m in store.inbox("worker:1", role="worker", now=1000.0)] == ["job"]


def test_has_messages(store):
    assert store.has_messages("b", now=1000.0) is False
    store.send("b", "x", sender="a", now=1.0)
    assert store.has_messages("b", now=1000.0) is True
    assert store.has_messages("b", since=10_000, now=1000.0) is False


# -- durability across reconnects ------------------------------------------


def test_messages_survive_new_store_instance(tmp_path):
    path = tmp_path / "sb.db"
    s1 = Store(path)
    s1.register("b", "worker", now=1.0)
    s1.send("b", "durable", sender="a", now=2.0)
    s1.close()

    # A brand new Store over the same file (simulating a restart) sees it.
    s2 = Store(path)
    assert [m.body for m in s2.inbox("b", peek=True, now=1000.0)] == ["durable"]
    live = [p.name for p in s2.participants(now=2.0)]
    assert "b" in live
    s2.close()


def test_init_schema_false_reads_existing_without_reinit(tmp_path):
    path = tmp_path / "sb.db"
    Store(path).register("lead", "coordinator", now=1.0)  # creates schema + data
    # An inspection-style open: no schema-establishing writes, still reads fine.
    ro = Store(path, init_schema=False)
    assert [p.name for p in ro.participants(now=1.0, ttl=300)] == ["lead"]
    ro.close()


def test_wal_mode_enabled(tmp_path):
    path = tmp_path / "sb.db"
    s = Store(path)
    # `with sqlite3.connect(...)` only manages the transaction, not the handle;
    # close it explicitly so it doesn't leak (ResourceWarning under -W error).
    conn = sqlite3.connect(str(path))
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        conn.close()
    assert mode.lower() == "wal"
    s.close()


def test_concurrent_claim_delivers_each_message_once_during_the_lease(tmp_path):
    """Parallel readers cannot claim the same message within one lease."""
    import threading

    path = tmp_path / "sb.db"
    seed = Store(path)
    n = 200
    for i in range(n):
        seed.send("b", f"m{i}", sender="a", now=float(i))
    seed.close()

    collected: list[str] = []
    lock = threading.Lock()

    def claim_worker():
        s = Store(path)
        try:
            while True:
                got = s.inbox("b", now=1000.0)
                if not got:
                    # One empty read isn't proof of "done" under contention, so
                    # check the shared store directly before giving up.
                    if not s.has_messages("b", now=1000.0):
                        return
                    continue
                with lock:
                    collected.extend(m.body for m in got)
        finally:
            s.close()

    threads = [threading.Thread(target=claim_worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(collected) == sorted(f"m{i}" for i in range(n))
    assert len(collected) == n


def test_concurrent_register_same_name_is_idempotent(tmp_path):
    """Many processes claiming the same fresh name must not crash (atomic upsert).

    A check-then-insert would let two racers both INSERT and the second hit the
    PRIMARY KEY constraint; the upsert makes it an idempotent create-or-refresh.
    """
    import threading

    path = tmp_path / "sb.db"
    Store(path).close()  # create schema/WAL once up front

    n = 16
    barrier = threading.Barrier(n)
    errors: list[Exception] = []

    def worker():
        s = Store(path)
        try:
            barrier.wait()  # maximize contention on the INSERT
            s.register("racer", "worker", now=100.0)
        except Exception as exc:  # pragma: no cover - regression guard
            errors.append(exc)
        finally:
            s.close()

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], errors
    final = Store(path)
    try:
        live = final.participants(now=100.0)
        assert [p.name for p in live] == ["racer"]  # exactly one row
        assert live[0].role == "worker"
    finally:
        final.close()


def test_send_rejects_non_string_body(store):
    with pytest.raises(ValueError):
        store.send("b", 123, sender="a", now=1.0)  # type: ignore[arg-type]


def test_touch_empty_name_is_noop(store):
    store.touch("", now=1.0)  # must not raise or write anything
    assert store.participants(now=1.0) == []


def test_memory_store_full_lifecycle():
    # The ":memory:" path keeps one shared connection alive; exercise it end to
    # end and then close it.
    s = Store(":memory:")
    try:
        s.register("lead", "coordinator", now=1.0)
        s.send("lead", "hi", sender="worker", now=2.0)
        assert [m.body for m in s.inbox("lead", now=1000.0)] == ["hi"]
        assert [p.name for p in s.participants(now=2.0)] == ["lead"]
    finally:
        s.close()
    # Idempotent close.
    s.close()


def test_default_db_path_honors_env(monkeypatch, tmp_path):
    monkeypatch.setenv("SWITCHBOARD_DB", str(tmp_path / "custom.db"))
    assert default_db_path() == tmp_path / "custom.db"


def test_default_db_path_falls_back_to_home(monkeypatch, tmp_path):
    monkeypatch.delenv("SWITCHBOARD_DB", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert default_db_path() == tmp_path / ".claude" / "switchboard.db"


# -- lock retry & WAL enablement (concurrency helpers) ---------------------


def test_with_lock_retry_retries_then_succeeds(monkeypatch):
    monkeypatch.setattr(store_mod.time, "sleep", lambda _s: None)  # no real sleeping
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise sqlite3.OperationalError("database is locked")
        return "ok"

    assert _with_lock_retry(flaky) == "ok"
    assert calls["n"] == 3


def test_with_lock_retry_reraises_non_lock_error():
    def boom():
        raise sqlite3.OperationalError("no such table: participants")

    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        _with_lock_retry(boom)


def test_with_lock_retry_exhausts_attempts_and_raises(monkeypatch):
    monkeypatch.setattr(store_mod.time, "sleep", lambda _s: None)

    def always_locked():
        raise sqlite3.OperationalError("database is locked")

    with pytest.raises(sqlite3.OperationalError, match="locked"):
        _with_lock_retry(always_locked, attempts=3)


def test_enable_wal_retries_until_switch_takes(monkeypatch):
    monkeypatch.setattr(store_mod.time, "sleep", lambda _s: None)
    state = {"n": 0}

    class FakeConn:
        def execute(self, sql):
            if "journal_mode" in sql:
                state["n"] += 1
                mode = "delete" if state["n"] < 2 else "wal"  # first attempt "loses" the lock
                return types.SimpleNamespace(fetchone=lambda: (mode,))
            return types.SimpleNamespace(fetchone=lambda: None)  # PRAGMA synchronous

    _enable_wal(FakeConn())  # must retry past the non-wal result, then succeed
    assert state["n"] >= 2


def test_db_path_expands_user(tmp_path, monkeypatch):
    """A '~' in the db path resolves to the home dir, not a literal '~' folder."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))  # Windows fallback
    s = Store("~/nested/switchboard.db")
    try:
        assert "~" not in str(s.db_path)
        assert s.db_path == (tmp_path / "nested" / "switchboard.db")
        assert (tmp_path / "nested").is_dir()  # parent created under real home
    finally:
        s.close()


def test_reply_and_ack_roll_back_if_the_key_record_fails(store):
    message_id = store.send("lead", "Question", sender="worker", now=1)
    [question] = store.inbox("lead", now=2)
    with store._connect() as conn:
        conn.execute("""
            CREATE TRIGGER reject_key BEFORE INSERT ON relay_submissions
            BEGIN SELECT RAISE(ABORT, 'test failure'); END
        """)
    with pytest.raises(sqlite3.IntegrityError, match="test failure"):
        store.send_reply(
            "worker",
            "Answer",
            sender="lead",
            reply_to=message_id,
            receipt=question.receipt,
            owner_token=None,
            clock=lambda: 3,
            max_age=None,
            idempotency_key="reply",
        )
    assert store.inbox("worker", now=4, peek=True) == []
    assert store.status(message_id, sender="worker", now=4)["status"] == "leased"


def test_old_submission_key_survives_reply_ack_migration(tmp_path):
    import hashlib
    import json

    path = tmp_path / "board.db"
    fingerprint = hashlib.sha256(
        json.dumps(
            ["lead", "A previous message", None],
            separators=(",", ":"),
        ).encode()
    ).digest()
    with contextlib.closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("""
            CREATE TABLE relay_submissions (
                operation TEXT NOT NULL, sender TEXT NOT NULL, request_key TEXT NOT NULL,
                fingerprint BLOB NOT NULL, result_id INTEGER NOT NULL, created_at REAL NOT NULL,
                PRIMARY KEY (operation, sender, request_key)
            )
        """)
        conn.execute(
            "INSERT INTO relay_submissions VALUES ('send','worker','old-key',?,23,1)",
            (fingerprint,),
        )

    store = Store(path)
    assert (
        store.send(
            "lead",
            "A previous message",
            sender="worker",
            now=2,
            idempotency_key="old-key",
        )
        == 23
    )
    assert store.inbox("lead", now=3, peek=True) == []
