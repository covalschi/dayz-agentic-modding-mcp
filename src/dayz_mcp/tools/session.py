"""One open project per server process, held here so every tool sees the same one."""
from __future__ import annotations

from pathlib import Path

from ..jobs import JobStore
from ..procs import is_alive
from ..profile import Profile

_state: dict = {
    "profile": None, "jobs": None, "game": None, "tools": None,
    "server_pid": 0, "server_image": "", "known_pids": set(), "stores": {},
}


def reset() -> None:
    _state.update({
        "profile": None, "jobs": None, "game": None, "tools": None,
        "server_pid": 0, "server_image": "", "known_pids": set(), "stores": {},
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

    The job store follows the same rule, but keyed by project rather than by
    "is this the project I saw last". JobStore.load() deliberately flips any
    job it finds recorded as "running" to "failed" ("lost: the server
    restarted..."), which is right after a genuine process restart -- the
    worker thread that owned it is truly gone -- and wrong for any project
    this process already has a store for, whose workers are alive in this very
    process and go on writing to whatever store instance they were handed.
    Building a second store and load()-ing it forks the job's history: the
    fresh store's copy is stamped "failed" forever, and every later job_status
    answers from that copy while the real worker writes "done" to disk
    underneath it.

    Comparing against the immediately previous project only (the shape of the
    first fix here) got the reopen case right and left A -> B -> A wrong, which
    is the same bug reached by a slightly longer route. So stores are held in a
    dict keyed by resolved root and reused whenever one exists; a project this
    process has genuinely not seen gets a fresh store and a load(), which is
    exactly the restart recovery that flip is for.
    """
    prev_profile = _state["profile"]
    prev_pid = int(_state["server_pid"] or 0)
    same_project = prev_profile is not None and Path(prev_profile.root).resolve() == Path(profile.root).resolve()

    orphaned_server_pid = 0
    if not same_project:
        # Pass the recorded image name (may be "", meaning "not known" -- then
        # this degrades to the old pid-only check): a dead server's pid can be
        # recycled by an unrelated Windows process, and a bare pid check would
        # then report someone else's process as this project's orphaned server.
        if prev_pid and is_alive(prev_pid, image=_state["server_image"]):
            orphaned_server_pid = prev_pid
        _state["server_pid"] = 0

    key = str(Path(profile.root).resolve())
    store = _state["stores"].get(key)
    if store is None:
        store = JobStore(Path(profile.root) / ".dayz-mcp" / "jobs")
        store.load()
        _state["stores"][key] = store
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


def set_server_pid(pid: int, image: str = "") -> None:
    """Record the pid this session's server is running under.

    `image` (e.g. "DayZDiag_x64.exe") is what `is_alive` checks against a
    recycled pid -- see its docstring. Clearing the pid (pid=0, the normal
    server_stop path) intentionally leaves the last known image in place
    rather than blanking it: it is a project-wide constant, not something
    tied to one particular boot, and a later server_stop(pid=...) for a pid
    orphaned by a project switch (see set_project) still needs it.
    """
    _state["server_pid"] = pid
    if image:
        _state["server_image"] = image
    if pid:
        _state["known_pids"].add(int(pid))


def server_image() -> str:
    return str(_state["server_image"] or "")


def known_pid(pid: int) -> bool:
    """True if this session ever recorded `pid` as its own server -- either the
    one it is currently tracking, or one later orphaned by a project switch
    (see set_project). Gates server_stop(pid=...) so it cannot be used to stop
    an arbitrary, unrelated process."""
    return int(pid) in _state["known_pids"]
