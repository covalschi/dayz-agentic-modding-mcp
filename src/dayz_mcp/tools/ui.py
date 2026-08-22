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

NOTHING HERE HAS BEEN RUN AGAINST A LIVE CLIENT. The owner's instruction for
this phase was not to start the stand. Every signature comes from the game's
sources and every mechanism has unit tests, but "it compiles and the tests pass"
is not "it works", and the spec keeps the list of what a first live run has to
settle.
"""
from __future__ import annotations

import time
from pathlib import Path

from .. import winui
from ..bridge.channel import CLIENT_CMD_FILENAME, CLIENT_STATE_FILENAME, Channel
from ..errors import Result, fail, ok
from ..procs import is_alive
from . import session
from .client import client_profiles_dir
from .project import require_project
from .world import WORLD_TIMEOUT_SECONDS, _args, _require_a_moving_bridge, _wire_args

#: The client half's own ceilings, kept in step with DZMCP_Ui in the mod. A
#: value above these is clamped there rather than refused -- these are page
#: sizes, where "as much as you can" is a clear intent.
NODES_MAX = 300
DEPTH_MAX = 32

#: Fields of one described node, in the order the mod writes them.
_FIELDS = 7


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


def _run(verb: str, args: dict, timeout: float) -> Result:
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
        return _with_ui(ok(payload), channel)
    if state.status == "failed":
        return Result(False, payload, state.detail or f"{verb} failed",
                      hint="the mod's own words are in the error above; a refusal is a "
                           "result, not a malfunction")
    return Result(
        False, payload, f"{verb} was still {state.status} after {timeout:g}s",
        hint="the client half carries the same 30s hard limit as the server half, so a "
             "command still running past this wait means the tick itself stalled",
    )


def _with_ui(answered: Result, channel: Channel) -> Result:
    """Add the client's published UI block, and the nodes parsed out of it."""
    state = channel.read_state()
    if state is None:
        time.sleep(0.3)
        state = channel.read_state()
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
    answered.data["truncated"] = (
        isinstance(answered.data["total"], int) and answered.data["total"] > len(nodes)
    )
    return answered


def _node(line: str) -> dict:
    """One `path|class|name|vis|rect|depth|text` line as a dict.

    A line that does not have every field is passed through under `raw` rather
    than dropped: a reader that silently discarded what it could not parse
    would report a smaller interface than the one the client found.
    """
    parts = line.split("|")
    if len(parts) != _FIELDS:
        return {"raw": line}
    flags = parts[3]
    return {
        "path": parts[0],
        "class": parts[1],
        "name": parts[2],
        # Two separate answers, because they differ exactly when a node is
        # visible in itself but sits inside a hidden parent -- which is the
        # ordinary state of half a menu and would otherwise read as "shown".
        "visible": flags[:1] == "1",
        "shown": flags[1:2] == "1",
        "rect": parts[4],
        "depth": int(parts[5]) if parts[5].lstrip("-").isdigit() else parts[5],
        "text": parts[6],
    }


def _centre(rect: str) -> tuple[int, int] | None:
    """The centre of an `x y w h` rectangle, or None if it is not one."""
    parts = rect.split()
    if len(parts) != 4:
        return None
    try:
        x, y, w, h = (int(float(p)) for p in parts)
    except ValueError:
        return None
    return x + w // 2, y + h // 2


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
    state = channel.read_state()
    if state is None:
        time.sleep(0.3)
        state = channel.read_state()
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


def ui_tree(root: str = "menu", depth: int = DEPTH_MAX, limit: int = NODES_MAX,
            timeout: float = WORLD_TIMEOUT_SECONDS) -> Result:
    """The client's widget tree: what is on screen, as the engine holds it.

    `root` is `"menu"` (the open scripted menu, the default) or `"screen"` (the
    whole workspace). Each node comes back with its path, class, name,
    visibility, screen rectangle, depth and -- where the engine allows it to be
    read -- its text.

    The answer is A PAGE: `total` is how many nodes the walk visited and `count`
    is how many it listed, and `truncated` says when they differ. A shorter list
    that did not say so would read as the whole interface.
    """
    return _run("ui_tree", _args(root=root, depth=depth, limit=limit), timeout)


def ui_find(name: str = "", class_name: str = "", text: str = "",
            root: str = "menu", depth: int = DEPTH_MAX, limit: int = NODES_MAX,
            timeout: float = WORLD_TIMEOUT_SECONDS) -> Result:
    """Find widgets by name, class or text, without fetching the whole tree.

    `name` and `class_name` match exactly; `text` matches as a substring,
    because a label's exact string is the one thing a caller rarely knows in
    advance. At least one of the three is required -- with none of them this
    would be `ui_tree`, and answering it as such would hide which question was
    actually asked.

    Filtering happens in the client, not here: sending the whole tree back so it
    could be filtered locally is exactly what the page limit exists to avoid.
    """
    if not (name or class_name or text):
        return fail(
            "ui_find needs at least one of name, class_name or text",
            hint="with none of them this is ui_tree -- call that instead",
        )
    return _run(
        "ui_find",
        _args(**{"name": name or None, "class": class_name or None,
                 "text": text or None, "root": root, "depth": depth, "limit": limit}),
        timeout,
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

    pid, _alive = _live_client()
    clicked = winui.click(pid, centre[0], centre[1])
    found.data["via"] = "cursor"
    found.data["clicked_at"] = {"x": centre[0], "y": centre[1]}
    found.data["click"] = clicked.data
    if not clicked.ok:
        return Result(False, found.data, clicked.error, clicked.hint)
    return found


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
