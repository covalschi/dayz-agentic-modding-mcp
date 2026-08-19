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
# patterns catch 3D source files nobody meant to ship. This is also the
# default for Profile.build.exclude -- see profile.py.
DEFAULT_EXCLUDE: tuple[str, ...] = (".git", "*.blend", "*.blend1")


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


def newest_source_mtime(src: Path) -> float:
    newest = 0.0
    for p in Path(src).rglob("*"):
        if p.is_file():
            newest = max(newest, p.stat().st_mtime)
    return newest


def pack_one(
    name: str,
    root: Path,
    tools: Path,
    log_path: Path,
    mod_dir: Path | None = None,
    exclude: list[str] | None = None,
) -> PackResult:
    root = Path(root)
    src = root / name
    mod_dir = Path(mod_dir) if mod_dir else root / f"@{name}"
    out_dir = mod_dir / "addons"

    # FileBank packs the source directory whole: a nested .git, a stray
    # .blend, anything matching `exclude` (default DEFAULT_EXCLUDE) would
    # silently ride along into the published pbo. Refuse rather than copy the
    # tree to a staging directory first -- a copy is always newer than the
    # sources and would permanently disable the stale-pbo check below.
    patterns = list(exclude) if exclude is not None else list(DEFAULT_EXCLUDE)
    if patterns:
        excluded = find_excluded(src, patterns)
        if excluded:
            return PackResult(
                name,
                error=f"refusing to pack {src}: found {', '.join(excluded)}, which FileBank "
                      "would pack whole into the published artifact -- remove it from the mod "
                      "source, or drop it from build.exclude if it belongs there",
            )

    out_dir.mkdir(parents=True, exist_ok=True)

    filebank = Path(tools) / FILEBANK_REL
    if not filebank.exists():
        return PackResult(name, error=f"FileBank not found at {filebank}")

    # FileBank does not parse config.cpp at all -- a syntax error in it
    # survives packing and only surfaces after a multi-minute server boot.
    # CfgConvert answers authoritatively in seconds. Skipped quietly if this
    # DayZ Tools install lacks it: FileBank is still what actually packs, and
    # this is a fast extra check, not a hard dependency.
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

    code, tail = run_blocking(filebank_cmd(filebank, name, src, out_dir), root, log_path, timeout=1800)
    if code != 0:
        return PackResult(name, error=f"FileBank exit {code}: {tail[-300:]}")

    pbo = out_dir / f"{name}.pbo"
    if not pbo.exists():
        return PackResult(name, error=f"{pbo} was not produced")

    # KNOWN FALSE POSITIVE: this compares mtimes, and `git checkout` changes a
    # file's mtime without changing its content -- so a perfectly good pbo
    # built right after switching branches can be flagged "stale" here even
    # though nothing needs rebuilding. A mature tool in this space moved to a
    # content hash for exactly this reason; that rewrite is out of scope for
    # now (see README), so a "stale pbo" error after a branch switch should be
    # read as this false positive, not as a packing failure.
    if pbo.stat().st_mtime < newest_source_mtime(src):
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
                signing_note = f"multiple private keys present (using {priv.stem}), but signer not found at {signer}"
        # Check for missing public key only if we haven't already set a note about the signer
        if not pub and not signing_note:
            signing_note = f"private key found ({priv.stem}) but public key with matching stem not found"
            if len(all_priv_keys) > 1:
                signing_note = f"multiple private keys present (using {priv.stem}), public key not found"

    # cfgconvert_note (a soft-degrade of the syntax gate, set above) and
    # signing_note are independent concerns; both can legitimately apply.
    note = "; ".join(x for x in (cfgconvert_note, signing_note) if x)

    return PackResult(name, pbo=str(pbo), size=pbo.stat().st_size, signed=signed, note=note)


def pack_all(
    names: list[str], root: Path, tools: Path, log_dir: Path, exclude: list[str] | None = None
) -> list[PackResult]:
    out: list[PackResult] = []
    for name in names:
        out.append(pack_one(name, root, tools, Path(log_dir) / f"pack-{name}.log", exclude=exclude))
    return out
