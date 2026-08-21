"""Reading `.paa` artifacts, and converting to and from them.

The PNG helper below is a deliberate second implementation: it applies the
PNG filters forward, while the module reverses them, so a mistake has to be
made twice in opposite directions to pass. The corpus tests at the bottom then
put both against real textures.

    DAYZ_MCP_SAMPLE_PNG_ALPHA   a PNG whose alpha is a gradient, not on/off
    DAYZ_TOOLS                  DayZ Tools, for the ImageToPAA round trip
"""
from __future__ import annotations

import os
import struct
import zlib
from pathlib import Path

import pytest

from dayz_mcp.assets.paa import (
    DXT1,
    DXT5,
    UNKNOWN,
    PaaError,
    alpha_levels,
    convert,
    expected_format,
    paa_format,
    read_paa,
)
from dayz_mcp.paths import IMAGETOPAA_REL, find_tools

# ------------------------------------------------------------- a PNG, by hand

CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}


def _chunk(kind: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))


def _filter(line: bytes, prev: bytes, bpp: int, ftype: int) -> bytes:
    out = bytearray(len(line))
    for i, raw in enumerate(line):
        left = line[i - bpp] if i >= bpp else 0
        up = prev[i]
        upleft = prev[i - bpp] if i >= bpp else 0
        if ftype == 0:
            out[i] = raw
        elif ftype == 1:
            out[i] = (raw - left) & 0xFF
        elif ftype == 2:
            out[i] = (raw - up) & 0xFF
        elif ftype == 3:
            out[i] = (raw - ((left + up) >> 1)) & 0xFF
        else:
            p = left + up - upleft
            pa, pb, pc = abs(p - left), abs(p - up), abs(p - upleft)
            pred = left if (pa <= pb and pa <= pc) else (up if pb <= pc else upleft)
            out[i] = (raw - pred) & 0xFF
    return bytes(out)


def png(
    rows: list[bytes],
    *,
    width: int,
    ctype: int = 6,
    depth: int = 8,
    ftype: int = 0,
    trns: bytes | None = None,
    palette: bytes | None = None,
    interlace: int = 0,
) -> bytes:
    """A PNG carrying `rows` (already in sample order) with one filter type."""
    bpp = max(1, CHANNELS[ctype] * depth // 8)
    body = bytearray()
    prev = bytes(len(rows[0]))
    for line in rows:
        body.append(ftype)
        body += _filter(line, prev, bpp, ftype)
        prev = line
    out = b"\x89PNG\r\n\x1a\n"
    out += _chunk(b"IHDR", struct.pack(">IIBBBBB", width, len(rows), depth, ctype, 0, 0, interlace))
    if palette is not None:
        out += _chunk(b"PLTE", palette)
    if trns is not None:
        out += _chunk(b"tRNS", trns)
    out += _chunk(b"IDAT", zlib.compress(bytes(body)))
    out += _chunk(b"IEND", b"")
    return out


def rgba(pixels: list[list[tuple[int, int, int, int]]]) -> bytes:
    return png([bytes(v for px in row for v in px) for row in pixels], width=len(pixels[0]))


# ------------------------------------------------------------ paa signatures


def test_the_two_measured_signatures_are_read_off_the_first_two_bytes():
    """`01ff` and `05ff` as they lie in the file -- the whole point of checking
    the signature rather than "does it have an alpha channel"."""
    assert paa_format(b"\x01\xff" + b"rest") == DXT1
    assert paa_format(b"\x05\xff" + b"rest") == DXT5


def test_an_unrecognised_signature_is_UNKNOWN_rather_than_a_guess():
    assert paa_format(b"\x00\x00") == UNKNOWN
    assert paa_format(b"\x01") == UNKNOWN
    assert paa_format(b"") == UNKNOWN


def test_read_paa_reports_the_signature_as_hex_the_way_a_dump_shows_it(tmp_path):
    p = tmp_path / "thing_co.paa"
    p.write_bytes(b"\x01\xff" + b"\x00" * 30)
    info = read_paa(p)
    assert info.signature == "01ff"
    assert info.format == DXT1
    assert info.size == 32
    assert info.path == str(p)


def test_a_zero_length_paa_is_refused_by_name(tmp_path):
    p = tmp_path / "empty.paa"
    p.write_bytes(b"")
    with pytest.raises(PaaError) as excinfo:
        read_paa(p)
    assert "empty" in str(excinfo.value).lower()


def test_a_missing_paa_is_refused_by_name(tmp_path):
    with pytest.raises(PaaError):
        read_paa(tmp_path / "nothing.paa")


# ------------------------------------------------------- suffix drives format


def test_the_suffix_decides_the_format_and_no_suffix_means_dxt5():
    """Measured on the real tool: the SOURCE file name is what ImageToPAA
    reads the intent off, so the suffix is not documentation, it is input."""
    assert expected_format("thing_co.png") == DXT1
    assert expected_format("thing_ca.png") == DXT5
    assert expected_format("thing.png") == DXT5
    assert expected_format("thing_nohq.png") == DXT5


def test_the_suffix_is_matched_on_the_stem_not_anywhere_in_the_path():
    """A directory called `..._co` upstream of the file must not decide the
    format of everything under it."""
    assert expected_format(r"mod\data\co_textures\thing_ca.png") == DXT5
    assert expected_format(r"mod\data\ca\thing_co.png") == DXT1


# --------------------------------------------------------------- alpha levels


def test_a_fully_opaque_source_has_one_alpha_level():
    data = rgba([[(1, 2, 3, 255), (4, 5, 6, 255)], [(7, 8, 9, 255), (0, 0, 0, 255)]])
    assert alpha_levels(data) == 1


def test_a_gradient_alpha_is_counted_level_by_level():
    data = rgba([[(0, 0, 0, 0), (0, 0, 0, 64)], [(0, 0, 0, 128), (0, 0, 0, 255)]])
    assert alpha_levels(data) == 4


def test_one_bit_alpha_and_a_gradient_are_distinguishable():
    """The measurement the whole check rests on: two levels is what survives
    DXT1, and it must not read the same as a source that had more."""
    flat = rgba([[(0, 0, 0, 0), (0, 0, 0, 255)]])
    graded = rgba([[(0, 0, 0, 0), (0, 0, 0, 96), (0, 0, 0, 255)]])
    assert alpha_levels(flat) == 2
    assert alpha_levels(graded) == 3


@pytest.mark.parametrize("ftype", [0, 1, 2, 3, 4])
def test_every_png_filter_type_is_reversed(ftype):
    """A decoder that gets Sub right and Paeth wrong reports a plausible
    number for the wrong reason. Each of the five is exercised."""
    rows = [
        bytes(v for px in row for v in px)
        for row in (
            [(10, 20, 30, 0), (40, 50, 60, 64)],
            [(70, 80, 90, 128), (100, 110, 120, 255)],
        )
    ]
    assert alpha_levels(png(rows, width=2, ftype=ftype)) == 4


def test_greyscale_with_alpha_is_read_on_its_own_channel():
    rows = [bytes([10, 0, 20, 255]), bytes([30, 128, 40, 255])]
    assert alpha_levels(png(rows, width=2, ctype=4)) == 3


def test_sixteen_bit_alpha_is_read_at_its_own_stride():
    """Depth 16 doubles every sample. Reading it at the 8-bit stride would
    count the high bytes of the colour channels as alpha."""
    def row(alphas):
        return b"".join(struct.pack(">HHHH", 1, 2, 3, a) for a in alphas)

    assert alpha_levels(png([row([0, 65535]), row([32768, 65535])], width=2, depth=16)) == 3


def test_a_source_with_no_alpha_channel_is_one_level_not_zero():
    """An RGB texture is legitimately opaque. Reporting 0 would read as "the
    alpha was destroyed", which is the opposite of the truth."""
    rows = [bytes([1, 2, 3, 4, 5, 6])]
    assert alpha_levels(png(rows, width=2, ctype=2)) == 1


def test_a_binary_trns_on_an_rgb_source_counts_as_two():
    rows = [bytes([1, 2, 3, 4, 5, 6])]
    assert alpha_levels(png(rows, width=2, ctype=2, trns=struct.pack(">HHH", 1, 2, 3))) == 2


def test_a_palette_alpha_table_is_counted_from_the_table():
    rows = [bytes([0, 1, 2])]
    palette = bytes([0, 0, 0, 1, 1, 1, 2, 2, 2])
    data = png(rows, width=3, ctype=3, palette=palette, trns=bytes([0, 128, 255]))
    assert alpha_levels(data) == 3


def test_palette_entries_the_alpha_table_does_not_reach_are_opaque():
    """`tRNS` may be shorter than the palette; everything past its end is fully
    opaque. Counting only the table would under-report a palette that is
    part transparent and part solid."""
    rows = [bytes([0, 1, 2])]
    palette = bytes([0, 0, 0, 1, 1, 1, 2, 2, 2])
    data = png(rows, width=3, ctype=3, palette=palette, trns=bytes([0, 128]))
    assert alpha_levels(data) == 3  # 0, 128, and the implied 255


def test_a_bit_depth_the_format_does_not_allow_is_refused():
    """Colour types 4 and 6 carry 8 or 16 bits per sample and nothing else. A
    reader that took another value on trust would step through the pixel data
    at the wrong stride and return a plausible wrong number."""
    with pytest.raises(PaaError):
        alpha_levels(png([bytes([0, 255])], width=1, ctype=6, depth=4))


def test_an_interlaced_source_is_refused_rather_than_mis_read():
    """Adam7 rearranges the scanlines. A decoder that ignored the flag would
    return a number that looks fine and is wrong -- exactly the failure this
    whole phase exists to stop."""
    rows = [bytes([0, 0, 0, 0, 0, 0, 0, 255])]
    with pytest.raises(PaaError) as excinfo:
        alpha_levels(png(rows, width=2, interlace=1))
    assert "interlac" in str(excinfo.value).lower()


def test_something_that_is_not_a_png_is_refused(tmp_path):
    p = tmp_path / "thing.png"
    p.write_bytes(b"not a png at all")
    with pytest.raises(PaaError):
        alpha_levels(p)


def test_alpha_levels_accepts_a_path_as_well_as_bytes(tmp_path):
    p = tmp_path / "thing.png"
    p.write_bytes(rgba([[(0, 0, 0, 0), (0, 0, 0, 255)]]))
    assert alpha_levels(p) == 2


# ------------------------------------------------------------- the conversion


def test_convert_refuses_a_direction_the_tool_does_not_do(tmp_path):
    """ImageToPAA reads the direction off the extensions. Asked for one it does
    not implement it still exits 0 on some inputs, so the pair is checked here,
    before anything is started."""
    src = tmp_path / "a.png"
    src.write_bytes(rgba([[(0, 0, 0, 255)]]))
    result = convert(tmp_path / "nope.exe", src, tmp_path / "a.tga", tmp_path / "log.txt")
    assert not result.ok
    assert ".tga" in result.error


def test_convert_refuses_a_missing_source_before_starting_anything(tmp_path):
    result = convert(tmp_path / "nope.exe", tmp_path / "gone.png", tmp_path / "a.paa",
                     tmp_path / "log.txt")
    assert not result.ok
    assert not (tmp_path / "log.txt").exists()


def test_convert_reports_a_tool_that_cannot_be_started(tmp_path):
    src = tmp_path / "a.png"
    src.write_bytes(rgba([[(0, 0, 0, 255)]]))
    result = convert(tmp_path / "missing.exe", src, tmp_path / "a.paa", tmp_path / "log.txt")
    assert not result.ok
    assert result.error


# ------------------------------------------------- the real corpus, if present

SOURCE = Path(os.environ.get("DAYZ_MCP_SAMPLE_PNG_ALPHA", ""))
TOOLS = find_tools()
IMAGETOPAA = Path(TOOLS or "") / IMAGETOPAA_REL

needs_source = pytest.mark.skipif(
    not (SOURCE.name and SOURCE.is_file()),
    reason="set DAYZ_MCP_SAMPLE_PNG_ALPHA to a PNG with graded alpha to run",
)
needs_tool = pytest.mark.skipif(
    not (TOOLS and IMAGETOPAA.is_file()),
    reason="DayZ Tools not installed on this machine",
)


@needs_source
def test_a_real_source_texture_has_more_than_one_bit_of_alpha():
    assert alpha_levels(SOURCE) > 2


@needs_source
@needs_tool
def test_the_co_suffix_quantises_a_real_gradient_to_one_bit(tmp_path):
    """The measured law, on the real tool: alpha is not "cut", it is quantised
    to 1 bit by DXT1 -- and the suffix on the SOURCE name is what chooses DXT1.
    Two runs of the same image differing only in that suffix."""
    before = alpha_levels(SOURCE)
    assert before > 2

    graded = {}
    for suffix, want in (("_co", DXT1), ("_ca", DXT5), ("", DXT5)):
        src = tmp_path / f"probe{suffix}.png"
        src.write_bytes(SOURCE.read_bytes())
        paa = tmp_path / f"probe{suffix}.paa"
        made = convert(IMAGETOPAA, src, paa, tmp_path / f"to{suffix or '_none'}.log")
        assert made.ok, made.error
        assert made.format == want, f"{suffix!r} -> {made.format}"

        back = tmp_path / f"back{suffix or '_none'}.png"
        undone = convert(IMAGETOPAA, paa, back, tmp_path / f"from{suffix or '_none'}.log")
        assert undone.ok, undone.error
        graded[suffix] = alpha_levels(back)

    assert graded["_co"] == 2, graded
    assert graded["_ca"] > 2, graded
    assert graded[""] > 2, graded


@needs_source
@needs_tool
def test_the_conversion_verifies_the_artifact_not_the_exit_code(tmp_path):
    """A conversion that reports success and wrote nothing is a failure here.
    The tool exits 1 and writes no file on a source it cannot load."""
    broken = tmp_path / "broken_co.png"
    broken.write_bytes(b"this is not an image")
    result = convert(IMAGETOPAA, broken, tmp_path / "broken_co.paa", tmp_path / "log.txt")
    assert not result.ok
    assert not (tmp_path / "broken_co.paa").exists()
    assert result.error
