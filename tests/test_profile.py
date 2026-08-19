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


def test_wrong_shaped_project_section_is_rejected(tmp_path):
    bad = BASE.replace('[project]\nname = "my-mod"', 'project = "oops"')
    r = load_profile(write(tmp_path, bad))
    assert not r.ok
    assert "[project] must be a table" in r.error
    assert "dayz-mcp.toml" in r.hint or "[section]" in r.hint


def test_wrong_shaped_build_section_is_rejected(tmp_path):
    bad = """
build = "oops"

[project]
name = "my-mod"

[expect]
ready_line = "MyMod loaded"
max_warnings = 0
forbid = ["Bad type"]

[expect.counters]
items = 12
"""
    r = load_profile(write(tmp_path, bad))
    assert not r.ok
    assert "[build] must be a table" in r.error
    assert "[section]" in r.hint


def test_build_mods_as_scalar_is_rejected(tmp_path):
    bad = BASE.replace('mods = ["MyMod"]', 'mods = "MyMod"')
    r = load_profile(write(tmp_path, bad))
    assert not r.ok
    assert "build.mods must be a list" in r.error
    assert "dayz-mcp.toml" in r.hint or '["MyMod"]' in r.hint


def test_wrong_shaped_expect_section_is_rejected(tmp_path):
    bad = """
expect = "oops"

[project]
name = "my-mod"

[build]
mods = ["MyMod"]
"""
    r = load_profile(write(tmp_path, bad))
    assert not r.ok
    assert "[expect] must be a table" in r.error
    assert "[section]" in r.hint


def test_wrong_shaped_expect_counters_is_rejected(tmp_path):
    bad = """
[project]
name = "my-mod"

[build]
mods = ["MyMod"]

[expect]
ready_line = "MyMod loaded"
max_warnings = 0
forbid = ["Bad type"]
counters = "oops"
"""
    r = load_profile(write(tmp_path, bad))
    assert not r.ok
    assert "expect.counters" in r.error
    assert "table" in r.error


def test_mods_in_portable_file_is_rejected(tmp_path):
    bad = BASE + '\n[mods]\nrequired = ["@CF"]\n'
    r = load_profile(write(tmp_path, bad))
    assert not r.ok
    assert "[mods] found in" in r.error
    assert "dayz-mcp.local.toml" in r.hint


def test_project_in_local_file_is_rejected(tmp_path):
    local = """
    [project]
    name = "wrong"
    """
    r = load_profile(write(tmp_path, BASE, local))
    assert not r.ok
    assert "[project] found in" in r.error
    assert "dayz-mcp.toml" in r.hint


def test_build_in_local_file_is_rejected(tmp_path):
    local = """
    [build]
    mods = ["Other"]
    """
    r = load_profile(write(tmp_path, BASE, local))
    assert not r.ok
    assert "[build] found in" in r.error
    assert "dayz-mcp.toml" in r.hint


def test_expect_in_local_file_is_rejected(tmp_path):
    local = """
    [expect]
    ready_line = "wrong"
    """
    r = load_profile(write(tmp_path, BASE, local))
    assert not r.ok
    assert "[expect] found in" in r.error
    assert "dayz-mcp.toml" in r.hint


def test_wrong_shaped_machine_in_local_is_rejected(tmp_path):
    local = """
    machine = "oops"
    """
    r = load_profile(write(tmp_path, BASE, local))
    assert not r.ok
    assert "[machine] must be a table" in r.error
    assert "[section]" in r.hint


def test_wrong_shaped_mods_in_local_is_rejected(tmp_path):
    local = """
    mods = "oops"
    """
    r = load_profile(write(tmp_path, BASE, local))
    assert not r.ok
    assert "[mods] must be a table" in r.error
    assert "[section]" in r.hint


def test_machine_port_defaults_to_2302(tmp_path):
    p = load_profile(write(tmp_path, BASE)).data
    assert p.machine.port == 2302


def test_machine_port_parses(tmp_path):
    local = """
    [machine]
    port = 27016
    """
    p = load_profile(write(tmp_path, BASE, local)).data
    assert p.machine.port == 27016


def test_machine_port_non_numeric_is_rejected(tmp_path):
    local = """
    [machine]
    port = "twenty-seven thousand"
    """
    r = load_profile(write(tmp_path, BASE, local))
    assert not r.ok
    assert "machine.port" in r.error
    assert "integer" in r.error


def test_local_supplies_server_only_mods(tmp_path):
    local = """
    [mods]
    required = ["@CF"]
    server_only = ["@ServerOnlyMod"]
    """
    p = load_profile(write(tmp_path, BASE, local)).data
    assert p.mods.server_only == ["@ServerOnlyMod"]
    assert p.mods.required == ["@CF"]


def test_server_only_mods_default_to_empty(tmp_path):
    p = load_profile(write(tmp_path, BASE)).data
    assert p.mods.server_only == []


def test_malformed_error_regex_is_rejected(tmp_path):
    bad = """
[project]
name = "my-mod"

[build]
mods = ["MyMod"]

[expect]
ready_line = "MyMod loaded"
error_regex = ["("]
"""
    r = load_profile(write(tmp_path, bad))
    assert not r.ok
    assert "error_regex[0]" in r.error
    assert "valid regular expression" in r.error
    assert "escape the characters" in r.hint
