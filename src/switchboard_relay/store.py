"""Durable SQLite-backed store for switchboard.

This module is intentionally free of any MCP / MCPServer / transport concerns so
it can be unit-tested in isolation. All time is passed in explicitly (``now``)
and TTL is a parameter, so the store contains no clocks and no sleeps -- the
server layer owns wall-clock time and identity.

Data model
----------
participants(name PK, role, last_seen, last_active, registered_at)
    A participant is an *address*. ``name`` is the primary address
    ("lead", "worker:feature-x"); ``role`` is an optional secondary address
    shared by a group of participants ("worker").

    Two clocks, deliberately separate. ``last_seen`` answers "is this session
    still there" and drives the TTL window surfaced by
    :meth:`Store.participants`; it is refreshed by any tool call *and* by the
    server's connection keepalive, because a connected stdio session is proof
    of life even while it is heads-down on work of its own.
    ``last_active`` answers "when did this peer last actually use switchboard"
    and is refreshed only by a real call, never by the keepalive -- it is what
    ``idle_seconds`` reports, and keeping it separate is what stops that number
    from collapsing to zero for every connected session.

messages(id PK, recipient, sender, body, reply_to, created_at)
    A message row is stored with ``recipient`` set to the *literal* ``to``
    address it was sent to. Delivery matching happens at read time: a
    participant can claim mail addressed to its own name or role. The initial
    queue uses the original table so existing boards and senders still work.

message_deliveries(id PK, ..., receipt, lease_expires_at, acknowledged_at, expired_at)
    The first claim moves a message here atomically. An older relay process can
    still read the initial queue but cannot drain a claimed message. Terminal
    rows hold only metadata and are retained for a bounded period.
"""

from __future__ import annotations

import os
import secrets
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# Default liveness window: a participant that has not been seen within this many
# seconds drops out of participants(). Overridable via $SWITCHBOARD_TTL and, per
# call, via the ttl argument. Any tool call by an identified participant is a
# heartbeat that refreshes last_seen, so only genuinely idle sessions expire.
#
# Sized for how long a live session goes heads-down *between* switchboard calls,
# not for how fast a dead one should disappear. A peer running a build, a test
# suite, or a CI wait makes no switchboard call for twenty minutes while being
# entirely alive, and expiring it there is the expensive direction to be wrong:
# broadcast() skips non-live recipients outright, so the message is never queued
# at all, and send() reports a false no_live_recipient. Erring long only leaves a
# stale row -- a session that really did leave lingers for one window and then
# goes, and unregister() drops it immediately on a clean exit.
DEFAULT_TTL_SECONDS = 1800.0
DEFAULT_DELIVERY_LEASE_SECONDS = 300.0
MESSAGE_STATUS_RETENTION_SECONDS = 7 * 86400.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS participants (
    name            TEXT PRIMARY KEY,
    role            TEXT NOT NULL DEFAULT '',
    last_seen       REAL NOT NULL,
    last_active     REAL NOT NULL DEFAULT 0,
    registered_at   REAL NOT NULL,
    ccd_session_id  TEXT NOT NULL DEFAULT ''
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
CREATE INDEX IF NOT EXISTS idx_messages_created ON messages (created_at);

CREATE TABLE IF NOT EXISTS message_deliveries (
    id               INTEGER PRIMARY KEY,
    recipient        TEXT NOT NULL,
    sender           TEXT NOT NULL,
    body             TEXT,
    reply_to         INTEGER,
    created_at       REAL NOT NULL,
    claimant         TEXT,
    owner_token      TEXT,
    receipt          TEXT,
    claimed_at       REAL,
    lease_expires_at REAL,
    acknowledged_at  REAL,
    expired_at       REAL
);
CREATE INDEX IF NOT EXISTS idx_message_deliveries_available
    ON message_deliveries (recipient, id) WHERE body IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_message_deliveries_created
    ON message_deliveries (created_at) WHERE body IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_message_deliveries_acknowledged
    ON message_deliveries (acknowledged_at) WHERE acknowledged_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_message_deliveries_expired
    ON message_deliveries (expired_at) WHERE expired_at IS NOT NULL;
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
    # Liveness clock (keepalive-refreshed) vs idle clock (real calls only).
    # See the module docstring for why these are separate.
    last_seen: float
    last_active: float
    registered_at: float
    # The CCD (Claude Desktop) session id used to inject a turn into this
    # participant via ccd_session_mgmt.send_message. Empty unless the session
    # registered with turn-injection-on-Desktop enabled. Deliberately kept out
    # of to_dict() so raw session ids don't surface in participants() output.
    ccd_session_id: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "role": self.role,
            "last_seen": self.last_seen,
            "last_active": self.last_active,
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
    receipt: Optional[str] = None
    lease_expires_at: Optional[float] = None

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
            **(
                {"receipt": self.receipt, "lease_expires_at": self.lease_expires_at}
                if self.receipt is not None
                else {}
            ),
        }


@dataclass(frozen=True)
class AvailabilityCursor:
    message_id: int = 0
    claim_version: int = 0
    observed_at: float = float("-inf")


class Store:
    """Durable participant registry + message mailboxes over SQLite.

    A ``Store`` is cheap to construct and opens a fresh short-lived connection
    per operation. This keeps it safe to share the same database file across
    many independent processes (every Codex or Claude Code session runs its own
    stdio server) -- WAL mode plus a busy timeout let concurrent readers and
    writers coexist without external coordination.
    """

    def __init__(
        self,
        db_path: Optional[os.PathLike | str] = None,
        *,
        busy_timeout_ms: int = 5000,
        init_schema: bool = True,
    ):
        # expanduser() so a "~/..." path (e.g. from $SWITCHBOARD_DB, which MCP
        # passes literally without shell expansion) resolves to the real home
        # directory instead of a literal "~" folder under the process CWD --
        # which would silently give sessions in different CWDs different DBs.
        # ":memory:" has no "~" so this leaves it untouched.
        self.db_path = default_db_path() if db_path is None else Path(db_path).expanduser()
        self._busy_timeout_ms = busy_timeout_ms
        # init_schema=False opens an already-created database without the
        # schema-establishing writes (WAL-mode switch + CREATE TABLE), so an
        # inspection command touches no db state. Connections are still opened
        # read-write -- a genuine mode=ro open of a WAL database is unreliable
        # (SQLite can't always initialize the -shm), which is why we don't use it.
        if init_schema and str(self.db_path) != ":memory:":
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # A single shared in-memory database only survives as long as one
        # connection is held open, so keep one alive for the ":memory:" case
        # (used by tests). File-backed stores keep no persistent handle.
        self._memory_conn: Optional[sqlite3.Connection] = None
        if str(self.db_path) == ":memory:":
            self._memory_conn = self._new_connection()
        if init_schema:
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
            _with_lock_retry(lambda: _migrate_participants(conn))
            _with_lock_retry(lambda: _migrate_claim_versions(conn))

    def close(self) -> None:
        if self._memory_conn is not None:
            self._memory_conn.close()
            self._memory_conn = None

    # -- participants -------------------------------------------------------

    def register(
        self, name: str, role: str = "", *, now: float, ccd_session_id: str = ""
    ) -> Participant:
        """Register a new participant or refresh an existing one's heartbeat.

        Re-registering an existing ``name`` updates its ``role`` (if a non-empty
        role is supplied), refreshes ``ccd_session_id`` (if a non-empty one is
        supplied), and always refreshes ``last_seen``.
        """
        name = _require_name(name)
        role = (role or "").strip()
        ccd = (ccd_session_id or "").strip()
        with self._connect() as conn:
            # Atomic upsert: create the participant or refresh it in ONE
            # statement. A check-then-insert (SELECT then INSERT) would race --
            # two processes claiming the same name concurrently both see no row
            # and both INSERT, and the second dies on the PRIMARY KEY. The
            # ON CONFLICT clause makes create-or-refresh a single atomic write:
            # it preserves the original registered_at and keeps the prior role /
            # ccd_session_id when the refresh omits one.
            _with_lock_retry(
                lambda: conn.execute(
                    "INSERT INTO participants (name, role, last_seen, last_active, "
                    "  registered_at, ccd_session_id) "
                    "VALUES (:name, :role, :now, :now, :now, :ccd) "
                    "ON CONFLICT(name) DO UPDATE SET "
                    "  role = CASE WHEN excluded.role != '' "
                    "              THEN excluded.role ELSE participants.role END, "
                    "  ccd_session_id = CASE WHEN excluded.ccd_session_id != '' "
                    "              THEN excluded.ccd_session_id "
                    "              ELSE participants.ccd_session_id END, "
                    "  last_seen = excluded.last_seen, "
                    "  last_active = excluded.last_active",
                    {"name": name, "role": role, "now": now, "ccd": ccd},
                )
            )
            row = conn.execute(
                "SELECT role, registered_at, ccd_session_id FROM participants WHERE name = ?",
                (name,),
            ).fetchone()
        return Participant(
            name=name,
            role=row["role"],
            last_seen=now,
            last_active=now,
            registered_at=row["registered_at"],
            ccd_session_id=row["ccd_session_id"],
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
        """Refresh both of a participant's clocks if it exists (a heartbeat).

        A tool call is a deliberate act by the participant, so it bumps
        ``last_active`` (the idle clock) as well as ``last_seen`` (liveness).
        No-op for an unregistered name. Used by the server on every tool call
        so active sessions stay live without an explicit re-register.
        """
        if not name:
            return
        with self._connect() as conn:
            conn.execute(
                "UPDATE participants SET last_seen = ?, last_active = ? WHERE name = ?",
                (now, now, name),
            )

    def keepalive(self, name: str, *, now: float) -> None:
        """Refresh only ``last_seen`` -- the liveness clock -- for ``name``.

        The connection-level counterpart to :meth:`touch`, called by the
        server's watch loop for every *connected* session. Each session owns its
        own switchboard process, so the process existing at all is proof that
        the session does: a peer that is building, running a suite, or waiting
        on CI makes no switchboard call for many minutes and must not be
        declared dead, because broadcast() skips non-live recipients outright.

        Deliberately leaves ``last_active`` alone. That is what keeps
        ``idle_seconds`` meaning "how long since this peer last used
        switchboard" instead of collapsing to zero for everyone connected.
        """
        if not name:
            return
        with self._connect() as conn:
            conn.execute("UPDATE participants SET last_seen = ? WHERE name = ?", (now, name))

    def participants(self, *, now: float, ttl: float = DEFAULT_TTL_SECONDS) -> list[Participant]:
        """Return participants live within ``ttl`` seconds of ``now``, freshest first.

        Liveness filters on ``last_seen``; the ordering is by ``last_active``,
        because the keepalive lifts every connected session's ``last_seen`` to
        roughly the same instant and sorting on it would be close to arbitrary.
        "Freshest" is most-recently-*active* first, which is what a caller
        scanning the list for someone to talk to actually wants.
        """
        cutoff = now - ttl
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT name, role, last_seen, last_active, registered_at, ccd_session_id "
                "FROM participants WHERE last_seen >= ? ORDER BY last_active DESC",
                (cutoff,),
            ).fetchall()
        return [
            Participant(
                name=r["name"],
                role=r["role"],
                last_seen=r["last_seen"],
                last_active=r["last_active"],
                registered_at=r["registered_at"],
                ccd_session_id=r["ccd_session_id"],
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
        now: float,
        max_age: Optional[float] = None,
        peek: bool = False,
        since: Optional[int] = None,
        owner_token: Optional[str] = None,
        lease: float = DEFAULT_DELIVERY_LEASE_SECONDS,
    ) -> list[Message]:
        """Read available mail oldest first. Peeking never claims a message."""
        where, params = _mailbox_filter(name, role, since)
        if peek:
            with self._connect() as conn:
                _check_owner(conn, name, owner_token, now)
                rows = _select_available(conn, now, max_age, where, params)
            return [_row_to_message(row) for row in rows]
        return self._claim(name, owner_token, now, max_age, lease, where, params)

    def take_reply(
        self,
        name: str,
        role: str = "",
        *,
        reply_to: int,
        now: float,
        max_age: Optional[float] = None,
        owner_token: Optional[str] = None,
        lease: float = DEFAULT_DELIVERY_LEASE_SECONDS,
    ) -> Optional[Message]:
        """Claim only the oldest available reply to the specified question."""
        messages = self.claim_replies(
            name,
            role,
            reply_to=reply_to,
            now=now,
            max_age=max_age,
            owner_token=owner_token,
            lease=lease,
            limit=1,
        )
        return messages[0] if messages else None

    def claim_replies(
        self,
        name: str,
        role: str = "",
        *,
        reply_to: int,
        now: float,
        max_age: Optional[float] = None,
        owner_token: Optional[str] = None,
        lease: float = DEFAULT_DELIVERY_LEASE_SECONDS,
        limit: int = -1,
    ) -> list[Message]:
        """Claim available matching replies, oldest first."""
        where, params = _mailbox_filter(name, role)
        return self._claim(
            name,
            owner_token,
            now,
            max_age,
            lease,
            where + " AND reply_to = ?",
            [*params, reply_to],
            limit=limit,
        )

    def _claim(
        self,
        name: str,
        owner_token: Optional[str],
        now: float,
        max_age: Optional[float],
        lease: float,
        where: str,
        params: list,
        *,
        limit: int = -1,
    ) -> list[Message]:
        with self._connect() as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            _check_owner(conn, name, owner_token, now)
            rows = _select_available(conn, now, max_age, where, params, limit=limit)
            if not rows:
                return []
            conn.execute("UPDATE relay_claim_version SET version=version+1")
            version = conn.execute("SELECT version FROM relay_claim_version").fetchone()[0]
            messages = []
            for row in rows:
                receipt = secrets.token_hex(16)
                lease_expires_at = now + lease
                if max_age is not None:
                    lease_expires_at = min(lease_expires_at, row["created_at"] + max_age)
                conn.execute(
                    """INSERT INTO message_deliveries (
                           id, recipient, sender, body, reply_to, created_at,
                           claimant, owner_token, receipt, claimed_at, lease_expires_at,
                           claim_version
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(id) DO UPDATE SET claimant=excluded.claimant,
                           owner_token=excluded.owner_token, receipt=excluded.receipt,
                           claimed_at=excluded.claimed_at,
                           lease_expires_at=excluded.lease_expires_at,
                           claim_version=excluded.claim_version
                    """,
                    (
                        row["id"],
                        row["recipient"],
                        row["sender"],
                        row["body"],
                        row["reply_to"],
                        row["created_at"],
                        name,
                        owner_token,
                        receipt,
                        now,
                        lease_expires_at,
                        version,
                    ),
                )
                if row["lease_expires_at"] is None:
                    conn.execute("DELETE FROM messages WHERE id = ?", (row["id"],))
                messages.append(
                    _row_to_message(row, receipt=receipt, lease_expires_at=lease_expires_at)
                )
        return messages

    def ack(
        self,
        id: int,
        receipt: str,
        *,
        name: str,
        owner_token: Optional[str] = None,
        clock: Callable[[], float] = time.time,
        max_age: Optional[float] = None,
    ) -> Optional[float]:
        """Confirm the current claim or return the timestamp of a repeated ack."""
        with self._connect() as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            now = clock()
            _check_owner(conn, name, owner_token, now)
            row = conn.execute(
                """SELECT acknowledged_at, expired_at, lease_expires_at, created_at
                   FROM message_deliveries
                   WHERE id=? AND receipt=? AND claimant=? AND owner_token IS ?""",
                (id, receipt, name, owner_token),
            ).fetchone()
            if row is None:
                return None
            acknowledged_at = row["acknowledged_at"]
            if acknowledged_at is not None:
                return (
                    acknowledged_at
                    if acknowledged_at > now - MESSAGE_STATUS_RETENTION_SECONDS
                    else None
                )
            if (
                row["expired_at"] is not None
                or row["lease_expires_at"] <= now
                or row["created_at"] <= _age_cutoff(now, max_age)
            ):
                return None
            conn.execute(
                "UPDATE message_deliveries SET body=NULL, acknowledged_at=? WHERE id=?",
                (now, id),
            )
        return now

    def status(
        self, id: int, *, sender: str, now: float, max_age: Optional[float] = None
    ) -> Optional[dict]:
        """Return the delivery state visible to the original sending address."""
        with self._connect() as conn:
            row = conn.execute(
                """SELECT recipient, created_at, NULL AS claimed_at, NULL AS lease_expires_at,
                          NULL AS acknowledged_at, NULL AS expired_at
                   FROM messages WHERE id=? AND sender=?
                   UNION ALL
                   SELECT recipient, created_at, claimed_at, lease_expires_at,
                          acknowledged_at, expired_at
                   FROM message_deliveries WHERE id=? AND sender=?""",
                (id, sender, id, sender),
            ).fetchone()
        if row is None:
            return None

        status = {"id": id, "to": row["recipient"], "created_at": row["created_at"]}
        acknowledged_at = row["acknowledged_at"]
        if acknowledged_at is not None:
            if acknowledged_at <= now - MESSAGE_STATUS_RETENTION_SECONDS:
                return None
            return {**status, "status": "acknowledged", "acknowledged_at": acknowledged_at}
        expired_at = row["expired_at"]
        if (
            expired_at is None
            and max_age is not None
            and row["created_at"] <= _age_cutoff(now, max_age)
        ):
            expired_at = row["created_at"] + max_age
        if expired_at is not None:
            if expired_at <= now - MESSAGE_STATUS_RETENTION_SECONDS:
                return None
            return {**status, "status": "expired", "expired_at": expired_at}

        lease_expires_at = row["lease_expires_at"]
        if lease_expires_at is not None and lease_expires_at > now:
            return {
                **status,
                "status": "leased",
                "claimed_at": row["claimed_at"],
                "lease_expires_at": lease_expires_at,
            }
        return {**status, "status": "queued"}

    def pending_messages(
        self,
        *,
        now: float,
        max_age: Optional[float] = None,
        since: Optional[int] = None,
        limit: int = 100,
    ) -> list[Message]:
        """Page through currently available mail, including expired claims."""
        with self._connect() as conn:
            # Read-only CLI commands can inspect a board before its first upgrade.
            has_deliveries = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name='message_deliveries'"
            ).fetchone()
            rows = _select_available(
                conn,
                now,
                max_age,
                "id > ?",
                [since if since is not None else 0],
                limit=limit,
                include_deliveries=has_deliveries is not None,
            )
        return [_row_to_message(row) for row in rows]

    def prune_messages(self, *, now: float, max_age: Optional[float]) -> int:
        """Age out message bodies and purge terminal metadata past its retention."""
        cutoff = _age_cutoff(now, max_age)
        retention_cutoff = now - MESSAGE_STATUS_RETENTION_SECONDS
        with self._connect() as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            expired = 0
            if max_age is not None:
                conn.execute(
                    """INSERT INTO message_deliveries
                           (id, recipient, sender, created_at, expired_at)
                       SELECT id, recipient, sender, created_at, created_at + ?
                       FROM messages WHERE created_at <= ?""",
                    (max_age, cutoff),
                )
                expired = conn.execute(
                    "DELETE FROM messages WHERE created_at <= ?", (cutoff,)
                ).rowcount
                expired += conn.execute(
                    """UPDATE message_deliveries
                       SET body=NULL, receipt=NULL, owner_token=NULL, expired_at=created_at + ?
                       WHERE body IS NOT NULL AND created_at <= ?""",
                    (max_age, cutoff),
                ).rowcount
            conn.execute(
                "DELETE FROM message_deliveries WHERE acknowledged_at <= ? OR expired_at <= ?",
                (retention_cutoff, retention_cutoff),
            )
        return expired

    def has_messages(
        self,
        name: str,
        role: str = "",
        *,
        now: float,
        max_age: Optional[float] = None,
        since: Optional[int] = None,
    ) -> bool:
        """Check availability without fetching payloads or claiming mail."""
        where, params = _mailbox_filter(name, role, since)
        with self._connect() as conn:
            return bool(_select_available(conn, now, max_age, where, params, limit=1, fields="id"))

    def newly_available(
        self,
        cursor: AvailabilityCursor,
        *,
        now: float,
        max_age: Optional[float],
        name: Optional[str] = None,
        role: str = "",
        include_body: bool = False,
    ) -> tuple[list[sqlite3.Row], AvailabilityCursor]:
        """Read new mail and newly available retries from a single SQLite snapshot."""
        where, params = _mailbox_filter(name, role) if name is not None else ("1", [])
        fields = "id, recipient, sender, reply_to" + (", body" if include_body else "")
        cutoff = _age_cutoff(now, max_age)
        with self._connect() as conn, conn:
            conn.execute("BEGIN")
            # The CLI can follow a board that has not been upgraded yet.
            has_version = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name='relay_claim_version'"
            ).fetchone()
            version = 0
            if has_version:
                version = conn.execute("SELECT version FROM relay_claim_version").fetchone()[0]
            sql = f"""SELECT {fields}, 0 AS retry FROM messages
                      WHERE {where} AND id > ? AND created_at > ?"""
            args = [*params, cursor.message_id, cutoff]
            if has_version:
                # The deadline range picks up claims already seen in earlier snapshots.
                # The version range also catches claims that committed after the last
                # snapshot, even if their deadline had already passed by then.
                retry_filter = f"WHERE {where} AND created_at > ? AND body IS NOT NULL"
                sql += f""" UNION ALL
                            SELECT {fields}, 1 AS retry FROM message_deliveries
                            INDEXED BY idx_message_deliveries_deadline
                            {retry_filter} AND lease_expires_at > ? AND lease_expires_at <= ?
                            UNION ALL
                            SELECT {fields}, 1 AS retry FROM message_deliveries
                            INDEXED BY idx_message_deliveries_version
                            {retry_filter} AND claim_version > ? AND lease_expires_at <= ?"""
                args.extend(
                    [
                        *params,
                        cutoff,
                        cursor.observed_at,
                        now,
                        *params,
                        cutoff,
                        cursor.claim_version,
                        min(cursor.observed_at, now),
                    ]
                )
            rows = conn.execute(sql + " ORDER BY id", args).fetchall()
        message_id = max((row["id"] for row in rows if not row["retry"]), default=cursor.message_id)
        return rows, AvailabilityCursor(message_id, version, now)


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


def _migrate_participants(conn: sqlite3.Connection) -> None:
    """Add columns introduced after the original schema to an existing DB.

    ``CREATE TABLE IF NOT EXISTS`` is a no-op on a database created by an older
    version, so a new column has to be added explicitly: ``ccd_session_id``
    (turn injection on Desktop) and ``last_active`` (the idle clock, split out
    from ``last_seen`` once the keepalive started refreshing the latter).
    Idempotent: checks the column list first, and a fresh DB already has both
    from ``_SCHEMA``.
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(participants)").fetchall()}
    if "ccd_session_id" not in cols:
        conn.execute("ALTER TABLE participants ADD COLUMN ccd_session_id TEXT NOT NULL DEFAULT ''")
    if "last_active" not in cols:
        # SQLite requires a constant default on ADD COLUMN, so seed from
        # last_seen afterwards: before this column existed the two clocks were
        # the same thing, and leaving legacy rows at 0 would report every one of
        # them as idle since the epoch.
        conn.execute("ALTER TABLE participants ADD COLUMN last_active REAL NOT NULL DEFAULT 0")
        conn.execute("UPDATE participants SET last_active = last_seen")


def _migrate_claim_versions(conn: sqlite3.Connection) -> None:
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(message_deliveries)")}
        if "claim_version" not in columns:
            conn.execute(
                "ALTER TABLE message_deliveries ADD COLUMN claim_version INTEGER NOT NULL DEFAULT 0"
            )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS relay_claim_version "
            "(singleton INTEGER PRIMARY KEY CHECK(singleton=1), version INTEGER NOT NULL)"
        )
        conn.execute("INSERT OR IGNORE INTO relay_claim_version VALUES (1, 0)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_message_deliveries_deadline "
            "ON message_deliveries (lease_expires_at) WHERE body IS NOT NULL"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_message_deliveries_version "
            "ON message_deliveries (claim_version) WHERE body IS NOT NULL"
        )


def _enable_wal(conn: sqlite3.Connection) -> None:
    """Switch the database to WAL mode (persisted), tolerating concurrent init.

    WAL lets many independent processes -- one per agent session -- read
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


def _mailbox_filter(name: str, role: str, since: Optional[int] = None) -> tuple[str, list]:
    addresses = _addresses(name, role)
    placeholders = ",".join("?" for _ in addresses)
    clause = f"recipient IN ({placeholders})"
    if since is not None:
        return clause + " AND id > ?", [*addresses, since]
    return clause, list(addresses)


def _age_cutoff(now: float, max_age: Optional[float]) -> float:
    return now - max_age if max_age is not None else float("-inf")


def _check_owner(
    conn: sqlite3.Connection, name: str, owner_token: Optional[str], now: float
) -> None:
    if (
        owner_token
        and not conn.execute(
            "SELECT 1 FROM relay_sessions WHERE token=? AND name=? AND expires>?",
            (owner_token, name, now),
        ).fetchone()
    ):
        raise ValueError("session ownership lost")


def _select_available(
    conn: sqlite3.Connection,
    now: float,
    max_age: Optional[float],
    where: str,
    params: list,
    *,
    limit: int = -1,
    fields: str = "*",
    include_deliveries: bool = True,
) -> list[sqlite3.Row]:
    cutoff = _age_cutoff(now, max_age)
    columns = "id, recipient, sender, body, reply_to, created_at"
    sql = f"""SELECT {columns}, NULL AS lease_expires_at
              FROM messages WHERE {where} AND created_at > ?"""
    args = [*params, cutoff]
    if include_deliveries:
        sql += f""" UNION ALL SELECT {columns}, lease_expires_at
                    FROM message_deliveries
                    WHERE {where} AND created_at > ?
                      AND body IS NOT NULL AND lease_expires_at <= ?"""
        args.extend([*params, cutoff, now])
    return conn.execute(
        f"SELECT {fields} FROM ({sql}) ORDER BY id LIMIT ?", (*args, limit)
    ).fetchall()


def _row_to_message(
    r: sqlite3.Row, *, receipt: Optional[str] = None, lease_expires_at: Optional[float] = None
) -> Message:
    return Message(
        id=r["id"],
        to=r["recipient"],
        sender=r["sender"],
        body=r["body"],
        reply_to=r["reply_to"],
        ts=r["created_at"],
        receipt=receipt,
        lease_expires_at=lease_expires_at,
    )
