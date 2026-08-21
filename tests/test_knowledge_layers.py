"""Three layers, three rhythms, and one rebuild that must not redo the world.

The layers exist because their sources change at completely different rates:
the game is updated every few weeks, a dependency when its author publishes,
and the project's own code between one agent turn and the next. An index that
rebuilds all three together is an index nobody rebuilds, and an index nobody
rebuilds answers about yesterday's code with today's confidence.

So the measurement that matters here is not "does it index" -- it is **what
does an edit cost**. The store measured one file at 4.1 ms against 3.93 s for a
whole layer. These tests hold the layer above it to that ratio end to end: an
edited file must re-read exactly itself, a deleted one must disappear, and an
untouched layer must re-read nothing at all.

The second thing under test is what a third-party mod is allowed to do to a
build. The store refuses a duplicate record key loudly, on purpose. Loud is
right for a bug in our own parser and wrong for a quirk in somebody else's
archive: failing an entire dependency layer because one of three dozen mods
ships one odd file would make the layer useless, and dropping the record
without a word is the exact failure this phase exists to prevent. The middle
these tests pin down is: keep the layer, keep the count, name the file.
"""
import os
import struct
import time
from pathlib import Path

import pytest

from dayz_mcp.knowledge.layers import (
    FAIL,
    REPORT,
    LayerReport,
    build_core,
    build_deps,
    build_project,
    dependency_dirs,
    scan_tree,
    staleness_of,
)
from dayz_mcp.knowledge import layers as layers_mod
from dayz_mcp.knowledge.parse import CLASS, CONFIG, METHOD, Declaration, parse_config
from dayz_mcp.knowledge.pbo import (
    CPRS,
    PboError,
    PboLimits,
    is_decoy_name,
    lzss_decompress,
    read_index,
    scan_pbo,
)
from dayz_mcp.knowledge.store import CORE, DEPS, PROJECT, KnowledgeStore, record_key


# ------------------------------------------------------------------ helpers


@pytest.fixture
def store(tmp_path):
    with KnowledgeStore(tmp_path / "index" / "knowledge.db") as s:
        yield s


def write(root: Path, rel: str, text: str) -> Path:
    path = Path(root) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def touch(path: Path, text: str) -> None:
    """Rewrite a file and make sure the change is visible to a stat-based
    comparison, whatever the filesystem's timestamp resolution is."""
    path.write_text(text, encoding="utf-8")
    later = time.time() + 10
    os.utime(path, (later, later))


def pbo_bytes(entries, props=None) -> bytes:
    """A PBO built by hand: header entries, terminator, then the data blobs.

    `entries` are (name, blob) or (name, blob, packing, original) -- the second
    form is for testing a compressed entry, where the stored bytes and their
    decompressed length differ.
    """
    head = bytearray()
    if props is not None:
        head += b"\x00"
        head += struct.pack("<IIIII", 0x56657273, 0, 0, 0, 0)
        for key, value in props.items():
            head += key.encode() + b"\x00" + value.encode() + b"\x00"
        head += b"\x00"
    data = bytearray()
    for entry in entries:
        name, blob = entry[0], entry[1]
        packing = entry[2] if len(entry) > 2 else 0
        original = entry[3] if len(entry) > 3 else len(blob)
        head += name.encode("utf-8") + b"\x00"
        head += struct.pack("<IIIII", packing, original, 0, 0, len(blob))
        data += blob
    head += b"\x00" + struct.pack("<IIIII", 0, 0, 0, 0, 0)
    return bytes(head + data)


def make_pbo(path: Path, entries, props=None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(pbo_bytes(entries, props))
    return path


def mod_dir(root: Path, name: str, entries) -> Path:
    """A mod folder the way a workshop mod is laid out: addons/<name>.pbo."""
    folder = root / name
    make_pbo(folder / "addons" / f"{name.lstrip('@')}.pbo", entries)
    return folder


CLASS_SOURCE = "class Alpha extends Beta { void Ping(); }\n"
OTHER_SOURCE = "class Gamma { void Pong(); }\n"


# ---------------------------------------------------------------- the archive


def test_a_pbo_gives_up_its_entries_without_being_unpacked():
    """The whole deps layer stands on this: a mod's scripts are a few hundred
    kilobytes inside an archive that is routinely two gigabytes of models and
    textures, and unpacking the archive to read them is not a trade anyone
    would make twice."""
    raw = pbo_bytes([("scripts/a.c", b"one"), ("scripts/b.c", b"two")])
    entries, props, _end = read_index(raw, wanted=lambda name: True)
    assert [e.name for e in entries] == ["scripts/a.c", "scripts/b.c"]
    assert [e.size for e in entries] == [3, 3]
    # Offsets are absolute, so the second entry can be read without touching
    # the first.
    assert raw[entries[1].offset : entries[1].offset + 3] == b"two"


def test_only_wanted_entries_are_kept_in_memory():
    """A real archive carries three quarters of a million entries. Holding the
    ones nobody asked for is how a reader turns into a memory problem."""
    raw = pbo_bytes([("a.paa", b"xx"), ("scripts/b.c", b"yy"), ("c.p3d", b"zz")])
    entries, _props, _end = read_index(raw, wanted=lambda name: name.endswith(".c"))
    assert [e.name for e in entries] == ["scripts/b.c"]
    # The offset still accounts for the entries that were skipped.
    assert raw[entries[0].offset : entries[0].offset + 2] == b"yy"


def test_the_properties_entry_is_read_and_stepped_over():
    raw = pbo_bytes([("scripts/a.c", b"one")], props={"prefix": "SomeMod"})
    entries, props, _end = read_index(raw, wanted=lambda name: True)
    assert props["prefix"] == "SomeMod"
    assert [e.name for e in entries] == ["scripts/a.c"]


def test_a_compressed_entry_is_decompressed_on_the_way_out(tmp_path):
    packed = b"\xff" + b"ABCDEFGH" + b"\x00" + b"\x08\x00"
    path = make_pbo(tmp_path / "x.pbo", [("scripts/a.c", packed, CPRS, 11)])
    got = dict(
        (entry.name, blob) for entry, blob in scan_pbo(path, lambda name: True)
    )
    assert got["scripts/a.c"] == b"ABCDEFGHABC"


def test_lzss_fills_with_spaces_before_the_start():
    """Bohemia's decoder reads positions before the output start as 0x20. A
    ring-buffer variant decodes the first bytes right and then diverges, which
    is the kind of wrong that produces plausible garbage."""
    # One back-reference, distance 4096, at output position 0.
    assert lzss_decompress(b"\x00\x00\x00", 3) == b"   "


def test_a_truncated_header_is_refused_rather_than_guessed():
    with pytest.raises(PboError):
        read_index(b"scripts/a.c\x00\x01\x02", wanted=lambda name: True)


def test_an_entry_table_past_the_ceiling_is_refused(tmp_path):
    """A real archive on this machine carries two million entries in a 252 MB
    entry table. Walking it costs more memory than the answer is worth, so the
    reader stops -- and the layer above reports the archive by name instead of
    pretending it held nothing."""
    raw = pbo_bytes([(f"s{i}.c", b"x") for i in range(20)])
    with pytest.raises(PboError):
        read_index(raw, wanted=lambda n: True, limits=PboLimits(max_entries=5))
    with pytest.raises(PboError):
        read_index(raw, wanted=lambda n: True, limits=PboLimits(header_bytes=32))


def test_decoy_entry_names_are_recognised():
    """Obfuscated archives pad themselves with hundreds of thousands of
    entries whose names carry zero-width characters and reserved device names.
    Measured on this machine's modpack: 7.6 million entries, of which under
    five thousand hold anything that parses."""
    assert is_decoy_name("scripts/3_game/player.c") is False
    assert is_decoy_name("config.cpp") is False
    assert is_decoy_name("scenes/x​/COM6.{4BD8D571}.c") is True
    assert is_decoy_name("gui/LPT2.c") is True
    assert is_decoy_name("gui/﻿/a.c") is True


# ----------------------------------------------------------------- the config


def test_a_config_class_is_indexed_with_its_parent():
    decls = parse_config(
        "class CfgVehicles\n{\n\tclass Thing: Base\n\t{\n\t\tscope=2;\n\t};\n};\n",
        file="config.cpp",
    )
    by_name = {d.name: d for d in decls}
    assert by_name["Thing"].parent == "Base"
    assert by_name["Thing"].owner == "CfgVehicles"
    assert by_name["Thing"].kind == CONFIG
    assert by_name["Thing"].line == 3


def test_a_forward_declaration_is_not_a_declaration():
    """Every mod's config.cpp opens by naming the vanilla classes it extends.
    Indexing those would have each mod claim to declare half the game -- the
    confident wrongness this phase exists to remove."""
    decls = parse_config("class Base;\nclass Thing: Base\n{\n};\n", file="config.cpp")
    assert [d.name for d in decls] == ["Thing"]


def test_config_arrays_and_values_are_not_declarations():
    decls = parse_config(
        'class Thing\n{\n\tdisplayName="x";\n\tmagazines[]={"a","b"};\n'
        '\tclass Nested {};\n};\n',
        file="config.cpp",
    )
    assert sorted(d.name for d in decls) == ["Nested", "Thing"]
    assert next(d for d in decls if d.name == "Nested").owner == "Thing"


def test_a_class_named_inside_a_config_comment_or_string_is_not_indexed():
    decls = parse_config(
        'class Real {};\n// class Commented {};\n/* class Blocked {}; */\n'
        'class Str { name="class Quoted {}"; };\n',
        file="config.cpp",
    )
    assert sorted(d.name for d in decls) == ["Real", "Str"]


# ------------------------------------------------------------ walking a tree


def test_scan_tree_reports_size_and_time_with_the_walk(tmp_path):
    """Staleness costs 410 ms on the vanilla layer when every source is
    stat'ed one at a time. The directory walk already carries size and
    modification time, so the layer pays for them once instead of twice."""
    write(tmp_path, "a.c", CLASS_SOURCE)
    write(tmp_path, "sub/b.c", OTHER_SOURCE)
    write(tmp_path, "skip.txt", "not a source")
    found = {Path(f.path).name: f for f in scan_tree(tmp_path, suffixes=(".c",))}
    assert set(found) == {"a.c", "b.c"}
    on_disk = (tmp_path / "a.c").stat()
    assert found["a.c"].size == on_disk.st_size
    assert found["a.c"].mtime == pytest.approx(on_disk.st_mtime)


def test_scan_tree_skips_what_is_never_a_source(tmp_path):
    write(tmp_path, ".git/objects/x.c", CLASS_SOURCE)
    write(tmp_path, "@B/addons/y.c", CLASS_SOURCE)
    write(tmp_path, ".dayz-mcp/jobs/z.c", CLASS_SOURCE)
    write(tmp_path, "keep.c", CLASS_SOURCE)
    found = [Path(f.path).name for f in scan_tree(tmp_path, suffixes=(".c",))]
    assert found == ["keep.c"]


def test_scan_tree_matches_names_case_insensitively(tmp_path):
    """One machine's modpack spells the same folder `addons` and `Addons`, and
    a config is `config.cpp` in one mod and `Config.cpp` in the next."""
    write(tmp_path, "Config.cpp", "class Thing {};")
    write(tmp_path, "b.C", CLASS_SOURCE)
    found = {Path(f.path).name for f in scan_tree(tmp_path, suffixes=(".c",), names=("config.cpp",))}
    assert found == {"Config.cpp", "b.C"}


# ---------------------------------------------------------- the project layer


def test_the_project_layer_reads_sources_where_they_lie(tmp_path, store):
    """No unpacking: the project's own code is on disk, and it is the layer
    that goes stale between one agent turn and the next."""
    root = tmp_path / "mod"
    write(root, "MyMod/scripts/4_world/thing.c", CLASS_SOURCE)
    write(root, "MyMod/config.cpp", "class CfgPatches { class MyMod {}; };")

    report = build_project(store, root)

    assert report.layer == PROJECT
    assert report.sources == 2
    assert report.declarations > 0
    found = store.find("Alpha", layer=PROJECT)
    assert found and found[0].kind == CLASS
    # Recorded relative to the project, so the answer reads like the repository
    # and not like this machine.
    assert found[0].file == str(Path("MyMod/scripts/4_world/thing.c"))
    assert store.find("MyMod", layer=PROJECT, kind=CONFIG)


def test_a_second_build_with_nothing_edited_reads_nothing(tmp_path, store):
    root = tmp_path / "mod"
    write(root, "a.c", CLASS_SOURCE)
    write(root, "b.c", OTHER_SOURCE)
    build_project(store, root)

    again = build_project(store, root)
    assert again.indexed == 0
    assert again.unchanged == 2
    assert again.incremental is True
    assert store.count(PROJECT) > 0


def test_one_edited_file_reindexes_exactly_that_file(tmp_path, store):
    """The measurement the whole phase turns on: 4.1 ms against 3.93 s. A
    rebuild that re-reads everything would pass every other test here."""
    root = tmp_path / "mod"
    a = write(root, "a.c", CLASS_SOURCE)
    write(root, "b.c", OTHER_SOURCE)
    build_project(store, root)

    touch(a, "class Alpha extends Beta { void Ping(); void Pong(); }\n")
    report = build_project(store, root)

    assert report.indexed == 1
    assert report.unchanged == 1
    assert report.removed == 0
    assert store.find("Pong", owner="Alpha", layer=PROJECT)


def test_a_deleted_file_leaves_the_layer(tmp_path, store):
    root = tmp_path / "mod"
    write(root, "a.c", CLASS_SOURCE)
    b = write(root, "b.c", OTHER_SOURCE)
    build_project(store, root)
    assert store.find("Gamma", layer=PROJECT)

    b.unlink()
    report = build_project(store, root)

    assert report.removed == 1
    assert report.indexed == 0
    assert not store.find("Gamma", layer=PROJECT)
    assert store.find("Alpha", layer=PROJECT)


def test_a_new_file_joins_the_layer(tmp_path, store):
    root = tmp_path / "mod"
    write(root, "a.c", CLASS_SOURCE)
    build_project(store, root)

    write(root, "c.c", "class Delta {}\n")
    report = build_project(store, root)

    assert report.indexed == 1
    assert report.sources == 2
    assert store.find("Delta", layer=PROJECT)


def test_a_full_rebuild_is_available_and_says_so(tmp_path, store):
    root = tmp_path / "mod"
    write(root, "a.c", CLASS_SOURCE)
    write(root, "b.c", OTHER_SOURCE)
    build_project(store, root)

    report = build_project(store, root, full=True)
    assert report.incremental is False
    assert report.indexed == 2
    assert store.count(PROJECT) == report.declarations


def test_a_file_rewritten_to_the_same_size_at_the_same_time_is_still_caught(tmp_path, store):
    """Not a hypothetical: restoring from a copy, unpacking an archive and
    copying with a tool all preserve the modification time."""
    root = tmp_path / "mod"
    a = write(root, "a.c", "class Alpha {}\n")
    build_project(store, root)
    before = a.stat()

    a.write_text("class Omega {}\n", encoding="utf-8")
    os.utime(a, (before.st_atime, before.st_mtime))
    assert a.stat().st_size == before.st_size

    report = build_project(store, root)
    # Same size, same mtime: this is the known blind spot, stated rather than
    # hidden -- and a full rebuild is the way out of it.
    assert report.indexed == 0
    report = build_project(store, root, full=True)
    assert store.find("Omega", layer=PROJECT)


def test_the_incremental_path_is_actually_faster(tmp_path, store):
    """Incrementality that is written but not measured is a claim. Two hundred
    files is small next to the real corpus and already enough for the
    difference to be an order of magnitude."""
    root = tmp_path / "mod"
    for i in range(200):
        write(root, f"s{i}.c", f"class C{i} extends Base {{ void M{i}(); }}\n")
    build_project(store, root, full=True)

    started = time.perf_counter()
    build_project(store, root, full=True)
    full = time.perf_counter() - started

    touch(root / "s7.c", "class C7 extends Base { void M7(); void Extra(); }\n")
    started = time.perf_counter()
    report = build_project(store, root)
    incremental = time.perf_counter() - started

    assert report.indexed == 1
    assert incremental * 5 < full, f"incremental {incremental:.4f}s vs full {full:.4f}s"


def test_staleness_is_measured_without_a_second_pass_over_the_disk(tmp_path, store, monkeypatch):
    """The store's own measurement stats every source: 410 ms for the vanilla
    layer, paid on every status check. The walk already carried those numbers,
    so this comparison must not go back to the disk for them at all."""
    root = tmp_path / "mod"
    a = write(root, "a.c", CLASS_SOURCE)
    write(root, "b.c", OTHER_SOURCE)
    build_project(store, root)

    touch(a, "class Alpha { void Ping(); void Extra(); }\n")
    scanned = scan_tree(root, suffixes=(".c",))

    def refuse(*args, **kwargs):
        raise AssertionError("staleness went back to the disk")

    monkeypatch.setattr(os, "stat", refuse)
    state = staleness_of(store, PROJECT, scanned)

    assert state.changed == (str(a),)
    assert state.unchanged == 1
    assert state.scanned_for_new is True
    assert state.stale is True


# ------------------------------------------------------- the duplicate answer


DOUBLED = "class Twin extends A {}; class Twin extends B {};\n"


def test_a_duplicate_inside_one_mod_file_costs_the_record_not_the_layer(tmp_path, store):
    """A mod ships one minified file where two declarations land on the same
    line with the same name -- indistinguishable to the record key, and so to
    every question the index can be asked. Failing the whole layer over it
    would cost thirty-five other mods; dropping it without a word is the
    silent loss this phase exists to prevent. The middle: keep the layer,
    count the loss, name the file."""
    root = tmp_path / "mods"
    mod_dir(root, "@Dep", [("scripts/twin.c", DOUBLED.encode())])
    mod_dir(root, "@SomeDependency", [("scripts/ok.c", OTHER_SOURCE.encode())])

    report = build_deps(store, [root / "@Dep", root / "@SomeDependency"], on_duplicate=REPORT)

    assert report.sources == 2
    assert store.find("Gamma", layer=DEPS)
    assert store.find("Twin", layer=DEPS)
    assert report.lost == 1
    assert report.skipped and "twin.c" in report.skipped[0].path
    assert "Twin" in report.skipped[0].reason


def test_strict_mode_still_refuses_loudly(tmp_path, store):
    """The loud contract is not deleted, it is made a choice. Our own parser
    emitting two identical records is a bug, and a bug should stop the build."""
    root = tmp_path / "mods"
    mod_dir(root, "@Dep", [("scripts/twin.c", DOUBLED.encode())])
    with pytest.raises(layers_mod.LayerBuildError):
        build_deps(store, [root / "@Dep"], on_duplicate=FAIL)


def test_a_collision_the_layer_did_not_foresee_costs_only_its_source(tmp_path, store, monkeypatch):
    """Belt and braces. If the layer's idea of the record key ever drifted from
    the store's, the store would raise where the layer expected no trouble --
    and the answer must still be one named source lost, never a dead layer."""
    root = tmp_path / "mods"
    mod_dir(root, "@Dep", [("scripts/twin.c", DOUBLED.encode())])
    mod_dir(root, "@SomeDependency", [("scripts/ok.c", OTHER_SOURCE.encode())])

    counter = iter(range(10_000))
    monkeypatch.setattr(layers_mod, "record_key", lambda layer, d: next(counter))

    report = build_deps(store, [root / "@Dep", root / "@SomeDependency"])

    assert store.find("Gamma", layer=DEPS)
    assert report.sources == 1
    # The archive is what could not be written; the store's own message says
    # which declaration inside it caused that, and both reach the report.
    assert report.skipped
    assert any("twin.c" in s.path + s.reason for s in report.skipped)


def test_the_record_key_the_layer_uses_is_the_one_the_store_enforces(store):
    """One formula, one owner. Two spellings of the record key is how a
    deduplication quietly stops matching the constraint it exists to satisfy."""
    twin = Declaration(name="T", kind=CLASS, owner="O", file="f.c", line=3)
    other = Declaration(name="T", kind=METHOD, owner="O", file="f.c", line=3)
    assert record_key(PROJECT, twin) == record_key(PROJECT, twin)
    assert record_key(PROJECT, twin) != record_key(PROJECT, other)
    assert record_key(PROJECT, twin) != record_key(CORE, twin)
    # And the store agrees: same key, refused; different key, accepted.
    store.put_source(PROJECT, "f.c", [twin, other], size=1, mtime=1.0)
    with pytest.raises(Exception):
        store.put_source(PROJECT, "g.c", [twin, twin], size=1, mtime=1.0)


# ------------------------------------------------------------- the deps layer


def test_the_deps_layer_reads_the_mods_the_project_declares(tmp_path, store):
    root = tmp_path / "mods"
    mod_dir(root, "@Dep", [("scripts/a.c", CLASS_SOURCE.encode()),
                           ("config.cpp", b"class CfgPatches { class Packed {}; };")])
    mod_dir(root, "@SomeDependency", [("scripts/b.c", OTHER_SOURCE.encode())])

    report = build_deps(store, [root / "@Dep", root / "@SomeDependency"])

    assert report.layer == DEPS
    assert report.sources == 2  # one pbo each, and a pbo is the unit
    found = store.find("Alpha", layer=DEPS)
    assert found
    # The label says which mod, which archive and which entry -- an answer
    # that names a file nobody can open is barely an answer.
    assert found[0].file.replace("\\", "/") == "@Dep/Dep/scripts/a.c"
    assert store.find("Packed", layer=DEPS, kind=CONFIG)


def test_a_mod_updated_reindexes_only_its_own_archive(tmp_path, store):
    """A mod is republished; the other thirty-five have not changed. The
    archive is the incremental unit here, exactly as the file is for the
    project."""
    root = tmp_path / "mods"
    one = mod_dir(root, "@Dep", [("scripts/a.c", CLASS_SOURCE.encode())])
    mod_dir(root, "@SomeDependency", [("scripts/b.c", OTHER_SOURCE.encode())])
    build_deps(store, [one, root / "@SomeDependency"])

    pbo = one / "addons" / "Dep.pbo"
    pbo.write_bytes(pbo_bytes([("scripts/a.c", b"class Alpha { void Fresh(); }\n")]))
    later = time.time() + 10
    os.utime(pbo, (later, later))

    report = build_deps(store, [one, root / "@SomeDependency"])
    assert report.indexed == 1
    assert report.unchanged == 1
    assert store.find("Fresh", layer=DEPS)


def test_an_unreadable_archive_is_named_and_the_layer_survives(tmp_path, store):
    """Three archives in this machine's modpack carry entry tables past any
    ceiling worth walking. The layer must come out with the other thirty-three
    mods and a sentence about the three."""
    root = tmp_path / "mods"
    good = mod_dir(root, "@Name", [("scripts/a.c", CLASS_SOURCE.encode())])
    bad = root / "@ModName" / "addons" / "ModName.pbo"
    bad.parent.mkdir(parents=True)
    bad.write_bytes(b"not a pbo at all, no terminator here")

    report = build_deps(store, [good, root / "@ModName"])

    assert store.find("Alpha", layer=DEPS)
    assert [Path(s.path).name for s in report.skipped] == ["ModName.pbo"]
    assert report.skipped[0].reason


def test_decoy_entries_do_not_reach_the_index(tmp_path, store):
    root = tmp_path / "mods"
    mod_dir(root, "@Dep", [
        ("scripts/real.c", CLASS_SOURCE.encode()),
        ("scenes/​/COM6.{1}.c", b"class Decoy {}\n"),
    ])
    build_deps(store, [root / "@Dep"])
    assert store.find("Alpha", layer=DEPS)
    assert not store.find("Decoy", layer=DEPS)


def test_a_binary_config_goes_through_the_converter(tmp_path, store):
    """`CfgConvert -txt` is the only thing that reads a binarised config, and
    a binarised config is what most published mods ship."""
    root = tmp_path / "mods"
    mod_dir(root, "@Dep", [("config.bin", b"\x00raPfake binary config")])
    calls = []

    def fake_convert(source: Path, dest: Path) -> str:
        calls.append((Path(source).name, Path(dest).name))
        dest.write_text("class Converted: Base {};\n", encoding="utf-8")
        return ""

    report = build_deps(store, [root / "@Dep"], convert=fake_convert)

    assert calls and calls[0][0] == "config.bin"
    found = store.find("Converted", layer=DEPS)
    assert found and found[0].kind == CONFIG and found[0].parent == "Base"
    assert not report.notes or all("CfgConvert" not in n for n in report.notes)


def test_the_same_entry_name_twice_in_one_archive_keeps_both(tmp_path, store):
    """Found on the real modpack, not imagined: 112 of 523 archives repeat an
    entry name, 127 495 times between them. Under one label the second copy
    claims the first one's record row and is dropped -- 25 real declarations
    went that way before the repeats were numbered."""
    root = tmp_path / "mods"
    mod_dir(root, "@Dep", [
        ("scripts/dup.c", b"class First {}\n"),
        ("scripts/dup.c", b"class Second {}\n"),
    ])

    report = build_deps(store, [root / "@Dep"])

    assert report.lost == 0
    assert store.find("First", layer=DEPS)
    assert store.find("Second", layer=DEPS)
    files = {r.file for r in store.find("", prefix=True, layer=DEPS)}
    assert any(f.endswith("#2") for f in files)


def test_a_converter_that_refuses_a_file_is_not_reported_as_a_missing_tool(tmp_path, store):
    """"There is no CfgConvert" and "CfgConvert refused this file" are
    different facts. One note covering both told the reader to install a tool
    that was installed and working -- a wrong hint is worse than no hint,
    because it is acted on."""
    root = tmp_path / "mods"
    mod_dir(root, "@Dep", [("config.bin", b"\x00raPnot really binarised")])

    def refuses(source: Path, dest: Path) -> str:
        return "'.raP': '' encountered instead of '='"

    report = build_deps(store, [root / "@Dep"], convert=refuses)

    assert any("refused" in note and ".raP" in note for note in report.notes)
    assert not any("was not found" in note for note in report.notes)


def test_an_archive_that_cannot_be_read_is_not_walked_again(tmp_path, store):
    """A padded archive costs a full walk of its entry table before the
    ceiling stops it -- 2.2 s for three of them on the real modpack, and a
    staleness check must not pay that every time it is asked."""
    root = tmp_path / "mods"
    mod_dir(root, "@Name", [("scripts/a.c", CLASS_SOURCE.encode())])
    bad = root / "@ModName" / "addons" / "ModName.pbo"
    bad.parent.mkdir(parents=True)
    bad.write_bytes(b"not a pbo at all, no terminator here")

    first = build_deps(store, [root / "@Name", root / "@ModName"])
    assert [Path(s.path).name for s in first.skipped] == ["ModName.pbo"]

    again = build_deps(store, [root / "@Name", root / "@ModName"])
    assert again.indexed == 0
    assert again.unchanged == 2
    assert again.skipped == ()

    # ...and it is read again the moment the archive itself moves.
    bad.write_bytes(pbo_bytes([("scripts/b.c", OTHER_SOURCE.encode())]))
    later = time.time() + 10
    os.utime(bad, (later, later))
    third = build_deps(store, [root / "@Name", root / "@ModName"])
    assert third.indexed == 1
    assert store.find("Gamma", layer=DEPS)


def test_a_binary_config_without_the_converter_is_a_note_not_a_silence(tmp_path, store):
    root = tmp_path / "mods"
    mod_dir(root, "@Dep", [("config.bin", b"\x00raPfake"),
                           ("scripts/a.c", CLASS_SOURCE.encode())])

    report = build_deps(store, [root / "@Dep"], convert=None)

    assert store.find("Alpha", layer=DEPS)
    assert any("config.bin" in note for note in report.notes)
    assert any("CfgConvert" in note for note in report.notes)


def test_dependency_dirs_leaves_out_the_projects_own_mods(tmp_path):
    """The project's own code belongs to the project layer. Indexing it twice
    would make every one of its declarations answer twice, from two layers,
    with the same file."""

    class FakeProfile:
        root = tmp_path / "repo"
        own_mod_dirs = ["@MyMod"]

        class mods:  # noqa: N801 - mirrors the profile's shape
            required = ["@Dep", "@MyMod"]
            extra = [str(tmp_path / "elsewhere" / "@B")]
            server_only = []

    dirs = [str(p) for p in dependency_dirs(FakeProfile, game=str(tmp_path / "game"))]
    assert str(tmp_path / "game" / "!Workshop" / "@Dep") in dirs
    assert str(tmp_path / "elsewhere" / "@B") in dirs
    assert not any("@MyMod" in d for d in dirs)


# ------------------------------------------------------------- the core layer


def test_the_core_layer_indexes_an_already_unpacked_corpus(tmp_path, store):
    corpus = tmp_path / "scripts"
    write(corpus, "3_game/thing.c", CLASS_SOURCE)
    write(corpus, "4_world/other.c", OTHER_SOURCE)

    report = build_core(store, scripts=corpus, configs=False)

    assert report.layer == CORE
    assert report.sources == 2
    found = store.find("Alpha", layer=CORE)
    assert found and found[0].file == str(Path("3_game/thing.c"))


def test_the_core_layer_unpacks_the_game_when_there_is_no_corpus(tmp_path, store):
    """BankRev is the documented way in. When DayZ Tools is not installed the
    reader that already reads mod archives reads this one too -- the server
    stays a thing you can start anywhere, which is the whole reason it carries
    no external services."""
    game = tmp_path / "game"
    make_pbo(game / "dta" / "scripts.pbo",
             [("3_game/thing.c", CLASS_SOURCE.encode()),
              ("4_world/other.c", OTHER_SOURCE.encode())])

    report = build_core(
        store, game=game, tools=None, workdir=tmp_path / "work", configs=False
    )

    assert store.find("Alpha", layer=CORE)
    assert report.sources == 2
    assert any("scripts.pbo" in note for note in report.notes)


def test_the_core_layer_indexes_the_games_own_configs(tmp_path, store):
    """"Is there a class with this name in the game" is answered by the
    configs, not by the scripts, and it is a question that came up twice in
    one session of ordinary work."""
    game = tmp_path / "game"
    corpus = tmp_path / "scripts"
    write(corpus, "3_game/thing.c", CLASS_SOURCE)
    make_pbo(game / "Addons" / "dz_data.pbo", [("config.bin", b"\x00raPbinary")])

    def fake_convert(source: Path, dest: Path) -> str:
        dest.write_text("class CfgVehicles { class Barrel_Blue: Container {}; };\n",
                        encoding="utf-8")
        return ""

    build_core(store, scripts=corpus, game=game, convert=fake_convert)

    found = store.find("Barrel_Blue", layer=CORE)
    assert found and found[0].kind == CONFIG and found[0].parent == "Container"
    assert store.find("Alpha", layer=CORE)


def test_the_core_layer_goes_stale_with_the_game_not_with_the_project(tmp_path, store):
    """Three layers exist for exactly this: one timestamp cannot tell "the
    author saved a file" from "the game was updated"."""
    corpus = tmp_path / "scripts"
    write(corpus, "a.c", CLASS_SOURCE)
    project = tmp_path / "mod"
    write(project, "own.c", OTHER_SOURCE)
    build_core(store, scripts=corpus, configs=False)
    build_project(store, project)

    touch(project / "own.c", "class Gamma { void Extra(); }\n")

    core = build_core(store, scripts=corpus, configs=False)
    assert core.indexed == 0
    assert build_project(store, project).indexed == 1


# ------------------------------------------------------------------ reporting


def test_a_report_survives_the_trip_through_json(tmp_path, store):
    """Task 4 puts this inside the `{ok, data, error, hint}` envelope. A
    dataclass that cannot be serialised would be found there, in the tool, at
    the worst possible time."""
    import json

    root = tmp_path / "mod"
    write(root, "a.c", CLASS_SOURCE)
    report = build_project(store, root)
    assert isinstance(report, LayerReport)
    data = json.loads(json.dumps(report.to_dict()))
    assert data["layer"] == PROJECT
    assert data["declarations"] > 0
    assert data["seconds"] >= 0
    assert "skipped" in data and "notes" in data


def test_a_report_says_what_it_did_in_a_sentence(tmp_path, store):
    root = tmp_path / "mod"
    write(root, "a.c", CLASS_SOURCE)
    write(root, "b.c", OTHER_SOURCE)
    build_project(store, root)
    touch(root / "a.c", "class Alpha { void Extra(); }\n")
    assert "1" in build_project(store, root).describe()


# -------------------------------------------------- the real data, if present

VANILLA = Path(os.environ.get("DAYZ_MCP_VANILLA_SCRIPTS", ""))
MODPACK = Path(os.environ.get("DAYZ_MCP_MODPACK", ""))


@pytest.mark.skipif(
    not (VANILLA.name and VANILLA.is_dir()),
    reason="set DAYZ_MCP_VANILLA_SCRIPTS to an unpacked scripts.pbo to run",
)
def test_the_core_layer_on_the_real_corpus(tmp_path):
    with KnowledgeStore(tmp_path / "knowledge.db") as store:
        started = time.perf_counter()
        report = build_core(store, scripts=VANILLA, configs=False)
        elapsed = time.perf_counter() - started
        assert report.sources > 2000
        assert report.declarations > 40000

        started = time.perf_counter()
        again = build_core(store, scripts=VANILLA, configs=False)
        measure = time.perf_counter() - started
        assert again.indexed == 0
        print(
            f"\ncore: {report.sources} sources, {report.declarations} declarations, "
            f"build {elapsed:.1f}s, staleness {measure * 1000:.0f}ms, "
            f"index {(tmp_path / 'knowledge.db').stat().st_size / 1e6:.1f} MB"
        )
        # The names this project looked up by hand before the index existed.
        for name in ("SetupAction", "OnActionEnd", "IsSprinting", "ChatMP"):
            assert store.find(name, layer=CORE), name


@pytest.mark.skipif(
    not (MODPACK.name and MODPACK.is_dir()),
    reason="set DAYZ_MCP_MODPACK to a folder of installed mods to run",
)
def test_the_deps_layer_on_a_real_modpack(tmp_path):
    """The first time this code meets somebody else's archives: obfuscated
    names, decoy entries, binarised configs, and whatever else three dozen
    authors thought of."""
    mods = sorted(p for p in MODPACK.iterdir() if p.is_dir())
    with KnowledgeStore(tmp_path / "knowledge.db") as store:
        started = time.perf_counter()
        report = build_deps(store, mods)
        elapsed = time.perf_counter() - started
        assert report.declarations > 1000
        print(
            f"\ndeps: {len(mods)} mods, {report.sources} archives, "
            f"{report.declarations} declarations, build {elapsed:.1f}s, "
            f"skipped {len(report.skipped)}, lost {report.lost}, "
            f"index {(tmp_path / 'knowledge.db').stat().st_size / 1e6:.1f} MB"
        )
        started = time.perf_counter()
        again = build_deps(store, mods)
        assert again.indexed == 0
        print(f"deps staleness: {(time.perf_counter() - started) * 1000:.0f} ms")
