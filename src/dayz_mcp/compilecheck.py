"""Client-side script compilation check.

A server boot compiles nothing that lives behind the client-only guard, so a
broken menu passes a server boot and breaks in front of a player. This runs the
diagnostic client instead, waits, and reads the logs.

`Module: Mission` is the proof that compilation actually got as far as the game's
own mission module. Without it, "no errors" only means "not yet".
"""
from __future__ import annotations

import re
from pathlib import Path

from .profile import ExpectCfg

MISSION_MARKER = "Module: Mission"
FATAL_PATTERNS = [r"Can't compile", r"Compiling .* failed"]


def client_cmd(game: Path, mods: str, profiles: Path) -> list[str]:
    return [
        str(Path(game) / "DayZDiag_x64.exe"),
        f"-mod={mods}",
        f"-profiles={profiles}",
        "-nolauncher",
    ]


def _matches(lines: list[str], patterns: list[str]) -> list[str]:
    out: list[str] = []
    for pat in patterns:
        rx = re.compile(pat)
        out += [ln.strip() for ln in lines if rx.search(ln)]
    return out


def judge(rpt_lines: list[str], script_lines: list[str], expect: ExpectCfg) -> dict:
    patterns = list(FATAL_PATTERNS) + list(expect.error_regex)
    errors = _matches(rpt_lines, patterns) + _matches(script_lines, patterns)
    if errors:
        return {"status": "fail", "errors": errors[:25], "reason": "compilation errors"}

    reached = any(MISSION_MARKER in ln for ln in script_lines)
    if not reached:
        return {
            "status": "unknown",
            "errors": [],
            "reason": f"{MISSION_MARKER!r} never appeared: compilation did not get that far, "
                      "so a clean log proves nothing -- allow more time",
        }
    return {"status": "ok", "errors": [], "reason": ""}
