"""Call sites: who calls what, and from inside which declaration.

The index already answers "who declares this" and "who overrides this". The
question it could not answer is "who CALLS this" -- the one a text sweep
answers worst, because the same name appears in its own declaration, inside
comments, and inside string literals, and a sweep counts all three.
"""

from dayz_mcp.knowledge.calls import Call
from dayz_mcp.knowledge.parse import parse_calls


def sites(source: str, file: str = "test.c") -> list[Call]:
    return parse_calls(source, file)


def named(source: str, name: str) -> list[Call]:
    return [c for c in sites(source) if c.name == name]


def test_call_inside_a_method_names_the_class_and_the_method():
    found = named(
        """
        class Foo
        {
            void Run()
            {
                Helper();
            }
        }
        """,
        "Helper",
    )
    assert len(found) == 1
    assert (found[0].owner, found[0].method) == ("Foo", "Run")
    assert found[0].kind == "call"


def test_a_declaration_is_not_a_call_to_itself():
    """The single most likely way to get this wrong: `void Run()` matches
    `Run(` exactly as a call site does."""
    assert named("class Foo { void Run() { } }", "Run") == []


def test_recursion_is_still_a_call():
    found = named("class Foo { void Run() { Run(); } }", "Run")
    assert len(found) == 1
    assert (found[0].owner, found[0].method) == ("Foo", "Run")


def test_new_is_recorded_as_its_own_kind():
    found = named("class Foo { void Run() { Item x = new Item(); } }", "Item")
    assert [c.kind for c in found] == ["new"]


def test_a_call_inside_a_string_literal_is_not_a_call():
    assert named('class Foo { void Run() { Print("Helper()"); } }', "Helper") == []


def test_a_call_inside_a_comment_is_not_a_call():
    assert named("class Foo { void Run() { /* Helper(); */ } }", "Helper") == []
    assert named("class Foo { void Run() { // Helper();\n } }", "Helper") == []


def test_a_qualified_call_records_the_method_and_the_qualifier():
    found = named("class Foo { void Run() { GetGame().CreateObject(); } }", "CreateObject")
    assert len(found) == 1
    assert found[0].qualifier == "GetGame()"


def test_control_flow_words_are_not_calls():
    source = """
    class Foo
    {
        void Run()
        {
            if (a) { }
            while (b) { }
            switch (c) { }
            foreach (int i : d) { }
            return;
        }
    }
    """
    names = {c.name for c in sites(source)}
    assert not names & {"if", "while", "switch", "foreach", "for", "return"}


def test_a_call_at_file_scope_has_no_owner():
    found = named("void Global() { Helper(); }", "Helper")
    assert len(found) == 1
    assert (found[0].owner, found[0].method) == ("", "Global")


def test_a_global_function_between_two_classes_is_not_attributed_to_the_first():
    source = """
    class First { void A() { } }
    void Between() { Helper(); }
    class Second { void B() { } }
    """
    found = named(source, "Helper")
    assert len(found) == 1
    assert (found[0].owner, found[0].method) == ("", "Between")


def test_the_line_is_the_line_of_the_call_not_of_the_method():
    source = "class Foo\n{\n    void Run()\n    {\n        Helper();\n    }\n}\n"
    found = named(source, "Helper")
    assert [c.line for c in found] == [5]


def test_super_call_is_recorded():
    found = named("class Foo { override void Run() { super.Run(); } }", "Run")
    assert len(found) == 1
    assert found[0].qualifier == "super"


def test_calls_survive_a_conditional_block():
    source = """
    class Foo
    {
        void Run()
        {
            #ifdef DIAG_DEVELOPER
            Helper();
            #endif
        }
    }
    """
    assert len(named(source, "Helper")) == 1


def test_a_cast_is_a_call_like_any_other():
    found = named("class Foo { void Run() { PlayerBase.Cast(x); } }", "Cast")
    assert len(found) == 1
    assert found[0].qualifier == "PlayerBase"
