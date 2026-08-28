"""bridge_build and bridge_status.

The bridge is the one mod this server builds for itself: its sources live in
this repository, not in whatever project happens to be open, and every project
gets the same one. Both halves of that -- packing OUR sources, and never the
project's -- are asserted here, because getting it wrong would look like a
working build right up until the pbo turned out to hold someone else's mod.
"""
import json
import re
import os
import textwrap
import threading
import time
from pathlib import Path

import pytest

from dayz_mcp import server as mcp_server
from dayz_mcp import tools
from dayz_mcp.errors import ok as errors_ok
from dayz_mcp.bridge.channel import CMD_FILENAME, STATE_FILENAME, Channel, HeartbeatSample
from dayz_mcp.bridge.protocol import ParseRejection
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


def write_state(profiles: Path, tick: int, session_id: str = "boot-1") -> None:
    """Write a state snapshot the way a reader must be able to consume it.

    `session_id` is REQUIRED on the wire (protocol.BridgeState): the mod's tick
    restarts at 0 every boot while this file survives in the profile directory,
    so a tick comparison is only meaningful within one session. Tests that care
    about a restart pass a different one for the second boot.

    Deliberately atomic (temp file + replace) even though the real mod cannot
    be: these tests are about liveness, not about torn-read tolerance, which
    the channel's own tests already cover.
    """
    tmp = profiles / f".state-{tick}.tmp"
    tmp.write_text(
        json.dumps({
            "tick": tick, "session_id": session_id,
            "command": None, "errors": [], "world": {},
        }),
        encoding="utf-8",
    )
    # Windows denies a replace while the destination is open for reading, and
    # these tests deliberately have a reader sampling the same file a few times
    # a second. That is an artifact of writing atomically -- the real mod
    # overwrites in place and never hits it -- so retry briefly rather than
    # letting a ticker thread die mid-test.
    for attempt in range(50):
        try:
            os.replace(tmp, profiles / STATE_FILENAME)
            return
        except PermissionError:
            if attempt == 49:
                raise
            time.sleep(0.005)


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
        lambda names, root, tools_root, log_dir, exclude=None, sources=None, stage=False, manifest_dir=None: [
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


def test_bridge_status_reports_a_command_wedged_in_a_stopped_stand(tmp_path):
    """Nothing but the mod ever empties the mailbox, and a stopped stand keeps
    its profile directory. So a command sent while the server was down sits
    there forever AND executes at the first tick of the next boot -- a specific,
    diagnosable situation that nothing reported before this."""
    session.reset()
    root = make_project(tmp_path)
    profiles = with_stand(root, tmp_path / "stand")
    tools.project_open(str(root))
    mailbox = profiles / CMD_FILENAME
    mailbox.write_text('{"id": "spawn-1", "verb": "spawn", "args": {}}', encoding="utf-8")

    r = tools.bridge_status(window=0.1)
    assert not r.ok
    assert r.data["state"] == "stale_command"
    assert r.data["mailbox"]["present"] is True
    assert r.data["mailbox"]["path"] == str(mailbox)
    assert r.data["mailbox"]["age_seconds"] is not None
    # The hazard is not "a file is here", it is "this runs when you next boot".
    assert "next" in r.error.lower() or "boot" in r.error.lower()
    assert str(mailbox) in r.hint


def test_bridge_status_surfaces_an_unclaimed_command_while_the_bridge_is_unwired(tmp_path, monkeypatch):
    """The same wedge with the server UP: the bridge was never attached, so
    nothing will ever claim the command. The state stays no_state_file -- that
    is still the fix to make -- but the waiting command has to be visible, or
    the agent wires the bridge and is then surprised by a command it sent
    minutes ago running at the first tick."""
    session.reset()
    root = make_project(tmp_path)
    profiles = with_stand(root, tmp_path / "stand")
    tools.project_open(str(root))
    session.set_server_pid(4321, "DayZDiag_x64.exe")
    monkeypatch.setattr("dayz_mcp.tools.bridge.is_alive", lambda pid, image="": True)
    (profiles / CMD_FILENAME).write_text('{"id": "ping-1", "verb": "ping", "args": {}}', encoding="utf-8")

    r = tools.bridge_status(window=0.1)
    assert not r.ok
    assert r.data["state"] == "no_state_file"
    assert r.data["mailbox"]["present"] is True
    assert "unclaimed" in r.error.lower() or "waiting" in r.error.lower()


def test_bridge_status_reports_the_mailbox_on_every_path(tmp_path, monkeypatch):
    """Including the healthy one: an empty mailbox is a fact worth stating
    once, so a caller never has to guess whether the field is missing or the
    mailbox is clear."""
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
    assert r.data["mailbox"]["present"] is False
    assert r.data["mailbox"]["age_seconds"] is None


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
    in there", and reporting the second would send the reader off to rebuild a
    mod whose file is perfectly well-formed.

    The tick-0 ambiguity is now settled upstream: with session_id on the wire,
    two readable samples of the same session with the same tick are `stalled`
    whatever that tick happens to be, so 0 reaches the ordinary frozen answer
    instead of a reason about readability. That IS the truth here -- a
    published tick that does not move is frozen -- so the assertions are about
    which diagnosis is NOT reached."""
    session.reset()
    root = make_project(tmp_path)
    profiles = with_stand(root, tmp_path / "stand")
    tools.project_open(str(root))
    session.set_server_pid(4321, "DayZDiag_x64.exe")
    monkeypatch.setattr("dayz_mcp.tools.bridge.is_alive", lambda pid, image="": True)
    write_state(profiles, tick=0)

    # Same reason as the frozen test above: a stall verdict needs a gap the mod
    # could have ticked across.
    r = tools.bridge_status(window=1.2)
    assert not r.ok
    assert r.data["state"] not in ("unreadable_state", "outdated_bridge", "no_state_file")
    assert r.data["state"] == "frozen"
    assert r.data["heartbeat"] == "stalled"
    assert r.data["tick"] == 0
    assert "rebuild" not in r.hint  # nothing is wrong with the mod's build


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

    # Longer than the mod's publish interval on purpose: a "same tick twice"
    # verdict taken over a shorter gap is evidence of nothing -- the mod has not
    # had a fair chance to write again -- so a frozen diagnosis must not be
    # reachable from one. A test that asked for `frozen` over 0.3s was asserting
    # exactly the wrong-diagnosis this rule exists to prevent.
    r = tools.bridge_status(window=1.2)
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

    def fake_heartbeat_detail(self, window=3.0):
        captured["window"] = window
        # Keywords, not positions: `gap` was inserted ahead of
        # `previous_session_id`, and a positional call at the channel's own
        # restarted branch silently carried a session id into the new slot when
        # it landed. This fixture has no business knowing the field order.
        return HeartbeatSample(status="unmeasurable", tick=0, session_id=None, gap=None)

    # Patched at the PUBLIC entry point this tool actually calls. An earlier
    # version reached past it to the channel's sampling internals, which then
    # changed arity underneath the patch -- a test breaking on a private
    # signature it had no business knowing.
    monkeypatch.setattr("dayz_mcp.bridge.channel.Channel.heartbeat_detail", fake_heartbeat_detail)
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
    assert "bridge_clear" in listed
    assert "window" in listed["bridge_status"].inputSchema["properties"]
    clear_params = listed["bridge_clear"].inputSchema["properties"]
    assert "force" in clear_params and "probe_window" in clear_params
    # The destructive path is opt-in, and the schema is where a caller sees it.
    assert clear_params["force"].get("default") is False


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


# --- The channel's session-aware heartbeat: four outcomes, four answers -------


def test_bridge_status_tells_a_restart_from_a_freeze(tmp_path, monkeypatch):
    """The tick restarts at 0 every boot while the state file survives in the
    profile directory, so a naive comparison reads a freshly booted, healthy
    bridge as dead. A changed session id between the two samples means a NEW
    world came up -- alive, and emphatically not frozen."""
    session.reset()
    root = make_project(tmp_path)
    profiles = with_stand(root, tmp_path / "stand")
    tools.project_open(str(root))
    session.set_server_pid(4321, "DayZDiag_x64.exe")
    monkeypatch.setattr("dayz_mcp.tools.bridge.is_alive", lambda pid, image="": True)

    write_state(profiles, tick=5000, session_id="boot-before")

    def restart():
        time.sleep(0.05)
        write_state(profiles, tick=1, session_id="boot-after")

    worker = threading.Thread(target=restart, daemon=True)
    worker.start()
    try:
        r = tools.bridge_status(window=0.3)
    finally:
        worker.join(timeout=5)

    assert r.ok, f"{r.error} | {r.hint}"
    assert r.data["state"] == "restarted"
    assert r.data["alive"] is True
    assert r.data["heartbeat"] == "restarted"
    assert r.data["tick"] == 1  # the NEW session's own tick, never compared to the old
    # Nothing was measured about movement WITHIN the new session.
    assert r.data["advancing"] is None


def test_bridge_status_says_it_could_not_measure_rather_than_frozen(tmp_path, monkeypatch):
    """A failed second sample is a measurement failure, not a diagnosis.
    Reporting it as "frozen" is what sent an agent hunting script errors that
    were never there."""
    session.reset()
    root = make_project(tmp_path)
    profiles = with_stand(root, tmp_path / "stand")
    tools.project_open(str(root))
    session.set_server_pid(4321, "DayZDiag_x64.exe")
    monkeypatch.setattr("dayz_mcp.tools.bridge.is_alive", lambda pid, image="": True)

    write_state(profiles, tick=77)

    def vanish():
        time.sleep(0.05)
        (profiles / STATE_FILENAME).unlink(missing_ok=True)

    worker = threading.Thread(target=vanish, daemon=True)
    worker.start()
    try:
        r = tools.bridge_status(window=0.4)
    finally:
        worker.join(timeout=5)

    assert not r.ok
    assert r.data["state"] == "unknown", r.data
    assert r.data["heartbeat"] == "unmeasurable"
    assert r.data["tick"] == 77  # the one sample that WAS read
    assert r.data["advancing"] is None
    # The frozen diagnosis, and its script-error hunt, must not be reached here.
    assert "frozen" not in r.error
    assert "log_verdict" not in r.hint


def test_bridge_status_names_a_bridge_mod_older_than_this_server(tmp_path, monkeypatch):
    """session_id is required on the wire, and required only works if its
    absence is diagnosable. A state document that parses perfectly but has no
    session_id was written by a bridge mod predating this server -- saying "the
    mod is writing something it cannot finish" is false, and sends the reader to
    log_verdict and a rebuild of the wrong thing."""
    session.reset()
    root = make_project(tmp_path)
    profiles = with_stand(root, tmp_path / "stand")
    tools.project_open(str(root))
    session.set_server_pid(4321, "DayZDiag_x64.exe")
    monkeypatch.setattr("dayz_mcp.tools.bridge.is_alive", lambda pid, image="": True)
    (profiles / STATE_FILENAME).write_text(
        json.dumps({"tick": 12, "command": None, "errors": [], "world": {}}), encoding="utf-8"
    )

    r = tools.bridge_status(window=0.1)
    assert not r.ok
    assert r.data["state"] == "outdated_bridge", r.data
    assert "session_id" in r.error
    assert "bridge_build" in r.hint
    assert "cannot finish" not in r.error  # that is the OTHER diagnosis


# --- bridge_clear -------------------------------------------------------------


def test_bridge_clear_refuses_without_an_open_project():
    session.reset()
    r = tools.bridge_clear()
    assert not r.ok
    assert "project_open" in r.hint


def test_bridge_clear_on_an_empty_mailbox_says_so(tmp_path):
    session.reset()
    root = make_project(tmp_path)
    with_stand(root, tmp_path / "stand")
    tools.project_open(str(root))

    r = tools.bridge_clear()
    assert not r.ok
    assert "empty" in r.error
    assert r.hint


def test_bridge_clear_discards_a_wedged_command_and_names_it(tmp_path):
    """The remedy for the wedge bridge_status reports. WHICH command was thrown
    away is the whole point: an agent has to be able to tell whether it was the
    one it cared about."""
    session.reset()
    root = make_project(tmp_path)
    profiles = with_stand(root, tmp_path / "stand")
    tools.project_open(str(root))
    mailbox = profiles / CMD_FILENAME
    mailbox.write_text(
        json.dumps({"id": "spawn-17", "verb": "spawn", "args": {"type": "Apple"}}), encoding="utf-8"
    )

    r = tools.bridge_clear(probe_window=0.1)
    assert r.ok, f"{r.error} | {r.hint}"
    assert r.data["discarded_id"] == "spawn-17"
    assert r.data["discarded"]["verb"] == "spawn"
    assert r.data["heartbeat"] == "unmeasurable"  # nothing running, nothing to measure
    assert not mailbox.exists()

    # And the channel is usable again: there is nothing left to clear.
    assert not tools.bridge_clear(probe_window=0.1).ok


def test_bridge_clear_refuses_a_live_bridge_unless_forced(tmp_path):
    """Discarding a command a running mod could claim any moment is worse than
    the wedge. The destructive path exists, but it has to be asked for."""
    session.reset()
    root = make_project(tmp_path)
    profiles = with_stand(root, tmp_path / "stand")
    tools.project_open(str(root))
    (profiles / CMD_FILENAME).write_text(
        '{"id": "ping-3", "verb": "ping", "args": {}}', encoding="utf-8"
    )

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
        refused = tools.bridge_clear(probe_window=0.3)
        assert not refused.ok
        assert "force" in refused.hint
        assert (profiles / CMD_FILENAME).exists(), "a refusal still deleted the command"

        forced = tools.bridge_clear(force=True, probe_window=0.3)
        assert forced.ok, f"{forced.error} | {forced.hint}"
        assert forced.data["discarded_id"] == "ping-3"
        # What the force overrode stays on the record.
        assert forced.data["heartbeat"] == "growing"
    finally:
        stop.set()
        worker.join(timeout=5)

    assert not (profiles / CMD_FILENAME).exists()


def test_bridge_clear_clamps_its_probe_window(tmp_path, monkeypatch):
    """It blocks for probe_window like every other sampling call here, so it
    obeys the same ceiling."""
    session.reset()
    root = make_project(tmp_path)
    profiles = with_stand(root, tmp_path / "stand")
    tools.project_open(str(root))
    (profiles / CMD_FILENAME).write_text('{"id": "x", "verb": "x", "args": {}}', encoding="utf-8")

    captured = {}

    def fake_clear(self, force=False, probe_window=3.0):
        captured["force"] = force
        captured["probe_window"] = probe_window
        return errors_ok({"discarded": {"id": "x"}, "heartbeat": "unmeasurable"})

    monkeypatch.setattr("dayz_mcp.bridge.channel.Channel.clear_mailbox", fake_clear)
    tools.bridge_clear(probe_window=10_000)
    assert captured["probe_window"] == bridge.STATUS_WINDOW_MAX
    assert captured["force"] is False


def test_stale_command_points_at_the_tool_that_fixes_it(tmp_path):
    """The state and its remedy shipped a round apart; they have to meet."""
    session.reset()
    root = make_project(tmp_path)
    profiles = with_stand(root, tmp_path / "stand")
    tools.project_open(str(root))
    (profiles / CMD_FILENAME).write_text(
        '{"id": "c-1", "verb": "ping", "args": {}}', encoding="utf-8"
    )

    r = tools.bridge_status(window=0.1)
    assert r.data["state"] == "stale_command"
    assert "bridge_clear" in r.hint


# --- The in-flight slot must never outlive the build it stands for -----------


def test_a_failure_before_the_build_starts_does_not_wedge_every_later_build(tmp_path, monkeypatch):
    """store.start() used to sit OUTSIDE the worker's try. Anything failing
    there -- an antivirus lock on job.json, a removed .dayz-mcp -- killed the
    thread before its finally, so the in-flight slot was never released: the
    job stayed queued forever and every later bridge_build in the process was
    refused, naming a job that would never run. The traceback reached stderr
    and nowhere else."""
    session.reset()
    project = make_project(tmp_path / "project")
    repo = fake_bridge_sources(tmp_path)
    tools.project_open(str(project))
    monkeypatch.setattr("dayz_mcp.tools.bridge.session_tools_root", lambda: "C:/tools")
    monkeypatch.setattr(bridge, "SERVER_REPO_ROOT", repo)
    monkeypatch.setattr(
        "dayz_mcp.tools.bridge.pack_one",
        lambda name, root, tools_root, log_path, **kw: PackResult(
            name=name, pbo=str(kw["mod_dir"] / "addons/x.pbo"), size=1, signed=False),
    )

    store = session.jobs()
    real_start = store.start
    calls = {"n": 0}

    def start_that_fails_once(job_id):
        calls["n"] += 1
        if calls["n"] == 1:
            raise PermissionError("job.json is locked by something else")
        return real_start(job_id)

    monkeypatch.setattr(store, "start", start_that_fails_once)

    first = tools.bridge_build()
    assert first.ok, first.error
    waited = tools.job_wait(first.data["job_id"], timeout=15)
    assert waited.data["status"] == "failed", waited.data
    assert "PermissionError" in waited.data["error"]

    # The slot has to be free again: this is the wedge.
    second = tools.bridge_build()
    assert second.ok, f"the process is wedged: {second.error} | {second.hint}"
    assert tools.job_wait(second.data["job_id"], timeout=15).data["status"] == "done"


def test_a_thread_that_never_starts_does_not_wedge_every_later_build(tmp_path, monkeypatch):
    """The other end of the same hole: the slot is claimed before the worker
    thread exists, so a Thread.start() that raises leaves nothing to release
    it."""
    session.reset()
    project = make_project(tmp_path / "project")
    repo = fake_bridge_sources(tmp_path)
    tools.project_open(str(project))
    monkeypatch.setattr("dayz_mcp.tools.bridge.session_tools_root", lambda: "C:/tools")
    monkeypatch.setattr(bridge, "SERVER_REPO_ROOT", repo)
    monkeypatch.setattr(
        "dayz_mcp.tools.bridge.pack_one",
        lambda name, root, tools_root, log_path, **kw: PackResult(
            name=name, pbo=str(kw["mod_dir"] / "addons/x.pbo"), size=1, signed=False),
    )

    class ThreadingThatCannotStart:
        @staticmethod
        def Thread(*args, **kwargs):
            raise RuntimeError("can't start new thread")

    monkeypatch.setattr(bridge, "threading", ThreadingThatCannotStart)
    refused = tools.bridge_build()
    assert not refused.ok
    assert "thread" in refused.error.lower()
    monkeypatch.undo()

    # Same monkeypatches as above, minus the broken threading.
    monkeypatch.setattr("dayz_mcp.tools.bridge.session_tools_root", lambda: "C:/tools")
    monkeypatch.setattr(bridge, "SERVER_REPO_ROOT", repo)
    monkeypatch.setattr(
        "dayz_mcp.tools.bridge.pack_one",
        lambda name, root, tools_root, log_path, **kw: PackResult(
            name=name, pbo=str(kw["mod_dir"] / "addons/x.pbo"), size=1, signed=False),
    )
    again = tools.bridge_build()
    assert again.ok, f"the process is wedged: {again.error} | {again.hint}"
    assert tools.job_wait(again.data["job_id"], timeout=15).data["status"] == "done"


def test_the_refusal_names_the_project_that_can_actually_show_the_job(tmp_path, monkeypatch):
    """Job stores are per project. After a switch, the refusal named a job that
    job_status and job_wait both answer "unknown job" for -- and that hint sends
    the agent hunting an imaginary typo."""
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
    assert started.wait(timeout=10)

    assert tools.project_open(str(b)).ok
    refused = tools.bridge_build()
    assert not refused.ok
    # The job really is unreachable from here...
    assert not tools.job_status(first.data["job_id"]).ok
    # ...so the refusal has to say where it lives.
    assert str(a) in refused.hint or a.name in refused.hint
    assert "project_open" in refused.hint

    release.set()
    tools.project_open(str(a))
    assert tools.job_wait(first.data["job_id"], timeout=10).data["status"] == "done"


# --- The strip must not fail silently, and must run when packing raises ------


def test_a_keys_folder_that_cannot_be_removed_fails_the_build(tmp_path, monkeypatch):
    """ignore_errors=True let an undeletable keys/ survive while the job
    reported success and the summary said "(unsigned)" -- the exact
    accumulate-every-project's-key state this strip exists to prevent, now with
    no signal at all."""
    session.reset()
    project = make_project(tmp_path / "project")
    repo = fake_bridge_sources(tmp_path)
    mod_dir = repo / f"@{bridge.BRIDGE_MOD_NAME}"
    (mod_dir / "keys").mkdir(parents=True)
    (mod_dir / "keys" / "SomeoneElse.bikey").write_bytes(b"public")

    tools.project_open(str(project))
    monkeypatch.setattr("dayz_mcp.tools.bridge.session_tools_root", lambda: "C:/tools")
    monkeypatch.setattr(bridge, "SERVER_REPO_ROOT", repo)
    monkeypatch.setattr(
        "dayz_mcp.tools.bridge.pack_one",
        lambda name, root, tools_root, log_path, **kw: PackResult(
            name=name, pbo=str(kw["mod_dir"] / "addons/x.pbo"), size=1, signed=False),
    )

    class ShutilThatCannotRemove:
        @staticmethod
        def rmtree(path, *args, **kwargs):
            raise PermissionError(f"{path} is held open")

    monkeypatch.setattr(bridge, "shutil", ShutilThatCannotRemove)

    waited = tools.job_wait(tools.bridge_build().data["job_id"], timeout=15)
    assert waited.data["status"] == "failed", waited.data
    assert "keys" in waited.data["error"]
    assert (mod_dir / "keys").exists()  # and it really did survive, as reported


def test_the_strip_runs_even_when_packing_raises(tmp_path, monkeypatch):
    """The strip lives in a finally so a crash mid-pack cannot leave a
    signature over an artifact nobody rebuilt. Moving it to a sequential call
    leaves every other bridge test green, so this is the one that notices."""
    session.reset()
    project = make_project(tmp_path / "project")
    repo = fake_bridge_sources(tmp_path)
    mod_dir = repo / f"@{bridge.BRIDGE_MOD_NAME}"
    (mod_dir / "addons").mkdir(parents=True)
    sig = mod_dir / "addons" / f"{bridge.BRIDGE_MOD_NAME}.pbo.OldKey.bisign"
    sig.write_bytes(b"signature")

    tools.project_open(str(project))
    monkeypatch.setattr("dayz_mcp.tools.bridge.session_tools_root", lambda: "C:/tools")
    monkeypatch.setattr(bridge, "SERVER_REPO_ROOT", repo)

    def boom(name, root, tools_root, log_path, **kw):
        raise RuntimeError("simulated packer crash")

    monkeypatch.setattr("dayz_mcp.tools.bridge.pack_one", boom)

    waited = tools.job_wait(tools.bridge_build().data["job_id"], timeout=15)
    assert waited.data["status"] == "failed"
    assert "simulated packer crash" in waited.data["error"]
    assert not sig.exists(), "a crash left a signature over a pbo nobody rebuilt"


def test_the_stale_command_answer_is_true_before_the_mod_reads_commands(tmp_path):
    """The file's survival across a boot is measured; the mod claiming it is
    not -- the shipped bridge reads no mailbox yet. The wording has to be true
    now and once that lands."""
    session.reset()
    root = make_project(tmp_path)
    profiles = with_stand(root, tmp_path / "stand")
    tools.project_open(str(root))
    (profiles / CMD_FILENAME).write_text('{"id": "c-9", "verb": "ping", "args": {}}', encoding="utf-8")

    r = tools.bridge_status(window=0.1)
    assert r.data["state"] == "stale_command"
    # It must not assert as fact something only a later version of the mod does.
    assert "will be executed by the first tick" not in r.error
    assert "expire" in r.error or "survive" in r.error
    assert "bridge_clear" in r.hint
    # Nor may it claim the command survives a boot: server_start clears the
    # transport before spawning, so the only stand that would pick this up is
    # one started outside these tools -- which is what the answer has to say.
    assert "next boot" not in r.error
    assert "OUTSIDE" in r.error or "outside" in r.error
    assert "server_start" in r.hint


# --- Round 4: the same wedge shape, a third time, and the tests that missed it


def _accepting_pack_one(name, root, tools_root, log_path, **kw):
    return PackResult(name=name, pbo=str(kw["mod_dir"] / "addons/x.pbo"), size=1, signed=False)


def _ready_to_build(tmp_path, monkeypatch):
    """An open project, a fake bridge repository and a pack_one that succeeds --
    everything the wedge tests need except the failure they inject."""
    session.reset()
    project = make_project(tmp_path / "project")
    repo = fake_bridge_sources(tmp_path)
    tools.project_open(str(project))
    monkeypatch.setattr("dayz_mcp.tools.bridge.session_tools_root", lambda: "C:/tools")
    monkeypatch.setattr(bridge, "SERVER_REPO_ROOT", repo)
    monkeypatch.setattr("dayz_mcp.tools.bridge.pack_one", _accepting_pack_one)
    return project, repo


def test_a_failure_preparing_the_artifacts_dir_returns_a_result_and_frees_the_slot(tmp_path, monkeypatch):
    """Third appearance of one shape: something between "call accepted" and the
    guarded region raises. store.artifacts_dir() calls mkdir, which fails on
    exactly the causes the fix already cites -- a permission change, a removed
    parent, the path replaced by a file. The exception escaped to the MCP layer
    with NO Result at all, the job stayed queued, and the slot stayed claimed."""
    _ready_to_build(tmp_path, monkeypatch)
    store = session.jobs()
    real_artifacts_dir = store.artifacts_dir
    calls = {"n": 0}

    def artifacts_dir_that_fails_once(job_id):
        calls["n"] += 1
        if calls["n"] == 1:
            raise PermissionError("cannot create the job directory")
        return real_artifacts_dir(job_id)

    monkeypatch.setattr(store, "artifacts_dir", artifacts_dir_that_fails_once)

    refused = tools.bridge_build()
    # A Result, not a traceback: the caller has to be told something.
    assert not refused.ok
    assert "PermissionError" in refused.error
    assert refused.hint

    monkeypatch.setattr(store, "artifacts_dir", real_artifacts_dir)
    again = tools.bridge_build()
    assert again.ok, f"the process is wedged: {again.error} | {again.hint}"
    assert tools.job_wait(again.data["job_id"], timeout=15).data["status"] == "done"


def test_a_failure_creating_the_job_still_answers_the_caller(tmp_path, monkeypatch):
    """The same sweep, one line earlier: store.create() writes job.json and can
    fail for the same reasons. Nothing is claimed yet at that point, so there is
    no wedge -- but a tool that raises instead of returning an envelope is its
    own defect."""
    _ready_to_build(tmp_path, monkeypatch)
    store = session.jobs()
    real_create = store.create
    calls = {"n": 0}

    def create_that_fails_once(kind):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("no space left on device")
        return real_create(kind)

    monkeypatch.setattr(store, "create", create_that_fails_once)

    refused = tools.bridge_build()
    assert not refused.ok
    assert "OSError" in refused.error

    monkeypatch.setattr(store, "create", real_create)
    again = tools.bridge_build()
    assert again.ok, f"the process is wedged: {again.error} | {again.hint}"
    assert tools.job_wait(again.data["job_id"], timeout=15).data["status"] == "done"


def test_the_slot_is_released_even_when_the_job_cannot_be_failed(tmp_path, monkeypatch):
    """THE test for the release itself. Every other wedge test here passes with
    both _release_build_slot calls deleted, because the in-flight check's
    status re-check rescues them: store.fail() marks the job failed, and the
    backstop then sees a terminal status and lets the next build through. That
    pins the outcome through the OLD mechanism.

    When store.fail ALSO raises, the job stays at "running" forever and the
    backstop cannot help. Only the worker's finally frees the slot -- so this
    is the case that fails if the release is removed."""
    _ready_to_build(tmp_path, monkeypatch)
    store = session.jobs()

    def start_that_fails(job_id):
        raise PermissionError("job.json is locked")

    def fail_that_also_fails(job_id, error):
        raise PermissionError("job.json is still locked")

    monkeypatch.setattr(store, "start", start_that_fails)
    monkeypatch.setattr(store, "fail", fail_that_also_fails)

    first = tools.bridge_build()
    assert first.ok, first.error

    # The job is stuck non-terminal: the backstop has nothing to notice.
    deadline = time.time() + 5
    while time.time() < deadline:
        stuck = tools.job_status(first.data["job_id"])
        if stuck.data["status"] in ("queued", "running"):
            break
        time.sleep(0.05)
    assert tools.job_status(first.data["job_id"]).data["status"] in ("queued", "running")

    monkeypatch.undo()
    monkeypatch.setattr("dayz_mcp.tools.bridge.session_tools_root", lambda: "C:/tools")
    monkeypatch.setattr(bridge, "SERVER_REPO_ROOT", bridge.SERVER_REPO_ROOT)
    monkeypatch.setattr("dayz_mcp.tools.bridge.pack_one", _accepting_pack_one)

    second = tools.bridge_build()
    assert second.ok, (
        "the slot outlived a build whose job could not even be marked failed: "
        f"{second.error} | {second.hint}"
    )


def test_one_readable_sample_at_tick_zero_is_not_an_unreadable_file(tmp_path, monkeypatch):
    """`tick > 0` as "a sample was read" collides with a real tick of 0: the
    answer fell through to unreadable_state, which claims no readable snapshot
    could be taken when one was, threw the tick away as None, and sent the
    reader to log_verdict and a rebuild. The same scenario at tick 7 answered
    `unknown` correctly, which is what makes it a regression rather than a
    gap."""
    session.reset()
    root = make_project(tmp_path)
    profiles = with_stand(root, tmp_path / "stand")
    tools.project_open(str(root))
    session.set_server_pid(4321, "DayZDiag_x64.exe")
    monkeypatch.setattr("dayz_mcp.tools.bridge.is_alive", lambda pid, image="": True)
    write_state(profiles, tick=0)

    # The reviewer's own reproduction, with no mocking: a readable sample at
    # tick 0, and a file that is torn by the time the second sample is taken --
    # and STAYS torn, which is what defeated the extra-read version of this fix.
    def tear_the_file():
        time.sleep(0.05)
        (profiles / STATE_FILENAME).write_text('{"tick": 1, "sess', encoding="utf-8")

    worker = threading.Thread(target=tear_the_file, daemon=True)
    worker.start()
    try:
        r = tools.bridge_status(window=0.4)
    finally:
        worker.join(timeout=5)
    assert not r.ok
    assert r.data["state"] == "unknown", r.data
    assert r.data["heartbeat"] == "unmeasurable"
    assert r.data["tick"] == 0  # the evidence, not None
    assert "rebuild" not in r.hint
    assert "log_verdict" not in r.hint


def test_a_mangled_session_id_is_not_reported_as_an_outdated_bridge(tmp_path, monkeypatch):
    """Under a non-truncating in-place overwrite, a length change ahead of the
    key can leave valid JSON with a tick and a MANGLED session_id -- and the
    tool then tells the user to rebuild a perfectly current bridge. Which write
    model the mod really uses is still an open question, so the verdict must not
    depend on the answer."""
    session.reset()
    root = make_project(tmp_path)
    profiles = with_stand(root, tmp_path / "stand")
    tools.project_open(str(root))
    session.set_server_pid(4321, "DayZDiag_x64.exe")
    monkeypatch.setattr("dayz_mcp.tools.bridge.is_alive", lambda pid, image="": True)
    monkeypatch.setattr(bridge, "SECOND_OPINION_SECONDS", 0.01)
    (profiles / STATE_FILENAME).write_text(
        json.dumps({"tick": 1000, "ession_id": "boot-1", "command": None,
                    "errors": [], "world": {}}),
        encoding="utf-8",
    )

    # A live mod repairs the mangle with its very next write, so the rejection
    # is there once and gone a moment later. Driven from the read rather than
    # from a sleeping writer thread, and counted -- the timed version of this
    # shape was vacuous once already, passing with the second read deleted.
    reads = {"n": 0}
    real_rejection = Channel.read_state_rejection

    def mangled_once(self):
        reads["n"] += 1
        return real_rejection(self) if reads["n"] == 1 else None

    monkeypatch.setattr("dayz_mcp.bridge.channel.Channel.read_state_rejection", mangled_once)

    r = tools.bridge_status(window=0.1)
    assert not r.ok
    assert reads["n"] == 2, f"the rejection was checked {reads['n']} time(s), not twice"
    assert r.data["state"] != "invalid_state", r.data
    assert r.data["state"] != "outdated_bridge", r.data


def test_an_outdated_state_document_needs_two_agreeing_reads(tmp_path, monkeypatch):
    """The other half: a document that reads as pre-session once and as current
    a moment later is a transient, not an old mod. Only a verdict that holds
    across a full publish interval accuses the bridge of being outdated.

    The transition is driven from the read itself, not from a sleeping writer
    thread. The timed version of this test was vacuous -- the healing write
    landed before the first read, so the verdict was already "not outdated"
    and the test passed with the second read deleted entirely. Counting the
    reads is what actually pins the mechanism."""
    session.reset()
    root = make_project(tmp_path)
    profiles = with_stand(root, tmp_path / "stand")
    tools.project_open(str(root))
    session.set_server_pid(4321, "DayZDiag_x64.exe")
    monkeypatch.setattr("dayz_mcp.tools.bridge.is_alive", lambda pid, image="": True)
    monkeypatch.setattr(bridge, "SECOND_OPINION_SECONDS", 0.01)
    (profiles / STATE_FILENAME).write_text(
        json.dumps({"tick": 5, "command": None, "errors": [], "world": {}}), encoding="utf-8"
    )

    reads = {"n": 0}

    def pre_session_once(state_file):
        # Old on the first read, current on every later one -- exactly what a
        # single mangled in-place write looks like from outside.
        reads["n"] += 1
        return reads["n"] == 1

    monkeypatch.setattr(bridge, "_reads_as_pre_session", pre_session_once)

    r = tools.bridge_status(window=0.1)

    assert reads["n"] == 2, f"the state file was read {reads['n']} time(s), not twice"
    assert r.data["state"] != "outdated_bridge", r.data


def test_a_genuinely_old_state_document_is_still_named(tmp_path, monkeypatch):
    """Strictness must not blunt the verdict this exists for."""
    session.reset()
    root = make_project(tmp_path)
    profiles = with_stand(root, tmp_path / "stand")
    tools.project_open(str(root))
    session.set_server_pid(4321, "DayZDiag_x64.exe")
    monkeypatch.setattr("dayz_mcp.tools.bridge.is_alive", lambda pid, image="": True)
    monkeypatch.setattr(bridge, "SECOND_OPINION_SECONDS", 0.05)
    (profiles / STATE_FILENAME).write_text(
        json.dumps({"tick": 12, "command": None, "errors": [], "world": {}}), encoding="utf-8"
    )

    r = tools.bridge_status(window=0.1)
    assert r.data["state"] == "outdated_bridge", r.data
    assert "bridge_build" in r.hint


def test_a_signature_that_cannot_be_removed_fails_the_build(tmp_path, monkeypatch):
    """The keys half of the strip had a test; the .bisign half did not, though
    it is the half that makes a stand reject the mod."""
    session.reset()
    project = make_project(tmp_path / "project")
    repo = fake_bridge_sources(tmp_path)
    mod_dir = repo / f"@{bridge.BRIDGE_MOD_NAME}"
    (mod_dir / "addons").mkdir(parents=True)
    sig = mod_dir / "addons" / f"{bridge.BRIDGE_MOD_NAME}.pbo.OldKey.bisign"
    sig.write_bytes(b"signature")

    tools.project_open(str(project))
    monkeypatch.setattr("dayz_mcp.tools.bridge.session_tools_root", lambda: "C:/tools")
    monkeypatch.setattr(bridge, "SERVER_REPO_ROOT", repo)
    monkeypatch.setattr("dayz_mcp.tools.bridge.pack_one", _accepting_pack_one)

    real_unlink = Path.unlink

    def unlink_that_fails_for_signatures(self, *args, **kwargs):
        if self.suffix == ".bisign":
            raise PermissionError(f"{self} is held open")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink_that_fails_for_signatures)

    waited = tools.job_wait(tools.bridge_build().data["job_id"], timeout=15)
    assert waited.data["status"] == "failed", waited.data
    assert ".bisign" in waited.data["error"]
    assert sig.exists()


def test_the_owner_refusal_spells_a_path_the_way_the_other_hints_do(tmp_path, monkeypatch):
    """A raw Windows path inside quotes reads as an escape soup and cannot be
    pasted into project_open. wiring_instructions() already uses as_posix() for
    exactly this reason."""
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
        return _accepting_pack_one(name, root, tools_root, log_path, **kw)

    monkeypatch.setattr("dayz_mcp.tools.bridge.pack_one", slow_pack_one)
    first = tools.bridge_build()
    assert started.wait(timeout=10)

    tools.project_open(str(b))
    refused = tools.bridge_build()
    assert not refused.ok
    assert Path(a).as_posix() in refused.hint
    assert "\\" not in refused.hint

    release.set()
    tools.project_open(str(a))
    tools.job_wait(first.data["job_id"], timeout=10)


# --- The tool descriptions are a contract, and they have rotted three times ---


@pytest.mark.anyio
async def test_the_tool_descriptions_do_not_contradict_the_code():
    """These strings are what FastMCP hands the driving agent, and what the
    mod-side author reads as the contract, so a wrong one costs more than a
    wrong comment. Three separate rounds have had to correct a claim here that
    described how the system USED to work -- "only the mod empties the mailbox"
    after two Python-side emptiers existed, "the next boot executes it" after
    server_start started clearing the transport.

    Pinned as load-bearing FACTS rather than sentences: the phrasing is free to
    change, but a description that stops mentioning the behaviour it is the
    contract for fails here.
    """
    # Whitespace-normalised: a docstring rewrap is not rot, and a phrase split
    # across two lines would otherwise fail for the wrong reason.
    listed = {
        t.name: " ".join((t.description or "").split())
        for t in await mcp_server.mcp.list_tools()
    }

    # server_start deletes two files before spawning. An agent that does not
    # know this cannot reason about a command it queued a moment ago -- so the
    # description has to name WHAT is removed, not merely mention the bridge.
    start = listed["server_start"].lower()
    assert "mailbox" in start
    assert "state file" in start
    assert "clear" in start or "remove" in start
    # Readiness has two signals now, and which one answered changes what the
    # answer means. An agent that does not know the port is one of them cannot
    # read "ready via port bind" correctly.
    assert "port" in start
    assert "expect.ready_line" in listed["server_start"]

    # bridge_clear is not the only thing that empties the mailbox, and the
    # description must not go back to implying it is.
    assert "server_start" in listed["bridge_clear"]
    assert "force" in listed["bridge_clear"]
    for gone in ("only the MOD ever empties", "boots next"):
        assert gone not in listed["bridge_clear"], gone

    # bridge_status's stale_command answer must not claim the next boot runs it.
    assert "next\n                    boot executes" not in listed["bridge_status"]
    assert "server_start" in listed["bridge_status"]

    # bridge_build produces an UNSIGNED artifact and does not attach it.
    assert "unsigned" in listed["bridge_build"].lower()

    # The live session id is a contract fact the mod-side acceptance probes
    # depend on, and the only place it is published is this answer.
    assert "session_id" in listed["bridge_status"]
    # bridge_clear refuses on a running server before it probes anything.
    assert "session started is running" in listed["bridge_clear"]

    # The window question was answered with documentation rather than a
    # different number, which makes the documentation the deliverable: what a
    # missing state file now costs, and the way out of paying it.
    status_desc = listed["bridge_status"]
    assert "window=0" in status_desc
    assert "full window" in status_desc.lower()
    assert re.search(r"\d+\.\d+s", status_desc), "the measured cost is gone"


# --- M4b: the live session id, on every answer that read one ------------------


def test_the_alive_answer_carries_the_session_id(tmp_path, monkeypatch):
    """Three of Task 5's acceptance probes need to know which world they are
    talking to, and until heartbeat_detail existed no tool could tell them."""
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
            write_state(profiles, tick=n, session_id="world-abc")
            n += 1
            time.sleep(0.02)

    write_state(profiles, tick=1, session_id="world-abc")
    worker = threading.Thread(target=ticker, daemon=True)
    worker.start()
    try:
        r = tools.bridge_status(window=0.3)
    finally:
        stop.set()
        worker.join(timeout=5)

    assert r.ok, f"{r.error} | {r.hint}"
    assert r.data["state"] == "alive"
    assert r.data["session_id"] == "world-abc"
    assert r.data["previous_session_id"] is None if "previous_session_id" in r.data else True


def test_the_restarted_answer_carries_both_session_ids(tmp_path, monkeypatch):
    """What it was and what it is now. "A restart happened" without the old id
    leaves a caller unable to say whether the session IT was talking to is the
    one that went away."""
    session.reset()
    root = make_project(tmp_path)
    profiles = with_stand(root, tmp_path / "stand")
    tools.project_open(str(root))
    session.set_server_pid(4321, "DayZDiag_x64.exe")
    monkeypatch.setattr("dayz_mcp.tools.bridge.is_alive", lambda pid, image="": True)
    write_state(profiles, tick=5000, session_id="world-before")

    def restart():
        time.sleep(0.05)
        write_state(profiles, tick=1, session_id="world-after")

    worker = threading.Thread(target=restart, daemon=True)
    worker.start()
    try:
        r = tools.bridge_status(window=0.3)
    finally:
        worker.join(timeout=5)

    assert r.ok, f"{r.error} | {r.hint}"
    assert r.data["state"] == "restarted"
    assert r.data["session_id"] == "world-after"
    assert r.data["previous_session_id"] == "world-before"


def test_an_answer_that_read_nothing_reports_no_session(tmp_path, monkeypatch):
    session.reset()
    root = make_project(tmp_path)
    with_stand(root, tmp_path / "stand")
    tools.project_open(str(root))
    session.set_server_pid(4321, "DayZDiag_x64.exe")
    monkeypatch.setattr("dayz_mcp.tools.bridge.is_alive", lambda pid, image="": True)

    r = tools.bridge_status(window=0.1)
    assert r.data["state"] == "no_state_file"
    assert r.data["session_id"] is None


def test_the_seam_invariant_a_readable_state_always_has_a_session(tmp_path):
    """_a_sample_was_read reads `session_id is not None` as "a sample came
    back", which is only sound while the layer below cannot produce a state
    that parses AND has no session id. That is parse_state's rule, not mine, so
    it is pinned here: if it ever changes, this fails loudly instead of
    bridge_status silently reporting a readable sample as unread."""
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    channel = Channel(profiles)
    state = profiles / STATE_FILENAME

    for document in (
        {"tick": 5, "command": None, "errors": [], "world": {}},          # no session_id
        {"tick": 5, "session_id": "", "command": None, "errors": []},      # empty
        {"tick": 5, "session_id": None, "command": None, "errors": []},    # null
    ):
        state.write_text(json.dumps(document), encoding="utf-8")
        assert channel.read_state() is None, document

    state.write_text(json.dumps({"tick": 5, "session_id": "w1"}), encoding="utf-8")
    read = channel.read_state()
    assert read is not None and read.session_id == "w1"


# --- M1b: name the offending field instead of blaming a torn write ------------


def _status_over_a_state_document(tmp_path, monkeypatch, document_text: str):
    session.reset()
    root = make_project(tmp_path)
    profiles = with_stand(root, tmp_path / "stand")
    tools.project_open(str(root))
    session.set_server_pid(4321, "DayZDiag_x64.exe")
    monkeypatch.setattr("dayz_mcp.tools.bridge.is_alive", lambda pid, image="": True)
    monkeypatch.setattr(bridge, "SECOND_OPINION_SECONDS", 0.01)
    (profiles / STATE_FILENAME).write_text(document_text, encoding="utf-8")
    return tools.bridge_status(window=0.1)


@pytest.mark.parametrize(
    ("label", "document", "field"),
    [
        ("root is not an object", "[1, 2, 3]", "<root>"),
        ("bad command status",
         json.dumps({"tick": 1, "session_id": "w", "command": {"id": "c", "status": "nope"}}),
         "command.status"),
        ("errors is not a list",
         json.dumps({"tick": 1, "session_id": "w", "errors": "boom"}), "errors"),
        ("world is not an object",
         json.dumps({"tick": 1, "session_id": "w", "world": [1]}), "world"),
        ("tick is not a genuine int",
         json.dumps({"tick": "7", "session_id": "w"}), "tick"),
        ("session_id is empty",
         json.dumps({"tick": 1, "session_id": "", "command": None, "errors": [], "world": {}}),
         "session_id"),
    ],
)
def test_a_schema_failure_names_the_field(tmp_path, monkeypatch, label, document, field):
    """A mod author reads this answer at six minutes a try -- a boot each. It
    has to let them fix a field, not rebuild a healthy mod. All six shapes
    parse_state validates are covered, because a diagnosis that only works for
    some of them sends the rest to the torn-write advice."""
    r = _status_over_a_state_document(tmp_path, monkeypatch, document)

    assert not r.ok
    assert r.data["state"] == "invalid_state", f"{label}: {r.data}"
    assert field in r.error, f"{label}: {r.error}"
    assert r.data["invalid_field"] == field
    assert r.data["invalid_reason"]
    # The torn-write advice must not be reached: nothing here is a half-written
    # file, and a rebuild of a healthy mod is not the fix.
    assert "cannot finish" not in r.error, label
    assert "log_verdict" not in r.hint, label
    assert "log_tail" not in r.hint, label


def test_a_schema_failure_shows_the_value_that_was_seen(tmp_path, monkeypatch):
    """The field name alone still costs a boot to act on: "tick is wrong" and
    "tick is the STRING '7'" are minutes apart for whoever has to fix it."""
    r = _status_over_a_state_document(
        tmp_path, monkeypatch, json.dumps({"tick": "7", "session_id": "w"})
    )
    assert "'7'" in r.error or '"7"' in r.error


def test_a_torn_write_still_gets_the_torn_write_answer(tmp_path, monkeypatch):
    """The other side of the same rule: a document that is not valid JSON at all
    is the ordinary once-a-second condition, and keeps the answer that says so."""
    r = _status_over_a_state_document(tmp_path, monkeypatch, '{"tick": 1, "sess')
    assert r.data["state"] == "unreadable_state", r.data
    assert r.data["invalid_field"] is None


# --- M3b: a tracked live server needs force, whatever the state file says -----


def test_bridge_clear_refuses_while_this_sessions_server_is_running(tmp_path, monkeypatch):
    """The state file is not the only evidence of life, and the tool layer holds
    the other half: a server this session started IS running, whatever its
    bridge is or is not publishing. A mod that has not written a state document
    yet -- every mod before the state writer lands -- otherwise looks exactly
    like a downed stand to the probe."""
    session.reset()
    root = make_project(tmp_path)
    profiles = with_stand(root, tmp_path / "stand")
    tools.project_open(str(root))
    mailbox = profiles / CMD_FILENAME
    mailbox.write_text('{"id": "ping-9", "verb": "ping", "args": {}}', encoding="utf-8")
    session.set_server_pid(4321, "DayZDiag_x64.exe")
    monkeypatch.setattr("dayz_mcp.tools.bridge.is_alive", lambda pid, image="": True)

    refused = tools.bridge_clear()
    assert not refused.ok
    assert "4321" in refused.error
    assert "force" in refused.hint
    assert mailbox.exists(), "a refusal still deleted the command"

    forced = tools.bridge_clear(force=True)
    assert forced.ok, f"{forced.error} | {forced.hint}"
    assert forced.data["discarded_id"] == "ping-9"
    # What the force overrode stays on the record, from this layer as well as
    # the channel's.
    assert forced.data["server_running"] is True
    assert forced.data["forced"] is True
    assert not mailbox.exists()


def test_bridge_clear_does_not_probe_at_all_when_it_can_refuse_outright(tmp_path, monkeypatch):
    """The channel now retries its first sample to the window's deadline, so a
    probe over a missing state file costs the whole window. Refusing on the
    liveness this layer already knows costs nothing -- and must not pay it."""
    session.reset()
    root = make_project(tmp_path)
    profiles = with_stand(root, tmp_path / "stand")
    tools.project_open(str(root))
    (profiles / CMD_FILENAME).write_text('{"id": "x", "verb": "x", "args": {}}', encoding="utf-8")
    session.set_server_pid(4321, "DayZDiag_x64.exe")
    monkeypatch.setattr("dayz_mcp.tools.bridge.is_alive", lambda pid, image="": True)

    probed = {"n": 0}

    def counting_clear(self, force=False, probe_window=3.0):
        probed["n"] += 1
        return errors_ok({"discarded": None, "heartbeat": "unmeasurable"})

    monkeypatch.setattr("dayz_mcp.bridge.channel.Channel.clear_mailbox", counting_clear)
    tools.bridge_clear()
    assert probed["n"] == 0, "it probed for a window it did not need"


def test_bridge_clear_floors_its_probe_window(tmp_path, monkeypatch):
    """Below the mod's publish interval a "stalled" verdict proves nothing, and
    the channel rightly demands force for it. A caller who passes 0.1 should get
    a probe that can actually answer, not a refusal about their own window."""
    session.reset()
    root = make_project(tmp_path)
    profiles = with_stand(root, tmp_path / "stand")
    tools.project_open(str(root))
    (profiles / CMD_FILENAME).write_text('{"id": "x", "verb": "x", "args": {}}', encoding="utf-8")

    captured = {}

    def fake_clear(self, force=False, probe_window=3.0):
        captured["probe_window"] = probe_window
        return errors_ok({"discarded": {"id": "x"}, "heartbeat": "unmeasurable"})

    monkeypatch.setattr("dayz_mcp.bridge.channel.Channel.clear_mailbox", fake_clear)
    tools.bridge_clear(probe_window=0.1)
    assert captured["probe_window"] == bridge.CLEAR_PROBE_MIN_SECONDS
    tools.bridge_clear(probe_window=10_000)
    assert captured["probe_window"] == bridge.STATUS_WINDOW_MAX


# --- Round 6: the halves that could rot silently ------------------------------


def test_two_different_fields_failing_in_turn_is_not_a_schema_bug(tmp_path, monkeypatch):
    """The SAME-FIELD half of the repeat requirement, which the top-level
    counted-read test does not reach: it proves a rejection must appear twice,
    not that it must be the same one twice.

    A file being written through can reject on `tick` at one instant and on
    `session_id` at the next -- two different fields, neither of them a
    consistent shape anybody is publishing. Blaming either would send an author
    to correct a field that is fine, which is the whole failure mode this
    mechanism exists to prevent."""
    session.reset()
    root = make_project(tmp_path)
    profiles = with_stand(root, tmp_path / "stand")
    tools.project_open(str(root))
    session.set_server_pid(4321, "DayZDiag_x64.exe")
    monkeypatch.setattr("dayz_mcp.tools.bridge.is_alive", lambda pid, image="": True)
    monkeypatch.setattr(bridge, "SECOND_OPINION_SECONDS", 0.01)
    (profiles / STATE_FILENAME).write_text(
        json.dumps({"tick": "7", "session_id": "w"}), encoding="utf-8"
    )

    reads = {"n": 0}
    fields = ["tick", "session_id"]

    def a_different_field_each_time(self):
        # Driven from the read, and counted, for the same reason the other
        # repeat tests are: a timed version passes with the mechanism removed.
        reads["n"] += 1
        return ParseRejection(fields[min(reads["n"], len(fields)) - 1], "wrong", "x")

    monkeypatch.setattr(
        "dayz_mcp.bridge.channel.Channel.read_state_rejection", a_different_field_each_time
    )

    r = tools.bridge_status(window=0.1)

    assert reads["n"] == 2, f"the rejection was checked {reads['n']} time(s), not twice"
    assert r.data["state"] != "invalid_state", r.data
    assert r.data["invalid_field"] is None


def test_the_same_field_twice_is_still_named(tmp_path, monkeypatch):
    """The other side of that rule, so the fix cannot be "never accuse"."""
    session.reset()
    root = make_project(tmp_path)
    profiles = with_stand(root, tmp_path / "stand")
    tools.project_open(str(root))
    session.set_server_pid(4321, "DayZDiag_x64.exe")
    monkeypatch.setattr("dayz_mcp.tools.bridge.is_alive", lambda pid, image="": True)
    monkeypatch.setattr(bridge, "SECOND_OPINION_SECONDS", 0.01)
    (profiles / STATE_FILENAME).write_text(
        json.dumps({"tick": "7", "session_id": "w"}), encoding="utf-8"
    )

    r = tools.bridge_status(window=0.1)
    assert r.data["state"] == "invalid_state", r.data
    assert r.data["invalid_field"] == "tick"


# --- The wiring-up state must not be sent into a refusal ----------------------


def test_the_unwired_answer_names_what_actually_works(tmp_path, monkeypatch):
    """The exact state a bridge is wired up in: server running, bridge not
    loaded, a command already sent. The answer used to say bridge_clear discards
    it -- and bridge_clear then refuses, on this tool's own liveness gate, for a
    command nothing in that world can possibly claim. An instruction that leads
    to a refusal is worse than none: it costs a call to find out.

    Asserted by FOLLOWING it, not by matching a string."""
    session.reset()
    root = make_project(tmp_path)
    profiles = with_stand(root, tmp_path / "stand")
    tools.project_open(str(root))
    mailbox = profiles / CMD_FILENAME
    mailbox.write_text('{"id": "spawn-2", "verb": "spawn", "args": {}}', encoding="utf-8")
    session.set_server_pid(4321, "DayZDiag_x64.exe")
    monkeypatch.setattr("dayz_mcp.tools.bridge.is_alive", lambda pid, image="": True)

    status = tools.bridge_status(window=0.1)
    assert status.data["state"] == "no_state_file"
    assert status.data["mailbox"]["present"] is True

    # What the answer says to do has to be a thing that works from here.
    assert "force=True" in status.error or "force=True" in status.hint
    assert not tools.bridge_clear().ok  # the plain call really would refuse
    followed = tools.bridge_clear(force=True)
    assert followed.ok, f"the instruction led to a refusal: {followed.error} | {followed.hint}"
    assert followed.data["discarded_id"] == "spawn-2"


# --- The two halves of "unknown", now that the gap is measurable -------------


def _unmeasurable_status(tmp_path, monkeypatch, sample):
    session.reset()
    root = make_project(tmp_path)
    profiles = with_stand(root, tmp_path / "stand")
    tools.project_open(str(root))
    session.set_server_pid(4321, "DayZDiag_x64.exe")
    monkeypatch.setattr("dayz_mcp.tools.bridge.is_alive", lambda pid, image="": True)
    write_state(profiles, tick=9, session_id="w1")
    monkeypatch.setattr(
        "dayz_mcp.bridge.channel.Channel.heartbeat_detail", lambda self, window=3.0: sample
    )
    return tools.bridge_status(window=0.1)


def test_a_gap_too_short_to_conclude_says_so_and_offers_the_window(tmp_path, monkeypatch):
    """A measurement that happened but could not conclude. The window IS the
    problem here, so the answer names the gap, names the publish interval the
    gap has to clear, and points at the one knob that changes it."""
    r = _unmeasurable_status(
        tmp_path, monkeypatch,
        HeartbeatSample(status="unmeasurable", tick=9, session_id="w1", gap=0.30),
    )
    assert not r.ok
    assert r.data["state"] == "unknown"
    assert "0.30s" in r.error
    assert "1s" in r.error or "1.0s" in r.error  # the interval it has to clear
    assert "window" in r.hint


def test_a_lost_second_sample_does_not_blame_the_window(tmp_path, monkeypatch):
    """The other half, and the one that matters: the gap was long enough, so
    the window was never the problem. Telling the caller to enlarge it would be
    the same false diagnosis in miniature -- this is a fact about the state
    file, and the answer says which."""
    r = _unmeasurable_status(
        tmp_path, monkeypatch,
        HeartbeatSample(status="unmeasurable", tick=9, session_id="w1", gap=2.40),
    )
    assert not r.ok
    assert r.data["state"] == "unknown"
    assert "2.40s" in r.error
    assert "second sample could not be read" in r.error
    assert "the window is not the problem" in r.hint
    # It must not send them to enlarge a window that was already big enough.
    assert "bigger window" not in r.hint
    assert "window >=" not in r.hint


def test_no_measurement_at_all_is_still_the_no_snapshot_family(tmp_path, monkeypatch):
    """gap is None only when nothing could be measured at all -- that is not an
    `unknown` about a window, it is "there is nothing readable there", and it
    keeps the answers that say so."""
    session.reset()
    root = make_project(tmp_path)
    with_stand(root, tmp_path / "stand")
    tools.project_open(str(root))
    session.set_server_pid(4321, "DayZDiag_x64.exe")
    monkeypatch.setattr("dayz_mcp.tools.bridge.is_alive", lambda pid, image="": True)
    monkeypatch.setattr(
        "dayz_mcp.bridge.channel.Channel.heartbeat_detail",
        lambda self, window=3.0: HeartbeatSample(
            status="unmeasurable", tick=0, session_id=None, gap=None
        ),
    )

    r = tools.bridge_status(window=0.1)
    assert r.data["state"] == "no_state_file"
    assert r.data["session_id"] is None
