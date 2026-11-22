"""Reconcile sent and acknowledged IDs under multi-process shared-role contention.

The workers claim from the same role over WAL and acknowledge each delivery.
A long lease prevents redelivery during this test so duplicate IDs expose
overlapping claims. Disconnect and redelivery are covered by the stdio tests.
"""

from __future__ import annotations

import multiprocessing as mp
import time

from switchboard_relay.store import Store

# Kept small enough for CI but large enough to actually contend.
SENDERS = 4
DRAINERS = 3
PER_SENDER = 40
TOTAL = SENDERS * PER_SENDER
_ROLE = "worker"
_SAFETY_TIMEOUT_S = 30.0


def _sender(db_path: str, idx: int, count: int, sent_ids) -> None:
    store = Store(db_path)
    for j in range(count):
        mid = store.send(_ROLE, f"{idx}-{j}", sender=f"s{idx}", now=time.time())
        sent_ids.append(mid)


def _receiver(db_path: str, name: str, done, acked_ids, deadline: float) -> None:
    store = Store(db_path)
    empty_streak = 0
    while True:
        got = store.inbox(name, _ROLE, now=time.time())
        if got:
            for message in got:
                assert (
                    store.ack(message.id, message.receipt, name=name, clock=time.time) is not None
                )
            acked_ids.extend([m.id for m in got])
            empty_streak = 0
        else:
            empty_streak += 1
        # Exit only once senders are finished AND we've seen the mailbox empty a
        # few polls running -- so nothing in flight is missed.
        if done.is_set() and empty_streak >= 3:
            return
        if time.time() > deadline:  # pragma: no cover - safety valve, should not fire
            return
        time.sleep(0.005)


def test_n_process_claim_and_ack_shared_role(tmp_path):
    ctx = mp.get_context("spawn")  # deterministic across platforms
    db = str(tmp_path / "stress.db")
    Store(db).close()  # create schema + WAL up front so workers don't race first-init

    with ctx.Manager() as mgr:
        sent_ids = mgr.list()
        acked_ids = mgr.list()
        done = ctx.Event()
        deadline = time.time() + _SAFETY_TIMEOUT_S

        drainers = [
            ctx.Process(target=_receiver, args=(db, f"d{k}", done, acked_ids, deadline))
            for k in range(DRAINERS)
        ]
        for d in drainers:
            d.start()
        senders = [
            ctx.Process(target=_sender, args=(db, i, PER_SENDER, sent_ids)) for i in range(SENDERS)
        ]
        for s in senders:
            s.start()

        for s in senders:
            s.join(_SAFETY_TIMEOUT_S)
        done.set()  # tell drainers the producers are finished
        for d in drainers:
            d.join(_SAFETY_TIMEOUT_S)

        # No worker should still be running; kill any straggler and fail loudly so
        # a hang surfaces here instead of leaking a process past the test.
        stragglers = [p for p in (*senders, *drainers) if p.is_alive()]
        for p in stragglers:
            p.terminate()
            p.join(1)
        assert not stragglers, f"{len(stragglers)} worker process(es) did not exit in time"

        sent = list(sent_ids)
        acknowledged = list(acked_ids)

    assert len(sent) == TOTAL, "every send must return an id"
    assert len(acknowledged) == len(set(acknowledged)), "no message claimed by two workers"
    assert sorted(acknowledged) == sorted(sent), "every sent message acknowledged"
