from dayz_mcp.paths import (
    BLENDER_LEAF,
    BLENDER_VENDOR_DIR,
    find_blender,
    pick,
    steam_libraries,
)


def test_pick_returns_first_candidate_that_has_the_probe():
    seen = {"C:/a/probe.exe": False, "C:/b/probe.exe": True}
    got = pick(["C:/a", "C:/b"], "probe.exe", exists=lambda p: seen.get(p.replace("\\", "/"), False))
    assert got == "C:/b"


def test_pick_returns_none_when_nothing_matches():
    assert pick(["C:/a"], "probe.exe", exists=lambda p: False) is None


def test_pick_skips_empty_candidates():
    assert pick(["", None, "C:/b"], "probe.exe", exists=lambda p: "b" in p) == "C:/b"


def test_steam_libraries_parses_vdf_paths():
    vdf = '"libraryfolders" { "0" { "path" "C:\\\\Steam" } "1" { "path" "D:\\\\SteamLibrary" } }'
    libs = steam_libraries(registry=lambda: "C:/Steam", read_text=lambda p: vdf)
    joined = " ".join(libs)
    assert "SteamLibrary" in joined


# ------------------------------------------------------------------- Blender
# Not a Steam application here and with no registry key this server may rely
# on, so its discovery is its own: what the profile says, what the environment
# says, what is on PATH, and then the versioned install folders.


def test_blender_is_found_by_the_explicit_path_first(monkeypatch):
    monkeypatch.setenv("BLENDER_EXE", "C:/env/blender.exe")
    got = find_blender("C:/given/blender.exe",
                       exists=lambda p: True, which=lambda n: None)
    assert got == "C:/given/blender.exe"


def test_blender_falls_back_to_the_environment_then_to_the_path(monkeypatch):
    monkeypatch.setenv("BLENDER_EXE", "C:/env/blender.exe")
    assert find_blender("", exists=lambda p: "env" in p, which=lambda n: None) \
        == "C:/env/blender.exe"
    monkeypatch.delenv("BLENDER_EXE")
    assert find_blender("", exists=lambda p: "path" in p,
                        which=lambda n: "C:/path/blender.exe") == "C:/path/blender.exe"


def test_blender_returns_none_when_nothing_is_there(monkeypatch):
    monkeypatch.delenv("BLENDER_EXE", raising=False)
    assert find_blender("", exists=lambda p: False, which=lambda n: None) is None


def test_a_candidate_that_is_not_a_file_never_wins(monkeypatch):
    """The same rule the game and the tools follow: a leftover empty folder
    with the right name must not be chosen over a real install."""
    monkeypatch.setenv("BLENDER_EXE", "C:/gone/blender.exe")
    assert find_blender("", exists=lambda p: p == "C:/real/blender.exe",
                        which=lambda n: "C:/real/blender.exe") == "C:/real/blender.exe"


def test_installs_are_ordered_by_version_not_alphabetically(tmp_path, monkeypatch):
    """"Blender 10.0" sorts before "Blender 5.2" as text, and picking that way
    would choose the older install on any machine that reaches double digits."""
    vendor = tmp_path / BLENDER_VENDOR_DIR
    for name in ("Blender 4.2", "Blender 10.0", "Blender 5.2"):
        (vendor / name).mkdir(parents=True)
        (vendor / name / BLENDER_LEAF).write_text("stub", encoding="utf-8")
    monkeypatch.setenv("ProgramFiles", str(tmp_path))
    monkeypatch.delenv("ProgramFiles(x86)", raising=False)
    monkeypatch.delenv("BLENDER_EXE", raising=False)
    found = find_blender("", which=lambda n: None)
    assert found is not None
    assert "Blender 10.0" in found
