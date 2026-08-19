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
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

from ..errors import Result, fail, ok
from .protocol import BridgeState, Command, CommandState, parse_state

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

    def send(self, cmd: Command) -> Result:
        """Write `cmd` into the mailbox, atomically, and refuse if it is
        already occupied by a command the mod has not yet claimed (claiming
        means the mod deletes the file after reading it -- the mod side is
        Task 5).

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

        Refuses to act, and reports what it found, when `heartbeat` says the
        bridge looks alive (`"growing"` or `"restarted"`) -- something is
        actively running right now and could claim the mailbox any moment,
        so clearing then risks silently destroying live in-flight work,
        which is worse than leaving the wedge in place a while longer. Pass
        `force=True` to discard anyway once that risk has been accepted
        (e.g. after `bridge_status`/`world_state` corroborate the command is
        stale). `"stalled"` or `"unmeasurable"` mean nothing is currently
        proven to be moving -- a genuinely down stand or an unwired bridge
        reads as one of these two, never as `"growing"`/`"restarted"` -- so
        those proceed without needing `force`.

        On success, reports the discarded command (parsed back from its
        JSON: id, verb, args) so a caller can tell whether the thing just
        thrown away was the one they cared about, plus the heartbeat status
        the probe saw -- recorded even when `force=True` bypassed the
        refusal, so a forced clear still leaves a record of what it
        overrode.
        """
        cmd_path = self._cmd_path()
        try:
            raw = cmd_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return fail(
                f"mailbox at {cmd_path} is already empty",
                hint="there is nothing to clear -- send() would succeed right now",
            )
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

        status, tick = self.heartbeat(window=probe_window)
        if status in (HEARTBEAT_GROWING, HEARTBEAT_RESTARTED) and not force:
            return fail(
                f"refusing to clear the mailbox at {cmd_path}: the bridge looks alive "
                f"(heartbeat={status!r}, tick={tick}) and may claim this command any moment",
                hint="pass force=True if you are certain this command should be discarded "
                     "anyway; check bridge_status/world_state first if you are not sure",
            )

        try:
            cmd_path.unlink()
        except FileNotFoundError:
            # Claimed (by the mod) or cleared (by a concurrent caller)
            # between our read above and this unlink -- the wedge is gone
            # either way, which is the outcome this method exists to reach.
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
          "stalled"      -- same session, tick unchanged: alive, but frozen.
          "restarted"    -- session id changed between samples: a NEW world
                             came up, not a stall on the old one. `tick` is
                             the new session's own tick -- there is nothing
                             meaningful to compare it against across a
                             restart, so it is not compared.
          "unmeasurable" -- fewer than two readable samples were obtained
                             (the state file was never readable within
                             `window`, or the second read failed after the
                             first one succeeded). Distinct from "stalled":
                             stalled means the same world was observed twice
                             and did not move; this means no comparison
                             could be made at all. `tick` is the last tick
                             actually read, or 0 if nothing was ever read.
        """
        deadline = time.monotonic() + window
        before = self._read_state_tolerant()
        if before is None:
            return HEARTBEAT_UNMEASURABLE, 0

        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)

        after = self._read_state_tolerant()
        if after is None:
            return HEARTBEAT_UNMEASURABLE, before.tick

        if after.session_id != before.session_id:
            return HEARTBEAT_RESTARTED, after.tick
        if after.tick > before.tick:
            return HEARTBEAT_GROWING, after.tick
        return HEARTBEAT_STALLED, after.tick
