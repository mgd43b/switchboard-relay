# switchboard

[![CI](https://github.com/mgd43b/switchboard/actions/workflows/ci.yml/badge.svg)](https://github.com/mgd43b/switchboard/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/switchboard-mcp)](https://pypi.org/project/switchboard-mcp/)
[![Python](https://img.shields.io/pypi/pyversions/switchboard-mcp)](https://pypi.org/project/switchboard-mcp/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

**A local MCP server that gives independent Claude Code sessions a shared, durable messaging channel.**

Claude Code's built‑in session channel is injected only by the desktop/GUI app, so
terminal CLI sessions can't use it. A *config‑level* MCP server, by contrast, loads on
**every** surface — terminal CLI, desktop, and IDE — and its tools are allowlistable. `switchboard`
is a tiny such server: it routes named messages between any set of Claude Code sessions on
one machine, backed by SQLite so mailboxes survive restarts.

```
  worker:feature-x ──send("lead", "what's the API for X?")──▶  ┌───────────┐
                                                               │switchboard│  (SQLite mailbox)
  lead ◀──inbox() / wait()── "what's the API for X?" ──────────└───────────┘
  lead ──send("worker:feature-x", "use client.foo()")─────────▶  …delivered to worker's inbox
```

The canonical pattern: a long‑running **lead** session that short‑lived **worker** sessions ask
questions of — no copy‑pasting between windows. But `switchboard` just routes named messages, so
any addressing scheme works.

---

## Install

`switchboard` is a standard Python package (Python ≥ 3.10). Install it so the `switchboard`
command is on your `PATH`:

```bash
# Homebrew (macOS/Linux)
brew install mgd43b/taps/switchboard

# with uv (recommended)
uv tool install switchboard-mcp

# or pipx
pipx install switchboard-mcp

# or from a checkout of this repo
uv tool install .
```

Then register it with Claude Code at **user scope** (loads in every project, on every surface):

```bash
claude mcp add --scope user -- switchboard
```

That's it. Open any Claude Code session (terminal or desktop) and the five `switchboard`
tools are available. Verify with `claude mcp list`.

> **No install step?** If you'd rather not install anything, point Claude Code at `uvx`:
> ```bash
> claude mcp add --scope user -- uvx switchboard-mcp
> ```

### Make the tools run without a confirmation prompt (allowlist)

MCP tools are named `mcp__switchboard__<tool>`. To let them fire without a per‑call prompt,
add them to `permissions.allow` in your user settings (`~/.claude/settings.json`):

```json
{
  "permissions": {
    "allow": [
      "mcp__switchboard__register",
      "mcp__switchboard__participants",
      "mcp__switchboard__send",
      "mcp__switchboard__inbox",
      "mcp__switchboard__wait",
      "mcp__switchboard__ask",
      "mcp__switchboard__broadcast",
      "mcp__switchboard__unregister"
    ]
  }
}
```

Or allowlist the whole server with a single entry: `"mcp__switchboard"`.

---

## Tools

| Tool | Signature | What it does |
|------|-----------|--------------|
| `register` | `register(name, role?)` | Claim an address for this session (e.g. `"lead"`, `"worker:feature-x"`). `role` is an optional shared address (e.g. `"worker"`). Re‑call to heartbeat or change role. Returns the live participants. |
| `participants` | `participants()` | List sessions seen within the TTL window: `name`, `role`, `idle_seconds`. |
| `send` | `send(to, body, reply_to?)` | Append a message to `to`'s durable inbox. `to` matches a participant **name or role**. `reply_to` threads a reply to a message id. Returns the new message `id`. |
| `inbox` | `inbox(peek?, since?)` | Read messages addressed to you. **Drains** by default (each message delivered once); `peek=true` reads without removing; `since=<id>` returns only messages newer than that id. |
| `wait` | `wait(timeout_s?)` | Block up to `timeout_s` seconds (default 30, max 3600) until a message arrives, then drain and return it. Returns `timed_out: true` on timeout. |
| `ask` | `ask(to, body, timeout_s?)` | **Request/response in one call:** send `body` to `to`, then block until a reply threaded to it comes back (`reply_to` = the returned `question_id`). Leaves other inbox messages untouched; returns `timed_out: true` if no reply in time. |
| `broadcast` | `broadcast(body)` | Send `body` to every currently‑live participant except yourself. Returns the per‑recipient message ids. |
| `unregister` | `unregister()` | Leave the switchboard (drop out of `participants()`). Your mailbox is preserved for if you return. |

Messages are **durable**: a message sent to a name that hasn't registered yet simply waits in
that mailbox until it's read. Addressing by `role` fans a message out to whichever participant
reads with that role — for reliable one‑to‑one delivery, use unique names.

---

## The lead / worker pattern

**In your long‑running lead session** (e.g. a coordinator you keep open in a terminal):

```
Register me on switchboard as "lead", then keep calling wait() in a loop:
whenever a message arrives, answer the question by sending a reply back to
its sender (use reply_to), then wait() again.
```

Claude will `register(name="lead")` and then park in `wait()`. Pair it with the
[`/loop`](https://code.claude.com/docs/en/slash-commands) skill to keep it going hands‑free:

```
/loop wait for a switchboard message, answer it, and reply to the sender
```

**In each worker session:**

```
Register me on switchboard as "worker:auth" with role "worker", then use ask()
to ask the lead how our auth middleware refreshes tokens.
```

The worker calls `ask("lead", "…")` — one call that sends the question and blocks for the
answer. The lead's loop picks it up, replies with `reply_to` set, and the worker's `ask()`
returns the reply. No window‑switching, no manual `wait()`.

> **Tip:** launch a worker pre‑addressed with environment variables so it doesn't even need an
> explicit `register` call — set `SWITCHBOARD_NAME=worker:auth` and `SWITCHBOARD_ROLE=worker`
> in that session's MCP server env.

### Peek at the traffic from your terminal

`switchboard` doubles as a small inspection CLI over the same database — handy for debugging
who's connected and what's queued, without an MCP client:

```bash
switchboard participants     # live participants (name, role, idle time)
switchboard tail             # queued (undelivered) messages
switchboard tail --follow    # …and keep watching
```

Example recipes for the lead loop and workers live in [`examples/`](examples/).

---

## Configuration

All optional. Set as environment variables (e.g. via `claude mcp add --env KEY=value`):

| Variable | Default | Meaning |
|----------|---------|---------|
| `SWITCHBOARD_DB` | `~/.claude/switchboard.db` | SQLite database path (shared by all sessions). |
| `SWITCHBOARD_TTL` | `300` | Seconds of inactivity before a participant drops out of `participants()`. |
| `SWITCHBOARD_NAME` | — | Auto‑register this session under this address (skips an explicit `register`). |
| `SWITCHBOARD_ROLE` | — | Role to pair with `SWITCHBOARD_NAME`. |
| `SWITCHBOARD_PUSH` | `0` | Opt into experimental push (see below). Only meaningful in daemon mode. |

Example with a custom TTL:

```bash
claude mcp add --scope user --env SWITCHBOARD_TTL=600 -- switchboard
```

---

## How it works (and its one limitation)

Each Claude Code session spawns its **own** `switchboard` process (stdio transport). Those
processes don't talk to each other directly — they share state through the SQLite database, which
gives you durability and cross‑session delivery for free. `wait()` long‑polls that database.

This design has one consequence worth knowing: **switchboard cannot wake a session that isn't
already doing something.** A recipient learns about a message by calling `inbox()` or `wait()`
(on its next turn, or while parked in a `/loop`). Nothing can push into a fully idle or closed
session — that's a property of how Claude Code sessions work, not a switchboard limitation.

### Experimental: push via Channels (daemon mode)

There's an opt‑in path for lower‑latency delivery into a session that's *currently open*. Run
switchboard as a single shared HTTP daemon instead of per‑session stdio:

```bash
# start the daemon (push is a daemon-side opt-in)
SWITCHBOARD_PUSH=1 switchboard serve --host 127.0.0.1 --port 8765
# point every session at it instead of the stdio server
claude mcp add --scope user --transport http switchboard http://127.0.0.1:8765/mcp
```

In daemon mode all sessions connect to one process, so `send()` can emit a Claude Code
[Channels](https://code.claude.com/docs/en/channels) notification (`notifications/claude/channel`)
to a connected recipient. The notification is a **nudge to check your inbox**, not the message body —
the durable SQLite row stays the single source of truth, so a recipient still `inbox()`‑drains the
message exactly once (and a role‑addressed nudge reaches every connected member, but only one drains
the row). This is **experimental**: it requires the recipient session to be running and subscribed to
switchboard as a channel (Claude Code's channels feature is itself a research preview and may require
extra flags), and it never replaces durable delivery. Leave it off (the default) and everything works
via polling.

---

## Development

```bash
uv venv && uv pip install -e '.[dev]'
uv run pytest                 # runs all tiers; coverage gate is enforced (fail-under 95%)
uv run pytest -m unit         # or a single tier: unit | feature | integration
uv run ruff check .           # lint
uv run ruff format .          # format
```

- `src/switchboard/store.py` — the durable SQLite store (registry + mailboxes). Pure and clock‑free.
- `src/switchboard/server.py` — the FastMCP server: identity binding, the five tools, the `wait()` poll loop, and best‑effort push.
- `tests/` is split into three tiers:
  - `tests/unit/` — the SQLite store in isolation.
  - `tests/feature/` — tool/feature behavior through the server layer (identity, push, roles, the CLI) with a fake Context.
  - `tests/integration/` — the tools driven over a real MCP transport, including **two real stdio subprocesses** sharing one database.

CI (Python 3.10–3.14) runs ruff + the full suite with the coverage gate on every push and PR.
Releases and Homebrew packaging are documented in [RELEASING.md](RELEASING.md).

## Non‑goals (v1)

Single machine only (no cross‑machine bus), no auth/encryption, no transcript search, no GUI.

## License

[MIT](LICENSE)
