from __future__ import annotations

from ..errors import Result, fail, ok
from ..paths import find_game, find_tools
from ..procs import is_alive
from ..profile import load_profile
from . import session

NO_PROJECT_HINT = "call project_open with the path to a mod repository first"


def require_project() -> Result | None:
    if session.profile() is None:
        return fail("no project is open", hint=NO_PROJECT_HINT)
    return None


def project_open(path: str) -> Result:
    loaded = load_profile(path)
    if not loaded.ok:
        return loaded
    prof = loaded.data
    game = find_game(prof.machine.game)
    tools_root = find_tools(prof.machine.tools)
    switch = session.set_project(prof, game, tools_root)

    missing = [n for n, v in (("game", game), ("tools", tools_root)) if not v]
    data = {
        "name": prof.name,
        "root": str(prof.root),
        "game": game,
        "tools": tools_root,
        "stand_root": prof.machine.stand_root,
        "own_mod_dirs": prof.own_mod_dirs,
        "notes": prof.notes,
        "missing": missing,
    }
    # A server left running by whatever project was open before this call is no
    # longer tracked by this session (see session.set_project) -- surfaced here
    # rather than silently dropped, so the caller knows something else is up.
    if switch["orphaned_server_pid"]:
        data["orphaned_server_pid"] = switch["orphaned_server_pid"]
    return ok(data)


def project_status() -> Result:
    guard = require_project()
    if guard:
        return guard
    prof = session.profile()
    pid = session.server_pid()
    return ok(
        {
            "name": prof.name,
            "root": str(prof.root),
            "server_pid": pid,
            "server_running": is_alive(pid, image=session.server_image()) if pid else False,
            "jobs": [j.to_dict() for j in session.jobs().all()[-10:]],
        }
    )
