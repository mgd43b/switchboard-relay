"""Task claim fencing and durable history on a shared SQLite board."""

from concurrent.futures import ThreadPoolExecutor

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
    assert renewed["lease_expires_at"] == expires
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
