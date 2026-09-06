from __future__ import annotations

from pathlib import Path

from ..errors import Result, fail, ok
from ..verdict import build_verdict
from . import session
from .lifecycle import newest_client_profile, server_profiles_dir
from .project import require_project
from ..clock import belongs_to_run

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
        # server_profiles_dir, not a second copy of its formula: this line held
        # that formula character for character, which is the same "two owners
        # for one path" arrangement the client side already got wrong once.
        folder = server_profiles_dir()
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
        if not belongs_to_run(mtime, since):
            return fail(
                f"the newest {source} log predates the run being judged "
                f"(log last modified at {mtime:.1f}, run started at {since:.1f})",
                hint="wait for the server to write fresh output, then call log_verdict again with the same since",
            )
    lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
    data = build_verdict(lines, session.profile().expect)
    data["log"] = str(log)
    return ok(data)


#: How much of a log to read at a time when only its end is wanted. 64 KB is
#: several hundred lines of an engine log, so one block almost always answers.
_TAIL_BLOCK = 64 * 1024


def _decode(block: bytes) -> str:
    """Decode one block's raw bytes. Broken out of `_tail_lines` so a test can
    meter how much work a block read costs -- the whole point of the fix below
    is that this is called once per block, never on a growing buffer."""
    return block.decode("utf-8", errors="replace")


def _tail_lines(log: Path, n: int, pattern: str) -> list[str]:
    """The last `n` lines of `log` (matching `pattern`, if given), read from
    the END rather than from the start.

    This is the tool an agent calls repeatedly WHILE a boot is running, on a
    file that grows the whole time -- and a DayZ script log reaches megabytes.
    Reading it whole to answer with fifty lines made every call cost the size
    of the log; reading backwards in blocks costs the size of the answer,
    except when a `pattern` matches nothing near the end and the whole file
    genuinely has to be searched.

    Each block is decoded and split ONCE, not accumulated with every earlier
    block and re-decoded/re-split/re-filtered on every iteration -- that
    earlier shape was quadratic in the number of blocks, which is exactly the
    case a non-matching `pattern` hits (it has to walk the whole file). A
    block boundary lands mid-line, so the block's first line is still missing
    its beginning; it is carried forward as `carry` and glued onto the front
    of the NEXT (earlier) block's own text before that block is split, which
    is where its true beginning lives. Once the read reaches byte 0 there is
    no earlier block left to complete it, so what remains is a real line, not
    a carry.
    """
    try:
        with log.open("rb") as fh:
            end = log.stat().st_size
            found: list[str] = []
            carry = ""
            while end > 0:
                start = max(0, end - _TAIL_BLOCK)
                fh.seek(start)
                block = fh.read(end - start)
                end = start
                lines = (_decode(block) + carry).splitlines()
                if start > 0:
                    carry = lines[0] if lines else ""
                    lines = lines[1:]
                else:
                    carry = ""
                if pattern:
                    lines = [ln for ln in lines if pattern in ln]
                found = lines + found
                if len(found) >= n:
                    break
    except OSError:
        return []
    return found[-n:]


def log_tail(source: str = "server", pattern: str = "", n: int = 50) -> Result:
    """The last `n` lines of the newest log, optionally only the lines
    containing `pattern`.

    `source` is "server" (the stand's own script log) or "client" (the log the
    last client_compile_check produced). Raw text, deliberately: log_verdict is
    the tool that judges a run, and this is the one for looking at what it
    judged -- or at a boot that has not finished yet.
    """
    guard = require_project()
    if guard:
        return guard
    log = _newest_log(source)
    if log is None:
        return fail(f"no {source} log found", hint=NO_LOG_HINT.get(source, NO_LOG_HINT["server"]))
    lines = _tail_lines(log, n, pattern)
    return ok({"log": str(log), "lines": lines})
