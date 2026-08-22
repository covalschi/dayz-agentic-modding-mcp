"""Exporting a model out of Blender, headless, from a root that was declared.

This is the pipeline's OPTIONAL first half (decision D2). Everything downstream
of it -- binarize, the checks, the packing -- works on a `.p3d` that came from
anywhere. What this adds is that the step before it stops being a hand
operation in a GUI, and that its two silent failures acquire names.

**The export reports success for a partial export, and says nothing at all.**
Measured on a real source file: with the operator's own default arguments it
returned `FINISHED`, Blender exited 0, and out came a valid MLOD of 319,309
bytes carrying **2 of the model's 5 LODs**, with correct texture paths. Not one
line of the 169-line log mentioned it. The one thing that separated that file
from the good one was counting: 5 LOD objects in the source, 2 in the output.

(The other half of that operator's failure list is less dangerous than it
looks: when it finds nothing to export at all it reports an ERROR and returns
`FINISHED`, but an ERROR report makes `bpy.ops` raise for a Python caller, so
that one is loud. Measured: the target file was not touched, byte for byte.)

So the verdict here is read off the ARTIFACT -- is it there, is it an MLOD, was
it written by THIS run, did all of it come out -- exactly as `binarize.py`
reads its own. The same lesson twice, from two unrelated tools, is what makes
it a rule rather than a workaround.

**The stored project root is a live defect, not a hypothesis.** The add-on
keeps a root in Blender's user preferences, and on the machine this was
measured on it pointed at a directory from an unrelated session. A root that
does not contain the model's own prefix folder is not refused by anything: the
add-on strips the drive letter and writes what is left, so the model comes out
naming `\\<some>\\<other>\\path\\texture.paa`, valid-looking and unresolvable.
That is why the root is DECLARED once in the profile and pushed into the
add-on for the duration of the run (decision D1) -- eliminating the trap rather
than detecting it. What the preference held is reported, never used.

Three things about driving Blender itself, each measured on this machine:

* **`--factory-startup` disables the add-on entirely.** With it, the enabled
  add-on list came back with eight entries, none of them the exporter, and
  `dir(bpy.ops.a3ob)` was empty. So the real user preferences have to load.
* **Loading them drags in whatever else the owner has enabled**, and here that
  is two asset-library add-ons that reach the network as they start. Startup
  measured 8.8 s with them against 6.6 s without, and 55 of their modules
  loaded against 1. They are kept out by pointing `BLENDER_USER_SCRIPTS` at an
  empty directory for the run -- which removes them from the search path
  BEFORE they can register, where disabling them from inside the script would
  run after their start-up work is already done. Nothing on disk is touched:
  it is one environment variable, on one child process.
* **Preferences are never written back.** `use_preferences_save` is turned off
  by the driver before it touches anything.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, replace
from pathlib import Path

from ..procs import run_blocking
from .binarize import LogDigest, digest_log
from .checks import (
    PASS,
    PROJECT_ROOT_KEY,
    REFUSE,
    SKIP,
    WARN,
    Finding,
    Report,
    c3_references_stay_inside_the_mod,
    read_artifact,
)
from .p3d import MLOD, Fingerprint, P3dError, fingerprint

#: The driver Blender runs, beside this file. It imports `bpy` and must never
#: be imported by the server -- it is a path here, not a module.
DRIVER = Path(__file__).with_name("blend_driver.py")

BLEND_SUFFIX = ".blend"
MODEL_SUFFIX = ".p3d"

#: Blender's own switches. `-b` is background, and `--python-exit-code` makes a
#: driver that raises leave a non-zero code behind -- recorded, not believed,
#: because the interesting failure exits 0. `--factory-startup` is NOT here and
#: must never be: it takes the add-on away (see the module docstring).
BACKGROUND = "-b"
PYTHON_EXIT_CODE = ("--python-exit-code", "1")

#: The environment variable that decides where Blender looks for legacy
#: add-ons. Pointed at an empty directory it removes the owner's asset-library
#: add-ons from the search path while leaving the EXTENSIONS directory -- where
#: the exporter lives -- exactly where it was.
USER_SCRIPTS_VAR = "BLENDER_USER_SCRIPTS"

#: The export operator's arguments. Fixed, not configurable, and every one of
#: the four is a measured decision:
#:
#: * `visible_only=False` -- LOD collections in a real source file are hidden,
#:   and the default (True) then exports 2 LODs of 5 and reports success.
#: * `relative_paths=True` -- what makes the exported paths prefixed rather
#:   than absolute. Combined with the declared root, this is the whole point.
#: * `validate_lods=False` -- the validator SKIPS the LODs it dislikes, which
#:   is a silent partial export; a LOD nobody exported is caught below by
#:   counting instead.
#: * `lod_collisions='IGNORE'` -- the alternatives are to fail the run or to
#:   silently drop the duplicate. This exports what the modeller made, and the
#:   LOD count then says what came out.
EXPORT_OPTIONS: dict[str, object] = {
    "visible_only": False,
    "relative_paths": True,
    "validate_lods": False,
    "lod_collisions": "IGNORE",
}

#: The ceiling on one export. Generous for the same reason binarize's is: the
#: measurement is a fact about one model on one machine, not a budget.
EXPORT_TIMEOUT = 900.0

#: Boilerplate in Blender's own output, counted in a real export's log rather
#: than guessed. That log ran to 169 lines, of which FOUR said anything: what
#: was exported, the two transformations applied to it, and how long it took.
#: The two big families are the add-on announcing every panel it registers and
#: then every panel it unregisters (50 lines, 30% of the log), and its own
#: per-LOD report (18 lines for each of 5 LODs).
#:
#: Substrings rather than regexes, as in `binarize.NOISE` and
#: `logparse.DEFAULT_NOISE`. Where the bare word would be too greedy the
#: leading TAB is part of the pattern: the exporter indents its own structured
#: report and nothing else in this log is indented, so `\tType: ` cannot
#: swallow a message that merely contains the word.
NOISE: tuple[str, ...] = (
    # The add-on's registration banner, both directions.
    "Registering Arma 3 Object Builder", "Unregistering Arma 3 Object Builder",
    "Register done", "Unregister done", "\tProperties: ", "\tUI: ",
    # This server's own doing: the owner's asset-library add-ons are kept off
    # the search path for the run, and Blender says so twice, every time. Muted
    # rather than hidden -- the digest reports the group and its count.
    "Add-on not loaded:",
    # The exporter's per-LOD report.
    "Preprocessing done in", " LOD objects", "File type: ", "File version: ",
    "Processing LOD data:", "Processing data:", "File report:", ">> Done in",
    "Collected ", "Finalized proxy selection names",
    "\tSignature: ", "\tType: ", "\tVersion: ", "\tVertices: ", "\tNormals: ",
    "\tFaces: ", "\tTaggs: ", "\tLOD ",
    # The operator's own success report, which is precisely the claim this
    # module does not accept: it says the same thing after exporting nothing.
    "Info: Successfully exported",
    # Blender's own startup and shutdown.
    "Read blend:", "Read prefs:", "found bundled python", "Blender quit", " (hash ",
)


@dataclass(frozen=True)
class ExportResult:
    """One export, and what the file it produced turned out to be.

    `ok` is never "Blender exited 0". It means an MLOD came out, this run is
    what wrote it, and its references stay inside the mod.
    """

    ok: bool
    blend: str
    output: str
    root: str
    prefix: str = ""
    code: int | None = None
    seconds: float = 0.0
    size: int = 0
    kind: str = ""
    lod_count: int | None = None
    lods_in_blend: int | None = None
    stored_root: str = ""
    operator_result: tuple[str, ...] = ()
    digest: str = ""
    error: str = ""
    hint: str = ""
    findings: tuple[Finding, ...] = ()
    notes: tuple[str, ...] = ()
    log: LogDigest | None = None

    @property
    def report(self) -> Report:
        return Report(self.findings)

    @property
    def refusals(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.status == REFUSE)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "blend": self.blend,
            "output": self.output,
            "root": self.root,
            "prefix": self.prefix,
            "code": self.code,
            "seconds": round(self.seconds, 2),
            "size": self.size,
            "kind": self.kind,
            "lod_count": self.lod_count,
            "lods_in_blend": self.lods_in_blend,
            "stored_root": self.stored_root,
            "operator_result": list(self.operator_result),
            "digest": self.digest,
            "error": self.error,
            "hint": self.hint,
            "report": self.report.to_dict(),
            "notes": list(self.notes),
            "log": self.log.to_dict() if self.log else None,
        }


def export_command(
    blender_exe: str | os.PathLike[str],
    blend: str | os.PathLike[str],
    payload: str | os.PathLike[str],
    driver: str | os.PathLike[str] = DRIVER,
) -> list[str]:
    """The one invocation this server makes.

    The blend file is opened by Blender itself rather than by the driver, so a
    file it cannot read fails before any of our code runs and says so in its
    own words. Everything after `--` is passed through untouched.
    """
    return [
        str(blender_exe), BACKGROUND, str(blend),
        "--python", str(driver), *PYTHON_EXIT_CODE,
        "--", str(payload),
    ]


def export_environment(scripts_dir: str | os.PathLike[str]) -> dict[str, str]:
    """The environment overlay one run needs, and nothing else.

    An overlay, never a replacement: Blender started without PATH or
    SystemRoot fails in ways that read like a broken installation.
    """
    return {USER_SCRIPTS_VAR: str(scripts_dir)}


# ---------------------------------------------------------------- the verdict


def e1_the_export_is_an_mlod(path: Path) -> Finding:
    """E1 -- a source model is there, is not empty, and begins with `MLOD`.

    The mirror of C1, and refused for the mirror reason: C1 refuses an MLOD
    where a built artifact belongs, and this refuses anything that is not one
    where an export belongs. An `ODOL` in the export's slot means the export
    never happened and a binarized file is sitting where its source should be
    -- which then feeds `binarize` its own output, the crash that leaves a
    zero-length file behind.
    """
    title = "the export is an MLOD source"
    try:
        artifact = read_artifact(path)
    except P3dError as exc:
        return Finding(
            "E1", title, REFUSE, str(exc),
            action="Blender reports success for an export that wrote nothing at all -- read "
                   "the run's log for what the exporter said, and check that the file holds "
                   "objects the add-on recognises as LODs",
        )
    if artifact.info.kind != MLOD:
        return Finding(
            "E1", title, REFUSE,
            f"{path} is {artifact.info.kind}, not an MLOD source "
            f"({artifact.info.size} bytes, {artifact.info.lod_count} LODs)",
            action="the export target must be the MLOD, and the binarized model is what is "
                   "built FROM it -- point the export at a source path, because handing "
                   "binarize its own output crashes it and leaves a zero-length file",
        )
    return Finding(
        "E1", title, PASS,
        f"MLOD v{artifact.info.version}, {artifact.info.size} bytes, "
        f"{artifact.info.lod_count} LODs",
    )


def e2_this_run_wrote_it(path: Path, marker: float | None) -> Finding:
    """E2 -- the file on the disk is the one this run produced.

    The shape this exists for: the operator can return without ever opening
    the output file. The previous export is then still lying there, and every
    check downstream passes on it -- E1, C3, and the whole of C1-C12 once it
    has been binarized. Only its age can tell.

    `marker` is the modification time seen before the run, or None when there
    was no file. The old file is deliberately NOT deleted first, unlike the
    binarizer's output: `binarize` crashing writes a zero-length file OVER the
    target, so there is nothing left to preserve, while an export that writes
    nothing leaves the previous one intact and destroying it would be
    gratuitous.
    """
    title = "this run wrote the file"
    if not path.is_file():
        return Finding("E2", title, REFUSE, f"{path} is not there", action="see E1")
    if marker is None:
        return Finding("E2", title, PASS, "the file did not exist before this run")
    if path.stat().st_mtime > marker:
        return Finding("E2", title, PASS, "the file is newer than it was before this run")
    return Finding(
        "E2", title, REFUSE,
        f"{path} was not touched: it is the file that was already there before the run",
        action="the export reported success and wrote nothing, which is what it does when it "
               "finds no LODs to export -- check that the source file's LOD objects are "
               "declared as LODs, and read the run's log for the exporter's own message. The "
               "file still on the disk is the PREVIOUS export, not this one",
    )


def e3_every_lod_reached_the_file(in_blend: int | None, in_file: int | None) -> Finding:
    """E3 -- as many LODs came out as the source file declares.

    THE check that catches a partial export, because nothing else does.
    Measured with the operator's own defaults on a real source file: 2 LODs of
    5, a valid MLOD, correct texture paths, `FINISHED`, exit 0, and no mention
    of it anywhere in a 169-line log. E1 passed, E2 passed, C3 passed. This
    fired.

    Warns rather than refuses: an object marked as a LOD but not linked into
    the scene counts on one side and not the other, and this cannot tell that
    from a defect. With the arguments this server passes it should not fire at
    all.
    """
    title = "every LOD reached the file"
    if in_blend is None or in_file is None:
        return Finding("E3", title, SKIP, "the LOD count of one side is unknown")
    if in_blend == in_file:
        return Finding("E3", title, PASS, f"{in_file} LOD(s), as many as the source declares")
    return Finding(
        "E3", title, WARN,
        f"the source declares {in_blend} LOD(s) and {in_file} reached the file",
        action="an export that leaves LODs behind still reports success. Check whether the "
               "missing ones are hidden in the source file, or whether the exporter rejected "
               "them -- the run's log carries its own count",
    )


# ----------------------------------------------------------------- the runner


def _refuse(base: ExportResult, error: str, hint: str = "",
            findings: tuple[Finding, ...] = ()) -> ExportResult:
    return replace(base, ok=False, error=error, hint=hint, findings=findings)


def export_p3d(
    blender_exe: str | os.PathLike[str] | None,
    *,
    blend: str | os.PathLike[str] | None,
    output: str | os.PathLike[str] | None,
    root: str | os.PathLike[str] | None,
    work_dir: str | os.PathLike[str],
    log_path: str | os.PathLike[str],
    prefix: str = "",
    timeout: float = EXPORT_TIMEOUT,
    run=run_blocking,
) -> ExportResult:
    """Export one model out of one `.blend`, into the declared project root.

    `root` is pushed into the add-on's preference for the run and nothing else
    decides it -- not what the add-on has stored, not where Blender happens to
    be started. `prefix` is the mod's path prefix; given, the output must lie
    under `<root>/<prefix>`, which is the same containment rule the binarizer
    applies and the same measured failure it closes: one level off, and the
    paths inside the model come out plausible and unresolvable.

    `work_dir` holds the run's two scratch files -- the payload handed to the
    driver and the answer it writes back -- and the empty directory that keeps
    the owner's other add-ons off the search path.

    `run` is the waiter, injected so every refusal above can be exercised on a
    machine with no Blender at all.
    """
    work = Path(work_dir)
    base = ExportResult(
        ok=False,
        blend=str(blend or ""), output=str(output or ""), root=str(root or ""),
        prefix=prefix,
    )

    # ---- everything below this line happens BEFORE a process is started ----

    if not blender_exe or not Path(blender_exe).is_file():
        return _refuse(
            base,
            f"Blender not found at {blender_exe}",
            hint="install Blender, or set machine.blender in dayz-mcp.local.toml to its "
                 "executable. This step is optional: a model already exported to .p3d is "
                 "built without it",
        )

    blend_path = Path(blend) if blend else Path()
    if not blend_path.is_file():
        return _refuse(
            base, f"no such source file: {blend_path}",
            hint=f"point this at the {BLEND_SUFFIX} the model lives in",
        )
    if blend_path.suffix.lower() != BLEND_SUFFIX:
        return _refuse(
            base, f"{blend_path.name} is not a {BLEND_SUFFIX} file",
            hint="this exports FROM Blender; the model it produces is the .p3d",
        )

    root_path = Path(root) if root else Path()
    if not root_path.is_dir():
        return _refuse(
            base,
            f"the project root is not a directory: {root_path}",
            hint=f"declare it as {PROJECT_ROOT_KEY} in the project profile. The add-on "
                 "relativises every texture path against this root, and against the wrong one "
                 "it silently strips the drive letter and writes the rest",
        )
    root_resolved = root_path.resolve()

    out_path = Path(output) if output else Path()
    if out_path.suffix.lower() != MODEL_SUFFIX:
        return _refuse(
            base, f"the export target is not a {MODEL_SUFFIX}: {out_path.name}",
            hint=f"name the file the model should become, ending in {MODEL_SUFFIX}",
        )
    out_resolved = (out_path if out_path.is_absolute() else root_resolved / out_path).resolve()
    try:
        relative = out_resolved.relative_to(root_resolved)
    except ValueError:
        return _refuse(
            base,
            f"the export target {out_resolved} is not inside the project root {root_resolved}",
            hint="every path inside the model is written relative to the root, so a model "
                 "written outside it cannot carry references that resolve",
        )
    if prefix:
        first = relative.parts[0] if relative.parts else ""
        if first.lower() != prefix.lower():
            return _refuse(
                base,
                f"the export target is under {first or '(the root itself)'!r}, not under the "
                f"mod's own {prefix!r} folder, so the root is off by at least one level",
                hint=f"set {PROJECT_ROOT_KEY} to the directory that CONTAINS {prefix!r}. One "
                     f"level too deep is the measured silent failure: every texture path in "
                     f"the model comes out with the drive letter stripped and the rest kept, "
                     f"which is valid-looking and resolves to nothing",
            )

    base = replace(base, output=str(out_resolved), root=str(root_resolved))

    # ---- from here on a process may start ----

    work.mkdir(parents=True, exist_ok=True)
    scripts_dir = work / "no-user-scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    payload_path = work / "export-payload.json"
    answer_path = work / "export-answer.json"
    answer_path.unlink(missing_ok=True)
    out_resolved.parent.mkdir(parents=True, exist_ok=True)
    # Not deleted, deliberately -- see e2_this_run_wrote_it.
    marker = out_resolved.stat().st_mtime if out_resolved.is_file() else None

    payload_path.write_text(json.dumps({
        "root": str(root_resolved),
        "output": str(out_resolved),
        "result": str(answer_path),
        "options": EXPORT_OPTIONS,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    started = time.monotonic()
    code, tail = run(
        export_command(blender_exe, blend_path, payload_path),
        root_resolved, Path(log_path), timeout,
        export_environment(scripts_dir),
    )
    seconds = time.monotonic() - started

    try:
        text = Path(log_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = tail
    outcome = replace(
        base, code=code, seconds=seconds, log=digest_log(text, NOISE),
    )

    if code == 124:
        return _refuse(
            outcome,
            f"Blender did not finish within {int(timeout)} s and was stopped",
            hint="the file on the disk was left alone, so nothing was lost; raise the ceiling "
                 "if the model is genuinely large",
        )

    answer, answer_error = _read_answer(answer_path)
    if answer_error:
        return _refuse(
            outcome, answer_error,
            hint=f"Blender exited {code} without leaving an answer, so the driver did not "
                 f"finish -- read {log_path} for what it said",
        )
    outcome = replace(
        outcome,
        stored_root=str(answer.get("stored_root", "")),
        lods_in_blend=answer.get("lods_in_blend"),
        operator_result=tuple(str(r) for r in answer.get("operator_result", ())),
    )
    notes: list[str] = []
    stored = outcome.stored_root
    if stored and Path(stored).resolve() != root_resolved:
        # Reported, never used. This IS the defect decision D1 removes, and on
        # the machine this was measured on it was live: a root left behind by
        # an unrelated session, which nothing would have complained about.
        notes.append(
            f"the add-on had {stored!r} stored as its project root; the declared "
            f"{PROJECT_ROOT_KEY} was used instead, for this run only"
        )
    size = out_resolved.stat().st_size if out_resolved.is_file() else 0
    if answer.get("error"):
        # Reported without being judged: what is on the disk belongs to some
        # earlier run, and running the checks over it would answer a question
        # about a file this call did not make.
        return _refuse(
            replace(outcome, size=size, notes=tuple(notes)), str(answer["error"]),
            hint="nothing was exported" + (
                f"; the {size}-byte file at {out_resolved.name} is an EARLIER export, left "
                "untouched" if size else ""
            ),
        )

    findings: list[Finding] = [e1_the_export_is_an_mlod(out_resolved)]
    kind = ""
    lod_count: int | None = None
    digest = ""
    if findings[0].status != REFUSE:
        artifact = read_artifact(out_resolved)
        kind, lod_count = artifact.info.kind, artifact.info.lod_count
        digest = _digest_of(artifact)
        findings.append(e2_this_run_wrote_it(out_resolved, marker))
        findings.append(e3_every_lod_reached_the_file(outcome.lods_in_blend, lod_count))
        findings.append(c3_references_stay_inside_the_mod(artifact, prefix))
    outcome = replace(
        outcome, size=size, kind=kind, lod_count=lod_count, digest=digest,
        findings=tuple(findings), notes=tuple(notes),
    )
    refused = outcome.refusals
    if refused:
        return replace(
            outcome, ok=False,
            error="the export was refused by "
                  + ", ".join(f"{f.check} ({f.detail})" for f in refused),
            hint=refused[0].action,
        )
    return replace(outcome, ok=True)


def _digest_of(artifact) -> str:
    """The structural fingerprint's digest, or "".

    Structural rather than a hash of the bytes, because the export is not
    byte-reproducible: three runs on an unchanged source file gave three
    different SHA-256s at a constant 334,032 bytes, the difference being the
    order of one internal block. A content hash would call every re-export a
    change; this answers whether the MODEL changed.
    """
    try:
        return fingerprint(artifact.info).digest
    except (P3dError, ValueError):
        return ""


def _read_answer(path: Path) -> tuple[dict, str]:
    """The driver's answer file, or the reason there isn't one.

    The answer file is what this reads, never the exit code and never the log:
    Blender exits 0 for an export that wrote nothing, and the operator returns
    `FINISHED` after reporting an error to a window that does not exist in
    background mode.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}, f"Blender left no answer at {path}: the driver never finished"
    except (OSError, json.JSONDecodeError) as exc:
        return {}, f"Blender's answer at {path} cannot be read: {exc}"
    if not isinstance(raw, dict):
        return {}, f"Blender's answer at {path} is not an object"
    return raw, ""


def fingerprints_match(a: Fingerprint, b: Fingerprint) -> bool:
    """Whether two exports describe the same model.

    Deliberately the whole structure rather than the digest alone, so a caller
    that wants to say WHY they differ has the parts to hand.
    """
    return (a.kind, a.size, a.lod_count, tuple(a.strings)) == (
        b.kind, b.size, b.lod_count, tuple(b.strings))
