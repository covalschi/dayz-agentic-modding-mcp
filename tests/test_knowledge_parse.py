"""Enforce Script declaration parser.

Every sample in this file is copied from the unpacked vanilla `scripts.pbo`,
not invented. Real Enforce carries shapes nobody guesses: multi-line
signatures with string defaults, `proto` declarations with no body, templated
classes, commented-out `modded class` blocks kept as documentation, and doc
comments whose code examples declare classes that do not exist.

The failure this file exists to prevent is the quiet one: a parser that misses
declarations still builds an index, and the index then answers confidently
about an API surface it never saw.
"""
import os
from pathlib import Path

import pytest

from dayz_mcp.knowledge.parse import (
    BOUNDARY,
    CLASS,
    CONSTANT,
    ENUM,
    METHOD,
    Declaration,
    parse_config,
    parse_file,
    parse_source,
    strip_source,
)


def names(decls, kind=None):
    return [d.name for d in decls if kind is None or d.kind == kind]


def one(decls, name, kind=None):
    """The single declaration called `name`, so a test that silently matched
    two of them fails instead of picking the first."""
    found = [d for d in decls if d.name == name and (kind is None or d.kind == kind)]
    assert len(found) == 1, f"expected exactly one {name}, got {found}"
    return found[0]


# ---------------------------------------------------------------- stripping


def test_strip_preserves_offsets_and_lines():
    """Line and column positions must survive stripping, or every reported
    line number is a guess."""
    src = 'class A\n{\n\t// class B\n\tstring s = "class C";\n}\n'
    st = strip_source(src)
    assert len(st.code) == len(src)
    assert len(st.text) == len(src)
    assert st.code.count("\n") == src.count("\n")
    assert st.text.count("\n") == src.count("\n")


def test_strip_blanks_comments_in_both_views():
    src = "int a; // class Hidden\nint b; /* class AlsoHidden */ int c;\n"
    st = strip_source(src)
    assert "Hidden" not in st.code
    assert "Hidden" not in st.text
    assert "AlsoHidden" not in st.code
    assert "AlsoHidden" not in st.text
    assert "int c;" in st.code


def test_strip_blanks_string_contents_in_code_but_keeps_them_in_text():
    """`code` is what the scanner reads, so a class name inside a literal must
    be gone. `text` is what signatures are sliced from, so a default argument
    such as `vector local_pos = "0 0 0"` must survive verbatim."""
    src = 'void F(vector p = "0 0 0", string n = "class Nope") {}\n'
    st = strip_source(src)
    assert "Nope" not in st.code
    assert '"0 0 0"' not in st.code       # contents blanked
    assert st.code.count('"') == src.count('"')  # quotes kept
    assert '"     "' in st.code
    assert '"0 0 0"' in st.text
    assert '"class Nope"' in st.text


def test_strip_does_not_treat_slashes_inside_a_string_as_a_comment():
    """A URL in a literal starts no comment; treating it as one would blank the
    rest of the line, including a real declaration."""
    src = 'class A { string url = "http://example.com"; }\nclass B {}\n'
    st = strip_source(src)
    assert "class B" in st.code
    decls = parse_source(src)
    assert "B" in names(decls, CLASS)


def test_strip_handles_escaped_quote_inside_a_string():
    src = 'string s = "he said \\"class X\\" loudly";\nclass Real {}\n'
    st = strip_source(src)
    assert "class X" not in st.code
    assert "class Real" in st.code


def test_strip_blanks_preprocessor_directives_but_remembers_them():
    """`#ifdef`/`#endif` are not declarations and must not be parsed as any --
    but the index still has to be able to say what a declaration is guarded
    by, so what they said is kept."""
    src = "#ifdef DIAG_DEVELOPER\nclass A {}\n#endif\n"
    st = strip_source(src)
    assert "DIAG_DEVELOPER" not in st.code
    assert "class A" in st.code
    assert st.code.count(BOUNDARY) == 2
    assert sorted(st.directives.values()) == [("endif", ""), ("ifdef", "DIAG_DEVELOPER")]
    # The signature view carries no sentinel: it is sliced into user-facing text.
    assert BOUNDARY not in st.text


def test_a_byte_order_mark_does_not_swallow_the_first_declaration():
    """Found by a name-by-name parity sweep over this machine's modpack: one
    config in 1458 opens with a BOM, and the parser lost its outer class --
    the index then said the mod had no CfgPatches and filed everything nested
    inside it at file scope.

    In Enforce Script the same bug is worse, because the first declaration is
    usually the class: the whole file parsed to nothing. Both readers feeding
    this parser decode as plain utf-8 (`read_text(encoding="utf-8")` and
    `bytes.decode("utf-8")`), and neither of those drops the mark -- so it has
    to be handled here rather than assumed away upstream."""
    bom = "﻿"
    script = parse_source(bom + "class Foo\n{\n\tvoid Bar();\n}\n")
    assert [(d.name, d.kind, d.line) for d in script] == [
        ("Foo", CLASS, 1), ("Bar", METHOD, 3)
    ]
    config = parse_config(bom + "class CfgPatches\n{\n\tclass A {};\n};\n")
    assert [(d.name, d.owner, d.line) for d in config] == [
        ("CfgPatches", "", 1), ("A", "CfgPatches", 3)
    ]
    # The stripper's own contract: both views stay exactly as long as the
    # source, or every offset after the mark points one character wrong.
    src = bom + "class Foo {}\n"
    st = strip_source(src)
    assert len(st.code) == len(src)
    assert len(st.text) == len(src)


# ------------------------------------------------------------------ classes


def test_class_with_extends():
    # 4_world/systems/bot/botstates.c shape
    src = "class BotStateBase extends BotStateBase_Basic\n{\n}\n"
    d = one(parse_source(src, file="botstates.c"), "BotStateBase", CLASS)
    assert d.kind == CLASS
    assert d.parent == "BotStateBase_Basic"
    assert d.owner == ""
    assert d.flags == ()
    assert d.file == "botstates.c"
    assert d.line == 1
    assert d.signature == "class BotStateBase extends BotStateBase_Basic"


def test_class_with_colon_inheritance():
    # 3_game/services/bioslobbyservice.c
    src = "class JsonDataNewsArticle: Managed\n{\n}\n"
    d = one(parse_source(src), "JsonDataNewsArticle", CLASS)
    assert d.parent == "Managed"
    assert d.signature == "class JsonDataNewsArticle: Managed"


def test_class_without_parent():
    src = "class ParticleList\n{\n}\n"
    d = one(parse_source(src), "ParticleList", CLASS)
    assert d.parent == ""


def test_modded_class_carries_the_modded_flag():
    src = "modded class PluginDiagMenu\n{\n}\n"
    d = one(parse_source(src), "PluginDiagMenu", CLASS)
    assert "modded" in d.flags
    assert d.signature == "modded class PluginDiagMenu"


def test_templated_class_keeps_its_bare_name_and_full_signature():
    # 1_core/param.c
    src = "class Param3<Class T1, Class T2, Class T3> extends Param\n{\n}\n"
    d = one(parse_source(src), "Param3", CLASS)
    assert d.parent == "Param"
    assert d.signature == "class Param3<Class T1, Class T2, Class T3> extends Param"


def test_sealed_class_is_a_class_and_keeps_its_members():
    """1_core/physics/physicsworld.c. A class-modifier the parser does not
    know is not one lost class -- it is that class and every member inside it,
    because the body then opens as an anonymous block."""
    src = (
        "sealed class PhysicsWorld\n"
        "{\n"
        "\tprivate void ~PhysicsWorld();\n"
        "\tstatic proto vector GetGravity(notnull IEntity worldEntity);\n"
        "}\n"
    )
    decls = parse_source(src)
    d = one(decls, "PhysicsWorld", CLASS)
    assert d.signature == "sealed class PhysicsWorld"
    assert "modded" not in d.flags
    assert one(decls, "GetGravity", METHOD).owner == "PhysicsWorld"
    assert "proto" in one(decls, "GetGravity", METHOD).flags


def test_other_class_modifiers_are_accepted():
    """Not in vanilla's `scripts.pbo`, but the same parser reads mod PBOs."""
    for modifier in ("abstract", "final", "static", "private"):
        d = one(parse_source(f"{modifier} class Thing {{}}\n"), "Thing", CLASS)
        assert d.signature == f"{modifier} class Thing"


def test_several_one_line_classes_on_consecutive_lines():
    # 4_world/systems/bot/botevents.c
    src = (
        "class BotEventStart : BotEventBase { };\n"
        "class BotEventStop : BotEventBase { };\n"
        "\n"
        "class BotEventEndOK : BotEventBase { };\n"
    )
    decls = parse_source(src)
    assert names(decls, CLASS) == ["BotEventStart", "BotEventStop", "BotEventEndOK"]
    assert [d.line for d in decls if d.kind == CLASS] == [1, 2, 4]
    assert all(d.parent == "BotEventBase" for d in decls if d.kind == CLASS)


# ------------------------------------------------------------------ methods


def test_method_owner_and_override_flag():
    src = (
        "class WorldLighting\n"
        "{\n"
        "\toverride void SetGlobalLighting( int lightingID )\n"
        "\t{\n"
        "\t}\n"
        "}\n"
    )
    d = one(parse_source(src), "SetGlobalLighting", METHOD)
    assert d.owner == "WorldLighting"
    assert "override" in d.flags
    assert d.signature == "override void SetGlobalLighting( int lightingID )"
    assert d.line == 3


def test_proto_native_method_has_no_body_and_is_flagged():
    # 1_core/proto/ shapes
    src = (
        "class SoundObjectBuilder\n"
        "{\n"
        "\tproto native int GetEventNames(out array<string> events);\n"
        "\tproto native void Activate(IEntity owner);\n"
        "}\n"
    )
    decls = parse_source(src)
    assert names(decls, METHOD) == ["GetEventNames", "Activate"]
    d = one(decls, "GetEventNames", METHOD)
    assert "proto native" in d.flags
    assert d.owner == "SoundObjectBuilder"
    assert d.signature == "proto native int GetEventNames(out array<string> events)"


def test_proto_without_native_is_flagged_as_proto_only():
    # 1_core/proto/enscript.c
    src = "class Class\n{\n\tproto static bool CastTo(out Class to, Class from);\n}\n"
    d = one(parse_source(src), "CastTo", METHOD)
    assert "proto" in d.flags
    assert "proto native" not in d.flags


def test_multi_line_signature_is_captured_whole_with_string_defaults():
    # 3_game/particles/particlemanager/particlesource.c
    src = (
        "class Particle\n"
        "{\n"
        "\toverride static Particle CreateOnObject(\n"
        "\t\tint particle_id,\n"
        "\t\tObject parent_obj,\n"
        '\t\tvector local_pos = "0 0 0",\n'
        '\t\tvector local_ori = "0 0 0",\n'
        "\t\tbool force_world_rotation = false )\n"
        "\t{\n"
        "\t\treturn null;\n"
        "\t}\n"
        "}\n"
    )
    d = one(parse_source(src), "CreateOnObject", METHOD)
    assert d.owner == "Particle"
    assert d.line == 3
    assert d.signature == (
        "override static Particle CreateOnObject( int particle_id, Object parent_obj, "
        'vector local_pos = "0 0 0", vector local_ori = "0 0 0", '
        "bool force_world_rotation = false )"
    )


def test_destructor_is_a_method():
    # 1_core/workbenchapi.c
    src = "class WorldEditorAPI\n{\n\tprivate void ~WorldEditorAPI() {}\n}\n"
    d = one(parse_source(src), "~WorldEditorAPI", METHOD)
    assert d.owner == "WorldEditorAPI"


def test_global_function_has_no_owner():
    src = "proto native external void Print(void var);\n"
    d = one(parse_source(src), "Print", METHOD)
    assert d.owner == ""
    assert "proto native" in d.flags


def test_statements_inside_a_method_body_are_not_declarations():
    """Method bodies are full of call-shaped lines. `AddAction(...)` looks
    exactly like a declaration to a line-oriented regex, and there are over a
    thousand of them in vanilla."""
    src = (
        "class PlayerBase\n"
        "{\n"
        "\toverride void SetActions()\n"
        "\t{\n"
        "\t\tsuper.SetActions();\n"
        "\t\tAddAction(ActionOpenDoors);\n"
        "\t\tif (GetGame().IsServer())\n"
        "\t\t{\n"
        "\t\t\tPrint(\"hello\");\n"
        "\t\t}\n"
        "\t\tfor (int i = 0; i < 3; i++)\n"
        "\t\t{\n"
        "\t\t\tint local = i;\n"
        "\t\t}\n"
        "\t}\n"
        "}\n"
    )
    decls = parse_source(src)
    assert names(decls, METHOD) == ["SetActions"]
    assert names(decls, CLASS) == ["PlayerBase"]
    assert names(decls, CONSTANT) == []


def test_member_variables_are_not_methods_and_do_not_break_the_scope():
    src = (
        "class BotEventBase\n"
        "{\n"
        "\tPlayerBase m_Player;\n"
        "\tref array<int> m_Numbers = new array<int>();\n"
        "\tint m_Fixed[4] = {1, 2, 3, 4};\n"
        "\tstring DumpToString () {}\n"
        "}\n"
    )
    decls = parse_source(src)
    assert names(decls, METHOD) == ["DumpToString"]
    assert one(decls, "DumpToString", METHOD).owner == "BotEventBase"


def test_attribute_before_a_member_does_not_hide_the_next_declaration():
    # 3_game/client/syncplayer.c
    src = (
        "class SyncPlayer\n"
        "{\n"
        "\t[NonSerialized()]\n"
        "\tstring m_UID;\n"
        "\n"
        "\tvoid Reset();\n"
        "}\n"
    )
    decls = parse_source(src)
    assert names(decls, METHOD) == ["Reset"]
    assert names(decls, CLASS) == ["SyncPlayer"]


# ------------------------------------------------------- constants and enums


def test_global_constant():
    # 3_game/ce/centraleconomy.c -- the flags the plan names by name
    src = (
        "const int ECE_NOLIFETIME\t\t\t\t\t= 4194304;\t// do not set lifetime\n"
        "const int ECE_PLACE_ON_SURFACE\t\t\t\t= 1060;\n"
    )
    decls = parse_source(src)
    assert names(decls, CONSTANT) == ["ECE_NOLIFETIME", "ECE_PLACE_ON_SURFACE"]
    d = one(decls, "ECE_NOLIFETIME", CONSTANT)
    assert d.owner == ""
    assert d.line == 1
    assert d.signature == "const int ECE_NOLIFETIME = 4194304"


def test_class_constant_has_an_owner():
    src = (
        "class ParticleList\n"
        "{\n"
        '\tstatic const int MODDED_PARTICLE = RegisterParticle("folder/", "name");\n'
        "}\n"
    )
    d = one(parse_source(src), "MODDED_PARTICLE", CONSTANT)
    assert d.owner == "ParticleList"
    assert d.signature == (
        'static const int MODDED_PARTICLE = RegisterParticle("folder/", "name")'
    )


def test_enum_and_its_members():
    # 3_game/enums/ecamerazoomtype.c
    src = (
        "enum ECameraZoomType\n"
        "{\n"
        "\tNONE \t= 0,\n"
        "\tNORMAL \t= 1,\n"
        "\tSHALLOW\t= 2,\n"
        "}\n"
    )
    decls = parse_source(src)
    e = one(decls, "ECameraZoomType", ENUM)
    assert e.kind == ENUM
    assert e.owner == ""
    assert e.signature == "enum ECameraZoomType"
    assert names(decls, CONSTANT) == ["NONE", "NORMAL", "SHALLOW"]
    member = one(decls, "NORMAL", CONSTANT)
    assert member.owner == "ECameraZoomType"
    assert member.line == 4
    assert member.signature == "NORMAL = 1"


def test_enum_member_without_a_value_and_without_a_trailing_comma():
    src = "enum EFoo\n{\n\tA,\n\tB\n}\n"
    decls = parse_source(src)
    assert names(decls, CONSTANT) == ["A", "B"]
    assert one(decls, "B", CONSTANT).signature == "B"


# --------------------------------------- what must NEVER reach the index


def test_line_commented_class_is_ignored():
    # 4_world/systems/bot/botevents.c, verbatim
    src = (
        "class BotEventBase\n{\n}\n"
        "\n"
        "//class BotEventXXX : BotEventBase { void BotEventXXX (PlayerBase p = NULL) { } };\n"
        "\n"
        "class BotEventStart : BotEventBase { };\n"
    )
    decls = parse_source(src)
    assert names(decls, CLASS) == ["BotEventBase", "BotEventStart"]
    assert "BotEventXXX" not in [d.name for d in decls]


def test_block_commented_modded_class_is_ignored_and_the_real_one_is_found():
    # 3_game/particles/particlelist.c, verbatim
    src = (
        "// Register all particles below!\n"
        "\n"
        "// Example how to register particles from a mod\n"
        "/*\n"
        "modded class ParticleList\n"
        "{\n"
        '\tstatic const int MODDED_PARTICLE = RegisterParticle( "mod_folder/" , "my_particle");\n'
        "}\n"
        "*/\n"
        "\n"
        "class ParticleList\n"
        "{\n"
        "}\n"
    )
    decls = parse_source(src)
    assert names(decls, CLASS) == ["ParticleList"]
    d = one(decls, "ParticleList", CLASS)
    assert d.line == 11
    assert "modded" not in d.flags
    assert names(decls, CONSTANT) == []


def test_block_comment_opened_mid_line_hides_everything_to_its_end():
    # 3_game/worldlighting.c, verbatim tail
    src = (
        "class Real\n{\n}\n"
        "\n"
        "/*modded class WorldLighting\n"
        "{\n"
        '\tprotected string lighting_modded = "your\\\\path\\\\to\\\\cfg.txt";\n'
        "\n"
        "\toverride void SetGlobalLighting( int lightingID )\n"
        "\t{\n"
        "\t}\n"
        "}*/\n"
    )
    decls = parse_source(src)
    assert names(decls) == ["Real"]


def test_class_declared_inside_a_doc_comment_example_is_ignored():
    """1_core/proto/serializer.c documents the API with a worked example whose
    `class MyData` sits at the start of its line inside a block comment. A
    crude line sweep counts it; the index must not."""
    src = (
        "/**\n"
        " \\brief Serialization general interface.\n"
        " \\par usage:\n"
        "\tclass MyData\n"
        "\t{\n"
        "\t\tint m_id;\n"
        "\t\tautoptr map<string, float> m_values;\n"
        "\t}\n"
        "\n"
        "\tvoid Serialize(Serializer s)\n"
        "\t{\n"
        "\t\tint statArray[4] = {6,9,2,3};\n"
        "\t}\n"
        "*/\n"
        "class Serializer\n"
        "{\n"
        "\tproto bool Write(void value_out);\n"
        "}\n"
    )
    decls = parse_source(src)
    assert names(decls, CLASS) == ["Serializer"]
    assert names(decls, METHOD) == ["Write"]
    assert "MyData" not in [d.name for d in decls]
    assert "Serialize" not in [d.name for d in decls]


def test_class_name_inside_a_string_literal_is_ignored():
    src = (
        "class ItemBase\n"
        "{\n"
        "\tvoid Spawn()\n"
        "\t{\n"
        '\t\tGetGame().CreateObject("class FakeClass extends Nothing", "0 0 0");\n'
        "\t}\n"
        "}\n"
    )
    decls = parse_source(src)
    assert names(decls, CLASS) == ["ItemBase"]
    assert "FakeClass" not in [d.name for d in decls]


def test_braces_inside_strings_and_comments_do_not_move_the_scope():
    """A stray `}` in a literal would close the class early and hand every
    later method the wrong owner -- a silent corruption, not a crash."""
    src = (
        "class Holder\n"
        "{\n"
        '\tstring m_Json = "{ \\"a\\": 1 }";\n'
        "\t// } this brace is a comment\n"
        "\t/* { and this one too */\n"
        "\tvoid Later() {}\n"
        "}\n"
        "class After {}\n"
    )
    decls = parse_source(src)
    assert one(decls, "Later", METHOD).owner == "Holder"
    assert names(decls, CLASS) == ["Holder", "After"]


# ------------------------------------------------- conditional compilation
#
# Recorded, never resolved. Nearly five percent of vanilla lines are guarded,
# and the symbols are build-configuration flags: the same tree compiles to a
# server, a client and a diag build, so filtering the index against any one
# define set would make it deny methods that exist in the build being run.


def test_declaration_under_ifdef_records_its_guard():
    src = "#ifdef DIAG_DEVELOPER\nclass DebugOnly\n{\n\tvoid Dump();\n}\n#endif\n"
    decls = parse_source(src)
    assert one(decls, "DebugOnly", CLASS).guard == ("DIAG_DEVELOPER",)
    assert one(decls, "Dump", METHOD).guard == ("DIAG_DEVELOPER",)


def test_ifndef_records_a_negated_guard_and_else_flips_it():
    src = (
        "#ifndef SERVER\n"
        "class ClientSide {}\n"
        "#else\n"
        "class ServerSide {}\n"
        "#endif\n"
        "class Always {}\n"
    )
    decls = parse_source(src)
    assert one(decls, "ClientSide", CLASS).guard == ("!SERVER",)
    assert one(decls, "ServerSide", CLASS).guard == ("SERVER",)
    assert one(decls, "Always", CLASS).guard == ()


def test_nested_guards_stack_outermost_first():
    """Vanilla nests these five deep."""
    src = (
        "#ifdef PLATFORM_CONSOLE\n"
        "#ifdef SERVER_FOR_CONSOLE\n"
        "class Both {}\n"
        "#endif\n"
        "class OuterOnly {}\n"
        "#endif\n"
    )
    decls = parse_source(src)
    assert one(decls, "Both", CLASS).guard == ("PLATFORM_CONSOLE", "SERVER_FOR_CONSOLE")
    assert one(decls, "OuterOnly", CLASS).guard == ("PLATFORM_CONSOLE",)


def test_a_class_declared_once_per_branch_is_recorded_once_per_branch():
    """3_game/entities/man.c, verbatim shape: the same class gets two headers
    and one body. Both parents are real -- which one applies is a build
    decision -- so both are indexed and the body is attributed to the class."""
    src = (
        "#ifdef FEATURE_NETWORK_RECONCILIATION\n"
        "class Man extends Person\n"
        "#else\n"
        "class Man extends EntityAI\n"
        "#endif\n"
        "{\n"
        "\tproto native UAInterface GetInputInterface();\n"
        "}\n"
    )
    decls = parse_source(src)
    men = [d for d in decls if d.name == "Man"]
    assert [d.parent for d in men] == ["Person", "EntityAI"]
    assert [d.guard for d in men] == [
        ("FEATURE_NETWORK_RECONCILIATION",),
        ("!FEATURE_NETWORK_RECONCILIATION",),
    ]
    # The shared body still belongs to the class, not to an anonymous block.
    assert one(decls, "GetInputInterface", METHOD).owner == "Man"


def test_a_directive_does_not_end_a_declaration_that_it_interrupts():
    """`#endif` is a boundary, not a statement. Treating it as one detached
    every `#ifdef`-guarded class from the body underneath it."""
    src = "#ifdef X\nclass Guarded\n#endif\n{\n\tvoid Inside();\n}\n"
    assert one(parse_source(src), "Inside", METHOD).owner == "Guarded"


def test_define_is_a_boundary_but_not_a_guard():
    src = "#define DIAG\nclass A {}\n"
    assert one(parse_source(src), "A", CLASS).guard == ()


# ------------------------------------------------------------ error recovery


def test_a_stray_endif_does_not_derail_the_rest_of_the_file():
    src = "#endif\nclass A {}\n#else\nclass B {}\n"
    decls = parse_source(src)
    assert names(decls, CLASS) == ["A", "B"]


def test_an_unterminated_block_comment_ends_at_the_file_and_nothing_hangs():
    src = "class A {}\n/* never closed\nclass B {}\n"
    assert names(parse_source(src), CLASS) == ["A"]


def test_an_unterminated_string_stops_at_the_line_break():
    """Otherwise one missing quote blanks the remainder of the file."""
    src = 'class A { string s = "oops;\n}\nclass B {}\n'
    assert names(parse_source(src), CLASS) == ["A", "B"]


def test_extra_closing_braces_do_not_underflow_the_scope_stack():
    src = "}\n}\nclass A\n{\n\tvoid M();\n}\n}\nclass B {}\n"
    decls = parse_source(src)
    assert names(decls, CLASS) == ["A", "B"]
    assert one(decls, "M", METHOD).owner == "A"


def test_unbalanced_parentheses_do_not_swallow_the_next_declaration():
    src = "class A\n{\n\tvoid Broken(int a\n}\nclass B {}\n"
    # The broken member may or may not be recognised; what must survive is B.
    assert "B" in names(parse_source(src), CLASS)


def test_garbage_between_declarations_is_skipped():
    src = "class A {}\n@#$%^&*\n!!!\nclass B {}\n"
    assert names(parse_source(src), CLASS) == ["A", "B"]


def test_a_declaration_that_never_terminates_does_not_eat_the_next_one():
    """1_core/proto/proto.c lists engine prototypes with no trailing `;`. A
    scanner that reads on to the next `{` walks over the class below them."""
    src = (
        "proto native int SetSoundVolume(HSOUND sound, float volume)\n"
        "proto native int SetSoundFrequency(HSOUND sound, int freq)\n"
        "\n"
        "class PacketOutputAdapter\n"
        "{\n"
        "\tproto native void WriteBool(bool value);\n"
        "}\n"
    )
    decls = parse_source(src)
    assert names(decls, CLASS) == ["PacketOutputAdapter"]
    assert one(decls, "WriteBool", METHOD).owner == "PacketOutputAdapter"
    assert one(decls, "SetSoundVolume", METHOD).owner == ""


def test_a_typedef_without_a_semicolon_does_not_eat_the_next_declaration():
    """3_game/gui/inventorygrid.c and three others end a `typedef` at the line
    break. Reading past it cost the class declared underneath -- and, with it,
    every member of that class."""
    src = (
        "typedef map<InventoryItem, vector> TItemsMap\n"
        "\n"
        "class InventoryGrid extends ScriptedWidgetEventHandler\n"
        "{\n"
        "\tvoid Refresh();\n"
        "}\n"
    )
    decls = parse_source(src)
    assert names(decls, CLASS) == ["InventoryGrid"]
    assert one(decls, "Refresh", METHOD).owner == "InventoryGrid"


def test_class_declared_after_a_block_comment_on_the_same_line():
    """3_game/surfaceinfo.c disables a modifier by commenting it out in place.
    A line-anchored sweep cannot see these two classes at all."""
    src = "/*sealed*/ class SurfaceDetectionParameters\n{\n}\n"
    d = one(parse_source(src), "SurfaceDetectionParameters", CLASS)
    assert d.line == 1


# ------------------------------------------------------------------- files


def test_parse_file_reports_the_path_it_was_given(tmp_path):
    p = tmp_path / "sample.c"
    p.write_text("class A extends B\n{\n}\n", encoding="utf-8")
    decls = parse_file(p)
    assert decls[0].file == str(p)
    assert decls[0].line == 1

    decls = parse_file(p, file="scripts/3_game/sample.c")
    assert decls[0].file == "scripts/3_game/sample.c"


def test_parse_file_survives_a_non_utf8_byte(tmp_path):
    """Vanilla sources are not uniformly UTF-8; a decode error must not take
    the whole layer down."""
    p = tmp_path / "latin.c"
    p.write_bytes(b"// caf\xe9 comment\nclass A\n{\n}\n")
    assert names(parse_file(p), CLASS) == ["A"]


def test_declaration_is_hashable_and_comparable():
    """The store and the parity check both put declarations in sets."""
    a = Declaration(name="A", kind=CLASS, file="f.c", line=1)
    b = Declaration(name="A", kind=CLASS, file="f.c", line=1)
    assert a == b
    assert len({a, b}) == 1


# ------------------------------------------------- the real corpus, if present

# Machine-dependent by nature, so it is named by the environment rather than
# hard-coded: this repository must stay portable, and no test may depend on one
# machine's unpacked copy of the game.
VANILLA = Path(os.environ.get("DAYZ_MCP_VANILLA_SCRIPTS", ""))


@pytest.mark.skipif(
    not (VANILLA.name and VANILLA.is_dir()),
    reason="set DAYZ_MCP_VANILLA_SCRIPTS to an unpacked scripts.pbo to run",
)
def test_real_vanilla_file_parses_with_plausible_shape():
    """The hermetic tests above prove the shapes; this one proves the shapes
    were real. Skipped anywhere the corpus is not unpacked."""
    src = (VANILLA / "3_game" / "enums" / "ecamerazoomtype.c").read_text(
        encoding="utf-8", errors="replace"
    )
    decls = parse_source(src, file="ecamerazoomtype.c")
    assert names(decls, ENUM) == ["ECameraZoomType"]
    assert "NORMAL" in names(decls, CONSTANT)
