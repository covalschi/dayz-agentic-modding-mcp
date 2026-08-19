"""Pure protocol types for the bridge into a running game server.

The mod cannot write its state file atomically -- Enforce Script has no
rename primitive, so it overwrites the state file in place once a second.
A reader will therefore sometimes observe a half-written file. That is the
normal case here, not an error: `parse_state` returns None on any input it
cannot make sense of, rather than raising. See
specs/2026-08-19-dayz-mcp-phase2-bridge.md §3 (hub repo) for the constraint
this is answering to.

This module is pure: no filesystem, no subprocess. `classify_timeout` takes
`now` as an explicit argument rather than reading the clock itself, so its
branching is deterministic and testable without mocking time.
"""
from __future__ import annotations

import itertools
import json
import threading
import time
from dataclasses import dataclass, field

# Closed set -- anything else in a "status" field is not trustworthy data
# (most likely a torn write that happened to still be syntactically valid
# JSON, e.g. a string cut short partway through).
STATUSES = ("idle", "running", "done", "failed")


@dataclass(frozen=True)
class Command:
    """A command bound for the mod. `id` is what lets a later state report be
    matched back to this exact command rather than some other one."""

    id: str
    verb: str
    args: dict

    def to_json(self) -> str:
        return json.dumps(
            {"id": self.id, "verb": self.verb, "args": self.args}, ensure_ascii=False
        )


@dataclass(frozen=True)
class CommandState:
    """The mod's report on the command it currently knows about.

    `id` is the command id being reported on, not necessarily the id of
    whatever command a given caller last sent -- that comparison is the
    caller's job. `status` is one of STATUSES; `detail` is a human-readable
    reason or result; `finished_at` is set once status is "done" or "failed".
    """

    id: str
    status: str
    detail: str = ""
    finished_at: float | None = None


@dataclass(frozen=True)
class BridgeState:
    """One snapshot of the mod's state file.

    `session_id` is an opaque value the mod sets once at boot and never
    changes again until the next boot -- required, like `tick`, because the
    two exist for the same reason: the mod's tick counter restarts at 0
    every boot, but the profile directory (and so the state file) survives
    a restart. Comparing tick alone across two samples therefore cannot
    tell "the world stalled" apart from "a new world just came up, whose
    tick has not caught up yet" -- across a restart tick can go DOWN, which
    a naive comparison reads as backwards progress or a frozen bridge,
    either way wrong. `session_id` changing between two samples is what
    `Channel.heartbeat` (bridge/channel.py) uses to tell those apart. Do not
    assume it is ordered, numeric, or comparable in any way other than
    equality -- it is opaque by design, chosen entirely by the mod side.
    """

    tick: int
    session_id: str
    command: CommandState | None = None
    errors: list[str] = field(default_factory=list)
    world: dict = field(default_factory=dict)


def parse_state(text: str) -> BridgeState | None:
    """Parse one snapshot of the mod's state file.

    Returns None for anything that doesn't parse into a well-formed
    BridgeState: invalid JSON syntax (the expected shape of a torn write),
    JSON that parses but isn't an object, or a value in the wrong shape
    (missing field, wrong type, status outside the closed set). All of
    these are the same case from the caller's point of view -- this read
    didn't land on a complete write, try again next tick -- so they all
    collapse to the same None rather than some raising and some not.
    """
    try:
        raw = json.loads(text)
        if not isinstance(raw, dict):
            return None

        command_raw = raw.get("command")
        command: CommandState | None = None
        if command_raw is not None:
            status = str(command_raw["status"])
            if status not in STATUSES:
                return None
            finished_at = command_raw.get("finished_at")
            command = CommandState(
                id=str(command_raw["id"]),
                status=status,
                detail=str(command_raw.get("detail", "")),
                finished_at=None if finished_at is None else float(finished_at),
            )

        errors = raw.get("errors", [])
        if not isinstance(errors, list):
            return None

        world = raw.get("world", {})
        if not isinstance(world, dict):
            return None

        return BridgeState(
            tick=int(raw["tick"]),
            # Required, same as tick -- see BridgeState's docstring. A state
            # JSON that parses but omits session_id entirely (as opposed to
            # a torn write, which would have failed json.loads already
            # above) means whatever wrote it does not speak this version of
            # the protocol, which is exactly the "cannot make sense of it"
            # case this function returns None for everywhere else.
            session_id=str(raw["session_id"]),
            command=command,
            errors=[str(e) for e in errors],
            world=world,
        )
    except (json.JSONDecodeError, KeyError, TypeError, ValueError, AttributeError):
        return None


_id_lock = threading.Lock()
_id_seq = itertools.count(1)


def new_command_id(verb: str) -> str:
    """Build a command id that stays unique across a process restart, not
    just within one process's memory.

    jobs.JobStore hit exactly this failure mode: `int(time.time())` (whole
    -second resolution) plus an in-memory counter that resets on restart let
    two jobs of the same kind created in the same second, across a restart,
    collide. JobStore's fix checks candidate ids against files already on
    disk -- not available here, since this module touches no filesystem. The
    fix that *is* available without a filesystem is resolution: `time.time_ns()`
    is precise enough that two processes landing on the exact same nanosecond
    is negligible, which is what "unique against reality" has to mean when
    there is nothing persistent to check against. The counter only breaks
    ties for two calls inside the *same* process landing on a clock reading
    that did not advance between them; it intentionally is not seeded from
    anywhere and is allowed to restart at 1 on every process start, because
    the timestamp component is what carries uniqueness across that boundary.
    """
    with _id_lock:
        seq = next(_id_seq)
    return f"{verb}-{time.time_ns()}-{seq}"


def classify_timeout(sent_at: float, now: float, timeout: float) -> str:
    """Classify how much time has passed since a command was sent, from
    outside the game.

    This is the generic, Python-side ceiling. The game-side timeout is
    deliberately set higher (see spec) so that when a command genuinely
    fails, the mod's own specific reason reaches the caller before this
    generic classifier would have already called it "expired".
    """
    elapsed = now - sent_at
    return "expired" if elapsed >= timeout else "waiting"
