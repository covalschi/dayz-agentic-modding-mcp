"""Reading `.p3d` artifacts.

Hermetic tests pin the shapes; the corpus tests at the bottom prove the shapes
were real. Corpus samples are named by the environment rather than hard-coded,
exactly as the knowledge parser's own corpus test is: this repository must stay
portable, and no test may depend on one machine's models. Each variable names
a sample by the PROPERTY the test needs, never by the mod it came from --

    DAYZ_MCP_SAMPLE_ODOL              a binarized model that works in game
    DAYZ_MCP_SAMPLE_ODOL_ALT          a second one, from a different mod
    DAYZ_MCP_SAMPLE_ODOL_NO_RVMAT     one built from the wrong working directory
    DAYZ_MCP_SAMPLE_ODOL_FOREIGN      one whose texture paths lost their prefix
    DAYZ_MCP_SAMPLE_MLOD              an MLOD export, as it leaves the modeller
"""
from __future__ import annotations

import os
import struct
from pathlib import Path

import pytest

from dayz_mcp.assets.p3d import (
    MLOD,
    ODOL,
    RVMAT_INLINE_MARKERS,
    UNKNOWN,
    P3dError,
    asciiz_strings,
    fingerprint,
    inlined_material_markers,
    parse_p3d,
    read_p3d,
)


def odol(lods: int = 2, tail: bytes = b"") -> bytes:
    """The smallest byte string this reader must accept as an ODOL."""
    return b"ODOL" + struct.pack("<II", 55, lods) + b"\x00" * (4 * lods) + tail


def mlod(lods: int = 3, tail: bytes = b"") -> bytes:
    return b"MLOD" + struct.pack("<II", 0x101, lods) + tail


def named(*names: str) -> bytes:
    """Names as a p3d stores them: NUL-terminated, back to back."""
    return b"".join(n.encode("ascii") + b"\x00" for n in names)


# ------------------------------------------------------------------ file kind


def test_odol_magic_is_recognised_with_its_version():
    info = parse_p3d(odol())
    assert info.kind == ODOL
    assert info.version == 55


def test_mlod_magic_is_recognised_with_its_version():
    """MLOD's version word is 0x00000101, not a small integer -- read as one it
    would make every MLOD look like a corrupt ODOL."""
    info = parse_p3d(mlod())
    assert info.kind == MLOD
    assert info.version == 0x101


def test_anything_else_is_UNKNOWN_rather_than_an_exception():
    """C1 has to say "this is not an ODOL" in a report. A reader that raises
    instead forces the check to guess what it was looking at."""
    info = parse_p3d(b"PBOX" + b"\x00" * 32)
    assert info.kind == UNKNOWN
    assert info.version is None
    assert info.lod_count is None


def test_a_file_too_short_to_hold_a_magic_is_UNKNOWN():
    assert parse_p3d(b"OD").kind == UNKNOWN


def test_a_zero_length_file_is_refused_by_name(tmp_path):
    """The measured signature of a binarize crash: exit 0xC0000005 and a
    zero-length file in the output directory. Naming it is the whole point --
    an empty artifact must never read as "a model with no strings"."""
    empty = tmp_path / "empty.p3d"
    empty.write_bytes(b"")
    with pytest.raises(P3dError) as excinfo:
        read_p3d(empty)
    assert "empty" in str(excinfo.value).lower()


def test_a_missing_file_is_refused_by_name(tmp_path):
    with pytest.raises(P3dError) as excinfo:
        read_p3d(tmp_path / "nothing.p3d")
    assert "not" in str(excinfo.value).lower()


# ------------------------------------------------------------------ lod count


def test_lod_count_is_read_from_both_kinds():
    assert parse_p3d(odol(lods=4)).lod_count == 4
    assert parse_p3d(mlod(lods=5)).lod_count == 5


def test_an_implausible_lod_count_is_reported_as_unknown_not_believed():
    """The word at offset 8 is only a LOD count in a file that really is a
    p3d. Believing a garbage value there would make a truncated file read as a
    model with four billion LODs."""
    assert parse_p3d(b"ODOL" + struct.pack("<II", 55, 0)).lod_count is None
    assert parse_p3d(b"ODOL" + struct.pack("<II", 55, 10_000)).lod_count is None


def test_an_odol_truncated_before_its_resolutions_has_no_lod_count():
    """A count of 4 with no room for four floats is not a count."""
    assert parse_p3d(b"ODOL" + struct.pack("<II", 55, 4)).lod_count is None


# ---------------------------------------------------------------- the strings


def test_only_nul_terminated_printable_runs_count_as_strings():
    """Float payload produces printable runs constantly. A run that is not
    NUL-terminated is not a name, and admitting it would make the fingerprint
    move whenever a vertex does."""
    data = odol(tail=named("surf_body") + b"\x3c\x6f\x3e\x2a")
    assert "surf_body" in asciiz_strings(data)
    assert "<o>*" not in asciiz_strings(data)


def test_binary_noise_is_kept_out_of_the_string_set():
    """`asciiz_strings` is the raw superset; `strings` is what a human would
    call a name. Both exist because membership questions ("is this bone in the
    model") want the superset and the fingerprint wants the filtered set."""
    noise = named("<*,", "=lA*,", "p}?", "l,l", "9al")
    info = parse_p3d(odol(tail=named("component01") + noise))
    assert "component01" in info.strings
    assert not {"<*,", "=lA*,", "p}?", "l,l", "9al"} & set(info.strings)
    assert "<*," in asciiz_strings(odol(tail=noise))


def test_paths_procedural_textures_and_tagg_names_all_survive_the_filter():
    data = odol(tail=named(
        r"somemod\data\textures\thing_co.paa",
        r"somemod\data\textures\thing.rvmat",
        "#(argb,8,8,3)color(1,1,1,1,dt)",
        "#SharpEdges#",
    ))
    got = set(parse_p3d(data).strings)
    assert r"somemod\data\textures\thing_co.paa" in got
    assert r"somemod\data\textures\thing.rvmat" in got
    assert "#(argb,8,8,3)color(1,1,1,1,dt)" in got
    assert "#SharpEdges#" in got


def test_a_truncated_path_fragment_does_not_pass_as_a_path():
    """Compressed regions leak eight-byte fragments of real paths. A fragment
    is not a reference, and a check that treated one as a reference would go
    looking for a file nobody named."""
    got = set(parse_p3d(odol(tail=named(r"ta\textu", r"ent\data", "co.paa"))).strings)
    assert got == set()


def test_strings_are_sorted_and_deduplicated():
    data = odol(tail=named("zulu", "alpha", "zulu", "mike"))
    assert parse_p3d(data).strings == ("alpha", "mike", "zulu")


# ------------------------------------------------ textures, materials, prefix


def test_textures_and_materials_are_separated_from_procedural_ones():
    data = odol(tail=named(
        r"somemod\data\textures\thing_co.paa",
        r"othermod\data\thing.rvmat",
        "#(ai,64,64,1)fresnel(1,0.7)",
        "surf_body",
    ))
    info = parse_p3d(data)
    assert info.textures == (r"somemod\data\textures\thing_co.paa",)
    assert info.materials == (r"othermod\data\thing.rvmat",)
    assert info.procedural == ("#(ai,64,64,1)fresnel(1,0.7)",)


def test_a_procedural_texture_is_never_reported_as_a_texture_path():
    """C3 asks whether every path starts with the mod prefix. A procedural
    texture has no path at all, and counting it as one would refuse every
    correct build."""
    info = parse_p3d(odol(tail=named("#(argb,8,8,3)color(0,0,0,0,mc)")))
    assert info.textures == ()
    assert info.procedural == ("#(argb,8,8,3)color(0,0,0,0,mc)",)


def test_a_path_that_escapes_upwards_is_surfaced_not_filtered_away():
    """The check above this module refuses any reference starting with `..`.
    It can only do that if the reader reports one -- a filter that dropped it
    as malformed would make the most valuable check unable to ever fire."""
    info = parse_p3d(odol(tail=named(r"..\..\textures\thing_co.paa")))
    assert info.textures == (r"..\..\textures\thing_co.paa",)
    assert info.prefixes == ("..",)


def test_a_reference_carrying_a_drive_letter_is_surfaced_too():
    """An absolute path is the same class of defect as `..`: it names a file
    that will not exist inside the pbo. Reported, not swallowed."""
    info = parse_p3d(odol(tail=named(r"e:\work\textures\thing_co.paa")))
    assert info.textures == (r"e:\work\textures\thing_co.paa",)
    assert info.prefixes == ("e:",)


def test_path_prefixes_are_the_first_segment_of_every_reference():
    data = odol(tail=named(
        r"somemod\data\a_co.paa", r"somemod\data\a.rvmat", r"dz\data\data\env_land_co.paa",
    ))
    assert parse_p3d(data).prefixes == ("dz", "somemod")


# ------------------------------------------------------- the rvmat signature


def test_the_rvmat_signature_separates_a_resolved_build_from_an_unresolved_one():
    """The one string test that told a working ODOL from a broken one on six
    samples out of six: binarize inlines a resolved rvmat's own stage textures,
    and a run from the wrong working directory inlines nothing."""
    resolved = odol(tail=named(
        "#(ai,64,64,1)fresnel(1,0.7)",
        "#(argb,8,8,3)color(1,1,1,1,dt)",
        r"dz\data\data\env_land_co.paa",
        r"somemod\data\textures\thing_nohq.paa",
        r"somemod\data\textures\thing_smdi.paa",
    ))
    unresolved = odol(tail=named(
        r"somemod\data\textures\thing_co.paa", r"somemod\data\textures\thing.rvmat",
    ))
    assert inlined_material_markers(parse_p3d(resolved)) == RVMAT_INLINE_MARKERS
    assert inlined_material_markers(parse_p3d(unresolved)) == ()


def test_the_marker_set_is_the_measured_one():
    """Pinned so a later edit cannot quietly drop the marker that did the
    discriminating."""
    assert set(RVMAT_INLINE_MARKERS) == {
        "fresnel", "#(argb,8,8,3)", "env_land_co.paa", "_nohq.paa", "_smdi.paa",
    }


# --------------------------------------------------------------- fingerprint


def test_the_fingerprint_is_stable_under_a_reordering_of_the_payload():
    """The reason this exists instead of a content hash: three exports of one
    unmodified source produced three different SHA-256 hashes at a constant
    334032 bytes, the difference being an ordering permutation inside a TAGG.
    A fingerprint that moved with the order would call every re-export a
    change, and a build cache keyed on content would never hit."""
    a = mlod(tail=named("#SharpEdges#") + bytes([1, 2, 3, 4, 5, 6, 7, 8]) + named("lep_01", "lep_02"))
    b = mlod(tail=named("#SharpEdges#") + bytes([5, 6, 7, 8, 1, 2, 3, 4]) + named("lep_02", "lep_01"))
    assert a != b
    assert fingerprint(parse_p3d(a)) == fingerprint(parse_p3d(b))
    assert fingerprint(parse_p3d(a)).digest == fingerprint(parse_p3d(b)).digest


def test_the_fingerprint_moves_when_a_name_changes():
    a = mlod(tail=named("lep_01", "lep_02"))
    b = mlod(tail=named("lep_01", "lep_03"))
    assert fingerprint(parse_p3d(a)) != fingerprint(parse_p3d(b))


def test_the_fingerprint_moves_when_the_size_changes():
    """Size is in the fingerprint precisely because it did NOT move across the
    three exports -- a size that changes is therefore real evidence."""
    a = parse_p3d(mlod(tail=named("lep_01")))
    b = parse_p3d(mlod(tail=named("lep_01") + b"\x00" * 64))
    assert a.strings == b.strings
    assert fingerprint(a) != fingerprint(b)


def test_the_fingerprint_moves_when_the_lod_count_changes():
    a = parse_p3d(mlod(lods=3, tail=named("lep_01")))
    b = parse_p3d(mlod(lods=4, tail=named("lep_01")))
    assert a.size == b.size
    assert fingerprint(a) != fingerprint(b)


def test_an_mlod_and_an_odol_never_fingerprint_alike():
    """Otherwise "is the built artifact current" could be answered yes by the
    source file sitting where the artifact should be."""
    tail = named("lep_01", "lep_02")
    a = parse_p3d(mlod(lods=2, tail=tail))
    b = parse_p3d(odol(lods=2, tail=tail))
    assert fingerprint(a).digest != fingerprint(b).digest


def test_the_digest_is_reproducible_across_calls():
    data = odol(tail=named("lep_01", "surf_body"))
    assert fingerprint(parse_p3d(data)).digest == fingerprint(parse_p3d(data)).digest


# ------------------------------------------------------- reading from a file


def test_read_p3d_carries_the_path_and_the_size(tmp_path):
    p = tmp_path / "thing.p3d"
    data = odol(tail=named("surf_body"))
    p.write_bytes(data)
    info = read_p3d(p)
    assert info.path == str(p)
    assert info.size == len(data)
    assert info.strings == ("surf_body",)


# ------------------------------------------------- the real corpus, if present


def sample(role: str) -> Path:
    """The sample named by `DAYZ_MCP_SAMPLE_<role>`, or a path that is not
    there. Roles describe the PROPERTY under test, never the mod."""
    return Path(os.environ.get(f"DAYZ_MCP_SAMPLE_{role}", ""))


def needs(role: str):
    path = sample(role)
    return pytest.mark.skipif(
        not (path.name and path.is_file()),
        reason=f"set DAYZ_MCP_SAMPLE_{role} to a sample file to run",
    )


@needs("ODOL")
def test_a_real_working_odol_reads_as_a_resolved_build():
    """The shipped artifact that is confirmed working in game. Every claim the
    reader makes about a good build is made here, against it."""
    info = read_p3d(sample("ODOL"))
    assert info.kind == ODOL
    assert info.version == 55
    assert info.lod_count and info.lod_count >= 1
    assert info.textures and info.materials
    assert inlined_material_markers(info) == RVMAT_INLINE_MARKERS
    assert all(not t.startswith("..") for t in info.textures)


@needs("ODOL_ALT")
def test_a_second_real_working_odol_reads_the_same_way():
    """A different mod, a different modeller's export, the same verdict --
    which is what stops the reader from being fitted to one file."""
    info = read_p3d(sample("ODOL_ALT"))
    assert info.kind == ODOL
    assert inlined_material_markers(info) == RVMAT_INLINE_MARKERS
    assert info.textures and info.materials


@needs("ODOL_NO_RVMAT")
def test_a_real_odol_built_from_the_wrong_directory_carries_no_inlined_rvmat():
    """This artifact was broken and nobody knew. It is a valid ODOL, it has
    plausible texture paths, and the game renders it untextured. The only thing
    that separates it from the working build is the inlined rvmat strings."""
    info = read_p3d(sample("ODOL_NO_RVMAT"))
    assert info.kind == ODOL
    assert info.textures  # it looks fine right up to here
    assert inlined_material_markers(info) == ()


@needs("ODOL_FOREIGN")
def test_a_real_odol_with_stripped_paths_keeps_them_readable():
    """Its texture paths lost their leading segments when the repository was
    split. The reader must report them as they lie, so C3 can refuse on the
    prefix rather than on a path this module already normalised away."""
    info = read_p3d(sample("ODOL_FOREIGN"))
    assert info.kind == ODOL
    assert info.textures
    assert inlined_material_markers(info) == ()
    # Whatever the prefixes are, they are not the mod's -- and they are visible.
    assert info.prefixes


@needs("MLOD")
def test_a_real_mlod_export_reads_as_a_source_file():
    info = read_p3d(sample("MLOD"))
    assert info.kind == MLOD
    assert info.lod_count and info.lod_count >= 1
    assert "#EndOfFile#" in info.strings
    # An MLOD names the textures the modeller assigned -- and nothing binarize
    # would have inlined, which is exactly why C4 can only be asked of an ODOL.
    assert info.textures
    assert inlined_material_markers(info) == ()


@needs("ODOL")
@needs("ODOL_NO_RVMAT")
def test_the_four_real_artifacts_are_told_apart_by_the_reader():
    """The acceptance shape of this task, stated as one assertion: a good build
    and a broken one share a name, a LOD count and a texture list, and differ
    only where the reader says they do."""
    good = read_p3d(sample("ODOL"))
    broken = read_p3d(sample("ODOL_NO_RVMAT"))
    assert good.kind == broken.kind == ODOL
    assert good.size != broken.size
    assert fingerprint(good) != fingerprint(broken)
    assert inlined_material_markers(good) and not inlined_material_markers(broken)


@needs("MLOD")
def test_a_real_mlod_is_never_mistaken_for_a_built_artifact():
    """C10 refuses to feed an ODOL back into binarize; it can only do that if
    the kinds are read correctly off the real files."""
    assert read_p3d(sample("MLOD")).kind == MLOD
