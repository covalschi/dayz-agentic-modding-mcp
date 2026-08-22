"""Three layers, three rhythms, and a rebuild that costs what changed.

The game is updated every few weeks, a dependency when its author publishes,
and the project's own code between one agent turn and the next. One index
built in one pass would be an index nobody rebuilds -- and an index nobody
rebuilds answers about yesterday's code with today's confidence, which is
worse than having none.

| layer     | source                                   | unit of change |
|-----------|------------------------------------------|----------------|
| `core`    | the game's `scripts.pbo` and its configs | the game       |
| `deps`    | the archives of the mods a project needs | one archive    |
| `project` | the project's own files, read where they lie | one file   |

The unit column is the whole design. The store measured one file at 4.1 ms
against 3.93 s for a full layer -- 870x -- and that ratio only exists if the
layer above it re-reads exactly what changed. So each layer scans its tree
once, compares size and modification time against what the index recorded,
and touches nothing else. For the project a source is a file, because a file
is what an editor saves. For dependencies a source is a whole archive,
because an archive is what a workshop update replaces.

**Dependencies are read out of their archives, not unpacked.** Measured on
this machine: 36 installed mods, 523 archives, 86 GB, of which this index
wants 56 MB. Unpacking to read that would be eighty-six gigabytes through the
disk for fifty-six megabytes of answer, redone whenever one mod is
republished. `pbo.py` reads the entry table and seeks to what it needs.

**A third-party archive is allowed to be odd; it is not allowed to be fatal.**
The store refuses a duplicate record key loudly, and loud is right for a bug
in our own parser. It is wrong for a quirk in somebody else's file: failing a
whole dependency layer because one of three dozen mods ships one strange
source would make the feature useless, and dropping the record silently is
the exact failure this phase exists to prevent. So the default is neither:
duplicates are deduplicated by the store's own key formula, the loss is
counted, and the file is named in the report. `on_duplicate=FAIL` restores
the strict behaviour for callers who want a bug to stop the build.

Measured before that policy was chosen, on the 36 real mods: 103 166
declarations, **zero** record-key collisions. The tolerance is insurance, not
a workaround -- which is exactly why it must not be silent.

What is NOT filtered here: conditional compilation. The parser indexes every
declaration and records the `#ifdef` it sits under, because this server drives
server, client and diagnostic builds and there is no one define set that is
the truth. Filtering at this level would make the index answer "no such
method" about a method that exists in the build the agent is running.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Iterator, Sequence

from ..packer import bankrev_cmd, bankrev_output, config_text_cmd
from ..paths import BANKREV_REL, CFGCONVERT_REL, find_tools
from ..procs import run_blocking
from .calls import Call
from .parse import Declaration, parse_all, parse_config
from .pbo import DEFAULT_LIMITS, PboError, PboLimits, scan_pbo
from .store import (
    CORE,
    DEPS,
    PROJECT,
    DuplicateDeclaration,
    KnowledgeStore,
    Staleness,
    record_key,
)

#: What to do when two declarations claim the same record row.
REPORT = "report"  # keep the first, count the loss, name the file
FAIL = "fail"      # refuse the build, as the store does on its own

#: "Work it out for yourself" -- distinct from None, which means "there is
#: none, and do not go looking". A test on a machine that happens to have
#: DayZ Tools installed must be able to say "pretend it does not".
AUTO = "auto"

SCRIPT_SUFFIXES: tuple[str, ...] = (".c",)
#: Configs and their include fragments. Included files are indexed as sources
#: in their own right rather than followed, so nothing is lost by not
#: resolving `#include`, and nothing is invented by guessing where it points.
CONFIG_SUFFIXES: tuple[str, ...] = (".cpp", ".hpp", ".h")
BINARY_CONFIG = "config.bin"

#: Directories that never hold a source of this project's own. A folder whose
#: name starts with an at-sign is a built mod -- the packed copy of the very
#: files being indexed -- and a dot-directory is somebody's working state,
#: this server's own included.
_SKIP_PREFIXES = (".", "@")
_SKIP_NAMES = frozenset({"__pycache__", "node_modules"})

#: How long CfgConvert gets for one binarised config. Generous: the largest in
#: the game is a hundred kilobytes and converts in 70 ms. A ceiling all the
#: same, because a tool that never returns is the one failure an agent cannot
#: diagnose.
CONVERT_TIMEOUT = 60.0
UNPACK_TIMEOUT = 600.0


class LayerBuildError(Exception):
    """The layer cannot be built, and saying so beats building half of one."""


class _SourceFailed(Exception):
    """Internal: this one source is out, the layer goes on without it."""


@dataclass(frozen=True)
class FileStat:
    """A file as the directory walk saw it. The three numbers staleness is
    measured from, carried away from a walk that had them anyway."""

    path: str
    size: int
    mtime: float


@dataclass(frozen=True)
class SkippedSource:
    """Something the build could not take whole, named rather than swallowed.

    `lost` counts declarations dropped from a source that was otherwise
    indexed (a duplicate record key). A source that could not be read at all
    is listed with `lost = 0`, because how much it held is exactly what could
    not be found out.
    """

    path: str
    reason: str
    lost: int = 0

    def to_dict(self) -> dict:
        return {"path": self.path, "reason": self.reason, "lost": self.lost}


@dataclass(frozen=True)
class LayerReport:
    """What one build did, in numbers a caller can act on.

    `indexed` and `unchanged` are the pair that proves incrementality: a
    rebuild after one edit reads 1 and skips the rest, and a rebuild that
    quietly re-read everything would pass every other check in this module.
    """

    layer: str
    root: str = ""
    sources: int = 0
    declarations: int = 0
    indexed: int = 0
    removed: int = 0
    unchanged: int = 0
    lost: int = 0
    seconds: float = 0.0
    incremental: bool = True
    skipped: tuple[SkippedSource, ...] = ()
    notes: tuple[str, ...] = ()

    def describe(self) -> str:
        how = "rebuilt" if not self.incremental else "updated"
        parts = [f"{self.indexed} read", f"{self.unchanged} unchanged"]
        if self.removed:
            parts.append(f"{self.removed} gone")
        if self.lost:
            parts.append(f"{self.lost} declaration(s) dropped as duplicates")
        if self.skipped:
            parts.append(f"{len(self.skipped)} source(s) skipped")
        return (
            f"{self.layer} {how} in {self.seconds:.2f}s: "
            f"{self.sources} sources, {self.declarations} declarations "
            f"({', '.join(parts)})"
        )

    def to_dict(self) -> dict:
        return {
            "layer": self.layer,
            "root": self.root,
            "sources": self.sources,
            "declarations": self.declarations,
            "indexed": self.indexed,
            "removed": self.removed,
            "unchanged": self.unchanged,
            "lost": self.lost,
            "seconds": round(self.seconds, 3),
            "incremental": self.incremental,
            "skipped": [s.to_dict() for s in self.skipped],
            "notes": list(self.notes),
            "summary": self.describe(),
        }


# ------------------------------------------------------------- walking a tree


def _skip_dir(name: str) -> bool:
    return name.startswith(_SKIP_PREFIXES) or name in _SKIP_NAMES


def scan_tree(
    root: str | Path,
    *,
    suffixes: Sequence[str] = (),
    names: Sequence[str] = (),
    skip: Callable[[str], bool] = _skip_dir,
) -> list[FileStat]:
    """Every source under `root`, with the size and time it had during the walk.

    `os.scandir` rather than a walk plus a stat per file: on Windows the
    directory entry already carries both numbers, so staleness costs the walk
    and nothing more. The store measured the other way round at 410 ms for the
    vanilla layer's 2810 separate `os.stat` calls -- paid on every status
    check, for numbers the walk was about to hand over for free.

    Matching is case-insensitive: one machine's modpack spells the same folder
    `addons` and `Addons`, and a config is `config.cpp` in one mod and
    `Config.cpp` in the next.
    """
    want_suffix = tuple(s.lower() for s in suffixes)
    want_names = {n.lower() for n in names}
    out: list[FileStat] = []
    seen: set[str] = set()
    stack = [(str(root), True)]
    while stack:
        directory, linked = stack.pop()
        if linked:
            # A cycle has to pass through a link, so only links need resolving.
            # Mod folders are junctions on this machine, and `realpath` on
            # every directory cost a third of the walk for a guard that plain
            # directories can never need.
            try:
                real = os.path.realpath(directory)
            except OSError:  # pragma: no cover - a path that vanished mid-walk
                continue
            if real in seen:
                continue
            seen.add(real)
        try:
            entries = list(os.scandir(directory))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir():
                    if not skip(entry.name):
                        stack.append((entry.path, entry.is_symlink()))
                    continue
                low = entry.name.lower()
                if low in want_names or (want_suffix and low.endswith(want_suffix)):
                    stat = entry.stat()
                    out.append(FileStat(entry.path, stat.st_size, stat.st_mtime))
            except OSError:
                continue
    out.sort(key=lambda f: f.path)
    return out


def staleness_of(store: KnowledgeStore, layer: str, scanned: Sequence[FileStat]) -> Staleness:
    """What a layer would have to re-read, measured against a walk already done.

    The same answer `KnowledgeStore.staleness` gives, without stat'ing every
    source a second time: the walk that produced `scanned` had the numbers.
    """
    if store.layer(layer) is None:
        return Staleness(layer=layer, never_built=True)
    recorded = {s.path: s for s in store.sources(layer)}
    current = {f.path: f for f in scanned}
    changed: list[str] = []
    added: list[str] = []
    unchanged = 0
    for path, found in current.items():
        was = recorded.get(path)
        if was is None:
            added.append(path)
        elif was.size != found.size or was.mtime != found.mtime:
            changed.append(path)
        else:
            unchanged += 1
    return Staleness(
        layer=layer,
        changed=tuple(sorted(changed)),
        missing=tuple(sorted(p for p in recorded if p not in current)),
        added=tuple(sorted(added)),
        unchanged=unchanged,
        scanned_for_new=True,
    )


# ----------------------------------------------------------------- the engine


@dataclass
class _Source:
    """One unit of incrementality: what it is, what it was, how to read it."""

    key: str
    size: int
    mtime: float
    #: Declarations AND call sites, from one read of the source. Two reads
    #: would double the cost of the slowest part of a build.
    load: Callable[[], tuple[list[Declaration], list[Call]]]


@dataclass
class _Build:
    """The state one build accumulates besides the index itself."""

    notes: list[str] = field(default_factory=list)
    problems: list[SkippedSource] = field(default_factory=list)
    lost: int = 0
    #: Binarised configs nobody could read, kept apart on purpose: "there is
    #: no converter" and "the converter refused this file" are different
    #: facts, and one note covering both told the reader the tool was missing
    #: on a machine where it was installed and working.
    binary_unread: int = 0
    binary_failed: int = 0
    first_failure: str = ""
    #: Either a converter, None for "there is none", or AUTO for "find one if
    #: a binarised config actually turns up". Resolved lazily: discovery reads
    #: the registry and Steam's library files, and a build that meets no
    #: `config.bin` has no business paying for that.
    convert: Callable[[Path, Path], str] | None | str = AUTO
    tools: str | Path | None = AUTO
    resolved: bool = False
    tempdir: str = ""

    def converter(self) -> Callable[[Path, Path], str] | None:
        if not self.resolved:
            self.convert = _resolve_convert(self.convert, self.tools)
            self.resolved = True
        return self.convert  # type: ignore[return-value]

    def note(self, text: str) -> None:
        if text not in self.notes:
            self.notes.append(text)

    def scratch(self) -> Path:
        if not self.tempdir:
            self.tempdir = tempfile.mkdtemp(prefix="dayz-mcp-cfg-")
        return Path(self.tempdir)

    def cleanup(self) -> None:
        if self.tempdir:
            shutil.rmtree(self.tempdir, ignore_errors=True)
            self.tempdir = ""


def _dedupe(
    layer: str, declarations: list[Declaration], on_duplicate: str, build: _Build, source: str
) -> list[Declaration]:
    """Drop declarations that would claim a row another one already holds.

    Uses the store's own `record_key`, so this can never disagree with the
    constraint it exists to satisfy. Two declarations that collide here are
    indistinguishable to every question the index can be asked -- same layer,
    name, kind, owner, file and line -- so the one kept answers for both. What
    must not happen is that nobody is told, which is why the loss is counted
    and the file named.
    """
    seen: set = set()
    kept: list[Declaration] = []
    dropped = 0
    first: Declaration | None = None
    for declaration in declarations:
        key = record_key(layer, declaration)
        if key in seen:
            dropped += 1
            if first is None:
                first = declaration
            continue
        seen.add(key)
        kept.append(declaration)
    if dropped and first is not None:
        detail = (
            f"{dropped} declaration(s) claim a record key an earlier one already "
            f"holds (first: {first.kind} {first.name} at line {first.line}); "
            "kept the first of each"
        )
        if on_duplicate == FAIL:
            raise LayerBuildError(f"{first.file or source}: {detail}")
        build.lost += dropped
        build.problems.append(
            SkippedSource(path=first.file or source, reason=detail, lost=dropped)
        )
    return kept


def _load(
    source: _Source, layer: str, on_duplicate: str, build: _Build
) -> tuple[list[Declaration], list[Call]]:
    try:
        declarations, calls = source.load()
    except (OSError, PboError, ValueError, UnicodeError) as exc:
        build.problems.append(
            SkippedSource(path=source.key, reason=f"{type(exc).__name__}: {exc}")
        )
        raise _SourceFailed from exc
    # Only declarations are de-duplicated. Two call sites of the same name in
    # one file are two real call sites, and collapsing them would turn "called
    # in eleven places" into "called".
    return _dedupe(layer, declarations, on_duplicate, build, source.key), calls


def _record_empty(store: KnowledgeStore, layer: str, root: str, source: _Source) -> None:
    """Remember that a source was read and yielded nothing.

    Without this, an archive that cannot be read is "new" on every single
    build, and gets walked again on every staleness check. Measured on this
    machine: three archives out of 523 carry entry tables past the ceiling,
    and re-walking them cost 2.2 s of every check for an answer that never
    changed.

    The trade is stated rather than hidden: those three are named in the
    report of the build that discovered them and in every `full=True` rebuild,
    not in the quick checks in between. They are re-read the moment their size
    or modification time moves -- which is the only event that could make an
    unreadable archive readable.
    """
    try:
        store.put_source(layer, source.key, (), root=root, size=source.size, mtime=source.mtime)
    except DuplicateDeclaration:  # pragma: no cover - no declarations to collide
        pass


def _write_one(
    store: KnowledgeStore, layer: str, root: str, source: _Source,
    on_duplicate: str, build: _Build,
) -> bool:
    try:
        declarations, calls = _load(source, layer, on_duplicate, build)
    except _SourceFailed:
        _record_empty(store, layer, root, source)
        return False
    try:
        store.put_source(
            layer, source.key, declarations, calls=calls,
            root=root, size=source.size, mtime=source.mtime,
        )
    except DuplicateDeclaration as exc:
        # The store's key notion and ours disagreed. That is a defect, not a
        # mod's quirk -- and it still must cost only this source.
        if on_duplicate == FAIL:
            raise LayerBuildError(str(exc)) from exc
        build.problems.append(SkippedSource(path=source.key, reason=str(exc)))
        return False
    return True


def _rebuild(
    store: KnowledgeStore, layer: str, root: str, sources: Sequence[_Source],
    on_duplicate: str, build: _Build,
) -> int:
    """A whole layer at once, inside one transaction.

    Atomic on purpose: a rebuild that dies halfway -- a killed job, an
    unreadable archive -- must leave the last good index rather than a partial
    one that looks complete.
    """
    written = 0

    def stream() -> Iterator[tuple[str, list[Declaration], list[Call]]]:
        nonlocal written
        for source in sources:
            try:
                declarations, calls = _load(source, layer, on_duplicate, build)
            except _SourceFailed:
                # Recorded as empty rather than left out: see _record_empty.
                yield source.key, (), ()
                continue
            written += 1
            yield source.key, declarations, calls

    try:
        store.replace_layer(layer, stream(), root=root)
    except DuplicateDeclaration as exc:
        if on_duplicate == FAIL:
            raise LayerBuildError(str(exc)) from exc
        # Nothing was written; the transaction rolled back. Fall back to
        # writing source by source so the collision costs its own source and
        # not the entire layer.
        build.note(
            "a record key collided where deduplication saw none; rebuilt source "
            "by source so the collision cost only its own source"
        )
        store.drop_layer(layer)
        written = 0
        for source in sources:
            if _write_one(store, layer, root, source, on_duplicate, build):
                written += 1
    return written


def _sync(
    store: KnowledgeStore, layer: str, root: str, sources: Sequence[_Source],
    *, full: bool, on_duplicate: str, build: _Build, only: frozenset[str] | None = None,
) -> LayerReport:
    """`only` narrows the build to a named set of sources.

    Without it the walk IS the layer's file set, so anything recorded and not
    walked has been deleted. With it, the caller looked at a handful of paths
    and nothing else -- so the sweep for vanished sources is confined to those
    same paths, and the rest of the layer is left exactly where it was. Getting
    that wrong would turn "reindex the file I just saved" into "throw the layer
    away and keep one file", which still answers, and answers confidently.
    """
    started = time.perf_counter()
    try:
        known = store.layer(layer) is not None
        rebuild = full or not known
        current = {s.key for s in sources}
        removed = 0
        if rebuild:
            indexed = _rebuild(store, layer, root, sources, on_duplicate, build)
            unchanged = 0
        else:
            # Reading the whole source table is the price of knowing what
            # disappeared. A caller that named its sources already answered
            # that question, so it pays for its own rows and no others.
            recorded = {s.path: s for s in store.sources(layer, only)}
            todo = [
                s for s in sources
                if s.key not in recorded
                or recorded[s.key].size != s.size
                or recorded[s.key].mtime != s.mtime
            ]
            indexed = 0
            for source in todo:
                if _write_one(store, layer, root, source, on_duplicate, build):
                    indexed += 1
            for path in [p for p in recorded if p not in current]:
                removed += store.drop_source(layer, path)
            unchanged = len(sources) - len(todo)
        if build.binary_unread:
            build.note(
                f"{build.binary_unread} binary config(s) (config.bin) left unindexed: "
                "CfgConvert was not found -- set machine.tools to the DayZ Tools "
                "directory to index them"
            )
        if build.binary_failed:
            build.note(
                f"{build.binary_failed} binary config(s) (config.bin) CfgConvert "
                f"refused to read; first: {build.first_failure}"
            )
        info = store.layer(layer)
        return LayerReport(
            layer=layer,
            root=root,
            sources=info.sources if info else 0,
            declarations=info.declarations if info else 0,
            indexed=indexed,
            removed=removed,
            unchanged=unchanged,
            lost=build.lost,
            seconds=time.perf_counter() - started,
            incremental=not rebuild,
            skipped=tuple(build.problems),
            notes=tuple(build.notes),
        )
    finally:
        build.cleanup()


# ---------------------------------------------------------------- the sources


def _label(path: str | Path, root: Path) -> str:
    try:
        return str(Path(path).relative_to(root))
    except ValueError:  # pragma: no cover - a source from outside its own root
        return str(path)


def _parse_text(
    text: str, name: str, label: str
) -> tuple[list[Declaration], list[Call]]:
    """Enforce Script or config, decided by the file's own name.

    A config has no call sites -- it is data, not code -- so the second half
    of the answer is empty there rather than absent.
    """
    if name.lower().endswith(CONFIG_SUFFIXES):
        return parse_config(text, file=label), []
    return parse_all(text, file=label)


def _file_source(found: FileStat, root: Path) -> _Source:
    """One file as one unit of change.

    The label is worked out inside `load`, not before it: a layer with 2810
    sources computes 2810 relative paths on every staleness check otherwise,
    and on this machine that alone was 40% of the check. Only the handful of
    files actually re-read need one.
    """
    path = Path(found.path)

    def load() -> tuple[list[Declaration], list[Call]]:
        return _parse_text(
            path.read_text(encoding="utf-8", errors="replace"),
            path.name,
            _label(found.path, root),
        )

    return _Source(key=found.path, size=found.size, mtime=found.mtime, load=load)


def _wanted_entry(name: str) -> bool:
    """Entries worth pulling out of an archive: script sources and configs."""
    low = name.lower().replace("\\", "/")
    base = low.rsplit("/", 1)[-1]
    if base in ("config.cpp", BINARY_CONFIG):
        return True
    return low.endswith(SCRIPT_SUFFIXES) or low.endswith((".hpp", ".h"))


def _pbo_source(
    found: FileStat, prefix: str, build: _Build, limits: PboLimits
) -> _Source:
    """One archive as one unit of change: a workshop update replaces the whole
    file, so re-reading the whole file is exactly right."""
    path = Path(found.path)

    def load() -> tuple[list[Declaration], list[Call]]:
        out: list[Declaration] = []
        calls: list[Call] = []
        stem = path.stem
        # An archive may hold the same entry name several times: measured on
        # this machine, 112 of 523 archives do, with 127 495 repeated entries
        # between them. Two entries under one label would claim the same
        # record row and one of them would be dropped -- so the repeats are
        # numbered instead, and nothing is lost to somebody else's packing.
        seen: dict[str, int] = {}
        for entry, blob in scan_pbo(path, _wanted_entry, limits=limits):
            times = seen.get(entry.name.lower(), 0) + 1
            seen[entry.name.lower()] = times
            name = entry.name if times == 1 else f"{entry.name}#{times}"
            label = f"{prefix}/{stem}/{name}" if prefix else f"{stem}/{name}"
            base = entry.name.lower().replace("\\", "/").rsplit("/", 1)[-1]
            if base == BINARY_CONFIG:
                text = _convert_binary(blob, build)
                if text is None:
                    continue
                out += parse_config(text, file=label)
                continue
            found_decls, found_calls = _parse_text(
                blob.decode("utf-8", "replace"), base, label
            )
            out += found_decls
            calls += found_calls
        return out, calls

    return _Source(key=found.path, size=found.size, mtime=found.mtime, load=load)


def _convert_binary(blob: bytes, build: _Build) -> str | None:
    """A binarised config as text, or None with the reason counted.

    CfgConvert is the only thing that reads a `config.bin`, and a `config.bin`
    is what most published mods ship. Without it the answer is "not indexed,
    and here is why" -- never an empty result that reads like "no such class".
    """
    convert = build.converter()
    if convert is None:
        build.binary_unread += 1
        return None
    scratch = build.scratch()
    source = scratch / "config.bin"
    dest = scratch / "config.cpp"
    try:
        source.write_bytes(blob)
        if dest.exists():
            dest.unlink()
        error = convert(source, dest)
        if error or not dest.exists():
            build.binary_failed += 1
            build.first_failure = build.first_failure or _one_line(error) or "no output"
            return None
        return dest.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        build.binary_failed += 1
        build.first_failure = build.first_failure or _one_line(str(exc))
        return None


def _one_line(text: str) -> str:
    return " ".join(str(text).split())[:200]


def _cfgconvert(tools: str | Path) -> Callable[[Path, Path], str] | None:
    exe = Path(tools) / CFGCONVERT_REL
    if not exe.is_file():
        return None

    def convert(source: Path, dest: Path) -> str:
        code, tail = run_blocking(
            config_text_cmd(exe, source, dest),
            cwd=source.parent,
            log_path=source.parent / "cfgconvert.log",
            timeout=CONVERT_TIMEOUT,
        )
        return "" if code == 0 else tail[-200:]

    return convert


def _resolve_convert(
    convert: Callable[[Path, Path], str] | None | str, tools: str | Path | None | str
) -> Callable[[Path, Path], str] | None:
    if convert != AUTO:
        return convert  # type: ignore[return-value]
    root = find_tools() if tools == AUTO else tools
    if not root:
        return None
    return _cfgconvert(root)


# ------------------------------------------------------------ the three layers


def _indexable(path: Path, root: Path, suffixes: Sequence[str]) -> str:
    """Why this named path is not a source of this layer, or "" if it is.

    The same three rules the walk applies, spelled once so a path reached by
    name can never enter the index by a route the walk would have refused --
    a built mod under `@...` holds the packed copy of the very files being
    indexed, and indexing both makes every declaration answer twice.
    """
    try:
        relative = path.relative_to(root)
    except ValueError:
        return "outside the project root"
    if any(_skip_dir(part) for part in relative.parts[:-1]):
        return (
            "inside a directory the project layer never walks (a built mod folder, "
            "a dot-directory)"
        )
    if not path.name.lower().endswith(tuple(s.lower() for s in suffixes)):
        return f"not one of the indexed source kinds ({', '.join(suffixes)})"
    return ""


def _named_sources(
    only: Iterable[str | Path], root: Path, suffixes: Sequence[str], build: _Build
) -> tuple[frozenset[str], list[_Source]]:
    """The sources the caller named, and the keys the build is allowed to touch.

    A path that cannot be indexed is skipped and NAMED -- an agent that thinks
    it reindexed a file it did not is one silent step from a confident wrong
    answer, which is the whole failure this phase is built against.

    A named path that no longer exists stays in the key set on purpose: `only`
    means "these changed", and a delete is a change. It ends up in the key set
    without a source, which is exactly how `_sync` comes to drop it.
    """
    keys: set[str] = set()
    sources: list[_Source] = []
    for raw in only:
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = root / candidate
        try:
            path = Path(os.path.normpath(str(candidate))).resolve()
        except OSError:  # pragma: no cover - a path the OS will not even parse
            build.note(f"{raw}: not a usable path, left alone")
            continue
        refusal = _indexable(path, root, suffixes)
        if refusal:
            build.note(f"{raw}: {refusal}, left alone")
            continue
        keys.add(str(path))
        try:
            stat = path.stat()
        except OSError:
            # Gone. Named, so it is dropped from the index below.
            continue
        sources.append(_file_source(FileStat(str(path), stat.st_size, stat.st_mtime), root))
    return frozenset(keys), sources


def build_project(
    store: KnowledgeStore,
    root: str | Path,
    *,
    configs: bool = True,
    full: bool = False,
    on_duplicate: str = REPORT,
    only: Iterable[str | Path] | None = None,
) -> LayerReport:
    """The layer that goes stale between one agent turn and the next.

    Read straight off the disk -- there is nothing to unpack, and unpacking a
    build output would index yesterday's copy of today's file. One edited file
    costs one re-read; that ratio is the reason this phase is incremental at
    all.

    `only` is the route for a caller that already knows what moved. The walk
    exists to discover which of the layer's files changed; an agent that just
    saved one does not need discovering, and skipping the walk is the
    difference between a rebuild that costs the tree and one that costs the
    file. What it gives up is stated rather than hidden: nothing outside the
    named paths is looked at, so a file created or deleted elsewhere goes
    unnoticed until the next ordinary build.
    """
    root = Path(root).resolve()
    suffixes = SCRIPT_SUFFIXES + (CONFIG_SUFFIXES if configs else ())
    build = _Build()
    if only is None:
        limit: frozenset[str] | None = None
        sources = [_file_source(f, root) for f in scan_tree(root, suffixes=suffixes)]
    else:
        if full:
            raise LayerBuildError(
                "only= re-reads the sources it names and full= re-reads every source "
                "there is; asking for both says nothing about which was meant"
            )
        if store.layer(PROJECT) is None:
            raise LayerBuildError(
                "the project layer has never been built, so there is no layer to update "
                "one file of -- an index holding only the named files would look like a "
                "whole project and answer like one"
            )
        limit, sources = _named_sources(only, root, suffixes, build)
    return _sync(
        store, PROJECT, str(root), sources,
        full=full, on_duplicate=on_duplicate, build=build, only=limit,
    )


def build_deps(
    store: KnowledgeStore,
    mods: Iterable[str | Path],
    *,
    tools: str | Path | None = AUTO,
    convert: Callable[[Path, Path], str] | None | str = AUTO,
    limits: PboLimits = DEFAULT_LIMITS,
    full: bool = False,
    on_duplicate: str = REPORT,
) -> LayerReport:
    """What the project builds against, read out of the archives themselves.

    Every archive of every declared mod, without unpacking one of them. An
    archive that cannot be read is named in the report and costs only itself:
    on this machine three of 523 carry entry tables past any ceiling worth
    walking, and the other 520 are the answer.
    """
    folders = [Path(m) for m in mods]
    build = _Build(convert=convert, tools=tools)
    sources: list[_Source] = []
    for folder in folders:
        for found in scan_tree(folder, suffixes=(".pbo",)):
            sources.append(_pbo_source(found, folder.name, build, limits))
    return _sync(
        store, DEPS, _common_root(folders), sources,
        full=full, on_duplicate=on_duplicate, build=build,
    )


def build_core(
    store: KnowledgeStore,
    *,
    scripts: str | Path | None = None,
    game: str | Path | None = None,
    tools: str | Path | None = AUTO,
    workdir: str | Path | None = None,
    configs: bool = True,
    convert: Callable[[Path, Path], str] | None | str = AUTO,
    limits: PboLimits = DEFAULT_LIMITS,
    full: bool = False,
    on_duplicate: str = REPORT,
) -> LayerReport:
    """The game itself: `scripts.pbo` for the API, `Addons` for the classes.

    `scripts` is an already-unpacked corpus, which is what a machine that has
    ever opened the sources already has. Without one the game's own archive is
    unpacked into `workdir` -- with BankRev when DayZ Tools is installed, and
    with this server's own reader when it is not, because a server that only
    works next to a toolchain is not the all-in-one this one is meant to be.

    The configs are the half that answers "is there a class with this name in
    the game" -- `Addons/*.pbo` hold `config.bin`, and CfgConvert turns those
    into something readable.
    """
    build = _Build(convert=convert, tools=tools)
    if scripts:
        corpus = Path(scripts)
    elif game:
        corpus = _unpack_scripts(Path(game), tools, workdir, store, build, limits)
    else:
        raise LayerBuildError(
            "core needs either an unpacked corpus (scripts=) or the game (game=)"
        )
    sources = [
        _file_source(f, corpus)
        for f in scan_tree(corpus, suffixes=SCRIPT_SUFFIXES + CONFIG_SUFFIXES)
    ]
    if configs and game:
        addons = _addons_dir(Path(game))
        if addons is not None:
            for found in scan_tree(addons, suffixes=(".pbo",)):
                sources.append(_pbo_source(found, addons.name, build, limits))
    return _sync(
        store, CORE, str(corpus), sources,
        full=full, on_duplicate=on_duplicate, build=build,
    )


def dependency_dirs(profile, game: str | Path = "") -> list[Path]:
    """The mod folders a profile declares, minus the project's own.

    `mods.required` names folders inside the game's `!Workshop`; `mods.extra`
    carries full paths. The project's own built mods are left out: their
    sources are the project layer, and indexing them twice would have every
    declaration answer from two layers with the same file.
    """
    own = {name.lower() for name in getattr(profile, "own_mod_dirs", [])}
    mods = getattr(profile, "mods", None)
    out: list[Path] = []
    for name in getattr(mods, "required", []) or []:
        if name.lower() not in own:
            out.append(Path(game) / "!Workshop" / name)
    for path in getattr(mods, "extra", []) or []:
        if Path(path).name.lower() not in own:
            out.append(Path(path))
    return out


# --------------------------------------------------------------- the game tree


def _common_root(folders: Sequence[Path]) -> str:
    parents = {str(f.parent) for f in folders}
    return parents.pop() if len(parents) == 1 else ""


def _child(root: Path, name: str) -> Path | None:
    """A child directory by name, case-insensitively -- the game spells it
    `Addons` and a mod spells it `addons`."""
    try:
        for entry in os.scandir(root):
            if entry.is_dir() and entry.name.lower() == name:
                return Path(entry.path)
    except OSError:
        return None
    return None


def _addons_dir(game: Path) -> Path | None:
    return _child(game, "addons")


def _scripts_pbo(game: Path) -> Path:
    dta = _child(game, "dta")
    if dta is not None:
        for entry in sorted(os.scandir(dta), key=lambda e: e.name):
            if entry.is_file() and entry.name.lower() == "scripts.pbo":
                return Path(entry.path)
    raise LayerBuildError(
        f"no dta/scripts.pbo under {game}: point `game` at the DayZ installation, "
        "or pass an already-unpacked corpus as `scripts`"
    )


def _unpack_scripts(
    game: Path, tools: str | Path | None | str, workdir: str | Path | None,
    store: KnowledgeStore, build: _Build, limits: PboLimits,
) -> Path:
    """The vanilla corpus on disk, unpacked once and reused until the game
    changes.

    The stamp is what makes "until the game changes" a measurement rather than
    a hope: an unpack that ran on every status check would cost two seconds
    and a few thousand file writes for an answer that did not move.
    """
    pbo = _scripts_pbo(game)
    target = Path(workdir) if workdir else store.path.parent / "corpus"
    target.mkdir(parents=True, exist_ok=True)
    stat = pbo.stat()
    token = f"{stat.st_size} {stat.st_mtime_ns}"
    stamp = target / f"{pbo.stem}.stamp"
    corpus = target / pbo.stem
    if corpus.is_dir() and stamp.is_file():
        try:
            if stamp.read_text(encoding="utf-8").strip() == token:
                build.note(f"reused the corpus already unpacked from {pbo.name}")
                return corpus
        except OSError:  # pragma: no cover - unreadable stamp, just re-unpack
            pass

    root = find_tools() if tools == AUTO else tools
    bankrev = Path(root) / BANKREV_REL if root else None
    if bankrev is not None and bankrev.is_file():
        code, tail = run_blocking(
            bankrev_cmd(bankrev, pbo, target),
            cwd=target,
            log_path=target / "bankrev.log",
            timeout=UNPACK_TIMEOUT,
        )
        if code != 0:
            raise LayerBuildError(f"BankRev could not unpack {pbo.name}: {tail[-300:]}")
        corpus = bankrev_output(pbo, target)
        build.note(f"unpacked {pbo.name} with BankRev into {corpus}")
    else:
        count = _extract(pbo, corpus, limits)
        build.note(
            f"unpacked {count} entries of {pbo.name} with the built-in reader "
            "(BankRev was not found; install DayZ Tools for the supported path)"
        )
    stamp.write_text(token, encoding="utf-8")
    return corpus


def _safe_relative(name: str) -> Path | None:
    """An entry name as a path under the output directory, or None.

    An archive is somebody else's data: a name with `..` in it, an absolute
    path or a drive letter is a write outside the directory we chose, and no
    unpacking is worth that.
    """
    parts = [p for p in name.replace("\\", "/").split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts) or ":" in parts[0]:
        return None
    return Path(*parts)


def _extract(pbo: Path, out_dir: Path, limits: PboLimits) -> int:
    """Write the script sources of one archive to disk.

    The fallback when DayZ Tools is absent. Only what the index reads is
    written: this is not a general unpacker, and it must not pretend to be.
    """
    written = 0
    for entry, blob in scan_pbo(pbo, _wanted_entry, limits=limits):
        relative = _safe_relative(entry.name)
        if relative is None or relative.name.lower() == BINARY_CONFIG:
            continue
        target = out_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(blob)
        written += 1
    return written
