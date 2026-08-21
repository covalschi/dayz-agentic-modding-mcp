"""Reading a PBO without unpacking it.

A dependency layer over this machine's modpack means 523 archives holding
86 GB, of which the index wants 56 MB: the scripts and the configs. Unpacking
that with BankRev would write eighty-six gigabytes through the disk to read
fifty-six megabytes, and would have to be redone whenever one mod is
republished. So the archive is read where it lies: the entry table is walked,
the wanted entries are seeked to directly, and nothing else is ever touched.

The algorithm is ported, not the file: the same scan is already proven in
another project of this hub (469 archives in 3.7 s, with its LZSS output
verified byte-identical against Bohemia's own BankRev). What is new here is
that it never holds an archive in memory -- entries are found by streaming the
table and read by offset -- because the archives it now has to survive are two
gigabytes each.

**Obfuscated archives are the normal case, not the exception.** Measured on
this machine's 36 installed mods: 7.6 million entries, of which 4 596 hold
anything that parses. The padding is entries whose names carry zero-width
characters and reserved Windows device names, each a few dozen bytes of noise.
Three archives go further and carry a 252 MB entry table with two million
entries; those are refused by a stated ceiling rather than walked, and the
layer above reports them by name. A limit that is announced costs one line in
a report; a reader that quietly runs out of memory costs the whole build.

What is deliberately NOT done here: judging an entry by its contents. The
obvious filter -- "mostly printable bytes, therefore a real script" -- was
measured against the real modpack and rejected: it throws away real configs,
because a mod's display names are written in the language its players speak,
and a UTF-8 Cyrillic config fails a printable-ASCII ratio test. Decoys are
filtered by name, and whatever survives is handed to the parser, which finds
nothing in noise and costs nothing for the privilege.
"""
from __future__ import annotations

import io
import os
import re
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Callable, Iterator

#: Entry packing markers, as they appear in the header (little-endian ASCII).
CPRS = 0x43707273  # 'Cprs' -- LZSS compressed
VERS = 0x56657273  # 'Vers' -- the properties pseudo-entry

_CHUNK = 1 << 20
_ENTRY = struct.Struct("<IIIII")  # packing, original, reserved, timestamp, size


class PboError(Exception):
    """The archive cannot be read as far as the index needs it.

    Always carries what stopped it, because the caller's job is to name this
    archive in a report rather than to pretend it was empty.
    """


@dataclass(frozen=True)
class PboLimits:
    """Ceilings, stated rather than discovered under memory pressure.

    `header_bytes` is generous on purpose: a legitimate archive's entry table
    is a few hundred kilobytes, while an obfuscated one on this machine
    reaches 18 MB and still holds real code. The three that reach 252 MB do
    not, and stop here.
    """

    header_bytes: int = 64 << 20
    max_entries: int = 1_000_000
    max_entry_bytes: int = 64 << 20


DEFAULT_LIMITS = PboLimits()


@dataclass(frozen=True)
class PboEntry:
    """One file inside the archive, and where its bytes start in it."""

    name: str
    packing: int
    original: int
    size: int
    offset: int

    @property
    def compressed(self) -> bool:
        return self.packing == CPRS


#: Everything a filename may contain before this reader stops believing in it.
#: Printable ASCII only: the padding in obfuscated archives leans on zero-width
#: joiners and byte-order marks to make thousands of distinct names that all
#: look empty.
_PLAIN = re.compile(r"^[\x20-\x7e]+$")
_RESERVED = {"CON", "PRN", "AUX", "NUL"}


def is_decoy_name(name: str) -> bool:
    """Whether an entry name is padding rather than a file.

    Two tells, both from the real modpack: characters no build tool would put
    in a path, and MS-DOS device names (`COM1`, `LPT2`) that Windows itself
    refuses to open. Neither can appear in a name that a mod's author typed.
    """
    if not name or not _PLAIN.match(name):
        return True
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    stem = base.split(".", 1)[0].upper().strip()
    if stem in _RESERVED:
        return True
    return stem[:3] in ("COM", "LPT") and stem[3:4].isdigit()


def lzss_decompress(data: bytes, expected: int) -> bytes:
    """Bohemia's distance-based LZ77, as used for 'Cprs' entries.

    Distance counts back from the current output position, `dist == 0` means
    4096, and positions before the start of the output read as 0x20. The
    classic space-filled ring-buffer variant decodes the first bytes correctly
    and then diverges, which is the worst possible failure mode: output that
    looks right until it does not.
    """
    out = bytearray()
    i = 0
    n = len(data)
    while len(out) < expected and i < n:
        flags = data[i]
        i += 1
        for bit in range(8):
            if len(out) >= expected or i >= n:
                break
            if flags & (1 << bit):
                out.append(data[i])
                i += 1
                continue
            if i + 1 >= n:
                break
            b1 = data[i]
            b2 = data[i + 1]
            i += 2
            dist = b1 | ((b2 & 0xF0) << 4)
            if dist == 0:
                dist = 4096
            length = (b2 & 0x0F) + 3
            src = len(out) - dist
            if src >= 0 and src + length <= len(out):
                out += out[src : src + length]
                if len(out) > expected:
                    del out[expected:]
                continue
            for k in range(length):
                if len(out) >= expected:
                    break
                out.append(0x20 if src + k < 0 else out[src + k])
    return bytes(out)


class _Header:
    """The entry table, read in chunks and never past its ceiling."""

    def __init__(self, fh: BinaryIO, cap: int):
        self._fh = fh
        self._cap = cap
        self.buf = bytearray()

    def need(self, upto: int) -> None:
        while len(self.buf) < upto:
            if len(self.buf) >= self._cap:
                raise PboError(
                    f"entry table exceeds {self._cap} bytes: the archive is padded "
                    "beyond what this reader will walk"
                )
            chunk = self._fh.read(min(_CHUNK, self._cap - len(self.buf)))
            if not chunk:
                raise PboError("header truncated: no entry table terminator")
            self.buf += chunk

    def zstr(self, pos: int) -> tuple[bytes, int]:
        end = self.buf.find(b"\x00", pos)
        while end < 0:
            before = len(self.buf)
            self.need(before + 1)
            end = self.buf.find(b"\x00", before)
        return bytes(self.buf[pos:end]), end + 1


def read_index(
    source: bytes | BinaryIO,
    *,
    wanted: Callable[[str], bool] | None = None,
    limits: PboLimits = DEFAULT_LIMITS,
) -> tuple[list[PboEntry], dict[str, str], int]:
    """The entries `wanted` accepts, with absolute data offsets.

    Only accepted entries are kept: an archive here routinely declares half a
    million of them, and materialising the ones nobody asked for turns a
    reader into a memory problem. Offsets still account for every skipped
    entry, because the data section is one concatenation in table order.

    Returns `(entries, properties, data_start)`.
    """
    fh: BinaryIO = io.BytesIO(source) if isinstance(source, (bytes, bytearray)) else source
    keep = wanted or (lambda _name: True)
    head = _Header(fh, limits.header_bytes)
    props: dict[str, str] = {}
    entries: list[PboEntry] = []
    pos = 0
    seen = 0
    running = 0
    while True:
        raw, pos = head.zstr(pos)
        head.need(pos + _ENTRY.size)
        packing, original, _reserved, _stamp, size = _ENTRY.unpack_from(head.buf, pos)
        pos += _ENTRY.size
        if packing == VERS:
            while True:
                key, pos = head.zstr(pos)
                if not key:
                    break
                value, pos = head.zstr(pos)
                props[key.decode("utf-8", "replace")] = value.decode("utf-8", "replace")
            continue
        if not raw and packing == 0 and size == 0:
            break
        seen += 1
        if seen > limits.max_entries:
            raise PboError(
                f"more than {limits.max_entries} entries: the archive is padded "
                "beyond what this reader will walk"
            )
        name = raw.decode("utf-8", "replace")
        if keep(name):
            entries.append(PboEntry(name, packing, original, size, running))
            running += size
        else:
            running += size
    data_start = pos
    return (
        [
            PboEntry(e.name, e.packing, e.original, e.size, e.offset + data_start)
            for e in entries
        ],
        props,
        data_start,
    )


def read_entry(fh: BinaryIO, entry: PboEntry) -> bytes:
    """One entry's bytes, decompressed if it was stored compressed."""
    fh.seek(entry.offset)
    raw = fh.read(entry.size)
    if entry.compressed and 0 < entry.original:
        return lzss_decompress(raw, entry.original)
    return raw


def scan_pbo(
    path: str | os.PathLike[str],
    wanted: Callable[[str], bool] | None = None,
    *,
    limits: PboLimits = DEFAULT_LIMITS,
    skip_decoys: bool = True,
) -> Iterator[tuple[PboEntry, bytes]]:
    """Every wanted entry of one archive, name and contents.

    Reads the table once, then seeks straight to each entry. Nothing that was
    not asked for is ever read off the disk.
    """
    keep = wanted or (lambda _name: True)

    def accept(name: str) -> bool:
        if skip_decoys and is_decoy_name(name):
            return False
        return keep(name)

    with Path(path).open("rb") as fh:
        entries, _props, _start = read_index(fh, wanted=accept, limits=limits)
        for entry in entries:
            if entry.size <= 0 or entry.size > limits.max_entry_bytes:
                continue
            if entry.original > limits.max_entry_bytes:
                continue
            yield entry, read_entry(fh, entry)
