"""Packing and signing: what a hand-written build script used to do.

Nothing here is project-specific. A mod name gives the source directory, the pbo
name and the prefix; the key pair is whatever lies in <root>/keys.

The stale-pbo check exists because packing can fail without FileBank saying so:
a running server holds the old pbo open, the new one is never written, and the
build silently ships yesterday's code.
"""
from __future__ import annotations

import fnmatch
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
            if any(fnmatch.fnmatch(d, pat) for pat in patterns):
                found.append(str((Path(dirpath) / d).relative_to(src)))
            else:
                keep.append(d)
        dirnames[:] = keep
        for f in filenames:
            if any(fnmatch.fnmatch(f, pat) for pat in patterns):
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


def newest_source_mtime(src: Path, ignore: Sequence[str] = ()) -> float:
    """Newest mtime among files under `src`, skipping anything matching
    `ignore` by name (not descending into a matching directory, same
    discipline as find_excluded).

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
    for dirpath, dirnames, filenames in os.walk(src):
        if ignore:
            dirnames[:] = [d for d in dirnames if not any(fnmatch.fnmatch(d, pat) for pat in ignore)]
        for f in filenames:
            if ignore and any(fnmatch.fnmatch(f, pat) for pat in ignore):
                continue
            try:
                newest = max(newest, (Path(dirpath) / f).stat().st_mtime)
            except OSError:
                continue
    return newest


def pack_one(
    name: str,
    root: Path,
    tools: Path,
    log_path: Path,
    mod_dir: Path | None = None,
    exclude: list[str] | None = None,
    src: Path | None = None,
    stage: bool = False,
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
    # two cannot drift apart again. The cost is a false refusal for a mod
    # that genuinely carries a nested directory called "keys" -- which the
    # error names explicitly, and `stage = true` packs anyway (minus that
    # directory).
    intruders = find_excluded(src, own_artifacts) if not stage else []
    if intruders:
        return PackResult(
            name,
            error=f"refusing to pack {src}: found {', '.join(intruders)}, which belong to the "
                  "build server and not to the mod -- the signing keys, this server's own "
                  "profile, its job store, this mod's previous build. FileBank packs the "
                  "source folder whole, so publishing the result would publish them, the "
                  "private signing key first of all. Point build.sources at the mod's own "
                  "folder, or set build.stage = true to pack a filtered copy instead (the "
                  "layout a mod whose source is the repository root itself needs)",
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
        if omitted:
            staging_note += f"; omitted: {', '.join(omitted)}"

    try:
        code, tail = run_blocking(filebank_cmd(filebank, name, pack_src, out_dir), root, log_path, timeout=1800)
        if code != 0:
            return PackResult(name, error=f"FileBank exit {code}: {tail[-300:]}")

        pbo = out_dir / f"{name}.pbo"
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
                for old in out_dir.glob(f"{name}.pbo.*.bisign"):
                    old.unlink()
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

        # cfgconvert_note and staging_note (soft-degrades / disclosures set
        # above) and signing_note are independent concerns; any of them can
        # legitimately apply at once.
        note = "; ".join(x for x in (cfgconvert_note, staging_note, signing_note) if x)

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
) -> list[PackResult]:
    out: list[PackResult] = []
    for name in names:
        mod_src = (sources or {}).get(name)
        out.append(
            pack_one(
                name, root, tools, Path(log_dir) / f"pack-{name}.log",
                exclude=exclude, src=mod_src, stage=stage,
            )
        )
    return out
