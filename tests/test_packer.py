import os
import time
from pathlib import Path
from unittest.mock import MagicMock

from dayz_mcp.packer import (
    DEFAULT_EXCLUDE, PackResult, config_syntax_cmd, filebank_cmd, find_excluded, find_keys,
    newest_source_mtime, pack_all, pack_one, sign_cmd,
)


def test_filebank_command_shape():
    cmd = filebank_cmd(Path("C:/T/FileBank.exe"), "MyMod", Path("C:/r/MyMod"), Path("C:/r/@MyMod/addons"))
    assert cmd[0].endswith("FileBank.exe")
    assert "-dst" in cmd
    assert "prefix=MyMod" in " ".join(cmd)
    assert cmd[-1].endswith("MyMod")


def test_sign_command_shape():
    cmd = sign_cmd(Path("C:/T/DSSignFile.exe"), Path("C:/r/keys/k.biprivatekey"), Path("C:/r/a.pbo"))
    assert cmd[0].endswith("DSSignFile.exe")
    assert cmd[1].endswith(".biprivatekey")
    assert cmd[2].endswith(".pbo")


def test_find_keys_picks_private_and_public(tmp_path):
    (tmp_path / "a.biprivatekey").write_text("x", encoding="utf-8")
    (tmp_path / "a.bikey").write_text("y", encoding="utf-8")
    priv, pub = find_keys(tmp_path)
    assert priv.name.endswith(".biprivatekey")
    assert pub.name.endswith(".bikey")


def test_find_keys_tolerates_a_missing_directory(tmp_path):
    priv, pub = find_keys(tmp_path / "nope")
    assert priv is None and pub is None


def test_find_keys_pairs_by_stem(tmp_path):
    """Test that public key is matched by stem, not just by any .bikey file."""
    (tmp_path / "a.biprivatekey").write_text("priv_a", encoding="utf-8")
    (tmp_path / "a.bikey").write_text("pub_a", encoding="utf-8")
    priv, pub = find_keys(tmp_path)
    assert priv.stem == "a"
    assert pub.stem == "a"


def test_find_keys_with_multiple_key_pairs(tmp_path):
    """Test that first sorted private key is selected when multiple exist."""
    (tmp_path / "alpha.biprivatekey").write_text("priv_alpha", encoding="utf-8")
    (tmp_path / "alpha.bikey").write_text("pub_alpha", encoding="utf-8")
    (tmp_path / "bravo.biprivatekey").write_text("priv_bravo", encoding="utf-8")
    (tmp_path / "bravo.bikey").write_text("pub_bravo", encoding="utf-8")
    priv, pub = find_keys(tmp_path)
    # Should pick the first (sorted)
    assert priv.stem == "alpha"
    assert pub.stem == "alpha"


def test_find_keys_private_without_matching_public(tmp_path):
    """Test that private key is returned even if no matching public key exists."""
    (tmp_path / "a.biprivatekey").write_text("priv_a", encoding="utf-8")
    (tmp_path / "b.bikey").write_text("pub_b", encoding="utf-8")
    priv, pub = find_keys(tmp_path)
    assert priv is not None
    assert priv.stem == "a"
    assert pub is None


def test_pack_result_has_note_field():
    """Test that PackResult includes a note field for non-fatal remarks."""
    r = PackResult(name="MyMod", signed=False, note="unsigned by design")
    assert r.note == "unsigned by design"


def test_pack_one_happy_path(tmp_path, monkeypatch):
    """Test successful packing: fake FileBank creates fresh PBO."""
    root = tmp_path / "root"
    root.mkdir()
    (root / "MyMod").mkdir()
    (root / "MyMod" / "config.cpp").write_text("class CfgMods {};", encoding="utf-8")

    # Create stub FileBank.exe
    tools = tmp_path / "tools"
    filebank_dir = tools / "Bin" / "PboUtils"
    filebank_dir.mkdir(parents=True, exist_ok=True)
    filebank_exe = filebank_dir / "FileBank.exe"
    filebank_exe.write_text("stub", encoding="utf-8")

    log_path = root / "build.log"

    # Mock run_blocking to simulate FileBank creating the PBO
    def fake_run_blocking(cmd, cwd, log_path_arg, timeout=None):
        # Simulate FileBank behavior: create the output PBO
        out_dir = root / "@MyMod" / "addons"
        out_dir.mkdir(parents=True, exist_ok=True)
        pbo = out_dir / "MyMod.pbo"
        pbo.write_text("fake pbo data", encoding="utf-8")
        return 0, "FileBank success"

    monkeypatch.setattr("dayz_mcp.packer.run_blocking", fake_run_blocking)

    result = pack_one("MyMod", root, tools, log_path)
    assert result.name == "MyMod"
    assert result.error == ""
    assert result.signed is False  # No key configured
    assert result.size > 0
    assert result.pbo != ""


def test_pack_one_filebank_missing(tmp_path, monkeypatch):
    """Test error when FileBank executable is not found."""
    root = tmp_path / "root"
    root.mkdir()
    (root / "MyMod").mkdir()

    tools = tmp_path / "empty_tools"  # No FileBank here
    tools.mkdir()

    log_path = root / "build.log"

    result = pack_one("MyMod", root, tools, log_path)
    assert result.error != ""
    assert "FileBank" in result.error


def test_pack_one_stale_pbo(tmp_path, monkeypatch):
    """Test detection of stale PBO (older than sources)."""
    root = tmp_path / "root"
    root.mkdir()
    src_dir = root / "MyMod"
    src_dir.mkdir()
    src_file = src_dir / "config.cpp"
    src_file.write_text("class CfgMods {};", encoding="utf-8")

    # Make source file modification time in the past
    old_mtime = time.time() - 1000
    os.utime(src_file, (old_mtime, old_mtime))

    # Create stub FileBank.exe
    tools = tmp_path / "tools"
    filebank_dir = tools / "Bin" / "PboUtils"
    filebank_dir.mkdir(parents=True, exist_ok=True)
    (filebank_dir / "FileBank.exe").write_text("stub", encoding="utf-8")

    log_path = root / "build.log"

    # Mock run_blocking to create a stale PBO (even older than source)
    def fake_run_blocking(cmd, cwd, log_path_arg, timeout=None):
        out_dir = root / "@MyMod" / "addons"
        out_dir.mkdir(parents=True, exist_ok=True)
        pbo = out_dir / "MyMod.pbo"
        pbo.write_text("stale pbo", encoding="utf-8")
        # Back-date the PBO to be older than the source
        stale_mtime = old_mtime - 100
        os.utime(pbo, (stale_mtime, stale_mtime))
        return 0, "FileBank success"

    monkeypatch.setattr("dayz_mcp.packer.run_blocking", fake_run_blocking)

    result = pack_one("MyMod", root, tools, log_path)
    assert result.error != ""
    assert "stale pbo" in result.error


def test_pack_one_pbo_not_produced(tmp_path, monkeypatch):
    """Test error when FileBank returns success but PBO is not created."""
    root = tmp_path / "root"
    root.mkdir()
    (root / "MyMod").mkdir()
    (root / "MyMod" / "config.cpp").write_text("class CfgMods {};", encoding="utf-8")

    # Create stub FileBank.exe
    tools = tmp_path / "tools"
    filebank_dir = tools / "Bin" / "PboUtils"
    filebank_dir.mkdir(parents=True, exist_ok=True)
    (filebank_dir / "FileBank.exe").write_text("stub", encoding="utf-8")

    log_path = root / "build.log"

    # Mock run_blocking to succeed but not create the PBO
    def fake_run_blocking(cmd, cwd, log_path_arg, timeout=None):
        return 0, "FileBank success (but we didn't actually create it)"

    monkeypatch.setattr("dayz_mcp.packer.run_blocking", fake_run_blocking)

    result = pack_one("MyMod", root, tools, log_path)
    assert result.error != ""
    assert "was not produced" in result.error


def test_pack_one_filebank_failure(tmp_path, monkeypatch):
    """Test error when FileBank returns non-zero exit code."""
    root = tmp_path / "root"
    root.mkdir()
    (root / "MyMod").mkdir()

    # Create stub FileBank.exe
    tools = tmp_path / "tools"
    filebank_dir = tools / "Bin" / "PboUtils"
    filebank_dir.mkdir(parents=True, exist_ok=True)
    (filebank_dir / "FileBank.exe").write_text("stub", encoding="utf-8")

    log_path = root / "build.log"

    # Mock run_blocking to fail
    def fake_run_blocking(cmd, cwd, log_path_arg, timeout=None):
        return 1, "FileBank error: bad config"

    monkeypatch.setattr("dayz_mcp.packer.run_blocking", fake_run_blocking)

    result = pack_one("MyMod", root, tools, log_path)
    assert result.error != ""
    assert "FileBank exit 1" in result.error


def test_pack_all_multiple_mods(tmp_path, monkeypatch):
    """Test pack_all with two mods, where the first fails."""
    root = tmp_path / "root"
    root.mkdir()

    # Create two mod source directories
    (root / "Mod1").mkdir()
    (root / "Mod1" / "config.cpp").write_text("class CfgMods {};", encoding="utf-8")
    (root / "Mod2").mkdir()
    (root / "Mod2" / "config.cpp").write_text("class CfgMods {};", encoding="utf-8")

    tools = tmp_path / "empty_tools"
    tools.mkdir()

    log_dir = root / "logs"

    # Use real run_blocking (which will fail because tools dir is empty)
    # This tests that pack_all returns all results even when one fails

    results = pack_all(["Mod1", "Mod2"], root, tools, log_dir)

    assert len(results) == 2
    assert results[0].name == "Mod1"
    assert results[1].name == "Mod2"
    # Both should have errors because FileBank is not found
    assert results[0].error != ""
    assert results[1].error != ""


def test_pack_one_with_keys_creates_public_key_copy(tmp_path, monkeypatch):
    """Test that public key is copied to @ModName/keys when it exists."""
    root = tmp_path / "root"
    root.mkdir()
    src_dir = root / "MyMod"
    src_dir.mkdir()
    (src_dir / "config.cpp").write_text("class CfgMods {};", encoding="utf-8")

    # Create a key pair
    keys_dir = root / "keys"
    keys_dir.mkdir()
    (keys_dir / "test.biprivatekey").write_text("private_key_content", encoding="utf-8")
    (keys_dir / "test.bikey").write_text("public_key_content", encoding="utf-8")

    # Create stub FileBank.exe
    tools = tmp_path / "tools"
    filebank_dir = tools / "Bin" / "PboUtils"
    filebank_dir.mkdir(parents=True, exist_ok=True)
    (filebank_dir / "FileBank.exe").write_text("stub", encoding="utf-8")

    log_path = root / "build.log"

    # Mock run_blocking to create the PBO
    def fake_run_blocking(cmd, cwd, log_path_arg, timeout=None):
        out_dir = root / "@MyMod" / "addons"
        out_dir.mkdir(parents=True, exist_ok=True)
        pbo = out_dir / "MyMod.pbo"
        pbo.write_text("fake pbo data", encoding="utf-8")
        return 0, "FileBank success"

    monkeypatch.setattr("dayz_mcp.packer.run_blocking", fake_run_blocking)

    result = pack_one("MyMod", root, tools, log_path)

    # Check that public key was copied
    public_key_dest = root / "@MyMod" / "keys" / "test.bikey"
    assert public_key_dest.exists()
    assert public_key_dest.read_text(encoding="utf-8") == "public_key_content"


def test_pack_one_with_private_key_but_no_signer(tmp_path, monkeypatch):
    """Test that note is set when private key exists but signer is missing."""
    root = tmp_path / "root"
    root.mkdir()
    src_dir = root / "MyMod"
    src_dir.mkdir()
    (src_dir / "config.cpp").write_text("class CfgMods {};", encoding="utf-8")

    # Create a private key but not the signer executable
    keys_dir = root / "keys"
    keys_dir.mkdir()
    (keys_dir / "test.biprivatekey").write_text("private_key_content", encoding="utf-8")

    # Create stub FileBank.exe but NOT DSSignFile.exe
    tools = tmp_path / "tools"
    filebank_dir = tools / "Bin" / "PboUtils"
    filebank_dir.mkdir(parents=True, exist_ok=True)
    (filebank_dir / "FileBank.exe").write_text("stub", encoding="utf-8")

    log_path = root / "build.log"

    # Mock run_blocking to create the PBO
    def fake_run_blocking(cmd, cwd, log_path_arg, timeout=None):
        out_dir = root / "@MyMod" / "addons"
        out_dir.mkdir(parents=True, exist_ok=True)
        pbo = out_dir / "MyMod.pbo"
        pbo.write_text("fake pbo data", encoding="utf-8")
        return 0, "FileBank success"

    monkeypatch.setattr("dayz_mcp.packer.run_blocking", fake_run_blocking)

    result = pack_one("MyMod", root, tools, log_path)

    # Should have no error, but a note about missing signer
    assert result.error == ""
    assert result.signed is False
    assert "signer" in result.note.lower()


# --- Requirement: a CfgConvert syntax gate before FileBank (FileBank does not
# parse config.cpp at all; a syntax error there survives packing and only
# surfaces after a multi-minute server boot) ---


def test_config_syntax_command_shape():
    cmd = config_syntax_cmd(
        Path("C:/T/CfgConvert.exe"), Path("C:/r/MyMod/config.cpp"), Path("C:/tmp/out.bin")
    )
    assert cmd[0].endswith("CfgConvert.exe")
    assert "-bin" in cmd
    assert "-dst" in cmd
    assert cmd[-1].endswith("config.cpp")


def _make_cfgconvert_stub(tools: Path) -> Path:
    cfgconvert_dir = tools / "Bin" / "CfgConvert"
    cfgconvert_dir.mkdir(parents=True, exist_ok=True)
    exe = cfgconvert_dir / "CfgConvert.exe"
    exe.write_text("stub", encoding="utf-8")
    return exe


def test_pack_one_rejects_a_config_cpp_syntax_error_before_filebank_runs(tmp_path, monkeypatch):
    """FileBank does not parse config.cpp, so a syntax error there would
    otherwise survive packing silently. CfgConvert must catch it and refuse to
    pack, naming the file and line, before FileBank is ever invoked. The exit
    code alone is the gate -- this fake mirrors CfgConvert's real diagnostic
    text on an actual syntax error (confirmed against the real binary on a
    broken config.cpp: exit 1, then a line naming the file and line number,
    then "Error reading config file '<path>'"), not a stand-in string that
    happens to contain the word "error"."""
    root = tmp_path / "root"
    root.mkdir()
    src_dir = root / "MyMod"
    src_dir.mkdir()
    (src_dir / "config.cpp").write_text("class CfgMods { garbage", encoding="utf-8")

    tools = tmp_path / "tools"
    filebank_dir = tools / "Bin" / "PboUtils"
    filebank_dir.mkdir(parents=True, exist_ok=True)
    (filebank_dir / "FileBank.exe").write_text("stub", encoding="utf-8")
    _make_cfgconvert_stub(tools)

    log_path = root / "build.log"
    calls = []
    diagnostic = (
        "File config.cpp, line 40: /CfgMods/MyMod/defs/: Missing '}'\n"
        "Error reading config file 'config.cpp'\n"
    )

    def fake_run_blocking(cmd, cwd, log_path_arg, timeout=None):
        calls.append(cmd)
        return 1, diagnostic

    monkeypatch.setattr("dayz_mcp.packer.run_blocking", fake_run_blocking)

    result = pack_one("MyMod", root, tools, log_path)

    assert result.error != ""
    assert "config.cpp" in result.error
    assert "line 40" in result.error
    # FileBank must never have been invoked: exactly the one CfgConvert call.
    assert len(calls) == 1
    assert calls[0][0].endswith("CfgConvert.exe")


def test_pack_one_accepts_a_cfgconvert_success_that_mentions_the_word_error(tmp_path, monkeypatch):
    """CfgConvert's own success message can legitimately contain the word
    "error" -- e.g. "Config : 0 errors, 0 warnings", confirmed against the
    real binary. The exit code alone must gate packing; a substring search of
    the output would refuse a perfectly valid config.cpp on a message like
    this one."""
    root = tmp_path / "root"
    root.mkdir()
    src_dir = root / "MyMod"
    src_dir.mkdir()
    (src_dir / "config.cpp").write_text("class CfgMods {};", encoding="utf-8")

    tools = tmp_path / "tools"
    filebank_dir = tools / "Bin" / "PboUtils"
    filebank_dir.mkdir(parents=True, exist_ok=True)
    (filebank_dir / "FileBank.exe").write_text("stub", encoding="utf-8")
    _make_cfgconvert_stub(tools)

    log_path = root / "build.log"

    def fake_run_blocking(cmd, cwd, log_path_arg, timeout=None):
        if str(cmd[0]).endswith("CfgConvert.exe"):
            return 0, "Config : 0 errors, 0 warnings"
        out_dir = root / "@MyMod" / "addons"
        out_dir.mkdir(parents=True, exist_ok=True)
        pbo = out_dir / "MyMod.pbo"
        pbo.write_text("fake pbo data", encoding="utf-8")
        return 0, "FileBank success"

    monkeypatch.setattr("dayz_mcp.packer.run_blocking", fake_run_blocking)

    result = pack_one("MyMod", root, tools, log_path)
    assert result.error == ""
    assert result.pbo != ""


def test_pack_one_proceeds_when_cfgconvert_is_not_available(tmp_path, monkeypatch):
    """When DayZ Tools does not have CfgConvert (e.g. a partial install), the
    gate must not block packing outright -- FileBank is still the thing doing
    the real work, and this is a bonus authoritative check, not a hard
    dependency."""
    root = tmp_path / "root"
    root.mkdir()
    src_dir = root / "MyMod"
    src_dir.mkdir()
    (src_dir / "config.cpp").write_text("class CfgMods {};", encoding="utf-8")

    tools = tmp_path / "tools"
    filebank_dir = tools / "Bin" / "PboUtils"
    filebank_dir.mkdir(parents=True, exist_ok=True)
    (filebank_dir / "FileBank.exe").write_text("stub", encoding="utf-8")
    # No CfgConvert.exe anywhere under tools.

    log_path = root / "build.log"

    def fake_run_blocking(cmd, cwd, log_path_arg, timeout=None):
        out_dir = root / "@MyMod" / "addons"
        out_dir.mkdir(parents=True, exist_ok=True)
        pbo = out_dir / "MyMod.pbo"
        pbo.write_text("fake pbo data", encoding="utf-8")
        return 0, "FileBank success"

    monkeypatch.setattr("dayz_mcp.packer.run_blocking", fake_run_blocking)

    result = pack_one("MyMod", root, tools, log_path)
    assert result.error == ""
    assert result.pbo != ""


def test_pack_one_runs_cfgconvert_with_cwd_in_the_configs_own_folder(tmp_path, monkeypatch):
    """Relative #include directives in config.cpp only resolve if CfgConvert
    runs with its working directory set to config.cpp's own folder."""
    root = tmp_path / "root"
    root.mkdir()
    src_dir = root / "MyMod"
    src_dir.mkdir()
    (src_dir / "config.cpp").write_text("class CfgMods {};", encoding="utf-8")

    tools = tmp_path / "tools"
    filebank_dir = tools / "Bin" / "PboUtils"
    filebank_dir.mkdir(parents=True, exist_ok=True)
    (filebank_dir / "FileBank.exe").write_text("stub", encoding="utf-8")
    _make_cfgconvert_stub(tools)

    log_path = root / "build.log"
    captured_cwd = []

    def fake_run_blocking(cmd, cwd, log_path_arg, timeout=None):
        captured_cwd.append(Path(cwd))
        if str(cmd[0]).endswith("CfgConvert.exe"):
            return 0, "ok"
        out_dir = root / "@MyMod" / "addons"
        out_dir.mkdir(parents=True, exist_ok=True)
        pbo = out_dir / "MyMod.pbo"
        pbo.write_text("fake pbo data", encoding="utf-8")
        return 0, "FileBank success"

    monkeypatch.setattr("dayz_mcp.packer.run_blocking", fake_run_blocking)

    result = pack_one("MyMod", root, tools, log_path)
    assert result.error == ""
    assert captured_cwd[0] == src_dir


def test_pack_one_soft_degrades_when_cfgconvert_cannot_be_run(tmp_path, monkeypatch):
    """An existing but unrunnable CfgConvert.exe (wrong architecture, broken
    permissions, a placeholder) makes run_blocking return its own "cannot
    start" code, 127. That means the gate itself could not run -- a broken
    toolchain, not a config problem -- and must not block packing, the same
    soft-degrade already applied when the signer executable is missing."""
    root = tmp_path / "root"
    root.mkdir()
    src_dir = root / "MyMod"
    src_dir.mkdir()
    (src_dir / "config.cpp").write_text("class CfgMods {};", encoding="utf-8")

    tools = tmp_path / "tools"
    filebank_dir = tools / "Bin" / "PboUtils"
    filebank_dir.mkdir(parents=True, exist_ok=True)
    (filebank_dir / "FileBank.exe").write_text("stub", encoding="utf-8")
    cfgconvert = _make_cfgconvert_stub(tools)

    log_path = root / "build.log"

    def fake_run_blocking(cmd, cwd, log_path_arg, timeout=None):
        if str(cmd[0]).endswith("CfgConvert.exe"):
            return 127, "[dayz-mcp] cannot start: [WinError 216] not a valid Win32 application"
        out_dir = root / "@MyMod" / "addons"
        out_dir.mkdir(parents=True, exist_ok=True)
        pbo = out_dir / "MyMod.pbo"
        pbo.write_text("fake pbo data", encoding="utf-8")
        return 0, "FileBank success"

    monkeypatch.setattr("dayz_mcp.packer.run_blocking", fake_run_blocking)

    result = pack_one("MyMod", root, tools, log_path)
    assert result.error == ""
    assert result.pbo != ""
    assert str(cfgconvert) in result.note


def test_pack_one_deletes_the_temporary_cfgconvert_output(tmp_path, monkeypatch):
    """The compiled .bin CfgConvert writes as part of the syntax check is
    only useful as an on/off signal here -- nothing downstream reads it, and
    it must not linger next to the real build artifacts."""
    root = tmp_path / "root"
    root.mkdir()
    src_dir = root / "MyMod"
    src_dir.mkdir()
    (src_dir / "config.cpp").write_text("class CfgMods {};", encoding="utf-8")

    tools = tmp_path / "tools"
    filebank_dir = tools / "Bin" / "PboUtils"
    filebank_dir.mkdir(parents=True, exist_ok=True)
    (filebank_dir / "FileBank.exe").write_text("stub", encoding="utf-8")
    _make_cfgconvert_stub(tools)

    log_path = root / "build.log"
    written_bin = {}

    def fake_run_blocking(cmd, cwd, log_path_arg, timeout=None):
        if str(cmd[0]).endswith("CfgConvert.exe"):
            out_path = Path(cmd[cmd.index("-dst") + 1])
            out_path.write_bytes(b"fake compiled config")
            written_bin["path"] = out_path
            return 0, "Config : 0 errors, 0 warnings"
        out_dir = root / "@MyMod" / "addons"
        out_dir.mkdir(parents=True, exist_ok=True)
        pbo = out_dir / "MyMod.pbo"
        pbo.write_text("fake pbo data", encoding="utf-8")
        return 0, "FileBank success"

    monkeypatch.setattr("dayz_mcp.packer.run_blocking", fake_run_blocking)

    result = pack_one("MyMod", root, tools, log_path)
    assert result.error == ""
    assert "path" in written_bin
    assert not written_bin["path"].exists()


# --- Requirement: refuse to pack instead of silently including .git/.blend/etc ---


def test_find_excluded_reports_a_nested_git_directory(tmp_path):
    (tmp_path / "config.cpp").write_text("", encoding="utf-8")
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    found = find_excluded(tmp_path, DEFAULT_EXCLUDE)
    assert found == [".git"]


def test_find_excluded_matches_blend_globs(tmp_path):
    """Exercises the FILE branch of find_excluded (a real .blend on disk),
    which test_find_excluded_reports_a_nested_git_directory's directory-only
    fixture never touches -- a version of this test that only asserted raw
    fnmatch.fnmatch() behaviour, without calling find_excluded at all, would
    pass regardless of whether that branch worked."""
    (tmp_path / "config.cpp").write_text("", encoding="utf-8")
    (tmp_path / "scene.blend").write_bytes(b"fake blend data")
    (tmp_path / "scene.blend1").write_bytes(b"fake blend backup")
    (tmp_path / "notes.txt").write_text("keep me", encoding="utf-8")

    found = find_excluded(tmp_path, DEFAULT_EXCLUDE)

    assert sorted(found) == ["scene.blend", "scene.blend1"]


def test_pack_one_refuses_when_git_is_present(tmp_path):
    """FileBank packs the source directory whole. A .git folder inside it
    would end up in the published pbo -- refuse instead of shipping it."""
    root = tmp_path / "root"
    root.mkdir()
    src_dir = root / "MyMod"
    src_dir.mkdir()
    (src_dir / "config.cpp").write_text("class CfgMods {};", encoding="utf-8")
    (src_dir / ".git").mkdir()
    (src_dir / ".git" / "config").write_text("", encoding="utf-8")

    log_path = root / "build.log"
    # tools deliberately does not exist -- proves the refusal happens before
    # packing is even attempted, not as a side effect of FileBank being missing.
    result = pack_one("MyMod", root, tmp_path / "no-such-tools", log_path)

    assert result.error != ""
    assert ".git" in result.error
    assert result.pbo == ""


def test_pack_one_succeeds_with_git_present_when_exclude_list_is_empty(tmp_path, monkeypatch):
    """An empty exclude list (explicit opt-out) must not block packing."""
    root = tmp_path / "root"
    root.mkdir()
    src_dir = root / "MyMod"
    src_dir.mkdir()
    (src_dir / "config.cpp").write_text("class CfgMods {};", encoding="utf-8")
    (src_dir / ".git").mkdir()
    (src_dir / ".git" / "config").write_text("", encoding="utf-8")

    tools = tmp_path / "tools"
    filebank_dir = tools / "Bin" / "PboUtils"
    filebank_dir.mkdir(parents=True, exist_ok=True)
    (filebank_dir / "FileBank.exe").write_text("stub", encoding="utf-8")

    log_path = root / "build.log"

    def fake_run_blocking(cmd, cwd, log_path_arg, timeout=None):
        out_dir = root / "@MyMod" / "addons"
        out_dir.mkdir(parents=True, exist_ok=True)
        pbo = out_dir / "MyMod.pbo"
        pbo.write_text("fake pbo data", encoding="utf-8")
        return 0, "FileBank success"

    monkeypatch.setattr("dayz_mcp.packer.run_blocking", fake_run_blocking)

    result = pack_one("MyMod", root, tools, log_path, exclude=[])
    assert result.error == ""
    assert result.pbo != ""
