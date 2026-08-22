"""Static checks that run before anything is packed.

Every defect here costs a full server boot to find otherwise: pack, launch,
wait, read the log. Two of them cost more than that -- they produce a mod that
loads, runs, and silently does nothing, which no boot reports at all.

Each rule was measured against the game's own 2810 sources before it was
written. A rule that fires on vanilla is a rule that would fire on everyone.
"""
import pytest

from dayz_mcp.lint import REFUSE, WARN, lint_text


def checks(text, file="a.c"):
    return [f.check for f in lint_text(text, file)]


def one(text, file="a.c"):
    found = lint_text(text, file)
    assert len(found) == 1, [f.check for f in found]
    return found[0]


# ------------------------------------------------------- a mod that does nothing


def test_a_modded_class_that_extends_itself_is_refused():
    """`modded class X extends X` compiles, loads, and silently applies
    nothing. There is no log line for it."""
    f = one("modded class PlayerBase extends PlayerBase { }")
    assert f.check == "modded-self"
    assert f.severity == REFUSE
    assert "PlayerBase" in f.message


def test_a_modded_class_without_extends_is_correct():
    assert checks("modded class PlayerBase { void Foo() {} }") == []


def test_a_plain_class_extending_another_is_not_the_defect():
    """Only `modded` self-extension is the trap; ordinary inheritance from a
    differently named class is how every mod is written."""
    assert checks("class MyThing extends PlayerBase { }") == []


def test_a_plain_class_extending_itself_is_still_refused():
    f = one("class Loop extends Loop { }")
    assert f.check == "class-self"
    assert f.severity == REFUSE


# ---------------------------------------------------------- not Enforce at all


@pytest.mark.parametrize("word", ["try", "catch", "finally"])
def test_exception_statements_are_refused(word):
    f = one("class A { void Run() { %s { } } }" % word)
    assert f.check == "exceptions"
    assert f.severity == REFUSE


def test_a_commented_out_exception_is_not_code():
    """Written so the raw text WOULD match: without stripping, a linter reads
    the commented-out version and reports it."""
    assert checks("class A { void Run() { /* try { Foo(); } */ } }") == []
    assert checks("class A { void Run() { // try { Foo(); }" + chr(10) + " } }") == []


def test_an_exception_keyword_inside_a_string_is_not_code():
    assert checks('class A { void Run() { Print("try (this)"); } }') == []


def test_a_commented_out_self_extension_is_not_a_defect():
    """The same trap the other way round: the defect is what the compiler
    reads, and the compiler does not read comments."""
    assert checks("// modded class PlayerBase extends PlayerBase") == []
    assert checks(chr(10).join(["/*", "modded class PlayerBase extends PlayerBase", "*/"])) == []


def test_a_leading_plus_inside_a_comment_is_not_a_continuation():
    assert checks('class A { void Run() { /* string s = "a"' + chr(10) + ' + "b" */ } }') == []


def test_a_method_named_try_something_is_not_an_exception():
    assert checks("class A { void Run() { TryAgain(); } }") == []


# ------------------------------------------------------- the statement ends here


def test_a_line_starting_with_plus_is_reported():
    """Enforce statements end at the end of the line: the second line here is
    not a continuation, it is dropped. Zero lines in the whole game start with
    `+`, so this rule cannot fire on ordinary code."""
    f = one('class A { void Run() { string s = "a"\n + "b"; } }')
    assert f.check == "line-continuation"
    assert f.severity == WARN


def test_an_increment_is_not_a_continuation():
    assert checks("class A { void Run() { int i = 0;\n ++i; } }") == []


def test_a_plus_after_a_finished_statement_is_left_alone():
    """After a `;` the parser is not mid-statement, so a leading `+` is
    somebody's formatting, not a dropped expression."""
    assert checks("class A { void Run() { int i = 0;\n + 1; } }") == []


# ----------------------------------------------------------------- the envelope


def test_a_finding_carries_the_file_and_the_line():
    f = one("class A { }\nmodded class B extends B { }", file="mod/x.c")
    assert (f.file, f.line) == ("mod/x.c", 2)


def test_every_finding_says_what_to_do():
    for text in (
        "modded class P extends P { }",
        "class A { void Run() { try { } } }",
    ):
        for f in lint_text(text, "a.c"):
            assert f.hint, f.check


def test_clean_source_produces_nothing():
    assert lint_text("class A extends B { override void Run() { Foo(); } }", "a.c") == []


# --------------------------------------------------- the checks that need an index

from pathlib import Path  # noqa: E402

from dayz_mcp.knowledge.parse import parse_source  # noqa: E402
from dayz_mcp.knowledge.store import CORE, DEPS, PROJECT, KnowledgeStore  # noqa: E402
from dayz_mcp.lint import lint_index  # noqa: E402


@pytest.fixture
def store(tmp_path):
    with KnowledgeStore(tmp_path / "knowledge.db") as s:
        yield s


def seed(store, layer, rel, text):
    store.put_source(layer, Path(rel), parse_source(text, file=rel), size=len(text), mtime=1.0)


def index_findings(store, text, file="mine.c", **kw):
    return lint_index(parse_source(text, file=file), store, **kw)


def test_a_modded_class_with_no_target_is_refused_once_the_layers_are_built(store):
    seed(store, CORE, "game.c", "class PlayerBase { }")
    seed(store, DEPS, "Mod/x/a.c", "class Other { }")
    found = index_findings(store, "modded class PlayerBse { }")
    assert [(f.check, f.severity) for f in found] == [("modded-target", REFUSE)]


def test_a_modded_class_with_a_real_target_is_clean(store):
    seed(store, CORE, "game.c", "class PlayerBase { }")
    seed(store, DEPS, "Mod/x/a.c", "class Other { }")
    assert index_findings(store, "modded class PlayerBase { }") == []


def test_a_target_in_a_dependency_counts(store):
    seed(store, CORE, "game.c", "class PlayerBase { }")
    seed(store, DEPS, "Mod/x/a.c", "class TheirThing { }")
    assert index_findings(store, "modded class TheirThing { }") == []


def test_an_unbuilt_layer_warns_instead_of_accusing(store):
    """The index cannot tell 'this class does not exist' from 'I have not read
    the game yet'. Saying the first when the second is true is the same
    confident lie a stale layer would tell."""
    seed(store, PROJECT, "mine.c", "class Mine { }")
    found = index_findings(store, "modded class PlayerBase { }")
    assert [f.severity for f in found] == [WARN]
    assert "not built" in found[0].message


def test_the_active_mod_set_decides_whether_a_target_exists(store):
    """A target that lives only in a mod the server does not run is a mod that
    loads and modifies nothing -- on that server."""
    seed(store, CORE, "game.c", "class PlayerBase { }")
    seed(store, DEPS, "ModA/x/a.c", "class TheirThing { }")
    assert index_findings(store, "modded class TheirThing { }", mods=["ModA"]) == []
    narrowed = index_findings(store, "modded class TheirThing { }", mods=["ModB"])
    assert [f.check for f in narrowed] == ["modded-target"]


def test_the_project_declaring_it_itself_counts(store):
    seed(store, CORE, "game.c", "class PlayerBase { }")
    seed(store, DEPS, "Mod/x/a.c", "class Other { }")
    seed(store, PROJECT, "mine.c", "class Mine { }")
    assert index_findings(store, "modded class Mine { }") == []
