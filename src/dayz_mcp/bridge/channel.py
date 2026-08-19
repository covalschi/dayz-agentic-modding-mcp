"""File-touching half of the bridge: writes the command mailbox atomically
and reads the mod's state file tolerantly.

Two facts drive every choice here (see
specs/2026-08-19-dayz-mcp-phase2-bridge.md Sec 3, hub repo):

1. The mod cannot write its state file atomically -- Enforce Script has no
   rename primitive, so it overwrites dayz_mcp_state.json in place once a
   second. Seeing a half-written file is therefore the ordinary case, not an
   error: `protocol.parse_state` already returns None for it, and this
   module treats a single such read as unremarkable. Only a short run of
   failures (see `_read_state_tolerant`) is treated as a real signal.
2. Python CAN write atomically, and must: `send` writes the mailbox to a
   temporary file in the same directory and `os.link`s it into place, so the
   mod never observes a half-written command. `os.link` also doubles as the
   atomic claim that stops two concurrent senders from both succeeding --
   see `send`'s own docstring.
3. The mod's tick counter is scoped to a single boot: it restarts at 0 while
   the state file (living in the profile directory, which the stand keeps
   across restarts) still holds whatever the previous run last wrote. Every
   state therefore also carries `session_id` (see `protocol.BridgeState`),
   an opaque value that changes every boot -- `heartbeat` uses it to tell
   "a new world just came up" apart from "the same world stalled", since a
   plain tick comparison across a restart can go DOWN and reads a freshly
   booted, healthy bridge as dead.
4. The engine gives Enforce Script no boot identity of its own, so a
   command needs the SAME session stamped on it (see `protocol.Command`) --
   without it the mod cannot tell a command meant for it apart from a stale
   one left over from before a restart. `build_command` is where a `Command`
   gets one, from the state the mod most recently published; `send` refuses
   a command whose session is blank as a second line of defence.
5. A dead stand is a wedge waiting to happen: a command written into a
   profile directory nothing is running against sits forever, since only
   the mod (by claiming it) can ever clear it. This module has no notion of
   "is a server alive" of its own and must not import `session` (a
   singleton tied to one running MCP server, layered above this reusable
   primitive) to get one -- so `send` takes `is_alive` as a required
   argument the caller already knows the answer to, rather than guessing.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

from ..errors import Result, fail, ok
from .protocol import BridgeState, Command, CommandState, new_command_id, parse_state

# Wire filenames inside a server's -profiles directory. See spec Sec 5.2.
CMD_FILENAME = "dayz_mcp_cmd.json"
STATE_FILENAME = "dayz_mcp_state.json"

# A torn read of the state file is the ordinary case, once a second, forever
# (fact 1 above). Retrying a few times a short beat apart absorbs that
# without reporting every single stumble as "the bridge is broken"; only
# exhausting every attempt -- a genuine run of failures -- gives up.
_TOLERANT_READ_ATTEMPTS = 3
_TOLERANT_READ_DELAY = 0.05

# Once the mod reports one of these for the command we asked about, waiting
# longer cannot produce more information -- that IS the result.
_TERMINAL_STATUSES = ("done", "failed")

# heartbeat() outcomes. Four, not two: "the tick did not move" and "I could
# not take a second sample" are different claims (see heartbeat's own
# docstring), and "a new world just booted" is different again from either.
HEARTBEAT_GROWING = "growing"
HEARTBEAT_STALLED = "stalled"
HEARTBEAT_RESTARTED = "restarted"
HEARTBEAT_UNMEASURABLE = "unmeasurable"


class Channel:
    """One channel into a single running server's -profiles directory."""

    def __init__(self, profiles_dir: Path) -> None:
        self.profiles_dir = Path(profiles_dir)

    def _cmd_path(self) -> Path:
        return self.profiles_dir / CMD_FILENAME

    def _state_path(self) -> Path:
        return self.profiles_dir / STATE_FILENAME

    def _unclaimed_mailbox_result(self, cmd_path: Path) -> Result:
        """The mailbox already holds a command the mod has not yet claimed
        (the pre-check below found it sitting there before this send even
        tried to write). This means the MOD may be slow or wedged -- there
        is a real previous command still waiting, which is worth a caller's
        attention. Deliberately a different message/hint from
        `_lost_race_result` below: that one is ordinary contention between
        two senders and implies nothing about the mod at all, so a caller
        (or a human reading a log) needs to be able to tell the two apart
        rather than treating both as "something is stuck"."""
        return fail(
            f"mailbox already holds an unclaimed command at {cmd_path}",
            hint="the mod has not picked up the previous command yet -- wait for it to "
                 "be claimed (the mailbox file to disappear) before sending another",
        )

    def _lost_race_result(self, cmd_path: Path) -> Result:
        """A concurrent `send()` call claimed the mailbox microseconds
        before this one did (the atomic `os.link` claim below lost the
        race). This says NOTHING about the mod -- there was no previous
        command sitting unclaimed, and nothing is wedged; some OTHER sender
        simply won first (another thread in this process, or, per `send`'s
        own docstring, even an unrelated process pointed at the same
        mailbox -- `os.link`'s atomicity is not process-scoped, so this is
        not assuming it was a sibling in the same process). The caller's
        response should be an ordinary short retry, not "go investigate a
        stuck mod", which is why this is a distinct message/hint from
        `_unclaimed_mailbox_result` above rather than reusing it."""
        return fail(
            f"lost a race for the mailbox at {cmd_path} to a concurrent sender",
            hint="another send() call claimed the mailbox microseconds ago -- this is "
                 "ordinary contention between senders, not a stuck mailbox; retry shortly, "
                 "once the winning command has been claimed by the mod (or has finished)",
        )

    def current_session_id(self) -> str | None:
        """The `session_id` from the most recent tolerant read of the state
        file, or `None` if no state has ever been read -- or every attempt
        to read it right now failed (see `_read_state_tolerant`).

        This is what a `Command` needs to be accepted by `send` (see
        `build_command`, the recommended way to obtain one already stamped
        with it rather than calling this directly and threading the value
        through by hand)."""
        state = self._read_state_tolerant()
        return state.session_id if state is not None else None

    def build_command(self, verb: str, args: dict) -> Result:
        """Build a `Command` carrying the CURRENT session, refusing if none
        is known yet.

        The engine gives Enforce Script no boot identity of its own, so a
        command with no session (or the wrong one) is indistinguishable to
        the mod from a stale command left over from before a restart --
        carrying the CURRENT session is the ONLY defence against a stale
        command detonating in a freshly booted world (see `Command`'s own
        docstring in protocol.py). `current_session_id` reads it from the
        most recent state the mod itself published; when nothing has been
        published yet (the bridge was never up, or nothing has called
        `heartbeat`/`read_state`/this method against it yet), there is
        nothing to stamp the command with, so this refuses outright rather
        than sending one with a blank or guessed session -- `send` itself
        also refuses a blank session as a second line of defence (see its
        own docstring), but the point of failure a caller actually wants to
        see is here, before a command id is even minted.
        """
        session_id = self.current_session_id()
        if not session_id:
            return fail(
                "the bridge has not published a session yet",
                hint="call bridge_status (or heartbeat) first to confirm the bridge is up "
                     "and has written at least one state snapshot, then build the command "
                     "again",
            )
        return ok(Command(id=new_command_id(verb), session_id=session_id, verb=verb, args=args))

    def send(self, cmd: Command, *, is_alive: bool) -> Result:
        """Write `cmd` into the mailbox, atomically, and refuse if it is
        already occupied by a command the mod has not yet claimed (claiming
        means the mod deletes the file after reading it -- the mod side is
        Task 5).

        `is_alive` is REQUIRED, not defaulted, and this method does not
        determine it itself: a command sent into a dead stand's profile
        directory IS a wedge -- it sits there with nothing alive to ever
        claim it, and every later `send` is refused as "wait for it to be
        claimed" forever, a wait that can never end. `bridge_status`
        already knows how to answer "is a server alive" (`session.server_pid()`
        plus `procs.is_alive`), but this module must not import `session` --
        it is a low-level, reusable primitive; `session` is the opposite, a
        singleton tied to one running MCP server's process-tracking state,
        and importing it here would pull that layering inside-out and risk
        a circular import besides. So the knowledge is threaded in as a
        plain, already-computed bool the caller supplies -- not an injected
        callable stored on the instance, because a fresh `Channel(profiles_dir)`
        is built by every tool call in this codebase anyway (see below), so
        there is no lifetime for a stored callable to usefully outlive a
        single `send`; and not a constructor parameter, because that would
        force every existing construction site (including the many tests
        that never call `send` at all) to thread liveness through just to
        build a `Channel`. A required keyword argument on `send` itself
        confines the cost to exactly the call sites that need it. When
        `is_alive` is false, the command is NOT written -- stated plainly in
        the refusal, because a caller who thinks it MIGHT have been written
        will not retry, and a wedge nothing can ever clear is worse than a
        refusal that is clear about what did not happen.

        `cmd.session_id` must also be non-empty: a command with no session
        is indistinguishable to the mod from a stale one (see `Command`'s
        docstring), so this refuses rather than let the Python side
        accidentally produce one -- `build_command` is the recommended way
        to avoid ever reaching this refusal in practice.

        A plain "does the file exist, then write it" check-then-act is NOT
        enough here: two `send()` calls on separate threads (this server
        dispatches tool calls through a thread pool, so this is the expected
        case once Task 4 wires callers in, not a hypothetical) can both pass
        the check while the mailbox is still empty and both write, and one
        silently overwrites the other's command with no error anywhere --
        confirmed by firing 8 concurrent sends, which let 2-4 through as
        silent successes.

        The fix: write the full content to a temp file first, then claim the
        mailbox name with `os.link`, not `os.replace`. A hard link creation
        is atomic with respect to the destination's existence -- the
        filesystem itself either creates the name (nothing there yet) or
        fails with FileExistsError (something already claimed it) as one
        indivisible operation, so two racing claims cannot both win. This
        works identically on POSIX and Windows/NTFS, needs no lock object
        shared across `Channel` instances (a fresh `Channel(profiles_dir)`
        built by every tool call, as this codebase does, still lines up
        against the same directory entry), and holds even across two
        unrelated processes on the same machine pointed at the same stand --
        `os.replace`'s destination-overwrite semantics gave none of that.
        """
        if not is_alive:
            return fail(
                "refusing to send: no live server is tracked for this bridge -- the "
                "command was NOT written",
                hint="start (or reconnect to) a live server first; writing into a dead "
                     "stand's profile directory would create a mailbox nothing can ever "
                     "claim, which every later send() would then refuse as 'unclaimed' "
                     "forever",
            )
        if not cmd.session_id:
            return fail(
                "refusing to send a command with no session_id -- the mod cannot tell it "
                "apart from a stale command left over from a previous boot",
                hint="build the command with Channel.build_command(verb, args), which "
                     "stamps the current session in automatically (and refuses on its own "
                     "if no session has been published yet)",
            )

        cmd_path = self._cmd_path()
        # Fast path only: skip the temp-file write in the common case where
        # the mailbox is visibly still occupied. This check is racy by
        # itself and is NOT what makes concurrent sends safe -- the os.link
        # claim below is the actual guard, and runs even when this check
        # says "empty".
        if cmd_path.exists():
            return self._unclaimed_mailbox_result(cmd_path)

        try:
            fd, tmp_name = tempfile.mkstemp(
                dir=self.profiles_dir, prefix=".dayz_mcp_cmd_", suffix=".tmp"
            )
        except OSError as exc:
            return fail(
                f"failed to create the mailbox write: {exc}",
                hint=f"check that {self.profiles_dir} exists and is writable",
            )

        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(cmd.to_json())
            os.link(tmp_name, cmd_path)
        except FileExistsError:
            # Lost the race: someone else's os.link claimed cmd_path between
            # our exists() check and this one. A DIFFERENT refusal from the
            # fast-path case above -- see _lost_race_result's docstring for
            # why the two must not be conflated.
            try:
                os.remove(tmp_name)
            except OSError:
                pass
            return self._lost_race_result(cmd_path)
        except OSError as exc:
            try:
                os.remove(tmp_name)
            except OSError:
                pass
            return fail(
                f"failed to write mailbox: {exc}",
                hint=f"check that {self.profiles_dir} exists and is writable, and that its "
                     "filesystem supports hard links -- NTFS does, FAT/exFAT and some "
                     "network shares do not (os.link fails there for every command, not "
                     "just this one)",
            )
        else:
            # cmd_path now holds its own hard link to the same content --
            # delivery already succeeded. Removing the temp name from here
            # on is cleanup, not delivery: a second handle held on tmp_name
            # by something else (a virus scanner, the search indexer, a
            # backup agent -- all routine on Windows, including inside a
            # directory a running server is actively writing to) makes
            # os.remove raise PermissionError. That must never turn a
            # successful send into a raised exception -- the caller would
            # see a traceback, assume nothing happened, and retry into a
            # spurious "mailbox occupied" refusal for a command that in fact
            # already went through. A stray temp file left behind here is
            # harmless; failing this cleanup is deliberately not an error.
            try:
                os.remove(tmp_name)
            except OSError:
                pass
        return ok(cmd.id)

    def clear_mailbox(self, force: bool = False, probe_window: float = 3.0) -> Result:
        """Discard whatever the mailbox currently holds -- the only way to
        unwedge a channel that nothing else can ever clear.

        `send` is the only writer of the mailbox and never removes it:
        "claimed" is defined entirely as the mod deleting the file after
        reading it (Task 5). A command sent while the stand is down, or
        before the bridge mod is wired into -serverMod, therefore sits
        there forever -- every later `send` returns the mailbox-occupied
        refusal, and nothing in the running system can ever clear it on its
        own. This is that escape hatch, and deliberately not automatic:
        nothing else in this module calls it, so a wedge is never silently
        cleared as a side effect of something else -- a caller (the tool
        layer, ultimately a human) has to decide to call this.

        Refuses to act, and reports what it found, unless `force=True`, in
        TWO situations that both mean "something may claim this any
        moment": `_classify_samples` reporting `"growing"`/`"restarted"`
        (the bridge is visibly alive), and a subtler one this method checks
        directly rather than trusting `heartbeat`'s own reduction of the
        same two samples -- a readable FIRST sample followed by a failed
        SECOND one. `heartbeat` reports both that case and "never got any
        sample at all" as the same `"unmeasurable"`, which is exactly right
        for `heartbeat`'s own job (a caller just wants to know "is it
        growing", and both are equally "no"), but wrong for THIS method's
        job: a readable first sample is proof something was alive inside
        the probe window, which a genuinely down stand or an unwired bridge
        never produces even once. Reproduced without mocks: while the tick
        was genuinely advancing, a second sample blocked by another process
        holding an exclusive handle on the state file (the same real
        Windows condition this module has already had to account for
        elsewhere) made the reduced status read `"unmeasurable"` -- and
        without checking the raw samples here, `force` was never required
        to destroy a command a live bridge was about to claim. The
        genuinely-down-stand path is untouched: it never produces even a
        first sample, so it is unaffected by this check and still proceeds
        without `force`.

        On success, reports the discarded command (parsed back from its
        JSON: id, verb, args) so a caller can tell whether the thing just
        thrown away was the one they cared about -- read IMMEDIATELY before
        the `unlink` below, not at the top of this method. Reading early and
        reporting that is a real, reproduced bug, not a theoretical one: the
        probe can run for seconds, during which the mod can claim the
        original command AND a fresh `send` can land a new one in the same
        window, at which point an early read reports discarding the OLD
        command while the file actually removed holds the NEW one -- a
        caller trusting the report would believe it recovered what it
        wanted when the real, current command was destroyed instead. Also
        includes the heartbeat status the probe saw, recorded even when
        `force=True` bypassed a refusal, so a forced clear still leaves a
        record of what it overrode.
        """
        cmd_path = self._cmd_path()
        if not cmd_path.exists():
            return fail(
                f"mailbox at {cmd_path} is already empty",
                hint="there is nothing to clear -- send() would succeed right now",
            )

        before, after = self._sample_twice(probe_window)
        status, tick = self._classify_samples(before, after)
        # See this method's own docstring: a readable first sample proves
        # something was alive inside the window even though the second read
        # then failed, which `status == "unmeasurable"` alone cannot say.
        proof_of_life_then_lost_contact = before is not None and after is None
        if not force and (
            status in (HEARTBEAT_GROWING, HEARTBEAT_RESTARTED) or proof_of_life_then_lost_contact
        ):
            if proof_of_life_then_lost_contact:
                reason = (
                    f"a readable state was seen (tick={tick}) inside the probe window, but "
                    f"a second sample could not be read within {probe_window}s -- this "
                    f"proves something was alive moments ago, not a downed stand"
                )
            else:
                reason = f"the bridge looks alive (heartbeat={status!r}, tick={tick})"
            return fail(
                f"refusing to clear the mailbox at {cmd_path}: {reason} and may claim "
                f"this command any moment",
                hint="pass force=True if you are certain this command should be discarded "
                     "anyway; check bridge_status/world_state first if you are not sure",
            )

        # Re-read HERE, immediately before the unlink -- see this method's
        # docstring for the reproduced bug this ordering fixes. Up to
        # probe_window seconds have passed since the exists() check above.
        try:
            raw = cmd_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ok({"discarded": None, "heartbeat": status,
                       "note": "mailbox was claimed or cleared concurrently"})
        except OSError as exc:
            return fail(
                f"failed to read the mailbox to clear it: {exc}",
                hint=f"check that {cmd_path} is accessible",
            )

        try:
            discarded = json.loads(raw)
        except json.JSONDecodeError:
            # send() only ever writes complete, valid JSON in one atomic
            # step -- a parse failure here means something other than this
            # module put content in the mailbox. Report the raw text rather
            # than silently losing it.
            discarded = {"raw": raw}

        try:
            cmd_path.unlink()
        except FileNotFoundError:
            # Claimed (by the mod) or cleared (by a concurrent caller)
            # between the read just above and this unlink -- the wedge is
            # gone either way, which is the outcome this method exists to
            # reach. (This particular race's window is one read/unlink pair
            # apart, not the multi-second probe -- see the docstring.)
            return ok({"discarded": None, "heartbeat": status,
                       "note": "mailbox was claimed or cleared concurrently"})
        except OSError as exc:
            return fail(
                f"failed to remove the mailbox: {exc}",
                hint=f"check that {cmd_path} is writable",
            )
        return ok({"discarded": discarded, "heartbeat": status})

    def read_state(self) -> BridgeState | None:
        """One read of the mod's state file. None covers every unusable
        outcome -- missing file, a torn write mid-overwrite, even a
        multi-byte character split by that same torn write -- because none
        of them is distinguishable from "try again next tick"."""
        try:
            text = self._state_path().read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None
        return parse_state(text)

    def _read_state_tolerant(self) -> BridgeState | None:
        """`read_state`, but absorbs a short run of torn reads instead of
        surfacing the first one. See the module docstring's fact 1."""
        for attempt in range(_TOLERANT_READ_ATTEMPTS):
            state = self.read_state()
            if state is not None:
                return state
            if attempt < _TOLERANT_READ_ATTEMPTS - 1:
                time.sleep(_TOLERANT_READ_DELAY)
        return None

    def await_result(self, cmd_id: str, timeout: float, poll: float = 0.5) -> CommandState | None:
        """Wait for the mod to report a terminal status for `cmd_id`.

        A state reporting on a different command id tells us nothing about
        ours -- Task 2 shipped no correlation helper by design, leaving this
        check here -- so it is never returned, whether it turns up mid-wait
        or is the last thing seen when the timeout expires. On timeout, the
        last state actually observed FOR cmd_id is returned, even if it is
        still "running", rather than an invented failure: the caller decides
        what an unfinished command means, the same principle JobStore.wait
        follows for background jobs.
        """
        deadline = time.monotonic() + timeout
        last_own: CommandState | None = None
        while True:
            state = self.read_state()
            if state is not None and state.command is not None and state.command.id == cmd_id:
                last_own = state.command
                if last_own.status in _TERMINAL_STATUSES:
                    return last_own
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return last_own
            time.sleep(max(0.0, min(poll, remaining)))

    def _sample_twice(self, window: float) -> tuple[BridgeState | None, BridgeState | None]:
        """Take two tolerant samples `window` seconds apart -- the shared
        timing/retry logic behind `heartbeat`. Returns `(before, after)`,
        either of which may be `None`. Exposed as its own method (not
        inlined into `heartbeat`) because `clear_mailbox` needs the RAW
        samples, not just heartbeat's 4-outcome reduction of them -- see
        `_classify_samples` and `clear_mailbox`'s own docstring for why."""
        deadline = time.monotonic() + window
        before = self._read_state_tolerant()
        if before is None:
            return None, None

        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)

        after = self._read_state_tolerant()
        return before, after

    def _classify_samples(
        self, before: BridgeState | None, after: BridgeState | None
    ) -> tuple[str, int]:
        """Reduce two samples (as returned by `_sample_twice`) to
        `heartbeat`'s 4-outcome contract. Pure function of its two
        arguments, shared by `heartbeat` and `clear_mailbox` so the
        classification rule lives in exactly one place.

        Session comparison is checked BEFORE tick comparison, deliberately:
        a session id change means a new world came up, and that must read
        as "restarted" even if the new world's tick happens to already
        exceed the old one's (a smaller old tick does not make the
        comparison "growing" -- it says nothing about progress on the OLD
        world, which is what "growing" would claim). Only ONE ordering was
        ever pinned by a test before this fix-round -- a restart where the
        tick goes down, which both orderings happen to agree on; a restart
        where the tick goes up is the case that actually distinguishes them.
        "stalled" covers same-session tick that did not increase, including
        the (should-never-happen-but-not-this-module's-job-to-assume-away)
        case of a same-session tick moving backwards -- not just "unchanged"
        as an earlier version of this docstring said.
        """
        if before is None:
            return HEARTBEAT_UNMEASURABLE, 0
        if after is None:
            return HEARTBEAT_UNMEASURABLE, before.tick
        if after.session_id != before.session_id:
            return HEARTBEAT_RESTARTED, after.tick
        if after.tick > before.tick:
            return HEARTBEAT_GROWING, after.tick
        return HEARTBEAT_STALLED, after.tick

    def heartbeat(self, window: float = 3.0) -> tuple[str, int]:
        """Is the world's tick counter growing over `window` seconds -- and
        is it still the SAME world?

        The tick is the only proof the world is running -- a state file that
        merely exists proves nothing, since the mod rewrites it whether the
        world is progressing or hung. But tick alone is not enough either:
        it restarts at 0 every boot while the profile directory (and so the
        state file) survives a restart, so a plain before/after tick
        comparison reads a freshly booted, healthy bridge as dead -- 0 is
        never greater than whatever the previous run last wrote. Every
        state also carries `session_id` (module fact 3 / `BridgeState`),
        which changes every boot; a changed session id between the two
        samples means "a new world came up in between", not "this one
        froze".

        Both samples go through `_read_state_tolerant`, so one torn read
        landing at exactly the wrong moment is not mistaken for a dead
        bridge. But tolerance has a limit, and reaching it is itself
        information the caller needs: if the SECOND sample never comes back
        readable within its own retry budget, that is a measurement
        failure, not evidence the tick stood still -- collapsing the two
        would tell a caller the world is frozen when the truth may simply be
        "could not check just now" (e.g. the state file was deleted, or the
        stand is mid-restart, between the two samples).

        Returns (status, tick):
          "growing"      -- same session, tick increased: alive, progressing.
          "stalled"      -- same session, tick did not increase (unchanged,
                             or -- in principle -- went backwards without a
                             session change; only an INCREASE counts as
                             progress, so anything else here is "stalled",
                             not a third thing).
          "restarted"    -- session id changed between samples: a NEW world
                             came up, not a stall on the old one. `tick` is
                             the new session's own tick -- there is nothing
                             meaningful to compare it against across a
                             restart, so it is not compared. Checked BEFORE
                             tick, so this outcome does not depend on which
                             direction the tick happened to move.
          "unmeasurable" -- fewer than two readable samples were obtained
                             (the state file was never readable within
                             `window`, or the second read failed after the
                             first one succeeded). Distinct from "stalled":
                             stalled means the same world was observed twice
                             and did not move; this means no comparison
                             could be made at all. `tick` is the last tick
                             actually read, or 0 if nothing was ever read.
                             NOTE for callers deciding what "unmeasurable" is
                             safe to do next: it does NOT distinguish "never
                             got any sample" (nothing proven alive) from "got
                             a first sample, then lost contact" (proof of
                             life moments ago) -- `clear_mailbox` needs that
                             distinction and gets it from `_sample_twice`
                             directly rather than from this method.
        """
        before, after = self._sample_twice(window)
        return self._classify_samples(before, after)
