"""The three asset tools: `asset_build`, `asset_check`, `asset_convert`.

Four properties are what this layer is FOR, and each one is a silent failure
the pipeline produced before it existed:

1. **A build never writes into the mod until the artifact has been judged.**
   `binarize` crashes on an already-binarized model and leaves a ZERO-LENGTH
   file in its output directory -- so an output directory that IS the mod
   destroys the working artifact that was sitting there. Everything is built
   into the job's own directory and copied in afterwards, and a refused build
   leaves the shipped file byte-for-byte untouched.
2. **The root is declared, never assumed.** `binarize` has no project-root
   switch: the root is the working directory. `asset_build` without
   `build.project_root` refuses and names the key, because that declaration is
   what makes a wrong root impossible instead of detectable.
3. **The prefix and both model.cfg copies always reach the checks.** The
   containment rule that catches "the root is one level too deep" only fires
   when the prefix is passed, and C11 falls back to a clock -- which cannot see
   the live defect on this machine, an OLDER shipped copy -- unless it is given
   the second copy to compare against.
4. **Long work returns a job id.** Measured on ONE small model: 75.6, 77.6,
   78.3 and 78.7 seconds across four runs.

Samples are named by the PROPERTY under test and never by the mod they came
from, exactly as the readers', the checks' and the binarizer's corpus tests
are. On a machine with none of them set, every corpus test skips and the
hermetic half still runs -- with no DayZ Tools installed at all.

    DAYZ_MCP_SAMPLE_PROJECT_ROOT   a directory whose children are prefix trees
    DAYZ_MCP_SAMPLE_PREFIX         the prefix folder inside it to build
    DAYZ_MCP_SAMPLE_SOURCE_REL     the model directory, relative to that folder
    DAYZ_MCP_SAMPLE_PNG_GRADED     a PNG whose alpha is graded, for C7

The corpus tests COPY everything they touch. Nothing here writes into the
directory it was pointed at.
"""
from __future__ import annotations

import json
import os
import shutil
import struct
import textwrap
import threading
import time
import zlib
from pathlib import Path

import pytest

from dayz_mcp import server as mcp_server
from dayz_mcp import tools
from dayz_mcp.assets import binarize as binarize_module
from dayz_mcp.assets.checks import PROJECT_ROOT_KEY
from dayz_mcp.assets.p3d import read_p3d
from dayz_mcp.paths import BINARIZE_REL, IMAGETOPAA_REL, find_tools
from dayz_mcp.tools import assets, session

MOD = "MyMod"
PREFIX = MOD.lower()

PROFILE = """
[project]
name = "a-project"

[build]
mods = ["{mod}"]
{root_line}
"""


# --------------------------------------------------------------- p3d fixtures
# The smallest byte strings the reader accepts, spelled out here rather than
# imported from another test module: a test file that depends on a test file
# breaks the moment either of them moves.


def odol(lods: int = 4, tail: bytes = b"") -> bytes:
    return b"ODOL" + struct.pack("<II", 55, lods) + b"\x00" * (4 * lods) + tail


def mlod(lods: int = 5, tail: bytes = b"") -> bytes:
    return b"MLOD" + struct.pack("<II", 0x101, lods) + tail


def named(*names: str) -> bytes:
    return b"".join(n.encode("ascii") + b"\x00" for n in names)


#: What `binarize` leaves in an artifact when it actually resolved the rvmat.
RESOLVED = named(
    "#(ai,64,64,1)fresnel(1,0.7)",
    "#(argb,8,8,3)color(1,1,1,1,dt)",
    r"dz\data\data\env_land_co.paa",
    rf"{PREFIX}\data\textures\thing_nohq.paa",
    rf"{PREFIX}\data\textures\thing_smdi.paa",
)
MATERIAL = named(rf"{PREFIX}\data\textures\thing.rvmat")

#: A build from the declared root: every marker of a resolved material.
GOOD_ODOL = odol(tail=MATERIAL + RESOLVED)
#: A valid ODOL with plausible paths and NO inlined material -- what a run from
#: the wrong working directory produces, with a success exit code. C4 refuses it.
UNRESOLVED_ODOL = odol(tail=MATERIAL)
#: The source. Carries none of the markers, by design.
SOURCE_MLOD = mlod(tail=MATERIAL)

MODEL_CFG = """
class CfgSkeletons
{
    class thing
    {
        isDiscrete = 1;
        skeletonInherit = "";
        skeletonBones[] = { "handle", "" };
    };
};
class CfgModels
{
    class thing
    {
        skeletonName = "thing";
        sections[] = { "camo" };
        class Animations
        {
            class lid_open
            {
                type = "rotation";
                source = "openness";
                selection = "handle";
            };
        };
    };
};
"""

#: The same file with one animation removed: two copies that disagree, which is
#: the only thing C11 can see when the shipped copy is OLDER than the artifact.
MODEL_CFG_SHORTER = MODEL_CFG.replace(
    """            class lid_open
            {
                type = "rotation";
                source = "openness";
                selection = "handle";
            };
""",
    "",
)


# ------------------------------------------------------------- a PNG, by hand


def _chunk(kind: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))


def png_rgba(rows: list[list[tuple[int, int, int, int]]]) -> bytes:
    """An unfiltered 8-bit RGBA PNG carrying exactly these pixels."""
    body = bytearray()
    for row in rows:
        body.append(0)
        body += bytes(v for px in row for v in px)
    out = b"\x89PNG\r\n\x1a\n"
    out += _chunk(b"IHDR", struct.pack(">IIBBBBB", len(rows[0]), len(rows), 8, 6, 0, 0, 0))
    out += _chunk(b"IDAT", zlib.compress(bytes(body)))
    out += _chunk(b"IEND", b"")
    return out


#: Four distinct alpha levels: a gradient, not a mask. DXT1 keeps one bit of it.
GRADED_PNG = png_rgba([[(9, 9, 9, a) for a in (0, 80, 160, 255)]])
DXT1_PAA = b"\x01\xff" + b"\x00" * 60
DXT5_PAA = b"\x05\xff" + b"\x00" * 60


# ------------------------------------------------------------------ a project


def make_project(
    tmp: Path,
    *,
    project_root: str | None = "staging",
    prefix_dir: str = MOD,
    source_rel: str = "data/models",
    exclude: list[str] | None = None,
    model_cfg: str | None = MODEL_CFG,
    shipped_model_cfg: str | None = None,
    source: bytes | None = SOURCE_MLOD,
) -> Path:
    """A repository, its staging root and one model in it.

    `prefix_dir` is the folder name under the staging root, given separately
    from the mod name on purpose: the layout this was measured on spells it
    with the mod's own capitalisation while the paths inside the artifact are
    lowercase, and the match has to survive that.
    """
    root = tmp / "repo"
    (root / MOD).mkdir(parents=True, exist_ok=True)
    (root / MOD / "config.cpp").write_text(
        "class CfgPatches\n{\n    class APatch\n    {\n        units[]={};\n    };\n};\n",
        encoding="utf-8",
    )
    root_line = f'project_root = "{project_root}"' if project_root is not None else ""
    if exclude is not None:
        root_line += "\nexclude = " + json.dumps(exclude)
    (root / "dayz-mcp.toml").write_text(
        textwrap.dedent(PROFILE).format(mod=MOD, root_line=root_line), encoding="utf-8"
    )

    if project_root is not None:
        models = root / project_root / prefix_dir / source_rel
        models.mkdir(parents=True, exist_ok=True)
        if source is not None:
            (models / "thing.p3d").write_bytes(source)
        if model_cfg is not None:
            (models / "model.cfg").write_text(model_cfg, encoding="utf-8")

    shipped = root / MOD / source_rel
    shipped.mkdir(parents=True, exist_ok=True)
    if shipped_model_cfg is not None:
        (shipped / "model.cfg").write_text(shipped_model_cfg, encoding="utf-8")
    return root


def ship_references(root: Path) -> Path:
    """The files the good artifact names, put where the pbo will carry them.

    Without them C5 is right to warn, and a test that wanted a clean verdict
    would be asking for a wrong answer.
    """
    textures = root / MOD / "data" / "textures"
    textures.mkdir(parents=True, exist_ok=True)
    (textures / "thing.rvmat").write_text('class Stage1 {};', encoding="utf-8")
    for name in ("thing_nohq.paa", "thing_smdi.paa"):
        (textures / name).write_bytes(b"\x05\xff")
    return textures


def fake_tools(tmp: Path) -> Path:
    """A DayZ Tools install with the two executables and nothing behind them."""
    tools_root = tmp / "tools"
    for rel in (BINARIZE_REL, IMAGETOPAA_REL):
        exe = tools_root / rel
        exe.parent.mkdir(parents=True, exist_ok=True)
        exe.write_text("stub", encoding="utf-8")
    return tools_root


def open_project(tmp: Path, monkeypatch, *, tools_root: Path | None = None, **kw):
    session.reset()
    root = make_project(tmp, **kw)
    result = tools.project_open(str(root))
    assert result.ok, result.error
    where = fake_tools(tmp) if tools_root is None else tools_root
    monkeypatch.setattr(assets, "session_tools_root", lambda: (str(where) if where else None))
    return root


def waiter(writes: dict[str, bytes], code: int = 0, text: str = "") -> object:
    """A stand-in for `procs.run_blocking` that writes what a run would leave.

    Injected into the REAL `binarize_models`, so every pre-flight refusal and
    the artifact verdict are exercised as they will run -- on a machine with no
    DayZ Tools at all.
    """
    calls: list[dict] = []

    def run(cmd, cwd, log_path, timeout=None):
        calls.append({"cmd": list(cmd), "cwd": Path(cwd), "timeout": timeout})
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        Path(log_path).write_text(text, encoding="utf-8")
        out_dir = Path(cmd[-1])
        out_dir.mkdir(parents=True, exist_ok=True)
        for name, data in writes.items():
            (out_dir / name).write_bytes(data)
        return code, text

    run.calls = calls  # type: ignore[attr-defined]
    return run


def with_waiter(monkeypatch, run) -> list[dict]:
    """Point the tool at the real binarizer, with `run` as its process.

    Returns the list every call's keyword arguments are recorded into, so the
    two arguments this task exists to guarantee -- the root and the prefix --
    are provable at the boundary rather than by inference.
    """
    seen: list[dict] = []
    real = binarize_module.binarize_models

    def spy(exe, **kwargs):
        seen.append(dict(kwargs))
        return real(exe, run=run, **kwargs)

    monkeypatch.setattr(assets, "binarize_models", spy)
    return seen


def run_build(**kw):
    """asset_build, then wait for its job. Returns (answer, job)."""
    answer = assets.asset_build(**kw)
    if not answer.ok:
        return answer, None
    job = session.jobs().wait(answer.data["job_id"], timeout=30)
    return answer, job


# ------------------------------------------------------- refusals before work


def test_asset_build_without_a_project_says_which_call_opens_one():
    session.reset()
    result = assets.asset_build()
    assert not result.ok
    assert "project_open" in result.hint


def test_asset_build_without_a_declared_root_refuses_and_names_the_key(tmp_path, monkeypatch):
    """Decision D1, and the whole reason this refusal exists: `binarize` has no
    project-root switch, so a root nobody declared is whatever directory the
    server happens to sit in. The refusal has to say what to DECLARE -- naming
    the effect ("the paths are wrong") is what left this trap alive for a year.
    """
    open_project(tmp_path, monkeypatch, project_root=None)
    result = assets.asset_build()
    assert not result.ok
    assert PROJECT_ROOT_KEY in result.error or PROJECT_ROOT_KEY in result.hint
    assert PROJECT_ROOT_KEY in result.hint
    # It must say where to put it and what it points AT, not merely name it.
    assert "dayz-mcp.toml" in result.hint
    assert PREFIX in result.hint.lower()


def test_asset_build_refuses_a_mod_it_was_not_told_about(tmp_path, monkeypatch):
    open_project(tmp_path, monkeypatch)
    result = assets.asset_build(mod="NotDeclared")
    assert not result.ok
    assert MOD in result.hint


def test_asset_build_asks_which_mod_when_the_project_declares_several(tmp_path, monkeypatch):
    session.reset()
    root = make_project(tmp_path)
    (root / "Second").mkdir()
    (root / "Second" / "config.cpp").write_text("class CfgPatches{};", encoding="utf-8")
    (root / "dayz-mcp.toml").write_text(
        textwrap.dedent(PROFILE).format(
            mod=MOD, root_line='project_root = "staging"'
        ).replace(f'mods = ["{MOD}"]', f'mods = ["{MOD}", "Second"]'),
        encoding="utf-8",
    )
    assert tools.project_open(str(root)).ok
    monkeypatch.setattr(assets, "session_tools_root", lambda: str(fake_tools(tmp_path)))
    result = assets.asset_build()
    assert not result.ok
    assert "Second" in result.hint and MOD in result.hint


def test_asset_build_refuses_when_the_root_holds_no_folder_for_this_mod(tmp_path, monkeypatch):
    """The containment rule, one step earlier and cheaper: a root that does not
    even contain the mod's prefix folder is off by at least one level, and that
    is the measured silent failure -- a valid, smaller artifact with plausible
    paths and a success code."""
    open_project(tmp_path, monkeypatch, prefix_dir="somethingelse")
    result = assets.asset_build()
    assert not result.ok
    assert PREFIX in result.error
    assert PROJECT_ROOT_KEY in result.hint


def test_asset_build_refuses_without_the_tools(tmp_path, monkeypatch):
    open_project(tmp_path, monkeypatch)
    monkeypatch.setattr(assets, "session_tools_root", lambda: None)
    result = assets.asset_build()
    assert not result.ok
    assert "DayZ Tools" in result.error or "DayZ Tools" in result.hint


def test_asset_build_refuses_when_binarize_is_not_in_the_install(tmp_path, monkeypatch):
    empty = tmp_path / "empty-tools"
    empty.mkdir()
    open_project(tmp_path, monkeypatch, tools_root=empty)
    result = assets.asset_build()
    assert not result.ok
    assert "binarize" in result.error.lower()


def test_asset_build_refuses_a_second_build_while_one_is_in_flight(tmp_path, monkeypatch):
    open_project(tmp_path, monkeypatch)
    busy = session.jobs().create(assets.BUILD_KIND)
    result = assets.asset_build()
    assert not result.ok
    assert busy.id in result.error or busy.id in result.hint
    assert "job_wait" in result.hint


def test_asset_build_finds_the_only_model_directory_by_itself(tmp_path, monkeypatch):
    open_project(tmp_path, monkeypatch)
    with_waiter(monkeypatch, waiter({"thing.p3d": GOOD_ODOL}))
    answer, job = run_build()
    assert answer.ok, answer.error
    assert Path(answer.data["source"]).name == "models"


def test_asset_build_refuses_to_guess_between_two_model_directories(tmp_path, monkeypatch):
    root = open_project(tmp_path, monkeypatch)
    other = root / "staging" / MOD / "data" / "proxies"
    other.mkdir(parents=True)
    (other / "other.p3d").write_bytes(SOURCE_MLOD)
    result = assets.asset_build()
    assert not result.ok
    assert "data/models" in result.hint.replace("\\", "/")
    assert "data/proxies" in result.hint.replace("\\", "/")


def test_asset_build_refuses_when_the_prefix_folder_holds_no_model(tmp_path, monkeypatch):
    open_project(tmp_path, monkeypatch, source=None)
    result = assets.asset_build()
    assert not result.ok
    assert ".p3d" in result.error


def test_asset_build_refuses_a_source_that_climbs_out_of_the_prefix(tmp_path, monkeypatch):
    open_project(tmp_path, monkeypatch)
    result = assets.asset_build(source="../../elsewhere")
    assert not result.ok
    assert PREFIX in result.error or PREFIX in result.hint


# ------------------------------------------------------------------- the job


def test_asset_build_returns_a_job_id_without_waiting_for_the_tool(tmp_path, monkeypatch):
    """78 seconds for ONE small model, measured three times. A blocking call
    would stall the whole server for that long -- which is the phase-1 defect
    this shape exists to prevent."""
    open_project(tmp_path, monkeypatch)
    release = threading.Event()

    def slow(exe, **kwargs):
        release.wait(20)
        return binarize_module.binarize_models(exe, run=waiter({"thing.p3d": GOOD_ODOL}), **kwargs)

    monkeypatch.setattr(assets, "binarize_models", slow)
    started = time.monotonic()
    answer = assets.asset_build()
    elapsed = time.monotonic() - started
    assert answer.ok, answer.error
    assert answer.data["job_id"]
    assert elapsed < 5, elapsed
    release.set()
    session.jobs().wait(answer.data["job_id"], timeout=30)


def test_a_good_build_lands_in_the_mod(tmp_path, monkeypatch):
    root = open_project(tmp_path, monkeypatch)
    with_waiter(monkeypatch, waiter({"thing.p3d": GOOD_ODOL}))
    answer, job = run_build()
    assert answer.ok, answer.error
    assert job.status == "done", job.error
    shipped = root / MOD / "data" / "models" / "thing.p3d"
    assert shipped.read_bytes() == GOOD_ODOL
    assert str(shipped) in job.summary or "thing.p3d" in job.summary


def test_a_refused_build_never_touches_the_shipped_artifact(tmp_path, monkeypatch):
    """The property the whole shape exists for. `binarize` leaves a ZERO-LENGTH
    file in its output directory when it crashes, so an output directory that
    IS the mod destroys the working artifact before anyone can judge it."""
    root = open_project(tmp_path, monkeypatch)
    shipped = root / MOD / "data" / "models" / "thing.p3d"
    shipped.write_bytes(GOOD_ODOL)
    before = shipped.read_bytes()

    with_waiter(monkeypatch, waiter({"thing.p3d": UNRESOLVED_ODOL}))
    answer, job = run_build()
    assert answer.ok, answer.error
    assert job.status == "failed"
    assert "C4" in job.error
    assert shipped.read_bytes() == before


def test_a_crash_that_leaves_a_zero_length_file_never_reaches_the_mod(tmp_path, monkeypatch):
    root = open_project(tmp_path, monkeypatch)
    shipped = root / MOD / "data" / "models" / "thing.p3d"
    shipped.write_bytes(GOOD_ODOL)
    with_waiter(monkeypatch, waiter({"thing.p3d": b""}, code=0, text="Exception Code: 0xC0000005"))
    answer, job = run_build()
    assert job.status == "failed"
    assert shipped.read_bytes() == GOOD_ODOL


def test_the_tool_builds_into_its_own_job_directory(tmp_path, monkeypatch):
    root = open_project(tmp_path, monkeypatch)
    seen = with_waiter(monkeypatch, waiter({"thing.p3d": GOOD_ODOL}))
    run_build()
    output = Path(seen[-1]["output"])
    assert ".dayz-mcp" in output.parts
    assert MOD not in output.relative_to(root).parts


def test_the_prefix_and_the_declared_root_always_reach_the_binarizer(tmp_path, monkeypatch):
    """Without the prefix the containment rule cannot fire, and "the root is one
    level too deep" becomes visible only after the fact, in C4."""
    root = open_project(tmp_path, monkeypatch)
    seen = with_waiter(monkeypatch, waiter({"thing.p3d": GOOD_ODOL}))
    run_build()
    assert seen[-1]["prefix"] == PREFIX
    assert Path(seen[-1]["root"]) == (root / "staging")


def test_both_model_cfg_copies_reach_c11(tmp_path, monkeypatch):
    """C11 without the second copy falls back to comparing clocks -- and the
    live defect on this machine is a shipped copy SIX HOURS OLDER than the
    artifact beside it, which a clock comparison calls fine."""
    root = open_project(tmp_path, monkeypatch, shipped_model_cfg=MODEL_CFG_SHORTER)
    with_waiter(monkeypatch, waiter({"thing.p3d": GOOD_ODOL}))
    answer, job = run_build()
    payload = json.loads(Path(job.artifacts[-1]).read_text(encoding="utf-8"))
    findings = {f["check"]: f for f in payload["models"][0]["report"]["findings"]}
    assert findings["C11"]["status"] == "warn", findings["C11"]
    assert "lid_open" in findings["C11"]["detail"] or "lid_open" in str(findings["C11"]["evidence"])


def test_two_matching_model_cfg_copies_leave_c11_quiet(tmp_path, monkeypatch):
    open_project(tmp_path, monkeypatch, shipped_model_cfg=MODEL_CFG)
    with_waiter(monkeypatch, waiter({"thing.p3d": GOOD_ODOL}))
    answer, job = run_build()
    payload = json.loads(Path(job.artifacts[-1]).read_text(encoding="utf-8"))
    findings = {f["check"]: f for f in payload["models"][0]["report"]["findings"]}
    assert findings["C11"]["status"] == "pass", findings["C11"]


def test_the_job_artifact_holds_the_whole_run(tmp_path, monkeypatch):
    """The answer carries a decision; the artifact carries everything. A report
    that fits in the answer is a report that had to be truncated."""
    open_project(tmp_path, monkeypatch)
    with_waiter(monkeypatch, waiter(
        {"thing.p3d": GOOD_ODOL}, text="vertices of bone a are shared with parent bone b\nreal\n"
    ))
    answer, job = run_build()
    payload = json.loads(Path([a for a in job.artifacts if a.endswith(".json")][0]).read_text("utf-8"))
    assert payload["log"]["total"] == 2
    assert payload["log"]["dropped"] == 1
    assert payload["models"][0]["report"]["findings"]
    assert payload["deployed"]


def test_a_worker_that_raises_still_resolves_its_job(tmp_path, monkeypatch):
    """A thread that dies before resolving its job leaves it "running" forever,
    blocks every later build, and reports its traceback to stderr where the
    calling agent never looks."""
    open_project(tmp_path, monkeypatch)

    def boom(exe, **kwargs):
        raise RuntimeError("the tool exploded")

    monkeypatch.setattr(assets, "binarize_models", boom)
    answer, job = run_build()
    assert answer.ok
    assert job.status == "failed"
    assert "the tool exploded" in job.error


def test_deploy_false_builds_and_judges_without_writing_into_the_mod(tmp_path, monkeypatch):
    root = open_project(tmp_path, monkeypatch)
    with_waiter(monkeypatch, waiter({"thing.p3d": GOOD_ODOL}))
    answer, job = run_build(deploy=False)
    assert job.status == "done", job.error
    assert not (root / MOD / "data" / "models" / "thing.p3d").exists()


# ---------------------------------------------------------------- asset_check


def test_asset_check_needs_neither_the_tools_nor_a_build(tmp_path, monkeypatch):
    """Reading an artifact is not running a tool. A project on a machine with no
    DayZ Tools at all can still ask whether what it ships is sound."""
    root = open_project(tmp_path, monkeypatch)
    monkeypatch.setattr(assets, "session_tools_root", lambda: None)
    (root / MOD / "data" / "models" / "thing.p3d").write_bytes(GOOD_ODOL)
    result = assets.asset_check()
    assert result.ok, result.error
    assert result.data["models"][0]["summary"]


def test_asset_check_refuses_a_broken_artifact_and_says_what_to_do(tmp_path, monkeypatch):
    root = open_project(tmp_path, monkeypatch)
    (root / MOD / "data" / "models" / "thing.p3d").write_bytes(UNRESOLVED_ODOL)
    result = assets.asset_check()
    assert not result.ok
    assert "C4" in result.error
    # A refusal still carries what was measured: nothing has to be asked twice.
    assert result.data["models"]
    assert PROJECT_ROOT_KEY in result.hint


def test_asset_check_refuses_a_source_model_sitting_where_the_artifact_belongs(tmp_path, monkeypatch):
    root = open_project(tmp_path, monkeypatch)
    (root / MOD / "data" / "models" / "thing.p3d").write_bytes(SOURCE_MLOD)
    result = assets.asset_check()
    assert not result.ok
    assert "C1" in result.error


def test_asset_check_says_so_when_the_mod_ships_no_model_at_all(tmp_path, monkeypatch):
    open_project(tmp_path, monkeypatch)
    result = assets.asset_check()
    assert result.ok
    assert result.data["models"] == []
    assert any("p3d" in note for note in result.data["notes"]), result.data["notes"]


def test_asset_check_compares_against_the_copy_under_the_build_root(tmp_path, monkeypatch):
    root = open_project(tmp_path, monkeypatch, shipped_model_cfg=MODEL_CFG_SHORTER)
    (root / MOD / "data" / "models" / "thing.p3d").write_bytes(GOOD_ODOL)
    result = assets.asset_check()
    fired = {f["check"] for f in result.data["models"][0]["findings"]}
    assert "C11" in fired


def test_asset_check_reads_the_texture_beside_the_model(tmp_path, monkeypatch):
    """C7 cannot be answered from the output alone: a legitimately opaque
    texture and one whose transparency was destroyed are identical in DXT1, and
    only the SOURCE separates them."""
    root = open_project(tmp_path, monkeypatch)
    textures = root / MOD / "data" / "textures"
    textures.mkdir(parents=True)
    (textures / "thing_co.png").write_bytes(GRADED_PNG)
    (textures / "thing_co.paa").write_bytes(DXT1_PAA)
    result = assets.asset_check()
    assert result.data["textures"]["checked"] == 1
    assert result.data["textures"]["warnings"]
    assert "_ca" in result.data["textures"]["warnings"][0]["action"]


def test_asset_check_ignores_what_the_packer_will_not_ship(tmp_path, monkeypatch):
    """A model inside a directory `build.exclude` drops is not shipped, so a
    refusal about it is a refusal about a file nobody will ever load."""
    root = open_project(tmp_path, monkeypatch, exclude=[".git", "source"])
    junk = root / MOD / "source"
    junk.mkdir(parents=True)
    (junk / "thing.p3d").write_bytes(UNRESOLVED_ODOL)
    result = assets.asset_check()
    assert result.ok, result.error
    assert result.data["models"] == []


def test_asset_check_answers_c12_from_what_the_last_build_deployed(tmp_path, monkeypatch):
    """The recorded fingerprint is written at deployment, never at build time:
    `binarize`'s own output is not size-reproducible (four runs of one input
    gave three fingerprints), so a build compared against the previous build
    would warn about a change nobody made. Compared against what was DEPLOYED,
    a difference is a real difference -- someone edited or replaced the
    artifact by hand."""
    root = open_project(tmp_path, monkeypatch)
    ship_references(root)
    with_waiter(monkeypatch, waiter({"thing.p3d": GOOD_ODOL}))
    run_build()
    assert assets.asset_check().data["models"][0]["summary"] == "clean"

    shipped = root / MOD / "data" / "models" / "thing.p3d"
    shipped.write_bytes(GOOD_ODOL + named("handEdited"))
    fired = {f["check"] for f in assets.asset_check().data["models"][0]["findings"]}
    assert "C12" in fired


# -------------------------------------------------------------- asset_convert


def test_asset_convert_refuses_an_unsupported_pair_before_starting_anything(tmp_path, monkeypatch):
    root = open_project(tmp_path, monkeypatch)
    (root / "thing.tga").write_bytes(b"x")
    result = assets.asset_convert("thing.tga")
    assert not result.ok
    assert ".png" in result.hint and ".paa" in result.hint


def test_asset_convert_refuses_a_source_that_is_not_there_and_says_where_it_looked(
    tmp_path, monkeypatch
):
    open_project(tmp_path, monkeypatch)
    result = assets.asset_convert("data/textures/absent.png")
    assert not result.ok
    assert "absent.png" in result.error
    assert "staging" in result.hint


def test_asset_convert_puts_the_output_beside_the_source_by_default(tmp_path, monkeypatch):
    root = open_project(tmp_path, monkeypatch)
    (root / MOD / "thing_ca.png").write_bytes(GRADED_PNG)
    monkeypatch.setattr(assets, "convert", _convert_writing(DXT5_PAA))
    result = assets.asset_convert(f"{MOD}/thing_ca.png")
    assert result.ok, result.error
    assert Path(result.data["output"]) == root / MOD / "thing_ca.paa"


def test_asset_convert_warns_before_it_quantises_a_graded_alpha(tmp_path, monkeypatch):
    """The format is chosen by the SOURCE FILE'S NAME, so this is entirely
    avoidable -- and entirely silent. Once the conversion has run the levels are
    gone and the output cannot be repaired, so the warning has to name the
    rename."""
    root = open_project(tmp_path, monkeypatch)
    (root / MOD / "thing_co.png").write_bytes(GRADED_PNG)
    monkeypatch.setattr(assets, "convert", _convert_writing(DXT1_PAA))
    result = assets.asset_convert(f"{MOD}/thing_co.png")
    assert result.ok, result.error
    assert result.data["warnings"]
    assert "_ca" in result.hint
    assert result.data["checks"][0]["check"] == "C7"


def test_asset_convert_is_quiet_when_the_alpha_survives(tmp_path, monkeypatch):
    root = open_project(tmp_path, monkeypatch)
    (root / MOD / "thing_ca.png").write_bytes(GRADED_PNG)
    monkeypatch.setattr(assets, "convert", _convert_writing(DXT5_PAA))
    result = assets.asset_convert(f"{MOD}/thing_ca.png")
    assert result.ok, result.error
    assert not result.data["warnings"]
    assert result.hint == ""


def test_asset_convert_reports_a_tool_that_wrote_nothing(tmp_path, monkeypatch):
    root = open_project(tmp_path, monkeypatch)
    (root / MOD / "thing_co.png").write_bytes(GRADED_PNG)

    def nothing(exe, source, output, log_path, timeout=300):
        from dayz_mcp.assets.paa import ConvertResult
        return ConvertResult(
            ok=False, source=str(source), output=str(output),
            error="ImageToPAA exited 0 and produced no thing_co.paa",
        )

    monkeypatch.setattr(assets, "convert", nothing)
    result = assets.asset_convert(f"{MOD}/thing_co.png")
    assert not result.ok
    assert "produced no" in result.error


def _convert_writing(payload: bytes):
    """A stand-in for `paa.convert` that writes `payload` and reports it."""
    from dayz_mcp.assets.paa import ConvertResult, paa_format

    def run(exe, source, output, log_path, timeout=300):
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        Path(output).write_bytes(payload)
        return ConvertResult(
            ok=True, source=str(source), output=str(output),
            size=len(payload), format=paa_format(payload[:2]),
        )

    return run


# ------------------------------------------------- registration and the words


@pytest.mark.anyio
async def test_the_three_asset_tools_are_registered_with_real_parameters():
    """`functools.wraps` in server.py is what keeps the parameter names on the
    registered tool. Without it FastMCP publishes an opaque args/kwargs schema
    and the driving agent cannot call these at all -- a phase-1 defect that must
    not come back through a new namespace."""
    listed = {tool.name: tool for tool in await mcp_server.mcp.list_tools()}
    for name in ("asset_export", "asset_build", "asset_check", "asset_convert"):
        assert name in listed, name
        assert (listed[name].description or "").strip(), name
    assert "blend" in listed["asset_export"].inputSchema["properties"]
    assert "name" in listed["asset_export"].inputSchema["properties"]
    assert "mod" in listed["asset_build"].inputSchema["properties"]
    assert "source" in listed["asset_build"].inputSchema["properties"]
    assert "deploy" in listed["asset_build"].inputSchema["properties"]
    assert "model" in listed["asset_check"].inputSchema["properties"]
    assert "output" in listed["asset_convert"].inputSchema["properties"]


@pytest.mark.anyio
async def test_the_asset_tool_descriptions_carry_their_contract():
    """These strings are the whole contract the driving agent reads. On this
    project they have rotted repeatedly, so the load-bearing facts are pinned as
    facts -- the phrasing stays free to change."""
    listed = {
        t.name: " ".join((t.description or "").split())
        for t in await mcp_server.mcp.list_tools()
    }

    build = listed["asset_build"]
    # Long work returns a job id, and the measured duration is the only thing
    # telling a caller what timeout job_wait deserves.
    assert "job_id" in build
    assert "job_wait" in build
    assert "78" in build
    # D1, in the description as well as in the refusal: this is what a caller
    # has to declare before anything can be built at all.
    assert PROJECT_ROOT_KEY in build
    # The source is an MLOD and the artifact is the build's output (D3).
    assert "MLOD" in build
    # The rule the whole phase rests on.
    assert "exit code" in build.lower()

    check = listed["asset_check"]
    # The two things that decide whether a caller can use it at all: it needs
    # no build, and it needs no toolchain.
    assert "no build" in check.lower()
    assert "dayz tools" in check.lower()
    assert "C7" in check or "transparency" in check.lower()

    convert = listed["asset_convert"]
    assert "_co" in convert and "_ca" in convert
    assert "png" in convert.lower() and "paa" in convert.lower()

    export = listed["asset_export"]
    # Long work returns a job id here too.
    assert "job_id" in export and "job_wait" in export
    # The root that decides every path inside the model.
    assert PROJECT_ROOT_KEY in export
    # It is the OPTIONAL half, and what it makes is not what the engine loads.
    assert "optional" in export.lower()
    assert "MLOD" in export
    assert "asset_build" in export


@pytest.mark.anyio
async def test_no_asset_description_promises_byte_equality():
    """Measured twice, on both halves of the pipeline: three exports of one
    unchanged source gave three hashes, and FOUR runs of `binarize` on one input
    gave three structural fingerprints. A description that promised a
    reproducible artifact would be teaching the caller to expect something that
    does not happen."""
    listed = {
        t.name: " ".join((t.description or "").split()).lower()
        for t in await mcp_server.mcp.list_tools()
    }
    for name in ("asset_export", "asset_build", "asset_check", "asset_convert"):
        for promise in ("byte-identical", "byte for byte", "identical bytes",
                        "the same bytes", "byte-for-byte"):
            assert promise not in listed[name], (name, promise)
    assert "structural" in listed["asset_check"] or "fingerprint" in listed["asset_check"]


# ------------------------------------------------------------------- the notes


def test_a_root_outside_the_repository_is_announced_where_it_is_read(tmp_path, monkeypatch):
    """A staging area that gathers several mods' prefix trees legitimately sits
    beside the repositories rather than inside one, so this is allowed -- but
    the build then depends on a directory this repository does not own, and
    nothing in it is covered by the repository's history. A note nobody surfaces
    is a note nobody reads."""
    session.reset()
    outside = tmp_path / "staging"
    (outside / MOD / "data" / "models").mkdir(parents=True)
    (outside / MOD / "data" / "models" / "thing.p3d").write_bytes(SOURCE_MLOD)
    root = make_project(tmp_path, project_root=None)
    (root / "dayz-mcp.toml").write_text(
        textwrap.dedent(PROFILE).format(mod=MOD, root_line='project_root = "../staging"'),
        encoding="utf-8",
    )
    opened = tools.project_open(str(root))
    assert opened.ok, opened.error
    assert any(PROJECT_ROOT_KEY in n for n in opened.data["notes"]), opened.data["notes"]

    monkeypatch.setattr(assets, "session_tools_root", lambda: str(fake_tools(tmp_path)))
    with_waiter(monkeypatch, waiter({"thing.p3d": GOOD_ODOL}))
    answer = assets.asset_build()
    assert answer.ok, answer.error
    assert any(PROJECT_ROOT_KEY in n for n in answer.data["notes"]), answer.data["notes"]
    session.jobs().wait(answer.data["job_id"], timeout=30)


# ------------------------------------------------------------------ the corpus

SAMPLE_ROOT = os.environ.get("DAYZ_MCP_SAMPLE_PROJECT_ROOT", "")
SAMPLE_PREFIX = os.environ.get("DAYZ_MCP_SAMPLE_PREFIX", "")
SAMPLE_SOURCE_REL = os.environ.get("DAYZ_MCP_SAMPLE_SOURCE_REL", "")
SAMPLE_PNG = os.environ.get("DAYZ_MCP_SAMPLE_PNG_GRADED", "")

needs_sample = pytest.mark.skipif(
    not (SAMPLE_ROOT and SAMPLE_PREFIX and SAMPLE_SOURCE_REL),
    reason="set DAYZ_MCP_SAMPLE_PROJECT_ROOT, _PREFIX and _SOURCE_REL to run this",
)
needs_tools = pytest.mark.skipif(
    find_tools() is None or not (Path(find_tools() or ".") / BINARIZE_REL).is_file(),
    reason="DayZ Tools with binarize.exe is not installed here",
)


def _live_project(tmp_path: Path, monkeypatch) -> tuple[Path, str, str]:
    """A throwaway repository whose staging root is a COPY of the real one.

    The mod is named after the sample's prefix because the prefix folder, the
    mod's own directory and the paths baked into the model all have to agree --
    which is the very thing being tested.
    """
    session.reset()
    root = tmp_path / "repo"
    mod = SAMPLE_PREFIX
    staging = root / "staging"
    shutil.copytree(Path(SAMPLE_ROOT), staging)
    (root / mod).mkdir(parents=True)
    (root / mod / "config.cpp").write_text("class CfgPatches{};", encoding="utf-8")
    (root / "dayz-mcp.toml").write_text(
        textwrap.dedent(PROFILE).format(mod=mod, root_line='project_root = "staging"'),
        encoding="utf-8",
    )
    assert tools.project_open(str(root)).ok
    monkeypatch.setattr(assets, "session_tools_root", lambda: find_tools())
    return root, mod, SAMPLE_SOURCE_REL


@needs_sample
@needs_tools
def test_a_real_model_is_built_from_its_mlod_and_lands_in_the_mod(tmp_path, monkeypatch):
    """The live acceptance: MLOD in, ODOL out, judged, deployed. About 80 s."""
    root, mod, rel = _live_project(tmp_path, monkeypatch)
    started = time.monotonic()
    answer = assets.asset_build(mod=mod, source=rel)
    assert answer.ok, answer.error
    job = session.jobs().wait(answer.data["job_id"], timeout=600)
    assert job.status == "done", job.error
    elapsed = time.monotonic() - started

    payload = json.loads(
        Path([a for a in job.artifacts if a.endswith(".json")][0]).read_text("utf-8")
    )
    model = payload["models"][0]
    findings = {f["check"]: f for f in model["report"]["findings"]}
    assert findings["C3"]["status"] == "pass", findings["C3"]
    assert findings["C4"]["status"] == "pass", findings["C4"]
    shipped = Path(payload["deployed"][0])
    assert shipped.is_file() and shipped.stat().st_size > 0
    assert read_p3d(shipped).kind == "ODOL"
    print(f"\nlive asset_build: {elapsed:.1f} s, {shipped.stat().st_size} B, "
          f"{model['report']['summary']}")

    # And the check tool agrees with the build, reading only what was deployed.
    verdict = assets.asset_check(mod=mod)
    assert verdict.ok, verdict.error


@needs_sample
def test_asset_check_reads_a_real_mod_without_building_anything(tmp_path, monkeypatch):
    """The staging tree IS a mod-shaped tree: models beside their textures. Read
    as one, every check must answer, and nothing may be written."""
    session.reset()
    root = tmp_path / "repo"
    mod = SAMPLE_PREFIX
    shutil.copytree(Path(SAMPLE_ROOT) / SAMPLE_PREFIX, root / mod)
    (root / mod / "config.cpp").write_text("class CfgPatches{};", encoding="utf-8")
    (root / "dayz-mcp.toml").write_text(
        textwrap.dedent(PROFILE).format(mod=mod, root_line=""), encoding="utf-8"
    )
    assert tools.project_open(str(root)).ok
    monkeypatch.setattr(assets, "session_tools_root", lambda: None)
    result = assets.asset_check(mod=mod)
    print(f"\nlive asset_check: {len(result.data['models'])} model(s), "
          f"{result.data['textures']['checked']} texture(s) checked")
    assert result.data["models"] or result.data["textures"]["checked"]


@pytest.mark.skipif(not SAMPLE_PNG, reason="set DAYZ_MCP_SAMPLE_PNG_GRADED to run this")
@pytest.mark.skipif(
    find_tools() is None or not (Path(find_tools() or ".") / IMAGETOPAA_REL).is_file(),
    reason="DayZ Tools with ImageToPAA.exe is not installed here",
)
def test_a_real_png_converts_and_c7_answers_from_the_source(tmp_path, monkeypatch):
    root = open_project(tmp_path, monkeypatch, tools_root=Path(find_tools()))
    target = root / MOD / "sample_co.png"
    shutil.copyfile(SAMPLE_PNG, target)
    started = time.monotonic()
    result = assets.asset_convert(f"{MOD}/sample_co.png")
    elapsed = time.monotonic() - started
    assert result.ok, result.error
    print(f"\nlive asset_convert: {elapsed:.2f} s -> {result.data['format']}, "
          f"{result.data['size']} B, warnings={len(result.data['warnings'])}")
    assert result.data["format"] in ("DXT1", "DXT5")


# ------------------------------------------------------------------ the export
# The optional first half. Its own module's tests cover the export itself; these
# cover the tool around it -- what it refuses before anything starts, and that
# the two halves of the pipeline cannot run over each other.


class ExportWaiter:
    """A stand-in for `procs.run_blocking` inside the real `export_p3d`."""

    def __init__(self, writes: bytes | None = SOURCE_MLOD, lods: int = 5):
        self.writes = writes
        self.lods = lods
        self.calls: list[list[str]] = []

    def __call__(self, cmd, cwd, log_path, timeout, env=None):
        self.calls.append(list(cmd))
        payload = json.loads(Path(cmd[-1]).read_text(encoding="utf-8"))
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        Path(log_path).write_text("P3D export finished in 0.01 sec\n", encoding="utf-8")
        Path(payload["result"]).write_text(json.dumps({
            "addon": "x", "operators": ["export_p3d"], "stored_root": "",
            "root": payload["root"], "lods_in_blend": self.lods,
            "operator_result": ["FINISHED"], "error": "",
        }), encoding="utf-8")
        if self.writes is not None:
            out = Path(payload["output"])
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(self.writes)
        return 0, ""


def with_blender(monkeypatch, tmp_path, waiter=None, *, present: bool = True):
    """Point the export tool at a stub Blender and an injected waiter."""
    exe = tmp_path / "blender" / "blender.exe"
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_text("stub", encoding="utf-8")
    monkeypatch.setattr(assets, "session_blender", lambda: (str(exe) if present else None))
    if waiter is not None:
        real = assets.export_p3d
        monkeypatch.setattr(
            assets, "export_p3d", lambda exe_, **kw: real(exe_, run=waiter, **kw),
        )
    return exe


def a_blend(tmp_path) -> Path:
    blend = tmp_path / "sources" / "thing.blend"
    blend.parent.mkdir(parents=True, exist_ok=True)
    blend.write_bytes(b"BLENDER-v502stub")
    return blend


def run_export(**kw):
    """asset_export, then wait for its job. Returns (answer, job)."""
    answer = assets.asset_export(**kw)
    if not answer.ok:
        return answer, None
    return answer, session.jobs().wait(answer.data["job_id"], timeout=30)


def test_asset_export_without_a_declared_root_refuses_and_names_the_key(tmp_path, monkeypatch):
    """The same D1 refusal the build gives, because the add-on has the same
    problem `binarize` has: a root it remembers from somewhere else."""
    open_project(tmp_path, monkeypatch, project_root=None)
    with_blender(monkeypatch, tmp_path)
    result = assets.asset_export(blend=str(a_blend(tmp_path)))
    assert not result.ok
    assert PROJECT_ROOT_KEY in result.hint


def test_asset_export_without_blender_says_the_step_is_optional(tmp_path, monkeypatch):
    """A machine with no Blender must still be able to build a mod: the model
    it ships came from somewhere, and this half is the one that can be skipped.
    """
    open_project(tmp_path, monkeypatch)
    with_blender(monkeypatch, tmp_path, present=False)
    result = assets.asset_export(blend=str(a_blend(tmp_path)))
    assert not result.ok
    assert "Blender not found" in result.error
    assert "optional" in result.hint.lower()
    assert "machine.blender" in result.hint


def test_asset_export_refuses_a_source_that_is_not_a_blend(tmp_path, monkeypatch):
    root = open_project(tmp_path, monkeypatch)
    with_blender(monkeypatch, tmp_path)
    model = root / "staging" / MOD / "data" / "models" / "thing.p3d"
    result = assets.asset_export(blend=str(model))
    assert not result.ok
    assert ".blend" in result.error


def test_asset_export_says_where_it_looked_for_a_source_it_could_not_find(tmp_path, monkeypatch):
    open_project(tmp_path, monkeypatch)
    with_blender(monkeypatch, tmp_path)
    result = assets.asset_export(blend="nowhere/thing.blend")
    assert not result.ok
    assert "looked in" in result.hint


def test_a_name_that_is_a_path_is_refused(tmp_path, monkeypatch):
    """`name` says what the file is called; `source` says where it goes. One
    argument that could do both is a second place for the root to be wrong."""
    open_project(tmp_path, monkeypatch)
    with_blender(monkeypatch, tmp_path)
    result = assets.asset_export(blend=str(a_blend(tmp_path)), name="../elsewhere/thing.p3d")
    assert not result.ok
    assert "must be a file name" in result.error


def test_the_export_lands_where_the_build_will_look_for_it(tmp_path, monkeypatch):
    root = open_project(tmp_path, monkeypatch)
    waiter = ExportWaiter()
    with_blender(monkeypatch, tmp_path, waiter)
    answer, job = run_export(blend=str(a_blend(tmp_path)), name="thing.p3d")
    assert answer.ok, answer.error
    assert job.status == "done", job.error
    expected = root / "staging" / MOD / "data" / "models" / "thing.p3d"
    assert Path(answer.data["output"]) == expected
    assert expected.read_bytes() == SOURCE_MLOD
    assert "asset_build" in job.summary


def test_the_default_name_comes_from_the_source_file(tmp_path, monkeypatch):
    open_project(tmp_path, monkeypatch)
    waiter = ExportWaiter()
    with_blender(monkeypatch, tmp_path, waiter)
    answer, job = run_export(blend=str(a_blend(tmp_path)))
    assert answer.ok, answer.error
    assert job.status == "done", job.error
    assert Path(answer.data["output"]).name == "thing.p3d"


def test_an_export_that_wrote_nothing_fails_the_job(tmp_path, monkeypatch):
    """Blender exits 0 and reports FINISHED. The model already on the disk is
    the previous one, and the job must not call that a success."""
    root = open_project(tmp_path, monkeypatch)
    shipped = root / "staging" / MOD / "data" / "models" / "thing.p3d"
    before = shipped.read_bytes()
    waiter = ExportWaiter(writes=None)
    with_blender(monkeypatch, tmp_path, waiter)
    _answer, job = run_export(blend=str(a_blend(tmp_path)), name="thing.p3d")
    assert job.status == "failed"
    assert "E2" in (job.error or "") or "E2" in job.summary
    assert shipped.read_bytes() == before


def test_a_partial_export_is_reported_in_the_summary(tmp_path, monkeypatch):
    """It warns rather than refuses, so the only thing standing between a
    two-LOD model and the mod is that the count reaches the person reading."""
    open_project(tmp_path, monkeypatch)
    waiter = ExportWaiter(writes=mlod(lods=2, tail=MATERIAL), lods=5)
    with_blender(monkeypatch, tmp_path, waiter)
    _answer, job = run_export(blend=str(a_blend(tmp_path)), name="thing.p3d")
    assert job.status == "done", job.error
    assert "E3" in job.summary


def test_an_export_and_a_build_refuse_to_run_over_each_other(tmp_path, monkeypatch):
    """They share a directory, one writing the models the other reads."""
    open_project(tmp_path, monkeypatch)
    with_blender(monkeypatch, tmp_path)
    store = session.jobs()
    job = store.create(assets.EXPORT_KIND)
    store.start(job.id)
    build = assets.asset_build()
    assert not build.ok
    assert assets.EXPORT_KIND in build.error
    assert job.id in build.hint
    store.finish(job.id, 0, summary="done")

    other = store.create(assets.BUILD_KIND)
    store.start(other.id)
    export = assets.asset_export(blend=str(a_blend(tmp_path)))
    assert not export.ok
    assert assets.BUILD_KIND in export.error


def test_the_export_job_keeps_its_log_and_its_answer(tmp_path, monkeypatch):
    open_project(tmp_path, monkeypatch)
    waiter = ExportWaiter()
    with_blender(monkeypatch, tmp_path, waiter)
    answer, job = run_export(blend=str(a_blend(tmp_path)), name="thing.p3d")
    assert job.status == "done", job.error
    artifacts = tools.job_artifacts(answer.data["job_id"])
    assert artifacts.ok
    names = {Path(a).name for a in artifacts.data["artifacts"]}
    assert "blender.log" in names
    assert "asset-export.json" in names
