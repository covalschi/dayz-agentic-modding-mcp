"""The active mod set: which mods an answer is allowed to come from.

The index knows every mod found on this machine. Work, though, always happens
against ONE server, which runs its own subset -- and an answer about a class
from a mod that server does not run is a harmful answer: the agent writes code
that cannot load there.

Two rules are the whole feature, and every test here defends one of them:

1. **A filtered-out result is NAMED, never silently hidden.** An empty answer
   because the mod is outside the set is the same silent lie as an answer from
   a stale layer -- the agent reads "no such thing" and writes its own.
2. **Matching is by Workshop id, never by name.** Measured: one mod carries up
   to four different names (from the server, in the Workshop, on disk), and
   among 14 740 Workshop ids, 19 names belong to more than one mod. This
   machine has two mods whose names are also carried by different mods, with
   different ids, on foreign servers -- a name matcher would silently equate
   different builds.

Every mod name in this file is synthetic, and the ones that would trip the
repository's own mod-name guard are assembled at runtime, exactly as that
guard's own tests do.
"""
from __future__ import annotations

import json
import textwrap
from pathlib import Path

from dayz_mcp.a2s import ServerMod
from dayz_mcp.knowledge import scope
from dayz_mcp.knowledge.parse import CLASS, Declaration
from dayz_mcp.knowledge.store import CORE, DEPS, PROJECT, KnowledgeStore, mod_folder
from dayz_mcp.profile import BuildCfg, ExpectCfg, MachineCfg, ModsCfg, Profile

# Assembled at runtime: written as a literal these would be mod-shaped tokens
# in a scanned file, and the repository's guard would (correctly) fail on the
# very tests that prove the feature works.
UNDERSCORED = "@" + "Dep_two"
SPACED = "@" + "Dep three"


def decl(name: str, file: str, **kw) -> Declaration:
    return Declaration(name=name, kind=CLASS, file=file, **kw)


def store_with(tmp_path: Path) -> KnowledgeStore:
    return KnowledgeStore(tmp_path / "index.db")


def seed(store: KnowledgeStore, layer: str, path: str, declarations) -> None:
    store.put_source(layer, path, declarations, size=len(declarations) + 1, mtime=1000.0)


def a_profile(root: Path, required=(), extra=(), own=("MyMod",)) -> Profile:
    return Profile(
        name="p", root=root, build=BuildCfg(mods=list(own)),
        mods=ModsCfg(required=list(required), extra=[str(e) for e in extra]),
        expect=ExpectCfg(), machine=MachineCfg(),
        own_mod_dirs=[f"@{m}" for m in own],
    )


def a_mod_folder(root: Path, name: str, published: int | None) -> Path:
    folder = root / name
    (folder / "addons").mkdir(parents=True, exist_ok=True)
    if published is not None:
        (folder / "meta.cpp").write_text(
            textwrap.dedent(f"""
                protocol = 1;
                publishedid = {published};
                name = "a title from the workshop";
                timestamp = 1700000000;
            """).strip(),
            encoding="utf-8",
        )
    return folder


# ------------------------------------------------- which mod a record is from


def test_a_dependency_records_mod_folder_is_the_first_segment_of_its_label():
    """No schema change and no re-indexing: the index ALREADY labels every
    dependency declaration `<mod folder>/<archive>/<entry>`, so the mod a
    declaration came from is already written down."""
    assert mod_folder(DEPS, "@Dep/somepbo/scripts/4_World/thing.c") == "@Dep"
    assert mod_folder(DEPS, UNDERSCORED + "/somepbo/config.cpp") == UNDERSCORED
    assert mod_folder(DEPS, SPACED + "/somepbo/config.cpp") == SPACED


def test_the_game_and_the_project_belong_to_no_mod_folder():
    """Scoping narrows the DEPENDENCY layer and nothing else. The game is the
    substrate every DayZ mod is written against, and the project's own code is
    the thing being written -- narrowing either would answer "no such class"
    about code the agent is looking at."""
    assert mod_folder(CORE, "Addons/data/config.bin") == ""
    assert mod_folder(CORE, "3_Game/Entities/EntityAI.c") == ""
    assert mod_folder(PROJECT, "MyMod/scripts/4_World/thing.c") == ""


def test_a_label_with_no_separator_has_no_mod_folder():
    assert mod_folder(DEPS, "loose.c") == ""


# --------------------------------------------------- the set, as stored state


def test_an_unset_scope_reads_as_inactive_rather_than_as_an_empty_set(tmp_path):
    """"No scope" and "a scope naming nothing" are different facts: the first
    means every mod answers, the second would mean none does."""
    active = scope.load(tmp_path)
    assert active.active is False
    assert active.mods == ()


def test_a_scope_survives_being_written_and_read_back(tmp_path):
    saved = scope.save(tmp_path, ["@Dep", "@CF"], source="the server at a test address")
    assert saved.active is True
    again = scope.load(tmp_path)
    assert again.mods == ("@Dep", "@CF")
    assert again.source == "the server at a test address"
    assert again.set_at > 0


def test_clearing_a_scope_returns_the_index_to_answering_from_everything(tmp_path):
    scope.save(tmp_path, ["@Dep"])
    scope.clear(tmp_path)
    assert scope.load(tmp_path).active is False


def test_a_damaged_scope_file_reads_as_no_scope_rather_than_failing(tmp_path):
    """The scope is state, not data anybody would grieve. A file nobody can
    parse must not take every knowledge tool down with it -- but it must not
    silently keep narrowing either, so it reads as "no scope"."""
    (tmp_path / scope.SCOPE_FILE).write_text("{not json", encoding="utf-8")
    assert scope.load(tmp_path).active is False
    (tmp_path / scope.SCOPE_FILE).write_text(json.dumps({"mods": "a string"}), encoding="utf-8")
    assert scope.load(tmp_path).active is False


def test_a_scope_ignores_blanks_and_keeps_the_order_it_was_given(tmp_path):
    saved = scope.save(tmp_path, ["@Dep", "  ", "", "@CF", "@Dep"])
    assert saved.mods == ("@Dep", "@CF")


def test_membership_is_case_insensitive_because_folders_are(tmp_path):
    # Case variants assembled at runtime: as literals they are mod-shaped
    # tokens the repository guard has no reason to allow-list.
    saved = scope.save(tmp_path, ["@Dep"])
    assert saved.contains("@" + "dep") is True
    assert saved.contains("@" + "DEP") is True
    assert saved.contains("@CF") is False


# ------------------------------------------------------ local mod identity


def test_the_workshop_id_of_an_installed_mod_comes_from_its_meta_cpp(tmp_path):
    """Measured on this machine: all 35 modpack folders carry `publishedid` in
    meta.cpp, and all 35 agreed with the Steam content directory they link to
    -- zero disagreements. So local identity is exact, not a guess."""
    folder = a_mod_folder(tmp_path, "@Dep", 4242)
    assert scope.read_published_id(folder) == (4242, "a title from the workshop")


def test_a_mod_without_meta_cpp_has_no_id_and_says_so(tmp_path):
    """A BOUNDARY, stated rather than discovered: a project's OWN mods have no
    meta.cpp and therefore no Workshop id. They can only ever come from the
    profile, never from a server's answer."""
    folder = a_mod_folder(tmp_path, "@MyMod", None)
    assert scope.read_published_id(folder) == (0, "")


def test_a_meta_cpp_without_a_published_id_is_not_guessed_at(tmp_path):
    folder = tmp_path / "@Dep"
    folder.mkdir()
    (folder / "meta.cpp").write_text('name = "no id here";', encoding="utf-8")
    assert scope.read_published_id(folder) == (0, "no id here")


def test_installed_mods_covers_the_workshop_folder_and_the_declared_extras(tmp_path):
    game = tmp_path / "game"
    workshop = game / scope.WORKSHOP_DIRNAME
    a_mod_folder(workshop, "@Dep", 111)
    a_mod_folder(workshop, "@CF", 222)
    elsewhere = a_mod_folder(tmp_path / "other", "@B", 333)
    profile = a_profile(tmp_path / "proj", required=["@Dep"], extra=[elsewhere])

    found = {m.folder: m for m in scope.installed_mods(profile, str(game))}
    assert set(found) == {"@Dep", "@CF", "@B"}
    assert found["@Dep"].workshop_id == 111
    assert found["@B"].workshop_id == 333
    # `declared` is what the index actually holds a dependency layer for. A mod
    # merely present on disk is installed, and answering "not installed" about
    # it would be false.
    assert found["@Dep"].declared is True
    assert found["@B"].declared is True
    assert found["@CF"].declared is False


def test_the_projects_own_mods_are_not_reported_as_installed_dependencies(tmp_path):
    """They are the project layer. Counting them here would offer the agent a
    scope entry that narrows a layer they are not in."""
    game = tmp_path / "game"
    a_mod_folder(game / scope.WORKSHOP_DIRNAME, "@MyMod", None)
    a_mod_folder(game / scope.WORKSHOP_DIRNAME, "@Dep", 111)
    profile = a_profile(tmp_path / "proj", own=("MyMod",))
    assert [m.folder for m in scope.installed_mods(profile, str(game))] == ["@Dep"]


def test_installed_mods_without_a_game_still_reports_what_the_profile_names(tmp_path):
    elsewhere = a_mod_folder(tmp_path / "other", "@B", 333)
    profile = a_profile(tmp_path / "proj", extra=[elsewhere])
    assert [m.folder for m in scope.installed_mods(profile, None)] == ["@B"]


# ------------------------------------------------------- matching, by id only


def test_matching_sorts_into_three_buckets_by_workshop_id(tmp_path):
    local = [
        scope.LocalMod(folder="@Dep", path="x", workshop_id=111, declared=True),
        scope.LocalMod(folder="@CF", path="y", workshop_id=222, declared=True),
    ]
    remote = [ServerMod(111, "a name the server uses"), ServerMod(999, "not here")]
    buckets = scope.match_by_id(remote, local)

    assert [(s.workshop_id, l.folder) for s, l in buckets.matched] == [(111, "@Dep")]
    assert [m.workshop_id for m in buckets.server_only] == [999]
    assert [m.folder for m in buckets.local_only] == ["@CF"]


def test_a_mod_matches_across_four_different_names_for_the_same_id(tmp_path):
    """THE measured reason the id is the identity. One mod is called one thing
    by the server, another in the Workshop, and a third by the folder it sits
    in -- and all three are the same build."""
    local = [scope.LocalMod(folder="@CF", path="x", workshop_id=111,
                            meta_name="a workshop title", declared=True)]
    remote = [ServerMod(111, "an entirely different display name")]
    buckets = scope.match_by_id(remote, local)
    assert len(buckets.matched) == 1
    assert buckets.matched[0][1].folder == "@CF"
    assert not buckets.server_only and not buckets.local_only


def test_the_same_name_with_different_ids_is_two_different_mods(tmp_path):
    """The other half of the same measurement: 19 names among 14 740 ids belong
    to more than one mod, and a name matcher would silently equate different
    builds -- the exact failure that makes an answer confidently wrong."""
    local = [scope.LocalMod(folder="@B", path="x", workshop_id=111,
                            meta_name="a shared name", declared=True)]
    remote = [ServerMod(222, "a shared name")]
    buckets = scope.match_by_id(remote, local)
    assert buckets.matched == ()
    assert [m.workshop_id for m in buckets.server_only] == [222]
    assert [m.folder for m in buckets.local_only] == ["@B"]


def test_a_local_mod_with_no_id_can_never_match_and_is_named_apart(tmp_path):
    """Own mods and hand-made ones. Putting them in `local_only` would read as
    "the server does not run it", which is not something an absent id can say."""
    local = [scope.LocalMod(folder="@MyMod", path="x", workshop_id=0, declared=True)]
    buckets = scope.match_by_id([ServerMod(111, "anything")], local)
    assert buckets.matched == ()
    assert buckets.local_only == ()
    assert [m.folder for m in buckets.unidentified] == ["@MyMod"]


def test_two_folders_carrying_one_workshop_id_both_survive_the_match(tmp_path):
    """Keeping only the first folder for an id made the second vanish from
    EVERY bucket at once: not matched, not installed-only, not unidentified. A
    mod sitting on this machine, reported nowhere, by the code whose whole
    purpose is that nothing is narrowed away quietly.

    Two folders with one id are ordinary here: a copy taken beside the link, or
    the same mod reached both through the modpack and through a declared path
    -- the measurement that found 37 Steam content directories against 35
    modpack folders is the same shape of fact.
    """
    local = [
        scope.LocalMod(folder="@Dep", path="x", workshop_id=111, declared=True),
        scope.LocalMod(folder="@B", path="y", workshop_id=111, declared=False),
    ]
    matched = scope.match_by_id([ServerMod(111, "a name the server uses")], local)
    assert sorted(m.folder for _, m in matched.matched) == ["@B", "@Dep"]
    assert sorted(matched.proposed()) == ["@B", "@Dep"]
    assert matched.shared_ids() == [(111, ["@B", "@Dep"])]

    # And when the server runs neither of them, neither is lost either.
    unmatched = scope.match_by_id([ServerMod(999, "elsewhere")], local)
    assert sorted(m.folder for m in unmatched.local_only) == ["@B", "@Dep"]
    assert unmatched.shared_ids() == [(111, ["@B", "@Dep"])]


def test_one_folder_per_id_is_not_reported_as_a_clash(tmp_path):
    """The ordinary case, so the clash report cannot pass by always firing."""
    local = [
        scope.LocalMod(folder="@Dep", path="x", workshop_id=111, declared=True),
        scope.LocalMod(folder="@B", path="y", workshop_id=222, declared=True),
    ]
    assert scope.match_by_id([ServerMod(111, "n")], local).shared_ids() == []


def test_the_proposed_scope_is_exactly_the_matched_folders():
    local = [
        scope.LocalMod(folder="@Dep", path="x", workshop_id=111, declared=True),
        scope.LocalMod(folder="@CF", path="y", workshop_id=222, declared=True),
    ]
    buckets = scope.match_by_id([ServerMod(222, "n"), ServerMod(111, "m")], local)
    assert sorted(buckets.proposed()) == ["@CF", "@Dep"]


# ------------------------------------------- the index answering inside a set


def seeded(tmp_path: Path) -> KnowledgeStore:
    store = store_with(tmp_path)
    seed(store, DEPS, "a.pbo", [decl("Shared", "@Dep/a/one.c"),
                                decl("OnlyInDep", "@Dep/a/two.c")])
    seed(store, DEPS, "b.pbo", [decl("Shared", "@CF/b/one.c"),
                                decl("OnlyInCF", "@CF/b/two.c")])
    seed(store, DEPS, "c.pbo", [decl("Shared", UNDERSCORED + "/c/one.c")])
    seed(store, CORE, "game.c", [decl("Shared", "3_Game/one.c")])
    seed(store, PROJECT, "mine.c", [decl("Shared", "MyMod/one.c")])
    return store


def test_a_scoped_search_answers_from_the_named_mods_and_from_no_others(tmp_path):
    store = seeded(tmp_path)
    found = store.find("Shared", mods=["@Dep"])
    assert {mod_folder(r.layer, r.file) for r in found} == {"@Dep", ""}
    assert {r.layer for r in found} == {DEPS, CORE, PROJECT}


def test_the_inverse_of_a_scope_is_exactly_what_it_excluded(tmp_path):
    """What makes "named, never hidden" possible at all: the same formula run
    the other way says precisely which mods held the answer that was filtered
    out."""
    store = seeded(tmp_path)
    excluded = store.find("Shared", mods=["@Dep"], outside=True)
    assert sorted(mod_folder(r.layer, r.file) for r in excluded) == [
        "@CF", UNDERSCORED,
    ]


def test_a_mod_folder_holding_an_underscore_is_matched_exactly(tmp_path):
    """An underscore is a single-character wildcard in SQL's LIKE, and real mod
    folders carry underscores. A LIKE-based filter would quietly admit a
    different mod whose name differs in exactly that position."""
    store = store_with(tmp_path)
    seed(store, DEPS, "a.pbo", [decl("Thing", UNDERSCORED + "/a/one.c")])
    seed(store, DEPS, "b.pbo", [decl("Thing", UNDERSCORED.replace("_", "x") + "/b/one.c")])
    found = store.find("Thing", mods=[UNDERSCORED])
    assert [mod_folder(r.layer, r.file) for r in found] == [UNDERSCORED]


def test_a_scope_matches_a_folder_however_it_is_capitalised(tmp_path):
    store = seeded(tmp_path)
    assert store.find("OnlyInDep", mods=["@" + "dEp"])
    assert store.find("OnlyInDep", mods=["@" + "DEP"])


def test_an_empty_scope_list_admits_the_game_and_the_project_and_nothing_else(tmp_path):
    """The store does what it is told. "A set naming no mods" is a coherent
    request -- answer from the game and the project only -- and the tool layer
    is what decides that nobody should ever mean it by accident."""
    store = seeded(tmp_path)
    found = store.find("Shared", mods=[])
    assert {r.layer for r in found} == {CORE, PROJECT}


def test_the_limit_counts_results_that_survived_the_scope(tmp_path):
    """Proof the filter runs inside the query rather than over its output. A
    filter applied afterwards would spend the limit on rows it then discards,
    and a caller asking for two would get one."""
    store = store_with(tmp_path)
    for index in range(10):
        seed(store, DEPS, f"out{index}.pbo", [decl(f"Thing{index:02d}", f"@CF/x/{index}.c")])
    for index in range(10):
        seed(store, DEPS, f"in{index}.pbo", [decl(f"Thing{index:02d}", f"@Dep/x/{index}.c")])
    found = store.find("Thing", mods=["@Dep"], prefix=True, limit=5)
    assert len(found) == 5
    assert all(mod_folder(r.layer, r.file) == "@Dep" for r in found)


def test_overrides_obeys_the_scope_too(tmp_path):
    """The tool that answers "who changes this" is exactly the one whose empty
    answer sends an agent off to write a modded class of its own."""
    store = store_with(tmp_path)
    seed(store, CORE, "game.c", [decl("Base", "3_Game/base.c")])
    seed(store, DEPS, "a.pbo", [decl("FromDep", "@Dep/a/one.c", parent="Base")])
    seed(store, DEPS, "b.pbo", [decl("FromCF", "@CF/b/one.c", parent="Base")])

    assert [r.name for r in store.overrides("Base", mods=["@Dep"])] == ["FromDep"]
    assert [r.name for r in store.overrides("Base", mods=["@Dep"], outside=True)] == ["FromCF"]


def test_the_sql_filter_and_the_python_formula_agree_on_every_record(tmp_path):
    """Two spellings of one rule is how a filter quietly stops matching the
    function that explains it. This is the parity check that keeps them one.
    """
    store = seeded(tmp_path)
    everything = store.find("", prefix=True, limit=1000)
    assert everything
    for name in ("@Dep", "@CF", UNDERSCORED, "@" + "nothing_indexed_here"):
        by_sql = {(r.layer, r.file) for r in store.find("", prefix=True, mods=[name], limit=1000)}
        by_python = {
            (r.layer, r.file) for r in everything
            if mod_folder(r.layer, r.file).lower() in ("", name.lower())
        }
        assert by_sql == by_python, name


def test_scoped_searches_still_use_the_name_index(tmp_path):
    """"Instant" is the point of the whole phase, and a filter term that stole
    the query plan would pass every test above -- just slower as the corpus
    grows."""
    store = seeded(tmp_path)
    plan = store.explain_find("Shared", mods=["@Dep"])
    assert "idx_decl_name" in plan


def test_the_mod_folders_a_layer_holds_can_be_listed_with_their_weight(tmp_path):
    """What a caller needs to choose a set at all, and what tells a typo from a
    mod that is genuinely not indexed."""
    store = seeded(tmp_path)
    folders = dict(store.mod_folders())
    assert folders["@Dep"] == 2
    assert folders["@CF"] == 2
    assert folders[UNDERSCORED] == 1
    assert "" not in folders
