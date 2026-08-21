"""Reading a `.paa`, and converting to and from one with `ImageToPAA`.

**The format is decided by the SOURCE file's name, not by anything in the
image.** Measured on the real tool, three runs of one identical RGBA image
differing only in the file name: `_co` produces DXT1, `_ca` produces DXT5, and
no suffix at all produces DXT5. The suffix is input, not documentation.

**Transparency is not "cut", it is quantised.** DXT1 carries one bit of alpha.
The same graded source went in at 6 alpha levels and came back out of a `_co`
conversion at exactly 2, and out of a `_ca` conversion still graded. So the
question "did this texture keep its transparency" is answered by two numbers
that nothing else here can supply: the two-byte format signature of the `.paa`
(`01ff` against `05ff`), and the number of distinct alpha levels in the source
PNG. Without the second, a legitimately opaque texture and a texture whose
transparency was destroyed are indistinguishable -- both are "no gradient in
the output".

The PNG is decoded here rather than with an imaging library because this
server has no imaging dependency and needs exactly one number out of the file.
The decoder is the plain non-interlaced path of the PNG specification, and its
answers were checked against Pillow on 24 real textures -- every one identical.
Interlaced sources are REFUSED rather than approximated: a wrong number that
looks plausible is the failure mode this whole phase exists to stop. Cost is
one pass in Python over the pixel data, measured at 0.34 s for 1024x1024.
"""
from __future__ import annotations

import os
import struct
import zlib
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath

from ..procs import run_blocking

DXT1 = "DXT1"
DXT5 = "DXT5"
UNKNOWN = "UNKNOWN"

#: The two-byte type word as it lies in the file. Only DXT1 and DXT5 were
#: measured on this machine's textures; the rest are the other types the format
#: defines, named so an unexpected one is reported rather than called unknown.
PAA_FORMATS: dict[bytes, str] = {
    b"\x01\xff": DXT1,
    b"\x02\xff": "DXT2",
    b"\x03\xff": "DXT3",
    b"\x04\xff": "DXT4",
    b"\x05\xff": DXT5,
    b"\x44\x44": "ARGB4444",
    b"\x55\x15": "ARGB1555",
    b"\x80\x80": "AI88",
}

#: What `ImageToPAA` produces for a given source name suffix, measured.
SUFFIX_FORMATS: dict[str, str] = {"_co": DXT1, "_ca": DXT5}
DEFAULT_FORMAT = DXT5

#: The only two extensions this server converts between. Narrow on purpose:
#: the alpha measurement above needs a PNG, and a silently accepted `.tga`
#: would leave the check with nothing to measure.
CONVERTIBLE = frozenset({".png", ".paa"})

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_PNG_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}


class PaaError(Exception):
    """The file cannot be read as the image it claims to be."""


@dataclass(frozen=True)
class PaaInfo:
    path: str
    size: int
    signature: str
    format: str


@dataclass(frozen=True)
class ConvertResult:
    """The outcome of one `ImageToPAA` run, judged on the artifact.

    `ok` never means "the tool exited 0". It means a file was produced, it has
    bytes in it, and -- when it is a `.paa` -- it carries a signature this
    module recognises.
    """

    ok: bool
    source: str
    output: str
    size: int = 0
    format: str = ""
    code: int = 0
    error: str = ""
    tail: str = ""


# ------------------------------------------------------------------- the .paa


def paa_format(data: bytes) -> str:
    """The format named by the first two bytes, or `UNKNOWN`."""
    return PAA_FORMATS.get(bytes(data[:2]), UNKNOWN) if len(data) >= 2 else UNKNOWN


def read_paa(path: str | os.PathLike[str]) -> PaaInfo:
    """Read a paa's header off the disk. Refuses missing and empty by name."""
    p = Path(path)
    try:
        with p.open("rb") as fh:
            head = fh.read(2)
        size = p.stat().st_size
    except FileNotFoundError as exc:
        raise PaaError(f"{p} is not there: nothing was converted into it") from exc
    except OSError as exc:
        raise PaaError(f"{p} cannot be read: {exc}") from exc
    if size == 0:
        raise PaaError(f"{p} is empty: the conversion wrote a file and no image into it")
    return PaaInfo(path=str(p), size=size, signature=head.hex(), format=paa_format(head))


def expected_format(name: str) -> str:
    """The format `ImageToPAA` will choose for a source called `name`.

    Matched on the file's own stem, never on the path: a directory named
    `co_textures` must not decide the format of everything under it.
    """
    stem = PurePosixPath(str(name).replace("\\", "/")).stem.lower()
    for suffix, fmt in SUFFIX_FORMATS.items():
        if stem.endswith(suffix):
            return fmt
    return DEFAULT_FORMAT


# ------------------------------------------------------------------- the .png


def _png_chunks(data: bytes):
    pos = len(PNG_MAGIC)
    while pos + 8 <= len(data):
        (length,) = struct.unpack_from(">I", data, pos)
        kind = data[pos + 4 : pos + 8]
        yield kind, data[pos + 8 : pos + 8 + length]
        pos += 12 + length


def _paeth(left: int, up: int, upleft: int) -> int:
    p = left + up - upleft
    pa, pb, pc = abs(p - left), abs(p - up), abs(p - upleft)
    if pa <= pb and pa <= pc:
        return left
    return up if pb <= pc else upleft


def _unfilter(line: bytearray, prev: bytes, bpp: int, ftype: int) -> None:
    if ftype == 0:
        return
    for i in range(len(line)):
        left = line[i - bpp] if i >= bpp else 0
        if ftype == 1:
            line[i] = (line[i] + left) & 0xFF
        elif ftype == 2:
            line[i] = (line[i] + prev[i]) & 0xFF
        elif ftype == 3:
            line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif ftype == 4:
            upleft = prev[i - bpp] if i >= bpp else 0
            line[i] = (line[i] + _paeth(left, prev[i], upleft)) & 0xFF
        else:
            raise PaaError(f"unknown PNG filter type {ftype}")


def alpha_levels(source: bytes | str | os.PathLike[str]) -> int:
    """How many distinct alpha values a PNG holds.

    1 means the image is opaque -- a legitimate state, and the reason this
    never returns 0 for a source with no alpha channel. 2 means one bit of
    alpha, which is what DXT1 leaves behind. Anything more is a gradient that
    only DXT5 can carry.
    """
    data = source if isinstance(source, (bytes, bytearray)) else Path(source).read_bytes()
    if data[: len(PNG_MAGIC)] != PNG_MAGIC:
        raise PaaError("not a PNG: the alpha of a source can only be counted before conversion")

    header = None
    trns: bytes | None = None
    palette_entries = 0
    idat = bytearray()
    for kind, body in _png_chunks(bytes(data)):
        if kind == b"IHDR":
            header = struct.unpack(">IIBBBBB", body[:13])
        elif kind == b"PLTE":
            palette_entries = len(body) // 3
        elif kind == b"tRNS":
            trns = body
        elif kind == b"IDAT":
            idat += body
        elif kind == b"IEND":
            break
    if header is None:
        raise PaaError("PNG has no IHDR chunk")
    width, height, depth, ctype, _compression, _filter, interlace = header
    if interlace:
        raise PaaError(
            "PNG is interlaced (Adam7); this reader does not rearrange the passes. "
            "Re-save the source without interlacing."
        )
    if ctype not in _PNG_CHANNELS:
        raise PaaError(f"unsupported PNG colour type {ctype}")

    if ctype not in (4, 6):
        # No alpha channel. `tRNS` may still make one colour or some palette
        # entries transparent -- that is binary alpha, and it is counted as
        # such.
        if trns is None:
            return 1
        if ctype == 3:
            levels = set(trns)
            # The table is allowed to stop short of the palette; every entry
            # past its end is fully opaque.
            if palette_entries > len(trns):
                levels.add(255)
            return max(1, len(levels))
        return 2

    if depth not in (8, 16):
        raise PaaError(f"PNG colour type {ctype} cannot carry {depth} bits per sample")
    channels = _PNG_CHANNELS[ctype]
    sample = depth // 8
    bpp = channels * sample
    stride = (width * channels * depth + 7) // 8
    try:
        raw = zlib.decompress(bytes(idat))
    except zlib.error as exc:
        raise PaaError(f"PNG pixel data will not decompress: {exc}") from exc
    if len(raw) < height * (stride + 1):
        raise PaaError("PNG pixel data is shorter than its own header declares")

    # Alpha is the last channel; at depth 16 its high byte alone carries the
    # level, which is why the stride is in samples rather than in bytes.
    offset = (channels - 1) * sample
    seen: set[int] = set()
    prev = bytes(stride)
    pos = 0
    for _row in range(height):
        ftype = raw[pos]
        line = bytearray(raw[pos + 1 : pos + 1 + stride])
        pos += 1 + stride
        _unfilter(line, prev, bpp, ftype)
        seen.update(line[offset::bpp])
        prev = bytes(line)
    return len(seen)


# ------------------------------------------------------------- the conversion


def convert(
    image_to_paa: str | os.PathLike[str],
    source: str | os.PathLike[str],
    output: str | os.PathLike[str],
    log_path: str | os.PathLike[str],
    *,
    timeout: float = 300,
) -> ConvertResult:
    """Run `ImageToPAA` once, then judge what is on the disk.

    Direction comes from the extensions, which is how the tool itself decides,
    so an unsupported pair is refused HERE -- before anything is started,
    because the tool answers some of them with a success code and no file.
    """
    src, dst, log = Path(source), Path(output), Path(log_path)
    outcome = ConvertResult(ok=False, source=str(src), output=str(dst))

    src_kind, dst_kind = src.suffix.lower(), dst.suffix.lower()
    if src_kind not in CONVERTIBLE or dst_kind not in CONVERTIBLE or src_kind == dst_kind:
        return replace(outcome, error=(
            f"cannot convert {src_kind or '(no extension)'} to {dst_kind or '(no extension)'}: "
            f"this converts between {' and '.join(sorted(CONVERTIBLE))} only"
        ))
    if not src.is_file():
        return replace(outcome, error=f"{src} is not there")

    dst.parent.mkdir(parents=True, exist_ok=True)
    # The destination is removed first so a stale file from an earlier run can
    # never be mistaken for this run's output -- the tool leaves the previous
    # one in place when it refuses to load a source.
    dst.unlink(missing_ok=True)
    code, tail = run_blocking([str(image_to_paa), str(src), str(dst)], dst.parent, log, timeout)
    outcome = replace(outcome, code=code, tail=tail)

    if not dst.exists():
        return replace(outcome, error=(
            f"ImageToPAA exited {code} and produced no {dst.name}: {tail[-300:]}"
        ))
    outcome = replace(outcome, size=dst.stat().st_size)
    if outcome.size == 0:
        return replace(outcome, error=f"{dst} was written empty")
    if dst_kind == ".paa":
        with dst.open("rb") as fh:
            head = fh.read(2)
        outcome = replace(outcome, format=paa_format(head))
        if outcome.format == UNKNOWN:
            return replace(outcome, error=(
                f"{dst} carries no recognised paa signature ({head.hex()})"
            ))
    if code != 0:
        return replace(outcome, error=f"ImageToPAA exited {code}: {tail[-300:]}")
    return replace(outcome, ok=True)
