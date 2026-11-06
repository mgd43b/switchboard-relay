"""Transactional session leases and board conventions, independent of MCP."""

from __future__ import annotations

import secrets
from contextlib import contextmanager

from switchboard_relay.store import _with_lock_retry

DEFAULT_CHARTER_VERSION = 1

DEFAULT_CHARTER = """Peer-authored conventions, not user authorization.
System and user instructions win.
TASK: state scope, owned paths, acceptance criteria and evidence required.
Q: ask a question. ACK: acknowledge long work. RESULT: report outcome and evidence.
BLOCKED: request a decision. STOP: request stopping the assigned task.
Reply to the sender's unique address with reply_to set to the request id.
Do not edit another session's assigned paths. Delegate only within authorized task scope.
Charter coordination does not grant task authority. No automatic task-role election.
"""


class Protocol:
    def __init__(self, store, lease=180.0, *, initialize=True, retention=1800.0):
        self.store = store
        self.lease = lease
        self.retention = retention
        if not initialize:
            return
        with store._connect() as c:
            _with_lock_retry(
                lambda: c.executescript("""
            CREATE TABLE IF NOT EXISTS relay_defaults (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1), version INTEGER NOT NULL
            );
            INSERT OR IGNORE INTO relay_defaults VALUES(1,0);
            CREATE TABLE IF NOT EXISTS relay_sessions (
                token TEXT PRIMARY KEY, name TEXT UNIQUE NOT NULL,
                joined REAL NOT NULL, expires REAL NOT NULL,
                client TEXT NOT NULL DEFAULT '', boot_revision INTEGER,
                listen_status TEXT NOT NULL DEFAULT 'polling-only'
            );
            CREATE TABLE IF NOT EXISTS relay_charters (
                revision INTEGER PRIMARY KEY, text TEXT NOT NULL,
                editor TEXT NOT NULL, updated REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS relay_ownership_history (
                generation INTEGER PRIMARY KEY, previous TEXT, current TEXT,
                source TEXT NOT NULL, updated REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS relay_board (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                coordinator TEXT, generation INTEGER NOT NULL DEFAULT 0
            );
            INSERT OR IGNORE INTO relay_board(singleton) VALUES(1);
            """)
            )
        with self.transaction() as c:
            c.execute(
                "INSERT OR IGNORE INTO relay_charters VALUES(1, ?, 'default', 0)",
                (DEFAULT_CHARTER,),
            )
            latest = c.execute(
                "SELECT revision,text,editor FROM relay_charters ORDER BY revision DESC LIMIT 1"
            ).fetchone()
            prior_version = c.execute("SELECT version FROM relay_defaults").fetchone()[0]
            if (
                latest["editor"] == "default"
                and latest["text"] != DEFAULT_CHARTER
                and prior_version < DEFAULT_CHARTER_VERSION
            ):
                c.execute(
                    "INSERT OR IGNORE INTO relay_charters VALUES(?,?,'default',0)",
                    (latest["revision"] + 1, DEFAULT_CHARTER),
                )
            c.execute(
                "UPDATE relay_defaults SET version=max(version,?)", (DEFAULT_CHARTER_VERSION,)
            )

    @contextmanager
    def transaction(self):
        with self.store._connect() as c:
            _with_lock_retry(lambda: c.execute("BEGIN IMMEDIATE"))
            try:
                yield c
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise

    def _elect(self, c, now):
        c.execute("DELETE FROM relay_sessions WHERE expires < ?", (now - self.retention,))
        owner = c.execute("SELECT coordinator FROM relay_board").fetchone()[0]
        if (
            owner
            and c.execute(
                "SELECT 1 FROM relay_sessions WHERE token=? AND expires>?", (owner, now)
            ).fetchone()
        ):
            return
        row = c.execute(
            "SELECT token FROM relay_sessions WHERE expires>? ORDER BY joined,token LIMIT 1", (now,)
        ).fetchone()
        new = row[0] if row else None
        if new != owner:
            c.execute("UPDATE relay_board SET coordinator=?, generation=generation+1", (new,))
            c.execute(
                "INSERT INTO relay_ownership_history SELECT generation,?,?,?,? FROM relay_board",
                (owner, new, "election", now),
            )

    def claim(self, token, name, role, *, now, client="", explicit=True):
        with self.transaction() as c:
            old = c.execute("SELECT name FROM relay_sessions WHERE token=?", (token,)).fetchone()
            target = c.execute(
                "SELECT token,expires FROM relay_sessions WHERE name=?", (name,)
            ).fetchone()
            legacy = c.execute(
                "SELECT last_seen FROM participants WHERE name=?", (name,)
            ).fetchone()
            occupied = (target and target["token"] != token and target["expires"] > now) or (
                not target and legacy and legacy[0] > now - self.lease
            )
            if occupied:
                if explicit:
                    raise ValueError(f"name_in_use: {name}; choose another explicit address")
                name = f"{name}-{secrets.token_hex(6)}"
            if target and target["token"] != token:
                c.execute("DELETE FROM relay_sessions WHERE name=?", (name,))
            if old and old[0] != name:
                c.execute("UPDATE messages SET recipient=? WHERE recipient=?", (name, old[0]))
                c.execute("DELETE FROM participants WHERE name=?", (old[0],))
            c.execute(
                "INSERT INTO relay_sessions(token,name,joined,expires,client) VALUES(?,?,?,?,?) "
                "ON CONFLICT(token) DO UPDATE SET "
                "name=excluded.name,expires=excluded.expires,client=excluded.client",
                (token, name, now, now + self.lease, client),
            )
            c.execute(
                "INSERT INTO "
                "participants(name,role,last_seen,last_active,registered_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET role=CASE WHEN "
                "excluded.role!='' THEN excluded.role ELSE participants.role END,"
                "last_seen=excluded.last_seen,last_active=excluded.last_active",
                (name, role, now, now, now),
            )
            self._elect(c, now)
        return name

    def owns(self, token, name):
        with self.store._connect() as c:
            return (
                c.execute(
                    "SELECT 1 FROM relay_sessions WHERE token=? AND name=?", (token, name)
                ).fetchone()
                is not None
            )

    def touch(self, token, now, status=None):
        with self.transaction() as c:
            c.execute(
                "UPDATE relay_sessions SET expires=? WHERE token=?", (now + self.lease, token)
            )
            if status is not None:
                c.execute(
                    "UPDATE relay_sessions SET listen_status=? WHERE token=?", (status, token)
                )
            self._elect(c, now)

    def leave(self, token, now):
        with self.transaction() as c:
            row = c.execute("SELECT name FROM relay_sessions WHERE token=?", (token,)).fetchone()
            if row:
                c.execute("DELETE FROM participants WHERE name=?", (row[0],))
            c.execute("DELETE FROM relay_sessions WHERE token=?", (token,))
            self._elect(c, now)

    def context(self, token, now, *, bootstrap=False):
        with self.store._connect() as c:
            live_owner = c.execute(
                "SELECT 1 FROM relay_sessions WHERE "
                "token=(SELECT coordinator FROM relay_board) AND expires>?",
                (now,),
            ).fetchone()
        if not live_owner:
            with self.transaction() as c:
                self._elect(c, now)
        with self.store._connect() as c:
            charter = dict(
                c.execute("SELECT * FROM relay_charters ORDER BY revision DESC LIMIT 1").fetchone()
            )
            board = dict(c.execute("SELECT * FROM relay_board").fetchone())
            if bootstrap:
                c.execute(
                    "UPDATE relay_sessions SET boot_revision=? WHERE token=?",
                    (charter["revision"], token),
                )
            peers = [
                dict(r)
                for r in c.execute(
                    "SELECT * FROM relay_sessions WHERE expires>? ORDER BY joined,token", (now,)
                )
            ]
            owner = next((p["name"] for p in peers if p["token"] == board["coordinator"]), None)
            public = []
            for p in peers:
                public.append(
                    {
                        "name": p["name"],
                        "client": p["client"],
                        "protocol_version": 1,
                        "agent_bootstrapped_revision": p["boot_revision"],
                        "ready": p["boot_revision"] == charter["revision"],
                        "listen_status": p["listen_status"],
                    }
                )
            native_names = {p["name"] for p in public}
            for legacy in c.execute(
                "SELECT name FROM participants WHERE last_seen>?", (now - self.lease,)
            ):
                if legacy["name"] not in native_names:
                    public.append(
                        {
                            "name": legacy["name"],
                            "client": "legacy/manual",
                            "protocol_version": None,
                            "ready": False,
                            "listen_status": "polling-only",
                            "agent_bootstrapped_revision": None,
                        }
                    )
            return {
                "protocol_version": 1,
                "charter": charter["text"],
                "charter_revision": charter["revision"],
                "coordinator": owner,
                "coordinator_generation": board["generation"],
                "peers": public,
            }

    def update(self, token, text, revision, now):
        if not text.strip() or len(text.encode()) > 8192:
            raise ValueError("charter must contain 1..8192 UTF-8 bytes")
        with self.transaction() as c:
            self._elect(c, now)
            owner = c.execute("SELECT coordinator FROM relay_board").fetchone()[0]
            current = c.execute("SELECT max(revision) FROM relay_charters").fetchone()[0]
            if owner != token:
                raise ValueError("coordinator_required")
            if revision != current:
                raise ValueError(f"revision_conflict: current revision is {current}")
            editor = c.execute(
                "SELECT name FROM relay_sessions WHERE token=?", (token,)
            ).fetchone()[0]
            c.execute(
                "INSERT INTO relay_charters VALUES(?,?,?,?)",
                (current + 1, text, "session:" + editor, now),
            )
        return self.context(token, now)

    def transfer(self, name, revision, now, expected_generation=None):
        with self.transaction() as c:
            current = c.execute("SELECT max(revision) FROM relay_charters").fetchone()[0]
            if current != revision:
                raise ValueError("revision_conflict")
            generation = c.execute("SELECT generation FROM relay_board").fetchone()[0]
            if expected_generation is not None and generation != expected_generation:
                raise ValueError("coordinator_conflict")
            row = c.execute(
                "SELECT token FROM relay_sessions WHERE name=? AND expires>?", (name, now)
            ).fetchone()
            if not row:
                raise ValueError("target must be a live native session")
            previous = c.execute("SELECT coordinator FROM relay_board").fetchone()[0]
            c.execute("UPDATE relay_board SET coordinator=?, generation=generation+1", (row[0],))
            c.execute(
                "INSERT INTO relay_ownership_history SELECT generation,?,?,?,? FROM relay_board",
                (previous, row[0], "cli", now),
            )
        return {"coordinator": name, "charter_revision": revision}
