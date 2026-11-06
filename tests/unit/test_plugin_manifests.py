"""Packaging guardrails for the Claude Code and Codex plugins.

Each host resolves plugin hooks differently, so each gets its own hook file:

* Codex reads ``hooks`` from ``.codex-plugin/plugin.json``; an explicit entry
  *replaces* the default ``hooks/hooks.json`` discovery. Its ``mcp_tool``
  handler names the bare server key from ``.mcp.json``.
* Claude Code always loads ``hooks/hooks.json`` from the plugin root and
  *merges* any manifest ``hooks`` entry into it. A plugin-bundled server is
  addressed by its scoped name ``plugin:<plugin>:<server>``, never the bare key.

A hook whose ``server`` does not match the host's name for the bundled server is
a silent no-op (Claude reports a non-blocking error and continues), so these
tests pin every name to the manifest it must agree with.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import anyio

from switchboard_relay.server import build_server
from switchboard_relay.store import Store

ROOT = Path(__file__).resolve().parents[2]

# Claude Code aborts an MCP tool call that sends no response or progress for
# 1800s (CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT). Every standby window must fit under it.
_CLAUDE_MCP_IDLE_ABORT_SECONDS = 1800


def _json(path: str) -> dict:
    return json.loads((ROOT / path).read_text())


def _rel(manifest_path: str) -> Path:
    """Resolve a ``./``-prefixed manifest path, requiring it to stay in the plugin root."""
    assert manifest_path.startswith("./"), manifest_path
    resolved = (ROOT / manifest_path).resolve()
    assert resolved.is_relative_to(ROOT), manifest_path
    assert resolved.is_file(), manifest_path
    return resolved


def _stop_handler(hooks_file: dict) -> dict:
    groups = hooks_file["hooks"]["Stop"]
    assert len(groups) == 1
    handlers = groups[0]["hooks"]
    assert len(handlers) == 1
    return handlers[0]


def _server_tool_names() -> set[str]:
    mcp = build_server(Store(":memory:"), ttl=300, board="manifest")
    return {tool.name for tool in anyio.run(mcp.list_tools)}


def _assert_standby_handler(handler: dict) -> None:
    assert handler["type"] == "mcp_tool"
    assert handler["tool"] == "host_standby"
    window = handler["input"]["timeout_s"]
    # Each window must finish before the host gives up on the call.
    assert 0 < window < _CLAUDE_MCP_IDLE_ABORT_SECONDS
    assert handler["timeout"] > window
    assert handler["statusMessage"]


# -- Codex -------------------------------------------------------------------


def test_codex_plugin_points_to_local_mcp_config():
    codex = _json(".codex-plugin/plugin.json")

    assert codex["name"] == "switchboard-relay"
    assert codex["mcpServers"] == "./.mcp.json"
    assert "Codex" in codex["description"]
    assert "standby" in codex["interface"]["defaultPrompt"][0]


def test_codex_mcp_config_runs_published_server_with_long_call_timeout():
    servers = _json(".mcp.json")["mcpServers"]
    assert list(servers) == ["switchboard_relay_default"]
    server = servers["switchboard_relay_default"]

    assert server["command"] == "uvx"
    assert server["args"] == ["switchboard-relay"]
    # The Codex tool timeout must outlast the Stop hook's own timeout.
    handler = _stop_handler(_json("hooks/codex.json"))
    assert server["tool_timeout_sec"] > handler["timeout"]


def test_codex_marketplace_installs_the_root_plugin_from_github():
    marketplace = _json(".agents/plugins/marketplace.json")
    assert marketplace["name"] == "mgd43b"
    assert marketplace["interface"]["displayName"] == "mgd43b"

    assert len(marketplace["plugins"]) == 1
    plugin = marketplace["plugins"][0]
    assert plugin["name"] == "switchboard-relay"
    assert plugin["source"] == {
        "source": "url",
        "url": "https://github.com/mgd43b/switchboard-relay.git",
        "ref": "main",
    }
    assert plugin["policy"] == {
        "installation": "AVAILABLE",
        "authentication": "ON_INSTALL",
    }
    assert plugin["category"] == "Development"


def test_codex_manifest_uses_its_own_hook_file_not_the_claude_default():
    codex = _json(".codex-plugin/plugin.json")
    # An explicit entry replaces Codex's default hooks/hooks.json discovery, which
    # is the Claude Code hook file and would target a server Codex doesn't have.
    assert codex["hooks"] == "./hooks/codex.json"
    _rel(codex["hooks"])


def test_codex_stop_hook_targets_bundled_server_by_bare_key():
    handler = _stop_handler(_json(_json(".codex-plugin/plugin.json")["hooks"]))
    servers = _json(".mcp.json")["mcpServers"]

    assert handler["server"] in servers
    assert handler["server"] == "switchboard_relay_default"
    _assert_standby_handler(handler)


# -- Claude Code -------------------------------------------------------------


def test_claude_plugin_declares_one_server_and_no_extra_hooks():
    claude = _json(".claude-plugin/plugin.json")
    assert claude["name"] == "switchboard-relay"
    assert list(claude["mcpServers"]) == ["switchboard-relay"]
    # Claude merges a manifest `hooks` entry with hooks/hooks.json; pointing it
    # at the Codex file would add a second, mis-targeted Stop hook.
    assert "hooks" not in claude


def test_claude_stop_hook_targets_bundled_server_by_scoped_name():
    claude = _json(".claude-plugin/plugin.json")
    handler = _stop_handler(_json("hooks/hooks.json"))
    (server_key,) = claude["mcpServers"]

    assert handler["server"] == f"plugin:{claude['name']}:{server_key}"
    _assert_standby_handler(handler)


# -- both hosts --------------------------------------------------------------


def test_hook_files_are_host_specific():
    codex_handler = _stop_handler(_json("hooks/codex.json"))
    claude_handler = _stop_handler(_json("hooks/hooks.json"))

    assert codex_handler["server"] != claude_handler["server"]
    assert codex_handler["input"] == claude_handler["input"]


def test_hook_targets_are_real_server_tools():
    tools = _server_tool_names()
    for path in ("hooks/codex.json", "hooks/hooks.json"):
        assert _stop_handler(_json(path))["tool"] in tools, path
    # Older hook installs still call the Codex-only name.
    assert "codex_standby" in tools


def test_client_plugin_versions_stay_in_sync():
    codex = _json(".codex-plugin/plugin.json")
    claude = _json(".claude-plugin/plugin.json")

    assert codex["version"] == claude["version"]


# -- docs --------------------------------------------------------------------


def _readme_json_blocks(path: str) -> list[dict]:
    text = (ROOT / path).read_text()
    blocks = []
    for body in re.findall(r"```json\n(.*?)```", text, flags=re.S):
        try:
            blocks.append(json.loads(body))
        except json.JSONDecodeError:
            continue
    return blocks


def _documented_hook_servers(path: str) -> set[str]:
    servers = set()
    for block in _readme_json_blocks(path):
        for group in block.get("hooks", {}).get("Stop", []):
            for handler in group.get("hooks", []):
                if handler.get("type") == "mcp_tool":
                    servers.add(handler["server"])
    return servers


def test_readme_direct_install_hook_snippets_match_install_names():
    readme = (ROOT / "README.md").read_text()
    codex_names = set(re.findall(r"codex mcp add (\w+)", readme))
    # Every direct Codex install in the docs uses one name...
    assert codex_names == {"switchboard_relay"}
    # ...and the hook snippets for direct installs target exactly the names
    # those installs create: Codex's switchboard_relay, Claude's switchboard-relay.
    documented = _documented_hook_servers("README.md")
    assert documented == {"switchboard_relay", "switchboard-relay"}


def test_docs_do_not_cap_waits_above_the_claude_idle_abort():
    for path in ("README.md", "examples/README.md"):
        text = (ROOT / path).read_text()
        assert '"timeout_s": 3600' not in text, path
        assert "capped at 3600" not in text, path
