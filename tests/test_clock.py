"""The one rule that decides whether a file belongs to the run that is asking.

Four call sites share it -- the client's crash dump, the server's ready line,
the mission-module check and log_verdict -- and all four used to get it wrong
in the same direction. See clock.py for the measurement.
"""
import time
from pathlib import Path

from dayz_mcp.clock import CLOCK_SKEW_SECONDS, belongs_to_run


def test_a_file_written_after_the_run_began_belongs_to_it() -> None:
    began = time.time()
    assert belongs_to_run(began + 0.5, began) is True


def test_a_file_whose_timestamp_trails_the_clock_still_belongs() -> None:
    """The defect this exists for: Windows hands time.time() a finer clock than
    it hands file timestamps, so a file written AFTER a run began can carry an
    mtime a millisecond or two BEFORE it."""
    began = time.time()
    assert belongs_to_run(began - 0.0014, began) is True


def test_a_file_from_an_earlier_run_does_not() -> None:
    began = time.time()
    assert belongs_to_run(began - 5.0, began) is False


def test_the_tolerance_is_narrower_than_any_gap_between_runs() -> None:
    """It has to swallow clock disagreement and nothing else. A second is
    already far shorter than one boot, and must still be refused."""
    assert CLOCK_SKEW_SECONDS < 1.0
    began = time.time()
    assert belongs_to_run(began - 1.0, began) is False


def test_the_skew_this_machine_actually_shows_fits_inside_the_tolerance(tmp_path: Path) -> None:
    """Measured rather than assumed, and re-measured on whatever runs the suite:
    if some filesystem trails further than the tolerance, this says so here
    instead of in an intermittent failure three modules away."""
    worst = 0.0
    for i in range(50):
        began = time.time()
        f = tmp_path / f"probe{i}.bin"
        f.write_bytes(b"x")
        worst = min(worst, f.stat().st_mtime - began)

    assert -worst < CLOCK_SKEW_SECONDS, (
        f"file timestamps trail the clock by {-worst:.4f}s here, which is more "
        f"than the {CLOCK_SKEW_SECONDS}s tolerance"
    )
