"""`workshop.py`: the Workshop side of a built mod, with no Steam anywhere.

What matters here: the meta.cpp this server writes is the one Publisher
writes, byte for byte, so the two tools can hand a folder back and forth; a
folder is inspected the way Steam uploads it, links included; and the public
listing is parsed into something an answer can carry.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

from dayz_mcp import workshop
from dayz_mcp.workshop import (
    Folder, Item, Meta, eresult_name, explain, inspect_folder, item_url, parse_details,
    parse_meta, read_item, render_meta, stale,
)

# The shape Publisher leaves in a folder it published from, read off a real
# one 2026-09-19 with the name and id made generic: three lines, LF, a
# trailing newline, nothing else.
PUBLISHER_META = 'protocol = 1;\npublishedid = 123456789;\nname = "MyMod";\n'


# --------------------------------------------------------------- meta.cpp


def test_meta_round_trips_publishers_own_bytes():
    meta = parse_meta(PUBLISHER_META)
    assert meta == Meta(123456789, "MyMod")
    assert render_meta(meta.published_id, meta.name) == PUBLISHER_META


def test_meta_tolerates_whitespace_and_crlf():
    text = 'protocol=1;\r\n  publishedid =  42 ;\r\nname="X" ;\r\n'
    assert parse_meta(text) == Meta(42, "X")


def test_meta_without_an_id_is_invalid_not_zero_by_accident():
    meta = parse_meta('protocol = 1;\nname = "X";\n')
    assert meta.published_id == 0
    assert not meta.valid


def test_render_meta_keeps_the_file_to_one_string():
    text = render_meta(7, 'My "Mod"')
    assert text == "protocol = 1;\npublishedid = 7;\nname = \"My 'Mod'\";\n"
    assert parse_meta(text) == Meta(7, "My 'Mod'")


# ------------------------------------------------------------- the folder


def a_built_folder(tmp_path: Path, *, signed: bool = True, meta: str | None = PUBLISHER_META) -> Path:
    folder = tmp_path / "@MyMod"
    (folder / "addons").mkdir(parents=True)
    (folder / "addons" / "MyMod.pbo").write_bytes(b"x" * 100)
    if signed:
        (folder / "addons" / "MyMod.pbo.Key.bisign").write_bytes(b"s" * 10)
    (folder / "keys").mkdir()
    (folder / "keys" / "Key.bikey").write_bytes(b"k" * 5)
    (folder / "mod.cpp").write_text('name = "My Mod";\n', encoding="utf-8", newline="\n")
    if meta is not None:
        (folder / "meta.cpp").write_text(meta, encoding="utf-8", newline="\n")
    return folder


def test_inspect_reads_pbos_signatures_keys_and_meta(tmp_path):
    folder = inspect_folder(a_built_folder(tmp_path))
    assert folder.exists and folder.built
    assert folder.pbos == ["addons/MyMod.pbo"]
    assert folder.unsigned == []
    assert folder.keys == ["keys/Key.bikey"]
    assert folder.files == 5
    assert folder.bytes == 100 + 10 + 5 + len('name = "My Mod";\n') + len(PUBLISHER_META)
    assert folder.meta == Meta(123456789, "MyMod")
    assert folder.meta_error == ""
    assert folder.newest_pbo == pytest.approx((tmp_path / "@MyMod" / "addons" / "MyMod.pbo").stat().st_mtime)


def test_inspect_names_an_unsigned_pbo(tmp_path):
    folder = inspect_folder(a_built_folder(tmp_path, signed=False))
    assert folder.unsigned == ["addons/MyMod.pbo"]


def test_inspect_distinguishes_no_meta_from_a_broken_one(tmp_path):
    none = inspect_folder(a_built_folder(tmp_path / "a", meta=None))
    assert none.meta is None and none.meta_error == ""

    broken = inspect_folder(a_built_folder(tmp_path / "b", meta="protocol = 1;\n"))
    assert broken.meta is not None and not broken.meta.valid
    assert "publishedid" in broken.meta_error


def test_inspect_reports_a_missing_or_empty_folder(tmp_path):
    missing = inspect_folder(tmp_path / "never-built")
    assert not missing.exists and not missing.built

    (tmp_path / "empty").mkdir()
    empty = inspect_folder(tmp_path / "empty")
    assert empty.exists and not empty.built and empty.files == 0


@pytest.mark.skipif(sys.platform != "win32", reason="junctions are a Windows thing")
def test_inspect_names_a_junction_and_does_not_walk_it(tmp_path):
    """A junction inside the folder would make Steam upload the whole tree
    behind it -- the very source tree the pbo was packed from. So it is
    named, and nothing under it is counted."""
    import _winapi

    folder = a_built_folder(tmp_path)
    src = tmp_path / "src"
    (src / "scripts").mkdir(parents=True)
    (src / "scripts" / "big.c").write_bytes(b"c" * 5000)
    _winapi.CreateJunction(str(src), str(folder / "MyMod"))

    seen = inspect_folder(folder)
    assert seen.links == ["MyMod"]
    assert seen.files == 5, "nothing behind the junction was counted"
    assert seen.bytes < 5000


def test_inspect_names_a_symlinked_file(tmp_path):
    folder = a_built_folder(tmp_path)
    target = tmp_path / "elsewhere.txt"
    target.write_text("x", encoding="utf-8")
    try:
        os.symlink(target, folder / "link.txt")
    except (OSError, NotImplementedError):
        pytest.skip("this account cannot create symlinks")
    seen = inspect_folder(folder)
    assert seen.links == ["link.txt"]
    assert seen.files == 5


# ------------------------------------------------------- the public listing


def an_answer(**fields) -> bytes:
    entry = {
        "publishedfileid": "123456789", "result": 1, "title": "MyMod",
        "time_created": 1757400000, "time_updated": 1758200000, "file_size": "123456",
        "visibility": 0, "subscriptions": 12, "tags": [{"tag": "Mod"}],
        "preview_url": "https://images/preview.png",
    } | fields
    return json.dumps({"response": {"result": 1, "resultcount": 1, "publishedfiledetails": [entry]}}).encode()


def test_parse_details_reads_the_fields_an_answer_needs():
    item = parse_details(an_answer(), 123456789)
    assert item.visible
    assert item.title == "MyMod"
    assert item.time_updated == 1758200000
    assert item.file_size == 123456
    assert item.visibility == 0
    assert item.subscriptions == 12
    assert item.tags == ["Mod"]
    assert item.url == item_url(123456789) == "https://steamcommunity.com/sharedfiles/filedetails/?id=123456789"
    assert item.to_dict()["visibility_name"] == "public"


def test_parse_details_keeps_a_hidden_item_as_a_fact_not_an_error():
    """A private item comes back as result 9 with nothing else -- that is
    Steam saying "not for you", which an answer must carry, not raise on."""
    body = json.dumps({"response": {"result": 1, "resultcount": 1,
                                    "publishedfiledetails": [{"publishedfileid": "5", "result": 9}]}}).encode()
    item = parse_details(body, 5)
    assert not item.visible and item.result == 9 and item.title == ""


def test_parse_details_refuses_an_answer_that_is_not_one():
    with pytest.raises(ValueError):
        parse_details(b'{"response": {}}', 5)
    with pytest.raises(ValueError):
        parse_details(b"<html>429</html>", 5)


def test_read_item_posts_the_id_and_never_raises():
    calls = []

    def post(url, data, timeout):
        calls.append((url, data, timeout))
        return an_answer()

    item, err = read_item(123456789, post=post)
    assert err == "" and item is not None and item.visible
    assert calls[0][0] == workshop.DETAILS_URL
    assert b"itemcount=1" in calls[0][1] and b"publishedfileids%5B0%5D=123456789" in calls[0][1]

    def down(url, data, timeout):
        raise OSError("no route to host")

    item, err = read_item(123456789, post=down)
    assert item is None and "no route to host" in err


def test_stale_compares_the_newest_pbo_with_the_last_upload(tmp_path):
    folder = inspect_folder(a_built_folder(tmp_path))
    now = time.time()
    older_upload = Item(id=1, result=1, time_updated=int(now) - 3600)
    newer_upload = Item(id=1, result=1, time_updated=int(now) + 3600)
    assert stale(folder, older_upload) is True
    assert stale(folder, newer_upload) is False
    assert stale(folder, None) is None
    assert stale(folder, Item(id=1, result=9)) is None, "a hidden item says nothing about age"
    assert stale(Folder(path="x"), older_upload) is None, "nothing built, nothing to compare"


# ------------------------------------------------------------- the codes


def test_result_codes_are_named_and_the_likely_ones_explained():
    assert eresult_name(1) == "OK"
    assert eresult_name(25) == "LimitExceeded"
    assert eresult_name(999) == "EResult 999"
    assert "1 MB" in explain(25)
    assert explain(1) == ""
