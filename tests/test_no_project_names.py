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
# one of them collides head-on with this project's vocabulary -- exactly the
# kind of leak that would otherwise read as an ordinary word and slip through.
MOD_TOKEN = re.compile(r"@[A-Za-z][A-Za-z0-9_]{1,40}")

# Decorator immunity comes from POSITION, not from spelling: a Python decorator
# is a whole line of its own (indentation and a trailing comment allowed), while
# a mod name in prose, in a path or in a TOML value never is. This replaced an
# earlier attempt at immunity-by-spelling (requiring an uppercase initial),
# which bought the same immunity at the cost of every lowercase mod name in
# existence.
#
# Known and accepted residue: a line consisting of nothing but a mod name is
# read as a decorator and missed. Nothing in this repository writes one, and
# the alternative -- parsing Python for real -- is not worth it here.
DECORATOR_LINE = re.compile(r"^\s*@[A-Za-z_][A-Za-z0-9_.]*(\(.*)?\s*(#.*)?$")

SKIP_DIRS = {".git", ".venv", "__pycache__", ".pytest_cache", ".dayz-mcp", "build", "dist"}
# ".c" and ".cpp" are here because the bridge mod's own sources are Enforce
# Script and config.cpp: without them the sweep cannot see the one mod that
# actually lives in this repository, nor anything a future task adds beside it.
TEXT_SUFFIXES = {".py", ".toml", ".md", ".log", ".cfg", ".txt", ".yml", ".yaml", ".c", ".cpp"}


def offending_tokens(text: str) -> set[str]:
    """Every mod-name-looking token in `text` that is not allow-listed.

    THE definition of "offends this guard", so the tests below that prove the
    guard still bites exercise the same code path as the sweep itself rather
    than a re-spelling of it.
    """
    found: set[str] = set()
    for line in text.splitlines():
        scan = line
        decorator = DECORATOR_LINE.match(line)
        if decorator:
            scan = line[decorator.end():]
        found |= {m.group(0) for m in MOD_TOKEN.finditer(scan)}
    return found - ALLOWED


def iter_text_files():
    for p in ROOT.rglob("*"):
        if p.is_dir() or p.suffix.lower() not in TEXT_SUFFIXES:
            continue
        if any(part in SKIP_DIRS for part in p.parts):
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
        found = offending_tokens(text)
        if found:
            offenders[str(path.relative_to(ROOT))] = sorted(found)
    assert not offenders, f"mod names leaked into the repository: {offenders}"


def test_the_sweep_covers_the_bridge_mods_own_sources():
    """The one mod that does live in this repository is written in Enforce
    Script and config.cpp. Those suffixes were outside the sweep when they were
    added, so the guard could not see the only mod sources it actually has."""
    scanned = {p.name for p in iter_text_files()}
    assert "config.cpp" in scanned
    assert any(name.endswith(".c") for name in scanned)


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
        # At the START of a line, which is where a decorator also lives. Only
        # the WHOLE line being a decorator buys immunity -- a rule that merely
        # skipped a leading token would lose this one silently, and a mod name
        # opening a sentence or a markdown bullet is entirely ordinary prose.
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
        assert offending_tokens(f"{token}\ndef f(): ...") == set(), token
        assert offending_tokens(f"    {token}\n    def f(self): ...") == set(), token
        assert offending_tokens(f"see {token} for details") == {token}, token

    # Dotted and called forms, with arguments and trailing comments.
    for line in ("@" + "pytest.mark.anyio", "@" + "functools.wraps(fn)",
                 "@" + "dataclass(frozen=True)  # immutable"):
        assert offending_tokens(f"{line}\ndef f(): ...") == set(), line
