"""Shared test configuration.

Tests are organized into three tiers by directory:

* ``tests/unit``        — pure component tests (the SQLite store in isolation).
* ``tests/feature``     — tool/feature behavior through the server layer with a
                          lightweight fake Context (no transport).
* ``tests/integration`` — the tools driven over a real MCP transport, including
                          cross-process stdio.

Each test is auto-marked with its tier based on its path, so you can select a
tier with e.g. ``pytest -m unit`` without decorating every test by hand.
"""

from __future__ import annotations

import os

import pytest

_TIERS = ("unit", "feature", "integration")


def pytest_collection_modifyitems(config, items):
    for item in items:
        path = str(item.path)
        for tier in _TIERS:
            if f"{os.sep}tests{os.sep}{tier}{os.sep}" in path:
                item.add_marker(getattr(pytest.mark, tier))
                break
