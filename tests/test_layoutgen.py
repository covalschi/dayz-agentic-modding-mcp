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
             "hint": {"size": 14}, "small": {"size": 13},
             "field": {"face": "gui/fonts/MetronBook14", "size": 14, "fixed": True}},
    "space": {"page": 20, "gap": 10, "tight": 6},
    "size": {"button": 30, "field": 28, "header": 34, "hint": 22, "contactRow": 55},
    "device": {"page": [1306, 518]},
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
