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
import textwrap
from pathlib import Path

import pytest

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
              rect="100 200 40 20", depth="2", text="OK"):
    return "|".join([path, cls, name, vis, rect, depth, text])


# ------------------------------------------------------------------ parsing


def test_a_node_line_becomes_its_fields():
    parsed = ui._node(node_line())
    assert parsed == {
        "path": "0.1", "class": "ButtonWidget", "name": "ok_button",
        "visible": True, "shown": True, "rect": "100 200 40 20",
        "depth": 2, "text": "OK",
    }


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
    assert live.sent[-1].args == {"root": "screen", "depth": "4", "limit": "25"}


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
