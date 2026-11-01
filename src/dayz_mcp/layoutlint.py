"""Static checks on `.layout` files, before any client is started.

Refusals are the defects the engine reports nowhere: a quote inside a text
value hangs its layout parser (measured 2026-08-30 by bisecting a file), an
unquoted multi-word key or an unknown class is ignored without a word, and an
ItemPreviewWidget whose own priority is 300 or more, or below 100 under an
ancestor at 100 or more, draws nothing (measured 2026-11-01, 74 cells).
Warnings are the things the eyes (ui_preview) settle for certain but which
are cheap to name here.
"""
from __future__ import annotations

from typing import Iterable

from .layoutparse import LayoutNode, LayoutProp, LayoutSyntaxError, parse_layout
from .layoutvocab import load_vocab
from .lint import REFUSE, WARN, Finding

#: ItemPreviewWidget and `priority`, measured 2026-11-01 on DayZ 1.30.164200 (74
#: cells, skill gui-layouts.md §13): the preview's OWN priority decides. 201 drew
#: under every root from 1 to 998 -- the vanilla tooltip is 952 > 0 > 201; 300 and
#: above never drew, nor did anything under a middle frame at 300; below 100 it drew
#: only while every ancestor was below 100; 100 drew under root 100 and under
#: 150 > 5, not under roots 256 or 952. The 2026-08 rule, "nothing above 256 on the
#: way to the preview", refused layouts that draw.
PREVIEW_OWN_NEVER = 300
PREVIEW_LOW_OWN = 100
PREVIEW_SAFE_OWN = 201
PREVIEW_HIGH_ROOT = 256


def preview_priority_verdict(own: int, ancestors: list[int]) -> tuple[str, str, str] | None:
    """(severity, message, hint) for an ItemPreviewWidget at priority `own` under
    `ancestors` -- outermost first, the layout's root at [0] -- or None when the
    measurement has nothing against it.

    One layout file is all this sees: a root created inside another menu is a
    middle frame in the engine's tree, so a chain spread over two files is
    judged in halves.
    """
    hint = (f"give the preview itself priority {PREVIEW_SAFE_OWN} "
            "(vanilla's tooltip: root 952 > frame 0 > preview 201)")
    if own >= PREVIEW_OWN_NEVER:
        return (REFUSE, f"its own priority {own} is {PREVIEW_OWN_NEVER} or more -- the model never "
                        "drew there (measured)", hint)
    middles = [p for p in ancestors[1:] if p >= PREVIEW_OWN_NEVER]
    if middles:
        return (REFUSE, f"a frame between the root and the preview sits at priority {max(middles)} -- "
                        f"under a middle frame at {PREVIEW_OWN_NEVER} the model never drew (measured)",
                hint)
    top = max(ancestors, default=0)
    if own < PREVIEW_LOW_OWN and top >= PREVIEW_LOW_OWN:
        return (REFUSE, f"its own priority {own} is below {PREVIEW_LOW_OWN} under an ancestor at {top} "
                        "-- that never drew (measured)", hint)
    if own < PREVIEW_SAFE_OWN and top >= PREVIEW_HIGH_ROOT:
        return (WARN, f"its own priority {own} under an ancestor at {top}: own 100 did not draw under "
                      f"roots {PREVIEW_HIGH_ROOT} and 952, own {PREVIEW_SAFE_OWN} drew under every root "
                      "measured", hint)
    return None

#: What may draw a frame behind an edit box.
FRAMING_CLASSES = ("PanelWidgetClass", "ImageWidgetClass", "FrameWidgetClass")


def lint_layout(text: str, file: str = "", vocab: dict | None = None,
                extra_classes: Iterable[str] = ()) -> list[Finding]:
    try:
        root = parse_layout(text)
    except LayoutSyntaxError as exc:
        return [Finding("layout-syntax", REFUSE, f"does not parse: {exc.message}",
                        "the engine's parser is stricter than this one, not looser -- fix the line",
                        file, exc.line)]
    vocab = vocab or load_vocab()
    classes = set(vocab["classes"]) | set(extra_classes)
    keys = set(vocab["keys"])
    multiword = {k for k in keys if " " in k}

    out: list[Finding] = []
    first_seen: dict[str, int] = {}
    for _path, node in root.walk():
        _check_class(node, classes, file, out)
        _check_keys(node, keys, multiword, file, out)
        _check_text(node, multiword, file, out)
        _check_size(node, file, out)
        _check_scroll(node, file, out)
        _check_scriptclass(node, file, out)
        _check_name(node, first_seen, file, out)
        _check_edit_boxes(node, file, out)
        _check_proportional_magnitude(node, file, out)
    _check_preview_priority(root, [], file, out)
    return out


def _check_class(node: LayoutNode, classes: set[str], file: str, out: list[Finding]) -> None:
    if node.cls not in classes:
        out.append(Finding("layout-class", REFUSE,
                           f"{node.cls} is not a widget class the game ships (widget {node.name!r})",
                           "check the spelling against a vanilla layout; a project's own widget class "
                           "has to be listed in build.layout_classes",
                           file, node.line))


def _multiword_match(prop: LayoutProp, multiword: set[str]) -> str | None:
    """The known multi-word key an unquoted property's own tokens spell, if any.

    Vocabulary multi-word keys run two to four words long (`"text color"`,
    `"disabled text color"`, `"size to text h"`). Several of them start with a
    word -- `text`, `size`, `stretch`, `disabled` -- that is ALSO a valid
    standalone key on its own, so the only way to tell "the standalone key,
    followed by its value" from "the multi-word key, unquoted" apart is to
    try reattaching each possible number of leading value tokens and see
    whether that longer string is itself a known key.

    Only UNQUOTED value tokens can be part of an unquoted key -- a quoted
    token is a value written as one (`text "color"` means the literal word
    "color", not the key `text color` missing its quotes), so the search
    stops at the first quoted token. `prop.quoted` empty (a property built
    without that information) is treated as "every value unquoted", so the
    search still runs. The longest reconstruction that is a real key wins:
    `exact text size 32` unquoted names `exact text size`, not the shorter
    `exact text` that also happens to be a real key.
    """
    quoted = prop.quoted if prop.quoted else [False] * len(prop.values)
    limit = 0
    for q in quoted[:3]:
        if q:
            break
        limit += 1
    for n in range(limit, 0, -1):
        candidate = " ".join([prop.key, *prop.values[:n]])
        if candidate in multiword:
            return candidate
    return None


def _check_keys(node: LayoutNode, keys: set[str], multiword: set[str], file: str, out: list[Finding]) -> None:
    for prop in node.props:
        joined = _multiword_match(prop, multiword)
        if joined:
            out.append(Finding("layout-unquoted-key", REFUSE,
                               f"'{joined}' must be quoted as \"{joined}\" -- unquoted, the engine reads "
                               f"key {prop.key!r} with a stray value and ignores it silently",
                               "put the multi-word key in double quotes", file, prop.line))
            continue
        if prop.key in keys:
            continue
        out.append(Finding("layout-key", WARN,
                           f"{prop.key!r} is not a property any vanilla layout uses (widget {node.name!r})",
                           "the engine ignores unknown properties without a word -- check the spelling",
                           file, prop.line))


def _check_text(node: LayoutNode, multiword: set[str], file: str, out: list[Finding]) -> None:
    for prop in node.props:
        if prop.key == "text" and len(prop.values) != 1 and not _multiword_match(prop, multiword):
            out.append(Finding("layout-quote-in-text", REFUSE,
                               f"a quote inside the text of {node.name!r} hangs the engine's layout parser "
                               "(measured 2026-08-30)",
                               "drop the inner quotes, or move the string to a #STR_ key", file, prop.line))


def _check_size(node: LayoutNode, file: str, out: list[Finding]) -> None:
    size = node.prop("size")
    if not size:
        return
    for value in size:
        try:
            if float(value) < 0:
                out.append(Finding("layout-negative-size", REFUSE,
                                   f"{node.name!r} has a negative size ({' '.join(size)}) -- undefined rendering",
                                   "sizes are never negative; move the widget with position instead",
                                   file, node.line))
                return
        except ValueError:
            return


def _check_scroll(node: LayoutNode, file: str, out: list[Finding]) -> None:
    if node.cls == "ScrollWidgetClass" and node.prop("clipchildren") != ["1"]:
        out.append(Finding("layout-scroll-no-clip", WARN,
                           f"ScrollWidgetClass {node.name!r} has no `clipchildren 1` -- overflowing content "
                           "renders outside the viewport",
                           "add `clipchildren 1` to the scroll widget", file, node.line))


def _check_scriptclass(node: LayoutNode, file: str, out: list[Finding]) -> None:
    value = node.prop("scriptclass")
    if value and value[0] and "_" not in value[0]:
        out.append(Finding("layout-scriptclass-prefix", WARN,
                           f"scriptclass {value[0]!r} on {node.name!r} carries no prefix -- scriptclass names "
                           "are global across every loaded mod",
                           "prefix it with the project's tag, e.g. MM_", file, node.line))


def _check_name(node: LayoutNode, first_seen: dict[str, int], file: str, out: list[Finding]) -> None:
    if node.name in first_seen:
        out.append(Finding("layout-dup-name", WARN,
                           f"{node.name!r} is declared twice (first at line {first_seen[node.name]}) -- "
                           "FindAnyWidget returns the first",
                           "give each widget a unique name within the file", file, node.line))
        return
    first_seen[node.name] = node.line


def _rect(node: LayoutNode) -> tuple[float, float, float, float] | None:
    pos = node.prop("position") or ["0", "0"]
    size = node.prop("size")
    if not size or len(pos) < 2 or len(size) < 2:
        return None
    try:
        return float(pos[0]), float(pos[1]), float(size[0]), float(size[1])
    except ValueError:
        return None


def _contains(outer: tuple, inner: tuple) -> bool:
    ox, oy, ow, oh = outer
    ix, iy, iw, ih = inner
    return ox <= ix and oy <= iy and ox + ow >= ix + iw and oy + oh >= iy + ih


def _check_edit_boxes(parent: LayoutNode, file: str, out: list[Finding]) -> None:
    """An edit box draws its frame from its style; without one it needs a
    panel behind it. Vanilla does both (58 of 124 carry a style)."""
    for index, node in enumerate(parent.children):
        if node.cls != "EditBoxWidgetClass" or node.prop("style"):
            continue
        rect = _rect(node)
        framed = parent.cls in FRAMING_CLASSES and parent.prop("style") is not None
        if rect and not framed:
            for sibling in parent.children[:index]:
                if sibling.cls in FRAMING_CLASSES:
                    other = _rect(sibling)
                    if other and _contains(other, rect):
                        framed = True
                        break
        if not framed:
            out.append(Finding("layout-editbox-bare", WARN,
                               f"edit box {node.name!r} has no style and no panel behind it -- it draws no frame",
                               "add `style Default` (vanilla's usual edit-box style), or declare a "
                               "PanelWidgetClass with `style rover_sim_colorable` before it, enclosing it",
                               file, node.line))


def _gt1(value: str) -> bool:
    """True if `value` parses as a number greater than 1 -- False (never a
    finding) for anything unparsable, matching `_check_size`'s own
    tolerance for a value that is not a plain number."""
    try:
        return float(value) > 1
    except ValueError:
        return False


def _check_proportional_magnitude(node: LayoutNode, file: str, out: list[Finding]) -> None:
    """`hexactsize`/`vexactsize` (and the `pos` twins) 0 means "this
    component of `size`/`position` is a FRACTION (0..1) of the parent", not
    a pixel count. An EXPLICIT 0 paired with a component greater than 1 is
    not a large widget, it is hundreds or thousands of PARENT
    widths/heights -- measured on the first gallery run, 2026-09-03:
    `WrapSpacerWidgetClass ContactList { size 600 395 hexactsize 1
    vexactsize 0 }` came back 231148 px tall on the stand, 395 parent
    heights. Only an explicit `0` triggers this -- the vocabulary's own
    default for an ABSENT flag is not settled here, and treating "absent"
    as "proportional" would warn on every ordinary vanilla widget that
    leaves the flag out (`size 1 1` with no flags at all is the single most
    common shape in the vanilla layouts, and is not this bug)."""
    # size/position x horizontal/vertical: one rule, four instances. Written
    # out four times it was four places to change the wording and four
    # chances to change only three of them.
    for prop, flag, index, unit, what in (
        ("size", "hexactsize", 0, "widths", "width"),
        ("size", "vexactsize", 1, "heights", "height"),
        ("position", "hexactpos", 0, "widths from the edge", "x position"),
        ("position", "vexactpos", 1, "heights from the edge", "y position"),
    ):
        values = node.prop(prop)
        if not values or len(values) <= index or node.prop(flag) != ["0"]:
            continue
        if not _gt1(values[index]):
            continue
        out.append(Finding("layout-proportional-magnitude", WARN,
                           f"`{prop} {' '.join(values)}` with `{flag} 0` asks for "
                           f"{values[index]} parent {unit} -- a proportional flag with a "
                           "pixel number",
                           f"set {flag} 1 for a pixel {what}, or a fraction 0..1 for a "
                           "proportional one",
                           file, node.line))


def _check_preview_priority(node: LayoutNode, chain: list[int], file: str, out: list[Finding]) -> None:
    mine = 0
    own = node.prop("priority")
    if own:
        try:
            mine = int(float(own[0]))
        except ValueError:
            pass
    if node.cls == "ItemPreviewWidgetClass":
        verdict = preview_priority_verdict(mine, chain)
        if verdict:
            severity, message, hint = verdict
            out.append(Finding("layout-preview-priority", severity,
                               f"ItemPreviewWidgetClass {node.name!r}: {message}", hint, file, node.line))
    for child in node.children:
        _check_preview_priority(child, chain + [mine], file, out)
