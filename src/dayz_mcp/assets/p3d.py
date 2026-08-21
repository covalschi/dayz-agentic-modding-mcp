"""Reading a `.p3d` without a modelling tool.

Two formats share the extension. `MLOD` is what a modeller exports -- editable,
one `P3DM` block per level of detail. `ODOL` is what `binarize` produces and
what the engine loads. Telling them apart is not cosmetic: feeding an `ODOL`
back into `binarize` was measured to crash it (`0xC0000005`) and leave a
ZERO-LENGTH file in the output directory, so the kind has to be known before
anything is started.

**Why there is a fingerprint here instead of a content hash.** Three exports of
one unmodified source file produced three different SHA-256 hashes at a
constant 334 032 bytes; the difference is an ordering permutation inside a
`#SharpEdges#` TAGG. Content hashing therefore cannot answer "did the model
change" and cannot key a build cache. What is stable across a re-export is the
STRUCTURE: the size, the number of LODs, and the set of names. That is the
fingerprint.

**Why the strings are read the way they are.** These files are mostly float
payload, and float payload produces printable runs constantly -- a plain
`strings` scan of one small jar returns hundreds of them, most of it noise that
moves whenever a vertex does. Two filters are applied, and both are load
bearing:

1. A name is NUL-terminated in both formats. A printable run that is not is not
   a name.
2. A name looks like an identifier, a path with an extension, a procedural
   texture (`#(argb,8,8,3)color(...)`) or a TAGG (`#SharpEdges#`). Nothing else
   is admitted.

Together they take one real ODOL from 332 printable runs to 51 names, and the
survivors are exactly the bones, the selections, the textures and the
materials. Compressed regions still leak eight-byte fragments of real strings
(`akm_fort`, `radiatio`); a fragment that survives both filters is accepted as
a name and is harmless, because nothing here treats an unknown identifier as
evidence of anything.

What is deliberately NOT done: decompressing the LOD blocks. Everything the
checks need -- the kind, the LOD count, the texture and material paths, the
bone and selection names -- lies in the plain regions, and an LZO decompressor
would be a second proprietary format to get subtly wrong for no new answer.
"""
from __future__ import annotations

import hashlib
import os
import re
import struct
from dataclasses import dataclass
from pathlib import Path

MLOD = "MLOD"
ODOL = "ODOL"
UNKNOWN = "UNKNOWN"

#: A model with more LODs than this is not a model -- the word at offset 8 is
#: only a count in a file that really is a p3d, and believing a garbage value
#: there would turn a truncated file into a model with four billion LODs. Real
#: ones carry a handful: four and five on this machine's own exports.
MAX_LODS = 256

#: The strings `binarize` inlines into an ODOL when it successfully RESOLVES an
#: rvmat -- it copies that rvmat's own stage textures into the model. Measured
#: on six samples with no exceptions: a build made from the correct working
#: directory carries all five, and a build made from the wrong one carries
#: none, while both are valid ODOL files with plausible-looking texture paths.
#: This is the only cheap signal that separates them, and it found a broken
#: artifact nobody knew about.
RVMAT_INLINE_MARKERS: tuple[str, ...] = (
    "fresnel", "#(argb,8,8,3)", "env_land_co.paa", "_nohq.paa", "_smdi.paa",
)

_MAGIC = {b"MLOD": MLOD, b"ODOL": ODOL}

#: A printable run, NUL-terminated. Three characters is the floor: shorter
#: names exist in theory but every one seen in the corpus is noise.
_ASCIIZ = re.compile(rb"[\x20-\x7e]{3,}\x00")

# The shapes admitted as names. Identifiers start at four characters because
# three-character alphanumeric runs out of float payload are common and
# three-character selection names are not; the cost is that a genuine `lod`
# would be missed, which is why `asciiz_strings` stays available as the
# unfiltered superset for membership questions.
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{3,}$")
# A path reference. The first segment may also be a relative escape (`..`) or
# a drive (`e:`) -- both are DEFECTS the checks above this module exist to
# refuse, so a reader that filtered them out as malformed would make its most
# valuable check unable to ever fire.
_PATH = re.compile(
    r"^(?:[A-Za-z]:|\.{1,2}|[A-Za-z0-9_][A-Za-z0-9_ +.-]*)"
    r"(?:[\\/][A-Za-z0-9_ +.-]+)+\.[A-Za-z0-9]{2,6}$"
)
_PROCEDURAL = re.compile(r"^#\(.+\)$")
_TAGG = re.compile(r"^#[A-Za-z]+#$")


class P3dError(Exception):
    """The file cannot be read as a p3d at all.

    Raised only for a file that is missing or empty -- both states a caller has
    to report differently from "a model without textures". A file that exists
    and holds bytes is always parsed, and an unrecognised magic comes back as
    `kind == UNKNOWN` rather than as an exception, because a check has to be
    able to say WHAT it was handed.
    """


@dataclass(frozen=True)
class P3dInfo:
    """Everything this reader will claim about one file."""

    path: str
    size: int
    kind: str
    version: int | None
    lod_count: int | None
    strings: tuple[str, ...]

    @property
    def textures(self) -> tuple[str, ...]:
        """Texture references that are paths. Procedural textures are not
        here: they have no path, and a check that asked whether they start
        with the mod's prefix would refuse every correct build."""
        return tuple(s for s in self.strings if _is_path(s) and s.lower().endswith(".paa"))

    @property
    def materials(self) -> tuple[str, ...]:
        return tuple(s for s in self.strings if _is_path(s) and s.lower().endswith(".rvmat"))

    @property
    def procedural(self) -> tuple[str, ...]:
        return tuple(s for s in self.strings if _PROCEDURAL.match(s))

    @property
    def prefixes(self) -> tuple[str, ...]:
        """The first path segment of every reference, lowercased.

        Lowercased because DayZ paths are case-insensitive and this exists to
        be compared against a declared mod prefix. The paths themselves are
        reported exactly as they lie in the file -- a check has to be able to
        quote what is really written there.
        """
        first = {
            s.replace("\\", "/").split("/", 1)[0].lower()
            for s in (*self.textures, *self.materials)
        }
        return tuple(sorted(first))


@dataclass(frozen=True)
class Fingerprint:
    """What survives a re-export of an unchanged source.

    `kind` is in it so a source file sitting where a built artifact belongs can
    never answer "is the artifact current" with yes.
    """

    kind: str
    size: int
    lod_count: int | None
    strings: tuple[str, ...]
    digest: str


def _is_path(s: str) -> bool:
    return bool(_PATH.match(s))


def is_name(s: str) -> bool:
    """Whether a run reads as something a person named, rather than payload."""
    return bool(_IDENT.match(s) or _PATH.match(s) or _PROCEDURAL.match(s) or _TAGG.match(s))


def asciiz_strings(data: bytes, *, start: int = 0) -> tuple[str, ...]:
    """Every NUL-terminated printable run in `data`, sorted and deduplicated.

    The unfiltered superset. Use it to ask whether a specific name is present
    -- an animation source, a bone -- where missing a short real name would
    raise a false alarm. Use `P3dInfo.strings` for anything that has to be
    stable across a re-export.
    """
    found = {m.group(0)[:-1].decode("ascii") for m in _ASCIIZ.finditer(data[start:])}
    return tuple(sorted(found))


def kind_of(data: bytes) -> tuple[str, int | None]:
    """The format and its version word, or `(UNKNOWN, None)`."""
    if len(data) < 8:
        return UNKNOWN, None
    kind = _MAGIC.get(bytes(data[:4]))
    if kind is None:
        return UNKNOWN, None
    return kind, struct.unpack_from("<I", data, 4)[0]


def _lod_count(data: bytes, kind: str) -> int | None:
    if kind == UNKNOWN or len(data) < 12:
        return None
    count = struct.unpack_from("<I", data, 8)[0]
    if not 1 <= count <= MAX_LODS:
        return None
    # An ODOL follows the count with one resolution float per LOD. A count
    # with no room for its own table is not a count -- that is a truncated
    # file, and the caller has to hear "unknown" rather than a number.
    if kind == ODOL and len(data) < 12 + 4 * count:
        return None
    return count


def parse_p3d(data: bytes, *, path: str = "") -> P3dInfo:
    """Read a p3d that is already in memory."""
    kind, version = kind_of(data)
    # The magic and its version word are not names: scanned in, an ODOL would
    # carry "ODOL7" (the version byte 0x37 is the digit 7) in every string set.
    start = 8 if kind != UNKNOWN else 0
    strings = tuple(s for s in asciiz_strings(data, start=start) if is_name(s))
    return P3dInfo(
        path=path,
        size=len(data),
        kind=kind,
        version=version,
        lod_count=_lod_count(data, kind),
        strings=strings,
    )


def read_p3d(path: str | os.PathLike[str]) -> P3dInfo:
    """Read a p3d off the disk.

    Refuses a missing or an empty file BY NAME. Zero length is not a corner
    case here: it is the measured signature of a `binarize` crash, and an empty
    artifact that read as "a model with no strings" would pass every check that
    asks what a model contains.
    """
    p = Path(path)
    try:
        data = p.read_bytes()
    except FileNotFoundError as exc:
        raise P3dError(f"{p} is not there: nothing was built, or it was built elsewhere") from exc
    except OSError as exc:
        raise P3dError(f"{p} cannot be read: {exc}") from exc
    if not data:
        raise P3dError(
            f"{p} is empty: binarize crashes on an already-binarized model and leaves "
            "a zero-length file behind. Build from the MLOD source instead."
        )
    return parse_p3d(data, path=str(p))


def inlined_material_markers(info: P3dInfo) -> tuple[str, ...]:
    """Which of `RVMAT_INLINE_MARKERS` this artifact carries, in that order.

    All five means the rvmat resolved. None means it did not, which is what a
    run from the wrong working directory produces -- silently, with a valid
    file and a success exit code.

    Only ever meaningful for an ODOL: an MLOD legitimately carries none of
    them, because inlining is something `binarize` does.
    """
    haystack = "\n".join(info.strings).lower()
    return tuple(m for m in RVMAT_INLINE_MARKERS if m.lower() in haystack)


def fingerprint(info: P3dInfo) -> Fingerprint:
    """The structural identity of a model. See the module docstring for why
    this exists instead of a hash of the bytes."""
    canonical = "\n".join((info.kind, str(info.size), str(info.lod_count), *info.strings))
    return Fingerprint(
        kind=info.kind,
        size=info.size,
        lod_count=info.lod_count,
        strings=info.strings,
        digest=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    )
