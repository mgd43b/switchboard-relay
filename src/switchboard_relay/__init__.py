"""switchboard-relay: a local MCP server for inter-session messaging in Claude Code."""

from importlib.metadata import PackageNotFoundError, version

from switchboard_relay.store import Message, Participant, Store, default_db_path

try:
    __version__ = version("switchboard-relay")
except PackageNotFoundError:  # pragma: no cover - running from a source tree, not installed
    __version__ = "0+unknown"

__all__ = ["Message", "Participant", "Store", "__version__", "default_db_path"]
