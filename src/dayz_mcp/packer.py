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


def filebank_cmd(filebank: Path, name: str, src: Path, out_dir: Path) -> list[str]:
    return [str(filebank), "-dst", str(out_dir), "-property", f"prefix={name}", str(src)]


def sign_cmd(signer: Path, private_key: Path, pbo: Path) -> list[str]:
    return [str(signer), str(private_key), str(pbo)]


def find_keys(keys_dir: Path) -> tuple[Path | None, Path | None]:
    if not Path(keys_dir).is_dir():
        return None, None
    priv = sorted(Path(keys_dir).glob("*.biprivatekey"))
    pub = sorted(Path(keys_dir).glob("*.bikey"))
    return (priv[0] if priv else None), (pub[0] if pub else None)


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

    priv, pub = find_keys(root / "keys")
    signed = False
    if pub:
        keys_out = mod_dir / "keys"
        keys_out.mkdir(parents=True, exist_ok=True)
        (keys_out / pub.name).write_bytes(pub.read_bytes())
    if priv:
        signer = Path(tools) / SIGNER_REL
        if signer.exists():
            for old in out_dir.glob(f"{name}.pbo.*.bisign"):
                old.unlink()
            run_blocking(sign_cmd(signer, priv, pbo), root, log_path.with_suffix(".sign.log"), timeout=300)
            signed = any(out_dir.glob(f"{name}.pbo.*.bisign"))

    return PackResult(name, pbo=str(pbo), size=pbo.stat().st_size, signed=signed)


def pack_all(names: list[str], root: Path, tools: Path, log_dir: Path) -> list[PackResult]:
    out: list[PackResult] = []
    for name in names:
        out.append(pack_one(name, root, tools, Path(log_dir) / f"pack-{name}.log"))
    return out
