# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.1](https://github.com/mgd43b/switchboard-relay/compare/v0.1.0...v0.1.1) (2026-07-15)


### Documentation

* fix uvx invocation (command is 'switchboard', dist is 'switchboard-relay') ([#7](https://github.com/mgd43b/switchboard-relay/issues/7)) ([a5f56f1](https://github.com/mgd43b/switchboard-relay/commit/a5f56f1cc19e66d7b15d6b519c2f559f63f7d421))

## [0.1.0](https://github.com/mgd43b/switchboard-relay/releases/tag/v0.1.0) (2026-07-15)

Initial release.

### Added

- Local MCP server giving independent Claude Code sessions a shared, durable messaging
  channel over SQLite (`~/.claude/switchboard.db`, override `$SWITCHBOARD_DB`).
- Tools: `register`, `participants`, `send`, `inbox`, `wait`, `ask` (one‑call
  request/response), `broadcast`, and `unregister`.
- Durable mailboxes with exactly‑once drain; a participant registry with TTL liveness;
  name‑ and role‑based addressing.
- stdio transport (default, one process per session, shared via SQLite) and an experimental
  streamable‑HTTP daemon (`switchboard serve`) with opt‑in Channels push (`SWITCHBOARD_PUSH`).
- Human‑facing inspection CLI: `switchboard participants`, `switchboard tail [--follow]`, and
  `switchboard prune` (delete old dead‑letter messages and expired participants).
- Environment configuration: `SWITCHBOARD_DB`, `SWITCHBOARD_TTL`, `SWITCHBOARD_NAME`,
  `SWITCHBOARD_ROLE`, `SWITCHBOARD_PUSH`, `SWITCHBOARD_HOST`, `SWITCHBOARD_PORT`.
- Tooling: ruff lint + format, pytest with an enforced coverage gate (100% today), CI on
  Python 3.10–3.14, PyPI trusted‑publishing release workflow, and a Homebrew tap formula.
