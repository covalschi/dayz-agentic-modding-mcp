"""Checks over the rectangles the engine computed for a widget tree.

Input is what ui_tree reports (see tools/ui.py `_node`): the engine's own
screen rectangles, so nothing here re-implements layout. The rules are the
owner's three complaints -- frames crossing, text under the scrollbar, edit
boxes without a frame -- plus the two shapes a broken self-sizing spacer
takes (zero height, or a hundred thousand units of it).

`scale` is the layout-unit-to-pixel ratio of the window the tree was read
from -- s = H/1080, exact (spec F1, measured 2026-09-03) -- and it is what
turns a measurement taken in layout units (the scrollbar's width, a border
panel's 1-unit overhang) into the screen pixels this module compares
rectangles in. 1.0 (the default) is correct for a 1080-row window.

Every number these rules once waited on is measured now: the scrollbar is 10
layout units wide, drawn OVER the content (spec F3), and the engine cannot
tell a panel from a frame by class name alone -- Widget.ClassName() reports
"Widget" for both PanelWidgetClass and FrameWidgetClass (spec F4) -- so
`check` uses the SOURCE layout's own class, when one was given, to judge
what draws a frame behind an edit box.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

from .layoutparse import LayoutNode
from .uigeom import parse_rect

ERROR = "error"
WARN = "warn"

#: A child may poke this far out of its parent before it is overflow.
OVERFLOW_TOLERANCE_PX = 1
#: Two content siblings must cross by this much on BOTH axes to count.
OVERLAP_MIN_PX = 2
#: A node this many times taller or wider than the host is a runaway spacer.
RUNAWAY_FACTOR = 4
#: Width of the engine's vertical scrollbar, in LAYOUT UNITS -- 10 px at 1080
#: rows, 15 at 1600 (round(10 * 1600 / 1080), measured 2026-09-03, spec F3).
#: Scaled to screen pixels by `check`'s own `scale` argument before it is
#: compared against a rect.
SCROLLBAR_UNITS = 10

#: Engine class names (Widget.ClassName()) that carry content rather than
#: paint a background. Spec M7 confirms what a PanelWidgetClass reports.
CONTENT_CLASSES = frozenset({
    "TextWidget", "MultilineTextWidget", "RichTextWidget", "EditBoxWidget",
    "MultilineEditBoxWidget", "PasswordEditBoxWidget", "ButtonWidget", "ImageWidget",
    "TextListboxWidget", "CheckBoxWidget", "SliderWidget", "XComboBoxWidget",
    "ItemPreviewWidget", "PlayerPreviewWidget", "MapWidget",
})
#: What the ENGINE reports for a widget that draws a frame -- the fallback
#: for a framing candidate (a parent, an earlier sibling) that has no
#: counterpart in the source layout, e.g. one a fixture added at runtime.
#: "PanelWidget" is deliberately absent: spec F4 measured Widget.ClassName()
#: answering "Widget" for BOTH PanelWidgetClass and FrameWidgetClass, so the
#: engine alone can never report "PanelWidget" -- only a source class can
#: (see FRAMING_SOURCE_CLASSES, which `check` prefers whenever it can).
FRAMING_CLASSES = frozenset({"Widget", "ImageWidget"})
#: Source classes (LayoutNode.cls) that draw a frame behind an edit box, for
#: a framing candidate whose own node in the SOURCE layout is known -- the
#: reliable read, since the engine's class name conflates panel and frame.
FRAMING_SOURCE_CLASSES = frozenset({"PanelWidgetClass", "ImageWidgetClass"})
SCROLL_CLASS = "ScrollWidget"
EDITBOX_CLASS = "EditBoxWidget"


@dataclass
class Issue:
    rule: str
    severity: str
    path: str
    name: str
    cls: str
    detail: str
    other: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def rect_of(node: dict) -> tuple[int, int, int, int] | None:
    return parse_rect(node.get("rect", ""))


def _parent_path(path: str) -> str | None:
    if path == "":
        return None
    return path.rsplit(".", 1)[0] if "." in path else ""


def _contains(outer, inner, tolerance: int = 0) -> bool:
    ox, oy, ow, oh = outer
    ix, iy, iw, ih = inner
    return (ix >= ox - tolerance and iy >= oy - tolerance
            and ix + iw <= ox + ow + tolerance and iy + ih <= oy + oh + tolerance)


def _cross(a, b) -> tuple[int, int]:
    """How far two rectangles cross on each axis (0 when they do not)."""
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    dx = min(ax + aw, bx + bw) - max(ax, bx)
    dy = min(ay + ah, by + bh) - max(ay, by)
    return max(0, dx), max(0, dy)


def _border_overhang(parent: tuple, child: tuple) -> tuple[int, int, int, int] | None:
    """How far `child` pokes out past `parent` on each of the four sides
    (left, top, right, bottom) when it encloses `parent` on EVERY side --
    None if it does not enclose it on all four.

    This is the shape our own layouts draw a button's border in (spec F7): a
    child panel `position -1 -1, size w+2 h+2`, one unit larger than its
    parent all the way round. `check` treats a small enough overhang of this
    exact shape as a border, not an overflow.
    """
    px, py, pw, ph = parent
    cx, cy, cw, ch = child
    left, top = px - cx, py - cy
    right, bottom = (cx + cw) - (px + pw), (cy + ch) - (py + ph)
    if left < 0 or top < 0 or right < 0 or bottom < 0:
        return None
    return left, top, right, bottom


def check(nodes: list[dict], host: tuple[int, int, int, int] | None,
          source: LayoutNode | None = None, scale: float = 1.0) -> tuple[list[Issue], list[str]]:
    issues: list[Issue] = []
    notes: list[str] = []
    shown = {n["path"]: n for n in nodes if "path" in n and n.get("shown", True) and rect_of(n)}
    children: dict[str, list[dict]] = {}
    for path, n in shown.items():
        parent = _parent_path(path)
        if parent is not None:
            children.setdefault(parent, []).append(n)
    source_by_path = {p: s for p, s in source.walk()} if source else {}

    for path, n in shown.items():
        rect = rect_of(n)
        cls = n["class"]
        name = n["name"]
        x, y, w, h = rect

        if w == 0 or h == 0:
            issues.append(Issue("zero_size", WARN, path, name, cls,
                                f"visible but {w}x{h} px -- a collapsed Size To Content, or an empty container"))
        if host and (w > host[2] * RUNAWAY_FACTOR or h > host[3] * RUNAWAY_FACTOR):
            issues.append(Issue("runaway", ERROR, path, name, cls,
                                f"{w}x{h} px against a host of {host[2]}x{host[3]} -- a self-sizing spacer that grew without bound"))
        if host and (_cross(rect, host) == (0, 0)) and w > 0 and h > 0:
            issues.append(Issue("offhost", WARN, path, name, cls, f"entirely outside the host {host}"))

        parent_path = _parent_path(path)
        parent = shown.get(parent_path) if parent_path is not None else None
        if parent:
            prect = rect_of(parent)
            border = _border_overhang(prect, rect)
            if border is not None and max(border) <= round(2 * scale) + 1:
                # F7: our own button-border panel (position -1 -1, size w+2
                # h+2 in layout units) -- drawn that way on purpose, and it
                # is not a CONTENT_CLASSES widget, so it never overlaps
                # either.
                pass
            elif parent["class"] == SCROLL_CLASS:
                # Content longer than the viewport is what a scroll widget is for;
                # only the horizontal edges are held.
                px, py, pw, ph = prect
                inside_h = x >= px - OVERFLOW_TOLERANCE_PX and x + w <= px + pw + OVERFLOW_TOLERANCE_PX
                if not inside_h:
                    issues.append(Issue("overflow", ERROR, path, name, cls,
                                        f"{rect} pokes out of its scroll viewport {prect} sideways", parent_path))
            elif parent["class"].endswith("SpacerWidget"):
                # F5: a WrapSpacer's full-width (size 1) children overhang it
                # by the padding (2 layout units, default) on the right --
                # an engine behaviour, not a layout bug.
                tolerance = round(2 * scale) + OVERFLOW_TOLERANCE_PX
                if not _contains(prect, rect, tolerance):
                    issues.append(Issue("overflow", ERROR, path, name, cls,
                                        f"{rect} pokes out of its parent {parent['name']!r} {prect}", parent_path))
            elif not _contains(prect, rect, OVERFLOW_TOLERANCE_PX):
                issues.append(Issue("overflow", ERROR, path, name, cls,
                                    f"{rect} pokes out of its parent {parent['name']!r} {prect}", parent_path))

        text_size = n.get("text_size")
        if text_size and (text_size[0] > w + OVERFLOW_TOLERANCE_PX or text_size[1] > h + OVERFLOW_TOLERANCE_PX):
            issues.append(Issue("text_overflow", ERROR, path, name, cls,
                                f"the text measures {text_size[0]}x{text_size[1]} px in a {w}x{h} px box"))

        if cls == EDITBOX_CLASS:
            src = source_by_path.get(path)
            if src is None:
                unjudged = "editbox_bare: no source layout was given, so edit boxes were not judged"
                if not source and unjudged not in notes:
                    notes.append(unjudged)
            elif src.prop("style") is None and not _framed(n, shown, children, parent_path, source_by_path):
                issues.append(Issue("editbox_bare", ERROR, path, name, cls,
                                    "no style and no panel behind it -- the field draws no frame"))

    for parent_path, kids in children.items():
        content = [k for k in kids if k["class"] in CONTENT_CLASSES]
        for i, a in enumerate(content):
            for b in content[i + 1:]:
                dx, dy = _cross(rect_of(a), rect_of(b))
                if dx > OVERLAP_MIN_PX and dy > OVERLAP_MIN_PX:
                    issues.append(Issue("overlap", ERROR, a["path"], a["name"], a["class"],
                                        f"crosses {b['name']!r} by {dx}x{dy} px", b["path"]))

    # F3: drawn OVER the content -- the content is never narrowed for it --
    # and only when the content is taller than the viewport, so the rule
    # runs unconditionally rather than gating on whether a ScrollWidget is
    # even present.
    bar_px = round(SCROLLBAR_UNITS * scale)
    for path, scroll in shown.items():
        if scroll["class"] != SCROLL_CLASS:
            continue
        sx, sy, sw, sh = rect_of(scroll)
        content = [k for k in children.get(path, [])]
        if not any(rect_of(k)[3] > sh for k in content):
            continue  # no bar is drawn when the content fits (measured F3)
        limit = sx + sw - bar_px
        for dpath, d in shown.items():
            if not dpath.startswith(path + ".") or d["class"] not in CONTENT_CLASSES:
                continue
            dx, dy, dw, dh = rect_of(d)
            if dx + dw > limit + OVERFLOW_TOLERANCE_PX:
                issues.append(Issue("under_scrollbar", ERROR, dpath, d["name"], d["class"],
                                    f"right edge {dx + dw} px runs under the scrollbar, which starts at {limit} px", path))

    issues.sort(key=lambda i: (i.severity != ERROR, i.path, i.rule))
    return issues, notes


def _framed(node: dict, shown: dict, children: dict, parent_path: str | None,
            source_by_path: dict) -> bool:
    """A panel behind the edit box: the parent itself, or an earlier sibling
    whose rectangle encloses it.

    "Draws a frame" is decided by the candidate's own node in the SOURCE
    layout when one is known there (PanelWidgetClass or ImageWidgetClass) --
    the reliable read, since the engine's own ClassName() cannot tell a panel
    from a frame apart, both report "Widget" (spec F4). A candidate absent
    from the source -- most often one a fixture added at runtime, which never
    has a counterpart in the STATIC file `source_by_path` was built from --
    falls back to FRAMING_CLASSES against the engine's own class instead of
    being silently ignored.
    """
    def frames(candidate_path: str, engine_cls: str) -> bool:
        src = source_by_path.get(candidate_path)
        if src is not None:
            return src.cls in FRAMING_SOURCE_CLASSES
        return engine_cls in FRAMING_CLASSES

    rect = rect_of(node)
    if parent_path is not None:
        parent = shown.get(parent_path)
        if parent and frames(parent_path, parent["class"]):
            return True
        for sibling in children.get(parent_path, []):
            if sibling is node:
                break
            if frames(sibling["path"], sibling["class"]) and _contains(rect_of(sibling), rect):
                return True
    return False
