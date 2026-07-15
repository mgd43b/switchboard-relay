"""switchboard: a local MCP server for inter-session messaging in Claude Code."""

from switchboard.store import Message, Participant, Store, default_db_path

__version__ = "0.1.0"

__all__ = ["Message", "Participant", "Store", "__version__", "default_db_path"]
