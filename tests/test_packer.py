import time
from pathlib import Path

from dayz_mcp.packer import (
    PackResult, filebank_cmd, find_keys, newest_source_mtime, sign_cmd,
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


def test_newest_source_mtime_sees_nested_files(tmp_path):
    (tmp_path / "sub").mkdir()
    old = tmp_path / "sub" / "old.c"
    old.write_text("", encoding="utf-8")
    time.sleep(0.05)
    new = tmp_path / "sub" / "new.c"
    new.write_text("", encoding="utf-8")
    assert newest_source_mtime(tmp_path) >= new.stat().st_mtime - 0.01


def test_pack_result_reports_absence_of_a_signature():
    r = PackResult(name="MyMod", pbo="", size=0, signed=False, error="FileBank exit 1")
    assert not r.signed
    assert "FileBank" in r.error
