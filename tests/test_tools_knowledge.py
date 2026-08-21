"""The five knowledge tools: build, status, find, show, overrides.

Four properties are what this layer is FOR, and each one is a way the index
could otherwise lie to the agent that trusts it:

1. **An answer never hides the age of the layer it came from.** A project layer
   built before the last edit answers about code that no longer exists, and it
   has to say so itself -- the same discipline as "could not measure" instead of
   "frozen" in the bridge.
2. **An empty index says what to build.** "Nothing found" from a layer that was
   never built is not an answer, it is a silence dressed as one.
3. **Every search has a ceiling.** The predecessor project's two search tools
   hang forever because their client was built without a timeout. An unbounded
   answer is its own kind of hang.
4. **A long build returns a job id.** Measured on real data: the game's scripts
   3.9 s, the game with its configs 69 s, the dependency archives 150 s.

The fifth thing under test is the separation of `kind="config"` from
`kind="class"`. There are three times as many config classes as script
declarations (131 697 against 43 579 in the game alone), so a caller asking
about a script class must not have to wade through them -- and a caller asking
"is there an item class with this name in the game" must be able to ask exactly
that.
"""
import re
import textwrap
import threading
from pathlib import Path

import pytest

from dayz_mcp import server as mcp_server
from dayz_mcp import tools
from dayz_mcp.knowledge.parse import CLASS, CONFIG, CONSTANT, METHOD, Declaration
from dayz_mcp.knowledge.store import CORE, DEPS, PROJECT, KnowledgeStore, SearchTimeout
from dayz_mcp.tools import knowledge, session

PROFILE = """
[project]
name = "my-mod"

[build]
mods = ["MyMod"]
"""


def make_project(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "dayz-mcp.toml").write_text(textwrap.dedent(PROFILE), encoding="utf-8")
    mod = root / "MyMod"
    src = mod / "scripts" / "4_World"
    src.mkdir(parents=True, exist_ok=True)
    # A mod folder needs one to be packable at all, and it doubles as the
    # config-kind half of the project layer.
    (mod / "config.cpp").write_text(
        "class CfgPatches\n{\n    class ProjectPatch\n    {\n        units[]={};\n    };\n};\n",
        encoding="utf-8",
    )
    (src / "thing.c").write_text(
        "class ProjectThing extends ItemBase\n"
        "{\n"
        "    void OnProjectStart(int howMany)\n"
        "    {\n"
        "        Print(howMany);\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )
    return root


def open_project(root: Path):
    session.reset()
    make_project(root)
    return tools.project_open(str(root))


def decl(name, kind=CLASS, **kw) -> Declaration:
    return Declaration(name=name, kind=kind, **kw)


def seed(layer: str, path: str, declarations, root: str = "") -> None:
    """Put declarations into a layer without touching a real game or modpack.

    Passing size/mtime explicitly is the store's own escape hatch for callers
    that have no file -- the point here is the tool layer, not the walk.
    """
    store = session.knowledge()
    store.put_source(
        layer, path, declarations, root=root, size=len(declarations) + 1, mtime=1000.0
    )


def build_project_layer() -> dict:
    """Run the project layer to completion and hand back the job."""
    started = tools.knowledge_build(layer="project")
    assert started.ok, started.error
    done = tools.job_wait(started.data["job_id"], timeout=60)
    assert done.ok, done.error
    return done.data


# --------------------------------------------------------------------- build


def test_every_knowledge_tool_refuses_without_a_project(tmp_path):
    """The index lives in the project's own .dayz-mcp/, so there is no index to
    speak of until a project is open -- and the refusal has to say that rather
    than answer emptily."""
    session.reset()
    for call in (
        lambda: tools.knowledge_build(),
        lambda: tools.knowledge_status(),
        lambda: tools.knowledge_find("Thing"),
        lambda: tools.knowledge_show("Thing"),
        lambda: tools.knowledge_overrides("Thing"),
    ):
        result = call()
        assert not result.ok
        assert "project" in result.hint


def test_build_returns_a_job_id_without_waiting_for_the_build(tmp_path, monkeypatch):
    """PROPERTY 4. The dependency layer took 150 s on this machine's modpack and
    the game with its configs 69 s. A tool that blocked for that would stall the
    whole session -- so the job id has to come back while the work is still
    running, which is what the gate here proves."""
    open_project(tmp_path / "proj")

    inside = threading.Event()
    release = threading.Event()
    real = knowledge.build_project

    def slow(store, root, **kw):
        inside.set()
        assert release.wait(timeout=10), "the test never released the worker"
        return real(store, root, **kw)

    monkeypatch.setattr(knowledge, "build_project", slow)
    started = tools.knowledge_build(layer="project")
    assert started.ok, started.error
    assert started.data["job_id"]
    assert inside.wait(timeout=10), "the build never started"
    # The tool answered while the worker is provably still inside the build.
    assert tools.job_status(started.data["job_id"]).data["status"] in ("queued", "running")
    release.set()
    tools.job_wait(started.data["job_id"], timeout=30)


def test_build_indexes_the_projects_own_sources(tmp_path):
    open_project(tmp_path / "proj")
    job = build_project_layer()
    assert job["status"] == "done", job
    found = tools.knowledge_find("ProjectThing")
    assert found.ok, found.error
    assert found.data["count"] == 1
    record = found.data["results"][0]
    assert record["layer"] == PROJECT
    assert record["parent"] == "ItemBase"
    assert record["line"] == 1


def test_build_refuses_an_unknown_layer(tmp_path):
    open_project(tmp_path / "proj")
    result = tools.knowledge_build(layer="everything")
    assert not result.ok
    for name in (PROJECT, DEPS, CORE, "all"):
        assert name in result.hint


def test_build_refuses_a_second_build_of_the_same_project(tmp_path, monkeypatch):
    """Two builds of one layer would write the same rows from two threads. The
    store is transactional, so nothing is corrupted -- but the second build's
    report describes a layer the first one is still rewriting, and the counts it
    prints are nobody's truth."""
    open_project(tmp_path / "proj")
    release = threading.Event()
    inside = threading.Event()
    real = knowledge.build_project

    def slow(store, root, **kw):
        inside.set()
        assert release.wait(timeout=10)
        return real(store, root, **kw)

    monkeypatch.setattr(knowledge, "build_project", slow)
    first = tools.knowledge_build(layer="project")
    assert inside.wait(timeout=10)
    second = tools.knowledge_build(layer="project")
    assert not second.ok
    assert first.data["job_id"] in second.hint
    release.set()
    tools.job_wait(first.data["job_id"], timeout=30)


def test_all_builds_what_it_can_and_names_what_it_cannot(tmp_path, monkeypatch):
    """A machine with no game installed can still index the project. The layers
    it cannot build are named with the reason, rather than failing the call or
    -- worse -- being left out of the answer entirely.

    The game is hidden deliberately: this machine has one, and without this the
    test would spend a minute indexing the real installation to prove a point
    about machines that have none."""
    open_project(tmp_path / "proj")
    monkeypatch.setattr(session, "game", lambda: None)
    started = tools.knowledge_build(layer="all")
    assert started.ok, started.error
    assert PROJECT in started.data["layers"]
    job = tools.job_wait(started.data["job_id"], timeout=60).data
    text = job["summary"] + job["error"]
    assert CORE in text
    assert "game" in text.lower()


# ------------------------------------------- only=: the caller knows what moved


def test_only_reindexes_exactly_the_named_file(tmp_path):
    """The route Task 3 handed over. A full project rebuild walks the tree; an
    agent that just saved one file already knows which one, so naming it skips
    the walk entirely. What it must NOT do is mistake "I only looked at one
    file" for "the layer has one file"."""
    root = tmp_path / "proj"
    open_project(root)
    other = root / "MyMod" / "scripts" / "4_World" / "other.c"
    other.write_text("class OtherThing {}\n", encoding="utf-8")
    build_project_layer()
    assert tools.knowledge_find("OtherThing").data["count"] == 1

    edited = root / "MyMod" / "scripts" / "4_World" / "thing.c"
    edited.write_text("class RenamedThing extends ItemBase {}\n", encoding="utf-8")
    started = tools.knowledge_build(layer="project", only=[str(edited)])
    assert started.ok, started.error
    job = tools.job_wait(started.data["job_id"], timeout=30).data
    assert job["status"] == "done", job

    assert tools.knowledge_find("RenamedThing").data["count"] == 1
    assert tools.knowledge_find("ProjectThing").data["count"] == 0
    # Everything not named survived -- the layer was not pruned to the one file.
    assert tools.knowledge_find("OtherThing").data["count"] == 1


def test_only_forgets_a_file_that_was_deleted(tmp_path):
    """`only` means "these paths changed", and a delete is a change. A named
    path that is gone is dropped from the index; nothing else is."""
    root = tmp_path / "proj"
    open_project(root)
    other = root / "MyMod" / "scripts" / "4_World" / "other.c"
    other.write_text("class OtherThing {}\n", encoding="utf-8")
    build_project_layer()

    other.unlink()
    started = tools.knowledge_build(layer="project", only=[str(other)])
    tools.job_wait(started.data["job_id"], timeout=30)
    assert tools.knowledge_find("OtherThing").data["count"] == 0
    assert tools.knowledge_find("ProjectThing").data["count"] == 1


def test_only_refuses_when_the_layer_was_never_built(tmp_path):
    """Indexing one file into a layer that does not exist would produce a layer
    of one file that looks like the whole project -- the confident-and-wrong
    answer this phase exists to prevent."""
    root = tmp_path / "proj"
    open_project(root)
    target = root / "MyMod" / "scripts" / "4_World" / "thing.c"
    result = tools.knowledge_build(layer="project", only=[str(target)])
    assert not result.ok
    assert "knowledge_build" in result.hint


def test_only_names_a_path_it_cannot_index(tmp_path):
    """A path outside the project, or one that is not a source at all, is
    ignored -- and said out loud, because an agent that thinks it reindexed a
    file it did not is exactly one silent lie away from a wrong answer."""
    root = tmp_path / "proj"
    open_project(root)
    build_project_layer()
    outsider = tmp_path / "elsewhere.c"
    outsider.write_text("class Outsider {}\n", encoding="utf-8")
    started = tools.knowledge_build(layer="project", only=[str(outsider)])
    job = tools.job_wait(started.data["job_id"], timeout=30).data
    assert "elsewhere.c" in (job["summary"] + job["error"])
    assert tools.knowledge_find("Outsider").data["count"] == 0


def test_only_and_full_together_are_refused(tmp_path):
    open_project(tmp_path / "proj")
    result = tools.knowledge_build(layer="project", only=["a.c"], full=True)
    assert not result.ok


def test_only_is_refused_for_layers_that_are_not_files(tmp_path):
    """`only` names files. A dependency layer's unit of change is a whole
    archive and the core layer's is the game, so the flag has no meaning there
    and pretending otherwise would index a pbo path into the wrong layer."""
    open_project(tmp_path / "proj")
    for layer in (DEPS, CORE, "all"):
        result = tools.knowledge_build(layer=layer, only=["a.c"])
        assert not result.ok, layer
        assert PROJECT in result.hint


# -------------------------------------------------------------------- status


def test_status_lists_every_layer_including_the_ones_never_built(tmp_path):
    """PROPERTY 2 for status: a layer that was never built is a fact worth
    reporting, and leaving it out of the list is how "not built" becomes
    indistinguishable from "empty"."""
    open_project(tmp_path / "proj")
    result = tools.knowledge_status()
    assert result.ok, result.error
    by_name = {layer["layer"]: layer for layer in result.data["layers"]}
    assert set(by_name) == {PROJECT, DEPS, CORE}
    assert all(not layer["built"] for layer in by_name.values())
    # "Never built" is a to-do list, and a layer this project does not have is
    # not on it: a project with no declared dependencies would otherwise be told
    # forever to build a layer that can never hold anything. It is still listed
    # above, with the reason, so "does not apply" never reads as "forgotten".
    assert set(result.data["never_built"]) == {PROJECT, CORE}
    assert by_name[DEPS]["applies"] is False
    assert "mods.required" in by_name[DEPS]["why_not"]
    assert "knowledge_build" in result.hint


def test_status_reports_each_layer_separately_with_its_age(tmp_path):
    open_project(tmp_path / "proj")
    build_project_layer()
    result = tools.knowledge_status()
    by_name = {layer["layer"]: layer for layer in result.data["layers"]}
    project = by_name[PROJECT]
    assert project["built"] is True
    assert project["declarations"] >= 2
    assert project["age_seconds"] is not None
    assert project["age"]
    assert project["stale"] is False
    # The other two are untouched by building this one -- that separation is the
    # whole reason there are three layers.
    assert by_name[CORE]["built"] is False
    assert by_name[DEPS]["built"] is False


def test_status_measures_staleness_rather_than_guessing_it(tmp_path):
    root = tmp_path / "proj"
    open_project(root)
    build_project_layer()
    edited = root / "MyMod" / "scripts" / "4_World" / "thing.c"
    edited.write_text("class ProjectThing extends ItemBase {}\n// touched\n", encoding="utf-8")
    result = tools.knowledge_status()
    project = {layer["layer"]: layer for layer in result.data["layers"]}[PROJECT]
    assert project["stale"] is True
    assert any("thing.c" in path for path in project["changed"])
    assert PROJECT in result.data["stale_layers"]
    assert "knowledge_build" in result.hint


def test_status_sees_a_source_the_index_has_never_met(tmp_path):
    root = tmp_path / "proj"
    open_project(root)
    build_project_layer()
    (root / "MyMod" / "scripts" / "4_World" / "fresh.c").write_text(
        "class FreshThing {}\n", encoding="utf-8"
    )
    project = {
        layer["layer"]: layer for layer in tools.knowledge_status().data["layers"]
    }[PROJECT]
    assert project["stale"] is True
    assert any("fresh.c" in path for path in project["added"])


def test_status_separates_sources_that_gave_nothing_from_sources_that_were_indexed(tmp_path):
    """Task 3's concern, in the place it asked for it. `sources` counts every
    archive the build walked, unreadable ones included -- and a source that
    could not be read is not a source that was indexed. Conflating them is a
    quiet overstatement of coverage."""
    open_project(tmp_path / "proj")
    seed(CORE, "a.c", [decl("Alpha")])
    seed(CORE, "unreadable.pbo", [])
    core = {layer["layer"]: layer for layer in tools.knowledge_status().data["layers"]}[CORE]
    assert core["sources"] == 2
    assert core["empty_sources"] == 1


def test_status_carries_the_last_builds_skipped_sources(tmp_path):
    """The count above says how many gave nothing; only the build knows WHY.
    That record is kept beside the index so status can name them instead of
    making the reader go and rebuild to find out."""
    open_project(tmp_path / "proj")
    build_project_layer()
    project = {
        layer["layer"]: layer for layer in tools.knowledge_status().data["layers"]
    }[PROJECT]
    assert project["last_build"] is not None
    assert project["last_build"]["layer"] == PROJECT
    assert "skipped" in project["last_build"]


# ---------------------------------------------------------------------- find


def test_find_on_an_empty_index_says_what_to_build(tmp_path):
    """PROPERTY 2. Answering "nothing found" out of an index nobody has built is
    the failure mode this whole phase exists to remove."""
    open_project(tmp_path / "proj")
    result = tools.knowledge_find("ItemBase")
    assert not result.ok
    assert "knowledge_build" in result.hint
    assert CORE in result.hint


def test_find_names_the_layer_and_its_age(tmp_path):
    """PROPERTY 1, the plain half: every answer says which layer answered and
    how old that layer is."""
    open_project(tmp_path / "proj")
    build_project_layer()
    result = tools.knowledge_find("ProjectThing")
    assert result.ok, result.error
    layers = {view["layer"]: view for view in result.data["layers"]}
    assert PROJECT in layers
    assert layers[PROJECT]["age_seconds"] is not None
    assert layers[PROJECT]["age"]
    assert result.data["results"][0]["layer"] == PROJECT


def test_a_stale_project_layer_says_so_on_a_successful_answer(tmp_path):
    """PROPERTY 1, the half that matters. The answer is found, the record looks
    right, and the file it describes was edited after the index was built. An
    answer that stayed silent here would be a confident description of code that
    no longer exists."""
    root = tmp_path / "proj"
    open_project(root)
    build_project_layer()
    edited = root / "MyMod" / "scripts" / "4_World" / "thing.c"
    edited.write_text("class ProjectThing extends House {}\n", encoding="utf-8")

    result = tools.knowledge_find("ProjectThing")
    assert result.ok, result.error
    assert result.data["count"] == 1
    assert result.data["stale"] is True
    project = {view["layer"]: view for view in result.data["layers"]}[PROJECT]
    assert project["stale"] is True
    assert "knowledge_build" in result.hint


def test_an_empty_answer_from_an_incomplete_index_is_a_refusal(tmp_path):
    """"Not found" and "not looked" are different answers, and only one of them
    is safe to act on. With a layer that could have held the name never built,
    this is the second."""
    open_project(tmp_path / "proj")
    build_project_layer()
    result = tools.knowledge_find("PlayerBase")
    assert not result.ok
    # The refusal still carries what was measured -- the same shape the bridge
    # uses for a refusal that observed something.
    assert result.data["count"] == 0
    assert CORE in result.error or CORE in result.hint
    assert "knowledge_build" in result.hint


def test_an_empty_answer_from_a_complete_index_is_a_real_answer(tmp_path):
    open_project(tmp_path / "proj")
    build_project_layer()
    seed(CORE, "core.c", [decl("ItemBase")])
    seed(DEPS, "dep.pbo", [decl("DepThing")])
    result = tools.knowledge_find("NoSuchNameAnywhere")
    assert result.ok, result.error
    assert result.data["count"] == 0
    assert result.data["unbuilt"] == []


def test_find_separates_script_classes_from_config_classes(tmp_path):
    """The design point. Config classes outnumber script declarations three to
    one, so they live under their own kind: asking for a script class must not
    return a pile of config entries, and "is there an item class with this name
    in the game" has to be a question you can actually ask."""
    open_project(tmp_path / "proj")
    seed(CORE, "scripts/barrel.c", [decl("Barrel_ColorBase", CLASS, parent="ItemBase")])
    seed(CORE, "Addons/gear.pbo", [decl("Barrel_ColorBase", CONFIG, parent="Container_Base")])

    script_only = tools.knowledge_find("Barrel_ColorBase", kind=CLASS)
    assert [r["kind"] for r in script_only.data["results"]] == [CLASS]
    config_only = tools.knowledge_find("Barrel_ColorBase", kind=CONFIG)
    assert [r["kind"] for r in config_only.data["results"]] == [CONFIG]
    both = tools.knowledge_find("Barrel_ColorBase")
    assert {r["kind"] for r in both.data["results"]} == {CLASS, CONFIG}
    assert both.data["by_kind"] == {CLASS: 1, CONFIG: 1}


def test_find_refuses_an_unknown_kind(tmp_path):
    open_project(tmp_path / "proj")
    build_project_layer()
    result = tools.knowledge_find("ProjectThing", kind="klass")
    assert not result.ok
    for kind in (CLASS, METHOD, CONSTANT, CONFIG):
        assert kind in result.hint


def test_find_caps_the_number_of_results_and_says_it_capped_them(tmp_path):
    open_project(tmp_path / "proj")
    seed(CORE, "many.c", [decl(f"Many{n:03d}") for n in range(40)])
    result = tools.knowledge_find("Many", prefix=True, limit=5)
    assert result.ok, result.error
    assert result.data["count"] == 5
    assert result.data["truncated"] is True
    assert "limit" in result.hint or "kind" in result.hint


def test_find_has_a_time_ceiling(tmp_path, monkeypatch):
    """PROPERTY 3. The predecessor's search tools hang forever, because the
    client behind them was built with no timeout at all. Here the ceiling is the
    store's, and this proves the tool turns it into an answer rather than an
    exception nobody sees."""
    open_project(tmp_path / "proj")
    build_project_layer()

    def slow(*args, **kwargs):
        raise SearchTimeout("the search was still running after 5s")

    monkeypatch.setattr(KnowledgeStore, "find", slow)
    result = tools.knowledge_find("ProjectThing")
    assert not result.ok
    assert "5s" in result.error or "ceiling" in result.error.lower()
    assert result.hint


def test_the_store_ceiling_actually_interrupts_a_query(tmp_path):
    """And the ceiling itself is not a decoration: a deadline already past has
    to stop a real query, or the tool above is guarding nothing."""
    with KnowledgeStore(tmp_path / "index.db") as store:
        store.put_source(
            CORE, "big.c", [decl(f"Name{n:05d}") for n in range(5000)],
            size=1, mtime=1.0,
        )
        with pytest.raises(SearchTimeout):
            with store.time_limit(0.0):
                store.find("", prefix=True, limit=10_000)
        # And the handler is removed afterwards, or every later search on this
        # connection would inherit a deadline that has long since passed.
        assert store.find("Name00001")


# ---------------------------------------------------------------------- show


def test_show_returns_the_declaration_with_its_members_and_ancestry(tmp_path):
    open_project(tmp_path / "proj")
    build_project_layer()
    seed(CORE, "core.c", [decl("ItemBase", CLASS, parent="InventoryItem"),
                          decl("InventoryItem", CLASS, parent="EntityAI")])
    result = tools.knowledge_show("ProjectThing")
    assert result.ok, result.error
    shown = result.data["declarations"][0]
    assert shown["name"] == "ProjectThing"
    assert [m["name"] for m in shown["members"]] == ["OnProjectStart"]
    assert shown["inherits"] == ["ItemBase", "InventoryItem", "EntityAI"]


def test_show_reads_the_body_out_of_the_source_when_asked(tmp_path):
    open_project(tmp_path / "proj")
    build_project_layer()
    result = tools.knowledge_show("OnProjectStart", body=True)
    assert result.ok, result.error
    body = result.data["declarations"][0]["body"]
    assert "Print(howMany)" in body["text"]
    assert body["from_line"] == 3


def test_show_says_what_to_do_when_the_name_is_not_indexed(tmp_path):
    open_project(tmp_path / "proj")
    build_project_layer()
    result = tools.knowledge_show("NotAThingHere")
    assert not result.ok
    assert "knowledge_build" in result.hint or "knowledge_find" in result.hint


# ----------------------------------------------------------------- overrides


def test_overrides_finds_who_reopens_a_class_and_who_replaces_a_method(tmp_path):
    open_project(tmp_path / "proj")
    seed(CORE, "core.c", [
        decl("PlayerBase", CLASS, parent="ManBase"),
        decl("OnConnect", METHOD, owner="PlayerBase"),
    ])
    seed(DEPS, "dep.pbo", [
        decl("PlayerBase", CLASS, flags=("modded",)),
        decl("OnConnect", METHOD, owner="PlayerBase", flags=("override",)),
    ])
    result = tools.knowledge_overrides("OnConnect")
    assert result.ok, result.error
    assert result.data["count"] == 1
    assert result.data["results"][0]["layer"] == DEPS
    classes = tools.knowledge_overrides("PlayerBase")
    assert classes.data["count"] >= 1
    assert {view["layer"] for view in classes.data["layers"]} <= {PROJECT, DEPS, CORE}


def test_overrides_on_an_empty_index_says_what_to_build(tmp_path):
    open_project(tmp_path / "proj")
    result = tools.knowledge_overrides("OnConnect")
    assert not result.ok
    assert "knowledge_build" in result.hint


# ------------------------------------------------------ registration and words


@pytest.mark.anyio
async def test_all_five_tools_are_registered_with_real_parameters():
    """`functools.wraps` in server.py is what keeps the parameter names on the
    registered tool. Without it FastMCP publishes an opaque args/kwargs schema
    and the driving agent cannot call these at all -- a phase-1 defect that must
    not come back through a new namespace."""
    listed = {tool.name: tool for tool in await mcp_server.mcp.list_tools()}
    for name in ("knowledge_build", "knowledge_status", "knowledge_find",
                 "knowledge_show", "knowledge_overrides"):
        assert name in listed, name
        assert (listed[name].description or "").strip(), name
    assert "layer" in listed["knowledge_build"].inputSchema["properties"]
    assert "only" in listed["knowledge_build"].inputSchema["properties"]
    assert "name" in listed["knowledge_find"].inputSchema["properties"]
    assert "kind" in listed["knowledge_find"].inputSchema["properties"]
    assert "body" in listed["knowledge_show"].inputSchema["properties"]


@pytest.mark.anyio
async def test_the_knowledge_tool_descriptions_carry_their_contract():
    """These strings are the whole contract the driving agent reads. On this
    project they have rotted repeatedly, so the load-bearing facts are pinned
    as facts -- the phrasing stays free to change."""
    listed = {
        t.name: " ".join((t.description or "").split())
        for t in await mcp_server.mcp.list_tools()
    }

    build = listed["knowledge_build"]
    assert "job_id" in build
    for layer in (PROJECT, DEPS, CORE):
        assert layer in build, layer
    # The measured cost of each layer: it is why this is a job at all, and it is
    # the only thing telling a caller what timeout job_wait deserves.
    assert re.search(r"\d+(\.\d+)?\s*s\b", build), build
    assert "job_wait" in build
    # `only` is a real contract, not a convenience: it does not see files it was
    # not told about, and a description that omitted that would be worse than
    # one that omitted the flag.
    assert "only" in build
    assert "created or deleted" in build

    status = listed["knowledge_status"]
    assert "stale" in status.lower()
    # The two counts a reader must not conflate.
    assert "empty_sources" in status

    find = listed["knowledge_find"]
    # The kind separation is a design decision the caller cannot guess.
    assert "config" in find
    assert "class" in find
    # Property 1 stated where the caller reads it, and property 3 with it.
    assert "stale" in find.lower()
    assert "ceiling" in find.lower()
    # Property 2: an empty answer out of an unbuilt layer is a refusal.
    assert "refused" in find.lower()

    assert "override" in listed["knowledge_overrides"].lower()
    assert "body" in listed["knowledge_show"]


def test_the_index_lives_beside_the_projects_other_working_state(tmp_path):
    root = tmp_path / "proj"
    open_project(root)
    build_project_layer()
    status = tools.knowledge_status()
    assert Path(status.data["index"]) == (root / ".dayz-mcp" / "knowledge.db")
    assert status.data["index_bytes"] > 0


def test_reopening_the_same_project_keeps_one_index(tmp_path):
    """One store per project, held by the session -- two connections to the same
    file would each carry their own transaction and their own view of it."""
    root = tmp_path / "proj"
    open_project(root)
    first = session.knowledge()
    tools.project_open(str(root))
    assert session.knowledge() is first
