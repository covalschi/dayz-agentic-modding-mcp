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

from dayz_mcp.bridge.channel import (
    CMD_FILENAME,
    HEARTBEAT_GROWING,
    HEARTBEAT_RESTARTED,
    HEARTBEAT_STALLED,
    HEARTBEAT_UNMEASURABLE,
    STATE_FILENAME,
    Channel,
)
from dayz_mcp.bridge.protocol import Command, ParseRejection


def _write_state(profiles_dir, **overrides) -> None:
    payload = {"tick": 1, "session_id": "session-1", "command": None, "errors": [], "world": {}}
    payload.update(overrides)
    (profiles_dir / STATE_FILENAME).write_text(json.dumps(payload), encoding="utf-8")


def _command_payload(cmd_id, status, detail="", finished_at=None) -> dict:
    return {"id": cmd_id, "status": status, "detail": detail, "finished_at": finished_at}


def _cmd(cmd_id, verb="ping", args=None, session_id="s1") -> Command:
    """A ready-to-send Command with a plausible session already stamped --
    most tests here care about the mailbox mechanics, not about where the
    session came from (build_command's own tests cover that)."""
    return Command(id=cmd_id, session_id=session_id, verb=verb, args=args or {})


# --- send: atomic write --------------------------------------------------


def test_send_writes_mailbox_and_leaves_no_temp_file(tmp_path):
    ch = Channel(tmp_path)
    cmd = _cmd("ping-1-1", args={"n": 1})

    result = ch.send(cmd, is_alive=True)

    assert result.ok
    mailbox = tmp_path / CMD_FILENAME
    assert mailbox.exists()
    assert json.loads(mailbox.read_text(encoding="utf-8")) == {
        "id": "ping-1-1", "session_id": "s1", "verb": "ping", "args": {"n": 1}
    }
    leftovers = [p.name for p in tmp_path.iterdir() if p.name != CMD_FILENAME]
    assert leftovers == [], f"temp file(s) left behind: {leftovers}"


def test_send_refuses_when_mailbox_is_unclaimed(tmp_path):
    ch = Channel(tmp_path)
    first = _cmd("ping-1-1")
    second = _cmd("ping-2-2", args={"x": 1})
    assert ch.send(first, is_alive=True).ok

    result = ch.send(second, is_alive=True)

    assert not result.ok
    assert result.hint, "a refusal must say what to do, not just that it failed"
    assert "picked up" in result.hint
    # The unclaimed command must survive untouched, not be silently dropped.
    on_disk = json.loads((tmp_path / CMD_FILENAME).read_text(encoding="utf-8"))
    assert on_disk["id"] == first.id


def test_send_refuses_when_not_alive_and_does_not_write(tmp_path):
    """A command written into a dead stand's profile directory IS the
    wedge: nothing alive would ever claim it, and every later send() would
    then refuse it as unclaimed forever. is_alive=False must refuse before
    writing anything at all, and say plainly that nothing was written -- a
    caller who thinks it MIGHT have been written will not retry."""
    ch = Channel(tmp_path)

    result = ch.send(_cmd("ping-1-1"), is_alive=False)

    assert not result.ok
    assert "NOT written" in result.error
    assert result.hint
    assert not (tmp_path / CMD_FILENAME).exists()


def test_send_refuses_when_command_has_no_session_id(tmp_path):
    """A command with no session is indistinguishable to the mod from a
    stale one left over from a previous boot -- send() must not let the
    Python side accidentally write one, even bypassing build_command and
    constructing a Command directly with a blank session."""
    ch = Channel(tmp_path)
    cmd = Command(id="ping-1-1", session_id="", verb="ping", args={})

    result = ch.send(cmd, is_alive=True)

    assert not result.ok
    assert "session_id" in result.error
    assert result.hint
    assert not (tmp_path / CMD_FILENAME).exists()


def _fire_concurrent_sends(round_dir, n, tag):
    """Fire `n` send()s at the same instant (barrier-released, not a sleep
    race) against a fresh empty mailbox in `round_dir`. Returns the list of
    Results, one per thread, in thread-index order."""
    ch = Channel(round_dir)
    commands = [_cmd(f"{tag}-{i}", args={"i": i}) for i in range(n)]
    barrier = threading.Barrier(n)
    results: list = [None] * n

    def worker(i):
        barrier.wait()
        results[i] = ch.send(commands[i], is_alive=True)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    return commands, results


def test_concurrent_sends_exactly_one_succeeds(tmp_path):
    """Check-then-write is not atomic: two threads can both see an empty
    mailbox and both proceed to write, and one silently overwrites the
    other's command with no error raised anywhere. This is not hypothetical
    once tool calls are dispatched through a thread pool (Task 4) -- several
    world_* calls can reach the same Channel at once. Exactly one send must
    win every round; every loser must return ONE OF the two documented
    refusals, and nothing must be left half-written or double-written on
    disk.

    A loser can legitimately land on EITHER refusal -- that is correct, not
    a bug. A thread descheduled between barrier.wait() and its own exists()
    pre-check can find the winner's file already on disk and take the
    mailbox-occupied path rather than losing the os.link claim itself. An
    earlier version of this test asserted every loser hits the os.link race
    path specifically; that assumption does not hold under load and made
    the test itself flaky -- reviewer-measured at 16% of rounds failing with
    6 concurrent threads under ordinary background load (2.5% at 2 threads),
    because a busy machine reschedules threads far more than an idle one,
    and this suite (which builds mods and boots servers) is not idle. Fixed
    here to assert only what os.link's atomicity actually guarantees
    (exactly one winner; every loser is one of the two known refusals;
    nothing corrupted or left behind), and to prove the os.link race path is
    genuinely exercised by repeating the round rather than by asserting it
    happens on every single one.
    """
    n = 8
    rounds = 20
    race_path_seen = False

    for round_i in range(rounds):
        round_dir = tmp_path / f"round-{round_i}"
        round_dir.mkdir()
        commands, results = _fire_concurrent_sends(round_dir, n, tag=f"ping-{round_i}")

        assert all(r is not None for r in results), f"round {round_i}: a thread did not finish"
        successes = [r for r in results if r.ok]
        failures = [r for r in results if not r.ok]
        assert len(successes) == 1, f"round {round_i}: expected exactly one success, got {len(successes)}"
        assert len(failures) == n - 1

        for f in failures:
            assert f.hint, "a refusal must say what to do, not just that it failed"
            is_lost_race = "concurrent sender" in f.error
            is_unclaimed = "picked up" in f.hint
            assert is_lost_race or is_unclaimed, (
                f"round {round_i}: refusal matched neither known text: "
                f"error={f.error!r} hint={f.hint!r}"
            )
            if is_lost_race:
                race_path_seen = True

        # The mailbox holds exactly the one command that won -- no
        # corruption from two writers landing on the same file at once.
        on_disk = json.loads((round_dir / CMD_FILENAME).read_text(encoding="utf-8"))
        assert on_disk["id"] in {c.id for c in commands}

        # And every loser's cleanup left no temp file behind either.
        leftovers = [p.name for p in round_dir.iterdir() if p.name != CMD_FILENAME]
        assert leftovers == [], f"round {round_i}: temp file(s) left behind: {leftovers}"

    assert race_path_seen, (
        f"the atomic os.link race path (FileExistsError) was never observed across "
        f"{rounds} rounds of {n} threads -- this test would no longer be proving the "
        f"claim it makes"
    )


def test_unclaimed_mailbox_and_lost_race_refusals_are_distinguishable(tmp_path):
    """Two different situations must never collapse into the same text: a
    mailbox left unclaimed implies the MOD may be slow or wedged and is
    worth a caller's attention; a lost race implies nothing about the mod at
    all, just an ordinary sibling sender, and calls for a plain retry. A
    caller (or a human reading a log) must be able to tell which happened.

    It is specifically the HINT comparison that proves this, not the error
    comparison: the error text embeds the mailbox's own path, which differs
    between scenario A and B's directories regardless of whether the
    underlying refusal was merged back into one -- so error != error would
    pass even against broken code. The hint carries no such per-scenario
    substitution, so it is what actually fails if the two messages are ever
    merged again.
    """
    # Scenario A: the exists() pre-check finds a real, earlier command the
    # mod has not claimed yet.
    a_dir = tmp_path / "a"
    a_dir.mkdir()
    ch_a = Channel(a_dir)
    assert ch_a.send(_cmd("ping-1-1"), is_alive=True).ok
    unclaimed = ch_a.send(_cmd("ping-2-1"), is_alive=True)
    assert not unclaimed.ok

    # Scenario B: several sends racing for the SAME empty mailbox. A loser
    # can legitimately land on either refusal (see
    # test_concurrent_sends_exactly_one_succeeds's docstring for why), so a
    # small retry loop over fresh directories is what makes finding a
    # genuine os.link-race loser deterministic rather than a low-probability
    # flake in a test whose whole point is to inspect that specific one.
    lost_race = None
    for attempt in range(10):
        b_dir = tmp_path / f"b-{attempt}"
        b_dir.mkdir()
        _commands, results = _fire_concurrent_sends(b_dir, n=6, tag=f"race-{attempt}")
        lost_race = next(
            (r for r in results if r is not None and not r.ok and "concurrent sender" in r.error),
            None,
        )
        if lost_race is not None:
            break
    assert lost_race is not None, "never observed a genuine lost-race refusal across 10 attempts"

    # The two refusals must be genuinely different HINTS.
    assert unclaimed.hint != lost_race.hint
    assert "picked up" in unclaimed.hint
    assert "picked up" not in lost_race.hint
    assert "concurrent sender" in lost_race.error
    assert "concurrent sender" not in unclaimed.error


def test_send_success_survives_a_failed_temp_file_cleanup(tmp_path, monkeypatch):
    """The reviewer reproduced this with no mocking: an antivirus scanner,
    the search indexer, or a backup agent holding a second handle on the
    temp file (routine on Windows, even in a directory a running server is
    actively writing to) makes the post-link os.remove raise PermissionError.
    That must not turn a successful send into a raised exception -- the
    command already reached the mailbox via os.link by that point, so a
    caller seeing a traceback would wrongly conclude nothing happened and
    retry into a spurious "mailbox occupied" refusal for a command that, in
    fact, already went through.
    """
    from dayz_mcp.bridge import channel as channel_module

    def flaky_remove(path):
        raise PermissionError(32, "used by another process")

    monkeypatch.setattr(channel_module.os, "remove", flaky_remove)

    ch = Channel(tmp_path)
    cmd = _cmd("ping-1-1")

    result = ch.send(cmd, is_alive=True)  # must return, not raise

    assert result.ok
    assert (tmp_path / CMD_FILENAME).exists()
    assert json.loads((tmp_path / CMD_FILENAME).read_text(encoding="utf-8"))["id"] == cmd.id


# --- current_session_id / build_command: stamping the live session --------


def test_current_session_id_is_none_when_nothing_has_been_read(tmp_path):
    ch = Channel(tmp_path)
    assert ch.current_session_id() is None


def test_current_session_id_returns_the_last_published_session(tmp_path):
    ch = Channel(tmp_path)
    _write_state(tmp_path, tick=5, session_id="session-xyz")
    assert ch.current_session_id() == "session-xyz"


def test_build_command_refuses_when_no_session_is_known_yet(tmp_path):
    """The exact refusal shape the coordinator asked for: a caller with
    nothing to stamp a command's session with gets told plainly, before a
    command id is even minted, rather than being handed a Command that
    would only fail later inside send()."""
    ch = Channel(tmp_path)

    result = ch.build_command("spawn", {"class": "Apple"})

    assert not result.ok
    assert "session" in result.error
    assert result.hint


def test_build_command_stamps_the_current_session(tmp_path):
    ch = Channel(tmp_path)
    _write_state(tmp_path, tick=5, session_id="session-xyz")

    result = ch.build_command("spawn", {"class": "Apple"})

    assert result.ok, result.error
    cmd = result.data
    assert cmd.session_id == "session-xyz"
    assert cmd.verb == "spawn"
    assert cmd.args == {"class": "Apple"}
    assert cmd.id.startswith("spawn-")


def test_build_command_then_send_round_trips_the_session_onto_disk(tmp_path):
    """End-to-end: the session build_command stamped in is exactly what
    lands in the mailbox, unmodified by send()."""
    ch = Channel(tmp_path)
    _write_state(tmp_path, tick=5, session_id="session-xyz")

    built = ch.build_command("spawn", {"class": "Apple"})
    assert built.ok, built.error
    sent = ch.send(built.data, is_alive=True)
    assert sent.ok, sent.error

    on_disk = json.loads((tmp_path / CMD_FILENAME).read_text(encoding="utf-8"))
    assert on_disk["session_id"] == "session-xyz"


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


# --- heartbeat: growing / stalled / restarted / unmeasurable ---------------


def test_heartbeat_detects_a_growing_tick(tmp_path):
    ch = Channel(tmp_path)
    _write_state(tmp_path, tick=10, session_id="s1")

    def bump_later():
        time.sleep(0.1)
        _write_state(tmp_path, tick=11, session_id="s1")

    threading.Thread(target=bump_later, daemon=True).start()
    status, tick = ch.heartbeat(window=0.25)

    assert status == HEARTBEAT_GROWING
    assert tick == 11


def test_heartbeat_detects_a_stalled_tick(tmp_path):
    ch = Channel(tmp_path)
    _write_state(tmp_path, tick=42, session_id="s1")

    status, tick = ch.heartbeat(window=0.15)

    assert status == HEARTBEAT_STALLED
    assert tick == 42


def test_heartbeat_with_no_state_file_is_unmeasurable(tmp_path):
    ch = Channel(tmp_path)
    status, tick = ch.heartbeat(window=0.05)
    assert status == HEARTBEAT_UNMEASURABLE
    assert tick == 0


def test_heartbeat_tolerates_a_single_torn_read(tmp_path):
    """A read that fails once (file not there yet -- the same symptom a torn
    write leaves) and recovers shortly after must not be mistaken for a dead
    bridge, only a genuine run of failures should be."""
    ch = Channel(tmp_path)

    def create_soon():
        time.sleep(0.02)
        _write_state(tmp_path, tick=3, session_id="s1")

    threading.Thread(target=create_soon, daemon=True).start()
    status, tick = ch.heartbeat(window=0.3)

    assert tick == 3
    assert status == HEARTBEAT_STALLED  # recovered to the same tick both times, not growth


def test_heartbeat_detects_a_restart_via_changed_session_id(tmp_path):
    """The mod's tick counter restarts at 0 every boot while the profile
    directory (and so the state file) survives a restart -- across a
    restart the published tick can go DOWN, and a naive comparison would
    read a healthy, freshly booted bridge as dead. A changed session_id
    between the two samples must be its own outcome, not folded into
    "stalled" (which implies the same world didn't move) or "growing"
    (which implies meaningful progress on the same world)."""
    ch = Channel(tmp_path)
    _write_state(tmp_path, tick=500, session_id="session-old")

    def reboot_soon():
        time.sleep(0.05)
        _write_state(tmp_path, tick=3, session_id="session-new")  # tick went DOWN

    threading.Thread(target=reboot_soon, daemon=True).start()
    status, tick = ch.heartbeat(window=0.2)

    assert status == HEARTBEAT_RESTARTED
    assert tick == 3  # the new session's own tick -- not compared against the old one


def test_heartbeat_reports_restart_even_when_the_new_tick_is_higher(tmp_path):
    """Session comparison must be checked BEFORE tick comparison: a restart
    where the new session's tick already exceeds the old session's must
    still read as "restarted", not "growing" -- growing would claim
    progress on the SAME world, which is not what happened. The other
    restart test above only exercises a tick going DOWN, where both
    orderings of the two checks happen to agree; swapping them would still
    pass that test but fail this one, which is the point of having it."""
    ch = Channel(tmp_path)
    _write_state(tmp_path, tick=5, session_id="session-old")

    def reboot_soon():
        time.sleep(0.03)
        _write_state(tmp_path, tick=500, session_id="session-new")  # tick went UP

    threading.Thread(target=reboot_soon, daemon=True).start()
    status, tick = ch.heartbeat(window=0.15)

    assert status == HEARTBEAT_RESTARTED
    assert tick == 500


def test_heartbeat_reports_stalled_for_a_backwards_tick_in_the_same_session(tmp_path):
    """Only an INCREASE counts as growth: a same-session tick that moves
    backwards (should never happen for a well-behaved mod, but is not this
    module's job to assume away) reads as "stalled", not "growing" and not
    a misdetected restart either."""
    ch = Channel(tmp_path)
    _write_state(tmp_path, tick=50, session_id="s1")

    def regress_soon():
        time.sleep(0.03)
        _write_state(tmp_path, tick=10, session_id="s1")

    threading.Thread(target=regress_soon, daemon=True).start()
    status, tick = ch.heartbeat(window=0.15)

    assert status == HEARTBEAT_STALLED
    assert tick == 10


def test_heartbeat_reports_unmeasurable_when_the_second_sample_fails(tmp_path):
    """A missing/unreadable SECOND sample is a measurement failure, not
    evidence of a stalled tick -- conflating the two tells a caller the game
    is frozen when the truth may just be "could not check right now" (here:
    the state file was deleted between samples, e.g. mid-restart or because
    the stand went down). A single failed read on the second sample is
    enough to reach this -- read_state returns None for a missing file on
    every one of the tolerant reader's attempts, not just the first."""
    ch = Channel(tmp_path)
    _write_state(tmp_path, tick=7, session_id="s1")

    def delete_soon():
        time.sleep(0.05)
        (tmp_path / STATE_FILENAME).unlink()

    threading.Thread(target=delete_soon, daemon=True).start()
    status, tick = ch.heartbeat(window=0.3)

    assert status == HEARTBEAT_UNMEASURABLE
    assert tick == 7  # the last tick actually read (the FIRST sample), not 0


# --- clear_mailbox: the only way to unwedge a channel -----------------------


def test_clear_mailbox_when_empty_reports_nothing_to_clear(tmp_path):
    ch = Channel(tmp_path)

    result = ch.clear_mailbox()

    assert not result.ok
    assert "empty" in result.error
    assert result.hint


def test_clear_mailbox_discards_a_wedged_command_when_bridge_is_not_alive(tmp_path):
    """The exact scenario clear_mailbox exists for: a command sent while
    nothing is running to claim it. No state file at all means the probe
    never even gets a first sample -- "unmeasurable" with nothing proven
    alive, never "growing"/"restarted" -- so this must be clearable
    WITHOUT force."""
    ch = Channel(tmp_path)
    cmd = _cmd("ping-1-1", args={"x": 1})
    assert ch.send(cmd, is_alive=True).ok

    result = ch.clear_mailbox(probe_window=0.05)

    assert result.ok, result.error
    assert result.data["discarded"]["id"] == cmd.id
    assert result.data["discarded"]["verb"] == cmd.verb
    assert result.data["heartbeat"] == HEARTBEAT_UNMEASURABLE
    assert result.data["override_reason"] is None  # nothing needed overriding
    assert not (tmp_path / CMD_FILENAME).exists()
    # The wedge is gone -- a fresh send() succeeds right away.
    assert ch.send(_cmd("ping-2-1"), is_alive=True).ok


def test_clear_mailbox_proceeds_without_force_when_stalled(tmp_path):
    """A state file that exists but is frozen (same tick, same session,
    across the probe window) is the OTHER shape a genuinely down or
    not-yet-wired bridge can take -- e.g. a stale file left over from a
    previous run. Also clearable without force: "stalled" is not "alive".

    probe_window must be AT LEAST the mod's publish interval here -- a
    shorter window cannot trust its own "stalled" verdict (see
    test_clear_mailbox_requires_force_when_probe_window_is_too_short_to_trust_stalled),
    so this uses one comfortably past it to test the STALLED path
    specifically, not the window-too-short path.
    """
    ch = Channel(tmp_path)
    _write_state(tmp_path, tick=99, session_id="stale-session")
    assert ch.send(_cmd("ping-1-1"), is_alive=True).ok

    result = ch.clear_mailbox(probe_window=1.05)

    assert result.ok, result.error
    assert result.data["heartbeat"] == HEARTBEAT_STALLED
    assert result.data["override_reason"] is None


def test_clear_mailbox_refuses_when_bridge_looks_alive(tmp_path):
    ch = Channel(tmp_path)
    _write_state(tmp_path, tick=10, session_id="s1")
    assert ch.send(_cmd("ping-1-1"), is_alive=True).ok

    def bump_later():
        time.sleep(0.03)
        _write_state(tmp_path, tick=11, session_id="s1")

    threading.Thread(target=bump_later, daemon=True).start()
    result = ch.clear_mailbox(probe_window=0.1)

    assert not result.ok
    assert "alive" in result.error
    assert "force=True" in result.hint
    # Refused -- the command must still be sitting there, untouched.
    assert (tmp_path / CMD_FILENAME).exists()


def test_clear_mailbox_refuses_when_bridge_looks_restarted(tmp_path):
    """The "growing" refusal is tested above; "restarted" is a SEPARATE
    branch of the same guard and needs its own test -- narrowing the guard
    to "growing" alone would leave the rest of the suite unchanged, which
    is exactly the gap a reviewer found by trying it."""
    ch = Channel(tmp_path)
    _write_state(tmp_path, tick=10, session_id="s1")
    assert ch.send(_cmd("ping-1-1"), is_alive=True).ok

    def reboot_soon():
        time.sleep(0.03)
        _write_state(tmp_path, tick=1, session_id="s2")

    threading.Thread(target=reboot_soon, daemon=True).start()
    result = ch.clear_mailbox(probe_window=0.1)

    assert not result.ok
    assert "alive" in result.error
    assert "force=True" in result.hint
    assert (tmp_path / CMD_FILENAME).exists()


def test_clear_mailbox_force_overrides_an_alive_looking_bridge(tmp_path):
    """force=True proceeds anyway, but the result still records that it
    overrode a live-looking bridge -- a forced clear is not silent about
    what it did."""
    ch = Channel(tmp_path)
    _write_state(tmp_path, tick=10, session_id="s1")
    cmd = _cmd("ping-1-1")
    assert ch.send(cmd, is_alive=True).ok

    def bump_later():
        time.sleep(0.03)
        _write_state(tmp_path, tick=11, session_id="s1")

    threading.Thread(target=bump_later, daemon=True).start()
    result = ch.clear_mailbox(force=True, probe_window=0.1)

    assert result.ok, result.error
    assert result.data["discarded"]["id"] == cmd.id
    assert result.data["heartbeat"] == HEARTBEAT_GROWING
    assert result.data["override_reason"] is not None
    assert "alive" in result.data["override_reason"]
    assert not (tmp_path / CMD_FILENAME).exists()


def test_clear_mailbox_requires_force_when_a_readable_first_sample_loses_contact(tmp_path):
    """Reviewer-reproduced without mocks: a live, ticking bridge whose
    SECOND probe sample fails (a real Windows exclusive-handle contender --
    simulated here by deleting the file mid-probe, the same symptom) must
    NOT be treated the same as a stand that was never up at all. A readable
    first sample is proof something was alive inside the window; losing
    contact after that must still require force, unlike a genuinely down
    stand, which never produces even a first sample (see
    test_clear_mailbox_discards_a_wedged_command_when_bridge_is_not_alive,
    unaffected by this guard)."""
    ch = Channel(tmp_path)
    _write_state(tmp_path, tick=101, session_id="s1")
    cmd = _cmd("ping-1-1")
    assert ch.send(cmd, is_alive=True).ok

    def lose_contact_soon():
        time.sleep(0.03)
        (tmp_path / STATE_FILENAME).unlink()

    threading.Thread(target=lose_contact_soon, daemon=True).start()
    result = ch.clear_mailbox(probe_window=0.15)

    assert not result.ok
    assert "alive moments ago" in result.error
    assert "force=True" in result.hint
    assert (tmp_path / CMD_FILENAME).exists()  # refused -- command untouched


def test_clear_mailbox_force_overrides_lost_contact_after_a_readable_first_sample(tmp_path):
    ch = Channel(tmp_path)
    _write_state(tmp_path, tick=101, session_id="s1")
    cmd = _cmd("ping-1-1")
    assert ch.send(cmd, is_alive=True).ok

    def lose_contact_soon():
        time.sleep(0.03)
        (tmp_path / STATE_FILENAME).unlink()

    threading.Thread(target=lose_contact_soon, daemon=True).start()
    result = ch.clear_mailbox(force=True, probe_window=0.15)

    assert result.ok, result.error
    assert result.data["discarded"]["id"] == cmd.id
    # Recorded even though "heartbeat" alone reads as the harmless
    # "unmeasurable" -- the specific reason force was needed must survive.
    assert result.data["heartbeat"] == HEARTBEAT_UNMEASURABLE
    assert result.data["override_reason"] is not None
    assert "alive moments ago" in result.data["override_reason"]
    assert not (tmp_path / CMD_FILENAME).exists()


def test_clear_mailbox_requires_force_when_probe_window_is_too_short_to_trust_stalled(tmp_path):
    """A probe window shorter than the mod's publish interval cannot tell
    "frozen" apart from "alive, but has not ticked again yet" -- both look
    identical (same tick, both samples readable). Without this interlock, a
    genuinely live bridge would silently classify as "stalled" and
    clear_mailbox would destroy an in-flight command without force."""
    ch = Channel(tmp_path)
    _write_state(tmp_path, tick=10, session_id="s1")  # a real, live, ticking bridge
    assert ch.send(_cmd("ping-1-1"), is_alive=True).ok

    result = ch.clear_mailbox(probe_window=0.1)  # far shorter than the 1 Hz publish interval

    assert not result.ok
    assert "publish interval" in result.error
    assert "force=True" in result.hint
    assert (tmp_path / CMD_FILENAME).exists()


def test_clear_mailbox_force_overrides_window_too_short_and_records_it(tmp_path):
    ch = Channel(tmp_path)
    _write_state(tmp_path, tick=10, session_id="s1")
    cmd = _cmd("ping-1-1")
    assert ch.send(cmd, is_alive=True).ok

    result = ch.clear_mailbox(force=True, probe_window=0.1)

    assert result.ok, result.error
    assert result.data["discarded"]["id"] == cmd.id
    assert result.data["heartbeat"] == HEARTBEAT_STALLED
    assert result.data["override_reason"] is not None
    assert "publish interval" in result.data["override_reason"]
    assert not (tmp_path / CMD_FILENAME).exists()


def test_clear_mailbox_reports_what_it_actually_deleted_not_a_stale_read(tmp_path):
    """Reproduced: the mod claims the original command mid-probe, and a
    fresh send() lands a new one in the same window -- clear_mailbox must
    report discarding the NEW command (the one actually removed below),
    never the original, whose content was only ever true at a read that
    would otherwise have happened before the probe even started."""
    ch = Channel(tmp_path)
    original = _cmd("ping-1-1", args={"n": 1})
    assert ch.send(original, is_alive=True).ok

    def claim_then_resend():
        time.sleep(0.03)
        (tmp_path / CMD_FILENAME).unlink()  # the mod "claims" the original
        replacement = _cmd("ping-2-1", args={"n": 2})
        assert ch.send(replacement, is_alive=True).ok

    threading.Thread(target=claim_then_resend, daemon=True).start()
    result = ch.clear_mailbox(probe_window=0.15)

    assert result.ok, result.error
    # Must name the REPLACEMENT command -- the one actually removed -- never
    # the original, which was already gone by the time the unlink happened.
    assert result.data["discarded"]["id"] == "ping-2-1"
    assert not (tmp_path / CMD_FILENAME).exists()


# --- _sample_twice honours window on the FIRST sample too -------------------


def test_heartbeat_honours_window_when_the_first_sample_is_slow_to_appear(tmp_path):
    """_read_state_tolerant's own retry budget is only ~0.1-0.15s -- far
    shorter than a realistic probe window. Before this fix, _sample_twice
    gave up on the FIRST sample after that short budget regardless of
    `window`, so a state file that simply had not been written yet (the
    ORDINARY condition while Task 5's mod is under active development, not
    an edge case) made any longer window meaningless. Here the file appears
    well after that short budget but comfortably inside a longer window."""
    ch = Channel(tmp_path)

    def create_late():
        time.sleep(0.3)  # well past _read_state_tolerant's own ~0.15s budget
        _write_state(tmp_path, tick=5, session_id="s1")

    threading.Thread(target=create_late, daemon=True).start()
    status, tick = ch.heartbeat(window=1.0)

    # Must have found the file at all -- unmeasurable/tick=0 is what the
    # pre-fix code returns here (gives up long before t=0.3).
    assert status != HEARTBEAT_UNMEASURABLE
    assert tick == 5


def test_clear_mailbox_honours_probe_window_when_the_bridge_is_slow_to_become_readable(tmp_path):
    """Reproduced consequence of _sample_twice not honouring window on its
    first sample: a live bridge whose state file is not yet readable when
    clear_mailbox starts (the ORDINARY condition while Task 5's mod is
    being iterated on) used to be misread as a down stand within ~0.15s
    regardless of probe_window, letting clear_mailbox destroy an in-flight
    command without force. With a probe_window long enough to span the
    mod's publish interval, this must find the live bridge and require
    force instead."""
    ch = Channel(tmp_path)
    cmd = _cmd("ping-1-1")
    assert ch.send(cmd, is_alive=True).ok

    def go_live_late():
        time.sleep(0.3)
        _write_state(tmp_path, tick=5, session_id="s1")
        time.sleep(0.3)
        _write_state(tmp_path, tick=6, session_id="s1")

    threading.Thread(target=go_live_late, daemon=True).start()
    result = ch.clear_mailbox(probe_window=1.0)

    assert not result.ok
    assert (tmp_path / CMD_FILENAME).exists()  # NOT destroyed


# --- Channel.read_state_rejection: WHY the state file cannot be read now ----


def test_read_state_rejection_is_none_for_a_valid_state(tmp_path):
    ch = Channel(tmp_path)
    _write_state(tmp_path, tick=7, session_id="s1")
    assert ch.read_state_rejection() is None


def test_read_state_rejection_is_none_for_a_missing_file(tmp_path):
    ch = Channel(tmp_path)
    assert ch.read_state_rejection() is None


def test_read_state_rejection_is_none_for_a_torn_write(tmp_path):
    ch = Channel(tmp_path)
    (tmp_path / STATE_FILENAME).write_text('{"tick": 7, "wor', encoding="utf-8")
    assert ch.read_state() is None  # sanity: this really is the torn-write path
    assert ch.read_state_rejection() is None


def test_read_state_rejection_explains_a_genuine_schema_bug(tmp_path):
    """The exact scenario this exists for: a document that parses as valid
    JSON but fails schema validation -- a real mod-side bug, not a
    mid-write snapshot -- must be explained, not silently folded into
    "try again next tick"."""
    ch = Channel(tmp_path)
    payload = {
        "tick": 7, "session_id": "", "command": None, "errors": [], "world": {}
    }
    (tmp_path / STATE_FILENAME).write_text(json.dumps(payload), encoding="utf-8")

    rejection = ch.read_state_rejection()

    assert rejection is not None
    assert isinstance(rejection, ParseRejection)
    assert rejection.field == "session_id"


# --- heartbeat_detail: exposes the session id(s) M4a asked for --------------


def test_heartbeat_detail_exposes_the_session_id_when_growing(tmp_path):
    ch = Channel(tmp_path)
    _write_state(tmp_path, tick=10, session_id="s1")

    def bump_later():
        time.sleep(0.03)
        _write_state(tmp_path, tick=11, session_id="s1")

    threading.Thread(target=bump_later, daemon=True).start()
    sample = ch.heartbeat_detail(window=0.15)

    assert sample.status == HEARTBEAT_GROWING
    assert sample.tick == 11
    assert sample.session_id == "s1"
    assert sample.previous_session_id is None


def test_heartbeat_detail_exposes_the_session_id_when_stalled(tmp_path):
    ch = Channel(tmp_path)
    _write_state(tmp_path, tick=42, session_id="s1")

    sample = ch.heartbeat_detail(window=0.15)

    assert sample.status == HEARTBEAT_STALLED
    assert sample.session_id == "s1"
    assert sample.previous_session_id is None


def test_heartbeat_detail_exposes_both_session_ids_on_restart(tmp_path):
    """The tool layer has to report the live session -- and, for a restart,
    BOTH halves: what it was, what it is now."""
    ch = Channel(tmp_path)
    _write_state(tmp_path, tick=500, session_id="session-old")

    def reboot_soon():
        time.sleep(0.03)
        _write_state(tmp_path, tick=3, session_id="session-new")

    threading.Thread(target=reboot_soon, daemon=True).start()
    sample = ch.heartbeat_detail(window=0.15)

    assert sample.status == HEARTBEAT_RESTARTED
    assert sample.session_id == "session-new"
    assert sample.previous_session_id == "session-old"


def test_heartbeat_detail_session_id_is_none_when_nothing_was_ever_read(tmp_path):
    ch = Channel(tmp_path)
    sample = ch.heartbeat_detail(window=0.05)

    assert sample.status == HEARTBEAT_UNMEASURABLE
    assert sample.session_id is None
    assert sample.previous_session_id is None


def test_heartbeat_detail_session_id_reflects_the_readable_first_sample_on_lost_contact(tmp_path):
    """Proof-of-life-then-lost-contact still has a session id to report --
    the one from the sample that DID succeed -- not None."""
    ch = Channel(tmp_path)
    _write_state(tmp_path, tick=7, session_id="s1")

    def delete_soon():
        time.sleep(0.05)
        (tmp_path / STATE_FILENAME).unlink()

    threading.Thread(target=delete_soon, daemon=True).start()
    sample = ch.heartbeat_detail(window=0.3)

    assert sample.status == HEARTBEAT_UNMEASURABLE
    assert sample.session_id == "s1"
    assert sample.previous_session_id is None


def test_heartbeat_still_returns_a_plain_two_tuple(tmp_path):
    """heartbeat()'s own public contract must stay exactly (status, tick) --
    unpacking into more than two variables must fail, the same way it would
    have before heartbeat_detail/HeartbeatSample existed. Nothing already
    consuming heartbeat() as a 2-tuple should ever need to change."""
    ch = Channel(tmp_path)
    _write_state(tmp_path, tick=1, session_id="s1")

    result = ch.heartbeat(window=0.05)

    assert isinstance(result, tuple)
    assert len(result) == 2
    status, tick = result  # must not raise ValueError: too many values to unpack
    assert status == HEARTBEAT_STALLED
    assert tick == 1
