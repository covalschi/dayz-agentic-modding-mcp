"""The artifact checks C1-C12.

Two halves, and both are load bearing. The hermetic half pins the SHAPE of
every verdict -- which check fires, at which severity, and whether the message
says what to do. The corpus half at the bottom proves the shapes are real: each
check is run against an artifact on this machine that actually carries the
defect, AND against one that does not, because a check that cannot stay silent
on a good build is worse than no check at all -- it teaches the caller to
ignore the tool.

Samples are named by the environment, by the PROPERTY under test and never by
the mod they came from, exactly as the p3d reader's own corpus tests are. On a
machine with none of them set, every corpus test skips and the hermetic half
still runs.

    DAYZ_MCP_SAMPLE_ODOL                  a binarized model that works in game
    DAYZ_MCP_SAMPLE_PREFIX                the path prefix its references carry
    DAYZ_MCP_SAMPLE_ROOT                  the directory that prefix names
    DAYZ_MCP_SAMPLE_MLOD                  the MLOD source of that model
    DAYZ_MCP_SAMPLE_MODEL_CFG             the model.cfg shipped beside it
    DAYZ_MCP_SAMPLE_MODEL_CFG_BUILT       the model.cfg in the build root
    DAYZ_MCP_SAMPLE_HIDDEN_SELECTION      a hidden selection its config declares

    DAYZ_MCP_SAMPLE_ODOL_ALT              a second working build, another mod
    DAYZ_MCP_SAMPLE_PREFIX_ALT            that mod's prefix
    DAYZ_MCP_SAMPLE_ROOT_ALT              that mod's source directory
    DAYZ_MCP_SAMPLE_ODOL_NO_ANIM          a build whose animations were dropped
    DAYZ_MCP_SAMPLE_MODEL_CFG_NO_BONES    the model.cfg that dropped them

    DAYZ_MCP_SAMPLE_ODOL_NO_RVMAT         built from the wrong directory
    DAYZ_MCP_SAMPLE_PREFIX_NO_RVMAT       the prefix that build correctly carries
    DAYZ_MCP_SAMPLE_ODOL_FOREIGN          built with the paths stripped

    DAYZ_MCP_SAMPLE_RVMAT                 an rvmat whose stages stay in its mod
    DAYZ_MCP_SAMPLE_RVMAT_FOREIGN         one whose stages point at another mod
    DAYZ_MCP_SAMPLE_RVMAT_FOREIGN_PREFIX  the prefix that rvmat's own mod uses

    DAYZ_MCP_SAMPLE_PNG_OPAQUE            an opaque source, converted to DXT1
    DAYZ_MCP_SAMPLE_PNG_GRADED_KEPT       a graded source, converted to DXT5
    DAYZ_MCP_SAMPLE_PNG_GRADED_LOST       a graded source, converted to DXT1

Each PNG sample is paired with the `.paa` sitting beside it under the same
stem, which is how the real pipeline pairs them.
"""
from __future__ import annotations

import os
import shutil
import struct
from pathlib import Path

import pytest

from dayz_mcp.assets.checks import (
    PASS,
    PROJECT_ROOT_KEY,
    REFUSE,
    SKIP,
    VANILLA_PREFIXES,
    WARN,
    Report,
    c1_artifact_is_a_binarized_model,
    c2_artifact_is_newer_than_its_inputs,
    c3_references_stay_inside_the_mod,
    c4_materials_were_inlined,
    c5_references_land_inside_the_pbo,
    c6_rvmat_stages_stay_inside_the_mod,
    c7_transparency_survived_conversion,
    c8_animations_reached_the_artifact,
    c9_selections_are_declared,
    c10_binarize_input_is_a_source,
    c11_model_cfg_is_the_one_it_was_built_from,
    c12_fingerprint_matches_the_recorded_one,
    check_model,
    check_texture,
    parse_model_cfg,
    read_artifact,
    references,
)
from dayz_mcp.assets.p3d import fingerprint, read_p3d

# --------------------------------------------------------------- p3d fixtures
# The smallest byte strings the reader accepts. Redefined here rather than
# imported from the reader's own test module: a test file that depends on
# another test file breaks the moment either is moved.


def odol(lods: int = 4, tail: bytes = b"") -> bytes:
    return b"ODOL" + struct.pack("<II", 55, lods) + b"\x00" * (4 * lods) + tail


def mlod(lods: int = 5, tail: bytes = b"") -> bytes:
    return b"MLOD" + struct.pack("<II", 0x101, lods) + tail


def named(*names: str) -> bytes:
    return b"".join(n.encode("ascii") + b"\x00" for n in names)


RESOLVED = named(
    "#(ai,64,64,1)fresnel(1,0.7)",
    "#(argb,8,8,3)color(1,1,1,1,dt)",
    r"dz\data\data\env_land_co.paa",
    r"somemod\data\textures\thing_nohq.paa",
    r"somemod\data\textures\thing_smdi.paa",
)


def write(path: Path, data: bytes | str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        path.write_text(data, encoding="utf-8")
    else:
        path.write_bytes(data)
    return path


def one(report: Report, check: str):
    """The single finding for `check` in a report."""
    found = [f for f in report.findings if f.check == check]
    assert len(found) == 1, f"{check} appears {len(found)} times in {report.findings}"
    return found[0]


# ------------------------------------------------------------------- C1 exists


def test_c1_refuses_a_missing_artifact(tmp_path):
    finding = c1_artifact_is_a_binarized_model(tmp_path / "nothing.p3d")
    assert finding.status == REFUSE
    assert finding.action


def test_c1_refuses_a_zero_length_artifact(tmp_path):
    """The measured signature of a binarize crash: exit 0xC0000005 and an empty
    file left in the output directory."""
    finding = c1_artifact_is_a_binarized_model(write(tmp_path / "a.p3d", b""))
    assert finding.status == REFUSE
    assert "empty" in finding.detail.lower()


def test_c1_refuses_an_mlod_sitting_where_the_built_artifact_belongs(tmp_path):
    finding = c1_artifact_is_a_binarized_model(write(tmp_path / "a.p3d", mlod()))
    assert finding.status == REFUSE
    assert "MLOD" in finding.detail
    assert "binarize" in finding.action.lower()


def test_c1_refuses_a_file_that_is_neither(tmp_path):
    finding = c1_artifact_is_a_binarized_model(write(tmp_path / "a.p3d", b"PBOX" + b"\x00" * 32))
    assert finding.status == REFUSE


def test_c1_passes_a_binarized_model(tmp_path):
    assert c1_artifact_is_a_binarized_model(write(tmp_path / "a.p3d", odol())).status == PASS


# -------------------------------------------------------------------- C2 stale


def test_c2_warns_when_an_input_is_newer_than_the_artifact(tmp_path):
    """A silently skipped build leaves last week's artifact in place, and every
    other check then passes on it happily."""
    art = write(tmp_path / "a.p3d", odol())
    src = write(tmp_path / "a_source.p3d", mlod())
    os.utime(art, (1_000_000, 1_000_000))
    os.utime(src, (2_000_000, 2_000_000))
    finding = c2_artifact_is_newer_than_its_inputs(art, [src])
    assert finding.status == WARN
    assert "a_source.p3d" in finding.detail
    assert finding.action


def test_c2_passes_when_the_artifact_is_the_newest_thing(tmp_path):
    art = write(tmp_path / "a.p3d", odol())
    src = write(tmp_path / "a_source.p3d", mlod())
    os.utime(src, (1_000_000, 1_000_000))
    os.utime(art, (2_000_000, 2_000_000))
    assert c2_artifact_is_newer_than_its_inputs(art, [src]).status == PASS


def test_c2_skips_rather_than_warns_when_there_are_no_inputs_to_compare(tmp_path):
    art = write(tmp_path / "a.p3d", odol())
    assert c2_artifact_is_newer_than_its_inputs(art, []).status == SKIP


def test_c2_names_an_input_that_is_not_there_instead_of_ignoring_it(tmp_path):
    """A declared input that does not exist is a broken declaration, and
    silently treating it as "not newer" would make the check pass by accident."""
    art = write(tmp_path / "a.p3d", odol())
    finding = c2_artifact_is_newer_than_its_inputs(art, [tmp_path / "gone.p3d"])
    assert finding.status == WARN
    assert "gone.p3d" in finding.detail


# ------------------------------------------------------------------- C3 prefix


def test_c3_refuses_a_reference_that_escapes_upwards(tmp_path):
    """The reason the reader reports `..` paths instead of filtering them: a
    check cannot refuse what it never sees."""
    art = read_artifact(write(tmp_path / "a.p3d", odol(tail=named(r"..\..\textures\x_co.paa"))))
    finding = c3_references_stay_inside_the_mod(art, prefix="somemod")
    assert finding.status == REFUSE
    assert r"..\..\textures\x_co.paa" in finding.detail


def test_c3_refuses_a_reference_carrying_a_drive_letter(tmp_path):
    art = read_artifact(write(tmp_path / "a.p3d", odol(tail=named(r"e:\work\x_co.paa"))))
    assert c3_references_stay_inside_the_mod(art, prefix="somemod").status == REFUSE


def test_c3_refuses_a_first_segment_that_is_not_the_mods_prefix(tmp_path):
    """The measured failure of a wrong working directory: valid ODOL, plausible
    paths, and every segment of the build machine's own tree baked in."""
    art = read_artifact(write(tmp_path / "a.p3d", odol(
        tail=named(r"models\export\staging\somemod\data\textures\x_co.paa"))))
    finding = c3_references_stay_inside_the_mod(art, prefix="somemod")
    assert finding.status == REFUSE
    assert "models" in finding.detail


def test_c3_refusal_says_what_to_do_about_the_project_root(tmp_path):
    """A wrong root is fixed by declaring the right one, not by editing paths.
    A refusal that only names the defect leaves the caller guessing."""
    art = read_artifact(write(tmp_path / "a.p3d", odol(tail=named(r"wrong\data\x_co.paa"))))
    action = c3_references_stay_inside_the_mod(art, prefix="somemod").action
    assert PROJECT_ROOT_KEY in action
    assert "somemod" in action


def test_c3_passes_the_vanilla_prefix(tmp_path):
    """binarize inlines the game's own textures into a CORRECT build. A check
    that refused them would refuse every good artifact there is."""
    assert "dz" in VANILLA_PREFIXES
    art = read_artifact(write(tmp_path / "a.p3d", odol(tail=named(
        r"somemod\data\textures\x_co.paa", r"dz\data\data\env_land_co.paa"))))
    assert c3_references_stay_inside_the_mod(art, prefix="somemod").status == PASS


def test_c3_passes_a_declared_dependency_prefix(tmp_path):
    art = read_artifact(write(tmp_path / "a.p3d", odol(tail=named(r"otherdep\data\x_co.paa"))))
    assert c3_references_stay_inside_the_mod(art, prefix="somemod").status == REFUSE
    assert c3_references_stay_inside_the_mod(
        art, prefix="somemod", also_allow=["otherdep"]).status == PASS


def test_c3_ignores_a_leading_separator_rather_than_calling_it_an_escape(tmp_path):
    """`\\SomeMod\\data\\...` is how a config.cpp writes the same path. It names
    the same file inside the same pbo, and refusing it would be a false alarm."""
    art = read_artifact(write(tmp_path / "a.p3d", odol(tail=named(r"\somemod\data\x_co.paa"))))
    assert c3_references_stay_inside_the_mod(art, prefix="somemod").status == PASS


def test_c3_is_case_insensitive(tmp_path):
    """DayZ paths are. The pbo prefix is written `SomeMod` and binarize bakes
    `somemod`, on this machine's own artifacts."""
    art = read_artifact(write(tmp_path / "a.p3d", odol(tail=named(r"SoMeMoD\data\x_co.paa"))))
    assert c3_references_stay_inside_the_mod(art, prefix="someMod").status == PASS


def test_c3_never_treats_a_procedural_texture_as_a_path(tmp_path):
    """`#(argb,8,8,3)color(...)` is a texture with no path at all, and every
    correct build carries several."""
    art = read_artifact(write(tmp_path / "a.p3d", odol(tail=RESOLVED)))
    assert c3_references_stay_inside_the_mod(art, prefix="somemod").status == PASS


# ------------------------------------------------------------------- C4 rvmat


def test_c4_refuses_an_odol_that_inlined_no_material(tmp_path):
    """The one signal separating a working build from one made in the wrong
    directory: both are valid ODOL files with plausible texture paths."""
    art = read_artifact(write(tmp_path / "a.p3d", odol(tail=named(
        r"somemod\data\textures\x_co.paa", r"somemod\data\textures\x.rvmat"))))
    finding = c4_materials_were_inlined(art)
    assert finding.status == REFUSE
    assert "rvmat" in finding.detail.lower()
    assert finding.action


def test_c4_passes_an_odol_carrying_the_whole_inlined_signature(tmp_path):
    art = read_artifact(write(tmp_path / "a.p3d", odol(tail=RESOLVED + named(
        r"somemod\data\textures\x.rvmat"))))
    assert c4_materials_were_inlined(art).status == PASS


def test_c4_only_warns_when_the_signature_is_partial(tmp_path):
    """Zero markers out of five was measured on every broken build and on no
    good one. A partial set was never measured at all -- a material with fewer
    stages would produce one legitimately, so it is reported, not refused."""
    art = read_artifact(write(tmp_path / "a.p3d", odol(tail=named(
        "#(ai,64,64,1)fresnel(1,0.7)", r"somemod\data\textures\x.rvmat"))))
    assert c4_materials_were_inlined(art).status == WARN


def test_c4_skips_a_model_that_references_no_material_at_all(tmp_path):
    """binarize inlines nothing when there is nothing to inline. Refusing here
    would refuse every untextured proxy and collision-only model."""
    art = read_artifact(write(tmp_path / "a.p3d", odol(tail=named("surf_body"))))
    assert c4_materials_were_inlined(art).status == SKIP


def test_c4_skips_an_mlod_because_inlining_is_something_binarize_does(tmp_path):
    """Trap: an MLOD legitimately carries none of the five markers. Asking C4
    of a source file would call every correct export broken."""
    art = read_artifact(write(tmp_path / "a.p3d", mlod(tail=named(
        r"somemod\data\textures\x.rvmat"))))
    assert c4_materials_were_inlined(art).status == SKIP


# --------------------------------------------------------------- C5 references


def test_c5_warns_about_a_reference_with_no_file_behind_it(tmp_path):
    root = tmp_path / "somemod"
    write(root / "data" / "textures" / "there_co.paa", b"\x01\xff")
    art = read_artifact(write(tmp_path / "a.p3d", odol(tail=named(
        r"somemod\data\textures\there_co.paa", r"somemod\data\textures\gone_co.paa"))))
    finding = c5_references_land_inside_the_pbo(art, {"somemod": root})
    assert finding.status == WARN
    assert "gone_co.paa" in finding.detail
    assert "there_co.paa" not in finding.detail
    assert finding.action


def test_c5_passes_when_every_reference_has_a_file(tmp_path):
    root = tmp_path / "somemod"
    write(root / "data" / "textures" / "there_co.paa", b"\x01\xff")
    art = read_artifact(write(tmp_path / "a.p3d", odol(
        tail=named(r"somemod\data\textures\there_co.paa"))))
    assert c5_references_land_inside_the_pbo(art, {"somemod": root}).status == PASS


def test_c5_never_goes_looking_for_the_games_own_files(tmp_path):
    """`dz\\...` ships with DayZ, not with the mod. Reporting it as dangling
    would put a permanent false warning on every correct build."""
    root = tmp_path / "somemod"
    root.mkdir()
    art = read_artifact(write(tmp_path / "a.p3d", odol(
        tail=named(r"dz\data\data\env_land_co.paa"))))
    assert c5_references_land_inside_the_pbo(art, {"somemod": root}).status == PASS


def test_c5_skips_a_prefix_nobody_declared_a_root_for(tmp_path):
    """C3 already refuses a foreign prefix. C5 guessing where it might live
    would report the same defect twice, in weaker words."""
    root = tmp_path / "somemod"
    root.mkdir()
    art = read_artifact(write(tmp_path / "a.p3d", odol(tail=named(r"other\data\x_co.paa"))))
    finding = c5_references_land_inside_the_pbo(art, {"somemod": root})
    assert finding.status == SKIP


def test_c5_skips_when_no_root_is_declared_at_all(tmp_path):
    art = read_artifact(write(tmp_path / "a.p3d", odol(tail=named(r"somemod\data\x_co.paa"))))
    assert c5_references_land_inside_the_pbo(art, {}).status == SKIP


def test_c5_warns_about_a_file_the_packer_will_leave_out(tmp_path):
    """"On the disk" and "inside the pbo" are not the same question, and only
    the second one is C5's. The packer drops everything matching the project's
    exclude list before FileBank ever sees it, so a texture sitting in an
    excluded folder is exactly as dangling in game as one that was never built
    -- and it looks perfectly fine to a check that only calls `exists()`."""
    root = tmp_path / "somemod"
    write(root / "source" / "x_co.paa", b"\x01\xff")
    art = read_artifact(write(tmp_path / "a.p3d", odol(tail=named(r"somemod\source\x_co.paa"))))
    assert c5_references_land_inside_the_pbo(art, {"somemod": root}).status == PASS

    finding = c5_references_land_inside_the_pbo(art, {"somemod": root}, exclude=["source"])
    assert finding.status == WARN
    assert "x_co.paa" in finding.detail
    assert "source" in finding.detail or "source" in finding.action
    assert finding.action


def test_c5_excludes_by_name_at_any_depth_like_the_packer_does(tmp_path):
    """Same rule as `packer.find_excluded`, imported rather than re-spelled: a
    pattern matches a NAME anywhere in the tree, and a matched directory takes
    everything under it. Two spellings of one rule is how a check and the thing
    it predicts drift apart."""
    root = tmp_path / "somemod"
    write(root / "data" / "wip" / "deep" / "x_co.paa", b"\x01\xff")
    write(root / "data" / "textures" / "kept_co.paa", b"\x01\xff")
    art = read_artifact(write(tmp_path / "a.p3d", odol(tail=named(
        r"somemod\data\wip\deep\x_co.paa", r"somemod\data\textures\kept_co.paa"))))
    finding = c5_references_land_inside_the_pbo(art, {"somemod": root}, exclude=["wip"])
    assert finding.status == WARN
    assert "x_co.paa" in finding.detail
    assert "kept_co.paa" not in finding.detail


def test_check_model_hands_c5_the_projects_exclude_list(tmp_path):
    """The wiring, not the rule: a report that had the list and did not pass it
    on would answer the easier question and look identical."""
    root = tmp_path / "somemod"
    write(root / "source" / "x_co.paa", b"\x01\xff")
    artifact = write(tmp_path / "a.p3d", odol(
        tail=named(r"somemod\data\textures\thing.rvmat", r"somemod\source\x_co.paa") + RESOLVED))
    report = check_model(artifact, prefix="somemod", roots={"somemod": root},
                         exclude=["source"])
    c5 = one(report, "C5")
    assert c5.status == WARN
    assert "x_co.paa" in c5.detail


# -------------------------------------------------------------------- C6 rvmat


def test_c6_warns_about_a_stage_pointing_into_another_mod(tmp_path):
    rvmat = write(tmp_path / "x.rvmat", 'class Stage1 { texture="othermod\\data\\x_nohq.paa"; };')
    finding = c6_rvmat_stages_stay_inside_the_mod(rvmat, prefix="somemod")
    assert finding.status == WARN
    assert "othermod" in finding.detail
    assert finding.action


def test_c6_warns_about_a_stage_that_is_not_a_paa(tmp_path):
    """A `.png` or `.tga` in an rvmat resolves to nothing once the mod is
    packed; the engine renders the surface untextured and says nothing."""
    rvmat = write(tmp_path / "x.rvmat", 'class Stage1 { texture="somemod\\data\\x_nohq.png"; };')
    finding = c6_rvmat_stages_stay_inside_the_mod(rvmat, prefix="somemod")
    assert finding.status == WARN
    assert ".png" in finding.detail


def test_c6_passes_procedural_stages_and_the_vanilla_prefix(tmp_path):
    """Trap: four of the seven stages in every correct rvmat on this machine
    are procedural textures with no path, and one is the game's own."""
    rvmat = write(tmp_path / "x.rvmat", "\n".join([
        'class Stage1 { texture="somemod\\data\\x_nohq.paa"; };',
        'class Stage2 { texture="#(argb,8,8,3)color(0.5,0.5,0.5,1,DT)"; };',
        'class Stage3 { texture="#(ai,64,64,1)fresnel(1,0.7)"; };',
        'class Stage4 { texture="dz\\data\\data\\env_land_co.paa"; };',
    ]))
    assert c6_rvmat_stages_stay_inside_the_mod(rvmat, prefix="somemod").status == PASS


def test_c6_reports_a_missing_rvmat_rather_than_passing_it(tmp_path):
    finding = c6_rvmat_stages_stay_inside_the_mod(tmp_path / "gone.rvmat", prefix="somemod")
    assert finding.status == WARN
    assert "gone.rvmat" in finding.detail


# ------------------------------------------------------------ C7 transparency


def test_c7_warns_when_a_graded_source_became_dxt1(png_graded, tmp_path):
    """DXT1 carries ONE BIT of alpha. The measured loss is not a missing
    channel, it is quantisation -- 6 levels in, 2 levels out."""
    paa = write(tmp_path / "x_co.paa", b"\x01\xff" + b"\x00" * 32)
    finding = c7_transparency_survived_conversion(png_graded, paa)
    assert finding.status == WARN
    assert "DXT1" in finding.detail
    assert "_ca" in finding.action


def test_c7_passes_when_a_graded_source_became_dxt5(png_graded, tmp_path):
    paa = write(tmp_path / "x_ca.paa", b"\x05\xff" + b"\x00" * 32)
    assert c7_transparency_survived_conversion(png_graded, paa).status == PASS


def test_c7_passes_an_opaque_source_in_dxt1(png_opaque, tmp_path):
    """The false alarm this check exists to avoid: a legitimately opaque
    texture and one whose transparency was destroyed both come out of DXT1
    with no gradient. Only the SOURCE tells them apart."""
    paa = write(tmp_path / "x_co.paa", b"\x01\xff" + b"\x00" * 32)
    assert c7_transparency_survived_conversion(png_opaque, paa).status == PASS


def test_c7_never_judges_the_format_by_the_file_name(png_opaque, tmp_path):
    """Measured on this machine: `_smdi` sources come out DXT1 while the suffix
    table predicts DXT5. A check that compared the two would put a warning on
    twelve correct textures."""
    paa = write(tmp_path / "x_smdi.paa", b"\x01\xff" + b"\x00" * 32)
    assert c7_transparency_survived_conversion(png_opaque, paa).status == PASS


def test_c7_reports_an_unreadable_source_instead_of_guessing(tmp_path):
    paa = write(tmp_path / "x_co.paa", b"\x01\xff" + b"\x00" * 32)
    finding = c7_transparency_survived_conversion(tmp_path / "gone.png", paa)
    assert finding.status == SKIP
    assert "gone.png" in finding.detail


# ---------------------------------------------------------- the model.cfg read


SKELETON_CFG = """
class CfgSkeletons
{
    class thing_skeleton
    {
        isDiscrete = 1;
        skeletonInherit = "";
        skeletonBones[] = { "root", "", "lid", "root", "lep_01", "root" };
    };
};
class CfgModels
{
    class Default { sectionsInherit = ""; sections[] = {}; skeletonName = ""; };
    class thing : Default
    {
        skeletonName = "thing_skeleton";
        sections[] = { "lep_01", "lid", "surf_body" };
        class Animations
        {
            class hide_lid { type = "hide"; source = "hide_lid"; selection = "lid"; };
            class raise { type = "translation"; source = "raise"; selection = "root"; };
        };
    };
};
"""


def test_the_model_cfg_reader_finds_bones_sections_and_animations():
    cfg = parse_model_cfg(SKELETON_CFG)
    entry = cfg.model("thing")
    assert entry is not None
    assert entry.skeleton == "thing_skeleton"
    assert entry.sections == ("lep_01", "lid", "surf_body")
    assert [a.name for a in entry.animations] == ["hide_lid", "raise"]
    assert [a.selection for a in entry.animations] == ["lid", "root"]
    assert cfg.bones("thing_skeleton") == ("root", "lid", "lep_01")


def test_the_model_cfg_reader_takes_bones_from_the_pairs_not_the_parents():
    """skeletonBones[] is a flat list of (bone, parent) pairs. Read as a flat
    set, a typo'd parent becomes a bone that nothing declares."""
    cfg = parse_model_cfg(
        'class CfgSkeletons { class s { skeletonBones[] = { "a", "", "b", "a" }; }; };')
    assert cfg.bones("s") == ("a", "b")


def test_the_model_cfg_reader_ignores_commented_out_declarations():
    cfg = parse_model_cfg("""
    class CfgModels {
      class thing : Default {
        // class Animations { class ghost { source = "ghost"; selection = "x"; }; };
        /* sections[] = { "commented" }; */
        sections[] = { "real" };
      };
    };
    """)
    entry = cfg.model("thing")
    assert entry.sections == ("real",)
    assert entry.animations == ()


def test_the_model_cfg_reader_merges_a_class_that_is_opened_twice():
    """These configs are routinely written as several blocks that reopen the
    same class. Keeping only the last would drop every earlier declaration."""
    cfg = parse_model_cfg(
        'class CfgSkeletons { class a { skeletonBones[] = { "x", "" }; }; };'
        'class CfgSkeletons { class b { skeletonBones[] = { "y", "" }; }; };')
    assert cfg.bones("a") == ("x",)
    assert cfg.bones("b") == ("y",)


def test_the_model_cfg_reader_matches_a_class_name_case_insensitively():
    cfg = parse_model_cfg(SKELETON_CFG)
    assert cfg.model("THING") is not None
    assert cfg.model("nothing_like_it") is None


# --------------------------------------------------------------- C8 animations


def test_c8_warns_when_an_animation_never_reached_the_artifact(tmp_path):
    """The measured mechanism: with an empty skeletonBones[], binarize drops
    every animation from the ODOL and reports success."""
    art = read_artifact(write(tmp_path / "thing.p3d", odol(tail=named("lid", "root"))))
    finding = c8_animations_reached_the_artifact(art, parse_model_cfg(SKELETON_CFG))
    assert finding.status == WARN
    assert "hide_lid" in finding.detail
    assert "skeletonBones" in finding.action


def test_c8_passes_when_every_animation_is_in_the_artifact(tmp_path):
    art = read_artifact(write(tmp_path / "thing.p3d", odol(tail=named("hide_lid", "raise"))))
    assert c8_animations_reached_the_artifact(art, parse_model_cfg(SKELETON_CFG)).status == PASS


def test_c8_asks_the_unfiltered_string_superset(tmp_path):
    """Trap: the reader's `strings` needs four characters to call a run a name,
    so a genuine three-character animation is not in it. Asking the filtered
    set would raise a false alarm on a correct build."""
    cfg = parse_model_cfg(
        'class CfgModels { class thing { class Animations { '
        'class cap { type = "hide"; source = "cap"; selection = "lid"; }; }; }; };')
    art = read_artifact(write(tmp_path / "thing.p3d", odol(tail=named("cap", "lid"))))
    assert "cap" not in art.info.strings  # the filtered set drops it
    assert "cap" in art.names  # the superset does not
    assert c8_animations_reached_the_artifact(art, cfg).status == PASS


def test_c8_accepts_either_the_animation_name_or_its_source(tmp_path):
    cfg = parse_model_cfg(
        'class CfgModels { class thing { class Animations { '
        'class anim_open { type = "hide"; source = "open_state"; selection = "lid"; }; }; }; };')
    art = read_artifact(write(tmp_path / "thing.p3d", odol(tail=named("open_state"))))
    assert c8_animations_reached_the_artifact(art, cfg).status == PASS


def test_c8_warns_when_the_model_cfg_declares_no_class_for_this_model(tmp_path):
    """Without a class of its own the engine falls back to Default: no
    skeleton, no sections, no animations, and no complaint."""
    art = read_artifact(write(tmp_path / "unlisted.p3d", odol()))
    finding = c8_animations_reached_the_artifact(art, parse_model_cfg(SKELETON_CFG))
    assert finding.status == WARN
    assert "unlisted" in finding.detail


def test_c8_passes_a_model_that_declares_no_animations(tmp_path):
    cfg = parse_model_cfg('class CfgModels { class thing { sections[] = { "a" }; }; };')
    art = read_artifact(write(tmp_path / "thing.p3d", odol()))
    assert c8_animations_reached_the_artifact(art, cfg).status == PASS


# ---------------------------------------------------------------- C9 upstream


def test_c9_warns_when_an_animated_selection_is_not_a_bone():
    """Upstream of C8: the same defect, caught in the source file, before an
    hour of binarize has been spent on it."""
    cfg = parse_model_cfg(SKELETON_CFG.replace(
        '{ "root", "", "lid", "root", "lep_01", "root" }', "{ }"))
    finding = c9_selections_are_declared(cfg, "thing")
    assert finding.status == WARN
    assert "lid" in finding.detail
    assert "skeletonBones" in finding.action


def test_c9_warns_when_a_hidden_selection_is_not_a_section():
    finding = c9_selections_are_declared(
        parse_model_cfg(SKELETON_CFG), "thing", hidden_selections=["camo"])
    assert finding.status == WARN
    assert "camo" in finding.detail
    assert "sections" in finding.action


def test_c9_passes_a_model_cfg_that_declares_everything_it_animates():
    finding = c9_selections_are_declared(
        parse_model_cfg(SKELETON_CFG), "thing", hidden_selections=["surf_body"])
    assert finding.status == PASS


def test_c9_will_not_judge_bones_it_cannot_see(tmp_path):
    """A skeleton that inherits from one declared elsewhere has bones this file
    does not list. Judging on the visible half would refuse every mod that
    builds on a vanilla skeleton."""
    cfg = parse_model_cfg(SKELETON_CFG.replace(
        'skeletonInherit = ""', 'skeletonInherit = "SomeVanillaSkeleton"'))
    finding = c9_selections_are_declared(cfg, "thing")
    assert finding.status == SKIP
    assert "SomeVanillaSkeleton" in finding.detail


def test_c9_will_not_judge_bones_a_grandparent_hides_either():
    """The chain is walked, not just the first link: a skeleton that inherits
    from one declared here, which in turn inherits from one that is not, still
    has bones nobody can see."""
    cfg = parse_model_cfg(SKELETON_CFG.replace(
        'skeletonInherit = ""', 'skeletonInherit = "middle"'
    ) + 'class CfgSkeletons { class middle { skeletonInherit = "far_away"; '
        'skeletonBones[] = {}; }; };')
    finding = c9_selections_are_declared(cfg, "thing")
    assert finding.status == SKIP
    assert "far_away" in finding.detail


def test_c9_warns_when_the_named_skeleton_is_not_declared_here():
    cfg = parse_model_cfg(
        'class CfgModels { class thing { skeletonName = "absent_skeleton"; '
        'class Animations { class a { source = "a"; selection = "lid"; }; }; }; };')
    finding = c9_selections_are_declared(cfg, "thing")
    assert finding.status == WARN
    assert "absent_skeleton" in finding.detail


# ------------------------------------------------------------ C10 binarize in


def test_c10_refuses_to_feed_a_binarized_model_back_in(tmp_path):
    """Measured: binarize crashes with 0xC0000005 and leaves a zero-length file
    in the output directory. This refusal happens BEFORE that."""
    finding = c10_binarize_input_is_a_source(write(tmp_path / "a.p3d", odol()))
    assert finding.status == REFUSE
    assert "ODOL" in finding.detail
    assert finding.action


def test_c10_passes_an_mlod_source(tmp_path):
    assert c10_binarize_input_is_a_source(write(tmp_path / "a.p3d", mlod())).status == PASS


def test_c10_refuses_a_missing_or_empty_input(tmp_path):
    assert c10_binarize_input_is_a_source(tmp_path / "gone.p3d").status == REFUSE
    assert c10_binarize_input_is_a_source(write(tmp_path / "a.p3d", b"")).status == REFUSE


# ------------------------------------------------------------- C11 model.cfg


def test_c11_warns_when_the_shipped_model_cfg_is_not_the_built_one(tmp_path):
    shipped = write(tmp_path / "ship" / "model.cfg", SKELETON_CFG)
    built = write(tmp_path / "root" / "model.cfg", SKELETON_CFG.replace("hide_lid", "hide_cap"))
    finding = c11_model_cfg_is_the_one_it_was_built_from(shipped, built)
    assert finding.status == WARN
    assert "hide_cap" in finding.detail or "hide_lid" in finding.detail
    assert finding.action


def test_c11_ignores_comments_and_layout(tmp_path):
    """The two copies of a model.cfg drift in comments constantly. A byte
    comparison would warn on every one of them."""
    shipped = write(tmp_path / "ship" / "model.cfg", SKELETON_CFG)
    built = write(tmp_path / "root" / "model.cfg",
                  "// a note nobody else has\n" + SKELETON_CFG.replace("    ", "\t"))
    assert c11_model_cfg_is_the_one_it_was_built_from(shipped, built).status == PASS


def test_c11_falls_back_to_the_clock_when_there_is_only_one_copy(tmp_path):
    art = write(tmp_path / "a.p3d", odol())
    shipped = write(tmp_path / "model.cfg", SKELETON_CFG)
    os.utime(art, (1_000_000, 1_000_000))
    os.utime(shipped, (2_000_000, 2_000_000))
    finding = c11_model_cfg_is_the_one_it_was_built_from(shipped, None, artifact=art)
    assert finding.status == WARN
    assert finding.action


def test_c11_is_quiet_when_the_only_copy_predates_the_artifact(tmp_path):
    art = write(tmp_path / "a.p3d", odol())
    shipped = write(tmp_path / "model.cfg", SKELETON_CFG)
    os.utime(shipped, (1_000_000, 1_000_000))
    os.utime(art, (2_000_000, 2_000_000))
    assert c11_model_cfg_is_the_one_it_was_built_from(shipped, None, artifact=art).status == PASS


def test_c11_reports_a_missing_model_cfg(tmp_path):
    finding = c11_model_cfg_is_the_one_it_was_built_from(tmp_path / "gone.cfg", None)
    assert finding.status == WARN
    assert "gone.cfg" in finding.detail


# ------------------------------------------------------------ C12 fingerprint


def test_c12_warns_when_the_artifact_is_not_the_recorded_one(tmp_path):
    a = read_artifact(write(tmp_path / "a.p3d", odol(tail=named("lep_01"))))
    b = read_artifact(write(tmp_path / "b.p3d", odol(tail=named("lep_02"))))
    finding = c12_fingerprint_matches_the_recorded_one(a, fingerprint(b.info))
    assert finding.status == WARN
    assert "lep_01" in finding.detail or "lep_02" in finding.detail
    assert finding.action


def test_c12_passes_the_same_artifact(tmp_path):
    a = read_artifact(write(tmp_path / "a.p3d", odol(tail=named("lep_01"))))
    assert c12_fingerprint_matches_the_recorded_one(a, fingerprint(a.info)).status == PASS


def test_c12_is_not_fooled_by_a_reordering_of_the_payload(tmp_path):
    """The whole reason a fingerprint exists instead of a hash: three exports
    of one unmodified source gave three different SHA-256 hashes at a constant
    size. A hash comparison would call every re-export a change."""
    a = read_artifact(write(tmp_path / "a.p3d", odol(
        tail=named("#SharpEdges#") + bytes([1, 2, 3, 4]) + named("lep_01", "lep_02"))))
    b = read_artifact(write(tmp_path / "b.p3d", odol(
        tail=named("#SharpEdges#") + bytes([3, 4, 1, 2]) + named("lep_02", "lep_01"))))
    assert Path(a.path).read_bytes() != Path(b.path).read_bytes()
    assert c12_fingerprint_matches_the_recorded_one(a, fingerprint(b.info)).status == PASS


def test_c12_skips_when_nothing_was_ever_recorded(tmp_path):
    a = read_artifact(write(tmp_path / "a.p3d", odol()))
    assert c12_fingerprint_matches_the_recorded_one(a, None).status == SKIP


# ---------------------------------------------------------------- the report


def test_a_report_is_not_ok_when_anything_refused(tmp_path):
    report = check_model(write(tmp_path / "a.p3d", mlod()), prefix="somemod")
    assert not report.ok
    assert report.refusals
    assert "C1" in report.summary


def test_a_report_is_ok_when_only_warnings_were_raised(tmp_path):
    """The severity split exists so a warning cannot stop a build and a refusal
    always does."""
    art = write(tmp_path / "a.p3d", odol(tail=RESOLVED + named(
        r"somemod\data\textures\x.rvmat", r"somemod\data\textures\gone_co.paa")))
    root = tmp_path / "somemod"
    root.mkdir()
    report = check_model(art, prefix="somemod", roots={"somemod": root})
    assert report.ok
    assert report.warnings
    assert one(report, "C5").status == WARN


def test_a_failed_c1_leaves_the_rest_skipped_rather_than_guessing(tmp_path):
    report = check_model(tmp_path / "gone.p3d", prefix="somemod")
    assert one(report, "C1").status == REFUSE
    assert {f.status for f in report.findings if f.check != "C1"} == {SKIP}


def test_every_check_the_report_runs_says_what_to_do_when_it_fires(tmp_path):
    """A finding without an action is a log line nobody reads, which is how
    every one of these traps survived in the first place."""
    art = write(tmp_path / "a.p3d", odol(tail=named(r"..\x_co.paa", r"wrong\y.rvmat")))
    report = check_model(art, prefix="somemod", model_cfg=tmp_path / "gone.cfg")
    fired = [f for f in report.findings if f.status in (REFUSE, WARN)]
    assert fired
    assert all(f.action for f in fired), [f.check for f in fired if not f.action]


def test_the_report_does_not_say_the_same_thing_twice(tmp_path):
    """A referenced rvmat that is not on the disk is C5's finding. C6 saying it
    again in its own words teaches the reader that findings are echoes."""
    root = tmp_path / "somemod"
    root.mkdir()
    art = write(tmp_path / "a.p3d", odol(tail=RESOLVED + named(r"somemod\data\x.rvmat")))
    report = check_model(art, prefix="somemod", roots={"somemod": root})
    assert one(report, "C5").status == WARN
    assert one(report, "C6").status == SKIP


def test_the_report_still_reads_an_rvmat_that_is_there(tmp_path):
    root = tmp_path / "somemod"
    write(root / "data" / "x.rvmat", 'class Stage1 { texture="elsewhere\\data\\x_nohq.paa"; };')
    art = write(tmp_path / "a.p3d", odol(tail=RESOLVED + named(r"somemod\data\x.rvmat")))
    report = check_model(art, prefix="somemod", roots={"somemod": root})
    assert one(report, "C6").status == WARN
    assert "elsewhere" in one(report, "C6").detail


def test_the_report_survives_a_round_trip_to_a_dict(tmp_path):
    report = check_model(write(tmp_path / "a.p3d", odol(tail=RESOLVED)), prefix="somemod")
    as_dict = report.to_dict()
    assert as_dict["ok"] is True
    assert {f["check"] for f in as_dict["findings"]} >= {"C1", "C3", "C4"}


def test_a_long_list_of_evidence_is_capped_and_says_so(tmp_path):
    """A model with two hundred dangling references must not hand the caller
    the reference list back instead of a decision."""
    many = named(*[f"somemod\\data\\textures\\gone_{i:03}_co.paa" for i in range(200)])
    art = read_artifact(write(tmp_path / "a.p3d", odol(tail=many)))
    root = tmp_path / "somemod"
    root.mkdir()
    finding = c5_references_land_inside_the_pbo(art, {"somemod": root})
    assert finding.status == WARN
    assert len(finding.evidence) < 200
    assert "200" in finding.detail


# ---------------------------------------------------- the artifact reader here


def test_read_artifact_reads_the_bytes_once_and_keeps_both_string_sets(tmp_path):
    art = read_artifact(write(tmp_path / "a.p3d", odol(tail=named("cap", "component01"))))
    assert art.info.strings == ("component01",)
    assert set(art.names) >= {"cap", "component01"}


def test_read_artifact_keeps_the_magic_out_of_the_superset(tmp_path):
    """The ODOL version byte is 0x37 -- the digit 7. Scanned from zero, every
    artifact would carry a name called `ODOL7`, and a membership question
    would be answered about the file header."""
    art = read_artifact(write(tmp_path / "a.p3d", odol(tail=named("component01"))))
    assert "ODOL7" not in art.names


def test_references_admit_a_shape_the_readers_path_filter_rejects(tmp_path):
    """The lesson of the reader's own first draft: a rule that filters input
    before judging it can filter away exactly the case the check exists for.
    A leading separator is one such shape, and it names a real file."""
    art = read_artifact(write(tmp_path / "a.p3d", odol(tail=named(r"\somemod\data\x_co.paa"))))
    assert r"\somemod\data\x_co.paa" not in art.info.textures
    assert r"\somemod\data\x_co.paa" in references(art)


def test_references_drop_a_truncated_fragment_of_a_real_path(tmp_path):
    """Compressed regions leak fragments. `co.paa` with no separator in it is
    not a reference, and treating it as one would refuse a good build for
    having a prefix called `co.paa`."""
    art = read_artifact(write(tmp_path / "a.p3d", odol(tail=named(
        "co.paa", r"somemod\data\x_co.paa"))))
    assert references(art) == (r"somemod\data\x_co.paa",)


# ------------------------------------------------- the real corpus, if present


def sample(role: str) -> Path:
    return Path(os.environ.get(f"DAYZ_MCP_SAMPLE_{role}", ""))


def text(role: str) -> str:
    return os.environ.get(f"DAYZ_MCP_SAMPLE_{role}", "")


def needs(*roles: str):
    missing = [r for r in roles if not os.environ.get(f"DAYZ_MCP_SAMPLE_{r}", "")]
    absent = [r for r in roles if not missing and not sample(r).exists()]
    return pytest.mark.skipif(
        bool(missing or absent),
        reason=f"set DAYZ_MCP_SAMPLE_{{{','.join(roles)}}} to run",
    )


def needs_value(*roles: str):
    missing = [r for r in roles if not os.environ.get(f"DAYZ_MCP_SAMPLE_{r}", "")]
    return pytest.mark.skipif(bool(missing), reason=f"set DAYZ_MCP_SAMPLE_{{{','.join(roles)}}}")


@pytest.fixture
def png_graded(tmp_path) -> Path:
    """A real source whose alpha is graded, copied out of the corpus. Skips
    when the corpus is absent, because the point is a REAL alpha histogram."""
    src = sample("PNG_GRADED_LOST")
    if not src.name or not src.is_file():
        pytest.skip("set DAYZ_MCP_SAMPLE_PNG_GRADED_LOST to run")
    return Path(shutil.copy2(src, tmp_path / "graded.png"))


@pytest.fixture
def png_opaque(tmp_path) -> Path:
    src = sample("PNG_OPAQUE")
    if not src.name or not src.is_file():
        pytest.skip("set DAYZ_MCP_SAMPLE_PNG_OPAQUE to run")
    return Path(shutil.copy2(src, tmp_path / "opaque.png"))


@needs("ODOL")
def test_c1_stays_silent_on_the_artifact_that_works_in_game():
    assert c1_artifact_is_a_binarized_model(sample("ODOL")).status == PASS


@needs("MLOD")
def test_c1_refuses_the_real_mlod_source_standing_in_for_the_artifact():
    finding = c1_artifact_is_a_binarized_model(sample("MLOD"))
    assert finding.status == REFUSE
    assert "MLOD" in finding.detail


@needs("ODOL", "MLOD", "MODEL_CFG_BUILT")
def test_c2_is_quiet_on_the_real_build_that_postdates_its_inputs():
    finding = c2_artifact_is_newer_than_its_inputs(
        sample("ODOL"), [sample("MLOD"), sample("MODEL_CFG_BUILT")])
    assert finding.status == PASS


@needs("ODOL", "MLOD")
def test_c2_catches_the_real_pair_the_other_way_round():
    """The same two real files, with the roles swapped: the source is newer
    than the artifact, which is exactly what a skipped build leaves behind."""
    finding = c2_artifact_is_newer_than_its_inputs(sample("MLOD"), [sample("ODOL")])
    assert finding.status == WARN


@needs("ODOL")
@needs_value("PREFIX")
def test_c3_stays_silent_on_the_real_working_build():
    """It carries the mod's own prefix AND the vanilla one, which binarize
    inlined. Refusing the second would refuse every correct artifact."""
    art = read_artifact(sample("ODOL"))
    assert set(art.info.prefixes) & set(VANILLA_PREFIXES)
    assert c3_references_stay_inside_the_mod(art, prefix=text("PREFIX")).status == PASS


@needs("ODOL_FOREIGN")
@needs_value("PREFIX_ALT")
def test_c3_refuses_the_real_build_whose_paths_lost_their_root():
    """Its references start with a segment of the build machine's own tree.
    Nobody knew until a check looked at the artifact instead of the log."""
    art = read_artifact(sample("ODOL_FOREIGN"))
    finding = c3_references_stay_inside_the_mod(art, prefix=text("PREFIX_ALT"))
    assert finding.status == REFUSE
    assert art.info.prefixes[0] in finding.detail
    assert PROJECT_ROOT_KEY in finding.action


@needs("ODOL_NO_RVMAT")
@needs_value("PREFIX_NO_RVMAT")
def test_c3_stays_silent_on_the_build_that_only_c4_can_catch():
    """This artifact IS broken, and its paths are perfectly well formed. C3
    passing it is the point: the two checks catch different things, and a C3
    that fired here would be firing for the wrong reason."""
    art = read_artifact(sample("ODOL_NO_RVMAT"))
    assert c3_references_stay_inside_the_mod(art, prefix=text("PREFIX_NO_RVMAT")).status == PASS


@needs("ODOL", "ODOL_ALT")
def test_c4_stays_silent_on_both_real_working_builds():
    for role in ("ODOL", "ODOL_ALT"):
        assert c4_materials_were_inlined(read_artifact(sample(role))).status == PASS, role


@needs("ODOL_NO_RVMAT", "ODOL_FOREIGN")
def test_c4_refuses_both_real_broken_builds():
    for role in ("ODOL_NO_RVMAT", "ODOL_FOREIGN"):
        finding = c4_materials_were_inlined(read_artifact(sample(role)))
        assert finding.status == REFUSE, role


@needs("MLOD")
def test_c4_stays_silent_on_the_real_mlod_that_carries_no_markers_by_design():
    """The trap, on a real file: the source of a WORKING build has none of the
    five markers, because inlining is something binarize does."""
    assert c4_materials_were_inlined(read_artifact(sample("MLOD"))).status == SKIP


@needs("ODOL", "ROOT")
@needs_value("PREFIX")
def test_c5_finds_every_reference_of_the_real_working_build_on_the_disk():
    art = read_artifact(sample("ODOL"))
    finding = c5_references_land_inside_the_pbo(art, {text("PREFIX"): sample("ROOT")})
    assert finding.status == PASS, finding.detail


@needs("ODOL_ALT", "ROOT_ALT")
@needs_value("PREFIX_ALT")
def test_c5_catches_a_real_reference_with_no_file_behind_it():
    """A real dangling reference on this disk: the artifact names textures that
    were never copied into the mod, and the engine renders it untextured."""
    art = read_artifact(sample("ODOL_ALT"))
    finding = c5_references_land_inside_the_pbo(art, {text("PREFIX_ALT"): sample("ROOT_ALT")})
    assert finding.status == WARN
    assert ".paa" in finding.detail


@needs("RVMAT")
@needs_value("PREFIX")
def test_c6_stays_silent_on_a_real_rvmat_with_four_procedural_stages():
    assert c6_rvmat_stages_stay_inside_the_mod(sample("RVMAT"), prefix=text("PREFIX")).status == PASS


@needs("RVMAT_FOREIGN")
@needs_value("RVMAT_FOREIGN_PREFIX")
def test_c6_catches_the_real_rvmat_that_points_into_another_mod():
    """Upstream of the artifact that carries no inlined material: this rvmat
    sits in one mod and names another mod's textures, so binarize -- running
    with only the first mod under its root -- resolved nothing and said so
    nowhere."""
    finding = c6_rvmat_stages_stay_inside_the_mod(
        sample("RVMAT_FOREIGN"), prefix=text("RVMAT_FOREIGN_PREFIX"))
    assert finding.status == WARN
    assert finding.evidence


@needs("PNG_GRADED_LOST")
def test_c7_catches_the_real_texture_whose_transparency_was_quantised():
    png = sample("PNG_GRADED_LOST")
    finding = c7_transparency_survived_conversion(png, png.with_suffix(".paa"))
    assert finding.status == WARN
    assert "DXT1" in finding.detail


@needs("PNG_GRADED_KEPT")
def test_c7_stays_silent_on_the_real_graded_texture_that_kept_its_alpha():
    png = sample("PNG_GRADED_KEPT")
    assert c7_transparency_survived_conversion(png, png.with_suffix(".paa")).status == PASS


@needs("PNG_OPAQUE")
def test_c7_stays_silent_on_the_real_opaque_texture_in_dxt1():
    png = sample("PNG_OPAQUE")
    assert c7_transparency_survived_conversion(png, png.with_suffix(".paa")).status == PASS


@needs("ODOL", "MODEL_CFG")
def test_c8_stays_silent_on_the_real_build_that_kept_its_animations():
    art = read_artifact(sample("ODOL"))
    cfg = parse_model_cfg(sample("MODEL_CFG").read_text(encoding="utf-8", errors="ignore"))
    assert c8_animations_reached_the_artifact(art, cfg).status == PASS


@needs("ODOL_NO_ANIM", "MODEL_CFG_NO_BONES")
def test_c8_catches_the_real_build_whose_animations_were_dropped():
    """A defect on this disk that nothing reported: the model.cfg declares ten
    animations, its skeleton declares no bones, and the ODOL carries none of
    the ten. binarize exited 0."""
    art = read_artifact(sample("ODOL_NO_ANIM"))
    cfg = parse_model_cfg(sample("MODEL_CFG_NO_BONES").read_text(encoding="utf-8", errors="ignore"))
    finding = c8_animations_reached_the_artifact(art, cfg)
    assert finding.status == WARN
    assert finding.evidence


@needs("MODEL_CFG")
@needs_value("HIDDEN_SELECTION")
def test_c9_stays_silent_on_the_real_model_cfg_that_declares_what_it_animates():
    cfg = parse_model_cfg(sample("MODEL_CFG").read_text(encoding="utf-8", errors="ignore"))
    finding = c9_selections_are_declared(
        cfg, corpus_model_name("MODEL_CFG"), hidden_selections=[text("HIDDEN_SELECTION")])
    assert finding.status == PASS, finding.detail


@needs("MODEL_CFG")
@needs_value("HIDDEN_SELECTION")
def test_c9_catches_a_hidden_selection_that_is_not_a_section():
    """Derived from the real one so the name is provably absent rather than
    invented: the same selection with a suffix nothing declares."""
    cfg = parse_model_cfg(sample("MODEL_CFG").read_text(encoding="utf-8", errors="ignore"))
    absent = text("HIDDEN_SELECTION") + "_absent"
    finding = c9_selections_are_declared(
        cfg, corpus_model_name("MODEL_CFG"), hidden_selections=[absent])
    assert finding.status == WARN
    assert absent in finding.detail


@needs("MODEL_CFG_NO_BONES", "ODOL_NO_ANIM")
def test_c9_catches_the_real_empty_skeleton_upstream_of_the_artifact():
    cfg = parse_model_cfg(sample("MODEL_CFG_NO_BONES").read_text(encoding="utf-8", errors="ignore"))
    finding = c9_selections_are_declared(cfg, sample("ODOL_NO_ANIM").stem)
    assert finding.status == WARN
    assert "skeletonBones" in finding.action


@needs("ODOL")
def test_c10_refuses_the_real_working_artifact_as_a_binarize_input():
    finding = c10_binarize_input_is_a_source(sample("ODOL"))
    assert finding.status == REFUSE
    assert finding.action


@needs("MLOD")
def test_c10_passes_the_real_mlod_source():
    assert c10_binarize_input_is_a_source(sample("MLOD")).status == PASS


@needs("MODEL_CFG", "MODEL_CFG_BUILT")
def test_c11_catches_the_real_model_cfg_the_artifact_was_not_built_from():
    """A live desync on this disk: the copy shipped inside the mod declares one
    animation fewer than the copy binarize actually read, and its timestamp is
    OLDER than the artifact -- so a clock comparison alone calls it fine."""
    finding = c11_model_cfg_is_the_one_it_was_built_from(
        sample("MODEL_CFG"), sample("MODEL_CFG_BUILT"))
    assert finding.status == WARN
    assert finding.evidence


@needs("MODEL_CFG_BUILT")
def test_c11_stays_silent_when_both_copies_are_the_same_file():
    finding = c11_model_cfg_is_the_one_it_was_built_from(
        sample("MODEL_CFG_BUILT"), sample("MODEL_CFG_BUILT"))
    assert finding.status == PASS


@needs("ODOL", "ODOL_NO_RVMAT")
def test_c12_tells_the_real_good_build_from_the_real_broken_one():
    good = read_artifact(sample("ODOL"))
    broken = read_p3d(sample("ODOL_NO_RVMAT"))
    assert c12_fingerprint_matches_the_recorded_one(good, fingerprint(broken)).status == WARN
    assert c12_fingerprint_matches_the_recorded_one(good, fingerprint(good.info)).status == PASS


# ------------------------------------------------------ the acceptance shape


def corpus_model_name(role: str) -> str:
    """The model class a model.cfg sample belongs to: the artifact's own stem.

    Kept here rather than in another environment variable because binarize
    resolves it the same way -- `thing.p3d` is built from `class thing`.
    """
    return {"MODEL_CFG": sample("ODOL").stem, "MODEL_CFG_NO_BONES": sample("ODOL_NO_ANIM").stem}[role]


@needs("ODOL", "ROOT", "MODEL_CFG")
@needs_value("PREFIX")
def test_the_whole_report_refuses_nothing_on_the_real_working_build():
    """The acceptance shape, stated once: the artifact that works in game
    produces no refusal from any of the twelve."""
    report = check_model(
        sample("ODOL"),
        prefix=text("PREFIX"),
        roots={text("PREFIX"): sample("ROOT")},
        model_cfg=sample("MODEL_CFG"),
    )
    assert report.ok, report.summary
    assert one(report, "C1").status == PASS
    assert one(report, "C3").status == PASS
    assert one(report, "C4").status == PASS
    assert one(report, "C5").status == PASS


@needs("ODOL_FOREIGN")
@needs_value("PREFIX_ALT")
def test_the_whole_report_refuses_the_real_build_with_the_lost_root():
    report = check_model(sample("ODOL_FOREIGN"), prefix=text("PREFIX_ALT"))
    assert not report.ok
    assert {f.check for f in report.refusals} == {"C3", "C4"}


@needs("ODOL_NO_RVMAT")
@needs_value("PREFIX_NO_RVMAT")
def test_the_whole_report_refuses_the_real_build_made_in_the_wrong_directory():
    """The hardest of the four: nothing about its paths is wrong. Only C4
    separates it from a build that works."""
    report = check_model(sample("ODOL_NO_RVMAT"), prefix=text("PREFIX_NO_RVMAT"))
    assert not report.ok
    assert {f.check for f in report.refusals} == {"C4"}


@needs("ODOL", "MLOD")
def test_the_lod_count_drops_between_the_real_source_and_the_real_artifact():
    """Pinned as a fact, not as a check. binarize collapses one LOD, so the
    obvious "same number of LODs in and out" rule would refuse a build that
    works in game -- which is why no check here compares them."""
    source = read_p3d(sample("MLOD"))
    built = read_p3d(sample("ODOL"))
    assert source.lod_count == built.lod_count + 1
