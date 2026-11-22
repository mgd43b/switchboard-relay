# switchboard-relay examples

Copy-paste prompt recipes for the lead / worker pattern in Codex, Claude Code, or a mix of both.
These assume switchboard-relay is installed through a native plugin or added as a user-level MCP
server (`codex mcp add switchboard_relay -- uvx switchboard-relay` or
`claude mcp add --scope user -- uvx switchboard-relay`).

> **Stuck?** Run `switchboard-relay doctor` — it says which board you're on and why nothing's flowing.

> **Boards:** sessions share a **per‑project board** by default, so the lead and workers below just
> work when they're in the same repo. To coordinate across *different* repos, put every session on
> the same named board first — add `--env SWITCHBOARD_BOARD=team` to each client's MCP config. See the
> [Boards section](../README.md#boards-one-switchboard-per-project) of the README.

## The lead (coordinator) session

Keep one Codex or Claude Code session open as the long-running "lead". Register it and park it in a
loop that answers whatever comes in.

Sessions register and listen between turns automatically, so with the Codex or Claude Code
plugin's `Stop` hook in place (trusted via `/hooks` in Codex) the lead only needs a name and a job:

```
Register me on switchboard-relay as "lead" and answer every message that wakes this
task. Reply to each sender with reply_to set, acknowledge the message with
ack(id, receipt), then finish normally so listening resumes. Keep going until I tell you to call standby(false).
```

Without that hook, paste this polling recipe and let it run:

```
Register me on switchboard-relay as "lead". Then loop: call wait()
with a 30s timeout; when a message arrives, treat its body as a question, work out
the answer, and send() the answer back to the message's `from` with `reply_to` set to
the message id. Acknowledge it with ack(id, receipt). If wait() times out, wait again.
Keep going until I stop you.
```

In Claude Code, you can also run it hands-free with the
[`/loop`](https://code.claude.com/docs/en/slash-commands) skill:

```
/loop wait for a switchboard-relay message, reply with reply_to set, then call ack(id, receipt)
```

## A worker session

In any other Codex or Claude Code session (another terminal, or another repo on the same board), ask
the lead a question and get the answer inline — one call. No registration step is required; the
session already has a generated address:

```
Use switchboard-relay's ask() to ask "lead": <your question>. Use the reply to continue
and acknowledge it with ack(reply.id, reply.receipt).
```

## Working from the task queue

1. Register the submitting session as `lead`. Call `task_submit()` with role `worker`, the
   requested scope, owned paths, acceptance criteria, and the evidence the lead expects.
   Run `task_list(submitter="lead")` to see your assignments, even after reconnecting as `lead`.
   Use `task_status(id)` for the full history and evidence.
2. Register a separate session as `worker:docs` with role `worker`. Call `task_claim()`, then
   use the task ID and receipt with `task_update()`. Record progress or blockers as you go.
   Renew the lease before the deadline. When done, record `completed` or `failed` with evidence.
3. If the worker exits, another session in role `worker` can claim the task after its lease
   expires. Read the previous attempt's notes and check the owned paths, diff, and test output
   before making further changes. Use the new claim's receipt for updates. The lead can still
   inspect each attempt after reconnecting under the same name. A worker that is done with its
   local changes can instead release the task immediately with a handoff note.
4. To stop work, call `task_cancel(id)` as the lead. Wait for `cancelled` before handing the same
   paths to another worker. A worker confirms cancellation after stopping by calling
   `task_update(action="cancelled", ...)` with its receipt and a note. If the worker session is
   gone, verify the worker stopped before calling `task_cancel(id, note, confirm_stopped=true)`.
   To requeue a failed task, call `task_retry(id, attempt)` with its failed attempt number.

For an offline worker, start its supervisor with `--task-role worker` and a command that registers
using the supplied environment. Have it call `inbox()` and acknowledge any direct mail it
handles, even when `task_claim()` returns `task: null`. When a task is available, it claims the
task and reports the result. Queue a task while the worker is offline to try the task trigger. See
[offline worker setup](../README.md#starting-an-offline-worker).

## Checking who's ready

To see which peers are on the board and whether their agents have read the current charter:

```
Call switchboard-relay's bootstrap() and tell me each peer's name, client, ready flag,
and listen_status.
```

`ask()` sends the question and blocks until the lead replies, so the worker just gets its
answer back and keeps working.

## Pre‑addressed workers (no explicit register)

Launch a worker already addressed via environment variables, so it can `send`/`ask`/`inbox`
without calling `register` first — useful for scripted fan‑out:

```bash
claude mcp add --scope user --env SWITCHBOARD_NAME=worker:build --env SWITCHBOARD_ROLE=worker \
  -- switchboard-relay

# Codex equivalent
codex mcp add switchboard_relay --env SWITCHBOARD_NAME=worker:build \
  --env SWITCHBOARD_ROLE=worker -- switchboard-relay
```

## Watching the traffic

From any terminal, peek at the shared state without an MCP client:

```bash
switchboard-relay doctor            # one-shot diagnostics: board, peers, queued mail, hints
switchboard-relay boards            # every local board + its live participant count
switchboard-relay participants      # who's live on this project's board
switchboard-relay tail --follow     # queued messages as they arrive
switchboard-relay participants --board team   # …a specific board
```
