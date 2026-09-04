"""JSON descriptions -> .layout text. The bar: every idiom the generator
writes is one the engine was MEASURED to lay out (gui-layouts.md §24), every
number it derives is derived once, and nothing it writes trips lint_layout."""
import json

import pytest

from dayz_mcp import layoutgen
from dayz_mcp.layoutgen import LayoutGenError, Tokens, fmt

TOKENS = {
    "color": {"screen": [0.055, 0.075, 0.095, 1], "panel": [0.10, 0.135, 0.17, 1],
              "raised": [0.135, 0.18, 0.225, 1], "edge": [0.22, 0.30, 0.38, 1],
              "text": [0.90, 0.93, 0.95, 1], "muted": [0.58, 0.65, 0.71, 1],
              "faint": [0.37, 0.45, 0.52, 1], "accent": [0.31, 0.71, 0.91, 1],
              "alert": [0.94, 0.54, 0.14, 1], "rule": [1, 1, 1, 0.08],
              "pick": [0.31, 0.71, 0.91, 0.18], "none": [0, 0, 0, 0], "white": [1, 1, 1, 1]},
    "font": {"title": {"size": 18}, "header": {"size": 16}, "body": {"size": 15},
             "hint": {"size": 14}, "small": {"size": 13}, "tiny": {"size": 10},
             "field": {"face": "gui/fonts/MetronBook14", "size": 14, "fixed": True}},
    "space": {"page": 20, "gap": 10, "tight": 6},
    "size": {"button": 30, "field": 28, "header": 34, "hint": 22, "contactRow": 55, "bar": 10},
    "device": {"page": [1306, 518], "iconset": "my_icons", "rail": 60},
}


def tokens() -> Tokens:
    return Tokens.from_text(json.dumps(TOKENS), "ui/tokens.json")


def test_fmt_writes_numbers_the_way_vanilla_layouts_do():
    assert fmt(30) == "30" and fmt(30.0) == "30" and fmt(0.918) == "0.918"
    assert fmt(0.5) == "0.5" and fmt(1 / 3) == "0.333"


def test_tokens_resolve_numbers_colors_and_fonts():
    t = tokens()
    assert t.number("$space.page", "f", "n") == 20.0
    assert t.number("$size.button", "f", "n") == 30.0
    assert t.number(12, "f", "n") == 12.0
    rgba, from_token = t.color_of("$accent", "f", "n")
    assert rgba == [0.31, 0.71, 0.91, 1.0] and from_token
    rgba, from_token = t.color_of([1, 0, 0, 1], "f", "n")
    assert rgba == [1.0, 0.0, 0.0, 1.0] and not from_token
    font, from_token = t.font_of("header", "f", "n")
    assert font == {"face": layoutgen.FONT_FACE_DEFAULT, "size": 16.0, "fixed": False} and from_token
    assert t.font_of("field", "f", "n")[0]["fixed"] is True
    assert t.pair("$device.page", "f", "n") == (1306.0, 518.0)
    assert t.pair([640, "$size.contactRow"], "f", "n") == (640.0, 55.0)


def test_device_group_scalars_resolve_and_shapes_refuse_each_other():
    """A `$device.<name>` token can hold either shape. `number()` used to
    know only `space`/`size`, so a scalar device token (a rail width, say)
    never reached it, and there was no dedicated message for a pair used
    where a scalar is expected, or a scalar used where a pair is expected."""
    t = tokens()
    assert t.number("$device.rail", "f", "n") == 60.0
    assert t.number("$size.header", "f", "n") == 34.0
    with pytest.raises(LayoutGenError, match=r"is a pair, not a number"):
        t.number("$device.page", "f", "n")
    with pytest.raises(LayoutGenError, match=r"is a number, not a pair"):
        t.pair("$device.rail", "f", "n")


def test_a_present_but_wrong_shaped_device_token_says_so_not_unknown():
    """`$device.iconset` (a string, `_iconset`'s own reader) used where a
    number is expected used to fall through to "unknown token" -- the
    generic message for a name absent from the group entirely -- even though
    the token IS present. Since `8ae65d9` made `device` heterogeneous
    (pairs, scalars AND strings), the fall-through case needed its own
    message naming the actual problem: the right shape, not a missing name."""
    t = tokens()
    with pytest.raises(LayoutGenError, match=r"'\$device\.iconset' is not a number"):
        t.number("$device.iconset", "f", "n")


@pytest.mark.parametrize("value, what", [
    ("$space.nope", "unknown token"), ("$nope.page", "unknown token"),
    ("fill", "must be a number"), (True, "not a bool"),
])
def test_a_bad_number_names_the_file_the_node_and_the_reason(value, what):
    with pytest.raises(LayoutGenError) as caught:
        tokens().number(value, "ui/x.json", "root.0", "w")
    assert what in str(caught.value) and "ui/x.json root.0" in str(caught.value)


def test_a_bad_color_or_font_token_is_refused():
    with pytest.raises(LayoutGenError, match="unknown color token"):
        tokens().color_of("$mauve", "f", "n")
    with pytest.raises(LayoutGenError, match="unknown font token"):
        tokens().font_of("mono", "f", "n")


def test_tokens_file_is_validated_shape_by_shape():
    with pytest.raises(LayoutGenError, match="color.bad must be four numbers 0..1"):
        Tokens.from_text('{"color": {"bad": [1, 2]}}', "ui/tokens.json")
    with pytest.raises(LayoutGenError, match="font.h must be"):
        Tokens.from_text('{"font": {"h": "big"}}', "ui/tokens.json")
    with pytest.raises(LayoutGenError, match="space.page must be a number"):
        Tokens.from_text('{"space": {"page": "20"}}', "ui/tokens.json")
    with pytest.raises(LayoutGenError, match="do not parse"):
        Tokens.from_text("{", "ui/tokens.json")


from dayz_mcp.layoutgen import build_layout
from dayz_mcp.layoutlint import lint_layout


def page(body, root=None, layout="oz_page", note=""):
    return {"layout": layout, "note": note,
            "root": root or {"frame": {"name": "MyPage", "size": [640, 518], "inset": "$space.page"}},
            "body": body}


def build(desc):
    return build_layout(desc, tokens(), "ui/MyMod/oz_page.json", "MyMod/gui/layouts")


def clean(text: str) -> list:
    return [(f.check, f.message) for f in lint_layout(text, "gen.layout")]


def test_a_frame_root_with_one_label_renders_the_vanilla_shape():
    out = build(page({"label": {"name": "Title", "text": "Hello", "font": "header", "color": "$accent",
                                "w": 300, "h": 26}}, note="A page."))
    assert list(out.files) == ["MyMod/gui/layouts/oz_page.layout"]
    text = out.files["MyMod/gui/layouts/oz_page.layout"]
    assert text.splitlines()[0] == "// GENERATED by dayz-mcp layout_build from ui/MyMod/oz_page.json -- edit the source, not this file."
    assert text.splitlines()[1] == "// A page."
    assert text.splitlines()[2] == "FrameWidgetClass MyPage {"
    assert (
        "  TextWidgetClass Title {\n"
        "   visible 1\n"
        "   ignorepointer 1\n"
        "   position 20 20\n"
        "   size 300 26\n"
        "   hexactpos 1\n"
        "   vexactpos 1\n"
        "   hexactsize 1\n"
        "   vexactsize 1\n"
        "   color 0.31 0.71 0.91 1\n"
        "   priority 0\n"
        '   text "Hello"\n'
        '   font "gui/fonts/sdf_MetronBook24"\n'
        '   "exact text" 1\n'
        '   "exact text size" 16\n'
        '   "text halign" left\n'
        '   "text valign" center\n'
        "  }\n"
    ) in text
    assert clean(text) == []
    assert out.notes == []


def test_frame_w_and_a_size_element_resolve_a_device_scalar():
    """`"w": "$device.rail"` used to raise "unknown token": `number()` never
    consulted the device group, only `pair()` did, and only for the whole
    `size`. A `size` element goes through `number()` too, so it had the
    same gap."""
    out = build(page({"frame": {"name": "Rail", "w": "$device.rail", "h": 40}}))
    text = out.files["MyMod/gui/layouts/oz_page.layout"]
    assert "FrameWidgetClass Rail {\n   visible 1\n   position 20 20\n   size 60 40\n" in text
    assert clean(text) == []
    out = build(page({"frame": {"name": "Rail", "size": ["$device.rail", 40]}}))
    text = out.files["MyMod/gui/layouts/oz_page.layout"]
    assert "FrameWidgetClass Rail {\n   visible 1\n   position 20 20\n   size 60 40\n" in text
    assert clean(text) == []


def test_root_props_inset_and_children_offsets():
    out = build(page({"panel": {"name": "Bg", "size": "fill", "color": "$panel"}}))
    text = out.files["MyMod/gui/layouts/oz_page.layout"]
    assert " visible 1\n position 0 0\n size 640 518\n hexactpos 1\n vexactpos 1\n hexactsize 1\n vexactsize 1\n" in text
    # size "fill" at the inner origin is the full inner box, exact
    assert "  PanelWidgetClass Bg {\n   visible 1\n   ignorepointer 1\n   position 20 20\n   size 600 478\n" in text
    assert "   style rover_sim_colorable\n" in text


NO_INSET_ROOT = {"frame": {"name": "MyPage", "size": [640, 518]}}


def test_a_proportional_panel_insets_an_exact_width_child():
    """`Bg` is `size: "fill"` at the box's own (uninset) origin -- the "full"
    shortcut -- so IT is written proportional (`size 1 1`). Its OWN `inset`
    must still apply to ITS children exactly as the exact-size path already
    does: `Card` (an ordinary declared size) lands at `inset, inset` with its
    width unchanged, not at `0, 0`."""
    out = build(page({"panel": {"name": "Bg", "size": "fill", "inset": 20, "children": [
        {"panel": {"name": "Card", "size": [100, 50], "color": "$panel"}},
    ]}}, root=NO_INSET_ROOT))
    text = out.files["MyMod/gui/layouts/oz_page.layout"]
    assert "  PanelWidgetClass Bg {\n   visible 1\n   ignorepointer 1\n   position 0 0\n   size 1 1\n   hexactpos 1\n   vexactpos 1\n   hexactsize 0\n   vexactsize 0\n" in text
    assert "    PanelWidgetClass Card {\n     visible 1\n     ignorepointer 1\n     position 20 20\n     size 100 50\n     hexactpos 1\n     vexactpos 1\n     hexactsize 1\n     vexactsize 1\n" in text
    assert clean(text) == []


def test_a_proportional_panels_fill_child_is_reduced_by_the_inset_on_both_sides():
    """`Bg` renders proportional, but the ROOM it hands its children is the
    real (unit) width the enclosing frame gave it -- carried by `box.w` even
    though `Bg`'s own `size` line never spells it out -- minus the inset on
    both sides: 640 - 2*20 = 600, exactly what the exact-size path computes
    for the same panel with a declared `size`."""
    out = build(page({"panel": {"name": "Bg", "size": "fill", "inset": 20, "children": [
        {"panel": {"name": "Inner", "w": "fill", "h": 30, "color": "$panel"}},
    ]}}, root=NO_INSET_ROOT))
    text = out.files["MyMod/gui/layouts/oz_page.layout"]
    assert "    PanelWidgetClass Inner {\n     visible 1\n     ignorepointer 1\n     position 20 20\n     size 600 30\n     hexactpos 1\n     vexactpos 1\n     hexactsize 1\n     vexactsize 1\n" in text
    assert clean(text) == []


def test_a_proportional_frames_fill_child_is_reduced_by_the_inset_on_both_sides():
    """The frame twin of the panel test above. `_b_frame` carried the exact
    defect `ad310af` fixed in `_b_panel` one commit earlier: `inner_w = width
    - 2 * inset if hx else box.w` never subtracted the inset on the
    proportional path at all, so a `w: "fill"` child of an inset
    proportional frame overflowed the frame's own right edge by the inset on
    each side (a byte-identical description with `panel` was already
    correct)."""
    out = build(page({"frame": {"name": "Bg", "size": "fill", "inset": 20, "children": [
        {"panel": {"name": "Inner", "w": "fill", "h": 30, "color": "$panel"}},
    ]}}, root=NO_INSET_ROOT))
    text = out.files["MyMod/gui/layouts/oz_page.layout"]
    assert "    PanelWidgetClass Inner {\n     visible 1\n     ignorepointer 1\n     position 20 20\n     size 600 30\n     hexactpos 1\n     vexactpos 1\n     hexactsize 1\n     vexactsize 1\n" in text
    assert clean(text) == []


@pytest.mark.parametrize("kind", ["panel", "frame"])
def test_an_inset_proportional_containers_fill_child_is_reduced_on_the_vertical_axis_too(kind):
    """Every case above only ever gives `Inner` a `w: "fill"` -- its `h` is
    always a fixed 30 -- so `_inset_extent`'s HEIGHT argument is computed but
    never actually consumed by a child that fills it. A root of unequal
    width and height (640x518) makes a height derived from the wrong axis
    (or not reduced by the inset at all) read as a different, wrong number
    instead of coincidentally matching the width case."""
    out = build(page({kind: {"name": "Bg", "size": "fill", "inset": 20, "children": [
        {"panel": {"name": "Inner", "w": 50, "h": "fill", "color": "$panel"}},
    ]}}, root=NO_INSET_ROOT))
    text = out.files["MyMod/gui/layouts/oz_page.layout"]
    assert "    PanelWidgetClass Inner {\n     visible 1\n     ignorepointer 1\n     position 20 20\n     size 50 478\n     hexactpos 1\n     vexactpos 1\n     hexactsize 1\n     vexactsize 1\n" in text
    assert clean(text) == []


def test_a_proportional_panel_with_an_inset_needs_a_known_width():
    """Under a `screen` root nobody knows the real resolution ahead of time
    -- the box handed down is genuinely `Box(None, None)` -- so a
    proportional panel cannot subtract its inset from an unknown width.
    Silently keeping the un-reduced (unknown-turned-`None`) box would either
    crash somewhere unrelated downstream or -- once a width IS eventually
    known some other way -- overflow the panel's own edge by `inset`; this
    refuses at the point of the actual problem instead."""
    with pytest.raises(LayoutGenError, match="a proportional panel or frame with an inset needs a known width"):
        build_layout({"layout": "x", "root": {"frame": {"name": "R", "size": "screen", "children": [
            {"panel": {"name": "Bg", "size": "fill", "inset": 10, "children": [
                {"panel": {"name": "Card", "size": [50, 50], "color": "$panel"}}]}}]}}},
            tokens(), "ui/MyMod/x.json", "MyMod/gui/layouts")


def test_a_proportional_frame_with_an_inset_also_needs_a_known_width():
    """`_b_frame` used to skip `_inset_extent` entirely (it did its own
    inline, unguarded arithmetic), so this exact shape raised a different,
    less specific error -- `w: fill needs an exact ancestor to fill` --
    instead of naming the inset as the actual reason. Same mistake as the
    panel case above, silently handled two different ways."""
    with pytest.raises(LayoutGenError, match="a proportional panel or frame with an inset needs a known width"):
        build_layout({"layout": "x", "root": {"frame": {"name": "R", "size": "screen", "children": [
            {"frame": {"name": "Bg", "size": "fill", "inset": 10, "children": [
                {"panel": {"name": "Card", "size": [50, 50], "color": "$panel"}}]}}]}}},
            tokens(), "ui/MyMod/x.json", "MyMod/gui/layouts")


def test_a_label_can_size_itself_and_anchor_right():
    out = build(page({"label": {"name": "Where", "anchor": "right", "at": [12, 6], "w": "auto", "h": 25,
                                "text": "nearby", "font": "small", "color": "$muted", "align": "right"}}))
    text = out.files["MyMod/gui/layouts/oz_page.layout"]
    assert "   halign right_ref\n   position 32 26\n   size 0 25\n" in text
    assert '   "size to text h" 1\n' in text and '   "text halign" right\n' in text
    assert clean(text) == []


def test_rule_chip_and_hidden_widgets():
    out = build(page({"frame": {"name": "Card", "at": [0, 0], "size": [300, 55], "children": [
        {"chip": {"name": "Chip"}},
        {"rule": {"name": "Line", "anchor": "bottom", "at": [22, 0], "w": "fill"}},
        {"panel": {"name": "Pick", "size": "fill", "color": "$pick", "hidden": True}},
    ]}}))
    text = out.files["MyMod/gui/layouts/oz_page.layout"]
    assert "PanelWidgetClass Chip {\n     visible 0\n     ignorepointer 1\n     position 0 0\n     size 4 1\n     hexactpos 1\n     vexactpos 1\n     hexactsize 1\n     vexactsize 0\n     color 1 1 1 1\n     priority 0\n" in text
    assert "PanelWidgetClass Line {\n     visible 1\n     ignorepointer 1\n     valign bottom_ref\n     position 22 0\n     size 278 1\n" in text
    assert "PanelWidgetClass Pick {\n     visible 0\n" in text and "     priority 2\n" in text
    assert clean(text) == []


@pytest.mark.parametrize("body", [
    {"vbox": {"hidden": True, "children": [{"label": {"name": "A", "h": 20}}]}},
    {"hbox": {"h": 20, "hidden": True, "children": [{"label": {"name": "A", "w": 20}}]}},
    {"header": {"title": {"name": "H"}, "hidden": True}},
])
def test_hidden_on_a_nameless_container_is_refused(body):
    """`_stackbox` only builds a wrapper widget (something with a `visible`
    line) when the node has a `name`; a nameless one is flattened straight
    into its parent and `hidden` is read nowhere else. `COMMON` grants
    `hidden` to every primitive including these, so the schema promised
    something the builder could not actually deliver -- it used to render a
    fully visible container with no note at all. `header` reaches the same
    code through `_stackbox` too (an unnamed row), so the same case applies
    to it without a name of its own."""
    with pytest.raises(LayoutGenError, match="hidden needs a name -- a nameless container is not a widget"):
        build(page(body))


def test_hidden_on_a_named_vbox_still_works():
    out = build(page({"vbox": {"name": "Column", "hidden": True, "children": [
        {"label": {"name": "A", "h": 20}}]}}))
    text = out.files["MyMod/gui/layouts/oz_page.layout"]
    assert "FrameWidgetClass Column {\n   visible 0\n" in text
    assert clean(text) == []


def test_at_on_a_page_child_is_noted_but_hidden_overlays_are_not():
    out = build(page({"frame": {"name": "Wrap", "size": [100, 100], "children": [
        {"panel": {"name": "A", "at": [5, 5], "size": [10, 10], "color": "$panel"}},
        {"panel": {"name": "Over", "at": [5, 5], "size": [10, 10], "color": "$panel", "hidden": True}},
    ]}}))
    assert out.notes == []
    out = build(page({"panel": {"name": "Loose", "at": [5, 5], "size": [10, 10], "color": "$panel"}}))
    assert out.notes == ["ui/MyMod/oz_page.json root.0: `at` on a page child -- build the page with vbox/hbox"]


@pytest.mark.parametrize("desc, message", [
    ({"layout": "x", "root": {"frame": {"name": "R", "size": [1, 1]}},
      "body": {"label": {"name": "A", "h": 20}, "panel": {}}}, "exactly one primitive key"),
    ({"layout": "x", "root": {"frame": {"name": "R", "size": [1, 1]}},
      "body": {"sparkle": {"name": "A"}}}, "unknown primitive 'sparkle'"),
    ({"layout": "x", "root": {"frame": {"name": "R", "size": [1, 1]}},
      "body": {"label": {"name": "A", "h": 20, "bold": 1}}}, "label does not take ['bold']"),
    ({"layout": "x", "root": {"frame": {"name": "R", "size": [100, 100]}},
      "body": {"frame": {"name": "F", "size": [50, 50], "children": [
          {"panel": {"name": "Dup", "size": [1, 1], "color": "$panel"}},
          {"panel": {"name": "Dup", "size": [1, 1], "color": "$panel"}}]}}}, "duplicate widget name 'Dup'"),
    ({"layout": "x", "root": {"frame": {"name": "R", "size": [100, 100]}},
      "body": {"label": {"name": "A", "h": 20, "text": 'say "hi"'}}}, "quote inside text"),
    ({"layout": "x", "root": {"label": {"name": "R"}}}, "root must be a frame, a panel or a button"),
    ({"layout": "x", "root": {"frame": {"name": "R"}}}, "root needs an exact size"),
    ({"layout": "x", "root": {"frame": {"name": "R", "size": [100, 100]}},
      "body": {"label": {"name": "A", "h": "auto"}}}, "auto is only a label's width"),
    # `size` used to win over a sibling `w`/`h` without a word, so a node
    # carrying both was read one way by the container and another by the leaf.
    ({"layout": "x", "root": {"frame": {"name": "R", "size": [100, 100]}},
      "body": {"label": {"name": "A", "size": [10, 10], "h": 20}}}, "size and w/h on one node -- use one"),
    # A row template lives in a list's `rows`, never as a node of the page:
    # `ALLOWED["row"]` admitted it here and then it died on "not implemented".
    ({"layout": "x", "root": {"frame": {"name": "R", "size": [100, 100]}},
      "body": {"row": {"name": "A", "h": 20}}}, "unknown primitive 'row'"),
])
def test_refusals_name_the_node(desc, message):
    with pytest.raises(LayoutGenError) as caught:
        build_layout(desc, tokens(), "ui/MyMod/x.json", "MyMod/gui/layouts")
    assert message in str(caught.value) and "ui/MyMod/x.json" in str(caught.value)


def test_a_root_can_anchor_and_offset_itself():
    """The root's own `anchor` and `at` were accepted and then dropped: the
    page was always placed at 0 0. Phase C centres the settings window with
    an anchor instead of a proportional position, so both are applied."""
    out = build(page({"label": {"name": "A", "h": 20}},
                     root={"frame": {"name": "Win", "size": [640, 518], "anchor": "center"}}))
    text = out.files["MyMod/gui/layouts/oz_page.layout"]
    assert "FrameWidgetClass Win {\n visible 1\n halign center_ref\n valign center_ref\n position 0 0\n size 640 518\n" in text
    assert clean(text) == []
    out = build(page({"label": {"name": "A", "h": 20}},
                     root={"frame": {"name": "Win", "size": [640, 518], "at": [40, 30]}}))
    assert "FrameWidgetClass Win {\n visible 1\n position 40 30\n size 640 518\n" in out.files["MyMod/gui/layouts/oz_page.layout"]


def test_a_chip_says_it_ignores_h():
    out = build(page({"frame": {"name": "Row", "size": [300, 40], "children": [
        {"chip": {"name": "Chip", "h": 10}}]}}))
    assert out.notes == ["ui/MyMod/oz_page.json root.0.0: chip ignores h -- it is its parent's full height"]


def test_a_click_rows_color_is_noted_as_unpainted():
    out = build(page({"list": {"name": "S", "stack": "L", "size": [200, 100], "rows": {
        "r": {"row": {"name": "R", "h": 30, "click": True, "color": "$panel"}}}}}))
    assert out.notes == ["ui/MyMod/oz_page.json rows.r: row color is only painted on a plain (non-click, non-stack) row"]


def test_a_grid_refuses_more_children_than_cells():
    with pytest.raises(LayoutGenError, match="grid holds 1 cells, 2 children given"):
        build(page({"grid": {"name": "G", "size": [100, 100], "cols": 1, "rows": 1, "children": [
            {"panel": {"name": "A", "color": "$panel"}}, {"panel": {"name": "B", "color": "$panel"}}]}}))


def test_unnamed_hrows_are_numbered_by_their_own_counter():
    """The auto-name counted the widget names already claimed that started
    with "HRow", so a widget called HRowFoo earlier in the page renamed every
    anonymous row after it -- churn in a committed artifact, from an edit
    that has nothing to do with those rows."""
    out = build(page({"frame": {"name": "HRowFoo", "size": [300, 100], "children": [
        {"hrow": {"h": 20, "children": [{"label": {"name": "A", "w": "auto", "font": "small"}}]}},
        {"hrow": {"at": [0, 40], "h": 20, "children": [{"label": {"name": "B", "w": "auto", "font": "small"}}]}},
    ]}}))
    text = out.files["MyMod/gui/layouts/oz_page.layout"]
    assert "GridSpacerWidgetClass HRow1 {" in text and "GridSpacerWidgetClass HRow2 {" in text
    assert "HRow3" not in text
    assert clean(text) == []


def test_literal_colors_and_fonts_are_allowed_but_noted():
    out = build(page({"label": {"name": "A", "h": 20, "color": [1, 0, 0, 1], "font": {"size": 12}}}))
    assert out.notes == [
        "ui/MyMod/oz_page.json root.0: color given as a literal -- use a $color token",
        "ui/MyMod/oz_page.json root.0: font given as a literal -- use a $font token",
    ]


def test_a_gap_keeps_its_anchor():
    out = build(page({"frame": {"name": "Row", "size": [300, 40], "children": [
        {"gap": {"anchor": "right", "at": [10, 0], "w": 20, "h": 5}}]}}))
    text = out.files["MyMod/gui/layouts/oz_page.layout"]
    assert "PanelWidgetClass Gap1 {\n     visible 1\n     ignorepointer 1\n     halign right_ref\n     position 10 0\n     size 20 5\n" in text
    assert clean(text) == []


def test_vbox_places_children_and_resolves_one_fill():
    out = build(page({"vbox": {"gap": "$space.tight", "children": [
        {"label": {"name": "Header", "h": "$size.header", "font": "title", "color": "$accent"}},
        {"panel": {"name": "Body", "h": "fill", "color": "$panel"}},
        {"label": {"name": "Hint", "h": "$size.hint", "font": "hint", "color": "$faint"}},
        {"hbox": {"h": "$size.button", "gap": 30, "children": [
            {"button": {"name": "BtnMsg", "w": "fill", "hidden": True}},
            {"button": {"name": "BtnFriend", "w": "fill"}},
        ]}},
    ]}}))
    text = out.files["MyMod/gui/layouts/oz_page.layout"]
    assert "TextWidgetClass Header {\n   visible 1\n   ignorepointer 1\n   position 20 20\n   size 600 34\n" in text
    # 478 inner - 34 - 22 - 30 - three gaps of 6 = 374
    assert "PanelWidgetClass Body {\n   visible 1\n   ignorepointer 1\n   position 20 60\n   size 600 374\n" in text
    assert "TextWidgetClass Hint {\n   visible 1\n   ignorepointer 1\n   position 20 440\n   size 600 22\n" in text
    assert "ButtonWidgetClass BtnMsg {\n   visible 0\n   position 20 468\n   size 285 30\n" in text
    assert "ButtonWidgetClass BtnFriend {\n   visible 1\n   position 335 468\n   size 285 30\n" in text
    assert clean(text) == [] and out.notes == []


def test_a_named_vbox_is_a_frame_with_relative_children():
    out = build(page({"vbox": {"name": "Column", "w": 300, "h": 100, "children": [
        {"label": {"name": "A", "h": 20}}, {"label": {"name": "B", "h": 20}}]}}))
    text = out.files["MyMod/gui/layouts/oz_page.layout"]
    assert "  FrameWidgetClass Column {\n   visible 1\n   position 20 20\n   size 300 100\n" in text
    assert "    TextWidgetClass A {\n     visible 1\n     ignorepointer 1\n     position 0 0\n     size 300 20\n" in text
    assert "    TextWidgetClass B {\n     visible 1\n     ignorepointer 1\n     position 0 20\n     size 300 20\n" in text


def test_several_fills_share_the_remainder_equally():
    out = build(page({"vbox": {"children": [{"panel": {"name": "A", "h": "fill", "color": "$panel"}},
                                            {"panel": {"name": "B", "h": 78, "color": "$panel"}},
                                            {"panel": {"name": "C", "h": "fill", "color": "$panel"}}]}}))
    text = out.files["MyMod/gui/layouts/oz_page.layout"]
    assert "PanelWidgetClass A {\n   visible 1\n   ignorepointer 1\n   position 20 20\n   size 600 200\n" in text
    assert "PanelWidgetClass C {\n   visible 1\n   ignorepointer 1\n   position 20 298\n   size 600 200\n" in text


@pytest.mark.parametrize("body, message", [
    ({"vbox": {"children": [{"panel": {"name": "A", "h": 400, "color": "$panel"}},
                            {"panel": {"name": "B", "h": 400, "color": "$panel"}}]}}, "does not fit: needs 800, has 478"),
    ({"vbox": {"children": [{"panel": {"name": "A", "at": [1, 1], "h": 40, "color": "$panel"}}]}}, "`at` is not allowed under a vbox/hbox"),
    # An anchor changes what the container's own coordinate MEANS: the vbox
    # computed "40 down from the top" and `bottom` makes the engine read it
    # as "40 up from the bottom", moving the child a screen away from where
    # the column put it while its siblings stay. Same class of mistake as
    # `at`, and refused the same way.
    ({"vbox": {"children": [{"panel": {"name": "A", "anchor": "bottom", "h": 40, "color": "$panel"}}]}}, "`anchor` is not allowed under a vbox/hbox"),
    ({"hbox": {"h": 30, "children": [{"label": {"name": "A", "text": "x"}}]}}, "w is required here"),
    # A header's default is a ROW's height ($size.header) -- an hbox needs
    # a default WIDTH instead, which no single token stands for, so a
    # header inside an hbox keeps demanding an explicit w, same as before.
    ({"hbox": {"h": 30, "children": [{"header": {"title": {"name": "H"}}}]}}, "w is required here"),
    ({"vbox": {"children": [{"text": {"name": "T", "text": "x"}}]}}, "text needs h inside a vbox"),
])
def test_vbox_and_hbox_refusals(body, message):
    with pytest.raises(LayoutGenError, match=message):
        build(page(body))


def test_button_writes_the_edge_bg_text_idiom():
    out = build(page({"button": {"name": "BtnHide", "size": [195, 30], "text": "#STR_HIDE", "font": "small"}}))
    text = out.files["MyMod/gui/layouts/oz_page.layout"]
    assert (
        "  ButtonWidgetClass BtnHide {\n   visible 1\n   position 20 20\n   size 195 30\n"
        "   hexactpos 1\n   vexactpos 1\n   hexactsize 1\n   vexactsize 1\n   text \"\"\n   priority 0\n   {\n"
        "    PanelWidgetClass BtnHideEdge {\n     visible 1\n     ignorepointer 1\n     position -1 -1\n     size 197 32\n"
    ) in text
    assert "    PanelWidgetClass BtnHideBg {\n     visible 1\n     ignorepointer 1\n     position 0 0\n     size 195 30\n" in text
    assert "     color 0.135 0.18 0.225 1\n     priority 1\n" in text
    assert "    TextWidgetClass BtnHideText {\n     visible 1\n     ignorepointer 1\n     position 0 0\n     size 195 30\n" in text
    assert '     text "#STR_HIDE"\n' in text and '     "text halign" center\n' in text
    assert clean(text) == []


def test_a_non_root_button_says_it_ignores_children():
    """`children` is a root-only capability (a button root holds page content);
    `_b_button` never reads it, so a nested button that carries one silently
    dropped it until this note was added."""
    out = build(page({"button": {"name": "BtnHide", "size": [195, 30], "text": "x", "children": [
        {"panel": {"name": "Ghost", "size": [1, 1], "color": "$panel"}}]}}))
    assert out.notes == ["ui/MyMod/oz_page.json root.0: button ignores children outside the root"]
    assert clean(out.files["MyMod/gui/layouts/oz_page.layout"]) == []


def test_field_puts_the_edit_box_inside_a_frame_and_a_fill():
    out = build(page({"field": {"name": "ChatInput", "size": [472, 28]}}))
    text = out.files["MyMod/gui/layouts/oz_page.layout"]
    assert "  PanelWidgetClass ChatInputFrame {\n   visible 1\n   position 20 20\n   size 472 28\n" in text
    assert "    PanelWidgetClass ChatInputFill {\n     visible 1\n     ignorepointer 1\n     position 1 1\n     size 470 26\n" in text
    assert "    EditBoxWidgetClass ChatInput {\n     visible 1\n     position 6 0\n     size 460 28\n" in text
    assert "     style Default\n" in text and '     font "gui/fonts/MetronBook14"\n' in text
    assert '"exact text size"' not in text.split("EditBoxWidgetClass ChatInput")[1]
    assert clean(text) == []
    out = build(page({"field": {"name": "Body", "size": [400, 200], "lines": 8}}))
    assert "MultilineEditBoxWidgetClass Body {" in out.files["MyMod/gui/layouts/oz_page.layout"]
    assert "     lines 8\n" in out.files["MyMod/gui/layouts/oz_page.layout"]


def test_text_wraps_and_sizes_its_height_to_the_text():
    out = build(page({"text": {"name": "Story", "w": 400, "text": "long", "font": "body"}}))
    text = out.files["MyMod/gui/layouts/oz_page.layout"]
    assert "  RichTextWidgetClass Story {\n   visible 1\n   ignorepointer 1\n   position 20 20\n   size 400 20\n" in text
    assert '   wrap 1\n   "size to text h" 0\n   "size to text v" 1\n' in text
    out = build(page({"text": {"name": "Plain", "w": 400, "plain": True}}))
    assert "MultilineTextWidgetClass Plain {" in out.files["MyMod/gui/layouts/oz_page.layout"]


def test_section_writes_bar_label_and_rule():
    out = build(page({"section": {"name": "SecSpZ", "w": 560, "text": "#STR_SPAWNS"}}))
    text = out.files["MyMod/gui/layouts/oz_page.layout"]
    assert ("  FrameWidgetClass SecSpZ {\n   visible 1\n   position 20 20\n   size 560 30\n"
            "   hexactpos 1\n   vexactpos 1\n   hexactsize 1\n   vexactsize 1\n   priority 0\n   {\n") in text
    assert "    PanelWidgetClass SecSpZBar {\n     visible 1\n     ignorepointer 1\n     position 0 4\n     size 3 16\n" in text
    assert "    TextWidgetClass SecSpZLbl {\n     visible 1\n     ignorepointer 1\n     position 12 0\n     size 548 24\n" in text
    assert "    PanelWidgetClass SecSpZRule {\n     visible 1\n     ignorepointer 1\n     position 0 29\n     size 560 1\n" in text
    assert clean(text) == []


SHEET = {"frame": {"name": "Sheet", "size": [640, 518]}}
#: The edge sibling per anchor: its position, and the alignments it inherits.
EDGE_ANCHORS = [
    ("right", "19 19", ("halign right_ref",)),
    ("bottom", "19 19", ("valign bottom_ref",)),
    ("center", "20 20", ("halign center_ref", "valign center_ref")),
    ("bottom-right", "19 19", ("halign right_ref", "valign bottom_ref")),
]


@pytest.mark.parametrize("anchor, edge_pos, aligns", EDGE_ANCHORS)
def test_a_panel_edge_offsets_only_the_axes_the_anchor_does_not_centre(anchor, edge_pos, aligns):
    """`halign right_ref position X` puts the widget X units INSIDE the
    parent's right edge, so a border one unit outside the panel is still
    `X - 1` with a size two units larger -- the same arithmetic as the
    unanchored case. `center_ref` is the exception: it measures from the
    middle, where growing by 2 is the whole of it and the -1 would push the
    border a unit up and left, leaving no border at all on the other two
    sides (verified against the generated file, 2026-09-04)."""
    out = build(page({"panel": {"name": "Card", "anchor": anchor, "at": [20, 20], "size": [200, 100],
                                "color": "$panel", "edge": True}}, root=SHEET))
    text = out.files["MyMod/gui/layouts/oz_page.layout"]
    edge = text.split("PanelWidgetClass CardEdge {")[1].split("}")[0]
    card = text.split("PanelWidgetClass Card {")[1].split("}")[0]
    for align in aligns:
        assert align in edge and align in card
    assert f" position {edge_pos}\n" in edge and " size 202 102\n" in edge
    assert " position 20 20\n" in card and " size 200 100\n" in card
    assert clean(text) == []


def test_an_anchored_section_moves_as_one_piece():
    """The bar, the label and the rule are placed INSIDE the section's own
    frame, so their offsets are the same whatever the anchor is. As three
    anchored siblings they each measured from the anchored edge, and under
    `right` the label ended up entirely to the LEFT of the bar it follows."""
    out = build(page({"section": {"name": "Sec", "anchor": "right", "at": [20, 20], "w": 560,
                                  "text": "#STR_SPAWNS"}}, root=SHEET))
    text = out.files["MyMod/gui/layouts/oz_page.layout"]
    assert "  FrameWidgetClass Sec {\n   visible 1\n   halign right_ref\n   position 20 20\n   size 560 30\n" in text
    assert "    PanelWidgetClass SecBar {\n     visible 1\n     ignorepointer 1\n     position 0 4\n     size 3 16\n" in text
    assert "    TextWidgetClass SecLbl {\n     visible 1\n     ignorepointer 1\n     position 12 0\n     size 548 24\n" in text
    assert "    PanelWidgetClass SecRule {\n     visible 1\n     ignorepointer 1\n     position 0 29\n     size 560 1\n" in text
    assert "halign right_ref" not in text.split("FrameWidgetClass Sec {")[1].split("{", 1)[1]
    assert clean(text) == []


CONTACT_LIST = {"list": {"name": "ContactScroll", "stack": "ContactList", "size": [600, 395], "rows": {
    "contact_row": {"row": {"name": "ContactRow", "h": "$size.contactRow", "click": True,
        "note": "One contact.", "children": [
        {"panel": {"name": "RowBg", "size": "fill", "color": "$panel"}},
        {"panel": {"name": "RowPick", "size": "fill", "color": "$pick", "hidden": True}},
        {"chip": {"name": "RowChip"}},
        {"label": {"name": "RowName", "at": [22, 6], "size": [300, 25], "font": "header"}},
        {"label": {"name": "RowWhere", "anchor": "right", "at": [12, 6], "w": "auto", "h": 25, "font": "small", "color": "$muted", "align": "right"}},
        {"label": {"name": "RowDetail", "at": [22, 30], "w": "fill", "h": 20, "font": "small", "color": "$muted"}},
        {"rule": {"name": "RowLine", "anchor": "bottom", "at": [22, 0], "w": "fill"}},
    ]}}}}}


def test_list_writes_scroll_spacer_and_a_row_file_of_the_scroll_width_minus_the_bar():
    out = build(page(CONTACT_LIST))
    page_text = out.files["MyMod/gui/layouts/oz_page.layout"]
    assert (
        "  ScrollWidgetClass ContactScroll {\n   visible 1\n   position 20 20\n   size 600 395\n"
        "   hexactpos 1\n   vexactpos 1\n   hexactsize 1\n   vexactsize 1\n   priority 0\n   clipchildren 1\n"
        '   "Scrollbar V" 1\n   {\n'
        "    WrapSpacerWidgetClass ContactList {\n     visible 1\n     position 0 0\n     size 1 0\n"
        "     hexactpos 1\n     vexactpos 1\n     hexactsize 0\n     vexactsize 1\n     priority 0\n"
        '     Padding 0\n     Margin 0\n     "Size To Content V" 1\n    }\n'
    ) in page_text
    row = out.files["MyMod/gui/layouts/contact_row.layout"]
    assert row.splitlines()[0] == "// GENERATED by dayz-mcp layout_build from ui/MyMod/oz_page.json (rows.contact_row) -- edit the source, not this file."
    assert row.splitlines()[1] == "// One contact."
    assert "ButtonWidgetClass ContactRow {\n visible 1\n position 0 0\n size 1 55\n hexactpos 1\n vexactpos 1\n hexactsize 0\n vexactsize 1\n text \"\"\n" in row
    assert "  PanelWidgetClass RowBg {\n   visible 1\n   ignorepointer 1\n   position 0 0\n   size 1 1\n   hexactpos 1\n   vexactpos 1\n   hexactsize 0\n   vexactsize 0\n" in row
    assert "  PanelWidgetClass RowChip {\n   visible 0\n   ignorepointer 1\n   position 0 0\n   size 4 1\n" in row
    # 600 - 10 for the bar = 590 wide; fill from x = 22 is 568
    assert "  TextWidgetClass RowDetail {\n   visible 1\n   ignorepointer 1\n   position 22 30\n   size 568 20\n" in row
    assert "  TextWidgetClass RowWhere {\n   visible 1\n   ignorepointer 1\n   halign right_ref\n   position 12 6\n   size 0 25\n" in row
    assert "  PanelWidgetClass RowLine {\n   visible 1\n   ignorepointer 1\n   valign bottom_ref\n   position 22 0\n   size 568 1\n" in row
    assert clean(page_text) == [] and clean(row) == []


CHAT_LINE = {"row": {"name": "ChatLine", "stack": True, "children": [
    {"hrow": {"h": 22, "children": [
        {"panel": {"name": "LineMine", "w": 2, "h": 16, "color": "$accent", "hidden": True}},
        {"gap": {"w": 8}},
        {"label": {"name": "LineWho", "w": "auto", "font": "small", "color": "$accent"}},
        {"gap": {"w": 10}},
        {"label": {"name": "LineAt", "w": "auto", "font": "small", "color": "$faint"}},
    ]}},
    {"text": {"name": "LineText", "font": "body"}},
    {"gap": {"h": 8}},
]}}


def test_a_stack_row_lets_the_engine_size_it_from_its_text():
    out = build(page({"list": {"name": "ChatScroll", "stack": "ChatLines", "size": [871, 342],
                               "rows": {"chat_line": CHAT_LINE}}}))
    row = out.files["MyMod/gui/layouts/chat_line.layout"]
    assert (
        "WrapSpacerWidgetClass ChatLine {\n visible 1\n position 0 0\n size 1 0\n hexactpos 1\n vexactpos 1\n"
        ' hexactsize 0\n vexactsize 1\n Padding 0\n Margin 0\n "Size To Content V" 1\n {\n'
    ) in row
    # the hrow is a GridSpacer that sizes itself; a hidden marker, gaps as panels, self-sized labels
    assert (
        "  GridSpacerWidgetClass HRow1 {\n   visible 1\n   position 0 0\n   size 0 0\n   hexactpos 1\n   vexactpos 1\n"
        '   hexactsize 1\n   vexactsize 1\n   priority 0\n   Padding 0\n   Margin 0\n   "Size To Content H" 1\n'
        '   "Size To Content V" 1\n   Columns 5\n   Rows 1\n'
    ) in row
    assert "    PanelWidgetClass LineMine {\n     visible 0\n     ignorepointer 1\n     position 0 0\n     size 2 16\n" in row
    assert "    PanelWidgetClass Gap1 {\n     visible 1\n     ignorepointer 1\n     position 0 0\n     size 8 22\n" in row
    assert "    TextWidgetClass LineWho {\n     visible 1\n     ignorepointer 1\n     position 0 0\n     size 0 22\n" in row
    assert '     "size to text h" 1\n' in row
    assert "  RichTextWidgetClass LineText {\n   visible 1\n   ignorepointer 1\n   position 0 0\n   size 1 20\n   hexactpos 1\n   vexactpos 1\n   hexactsize 0\n   vexactsize 1\n" in row
    assert '   "size to text v" 1\n' in row
    # Gap1/Gap2 are the hrow's own w:8/w:10 gaps (ctx's gap counter is one
    # sequence, depth-first); this row's own h:8 gap is the third to claim a name.
    assert "  PanelWidgetClass Gap3 {\n   visible 1\n   ignorepointer 1\n   position 0 0\n   size 1 8\n   hexactpos 1\n   vexactpos 1\n   hexactsize 0\n   vexactsize 1\n" in row
    assert clean(row) == []


def test_a_static_stack_and_a_grid_write_exact_numbers():
    out = build(page({"vbox": {"children": [
        {"stack": {"name": "Notes", "h": 100, "children": [
            {"label": {"name": "L1", "h": 20}}, {"text": {"name": "T1"}}]}},
        {"grid": {"name": "Pad", "size": [184, 154], "cols": 3, "rows": 4, "gap": 4, "children": [
            {"panel": {"name": "K1", "color": "$raised"}}, {"panel": {"name": "K2", "color": "$raised"}}]}},
    ]}}))
    text = out.files["MyMod/gui/layouts/oz_page.layout"]
    assert "  WrapSpacerWidgetClass Notes {\n   visible 1\n   position 20 20\n   size 600 0\n   hexactpos 1\n   vexactpos 1\n   hexactsize 1\n   vexactsize 1\n" in text
    assert "    TextWidgetClass L1 {\n     visible 1\n     ignorepointer 1\n     position 0 0\n     size 600 20\n" in text
    assert "    RichTextWidgetClass T1 {\n     visible 1\n     ignorepointer 1\n     position 0 0\n     size 600 20\n" in text
    assert "  GridSpacerWidgetClass Pad {\n   visible 1\n   position 20 120\n   size 184 154\n" in text
    assert "   Padding 4\n   Margin 0\n   Columns 3\n   Rows 4\n" in text
    # cells: (184 - 2*4)/3 = 58.667 by (154 - 3*4)/4 = 35.5
    assert "    PanelWidgetClass K1 {\n     visible 1\n     ignorepointer 1\n     position 0 0\n     size 58.667 35.5\n" in text
    assert clean(text) == []


def test_keypad_is_a_grid_of_glyph_buttons():
    out = build(page({"keypad": {"name": "LockPad", "at": [0, 40], "anchor": "center", "size": [184, 154],
                                 "cols": 3, "rows": 4, "gap": 4,
                                 "keys": [{"name": "Key1", "glyph": "1"}, {"name": "Key2", "glyph": "2"}]}}))
    text = out.files["MyMod/gui/layouts/oz_page.layout"]
    assert "  GridSpacerWidgetClass LockPad {\n   visible 1\n   halign center_ref\n   valign center_ref\n   position 0 40\n   size 184 154\n" in text
    assert "    ButtonWidgetClass Key1 {\n     visible 1\n     position 0 0\n     size 58.667 35.5\n" in text
    assert "      TextWidgetClass Key1Text {" in text and '       text "1"\n' in text
    assert clean(text) == []


def test_listbox_and_raw_pass_their_properties_through():
    out = build(page({"listbox": {"name": "InviteList", "size": [851, 295], "font": "hint",
                                  "props": {"style": "NoScrollBar", "title visible": 0, "highlight row": 1, "lines": 14,
                                            "colums": "Name;70;Faction;30"}}}))
    text = out.files["MyMod/gui/layouts/oz_page.layout"]
    assert "  TextListboxWidgetClass InviteList {\n   visible 1\n   position 20 20\n   size 851 295\n" in text
    assert '   style NoScrollBar\n   "title visible" 0\n   "highlight row" 1\n   lines 14\n   colums "Name;70;Faction;30"\n' in text
    assert '   font "gui/fonts/sdf_MetronBook24"\n   "exact text" 1\n   "exact text size" 14\n' in text
    out = build(page({"raw": {"name": "Map", "class": "MapWidgetClass", "size": "fill", "props": {"filter": 1}}}))
    assert "  MapWidgetClass Map {\n   visible 1\n   position 20 20\n   size 600 478\n" in out.files["MyMod/gui/layouts/oz_page.layout"]
    assert clean(text) == []


def test_a_preview_under_a_high_priority_is_refused():
    with pytest.raises(LayoutGenError, match="priority 300 above 256 on the way to the preview"):
        build(page({"frame": {"name": "Card", "size": [300, 300], "priority": 300, "children": [
            {"preview": {"name": "ItemView", "size": [200, 200]}}]}}))
    out = build(page({"preview": {"name": "ItemView", "size": [200, 200], "priority": 256}}))
    assert "  ItemPreviewWidgetClass ItemView {\n   visible 1\n   position 20 20\n   size 200 200\n" in out.files["MyMod/gui/layouts/oz_page.layout"]


def test_stack_children_may_not_fill():
    with pytest.raises(LayoutGenError, match="a stack child cannot fill"):
        build(page({"stack": {"name": "S", "h": 100, "children": [{"panel": {"name": "P", "size": "fill", "color": "$panel"}}]}}))


def test_everything_generated_in_this_file_is_lint_clean():
    """Every golden case above, in one sweep: the generator's contract."""
    pages = [page(CONTACT_LIST), page({"list": {"name": "S", "stack": "L", "size": [871, 342], "rows": {"r": CHAT_LINE}}}),
             page({"button": {"name": "B", "size": [100, 30]}}), page({"field": {"name": "F", "size": [200, 28]}})]
    for desc in pages:
        for path, text in build(desc).files.items():
            assert clean(text) == [], path


from pathlib import Path

from dayz_mcp.layoutgen import build_project

PAGE = {"layout": "oz_page", "root": {"frame": {"name": "MyPage", "size": [640, 518], "inset": 20}},
        "body": {"label": {"name": "Title", "h": 30, "text": "Hello"}}}


def project(tmp_path: Path) -> Path:
    (tmp_path / "ui").mkdir()
    (tmp_path / "ui" / "tokens.json").write_text(json.dumps(TOKENS), encoding="utf-8")
    (tmp_path / "ui" / "MyMod").mkdir()
    (tmp_path / "ui" / "MyMod" / "oz_page.json").write_text(json.dumps(PAGE), encoding="utf-8")
    (tmp_path / "MyMod").mkdir()
    return tmp_path


def test_build_project_writes_changed_files_only(tmp_path):
    root = project(tmp_path)
    first = build_project(root, ["MyMod"], {}, write=True)
    assert first.written == ["MyMod/gui/layouts/oz_page.layout"] and first.unchanged == []
    out = root / "MyMod" / "gui" / "layouts" / "oz_page.layout"
    assert out.read_text(encoding="utf-8").startswith("// GENERATED by dayz-mcp layout_build from ui/MyMod/oz_page.json")
    assert b"\r\n" not in out.read_bytes()
    second = build_project(root, ["MyMod"], {}, write=True)
    assert second.written == [] and second.unchanged == ["MyMod/gui/layouts/oz_page.layout"]
    assert second.sources == {"MyMod/gui/layouts/oz_page.layout": "ui/MyMod/oz_page.json"}


def test_build_project_with_write_false_reports_but_touches_nothing(tmp_path):
    root = project(tmp_path)
    report = build_project(root, ["MyMod"], {}, write=False)
    assert report.written == ["MyMod/gui/layouts/oz_page.layout"]
    assert not (root / "MyMod" / "gui").exists()


def test_build_project_without_a_ui_folder_is_a_quiet_no_op(tmp_path):
    (tmp_path / "MyMod").mkdir()
    report = build_project(tmp_path, ["MyMod"], {}, write=True)
    assert report.written == [] and report.files == {}


def test_build_project_refuses_a_bad_description_before_writing_anything(tmp_path):
    root = project(tmp_path)
    (root / "ui" / "MyMod" / "bad.json").write_text('{"layout": "bad", "root": {"label": {}}}', encoding="utf-8")
    with pytest.raises(LayoutGenError, match="ui/MyMod/bad.json root: root must be a frame, a panel or a button"):
        build_project(root, ["MyMod"], {}, write=True)
    assert not (root / "MyMod" / "gui").exists()


def test_build_project_refuses_two_descriptions_for_one_file(tmp_path):
    root = project(tmp_path)
    (root / "ui" / "MyMod" / "twin.json").write_text(json.dumps(PAGE), encoding="utf-8")
    with pytest.raises(LayoutGenError, match="oz_page.layout is generated by two descriptions"):
        build_project(root, ["MyMod"], {}, write=True)


def test_build_project_needs_tokens_once_a_ui_folder_exists(tmp_path):
    root = project(tmp_path)
    (root / "ui" / "tokens.json").unlink()
    with pytest.raises(LayoutGenError, match="ui/tokens.json is missing"):
        build_project(root, ["MyMod"], {}, write=True)


def test_build_project_reads_tokens_from_the_given_path(tmp_path):
    # ui/tokens.json absent; tokens live elsewhere, named by the profile
    (tmp_path / "ui" / "MyMod").mkdir(parents=True)
    (tmp_path / "ui" / "MyMod" / "oz_page.json").write_text(json.dumps(PAGE), encoding="utf-8")
    (tmp_path / "MyMod").mkdir()
    tokens_path = tmp_path / "shared" / "tokens.json"
    tokens_path.parent.mkdir()
    tokens_path.write_text(json.dumps(TOKENS), encoding="utf-8")
    report = build_project(tmp_path, ["MyMod"], {}, write=True, tokens_path=tokens_path)
    assert report.written == ["MyMod/gui/layouts/oz_page.layout"]
    assert (tmp_path / "MyMod" / "gui" / "layouts" / "oz_page.layout").is_file()


def test_build_project_names_a_missing_given_tokens_path_in_the_refusal(tmp_path):
    (tmp_path / "ui" / "MyMod").mkdir(parents=True)
    (tmp_path / "ui" / "MyMod" / "oz_page.json").write_text(json.dumps(PAGE), encoding="utf-8")
    (tmp_path / "MyMod").mkdir()
    with pytest.raises(LayoutGenError, match=r"shared/tokens\.json is missing"):
        build_project(tmp_path, ["MyMod"], {}, write=True, tokens_path=tmp_path / "shared" / "tokens.json")


def test_main_refuses_an_unknown_mod_and_builds_a_known_one(tmp_path, capsys):
    root = project(tmp_path)
    (root / "dayz-mcp.toml").write_text('[project]\nname = "my-mod"\n\n[build]\nmods = ["MyMod"]\n', encoding="utf-8")
    (root / "MyMod" / "config.cpp").write_text("class CfgPatches { };\n", encoding="utf-8")
    assert layoutgen.main([str(root), "Other"]) == 1
    assert "'Other' is not a mod of this project; it declares: MyMod" in capsys.readouterr().out
    assert not (root / "MyMod" / "gui").exists()
    assert layoutgen.main([str(root), "MyMod"]) == 0
    assert "wrote MyMod/gui/layouts/oz_page.layout" in capsys.readouterr().out
    assert layoutgen.main([]) == 2


def test_icon_writes_an_imageset_sprite_tinted_by_a_token():
    out = build(page({"icon": {"name": "TabIcon", "at": [17, 8], "size": [26, 26], "image": "tab_chat", "color": "$muted"}}))
    text = out.files["MyMod/gui/layouts/oz_page.layout"]
    assert (
        "  ImageWidgetClass TabIcon {\n   visible 1\n   ignorepointer 1\n   position 37 28\n   size 26 26\n"
        "   hexactpos 1\n   vexactpos 1\n   hexactsize 1\n   vexactsize 1\n   color 0.58 0.65 0.71 1\n   priority 0\n"
        '   image0 "set:my_icons image:tab_chat"\n   mode blend\n   "src alpha" 1\n  }\n'
    ) in text
    assert clean(text) == []
    with pytest.raises(LayoutGenError, match="icon needs image"):
        build(page({"icon": {"name": "X", "size": [10, 10]}}))


def test_icon_needs_a_set_when_the_tokens_name_none():
    t = Tokens.from_text(json.dumps({**TOKENS, "device": {"page": [1306, 518]}}), "ui/tokens.json")
    with pytest.raises(LayoutGenError, match="icon needs set, or device.iconset"):
        build_layout(page({"icon": {"name": "X", "size": [10, 10], "image": "a"}}), t, "ui/MyMod/x.json", "MyMod/gui/layouts")
    out = build_layout(page({"icon": {"name": "X", "size": [10, 10], "image": "a", "set": "other_set"}}), t, "ui/MyMod/x.json", "MyMod/gui/layouts")
    text = out.files["MyMod/gui/layouts/oz_page.layout"]
    assert 'image0 "set:other_set image:a"' in text
    assert clean(text) == []


def test_image_writes_the_three_texture_traps_once():
    out = build(page({"image": {"name": "Bezel", "size": [1403, 590], "file": "MyMod/gui/textures/bezel_ca.paa", "color": "$white"}}))
    text = out.files["MyMod/gui/layouts/oz_page.layout"]
    assert '   image0 "MyMod/gui/textures/bezel_ca.paa"\n   mode blend\n   "src alpha" 1\n   "stretch mode" stretch_w_h\n' in text
    assert clean(text) == []
    out = build(page({"image": {"name": "Bezel", "size": [10, 10], "file": "MyMod/gui/textures/b_ca.paa", "stretch": False}}))
    text = out.files["MyMod/gui/layouts/oz_page.layout"]
    assert '"stretch mode"' not in text
    assert clean(text) == []
    with pytest.raises(LayoutGenError, match="image needs file"):
        build(page({"image": {"name": "Bezel", "size": [10, 10], "file": "bezel.png"}}))


def test_badge_is_a_hidden_disc_with_a_count():
    out = build(page({"badge": {"name": "TabBadge", "anchor": "right", "at": [6, 4]}}))
    text = out.files["MyMod/gui/layouts/oz_page.layout"]
    assert (
        "  ImageWidgetClass TabBadge {\n   visible 0\n   ignorepointer 1\n   halign right_ref\n   position 26 24\n   size 18 18\n"
        "   hexactpos 1\n   vexactpos 1\n   hexactsize 1\n   vexactsize 1\n   color 0.94 0.54 0.14 1\n   priority 0\n"
        '   image0 "set:my_icons image:badge"\n   mode blend\n   "src alpha" 1\n   {\n'
        "    TextWidgetClass TabBadgeText {\n     visible 1\n     ignorepointer 1\n     position 0 0\n     size 18 18\n"
    ) in text
    assert '     "exact text size" 10\n' in text and '     "text halign" center\n' in text
    assert clean(text) == []


def test_badge_accepts_explicit_w_h_or_size():
    """`sized.setdefault("size", [18, 18])` used to inject `size` even when the
    caller gave `w`/`h` directly, colliding with them inside `_dim`."""
    out = build(page({"badge": {"name": "TabBadge", "w": 24, "h": 24}}))
    text = out.files["MyMod/gui/layouts/oz_page.layout"]
    assert "   size 24 24\n" in text
    assert clean(text) == []
    out = build(page({"badge": {"name": "TabBadge", "size": [30, 30]}}))
    text = out.files["MyMod/gui/layouts/oz_page.layout"]
    assert "   size 30 30\n" in text
    assert clean(text) == []


def test_header_is_a_row_of_icon_title_and_actions():
    out = build(page({"header": {"icon": "tab_contacts", "title": {"name": "ContactsHeader"}, "actions": [
        {"button": {"name": "BtnHide", "w": 195, "font": "small"}},
        {"button": {"name": "BtnHideContacts", "w": 195, "font": "small"}}]}}))
    text = out.files["MyMod/gui/layouts/oz_page.layout"]
    # 34 high; the 22-unit icon is centred: y = 20 + (34 - 22) / 2 = 26
    assert "  ImageWidgetClass ContactsHeaderIcon {\n   visible 1\n   ignorepointer 1\n   position 20 26\n   size 22 22\n" in text
    assert "   color 0.31 0.71 0.91 1\n" in text
    # icon 22 + gap 10 -> title at x 52, width 600 - 22 - 195 - 195 - 3 gaps of 10 = 158
    assert "  TextWidgetClass ContactsHeader {\n   visible 1\n   ignorepointer 1\n   position 52 20\n   size 158 34\n" in text
    assert '   "exact text size" 18\n' in text
    assert "  ButtonWidgetClass BtnHide {\n   visible 1\n   position 220 20\n   size 195 34\n" in text
    assert "  ButtonWidgetClass BtnHideContacts {\n   visible 1\n   position 425 20\n   size 195 34\n" in text
    assert clean(text) == []
    with pytest.raises(LayoutGenError, match="header needs title"):
        build(page({"header": {"icon": "x"}}))


def test_header_accepts_size_instead_of_h():
    """`row.setdefault("h", "$size.header")` used to fire even when the caller
    gave `size`, colliding with it inside `_dim`."""
    out = build(page({"header": {"title": {"name": "ContactsHeader"}, "size": [600, 40]}}))
    text = out.files["MyMod/gui/layouts/oz_page.layout"]
    assert "  TextWidgetClass ContactsHeader {\n   visible 1\n   ignorepointer 1\n   position 20 20\n   size 600 40\n" in text
    assert clean(text) == []


def test_a_header_inside_a_vbox_defaults_its_height_to_size_header():
    """`_default_main`'s vertical branch had no `header` case, so a vbox
    child header without an explicit h hit "h is required here" before
    `_b_header` ever got a chance to fall back to $size.header itself."""
    out = build(page({"vbox": {"children": [
        {"header": {"title": {"name": "ContactsHeader"}}},
        {"panel": {"name": "Body", "h": "fill", "color": "$panel"}},
    ]}}))
    text = out.files["MyMod/gui/layouts/oz_page.layout"]
    assert "TextWidgetClass ContactsHeader {\n   visible 1\n   ignorepointer 1\n   position 20 20\n   size 600 34\n" in text
    assert "PanelWidgetClass Body {\n   visible 1\n   ignorepointer 1\n   position 20 54\n   size 600 444\n" in text
    assert clean(text) == []


def test_hbox_centres_a_child_that_declares_its_own_height():
    out = build(page({"hbox": {"h": 40, "children": [
        {"panel": {"name": "Tall", "w": 100, "color": "$panel"}},
        {"panel": {"name": "Short", "w": 100, "h": 20, "color": "$panel"}}]}}))
    text = out.files["MyMod/gui/layouts/oz_page.layout"]
    assert "PanelWidgetClass Tall {\n   visible 1\n   ignorepointer 1\n   position 20 20\n   size 100 40\n" in text
    assert "PanelWidgetClass Short {\n   visible 1\n   ignorepointer 1\n   position 120 30\n   size 100 20\n" in text
    assert clean(text) == []


def test_a_screen_root_is_proportional_and_holds_anchored_exact_children():
    out = build_layout({"layout": "oz_menu", "root": {"frame": {"name": "Root", "size": "screen", "priority": 2, "children": [
        {"panel": {"name": "Backdrop", "size": "fill", "color": "$none", "priority": 1}},
        {"frame": {"name": "Device", "anchor": "center", "size": [1403, 590], "priority": 2}}]}}},
        tokens(), "ui/MyMod/oz_menu.json", "MyMod/gui/layouts")
    text = out.files["MyMod/gui/layouts/oz_menu.layout"]
    assert "FrameWidgetClass Root {\n visible 1\n position 0 0\n size 1 1\n hexactpos 1\n vexactpos 1\n hexactsize 0\n vexactsize 0\n priority 2\n" in text
    assert "  PanelWidgetClass Backdrop {\n   visible 1\n   ignorepointer 1\n   position 0 0\n   size 1 1\n   hexactpos 1\n   vexactpos 1\n   hexactsize 0\n   vexactsize 0\n" in text
    assert "  FrameWidgetClass Device {\n   visible 1\n   halign center_ref\n   valign center_ref\n   position 0 0\n   size 1403 590\n" in text
    assert clean(text) == []
    with pytest.raises(LayoutGenError, match="fill needs an exact ancestor"):
        build_layout({"layout": "x", "root": {"frame": {"name": "R", "size": "screen", "children": [
            {"panel": {"name": "P", "w": "fill", "h": 10, "color": "$none"}}]}}}, tokens(), "ui/MyMod/x.json", "MyMod/gui/layouts")
    with pytest.raises(LayoutGenError, match="only a frame root can be the whole screen"):
        build_layout({"layout": "x", "root": {"panel": {"name": "R", "size": "screen"}}}, tokens(), "ui/MyMod/x.json", "MyMod/gui/layouts")


@pytest.mark.parametrize("extra", [{"inset": 5}, {"at": [10, 0]}, {"anchor": "center"},
                                   {"inset": 5, "at": [10, 0], "anchor": "center"}])
def test_a_screen_roots_ignored_inset_at_and_anchor_are_all_noted(extra):
    """A screen root is always `position 0 0 / size 1 1`: `inset` already got
    a `ctx.note` saying so, but `anchor: "center"` (validated, then thrown
    away without a word) and `at` (never even read) did not -- two of three
    ways to move a root silently doing nothing while the third at least
    said so."""
    out = build_layout({"layout": "x", "root": {"frame": {"name": "R", "size": "screen", **extra}}},
                        tokens(), "ui/MyMod/x.json", "MyMod/gui/layouts")
    assert out.notes == ["ui/MyMod/x.json root: inset/at/anchor are ignored on a screen root"]


def test_a_screen_root_with_none_of_inset_at_or_anchor_is_not_noted():
    out = build_layout({"layout": "x", "root": {"frame": {"name": "R", "size": "screen"}}},
                        tokens(), "ui/MyMod/x.json", "MyMod/gui/layouts")
    assert out.notes == []


def test_header_does_not_accept_color():
    """No description under any live project's `ui/` uses `color` directly
    on a `header` node (grepped 2026-09-04, across a real project's page
    descriptions) -- only ever inside `title`, where `_b_header`'s own
    `label.setdefault(...)` already reaches the title label with it. `header`
    granted it anyway (COMMON) and `_stackbox`'s `row` never read it:
    accepted, then silently dropped, no note. Kept out of ALLOWED instead,
    the same as any other key the schema does not actually back."""
    with pytest.raises(LayoutGenError, match=r"header does not take \['color'\]"):
        build(page({"header": {"color": "$accent", "title": {"name": "H"}}}))


def test_a_button_root_is_bare():
    out = build_layout({"layout": "oz_tab", "root": {"button": {"name": "MyTab", "size": [60, 60], "children": [
        {"panel": {"name": "TabActive", "size": "fill", "color": "$pick", "hidden": True}}]}}},
        tokens(), "ui/MyMod/oz_tab.json", "MyMod/gui/layouts")
    text = out.files["MyMod/gui/layouts/oz_tab.layout"]
    assert text.splitlines()[1] == "ButtonWidgetClass MyTab {"
    assert ' text ""\n' in text and "MyTabEdge" not in text and "MyTabText" not in text
    assert clean(text) == []
    assert out.notes == []


def test_a_button_root_notes_the_attributes_it_ignores():
    """The mirror image of the gap `a409c5e` closed one commit earlier for a
    NESTED button (it notes an ignored `children`): a button ROOT holds page
    content the way a frame/panel root does, so `build_layout` overwrites
    whatever `text`/`font`/`bg`/`edge`/`glyph` the description gave it with a
    bare `text ""` and used to emit no Edge/Bg/Text children AND no note --
    the attributes simply vanished."""
    out = build_layout({"layout": "oz_tab", "root": {"button": {
        "name": "MyTab", "size": [60, 60], "text": "#STR_HI", "font": "small",
        "bg": "$panel", "edge": "$accent", "glyph": True}}},
        tokens(), "ui/MyMod/oz_tab.json", "MyMod/gui/layouts")
    text = out.files["MyMod/gui/layouts/oz_tab.layout"]
    assert ' text ""\n' in text and "MyTabEdge" not in text and "MyTabText" not in text
    assert out.notes == ["ui/MyMod/oz_tab.json root: button root ignores text/font/bg/edge/glyph -- give it children"]
    assert clean(text) == []


def test_at_on_a_button_roots_child_is_not_noted():
    """A button root's children are placed absolutely BY DESIGN -- a fixed
    60x60 tab holding an icon, a label and a badge at specific offsets is
    not a page built out of vbox/hbox -- so the page-child `at` note (meant
    to steer PAGE authors towards containers) does not apply here. `on_page`
    used to be unconditionally True for every root's direct children, which
    reproduced live against a real project's own tab description (a button
    root whose icon, label and badge children each declare `at`, exactly
    like this one): three false "root.N: `at` on a page child" notes, none
    of them actionable advice (a button cannot hold a vbox/hbox in the
    first place)."""
    out = build_layout({"layout": "oz_tab", "root": {"button": {"name": "MyTab", "size": [60, 60], "children": [
        {"icon": {"name": "TabIcon", "at": [17, 8], "size": [26, 26], "image": "tab_page"}},
        {"label": {"name": "TabLabel", "at": [0, 38], "w": "fill", "h": 16}},
    ]}}}, tokens(), "ui/MyMod/oz_tab.json", "MyMod/gui/layouts")
    assert out.notes == []


def test_a_list_may_hold_static_children_in_its_spacer():
    out = build(page({"list": {"name": "PostScroll", "stack": "PostStack", "size": [400, 300], "children": [
        {"text": {"name": "PostBody", "plain": True, "font": "body"}}]}}))
    text = out.files["MyMod/gui/layouts/oz_page.layout"]
    assert "    WrapSpacerWidgetClass PostStack {\n" in text
    assert "      MultilineTextWidgetClass PostBody {\n       visible 1\n       ignorepointer 1\n       position 0 0\n       size 1 20\n       hexactpos 1\n       vexactpos 1\n       hexactsize 0\n       vexactsize 1\n" in text
    assert clean(text) == []


def test_bottom_center_anchor():
    out = build(page({"label": {"name": "LockHint", "anchor": "bottom-center", "at": [0, 18], "size": [800, 24], "text": "hint"}}))
    text = out.files["MyMod/gui/layouts/oz_page.layout"]
    assert "   halign center_ref\n   valign bottom_ref\n   position 0 38\n   size 800 24\n" in text
    assert clean(text) == []


def test_bar_is_a_track_with_an_empty_fill():
    out = build({"layout": "oz_page", "root": {"frame": {"name": "P", "size": [300, 100], "children": [
        {"bar": {"name": "Charge", "at": [10, 10], "w": 200, "h": 10, "track": "$rule", "fill": "$accent"}}]}}})
    text = out.files["MyMod/gui/layouts/oz_page.layout"]
    assert (
        "  PanelWidgetClass Charge {\n"
        "   visible 1\n"
        "   ignorepointer 1\n"
        "   position 10 10\n"
        "   size 200 10\n"
        "   hexactpos 1\n"
        "   vexactpos 1\n"
        "   hexactsize 1\n"
        "   vexactsize 1\n"
        "   color 1 1 1 0.08\n"
        "   priority 0\n"
        "   style rover_sim_colorable\n"
    ) in text
    assert (
        "    PanelWidgetClass ChargeFill {\n"
        "     visible 1\n"
        "     ignorepointer 1\n"
        "     position 0 0\n"
        "     size 0 10\n"            # the fill starts empty; the script widens it
        "     hexactpos 1\n"
        "     vexactpos 1\n"
        "     hexactsize 1\n"
        "     vexactsize 1\n"
        "     color 0.31 0.71 0.91 1\n"
        "     priority 1\n"
        "     style rover_sim_colorable\n"
        "    }\n"
    ) in text
    assert clean(text) == []


def test_bar_takes_its_default_height_in_a_vbox():
    out = build({"layout": "oz_page", "root": {"frame": {"name": "P", "size": [300, 100], "children": [
        {"vbox": {"name": "Col", "size": "fill", "children": [
            {"bar": {"name": "Charge", "w": "fill"}}]}}]}}})
    text = out.files["MyMod/gui/layouts/oz_page.layout"]
    assert "     size 300 10\n" in text            # $size.bar = 10 in the test TOKENS
    assert "       size 0 10\n" in text            # the fill, same height as the track
    assert "     color 1 1 1 0.08\n" in text        # track defaults to $rule
    assert "       color 0.31 0.71 0.91 1\n" in text  # fill defaults to $accent
    assert clean(text) == []


def test_bar_accepts_no_children():
    with pytest.raises(LayoutGenError, match=r"bar does not take \['children'\]"):
        build(page({"bar": {"name": "Charge", "w": 200, "h": 10, "children": [{"label": {"name": "L"}}]}}))


def test_map_is_a_clipped_map_widget_without_children():
    out = build({"layout": "oz_page", "root": {"frame": {"name": "P", "size": [1306, 518], "children": [
        {"map": {"name": "Map", "at": [0, 0], "size": [1306, 430]}}]}}})
    text = out.files["MyMod/gui/layouts/oz_page.layout"]
    assert (
        "  MapWidgetClass Map {\n"
        "   visible 1\n"
        "   position 0 0\n"
        "   size 1306 430\n"
        "   hexactpos 1\n"
        "   vexactpos 1\n"
        "   hexactsize 1\n"
        "   vexactsize 1\n"
        "   clipchildren 1\n"
        "  }\n"
    ) in text
    assert clean(text) == []
    with pytest.raises(LayoutGenError, match="paints over its children"):
        build({"layout": "oz_page", "root": {"frame": {"name": "P", "size": [10, 10], "children": [
            {"map": {"name": "Map", "size": [10, 10], "children": [{"label": {"name": "L"}}]}}]}}})
