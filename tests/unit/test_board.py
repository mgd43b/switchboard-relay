"""Unit tests for board (switchboard) resolution.

Covers the pure pieces (sanitizing, path mapping, precedence) with plain data,
and the git-backed project derivation both against a real repo+worktree (the
behavior that makes every worktree of a repo share one board) and via monkeypatch
for the failure branches that are awkward to reproduce with a live git.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import types

import pytest

from switchboard_relay import board

HAS_GIT = shutil.which("git") is not None
requires_git = pytest.mark.skipif(not HAS_GIT, reason="git not on PATH")

_NAME_RE = re.compile(r"[a-z0-9._-]+-[0-9a-f]{6}")


def _git(cwd, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.com",
        },
    )


# -- sanitize_board ---------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("lead", "lead"),
        ("Team Alpha!", "team-alpha"),
        ("  spaced  ", "spaced"),
        ("a/b\\c", "a-b-c"),
        ("UPPER", "upper"),
        ("...", "default"),
        ("", "default"),
        ("-._-", "default"),
        ("x" * 100, "x" * 64),
    ],
)
def test_sanitize_board(raw, expected):
    assert board.sanitize_board(raw) == expected


# -- path mapping -----------------------------------------------------------


def test_boards_dir(tmp_path):
    assert board.boards_dir(tmp_path) == tmp_path / ".claude" / "switchboard"


def test_legacy_db_path(tmp_path):
    assert board.legacy_db_path(tmp_path) == tmp_path / ".claude" / "switchboard.db"


def test_board_db_path_sanitizes(tmp_path):
    assert (
        board.board_db_path("Team X", home=tmp_path)
        == tmp_path / ".claude" / "switchboard" / "team-x.db"
    )


# -- derive_board_name ------------------------------------------------------


def test_derive_board_name_format():
    name = board._derive_board_name("/home/me/myproj")
    assert _NAME_RE.fullmatch(name)
    assert name.startswith("myproj-")


def test_derive_board_name_is_stable():
    assert board._derive_board_name("/a/b") == board._derive_board_name("/a/b")


def test_derive_board_name_distinguishes_same_basename():
    # Two different checkouts that share a basename must not collide.
    assert board._derive_board_name("/one/app") != board._derive_board_name("/two/app")


def test_derive_board_name_root_key():
    # A key with no basename (e.g. "/") falls back to "root".
    assert board._derive_board_name(os.sep).startswith("root-")


# -- _git_common_dir branches ----------------------------------------------


def test_git_common_dir_handles_subprocess_error(monkeypatch):
    def boom(*a, **k):
        raise OSError("git not found")

    monkeypatch.setattr(board.subprocess, "run", boom)
    assert board._git_common_dir("/anywhere") is None


def test_git_common_dir_nonzero_returncode(monkeypatch):
    monkeypatch.setattr(
        board.subprocess, "run", lambda *a, **k: types.SimpleNamespace(returncode=1, stdout="")
    )
    assert board._git_common_dir("/anywhere") is None


def test_git_common_dir_empty_output(monkeypatch):
    monkeypatch.setattr(
        board.subprocess, "run", lambda *a, **k: types.SimpleNamespace(returncode=0, stdout="  \n")
    )
    assert board._git_common_dir("/anywhere") is None


def test_git_common_dir_success(monkeypatch, tmp_path):
    gitdir = str(tmp_path / ".git")
    monkeypatch.setattr(
        board.subprocess,
        "run",
        lambda *a, **k: types.SimpleNamespace(returncode=0, stdout=gitdir + "\n"),
    )
    assert board._git_common_dir("/anywhere") == gitdir


# -- _project_key -----------------------------------------------------------


def test_project_key_uses_repo_root_when_git(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    monkeypatch.setattr(board, "_git_common_dir", lambda base: str(repo / ".git"))
    assert board._project_key("ignored") == str(repo.resolve())


def test_project_key_falls_back_to_dir_when_not_git(monkeypatch, tmp_path):
    monkeypatch.setattr(board, "_git_common_dir", lambda base: None)
    assert board._project_key(str(tmp_path)) == str(tmp_path.resolve())


# -- project_board / resolve_board ------------------------------------------


def test_project_board_uses_claude_project_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(board, "_git_common_dir", lambda base: None)
    name = board.project_board({"CLAUDE_PROJECT_DIR": str(tmp_path)})
    assert _NAME_RE.fullmatch(name)


def test_project_board_falls_back_to_cwd(monkeypatch, tmp_path):
    monkeypatch.setattr(board, "_git_common_dir", lambda base: None)
    monkeypatch.chdir(tmp_path)
    # No CLAUDE_PROJECT_DIR -> derive from the working directory, which lands on
    # the same board as pointing CLAUDE_PROJECT_DIR at it explicitly.
    assert board.project_board({}) == board.project_board({"CLAUDE_PROJECT_DIR": str(tmp_path)})


def test_resolve_board_env_wins_and_is_sanitized():
    assert board.resolve_board({"SWITCHBOARD_BOARD": "My Team"}) == "my-team"


def test_resolve_board_project_sentinel_derives(monkeypatch, tmp_path):
    monkeypatch.setattr(board, "_git_common_dir", lambda base: None)
    env = {"SWITCHBOARD_BOARD": "project", "CLAUDE_PROJECT_DIR": str(tmp_path)}
    assert _NAME_RE.fullmatch(board.resolve_board(env))


def test_resolve_board_defaults_to_project(monkeypatch, tmp_path):
    monkeypatch.setattr(board, "_git_common_dir", lambda base: None)
    assert _NAME_RE.fullmatch(board.resolve_board({"CLAUDE_PROJECT_DIR": str(tmp_path)}))


# -- resolve_target precedence ----------------------------------------------


def test_resolve_target_db_arg_wins(tmp_path):
    p, label = board.resolve_target(
        db_arg=str(tmp_path / "x.db"), board_arg="team", env={"SWITCHBOARD_DB": "/other.db"}
    )
    assert p == tmp_path / "x.db"
    assert label == "x"


def test_resolve_target_board_arg_beats_env_db(monkeypatch, tmp_path):
    monkeypatch.setattr(board, "boards_dir", lambda home=None: tmp_path / "sb")
    p, label = board.resolve_target(board_arg="Team X", env={"SWITCHBOARD_DB": "/other.db"})
    assert label == "team-x"
    assert p == tmp_path / "sb" / "team-x.db"


def test_resolve_target_db_env(tmp_path):
    p, label = board.resolve_target(env={"SWITCHBOARD_DB": str(tmp_path / "e.db")})
    assert p == tmp_path / "e.db"
    assert label == "e"


def test_resolve_target_board_env(monkeypatch, tmp_path):
    monkeypatch.setattr(board, "boards_dir", lambda home=None: tmp_path / "sb")
    p, label = board.resolve_target(env={"SWITCHBOARD_BOARD": "team"})
    assert label == "team"
    assert p == tmp_path / "sb" / "team.db"


def test_resolve_target_project_default(monkeypatch, tmp_path):
    monkeypatch.setattr(board, "_git_common_dir", lambda base: None)
    monkeypatch.setattr(board, "boards_dir", lambda home=None: tmp_path / "sb")
    p, label = board.resolve_target(env={"CLAUDE_PROJECT_DIR": str(tmp_path)})
    assert _NAME_RE.fullmatch(label)
    assert p == tmp_path / "sb" / f"{label}.db"


# -- describe_target reports the resolution source --------------------------


def test_describe_target_source_db_arg(tmp_path):
    assert board.describe_target(db_arg=str(tmp_path / "x.db")).source == "--db"


def test_describe_target_source_board_arg():
    assert board.describe_target(board_arg="team").source == "--board"


def test_describe_target_source_db_env(tmp_path):
    assert board.describe_target(env={"SWITCHBOARD_DB": str(tmp_path / "e.db")}).source == (
        "$SWITCHBOARD_DB"
    )


def test_describe_target_source_board_env():
    assert board.describe_target(env={"SWITCHBOARD_BOARD": "team"}).source == "$SWITCHBOARD_BOARD"


def test_describe_target_source_project(monkeypatch, tmp_path):
    monkeypatch.setattr(board, "_git_common_dir", lambda base: None)
    t = board.describe_target(env={"CLAUDE_PROJECT_DIR": str(tmp_path)})
    assert t.source == "project"
    assert _NAME_RE.fullmatch(t.board)


# -- real git: worktrees share a board, distinct repos do not ---------------


@requires_git
def test_worktrees_and_subdirs_share_one_board(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "commit", "--allow-empty", "-qm", "init")  # a worktree needs a commit
    worktree = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", str(worktree))
    subdir = repo / "src" / "pkg"
    subdir.mkdir(parents=True)

    main_board = board.project_board({"CLAUDE_PROJECT_DIR": str(repo)})
    wt_board = board.project_board({"CLAUDE_PROJECT_DIR": str(worktree)})
    sub_board = board.project_board({"CLAUDE_PROJECT_DIR": str(subdir)})

    assert main_board == wt_board == sub_board


@requires_git
def test_distinct_repos_get_distinct_boards(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    for r in (a, b):
        r.mkdir()
        _git(r, "init", "-q")
    assert board.project_board({"CLAUDE_PROJECT_DIR": str(a)}) != board.project_board(
        {"CLAUDE_PROJECT_DIR": str(b)}
    )


@requires_git
def test_git_common_dir_none_outside_repo(tmp_path):
    # A plain, non-repo directory yields no common dir.
    assert board._git_common_dir(str(tmp_path)) is None
