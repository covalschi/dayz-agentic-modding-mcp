"""The index: declarations in, instant answers out.

SQLite from the standard library, one file per project in `.dayz-mcp/`, beside
the job journals. No new dependency, nothing to install, nothing to run --
the same rule that keeps the rest of this server startable anywhere.

Three things decide whether this store is worth having.

**The record key is `(layer, name, kind, owner, file, line)`.** Not the name.
Measured on the vanilla corpus: 43 579 declarations carry 26 434 distinct
names, so a name key drops 39% of them without a word -- and among the
casualties are the classes declared once per `#ifdef` branch, where the two
records differ precisely in the parent the index would then be asked about.
Not `(name, file, line)` either: `class Thing { void Thing() {} }` written on
one line is two declarations sharing all three, told apart only by kind and
owner. The key is enforced by a UNIQUE index, so a future parser that emitted
two identical records would raise instead of quietly keeping one.

**A layer is replaced, never appended to.** Rebuilding deletes the previous
generation inside the same transaction that writes the new one. An index that
grew a second copy on every build would still answer -- with duplicates
indistinguishable from real second declarations -- and a rebuild that died
halfway would leave a half-index that looks complete. Both are the quiet kind
of wrong this phase exists to prevent.

**Staleness is measured per source, not per layer.** Each source records what
it was when it was indexed: path, size, modification time. That is what makes
"one project file was saved" a different fact from "the game was updated" --
one timestamp on a layer cannot tell those apart, and incremental rebuild of
the project layer depends on telling them apart. Known limit, stated rather
than hidden: a file rewritten within the same filesystem timestamp tick AND to
exactly the same byte count reads as unchanged. Size catches what the clock
misses and vice versa; only a content digest would close it completely, and
that costs a full read of every source on every check.

Search is indexed on what is actually searched -- name, owner, kind, parent --
with exact and prefix matching, and every query carries a ceiling. The
predecessor's two search tools hang forever (their open bug #51); here an
unbounded answer counts as its own kind of hang.

The approach -- an API index over unpacked sources -- follows
`quantumloader/dayz-api-mcp-server` (MIT), re-implemented here rather than
ported.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Sequence

from .parse import CLASS, MODDED, OVERRIDE, Declaration

#: The three layers, in the order that answers them: the project's own code
#: first, then what it depends on, then the game. A name found in two of them
#: is two true answers, and the nearest one is the one that matters.
PROJECT = "project"
DEPS = "deps"
CORE = "core"
LAYERS: tuple[str, ...] = (PROJECT, DEPS, CORE)

_RANK = {name: rank for rank, name in enumerate(LAYERS)}
#: Anything not one of the three sorts last rather than being refused: the
#: store is not the place to police a layer vocabulary.
_UNKNOWN_RANK = len(LAYERS)

#: Bumped whenever the schema changes. An index is derived data, so a file
#: stamped with any other version is rebuilt rather than read through the new
#: schema -- misreading old rows is how an index starts answering plausible
#: nonsense.
SCHEMA_VERSION = 1

#: Every search has a ceiling. Callers may lower it, never remove it.
DEFAULT_LIMIT = 200

#: How many SQLite virtual-machine steps pass between two checks of the search
#: deadline. Small enough that a runaway query notices its ceiling promptly,
#: large enough that the callback is noise next to the query itself -- measured
#: on the vanilla corpus, an exact lookup runs in 0.07 ms with the handler
#: installed and 0.07 ms without.
_PROGRESS_STEPS = 1000

#: Upper bound for a prefix range scan. The largest code point there is, so
#: every name starting with the prefix sorts below it under SQLite's default
#: (byte-wise) collation.
_PREFIX_CEILING = "\U0010ffff"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS layer (
    name        TEXT PRIMARY KEY,
    rank        INTEGER NOT NULL,
    root        TEXT NOT NULL DEFAULT '',
    built       REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS source (
    id            INTEGER PRIMARY KEY,
    layer         TEXT NOT NULL,
    path          TEXT NOT NULL,
    size          INTEGER NOT NULL,
    mtime         REAL NOT NULL,
    indexed       REAL NOT NULL,
    declarations  INTEGER NOT NULL DEFAULT 0,
    UNIQUE (layer, path)
);

CREATE TABLE IF NOT EXISTS decl (
    id            INTEGER PRIMARY KEY,
    source_id     INTEGER NOT NULL REFERENCES source(id) ON DELETE CASCADE,
    layer         TEXT NOT NULL,
    layer_rank    INTEGER NOT NULL,
    name          TEXT NOT NULL,
    name_lower    TEXT NOT NULL,
    kind          TEXT NOT NULL,
    owner         TEXT NOT NULL,
    owner_lower   TEXT NOT NULL,
    signature     TEXT NOT NULL,
    file          TEXT NOT NULL,
    line          INTEGER NOT NULL,
    flags         TEXT NOT NULL,
    parent        TEXT NOT NULL,
    parent_lower  TEXT NOT NULL,
    guard         TEXT NOT NULL,
    is_modded     INTEGER NOT NULL,
    is_override   INTEGER NOT NULL,
    owner_modded  INTEGER NOT NULL
);

-- The record key. A UNIQUE index rather than a convention, so a collision is
-- a loud error instead of a lost declaration.
CREATE UNIQUE INDEX IF NOT EXISTS idx_decl_key
    ON decl (layer, name, kind, owner, file, line);

-- Name lookup, exact and prefix. The trailing columns are the result order,
-- term for term (see `_ORDER`), so SQLite reads rows out of the index already
-- sorted and stops at the limit -- no temp b-tree, no collecting every match
-- first. Any drift between this list and `_ORDER` silently reintroduces the
-- sort, which is why a test asserts on the query plan.
CREATE INDEX IF NOT EXISTS idx_decl_name
    ON decl (name_lower, layer_rank, file, line);
CREATE INDEX IF NOT EXISTS idx_decl_owner  ON decl (owner_lower, name_lower);
CREATE INDEX IF NOT EXISTS idx_decl_kind   ON decl (kind, name_lower);
-- "Who overrides this" for a class: everyone who extends it.
CREATE INDEX IF NOT EXISTS idx_decl_parent ON decl (parent_lower);
CREATE INDEX IF NOT EXISTS idx_decl_source ON decl (source_id);
"""


class KnowledgeStoreError(Exception):
    """Something the store refuses to do quietly."""


class SearchTimeout(KnowledgeStoreError):
    """A search was still running when its ceiling ran out.

    Every search in this server has one. The predecessor project's two search
    tools hang forever -- their open bug #51 -- because the client behind them
    was created without a timeout, and an answer that never arrives is the one
    failure a calling agent cannot diagnose, retry or route around.
    """


class DuplicateDeclaration(KnowledgeStoreError):
    """Two declarations claimed the same record key.

    Never seen on the vanilla corpus (measured: zero collisions across 43 579
    declarations). Raised rather than resolved, because every way of resolving
    it loses one of the two, and losing one silently is the failure this whole
    layer is built against.
    """


def record_key(layer: str, declaration: Declaration) -> tuple:
    """THE record key, as a value.

    Must stay term-for-term identical to `idx_decl_key` in the schema above.
    It is exported because the layer builder has to know when two declarations
    would claim the same row *before* the constraint says so -- a third-party
    archive is allowed to be odd in ways vanilla never is, and the answer to
    that has to be a counted, named loss rather than a dead layer. Two
    spellings of this formula is how a deduplication quietly stops matching
    the constraint it exists to satisfy, so there is one.
    """
    return (
        layer,
        declaration.name,
        declaration.kind,
        declaration.owner,
        declaration.file,
        declaration.line,
    )


def mod_folder(layer: str, file: str) -> str:
    """Which mod folder a declaration came from, or "" when it belongs to none.

    THE formula for the active mod set, and the reason that feature needs
    neither a schema change nor a re-index: the dependency layer already labels
    every declaration `<mod folder>/<archive>/<entry>`, so which mod owns a
    declaration is a fact the index has been recording since it was built.

    Only the dependency layer has one. The game is the substrate every DayZ mod
    is written against and the project layer is the code being written -- an
    active set that narrowed either would answer "no such class" about code the
    agent is looking at.

    Case is preserved, because this is also what an answer prints. Matching is
    case-insensitive, which is `lower()` on both sides in the SQL below.

    Must stay term-for-term identical to `_MOD_FOLDER_SQL`; a test compares the
    two over every record of a seeded index, because two spellings of one rule
    is how a filter quietly stops matching the function that explains it.
    """
    if layer != DEPS:
        return ""
    head, separator, _ = file.partition("/")
    return head if separator else ""


#: `mod_folder` above, as SQL. In the query rather than over its output so the
#: LIMIT counts rows that SURVIVED the filter: filtering afterwards spends the
#: limit on rows it then discards, and a caller asking for fifty gets nine.
#:
#: A CASE expression cannot drive an index, so this cannot steal the query plan
#: from the name index the way a bare `layer = ?` does (see `_layer_term`).
_MOD_FOLDER_SQL = (
    f"(CASE WHEN decl.layer = '{DEPS}' AND instr(decl.file, '/') > 0"
    " THEN substr(decl.file, 1, instr(decl.file, '/') - 1) ELSE '' END)"
)


def _mods_term(mods: Sequence[str], outside: bool) -> tuple[str, list]:
    """Restrict to (or to everything but) the mods named.

    The empty string rides in the list on purpose: it is the folder of
    everything that has none, so `IN ('', ...)` admits the game and the project
    unconditionally and `NOT IN ('', ...)` excludes them from the inverse. One
    formula, both directions -- and the inverse is what makes "a filtered-out
    result is named, never hidden" possible at all.

    NOT `LIKE '<folder>/%'`: an underscore is a single-character wildcard in
    LIKE and real mod folders carry underscores, so that spelling would quietly
    admit a different mod whose name differs in exactly that position.
    """
    values = [str(m).strip().lower() for m in mods if str(m).strip()]
    marks = ",".join("?" * (len(values) + 1))
    operator = "NOT IN" if outside else "IN"
    return f"lower({_MOD_FOLDER_SQL}) {operator} ({marks})", ["", *values]


@dataclass(frozen=True)
class Record:
    """One declaration as the index holds it: everything the parser produced,
    plus which layer it came from and which file on disk it was read out of."""

    name: str
    kind: str
    owner: str = ""
    signature: str = ""
    file: str = ""
    line: int = 0
    flags: tuple[str, ...] = ()
    parent: str = ""
    guard: tuple[str, ...] = ()
    layer: str = ""
    source: str = ""

    @property
    def declaration(self) -> Declaration:
        """The parser's own record, unchanged -- so a round trip through the
        store can be compared against what went in."""
        return Declaration(
            name=self.name,
            kind=self.kind,
            owner=self.owner,
            signature=self.signature,
            file=self.file,
            line=self.line,
            flags=self.flags,
            parent=self.parent,
            guard=self.guard,
        )

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "kind": self.kind,
            "owner": self.owner,
            "signature": self.signature,
            "file": self.file,
            "line": self.line,
            "flags": list(self.flags),
            "parent": self.parent,
            "guard": list(self.guard),
            "layer": self.layer,
            "source": self.source,
        }


@dataclass(frozen=True)
class SourceState:
    """What one source was when it was indexed. The whole staleness
    measurement is a comparison against these three numbers."""

    path: str
    size: int
    mtime: float
    indexed: float
    declarations: int


@dataclass(frozen=True)
class LayerInfo:
    name: str
    root: str
    built: float
    updated: float
    sources: int
    declarations: int

    def age(self, now: float | None = None) -> float:
        """Seconds since anything in this layer was last indexed. What an
        answer quotes when it says which layer it came from and how old."""
        current = time.time() if now is None else now
        return max(0.0, current - self.updated)


@dataclass(frozen=True)
class Staleness:
    """The result of a measurement, never of a guess.

    `changed` and `added` are what to reindex; `missing` is what to forget.
    `scanned_for_new` says whether the caller supplied the current file set --
    without it the store cannot know about files it has never seen, and says
    so rather than implying there are none.
    """

    layer: str
    changed: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    added: tuple[str, ...] = ()
    unchanged: int = 0
    scanned_for_new: bool = False
    never_built: bool = False

    @property
    def stale(self) -> bool:
        return self.never_built or bool(self.changed or self.missing or self.added)

    @property
    def outdated(self) -> tuple[str, ...]:
        """Exactly the sources an incremental rebuild has to re-read."""
        return self.changed + self.added

    def describe(self) -> str:
        if self.never_built:
            return f"layer '{self.layer}' was never built"
        parts = []
        if self.changed:
            parts.append(f"{len(self.changed)} changed")
        if self.added:
            parts.append(f"{len(self.added)} new")
        if self.missing:
            parts.append(f"{len(self.missing)} gone")
        if not parts:
            suffix = "" if self.scanned_for_new else " (new files not looked for)"
            return f"up to date: {self.unchanged} sources{suffix}"
        return f"{self.unchanged} unchanged, " + ", ".join(parts)


class KnowledgeStore:
    """The index for one project. Safe to use from the worker threads that run
    every long operation in this server: one connection, one lock."""

    FILENAME = "knowledge.db"

    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._lock = threading.RLock()
        self._conn = self._connect()

    @classmethod
    def for_project(cls, root: str | Path) -> "KnowledgeStore":
        """`.dayz-mcp/` is where this server already keeps a project's working
        state; the job journals are the neighbours."""
        return cls(Path(root) / ".dayz-mcp" / cls.FILENAME)

    # ------------------------------------------------------------- lifecycle

    @property
    def path(self) -> Path:
        return self._path

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None  # type: ignore[assignment]

    def __enter__(self) -> "KnowledgeStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _connect(self) -> sqlite3.Connection:
        """Open the index, rebuilding the file rather than failing on it.

        An index is derived data: a corrupt file or one written by another
        schema version costs a rebuild, while refusing to open costs the agent
        every knowledge tool at once.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._prepare()
        if conn is None:
            self._discard()
            conn = self._prepare()
            if conn is None:  # pragma: no cover - the file was just removed
                raise KnowledgeStoreError(f"cannot open knowledge index at {self._path}")
        conn.executescript(_SCHEMA)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        return conn

    def _prepare(self) -> sqlite3.Connection | None:
        """A usable connection at the expected schema version, or None."""
        conn = sqlite3.connect(self._path, check_same_thread=False, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.execute("PRAGMA foreign_keys = ON")
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        except sqlite3.DatabaseError:
            conn.close()
            return None
        if version not in (0, SCHEMA_VERSION):
            conn.close()
            return None
        return conn

    def _discard(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            try:
                Path(str(self._path) + suffix).unlink()
            except OSError:
                pass

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        """One write, all or nothing.

        A rebuild that dies halfway -- an unreadable source, a killed job --
        must leave the last good index rather than a partial one that looks
        complete.
        """
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    @contextmanager
    def time_limit(self, seconds: float) -> Iterator[None]:
        """Stop any query run inside this block once `seconds` have passed.

        SQLite cannot be handed a timeout, so this is a progress handler that
        aborts the statement once the deadline is behind it -- the only
        mechanism that interrupts a query already running, rather than
        declining to start one.

        Holds the store's lock for the whole block. The handler belongs to the
        connection, not to a statement, so two searches overlapping on one
        connection would each be running under the other's deadline; the lock
        is re-entrant, so `find` and `overrides` nest inside this without
        noticing.

        The handler is removed on the way out either way. A deadline left
        installed would have expired by the next search, and every later query
        on this connection would abort instantly.
        """
        with self._lock:
            deadline = time.monotonic() + max(0.0, float(seconds))

            def expired() -> int:
                return 1 if time.monotonic() > deadline else 0

            self._conn.set_progress_handler(expired, _PROGRESS_STEPS)
            try:
                yield
            except sqlite3.OperationalError as exc:
                if "interrupt" not in str(exc).lower():
                    raise
                raise SearchTimeout(
                    f"the search was still running after {seconds:g}s and was stopped"
                ) from exc
            finally:
                self._conn.set_progress_handler(None, 0)

    # ---------------------------------------------------------------- writing

    def put_source(
        self,
        layer: str,
        path: str | Path,
        declarations: Iterable[Declaration],
        *,
        root: str = "",
        size: int | None = None,
        mtime: float | None = None,
    ) -> int:
        """Index one source, replacing whatever this layer held for it.

        This is the incremental unit: one edited file costs one call, and
        nothing else in the layer is touched. `size` and `mtime` are read from
        the file unless given -- passing them is for callers that already have
        the stat, and for tests that have no file.
        """
        with self._transaction() as conn:
            self._ensure_layer(conn, layer, root)
            return self._write_source(conn, layer, path, declarations, size, mtime)

    def replace_layer(
        self,
        layer: str,
        sources: Iterable[tuple[str | Path, Iterable[Declaration]]],
        *,
        root: str = "",
    ) -> int:
        """Rebuild a whole layer: the previous generation is gone before the
        new one lands, both inside one transaction.

        `sources` is consumed lazily, so a caller may parse as it goes rather
        than holding a corpus in memory.
        """
        with self._transaction() as conn:
            self._clear_layer(conn, layer)
            self._ensure_layer(conn, layer, root, built=time.time())
            total = 0
            for path, declarations in sources:
                total += self._write_source(conn, layer, path, declarations, None, None)
            return total

    def drop_source(self, layer: str, path: str | Path) -> int:
        """Forget one source. Returns how many were actually removed, so a
        caller can tell "deleted" from "was never there"."""
        with self._transaction() as conn:
            cur = conn.execute(
                "DELETE FROM source WHERE layer = ? AND path = ?", (layer, str(path))
            )
            return cur.rowcount

    def drop_layer(self, layer: str) -> None:
        with self._transaction() as conn:
            self._clear_layer(conn, layer)
            conn.execute("DELETE FROM layer WHERE name = ?", (layer,))

    def _ensure_layer(
        self, conn: sqlite3.Connection, layer: str, root: str = "", built: float | None = None
    ) -> None:
        conn.execute(
            "INSERT OR IGNORE INTO layer (name, rank, root, built) VALUES (?, ?, ?, ?)",
            (layer, _RANK.get(layer, _UNKNOWN_RANK), root, built or time.time()),
        )
        if built is not None:
            conn.execute("UPDATE layer SET built = ? WHERE name = ?", (built, layer))
        if root:
            conn.execute("UPDATE layer SET root = ? WHERE name = ?", (root, layer))

    def _clear_layer(self, conn: sqlite3.Connection, layer: str) -> None:
        # Declarations follow their source out by cascade.
        conn.execute("DELETE FROM source WHERE layer = ?", (layer,))

    def _write_source(
        self,
        conn: sqlite3.Connection,
        layer: str,
        path: str | Path,
        declarations: Iterable[Declaration],
        size: int | None,
        mtime: float | None,
    ) -> int:
        key = str(path)
        if size is None or mtime is None:
            try:
                stat = os.stat(path)
                size = stat.st_size if size is None else size
                mtime = stat.st_mtime if mtime is None else mtime
            except OSError:
                # A source that cannot be stat'ed is recorded as absent, which
                # is exactly what the next staleness check will report.
                size = -1 if size is None else size
                mtime = 0.0 if mtime is None else mtime

        decls = list(declarations)
        conn.execute("DELETE FROM source WHERE layer = ? AND path = ?", (layer, key))
        cur = conn.execute(
            "INSERT INTO source (layer, path, size, mtime, indexed, declarations)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (layer, key, int(size), float(mtime), time.time(), len(decls)),
        )
        source_id = int(cur.lastrowid or 0)
        rows = _rows_for(source_id, layer, decls)
        if rows:
            self._insert(conn, rows, key)
        return len(rows)

    @staticmethod
    def _insert(conn: sqlite3.Connection, rows: list[tuple], source: str) -> None:
        sql = (
            "INSERT INTO decl (source_id, layer, layer_rank, name, name_lower, kind,"
            " owner, owner_lower, signature, file, line, flags, parent, parent_lower,"
            " guard, is_modded, is_override, owner_modded)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        )
        try:
            conn.executemany(sql, rows)
        except sqlite3.IntegrityError as exc:
            # Name the offender rather than report a bare constraint failure --
            # the point of having a record key is being able to say which two
            # declarations claimed it.
            #
            # Found in Python, not by re-inserting row by row: `executemany`
            # stops at the failing row and leaves the earlier ones in the
            # transaction, so a retry would trip over its own first insert and
            # accuse the wrong declaration. (The transaction is rolled back by
            # the caller either way, so nothing survives this.)
            raise DuplicateDeclaration(_collision(rows, source)) from exc

    # ---------------------------------------------------------------- reading

    def layers(self) -> list[LayerInfo]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT name FROM layer ORDER BY rank, name"
            ).fetchall()
        return [info for info in (self.layer(row["name"]) for row in rows) if info]

    def layer(self, name: str) -> LayerInfo | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT name, root, built FROM layer WHERE name = ?", (name,)
            ).fetchone()
            if row is None:
                return None
            agg = self._conn.execute(
                "SELECT COUNT(*) AS sources, COALESCE(SUM(declarations), 0) AS decls,"
                " COALESCE(MAX(indexed), 0.0) AS updated"
                " FROM source WHERE layer = ?",
                (name,),
            ).fetchone()
        return LayerInfo(
            name=row["name"],
            root=row["root"],
            built=float(row["built"]),
            updated=max(float(agg["updated"]), float(row["built"])),
            sources=int(agg["sources"]),
            declarations=int(agg["decls"]),
        )

    def sources(self, layer: str, paths: Iterable[str | Path] | None = None) -> list[SourceState]:
        """What this layer has indexed, all of it or only the paths named.

        `paths` exists for the caller that already knows which sources it cares
        about. A layer with 2810 sources costs a full table read per staleness
        check otherwise -- which is fine when the answer is "what changed
        anywhere" and pure waste when the question is "what was this one file
        when I indexed it".
        """
        sql = "SELECT path, size, mtime, indexed, declarations FROM source WHERE layer = ?"
        rows: list[sqlite3.Row] = []
        with self._lock:
            if paths is None:
                rows = list(self._conn.execute(sql + " ORDER BY path", (layer,)).fetchall())
            else:
                wanted = [str(p) for p in paths]
                # Chunked: SQLite's parameter limit is finite and a caller with
                # a thousand paths must not turn into a silent failure.
                for start in range(0, len(wanted), 400):
                    chunk = wanted[start:start + 400]
                    marks = ",".join("?" * len(chunk))
                    rows += self._conn.execute(
                        f"{sql} AND path IN ({marks}) ORDER BY path", (layer, *chunk)
                    ).fetchall()
        return [
            SourceState(
                path=row["path"],
                size=int(row["size"]),
                mtime=float(row["mtime"]),
                indexed=float(row["indexed"]),
                declarations=int(row["declarations"]),
            )
            for row in rows
        ]

    def empty_sources(self, layer: str) -> int:
        """How many of this layer's sources were read and yielded nothing.

        An archive nobody could read is recorded exactly like a file that
        genuinely declares nothing -- both were seen, both gave zero -- so this
        is the number that stops "sources" from overstating coverage. Which of
        the two a particular source is belongs to the build that read it.

        Counted in SQL rather than by listing the sources: an answer that
        reports this on every search was materialising 2927 rows into Python
        objects to add up a column, which cost twenty times the search itself.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM source WHERE layer = ? AND declarations = 0",
                (layer,),
            ).fetchone()
        return int(row[0])

    def mod_folders(self, layer: str = DEPS) -> list[tuple[str, int]]:
        """The mod folders this layer holds declarations from, with how many.

        What lets a caller choose an active set at all, and what tells a typo
        apart from a mod that is genuinely not indexed -- a distinction the
        set's own answer depends on, because a scope naming only names nobody
        knows would silently blank every dependency answer.

        One grouped scan of the layer, so it belongs to the tools that report
        state (`knowledge_scope`, `knowledge_status`) and NOT to the search
        path, which runs on every question.
        """
        sql = (
            f"SELECT {_MOD_FOLDER_SQL} AS folder, COUNT(*) AS held FROM decl"
            " WHERE decl.layer = ? GROUP BY folder ORDER BY folder"
        )
        with self._lock:
            rows = self._conn.execute(sql, (layer,)).fetchall()
        return [(row["folder"], int(row["held"])) for row in rows if row["folder"]]

    def count(self, layer: str | None = None) -> int:
        with self._lock:
            if layer is None:
                row = self._conn.execute("SELECT COUNT(*) FROM decl").fetchone()
            else:
                row = self._conn.execute(
                    "SELECT COUNT(*) FROM decl WHERE layer = ?", (layer,)
                ).fetchone()
        return int(row[0])

    # -------------------------------------------------------------- staleness

    def staleness(
        self, layer: str, current: Iterable[str | Path] | None = None
    ) -> Staleness:
        """Compare what was indexed against what is on disk now.

        `current` is the layer's file set as it stands today. The store never
        walks the filesystem itself: it does not know what a layer's sources
        are supposed to be, and a guess here would be exactly the kind of
        confident wrongness this phase exists to remove. Without it, files the
        index has never seen cannot be reported, and `scanned_for_new` says so.
        """
        if self.layer(layer) is None:
            return Staleness(layer=layer, never_built=True)

        recorded = {s.path: s for s in self.sources(layer)}
        expected = None if current is None else {str(p) for p in current}

        changed: list[str] = []
        missing: list[str] = []
        unchanged = 0
        for path, state in recorded.items():
            if expected is not None and path not in expected:
                missing.append(path)
                continue
            try:
                stat = os.stat(path)
            except OSError:
                missing.append(path)
                continue
            if stat.st_size != state.size or stat.st_mtime != state.mtime:
                changed.append(path)
            else:
                unchanged += 1

        added = (
            tuple(sorted(expected - recorded.keys())) if expected is not None else ()
        )
        return Staleness(
            layer=layer,
            changed=tuple(sorted(changed)),
            missing=tuple(sorted(missing)),
            added=added,
            unchanged=unchanged,
            scanned_for_new=expected is not None,
        )

    # ----------------------------------------------------------------- search

    def find(
        self,
        name: str,
        *,
        kind: str | None = None,
        owner: str | None = None,
        layer: str | None = None,
        prefix: bool = False,
        limit: int = DEFAULT_LIMIT,
        mods: Sequence[str] | None = None,
        outside: bool = False,
    ) -> list[Record]:
        """Declarations called `name`, by name and then nearest layer first.

        Matching is case-insensitive -- the agent types what it remembers, and
        the verbatim spelling comes back in the answer, so nothing is lost.
        An empty `name` means "any", which is how a whole kind gets listed.

        `mods` is the active mod set: only declarations from those mod folders
        answer, plus everything belonging to no mod folder (the game and the
        project). `outside=True` inverts it, which is how a caller finds out
        exactly what the set kept from it -- see `_mods_term`. `mods=None` is
        no set at all, and an empty sequence is a set naming nothing, which are
        different requests and give different answers.
        """
        sql, params = self._find_query(
            name, kind, owner, layer, prefix, limit, mods, outside
        )
        return self._select(sql, params)

    def explain_find(self, name: str, **kwargs) -> str:
        """SQLite's query plan for the same search.

        Here because "instant" is the entire point of this phase, and a query
        that quietly stopped using its index would pass every behavioural test
        above -- just slower and slower as the corpus grows.
        """
        sql, params = self._find_query(
            name,
            kwargs.get("kind"),
            kwargs.get("owner"),
            kwargs.get("layer"),
            kwargs.get("prefix", False),
            kwargs.get("limit", DEFAULT_LIMIT),
            kwargs.get("mods"),
            kwargs.get("outside", False),
        )
        return self._explain(sql, params)

    def _find_query(
        self,
        name: str,
        kind: str | None,
        owner: str | None,
        layer: str | None,
        prefix: bool,
        limit: int,
        mods: Sequence[str] | None = None,
        outside: bool = False,
    ) -> tuple[str, list]:
        where: list[str] = []
        params: list = []
        if name:
            lowered = name.lower()
            if prefix:
                where.append("decl.name_lower >= ? AND decl.name_lower < ?")
                params += [lowered, lowered + _PREFIX_CEILING]
            else:
                where.append("decl.name_lower = ?")
                params.append(lowered)
        if kind:
            where.append("decl.kind = ?")
            params.append(kind)
        if owner is not None:
            where.append("decl.owner_lower = ?")
            params.append(owner.lower())
        if layer:
            where.append(_layer_term(bool(where)))
            params.append(layer)
        if mods is not None:
            clause, values = _mods_term(mods, outside)
            where.append(clause)
            params += values
        params.append(_ceiling(limit))
        return _SELECT + _where(where) + _ORDER, params

    def overrides(
        self,
        name: str,
        *,
        owner: str | None = None,
        layer: str | None = None,
        limit: int = DEFAULT_LIMIT,
        mods: Sequence[str] | None = None,
        outside: bool = False,
    ) -> list[Record]:
        """Who overrides `name` -- the question a text sweep answers worst.

        For a method: every declaration of it marked `override`, plus every one
        declared inside a `modded class`, which routinely re-declares a method
        without writing `override` and replaces it just the same. The base
        declaration itself is not an answer.

        For a class: everyone who extends it, plus every `modded class` of that
        name.

        Both readings are answered at once, because the caller asking "who
        overrides X" usually does not yet know which X is.
        """
        found: dict[tuple, Record] = {}
        for sql, params in self._override_queries(name, owner, layer, limit, mods, outside):
            for record in self._select(sql, params):
                found[(record.layer, record.name, record.kind, record.owner,
                       record.file, record.line)] = record
        ordered = sorted(
            found.values(),
            key=lambda r: (
                r.name.lower(), _RANK.get(r.layer, _UNKNOWN_RANK), r.file, r.line
            ),
        )
        return ordered[: _ceiling(limit)]

    def explain_overrides(self, name: str, **kwargs) -> str:
        plans = [
            self._explain(sql, params)
            for sql, params in self._override_queries(
                name,
                kwargs.get("owner"),
                kwargs.get("layer"),
                kwargs.get("limit", DEFAULT_LIMIT),
                kwargs.get("mods"),
                kwargs.get("outside", False),
            )
        ]
        return "\n".join(plans)

    def _override_queries(
        self, name: str, owner: str | None, layer: str | None, limit: int,
        mods: Sequence[str] | None = None, outside: bool = False,
    ) -> list[tuple[str, list]]:
        """Three separate indexed searches rather than one query with an OR
        across three columns: an OR over different columns is where SQLite
        gives up and scans the table."""
        lowered = name.lower()
        ceiling = _ceiling(limit)
        extra: list[str] = []
        extra_params: list = []
        if owner is not None:
            extra.append("decl.owner_lower = ?")
            extra_params.append(owner.lower())
        if layer:
            # Always the suppressed form here: every one of the three queries
            # below leads with an indexed, selective term of its own.
            extra.append(_layer_term(True))
            extra_params.append(layer)
        if mods is not None:
            clause, values = _mods_term(mods, outside)
            extra.append(clause)
            extra_params += values

        queries = [
            # The method reading.
            (
                ["decl.name_lower = ?", "(decl.is_override = 1 OR decl.owner_modded = 1)"],
                [lowered],
            ),
            # The class reading: everyone who extends it...
            (["decl.parent_lower = ?"], [lowered]),
            # ...and everyone who reopens it.
            (["decl.name_lower = ?", "decl.is_modded = 1", "decl.kind = ?"], [lowered, CLASS]),
        ]
        return [
            (
                _SELECT + _where(clauses + extra) + _ORDER,
                params + extra_params + [ceiling],
            )
            for clauses, params in queries
        ]

    def _select(self, sql: str, params: Sequence) -> list[Record]:
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [_record(row) for row in rows]

    def _explain(self, sql: str, params: Sequence) -> str:
        with self._lock:
            rows = self._conn.execute(
                "EXPLAIN QUERY PLAN " + sql, params
            ).fetchall()
        return "\n".join(str(row["detail"]) for row in rows)


_SELECT = (
    "SELECT decl.layer, decl.name, decl.kind, decl.owner, decl.signature,"
    " decl.file, decl.line, decl.flags, decl.parent, decl.guard,"
    " source.path AS source_path"
    " FROM decl JOIN source ON source.id = decl.source_id"
)
#: By name, then by nearest layer. One rule that answers both questions: an
#: exact lookup has one name, so it degenerates to "the project's own
#: declaration first, then its dependencies, then the game", while a prefix
#: browse comes out alphabetical with each name's layers together.
#:
#: It is also the order `idx_decl_name` already stores, so SQLite reads the
#: rows out of the index and stops at LIMIT instead of collecting every match
#: and sorting it. Measured on the vanilla corpus: prefix "On" took 8.4 ms
#: ordered by layer first and 0.44 ms this way.
_ORDER = " ORDER BY decl.name_lower, decl.layer_rank, decl.file, decl.line LIMIT ?"


def _layer_term(has_other_terms: bool) -> str:
    """The layer filter, written so it does not steal the query plan.

    `layer` is the first column of `idx_decl_key`, and that index is UNIQUE --
    so with no statistics on the table SQLite estimates a `layer = ?` lookup as
    far more selective than it is, picks that index, and scans every row of the
    layer. Measured on the real game's index (131 697 rows in `core`):

        owner + layer   167 ms  ->  0.27 ms
        kind  + layer    28 ms  ->  0.40 ms

    The leading `+` is SQLite's documented way to say "this is a filter, not a
    lookup": it makes the term unusable as an index driver without changing
    what it matches. Applied only when some OTHER indexed term can drive the
    query -- with `layer` alone there is nothing better, and suppressing it
    would turn an index scan into a full table scan.

    ANALYZE would fix the estimate instead, but it would have to be re-run
    after every build to stay true, and a stale sqlite_stat1 misleads the
    planner exactly as much as no statistics do.
    """
    return "+decl.layer = ?" if has_other_terms else "decl.layer = ?"


def _where(clauses: Sequence[str]) -> str:
    return " WHERE " + " AND ".join(clauses) if clauses else ""


def _ceiling(limit: int) -> int:
    """No search is unbounded, however the caller asks."""
    return max(1, min(int(limit), 10_000))


def _record(row: sqlite3.Row) -> Record:
    return Record(
        name=row["name"],
        kind=row["kind"],
        owner=row["owner"],
        signature=row["signature"],
        file=row["file"],
        line=int(row["line"]),
        flags=_decode(row["flags"]),
        parent=row["parent"],
        guard=_decode(row["guard"]),
        layer=row["layer"],
        source=row["source_path"],
    )


def _encode(values: Sequence[str]) -> str:
    """JSON rather than a joined string: one of the flags is `proto native`,
    and any separator simple enough to split on is a separator that flag
    already contains."""
    return json.dumps(list(values))


def _decode(raw: str) -> tuple[str, ...]:
    if not raw:
        return ()
    try:
        return tuple(json.loads(raw))
    except (ValueError, TypeError):  # pragma: no cover - written by _encode
        return ()


def _collision(rows: Sequence[tuple], source: str) -> str:
    """Say which record key was claimed twice, and by what."""
    seen: set[tuple] = set()
    for row in rows:
        key = (row[1], row[3], row[5], row[6], row[9], row[10])
        if key in seen:
            return (
                f"{source}: two declarations share the record key "
                f"(layer={key[0]}, name={key[1]}, kind={key[2]}, owner={key[3]}, "
                f"file={key[4]}, line={key[5]})"
            )
        seen.add(key)
    # Nothing repeats inside this source, so the clash is with a declaration
    # already indexed -- which means two sources were recorded under the same
    # `file` label, and one of them was about to overwrite the other.
    return (
        f"{source}: a declaration collides with one already indexed in layer "
        f"'{rows[0][1]}' -- two sources recorded under the same file name?"
    )


def _rows_for(source_id: int, layer: str, decls: Sequence[Declaration]) -> list[tuple]:
    """Turn one source's declarations into rows, resolving the one fact that
    needs the whole file to see: whether a member sits inside a `modded class`.

    The parser puts `modded` on the class declaration, not on its members, so
    without this pass "who overrides OnConnect" would miss every method a mod
    re-declares without writing `override` -- which is most of them.
    """
    modded_owners = {
        d.name.lower() for d in decls if d.kind == CLASS and MODDED in d.flags
    }
    rank = _RANK.get(layer, _UNKNOWN_RANK)
    return [
        (
            source_id,
            layer,
            rank,
            d.name,
            d.name.lower(),
            d.kind,
            d.owner,
            d.owner.lower(),
            d.signature,
            d.file,
            int(d.line),
            _encode(d.flags),
            d.parent,
            d.parent.lower(),
            _encode(d.guard),
            1 if MODDED in d.flags else 0,
            1 if OVERRIDE in d.flags else 0,
            1 if d.owner and d.owner.lower() in modded_owners else 0,
        )
        for d in decls
    ]
