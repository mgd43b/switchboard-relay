"""Durable work assignments and their lease history on a shared board."""

from __future__ import annotations

import json
import secrets
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Literal, Optional, get_args

from switchboard_relay.store import Store, _check_owner, _submit_once, _with_lock_retry

TASK_LEASE_SECONDS = 900.0
TaskAction = Literal["renew", "progress", "blocked", "completed", "failed", "release", "cancelled"]
TaskState = Literal[
    "queued", "claimed", "blocked", "completed", "failed", "cancel_requested", "cancelled"
]
TASK_STATES = get_args(TaskState)
_TASK_OUTCOMES = {"completed", "failed"}
_FINISHED_ATTEMPTS = _TASK_OUTCOMES | {"released", "cancelled"}
_ACTIVE_ATTEMPTS = {"claimed", "blocked"}

_TASK_IS_AVAILABLE = "role=? AND available_at IS NOT NULL AND available_at<=?"

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
CREATE INDEX IF NOT EXISTS idx_tasks_submitter ON tasks (submitter, id);
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
CREATE TABLE IF NOT EXISTS task_cancellations (
    task_id      INTEGER PRIMARY KEY,
    requested_at REAL NOT NULL,
    confirmed_at REAL
);
CREATE TRIGGER IF NOT EXISTS protect_cancelled_task_attempt
BEFORE UPDATE ON task_attempts
WHEN EXISTS (SELECT 1 FROM task_cancellations WHERE task_id=NEW.task_id)
  AND NOT (NEW.state='cancelled' AND OLD.state IN ('claimed','blocked')
           AND NEW.lease_expires_at=OLD.lease_expires_at)
BEGIN
    SELECT RAISE(ABORT, 'task cancellation requested');
END;
"""


class TaskQueue:
    def __init__(self, store: Store, *, initialize: bool = True):
        self.store = store
        if not initialize:
            return
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
        idempotency_key: Optional[str] = None,
    ) -> dict:
        with self._write(name, token, clock) as (conn, now):

            def insert() -> int:
                return int(
                    conn.execute(
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
                    ).lastrowid
                )

            task_id, created_at = _submit_once(
                conn,
                operation="task_submit",
                sender=name,
                key=idempotency_key,
                payload=(role, scope, owned_paths, acceptance_criteria, expected_evidence),
                now=now,
                insert=insert,
            )
            return {"id": task_id, "role": role, "status": "queued", "created_at": created_at}

    def has_available(self, role: str, *, now: float) -> bool:
        with self.store._connect() as conn:
            return (
                conn.execute(
                    f"SELECT 1 FROM tasks INDEXED BY idx_tasks_due "
                    f"WHERE {_TASK_IS_AVAILABLE} LIMIT 1",
                    (role, now),
                ).fetchone()
                is not None
            )

    def claim(
        self, name: str, token: str, role: str, *, clock: Callable[[], float]
    ) -> Optional[dict]:
        with self._write(name, token, clock) as (conn, now):
            task = conn.execute(
                f"SELECT * FROM tasks INDEXED BY idx_tasks_available "
                f"WHERE {_TASK_IS_AVAILABLE} ORDER BY id LIMIT 1",
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
            return {**_describe(conn, task_id, now), "receipt": receipt}

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

            state = {"progress": "claimed", "release": "released"}.get(action, action)
            if attempt["state"] in _FINISHED_ATTEMPTS:
                return _describe(conn, task_id, now) if state == attempt["state"] else None

            cancellation = _cancellation(conn, task_id)
            if cancellation is not None:
                if action != "cancelled":
                    return {"id": task_id, "status": "cancel_requested"}
                # A cancellation request keeps this attempt current even if
                # the worker needs to acknowledge it after the lease expires.
                _confirm_cancellation(conn, *key, "cancelled", note, now)
                return _describe(conn, task_id, now)
            if action == "cancelled" or attempt["lease_expires_at"] <= now:
                return None

            if action == "renew":
                deadline = now + TASK_LEASE_SECONDS
                conn.execute(
                    "UPDATE task_attempts SET lease_expires_at=? WHERE task_id=? AND number=?",
                    (deadline, *key),
                )
                conn.execute("UPDATE tasks SET available_at=? WHERE id=?", (deadline, task_id))
                return {"id": task_id, "status": attempt["state"], "lease_expires_at": deadline}

            conn.execute(
                "UPDATE task_attempts SET state=? WHERE task_id=? AND number=?",
                (state, *key),
            )
            if state in _FINISHED_ATTEMPTS:
                conn.execute(
                    "UPDATE tasks SET available_at=? WHERE id=?",
                    (now if state == "released" else None, task_id),
                )
            _record_update(conn, *key, action, note, now)
            return _describe(conn, task_id, now)

    def cancel(
        self,
        name: str,
        token: str,
        *,
        task_id: int,
        note: str,
        clock: Callable[[], float],
        confirm_stopped: bool = False,
    ) -> Optional[dict]:
        if confirm_stopped and not note.strip():
            raise ValueError("Provide a note explaining how you confirmed the worker stopped.")
        with self._write(name, token, clock) as (conn, now):
            task = _submitted_task(conn, task_id, name)
            if task is None:
                return None
            cancellation = _cancellation(conn, task_id)
            if confirm_stopped:
                if cancellation is None:
                    return None
                if cancellation["confirmed_at"] is None:
                    worker_online = conn.execute(
                        "SELECT 1 FROM relay_sessions WHERE token=? AND expires>?",
                        (task["attempt_owner_token"], now),
                    ).fetchone()
                    if worker_online:
                        return None
                    _confirm_cancellation(
                        conn, task_id, task["attempt"], "cancel_confirmed_by_submitter", note, now
                    )
                return _describe(conn, task_id, now)
            if cancellation is not None:
                return _describe(conn, task_id, now)
            if task["available_at"] is None:
                return None

            # Even an expired attempt may still be doing work. Keep that
            # cancellation pending until the last worker confirms it stopped.
            active = task["attempt_state"] in _ACTIVE_ATTEMPTS
            conn.execute("UPDATE tasks SET available_at=NULL WHERE id=?", (task_id,))
            conn.execute(
                "INSERT INTO task_cancellations VALUES (?,?,?)",
                (task_id, now, None if active else now),
            )
            _record_update(
                conn,
                task_id,
                task["attempt"],
                "cancel_requested" if active else "cancelled",
                note,
                now,
            )
            return _describe(conn, task_id, now)

    def retry(
        self,
        name: str,
        token: str,
        *,
        task_id: int,
        attempt: int,
        note: str,
        clock: Callable[[], float],
    ) -> Optional[dict]:
        with self._write(name, token, clock) as (conn, now):
            task = _submitted_task(conn, task_id, name)
            if (
                task is None
                or task["attempt"] != attempt
                or task["attempt_state"] != "failed"
                or _cancellation(conn, task_id) is not None
            ):
                return None
            if task["available_at"] is None:
                conn.execute("UPDATE tasks SET available_at=? WHERE id=?", (now, task_id))
                _record_update(conn, task_id, attempt, "retry", note, now)
            return _describe(conn, task_id, now)

    def status(
        self, task_id: int, *, name: str, role: str, token: str, now: float
    ) -> Optional[dict]:
        with self.store._connect() as conn, conn:
            conn.execute("BEGIN")
            _check_owner(conn, name, token, now)
            row = _overview_rows(conn, now=now, task_id=task_id, viewer=(name, role, token))
            return _describe(conn, task_id, now, overview=row[0]) if row else None

    def list_tasks(
        self,
        *,
        now: float,
        role: Optional[str] = None,
        submitter: Optional[str] = None,
        state: Optional[TaskState] = None,
        after_id: int = 0,
        limit: int = 50,
        viewer: Optional[tuple[str, str, str]] = None,
    ) -> dict:
        """Return a page of summaries. The local CLI can inspect the whole board."""
        if not 0 <= after_id <= 2**63 - 1:
            raise ValueError("after_id must be a non-negative signed 64-bit integer.")
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100.")
        with self.store._connect() as conn, conn:
            conn.execute("BEGIN")
            if viewer is not None:
                _check_owner(conn, viewer[0], viewer[2], now)
            rows = _overview_rows(
                conn,
                now=now,
                role=role,
                submitter=submitter,
                state=state,
                after_id=after_id,
                limit=limit + 1,
                viewer=viewer,
                preview=True,
            )
        return {
            "tasks": [_summarize(row) for row in rows[:limit]],
            "next_after_id": rows[limit - 1]["id"] if len(rows) > limit else None,
        }


def _submitted_task(conn: sqlite3.Connection, task_id: int, name: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        """SELECT task.*, attempt.state AS attempt_state,
                  attempt.owner_token AS attempt_owner_token
           FROM tasks AS task
           LEFT JOIN task_attempts AS attempt
             ON attempt.task_id=task.id AND attempt.number=task.attempt
           WHERE task.id=? AND task.submitter=?""",
        (task_id, name),
    ).fetchone()


def _cancellation(conn: sqlite3.Connection, task_id: int) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT requested_at, confirmed_at FROM task_cancellations WHERE task_id=?", (task_id,)
    ).fetchone()


def _confirm_cancellation(
    conn: sqlite3.Connection, task_id: int, attempt: int, kind: str, note: str, now: float
) -> None:
    conn.execute("UPDATE task_cancellations SET confirmed_at=? WHERE task_id=?", (now, task_id))
    conn.execute(
        "UPDATE task_attempts SET state='cancelled' WHERE task_id=? AND number=?",
        (task_id, attempt),
    )
    _record_update(conn, task_id, attempt, kind, note, now)


def _record_update(
    conn: sqlite3.Connection, task_id: int, attempt: int, kind: str, note: str, now: float
) -> None:
    conn.execute(
        "INSERT INTO task_updates (task_id, attempt, kind, note, at) VALUES (?,?,?,?,?)",
        (task_id, attempt, kind, note, now),
    )


def _overview_rows(
    conn: sqlite3.Connection,
    *,
    now: float,
    task_id: Optional[int] = None,
    role: Optional[str] = None,
    submitter: Optional[str] = None,
    state: Optional[str] = None,
    after_id: int = 0,
    limit: int = 1,
    viewer: Optional[tuple[str, str, str]] = None,
    preview: bool = False,
) -> list[sqlite3.Row]:
    if preview:
        # Inspection must also work before an old board has been opened by an
        # updated server. In that case there simply are no cancellation rows.
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE name IN "
                "('tasks', 'task_attempts', 'task_updates', 'task_cancellations')"
            )
        }
        if not {"tasks", "task_attempts"} <= tables:
            return []
    else:
        tables = {"task_updates", "task_cancellations"}

    available_at = "task.available_at"
    cancellation_join = "LEFT JOIN task_cancellations AS cancel ON cancel.task_id=task.id"
    if "task_cancellations" not in tables:
        if not any(
            row["name"] == "available_at" for row in conn.execute("PRAGMA table_info(tasks)")
        ):
            available_at = "NULL"
        cancellation_join = """LEFT JOIN (
            SELECT NULL AS task_id, NULL AS requested_at, NULL AS confirmed_at
        ) AS cancel ON cancel.task_id=task.id"""

    conditions = ["task.id > :after_id"]
    if task_id is not None:
        conditions.append("task.id = :task_id")
    if role is not None:
        conditions.append("task.role = :role")
    if submitter is not None:
        conditions.append("task.submitter = :submitter")
    if viewer is not None:
        conditions.append(
            """(task.submitter=:viewer_name OR task.role=:viewer_role OR EXISTS (
                SELECT 1 FROM task_attempts
                WHERE task_id=task.id AND owner_token=:viewer_token
            ))"""
        )
    viewer_name, viewer_role, viewer_token = viewer or (None, None, None)
    update_join = """LEFT JOIN task_updates AS last ON last.id=(
        SELECT id FROM task_updates WHERE task_id=page.id ORDER BY id DESC LIMIT 1
    )"""
    if "task_updates" not in tables:
        update_join = """LEFT JOIN (
            SELECT NULL AS kind, NULL AS note, NULL AS at
        ) AS last ON 0"""
    scope = "substr(page.scope, 1, 500)" if preview else "page.scope"
    note = "substr(last.note, 1, 500)" if preview else "last.note"
    extra = "" if preview else ", page.owned_paths, page.acceptance, page.expected_evidence"
    # Both list and detail use the same projection of task and lease state.
    return conn.execute(
        f"""WITH states AS (
            SELECT task.*, attempt.worker, attempt.lease_expires_at,
                cancel.requested_at, cancel.confirmed_at,
                CASE
                    WHEN cancel.confirmed_at IS NOT NULL THEN 'cancelled'
                    WHEN cancel.requested_at IS NOT NULL THEN 'cancel_requested'
                    WHEN attempt.state IS NULL OR attempt.state='released' THEN 'queued'
                    WHEN attempt.state='failed' AND {available_at} IS NOT NULL THEN 'queued'
                    WHEN attempt.state IN ('claimed','blocked')
                         AND attempt.lease_expires_at<=:now THEN 'queued'
                    ELSE attempt.state
                END AS status
            FROM tasks AS task
            LEFT JOIN task_attempts AS attempt
              ON attempt.task_id=task.id AND attempt.number=task.attempt
            {cancellation_join}
            WHERE {" AND ".join(conditions)}
        ), page AS (
            SELECT * FROM states WHERE (:state IS NULL OR status=:state)
            ORDER BY id LIMIT :limit
        )
        SELECT page.id, page.role, page.submitter, page.created_at, page.status, page.attempt,
               page.worker, page.lease_expires_at, page.requested_at, page.confirmed_at,
               {scope} AS display_scope, last.kind AS update_kind,
               {note} AS update_note, last.at AS update_at {extra}
        FROM page
        {update_join}
        ORDER BY page.id""",
        {
            "now": now,
            "role": role,
            "submitter": submitter,
            "task_id": task_id,
            "state": state,
            "after_id": after_id,
            "limit": limit,
            "viewer_name": viewer_name,
            "viewer_role": viewer_role,
            "viewer_token": viewer_token,
        },
    ).fetchall()


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
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tasks_due "
            "ON tasks (role, available_at) WHERE available_at IS NOT NULL"
        )
        conn.execute("DROP INDEX IF EXISTS idx_tasks_role")


def _summarize(row: sqlite3.Row) -> dict:
    update = (
        {"kind": row["update_kind"], "note": row["update_note"], "at": row["update_at"]}
        if row["update_kind"] is not None
        else None
    )
    active = row["status"] in _ACTIVE_ATTEMPTS or row["status"] == "cancel_requested"
    return {
        "id": row["id"],
        "role": row["role"],
        "submitted_by": row["submitter"],
        "scope": row["display_scope"],
        "created_at": row["created_at"],
        "status": row["status"],
        "attempt": row["attempt"],
        "owner": row["worker"] if active else None,
        "lease_expires_at": row["lease_expires_at"] if active else None,
        "last_update": update,
        "blocker": row["update_note"] if row["status"] == "blocked" else None,
        "cancel_requested_at": row["requested_at"],
        "cancelled_at": row["confirmed_at"],
    }


def _describe(
    conn: sqlite3.Connection, task_id: int, now: float, *, overview: Optional[sqlite3.Row] = None
) -> dict:
    task = overview if overview is not None else _overview_rows(conn, now=now, task_id=task_id)[0]
    updates: dict[int, list[dict]] = {}
    for row in conn.execute(
        "SELECT attempt, kind, note, at FROM task_updates WHERE task_id=? ORDER BY id",
        (task_id,),
    ):
        updates.setdefault(row["attempt"], []).append(
            {"kind": row["kind"], "note": row["note"], "at": row["at"]}
        )
    attempts = []
    for row in conn.execute(
        """SELECT number, worker, started_at, lease_expires_at, state
           FROM task_attempts WHERE task_id=? ORDER BY number""",
        (task_id,),
    ):
        status = row["state"]
        if status not in _FINISHED_ATTEMPTS and (
            row["number"] < task["attempt"] or row["lease_expires_at"] <= now
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
    return {
        **_summarize(task),
        "owned_paths": json.loads(task["owned_paths"]),
        "acceptance_criteria": json.loads(task["acceptance"]),
        "expected_evidence": task["expected_evidence"],
        "attempts": attempts,
    }
