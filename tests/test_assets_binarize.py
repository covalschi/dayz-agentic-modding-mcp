"""Running `binarize` from a declared project root, and judging what it made.

Two halves, as the checks' own tests have. The hermetic half injects the waiter
and therefore needs no DayZ Tools at all: it pins the command's shape (`-silent`
is not optional, the working directory is the declared root, the ceiling is
real), every refusal that has to happen BEFORE a process starts, and the fact
that a success code with an empty output directory is a refusal. The corpus
half at the bottom actually runs the tool on a real model.

Samples are named by the PROPERTY under test and never by the mod they came
from, exactly as the readers' and the checks' corpus tests are. On a machine
with none of them set, every corpus test skips and the hermetic half still runs.

    DAYZ_MCP_SAMPLE_PROJECT_ROOT   a directory whose children are prefix trees
    DAYZ_MCP_SAMPLE_PREFIX         the prefix folder inside it to build
    DAYZ_MCP_SAMPLE_SOURCE_REL     the model directory, relative to that folder

The corpus test COPIES the root before building. Nothing here writes into the
directory it was pointed at.
"""
from __future__ import annotations

import os
import shutil
import struct
import sys
import time
from pathlib import Path

import pytest

from dayz_mcp.assets.binarize import (
    ALWAYS,
    BINARIZE_TIMEOUT,
    NOISE,
    SILENT,
    binarize_command,
    binarize_models,
    digest_log,
    find_binpath,
)
from dayz_mcp.assets.checks import PROJECT_ROOT_KEY, REFUSE
from dayz_mcp.paths import BINARIZE_BINPATH_REL, BINARIZE_REL, find_tools
from dayz_mcp.procs import run_blocking, stop

# --------------------------------------------------------------- p3d fixtures
# The smallest byte strings the reader accepts, redefined here rather than
# imported from another test module: a test file that depends on a test file
# breaks the moment either is moved.

PREFIX = "somemod"


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

GOOD_ODOL = odol(tail=MATERIAL + RESOLVED)
#: A valid ODOL with plausible paths and no inlined material -- what a run from
#: the wrong working directory produces, with a success code.
UNRESOLVED_ODOL = odol(tail=MATERIAL)


class Waiter:
    """A stand-in for `run_blocking` that records how it was called.

    Injected so the command's shape and the working directory can be pinned on
    a machine with no DayZ Tools -- and so the three measured failures (a
    success code with no output, a success code with a zero-length file, a
    success code with an unresolved artifact) can be reproduced exactly.
    """

    def __init__(self, code: int = 0, text: str = "", writes: dict[str, bytes] | None = None):
        self.code, self.text, self.writes = code, text, writes or {}
        self.calls: list[dict] = []

    def __call__(self, cmd, cwd, log_path, timeout=None):
        self.calls.append({"cmd": list(cmd), "cwd": Path(cwd), "log": Path(log_path),
                           "timeout": timeout})
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        Path(log_path).write_text(self.text, encoding="utf-8")
        for name, data in self.writes.items():
            target = Path(name)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        return self.code, self.text

    @property
    def called(self) -> bool:
        return bool(self.calls)

    @property
    def cmd(self) -> list[str]:
        return self.calls[-1]["cmd"]


def stand(tmp_path: Path, *, source_name: str = "thing.p3d", source: bytes | None = None):
    """A project root laid out the way `binarize` needs it, plus a fake exe.

    `root/<prefix>/data/models` holds the source, which is the only shape under
    which the relative paths inside the model can resolve from the root.
    """
    root = tmp_path / "root"
    src_dir = root / PREFIX / "data" / "models"
    src_dir.mkdir(parents=True)
    (src_dir / source_name).write_bytes(mlod() if source is None else source)
    out = tmp_path / "out"
    exe = tmp_path / "binarize.exe"
    exe.write_bytes(b"MZ")
    return exe, root, src_dir, out


def run(tmp_path: Path, waiter: Waiter, **kwargs):
    exe = kwargs.pop("exe", None)
    root = kwargs.pop("root", None)
    source = kwargs.pop("source", None)
    output = kwargs.pop("output", None)
    return binarize_models(
        exe, root=root, source=source, output=output,
        log_path=tmp_path / "binarize.log", prefix=kwargs.pop("prefix", PREFIX),
        run=waiter, **kwargs,
    )


# ------------------------------------------------------- refusals before launch


def test_a_missing_binarize_is_refused_without_starting_anything(tmp_path):
    exe, root, src, out = stand(tmp_path)
    exe.unlink()
    waiter = Waiter()
    result = run(tmp_path, waiter, exe=exe, root=root, source=src, output=out)
    assert not result.ok
    assert not waiter.called
    assert "binarize" in result.error.lower()


def test_a_root_that_is_not_a_directory_is_refused(tmp_path):
    """The root is not a hint -- it is the ONLY thing that decides what the
    paths inside the artifact will mean, because binarize has no root flag."""
    exe, root, src, out = stand(tmp_path)
    waiter = Waiter()
    result = run(tmp_path, waiter, exe=exe, root=tmp_path / "nowhere", source=src, output=out)
    assert not result.ok
    assert not waiter.called
    assert PROJECT_ROOT_KEY in result.hint


def test_a_source_file_where_a_directory_belongs_is_refused(tmp_path):
    """Measured: handed a file, binarize exits 0, writes nothing at all and
    prints not one line. Refused here, before that silence can happen."""
    exe, root, src, out = stand(tmp_path)
    waiter = Waiter()
    result = run(tmp_path, waiter, exe=exe, root=root, source=src / "thing.p3d", output=out)
    assert not result.ok
    assert not waiter.called
    assert "directory" in result.error.lower()


def test_a_source_outside_the_root_is_refused(tmp_path):
    """Paths inside the model resolve against the root. A source that is not
    under it cannot produce references that resolve -- and binarize would say
    so nowhere."""
    exe, root, src, out = stand(tmp_path)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "thing.p3d").write_bytes(mlod())
    waiter = Waiter()
    result = run(tmp_path, waiter, exe=exe, root=root, source=outside, output=out)
    assert not result.ok
    assert not waiter.called
    assert PROJECT_ROOT_KEY in result.hint


def test_a_root_one_level_too_deep_is_refused_before_launch(tmp_path):
    """THE measured silent failure, made structural.

    Running from `<root>/<prefix>` instead of `<root>` produced a valid ODOL of
    46,190 bytes instead of 58,644 and a success code. The source still lies
    under that root, so containment alone cannot see it -- what separates them
    is that the source no longer lies under `<root>/<prefix>`.
    """
    exe, root, src, out = stand(tmp_path)
    waiter = Waiter()
    result = run(tmp_path, waiter, exe=exe, root=root / PREFIX, source=src, output=out)
    assert not result.ok
    assert not waiter.called
    assert PREFIX in result.error
    assert PROJECT_ROOT_KEY in result.hint


def test_an_already_binarized_source_is_refused_before_launch(tmp_path):
    """C10. Measured: binarize dies with 0xC0000005 on an ODOL and leaves a
    ZERO-LENGTH file behind, which then replaces a working artifact. The only
    place this refusal helps is before the process starts."""
    exe, root, src, out = stand(tmp_path, source=GOOD_ODOL)
    waiter = Waiter()
    result = run(tmp_path, waiter, exe=exe, root=root, source=src, output=out)
    assert not result.ok
    assert not waiter.called
    assert "C10" in result.error
    assert any(f.check == "C10" and f.status == REFUSE for f in result.refusals)


def test_a_source_directory_with_no_model_is_refused(tmp_path):
    exe, root, src, out = stand(tmp_path)
    (src / "thing.p3d").unlink()
    waiter = Waiter()
    result = run(tmp_path, waiter, exe=exe, root=root, source=src, output=out)
    assert not result.ok
    assert not waiter.called
    assert ".p3d" in result.error


# ------------------------------------------------------------- the command line


def test_silent_is_never_optional(tmp_path):
    """Without it binarize opens a message box and waits for a click, which in
    an unattended job is a hang with no diagnosis."""
    exe, root, src, out = stand(tmp_path)
    waiter = Waiter(writes={str(out / "thing.p3d"): GOOD_ODOL})
    run(tmp_path, waiter, exe=exe, root=root, source=src, output=out)
    assert SILENT in waiter.cmd
    assert SILENT in binarize_command(exe, src, out)


def test_the_timestamp_skip_is_always_overridden(tmp_path):
    """`-always`. Without it binarize decides from timestamps whether to work
    at all, and a skipped build leaves last week's artifact in place looking
    exactly like a fresh one."""
    exe, root, src, out = stand(tmp_path)
    waiter = Waiter(writes={str(out / "thing.p3d"): GOOD_ODOL})
    run(tmp_path, waiter, exe=exe, root=root, source=src, output=out)
    assert ALWAYS in waiter.cmd


def test_the_working_directory_is_the_declared_root(tmp_path):
    """The whole point of the task. binarize has NO project-root option: the
    root is the process's working directory, and the same command from
    somewhere else produces a broken artifact and a success code."""
    exe, root, src, out = stand(tmp_path)
    waiter = Waiter(writes={str(out / "thing.p3d"): GOOD_ODOL})
    run(tmp_path, waiter, exe=exe, root=root, source=src, output=out)
    assert waiter.calls[-1]["cwd"] == root.resolve()


def test_the_waiter_is_given_a_ceiling(tmp_path):
    """Runtime was measured at seconds, at 68 s, and at "not finished after
    120 s". A wait without a ceiling is a job that can never be reclaimed."""
    exe, root, src, out = stand(tmp_path)
    waiter = Waiter(writes={str(out / "thing.p3d"): GOOD_ODOL})
    run(tmp_path, waiter, exe=exe, root=root, source=src, output=out)
    assert waiter.calls[-1]["timeout"] == BINARIZE_TIMEOUT
    assert BINARIZE_TIMEOUT >= 600


def test_binpath_is_passed_only_when_the_main_config_is_really_there(tmp_path):
    """`-binpath=` names the folder that CONTAINS `bin`, not `bin` itself --
    measured, and the difference is the whole effect. Handed a folder with no
    config in it, the switch is worse than useless, so it is omitted."""
    exe, root, src, out = stand(tmp_path)
    empty = tmp_path / "notools"
    empty.mkdir()
    assert find_binpath(empty) == ""

    with_config = tmp_path / "tools"
    (with_config / BINARIZE_BINPATH_REL / "bin").mkdir(parents=True)
    (with_config / BINARIZE_BINPATH_REL / "bin" / "config.cpp").write_text("", encoding="utf-8")
    found = find_binpath(with_config)
    assert found and Path(found).name == Path(BINARIZE_BINPATH_REL).name
    assert not found.endswith("bin")

    plain = binarize_command(exe, src, out)
    assert not any(a.startswith("-binpath") for a in plain)
    with_flag = binarize_command(exe, src, out, binpath=found)
    assert f"-binpath={found}" in with_flag


# ------------------------------------------------ judging the artifact, not the report


def test_a_success_code_with_an_empty_output_directory_is_a_refusal(tmp_path):
    """The first of the three measured lies: exit 0, nothing written, no text."""
    exe, root, src, out = stand(tmp_path)
    waiter = Waiter(code=0)
    result = run(tmp_path, waiter, exe=exe, root=root, source=src, output=out)
    assert waiter.called
    assert not result.ok
    assert "thing.p3d" in result.error


def test_a_success_code_with_a_zero_length_artifact_is_a_refusal(tmp_path):
    """The second: the file binarize leaves behind when it crashes."""
    exe, root, src, out = stand(tmp_path)
    waiter = Waiter(code=0, writes={str(out / "thing.p3d"): b""})
    result = run(tmp_path, waiter, exe=exe, root=root, source=src, output=out)
    assert not result.ok
    assert any(f.check == "C1" for f in result.refusals)


def test_a_success_code_with_an_unresolved_material_is_a_refusal(tmp_path):
    """The third, and the expensive one: a valid ODOL with plausible texture
    paths that the engine renders untextured. Only the inlined-material strings
    separate it from a correct build."""
    exe, root, src, out = stand(tmp_path)
    waiter = Waiter(code=0, writes={str(out / "thing.p3d"): UNRESOLVED_ODOL})
    result = run(tmp_path, waiter, exe=exe, root=root, source=src, output=out)
    assert not result.ok
    assert any(f.check == "C4" for f in result.refusals)


def test_a_source_left_in_the_output_directory_is_a_refusal(tmp_path):
    """An MLOD where the build belongs. The engine does not load one, and does
    not say so either -- the item is simply invisible."""
    exe, root, src, out = stand(tmp_path)
    waiter = Waiter(code=0, writes={str(out / "thing.p3d"): mlod()})
    result = run(tmp_path, waiter, exe=exe, root=root, source=src, output=out)
    assert not result.ok
    assert any(f.check == "C1" for f in result.refusals)


def test_a_stale_artifact_cannot_be_counted_as_this_run(tmp_path):
    """Left in place, last run's good artifact makes a run that produced
    nothing at all look like a success -- every check would pass on it."""
    exe, root, src, out = stand(tmp_path)
    out.mkdir(parents=True)
    (out / "thing.p3d").write_bytes(GOOD_ODOL)
    waiter = Waiter(code=0)
    result = run(tmp_path, waiter, exe=exe, root=root, source=src, output=out)
    assert not result.ok
    assert not (out / "thing.p3d").exists()


def test_a_good_artifact_is_accepted_and_measured(tmp_path):
    exe, root, src, out = stand(tmp_path)
    waiter = Waiter(code=0, writes={str(out / "thing.p3d"): GOOD_ODOL})
    result = run(tmp_path, waiter, exe=exe, root=root, source=src, output=out)
    assert result.ok, result.error
    assert result.models == (str((out / "thing.p3d").resolve()),)
    assert result.builds[0].size == len(GOOD_ODOL)
    assert result.seconds >= 0
    assert result.code == 0


def test_a_timeout_is_a_refusal_that_names_the_ceiling(tmp_path):
    """124 is `run_blocking`'s own code for "the ceiling expired and the tree
    was killed". Whatever is in the output directory was written by a process
    that did not finish."""
    exe, root, src, out = stand(tmp_path)
    waiter = Waiter(code=124, writes={str(out / "thing.p3d"): GOOD_ODOL})
    result = run(tmp_path, waiter, exe=exe, root=root, source=src, output=out)
    assert not result.ok
    assert str(int(BINARIZE_TIMEOUT)) in result.error


def test_a_nonzero_code_with_a_good_artifact_is_reported_but_not_believed(tmp_path):
    """The phase's rule applied in both directions: the tool's report is not
    evidence that a build failed any more than it is evidence that one worked.
    A real argument error exits 2 AND produces nothing, so the artifact still
    decides -- but the code is never swallowed."""
    exe, root, src, out = stand(tmp_path)
    waiter = Waiter(code=3, writes={str(out / "thing.p3d"): GOOD_ODOL})
    result = run(tmp_path, waiter, exe=exe, root=root, source=src, output=out)
    assert result.ok, result.error
    assert result.code == 3
    assert any("3" in n for n in result.notes)


def test_the_caller_can_own_the_verdict_and_it_is_the_only_one(tmp_path):
    """One verdict per model. The default asks what this module can answer on
    its own -- references against the BUILD root -- and a caller that knows
    which directory ends up in the pbo, what the packer drops and where the
    other model.cfg lives asks a strictly better question. Judging twice would
    produce two answers, and a build refused by one pass and allowed by the
    other is not a decision."""
    from dayz_mcp.assets.checks import Finding, Report

    exe, root, src, out = stand(tmp_path)
    waiter = Waiter(writes={str(out / "thing.p3d"): GOOD_ODOL})
    asked: list[tuple[str, str]] = []

    def judge(built, source):
        asked.append((Path(built).name, Path(source).name))
        return Report((Finding("CX", "the caller's own question", REFUSE, "not shippable",
                               action="do this instead"),))

    result = run(tmp_path, waiter, exe=exe, root=root, source=src, output=out, judge=judge)
    assert asked == [("thing.p3d", "thing.p3d")]
    assert not result.ok
    assert [f.check for f in result.refusals] == ["CX"]
    assert result.hint == "do this instead"


def test_without_a_judge_the_default_still_answers(tmp_path):
    """The seam must not become a way to build with no verdict at all."""
    exe, root, src, out = stand(tmp_path)
    waiter = Waiter(writes={str(out / "thing.p3d"): UNRESOLVED_ODOL})
    result = run(tmp_path, waiter, exe=exe, root=root, source=src, output=out)
    assert not result.ok
    assert [f.check for f in result.refusals] == ["C4"]


# ------------------------------------------------------------------- the log noise


def test_the_noise_list_mutes_the_measured_families_and_says_how_many():
    """Measured on one small jar: 73 bone lines (one per bone per LOD), 19
    config lookups and 6 of their companions, out of 112. A log that is 84 per
    cent boilerplate is a log nobody reads."""
    lines = [
        "FileServer MaxCacheSize = 48559 MB",
        "No entry '.CfgVehicles'.",
        "Trying to access error value.",
        *[f"Warning: x.p3d:1, vertices of bone lep_{i:02d} are shared with parent bone root"
          for i in range(1, 11)],
        "Warning: CfgWeapons missing in PreloadConfig - may slow down vehicle creation",
        "UV mapping too varied on somemod\\data\\textures\\thing_ca.paa, stage 0: texel size 0.1",
    ]
    digest = digest_log("\n".join(lines))
    assert digest.total == len(lines)
    assert digest.dropped == len(lines) - 1
    assert digest.kept == ("UV mapping too varied on somemod\\data\\textures\\thing_ca.paa, "
                           "stage 0: texel size 0.1",)
    # What was muted stays visible as a count. A filter nobody can audit is how
    # a real complaint disappears for a year.
    assert sum(count for _, count in digest.muted) == digest.dropped
    assert any("bone" in group for group, _ in digest.muted)


def test_the_noise_list_never_swallows_a_crash():
    """0xC0000005 is the measured signature of an ODOL fed back in. It must
    survive every filter -- and it is the one line that explains a zero-length
    artifact."""
    digest = digest_log("No entry '.CfgVehicles'.\nException Code: 0xC0000005\n")
    assert any("0xC0000005" in line for line in digest.kept)


def test_the_noise_list_is_made_of_measured_substrings():
    """Not regexes, and not guesses: each entry was counted in a real log."""
    assert "are shared with parent bone" in NOISE
    assert any("No entry" in n for n in NOISE)


def test_the_log_digest_is_attached_to_the_result(tmp_path):
    exe, root, src, out = stand(tmp_path)
    waiter = Waiter(code=0, text="No entry '.CfgVehicles'.\nConvert model a -> b\n",
                    writes={str(out / "thing.p3d"): GOOD_ODOL})
    result = run(tmp_path, waiter, exe=exe, root=root, source=src, output=out)
    assert result.log is not None
    assert result.log.dropped == 1


# ------------------------------------- the wait is on the process, not on the stream


def test_the_waiter_returns_when_the_child_exits_even_with_a_grandchild_holding_the_pipe(tmp_path):
    """binarize spawns a FileServer grandchild that inherits the output handle
    and outlives it. Waiting for end-of-stream therefore waits for the
    GRANDCHILD, which is how an unattended job hangs forever. `run_blocking`
    waits on the process handle, and this proves it rather than asserting it.

    The grandchild is killed afterwards -- while it lives it holds the log file
    open, which on Windows is itself a demonstration that the handle really was
    inherited.
    """
    with_grandchild = (
        "import subprocess, sys;"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(20)'],"
        " stdout=sys.stdout, stderr=sys.stdout);"
        "print('grandchild', p.pid);"
        "print('child done')"
    )
    log = tmp_path / "run.log"
    started = time.monotonic()
    code, text = run_blocking([sys.executable, "-c", with_grandchild], tmp_path, log, 60)
    elapsed = time.monotonic() - started
    try:
        assert code == 0
        assert "child done" in text
        assert elapsed < 15, f"waited {elapsed:.1f}s -- that is the grandchild, not the child"
    finally:
        for line in text.splitlines():
            if line.startswith("grandchild "):
                stop(int(line.split()[1]))


# --------------------------------------------------------------------- the corpus

SAMPLE_ROOT = os.environ.get("DAYZ_MCP_SAMPLE_PROJECT_ROOT", "")
SAMPLE_PREFIX = os.environ.get("DAYZ_MCP_SAMPLE_PREFIX", "")
SAMPLE_SOURCE_REL = os.environ.get("DAYZ_MCP_SAMPLE_SOURCE_REL", "")
TOOLS = find_tools()

live = pytest.mark.skipif(
    not (SAMPLE_ROOT and SAMPLE_PREFIX and SAMPLE_SOURCE_REL and TOOLS),
    reason="needs DAYZ_MCP_SAMPLE_PROJECT_ROOT / _PREFIX / _SOURCE_REL and DayZ Tools",
)


@live
def test_binarize_really_builds_a_working_model_from_the_declared_root(tmp_path):
    """The acceptance, on the real tool and a real model.

    The root is COPIED first: this test must never write into the directory it
    was pointed at. Everything it asserts is read off the artifact -- the exit
    code is recorded and believed about nothing.
    """
    root = tmp_path / "root"
    shutil.copytree(Path(SAMPLE_ROOT) / SAMPLE_PREFIX, root / SAMPLE_PREFIX)
    source = root / SAMPLE_PREFIX / SAMPLE_SOURCE_REL
    result = binarize_models(
        Path(TOOLS) / BINARIZE_REL,
        root=root, source=source, output=tmp_path / "out",
        log_path=tmp_path / "binarize.log", prefix=SAMPLE_PREFIX.lower(),
        binpath=find_binpath(TOOLS),
    )
    assert result.ok, f"{result.error}\n{result.hint}"
    assert result.models
    build = result.builds[0]
    assert build.size > 1000
    assert Path(build.output).open("rb").read(4) == b"ODOL"
    # The two refusing checks that only a correct working directory can pass.
    statuses = {f.check: f.status for f in build.report.findings}
    assert statuses["C3"] == "pass", build.report.to_dict()
    assert statuses["C4"] == "pass", build.report.to_dict()
    assert result.seconds > 0
    assert result.log is not None and result.log.dropped > 0


@live
def test_the_same_model_from_a_root_one_level_too_deep_never_starts(tmp_path):
    """The counterpart, on the real layout: the mistake that produced a broken
    artifact with a success code cannot reach the tool at all."""
    root = tmp_path / "root"
    shutil.copytree(Path(SAMPLE_ROOT) / SAMPLE_PREFIX, root / SAMPLE_PREFIX)
    result = binarize_models(
        Path(TOOLS) / BINARIZE_REL,
        root=root / SAMPLE_PREFIX,
        source=root / SAMPLE_PREFIX / SAMPLE_SOURCE_REL,
        output=tmp_path / "out", log_path=tmp_path / "binarize.log",
        prefix=SAMPLE_PREFIX.lower(),
    )
    assert not result.ok
    assert result.code is None, "nothing may have been started"
    assert not (tmp_path / "out").exists() or not list((tmp_path / "out").glob("*.p3d"))
