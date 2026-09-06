"""The session: one open project per process, and what switching, reopening
and stopping must not do to a server or a job somebody is still waiting on.

Split out of test_tools.py, which had grown into six suites in one file.
"""
import sys
import threading
from importlib.metadata import version as metadata_version

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
from dayz_mcp.profile import load_profile
from dayz_mcp.tools import jobs_api, session

from conftest import make_project, with_stand_and_game


def test_project_open_reports_what_it_found(tmp_path):
    r = tools.project_open(str(make_project(tmp_path)))
    assert r.ok, r.error
    assert r.data["name"] == "my-mod"
    assert r.data["own_mod_dirs"] == ["@MyMod"]


def test_tools_refuse_to_work_without_a_project():
    r = tools.mod_build()
    assert not r.ok
    assert "project_open" in r.hint


# --- Review round 1, Finding 1 (Critical): worker bodies must not hang the job on
# an uncaught exception ---


def test_server_start_worker_exception_fails_the_job_instead_of_hanging(tmp_path):
    """Reproduces the exact unmocked failure the reviewer found: a game directory
    whose DayZDiag_x64.exe exists (so find_game's existence probe passes) but is
    not a valid image (with_stand_and_game deliberately writes it as zero bytes).
    subprocess.Popen then raises OSError ("not a valid Win32 application") inside
    the worker thread. Without a catch there, the job would stay "running"
    forever and the next process start would mislabel it as merely lost."""
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


def test_client_compile_check_worker_exception_fails_the_job_instead_of_hanging(tmp_path, monkeypatch):
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
    root = make_project(tmp_path)
    tools.project_open(str(root))

    started = threading.Event()
    release = threading.Event()

    def slow_pack_all(names, root, tools_root, log_dir, exclude=None, sources=None, stage=False):
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
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    a = make_project(tmp_path / "a")
    b = make_project(tmp_path / "b")
    tools.project_open(str(a))

    started = threading.Event()
    release = threading.Event()

    def slow_pack_all(names, root, tools_root, log_dir, exclude=None, sources=None, stage=False):
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
