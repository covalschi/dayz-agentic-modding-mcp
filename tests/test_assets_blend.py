"""Exporting a model out of Blender, headless.

Two halves, as the binarizer's tests have. The hermetic half injects the waiter
and needs no Blender at all: it pins the command's shape (the switch that would
disable the add-on is never in it), every refusal that happens BEFORE a process
starts, and -- the point of the module -- that the verdict is read off the file
rather than off what Blender reported.

The corpus half at the bottom runs the real thing. Its samples are named by the
PROPERTY under test and never by the mod they came from:

    DAYZ_MCP_SAMPLE_BLEND         a .blend holding LOD objects, whose material
                                  paths resolve under the root below
    DAYZ_MCP_SAMPLE_BLEND_ROOT    that root -- the directory whose children are
                                  prefix trees
    DAYZ_MCP_SAMPLE_BLEND_PREFIX  the prefix folder inside it
    DAYZ_MCP_SAMPLE_BLEND_REL     the model directory inside that folder

**Unlike every other corpus test here, these WRITE into the root they are
pointed at** -- one file, named for the test, removed afterwards. An export
that writes nothing is not an export, and the alternative (copying the root
first) would break the very thing under test: the paths inside a source file
are absolute, so a copied root is a WRONG root and the export would correctly
be refused. Point these at a scratch copy, not at anything shipped.
"""
from __future__ import annotations

import ast
import hashlib
import os
import struct
from pathlib import Path

import pytest

from dayz_mcp.assets.blend import (
    DRIVER,
    EXPORT_OPTIONS,
    NOISE,
    USER_SCRIPTS_VAR,
    e1_the_export_is_an_mlod,
    e2_this_run_wrote_it,
    e3_every_lod_reached_the_file,
    export_command,
    export_environment,
    export_p3d,
    fingerprints_match,
)
from dayz_mcp.assets.checks import PASS, PROJECT_ROOT_KEY, REFUSE, SKIP, WARN
from dayz_mcp.assets.p3d import fingerprint, parse_p3d, read_p3d
from dayz_mcp.paths import find_blender

PREFIX = "somemod"


# --------------------------------------------------------------- p3d fixtures
# The smallest byte strings the reader accepts, spelled out here rather than
# imported from another test module: a test file that depends on a test file
# breaks the moment either of them moves.


def mlod(lods: int = 5, tail: bytes = b"") -> bytes:
    return b"MLOD" + struct.pack("<II", 0x101, lods) + tail


def odol(lods: int = 4, tail: bytes = b"") -> bytes:
    return b"ODOL" + struct.pack("<II", 55, lods) + b"\x00" * (4 * lods) + tail


def named(*names: str) -> bytes:
    return b"".join(n.encode("ascii") + b"\x00" for n in names)


#: What a correct export names: prefixed, relative, resolvable.
GOOD_MLOD = mlod(tail=named(
    rf"{PREFIX}\data\textures\thing_co.paa",
    rf"{PREFIX}\data\textures\thing.rvmat",
))
#: What an export against the WRONG root names. The add-on does not fail: it
#: strips the drive letter and keeps the rest, so the paths look like paths.
DRIVE_STRIPPED_MLOD = mlod(tail=named(
    r"\work\someone\else\data\textures\thing_co.paa",
    r"c:\work\someone\else\data\textures\thing.rvmat",
))


# ------------------------------------------------------------------- the rig


def a_blender(tmp: Path) -> Path:
    exe = tmp / "blender.exe"
    exe.write_text("stub", encoding="utf-8")
    return exe


def a_project(tmp: Path, *, model: bytes | None = None) -> tuple[Path, Path]:
    """A root with one prefix tree in it, and the model path inside it."""
    root = tmp / "root"
    models = root / PREFIX / "data" / "models"
    models.mkdir(parents=True, exist_ok=True)
    out = models / "thing.p3d"
    if model is not None:
        out.write_bytes(model)
    (tmp / "thing.blend").write_bytes(b"BLENDER-v502stub")
    return root, out


class Waiter:
    """A stand-in for `run_blocking` that records what it was asked to do.

    It writes the driver's answer file and, optionally, the exported model --
    which is how "Blender reported success and wrote nothing" is reproduced
    without Blender.
    """

    def __init__(self, *, answer: dict | None = None, writes: bytes | None = None,
                 code: int = 0, text: str = "", write_answer: bool = True):
        self.answer = {"addon": "x", "operators": ["export_p3d"], "stored_root": "",
                       "root": "", "lods_in_blend": 5, "operator_result": ["FINISHED"],
                       "error": ""} | (answer or {})
        self.writes = writes
        self.code = code
        self.text = text
        self.write_answer = write_answer
        self.calls: list[tuple] = []

    def __call__(self, cmd, cwd, log_path, timeout, env=None):
        import json as _json

        self.calls.append((list(cmd), Path(cwd), Path(log_path), timeout, dict(env or {})))
        payload = _json.loads(Path(cmd[-1]).read_text(encoding="utf-8"))
        self.payload = payload
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        Path(log_path).write_text(self.text, encoding="utf-8")
        if self.write_answer:
            Path(payload["result"]).write_text(
                _json.dumps(self.answer), encoding="utf-8")
        if self.writes is not None:
            out = Path(payload["output"])
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(self.writes)
        return self.code, self.text


def export(tmp: Path, waiter: Waiter, **kw):
    root, out = a_project(tmp, model=kw.pop("model", None))
    return export_p3d(
        kw.pop("blender", None) or a_blender(tmp),
        blend=kw.pop("blend", tmp / "thing.blend"),
        output=kw.pop("output", out),
        root=kw.pop("root", root),
        prefix=kw.pop("prefix", PREFIX),
        work_dir=tmp / "work",
        log_path=tmp / "work" / "blender.log",
        run=waiter,
        **kw,
    )


# ------------------------------------------------------------- the invocation


def test_the_switch_that_disables_the_addon_is_never_passed():
    """`--factory-startup` is the obvious way to make a headless run
    reproducible, and it is the one thing that must never be here: measured, it
    left the add-on list with eight entries, none of them the exporter, and
    `dir(bpy.ops.a3ob)` empty. The export would then fail with an
    AttributeError about an operator that is installed and working."""
    cmd = export_command("blender.exe", "m.blend", "payload.json")
    assert "--factory-startup" not in cmd
    assert cmd[0] == "blender.exe"
    assert "-b" in cmd
    assert cmd[cmd.index("-b") + 1] == "m.blend"
    assert cmd[cmd.index("--python") + 1] == str(DRIVER)
    assert cmd[-2:] == ["--", "payload.json"]


def test_the_owners_other_addons_are_kept_off_the_search_path():
    """One environment variable on one child process, and nothing on disk.

    Measured: pointed at an empty directory it took BlenderKit from 55 loaded
    modules to 1 and Sketchfab to none, while the exporter stayed and startup
    went from 8.8 s to 6.6 s. Disabling them from inside the script cannot do
    this -- by then they have already started.
    """
    env = export_environment(r"X:\empty")
    assert env == {USER_SCRIPTS_VAR: r"X:\empty"}


def test_the_export_arguments_are_the_measured_ones(tmp_path):
    """Four arguments, and `visible_only=False` is the one that matters: the
    default exports only what is visible, and LOD collections in a real source
    file are routinely hidden -- measured at 2 LODs of 5, reported as a
    success."""
    assert EXPORT_OPTIONS["visible_only"] is False
    assert EXPORT_OPTIONS["relative_paths"] is True
    waiter = Waiter(writes=GOOD_MLOD)
    export(tmp_path, waiter)
    assert waiter.payload["options"] == EXPORT_OPTIONS


def test_the_declared_root_is_what_reaches_the_addon(tmp_path):
    """The whole of decision D1 in one assertion: the value handed to the
    driver is the declared root, and nothing consults what the add-on has
    stored."""
    waiter = Waiter(writes=GOOD_MLOD)
    result = export(tmp_path, waiter)
    assert waiter.payload["root"] == str((tmp_path / "root").resolve())
    assert result.ok, result.error


def test_the_run_happens_with_an_empty_scripts_directory_that_exists(tmp_path):
    waiter = Waiter(writes=GOOD_MLOD)
    export(tmp_path, waiter)
    _cmd, _cwd, _log, _timeout, env = waiter.calls[0]
    assert Path(env[USER_SCRIPTS_VAR]).is_dir()
    assert not list(Path(env[USER_SCRIPTS_VAR]).iterdir())


# ------------------------------------------------------------ the driver text
# The driver runs inside Blender, so it cannot be imported here. These read it.


def driver_source() -> str:
    return DRIVER.read_text(encoding="utf-8")


def test_the_driver_is_valid_python():
    ast.parse(driver_source())


def test_the_driver_never_asks_hasattr_about_an_operator():
    """MEASURED: with the add-on absent, `hasattr(bpy.ops.a3ob, "export_p3d")`
    returns **True** -- the operator namespace is lazy and answers for names it
    does not have. In the same run `dir(bpy.ops.a3ob)` was empty and the call
    raised. So the existence test is `dir`, and this pins it: the test is here
    because the wrong spelling is the one that looks right.

    Read as a syntax tree rather than as text, so the module's own prose about
    the trap does not count as committing it.
    """
    tree = ast.parse(driver_source())
    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in ("hasattr", "getattr")
        and node.args
        and "bpy.ops" in ast.unparse(node.args[0])
    ]
    assert not [c for c in calls if c.func.id == "hasattr"], [ast.unparse(c) for c in calls]
    assert "dir(getattr(bpy.ops, NAMESPACE))" in driver_source()


def test_the_driver_turns_off_saving_the_owners_preferences():
    """It sets the project root on the live preference object, which is the
    only thing the add-on reads -- and that is exactly why it must be unable
    to write the file back."""
    source = driver_source()
    assert "use_preferences_save = False" in source
    assert "save_userpref" not in source


# ------------------------------------- refusals, before any process is started


@pytest.mark.parametrize(
    ("kw", "expected"),
    [
        ({"blender": Path("nope") / "blender.exe"}, "Blender not found"),
        ({"blend": Path("nope.blend")}, "no such source file"),
        ({"root": Path("nope")}, "the project root is not a directory"),
    ],
)
def test_a_refusal_happens_before_blender_is_started(tmp_path, kw, expected):
    waiter = Waiter(writes=GOOD_MLOD)
    result = export(tmp_path, waiter, **kw)
    assert not result.ok
    assert expected in result.error
    assert waiter.calls == [], "a process was started for a run that could not work"


def test_a_source_that_is_not_a_blend_is_refused(tmp_path):
    (tmp_path / "thing.p3d").write_bytes(GOOD_MLOD)
    waiter = Waiter(writes=GOOD_MLOD)
    result = export(tmp_path, waiter, blend=tmp_path / "thing.p3d")
    assert not result.ok
    assert "is not a .blend file" in result.error
    assert waiter.calls == []


def test_a_target_that_is_not_a_model_is_refused(tmp_path):
    waiter = Waiter(writes=GOOD_MLOD)
    result = export(tmp_path, waiter, output=tmp_path / "root" / PREFIX / "thing.txt")
    assert not result.ok
    assert "is not a .p3d" in result.error
    assert waiter.calls == []


def test_a_target_outside_the_root_is_refused(tmp_path):
    waiter = Waiter(writes=GOOD_MLOD)
    result = export(tmp_path, waiter, output=tmp_path / "elsewhere" / "thing.p3d")
    assert not result.ok
    assert "is not inside the project root" in result.error
    assert waiter.calls == []


def test_a_root_one_level_too_deep_never_reaches_blender(tmp_path):
    """THE measured silent failure, and the reason the prefix is passed at all.
    Containment alone cannot see it: the target really is inside the root. What
    gives it away is that its first segment is not the mod's own folder."""
    root, out = a_project(tmp_path)
    waiter = Waiter(writes=GOOD_MLOD)
    result = export_p3d(
        a_blender(tmp_path), blend=tmp_path / "thing.blend", output=out,
        root=root / PREFIX, prefix=PREFIX,
        work_dir=tmp_path / "work", log_path=tmp_path / "work" / "b.log", run=waiter,
    )
    assert not result.ok
    assert "off by at least one level" in result.error
    assert PROJECT_ROOT_KEY in result.hint
    assert waiter.calls == []


# -------------------------------------------- the verdict is read off the file


def test_a_success_that_wrote_nothing_is_refused_and_the_old_file_survives(tmp_path):
    """The shape this module exists for. Blender exits 0, the driver reports
    `FINISHED`, and the file on the disk is the PREVIOUS export -- which passes
    E1, passes C3, and would pass all of C1-C12 once binarized."""
    waiter = Waiter(writes=None)
    root, out = a_project(tmp_path, model=GOOD_MLOD)
    before = out.read_bytes()
    result = export_p3d(
        a_blender(tmp_path), blend=tmp_path / "thing.blend", output=out, root=root,
        prefix=PREFIX, work_dir=tmp_path / "work",
        log_path=tmp_path / "work" / "b.log", run=waiter,
    )
    assert result.code == 0
    assert result.operator_result == ("FINISHED",)
    assert not result.ok
    assert "E2" in result.error
    assert out.read_bytes() == before, "the previous export was destroyed"


def test_an_odol_where_the_export_belongs_is_refused(tmp_path):
    """An export target holding a binarized model means the export never
    happened -- and feeding that back to `binarize` is the crash that leaves a
    zero-length file behind."""
    waiter = Waiter(writes=odol())
    result = export(tmp_path, waiter)
    assert not result.ok
    assert "E1" in result.error
    assert "ODOL" in result.error


def test_an_empty_file_is_refused(tmp_path):
    waiter = Waiter(writes=b"")
    result = export(tmp_path, waiter)
    assert not result.ok
    assert "E1" in result.error


def test_drive_stripped_paths_are_refused_by_c3(tmp_path):
    """What an export against the wrong root actually produces. The add-on
    reports nothing: it strips the drive letter and writes what is left."""
    waiter = Waiter(writes=DRIVE_STRIPPED_MLOD)
    result = export(tmp_path, waiter)
    assert not result.ok
    assert "C3" in result.error
    assert PROJECT_ROOT_KEY in result.hint


def test_a_partial_export_warns_and_names_the_counts(tmp_path):
    """MEASURED with the exporter's own default arguments on a real model: a
    valid MLOD of 319,309 bytes with 2 of 5 LODs, correct texture paths,
    `FINISHED`, exit 0, and not one line about it in a 169-line log. E1, E2 and
    C3 all passed. Only counting caught it."""
    waiter = Waiter(writes=mlod(lods=2, tail=named(rf"{PREFIX}\data\textures\t_co.paa")),
                    answer={"lods_in_blend": 5})
    result = export(tmp_path, waiter)
    assert result.ok, result.error
    warned = [f for f in result.findings if f.status == WARN]
    assert [f.check for f in warned] == ["E3"]
    assert "5" in warned[0].detail and "2" in warned[0].detail


def test_an_answer_that_never_arrived_is_not_a_success(tmp_path):
    waiter = Waiter(writes=GOOD_MLOD, write_answer=False)
    result = export(tmp_path, waiter)
    assert not result.ok
    assert "left no answer" in result.error


def test_the_drivers_own_error_is_reported_and_the_file_is_left_alone(tmp_path):
    waiter = Waiter(writes=None, answer={"error": "RuntimeError: nothing to export"})
    root, out = a_project(tmp_path, model=GOOD_MLOD)
    result = export_p3d(
        a_blender(tmp_path), blend=tmp_path / "thing.blend", output=out, root=root,
        prefix=PREFIX, work_dir=tmp_path / "work",
        log_path=tmp_path / "work" / "b.log", run=waiter,
    )
    assert not result.ok
    assert "nothing to export" in result.error
    assert "EARLIER export" in result.hint
    assert result.size == len(GOOD_MLOD)
    assert out.read_bytes() == GOOD_MLOD


def test_the_ceiling_is_reported_as_a_ceiling(tmp_path):
    waiter = Waiter(writes=None, code=124)
    result = export(tmp_path, waiter, timeout=5.0)
    assert not result.ok
    assert "did not finish within 5 s" in result.error


def test_what_the_addon_had_stored_is_reported_and_not_used(tmp_path):
    """The live defect on the machine this was measured on: the add-on's stored
    root pointed at a directory from an unrelated session. It is surfaced so a
    person can fix it, and it decides nothing."""
    waiter = Waiter(writes=GOOD_MLOD, answer={"stored_root": r"E:\somewhere\stale"})
    result = export(tmp_path, waiter)
    assert result.ok, result.error
    assert result.stored_root == r"E:\somewhere\stale"
    assert any("stale" in n for n in result.notes)
    assert waiter.payload["root"] != r"E:\somewhere\stale"


def test_a_matching_stored_root_produces_no_note(tmp_path):
    root = (tmp_path / "root").resolve()
    waiter = Waiter(writes=GOOD_MLOD, answer={"stored_root": str(root)})
    result = export(tmp_path, waiter)
    assert result.ok, result.error
    assert result.notes == ()


# -------------------------------------------------------------- the findings


def test_e2_passes_when_there_was_no_file_before(tmp_path):
    target = tmp_path / "x.p3d"
    target.write_bytes(GOOD_MLOD)
    assert e2_this_run_wrote_it(target, None).status == PASS


def test_e3_skips_rather_than_passing_when_a_count_is_unknown():
    """A pass and a skip are different facts, and only one of them is safe to
    act on: `None` means nobody counted."""
    assert e3_every_lod_reached_the_file(None, 4).status == SKIP
    assert e3_every_lod_reached_the_file(4, None).status == SKIP
    assert e3_every_lod_reached_the_file(4, 4).status == PASS


def test_e1_refuses_a_file_that_is_not_there(tmp_path):
    finding = e1_the_export_is_an_mlod(tmp_path / "never.p3d")
    assert finding.status == REFUSE
    assert finding.action


def test_every_finding_that_fires_says_what_to_do(tmp_path):
    """A finding that fires without an action is the defect this whole phase is
    about: nobody acts on prose."""
    waiter = Waiter(writes=DRIVE_STRIPPED_MLOD, answer={"lods_in_blend": 9})
    result = export(tmp_path, waiter)
    fired = [f for f in result.findings if f.fired]
    assert fired
    assert all(f.action for f in fired), [f.check for f in fired if not f.action]


# ---------------------------------------------------------------- the log


REAL_LOG = "\n".join([
    r'00:01.797  blend            | Read blend: "X:\src\thing.blend"',
    "Registering Arma 3 Object Builder ( 'bl_ext.user_default.Arma3ObjectBuilder' )",
    "\tProperties: object",
    "\tUI: P3D Import / Export",
    "Register done",
    'Add-on not loaded: "somethingelse", cause: No module named \'somethingelse\'',
    r"P3D export to X:\root\somemod\data\models\thing.p3d",
    "\tPreprocessing done in 0.020378 sec",
    "\tDetected 5 LOD objects",
    "\t\tLOD 1: thing_geo",
    "\t\t\tType: Geometry",
    "\t\t\t\tCollected vertices",
    "\t\t\t\tFinalized proxy selection names",
    "\t\t\tFile report:",
    "\t\t\t\tSignature: 10000000000000",
    "\t\t\t\tVertices: 26",
    "\t\t\t>> Done in 0.001 sec",
    "\tSorted LODs",
    "\tForced lowercase",
    "Info: Successfully exported all 5 LODs (check the logs in the system console)",
    "P3D export finished in 0.043711 sec",
    "Unregister done",
    "Blender 5.2.0 LTS (hash fbe6228777e7 built 2026-07-14 01:35:40)",
    "Blender quit",
])


def test_the_noise_list_leaves_only_what_a_person_would_read(tmp_path):
    """Counted on a real export's log: 169 lines in, FOUR kept -- what was
    exported, the two transformations applied, and how long it took."""
    waiter = Waiter(writes=GOOD_MLOD, text=REAL_LOG)
    result = export(tmp_path, waiter)
    assert result.log is not None
    assert [line for line in result.log.kept] == [
        r"P3D export to X:\root\somemod\data\models\thing.p3d",
        "Sorted LODs",
        "Forced lowercase",
        "P3D export finished in 0.043711 sec",
    ]
    assert result.log.dropped == result.log.total - 4


def test_the_exporters_own_success_report_is_muted_and_its_failure_is_not():
    """The claim this module refuses to accept is boilerplate; the one it must
    never lose is not. `Only exported 2/5` and any error line stay."""
    assert any("Info: Successfully exported" in n for n in NOISE)
    for kept in ("Error: There are no LODs to export",
                 "Only exported 2/5 LODs (check the logs in the system console)",
                 "Warning: the material could not be resolved"):
        assert not any(n in kept for n in NOISE), kept


# ---------------------------------------------------------- the fingerprint


def test_two_files_with_the_same_structure_match_whatever_they_are_called():
    """The comparison is of the model, not of the file: the same bytes read
    from two paths are the same model, and that is what a re-export produces."""
    a = fingerprint(parse_p3d(GOOD_MLOD, path="one.p3d"))
    b = fingerprint(parse_p3d(GOOD_MLOD, path="two.p3d"))
    assert fingerprints_match(a, b)
    assert not fingerprints_match(a, fingerprint(parse_p3d(DRIVE_STRIPPED_MLOD, path="x.p3d")))


# ------------------------------------------------------------------- corpus


def _sample(name: str) -> str:
    return os.environ.get(name, "")


BLEND = _sample("DAYZ_MCP_SAMPLE_BLEND")
BLEND_ROOT = _sample("DAYZ_MCP_SAMPLE_BLEND_ROOT")
BLEND_PREFIX = _sample("DAYZ_MCP_SAMPLE_BLEND_PREFIX")
BLEND_REL = _sample("DAYZ_MCP_SAMPLE_BLEND_REL")

needs_blender = pytest.mark.skipif(
    not (BLEND and BLEND_ROOT and BLEND_PREFIX and BLEND_REL and find_blender()),
    reason="needs Blender and a sample .blend with the root its paths resolve against",
)


@needs_blender
def test_a_real_export_produces_an_mlod_the_checks_call_clean(tmp_path):
    target = Path(BLEND_ROOT) / BLEND_PREFIX / BLEND_REL / "corpus-export-check.p3d"
    try:
        result = export_p3d(
            find_blender(), blend=BLEND, output=target, root=BLEND_ROOT,
            prefix=BLEND_PREFIX.lower(),
            work_dir=tmp_path / "work", log_path=tmp_path / "work" / "blender.log",
        )
        assert result.ok, f"{result.error} -- {result.hint}"
        assert result.kind == "MLOD"
        assert result.size > 0
        assert result.report.summary == "clean"
        assert result.lod_count == result.lods_in_blend
    finally:
        target.unlink(missing_ok=True)


@needs_blender
def test_two_real_exports_differ_in_bytes_and_agree_in_structure(tmp_path):
    """The confirmation the fingerprint was built for, on real data rather than
    on a synthetic reordering.

    Measured across seven exports of one unchanged source -- three from an
    earlier session, three from this one, and one made by hand in the GUI
    months earlier -- SEVEN distinct SHA-256s, one constant size, and ONE
    structural fingerprint. Caching a model by content hash is therefore
    impossible; asking whether the MODEL changed is not.
    """
    target = Path(BLEND_ROOT) / BLEND_PREFIX / BLEND_REL / "corpus-export-twice.p3d"
    try:
        first = export_p3d(
            find_blender(), blend=BLEND, output=target, root=BLEND_ROOT,
            prefix=BLEND_PREFIX.lower(),
            work_dir=tmp_path / "one", log_path=tmp_path / "one" / "blender.log",
        )
        assert first.ok, first.error
        one_bytes = target.read_bytes()
        one = fingerprint(read_p3d(target))

        second = export_p3d(
            find_blender(), blend=BLEND, output=target, root=BLEND_ROOT,
            prefix=BLEND_PREFIX.lower(),
            work_dir=tmp_path / "two", log_path=tmp_path / "two" / "blender.log",
        )
        assert second.ok, second.error
        two_bytes = target.read_bytes()
        two = fingerprint(read_p3d(target))
    finally:
        target.unlink(missing_ok=True)

    assert hashlib.sha256(one_bytes).digest() != hashlib.sha256(two_bytes).digest()
    assert len(one_bytes) == len(two_bytes)
    assert fingerprints_match(one, two)
    assert one.digest == two.digest


@needs_blender
def test_a_real_run_keeps_the_owners_blender_preferences_byte_identical(tmp_path):
    """Nothing this server does may outlive its own run. The driver turns off
    preference saving before it touches the project root, and this is the
    assertion that the file on disk really is untouched."""
    prefs = Path(os.environ.get("BLENDER_USER_RESOURCES", "")) / "config" / "userpref.blend"
    if not prefs.is_file():
        pytest.skip("no user preferences file to compare")
    before = hashlib.sha256(prefs.read_bytes()).hexdigest()
    target = Path(BLEND_ROOT) / BLEND_PREFIX / BLEND_REL / "corpus-export-prefs.p3d"
    try:
        result = export_p3d(
            find_blender(), blend=BLEND, output=target, root=BLEND_ROOT,
            prefix=BLEND_PREFIX.lower(),
            work_dir=tmp_path / "work", log_path=tmp_path / "work" / "blender.log",
        )
        assert result.ok, result.error
    finally:
        target.unlink(missing_ok=True)
    assert hashlib.sha256(prefs.read_bytes()).hexdigest() == before


@needs_blender
def test_a_real_run_finds_the_addon_and_names_what_it_had_stored(tmp_path):
    """`dir(bpy.ops.a3ob)` answering for real, through the shipped path."""
    target = Path(BLEND_ROOT) / BLEND_PREFIX / BLEND_REL / "corpus-export-addon.p3d"
    try:
        result = export_p3d(
            find_blender(), blend=BLEND, output=target, root=BLEND_ROOT,
            prefix=BLEND_PREFIX.lower(),
            work_dir=tmp_path / "work", log_path=tmp_path / "work" / "blender.log",
        )
        assert result.ok, f"{result.error} -- {result.hint}"
        answer = (tmp_path / "work" / "export-answer.json").read_text(encoding="utf-8")
    finally:
        target.unlink(missing_ok=True)
    assert "export_p3d" in answer
    assert result.lods_in_blend and result.lods_in_blend > 0


@needs_blender
def test_the_real_log_is_almost_entirely_boilerplate(tmp_path):
    target = Path(BLEND_ROOT) / BLEND_PREFIX / BLEND_REL / "corpus-export-log.p3d"
    try:
        result = export_p3d(
            find_blender(), blend=BLEND, output=target, root=BLEND_ROOT,
            prefix=BLEND_PREFIX.lower(),
            work_dir=tmp_path / "work", log_path=tmp_path / "work" / "blender.log",
        )
        assert result.ok, result.error
    finally:
        target.unlink(missing_ok=True)
    assert result.log is not None
    assert result.log.total > 100
    assert len(result.log.kept) <= 10, result.log.kept
    assert any("P3D export to" in line for line in result.log.kept)
