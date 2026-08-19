import os
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from dayz_mcp import server as mcp_server
from dayz_mcp import tools
from dayz_mcp.errors import ok as errors_ok
from dayz_mcp.packer import PackResult
from dayz_mcp.procs import is_alive as procs_is_alive
from dayz_mcp.procs import spawn as procs_spawn
from dayz_mcp.procs import stop as procs_stop
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


def make_project(tmp_path: Path) -> Path:
    (tmp_path / "dayz-mcp.toml").write_text(textwrap.dedent(PROFILE), encoding="utf-8")
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
        lambda names, root, tools_root, log_dir: [
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
        lambda names, root, tools_root, log_dir: [PackResult(name="MyMod", error="stale pbo")],
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
    monkeypatch.setattr("dayz_mcp.tools.lifecycle.is_alive", lambda pid: False)  # dies instantly

    started = tools.server_start(timeout=5)
    assert started.ok, started.error
    since = started.data["since"]
    waited = tools.job_wait(started.data["job_id"], timeout=5)
    assert waited.data["status"] == "failed"
    assert waited.data["started"] == since


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
    monkeypatch.setattr("dayz_mcp.tools.lifecycle.is_alive", lambda pid: False)

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
    monkeypatch.setattr("dayz_mcp.tools.lifecycle.is_alive", lambda pid: True)  # keeps "running"

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
    monkeypatch.setattr("dayz_mcp.tools.lifecycle.is_alive", lambda pid: True)
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
    monkeypatch.setattr("dayz_mcp.tools.lifecycle.is_alive", lambda pid: False)

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
    monkeypatch.setattr("dayz_mcp.tools.lifecycle.is_alive", lambda pid: False)

    job_id = tools.server_start(timeout=5).data["job_id"]
    tools.job_wait(job_id, timeout=5)
    cfg_arg = next(a for a in captured["cmd"] if a.startswith("-config="))
    cfg_value = cfg_arg.split("=", 1)[1]
    assert Path(cfg_value).is_absolute()


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

    def boom(names, root, tools_root, log_dir):
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


# --- Review round 1, Finding 2 (Important): a non-empty PackResult.note must
# reach the job summary ---


def test_mod_build_summary_includes_pack_result_notes(tmp_path, monkeypatch):
    session.reset()
    root = make_project(tmp_path)
    tools.project_open(str(root))
    note = "private key present but signer executable not found at C:/tools/Bin/DsUtils/DSSignFile.exe"
    monkeypatch.setattr(
        "dayz_mcp.tools.build.pack_all",
        lambda names, root, tools_root, log_dir: [
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
