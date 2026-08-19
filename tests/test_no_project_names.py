import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ALLOWED = {
    "@MyMod", "@Dep", "@SomeDependency", "@CF", "@B",
    # Python decorators and metadata
    "@dataclass", "@pytest", "@functools", "@folder",
    # Generic examples and test fixtures
    "@Name", "@ModName", "@ServerOnlyMod",
}
MOD_TOKEN = re.compile(r"@[A-Za-z][A-Za-z0-9_]{1,40}")
SKIP_DIRS = {".git", ".venv", "__pycache__", ".pytest_cache", ".dayz-mcp", "build", "dist"}
TEXT_SUFFIXES = {".py", ".toml", ".md", ".log", ".cfg", ".txt", ".yml", ".yaml"}


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
        found = {m.group(0) for m in MOD_TOKEN.finditer(text)} - ALLOWED
        if found:
            offenders[str(path.relative_to(ROOT))] = sorted(found)
    assert not offenders, f"mod names leaked into the repository: {offenders}"
