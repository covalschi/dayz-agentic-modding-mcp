from pathlib import Path
import textwrap
import tomllib
from dayz_mcp.packer import DEFAULT_EXCLUDE
from dayz_mcp.profile import load_profile, resolve_mod_dir, resolve_project_root

EXAMPLE_PROFILE = Path(__file__).resolve().parents[1] / "dayz-mcp.example.toml"

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


def test_mod_folder_without_config_cpp_is_rejected(tmp_path):
    """A folder without its own config.cpp packs into a pbo the engine will
    silently ignore -- load_profile must refuse before that ever happens,
    not just when the folder itself is missing."""
    d = write(tmp_path, BASE)
    (d / "MyMod" / "config.cpp").unlink()
    r = load_profile(d)
    assert not r.ok
    assert "MyMod/config.cpp" in r.error
    assert "config.cpp" in r.hint


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


def test_machine_server_defaults_to_empty(tmp_path):
    """Empty means "run the stand from machine.game", which is what every
    profile written before this key existed still means."""
    p = load_profile(write(tmp_path, BASE)).data
    assert p.machine.server == ""


def test_machine_server_parses(tmp_path):
    local = """
    [machine]
    server = "C:/Games/DayZServer"
    """
    p = load_profile(write(tmp_path, BASE, local)).data
    assert p.machine.server == "C:/Games/DayZServer"


def test_machine_config_defaults_to_serverDZ_cfg(tmp_path):
    p = load_profile(write(tmp_path, BASE)).data
    assert p.machine.config == "serverDZ.cfg"


def test_machine_config_parses(tmp_path):
    local = """
    [machine]
    config = "custom.cfg"
    """
    p = load_profile(write(tmp_path, BASE, local)).data
    assert p.machine.config == "custom.cfg"


def test_machine_config_non_string_is_rejected(tmp_path):
    local = """
    [machine]
    config = 123
    """
    r = load_profile(write(tmp_path, BASE, local))
    assert not r.ok
    assert "machine.config" in r.error
    assert "string" in r.error


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


# --- Requirement: build.exclude (what pack_one refuses to ship whole) is a
# profile-level key with a safe default ---


def test_build_exclude_defaults_to_git_and_blend(tmp_path):
    p = load_profile(write(tmp_path, BASE)).data
    assert p.build.exclude == list(DEFAULT_EXCLUDE)


def test_build_exclude_can_be_overridden(tmp_path):
    custom = BASE.replace('mods = ["MyMod"]', 'mods = ["MyMod"]\nexclude = [".svn"]')
    p = load_profile(write(tmp_path, custom)).data
    assert p.build.exclude == [".svn"]


def test_build_exclude_as_scalar_is_rejected(tmp_path):
    bad = BASE.replace('mods = ["MyMod"]', 'mods = ["MyMod"]\nexclude = ".git"')
    r = load_profile(write(tmp_path, bad))
    assert not r.ok
    assert "build.exclude must be a list" in r.error


# --- Requirement: build.sources lets a mod's source live somewhere other
# than <root>/<name> -- e.g. "." for a mod whose config.cpp sits at the
# repository root, next to dayz-mcp.toml itself ---


def test_sources_defaults_to_root_name_when_mod_is_absent(tmp_path):
    p = load_profile(write(tmp_path, BASE)).data
    assert p.build.sources == {}
    assert resolve_mod_dir(p.root, p.build.sources, "MyMod") == (tmp_path / "MyMod").resolve()


def test_sources_can_point_a_mod_at_the_repository_root(tmp_path):
    profile_text = BASE.replace('mods = ["MyMod"]', 'mods = ["MyMod"]\n\n[build.sources]\nMyMod = "."')
    d = write(tmp_path, profile_text)
    (d / "config.cpp").write_text("class CfgMods {};", encoding="utf-8")
    r = load_profile(d)
    assert r.ok, r.error
    assert r.data.build.sources == {"MyMod": "."}
    assert resolve_mod_dir(r.data.root, r.data.build.sources, "MyMod") == tmp_path.resolve()


def test_sources_missing_config_cpp_names_the_resolved_path(tmp_path):
    (tmp_path / "elsewhere").mkdir()
    profile_text = BASE.replace(
        'mods = ["MyMod"]', 'mods = ["MyMod"]\n\n[build.sources]\nMyMod = "elsewhere"'
    )
    d = write(tmp_path, profile_text)
    r = load_profile(d)
    assert not r.ok
    assert "config.cpp" in r.error
    assert "elsewhere" in r.hint


def test_sources_escaping_the_profile_directory_is_rejected(tmp_path):
    profile_text = BASE.replace(
        'mods = ["MyMod"]', 'mods = ["MyMod"]\n\n[build.sources]\nMyMod = "../outside"'
    )
    d = write(tmp_path, profile_text)
    r = load_profile(d)
    assert not r.ok
    assert "MyMod" in r.error
    assert "escapes" in r.error


def test_sources_as_scalar_is_rejected(tmp_path):
    bad = BASE.replace('mods = ["MyMod"]', 'mods = ["MyMod"]\nsources = "."')
    r = load_profile(write(tmp_path, bad))
    assert not r.ok
    assert "build.sources must be a table" in r.error


# --- Requirement: build.project_root declares, once, the directory the model
# tools must resolve prefixed paths against. It lives on the PORTABLE side
# because it describes the repository's layout, not the machine: the same
# declaration has to produce the same paths inside the artifact on every
# checkout, and a value that is not committed cannot do that. Today the same
# root is stated in three places at once -- the material paths inside the
# .blend, a modelling add-on's stored preference, and binarize's working
# directory -- and nothing makes them agree. ---


def test_project_root_is_absent_by_default(tmp_path):
    """A project with no models declares nothing and behaves exactly as before
    this key existed."""
    p = load_profile(write(tmp_path, BASE)).data
    assert p.build.project_root == ""
    assert resolve_project_root(p.root, p.build.project_root) is None


def test_project_root_resolves_against_the_profile_directory(tmp_path):
    (tmp_path / "staging").mkdir()
    text = BASE.replace('mods = ["MyMod"]', 'mods = ["MyMod"]\nproject_root = "staging"')
    r = load_profile(write(tmp_path, text))
    assert r.ok, r.error
    assert r.data.build.project_root == "staging"
    assert resolve_project_root(r.data.root, r.data.build.project_root) == (
        tmp_path / "staging").resolve()


def test_an_absolute_project_root_is_rejected(tmp_path):
    """An absolute path in a committed file is the defect this key exists to
    remove, not a way of expressing it: the modelling add-on on the machine
    this was designed against stores exactly such a path, and it points at a
    directory left over from an unrelated session."""
    (tmp_path / "staging").mkdir()
    absolute = str(tmp_path / "staging").replace("\\", "/")
    text = BASE.replace('mods = ["MyMod"]', f'mods = ["MyMod"]\nproject_root = "{absolute}"')
    r = load_profile(write(tmp_path, text))
    assert not r.ok
    assert "project_root" in r.error
    assert "relative" in r.hint


def test_a_project_root_that_is_not_there_is_rejected(tmp_path):
    """binarize started in a directory that does not exist fails in a way
    nobody can read. The profile answers first, by name."""
    text = BASE.replace('mods = ["MyMod"]', 'mods = ["MyMod"]\nproject_root = "staging"')
    r = load_profile(write(tmp_path, text))
    assert not r.ok
    assert "project_root" in r.error
    assert "staging" in r.error


def test_a_project_root_that_is_a_file_is_rejected(tmp_path):
    (tmp_path / "staging").write_text("", encoding="utf-8")
    text = BASE.replace('mods = ["MyMod"]', 'mods = ["MyMod"]\nproject_root = "staging"')
    r = load_profile(write(tmp_path, text))
    assert not r.ok
    assert "project_root" in r.error


def test_a_project_root_beside_the_repository_is_allowed_but_announced(tmp_path):
    """Unlike build.sources, this one may point outside the repository: a
    staging area that gathers the prefix trees of several mods legitimately
    sits beside them, and that is the layout on the machine this was measured
    against. It is announced, because a build then depends on a directory the
    repository does not own."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (tmp_path / "staging").mkdir()
    text = BASE.replace('mods = ["MyMod"]', 'mods = ["MyMod"]\nproject_root = "../staging"')
    r = load_profile(write(repo, text))
    assert r.ok, r.error
    assert resolve_project_root(r.data.root, r.data.build.project_root) == (
        tmp_path / "staging").resolve()
    assert any("project_root" in n for n in r.data.notes)


def test_project_root_as_a_non_string_is_rejected(tmp_path):
    text = BASE.replace('mods = ["MyMod"]', 'mods = ["MyMod"]\nproject_root = 3')
    r = load_profile(write(tmp_path, text))
    assert not r.ok
    assert "project_root must be a string" in r.error


def test_project_root_in_the_machine_file_is_refused_by_the_merge_rule(tmp_path):
    """It belongs to [build], and [build] is portable-only. Stated in the local
    file it would be one more uncommitted place for the root to drift -- which
    is the whole defect."""
    (tmp_path / "staging").mkdir()
    r = load_profile(write(tmp_path, BASE, '[build]\nproject_root = "staging"\n'))
    assert not r.ok
    assert "[build]" in r.error


# --- Requirement: build.stage opts into packing a filtered copy instead of
# refusing when excluded entries are present ---


def test_stage_defaults_to_false(tmp_path):
    p = load_profile(write(tmp_path, BASE)).data
    assert p.build.stage is False


def test_stage_can_be_enabled(tmp_path):
    on = BASE.replace('mods = ["MyMod"]', 'mods = ["MyMod"]\nstage = true')
    p = load_profile(write(tmp_path, on)).data
    assert p.build.stage is True


def test_stage_non_boolean_is_rejected(tmp_path):
    bad = BASE.replace('mods = ["MyMod"]', 'mods = ["MyMod"]\nstage = "yes"')
    r = load_profile(write(tmp_path, bad))
    assert not r.ok
    assert "build.stage must be a boolean" in r.error


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


def test_example_profile_never_states_a_false_exclude_default():
    """The README says to start from the example, and the example shipped a
    three-pattern `exclude` annotated "This is the default" while the real
    default has seven. Copying it verbatim silently narrowed the list -- the
    reviewer got four of the six files from an earlier leak straight back.

    Either form is fine: show the real default, or comment the key out and
    document it. A stated default that is wrong is not, so whichever form the
    example uses, every `exclude` line in it -- live or commented -- must
    parse to exactly DEFAULT_EXCLUDE.
    """
    text = EXAMPLE_PROFILE.read_text(encoding="utf-8")
    declarations = [
        stripped for stripped in (ln.lstrip("#").strip() for ln in text.splitlines())
        if stripped.startswith("exclude")
    ]
    assert declarations, "the example must show what build.exclude defaults to"
    for decl in declarations:
        assert tomllib.loads(decl)["exclude"] == list(DEFAULT_EXCLUDE), decl


def test_example_profile_is_a_loadable_profile(tmp_path):
    """It is offered as a starting point, so it must actually start."""
    (tmp_path / "MyMod").mkdir()
    (tmp_path / "MyMod" / "config.cpp").write_text("class CfgPatches {};", encoding="utf-8")
    (tmp_path / "dayz-mcp.toml").write_text(
        EXAMPLE_PROFILE.read_text(encoding="utf-8"), encoding="utf-8"
    )

    r = load_profile(tmp_path)

    assert r.ok, r.error
    # No exclude key of its own: the packer default applies, all seven patterns.
    assert r.data.build.exclude == list(DEFAULT_EXCLUDE)


def test_machine_blender_is_read_from_the_local_half(tmp_path):
    """The Blender executable is machine-specific like the game and the tools,
    and optional like the export step it serves."""
    local = """
    [machine]
    blender = "C:/Program Files/SomeVendor/blender.exe"
    """
    p = load_profile(write(tmp_path, BASE, local)).data
    assert p.machine.blender == "C:/Program Files/SomeVendor/blender.exe"


def test_machine_blender_defaults_to_empty(tmp_path):
    """Absent, discovery answers instead -- a project that never exports a
    model must not need the key at all."""
    assert load_profile(write(tmp_path, BASE)).data.machine.blender == ""


def test_client_file_patching_is_portable_and_off_by_default(tmp_path):
    p = load_profile(write(tmp_path, BASE)).data
    assert p.client.file_patching is False
    p = load_profile(write(tmp_path, BASE + '\n[client]\nfile_patching = true\n')).data
    assert p.client.file_patching is True


def test_client_file_patching_must_be_a_boolean(tmp_path):
    r = load_profile(write(tmp_path, BASE + '\n[client]\nfile_patching = "yes"\n'))
    assert not r.ok
    assert "file_patching" in r.error


def test_a_client_section_in_the_local_file_is_rejected(tmp_path):
    r = load_profile(write(tmp_path, BASE, "[client]\nfile_patching = true\n"))
    assert not r.ok
    assert "[client]" in r.error


def test_layout_classes_are_a_build_setting(tmp_path):
    main = BASE.replace('mods = ["MyMod"]', 'mods = ["MyMod"]\nlayout_classes = ["MyMapWidgetClass"]')
    p = load_profile(write(tmp_path, main)).data
    assert p.build.layout_classes == ["MyMapWidgetClass"]
    assert load_profile(write(tmp_path, BASE)).data.build.layout_classes == []


def test_machine_window_is_two_positive_integers(tmp_path):
    p = load_profile(write(tmp_path, BASE, "[machine]\nwindow = [3840, 1600]\n")).data
    assert p.machine.window == (3840, 1600)
    assert load_profile(write(tmp_path, BASE, "[machine]\n")).data.machine.window is None
    for bad in ("window = [3840]", 'window = "3840x1600"', "window = [0, 1600]", "window = [1920, 1080, 60]"):
        r = load_profile(write(tmp_path, BASE, f"[machine]\n{bad}\n"))
        assert not r.ok, bad
        assert "window" in r.error
