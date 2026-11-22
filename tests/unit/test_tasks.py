"""Task claim fencing and durable history on a shared SQLite board."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from switchboard_relay.protocol import Protocol
from switchboard_relay.store import Store
from switchboard_relay.tasks import TASK_LEASE_SECONDS, TaskQueue


def test_task_lease_contention_renewal_and_recovery(tmp_path):
    path = tmp_path / "board.db"
    store = Store(path)
    protocol = Protocol(store, lease=10_000)
    for name, role in (("lead", ""), ("one", "worker"), ("two", "worker"), ("other", "reviewer")):
        protocol.claim(name, name, role, now=0)
    queue = TaskQueue(store)
    task = queue.submit(
        "lead",
        "lead",
        role="worker",
        scope="Fix docs",
        owned_paths=["README.md"],
        acceptance_criteria=["Examples pass"],
        expected_evidence="Test output",
        clock=lambda: 1,
    )
    task_id = task["id"]
    assert queue.claim("other", "other", "reviewer", clock=lambda: 2) is None
    assert queue.status(task_id, name="other", role="reviewer", token="other", now=2) is None

    def claim(name):
        return TaskQueue(Store(path)).claim(name, name, "worker", clock=lambda: 2)

    with ThreadPoolExecutor(max_workers=2) as pool:
        claimed = [task for task in pool.map(claim, ["one", "two"]) if task]
    assert len(claimed) == 1
    first = claimed[0]
    owner = first["owner"]
    other = "two" if owner == "one" else "one"
    original = {"task_id": task_id, "receipt": first["receipt"]}

    def update(name, args, action, at, note=""):
        return queue.update(name, name, **args, action=action, note=note, clock=lambda: at)

    assert update(other, original, "completed", 2, "wrong owner") is None
    assert update(owner, original, "blocked", 3, "Waiting for input")["status"] == "blocked"
    assert (
        update(owner, original, "progress", 4, "Found the failing example")["status"] == "claimed"
    )
    renewed = update(owner, original, "renew", 5)
    expires = 5 + TASK_LEASE_SECONDS
    assert renewed == {"id": task_id, "status": "claimed", "lease_expires_at": expires}
    assert queue.claim(other, other, "worker", clock=lambda: expires - 1) is None
    assert update(owner, original, "failed", expires, "late") is None

    assert (
        queue.status(task_id, name="lead", role="", token="lead", now=expires)["status"] == "queued"
    )
    second = queue.claim(other, other, "worker", clock=lambda: expires)
    assert second["id"] == task_id
    assert second["attempts"][0]["status"] == "expired"
    assert second["attempts"][0]["updates"][1]["note"] == "Found the failing example"
    assert second["receipt"] != first["receipt"]
    assert update(owner, original, "completed", expires + 1, "stale") is None

    current = {"task_id": task_id, "receipt": second["receipt"]}
    result = update(other, current, "failed", expires + 2, "Reproduced the docs failure")
    assert result["status"] == "failed"
    assert update(other, current, "failed", expires + 3, "duplicate") == result
    assert update(other, current, "completed", expires + 3, "different outcome") is None
    assert queue.claim(other, other, "worker", clock=lambda: expires + 3) is None
    assert (
        queue.update(
            other,
            other,
            task_id=999,
            receipt=second["receipt"],
            action="renew",
            note="",
            clock=lambda: expires + 3,
        )
        is None
    )

    reopened = TaskQueue(Store(path)).status(
        task_id,
        name="lead",
        role="",
        token="lead",
        now=expires + 4,
    )
    assert reopened == result


def test_upgrade_indexes_existing_unfinished_tasks(tmp_path):
    store = Store(tmp_path / "board.db")
    protocol = Protocol(store, lease=100)
    protocol.claim("lead", "lead", "", now=0)
    protocol.claim("worker", "worker", "team", now=0)
    with store._connect() as conn:
        conn.executescript("""
            CREATE TABLE tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT, role TEXT NOT NULL, submitter TEXT NOT NULL,
                scope TEXT NOT NULL, owned_paths TEXT NOT NULL, acceptance TEXT NOT NULL,
                expected_evidence TEXT NOT NULL, created_at REAL NOT NULL,
                attempt INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX idx_tasks_role ON tasks (role, id);
            CREATE TABLE task_attempts (
                task_id INTEGER NOT NULL, number INTEGER NOT NULL, worker TEXT NOT NULL,
                owner_token TEXT NOT NULL, receipt TEXT NOT NULL, started_at REAL NOT NULL,
                lease_expires_at REAL NOT NULL, state TEXT NOT NULL, PRIMARY KEY (task_id, number)
            );
            INSERT INTO tasks VALUES
                (1,'team','lead','done','[]','[]','',1,1),
                (2,'team','lead','leased','[]','[]','',1,1),
                (3,'team','lead','queued','[]','[]','',1,0);
            INSERT INTO task_attempts VALUES
                (1,1,'worker','worker','old-1',1,2,'completed'),
                (2,1,'worker','worker','old-2',1,5,'blocked');
        """)

    page = TaskQueue(store, initialize=False).list_tasks(now=3)
    assert [task["status"] for task in page["tasks"]] == ["completed", "blocked", "queued"]
    queue = TaskQueue(store)
    assert queue.claim("worker", "worker", "team", clock=lambda: 3)["id"] == 3
    assert queue.claim("worker", "worker", "team", clock=lambda: 4) is None
    retry = queue.claim("worker", "worker", "team", clock=lambda: 5)
    assert retry["id"] == 2
    assert [attempt["status"] for attempt in retry["attempts"]] == ["expired", "claimed"]
    assert queue.status(1, name="lead", role="", token="lead", now=5)["status"] == "completed"


def test_cancellation_stays_pending_after_expiry_until_worker_confirms(tmp_path):
    store = Store(tmp_path / "board.db")
    protocol = Protocol(store, lease=10_000)
    for name, role in (("lead", ""), ("one", "worker"), ("two", "worker")):
        protocol.claim(name, name, role, now=0)
    queue = TaskQueue(store)
    info = {
        "role": "worker",
        "scope": "Fix docs",
        "owned_paths": [],
        "acceptance_criteria": [],
        "expected_evidence": "",
    }
    queued = queue.submit("lead", "lead", **info, clock=lambda: 1)["id"]
    assert queue.cancel("lead", "lead", task_id=queued, note="", clock=lambda: 2)["status"] == (
        "cancelled"
    )
    assert queue.claim("one", "one", "worker", clock=lambda: 2) is None

    task_id = queue.submit("lead", "lead", **info, clock=lambda: 3)["id"]
    claim = queue.claim("one", "one", "worker", clock=lambda: 4)

    def clock():
        return claim["lease_expires_at"] + 1

    pending = queue.cancel("lead", "lead", task_id=task_id, note="Stop", clock=clock)
    assert pending["status"] == "cancel_requested"
    assert pending["owner"] == "one" and pending["cancelled_at"] is None
    assert queue.claim("two", "two", "worker", clock=clock) is None
    with store._connect() as conn, pytest.raises(sqlite3.IntegrityError, match="cancellation"):
        conn.execute("UPDATE task_attempts SET lease_expires_at=lease_expires_at+1")
    assert (
        queue.update(
            "two",
            "two",
            task_id=task_id,
            receipt=claim["receipt"],
            action="cancelled",
            note="Wrong worker",
            clock=clock,
        )
        is None
    )
    final = queue.update(
        "one",
        "one",
        task_id=task_id,
        receipt=claim["receipt"],
        action="cancelled",
        note="Stopped",
        clock=clock,
    )
    assert final["status"] == "cancelled" and final["cancelled_at"] == clock()
    assert queue.cancel("lead", "lead", task_id=task_id, note="Again", clock=clock) == final
    with store._connect() as conn, pytest.raises(sqlite3.IntegrityError, match="cancellation"):
        conn.execute("UPDATE task_attempts SET lease_expires_at=lease_expires_at+1")
