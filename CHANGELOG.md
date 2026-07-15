# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

First release. Everything below ships in `0.1.0` when the first `v0.1.0` tag is cut.

### Added

- Local MCP server giving independent Claude Code sessions a shared, durable messaging
  channel over SQLite (`~/.claude/switchboard.db`, override `$SWITCHBOARD_DB`).
- Tools: `register`, `participants`, `send`, `inbox`, `wait`, `ask` (one‑call
  request/response), `broadcast`, and `unregister`.
- Durable mailboxes with exactly‑once drain; a participant registry with TTL liveness;
  name‑ and role‑based addressing.
- stdio transport (default, one process per session, shared via SQLite) and an experimental
  streamable‑HTTP daemon (`switchboard serve`) with opt‑in Channels push (`SWITCHBOARD_PUSH`).
- Human‑facing inspection CLI: `switchboard participants` and `switchboard tail [--follow]`.
- Environment configuration: `SWITCHBOARD_DB`, `SWITCHBOARD_TTL`, `SWITCHBOARD_NAME`,
  `SWITCHBOARD_ROLE`, `SWITCHBOARD_PUSH`, `SWITCHBOARD_HOST`, `SWITCHBOARD_PORT`.
- Tooling: ruff lint + format, pytest with an enforced coverage gate (100% today), CI on
  Python 3.10–3.14, PyPI trusted‑publishing release workflow, and a Homebrew tap formula.

[Unreleased]: https://github.com/mgd43b/switchboard/commits/main
