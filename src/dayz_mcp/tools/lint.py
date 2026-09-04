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
from ..layoutgen import GENERATED_MARK, LayoutGenError, build_project, target_path
from ..layoutlint import lint_layout
from ..layoutvocab import load_vocab
from ..lint import Finding, REFUSE, WARN, lint_index, lint_text
from ..profile import resolve_mod_dir
from . import session
from .knowledge import _active, _index, _scoped
from .project import require_project

#: A ceiling on how much is read in one call, so that pointing this at a tree
#: that is not a mod costs a bounded amount of time. Reaching it is reported,
#: never silent: a lint that stopped early and said nothing would be read as
#: "clean", which is the one answer it must never fake.
MAX_FILES = 5000


def _read_text(path: Path) -> tuple[str | None, str]:
    """A file's text, or None and the reason: a locked or vanished layout is
    a finding, never a crash of the whole verdict."""
    try:
        return path.read_text(encoding="utf-8", errors="replace"), ""
    except OSError as exc:
        return None, str(exc)


def _first_difference(a: str, b: str) -> int:
    """1-based line where `a` and `b` first differ (CRLF read as LF)."""
    left = a.replace("\r\n", "\n").split("\n")
    right = b.replace("\r\n", "\n").split("\n")
    for i, (x, y) in enumerate(zip(left, right), start=1):
        if x != y:
            return i
    return min(len(left), len(right)) + 1


def _generated_layout_findings(prof, mods: list[str]) -> list:
    """The three ways a generated layout and its description drift apart:
    the file is behind the description (layout-stale, refuse -- mod_build
    would ship stale geometry); a file says it is generated but nothing
    generates it (layout-orphan, warn); a description does not build or
    raised a note (layout-desc: refuse / warn)."""
    out = []
    try:
        report = build_project(prof.root, mods, prof.build.sources, write=False, tokens_path=prof.build.tokens)
    except LayoutGenError as exc:
        return [Finding("layout-desc", REFUSE, str(exc), "fix the description under ui/", exc.file, 0)]
    for note in report.notes:
        where, _, message = note.partition(": ")
        # `where` is "<description file> <node>". The file becomes the
        # finding's own field; the node stays in the message, because a page
        # of forty widgets warning "color given as a literal" with nothing
        # to point at is a finding nobody can act on.
        desc_file, _, node = where.partition(" ")
        out.append(Finding("layout-desc", WARN, f"{node}: {message}" if node else message,
                           "tokens instead of literals, containers instead of coordinates",
                           desc_file, 0))
    for target in report.written:
        src = report.sources[target]
        disk = target_path(prof.root, prof.build.sources, target)
        if not disk.is_file():
            out.append(Finding("layout-stale", REFUSE, f"{target} is described by {src} but has not been built",
                               "run layout_build", target, 0))
            continue
        current, why = _read_text(disk)
        if current is None:
            out.append(Finding("unreadable", WARN, f"{target} could not be read: {why}",
                               "check the file is not locked by another program", target, 0))
            continue
        line = _first_difference(current, report.files[target])
        out.append(Finding("layout-stale", REFUSE, f"{target} is behind {src} (first difference at line {line})",
                           "run layout_build and commit the result", target, line))
    generated = set(report.files)
    for name in mods:
        mod_root = resolve_mod_dir(prof.root, prof.build.sources, name)
        for path in sorted(mod_root.rglob("*.layout")):
            try:
                rel = path.relative_to(prof.root).as_posix()
            except ValueError:
                rel = path.as_posix()
            # `generated` is keyed LOGICALLY (`<Mod>/gui/layouts/x.layout`),
            # which is the disk path only when `sources[mod] == mod`. The
            # finding still names the disk-relative path, because that is
            # the file a reader has to go and delete.
            logical = f"{name}/{path.relative_to(mod_root).as_posix()}"
            if logical in generated:
                continue
            text, why = _read_text(path)
            if text is None:
                out.append(Finding("unreadable", WARN, f"{rel} could not be read: {why}",
                                   "check the file is not locked by another program", rel, 0))
                continue
            if text.startswith(GENERATED_MARK):
                out.append(Finding("layout-orphan", WARN,
                                   f"{rel} says it is generated but no description under ui/ makes it",
                                   "delete it, or restore its description", rel, 1))
    return out


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

    A generated `.layout` (first line `// GENERATED by dayz-mcp
    layout_build`) is rebuilt in memory from its description under `ui/`
    and compared: behind its description -- refuse (`layout-stale`);
    generated but described by nothing -- warn (`layout-orphan`); a
    description that does not build refuses, a note it raises warns
    (`layout-desc`).
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
    vocab = load_vocab()
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
                findings += [f.to_dict() for f in lint_layout(text, label, vocab=vocab, extra_classes=prof.build.layout_classes)]
                continue
            findings += [f.to_dict() for f in lint_text(text, label)]
            declarations += parse_source(text, file=label)

    findings += [f.to_dict() for f in _generated_layout_findings(prof, wanted)]

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
