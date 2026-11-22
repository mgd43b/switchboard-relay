"""Opt-in supervisor for one named worker on one local board."""

from __future__ import annotations

import errno
import hashlib
import os
import sqlite3
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Optional

from switchboard_relay.board import Target
from switchboard_relay.protocol import Protocol
from switchboard_relay.store import Store, _is_locked_error
from switchboard_relay.tasks import TaskQueue

_POLL_SECONDS = 1.0
_RETRY_SECONDS = 30.0


@contextmanager
def _try_lock(path: Path) -> Iterator[Optional[int]]:
    import fcntl

    with path.open("a+b") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                raise
            yield None
            return
        yield lock.fileno()


def watch_worker(
    target: Target,
    name: str,
    command: list[str],
    *,
    ttl: float,
    max_age: Optional[float],
    task_role: Optional[str] = None,
) -> int:
    db_path = target.db_path.resolve()
    store = Store(db_path)
    protocol = Protocol(store, min(180.0, ttl), retention=ttl)
    queue = TaskQueue(store) if task_role is not None else None
    stat = db_path.stat()
    board_identity = f"{stat.st_dev}:{stat.st_ino}:"
    key = hashlib.sha256((board_identity + name).encode()).hexdigest()
    lock_path = db_path.parent / f".switchboard-worker-{key}.lock"
    label = f"{target.board}/{name}"
    child_env = {
        **os.environ,
        "SWITCHBOARD_DB": str(db_path),
        "SWITCHBOARD_BOARD": target.board,
        "SWITCHBOARD_NAME": name,
        "SWITCHBOARD_TTL": str(ttl),
    }
    role_lock_path = None
    if task_role is not None:
        role_key = hashlib.sha256((board_identity + task_role).encode()).hexdigest()
        role_lock_path = db_path.parent / f".switchboard-worker-role-{role_key}.lock"
        child_env["SWITCHBOARD_ROLE"] = task_role

    def ready() -> bool:
        now = time.time()
        return store.has_messages(name, now=now, max_age=max_age) or (
            queue is not None and task_role is not None and queue.has_available(task_role, now=now)
        )

    with _try_lock(lock_path) as lock_fd:
        if lock_fd is None:
            _report(label, "already watched")
            return 1

        _report(
            label, f"watching mail and tasks for role {task_role!r}" if task_role else "watching"
        )
        child = None
        retry_at = 0.0
        try:
            while True:
                if child is not None and (code := child.poll()) is not None:
                    _report(label, f"worker exited with status {code}")
                    child = None
                    retry_at = time.monotonic() + _RETRY_SECONDS
                if child is None and time.monotonic() >= retry_at:
                    try:
                        if ready():
                            with ExitStack() as locks:
                                guard_fds = (lock_fd,)
                                can_launch = True
                                if role_lock_path is not None:
                                    role_fd = locks.enter_context(_try_lock(role_lock_path))
                                    can_launch = role_fd is not None and ready()
                                    if role_fd is not None:
                                        guard_fds += (role_fd,)
                                if can_launch:
                                    child = _start_worker(
                                        protocol, name, label, command, child_env, guard_fds
                                    )
                    except sqlite3.OperationalError as exc:
                        if not _is_locked_error(exc):
                            raise
                    except OSError as exc:
                        _report(label, f"launch failed: {exc}")
                        retry_at = time.monotonic() + _RETRY_SECONDS
                time.sleep(_POLL_SECONDS)
        except KeyboardInterrupt:
            _report(label, "startup disabled")
            return 0


def _start_worker(
    protocol: Protocol,
    name: str,
    label: str,
    command: list[str],
    env: dict[str, str],
    lock_fds: tuple[int, ...],
) -> Optional[subprocess.Popen]:
    startup_token = protocol.reserve_startup(name, time.time())
    if startup_token is None:
        return None
    _report(label, "launching worker")
    try:
        return subprocess.Popen(
            [
                sys.executable,
                "-m",
                "switchboard_relay.worker_watch",
                ",".join(map(str, lock_fds)),
                label,
                *command,
            ],
            env={**env, "SWITCHBOARD_STARTUP_TOKEN": startup_token},
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            pass_fds=lock_fds,
        )
    except OSError:
        protocol.release_startup(name, startup_token)
        raise


def _report(label: str, message: str) -> None:
    print(f"worker-watch {label}: {message}", file=sys.stderr, flush=True)


def _guard_worker(lock_fds: tuple[int, ...], label: str, command: list[str]) -> int:
    # Keep the name lock and any role slot while the foreground worker runs.
    # Only the guard holds them across a watcher exit.
    with ExitStack() as locks:
        for lock_fd in lock_fds:
            locks.enter_context(os.fdopen(lock_fd, "rb"))
            os.set_inheritable(lock_fd, False)
        store = Store(os.environ["SWITCHBOARD_DB"], init_schema=False)
        protocol = Protocol(store, initialize=False)
        name = os.environ["SWITCHBOARD_NAME"]
        token = os.environ["SWITCHBOARD_STARTUP_TOKEN"]
        try:
            with protocol.startup_launch_window(name, token, time.time) as reserved:
                if not reserved:
                    _report(label, "launch failed: startup reservation expired")
                    return 1
                worker = subprocess.Popen(command, stdin=subprocess.DEVNULL, close_fds=True)
            returncode = worker.wait()
            return returncode if returncode >= 0 else 128 - returncode
        except OSError as exc:
            _report(label, f"launch failed: {exc}")
            return 1
        finally:
            protocol.release_startup(name, token)


if __name__ == "__main__":
    fds = tuple(int(value) for value in sys.argv[1].split(","))
    raise SystemExit(_guard_worker(fds, sys.argv[2], sys.argv[3:]))
