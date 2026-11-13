"""A measured web page -> .layout text. No browser here: the tests hand the converter the
measurement a probe would have written (one tree, the rectangles at four sizes) and read
what it makes of it. What the engine does with that text is measured on a stand, not here."""
import json

import pytest

from dayz_mcp import css2layout
from dayz_mcp.css2layout import Converter, Icons, Strings, load_config

W, H, DW, DH = 400, 200, 100, 50
TEXT = {"fam": "Test Sans", "px": 13, "w": 400, "col": {"c": "#e0dfda"}}


class Page:
    """Builds a probe: every node says where it is as a function of the page's size."""

    def __init__(self):
        self.rects = {s: [] for s in "abcd"}
        self.n = 0

    def _put(self, at):
        i = self.n
        self.n += 1
        for step, s in enumerate("abcd"):
            self.rects[s].append([float(v) for v in at(W + step * DW, H + step * DH)])
        return i

    def el(self, tag, at, kids=None, **more):
        node = {"i": self._put(at), "t": tag, "r": None, "eb": "#0c0c0c"}
        node.update(more)
        if kids:
            node["k"] = kids
        return node

    def text(self, text, at, **font):
        i = self._put(at)
        f = dict(TEXT, **font)
        return {"i": i, "text": text, "lines": [self.rects["a"][i]], "f": f}

    def probe(self, tree):
        return {"size": [W, H], "step": [DW, DH], "fixed": True, "tree": tree,
                "a": self.rects["a"], "b": self.rects["b"], "c": self.rects["c"], "d": self.rects["d"]}


def config(tmp_path, strings=None):
    table = tmp_path / "stringtable.csv"
    rows = ['"Language","original","english",']
    for k, (ua, en) in (strings or {}).items():
        rows.append(f'"{k}","{ua}","{en}",')
    table.write_text("\n".join(rows) + "\n", encoding="utf-8")
    cfg = {
        "out": "out", "prefix": "MyMod/gui/layouts", "report": "report",
        "strings": {"files": ["stringtable.csv"], "column": "original"},
        "fonts": [{"family": "Test Sans", "weight": [0, 1000], "face": "MyMod/gui/fonts/sans28", "sizes": {"13": 14},
                   "wrap": {"face": "MyMod/gui/fonts/sdf32", "sizes": {"13": 14}}}],
        "glyphs": {"●": {"icon": "dot", "scale": 0.6}},
        "icons": {"set": "myset"},
        "pages": {"page": {"page": "page.html", "size": [W, H], "layout": "mymod_page", "root": "PaneTest", "auto": "t"}},
    }
    path = tmp_path / "css2layout.json"
    path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    return load_config(path)


def convert(cfg, probe, demo=False):
    conv = Converter(cfg, "page", cfg["pages"]["page"], probe, Strings(cfg), Icons(cfg))
    return conv, conv.convert(demo)


def widget(text, name):
    """The body of one widget of a layout's text, down to its first child or its end."""
    lines = text.split("\n")
    at = next(i for i, line in enumerate(lines) if line.rstrip().endswith(f" {name} {{"))
    out = [lines[at]]
    for line in lines[at + 1:]:
        if line.strip() in ("{", "}"):
            break
        out.append(line)
    return "\n".join(out)


def simple_page():
    p = Page()
    title = p.text("Title", lambda w, h: [10, 10, 40, 15])
    caption = p.text("Go", lambda w, h: [w - 60, 12, 20, 15], ta="center")
    button = p.el("button", lambda w, h: [w - 90, 5, 80, 30], [caption], bg={"c": "#e8782b"}, click=1, dz={"name": "BtnGo"})
    value = p.text("42", lambda w, h: [100, 60, 20, 15])
    holder = p.el("span", lambda w, h: [100, 60, 20, 15], [value], dz={"text": "ValText"})
    head = p.el("div", lambda w, h: [0, 0, w, 40], [title, button], bg={"c": "#131312"},
                bd=[{"s": "b", "w": 1, "st": "solid", "col": {"c": "#3c3c3a"}}])
    root = p.el("div", lambda w, h: [0, 0, w, h], [head, holder], bg={"c": "#0c0c0c"})
    return p.probe(root)


def test_boxes_texts_and_a_button(tmp_path):
    cfg = config(tmp_path, {"STR_TITLE": ("Title", "Title in English")})
    conv, files = convert(cfg, simple_page())
    text = files["mymod_page.layout"]
    assert text.startswith(css2layout.GENERATED)
    assert "FrameWidgetClass PaneTest {" in text
    # the caption that the stringtable knows became its key; the one it does not know stayed
    assert 'text "#STR_TITLE"' in text
    assert 'text "Go"' in text and "Go" in conv.rep.captions
    # a named button keeps its parts reachable, and its caption is centred in it
    assert "ButtonWidgetClass BtnGo {" in text and "PanelWidgetClass BtnGoBg {" in text
    assert '"text halign" center' in text
    # the header: its ground and its bottom line, both as wide as the page grows
    assert text.count("style rover_sim_colorable") >= 4
    assert f"userID {css2layout.FLEX_FLAG | (12 << 8)}" in text


def test_what_follows_the_right_edge_says_so(tmp_path):
    cfg = config(tmp_path)
    _conv, files = convert(cfg, simple_page())
    button = widget(files["mymod_page.layout"], "BtnGo")
    assert f"userID {css2layout.FLEX_FLAG | 12}" in button        # x takes the whole growth, nothing else
    assert "position 310 5" in button and "size 80 30" in button


def test_a_text_the_script_fills_is_empty_until_the_demo(tmp_path):
    cfg = config(tmp_path)
    _conv, files = convert(cfg, simple_page(), demo=True)
    assert 'text ""' in widget(files["mymod_page.layout"], "ValText")
    assert 'text "42"' in widget(files["_demo_mymod_page.layout"], "ValText")


def list_page():
    p = Page()
    rows = []
    for n, name in enumerate(["first", "second"]):
        label = p.text(name, lambda w, h, n=n: [20, 50 + n * 36 + 8, 40, 15])
        span = p.el("span", lambda w, h, n=n: [20, 50 + n * 36 + 8, 40, 15], [label], dz={"text": "RowName"})
        deco = {"bg": {"c": "#2f1d11"}} if n == 1 else {}
        rows.append(p.el("button", lambda w, h, n=n: [10, 50 + n * 36, w - 20, 30], [span], click=1,
                         dz={"name": "MyMod_Row", "row": "mymod_row"}, **deco))
    lst = p.el("div", lambda w, h: [10, 50, w - 20, h - 60], rows, of=["visible", "auto"], sc=[380, 66, 380, 140],
               dz={"name": "List"})
    root = p.el("div", lambda w, h: [0, 0, w, h], [lst], bg={"c": "#0c0c0c"})
    return p.probe(root)


def test_a_list_is_a_spacer_and_a_row_template(tmp_path):
    cfg = config(tmp_path)
    conv, files = convert(cfg, list_page(), demo=True)
    page, row, demo = files["mymod_page.layout"], files["mymod_row.layout"], files["_demo_mymod_page.layout"]
    # the page: a scroll, the spacer the rows go into, and no row of the sample
    assert "ScrollWidgetClass ListScroll {" in page and "WrapSpacerWidgetClass ListRows {" in page
    assert "MyMod_Row" not in page
    assert '"Size To Content V" 1' in page and "Padding 6" in page
    # the template: the richest instance (the one that draws a ground), named parts, no sample text
    assert row.splitlines()[1] == "ButtonWidgetClass MyMod_Row {"
    assert "PanelWidgetClass MyMod_RowBg {" in row
    assert 'text ""' in widget(row, "RowName")
    assert "size 380 30" in row
    # the demo holds both rows with their words
    assert demo.count("ButtonWidgetClass MyMod_Row") == 2 and 'text "second"' in demo
    assert any("mymod_row.layout" in line for line in conv.rep.rows)


def test_one_jump_among_three_steps_does_not_bend_the_rule(tmp_path):
    p = Page()
    # a box that follows the bottom edge, but drops 14 units further at the second size only
    # (a line of text above it unwrapped there): two steps of three still agree
    jump = {W: 0, W + DW: 0, W + 2 * DW: -14, W + 3 * DW: -14}
    box = p.el("div", lambda w, h: [10, h - 40 + jump[w], w - 20, 30], bg={"c": "#131312"}, dz={"name": "Bar"})
    root = p.el("div", lambda w, h: [0, 0, w, h], [box], bg={"c": "#0c0c0c"})
    cfg = config(tmp_path)
    conv, files = convert(cfg, p.probe(root))
    bar = widget(files["mymod_page.layout"], "Bar")
    assert f"userID {css2layout.FLEX_FLAG | (12 << 4) | (12 << 8)}" in bar   # y and width, in full
    assert conv.reflows >= 1


def test_a_sign_the_font_lacks_becomes_an_icon(tmp_path):
    p = Page()
    dot = p.text("●", lambda w, h: [10, 10, 8, 15])
    dot["g"] = "●"
    word = p.text("ready", lambda w, h: [22, 10, 40, 15])
    span = p.el("span", lambda w, h: [10, 10, 52, 15], [dot, word], dz={"text": "State", "icon": "StateDot"})
    root = p.el("div", lambda w, h: [0, 0, w, h], [span], bg={"c": "#0c0c0c"})
    cfg = config(tmp_path)
    _conv, files = convert(cfg, p.probe(root))
    text = files["mymod_page.layout"]
    assert 'ImageWidgetClass StateDot {' in text and 'image0 "set:myset image:dot"' in text
    assert "TextWidgetClass State {" in text


def test_a_quote_in_a_text_never_reaches_the_layout(tmp_path):
    p = Page()
    said = p.text('say "hi"', lambda w, h: [10, 10, 60, 15])
    root = p.el("div", lambda w, h: [0, 0, w, h], [said], bg={"c": "#0c0c0c"})
    cfg = config(tmp_path)
    conv, files = convert(cfg, p.probe(root))
    assert "say 'hi'" in files["mymod_page.layout"] and '"hi"' not in files["mymod_page.layout"]
    assert any("double quote" in k for k in conv.rep.dropped)


def test_a_config_laid_over_another_keeps_each_ones_paths(tmp_path):
    base = tmp_path / "core" / "ui"
    base.mkdir(parents=True)
    (base / "css2layout.json").write_text(json.dumps({"serve": "../pages", "out": "../layouts", "fonts": [{"family": "A"}]}),
                                          encoding="utf-8")
    glue = tmp_path / "glue" / "ui"
    glue.mkdir(parents=True)
    (glue / "css2layout.json").write_text(json.dumps({"extends": "../../core/ui/css2layout.json", "out": "../mine"}),
                                          encoding="utf-8")
    cfg = load_config(glue / "css2layout.json")
    assert cfg["serve"] == str((tmp_path / "core" / "pages").resolve())
    assert cfg["out"] == str((tmp_path / "glue" / "mine").resolve())
    assert cfg["fonts"] == [{"family": "A"}]


def test_run_refuses_a_page_the_config_does_not_name(tmp_path):
    cfg = config(tmp_path)
    with pytest.raises(css2layout.ConvertError):
        css2layout.run(tmp_path / "css2layout.json", ["nope"], reuse=True)


def test_what_a_scroll_holds_past_the_pages_edge_is_kept(tmp_path):
    p = Page()
    far = p.text("far below", lambda w, h: [20, h + 100, 60, 15])
    tail = p.el("div", lambda w, h: [10, h + 90, w - 20, 40], [far], bg={"c": "#131312"})
    area = p.el("div", lambda w, h: [0, 0, w, h], [tail], of=["visible", "auto"], sc=[W, H + 140, W, H], dz={"name": "Area"})
    lost = p.text("screen reader only", lambda w, h: [-9999, 0, 100, 15])
    root = p.el("div", lambda w, h: [0, 0, w, h], [area, lost], bg={"c": "#0c0c0c"})
    cfg = config(tmp_path)
    conv, files = convert(cfg, p.probe(root))
    text = files["mymod_page.layout"]
    assert "far below" in text                 # the scroll's tail is its content
    assert "screen reader only" not in text    # what the page keeps off itself is not drawn
    assert any("off the page" in note for note in conv.rep.notes)
    # the scrolled frame is as tall as what it holds
    assert f"size {W} {H + 140}" in widget(text, "AreaBody")


def test_a_fieldsets_border_starts_at_its_legends_middle(tmp_path):
    p = Page()
    word = p.text("MAIN", lambda w, h: [30, 40, 40, 14])
    legend = p.el("legend", lambda w, h: [24, 40, 52, 14], [word], pad=[0, 6, 0, 6])
    inside = p.text("inside", lambda w, h: [30, 70, 40, 15])
    group = p.el("fieldset", lambda w, h: [10, 40, w - 20, 100], [legend, inside],
                 bd=[{"s": s, "w": 1, "st": "solid", "col": {"c": "#3c3c3a"}} for s in "trbl"], dz={"name": "Group"})
    root = p.el("div", lambda w, h: [0, 0, w, h], [group], bg={"c": "#0c0c0c"})
    cfg = config(tmp_path)
    _conv, files = convert(cfg, p.probe(root))
    text = files["mymod_page.layout"]
    edge = widget(text, "GroupEdge")
    assert "position 0 7" in edge and "size 380 93" in edge      # the frame begins 7 units down: the legend's middle
    assert "position 10 40" in widget(text, "Group") and "size 380 100" in widget(text, "Group")   # the box is still the fieldset's
    # the legend stands on the page's ground, so the border stops under it
    assert text.index("GroupEdge") < text.index('text "MAIN"')
    assert text.count("color 0.047 0.047 0.047 1") >= 3           # root, the fieldset's fill, the legend's ground


def test_an_edit_box_keeps_its_inputs_alignment(tmp_path):
    p = Page()
    box = p.el("input", lambda w, h: [10, 10, 80, 30], inp={"type": "text", "value": "42", "ph": "0"},
               f=dict(TEXT, ta="right"), dz={"name": "Amount"},
               bd=[{"s": s, "w": 1, "st": "solid", "col": {"c": "#3c3c3a"}} for s in "trbl"])
    root = p.el("div", lambda w, h: [0, 0, w, h], [box], bg={"c": "#0c0c0c"})
    cfg = config(tmp_path)
    _conv, files = convert(cfg, p.probe(root), demo=True)
    assert '"text halign" right' in widget(files["mymod_page.layout"], "Amount")
    assert '"text halign" right' in widget(files["mymod_page.layout"], "AmountPh")
    assert 'text "42"' in widget(files["_demo_mymod_page.layout"], "Amount")


def test_an_edit_boxs_sample_cannot_break_the_layout(tmp_path):
    p = Page()
    box = p.el("textarea", lambda w, h: [10, 10, 300, 100], inp={"type": "textarea", "value": '{\n  "a": "b"\n}', "ph": ""},
               f=dict(TEXT), dz={"name": "Editor"})
    root = p.el("div", lambda w, h: [0, 0, w, h], [box], bg={"c": "#0c0c0c"})
    cfg = config(tmp_path)
    _conv, files = convert(cfg, p.probe(root), demo=True)
    line = next(ln for ln in files["_demo_mymod_page.layout"].split("\n") if ln.strip().startswith("text ") and "a" in ln)
    assert line.strip() == """text "{ 'a': 'b' }\""""


def test_a_tick_boxs_parts_are_named_and_a_radio_is_round(tmp_path):
    p = Page()
    tick = p.el("input", lambda w, h: [10, 10, 13, 13], inp={"type": "checkbox", "value": "on", "ph": "", "on": 1},
                f=dict(TEXT), dz={"name": "Tick"})
    radio = p.el("input", lambda w, h: [40, 10, 13, 13], inp={"type": "radio", "value": "on", "ph": ""},
                 f=dict(TEXT), dz={"name": "Pick"})
    root = p.el("div", lambda w, h: [0, 0, w, h], [tick, radio], bg={"c": "#0c0c0c"})
    cfg = config(tmp_path)
    cfg["icons"]["disc"] = "disc"
    cfg["glyphs"]["\u2713"] = {"icon": "check", "scale": 0.9}
    _conv, files = convert(cfg, p.probe(root))
    text = files["mymod_page.layout"]
    assert "PanelWidgetClass TickEdge {" in text and "PanelWidgetClass TickBg {" in text and "ImageWidgetClass TickMark {" in text
    assert "ImageWidgetClass PickEdge {" in text and "ImageWidgetClass PickBg {" in text
    assert "visible 1" in widget(text, "TickMark") and "visible 0" in widget(text, "PickMark")


def test_a_caption_may_name_its_own_key(tmp_path):
    p = Page()
    word = p.text("Node", lambda w, h: [10, 10, 40, 15])
    span = p.el("span", lambda w, h: [10, 10, 40, 15], [word], dz={"str": "STR_MY_NODE"})
    root = p.el("div", lambda w, h: [0, 0, w, h], [span], bg={"c": "#0c0c0c"})
    cfg = config(tmp_path, {"STR_OTHER_NODE": ("Node", "Node")})
    conv, files = convert(cfg, p.probe(root))
    assert 'text "#STR_MY_NODE"' in files["mymod_page.layout"]
    assert not conv.rep.captions


def test_a_round_dot_is_one_disc(tmp_path):
    p = Page()
    dot = p.el("span", lambda w, h: [10, 10, 8, 8], bg={"c": "#66c75c"}, rad=4, dz={"name": "Dot"})
    root = p.el("div", lambda w, h: [0, 0, w, h], [dot], bg={"c": "#0c0c0c"})
    cfg = config(tmp_path)
    cfg["icons"]["disc"] = "disc"
    _conv, files = convert(cfg, p.probe(root))
    text = files["mymod_page.layout"]
    assert 'ImageWidgetClass DotBg {' in text and 'image:disc' in widget(text, "DotBg")
    assert "DotBgL" not in text
