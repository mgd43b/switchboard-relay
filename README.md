<div align="center">

# 🎛️ switchboard-relay

[![CI](https://github.com/mgd43b/switchboard-relay/actions/workflows/ci.yml/badge.svg)](https://github.com/mgd43b/switchboard-relay/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/switchboard-relay)](https://pypi.org/project/switchboard-relay/)
[![Python](https://img.shields.io/pypi/pyversions/switchboard-relay)](https://pypi.org/project/switchboard-relay/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

**One session asks, another answers — no copy‑pasting between terminals.**

A tiny local [MCP](https://modelcontextprotocol.io) server that gives independent Claude Code
sessions a shared, durable messaging channel.

</div>

Claude Code's built‑in session channel is injected only by the desktop app, so terminal sessions
can't use it. A *config‑level* MCP server, by contrast, loads on **every** surface — terminal CLI,
desktop, and IDE — and its tools are allowlistable. `switchboard-relay` is exactly that: it routes
named messages between any set of Claude Code sessions on one machine, backed by SQLite so mailboxes
survive restarts.

```
        register("worker:auth")                       register("lead")
                  │                                           │
   ┌──────────────▼──────────────┐    ask()    ┌──────────────▼─────────────┐
   │       worker session        │ ──────────▶ │        lead session        │
   │  "how do we refresh JWTs?"  │             │  parked in a wait() loop,  │
   │           …blocks…          │ ◀────────── │  answers with reply_to set │
   └──────────────┬──────────────┘    reply    └──────────────┬─────────────┘
                  │                                            │
                  └─────────────────────┬──────────────────────┘
                                        ▼
                     ┌──────────────────────────────────────┐
                     │  board  ·  myrepo-3f9c1a  (SQLite)    │
                     │  durable mailboxes, survive restarts  │
                     └──────────────────────────────────────┘
```

The canonical pattern is a long‑running **lead** that short‑lived **workers** ask questions of. But
switchboard just routes named messages, so any addressing scheme works.

---

## Contents

- [Quickstart](#quickstart) — zero to working in about a minute
- [Concepts](#concepts) — the five words that explain everything
- [Install](#install) — Claude Code, Claude Desktop, allowlisting
- [Tools](#tools) — the eight tools, at a glance
- [Boards: one switchboard per project](#boards-one-switchboard-per-project)
- [The lead / worker pattern](#the-lead--worker-pattern) — recipes + terminal inspection
- [Configuration](#configuration) — environment variables
- [How it works](#how-it-works) — and its one honest limitation
- [Turn injection (push)](#turn-injection-push) — make an open session *react* to a message, no daemon needed
- [Troubleshooting](#troubleshooting) — the common "huh?" moments
- [Development](#development)

---

## Quickstart

Three steps, no configuration.

**1. Install** (pick one):

```bash
brew install mgd43b/taps/switchboard-relay   # macOS/Linux (Homebrew)
uv tool install switchboard-relay            # or uv
pipx install switchboard-relay               # or pipx
```

**2. Add it to Claude Code** at user scope, so it loads in every project:

```bash
claude mcp add --scope user -- switchboard-relay
```

**3. Try it.** Open **two** Claude Code sessions *in the same repo*. Paste into the first:

```
Register me on switchboard-relay as "lead", then wait() for a message and reply to its sender.
```

…and into the second:

```
Register me on switchboard-relay, then ask() "lead": what should I work on next?
```

The second session gets its answer back inline — no window‑switching. That's the whole loop. 🎉

> **Why did that just work?** Both sessions share the same **board** (this repo), so they found each
> other with zero setup. The second session didn't even need a name — it registered under its
> session title. Read on for how names, roles, durability, and boards fit together.

---

## Concepts

Five words cover the whole model:

| Term | What it is |
|------|------------|
| **Participant** | A registered session. Any Claude Code session becomes one by calling `register()`. |
| **Name** | Your address that others `send()` to — e.g. `"lead"`, `"worker:auth"`. **Optional:** omit it and Claude registers under your **session title**. |
| **Role** | An optional *shared* address for a group (e.g. `"worker"`). A message to a role goes to whichever member reads it first. |
| **Board** | One isolated switchboard — its own participants and mailboxes. Defaults to **one per project**, so repos don't cross wires. |
| **Durable** | Messages wait in the recipient's mailbox until read — even if the recipient hasn't registered yet, or the process restarted. |

---

## Install

The fastest path is the **Claude Code plugin** — two commands, no Python setup (the server runs via
`uvx`). Prefer explicit config? Jump to [installing the package manually](#install-the-package-manually).

### Claude Code plugin (recommended)

Run these in any Claude Code session:

```
/plugin marketplace add mgd43b/switchboard-relay
/plugin install switchboard-relay@mgd43b
```

The `mcp__switchboard-relay__*` tools are wired up automatically — the plugin declares an MCP server
that runs via `uvx switchboard-relay`, so there's nothing to `pip install`. Verify with `/plugin` or
`claude mcp list`. (Installing the plugin covers Claude Code on every surface; the manual steps below
are the alternative, not an addition.)

### Install the package manually

`switchboard-relay` is a standard Python package (Python ≥ 3.10). Install it so the
`switchboard-relay` command is on your `PATH`:

```bash
# Homebrew (macOS/Linux)
brew install mgd43b/taps/switchboard-relay

# with uv (recommended)
uv tool install switchboard-relay

# or pipx
pipx install switchboard-relay

# or from a checkout of this repo
uv tool install .
```

#### Add it to Claude Code

Register it at **user scope** so it loads in every project, on every surface (terminal CLI,
desktop, and IDE):

```bash
claude mcp add --scope user -- switchboard-relay
```

That's it — open any Claude Code session and the eight `switchboard-relay` tools are available.
Verify with `claude mcp list`.

> **No install step?** Point Claude Code at `uvx` and skip installing anything:
> ```bash
> claude mcp add --scope user -- uvx switchboard-relay
> ```

#### Add it to Claude Desktop

Open **Settings → Developer → Edit Config** (or edit `claude_desktop_config.json` directly —
`~/Library/Application Support/Claude/` on macOS, `%APPDATA%\Claude\` on Windows) and add a
`switchboard-relay` entry:

```json
{
  "mcpServers": {
    "switchboard-relay": {
      "command": "switchboard-relay",
      "env": { "SWITCHBOARD_BOARD": "desktop" }
    }
  }
}
```

Then restart Claude Desktop. Notes:

- Use the full path (`which switchboard-relay`) if the binary isn't on Claude Desktop's `PATH`, or
  swap in `"command": "uvx", "args": ["switchboard-relay"]` to skip installing.
- Claude Desktop isn't project‑scoped, so pin an explicit `SWITCHBOARD_BOARD` (see
  [Boards](#boards-one-switchboard-per-project)) to keep its sessions on a predictable board.

### Run the tools without a confirmation prompt (optional)

By default Claude asks before each tool call. To let switchboard's tools fire silently, allowlist
them in your user settings (`~/.claude/settings.json`). The easy way — one entry for the whole
server:

```json
{ "permissions": { "allow": ["mcp__switchboard-relay"] } }
```

<details>
<summary>Or allowlist each tool individually</summary>

```json
{
  "permissions": {
    "allow": [
      "mcp__switchboard-relay__register",
      "mcp__switchboard-relay__participants",
      "mcp__switchboard-relay__send",
      "mcp__switchboard-relay__inbox",
      "mcp__switchboard-relay__wait",
      "mcp__switchboard-relay__ask",
      "mcp__switchboard-relay__broadcast",
      "mcp__switchboard-relay__unregister"
    ]
  }
}
```

</details>

---

## Tools

Eight tools, grouped by what you reach for:

**Presence** — join and see who's around

| Tool | Signature | What it does |
|------|-----------|--------------|
| `register` | `register(name?, role?)` | Claim an address for this session. `name` is **optional** — omit it and Claude registers under the **session title**. `role` is an optional shared group address. Re‑call to heartbeat or change role. Returns the `board` you joined and the live participants. |
| `participants` | `participants()` | List sessions seen within the TTL window: `name`, `role`, `idle_seconds`. |
| `unregister` | `unregister()` | Leave the switchboard (drop out of `participants()`). Your mailbox is preserved for when you return. |

**Send** — put a message in someone's mailbox

| Tool | Signature | What it does |
|------|-----------|--------------|
| `send` | `send(to, body, reply_to?)` | Append a message to `to`'s durable inbox. `to` matches a participant **name or role**. `reply_to` threads a reply to a message id. Returns the new message `id` — plus `no_live_recipient: true` and a `warning` if nobody is currently registered as `to` (usually a typo; the message is still queued). |
| `broadcast` | `broadcast(body)` | Send `body` to every currently‑live participant except yourself. Returns the per‑recipient message ids. |

**Receive** — read your mailbox

| Tool | Signature | What it does |
|------|-----------|--------------|
| `inbox` | `inbox(peek?, since?)` | Read messages addressed to you. **Drains** by default (each delivered once); `peek=true` reads without removing; `since=<id>` returns only messages newer than that id. |
| `wait` | `wait(timeout_s?)` | Block up to `timeout_s` seconds (default 30, max 3600) until a message arrives, then drain and return it. Returns `timed_out: true` on timeout. |

**Ask** — send and block for the answer in one call

| Tool | Signature | What it does |
|------|-----------|--------------|
| `ask` | `ask(to, body, timeout_s?)` | Send `body` to `to`, then block until a reply threaded to it comes back (`reply_to` = the returned `question_id`). Leaves other inbox messages untouched; returns `timed_out: true` if no reply in time — with `no_live_recipient: true` when nobody was registered as `to` at send time, so you can tell a wrong address from a slow reply. |

> **Durability & addressing.** A message sent to a name that hasn't registered yet simply waits in
> that mailbox until it's read. Addressing by `role` fans a message out to whichever participant
> reads with that role first — for reliable one‑to‑one delivery, use unique names.

---

## Boards: one switchboard per project

A **board** is one isolated switchboard — its own participants and its own mailboxes. By default the
board is derived from your **project**, so sessions in different repos don't see each other and each
project gets a private bus for free. All of a repo's **git worktrees** (and any subdirectory)
resolve to the *same* board, because the board is keyed off the repository's shared `.git`, not the
working directory.

The board a session joins is resolved in this order:

1. **`$SWITCHBOARD_BOARD`** — an explicit board name (any string), used verbatim. The special value
   `project` forces the project‑derived board below.
2. **The current project** *(the default)* — keyed off the git repo (via `CLAUDE_PROJECT_DIR`, which
   Claude Code injects into the server), falling back to the launch directory when it isn't a git
   repo. The resulting board name looks like `myrepo-3f9c1a`.

`register()` returns the `board` you joined, so a session can always see which switchboard it's on.
Each board is its own SQLite file under `~/.claude/switchboard/<board>.db`. (Setting `SWITCHBOARD_DB`
to a raw path still overrides everything — handy for pointing several sessions at one exact file.)

### Sharing a board across projects

Want the classic cross‑repo setup where a **worker** in repo A asks a **lead** in repo B? Put both
sessions on the same named board:

```bash
# lead, in repo B
claude mcp add --scope user --env SWITCHBOARD_BOARD=team -- switchboard-relay
# worker, in repo A — same board name
claude mcp add --scope user --env SWITCHBOARD_BOARD=team -- switchboard-relay
```

Any shared string works; pick one name and use it everywhere those sessions should talk.

> **Upgrading from ≤ 0.2?** The default used to be a single global board (`~/.claude/switchboard.db`).
> It's now per‑project. To get the old global behavior back, set `SWITCHBOARD_BOARD` to a shared name
> (as above), or point `SWITCHBOARD_DB` at the old file.

---

## The lead / worker pattern

Within one project this works with **zero board configuration** — every session in the repo shares
the project's board automatically. For a lead and workers spread across *different* repos, first put
them on a shared board (see [Boards](#boards-one-switchboard-per-project)).

### The lead (coordinator)

Keep one session open as the long‑running lead. Paste this and let it run:

```
Register me on switchboard-relay as "lead", then keep calling wait() in a loop:
whenever a message arrives, answer the question by sending a reply back to its
sender (use reply_to), then wait() again.
```

Claude will `register(name="lead")` and park in `wait()`. A lead keeps a well‑known name so workers
can address it; anyone who doesn't need a fixed address can just say *"register me on
switchboard-relay"* and Claude registers under the session title. Keep it going hands‑free with the
[`/loop`](https://code.claude.com/docs/en/slash-commands) skill:

```
/loop wait for a switchboard-relay message, answer it, and reply to the sender
```

### A worker

In any other session, ask the lead and get the answer inline — one call, no explicit name needed:

```
Register me on switchboard-relay with role "worker", then use ask() to ask the
lead how our auth middleware refreshes tokens.
```

The worker registers under its session title and calls `ask("lead", "…")` — one call that sends the
question and blocks for the answer. The lead's loop picks it up, replies with `reply_to` set, and the
worker's `ask()` returns the reply. No window‑switching, no manual `wait()`.

> **Tip:** launch a worker pre‑addressed via environment variables so it doesn't even need an
> explicit `register` call — set `SWITCHBOARD_NAME=worker:auth` and `SWITCHBOARD_ROLE=worker` in that
> session's MCP server env.

More copy‑paste recipes live in [`examples/`](examples/).

### Peek at the traffic from your terminal

`switchboard-relay` doubles as a small inspection CLI over the same database — handy for seeing who's
connected and what's queued, without an MCP client. It targets the current project's board by
default; add `--board <name>` (or `--db <path>`) to inspect another:

```bash
switchboard-relay doctor                         # ⭐ "why isn't this working?" — resolution, peers, hints
switchboard-relay boards                         # every local board + its live participant count
switchboard-relay participants                   # live participants on this board (name, role, idle)
switchboard-relay tail                           # queued (undelivered) messages on this board
switchboard-relay tail --follow                  # …and keep watching
switchboard-relay prune                          # delete old dead-letter messages + expired participants
switchboard-relay participants --board team      # …a specific board instead
```

**`doctor`** is the one-shot diagnostic: it prints which board you resolved to (and *how* — `--board`,
`$SWITCHBOARD_BOARD`, project-derived, …), the relevant env vars, live participants, the queued-message
count, and a plain-English hint when something looks off (you're alone on a board, or messages are
piling up against a name nobody reads).

---

## Configuration

All optional. Set as environment variables (e.g. via `claude mcp add --env KEY=value`):

| Variable | Default | Meaning |
|----------|---------|---------|
| `SWITCHBOARD_BOARD` | *(project)* | Board to join. An explicit name (any string) puts these sessions on a shared bus; `project` forces per‑project derivation. See [Boards](#boards-one-switchboard-per-project). |
| `SWITCHBOARD_DB` | *(the board's file)* | Raw SQLite path override — wins over `SWITCHBOARD_BOARD`. Point several sessions at one exact file to share it. |
| `SWITCHBOARD_TTL` | `300` | Seconds of inactivity before a participant drops out of `participants()`. |
| `SWITCHBOARD_MSG_TTL` | `604800` (7 days) | Undelivered messages older than this are pruned automatically during normal operation. Set `0` to disable age‑out. |
| `SWITCHBOARD_MAX_BODY` | `262144` (256 KiB) | Reject a `send()` whose body exceeds this many UTF‑8 bytes. Set `0` to disable the cap. |
| `SWITCHBOARD_NAME` | — | Auto‑register this session under this address (skips an explicit `register`; also the fallback when `register()` is called without a name). |
| `SWITCHBOARD_ROLE` | — | Role to pair with `SWITCHBOARD_NAME`. |
| `SWITCHBOARD_PUSH` | *(stdio: off, daemon: on)* | Enable [turn‑injection push](#turn-injection-push). On **stdio** it's off by default (a background poll cost) — set `1` to run the self‑watch loop. In the **daemon** it defaults **on** (set `0` to disable). |

Example — a longer liveness window:

```bash
claude mcp add --scope user --env SWITCHBOARD_TTL=600 -- switchboard-relay
```

---

## How it works

Each Claude Code session spawns its **own** `switchboard-relay` process (stdio transport). Those
processes don't talk to each other directly — they share state through a SQLite database (one file
per [board](#boards-one-switchboard-per-project)), which gives you durability and cross‑session
delivery for free. `wait()` long‑polls that database.

**The one honest limitation:** switchboard **cannot wake a *closed* session.** In the baseline
(poll) model a recipient learns about a message by calling `inbox()` or `wait()` — on its next turn,
or while parked in a `/loop`. An *open but idle* session can be pushed into a turn with
[turn injection](#turn-injection-push), but nothing reaches a session that isn't running. That's a
property of how Claude Code sessions work, not a switchboard limitation.

---

## Turn injection (push)

By default a recipient only learns about a message when *it* next polls (`inbox()`/`wait()`).
**Turn injection** removes that wait: a `send()` makes the recipient's open session **react on the
spot** — the message arrives *as a turn*, and the session drains its inbox and acts, with no manual
poll. It rides Claude Code's [Channels](https://code.claude.com/docs/en/channels) capability (a
research preview) — the "responsive‑lead" setup, a lead that answers the instant a worker asks.

### The three hard constraints (be honest about these)

Turn injection works **only** when all three hold. Miss any one and delivery silently falls back to
the durable poll — nothing breaks, the message just waits in the inbox as usual:

1. **Push is enabled.** It's a background convenience with a small polling cost, so it's **off by
   default on stdio** — set `SWITCHBOARD_PUSH=1` to turn it on. (The daemon defaults it **on**.)
2. **The recipient session is open.** Channels inject into a *running* session on its next turn.
   Nothing can wake a fully idle or closed session — see [How it works](#how-it-works).
3. **The recipient subscribed to switchboard as a channel** — launched with the channel flag below.
   A session that connected normally still works; it just polls instead of reacting.

Note that "daemon" is **not** a constraint: turn injection works over the plain stdio install (below).
The daemon is an optional alternative for the multi‑client‑on‑one‑process case.

### Setup (recommended: stdio, no daemon)

Because MCP stdio is bidirectional, each session's own switchboard process can push a notification to
*its own* client. With push enabled, that process runs a background watcher that polls the shared
board and self‑nudges when a message lands — so you get turn injection with **no daemon, no launchd,
no reboot story**, and [per‑project boards](#boards-one-switchboard-per-project) keep working.

```bash
# 1. Register the stdio server with push enabled (one extra env var):
claude mcp add --scope user --env SWITCHBOARD_PUSH=1 -- switchboard-relay

# 2. Launch each session that should REACT with switchboard subscribed as a
#    channel. A config‑level MCP server is a `server:` channel, which during the
#    research preview is never on the first‑party allowlist — so it needs the
#    development flag (plain `--channels server:…` is skipped as "not on the
#    approved channels allowlist"):
claude --dangerously-load-development-channels server:switchboard-relay
```

That last flag is the whole subscription recipe. On start you'll see a dim confirmation like
`Channels (experimental) messages from server:switchboard-relay inject directly in this session`.
Now park the lead in a `wait()` [`/loop`](https://code.claude.com/docs/en/slash-commands) and have
workers `ask()` as usual — the lead reacts the moment a question lands, no poll required.

> **Org policy.** On Claude.ai Team/Enterprise (and Console orgs with managed settings) an admin must
> set [`channelsEnabled: true`](https://code.claude.com/docs/en/channels#enterprise-controls) first,
> or channels are blocked (the server still connects and its tools still work — only the *push* is
> suppressed). Pro/Max users without an org skip that check.

### Alternative: the shared daemon

Prefer one long‑lived process holding every connection (e.g. many clients, or lower‑latency
push‑on‑write instead of a poll)? Run the daemon instead of stdio. It needs a supervisor and a reboot
story (launchd/`KeepAlive`, or a container with `--restart unless-stopped`), and it's board‑scoped
(one process per board), so it's the heavier option — but the subscription flag is identical.

```bash
# Push is ON by default in the daemon (SWITCHBOARD_PUSH=0 disables); the banner
# prints the subscription flag.
switchboard-relay serve --host 127.0.0.1 --port 8765
claude mcp add --scope user --transport http switchboard-relay http://127.0.0.1:8765/mcp
# ...then launch each reacting session with the same --dangerously-load-development-channels flag.
```

### What's actually sent (and why it stays exactly‑once)

Either way, switchboard emits a `notifications/claude/channel` notification that Claude Code wraps
into the recipient's next turn as `<channel source="switchboard-relay" msg_from="…"
msg_id="…">…</channel>`. **stdio:** each session's watcher polls the board for messages addressed to
*itself* and self‑nudges. **daemon:** `send()` pushes directly to the connected recipient. The
cross‑session hop is the shared SQLite board in both cases.

The notification is a **nudge that says "drain your inbox and handle it"**, *not* the message body.
The durable SQLite row stays the single source of truth, which is what preserves **drain‑once**: when
a **role** is addressed, every connected member is nudged, but only the one that wins the atomic
`inbox()` drain receives the message — the others find an empty inbox (the nudge says so) and do
nothing. (Inlining the body for a unique‑name/single‑reader target was considered and deliberately
declined: it would split the source of truth and risk double‑handling.)

Two things stay true no matter what: push **never replaces durable delivery** (disable it and
everything still works by polling), and it **can't wake a closed session**. Channels is a research
preview whose contract may change, so treat push as the *fast path on top of* the durable poll, never
a dependency.

### Verify it end‑to‑end (two real sessions)

The research‑preview reaction can't be unit‑tested, so confirm it by hand once (stdio, no daemon):

1. **Register the server with push on:** `claude mcp add --scope user --env SWITCHBOARD_PUSH=1 -- switchboard-relay`.
2. **Terminal 1 — session B (the reactor):** launch subscribed, register, and park it:
   ```bash
   claude --dangerously-load-development-channels server:switchboard-relay
   ```
   In B: `register(name="lead")`, then run `/loop` with `wait(timeout_s=600)` (or just leave it idle).
   Do **not** call `inbox()` by hand.
3. **Terminal 2 — session A (the sender):** launch (subscription optional for a pure sender),
   `register(name="worker")`, then `send(to="lead", body="ping — what's 2+2?")`.
4. **Watch B react with no manual poll:** within ~a second a `<channel source="switchboard-relay" …>`
   turn appears in B, and B calls `inbox()` on its own, reads *"ping — what's 2+2?"*, and handles it
   (e.g. replies with `send(to="worker", body="4", reply_to=<id>)`). If instead B does nothing,
   re‑check the three constraints — most often push wasn't enabled (`SWITCHBOARD_PUSH=1`), B wasn't
   launched with `--dangerously-load-development-channels`, or an org policy has `channelsEnabled` off.

---

## Troubleshooting

**Start here:** run **`switchboard-relay doctor`**. It resolves your board, lists live peers and queued
messages, and prints a hint for the two most common failures below — usually enough to spot the problem
in one shot.

**"I sent a message but nothing happened."**
switchboard can't wake an idle session — a recipient only sees a message when *it* calls `inbox()` or
`wait()`. Keep your lead parked in a [`/loop`](#the-lead--worker-pattern) so it's always listening.
(See [How it works](#how-it-works).)

**"The other session can't see me / `participants()` is empty."**
You're probably on different **boards** — each project gets its own by default. Run
`switchboard-relay boards` to list them, and make sure both sessions are on the same one: the same
repo, or the same `SWITCHBOARD_BOARD`. (See [Boards](#boards-one-switchboard-per-project).)

**"`ask()` timed out, or a `send()` came back with `no_live_recipient`."**
Nobody is registered under that name/role right now — usually a typo (`"leed"` vs `"lead"`) or the
recipient is offline. Check live addresses with `participants()` or `switchboard-relay participants`.
The message is still queued durably, so a correctly‑named recipient gets it later.

**"register() gave me a `session-…` name I didn't choose."**
No `name` was passed and none could be derived, so one was assigned. That's fine for a worker that
only asks questions; for anything others need to address (like a lead), pass an explicit name —
`register(name="lead")`.

---

## Development

```bash
uv venv && uv pip install -e '.[dev]'
uv run pytest                 # runs all tiers; coverage gate is enforced (fail-under 95%)
uv run pytest -m unit         # or a single tier: unit | feature | integration
uv run ruff check .           # lint
uv run ruff format .          # format
```

Source layout — each module is small and single‑purpose:

- [`store.py`](src/switchboard_relay/store.py) — the durable SQLite store (registry + mailboxes). Pure and clock‑free.
- [`board.py`](src/switchboard_relay/board.py) — board resolution: which switchboard a session joins (env / git worktree → DB path). Pure and transport‑free.
- [`server.py`](src/switchboard_relay/server.py) — the FastMCP server: identity binding, the eight tools, the `wait()` poll loop, and best‑effort push.

Tests are split into three tiers:

- `tests/unit/` — the SQLite store and board resolution in isolation.
- `tests/feature/` — tool behavior through the server layer (identity, push, roles, hygiene bounds, the CLI) with a fake Context.
- `tests/integration/` — the tools over a real MCP transport, including **two real stdio subprocesses**, plus an **N‑process exactly‑once stress test** that reconciles sent‑vs‑received ids under contention (including shared‑role drains).

The Claude Code plugin/marketplace manifests live in [`.claude-plugin/`](.claude-plugin/); validate them
with `claude plugin validate .`.

CI (Python 3.10–3.14) runs ruff + the full suite with the coverage gate on every push and PR. Releases
and Homebrew packaging are documented in [RELEASING.md](RELEASING.md).

## Non‑goals (v1)

Single machine only (no cross‑machine bus), no auth/encryption, no transcript search, no GUI.

## License

[MIT](LICENSE)
