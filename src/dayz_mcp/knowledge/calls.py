"""Call sites: one record per place a name is called or instantiated.

Separate from `parse.py` only because that module is already long; the scan
itself runs inside the parser's own walk, which is the only place that knows
which class and which method a body belongs to. Nothing here imports the
parser -- the dependency points one way, from `parse` to here.

What is deliberately NOT recorded, so that a caller reading an answer knows
what it does not contain:

* templated instantiation -- `new array<string>()` puts `>` immediately before
  the parenthesis, so there is no identifier to record. Recording `array`
  would mean parsing the template, and no question in this index is asked
  about it;
* the declaration a call sits in is recorded by NAME, not by identity. Two
  methods with the same name on the same class -- which Enforce does not
  allow -- would be indistinguishable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: How a name was reached at the call site.
CALL = "call"
NEW = "new"

#: Words that are followed by `(` and are not calls. `new` is here as well:
#: it is recognised before the identifier it constructs, and recording it as
#: a call to something named "new" would be noise in every answer.
_NOT_CALLS = frozenset(
    """if else while for foreach switch case default return break continue
    delete new super this thread typedef class enum modded sizeof typeof
    catch try do""".split()
)

#: An identifier immediately before an opening parenthesis. Whitespace between
#: the two is allowed because it is written -- `Print (x)` is a call. The
#: lookbehind excludes only a preceding word character, so that half of a
#: longer identifier is never matched; a preceding DOT must be allowed, since
#: `obj.Method(` is the commonest call site there is.
_CALL_RE = re.compile(r"(?<!\w)([A-Za-z_]\w*)\s*\(")

#: `new` immediately before an identifier. Matched separately from the call
#: itself so that `new Item()` is ONE record of kind `new`, not a `new` record
#: plus a `call` record for the same parenthesis.
_NEW_BEFORE = re.compile(r"\bnew\s+$")


@dataclass(frozen=True)
class Call:
    """One place a name is called, and the declaration it was called from.

    `owner` is the class the calling code sits in, empty for a global
    function. `method` is the method it sits in, empty for a call written at
    class scope (a member initialiser). `qualifier` is what stood before the
    dot -- `super`, a class name for a cast, or a whole expression such as
    `GetGame()` -- and is empty for an unqualified call.
    """

    name: str
    kind: str = CALL
    owner: str = ""
    method: str = ""
    qualifier: str = ""
    file: str = ""
    line: int = 0


def _qualifier(code: str, at: int) -> str:
    """What stood before the dot in front of the identifier starting at `at`.

    Returns "" when the call is unqualified. Walks backwards rather than
    matching a pattern forwards, because the qualifier can be a whole call --
    `GetGame().CreateObject()` -- and a forward pattern for that is a pattern
    for every expression Enforce can write.
    """
    i = at - 1
    while i >= 0 and code[i].isspace():
        i -= 1
    if i < 0 or code[i] != ".":
        return ""
    end = i  # exclusive: the dot itself is not part of the qualifier
    i -= 1
    while i >= 0 and code[i].isspace():
        i -= 1
    if i < 0:
        return ""
    if code[i] == ")":
        depth = 0
        while i >= 0:
            if code[i] == ")":
                depth += 1
            elif code[i] == "(":
                depth -= 1
                if depth == 0:
                    break
            i -= 1
        if i < 0:
            return ""
        i -= 1
        while i >= 0 and code[i].isspace():
            i -= 1
    while i >= 0 and (code[i].isalnum() or code[i] == "_"):
        i -= 1
    return code[i + 1 : end].strip()


def find_calls(code: str, start: int, end: int) -> list[tuple[str, str, str, int]]:
    """Every call site in `code[start:end]`, as `(name, kind, qualifier, pos)`.

    `code` is the parser's stripped view: comments are blanked and string
    CONTENTS are blanked, so a call written inside a string or a comment
    cannot be found here. That is the whole reason this reads the stripped
    view rather than the source.
    """
    out: list[tuple[str, str, str, int]] = []
    for m in _CALL_RE.finditer(code, start, end):
        name = m.group(1)
        if name in _NOT_CALLS:
            continue
        at = m.start(1)
        kind = NEW if _NEW_BEFORE.search(code, max(0, at - 16), at) else CALL
        out.append((name, kind, _qualifier(code, at), at))
    return out
