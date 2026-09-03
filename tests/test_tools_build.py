"""mod_build's file-patching link: made after a successful pack, never over a
real folder, and reported in the job summary."""
import sys
import textwrap
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
    root.mkdir(parents=True, exist_ok=True)
    (root / "dayz-mcp.toml").write_text(textwrap.dedent(PROFILE), encoding="utf-8")
    (root / "MyMod").mkdir(exist_ok=True)
    (root / "MyMod" / "config.cpp").write_text("class CfgPatches { };\n", encoding="utf-8")
    return root


def wait(job_id: str):
    result = tools.job_wait(job_id, timeout=5)
    assert result.ok, result.error
    return result.data


@pytest.mark.skipif(sys.platform != "win32", reason="junctions are a Windows thing")
def test_a_build_with_file_patching_leaves_a_junction_at_the_prefix(tmp_path, monkeypatch):
    session.reset()
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
    assert is_junction(root / "@MyMod" / "MyMod")
    assert "linked" in job["summary"]


@pytest.mark.skipif(sys.platform != "win32", reason="junctions are a Windows thing")
def test_a_real_folder_at_the_prefix_fails_the_build_instead_of_being_replaced(tmp_path, monkeypatch):
    session.reset()
    root = make_project(tmp_path / "p")
    assert tools.project_open(str(root)).ok
    monkeypatch.setattr(build, "session_tools_root", lambda: str(tmp_path / "tools"))
    monkeypatch.setattr(build, "pack_all", lambda names, *a, **kw: [PackResult(n, pbo="x", size=1, signed=False) for n in names])
    real = root / "@MyMod" / "MyMod"
    real.mkdir(parents=True)
    (real / "keep.txt").write_text("keep", encoding="utf-8")
    started = tools.mod_build(skip_lint=True)
    job = wait(started.data["job_id"])
    assert job["status"] == "failed", job
    assert "real folder" in job["error"]
    assert (real / "keep.txt").exists()
