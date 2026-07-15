# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.2](https://github.com/mgd43b/switchboard-relay/compare/v0.2.1...v0.2.2) (2026-07-15)


### Features

* doctor diagnostics, message hygiene, hardened push, and a Claude Code plugin ([#21](https://github.com/mgd43b/switchboard-relay/issues/21)) ([8cddc52](https://github.com/mgd43b/switchboard-relay/commit/8cddc52607760380317d11af08c9740e29d494f5))

## [0.2.1](https://github.com/mgd43b/switchboard-relay/compare/v0.2.0...v0.2.1) (2026-07-15)


### Features

* per-project boards, optional register name, and friendlier UX ([#14](https://github.com/mgd43b/switchboard-relay/issues/14)) ([5aa7622](https://github.com/mgd43b/switchboard-relay/commit/5aa76226096748d7ce5b0f0d3e0f4bd4b6ceed1f))

## [0.2.0](https://github.com/mgd43b/switchboard-relay/compare/v0.1.1...v0.2.0) (2026-07-15)


### ⚠ BREAKING CHANGES

* the console command is now `switchboard-relay` (was `switchboard`); anyone who installed 0.1.x gets a renamed command on upgrade.

### Features

* rename CLI command and module to switchboard-relay ([#12](https://github.com/mgd43b/switchboard-relay/issues/12)) ([0e817dc](https://github.com/mgd43b/switchboard-relay/commit/0e817dc7346307ceef38db8c5f3ecbe35396a87b))

## [0.1.1](https://github.com/mgd43b/switchboard-relay/compare/v0.1.0...v0.1.1) (2026-07-15)


### Miscellaneous Chores

* **deps:** bump the github-actions group across 1 directory with 3 updates ([#5](https://github.com/mgd43b/switchboard-relay/issues/5)) ([9d8399e](https://github.com/mgd43b/switchboard-relay/commit/9d8399e91e00f1847d0483094124755fa255d33f))

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
