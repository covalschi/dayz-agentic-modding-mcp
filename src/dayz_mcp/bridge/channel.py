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
"""
from __future__ import annotations

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
        `_lost_race_result`: that one is ordinary contention between two
        senders and implies nothing about the mod at all. Conflating the two
        was tried and reverted -- see the fix-round-2 note in the report."""
        return fail(
            f"mailbox already holds an unclaimed command at {cmd_path}",
            hint="the mod has not picked up the previous command yet -- wait for it to "
                 "be claimed (the mailbox file to disappear) before sending another",
        )

    def _lost_race_result(self, cmd_path: Path) -> Result:
        """A concurrent `send()` call claimed the mailbox microseconds
        before this one did (the atomic `os.link` claim below lost the
        race). This says NOTHING about the mod -- there was no previous
        command sitting unclaimed, and nothing is wedged; a sibling call on
        this same process simply won first. The caller's response should be
        an ordinary short retry, not "go investigate a stuck mod", which is
        why this is a distinct message/hint from `_unclaimed_mailbox_result`
        rather than reusing it."""
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

    def heartbeat(self, window: float = 3.0) -> tuple[bool, int]:
        """Is the world's tick counter growing over `window` seconds?

        The tick is the only proof the world is running -- a state file that
        merely exists proves nothing, since the mod rewrites it whether the
        world is progressing or hung. Sampled tolerantly (see
        `_read_state_tolerant`) at both ends, so one torn read landing at
        exactly the wrong moment is not reported as a dead bridge.

        Returns (growing, latest_tick). latest_tick is 0 when no valid
        reading was ever obtained -- there is no tick to report.
        """
        deadline = time.monotonic() + window
        before = self._read_state_tolerant()
        if before is None:
            return False, 0

        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)

        after = self._read_state_tolerant()
        if after is None:
            return False, before.tick
        return after.tick > before.tick, after.tick
