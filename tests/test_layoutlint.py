"""Static checks on .layout files. Each refusing rule names a failure that the
engine reports nowhere: a hung parser, a silently ignored property, an
ItemPreview that draws nothing."""
import pytest

from dayz_mcp.layoutlint import lint_layout
from dayz_mcp.lint import REFUSE, WARN


def wrap(body: str) -> str:
    return "FrameWidgetClass Root {\n size 1 1\n {\n" + body + " }\n}\n"


def checks(text):
    return [(f.check, f.severity) for f in lint_layout(text, "a.layout")]


def one(text, check):
    found = [f for f in lint_layout(text, "a.layout") if f.check == check]
    assert len(found) == 1, [f.check for f in lint_layout(text, "a.layout")]
    return found[0]


def test_a_clean_layout_has_no_findings():
    text = wrap('  TextWidgetClass T {\n   size 100 20\n   text "Hi"\n   "exact text" 1\n  }\n')
    assert checks(text) == []


def test_syntax_errors_refuse_with_the_line():
    f = one("FrameWidgetClass A {\n visible 1\n", "layout-syntax")
    assert f.severity == REFUSE and f.line == 2


def test_an_unknown_widget_class_is_refused():
    f = one(wrap("  BogusTextWidgetClass T {\n   size 1 1\n  }\n"), "layout-class")
    assert f.severity == REFUSE and "BogusTextWidgetClass" in f.message


def test_an_unquoted_multi_word_key_is_refused():
    f = one(wrap("  TextWidgetClass T {\n   size 1 1\n   exact text 1\n  }\n"), "layout-unquoted-key")
    assert f.severity == REFUSE and '"exact text"' in f.message
    assert ("layout-key", WARN) not in checks(wrap("  TextWidgetClass T {\n   size 1 1\n   exact text 1\n  }\n"))


def test_a_multiword_keys_first_word_being_a_standalone_key_does_not_hide_it():
    """`text`, `size`, `stretch` and `disabled` are each both a standalone key
    and the first word of a multi-word one -- unquoted, only the tokens that
    follow say which was meant, so the multi-word reconstruction has to be
    tried before the standalone key is accepted."""
    assert checks(wrap("  TextWidgetClass T {\n   size 1 1\n   text color 1 1 1 1\n  }\n")) == [
        ("layout-unquoted-key", REFUSE)]
    assert checks(wrap("  TextWidgetClass T {\n   size 1 1\n   stretch mode stretch_w_h\n  }\n")) == [
        ("layout-unquoted-key", REFUSE)]
    assert checks(wrap("  TextWidgetClass T {\n   size 1 1\n   size to text h 1\n  }\n")) == [
        ("layout-unquoted-key", REFUSE)]
    assert checks(wrap('  TextWidgetClass T {\n   size 1 1\n   "text color" 1 1 1 1\n  }\n')) == []
    assert checks(wrap('  TextWidgetClass T {\n   size 1 1\n   text "say "hi" now"\n  }\n')) == [
        ("layout-quote-in-text", REFUSE)]
    assert checks(wrap('  TextWidgetClass T {\n   size 1 1\n   text "Hello"\n  }\n')) == []


def test_a_quoted_value_is_never_mistaken_for_an_unquoted_multiword_key():
    """`text "color"` is the widget's text, set to the literal word "color" --
    not the key `text color` written without its quotes. A quoted token is a
    value, never a stray key word, so it stops the reconstruction outright."""
    assert checks(wrap('  TextWidgetClass T {\n   size 1 1\n   text "color"\n  }\n')) == []
    assert checks(wrap('  TextWidgetClass T {\n   size 1 1\n   text "offset"\n  }\n')) == []
    assert checks(wrap('  TextWidgetClass T {\n   size 1 1\n   stretch "mode"\n  }\n')) == []


def test_the_longest_known_multiword_key_wins_over_a_shorter_prefix():
    """`Scrollbar V` alone is a real key, and so is `Scrollbar V Left`; unquoted
    `Scrollbar V Left 1` must be read as the longer one. Same for `exact text`
    versus `exact text size`."""
    findings = lint_layout(wrap("  TextWidgetClass T {\n   size 1 1\n   Scrollbar V Left 1\n  }\n"), "a.layout")
    assert [(f.check, f.severity) for f in findings] == [("layout-unquoted-key", REFUSE)]
    assert "'Scrollbar V Left'" in findings[0].message

    findings = lint_layout(wrap("  TextWidgetClass T {\n   size 1 1\n   exact text size 32\n  }\n"), "a.layout")
    assert [(f.check, f.severity) for f in findings] == [("layout-unquoted-key", REFUSE)]
    assert "'exact text size'" in findings[0].message


def test_an_unknown_key_only_warns():
    f = one(wrap("  TextWidgetClass T {\n   size 1 1\n   borderRadius 4\n  }\n"), "layout-key")
    assert f.severity == WARN


def test_a_quote_inside_a_text_value_is_refused():
    text = wrap('  TextWidgetClass T {\n   size 1 1\n   text "say "hi" now"\n  }\n')
    f = one(text, "layout-quote-in-text")
    assert f.severity == REFUSE


def test_a_negative_size_is_refused_but_a_negative_position_is_fine():
    assert one(wrap("  PanelWidgetClass P {\n   size -1 10\n  }\n"), "layout-negative-size").severity == REFUSE
    assert checks(wrap("  PanelWidgetClass P {\n   position -1 -1\n   size 10 10\n  }\n")) == []


def test_an_item_preview_under_a_high_priority_is_refused():
    text = ("FrameWidgetClass Root {\n priority 990\n {\n"
            "  ItemPreviewWidgetClass P {\n   size 1 1\n  }\n }\n}\n")
    f = one(text, "layout-preview-priority")
    assert f.severity == REFUSE and "990" in f.message
    fine = ("FrameWidgetClass Root {\n priority 200\n {\n"
            "  ItemPreviewWidgetClass P {\n   size 1 1\n   priority 250\n  }\n }\n}\n")
    assert checks(fine) == []


def test_a_scroll_widget_without_clipchildren_warns():
    f = one(wrap('  ScrollWidgetClass S {\n   size 1 1\n   "Scrollbar V" 1\n  }\n'), "layout-scroll-no-clip")
    assert f.severity == WARN
    assert checks(wrap('  ScrollWidgetClass S {\n   size 1 1\n   clipchildren 1\n  }\n')) == []


def test_an_edit_box_with_neither_style_nor_panel_warns():
    bare = wrap("  EditBoxWidgetClass E {\n   position 10 10\n   size 100 20\n  }\n")
    assert one(bare, "layout-editbox-bare").severity == WARN
    styled = wrap("  EditBoxWidgetClass E {\n   position 10 10\n   size 100 20\n   style Default\n  }\n")
    assert checks(styled) == []
    framed = wrap("  PanelWidgetClass F {\n   position 8 8\n   size 104 24\n   style rover_sim_colorable\n  }\n"
                  "  EditBoxWidgetClass E {\n   position 10 10\n   size 100 20\n  }\n")
    assert checks(framed) == []
    too_small = wrap("  PanelWidgetClass F {\n   position 8 8\n   size 50 24\n   style rover_sim_colorable\n  }\n"
                     "  EditBoxWidgetClass E {\n   position 10 10\n   size 100 20\n  }\n")
    assert one(too_small, "layout-editbox-bare").severity == WARN


def test_a_proportional_flag_paired_with_a_pixel_number_warns():
    """The ContactList shape measured on the stand, 2026-09-03: `size 600
    395` with `vexactsize 0` is not 395 px tall, it is 395 PARENT heights --
    the cause of a spacer measured at 231148 px."""
    contact_list = wrap('  WrapSpacerWidgetClass ContactList {\n   size 600 395\n   hexactsize 1\n   vexactsize 0\n  }\n')
    f = one(contact_list, "layout-proportional-magnitude")
    assert f.severity == WARN
    assert "`size 600 395` with `vexactsize 0` asks for 395 parent heights" in f.message
    assert "vexactsize 1" in f.hint


def test_a_fully_proportional_size_of_one_is_clean():
    assert checks(wrap('  PanelWidgetClass P {\n   size 1 1\n   hexactsize 0\n   vexactsize 0\n  }\n')) == []


def test_a_pixel_sized_widget_with_the_matching_exact_flag_is_clean():
    assert checks(wrap('  WrapSpacerWidgetClass W {\n   size 600 395\n   hexactsize 1\n   vexactsize 1\n  }\n')) == []


def test_a_proportional_position_within_zero_to_one_is_clean():
    assert checks(wrap('  PanelWidgetClass P {\n   position 0.24 0.14\n   size 1 1\n   hexactpos 0\n   vexactpos 0\n  }\n')) == []


def test_a_duplicate_name_warns_with_the_first_line():
    text = wrap("  TextWidgetClass T {\n   size 1 1\n  }\n  TextWidgetClass T {\n   size 1 1\n  }\n")
    f = one(text, "layout-dup-name")
    assert f.severity == WARN and "line 4" in f.message


def test_a_scriptclass_without_a_prefix_warns_and_an_empty_one_does_not():
    assert one(wrap('  TextWidgetClass T {\n   size 1 1\n   scriptclass "Tabber"\n  }\n'), "layout-scriptclass-prefix").severity == WARN
    assert checks(wrap('  TextWidgetClass T {\n   size 1 1\n   scriptclass "OZ_Tabber"\n  }\n')) == []
    assert checks(wrap('  TextWidgetClass T {\n   size 1 1\n   scriptclass ""\n  }\n')) == []


def test_project_classes_can_be_allowed():
    text = wrap("  MyMapWidgetClass M {\n   size 1 1\n  }\n")
    assert ("layout-class", REFUSE) in checks(text)
    assert lint_layout(text, "a.layout", extra_classes=["MyMapWidgetClass"]) == []
