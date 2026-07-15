"""Feature tests for the CLI entry point and env-var configuration.

These drive ``main()`` with a fake FastMCP so the blocking ``mcp.run()`` call is
observed rather than actually started, plus the ``$SWITCHBOARD_TTL`` parsing.
"""

from __future__ import annotations

import time
import types

import pytest

import switchboard.server as server
from switchboard.server import DEFAULT_TTL_SECONDS, _resolve_ttl, main
from switchboard.store import Store


class FakeMCP:
    """Records how main() runs it instead of starting a real server."""

    def __init__(self):
        self.settings = types.SimpleNamespace(host=None, port=None, streamable_http_path="/mcp")
        self.ran_transport = "UNSET"

    def run(self, transport="stdio"):
        self.ran_transport = transport


@pytest.fixture()
def fake_build(monkeypatch, tmp_path):
    """Patch build_server to return a FakeMCP and capture (db, ttl)."""
    captured = {}
    fake = FakeMCP()

    def _build(store, ttl=None):
        captured["db_path"] = str(store.db_path)
        captured["ttl"] = ttl
        return fake

    monkeypatch.setenv("SWITCHBOARD_DB", str(tmp_path / "cli.db"))
    monkeypatch.setattr(server, "build_server", _build)
    return fake, captured


def test_main_version(capsys):
    rc = main(["--version"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "switchboard" in out
    assert any(ch.isdigit() for ch in out)  # includes a version number


def test_main_defaults_to_stdio(fake_build):
    fake, _ = fake_build
    assert main([]) == 0
    assert fake.ran_transport == "stdio"


def test_main_serve_uses_streamable_http(fake_build):
    fake, _ = fake_build
    rc = main(["serve", "--host", "9.9.9.9", "--port", "1234"])
    assert rc == 0
    assert fake.ran_transport == "streamable-http"
    assert fake.settings.host == "9.9.9.9"
    assert fake.settings.port == 1234


def test_main_passes_db_and_ttl(fake_build, tmp_path):
    _, captured = fake_build
    custom = tmp_path / "explicit.db"
    assert main(["--db", str(custom), "--ttl", "42"]) == 0
    assert captured["db_path"] == str(custom)
    assert captured["ttl"] == 42.0


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


def test_inspection_commands_do_not_create_missing_db(monkeypatch, tmp_path, capsys):
    missing = tmp_path / "never" / "switchboard.db"
    monkeypatch.setenv("SWITCHBOARD_DB", str(missing))

    assert main(["participants"]) == 0
    assert "No live participants" in capsys.readouterr().out
    assert main(["tail"]) == 0

    # A read-only peek must not materialize the database or its parent dir.
    assert not missing.exists()
    assert not missing.parent.exists()
