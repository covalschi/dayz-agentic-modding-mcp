"""Asking the index: what exists, where it is declared, and how old the answer is.

Five tools over the three layers. Everything about their shape follows from
four properties, and each one is a way an index can lie to the agent trusting
it.

**An answer never hides the age of the layer it came from.** Every search
carries, for each layer it used, when that layer was last indexed -- and the
project layer's freshness is *measured*, on every answer, whether or not it
contributed. That last part is the one that matters: an agent that adds a class
and immediately asks about it gets "not found" from a layer built a minute ago,
and a silent "not found" is a confident statement about code that exists. The
same discipline as "could not measure" instead of "frozen" elsewhere in this
server: the unknown is named.

**An empty index says what to build.** "Nothing found" is only an answer when
the layers that could have held it were built. Otherwise the tool refuses, and
the refusal names the layer and the call that builds it. A refusal still
carries what was measured, so nothing has to be asked twice.

**Every search has a ceiling.** The predecessor project's two search tools hang
forever, because the client behind them was created without a timeout (their
open bug #51). Here the store's own `time_limit` interrupts the query and this
layer turns that into an answer.

**A long build returns a job id.** Measured on real data: the game's scripts
3.9 s, the game with its configs 69 s, the dependency archives 150 s. Even the
project layer, at a tenth of a second, goes through the same door -- one shape
for every build means a caller never has to know which of them blocks.

One design decision a caller cannot guess, so it is stated in every relevant
description: **config classes are indexed under `kind="config"`, not
`kind="class"`.** Counted in the game's own index: 88 102 config classes
against 43 595 script declarations of every kind put together, so mixed into
one kind they would bury every script answer. Separated, "does the game have an
item class with this name" becomes a question you can ask exactly.

What this index does NOT answer is the other half of the job, and it is not a
gap to be closed later: whether `modded class X extends X` silently fails to
apply, whether `_co` costs the alpha channel. None of that is derivable from
the sources. The index answers what exists; the modding skill answers what is
right.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from ..errors import Result, fail, ok
from ..jobs import QUEUED, RUNNING
from ..knowledge import scope as modscope
from ..knowledge.calls import CALL, NEW
from ..knowledge.layers import (
    CONFIG_SUFFIXES,
    SCRIPT_SUFFIXES,
    FileStat,
    LayerBuildError,
    build_core,
    build_deps,
    build_project,
    dependency_dirs,
    scan_tree,
    staleness_of,
)
from ..knowledge.parse import CLASS, CONFIG, CONSTANT, ENUM, METHOD, strip_source
from ..knowledge.pbo import PboError, scan_pbo
from ..knowledge.store import (
    CORE,
    DEPS,
    LAYERS,
    PROJECT,
    KnowledgeStore,
    LayerInfo,
    Record,
    SearchTimeout,
    Staleness,
    mod_folder,
)
from . import session
from .project import require_project

#: `layer="all"` builds every layer that applies to this project.
ALL = "all"

#: The kinds a caller may ask for. `config` is a class out of a `config.cpp`
#: (or a binarised `config.bin`), which is a different namespace from a script
#: class -- see the module docstring.
KINDS: tuple[str, ...] = (CLASS, METHOD, CONSTANT, ENUM, CONFIG)

#: The job kind. Distinct from "build" (mod packing) and "bridge-build": they
#: contend for different things, so one must not lock the others out.
BUILD_KIND = "knowledge-build"

#: How long any one search may run before the store interrupts it. Generous
#: against the measurements -- an exact lookup is 0.07 ms and a prefix browse
#: 2.5 ms on the vanilla corpus -- because this is a ceiling, not a budget: it
#: exists so that nothing ever hangs, not to make a slow query fail sooner.
SEARCH_SECONDS = 5.0

#: What a search returns unasked, and the most it will return however it is
#: asked. The store's own ceiling is 10 000; this one is lower because these
#: results are read by a language model, and ten thousand declarations is not
#: an answer, it is a corpus.
DEFAULT_LIMIT = 50
MAX_LIMIT = 500

#: knowledge_show is the "one declaration, in full" tool, so its defaults are
#: small on purpose -- a caller who wants breadth wants knowledge_find.
SHOW_LIMIT = 5
MEMBER_LIMIT = 400
#: Inheritance is walked, not stored, so it needs a stop: a chain longer than
#: this is a cycle somebody wrote or a mistake in the index, and either way an
#: infinite walk is the worst possible way to report it.
ANCESTOR_LIMIT = 32
BODY_LINES = 80
MAX_BODY_LINES = 400

#: How many paths a status answer names before it stops listing and counts.
_NAMED = 10

#: How much of what the active mod set kept out is fetched so it can be NAMED.
#: A ceiling, like every other search here: the answer needs the mods and the
#: counts, not the corpus. Reaching it is reported (`filtered_truncated`), so a
#: number that stops short says so instead of understating the loss.
EXCLUDED_LIMIT = 200

_SOURCE_SUFFIXES: tuple[str, ...] = SCRIPT_SUFFIXES + CONFIG_SUFFIXES


# --------------------------------------------------------------- layer basics


def _index() -> tuple[KnowledgeStore | None, Result | None]:
    """The open project's index, or a refusal shaped like every other answer.

    Opening it is the one step in these tools that can fail outside their own
    logic: the file is created on first use, and a file that cannot be opened
    at all raises out of SQLite rather than returning anything. `server._wrap`
    does not catch, so an escaping exception reaches the caller as a raised
    tool call instead of `{ok, data, error, hint}` -- the one shape this server
    never breaks. Broad on purpose: a narrow catch that misses one exception
    type leaves exactly the hole this exists to close.

    Call it after `require_project`, which is what makes `None` here mean "the
    index could not be opened" rather than "no project".
    """
    try:
        store = session.knowledge()
    except Exception as exc:  # noqa: BLE001 - an escaping raise answers nobody
        return None, fail(
            f"the knowledge index could not be opened: {type(exc).__name__}: {exc}",
            hint="the index lives in the project's .dayz-mcp/ and is derived data -- "
                 "check that the directory is writable, or delete knowledge.db and "
                 "rebuild with knowledge_build()",
        )
    if store is None:  # pragma: no cover - require_project already refused
        return None, fail(
            "no project is open, so there is no knowledge index",
            hint="open one with project_open(path)",
        )
    return store, None


def _inapplicable(profile, game: str | None) -> dict[str, str]:
    """Why each layer does not apply to this project, empty where it does.

    A reason string rather than a flag, and named for the FALSE case, so that
    `not _inapplicable(...)[layer]` reads as what it means. The reason is the
    useful half anyway: a caller told "no dependency layer" needs to know it is
    because nothing is declared, not because something failed.

    Applicability is structural and has nothing to do with whether the layer
    can be built on this machine right now: the game is always the substrate a
    DayZ mod is written against, so `core` always applies even where the game
    is not installed -- an answer taken without it is incomplete either way.
    A project that declares no dependency mods genuinely has no `deps` layer,
    and reporting one as "never built" forever would turn a permanent, correct
    state into a permanent complaint.
    """
    reasons = {PROJECT: "", CORE: "", DEPS: ""}
    if not _dependency_folders(profile, game):
        reasons[DEPS] = (
            "this project declares no dependency mods (mods.required / mods.extra "
            "in its profile), so there is nothing for a dependency layer to hold"
        )
    return reasons


def _unbuildable(profile, game: str | None) -> dict[str, str]:
    """Why each layer cannot be built here and now, empty where it can.

    Same convention as `_inapplicable`: the string is the reason, and empty
    means there is none."""
    blocked = {PROJECT: "", CORE: "", DEPS: ""}
    inapplicable = _inapplicable(profile, game)
    if not game:
        blocked[CORE] = (
            "the DayZ installation was not found -- set machine.game in "
            "dayz-mcp.local.toml, or install the game on this machine"
        )
        if not inapplicable[DEPS]:
            # mods.required names folders inside the game's !Workshop, so
            # without the game those paths cannot even be spelled.
            required = list(getattr(getattr(profile, "mods", None), "required", []) or [])
            if required:
                blocked[DEPS] = (
                    "mods.required names folders inside the game's !Workshop and the "
                    "DayZ installation was not found -- set machine.game in "
                    "dayz-mcp.local.toml"
                )
    for layer, why in inapplicable.items():
        if why and not blocked[layer]:
            blocked[layer] = why
    return blocked


def _dependency_folders(profile, game: str | None) -> list[Path]:
    return dependency_dirs(profile, game or "")


def _age_text(seconds: float | None) -> str:
    if seconds is None:
        return "never built"
    whole = int(max(0.0, seconds))
    if whole < 90:
        return f"{whole}s"
    if whole < 5400:
        return f"{whole // 60}m"
    if whole < 172_800:
        return f"{whole // 3600}h"
    return f"{whole // 86_400}d"


def _how_to_build(layer: str, why_not: str) -> str:
    call = f"knowledge_build(layer='{layer}')"
    return f"{call} -- {why_not}" if why_not else call


# ------------------------------------------------------------ measuring a layer


def _current_files(
    store: KnowledgeStore, layer: str, info: LayerInfo, profile, game: str | None
):
    """The layer's source set as it stands now, or None when it cannot be
    walked the same way it was built.

    None is an answer, not a failure: measuring a layer against a walk of a
    different shape reports sources as gone that are sitting exactly where the
    build left them, and a false "the game lost 2810 files" is worse than an
    honest "not measured here".

    The core layer's walk is reconstructed from the index itself rather than
    from the game path -- the unpacked corpus is `info.root`, and the archives
    it also read are found by looking at which directories its recorded `.pbo`
    sources came from. That way the measurement mirrors the build even if the
    game has since moved, and a new archive appearing in the game's Addons is
    still seen.
    """
    if layer == PROJECT:
        return scan_tree(profile.root, suffixes=_SOURCE_SUFFIXES)

    if layer == DEPS:
        required = list(getattr(getattr(profile, "mods", None), "required", []) or [])
        if required and not game:
            return None
        folders = _dependency_folders(profile, game)
        if not folders:
            return None
        found: list[FileStat] = []
        for folder in folders:
            found += scan_tree(folder, suffixes=(".pbo",))
        return found

    root = Path(info.root) if info.root else None
    if root is None or not root.is_dir():
        return None
    found = scan_tree(root, suffixes=_SOURCE_SUFFIXES)
    archives = {
        str(Path(state.path).parent)
        for state in store.sources(layer)
        if state.path.lower().endswith(".pbo")
    }
    for directory in sorted(archives):
        if Path(directory).is_dir():
            found += scan_tree(directory, suffixes=(".pbo",))
    return found


def _measure(
    store: KnowledgeStore, layer: str, info: LayerInfo, profile, game: str | None
) -> Staleness:
    """What this layer would have to re-read. Always a measurement.

    When the walk cannot be reconstructed, this falls back to stat'ing what was
    recorded -- which still catches an edited or deleted source and reports
    `scanned_for_new=False`, the store's way of saying "did not look" rather
    than "there are none".
    """
    walked = _current_files(store, layer, info, profile, game)
    if walked is None:
        return store.staleness(layer)
    return staleness_of(store, layer, walked)


def _layer_view(
    store: KnowledgeStore, layer: str, profile, game: str | None, *, measured: bool = True
) -> dict:
    """One layer as an answer reports it: what it holds, how old it is, and
    whether it still matches what is on disk."""
    info = store.layer(layer)
    inapplicable = _inapplicable(profile, game)[layer]
    unbuildable = _unbuildable(profile, game)[layer]
    view: dict = {
        "layer": layer,
        "built": info is not None,
        "applies": not inapplicable,
        "why_not": inapplicable,
        "available": not unbuildable,
        "unavailable_reason": unbuildable,
        "root": info.root if info else "",
        "sources": info.sources if info else 0,
        "empty_sources": 0,
        "declarations": info.declarations if info else 0,
        "built_at": info.built if info else None,
        "updated_at": info.updated if info else None,
        "age_seconds": None,
        "age": "never built",
        "stale": None,
        "measured": False,
        "staleness": "never built",
        "changed": [],
        "added": [],
        "missing": [],
    }
    if info is None:
        return view
    age = info.age()
    view["age_seconds"] = round(age, 1)
    view["age"] = _age_text(age)
    view["empty_sources"] = store.empty_sources(layer)
    if not measured:
        view["staleness"] = "not measured here -- ask knowledge_status"
        return view
    staleness = _measure(store, layer, info, profile, game)
    view["stale"] = bool(staleness.stale)
    view["measured"] = True
    view["scanned_for_new"] = staleness.scanned_for_new
    view["staleness"] = staleness.describe()
    view["changed"] = _cap(staleness.changed)
    view["added"] = _cap(staleness.added)
    view["missing"] = _cap(staleness.missing)
    return view


def _cap(paths) -> list[str]:
    listed = list(paths[:_NAMED])
    if len(paths) > _NAMED:
        listed.append(f"... and {len(paths) - _NAMED} more")
    return listed


def _freshness(
    store: KnowledgeStore, used: set[str], profile, game: str | None
) -> list[dict]:
    """The layer views a search answer carries.

    Every layer that contributed a record, plus the project layer whether it
    did or not. That exception is the point: the project layer is the one that
    goes stale between one agent turn and the next, and the dangerous answer is
    the one it did NOT contribute to -- a class the agent added a minute ago,
    reported as not existing. Its staleness is measured on every answer; the
    other two are reported with their age and left to knowledge_status, because
    measuring them costs a walk of the game or of the modpack and their rhythm
    is game updates, not keystrokes.
    """
    views = []
    for layer in LAYERS:
        if layer == PROJECT or layer in used:
            views.append(
                _layer_view(store, layer, profile, game, measured=layer == PROJECT)
            )
    return views


def _incomplete(store: KnowledgeStore, profile, game: str | None) -> list[dict]:
    """Layers that apply to this project and have never been built.

    What turns "nothing found" from an answer into a refusal.
    """
    inapplicable = _inapplicable(profile, game)
    unbuildable = _unbuildable(profile, game)
    return [
        {"layer": layer, "how": _how_to_build(layer, unbuildable[layer])}
        for layer in LAYERS
        if not inapplicable[layer] and store.layer(layer) is None
    ]


# ------------------------------------------------------- the active mod set


def _active(store: KnowledgeStore) -> modscope.ActiveSet:
    """The mod set in force for this project, or "no set"."""
    return modscope.load(store.path.parent)


def _scoped(active: modscope.ActiveSet) -> list[str] | None:
    """What the store wants: the mods, or None for "do not narrow"."""
    return list(active.mods) if active.active else None


def _excluded_find(store: KnowledgeStore, active: modscope.ActiveSet, name: str, **kw):
    """The same search, run over exactly what the set kept out.

    This is what makes "a filtered-out result is NAMED, never hidden" true. It
    costs one extra query, and only when a set is in force -- measured against
    an exact lookup at 0.07 ms, that is the price of never answering "no such
    thing" about a class that exists.

    A timeout here returns nothing rather than failing: the real answer has
    already been computed, and a slow second opinion must never turn a good
    answer into a refusal.
    """
    if not active.active:
        return []
    try:
        with store.time_limit(SEARCH_SECONDS):
            return store.find(
                name, mods=list(active.mods), outside=True, limit=EXCLUDED_LIMIT, **kw
            )
    except SearchTimeout:
        return []


def _excluded_callers(store: KnowledgeStore, active: modscope.ActiveSet, name: str, **kw):
    if not active.active:
        return []
    try:
        with store.time_limit(SEARCH_SECONDS):
            return store.callers(
                name, mods=list(active.mods), outside=True, limit=EXCLUDED_LIMIT, **kw
            )
    except SearchTimeout:
        return []


def _excluded_overrides(store: KnowledgeStore, active: modscope.ActiveSet, name: str, **kw):
    if not active.active:
        return []
    try:
        with store.time_limit(SEARCH_SECONDS):
            return store.overrides(
                name, mods=list(active.mods), outside=True, limit=EXCLUDED_LIMIT, **kw
            )
    except SearchTimeout:
        return []


def _scope_view(active: modscope.ActiveSet, excluded) -> dict:
    """The active set as an answer carries it: what it is, and what it cost.

    Every answer carries this whether or not it was narrowed, for the same
    reason every answer carries its layer ages: a narrowing nobody can see is a
    narrowing nobody can correct. `filtered_out` names the mods that held the
    matches the set kept out, with counts and one example each -- names, not
    silence, is the whole design decision.
    """
    view = {**active.to_dict(), "age": _age_text(active.age()) if active.active else ""}
    grouped: dict[str, dict] = {}
    for record in excluded:
        folder = mod_folder(record.layer, record.file) or "(no mod folder)"
        entry = grouped.setdefault(
            folder, {"mod": folder, "count": 0, "kinds": {}, "example": None}
        )
        entry["count"] += 1
        entry["kinds"][record.kind] = entry["kinds"].get(record.kind, 0) + 1
        if entry["example"] is None:
            entry["example"] = record.to_dict()
    view["filtered_out"] = sorted(
        grouped.values(), key=lambda e: (-e["count"], e["mod"].lower())
    )
    view["filtered_count"] = len(excluded)
    view["filtered_truncated"] = len(excluded) >= EXCLUDED_LIMIT
    return view


def _scope_names(view: dict) -> str:
    """The mods the set kept out, named with their counts.

    The counts say "at least" when the ceiling was reached, because a bare
    number that stopped short understates the loss -- and understating it is
    the same shape of quiet as hiding it, just smaller.
    """
    named = ", ".join(f"{e['count']} in {e['mod']}" for e in view["filtered_out"][:8])
    if len(view["filtered_out"]) > 8:
        named += f", and {len(view['filtered_out']) - 8} more mod(s)"
    if view.get("filtered_truncated"):
        named += (
            f" (counted up to the ceiling of {EXCLUDED_LIMIT}, so these are lower bounds)"
        )
    return named


def _scope_hint(view: dict) -> str:
    if not view.get("active"):
        return ""
    parts = [
        f"narrowed by the active mod set: {view['count']} mod(s), "
        f"{view['source'] or 'source not recorded'}, declared {view['age']} ago"
    ]
    if view["filtered_out"]:
        parts.append(
            "kept out of this answer: " + _scope_names(view)
            + " -- widen it with knowledge_scope(mods=[...]), or write without that mod. "
              "Nothing here was hidden: knowledge_scope(clear=True) removes the set"
        )
    return " | ".join(parts)


def _narrowed_away(lead: str, view: dict) -> str:
    """The refusal for "not in the set, but it exists".

    A refusal rather than an empty success on purpose. The set is AMBIENT state
    -- possibly declared in an earlier session -- so unlike a `kind=` passed in
    the same call, the caller has no reason to suspect a narrowing. An `ok`
    answer with no results is exactly what "there is no such thing" looks like,
    and that is the lie this whole feature exists to prevent.
    """
    return (
        lead + ", but the mods outside it hold "
        + _scope_names(view)
        + " -- named rather than answered empty, because an empty answer here reads as "
          "'no such thing' and sends you off to write one"
    )


# ------------------------------------------------------------------- the build


def _in_flight():
    store = session.jobs()
    running = [
        j for j in store.all() if j.kind == BUILD_KIND and j.status in (QUEUED, RUNNING)
    ]
    return running[-1] if running else None


def _sidecar(store: KnowledgeStore) -> Path:
    """Where the last build of each layer is remembered.

    Beside the index rather than inside it: what a build skipped and why is a
    record of an event, not a property of the rows, and putting it in the
    schema would cost every existing index a rebuild to add a field nothing
    queries.
    """
    return store.path.parent / "knowledge-builds.json"


def _read_sidecar(store: KnowledgeStore) -> dict:
    try:
        data = json.loads(_sidecar(store).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_sidecar(store: KnowledgeStore, reports: dict) -> None:
    data = _read_sidecar(store)
    for layer, report in reports.items():
        info = store.layer(layer)
        entry = report.to_dict()
        # Stamped with the layer generation it describes. An index deleted and
        # rebuilt by anything else gets a new `built`, and this record stops
        # being shown rather than describing a build that no longer happened.
        entry["built"] = info.built if info else 0.0
        entry["at"] = time.time()
        # Only what belongs to THIS generation is carried: a full rebuild
        # stamps a new `built`, and the problems of the layer it replaced are
        # not this layer's problems.
        previous = data.get(layer)
        if not isinstance(previous, dict) or abs(
            float(previous.get("built", -1.0)) - entry["built"]
        ) > 1e-6:
            previous = None
        entry["outstanding"] = _outstanding(store, layer, report, previous)
        data[layer] = entry
    try:
        _sidecar(store).write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        # A record of the last build is a convenience; failing the build over
        # it would trade the whole index for a footnote.
        pass


def _last_build(
    store: KnowledgeStore, layer: str, info: LayerInfo | None
) -> dict | None:
    if info is None:
        return None
    entry = _read_sidecar(store).get(layer)
    if not isinstance(entry, dict):
        return None
    if abs(float(entry.get("built", -1.0)) - info.built) > 1e-6:
        return None
    return entry


def _outstanding(store: KnowledgeStore, layer: str, report, previous: dict | None) -> list[dict]:
    """Sources this layer still holds nothing from, across builds -- not just
    the last one.

    `skipped` is a record of one build, and after an incremental build that
    re-read nothing it is correctly empty. Measured on this machine: a full
    dependency build named three unreadable archives, and the very next idle
    rebuild (0 sources re-read) replaced that list with an empty one -- while
    `empty_sources` still counted six. The reader was then told the layer has
    six sources that gave nothing and that nothing was skipped, which is two
    true statements adding up to a false story.

    So the explanation is accumulated instead, and it cleans itself: an entry
    survives only while its path is still a source of this layer AND still
    holds no declarations. A source that has since been read successfully, or
    that has left the layer, drops out on the next build without anyone
    deciding it should.

    Only entries for sources that gave nothing at all are carried. A source
    that WAS indexed but lost some declarations to a key collision (`lost > 0`)
    stays with the build that saw it: it has declarations, so this list's
    self-cleaning rule cannot tell whether a later build fixed it.
    """
    entries: dict[str, dict] = {}
    for old in (previous or {}).get("outstanding") or []:
        if isinstance(old, dict) and old.get("path"):
            entries[str(old["path"])] = {**old, "carried": True}
    for problem in report.skipped:
        if not problem.lost:
            entries[problem.path] = {**problem.to_dict(), "carried": False}
    if not entries:
        return []
    still = {state.path: state.declarations for state in store.sources(layer, list(entries))}
    return [
        entry for path, entry in sorted(entries.items())
        if path in still and not still[path]
    ]


def _build_one(store: KnowledgeStore, layer: str, profile, game: str | None,
               tools_root: str | None, full: bool, only: list[str] | None):
    if layer == PROJECT:
        return build_project(store, profile.root, full=full, only=only)
    # `tools_root` is passed rather than left to AUTO discovery: this session
    # has already looked for DayZ Tools, and None here means "looked, not
    # found" -- which spares every binarised config a second registry sweep
    # that would reach the same conclusion.
    if layer == DEPS:
        return build_deps(
            store, _dependency_folders(profile, game), tools=tools_root, full=full
        )
    return build_core(store, game=game, tools=tools_root, full=full)


def knowledge_build(layer: str = ALL, full: bool = False, only: list[str] | None = None) -> Result:
    """Build or refresh a knowledge layer. Returns a `job_id` immediately.

    Layers, and how often each one needs this:

      project  the mod's own sources, read where they lie. Goes stale on every
               edit -- rebuild it whenever you want to ask about code you just
               wrote. Measured: 0.11 s for 41 files.
      deps     the archives of the mods this project declares as dependencies,
               read without unpacking them. Stale when a dependency is updated
               or the declared set changes. Measured: 150 s for 36 mods.
      core     the game itself: scripts.pbo for the API, Addons/*.pbo for the
               item classes. Stale when the game updates. Measured: 3.9 s for
               the scripts alone, 69 s with the configs.
      all      (the default) every layer that applies to this project. Layers
               that cannot be built here -- no game installed, no dependencies
               declared -- are named with the reason instead of failing the
               call.

    Nothing here blocks: wait with `job_wait(job_id, timeout=...)` and read the
    per-layer numbers in the job's summary and its `knowledge-build.json`
    artifact.

    `only=[path, ...]` is the fast route for the project layer when you already
    know what changed. It re-reads exactly those files and skips the directory
    walk entirely -- and it therefore does NOT notice a file created or deleted
    anywhere else. A named path that no longer exists is dropped from the
    index, so a delete you name is handled; one you do not name is not. Without
    `only`, every build is incremental anyway: unchanged sources are skipped by
    size and modification time, and `full=True` forces the whole layer to be
    re-read.
    """
    guard = require_project()
    if guard:
        return guard

    profile = session.profile()
    game = session.game()
    tools_root = session.tools_root()
    # Resolved here, in the calling thread, so the worker below can never open
    # a second connection to the same index file -- two of those race over the
    # exclusive lock that switching to WAL needs, and the loser's recovery path
    # deletes the database. The window is bounded: WAL lives in the file
    # header, so an index that already exists is opened by both without a
    # fight, and only FIRST CREATION can destroy anything.
    index, failure = _index()
    if failure:
        return failure

    wanted = (layer or "").strip().lower()
    if wanted not in (ALL, PROJECT, DEPS, CORE):
        return fail(
            f"unknown layer {layer!r}",
            hint=f"layer is one of '{PROJECT}', '{DEPS}', '{CORE}' or '{ALL}'",
        )

    if only is not None:
        if wanted != PROJECT:
            return fail(
                f"only= names files, and the {wanted!r} layer is not built from files",
                hint=f"only= applies to layer='{PROJECT}', whose unit of change is one "
                     f"file. A dependency layer's unit is a whole archive and the core "
                     f"layer's is the game, so both re-read what changed on their own",
            )
        if full:
            return fail(
                "only= and full= ask for opposite things",
                hint="pass only= to re-read the files you name, or full=True to re-read "
                     "the whole layer -- not both",
            )
        if index.layer(PROJECT) is None:
            return fail(
                f"the {PROJECT!r} layer has never been built, so there is nothing to "
                "update one file of",
                hint=f"build it once with knowledge_build(layer='{PROJECT}'), then use "
                     "only= for the files you edit",
            )

    blocked = _unbuildable(profile, game)
    if wanted == ALL:
        plan = [name for name in (CORE, DEPS, PROJECT) if not blocked[name]]
        skipped = {name: blocked[name] for name in (CORE, DEPS, PROJECT) if blocked[name]}
    else:
        if blocked[wanted]:
            return fail(
                f"the {wanted!r} layer cannot be built here: {blocked[wanted]}",
                hint="knowledge_status shows what each layer needs; the other layers can "
                     "still be built on their own",
            )
        plan, skipped = [wanted], {}

    if not plan:
        return fail(
            "no layer can be built for this project right now",
            hint="; ".join(f"{name}: {why}" for name, why in skipped.items()),
        )

    store = session.jobs()
    busy = _in_flight()
    if busy is not None:
        return fail(
            f"a knowledge build is already running for this project (job {busy.id})",
            hint=f"wait for it with job_wait('{busy.id}'), or look at it with "
                 f"job_status('{busy.id}') -- two builds of one layer would each report "
                 "counts for an index the other is still rewriting",
        )

    job = store.create(BUILD_KIND)
    log_dir = store.artifacts_dir(job.id)

    def run() -> None:
        # Everything that can fail is inside this try, `store.start` included:
        # a thread that dies before its job is resolved leaves the job at
        # "running" forever, blocks every later build, and reports its
        # traceback to stderr where the calling agent never looks.
        try:
            store.start(job.id)
            reports: dict = {}
            failures: list[str] = []
            for name in plan:
                try:
                    reports[name] = _build_one(
                        index, name, profile, game, tools_root, full, only
                    )
                except (LayerBuildError, OSError, PboError, ValueError) as exc:
                    failures.append(f"{name}: {type(exc).__name__}: {exc}")
            if reports:
                _write_sidecar(index, reports)
            artifact = log_dir / "knowledge-build.json"
            payload = {
                "layers": {name: report.to_dict() for name, report in reports.items()},
                "skipped": skipped,
                "failed": failures,
            }
            try:
                artifact.write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                store.add_artifact(job.id, artifact)
            except OSError:
                pass
            parts = [report.describe() for report in reports.values()]
            for name, report in reports.items():
                for note in report.notes:
                    parts.append(f"{name}: {note}")
                for problem in report.skipped[:5]:
                    parts.append(f"{name}: skipped {problem.path}: {problem.reason}")
                if len(report.skipped) > 5:
                    parts.append(
                        f"{name}: and {len(report.skipped) - 5} more skipped source(s) "
                        "-- see the knowledge-build.json artifact"
                    )
            for name, why in skipped.items():
                parts.append(f"{name}: not built -- {why}")
            summary = " | ".join(parts)
            store.finish(job.id, 1 if failures else 0, summary=summary)
            if failures:
                # `finish` already marked it failed; this puts the reason where
                # a caller reading `error` will find it, without losing the
                # summary of the layers that did build.
                store.fail(job.id, "; ".join(failures))
        except Exception as exc:  # noqa: BLE001 - must reach the job, not just stderr
            try:
                store.fail(job.id, f"{type(exc).__name__}: {exc}")
            except Exception:  # noqa: BLE001 - the job store is the broken part
                pass

    try:
        threading.Thread(target=run, daemon=True).start()
    except Exception as exc:  # noqa: BLE001 - a raised tool call answers nobody
        store.fail(job.id, f"the build never started: {type(exc).__name__}: {exc}")
        return fail(
            f"the knowledge build could not be started: {type(exc).__name__}: {exc}",
            hint="this is the process, not the index -- try again, and check "
                 "job_status for what was recorded",
        )
    return ok({
        "job_id": job.id,
        "layers": plan,
        "skipped": skipped,
        "index": str(index.path),
        "full": full,
        "only": list(only) if only else [],
    })


# ------------------------------------------------------------------ the status


def knowledge_status() -> Result:
    """What each knowledge layer holds, how old it is, and whether it still
    matches what is on disk.

    Staleness is a measurement, never a guess: each layer records the size and
    modification time of every source it read, and this compares them against
    the files as they are now. A layer reports what changed, what appeared and
    what is gone -- so "the project was edited" is a different fact from "the
    game was updated", which is the whole reason there are three layers.

    Two counts that must not be confused, and both are here: `sources` is
    everything the build walked, and `empty_sources` is how many of those gave
    no declarations at all -- an archive that could not be read, or a file that
    genuinely declares nothing. Which of the two is in `last_build`: `skipped`
    is what the LAST build could not read, and `outstanding` is every source
    the layer still holds nothing from, including ones an earlier build found.
    The second exists because the first is correctly empty after an incremental
    build that re-read nothing, which left `empty_sources` standing with no
    explanation at all.
    """
    guard = require_project()
    if guard:
        return guard
    store, failure = _index()
    if failure:
        return failure
    profile = session.profile()
    game = session.game()

    views = []
    for layer in LAYERS:
        view = _layer_view(store, layer, profile, game, measured=True)
        view["last_build"] = _last_build(store, layer, store.layer(layer))
        views.append(view)

    try:
        size = store.path.stat().st_size
    except OSError:
        size = 0

    never = [v["layer"] for v in views if v["applies"] and not v["built"]]
    stale = [v["layer"] for v in views if v["stale"]]
    active = _active(store)
    data = {
        "index": str(store.path),
        "index_bytes": size,
        "layers": views,
        "scope": _scope_view(active, []),
        "never_built": never,
        "stale_layers": stale,
        "declarations": store.count(),
    }
    hints = []
    if active.active:
        hints.append(
            f"an active mod set is in force ({len(active.mods)} mod(s), "
            f"{active.source or 'source not recorded'}): answers come from those mods "
            "plus the game and this project, and anything outside is NAMED rather than "
            "hidden -- knowledge_scope() to see it, knowledge_scope(clear=True) to drop it"
        )
    if never:
        hints.append(
            "never built: " + "; ".join(
                _how_to_build(
                    layer, next(v["unavailable_reason"] for v in views if v["layer"] == layer)
                )
                for layer in never
            )
        )
    if stale:
        hints.append(
            "out of date: " + "; ".join(
                f"knowledge_build(layer='{layer}')" for layer in stale
            )
        )
    return Result(True, data, "", " | ".join(hints))


# ------------------------------------------------------------------ the search


def _empty_index_refusal(
    store: KnowledgeStore, profile, game: str | None
) -> Result | None:
    """Refuse before searching when there is nothing to search.

    An index nobody has built answering "not found" is the single most
    expensive lie this phase can tell: it is indistinguishable from a real
    answer and it is wrong about everything.
    """
    if any(store.layer(layer) is not None for layer in LAYERS):
        return None
    blocked = _unbuildable(profile, game)
    return fail(
        "the knowledge index is empty -- no layer has been built, so a search here "
        "would answer 'not found' about everything that exists",
        hint="build it with knowledge_build() (every layer that applies), or one at a "
             "time: " + "; ".join(
                 _how_to_build(layer, blocked[layer]) for layer in LAYERS
             ),
    )


def _check_search_args(store: KnowledgeStore, kind: str, layer: str) -> Result | None:
    if kind and kind not in KINDS:
        return fail(
            f"unknown kind {kind!r}",
            hint="kind is one of " + ", ".join(f"'{k}'" for k in KINDS)
                 + f" -- '{CLASS}' is a class declared in Enforce Script and '{CONFIG}' "
                   "is a class declared in a config.cpp or config.bin, which are "
                   "different namespaces",
        )
    if layer and layer not in LAYERS:
        return fail(
            f"unknown layer {layer!r}",
            hint="layer is one of " + ", ".join(f"'{name}'" for name in LAYERS),
        )
    if layer and store.layer(layer) is None:
        return fail(
            f"the {layer!r} layer has never been built, so restricting the search to it "
            "can only answer 'not found'",
            hint=f"build it with knowledge_build(layer='{layer}'), or search without the "
                 "layer argument",
        )
    return None


def _clamp(limit: int) -> int:
    try:
        value = int(limit)
    except (TypeError, ValueError):
        return DEFAULT_LIMIT
    return max(1, min(value, MAX_LIMIT))


def _timeout_refusal(exc: SearchTimeout) -> Result:
    return fail(
        str(exc),
        hint=f"the {SEARCH_SECONDS:g}s ceiling every search in this server carries -- "
             "narrow it with kind=, owner= or layer=, use a longer prefix, or lower "
             "limit=, then try again",
    )


def _searched(store: KnowledgeStore, layer: str) -> set[str]:
    """Which layers a query actually looked in.

    What an answer with no results has to report. A record carries its own
    layer, so a non-empty answer names itself; an empty one names nothing at
    all unless this says what was consulted -- and "not found" is precisely the
    answer whose worth depends on how old the layers behind it are. A `core`
    layer indexed before the last game update answers "no such method" exactly
    like a layer indexed a minute ago.
    """
    if layer:
        return {layer}
    return {name for name in LAYERS if store.layer(name) is not None}


def _answer(
    store: KnowledgeStore, records: list[Record], limit: int, profile, game: str | None,
    *, layer: str = "",
) -> tuple[list[Record], bool, list[dict], list[dict]]:
    truncated = len(records) > limit
    page = records[:limit]
    used = {record.layer for record in page} if page else _searched(store, layer)
    return (
        page, truncated,
        _freshness(store, used, profile, game),
        _incomplete(store, profile, game),
    )


def _stale_layers(views: list[dict]) -> list[str]:
    return [view["layer"] for view in views if view.get("stale")]


def _elsewhere(store: KnowledgeStore, name: str, kind: str, owner: str, layer: str,
               mods: list[str] | None = None) -> list[Record]:
    """The same name, without the narrowing that found nothing.

    A search narrowed by `kind=`, `owner=` or `layer=` answers about that slice
    and nothing else, so its "not found" is true and misleading in the same
    breath: `kind='class'` over a name the game declares only in a config says
    nothing about the config, and the caller reads it as "the game has no such
    class". The same "not found is not not looked" the empty index gets, one
    level down -- and it costs a query only when the answer was empty anyway.

    It stays inside the active mod set. What the SET kept out is reported
    separately and by mod name; a second opinion that quietly reached past the
    set would report the same records twice under two different explanations.
    """
    if not name or not (kind or owner or layer):
        return []
    try:
        with store.time_limit(SEARCH_SECONDS):
            return store.find(name, limit=SHOW_LIMIT, mods=mods)
    except SearchTimeout:
        # The narrowed search already answered; a slow second opinion is worth
        # nothing and must never turn a good answer into a failure.
        return []


def _describe_elsewhere(records: list[Record]) -> str:
    seen: list[str] = []
    for record in records:
        where = f"kind='{record.kind}' in layer '{record.layer}'"
        if where not in seen:
            seen.append(where)
    return ", ".join(seen)


def _join(*parts: str) -> str:
    return " | ".join(part for part in parts if part)


def _ages(views: list[dict]) -> str:
    return ", ".join(
        f"{view['layer']} {view['age']}" for view in views if view.get("built")
    )


def _search_hint(
    views: list[dict], missing: list[dict], truncated: bool, empty: bool,
    elsewhere: list[Record] | None = None,
) -> str:
    parts = []
    stale = _stale_layers(views)
    if stale:
        parts.append(
            "measured out of date since it was indexed: "
            + "; ".join(f"knowledge_build(layer='{layer}')" for layer in stale)
            + " -- this answer describes the sources as they were, not as they are"
        )
    if missing and empty:
        parts.append(
            "never built, so this is 'not looked' rather than 'not there': "
            + "; ".join(entry["how"] for entry in missing)
        )
    elif missing:
        parts.append(
            "not searched, because it was never built: "
            + "; ".join(entry["how"] for entry in missing)
        )
    if empty and elsewhere:
        parts.append(
            f"the name IS indexed outside what was asked for -- {len(elsewhere)} "
            f"declaration(s): {_describe_elsewhere(elsewhere)}. Drop kind=/owner=/"
            "layer= to see them"
        )
    elif empty and not missing:
        ages = _ages(views)
        if ages:
            parts.append(
                f"nothing matched, and every layer that could hold it was searched "
                f"-- as each was last indexed ({ages}), so this is 'not there' as of "
                "then, not as of now"
            )
    if truncated:
        parts.append(
            "there are more matches than the limit -- raise limit=, or narrow with "
            f"kind= (for example kind='{CLASS}' for script classes, kind='{CONFIG}' "
            "for config classes) or owner="
        )
    return " | ".join(parts)


def knowledge_find(
    name: str,
    kind: str = "",
    owner: str = "",
    layer: str = "",
    prefix: bool = False,
    limit: int = DEFAULT_LIMIT,
) -> Result:
    """Find declarations by name: classes, methods, constants, enums, configs.

    Matching is case-insensitive and exact unless `prefix=True`. Answers come
    nearest-layer-first -- the project's own declaration before its
    dependencies', and those before the game's.

    `kind` separates two namespaces that share a name space:

      'class'    a class declared in Enforce Script
      'method'   a function, with its owning class and full signature
      'constant' / 'enum'   what flags like ECE_* are found through
      'config'   a class declared in a config.cpp or a binarised config.bin --
                 this is how you ask "does the game have an item class called
                 X". Kept apart because the game alone holds 88 102 of them
                 against 43 595 script declarations, and mixed together they
                 bury every script answer.

    Every answer names the layers it used and how old each one is -- and an
    answer with no results names every layer it *searched*, because "not found"
    is worth exactly as much as the layers behind it are current. The project
    layer's staleness is measured on every call, so an answer taken from an
    index built before your last edit says so instead of describing code that
    no longer exists. A search that finds nothing while a layer that could have
    held it was never built is refused, not answered: "not found" and "not
    looked" are different facts.

    Narrowing has the same trap one level down, so an empty narrowed answer
    checks whether the name exists outside the narrowing and says where in
    `elsewhere` -- asking for `kind='class'` about a name the game declares
    only in a config gets a true "no" that reads as "the game has no such
    class".

    Every search runs under a hard time ceiling and a result limit; neither can
    be removed.
    """
    guard = require_project()
    if guard:
        return guard
    store, failure = _index()
    if failure:
        return failure
    profile = session.profile()
    game = session.game()

    kind = (kind or "").strip().lower()
    layer = (layer or "").strip().lower()
    refusal = (
        _empty_index_refusal(store, profile, game)
        or _check_search_args(store, kind, layer)
    )
    if refusal:
        return refusal
    name = (name or "").strip()
    if not name and not (kind or owner):
        return fail(
            "a search needs something to search for",
            hint="pass a name (add prefix=True to match the start of one), or narrow by "
                 "kind= or owner= to list a whole group",
        )

    limit = _clamp(limit)
    active = _active(store)
    started = time.perf_counter()
    try:
        with store.time_limit(SEARCH_SECONDS):
            records = store.find(
                name,
                kind=kind or None,
                owner=owner or None,
                layer=layer or None,
                prefix=prefix,
                limit=limit + 1,
                mods=_scoped(active),
            )
    except SearchTimeout as exc:
        return _timeout_refusal(exc)
    elapsed = (time.perf_counter() - started) * 1000.0
    scope_view = _scope_view(active, _excluded_find(
        store, active, name, kind=kind or None, owner=owner or None,
        layer=layer or None, prefix=prefix,
    ))

    page, truncated, views, missing = _answer(
        store, records, limit, profile, game, layer=layer
    )
    by_kind: dict[str, int] = {}
    by_layer: dict[str, int] = {}
    for record in page:
        by_kind[record.kind] = by_kind.get(record.kind, 0) + 1
        by_layer[record.layer] = by_layer.get(record.layer, 0) + 1
    data = {
        "query": {
            "name": name, "kind": kind, "owner": owner, "layer": layer,
            "prefix": bool(prefix), "limit": limit,
        },
        "count": len(page),
        "truncated": truncated,
        "elapsed_ms": round(elapsed, 3),
        "results": [record.to_dict() for record in page],
        "by_kind": by_kind,
        "by_layer": by_layer,
        "layers": views,
        "scope": scope_view,
        "unbuilt": [entry["layer"] for entry in missing],
        "stale": bool(_stale_layers(views)),
    }
    elsewhere = [] if page else _elsewhere(store, name, kind, owner, layer, _scoped(active))
    data["elsewhere"] = [record.to_dict() for record in elsewhere]
    hint = _join(
        _search_hint(views, missing, truncated, empty=not page, elsewhere=elsewhere),
        _scope_hint(scope_view),
    )
    if not page and missing:
        return Result(
            False,
            data,
            f"nothing called {name!r} is indexed, and the "
            + ", ".join(repr(entry["layer"]) for entry in missing)
            + " layer(s) have never been built -- this is 'not looked', not 'not there'",
            hint,
        )
    if not page and scope_view["filtered_out"]:
        return Result(
            False, data,
            _narrowed_away(
                f"nothing called {name!r} is in the active mod set", scope_view
            ),
            hint,
        )
    return Result(True, data, "", hint)


# -------------------------------------------------------------------- the show


def _members(store: KnowledgeStore, record: Record, limit: int) -> list[dict]:
    """What is declared inside this class, in its own layer.

    Its own layer on purpose: a class reopened by a mod declares its members in
    the mod's layer, and listing those under the game's declaration would show
    one class holding members from two different bodies of code. The question
    "who else touches this" has its own tool.
    """
    if record.kind not in (CLASS, CONFIG, ENUM):
        return []
    found = store.find("", owner=record.name, layer=record.layer, limit=limit)
    return [
        {"name": m.name, "kind": m.kind, "signature": m.signature,
         "file": m.file, "line": m.line, "flags": list(m.flags)}
        for m in found
    ]


def _ancestors(store: KnowledgeStore, record: Record) -> list[str]:
    """The inheritance chain above this declaration, nearest first.

    Walked rather than stored, so it is capped and cycle-guarded: a chain that
    loops is something somebody wrote, and answering it with an endless walk is
    the worst possible way to report that.

    Each step takes ONE declaration of the parent -- whichever the store's own
    order puts first, which is nearest-layer-first. A name declared twice (a
    mod reopening a game class, or the two branches of an `#ifdef`) therefore
    picks a branch rather than reporting both. It has not misreported on real
    data, and it cannot go far wrong: only the first link can have a second
    declaration worth choosing between, because above it the chain is whatever
    that one declaration says. `knowledge_find` on the parent shows every
    declaration of it when the choice matters.
    """
    chain: list[str] = []
    seen = {record.name.lower()}
    parent = record.parent
    while parent and len(chain) < ANCESTOR_LIMIT:
        chain.append(parent)
        key = parent.lower()
        if key in seen:
            chain[-1] = f"{parent} (cycle)"
            break
        seen.add(key)
        above = store.find(parent, kind=record.kind, limit=1)
        if not above:
            break
        parent = above[0].parent
    return chain


def _body_from_file(path: Path, line: int, max_lines: int) -> dict:
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    if line < 1 or line > len(lines):
        return {"error": f"{path} has {len(lines)} lines; the index recorded line {line}"}
    # Braces are counted on the stripped view, where comments and string
    # contents are blanked out, and sliced from the original: a brace inside a
    # string literal would otherwise end the declaration in the wrong place.
    code = strip_source(text).code.splitlines()
    depth = 0
    opened = False
    end = min(len(lines), line + max_lines - 1)
    for index in range(line - 1, min(len(code), line - 1 + max_lines)):
        row = code[index]
        depth += row.count("{") - row.count("}")
        if "{" in row:
            opened = True
        if opened and depth <= 0:
            end = index + 1
            break
        if not opened and ";" in row:
            end = index + 1
            break
    body = lines[line - 1:end]
    return {
        "file": str(path),
        "from_line": line,
        "to_line": end,
        "truncated": end - line + 1 >= max_lines,
        "text": "\n".join(body),
    }


def _body_from_archive(path: Path, label: str, line: int, max_lines: int) -> dict:
    """The declaration's source, pulled back out of the archive it was read from.

    The index stores a label of the shape `<folder>/<archive>/<entry>`, with a
    `#2` suffix where one archive holds the same entry name twice, so the entry
    can be found again without unpacking anything.
    """
    parts = label.replace("\\", "/").split("/", 2)
    if len(parts) < 3:
        return {"error": f"cannot tell which entry of {path.name} {label!r} names"}
    entry_name = parts[2]
    occurrence = 1
    head, _, tail = entry_name.rpartition("#")
    if head and tail.isdigit():
        entry_name, occurrence = head, int(tail)
    if entry_name.lower().endswith(".bin"):
        return {
            "error": "this declaration came from a binarised config; the archive holds "
                     "the binary form, and the index holds what CfgConvert made of it",
        }
    wanted = entry_name.lower()
    seen = 0
    for entry, blob in scan_pbo(path, lambda n: n.lower().replace("\\", "/") == wanted):
        seen += 1
        if seen < occurrence:
            continue
        text = blob.decode("utf-8", "replace")
        lines = text.splitlines()
        if line < 1 or line > len(lines):
            return {"error": f"{label} has {len(lines)} lines; the index recorded {line}"}
        end = min(len(lines), line + max_lines - 1)
        return {
            "file": f"{path}::{entry.name}",
            "from_line": line,
            "to_line": end,
            "truncated": end - line + 1 >= max_lines,
            "text": "\n".join(lines[line - 1:end]),
        }
    return {"error": f"{path.name} no longer holds an entry called {entry_name!r}"}


def _body(record: Record, max_lines: int) -> dict:
    source = Path(record.source) if record.source else None
    if source is None:
        return {"error": "the index did not record where this was read from"}
    try:
        if source.suffix.lower() == ".pbo":
            return _body_from_archive(source, record.file, record.line, max_lines)
        if not source.is_file():
            return {"error": f"{source} is no longer there -- rebuild the layer"}
        return _body_from_file(source, record.line, max_lines)
    except (OSError, PboError, ValueError, UnicodeError) as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def knowledge_show(
    name: str,
    kind: str = "",
    owner: str = "",
    layer: str = "",
    body: bool = False,
    limit: int = SHOW_LIMIT,
    max_lines: int = BODY_LINES,
) -> Result:
    """Everything the index holds about one declaration.

    Where `knowledge_find` lists matches, this expands them: the full
    signature, the file and line, what the declaration inherits from all the
    way up, and -- for a class -- what is declared inside it.

    `body=True` reads the declaration back out of the source it was indexed
    from, up to `max_lines` lines, including out of an archive that was never
    unpacked. A binarised config has no readable source to return and says so
    rather than returning nothing.

    Matches come nearest-layer-first, so a class a dependency reopens with
    `modded class` is shown before the game's own declaration of it: pass
    `layer='core'` to ask the game specifically.

    Like every answer here, it names the layer each declaration came from and
    how old that layer is -- and when it finds nothing, every layer it looked
    in.
    """
    guard = require_project()
    if guard:
        return guard
    store, failure = _index()
    if failure:
        return failure
    profile = session.profile()
    game = session.game()

    kind = (kind or "").strip().lower()
    layer = (layer or "").strip().lower()
    refusal = (
        _empty_index_refusal(store, profile, game)
        or _check_search_args(store, kind, layer)
    )
    if refusal:
        return refusal
    name = (name or "").strip()
    if not name:
        return fail(
            "knowledge_show needs the name of a declaration",
            hint="use knowledge_find with prefix=True to look one up first",
        )

    limit = max(1, min(int(limit or SHOW_LIMIT), MAX_LIMIT))
    lines = max(1, min(int(max_lines or BODY_LINES), MAX_BODY_LINES))
    active = _active(store)
    try:
        with store.time_limit(SEARCH_SECONDS):
            records = store.find(
                name, kind=kind or None, owner=owner or None,
                layer=layer or None, limit=limit + 1, mods=_scoped(active),
            )
            page, truncated, views, missing = _answer(
                store, records, limit, profile, game, layer=layer
            )
            shown = []
            for record in page:
                entry = record.to_dict()
                members = _members(store, record, MEMBER_LIMIT)
                entry["members"] = members
                entry["member_count"] = len(members)
                entry["inherits"] = _ancestors(store, record)
                entry["body"] = _body(record, lines) if body else None
                shown.append(entry)
    except SearchTimeout as exc:
        return _timeout_refusal(exc)

    scope_view = _scope_view(active, _excluded_find(
        store, active, name, kind=kind or None, owner=owner or None,
        layer=layer or None,
    ))
    elsewhere = [] if shown else _elsewhere(store, name, kind, owner, layer, _scoped(active))
    data = {
        "query": {"name": name, "kind": kind, "owner": owner, "layer": layer,
                  "body": bool(body), "limit": limit},
        "count": len(shown),
        "truncated": truncated,
        "declarations": shown,
        "layers": views,
        "scope": scope_view,
        "unbuilt": [entry["layer"] for entry in missing],
        "stale": bool(_stale_layers(views)),
        "elsewhere": [record.to_dict() for record in elsewhere],
    }
    hint = _join(
        _search_hint(views, missing, truncated, empty=not shown, elsewhere=elsewhere),
        _scope_hint(scope_view),
    )
    if not shown and scope_view["filtered_out"]:
        return Result(
            False, data,
            _narrowed_away(
                f"nothing called {name!r} is in the active mod set", scope_view
            ),
            hint,
        )
    if not shown:
        return Result(
            False,
            data,
            f"nothing called {name!r} is indexed",
            hint or f"look for it with knowledge_find('{name}', prefix=True); if it "
                    "should be there, the layer holding it may need knowledge_build",
        )
    return Result(True, data, "", hint)


# --------------------------------------------------------------- the overrides


def knowledge_callers(
    name: str, kind: str = "", owner: str = "", layer: str = "",
    limit: int = DEFAULT_LIMIT,
) -> Result:
    """Who CALLS this -- every place a method is invoked or a class is built.

    A different question from `knowledge_overrides`, and one no search over
    declarations can answer: an override is a declaration, a call is not.
    This is the question asked before changing a signature, before removing
    something, and when working out how a piece of the game is actually used.

    `kind` narrows to `call` or `new`. `owner` narrows to calls made from one
    class. Each hit names the class and method it was made from, the file and
    line, and the layer -- with that layer's age, like every other answer
    here.

    What it does NOT see, so that an empty answer is not read as proof: a call
    reached through a variable whose type the index does not track is recorded
    under the method's own name, not the variable's, and `new array<string>()`
    has no identifier before its parenthesis to record at all.
    """
    guard = require_project()
    if guard:
        return guard
    store, failure = _index()
    if failure:
        return failure
    profile = session.profile()
    game = session.game()

    layer = (layer or "").strip().lower()
    refusal = (
        _empty_index_refusal(store, profile, game)
        or _check_search_args(store, "", layer)
    )
    if refusal:
        return refusal
    name = (name or "").strip()
    if not name:
        return fail(
            "knowledge_callers needs the name of a method or a class",
            hint="find one with knowledge_find first",
        )
    kind = (kind or "").strip().lower()
    if kind and kind not in (CALL, NEW):
        return fail(
            f"{kind!r} is not a call kind",
            hint=f"use {CALL!r} for an invocation, {NEW!r} for an instantiation, "
                 "or leave it empty for both",
        )

    limit = _clamp(limit)
    active = _active(store)
    started = time.perf_counter()
    try:
        with store.time_limit(SEARCH_SECONDS):
            records = store.callers(
                name, kind=kind or None, owner=owner or None, layer=layer or None,
                limit=limit + 1, mods=_scoped(active),
            )
    except SearchTimeout as exc:
        return _timeout_refusal(exc)
    elapsed = (time.perf_counter() - started) * 1000.0
    scope_view = _scope_view(active, _excluded_callers(
        store, active, name, kind=kind or None, owner=owner or None,
        layer=layer or None,
    ))

    page, truncated, views, missing = _answer(
        store, records, limit, profile, game, layer=layer
    )
    data = {
        "query": {"name": name, "kind": kind, "owner": owner, "layer": layer,
                  "limit": limit},
        "count": len(page),
        "truncated": truncated,
        "elapsed_ms": round(elapsed, 3),
        "results": [record.to_dict() for record in page],
        "layers": views,
        "scope": scope_view,
        "unbuilt": [entry["layer"] for entry in missing],
        "stale": bool(_stale_layers(views)),
    }
    hint = _join(_search_hint(views, missing, truncated, empty=not page),
                 _scope_hint(scope_view))
    if not page and missing:
        return Result(
            False,
            data,
            f"nothing calls {name!r} in what is indexed, and the "
            + ", ".join(repr(entry["layer"]) for entry in missing)
            + " layer(s) have never been built -- a caller lives wherever the code "
              "that uses this was written, so a missing layer is exactly where one "
              "would hide",
            hint,
        )
    if not page and scope_view["filtered_out"]:
        return Result(
            False, data,
            _narrowed_away(
                f"nothing in the active mod set calls {name!r}", scope_view
            ),
            hint,
        )
    return Result(True, data, "", hint)


def knowledge_overrides(
    name: str, owner: str = "", layer: str = "", limit: int = DEFAULT_LIMIT
) -> Result:
    """Who overrides this class or this method -- the question a text sweep
    answers worst.

    For a method: every declaration of it marked `override`, and every one
    declared inside a `modded class`, which routinely replaces a method without
    writing `override` at all. The original declaration is not an answer.

    For a class: everyone who extends it, and every `modded class` that reopens
    it.

    Both readings are answered at once, because a caller asking "who overrides
    X" usually does not yet know which X it is. The answer names the layer each
    hit came from and how old that layer is; a search over a layer that was
    never built is refused rather than answered emptily.
    """
    guard = require_project()
    if guard:
        return guard
    store, failure = _index()
    if failure:
        return failure
    profile = session.profile()
    game = session.game()

    layer = (layer or "").strip().lower()
    refusal = (
        _empty_index_refusal(store, profile, game)
        or _check_search_args(store, "", layer)
    )
    if refusal:
        return refusal
    name = (name or "").strip()
    if not name:
        return fail(
            "knowledge_overrides needs the name of a class or a method",
            hint="find one with knowledge_find first",
        )

    limit = _clamp(limit)
    active = _active(store)
    started = time.perf_counter()
    try:
        with store.time_limit(SEARCH_SECONDS):
            records = store.overrides(
                name, owner=owner or None, layer=layer or None, limit=limit + 1,
                mods=_scoped(active),
            )
    except SearchTimeout as exc:
        return _timeout_refusal(exc)
    elapsed = (time.perf_counter() - started) * 1000.0
    scope_view = _scope_view(active, _excluded_overrides(
        store, active, name, owner=owner or None, layer=layer or None,
    ))

    page, truncated, views, missing = _answer(
        store, records, limit, profile, game, layer=layer
    )
    data = {
        "query": {"name": name, "owner": owner, "layer": layer, "limit": limit},
        "count": len(page),
        "truncated": truncated,
        "elapsed_ms": round(elapsed, 3),
        "results": [record.to_dict() for record in page],
        "layers": views,
        "scope": scope_view,
        "unbuilt": [entry["layer"] for entry in missing],
        "stale": bool(_stale_layers(views)),
    }
    hint = _join(_search_hint(views, missing, truncated, empty=not page),
                 _scope_hint(scope_view))
    if not page and missing:
        return Result(
            False,
            data,
            f"nothing overrides {name!r} in what is indexed, and the "
            + ", ".join(repr(entry["layer"]) for entry in missing)
            + " layer(s) have never been built -- an override lives in a layer above "
              "the declaration, so a missing layer is exactly where one would hide",
            hint,
        )
    if not page and scope_view["filtered_out"]:
        return Result(
            False, data,
            _narrowed_away(
                f"nothing in the active mod set overrides {name!r}", scope_view
            ),
            hint,
        )
    return Result(True, data, "", hint)
