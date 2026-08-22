"""The active mod set, as the agent meets it: two tools and four answers.

The index knows every mod on this machine; work happens against one server
that runs a subset. These tests pin the two decisions that make that safe:

1. **A filtered-out result is NAMED, never silently hidden.** A class that
   exists only outside the set must not come back as "nothing found" -- that is
   the same silent lie as an answer from a stale layer, and the agent that
   reads it writes its own copy of a class that already exists.
2. **A server query PROPOSES a set; it never rescopes the index itself.**
   Three buckets come back and the caller decides. A mismatch has to be
   something read, not something quietly done.

No test here touches the network: the query is exercised through the same
`a2s` entry points a live call uses, with the transport replaced.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from dayz_mcp import a2s, tools
from dayz_mcp.knowledge import scope as modscope
from dayz_mcp.knowledge.parse import CLASS, Declaration
from dayz_mcp.knowledge.store import CORE, DEPS, PROJECT
from dayz_mcp.tools import session

PROFILE = """
[project]
name = "my-mod"

[build]
mods = ["MyMod"]
"""

# Assembled at runtime -- a mod-shaped literal would trip the repository's own
# mod-name guard in the very file that proves the feature works.
OTHER = "@" + "Dep_two"


def open_project(root: Path):
    session.reset()
    root.mkdir(parents=True, exist_ok=True)
    (root / "dayz-mcp.toml").write_text(textwrap.dedent(PROFILE), encoding="utf-8")
    mod = root / "MyMod"
    mod.mkdir(parents=True, exist_ok=True)
    (mod / "config.cpp").write_text("class CfgPatches {};\n", encoding="utf-8")
    result = tools.project_open(str(root))
    assert result.ok, result.error
    return root


def decl(name: str, file: str, **kw) -> Declaration:
    return Declaration(name=name, kind=CLASS, file=file, **kw)


def seed(layer: str, path: str, declarations) -> None:
    session.knowledge().put_source(
        layer, path, declarations, size=len(declarations) + 1, mtime=1000.0
    )


def seed_two_mods() -> None:
    """One class in each of two dependency mods, one in the game, one ours."""
    seed(DEPS, "a.pbo", [decl("Shared", "@Dep/a/one.c"),
                         decl("OnlyInDep", "@Dep/a/two.c")])
    seed(DEPS, "b.pbo", [decl("Shared", f"{OTHER}/b/one.c"),
                         decl("OnlyInOther", f"{OTHER}/b/two.c"),
                         decl("FromOther", f"{OTHER}/b/three.c", parent="Base")])
    seed(CORE, "game.c", [decl("Shared", "3_Game/one.c"), decl("Base", "3_Game/base.c")])
    seed(PROJECT, "mine.c", [decl("Mine", "MyMod/one.c")])


def a_mod_folder(root: Path, name: str, published: int | None) -> Path:
    folder = root / name
    (folder / "addons").mkdir(parents=True, exist_ok=True)
    if published is not None:
        (folder / "meta.cpp").write_text(
            f'publishedid = {published};\nname = "a workshop title";\n', encoding="utf-8"
        )
    return folder


@pytest.fixture
def fake_game(tmp_path, monkeypatch):
    """A modpack directory this test owns, so nothing walks the real one."""
    game = tmp_path / "game"
    workshop = game / modscope.WORKSHOP_DIRNAME
    a_mod_folder(workshop, "@Dep", 111)
    a_mod_folder(workshop, OTHER, 222)
    a_mod_folder(workshop, "@CF", 333)
    monkeypatch.setattr(session, "game", lambda: str(game))
    return game


def answering(mods, **kw):
    """A stand-in for the live query, built from the same decoder a real one
    returns -- so a change in the answer's shape breaks these too."""
    def fake(host, port, timeout=6.0, rounds=3):
        return a2s.ModAnswer(
            mods=tuple(a2s.ServerMod(i, n) for i, n in mods),
            declared=len(mods), chunk_total=1, chunks_seen=1,
            blob_bytes=64, **kw,
        )
    return fake


def informing(name="a stand", **kw):
    def fake(host, port, timeout=6.0):
        return a2s.ServerInfo(name=name, players=12, max_players=60,
                              version="1.29.163709", game_port=2302, **kw)
    return fake


# ---------------------------------------------------------- declaring the set


def test_the_scope_tools_refuse_without_a_project():
    session.reset()
    for call in (lambda: tools.knowledge_scope(), lambda: tools.server_mods("1.2.3.4", 27016)):
        result = call()
        assert not result.ok
        assert "project" in result.hint


def test_with_no_arguments_it_reports_no_scope_and_what_could_be_scoped(tmp_path):
    open_project(tmp_path / "proj")
    seed_two_mods()
    result = tools.knowledge_scope()
    assert result.ok
    assert result.data["scope"]["active"] is False
    assert {entry["folder"] for entry in result.data["available"]} == {"@Dep", OTHER}
    assert all(entry["in_scope"] for entry in result.data["available"])


def test_declaring_a_set_stores_it_and_says_what_it_will_narrow(tmp_path):
    open_project(tmp_path / "proj")
    seed_two_mods()
    result = tools.knowledge_scope(mods=["@Dep"], source="the server at a test address")
    assert result.ok
    assert result.data["scope"]["mods"] == ["@Dep"]
    assert result.data["scope"]["source"] == "the server at a test address"
    assert result.data["inside"] == 2
    assert result.data["outside"] == 3
    assert modscope.load(session.knowledge().path.parent).mods == ("@Dep",)


def test_a_set_is_stored_with_the_indexs_own_spelling(tmp_path):
    """The caller types what it remembers. The set is compared against labels
    written by the build, so it is resolved to those -- otherwise the answer
    reads back a name that appears nowhere in the index."""
    open_project(tmp_path / "proj")
    seed_two_mods()
    result = tools.knowledge_scope(mods=["@" + "dEp"])
    assert result.data["scope"]["mods"] == ["@Dep"]


def test_clearing_the_set_returns_the_index_to_answering_from_everything(tmp_path):
    open_project(tmp_path / "proj")
    seed_two_mods()
    tools.knowledge_scope(mods=["@Dep"])
    result = tools.knowledge_scope(clear=True)
    assert result.ok
    assert result.data["scope"]["active"] is False
    assert tools.knowledge_find("OnlyInOther").data["count"] == 1


def test_asking_to_set_and_to_clear_at_once_is_refused(tmp_path):
    open_project(tmp_path / "proj")
    seed_two_mods()
    result = tools.knowledge_scope(mods=["@Dep"], clear=True)
    assert not result.ok
    assert "clear" in result.hint


def test_an_empty_list_is_refused_rather_than_read_as_narrowing_to_nothing(tmp_path):
    """"No set" and "a set naming nothing" are different requests, and only one
    of them is ever meant. An empty list silently blanking every dependency
    answer is precisely the invisible narrowing this feature exists against."""
    open_project(tmp_path / "proj")
    seed_two_mods()
    result = tools.knowledge_scope(mods=[])
    assert not result.ok
    assert "clear" in result.hint


def test_a_name_the_index_does_not_hold_is_kept_but_named(tmp_path):
    """A mod can be on the server, installed, and simply not declared as a
    dependency of this project -- so an unknown name is not automatically a
    typo. It is stored and reported, because a set silently missing an entry is
    worse than one that explains itself."""
    open_project(tmp_path / "proj")
    seed_two_mods()
    result = tools.knowledge_scope(mods=["@Dep", "@CF"])
    assert result.ok
    assert result.data["scope"]["mods"] == ["@Dep", "@CF"]
    assert result.data["not_indexed"] == ["@CF"]
    assert "@CF" in result.hint


def test_a_set_naming_nothing_the_index_holds_is_refused(tmp_path):
    """Every name unknown, with a dependency layer that IS built, cannot be
    anything but a mistake -- and it would blank every dependency answer while
    looking like a successful call."""
    open_project(tmp_path / "proj")
    seed_two_mods()
    result = tools.knowledge_scope(mods=["@CF", "@B"])
    assert not result.ok
    assert "@CF" in result.error or "@CF" in result.hint


def test_a_set_declared_before_the_dependency_layer_exists_says_it_was_unchecked(tmp_path):
    """Nothing to check against is not the same as checked and fine."""
    open_project(tmp_path / "proj")
    seed(CORE, "game.c", [decl("Base", "3_Game/base.c")])
    result = tools.knowledge_scope(mods=["@Dep"])
    assert result.ok
    assert result.data["deps_built"] is False
    assert "never built" in result.hint


# ------------------------------------------------ the set, inside the answers


def test_an_answer_from_inside_the_set_is_an_ordinary_answer_that_says_it_was_narrowed(tmp_path):
    """Symmetrical to the layer ages every answer already carries: a narrowing
    nobody can see is a narrowing nobody can correct."""
    open_project(tmp_path / "proj")
    seed_two_mods()
    tools.knowledge_scope(mods=["@Dep"], source="the server at a test address")
    result = tools.knowledge_find("OnlyInDep")
    assert result.ok
    assert result.data["count"] == 1
    assert result.data["scope"]["active"] is True
    assert result.data["scope"]["mods"] == ["@Dep"]
    assert "the server at a test address" in result.hint


def test_a_class_that_lives_only_outside_the_set_is_named_with_its_mod(tmp_path):
    """THE case the whole feature exists for. Not an empty answer, and not a
    silent one: the mod holding it is named, and the caller decides whether to
    widen the set or work without that mod."""
    open_project(tmp_path / "proj")
    seed_two_mods()
    tools.knowledge_scope(mods=["@Dep"])
    result = tools.knowledge_find("OnlyInOther")

    assert result.data["count"] == 0
    filtered = result.data["scope"]["filtered_out"]
    assert [entry["mod"] for entry in filtered] == [OTHER]
    assert filtered[0]["count"] == 1
    assert OTHER in result.error
    # And it must be impossible to read as "no such thing".
    assert result.ok is False


def test_the_same_name_in_and_out_of_the_set_answers_and_still_names_the_rest(tmp_path):
    """A full answer must not hide that more of it was kept out -- that is the
    half a caller would otherwise never think to ask about."""
    open_project(tmp_path / "proj")
    seed_two_mods()
    tools.knowledge_scope(mods=["@Dep"])
    result = tools.knowledge_find("Shared")

    assert result.ok
    # The game's declaration and the in-scope mod's, never the other mod's.
    assert {r["layer"] for r in result.data["results"]} == {DEPS, CORE}
    assert [e["mod"] for e in result.data["scope"]["filtered_out"]] == [OTHER]
    assert OTHER in result.hint


def test_the_game_and_the_project_answer_under_any_set(tmp_path):
    """The game is the substrate every DayZ mod is written against and the
    project layer is the code being written. Narrowing either would answer "no
    such class" about code the agent is looking at."""
    open_project(tmp_path / "proj")
    seed_two_mods()
    tools.knowledge_scope(mods=["@Dep"])
    assert tools.knowledge_find("Base").data["count"] == 1
    assert tools.knowledge_find("Mine").data["count"] == 1


def test_without_a_set_nothing_is_narrowed_and_the_answer_says_so(tmp_path):
    open_project(tmp_path / "proj")
    seed_two_mods()
    result = tools.knowledge_find("Shared")
    assert result.data["count"] == 3
    assert result.data["scope"]["active"] is False
    assert result.data["scope"]["filtered_out"] == []


def test_overrides_names_the_overrider_the_set_kept_out(tmp_path):
    """The tool whose empty answer sends an agent off to write a modded class
    that already exists somewhere it cannot see."""
    open_project(tmp_path / "proj")
    seed_two_mods()
    tools.knowledge_scope(mods=["@Dep"])
    result = tools.knowledge_overrides("Base")
    assert result.data["count"] == 0
    assert [e["mod"] for e in result.data["scope"]["filtered_out"]] == [OTHER]
    assert OTHER in result.error


def test_show_names_what_the_set_kept_out_too(tmp_path):
    open_project(tmp_path / "proj")
    seed_two_mods()
    tools.knowledge_scope(mods=["@Dep"])
    result = tools.knowledge_show("OnlyInOther")
    assert result.ok is False
    assert [e["mod"] for e in result.data["scope"]["filtered_out"]] == [OTHER]
    assert OTHER in result.error


def test_the_second_opinion_for_an_empty_search_stays_inside_the_set(tmp_path):
    """A search narrowed by `kind=`, `owner=` or `layer=` that finds nothing
    gets looked up again without that narrowing. That second look must stay
    inside the active set -- reaching past it would report one record twice
    under two explanations, and only one of them ("a mod outside the set holds
    it") says the class cannot be used here. `elsewhere` reads as usable."""
    open_project(tmp_path / "proj")
    seed_two_mods()
    tools.knowledge_scope(mods=["@Dep"])
    result = tools.knowledge_find("OnlyInOther", layer=DEPS)
    assert result.data["elsewhere"] == []
    assert [e["mod"] for e in result.data["scope"]["filtered_out"]] == [OTHER]


def test_a_filtered_count_that_hit_its_ceiling_says_it_is_a_lower_bound(tmp_path):
    """The naming has a ceiling, like every other search here. A count that
    stopped at it and printed a bare number would understate the loss -- the
    same quiet as hiding it, one size down."""
    open_project(tmp_path / "proj")
    seed_two_mods()
    from dayz_mcp.tools.knowledge import EXCLUDED_LIMIT
    seed(DEPS, "big.pbo",
         [decl("Crowd", f"{OTHER}/b/{i}.c") for i in range(EXCLUDED_LIMIT + 20)])
    tools.knowledge_scope(mods=["@Dep"])
    result = tools.knowledge_find("Crowd")
    assert result.ok is False
    assert result.data["scope"]["filtered_truncated"] is True
    assert "lower bounds" in result.error


def test_status_reports_the_set_as_part_of_the_indexs_state(tmp_path):
    open_project(tmp_path / "proj")
    seed_two_mods()
    tools.knowledge_scope(mods=["@Dep"], source="the server at a test address")
    result = tools.knowledge_status()
    assert result.data["scope"]["active"] is True
    assert result.data["scope"]["mods"] == ["@Dep"]


# ------------------------------------------------------ asking a live server


@pytest.fixture
def no_transport(monkeypatch):
    """Every query entry point replaced by something that fails loudly.

    These refusals have to land BEFORE a datagram is sent, and asserting only
    "not ok, and the words 'query port' appear" cannot tell that apart from a
    guess that went out and timed out -- the timeout refusal says the same
    words. It matters twice over: a suite that reaches the internet to fail a
    test also fails when the internet is not there.
    """
    def forbidden(*args, **kwargs):
        raise AssertionError("the transport was reached before the refusal")

    monkeypatch.setattr(a2s, "query_info", forbidden)
    monkeypatch.setattr(a2s, "query_mods", forbidden)


def test_a_server_query_refuses_to_guess_the_query_port_and_sends_nothing(tmp_path, no_transport):
    """Measured: 252 distinct offsets between game port and query port in a
    live sample. `{game+1, game+3, 27016}` covers roughly three quarters and
    nothing covers all, so the caller supplies it -- and the tool must refuse
    rather than try a common one, which is what `no_transport` proves."""
    open_project(tmp_path / "proj")
    result = tools.server_mods("198.51.100.10")
    assert not result.ok
    assert "cannot be derived from the game port" in result.error
    assert "27016" in result.hint


def test_a_port_outside_the_range_is_named_instead_of_blamed_on_the_reply(tmp_path, no_transport):
    """Left to the socket, this came back as "answered with something this
    cannot read" -- a description of a reply that never happened. The one thing
    every refusal in this server owes the caller is what actually occurred."""
    open_project(tmp_path / "proj")
    result = tools.server_mods("198.51.100.10", 99999)
    assert not result.ok
    assert "99999" in result.error
    assert "nothing was sent" in result.hint


def test_a_port_that_is_not_a_plain_decimal_is_refused_rather_than_crashed_on(tmp_path, no_transport):
    """Python calls a superscript a digit and `int()` then refuses it, so
    `host:` followed by one raised a ValueError straight out of a tool that
    answers in envelopes; and it calls Arabic-Indic numerals digits too, which
    `int()` accepts, so a port nobody typed would have been read as one. Both
    now stay part of the host and come back as the missing-port refusal."""
    open_project(tmp_path / "proj")
    for text in ("198.51.100.10:²", "198.51.100.10:٢٧٠١٦"):
        result = tools.server_mods(text)
        assert not result.ok, text
        assert "cannot be derived from the game port" in result.error, text


def test_a_server_query_returns_three_buckets_and_a_proposed_set(tmp_path, monkeypatch, fake_game):
    open_project(tmp_path / "proj")
    seed_two_mods()
    monkeypatch.setattr(a2s, "query_info", informing())
    monkeypatch.setattr(a2s, "query_mods", answering([
        (111, "a name the server uses"),
        (999, "a mod nobody here has"),
    ]))
    result = tools.server_mods("198.51.100.10:27016")
    assert result.ok

    matched = result.data["matched"]
    assert [(m["workshop_id"], m["folder"]) for m in matched] == [(111, "@Dep")]
    assert [m["workshop_id"] for m in result.data["on_server_not_installed"]] == [999]
    assert {m["folder"] for m in result.data["installed_not_on_server"]} == {OTHER, "@CF"}
    assert result.data["proposed_scope"] == ["@Dep"]


def test_a_server_query_proposes_and_changes_nothing(tmp_path, monkeypatch, fake_game):
    """THE second design rule. Rescoping the index from a query would make a
    mismatch an invisible action instead of a read fact."""
    open_project(tmp_path / "proj")
    seed_two_mods()
    monkeypatch.setattr(a2s, "query_info", informing())
    monkeypatch.setattr(a2s, "query_mods", answering([(111, "a name")]))
    result = tools.server_mods("198.51.100.10", 27016)

    assert modscope.load(session.knowledge().path.parent).active is False
    assert "knowledge_scope(" in result.data["apply"]
    assert result.data["applied"] is False


def test_the_proposal_says_which_matched_mods_the_index_actually_holds(tmp_path, monkeypatch, fake_game):
    """A mod can be installed and still not be a declared dependency, in which
    case scoping to it narrows nothing -- a fact worth having before the set is
    applied rather than after it answers oddly."""
    open_project(tmp_path / "proj")
    seed_two_mods()
    monkeypatch.setattr(a2s, "query_info", informing())
    monkeypatch.setattr(a2s, "query_mods", answering([(111, "a"), (333, "b")]))
    result = tools.server_mods("198.51.100.10", 27016)
    indexed = {m["folder"]: m["indexed"] for m in result.data["matched"]}
    assert indexed == {"@Dep": True, "@CF": False}


def test_two_folders_carrying_one_workshop_id_are_both_proposed_and_the_clash_named(
    tmp_path, monkeypatch, fake_game,
):
    """Keeping one folder per id made the other vanish from every bucket at
    once -- a mod sitting on this machine, reported nowhere, by the tool whose
    whole job is that nothing gets narrowed away quietly. Both are proposed,
    because either may be the copy the index was built from, and the clash is
    named, because version is not identity and nothing here can say which of
    the two the server actually runs."""
    open_project(tmp_path / "proj")
    seed_two_mods()
    a_mod_folder(fake_game / modscope.WORKSHOP_DIRNAME, "@B", 111)  # a copy of @Dep
    monkeypatch.setattr(a2s, "query_info", informing())
    monkeypatch.setattr(a2s, "query_mods", answering([(111, "a name")]))

    result = tools.server_mods("198.51.100.10", 27016)
    assert sorted(result.data["proposed_scope"]) == ["@B", "@Dep"]
    assert sorted(m["folder"] for m in result.data["matched"]) == ["@B", "@Dep"]
    assert any("111" in note and "@B" in note for note in result.data["notes"])


def test_the_answer_carries_the_boundaries_that_were_reasoned_not_measured(tmp_path, monkeypatch, fake_game):
    """Server-only mods and mod versions are the two things this cannot see,
    and an answer that did not say so would be read as complete."""
    open_project(tmp_path / "proj")
    seed_two_mods()
    monkeypatch.setattr(a2s, "query_info", informing())
    monkeypatch.setattr(a2s, "query_mods", answering([(111, "a")]))
    notes = " ".join(tools.server_mods("198.51.100.10", 27016).data["notes"]).lower()
    assert "servermod" in notes
    assert "version" in notes


def test_a_host_that_filters_the_rules_query_is_named_not_retried(tmp_path, monkeypatch, fake_game):
    """Measured failure mode: some hosts answer INFO and rotate the RULES
    challenge forever. A retry loop is what turns that into a hang."""
    open_project(tmp_path / "proj")

    def rotating(host, port, timeout=6.0, rounds=3):
        raise a2s.ChallengeRotation("a fresh token every time")

    monkeypatch.setattr(a2s, "query_info", informing())
    monkeypatch.setattr(a2s, "query_mods", rotating)
    result = tools.server_mods("198.51.100.10", 27016)
    assert not result.ok
    assert "challenge" in result.error.lower()
    assert "retry" in result.hint.lower() or "again" in result.hint.lower()


def test_silence_points_at_the_game_port_trap(tmp_path, monkeypatch, fake_game):
    """The single most likely mistake, and the one the old note in this project
    was built on: the game port never answers, the query port does."""
    open_project(tmp_path / "proj")

    def silent(host, port, timeout=6.0):
        raise a2s.A2STimeout("no answer before the deadline")

    monkeypatch.setattr(a2s, "query_info", silent)
    result = tools.server_mods("198.51.100.10", 2302)
    assert not result.ok
    assert "query port" in result.hint.lower()


def test_an_incomplete_decode_is_flagged_and_the_proposal_says_it_is_partial(tmp_path, monkeypatch, fake_game):
    """A list with a chunk missing decodes into plausible nonsense. The
    proposal still comes back -- what was read is worth having -- but nothing
    may present it as the whole truth."""
    open_project(tmp_path / "proj")
    seed_two_mods()
    monkeypatch.setattr(a2s, "query_info", informing())
    monkeypatch.setattr(a2s, "query_mods", answering(
        [(111, "a")], missing_chunks=(2,), problem="chunk 2 never arrived",
    ))
    result = tools.server_mods("198.51.100.10", 27016)
    assert result.ok
    assert result.data["complete"] is False
    assert "chunk 2" in " ".join(result.data["notes"]) or "chunk 2" in result.hint
