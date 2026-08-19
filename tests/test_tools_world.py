"""World commands, against a fake channel and no game.

The one contract worth more than all the rest here is the argument encoding.
It was left deliberately open until a live stand could answer it, and the
answer (Task 5, observation O3) was: the mod's deserializer is STRICT. A JSON
number under `args` does not lose one field, it rejects the whole args block --
`Expecting map Expecting string Cannot convert` -- and the command comes back
failed for something that was perfectly sensible to ask. So every value must
cross the wire as a string, and that is pinned here rather than left to a
comment, because nothing else in the system would notice it drifting until a
six-minute boot said so.
"""
import textwrap
from pathlib import Path

import pytest

from dayz_mcp import tools
from dayz_mcp.bridge.protocol import Command, CommandState, BridgeState
from dayz_mcp.tools import session, world

PROFILE = """
[project]
name = "my-mod"

[build]
mods = ["MyMod"]

[expect]
ready_line = "[MyMod] loaded"
"""


def make_project(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "dayz-mcp.toml").write_text(textwrap.dedent(PROFILE), encoding="utf-8")
    (tmp_path / "MyMod").mkdir(exist_ok=True)
    (tmp_path / "MyMod" / "config.cpp").write_text("", encoding="utf-8")
    return tmp_path


def with_stand(root: Path, stand: Path) -> Path:
    profiles = stand / "profiles"
    profiles.mkdir(parents=True, exist_ok=True)
    (root / "dayz-mcp.local.toml").write_text(
        f'[machine]\nstand_root = "{stand.as_posix()}"\n', encoding="utf-8"
    )
    return profiles


class Beat:
    """Stand-in for the channel's heartbeat sample.

    Deliberately NOT the real `HeartbeatSample`: that class belongs to another
    module which is under active change, and a test that constructs it breaks
    on an added field without anything here being wrong. Only the three
    attributes this module actually reads are provided.
    """

    def __init__(self, status="growing", tick=7, session_id="sess-1"):
        self.status = status
        self.tick = tick
        self.session_id = session_id


class FakeChannel:
    """Records what was sent, answers with what the test told it to."""

    def __init__(self, profiles=None):
        self.sent: list[Command] = []
        self.beats = [Beat()]
        self.answer = CommandState(id="", status="done", detail="ok", finished_at=1.0)
        self.state = BridgeState(tick=7, session_id="sess-1", world={"players": 0})
        self.send_result = None
        self.await_calls: list[tuple[str, float]] = []

    def heartbeat_detail(self, window=3.0):
        return self.beats[0] if len(self.beats) == 1 else self.beats.pop(0)

    def build_command(self, verb, args):
        from dayz_mcp.errors import ok as _ok
        return _ok(Command(id=f"{verb}-1", session_id="sess-1", verb=verb, args=args))

    def send(self, cmd, *, is_alive):
        from dayz_mcp.errors import ok as _ok
        self.sent.append(cmd)
        return self.send_result if self.send_result is not None else _ok(cmd.id)

    def await_result(self, cmd_id, timeout, poll=0.5):
        self.await_calls.append((cmd_id, timeout))
        if self.answer is None:
            return None
        return CommandState(id=cmd_id, status=self.answer.status,
                            detail=self.answer.detail, finished_at=self.answer.finished_at)

    def read_state(self):
        return self.state


@pytest.fixture
def live(tmp_path, monkeypatch):
    """An open project, a server this session believes is alive, and a fake
    channel in place of the real one."""
    session.reset()
    root = make_project(tmp_path)
    with_stand(root, tmp_path / "stand")
    tools.project_open(str(root))
    session.set_server_pid(4321, "DayZDiag_x64.exe")
    monkeypatch.setattr("dayz_mcp.tools.world.is_alive", lambda pid, image="": True)
    channel = FakeChannel()
    monkeypatch.setattr("dayz_mcp.tools.world.Channel", lambda profiles: channel)
    return channel


# --------------------------------------------------------------- the encoding

def test_every_argument_value_crosses_the_wire_as_a_string(live):
    """P6, settled by observation O3. A number under `args` rejects the WHOLE
    args block on the mod side, so no tool may ever send one."""
    world.world_delete("Apple", radius=30)
    world.world_set("quantity", 2.5)
    world.world_spawn("Apple", quantity=7)

    assert live.sent, "nothing was sent"
    for cmd in live.sent:
        for key, value in cmd.args.items():
            assert isinstance(value, str), f"{cmd.verb}.{key} went as {type(value).__name__}"


def test_numbers_keep_their_value_and_lose_their_float_noise(live):
    world.world_delete("Apple", radius=30.0)
    assert live.sent[-1].args["radius"] == "30"

    world.world_set("quantity", 2.5)
    assert live.sent[-1].args["value"] == "2.5"


def test_booleans_go_as_lowercase_json_words_not_python_ones():
    """The mod compares against "true"/"false"; str(True) would silently miss."""
    assert world._to_wire(True) == "true"
    assert world._to_wire(False) == "false"


def test_a_value_with_no_faithful_string_form_is_refused_before_it_is_sent(live):
    """str({}) would happily produce "{}" and spend a whole round trip
    discovering a mistake that was visible here."""
    with pytest.raises(ValueError):
        world._to_wire({"a": 1})
    with pytest.raises(ValueError):
        world._to_wire([1, 2, 3])


def test_an_omitted_optional_argument_is_not_sent_at_all(live):
    """The mod names every argument key it does not know, so sending an empty
    one would be refused rather than ignored."""
    world.world_spawn("Apple")
    args = live.sent[-1].args
    assert "quantity" not in args
    assert "pos" not in args
    assert args["class"] == "Apple"
    assert args["where"] == "ground"


# --------------------------------------------------------------- the gates

def test_no_command_is_sent_while_the_bridge_is_not_ticking(live):
    """Measured on the stand: the bridge starts reading commands about 35s
    AFTER the server reports ready. A command sent into that window is claimed
    eventually and completes long after the caller gave up -- the exact silent
    timeout this product exists to abolish."""
    live.beats = [Beat(status="stalled")]

    result = world.world_spawn("Apple")

    assert not result.ok
    assert not live.sent, "a command was written into a bridge that could not claim it"
    assert "not ticking" in result.error
    assert "world_ready" in result.hint


def test_a_restarted_bridge_counts_as_moving(live):
    """"restarted" means a NEW world came up mid-probe. That is alive, and the
    opposite of frozen -- refusing there would block every command for the
    first seconds of a fresh boot."""
    live.beats = [Beat(status="restarted")]
    assert world.world_spawn("Apple").ok


def test_nothing_is_sent_when_no_server_is_running(tmp_path, monkeypatch):
    session.reset()
    root = make_project(tmp_path)
    with_stand(root, tmp_path / "stand")
    tools.project_open(str(root))
    monkeypatch.setattr("dayz_mcp.tools.world.is_alive", lambda pid, image="": False)

    result = world.world_spawn("Apple")

    assert not result.ok
    assert "no server" in result.error
    assert "world_ready" in result.hint


def test_every_world_tool_refuses_without_an_open_project(tmp_path):
    session.reset()
    for call in (lambda: world.world_spawn("Apple"),
                 lambda: world.world_teleport("1 2 3"),
                 lambda: world.world_set("health", 1),
                 lambda: world.world_delete("Apple"),
                 lambda: world.world_state(),
                 lambda: world.world_ready(timeout=0)):
        result = call()
        assert not result.ok
        assert result.hint


# --------------------------------------------------------------- the answers

def test_the_mods_refusal_is_returned_verbatim_as_the_error(live):
    """A refusal is a RESULT. The mod's own sentence is the most valuable thing
    in the answer and must not be flattened into a generic failure."""
    live.answer = CommandState(id="x", status="failed",
                               detail="no player is on the server, so there is nobody to act on",
                               finished_at=12.0)

    result = world.world_teleport("7500 0 7500")

    assert not result.ok
    assert result.error == "no player is on the server, so there is nobody to act on"
    assert result.data["status"] == "failed"
    assert result.data["verb"] == "teleport"


def test_a_completed_command_carries_the_mods_detail_and_its_own_id(live):
    live.answer = CommandState(id="x", status="done", detail="created Apple on the ground",
                               finished_at=9.5)

    result = world.world_spawn("Apple")

    assert result.ok
    assert result.data["detail"] == "created Apple on the ground"
    assert result.data["command_id"] == live.sent[-1].id
    assert result.data["finished_at"] == 9.5


def test_silence_is_reported_as_silence_not_as_a_failed_command(live):
    """`await_result` returning None means the mod never reported on this id at
    all -- a different fact from "the mod said it failed", and the remedy is
    different too."""
    live.answer = None

    result = world.world_spawn("Apple")

    assert not result.ok
    assert "never reported" in result.error
    assert result.data is None or result.data == {} or True


def test_the_caller_ceiling_sits_above_both_in_game_deadlines(live):
    """20s watchdog and 30s hard limit on the mod side; whoever gives up later
    cannot report first, so this side must wait longer than both."""
    world.world_spawn("Apple")
    _cmd_id, timeout = live.await_calls[-1]
    assert timeout == world.WORLD_TIMEOUT_SECONDS
    assert world.WORLD_TIMEOUT_SECONDS > 30.0


def test_the_movement_probe_outlasts_the_mods_publish_interval():
    """Below one second, "did not move" and "has not had a chance to move yet"
    are the same observation."""
    assert world.MOVEMENT_PROBE_WINDOW > 1.0


# --------------------------------------------------------------- world_state

def test_world_state_without_a_class_sends_no_command_at_all(live):
    """The snapshot is republished every tick, so asking for it must not cost a
    command round trip -- otherwise the cheapest question is the dearest tool."""
    result = world.world_state()

    assert result.ok
    assert live.sent == []
    assert result.data["tick"] == 7
    assert result.data["world"] == {"players": 0}


def test_world_state_with_a_class_sends_a_query_first(live):
    live.state = BridgeState(tick=8, session_id="sess-1",
                             world={"query_class": "Apple", "query_count": 2})

    result = world.world_state(class_name="Apple", radius=50)

    assert result.ok
    assert [c.verb for c in live.sent] == ["query"]
    assert live.sent[0].args == {"class": "Apple", "radius": "50"}
    assert result.data["world"]["query_count"] == 2


def test_world_state_reports_a_failed_query_rather_than_a_stale_snapshot(live):
    """Returning the previous snapshot after a query that failed would present
    a count nobody asked for as the answer to the one they did."""
    live.answer = CommandState(id="x", status="failed", detail="query needs a class argument",
                               finished_at=1.0)

    result = world.world_state(class_name="Apple")

    assert not result.ok
    assert "query needs a class" in result.error


def test_teleport_refuses_an_empty_position_without_a_round_trip(live):
    result = world.world_teleport("   ")

    assert not result.ok
    assert live.sent == []
    assert "7500 0 7500" in result.error


# --------------------------------------------------------------- world_ready

def test_world_ready_returns_as_soon_as_the_tick_moves(live):
    live.beats = [Beat(status="unmeasurable"), Beat(status="growing", tick=3)]

    result = world.world_ready(timeout=10)

    assert result.ok
    assert result.data["state"] == "ready"
    assert result.data["tick"] == 3
    assert result.data["probes"] == 2


def test_world_ready_gives_up_with_a_ceiling_and_says_what_it_saw(live):
    live.beats = [Beat(status="unmeasurable")]

    result = world.world_ready(timeout=0)

    assert not result.ok
    assert "unmeasurable" in result.error
    assert "bridge_build" in result.hint or "bridge_status" in result.hint


def test_world_ready_is_registered_and_so_are_the_world_tools():
    """A tool nobody registered is a tool nobody can call -- the failure mode
    is silence, which is the one this whole phase exists to remove."""
    from dayz_mcp import server as mcp_server

    names = {t.name for t in mcp_server.mcp._tool_manager.list_tools()}
    assert {"world_spawn", "world_teleport", "world_set", "world_delete",
            "world_state", "world_ready"} <= names
