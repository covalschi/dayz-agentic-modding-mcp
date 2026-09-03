import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ALLOWED = {
    "@MyMod", "@Dep", "@SomeDependency", "@CF", "@B",
    # Generic examples, placeholders and test fixtures
    "@Name", "@ModName", "@ServerOnlyMod", "@folder",
    # The bridge mod this server packs and ships itself (bridge/, tools/bridge.py).
    # It is NOT a concrete project's mod: it belongs to the server, is built from
    # sources inside THIS repository, and every project that uses it uses the same
    # one. Naming it here therefore does not make the server fit one project --
    # which is the only thing this guard exists to prevent.
    "@DZMCP_Bridge",
}

# A mod folder name. Case-INSENSITIVE on purpose: real mods are not all
# capitalised. This machine's own modpack carries lowercase ones, and at least
# one of them collides head-on with this project's vocabulary, which is exactly
# the kind of leak an eye would not catch.
MOD_TOKEN = re.compile(r"@[A-Za-z][A-Za-z0-9_]{1,40}")

# Decorator immunity comes from POSITION, not from spelling: a Python decorator
# starts its own line (indentation allowed), while a mod name in prose, in a
# path or in a TOML value does not. This replaced an earlier attempt at
# immunity-by-spelling (requiring an uppercase initial), which bought the same
# immunity at the cost of every lowercase mod name in existence.
#
# Only the decorator's NAME is skipped. Everything after it -- an argument
# list, a trailing comment -- is still swept, because a decorator's arguments
# and the comment beside it are ordinary text where a mod name can hide: a
# decorator with a mod name in its trailing comment, and one carrying a mod
# name as a parametrize argument, both slipped through when the pattern
# consumed the whole line (see the test below for both shapes).
DECORATOR_NAME = re.compile(r"^\s*@[A-Za-z_][A-Za-z0-9_.]*")

SKIP_DIRS = {".git", ".venv", "__pycache__", ".pytest_cache", ".dayz-mcp", "build", "dist", ".superpowers"}  # .superpowers: git-ignored workspace of plan-execution tooling, never ships
# ".c" and ".cpp" are here because the bridge mod's own sources are Enforce
# Script and config.cpp; ".json" because it is this protocol's own wire format;
# ".hpp"/".h" because that is how DayZ configs are split up. The last three
# hold nothing today -- they are here so the first file of that kind is swept
# on the day it lands, not whenever someone remembers this list exists.
TEXT_SUFFIXES = {
    ".py", ".toml", ".md", ".log", ".cfg", ".txt", ".yml", ".yaml",
    ".c", ".cpp", ".hpp", ".h", ".json", ".xml",
}


def offending_tokens(text: str, *, is_python: bool = False) -> set[str]:
    """Every mod-name-looking token in `text` that is not allow-listed.

    THE definition of "offends this guard", so the tests below that prove the
    guard still bites exercise the same code path as the sweep itself rather
    than a re-spelling of it.

    `is_python` gates the decorator immunity, and nothing else. Nothing is a
    decorator in Markdown, TOML or Enforce Script, so applying it there only
    ever hid real leaks -- a README line consisting of a mod name and nothing
    else looks exactly like a decorator to a positional rule.
    """
    found: set[str] = set()
    for line in text.splitlines():
        scan = line
        if is_python:
            decorator = DECORATOR_NAME.match(line)
            # A decorator name is followed by its arguments, a comment, or
            # nothing at all. Anything else ("@<mod> must be loaded first")
            # is prose that happens to start with a token, and is swept whole.
            if decorator:
                rest = line[decorator.end():]
                if rest.strip() == "" or rest.lstrip().startswith(("(", "#")):
                    scan = rest
        found |= {m.group(0) for m in MOD_TOKEN.finditer(scan)}
    return found - ALLOWED


def iter_paths():
    """Everything under the repository root that is not deliberately skipped --
    directories included, because a directory NAME can be a leak all by
    itself."""
    for p in ROOT.rglob("*"):
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        yield p


def iter_text_files():
    for p in iter_paths():
        if p.is_dir() or p.suffix.lower() not in TEXT_SUFFIXES:
            continue
        if p.name in {"LICENSE", "NOTICE.md"}:
            continue
        yield p


def test_no_concrete_mod_names_anywhere():
    """The server must stay universal: a mod name in the code, an example or a
    fixture is the first step towards a parser that only fits one project."""
    offenders = {}
    for path in iter_text_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        found = offending_tokens(text, is_python=path.suffix.lower() == ".py")
        if found:
            offenders[str(path.relative_to(ROOT))] = sorted(found)
    assert not offenders, f"mod names leaked into the repository: {offenders}"


def test_the_path_sweep_actually_visits_directories():
    """Wiring proof for the sweep below. Its whole reason to exist is that a
    directory NAME can be the leak, and a sweep quietly narrowed to files would
    still pass every other test in this file -- including the one that looks
    like it covers this."""
    swept = set(iter_paths())
    assert (ROOT / "bridge") in swept
    assert (ROOT / "bridge" / "scripts") in swept
    assert any(p.is_dir() for p in swept)


def test_no_concrete_mod_names_in_paths():
    """Contents were swept; names were not. A committed mod-named directory, or a
    documentation file named after a mod, is the same leak in a place the
    content sweep cannot see -- and mod-shaped directory names are exactly what
    this repository builds."""
    offenders = {}
    for path in iter_paths():
        rel = path.relative_to(ROOT)
        found = offending_tokens(str(rel))
        if found:
            offenders[str(rel)] = sorted(found)
    assert not offenders, f"mod names leaked into path names: {offenders}"


def test_the_sweep_covers_the_bridge_mods_own_sources():
    """The one mod that does live in this repository is written in Enforce
    Script and config.cpp. Those suffixes were outside the sweep when they were
    added, so the guard could not see the only mod sources it actually has."""
    scanned = {p.name for p in iter_text_files()}
    assert "config.cpp" in scanned
    assert any(name.endswith(".c") for name in scanned)
    # The protocol's own format and DayZ's config includes, swept before they
    # exist rather than after the first leak through them.
    assert {".json", ".hpp", ".h", ".xml"} <= TEXT_SUFFIXES


def test_the_guard_still_catches_a_real_mod_name():
    """Every token in these tests is assembled at runtime on purpose: spelled
    out as a literal it would be a real offender sitting in a scanned file, and
    the sweep above would (correctly) fail on the very tests that prove it
    works."""
    token = "@" + "StalkerZoneProtocol"
    assert offending_tokens(f'extra = ["D:/mods/{token}"]') == {token}
    # And in prose, not just in a path -- a mod name in a comment or a README
    # is exactly as much of a leak.
    assert offending_tokens(f"# depends on {token} being loaded first") == {token}


def test_the_guard_catches_lowercase_mod_names():
    """Real mods are not all capitalised. Both of these are installed in this
    machine's own modpack, and the first one's name is a word this project uses
    constantly -- which is precisely why an eye alone would not catch it."""
    for name in ("protocol", "zomberry"):
        token = "@" + name
        assert offending_tokens(f'extra = ["D:/mods/{token}"]') == {token}, token
        assert offending_tokens(f"the bridge talks to {token} over a file") == {token}, token
        # At the START of a line, which is where a decorator also lives. Only a
        # real decorator shape buys immunity -- a rule that merely skipped a
        # leading token would lose this one silently, and a mod name opening a
        # sentence or a markdown bullet is entirely ordinary prose.
        assert offending_tokens(f"{token} must be loaded before the mission") == {token}, token
        assert offending_tokens(f"* {token} -- the STALKER framework") == {token}, token


def test_the_guard_ignores_python_decorators():
    """Decorators must cost the code nothing: `property` was avoidable-by-name
    once, and an implementer really did work around it. Each name is checked
    BOTH ways -- ignored on a line of its own, caught in prose -- which is what
    proves the position rule is doing the work rather than an allow-list entry
    quietly covering the name."""
    for name in ("property", "staticmethod", "classmethod", "cached_property", "patch"):
        token = "@" + name
        assert offending_tokens(f"{token}\ndef f(): ...", is_python=True) == set(), token
        assert offending_tokens(f"    {token}\n    def f(self): ...", is_python=True) == set(), token
        assert offending_tokens(f"see {token} for details", is_python=True) == {token}, token

    # Dotted and called forms, with arguments and trailing comments.
    for line in ("@" + "pytest.mark.anyio", "@" + "functools.wraps(fn)",
                 "@" + "dataclass(frozen=True)  # immutable"):
        assert offending_tokens(f"{line}\ndef f(): ...", is_python=True) == set(), line


def test_a_decorators_arguments_and_comment_are_still_swept():
    """Immunity covers the decorator's NAME, not the rest of the line. A
    comment beside a decorator is exactly the prose the position rule was meant
    to protect, and an argument list is ordinary data."""
    mod = "@" + "zomberry"
    commented = "@" + f"property  # also loads {mod}"
    parametrised = "@" + f'pytest.mark.parametrize("mod", ["{mod}"])'
    assert offending_tokens(commented, is_python=True) == {mod}
    assert offending_tokens(parametrised, is_python=True) == {mod}


def test_decorator_immunity_applies_to_python_only():
    """Nothing is a decorator in Markdown, TOML or Enforce Script. Granting the
    immunity there hid a README line that was nothing but a mod name.

    KNOWN AND ACCEPTED LIMIT, so nobody reads it as an oversight: inside a
    .py file, a line consisting of nothing but a mod name IS still immune,
    docstrings and comments included -- a positional rule cannot tell that line
    from a decorator without parsing Python. It was weighed and kept: the
    alternative mechanisms cost more than the case is worth, and nothing in
    this repository writes such a line."""
    token = "@" + "protocol"
    assert offending_tokens(token, is_python=True) == set()  # a decorator, in Python
    assert offending_tokens(token) == {token}  # the same line in a README
    assert offending_tokens(f"{token}\n") == {token}
