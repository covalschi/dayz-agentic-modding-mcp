"""`mod_lint`: judge a mod's script before anything is packed.

The point is the boot it saves. Every other verdict in this server needs the
game: pack, launch, wait, read the log. These checks need a text editor's
worth of work, and two of the things they catch are not in any log at all --
a `modded class` that modifies nothing loads and runs and reports success.

What it refuses on is deliberately narrow. Each refusing rule was measured
against the game's own 2810 sources and against real mods before it was
written, and the counts are in `lint.py` beside each rule. A linter that cries
wolf gets skipped, and then it is worth less than nothing.
"""

from __future__ import annotations

import time
from pathlib import Path

from ..errors import Result, fail
from ..knowledge.layers import SCRIPT_SUFFIXES
from ..knowledge.parse import parse_source
from ..layoutlint import lint_layout
from ..lint import REFUSE, WARN, lint_index, lint_text
from ..profile import resolve_mod_dir
from . import session
from .knowledge import _active, _index, _scoped
from .project import require_project

#: A ceiling on how much is read in one call, so that pointing this at a tree
#: that is not a mod costs a bounded amount of time. Reaching it is reported,
#: never silent: a lint that stopped early and said nothing would be read as
#: "clean", which is the one answer it must never fake.
MAX_FILES = 5000


def mod_lint(mod: str = "", strict: bool = False) -> Result:
    """Check a mod's Enforce Script without packing or booting anything.

    `mod` limits the check to one of the project's mods; empty checks them
    all. `strict` makes warnings count as failure too -- off by default,
    because a warning is something to read, not something to stop for.

    Refusals: a class that extends itself (`modded class X extends X` loads
    and applies nothing), an exception statement (`try`/`catch`/`finally` are
    not Enforce), and a `modded class` whose target nothing declares.

    Warnings: a statement continued onto the next line with a leading `+`,
    which Enforce drops silently.

    The `modded class` check needs the knowledge index, and says so when the
    index cannot answer: an unbuilt layer makes it warn rather than accuse,
    because "no such class" and "I have not read the game yet" are different
    answers and only one of them is the mod's fault.

    `.layout` files are checked too: a quote inside a text value, an
    unquoted multi-word key, an unknown widget class and an ItemPreview
    under a priority above 256 refuse; a bare edit box, a scroll widget
    without clipchildren, a duplicate name and an unprefixed scriptclass
    warn.
    """
    guard = require_project()
    if guard:
        return guard
    prof = session.profile()

    wanted = [m for m in prof.build.mods if not mod or m == mod]
    if mod and not wanted:
        return fail(
            f"{mod!r} is not a mod of this project",
            hint="the project declares: " + ", ".join(prof.build.mods),
        )

    started = time.perf_counter()
    findings = []
    declarations = []
    scanned: list[str] = []
    truncated = False
    layouts = 0
    for name in wanted:
        root = resolve_mod_dir(prof.root, prof.build.sources, name)
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            lower = path.name.lower()
            is_layout = lower.endswith(".layout")
            if not is_layout and not lower.endswith(SCRIPT_SUFFIXES):
                continue
            if len(scanned) >= MAX_FILES:
                truncated = True
                break
            label = path.relative_to(prof.root).as_posix()
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                findings.append(
                    {"check": "unreadable", "severity": WARN, "file": label, "line": 0,
                     "message": f"could not be read: {exc}",
                     "hint": "check the file is not locked by another program"}
                )
                continue
            scanned.append(label)
            if is_layout:
                layouts += 1
                findings += [f.to_dict() for f in lint_layout(text, label)]
                continue
            findings += [f.to_dict() for f in lint_text(text, label)]
            declarations += parse_source(text, file=label)

    index_note = ""
    store, failure = _index()
    if store is None:
        # Not a refusal: the checks that need no index have already run, and
        # saying which half was skipped beats failing the whole call.
        index_note = (failure.error if failure else "the index could not be opened")
    else:
        active = _active(store)
        findings += [f.to_dict() for f in lint_index(
            declarations, store, mods=_scoped(active)
        )]

    findings.sort(key=lambda f: (f["file"], f["line"], f["check"]))
    refusals = [f for f in findings if f["severity"] == REFUSE]
    warnings = [f for f in findings if f["severity"] == WARN]
    data = {
        "mods": wanted,
        "files": len(scanned),
        "layouts": layouts,
        "declarations": len(declarations),
        "truncated": truncated,
        "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 1),
        "refusals": len(refusals),
        "warnings": len(warnings),
        "findings": findings,
        "index_skipped": index_note,
    }
    hints = []
    if truncated:
        hints.append(f"stopped after {MAX_FILES} files -- lint one mod at a time")
    if index_note:
        hints.append("the `modded class` target check did not run: " + index_note)
    if warnings and not refusals:
        hints.append("warnings do not stop a build; pass strict=True to make them")
    hint = "; ".join(hints)

    if refusals:
        first = refusals[0]
        return Result(
            False, data,
            f"{len(refusals)} defect(s) that no server boot would report: "
            f"{first['file']}:{first['line']} {first['message']}",
            "; ".join([first["hint"], *hints]),
        )
    if strict and warnings:
        first = warnings[0]
        return Result(
            False, data,
            f"{len(warnings)} warning(s), and strict=True was asked for: "
            f"{first['file']}:{first['line']} {first['message']}",
            "; ".join([first["hint"], *hints]),
        )
    return Result(True, data, "", hint)
