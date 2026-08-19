import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ALLOWED = {
    "@MyMod", "@Dep", "@SomeDependency", "@CF", "@B",
    # Generic examples and test fixtures
    "@Name", "@ModName", "@ServerOnlyMod",
    # The bridge mod this server packs and ships itself (bridge/, tools/bridge.py).
    # It is NOT a concrete project's mod: it belongs to the server, is built from
    # sources inside THIS repository, and every project that uses it uses the same
    # one. Naming it here therefore does not make the server fit one project --
    # which is the only thing this guard exists to prevent.
    "@DZMCP_Bridge",
}
# Deliberately `@` + an UPPERCASE letter: DayZ mod folder names are capitalised
# ("@CF", "@MyMod"), Python decorators are not ("@property", "@staticmethod",
# "@patch", "@dataclass"). The pattern used to accept either case, so every
# decorator in the repository was an "offender" that had to be allow-listed by
# name -- and one that had not been allow-listed yet distorted real code: an
# implementer avoided `@property` outright to keep this guard green. A guard
# that changes the code it guards is worse than no guard, so the cheap fix is
# to stop matching decorators at all rather than to keep listing them.
MOD_TOKEN = re.compile(r"@[A-Z][A-Za-z0-9_]{1,40}")
SKIP_DIRS = {".git", ".venv", "__pycache__", ".pytest_cache", ".dayz-mcp", "build", "dist"}
TEXT_SUFFIXES = {".py", ".toml", ".md", ".log", ".cfg", ".txt", ".yml", ".yaml"}


def offending_tokens(text: str) -> set[str]:
    """Every mod-name-looking token in `text` that is not allow-listed.

    THE definition of "offends this guard", so the two tests below that prove
    the guard still bites exercise the same code path as the sweep itself
    rather than a re-spelling of it.
    """
    return {m.group(0) for m in MOD_TOKEN.finditer(text)} - ALLOWED


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


def test_the_guard_still_catches_a_real_mod_name():
    """Narrowing the pattern must not blunt it. The token is assembled at
    runtime on purpose: spelled out as a literal it would be a real offender
    sitting in a scanned file, and the sweep above would (correctly) fail on
    this very test."""
    token = "@" + "StalkerZoneProtocol"
    assert offending_tokens(f'extra = ["D:/mods/{token}"]') == {token}
    # And in prose, not just in a path -- a mod name in a comment or a README
    # is exactly as much of a leak.
    assert offending_tokens(f"# depends on {token} being loaded first") == {token}


def test_the_guard_ignores_python_decorators():
    """The reason the pattern was narrowed: decorators are not mod names, and
    treating them as offenders costs real code. `@property` was never in the
    allow-list, so before this it could not be used anywhere in the repository."""
    for decorator in ("@property", "@staticmethod", "@classmethod", "@patch",
                      "@dataclass", "@pytest.fixture", "@functools.wraps"):
        assert offending_tokens(f"{decorator}\ndef f(): ...") == set(), decorator
