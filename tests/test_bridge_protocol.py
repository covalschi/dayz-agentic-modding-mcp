import json

from dayz_mcp.bridge.protocol import (
    BridgeState,
    Command,
    CommandState,
    classify_timeout,
    new_command_id,
    parse_state,
)


def _state_json(**overrides) -> str:
    payload = {
        "tick": 42,
        "command": {
            "id": "ping-1000-1",
            "status": "done",
            "detail": "pong",
            "finished_at": 1000.5,
        },
        "errors": ["earlier hiccup"],
        "world": {"player": {"health": 100}},
    }
    payload.update(overrides)
    return json.dumps(payload)


# --- Command -----------------------------------------------------------


def test_command_to_json_round_trips_fields():
    cmd = Command(id="ping-1000-1", verb="ping", args={"x": 1})
    decoded = json.loads(cmd.to_json())
    assert decoded == {"id": "ping-1000-1", "verb": "ping", "args": {"x": 1}}


# --- parse_state: the happy path ---------------------------------------


def test_parse_valid_state_returns_populated_bridge_state():
    state = parse_state(_state_json())
    assert state == BridgeState(
        tick=42,
        command=CommandState(
            id="ping-1000-1", status="done", detail="pong", finished_at=1000.5
        ),
        errors=["earlier hiccup"],
        world={"player": {"health": 100}},
    )


# --- parse_state: torn reads are normal, not exceptional ----------------


def test_parse_torn_json_returns_none_instead_of_raising():
    # A write caught mid-overwrite: valid prefix, cut off before the object closes.
    # This must not propagate json.JSONDecodeError -- a torn read is the ordinary
    # case for this file, once a second, forever.
    torn = _state_json()[:40]
    assert parse_state(torn) is None


def test_parse_empty_string_returns_none():
    assert parse_state("") is None


def test_parse_json_that_is_not_an_object_returns_none():
    # Structurally valid JSON, but not the shape the mod ever writes.
    assert parse_state("[1, 2, 3]") is None


def test_parse_state_with_unknown_status_returns_none():
    # A torn write can also land on syntactically valid JSON with a garbled
    # value -- e.g. a string cut short so "done" becomes "do". Since the
    # status set is closed, anything outside it is not trustworthy data.
    bad = _state_json(command={"id": "x", "status": "dun", "detail": "", "finished_at": None})
    assert parse_state(bad) is None


# --- parse_state: no command has ever been received ---------------------


def test_parse_state_without_command_block_has_no_command():
    payload = json.loads(_state_json())
    del payload["command"]
    state = parse_state(json.dumps(payload))
    assert state is not None
    assert state.command is None


def test_parse_state_with_null_command_has_no_command():
    state = parse_state(_state_json(command=None))
    assert state is not None
    assert state.command is None


# --- correlation: a state can be reporting on someone else's command ----


def test_state_reporting_a_different_command_id_is_not_our_answer():
    """The state carries the id of whatever command the mod last knew about.
    If that id isn't the one we sent, the status inside tells us nothing
    about our own command -- the caller must check the id before trusting
    status/detail. parse_state's job is to preserve that id exactly, byte for
    byte, so the check is even possible; a version that dropped, truncated or
    otherwise mangled the id would silently defeat correlation."""
    our_id = "ping-2000-7"
    other_id = "someone-elses-command-99"
    state = parse_state(_state_json(command={
        "id": other_id,
        "status": "done",
        "detail": "looks like success",
        "finished_at": 2001.0,
    }))
    assert state is not None
    assert state.command is not None
    # Fidelity: the id we get back is exactly the id that was written, not a
    # mangled or partial version of it.
    assert state.command.id == other_id
    # Correlation: a caller comparing against its own id must be able to
    # reject this state as an answer to something else entirely.
    assert state.command.id != our_id


# --- new_command_id -------------------------------------------------------


def test_new_command_id_contains_the_verb():
    assert new_command_id("ping").startswith("ping-")


def test_two_consecutively_created_ids_differ():
    first = new_command_id("ping")
    second = new_command_id("ping")
    assert first != second


# --- classify_timeout ------------------------------------------------------


def test_classify_timeout_waiting_before_the_deadline():
    assert classify_timeout(sent_at=100.0, now=104.9, timeout=5.0) == "waiting"


def test_classify_timeout_expired_exactly_at_the_deadline():
    assert classify_timeout(sent_at=100.0, now=105.0, timeout=5.0) == "expired"


def test_classify_timeout_expired_well_past_the_deadline():
    assert classify_timeout(sent_at=100.0, now=999.0, timeout=5.0) == "expired"


def test_classify_timeout_counts_from_sent_at_not_from_zero():
    # Same gap (4s), but shifted far along the timeline -- must not be
    # confused with "elapsed since program start" or similar.
    assert classify_timeout(sent_at=1_000_000.0, now=1_000_004.0, timeout=5.0) == "waiting"
    assert classify_timeout(sent_at=1_000_000.0, now=1_000_005.0, timeout=5.0) == "expired"
