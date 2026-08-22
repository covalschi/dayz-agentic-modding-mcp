"""Static checks on Enforce Script, run before anything is packed.

Why this exists at all: every defect it reports costs a full server boot to
find otherwise -- pack, launch, wait, read the log -- and the two most
expensive ones are not in the log at all. `modded class X extends X` compiles,
loads, and silently applies nothing; a mod that modifies a class no longer in
the game does the same. A boot reports success for both.

**Every rule here was measured against the game's own 2810 sources before it
was written**, and the count is recorded beside it. A rule that fires on
vanilla is a rule that would fire on everyone, and a linter that cries wolf is
worse than no linter: the warnings get skipped, including the true ones.

The checks that need the knowledge index -- does this class exist, does any
ancestor declare this method -- live in `lint_index`, apart from these,
because they can only be as complete as the index is. An unbuilt layer makes
them say so rather than accuse.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .knowledge.parse import MODDED as MODDED_FLAG
from .knowledge.parse import strip_source

#: A finding that stops a pack. Reserved for defects with NO ambiguity and no
#: measured false positive -- the same bar phase 5 set for artifact checks.
REFUSE = "refuse"
#: A finding worth reading that a human or an agent may decide to live with.
WARN = "warn"


@dataclass(frozen=True)
class Finding:
    """One defect, where it is, and what to do about it."""

    check: str
    severity: str
    message: str
    hint: str
    file: str = ""
    line: int = 0

    def to_dict(self) -> dict:
        return {
            "check": self.check, "severity": self.severity,
            "message": self.message, "hint": self.hint,
            "file": self.file, "line": self.line,
        }


#: `modded class X extends Y` / `class X extends Y`, with the two names apart
#: so that self-extension is a comparison rather than a pattern.
_EXTENDS_RE = re.compile(
    r"^[ \t]*(?P<modded>modded[ \t]+)?class[ \t]+(?P<name>[A-Za-z_]\w*)"
    r"[ \t]+extends[ \t]+(?P<parent>[A-Za-z_]\w*)",
    re.M,
)

#: Exception statements. Enforce has none: measured on the whole game, `try`,
#: `catch` and `finally` appear 49 times and EVERY ONE is inside a comment or
#: a string -- zero as code. Matched against the stripped view, where those 49
#: are already blanked.
_EXCEPTION_RE = re.compile(r"\b(try|catch|finally)\b[ \t]*[({]")


def lint_text(text: str, file: str = "") -> list[Finding]:
    """Every finding that one source can produce on its own.

    Reads the parser's stripped view, so a keyword inside a comment or a
    string literal is not code here -- which is most of the reason a text
    sweep cannot do this job.
    """
    stripped = strip_source(text)
    code = stripped.code
    out: list[Finding] = []
    out += _self_extension(code, file)
    out += _exceptions(code, file)
    out += _continuations(code, file)
    return sorted(out, key=lambda f: (f.line, f.check))


def _line_of(code: str, pos: int) -> int:
    return code.count("\n", 0, pos) + 1


def _self_extension(code: str, file: str) -> list[Finding]:
    """A class that extends itself.

    On `modded` this is the single most expensive silent failure in DayZ
    modding: it compiles, it loads, and it applies nothing. On a plain class
    it is a cycle the engine cannot resolve.
    """
    out = []
    for m in _EXTENDS_RE.finditer(code):
        name, parent = m.group("name"), m.group("parent")
        if name.lower() != parent.lower():
            continue
        line = _line_of(code, m.start())
        if m.group("modded"):
            out.append(Finding(
                "modded-self", REFUSE,
                f"`modded class {name} extends {name}` modifies nothing",
                f"write `modded class {name}` -- a modded class already IS the "
                "class it names, and naming it again as the base compiles, "
                "loads and silently applies none of the changes",
                file, line,
            ))
        else:
            out.append(Finding(
                "class-self", REFUSE,
                f"`class {name} extends {name}` inherits from itself",
                "name the real base class, or drop `extends` if there is none",
                file, line,
            ))
    return out


def _exceptions(code: str, file: str) -> list[Finding]:
    out = []
    for m in _EXCEPTION_RE.finditer(code):
        word = m.group(1)
        out.append(Finding(
            "exceptions", REFUSE,
            f"`{word}` is not Enforce Script",
            "Enforce has no exceptions: check the value and return, or guard "
            "with `if`. Measured on the whole game: not one `try`, `catch` or "
            "`finally` is written as code",
            file, _line_of(code, m.start()),
        ))
    return out


def _continuations(code: str, file: str) -> list[Finding]:
    """A line whose first character is `+`, where the line above did not end.

    An Enforce statement ends at the end of its line, so the second half is
    dropped without a word. Measured on the whole game: NOT ONE line begins
    with `+`, so this cannot fire on ordinary code.
    """
    out = []
    previous = ""
    for number, raw in enumerate(code.splitlines(), 1):
        line = raw.strip()
        if line.startswith("+") and not line.startswith("++"):
            if previous and previous[-1] not in ";{}":
                out.append(Finding(
                    "line-continuation", WARN,
                    "a statement was continued onto the next line with `+`",
                    "an Enforce statement ends at the end of its line: put the "
                    "whole expression on one line, or build it up with `s = s + ...`",
                    file, number,
                ))
        if line:
            previous = line
    return out


# --------------------------------------------------------------- with the index

def lint_index(declarations, store, *, mods=None) -> list[Finding]:
    """The checks that need to know what else exists.

    `modded class X` where nothing declares `X` is the second silent failure:
    the mod loads and modifies nothing, exactly as self-extension does. It can
    only be REPORTED as a defect when the layers that could hold `X` are
    actually built -- otherwise the honest answer is "I looked in an index
    that does not have the game in it yet", and that is what it says.

    `mods` is the active mod set, so that a target living in a mod the server
    does not run is reported rather than silently accepted: on that server the
    mod would load and modify nothing, which is the whole point.

    **There is deliberately no "extends an unknown class" rule.** It was
    written, measured, and removed: it fires **58 times on the game's own
    scripts**, because types such as `BuildingSuper` and `ParamsWriteContext`
    are provided by the engine and declared in no script anywhere. No index
    built from scripts can ever know them, so the rule could not be made
    sound -- and a rule that fires on vanilla is a rule that fires on
    everyone. `modded-target` was measured the same way and kept: zero
    findings on vanilla's 3 modded classes and on 19 in real mods.
    """
    from .knowledge.parse import CLASS
    from .knowledge.store import CORE, DEPS

    built = {info.name for info in store.layers()}
    missing_layers = [name for name in (CORE, DEPS) if name not in built]
    severity = WARN if missing_layers else REFUSE
    blind = (
        " (the " + ", ".join(missing_layers) + " layer(s) are not built, so this "
        "is what the index knows, not what the game has)" if missing_layers else ""
    )

    def declared(name: str) -> bool:
        return bool(store.find(name, kind=CLASS, limit=1, mods=mods))

    out: list[Finding] = []
    for d in declarations:
        if d.kind != CLASS:
            continue
        if MODDED_FLAG in d.flags:
            if not declared(d.name):
                out.append(Finding(
                    "modded-target", severity,
                    f"`modded class {d.name}` modifies a class nothing declares"
                    + blind,
                    "check the spelling and the case, and check that the mod "
                    "declaring it is a dependency of this project -- a modded "
                    "class with no target compiles, loads and does nothing",
                    d.file, d.line,
                ))
            continue
    return sorted(out, key=lambda f: (f.file, f.line, f.check))
