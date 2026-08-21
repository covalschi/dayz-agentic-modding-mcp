"""The model and texture pipeline as three tools: build, check, convert.

Everything about their shape follows from four properties, and each one is a
way this pipeline lied to the person running it before the server owned it.

**Nothing is written into the mod until the artifact has been judged.**
`binarize` crashes on an already-binarized model with `0xC0000005` and leaves a
ZERO-LENGTH file in its output directory -- on top of whatever was there. An
output directory that IS the mod therefore destroys the working artifact before
anyone can look at it. So every build goes into the job's own directory, the
checks read what came out, and only a clean verdict is copied in. A refused
build leaves the shipped file untouched.

**The root is declared, never assumed.** `binarize` has no project-root switch
at all: the root is the working directory of the process. The same command,
the same input, a different directory -- and out comes a valid ODOL with
plausible texture paths that the engine renders untextured, exit code 0, not
one line of complaint. `build.project_root` is that directory, stated once in
the profile, which is what makes a wrong root impossible instead of merely
detectable (decision D1). Every refusal caused by it says so.

**The prefix and both model.cfg copies always reach the checks.** The rule that
catches "the root is one level too deep" compares the source's first path
segment against the mod's prefix, so it only fires when the prefix is passed --
and it is passed on every build here. C11 without the second copy of the
model.cfg falls back to comparing clocks, which cannot see the live defect it
exists for: a shipped copy SIX HOURS OLDER than the artifact beside it.

**Long work returns a job id.** One small model measured 75.6, 77.6, 78.3 and
78.7 seconds across four runs. A blocking call would stall the whole server for
that long -- protocol ping and cancellation included, not just that one call.

The half these tools do NOT own is the visual verdict: whether the model looks
right, is scaled right, is wound right, has a collision. Nothing outside the
game answers that. C1-C12 shorten the road to it; they do not replace it.
"""
from __future__ import annotations

import json
import shutil
import threading
from dataclasses import asdict
from pathlib import Path, PurePosixPath

from ..assets.binarize import binarize_models, find_binpath
from ..assets.checks import (
    PROJECT_ROOT_KEY,
    Finding,
    check_model,
    check_texture,
)
from ..assets.p3d import Fingerprint, P3dError, fingerprint, read_p3d
from ..assets.paa import CONVERTIBLE, DXT1, PaaError, alpha_levels, convert, expected_format
from ..errors import Result, fail, ok
from ..jobs import QUEUED, RUNNING
from ..packer import ALWAYS_OMIT_FROM_STAGING, name_matches
from ..paths import BINARIZE_REL, IMAGETOPAA_REL
from ..profile import resolve_mod_dir, resolve_project_root
from . import session
from .project import require_project

#: The job kind, so a build in flight can be recognised without guessing.
BUILD_KIND = "asset-build"

#: Where a successful deployment records what it put in the mod. Structural,
#: never a content hash: `binarize`'s own output is not reproducible -- four
#: runs of one unchanged input gave three different fingerprints -- so a hash
#: would call every rebuild a change. Recorded at DEPLOYMENT rather than at
#: build time for the same reason: compared against the last build, a rebuild
#: would warn about a difference nobody made; compared against what was
#: deployed, a difference means the shipped file was edited or replaced.
RECORD_NAME = "assets.json"

#: How many models an answer will describe before it stops being an answer.
MAX_MODELS = 50

MODEL_SUFFIX = ".p3d"
TEXTURE_SUFFIX = ".paa"
SOURCE_SUFFIX = ".png"
MODEL_CFG = "model.cfg"


def session_tools_root() -> str | None:
    """Indirection on purpose: it is what lets these tools be exercised on a
    machine with no DayZ Tools installed."""
    return session.tools_root()


# ------------------------------------------------------------------- the parts


def _choose_mod(prof, mod: str) -> tuple[str, Result | None]:
    """Which declared mod this call is about."""
    names = list(prof.build.mods)
    wanted = (mod or "").strip()
    if wanted:
        for name in names:
            if name.lower() == wanted.lower():
                return name, None
        return "", fail(
            f"{wanted!r} is not a mod this project declares",
            hint="build.mods declares " + ", ".join(repr(n) for n in names)
                 + " -- pass one of those as mod=, or add this one to the profile",
        )
    if len(names) == 1:
        return names[0], None
    return "", fail(
        f"this project declares {len(names)} mods, so the one to work on has to be named",
        hint="pass mod=<name>, one of " + ", ".join(repr(n) for n in names),
    )


def _prefix_of(mod: str) -> str:
    """The path prefix of a mod, which is its name.

    Lowercased because that is how it lies inside a built artifact, and every
    comparison against it -- C3's, C5's, the containment rule's -- is
    case-insensitive, so the folder on the disk may be spelled either way.
    """
    return mod.lower()


def _no_root(prefix: str) -> Result:
    """The refusal decision D1 exists for. It names what to DECLARE."""
    return fail(
        f"this project declares no model root: {PROJECT_ROOT_KEY} is empty",
        hint=f'add it to dayz-mcp.toml under [build] -- {PROJECT_ROOT_KEY} = "<path relative '
             f'to that file>" -- pointing at the directory that CONTAINS the {prefix!r} folder. '
             f"binarize has no project-root switch: the root is whatever working directory it "
             f"was started in, and started in the wrong one it writes a valid artifact with "
             f"plausible texture paths that the engine renders untextured, exits 0 and prints "
             f"nothing. Declaring it once is what makes that impossible rather than detectable",
    )


def _prefix_dir(root: Path, prefix: str) -> Path | None:
    """The mod's own folder under the project root, matched case-insensitively.

    The layout this was measured on spells the folder with the mod's own
    capitalisation while the paths baked into the artifact are lowercase; DayZ
    paths are case-insensitive and so is this.
    """
    exact = root / prefix
    if exact.is_dir():
        return exact
    if not root.is_dir():
        return None
    for child in sorted(root.iterdir()):
        if child.is_dir() and child.name.lower() == prefix:
            return child
    return None


def _model_dirs(base: Path) -> list[Path]:
    """Every directory under `base` that holds at least one model."""
    return sorted({p.parent for p in base.rglob(f"*{MODEL_SUFFIX}") if p.is_file()})


def _within(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
    except ValueError:
        return False
    return True


def _shipped(mod_dir: Path, exclude: list[str], suffix: str) -> list[Path]:
    """Every file of one suffix the packer will actually put in the pbo.

    Excluded directories are not descended into, exactly as `find_excluded`
    does not descend into them: a model inside one is not shipped, so a
    refusal about it would be a refusal about a file nobody will ever load.
    """
    found: list[Path] = []
    stack = [mod_dir]
    while stack:
        current = stack.pop()
        if not current.is_dir():
            continue
        for child in sorted(current.iterdir()):
            if name_matches(child.name, exclude):
                continue
            if child.is_dir():
                stack.append(child)
            elif child.suffix.lower() == suffix:
                found.append(child)
    return sorted(found)


def _exclude_list(prof) -> list[str]:
    """What the packer will drop: the project's own list plus this server's
    own artifacts, which a project must never have to know about."""
    return list(prof.build.exclude) + list(ALWAYS_OMIT_FROM_STAGING)


def _root_notes(prof) -> list[str]:
    """The profile's remarks about the model root, repeated where they are read.

    A root outside the repository is allowed -- a staging area gathering
    several mods' prefix trees legitimately sits beside them -- and the profile
    says so in a note. A note nobody surfaces is a note nobody reads, so it is
    carried into the answer of every tool the root actually governs.
    """
    return [n for n in prof.notes if PROJECT_ROOT_KEY in n]


def _fired(report) -> list[dict]:
    """Only the findings that fired. The full report goes in the job artifact:
    a decision that has to be looped over before it can be read is not one."""
    return [asdict(f) for f in report.findings if f.fired]


def _record_path(prof) -> Path:
    return Path(prof.root) / ".dayz-mcp" / RECORD_NAME


def _read_records(prof) -> dict[str, Fingerprint]:
    try:
        raw = json.loads(_record_path(prof).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    out: dict[str, Fingerprint] = {}
    for key, value in (raw or {}).items():
        try:
            out[key] = Fingerprint(
                kind=str(value["kind"]), size=int(value["size"]),
                lod_count=value["lod_count"],
                strings=tuple(value["strings"]), digest=str(value["digest"]),
            )
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _write_records(prof, updates: dict[str, Fingerprint]) -> None:
    current = {k: asdict(v) for k, v in _read_records(prof).items()}
    current.update({k: asdict(v) for k, v in updates.items()})
    path = _record_path(prof)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        # A record that cannot be written costs C12 its answer on the next
        # call, and nothing else. Losing the build over it would be worse.
        pass


def _record_key(prof, path: Path) -> str:
    try:
        return Path(path).resolve().relative_to(Path(prof.root).resolve()).as_posix()
    except ValueError:
        return Path(path).resolve().as_posix()


def _texture_pairs(mod_dir: Path, prefix_dir: Path | None, exclude: list[str]) -> list[tuple[Path, Path]]:
    """Every shipped `.paa` paired with the source it was converted from.

    C7 cannot be answered from the output alone: a legitimately opaque texture
    and one whose transparency was destroyed are identical in DXT1, and only
    the source separates them. The source sits either beside the `.paa` or at
    the same place under the project root, and both layouts exist here.
    """
    pairs: list[tuple[Path, Path]] = []
    for paa in _shipped(mod_dir, exclude, TEXTURE_SUFFIX):
        candidates = [paa.with_suffix(SOURCE_SUFFIX)]
        if prefix_dir is not None:
            candidates.append(
                prefix_dir / paa.relative_to(mod_dir).with_suffix(SOURCE_SUFFIX)
            )
        for source in candidates:
            if source.is_file():
                pairs.append((source, paa))
                break
    return pairs


# ------------------------------------------------------------------ the build


def asset_build(mod: str = "", source: str = "", deploy: bool = True) -> Result:
    """Build a mod's models from their MLOD sources and put them in the mod.

    Returns a `job_id` immediately: ONE small model measured 75.6 to 78.7
    seconds across four runs, so this can never be a blocking call. Wait for it
    with `job_wait(job_id, timeout=...)` -- give it minutes, not seconds -- and
    read the numbers in the job's summary and its `asset-build.json` artifact.

    What it does, in order: run `binarize` with its working directory set to
    the project root declared as `build.project_root`, judge the ARTIFACT that
    came out (never the tool's exit code -- three separate broken outcomes were
    measured exiting 0, one of them leaving a zero-length file), and only then
    copy the models into the mod. A refused build deploys nothing and leaves
    the artifact the mod already ships exactly as it was.

    `mod` names one of `build.mods`; with a single declared mod it can be
    omitted. `source` is the model directory relative to the mod's own folder
    under the root (e.g. "data/models"); left out, the only directory holding
    `.p3d` files is used and two candidates are a refusal rather than a guess.
    `deploy=False` builds and judges without writing into the mod.

    The source is the MLOD export and the ODOL is the build's output (decision
    D3). Handed an ODOL, `binarize` dies with `0xC0000005` and leaves a
    zero-length file behind, so that is refused before the process starts.

    Model.cfg is never copied for you: it is what the artifact was built from,
    and a mismatch between the copy under the root and the copy in the mod is
    reported by C11 with what to do about it. Rebuilds are not byte-stable and
    are not expected to be -- what a rebuild is compared against is structural.
    """
    guard = require_project()
    if guard:
        return guard
    prof = session.profile()

    mod_name, refusal = _choose_mod(prof, mod)
    if refusal:
        return refusal
    prefix = _prefix_of(mod_name)

    root = resolve_project_root(prof.root, prof.build.project_root)
    if root is None:
        return _no_root(prefix)
    prefix_dir = _prefix_dir(root, prefix)
    if prefix_dir is None:
        # The containment rule, one step earlier and cheaper than binarize's
        # own: a root that does not even hold the mod's folder is off by at
        # least one level, and that is the measured silent failure.
        return fail(
            f"the declared model root {root} holds no {prefix!r} folder",
            hint=f"{PROJECT_ROOT_KEY} must point at the directory that CONTAINS {prefix!r}, "
                 f"not at {prefix!r} itself and not one level above it. One level too deep is "
                 f"the measured silent failure: the artifact comes out valid, smaller, with "
                 f"plausible texture paths and a success code, and the engine renders it "
                 f"untextured",
        )

    if source:
        source_dir = (prefix_dir / PurePosixPath(str(source).replace("\\", "/"))).resolve()
        if not _within(source_dir, prefix_dir.resolve()):
            return fail(
                f"the source {source} climbs out of the mod's own {prefix!r} folder",
                hint=f"source is relative to <{PROJECT_ROOT_KEY}>/{prefix} -- every path inside "
                     f"a model resolves against the root, so a model built from outside the "
                     f"prefix folder cannot produce references that resolve",
            )
        if not source_dir.is_dir():
            return fail(
                f"no such model directory: {source_dir}",
                hint=f"source is relative to <{PROJECT_ROOT_KEY}>/{prefix}, e.g. "
                     f'source="data/models"',
            )
    else:
        candidates = _model_dirs(prefix_dir)
        if not candidates:
            return fail(
                f"no {MODEL_SUFFIX} anywhere under {prefix_dir}",
                hint="export the model to the prefix tree under the declared root first, or "
                     f"point {PROJECT_ROOT_KEY} at the root the exports really land in",
            )
        if len(candidates) > 1:
            shown = ", ".join(
                repr(c.relative_to(prefix_dir).as_posix()) for c in candidates[:10]
            )
            return fail(
                f"{len(candidates)} directories under {prefix_dir} hold models, so which one to "
                "build cannot be guessed",
                hint=f"pass source=<one of {shown}> -- subdirectories are deliberately not "
                     "searched, because recursion would pull in every sibling tree under the "
                     "root",
            )
        source_dir = candidates[0]

    tools_root = session_tools_root()
    if not tools_root:
        return fail(
            "DayZ Tools not found",
            hint="install DayZ Tools from Steam, or set machine.tools in dayz-mcp.local.toml",
        )
    binarize_exe = Path(tools_root) / BINARIZE_REL
    if not binarize_exe.is_file():
        return fail(
            f"binarize is not in this DayZ Tools install ({binarize_exe})",
            hint="install the Addon Builder component of DayZ Tools; nothing can be built from "
                 "an MLOD without it",
        )

    store = session.jobs()
    # Two builds of one mod would run binarize into two directories and copy
    # both results over the same shipped files. Same answer mod_build gives to
    # the same question, for the same reason.
    in_flight = [j for j in store.all() if j.kind == BUILD_KIND and j.status in (QUEUED, RUNNING)]
    if in_flight:
        busy = in_flight[-1].id
        return fail(
            f"a model build is already running for this project (job {busy})",
            hint=f"wait for it with job_wait('{busy}'), or look at it with job_status('{busy}')",
        )

    mod_dir = resolve_mod_dir(prof.root, prof.build.sources, mod_name)
    relative = source_dir.relative_to(prefix_dir.resolve())
    deploy_dir = mod_dir / relative
    job = store.create(BUILD_KIND)
    log_dir = store.artifacts_dir(job.id)

    def run() -> None:
        # Everything that can fail is inside this try, `store.start` included:
        # a thread that dies before its job is resolved leaves the job at
        # "running" forever, blocks every later build, and reports its
        # traceback to stderr where the calling agent never looks.
        try:
            store.start(job.id)
            _run_build(
                store, job.id, log_dir, prof, mod_name, prefix,
                root=root, source_dir=source_dir, deploy_dir=deploy_dir, mod_dir=mod_dir,
                binarize_exe=binarize_exe, tools_root=tools_root, deploy=deploy,
            )
        except Exception as exc:  # noqa: BLE001 - must reach the job, not just stderr
            try:
                store.fail(job.id, f"{type(exc).__name__}: {exc}")
            except Exception:  # noqa: BLE001 - the job store is the broken part
                pass

    try:
        threading.Thread(target=run, daemon=True).start()
    except Exception as exc:  # noqa: BLE001 - a raised tool call answers nobody
        store.fail(job.id, f"the build never started: {type(exc).__name__}: {exc}")
        return fail(
            f"the model build could not be started: {type(exc).__name__}: {exc}",
            hint="this is the process, not the model -- try again, and check job_status for "
                 "what was recorded",
        )
    return ok({
        "job_id": job.id,
        "mod": mod_name,
        "prefix": prefix,
        "root": str(root),
        "source": str(source_dir),
        "deploy_to": str(deploy_dir) if deploy else "",
        "notes": _root_notes(prof),
    })


def _run_build(
    store, job_id: str, log_dir: Path, prof, mod_name: str, prefix: str, *,
    root: Path, source_dir: Path, deploy_dir: Path, mod_dir: Path,
    binarize_exe: Path, tools_root: str, deploy: bool,
) -> None:
    """One build, inside its own thread. Every exit resolves the job."""
    log_path = log_dir / "binarize.log"
    out_dir = log_dir / "models"
    # ONE verdict per model, and this is it. The binarizer's own default asks
    # about the build root, which is a weaker question than the one that
    # decides whether a file may be shipped: references have to resolve inside
    # the PBO (the mod's directory, minus what the packer drops), and the
    # model.cfg a person edits has to be the one the artifact was built from.
    # Two passes would mean two answers, and a build refused by one and allowed
    # by the other is not a decision.
    exclude = _exclude_list(prof)
    shipped_cfg = deploy_dir / MODEL_CFG
    has_shipped = shipped_cfg.is_file()

    def judge(built: Path, source: Path):
        built_cfg = Path(source).parent / MODEL_CFG
        return check_model(
            built,
            prefix=prefix,
            roots={prefix: mod_dir},
            exclude=exclude,
            inputs=[source],
            # The shipped copy is the one a person edits; the copy under the
            # build root is the one binarize actually read. Given both, C11
            # compares declarations. Given one, it can only compare clocks --
            # and the live mismatch on this machine has an OLDER shipped copy,
            # which a clock comparison calls fine.
            model_cfg=(shipped_cfg if has_shipped else built_cfg if built_cfg.is_file() else None),
            built_model_cfg=(built_cfg if has_shipped and built_cfg.is_file() else None),
        )

    result = binarize_models(
        binarize_exe,
        root=root,
        source=source_dir,
        output=out_dir,
        log_path=log_path,
        prefix=prefix,
        binpath=find_binpath(tools_root),
        judge=judge,
    )
    if log_path.is_file():
        store.add_artifact(job_id, log_path)

    payload = result.to_dict()
    payload.update({"mod": mod_name, "prefix": prefix, "deployed": [],
                    "deploy_to": str(deploy_dir)})
    if not has_shipped:
        payload["notes"] = list(payload.get("notes", ())) + [
            f"the mod ships no {MODEL_CFG} beside the models, so C11 had only the copy under "
            "the build root to read and could not compare two copies"
        ]

    if not result.ok:
        _finish(store, job_id, log_dir, payload, 1,
                summary=f"binarize {result.seconds:.1f} s: {result.error}",
                error=result.error, hint=result.hint)
        return

    deployed: list[str] = []
    if deploy:
        deploy_dir.mkdir(parents=True, exist_ok=True)
        records: dict[str, Fingerprint] = {}
        for build in result.builds:
            target = deploy_dir / Path(build.output).name
            shutil.copy2(build.output, target)
            deployed.append(str(target))
            try:
                records[_record_key(prof, target)] = fingerprint(read_p3d(target))
            except P3dError:
                pass
        _write_records(prof, records)
    payload["deployed"] = deployed

    parts = [
        f"{Path(b.output).name} {b.size} B {b.report.summary}" for b in result.builds
    ]
    parts.append(f"binarize {result.seconds:.1f} s, exit {result.code}")
    if result.log:
        parts.append(f"log: {len(result.log.kept)} of {result.log.total} lines kept")
    parts += [f"deployed to {deploy_dir}"] if deployed else ["not deployed (deploy=False)"]
    parts += list(payload.get("notes", ()))
    _finish(store, job_id, log_dir, payload, 0, summary=" | ".join(parts))


def _finish(store, job_id: str, log_dir: Path, payload: dict, code: int,
            *, summary: str, error: str = "", hint: str = "") -> None:
    """Write the artifact, then resolve the job -- in that order, so a caller
    that reads the job the instant it turns failed still finds the evidence."""
    artifact = log_dir / "asset-build.json"
    try:
        artifact.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        store.add_artifact(job_id, artifact)
    except (OSError, TypeError, ValueError):
        pass
    store.finish(job_id, code, summary=summary)
    if error:
        store.fail(job_id, error + (f" -- {hint}" if hint else ""))


# ------------------------------------------------------------------ the check


def asset_check(mod: str = "", model: str = "") -> Result:
    """Judge the models and textures a mod already ships. Builds nothing.

    Needs no DayZ Tools and no build: every check reads a file that is already
    on the disk, because the tools that produce these files are structurally
    unable to report failure. Answers in milliseconds.

    Twelve checks (C1-C12). Four of them refuse, and this call fails when one
    does: a built model is there and is an ODOL, no reference escapes the mod,
    a material was actually inlined, and nothing already binarized is offered
    back to `binarize`. The rest warn -- dangling references, an rvmat pointing
    into another mod, a transparency lost to DXT1 (C7), an animation that never
    reached the artifact, a model.cfg that is not the one the artifact was
    built from, and a structural fingerprint that no longer matches what the
    last build deployed. Every finding says what to DO about it.

    `model` narrows it to one file, relative to the mod's directory. Files the
    packer will drop (`build.exclude`) are not judged: a refusal about a file
    that never enters the pbo is a refusal about nothing.

    A texture is judged against the PNG it came from -- beside it or at the
    same place under `build.project_root` -- because a legitimately opaque
    texture and one whose transparency was destroyed are identical in the
    output alone.
    """
    guard = require_project()
    if guard:
        return guard
    prof = session.profile()

    mod_name, refusal = _choose_mod(prof, mod)
    if refusal:
        return refusal
    prefix = _prefix_of(mod_name)
    mod_dir = resolve_mod_dir(prof.root, prof.build.sources, mod_name)
    if not mod_dir.is_dir():
        return fail(
            f"the mod directory is not there: {mod_dir}",
            hint="check build.mods and build.sources in the profile",
        )

    root = resolve_project_root(prof.root, prof.build.project_root)
    prefix_dir = _prefix_dir(root, prefix) if root is not None else None
    exclude = _exclude_list(prof)
    notes = _root_notes(prof)
    if root is None:
        notes.append(
            f"{PROJECT_ROOT_KEY} is not declared, so the copy of each {MODEL_CFG} the artifact "
            "was built from could not be compared against the shipped one, and C2 had no source "
            "to date the artifact against"
        )

    if model:
        one = (mod_dir / PurePosixPath(str(model).replace("\\", "/"))).resolve()
        if not _within(one, mod_dir.resolve()) or not one.is_file():
            return fail(
                f"no such model in {mod_name}: {model}",
                hint=f"model is a path relative to {mod_dir}",
            )
        models = [one]
    else:
        models = _shipped(mod_dir, exclude, MODEL_SUFFIX)
        if not models:
            notes.append(
                f"no {MODEL_SUFFIX} in {mod_dir} that the packer would ship"
            )

    records = _read_records(prof)
    entries: list[dict] = []
    refusals: list[tuple[Path, Finding]] = []
    clean = 0
    for path in models[:MAX_MODELS]:
        relative = path.relative_to(mod_dir)
        built_cfg = (prefix_dir / relative.parent / MODEL_CFG) if prefix_dir else None
        shipped_cfg = path.parent / MODEL_CFG
        mlod = (prefix_dir / relative) if prefix_dir else None
        report = check_model(
            path,
            prefix=prefix,
            roots={prefix: mod_dir},
            exclude=exclude,
            inputs=[mlod] if mlod is not None and mlod.is_file() else (),
            model_cfg=shipped_cfg if shipped_cfg.is_file() else None,
            built_model_cfg=(built_cfg if built_cfg is not None and built_cfg.is_file() else None),
            recorded=records.get(_record_key(prof, path)),
        )
        fired = _fired(report)
        clean += not fired
        entries.append({
            "path": str(path),
            "size": path.stat().st_size if path.is_file() else 0,
            "ok": report.ok,
            "summary": report.summary,
            "findings": fired,
        })
        refusals += [(path, f) for f in report.refusals]

    texture_findings = [
        check_texture(src, dst).findings[0]
        for src, dst in _texture_pairs(mod_dir, prefix_dir, exclude)
    ]
    warnings = [asdict(f) for f in texture_findings if f.fired]

    data = {
        "mod": mod_name,
        "prefix": prefix,
        "dir": str(mod_dir),
        "root": str(root) if root else "",
        "models": entries,
        "models_total": len(models),
        "clean": clean,
        "textures": {"checked": len(texture_findings), "warnings": warnings},
        "notes": notes,
    }
    if refusals:
        return Result(
            False, data,
            "refused: " + "; ".join(
                f"{f.check} on {p.name} ({f.detail})" for p, f in refusals[:5]
            ),
            refusals[0][1].action,
        )
    return ok(data)


# ---------------------------------------------------------------- the convert


def _locate(prof, given: str) -> tuple[Path | None, list[str]]:
    """A path as the caller wrote it: absolute, or relative to the repository,
    or relative to the declared model root. Every place tried is reported, so
    a typo is answered with where it was looked for."""
    p = Path(str(given).replace("\\", "/"))
    if p.is_absolute():
        return (p if p.is_file() else None), [str(p)]
    tried: list[str] = []
    bases = [Path(prof.root), resolve_project_root(prof.root, prof.build.project_root)]
    for base in bases:
        if base is None:
            continue
        candidate = base / p
        tried.append(str(candidate))
        if candidate.is_file():
            return candidate, tried
    return None, tried


def asset_convert(source: str, output: str = "") -> Result:
    """Convert one texture between `.png` and `.paa`, and judge the result.

    Which compression `ImageToPAA` writes is decided by the SOURCE FILE'S NAME,
    and nothing says so at the time: a name ending in `_co` produces DXT1,
    which keeps ONE BIT of alpha. A source measured 6 distinct alpha levels
    going in and 2 coming out. So this measures the source's alpha before
    converting, warns before the loss and again after it (C7), and says what to
    do -- rename the source to end in `_ca`, because the output itself cannot
    be repaired once the levels are gone.

    `source` is absolute, or relative to the repository, or relative to
    `build.project_root`. `output` defaults to the same name with the other
    extension, beside the source; a relative `output` also lands beside it.

    The verdict is read off the file that was written, never off the exit code.
    """
    guard = require_project()
    if guard:
        return guard
    prof = session.profile()

    src, tried = _locate(prof, source)
    if src is None:
        return fail(
            f"no such file: {source}",
            hint="looked in " + "; ".join(tried) + " -- give a path relative to the repository, "
                 f"relative to {PROJECT_ROOT_KEY}, or an absolute one",
        )
    kind = src.suffix.lower()
    if kind not in CONVERTIBLE:
        return fail(
            f"{src.name} is not something this converts: {kind or '(no extension)'}",
            hint="this converts between " + " and ".join(sorted(CONVERTIBLE))
                 + " only -- the alpha measurement C7 needs a .png to read, and a silently "
                   "accepted .tga would leave that check with nothing to measure",
        )

    if output:
        dst = Path(str(output).replace("\\", "/"))
        dst = dst if dst.is_absolute() else src.parent / dst
    else:
        dst = src.with_suffix(TEXTURE_SUFFIX if kind == SOURCE_SUFFIX else SOURCE_SUFFIX)

    tools_root = session_tools_root()
    if not tools_root:
        return fail(
            "DayZ Tools not found",
            hint="install DayZ Tools from Steam, or set machine.tools in dayz-mcp.local.toml",
        )
    exe = Path(tools_root) / IMAGETOPAA_REL
    if not exe.is_file():
        return fail(
            f"ImageToPAA is not in this DayZ Tools install ({exe})",
            hint="install the ImageToPAA component of DayZ Tools",
        )

    notes: list[str] = []
    if kind == SOURCE_SUFFIX:
        predicted = expected_format(src.name)
        try:
            levels = alpha_levels(src)
        except (PaaError, OSError):
            levels = 0
        if levels > 2 and predicted == DXT1:
            # Said BEFORE the run, because this is the only moment it can still
            # be acted on: after the conversion the levels are gone and the
            # output cannot be repaired.
            notes.append(
                f"{src.name} carries {levels} distinct alpha levels and its name selects "
                f"{DXT1}, which keeps one bit of them -- rename the SOURCE to end in `_ca` "
                "(or drop the `_co`) to keep the gradient"
            )

    log_path = Path(prof.root) / ".dayz-mcp" / "convert.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    outcome = convert(exe, src, dst, log_path)
    if not outcome.ok:
        return fail(
            outcome.error,
            hint="the verdict is read off the file, not off the exit code: this tool answers "
                 "some refusals with a success code and no file at all. Check the source opens, "
                 f"and read {log_path}",
        )

    checks = [check_texture(src, dst).findings[0]] if kind == SOURCE_SUFFIX else []
    fired = [f for f in checks if f.fired]
    data = {
        "source": str(src),
        "output": str(dst),
        "size": outcome.size,
        "format": outcome.format,
        "code": outcome.code,
        "log": str(log_path),
        "checks": [asdict(f) for f in checks],
        "warnings": [asdict(f) for f in fired],
        "notes": notes,
    }
    # A hint on a successful call, because "what to do" is exactly what a
    # warning is for and this is where an agent reads it.
    return Result(True, data, "", fired[0].action if fired else "")
