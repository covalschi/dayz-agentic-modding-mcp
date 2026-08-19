from __future__ import annotations

from pathlib import Path

from ..errors import Result, fail, ok
from ..verdict import build_verdict
from . import session
from .lifecycle import newest_client_profile
from .project import require_project

# What to change when there is no log to judge -- different for each source,
# and getting this wrong is expensive: the client hint used to name
# machine.stand_root, a setting the client side never reads at all.
NO_LOG_HINT = {
    "server": "start the server first, or check machine.stand_root",
    "client": "run client_compile_check first: the client's logs live with that job, "
              "not in the test stand",
}


def _newest_log(source: str) -> Path | None:
    """The newest script log for `source`.

    The two sources keep their logs in genuinely different places, and each
    place has exactly one owner: the server boots against the test stand
    (machine.stand_root, shared between runs), while each client compile check
    gets a throwaway profile inside its own job's artifacts -- see
    lifecycle.client_profile_dir, which is where that path is defined for both
    the writer and this reader.
    """
    if source == "client":
        folder = newest_client_profile()
        if folder is None:
            return None
    else:
        prof = session.profile()
        folder = Path(prof.machine.stand_root or prof.root / "testenv") / "profiles"
    if not folder.is_dir():
        return None
    logs = sorted(folder.glob("script_*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    return logs[0] if logs else None


def log_verdict(source: str = "server", since: float | None = None) -> Result:
    """Judge the newest log for `source`.

    `since` ties the verdict to a specific run (typically the value `server_start`
    returned): a log last modified before `since` cannot belong to the run being
    judged -- it is a leftover from an earlier boot (possibly one still holding the
    file open on Windows) -- so it is refused as a reason, not silently judged.
    """
    guard = require_project()
    if guard:
        return guard
    log = _newest_log(source)
    if log is None:
        return fail(f"no {source} log found", hint=NO_LOG_HINT.get(source, NO_LOG_HINT["server"]))
    if since is not None:
        mtime = log.stat().st_mtime
        if mtime < since:
            return fail(
                f"the newest {source} log predates the run being judged "
                f"(log last modified at {mtime:.1f}, run started at {since:.1f})",
                hint="wait for the server to write fresh output, then call log_verdict again with the same since",
            )
    lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
    data = build_verdict(lines, session.profile().expect)
    data["log"] = str(log)
    return ok(data)


def log_tail(source: str = "server", pattern: str = "", n: int = 50) -> Result:
    guard = require_project()
    if guard:
        return guard
    log = _newest_log(source)
    if log is None:
        return fail(f"no {source} log found", hint=NO_LOG_HINT.get(source, NO_LOG_HINT["server"]))
    lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
    if pattern:
        lines = [ln for ln in lines if pattern in ln]
    return ok({"log": str(log), "lines": lines[-n:]})
