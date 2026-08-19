from dayz_mcp.paths import pick, steam_libraries


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
