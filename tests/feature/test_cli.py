"""Feature tests for the CLI entry point and env-var configuration.

These drive ``main()`` with a fake FastMCP so the blocking ``mcp.run()`` call is
observed rather than actually started, plus the ``$SWITCHBOARD_TTL`` parsing.
"""

from __future__ import annotations

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

    def _build(store, ttl=None, board="", msg_ttl=None, max_body=None):
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


def test_main_tail_pages_past_the_limit(monkeypatch, tmp_path, capsys):
    # A backlog larger than pending_messages()'s per-query LIMIT (100) must still
    # be printed in full -- tail pages by id rather than windowing on the oldest.
    db = tmp_path / "big.db"
    s = Store(db)
    for i in range(150):
        s.send("lead", f"m{i}", sender="worker", now=float(i))
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
    assert [m.body for m in Store(db).inbox("lead", peek=True)] == ["fresh"]


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
