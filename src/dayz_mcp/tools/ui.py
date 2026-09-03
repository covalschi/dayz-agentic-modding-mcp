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

import json
import time
from pathlib import Path

from .. import uicheck, uireport
from .. import winui
from ..bridge.channel import CLIENT_CMD_FILENAME, CLIENT_STATE_FILENAME, Channel
from ..errors import Result, fail, ok
from ..layoutparse import LayoutSyntaxError, parse_layout
from ..procs import is_alive
from ..profile import resolve_mod_dir
from ..uigeom import parse_rect
from . import session
from .client import client_profiles_dir, client_start, client_stop
from .jobs_api import job_wait
from .project import require_project
from .world import WORLD_TIMEOUT_SECONDS, _args, _require_a_moving_bridge, _wire_args

#: The client half's own ceilings, kept in step with DZMCP_Ui in the mod. A
#: value above these is clamped there rather than refused -- these are page
#: sizes, where "as much as you can" is a clear intent.
NODES_MAX = 300
DEPTH_MAX = 32

#: Fields of one described node: seven before the text size was added, eight
#: with it. Both parse, so a bridge built before the change still reads.
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
    answered.data["offset"] = offset
    answered.data["truncated"] = (
        isinstance(answered.data["total"], int)
        and answered.data["total"] > offset + len(nodes)
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


def _is_within(path: Path, base: Path) -> bool:
    """True if `path` is `base` or lives underneath it (both already resolved)."""
    try:
        path.relative_to(base)
    except ValueError:
        return False
    return True


def _fixture_text(fixture, root: Path) -> tuple[str | None, str]:
    """The fixture as the JSON text the mod will parse, or an error.

    A dict is serialised; a string is a project-relative path to a JSON file,
    or JSON text itself when it starts with `{`. Validated HERE, before the
    round trip: the mod's own refusal costs a tick and names less.
    """
    if fixture is None:
        return None, ""
    if isinstance(fixture, str):
        if fixture.lstrip().startswith("{"):
            text = fixture
        else:
            root_resolved = root.resolve()
            path = (root / fixture).resolve()
            if not _is_within(path, root_resolved):
                return None, f"fixture path must stay inside the project: {path}"
            if not path.is_file():
                return None, f"fixture file not found: {path}"
            text = path.read_text(encoding="utf-8")
        try:
            fixture = json.loads(text)
        except ValueError as exc:
            return None, f"fixture is not valid JSON: {exc}"
    if not isinstance(fixture, dict) or not isinstance(fixture.get("ops"), list):
        return None, 'fixture must be an object with an "ops" list'
    for index, op in enumerate(fixture["ops"]):
        if not isinstance(op, dict) or not isinstance(op.get("op"), str):
            return None, f'fixture op {index} must be an object with an "op" string'
    return json.dumps(fixture, ensure_ascii=False), ""


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
            offset: int = 0, timeout: float = WORLD_TIMEOUT_SECONDS) -> Result:
    """The client's widget tree: what is on screen, as the engine holds it.

    `root` is `"menu"` (the open scripted menu, the default) or `"screen"` (the
    whole workspace). Each node comes back with its path, class, name,
    visibility, screen rectangle, depth, text and -- for widgets that derive
    from `TextWidget` -- the rendered text size in engine pixels.

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
                    hint="relative to the pbo prefix, e.g. OpenZone_PDA/gui/layouts/oz_pda_tab.layout")
    text, error = _fixture_text(fixture, Path(session.profile().root))
    if error:
        return fail(error, hint='a fixture is {"ops": [{"op": "add", "layout": "...", "into": "...", "count": 3}, ...]}')
    args = _args(layout=layout, host=host or None, fixture=text, depth=depth, limit=limit, offset=offset)
    return _run("ui_load", args, timeout, offset)


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
    """The layout's source in this project, parsed, for the checks that need
    the text (a style on an edit box). Empty with a reason when it is not
    this project's file -- a vanilla layout, another mod's."""
    prof = session.profile()
    head, _, rest = layout.partition("/")
    if head not in prof.build.mods:
        return None, f"{head!r} is not a mod of this project, so the source was not read"
    path = resolve_mod_dir(prof.root, prof.build.sources, head) / rest
    if not path.is_file():
        return None, f"source not found at {path}"
    try:
        return parse_layout(path.read_text(encoding="utf-8", errors="replace")), ""
    except LayoutSyntaxError as exc:
        return None, f"source does not parse: {exc}"


def ui_preview(layout: str = "", fixture: dict | str | None = None, host: str = "",
               live: bool = False, name: str = "",
               timeout: float = WORLD_TIMEOUT_SECONDS) -> Result:
    """A layout as the engine draws it: screenshot, every widget's rectangle,
    and the checks over those rectangles, written to one folder with an HTML
    report.

    `live=False` loads `layout` through ui_load (with `fixture` and `host` as
    there) and shoots the preview host. `live=True` loads nothing: it walks the
    OPEN scripted menu and shoots its root -- the way to look at the real PDA
    with real data. A host of its own size is an emulation of a screen that
    size and the report says so; the real check is the real window size.
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
    if live:
        first = ui_tree(root="menu", timeout=timeout)
        root = "menu"
    else:
        if not layout:
            return fail("ui_preview needs a layout, or live=True to look at the open menu")
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
                    hint="for live=True a scripted menu must be open; for a layout the bridge reports ui_host")

    label = name or (Path(layout).stem if layout else "live")
    out_dir = Path(prof.root) / ".dayz-mcp" / "shots" / f"preview-{label}-{int(time.time() * 1000)}"
    out_dir.mkdir(parents=True, exist_ok=True)
    pid, _alive = _live_client()
    shot = winui.shot(pid, out_dir / "shot.png", rect=rect)
    notes: list[str] = []
    shot_name = "shot.png" if shot.ok else None
    if not shot.ok:
        notes.append(f"no screenshot: {shot.error}")
    elif shot.data.get("warning"):
        notes.append(shot.data["warning"])

    source, why = (None, "") if live else _source_for(layout)
    if why:
        notes.append(why)
    issues, check_notes = uicheck.check(nodes, rect, source)
    notes += check_notes
    issue_dicts = [i.to_dict() for i in issues]
    meta = {"layout": layout or first.data.get("ui", {}).get("ui_menu", "menu"), "host": rect,
            "emulated": bool(host) and not live, "fixture": bool(fixture), "nodes": len(nodes), "total": total}
    report = uireport.write_report(out_dir, shot_name, nodes, issue_dicts, notes, meta)
    counts = {"error": sum(1 for i in issues if i.severity == uicheck.ERROR),
              "warn": sum(1 for i in issues if i.severity == uicheck.WARN)}
    return ok({
        "dir": str(out_dir), "shot": str(out_dir / "shot.png") if shot_name else None,
        "report": str(report), "count": len(nodes), "total": total, "issues": counts,
        "notes": notes, "host": rect, "emulated": meta["emulated"],
    })


def _restart_client(size: tuple[int, int], timeout: float) -> str:
    """Stop the client and start it again at `size`. Empty string on success,
    the reason otherwise."""
    stopped = client_stop()
    if not stopped.ok and "nothing" not in (stopped.error or ""):
        return f"could not stop the client: {stopped.error}"
    started = client_start(window=list(size))
    if not started.ok:
        return f"could not start the client at {size[0]}x{size[1]}: {started.error}"
    job = job_wait(started.data["job_id"], timeout=max(timeout, 240))
    if not job.ok or job.data.get("status") != "done":
        return f"the client did not connect at {size[0]}x{size[1]}: {job.error or job.data}"
    return ""


def ui_gallery(index: str = "preview/index.json", sizes: list[list[int]] | None = None,
               timeout: float = WORLD_TIMEOUT_SECONDS) -> Result:
    """Every entry of the project's preview index through ui_preview, and one
    index.html with all the pictures and counts -- the look before a push.

    `sizes` restarts the client at each [width, height] in turn (the owner's
    3840x1600 and the players' 1920x1080); without it the client is used as
    it is.
    """
    guard = require_project()
    if guard:
        return guard
    prof = session.profile()
    path = Path(prof.root) / index
    try:
        spec = json.loads(path.read_text(encoding="utf-8"))
        entries_in = spec["entries"]
        assert isinstance(entries_in, list)
    except (OSError, ValueError, KeyError, AssertionError) as exc:
        return fail(f"no usable preview index at {path}: {exc}",
                    hint='write {"entries": [{"name": "...", "layout": "...", "fixture": "preview/x.json", "host": "w h"}]}')

    rounds: list[tuple[int, int] | None] = [None]
    if sizes:
        rounds = [(int(s[0]), int(s[1])) for s in sizes]
    entries: list[dict] = []
    out_dir = Path(prof.root) / ".dayz-mcp" / "shots" / f"gallery-{int(time.time() * 1000)}"
    out_dir.mkdir(parents=True, exist_ok=True)
    for size in rounds:
        label = f"{size[0]}x{size[1]}" if size else "current"
        if size:
            problem = _restart_client(size, timeout)
            if problem:
                entries.append({"name": "(client)", "size": label, "ok": False, "report": "", "shot": "", "issues": {}, "error": problem})
                continue
        for entry in entries_in:
            name = str(entry.get("name") or Path(str(entry.get("layout", ""))).stem or "entry")
            result = ui_preview(layout=str(entry.get("layout", "")), fixture=entry.get("fixture"),
                                host=str(entry.get("host", "") or ""), live=bool(entry.get("live", False)),
                                name=name, timeout=timeout)
            if result.ok:
                report = Path(result.data["report"])
                shot = result.data.get("shot")
                entries.append({"name": name, "size": label, "ok": True,
                                "report": Path("..") / report.parent.name / report.name,
                                "shot": (Path("..") / report.parent.name / "shot.png") if shot else "",
                                "issues": result.data["issues"], "error": ""})
            else:
                entries.append({"name": name, "size": label, "ok": False, "report": "", "shot": "", "issues": {}, "error": result.error})
    for e in entries:
        e["report"] = str(e["report"]).replace("\\", "/") if e["report"] else ""
        e["shot"] = str(e["shot"]).replace("\\", "/") if e["shot"] else ""
    index_path = out_dir / "index.html"
    index_path.write_text(uireport.render_gallery(entries), encoding="utf-8")
    failed = sum(1 for e in entries if not e["ok"])
    return ok({"dir": str(out_dir), "index": str(index_path), "entries": entries, "failed": failed})
