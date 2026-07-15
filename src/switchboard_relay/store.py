"""Durable SQLite-backed store for switchboard.

This module is intentionally free of any MCP / FastMCP / transport concerns so
it can be unit-tested in isolation. All time is passed in explicitly (``now``)
and TTL is a parameter, so the store contains no clocks and no sleeps -- the
server layer owns wall-clock time and identity.

Data model
----------
participants(name PK, role, last_seen, registered_at)
    A participant is an *address*. ``name`` is the primary address
    ("lead", "worker:feature-x"); ``role`` is an optional secondary address
    shared by a group of participants ("worker"). ``last_seen`` drives the TTL
    liveness window surfaced by :meth:`Store.participants`.

messages(id PK, recipient, sender, body, reply_to, created_at)
    A message row is stored with ``recipient`` set to the *literal* ``to``
    address it was sent to. Delivery matching happens at read time: a
    participant drains messages whose ``recipient`` equals its own ``name`` or
    its ``role``. This keeps mailboxes durable -- a message sent to a name that
    is not registered yet simply waits in the table until that name reads it.
"""

from __future__ import annotations

import os
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# Default liveness window: a participant that has not been seen within this many
# seconds drops out of participants(). Overridable via $SWITCHBOARD_TTL and, per
# call, via the ttl argument. Any tool call by an identified participant is a
# heartbeat that refreshes last_seen, so only genuinely idle sessions expire.
DEFAULT_TTL_SECONDS = 300.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS participants (
    name          TEXT PRIMARY KEY,
    role          TEXT NOT NULL DEFAULT '',
    last_seen     REAL NOT NULL,
    registered_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    recipient  TEXT NOT NULL,
    sender     TEXT NOT NULL,
    body       TEXT NOT NULL,
    reply_to   INTEGER,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_recipient ON messages (recipient, id);
"""


def default_db_path() -> Path:
    """Resolve the database path.

    Honors ``$SWITCHBOARD_DB``; otherwise ``~/.claude/switchboard.db``.
    """
    override = os.environ.get("SWITCHBOARD_DB")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".claude" / "switchboard.db"


@dataclass(frozen=True)
class Participant:
    name: str
    role: str
    last_seen: float
    registered_at: float

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "role": self.role,
            "last_seen": self.last_seen,
            "registered_at": self.registered_at,
        }


@dataclass(frozen=True)
class Message:
    id: int
    to: str
    sender: str
    body: str
    reply_to: Optional[int]
    ts: float

    def to_dict(self) -> dict:
        # "from" is a reserved word in Python but a perfectly good JSON key, and
        # it reads naturally to the model consuming these messages.
        return {
            "id": self.id,
            "to": self.to,
            "from": self.sender,
            "body": self.body,
            "reply_to": self.reply_to,
            "ts": self.ts,
        }


class Store:
    """Durable participant registry + message mailboxes over SQLite.

    A ``Store`` is cheap to construct and opens a fresh short-lived connection
    per operation. This keeps it safe to share the same database file across
    many independent processes (every Claude Code session runs its own stdio
    server) -- WAL mode plus a busy timeout let concurrent readers and writers
    coexist without external coordination.
    """

    def __init__(self, db_path: Optional[os.PathLike | str] = None, *, busy_timeout_ms: int = 5000):
        # expanduser() so a "~/..." path (e.g. from $SWITCHBOARD_DB, which MCP
        # passes literally without shell expansion) resolves to the real home
        # directory instead of a literal "~" folder under the process CWD --
        # which would silently give sessions in different CWDs different DBs.
        # ":memory:" has no "~" so this leaves it untouched.
        self.db_path = default_db_path() if db_path is None else Path(db_path).expanduser()
        self._busy_timeout_ms = busy_timeout_ms
        if str(self.db_path) != ":memory:":
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # A single shared in-memory database only survives as long as one
        # connection is held open, so keep one alive for the ":memory:" case
        # (used by tests). File-backed stores keep no persistent handle.
        self._memory_conn: Optional[sqlite3.Connection] = None
        if str(self.db_path) == ":memory:":
            self._memory_conn = self._new_connection()
        self._init_schema()

    # -- connection plumbing ------------------------------------------------

    def _new_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(self.db_path),
            timeout=self._busy_timeout_ms / 1000.0,
            isolation_level=None,  # autocommit; we manage transactions explicitly
        )
        try:
            conn.row_factory = sqlite3.Row
            conn.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms}")
            if str(self.db_path) != ":memory:":
                # journal_mode is a persisted property of the DB file, set once
                # in _init_schema; here we only set the (non-locking, per-conn)
                # synchronous level. Re-issuing journal_mode=WAL on every
                # connection needlessly contends and can hit SQLITE_BUSY under
                # multi-process use.
                conn.execute("PRAGMA synchronous=NORMAL")
        except Exception:  # pragma: no cover - close the half-open handle on setup failure
            conn.close()
            raise
        return conn

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        if self._memory_conn is not None:
            yield self._memory_conn
            return
        conn = self._new_connection()
        try:
            yield conn
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._connect() as conn:
            if str(self.db_path) != ":memory:":
                _enable_wal(conn)
            _with_lock_retry(lambda: conn.executescript(_SCHEMA))

    def close(self) -> None:
        if self._memory_conn is not None:
            self._memory_conn.close()
            self._memory_conn = None

    # -- participants -------------------------------------------------------

    def register(self, name: str, role: str = "", *, now: float) -> Participant:
        """Register a new participant or refresh an existing one's heartbeat.

        Re-registering an existing ``name`` updates its ``role`` (if a non-empty
        role is supplied) and always refreshes ``last_seen``.
        """
        name = _require_name(name)
        role = (role or "").strip()
        with self._connect() as conn:
            # Atomic upsert: create the participant or refresh it in ONE
            # statement. A check-then-insert (SELECT then INSERT) would race --
            # two processes claiming the same name concurrently both see no row
            # and both INSERT, and the second dies on the PRIMARY KEY. The
            # ON CONFLICT clause makes create-or-refresh a single atomic write:
            # it preserves the original registered_at and keeps the prior role
            # when the refresh omits one.
            _with_lock_retry(
                lambda: conn.execute(
                    "INSERT INTO participants (name, role, last_seen, registered_at) "
                    "VALUES (:name, :role, :now, :now) "
                    "ON CONFLICT(name) DO UPDATE SET "
                    "  role = CASE WHEN excluded.role != '' "
                    "              THEN excluded.role ELSE participants.role END, "
                    "  last_seen = excluded.last_seen",
                    {"name": name, "role": role, "now": now},
                )
            )
            row = conn.execute(
                "SELECT role, registered_at FROM participants WHERE name = ?", (name,)
            ).fetchone()
        return Participant(
            name=name, role=row["role"], last_seen=now, registered_at=row["registered_at"]
        )

    def unregister(self, name: str) -> bool:
        """Remove a participant from the registry. Returns True if one existed.

        The participant's mailbox is left untouched: any undelivered messages
        remain and will be read if that name registers again later.
        """
        name = (name or "").strip()
        if not name:
            return False
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM participants WHERE name = ?", (name,))
            return (cur.rowcount or 0) > 0

    def touch(self, name: str, *, now: float) -> None:
        """Refresh a participant's ``last_seen`` if it exists (a heartbeat).

        No-op for an unregistered name. Used by the server on every tool call
        so active sessions stay live without an explicit re-register.
        """
        if not name:
            return
        with self._connect() as conn:
            conn.execute("UPDATE participants SET last_seen = ? WHERE name = ?", (now, name))

    def participants(self, *, now: float, ttl: float = DEFAULT_TTL_SECONDS) -> list[Participant]:
        """Return participants seen within ``ttl`` seconds of ``now``, freshest first."""
        cutoff = now - ttl
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT name, role, last_seen, registered_at FROM participants "
                "WHERE last_seen >= ? ORDER BY last_seen DESC",
                (cutoff,),
            ).fetchall()
        return [
            Participant(
                name=r["name"],
                role=r["role"],
                last_seen=r["last_seen"],
                registered_at=r["registered_at"],
            )
            for r in rows
        ]

    def prune_participants(self, *, now: float, ttl: float = DEFAULT_TTL_SECONDS) -> int:
        """Permanently delete participants past their TTL. Returns count removed.

        Expiry is computed at read time by :meth:`participants`, so pruning is
        purely optional housekeeping; the server calls it opportunistically.
        """
        cutoff = now - ttl
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM participants WHERE last_seen < ?", (cutoff,))
            return cur.rowcount if cur.rowcount is not None else 0

    # -- messages -----------------------------------------------------------

    def send(
        self,
        to: str,
        body: str,
        *,
        sender: str,
        reply_to: Optional[int] = None,
        now: float,
    ) -> int:
        """Append a message to ``to``'s durable inbox. Returns the message id."""
        to = _require_name(to, field="to")
        if not isinstance(body, str):
            raise ValueError("body must be a string")
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO messages (recipient, sender, body, reply_to, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (to, sender, body, reply_to, now),
            )
            return int(cur.lastrowid)

    def inbox(
        self,
        name: str,
        role: str = "",
        *,
        peek: bool = False,
        since: Optional[int] = None,
    ) -> list[Message]:
        """Read messages addressed to ``name`` or ``role``.

        Ordered oldest-first by id. When ``peek`` is False (the default) the
        returned rows are drained (deleted) atomically so each message is
        delivered once. ``since`` restricts to messages with ``id > since``.
        """
        addresses = _addresses(name, role)
        placeholders = ",".join("?" for _ in addresses)
        params: list = list(addresses)
        clause = f"recipient IN ({placeholders})"
        if since is not None:
            clause += " AND id > ?"
            params.append(int(since))

        with self._connect() as conn:
            if peek:
                rows = conn.execute(
                    f"SELECT id, recipient, sender, body, reply_to, created_at "
                    f"FROM messages WHERE {clause} ORDER BY id ASC",
                    params,
                ).fetchall()
                return [_row_to_message(r) for r in rows]

            # Drain atomically: lock the database for the read+delete so two
            # concurrent drainers can never return the same message.
            conn.execute("BEGIN IMMEDIATE")
            try:
                rows = conn.execute(
                    f"SELECT id, recipient, sender, body, reply_to, created_at "
                    f"FROM messages WHERE {clause} ORDER BY id ASC",
                    params,
                ).fetchall()
                if rows:
                    ids = [r["id"] for r in rows]
                    id_placeholders = ",".join("?" for _ in ids)
                    conn.execute(f"DELETE FROM messages WHERE id IN ({id_placeholders})", ids)
                conn.execute("COMMIT")
            except Exception:  # pragma: no cover - defensive: roll back a failed drain
                conn.execute("ROLLBACK")
                raise
        return [_row_to_message(r) for r in rows]

    def take_reply(self, name: str, role: str = "", *, reply_to: int) -> Optional[Message]:
        """Atomically remove and return the oldest reply addressed to this participant.

        A "reply" is a message to ``name``/``role`` whose ``reply_to`` matches the
        given id. Unlike :meth:`inbox`, this drains *only* the matching reply and
        leaves every other message in the mailbox -- it is the primitive behind
        the server's ``ask()`` (send-a-question-then-wait-for-its-answer) tool.
        """
        addresses = _addresses(name, role)
        placeholders = ",".join("?" for _ in addresses)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    f"SELECT id, recipient, sender, body, reply_to, created_at "
                    f"FROM messages WHERE recipient IN ({placeholders}) AND reply_to = ? "
                    f"ORDER BY id ASC LIMIT 1",
                    (*addresses, int(reply_to)),
                ).fetchone()
                if row is not None:
                    conn.execute("DELETE FROM messages WHERE id = ?", (row["id"],))
                conn.execute("COMMIT")
            except Exception:  # pragma: no cover - defensive: roll back a failed take
                conn.execute("ROLLBACK")
                raise
        return _row_to_message(row) if row is not None else None

    def pending_messages(self, *, since: Optional[int] = None, limit: int = 100) -> list[Message]:
        """A page of queued (undelivered) messages, oldest first.

        Returns up to ``limit`` messages with ``id`` greater than ``since`` (or
        from the beginning when ``since`` is None). Callers page through a large
        backlog by passing the last returned id as ``since``. For human
        inspection via the ``switchboard tail`` CLI; not part of the MCP tools.
        """
        clause = ""
        params: list = []
        if since is not None:
            clause = "WHERE id > ?"
            params.append(int(since))
        params.append(int(limit))
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT id, recipient, sender, body, reply_to, created_at "
                f"FROM messages {clause} ORDER BY id ASC LIMIT ?",
                params,
            ).fetchall()
        return [_row_to_message(r) for r in rows]

    def prune_messages(self, *, older_than: float) -> int:
        """Delete messages created before ``older_than`` (epoch seconds).

        Returns the number removed. Delivery only removes a row when a recipient
        drains it, so a message sent to an address that is never read would sit
        forever; this is the housekeeping escape hatch (exposed via the
        ``switchboard prune`` CLI). It never touches messages newer than the
        cutoff, so it will not race ahead of a recipient that is about to read.
        """
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM messages WHERE created_at < ?", (float(older_than),))
            return cur.rowcount if cur.rowcount is not None else 0

    def has_messages(self, name: str, role: str = "", *, since: Optional[int] = None) -> bool:
        """Cheap existence check used by the server's wait() poll loop."""
        addresses = _addresses(name, role)
        placeholders = ",".join("?" for _ in addresses)
        params: list = list(addresses)
        clause = f"recipient IN ({placeholders})"
        if since is not None:
            clause += " AND id > ?"
            params.append(int(since))
        with self._connect() as conn:
            row = conn.execute(f"SELECT 1 FROM messages WHERE {clause} LIMIT 1", params).fetchone()
        return row is not None


# -- helpers ----------------------------------------------------------------


def _is_locked_error(exc: sqlite3.OperationalError) -> bool:
    msg = str(exc).lower()
    return "locked" in msg or "busy" in msg


def _with_lock_retry(fn, *, attempts: int = 200, delay: float = 0.02):
    """Run ``fn`` retrying briefly on transient SQLITE_BUSY/locked errors.

    Multi-process first-init can momentarily contend; the busy_timeout handles
    ordinary writes but not every case (notably journal-mode switches), so we
    add a short application-level retry (~4s worst case) on top.
    """
    for _ in range(attempts - 1):
        try:
            return fn()
        except sqlite3.OperationalError as exc:
            if not _is_locked_error(exc):
                raise
            time.sleep(delay)
    return fn()  # final attempt: let a genuine error propagate


def _enable_wal(conn: sqlite3.Connection) -> None:
    """Switch the database to WAL mode (persisted), tolerating concurrent init.

    WAL lets many independent processes -- one per Claude Code session -- read
    and write the shared database without tripping over each other. Switching
    modes needs a brief exclusive lock that can return SQLITE_BUSY immediately
    (bypassing busy_timeout), so retry until it takes.
    """

    def _switch():
        row = conn.execute("PRAGMA journal_mode=WAL").fetchone()
        mode = str(row[0]).lower() if row else ""
        if mode != "wal":
            # Another initializer holds the lock; raise to trigger a retry.
            raise sqlite3.OperationalError("database is locked (journal_mode switch pending)")

    # If the switch cannot be made (rare), rollback-journal mode still works
    # correctly, just with coarser cross-process locking. Do not fail startup.
    with suppress(sqlite3.OperationalError):
        _with_lock_retry(_switch)
    conn.execute("PRAGMA synchronous=NORMAL")


def _require_name(value: str, field: str = "name") -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _addresses(name: str, role: str) -> tuple[str, ...]:
    name = (name or "").strip()
    role = (role or "").strip()
    if not name:
        raise ValueError("name must be a non-empty string")
    return (name, role) if role else (name,)


def _row_to_message(r: sqlite3.Row) -> Message:
    return Message(
        id=r["id"],
        to=r["recipient"],
        sender=r["sender"],
        body=r["body"],
        reply_to=r["reply_to"],
        ts=r["created_at"],
    )
