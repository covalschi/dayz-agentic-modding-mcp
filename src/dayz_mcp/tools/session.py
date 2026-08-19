"""One open project per server process, held here so every tool sees the same one."""
from __future__ import annotations

from pathlib import Path

from ..jobs import JobStore
from ..procs import is_alive
from ..profile import Profile

_state: dict = {"profile": None, "jobs": None, "game": None, "tools": None, "server_pid": 0}


def reset() -> None:
    _state.update({"profile": None, "jobs": None, "game": None, "tools": None, "server_pid": 0})


def set_project(profile: Profile, game: str | None, tools_root: str | None) -> dict:
    """Point the session at a new project.

    A pid recorded by a previous project is never carried over: if it were, the
    new project's server_start would wrongly refuse as "already running", and
    its server_stop would kill whatever process now holds that pid -- which, by
    the time this session gets around to it, is not necessarily even the same
    program anymore. If that previous server is still alive, it is left running
    (killing it silently on a project switch would be its own surprise) and its
    pid is returned here so project_open can tell the caller it is now
    orphaned from this session's tracking.
    """
    prev_pid = int(_state["server_pid"] or 0)
    orphaned_server_pid = prev_pid if prev_pid and is_alive(prev_pid) else 0

    _state["profile"] = profile
    _state["game"] = game
    _state["tools"] = tools_root
    _state["server_pid"] = 0
    store = JobStore(Path(profile.root) / ".dayz-mcp" / "jobs")
    store.load()
    _state["jobs"] = store
    return {"orphaned_server_pid": orphaned_server_pid}


def profile() -> Profile | None:
    return _state["profile"]


def jobs() -> JobStore | None:
    return _state["jobs"]


def game() -> str | None:
    return _state["game"]


def tools_root() -> str | None:
    return _state["tools"]


def server_pid() -> int:
    return int(_state["server_pid"] or 0)


def set_server_pid(pid: int) -> None:
    _state["server_pid"] = pid
