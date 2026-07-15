"""Board (switchboard) resolution: which shared bus a session joins.

A *board* is one isolated switchboard -- its own participant registry and set of
mailboxes. Every board is a separate SQLite file under ``~/.claude/switchboard/``,
so boards never see each other's traffic and no schema-level partitioning is
needed. This module is the pure, transport-free logic that decides *which* board
a session belongs to and where that board's database lives.

Resolution order (highest priority first)
------------------------------------------
1. An explicit database path (``--db`` or ``$SWITCHBOARD_DB``) -- a raw file
   override that bypasses boards entirely. Its board label is the file stem.
2. An explicit board name (``--board`` or ``$SWITCHBOARD_BOARD``) -- used
   verbatim (sanitized) as ``~/.claude/switchboard/<board>.db``. The sentinel
   value ``project`` forces the project derivation below.
3. **Project-derived (the default).** The board is keyed off the *main* git
   repository so that all of a repo's worktrees and subdirectories share one
   board, while different repos on the same machine stay isolated. The key is
   the parent of ``git rev-parse --git-common-dir`` (the shared ``.git`` that
   every worktree of a repo points back to -- unlike ``--show-toplevel``, which
   differs per worktree). When the project is not a git repository the launch
   directory itself is the key. The human-readable board name is
   ``<basename>-<short-hash-of-the-key>``: readable, and collision-safe when two
   different checkouts share a basename.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from pathlib import Path
from typing import Optional

# Board names become filenames, so restrict to a filesystem-safe alphabet and a
# sane length. Anything else collapses to a dash; an empty result becomes
# "default" so we always have a usable name.
_SAFE = re.compile(r"[^a-z0-9._-]+")
_MAX_BOARD_LEN = 64

# Sentinel value of $SWITCHBOARD_BOARD that forces project derivation even if we
# ever change the default away from it.
_PROJECT_SENTINEL = "project"

# Bound the git probe so a slow or hung git can never stall server startup.
_GIT_TIMEOUT_SECONDS = 2.0


def sanitize_board(name: str) -> str:
    """Coerce an arbitrary board name into a filesystem-safe slug.

    Lowercased (board files are matched case-insensitively so ``Team`` and
    ``team`` are one board), non-alphanumeric runs become a single dash, and the
    result is length-capped. An empty result falls back to ``"default"``.
    """
    slug = _SAFE.sub("-", (name or "").strip().lower()).strip("-._")
    slug = slug[:_MAX_BOARD_LEN].strip("-._")
    return slug or "default"


def boards_dir(home: Optional[Path] = None) -> Path:
    """Directory holding one ``<board>.db`` file per board."""
    return (Path.home() if home is None else home) / ".claude" / "switchboard"


def legacy_db_path(home: Optional[Path] = None) -> Path:
    """The pre-board single shared database (``~/.claude/switchboard.db``).

    Kept only so inspection tooling can still surface a database left behind by
    an older version; nothing writes here by default anymore.
    """
    return (Path.home() if home is None else home) / ".claude" / "switchboard.db"


def board_db_path(board: str, *, home: Optional[Path] = None) -> Path:
    """Map a board name to its SQLite file: ``~/.claude/switchboard/<board>.db``."""
    return boards_dir(home) / f"{sanitize_board(board)}.db"


def _git_common_dir(base: str) -> Optional[str]:
    """Absolute path to ``base``'s *shared* git dir, or None if not a git repo.

    ``--git-common-dir`` is the key: for a worktree it resolves to the main
    repository's ``.git`` (identical across every worktree of that repo), so all
    worktrees collapse to one board. ``git -C`` also searches upward, so a call
    from any subdirectory of the repo resolves the same way. Any failure (not a
    repo, git absent, timeout) yields None and the caller falls back to the raw
    directory.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", base, "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):  # git missing / timed out / spawn failure
        return None
    if proc.returncode != 0:
        return None
    out = proc.stdout.strip()
    return out or None


def _project_key(base: str) -> str:
    """A stable identity for the project rooted at ``base``.

    The parent of the shared ``.git`` (i.e. the main repository root) when ``base``
    is inside a git repo -- shared across worktrees -- otherwise the resolved
    ``base`` path itself.
    """
    common = _git_common_dir(base)
    if common is not None:
        # The main repo root is the parent of its .git directory.
        return str(Path(common).resolve().parent)
    return str(Path(base).resolve())


def _derive_board_name(key: str) -> str:
    """``<basename>-<6-hex>`` from a project key: readable and collision-safe."""
    base = os.path.basename(key.rstrip(os.sep)) or "root"
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:6]
    return f"{sanitize_board(base)}-{digest}"


def project_board(env: Optional[dict] = None) -> str:
    """The board derived from the current project (git repo, else launch dir).

    Uses ``$CLAUDE_PROJECT_DIR`` -- the project root Claude Code injects into an
    MCP server's environment -- falling back to the process CWD when it is unset
    (e.g. the inspection CLI run by hand).
    """
    env = os.environ if env is None else env
    base = (env.get("CLAUDE_PROJECT_DIR") or "").strip() or os.getcwd()
    return _derive_board_name(_project_key(base))


def resolve_board(env: Optional[dict] = None) -> str:
    """The active board name from the environment.

    ``$SWITCHBOARD_BOARD`` wins (verbatim, sanitized) unless it is the ``project``
    sentinel; otherwise the board is project-derived.
    """
    env = os.environ if env is None else env
    raw = (env.get("SWITCHBOARD_BOARD") or "").strip()
    if raw and raw.lower() != _PROJECT_SENTINEL:
        return sanitize_board(raw)
    return project_board(env)


def resolve_target(
    *,
    db_arg: Optional[str] = None,
    board_arg: Optional[str] = None,
    env: Optional[dict] = None,
) -> tuple[Path, str]:
    """Resolve ``(db_path, board_label)`` from CLI args and the environment.

    Precedence: an explicit db path (``--db`` / ``$SWITCHBOARD_DB``) → an explicit
    board (``--board`` / ``$SWITCHBOARD_BOARD``) → the project-derived board. When
    a raw db path is used the board label is that file's stem, purely for display.
    """
    env = os.environ if env is None else env

    if db_arg:
        p = Path(db_arg).expanduser()
        return p, p.stem
    if board_arg:
        board = sanitize_board(board_arg)
        return board_db_path(board), board

    db_env = (env.get("SWITCHBOARD_DB") or "").strip()
    if db_env:
        p = Path(db_env).expanduser()
        return p, p.stem

    board = resolve_board(env)
    return board_db_path(board), board
