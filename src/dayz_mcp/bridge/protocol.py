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
    matched back to this exact command rather than some other one.

    `session_id` is load-bearing, not decorative: the engine gives Enforce
    Script no boot identity of its own, so a command with no session (or the
    wrong one) is indistinguishable to the mod from a stale command left
    over from before a restart -- carrying the CURRENT session is the only
    defence against a stale command detonating in a freshly booted world.
    `Channel.build_command` (bridge/channel.py) is the intended way to
    obtain one already stamped with the session the mod most recently
    published; `Channel.send` refuses outright rather than write a command
    whose `session_id` is empty, so this module does not have to guess at
    what "wrong" looks like on the wire -- only "present and non-empty" is
    enforced here, structurally, by giving the field no default.
    """

    id: str
    session_id: str
    verb: str
    args: dict

    def to_json(self) -> str:
        return json.dumps(
            {"id": self.id, "session_id": self.session_id, "verb": self.verb, "args": self.args},
            ensure_ascii=False,
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
class ParseRejection:
    """Why `parse_state(text)` returned `None` for a document that WAS
    valid JSON but failed schema validation -- see `parse_rejection`, the
    function that produces this.

    NEVER populated for invalid JSON syntax (a torn write): that is the
    ordinary, once-a-second, uninteresting condition this whole module
    exists to treat as unremarkable, and stays silent (both `parse_state`
    and `parse_rejection` return `None` for it, with nothing to tell them
    apart from a genuine rejection at the type level -- callers that need
    to distinguish "no rejection because it parsed fine" from "no rejection
    because it was a torn write" already have that from `parse_state`'s own
    `None`-or-`BridgeState` result).

    `field` is a dotted path into the document (`"tick"`, `"session_id"`,
    `"command.status"`, `"errors"`, `"world"`, or `"<root>"` for the
    document itself not being an object) -- one of the six shapes
    `parse_state` explicitly validates, not every possible KeyError this
    module's broad `except` also catches (a missing `command.id`, for
    instance, is not detailed -- it was not one of the fields this exists
    to cover). `reason` is a short human-readable explanation of what was
    expected. `value` is the actual JSON-decoded value that was seen (not
    pre-formatted text) -- `None` if the field was missing outright, kept
    as a real Python value rather than a string so the tool layer that
    renders this can choose its own presentation rather than re-parsing a
    description back apart.
    """

    field: str
    reason: str
    value: object = None


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


def _parse_state_checked(text: str) -> tuple[BridgeState | None, ParseRejection | None]:
    """The one real implementation behind both `parse_state` and
    `parse_rejection` -- see their docstrings for the public contract. Two
    thin wrappers around one function rather than two independent
    implementations, so the validation rules can never drift out of sync
    between "does it parse" and "why didn't it".

    Returns `(state, rejection)`: exactly one of the two is populated for
    a rejected document (rejection only for the six shapes explicitly
    validated below; everything else -- invalid JSON syntax, or any other
    KeyError/TypeError/ValueError/AttributeError this function's broad
    `except` also catches, such as a missing `command.id` -- falls back to
    `(None, None)`, the same as a torn write).
    """
    try:
        raw = json.loads(text)
    except json.JSONDecodeError:
        return None, None  # torn write: ordinary, not a rejection

    try:
        if not isinstance(raw, dict):
            return None, ParseRejection("<root>", "must be a JSON object", raw)

        command_raw = raw.get("command")
        command: CommandState | None = None
        if command_raw is not None:
            if not isinstance(command_raw, dict) or "status" not in command_raw:
                return None, ParseRejection("command.status", "missing", command_raw)
            status = str(command_raw["status"])
            if status not in STATUSES:
                return None, ParseRejection(
                    "command.status", f"must be one of {STATUSES}", status
                )
            finished_at = command_raw.get("finished_at")
            command = CommandState(
                id=str(command_raw["id"]),
                status=status,
                detail=str(command_raw.get("detail", "")),
                finished_at=None if finished_at is None else float(finished_at),
            )

        errors = raw.get("errors", [])
        if not isinstance(errors, list):
            return None, ParseRejection("errors", "must be a list", errors)

        world = raw.get("world", {})
        if not isinstance(world, dict):
            return None, ParseRejection("world", "must be an object", world)

        # tick: required, and must genuinely be a JSON integer -- not a
        # numeric string (int("42") would silently accept one), not a float
        # (int(7.9) would silently truncate instead of rejecting it), and
        # not a bool (bool is a subtype of int in Python, so True/False
        # would otherwise slip past an isinstance(int) check unnoticed).
        if "tick" not in raw:
            return None, ParseRejection("tick", "missing")
        tick_raw = raw["tick"]
        if not isinstance(tick_raw, int) or isinstance(tick_raw, bool):
            return None, ParseRejection(
                "tick", "must be a genuine integer (not a string, float, or bool)", tick_raw
            )

        # session_id: required, same as tick -- see BridgeState's docstring.
        # A state JSON that parses but omits it entirely means whatever
        # wrote it does not speak this version of the protocol, the same
        # "cannot make sense of it" case every other malformed field
        # collapses to.
        #
        # OPEN MEASUREMENT, not yet verified: the reasoning above ("a state
        # that parses but is missing a required key is a torn write" would
        # be double-counting) assumes a torn write is caught as invalid JSON
        # by json.loads before reaching here. That held for a TRUNCATING
        # writer in every truncation offset two independent reviewers tried
        # (tens of thousands of attempts, zero forged-but-valid documents).
        # It does NOT hold for a non-truncating in-place overwrite, where a
        # length change ahead of a key can splice two partial key names
        # into one that happens to be valid JSON with a wrong-but-plausible
        # value -- also reproduced independently. Whether the mod's actual
        # writer (Task 5) truncates or overwrites in place is still open;
        # if it overwrites in place, a torn write could in principle land on
        # a syntactically valid document with a corrupted-but-present
        # session_id (or tick) that this function cannot tell apart from a
        # deliberate one. Requiring the key catches an ABSENT one either
        # way; it does not by itself catch every torn-write shape.
        #
        # Requiring the KEY is also not enough on its own regardless of the
        # above: an unvalidated VALUE would accept null, "", 0 and false as
        # "valid" session ids (str(None) == "None", str(False) == "False",
        # etc.), and any value that stays constant across boots defeats the
        # entire point of this field -- restart detection would silently
        # no-op instead of firing. The most plausible Enforce-side mistake
        # produces exactly this: an unset `string` field serialises as "",
        # not as an absent key. So the VALUE is validated too: it must be a
        # genuine, non-empty JSON string.
        if "session_id" not in raw:
            return None, ParseRejection("session_id", "missing")
        session_id_raw = raw["session_id"]
        if not isinstance(session_id_raw, str) or not session_id_raw:
            return None, ParseRejection(
                "session_id", "must be a non-empty string", session_id_raw
            )

        return BridgeState(
            tick=tick_raw,
            session_id=session_id_raw,
            command=command,
            errors=[str(e) for e in errors],
            world=world,
        ), None
    except (KeyError, TypeError, ValueError, AttributeError):
        return None, None


def parse_state(text: str) -> BridgeState | None:
    """Parse one snapshot of the mod's state file.

    Returns None for anything that doesn't parse into a well-formed
    BridgeState: invalid JSON syntax (the expected shape of a torn write),
    JSON that parses but isn't an object, or a value in the wrong shape
    (missing field, wrong type, status outside the closed set). All of
    these are the same case from the caller's point of view -- this read
    didn't land on a complete write, try again next tick -- so they all
    collapse to the same None rather than some raising and some not.

    This is the hot path every tolerant-reading call site in `Channel`
    polls once a second: deliberately unchanged in shape from before
    `parse_rejection` existed, so nothing that already treats a bare `None`
    as "try again" needs to change. Call `parse_rejection(text)` alongside
    this when a `None` needs explaining instead of just retrying -- see its
    own docstring for when that is (and is not) the right tool.
    """
    state, _rejection = _parse_state_checked(text)
    return state


def parse_rejection(text: str) -> ParseRejection | None:
    """Explain why `parse_state(text)` would return `None` for this
    document -- for the DIAGNOSTIC case only, not the routine polling loop.

    Returns `None` in two situations that look identical from the outside
    but mean opposite things: the document actually parses fine (nothing to
    explain), and the document is a torn write (invalid JSON syntax -- the
    ordinary, once-a-second condition this whole module treats as
    unremarkable, deliberately never detailed; see `ParseRejection`'s
    docstring). Returns a populated `ParseRejection` only for the third
    case: a document that parsed as valid JSON but failed schema
    validation -- a genuine mod-side bug (a wrong type, a typo'd status, an
    empty session id), not a mid-write snapshot.

    A caller cannot tell which of the two `None` cases happened from this
    function alone -- call `parse_state(text)` on the SAME text first (or
    use `Channel.read_state_rejection`, which does exactly that) to know
    whether there was anything to explain in the first place. This
    function's whole reason to exist is that `parse_state`'s own `None`
    already conflates "torn write, try again" with "persistent schema bug,
    something is actually wrong" -- the six named shapes this covers
    (`<root>`, `command.status`, `errors`, `world`, `tick`, `session_id`)
    are exactly parse_state's own explicit validation checks, not every
    possible malformation its broader exception handling also catches.
    """
    _state, rejection = _parse_state_checked(text)
    return rejection


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
