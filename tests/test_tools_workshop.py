"""`workshop_publish` and `workshop_status`: the one tool whose effect is
somewhere else, and its read-only twin.

The uploader is never run here -- `run_uploader` is replaced by something
that writes the result file the real one would -- and the public listing is
never read: `read_item` is replaced too. What is proved is everything the
tool decides on its own: what it refuses before a byte leaves, what it hands
the uploader, and what it says afterwards.
"""
from __future__ import annotations

import sys
import textwrap
import time
from pathlib import Path

import pytest

from dayz_mcp import tools
from dayz_mcp.steamugc import Outcome, Spec
from dayz_mcp.tools import session
from dayz_mcp.tools import workshop as wtool
from dayz_mcp.workshop import LEGAL_URL, META_NAME, WORKSHOP_APP_ID, Item, render_meta

PROFILE = """
[project]
name = "my-mod"

[build]
mods = ["MyMod"]
"""

PUBLISHER_META = 'protocol = 1;\npublishedid = 123456789;\nname = "MyMod";\n'
ITEM = 123456789


def make_project(tmp_path: Path, *, built: bool = True, meta: str | None = PUBLISHER_META,
                 signed: bool = True, dll: bool = True) -> Path:
    root = tmp_path / "project"
    root.mkdir(parents=True)
    (root / "dayz-mcp.toml").write_text(textwrap.dedent(PROFILE), encoding="utf-8")
    (root / "MyMod").mkdir()
    (root / "MyMod" / "config.cpp").write_text("", encoding="utf-8")

    stand = tmp_path / "stand"
    (stand / "profiles").mkdir(parents=True)
    game = tmp_path / "game"
    game.mkdir()
    (game / "DayZDiag_x64.exe").write_bytes(b"")
    if dll:
        (game / "steam_api64.dll").write_bytes(b"")
    (root / "dayz-mcp.local.toml").write_text(
        f'[machine]\nstand_root = "{stand.as_posix()}"\ngame = "{game.as_posix()}"\n', encoding="utf-8"
    )

    if built:
        folder = root / "@MyMod"
        (folder / "addons").mkdir(parents=True)
        (folder / "addons" / "MyMod.pbo").write_bytes(b"x" * 100)
        if signed:
            (folder / "addons" / "MyMod.pbo.Key.bisign").write_bytes(b"s")
        (folder / "keys").mkdir()
        (folder / "keys" / "Key.bikey").write_bytes(b"k")
        if meta is not None:
            (folder / META_NAME).write_text(meta, encoding="utf-8", newline="\n")

    session.reset()
    opened = tools.project_open(str(root))
    assert opened.ok, opened.error
    return root


class FakeUploader:
    """Stands in for `run_uploader`: records what it was handed, writes the
    result the real process would, and -- when told to -- the meta.cpp the
    real driver writes after creating an item."""

    def __init__(self, outcome: Outcome | None, *, write_meta: bool = False, code: int = 0,
                 text: str = ""):
        self.outcome = outcome
        self.write_meta = write_meta
        self.code = code
        self.text = text
        self.calls: list[tuple[Path, Path, Path, Path]] = []
        self.spec: Spec | None = None

    def __call__(self, spec_path, result_path, cwd, log_path):
        self.calls.append((Path(spec_path), Path(result_path), Path(cwd), Path(log_path)))
        self.spec = Spec.from_json(Path(spec_path).read_text(encoding="utf-8"))
        Path(log_path).write_text(self.text, encoding="utf-8")
        if self.outcome is not None:
            if self.write_meta:
                (Path(self.spec.content) / META_NAME).write_text(
                    render_meta(self.outcome.published_id, self.spec.title), encoding="utf-8", newline="\n"
                )
            Path(result_path).write_text(self.outcome.to_json(), encoding="utf-8")
        return self.code, self.text


def fresh_item(published_id: int = ITEM, **fields) -> Item:
    fields = {"time_updated": int(time.time())} | fields
    return Item(id=published_id, result=1, title="MyMod", **fields)


def publish(monkeypatch, uploader: FakeUploader, *, item: Item | None = None, err: str = "", **kw):
    monkeypatch.setattr(wtool, "run_uploader", uploader)
    monkeypatch.setattr(wtool, "read_item", lambda published_id: (item, err))
    started = tools.workshop_publish(**kw)
    assert started.ok, started.error
    waited = tools.job_wait(started.data["job_id"], timeout=15)
    return started.data, waited.data


# ----------------------------------------------------------------- refusals


def test_it_refuses_without_a_project():
    result = tools.workshop_publish("MyMod")
    assert not result.ok and result.hint


def test_it_refuses_a_mod_the_project_does_not_declare(tmp_path):
    make_project(tmp_path)
    result = tools.workshop_publish("Other")
    assert not result.ok
    assert "not a mod this project declares" in result.error
    assert "MyMod" in result.hint


def test_it_refuses_a_visibility_it_does_not_know(tmp_path):
    make_project(tmp_path)
    result = tools.workshop_publish("MyMod", visibility="hidden")
    assert not result.ok and "public" in result.hint and "unlisted" in result.hint


def test_it_refuses_while_a_build_is_running(tmp_path):
    make_project(tmp_path)
    store = session.jobs()
    build = store.create("build")
    store.start(build.id)
    result = tools.workshop_publish("MyMod")
    assert not result.ok
    assert "build is already running" in result.error and build.id in result.hint


def test_it_refuses_while_another_upload_is_running(tmp_path):
    make_project(tmp_path)
    store = session.jobs()
    upload = store.create("workshop")
    store.start(upload.id)
    result = tools.workshop_publish("MyMod")
    assert not result.ok and "Workshop upload is already running" in result.error


def test_mod_build_refuses_while_an_upload_is_running(tmp_path, monkeypatch):
    """The other way round: a build rewrites the pbo the upload is reading."""
    make_project(tmp_path)
    monkeypatch.setattr("dayz_mcp.tools.build.session_tools_root", lambda: "C:/tools")
    store = session.jobs()
    upload = store.create("workshop")
    store.start(upload.id)
    result = tools.mod_build(skip_lint=True)
    assert not result.ok and "Workshop upload is already running" in result.error


def test_it_refuses_a_mod_that_is_not_built(tmp_path):
    make_project(tmp_path, built=False)
    result = tools.workshop_publish("MyMod")
    assert not result.ok
    assert "not built" in result.error and "mod_build" in result.hint


@pytest.mark.skipif(sys.platform != "win32", reason="junctions are a Windows thing")
def test_it_refuses_a_junction_inside_the_folder(tmp_path):
    import _winapi

    root = make_project(tmp_path)
    src = tmp_path / "src"
    src.mkdir()
    _winapi.CreateJunction(str(src), str(root / "@MyMod" / "MyMod"))
    result = tools.workshop_publish("MyMod")
    assert not result.ok
    assert "junction" in result.error and "MyMod" in result.error


def test_it_refuses_a_meta_it_cannot_read(tmp_path):
    make_project(tmp_path, meta="protocol = 1;\n")
    result = tools.workshop_publish("MyMod", title="X")
    assert not result.ok
    assert "publishedid" in result.error and "duplicate" in result.hint


def test_it_refuses_to_create_without_a_title(tmp_path):
    make_project(tmp_path, meta=None)
    result = tools.workshop_publish("MyMod")
    assert not result.ok and "title" in result.error


def test_it_refuses_a_preview_that_is_missing_wrong_or_too_big(tmp_path):
    root = make_project(tmp_path)
    missing = tools.workshop_publish("MyMod", preview="art/preview.png")
    assert not missing.ok and "not found" in missing.error

    (root / "preview.bmp").write_bytes(b"bm")
    wrong = tools.workshop_publish("MyMod", preview="preview.bmp")
    assert not wrong.ok and ".png" in wrong.error

    (root / "preview.png").write_bytes(b"p" * (wtool.MAX_PREVIEW_BYTES + 1))
    big = tools.workshop_publish("MyMod", preview="preview.png")
    assert not big.ok and str(wtool.MAX_PREVIEW_BYTES) in big.error


def test_it_refuses_without_the_games_steam_dll(tmp_path):
    make_project(tmp_path, dll=False)
    result = tools.workshop_publish("MyMod")
    assert not result.ok and "steam_api64.dll" in result.error


# ------------------------------------------------------------------ update


def test_an_update_hands_the_uploader_the_item_and_the_folder(tmp_path, monkeypatch):
    root = make_project(tmp_path)
    uploader = FakeUploader(Outcome(ok=True, published_id=ITEM, step="done", result=1, seconds=14.0))
    started, job = publish(monkeypatch, uploader, item=fresh_item(), mod="MyMod", changenote="fix")

    assert job["status"] == "done", job
    assert started["item"] == ITEM and not started["creating"]
    assert started["visibility"] == "unchanged"
    assert started["url"].endswith(str(ITEM))

    spec = uploader.spec
    assert spec.published_id == ITEM
    assert spec.content == str((root / "@MyMod").resolve())
    assert spec.dll == str(tmp_path / "game" / "steam_api64.dll")
    assert spec.app_id == WORKSHOP_APP_ID
    assert spec.changenote == "fix"
    assert spec.title == "" and spec.description == "" and spec.preview == ""
    assert spec.visibility is None and spec.tags == []

    spec_path, result_path, cwd, log_path = uploader.calls[0]
    assert (cwd / "steam_appid.txt").read_text(encoding="ascii").strip() == str(WORKSHOP_APP_ID)
    assert spec_path.parent == cwd == result_path.parent == log_path.parent
    assert log_path.name == "workshop-MyMod.log"

    summary = job["summary"]
    assert f"MyMod: updated item {ITEM}" in summary
    assert "14 s" in summary and "4 files" in summary
    assert f"https://steamcommunity.com/sharedfiles/filedetails/?id={ITEM}" in summary
    assert "matches this upload" in summary and "'MyMod'" in summary
    assert "visibility" not in summary
    assert str(log_path) in job["artifacts"] and str(result_path) in job["artifacts"]


def test_an_update_sends_only_what_was_given(tmp_path, monkeypatch):
    root = make_project(tmp_path)
    (root / "art").mkdir()
    (root / "art" / "preview.jpg").write_bytes(b"jpg")
    uploader = FakeUploader(Outcome(ok=True, published_id=ITEM, step="done", result=1))
    _, job = publish(monkeypatch, uploader, item=fresh_item(), mod="MyMod", title=" New title ",
                     description="Longer", preview="art/preview.jpg", visibility="unlisted",
                     tags=["Mod", " ", "Tools"])
    assert job["status"] == "done", job
    spec = uploader.spec
    assert spec.title == "New title"
    assert spec.description == "Longer"
    assert spec.preview == str((root / "art" / "preview.jpg").resolve())
    assert spec.visibility == 3
    assert spec.tags == ["Mod", "Tools"]
    assert "visibility unlisted" in job["summary"]


def test_the_readback_says_when_the_listing_still_lags(tmp_path, monkeypatch):
    make_project(tmp_path)
    uploader = FakeUploader(Outcome(ok=True, published_id=ITEM, step="done", result=1))
    old = fresh_item(time_updated=int(time.time()) - 86400)
    _, job = publish(monkeypatch, uploader, item=old, mod="MyMod")
    assert job["status"] == "done"
    assert "still the previous upload" in job["summary"]


def test_the_readback_reports_a_hidden_item_and_a_dead_network(tmp_path, monkeypatch):
    make_project(tmp_path)
    hidden = Item(id=ITEM, result=9)
    _, job = publish(monkeypatch, FakeUploader(Outcome(ok=True, published_id=ITEM, step="done", result=1)),
                     item=hidden, mod="MyMod")
    assert "not visible to the public listing" in job["summary"]

    session.reset()
    make_project(tmp_path / "second")
    _, job = publish(monkeypatch, FakeUploader(Outcome(ok=True, published_id=ITEM, step="done", result=1)),
                     item=None, err="URLError: no network", mod="MyMod")
    assert "readback unavailable (URLError: no network)" in job["summary"]


def test_an_unsigned_pbo_is_a_note_not_a_refusal(tmp_path, monkeypatch):
    make_project(tmp_path, signed=False)
    started, job = publish(monkeypatch, FakeUploader(Outcome(ok=True, published_id=ITEM, step="done", result=1)),
                           item=fresh_item(), mod="MyMod")
    assert started["unsigned"] == ["addons/MyMod.pbo"]
    assert job["status"] == "done"
    assert "unsigned: addons/MyMod.pbo" in job["summary"] and "verifySignatures" in job["summary"]


# ------------------------------------------------------------------ create


def test_creating_is_private_by_default_and_reports_the_written_meta(tmp_path, monkeypatch):
    root = make_project(tmp_path, meta=None)
    uploader = FakeUploader(Outcome(ok=True, published_id=777, created=True, step="done", result=1),
                            write_meta=True)
    started, job = publish(monkeypatch, uploader, item=Item(id=777, result=9), mod="MyMod", title="My Mod")

    assert started["creating"] and started["item"] == 0 and started["url"] is None
    assert started["visibility"] == "private"
    assert uploader.spec.published_id == 0 and uploader.spec.visibility == 2
    assert uploader.spec.title == "My Mod"

    assert job["status"] == "done", job
    summary = job["summary"]
    assert "MyMod: created item 777" in summary
    assert "visibility private" in summary
    assert f"{META_NAME} written to @MyMod" in summary and "keep it with the sources" in summary
    assert "not visible to the public listing" in summary
    assert (root / "@MyMod" / META_NAME).read_text(encoding="utf-8") == render_meta(777, "My Mod")


def test_creating_can_be_public_when_asked(tmp_path, monkeypatch):
    make_project(tmp_path, meta=None)
    uploader = FakeUploader(Outcome(ok=True, published_id=778, created=True, step="done", result=1),
                            write_meta=True)
    started, job = publish(monkeypatch, uploader, item=fresh_item(778), mod="MyMod", title="M",
                           visibility="public")
    assert started["visibility"] == "public" and uploader.spec.visibility == 0
    assert job["status"] == "done" and "visibility public" in job["summary"]


def test_a_missing_meta_after_creation_is_said_with_the_lines_to_write(tmp_path, monkeypatch):
    """The uploader's word is not taken: the folder is read again, and a
    meta.cpp that is not there is named, with the three lines that would
    fix it, because without them the next publish creates a second item."""
    make_project(tmp_path, meta=None)
    uploader = FakeUploader(Outcome(ok=True, published_id=779, created=True, step="done", result=1),
                            write_meta=False)
    _, job = publish(monkeypatch, uploader, item=Item(id=779, result=9), mod="MyMod", title="M")
    assert job["status"] == "done"
    assert f"{META_NAME} is MISSING" in job["summary"]
    assert "publishedid = 779;" in job["summary"] and 'name = "M";' in job["summary"]


def test_the_legal_agreement_flag_reaches_the_summary(tmp_path, monkeypatch):
    make_project(tmp_path, meta=None)
    uploader = FakeUploader(Outcome(ok=True, published_id=780, created=True, needs_legal=True,
                                    step="done", result=1), write_meta=True)
    _, job = publish(monkeypatch, uploader, item=Item(id=780, result=9), mod="MyMod", title="M")
    assert job["status"] == "done" and LEGAL_URL in job["summary"]


# ------------------------------------------------------------ listing only


def test_a_listing_update_sends_no_content_and_needs_no_build(tmp_path, monkeypatch):
    """The way to fix a description without shipping whatever the build folder
    holds: no pbo is needed, only meta.cpp, and the summary says no content went."""
    root = make_project(tmp_path)
    (root / "@MyMod" / "addons" / "MyMod.pbo").unlink()
    uploader = FakeUploader(Outcome(ok=True, published_id=ITEM, step="done", result=1, seconds=3.0))
    listed = fresh_item(description="[b]Hello[/b]", tags=["Mod", "Mechanics"])
    started, job = publish(monkeypatch, uploader, item=listed, mod="MyMod",
                           description="[b]Hello[/b]", tags=["Mod", "Mechanics"], content=False)
    assert started["content"] is False
    assert job["status"] == "done", job
    assert uploader.spec.send_content is False and uploader.spec.published_id == ITEM
    assert uploader.spec.description == "[b]Hello[/b]" and uploader.spec.tags == ["Mod", "Mechanics"]
    summary = job["summary"]
    assert f"MyMod: updated the listing of item {ITEM}" in summary and "no content sent" in summary
    assert "description 12 chars" in summary and "tags [Mod, Mechanics]" in summary
    assert "matches this upload" not in summary


def test_a_listing_update_names_the_lag_of_the_public_listing(tmp_path, monkeypatch):
    """Steam's public API shows a new description minutes after the page does;
    a bare "0 chars" would read as a failed edit."""
    make_project(tmp_path)
    uploader = FakeUploader(Outcome(ok=True, published_id=ITEM, step="done", result=1))
    lagging = fresh_item(description="", tags=["Mod"])
    _, job = publish(monkeypatch, uploader, item=lagging, mod="MyMod", description="[b]New[/b]", content=False)
    assert job["status"] == "done"
    assert "not yet in the public listing" in job["summary"]
    assert "0 chars" not in job["summary"]


def test_a_listing_update_refuses_to_create_or_to_send_nothing(tmp_path, monkeypatch):
    make_project(tmp_path, meta=None)
    refused = tools.workshop_publish("MyMod", title="M", content=False)
    assert not refused.ok and "cannot create" in refused.error

    session.reset()
    make_project(tmp_path / "second")
    nothing = tools.workshop_publish("MyMod", content=False)
    assert not nothing.ok and "nothing to update" in nothing.error


# ----------------------------------------------------------------- failures


def test_a_failed_upload_fails_the_job_with_steams_reason(tmp_path, monkeypatch):
    make_project(tmp_path)
    uploader = FakeUploader(Outcome(ok=False, published_id=ITEM, step="submit", result=15,
                                    error="SubmitItemUpdate: AccessDenied -- this account does not own the item"))
    _, job = publish(monkeypatch, uploader, item=fresh_item(), mod="MyMod")
    assert job["status"] == "failed"
    assert "AccessDenied" in job["error"] and "(at submit)" in job["error"]
    assert LEGAL_URL not in job["error"]


def test_a_failure_after_creation_names_the_item_that_now_exists(tmp_path, monkeypatch):
    make_project(tmp_path, meta=None)
    uploader = FakeUploader(Outcome(ok=False, published_id=781, created=True, needs_legal=True,
                                    step="submit", result=2, error="SubmitItemUpdate: Fail"),
                            write_meta=True)
    _, job = publish(monkeypatch, uploader, item=None, mod="MyMod", title="M")
    assert job["status"] == "failed"
    assert "item 781 exists now" in job["error"] and "next publish is an update" in job["error"]
    assert LEGAL_URL in job["error"]


def test_an_uploader_that_leaves_no_result_fails_the_job(tmp_path, monkeypatch):
    make_project(tmp_path)
    uploader = FakeUploader(None, code=124, text="[dayz-mcp] timeout after 1920s\n")
    _, job = publish(monkeypatch, uploader, item=None, mod="MyMod")
    assert job["status"] == "failed"
    assert "without a result" in job["error"] and "exit 124" in job["error"] and "timeout" in job["error"]


def test_a_crash_in_the_worker_fails_the_job_instead_of_hanging(tmp_path, monkeypatch):
    make_project(tmp_path)

    def boom(*args):
        raise RuntimeError("no disk")

    monkeypatch.setattr(wtool, "run_uploader", boom)
    started = tools.workshop_publish("MyMod")
    assert started.ok
    job = tools.job_wait(started.data["job_id"], timeout=15).data
    assert job["status"] == "failed" and "RuntimeError: no disk" in job["error"]


# ------------------------------------------------------------ the process


def test_run_uploader_starts_the_module_under_the_games_app_id(tmp_path, monkeypatch):
    seen = {}

    def fake_run_blocking(cmd, cwd, log_path, timeout=None, env=None):
        seen.update(cmd=list(cmd), cwd=Path(cwd), log_path=Path(log_path), timeout=timeout, env=dict(env or {}))
        return 0, ""

    monkeypatch.setattr(wtool, "run_blocking", fake_run_blocking)
    code, _ = wtool.run_uploader(tmp_path / "spec.json", tmp_path / "result.json", tmp_path, tmp_path / "w.log")
    assert code == 0
    assert seen["cmd"][:3] == [sys.executable, "-m", "dayz_mcp.steamugc"]
    assert seen["cmd"][3:] == [str(tmp_path / "spec.json"), str(tmp_path / "result.json")]
    assert seen["env"] == {"SteamAppId": str(WORKSHOP_APP_ID), "SteamGameId": str(WORKSHOP_APP_ID)}
    assert seen["cwd"] == tmp_path
    assert seen["timeout"] > wtool.UPLOAD_TIMEOUT


# ------------------------------------------------------------------ status


def test_status_refuses_without_a_project_or_for_an_undeclared_mod(tmp_path):
    assert not tools.workshop_status("MyMod").ok
    make_project(tmp_path)
    assert not tools.workshop_status("Other").ok


def test_status_of_a_mod_that_is_not_built(tmp_path, monkeypatch):
    make_project(tmp_path, built=False)
    monkeypatch.setattr(wtool, "read_item", lambda published_id: (_ for _ in ()).throw(AssertionError("no network here")))
    result = tools.workshop_status("MyMod")
    assert result.ok
    assert result.data["built"] is False and result.data["item"] == 0
    assert result.data["remote"] is None and result.data["stale"] is None
    assert "mod_build" in result.data["note"]


def test_status_of_a_mod_never_published(tmp_path, monkeypatch):
    make_project(tmp_path, meta=None)
    monkeypatch.setattr(wtool, "read_item", lambda published_id: (_ for _ in ()).throw(AssertionError("no network here")))
    result = tools.workshop_status("MyMod")
    assert result.ok
    assert result.data["built"] and result.data["item"] == 0 and result.data["url"] is None
    assert "never published" in result.data["note"]


def test_status_reads_the_item_back_and_derives_staleness(tmp_path, monkeypatch):
    make_project(tmp_path)
    old = fresh_item(time_updated=int(time.time()) - 86400, subscriptions=3, visibility=0)
    monkeypatch.setattr(wtool, "read_item", lambda published_id: (old, ""))
    result = tools.workshop_status("MyMod")
    assert result.ok
    data = result.data
    assert data["item"] == ITEM and data["url"].endswith(str(ITEM))
    assert data["meta_name"] == "MyMod"
    assert data["pbos"] == ["addons/MyMod.pbo"] and data["unsigned"] == [] and data["keys"] == ["keys/Key.bikey"]
    assert data["remote"]["title"] == "MyMod" and data["remote"]["visibility_name"] == "public"
    assert data["remote"]["subscriptions"] == 3
    assert data["stale"] is True
    assert "does not carry this build" in data["note"]

    current = fresh_item(time_updated=int(time.time()) + 3600)
    monkeypatch.setattr(wtool, "read_item", lambda published_id: (current, ""))
    data = tools.workshop_status("MyMod").data
    assert data["stale"] is False and "carries this build" in data["note"]


def test_status_says_when_the_item_is_hidden_or_the_network_is_down(tmp_path, monkeypatch):
    make_project(tmp_path)
    monkeypatch.setattr(wtool, "read_item", lambda published_id: (Item(id=ITEM, result=9), ""))
    data = tools.workshop_status("MyMod").data
    assert data["remote"]["result"] == 9 and data["stale"] is None
    assert "not visible" in data["note"]

    monkeypatch.setattr(wtool, "read_item", lambda published_id: (None, "URLError: down"))
    data = tools.workshop_status("MyMod").data
    assert data["remote"] is None and data["remote_error"] == "URLError: down"
    assert "could not be read back" in data["note"]


def test_status_names_a_broken_meta(tmp_path, monkeypatch):
    make_project(tmp_path, meta="protocol = 1;\n")
    monkeypatch.setattr(wtool, "read_item", lambda published_id: (_ for _ in ()).throw(AssertionError("no network here")))
    data = tools.workshop_status("MyMod").data
    assert data["item"] == 0 and "publishedid" in data["meta_error"]
    assert "workshop_publish will refuse" in data["note"]
