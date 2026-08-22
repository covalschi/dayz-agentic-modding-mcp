"""Who CALLS this -- the question no search over declarations can answer.

`knowledge_overrides` finds who re-declares a name. That is a different
question from who uses one, and the second is the one asked when a change is
about to be made: nothing that searches declarations can find a call site,
because a call site is not a declaration.
"""
from pathlib import Path

import pytest

from dayz_mcp.knowledge.calls import CALL, NEW
from dayz_mcp.knowledge.parse import parse_all
from dayz_mcp.knowledge.store import CORE, DEPS, PROJECT, KnowledgeStore


@pytest.fixture
def store(tmp_path):
    with KnowledgeStore(tmp_path / "knowledge.db") as s:
        yield s


def index(store, layer, rel, text):
    """Index one source the way a layer build does: declarations and calls
    from a single read."""
    declarations, calls = parse_all(text, file=rel)
    store.put_source(layer, Path(rel), declarations, calls=calls, size=len(text), mtime=1.0)


def sites(store, name, **kw):
    return [(c.caller, c.file, c.line) for c in store.callers(name, **kw)]


def test_a_call_is_found_with_the_class_and_method_that_made_it(store):
    index(store, CORE, "a.c", "class Alpha { void Run() { Helper(); } }")
    assert sites(store, "Helper") == [("Alpha.Run", "a.c", 1)]


def test_the_declaration_of_the_name_is_not_a_call_of_it(store):
    index(store, CORE, "a.c", "class Alpha { void Helper() { } }")
    assert store.callers("Helper") == []


def test_every_call_site_is_kept_not_deduplicated(store):
    """`_dedupe` exists for declarations. Applying it here would turn
    'called in three places' into 'called'."""
    index(
        store, CORE, "a.c",
        "class Alpha { void Run() { Helper(); Helper(); } void Again() { Helper(); } }",
    )
    assert len(store.callers("Helper")) == 3


def test_instantiation_is_told_apart_from_calling(store):
    index(store, CORE, "a.c", "class Alpha { void Run() { Item i = new Item(); Item.Cast(i); } }")
    assert [c.kind for c in store.callers("Item")] == [NEW]
    assert [c.kind for c in store.callers("Cast")] == [CALL]
    assert store.callers("Item", kind=CALL) == []


def test_the_caller_class_narrows_the_answer(store):
    index(
        store, CORE, "a.c",
        "class Alpha { void Run() { Helper(); } }\nclass Beta { void Run() { Helper(); } }",
    )
    assert sites(store, "Helper", owner="Beta") == [("Beta.Run", "a.c", 2)]


def test_matching_is_case_insensitive_like_every_other_search(store):
    index(store, CORE, "a.c", "class Alpha { void Run() { Helper(); } }")
    assert len(store.callers("helper")) == 1


def test_re_indexing_a_source_replaces_its_call_sites(store):
    """The failure this pins is the one the whole store was built against: a
    layer rebuilt on top of itself still answers, it just answers twice."""
    index(store, PROJECT, "a.c", "class Alpha { void Run() { Helper(); } }")
    index(store, PROJECT, "a.c", "class Alpha { void Run() { Helper(); } }")
    assert len(store.callers("Helper")) == 1


def test_dropping_a_source_takes_its_call_sites_with_it(store):
    index(store, PROJECT, "a.c", "class Alpha { void Run() { Helper(); } }")
    store.drop_source(PROJECT, Path("a.c"))
    assert store.callers("Helper") == []


def test_a_layer_can_be_asked_for_on_its_own(store):
    index(store, CORE, "a.c", "class Alpha { void Run() { Helper(); } }")
    index(store, PROJECT, "b.c", "class Beta { void Run() { Helper(); } }")
    assert sites(store, "Helper", layer=PROJECT) == [("Beta.Run", "b.c", 1)]
    assert len(store.callers("Helper")) == 2


def test_the_nearest_layer_is_answered_first(store):
    index(store, CORE, "a.c", "class Alpha { void Run() { Helper(); } }")
    index(store, PROJECT, "b.c", "class Beta { void Run() { Helper(); } }")
    assert [c.layer for c in store.callers("Helper")] == [PROJECT, CORE]


def test_the_active_mod_set_narrows_call_sites_exactly_as_it_narrows_declarations(store):
    """A call site inside a mod the server does not run is as misleading as a
    declaration from one."""
    index(store, DEPS, "ModA/x/a.c", "class Alpha { void Run() { Helper(); } }")
    index(store, DEPS, "ModB/x/b.c", "class Beta { void Run() { Helper(); } }")
    kept = store.callers("Helper", mods=["ModA"])
    assert [c.owner for c in kept] == ["Alpha"]
    dropped = store.callers("Helper", mods=["ModA"], outside=True)
    assert [c.owner for c in dropped] == ["Beta"]


def test_the_game_and_the_project_are_never_narrowed_away(store):
    """Same rule as declarations: an active set that hid the game would answer
    'nobody calls this' about the code the agent is reading."""
    index(store, CORE, "a.c", "class Alpha { void Run() { Helper(); } }")
    assert len(store.callers("Helper", mods=["ModA"])) == 1


def test_the_limit_is_honoured(store):
    body = " ".join(["Helper();"] * 20)
    index(store, CORE, "a.c", "class Alpha { void Run() { %s } }" % body)
    assert len(store.callers("Helper", limit=5)) == 5


def test_the_name_index_drives_the_query(store):
    """A call table that is scanned rather than looked up makes every answer
    slower as the index grows -- which is the one thing an index must not do."""
    index(store, CORE, "a.c", "class Alpha { void Run() { Helper(); } }")
    plan = store.explain_callers("Helper", layer=CORE, mods=["ModA"])
    assert "idx_call_name" in plan
