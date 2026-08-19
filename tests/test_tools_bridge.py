"""bridge_build and bridge_status.

The bridge is the one mod this server builds for itself: its sources live in
this repository, not in whatever project happens to be open, and every project
gets the same one. Both halves of that -- packing OUR sources, and never the
project's -- are asserted here, because getting it wrong would look like a
working build right up until the pbo turned out to hold someone else's mod.
"""
import json
import os
import textwrap
import threading
import time
from pathlib import Path

import pytest

from dayz_mcp import server as mcp_server
from dayz_mcp import tools
from dayz_mcp.bridge.channel import STATE_FILENAME
from dayz_mcp.packer import PackResult
from dayz_mcp.tools import bridge, session

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
    """Point the project at `stand` and return its profiles directory -- the
    one the server boots against, and therefore the one the mod writes its
    state file into."""
    profiles = stand / "profiles"
    profiles.mkdir(parents=True, exist_ok=True)
    (root / "dayz-mcp.local.toml").write_text(
        f'[machine]\nstand_root = "{stand.as_posix()}"\n', encoding="utf-8"
    )
    return profiles


def fake_bridge_sources(tmp_path: Path) -> Path:
    """A stand-in for this repository's own bridge/ directory, so a test can
    assert WHICH sources got packed without depending on the real ones."""
    repo = tmp_path / "server_repo"
    src = repo / "bridge" / "scripts" / "5_Mission" / bridge.BRIDGE_MOD_NAME
    src.mkdir(parents=True)
    (src / "Bridge.c").write_text("// bridge script\n", encoding="utf-8")
    (repo / "bridge" / "config.cpp").write_text("class CfgPatches {};\n", encoding="utf-8")
    return repo


def write_state(profiles: Path, tick: int) -> None:
    """Write a state snapshot the way a reader must be able to consume it.

    Deliberately atomic (temp file + replace) even though the real mod cannot
    be: these tests are about liveness, not about torn-read tolerance, which
    the channel's own tests already cover.
    """
    tmp = profiles / f".state-{tick}.tmp"
    tmp.write_text(
        json.dumps({"tick": tick, "command": None, "errors": [], "world": {}}), encoding="utf-8"
    )
    os.replace(tmp, profiles / STATE_FILENAME)


# --- bridge_build ------------------------------------------------------------


def test_bridge_build_refuses_without_an_open_project():
    session.reset()
    r = tools.bridge_build()
    assert not r.ok
    assert "project_open" in r.hint


def test_bridge_build_refuses_without_dayz_tools(tmp_path, monkeypatch):
    session.reset()
    tools.project_open(str(make_project(tmp_path)))
    monkeypatch.setattr("dayz_mcp.tools.bridge.session_tools_root", lambda: None)
    r = tools.bridge_build()
    assert not r.ok
    assert "machine.tools" in r.hint


def test_bridge_build_refuses_when_the_bridge_sources_are_missing(tmp_path, monkeypatch):
    """Installed without the repository (a wheel, say) there is nothing to
    pack. Say where it looked instead of failing somewhere inside FileBank."""
    session.reset()
    tools.project_open(str(make_project(tmp_path)))
    monkeypatch.setattr("dayz_mcp.tools.bridge.session_tools_root", lambda: "C:/tools")
    monkeypatch.setattr(bridge, "SERVER_REPO_ROOT", tmp_path / "not_a_checkout")
    r = tools.bridge_build()
    assert not r.ok
    assert "not_a_checkout" in r.error
    assert r.hint


def test_bridge_build_packs_the_servers_own_sources_not_the_projects(tmp_path, monkeypatch):
    """The whole point of the tool: OUR mod, from OUR repository. A build that
    quietly packed the open project's tree would still produce a signed pbo
    named DZMCP_Bridge -- and the stand would load the wrong code."""
    session.reset()
    project = make_project(tmp_path / "project")
    repo = fake_bridge_sources(tmp_path)
    tools.project_open(str(project))
    monkeypatch.setattr("dayz_mcp.tools.bridge.session_tools_root", lambda: "C:/tools")
    monkeypatch.setattr(bridge, "SERVER_REPO_ROOT", repo)

    captured = {}

    def fake_pack_one(name, root, tools_root, log_path, **kwargs):
        captured["name"] = name
        captured["root"] = Path(root)
        captured["log_path"] = Path(log_path)
        captured.update(kwargs)
        return PackResult(name=name, pbo=str(kwargs["mod_dir"] / f"addons/{name}.pbo"), size=42, signed=True)

    monkeypatch.setattr("dayz_mcp.tools.bridge.pack_one", fake_pack_one)

    started = tools.bridge_build()
    assert started.ok, started.error
    waited = tools.job_wait(started.data["job_id"], timeout=15)
    assert waited.data["status"] == "done", waited.data

    assert captured["name"] == bridge.BRIDGE_MOD_NAME
    assert captured["src"] == repo / "bridge"
    assert captured["mod_dir"] == repo / f"@{bridge.BRIDGE_MOD_NAME}"
    # FileBank names the pbo after the SOURCE FOLDER's basename, not after the
    # -property prefix: packing "bridge/" unstaged produces bridge.pbo and the
    # build then fails "not produced". Staging copies the source into a
    # directory named after the mod, which is the supported way out.
    assert captured["stage"] is True
    # `root` is what pack_one reads keys/ from -- OUR repository, which has
    # none, so the bridge is built unsigned and no project's private key is
    # ever touched. See the unsigned tests below for why that is the ruling.
    assert captured["root"] == repo
    assert captured["root"] != Path(project)
    # Nothing about the project's own sources may end up in the bridge pbo.
    assert Path(project) not in captured["src"].parents


def test_bridge_build_reports_the_built_pbo_in_the_job_summary(tmp_path, monkeypatch):
    session.reset()
    project = make_project(tmp_path / "project")
    repo = fake_bridge_sources(tmp_path)
    tools.project_open(str(project))
    monkeypatch.setattr("dayz_mcp.tools.bridge.session_tools_root", lambda: "C:/tools")
    monkeypatch.setattr(bridge, "SERVER_REPO_ROOT", repo)
    monkeypatch.setattr(
        "dayz_mcp.tools.bridge.pack_one",
        lambda name, root, tools_root, log_path, **kw: PackResult(
            name=name, pbo=str(kw["mod_dir"] / "addons/x.pbo"), size=3564, signed=True,
            note="staged copy included: config.cpp, scripts",
        ),
    )

    job_id = tools.bridge_build().data["job_id"]
    waited = tools.job_wait(job_id, timeout=15)
    assert waited.data["status"] == "done", waited.data
    assert "3564" in waited.data["summary"]
    assert "staged copy included" in waited.data["summary"]
    # The path is what a profile has to name to actually load the thing.
    assert str(repo / f"@{bridge.BRIDGE_MOD_NAME}") in waited.data["summary"]


def test_bridge_build_summary_tells_the_agent_how_to_attach_the_bridge(tmp_path, monkeypatch):
    """A built bridge that nothing loads is invisible: the stand boots fine and
    bridge_status then reports "never wrote state". The instructions have to
    arrive with the build, not only after a boot has already been spent
    without them."""
    session.reset()
    project = make_project(tmp_path / "project")
    repo = fake_bridge_sources(tmp_path)
    tools.project_open(str(project))
    monkeypatch.setattr("dayz_mcp.tools.bridge.session_tools_root", lambda: "C:/tools")
    monkeypatch.setattr(bridge, "SERVER_REPO_ROOT", repo)
    monkeypatch.setattr(
        "dayz_mcp.tools.bridge.pack_one",
        lambda name, root, tools_root, log_path, **kw: PackResult(
            name=name, pbo=str(kw["mod_dir"] / "addons/x.pbo"), size=10, signed=False,
        ),
    )
    job_id = tools.bridge_build().data["job_id"]
    summary = tools.job_wait(job_id, timeout=15).data["summary"]
    assert "mods.extra" in summary
    assert "server_only" in summary
    assert f"@{bridge.BRIDGE_MOD_NAME}" in summary


def test_bridge_build_fails_the_job_when_packing_reports_an_error(tmp_path, monkeypatch):
    session.reset()
    project = make_project(tmp_path / "project")
    repo = fake_bridge_sources(tmp_path)
    tools.project_open(str(project))
    monkeypatch.setattr("dayz_mcp.tools.bridge.session_tools_root", lambda: "C:/tools")
    monkeypatch.setattr(bridge, "SERVER_REPO_ROOT", repo)
    monkeypatch.setattr(
        "dayz_mcp.tools.bridge.pack_one",
        lambda name, root, tools_root, log_path, **kw: PackResult(name=name, error="stale pbo: boom"),
    )
    job_id = tools.bridge_build().data["job_id"]
    waited = tools.job_wait(job_id, timeout=15)
    assert waited.data["status"] == "failed"
    assert "stale pbo" in waited.data["error"]


def test_bridge_build_worker_exception_fails_the_job_instead_of_hanging(tmp_path, monkeypatch):
    session.reset()
    project = make_project(tmp_path / "project")
    repo = fake_bridge_sources(tmp_path)
    tools.project_open(str(project))
    monkeypatch.setattr("dayz_mcp.tools.bridge.session_tools_root", lambda: "C:/tools")
    monkeypatch.setattr(bridge, "SERVER_REPO_ROOT", repo)

    def boom(name, root, tools_root, log_path, **kw):
        raise RuntimeError("simulated packer crash")

    monkeypatch.setattr("dayz_mcp.tools.bridge.pack_one", boom)
    job_id = tools.bridge_build().data["job_id"]
    waited = tools.job_wait(job_id, timeout=15)
    assert waited.data["status"] == "failed"
    assert "simulated packer crash" in waited.data["error"]


def test_bridge_build_refuses_a_second_build_while_one_is_running(tmp_path, monkeypatch):
    """Same reason mod_build refuses: two builds write the same pbo and unlink
    the same .bisign. Tools run on worker threads, so this is one impatient
    retry away."""
    session.reset()
    project = make_project(tmp_path / "project")
    repo = fake_bridge_sources(tmp_path)
    tools.project_open(str(project))
    monkeypatch.setattr("dayz_mcp.tools.bridge.session_tools_root", lambda: "C:/tools")
    monkeypatch.setattr(bridge, "SERVER_REPO_ROOT", repo)

    started = threading.Event()
    release = threading.Event()

    def slow_pack_one(name, root, tools_root, log_path, **kw):
        started.set()
        assert release.wait(timeout=10), "test never released the worker"
        return PackResult(name=name, pbo=str(kw["mod_dir"] / "addons/x.pbo"), size=1, signed=True)

    monkeypatch.setattr("dayz_mcp.tools.bridge.pack_one", slow_pack_one)

    first = tools.bridge_build()
    assert first.ok, first.error
    assert started.wait(timeout=10), "worker never started"

    second = tools.bridge_build()
    assert not second.ok
    assert first.data["job_id"] in second.error or first.data["job_id"] in second.hint
    assert "job_wait" in second.hint

    release.set()
    assert tools.job_wait(first.data["job_id"], timeout=10).data["status"] == "done"


def test_bridge_build_refuses_a_second_build_after_switching_projects(tmp_path, monkeypatch):
    """The lock has to protect the resource that actually exists. There is ONE
    @DZMCP_Bridge output directory per process, fed by whichever project is
    open; a per-project in-flight check lets project_open(B) walk straight past
    a build still running for project A, and the two then write the same pbo.
    The spec requires acceptance on two projects, so this is a normal sequence,
    not a contrived one."""
    session.reset()
    a = make_project(tmp_path / "a")
    b = make_project(tmp_path / "b")
    repo = fake_bridge_sources(tmp_path)
    tools.project_open(str(a))
    monkeypatch.setattr("dayz_mcp.tools.bridge.session_tools_root", lambda: "C:/tools")
    monkeypatch.setattr(bridge, "SERVER_REPO_ROOT", repo)

    started = threading.Event()
    release = threading.Event()

    def slow_pack_one(name, root, tools_root, log_path, **kw):
        started.set()
        assert release.wait(timeout=10), "test never released the worker"
        return PackResult(name=name, pbo=str(kw["mod_dir"] / "addons/x.pbo"), size=1, signed=False)

    monkeypatch.setattr("dayz_mcp.tools.bridge.pack_one", slow_pack_one)

    first = tools.bridge_build()
    assert first.ok, first.error
    assert started.wait(timeout=10), "worker never started"

    assert tools.project_open(str(b)).ok
    second = tools.bridge_build()
    assert not second.ok, "switching projects walked past the in-flight bridge build"
    assert first.data["job_id"] in second.error or first.data["job_id"] in second.hint

    release.set()
    # Job records are per project, so A's job is only visible from A.
    tools.project_open(str(a))
    assert tools.job_wait(first.data["job_id"], timeout=10).data["status"] == "done"

    # Once it is finished, the next project may build again.
    tools.project_open(str(b))
    third = tools.bridge_build()
    assert third.ok, third.error
    assert tools.job_wait(third.data["job_id"], timeout=10).data["status"] == "done"


def test_bridge_build_does_not_block_a_normal_mod_build(tmp_path, monkeypatch):
    """The in-flight check is per KIND: a bridge build and a project build
    touch different output directories and must not lock each other out."""
    session.reset()
    project = make_project(tmp_path / "project")
    repo = fake_bridge_sources(tmp_path)
    tools.project_open(str(project))
    monkeypatch.setattr("dayz_mcp.tools.bridge.session_tools_root", lambda: "C:/tools")
    monkeypatch.setattr("dayz_mcp.tools.build.session_tools_root", lambda: "C:/tools")
    monkeypatch.setattr(bridge, "SERVER_REPO_ROOT", repo)

    started = threading.Event()
    release = threading.Event()

    def slow_pack_one(name, root, tools_root, log_path, **kw):
        started.set()
        assert release.wait(timeout=10), "test never released the worker"
        return PackResult(name=name, pbo=str(kw["mod_dir"] / "addons/x.pbo"), size=1, signed=True)

    monkeypatch.setattr("dayz_mcp.tools.bridge.pack_one", slow_pack_one)
    monkeypatch.setattr(
        "dayz_mcp.tools.build.pack_all",
        lambda names, root, tools_root, log_dir, exclude=None, sources=None, stage=False: [
            PackResult(name="MyMod", pbo="x.pbo", size=1, signed=True)
        ],
    )

    bridge_job = tools.bridge_build()
    assert bridge_job.ok, bridge_job.error
    assert started.wait(timeout=10)

    mod_job = tools.mod_build()
    assert mod_job.ok, mod_job.error
    assert tools.job_wait(mod_job.data["job_id"], timeout=10).data["status"] == "done"

    release.set()
    assert tools.job_wait(bridge_job.data["job_id"], timeout=10).data["status"] == "done"


# --- the FileBank staleness trap, through the real packer ---------------------


def _stub_tools(tmp_path: Path) -> Path:
    tools_root = tmp_path / "tools"
    (tools_root / "Bin" / "PboUtils").mkdir(parents=True)
    (tools_root / "Bin" / "PboUtils" / "FileBank.exe").write_text("stub", encoding="utf-8")
    return tools_root


def _filebank_that_writes(repo: Path):
    def run(cmd, cwd, log_path, timeout=None):
        out_dir = repo / f"@{bridge.BRIDGE_MOD_NAME}" / "addons"
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"{bridge.BRIDGE_MOD_NAME}.pbo").write_text("fresh pbo", encoding="utf-8")
        return 0, "FileBank ok"
    return run


def _project_with_keys(tmp_path: Path) -> Path:
    """A project carrying a signing key pair, exactly like a real one."""
    project = make_project(tmp_path / "project")
    keys = project / "keys"
    keys.mkdir()
    (keys / "ProjectKey.biprivatekey").write_bytes(b"private")
    (keys / "ProjectKey.bikey").write_bytes(b"public")
    return project


def test_bridge_build_never_consumes_the_projects_signing_key(tmp_path, monkeypatch):
    """The bridge is the server's own server-side mod. It must not be signed
    with a user's private key: that key is the project's identity, one global
    output directory fed by per-project keys means every build re-signs the
    same pbo with whoever is open, and a -serverMod pbo is never handed to a
    client to verify in the first place."""
    session.reset()
    project = _project_with_keys(tmp_path)
    repo = fake_bridge_sources(tmp_path)
    tools.project_open(str(project))
    monkeypatch.setattr("dayz_mcp.tools.bridge.session_tools_root", lambda: str(_stub_tools(tmp_path)))
    monkeypatch.setattr(bridge, "SERVER_REPO_ROOT", repo)
    monkeypatch.setattr("dayz_mcp.packer.run_blocking", _filebank_that_writes(repo))

    job_id = tools.bridge_build().data["job_id"]
    waited = tools.job_wait(job_id, timeout=20)
    assert waited.data["status"] == "done", waited.data

    mod_dir = repo / f"@{bridge.BRIDGE_MOD_NAME}"
    assert not list((mod_dir / "addons").glob("*.bisign")), "the bridge was signed"
    assert not (mod_dir / "keys").exists(), "a project's public key was copied into our mod"
    assert "ProjectKey" not in waited.data["summary"]
    assert "unsigned" in waited.data["summary"]


def test_bridge_build_clears_a_signature_left_by_an_earlier_build(tmp_path, monkeypatch):
    """The measured symptom of the old design: pack_one only unlinks stale
    .bisign files inside `if priv:`, so a build that signs nothing leaves the
    PREVIOUS project's signature next to a pbo it no longer covers -- while the
    summary says "(unsigned)". A stand that verifies signatures rejects that,
    and bridge_status then blames the wiring, sending the agent to fix
    something already correct."""
    session.reset()
    project = make_project(tmp_path / "project")
    repo = fake_bridge_sources(tmp_path)
    mod_dir = repo / f"@{bridge.BRIDGE_MOD_NAME}"
    (mod_dir / "addons").mkdir(parents=True)
    stale_sig = mod_dir / "addons" / f"{bridge.BRIDGE_MOD_NAME}.pbo.OldProjectKey.bisign"
    stale_sig.write_bytes(b"signature of a pbo that no longer exists")
    (mod_dir / "keys").mkdir()
    (mod_dir / "keys" / "OldProjectKey.bikey").write_bytes(b"public")

    tools.project_open(str(project))
    monkeypatch.setattr("dayz_mcp.tools.bridge.session_tools_root", lambda: str(_stub_tools(tmp_path)))
    monkeypatch.setattr(bridge, "SERVER_REPO_ROOT", repo)
    monkeypatch.setattr("dayz_mcp.packer.run_blocking", _filebank_that_writes(repo))

    job_id = tools.bridge_build().data["job_id"]
    assert tools.job_wait(job_id, timeout=20).data["status"] == "done"

    assert not stale_sig.exists(), "a stale signature survived the build"
    assert not (mod_dir / "keys").exists(), "another project's public key survived the build"
    assert (mod_dir / "addons" / f"{bridge.BRIDGE_MOD_NAME}.pbo").exists()


def test_bridge_build_clears_a_stale_signature_even_when_the_build_fails(tmp_path, monkeypatch):
    """A failed build leaves the OLD pbo in place. A signature next to it is
    worse than none: it makes an out-of-date artifact look verified."""
    session.reset()
    project = make_project(tmp_path / "project")
    repo = fake_bridge_sources(tmp_path)
    mod_dir = repo / f"@{bridge.BRIDGE_MOD_NAME}"
    (mod_dir / "addons").mkdir(parents=True)
    old_pbo = mod_dir / "addons" / f"{bridge.BRIDGE_MOD_NAME}.pbo"
    old_pbo.write_text("yesterday's bridge", encoding="utf-8")
    old = time.time() - 5000
    os.utime(old_pbo, (old, old))
    stale_sig = mod_dir / "addons" / f"{bridge.BRIDGE_MOD_NAME}.pbo.OldProjectKey.bisign"
    stale_sig.write_bytes(b"signature")

    tools.project_open(str(project))
    monkeypatch.setattr("dayz_mcp.tools.bridge.session_tools_root", lambda: str(_stub_tools(tmp_path)))
    monkeypatch.setattr(bridge, "SERVER_REPO_ROOT", repo)
    monkeypatch.setattr("dayz_mcp.packer.run_blocking", lambda *a, **kw: (0, "FileBank: nothing to do"))

    job_id = tools.bridge_build().data["job_id"]
    assert tools.job_wait(job_id, timeout=20).data["status"] == "failed"
    assert not stale_sig.exists()


def test_bridge_build_is_covered_by_the_stale_pbo_guard(tmp_path, monkeypatch):
    """FileBank does nothing, silently and with exit code 0, when it judges the
    sources not newer than an existing pbo -- the stand then keeps running
    yesterday's bridge while the build reports success. pack_one's stale check
    is what catches that, and this asserts the bridge path really goes through
    it: the REAL pack_one runs here, with only FileBank itself stubbed out to
    reproduce the silent skip (pbo left untouched and older than the sources).
    """
    session.reset()
    project = make_project(tmp_path / "project")
    repo = fake_bridge_sources(tmp_path)
    tools.project_open(str(project))
    monkeypatch.setattr("dayz_mcp.tools.bridge.session_tools_root", lambda: str(_stub_tools(tmp_path)))
    monkeypatch.setattr(bridge, "SERVER_REPO_ROOT", repo)

    out_dir = repo / f"@{bridge.BRIDGE_MOD_NAME}" / "addons"
    out_dir.mkdir(parents=True)
    stale = out_dir / f"{bridge.BRIDGE_MOD_NAME}.pbo"
    stale.write_text("yesterday's bridge", encoding="utf-8")
    old = time.time() - 5000
    os.utime(stale, (old, old))

    def filebank_that_silently_skips(cmd, cwd, log_path, timeout=None):
        return 0, "FileBank: nothing to do"

    monkeypatch.setattr("dayz_mcp.packer.run_blocking", filebank_that_silently_skips)

    job_id = tools.bridge_build().data["job_id"]
    waited = tools.job_wait(job_id, timeout=20)
    assert waited.data["status"] == "failed", waited.data
    assert "stale pbo" in waited.data["error"]


def test_bridge_build_succeeds_when_filebank_really_writes_the_pbo(tmp_path, monkeypatch):
    """The other half of the guard: a build that genuinely happened must not be
    reported stale. Same setup, only FileBank's stand-in actually writes."""
    session.reset()
    project = make_project(tmp_path / "project")
    repo = fake_bridge_sources(tmp_path)
    tools.project_open(str(project))
    monkeypatch.setattr("dayz_mcp.tools.bridge.session_tools_root", lambda: str(_stub_tools(tmp_path)))
    monkeypatch.setattr(bridge, "SERVER_REPO_ROOT", repo)

    def filebank_that_writes(cmd, cwd, log_path, timeout=None):
        out_dir = repo / f"@{bridge.BRIDGE_MOD_NAME}" / "addons"
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"{bridge.BRIDGE_MOD_NAME}.pbo").write_text("fresh pbo", encoding="utf-8")
        return 0, "FileBank ok"

    monkeypatch.setattr("dayz_mcp.packer.run_blocking", filebank_that_writes)

    job_id = tools.bridge_build().data["job_id"]
    waited = tools.job_wait(job_id, timeout=20)
    assert waited.data["status"] == "done", waited.data
    assert bridge.BRIDGE_MOD_NAME in waited.data["summary"]


# --- bridge_status -----------------------------------------------------------


def test_bridge_status_refuses_without_an_open_project():
    session.reset()
    r = tools.bridge_status()
    assert not r.ok
    assert "project_open" in r.hint


def test_bridge_status_says_no_server_rather_than_calling_the_bridge_dead(tmp_path):
    session.reset()
    root = make_project(tmp_path)
    with_stand(root, tmp_path / "stand")
    tools.project_open(str(root))

    r = tools.bridge_status(window=0.1)
    assert not r.ok
    assert r.data["state"] == "no_server"
    assert "server" in r.error.lower()
    assert "server_start" in r.hint
    # It must not blame the bridge for something the bridge cannot control.
    assert "bridge is dead" not in r.error.lower()


def test_bridge_status_does_not_read_a_leftover_state_file_as_a_live_bridge(tmp_path):
    """The file survives the server that wrote it. With nothing running, an
    advancing-looking snapshot on disk is residue, and reporting it as a live
    bridge would be a straight lie."""
    session.reset()
    root = make_project(tmp_path)
    profiles = with_stand(root, tmp_path / "stand")
    tools.project_open(str(root))
    write_state(profiles, tick=999)

    r = tools.bridge_status(window=0.1)
    assert not r.ok
    assert r.data["state"] == "no_server"
    assert r.data["alive"] is False


def test_bridge_status_reports_a_server_that_never_wrote_state(tmp_path, monkeypatch):
    session.reset()
    root = make_project(tmp_path)
    with_stand(root, tmp_path / "stand")
    tools.project_open(str(root))
    session.set_server_pid(4321, "DayZDiag_x64.exe")
    monkeypatch.setattr("dayz_mcp.tools.bridge.is_alive", lambda pid, image="": True)

    r = tools.bridge_status(window=0.1)
    assert not r.ok
    assert r.data["state"] == "no_state_file"
    assert r.data["alive"] is False
    assert r.data["tick"] is None
    # The actionable cause: the mod is not loaded, or was never built.
    assert "bridge_build" in r.hint
    assert "serverMod" in r.hint


def test_bridge_status_tells_an_unreadable_state_file_from_a_missing_one(tmp_path, monkeypatch):
    """Different causes, different fixes: no file at all means the mod is not
    running; a file that never parses means it is running and writing rubbish."""
    session.reset()
    root = make_project(tmp_path)
    profiles = with_stand(root, tmp_path / "stand")
    tools.project_open(str(root))
    session.set_server_pid(4321, "DayZDiag_x64.exe")
    monkeypatch.setattr("dayz_mcp.tools.bridge.is_alive", lambda pid, image="": True)
    (profiles / STATE_FILENAME).write_text("{not json at all", encoding="utf-8")

    r = tools.bridge_status(window=0.1)
    assert not r.ok
    assert r.data["state"] == "unreadable_state"
    assert STATE_FILENAME in r.data["state_file"]


def test_bridge_status_does_not_call_a_published_tick_of_zero_unreadable(tmp_path, monkeypatch):
    """"Parsed, and the tick is 0" is a different fact from "nothing parseable
    in there". Reporting the second sends the reader off to hunt script errors
    and rebuild the mod for a file that is perfectly well-formed."""
    session.reset()
    root = make_project(tmp_path)
    profiles = with_stand(root, tmp_path / "stand")
    tools.project_open(str(root))
    session.set_server_pid(4321, "DayZDiag_x64.exe")
    monkeypatch.setattr("dayz_mcp.tools.bridge.is_alive", lambda pid, image="": True)
    write_state(profiles, tick=0)

    r = tools.bridge_status(window=0.2)
    assert not r.ok
    assert r.data["state"] != "unreadable_state"
    assert r.data["tick"] == 0
    assert "rebuild" not in r.hint
    assert "log_verdict" not in r.hint


def test_bridge_status_reports_a_frozen_tick_as_not_alive(tmp_path, monkeypatch):
    """A tick that does not move is a dead bridge with a file left behind. The
    number still has to come back -- it is the evidence."""
    session.reset()
    root = make_project(tmp_path)
    profiles = with_stand(root, tmp_path / "stand")
    tools.project_open(str(root))
    session.set_server_pid(4321, "DayZDiag_x64.exe")
    monkeypatch.setattr("dayz_mcp.tools.bridge.is_alive", lambda pid, image="": True)
    write_state(profiles, tick=42)

    r = tools.bridge_status(window=0.3)
    assert not r.ok
    assert r.data["state"] == "frozen"
    assert r.data["alive"] is False
    assert r.data["tick"] == 42
    assert r.data["advancing"] is False
    assert r.hint


def test_bridge_status_reports_an_advancing_tick_as_alive(tmp_path, monkeypatch):
    session.reset()
    root = make_project(tmp_path)
    profiles = with_stand(root, tmp_path / "stand")
    tools.project_open(str(root))
    session.set_server_pid(4321, "DayZDiag_x64.exe")
    monkeypatch.setattr("dayz_mcp.tools.bridge.is_alive", lambda pid, image="": True)

    stop = threading.Event()

    def ticker():
        n = 1
        while not stop.is_set():
            write_state(profiles, tick=n)
            n += 1
            time.sleep(0.02)

    write_state(profiles, tick=1)
    worker = threading.Thread(target=ticker, daemon=True)
    worker.start()
    try:
        r = tools.bridge_status(window=0.3)
    finally:
        stop.set()
        worker.join(timeout=5)

    assert r.ok, f"{r.error} | {r.hint}"
    assert r.data["state"] == "alive"
    assert r.data["alive"] is True
    assert r.data["advancing"] is True
    assert r.data["tick"] > 1


def test_bridge_status_admits_it_cannot_tell_without_a_second_sample(tmp_path, monkeypatch):
    """window=0 buys a fast answer at the price of the only thing that proves
    liveness. Reporting "not advancing" there would be a guess dressed as a
    measurement, so it reports "unknown" -- with the tick it did read."""
    session.reset()
    root = make_project(tmp_path)
    profiles = with_stand(root, tmp_path / "stand")
    tools.project_open(str(root))
    session.set_server_pid(4321, "DayZDiag_x64.exe")
    monkeypatch.setattr("dayz_mcp.tools.bridge.is_alive", lambda pid, image="": True)
    write_state(profiles, tick=7)

    r = tools.bridge_status(window=0)
    assert not r.ok
    assert r.data["state"] == "unknown"
    assert r.data["tick"] == 7
    assert r.data["advancing"] is None
    assert "window" in r.hint


def test_bridge_status_clamps_a_silly_window(tmp_path, monkeypatch):
    """A health check must stay a health check: nothing here may turn into a
    disguised long wait, which is what job_wait exists for."""
    session.reset()
    root = make_project(tmp_path)
    profiles = with_stand(root, tmp_path / "stand")
    tools.project_open(str(root))
    session.set_server_pid(4321, "DayZDiag_x64.exe")
    monkeypatch.setattr("dayz_mcp.tools.bridge.is_alive", lambda pid, image="": True)
    write_state(profiles, tick=5)

    captured = {}

    def fake_heartbeat(self, window=3.0):
        captured["window"] = window
        return False, 5

    monkeypatch.setattr("dayz_mcp.bridge.channel.Channel.heartbeat", fake_heartbeat)
    tools.bridge_status(window=10_000)
    assert captured["window"] == bridge.STATUS_WINDOW_MAX


# --- registration ------------------------------------------------------------


@pytest.mark.anyio
async def test_bridge_tools_are_registered_with_their_real_parameters():
    """Registration must go through the same wrapper as every other tool:
    functools.wraps is what lets FastMCP see the real signature (without it the
    tool exposes opaque args/kwargs), and the worker thread is what keeps a
    call that sleeps for a window off the server's event loop."""
    listed = {t.name: t for t in await mcp_server.mcp.list_tools()}
    assert "bridge_build" in listed
    assert "bridge_status" in listed
    assert "window" in listed["bridge_status"].inputSchema["properties"]


@pytest.mark.anyio
async def test_bridge_status_through_fastmcp_returns_the_envelope(tmp_path):
    session.reset()
    root = make_project(tmp_path)
    with_stand(root, tmp_path / "stand")
    tools.project_open(str(root))
    _content, structured = await mcp_server.mcp.call_tool("bridge_status", {"window": 0.1})
    assert structured["ok"] is False
    assert structured["hint"]
    assert structured["data"]["state"] == "no_server"
