# switchboard-relay examples

Copy‑paste prompt recipes for the lead / worker pattern. These assume switchboard-relay is
installed and added at user scope (`claude mcp add --scope user -- switchboard-relay`).

## The lead (coordinator) session

Keep one Claude Code session open as the long‑running "lead". Register it and park it in a
loop that answers whatever comes in. Paste this, then let it run:

```
Register me on switchboard-relay as "lead" (role "coordinator"). Then loop: call wait()
with a 300s timeout; when a message arrives, treat its body as a question, work out
the answer, and send() the answer back to the message's `from` with `reply_to` set to
the message id. If wait() times out, just wait() again. Keep going until I stop you.
```

Run it hands‑free with the [`/loop`](https://code.claude.com/docs/en/slash-commands) skill:

```
/loop wait for a switchboard-relay message, answer it, and reply to the sender with reply_to set
```

## A worker session

In any other Claude Code session (a different repo, a different terminal), ask the lead a
question and get the answer inline — one call:

```
Register me on switchboard-relay as "worker:$TASK" with role "worker". Then use ask() to ask
"lead": <your question>. Use the reply to continue.
```

`ask()` sends the question and blocks until the lead replies, so the worker just gets its
answer back and keeps working.

## Pre‑addressed workers (no explicit register)

Launch a worker already addressed via environment variables, so it can `send`/`ask`/`inbox`
without calling `register` first — useful for scripted fan‑out:

```bash
claude mcp add --scope user --env SWITCHBOARD_NAME=worker:build --env SWITCHBOARD_ROLE=worker \
  -- switchboard-relay
```

## Watching the traffic

From any terminal, peek at the shared state without an MCP client:

```bash
switchboard-relay participants     # who's live
switchboard-relay tail --follow    # queued messages as they arrive
```
