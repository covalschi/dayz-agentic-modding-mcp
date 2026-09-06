"""mod_build's file-patching link: made after a successful pack, never over a
real folder, and reported in the job summary."""
import sys
import textwrap
import threading
from pathlib import Path

import pytest

from dayz_mcp import tools
from dayz_mcp.packer import PackResult, is_junction
from dayz_mcp.tools import build, session

PROFILE = """
[project]
name = "my-mod"

[build]
mods = ["MyMod"]

[client]
file_patching = true
"""


def make_project(root: Path) -> Path:
    """A project whose `machine.game` resolves to a controlled temp
    directory -- required now that mod_build links the patch junction under
    the GAME directory (spec F6), not `@MyMod`: leaving machine.game unset
    would let `find_game` fall back to auto-discovery and, on a machine with
    a real DayZ install, plant a junction inside it."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "dayz-mcp.toml").write_text(textwrap.dedent(PROFILE), encoding="utf-8")
    (root / "MyMod").mkdir(exist_ok=True)
    (root / "MyMod" / "config.cpp").write_text("class CfgPatches { };\n", encoding="utf-8")
    game = root.parent / "game"
    game.mkdir(exist_ok=True)
    (game / "DayZDiag_x64.exe").write_bytes(b"")
    (root / "dayz-mcp.local.toml").write_text(
        f'[machine]\ngame = "{game.as_posix()}"\n', encoding="utf-8"
    )
    return root


def wait(job_id: str):
    result = tools.job_wait(job_id, timeout=5)
    assert result.ok, result.error
    return result.data


@pytest.mark.skipif(sys.platform != "win32", reason="junctions are a Windows thing")
def test_a_build_with_file_patching_leaves_a_junction_at_the_prefix(tmp_path, monkeypatch):
    root = make_project(tmp_path / "p")
    assert tools.project_open(str(root)).ok
    monkeypatch.setattr(build, "session_tools_root", lambda: str(tmp_path / "tools"))

    def fake_pack_all(names, root_, tools_, log_dir, exclude=None, sources=None, stage=False):
        return [PackResult(n, pbo=str(root / f"@{n}" / "addons" / f"{n}.pbo"), size=10, signed=False) for n in names]

    monkeypatch.setattr(build, "pack_all", fake_pack_all)
    started = tools.mod_build(skip_lint=True)
    assert started.ok, started.error
    job = wait(started.data["job_id"])
    assert job["status"] == "done", job
    assert is_junction(Path(session.game()) / "MyMod")
    assert "linked" in job["summary"]


@pytest.mark.skipif(sys.platform != "win32", reason="junctions are a Windows thing")
def test_a_real_folder_at_the_prefix_fails_the_build_instead_of_being_replaced(tmp_path, monkeypatch):
    root = make_project(tmp_path / "p")
    assert tools.project_open(str(root)).ok
    monkeypatch.setattr(build, "session_tools_root", lambda: str(tmp_path / "tools"))
    monkeypatch.setattr(build, "pack_all", lambda names, *a, **kw: [PackResult(n, pbo="x", size=1, signed=False) for n in names])
    real = Path(session.game()) / "MyMod"
    real.mkdir(parents=True)
    (real / "keep.txt").write_text("keep", encoding="utf-8")
    started = tools.mod_build(skip_lint=True)
    job = wait(started.data["job_id"])
    assert job["status"] == "failed", job
    assert "real folder" in job["error"]
    assert (real / "keep.txt").exists()


@pytest.mark.skipif(sys.platform != "win32", reason="junctions are a Windows thing")
def test_file_patching_without_a_known_game_directory_notes_and_skips_the_link(tmp_path, monkeypatch):
    """session.game() is empty when machine.game does not resolve. mod_build
    must not fail the whole build over a link it has nowhere to place --
    just say so and pack normally."""
    root = make_project(tmp_path / "p")
    assert tools.project_open(str(root)).ok
    monkeypatch.setattr(session, "game", lambda: None)
    monkeypatch.setattr(build, "session_tools_root", lambda: str(tmp_path / "tools"))
    monkeypatch.setattr(build, "pack_all", lambda names, *a, **kw: [PackResult(n, pbo="x", size=1, signed=False) for n in names])

    started = tools.mod_build(skip_lint=True)
    job = wait(started.data["job_id"])

    assert job["status"] == "done", job
    assert "no game directory is known" in job["summary"]
    assert not (root / "@MyMod").exists()


# --- moved here from test_tools.py ---------------------------------------


def test_build_runs_as_a_job_and_reports_packing_results(tmp_path, monkeypatch):
    root = make_project(tmp_path)
    tools.project_open(str(root))
    monkeypatch.setattr(
        "dayz_mcp.tools.build.pack_all",
        lambda names, root, tools_root, log_dir, exclude=None, sources=None, stage=False: [
            PackResult(name="MyMod", pbo=str(root / "@MyMod/addons/MyMod.pbo"), size=10, signed=True)
        ],
    )
    monkeypatch.setattr("dayz_mcp.tools.build.session_tools_root", lambda: "C:/tools")
    job_id = tools.mod_build().data["job_id"]
    waited = tools.job_wait(job_id, timeout=30)
    assert waited.data["status"] == "done"
    assert "MyMod" in waited.data["summary"]


def test_build_fails_the_job_when_packing_reports_an_error(tmp_path, monkeypatch):
    root = make_project(tmp_path)
    tools.project_open(str(root))
    monkeypatch.setattr(
        "dayz_mcp.tools.build.pack_all",
        lambda names, root, tools_root, log_dir, exclude=None, sources=None, stage=False: [PackResult(name="MyMod", error="stale pbo")],
    )
    monkeypatch.setattr("dayz_mcp.tools.build.session_tools_root", lambda: "C:/tools")
    job_id = tools.mod_build().data["job_id"]
    waited = tools.job_wait(job_id, timeout=30)
    assert waited.data["status"] == "failed"
    assert "stale" in waited.data["error"]


def test_mod_build_worker_exception_fails_the_job_instead_of_hanging(tmp_path, monkeypatch):
    root = make_project(tmp_path)
    tools.project_open(str(root))
    monkeypatch.setattr("dayz_mcp.tools.build.session_tools_root", lambda: "C:/tools")

    def boom(names, root, tools_root, log_dir, exclude=None, sources=None, stage=False):
        raise RuntimeError("simulated packer crash")

    monkeypatch.setattr("dayz_mcp.tools.build.pack_all", boom)
    job_id = tools.mod_build().data["job_id"]
    waited = tools.job_wait(job_id, timeout=10)
    assert waited.data["status"] == "failed"
    assert "simulated packer crash" in waited.data["error"]


# --- Final review, item 7: server_start refuses a second server; mod_build
# refused nothing, and two builds share an output directory ---


def test_mod_build_refuses_a_second_build_while_one_is_running(tmp_path, monkeypatch):
    """Two builds of the same project write the same pbo and unlink the same
    .bisign, so the second either loses the race or corrupts the artifact.
    Tools run on worker threads, so an agent firing mod_build twice is not an
    exotic case -- it is one impatient retry."""
    root = make_project(tmp_path)
    tools.project_open(str(root))

    started = threading.Event()
    release = threading.Event()

    def slow_pack_all(names, root, tools_root, log_dir, exclude=None, sources=None, stage=False):
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
    root = make_project(tmp_path)
    tools.project_open(str(root))
    note = "private key present but signer executable not found at C:/tools/Bin/DsUtils/DSSignFile.exe"
    monkeypatch.setattr(
        "dayz_mcp.tools.build.pack_all",
        lambda names, root, tools_root, log_dir, exclude=None, sources=None, stage=False: [
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
