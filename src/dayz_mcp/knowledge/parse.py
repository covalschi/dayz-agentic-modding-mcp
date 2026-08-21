"""Enforce Script declaration parsing: pure text in, declarations out.

No files, no processes, no knowledge of any project -- the same shape as
`logparse`. Callers that need the `{ok, data, error, hint}` envelope wrap
these results; the parser itself stays a library so it can be measured
against the sources directly.

Three properties carry the whole module:

**Comments and string literals are removed before anything is parsed.** Real
vanilla sources document their API with worked code examples inside `/** */`
blocks, keep obsolete implementations commented out, and pass class names
around as strings. Every one of those looks exactly like a declaration to a
line-oriented regex, and an index that swallows them is wrong in a way that
still reports success.

**Scope is tracked by brace depth, not by indentation.** A class body holds
declarations; a method body holds statements, and statements are call-shaped:
`AddAction(ActionOpenDoors);` is indistinguishable from a declaration without
knowing which of the two you are standing in. Vanilla has over a thousand of
that one line alone.

**A declaration that is never terminated must not swallow the next one.**
Vanilla is not uniformly punctuated: `1_core/proto/proto.c` lists engine
prototypes with no trailing `;`, and several `typedef` lines end at the line
break. A scanner that reads forward until the next `{` walks straight over
the following class -- which is why method headers end at their own closing
parenthesis and unrecognised constructs end at their own line.

Conditional compilation is **recorded, not resolved**. Measured on the vanilla
corpus: 4.9% of lines sit under `#ifdef`/`#ifndef`, nested up to five deep,
and 105 class declarations plus roughly four thousand members are guarded. The
guard symbols are build-configuration flags -- `SERVER`, `DIAG_DEVELOPER`,
`PLATFORM_*`, `DOXYGEN` -- so there is no one define set that is the truth:
the same tree is compiled for a server, a client and a diag build, and this
server drives all of them. Resolving against a chosen set would make the index
answer "no such method" for a method that exists in the build actually
running. So every declaration is indexed and carries the conjunction of the
conditions it is written under, and callers that know their build can filter.

The approach -- an API index over unpacked sources -- follows
`quantumloader/dayz-api-mcp-server` (MIT), re-implemented here rather than
ported.
"""
from __future__ import annotations

import re
from bisect import bisect_right
from dataclasses import dataclass, field
from pathlib import Path

CLASS = "class"
METHOD = "method"
CONSTANT = "constant"
ENUM = "enum"
#: A class declared in a config (`config.cpp`, or a `config.bin` run through
#: CfgConvert), which is a different namespace from an Enforce class and is
#: kept apart from one. They answer different questions -- "is there an item
#: with this name in the game" against "is there a script class" -- and on the
#: real modpack the config classes outnumber the script ones several times
#: over, so folding them together would bury every script answer under them.
CONFIG = "config"

#: Flags a declaration can carry. `proto native` and `proto` are exclusive:
#: `proto` alone still means "implemented by the engine, no script body", and
#: collapsing the two would lose a distinction the sources make.
MODDED = "modded"
OVERRIDE = "override"
PROTO = "proto"
PROTO_NATIVE = "proto native"

#: What a preprocessor directive is replaced by (see `strip_source`). It is a
#: boundary, not a statement: no declaration header may cross it, but it does
#: not end a declaration either, because the body of an `#ifdef`-guarded class
#: sits on the far side of the `#endif`.
BOUNDARY = "\x00"

# Characters that end a declaration header.
_STOPS = "{};" + BOUNDARY


@dataclass(frozen=True)
class Declaration:
    """One declaration, as written.

    `owner` is the class or enum it was declared inside, empty at file scope
    (a global function, a global constant). `parent` is the base class, empty
    when there is none. `signature` is the declaration's header with comments
    removed and runs of whitespace collapsed -- string literals survive
    verbatim, because a default argument like `vector pos = "0 0 0"` is part
    of how the method is called.

    `guard` is the conditional compilation this declaration is written under,
    outermost first, with `!` for a negated condition: `("DIAG_DEVELOPER",)`,
    `("!SERVER",)`, `("PLATFORM_CONSOLE", "SERVER_FOR_CONSOLE")`. Empty means
    unconditional. It is recorded rather than acted on -- see the module
    docstring for why there is no single define set to resolve against.
    """

    name: str
    kind: str
    owner: str = ""
    signature: str = ""
    file: str = ""
    line: int = 0
    flags: tuple[str, ...] = ()
    parent: str = ""
    guard: tuple[str, ...] = ()


@dataclass(frozen=True)
class Stripped:
    """Two views of one source, both the same length as the original so every
    offset in either one still points at the same character of the source.

    `code` is what the scanner reads: comments blanked, string *contents*
    blanked, quotes kept so the token structure survives, preprocessor
    directives replaced by a single `BOUNDARY`.
    `text` is what signatures are sliced from: comments blanked, strings
    intact.
    `directives` maps the offset of each `BOUNDARY` to the directive it
    replaced, as `(keyword, argument)` -- `("ifdef", "DIAG_DEVELOPER")`.
    """

    code: str
    text: str
    directives: dict[int, tuple[str, str]] = field(default_factory=dict)


@dataclass
class _Scope:
    kind: str          # CLASS, ENUM or "block"
    name: str = ""


_MODIFIER = (
    r"override|proto|native|external|static|private|protected|ref|autoptr|"
    r"const|volatile|sealed|owned|notnull|event|local|inout|out"
)
# A type name, optionally templated (`array<string>`, `map<string, ref Foo>`)
# with one level of nesting, optionally an array suffix.
_TYPE = r"[A-Za-z_]\w*\s*(?:<[^<>]*(?:<[^<>]*>[^<>]*)*>)?(?:\s*\[\s*\])?"
# A single base type: one name, optionally templated. Deliberately unable to
# span two type names -- an `#ifdef`/`#else` pair declares the same class
# twice with different parents, and a loose pattern happily recorded
# "Person class Man extends EntityAI" as one base class.
_BASE = r"[A-Za-z_]\w*(?:\s*<[^<>]*(?:<[^<>]*>[^<>]*)*>)?"

# Words that open a statement. None of them can be a return type or a method
# name, and without this a continuation line such as `new Foo();` reads as a
# declaration of a method called `Foo`.
_STATEMENT_WORDS = frozenset(
    """new delete return if else while for foreach switch case default break
    continue super this thread typedef class enum modded""".split()
)

# Modifiers a class or enum declaration may carry. `sealed` is not
# hypothetical: vanilla's `PhysicsWorld` and `Contact` use it, and rejecting it
# cost not just those two classes but every member inside them. `abstract` and
# `final` are here for the mod layers, which this parser also reads.
_CLASS_MODIFIER = (
    r"modded|sealed|abstract|final|native|script|static|private|protected|"
    r"local|external"
)

_CLASS_RE = re.compile(
    rf"^\s*(?P<mods>(?:(?:{_CLASS_MODIFIER})\s+)*)"
    r"(?P<kw>class|enum)\s+(?P<name>[A-Za-z_]\w*)"
    r"(?P<tmpl>\s*<[^<>]*(?:<[^<>]*>[^<>]*)*>)?"
    rf"(?:\s*(?::|extends\b)\s*(?P<parent>{_BASE}))?"
    r"\s*$",
    re.S,
)

_METHOD_RE = re.compile(
    rf"^\s*(?P<mods>(?:(?:{_MODIFIER})\s+)*)"
    rf"(?P<ret>{_TYPE})"
    r"\s+(?P<name>~?[A-Za-z_]\w*)"
    r"\s*\((?P<params>.*)\)$",
    re.S,
)

_CONST_RE = re.compile(
    rf"^\s*(?P<mods>(?:(?:{_MODIFIER})\s+)*)"
    rf"(?P<type>{_TYPE})"
    r"\s+(?P<name>[A-Za-z_]\w*)"
    r"(?:\s*\[[^\]]*\])?"
    r"(?:\s*=\s*(?P<value>.*))?\s*$",
    re.S,
)

_ENUM_MEMBER_RE = re.compile(
    r"^\s*(?P<name>[A-Za-z_]\w*)\s*(?:=\s*(?P<value>.*))?$", re.S
)

_CONST_WORD = re.compile(r"\bconst\b")
_MODDED_WORD = re.compile(r"\bmodded\b")
_LEADING_WORD = re.compile(r"[A-Za-z_]\w*")
_DIRECTIVE_RE = re.compile(r"#(\w+)[ \t]*(.*)")

#: Directives that open a conditional branch, and how the condition reads.
_OPENERS = {"ifdef": "{}", "ifndef": "!{}", "if": "{}"}


def _blank(chars: list[str], start: int, end: int) -> None:
    """Overwrite `chars[start:end]` with spaces, keeping newlines so line
    numbers computed from offsets stay true."""
    for k in range(start, end):
        if chars[k] != "\n":
            chars[k] = " "


def strip_source(source: str) -> Stripped:
    """Blank out everything that is not code, without moving a single offset."""
    n = len(source)
    code = list(source)
    text = list(source)
    i = 0
    while i < n:
        c = source[i]
        if c == "/" and i + 1 < n and source[i + 1] == "/":
            j = source.find("\n", i)
            j = n if j < 0 else j
            _blank(code, i, j)
            _blank(text, i, j)
            i = j
        elif c == "/" and i + 1 < n and source[i + 1] == "*":
            j = source.find("*/", i + 2)
            j = n if j < 0 else j + 2
            _blank(code, i, j)
            _blank(text, i, j)
            i = j
        elif c == '"':
            j = i + 1
            while j < n:
                if source[j] == "\\":
                    j += 2
                    continue
                if source[j] == '"':
                    j += 1
                    break
                if source[j] == "\n":
                    # Unterminated literal: stop at the newline rather than
                    # swallowing the rest of the file.
                    break
                j += 1
            j = min(j, n)
            # Contents only -- the quotes stay so `= ""` still reads as an
            # assignment rather than as a bare `=`.
            _blank(code, i + 1, max(i + 1, j - 1))
            i = j
        else:
            i += 1

    stripped_code, stripped_text, directives = _mark_directives(
        "".join(code), "".join(text)
    )
    return Stripped(stripped_code, stripped_text, directives)


def _mark_directives(
    code: str, text: str
) -> tuple[str, str, dict[int, tuple[str, str]]]:
    """Replace every preprocessor directive line with a single `BOUNDARY`, and
    remember what each one said.

    Runs after comments and strings are gone, so a `#` inside either one is
    already a space and cannot trigger this. A `BOUNDARY` already present in
    the source (sources are text, so this does not happen in practice) is
    simply treated as one more boundary, which is the conservative reading.
    """
    directives: dict[int, tuple[str, str]] = {}
    if "#" not in code:
        return code, text, directives
    out_code = list(code)
    out_text = list(text)
    pos = 0
    for line in code.split("\n"):
        indent = len(line) - len(line.lstrip())
        if line[indent : indent + 1] == "#":
            m = _DIRECTIVE_RE.match(line, indent)
            if m:
                directives[pos + indent] = (m.group(1).lower(), m.group(2).strip())
            _blank(out_code, pos, pos + len(line))
            _blank(out_text, pos, pos + len(line))
            out_code[pos + indent] = BOUNDARY
            out_text[pos + indent] = " "
        pos += len(line) + 1
    return "".join(out_code), "".join(out_text), directives


def _line_starts(source: str) -> list[int]:
    starts = [0]
    pos = source.find("\n")
    while pos >= 0:
        starts.append(pos + 1)
        pos = source.find("\n", pos + 1)
    return starts


def _collapse(s: str) -> str:
    return " ".join(s.split())


def _find_stop(code: str, start: int) -> int:
    """Index of the next header-ending character at or after `start`."""
    best = len(code)
    for ch in _STOPS:
        pos = code.find(ch, start)
        if 0 <= pos < best:
            best = pos
    return best


def _find_line_end(code: str, start: int) -> int:
    pos = code.find("\n", start)
    return len(code) if pos < 0 else pos


def _find_statement_end(code: str, start: int) -> int:
    """Index of the `;` that ends this statement, skipping over any braces it
    contains -- `const int a[2] = {1, 2};` is one statement, not three."""
    depth = 0
    i = start
    n = len(code)
    while i < n:
        c = code[i]
        if c == "{":
            depth += 1
        elif c == "}":
            if depth == 0:
                return i
            depth -= 1
        elif depth == 0 and (c == ";" or c == BOUNDARY):
            return i
        i += 1
    return n


def _find_bracket_end(code: str, start: int) -> int:
    """Index just past the `]` matching the `[` at `start`."""
    depth = 0
    i = start
    n = len(code)
    while i < n:
        if code[i] == "[":
            depth += 1
        elif code[i] == "]":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return start + 1


def _find_paren_end(code: str, start: int) -> int:
    """Index just past the `)` closing the parameter list of the declaration
    at `start`, or -1 when there is none.

    This -- not the next `{` -- is where a method header ends. A signature may
    legitimately span five lines, and an unterminated prototype must not reach
    forward into the next declaration.
    """
    stop = _find_stop(code, start)
    open_paren = code.find("(", start)
    if open_paren < 0 or open_paren > stop:
        return -1
    depth = 0
    i = open_paren
    n = len(code)
    while i < n:
        c = code[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return -1


def _find_enum_member_end(code: str, start: int) -> tuple[int, str]:
    """End of one enum member and the character that ended it."""
    depth = 0
    i = start
    n = len(code)
    while i < n:
        c = code[i]
        if c in "([":
            depth += 1
        elif c in ")]":
            depth -= 1
        elif depth == 0 and (c in ",}" or c == BOUNDARY):
            return i, c
        i += 1
    return n, ""


def _flags_from_mods(mods: str) -> tuple[str, ...]:
    words = mods.split()
    flags: list[str] = []
    if OVERRIDE in words:
        flags.append(OVERRIDE)
    if PROTO in words:
        flags.append(PROTO_NATIVE if "native" in words else PROTO)
    return tuple(flags)


def _is_statement_word(token: str) -> bool:
    m = _LEADING_WORD.match(token.strip())
    return bool(m) and m.group(0) in _STATEMENT_WORDS


def _negate(condition: str) -> str:
    return condition[1:] if condition.startswith("!") else "!" + condition


@dataclass
class _Parser:
    code: str
    text: str
    file: str
    starts: list[int] = field(default_factory=list)
    directives: dict[int, tuple[str, str]] = field(default_factory=dict)
    scopes: list[_Scope] = field(default_factory=list)
    guards: list[str] = field(default_factory=list)
    out: list[Declaration] = field(default_factory=list)

    def line_of(self, pos: int) -> int:
        return bisect_right(self.starts, pos)

    def apply_directive(self, pos: int) -> None:
        """Update the conditional-compilation stack for the directive at `pos`.

        A stray `#else` or `#endif` with nothing open is ignored rather than
        raised on: one malformed directive must not cost the rest of the file.
        """
        keyword, argument = self.directives.get(pos, ("", ""))
        shape = _OPENERS.get(keyword)
        if shape is not None:
            symbol = argument.split()[0] if argument else "?"
            self.guards.append(shape.format(symbol))
        elif keyword == "else":
            if self.guards:
                self.guards[-1] = _negate(self.guards[-1])
        elif keyword == "elif":
            if self.guards:
                self.guards[-1] = argument.split()[0] if argument else "?"
        elif keyword == "endif":
            if self.guards:
                self.guards.pop()

    def sync(self, start: int, end: int) -> None:
        """Apply every directive in `[start, end)`.

        Some jumps -- a parameter list, a braced initialiser, an attribute --
        step over a directive instead of landing on it, and a guard stack that
        misses an `#endif` mislabels everything after it.
        """
        pos = self.code.find(BOUNDARY, start, end)
        while pos >= 0:
            self.apply_directive(pos)
            pos = self.code.find(BOUNDARY, pos + 1, end)

    def owner(self) -> str:
        for scope in reversed(self.scopes):
            if scope.kind in (CLASS, ENUM):
                return scope.name
        return ""

    def at_decl_scope(self) -> bool:
        return not self.scopes or self.scopes[-1].kind in (CLASS, ENUM)

    def in_enum(self) -> bool:
        return bool(self.scopes) and self.scopes[-1].kind == ENUM

    def emit(self, pos: int, end: int, **kw) -> None:
        self.out.append(
            Declaration(
                signature=_collapse(self.text[pos:end]),
                file=self.file,
                line=self.line_of(pos),
                owner=self.owner(),
                guard=tuple(self.guards),
                **kw,
            )
        )


def parse_source(source: str, file: str = "") -> list[Declaration]:
    """Every declaration in one Enforce Script source, in the order written."""
    stripped = strip_source(source)
    p = _Parser(
        stripped.code,
        stripped.text,
        file,
        _line_starts(source),
        stripped.directives,
    )
    code = p.code
    n = len(code)
    # The scope the next `{` will open. Every matched declaration replaces it:
    # the innermost brace belongs to the nearest declaration before it, and
    # nothing else.
    pending: _Scope | None = None
    i = 0

    while i < n:
        c = code[i]
        if c.isspace():
            i += 1
            continue
        if c == BOUNDARY:
            # Deliberately does NOT clear `pending`: an `#ifdef`-guarded class
            # declares its header on one side of the directive and opens its
            # body on the other.
            p.apply_directive(i)
            i += 1
            continue
        if c == "{":
            p.scopes.append(pending or _Scope("block"))
            pending = None
            i += 1
            continue
        if c == "}":
            if p.scopes:
                p.scopes.pop()
            pending = None
            i += 1
            continue
        if c == ";":
            pending = None
            i += 1
            continue

        if not p.at_decl_scope():
            # Inside a method body. Nothing here is a declaration; advance to
            # the next brace or semicolon so the scope stack keeps tracking.
            i = _find_stop(code, i)
            continue

        if c == "[":
            # An attribute such as `[NonSerialized()]`. It belongs to the
            # declaration that follows, which we are about to read.
            step = _find_bracket_end(code, i)
            p.sync(i, step)
            i = _advance(i, step)
            continue

        if p.in_enum():
            i = _advance(i, _parse_enum_member(p, i))
            continue

        try:
            step, pending = _parse_declaration(p, i, pending)
        except Exception:
            # Error recovery: one shape this parser cannot read costs that
            # declaration, never the rest of the file.
            step, pending = min(_find_stop(code, i), _find_line_end(code, i)), pending
        i = _advance(i, step)

    return p.out


def _advance(current: int, proposed: int) -> int:
    """The scan position always moves forward. A matcher that returned where it
    started would spin here forever, and a hang is the one failure an agent
    cannot diagnose."""
    return proposed if proposed > current else current + 1


def _parse_enum_member(p: _Parser, i: int) -> int:
    end, terminator = _find_enum_member_end(p.code, i)
    # Matched against a slice, not with `pos`/`endpos`: `^` anchors to the real
    # start of the string, not to the index a search begins at.
    member = p.code[i:end]
    m = _ENUM_MEMBER_RE.match(member)
    if m:
        p.emit(i, i + len(member.rstrip()), name=m.group("name"), kind=CONSTANT)
    return end + 1 if terminator == "," else end


def _parse_declaration(
    p: _Parser, i: int, pending: _Scope | None
) -> tuple[int, _Scope | None]:
    """Try every declaration shape at `i`; return where to continue and the
    scope the next `{` should open."""
    code = p.code

    # Tried unconditionally rather than gated on a leading keyword: the gate
    # was a list of class modifiers waiting to be incomplete, and `sealed`
    # already was not on it.
    stop = _find_stop(code, i)
    header = code[i:stop]
    m = _CLASS_RE.match(header)
    if m:
        end = i + len(header.rstrip())
        kind = CLASS if m.group("kw") == CLASS else ENUM
        flags = (MODDED,) if _MODDED_WORD.search(m.group("mods")) else ()
        parent = _collapse(m.group("parent") or "")
        p.emit(i, end, name=m.group("name"), kind=kind, flags=flags, parent=parent)
        p.sync(i, end)
        return end, _Scope(kind, m.group("name"))

    close = _find_paren_end(code, i)
    if close > 0:
        header = code[i:close]
        m = _METHOD_RE.match(header)
        if (
            m
            and not _is_statement_word(m.group("ret"))
            and m.group("name") not in _STATEMENT_WORDS
        ):
            p.emit(
                i,
                close,
                name=m.group("name"),
                kind=METHOD,
                flags=_flags_from_mods(m.group("mods")),
            )
            p.sync(i, close)
            # A body if one follows; a `;` clears it if one does not.
            return close, _Scope("block")

    # Constants are the only non-callable members indexed: `const` is what
    # tells a flag apart from an ordinary mutable member variable.
    stop = _find_statement_end(code, i)
    header = code[i:stop]
    m = _CONST_RE.match(header)
    if m and _CONST_WORD.search(m.group("mods")):
        end = i + len(header.rstrip())
        p.emit(i, end, name=m.group("name"), kind=CONSTANT)
        p.sync(i, end)
        return end, pending

    # Not a declaration this index carries (a member variable, a typedef, a
    # shape we do not model). Stop at the end of this line as well as at the
    # next structural character: several vanilla constructs -- `typedef` among
    # them -- simply end at the line break, and reading past one swallows the
    # class declared underneath it.
    return min(_find_stop(code, i), _find_line_end(code, i)), pending


_CONFIG_CLASS_RE = re.compile(
    r"^\s*class\s+(?P<name>[A-Za-z_]\w*)"
    r"(?:\s*:\s*(?P<parent>[A-Za-z_]\w*))?\s*$",
    re.S,
)


def parse_config(source: str, file: str = "") -> list[Declaration]:
    """Class definitions in a DayZ config, with what each one inherits from.

    This is the half of the index that answers "is there a class with this
    name in the game" -- a question that came up twice in one session of
    ordinary work, and one the scripts cannot answer, because items live in
    `config.cpp` and never in Enforce Script.

    Config syntax is close enough to the script's to share the lexer (comments
    and string contents are removed first, for the same reasons) and far
    enough to need its own scanner. The one distinction that matters:

        class Base;            <- a forward declaration, indexed nowhere
        class Thing: Base {};  <- a definition, indexed here

    Every mod's config opens by naming the vanilla classes it extends. Reading
    those as declarations would have each mod claim to declare half the game,
    which is precisely the confident wrongness this phase exists to remove.

    Values are not declarations either: `magazines[] = {...}` opens a brace
    like a class body does, and is skipped by shape rather than by a list of
    known property names.

    `#include` is a boundary, not a door: included files are indexed as
    sources in their own right, so nothing is lost by not following them --
    and nothing is invented by guessing where they resolve to.
    """
    stripped = strip_source(source)
    code, text = stripped.code, stripped.text
    starts = _line_starts(source)
    out: list[Declaration] = []
    scopes: list[str] = []
    pending: str | None = None
    i = 0
    n = len(code)
    while i < n:
        c = code[i]
        if c.isspace() or c == BOUNDARY:
            i += 1
            continue
        if c == "{":
            scopes.append(pending or "")
            pending = None
            i += 1
            continue
        if c == "}":
            if scopes:
                scopes.pop()
            pending = None
            i += 1
            continue
        if c == ";":
            pending = None
            i += 1
            continue

        stop = _find_stop(code, i)
        m = _CONFIG_CLASS_RE.match(code[i:stop])
        # A body must follow. Anything else -- `;`, end of file -- is a
        # forward declaration or a truncated file, and neither declares
        # anything.
        if m and code[stop : stop + 1] == "{":
            end = i + len(code[i:stop].rstrip())
            out.append(
                Declaration(
                    name=m.group("name"),
                    kind=CONFIG,
                    owner=next((s for s in reversed(scopes) if s), ""),
                    signature=_collapse(text[i:end]),
                    file=file,
                    line=bisect_right(starts, i),
                    parent=m.group("parent") or "",
                )
            )
            pending = m.group("name")
        i = _advance(i, stop)
    return out


def parse_file(path: str | Path, file: str | None = None) -> list[Declaration]:
    """Parse one source file. `file` overrides the path recorded in the
    declarations, so a layer can store paths relative to its own root.

    Decoding never fails the file: vanilla sources are not uniformly UTF-8,
    and one stray byte must not cost a whole layer.
    """
    path = Path(path)
    source = path.read_text(encoding="utf-8", errors="replace")
    return parse_source(source, file=str(path) if file is None else file)
