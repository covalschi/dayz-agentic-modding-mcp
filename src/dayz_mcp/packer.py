"""Packing and signing: what a hand-written build script used to do.

Nothing here is project-specific. A mod name gives the source directory, the pbo
name and the prefix; the key pair is whatever lies in <root>/keys.

The stale-pbo check exists because packing can fail without FileBank saying so:
a running server holds the old pbo open, the new one is never written, and the
build silently ships yesterday's code.

Contract for callers, on signatures: a build that SUCCEEDS never leaves a
signature produced for an earlier pbo. `pack_one` clears every
`<name>.pbo.*.bisign` beside the new pbo before deciding whether it can sign a
new one, so `signed=False` in a successful result means there is no signature
on disk either -- not "the old one is still there".

A build that FAILS leaves both the pbo and its signature alone. Usually that is
exactly right, because nothing was rewritten. It is deliberately NOT a promise
that the two still match: FileBank can rewrite the pbo and the run still be
refused as stale, e.g. when a source file is saved during a multi-minute pack.
Read a failed build as "state unknown, rebuild", not as "unchanged". The one
exception: when manifest tracking (below) detects that the SET of source files
changed, the old pbo and its signatures are deleted before FileBank runs, so a
failure after that point leaves neither -- which is still "state unknown,
rebuild", just with less on disk.
"""
from __future__ import annotations

import fnmatch
import json
import os
import shutil
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .paths import CFGCONVERT_REL, FILEBANK_REL, SIGNER_REL
from .procs import run_blocking


@dataclass
class PackResult:
    name: str
    pbo: str = ""
    size: int = 0
    signed: bool = False
    error: str = ""
    note: str = ""


# FileBank packs a mod's source folder *whole*: anything matching one of
# these inside it rides along into the published pbo. ".git" catches a
# nested repository (history, hooks, possibly local config); the .blend
# patterns catch 3D source files nobody meant to ship; ".gitignore",
# ".gitattributes", "README.md" and "*.ps1" catch what a repository root
# normally carries alongside a mod and a mod never needs at runtime -- most
# relevant to a mod whose source *is* the repository root (see build.stage).
# This is a plain default: a project can override it wholesale via
# build.exclude, e.g. a mod that genuinely ships one of these. This is also
# the default for Profile.build.exclude -- see profile.py.
DEFAULT_EXCLUDE: tuple[str, ...] = (
    ".git", "*.blend", "*.blend1", ".gitignore", ".gitattributes", "README.md", "*.ps1",
)

# The server's OWN artifacts, never packed -- kept out of a staged copy, and
# refused outright when there is no copy to filter (see pack_one). Neither is
# conditional on build.exclude: a project must never have to know these exist,
# so this is not configurable the way DEFAULT_EXCLUDE is. "dayz-mcp.toml" and
# "dayz-mcp.local.toml" are the two halves of this server's own profile; the
# local half is the worse of the two, since its entire reason to exist is
# that it carries machine-specific absolute paths (game, tools, the test
# stand) and never leaves the machine -- packing it into a published
# artifact defeats that completely. ".dayz-mcp" is the job-store directory
# this server writes its own run history into.
ALWAYS_OMIT_FROM_STAGING: tuple[str, ...] = ("dayz-mcp.toml", "dayz-mcp.local.toml", ".dayz-mcp")


def filebank_cmd(filebank: Path, name: str, src: Path, out_dir: Path) -> list[str]:
    return [str(filebank), "-dst", str(out_dir), "-property", f"prefix={name}", str(src)]


def sign_cmd(signer: Path, private_key: Path, pbo: Path) -> list[str]:
    return [str(signer), str(private_key), str(pbo)]


def config_syntax_cmd(cfgconvert: Path, cfg: Path, out: Path) -> list[str]:
    """CfgConvert answers a config.cpp syntax question authoritatively in
    seconds, needing neither the vanilla class index nor the game -- unlike
    FileBank, which does not parse the config at all, so a syntax error in it
    survives packing and only surfaces after a multi-minute server boot.

    Must be run with the working directory set to `cfg`'s own folder (see
    pack_one): otherwise relative #include directives do not resolve and
    CfgConvert reports a false error.
    """
    return [str(cfgconvert), "-bin", "-dst", str(out), str(cfg)]


def config_text_cmd(cfgconvert: Path, cfg: Path, out: Path) -> list[str]:
    """The other direction: a binarised config back into readable text.

    `config.bin` is what most published mods actually ship, and it is where
    the answer to "is there a class with this name" lives. Nothing but
    CfgConvert reads it, so the knowledge index calls this and parses the
    result. Unlike `config_syntax_cmd` this needs no particular working
    directory: a binarised config has no unresolved #includes left in it.
    """
    return [str(cfgconvert), "-txt", "-dst", str(out), str(cfg)]


def bankrev_cmd(bankrev: Path, pbo: Path, out_dir: Path) -> list[str]:
    """Unpack a pbo. BankRev creates `out_dir/<pbo stem>/` and puts the
    contents there -- it is NOT `out_dir` itself, and a caller that assumes
    otherwise finds an empty directory and concludes the archive was empty.
    See `bankrev_output` for the one place that formula lives.
    """
    return [str(bankrev), "-f", str(out_dir), str(pbo)]


def bankrev_output(pbo: Path, out_dir: Path) -> Path:
    """Where `bankrev_cmd` actually puts the contents of `pbo`."""
    return Path(out_dir) / Path(pbo).stem


def name_matches(name: str, patterns: Sequence[str]) -> bool:
    """Whether a single file or directory NAME matches one of `patterns`.

    The one definition of "matches" in this module: find_excluded selects by
    it, and pack_one's staging note classifies find_excluded's own output by it
    to say which list an omission came from. Two spellings of the same rule
    would let the note describe an omission by the wrong reason.
    """
    return any(fnmatch.fnmatch(name, pat) for pat in patterns)


def find_excluded(src: Path, patterns: Sequence[str]) -> list[str]:
    """Paths (relative to `src`) matching one of `patterns` by filename.

    Does not descend into a matched directory: a `.git` folder can hold
    thousands of objects, and a check that only needs to say "this is here,
    remove it" has no reason to enumerate them.
    """
    src = Path(src)
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(src):
        keep: list[str] = []
        for d in dirnames:
            if name_matches(d, patterns):
                found.append(str((Path(dirpath) / d).relative_to(src)))
            else:
                keep.append(d)
        dirnames[:] = keep
        for f in filenames:
            if name_matches(f, patterns):
                found.append(str((Path(dirpath) / f).relative_to(src)))
    return found


def find_keys(keys_dir: Path) -> tuple[Path | None, Path | None]:
    """Find a private/public key pair, matching by stem.

    Returns (private, public) where public's stem matches private's stem.
    If multiple private keys exist, returns the sorted-first.
    If no matching public key exists, returns (private, None).
    """
    if not Path(keys_dir).is_dir():
        return None, None
    priv_keys = sorted(Path(keys_dir).glob("*.biprivatekey"))
    if not priv_keys:
        return None, None

    # Take the first (sorted) private key
    priv = priv_keys[0]

    # Find matching public key with the same stem
    stem = priv.stem  # e.g., "MyKey" from "MyKey.biprivatekey"
    pub_path = Path(keys_dir) / f"{stem}.bikey"
    pub = pub_path if pub_path.exists() else None

    return priv, pub


def _iter_source_files(src: Path, ignore: Sequence[str] = ()):
    """Every file under `src`, skipping anything matching `ignore` by name
    (not descending into a matching directory, same discipline as
    find_excluded). The one definition of which files count as "the source"
    for freshness purposes: newest_source_mtime and source_manifest are the
    two halves of one staleness story, and measuring different trees would
    let a defect slip between them.
    """
    for dirpath, dirnames, filenames in os.walk(src):
        if ignore:
            dirnames[:] = [d for d in dirnames if not any(fnmatch.fnmatch(d, pat) for pat in ignore)]
        for f in filenames:
            if ignore and any(fnmatch.fnmatch(f, pat) for pat in ignore):
                continue
            yield Path(dirpath) / f


def newest_source_mtime(src: Path, ignore: Sequence[str] = ()) -> float:
    """Newest mtime among files under `src` (per _iter_source_files).

    `ignore` matters for a mod whose source is (or contains) the profile
    root: the mod's own build output and this server's job-store directory
    then live INSIDE `src`, and are themselves written to *during the very
    packing run this function's result gates* -- the output pbo's own
    write, and the job log this run appends to, both land inside `src` in
    that layout. Without skipping them, this would compare a freshly-built
    pbo against files that only got newer because building it touched them,
    and report every single such build as stale. See pack_one's callers.
    """
    newest = 0.0
    for p in _iter_source_files(src, ignore):
        try:
            newest = max(newest, p.stat().st_mtime)
        except OSError:
            continue
    return newest


def source_manifest(src: Path, ignore: Sequence[str] = ()) -> dict[str, int]:
    """Relative posix path -> size for every file under `src` (per
    _iter_source_files).

    The half of freshness that mtimes cannot carry. A file MOVED with its
    mtime preserved, or DELETED outright, changes no surviving mtime at all:
    every source then looks older than the pbo, FileBank's own internal
    staleness check silently keeps the old bytes while reporting success (see
    the staging comment in pack_one), and the mtime guard sees a perfectly
    "fresh" pbo with the old layout still inside. Both happened on a real
    project the same day. Comparing this mapping against the one recorded at
    the last successful pack catches adds, moves, deletions and size changes
    alike; sizes ride along because they are free and catch a content change
    that kept the mtime (a restored backup, a `git checkout`).
    """
    src = Path(src)
    manifest: dict[str, int] = {}
    for p in _iter_source_files(src, ignore):
        try:
            manifest[p.relative_to(src).as_posix()] = p.stat().st_size
        except OSError:
            # A file that cannot be measured cannot be vouched for either;
            # leaving it out makes the comparison err toward repacking.
            continue
    return manifest


def _read_manifest(path: Path) -> dict[str, int] | None:
    """The manifest recorded at the last successful pack, or None when there
    is nothing usable to compare against -- absent, unreadable and malformed
    all mean the same thing here: the existing pbo's contents cannot be
    vouched for, and the caller must repack rather than trust it."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    if not all(isinstance(k, str) and isinstance(v, int) for k, v in data.items()):
        return None
    return data


def pack_one(
    name: str,
    root: Path,
    tools: Path,
    log_path: Path,
    mod_dir: Path | None = None,
    exclude: list[str] | None = None,
    src: Path | None = None,
    stage: bool = False,
    manifest_path: Path | None = None,
) -> PackResult:
    root = Path(root)
    src = Path(src) if src is not None else root / name
    mod_dir = Path(mod_dir) if mod_dir else root / f"@{name}"
    out_dir = mod_dir / "addons"

    # The server's own artifacts and this mod's own build output -- never
    # part of "the source", whether that means "safe to pack" (the guard
    # immediately below and staging further down) or "counts toward
    # staleness" (the mtime comparison at the end). All three only matter
    # when `src` is (or contains) the profile root, but computing this once
    # up front keeps them in sync.
    own_artifacts = [*ALWAYS_OMIT_FROM_STAGING, "keys", mod_dir.name]

    # Unconditional, and deliberately ahead of the `exclude` check below:
    # packing the private signing key is not a configuration mistake a
    # project can make, it is this server handing over its author's identity
    # -- whoever holds that key can sign arbitrary mods as them, and every
    # server whitelisting the matching .bikey will accept them. Staging
    # filters these out; without staging there is nothing to filter, so the
    # only way to honour the same rule is to refuse.
    #
    # This ran only inside `if stage:` before, which left the DEFAULT path
    # with no protection at all. What appeared to cover the common case was
    # the unrelated `.git` refusal below -- and that disappears the moment
    # .git is absent (a release archive, a CI checkout) or `exclude` is
    # narrowed, which the shipped example profile actively invited.
    #
    # Matched by NAME at any depth, via the same find_excluded call on the
    # same list that staging passes to shutil.ignore_patterns: what one path
    # refuses is then exactly what the other omits, by construction, so the
    # two cannot drift apart again. The cost is that a mod genuinely carrying
    # a directory called "keys" (public keys of the mods it depends on, say)
    # cannot pack it either -- a deliberate trade, but one the message must
    # be honest about. Claiming such a folder "belongs to the build server"
    # would be false, and recommending `stage = true` without saying that
    # staging drops it would steer the reader into silent data loss. So: name
    # the entries, say the names are reserved, and state what staging costs.
    intruders = find_excluded(src, own_artifacts) if not stage else []
    if intruders:
        return PackResult(
            name,
            error=f"refusing to pack {src}: found {', '.join(intruders)}. Those names are "
                  "reserved -- inside a mod source they collide with what the build server "
                  "manages in a project root: the signing keys, both halves of its own "
                  "profile, its job store, this mod's built output. FileBank packs the source "
                  "folder whole, so packing them would publish them, a private signing key "
                  "first of all, and no build.exclude setting changes that. Either point "
                  "build.sources at a mod folder that does not contain them, or set "
                  "build.stage = true, which packs the mod WITHOUT them: every entry named "
                  "above is omitted from the pbo, including any that genuinely belongs to "
                  "the mod",
        )

    # FileBank packs the source directory whole: a nested .git, a stray
    # .blend, anything matching `exclude` (default DEFAULT_EXCLUDE) would
    # silently ride along into the published pbo. Without `stage`, refuse
    # rather than copy the tree to a staging directory first -- a copy is
    # always newer than the sources and would silently disable the stale-pbo
    # check below *if that check measured the copy*. `stage` (below) makes
    # copying safe again by never doing that.
    patterns = list(exclude) if exclude is not None else list(DEFAULT_EXCLUDE)
    if not stage and patterns:
        excluded = find_excluded(src, patterns)
        if excluded:
            return PackResult(
                name,
                error=f"refusing to pack {src}: found {', '.join(excluded)}, which FileBank "
                      "would pack whole into the published artifact -- remove it from the mod "
                      "source, drop it from build.exclude if it belongs there, or set "
                      "build.stage = true to pack a filtered copy instead (e.g. for a mod whose "
                      "source is the repository root itself)",
            )

    out_dir.mkdir(parents=True, exist_ok=True)

    filebank = Path(tools) / FILEBANK_REL
    if not filebank.exists():
        return PackResult(name, error=f"FileBank not found at {filebank}")

    # FileBank does not parse config.cpp at all -- a syntax error in it
    # survives packing and only surfaces after a multi-minute server boot.
    # CfgConvert answers authoritatively in seconds. Skipped quietly if this
    # DayZ Tools install lacks it: FileBank is still what actually packs, and
    # this is a fast extra check, not a hard dependency. Always reads the
    # ORIGINAL `src`, never a staged copy -- staging (if any) happens below,
    # after this, and config.cpp is never something staging would omit.
    cfgconvert_note = ""
    cfg = src / "config.cpp"
    if cfg.exists():
        cfgconvert = Path(tools) / CFGCONVERT_REL
        if cfgconvert.exists():
            syntax_out = log_path.with_name(f"{log_path.stem}.cfgconvert.bin")
            code, tail = run_blocking(
                config_syntax_cmd(cfgconvert, cfg, syntax_out),
                cfg.parent,  # relative #include only resolves from config.cpp's own folder
                log_path.with_suffix(".cfgconvert.log"),
                timeout=120,
            )
            # The compiled output is only useful as an on/off signal here --
            # nothing downstream reads it, so it should not linger next to
            # the real build artifacts.
            syntax_out.unlink(missing_ok=True)
            if code == 127:
                # run_blocking's own "cannot start" code: CfgConvert.exe
                # exists as a file but could not actually be run (wrong
                # architecture, broken permissions, a placeholder). That is a
                # broken toolchain, not a config problem -- soft-degrade the
                # same way a missing signer does, rather than blocking a
                # possibly-correct build over a check that could not run.
                cfgconvert_note = f"CfgConvert found at {cfgconvert} but could not be run: {tail[-200:]}"
            elif code != 0:
                # The exit code alone is the gate. A substring search of the
                # output was tried and dropped: CfgConvert's own success
                # message can legitimately contain the word "error" (e.g.
                # "Config : 0 errors, 0 warnings" -- confirmed against the
                # real binary), so that check only ever risked rejecting a
                # correct config.cpp, never caught anything the exit code
                # did not already catch.
                return PackResult(name, error=f"{cfg} failed CfgConvert's syntax check: {tail[-300:]}")

    # The mtime comparison at the end of this function can only see EDITS.
    # A file moved with its mtime preserved (`mv` between script layers) or
    # deleted outright leaves every surviving mtime untouched -- and FileBank,
    # whose own internal staleness check silently skips rewriting a pbo whose
    # sources all look older than it (reproduced against the real binary, see
    # the staging comment below), then reports success over a pbo that still
    # holds the old layout. Both misses happened on a real project the same
    # day: a moved file compiled at its old path inside the pbo, a deleted
    # one stayed inside it. So callers that can keep bookkeeping pass
    # `manifest_path`, and the set of source paths+sizes is compared against
    # what the last successful pack recorded. Measured on the ORIGINAL `src`
    # with the same `ignore` as the mtime check, for the same reasons.
    manifest_now: dict[str, int] | None = None
    if manifest_path is not None:
        manifest_now = source_manifest(src, ignore=own_artifacts)

    staging_dir: Path | None = None
    staging_note = ""
    pack_src = src
    if stage:
        # Copy `src` into a temp directory, omitting anything matching
        # `patterns` (plus two entries staging always omits regardless of
        # `exclude`, below) -- then pack THAT. Safe only because the
        # stale-pbo comparison further down measures `src`, the ORIGINAL
        # tree, never this copy: a copy is always newer than its sources
        # (it was just written), so measuring it would make that check pass
        # unconditionally and silently disable the whole guard. That is the
        # one reason staging was forbidden before it had this explicit
        # opt-in with this rule attached.
        staging_ignore = list(dict.fromkeys([*patterns, *own_artifacts]))
        omitted = find_excluded(src, staging_ignore)
        staging_dir = Path(tempfile.mkdtemp(prefix="dayz-mcp-stage-"))
        pack_src = staging_dir / name
        # copy_function=shutil.copy, NOT the copytree default (copy2): copy2
        # preserves the ORIGINAL file's mtime on the copy. FileBank does its
        # own internal staleness check and silently skips rewriting a pbo
        # whose source files all look older than the destination -- which a
        # copy2'd staged copy always would, on any rebuild after the first
        # (the true source files keep their original edit times, predating
        # whatever pbo is already sitting at the destination). Reproduced
        # directly against FileBank: identical command, only the copy
        # function changed, between "reports success but the pbo's bytes on
        # disk do not change at all" and an actual rewrite.
        shutil.copytree(src, pack_src, ignore=shutil.ignore_patterns(*staging_ignore), copy_function=shutil.copy)
        # What went IN, not just what stayed out: an omission list alone still
        # leaves an agent (or a person reading the job summary) with no way
        # to notice something unexpected got packed short of dissecting the
        # pbo itself -- which is exactly how the six-file leak this list now
        # also guards against was actually found.
        included = sorted(p.name for p in pack_src.iterdir())
        staging_note = f"staged copy included: {', '.join(included)}"
        # Two kinds of omission, told apart rather than run together. What
        # build.exclude removed is routine -- the project asked for it. What a
        # reserved name removed is not: a mod may genuinely ship a directory
        # called "keys", and listed among ".git" and "README.md" its
        # disappearance reads as housekeeping. Classified by the same
        # name_matches rule find_excluded selected them with, so an omission
        # can never be reported under the wrong reason.
        reserved_out = [p for p in omitted if name_matches(Path(p).name, own_artifacts)]
        routine_out = [p for p in omitted if p not in set(reserved_out)]
        if routine_out:
            staging_note += f"; omitted by build.exclude: {', '.join(routine_out)}"
        if reserved_out:
            staging_note += (
                "; omitted as reserved names, so NOT in the pbo even if the mod ships them: "
                + ", ".join(reserved_out)
            )

    pbo = out_dir / f"{name}.pbo"
    try:
        # When the recorded set no longer matches the tree -- or there is no
        # record to check against, which is what a pbo built before manifest
        # tracking (or with its manifest lost) looks like -- the destination
        # is deleted first: FileBank cannot silently skip a rewrite whose
        # destination does not exist. An unchanged set deletes nothing, so
        # the stale-pbo refusal below keeps its territory: a running server
        # holding the file during an ordinary edit still gets reported by it,
        # not destroyed here.
        if manifest_now is not None and pbo.exists() and manifest_now != _read_manifest(manifest_path):
            try:
                pbo.unlink()
            except OSError as exc:
                return PackResult(
                    name,
                    pbo=str(pbo),
                    error="cannot repack: the set of source files changed since the last pack "
                          "(files added, moved or deleted -- a change mtimes cannot show, so "
                          "FileBank would silently keep the old contents), and the existing "
                          f"{pbo.name} could not be deleted to force a rewrite: {exc} "
                          "(a running server usually holds it open)",
                )
            # The pbo is gone, so every signature beside it describes a file
            # that no longer exists -- the same rule the post-pack cleanup
            # below applies. Deliberately only after the unlink SUCCEEDED:
            # when it fails, the old pbo is still on disk untouched and its
            # signature still describes it correctly, exactly the reasoning
            # the stale-pbo path uses to keep signatures.
            for old_sig in out_dir.glob(f"{name}.pbo.*.bisign"):
                old_sig.unlink(missing_ok=True)

        code, tail = run_blocking(filebank_cmd(filebank, name, pack_src, out_dir), root, log_path, timeout=1800)
        if code != 0:
            return PackResult(name, error=f"FileBank exit {code}: {tail[-300:]}")

        if not pbo.exists():
            return PackResult(name, error=f"{pbo} was not produced")

        # KNOWN FALSE POSITIVE: this compares mtimes, and `git checkout`
        # changes a file's mtime without changing its content -- so a
        # perfectly good pbo built right after switching branches can be
        # flagged "stale" here even though nothing needs rebuilding. A mature
        # tool in this space moved to a content hash for exactly this reason;
        # that rewrite is out of scope for now (see README), so a "stale
        # pbo" error after a branch switch should be read as this false
        # positive, not as a packing failure.
        #
        # MUST measure `src` here, never `pack_src` / the staging copy: the
        # copy was written moments ago by shutil.copytree above and is
        # therefore always newer than the pbo that was just built from it,
        # which would make this comparison never fire and silently disable
        # the guard entirely under `stage = true`. This is the one thing
        # that makes staging different from the tree-copy this project
        # explicitly forbids elsewhere.
        #
        # `ignore=own_artifacts` matters for the same reason it matters to
        # staging: when `src` is (or contains) the profile root, the pbo
        # this very call is about to judge -- and the job log this very
        # packing run is writing to -- both live INSIDE `src`. Without
        # skipping them, this comparison would find "the newest file in
        # src" to be a file the current build itself just touched, and
        # report every single root-layout build as stale, unconditionally.
        # Reproduced against a real project before this line existed: a
        # perfectly good build failed "stale pbo" on the very first attempt.
        if pbo.stat().st_mtime < newest_source_mtime(src, ignore=own_artifacts):
            return PackResult(
                name,
                pbo=str(pbo),
                error="stale pbo: it is older than the sources, so packing did not really happen "
                      "(a running server usually holds the old file open)",
            )

        # Only now, with the stale check's vouching, is the manifest recorded
        # -- and never on a failed build: a manifest that advanced past a pack
        # that did not really happen would stop the NEXT build from forcing
        # the rewrite this one failed to produce. Failing to write it is not
        # worth failing the build over, since the miss errs toward repacking;
        # but it must not be silent either, or the unconditional repack it
        # causes next time would read as FileBank misbehaving.
        manifest_note = ""
        if manifest_path is not None and manifest_now is not None:
            try:
                manifest_path.parent.mkdir(parents=True, exist_ok=True)
                manifest_path.write_text(
                    json.dumps(manifest_now, ensure_ascii=False, sort_keys=True, indent=1),
                    encoding="utf-8",
                )
            except OSError as exc:
                manifest_note = (
                    f"could not record the source manifest at {manifest_path}: {exc}; "
                    "the next build will repack unconditionally"
                )

        # A new pbo is on disk, so every signature next to it describes a file
        # that no longer exists. Removing them is UNCONDITIONAL, and that is
        # the point: this used to live two levels down, inside `if priv:` and
        # then inside `if signer.exists():`, so a build with the key gone (or
        # with the signer missing) left the previous signature sitting over the
        # new artifact while the result said "unsigned". A stand that verifies
        # signatures then rejects the mod, and every tool in the chain reports
        # success -- worse than no signature at all, because there is no
        # workflow in which keeping one is the desired outcome.
        #
        # Deliberately AFTER the stale-pbo check above, which returns early:
        # when packing did not really happen the old pbo is still the old pbo,
        # and its signature still describes it correctly. Only a pbo that was
        # genuinely rewritten invalidates them.
        for old_sig in out_dir.glob(f"{name}.pbo.*.bisign"):
            old_sig.unlink(missing_ok=True)

        # Determine signing state and collect notes
        keys_dir = root / "keys"
        all_priv_keys = sorted(keys_dir.glob("*.biprivatekey")) if keys_dir.is_dir() else []
        priv, pub = find_keys(keys_dir)
        signed = False
        signing_note = ""

        # Copy public key to mod output if it exists
        if pub:
            keys_out = mod_dir / "keys"
            keys_out.mkdir(parents=True, exist_ok=True)
            (keys_out / pub.name).write_bytes(pub.read_bytes())

        # Attempt signing if private key is present
        if priv:
            signer = Path(tools) / SIGNER_REL
            if signer.exists():
                # Stale signatures are already gone (above), for every path
                # through this function rather than only for this one.
                run_blocking(sign_cmd(signer, priv, pbo), root, log_path.with_suffix(".sign.log"), timeout=300)
                signed = any(out_dir.glob(f"{name}.pbo.*.bisign"))
                if len(all_priv_keys) > 1:
                    signing_note = f"multiple private keys present, using {priv.stem}"
            else:
                # Private key exists but signer executable is missing
                signing_note = f"private key present but signer executable not found at {signer}"
                if len(all_priv_keys) > 1:
                    signing_note = (
                        f"multiple private keys present (using {priv.stem}), but signer not found at {signer}"
                    )
            # Check for missing public key only if we haven't already set a note about the signer
            if not pub and not signing_note:
                signing_note = f"private key found ({priv.stem}) but public key with matching stem not found"
                if len(all_priv_keys) > 1:
                    signing_note = f"multiple private keys present (using {priv.stem}), public key not found"
        elif not keys_dir.is_dir():
            # An unsigned pbo stays a success -- plenty of builds never need a
            # signature. It must not be a SILENT one: with no note at all,
            # signed=False reads exactly like a signing attempt that failed,
            # and the only way to tell them apart is to go digging in the pack
            # log. Name the directory that was looked for, so the answer is
            # "put a key pair here", not "find out why".
            signing_note = f"no signing keys: {keys_dir} does not exist, so the pbo is unsigned"
        else:
            signing_note = f"no signing keys: {keys_dir} holds no *.biprivatekey, so the pbo is unsigned"

        # cfgconvert_note, staging_note, manifest_note (soft-degrades /
        # disclosures set above) and signing_note are independent concerns;
        # any of them can legitimately apply at once.
        note = "; ".join(x for x in (cfgconvert_note, staging_note, manifest_note, signing_note) if x)

        return PackResult(name, pbo=str(pbo), size=pbo.stat().st_size, signed=signed, note=note)
    finally:
        if staging_dir is not None:
            shutil.rmtree(staging_dir, ignore_errors=True)


def pack_all(
    names: list[str],
    root: Path,
    tools: Path,
    log_dir: Path,
    exclude: list[str] | None = None,
    sources: dict[str, Path] | None = None,
    stage: bool = False,
    manifest_dir: Path | None = None,
) -> list[PackResult]:
    out: list[PackResult] = []
    for name in names:
        mod_src = (sources or {}).get(name)
        out.append(
            pack_one(
                name, root, tools, Path(log_dir) / f"pack-{name}.log",
                exclude=exclude, src=mod_src, stage=stage,
                manifest_path=(Path(manifest_dir) / f"{name}.json") if manifest_dir is not None else None,
            )
        )
    return out
