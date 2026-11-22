"""Opt-in supervisor for one named worker on one local board."""

from __future__ import annotations

import errno
import hashlib
import os
import sqlite3
import subprocess
import sys
import time
from typing import Optional

from switchboard_relay.board import Target
from switchboard_relay.protocol import Protocol
from switchboard_relay.store import Store, _is_locked_error

_POLL_SECONDS = 1.0
_RETRY_SECONDS = 30.0


def watch_worker(
    target: Target, name: str, command: list[str], *, ttl: float, max_age: Optional[float]
) -> int:
    import fcntl

    db_path = target.db_path.resolve()
    store = Store(db_path)
    protocol = Protocol(store, min(180.0, ttl), retention=ttl)
    stat = db_path.stat()
    identity = f"{stat.st_dev}:{stat.st_ino}:{name}"
    key = hashlib.sha256(identity.encode()).hexdigest()
    lock_path = db_path.parent / f".switchboard-worker-{key}.lock"
    label = f"{target.board}/{name}"
    child_env = {
        **os.environ,
        "SWITCHBOARD_DB": str(db_path),
        "SWITCHBOARD_BOARD": target.board,
        "SWITCHBOARD_NAME": name,
        "SWITCHBOARD_TTL": str(ttl),
    }

    with lock_path.open("a+b") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                raise
            _report(label, "already watched")
            return 1

        _report(label, "watching")
        child = None
        retry_at = 0.0
        try:
            while True:
                if child is not None and (code := child.poll()) is not None:
                    _report(label, f"worker exited with status {code}")
                    child = None
                    retry_at = time.monotonic() + _RETRY_SECONDS
                if child is None and time.monotonic() >= retry_at:
                    now = time.time()
                    try:
                        ready = store.has_messages(
                            name, now=now, max_age=max_age
                        ) and not protocol.name_is_live(name, now)
                    except sqlite3.OperationalError as exc:
                        if not _is_locked_error(exc):
                            raise
                        ready = False
                    if ready:
                        _report(label, "launching worker")
                        try:
                            # The foreground child shares the lock across exec.
                            # It can finish its turn if the watcher is stopped.
                            child = subprocess.Popen(
                                command,
                                env=child_env,
                                stdin=subprocess.DEVNULL,
                                start_new_session=True,
                                pass_fds=(lock.fileno(),),
                            )
                        except OSError as exc:
                            _report(label, f"launch failed: {exc}")
                            retry_at = time.monotonic() + _RETRY_SECONDS
                time.sleep(_POLL_SECONDS)
        except KeyboardInterrupt:
            _report(label, "startup disabled")
            return 0


def _report(label: str, message: str) -> None:
    print(f"worker-watch {label}: {message}", file=sys.stderr, flush=True)
