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


def _matches(lines: list[str], patterns: list[str]) -> tuple[list[str], list[str]]:
    """Search lines for patterns, returning (matches, invalid_patterns).

    On regex compilation error, skips that pattern and records it, so that
    a malformed pattern does not silently reduce the set of things being searched.
    """
    out: list[str] = []
    invalid: list[str] = []
    for pat in patterns:
        try:
            rx = re.compile(pat)
            out += [ln.strip() for ln in lines if rx.search(ln)]
        except re.error as exc:
            invalid.append(f"{pat!r}: {exc}")
    return out, invalid


def judge(rpt_lines: list[str], script_lines: list[str], expect: ExpectCfg) -> dict:
    patterns = list(FATAL_PATTERNS) + list(expect.error_regex)
    rpt_errors, rpt_invalid = _matches(rpt_lines, patterns)
    script_errors, script_invalid = _matches(script_lines, patterns)
    invalid = rpt_invalid + script_invalid

    # If any patterns were malformed, report them as a failure
    if invalid:
        return {
            "status": "fail",
            "errors": [f"malformed regex pattern: {p}" for p in invalid],
            "reason": "regex patterns in configuration are invalid",
        }

    errors = rpt_errors + script_errors
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
