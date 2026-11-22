"""Feature tests for the CLI entry point and env-var configuration.

These drive ``main()`` with a fake MCPServer so the blocking ``mcp.run()`` call is
observed rather than actually started, plus the ``$SWITCHBOARD_TTL`` parsing.
"""

from __future__ import annotations

import json
import time

import pytest

import switchboard_relay.server as server
from switchboard_relay.server import DEFAULT_TTL_SECONDS, _resolve_ttl, main
from switchboard_relay.store import Store


class FakeMCP:
    """Records how main() runs it instead of starting a real server."""

    def __init__(self):
        self.ran_transport = "UNSET"

    def run(self, transport="stdio"):
        self.ran_transport = transport


@pytest.fixture()
def fake_build(monkeypatch, tmp_path):
    """Patch build_server to return a FakeMCP and capture (db, ttl)."""
    captured = {}
    fake = FakeMCP()

    def _build(store, ttl=None, board="", msg_ttl=None, max_body=None, **kwargs):
        captured["db_path"] = str(store.db_path)
        captured["ttl"] = ttl
        captured["board"] = board
        captured["msg_ttl"] = msg_ttl
        captured["max_body"] = max_body
        return fake

    monkeypatch.setenv("SWITCHBOARD_DB", str(tmp_path / "cli.db"))
    monkeypatch.setattr(server, "build_server", _build)
    return fake, captured


def test_main_version(capsys):
    rc = main(["--version"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "switchboard-relay" in out
    assert any(ch.isdigit() for ch in out)  # includes a version number


def test_main_defaults_to_stdio(fake_build):
    fake, _ = fake_build
    assert main([]) == 0
    assert fake.ran_transport == "stdio"


def test_main_passes_db_and_ttl(fake_build, tmp_path):
    _, captured = fake_build
    custom = tmp_path / "explicit.db"
    assert main(["--db", str(custom), "--ttl", "42"]) == 0
    assert captured["db_path"] == str(custom)
    assert captured["ttl"] == 42.0


def test_main_board_label_from_env_db(fake_build):
    # With $SWITCHBOARD_DB set (by the fixture) the board label follows the file
    # stem, and the server is built for that board.
    _, captured = fake_build
    assert main([]) == 0
    assert captured["board"] == "cli"  # $SWITCHBOARD_DB is <tmp>/cli.db


def test_main_board_arg_targets_board_file(fake_build, monkeypatch, tmp_path):
    # --board selects ~/.claude/switchboard/<board>.db and wins over $SWITCHBOARD_DB.
    _, captured = fake_build
    monkeypatch.setattr("switchboard_relay.board.boards_dir", lambda home=None: tmp_path / "sb")
    assert main(["--board", "Team X"]) == 0
    assert captured["board"] == "team-x"
    assert captured["db_path"] == str(tmp_path / "sb" / "team-x.db")


# -- boards subcommand ------------------------------------------------------


def test_main_boards_empty(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(server, "boards_dir", lambda home=None: tmp_path / "none")
    monkeypatch.setattr(server, "legacy_db_path", lambda home=None: tmp_path / "switchboard.db")
    assert main(["boards"]) == 0
    assert "No switchboards yet." in capsys.readouterr().out


def test_main_boards_lists_with_live_counts(monkeypatch, tmp_path, capsys):
    bdir = tmp_path / "switchboard"
    bdir.mkdir()
    Store(bdir / "team.db").register("lead", now=time.time())
    Store(bdir / "quiet.db").close()  # a board file that exists but has nobody live
    monkeypatch.setattr(server, "boards_dir", lambda home=None: bdir)
    monkeypatch.setattr(server, "legacy_db_path", lambda home=None: tmp_path / "switchboard.db")
    assert main(["boards"]) == 0
    out = capsys.readouterr().out
    assert "team" in out and "1 live" in out
    assert "quiet" in out and "0 live" in out


def test_main_boards_includes_legacy_db(monkeypatch, tmp_path, capsys):
    legacy = tmp_path / "switchboard.db"
    Store(legacy).close()
    monkeypatch.setattr(server, "boards_dir", lambda home=None: tmp_path / "missing")
    monkeypatch.setattr(server, "legacy_db_path", lambda home=None: legacy)
    assert main(["boards"]) == 0
    assert "switchboard" in capsys.readouterr().out  # legacy DB shown under its stem


# -- doctor subcommand ------------------------------------------------------


def test_main_doctor_reports_resolution_env_and_hints(monkeypatch, tmp_path, capsys):
    db = tmp_path / "d.db"
    s = Store(db)
    now = time.time()
    s.register("lead", "coordinator", now=now)  # a lone participant
    s.send("leed", "typo target", sender="lead", now=now)  # queued to a dead address
    monkeypatch.setenv("SWITCHBOARD_DB", str(db))
    assert main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "resolved by: $SWITCHBOARD_DB" in out
    assert "SWITCHBOARD_DB=" in out
    assert "SWITCHBOARD_BOARD (unset)" in out
    assert "SWITCHBOARD_PROJECT_DIR (unset)" in out
    assert "CLAUDE_PROJECT_DIR (unset)" in out
    assert "lead" in out and "coordinator" in out
    assert "pending (undelivered) messages: 1" in out
    assert "only 1 live participant" in out  # lone-participant hint
    assert "no live reader" in out  # queued-to-dead-address hint


def test_main_doctor_healthy_when_peers_and_no_dead_mail(monkeypatch, tmp_path, capsys):
    db = tmp_path / "h.db"
    s = Store(db)
    now = time.time()
    s.register("lead", now=now)
    s.register("worker:1", "worker", now=now)
    s.send("lead", "queued but to a live reader", sender="worker:1", now=now)
    monkeypatch.setenv("SWITCHBOARD_DB", str(db))
    assert main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "pending (undelivered) messages: 1" in out
    assert "looks healthy" in out


def test_main_doctor_zero_participants_hint(monkeypatch, tmp_path, capsys):
    # An existing board with nobody live must read grammatically ("no live
    # participants", not "only 0 live participant").
    db = tmp_path / "z.db"
    Store(db).send("x", "queued for nobody", sender="ghost", now=time.time())
    monkeypatch.setenv("SWITCHBOARD_DB", str(db))
    assert main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "live participants: 0" in out
    assert "no live participants on this board" in out


def test_main_doctor_no_db_does_not_create_it(monkeypatch, tmp_path, capsys):
    missing = tmp_path / "nope.db"
    monkeypatch.setenv("SWITCHBOARD_DB", str(missing))
    assert main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "does not exist yet" in out
    assert "different board" in out  # steers toward a board mismatch
    assert not missing.exists()  # read-only: must not materialize the db


# -- _resolve_msg_ttl / _resolve_max_body -----------------------------------


def test_resolve_msg_ttl_default_when_unset(monkeypatch):
    monkeypatch.delenv("SWITCHBOARD_MSG_TTL", raising=False)
    assert server._resolve_msg_ttl() == server.DEFAULT_MSG_TTL_SECONDS


def test_resolve_msg_ttl_zero_disables(monkeypatch):
    monkeypatch.setenv("SWITCHBOARD_MSG_TTL", "0")
    assert server._resolve_msg_ttl() == 0.0


def test_resolve_msg_ttl_reads_positive(monkeypatch):
    monkeypatch.setenv("SWITCHBOARD_MSG_TTL", "3600")
    assert server._resolve_msg_ttl() == 3600.0


def test_resolve_msg_ttl_falls_back_on_bad_value(monkeypatch):
    monkeypatch.setenv("SWITCHBOARD_MSG_TTL", "not-a-number")
    assert server._resolve_msg_ttl() == server.DEFAULT_MSG_TTL_SECONDS


def test_resolve_max_body_default_when_unset(monkeypatch):
    monkeypatch.delenv("SWITCHBOARD_MAX_BODY", raising=False)
    assert server._resolve_max_body() == server.DEFAULT_MAX_BODY_BYTES


def test_resolve_max_body_zero_disables(monkeypatch):
    monkeypatch.setenv("SWITCHBOARD_MAX_BODY", "0")
    assert server._resolve_max_body() == 0


def test_resolve_max_body_reads_positive(monkeypatch):
    monkeypatch.setenv("SWITCHBOARD_MAX_BODY", "1024")
    assert server._resolve_max_body() == 1024


def test_resolve_max_body_falls_back_on_bad_value(monkeypatch):
    monkeypatch.setenv("SWITCHBOARD_MAX_BODY", "huge")
    assert server._resolve_max_body() == server.DEFAULT_MAX_BODY_BYTES


def test_resolve_max_body_handles_overflow(monkeypatch):
    # "1e309" -> float('inf') -> int(inf) raises OverflowError; must not crash.
    monkeypatch.setenv("SWITCHBOARD_MAX_BODY", "1e309")
    assert server._resolve_max_body() == server.DEFAULT_MAX_BODY_BYTES


# -- _resolve_ttl -----------------------------------------------------------


def test_resolve_ttl_default_when_unset(monkeypatch):
    monkeypatch.delenv("SWITCHBOARD_TTL", raising=False)
    assert _resolve_ttl() == DEFAULT_TTL_SECONDS


def test_resolve_ttl_reads_env(monkeypatch):
    monkeypatch.setenv("SWITCHBOARD_TTL", "600")
    assert _resolve_ttl() == 600.0


@pytest.mark.parametrize("bad", ["not-a-number", "0", "-5"])
def test_resolve_ttl_falls_back_on_bad_values(monkeypatch, bad):
    monkeypatch.setenv("SWITCHBOARD_TTL", bad)
    assert _resolve_ttl() == DEFAULT_TTL_SECONDS


# -- human inspection commands ----------------------------------------------


def test_main_participants_lists_live(monkeypatch, tmp_path, capsys):
    db = tmp_path / "p.db"
    Store(db).register("lead", "coordinator", now=time.time())
    monkeypatch.setenv("SWITCHBOARD_DB", str(db))
    assert main(["participants"]) == 0
    out = capsys.readouterr().out
    assert "lead" in out
    assert "coordinator" in out


def test_main_participants_empty(monkeypatch, tmp_path, capsys):
    db = tmp_path / "empty.db"
    Store(db).close()  # the DB exists but nobody has registered
    monkeypatch.setenv("SWITCHBOARD_DB", str(db))
    assert main(["participants"]) == 0
    assert "No live participants" in capsys.readouterr().out


def test_main_tail_lists_pending(monkeypatch, tmp_path, capsys):
    db = tmp_path / "t.db"
    s = Store(db)
    qid = s.send("lead", "hello there", sender="worker", now=time.time())
    s.send("worker", "re", sender="lead", reply_to=qid, now=time.time())
    monkeypatch.setenv("SWITCHBOARD_DB", str(db))
    assert main(["tail"]) == 0
    out = capsys.readouterr().out
    assert "hello there" in out
    assert "worker -> lead" in out
    assert "reply_to #1" in out


def test_tail_follow_prints_an_older_redelivery_once(tmp_path, monkeypatch, capsys):
    db = tmp_path / "mail.db"
    store = Store(db)
    old = store.send("lead", "older", sender="worker", now=1)
    store.inbox("lead", now=2, lease=3)
    new = store.send("lead", "newer", sender="worker", now=3)
    times = iter([4, 5, 6])
    monkeypatch.setattr(server, "_now", lambda: next(times))
    monkeypatch.setattr(server.time, "sleep", lambda _: None)

    with pytest.raises(StopIteration):
        server._cli_tail(db, follow=True)

    lines = capsys.readouterr().out.splitlines()
    assert lines == [f"#{new} worker -> lead: newer", f"#{old} worker -> lead: older"]


def test_main_tail_pages_past_the_limit(monkeypatch, tmp_path, capsys):
    # A backlog larger than pending_messages()'s per-query LIMIT (100) must still
    # be printed in full -- tail pages by id rather than windowing on the oldest.
    db = tmp_path / "big.db"
    s = Store(db)
    for i in range(150):
        s.send("lead", f"m{i}", sender="worker", now=time.time())
    monkeypatch.setenv("SWITCHBOARD_DB", str(db))
    assert main(["tail"]) == 0
    out = capsys.readouterr().out
    assert "m0" in out  # oldest
    assert "m149" in out  # newest, beyond the 100-row window
    assert out.count("-> lead:") == 150


def test_main_prune_removes_old_messages_and_stale_participants(monkeypatch, tmp_path, capsys):
    db = tmp_path / "prune.db"
    s = Store(db)
    now = time.time()
    old = now - 8 * 86400  # 8 days ago (past the 7-day default and the 300s TTL)
    s.send("lead", "ancient", sender="w", now=old)
    s.send("lead", "fresh", sender="w", now=now)
    s.register("stale", now=old)
    monkeypatch.setenv("SWITCHBOARD_DB", str(db))

    assert main(["prune"]) == 0
    out = capsys.readouterr().out
    assert "Pruned 1 old message(s) and 1 expired participant(s)." in out
    assert [m.body for m in Store(db).inbox("lead", peek=True, now=time.time())] == ["fresh"]


def test_main_prune_no_database(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("SWITCHBOARD_DB", str(tmp_path / "nope.db"))
    assert main(["prune"]) == 0
    assert "Nothing to prune" in capsys.readouterr().out


def test_inspection_commands_do_not_create_missing_db(monkeypatch, tmp_path, capsys):
    missing = tmp_path / "never" / "switchboard.db"
    monkeypatch.setenv("SWITCHBOARD_DB", str(missing))

    assert main(["participants"]) == 0
    assert "No live participants" in capsys.readouterr().out
    assert main(["tail"]) == 0

    # A read-only peek must not materialize the database or its parent dir.
    assert not missing.exists()
    assert not missing.parent.exists()


def test_cli_tasks_can_read_a_board_before_upgrade(tmp_path, capsys):
    from switchboard_relay.protocol import Protocol
    from switchboard_relay.tasks import TaskQueue

    path = tmp_path / "board.db"
    opts = ["--db", str(path), "tasks"]
    assert main([*opts, "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"tasks": [], "next_after_id": None}
    assert not path.exists()

    store = Store(path)
    assert main(opts) == 0
    assert "No matching tasks." in capsys.readouterr().out
    Protocol(store).claim("owner", "lead", "", now=time.time())
    queue = TaskQueue(store)
    for role in ("worker", "reviewer", "worker"):
        queue.submit(
            "lead",
            "owner",
            role=role,
            scope="Updated docs",
            owned_paths=[],
            acceptance_criteria=[],
            expected_evidence="",
            clock=time.time,
        )
    with store._connect() as conn:
        conn.execute("DROP TRIGGER protect_cancelled_task_attempt")
        conn.execute("DROP TABLE task_cancellations")

    assert main([*opts, "--json", "--role", "worker", "--limit", "1"]) == 0
    page = json.loads(capsys.readouterr().out)
    assert [task["id"] for task in page["tasks"]] == [1]
    assert page["next_after_id"] == 1

    assert main([*opts, "--json", "--role", "worker", "--after-id", "1"]) == 0
    page = json.loads(capsys.readouterr().out)
    assert [task["id"] for task in page["tasks"]] == [3]
    assert page["next_after_id"] is None
    with store._connect() as conn:
        assert not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='task_cancellations'"
        ).fetchone()


def test_cli_tasks_escapes_board_authored_terminal_controls(tmp_path, capsys):
    from switchboard_relay.protocol import Protocol
    from switchboard_relay.tasks import TaskQueue

    path = tmp_path / "board.db"
    store = Store(path)
    protocol = Protocol(store)
    role = "docs" + chr(10) + "team"
    owner = "worker" + chr(27) + "[0m"
    now = time.time()
    protocol.claim("lead", "lead", "", now=now)
    protocol.claim("worker", owner, role, now=now)
    queue = TaskQueue(store)
    scope = "Scope" + chr(27) + "[2J"
    task = queue.submit(
        "lead",
        "lead",
        role=role,
        scope=scope,
        owned_paths=[],
        acceptance_criteria=[],
        expected_evidence="",
        clock=lambda: now,
    )
    claim = queue.claim(owner, "worker", role, clock=lambda: now)
    key = {"task_id": task["id"], "receipt": claim["receipt"]}
    args = ["--db", str(path), "tasks"]

    blocker = "Waiting" + chr(13) + "overwrite"
    queue.update(owner, "worker", **key, action="blocked", note=blocker, clock=lambda: now)
    assert main(args) == 0
    text = capsys.readouterr().out
    assert len(text.splitlines()) == 3
    for escaped in (r"docs\nteam", r"worker\x1b[0m", r"Scope\x1b[2J", r"Waiting\roverwrite"):
        assert escaped in text
    assert chr(27) not in text and chr(13) not in text

    progress = "Progress" + chr(0x9B) + "2J"
    queue.update(owner, "worker", **key, action="progress", note=progress, clock=lambda: now)
    assert main(args) == 0
    assert r"Progress\x9b2J" in capsys.readouterr().out
    assert main([*args, "--json"]) == 0
    [summary] = json.loads(capsys.readouterr().out)["tasks"]
    assert summary["last_update"]["note"] == progress
