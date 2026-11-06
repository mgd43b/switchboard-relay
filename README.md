<div align="center">

# 🎛️ switchboard-relay

[![CI](https://github.com/mgd43b/switchboard-relay/actions/workflows/ci.yml/badge.svg)](https://github.com/mgd43b/switchboard-relay/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/switchboard-relay)](https://pypi.org/project/switchboard-relay/)
[![Python](https://img.shields.io/pypi/pyversions/switchboard-relay)](https://pypi.org/project/switchboard-relay/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

**One session asks, another answers — no copy‑pasting between terminals.**

A tiny local [MCP](https://modelcontextprotocol.io) server that gives independent Codex,
Claude Code, and other MCP client sessions a shared, durable messaging channel.

</div>

Because `switchboard-relay` is a standards-based *config-level* MCP server, the same tools work in
Codex, Claude Code, and generic MCP clients. It routes named messages between any mix of local
sessions on one machine, backed by SQLite so mailboxes survive restarts. Sessions claim an address,
discover their peers, and receive the board's messaging conventions automatically when they
connect. The Codex and Claude Code plugins also keep sessions listening between turns; generic MCP
clients use the durable `inbox()` / `wait()` path.

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
- [Install](#install) — Codex, Claude Code, Claude Desktop, approvals
- [Tools](#tools) — every tool, at a glance
- [Boards: one switchboard per project](#boards-one-switchboard-per-project)
- [Connecting: identity, peers, and the charter](#connecting-identity-peers-and-the-charter)
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

**2. Add it to Codex or Claude Code** at user scope, so it loads in every project:

```bash
codex mcp add switchboard_relay -- switchboard-relay
# or
claude mcp add --scope user -- switchboard-relay
```

**3. Try it.** Open **two local Codex or Claude Code sessions** *in the same repo*. They can be
two sessions from one client or one of each. Paste into the first:

```
Register me on switchboard-relay as "lead", then wait() for a message and reply to its sender.
```

…and into the second:

```
Register me on switchboard-relay, then ask() "lead": what should I work on next?
```

The second session gets its answer back inline — no window‑switching. That's the whole loop. 🎉

> **Why did that just work?** Both sessions share the same **board** (this repo), so they found each
> other with zero setup. The second session didn't even need a name — it was registered under a
> unique generated address as soon as it connected. Read on for how names, roles, durability, and boards fit together.

---

## Concepts

Five words cover the whole model:

| Term | What it is |
|------|------------|
| **Participant** | A registered Codex, Claude Code, or other MCP client session. |
| **Name** | Your address that others `send()` to — e.g. `"lead"`, `"worker:auth"`. **Optional:** every session is registered under a unique generated address when it connects, built from the client's name plus a random suffix (e.g. `claude-code-3fa9c1d2e4b5`), or `session-*` if the client didn't identify itself. |
| **Role** | An optional *shared* address for a group (e.g. `"worker"`). A message to a role goes to whichever member reads it first. |
| **Board** | One isolated switchboard — its own participants and mailboxes. Defaults to **one per project**, so repos don't cross wires. |
| **Durable** | Messages wait in the recipient's mailbox until read — even if the recipient hasn't registered yet, or the process restarted. |

---

## Install

Use the native plugin for your client, or register the STDIO MCP server directly. Both plugin
packages run the published Python package through `uvx`, so no separate Python installation is
required.

### Codex plugin

This repository includes a Codex plugin manifest at [`.codex-plugin/plugin.json`](.codex-plugin/plugin.json)
and its local MCP definition at [`.mcp.json`](.mcp.json). Install the repository marketplace and
plugin with:

```bash
codex plugin marketplace add mgd43b/switchboard-relay
codex plugin add switchboard-relay@mgd43b
```

The plugin supplies the `switchboard-relay` MCP server and Codex lifecycle hook automatically.
Its bundled server key is `switchboard_relay_default`, keeping it isolated from project-specific
plugins that may pin their own switchboard board and package version.
After a new release, refresh the Git-backed marketplace and reinstall the plugin snapshot:

```bash
codex plugin marketplace upgrade mgd43b
codex plugin add switchboard-relay@mgd43b
```

Restart the ChatGPT desktop app and begin a new task after installing or upgrading so Codex picks
up the new MCP tools and hooks.

For MCP tools without the plugin lifecycle hook, add only the server:

```bash
codex mcp add switchboard_relay -- uvx switchboard-relay
codex mcp list
```

The ChatGPT desktop app, Codex CLI, and Codex IDE extension on the same host share this MCP
configuration. Restart the active client after adding it. The bundled config allows `wait()`,
`ask()`, and the standby hook to run through the server's 3600-second polling window.

The plugin also bundles a Codex `Stop` hook, [`hooks/codex.json`](hooks/codex.json), which the
manifest names explicitly so Codex never loads the Claude Code hook file. Installed plugin hooks are
not trusted automatically: review and trust it with `/hooks`. Once trusted, every session listens
between turns by default. When a turn finishes, the hook waits up to 15 minutes for durable mail. If
mail arrives, it continues the turn so the session can call `inbox()` and reply. If the window ends
quietly, it continues the turn once so the session can re-arm with `standby(true)` and finish. Call
`standby(false)` to stop listening for the rest of the session, or set `SWITCHBOARD_AUTO_LISTEN=0`
to start with listening off. Until the hook is trusted, the session reports `listen_status`
`configured` rather than `armed`. See the
[official Codex hooks documentation](https://learn.chatgpt.com/docs/hooks#stop).

Direct `codex mcp add` installs only the MCP server, not the plugin hook. To listen between turns
with a direct server named `switchboard_relay` (as in the commands below), add this `Stop` entry to
`~/.codex/hooks.json` and review it with `/hooks`. The `server` value must match the name you gave
`codex mcp add`:

```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          {
            "type": "mcp_tool",
            "server": "switchboard_relay",
            "tool": "host_standby",
            "input": { "timeout_s": 900 },
            "timeout": 960,
            "statusMessage": "Listening for switchboard messages"
          }
        ]
      }
    ]
  }
}
```

### Claude Code plugin (recommended)

Run these in any Claude Code session:

```
/plugin marketplace add mgd43b/switchboard-relay
/plugin install switchboard-relay@mgd43b
```

The switchboard tools are wired up automatically — the plugin declares an MCP server
that runs via `uvx switchboard-relay`, so there's nothing to `pip install`. Verify with `/plugin` or
`claude mcp list`. (Installing the plugin covers Claude Code on every surface; the manual steps below
are the alternative, not an addition.)

The plugin also ships a Claude Code `Stop` hook, [`hooks/hooks.json`](hooks/hooks.json), that keeps
the session listening between turns. It targets the plugin's server by its scoped name,
`plugin:switchboard-relay:switchboard-relay`. When a turn finishes, the hook waits up to 15 minutes
for durable mail. If mail arrives, it continues the turn so the session can read and answer it. If
the window ends quietly, it continues the turn once so the session can re-arm with `standby(true)`
and finish. An idle Claude session therefore takes about four short re-arm turns an hour. Call
`standby(false)` to stop listening for the rest of the session, or set `SWITCHBOARD_AUTO_LISTEN=0`
to start with listening off. Allowlist the `standby` tool (see
[below](#run-the-tools-without-a-confirmation-prompt-optional)) so an unattended re-arm doesn't stop
at a permission prompt.

The 15-minute window stays under Claude Code's 30-minute limit for an MCP call that sends no
response or progress. Claude Code aborts such a call and reports an error (adjust it with the
server's `timeout` setting or `CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT`). Keep your own `wait()` and
`ask()` timeouts under 1800 seconds in Claude Code for the same reason.

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

That's it — open any Claude Code session and the `switchboard-relay` tools are available.
Verify with `claude mcp list`.

A direct install doesn't include the plugin's `Stop` hook. To listen between turns, add this to
`~/.claude/settings.json`. A server you add yourself is addressed by its bare name,
`switchboard-relay`, rather than the plugin's scoped name:

```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          {
            "type": "mcp_tool",
            "server": "switchboard-relay",
            "tool": "host_standby",
            "input": { "timeout_s": 900 },
            "timeout": 960,
            "statusMessage": "Listening for switchboard messages"
          }
        ]
      }
    ]
  }
}
```

> **No install step?** Point Claude Code at `uvx` and skip installing anything:
> ```bash
> claude mcp add --scope user -- uvx switchboard-relay
> ```

#### Add it to Codex

Register the same STDIO server with Codex:

```bash
codex mcp add switchboard_relay -- uvx switchboard-relay
```

Verify with `codex mcp list` or `/mcp`. For a cross-client board shared with Claude Code, pin the
same explicit board in both clients:

```bash
codex mcp add switchboard_relay --env SWITCHBOARD_BOARD=team -- uvx switchboard-relay
claude mcp add --scope user --env SWITCHBOARD_BOARD=team -- uvx switchboard-relay
```

Direct CLI registration uses Codex's default MCP tool timeout. If a lead should make individual
`wait()` calls longer than that timeout, set the server entry in `~/.codex/config.toml` explicitly:

```toml
[mcp_servers.switchboard_relay]
command = "uvx"
args = ["switchboard-relay"]
tool_timeout_sec = 3660
```

Codex does not consume Claude's experimental Channels notification. Delivery remains durable and
works through `inbox()`, `wait()`, `ask()`, and plugin standby; leave `SWITCHBOARD_PUSH` off for
Codex-only sessions.

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
- Claude Desktop can't self‑react via Channels (no launch flag), but it *can* be a turn‑injection
  reactor via [`ccd_session_mgmt`](#reacting-on-claude-desktop-ccd_session_mgmt) with one approval
  click per message (`SWITCHBOARD_CCD_INJECT=1`). Otherwise it uses the durable tools and polls.

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
      "mcp__switchboard-relay__unregister",
      "mcp__switchboard-relay__standby",
      "mcp__switchboard-relay__bootstrap",
      "mcp__switchboard-relay__charter"
    ]
  }
}
```

</details>

---

## Tools

The tools, grouped by what you reach for:

**Presence** — join and see who's around

| Tool | Signature | What it does |
|------|-----------|--------------|
| `register` | `register(name?, role?)` | Claim or change this session's address. A session is already registered automatically under a unique generated address when it connects, so call this to pick an explicit `name` (e.g. `"lead"`). Explicit names are protected: if another live session holds the name, the call fails instead of sharing its mailbox. Registering a new name renames this session and drops the old one. Mail already queued for the old address stays queued under that address; it isn't moved. `role` is an optional shared group address. Returns the `board` you joined and the live participants. |
| `participants` | `participants()` | List the live sessions: `name`, `role`, `idle_seconds`. A session stays listed for as long as its process is connected, whatever it's busy with; `idle_seconds` separately reports how long it's been since that peer last *used* switchboard, so a quiet peer reads as live-but-idle rather than dropping off the list. |
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
| `wait` | `wait(timeout_s?)` | Block up to `timeout_s` seconds (default 30, max 3600) until a message arrives, then drain and return it. Returns `timed_out: true` on timeout. In Claude Code, keep `timeout_s` under 1800: Claude Code aborts an MCP call that sends nothing for 30 minutes. |

**Context** — what the board expects of this session

| Tool | Signature | What it does |
|------|-----------|--------------|
| `bootstrap` | `bootstrap()` | Return this session's full context: `you`, `board`, `protocol_version`, the board's charter (`charter_revision`, text, and `coordinator`), this session's `listen_status`, and each peer's `name`, `client`, `ready`, `agent_bootstrapped_revision`, and `listen_status`. Calling it records that this session's agent has read the current charter. |
| `charter` | `charter()` | Read the board's current charter: the messaging conventions every session on the board is expected to follow. |
| `set_charter` | `set_charter(text, expected_revision)` | Replace the charter. Only the board's coordinator can call it, and only when `expected_revision` matches the current revision, so two edits can't overwrite each other. |

**Listening between turns** — keep a session reachable after its turn ends

| Tool | Signature | What it does |
|------|-----------|--------------|
| `standby` | `standby(enabled?)` | Turn listening on or off for this session. It's on by default (unless `SWITCHBOARD_AUTO_LISTEN=0`), and `standby(false)` stays off for the rest of the session. Listening needs the host's trusted `Stop` hook; without one the session is `polling-only`. |
| `host_standby` | `host_standby(timeout_s?)` | Internal target of the Codex and Claude Code `Stop` hooks. It peeks for mail without draining it and returns the hook's continuation decision. Agents should call `standby()`, not this. |
| `codex_standby` | `codex_standby(timeout_s?)` | Older name for `host_standby`, kept so existing Codex hook installs keep working. |

**Ask** — send and block for the answer in one call

| Tool | Signature | What it does |
|------|-----------|--------------|
| `ask` | `ask(to, body, timeout_s?)` | Send `body` to `to`, then block until a reply threaded to it comes back (`reply_to` = the returned `question_id`). Leaves other inbox messages untouched; returns `timed_out: true` if no reply in time — with `no_live_recipient: true` when nobody was registered as `to` at send time, so you can tell a wrong address from a slow reply. |

> **Readiness.** A peer's `ready` flag means its agent called `bootstrap()` (or `charter()`) at the
> current charter revision. It does not mean the peer agrees to the charter or will follow it.
> Peers running an older switchboard are listed as `legacy/manual` and are never `ready`.
>
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
2. **The current project** *(the default)* — keyed off the git repo. `SWITCHBOARD_PROJECT_DIR` is the
   client-neutral explicit project root; Claude Code's `CLAUDE_PROJECT_DIR` remains supported for
   compatibility, and Codex or other MCP hosts fall back to the server launch directory. The
   resulting board name looks like `myrepo-3f9c1a`.

`register()` returns the `board` you joined, so a session can always see which switchboard it's on.
Each board is its own SQLite file under `~/.claude/switchboard/<board>.db`. (Setting `SWITCHBOARD_DB`
to a raw path still overrides everything — handy for pointing several sessions at one exact file.)

### Sharing a board across projects

Want the classic cross‑repo setup where a **worker** in repo A asks a **lead** in repo B? Put both
sessions on the same named board:

```bash
# lead, in repo B
claude mcp add --scope user --env SWITCHBOARD_BOARD=team -- switchboard-relay
# Codex worker, in repo A — same board name and the same local SQLite bus
codex mcp add switchboard_relay --env SWITCHBOARD_BOARD=team -- switchboard-relay
```

Any shared string works; pick one name and use it everywhere those sessions should talk.

> **Upgrading from ≤ 0.2?** The default used to be a single global board (`~/.claude/switchboard.db`).
> It's now per‑project. To get the old global behavior back, set `SWITCHBOARD_BOARD` to a shared name
> (as above), or point `SWITCHBOARD_DB` at the old file.

---

## Connecting: identity, peers, and the charter

A session doesn't need a setup prompt to join a board. When a Codex, Claude Code, or other MCP
session connects, its switchboard process:

1. **Claims an address.** The session is registered under a unique generated address, or under
   `SWITCHBOARD_NAME` if set. Call `register(name="lead")` to pick an explicit name. Explicit names
   are protected: while another live session holds a name, nobody else can take it.
2. **Discovers peers.** Peers are read from the board's registry, so joining sends no messages.
   Tool results carry the current peers and charter revision, and `bootstrap()` returns the full
   picture.
3. **Delivers the charter.** The charter is the board's shared messaging conventions, such as how
   to tag a task or a result and when to thread replies. A board starts with the default charter
   shipped with switchboard, and a board whose charter nobody has edited picks up a newer
   release's default. Every tool result carries a `context` entry, in both the structured result
   and a `Switchboard context` text block. It holds the charter revision and the coordinator. The
   full peer list is included only when it has changed, and the full charter text only the first
   time and whenever the revision changes. Otherwise the entry is a short summary.

   Each peer entry reports `client`, `protocol_version`, `ready`, `agent_bootstrapped_revision`,
   and `listen_status`. A peer running an older switchboard appears with `client`
   `legacy/manual`, a `protocol_version` of `null`, and `ready` false. It never receives the
   charter, so send it the conventions as an ordinary message if it needs them.

**Who edits the charter.** The first session on a board becomes its **coordinator**, the only
session that can change the charter with `set_charter()`. If the coordinator exits, or its lease
lapses (180 seconds without a heartbeat), the oldest live session takes over automatically. A
coordinator that comes back doesn't reclaim the role. The coordinator owns the charter only: it
doesn't assign work, and no session is elected to lead or delegate. A human can transfer the role
from a terminal:

```bash
switchboard-relay charter                                          # print charter, revision, coordinator, peers (JSON)
switchboard-relay charter --coordinator lead --expected-revision 3 # hand the charter to "lead"
switchboard-relay charter --coordinator lead --expected-revision 3 --expected-generation 7
                                                                   # …only if nobody else changed the coordinator first
```

**What the charter can't do.** It's text written by a peer agent, not by you. It can set
conventions, but it never grants one session authority over another, and your own instructions
to a session always win. A message from a peer is not your authorization either: a session acts on
peer mail only within the task you've already given it.

Set `SWITCHBOARD_AUTO=0` to turn off automatic identity, discovery, and listening and go back to
explicit `register()` calls.

---

## The lead / worker pattern

Within one project this works with **zero board configuration** — every session in the repo shares
the project's board automatically. For a lead and workers spread across *different* repos, first put
them on a shared board (see [Boards](#boards-one-switchboard-per-project)).

### The lead (coordinator)

Keep one session open as the long‑running lead. With the Codex or Claude Code plugin's `Stop` hook
in place (and, in Codex, trusted via `/hooks`), paste this and let it run:

```
Register me on switchboard-relay as "lead" and answer every message that wakes
this task. Reply to its sender using reply_to, then finish normally so listening
resumes. Keep going until I tell you to call standby(false).
```

The client renames itself to `lead` and parks in the `Stop` hook whenever its turn finishes;
listening is already on. A lead keeps a well-known name so workers can address it; anyone who
doesn't need a fixed address can keep the generated one. Without a `Stop` hook, ask the task to keep
calling `wait()`; in Claude Code, keep that polling loop going hands-free with the
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

The worker registers under an explicit or assigned name and calls `ask("lead", "…")` — one call
that sends the question and blocks for the answer. The lead's loop picks it up, replies with
`reply_to` set, and the worker's `ask()` returns the reply. No window-switching or manual polling.

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
switchboard-relay charter                        # the board's charter, revision, and coordinator
switchboard-relay participants --board team      # …a specific board instead
```

**`doctor`** is the one-shot diagnostic: it prints which board you resolved to (and *how* — `--board`,
`$SWITCHBOARD_BOARD`, project-derived, …), the relevant env vars, live participants, the queued-message
count, and a plain-English hint when something looks off (you're alone on a board, or messages are
piling up against a name nobody reads).

---

## Configuration

All optional. Set as environment variables through `codex mcp add --env KEY=value` or
`claude mcp add --env KEY=value`:

| Variable | Default | Meaning |
|----------|---------|---------|
| `SWITCHBOARD_BOARD` | *(project)* | Board to join. An explicit name (any string) puts these sessions on a shared bus; `project` forces per‑project derivation. See [Boards](#boards-one-switchboard-per-project). |
| `SWITCHBOARD_DB` | *(the board's file)* | Raw SQLite path override — wins over `SWITCHBOARD_BOARD`. Point several sessions at one exact file to share it. |
| `SWITCHBOARD_PROJECT_DIR` | *(server launch directory)* | Client-neutral project root used to derive the default board. Wins over the compatibility-only `CLAUDE_PROJECT_DIR`. |
| `SWITCHBOARD_TTL` | `1800` (30 min) | Seconds without a sign of life before a participant drops out of `participants()` — and stops receiving `broadcast()`. A connected session keeps itself alive automatically, so in practice this is the window for noticing a session whose *process* went away without a clean `unregister()`. |
| `SWITCHBOARD_MSG_TTL` | `604800` (7 days) | Undelivered messages older than this are pruned automatically during normal operation. Set `0` to disable age‑out. |
| `SWITCHBOARD_MAX_BODY` | `262144` (256 KiB) | Reject a `send()` whose body exceeds this many UTF‑8 bytes. Set `0` to disable the cap. |
| `SWITCHBOARD_AUTO` | `1` | Claim an address, discover peers, deliver the charter, and listen between turns automatically when a session connects. Set `0` to require an explicit `register()` and turn listening off. |
| `SWITCHBOARD_AUTO_LISTEN` | `1` | Start each session with listening between turns on. Set `0` to start with it off; `standby(true)` still turns it on. |
| `SWITCHBOARD_NAME` | — | Auto‑register this session under this address instead of a generated one (also the fallback when `register()` is called without a name). It's protected like any explicit name: if another live session already holds it, this session doesn't join, and its tool calls report `name_in_use` until you `register()` a different name. |
| `SWITCHBOARD_ROLE` | — | Role to pair with `SWITCHBOARD_NAME`. |
| `SWITCHBOARD_PUSH` | `0` | Enable [turn‑injection push](#turn-injection-push) over Channels (CLI reactors) — has the background loop poll the board and nudge this session's own client. Off by default (a small background poll cost); set `1` to enable. The loop itself always runs: with push off it only does the once‑a‑minute liveness keepalive. |
| `SWITCHBOARD_CCD_INJECT` | `0` | Enable [turn injection on Claude Desktop](#reacting-on-claude-desktop-ccd_session_mgmt): `send()` returns an `inject` hint so the sender's Claude can call `ccd_session_mgmt.send_message`. Off by default (leans on a Desktop tool + a per‑message approval). |
| `SWITCHBOARD_CCD_SESSION_ID` | *(`local_<CLAUDE_CODE_SESSION_ID>`)* | Override this session's CCD id used for Desktop injection. Needed only when the default derivation is wrong (e.g. an agent/child session). Used verbatim. |

Example — a longer liveness window, for peers whose process may outlive their attention:

```bash
claude mcp add --scope user --env SWITCHBOARD_TTL=3600 -- switchboard-relay
```

---

## How it works

Each local MCP session spawns its **own** `switchboard-relay` process (STDIO transport). Those
processes do not talk to each other directly — Codex and Claude Code processes share state through
the same SQLite database (one file per [board](#boards-one-switchboard-per-project)). `wait()`
long-polls that database.

**The one honest limitation:** switchboard **cannot wake a *closed* session or a client that is no
longer running.** In the baseline poll model a recipient learns about a message by calling
`inbox()` or `wait()` on its next turn or while parked in a polling loop. The Codex and Claude Code
`Stop` hooks keep an open session parked between turns; an open Claude Code session can also be
pushed into a turn with [turn injection](#turn-injection-push). Generic MCP clients are
polling-only. Each session's `listen_status` reports which applies. Durable mail remains queued when
no listener is active.

Delivery is **at most once**: each message is drained by one reader. If a client gives up on a
`wait()` that has already drained a message (for example, Claude Code aborting a call that ran past
its idle limit), that message can be lost. Keep waits under the host's limit.

Boards are SQLite files under your home directory, so "same machine" really means the same OS user
on one machine. There is no network transport.

---

## Turn injection (push)

By default a recipient only learns about a message when *it* next polls (`inbox()`/`wait()`).
**Turn injection** removes that wait: a `send()` makes the recipient's open session **react on the
spot** — the message arrives *as a turn*, and the session drains its inbox and acts, with no manual
poll. It's the "responsive‑lead" setup — a lead that answers the instant a worker asks.

There are **two mechanisms**, by recipient surface — set up whichever matches where your *reactor*
runs (the *sender* can be any surface):

| Reactor runs on… | Mechanism | Feel |
|---|---|---|
| **Codex (desktop/CLI/IDE)** | Plugin `Stop` hook (on by default once trusted), or `wait()` / `inbox()` polling | **automatic while the task and client remain open** |
| **Claude Code (any surface)** | Plugin `Stop` hook (on by default) | **automatic while the session remains open**, with about four short re-arm turns an hour when idle |
| **Claude Code (CLI/terminal)** | [Channels](https://code.claude.com/docs/en/channels) — the recipient self‑injects | **zero‑touch**, fully automatic |
| **Claude Desktop** | `ccd_session_mgmt.send_message` — the *sender* injects | **one approval click per message** |

The lifecycle and Claude mechanisms are accelerators on top of the durable polling path and preserve
**drain-once**. Codex listening requires the plugin hook to be reviewed and trusted with `/hooks`.

### Reacting on the CLI (Channels) — the three hard constraints

Turn injection over Channels works **only** when all three hold. Miss any one and delivery silently
falls back to the durable poll — nothing breaks, the message just waits in the inbox as usual:

1. **Push is enabled.** It's a background convenience with a small polling cost, so it's **off by
   default** — set `SWITCHBOARD_PUSH=1` to turn it on.
2. **The recipient session is open.** Channels inject into a *running* session on its next turn.
   Nothing can wake a fully idle or closed session — see [How it works](#how-it-works).
3. **The recipient subscribed to switchboard as a channel** — launched with the channel flag below.
   A session that connected normally still works; it just polls instead of reacting.

### Setup

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

### Reacting on Claude Desktop (`ccd_session_mgmt`)

Claude Desktop has no channel launch flag, so a Desktop session can't self‑inject the way a CLI
session does. But Desktop exposes a built‑in `ccd_session_mgmt.send_message` tool that injects a turn
into another session — so switchboard can **broker** it: it can't call that tool itself (an MCP
server can't invoke another server's tools), but it can hand the *sender's* Claude everything needed
to make the call.

```bash
# Register the stdio server with Desktop injection enabled:
claude mcp add --scope user --env SWITCHBOARD_CCD_INJECT=1 -- switchboard-relay
```

How it flows: each session's switchboard **auto‑captures its own** CCD id at `register()`
(`local_<CLAUDE_CODE_SESSION_ID>`, which Claude Code puts in the server's env) and stores it on the
board. When you `send(to=X)`, the result carries an `inject` field with X's `session_id` and a
ready‑to‑send message; the sender's Claude then calls `ccd_session_mgmt.send_message(...)` and X
reacts. No session ever discloses its id to another — each records only its own.

**The honest caveats:**

- **One approval click per message.** `send_message` is on a hardcoded list in the Desktop client
  (alongside `AskUserQuestion`/`ExitPlanMode`) that **always** prompts you to approve — it's the
  guardrail against one session silently driving another, and there is **no setting to disable it**.
- **The sender must be a Desktop/Cowork session** (only those have the `ccd_session_mgmt` tools). The
  *reactor* is Desktop; a pure terminal `claude` can't be the injecting sender. So this is the mirror
  image of the CLI path.
- **`ask()` isn't covered** — it blocks the sender while waiting, so the sender can't inject mid‑call.
  Use `send()` for a Desktop reactor (it replies with its own `send()`), or make the reactor a CLI
  session.
- **The CCD id derivation** (`local_<CLAUDE_CODE_SESSION_ID>`) holds for a normal top‑level session;
  in an **agent/child** context the env id isn't the addressable one, so set
  `SWITCHBOARD_CCD_SESSION_ID=<full id>` to override. Confirm once with the two‑session check below.
- Best‑effort and **idempotent**: the injected turn is a body‑less nudge (the real message stays in
  the durable inbox and is drained by one reader), so even if a recipient *also* has the CLI watcher, a
  double nudge can't double‑deliver.

### What's actually sent (and why it stays drain‑once)

On the CLI path, switchboard emits a `notifications/claude/channel` notification that Claude Code
wraps into the recipient's next turn as `<channel source="switchboard-relay" msg_from="…"
msg_id="…">…</channel>`. Each session's own watcher polls the board for messages addressed to
*itself* and self‑nudges; the cross‑session hop is the shared SQLite board.

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

The research‑preview reaction can't be unit‑tested, so confirm it by hand once:

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
switchboard cannot wake a closed session. Check the recipient's `listen_status` with `bootstrap()`.
With the Codex plugin installed, review its hook with `/hooks`; until then the session reports
`configured`, not `armed`. A session that called `standby(false)` stays off until `standby(true)`. A
direct MCP installation needs the `Stop` entry shown under [Install](#install), with `server` set to
the name you installed the server under, or a requested `wait()` loop. Generic MCP clients are
`polling-only`. (See [How it works](#how-it-works).)

**"My `wait()` failed with 'sent no response or progress for 1800s'."**
Claude Code aborts an MCP call that stays silent for 30 minutes. Use a `timeout_s` under 1800, or
raise the limit with the server's `timeout` setting or `CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT`.

**"The other session can't see me / `participants()` is empty."**
You're probably on different **boards** — each project gets its own by default. Run
`switchboard-relay boards` to list them, and make sure both sessions are on the same one: the same
repo, or the same `SWITCHBOARD_BOARD`. (See [Boards](#boards-one-switchboard-per-project).)

**"`ask()` timed out, or a `send()` came back with `no_live_recipient`."**
Nobody is registered under that name/role right now — usually a typo (`"leed"` vs `"lead"`) or the
recipient's session is closed. A session that's merely *busy* still counts as live, so this isn't
what you'll see from a peer that's deep in a build or a CI wait; check live addresses with
`participants()` or `switchboard-relay participants`. The message is still queued durably, so a
correctly‑named recipient gets it later — and `ask()` can simply be timing out because the peer
hasn't reached its next turn yet (`idle_seconds` tells you how long it's been away).

**"My session has a generated name I didn't choose."**
Every session gets a unique generated address when it connects. That's fine for a worker that
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
- [`server.py`](src/switchboard_relay/server.py) — the MCP server: identity binding, the tools, polling/standby loops, and best‑effort push.

Tests are split into three tiers:

- `tests/unit/` — the SQLite store and board resolution in isolation.
- `tests/feature/` — tool behavior through the server layer (identity, push, roles, hygiene bounds, the CLI) with a fake Context.
- `tests/integration/` — the tools over a real MCP transport, including **two real stdio subprocesses**, plus an **N‑process drain‑once stress test** that reconciles sent‑vs‑received ids under contention (including shared‑role drains).

The Claude Code plugin/marketplace manifests live in [`.claude-plugin/`](.claude-plugin/); validate
them with `claude plugin validate .`. Codex packaging lives in
[`.codex-plugin/`](.codex-plugin/), [`.mcp.json`](.mcp.json), and
[`hooks/hooks.json`](hooks/hooks.json), and is validated with the Codex plugin validator.

CI (Python 3.10–3.14) runs ruff + the full suite with the coverage gate on every push and PR. Releases
and Homebrew packaging are documented in [RELEASING.md](RELEASING.md).

## Non‑goals (v1)

Single machine only (no cross‑machine bus), no auth/encryption, no transcript search, no GUI.

## License

[MIT](LICENSE)
