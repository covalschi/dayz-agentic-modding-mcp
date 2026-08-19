"""Pure log analysis: no files, no processes, no knowledge of any mod.

Strings listed in DEFAULT_NOISE come from the engine itself and appear for every
mod, including vanilla ones, so counting them as problems would make every run
look broken.
"""
from __future__ import annotations

import re

DEFAULT_NOISE = [
    "skeletons.anim.xml",      # the engine probes optional animations in every mod folder
    "was not closed",          # left behind when a server is killed rather than shut down
]

_COUNTER = re.compile(r"(?<![\w.])([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(-?\d+)(?![\w.])")
_QUOTED = re.compile(r"""(['"])(?:(?!\1).)*\1""")
_NUMBER = re.compile(r"\d+")
_TIMESTAMP = re.compile(r"^\s*\d{1,2}:\d{2}:\d{2}(\.\d+)?\s*")
_WARNING = re.compile(r"\bWARNING\b", re.IGNORECASE)
_ERROR = re.compile(r"\bERROR\b", re.IGNORECASE)
_CRASH = re.compile(r"(Exception Code|ACCESS_VIOLATION|0xC0000005)", re.IGNORECASE)


def find_ready_line(lines: list[str], marker: str) -> str | None:
    if not marker:
        return None
    for line in reversed(lines):
        if marker in line:
            return line.strip()
    return None


def parse_counters(line: str) -> dict[str, int]:
    return {m.group(1): int(m.group(2)) for m in _COUNTER.finditer(line or "")}


def group_key(line: str) -> str:
    """Collapse a line to its shape so repeats of the same complaint about
    different objects land in one group. Returns the full normalized line
    to preserve grouping identity for lines that differ only after 120 chars."""
    s = _TIMESTAMP.sub("", line.strip())
    s = _QUOTED.sub("<x>", s)
    s = _NUMBER.sub("#", s)
    return " ".join(s.split())


def _bucket(store: dict[str, dict], line: str) -> None:
    key = group_key(line)
    entry = store.get(key)
    if entry is None:
        store[key] = {"group": key[:120], "count": 1, "sample": line.strip()}
    else:
        entry["count"] += 1


def classify(lines: list[str], forbid: list[str], noise: list[str]) -> dict:
    errors: list[dict] = []
    warnings: dict[str, dict] = {}
    noises: dict[str, dict] = {}
    crashes: list[dict] = []

    for line in lines:
        if not line.strip():
            continue
        # Order matters: forbid → crash → error → noise → warning
        # Noise must come after all more serious categories to avoid swallowing fatal lines
        if any(f and f in line for f in forbid):
            errors.append({"kind": "forbidden", "text": line.strip()})
            continue
        if _CRASH.search(line):
            crashes.append({"text": line.strip()})
            continue
        if _ERROR.search(line):
            errors.append({"kind": "error", "text": line.strip()})
            continue
        if any(n and n in line for n in noise):
            _bucket(noises, line)
            continue
        if _WARNING.search(line):
            _bucket(warnings, line)

    return {
        "errors": errors,
        "warnings": sorted(warnings.values(), key=lambda w: -w["count"]),
        "noise": sorted(noises.values(), key=lambda w: -w["count"]),
        "crashes": crashes,
    }
