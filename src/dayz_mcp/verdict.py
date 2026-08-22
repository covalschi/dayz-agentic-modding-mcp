"""Turning a log into a decision.

The point of this module is that the agent never has to read a log to know
whether the run was good. Everything that makes a run bad ends up in `reasons`.

A broken boot can produce thousands of error lines. An uncapped verdict would
hand the agent the log back in JSON form instead of a decision, so every list
that can grow without bound is capped at MAX_LIST_ITEMS and the true count is
reported alongside it (`<key>_total`) so truncation is visible, not silent.
"""
from __future__ import annotations

from .logparse import (
    DEFAULT_FORBID,
    DEFAULT_NOISE,
    classify,
    find_ready_line,
    parse_counters,
)
from .profile import ExpectCfg

MAX_LIST_ITEMS = 25


def build_verdict(lines: list[str], expect: ExpectCfg) -> dict:
    noise_patterns = list(DEFAULT_NOISE) + list(expect.noise)
    # Both lists ADD to the built-in ones rather than replacing them. A
    # profile's own forbidden string is about its own mod; the engine's are
    # about whether the run was usable at all, and no project has a reason to
    # turn those off.
    forbidden = list(DEFAULT_FORBID) + list(expect.forbid)
    buckets = classify(lines, forbid=forbidden, noise=noise_patterns)

    ready = find_ready_line(lines, expect.ready_line) if expect.ready_line else None
    counters = parse_counters(ready) if ready else {}

    unexpected: dict[str, dict] = {}
    for key, want in expect.counters.items():
        got = counters.get(key)
        if got != want:
            unexpected[key] = {"expected": want, "actual": got}

    reasons: list[str] = []
    if expect.ready_line and ready is None:
        reasons.append(f"ready line not found: {expect.ready_line!r}")
    for key, cmp in unexpected.items():
        reasons.append(f"counter {key}: {cmp['actual']} instead of the declared {cmp['expected']}")
    for err in buckets["errors"]:
        reasons.append(err["text"])
    for crash in buckets["crashes"]:
        reasons.append(f"crash: {crash['text']}")

    warn_total = sum(w["count"] for w in buckets["warnings"])
    if expect.max_warnings is not None and warn_total > expect.max_warnings:
        reasons.append(f"warnings: {warn_total} against the declared budget of {expect.max_warnings}")

    errors = buckets["errors"]
    crashes = buckets["crashes"]
    warnings = buckets["warnings"]
    noise = buckets["noise"]

    return {
        "verdict": "fail" if reasons else "pass",
        "ready_line": ready,
        "counters": counters,
        "counters_unexpected": unexpected,
        "errors": errors[:MAX_LIST_ITEMS],
        "errors_total": len(errors),
        "warnings": warnings[:MAX_LIST_ITEMS],
        "warnings_total": len(warnings),
        "noise": noise[:MAX_LIST_ITEMS],
        "noise_total": len(noise),
        "crashes": crashes[:MAX_LIST_ITEMS],
        "crashes_total": len(crashes),
        "warning_total": warn_total,
        "reasons": reasons[:MAX_LIST_ITEMS],
        "reasons_total": len(reasons),
    }
