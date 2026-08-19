from __future__ import annotations

import threading
import time
from pathlib import Path

from ..compilecheck import client_cmd, judge
from ..errors import Result, fail, ok
from ..procs import is_alive, spawn, stop
from . import session
from .project import require_project

# server_status pulses the newest log twice this many seconds apart to see whether
# it is actually growing. Kept small and capped so the tool is a quick health check,
# not a disguised long wait -- long operations belong behind a job_id.
STATUS_PULSE_MAX = 10.0


def _stand() -> Path:
    prof = session.profile()
    return Path(prof.machine.stand_root or prof.root / "testenv")


def _is_within(path: Path, base: Path) -> bool:
    """True if `path` is `base` or lives underneath it.

    Used to refuse a serverDZ.cfg that resolves (possibly through a symlink)
    outside machine.stand_root -- see the -config note on server_start.
    """
    try:
        path.relative_to(base)
    except ValueError:
        return False
    return True


def mod_list(profiles_extra: str = "") -> tuple[str, str]:
    """Split the profile's configured mods into (-mod, -serverMod) path strings.

    A mod is routed to -serverMod when its folder name is listed in
    mods.server_only; everything else goes to -mod. A mod in the wrong list
    breaks client connections without a single readable line in the log, so the
    split is made here, once, instead of being decided ad hoc by each caller.
    """
    prof = session.profile()
    game = session.game() or ""
    server_only = set(prof.mods.server_only)

    parts = [str(Path(game) / "!Workshop" / m) for m in prof.mods.required]
    parts += [str(Path(prof.root) / m) for m in prof.own_mod_dirs]
    parts += list(prof.mods.extra)
    if profiles_extra:
        parts.append(profiles_extra)
    parts = [p for p in parts if p]

    client_mods = [p for p in parts if Path(p).name not in server_only]
    server_mods = [p for p in parts if Path(p).name in server_only]
    return ";".join(client_mods), ";".join(server_mods)


def _newest(folder: Path, pattern: str) -> Path | None:
    items = sorted(folder.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
    return items[0] if items else None


def server_start(timeout: float = 420) -> Result:
    guard = require_project()
    if guard:
        return guard
    prof = session.profile()

    # A second boot on top of a live one would fight the first for the same port
    # and the same profiles directory instead of failing loudly.
    running_pid = session.server_pid()
    if running_pid and is_alive(running_pid):
        return fail(
            f"a server is already running for this session (pid {running_pid})",
            hint="call server_stop first, or server_status to check on the running one",
        )

    game = session.game()
    if not game:
        return fail("game not found", hint="set machine.game in dayz-mcp.local.toml")

    stand = _stand()
    cfg_path = stand / "serverDZ.cfg"
    if not cfg_path.exists():
        return fail(f"server config not found: {cfg_path}", hint="point machine.stand_root at a prepared stand")

    # DayZDiag_x64.exe forces $currentdir to its own directory on launch, so a
    # relative -config is silently replaced by the game installation's own config
    # (verified against another project's comment for engine 1.29). Resolve to an
    # absolute path, and refuse if that resolution (e.g. through a symlink) lands
    # outside the stand we were told to use.
    cfg = cfg_path.resolve()
    stand_resolved = stand.resolve()
    if not _is_within(cfg, stand_resolved):
        return fail(
            f"server config resolves outside stand_root: {cfg}",
            hint="serverDZ.cfg must be a real file inside machine.stand_root, not a link to somewhere else",
        )

    client_mods, server_mods = mod_list()
    store = session.jobs()
    job = store.create("boot")
    # The moment the job record was created, just before the process is spawned.
    # log_verdict is given this back as `since` so it never judges a log left over
    # from an earlier boot.
    since = job.started
    profiles = stand / "profiles"
    profiles.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(Path(game) / "DayZDiag_x64.exe"), "-server", f"-config={cfg}",
        f"-port={prof.machine.port}", f"-mod={client_mods}", f"-profiles={profiles}",
    ]
    if server_mods:
        cmd.append(f"-serverMod={server_mods}")

    def run() -> None:
        store.start(job.id)
        # An uncaught exception here must still resolve the job, not just print a
        # traceback to the stdio server's stderr where the agent cannot see it: a
        # game directory whose DayZDiag_x64.exe exists but is not a runnable image
        # (a partial download, a placeholder) passes find_game's existence probe
        # and then makes spawn() raise OSError. Without this, the job stays
        # "running" forever, and the next process start relabels it "lost to a
        # restart" instead of what actually happened.
        try:
            # Old script_*.log files are left alone: unlinking a file a live server
            # still holds open raises PermissionError on Windows, and that exception
            # inside this thread would leave the job stuck in "running" forever. The
            # `since` cutoff below is what tells this run's log apart from theirs.
            pid = spawn(cmd, Path(game))
            session.set_server_pid(pid)
            marker = prof.expect.ready_line
            deadline = time.time() + timeout
            while time.time() < deadline:
                if not is_alive(pid):
                    store.fail(job.id, "the server process died before it was ready")
                    return
                for log in profiles.glob("script_*.log"):
                    if log.stat().st_mtime < since:
                        continue
                    if marker and marker in log.read_text(encoding="utf-8", errors="replace"):
                        store.add_artifact(job.id, log)
                        store.finish(job.id, 0, summary=f"ready, pid {pid}")
                        return
                time.sleep(2)
            store.fail(job.id, f"no ready line within {timeout}s")
        except Exception as exc:  # noqa: BLE001 - must reach the job, not just stderr
            store.fail(job.id, f"{type(exc).__name__}: {exc}")

    threading.Thread(target=run, daemon=True).start()
    return ok({"job_id": job.id, "since": since})


def server_stop() -> Result:
    pid = session.server_pid()
    if not pid:
        return ok({"stopped": False, "reason": "no server was started by this session"})
    stopped = stop(pid)
    session.set_server_pid(0)
    return ok({"stopped": stopped, "pid": pid})


def server_status(pulse_seconds: float = 1.0) -> Result:
    """A quick health read: is the process alive, and is its log actually growing.

    A hung boot and a slow one both look "alive, no ready line yet" from the
    outside -- this project has hit a genuine post-compile server hang before --
    so this samples the newest log's size twice, `pulse_seconds` apart, and also
    reports how long it has been since the log last changed at all.
    """
    guard = require_project()
    if guard:
        return guard
    pid = session.server_pid()
    running = is_alive(pid) if pid else False

    profiles = _stand() / "profiles"
    log = _newest(profiles, "script_*.log")
    if log is None:
        return ok({"pid": pid, "running": running, "log": None, "growing": None, "stalled_seconds": None})

    pulse = max(0.0, min(pulse_seconds, STATUS_PULSE_MAX))
    try:
        size_before = log.stat().st_size
    except OSError:
        size_before = None
    time.sleep(pulse)
    try:
        stat_after = log.stat()
    except OSError:
        return ok({"pid": pid, "running": running, "log": str(log), "growing": None, "stalled_seconds": None})

    growing = size_before is not None and stat_after.st_size > size_before
    stalled_seconds = max(0.0, time.time() - stat_after.st_mtime)
    return ok(
        {
            "pid": pid,
            "running": running,
            "log": str(log),
            "growing": growing,
            "stalled_seconds": round(stalled_seconds, 1),
        }
    )


def client_compile_check(extra_mods: str = "", wait_seconds: float = 120) -> Result:
    guard = require_project()
    if guard:
        return guard
    prof = session.profile()
    game = session.game()
    if not game:
        return fail("game not found", hint="set machine.game in dayz-mcp.local.toml")

    store = session.jobs()
    job = store.create("client-compile")
    profiles = store.artifacts_dir(job.id) / "clientprofile"
    profiles.mkdir(parents=True, exist_ok=True)

    def run() -> None:
        store.start(job.id)
        try:
            # -serverMod mods are dedicated-server only; the diagnostic client never
            # loads them, so only the client half of the split is used here.
            client_mods, _server_mods = mod_list(extra_mods)
            pid = spawn(client_cmd(Path(game), client_mods, profiles), Path(game))
            time.sleep(wait_seconds)
            rpt = _newest(profiles, "*.RPT")
            slog = _newest(profiles, "script_*.log")
            stop(pid)
            rpt_lines = rpt.read_text(encoding="utf-8", errors="replace").splitlines() if rpt else []
            log_lines = slog.read_text(encoding="utf-8", errors="replace").splitlines() if slog else []
            for art in (rpt, slog):
                if art:
                    store.add_artifact(job.id, art)
            got = judge(rpt_lines, log_lines, prof.expect)
            if got["status"] == "ok":
                store.finish(job.id, 0, summary="client compiles")
            else:
                store.fail(job.id, f"{got['status']}: {got['reason']} {' | '.join(got['errors'][:5])}")
        except Exception as exc:  # noqa: BLE001 - must reach the job, not just stderr
            store.fail(job.id, f"{type(exc).__name__}: {exc}")

    threading.Thread(target=run, daemon=True).start()
    return ok({"job_id": job.id})
