"""World commands: make something happen inside the running game and say what
happened.

Thin by design. Each tool builds one command, hands it to the channel, waits
for the mod's own answer and returns it in the usual envelope -- there is no
logic here that could disagree with the mod, because a second opinion about
what a verb does is exactly how a tool starts reporting success for a world in
which nothing changed.

Three facts, all MEASURED on a live stand during Task 5, shape everything below.

1. ARGUMENT VALUES ARE STRINGS ON THE WIRE, all of them.
   Observation O3: the mod's deserializer is strict. `{"args": {"bytes": 512}}`
   -- a JSON number -- does not lose one field, it rejects the whole args block:
   `Expecting map Expecting string Cannot convert`. The command still comes back
   correlated (the mod's two-stage parse recovers the id and says so), but it
   comes back failed, every time, for a command that was perfectly sensible.
   So `_to_wire` stringifies numbers and booleans, and REFUSES anything it
   cannot stringify faithfully rather than sending a command it knows the mod
   will reject. Pinned by tests -- this is the contract that was left open until
   the live run answered it.

2. THE BRIDGE COMES UP TENS OF SECONDS AFTER THE SERVER SAYS IT IS READY.
   The spread observed so far is 18-38 s, and it varies boot to boot. The mod
   publishes its first state during mission init, but
   the repeating call that reads the mailbox does not start firing until well
   after the ready line. A command sent into that window is claimed eventually
   and completes normally -- long after the caller has given up, with no
   evidence anywhere that anything worked. That is the exact silent timeout this
   whole product exists to abolish, so every tool here proves the tick is MOVING
   before it sends, and refuses in one breath if it is not (see `_require_a_
   moving_bridge`). `world_ready` is the tool that does the waiting.

3. THE MOD GIVES UP BEFORE WE DO, AND THE MARGIN IS ONLY VALID WITH RULE 2.
   In-game: a no-progress watchdog at 20 s and a hard ceiling at 30 s, both
   BELOW the 45 s this module waits, so the mod's specific reason ("no progress
   for 20s") reaches the caller instead of a faceless "expired". That ordering
   holds only because rule 2 removes the claim delay: sent at the ready line
   instead, a 30 s wait to be claimed plus a 30 s run exceeds 45 s and the
   margin is gone. The two are one decision, not two.
"""
from __future__ import annotations

import re
import time

from ..bridge.channel import Channel
from ..errors import Result, fail, ok
from ..procs import is_alive
from . import session
from .lifecycle import boot_in_flight, server_profiles_dir
from .project import require_project

# How long to wait for the mod's answer. Above both in-game deadlines (20 s
# watchdog, 30 s hard limit) with room for the publish interval and the poll
# step, so the mod's own reason always lands first -- see the module docstring's
# rule 3, and note that it depends on rule 2.
WORLD_TIMEOUT_SECONDS = 45.0

# The probe that proves the tick is moving. Longer than the mod's 1 Hz publish
# interval on purpose: below that, "the tick did not move" and "the tick has not
# had a chance to move yet" are the same observation.
MOVEMENT_PROBE_WINDOW = 1.2

# How long `world_ready` will wait for the first tick. The gap observed so far
# spreads 18-38 s after the ready line; this leaves room for a slower machine
# without turning into an unbounded wait.
READY_TIMEOUT_SECONDS = 90.0

# The mod is moving under either of these -- "restarted" means a NEW world came
# up mid-probe, which is alive, and the opposite of frozen.
_MOVING = ("growing", "restarted")

_POS_HELP = "a position is three numbers separated by spaces, like '7500 0 7500'"

# The verb charset for world_exec, where the verb arrives from OUTSIDE. The
# mod recovers a command's id by a plain string search over the raw mailbox
# when the parse fails, and the id embeds the verb -- so a quote inside a verb
# breaks that recovery, and a non-ASCII verb makes the id non-ASCII, which the
# mod's sanitiser would mangle into an id nobody can correlate. One regex
# removes the whole class. Lowercase to match how every built-in verb is
# spelled and compared.
_VERB_RE = re.compile(r"^[a-z][a-z0-9_]{0,40}$")

_NON_STANDARD_NOTE = (
    "non-standard verb: this server passed it through without knowing it and does not "
    "answer for its behaviour -- the mod decides what (if anything) it means"
)


def _to_wire(value: object) -> str:
    """One argument value, as the mod will receive it.

    Every value goes as a string because the mod's deserializer rejects the
    whole args block otherwise (module docstring, rule 1). Booleans become
    "true"/"false" rather than Python's "True"/"False": the mod compares against
    lowercase, and `str(True)` would silently miss.

    Raises ValueError for anything that cannot be carried faithfully -- None,
    lists, dicts, objects. That is deliberate and is the whole point: `str({})`
    would happily produce "{}" and send a command the mod cannot use, which is a
    round trip and a failed command to discover a mistake that was visible here.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        # No exponent form: the mod parses with string.ToFloat(), and "1e-05"
        # is not something it is documented to read. Trailing zeros trimmed so
        # a whole number arrives as "30" rather than "30.000000".
        text = f"{value:.6f}".rstrip("0").rstrip(".")
        return text if text else "0"
    raise ValueError(
        f"{type(value).__name__} cannot be sent as a command argument -- every argument "
        "value crosses the wire as a string, and this one has no faithful string form"
    )


def _wire_args(args: dict) -> dict:
    """The whole args map, stringified -- every value through `_to_wire`,
    including None, which `_to_wire` refuses. An earlier version silently
    DROPPED None-valued keys here, which was fine for this module's own
    builders (omission is their deliberate signal) and wrong for `world_exec`,
    where a JSON null argument vanished instead of refusing -- the same
    silent-loss shape the mod's unknown-key check exists to prevent, and the
    mod cannot tell "absent" from "was null and got dropped". Omission now
    happens only in `_args`, where it is a decision this module makes
    explicitly."""
    return {key: _to_wire(value) for key, value in args.items()}


def _args(**kw: object) -> dict:
    """Build an args dict for this module's own tools, omitting keys the
    caller left as None -- the deliberate "not sent at all" signal the mod's
    unknown-key check depends on. Only this module's builders use it;
    `world_exec` hands user args straight to `_wire_args`, so a null VALUE
    from outside is refused rather than quietly disappearing."""
    return {key: value for key, value in kw.items() if value is not None}


def _live_server() -> tuple[int, bool]:
    pid = session.server_pid()
    return pid, bool(pid and is_alive(pid, image=session.server_image()))


def _no_server() -> Result:
    """Why there is nothing to act on -- and the two answers are different.

    A boot still in flight is named by its job, because "there is nothing to
    act on" sends the reader hunting for a server that died when in fact one is
    on its way up. The other sentence is kept EXACTLY as it was: client_chat
    matches on it to replace the hint, and so does the muscle memory of anyone
    who has read this refusal before.
    """
    booting = boot_in_flight()
    if booting:
        return fail(
            f"the server is still starting -- boot job {booting} has not finished, so there "
            "is nothing to act on yet",
            hint=f"wait for it with job_wait('{booting}'); if it fails, its own error says "
                 "why. Then call world_ready before the first command",
        )
    return fail(
        "no server started by this session is running, so there is nothing to act on",
        hint="start one with server_start, wait for the boot job to finish, then call "
             "world_ready before the first command",
    )


def _require_a_moving_bridge(channel: Channel) -> Result | None:
    """None when the bridge is proven to be ticking; a refusal otherwise.

    Costs one probe window (about 1.2 s) per command, and buys the difference
    between a 45-second silent timeout and an immediate sentence naming the
    cause. The bridge is unreachable for tens of seconds after the server
    reports ready (module docstring, rule 2), and during that window a command
    is not rejected -- it is accepted, sat on, and completed long after the
    caller stopped listening.
    """
    detail = channel.heartbeat_detail(MOVEMENT_PROBE_WINDOW)
    if detail.status in _MOVING:
        return None

    return fail(
        f"the bridge is not ticking (heartbeat={detail.status!r}), so a command sent now "
        "would sit unclaimed and its result would arrive after this call had given up",
        hint="call world_ready() -- the bridge starts ticking tens of seconds AFTER the "
             "server reports ready (18-38 s observed so far), so this is the ordinary state "
             "right after a boot, not a fault. If world_ready also times out, check "
             "bridge_status and log_verdict",
    )


def _run(verb: str, args: dict, timeout: float) -> Result:
    """Build one command, send it, wait for the mod's answer, return it.

    The mod's own `detail` is passed through verbatim in both directions. A
    refusal from the mod -- "no player is on the server", "the class does not
    exist", "the mod's own conditions did not hold" -- is a RESULT, and it comes
    back as `ok=False` with that sentence as the error, never flattened into a
    generic failure. That distinction is the product.
    """
    guard = require_project()
    if guard:
        return guard

    pid, alive = _live_server()
    if not alive:
        return _no_server()

    channel = Channel(server_profiles_dir())
    not_moving = _require_a_moving_bridge(channel)
    if not_moving:
        return not_moving

    try:
        wire = _wire_args(args)
    except ValueError as exc:
        return fail(
            str(exc),
            hint="pass numbers, booleans or strings; positions go as a single string like "
                 "'7500 0 7500'",
        )

    built = channel.build_command(verb, wire)
    if not built.ok:
        return built
    command = built.data

    sent = channel.send(command, is_alive=alive)
    if not sent.ok:
        return sent

    state = channel.await_result(command.id, timeout=timeout, poll=0.25)
    if state is None:
        return fail(
            f"no answer for {verb} within {timeout:g}s, and the mod never reported on "
            f"command {command.id} at all",
            hint="the mod's own deadlines (20s watchdog, 30s hard limit) are below this "
                 "one, so silence here means the command was never claimed rather than "
                 "that it ran long -- check bridge_status, then log_verdict",
        )

    payload = {
        "verb": verb,
        "command_id": command.id,
        "status": state.status,
        "detail": state.detail,
        "finished_at": state.finished_at,
        "args": wire,
    }

    if state.status == "done":
        return ok(payload)

    if state.status == "failed":
        return Result(False, payload, state.detail or f"{verb} failed", hint=_hint_for(verb))

    return Result(
        False, payload,
        f"{verb} was still {state.status} after {timeout:g}s",
        hint="the mod's own hard limit is 30s, so a command still running past this wait "
             "means the tick itself stalled -- check bridge_status",
    )


def _hint_for(verb: str) -> str:
    """What to do about a refusal the mod issued. Kept short: the mod's own
    `detail` already says WHAT went wrong, so this only ever says what to try."""
    if verb in ("spawn", "teleport", "set", "delete", "query"):
        return (
            "the mod refused this, and its own words are in the error above -- a refusal is "
            "a result, not a malfunction. Check world_state() for whether a player is "
            "connected and where they are"
        )
    return "see the error above; it is the mod's own words"


def world_ready(timeout: float = READY_TIMEOUT_SECONDS) -> Result:
    """Wait until the bridge inside the game is actually ticking.

    Call this once after `server_start`'s boot job finishes and before the first
    world command. The bridge publishes its first state during mission init but
    does not start reading commands until tens of seconds AFTER the server
    reports ready -- 18-38 s in the boots measured so far. A command sent in that
    window
    is claimed eventually and completes normally, long after the caller gave up.

    Blocks, with a ceiling, because there is nothing else to do with the answer:
    the alternative is handing back "not yet" and having the caller poll, which
    is the same wait with more round trips.
    """
    guard = require_project()
    if guard:
        return guard

    pid, alive = _live_server()
    if not alive:
        return _no_server()

    channel = Channel(server_profiles_dir())
    started = time.monotonic()
    deadline = started + max(0.0, timeout)
    attempts = 0
    last = "unmeasurable"

    while True:
        attempts += 1
        detail = channel.heartbeat_detail(MOVEMENT_PROBE_WINDOW)
        last = detail.status
        if detail.status in _MOVING:
            return ok({
                "state": "ready",
                "heartbeat": detail.status,
                "tick": detail.tick,
                "session_id": detail.session_id,
                "waited_seconds": round(time.monotonic() - started, 1),
                "probes": attempts,
            })
        if time.monotonic() >= deadline:
            return fail(
                f"the bridge was still not ticking after {timeout:g}s (heartbeat={last!r})",
                hint="check that the bridge mod is wired into mods.server_only and built "
                     "with bridge_build, then read log_verdict for this boot -- "
                     "bridge_status will say which of those it is",
            )
        # Nothing to sleep for: heartbeat_detail already spent a probe window.


def world_state(class_name: str = "", radius: float = 30.0, pos: str = "",
                timeout: float = WORLD_TIMEOUT_SECONDS) -> Result:
    """What the world looks like right now.

    With NO class_name this costs nothing and waits for nothing: the mod
    republishes the player's position, health and what is in their hands every
    tick, so the answer is already on disk. That is deliberate -- a snapshot a
    caller has to pay a full command round trip for (a second to be claimed,
    two more of terminal dwell) would make the cheapest question the most
    expensive one.

    With a class_name it also sends a `query` command to count objects of that
    class within `radius`, because a count needs arguments only a command
    carries. The count comes back in `world.query_count` as well as in the
    detail, so it stays readable on later snapshots too -- which is how "is the
    item I spawned a minute ago still there?" gets answered.
    """
    guard = require_project()
    if guard:
        return guard

    pid, alive = _live_server()
    if not alive:
        return _no_server()

    channel = Channel(server_profiles_dir())

    if class_name:
        answered = _run("query", _args(**{"class": class_name, "radius": radius, "pos": pos or None}),
                        timeout)
        if not answered.ok:
            return answered

    state = channel.read_state()
    if state is None:
        # One tolerant retry through the public reader: a single torn read is
        # the ordinary once-a-second condition, not news.
        time.sleep(0.3)
        state = channel.read_state()

    if state is None:
        return fail(
            "the bridge has not published a readable state",
            hint="call bridge_status -- it tells apart 'the mod is not loaded', 'the "
                 "document does not parse' and 'a named field is wrong'",
        )

    return ok({
        "tick": state.tick,
        "session_id": state.session_id,
        "world": state.world,
        "errors": state.errors,
        "command": None if state.command is None else {
            "id": state.command.id,
            "status": state.command.status,
            "detail": state.command.detail,
        },
    })


def world_spawn(class_name: str, where: str = "ground", pos: str = "",
                quantity: float | None = None,
                timeout: float = WORLD_TIMEOUT_SECONDS) -> Result:
    """Create an item: on the ground, in the player's hands, or in their
    inventory.

    `where` is "ground" (default), "hands" or "inventory". A ground spawn takes
    `pos` as "x y z" and falls back to the player's own position when it is
    omitted; with neither a position nor a player, the mod says so in words
    rather than doing nothing.

    Ground spawns are created with ECE_PLACE_ON_SURFACE **and ECE_NOLIFETIME**.
    Without the second flag the item lives by the lifetime in its own config and
    the central economy is free to remove it partway through a check -- which
    turns "my test item vanished" into a hunt through the mod under test. The
    flag is the mod's, not this tool's; it is named here because it is the
    reason a spawned item can be trusted to still be there a minute later.
    """
    return _run("spawn", _args(**{
        "class": class_name,
        "where": where,
        "pos": pos or None,
        "quantity": quantity,
    }), timeout)


def world_teleport(pos: str, timeout: float = WORLD_TIMEOUT_SECONDS) -> Result:
    """Move the player to `pos`, given as "x y z".

    The same format `world_state` reports positions in, so a position read out
    of a snapshot can be handed straight back. With nobody connected the mod
    refuses by name -- an absent player is a distinct, stated reason, never a
    silent no-op.
    """
    if not pos.strip():
        return fail(f"teleport needs a position -- {_POS_HELP}",
                    hint="read the current one from world_state()'s world.player_pos")
    return _run("teleport", {"pos": pos}, timeout)


def world_set(what: str, value: float, target: str = "",
              timeout: float = WORLD_TIMEOUT_SECONDS) -> Result:
    """Set `health` or `quantity`.

    `target` is "player" or "hands"; left empty it defaults per `what` -- health
    on the player, quantity on the held item -- because a single default for
    both would make one of the two combinations a trap (a player has no
    quantity, and empty hands have no health).
    """
    return _run("set", _args(what=what, value=value, target=target or None), timeout)


#: What `world_time_set` reads as "leave this field where it is".
#:
#: Not a magic number standing in for a real value: every field of a date is
#: non-negative, so -1 cannot collide with one. SetDate takes all five fields
#: at once, so a tool that defaulted the ones it was not given would silently
#: move the date to set the hour.
UNCHANGED = -1

#: How many objects `world_entities` lists by default. The mod caps it at 200
#: and reports the true total either way; asking for more than the cap is
#: clamped there rather than refused.
ENTITY_LIMIT = 200


def _date_args(year: int, month: int, day: int, hour: int, minute: int) -> dict:
    return _args(
        year=None if year == UNCHANGED else int(year),
        month=None if month == UNCHANGED else int(month),
        day=None if day == UNCHANGED else int(day),
        hour=None if hour == UNCHANGED else int(hour),
        minute=None if minute == UNCHANGED else int(minute),
    )


def world_time_set(hour: int = UNCHANGED, minute: int = UNCHANGED,
                   day: int = UNCHANGED, month: int = UNCHANGED,
                   year: int = UNCHANGED,
                   timeout: float = WORLD_TIMEOUT_SECONDS) -> Result:
    """Move the world clock.

    Every field left at -1 keeps the value the world already has, read back
    from the engine before the change. That matters because the engine sets a
    date as five numbers at once: a tool that filled in the missing ones would
    move the date every time somebody set the hour.

    Ranges are the engine's own documented ones -- month 1-12, day 1-31, hour
    0-23, minute 0-59 -- and are checked in the mod, before a native call that
    would otherwise be handed a value it does not define behaviour for.

    The answer carries the world's clock as it stands after the change, from
    the mod's own snapshot rather than from what was asked for.
    """
    if all(v == UNCHANGED for v in (hour, minute, day, month, year)):
        return fail(
            "world_time_set was given nothing to change",
            hint="pass at least one of hour, minute, day, month, year; "
                 "everything left at -1 keeps its current value",
        )
    answered = _run("time", _date_args(year, month, day, hour, minute), timeout)
    if not answered.ok:
        return answered
    return _with_world(answered)


def world_weather_set(what: str, value: float, seconds: float = 0.0,
                      duration: float = 0.0,
                      timeout: float = WORLD_TIMEOUT_SECONDS) -> Result:
    """Move one weather phenomenon towards a value.

    `what` is "overcast", "rain", "fog", "snowfall" or "wind". The first four
    take a value between 0 and 1; wind takes a speed in metres per second.
    `seconds` is how long the change takes (0 is immediate) and `duration` is
    how long the value is held before the engine's own simulation may move it
    again.

    THIS IS A NUDGE, NOT A LOCK. The engine keeps simulating weather, so a
    value set here drifts afterwards -- said here and in the mod's own answer,
    because the alternative is a caller who sets rain, looks up two minutes
    later and concludes the tool did nothing.
    """
    answered = _run(
        "weather",
        _args(what=what, value=value, seconds=seconds, duration=duration),
        timeout,
    )
    if not answered.ok:
        return answered
    return _with_world(answered)


def world_entities(class_name: str = "", radius: float = 30.0, pos: str = "",
                   limit: int = ENTITY_LIMIT,
                   timeout: float = WORLD_TIMEOUT_SECONDS) -> Result:
    """WHICH objects are nearby, not how many.

    `world_state(class_name=...)` counts; this one names them, with each
    object's class, position, distance and health. An empty `class_name` lists
    everything found rather than nothing.

    The list is a page: the mod caps it at 200 entries and reports the true
    total separately, so `total` larger than `count` means there is more out
    there -- never a shorter list quietly standing in for the world.

    `distance` is HORIZONTAL, because the engine's own radius test ignores
    height: at the centre of Chernarus the terrain is 300 m up, so a
    straight-line distance from a position written as "7500 0 7500" reads 320 m
    for objects the engine returned inside a 150 m radius. A number that
    contradicts the filter that produced it is worse than no number.

    Players are not in it. The mod's own gather step skips them, which is what
    keeps a `delete` of everything nearby from reaching the person standing in
    it, and this tool shares that step deliberately rather than growing a
    second notion of what is in the world.
    """
    answered = _run(
        "entities",
        _args(**{"class": class_name or None, "radius": radius,
                 "pos": pos or None, "limit": limit}),
        timeout,
    )
    if not answered.ok:
        return answered
    enriched = _with_world(answered)
    world = enriched.data.get("world") or {}
    enriched.data["entities"] = [
        _entity(line) for line in world.get("entities", []) if isinstance(line, str)
    ]
    enriched.data["total"] = world.get("entities_total", -1)
    enriched.data["count"] = len(enriched.data["entities"])
    enriched.data["truncated"] = (
        isinstance(enriched.data["total"], int)
        and enriched.data["total"] > enriched.data["count"]
    )
    return enriched


def _entity(line: str) -> dict:
    """One `class|x y z|distance|health` line as a dict.

    A line that does not have four parts is passed through under `raw` rather
    than dropped or guessed at: a reader that silently discarded what it could
    not parse would report a shorter world than the one the mod found.
    """
    parts = line.split("|")
    if len(parts) != 4:
        return {"raw": line}
    return {
        "class": parts[0],
        "pos": parts[1],
        "distance": _number(parts[2]),
        "health": _number(parts[3]),
    }


def _number(text: str) -> float | str:
    try:
        return float(text)
    except ValueError:
        return text


def _with_world(answered: Result) -> Result:
    """Add the mod's own world snapshot to an answer.

    Read after the command finished, so it is the world as it now is rather
    than the arguments echoed back. A snapshot that cannot be read is reported
    as absent rather than faked: the command itself already succeeded, and
    saying so while admitting the snapshot is missing beats either half.
    """
    channel = Channel(server_profiles_dir())
    state = channel.read_state()
    if state is None:
        time.sleep(0.3)
        state = channel.read_state()
    if state is None:
        answered.data["world"] = {}
        answered.data["world_unavailable"] = (
            "the command finished, but no readable state has been published since"
        )
        return answered
    answered.data["world"] = state.world
    answered.data["tick"] = state.tick
    return answered


def world_action(action_class: str, target_class: str = "", subject: str = "",
                 radius: float = 30.0, pos: str = "",
                 timeout: float = WORLD_TIMEOUT_SECONDS) -> Result:
    """Run a mod's own action through the engine's gate, on the server.

    `action_class` is the action's script class name. There is deliberately no
    verb dictionary: the same word means different things in a mod depending on
    context, so applicability is decided by the ACTION'S OWN `Can()` -- and its
    refusal is a meaningful test result, not a tool failure. The distinguishable
    refusals, classified in the mod before the engine is touched: the manager is
    busy; the player is already acting; the player is sprinting; the action
    class is unknown; and "the action's own Can() said no" -- the last one being
    the answer this tool exists to produce.

    `target_class` names the config class of the object to aim at (resolved to
    the first match near the player); many actions take no target and it can be
    omitted. `subject` optionally names a Man-derived entity class to act AS
    instead of the connected player -- a diagnostic escape hatch, because a
    spawned survivor owns an action manager while not being counted as a
    player.

    "Accepted" is not success: the engine can drop an accepted action one frame
    later without clearing it. The mod therefore holds the command `running`
    until the manager actually releases the action, and any failure path
    releases it too -- otherwise that player could never act again for the rest
    of the session. Expect an answer only after the action has genuinely ended;
    a stuck action fails by the mod's own 20s watchdog, with the release noted
    in the detail.
    """
    return _run("action", _args(
        action=action_class,
        target_class=target_class or None,
        subject=subject or None,
        radius=radius,
        pos=pos or None,
    ), timeout)


def world_delete(class_name: str, radius: float = 30.0, pos: str = "",
                 timeout: float = WORLD_TIMEOUT_SECONDS) -> Result:
    """Delete every object of `class_name` within `radius` of `pos` (or of the
    player, when `pos` is omitted).

    The class is required: the mod will not delete everything nearby regardless
    of class, and the radius is clamped on its side. Players are never deleted,
    whatever the class filter says.
    """
    return _run("delete", _args(**{"class": class_name, "radius": radius, "pos": pos or None}), timeout)


def world_exec(verb: str, args: dict | None = None,
               timeout: float = WORLD_TIMEOUT_SECONDS) -> Result:
    """Send an arbitrary verb through the bridge -- the debugging escape hatch,
    not a testing path.

    This server does not know the verb, does not validate its arguments beyond
    stringifying them, and does not answer for what the mod does with it; every
    answer is marked `non_standard` to say so. Anything a mod's behaviour can
    express as an ACTION should go through `world_action` instead, where the
    mod's own `Can()` gives the refusal meaning.

    A verb this bridge build does not know comes back as a failure listing the
    verbs it does -- that is the mod answering, not this tool guessing. A
    project that needs its own verb adds it to ITS OWN copy of the bridge's
    dispatcher (`IsKnownVerb`, the routing, and a handler); this server ships
    no registration machinery on purpose, because a verb the server typed and
    validated would be a verb the server answers for.

    The verb must be lowercase ASCII (letters, digits, underscore, up to 41
    chars): the mod recovers a command's id by a raw string search when a parse
    fails, and the id embeds the verb -- characters outside that set can make a
    failure impossible to correlate, which is the silence this product exists
    to remove.
    """
    if not _VERB_RE.fullmatch(verb or ""):
        return fail(
            f"world_exec refuses the verb {verb!r}: verbs are lowercase ASCII -- a letter "
            "followed by letters, digits or underscores, at most 41 characters",
            hint="quotes, spaces, and non-ASCII in a verb can make a failed command "
                 "impossible to correlate on the mod side; rename the verb",
        )

    result = _run(verb, dict(args or {}), timeout)
    if isinstance(result.data, dict):
        result.data["non_standard"] = True
        result.data["note"] = _NON_STANDARD_NOTE
    return result
