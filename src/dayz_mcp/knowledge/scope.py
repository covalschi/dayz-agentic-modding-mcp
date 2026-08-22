"""The active mod set: which mods the index is allowed to answer from.

The index knows every mod found on this machine. Work, though, always happens
against ONE server, which runs its own subset. An answer about a class from a
mod that server does not run is not merely useless, it is harmful: the agent
writes code that cannot load there, and nothing in the answer said so.

Two decisions shape everything in this module, and neither is an
implementation detail -- they are the feature.

**A filtered-out result is NAMED, never silently hidden.** If a class exists
only in a mod outside the set, the answer says exactly that, with the mod's
name. An empty answer would be the same silent lie as an answer from a stale
layer: the agent reads "no such thing" and goes off to write its own. This is
symmetrical to how every answer already carries the age of the layers behind
it -- a narrowing nobody can see is a narrowing nobody can correct.

**A server query PROPOSES a set; it does not silently rescope the index.**
Asking an address what it runs returns three buckets -- matched, on the server
but not installed, installed but not on the server -- and the caller decides.
A mismatch must be something read, not something quietly acted upon.

**Matching is by Workshop id, and there is no fallback to matching by name.**
Measured, not feared: one mod carries up to four different names (one from the
server, one as the Workshop titles it, one as the folder on disk), and among
14 740 Workshop ids, 19 names belong to more than one mod. Two mods installed
on this machine share their names with different mods, carrying different ids,
running on foreign servers -- a name matcher would silently equate different
builds. Local identity is exact instead: measured on this machine, all 35
modpack folders carry `publishedid` in `meta.cpp` and all 35 agreed with the
Steam content directory they link to, with zero disagreements.

Boundaries, stated here rather than discovered later:

* **A project's own mods have no Workshop id** -- they have no `meta.cpp` --
  so they can only ever come from the profile, never from a server's answer.
  They live in the project layer, which no set narrows.
* **`-serverMod` mods are probably absent from a server's list.** REASONED,
  not tested: that list describes what a client must load, and a server-only
  mod is by definition not that.
* **Version is not identity.** No source reports which BUILD of a mod a server
  runs. A local mod with the right id may be older than what is running there,
  and nothing here detects it.
* **A downloaded mod that is not linked into the modpack is still installed.**
  Measured on this machine: 37 Steam content directories against 35 modpack
  folders, so a folder-only walk calls two of them "not installed".
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from .layers import dependency_dirs

#: Beside the index and the build sidecar, in the project's `.dayz-mcp/`. On
#: disk rather than in memory because the set outlives one session: the server
#: it describes does not change when this process restarts, and a set that
#: silently reverted to "everything" on restart would be the invisible
#: narrowing this feature exists to prevent, running backwards.
SCOPE_FILE = "knowledge-scope.json"

#: Where the game keeps subscribed mods. The same spelling `lifecycle.mod_list`
#: and `layers.dependency_dirs` use.
WORKSHOP_DIRNAME = "!Workshop"

META_NAME = "meta.cpp"

_PUBLISHED = re.compile(r"\bpublishedid\s*=\s*(\d+)", re.IGNORECASE)
_NAME = re.compile(r'\bname\s*=\s*"([^"]*)"', re.IGNORECASE)

#: A meta.cpp is a few hundred bytes. Read with a ceiling anyway -- a folder
#: can hold anything, and this walks folders nobody in this project wrote.
_META_MAX_BYTES = 64 * 1024


# ------------------------------------------------------------------- the set


@dataclass(frozen=True)
class ActiveSet:
    """The mods an answer may come from, and where that list came from.

    `source` is prose on purpose: "the server at <address>", "the project
    profile", "declared by the model". It is carried into every answer that was
    narrowed, because "why is this answer smaller than the index" is the
    question a narrowing has to be able to answer about itself.
    """

    mods: tuple[str, ...] = ()
    source: str = ""
    set_at: float = 0.0
    note: str = ""

    @property
    def active(self) -> bool:
        """False for "no set" -- which is not the same as a set naming nothing.

        A set naming nothing would mean no dependency mod may answer; no set at
        all means every one of them may. Nothing should ever mean the first by
        accident, which is why an empty list never becomes an active set.
        """
        return bool(self.mods)

    def contains(self, folder: str) -> bool:
        return str(folder).strip().lower() in {m.lower() for m in self.mods}

    def age(self, now: float | None = None) -> float:
        if not self.set_at:
            return 0.0
        return max(0.0, (time.time() if now is None else now) - self.set_at)

    def to_dict(self) -> dict:
        return {
            "active": self.active,
            "mods": list(self.mods),
            "count": len(self.mods),
            "source": self.source,
            "set_at": self.set_at or None,
            "note": self.note,
        }


def _clean(mods: Iterable[str]) -> tuple[str, ...]:
    """Blanks dropped, duplicates dropped, the caller's order kept.

    Order is kept because it is how the caller reads its own set back, and a
    sorted answer to an unsorted question looks like a different list.
    """
    out: list[str] = []
    seen: set[str] = set()
    for mod in mods or ():
        name = str(mod).strip()
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        out.append(name)
    return tuple(out)


def _path(directory: str | Path) -> Path:
    return Path(directory) / SCOPE_FILE


def load(directory: str | Path) -> ActiveSet:
    """The set stored beside the index, or "no set".

    Never raises. The set is state, not data anyone would grieve, and a file
    nobody can parse must not take every knowledge tool down with it. Reading
    as "no set" rather than as some remembered narrowing is the safe direction:
    the failure is then a wider answer, which is visible, rather than a
    narrower one, which is not.
    """
    try:
        raw = json.loads(_path(directory).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return ActiveSet()
    if not isinstance(raw, dict):
        return ActiveSet()
    mods = raw.get("mods")
    if not isinstance(mods, list):
        return ActiveSet()
    return ActiveSet(
        mods=_clean(str(m) for m in mods),
        source=str(raw.get("source", "")),
        set_at=float(raw.get("set_at", 0.0) or 0.0),
        note=str(raw.get("note", "")),
    )


def save(directory: str | Path, mods: Iterable[str], *, source: str = "",
         note: str = "") -> ActiveSet:
    """Declare the set. Returns what was actually stored, blanks removed."""
    active = ActiveSet(
        mods=_clean(mods), source=source, set_at=time.time(), note=note
    )
    target = _path(directory)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(active.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return active


def clear(directory: str | Path) -> None:
    """Back to answering from every mod the index holds."""
    try:
        _path(directory).unlink()
    except OSError:
        pass


# ------------------------------------------------------------ local identity


@dataclass(frozen=True)
class LocalMod:
    """A mod folder on this machine, with the id that makes it comparable.

    `declared` is whether this project names it as a dependency -- which is
    what decides whether the index holds anything from it. A mod that is
    installed but not declared is still installed, and saying otherwise would
    be false; saying nothing about the difference would be useless.
    """

    folder: str
    path: str
    workshop_id: int = 0
    meta_name: str = ""
    declared: bool = False

    def to_dict(self) -> dict:
        return {
            "folder": self.folder, "path": self.path,
            "workshop_id": self.workshop_id or None,
            "workshop_name": self.meta_name, "declared": self.declared,
        }


def read_published_id(folder: str | Path) -> tuple[int, str]:
    """`(workshop id, workshop name)` from a mod folder's `meta.cpp`.

    `(0, "")` when there is no meta.cpp -- which is exactly what a project's
    own mod looks like, and is a fact rather than a failure. Nothing is
    inferred from the folder name: the whole point of using the id is that
    names are not identities.
    """
    path = Path(folder) / META_NAME
    try:
        if path.stat().st_size > _META_MAX_BYTES:
            return 0, ""
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0, ""
    published = _PUBLISHED.search(text)
    named = _NAME.search(text)
    return (int(published.group(1)) if published else 0,
            named.group(1) if named else "")


def _folders_in(root: Path) -> list[Path]:
    try:
        return sorted(
            (Path(e.path) for e in os.scandir(root) if e.is_dir()),
            key=lambda p: p.name.lower(),
        )
    except OSError:
        return []


def installed_mods(profile, game: str | None) -> list[LocalMod]:
    """Every mod folder on this machine that could take part in a match.

    Two sources, because either alone lies. The project's declared
    dependencies are what the index actually holds -- but a server runs mods
    this project never declared, and calling those "not installed" when they
    are sitting in the modpack would send the caller to download what they
    already have. So the game's modpack directory is walked too, and each entry
    says which of the two it is.

    The project's own mods are left out entirely: they belong to the project
    layer, which no set narrows, and offering one as a set entry would offer a
    narrowing that does nothing.
    """
    own = {name.lower() for name in getattr(profile, "own_mod_dirs", []) or ()}
    declared = [Path(p) for p in dependency_dirs(profile, game or "")]
    declared_keys = {p.name.lower() for p in declared}

    candidates: list[tuple[Path, bool]] = [(p, True) for p in declared]
    if game:
        for folder in _folders_in(Path(game) / WORKSHOP_DIRNAME):
            if folder.name.lower() not in declared_keys:
                candidates.append((folder, False))

    found: list[LocalMod] = []
    seen: set[str] = set()
    for folder, is_declared in candidates:
        key = folder.name.lower()
        if key in own or key in seen:
            continue
        seen.add(key)
        workshop_id, name = read_published_id(folder)
        found.append(LocalMod(
            folder=folder.name, path=str(folder), workshop_id=workshop_id,
            meta_name=name, declared=is_declared,
        ))
    return found


# ---------------------------------------------------------------- the match


@dataclass(frozen=True)
class Buckets:
    """The three answers a comparison can give, plus the one it cannot.

    `unidentified` is the fourth: mods installed here with no Workshop id at
    all. They are not `local_only` -- "the server does not run it" is a claim,
    and an absent id cannot make it.
    """

    matched: tuple[tuple, ...] = ()
    server_only: tuple = ()
    local_only: tuple[LocalMod, ...] = ()
    unidentified: tuple[LocalMod, ...] = ()

    def proposed(self) -> list[str]:
        """The set a server query suggests: the folders that matched.

        A suggestion, never an action -- applying it is a separate, deliberate
        call, so a mismatch stays something the caller reads.

        Every matched folder, including two folders carrying one id: both hold
        that mod's content and either may be the one the index was built from,
        so dropping one would narrow the set to a folder the build never saw.
        """
        out: list[str] = []
        for _, local in self.matched:
            if local.folder not in out:
                out.append(local.folder)
        return out

    def shared_ids(self) -> list[tuple[int, list[str]]]:
        """Workshop ids carried by more than one folder on this machine.

        Not an error and not a bucket: it is a fact the caller has to be told,
        because version is not identity. Two folders with one id are two copies
        of a mod that may be two different BUILDS of it, and nothing here can
        say which one the server runs.
        """
        by_id: dict[int, set[str]] = {}
        for local in (*[m for _, m in self.matched], *self.local_only):
            by_id.setdefault(local.workshop_id, set()).add(local.folder)
        return [
            (wid, sorted(folders, key=str.lower))
            for wid, folders in sorted(by_id.items())
            if len(folders) > 1
        ]

    def to_dict(self) -> dict:
        return {
            "matched": [
                {"workshop_id": remote.workshop_id,
                 "server_name": remote.name,
                 "folder": local.folder,
                 "workshop_name": local.meta_name,
                 "declared": local.declared}
                for remote, local in self.matched
            ],
            "on_server_not_installed": [
                {"workshop_id": m.workshop_id, "server_name": m.name}
                for m in self.server_only
            ],
            "installed_not_on_server": [m.to_dict() for m in self.local_only],
            "no_workshop_id": [m.to_dict() for m in self.unidentified],
        }


def match_by_id(server_mods: Sequence, local_mods: Sequence[LocalMod]) -> Buckets:
    """Compare a server's list against what is installed, by id and only by id.

    There is deliberately no name-based second pass. A near-match printed as a
    match is worse than no match at all: the caller would scope the index to a
    build the server is not running, and every answer after that would be
    confidently wrong in a way nothing downstream could detect.

    An id maps to a LIST of folders, not to one. Two folders can carry the same
    `publishedid` -- a copy taken beside the link, the same mod reached through
    both the modpack and a declared path -- and keeping only the first made the
    second vanish from every bucket at once: not matched, not installed-only,
    not unidentified. That is the silent narrowing this whole feature exists to
    prevent, committed by the thing that reports it.
    """
    by_id: dict[int, list[LocalMod]] = {}
    unidentified: list[LocalMod] = []
    for local in local_mods:
        if local.workshop_id:
            by_id.setdefault(local.workshop_id, []).append(local)
        else:
            unidentified.append(local)

    matched: list[tuple] = []
    server_only: list = []
    seen: set[int] = set()
    for remote in server_mods:
        here = by_id.get(remote.workshop_id)
        if not here:
            server_only.append(remote)
            continue
        seen.add(remote.workshop_id)
        matched.extend((remote, local) for local in here)

    local_only = [m for wid, group in by_id.items() if wid not in seen for m in group]
    local_only.sort(key=lambda m: m.folder.lower())
    return Buckets(
        matched=tuple(matched),
        server_only=tuple(server_only),
        local_only=tuple(local_only),
        unidentified=tuple(unidentified),
    )
