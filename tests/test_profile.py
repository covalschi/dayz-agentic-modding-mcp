from pathlib import Path
import textwrap
from dayz_mcp.profile import load_profile

BASE = """
[project]
name = "my-mod"

[build]
mods = ["MyMod"]

[expect]
ready_line = "MyMod loaded"
max_warnings = 0
forbid = ["Bad type"]

[expect.counters]
items = 12
"""


def write(tmp_path: Path, main: str, local: str | None = None) -> Path:
    (tmp_path / "dayz-mcp.toml").write_text(textwrap.dedent(main), encoding="utf-8")
    if local is not None:
        (tmp_path / "dayz-mcp.local.toml").write_text(textwrap.dedent(local), encoding="utf-8")
    (tmp_path / "MyMod").mkdir(exist_ok=True)
    (tmp_path / "MyMod" / "config.cpp").write_text("", encoding="utf-8")
    return tmp_path


def test_loads_from_directory(tmp_path):
    r = load_profile(write(tmp_path, BASE))
    assert r.ok, r.error
    p = r.data
    assert p.name == "my-mod"
    assert p.build.mods == ["MyMod"]
    assert p.expect.counters == {"items": 12}


def test_mod_directories_are_derived_from_one_declaration(tmp_path):
    p = load_profile(write(tmp_path, BASE)).data
    assert p.own_mod_dirs == ["@MyMod"]


def test_missing_source_directory_is_rejected(tmp_path):
    d = write(tmp_path, BASE)
    (d / "MyMod" / "config.cpp").unlink()
    (d / "MyMod").rmdir()
    r = load_profile(d)
    assert not r.ok
    assert "MyMod" in r.error


def test_missing_profile_is_a_clear_failure(tmp_path):
    r = load_profile(tmp_path)
    assert not r.ok
    assert "dayz-mcp.toml" in r.hint


def test_local_supplies_machine_paths_and_extra_mods(tmp_path):
    local = """
    [machine]
    game = "C:/Games/DayZ"
    stand_root = "D:/stand"

    [mods]
    required = ["@CF"]
    extra = ["D:/other/@Dep"]
    """
    p = load_profile(write(tmp_path, BASE, local)).data
    assert p.machine.game == "C:/Games/DayZ"
    assert p.mods.required == ["@CF"]
    assert p.mods.extra == ["D:/other/@Dep"]


def test_machine_paths_in_portable_profile_are_rejected(tmp_path):
    bad = BASE + '\n[machine]\ngame = "C:/Games/DayZ"\n'
    r = load_profile(write(tmp_path, bad))
    assert not r.ok
    assert "machine" in r.error


def test_declared_pre_script_must_exist(tmp_path):
    bad = BASE.replace('mods = ["MyMod"]', 'mods = ["MyMod"]\npre_script = "tools/gen.ps1"')
    r = load_profile(write(tmp_path, bad))
    assert not r.ok
    assert "gen.ps1" in r.error


def test_empty_ready_line_is_a_note_not_a_failure(tmp_path):
    no_ready = BASE.replace('ready_line = "MyMod loaded"', 'ready_line = ""')
    r = load_profile(write(tmp_path, no_ready))
    assert r.ok
    assert any("ready_line" in n for n in r.data.notes)


def test_non_integer_counter_is_rejected(tmp_path):
    bad = BASE.replace("items = 12", 'items = "twelve"')
    r = load_profile(write(tmp_path, bad))
    assert not r.ok
    assert "items" in r.error


def test_no_mods_declared_is_rejected(tmp_path):
    bad = BASE.replace('mods = ["MyMod"]', "mods = []")
    r = load_profile(write(tmp_path, bad))
    assert not r.ok
    assert "build.mods" in r.error
