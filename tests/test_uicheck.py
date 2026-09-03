"""Checks over rectangles the ENGINE computed. Nothing here guesses layout;
the nodes are what ui_tree reports, and the rules are the owner's three
complaints plus the shapes a broken spacer takes."""
from dayz_mcp import uicheck
from dayz_mcp.layoutparse import parse_layout
from dayz_mcp.uicheck import ERROR, WARN, check


def node(path, cls, name, rect, shown=True, text_size=None):
    return {"path": path, "class": cls, "name": name, "visible": True, "shown": shown,
            "rect": rect, "depth": path.count(".") + (1 if path else 0), "text": "",
            "text_size": text_size}


HOST = (0, 0, 1000, 600)


def rules(issues):
    return sorted((i.rule, i.name) for i in issues)


def test_a_clean_tree_has_no_issues():
    nodes = [node("", "FrameWidget", "Root", "0 0 1000 600"),
             node("0", "PanelWidget", "Bg", "10 10 500 100"),
             node("0.0", "TextWidget", "Label", "20 20 200 30", text_size=(150, 24))]
    issues, notes = check(nodes, HOST)
    assert issues == []


def test_a_child_outside_its_parent_overflows():
    nodes = [node("", "FrameWidget", "Root", "0 0 1000 600"),
             node("0", "PanelWidget", "Card", "10 10 200 100"),
             node("0.0", "TextWidget", "Label", "150 20 200 30")]
    issues, _ = check(nodes, HOST)
    assert rules(issues) == [("overflow", "Label")]
    assert issues[0].severity == ERROR and issues[0].other == "0"


def test_tall_content_inside_a_scroll_widget_is_not_overflow():
    nodes = [node("", "FrameWidget", "Root", "0 0 1000 600"),
             node("0", "ScrollWidget", "Scroll", "10 10 300 200"),
             node("0.0", "PanelWidget", "Content", "10 10 300 900")]
    assert rules(check(nodes, HOST)[0]) == []


def test_two_content_siblings_that_cross_overlap_but_a_panel_behind_them_does_not():
    nodes = [node("", "FrameWidget", "Root", "0 0 1000 600"),
             node("0", "PanelWidget", "Bg", "0 0 400 100"),
             node("1", "TextWidget", "A", "10 10 100 30"),
             node("2", "TextWidget", "B", "100 20 100 30"),
             node("3", "TextWidget", "C", "109 60 100 30")]
    issues, _ = check(nodes, HOST)
    assert rules(issues) == [("overlap", "A")]
    assert issues[0].other == "2"


def test_text_wider_than_its_box_overflows():
    nodes = [node("", "FrameWidget", "Root", "0 0 1000 600"),
             node("0", "TextWidget", "Label", "10 10 100 20", text_size=(140, 20))]
    assert rules(check(nodes, HOST)[0]) == [("text_overflow", "Label")]


def test_zero_size_runaway_and_offhost():
    nodes = [node("", "FrameWidget", "Root", "0 0 1000 600"),
             node("0", "WrapSpacerWidget", "Empty", "10 10 300 0"),
             node("1", "WrapSpacerWidget", "Huge", "10 10 300 105613"),
             node("2", "TextWidget", "Gone", "2000 2000 50 20")]
    issues, _ = check(nodes, HOST)
    found = rules(issues)
    assert ("zero_size", "Empty") in found
    assert ("runaway", "Huge") in found
    assert ("offhost", "Gone") in found
    by_name = {i.name: i.severity for i in issues}
    assert by_name["Empty"] == WARN and by_name["Huge"] == ERROR and by_name["Gone"] == WARN


def test_hidden_nodes_are_ignored():
    nodes = [node("", "FrameWidget", "Root", "0 0 1000 600"),
             node("0", "TextWidget", "Gone", "2000 2000 50 20", shown=False)]
    assert check(nodes, HOST)[0] == []


def test_under_scrollbar_is_off_until_the_bar_is_measured(monkeypatch):
    nodes = [node("", "FrameWidget", "Root", "0 0 1000 600"),
             node("0", "ScrollWidget", "Scroll", "10 10 300 200"),
             node("0.0", "PanelWidget", "Content", "10 10 300 900"),
             node("0.0.0", "TextWidget", "Line", "10 10 300 20")]
    monkeypatch.setattr(uicheck, "SCROLLBAR_PX", None)
    issues, notes = check(nodes, HOST)
    assert rules(issues) == []
    assert any("under_scrollbar" in n for n in notes)
    monkeypatch.setattr(uicheck, "SCROLLBAR_PX", 16)
    issues, notes = check(nodes, HOST)
    assert rules(issues) == [("under_scrollbar", "Line")]


def test_a_bare_edit_box_needs_the_source_to_be_judged():
    layout = ("FrameWidgetClass Root {\n size 1 1\n {\n"
              "  EditBoxWidgetClass Bare {\n   size 100 20\n  }\n"
              "  EditBoxWidgetClass Styled {\n   size 100 20\n   style Default\n  }\n"
              "  PanelWidgetClass Frame {\n   size 120 30\n   style rover_sim_colorable\n  }\n"
              "  EditBoxWidgetClass Framed {\n   size 100 20\n  }\n }\n}\n")
    nodes = [node("", "FrameWidget", "Root", "0 0 1000 600"),
             node("0", "EditBoxWidget", "Bare", "10 10 100 20"),
             node("1", "EditBoxWidget", "Styled", "10 40 100 20"),
             node("2", "PanelWidget", "Frame", "5 95 120 30"),
             node("3", "EditBoxWidget", "Framed", "10 100 100 20")]
    issues, notes = check(nodes, HOST)
    assert rules(issues) == []
    assert any("editbox_bare" in n for n in notes)
    issues, _ = check(nodes, HOST, source=parse_layout(layout))
    assert rules(issues) == [("editbox_bare", "Bare")]
