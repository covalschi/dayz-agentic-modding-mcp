import os
import time
from pathlib import Path
from unittest.mock import MagicMock

from dayz_mcp.packer import (
    PackResult, filebank_cmd, find_keys, newest_source_mtime, pack_all, pack_one, sign_cmd,
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
