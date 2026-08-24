"""Packaging guardrails for the Claude Code and Codex plugins."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _json(path: str) -> dict:
    return json.loads((ROOT / path).read_text())


def test_codex_plugin_points_to_local_mcp_config():
    codex = _json(".codex-plugin/plugin.json")

    assert codex["name"] == "switchboard-relay"
    assert codex["mcpServers"] == "./.mcp.json"
    assert "Codex" in codex["description"]
    assert "standby" in codex["interface"]["defaultPrompt"][0]


def test_codex_mcp_config_runs_published_server_with_long_call_timeout():
    servers = _json(".mcp.json")["mcpServers"]
    assert list(servers) == ["switchboard_relay"]
    server = servers["switchboard_relay"]

    assert server["command"] == "uvx"
    assert server["args"] == ["switchboard-relay"]
    assert server["tool_timeout_sec"] > 3600


def test_codex_plugin_stop_hook_targets_bundled_standby_tool():
    groups = _json("hooks/hooks.json")["hooks"]["Stop"]
    handler = groups[0]["hooks"][0]

    assert handler["type"] == "mcp_tool"
    assert handler["server"] == "switchboard_relay"
    assert handler["tool"] == "codex_standby"
    assert handler["input"]["timeout_s"] == 3600
    assert handler["timeout"] > handler["input"]["timeout_s"]


def test_client_plugin_versions_stay_in_sync():
    codex = _json(".codex-plugin/plugin.json")
    claude = _json(".claude-plugin/plugin.json")

    assert codex["version"] == claude["version"]
