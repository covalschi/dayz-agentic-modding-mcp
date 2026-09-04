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


def test_an_image_drawn_first_and_behind_a_control_is_not_an_overlap():
    """The device shell's bezel (an ImageWidget drawn first, covering the
    whole device) was reported as overlapping the close button drawn on top
    of it (measured on the first gallery run, 2026-09-03) -- a picture
    behind a control is background, not an overlap. Without the skip this
    pair crosses by 40x20 px, well past OVERLAP_MIN_PX, and would flag."""
    nodes = [node("", "FrameWidget", "Root", "0 0 1000 600"),
             node("1", "ImageWidget", "Bezel", "0 0 1000 600"),
             node("5", "ButtonWidget", "Close", "900 10 40 20")]
    assert check(nodes, HOST)[0] == []


def test_two_images_that_merely_cross_still_overlap():
    """The skip is for a picture BEHIND a control, not for "both nodes
    happen to be ImageWidget": two images side by side, nudged to cross,
    is a real layout collision -- neither contains the other."""
    nodes = [node("", "FrameWidget", "Root", "0 0 1000 600"),
             node("1", "ImageWidget", "Left", "0 0 100 100"),
             node("2", "ImageWidget", "Right", "90 10 100 100")]
    issues, _ = check(nodes, HOST)
    assert rules(issues) == [("overlap", "Left")]


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


def test_runaway_detail_names_a_proportional_flag_with_a_pixel_number():
    """The ContactList shape measured on the stand, 2026-09-03: `size 600
    395` with `vexactsize 0` is not 395 PIXELS tall, it is 395 PARENT
    heights -- the actual, statically-detectable cause of a spacer measured
    at 231148 px tall on the stand."""
    layout = 'FrameWidgetClass Root {\n size 1 1\n {\n  WrapSpacerWidgetClass ContactList {\n   size 600 395\n   hexactsize 1\n   vexactsize 0\n  }\n }\n}\n'
    nodes = [node("", "FrameWidget", "Root", "0 0 1000 600"),
             node("0", "WrapSpacerWidget", "ContactList", "10 10 300 231148")]
    issues, _ = check(nodes, HOST, source=parse_layout(layout))
    found = [i for i in issues if i.rule == "runaway"]
    assert len(found) == 1
    assert "`size 600 395` with `vexactsize 0` is 395 parent heights" in found[0].detail


def test_hidden_nodes_are_ignored():
    nodes = [node("", "FrameWidget", "Root", "0 0 1000 600"),
             node("0", "TextWidget", "Gone", "2000 2000 50 20", shown=False)]
    assert check(nodes, HOST)[0] == []


def test_under_scrollbar_uses_the_measured_bar_width_scaled():
    """F3: the scrollbar is 10 layout units wide -- 10 px at scale 1.0, 15 px
    (round(10 * 1.4815)) at 1600/1080. The SAME content right edge (87 px)
    clears the narrower bar but runs under the wider one, which is the point:
    the rule is not just "10 px", it is "10 layout units, scaled"."""
    def tree(right_edge):
        return [node("", "FrameWidget", "Root", "0 0 1000 600"),
                node("0", "ScrollWidget", "Scroll", "0 0 100 50"),
                node("0.0", "PanelWidget", "Content", "0 0 100 200"),
                node("0.0.0", "TextWidget", "Line", f"0 0 {right_edge} 20")]

    # scale 1.0: the bar is the last 10 px (limit 90) -- 85 stops clear of
    # it, 95 runs under it.
    assert check(tree(85), HOST, scale=1.0)[0] == []
    issues, _ = check(tree(95), HOST, scale=1.0)
    assert rules(issues) == [("under_scrollbar", "Line")]

    # scale 1.4815 (1600/1080): the bar widens to 15 px (limit 85) -- 85
    # still clears it, but 87 (which cleared the 10 px bar above) now runs
    # under the wider one.
    assert check(tree(85), HOST, scale=1.4815)[0] == []
    issues, _ = check(tree(87), HOST, scale=1.4815)
    assert rules(issues) == [("under_scrollbar", "Line")]


def test_a_bare_edit_box_needs_the_source_to_be_judged():
    """F4: Widget.ClassName() reports "Widget" for BOTH a FrameWidgetClass
    and a PanelWidgetClass -- the engine nodes below share that class on
    purpose, so a check that judged framing off the engine class alone could
    not tell Root and Frame apart. With a source layout, `check` decides by
    the SOURCE class instead: Root (FrameWidgetClass) does not frame Bare,
    but Frame (PanelWidgetClass) -- which HUGS Framed, 2 px of margin on
    every side -- does frame it, despite both reporting the same "Widget" to
    the engine."""
    layout = ("FrameWidgetClass Root {\n size 1 1\n {\n"
              "  EditBoxWidgetClass Bare {\n   size 100 20\n  }\n"
              "  EditBoxWidgetClass Styled {\n   size 100 20\n   style Default\n  }\n"
              "  PanelWidgetClass Frame {\n   size 104 24\n   style rover_sim_colorable\n  }\n"
              "  EditBoxWidgetClass Framed {\n   size 100 20\n  }\n }\n}\n")
    nodes = [node("", "Widget", "Root", "0 0 1000 600"),
             node("0", "EditBoxWidget", "Bare", "10 10 100 20"),
             node("1", "EditBoxWidget", "Styled", "10 40 100 20"),
             node("2", "Widget", "Frame", "8 98 104 24"),
             node("3", "EditBoxWidget", "Framed", "10 100 100 20")]
    issues, notes = check(nodes, HOST)
    assert rules(issues) == []
    assert any("editbox_bare" in n for n in notes)
    issues, _ = check(nodes, HOST, source=parse_layout(layout))
    assert rules(issues) == [("editbox_bare", "Bare")]


def test_a_page_sized_panel_is_not_a_frame_even_though_it_encloses_the_box():
    """F4/G3: every PDA edit box sits on the PAGE's own whole background
    panel. Untightened, the parent branch trusted the candidate's class
    alone and called any enclosing panel a frame -- so editbox_bare never
    fired on a real page. A panel far larger than the field it merely
    happens to contain is a page background, not a frame."""
    layout = 'FrameWidgetClass Root {\n size 1 1\n {\n  PanelWidgetClass PageBg {\n   position 0 0\n   size 1000 600\n   style rover_sim_colorable\n   {\n    EditBoxWidgetClass Field {\n     size 100 20\n    }\n   }\n  }\n }\n}\n'
    nodes = [node("", "FrameWidget", "Root", "0 0 1000 600"),
             node("0", "Widget", "PageBg", "0 0 1000 600"),
             node("0.0", "EditBoxWidget", "Field", "400 300 100 20")]
    issues, _ = check(nodes, HOST, source=parse_layout(layout))
    assert rules(issues) == [("editbox_bare", "Field")]


def test_a_panel_four_px_larger_on_every_side_hugs_and_frames():
    """FRAME_SLACK_UNITS is 6, so the allowed margin at scale 1.0 is
    round(6 * 1.0) + 1 = 7 px. A preceding sibling only 4 px larger than
    the box on every side is well within that -- a frame, not a page."""
    layout = 'FrameWidgetClass Root {\n size 1 1\n {\n  PanelWidgetClass Frame {\n   position 6 6\n   size 108 28\n   style rover_sim_colorable\n  }\n  EditBoxWidgetClass Field {\n   size 100 20\n  }\n }\n}\n'
    nodes = [node("", "FrameWidget", "Root", "0 0 1000 600"),
             node("0", "Widget", "Frame", "6 6 108 28"),
             node("1", "EditBoxWidget", "Field", "10 10 100 20")]
    issues, _ = check(nodes, HOST, source=parse_layout(layout))
    assert rules(issues) == []


def test_the_parent_panel_shortcut_obeys_the_same_slack():
    """The parent branch used to trust the candidate's class alone, with no
    tightness check at all -- exactly the bug in test_a_page_sized_panel
    above, which is the parent case failing to refuse a loose panel. This is
    its positive twin: a parent that HUGS the box (2 px margin on every
    side, same shape the sibling test above and the pre-existing
    test_a_bare_edit_box_needs_the_source_to_be_judged both use) still
    frames it directly, parent or not."""
    layout = 'FrameWidgetClass Root {\n size 1 1\n {\n  PanelWidgetClass Frame {\n   position 8 98\n   size 104 24\n   style rover_sim_colorable\n   {\n    EditBoxWidgetClass Field {\n     size 100 20\n    }\n   }\n  }\n }\n}\n'
    nodes = [node("", "FrameWidget", "Root", "0 0 1000 600"),
             node("0", "Widget", "Frame", "8 98 104 24"),
             node("0.0", "EditBoxWidget", "Field", "10 100 100 20")]
    issues, _ = check(nodes, HOST, source=parse_layout(layout))
    assert rules(issues) == []


def test_a_spacers_full_width_child_may_overhang_by_the_padding_scaled():
    """F5: a WrapSpacer's full-width (size 1) children overhang it by the
    padding (2 layout units, default) on the right -- an engine behaviour,
    not a layout bug. At scale 1.4815 that is round(2 * 1.4815) = 3 px."""
    nodes = [node("", "FrameWidget", "Root", "0 0 1000 600"),
             node("0", "WrapSpacerWidget", "Spacer", "10 10 300 100"),
             node("0.0", "PanelWidget", "Row", "10 10 303 20")]
    assert check(nodes, HOST, scale=1.4815)[0] == []


def test_a_border_panel_enclosing_its_parent_is_not_overflow():
    """F7: our layouts draw a button's border with a child panel 1 layout
    unit larger than its parent on every side (position -1 -1, size w+2
    h+2) -- the engine reports it poking out by 1-2 px on each side, and
    that is a border, not an overflowing child."""
    nodes = [node("", "FrameWidget", "Root", "0 0 1000 600"),
             node("0", "ButtonWidget", "Btn", "50 50 100 30"),
             node("0.0", "PanelWidget", "Border", "49 49 102 32")]
    assert check(nodes, HOST, scale=1.0)[0] == []


def test_overflow_beyond_the_border_and_spacer_tolerances_still_flags():
    """Neither new tolerance is a blank cheque: a child poking out by more
    than either one still overflows."""
    nodes = [node("", "FrameWidget", "Root", "0 0 1000 600"),
             node("0", "PanelWidget", "Card", "10 10 200 100"),
             node("0.0", "TextWidget", "Label", "10 10 205 20")]
    issues, _ = check(nodes, HOST, scale=1.0)
    assert rules(issues) == [("overflow", "Label")]


def test_text_overflow_trusts_a_self_sized_width_from_the_source():
    src = parse_layout('FrameWidgetClass Root {\n size 1 1\n {\n  TextWidgetClass Auto {\n   size 0 25\n   "size to text h" 1\n  }\n }\n}\n')
    nodes = [node("", "FrameWidget", "Root", "0 0 1000 600"),
             node("0", "TextWidget", "Auto", "10 10 174 34", text_size=(188, 34))]
    assert rules(check(nodes, HOST, source=src)[0]) == []


def test_text_overflow_judges_only_the_height_of_wrapped_text():
    src = parse_layout('FrameWidgetClass Root {\n size 1 1\n {\n  RichTextWidgetClass Body {\n   size 400 20\n   wrap 1\n   "size to text v" 1\n  }\n }\n}\n')
    nodes = [node("", "FrameWidget", "Root", "0 0 1000 600"),
             node("0", "RichTextWidget", "Body", "10 10 593 82", text_size=(636, 82))]
    assert rules(check(nodes, HOST, source=src)[0]) == []
    fixed = parse_layout('FrameWidgetClass Root {\n size 1 1\n {\n  RichTextWidgetClass Body {\n   size 400 20\n   wrap 1\n  }\n }\n}\n')
    nodes = [node("", "FrameWidget", "Root", "0 0 1000 600"),
             node("0", "RichTextWidget", "Body", "10 10 593 40", text_size=(636, 82))]
    assert rules(check(nodes, HOST, source=fixed)[0]) == [("text_overflow", "Body")]


def test_text_overflow_still_fires_without_flags():
    src = parse_layout('FrameWidgetClass Root {\n size 1 1\n {\n  TextWidgetClass T {\n   size 100 25\n  }\n }\n}\n')
    nodes = [node("", "FrameWidget", "Root", "0 0 1000 600"),
             node("0", "TextWidget", "T", "10 10 100 25", text_size=(150, 25))]
    assert rules(check(nodes, HOST, source=src)[0]) == [("text_overflow", "T")]


def test_a_fixture_row_is_judged_by_its_own_source_found_by_name():
    page = parse_layout('FrameWidgetClass Root {\n size 1 1\n {\n  WrapSpacerWidgetClass List {\n   size 1 0\n  }\n }\n}\n')
    row = parse_layout('WrapSpacerWidgetClass ChatLine {\n size 1 0\n {\n  RichTextWidgetClass LineText {\n   size 1 20\n   wrap 1\n   "size to text v" 1\n  }\n }\n}\n')
    nodes = [node("", "FrameWidget", "Root", "0 0 1000 600"),
             node("0", "WrapSpacerWidget", "List", "0 0 600 100"),
             node("0.0", "WrapSpacerWidget", "ChatLine", "0 0 600 80"),
             node("0.0.0", "RichTextWidget", "LineText", "0 20 590 52", text_size=(815, 52))]
    assert rules(check(nodes, HOST, source=page)[0]) == [("text_overflow", "LineText")]
    assert rules(check(nodes, HOST, source=page, sources=[row])[0]) == []


def test_two_fixture_rows_may_reuse_an_inner_name_without_sharing_flags():
    page = parse_layout('FrameWidgetClass Root {\n size 1 1\n {\n  WrapSpacerWidgetClass List {\n   size 1 0\n  }\n }\n}\n')
    wrapped = parse_layout('WrapSpacerWidgetClass RowA {\n size 1 0\n {\n  RichTextWidgetClass Label {\n   size 1 20\n   wrap 1\n   "size to text v" 1\n  }\n }\n}\n')
    fixed = parse_layout('FrameWidgetClass RowB {\n size 1 30\n {\n  TextWidgetClass Label {\n   size 100 25\n  }\n }\n}\n')
    nodes = [node("", "FrameWidget", "Root", "0 0 1000 600"),
             node("0", "WrapSpacerWidget", "List", "0 0 600 200"),
             node("0.0", "WrapSpacerWidget", "RowA", "0 0 600 80"),
             node("0.0.0", "RichTextWidget", "Label", "0 0 590 52", text_size=(815, 52)),
             node("0.1", "Widget", "RowB", "0 80 600 30"),
             node("0.1.0", "TextWidget", "Label", "0 80 100 25", text_size=(150, 25))]
    issues, _ = check(nodes, HOST, source=page, sources=[wrapped, fixed])
    assert rules(issues) == [("text_overflow", "Label")]
    assert issues[0].path == "0.1.0"


def test_two_templates_sharing_a_root_name_resolve_to_the_first_one_given():
    page = parse_layout('FrameWidgetClass Root {\n size 1 1\n {\n  WrapSpacerWidgetClass List {\n   size 1 0\n  }\n }\n}\n')
    first = parse_layout('WrapSpacerWidgetClass Row {\n size 1 0\n {\n  RichTextWidgetClass Label {\n   size 1 20\n   wrap 1\n   "size to text v" 1\n  }\n }\n}\n')
    second = parse_layout('FrameWidgetClass Row {\n size 1 30\n {\n  TextWidgetClass Label {\n   size 100 25\n  }\n }\n}\n')
    nodes = [node("", "FrameWidget", "Root", "0 0 1000 600"),
             node("0", "WrapSpacerWidget", "List", "0 0 600 200"),
             node("0.0", "WrapSpacerWidget", "Row", "0 0 600 80"),
             node("0.0.0", "RichTextWidget", "Label", "0 0 590 52", text_size=(815, 52))]
    assert rules(check(nodes, HOST, source=page, sources=[first, second])[0]) == []
    assert rules(check(nodes, HOST, source=page, sources=[second, first])[0]) == [("text_overflow", "Label")]


def test_an_unscoped_bare_name_prefers_any_flag_over_the_order_sources_were_given():
    """Unlike a shared ROOT name (the test above, which stays scoped to one
    template on purpose), a node whose ancestors match no known root at all
    has no scope to trust -- exactly what live=True's `sources` gives (task
    32): the WHOLE project's layouts, thrown together with no page/row
    structure telling `check` which one describes THIS widget. There the
    order `sources` happened to be given is not a reliable signal, so the
    flag wins over it: self-sized if ANY layout declaring this name says
    so, regardless of which one sorted first."""
    unflagged = parse_layout('TextWidgetClass RowMeta {\n size 100 20\n}\n')
    flagged = parse_layout('TextWidgetClass RowMeta {\n size 100 20\n "size to text h" 1\n}\n')
    nodes = [node("", "FrameWidget", "Menu", "0 0 1000 600"),
             node("0", "Widget", "SomeContainer", "0 0 1000 600"),
             node("0.0", "TextWidget", "RowMeta", "10 10 100 20", text_size=(109, 20))]
    assert rules(check(nodes, HOST, sources=[unflagged, flagged])[0]) == []
    assert rules(check(nodes, HOST, sources=[flagged, unflagged])[0]) == []


def test_a_fixture_rows_frame_candidate_is_read_from_the_same_template():
    """An edit box inside a fixture row is judged by its own template
    (`src_of`), but its framing CANDIDATE was still looked up by path in the
    page's source alone -- absent there, so it fell back to the engine's own
    class name, which reports "Widget" for a frame and a panel alike (F4).
    A row whose box sits in a FrameWidgetClass slot therefore counted as
    framed. The candidate now resolves the same way the box does."""
    page = parse_layout('FrameWidgetClass Page {\n size 1 1\n {\n  WrapSpacerWidgetClass List {\n   size 1 0\n  }\n }\n}\n')
    row = parse_layout("FrameWidgetClass Row {\n size 1 30\n {\n  FrameWidgetClass Slot {\n   size 104 24\n"
                       "   {\n    EditBoxWidgetClass Box {\n     size 100 20\n    }\n   }\n  }\n }\n}\n")
    nodes = [node("", "FrameWidget", "Page", "0 0 1000 600"),
             node("0", "WrapSpacerWidget", "List", "0 0 1000 100"),
             node("0.0", "Widget", "Row", "0 0 1000 30"),
             node("0.0.0", "Widget", "Slot", "10 5 104 24"),
             node("0.0.0.0", "EditBoxWidget", "Box", "12 7 100 20")]
    assert rules(check(nodes, HOST, source=page, sources=[row])[0]) == [("editbox_bare", "Box")]
    # The same shape with a PANEL slot is framed, which is what proves the
    # template is being read rather than everything being called bare.
    panelled = parse_layout("FrameWidgetClass Row {\n size 1 30\n {\n  PanelWidgetClass Slot {\n   size 104 24\n"
                            "   {\n    EditBoxWidgetClass Box {\n     size 100 20\n    }\n   }\n  }\n }\n}\n")
    assert rules(check(nodes, HOST, source=page, sources=[panelled])[0]) == []
