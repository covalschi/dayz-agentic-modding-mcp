"""A DayZ `.layout` file as a tree.

The format is the engine's own property file, not XML: `ClassName Name {`,
one `key value...` per line, an optional `{ ... }` block of child widgets,
and the widget's own `}`. Multi-word keys are quoted ("exact text"), string
values are quoted, `//` comments run to the end of the line. Every widget
class name ends in `Class`. Instance names are optional (e.g., `ScriptParamsClass {`
without a name); a widget can be declared with or without an instance name.
Widgets can have properties, then zero or more `{ ... }` blocks (which may
contain child widgets or be special blocks like `ScriptParamsClass { ... }`).

The tree keeps line numbers, because the consumer that matters most is a
linter, and a finding without a line is a finding nobody fixes. `walk()`
numbers children the way the bridge's DZMCP_Ui.Walk does -- depth-first, in
declaration order, dotted indexes, root is "" -- so an engine node and its
source can be paired by path.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterator


class LayoutSyntaxError(ValueError):
    def __init__(self, line: int, message: str):
        super().__init__(f"line {line}: {message}")
        self.line = line
        self.message = message


@dataclass
class LayoutProp:
    key: str
    values: list[str]
    line: int


@dataclass
class LayoutNode:
    cls: str
    name: str
    line: int
    props: list[LayoutProp] = field(default_factory=list)
    children: list["LayoutNode"] = field(default_factory=list)

    def prop(self, key: str) -> list[str] | None:
        """The first declaration of `key`, or None. First, because that is
        what FindAnyWidget-style lookups do with duplicates too."""
        for p in self.props:
            if p.key == key:
                return p.values
        return None

    def walk(self, path: str = "") -> Iterator[tuple[str, "LayoutNode"]]:
        yield path, self
        for index, child in enumerate(self.children):
            child_path = f"{path}.{index}" if path else str(index)
            yield from child.walk(child_path)


_TOKEN = re.compile(r'"((?:[^"\\]|\\.)*)"|(\S+)')


def _strip_comment(line: str) -> str:
    """Everything before a `//` that is not inside quotes."""
    in_quote = False
    for i, ch in enumerate(line):
        if ch == '"':
            in_quote = not in_quote
        elif not in_quote and line.startswith("//", i):
            return line[:i]
    return line


def tokenize(line: str, lineno: int) -> list[tuple[str, bool]]:
    """Tokens of one line as (text, quoted). Quoted tokens keep their spaces."""
    text = _strip_comment(line).strip()
    if text.count('"') % 2:
        raise LayoutSyntaxError(lineno, "unterminated quote")
    out = []
    for match in _TOKEN.finditer(text):
        if match.group(1) is not None:
            out.append((match.group(1), True))
        else:
            out.append((match.group(2), False))
    return out


def parse_layout(text: str) -> LayoutNode:
    root: LayoutNode | None = None
    stack: list[LayoutNode] = []
    # State per open widget: "header" (waiting for opening `{`), "props"
    # (accepting properties), or "blocked" (after child block, no more properties).
    # Nested `{ ... }` blocks are tracked separately.
    state: list[str] = []
    nesting: list[int] = []  # Track depth of nested braces for each open widget
    lineno = 0

    for lineno, raw in enumerate(text.splitlines(), 1):
        tokens = tokenize(raw, lineno)
        if not tokens:
            continue
        words = [t for t, _ in tokens]

        if words == ["{"]:
            if not stack:
                raise LayoutSyntaxError(lineno, "'{' outside any widget")
            if state[-1] == "header":
                # First `{` after widget header
                state[-1] = "props"
                nesting[-1] = 0
            else:
                # Nested block (child widgets or ScriptParamsClass)
                nesting[-1] += 1
                if state[-1] == "props":
                    # Entering first block, mark as blocked for future
                    state[-1] = "blocked"
            continue

        if words == ["}"]:
            if not stack:
                raise LayoutSyntaxError(lineno, "'}' with no open block")
            if nesting[-1] > 0:
                # Closing a nested block
                nesting[-1] -= 1
            else:
                # Closing the widget itself
                done = stack.pop()
                state.pop()
                nesting.pop()
                if stack:
                    stack[-1].children.append(done)
                elif root is None:
                    root = done
                else:
                    raise LayoutSyntaxError(lineno, f"a second root widget {done.name!r}; a layout has one")
            continue

        # Valid headers: "ClassName" / "ClassName {" / "ClassName InstanceName" / "ClassName InstanceName {"
        is_header = (words[0].endswith("Class") and
                     (len(words) == 1 or  # Just "ClassName"
                      (len(words) == 2 and words[1] == "{") or  # "ClassName {"
                      (len(words) == 2 and words[1] != "{") or  # "ClassName InstanceName"
                      (len(words) == 3 and words[2] == "{"))  # "ClassName InstanceName {"
                     )
        if is_header:
            # A new widget (either root or child)
            # Extract instance name (may be empty/missing)
            if len(words) >= 2 and words[1] != "{":
                instance_name = words[1]
                has_trailing_brace = len(words) == 3 and words[2] == "{"
            else:
                instance_name = ""
                has_trailing_brace = len(words) >= 2 and words[1] == "{"

            if stack and (nesting[-1] <= 0):
                raise LayoutSyntaxError(lineno, f"widget {instance_name!r} declared inside another widget's properties -- a child block needs its own '{{' line")
            if not stack and root is not None:
                raise LayoutSyntaxError(lineno, f"a second root widget {instance_name!r}; a layout has one")
            stack.append(LayoutNode(words[0], instance_name, lineno))
            state.append("header")
            nesting.append(-1)  # -1 means we haven't seen the opening `{` yet
            if has_trailing_brace:
                # `ClassName [Name] {` on same line
                state[-1] = "props"
                nesting[-1] = 0
            continue

        if not stack:
            raise LayoutSyntaxError(lineno, "a property outside any widget")
        if nesting[-1] > 0:
            # Inside a nested block, can only have widget declarations
            raise LayoutSyntaxError(lineno, f"property {words[0]!r} inside a child block")
        if state[-1] != "props":
            raise LayoutSyntaxError(lineno, f"property {words[0]!r} after the child block -- properties come before children")
        # Property of the current widget
        stack[-1].props.append(LayoutProp(words[0], words[1:], lineno))

    if stack:
        raise LayoutSyntaxError(lineno, f"unclosed widget {stack[-1].name!r}")
    if root is None:
        raise LayoutSyntaxError(0, "no widget in the file")
    return root
