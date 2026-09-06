"""Fixtures every test file may use, and the one thing that must happen before
any test module is even imported: this machine's corpus samples.
"""
from __future__ import annotations

import os
import textwrap
import tomllib
from pathlib import Path

import pytest

from dayz_mcp.tools import client, session

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


@pytest.fixture(autouse=True)
def clean_session():
    """No test may inherit -- or leak -- the process-wide session.

    One MCP server process holds one open project, one tracked server pid and
    one tracked client pid, so the session is a module singleton by design. In
    a test suite that makes it shared mutable state: a pid recorded by one file
    is a pid the next file's `client_*`/`server_*` call will happily act on,
    and the symptom is a failure in a test that never touched a process.

    Eleven files each defended against that by hand -- 162 `session.reset()`
    calls written into test bodies, which protects only the test that
    remembers to write one. Autouse, before AND after, protects every test
    including the ones written next year. `client._start_in_flight` is
    process-global for the same reason (one client profile directory, one
    machine) and is cleared with it, or a test that deliberately leaves a
    start in flight refuses every later one in the process.
    """
    session.reset()
    client._start_in_flight.update(job_id="", store=None)
    yield
    session.reset()
    client._start_in_flight.update(job_id="", store=None)


# --------------------------------------------------------------- one project
# The stand-and-game scaffolding four files build. It was one file's private
# helper until that file was split by tool family, and copying it four ways to
# make the split possible would have been the split's own first defect.

PROFILE = """
[project]
name = "my-mod"

[build]
mods = ["MyMod"]

[expect]
ready_line = "[MyMod] loaded"
forbid = ["Bad type"]

[expect.counters]
items = 12
"""


@pytest.fixture(autouse=True)
def _no_real_ports(monkeypatch):
    """Nothing in this file may consult the machine's actual network state.

    server_start now checks the game port before spawning, and that check reads
    netstat. Without this, twelve tests started failing the moment ANOTHER
    AGENT's live stand bound udp/2302 on this machine -- tests that had passed
    minutes earlier, for a reason nothing in them could express. A unit test
    that reads global machine state is flaky by construction, and this is a
    repository where a second stand really does come and go.

    The default is "nothing holds any port"; the tests that are about the port
    override it explicitly, which also makes them the only place the reader has
    to look for that behaviour.
    """
    monkeypatch.setattr("dayz_mcp.tools.lifecycle.udp_port_holders", lambda port: [])


PROFILE_WITHOUT_READY_LINE = """
[project]
name = "my-mod"

[build]
mods = ["MyMod"]

[expect]
forbid = ["Bad type"]
"""


def make_project(tmp_path: Path, profile_text: str = PROFILE) -> Path:
    (tmp_path / "dayz-mcp.toml").write_text(textwrap.dedent(profile_text), encoding="utf-8")
    (tmp_path / "MyMod").mkdir()
    (tmp_path / "MyMod" / "config.cpp").write_text("", encoding="utf-8")
    return tmp_path


def with_stand(root: Path, stand: Path, log_text: str) -> None:
    (stand / "profiles").mkdir(parents=True, exist_ok=True)
    (stand / "profiles" / "script_1.log").write_text(log_text, encoding="utf-8")
    (root / "dayz-mcp.local.toml").write_text(
        f'[machine]\nstand_root = "{stand.as_posix()}"\n', encoding="utf-8"
    )


def with_stand_and_game(
    root: Path,
    stand: Path,
    game_dir: Path,
    *,
    port: int | None = None,
    extra_mods: list[str] | None = None,
    server_only: list[str] | None = None,
    config: str | None = None,
    server_dir: Path | None = None,
    dedicated_image: bool = True,
) -> None:
    """Like with_stand, but also fabricates a fake game install so server_start's
    `find_game` succeeds deterministically, regardless of what is actually
    installed on the machine running the tests.

    `server_dir` additionally writes machine.server and fabricates a dedicated
    install there. `dedicated_image=False` creates the directory WITHOUT the
    server executable, which is the shape server_start must refuse."""
    (stand / "profiles").mkdir(parents=True, exist_ok=True)
    game_dir.mkdir(parents=True, exist_ok=True)
    (game_dir / "DayZDiag_x64.exe").write_bytes(b"")

    lines = ["[machine]", f'stand_root = "{stand.as_posix()}"', f'game = "{game_dir.as_posix()}"']
    if server_dir is not None:
        server_dir.mkdir(parents=True, exist_ok=True)
        if dedicated_image:
            (server_dir / "DayZServer_x64.exe").write_bytes(b"")
        lines.append(f'server = "{server_dir.as_posix()}"')
    if port is not None:
        lines.append(f"port = {port}")
    if config is not None:
        lines.append(f'config = "{config}"')
    if extra_mods or server_only:
        lines.append("")
        lines.append("[mods]")
        if extra_mods:
            items = ", ".join(f'"{x}"' for x in extra_mods)
            lines.append(f"extra = [{items}]")
        if server_only:
            items = ", ".join(f'"{x}"' for x in server_only)
            lines.append(f"server_only = [{items}]")
    (root / "dayz-mcp.local.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")
