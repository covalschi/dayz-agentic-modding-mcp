"""Packing and signing: what a hand-written build script used to do.

Nothing here is project-specific. A mod name gives the source directory, the pbo
name and the prefix; the key pair is whatever lies in <root>/keys.

The stale-pbo check exists because packing can fail without FileBank saying so:
a running server holds the old pbo open, the new one is never written, and the
build silently ships yesterday's code.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .paths import FILEBANK_REL, SIGNER_REL
from .procs import run_blocking


@dataclass
class PackResult:
    name: str
    pbo: str = ""
    size: int = 0
    signed: bool = False
    error: str = ""
    note: str = ""


def filebank_cmd(filebank: Path, name: str, src: Path, out_dir: Path) -> list[str]:
    return [str(filebank), "-dst", str(out_dir), "-property", f"prefix={name}", str(src)]


def sign_cmd(signer: Path, private_key: Path, pbo: Path) -> list[str]:
    return [str(signer), str(private_key), str(pbo)]


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


def pack_one(name: str, root: Path, tools: Path, log_path: Path, mod_dir: Path | None = None) -> PackResult:
    root = Path(root)
    src = root / name
    mod_dir = Path(mod_dir) if mod_dir else root / f"@{name}"
    out_dir = mod_dir / "addons"
    out_dir.mkdir(parents=True, exist_ok=True)

    filebank = Path(tools) / FILEBANK_REL
    if not filebank.exists():
        return PackResult(name, error=f"FileBank not found at {filebank}")

    code, tail = run_blocking(filebank_cmd(filebank, name, src, out_dir), root, log_path, timeout=1800)
    if code != 0:
        return PackResult(name, error=f"FileBank exit {code}: {tail[-300:]}")

    pbo = out_dir / f"{name}.pbo"
    if not pbo.exists():
        return PackResult(name, error=f"{pbo} was not produced")

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
    note = ""

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
                note = f"multiple private keys present, using {priv.stem}"
        else:
            # Private key exists but signer executable is missing
            note = f"private key present but signer executable not found at {signer}"
            if len(all_priv_keys) > 1:
                note = f"multiple private keys present (using {priv.stem}), but signer not found at {signer}"
        # Check for missing public key only if we haven't already set a note about the signer
        if not pub and not note:
            note = f"private key found ({priv.stem}) but public key with matching stem not found"
            if len(all_priv_keys) > 1:
                note = f"multiple private keys present (using {priv.stem}), public key not found"

    return PackResult(name, pbo=str(pbo), size=pbo.stat().st_size, signed=signed, note=note)


def pack_all(names: list[str], root: Path, tools: Path, log_dir: Path) -> list[PackResult]:
    out: list[PackResult] = []
    for name in names:
        out.append(pack_one(name, root, tools, Path(log_dir) / f"pack-{name}.log"))
    return out
