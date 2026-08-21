"""The index behind every knowledge answer.

Two failures are worth more than all the others put together, and both are
silent:

**A declaration overwritten by another one.** Vanilla declares `Man`,
`Transport` and `CargoList` twice each -- once per `#ifdef` branch, with
different parents. Keyed on the name alone, one of each pair disappears and
the index answers confidently with half the truth. Measured on the real
corpus: 43 579 declarations carry only 26 434 distinct names, so a name key
would drop 39% of them without a word.

**A layer rebuilt on top of itself.** An index that doubles its rows every
build still answers -- it just answers twice, and the duplicate is
indistinguishable from a genuine second declaration.

Staleness is measured here, never guessed, and measured **per source**: the
whole point of three layers is that editing one project file is a different
event from the game being updated, and one timestamp on a layer cannot tell
those apart.
"""
import os
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from dayz_mcp.knowledge.parse import (
    CLASS,
    CONSTANT,
    ENUM,
    METHOD,
    MODDED,
    PROTO_NATIVE,
    Declaration,
    parse_file,
)
from dayz_mcp.knowledge.store import (
    CORE,
    DEPS,
    PROJECT,
    SCHEMA_VERSION,
    DuplicateDeclaration,
    KnowledgeStore,
)


# ------------------------------------------------------------------ helpers


@pytest.fixture
def store(tmp_path):
    with KnowledgeStore(tmp_path / "knowledge.db") as s:
        yield s


def write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def put_text(store, layer, root, rel, text):
    """Write a real source file and index it, the way a layer will."""
    path = write(root, rel, text)
    store.put_source(layer, path, parse_file(path, file=rel))
    return path


def names(records):
    return [r.name for r in records]


def synthetic(name, kind=CLASS, **kw):
    return Declaration(name=name, kind=kind, **kw)


# --------------------------------------------------------------- where it lives


def test_the_index_lives_beside_the_job_journals(tmp_path):
    """`.dayz-mcp/` is this project's working directory; the job journals are
    already there and the .gitignore already covers it."""
    with KnowledgeStore.for_project(tmp_path) as s:
        assert s.path == tmp_path / ".dayz-mcp" / "knowledge.db"
        assert s.path.exists()


def test_an_index_survives_reopening(tmp_path):
    path = tmp_path / "knowledge.db"
    with KnowledgeStore(path) as s:
        put_text(s, CORE, tmp_path, "a.c", "class Alpha {}")
    with KnowledgeStore(path) as s:
        assert names(s.find("Alpha")) == ["Alpha"]


# ------------------------------------------------------------- round trip


def test_a_stored_declaration_comes_back_exactly_as_parsed(store, tmp_path):
    """Every field the parser produces has to survive storage, flags and
    guards included -- `proto native` contains a space, so a flags column
    joined on spaces would quietly split it in two."""
    decl = Declaration(
        name="GetInputInterface",
        kind=METHOD,
        owner="Man",
        signature="proto native UAInterface GetInputInterface()",
        file="3_game/entities/man.c",
        line=19,
        flags=(PROTO_NATIVE,),
        parent="",
        guard=("FEATURE_NETWORK_RECONCILIATION",),
    )
    store.put_source(CORE, tmp_path / "man.c", [decl], size=1, mtime=1.0)
    found = store.find("GetInputInterface")
    assert len(found) == 1
    assert found[0].declaration == decl
    assert found[0].layer == CORE


def test_a_record_serialises_to_plain_data(store, tmp_path):
    """Tools hand these straight to the agent inside the result envelope."""
    store.put_source(
        CORE,
        tmp_path / "a.c",
        [synthetic("Alpha", parent="Base", flags=(MODDED,), guard=("SERVER",))],
        size=1,
        mtime=1.0,
    )
    data = store.find("Alpha")[0].to_dict()
    assert data["name"] == "Alpha"
    assert data["parent"] == "Base"
    assert data["flags"] == [MODDED]
    assert data["guard"] == ["SERVER"]
    assert data["layer"] == CORE


# ------------------------------------------------- decision 1: the record key


def test_both_ifdef_branches_of_one_class_survive(store, tmp_path):
    """The shape is copied from `3_game/entities/man.c`: one class, one header
    per branch, one shared body. They differ only in parent and guard, so a
    name-keyed store silently keeps one of them -- and then answers "Man
    extends EntityAI" on a build where it extends Person."""
    put_text(
        store,
        CORE,
        tmp_path,
        "man.c",
        "#ifdef FEATURE_X\n"
        "class Thing extends Alpha\n"
        "#else\n"
        "class Thing extends Beta\n"
        "#endif\n"
        "{\n"
        "}\n",
    )
    found = store.find("Thing")
    assert len(found) == 2, found
    assert {r.parent for r in found} == {"Alpha", "Beta"}
    assert {r.guard for r in found} == {("FEATURE_X",), ("!FEATURE_X",)}


def test_a_class_and_its_constructor_on_one_line_both_survive(store, tmp_path):
    """Same name, same file, same LINE -- only kind and owner tell them apart.
    This is why the key is not `(name, file, line)`: a one-line class with a
    constructor collapses under that key, and one-line classes are ordinary in
    mod sources."""
    put_text(store, CORE, tmp_path, "t.c", "class Thing { void Thing() {} }\n")
    found = store.find("Thing")
    assert {(r.kind, r.owner) for r in found} == {(CLASS, ""), (METHOD, "Thing")}


def test_the_same_method_name_on_many_classes_is_many_records(store, tmp_path):
    """`Init` is declared 568 times in vanilla. Every one of them is a real
    answer to "where is Init declared"."""
    put_text(
        store,
        CORE,
        tmp_path,
        "many.c",
        "class Alpha { void Init(); }\n"
        "class Beta { void Init(); }\n"
        "class Gamma { void Init(); }\n",
    )
    assert sorted(r.owner for r in store.find("Init", kind=METHOD)) == [
        "Alpha",
        "Beta",
        "Gamma",
    ]


def test_indexing_the_same_source_twice_does_not_duplicate_it(store, tmp_path):
    """The identity of a source is its path within its layer. Re-reading an
    unchanged file must be a no-op, not a second copy."""
    path = write(tmp_path, "a.c", "class Alpha { void Init(); }\n")
    for _ in range(3):
        store.put_source(CORE, path, parse_file(path, file="a.c"))
    assert len(store.find("Alpha")) == 1
    assert store.count(CORE) == 2
    assert len(store.sources(CORE)) == 1


def test_two_declarations_claiming_one_key_is_a_loud_error(store, tmp_path):
    """The key is a UNIQUE index, not a convention. If a future parser ever
    did emit two records that are the same declaration, the store says so and
    names it -- rather than keeping whichever arrived last, which is the exact
    silence this key exists to prevent. Measured: zero collisions across the
    43 579 declarations of the real corpus."""
    twin = synthetic("Twin", file="a.c", line=1)
    with pytest.raises(DuplicateDeclaration) as caught:
        store.put_source(CORE, tmp_path / "a.c", [twin, twin], size=1, mtime=1.0)
    assert "Twin" in str(caught.value)
    assert "a.c" in str(caught.value)
    # And the refused write left nothing behind.
    assert store.find("Twin") == []
    assert store.sources(CORE) == []


def test_a_clash_with_an_already_indexed_declaration_is_reported_too(store, tmp_path):
    """The other way the key can be claimed twice: two different files on disk
    recorded under one `file` label. The offending source is named, and the
    declaration already in the index is left exactly as it was."""
    shared = synthetic("Twin", file="shared.c", line=1)
    store.put_source(CORE, tmp_path / "a.c", [shared], size=1, mtime=1.0)
    with pytest.raises(DuplicateDeclaration) as caught:
        store.put_source(CORE, tmp_path / "b.c", [shared], size=1, mtime=1.0)
    assert "b.c" in str(caught.value)
    assert len(store.find("Twin")) == 1
    assert [s.path for s in store.sources(CORE)] == [str(tmp_path / "a.c")]


# ------------------------------------------- decision 2: replacement, not growth


def test_rebuilding_a_layer_does_not_double_its_rows(store, tmp_path):
    a = write(tmp_path, "a.c", "class Alpha { void Init(); }\n")
    b = write(tmp_path, "b.c", "class Beta {}\n")

    def sources():
        for p in (a, b):
            yield p, parse_file(p, file=p.name)

    first = store.replace_layer(CORE, sources())
    second = store.replace_layer(CORE, sources())
    assert first == second == 3
    assert store.count(CORE) == 3
    assert len(store.sources(CORE)) == 2
    assert len(store.find("Alpha")) == 1


def test_rebuilding_a_layer_forgets_what_is_no_longer_there(store, tmp_path):
    """A class deleted from the sources must stop being an answer. An index
    that only ever adds is worse than no index: it is confidently wrong."""
    a = write(tmp_path, "a.c", "class Gone {}\nclass Stays {}\n")
    store.replace_layer(CORE, [(a, parse_file(a, file="a.c"))])
    a.write_text("class Stays {}\n", encoding="utf-8")
    store.replace_layer(CORE, [(a, parse_file(a, file="a.c"))])
    assert store.find("Gone") == []
    assert names(store.find("Stays")) == ["Stays"]


def test_rebuilding_a_layer_forgets_a_source_that_is_no_longer_in_it(store, tmp_path):
    """The case the test above cannot see. Re-reading the SAME file replaces
    its rows by itself, so a rebuild that never clears the layer still looks
    correct as long as the file set is unchanged -- and stays wrong the moment
    a file is deleted, renamed, or dropped from the mod. Verified by mutation:
    remove the layer clear and only this test fails."""
    a = write(tmp_path, "a.c", "class Alpha {}\n")
    b = write(tmp_path, "b.c", "class Beta {}\n")
    store.replace_layer(
        CORE, [(p, parse_file(p, file=p.name)) for p in (a, b)]
    )
    b.unlink()
    store.replace_layer(CORE, [(a, parse_file(a, file="a.c"))])
    assert store.find("Beta") == []
    assert len(store.sources(CORE)) == 1
    assert store.count(CORE) == 1


def test_rebuilding_one_layer_leaves_the_others_alone(store, tmp_path):
    store.put_source(CORE, tmp_path / "c.c", [synthetic("Vanilla")], size=1, mtime=1.0)
    store.put_source(DEPS, tmp_path / "d.c", [synthetic("Dependency")], size=1, mtime=1.0)
    store.replace_layer(
        PROJECT, [(tmp_path / "p.c", [synthetic("Mine")])], root=str(tmp_path)
    )
    assert names(store.find("Vanilla")) == ["Vanilla"]
    assert names(store.find("Dependency")) == ["Dependency"]
    assert names(store.find("Mine")) == ["Mine"]


def test_reindexing_one_source_touches_only_that_source(store, tmp_path):
    """Task 3's whole incrementality rests on this: one edited file, one file
    reindexed, everything else in the layer untouched."""
    put_text(store, PROJECT, tmp_path, "a.c", "class Alpha {}\n")
    put_text(store, PROJECT, tmp_path, "b.c", "class Beta {}\n")
    put_text(store, PROJECT, tmp_path, "a.c", "class AlphaRenamed {}\n")
    assert store.find("Alpha") == []
    assert names(store.find("AlphaRenamed")) == ["AlphaRenamed"]
    assert names(store.find("Beta")) == ["Beta"]
    assert len(store.sources(PROJECT)) == 2


def test_dropping_a_source_drops_its_declarations(store, tmp_path):
    put_text(store, PROJECT, tmp_path, "a.c", "class Alpha {}\n")
    put_text(store, PROJECT, tmp_path, "b.c", "class Beta {}\n")
    assert store.drop_source(PROJECT, tmp_path / "a.c") == 1
    assert store.find("Alpha") == []
    assert names(store.find("Beta")) == ["Beta"]


def test_dropping_a_layer_drops_everything_in_it(store, tmp_path):
    store.put_source(CORE, tmp_path / "c.c", [synthetic("Vanilla")], size=1, mtime=1.0)
    store.put_source(PROJECT, tmp_path / "p.c", [synthetic("Mine")], size=1, mtime=1.0)
    store.drop_layer(PROJECT)
    assert store.find("Mine") == []
    assert names(store.find("Vanilla")) == ["Vanilla"]
    assert store.layer(PROJECT) is None


def test_the_same_name_in_two_layers_is_two_answers(store, tmp_path):
    """A mod that declares `modded class PlayerBase` does not replace vanilla's
    declaration in the index; both are true, and which one matters depends on
    the question."""
    store.put_source(
        CORE, tmp_path / "c.c", [synthetic("PlayerBase", parent="ManBase")], size=1, mtime=1.0
    )
    store.put_source(
        PROJECT,
        tmp_path / "p.c",
        [synthetic("PlayerBase", flags=(MODDED,), parent="PlayerBase")],
        size=1,
        mtime=1.0,
    )
    found = store.find("PlayerBase")
    assert [r.layer for r in found] == [PROJECT, CORE]


def test_a_failed_rebuild_leaves_the_previous_generation_intact(store, tmp_path):
    """A rebuild that dies halfway -- an unreadable file, a killed job -- must
    leave the last good index, not a half-written one that looks complete."""
    a = write(tmp_path, "a.c", "class Alpha {}\n")
    store.replace_layer(CORE, [(a, parse_file(a, file="a.c"))])

    def half():
        yield tmp_path / "b.c", [synthetic("Beta")]
        raise RuntimeError("source vanished mid-rebuild")

    with pytest.raises(RuntimeError):
        store.replace_layer(CORE, half())
    assert names(store.find("Alpha")) == ["Alpha"]
    assert store.find("Beta") == []


# ------------------------------------------------------------------ searching


def test_find_by_exact_name(store, tmp_path):
    put_text(
        store,
        CORE,
        tmp_path,
        "a.c",
        "class SetupAction {}\nclass SetupActionOther {}\n",
    )
    assert names(store.find("SetupAction")) == ["SetupAction"]


def test_find_is_case_insensitive(store, tmp_path):
    """The agent types what it remembers. The verbatim spelling comes back in
    the answer, so nothing is lost by being forgiving on the way in."""
    put_text(store, CORE, tmp_path, "a.c", "class SetupAction {}\n")
    assert names(store.find("setupaction")) == ["SetupAction"]
    assert names(store.find("SETUPACTION")) == ["SetupAction"]


def test_find_by_prefix(store, tmp_path):
    put_text(
        store,
        CORE,
        tmp_path,
        "a.c",
        "class ActionOpenDoors {}\nclass ActionEatSmall {}\nclass Building {}\n",
    )
    assert sorted(names(store.find("Action", prefix=True))) == [
        "ActionEatSmall",
        "ActionOpenDoors",
    ]


def test_prefix_search_is_a_prefix_not_a_substring(store, tmp_path):
    """A substring search is a different, much slower question, and one an
    index cannot answer from the front of a key."""
    put_text(store, CORE, tmp_path, "a.c", "class DoActionThing {}\nclass ActionThing {}\n")
    assert names(store.find("Action", prefix=True)) == ["ActionThing"]


def test_prefix_search_does_not_leak_past_the_prefix(store, tmp_path):
    """A range scan built on `prefix` .. `prefix + high character` has to stop
    at names that merely sort nearby."""
    put_text(store, CORE, tmp_path, "a.c", "class Act {}\nclass Acu {}\nclass Acts {}\n")
    assert sorted(names(store.find("Act", prefix=True))) == ["Act", "Acts"]


def test_find_filters_by_kind(store, tmp_path):
    put_text(store, CORE, tmp_path, "a.c", "class Thing { void Thing(); }\n")
    assert names(store.find("Thing", kind=CLASS)) == ["Thing"]
    assert names(store.find("Thing", kind=METHOD)) == ["Thing"]
    assert len(store.find("Thing")) == 2


def test_find_filters_by_owner(store, tmp_path):
    put_text(
        store,
        CORE,
        tmp_path,
        "a.c",
        "class Alpha { void Init(); }\nclass Beta { void Init(); }\n",
    )
    assert [r.owner for r in store.find("Init", owner="Beta")] == ["Beta"]


def test_find_filters_by_layer(store, tmp_path):
    store.put_source(CORE, tmp_path / "c.c", [synthetic("Same")], size=1, mtime=1.0)
    store.put_source(PROJECT, tmp_path / "p.c", [synthetic("Same")], size=1, mtime=1.0)
    assert [r.layer for r in store.find("Same", layer=PROJECT)] == [PROJECT]


def test_find_lists_a_whole_kind_when_given_no_name(store, tmp_path):
    """"What enums are there" is a real question and the kind index answers
    it -- bounded, like every other search here."""
    put_text(
        store,
        CORE,
        tmp_path,
        "a.c",
        "enum EOne { A }\nenum ETwo { B }\nclass NotAnEnum {}\n",
    )
    assert sorted(names(store.find("", kind=ENUM))) == ["EOne", "ETwo"]


def test_the_nearest_layer_answers_first(store, tmp_path):
    """The project's own code, then its dependencies, then vanilla. An answer
    list that buried the project's declaration under six thousand vanilla ones
    would be useless however fast it arrived."""
    store.put_source(CORE, tmp_path / "c.c", [synthetic("Ranked")], size=1, mtime=1.0)
    store.put_source(PROJECT, tmp_path / "p.c", [synthetic("Ranked")], size=1, mtime=1.0)
    store.put_source(DEPS, tmp_path / "d.c", [synthetic("Ranked")], size=1, mtime=1.0)
    assert [r.layer for r in store.find("Ranked")] == [PROJECT, DEPS, CORE]


def test_a_prefix_browse_comes_out_alphabetical(store, tmp_path):
    """One ordering rule serves both questions: by name, then by nearest
    layer. An exact lookup has one name, so it degenerates to "the project
    first"; a prefix browse comes out alphabetical, which is also the order
    that makes a truncated browse mean something."""
    put_text(
        store,
        CORE,
        tmp_path,
        "a.c",
        "class ActionGamma {}\nclass ActionAlpha {}\nclass ActionBeta {}\n",
    )
    assert names(store.find("Action", prefix=True)) == [
        "ActionAlpha",
        "ActionBeta",
        "ActionGamma",
    ]


def test_a_prefix_search_reads_the_index_in_order_instead_of_sorting(store, tmp_path):
    """What makes prefix search instant rather than merely correct: the index
    already holds this order, so SQLite stops at the limit instead of
    collecting every match and sorting it. Measured on the real corpus, the
    difference on prefix "On" was 8.4 ms against 0.44 ms -- and a sort would
    still have passed every other test here."""
    put_text(store, CORE, tmp_path, "a.c", "class ActionAlpha {}\nclass ActionBeta {}\n")
    plan = store.explain_find("Action", prefix=True).upper()
    assert "TEMP B-TREE" not in plan, plan
    assert "SCAN" not in plan, plan


def test_search_is_bounded(store, tmp_path):
    """Every search in this server has a ceiling -- the predecessor's two
    search tools hang forever, and an unbounded answer is its own kind of
    hang."""
    body = "".join(f"class Bounded{i} {{}}\n" for i in range(50))
    put_text(store, CORE, tmp_path, "a.c", body)
    assert len(store.find("Bounded", prefix=True, limit=10)) == 10


def test_an_unknown_name_is_an_empty_answer_not_an_error(store, tmp_path):
    put_text(store, CORE, tmp_path, "a.c", "class Alpha {}\n")
    assert store.find("NoSuchClassAnywhere") == []


def test_name_search_uses_the_name_index(store, tmp_path):
    """The entire point of this phase is that the answer is instant. A query
    that stopped using its index would still pass every test above -- just
    slower and slower as the index grows, which is exactly the failure nobody
    notices until the corpus is real."""
    put_text(store, CORE, tmp_path, "a.c", "class Alpha {}\n")
    plan = store.explain_find("Alpha")
    assert "SCAN" not in plan.upper(), plan
    assert "idx_decl_name" in plan, plan


def test_owner_search_uses_the_owner_index(store, tmp_path):
    """"What does this class declare" has to be indexed too -- with no name to
    narrow on, the only thing standing between it and a full table scan is the
    owner index."""
    put_text(store, CORE, tmp_path, "a.c", "class Alpha { void Init(); }\n")
    plan = store.explain_find("", owner="Alpha")
    assert "SCAN" not in plan.upper(), plan
    assert "idx_decl_owner" in plan, plan


# ----------------------------------------------------------------- overrides


def test_overrides_finds_who_overrides_a_method(store, tmp_path):
    """The question a text sweep answers worst: `override void OnActionEnd()`
    is spelled differently everywhere it matters, and grep for the name alone
    returns the base declaration mixed in with every call site."""
    put_text(
        store,
        CORE,
        tmp_path,
        "base.c",
        "class ActionBase { void OnActionEnd(); }\n",
    )
    put_text(
        store,
        PROJECT,
        tmp_path,
        "mine.c",
        "class ActionMine extends ActionBase { override void OnActionEnd(); }\n",
    )
    found = store.overrides("OnActionEnd")
    assert [(r.owner, r.layer) for r in found] == [("ActionMine", PROJECT)]


def test_overrides_does_not_return_the_base_declaration(store, tmp_path):
    put_text(store, CORE, tmp_path, "base.c", "class ActionBase { void OnActionEnd(); }\n")
    assert store.overrides("OnActionEnd") == []


def test_a_method_declared_inside_a_modded_class_counts_as_an_override(store, tmp_path):
    """`modded class` bodies routinely re-declare a method without writing
    `override`. It still replaces the vanilla one, so it still answers "who
    overrides this"."""
    put_text(
        store,
        PROJECT,
        tmp_path,
        "mine.c",
        "modded class PlayerBase { void OnConnect(); }\n",
    )
    assert [r.owner for r in store.overrides("OnConnect")] == ["PlayerBase"]


def test_overrides_narrows_to_one_owner(store, tmp_path):
    put_text(
        store,
        PROJECT,
        tmp_path,
        "mine.c",
        "class A extends Base { override void Go(); }\n"
        "class B extends Base { override void Go(); }\n",
    )
    assert [r.owner for r in store.overrides("Go", owner="B")] == ["B"]


def test_overrides_of_a_class_are_its_subclasses_and_its_modded_copies(store, tmp_path):
    """For a class the same question means: who extends it, and who reopens it
    with `modded`."""
    put_text(store, CORE, tmp_path, "base.c", "class ItemBase {}\n")
    put_text(
        store,
        PROJECT,
        tmp_path,
        "mine.c",
        "class MyItem extends ItemBase {}\nmodded class ItemBase {}\n",
    )
    found = store.overrides("ItemBase")
    assert sorted(names(found)) == ["ItemBase", "MyItem"]
    assert all(r.layer == PROJECT for r in found)


def test_overrides_is_bounded_too(store, tmp_path):
    body = "".join(f"class Sub{i} extends Base {{}}\n" for i in range(40))
    put_text(store, PROJECT, tmp_path, "many.c", body)
    assert len(store.overrides("Base", limit=5)) == 5


def test_subclass_search_uses_the_parent_index(store, tmp_path):
    put_text(store, CORE, tmp_path, "a.c", "class Sub extends Base {}\n")
    plan = store.explain_overrides("Base")
    assert "SCAN" not in plan.upper(), plan


# ----------------------------------------------------------------- staleness


def test_a_freshly_indexed_layer_is_not_stale(store, tmp_path):
    put_text(store, PROJECT, tmp_path, "a.c", "class Alpha {}\n")
    st = store.staleness(PROJECT)
    assert not st.stale
    assert st.changed == () and st.missing == () and st.added == ()
    assert st.unchanged == 1


def test_an_edited_source_is_measured_as_changed(store, tmp_path):
    a = write(tmp_path, "a.c", "class Alpha {}\n")
    store.put_source(PROJECT, a, parse_file(a, file="a.c"))
    time.sleep(0.01)
    a.write_text("class Alpha {}\nclass Added {}\n", encoding="utf-8")
    st = store.staleness(PROJECT)
    assert st.stale
    assert st.changed == (str(a),)


def test_a_source_edited_without_changing_its_timestamp_is_still_caught(store, tmp_path):
    """Restored backups, archive extraction and copy tools all preserve mtime.
    Size is the second half of the measurement precisely for this."""
    a = write(tmp_path, "a.c", "class Alpha {}\n")
    store.put_source(PROJECT, a, parse_file(a, file="a.c"))
    before = a.stat()
    a.write_text("class Alpha {}\nclass Added {}\n", encoding="utf-8")
    os.utime(a, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert a.stat().st_mtime_ns == before.st_mtime_ns
    st = store.staleness(PROJECT)
    assert st.stale
    assert st.changed == (str(a),)


def test_only_the_edited_source_is_stale(store, tmp_path):
    """Per source, not per layer. One timestamp on the layer would mark every
    file in it for reindexing, and Task 3's incrementality would be a
    fiction."""
    a = write(tmp_path, "a.c", "class Alpha {}\n")
    b = write(tmp_path, "b.c", "class Beta {}\n")
    store.put_source(PROJECT, a, parse_file(a, file="a.c"))
    store.put_source(PROJECT, b, parse_file(b, file="b.c"))
    time.sleep(0.01)
    b.write_text("class Beta {}\nclass More {}\n", encoding="utf-8")
    st = store.staleness(PROJECT)
    assert st.changed == (str(b),)
    assert st.unchanged == 1


def test_editing_a_project_file_does_not_age_the_vanilla_layer(store, tmp_path):
    """The reason staleness is per source and per layer at all: the game being
    updated and a mod file being saved are different events, and an index that
    could not tell them apart would rebuild 2810 vanilla files on every save."""
    core = write(tmp_path, "core.c", "class Vanilla {}\n")
    mine = write(tmp_path, "mine.c", "class Mine {}\n")
    store.put_source(CORE, core, parse_file(core, file="core.c"))
    store.put_source(PROJECT, mine, parse_file(mine, file="mine.c"))
    time.sleep(0.01)
    mine.write_text("class Mine {}\nclass More {}\n", encoding="utf-8")
    assert store.staleness(PROJECT).stale
    assert not store.staleness(CORE).stale


def test_reindexing_the_edited_source_clears_its_staleness(store, tmp_path):
    a = write(tmp_path, "a.c", "class Alpha {}\n")
    store.put_source(PROJECT, a, parse_file(a, file="a.c"))
    time.sleep(0.01)
    a.write_text("class Alpha {}\nclass Added {}\n", encoding="utf-8")
    assert store.staleness(PROJECT).stale
    store.put_source(PROJECT, a, parse_file(a, file="a.c"))
    assert not store.staleness(PROJECT).stale


def test_a_deleted_source_is_reported_missing(store, tmp_path):
    a = write(tmp_path, "a.c", "class Alpha {}\n")
    store.put_source(PROJECT, a, parse_file(a, file="a.c"))
    a.unlink()
    st = store.staleness(PROJECT)
    assert st.stale
    assert st.missing == (str(a),)


def test_a_new_file_is_reported_only_when_the_caller_says_what_exists_now(store, tmp_path):
    """The store never walks the filesystem itself -- it does not know what a
    layer's sources are supposed to be, and guessing would be the same lie as
    a timestamp. When it is not told, it says so instead of implying "nothing
    new"."""
    a = write(tmp_path, "a.c", "class Alpha {}\n")
    b = write(tmp_path, "b.c", "class Beta {}\n")
    store.put_source(PROJECT, a, parse_file(a, file="a.c"))

    blind = store.staleness(PROJECT)
    assert blind.added == ()
    assert blind.scanned_for_new is False
    assert not blind.stale

    seeing = store.staleness(PROJECT, current=[a, b])
    assert seeing.added == (str(b),)
    assert seeing.scanned_for_new is True
    assert seeing.stale


def test_a_source_absent_from_the_current_set_is_missing(store, tmp_path):
    a = write(tmp_path, "a.c", "class Alpha {}\n")
    b = write(tmp_path, "b.c", "class Beta {}\n")
    store.put_source(PROJECT, a, parse_file(a, file="a.c"))
    store.put_source(PROJECT, b, parse_file(b, file="b.c"))
    st = store.staleness(PROJECT, current=[a])
    assert st.missing == (str(b),)


def test_staleness_names_what_to_reindex(store, tmp_path):
    a = write(tmp_path, "a.c", "class Alpha {}\n")
    b = write(tmp_path, "b.c", "class Beta {}\n")
    store.put_source(PROJECT, a, parse_file(a, file="a.c"))
    time.sleep(0.01)
    a.write_text("class Alpha {}\nclass Added {}\n", encoding="utf-8")
    st = store.staleness(PROJECT, current=[a, b])
    assert st.outdated == (str(a), str(b))


def test_staleness_of_a_layer_never_built_says_so(store):
    st = store.staleness(CORE)
    assert st.stale
    assert st.never_built
    assert "never" in st.describe().lower()


def test_staleness_describes_itself_in_a_sentence(store, tmp_path):
    a = write(tmp_path, "a.c", "class Alpha {}\n")
    store.put_source(PROJECT, a, parse_file(a, file="a.c"))
    assert "up to date" in store.staleness(PROJECT).describe().lower()
    time.sleep(0.01)
    a.write_text("class Alpha {}\nclass Added {}\n", encoding="utf-8")
    sentence = store.staleness(PROJECT).describe()
    assert "1" in sentence and "changed" in sentence.lower()


def test_a_layer_reports_what_it_is_made_of(store, tmp_path):
    put_text(store, CORE, tmp_path, "a.c", "class Alpha { void Init(); }\n")
    put_text(store, CORE, tmp_path, "b.c", "class Beta {}\n")
    info = store.layer(CORE)
    assert info.name == CORE
    assert info.sources == 2
    assert info.declarations == 3
    assert info.built > 0
    assert info.updated >= info.built
    assert info.age(now=info.updated + 60) == pytest.approx(60, abs=1)


def test_layers_are_listed_nearest_first(store, tmp_path):
    store.put_source(CORE, tmp_path / "c.c", [synthetic("A")], size=1, mtime=1.0)
    store.put_source(PROJECT, tmp_path / "p.c", [synthetic("B")], size=1, mtime=1.0)
    assert [layer.name for layer in store.layers()] == [PROJECT, CORE]


def test_a_layer_remembers_its_root(store, tmp_path):
    """`knowledge_status` has to say what a layer was built FROM, not just
    when: a core layer built from last month's unpacked copy is a different
    fact from one built from the installed game."""
    store.replace_layer(
        CORE, [(tmp_path / "a.c", [synthetic("Alpha")])], root=str(tmp_path)
    )
    assert store.layer(CORE).root == str(tmp_path)


def test_sources_report_what_they_were_when_indexed(store, tmp_path):
    a = write(tmp_path, "a.c", "class Alpha {}\n")
    store.put_source(PROJECT, a, parse_file(a, file="a.c"))
    (src,) = store.sources(PROJECT)
    assert src.path == str(a)
    assert src.size == a.stat().st_size
    assert src.mtime == pytest.approx(a.stat().st_mtime)
    assert src.declarations == 1


# -------------------------------------------------------- durability, recovery


def test_the_store_works_from_a_worker_thread(store, tmp_path):
    """Every long operation in this server runs on a worker thread. A sqlite
    connection bound to its creating thread would raise there and nowhere
    else -- in a job, where the traceback is hardest to see."""
    failures = []

    def work():
        try:
            store.put_source(
                CORE, tmp_path / "t.c", [synthetic("Threaded")], size=1, mtime=1.0
            )
        except Exception as exc:  # noqa: BLE001 - the point is to report any
            failures.append(exc)

    thread = threading.Thread(target=work)
    thread.start()
    thread.join()
    assert not failures, failures
    assert names(store.find("Threaded")) == ["Threaded"]


def test_a_corrupt_index_file_is_rebuilt_rather_than_raising(tmp_path):
    """An index is derived data. Losing it costs a rebuild; refusing to open
    costs the agent every knowledge tool at once."""
    path = tmp_path / "knowledge.db"
    path.write_bytes(b"not a database, not even close" * 200)
    with KnowledgeStore(path) as s:
        s.put_source(CORE, tmp_path / "a.c", [synthetic("Alpha")], size=1, mtime=1.0)
        assert names(s.find("Alpha")) == ["Alpha"]


def test_an_index_from_another_schema_version_is_rebuilt_not_misread(tmp_path):
    """The schema will change. Reading old rows through a new schema is how an
    index starts answering plausible nonsense."""
    path = tmp_path / "knowledge.db"
    with KnowledgeStore(path) as s:
        s.put_source(CORE, tmp_path / "a.c", [synthetic("Old")], size=1, mtime=1.0)
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    conn.commit()
    conn.close()
    with KnowledgeStore(path) as s:
        assert s.layers() == []
        assert s.find("Old") == []
        s.put_source(CORE, tmp_path / "a.c", [synthetic("New")], size=1, mtime=1.0)
        assert names(s.find("New")) == ["New"]


def test_the_schema_version_is_stamped_on_the_file(tmp_path):
    path = tmp_path / "knowledge.db"
    with KnowledgeStore(path):
        pass
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    conn.close()


def test_a_missing_working_directory_is_created(tmp_path):
    with KnowledgeStore(tmp_path / "deep" / "deeper" / "knowledge.db") as s:
        assert s.path.parent.is_dir()


# ------------------------------------------------- the real corpus, if present

# Named by the environment rather than hard-coded, exactly as the parser's own
# corpus test is: this repository must stay portable, and a store that is fast
# on ten rows proves nothing about one that holds forty thousand.
VANILLA = Path(os.environ.get("DAYZ_MCP_VANILLA_SCRIPTS", ""))


@pytest.mark.skipif(
    not (VANILLA.name and VANILLA.is_dir()),
    reason="set DAYZ_MCP_VANILLA_SCRIPTS to an unpacked scripts.pbo to run",
)
def test_the_real_corpus_stores_and_answers(tmp_path):
    """The shape that matters: 2810 files, tens of thousands of declarations,
    and the names this project actually looked up by hand in earlier sessions."""
    files = sorted(VANILLA.rglob("*.c"))
    assert len(files) > 2000

    def sources():
        for path in files:
            yield path, parse_file(path, file=str(path.relative_to(VANILLA)))

    with KnowledgeStore(tmp_path / "knowledge.db") as store:
        started = time.perf_counter()
        stored = store.replace_layer(CORE, sources(), root=str(VANILLA))
        elapsed = time.perf_counter() - started

        assert stored > 40000
        assert store.count(CORE) == stored
        assert len(store.sources(CORE)) == len(files)

        # Names looked up by hand while designing phases 2 and 3 -- the whole
        # reason this phase exists.
        for name in ("SetupAction", "OnActionEnd", "IsSprinting", "ChatMP"):
            assert store.find(name), name
        assert store.find("ECE_NOLIFETIME", kind=CONSTANT)

        # The `#ifdef` pair that decided the record key, from the real file.
        man = store.find("Man", kind=CLASS)
        assert sorted(r.parent for r in man) == ["EntityAI", "Person"]

        started = time.perf_counter()
        for _ in range(100):
            store.find("OnActionEnd")
            store.find("Action", prefix=True, limit=50)
            store.overrides("EntityAI")
        query = (time.perf_counter() - started) / 300

        size = (tmp_path / "knowledge.db").stat().st_size
        print(
            f"\ncorpus: {len(files)} files, {stored} declarations, "
            f"build {elapsed:.1f}s, index {size / 1e6:.1f} MB, "
            f"query {query * 1000:.2f} ms"
        )
        # A ceiling loose enough to survive a slow machine and tight enough to
        # catch an index that stopped being used.
        assert query < 0.05

        # Rebuilding the whole layer replaces it -- at this size, a doubling
        # would be the difference between an index and a landfill.
        store.replace_layer(CORE, sources(), root=str(VANILLA))
        assert store.count(CORE) == stored


@pytest.mark.skipif(
    not (VANILLA.name and VANILLA.is_dir()),
    reason="set DAYZ_MCP_VANILLA_SCRIPTS to an unpacked scripts.pbo to run",
)
def test_the_real_corpus_has_no_key_collisions(tmp_path):
    """Measured, not assumed: if two distinct declarations ever shared the
    record key, one of them would be lost on write. Across the whole corpus
    they do not -- and 5336 names carry more than one declaration, which is
    what a name key would have thrown away."""
    seen = set()
    total = 0
    for path in sorted(VANILLA.rglob("*.c")):
        rel = str(path.relative_to(VANILLA))
        for d in parse_file(path, file=rel):
            total += 1
            seen.add((d.name, d.kind, d.owner, d.file, d.line))
    assert len(seen) == total
    assert len({k[0] for k in seen}) < total  # names alone would collapse them
