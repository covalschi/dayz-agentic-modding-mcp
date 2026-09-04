"""The client's interface, read through the bridge's client half.

What is worth testing here is not "does it call the mod" but the three places
this layer could quietly lie:

1. **An empty tree that means "the bridge is not loaded".** One pbo carries
   both halves, and a profile listing it under `mods.server_only` keeps it off
   the client's own `-mod` line. The server half then works perfectly while
   every tool here answers with nothing -- which is indistinguishable from "this
   mod draws no interface" unless it is named.
2. **A page that does not say it is a page.** Same failure as a truncated
   entity listing, one level less visible: a menu is exactly the kind of thing
   somebody counts.
3. **A click that went somewhere else.** A path is only meaningful against the
   tree it was read from, so the expectation travels with it -- and the cursor
   tract has to click where the client says the widget IS, not where a stale
   answer said it was.
"""
import json
import textwrap
from pathlib import Path

import pytest

from dayz_mcp import tools, winui
from dayz_mcp.bridge.protocol import BridgeState, Command, CommandState
from dayz_mcp.tools import session, ui

PROFILE = """
[project]
name = "my-mod"

[build]
mods = ["MyMod"]
"""


def make_project(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "dayz-mcp.toml").write_text(textwrap.dedent(PROFILE), encoding="utf-8")
    mod = root / "MyMod"
    mod.mkdir(parents=True, exist_ok=True)
    (mod / "config.cpp").write_text("class CfgPatches { };\n", encoding="utf-8")
    return root


def with_stand(root: Path, stand: Path) -> None:
    (stand / "profiles").mkdir(parents=True, exist_ok=True)
    (root / "dayz-mcp.local.toml").write_text(
        f'[machine]\nstand_root = "{stand.as_posix()}"\n', encoding="utf-8"
    )


class Beat:
    def __init__(self, status="growing", tick=7, session_id="client-1"):
        self.status = status
        self.tick = tick
        self.session_id = session_id


class FakeChannel:
    """Records what was sent; answers with what the test set up."""

    def __init__(self, *a, **kw):
        self.sent: list[Command] = []
        self.session = "client-1"
        self.answer = CommandState(id="", status="done", detail="ok", finished_at=1.0)
        self.state = BridgeState(tick=7, session_id="client-1", world={})

    # the pieces tools/ui.py actually uses
    def current_session_id(self):
        return self.session

    def heartbeat_detail(self, window=3.0):
        return Beat()

    def build_command(self, verb, args):
        from dayz_mcp.errors import ok as _ok
        return _ok(Command(id=f"{verb}-1", session_id="client-1", verb=verb, args=args))

    def send(self, cmd, *, is_alive):
        from dayz_mcp.errors import ok as _ok
        self.sent.append(cmd)
        return _ok(cmd.id)

    def await_result(self, cmd_id, timeout, poll=0.5):
        if self.answer is None:
            return None
        return CommandState(id=cmd_id, status=self.answer.status,
                            detail=self.answer.detail, finished_at=1.0)

    def read_state(self):
        return self.state

    def _state_path(self):
        return Path("state.json")


@pytest.fixture
def live(tmp_path, monkeypatch):
    """An open project, a client this session believes is alive, and a fake
    client channel in place of the real one."""
    session.reset()
    root = make_project(tmp_path / "p")
    with_stand(root, tmp_path / "stand")
    from dayz_mcp import tools
    assert tools.project_open(str(root)).ok
    session.set_client_pid(9876, "DayZDiag_x64.exe")
    monkeypatch.setattr("dayz_mcp.tools.ui.is_alive", lambda pid, image="": True)
    channel = FakeChannel()
    monkeypatch.setattr("dayz_mcp.tools.ui._client_channel", lambda: channel)
    return channel


def node_line(path="0.1", cls="ButtonWidget", name="ok_button", vis="11",
              rect="100 200 40 20", depth="2", text="OK", metrics=None):
    parts = [path, cls, name, vis, rect, depth, text]
    if metrics is not None:
        parts.append(metrics)
    return "|".join(parts)


# ------------------------------------------------------------------ parsing


def test_a_node_line_becomes_its_fields():
    parsed = ui._node(node_line())
    assert parsed == {
        "path": "0.1", "class": "ButtonWidget", "name": "ok_button",
        "visible": True, "shown": True, "rect": "100 200 40 20",
        "depth": 2, "text": "OK", "text_size": None,
    }


def test_an_eight_field_line_carries_the_text_size():
    parsed = ui._node(node_line(cls="TextWidget", metrics="120 18"))
    assert parsed["text_size"] == (120, 18)
    assert ui._node(node_line(metrics=""))["text_size"] is None
    assert ui._node(node_line())["text_size"] is None


def test_visible_and_shown_are_two_answers_not_one():
    """They differ exactly when a node is visible in itself but sits inside a
    hidden parent -- the ordinary state of half a menu, which would otherwise
    read as being on screen."""
    parsed = ui._node(node_line(vis="10"))
    assert parsed["visible"] is True
    assert parsed["shown"] is False


def test_a_line_that_does_not_parse_survives_instead_of_vanishing():
    assert ui._node("nonsense") == {"raw": "nonsense"}


def test_the_centre_of_a_rectangle():
    assert ui._centre("100 200 40 20") == (120, 210)
    assert ui._centre("nonsense") is None
    assert ui._centre("1 2 3") is None


# ------------------------------------------------------- the bridge is absent


def test_no_client_is_refused_before_anything_is_sent(tmp_path):
    session.reset()
    root = make_project(tmp_path / "p")
    with_stand(root, tmp_path / "stand")
    from dayz_mcp import tools
    assert tools.project_open(str(root)).ok

    result = ui.ui_tree()
    assert not result.ok
    assert "no client" in result.error


def test_a_client_that_never_published_names_the_profile_key(live):
    """The failure this whole refusal exists for: the server half works, the
    client half is simply not loaded, and an empty tree looks like a mod with
    no interface."""
    live.session = None
    result = ui.ui_tree()
    assert not result.ok
    assert "never published" in result.error
    assert "server_only" in result.hint
    assert live.sent == []


# ------------------------------------------------------------------- listing


def test_the_tree_request_carries_its_page_size(live):
    ui.ui_tree(root="screen", depth=4, limit=25)
    assert live.sent[-1].verb == "ui_tree"
    assert live.sent[-1].args == {"root": "screen", "depth": "4", "limit": "25", "offset": "0"}


def test_the_tree_request_carries_its_offset(live):
    ui.ui_tree(offset=300)
    assert live.sent[-1].args["offset"] == "300"
    ui.ui_find(name="x", offset=25)
    assert live.sent[-1].args["offset"] == "25"


def test_every_argument_crosses_the_wire_as_a_string(live):
    ui.ui_tree(depth=3, limit=10)
    for key, value in live.sent[-1].args.items():
        assert isinstance(value, str), f"{key} went as {type(value).__name__}"


def test_nodes_come_back_parsed_with_the_count_and_the_total(live):
    live.state = BridgeState(tick=9, session_id="client-1", world={
        "ui_total": 120,
        "ui_nodes": [node_line(path="0"), node_line(path="0.1")],
    })
    result = ui.ui_tree()
    assert result.ok, result.error
    assert result.data["count"] == 2
    assert result.data["total"] == 120
    assert result.data["truncated"] is True
    assert result.data["nodes"][1]["path"] == "0.1"


def test_a_complete_listing_is_not_marked_truncated(live):
    live.state = BridgeState(tick=9, session_id="client-1", world={
        "ui_total": 1, "ui_nodes": [node_line()],
    })
    assert ui.ui_tree().data["truncated"] is False


def test_truncation_accounts_for_the_offset(live):
    live.state = BridgeState(tick=9, session_id="client-1", world={
        "ui_total": 302, "ui_nodes": [node_line(path="0"), node_line(path="0.1")],
    })
    assert ui.ui_tree(offset=300).data["truncated"] is False
    assert ui.ui_tree(offset=0).data["truncated"] is True


# ---------------------------------------------------------------------- find


def test_find_with_no_filter_is_refused_before_anything_is_sent(live):
    result = ui.ui_find()
    assert not result.ok
    assert "ui_tree" in result.hint
    assert live.sent == []


def test_find_sends_the_class_under_the_name_the_mod_reads(live):
    """The Python argument cannot be called `class`, and the mod's argument
    cannot be called anything else."""
    ui.ui_find(class_name="ButtonWidget")
    assert live.sent[-1].args["class"] == "ButtonWidget"
    assert "class_name" not in live.sent[-1].args


def test_find_omits_the_filters_it_was_not_given(live):
    ui.ui_find(name="ok_button")
    assert "text" not in live.sent[-1].args
    assert "class" not in live.sent[-1].args


# --------------------------------------------------------------------- click


def test_an_unknown_tract_is_refused_by_name(live):
    result = ui.ui_click("0.1", via="teleport")
    assert not result.ok
    assert "cursor" in result.hint
    assert live.sent == []


def test_the_script_tract_delivers_through_the_handler(live):
    result = ui.ui_click("0.1", expect_name="ok_button", expect_class="ButtonWidget")
    assert result.ok, result.error
    assert live.sent[-1].verb == "ui_click"
    assert live.sent[-1].args["expect_name"] == "ok_button"
    assert "deliver" not in live.sent[-1].args
    assert result.data["via"] == "script"


def test_the_cursor_tract_asks_the_client_where_the_widget_is_and_clicks_there(live, monkeypatch):
    """The path is resolved through the SAME check the script tract uses, and
    the click lands on the rectangle the CLIENT reported -- not on anything the
    caller remembered."""
    live.state = BridgeState(tick=9, session_id="client-1", world={
        "ui_total": 1, "ui_nodes": [node_line(rect="300 400 60 40")],
    })
    clicks = []

    def fake_click(pid, x, y):
        from dayz_mcp.errors import ok as _ok
        clicks.append((pid, x, y))
        return _ok({"x": x, "y": y})

    monkeypatch.setattr("dayz_mcp.tools.ui.winui.click", fake_click)

    result = ui.ui_click("0.1", via="cursor", expect_name="ok_button")
    assert result.ok, result.error
    assert live.sent[-1].args["deliver"] == "none"
    assert clicks == [(9876, 330, 420)]
    assert result.data["via"] == "cursor"
    assert result.data["clicked_at"] == {"x": 330, "y": 420}


def test_the_cursor_tract_stops_when_the_path_no_longer_matches(live, monkeypatch):
    """A refusal from the mod must not be followed by a click anyway -- that
    would be the exact "pressed the wrong button and reported success" failure
    the expectation exists to prevent."""
    live.answer = CommandState(
        id="", status="failed",
        detail="ui_click: the node at that path is named 'cancel_button', not 'ok_button'",
        finished_at=1.0,
    )
    clicked = []
    monkeypatch.setattr("dayz_mcp.tools.ui.winui.click",
                        lambda pid, x, y: clicked.append((x, y)))

    result = ui.ui_click("0.1", via="cursor", expect_name="ok_button")
    assert not result.ok
    assert "cancel_button" in result.error
    assert clicked == []


def test_the_cursor_tract_says_so_when_there_is_no_rectangle(live, monkeypatch):
    live.state = BridgeState(tick=9, session_id="client-1", world={
        "ui_total": 1, "ui_nodes": ["nonsense"],
    })
    monkeypatch.setattr("dayz_mcp.tools.ui.winui.click",
                        lambda pid, x, y: pytest.fail("clicked with no rectangle"))
    result = ui.ui_click("0.1", via="cursor")
    assert not result.ok
    assert "rectangle" in result.error


# ---------------------------------------------------------------------- text


def test_writing_a_field_carries_the_expectation(live):
    ui.ui_text("0.2", "hello", expect_class="EditBoxWidget")
    assert live.sent[-1].verb == "ui_text"
    assert live.sent[-1].args["text"] == "hello"
    assert live.sent[-1].args["expect_class"] == "EditBoxWidget"


# ---------------------------------------------------------------------- menu


def test_the_menu_answer_is_free(live):
    """Republished every tick, so it costs no command round trip -- the same
    bargain world_state makes with no arguments."""
    live.state = BridgeState(tick=11, session_id="client-1", world={
        "ui_menu": "MyModMenu", "ui_cursor": 1, "ui_dialog": 0,
    })
    result = ui.ui_menu()
    assert result.ok, result.error
    assert result.data["menu"] == "MyModMenu"
    assert result.data["cursor"] == 1
    assert result.data["dialog"] == 0
    assert live.sent == [], "a free answer must send nothing"


def test_the_mods_refusal_reaches_the_caller_verbatim(live):
    live.answer = CommandState(
        id="", status="failed",
        detail="ui_tree: no scripted menu is open, so there is no menu root to walk",
        finished_at=1.0,
    )
    result = ui.ui_tree()
    assert not result.ok
    assert "no scripted menu is open" in result.error


# ---------------------------------------------------------------------- load


def test_ui_load_sends_the_layout_and_the_host(live):
    result = ui.ui_load("OpenZone_PDA/gui/layouts/oz_pda_tab.layout", host="60 52")
    assert result.ok, result.error
    sent = live.sent[-1]
    assert sent.verb == "ui_load"
    assert sent.args["layout"] == "OpenZone_PDA/gui/layouts/oz_pda_tab.layout"
    assert sent.args["host"] == "60 52"
    assert "fixture" not in sent.args


def test_ui_load_normalises_backslashes_and_refuses_an_empty_path(live):
    ui.ui_load("OpenZone_PDA\\gui\\layouts\\x.layout")
    assert live.sent[-1].args["layout"] == "OpenZone_PDA/gui/layouts/x.layout"
    result = ui.ui_load("")
    assert not result.ok and "layout" in result.error


def test_a_fixture_dict_travels_as_json_text(live):
    fixture = {"ops": [{"op": "add", "layout": "OpenZone_PDA/gui/layouts/oz_pda_tab.layout", "into": "TabRail", "count": 6}]}
    ui.ui_load("a.layout", fixture=fixture)
    assert json.loads(live.sent[-1].args["fixture"]) == fixture


def test_a_fixture_path_is_read_from_the_project(live, tmp_path):
    from dayz_mcp.tools import session
    root = Path(session.profile().root)
    (root / "preview").mkdir(exist_ok=True)
    (root / "preview" / "tabs.json").write_text('{"ops": [{"op": "hide", "name": "TabHover"}]}', encoding="utf-8")
    ui.ui_load("a.layout", fixture="preview/tabs.json")
    assert json.loads(live.sent[-1].args["fixture"]) == {"ops": [{"op": "hide", "name": "TabHover"}]}


def test_a_broken_fixture_is_refused_before_anything_is_sent(live):
    for bad in ({"nope": []}, {"ops": "x"}, "{not json", "preview/missing.json"):
        result = ui.ui_load("a.layout", fixture=bad)
        assert not result.ok, bad
        assert "fixture" in result.error
    assert live.sent == []


def test_a_fixture_path_outside_the_project_is_refused(live, tmp_path):
    """`root / fixture` discards `root` entirely when `fixture` is itself
    absolute (pathlib's own rule for `/`), so an absolute path used to reach
    straight past the project root onto the real filesystem. The file is real
    and readable -- the refusal has to be about containment, not "not found"."""
    outside = tmp_path / "outside.json"
    outside.write_text('{"ops": [{"op": "hide", "name": "TabHover"}]}', encoding="utf-8")
    result = ui.ui_load("a.layout", fixture=str(outside))
    assert not result.ok
    assert "fixture" in result.error
    assert live.sent == []


def test_a_fixture_path_that_climbs_out_of_the_project_is_refused(live, tmp_path):
    """Same escape, spelled with `..` instead of an absolute path. The project
    root is tmp_path/p, so one level up lands exactly on the file below."""
    outside = tmp_path / "outside.json"
    outside.write_text('{"ops": []}', encoding="utf-8")
    result = ui.ui_load("a.layout", fixture="../outside.json")
    assert not result.ok
    assert "fixture" in result.error
    assert live.sent == []


def test_a_fixture_file_that_cannot_be_decoded_is_reported_not_raised(live):
    root = Path(session.profile().root)
    (root / "preview").mkdir(exist_ok=True)
    (root / "preview" / "bad.json").write_bytes(b"\xff\xfe\x00\x01")

    result = ui.ui_load("a.layout", fixture="preview/bad.json")

    assert not result.ok
    assert "could not be read" in result.error
    assert live.sent == []


def test_ui_load_reports_the_host_rectangle(live):
    live.state = BridgeState(tick=9, session_id="client-1", world={
        "ui_root": "preview", "ui_total": 1, "ui_nodes": [node_line(path="")],
        "ui_host": "0 0 3840 1600",
    })
    result = ui.ui_load("a.layout")
    assert result.data["host"] == (0, 0, 3840, 1600)
    live.state = BridgeState(tick=9, session_id="client-1", world={"ui_total": 0, "ui_nodes": []})
    assert ui.ui_tree(root="preview").data["host"] is None


def test_ui_unload_sends_its_verb(live):
    result = ui.ui_unload()
    assert result.ok
    assert live.sent[-1].verb == "ui_unload" and live.sent[-1].args == {}


def test_the_preview_root_is_accepted_by_the_tree(live):
    ui.ui_tree(root="preview")
    assert live.sent[-1].args["root"] == "preview"


# ------------------------------------------------------------------- preview


def fake_shot_factory(calls):
    def fake_shot(pid, path, rect=None):
        from dayz_mcp.errors import ok as _ok
        calls.append(rect)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(winui.png_bytes(bytes(4 * 2 * 2), 2, 2))
        return _ok({"path": str(path), "width": 2, "height": 2, "bytes": 1, "lit_fraction": 0.5, "foreground": False})
    return fake_shot


def test_ui_preview_loads_shoots_checks_and_reports(live, monkeypatch):
    live.state = BridgeState(tick=9, session_id="client-1", world={
        "ui_root": "preview", "ui_total": 2, "ui_host": "100 50 400 300",
        "ui_nodes": [node_line(path="", cls="FrameWidget", name="Root", rect="100 50 400 300", metrics=""),
                     node_line(path="0", cls="TextWidget", name="Label", rect="110 60 200 20", metrics="240 20")],
    })
    shots = []
    monkeypatch.setattr(winui, "shot", fake_shot_factory(shots))
    result = ui.ui_preview("OpenZone_PDA/gui/layouts/a.layout", name="a")
    assert result.ok, result.error
    assert [c.verb for c in live.sent] == ["ui_load"]
    assert shots == [(100, 50, 400, 300)]
    out = Path(result.data["dir"])
    assert out.name.startswith("preview-a-")
    assert (out / "shot.png").exists() and (out / "report.html").exists()
    assert result.data["issues"] == {"error": 1, "warn": 0}
    assert result.data["count"] == 2 and result.data["total"] == 2
    assert result.data["emulated"] is False


def test_ui_preview_pages_through_a_big_tree(live, monkeypatch):
    first = [node_line(path=str(i), cls="TextWidget", name=f"N{i}", rect=f"{i} 0 10 10") for i in range(300)]
    second = [node_line(path=str(i), cls="TextWidget", name=f"N{i}", rect=f"{i} 0 10 10") for i in range(300, 350)]
    pages = iter([first, second])

    def state_for(_cmd_id, timeout, poll=0.5):
        live.state = BridgeState(tick=9, session_id="client-1", world={
            "ui_root": "preview", "ui_total": 350, "ui_host": "0 0 1000 100", "ui_nodes": next(pages)})
        return CommandState(id=_cmd_id, status="done", detail="ok", finished_at=1.0)

    monkeypatch.setattr(live, "await_result", state_for)
    monkeypatch.setattr(winui, "shot", fake_shot_factory([]))
    result = ui.ui_preview("a.layout")
    assert result.ok, result.error
    assert [(c.verb, c.args.get("offset")) for c in live.sent] == [("ui_load", "0"), ("ui_tree", "300")]
    assert result.data["count"] == 350


def test_ui_preview_live_reads_the_open_menu_instead_of_loading(live, monkeypatch):
    live.state = BridgeState(tick=9, session_id="client-1", world={
        "ui_root": "menu", "ui_total": 1, "ui_host": "",
        "ui_nodes": [node_line(path="", cls="FrameWidget", name="OZ_PdaRoot", rect="0 0 3840 1600")],
    })
    shots = []
    monkeypatch.setattr(winui, "shot", fake_shot_factory(shots))
    result = ui.ui_preview(live=True, name="pda")
    assert result.ok, result.error
    # ui_unload first, so a leftover preview backdrop from an earlier
    # ui_load never sits on top of the menu this call is meant to shoot.
    assert [c.verb for c in live.sent] == ["ui_unload", "ui_tree"]
    assert shots == [(0, 0, 3840, 1600)]


def test_ui_preview_marks_a_host_of_its_own_size_as_emulated(live, monkeypatch):
    live.state = BridgeState(tick=9, session_id="client-1", world={
        "ui_root": "preview", "ui_total": 1, "ui_host": "500 200 1306 518",
        "ui_nodes": [node_line(path="", cls="FrameWidget", name="Root", rect="500 200 1306 518")],
    })
    monkeypatch.setattr(winui, "shot", fake_shot_factory([]))
    result = ui.ui_preview("a.layout", host="1306 518")
    assert result.data["emulated"] is True


def test_ui_preview_normalises_backslashes_so_the_source_is_still_found(live, monkeypatch):
    """A backslash-separated layout path is exactly what ui_load itself
    accepts (it normalises its own copy). _source_for must see the SAME
    normalised string ui_load sees -- otherwise it splits on "/", finds none,
    treats the whole path as a single unmatched mod name, and mis-attributes a
    real project layout as belonging to no mod at all: the "not a mod" note
    would be false, and editbox_bare would be silently skipped even though the
    source is right there."""
    root = Path(session.profile().root)
    layout_dir = root / "MyMod" / "gui" / "layouts"
    layout_dir.mkdir(parents=True, exist_ok=True)
    (layout_dir / "x.layout").write_text(
        "FrameWidgetClass Root {\n size 1 1\n {\n"
        "  EditBoxWidgetClass Field {\n   size 100 20\n  }\n }\n}\n",
        encoding="utf-8",
    )
    live.state = BridgeState(tick=9, session_id="client-1", world={
        "ui_root": "preview", "ui_total": 2, "ui_host": "0 0 200 100",
        "ui_nodes": [node_line(path="", cls="FrameWidget", name="Root", rect="0 0 200 100"),
                     node_line(path="0", cls="EditBoxWidget", name="Field", rect="10 10 100 20")],
    })
    monkeypatch.setattr(winui, "shot", fake_shot_factory([]))
    result = ui.ui_preview("MyMod\\gui\\layouts\\x.layout")
    assert result.ok, result.error
    assert not any("is not a mod" in n for n in result.data["notes"]), result.data["notes"]
    issues_on_disk = json.loads((Path(result.data["dir"]) / "issues.json").read_text(encoding="utf-8"))
    assert any(i["rule"] == "editbox_bare" for i in issues_on_disk)


def test_ui_preview_reports_the_window_scale(live, monkeypatch):
    """spec F1: s = H/1080, read off the live client's own window."""
    live.state = BridgeState(tick=9, session_id="client-1", world={
        "ui_root": "preview", "ui_total": 1, "ui_host": "0 0 400 300",
        "ui_nodes": [node_line(path="", cls="FrameWidget", name="Root", rect="0 0 400 300", metrics="")],
    })
    monkeypatch.setattr(winui, "shot", fake_shot_factory([]))
    monkeypatch.setattr(winui, "find_window", lambda pid: 4242)
    monkeypatch.setattr(winui, "client_size", lambda hwnd: (2560, 1600))
    result = ui.ui_preview("a.layout")
    assert result.ok, result.error
    assert result.data["scale"] == round(1600 / 1080, 4)


def test_ui_preview_falls_back_to_scale_1_with_a_note_when_no_window_is_found(live, monkeypatch):
    live.state = BridgeState(tick=9, session_id="client-1", world={
        "ui_root": "preview", "ui_total": 1, "ui_host": "0 0 400 300",
        "ui_nodes": [node_line(path="", cls="FrameWidget", name="Root", rect="0 0 400 300", metrics="")],
    })
    monkeypatch.setattr(winui, "shot", fake_shot_factory([]))
    monkeypatch.setattr(winui, "find_window", lambda pid: None)
    result = ui.ui_preview("a.layout")
    assert result.ok, result.error
    assert result.data["scale"] == 1.0
    assert any("scale" in n for n in result.data["notes"]), result.data["notes"]


def test_ui_preview_folds_a_warn_lint_finding_into_notes(live, monkeypatch):
    """A WARN from the pre-load lint was computed anyway -- discarding it
    would waste the pass ui_preview already paid for."""
    root = Path(session.profile().root)
    layout_dir = root / "MyMod" / "gui" / "layouts"
    layout_dir.mkdir(parents=True, exist_ok=True)
    (layout_dir / "warn.layout").write_text(
        "FrameWidgetClass Root {\n size 1 1\n {\n"
        "  EditBoxWidgetClass Field {\n   size 100 20\n  }\n }\n}\n",
        encoding="utf-8",
    )
    live.state = BridgeState(tick=9, session_id="client-1", world={
        "ui_root": "preview", "ui_total": 2, "ui_host": "0 0 200 100",
        "ui_nodes": [node_line(path="", cls="FrameWidget", name="Root", rect="0 0 200 100"),
                     node_line(path="0", cls="EditBoxWidget", name="Field", rect="10 10 100 20")],
    })
    monkeypatch.setattr(winui, "shot", fake_shot_factory([]))
    result = ui.ui_preview("MyMod/gui/layouts/warn.layout")
    assert result.ok, result.error
    assert any(n.startswith("lint: MyMod/gui/layouts/warn.layout") and "no style and no panel" in n
               for n in result.data["notes"]), result.data["notes"]


def test_ui_preview_refuses_a_layout_that_would_hang_the_engine(live):
    """A quote inside a text value does not stop THIS project's own parser --
    it just splits the value into more tokens -- but it hangs the ENGINE's,
    so ui_preview must catch it via lint_layout before ui_load ever reaches
    the client, and send nothing at all."""
    root = Path(session.profile().root)
    layout_dir = root / "MyMod" / "gui" / "layouts"
    layout_dir.mkdir(parents=True, exist_ok=True)
    (layout_dir / "hang.layout").write_text(
        'TextWidgetClass Label {\n size 1 1\n text "a "b" c"\n}\n',
        encoding="utf-8",
    )
    result = ui.ui_preview("MyMod/gui/layouts/hang.layout")
    assert not result.ok
    assert "hang.layout:3" in result.error, result.error
    assert "quote" in result.error
    assert live.sent == []


# ------------------------------------------------------------------- gallery


def test_ui_gallery_runs_every_entry_and_writes_an_index(live, monkeypatch, tmp_path):
    from dayz_mcp.tools import session
    root = Path(session.profile().root)
    (root / "preview").mkdir(exist_ok=True)
    (root / "preview" / "index.json").write_text(json.dumps({"entries": [
        {"name": "tab", "layout": "OpenZone_PDA/gui/layouts/oz_pda_tab.layout", "host": "60 52"},
        {"name": "bad", "layout": ""},
    ]}), encoding="utf-8")
    seen = []

    def fake_preview(layout="", fixture=None, host="", live=False, name="", timeout=45.0):
        from dayz_mcp.errors import fail as _fail, ok as _ok
        seen.append((layout, host, name))
        if not layout:
            return _fail("ui_preview needs a layout")
        out = root / ".dayz-mcp" / "shots" / f"preview-{name}-1"
        out.mkdir(parents=True, exist_ok=True)
        (out / "report.html").write_text("r", encoding="utf-8")
        return _ok({"dir": str(out), "shot": str(out / "shot.png"), "report": str(out / "report.html"),
                    "count": 1, "total": 1, "issues": {"error": 0, "warn": 1}, "notes": [], "host": (0, 0, 60, 52), "emulated": True})

    monkeypatch.setattr(ui, "ui_preview", fake_preview)
    result = ui.ui_gallery()
    assert result.ok, result.error
    assert seen == [("OpenZone_PDA/gui/layouts/oz_pda_tab.layout", "60 52", "tab"), ("", "", "bad")]
    assert result.data["failed"] == 1
    index = Path(result.data["index"])
    assert index.exists() and "tab" in index.read_text(encoding="utf-8")


def test_ui_gallery_restarts_the_client_for_each_requested_size(live, monkeypatch):
    from dayz_mcp.tools import session
    root = Path(session.profile().root)
    (root / "preview").mkdir(exist_ok=True)
    (root / "preview" / "index.json").write_text(json.dumps({"entries": [{"name": "t", "layout": "a.layout"}]}), encoding="utf-8")
    calls = []

    def fake_preview(**kw):
        from dayz_mcp.errors import ok as _ok
        calls.append(("preview", kw["name"]))
        return _ok({"dir": str(root), "shot": "", "report": str(root / "r.html"), "count": 0, "total": 0,
                    "issues": {"error": 0, "warn": 0}, "notes": [], "host": None, "emulated": False})

    def fake_restart(size, timeout):
        calls.append(("restart", size))
        return ""

    monkeypatch.setattr(ui, "ui_preview", fake_preview)
    monkeypatch.setattr(ui, "_restart_client", fake_restart)
    result = ui.ui_gallery(sizes=[[3840, 1600], [1920, 1080]])
    assert result.ok, result.error
    assert calls == [("restart", (3840, 1600)), ("preview", "t"), ("restart", (1920, 1080)), ("preview", "t")]
    assert [e["size"] for e in result.data["entries"]] == ["3840x1600", "1920x1080"]


def test_ui_gallery_refuses_a_missing_or_malformed_index(live):
    result = ui.ui_gallery(index="preview/nope.json")
    assert not result.ok and "index" in result.error


def test_ui_gallery_refuses_an_entry_that_is_not_an_object(live):
    """Without this, `entry.get(...)` inside the run loop raises a raw
    AttributeError on a string entry instead of a plain fail()."""
    root = Path(session.profile().root)
    (root / "preview").mkdir(exist_ok=True)
    (root / "preview" / "index.json").write_text(json.dumps({"entries": ["oops"]}), encoding="utf-8")
    result = ui.ui_gallery()
    assert not result.ok
    assert "index" in result.error


def test_ui_gallery_refuses_an_entry_with_no_layout_and_no_live(live):
    root = Path(session.profile().root)
    (root / "preview").mkdir(exist_ok=True)
    (root / "preview" / "index.json").write_text(json.dumps({"entries": [{"name": "x"}]}), encoding="utf-8")
    result = ui.ui_gallery()
    assert not result.ok
    assert "index" in result.error


def test_ui_gallery_refuses_a_malformed_size(live):
    """Without this, `int(s[1])` inside the sizes comprehension raises a raw
    IndexError on a size given as one element instead of two."""
    root = Path(session.profile().root)
    (root / "preview").mkdir(exist_ok=True)
    (root / "preview" / "index.json").write_text(
        json.dumps({"entries": [{"name": "t", "layout": "a.layout"}]}), encoding="utf-8")
    result = ui.ui_gallery(sizes=[[100]])
    assert not result.ok
    assert "sizes" in result.error


def test_restart_client_stops_then_starts_at_the_new_size_and_waits_for_it_to_connect(live, monkeypatch):
    """The success path, through the real function -- nothing about
    _restart_client itself is faked here, only its three collaborators."""
    calls = {}

    def fake_stop():
        from dayz_mcp.errors import ok as _ok
        return _ok({"stopped": True})

    def fake_start(window=None):
        from dayz_mcp.errors import ok as _ok
        calls["window"] = window
        return _ok({"job_id": "j1"})

    def fake_wait(job_id, timeout):
        from dayz_mcp.errors import ok as _ok
        calls["wait"] = (job_id, timeout)
        return _ok({"status": "done", "summary": "connected"})

    monkeypatch.setattr(ui, "client_stop", fake_stop)
    monkeypatch.setattr(ui, "client_start", fake_start)
    monkeypatch.setattr(ui, "job_wait", fake_wait)
    assert ui._restart_client((1920, 1080), 45.0) == ""
    assert calls["window"] == [1920, 1080]
    assert calls["wait"] == ("j1", 240)  # max(45.0, 240) floor


def test_restart_client_waits_for_the_server_to_drop_the_killed_player(live, monkeypatch):
    """G1: client_stop kills the client outright, and the server holds the
    killed player for its own timeout -- a second client started too soon is
    kicked at login. before=1, the poll sees 1 again (not yet dropped), and
    only the THIRD read of _server_players (0, below before) releases the
    wait -- three reads total, and only then does the client start."""
    readings = iter([1, 1, 0])
    calls = []

    def fake_players():
        calls.append("players")
        return next(readings)

    def fake_stop():
        from dayz_mcp.errors import ok as _ok
        calls.append("stop")
        return _ok({"stopped": True})

    def fake_start(window=None):
        from dayz_mcp.errors import ok as _ok
        calls.append("start")
        return _ok({"job_id": "j1"})

    def fake_wait(job_id, timeout):
        from dayz_mcp.errors import ok as _ok
        return _ok({"status": "done"})

    monkeypatch.setattr(ui, "_server_players", fake_players)
    monkeypatch.setattr(ui, "client_stop", fake_stop)
    monkeypatch.setattr(ui, "client_start", fake_start)
    monkeypatch.setattr(ui, "job_wait", fake_wait)
    monkeypatch.setattr(ui.time, "sleep", lambda seconds: None)
    assert ui._restart_client((1920, 1080), 45.0) == ""
    assert calls == ["players", "stop", "players", "players", "start"]


def test_restart_client_gives_up_if_the_server_never_drops_the_player(live, monkeypatch):
    """The wait is not unbounded: a server that keeps reporting the old
    player forever must not block the gallery past RESTART_RELEASE_SECONDS,
    and must never reach client_start."""
    def fake_players():
        return 1  # never drops, however many times it is read

    def fake_stop():
        from dayz_mcp.errors import ok as _ok
        return _ok({"stopped": True})

    started = []

    def fake_start(window=None):
        started.append(window)
        from dayz_mcp.errors import ok as _ok
        return _ok({"job_id": "j1"})

    monkeypatch.setattr(ui, "_server_players", fake_players)
    monkeypatch.setattr(ui, "client_stop", fake_stop)
    monkeypatch.setattr(ui, "client_start", fake_start)
    monkeypatch.setattr(ui.time, "sleep", lambda seconds: None)
    reason = ui._restart_client((1920, 1080), 45.0)
    assert "still reports 1 player(s)" in reason
    assert f"{ui.RESTART_RELEASE_SECONDS}s" in reason
    assert "already in game" in reason
    assert started == []


def test_restart_client_does_not_wait_when_the_server_signal_is_unreadable(live, monkeypatch):
    """before=None means the signal itself is unavailable (most often: the
    bridge is not loaded in the stand) -- not "zero players, safe to go" and
    not "wait and see" either, since a signal that is not there now will not
    become readable by waiting. Starting proceeds immediately, with no wait
    and no second read of _server_players."""
    calls = []

    def fake_players():
        calls.append("players")
        return None

    def fake_stop():
        from dayz_mcp.errors import ok as _ok
        calls.append("stop")
        return _ok({"stopped": True})

    def fake_start(window=None):
        calls.append("start")
        from dayz_mcp.errors import ok as _ok
        return _ok({"job_id": "j1"})

    def fake_wait(job_id, timeout):
        from dayz_mcp.errors import ok as _ok
        return _ok({"status": "done"})

    monkeypatch.setattr(ui, "_server_players", fake_players)
    monkeypatch.setattr(ui, "client_stop", fake_stop)
    monkeypatch.setattr(ui, "client_start", fake_start)
    monkeypatch.setattr(ui, "job_wait", fake_wait)
    assert ui._restart_client((1920, 1080), 45.0) == ""
    assert calls == ["players", "stop", "start"]


def test_restart_client_reports_a_start_that_refused(live, monkeypatch):
    waited = []

    def fake_stop():
        from dayz_mcp.errors import ok as _ok
        return _ok({"stopped": True})

    def fake_start(window=None):
        from dayz_mcp.errors import fail as _fail
        return _fail("no stand")

    def fake_wait(job_id, timeout):
        waited.append((job_id, timeout))
        from dayz_mcp.errors import ok as _ok
        return _ok({"status": "done"})

    monkeypatch.setattr(ui, "client_stop", fake_stop)
    monkeypatch.setattr(ui, "client_start", fake_start)
    monkeypatch.setattr(ui, "job_wait", fake_wait)
    reason = ui._restart_client((1920, 1080), 45.0)
    assert "could not start" in reason and "1920x1080" in reason
    assert waited == []  # a start that never got a job id must not reach job_wait


def test_restart_client_reports_a_client_that_never_connected(live, monkeypatch):
    def fake_stop():
        from dayz_mcp.errors import ok as _ok
        return _ok({"stopped": True})

    def fake_start(window=None):
        from dayz_mcp.errors import ok as _ok
        return _ok({"job_id": "j1"})

    def fake_wait(job_id, timeout):
        from dayz_mcp.errors import ok as _ok
        return _ok({"status": "failed", "error": "died"})

    monkeypatch.setattr(ui, "client_stop", fake_stop)
    monkeypatch.setattr(ui, "client_start", fake_start)
    monkeypatch.setattr(ui, "job_wait", fake_wait)
    reason = ui._restart_client((1920, 1080), 45.0)
    assert "did not connect" in reason


def test_restart_client_does_not_treat_nothing_to_stop_as_a_failure(live, monkeypatch):
    """client_stop answers ok even when this session started no client --
    stopped=False there is a fact about the machine, not a refusal, and the
    restart must still go on to start the client at the new size."""
    started = []

    def fake_stop():
        from dayz_mcp.errors import ok as _ok
        return _ok({"stopped": False, "reason": "no client was started by this session"})

    def fake_start(window=None):
        from dayz_mcp.errors import ok as _ok
        started.append(window)
        return _ok({"job_id": "j1"})

    def fake_wait(job_id, timeout):
        from dayz_mcp.errors import ok as _ok
        return _ok({"status": "done"})

    monkeypatch.setattr(ui, "client_stop", fake_stop)
    monkeypatch.setattr(ui, "client_start", fake_start)
    monkeypatch.setattr(ui, "job_wait", fake_wait)
    assert ui._restart_client((1920, 1080), 45.0) == ""
    assert started == [[1920, 1080]]


def test_ui_gallery_records_a_failed_restart_and_never_calls_preview_that_round(live, monkeypatch):
    """Through ui_gallery, with the real _restart_client -- only its
    collaborators are faked. A restart that cannot even start the client must
    not reach ui_preview at all for that round."""
    from dayz_mcp.tools import session
    root = Path(session.profile().root)
    (root / "preview").mkdir(exist_ok=True)
    (root / "preview" / "index.json").write_text(
        json.dumps({"entries": [{"name": "t", "layout": "a.layout"}]}), encoding="utf-8")

    def fake_stop():
        from dayz_mcp.errors import ok as _ok
        return _ok({"stopped": True})

    def fake_start(window=None):
        from dayz_mcp.errors import fail as _fail
        return _fail("no stand")

    preview_calls = []

    def fake_preview(**kw):
        preview_calls.append(kw.get("name"))
        from dayz_mcp.errors import ok as _ok
        return _ok({"dir": str(root), "shot": "", "report": str(root / "r.html"), "count": 0, "total": 0,
                    "issues": {"error": 0, "warn": 0}, "notes": [], "host": None, "emulated": False})

    monkeypatch.setattr(ui, "client_stop", fake_stop)
    monkeypatch.setattr(ui, "client_start", fake_start)
    monkeypatch.setattr(ui, "ui_preview", fake_preview)
    result = ui.ui_gallery(sizes=[[1920, 1080]])
    assert result.ok, result.error
    assert result.data["failed"] == 1
    assert preview_calls == []
    entry = result.data["entries"][0]
    assert entry["name"] == "(client)" and entry["ok"] is False
    assert "could not start" in entry["error"] and "1920x1080" in entry["error"]


def test_ui_gallery_retries_an_entry_that_hit_a_stalled_heartbeat(live, monkeypatch):
    """The bridge's own movement probe (_require_a_moving_bridge, world.py)
    can miss a 1Hz tick right after the client (re)connects and refuse the
    command -- gone seconds later. A gallery should not lose an entry to
    that: one retry, after GALLERY_RETRY_SECONDS, and the entry that needed
    it says so."""
    root = Path(session.profile().root)
    (root / "preview").mkdir(exist_ok=True)
    (root / "preview" / "index.json").write_text(
        json.dumps({"entries": [{"name": "t", "layout": "a.layout"}]}), encoding="utf-8")
    calls = []
    slept = []

    def fake_preview(**kw):
        from dayz_mcp.errors import fail as _fail, ok as _ok
        calls.append(kw["name"])
        if len(calls) == 1:
            return _fail("the bridge is not ticking (heartbeat='stalled'), so a command sent now "
                        "would sit unclaimed and its result would arrive after this call had given up")
        out = root / ".dayz-mcp" / "shots" / "preview-t-1"
        out.mkdir(parents=True, exist_ok=True)
        (out / "report.html").write_text("r", encoding="utf-8")
        return _ok({"dir": str(out), "shot": str(out / "shot.png"), "report": str(out / "report.html"),
                    "count": 1, "total": 1, "issues": {"error": 0, "warn": 0}, "notes": [],
                    "host": (0, 0, 60, 52), "emulated": False})

    monkeypatch.setattr(ui, "ui_preview", fake_preview)
    monkeypatch.setattr(ui.time, "sleep", lambda seconds: slept.append(seconds))
    result = ui.ui_gallery()
    assert result.ok, result.error
    assert calls == ["t", "t"]
    assert slept == [ui.GALLERY_RETRY_SECONDS]
    entry = result.data["entries"][0]
    assert entry["ok"] is True
    assert entry["retried"] is True


def test_ui_gallery_does_not_retry_a_failure_that_is_not_a_stalled_heartbeat(live, monkeypatch):
    root = Path(session.profile().root)
    (root / "preview").mkdir(exist_ok=True)
    (root / "preview" / "index.json").write_text(
        json.dumps({"entries": [{"name": "t", "layout": "a.layout"}]}), encoding="utf-8")
    calls = []
    slept = []

    def fake_preview(**kw):
        from dayz_mcp.errors import fail as _fail
        calls.append(kw["name"])
        return _fail("ui_preview needs a layout, or live=True to look at the open menu")

    monkeypatch.setattr(ui, "ui_preview", fake_preview)
    monkeypatch.setattr(ui.time, "sleep", lambda seconds: slept.append(seconds))
    result = ui.ui_gallery()
    assert result.ok, result.error
    assert calls == ["t"]
    assert slept == []
    entry = result.data["entries"][0]
    assert entry["ok"] is False
    assert "retried" not in entry


def test_ui_gallery_records_a_still_stalled_entry_after_one_retry(live, monkeypatch):
    root = Path(session.profile().root)
    (root / "preview").mkdir(exist_ok=True)
    (root / "preview" / "index.json").write_text(
        json.dumps({"entries": [{"name": "t", "layout": "a.layout"}]}), encoding="utf-8")
    calls = []

    def fake_preview(**kw):
        from dayz_mcp.errors import fail as _fail
        calls.append(kw["name"])
        return _fail("the bridge is not ticking (heartbeat='stalled'), so a command sent now "
                     "would sit unclaimed and its result would arrive after this call had given up")

    monkeypatch.setattr(ui, "ui_preview", fake_preview)
    monkeypatch.setattr(ui.time, "sleep", lambda seconds: None)
    result = ui.ui_gallery()
    assert result.ok, result.error
    assert calls == ["t", "t"]
    entry = result.data["entries"][0]
    assert entry["ok"] is False
    assert "not ticking" in entry["error"]
    assert entry["retried"] is True


# --------------------------------------------------------------- layout_build


def test_layout_build_generates_the_project_layouts(tmp_path):
    root = make_project(tmp_path / "proj")
    (root / "ui").mkdir()
    (root / "ui" / "tokens.json").write_text(json.dumps({"color": {"text": [1, 1, 1, 1]}, "font": {"body": {"size": 15}}}), encoding="utf-8")
    (root / "ui" / "MyMod").mkdir()
    (root / "ui" / "MyMod" / "oz_page.json").write_text(json.dumps(
        {"layout": "oz_page", "root": {"frame": {"name": "R", "size": [100, 100]}},
         "body": {"label": {"name": "T", "h": 20, "text": "Hi", "color": "$text"}}}), encoding="utf-8")
    session.reset()
    assert tools.project_open(str(root)).ok
    res = tools.layout_build()
    assert res.ok, res.error
    assert res.data["written"] == ["MyMod/gui/layouts/oz_page.layout"]
    assert res.data["descriptions"] == ["ui/MyMod/oz_page.json"]
    assert (root / "MyMod" / "gui" / "layouts" / "oz_page.layout").is_file()
    again = tools.layout_build()
    assert again.data["written"] == [] and again.data["unchanged"] == ["MyMod/gui/layouts/oz_page.layout"]
    assert tools.layout_build(mod="Other").ok is False


def test_layout_build_refuses_with_the_description_and_node(tmp_path):
    root = make_project(tmp_path / "proj")
    (root / "ui").mkdir()
    (root / "ui" / "tokens.json").write_text("{}", encoding="utf-8")
    (root / "ui" / "MyMod").mkdir()
    (root / "ui" / "MyMod" / "bad.json").write_text('{"layout": "bad", "root": {"frame": {"name": "R", "size": [10, 10]}}, "body": {"nope": {}}}', encoding="utf-8")
    session.reset()
    assert tools.project_open(str(root)).ok
    res = tools.layout_build()
    assert not res.ok and "ui/MyMod/bad.json root.0: unknown primitive 'nope'" in res.error
    assert not (root / "MyMod" / "gui").exists()
