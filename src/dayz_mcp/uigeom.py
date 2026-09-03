"""The one place the engine's own "x y w h" rectangle string is read.

Three modules parsed it independently before this one existed --
`tools/ui.py` (a node's screen rectangle and the host), `uicheck.py` (the same
rectangles, for the checks) and `uireport.py` (again, to draw the boxes) --
three copies of the same four lines, one edit away from drifting apart. This
is float-tolerant, because the engine sometimes writes "100.0" for "100", and
None when the text is not four numbers, because a caller that got a wrong
rectangle back instead would draw or check against it as if it were real.
"""
from __future__ import annotations


def parse_rect(text: str) -> tuple[int, int, int, int] | None:
    """An `x y w h` string as four ints, or None if it is not one."""
    parts = str(text).split()
    if len(parts) != 4:
        return None
    try:
        return tuple(int(float(p)) for p in parts)  # type: ignore[return-value]
    except ValueError:
        return None
