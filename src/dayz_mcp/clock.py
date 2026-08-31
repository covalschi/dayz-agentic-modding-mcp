"""Deciding whether a file was written by the run that is asking.

Four places need this and all four asked it the same wrong way: `mtime >=
since`, with `since` taken from `time.time()`. That comparison is not sound on
Windows, and the reason is measurable rather than theoretical.

MEASURED ON THIS MACHINE (300 files, written immediately after a `time.time()`
reading): 266 of them carried an `st_mtime` EARLIER than that reading, by up to
1.4 ms. `time.time()` is served by `GetSystemTimePreciseAsFileTime` at 1e-07
resolution; the timestamp landed on the file is coarser. So a file genuinely
written after a run began can look, to a strict comparison, like it belongs to
the run before.

WHAT THAT COST. Every one of the four call sites uses this to decide whether
some evidence is THIS run's -- a crash dump beside the client profile, a script
log the server just wrote. Discarding it does not fail loudly: the waiting loop
simply keeps waiting, to the full timeout, and then reports the silence instead
of the event that was sitting on disk the whole time. Two tests caught it
intermittently and were read as flaky tests for a while, which is the other
half of what a silent wrong answer costs.

The tolerance is the skew and nothing more. What must NOT be swallowed is a
file from an earlier RUN, and runs are whole boots apart -- seconds at the very
least -- so fifty milliseconds is both thirty-five times the observed
disagreement and nowhere near the gap it must keep refusing.
"""
from __future__ import annotations

#: How far a file's timestamp may trail the clock reading that began the run.
CLOCK_SKEW_SECONDS = 0.05


def belongs_to_run(mtime: float, since: float) -> bool:
    """Was a file with this mtime written by the run that started at `since`?"""
    return mtime >= since - CLOCK_SKEW_SECONDS
