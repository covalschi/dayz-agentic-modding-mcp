"""Static checks on `.layout` files, before any client is started.

Refusals are the defects the engine reports nowhere: a quote inside a text
value hangs its layout parser (measured 2026-08-30 by bisecting a file), an
unquoted multi-word key or an unknown class is ignored without a word, and an
ItemPreviewWidget under a priority above 256 draws nothing (measured: 256
draws, 300 does not). Warnings are the things the eyes (ui_preview) settle
for certain but which are cheap to name here.
"""
from __future__ import annotations

from typing import Iterable

from .layoutparse import LayoutNode, LayoutProp, LayoutSyntaxError, parse_layout
from .layoutvocab import load_vocab
from .lint import REFUSE, WARN, Finding

#: Above this, ItemPreviewWidget silently draws nothing. Measured live 2026-08
#: (OZ_PdaMenu): 256 draws, 300 does not; vanilla never goes above 151.
PREVIEW_PRIORITY_MAX = 256

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
    _check_preview_priority(root, 0, file, out)
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
                           "prefix it with the project's tag, e.g. OZ_", file, node.line))


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


def _check_preview_priority(node: LayoutNode, inherited: int, file: str, out: list[Finding]) -> None:
    own = node.prop("priority")
    current = inherited
    if own:
        try:
            current = max(inherited, int(float(own[0])))
        except ValueError:
            pass
    if node.cls == "ItemPreviewWidgetClass" and current > PREVIEW_PRIORITY_MAX:
        out.append(Finding("layout-preview-priority", REFUSE,
                           f"ItemPreviewWidgetClass {node.name!r} sits under priority {current}, above "
                           f"{PREVIEW_PRIORITY_MAX} -- the engine draws nothing there (measured)",
                           "lower the priority of the preview widget and of every ancestor to 256 or less",
                           file, node.line))
    for child in node.children:
        _check_preview_priority(child, current, file, out)
