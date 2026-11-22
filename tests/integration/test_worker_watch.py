"""Exercise the supervisor against a real foreground worker and shared board."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace

import pytest

import switchboard_relay.worker_watch as supervisor
from switchboard_relay.protocol import Protocol
from switchboard_relay.server import main
from switchboard_relay.store import Store
from switchboard_relay.tasks import TaskQueue

pytestmark = pytest.mark.skipif(os.name != "posix", reason="worker-watch uses POSIX file locks")

_WORKER = """
import asyncio
import json
import os
import signal
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
    deadline = time.monotonic() + 30
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


def test_launch_failure_leaves_mail_queued(tmp_path, monkeypatch, capfd):
    db = tmp_path / "board.db"
    store = Store(db)
    mid = store.send("worker", "keep me", sender="lead", now=time.time())
    logs = []
    deadline = time.monotonic() + 10

    def step(_seconds):
        logs.append(capfd.readouterr().err)
        if "worker exited with status 1" in logs[-1]:
            raise KeyboardInterrupt
        assert time.monotonic() < deadline, "".join(logs)
        time.sleep(0.02)

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
    log = "".join(logs)
    assert log.count("launching worker") == 1
    assert "launch failed" in log
    assert store.status(mid, sender="lead", now=time.time())["status"] == "queued"
    assert Protocol(store).reserve_startup("worker", time.time()) is not None


def test_worker_helper_cannot_keep_the_watch_lock(tmp_path, monkeypatch):
    import fcntl

    db = tmp_path / "board.db"
    protocol = Protocol(Store(db))
    token = protocol.reserve_startup("worker", time.time())
    assert token is not None
    monkeypatch.setenv("SWITCHBOARD_DB", str(db))
    monkeypatch.setenv("SWITCHBOARD_NAME", "worker")
    monkeypatch.setenv("SWITCHBOARD_STARTUP_TOKEN", token)

    helper = tmp_path / "helper.py"
    helper.write_text("""
import sys
import time
from pathlib import Path

Path(sys.argv[1]).touch()
deadline = time.monotonic() + 10
while not Path(sys.argv[2]).exists() and time.monotonic() < deadline:
    time.sleep(0.02)
Path(sys.argv[3]).touch()
""")
    ready, stop, done = (tmp_path / flag for flag in ("ready", "stop", "done"))
    command = [
        sys.executable,
        "-c",
        "import subprocess, sys\nsubprocess.Popen(sys.argv[1:], close_fds=False)",
        sys.executable,
        str(helper),
        str(ready),
        str(stop),
        str(done),
    ]
    lock_path = tmp_path / "worker.lock"
    try:
        with lock_path.open("a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            assert supervisor._guard_worker((os.dup(lock.fileno()),), "board/worker", command) == 0

        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists() and not done.exists()
        with lock_path.open("a+b") as probe:
            fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        stop.touch()


def test_guard_rejects_expired_reservation_before_spawn(tmp_path, monkeypatch, capsys):
    db = tmp_path / "board.db"
    protocol = Protocol(Store(db))
    token = protocol.reserve_startup("worker", time.time() - 61)
    assert token is not None
    monkeypatch.setenv("SWITCHBOARD_DB", str(db))
    monkeypatch.setenv("SWITCHBOARD_NAME", "worker")
    monkeypatch.setenv("SWITCHBOARD_STARTUP_TOKEN", token)
    assert protocol.claim("manual", "worker", "", now=time.time()) == "worker"

    def unexpected_spawn(*args, **kwargs):
        pytest.fail("An expired reservation must not launch the worker")

    monkeypatch.setattr(supervisor.subprocess, "Popen", unexpected_spawn)
    with (tmp_path / "worker.lock").open("a+b") as lock:
        assert supervisor._guard_worker((os.dup(lock.fileno()),), "board/worker", ["worker"]) == 1
    assert "startup reservation expired" in capsys.readouterr().err


@pytest.mark.parametrize("options", [[], ["--db", ":memory:"]])
def test_worker_watch_requires_explicit_file_backed_board(options, capsys):
    with pytest.raises(SystemExit) as error:
        main([*options, "worker-watch", "--name", "worker", "--", sys.executable])
    assert error.value.code == 2
    assert "worker-watch requires" in capsys.readouterr().err


_TASK_WORKER = """
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
        task = (await relay.call_tool("task_claim", {})).structured_content["task"]
        marker = Path(sys.argv[2])
        temp = marker.with_suffix(".tmp")
        temp.write_text(json.dumps({
            "worker": registered["you"], "role": registered["role"], "id": task["id"],
        }))
        temp.replace(marker)
        stop = asyncio.get_running_loop().time() + 45
        while not Path(sys.argv[3]).exists() and asyncio.get_running_loop().time() < stop:
            await asyncio.sleep(0.02)
        result = await relay.call_tool("task_update", {
            "id": task["id"], "receipt": task["receipt"], "action": "completed",
            "note": "Finished the queued task",
        })
        assert result.structured_content["ok"]


asyncio.run(run())
"""


def _wait_for(check, description):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.025)
    pytest.fail(f"Timed out waiting for {description}")


def _stop_watcher(process):
    if process.poll() is None:
        process.send_signal(signal.SIGINT)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def test_task_role_starts_offline_workers_with_one_slot_per_role(tmp_path):
    import fcntl

    relay = Path(sys.executable).with_name("switchboard-relay")
    if not relay.is_file():
        pytest.skip("Install the console script to test a real worker")
    db = tmp_path / "board.db"
    store = Store(db)
    protocol = Protocol(store)
    queue = TaskQueue(store)
    protocol.claim("lead", "lead", "", now=time.time())
    profile = tmp_path / "task_worker.py"
    profile.write_text(_TASK_WORKER)

    def submit(role):
        return queue.submit(
            "lead",
            "lead",
            role=role,
            scope="Check docs",
            owned_paths=["README.md"],
            acceptance_criteria=[],
            expected_evidence="",
            clock=time.time,
        )["id"]

    def start(stack, name, gate):
        marker = tmp_path / f"{name}.json"
        log = tmp_path / f"{name}.log"
        process = subprocess.Popen(
            [
                str(relay),
                "--db",
                str(db),
                "worker-watch",
                "--name",
                name,
                "--task-role",
                "docs",
                "--",
                sys.executable,
                str(profile),
                str(relay),
                str(marker),
                str(gate),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=stack.enter_context(log.open("wb")),
        )
        stack.callback(_stop_watcher, process)
        _wait_for(lambda: "watching mail and tasks" in log.read_text(), f"{name} supervisor")
        return process, marker, log

    submit("other-role")
    release_one, release_two = tmp_path / "release-one", tmp_path / "release-two"
    release_two.touch()
    with ExitStack() as stack:
        try:
            first_watch, first_marker, first_log = start(stack, "worker-one", release_one)
            time.sleep(1.1)
            assert "launching worker" not in first_log.read_text()
            first_id = submit("docs")
            _wait_for(first_marker.exists, "first task claim")
            assert json.loads(first_marker.read_text()) == {
                "worker": "worker-one",
                "role": "docs",
                "id": first_id,
            }

            # Stop the watcher while its worker is still running. The guard
            # keeps the role slot until that actual foreground worker exits.
            _stop_watcher(first_watch)
            [slot] = tmp_path.glob(".switchboard-worker-role-*.lock")
            with (
                slot.open("a+b") as fd,
                pytest.raises(BlockingIOError),
            ):
                fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

            second_id = submit("docs")
            _, second_marker, second_log = start(stack, "worker-two", release_two)
            time.sleep(1.1)
            assert "launching worker" not in second_log.read_text()
            assert not second_marker.exists()
            assert not any(
                store.has_messages(name, now=time.time()) for name in ("worker-one", "worker-two")
            )

            release_one.touch()
            _wait_for(second_marker.exists, "second task claim after the role slot opens")
            assert json.loads(second_marker.read_text()) == {
                "worker": "worker-two",
                "role": "docs",
                "id": second_id,
            }
            _wait_for(
                lambda: len(queue.list_tasks(now=time.time(), state="completed")["tasks"]) == 2,
                "both task results",
            )
        finally:
            release_one.touch()

    protocol.leave("lead", time.time())
    reopened = Store(db)
    Protocol(reopened).claim("new-lead", "lead", "", now=time.time())
    page = TaskQueue(reopened).list_tasks(
        now=time.time(),
        viewer=("lead", "", "new-lead"),
        state="completed",
    )
    assert [task["id"] for task in page["tasks"]] == [first_id, second_id]
