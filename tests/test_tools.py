import os
import sys
import textwrap
import threading
import time
from importlib.metadata import version as metadata_version
from pathlib import Path

import pytest

from dayz_mcp import DIST_NAME
from dayz_mcp import __version__ as dayz_mcp_version
from dayz_mcp import server as mcp_server
from dayz_mcp import tools
from dayz_mcp.errors import ok as errors_ok
from dayz_mcp.packer import PackResult
from dayz_mcp.procs import is_alive as procs_is_alive
from dayz_mcp.procs import spawn as procs_spawn
from dayz_mcp.procs import stop as procs_stop
from dayz_mcp.procs import process_mods_tail as procs_process_mods_tail
from dayz_mcp.procs import udp_port_holders as procs_udp_port_holders
from dayz_mcp.profile import load_profile
from dayz_mcp.tools import jobs_api, lifecycle, session

PROFILE = """
[project]
name = "my-mod"

[build]
mods = ["MyMod"]

[expect]
ready_line = "[MyMod] loaded"
forbid = ["Bad type"]

[expect.counters]
items = 12
"""


@pytest.fixture(autouse=True)
def _no_real_ports(monkeypatch):
    """Nothing in this file may consult the machine's actual network state.

    server_start now checks the game port before spawning, and that check reads
    netstat. Without this, twelve tests started failing the moment ANOTHER
    AGENT's live stand bound udp/2302 on this machine -- tests that had passed
    minutes earlier, for a reason nothing in them could express. A unit test
    that reads global machine state is flaky by construction, and this is a
    repository where a second stand really does come and go.

    The default is "nothing holds any port"; the tests that are about the port
    override it explicitly, which also makes them the only place the reader has
    to look for that behaviour.
    """
    monkeypatch.setattr("dayz_mcp.tools.lifecycle.udp_port_holders", lambda port: [])


PROFILE_WITHOUT_READY_LINE = """
[project]
name = "my-mod"

[build]
mods = ["MyMod"]

[expect]
forbid = ["Bad type"]
"""


def make_project(tmp_path: Path, profile_text: str = PROFILE) -> Path:
    (tmp_path / "dayz-mcp.toml").write_text(textwrap.dedent(profile_text), encoding="utf-8")
    (tmp_path / "MyMod").mkdir()
    (tmp_path / "MyMod" / "config.cpp").write_text("", encoding="utf-8")
    return tmp_path


def with_stand(root: Path, stand: Path, log_text: str) -> None:
    (stand / "profiles").mkdir(parents=True, exist_ok=True)
    (stand / "profiles" / "script_1.log").write_text(log_text, encoding="utf-8")
    (root / "dayz-mcp.local.toml").write_text(
        f'[machine]\nstand_root = "{stand.as_posix()}"\n', encoding="utf-8"
    )


def with_stand_and_game(
    root: Path,
    stand: Path,
    game_dir: Path,
    *,
    port: int | None = None,
    extra_mods: list[str] | None = None,
    server_only: list[str] | None = None,
    config: str | None = None,
) -> None:
    """Like with_stand, but also fabricates a fake game install so server_start's
    `find_game` succeeds deterministically, regardless of what is actually
    installed on the machine running the tests."""
    (stand / "profiles").mkdir(parents=True, exist_ok=True)
    game_dir.mkdir(parents=True, exist_ok=True)
    (game_dir / "DayZDiag_x64.exe").write_bytes(b"")

    lines = ["[machine]", f'stand_root = "{stand.as_posix()}"', f'game = "{game_dir.as_posix()}"']
    if port is not None:
        lines.append(f"port = {port}")
    if config is not None:
        lines.append(f'config = "{config}"')
    if extra_mods or server_only:
        lines.append("")
        lines.append("[mods]")
        if extra_mods:
            items = ", ".join(f'"{x}"' for x in extra_mods)
            lines.append(f"extra = [{items}]")
        if server_only:
            items = ", ".join(f'"{x}"' for x in server_only)
            lines.append(f"server_only = [{items}]")
    (root / "dayz-mcp.local.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_project_open_reports_what_it_found(tmp_path):
    session.reset()
    r = tools.project_open(str(make_project(tmp_path)))
    assert r.ok, r.error
    assert r.data["name"] == "my-mod"
    assert r.data["own_mod_dirs"] == ["@MyMod"]


def test_tools_refuse_to_work_without_a_project():
    session.reset()
    r = tools.mod_build()
    assert not r.ok
    assert "project_open" in r.hint


def test_build_runs_as_a_job_and_reports_packing_results(tmp_path, monkeypatch):
    session.reset()
    root = make_project(tmp_path)
    tools.project_open(str(root))
    monkeypatch.setattr(
        "dayz_mcp.tools.build.pack_all",
        lambda names, root, tools_root, log_dir, exclude=None, sources=None, stage=False, manifest_dir=None: [
            PackResult(name="MyMod", pbo=str(root / "@MyMod/addons/MyMod.pbo"), size=10, signed=True)
        ],
    )
    monkeypatch.setattr("dayz_mcp.tools.build.session_tools_root", lambda: "C:/tools")
    job_id = tools.mod_build().data["job_id"]
    waited = tools.job_wait(job_id, timeout=30)
    assert waited.data["status"] == "done"
    assert "MyMod" in waited.data["summary"]


def test_build_fails_the_job_when_packing_reports_an_error(tmp_path, monkeypatch):
    session.reset()
    root = make_project(tmp_path)
    tools.project_open(str(root))
    monkeypatch.setattr(
        "dayz_mcp.tools.build.pack_all",
        lambda names, root, tools_root, log_dir, exclude=None, sources=None, stage=False, manifest_dir=None: [PackResult(name="MyMod", error="stale pbo")],
    )
    monkeypatch.setattr("dayz_mcp.tools.build.session_tools_root", lambda: "C:/tools")
    job_id = tools.mod_build().data["job_id"]
    waited = tools.job_wait(job_id, timeout=30)
    assert waited.data["status"] == "failed"
    assert "stale" in waited.data["error"]


def test_log_verdict_reads_the_newest_log_and_decides(tmp_path):
    session.reset()
    root = make_project(tmp_path)
    with_stand(root, tmp_path / "stand", "SCRIPT : [MyMod] loaded: items=12\n")
    tools.project_open(str(root))
    r = tools.log_verdict()
    assert r.ok, r.error
    assert r.data["verdict"] == "pass"


def test_log_verdict_fails_when_a_counter_is_short(tmp_path):
    session.reset()
    root = make_project(tmp_path)
    with_stand(root, tmp_path / "stand", "SCRIPT : [MyMod] loaded: items=1\n")
    tools.project_open(str(root))
    r = tools.log_verdict()
    assert r.data["verdict"] == "fail"
    assert any("items" in reason for reason in r.data["reasons"])


def test_server_log_lookup_goes_through_the_one_profiles_dir_owner(tmp_path, monkeypatch):
    """The server's -profiles directory has exactly one definition
    (lifecycle.server_profiles_dir). logs.py held a character-for-character copy
    of its formula, which is the same "two owners for one path" arrangement that
    already broke both client-side log tools once -- so this asserts the copy is
    gone by moving the owner and watching the log tools follow."""
    session.reset()
    root = make_project(tmp_path)
    with_stand(root, tmp_path / "stand", "the stand's own log\n")
    tools.project_open(str(root))

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "script_9.log").write_text("moved with the owner\n", encoding="utf-8")
    monkeypatch.setattr("dayz_mcp.tools.logs.server_profiles_dir", lambda: elsewhere)

    r = tools.log_tail()
    assert r.ok, r.error
    assert r.data["lines"] == ["moved with the owner"]


def test_log_tail_filters(tmp_path):
    session.reset()
    root = make_project(tmp_path)
    with_stand(root, tmp_path / "stand", "one\ntwo needle\nthree\n")
    tools.project_open(str(root))
    r = tools.log_tail(pattern="needle")
    assert r.data["lines"] == ["two needle"]


# --- Extra requirement 1: the verdict must be tied to the run it judges (`since`) ---


def test_log_verdict_refuses_a_log_older_than_since(tmp_path):
    session.reset()
    root = make_project(tmp_path)
    with_stand(root, tmp_path / "stand", "SCRIPT : [MyMod] loaded: items=12\n")
    tools.project_open(str(root))
    log = tmp_path / "stand" / "profiles" / "script_1.log"
    since = log.stat().st_mtime + 1000  # a "run" that supposedly started after this log was written
    r = tools.log_verdict(since=since)
    assert not r.ok
    assert "predates" in r.error
    assert "wait" in r.hint


def test_log_verdict_accepts_a_log_at_or_after_since(tmp_path):
    session.reset()
    root = make_project(tmp_path)
    with_stand(root, tmp_path / "stand", "SCRIPT : [MyMod] loaded: items=12\n")
    tools.project_open(str(root))
    log = tmp_path / "stand" / "profiles" / "script_1.log"
    since = log.stat().st_mtime - 1000  # the run started well before the log was last written
    r = tools.log_verdict(since=since)
    assert r.ok, r.error
    assert r.data["verdict"] == "pass"


def test_server_start_returns_since_matching_the_job_it_created(tmp_path, monkeypatch):
    session.reset()
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
    session.reset()
    root = make_project(tmp_path, PROFILE_WITHOUT_READY_LINE)
    stand, game = tmp_path / "stand", tmp_path / "game"
    with_stand_and_game(root, stand, game)
    (stand / "serverDZ.cfg").write_text("", encoding="utf-8")
    opened = tools.project_open(str(root))
    assert any("readiness cannot be detected" in n for n in opened.data["notes"])

    monkeypatch.setattr("dayz_mcp.tools.lifecycle.spawn", lambda cmd, cwd: 4321)
    monkeypatch.setattr("dayz_mcp.tools.lifecycle.is_alive", lambda pid, image="": True)
    monkeypatch.setattr("dayz_mcp.tools.lifecycle.NO_READY_LINE_SETTLE_SECONDS", 0.05)
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
    session.reset()
    root = make_project(tmp_path, PROFILE_WITHOUT_READY_LINE)
    stand, game = tmp_path / "stand", tmp_path / "game"
    with_stand_and_game(root, stand, game)
    (stand / "serverDZ.cfg").write_text("", encoding="utf-8")
    tools.project_open(str(root))

    monkeypatch.setattr("dayz_mcp.tools.lifecycle.spawn", lambda cmd, cwd: 4321)
    monkeypatch.setattr("dayz_mcp.tools.lifecycle.is_alive", lambda pid, image="": False)
    monkeypatch.setattr("dayz_mcp.tools.lifecycle.NO_READY_LINE_SETTLE_SECONDS", 0.05)

    job_id = tools.server_start(timeout=300).data["job_id"]
    waited = tools.job_wait(job_id, timeout=20)

    assert waited.data["status"] == "failed"
    assert "died" in waited.data["error"]


# --- Extra requirement 2: server_start must not delete old logs ---


def test_server_start_does_not_delete_pre_existing_logs(tmp_path, monkeypatch):
    session.reset()
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
    session.reset()
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

    job_id = tools.server_start(timeout=3).data["job_id"]
    waited = tools.job_wait(job_id, timeout=8)
    assert waited.data["status"] == "failed"
    assert "ready line" in waited.data["error"]


# --- Extra requirement 3: refuse a second server_start while one is already running ---


def test_server_start_refuses_when_already_running(tmp_path, monkeypatch):
    session.reset()
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
    session.reset()
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


# --- Extra requirement 5: server_status ---


def test_server_status_with_no_log_yet(tmp_path):
    session.reset()
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
    session.reset()
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
    session.reset()
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


def test_is_within_detects_paths_outside_the_base():
    assert lifecycle._is_within(Path("C:/stand/serverDZ.cfg"), Path("C:/stand"))
    assert lifecycle._is_within(Path("C:/stand"), Path("C:/stand"))
    assert not lifecycle._is_within(Path("C:/other/serverDZ.cfg"), Path("C:/stand"))


def test_server_start_passes_an_absolute_config_path(tmp_path, monkeypatch):
    session.reset()
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
    session.reset()
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
    session.reset()
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
    session.reset()
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


# --- Final review, item 2: log_verdict(source="client") and log_tail(source=
# "client") looked under machine.stand_root, a directory nothing ever creates.
# The client's logs live with the job that produced them. ---


def _run_fake_client_compile(monkeypatch, log_text: str, rpt_text: str = "clean\n") -> str:
    """Run client_compile_check with a stand-in for the diagnostic client that
    writes its logs where the real one does: the -profiles directory the tool
    hands to the executable. Returns the job id."""

    def fake_spawn(cmd, cwd):
        profiles = Path(next(a for a in cmd if a.startswith("-profiles=")).split("=", 1)[1])
        (profiles / "script_1.log").write_text(log_text, encoding="utf-8")
        (profiles / "crash.RPT").write_text(rpt_text, encoding="utf-8")
        return 4242

    monkeypatch.setattr("dayz_mcp.tools.lifecycle.spawn", fake_spawn)
    monkeypatch.setattr("dayz_mcp.tools.lifecycle.stop", lambda pid: True)
    job_id = tools.client_compile_check(wait_seconds=0).data["job_id"]
    waited = tools.job_wait(job_id, timeout=15)
    # Terminal either way: whether the check itself passed is the business of
    # the test that cares (log_tail, for one, must work on a failing run).
    assert waited.data["status"] in ("done", "failed"), waited.data
    return job_id


def test_log_verdict_judges_the_client_log_the_compile_check_produced(tmp_path, monkeypatch):
    """The whole point of source="client": after a compile check, ask for a
    verdict on what the client wrote. This failed for every project -- the
    lookup went to <stand>/clientprofile while the check writes into the job's
    own artifacts -- and no test ever passed source="client"."""
    session.reset()
    root = make_project(tmp_path)
    with_stand_and_game(root, tmp_path / "stand", tmp_path / "game")
    tools.project_open(str(root))
    _run_fake_client_compile(monkeypatch, "SCRIPT : [MyMod] loaded: items=12\nModule: Mission\n")

    r = tools.log_verdict(source="client")

    assert r.ok, f"{r.error} | {r.hint}"
    assert r.data["verdict"] == "pass"
    assert r.data["counters"]["items"] == 12
    assert "clientprofile" in r.data["log"]


def test_log_tail_reads_the_client_log_the_compile_check_produced(tmp_path, monkeypatch):
    session.reset()
    root = make_project(tmp_path)
    with_stand_and_game(root, tmp_path / "stand", tmp_path / "game")
    tools.project_open(str(root))
    _run_fake_client_compile(monkeypatch, "one\nSCRIPT (E): boom\ntwo\n")

    r = tools.log_tail(source="client", pattern="SCRIPT (E)")

    assert r.ok, f"{r.error} | {r.hint}"
    assert r.data["lines"] == ["SCRIPT (E): boom"]
    assert "clientprofile" in r.data["log"]


def test_client_log_tools_do_not_send_the_user_to_change_stand_root(tmp_path):
    """With no compile check run yet there is genuinely no client log -- but
    the hint must name the thing that would produce one. It used to say
    "check machine.stand_root", a setting the client side never reads."""
    session.reset()
    root = make_project(tmp_path)
    with_stand(root, tmp_path / "stand", "SCRIPT : [MyMod] loaded: items=12\n")
    tools.project_open(str(root))

    for r in (tools.log_verdict(source="client"), tools.log_tail(source="client")):
        assert not r.ok
        assert "stand_root" not in r.hint
        assert "client_compile_check" in r.hint


def test_client_verdict_does_not_answer_for_a_run_that_produced_nothing(tmp_path, monkeypatch):
    """A compile check that died before the client ever wrote a line has no
    log -- and must say so, rather than quietly handing back the PREVIOUS
    run's log as this run's verdict. Same discipline as `since` on the server
    side, and stricter here because nothing in the reply would reveal the
    substitution."""
    session.reset()
    root = make_project(tmp_path)
    with_stand_and_game(root, tmp_path / "stand", tmp_path / "game")
    tools.project_open(str(root))
    first = _run_fake_client_compile(monkeypatch, "SCRIPT : [MyMod] loaded: items=12\nModule: Mission\n")
    assert tools.log_verdict(source="client").data["counters"]["items"] == 12

    def boom(cmd, cwd):
        raise RuntimeError("client never started")

    monkeypatch.setattr("dayz_mcp.tools.lifecycle.spawn", boom)
    later = tools.client_compile_check(wait_seconds=0).data["job_id"]
    assert tools.job_wait(later, timeout=10).data["status"] == "failed"
    assert lifecycle.client_profile_dir(later).is_dir()  # created, but empty

    r = tools.log_verdict(source="client")
    assert not r.ok, f"answered with a stale log: {r.data}"
    # The earlier run's log is still on disk and still readable -- through its
    # own job, which is where a question about it belongs.
    assert (lifecycle.client_profile_dir(first) / "script_1.log").exists()


# --- Review round 1, Finding 1 (Critical): worker bodies must not hang the job on
# an uncaught exception ---


def test_server_start_worker_exception_fails_the_job_instead_of_hanging(tmp_path):
    """Reproduces the exact unmocked failure the reviewer found: a game directory
    whose DayZDiag_x64.exe exists (so find_game's existence probe passes) but is
    not a valid image (with_stand_and_game deliberately writes it as zero bytes).
    subprocess.Popen then raises OSError ("not a valid Win32 application") inside
    the worker thread. Without a catch there, the job would stay "running"
    forever and the next process start would mislabel it as merely lost."""
    session.reset()
    root = make_project(tmp_path)
    stand, game = tmp_path / "stand", tmp_path / "game"
    with_stand_and_game(root, stand, game)
    (stand / "serverDZ.cfg").write_text("", encoding="utf-8")
    tools.project_open(str(root))

    job_id = tools.server_start(timeout=5).data["job_id"]
    waited = tools.job_wait(job_id, timeout=15)
    assert waited.data["status"] == "failed"
    assert waited.data["error"]
    assert "OSError" in waited.data["error"]


def test_mod_build_worker_exception_fails_the_job_instead_of_hanging(tmp_path, monkeypatch):
    session.reset()
    root = make_project(tmp_path)
    tools.project_open(str(root))
    monkeypatch.setattr("dayz_mcp.tools.build.session_tools_root", lambda: "C:/tools")

    def boom(names, root, tools_root, log_dir, exclude=None, sources=None, stage=False,
             manifest_dir=None):
        raise RuntimeError("simulated packer crash")

    monkeypatch.setattr("dayz_mcp.tools.build.pack_all", boom)
    job_id = tools.mod_build().data["job_id"]
    waited = tools.job_wait(job_id, timeout=10)
    assert waited.data["status"] == "failed"
    assert "simulated packer crash" in waited.data["error"]


def test_client_compile_check_worker_exception_fails_the_job_instead_of_hanging(tmp_path, monkeypatch):
    session.reset()
    root = make_project(tmp_path)
    game = tmp_path / "game"
    game.mkdir()
    (game / "DayZDiag_x64.exe").write_bytes(b"")
    (root / "dayz-mcp.local.toml").write_text(
        f'[machine]\ngame = "{game.as_posix()}"\n', encoding="utf-8"
    )
    tools.project_open(str(root))

    def boom(cmd, cwd):
        raise RuntimeError("simulated spawn crash")

    monkeypatch.setattr("dayz_mcp.tools.lifecycle.spawn", boom)
    job_id = tools.client_compile_check(wait_seconds=0).data["job_id"]
    waited = tools.job_wait(job_id, timeout=10)
    assert waited.data["status"] == "failed"
    assert "simulated spawn crash" in waited.data["error"]


# --- Final review, item 7: server_start refuses a second server; mod_build
# refused nothing, and two builds share an output directory ---


def test_mod_build_refuses_a_second_build_while_one_is_running(tmp_path, monkeypatch):
    """Two builds of the same project write the same pbo and unlink the same
    .bisign, so the second either loses the race or corrupts the artifact.
    Tools run on worker threads, so an agent firing mod_build twice is not an
    exotic case -- it is one impatient retry."""
    session.reset()
    root = make_project(tmp_path)
    tools.project_open(str(root))

    started = threading.Event()
    release = threading.Event()

    def slow_pack_all(names, root, tools_root, log_dir, exclude=None, sources=None, stage=False,
                      manifest_dir=None):
        started.set()
        assert release.wait(timeout=10), "test never released the worker"
        return [PackResult(name="MyMod", pbo=str(root / "@MyMod/addons/MyMod.pbo"), size=10, signed=True)]

    monkeypatch.setattr("dayz_mcp.tools.build.pack_all", slow_pack_all)
    monkeypatch.setattr("dayz_mcp.tools.build.session_tools_root", lambda: "C:/tools")

    first = tools.mod_build()
    assert first.ok, first.error
    assert started.wait(timeout=10), "worker never started"

    second = tools.mod_build()
    assert not second.ok
    assert first.data["job_id"] in second.error or first.data["job_id"] in second.hint
    assert "job_wait" in second.hint

    release.set()
    assert tools.job_wait(first.data["job_id"], timeout=10).data["status"] == "done"

    # Refusal only while one is in flight: the next build goes through.
    release.set()
    third = tools.mod_build()
    assert third.ok, third.error
    assert tools.job_wait(third.data["job_id"], timeout=10).data["status"] == "done"


# --- Review round 1, Finding 2 (Important): a non-empty PackResult.note must
# reach the job summary ---


def test_mod_build_summary_includes_pack_result_notes(tmp_path, monkeypatch):
    session.reset()
    root = make_project(tmp_path)
    tools.project_open(str(root))
    note = "private key present but signer executable not found at C:/tools/Bin/DsUtils/DSSignFile.exe"
    monkeypatch.setattr(
        "dayz_mcp.tools.build.pack_all",
        lambda names, root, tools_root, log_dir, exclude=None, sources=None, stage=False, manifest_dir=None: [
            PackResult(
                name="MyMod",
                pbo=str(root / "@MyMod/addons/MyMod.pbo"),
                size=10,
                signed=False,
                note=note,
            )
        ],
    )
    monkeypatch.setattr("dayz_mcp.tools.build.session_tools_root", lambda: "C:/tools")
    job_id = tools.mod_build().data["job_id"]
    waited = tools.job_wait(job_id, timeout=10)
    assert waited.data["status"] == "done"
    assert "MyMod" in waited.data["summary"]
    assert note in waited.data["summary"]


def test_mod_build_records_pack_manifests_next_to_the_job_store(tmp_path, monkeypatch):
    """mod_build must hand the packer a manifest directory under the server's
    own bookkeeping (<root>/.dayz-mcp, beside the job store) -- never inside
    the mod tree, which is packed into the published artifact."""
    session.reset()
    root = make_project(tmp_path)
    tools.project_open(str(root))
    seen = {}

    def fake_pack_all(names, root_, tools_root, log_dir, exclude=None, sources=None, stage=False,
                      manifest_dir=None):
        seen["manifest_dir"] = manifest_dir
        return [PackResult(name="MyMod", pbo=str(root_ / "@MyMod/addons/MyMod.pbo"), size=10, signed=True)]

    monkeypatch.setattr("dayz_mcp.tools.build.pack_all", fake_pack_all)
    monkeypatch.setattr("dayz_mcp.tools.build.session_tools_root", lambda: "C:/tools")

    waited = tools.job_wait(tools.mod_build().data["job_id"], timeout=30)

    assert waited.data["status"] == "done"
    assert seen.get("manifest_dir") is not None, "mod_build did not ask for manifest tracking"
    manifest_dir = Path(seen["manifest_dir"]).resolve()
    bookkeeping = (root / ".dayz-mcp").resolve()
    assert bookkeeping in [manifest_dir, *manifest_dir.parents]
    assert (root / "MyMod").resolve() not in manifest_dir.parents


# --- Review round 1, Finding 3 (Important): tools must not run inline on the
# server's event loop ---


@pytest.mark.anyio
async def test_wrapped_tool_runs_the_sync_body_off_the_event_loop():
    main_thread = threading.get_ident()
    seen = {}

    def probe(x: int):
        seen["thread"] = threading.get_ident()
        return errors_ok({"x": x})

    wrapped = mcp_server._wrap(probe)
    result = await wrapped(x=5)
    assert result == {"ok": True, "data": {"x": 5}, "error": "", "hint": ""}
    assert seen["thread"] != main_thread


@pytest.mark.anyio
async def test_real_tool_call_through_fastmcp_still_returns_the_result_envelope(tmp_path):
    session.reset()
    root = make_project(tmp_path)
    _content, structured = await mcp_server.mcp.call_tool("project_open", {"path": str(root)})
    assert structured["ok"] is True
    assert structured["data"]["name"] == "my-mod"


def test_server_reports_its_own_version_not_the_sdks():
    """`initialize` returned serverInfo.version = "1.29.0" -- the mcp SDK's own
    version, which the low-level server uses as a default when nothing supplies
    one. Confirmed over a real stdio session. A client asking what version of
    THIS product it is talking to was told the SDK's, and would go on being told
    the SDK's through every release this project makes.

    Asserted through create_initialization_options() because that is the exact
    structure that becomes serverInfo in the initialize response, and it is also
    what breaks if a future SDK moves where the version lives.
    """
    opts = mcp_server.mcp._mcp_server.create_initialization_options()

    assert opts.server_name == DIST_NAME
    assert opts.server_version != metadata_version("mcp")
    assert opts.server_version == dayz_mcp_version
    # Belt and braces: a fallback that silently became "unknown" would satisfy
    # the inequality above while telling a client nothing.
    assert opts.server_version == metadata_version(DIST_NAME)


def test_job_wait_clamps_timeout_to_a_sane_upper_bound(tmp_path, monkeypatch):
    session.reset()
    root = make_project(tmp_path)
    tools.project_open(str(root))
    store = session.jobs()
    job = store.create("build")
    store.finish(job.id, 0, summary="done")

    captured = {}
    real_wait = store.wait

    def spy_wait(job_id, timeout):
        captured["timeout"] = timeout
        return real_wait(job_id, timeout)

    monkeypatch.setattr(store, "wait", spy_wait)
    tools.job_wait(job.id, timeout=100000)
    assert captured["timeout"] == jobs_api.MAX_WAIT_SECONDS


# --- Review round 1, Finding 4 (promoted): switching projects must not inherit
# or kill a previous project's server pid ---


def test_opening_a_new_project_does_not_inherit_or_kill_a_previous_projects_server(tmp_path):
    session.reset()
    root_a = tmp_path / "a"
    root_a.mkdir()
    make_project(root_a)
    tools.project_open(str(root_a))

    real_pid = procs_spawn([sys.executable, "-c", "import time; time.sleep(30)"], tmp_path)
    session.set_server_pid(real_pid)
    try:
        assert procs_is_alive(real_pid)

        root_b = tmp_path / "b"
        root_b.mkdir()
        make_project(root_b)
        opened_b = tools.project_open(str(root_b))
        assert opened_b.ok, opened_b.error
        assert opened_b.data.get("orphaned_server_pid") == real_pid

        # B's session must not think a server is running...
        assert session.server_pid() == 0
        status_b = tools.project_status()
        assert status_b.data["server_running"] is False

        # ...and must not have touched A's process.
        assert procs_is_alive(real_pid)

        # server_stop from B's session must be a no-op for A's process.
        stopped = tools.server_stop()
        assert stopped.data["stopped"] is False
        assert procs_is_alive(real_pid)
    finally:
        procs_stop(real_pid)


# --- Review round 2, regression fix: reopening the SAME project must not drop
# a server this session already has running ---


def test_reopening_the_same_project_keeps_the_running_server(tmp_path):
    session.reset()
    root = tmp_path / "proj"
    root.mkdir()
    make_project(root)
    tools.project_open(str(root))

    real_pid = procs_spawn([sys.executable, "-c", "import time; time.sleep(30)"], tmp_path)
    session.set_server_pid(real_pid)
    try:
        assert procs_is_alive(real_pid)

        # Simulate an agent editing dayz-mcp.local.toml and reopening the same root.
        reopened = tools.project_open(str(root))
        assert reopened.ok, reopened.error
        assert "orphaned_server_pid" not in reopened.data

        assert session.server_pid() == real_pid
        status = tools.project_status()
        assert status.data["server_running"] is True

        stopped = tools.server_stop()
        assert stopped.data["stopped"] is True
        assert stopped.data["pid"] == real_pid
        assert not procs_is_alive(real_pid)
    finally:
        if procs_is_alive(real_pid):
            procs_stop(real_pid)


def test_set_project_resolves_paths_before_comparing_them(tmp_path):
    """A relative-looking path to the same root (here, one with a redundant '.'
    segment) must still count as the same project as the original absolute
    Profile.root -- proving the comparison resolves both sides rather than
    comparing raw strings."""
    session.reset()
    root = make_project(tmp_path)
    loaded = load_profile(str(root))
    assert loaded.ok, loaded.error
    session.set_project(loaded.data, None, None)
    session.set_server_pid(999)

    loaded_again = load_profile(str(root) + "/.")
    assert loaded_again.ok, loaded_again.error
    switch = session.set_project(loaded_again.data, None, None)
    assert switch["orphaned_server_pid"] == 0
    assert session.server_pid() == 999


# --- Review round 2: server_stop(pid=...) closes the orphaned-server
# reachability hole, guarded against stopping an arbitrary pid ---


def test_server_stop_with_pid_can_stop_an_orphaned_server(tmp_path):
    session.reset()
    root_a = tmp_path / "a"
    root_a.mkdir()
    make_project(root_a)
    tools.project_open(str(root_a))

    real_pid = procs_spawn([sys.executable, "-c", "import time; time.sleep(30)"], tmp_path)
    session.set_server_pid(real_pid)
    try:
        root_b = tmp_path / "b"
        root_b.mkdir()
        make_project(root_b)
        opened_b = tools.project_open(str(root_b))
        assert opened_b.data.get("orphaned_server_pid") == real_pid
        assert session.server_pid() == 0  # B's own session has no server

        # Without a pid, server_stop only ever touches B's own (absent) server.
        blind = tools.server_stop()
        assert blind.data["stopped"] is False
        assert procs_is_alive(real_pid)

        # With the orphaned pid, it can actually be reached and stopped.
        stopped = tools.server_stop(pid=real_pid)
        assert stopped.data["stopped"] is True
        assert stopped.data["pid"] == real_pid
        assert not procs_is_alive(real_pid)
    finally:
        if procs_is_alive(real_pid):
            procs_stop(real_pid)


def test_server_stop_refuses_a_pid_the_session_never_touched(tmp_path):
    session.reset()
    root = make_project(tmp_path)
    tools.project_open(str(root))

    real_pid = procs_spawn([sys.executable, "-c", "import time; time.sleep(30)"], tmp_path)
    try:
        # This session never started `real_pid` and was never told it is orphaned.
        r = tools.server_stop(pid=real_pid)
        assert not r.ok
        assert str(real_pid) in r.error
        assert procs_is_alive(real_pid)
    finally:
        procs_stop(real_pid)


# --- Requirement 3 (pid reuse): server_stop must not taskkill a process that
# has recycled a recorded pid ---


def test_server_stop_does_not_kill_a_process_that_recycled_the_pid(tmp_path):
    """If the recorded server pid has since been handed to an unrelated
    Windows process, server_stop must notice the image-name mismatch and
    leave that process alone rather than calling taskkill on it."""
    session.reset()
    root = make_project(tmp_path)
    tools.project_open(str(root))

    # A real, unrelated process standing in for "something else now holds
    # this pid" -- it is this interpreter, not DayZDiag_x64.exe, so recording
    # the pid together with that image name reproduces a recycled pid.
    unrelated_pid = procs_spawn([sys.executable, "-c", "import time; time.sleep(30)"], tmp_path)
    try:
        session.set_server_pid(unrelated_pid, "DayZDiag_x64.exe")
        assert procs_is_alive(unrelated_pid)

        stopped = tools.server_stop()
        assert stopped.data["stopped"] is True
        assert stopped.data["pid"] == unrelated_pid
        # The unrelated process must still be running: it was never touched.
        assert procs_is_alive(unrelated_pid)
        assert session.server_pid() == 0
    finally:
        procs_stop(unrelated_pid)


# --- Review round 3: reopening the SAME project must not mark a still-running
# job as lost ---


def test_reopening_the_same_project_does_not_mark_a_running_job_as_lost(tmp_path, monkeypatch):
    """End-to-end reproduction, not an internals check: a real job is created
    through mod_build's normal path and deliberately held mid-flight (via
    synchronization events on the worker thread, not by poking session state),
    project_open is called again on the exact same root while it is still
    running, and only then is the job allowed to finish. A store-identity
    assertion would pass without proving this -- the actual observable bug was
    the persisted/in-memory job record being flipped to "failed" underneath
    the still-running worker."""
    session.reset()
    root = make_project(tmp_path)
    tools.project_open(str(root))

    started = threading.Event()
    release = threading.Event()

    def slow_pack_all(names, root, tools_root, log_dir, exclude=None, sources=None, stage=False,
                      manifest_dir=None):
        started.set()
        assert release.wait(timeout=10), "test never released the worker"
        return [
            PackResult(name="MyMod", pbo=str(root / "@MyMod/addons/MyMod.pbo"), size=10, signed=True)
        ]

    monkeypatch.setattr("dayz_mcp.tools.build.pack_all", slow_pack_all)
    monkeypatch.setattr("dayz_mcp.tools.build.session_tools_root", lambda: "C:/tools")

    job_id = tools.mod_build().data["job_id"]
    assert started.wait(timeout=10), "worker never started"

    # Confirm the job is genuinely RUNNING (store.start() already persisted
    # this) before reopening -- otherwise this test would not reproduce the race.
    status_before = tools.job_status(job_id)
    assert status_before.data["status"] == "running"

    # Simulate an agent editing dayz-mcp.local.toml and reopening the same root
    # WHILE the build is still in flight.
    reopened = tools.project_open(str(root))
    assert reopened.ok, reopened.error

    # The reopen must not have marked the still-running job as lost.
    status_after_reopen = tools.job_status(job_id)
    assert status_after_reopen.data["status"] == "running"
    assert status_after_reopen.data["error"] == ""

    release.set()
    waited = tools.job_wait(job_id, timeout=10)
    assert waited.data["status"] == "done"
    assert waited.data["error"] == ""

    # A fresh, independent lookup agrees -- not just job_wait's own return value.
    final = tools.job_status(job_id)
    assert final.data["status"] == "done"


# --- Final review, item 3: the same defect returns when a project is left and
# came back to. The previous fix compared against the immediately previous
# project only, so A -> B -> A rebuilt and reloaded A's store while A's worker
# was still alive in this very process. ---


def test_switching_away_and_back_does_not_mark_a_running_job_as_lost(tmp_path, monkeypatch):
    """A -> B -> A while a build of A is genuinely mid-flight. The agent is
    otherwise told, permanently, that a build which then succeeded had failed:
    the reloaded store's copy is stamped "lost: the server restarted while this
    job was running" and every later job_status answers from that copy, while
    the real worker writes "done" to disk underneath it."""
    session.reset()
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    a = make_project(tmp_path / "a")
    b = make_project(tmp_path / "b")
    tools.project_open(str(a))

    started = threading.Event()
    release = threading.Event()

    def slow_pack_all(names, root, tools_root, log_dir, exclude=None, sources=None, stage=False,
                      manifest_dir=None):
        started.set()
        assert release.wait(timeout=10), "test never released the worker"
        return [PackResult(name="MyMod", pbo=str(root / "@MyMod/addons/MyMod.pbo"), size=10, signed=True)]

    monkeypatch.setattr("dayz_mcp.tools.build.pack_all", slow_pack_all)
    monkeypatch.setattr("dayz_mcp.tools.build.session_tools_root", lambda: "C:/tools")

    job_id = tools.mod_build().data["job_id"]
    assert started.wait(timeout=10), "worker never started"
    assert tools.job_status(job_id).data["status"] == "running"

    assert tools.project_open(str(b)).ok
    assert tools.project_open(str(a)).ok

    after = tools.job_status(job_id)
    assert after.data["status"] == "running", f"the round trip lost the job: {after.data}"
    assert after.data["error"] == ""

    release.set()
    waited = tools.job_wait(job_id, timeout=10)
    assert waited.data["status"] == "done"
    # The lasting half of the bug: the agent asks again later and is still told
    # the build failed, long after it succeeded.
    assert tools.job_status(job_id).data["status"] == "done"
    assert tools.job_status(job_id).data["error"] == ""


def test_a_project_opened_for_the_first_time_still_recovers_jobs_lost_to_a_restart(tmp_path):
    """The other half of the rule: reuse must not cost restart recovery. A
    project this process has never opened gets a store built and load()ed, so a
    job left "running" on disk by a dead process is correctly marked lost --
    its worker really is gone."""
    session.reset()
    root = make_project(tmp_path)
    stale_dir = root / ".dayz-mcp" / "jobs" / "build-1-1"
    stale_dir.mkdir(parents=True)
    (stale_dir / "job.json").write_text(
        '{"id": "build-1-1", "kind": "build", "status": "running", "started": 1.0, '
        '"finished": null, "exit_code": null, "artifacts": [], "summary": "", "error": ""}',
        encoding="utf-8",
    )

    tools.project_open(str(root))

    recovered = tools.job_status("build-1-1")
    assert recovered.data["status"] == "failed"
    assert "lost" in recovered.data["error"]


# --- Acceptance-driven fix: machine.config makes the server config filename
# configurable, since a real stand can have a "serverDZ.cfg" that hangs forever
# after world-compile and a working config under a different name ---


def test_server_start_uses_the_configured_config_filename(tmp_path, monkeypatch):
    session.reset()
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
    session.reset()
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
    session.reset()
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


# --- Phase-1 defect, reachable through the tool a real user calls: a rebuild
# with the signing key gone used to leave the PREVIOUS signature over the new
# pbo, while mod_build reported the build as unsigned and successful. ---


def test_mod_build_does_not_leave_a_signature_over_a_pbo_it_no_longer_describes(tmp_path, monkeypatch):
    """The user-facing half of packer.py's stale-signature fix: this goes
    through mod_build and the REAL packer, with only FileBank stubbed out. A
    project that was signed once, then builds on a machine without the private
    key, must end up with no signature at all -- not one covering a pbo that
    was replaced underneath it, which a signature-verifying stand rejects while
    every tool in the chain reports success."""
    session.reset()
    root = make_project(tmp_path)
    out_dir = root / "@MyMod" / "addons"
    out_dir.mkdir(parents=True)
    stale = out_dir / "MyMod.pbo.TheKey.bisign"
    stale.write_bytes(b"signed when this machine still had the key")

    tools_root = tmp_path / "tools"
    (tools_root / "Bin" / "PboUtils").mkdir(parents=True)
    (tools_root / "Bin" / "PboUtils" / "FileBank.exe").write_text("stub", encoding="utf-8")

    def filebank_that_writes(cmd, cwd, log_path, timeout=None):
        (out_dir / "MyMod.pbo").write_bytes(b"a genuinely new pbo")
        return 0, "FileBank ok"

    monkeypatch.setattr("dayz_mcp.packer.run_blocking", filebank_that_writes)
    monkeypatch.setattr("dayz_mcp.tools.build.session_tools_root", lambda: str(tools_root))
    tools.project_open(str(root))

    waited = tools.job_wait(tools.mod_build().data["job_id"], timeout=20)
    assert waited.data["status"] == "done", waited.data
    assert "unsigned" in waited.data["summary"]
    assert not stale.exists(), "the old signature outlived the pbo it described"
    assert not list(out_dir.glob("*.bisign"))


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
    session.reset()
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
    session.reset()
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
    session.reset()
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
    session.reset()
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
    session.reset()
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
    session.reset()
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
    session.reset()
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
    session.reset()
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

    started = tools.server_start(timeout=3)
    waited = tools.job_wait(started.data["job_id"], timeout=20)
    assert waited.data["status"] == "failed"
    assert "holds udp/2302" in waited.data["error"]
    assert "ready line that never appeared" in waited.data["error"]


def test_a_dead_server_is_still_reported_dead(tmp_path, monkeypatch):
    """The premise that started this work claimed a boot which had really
    succeeded was being called a failure. It was not: that server genuinely
    died, and this must keep saying so."""
    session.reset()
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
    session.reset()
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
    session.reset()
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
    session.reset()
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
    session.reset()
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
    session.reset()
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
    monkeypatch.setattr("dayz_mcp.tools.lifecycle.PORT_READY_WAIT_SECONDS", 6.0)
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
    session.reset()
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
    monkeypatch.setattr("dayz_mcp.tools.lifecycle.PORT_READY_WAIT_SECONDS", 5.0)
    monkeypatch.setattr("dayz_mcp.tools.lifecycle.udp_port_holders", lambda port: list(running))

    started = tools.server_start(timeout=300)
    waited = tools.job_wait(started.data["job_id"], timeout=30)

    assert waited.data["status"] == "failed", waited.data
    assert "mission module never compiled" in waited.data["error"]
