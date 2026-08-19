"""Tests for the file-touching half of the bridge: send()'s atomic mailbox
write, and read_state()/await_result()/heartbeat()'s tolerant reads of the
mod's state file.

There is no mod-side writer yet (Task 5) -- every test here writes files
directly under tmp_path, exercising the wire format Task 2's parse_state
defines and this module both produces (send) and consumes (read_state).
"""
from __future__ import annotations

import json
import threading
import time

from dayz_mcp.bridge.channel import CMD_FILENAME, STATE_FILENAME, Channel
from dayz_mcp.bridge.protocol import Command


def _write_state(profiles_dir, **overrides) -> None:
    payload = {"tick": 1, "command": None, "errors": [], "world": {}}
    payload.update(overrides)
    (profiles_dir / STATE_FILENAME).write_text(json.dumps(payload), encoding="utf-8")


def _command_payload(cmd_id, status, detail="", finished_at=None) -> dict:
    return {"id": cmd_id, "status": status, "detail": detail, "finished_at": finished_at}


# --- send: atomic write --------------------------------------------------


def test_send_writes_mailbox_and_leaves_no_temp_file(tmp_path):
    ch = Channel(tmp_path)
    cmd = Command(id="ping-1-1", verb="ping", args={"n": 1})

    result = ch.send(cmd)

    assert result.ok
    mailbox = tmp_path / CMD_FILENAME
    assert mailbox.exists()
    assert json.loads(mailbox.read_text(encoding="utf-8")) == {
        "id": "ping-1-1", "verb": "ping", "args": {"n": 1}
    }
    leftovers = [p.name for p in tmp_path.iterdir() if p.name != CMD_FILENAME]
    assert leftovers == [], f"temp file(s) left behind: {leftovers}"


def test_send_refuses_when_mailbox_is_unclaimed(tmp_path):
    ch = Channel(tmp_path)
    first = Command(id="ping-1-1", verb="ping", args={})
    second = Command(id="ping-2-2", verb="ping", args={"x": 1})
    assert ch.send(first).ok

    result = ch.send(second)

    assert not result.ok
    assert result.hint, "a refusal must say what to do, not just that it failed"
    assert "picked up" in result.hint
    # The unclaimed command must survive untouched, not be silently dropped.
    on_disk = json.loads((tmp_path / CMD_FILENAME).read_text(encoding="utf-8"))
    assert on_disk["id"] == first.id


def test_concurrent_sends_exactly_one_succeeds(tmp_path):
    """Check-then-write is not atomic: two threads can both see an empty
    mailbox and both proceed to write, and one silently overwrites the
    other's command with no error raised anywhere. This is not hypothetical
    once tool calls are dispatched through a thread pool (Task 4) -- several
    world_* calls can reach the same Channel at once. Exactly one send must
    win; every other thread must see the same refusal (same hint) a caller
    who lost to an already-unclaimed mailbox would see, and nothing must be
    left half-written or double-written on disk.

    Fired from a barrier, not a sleep race, so all N threads call send() at
    the same instant every run -- deterministic, not a chance to catch the
    race only sometimes.
    """
    ch = Channel(tmp_path)
    n = 8
    commands = [Command(id=f"ping-{i}-1", verb="ping", args={"i": i}) for i in range(n)]
    barrier = threading.Barrier(n)
    results: list = [None] * n

    def worker(i):
        barrier.wait()
        results[i] = ch.send(commands[i])

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert all(r is not None for r in results), "a thread did not finish within the join timeout"
    successes = [r for r in results if r.ok]
    failures = [r for r in results if not r.ok]
    assert len(successes) == 1, f"expected exactly one send to succeed, got {len(successes)}"
    assert len(failures) == n - 1
    for f in failures:
        assert f.hint, "a refusal must say what to do, not just that it failed"
        assert "picked up" in f.hint

    # The mailbox holds exactly the one command that won -- no corruption
    # from two writers landing on the same file at once.
    on_disk = json.loads((tmp_path / CMD_FILENAME).read_text(encoding="utf-8"))
    assert on_disk["id"] in {c.id for c in commands}

    # And the losers' cleanup left no temp file behind either.
    leftovers = [p.name for p in tmp_path.iterdir() if p.name != CMD_FILENAME]
    assert leftovers == [], f"temp file(s) left behind: {leftovers}"


# --- read_state: tolerant of torn/missing files ---------------------------


def test_read_state_on_valid_file_returns_populated_state(tmp_path):
    ch = Channel(tmp_path)
    _write_state(tmp_path, tick=7, world={"ok": True})

    state = ch.read_state()

    assert state is not None
    assert state.tick == 7
    assert state.world == {"ok": True}


def test_read_state_on_missing_file_returns_none(tmp_path):
    ch = Channel(tmp_path)
    assert ch.read_state() is None


def test_read_state_on_torn_file_returns_none_without_raising(tmp_path):
    ch = Channel(tmp_path)
    (tmp_path / STATE_FILENAME).write_text('{"tick": 7, "wor', encoding="utf-8")

    assert ch.read_state() is None  # must not raise json.JSONDecodeError


# --- await_result: correlation, early return, timeout fallback -----------


def test_await_result_returns_as_soon_as_the_result_appears(tmp_path):
    ch = Channel(tmp_path)
    cmd_id = "ping-1-1"
    _write_state(tmp_path, tick=1, command=None)

    def finish_later():
        time.sleep(0.15)
        _write_state(tmp_path, tick=2, command=_command_payload(cmd_id, "done", "pong", 123.0))

    threading.Thread(target=finish_later, daemon=True).start()
    started = time.monotonic()
    result = ch.await_result(cmd_id, timeout=5.0, poll=0.05)
    elapsed = time.monotonic() - started

    assert result is not None
    assert result.status == "done"
    assert result.id == cmd_id
    assert elapsed < 2.0, "must return promptly once the result lands, not wait out the full timeout"


def test_await_result_ignores_a_different_commands_id(tmp_path):
    ch = Channel(tmp_path)
    our_id = "ping-2-1"
    other_id = "ping-1-1"
    # The mod is still reporting a PREVIOUS, already-finished command under a
    # different id -- must never be mistaken for our own answer.
    _write_state(tmp_path, tick=1, command=_command_payload(other_id, "done", "stale result"))

    result = ch.await_result(our_id, timeout=0.2, poll=0.05)

    assert result is None


def test_await_result_on_timeout_returns_last_state_actually_observed(tmp_path):
    ch = Channel(tmp_path)
    cmd_id = "ping-3-1"
    _write_state(tmp_path, tick=5, command=_command_payload(cmd_id, "running", "still going"))

    result = ch.await_result(cmd_id, timeout=0.2, poll=0.05)

    assert result is not None
    assert result.status == "running"  # not an invented "failed"/"timeout" status
    assert result.detail == "still going"


def test_await_result_returns_none_when_nothing_ever_matches(tmp_path):
    ch = Channel(tmp_path)
    result = ch.await_result("ping-nope-1", timeout=0.15, poll=0.05)
    assert result is None


def test_await_result_never_blocks_past_its_timeout(tmp_path):
    ch = Channel(tmp_path)
    _write_state(tmp_path, tick=1, command=_command_payload("someone-elses-id", "running"))

    started = time.monotonic()
    ch.await_result("mine", timeout=0.3, poll=0.05)

    assert time.monotonic() - started < 2.0


# --- heartbeat: growing vs. stalled tick -----------------------------------


def test_heartbeat_detects_a_growing_tick(tmp_path):
    ch = Channel(tmp_path)
    _write_state(tmp_path, tick=10)

    def bump_later():
        time.sleep(0.1)
        _write_state(tmp_path, tick=11)

    threading.Thread(target=bump_later, daemon=True).start()
    growing, tick = ch.heartbeat(window=0.25)

    assert growing is True
    assert tick == 11


def test_heartbeat_detects_a_stalled_tick(tmp_path):
    ch = Channel(tmp_path)
    _write_state(tmp_path, tick=42)

    growing, tick = ch.heartbeat(window=0.15)

    assert growing is False
    assert tick == 42


def test_heartbeat_with_no_state_file_reports_not_growing(tmp_path):
    ch = Channel(tmp_path)
    growing, tick = ch.heartbeat(window=0.05)
    assert growing is False
    assert tick == 0


def test_heartbeat_tolerates_a_single_torn_read(tmp_path):
    """A read that fails once (file not there yet -- the same symptom a torn
    write leaves) and recovers shortly after must not be mistaken for a dead
    bridge, only a genuine run of failures should be."""
    ch = Channel(tmp_path)

    def create_soon():
        time.sleep(0.02)
        _write_state(tmp_path, tick=3)

    threading.Thread(target=create_soon, daemon=True).start()
    growing, tick = ch.heartbeat(window=0.3)

    assert tick == 3
    assert growing is False  # recovered to the same tick both times, not a growth signal
