"""One open project per server process, held here so every tool sees the same one."""
from __future__ import annotations

from pathlib import Path

from ..jobs import JobStore
from ..profile import Profile

_state: dict = {"profile": None, "jobs": None, "game": None, "tools": None, "server_pid": 0}


def reset() -> None:
    _state.update({"profile": None, "jobs": None, "game": None, "tools": None, "server_pid": 0})


def set_project(profile: Profile, game: str | None, tools_root: str | None) -> None:
    _state["profile"] = profile
    _state["game"] = game
    _state["tools"] = tools_root
    store = JobStore(Path(profile.root) / ".dayz-mcp" / "jobs")
    store.load()
    _state["jobs"] = store


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
