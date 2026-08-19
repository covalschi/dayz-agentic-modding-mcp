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


def test_pack_one_says_why_it_did_not_sign_when_there_is_no_keys_directory(tmp_path, monkeypatch):
    """An unsigned build stays a success, but never a silent one. With no
    <root>/keys at all the signing block was skipped entirely, so the result
    was signed=False with an empty note -- indistinguishable from a signing
    attempt that failed. The note must name the directory that was looked
    for, so the answer is "put a key here", not "read the pack log"."""
    root = tmp_path / "root"
    root.mkdir()
    src_dir = root / "MyMod"
    src_dir.mkdir()
    (src_dir / "config.cpp").write_text("class CfgMods {};", encoding="utf-8")

    tools = tmp_path / "tools"
    filebank_dir = tools / "Bin" / "PboUtils"
    filebank_dir.mkdir(parents=True, exist_ok=True)
    (filebank_dir / "FileBank.exe").write_text("stub", encoding="utf-8")

    def fake_run_blocking(cmd, cwd, log_path_arg, timeout=None):
        out_dir = root / "@MyMod" / "addons"
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "MyMod.pbo").write_text("fake pbo data", encoding="utf-8")
        return 0, "FileBank success"

    monkeypatch.setattr("dayz_mcp.packer.run_blocking", fake_run_blocking)

    result = pack_one("MyMod", root, tools, root / "build.log")

    assert result.error == ""       # unsigned is not a failure
    assert result.pbo != ""
    assert result.signed is False
    assert str(root / "keys") in result.note
    assert "unsigned" in result.note.lower()


def test_pack_one_says_why_it_did_not_sign_when_the_keys_directory_is_empty(tmp_path, monkeypatch):
    """The same silence, one step along: the directory exists (so "put a key
    here" has already been half-followed) but holds no private key."""
    root = tmp_path / "root"
    root.mkdir()
    src_dir = root / "MyMod"
    src_dir.mkdir()
    (src_dir / "config.cpp").write_text("class CfgMods {};", encoding="utf-8")
    keys_dir = root / "keys"
    keys_dir.mkdir()
    (keys_dir / "readme.txt").write_text("keys go here", encoding="utf-8")

    tools = tmp_path / "tools"
    filebank_dir = tools / "Bin" / "PboUtils"
    filebank_dir.mkdir(parents=True, exist_ok=True)
    (filebank_dir / "FileBank.exe").write_text("stub", encoding="utf-8")

    def fake_run_blocking(cmd, cwd, log_path_arg, timeout=None):
        out_dir = root / "@MyMod" / "addons"
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "MyMod.pbo").write_text("fake pbo data", encoding="utf-8")
        return 0, "FileBank success"

    monkeypatch.setattr("dayz_mcp.packer.run_blocking", fake_run_blocking)

    result = pack_one("MyMod", root, tools, root / "build.log")

    assert result.error == ""
    assert result.signed is False
    assert str(keys_dir) in result.note
    assert "biprivatekey" in result.note
    assert "unsigned" in result.note.lower()


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


# --- Requirement: opt-in staging lets a mod whose source is the repository
# root (config.cpp next to dayz-mcp.toml, README.md, keys/, its own .git)
# be packed, by copying a filtered copy instead of refusing ---


def test_pack_one_stages_and_packs_a_root_layout_mod(tmp_path, monkeypatch):
    """A mod whose source is the repository root itself packs successfully
    under stage=True even though the root contains a .git directory that
    would otherwise refuse packing outright."""
    root = tmp_path / "root"
    root.mkdir()
    (root / "config.cpp").write_text("class CfgMods {};", encoding="utf-8")
    (root / ".git").mkdir()
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (root / "README.md").write_text("hello", encoding="utf-8")

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

    result = pack_one("MyMod", root, tools, log_path, src=root, stage=True)

    assert result.error == ""
    assert result.pbo == str(root / "@MyMod" / "addons" / "MyMod.pbo")
    assert Path(result.pbo).exists()


def test_pack_one_staged_copy_omits_excluded_entries(tmp_path, monkeypatch):
    """The staged copy handed to FileBank must not contain anything matching
    `exclude`, and the omission must be visible in PackResult.note rather
    than silent."""
    root = tmp_path / "root"
    root.mkdir()
    (root / "config.cpp").write_text("class CfgMods {};", encoding="utf-8")
    (root / ".git").mkdir()
    (root / ".git" / "HEAD").write_text("x", encoding="utf-8")
    (root / "notes.txt").write_text("keep me", encoding="utf-8")

    tools = tmp_path / "tools"
    filebank_dir = tools / "Bin" / "PboUtils"
    filebank_dir.mkdir(parents=True, exist_ok=True)
    (filebank_dir / "FileBank.exe").write_text("stub", encoding="utf-8")

    log_path = root / "build.log"
    seen = {}

    def fake_run_blocking(cmd, cwd, log_path_arg, timeout=None):
        staged_src = Path(cmd[-1])  # filebank_cmd's last positional arg is the src directory
        seen["has_git"] = (staged_src / ".git").exists()
        seen["has_notes"] = (staged_src / "notes.txt").exists()
        seen["has_config"] = (staged_src / "config.cpp").exists()
        out_dir = root / "@MyMod" / "addons"
        out_dir.mkdir(parents=True, exist_ok=True)
        pbo = out_dir / "MyMod.pbo"
        pbo.write_text("fake pbo data", encoding="utf-8")
        return 0, "FileBank success"

    monkeypatch.setattr("dayz_mcp.packer.run_blocking", fake_run_blocking)

    result = pack_one("MyMod", root, tools, log_path, src=root, stage=True)

    assert result.error == ""
    assert seen["has_git"] is False
    assert seen["has_notes"] is True
    assert seen["has_config"] is True
    assert "staged copy included" in result.note
    assert "notes.txt" in result.note
    assert "config.cpp" in result.note
    assert "omitted" in result.note
    assert ".git" in result.note


def test_pack_one_staging_never_copies_the_servers_own_profile_or_job_store(tmp_path, monkeypatch):
    """The server's own profile halves and job-store directory must never be
    packed, regardless of build.exclude and regardless of `stage` -- a project
    must never have to know they exist. dayz-mcp.local.toml is the worst
    case: it carries machine-specific absolute paths (game, tools, stand)
    and its entire reason to exist is that it never leaves the machine.

    Both enforcement paths are exercised here: staging filters them out of the
    copy, and the default (non-staging) path, which has nothing to filter,
    refuses to pack at all. The two are asserted together because this test
    once claimed the unconditional rule while only ever running the staged
    half -- which is how the other half stayed uncovered."""
    root = tmp_path / "root"
    root.mkdir()
    (root / "config.cpp").write_text("class CfgMods {};", encoding="utf-8")
    (root / "dayz-mcp.toml").write_text('[project]\nname = "x"\n', encoding="utf-8")
    (root / "dayz-mcp.local.toml").write_text('[machine]\ngame = "C:/Games/DayZ"\n', encoding="utf-8")
    job_store = root / ".dayz-mcp" / "jobs"
    job_store.mkdir(parents=True)
    (job_store / "job.json").write_text("{}", encoding="utf-8")

    tools = tmp_path / "tools"
    filebank_dir = tools / "Bin" / "PboUtils"
    filebank_dir.mkdir(parents=True, exist_ok=True)
    (filebank_dir / "FileBank.exe").write_text("stub", encoding="utf-8")

    log_path = root / "build.log"
    seen = {}

    def fake_run_blocking(cmd, cwd, log_path_arg, timeout=None):
        staged_src = Path(cmd[-1])
        seen["has_toml"] = (staged_src / "dayz-mcp.toml").exists()
        seen["has_local_toml"] = (staged_src / "dayz-mcp.local.toml").exists()
        seen["has_job_store"] = (staged_src / ".dayz-mcp").exists()
        out_dir = root / "@MyMod" / "addons"
        out_dir.mkdir(parents=True, exist_ok=True)
        pbo = out_dir / "MyMod.pbo"
        pbo.write_text("fake pbo data", encoding="utf-8")
        return 0, "FileBank success"

    monkeypatch.setattr("dayz_mcp.packer.run_blocking", fake_run_blocking)

    result = pack_one("MyMod", root, tools, log_path, src=root, stage=True)

    assert result.error == ""
    assert seen["has_toml"] is False
    assert seen["has_local_toml"] is False
    assert seen["has_job_store"] is False
    assert "dayz-mcp.toml" in result.note
    assert "dayz-mcp.local.toml" in result.note
    assert ".dayz-mcp" in result.note

    # Same invariant, default path: exclude=[] proves the refusal is the
    # own-artifact rule firing and not the ordinary exclude check.
    seen.clear()
    refused = pack_one("MyMod", root, tools, log_path, src=root, stage=False, exclude=[])
    assert refused.error != ""
    assert "dayz-mcp.toml" in refused.error
    assert "dayz-mcp.local.toml" in refused.error
    assert ".dayz-mcp" in refused.error
    assert refused.pbo == ""
    assert seen == {}, "FileBank was invoked despite the refusal"


def test_default_exclude_covers_repository_root_clutter():
    """DEFAULT_EXCLUDE widened to cover what a repository root normally
    carries and a mod never needs -- most relevant to a root-layout mod
    packed via staging, where the whole root becomes the source."""
    assert set(DEFAULT_EXCLUDE) >= {
        ".git", "*.blend", "*.blend1", ".gitignore", ".gitattributes", "README.md", "*.ps1",
    }


def test_find_excluded_matches_the_widened_default_patterns(tmp_path):
    """Exercises find_excluded (not just the tuple) against the four newly
    added patterns, the same discipline test_find_excluded_matches_blend_globs
    already applies to the original three."""
    (tmp_path / "config.cpp").write_text("", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("", encoding="utf-8")
    (tmp_path / ".gitattributes").write_text("", encoding="utf-8")
    (tmp_path / "README.md").write_text("", encoding="utf-8")
    (tmp_path / "build.ps1").write_text("", encoding="utf-8")
    (tmp_path / "keep.txt").write_text("", encoding="utf-8")

    found = find_excluded(tmp_path, DEFAULT_EXCLUDE)

    assert sorted(found) == [".gitattributes", ".gitignore", "README.md", "build.ps1"]


def test_pack_one_staging_never_copies_the_keys_directory(tmp_path, monkeypatch):
    """The signing key directory must never be packed, on either path, even
    though it is not in DEFAULT_EXCLUDE -- a root-layout mod's source is the
    same directory as the profile root, which is exactly where the private
    signing key lives (root/keys). Letting it ride along would embed the
    private key inside the published pbo, and whoever holds that key can sign
    arbitrary mods as this author.

    As above, both halves of the rule are asserted here: staging filters the
    directory out, the default path refuses outright."""
    root = tmp_path / "root"
    root.mkdir()
    (root / "config.cpp").write_text("class CfgMods {};", encoding="utf-8")
    keys_dir = root / "keys"
    keys_dir.mkdir()
    (keys_dir / "secret.biprivatekey").write_text("do not ship me", encoding="utf-8")

    tools = tmp_path / "tools"
    filebank_dir = tools / "Bin" / "PboUtils"
    filebank_dir.mkdir(parents=True, exist_ok=True)
    (filebank_dir / "FileBank.exe").write_text("stub", encoding="utf-8")

    log_path = root / "build.log"
    seen = {}

    def fake_run_blocking(cmd, cwd, log_path_arg, timeout=None):
        staged_src = Path(cmd[-1])
        seen["has_keys"] = (staged_src / "keys").exists()
        out_dir = root / "@MyMod" / "addons"
        out_dir.mkdir(parents=True, exist_ok=True)
        pbo = out_dir / "MyMod.pbo"
        pbo.write_text("fake pbo data", encoding="utf-8")
        return 0, "FileBank success"

    monkeypatch.setattr("dayz_mcp.packer.run_blocking", fake_run_blocking)

    result = pack_one("MyMod", root, tools, log_path, src=root, stage=True)

    assert result.error == ""
    assert seen["has_keys"] is False
    assert "keys" in result.note

    seen.clear()
    refused = pack_one("MyMod", root, tools, log_path, src=root, stage=False, exclude=[])
    assert refused.error != ""
    assert "keys" in refused.error
    assert refused.pbo == ""
    assert seen == {}, "FileBank was invoked despite the refusal"


def test_pack_one_staging_never_copies_its_own_output_folder(tmp_path, monkeypatch):
    """On a root-layout mod, the built @Name folder is a subdirectory of the
    source. Packing it would put a previous build's pbo inside the new one,
    growing without bound on every rebuild -- so staging filters it out and
    the default path refuses, the same two halves as the tests above."""
    root = tmp_path / "root"
    root.mkdir()
    (root / "config.cpp").write_text("class CfgMods {};", encoding="utf-8")
    # Simulate a previous build already sitting there.
    prior = root / "@MyMod" / "addons"
    prior.mkdir(parents=True)
    (prior / "MyMod.pbo").write_bytes(b"old pbo bytes from a previous build")

    tools = tmp_path / "tools"
    filebank_dir = tools / "Bin" / "PboUtils"
    filebank_dir.mkdir(parents=True, exist_ok=True)
    (filebank_dir / "FileBank.exe").write_text("stub", encoding="utf-8")

    log_path = root / "build.log"
    seen = {}

    def fake_run_blocking(cmd, cwd, log_path_arg, timeout=None):
        staged_src = Path(cmd[-1])
        seen["has_output_folder"] = (staged_src / "@MyMod").exists()
        out_dir = root / "@MyMod" / "addons"
        out_dir.mkdir(parents=True, exist_ok=True)
        pbo = out_dir / "MyMod.pbo"
        pbo.write_text("fake pbo data", encoding="utf-8")
        return 0, "FileBank success"

    monkeypatch.setattr("dayz_mcp.packer.run_blocking", fake_run_blocking)

    result = pack_one("MyMod", root, tools, log_path, src=root, stage=True)

    assert result.error == ""
    assert seen["has_output_folder"] is False

    seen.clear()
    refused = pack_one("MyMod", root, tools, log_path, src=root, stage=False, exclude=[])
    assert refused.error != ""
    assert "@MyMod" in refused.error
    assert refused.pbo == ""
    assert seen == {}, "FileBank was invoked despite the refusal"


def test_pack_one_refuses_to_pack_the_servers_own_artifacts_without_staging(tmp_path, monkeypatch):
    """The reviewer's reproduction, verbatim: a root-layout mod, the stock
    exclude list, stage=False -- and NO .git, because a release archive or a
    CI checkout has none. The .git refusal is what appeared to cover this
    case; with .git gone it does not fire, and before this guard existed
    FileBank was handed the private signing key, both halves of this
    server's profile, its job store and the previous build's own pbo, with
    no error at all.

    The refusal must name what it found and point at the way forward, since
    for a genuine root-layout mod `stage = true` is the only way to pack at
    all."""
    root = tmp_path / "root"
    root.mkdir()
    (root / "config.cpp").write_text("class CfgMods {};", encoding="utf-8")
    (root / "dayz-mcp.toml").write_text('[project]\nname = "x"\n', encoding="utf-8")
    (root / "dayz-mcp.local.toml").write_text('[machine]\ngame = "C:/Games/DayZ"\n', encoding="utf-8")
    keys_dir = root / "keys"
    keys_dir.mkdir()
    (keys_dir / "MyKey.biprivatekey").write_text("do not ship me", encoding="utf-8")
    job_store = root / ".dayz-mcp" / "jobs" / "build-1-1"
    job_store.mkdir(parents=True)
    (job_store / "job.json").write_text("{}", encoding="utf-8")
    prior = root / "@MyMod" / "addons"
    prior.mkdir(parents=True)
    (prior / "MyMod.pbo").write_bytes(b"a previous build")

    tools = tmp_path / "tools"
    filebank_dir = tools / "Bin" / "PboUtils"
    filebank_dir.mkdir(parents=True, exist_ok=True)
    (filebank_dir / "FileBank.exe").write_text("stub", encoding="utf-8")

    packed = {}

    def fake_run_blocking(cmd, cwd, log_path_arg, timeout=None):
        staged_src = Path(cmd[-1])
        packed["files"] = sorted(str(p.relative_to(staged_src)) for p in staged_src.rglob("*") if p.is_file())
        return 0, "FileBank success"

    monkeypatch.setattr("dayz_mcp.packer.run_blocking", fake_run_blocking)

    result = pack_one("MyMod", root, tools, root / "build.log", src=root, stage=False)

    assert result.error != "", f"packed instead of refusing; FileBank got {packed.get('files')}"
    assert packed == {}, f"FileBank was handed {packed.get('files')}"
    for expected in ("keys", "dayz-mcp.toml", "dayz-mcp.local.toml", ".dayz-mcp", "@MyMod"):
        assert expected in result.error, f"{expected} missing from the refusal: {result.error}"
    assert "stage" in result.error
    assert result.pbo == ""


def test_the_refusal_states_what_staging_would_cost_instead_of_claiming_ownership(tmp_path):
    """A mod can legitimately ship a `keys` folder -- public keys for the mods
    it depends on, say. It still cannot be packed: the name is reserved, and
    matching by name is what keeps the refusal and the staging omission the
    same set. But the refusal used to assert the folder "belongs to the build
    server", which is simply untrue for such a mod, and then pointed at
    build.stage = true without saying that staging DROPS that folder. Told
    something false, then steered into silent data loss.

    The behaviour is a deliberate trade and stays. The text must be honest:
    name the entries, say they cannot be packed, and say plainly that
    stage = true packs the mod without them."""
    root = tmp_path / "root"
    root.mkdir()
    src_dir = root / "MyMod"
    src_dir.mkdir()
    (src_dir / "config.cpp").write_text("class CfgMods {};", encoding="utf-8")
    mod_keys = src_dir / "keys"
    mod_keys.mkdir()
    (mod_keys / "partner.bikey").write_text("a dependency's public key", encoding="utf-8")

    # tools deliberately absent: the refusal happens before packing is attempted.
    result = pack_one("MyMod", root, tmp_path / "no-such-tools", root / "build.log")

    assert result.error != ""
    assert "keys" in result.error, "the refusal must name what it found"
    # The old claim, which was false for a mod that ships its own keys folder.
    assert "belong to the build server and not to the mod" not in result.error
    # The consequence of the route it recommends must be stated, not implied.
    assert "stage" in result.error
    assert "omitted" in result.error
    assert "pbo" in result.error


def test_the_staged_copy_note_marks_reserved_omissions_apart_from_routine_ones(tmp_path, monkeypatch):
    """Under stage = true a mod's own `keys` folder is dropped from the copy.
    Listed among .git and README.md it reads as routine housekeeping, so the
    note separates the two: what build.exclude removed, and what was removed
    because the name is reserved and therefore is not in the pbo even though
    the mod ships it."""
    root = tmp_path / "root"
    root.mkdir()
    (root / "config.cpp").write_text("class CfgMods {};", encoding="utf-8")
    (root / ".git").mkdir()
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    mod_keys = root / "keys"
    mod_keys.mkdir()
    (mod_keys / "partner.bikey").write_text("a dependency's public key", encoding="utf-8")

    tools = tmp_path / "tools"
    filebank_dir = tools / "Bin" / "PboUtils"
    filebank_dir.mkdir(parents=True, exist_ok=True)
    (filebank_dir / "FileBank.exe").write_text("stub", encoding="utf-8")

    def fake_run_blocking(cmd, cwd, log_path_arg, timeout=None):
        out_dir = root / "@MyMod" / "addons"
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "MyMod.pbo").write_text("fake pbo data", encoding="utf-8")
        return 0, "FileBank success"

    monkeypatch.setattr("dayz_mcp.packer.run_blocking", fake_run_blocking)

    result = pack_one("MyMod", root, tools, root / "build.log", src=root, stage=True)

    assert result.error == ""
    note = result.note
    assert "build.exclude" in note and ".git" in note
    reserved_half = note.split("reserved", 1)
    assert len(reserved_half) == 2, f"reserved omissions are not called out: {note}"
    assert "keys" in reserved_half[1]
    assert "pbo" in reserved_half[1]


def test_newest_source_mtime_ignores_matching_names(tmp_path):
    """The `ignore` parameter skips both files and whole directories by
    name, the same pruning discipline find_excluded uses -- needed because
    a mod's own build output and this server's job-store directory can end
    up inside `src` (see pack_one's own_artifacts) and must not count
    toward what "the source" means for staleness purposes: both get
    written to during the very packing run being measured."""
    old = time.time() - 5000
    cfg = tmp_path / "config.cpp"
    cfg.write_text("x", encoding="utf-8")
    os.utime(cfg, (old, old))

    job_dir = tmp_path / ".dayz-mcp"
    job_dir.mkdir()
    job_file = job_dir / "job.json"
    job_file.write_text("{}", encoding="utf-8")  # fresh mtime ("now")

    # Without ignoring it, the freshly-written job file dominates.
    assert newest_source_mtime(tmp_path) == job_file.stat().st_mtime
    # Ignoring the directory by name skips it entirely -- back to config.cpp.
    assert newest_source_mtime(tmp_path, ignore=[".dayz-mcp"]) == cfg.stat().st_mtime


def test_pack_one_staging_gives_the_copy_a_fresh_mtime(tmp_path, monkeypatch):
    """Reproduced directly against the real FileBank binary (not just
    inferred): it does its own internal staleness check and silently skips
    rewriting a pbo when the source it is handed looks no newer than the
    existing destination -- reporting success while leaving the old bytes
    on disk untouched. shutil.copytree's default copy function (copy2)
    preserves the ORIGINAL file's mtime on the copy, which on any rebuild
    after the first is always older than whatever pbo is already at the
    destination (the true source files keep their real edit times), so a
    copy2'd staged copy would silently defeat every rebuild past the
    first. The staged copy's files must carry a fresh mtime instead."""
    root = tmp_path / "root"
    root.mkdir()
    old = time.time() - 5000
    cfg = root / "config.cpp"
    cfg.write_text("class CfgMods {};", encoding="utf-8")
    os.utime(cfg, (old, old))

    tools = tmp_path / "tools"
    filebank_dir = tools / "Bin" / "PboUtils"
    filebank_dir.mkdir(parents=True, exist_ok=True)
    (filebank_dir / "FileBank.exe").write_text("stub", encoding="utf-8")

    log_path = root / "build.log"
    seen = {}

    def fake_run_blocking(cmd, cwd, log_path_arg, timeout=None):
        staged_src = Path(cmd[-1])
        seen["staged_config_mtime"] = (staged_src / "config.cpp").stat().st_mtime
        out_dir = root / "@MyMod" / "addons"
        out_dir.mkdir(parents=True, exist_ok=True)
        pbo = out_dir / "MyMod.pbo"
        pbo.write_text("fake pbo data", encoding="utf-8")
        return 0, "FileBank success"

    monkeypatch.setattr("dayz_mcp.packer.run_blocking", fake_run_blocking)

    before = time.time()
    result = pack_one("MyMod", root, tools, log_path, src=root, stage=True)
    after = time.time()

    assert result.error == ""
    assert "staged_config_mtime" in seen
    assert seen["staged_config_mtime"] != cfg.stat().st_mtime
    assert before - 5 <= seen["staged_config_mtime"] <= after + 5


def test_pack_one_stale_guard_uses_original_source_not_staged_copy(tmp_path, monkeypatch):
    """The single most important property of staging: the stale-pbo
    comparison must measure the ORIGINAL `src` tree, never the staged copy.
    Reproduced by mutating `src` itself (not the copy) to a future mtime
    from inside the fake FileBank call -- by that point staging has already
    taken its copy, so a correct implementation still catches this because
    it re-reads `src` live; an implementation that measured the (frozen,
    now out-of-date) copy would not, and this test would then fail to see
    "stale pbo" in the result."""
    root = tmp_path / "root"
    root.mkdir()
    (root / "config.cpp").write_text("class CfgMods {};", encoding="utf-8")

    tools = tmp_path / "tools"
    filebank_dir = tools / "Bin" / "PboUtils"
    filebank_dir.mkdir(parents=True, exist_ok=True)
    (filebank_dir / "FileBank.exe").write_text("stub", encoding="utf-8")

    log_path = root / "build.log"
    future = time.time() + 1000

    def fake_run_blocking(cmd, cwd, log_path_arg, timeout=None):
        # Staging already copied `src` by the time this runs. Touching the
        # ORIGINAL now simulates the live source tree changing after that
        # copy was taken.
        os.utime(root / "config.cpp", (future, future))
        out_dir = root / "@MyMod" / "addons"
        out_dir.mkdir(parents=True, exist_ok=True)
        pbo = out_dir / "MyMod.pbo"
        pbo.write_text("fake pbo data", encoding="utf-8")
        return 0, "FileBank success"

    monkeypatch.setattr("dayz_mcp.packer.run_blocking", fake_run_blocking)

    result = pack_one("MyMod", root, tools, log_path, src=root, stage=True)

    assert "stale pbo" in result.error


def test_pack_one_staging_dir_removed_after_success_and_after_failure(tmp_path, monkeypatch):
    """The temporary staging directory must be gone once pack_one returns,
    whether packing succeeded or FileBank itself failed."""
    root = tmp_path / "root"
    root.mkdir()
    (root / "config.cpp").write_text("class CfgMods {};", encoding="utf-8")

    tools = tmp_path / "tools"
    filebank_dir = tools / "Bin" / "PboUtils"
    filebank_dir.mkdir(parents=True, exist_ok=True)
    (filebank_dir / "FileBank.exe").write_text("stub", encoding="utf-8")

    log_path = root / "build.log"
    captured_staging_roots = []

    def make_fake(filebank_code):
        def fake_run_blocking(cmd, cwd, log_path_arg, timeout=None):
            staged_src = Path(cmd[-1])
            captured_staging_roots.append(staged_src.parent)  # mkdtemp()'s own dir
            if filebank_code != 0:
                return filebank_code, "FileBank error: simulated failure"
            out_dir = root / "@MyMod" / "addons"
            out_dir.mkdir(parents=True, exist_ok=True)
            pbo = out_dir / "MyMod.pbo"
            pbo.write_text("fake pbo data", encoding="utf-8")
            return 0, "FileBank success"
        return fake_run_blocking

    monkeypatch.setattr("dayz_mcp.packer.run_blocking", make_fake(0))
    result = pack_one("MyMod", root, tools, log_path, src=root, stage=True)
    assert result.error == ""
    assert len(captured_staging_roots) == 1
    assert not captured_staging_roots[0].exists()

    monkeypatch.setattr("dayz_mcp.packer.run_blocking", make_fake(1))
    result2 = pack_one("MyMod", root, tools, log_path, src=root, stage=True)
    assert result2.error != ""
    assert len(captured_staging_roots) == 2
    assert not captured_staging_roots[1].exists()
