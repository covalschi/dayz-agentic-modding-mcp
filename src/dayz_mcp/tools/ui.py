"""The client's own interface, read and driven through the bridge's client half.

Until now the client was visible only as pixels: a screenshot to look at, a
gamepad walking menus blind, and "press that button" solved by guessing
coordinates. This reads the real widget tree instead -- the same engine classes
whatever mod drew it -- so a button is addressed by what it IS rather than by
where it seemed to be in a picture.

THREE LIMITS THE ENGINE IMPOSES, all read in the game's own sources and none of
them worked around here, because a tool that pretends to do what it cannot is
worse than one that says so:

1. **A plain `TextWidget` cannot be read.** `GetText` is declared exactly three
   times in `enwidgets.c` -- on `EditBoxWidget`, `MultilineEditBoxWidget` and
   `ButtonWidget`. The label a mod draws its numbers into has `SetText` and no
   getter. So `text` comes back empty for labels, and what a mod's UI MEANS
   stays a question for the server-side bridge, where the data is real.
2. **A script-level click reaches only the open scripted menu.** `Widget` has
   `SetHandler` and no `GetHandler`, so an arbitrary HUD widget's own handler
   cannot be reached from script at all. `via="cursor"` exists for everything
   else: it puts the REAL mouse on the rectangle the tree reports.
3. **The client must load the bridge.** One pbo carries both halves, but a
   profile that lists it under `mods.server_only` keeps it off the client's
   `-mod` line -- and then every tool here would answer with an empty tree,
   which is indistinguishable from "this mod has no interface". That case is
   refused by name instead.

These tools HAVE been run against a live client: the window scale (s = H/1080,
exact), the zero-rectangle-before-the-first-layout-pass rule, the 300-node walk
ceiling and the post-connect probe miss are all measurements taken on the stand
on 2026-09-03/04, and the constants below cite them where they are set. What is
still assumed rather than measured is called out at the line that assumes it.
"""
from __future__ import annotations

import itertools
import json
import time
from pathlib import Path

from .. import uicheck, uireport
from .. import winui
from ..bridge.channel import CLIENT_CMD_FILENAME, CLIENT_STATE_FILENAME, Channel
from ..errors import Result, fail, ok
from ..layoutgen import LAYOUT_DIR, LayoutGenError, build_project
from ..layoutlint import lint_layout
from ..layoutparse import LayoutSyntaxError, parse_layout
from ..lint import REFUSE, WARN
from ..procs import is_alive
from ..profile import resolve_mod_dir
from ..uigeom import parse_rect
from . import session
from .client import (
    ENGINE_LANGUAGES, _players_in, client_language, client_profiles_dir, client_start,
    client_stop, language_name, window_size,
)
from .jobs_api import job_wait
from .lifecycle import server_profiles_dir
from .project import require_project
from .world import WORLD_TIMEOUT_SECONDS, _args, _require_a_moving_bridge, _wire_args

#: The client half's own ceilings, kept in step with DZMCP_Ui in the mod. A
#: value above these is clamped there rather than refused -- these are page
#: sizes, where "as much as you can" is a clear intent.
NODES_MAX = 300
DEPTH_MAX = 32

#: Fields of one described node. The bridge's own Describe() always sends
#: eight -- path|class|name|flags|rect|depth|text|metrics -- with metrics
#: empty for a widget that does not derive from TextWidget: the field is
#: always appended, never omitted (measured 2026-09-03). Seven is not a
#: second normal shape; it is what an OLDER, not yet rebuilt bridge still on
#: disk sends, accepted here so a stale @DZMCP_Bridge turns into a node this
#: can still read rather than an unparsed "raw" line.
_FIELDS_MIN = 7
_FIELDS_MAX = 8


def _client_channel() -> Channel:
    """The client half's transport: its own profile directory AND its own file
    names. Two bridges pointed at one directory would otherwise claim each
    other's mail with no error anywhere."""
    return Channel(
        client_profiles_dir(),
        cmd_name=CLIENT_CMD_FILENAME,
        state_name=CLIENT_STATE_FILENAME,
    )


def _live_client() -> tuple[int, bool]:
    pid = session.client_pid()
    return pid, bool(pid and is_alive(pid, image=session.client_image()))


def _window_scale(pid: int) -> tuple[float, str]:
    """The live client window's layout-unit-to-pixel ratio: s = H/1080,
    exact (spec F1, measured 2026-09-03). 1.0 with a note when there is no
    window to measure it from -- the client is not up, or has not opened a
    window the OS will report a client area for yet."""
    hwnd = winui.find_window(pid) if pid else None
    if not hwnd:
        return 1.0, "scale: no client window found, assuming 1.0 (100 layout units = 100 px)"
    _w, h = winui.client_size(hwnd)
    if not h:
        return 1.0, "scale: the client window reported no height, assuming 1.0"
    return round(h / 1080, 4), ""


def _no_client() -> Result:
    return fail(
        "no client started by this session is running, so there is no interface to read",
        hint="start one with client_start and wait for it to connect, then try again",
    )


def _not_loaded(channel: Channel) -> Result:
    """The client is up but has never published a state.

    Named rather than answered with an empty tree, because an empty tree reads
    as "this mod draws nothing" -- and the commonest cause is a profile that
    lists the bridge under `mods.server_only`, which keeps it off the client's
    own `-mod` line while the server half works perfectly.
    """
    return fail(
        "the client is running but its half of the bridge has never published anything",
        hint="the likeliest cause is the profile: a mod listed under mods.server_only is "
             "routed to -serverMod and the CLIENT never loads it. One pbo carries both "
             "halves, so take @DZMCP_Bridge out of mods.server_only and leave it in "
             f"mods.extra. The state file it would write is {channel._state_path()}",
    )


def _run(verb: str, args: dict, timeout: float, offset: int = 0) -> Result:
    """One command to the client half, and its own answer back.

    Deliberately the same shape as the world tools' `_run`, and deliberately
    not the same function: that one talks to the server's channel and reads the
    server's pid, and a single function switching on which half it meant would
    be one edit away from sending a UI command into the world.
    """
    guard = require_project()
    if guard:
        return guard

    pid, alive = _live_client()
    if not alive:
        return _no_client()

    channel = _client_channel()
    if channel.current_session_id() is None:
        return _not_loaded(channel)

    not_moving = _require_a_moving_bridge(channel)
    if not_moving:
        return not_moving

    try:
        wire = _wire_args(args)
    except ValueError as exc:
        return fail(str(exc), hint="pass numbers, booleans or strings")

    built = channel.build_command(verb, wire)
    if not built.ok:
        return built
    command = built.data

    sent = channel.send(command, is_alive=alive)
    if not sent.ok:
        return sent

    state = channel.await_result(command.id, timeout=timeout, poll=0.25)
    if state is None:
        return fail(
            f"no answer for {verb} within {timeout:g}s, and the client's bridge never "
            f"reported on command {command.id} at all",
            hint="check that the client is still up and that its tick is moving -- the "
                 "client half arms the same 1 Hz call the server half does, but from a "
                 "different mission",
        )

    payload = {
        "verb": verb,
        "command_id": command.id,
        "status": state.status,
        "detail": state.detail,
        "args": wire,
    }
    if state.status == "done":
        return _with_ui(ok(payload), channel, offset)
    if state.status == "failed":
        return Result(False, payload, state.detail or f"{verb} failed",
                      hint="the mod's own words are in the error above; a refusal is a "
                           "result, not a malfunction")
    return Result(
        False, payload, f"{verb} was still {state.status} after {timeout:g}s",
        hint="the client half carries the same 30s hard limit as the server half, so a "
             "command still running past this wait means the tick itself stalled",
    )


def _with_ui(answered: Result, channel: Channel, offset: int = 0) -> Result:
    """Add the client's published UI block, and the nodes parsed out of it."""
    state = channel.read_state_tolerant()
    if state is None:
        answered.data["ui"] = {}
        answered.data["ui_unavailable"] = (
            "the command finished, but no readable state has been published since"
        )
        return answered

    block = state.world or {}
    answered.data["ui"] = block
    answered.data["tick"] = state.tick
    nodes = [_node(line) for line in block.get("ui_nodes", []) if isinstance(line, str)]
    answered.data["nodes"] = nodes
    answered.data["count"] = len(nodes)
    answered.data["total"] = block.get("ui_total", -1)
    # How many nodes PASSED THE FILTER, when there was one. A filtered walk
    # has two counts -- visited and matched -- and reporting the first as if
    # it were the second made `truncated` claim there was more of the answer
    # to fetch after every ui_find that walked more nodes than it matched.
    # -1 (or absent, from a bridge on disk that predates the field) means
    # "nothing filtered", and the page is measured against the visit count as
    # it always was.
    matched = block.get("ui_matched", -1)
    answered.data["matched"] = matched
    countable = matched if isinstance(matched, int) and matched >= 0 else answered.data["total"]
    answered.data["offset"] = offset
    answered.data["truncated"] = (
        isinstance(countable, int) and countable > offset + len(nodes)
    )
    answered.data["host"] = _rect(block.get("ui_host", ""))
    return answered


def _node(line: str) -> dict:
    parts = line.split("|")
    if len(parts) < _FIELDS_MIN or len(parts) > _FIELDS_MAX:
        return {"raw": line}
    flags = parts[3]
    text_size = None
    if len(parts) == _FIELDS_MAX and parts[7].strip():
        halves = parts[7].split()
        if len(halves) == 2 and all(h.lstrip("-").isdigit() for h in halves):
            text_size = (int(halves[0]), int(halves[1]))
    return {
        "path": parts[0],
        "class": parts[1],
        "name": parts[2],
        "visible": flags[:1] == "1",
        "shown": flags[1:2] == "1",
        "rect": parts[4],
        "depth": int(parts[5]) if parts[5].lstrip("-").isdigit() else parts[5],
        "text": parts[6],
        # Engine pixels of the rendered text, for widgets that derive from
        # TextWidget (labels, multiline, rich text, multiline edit boxes).
        # EditBoxWidget and ButtonWidget extend UIWidget and report nothing.
        "text_size": text_size,
    }


def _centre(rect: str) -> tuple[int, int] | None:
    """The centre of an `x y w h` rectangle, or None if it is not one."""
    parsed = parse_rect(rect)
    if parsed is None:
        return None
    x, y, w, h = parsed
    return x + w // 2, y + h // 2


#: Kept under this name because callers in this module and its tests already
#: use it; the parsing itself lives in uigeom, shared with uicheck and
#: uireport so "split 'x y w h' into four ints" exists in exactly one place.
_rect = parse_rect


#: The engine truncates a JSON string longer than this, silently (measured
#: 2026-09-04: a 953-byte minified fixture file, re-serialised at 1029 bytes
#: with Python's default separators, arrived cut). Everything below is what
#: keeps a fixture under it: compact separators, non-ASCII left unescaped
#: (one UTF-8 character instead of six `\uXXXX` ones), and a note before the
#: cliff rather than a mystery at the far end of the bridge.
FIXTURE_LIMIT_BYTES = 1023
FIXTURE_NOTE_BYTES = 1000


def _fixture_text(fixture, root: Path) -> tuple[str | None, str, list[str]]:
    """The fixture as the JSON text the mod will parse, an error, and notes.

    A dict is serialised; a string is a project-relative path to a JSON file,
    or JSON text itself when it starts with `{`. Validated HERE, before the
    round trip: the mod's own refusal costs a tick and names less. Text that
    is already JSON is re-serialised the same compact way, so a hand-minified
    file does not get 8% of spaces added back on its way out.
    """
    if fixture is None:
        return None, "", []
    if isinstance(fixture, str):
        if fixture.lstrip().startswith("{"):
            text = fixture
        else:
            root_resolved = root.resolve()
            path = (root / fixture).resolve()
            if not path.is_relative_to(root_resolved):
                return None, f"fixture path must stay inside the project: {path}", []
            if not path.is_file():
                return None, f"fixture file not found: {path}", []
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                return None, f"fixture file could not be read: {path}: {exc}", []
        try:
            fixture = json.loads(text)
        except ValueError as exc:
            return None, f"fixture is not valid JSON: {exc}", []
    if not isinstance(fixture, dict) or not isinstance(fixture.get("ops"), list):
        return None, 'fixture must be an object with an "ops" list', []
    for index, op in enumerate(fixture["ops"]):
        if not isinstance(op, dict) or not isinstance(op.get("op"), str):
            return None, f'fixture op {index} must be an object with an "op" string', []
    out = json.dumps(fixture, ensure_ascii=False, separators=(",", ":"))
    size = len(out.encode("utf-8"))
    notes = []
    if size > FIXTURE_NOTE_BYTES:
        notes.append(f"fixture is {size} bytes; the engine truncates a JSON string "
                     f"above {FIXTURE_LIMIT_BYTES} -- shorten it")
    return out, "", notes


# ------------------------------------------------------------------ the tools


def ui_menu() -> Result:
    """What the client's interface is doing right now.

    Free: the client half republishes the open menu's class, whether the cursor
    is visible and whether a modal dialog is up on every tick, so this answer is
    already on disk and costs no command round trip -- the same bargain
    `world_state` makes with no arguments.
    """
    guard = require_project()
    if guard:
        return guard
    pid, alive = _live_client()
    if not alive:
        return _no_client()

    channel = _client_channel()
    state = channel.read_state_tolerant()
    if state is None:
        return _not_loaded(channel)

    block = state.world or {}
    return ok({
        "tick": state.tick,
        "session_id": state.session_id,
        "menu": block.get("ui_menu", ""),
        "cursor": block.get("ui_cursor", -1),
        "dialog": block.get("ui_dialog", -1),
        "errors": state.errors,
    })


#: What a `root` argument may say, in one place because five tools take it.
#:
#:   "menu"       the open scripted menu's layout root (the default)
#:   "screen"     the workspace root -- everything on screen
#:   "workspace"  the same widget, under the name that says what it IS: the
#:                parent of every top-level window, menus and non-menus alike
#:   "preview"    the host ui_load puts a layout under
#:   anything else: a WIDGET NAME, resolved by the client against the whole
#:                workspace tree. A window a mod hangs off the workspace root
#:                is not a menu and has no reserved word of its own; its name
#:                is the only handle a caller has.
ROOTS = ("menu", "screen", "workspace", "preview")


def ui_tree(root: str = "menu", depth: int = DEPTH_MAX, limit: int = NODES_MAX,
            offset: int = 0, timeout: float = WORLD_TIMEOUT_SECONDS) -> Result:
    """The client's widget tree: what is on screen, as the engine holds it.

    `root` is `"menu"` (the open scripted menu, the default), `"screen"` or
    `"workspace"` (the whole workspace -- the same widget under two names,
    the second saying what it is: the parent of every top-level window,
    menus and non-menus alike), `"preview"` (the host `ui_load` uses), or
    THE NAME OF A WIDGET, which the client resolves against the whole
    workspace tree. That last one is how a window a mod hangs off the
    workspace root -- not a menu, no reserved word of its own -- is addressed
    at all. Each node comes back with its path, class, name, visibility,
    screen rectangle, depth, text and -- for widgets that derive from
    `TextWidget` -- the rendered text size in engine pixels.

    The answer is A PAGE: `total` is how many nodes the walk visited, `count`
    is how many this page listed, and `offset` is how many were skipped before
    it (0 by default, a page after the first). `truncated` says whether more
    remain after this page -- `total > offset + count`. A shorter list that did
    not say so would read as the whole interface.
    """
    return _run("ui_tree", _args(root=root, depth=depth, limit=limit, offset=offset),
                timeout, offset)


def ui_find(name: str = "", class_name: str = "", text: str = "",
            root: str = "menu", depth: int = DEPTH_MAX, limit: int = NODES_MAX,
            offset: int = 0, timeout: float = WORLD_TIMEOUT_SECONDS) -> Result:
    """Find widgets by name, class or text, without fetching the whole tree.

    `name` and `class_name` match exactly; `text` matches as a substring,
    because a label's exact string is the one thing a caller rarely knows in
    advance. At least one of the three is required -- with none of them this
    would be `ui_tree`, and answering it as such would hide which question was
    actually asked.

    Filtering happens in the client, not here: sending the whole tree back so it
    could be filtered locally is exactly what the page limit exists to avoid.
    It also happens DURING the walk, before the 300-node ceiling, so a name
    is found wherever it is in the tree rather than only in the first page
    (a window 2300 nodes into the workspace needed `offset=2300` to be seen
    at all -- measured 2026-09-06). `total` is how many nodes were walked and
    `matched` how many passed the filter; `limit` and `offset` page over the
    MATCHES, and `truncated` is measured against them.

    `root` takes the whole vocabulary `ui_tree` documents, including a
    widget's own name -- so a search can be narrowed to one window.
    """
    if not (name or class_name or text):
        return fail(
            "ui_find needs at least one of name, class_name or text",
            hint="with none of them this is ui_tree -- call that instead",
        )
    return _run(
        "ui_find",
        _args(**{"name": name or None, "class": class_name or None,
                 "text": text or None, "root": root, "depth": depth, "limit": limit,
                 "offset": offset}),
        timeout,
        offset,
    )


def ui_click(path: str, expect_name: str = "", expect_class: str = "",
             via: str = "script", root: str = "menu", button: int = 0,
             timeout: float = WORLD_TIMEOUT_SECONDS) -> Result:
    """Press a widget, by path, having checked it is still the one meant.

    `expect_name` and `expect_class` are how a path stops being a loaded gun. A
    tree walked a minute ago is not the tree in front of the mouse now, and
    pressing "whatever is at 0.3.1 today" is how an automated run presses the
    wrong button and reports success. Both are optional -- sometimes the caller
    genuinely means "whatever is there" -- but then that is the caller's own
    decision, taken in the open.

    TWO TRACTS, and the answer always says which one was used:

    * `via="script"` delivers the click to the open menu's own handler. Works
      with the client in the background, no focus taken. Reaches ONLY the open
      scripted menu: `Widget` has no `GetHandler`, so a HUD widget's own handler
      is not reachable from script at all.
    * `via="cursor"` puts the real mouse on the widget's rectangle and clicks.
      Reaches anything the player could click -- and TAKES THE FOREGROUND, like
      `client_type`, because a real click goes wherever the real cursor is.

    A handler that returns false is reported as what it is: the click was
    delivered and the menu did not act on it. That is a fact about the mod, not
    a failure of this tool, and the answer says so rather than inventing a
    verdict.

    THE CURSOR TRACT ANSWERS FOR ITS OWN CLICK. It asks the client what
    happened AFTER the mouse moved (`ui_cursor`): `under_cursor` and `hit` say
    what the cursor ended up on, `menu_before`/`menu_after` and
    `windows_before`/`windows_after` say what the screen did, and `changed`
    says whether anything did. The path-resolving probe's own sentence stays,
    under `resolve_detail`, but it no longer speaks for the click -- it was
    written before the mouse moved, and passing it on is how a click that
    opened an admin window came back saying "nothing was pressed".
    """
    if via not in ("script", "cursor"):
        return fail(
            f"{via!r} is not a click tract",
            hint='use via="script" to deliver through the open menu\'s handler (no focus '
                 'needed, menu only), or via="cursor" for a real mouse click (takes the '
                 "foreground, reaches anything)",
        )
    if via == "script":
        answered = _run(
            "ui_click",
            _args(path=path, expect_name=expect_name or None,
                  expect_class=expect_class or None, root=root, button=button),
            timeout,
        )
        if answered.ok:
            answered.data["via"] = "script"
        return answered

    # The cursor tract resolves the path through the SAME validation the script
    # tract uses -- one definition of "is this still the widget I meant" rather
    # than two that could drift apart -- and then clicks where the client says
    # the widget is.
    found = _run(
        "ui_click",
        _args(path=path, expect_name=expect_name or None,
              expect_class=expect_class or None, root=root, deliver="none"),
        timeout,
    )
    if not found.ok:
        return found

    nodes = found.data.get("nodes") or []
    if not nodes or "rect" not in nodes[0]:
        return Result(
            False, found.data,
            "the client resolved the path but published no rectangle for it, so there is "
            "nowhere to put the cursor",
            hint="call ui_tree and check the node has a screen rectangle",
        )
    centre = _centre(nodes[0]["rect"])
    if centre is None:
        return Result(
            False, found.data,
            f"the client published {nodes[0]['rect']!r} as the widget's rectangle, which is "
            "not an 'x y w h' rectangle",
            hint="this is a bug in the bridge's own listing, not in the caller's arguments",
        )

    target = nodes[0]
    before = found.data.get("ui") or {}
    # Kept, not thrown away: it is the PROBE's own true sentence ("nothing was
    # pressed" -- nothing had been, at that point), and it stops being the
    # whole call's verdict below rather than stopping being said.
    found.data["resolve_detail"] = found.data.get("detail", "")
    found.data["menu_before"] = before.get("ui_menu", "")
    found.data["windows_before"] = before.get("ui_windows", -1)

    pid, _alive = _live_client()
    clicked = winui.click(pid, centre[0], centre[1])
    found.data["via"] = "cursor"
    found.data["clicked_at"] = {"x": centre[0], "y": centre[1]}
    found.data["click"] = clicked.data
    if not clicked.ok:
        return Result(False, found.data, clicked.error, clicked.hint)
    return _with_click_verdict(found, target, timeout)


def _with_click_verdict(found: Result, target: dict, timeout: float) -> Result:
    """What the real click actually did, asked AFTER it happened.

    The probe that resolved the path answered before the mouse moved, so its
    sentence cannot speak for the click -- and it was being handed on as if it
    could, which is how a click that opened a whole admin window came back as
    "nothing was pressed" (measured 2026-09-06, task-57c). Three observations
    replace that claim, and every one of them is the client's, not this
    module's: what the cursor is over now, what menu is open now, and how many
    top-level widgets the workspace holds now.

    The last one is the load-bearing one for a window that is NOT a scripted
    menu -- VPP's admin panel is created under the workspace root, so the open
    menu class is identical before and after a click that plainly opened it.
    """
    probe = _run("ui_cursor", {}, timeout)
    at = found.data["clicked_at"]
    pressed = (f"clicked {target.get('class', '?')} '{target.get('name', '')}' at "
               f"{found.data['args'].get('path', '')} with the real mouse at "
               f"({at['x']}, {at['y']})")
    if not probe.ok:
        found.data["under_cursor"] = None
        found.data["hit"] = None
        found.data["menu_after"] = ""
        found.data["windows_after"] = -1
        found.data["changed"] = None
        found.data["check_unavailable"] = probe.error
        found.data["detail"] = (
            f"{pressed}; what it did could not be checked afterwards: {probe.error}"
        )
        return found

    after = probe.data.get("ui") or {}
    under = (probe.data.get("nodes") or [None])[0]
    found.data["cursor_detail"] = probe.data.get("detail", "")
    found.data["under_cursor"] = under
    found.data["hit"] = bool(under) and all(
        under.get(field) == target.get(field) for field in ("class", "name", "rect")
    )
    found.data["menu_after"] = after.get("ui_menu", "")
    found.data["windows_after"] = after.get("ui_windows", -1)
    found.data["changed"] = (
        found.data["menu_after"] != found.data["menu_before"]
        or found.data["windows_after"] != found.data["windows_before"]
    )

    said = [pressed]
    if under:
        said.append(f"the cursor is now over {under.get('class', '?')} '{under.get('name', '')}'")
    else:
        said.append("nothing is under the cursor now")
    if found.data["menu_after"] != found.data["menu_before"]:
        said.append(f"the open menu went from {found.data['menu_before'] or '(none)'} "
                    f"to {found.data['menu_after'] or '(none)'}")
    else:
        said.append(f"the open menu is still {found.data['menu_before'] or '(none)'}")
    if found.data["windows_after"] != found.data["windows_before"]:
        said.append(f"top-level widgets {found.data['windows_before']} -> "
                    f"{found.data['windows_after']}")
    else:
        said.append(f"top-level widgets unchanged at {found.data['windows_before']}")
    if not found.data["changed"]:
        said.append("so nothing changed that this can see -- the widget may act on a "
                    "different event, or on release")
    found.data["detail"] = "; ".join(said)
    return found


def ui_cursor(timeout: float = WORLD_TIMEOUT_SECONDS) -> Result:
    """What the real mouse is over right now, and what the screen holds.

    The widget under the cursor comes from the engine's own
    `GetWidgetUnderCursor`, so it is a hit test rather than a rectangle
    compared here -- which is what makes it worth a round trip. Beside it:
    the open menu's class and how many top-level widgets the workspace has,
    the pair `ui_click(via="cursor")` uses to say whether its click did
    anything. A window that is not a scripted menu (a mod hanging a panel off
    the workspace root) moves the second number and not the first.
    """
    answered = _run("ui_cursor", {}, timeout)
    if not answered.ok:
        return answered
    block = answered.data.get("ui") or {}
    answered.data["under_cursor"] = (answered.data.get("nodes") or [None])[0]
    answered.data["menu"] = block.get("ui_menu", "")
    answered.data["windows"] = block.get("ui_windows", -1)
    return answered


def ui_text(path: str, text: str, expect_name: str = "", expect_class: str = "",
            root: str = "menu", timeout: float = WORLD_TIMEOUT_SECONDS) -> Result:
    """Write into an edit box, and read it back.

    Only a field the player could type into may be written. A plain
    `TextWidget` has `SetText` too, but writing a mod's label from outside would
    change what the player sees without changing anything the mod believes -- a
    lie drawn on the screen -- so it is refused rather than quietly allowed.

    The value is read back out of the widget before the answer is returned:
    `SetText` is native and returns nothing, so "it was set" would otherwise be
    this tool's own claim about itself.
    """
    return _run(
        "ui_text",
        _args(path=path, text=text, expect_name=expect_name or None,
              expect_class=expect_class or None, root=root),
        timeout,
    )


def ui_load(layout: str, fixture: dict | str | None = None, host: str = "",
            depth: int = DEPTH_MAX, limit: int = NODES_MAX, offset: int = 0,
            timeout: float = WORLD_TIMEOUT_SECONDS) -> Result:
    """Show a layout file in the client, under a host of the bridge's own, and
    list what the engine made of it.

    `layout` is the path CreateWidgets takes: relative to the pbo prefix, with
    forward slashes. `host` is "w h" in layout units to emulate a screen of
    that size (empty: the whole screen). `fixture` fills the layout the way a
    mod's script would -- rows into a container, texts, visibility -- as a
    dict, JSON text, or a project-relative path to a JSON file; the operations
    are the bridge's (add, text, show, hide, color).

    The answer arrives a tick later than the command: a widget measured before
    its first layout pass reports zeros, so the mod waits one. The preview
    stays on screen for client_shot until ui_unload or the next ui_load; the
    HUD is hidden meanwhile.
    """
    guard = require_project()
    if guard:
        return guard
    layout = (layout or "").replace("\\", "/").strip()
    if not layout:
        return fail("ui_load needs a layout path",
                    hint="relative to the pbo prefix, e.g. MyMod/gui/layouts/x.layout")
    text, error, fixture_notes = _fixture_text(fixture, Path(session.profile().root))
    if error:
        return fail(error, hint='a fixture is {"ops": [{"op": "add", "layout": "...", "into": "...", "count": 3}, ...]}')
    args = _args(layout=layout, host=host or None, fixture=text, depth=depth, limit=limit, offset=offset)
    result = _run("ui_load", args, timeout, offset)
    if fixture_notes and result.ok and isinstance(result.data, dict):
        result.data["notes"] = list(result.data.get("notes") or []) + fixture_notes
    return result


def ui_unload(timeout: float = WORLD_TIMEOUT_SECONDS) -> Result:
    """Remove the preview ui_load put up, and give the HUD back."""
    return _run("ui_unload", {}, timeout)


def _collect_nodes(root: str, first: Result, timeout: float) -> tuple[list[dict], int]:
    """Every node of the tree, paging past the mod's 300-node ceiling."""
    nodes = list(first.data.get("nodes", []))
    total = first.data.get("total", -1)
    while isinstance(total, int) and total > len(nodes):
        page = ui_tree(root=root, offset=len(nodes), timeout=timeout)
        if not page.ok or not page.data.get("nodes"):
            break
        nodes += page.data["nodes"]
        total = page.data.get("total", total)
    return nodes, total


def _source_for(layout: str):
    """The layout's source in this project: `(node, text, why)`.

    `node` is the parsed tree, for the checks that need it (a style on an
    edit box); `text` is the raw file, for the checks that need THAT instead
    (lint_layout re-parses it itself, and catches what this function's own
    parse attempt does not survive). Both come back None with a reason when
    there is nothing to read at all -- not this project's file (a vanilla
    layout, another mod's), or no file at that path.
    """
    prof = session.profile()
    head, _, rest = layout.partition("/")
    if head not in prof.build.mods:
        return None, None, f"{head!r} is not a mod of this project, so the source was not read"
    path = resolve_mod_dir(prof.root, prof.build.sources, head) / rest
    if not path.is_file():
        return None, None, f"source not found at {path}"
    text = path.read_text(encoding="utf-8", errors="replace")
    try:
        return parse_layout(text), text, ""
    except LayoutSyntaxError as exc:
        return None, text, f"source does not parse: {exc}"


def _fixture_sources(fixture, root: Path) -> tuple[list, list[str]]:
    """The parsed sources of every layout a fixture's `add` ops bring in,
    for the checks to judge fixture rows by their own flags. Unreadable or
    foreign layouts are named in the notes, never fatal: the fixture itself
    is validated by the bridge."""
    text, _error, size_notes = _fixture_text(fixture, root)
    if not text:
        return [], list(size_notes)
    try:
        ops = json.loads(text).get("ops", [])
    except (json.JSONDecodeError, AttributeError):
        return [], list(size_notes)
    found, notes, seen = [], list(size_notes), set()
    for op in ops if isinstance(ops, list) else []:
        layout = op.get("layout") if isinstance(op, dict) and op.get("op") == "add" else None
        if not isinstance(layout, str) or layout in seen:
            continue
        seen.add(layout)
        node, _text, why = _source_for(layout.replace("\\", "/"))
        if node is None:
            notes.append(f"fixture row {layout}: {why}")
        else:
            found.append(node)
    return found, notes


def _live_sources(prof) -> tuple[list, list[str]]:
    """Every `.layout` this project declares, parsed, for `live=True` to
    hand `uicheck.check` as `sources`. A live walk has no ONE layout loaded
    the way `_source_for` reads for a real ui_load -- it looks at whatever
    menu is already open -- so the whole project stands in for it: a widget
    the engine reports is looked up by NAME across every file instead of by
    one page's own path (see `uicheck.check`'s `by_root`/`by_name_all` for
    how a name shared by several files is then resolved).

    Walks each of `[build] mods`' own `gui/layouts` -- not the whole mod
    tree, just where a layout actually lives (LAYOUT_DIR, the same constant
    layout_build targets). A mod with no such folder yet is skipped, not an
    error. A file that fails to READ or PARSE is skipped and named rather
    than raised: one stray syntax error two mods over must never blind the
    checks to every OTHER layout -- unlike `live=False`, nothing here is
    about to hang the engine's own parser, so there is no REFUSE to stop
    for. Every skipped file travels in the returned note, by name and by
    which of the two it was, never silently dropped.
    """
    found: list = []
    bad: list[str] = []
    loaded = 0
    for mod in prof.build.mods:
        layout_dir = resolve_mod_dir(prof.root, prof.build.sources, mod) / LAYOUT_DIR
        if not layout_dir.is_dir():
            continue
        for path in sorted(layout_dir.glob("*.layout")):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                bad.append(f"{path.name}: unreadable")
                continue
            try:
                found.append(parse_layout(text))
            except LayoutSyntaxError as exc:
                bad.append(f"{path.name}: does not parse ({exc})")
                continue
            loaded += 1
    note = f"live sources: {loaded} layout{'' if loaded == 1 else 's'}"
    if bad:
        note += f", {len(bad)} unreadable: " + ", ".join(bad)
    return found, [note]


def ui_preview(layout: str = "", fixture: dict | str | None = None, host: str = "",
               live: bool = False, name: str = "", root: str = "",
               timeout: float = WORLD_TIMEOUT_SECONDS) -> Result:
    """A layout as the engine draws it: screenshot, every widget's rectangle,
    and the checks over those rectangles, written to one folder with an HTML
    report.

    `live=False` loads `layout` through ui_load (with `fixture` and `host` as
    there) and shoots the preview host. `live=True` loads nothing: it walks a
    root that is already on screen and shoots its rectangle -- the way to look
    at the real PDA with real data. `root` chooses which: empty (the open
    scripted menu, as before), `"workspace"` for every top-level widget at
    once, or A WINDOW'S NAME, which is the only way to reach a panel a mod
    created under the workspace root rather than as a menu. It is ignored
    with `live=False`, where the loaded preview host is the root by
    definition. A host of its own size is an emulation of a screen that
    size and the report says so; the real check is the real window size.
    `live=True` still gives the checks something to judge a self-sized label
    against: `sources` is built from every `.layout` the open project
    declares (`_live_sources`), and `notes` says how many were read.

    `live=False` LINTS the source before ever reaching ui_load: a quote
    inside a text value parses fine here (it just splits into more tokens)
    but hangs the ENGINE's own layout parser, and a hung client answers
    nothing for every tool afterwards. Any REFUSE-severity finding stops the
    call before anything is sent -- fix it, or run mod_lint first. A WARN
    finding does not stop the call, but is folded into `notes` rather than
    thrown away.

    `data["scale"]` is the layout-unit-to-pixel ratio read off the live
    client's own window (s = H/1080, spec F1) -- 1.0, with a note in
    `notes`, when there is no window to measure. It is what `uicheck` uses
    to turn a tolerance measured in layout units (the scrollbar's width, a
    border panel's overhang) into the screen pixels this report compares.

    `data["language"]` is the client's current UI language (`client_language`,
    read out of its own DayZ.cfg -- "" when there is no file or no line), so
    every report says which language the shot was taken in. A layout that
    overflows only in translation looks identical to one that never does
    unless the report itself says which language it shows; `ui_gallery`'s
    `langs` is what actually switches it, one round per language.
    """
    guard = require_project()
    if guard:
        return guard
    # Normalised ONCE, here, rather than left to ui_load's own copy: this
    # same string is also handed to _source_for and into meta["layout"], and
    # a backslash-separated path (Windows-typed, exactly what ui_load itself
    # accepts) would otherwise reach _source_for unnormalised, split on "/"
    # into one segment that matches no configured mod, and mis-attribute a
    # real project layout as "not a mod of this project" -- wrong, not just
    # unhelpful, since the source is right there under a name that not
    # normalising failed to recognise.
    layout = (layout or "").replace("\\", "/").strip()
    prof = session.profile()
    lint_notes: list[str] = []
    if live:
        # A leftover preview backdrop from an earlier ui_load would otherwise
        # sit on top of the menu this is meant to shoot. Its own result is
        # not this call's business: "nothing was loaded" is a fact about the
        # client from before this call started, not a failure of this one.
        ui_unload(timeout=timeout)
        source, text, why = None, None, ""
        # live=True walks the open menu directly -- ui_load never runs, so
        # there is no ONE layout `_source_for` could point at the way a real
        # ui_load has one. Every `.layout` the open project declares stands
        # in for it instead (_live_sources), so a self-sized label is still
        # recognised even though nothing here loaded any single page.
        extra_sources, extra_notes = _live_sources(prof)
        # The open menu unless the caller named something else -- a window a
        # mod hung off the workspace root has no menu to be found under, and
        # was unreachable here until `root` could name it.
        walked = root.strip() or "menu"
        first = ui_tree(root=walked, timeout=timeout)
        root = walked
    else:
        if not layout:
            return fail("ui_preview needs a layout, or live=True to look at the open menu")
        source, text, why = _source_for(layout)
        extra_sources, extra_notes = _fixture_sources(fixture, Path(prof.root))
        if text is not None:
            findings = lint_layout(text, layout, extra_classes=prof.build.layout_classes)
            refusal = next((f for f in findings if f.severity == REFUSE), None)
            if refusal:
                return fail(
                    f"{refusal.file}:{refusal.line}: {refusal.message}",
                    hint="fix it or lint with mod_lint first -- a quote inside a text value "
                         "hangs the engine's layout parser and the client stops ticking",
                )
            # WARN findings do not stop the call the way a REFUSE does, but
            # they were computed by this same lint pass regardless -- folded
            # into notes rather than thrown away, so a caller sees them
            # without a separate mod_lint round trip.
            lint_notes = [f"lint: {f.file}:{f.line} {f.message}" for f in findings if f.severity == WARN]
        first = ui_load(layout, fixture=fixture, host=host, timeout=timeout)
        root = "preview"
    if not first.ok:
        return first

    nodes, total = _collect_nodes(root, first, timeout)
    if live:
        top = next((n for n in nodes if n.get("path") == ""), None)
        rect = _rect(top["rect"]) if top else None
    else:
        rect = first.data.get("host")
    if rect is None:
        return fail("the tree came back without a rectangle to shoot",
                    hint="for live=True a scripted menu must be open (or name a window with "
                         "root=, or root='workspace'); for a layout the bridge reports ui_host")

    label = name or (Path(layout).stem if layout else "live")
    out_dir = Path(prof.root) / ".dayz-mcp" / "shots" / f"preview-{label}-{int(time.time() * 1000)}"
    out_dir.mkdir(parents=True, exist_ok=True)
    pid, _alive = _live_client()
    scale, scale_note = _window_scale(pid)
    lang_value = client_language() or ""
    shot = winui.shot(pid, out_dir / "shot.png", rect=rect)
    notes: list[str] = list(lint_notes)
    notes += extra_notes
    if scale_note:
        notes.append(scale_note)
    shot_name = "shot.png" if shot.ok else None
    if not shot.ok:
        notes.append(f"no screenshot: {shot.error}")
    elif shot.data.get("warning"):
        notes.append(shot.data["warning"])

    if why:
        notes.append(why)
    issues, check_notes = uicheck.check(nodes, rect, source, scale=scale, sources=extra_sources)
    notes += check_notes
    issue_dicts = [i.to_dict() for i in issues]
    meta = {"layout": layout or first.data.get("ui", {}).get("ui_menu", "menu"), "host": rect,
            "emulated": bool(host) and not live, "fixture": bool(fixture), "nodes": len(nodes),
            "total": total, "scale": scale, "language": lang_value}
    report = uireport.write_report(out_dir, shot_name, nodes, issue_dicts, notes, meta)
    counts = {"error": sum(1 for i in issues if i.severity == uicheck.ERROR),
              "warn": sum(1 for i in issues if i.severity == uicheck.WARN)}
    return ok({
        "dir": str(out_dir), "shot": str(out_dir / "shot.png") if shot_name else None,
        "report": str(report), "count": len(nodes), "total": total, "issues": counts,
        "notes": notes, "host": rect, "emulated": meta["emulated"], "scale": scale,
        "language": lang_value,
    })


#: How long _restart_client will wait for the server to drop a killed
#: player before giving up on that round entirely. Measured 2026-09-03,
#: first gallery's second round: client_stop kills the client process
#: outright, no clean disconnect ever reaches the server, and it holds the
#: player for its own timeout -- tens of seconds -- before dropping them. A
#: second client started into that window is kicked at login: "Player with
#: same UID is already in game".
RESTART_RELEASE_SECONDS = 90

#: How often the wait re-reads the server's player count. A module constant,
#: not a literal in the loop, so a test can monkeypatch time.sleep to a
#: no-op and let the whole wait run in however many iterations it takes
#: instead of RESTART_RELEASE_SECONDS of real wall-clock time -- the wait
#: below counts elapsed time as iterations * this constant, never
#: time.time(), specifically so that mocking sleep alone is enough.
RESTART_POLL_SECONDS = 2.0


def _server_players() -> int | None:
    """How many players the SERVER half of the bridge reports right now, or
    None when its state cannot be read at all (bridge not loaded in the
    stand, most often) -- the exact signal client_start's own readiness loop
    reads as its baseline (_players_in, imported rather than reimplemented),
    off the exact same channel."""
    return _players_in(Channel(server_profiles_dir()).read_state())


def _restart_client(size: tuple[int, int] | None, timeout: float, language: str | None = None) -> str:
    """Stop the client and start it again at `size` and/or `language`. Empty
    string on success, the reason otherwise.

    `size` may be None -- a round that changes only the language leaves the
    window exactly where it was: `client_start` is called with `window=None`,
    which means "the machine's own configured size" (or the client's last
    one), not "no window at all". `language` may likewise be empty, for a
    round that changes only the size; it is passed through to `client_start`
    unchanged, which is where it is validated.

    `client_stop` kills the client process outright -- there is no clean
    disconnect for the server to react to, and it holds the killed player
    for its own timeout before dropping them (measured on the stand
    2026-09-03: a second gallery round's client was kicked at login, "Player
    with same UID is already in game", started only seconds after the first
    was killed). So: read the player count BEFORE stopping, stop, and if that
    reading was a real, positive count, wait for the server's own count to
    fall below it (or to 0) before starting the next client at all. `before` None
    (unreadable) or 0 (nobody was connected) means there is nothing to
    release, and starting proceeds immediately either way.

    `client_stop` is called for its effect alone, not its answer: the real
    one always reports ok, including "nothing was started" -- that is a fact
    about the machine, not a refusal, so there is nothing here for a status
    check to add.
    """
    label = f"{size[0]}x{size[1]}" if size else "current"
    before = _server_players()
    client_stop()
    if before is not None and before > 0:
        last_seen = before
        elapsed = 0.0
        while True:
            time.sleep(RESTART_POLL_SECONDS)
            elapsed += RESTART_POLL_SECONDS
            now = _server_players()
            if now is not None:
                last_seen = now
                if now < before:
                    break
            if elapsed >= RESTART_RELEASE_SECONDS:
                return (
                    f"the server still reports {last_seen} player(s) {RESTART_RELEASE_SECONDS}s "
                    "after the stop -- a new client would be kicked with 'Player with same UID "
                    "is already in game'"
                )
    started = client_start(window=list(size) if size else None, language=language or "")
    if not started.ok:
        return f"could not start the client at {label}: {started.error}"
    job = job_wait(started.data["job_id"], timeout=max(timeout, 240))
    if not job.ok or job.data.get("status") != "done":
        return f"the client did not connect at {label}: {job.error or job.data}"
    return ""


#: How long ui_gallery waits before retrying an entry that failed on a
#: stalled bridge heartbeat. Measured 2026-09-03 on the live gallery: right
#: after the client (re)connects, _require_a_moving_bridge's own 1.2s probe
#: window (world.MOVEMENT_PROBE_WINDOW) can land between two ticks of the
#: bridge's 1Hz heartbeat and refuse the command -- three consecutive entries
#: lost to exactly that, and the same entries succeeded when run again
#: seconds later. A module constant, not a literal in the retry, so a test
#: can monkeypatch time.sleep to a no-op the way RESTART_POLL_SECONDS's
#: callers already do.
GALLERY_RETRY_SECONDS = 3.0


def ui_gallery(index: str = "preview/index.json", sizes: list[list[int]] | None = None,
               langs: list[str] | None = None,
               timeout: float = WORLD_TIMEOUT_SECONDS, strict: bool = False) -> Result:
    """Every entry of the project's preview index through ui_preview, and one
    index.html with all the pictures and counts -- the look before a push.

    An entry is `ui_preview`'s own arguments as a dict: `name`, `layout`,
    `fixture`, `host`, or `live: true` with an optional `root` (a window's
    name, or "workspace") for a live entry that is not the open menu.

    `sizes` restarts the client at each [width, height] in turn (the owner's
    3840x1600 and the players' 1920x1080); without it the client is used as
    it is.

    `langs` restarts the client into each named engine language in turn --
    typically English and the mod's own (a non-English stringtable column),
    so a layout that overflows only in translation is caught here rather
    than by a player. Validated the same way `client_start`'s own `language`
    argument is (case-insensitively, against the engine's own columns),
    before any round runs at all.

    Rounds are the PRODUCT of `sizes` (or the single "use it as it is"
    round) and `langs` (or the single "leave the language alone" round):
    each round whose size OR language differs from the one before it
    restarts the client, so a language-only change does not also re-pick the
    window and a size-only change does not also re-pick the language.
    `size` can be None for a language-only round -- `_restart_client` then
    starts the client with `window=None`, the machine's own configured size,
    rather than inventing one.

    Every entry's `"size"` label is unchanged (`"3840x1600"` or `"current"`);
    a sibling `"language"` key carries the round's language, `""` when none
    was requested for it. `strict=True`'s failure message names entries as
    `name@size@language` once `langs` was given at all (kept as `name@size`
    otherwise, so a caller not using this axis sees the same message as
    before). The gallery index shows the language beside the size on every
    card.

    An entry that fails on a stalled bridge heartbeat gets one retry, after
    GALLERY_RETRY_SECONDS, rather than being recorded lost -- the same brief
    miss _require_a_moving_bridge warns about (world.py), landing right after
    a client (re)connect. That entry's dict then carries `"retried": True`
    (absent otherwise); any other failure is recorded as today, with no
    second attempt.

    `strict=True` fails the call when any entry has an error-severity issue
    or failed to render -- the one-call readiness criterion of spec
    2026-09-04 §9; the data is the same either way.
    """
    guard = require_project()
    if guard:
        return guard
    prof = session.profile()
    path = Path(prof.root) / index
    hint = 'write {"entries": [{"name": "...", "layout": "...", "fixture": "preview/x.json", "host": "w h"}]}'
    try:
        spec = json.loads(path.read_text(encoding="utf-8"))
        entries_in = spec["entries"]
        assert isinstance(entries_in, list), '"entries" must be a list'
        for entry in entries_in:
            # A string layout (possibly "", refused later by ui_preview
            # itself and recorded as a per-entry failure -- not aborted
            # here) or live: true is what makes an entry USABLE at all; a
            # non-dict entry, or one with neither, would otherwise reach
            # entry.get(...) below and raise AttributeError straight out of
            # this tool instead of a plain fail().
            usable = isinstance(entry, dict) and (
                isinstance(entry.get("layout"), str) or entry.get("live") is True
            )
            assert usable, f'entry {entry!r} needs a string "layout" or "live": true'
        size_rounds: list[tuple[int, int] | None] = [None]
        if sizes:
            parsed_sizes = [window_size(s) for s in sizes]
            assert all(parsed_sizes), f"sizes must each be two positive integers, not {sizes!r}"
            size_rounds = parsed_sizes
        lang_rounds: list[str | None] = [None]
        if langs:
            parsed_langs = [language_name(v) for v in langs]
            assert all(parsed_langs), (
                f"langs must each be one of {', '.join(ENGINE_LANGUAGES)}, not {langs!r}"
            )
            lang_rounds = parsed_langs
    except (OSError, ValueError, KeyError, TypeError, AssertionError) as exc:
        return fail(f"no usable preview index at {path}: {exc}", hint=hint)

    entries: list[dict] = []
    out_dir = Path(prof.root) / ".dayz-mcp" / "shots" / f"gallery-{int(time.time() * 1000)}"
    out_dir.mkdir(parents=True, exist_ok=True)
    # (None, None): nothing has configured the client yet, and it is also
    # the ONLY round when neither sizes nor langs was given -- comparing
    # that round against this same sentinel is what keeps the no-axes case
    # from restarting a client nobody asked to touch, exactly as before.
    previous: tuple = (None, None)
    for size, lang in itertools.product(size_rounds, lang_rounds):
        label = f"{size[0]}x{size[1]}" if size else "current"
        lang_label = lang or ""
        if (size, lang) != previous:
            problem = _restart_client(size, timeout, lang)
            if problem:
                entries.append({"name": "(client)", "size": label, "language": lang_label,
                                "ok": False, "report": "", "shot": "", "issues": {}, "error": problem})
                # `previous` is left as it was: a FAILED restart says nothing
                # about what the client is actually running, so the next
                # round must not read a stale match against it as "no change
                # needed" and skip its own restart.
                continue
            previous = (size, lang)
        for entry in entries_in:
            name = str(entry.get("name") or Path(str(entry.get("layout", ""))).stem or "entry")
            preview_args = dict(layout=str(entry.get("layout", "")), fixture=entry.get("fixture"),
                                host=str(entry.get("host", "") or ""), live=bool(entry.get("live", False)),
                                name=name, root=str(entry.get("root", "") or ""), timeout=timeout)
            result = ui_preview(**preview_args)
            retried = False
            if not result.ok and "not ticking" in result.error:
                time.sleep(GALLERY_RETRY_SECONDS)
                result = ui_preview(**preview_args)
                retried = True
            # `ui_preview` reads the client's OWN current language and
            # reports what it actually measured -- prefer that over the
            # round's mere request, so a client that has not really finished
            # switching shows a caption that matches its own screenshot. A
            # failed call's `data` is `fail()`'s own default, `None` (never
            # reaching the point where "language" would be set), hence the
            # `or {}` -- and then the round's requested value either way.
            entry_lang = (result.data or {}).get("language") or lang_label
            if result.ok:
                report = Path(result.data["report"])
                shot = result.data.get("shot")
                e = {"name": name, "size": label, "language": entry_lang, "ok": True,
                    "report": Path("..") / report.parent.name / report.name,
                    "shot": (Path("..") / report.parent.name / "shot.png") if shot else "",
                    "issues": result.data["issues"], "error": ""}
            else:
                e = {"name": name, "size": label, "language": entry_lang, "ok": False,
                    "report": "", "shot": "", "issues": {}, "error": result.error}
            if retried:
                e["retried"] = True
            entries.append(e)
    for e in entries:
        e["report"] = str(e["report"]).replace("\\", "/") if e["report"] else ""
        e["shot"] = str(e["shot"]).replace("\\", "/") if e["shot"] else ""
    index_path = out_dir / "index.html"
    index_path.write_text(uireport.render_gallery(entries), encoding="utf-8")
    failed = sum(1 for e in entries if not e["ok"])
    data = {"dir": str(out_dir), "index": str(index_path), "entries": entries, "failed": failed}
    if strict:
        # `name@size`: the same page is one entry per requested size, so a
        # failure naming the page alone cannot say which size failed -- nor,
        # for a "(client)" restart failure, which round it belongs to.
        # `@language` joins them once `langs` was given at all -- the same
        # page run in two languages needs the failure to say which one --
        # and is left off otherwise so a caller not using this axis sees
        # exactly the message it always has.
        bad = [f"{e['name']}@{e['size']}" + (f"@{e['language']}" if langs else "")
               for e in entries
               if not e.get("ok") or int((e.get("issues") or {}).get("error", 0)) > 0]
        if bad:
            return Result(False, data, f"{len(bad)} entries with errors: {', '.join(bad)}",
                          "open index.html -- every error is a rectangle the engine drew, not a guess")
    return ok(data)


def layout_build(mod: str = "") -> Result:
    """Generate every .layout described under `ui/<Mod>/` from the project's
    tokens file (`ui/tokens.json` unless `build.tokens` says otherwise).

    A description is one JSON file per layout (spec 2026-09-04 §3.3): a page
    is containers and tokens, and every number the engine cannot derive --
    the remainder a `fill` takes, the width of a row under a scrollbar -- is
    derived here once. Only files that differ are written (LF endings); the
    first bad description refuses the whole call with its file, node and
    reason, and nothing is written.

    `mod` limits the build to one of the project's mods; empty builds all.
    `mod_lint` refuses a build whose generated file is behind its description
    (layout-stale), so run this before `mod_build` after editing a
    description. Until the MCP server is restarted, the same build is
    `python -m dayz_mcp.layoutgen <project root> [mod]`.
    """
    guard = require_project()
    if guard:
        return guard
    prof = session.profile()
    if mod and mod not in prof.build.mods:
        return fail(f"{mod!r} is not a mod of this project",
                    hint="the project declares: " + ", ".join(prof.build.mods))
    try:
        report = build_project(prof.root, prof.build.mods, prof.build.sources, mod, tokens_path=prof.build.tokens)
    except LayoutGenError as exc:
        return fail(f"refused: {exc}", hint="fix the description under ui/; nothing was written")
    return ok({"written": report.written, "unchanged": report.unchanged, "notes": report.notes,
               "descriptions": sorted(set(report.sources.values()))})
