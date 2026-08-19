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
from dataclasses import dataclass
from pathlib import Path

from ..errors import Result, fail, ok
from .protocol import (
    BridgeState,
    Command,
    CommandState,
    ParseRejection,
    new_command_id,
    parse_rejection,
    parse_state,
)

# Wire filenames inside a server's -profiles directory. See spec Sec 5.2.
CMD_FILENAME = "dayz_mcp_cmd.json"
STATE_FILENAME = "dayz_mcp_state.json"

# A torn read of the state file is the ordinary case, once a second, forever
# (fact 1 above). Retrying a few times a short beat apart absorbs that
# without reporting every single stumble as "the bridge is broken"; only
# exhausting every attempt -- a genuine run of failures -- gives up.
_TOLERANT_READ_ATTEMPTS = 3
_TOLERANT_READ_DELAY = 0.05

# The mod publishes its tick once per second (spec Sec 5.1: CallLater on
# CALL_CATEGORY_SYSTEM, 1 Hz -- see Task 1's measurement). A probe window
# shorter than this cannot tell "frozen" apart from "alive, but has not had
# a chance to tick again yet" -- both look identical (same tick, both
# samples readable). Used by `clear_mailbox` to require `force` for a
# "stalled" verdict a too-short window cannot actually back up.
_MOD_PUBLISH_INTERVAL_SECONDS = 1.0

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


@dataclass(frozen=True)
class HeartbeatSample:
    """The full result of one heartbeat probe: `status`/`tick` (see
    `heartbeat`'s own docstring for exactly what these mean) plus the
    session id(s) observed and the measured `gap` -- information
    `heartbeat`'s plain `(status, tick)` contract has no room for, and
    which the tool layer needs both to report the live session to a caller
    (several of Task 5's acceptance probes need to know it) and to tell
    apart two situations that otherwise arrive identically.

    `session_id` is the most recently observed session: `after.session_id`
    if the second sample was read, else `before.session_id` if only the
    first was, else `None` if neither was (mirrors `tick`'s own "last
    actually read, or 0" rule). `previous_session_id` is populated ONLY
    when `status == "restarted"` -- the OLD session's id, so a caller can
    report both halves of a restart (what it was, what it is now), not just
    that one happened; `None` in every other case, including when there is
    no session to report at all.

    `gap` is the ACTUAL measured wall-clock time, in seconds, between the
    two reads that produced this sample -- see `Channel._sample_twice`'s
    own docstring for why this must be measured, never inferred by a
    caller comparing against the `window` it originally asked for. `None`
    only when no measurement was possible at all (the FIRST sample itself
    never came back -- `status == "unmeasurable"` with `session_id is
    None` too); a real float in every other case, including every other
    `"unmeasurable"` shape.

    This is what lets a caller tell apart two situations that would
    otherwise both arrive as `"unmeasurable"` with an identical `tick`/
    `session_id`: a `gap` under `_MOD_PUBLISH_INTERVAL_SECONDS` alongside a
    real `session_id` means the mod IS there, just not measured for long
    enough -- ask again with a bigger window, and that alone is likely to
    fix it. A `gap` at or above the publish interval with `status ==
    "unmeasurable"` means something else happened: the second sample was
    attempted for at least a full publish interval and still failed -- the
    state file went away, or stopped parsing, mid-probe. That is a fact
    about the MOD, not about the window, and a bigger window will not fix
    it -- collapsing the two into the same fields would have told a caller
    to enlarge a window that was never the problem, a smaller version of
    the false diagnosis the gap-awareness fix (see `_classify_samples`)
    exists to prevent in the first place.
    """

    status: str
    tick: int
    session_id: str | None
    gap: float | None
    previous_session_id: str | None = None


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

        NOTE, not a defect today: a bool over a callable trades away
        lifetime concerns, but not EVALUATION POINT. `is_alive` is computed
        by the caller before this call -- typically before `build_command`
        too, which itself spends up to `_TOLERANT_READ_ATTEMPTS *
        _TOLERANT_READ_DELAY` (currently ~0.1s) on its own tolerant read.
        A server that dies in that window is not re-checked: `is_alive`
        stays whatever it was computed as, and `send` writes the command
        the "no live server" gate exists specifically to prevent. Narrow,
        and not worth a callable (which would only move WHEN the check
        runs, not eliminate the gap -- the server could still die between
        the callable firing and the write completing), but real; worth
        knowing rather than rediscovering.

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
        reading it (Task 5). A command sent while nothing is running to
        claim it therefore sits there forever -- every later `send` returns
        the mailbox-occupied refusal, and nothing at the filesystem level
        can ever clear it on its own (the tool layer may add its own
        additional gating on top of this -- e.g. a `force` case for a
        bridge that was never wired into `-serverMod` at all -- but that is
        a decision made above this method, not a claim this docstring
        should make on its behalf). This is that escape hatch, and
        deliberately not automatic: nothing else in this module calls it,
        so a wedge is never silently cleared as a side effect of something
        else -- a caller (the tool layer, ultimately a human) has to decide
        to call this.

        Refuses to act, and reports what it found, unless `force=True`, in
        THREE situations that all mean "this verdict cannot be trusted
        enough to destroy something":

        1. `_classify_samples` reporting `"growing"`/`"restarted"` -- the
           bridge is visibly alive and could claim the mailbox any moment.
        2. A readable FIRST sample followed by a failed SECOND one, checked
           directly against the raw samples rather than trusting
           `heartbeat`'s own reduction of them. `heartbeat` reports both
           that case and "never got any sample at all" as the same
           `"unmeasurable"` -- exactly right for `heartbeat`'s own job (a
           caller just wants to know "is it growing", and both are equally
           "no"), but wrong here: a readable first sample is proof
           something was alive inside the probe window, which a genuinely
           down stand or an unwired bridge never produces even once.
           Reproduced without mocks: while the tick was genuinely
           advancing, a second sample blocked by another process holding an
           exclusive handle on the state file (the same real Windows
           condition this module has already had to account for elsewhere)
           made the reduced status read `"unmeasurable"` -- and without
           checking the raw samples here, `force` was never required to
           destroy a command a live bridge was about to claim.
        3. Both samples readable, but `_classify_samples` itself already
           answered `"unmeasurable"` rather than `"stalled"` because the
           MEASURED gap between the two reads (see `_sample_twice`'s
           docstring -- NOT the requested `probe_window`, which can be much
           larger than the actual gap when the first sample is slow to
           appear) was under the mod's publish interval. Fixing the
           classifier to return this honest answer is NOT sufficient here
           on its own: with both samples readable, case 2 above does not
           fire (`after` is not `None`), and a plain `"unmeasurable"` alone
           looks exactly like case 2's opposite (a genuinely down stand,
           which also reports `"unmeasurable"` but is safe to clear) --
           this method has to tell the two `"unmeasurable"` shapes apart
           itself, by whether BOTH samples actually came back, not just
           trust the label. Reproduced without mocks: a bridge that came up
           1.8s into a 2.0s probe (mirroring the case `_sample_twice`'s own
           docstring describes -- the ordinary shape of the FIRST
           `bridge_status` poll after any boot) let a forced-looking clear
           through with `override_reason: None`, destroying an in-flight
           command on a live bridge with no record of what happened.

        The genuinely-down-stand path (case 1's opposite: no first sample
        at all) is untouched by cases 2 and 3 -- it never produces even a
        first sample, so it is unaffected and still proceeds without
        `force`, no matter how short `probe_window` is or how short the
        resulting `gap` would have been.

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
        wanted when the real, current command was destroyed instead.

        Also includes `"heartbeat"` (the plain 4-outcome status the probe
        saw) and `"override_reason"` -- `None` when nothing needed
        overriding, otherwise the SPECIFIC reason `force` was required,
        even though `"heartbeat"` alone might read as the harmless
        `"unmeasurable"` or `"stalled"` (cases 2 and 3 above cannot be told
        apart from their safe counterparts by `"heartbeat"` alone -- that is
        the whole reason this method checks them separately). A forced
        clear is therefore never silent about what it overrode, and a
        caller that only wants to know "did this override something
        questionable" can check `override_reason is not None` without
        having to re-derive cases 2/3 itself.
        """
        cmd_path = self._cmd_path()
        if not cmd_path.exists():
            return fail(
                f"mailbox at {cmd_path} is already empty",
                hint="there is nothing to clear -- send() would succeed right now",
            )

        before, after, gap = self._sample_twice(probe_window)
        sample = self._classify_samples(before, after, gap)
        status, tick = sample.status, sample.tick

        # Case 2 (see docstring): a readable first sample proves something
        # was alive inside the window even though the second read then
        # failed, which `status == "unmeasurable"` alone cannot say.
        proof_of_life_then_lost_contact = before is not None and after is None
        # Case 3 (see docstring): BOTH samples readable but _classify_samples
        # already downgraded a same-tick result to "unmeasurable" because the
        # MEASURED gap was too short to trust -- the only way to reach
        # "unmeasurable" with both before and after non-None. Must be told
        # apart from case 2's genuinely-safe opposite (before is None, no
        # sample at all) by checking the raw samples, not the label alone.
        gap_too_short_to_trust_stalled = (
            status == HEARTBEAT_UNMEASURABLE and before is not None and after is not None
        )

        if status in (HEARTBEAT_GROWING, HEARTBEAT_RESTARTED):
            override_reason = f"the bridge looks alive (heartbeat={status!r}, tick={tick})"
        elif proof_of_life_then_lost_contact:
            override_reason = (
                f"a readable state was seen (tick={tick}) inside the probe window, but "
                f"a second sample could not be read within {probe_window}s -- this "
                f"proves something was alive moments ago, not a downed stand"
            )
        elif gap_too_short_to_trust_stalled:
            override_reason = (
                f"both samples were readable, but the MEASURED gap between them "
                f"({gap:.2f}s) is shorter than the mod's publish interval "
                f"({_MOD_PUBLISH_INTERVAL_SECONDS}s) -- a same tick proves nothing at "
                f"this gap; a live bridge that simply has not ticked again yet looks "
                f"identical to a frozen one"
            )
        else:
            override_reason = None  # genuinely safe to clear without force

        if override_reason is not None and not force:
            return fail(
                f"refusing to clear the mailbox at {cmd_path}: {override_reason} and may "
                f"claim this command any moment",
                hint="pass force=True if you are certain this command should be discarded "
                     "anyway; check bridge_status/world_state first if you are not sure",
            )

        # Re-read HERE, immediately before the unlink -- see this method's
        # docstring for the reproduced bug this ordering fixes. Up to
        # probe_window seconds have passed since the exists() check above.
        try:
            raw = cmd_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ok({"discarded": None, "heartbeat": status, "override_reason": override_reason,
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
            return ok({"discarded": None, "heartbeat": status, "override_reason": override_reason,
                       "note": "mailbox was claimed or cleared concurrently"})
        except OSError as exc:
            return fail(
                f"failed to remove the mailbox: {exc}",
                hint=f"check that {cmd_path} is writable",
            )
        return ok({"discarded": discarded, "heartbeat": status, "override_reason": override_reason})

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

    def read_state_rejection(self) -> ParseRejection | None:
        """Explain why the state file cannot be read RIGHT NOW, for the
        diagnostic case only -- not the routine polling loop, which
        `read_state`/`_read_state_tolerant` already serve correctly on
        their own. Before this existed, every realistic mod-side schema
        slip during Task 5 (`session_id: ""`, `tick: "7"`, a typo'd
        `status`) was reported to the person debugging it as an ordinary
        torn write, with a hint naming the wrong cause and the wrong
        remedy ("rebuild the mod") -- because `parse_state`'s plain `None`
        cannot tell "caught mid-write, try again next tick" apart from "this
        document will NEVER parse, something is actually wrong". This can.

        Returns `None` for a missing file (nothing to explain: `read_state`
        already says so) and for a torn write (see `protocol.parse_rejection`
        -- the ordinary, once-a-second condition this whole module treats as
        unremarkable, deliberately never detailed). Returns a populated
        `ParseRejection` only when the state file parses as valid JSON but
        fails schema validation -- the one case worth surfacing, because
        retrying will not fix it.

        A SINGLE read, not tolerant like `_read_state_tolerant`: retrying a
        persistent schema failure would not change the answer (unlike a
        torn write, which `parse_rejection` already refuses to detail on
        its own), so there is nothing extra tolerance would buy a caller
        who is diagnosing an ALREADY-persistent failure.
        """
        try:
            text = self._state_path().read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None
        return parse_rejection(text)

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

    def _sample_twice(
        self, window: float
    ) -> tuple[BridgeState | None, BridgeState | None, float]:
        """Take two tolerant samples, up to `window` seconds apart -- the
        shared timing/retry logic behind `heartbeat`. Returns `(before,
        after, gap)`: `before`/`after` may be `None`; `gap` is the ACTUAL
        measured wall-clock time between the two reads, in seconds (`0.0`
        when `before` is `None`, since no second read is even attempted
        then).

        `gap` exists because it can be much smaller than `window`, and a
        caller finding that out for itself (by comparing against the window
        it originally asked for) gets it wrong -- see the CRITICAL bug this
        docstring paragraph replaced: the FIRST sample retries to `window`'s
        deadline (below), but the SECOND sample is then taken AT THAT SAME
        deadline -- so the true gap between the two reads is
        `window - time_until_the_first_sample_succeeded`, not `window`.
        Whenever a slow-to-appear first sample eats most of the window, the
        remaining gap can fall under the mod's 1 Hz publish interval, and
        two reads that close together show the SAME tick whether the world
        is frozen OR simply has not had a chance to tick again yet -- an
        earlier version of `_classify_samples` could not tell those apart
        because it was never told the gap, only the ticks. Reproduced with
        real files and a real clock: a bridge appearing 1.8s into a 2.0s
        window (and 1.5/2.0, 2.5/3.0, 9.5/10.0) all read "frozen" -- the
        exact wrong diagnosis this module exists to prevent, and not a
        corner case: "the state file appears partway through the probe" is
        exactly the shape of the FIRST `bridge_status` poll after every
        boot, since the mod publishes once at init and then ticks at 1 Hz.
        Callers needing "did the tick genuinely fail to move" must compare
        against THIS `gap`, never against the `window` they asked for --
        `_classify_samples` does exactly that.

        The FIRST sample retries all the way to `window`'s own deadline,
        not just `_read_state_tolerant`'s own short (~0.1-0.15s) budget.
        Giving up on the first sample after that short budget regardless of
        a much longer `window` silently defeated `window` on this path
        entirely: `clear_mailbox` could still destroy an in-flight command
        on a genuinely live bridge whenever the state file merely happened
        to be unreadable at the exact moment the probe started, and ANY
        `probe_window` shorter than the time it took the state file to
        become readable again had the exact same effect as `probe_window`
        not existing. This is not an exotic timing edge case: while a mod
        is under active development (Task 5), a state file that is
        unreadable right now -- because the mod has not started writing it
        yet, or is mid-crash-and-recover -- is the ORDINARY condition, and
        `clear_mailbox` is exactly the tool its author reaches for then.
        The SECOND sample keeps its original short budget beyond `window`'s
        deadline (`_read_state_tolerant`'s own retries): `heartbeat`'s own
        docstring already treats a failed second sample as its own outcome
        ("unmeasurable") rather than something worth waiting out further --
        extending `window` itself to guarantee a bigger `gap` would break
        the bounded-time promise this method makes; reporting the honest,
        possibly-too-small `gap` instead is what keeps that promise while
        also staying truthful.
        """
        deadline = time.monotonic() + window
        before = self._read_state_tolerant()
        while before is None and time.monotonic() < deadline:
            time.sleep(_TOLERANT_READ_DELAY)
            before = self._read_state_tolerant()
        if before is None:
            return None, None, 0.0
        before_time = time.monotonic()

        remaining = deadline - before_time
        if remaining > 0:
            time.sleep(remaining)

        after = self._read_state_tolerant()
        gap = time.monotonic() - before_time
        return before, after, gap

    def _classify_samples(
        self, before: BridgeState | None, after: BridgeState | None, gap: float
    ) -> HeartbeatSample:
        """Reduce two samples (as returned by `_sample_twice`) to
        `heartbeat`'s 4-outcome contract, PLUS the session id(s) observed
        (see `HeartbeatSample`). Pure function of its arguments, shared by
        `heartbeat` and `clear_mailbox` so the classification rule lives in
        exactly one place.

        Session comparison is checked BEFORE tick comparison, deliberately:
        a session id change means a new world came up, and that must read
        as "restarted" even if the new world's tick happens to already
        exceed the old one's (a smaller old tick does not make the
        comparison "growing" -- it says nothing about progress on the OLD
        world, which is what "growing" would claim).

        A same-session tick that did NOT increase is "stalled" only when
        `gap` (the MEASURED time between the two reads -- see
        `_sample_twice`'s docstring for why this must be measured, not
        assumed from the requested window) is at least
        `_MOD_PUBLISH_INTERVAL_SECONDS`. Below that, the mod genuinely has
        not had a fair chance to write a new tick yet, so "the same tick
        twice" is not evidence of a stall -- it is evidence of nothing, and
        reporting "stalled" there was the exact wrong-diagnosis bug this
        gap-awareness exists to close. That case reports "unmeasurable"
        instead (with `after`'s tick/session still, since both reads DID
        succeed -- only the comparison between them is untrustworthy, not
        the values themselves). GROWING and RESTARTED are never subject to
        this: an observed increase, or an observed session change, is real
        evidence regardless of how little time passed to observe it -- only
        the ABSENCE of a visible change is ambiguous under too short a gap.

        `gap` is threaded onto the returned `HeartbeatSample` in every
        branch where a measurement was even attempted (every branch except
        `before is None`) -- see `HeartbeatSample`'s own docstring for why
        this specific field is what lets a caller tell "gap too short, ask
        again with a bigger window" apart from "second sample lost, a fact
        about the mod" when both would otherwise collapse into the same
        `"unmeasurable"` shape. All `HeartbeatSample(...)` calls below use
        keyword arguments deliberately: `gap` sits ahead of
        `previous_session_id` in the dataclass's field order, and a
        positional call here once already carried `before.session_id` into
        the wrong parameter when `gap` was inserted -- keywords make that
        class of mistake impossible to make silently again.
        """
        if before is None:
            return HeartbeatSample(status=HEARTBEAT_UNMEASURABLE, tick=0, session_id=None, gap=None)
        if after is None:
            return HeartbeatSample(
                status=HEARTBEAT_UNMEASURABLE, tick=before.tick, session_id=before.session_id, gap=gap
            )
        if after.session_id != before.session_id:
            return HeartbeatSample(
                status=HEARTBEAT_RESTARTED, tick=after.tick, session_id=after.session_id,
                gap=gap, previous_session_id=before.session_id,
            )
        if after.tick > before.tick:
            return HeartbeatSample(
                status=HEARTBEAT_GROWING, tick=after.tick, session_id=after.session_id, gap=gap
            )
        if gap < _MOD_PUBLISH_INTERVAL_SECONDS:
            return HeartbeatSample(
                status=HEARTBEAT_UNMEASURABLE, tick=after.tick, session_id=after.session_id, gap=gap
            )
        return HeartbeatSample(
            status=HEARTBEAT_STALLED, tick=after.tick, session_id=after.session_id, gap=gap
        )

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

        This is `heartbeat_detail(window).status, heartbeat_detail(window).tick`
        -- unchanged in shape from before `HeartbeatSample` existed, so
        nothing already unpacking a plain 2-tuple needs to change. Call
        `heartbeat_detail` instead when the session id itself is needed
        (e.g. to report the live session to a caller), not just whether the
        tick is moving.
        """
        sample = self._classify_samples(*self._sample_twice(window))
        return sample.status, sample.tick

    def heartbeat_detail(self, window: float = 3.0) -> HeartbeatSample:
        """Like `heartbeat`, but returns the full `HeartbeatSample` --
        status, tick, AND the session id(s) observed -- instead of just
        `(status, tick)`.

        Several of Task 5's acceptance probes need to know the bridge's
        live session id, and until this existed nothing could tell them
        without a second, separate probe (`current_session_id` does its own
        fresh tolerant read, which would cost another `_read_state_tolerant`
        round trip for information this method already has in hand from the
        same two samples `heartbeat` itself takes). Same timing and
        tolerance as `heartbeat` -- this is not a second, independent
        measurement, just a richer view of the one `heartbeat` already
        takes.
        """
        return self._classify_samples(*self._sample_twice(window))
