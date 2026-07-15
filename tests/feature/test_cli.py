"""Feature tests for the CLI entry point and env-var configuration.

These drive ``main()`` with a fake FastMCP so the blocking ``mcp.run()`` call is
observed rather than actually started, plus the ``$SWITCHBOARD_TTL`` parsing.
"""

from __future__ import annotations

import types

import pytest

import switchboard.server as server
from switchboard.server import DEFAULT_TTL_SECONDS, _resolve_ttl, main


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
