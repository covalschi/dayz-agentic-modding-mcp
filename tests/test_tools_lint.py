"""`mod_lint`: the only verdict in this server that needs no game at all.

Its whole value is the boot it saves, so the bar is the opposite of the other
tools': not "does it catch things" but "does it stay quiet on code that is
fine". A linter that refuses a good mod gets switched off, and then it catches
nothing at all.
"""
import textwrap
from pathlib import Path

import pytest

from dayz_mcp import tools
from dayz_mcp.knowledge.parse import CLASS, Declaration
from dayz_mcp.knowledge.store import CORE, DEPS
from dayz_mcp.lint import REFUSE, WARN
from dayz_mcp.tools import session

PROFILE = """
[project]
name = "my-mod"

[build]
mods = ["MyMod"]
"""


def make_project(root: Path, sources: dict[str, str]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "dayz-mcp.toml").write_text(textwrap.dedent(PROFILE), encoding="utf-8")
    mod = root / "MyMod"
    src = mod / "scripts" / "4_World"
    src.mkdir(parents=True, exist_ok=True)
    (mod / "config.cpp").write_text("class CfgPatches { };\n", encoding="utf-8")
    for name, text in sources.items():
        (src / name).write_text(text, encoding="utf-8")
    return root


def open_with(root: Path, sources: dict[str, str]):
    session.reset()
    make_project(root, sources)
    opened = tools.project_open(str(root))
    assert opened.ok, opened.error
    return opened


def seed(layer: str, path: str, names) -> None:
    store = session.knowledge()
    decls = [Declaration(name=n, kind=CLASS, file=path, line=1) for n in names]
    store.put_source(layer, path, decls, size=10, mtime=1000.0)


def checks(result):
    return [f["check"] for f in result.data["findings"]]


def test_it_refuses_without_a_project(tmp_path):
    session.reset()
    result = tools.mod_lint()
    assert not result.ok
    assert result.hint


def test_clean_sources_pass(tmp_path):
    open_with(tmp_path / "p", {"a.c": "class MyThing extends ItemBase { void Run() { Foo(); } }\n"})
    seed(CORE, "game.c", ["ItemBase"])
    seed(DEPS, "Mod/x.c", ["Other"])
    result = tools.mod_lint()
    assert result.ok, result.error
    assert result.data["findings"] == []
    assert result.data["files"] == 1


def test_a_self_extending_modded_class_stops_it(tmp_path):
    open_with(tmp_path / "p", {"a.c": "modded class PlayerBase extends PlayerBase { }\n"})
    seed(CORE, "game.c", ["PlayerBase"])
    seed(DEPS, "Mod/x.c", ["Other"])
    result = tools.mod_lint()
    assert not result.ok
    assert checks(result) == ["modded-self"]
    assert result.data["refusals"] == 1
    assert "MyMod" in result.data["findings"][0]["file"]


def test_a_modded_class_with_no_target_stops_it(tmp_path):
    open_with(tmp_path / "p", {"a.c": "modded class PlayerBse { }\n"})
    seed(CORE, "game.c", ["PlayerBase"])
    seed(DEPS, "Mod/x.c", ["Other"])
    result = tools.mod_lint()
    assert not result.ok
    assert checks(result) == ["modded-target"]


def test_an_unbuilt_index_downgrades_that_check_and_says_so(tmp_path):
    """Without the game indexed, "no such class" is a guess. It must warn and
    name what it could not read, not accuse the mod."""
    open_with(tmp_path / "p", {"a.c": "modded class PlayerBase { }\n"})
    result = tools.mod_lint()
    assert result.ok, result.error
    assert [f["severity"] for f in result.data["findings"]] == [WARN]
    assert "not built" in result.data["findings"][0]["message"]


def test_a_warning_alone_does_not_stop_a_build(tmp_path):
    open_with(tmp_path / "p", {"a.c": 'class A { void Run() { string s = "a"\n + "b"; } }\n'})
    seed(CORE, "game.c", ["ItemBase"])
    seed(DEPS, "Mod/x.c", ["Other"])
    result = tools.mod_lint()
    assert result.ok, result.error
    assert checks(result) == ["line-continuation"]
    assert result.data["warnings"] == 1


def test_strict_makes_a_warning_stop_it(tmp_path):
    open_with(tmp_path / "p", {"a.c": 'class A { void Run() { string s = "a"\n + "b"; } }\n'})
    seed(CORE, "game.c", ["ItemBase"])
    seed(DEPS, "Mod/x.c", ["Other"])
    result = tools.mod_lint(strict=True)
    assert not result.ok
    assert result.data["warnings"] == 1


def test_an_unknown_mod_name_is_refused_by_name(tmp_path):
    open_with(tmp_path / "p", {"a.c": "class A { }\n"})
    result = tools.mod_lint(mod="NotMine")
    assert not result.ok
    assert "MyMod" in result.hint


def test_the_answer_says_how_much_it_read(tmp_path):
    open_with(tmp_path / "p", {"a.c": "class A { }\n", "b.c": "class B { }\n"})
    result = tools.mod_lint()
    assert result.data["files"] == 2
    assert result.data["declarations"] == 2
    assert result.data["truncated"] is False
    assert result.data["elapsed_ms"] >= 0


def make_layout(root: Path, name: str, text: str) -> None:
    layouts = root / "MyMod" / "gui" / "layouts"
    layouts.mkdir(parents=True, exist_ok=True)
    (layouts / name).write_text(text, encoding="utf-8")


def test_layouts_are_linted_alongside_scripts(tmp_path):
    open_with(tmp_path / "p", {"a.c": "class MyThing { }\n"})
    seed(CORE, "game.c", ["ItemBase"])
    seed(DEPS, "Mod/x.c", ["Other"])
    make_layout(tmp_path / "p", "bare.layout",
                "FrameWidgetClass Root {\n size 1 1\n {\n  EditBoxWidgetClass E {\n   size 100 20\n  }\n }\n}\n")
    result = tools.mod_lint()
    assert result.ok, result.error
    assert "layout-editbox-bare" in checks(result)
    assert result.data["layouts"] == 1
    assert result.data["files"] == 2


def test_a_quote_inside_a_layout_text_stops_the_build(tmp_path):
    open_with(tmp_path / "p", {"a.c": "class MyThing { }\n"})
    seed(CORE, "game.c", ["ItemBase"])
    seed(DEPS, "Mod/x.c", ["Other"])
    make_layout(tmp_path / "p", "hang.layout",
                'FrameWidgetClass Root {\n size 1 1\n {\n  TextWidgetClass T {\n   size 1 1\n   text "a "b" c"\n  }\n }\n}\n')
    result = tools.mod_lint()
    assert not result.ok
    assert "layout-quote-in-text" in checks(result)


def test_project_layout_classes_are_allowed_by_the_layout_lint(tmp_path):
    root = tmp_path / "p"
    open_with(root, {"a.c": "class MyThing { }\n"})
    (root / "dayz-mcp.toml").write_text(
        textwrap.dedent(PROFILE).replace('mods = ["MyMod"]', 'mods = ["MyMod"]\nlayout_classes = ["MyMapWidgetClass"]'),
        encoding="utf-8",
    )
    assert tools.project_open(str(root)).ok
    seed(CORE, "game.c", ["ItemBase"])
    seed(DEPS, "Mod/x.c", ["Other"])
    make_layout(root, "map.layout", "FrameWidgetClass Root {\n size 1 1\n {\n  MyMapWidgetClass M {\n   size 1 1\n  }\n }\n}\n")
    result = tools.mod_lint()
    assert result.ok, result.error
    assert "layout-class" not in checks(result)
