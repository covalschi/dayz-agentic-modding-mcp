"""The .layout grammar, as the shipped files actually use it.

Brace-delimited `key value` pairs, one per line, NOT XML: a widget is
`ClassName Name {`, its properties, an optional `{ ... }` block of children,
and its own `}`. Multi-word keys are quoted (`"exact text" 1`), string values
are quoted (`text "Hello"`), `//` starts a comment.
"""
import os
from pathlib import Path

import pytest

from dayz_mcp.layoutparse import LayoutSyntaxError, parse_layout, tokenize

SMALL = '''
// a comment line
FrameWidgetClass Root {
 visible 1
 position 0 0
 size 1 1
 "exact text" 0
 {
  TextWidgetClass Title {
   text "Hello world"
   "exact text size" 18
  }
  ButtonWidgetClass BtnOk {
   text ""
   {
    PanelWidgetClass Bg {
     color 0.1 0.2 0.3 1
    }
   }
  }
 }
}
'''


def test_tokens_keep_quoted_words_together():
    assert tokenize('"exact text" 1', 1) == [("exact text", True), ("1", False)]
    assert tokenize('text "Hello world" // note', 1) == [("text", False), ("Hello world", True)]
    assert tokenize("   ", 1) == []


def test_an_unterminated_quote_is_a_syntax_error():
    with pytest.raises(LayoutSyntaxError) as caught:
        tokenize('text "oops', 7)
    assert caught.value.line == 7


def test_the_tree_has_classes_names_props_and_children():
    root = parse_layout(SMALL)
    assert (root.cls, root.name, root.line) == ("FrameWidgetClass", "Root", 3)
    assert root.prop("size") == ["1", "1"]
    assert root.prop("exact text") == ["0"]
    assert root.prop("missing") is None
    assert [c.name for c in root.children] == ["Title", "BtnOk"]
    assert root.children[0].prop("text") == ["Hello world"]
    assert root.children[1].children[0].prop("color") == ["0.1", "0.2", "0.3", "1"]


def test_walk_paths_match_the_engine_walker():
    """Depth-first, declaration order, dotted indexes, root is ''. The same
    addressing the bridge's DZMCP_Ui.Walk produces, so an engine node and a
    source node can be paired by path."""
    root = parse_layout(SMALL)
    assert [(p, n.name) for p, n in root.walk()] == [
        ("", "Root"), ("0", "Title"), ("1", "BtnOk"), ("1.0", "Bg"),
    ]


def test_a_property_after_the_child_block_is_refused():
    text = "FrameWidgetClass A {\n visible 1\n {\n }\n color 1 1 1 1\n}\n"
    with pytest.raises(LayoutSyntaxError) as caught:
        parse_layout(text)
    assert caught.value.line == 5


def test_a_brace_on_its_own_line_after_the_header_still_opens_the_widget():
    text = "FrameWidgetClass A\n{\n visible 1\n}\n"
    assert parse_layout(text).prop("visible") == ["1"]


def test_two_roots_are_refused():
    with pytest.raises(LayoutSyntaxError):
        parse_layout("FrameWidgetClass A {\n}\nFrameWidgetClass B {\n}\n")


def test_an_unclosed_widget_is_refused():
    with pytest.raises(LayoutSyntaxError):
        parse_layout("FrameWidgetClass A {\n visible 1\n")


def test_widget_declared_in_header_state_is_refused():
    """A child widget declared while parent is still in header state (no opening
    brace yet) should be rejected, not silently become the parent's child."""
    text = "FrameWidgetClass A\nTextWidgetClass B {\n visible 1\n}\n{\n}\n"
    with pytest.raises(LayoutSyntaxError) as caught:
        parse_layout(text)
    assert caught.value.line == 2
    assert "child block needs its own" in caught.value.message


def test_widget_with_two_sibling_blocks_parses():
    """Two sibling { ... } blocks in one widget: child widgets, then ScriptParams.
    Validates real vanilla grammar where blocks can contain either widget
    declarations or special config like ScriptParamsClass."""
    text = """FrameWidgetClass Root {
 position 0 0
 size 1 1
 {
  TextWidgetClass Title {
   text "Hello"
  }
 }
 {
  ScriptParamsClass {
   border 10
  }
 }
}
"""
    root = parse_layout(text)
    assert root.cls == "FrameWidgetClass"
    assert root.name == "Root"
    # Two children: Title and ScriptParamsClass
    assert len(root.children) == 2
    assert root.children[0].name == "Title"
    assert root.children[1].name == ""  # ScriptParamsClass without instance name
    assert root.children[1].cls == "ScriptParamsClass"
    # Paths should be in declaration order
    paths = [(p, n.name) for p, n in root.walk()]
    assert paths == [("", "Root"), ("0", "Title"), ("1", "")]


def test_script_params_class_without_instance_name():
    """ScriptParamsClass can appear without an instance name; should parse
    with empty-string name."""
    text = """FrameWidgetClass Root {
 {
  ScriptParamsClass {
   AlignChilds 1
  }
 }
}
"""
    root = parse_layout(text)
    assert len(root.children) == 1
    child = root.children[0]
    assert child.cls == "ScriptParamsClass"
    assert child.name == ""
    assert child.prop("AlignChilds") == ["1"]


VANILLA = os.environ.get("DAYZ_GUI_LAYOUTS", "")


@pytest.mark.skipif(not VANILLA, reason="set DAYZ_GUI_LAYOUTS to an unpacked gui.pbo layouts dir")
def test_every_vanilla_layout_parses():
    """The grammar test: 216 shipped files, zero syntax errors."""
    files = sorted(Path(VANILLA).rglob("*.layout"))
    assert files, VANILLA
    broken = []
    for path in files:
        try:
            parse_layout(path.read_text(encoding="utf-8", errors="replace"))
        except LayoutSyntaxError as exc:
            broken.append(f"{path.name}: {exc}")
    assert broken == []
