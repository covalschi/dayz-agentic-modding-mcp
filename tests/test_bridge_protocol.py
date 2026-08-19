import json

from dayz_mcp.bridge.protocol import (
    STATUSES,
    BridgeState,
    Command,
    CommandState,
    classify_timeout,
    new_command_id,
    parse_rejection,
    parse_state,
)


def _state_json(**overrides) -> str:
    payload = {
        "tick": 42,
        "session_id": "boot-1000",
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
    cmd = Command(id="ping-1000-1", session_id="boot-1000", verb="ping", args={"x": 1})
    decoded = json.loads(cmd.to_json())
    assert decoded == {
        "id": "ping-1000-1", "session_id": "boot-1000", "verb": "ping", "args": {"x": 1}
    }


# --- parse_state: the happy path ---------------------------------------


def test_parse_valid_state_returns_populated_bridge_state():
    state = parse_state(_state_json())
    assert state == BridgeState(
        tick=42,
        session_id="boot-1000",
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


# --- session_id: required KEY, required VALUE, opaque, exact ---------------


def test_parse_state_without_session_id_returns_none():
    # Required for the same reason tick is required (see BridgeState's
    # docstring): a state JSON that parses but omits it entirely comes from
    # something that does not speak this version of the protocol, which is
    # the same "cannot make sense of it" case as every other malformed field.
    payload = json.loads(_state_json())
    del payload["session_id"]
    assert parse_state(json.dumps(payload)) is None


def test_parse_state_preserves_session_id_exactly():
    # Fidelity matters here for the same reason it matters for command.id:
    # Channel.heartbeat compares session_id across two samples by equality
    # to detect a restart, so a version that mangled or truncated it would
    # silently defeat that comparison (report a restart that didn't happen,
    # or miss one that did).
    state = parse_state(_state_json(session_id="boot-restart-42"))
    assert state is not None
    assert state.session_id == "boot-restart-42"


def test_parse_state_with_null_session_id_returns_none():
    # A required KEY is not a required VALUE: null is present, but it is
    # not a session id -- str(None) == "None" would otherwise silently
    # "work" and compare equal to itself across every future boot too.
    assert parse_state(_state_json(session_id=None)) is None


def test_parse_state_with_empty_string_session_id_returns_none():
    # The single most plausible Enforce-side mistake: an unset `string`
    # field serialises as "", not as an absent key -- the missing-KEY guard
    # above does not catch this, only a value check does. An empty string
    # that stays constant across every boot would silently defeat restart
    # detection (heartbeat would never see it change), which is the entire
    # point of this field.
    assert parse_state(_state_json(session_id="")) is None


def test_parse_state_with_numeric_session_id_returns_none():
    # 0 is falsy but not "missing" to a careless `if raw["session_id"]:`
    # check, and str(0) == "0" would "work" as a constant, wrong session id.
    assert parse_state(_state_json(session_id=0)) is None


def test_parse_state_with_boolean_session_id_returns_none():
    assert parse_state(_state_json(session_id=False)) is None


def test_parse_state_with_non_empty_string_session_id_still_parses():
    # The validation added above must not become so strict it rejects the
    # ordinary case -- confirms it is narrowly "must be a real, non-empty
    # string", not something stricter that would break legitimate ids.
    state = parse_state(_state_json(session_id="a-real-session-id"))
    assert state is not None
    assert state.session_id == "a-real-session-id"


# --- tick: required, and must be a genuine JSON integer ---------------------


def test_parse_state_with_string_tick_returns_none():
    # int("42") would otherwise silently accept a numeric string.
    assert parse_state(_state_json(tick="42")) is None


def test_parse_state_with_boolean_tick_returns_none():
    # bool is a subtype of int in Python -- int(True) == 1 would otherwise
    # silently accept true/false as a tick.
    assert parse_state(_state_json(tick=True)) is None


def test_parse_state_with_float_tick_returns_none():
    # int(7.9) truncates to 7 instead of rejecting a value that was never a
    # whole tick count to begin with.
    assert parse_state(_state_json(tick=7.9)) is None


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


# --- parse_rejection: WHY parse_state returned None -------------------------


def test_parse_rejection_is_none_for_a_valid_document():
    # Nothing to explain -- must not invent a rejection for a document that
    # actually parsed fine.
    assert parse_rejection(_state_json()) is None


def test_parse_rejection_is_none_for_a_torn_write():
    # The ordinary, once-a-second case this whole module treats as
    # unremarkable stays unremarkable here too -- a torn write must never
    # be reported as a schema rejection, or every routine mid-write read
    # would start looking like a mod bug.
    torn = _state_json()[:40]
    assert parse_state(torn) is None  # sanity: this really is the torn-write path
    assert parse_rejection(torn) is None


def test_parse_rejection_for_non_object_root():
    rejection = parse_rejection("[1, 2, 3]")
    assert rejection is not None
    assert rejection.field == "<root>"
    assert rejection.value == [1, 2, 3]


def test_parse_rejection_for_missing_status():
    payload = json.loads(_state_json())
    del payload["command"]["status"]
    rejection = parse_rejection(json.dumps(payload))
    assert rejection is not None
    assert rejection.field == "command.status"
    assert "missing" in rejection.reason


def test_parse_rejection_for_bad_status():
    rejection = parse_rejection(_state_json(
        command={"id": "x", "status": "dun", "detail": "", "finished_at": None}
    ))
    assert rejection is not None
    assert rejection.field == "command.status"
    assert rejection.value == "dun"
    # The reason should actually be useful -- name the closed set, not just
    # say "invalid".
    for known in STATUSES:
        assert known in rejection.reason


def test_parse_rejection_for_errors_not_a_list():
    rejection = parse_rejection(_state_json(errors="none"))
    assert rejection is not None
    assert rejection.field == "errors"
    assert rejection.value == "none"


def test_parse_rejection_for_world_not_an_object():
    rejection = parse_rejection(_state_json(world="everything"))
    assert rejection is not None
    assert rejection.field == "world"
    assert rejection.value == "everything"


def test_parse_rejection_for_missing_tick():
    payload = json.loads(_state_json())
    del payload["tick"]
    rejection = parse_rejection(json.dumps(payload))
    assert rejection is not None
    assert rejection.field == "tick"
    assert "missing" in rejection.reason


def test_parse_rejection_for_tick_not_a_genuine_int():
    for bad_tick in ("7", 7.0, True):
        rejection = parse_rejection(_state_json(tick=bad_tick))
        assert rejection is not None, f"tick={bad_tick!r} should have been rejected"
        assert rejection.field == "tick"
        assert rejection.value == bad_tick


def test_parse_rejection_for_missing_session_id():
    payload = json.loads(_state_json())
    del payload["session_id"]
    rejection = parse_rejection(json.dumps(payload))
    assert rejection is not None
    assert rejection.field == "session_id"
    assert "missing" in rejection.reason


def test_parse_rejection_for_session_id_not_a_non_empty_string():
    for bad_session_id in ("", None, 0, False):
        rejection = parse_rejection(_state_json(session_id=bad_session_id))
        assert rejection is not None, f"session_id={bad_session_id!r} should have been rejected"
        assert rejection.field == "session_id"
        assert rejection.value == bad_session_id


def test_parse_rejection_and_parse_state_agree_on_which_documents_are_bad():
    # Every document parse_rejection explains must also be one parse_state
    # rejects, and vice versa (excluding the torn-write case, covered
    # separately above) -- the two must never disagree about WHETHER a
    # document is bad, only about whether the reason is known.
    bad_docs = [
        "[1, 2, 3]",
        _state_json(command={"id": "x", "status": "dun", "detail": "", "finished_at": None}),
        _state_json(errors="none"),
        _state_json(world="everything"),
        _state_json(tick="7"),
        _state_json(session_id=""),
    ]
    for doc in bad_docs:
        assert parse_state(doc) is None
        assert parse_rejection(doc) is not None
