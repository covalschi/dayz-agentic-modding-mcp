"""server_start / server_status / server_stop, and the readiness signals.

Split out of test_tools.py, which had grown into six suites in one file.
"""
import os
import textwrap
import time
from pathlib import Path

import pytest

from dayz_mcp import tools
from dayz_mcp.procs import process_mods_tail as procs_process_mods_tail
from dayz_mcp.procs import udp_port_holders as procs_udp_port_holders
from dayz_mcp.tools import lifecycle, session

from conftest import PROFILE_WITHOUT_READY_LINE, make_project, with_stand_and_game


def test_server_start_returns_since_matching_the_job_it_created(tmp_path, monkeypatch):
    root = make_project(tmp_path)
    stand, game = tmp_path / "stand", tmp_path / "game"
    with_stand_and_game(root, stand, game)
    (stand / "serverDZ.cfg").write_text("", encoding="utf-8")
    tools.project_open(str(root))

    monkeypatch.setattr("dayz_mcp.tools.lifecycle.spawn", lambda cmd, cwd: 111)
    monkeypatch.setattr("dayz_mcp.tools.lifecycle.is_alive", lambda pid, image="": False)  # dies instantly

    started = tools.server_start(timeout=5)
    assert started.ok, started.error
    since = started.data["since"]
    waited = tools.job_wait(started.data["job_id"], timeout=5)
    assert waited.data["status"] == "failed"
    assert waited.data["started"] == since


# --- Final review, item 4: a project that cannot declare a ready line waited
# the whole timeout and then reported a failure that never happened ---


def test_server_start_finishes_promptly_when_no_ready_line_is_declared(tmp_path, monkeypatch):
    """profile.py already notes that readiness cannot be detected without
    expect.ready_line, and server_start ignored the note: it polled for a
    marker that is the empty string, could never match, and after the full
    timeout (420s by default) failed with "no ready line within 420s" -- a
    false failure, seven minutes late, for a case the README nowhere calls
    unsupported.

    The wait is what is wrong, so the wait is what goes. The server is
    started, confirmed alive, and the job finishes saying what it can and
    cannot know."""
    root = make_project(tmp_path, PROFILE_WITHOUT_READY_LINE)
    stand, game = tmp_path / "stand", tmp_path / "game"
    with_stand_and_game(root, stand, game)
    (stand / "serverDZ.cfg").write_text("", encoding="utf-8")
    opened = tools.project_open(str(root))
    assert any("readiness cannot be detected" in n for n in opened.data["notes"])

    monkeypatch.setattr("dayz_mcp.tools.lifecycle.spawn", lambda cmd, cwd: 4321)
    monkeypatch.setattr("dayz_mcp.tools.lifecycle.is_alive", lambda pid, image="": True)
    monkeypatch.setattr("dayz_mcp.tools.lifecycle.NO_READY_LINE_SETTLE_SECONDS", 0.05)
    monkeypatch.setattr("dayz_mcp.tools.lifecycle.PORT_READY_WAIT_SECONDS", 0.3)
    monkeypatch.setattr("dayz_mcp.tools.lifecycle.BOOT_POLL_SECONDS", 0.02)
    # The port signal is watched for on this path now, bounded by its own
    # constant rather than by  -- squeezed here so the test still
    # asserts what it was written to assert: this configuration answers
    # promptly instead of waiting out a timeout for a signal that never comes.
    monkeypatch.setattr("dayz_mcp.tools.lifecycle.PORT_READY_WAIT_SECONDS", 0.2)

    began = time.time()
    started = tools.server_start(timeout=300)
    assert started.ok, started.error
    waited = tools.job_wait(started.data["job_id"], timeout=20)
    elapsed = time.time() - began

    assert waited.data["status"] == "done", waited.data
    assert elapsed < 20, f"waited {elapsed:.0f}s for a job that has nothing to wait for"
    assert "4321" in waited.data["summary"]
    assert "readiness cannot be detected" in waited.data["summary"]
    assert "errors" in waited.data["summary"]
    assert session.server_pid() == 4321  # still tracked, so server_stop can reach it


def test_server_start_without_a_ready_line_still_reports_a_server_that_died(tmp_path, monkeypatch):
    """Not waiting for readiness must not become not looking at all: if the
    process is gone by the time it is checked, that is a failed boot, and the
    only signal this configuration has left."""
    root = make_project(tmp_path, PROFILE_WITHOUT_READY_LINE)
    stand, game = tmp_path / "stand", tmp_path / "game"
    with_stand_and_game(root, stand, game)
    (stand / "serverDZ.cfg").write_text("", encoding="utf-8")
    tools.project_open(str(root))

    monkeypatch.setattr("dayz_mcp.tools.lifecycle.spawn", lambda cmd, cwd: 4321)
    monkeypatch.setattr("dayz_mcp.tools.lifecycle.is_alive", lambda pid, image="": False)
    monkeypatch.setattr("dayz_mcp.tools.lifecycle.NO_READY_LINE_SETTLE_SECONDS", 0.05)
    monkeypatch.setattr("dayz_mcp.tools.lifecycle.PORT_READY_WAIT_SECONDS", 0.3)
    monkeypatch.setattr("dayz_mcp.tools.lifecycle.BOOT_POLL_SECONDS", 0.02)

    job_id = tools.server_start(timeout=300).data["job_id"]
    waited = tools.job_wait(job_id, timeout=20)

    assert waited.data["status"] == "failed"
    assert "died" in waited.data["error"]


# --- Extra requirement 2: server_start must not delete old logs ---


def test_server_start_does_not_delete_pre_existing_logs(tmp_path, monkeypatch):
    root = make_project(tmp_path)
    stand, game = tmp_path / "stand", tmp_path / "game"
    with_stand_and_game(root, stand, game)
    (stand / "serverDZ.cfg").write_text("", encoding="utf-8")
    tools.project_open(str(root))

    old_log = stand / "profiles" / "script_old.log"
    old_log.write_text("leftover from a previous boot\n", encoding="utf-8")

    monkeypatch.setattr("dayz_mcp.tools.lifecycle.spawn", lambda cmd, cwd: 999)
    monkeypatch.setattr("dayz_mcp.tools.lifecycle.is_alive", lambda pid, image="": False)

    job_id = tools.server_start(timeout=5).data["job_id"]
    tools.job_wait(job_id, timeout=5)
    assert old_log.exists()
    assert old_log.read_text(encoding="utf-8") == "leftover from a previous boot\n"


def test_server_start_ignores_a_stale_log_that_already_contains_the_marker(tmp_path, monkeypatch):
    root = make_project(tmp_path)
    stand, game = tmp_path / "stand", tmp_path / "game"
    with_stand_and_game(root, stand, game)
    (stand / "serverDZ.cfg").write_text("", encoding="utf-8")
    tools.project_open(str(root))

    stale = stand / "profiles" / "script_old.log"
    stale.write_text("[MyMod] loaded\n", encoding="utf-8")
    old_time = time.time() - 500
    os.utime(stale, (old_time, old_time))

    monkeypatch.setattr("dayz_mcp.tools.lifecycle.spawn", lambda cmd, cwd: 999)
    monkeypatch.setattr("dayz_mcp.tools.lifecycle.is_alive", lambda pid, image="": True)  # keeps "running"
    # The fact under test is that a log older than this run cannot answer for
    # it; the ceiling only has to be long enough to look more than once.
    monkeypatch.setattr("dayz_mcp.tools.lifecycle.BOOT_POLL_SECONDS", 0.02)

    job_id = tools.server_start(timeout=0.2).data["job_id"]
    waited = tools.job_wait(job_id, timeout=8)
    assert waited.data["status"] == "failed"
    assert "ready line" in waited.data["error"]


# --- Extra requirement 3: refuse a second server_start while one is already running ---


def test_server_start_refuses_when_already_running(tmp_path, monkeypatch):
    root = make_project(tmp_path)
    tools.project_open(str(root))
    session.set_server_pid(4242)
    monkeypatch.setattr("dayz_mcp.tools.lifecycle.is_alive", lambda pid, image="": True)
    r = tools.server_start()
    assert not r.ok
    assert "already running" in r.error
    assert "server_stop" in r.hint


# --- Extra requirement 4: the port comes from the profile, not a constant ---


def test_server_start_uses_the_configured_port(tmp_path, monkeypatch):
    root = make_project(tmp_path)
    stand, game = tmp_path / "stand", tmp_path / "game"
    with_stand_and_game(root, stand, game, port=27016)
    (stand / "serverDZ.cfg").write_text("", encoding="utf-8")
    tools.project_open(str(root))

    captured = {}

    def fake_spawn(cmd, cwd):
        captured["cmd"] = cmd
        return 123

    monkeypatch.setattr("dayz_mcp.tools.lifecycle.spawn", fake_spawn)
    monkeypatch.setattr("dayz_mcp.tools.lifecycle.is_alive", lambda pid, image="": False)

    job_id = tools.server_start(timeout=5).data["job_id"]
    tools.job_wait(job_id, timeout=5)
    assert "-port=27016" in captured["cmd"]


# --- machine.server: running the stand from the dedicated server install ---


def _boot_capturing(tmp_path, monkeypatch, **stand_kwargs) -> dict:
    """Boot far enough to see the command line and the working directory."""
    session.reset()
    root = make_project(tmp_path)
    stand, game = tmp_path / "stand", tmp_path / "game"
    with_stand_and_game(root, stand, game, **stand_kwargs)
    (stand / "serverDZ.cfg").write_text("", encoding="utf-8")
    tools.project_open(str(root))

    captured = {}

    def fake_spawn(cmd, cwd):
        captured["cmd"] = cmd
        captured["cwd"] = cwd
        return 123

    monkeypatch.setattr("dayz_mcp.tools.lifecycle.spawn", fake_spawn)
    monkeypatch.setattr("dayz_mcp.tools.lifecycle.is_alive", lambda pid, image="": False)

    captured["result"] = tools.server_start(timeout=5)
    if captured["result"].ok:
        tools.job_wait(captured["result"].data["job_id"], timeout=5)
    return captured


def test_server_start_runs_the_client_diag_image_when_machine_server_is_unset(
    tmp_path, monkeypatch
):
    """The historical shape. A profile written before machine.server existed
    must boot exactly as it did, out of the client install."""
    got = _boot_capturing(tmp_path, monkeypatch)
    assert got["result"].ok, got["result"].error
    assert got["cmd"][0].endswith("DayZDiag_x64.exe")
    assert Path(got["cmd"][0]).parent == tmp_path / "game"
    assert Path(got["cwd"]) == tmp_path / "game"


def test_server_start_runs_the_dedicated_image_when_machine_server_is_set(
    tmp_path, monkeypatch
):
    """Both the image and the working directory move: DayZDiag forces
    $currentdir to its own directory, and the dedicated server resolves
    mpmissions and any proxy DLL beside the executable the same way."""
    got = _boot_capturing(tmp_path, monkeypatch, server_dir=tmp_path / "dedicated")
    assert got["result"].ok, got["result"].error
    assert got["cmd"][0].endswith("DayZServer_x64.exe")
    assert Path(got["cmd"][0]).parent == tmp_path / "dedicated"
    assert Path(got["cwd"]) == tmp_path / "dedicated"


def test_server_start_refuses_a_machine_server_without_the_dedicated_image(
    tmp_path, monkeypatch
):
    """A directory that is not a server install must be named as such. The
    engine's answer is a process that never appears, which reads as the mod
    failing to boot rather than as a mistyped path."""
    got = _boot_capturing(
        tmp_path, monkeypatch, server_dir=tmp_path / "dedicated", dedicated_image=False
    )
    assert not got["result"].ok
    assert "DayZServer_x64.exe" in got["result"].error
    assert "223350" in got["result"].hint
    assert "cmd" not in got, "it must refuse before spawning anything"


def test_server_image_recorded_for_liveness_is_the_one_that_was_launched(
    tmp_path, monkeypatch
):
    """is_alive compares the recorded image, so recording the client's would
    make a live dedicated server look dead to every later tool."""
    _boot_capturing(tmp_path, monkeypatch, server_dir=tmp_path / "dedicated")
    assert session.server_image() == "DayZServer_x64.exe"


# --- Extra requirement 5: server_status ---


def test_server_status_with_no_log_yet(tmp_path):
    root = make_project(tmp_path)
    tools.project_open(str(root))
    r = lifecycle.server_status(pulse_seconds=0.01)
    assert r.ok, r.error
    assert r.data["pid"] == 0
    assert r.data["running"] is False
    assert r.data["log"] is None
    assert r.data["growing"] is None
    assert r.data["stalled_seconds"] is None


def test_server_status_detects_a_growing_log(tmp_path, monkeypatch):
    root = make_project(tmp_path)
    stand = tmp_path / "stand"
    (stand / "profiles").mkdir(parents=True)
    log = stand / "profiles" / "script_1.log"
    log.write_text("hello\n", encoding="utf-8")
    (root / "dayz-mcp.local.toml").write_text(
        f'[machine]\nstand_root = "{stand.as_posix()}"\n', encoding="utf-8"
    )
    tools.project_open(str(root))

    def fake_sleep(_seconds):
        with log.open("a", encoding="utf-8") as fh:
            fh.write("more output\n")

    monkeypatch.setattr("dayz_mcp.tools.lifecycle.time.sleep", fake_sleep)
    r = lifecycle.server_status(pulse_seconds=0.01)
    assert r.ok, r.error
    assert r.data["growing"] is True


def test_server_status_detects_a_stalled_log(tmp_path, monkeypatch):
    root = make_project(tmp_path)
    stand = tmp_path / "stand"
    (stand / "profiles").mkdir(parents=True)
    log = stand / "profiles" / "script_1.log"
    log.write_text("hello\n", encoding="utf-8")
    (root / "dayz-mcp.local.toml").write_text(
        f'[machine]\nstand_root = "{stand.as_posix()}"\n', encoding="utf-8"
    )
    tools.project_open(str(root))

    monkeypatch.setattr("dayz_mcp.tools.lifecycle.time.sleep", lambda _s: None)
    r = lifecycle.server_status(pulse_seconds=0.01)
    assert r.ok, r.error
    assert r.data["growing"] is False
    assert r.data["stalled_seconds"] >= 0


# --- Extra requirement 6: -config must be absolute and inside stand_root ---


def test_server_start_passes_an_absolute_config_path(tmp_path, monkeypatch):
    root = make_project(tmp_path)
    stand, game = tmp_path / "stand", tmp_path / "game"
    with_stand_and_game(root, stand, game)
    (stand / "serverDZ.cfg").write_text("", encoding="utf-8")
    tools.project_open(str(root))

    captured = {}

    def fake_spawn(cmd, cwd):
        captured["cmd"] = cmd
        return 123

    monkeypatch.setattr("dayz_mcp.tools.lifecycle.spawn", fake_spawn)
    monkeypatch.setattr("dayz_mcp.tools.lifecycle.is_alive", lambda pid, image="": False)

    job_id = tools.server_start(timeout=5).data["job_id"]
    tools.job_wait(job_id, timeout=5)
    cfg_arg = next(a for a in captured["cmd"] if a.startswith("-config="))
    cfg_value = cfg_arg.split("=", 1)[1]
    assert Path(cfg_value).is_absolute()
    assert Path(cfg_value).name == "serverDZ.cfg"  # default filename, machine.config unset


def test_server_start_refuses_when_config_resolves_outside_stand_root(tmp_path):
    root = make_project(tmp_path)
    stand = tmp_path / "stand"
    (stand / "profiles").mkdir(parents=True)
    game = tmp_path / "game"
    game.mkdir()
    (game / "DayZDiag_x64.exe").write_bytes(b"")
    outside = tmp_path / "outside_real.cfg"
    outside.write_text("", encoding="utf-8")
    try:
        os.symlink(str(outside), str(stand / "serverDZ.cfg"))
    except OSError:
        pytest.skip("this machine does not permit unprivileged symlink creation")

    (root / "dayz-mcp.local.toml").write_text(
        f'[machine]\nstand_root = "{stand.as_posix()}"\ngame = "{game.as_posix()}"\n',
        encoding="utf-8",
    )
    tools.project_open(str(root))
    r = tools.server_start(timeout=5)
    assert not r.ok
    assert "outside stand_root" in r.error


# --- Extra requirement 7: -mod / -serverMod split ---


def test_mod_list_splits_server_only_mods_into_serverMod(tmp_path):
    root = make_project(tmp_path)
    (root / "dayz-mcp.local.toml").write_text(
        textwrap.dedent(
            """
            [mods]
            extra = ["D:/other/@ServerOnlyMod"]
            server_only = ["@ServerOnlyMod"]
            """
        ),
        encoding="utf-8",
    )
    tools.project_open(str(root))
    client_mods, server_mods = lifecycle.mod_list()
    assert "@ServerOnlyMod" in server_mods
    assert "@ServerOnlyMod" not in client_mods
    assert "@MyMod" in client_mods


def test_client_compile_check_excludes_server_only_mods(tmp_path, monkeypatch):
    root = make_project(tmp_path)
    game = tmp_path / "game"
    game.mkdir()
    (game / "DayZDiag_x64.exe").write_bytes(b"")
    (root / "dayz-mcp.local.toml").write_text(
        textwrap.dedent(
            f"""
            [machine]
            game = "{game.as_posix()}"

            [mods]
            extra = ["D:/other/@ServerOnlyMod"]
            server_only = ["@ServerOnlyMod"]
            """
        ),
        encoding="utf-8",
    )
    tools.project_open(str(root))

    captured = {}

    def fake_spawn(cmd, cwd):
        captured["cmd"] = cmd
        return 123

    monkeypatch.setattr("dayz_mcp.tools.lifecycle.spawn", fake_spawn)
    monkeypatch.setattr("dayz_mcp.tools.lifecycle.stop", lambda pid: True)
    monkeypatch.setattr("dayz_mcp.tools.lifecycle.time.sleep", lambda _s: None)

    job_id = tools.client_compile_check(wait_seconds=0).data["job_id"]
    tools.job_wait(job_id, timeout=5)
    mod_arg = next(a for a in captured["cmd"] if a.startswith("-mod="))
    assert "@ServerOnlyMod" not in mod_arg


# --- Acceptance-driven fix: machine.config makes the server config filename
# configurable, since a real stand can have a "serverDZ.cfg" that hangs forever
# after world-compile and a working config under a different name ---


def test_server_start_uses_the_configured_config_filename(tmp_path, monkeypatch):
    root = make_project(tmp_path)
    stand, game = tmp_path / "stand", tmp_path / "game"
    with_stand_and_game(root, stand, game, config="custom.cfg")
    (stand / "custom.cfg").write_text("", encoding="utf-8")
    tools.project_open(str(root))

    captured = {}

    def fake_spawn(cmd, cwd):
        captured["cmd"] = cmd
        return 123

    monkeypatch.setattr("dayz_mcp.tools.lifecycle.spawn", fake_spawn)
    monkeypatch.setattr("dayz_mcp.tools.lifecycle.is_alive", lambda pid, image="": False)

    job_id = tools.server_start(timeout=5).data["job_id"]
    tools.job_wait(job_id, timeout=5)
    cfg_arg = next(a for a in captured["cmd"] if a.startswith("-config="))
    cfg_value = cfg_arg.split("=", 1)[1]
    assert Path(cfg_value).is_absolute()
    assert Path(cfg_value) == (stand / "custom.cfg").resolve()


def test_server_start_missing_configured_file_names_the_file_and_the_key(tmp_path):
    root = make_project(tmp_path)
    stand, game = tmp_path / "stand", tmp_path / "game"
    with_stand_and_game(root, stand, game, config="custom.cfg")
    # Deliberately do NOT create stand/custom.cfg.
    tools.project_open(str(root))

    r = tools.server_start(timeout=5)
    assert not r.ok
    assert "custom.cfg" in r.error
    assert "machine.config" in r.hint


def test_server_start_refuses_when_a_custom_config_resolves_outside_stand_root(tmp_path):
    root = make_project(tmp_path)
    stand = tmp_path / "stand"
    (stand / "profiles").mkdir(parents=True)
    game = tmp_path / "game"
    game.mkdir()
    (game / "DayZDiag_x64.exe").write_bytes(b"")
    outside = tmp_path / "outside_real.cfg"
    outside.write_text("", encoding="utf-8")
    try:
        os.symlink(str(outside), str(stand / "custom.cfg"))
    except OSError:
        pytest.skip("this machine does not permit unprivileged symlink creation")

    (root / "dayz-mcp.local.toml").write_text(
        f'[machine]\nstand_root = "{stand.as_posix()}"\ngame = "{game.as_posix()}"\nconfig = "custom.cfg"\n',
        encoding="utf-8",
    )
    tools.project_open(str(root))
    r = tools.server_start(timeout=5)
    assert not r.ok
    assert "outside stand_root" in r.error


# --- P1: the bridge transport must not survive a boot -------------------------
#
# server_start only ever did profiles.mkdir(), so both dayz_mcp_cmd.json and
# dayz_mcp_state.json outlived a restart. Four confirmed defects came out of
# that one omission: a command written while the stand was down detonating at
# the first tick of a world the agent believes untouched; a wedge that survives
# every restart; a leftover state file keeping the tick large and positive,
# which makes bridge_status's no_state_file branch -- the only one whose hint
# says to build and wire the bridge -- unreachable for the life of the stand
# directory; and a published tick that goes DOWN across a restart, since the
# mod's counter restarts at 0 while the file keeps the old number.


def _spawn_capturing_profiles(captured):
    """A spawn stand-in that records what the transport looked like AT THE
    MOMENT the server was started -- the only instant that matters here."""

    def fake_spawn(cmd, cwd):
        profiles = Path(next(a for a in cmd if a.startswith("-profiles=")).split("=", 1)[1])
        captured["cmd_json"] = (profiles / "dayz_mcp_cmd.json").exists()
        captured["state_json"] = (profiles / "dayz_mcp_state.json").exists()
        captured["logs"] = sorted(p.name for p in profiles.glob("script_*.log"))
        return 4242

    return fake_spawn


def test_server_start_clears_the_bridge_transport_before_spawning(tmp_path, monkeypatch):
    root = make_project(tmp_path)
    stand, game = tmp_path / "stand", tmp_path / "game"
    with_stand_and_game(root, stand, game)
    (stand / "serverDZ.cfg").write_text("", encoding="utf-8")
    tools.project_open(str(root))

    profiles = stand / "profiles"
    profiles.mkdir(parents=True, exist_ok=True)
    (profiles / "dayz_mcp_cmd.json").write_text(
        '{"id": "spawn-1", "verb": "spawn", "args": {}}', encoding="utf-8"
    )
    (profiles / "dayz_mcp_state.json").write_text(
        '{"tick": 91234, "session_id": "an-old-boot"}', encoding="utf-8"
    )
    # The asymmetry: logs are a record of the past and are deliberately kept.
    (profiles / "script_old.log").write_text("leftover from a previous boot\n", encoding="utf-8")

    captured = {}
    monkeypatch.setattr("dayz_mcp.tools.lifecycle.spawn", _spawn_capturing_profiles(captured))
    monkeypatch.setattr("dayz_mcp.tools.lifecycle.is_alive", lambda pid, image="": False)

    started = tools.server_start(timeout=5)
    assert started.ok, started.error
    tools.job_wait(started.data["job_id"], timeout=10)

    assert captured["cmd_json"] is False, "a stale command was still in the mailbox at spawn time"
    assert captured["state_json"] is False, "a stale state file was still there at spawn time"
    assert captured["logs"] == ["script_old.log"], "the logs were cleared too"


def test_a_transport_file_that_cannot_be_removed_does_not_fail_the_boot(tmp_path, monkeypatch):
    """Clearing is hygiene, not a precondition. A file that cannot be removed
    must be reported and the boot must go ahead -- refusing to start a server
    over a leftover json would be a worse trade than booting with it."""
    # No ready line: this test is about the transport, so the boot should
    # settle and finish instead of polling for a marker nothing will print.
    root = make_project(tmp_path, PROFILE_WITHOUT_READY_LINE)
    stand, game = tmp_path / "stand", tmp_path / "game"
    with_stand_and_game(root, stand, game)
    (stand / "serverDZ.cfg").write_text("", encoding="utf-8")
    tools.project_open(str(root))

    profiles = stand / "profiles"
    profiles.mkdir(parents=True, exist_ok=True)
    mailbox = profiles / "dayz_mcp_cmd.json"
    mailbox.write_text('{"id": "spawn-1", "verb": "spawn", "args": {}}', encoding="utf-8")

    real_unlink = Path.unlink

    def unlink_that_fails_for_the_mailbox(self, *args, **kwargs):
        if self.name == "dayz_mcp_cmd.json":
            raise PermissionError(f"{self} is held open")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink_that_fails_for_the_mailbox)
    monkeypatch.setattr("dayz_mcp.tools.lifecycle.spawn", lambda cmd, cwd: 4242)
    monkeypatch.setattr("dayz_mcp.tools.lifecycle.is_alive", lambda pid, image="": True)
    monkeypatch.setattr("dayz_mcp.tools.lifecycle.NO_READY_LINE_SETTLE_SECONDS", 0.05)
    monkeypatch.setattr("dayz_mcp.tools.lifecycle.PORT_READY_WAIT_SECONDS", 0.3)
    monkeypatch.setattr("dayz_mcp.tools.lifecycle.BOOT_POLL_SECONDS", 0.02)

    started = tools.server_start(timeout=5)
    assert started.ok, started.error
    # Reported where the caller looks first...
    assert started.data["bridge_transport_left"], started.data
    assert "dayz_mcp_cmd.json" in started.data["bridge_transport_left"][0]

    waited = tools.job_wait(started.data["job_id"], timeout=10)
    assert waited.data["status"] == "done", waited.data
    # ...and on the job, which is what an agent reads afterwards.
    assert "dayz_mcp_cmd.json" in waited.data["summary"]
    assert mailbox.exists()  # and it really did survive, as reported


def test_a_clean_boot_says_nothing_about_the_transport(tmp_path, monkeypatch):
    """No leftovers, no noise: the field only appears when something is wrong."""
    root = make_project(tmp_path)
    stand, game = tmp_path / "stand", tmp_path / "game"
    with_stand_and_game(root, stand, game)
    (stand / "serverDZ.cfg").write_text("", encoding="utf-8")
    tools.project_open(str(root))

    monkeypatch.setattr("dayz_mcp.tools.lifecycle.spawn", lambda cmd, cwd: 4242)
    monkeypatch.setattr("dayz_mcp.tools.lifecycle.is_alive", lambda pid, image="": False)

    started = tools.server_start(timeout=5)
    assert started.ok, started.error
    assert "bridge_transport_left" not in started.data


# --- Readiness by port bind, and the collision it was really built for -------
#
# The premise this work started from -- "the engine relaunches itself and
# server_start reads that as death" -- turned out to be wrong, and the artifacts
# say what actually happened: two agents booting stands on ONE shared port and
# ONE shared -profiles directory. See the task report. What survives from it is
# a readiness signal that needs neither our tracked pid nor a mod, and a
# pre-flight check for the collision that really occurred.


def test_server_start_refuses_a_port_someone_else_is_holding(tmp_path, monkeypatch):
    """The check that would have prevented the failure. A stand is shared: one
    machine, one port, one profile directory. Booting into a held port produces
    a server that dies mid-world-load with nothing in its own log to say why.

    The owner has since authorised stopping a neighbouring stand that blocks a
    live run, so the stranger branch now OFFERS that -- but the offer must come
    with identification (the pid, and the -mod= tail where it can be read),
    because the caller is choosing what to kill. The tool itself still never
    auto-stops what it did not start."""
    root = make_project(tmp_path)
    stand, game = tmp_path / "stand", tmp_path / "game"
    with_stand_and_game(root, stand, game)
    (stand / "serverDZ.cfg").write_text("", encoding="utf-8")
    tools.project_open(str(root))

    monkeypatch.setattr("dayz_mcp.tools.lifecycle.udp_port_holders", lambda port: [4242])
    monkeypatch.setattr("dayz_mcp.tools.lifecycle.process_mods_tail",
                        lambda pid: "@CF;@Dep;@SomeDependency")
    spawned = []
    monkeypatch.setattr("dayz_mcp.tools.lifecycle.spawn",
                        lambda cmd, cwd: spawned.append(cmd) or 1)

    r = tools.server_start(timeout=5)
    assert not r.ok
    assert "4242" in r.error
    assert "2302" in r.error
    assert not spawned, "it started a server into a port it knew was taken"
    # Identification travels WITH the offer: the mod set is the one cheap field
    # that tells two stands on this machine apart.
    assert "@SomeDependency" in r.error
    # The offer itself, and its limits: stopping is the caller's act (the tool
    # never auto-stops a stranger), and the alternative is still named.
    assert "taskkill" in r.hint
    assert "4242" in r.hint
    assert "machine.port" in r.hint

    # A holder this session DID start is a different situation with a different
    # answer: it is ours, and server_stop is the way out.
    session.set_server_pid(4242, "DayZDiag_x64.exe")
    mine = tools.server_start(timeout=5)
    assert not mine.ok
    assert "server_stop(pid=4242)" in mine.hint


def test_the_port_refusal_degrades_to_pid_only_when_the_command_line_is_unreadable(tmp_path, monkeypatch):
    """Identification is best-effort: a pid that died between netstat and the
    lookup, or an access-denied process, yields no -mod= tail. The offer stands
    -- on the pid alone -- and nothing invents a mod list."""
    root = make_project(tmp_path)
    stand, game = tmp_path / "stand", tmp_path / "game"
    with_stand_and_game(root, stand, game)
    (stand / "serverDZ.cfg").write_text("", encoding="utf-8")
    tools.project_open(str(root))

    monkeypatch.setattr("dayz_mcp.tools.lifecycle.udp_port_holders", lambda port: [4242])
    monkeypatch.setattr("dayz_mcp.tools.lifecycle.process_mods_tail", lambda pid: "")

    r = tools.server_start(timeout=5)
    assert not r.ok
    assert "4242" in r.error
    assert "mods: unknown" in r.error
    assert "taskkill" in r.hint


def test_process_mods_tail_extracts_basenames_from_a_real_command_line(monkeypatch):
    """Parsed against the shape of a real stand's command line (quoted exe,
    -mod= with absolute paths). Basenames only: the full paths are noise, the
    @Name segments are what a human recognises a stand by."""
    line = (
        '"C:\\game\\DayZDiag_x64.exe" -server -config=C:\\stand\\serverDZ.cfg -port=2302 '
        "-mod=C:\\ws\\@CF;C:\\ws\\@Dep;E:\\proj\\build\\@MyMod "
        "-profiles=C:\\stand\\profiles"
    )

    class Done:
        returncode = 0
        stdout = line + "\n"

    monkeypatch.setattr("dayz_mcp.procs.subprocess.run", lambda *a, **kw: Done())
    monkeypatch.setattr("dayz_mcp.procs.os.name", "nt")
    assert procs_process_mods_tail(4242) == "@CF;@Dep;@MyMod"


def test_process_mods_tail_returns_empty_on_any_failure(monkeypatch):
    """Evidence when present, silence when not -- never a guess and never an
    exception on the refusal path that uses it."""
    monkeypatch.setattr("dayz_mcp.procs.os.name", "nt")

    class NoLine:
        returncode = 0
        stdout = ""

    monkeypatch.setattr("dayz_mcp.procs.subprocess.run", lambda *a, **kw: NoLine())
    assert procs_process_mods_tail(4242) == ""

    class NoMods:
        returncode = 0
        stdout = "C:\\game\\DayZDiag_x64.exe -server -port=2302\n"

    monkeypatch.setattr("dayz_mcp.procs.subprocess.run", lambda *a, **kw: NoMods())
    assert procs_process_mods_tail(4242) == ""

    def boom(*a, **kw):
        raise OSError("powershell missing")

    monkeypatch.setattr("dayz_mcp.procs.subprocess.run", boom)
    assert procs_process_mods_tail(4242) == ""


def test_the_port_and_the_mission_module_are_the_readiness_signal(tmp_path, monkeypatch):
    """A project with no ready line used to get a three-second dwell and an
    honest "cannot be determined". Two engine signals answer for that case --
    both the server's own doing, needing neither a mod nor a declared line.

    THE PORT ALONE USED TO BE ENOUGH, AND THAT WAS WRONG. Measured on this
    machine: the port binds about 17 s after spawn, the mission module compiles
    about 25 s after it. Between the two the server is listening with no
    mission scripts -- it answers queries and refuses every player -- and a
    verdict taken there reads a log with no errors and says "pass".
    """
    root = make_project(tmp_path, PROFILE_WITHOUT_READY_LINE)
    stand, game = tmp_path / "stand", tmp_path / "game"
    with_stand_and_game(root, stand, game)
    (stand / "serverDZ.cfg").write_text("", encoding="utf-8")
    tools.project_open(str(root))

    holders = {"pids": []}
    profiles = stand / "profiles"

    def fake_spawn(cmd, cwd):
        (profiles / "script_test.log").write_text(
            "SCRIPT: Module: Mission; loaded 216x files; 450x classes;" + chr(10),
            encoding="utf-8",
        )
        return 4321

    monkeypatch.setattr("dayz_mcp.tools.lifecycle.udp_port_holders",
                        lambda port: holders["pids"])
    monkeypatch.setattr("dayz_mcp.tools.lifecycle.spawn", fake_spawn)
    monkeypatch.setattr("dayz_mcp.tools.lifecycle.is_alive", lambda pid, image="": True)
    monkeypatch.setattr("dayz_mcp.tools.lifecycle.NO_READY_LINE_SETTLE_SECONDS", 0.05)
    monkeypatch.setattr("dayz_mcp.tools.lifecycle.PORT_READY_WAIT_SECONDS", 0.3)
    monkeypatch.setattr("dayz_mcp.tools.lifecycle.BOOT_POLL_SECONDS", 0.02)

    started = tools.server_start(timeout=30)
    assert started.ok, started.error
    holders["pids"] = [4321]  # the server binds, a moment later

    waited = tools.job_wait(started.data["job_id"], timeout=20)
    assert waited.data["status"] == "done", waited.data
    assert "bound AND the mission module compiled" in waited.data["summary"]
    # And it must still not overclaim: two engine signals say the engine and its
    # mission are up, not that any particular mod finished loading.
    assert "NOT that any particular mod finished loading" in waited.data["summary"]


def test_a_declared_ready_line_stays_the_readiness_verdict(tmp_path, monkeypatch):
    """The two signals answer different questions, so they are not alternatives
    for the same verdict. With a ready line declared it remains THE answer --
    the port cannot say a mod finished loading -- and the port is reported
    beside it."""
    root = make_project(tmp_path)
    stand, game = tmp_path / "stand", tmp_path / "game"
    with_stand_and_game(root, stand, game)
    (stand / "serverDZ.cfg").write_text("", encoding="utf-8")
    tools.project_open(str(root))

    # Free before the spawn (or the pre-flight refuses), held after it -- which
    # is also the real sequence.
    holders = {"pids": []}

    def spawn_and_write_the_line(cmd, cwd):
        profiles = Path(next(a for a in cmd if a.startswith("-profiles=")).split("=", 1)[1])
        (profiles / "script_now.log").write_text("[MyMod] loaded\n", encoding="utf-8")
        holders["pids"] = [4321]
        return 4321

    monkeypatch.setattr("dayz_mcp.tools.lifecycle.spawn", spawn_and_write_the_line)
    monkeypatch.setattr("dayz_mcp.tools.lifecycle.is_alive", lambda pid, image="": True)
    monkeypatch.setattr("dayz_mcp.tools.lifecycle.udp_port_holders", lambda port: holders["pids"])

    started = tools.server_start(timeout=20)
    waited = tools.job_wait(started.data["job_id"], timeout=20)
    assert waited.data["status"] == "done", waited.data
    assert "ready via expect.ready_line" in waited.data["summary"]
    assert "udp/2302 bound" in waited.data["summary"]


def test_a_missing_ready_line_over_a_listening_server_says_which_half_failed(tmp_path, monkeypatch):
    """The failure worth telling apart: the server is up and listening, and it
    is the MOD's line that never appeared. "no ready line within Ns" alone sends
    the reader to look at the boot, which is fine."""
    root = make_project(tmp_path)
    stand, game = tmp_path / "stand", tmp_path / "game"
    with_stand_and_game(root, stand, game)
    (stand / "serverDZ.cfg").write_text("", encoding="utf-8")
    tools.project_open(str(root))

    holders = {"pids": []}

    def spawn_that_binds(cmd, cwd):
        holders["pids"] = [4321]
        return 4321

    monkeypatch.setattr("dayz_mcp.tools.lifecycle.spawn", spawn_that_binds)
    monkeypatch.setattr("dayz_mcp.tools.lifecycle.is_alive", lambda pid, image="": True)
    monkeypatch.setattr("dayz_mcp.tools.lifecycle.udp_port_holders", lambda port: holders["pids"])
    monkeypatch.setattr("dayz_mcp.tools.lifecycle.BOOT_POLL_SECONDS", 0.02)

    started = tools.server_start(timeout=0.2)
    waited = tools.job_wait(started.data["job_id"], timeout=20)
    assert waited.data["status"] == "failed"
    assert "holds udp/2302" in waited.data["error"]
    assert "ready line that never appeared" in waited.data["error"]


def test_a_dead_server_is_still_reported_dead(tmp_path, monkeypatch):
    """The premise that started this work claimed a boot which had really
    succeeded was being called a failure. It was not: that server genuinely
    died, and this must keep saying so."""
    root = make_project(tmp_path)
    stand, game = tmp_path / "stand", tmp_path / "game"
    with_stand_and_game(root, stand, game)
    (stand / "serverDZ.cfg").write_text("", encoding="utf-8")
    tools.project_open(str(root))

    monkeypatch.setattr("dayz_mcp.tools.lifecycle.spawn", lambda cmd, cwd: 4321)
    monkeypatch.setattr("dayz_mcp.tools.lifecycle.is_alive", lambda pid, image="": False)
    monkeypatch.setattr("dayz_mcp.tools.lifecycle.udp_port_holders", lambda port: [])

    started = tools.server_start(timeout=5)
    waited = tools.job_wait(started.data["job_id"], timeout=20)
    assert waited.data["status"] == "failed"
    assert "died" in waited.data["error"]


def test_udp_port_holders_parses_netstat_and_matches_the_whole_port(tmp_path, monkeypatch):
    """Captured from the real thing on this machine. The suffix match must be
    on ":2302" and not on the digits appearing anywhere -- ":12302" is a
    different port on the same machine."""
    captured = textwrap.dedent(
        """
        Active Connections

          Proto  Local Address          Foreign Address        State           PID
          UDP    0.0.0.0:2302           *:*                                    67688
          UDP    0.0.0.0:12302          *:*                                    999
          UDP    127.0.0.1:2302         *:*                                    67688
          TCP    0.0.0.0:2302           0.0.0.0:0              LISTENING       555
        """
    ).strip()

    class Done:
        stdout = captured

    monkeypatch.setattr("dayz_mcp.procs.subprocess.run", lambda *a, **kw: Done())
    monkeypatch.setattr("dayz_mcp.procs.os.name", "nt")
    assert procs_udp_port_holders(2302) == [67688]
    assert procs_udp_port_holders(12302) == [999]
    assert procs_udp_port_holders(9999) == []


# --- Extra launch arguments: an explicit one-run opt-in, like the bridge attach


def _extra_args_project(tmp_path, monkeypatch):
    """A boot that reaches "ready via expect.ready_line" -- the port must be
    FREE before spawn (the preflight runs first; my first version handed it a
    held port and tested the preflight instead) and the marker must appear, so
    the job lands on a summary the extras note can be read from."""
    session.reset()
    root = make_project(tmp_path)
    stand, game = tmp_path / "stand", tmp_path / "game"
    with_stand_and_game(root, stand, game)
    (stand / "serverDZ.cfg").write_text("", encoding="utf-8")
    tools.project_open(str(root))
    captured = {}

    def fake_spawn(cmd, cwd):
        captured["cmd"] = cmd
        profiles = Path(next(a for a in cmd if a.startswith("-profiles=")).split("=", 1)[1])
        (profiles / "script_now.log").write_text("[MyMod] loaded\n", encoding="utf-8")
        return 123

    monkeypatch.setattr("dayz_mcp.tools.lifecycle.spawn", fake_spawn)
    monkeypatch.setattr("dayz_mcp.tools.lifecycle.is_alive", lambda pid, image="": True)
    return captured


def test_server_start_appends_extra_args_after_the_fixed_ones(tmp_path, monkeypatch):
    """The client-session runbook boots with -doScriptLogs=1 -logToFile=1 (the
    engine's action log is gated by launch flags). Its only route used to be a
    by-hand boot, which loses the preflight, the transport clearing and the
    port-readiness work -- so the flags come through THIS tool, per call."""
    captured = _extra_args_project(tmp_path, monkeypatch)

    started = tools.server_start(timeout=10, extra_args=["-doScriptLogs=1", "-logToFile=1"])
    assert started.ok, started.error
    waited = tools.job_wait(started.data["job_id"], timeout=20)

    # Appended AFTER everything the tool owns, so an extra can never displace
    # or precede a fixed argument.
    assert captured["cmd"][-2:] == ["-doScriptLogs=1", "-logToFile=1"]
    fixed = [a for a in captured["cmd"] if a.split("=", 1)[0] in
             ("-config", "-port", "-mod", "-profiles", "-serverMod")]
    assert all(captured["cmd"].index(f) < captured["cmd"].index("-doScriptLogs=1") for f in fixed)
    # A later reader must be able to see this boot was non-standard.
    assert "-doScriptLogs=1 -logToFile=1" in waited.data["summary"]


def test_server_start_without_extras_stays_exactly_as_before(tmp_path, monkeypatch):
    captured = _extra_args_project(tmp_path, monkeypatch)

    started = tools.server_start(timeout=10)
    waited = tools.job_wait(started.data["job_id"], timeout=20)
    assert captured["cmd"][-1].startswith(("-profiles=", "-serverMod="))
    assert "extra args" not in waited.data["summary"]


def test_server_start_refuses_extras_that_collide_with_owned_arguments(tmp_path, monkeypatch):
    """-config, -profiles, -port, -mod and -serverMod are the tool's own: the
    preflight, the log discipline and the mod split all assume they are what
    the tool computed. An extra overriding one would silently invalidate every
    guarantee built on them -- and the engine takes the LAST occurrence."""
    captured = _extra_args_project(tmp_path, monkeypatch)

    for arg in ("-config=C:/other.cfg", "-profiles=C:/elsewhere", "-port=9999",
                "-mod=@Dep", "-serverMod=@Dep", "-PORT=9999", "-port"):
        r = tools.server_start(timeout=3, extra_args=[arg])
        assert not r.ok, f"{arg} was accepted"
        assert arg.split("=", 1)[0].lower().lstrip("-") in r.error.lower(), arg
        assert "profile" in r.hint, arg
        assert "cmd" not in captured, f"{arg}: it spawned anyway"


def test_server_start_refuses_a_single_string_rather_than_resplitting_it(tmp_path, monkeypatch):
    """A string would have to be re-split, and quoting rules are exactly the
    kind of thing two halves disagree about. A list of strings or nothing."""
    captured = _extra_args_project(tmp_path, monkeypatch)

    r = tools.server_start(timeout=3, extra_args="-doScriptLogs=1 -logToFile=1")
    assert not r.ok
    assert "list of strings" in r.error
    assert "separately" in r.hint
    assert "cmd" not in captured

    also = tools.server_start(timeout=3, extra_args=[1, "-x"])
    assert not also.ok
    assert "cmd" not in captured


# --- The window between "server_start returned" and "the session knows the pid" ---
#
# Measured, not theorised: three live runs called world_ready straight after
# server_start and were told "no server started by this session is running"
# while the server was in fact coming up. The pid was set inside the worker
# thread, so every tool that asks the session for it lost that race.


def test_server_start_knows_the_pid_before_it_returns(tmp_path, monkeypatch):
    """The spawn happens in the CALLER's thread now, so there is no window in
    which a started server is invisible to the next call."""
    root = make_project(tmp_path)
    stand, game = tmp_path / "stand", tmp_path / "game"
    with_stand_and_game(root, stand, game)
    (stand / "serverDZ.cfg").write_text("", encoding="utf-8")
    tools.project_open(str(root))

    monkeypatch.setattr("dayz_mcp.tools.lifecycle.spawn", lambda cmd, cwd: 4242)
    monkeypatch.setattr("dayz_mcp.tools.lifecycle.is_alive", lambda pid, image="": True)

    # The readiness worker is prevented from running AT ALL. Without this the
    # test passes either way: the thread wins the race in-process and sets the
    # pid before the assertion is reached, which is exactly the shape of a test
    # that passes with its own mechanism removed.
    class NeverStarts:
        def __init__(self, *a, **kw):
            pass

        def start(self):
            pass

    monkeypatch.setattr("dayz_mcp.tools.lifecycle.threading.Thread", NeverStarts)

    started = tools.server_start(timeout=1)
    assert started.ok, started.error
    assert started.data["pid"] == 4242
    assert session.server_pid() == 4242, "the pid was set by the worker, not by the call"


def test_a_spawn_that_fails_is_answered_by_the_call_itself(tmp_path, monkeypatch):
    """An image that cannot be launched is not a boot outcome, it is a refusal:
    the caller learns at once instead of after a round trip through job_wait.
    The job is still recorded as failed, so nothing is left looking alive."""
    root = make_project(tmp_path)
    stand, game = tmp_path / "stand", tmp_path / "game"
    with_stand_and_game(root, stand, game)
    (stand / "serverDZ.cfg").write_text("", encoding="utf-8")
    tools.project_open(str(root))

    def boom(cmd, cwd):
        raise OSError("not a valid Win32 application")

    monkeypatch.setattr("dayz_mcp.tools.lifecycle.spawn", boom)

    started = tools.server_start(timeout=5)
    assert not started.ok
    assert "OSError" in started.error or "Win32" in started.error
    assert session.server_pid() in (0, None)
    job_id = started.data["job_id"]
    assert tools.job_status(job_id).data["status"] == "failed"


# --- A boot the engine reports as fine while nobody can connect ---
#
# Found by another session using this server, not by these tests. The stand is
# launched with the DIAGNOSTIC EXE OUT OF THE CLIENT INSTALL, and the engine
# resolves `mpmissions` next to the executable it is running -- so a machine
# whose missions live in the separate DayZServer install starts a server that
# binds its port, logs no error, passes the verdict, and refuses every player
# with one line: "Mission script has no main function, player connect will stay
# disabled!". This machine only worked by accident: it happens to have a copy
# under the client install too.


def test_a_missing_mission_is_refused_before_the_server_is_started(tmp_path, monkeypatch):
    root = make_project(tmp_path)
    stand, game = tmp_path / "stand", tmp_path / "game"
    with_stand_and_game(root, stand, game)
    (stand / "serverDZ.cfg").write_text(
        'class Missions\n{\n    class DayZ\n    {\n'
        '        template="dayzOffline.chernarusplus";\n    };\n};\n',
        encoding="utf-8",
    )
    tools.project_open(str(root))

    spawned = []
    monkeypatch.setattr("dayz_mcp.tools.lifecycle.spawn",
                        lambda cmd, cwd: spawned.append(cmd) or 111)

    started = tools.server_start(timeout=5)
    assert not started.ok
    assert "dayzOffline.chernarusplus" in started.error
    assert spawned == [], "the server must not be started at all"
    # The remedy has to name where the engine looked, or the reader has no idea
    # which of two DayZ installations is missing the folder.
    assert "mpmissions" in started.hint


def test_a_mission_that_is_there_is_not_refused(tmp_path, monkeypatch):
    root = make_project(tmp_path)
    stand, game = tmp_path / "stand", tmp_path / "game"
    with_stand_and_game(root, stand, game)
    (stand / "serverDZ.cfg").write_text(
        'class Missions\n{\n    class DayZ\n    {\n'
        '        template="dayzOffline.chernarusplus";\n    };\n};\n',
        encoding="utf-8",
    )
    (game / "mpmissions" / "dayzOffline.chernarusplus").mkdir(parents=True)
    tools.project_open(str(root))

    monkeypatch.setattr("dayz_mcp.tools.lifecycle.spawn", lambda cmd, cwd: 111)
    monkeypatch.setattr("dayz_mcp.tools.lifecycle.is_alive", lambda pid, image="": True)

    started = tools.server_start(timeout=1)
    assert started.ok, started.error


def test_a_config_that_names_no_mission_is_not_second_guessed(tmp_path, monkeypatch):
    """No template means nothing to check. Refusing on a config this tool
    cannot read would block boots that work today, which is a worse failure
    than the one being fixed."""
    root = make_project(tmp_path)
    stand, game = tmp_path / "stand", tmp_path / "game"
    with_stand_and_game(root, stand, game)
    (stand / "serverDZ.cfg").write_text("hostname = \"whatever\";\n", encoding="utf-8")
    tools.project_open(str(root))

    monkeypatch.setattr("dayz_mcp.tools.lifecycle.spawn", lambda cmd, cwd: 111)
    monkeypatch.setattr("dayz_mcp.tools.lifecycle.is_alive", lambda pid, image="": True)

    started = tools.server_start(timeout=1)
    assert started.ok, started.error


# --- "Ready" that arrives before the scripts do ---
#
# Measured on this machine: the port binds about 17 s after spawn and the
# mission module compiles about 25 s after it. A boot judged ready at the port
# bind has not compiled a single line of the mod -- and log_verdict, looking at
# that same moment, sees a log with no errors and says "pass". I was fooled by
# this myself: four bisect runs in a row reported "the mission module never
# compiled" when the truth was that the server had been stopped before it got
# there.


def _mission_line() -> str:
    return "SCRIPT       : Module: Mission; loaded 216x files; 450x classes;\n"


def _bootable(tmp_path, monkeypatch, log_text: str, port_bound: bool = True):
    """A project with no ready line, whose fake server writes `log_text`."""
    session.reset()
    root = make_project(tmp_path, PROFILE_WITHOUT_READY_LINE)
    stand, game = tmp_path / "stand", tmp_path / "game"
    with_stand_and_game(root, stand, game)
    (stand / "serverDZ.cfg").write_text("", encoding="utf-8")
    tools.project_open(str(root))

    profiles = stand / "profiles"

    running = []

    def fake_spawn(cmd, cwd):
        # Written from inside the spawn so its mtime is newer than the job's
        # `since`, exactly as a real server's log would be.
        (profiles / "script_test.log").write_text(log_text, encoding="utf-8")
        running.append(4321)
        return 4321

    monkeypatch.setattr("dayz_mcp.tools.lifecycle.spawn", fake_spawn)
    monkeypatch.setattr("dayz_mcp.tools.lifecycle.is_alive", lambda pid, image="": True)
    monkeypatch.setattr("dayz_mcp.tools.lifecycle.NO_READY_LINE_SETTLE_SECONDS", 0.05)
    monkeypatch.setattr("dayz_mcp.tools.lifecycle.PORT_READY_WAIT_SECONDS", 0.3)
    monkeypatch.setattr("dayz_mcp.tools.lifecycle.BOOT_POLL_SECONDS", 0.02)
    # What these tests are about is which branch the worker takes, never how
    # long it waited: at the real 2 s poll step against a 6 s ceiling each of
    # them spent 6.1 s of the suite proving nothing about time.
    monkeypatch.setattr("dayz_mcp.tools.lifecycle.PORT_READY_WAIT_SECONDS", 0.3)
    monkeypatch.setattr("dayz_mcp.tools.lifecycle.BOOT_POLL_SECONDS", 0.02)
    # Empty until the process exists: the same function answers the pre-flight
    # "is this port already held by somebody else" check, and a port that looks
    # held before the spawn refuses the boot outright.
    monkeypatch.setattr(
        "dayz_mcp.tools.lifecycle.udp_port_holders",
        lambda port: (list(running) if port_bound else []),
    )
    return tools.server_start(timeout=300)


def test_a_bound_port_alone_is_not_ready_when_the_mission_never_compiled(tmp_path, monkeypatch):
    started = _bootable(tmp_path, monkeypatch, "SCRIPT: Module: World; loaded 2156x files;\n")
    assert started.ok, started.error
    waited = tools.job_wait(started.data["job_id"], timeout=30)

    assert waited.data["status"] == "failed", waited.data
    assert "mission" in waited.data["error"].lower()
    # The distinction that makes the answer actionable: the engine is up, the
    # mod is not.
    assert "listening" in waited.data["error"] or "bound" in waited.data["error"]


def test_the_boot_is_ready_when_the_port_is_bound_and_the_mission_compiled(tmp_path, monkeypatch):
    started = _bootable(
        tmp_path, monkeypatch,
        "SCRIPT: Module: World; loaded 2156x files;\n" + _mission_line(),
    )
    assert started.ok, started.error
    waited = tools.job_wait(started.data["job_id"], timeout=30)

    assert waited.data["status"] == "done", waited.data
    summary = waited.data["summary"]
    assert "mission" in summary.lower()
    assert "4321" in summary


def test_a_server_that_binds_nothing_still_answers_as_it_did(tmp_path, monkeypatch):
    """Unchanged on purpose: a stand that does not bind this port is unusual,
    not proof of anything, and this configuration could not judge readiness at
    all before the port signal existed."""
    started = _bootable(tmp_path, monkeypatch, "", port_bound=False)
    waited = tools.job_wait(started.data["job_id"], timeout=30)
    assert waited.data["status"] == "done", waited.data
    assert "readiness cannot be detected" in waited.data["summary"]


def test_a_previous_boots_log_does_not_answer_for_this_one(tmp_path, monkeypatch):
    """The stale-evidence failure, one level down from the one log_verdict's
    `since` already guards: a log left by an earlier run carries the mission
    module line for ever, and reading it would make every later boot look ready
    the instant its port bound -- which is the exact defect this signal was
    added to close."""
    root = make_project(tmp_path, PROFILE_WITHOUT_READY_LINE)
    stand, game = tmp_path / "stand", tmp_path / "game"
    with_stand_and_game(root, stand, game)
    (stand / "serverDZ.cfg").write_text("", encoding="utf-8")
    tools.project_open(str(root))

    profiles = stand / "profiles"
    stale = profiles / "script_old.log"
    stale.write_text("SCRIPT: Module: Mission; loaded 216x files;" + chr(10), encoding="utf-8")
    old = time.time() - 3600
    os.utime(stale, (old, old))

    running = []
    monkeypatch.setattr("dayz_mcp.tools.lifecycle.spawn",
                        lambda cmd, cwd: (running.append(4321), 4321)[1])
    monkeypatch.setattr("dayz_mcp.tools.lifecycle.is_alive", lambda pid, image="": True)
    monkeypatch.setattr("dayz_mcp.tools.lifecycle.NO_READY_LINE_SETTLE_SECONDS", 0.05)
    monkeypatch.setattr("dayz_mcp.tools.lifecycle.PORT_READY_WAIT_SECONDS", 0.3)
    monkeypatch.setattr("dayz_mcp.tools.lifecycle.BOOT_POLL_SECONDS", 0.02)
    monkeypatch.setattr("dayz_mcp.tools.lifecycle.udp_port_holders", lambda port: list(running))

    started = tools.server_start(timeout=300)
    waited = tools.job_wait(started.data["job_id"], timeout=30)

    assert waited.data["status"] == "failed", waited.data
    assert "mission module never compiled" in waited.data["error"]
