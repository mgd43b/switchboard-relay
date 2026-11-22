"""Durable work assignments and their lease history on a shared board."""

from __future__ import annotations

import json
import secrets
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Literal, Optional

from switchboard_relay.store import Store, _check_owner, _with_lock_retry

TASK_LEASE_SECONDS = 900.0
TaskAction = Literal["renew", "progress", "blocked", "completed", "failed"]
_TERMINAL = {"completed", "failed"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    role              TEXT NOT NULL,
    submitter         TEXT NOT NULL,
    scope             TEXT NOT NULL,
    owned_paths       TEXT NOT NULL,
    acceptance        TEXT NOT NULL,
    expected_evidence TEXT NOT NULL,
    created_at        REAL NOT NULL,
    attempt           INTEGER NOT NULL DEFAULT 0,
    available_at      REAL
);
CREATE TABLE IF NOT EXISTS task_attempts (
    task_id          INTEGER NOT NULL,
    number           INTEGER NOT NULL,
    worker           TEXT NOT NULL,
    owner_token      TEXT NOT NULL,
    receipt          TEXT NOT NULL,
    started_at       REAL NOT NULL,
    lease_expires_at REAL NOT NULL,
    state            TEXT NOT NULL,
    PRIMARY KEY (task_id, number)
);
CREATE TABLE IF NOT EXISTS task_updates (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    attempt INTEGER NOT NULL,
    kind    TEXT NOT NULL,
    note    TEXT NOT NULL,
    at      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_task_updates ON task_updates (task_id, id);
"""


class TaskQueue:
    def __init__(self, store: Store):
        self.store = store
        with store._connect() as conn:
            _with_lock_retry(lambda: conn.executescript(_SCHEMA))
            _with_lock_retry(lambda: _init_availability(conn))

    @contextmanager
    def _write(
        self, name: str, token: str, clock: Callable[[], float]
    ) -> Iterator[tuple[sqlite3.Connection, float]]:
        with self.store._connect() as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            now = clock()
            _check_owner(conn, name, token, now)
            yield conn, now

    def submit(
        self,
        name: str,
        token: str,
        *,
        role: str,
        scope: str,
        owned_paths: list[str],
        acceptance_criteria: list[str],
        expected_evidence: str,
        clock: Callable[[], float],
    ) -> dict:
        with self._write(name, token, clock) as (conn, now):
            result = conn.execute(
                """INSERT INTO tasks
                   (role, submitter, scope, owned_paths, acceptance, expected_evidence,
                    created_at, available_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    role,
                    name,
                    scope,
                    json.dumps(owned_paths),
                    json.dumps(acceptance_criteria),
                    expected_evidence,
                    now,
                    now,
                ),
            )
            return {"id": result.lastrowid, "role": role, "status": "queued", "created_at": now}

    def claim(
        self, name: str, token: str, role: str, *, clock: Callable[[], float]
    ) -> Optional[dict]:
        with self._write(name, token, clock) as (conn, now):
            task = conn.execute(
                """SELECT * FROM tasks
                   WHERE role=? AND available_at IS NOT NULL AND available_at<=?
                   ORDER BY id LIMIT 1""",
                (role, now),
            ).fetchone()
            if task is None:
                return None
            task_id, number = task["id"], task["attempt"] + 1
            receipt = secrets.token_hex(16)
            deadline = now + TASK_LEASE_SECONDS
            conn.execute(
                """INSERT INTO task_attempts
                   (task_id, number, worker, owner_token, receipt,
                    started_at, lease_expires_at, state)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'claimed')""",
                (task_id, number, name, token, receipt, now, deadline),
            )
            conn.execute(
                "UPDATE tasks SET attempt=?, available_at=? WHERE id=?",
                (number, deadline, task_id),
            )
            return {**_describe(conn, task, now), "receipt": receipt}

    def update(
        self,
        name: str,
        token: str,
        *,
        task_id: int,
        receipt: str,
        action: TaskAction,
        note: str,
        clock: Callable[[], float],
    ) -> Optional[dict]:
        with self._write(name, token, clock) as (conn, now):
            task = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if task is None:
                return None
            key = (task_id, task["attempt"])
            attempt = conn.execute(
                """SELECT * FROM task_attempts
                   WHERE task_id=? AND number=? AND owner_token=? AND receipt=?""",
                (*key, token, receipt),
            ).fetchone()
            if attempt is None:
                return None
            if attempt["state"] in _TERMINAL:
                return _describe(conn, task, now) if action == attempt["state"] else None
            if attempt["lease_expires_at"] <= now:
                return None

            if action == "renew":
                deadline = now + TASK_LEASE_SECONDS
                conn.execute(
                    "UPDATE task_attempts SET lease_expires_at=? WHERE task_id=? AND number=?",
                    (deadline, *key),
                )
                conn.execute("UPDATE tasks SET available_at=? WHERE id=?", (deadline, task_id))
                return {"id": task_id, "status": attempt["state"], "lease_expires_at": deadline}

            state = "claimed" if action == "progress" else action
            conn.execute(
                "UPDATE task_attempts SET state=? WHERE task_id=? AND number=?",
                (state, *key),
            )
            if state in _TERMINAL:
                conn.execute("UPDATE tasks SET available_at=NULL WHERE id=?", (task_id,))
            conn.execute(
                "INSERT INTO task_updates (task_id, attempt, kind, note, at) VALUES (?,?,?,?,?)",
                (*key, action, note, now),
            )
            return _describe(conn, task, now)

    def status(
        self, task_id: int, *, name: str, role: str, token: str, now: float
    ) -> Optional[dict]:
        with self.store._connect() as conn, conn:
            conn.execute("BEGIN")
            _check_owner(conn, name, token, now)
            task = conn.execute(
                """SELECT * FROM tasks WHERE id=? AND
                   (submitter=? OR role=? OR EXISTS (
                       SELECT 1 FROM task_attempts WHERE task_id=tasks.id AND owner_token=?
                   ))""",
                (task_id, name, role, token),
            ).fetchone()
            return _describe(conn, task, now) if task is not None else None


def _init_availability(conn: sqlite3.Connection) -> None:
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
        if "available_at" not in columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN available_at REAL")
            conn.execute(
                """UPDATE tasks SET available_at=CASE WHEN attempt=0 THEN created_at
                   ELSE (SELECT lease_expires_at FROM task_attempts
                         WHERE task_id=tasks.id AND number=tasks.attempt
                           AND state IN ('claimed','blocked')) END"""
            )
        # Each row stays indexed while queued or leased. A terminal outcome
        # removes it from the claim index while retaining its history.
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tasks_available "
            "ON tasks (role, id) WHERE available_at IS NOT NULL"
        )
        conn.execute("DROP INDEX IF EXISTS idx_tasks_role")


def _describe(conn: sqlite3.Connection, task: sqlite3.Row, now: float) -> dict:
    updates: dict[int, list[dict]] = {}
    for row in conn.execute(
        "SELECT attempt, kind, note, at FROM task_updates WHERE task_id=? ORDER BY id",
        (task["id"],),
    ):
        updates.setdefault(row["attempt"], []).append(
            {"kind": row["kind"], "note": row["note"], "at": row["at"]}
        )
    attempts = []
    rows = conn.execute(
        """SELECT number, worker, started_at, lease_expires_at, state
           FROM task_attempts WHERE task_id=? ORDER BY number""",
        (task["id"],),
    ).fetchall()
    for row in rows:
        status = row["state"]
        if status not in _TERMINAL and (
            row["number"] < rows[-1]["number"] or row["lease_expires_at"] <= now
        ):
            status = "expired"
        attempts.append(
            {
                "number": row["number"],
                "worker": row["worker"],
                "started_at": row["started_at"],
                "lease_expires_at": row["lease_expires_at"],
                "status": status,
                "updates": updates.get(row["number"], []),
            }
        )
    latest = attempts[-1] if attempts else None
    status = latest["status"] if latest and latest["status"] != "expired" else "queued"
    active = latest if status in {"claimed", "blocked"} else None
    return {
        "id": task["id"],
        "role": task["role"],
        "submitted_by": task["submitter"],
        "scope": task["scope"],
        "owned_paths": json.loads(task["owned_paths"]),
        "acceptance_criteria": json.loads(task["acceptance"]),
        "expected_evidence": task["expected_evidence"],
        "created_at": task["created_at"],
        "status": status,
        "owner": active["worker"] if active else None,
        "lease_expires_at": active["lease_expires_at"] if active else None,
        "attempts": attempts,
    }
