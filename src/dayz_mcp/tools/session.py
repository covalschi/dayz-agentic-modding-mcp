"""One open project per server process, held here so every tool sees the same one."""
from __future__ import annotations

from pathlib import Path

from ..jobs import JobStore
from ..procs import is_alive
from ..profile import Profile

_state: dict = {
    "profile": None, "jobs": None, "game": None, "tools": None,
    "server_pid": 0, "known_pids": set(),
}


def reset() -> None:
    _state.update({
        "profile": None, "jobs": None, "game": None, "tools": None,
        "server_pid": 0, "known_pids": set(),
    })


def set_project(profile: Profile, game: str | None, tools_root: str | None) -> dict:
    """Point the session at a project.

    Reopening the SAME root (e.g. after editing dayz-mcp.local.toml) is routine
    and must not disturb a server this session already has running: the pid is
    kept exactly as-is. Only an actual project switch clears it -- if it were
    carried over to an unrelated project, that project's server_start would
    wrongly refuse as "already running", and its server_stop would kill
    whatever process now holds that pid, which by the time this session gets
    around to it is not necessarily even the same program anymore. Both roots
    are resolved to absolute paths before comparing, so "." and an absolute
    path to the same directory count as the same project.

    On an actual switch, a still-alive previous pid is left running (killing it
    silently on a project switch would be its own surprise) and is returned
    here as `orphaned_server_pid` so project_open can tell the caller. It stays
    in `known_pids` so a later `server_stop(pid=...)` can still be used to deal
    with it -- see `known_pid`.

    The same reuse-on-reopen rule applies to the job store. JobStore.load()
    deliberately flips any job it finds recorded as "running" to "failed"
    ("lost: the server restarted..."), which is correct after a genuine
    process restart -- the worker thread that owned it is truly gone -- but
    wrong on a same-project reopen, where that worker thread is still running
    in this very process and will go on writing to whatever store instance it
    was handed. Building a second store and load()-ing it on a reopen would
    silently fork the job's history: the fresh store's in-memory copy gets
    permanently marked "failed" while the real worker's store (still the one
    `session.jobs()` used to return) later writes "done" to disk underneath
    it. So on a reopen, `_state["jobs"]` is left exactly as it is; only an
    actual switch builds a fresh store and loads it.
    """
    prev_profile = _state["profile"]
    prev_pid = int(_state["server_pid"] or 0)
    same_project = prev_profile is not None and Path(prev_profile.root).resolve() == Path(profile.root).resolve()

    orphaned_server_pid = 0
    if not same_project:
        if prev_pid and is_alive(prev_pid):
            orphaned_server_pid = prev_pid
        _state["server_pid"] = 0
        store = JobStore(Path(profile.root) / ".dayz-mcp" / "jobs")
        store.load()
        _state["jobs"] = store

    _state["profile"] = profile
    _state["game"] = game
    _state["tools"] = tools_root
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
    if pid:
        _state["known_pids"].add(int(pid))


def known_pid(pid: int) -> bool:
    """True if this session ever recorded `pid` as its own server -- either the
    one it is currently tracking, or one later orphaned by a project switch
    (see set_project). Gates server_stop(pid=...) so it cannot be used to stop
    an arbitrary, unrelated process."""
    return int(pid) in _state["known_pids"]
