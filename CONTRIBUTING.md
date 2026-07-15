# Contributing to switchboard

Thanks for your interest! switchboard aims to stay small, dependency‑light, and well‑tested.

## Setup

```bash
uv venv && uv pip install -e '.[dev]'
pre-commit install        # optional: run ruff automatically on commit
```

## Before you push

The CI gate is: **ruff (lint + format) clean**, **all tests pass**, and **coverage ≥ 95%**.
Run the same checks locally:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest             # coverage gate enforced via pyproject
```

## Tests

Tests live in three tiers (see [tests/conftest.py](tests/conftest.py)); pick the right one for
your change and keep coverage up:

- `tests/unit/` — pure logic (the SQLite store), time injected, no I/O beyond a temp DB.
- `tests/feature/` — tool/feature behavior through the `Switchboard` layer with a fake Context.
- `tests/integration/` — the tools over a real MCP transport (in‑memory and real stdio subprocesses).

Run one tier with `uv run pytest -m unit` (or `feature` / `integration`).

## Design guardrails

- **`store.py` stays pure**: no clocks, no sleeps, no MCP imports. Time is passed in as `now`;
  TTL is a parameter. This is what keeps it deterministically testable.
- **Durability is the source of truth**: every message is a SQLite row delivered exactly once.
  Push (daemon mode) is only ever a best‑effort *nudge* on top — it must never be required for
  correctness.
- **Multi‑process safety**: many independent processes share one DB file (WAL). Writes that must
  be atomic use `BEGIN IMMEDIATE` or a single `ON CONFLICT` statement.

## Releasing

Maintainers: see [RELEASING.md](RELEASING.md).
