"""N-process exactly-once delivery stress test.

The rest of the integration suite proves *delivery*; this proves the load-bearing
safety property under *contention*: with many independent OS processes reading and
writing the same board over WAL, every message is delivered **exactly once** -- no
loss, no duplication -- including the shared-role case where several drainers read
the same role address and each message must be claimed by exactly one of them.

It spawns real processes (not just tasks) so the drain's ``BEGIN IMMEDIATE`` lock is
exercised across process boundaries, the way independent Claude Code sessions run.
Bounded workers + message count keep it to a couple of seconds in CI.
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


def _drainer(db_path: str, name: str, done, recv_ids, deadline: float) -> None:
    # Drains messages addressed to the shared role. drain-once is enforced by the
    # store's BEGIN IMMEDIATE, so two drainers can never return the same row.
    store = Store(db_path)
    empty_streak = 0
    while True:
        got = store.inbox(name, _ROLE)  # drains atomically
        if got:
            recv_ids.extend([m.id for m in got])  # a list -- the proxy can't pickle a generator
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


def test_n_process_exactly_once_shared_role(tmp_path):
    ctx = mp.get_context("spawn")  # deterministic across platforms
    db = str(tmp_path / "stress.db")
    Store(db).close()  # create schema + WAL up front so workers don't race first-init

    with ctx.Manager() as mgr:
        sent_ids = mgr.list()
        recv_ids = mgr.list()
        done = ctx.Event()
        deadline = time.time() + _SAFETY_TIMEOUT_S

        drainers = [
            ctx.Process(target=_drainer, args=(db, f"d{k}", done, recv_ids, deadline))
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
        received = list(recv_ids)

    assert len(sent) == TOTAL, "every send must return an id"
    assert len(received) == len(set(received)), "no message delivered more than once"
    assert sorted(received) == sorted(sent), "every message delivered exactly once, none dropped"
