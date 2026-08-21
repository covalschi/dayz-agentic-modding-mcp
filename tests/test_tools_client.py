"""Tests for the client tools: lifecycle, eyes, hands, text, verdict.

NOTHING here starts a game, needs a virtual gamepad driver, or calls a
Windows-only API. That is a hard requirement rather than a convenience: the
suite has to stay runnable on a machine with no game installed and no kernel
driver, and the REFUSALS are themselves most of what is under test.

The one behavioural invariant this file guards hardest is the focus contract:
exactly one tool in the set takes the foreground, and it is proved by watching
who calls `winui.focus`, not by reading prose.
"""
import json
import textwrap
import time
from pathlib import Path

import pytest

from dayz_mcp import gamepad, winui
from dayz_mcp import server as mcp_server
from dayz_mcp import tools
from dayz_mcp.errors import Result, fail, ok
from dayz_mcp.tools import client, lifecycle, session

# A pid no process on this machine can hold (Windows pids stay far below this),
# so a test that reaches a kill path by mistake still cannot touch anything.
UNREACHABLE_PID = 4_000_000_001

PROFILE = """
[project]
name = "my-mod"

[build]
mods = ["MyMod"]

[expect]
ready_line = "[MyMod] loaded"
forbid = ["Bad type"]
"""


@pytest.fixture(autouse=True)
def _clean_session():
    """No test may inherit -- or leak -- a tracked pid.

    These tests record client pids in the process-wide session, and a leaked
    one would make an unrelated test file's `client_*` call act on a pid this
    file invented. Reset both ends.
    """
    session.reset()
    client._start_in_flight.update(job_id="", store=None)
    yield
    session.reset()
    # The start slot is process-global by design (one client profile
    # directory, one machine). A test that deliberately leaves a start
    # in flight would otherwise refuse every later one in this process.
    client._start_in_flight.update(job_id="", store=None)


@pytest.fixture(autouse=True)
def _no_real_ports(monkeypatch):
    """The stand check reads netstat, and this machine really does have a
    neighbouring stand come and go on udp/2302. A unit test that consults
    global machine state is flaky by construction, so the default here is
    "nothing holds any port"; the tests about the port override it."""
    monkeypatch.setattr(client, "udp_port_holders", lambda port: [])


def make_project(tmp_path: Path, *, port: int = 2302) -> tuple[Path, Path, Path]:
    """A project, a stand and a fake game install. Returns (root, stand, game)."""
    root = tmp_path / "project"
    root.mkdir(parents=True)
    (root / "dayz-mcp.toml").write_text(textwrap.dedent(PROFILE), encoding="utf-8")
    (root / "MyMod").mkdir()
    (root / "MyMod" / "config.cpp").write_text("", encoding="utf-8")

    stand = tmp_path / "stand"
    (stand / "profiles").mkdir(parents=True)
    game = tmp_path / "game"
    game.mkdir()
    (game / "DayZDiag_x64.exe").write_bytes(b"")

    (root / "dayz-mcp.local.toml").write_text(
        "[machine]\n"
        f'stand_root = "{stand.as_posix()}"\n'
        f'game = "{game.as_posix()}"\n'
        f"port = {port}\n",
        encoding="utf-8",
    )
    opened = tools.project_open(str(root))
    assert opened.ok, opened.error
    return root, stand, game


def publish_state(stand: Path, players, tick: int = 1, session_id: str = "sess-1") -> None:
    """Write a genuine bridge state document -- parsed by the real parser, so a
    test cannot pass against a shape the product would reject."""
    world = {} if players is None else {"players": players}
    (stand / "profiles" / "dayz_mcp_state.json").write_text(
        json.dumps({"tick": tick, "session_id": session_id, "world": world}),
        encoding="utf-8",
    )


def live_stand(monkeypatch, alive: set) -> None:
    """Make the session believe its own server is up, and let the test decide
    which pids are alive."""
    session.set_server_pid(4321, "DayZDiag_x64.exe")
    alive.add(4321)
    monkeypatch.setattr(client, "is_alive", lambda pid, image="": pid in alive)


def with_pause_mode(stand: Path, value) -> None:
    """Write the client's settings file the way the game writes it."""
    users = stand / "clientprofile" / "Users" / "Survivor"
    users.mkdir(parents=True, exist_ok=True)
    body = "" if value is None else f"pauseMode={value};\n"
    (users / "someone_settings.DayZProfile").write_text(
        "sceneComplexity=200000;\n" + body, encoding="utf-8"
    )


def started_client(tmp_path, monkeypatch, *, players=1, pause_mode=2):
    """The common arrangement: a live stand, a connected client, fast polling.

    Returns (root, stand, game, spawned) where `spawned` collects command lines.
    """
    root, stand, game = make_project(tmp_path)
    with_pause_mode(stand, pause_mode)
    alive = {777}
    live_stand(monkeypatch, alive)
    spawned: list[list[str]] = []

    def fake_spawn(cmd, cwd):
        spawned.append(list(cmd))
        return 777

    monkeypatch.setattr(client, "spawn", fake_spawn)
    monkeypatch.setattr(client, "CONNECT_POLL_SECONDS", 0.02)
    publish_state(stand, players)
    return root, stand, game, spawned


def wait_for_job(job_id: str, seconds: float = 5.0):
    deadline = time.time() + seconds
    while time.time() < deadline:
        job = session.jobs().get(job_id)
        if job is not None and job.status in ("done", "failed"):
            return job
        time.sleep(0.02)
    return session.jobs().get(job_id)


# ---------------------------------------------------------------------------
# lifecycle
# ---------------------------------------------------------------------------


def test_every_client_tool_refuses_without_a_project():
    for call in (
        lambda: client.client_start(timeout=1),
        client.client_status,
        client.client_shot,
        lambda: client.client_move(0, 1, 0),
        lambda: client.client_look(0, 1, 0),
        lambda: client.client_press("a"),
        lambda: client.client_type("x"),
        client.client_verdict,
    ):
        result = call()
        assert result.ok is False
        assert "no project" in result.error


def test_client_start_refuses_when_no_stand_is_running(tmp_path, monkeypatch):
    """The client CONNECTS -- it does not listen. With nothing to join it sits
    at the browser forever, which is the silent failure this refusal removes."""
    root, stand, game = make_project(tmp_path)
    monkeypatch.setattr(client, "is_alive", lambda pid, image="": False)

    result = client.client_start(timeout=1)

    assert result.ok is False
    assert "2302" in result.error
    assert "server_start" in result.hint


def test_client_start_accepts_a_stand_this_session_did_not_start(tmp_path, monkeypatch):
    """A stand holding the port is a stand the client can join, whoever started
    it. It is not refused -- it is named, because the readiness signal is read
    out of THIS project's profile directory, which only that stand writes if it
    happens to use the same one."""
    root, stand, game = make_project(tmp_path)
    monkeypatch.setattr(client, "is_alive", lambda pid, image="": False)
    monkeypatch.setattr(client, "udp_port_holders", lambda port: [9999])
    monkeypatch.setattr(client, "spawn", lambda cmd, cwd: 777)
    monkeypatch.setattr(client, "CONNECT_POLL_SECONDS", 0.02)

    result = client.client_start(timeout=1)

    assert result.ok is True
    assert result.data["stand"]["port_holders"] == [9999]
    assert result.data["stand"]["started_by_this_session"] is False
    assert "not started by this session" in result.data["stand"]["note"]


def test_client_start_launches_windowed_and_connects_to_the_stand(tmp_path, monkeypatch):
    """The window flag is not cosmetic: a fullscreen D3D window will not yield
    the foreground, and a probe that tried to cover one hung."""
    root, stand, game, spawned = started_client(tmp_path, monkeypatch)

    started = client.client_start(timeout=5)
    assert started.ok is True
    wait_for_job(started.data["job_id"])

    cmd = spawned[0]
    assert cmd[0].endswith("DayZDiag_x64.exe")
    assert "-window" in cmd
    assert "-connect=127.0.0.1" in cmd
    assert "-port=2302" in cmd
    assert f"-profiles={stand / 'clientprofile'}" in cmd
    assert any(part.startswith("-mod=") for part in cmd)


def test_readiness_is_the_player_count_not_a_timer(tmp_path, monkeypatch):
    """The measured connect time is ~50s and it varies. A timer would call a
    client ready that is still loading, and would call a client that never
    joined a success."""
    root, stand, game, _spawned = started_client(tmp_path, monkeypatch, players=0)

    started = client.client_start(timeout=5)
    job_id = started.data["job_id"]

    # Long enough for several poll rounds: the job must NOT finish on time
    # alone while the bridge still reports nobody connected.
    time.sleep(0.3)
    assert session.jobs().get(job_id).status == "running"

    publish_state(stand, 1)
    job = wait_for_job(job_id)
    assert job.status == "done"
    assert "player" in job.summary


def test_client_start_fails_the_job_when_the_client_dies_before_connecting(tmp_path, monkeypatch):
    root, stand, game = make_project(tmp_path)
    with_pause_mode(stand, 2)
    alive = set()
    live_stand(monkeypatch, alive)  # the server is alive, the client is not
    monkeypatch.setattr(client, "spawn", lambda cmd, cwd: 777)
    monkeypatch.setattr(client, "CONNECT_POLL_SECONDS", 0.02)
    publish_state(stand, 0)

    started = client.client_start(timeout=5)
    job = wait_for_job(started.data["job_id"])

    assert job.status == "failed"
    assert "died" in job.error


def test_a_client_that_never_connects_says_whether_the_bridge_was_readable(tmp_path, monkeypatch):
    """Two different failures wear the same face. "The bridge never published"
    means the signal was unavailable (the mod may not be loaded at all);
    "players stayed 0" means the client itself never joined."""
    root, stand, game, _spawned = started_client(tmp_path, monkeypatch, players=0)
    started = client.client_start(timeout=0.2)
    job = wait_for_job(started.data["job_id"])
    assert job.status == "failed"
    assert "stayed at 0" in job.error
    assert "client_verdict" in job.error

    session.reset()
    root, stand, game, _spawned = started_client(tmp_path / "second", monkeypatch, players=None)
    (stand / "profiles" / "dayz_mcp_state.json").unlink()
    started = client.client_start(timeout=0.2)
    job = wait_for_job(started.data["job_id"])
    assert job.status == "failed"
    assert "bridge_status" in job.error


def test_client_start_refuses_a_second_client(tmp_path, monkeypatch):
    root, stand, game, _spawned = started_client(tmp_path, monkeypatch)
    first = client.client_start(timeout=5)
    wait_for_job(first.data["job_id"])

    second = client.client_start(timeout=5)

    assert second.ok is False
    assert "already running" in second.error
    assert "client_stop" in second.hint


def test_client_start_refuses_a_second_start_that_is_still_in_flight(tmp_path, monkeypatch):
    """The pid check cannot close this window on its own: the call returns as
    soon as the worker has the job, and the pid only exists once that worker
    has spawned. Two calls in quick succession would otherwise start two
    clients, each several gigabytes, both writing one profile directory."""
    root, stand, game, _spawned = started_client(tmp_path, monkeypatch, players=0)

    first = client.client_start(timeout=5)
    second = client.client_start(timeout=5)

    assert first.ok is True
    assert second.ok is False
    assert "already running" in second.error
    assert first.data["job_id"] in second.hint


def test_the_start_slot_is_given_back_when_the_job_ends(tmp_path, monkeypatch):
    root, stand, game, _spawned = started_client(tmp_path, monkeypatch, players=1)
    first = client.client_start(timeout=5)
    wait_for_job(first.data["job_id"])
    session.set_client_pid(0)  # pretend the client exited; the slot must be free

    again = client.client_start(timeout=5)

    assert again.ok is True
    assert again.data["job_id"] != first.data["job_id"]


def test_client_start_warns_when_pause_mode_is_not_the_background_value(tmp_path, monkeypatch):
    """Background capture and background gamepad both rest on this setting.
    Without a warning a user with a different one sees the eyes and the pad
    "just stop working" with nothing said anywhere."""
    root, stand, game, _spawned = started_client(tmp_path, monkeypatch, pause_mode=0)

    started = client.client_start(timeout=5)

    assert started.ok is True
    assert started.data["background"]["background_verified"] is False
    assert "UPDATE IN BACKGROUND" in started.data["warning"]
    job = wait_for_job(started.data["job_id"])
    assert "pauseMode" in job.summary


def test_client_start_says_nothing_alarming_when_pause_mode_is_the_measured_value(tmp_path, monkeypatch):
    root, stand, game, _spawned = started_client(tmp_path, monkeypatch, pause_mode=2)

    started = client.client_start(timeout=5)

    assert started.data["background"]["background_verified"] is True
    assert "warning" not in started.data


def test_client_start_never_rewrites_the_owners_setting(tmp_path, monkeypatch):
    """It is the machine owner's setting, changed from inside the game."""
    root, stand, game, _spawned = started_client(tmp_path, monkeypatch, pause_mode=0)
    settings = next((stand / "clientprofile").rglob("*_settings.DayZProfile"))
    before = settings.read_bytes()

    client.client_start(timeout=5)

    assert settings.read_bytes() == before


def test_client_start_refuses_extras_that_collide_with_arguments_it_owns(tmp_path, monkeypatch):
    root, stand, game, _spawned = started_client(tmp_path, monkeypatch)
    for arg in ("-connect=1.2.3.4", "-port=2402", "-profiles=C:/x", "-mod=@Dep", "-window"):
        result = client.client_start(timeout=1, extra_args=[arg])
        assert result.ok is False, arg
        assert arg.split("=", 1)[0] in result.error


def test_client_stop_reports_when_nothing_was_started(tmp_path, monkeypatch):
    make_project(tmp_path)
    result = client.client_stop()
    assert result.ok is True
    assert result.data["stopped"] is False


def test_client_stop_kills_the_tracked_client_and_unplugs_the_pad(tmp_path, monkeypatch):
    root, stand, game, _spawned = started_client(tmp_path, monkeypatch)
    started = client.client_start(timeout=5)
    wait_for_job(started.data["job_id"])
    killed = []
    monkeypatch.setattr(client, "stop", lambda pid: killed.append(pid) or True)
    closed = []
    monkeypatch.setattr(gamepad, "close_pad", lambda: closed.append(True) or ok({"pad": "closed"}))

    result = client.client_stop()

    assert result.ok is True
    assert killed == [777]
    assert closed == [True]
    assert session.client_pid() == 0


def test_client_status_reports_the_window_the_setting_and_the_player_count(tmp_path, monkeypatch):
    root, stand, game, _spawned = started_client(tmp_path, monkeypatch, players=1)
    started = client.client_start(timeout=5)
    wait_for_job(started.data["job_id"])
    monkeypatch.setattr(
        winui, "geometry",
        lambda pid: ok({"hwnd": 1, "client_width": 1600, "client_height": 900,
                        "minimized": False, "foreground": False}),
    )

    result = client.client_status()

    assert result.ok is True
    assert result.data["pid"] == 777
    assert result.data["running"] is True
    assert result.data["window"]["client_width"] == 1600
    assert result.data["background"]["pause_mode"] == 2
    assert result.data["players"] == 1
    assert result.data["gamepad"]["pad"] == "closed"


# ---------------------------------------------------------------------------
# eyes
# ---------------------------------------------------------------------------


def test_client_shot_returns_the_path_and_the_size(tmp_path, monkeypatch):
    root, stand, game, _spawned = started_client(tmp_path, monkeypatch)
    started = client.client_start(timeout=5)
    wait_for_job(started.data["job_id"])

    def fake_shot(pid, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(b"x" * 1234)
        return ok({"path": str(path), "width": 1600, "height": 900,
                   "bytes": 1234, "lit_fraction": 0.42, "foreground": False})

    monkeypatch.setattr(winui, "shot", fake_shot)

    result = client.client_shot()

    assert result.ok is True
    assert Path(result.data["path"]).exists()
    assert result.data["bytes"] == 1234


def test_client_shot_refuses_when_no_client_is_tracked(tmp_path):
    make_project(tmp_path)
    result = client.client_shot()
    assert result.ok is False
    assert "client_start" in result.hint


def test_a_background_shot_with_the_wrong_pause_mode_warns_about_a_frozen_frame(tmp_path, monkeypatch):
    """lit_fraction cannot see this: a frozen frame is a perfectly bright
    picture of a moment that has passed."""
    root, stand, game, _spawned = started_client(tmp_path, monkeypatch, pause_mode=0)
    started = client.client_start(timeout=5)
    wait_for_job(started.data["job_id"])
    monkeypatch.setattr(
        winui, "shot",
        lambda pid, path: ok({"path": str(path), "width": 8, "height": 8, "bytes": 9,
                              "lit_fraction": 0.9, "foreground": False}),
    )

    result = client.client_shot()

    assert result.ok is True
    assert "pauseMode" in result.data["warning"]


def test_a_foreground_shot_does_not_warn_about_the_setting(tmp_path, monkeypatch):
    root, stand, game, _spawned = started_client(tmp_path, monkeypatch, pause_mode=0)
    started = client.client_start(timeout=5)
    wait_for_job(started.data["job_id"])
    monkeypatch.setattr(
        winui, "shot",
        lambda pid, path: ok({"path": str(path), "width": 8, "height": 8, "bytes": 9,
                              "lit_fraction": 0.9, "foreground": True}),
    )

    result = client.client_shot()

    assert "warning" not in result.data


# ---------------------------------------------------------------------------
# hands -- the gamepad, in the background
# ---------------------------------------------------------------------------


def test_the_pad_tools_pass_through_and_report_what_the_pad_did(tmp_path, monkeypatch):
    root, stand, game, _spawned = started_client(tmp_path, monkeypatch)
    started = client.client_start(timeout=5)
    wait_for_job(started.data["job_id"])
    calls = []
    monkeypatch.setattr(gamepad, "move", lambda x, y, s: calls.append(("move", x, y, s)) or ok({"stick": "left"}))
    monkeypatch.setattr(gamepad, "look", lambda x, y, s: calls.append(("look", x, y, s)) or ok({"stick": "right"}))
    monkeypatch.setattr(gamepad, "press", lambda b, s: calls.append(("press", b, s)) or ok({"button": b}))

    assert client.client_move(0, 1, 6).data["stick"] == "left"
    assert client.client_look(1, 0, 1).data["stick"] == "right"
    assert client.client_press("back").data["button"] == "back"
    assert calls == [("move", 0, 1, 6), ("look", 1, 0, 1), ("press", "back", 0.1)]


def test_the_pad_tools_refuse_when_the_client_is_gone(tmp_path, monkeypatch):
    root, stand, game, _spawned = started_client(tmp_path, monkeypatch)
    started = client.client_start(timeout=5)
    wait_for_job(started.data["job_id"])
    monkeypatch.setattr(client, "is_alive", lambda pid, image="": False)

    for call in (lambda: client.client_move(0, 1, 1),
                 lambda: client.client_look(0, 1, 1),
                 lambda: client.client_press("a")):
        result = call()
        assert result.ok is False
        assert "client_start" in result.hint


def test_an_unknown_button_is_refused_with_the_names(tmp_path, monkeypatch):
    root, stand, game, _spawned = started_client(tmp_path, monkeypatch)
    started = client.client_start(timeout=5)
    wait_for_job(started.data["job_id"])

    result = client.client_press("triangle")

    assert result.ok is False
    assert "back" in result.hint


# ---------------------------------------------------------------------------
# text -- the two cases the server must tell apart
# ---------------------------------------------------------------------------


def test_client_chat_goes_through_the_bridge(tmp_path, monkeypatch):
    make_project(tmp_path)
    sent = {}

    def fake_command(verb, args, timeout):
        sent["verb"] = verb
        sent["args"] = args
        return ok({"verb": verb, "status": "done", "detail": "said it"})

    monkeypatch.setattr(client, "_world_command", fake_command)

    result = client.client_chat("hello")

    assert result.ok is True
    assert sent["verb"] == "chat"
    assert sent["args"]["text"] == "hello"


def test_client_chat_never_touches_the_foreground(tmp_path, monkeypatch):
    """The half the first draft of this contract missed: an agent that knows
    the general rule about typing will go and fight for the foreground unless
    the chat tool says it is not needed."""
    make_project(tmp_path)
    monkeypatch.setattr(client, "_world_command", lambda v, a, t: ok({"status": "done"}))
    focused = []
    monkeypatch.setattr(winui, "focus", lambda pid, **kw: focused.append(pid) or True)

    client.client_chat("hello")

    assert focused == []
    # And it must SAY so: an agent that knows the rule but not the exception
    # will take the foreground anyway, at the owner's expense, for nothing.
    assert "focus is not needed" in client.client_chat.__doc__.lower()


def test_client_chat_explains_a_bridge_build_that_has_no_chat_verb(tmp_path, monkeypatch):
    """Chat is delivered server-side, so the verb has to exist in the bridge.
    The mod's own refusal names the verbs it knows; the hint has to name the
    fix, not leave the caller staring at a list."""
    make_project(tmp_path)
    monkeypatch.setattr(
        client, "_world_command",
        lambda v, a, t: Result(False, {"verb": v}, "unknown verb 'chat'; this build knows: ping", ""),
    )

    result = client.client_chat("hello")

    assert result.ok is False
    assert "bridge_build" in result.hint


def test_client_chat_refuses_empty_text(tmp_path, monkeypatch):
    make_project(tmp_path)
    monkeypatch.setattr(client, "_world_command", lambda v, a, t: ok({}))
    result = client.client_chat("   ")
    assert result.ok is False


def test_client_type_refuses_when_the_foreground_cannot_be_taken(tmp_path, monkeypatch):
    """Cause, action -- and NOT a false substitute. A mod's input field exists
    only on the client; telling the caller to use the bridge instead would send
    them to a tool that cannot do the job."""
    root, stand, game, _spawned = started_client(tmp_path, monkeypatch)
    started = client.client_start(timeout=5)
    wait_for_job(started.data["job_id"])
    monkeypatch.setattr(winui, "focus", lambda pid, **kw: False)
    monkeypatch.setattr(winui, "foreground_pid", lambda: 4242)
    typed = []
    monkeypatch.setattr(winui, "type_text", lambda pid, text: typed.append(text) or ok({}))

    result = client.client_type("hello")

    assert result.ok is False
    assert typed == []
    assert "4242" in result.error
    assert "foreground" in result.hint.lower()
    # The refusal must not send the caller to the bridge for a mod's own field.
    assert "client_chat is not" in result.hint or "not a way around" in result.hint


def test_client_type_reports_that_it_took_the_foreground(tmp_path, monkeypatch):
    """A side effect on the person at the machine, named out loud -- the same
    treatment plugging in the virtual controller gets."""
    root, stand, game, _spawned = started_client(tmp_path, monkeypatch)
    started = client.client_start(timeout=5)
    wait_for_job(started.data["job_id"])
    monkeypatch.setattr(winui, "focus", lambda pid, **kw: True)
    monkeypatch.setattr(winui, "type_text", lambda pid, text: ok({"typed": text, "characters": len(text)}))

    result = client.client_type("hello")

    assert result.ok is True
    assert result.data["foreground_taken"] is True
    assert "foreground" in result.data["side_effect"].lower()


def test_client_type_can_submit_the_field(tmp_path, monkeypatch):
    root, stand, game, _spawned = started_client(tmp_path, monkeypatch)
    started = client.client_start(timeout=5)
    wait_for_job(started.data["job_id"])
    monkeypatch.setattr(winui, "focus", lambda pid, **kw: True)
    monkeypatch.setattr(winui, "type_text", lambda pid, text: ok({"typed": text, "characters": len(text)}))
    keys = []
    monkeypatch.setattr(winui, "press_key", lambda pid, name: keys.append(name) or ok({"key": name}))

    result = client.client_type("hello", submit=True)

    assert result.ok is True
    assert keys == ["enter"]
    assert result.data["submitted"] is True


def test_client_type_refuses_untypeable_text_before_stealing_the_screen(tmp_path, monkeypatch):
    """Order matters: taking the foreground away from the owner and only then
    discovering the string cannot be typed costs them the screen for nothing."""
    root, stand, game, _spawned = started_client(tmp_path, monkeypatch)
    started = client.client_start(timeout=5)
    wait_for_job(started.data["job_id"])
    focused = []
    monkeypatch.setattr(winui, "focus", lambda pid, **kw: focused.append(pid) or True)

    result = client.client_type("\u043f\u0440\u0438\u0432\u0456\u0442")

    assert result.ok is False
    assert focused == []


def registered_client_tools() -> set[str]:
    """Every client tool this module exports, DERIVED rather than listed.

    The invariant below used to enumerate six tools by hand, which left
    client_start, client_stop and client_verdict unwatched -- adding a focus
    grab to two of them kept the whole suite green. An invariant that does not
    cover tools added later is not an invariant, so the names come from the
    export list and the sweep asserts it covered all of them.

    client_compile_check shares the prefix and is registered, but belongs to
    lifecycle.py: it spawns a throwaway client of its own and has nothing to do
    with the live one.
    """
    return {
        name for name in tools.__all__
        if name.startswith("client_")
        and getattr(getattr(tools, name), "__module__", "") == client.__name__
    }


def test_exactly_one_client_tool_asks_for_the_foreground(tmp_path, monkeypatch):
    """The invariant the whole phase rests on, proved by watching who calls
    `focus` rather than by reading docstrings. Eyes, gamepad, chat, lifecycle
    and the verdict all work with the client in the background; only field
    entry does not.

    Every tool is called on a path where it does its REAL work, never an early
    refusal: a refusal returns before reaching the body, so a focus grab hidden
    below it would go unseen -- which is exactly how the hand-written version of
    this test missed three tools. Hence the ordering: start first (nothing is
    tracked yet), stop last (it clears the pid).
    """
    root, stand, game, _spawned = started_client(tmp_path, monkeypatch, players=1)
    (stand / "clientprofile").mkdir(parents=True, exist_ok=True)
    (stand / "clientprofile" / "DayZ_x64_probe.RPT").write_text("starting\n", encoding="utf-8")

    asked = []
    monkeypatch.setattr(winui, "focus", lambda pid, **kw: asked.append(pid) or True)
    monkeypatch.setattr(winui, "shot", lambda pid, path: ok({"path": str(path), "bytes": 1,
                                                            "foreground": False}))
    monkeypatch.setattr(gamepad, "move", lambda x, y, s: ok({}))
    monkeypatch.setattr(gamepad, "look", lambda x, y, s: ok({}))
    monkeypatch.setattr(gamepad, "press", lambda b, s: ok({}))
    monkeypatch.setattr(gamepad, "close_pad", lambda: ok({"pad": "closed"}))
    monkeypatch.setattr(client, "_world_command", lambda v, a, t: ok({}))
    monkeypatch.setattr(winui, "geometry", lambda pid: ok({"minimized": False, "foreground": False}))
    monkeypatch.setattr(client, "stop", lambda pid: True)

    started = client.client_start(timeout=5)
    assert started.ok is True
    assert wait_for_job(started.data["job_id"]).status == "done"
    swept = {"client_start"}

    for name, call in (
        ("client_status", client.client_status),
        ("client_shot", client.client_shot),
        ("client_move", lambda: client.client_move(0, 1, 0)),
        ("client_look", lambda: client.client_look(0, 1, 0)),
        ("client_press", lambda: client.client_press("a")),
        ("client_chat", lambda: client.client_chat("hi")),
        ("client_verdict", client.client_verdict),
        # Last on purpose: it clears the tracked pid, and everything above
        # would then refuse before reaching its own body.
        ("client_stop", client.client_stop),
    ):
        result = call()
        assert result.ok is True, (name, result.error)
        swept.add(name)

    missed = registered_client_tools() - swept - {"client_type"}
    assert not missed, f"these client tools escaped the foreground sweep: {sorted(missed)}"
    assert asked == [], "a tool other than client_type asked for the foreground"

    # And the one that is allowed to, does.
    session.set_client_pid(777, client.CLIENT_IMAGE)
    monkeypatch.setattr(winui, "type_text", lambda pid, text: ok({"typed": text, "characters": 1}))
    assert client.client_type("a").ok is True
    assert asked == [777]


def test_the_client_pid_never_becomes_something_server_stop_can_kill(tmp_path, monkeypatch):
    """The client runs the SAME executable as the server, so the image check
    that protects server_stop from a recycled pid cannot tell the two apart.
    known_pids is server_stop's only other guard -- if a client pid ever
    entered it, server_stop(pid=...) would sail through and kill the client.

    Guarded on the property rather than on the reading: adding
    known_pids.add(pid) to set_client_pid left the whole suite green.
    """
    make_project(tmp_path)
    killed = []
    monkeypatch.setattr(lifecycle, "is_alive", lambda pid, image="": False)
    monkeypatch.setattr(lifecycle, "stop", lambda pid: killed.append(pid) or True)

    session.set_client_pid(UNREACHABLE_PID, client.CLIENT_IMAGE)

    assert session.known_pid(UNREACHABLE_PID) is False
    refused = tools.server_stop(pid=UNREACHABLE_PID)
    assert refused.ok is False
    assert "not one this session started" in refused.error
    assert killed == []
    assert session.client_pid() == UNREACHABLE_PID


def test_the_two_text_tools_say_which_case_they_are(tmp_path):
    """The server's job is not to repeat the general rule about typing -- it is
    to say which of the two cases a call is: chat (server-side, free) or a
    mod's own field (client-side, costs the foreground)."""
    chat = client.client_chat.__doc__
    field = client.client_type.__doc__
    assert "bridge" in chat.lower()
    assert "chat" in field.lower()  # it says what it is NOT for
    assert "active window" in field.lower() or "foreground" in field.lower()


def test_the_background_tools_say_they_need_no_focus():
    for fn in (client.client_shot, client.client_move, client.client_look, client.client_press):
        doc = fn.__doc__.lower()
        assert "focus" in doc or "foreground" in doc or "background" in doc, fn.__name__


# ---------------------------------------------------------------------------
# the client's own verdict
# ---------------------------------------------------------------------------


def test_client_verdict_judges_the_clients_rpt(tmp_path, monkeypatch):
    root, stand, game = make_project(tmp_path)
    profiles = stand / "clientprofile"
    profiles.mkdir(parents=True, exist_ok=True)
    (profiles / "DayZ_x64_2026.RPT").write_text(
        "starting\nBad type in something\n", encoding="utf-8"
    )

    result = client.client_verdict()

    assert result.ok is True
    assert result.data["verdict"] == "fail"
    assert result.data["log"].endswith(".RPT")


def test_client_verdict_says_it_reads_the_rpt_not_the_script_log(tmp_path):
    root, stand, game = make_project(tmp_path)
    profiles = stand / "clientprofile"
    profiles.mkdir(parents=True, exist_ok=True)
    (profiles / "DayZ_x64_2026.RPT").write_text("starting\n", encoding="utf-8")

    result = client.client_verdict()

    assert "RPT" in result.data["source"]
    assert "script" in result.data["note"].lower()


def test_client_verdict_without_a_run_names_client_start(tmp_path):
    root, stand, game = make_project(tmp_path)
    result = client.client_verdict()
    assert result.ok is False
    assert "client_start" in result.hint


def test_client_verdict_refuses_a_report_older_than_the_run(tmp_path):
    root, stand, game = make_project(tmp_path)
    profiles = stand / "clientprofile"
    profiles.mkdir(parents=True, exist_ok=True)
    old = profiles / "DayZ_x64_2026.RPT"
    old.write_text("starting\n", encoding="utf-8")

    result = client.client_verdict(since=time.time() + 60)

    assert result.ok is False
    assert "predates" in result.error


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------


CLIENT_TOOL_NAMES = (
    "client_start", "client_stop", "client_status", "client_shot",
    "client_move", "client_look", "client_press",
    "client_chat", "client_type", "client_verdict",
)


def test_every_client_tool_is_exported_and_registered():
    for name in CLIENT_TOOL_NAMES:
        assert hasattr(tools, name), name
        assert name in tools.__all__, name


@pytest.mark.anyio
async def test_the_client_tools_are_registered_with_their_real_signatures():
    """functools.wraps is not optional: without it FastMCP builds the schema
    from `*args, **kwargs` and the agent gets opaque fields instead of named
    ones. That was a phase-1 defect."""
    listed = {tool.name: tool for tool in await mcp_server.mcp.list_tools()}
    for name in CLIENT_TOOL_NAMES:
        assert name in listed, name
    assert "text" in listed["client_chat"].inputSchema["properties"]
    assert "button" in listed["client_press"].inputSchema["properties"]
    assert "submit" in listed["client_type"].inputSchema["properties"]


@pytest.mark.anyio
async def test_no_tool_in_the_client_namespace_is_registered_without_a_description():
    """FastMCP takes a tool's description from its docstring, and an agent
    browsing this namespace sees the descriptions and nothing else. One tool
    reading `<none>` beside well-described siblings is a tool that gets used
    last or not at all -- which is what client_compile_check was, phase-1 and
    docstringless, sitting in the middle of the client_* list."""
    listed = await mcp_server.mcp.list_tools()
    bare = [
        tool.name for tool in listed
        if tool.name.startswith("client_") and not (tool.description or "").strip()
    ]
    assert bare == []


def test_importing_the_client_tools_does_not_touch_the_driver_or_the_window_layer():
    """The module must import on a machine with no ViGEmBus and no game: the
    gamepad's own import is lazy, and everything Windows-only in winui sits
    behind a platform guard."""
    import subprocess
    import sys

    out = subprocess.run(
        [sys.executable, "-c",
         "import sys; import dayz_mcp.tools.client as c; "
         "print('vgamepad' in sys.modules)"],
        capture_output=True, text=True, check=False,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "False"
