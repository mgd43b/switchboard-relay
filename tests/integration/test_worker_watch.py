"""Exercise the supervisor against a real foreground worker and shared board."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import switchboard_relay.worker_watch as supervisor
from switchboard_relay.protocol import Protocol
from switchboard_relay.server import main
from switchboard_relay.store import Store

pytestmark = pytest.mark.skipif(os.name != "posix", reason="worker-watch uses POSIX file locks")

_WORKER = """
import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import Client, StdioServerParameters

async def run():
    params = StdioServerParameters(command=sys.argv[1], env=dict(os.environ))
    async with Client(params) as relay:
        registered = (await relay.call_tool("register", {})).structured_content
        messages = (await relay.call_tool("inbox", {})).structured_content["messages"]
        for message in messages:
            result = await relay.call_tool("ack", {
                "id": message["id"], "receipt": message["receipt"],
            })
            assert result.structured_content["ok"]
        Path(sys.argv[2]).write_text(json.dumps({
            "name": registered["you"], "db": os.environ["SWITCHBOARD_DB"],
            "messages": [message["body"] for message in messages],
        }))

asyncio.run(run())
"""


def test_real_worker_launch_is_opt_in_scoped_and_exclusive(tmp_path, monkeypatch, capsys):
    relay = Path(sys.executable).with_name("switchboard-relay")
    if not relay.is_file():
        pytest.skip("Install the console script to test a real worker")
    db = tmp_path / "board.db"
    store = Store(db)
    other = Store(tmp_path / "other.db")
    protocol = Protocol(store)
    role_name = "worker:docs"
    store.send("other", "wrong address", sender="lead", now=time.time())
    other.send(role_name, "wrong board", sender="lead", now=time.time())
    profile = tmp_path / "worker.py"
    profile.write_text(_WORKER)
    output = tmp_path / "worked.json"
    args = [
        "--db",
        str(db),
        "worker-watch",
        "--name",
        role_name,
        "--",
        sys.executable,
        str(profile),
        str(relay),
        str(output),
    ]
    messages = []
    logs = []
    deadline = time.monotonic() + 12
    ticks = 0

    def step(_seconds):
        nonlocal ticks
        ticks += 1
        logs.append(capsys.readouterr().err)
        if ticks == 1:
            assert "launching worker" not in logs[-1]
            duplicate = subprocess.run(
                [str(relay), *args], capture_output=True, text=True, check=False, timeout=5
            )
            assert duplicate.returncode == 1 and "already watched" in duplicate.stderr
            protocol.claim("existing", role_name, "", now=time.time())
            for body in ["one", "two"]:
                messages.append(store.send(role_name, body, sender="lead", now=time.time()))
        elif ticks == 2:
            assert "launching worker" not in logs[-1]
            protocol.leave("existing", time.time())
        if "worker exited with status 0" in logs[-1]:
            raise KeyboardInterrupt
        assert time.monotonic() < deadline, "".join(logs)
        time.sleep(0.02)

    monkeypatch.setattr(
        supervisor, "time", SimpleNamespace(time=time.time, monotonic=time.monotonic, sleep=step)
    )
    assert main(args) == 0
    assert "".join(logs).count("launching worker") == 1
    assert json.loads(output.read_text()) == {
        "name": role_name,
        "db": str(db.resolve()),
        "messages": ["one", "two"],
    }
    assert all(
        store.status(mid, sender="lead", now=time.time())["status"] == "acknowledged"
        for mid in messages
    )
    assert store.has_messages("other", now=time.time())
    assert other.has_messages(role_name, now=time.time())
    assert "startup disabled" in capsys.readouterr().err


def test_launch_failure_leaves_mail_queued(tmp_path, monkeypatch, capsys):
    db = tmp_path / "board.db"
    store = Store(db)
    mid = store.send("worker", "keep me", sender="lead", now=time.time())
    ticks = 0

    def step(_seconds):
        nonlocal ticks
        ticks += 1
        if ticks == 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(
        supervisor, "time", SimpleNamespace(time=time.time, monotonic=time.monotonic, sleep=step)
    )
    assert (
        main(
            [
                "--db",
                str(db),
                "worker-watch",
                "--name",
                "worker",
                "--",
                str(tmp_path / "missing"),
            ]
        )
        == 0
    )
    log = capsys.readouterr().err
    assert log.count("launching worker") == 1
    assert "launch failed" in log
    assert store.status(mid, sender="lead", now=time.time())["status"] == "queued"


@pytest.mark.parametrize("options", [[], ["--db", ":memory:"]])
def test_worker_watch_requires_explicit_file_backed_board(options, capsys):
    with pytest.raises(SystemExit) as error:
        main([*options, "worker-watch", "--name", "worker", "--", sys.executable])
    assert error.value.code == 2
    assert "worker-watch requires" in capsys.readouterr().err
