"""Fixtures every test file may use, and the one thing that must happen before
any test module is even imported: this machine's corpus samples.
"""
from __future__ import annotations

import os
import tomllib
from pathlib import Path

import pytest

#: Machine-specific paths to the real artifacts the corpus tests read. Never
#: committed: a path out of one developer's disk is a fact about that disk, and
#: the moment it is in the repository it is wrong for everyone else.
#: `samples.local.example.toml` (committed) says what may go in it.
SAMPLES_LOCAL = Path(__file__).with_name("samples.local.toml")


def _export_local_samples() -> None:
    """Put this machine's sample paths into the environment.

    At MODULE level, not in a fixture, and deliberately so: the corpus tests
    gate with `pytest.mark.skipif`, evaluated while their own module is
    imported. conftest is imported before any of them; a fixture would run long
    after every skip decision had already been made.

    An existing environment variable always wins, so `DAYZ_MCP_SAMPLE_ODOL=...
    pytest` still overrides the file for one run. A missing file is the
    ordinary state of a fresh clone and means what it always meant: every
    corpus test skips and the hermetic half still runs.
    """
    try:
        raw = tomllib.loads(SAMPLES_LOCAL.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return
    for name, value in raw.items():
        if isinstance(value, str) and value and not os.environ.get(name):
            os.environ[name] = value


_export_local_samples()


@pytest.fixture
def anyio_backend():
    """Run pytest.mark.anyio tests on asyncio -- the only backend this project
    (and the FastMCP server it tests) actually uses."""
    return "asyncio"
