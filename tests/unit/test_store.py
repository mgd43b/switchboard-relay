"""Unit tests for the switchboard-relay SQLite store.

These exercise the pure store in isolation: register/heartbeat, send/inbox
drain semantics, role addressing, `since`, and TTL expiry. Time is injected via
`now` so the tests are deterministic (no sleeps, no wall clock).
"""

from __future__ import annotations

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


def test_touch_refreshes_liveness(store):
    store.register("x", now=0.0)
    store.touch("x", now=290.0)
    # Without the touch this would be expired at now=299 (ttl 300 from t=0).
    assert [p.name for p in store.participants(now=299.0, ttl=300.0)] == ["x"]


def test_touch_unregistered_is_noop(store):
    store.touch("nobody", now=1.0)  # must not raise
    assert store.participants(now=1.0) == []


def test_prune_deletes_expired(store):
    store.register("old", now=0.0)
    store.register("new", now=1000.0)
    removed = store.prune_participants(now=1000.0, ttl=300.0)
    assert removed == 1
    assert [p.name for p in store.participants(now=1000.0)] == ["new"]


# -- send / inbox ----------------------------------------------------------


def test_send_and_drain_roundtrip(store):
    store.register("b", now=1.0)
    mid = store.send("b", "hello", sender="a", now=2.0)
    assert isinstance(mid, int) and mid > 0

    msgs = store.inbox("b")
    assert len(msgs) == 1
    m = msgs[0].to_dict()
    assert m["id"] == mid
    assert m["to"] == "b"
    assert m["from"] == "a"
    assert m["body"] == "hello"
    assert m["reply_to"] is None

    # Drained: a second read returns nothing.
    assert store.inbox("b") == []


def test_peek_does_not_drain(store):
    store.send("b", "hi", sender="a", now=1.0)
    assert len(store.inbox("b", peek=True)) == 1
    assert len(store.inbox("b", peek=True)) == 1  # still there
    assert len(store.inbox("b")) == 1  # now drained
    assert store.inbox("b", peek=True) == []


def test_messages_ordered_oldest_first(store):
    store.send("b", "one", sender="a", now=1.0)
    store.send("b", "two", sender="a", now=2.0)
    store.send("b", "three", sender="a", now=3.0)
    bodies = [m.body for m in store.inbox("b")]
    assert bodies == ["one", "two", "three"]


def test_reply_to_roundtrip(store):
    mid = store.send("b", "question", sender="a", now=1.0)
    reply_id = store.send("a", "answer", sender="b", reply_to=mid, now=2.0)
    replies = store.inbox("a")
    assert len(replies) == 1
    assert replies[0].reply_to == mid
    assert reply_id == replies[0].id


def test_lead_worker_two_way(store):
    # Acceptance criterion 2, at the store layer.
    store.register("lead", "coordinator", now=1.0)
    store.register("worker:x", "worker", now=1.0)

    q = store.send("lead", "what is 2+2?", sender="worker:x", now=2.0)
    got = store.inbox("lead")
    assert [m.body for m in got] == ["what is 2+2?"]

    store.send("worker:x", "4", sender="lead", reply_to=q, now=3.0)
    reply = store.inbox("worker:x")
    assert [m.body for m in reply] == ["4"]
    assert reply[0].reply_to == q


def test_isolation_between_recipients(store):
    store.send("b", "for-b", sender="a", now=1.0)
    store.send("c", "for-c", sender="a", now=1.0)
    assert [m.body for m in store.inbox("b")] == ["for-b"]
    assert [m.body for m in store.inbox("c")] == ["for-c"]


def test_since_filters_older_messages(store):
    m1 = store.send("b", "old", sender="a", now=1.0)
    m2 = store.send("b", "new", sender="a", now=2.0)
    peeked = store.inbox("b", peek=True, since=m1)
    assert [m.id for m in peeked] == [m2]


def test_send_to_durable_mailbox_before_register(store):
    # Sending to a name that has never registered is fine; it waits.
    store.send("future", "waiting for you", sender="a", now=1.0)
    assert [m.body for m in store.inbox("future")] == ["waiting for you"]


def test_send_rejects_empty_recipient(store):
    with pytest.raises(ValueError):
        store.send("", "body", sender="a", now=1.0)


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
    assert [m.body for m in store.inbox("lead")] == ["still here"]


# -- take_reply (ask() primitive) ------------------------------------------


def test_take_reply_drains_only_the_matching_reply(store):
    q = store.send("worker", "question", sender="lead", now=1.0)
    # An unrelated message and the actual reply both land for worker.
    store.send("worker", "unrelated", sender="other", now=2.0)
    store.send("worker", "the answer", sender="lead", reply_to=q, now=3.0)

    reply = store.take_reply("worker", reply_to=q)
    assert reply is not None
    assert reply.body == "the answer"
    assert reply.reply_to == q
    # The unrelated message is untouched.
    assert [m.body for m in store.inbox("worker")] == ["question", "unrelated"]


def test_take_reply_returns_none_when_no_match(store):
    store.send("worker", "no reply here", sender="lead", now=1.0)
    assert store.take_reply("worker", reply_to=999) is None


def test_take_reply_matches_role(store):
    q = 7
    store.send("worker", "answer", sender="lead", reply_to=q, now=1.0)
    reply = store.take_reply("worker:1", role="worker", reply_to=q)
    assert reply is not None and reply.body == "answer"


def test_take_reply_oldest_first(store):
    store.send("w", "first", sender="a", reply_to=5, now=1.0)
    store.send("w", "second", sender="a", reply_to=5, now=2.0)
    assert store.take_reply("w", reply_to=5).body == "first"
    assert store.take_reply("w", reply_to=5).body == "second"


# -- pending_messages (CLI inspection) -------------------------------------


def test_pending_messages_lists_all_queued(store):
    store.send("a", "one", sender="x", now=1.0)
    store.send("b", "two", sender="y", now=2.0)
    pending = store.pending_messages()
    assert [(m.to, m.body) for m in pending] == [("a", "one"), ("b", "two")]


def test_pending_messages_respects_limit(store):
    for i in range(5):
        store.send("a", f"m{i}", sender="x", now=float(i))
    assert len(store.pending_messages(limit=3)) == 3


def test_prune_messages_removes_old_keeps_new(store):
    store.send("a", "old", sender="x", now=100.0)
    store.send("a", "new", sender="x", now=200.0)
    removed = store.prune_messages(older_than=150.0)
    assert removed == 1
    assert [m.body for m in store.inbox("a", peek=True)] == ["new"]


def test_pending_messages_since_pages_through_backlog(store):
    ids = [store.send("a", f"m{i}", sender="x", now=float(i)) for i in range(5)]
    # Everything after the 2nd id.
    rest = store.pending_messages(since=ids[1])
    assert [m.id for m in rest] == ids[2:]
    # since past the end -> empty.
    assert store.pending_messages(since=ids[-1]) == []


# -- role addressing -------------------------------------------------------


def test_inbox_matches_role_address(store):
    # A message addressed to the role "worker" is drained by a participant that
    # reads with that role.
    store.send("worker", "job for any worker", sender="lead", now=1.0)
    msgs = store.inbox("worker:1", role="worker")
    assert [m.body for m in msgs] == ["job for any worker"]


def test_inbox_matches_both_name_and_role(store):
    store.send("worker:1", "personal", sender="lead", now=1.0)
    store.send("worker", "broadcast", sender="lead", now=2.0)
    msgs = store.inbox("worker:1", role="worker")
    assert sorted(m.body for m in msgs) == ["broadcast", "personal"]


def test_role_message_not_seen_by_other_role(store):
    store.send("worker", "job", sender="lead", now=1.0)
    # A reviewer must not drain a worker-addressed message.
    assert store.inbox("reviewer:1", role="reviewer") == []
    # And the worker still gets it.
    assert [m.body for m in store.inbox("worker:1", role="worker")] == ["job"]


def test_has_messages(store):
    assert store.has_messages("b") is False
    store.send("b", "x", sender="a", now=1.0)
    assert store.has_messages("b") is True
    assert store.has_messages("b", since=10_000) is False


# -- durability across reconnects ------------------------------------------


def test_messages_survive_new_store_instance(tmp_path):
    path = tmp_path / "sb.db"
    s1 = Store(path)
    s1.register("b", "worker", now=1.0)
    s1.send("b", "durable", sender="a", now=2.0)
    s1.close()

    # A brand new Store over the same file (simulating a restart) sees it.
    s2 = Store(path)
    assert [m.body for m in s2.inbox("b", peek=True)] == ["durable"]
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


def test_concurrent_drain_delivers_each_message_once(tmp_path):
    """Parallel drainers must never see the same message twice (atomic drain)."""
    import threading

    path = tmp_path / "sb.db"
    seed = Store(path)
    n = 200
    for i in range(n):
        seed.send("b", f"m{i}", sender="a", now=float(i))
    seed.close()

    collected: list[str] = []
    lock = threading.Lock()

    def drain_worker():
        s = Store(path)
        try:
            while True:
                got = s.inbox("b")  # drains
                if not got:
                    # One empty read isn't proof of "done" under contention, so
                    # check the shared store directly before giving up.
                    if not s.has_messages("b"):
                        return
                    continue
                with lock:
                    collected.extend(m.body for m in got)
        finally:
            s.close()

    threads = [threading.Thread(target=drain_worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(collected) == sorted(f"m{i}" for i in range(n))
    assert len(collected) == n  # exactly once, no duplicates


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
        assert [m.body for m in s.inbox("lead")] == ["hi"]
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
