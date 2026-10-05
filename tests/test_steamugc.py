"""`steamugc.py`: the upload driver, against a fake Steam.

The real DLL is not here and never will be: what these prove is that the
driver takes the steps in the order Publisher takes them (create, write
meta.cpp, set what was given, content, submit), that every refusal names its
step, and that the result file is written whatever happens -- including
when the DLL cannot be loaded at all, which is the one path exercised
through a real subprocess.
"""
from __future__ import annotations

import ctypes
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from dayz_mcp import steamugc
from dayz_mcp.steamugc import (
    ADD_DEPENDENCY_RESULT, CREATE_ITEM_RESULT, REMOVE_DEPENDENCY_RESULT, SUBMIT_ITEM_UPDATE_RESULT,
    UGC_QUERY_COMPLETED, CreateItemResult, DependencyResult, Outcome, Spec, SubmitItemUpdateResult,
    UGCQueryCompleted, run_upload,
)
from dayz_mcp.workshop import META_NAME, parse_meta


class FakeApi:
    """Steam as the driver sees it: call handles that complete, results in
    the structs Steam would fill, a required-items list that AddDependency
    and RemoveDependency edit, and a record of every call made."""

    def __init__(self, *, init_ok=True, create=(1, 777, False), submit=(1, False),
                 refuse=(), handle=5, fail_call=None, pending_polls=0, progress=None,
                 children=(), dep_refuse=()):
        self.init_ok = init_ok
        self.create_res = create
        self.submit_res = submit
        self.refuse = set(refuse)
        self.handle = handle
        self.fail_call = fail_call
        self.pending_polls = pending_polls
        self.progress_seq = list(progress or [])
        self.children: list[int] = list(children)
        self.dep_refuse = set(dep_refuse)
        self.calls: list[tuple] = []
        self.shutdown_called = False
        self.meta_seen_at_content: bool | None = None
        self._pending: dict[int, tuple] = {}
        self._polls: dict[int, int] = {}
        self._n = 100

    def _call(self, kind: str, *extra) -> int:
        self._n += 1
        self._pending[self._n] = (kind, *extra)
        self._polls[self._n] = 0
        return self._n

    def init(self):
        self.calls.append(("init",))
        return self.init_ok

    def shutdown(self):
        self.shutdown_called = True

    def run_callbacks(self):
        pass

    def create_item(self, app_id):
        self.calls.append(("create_item", app_id))
        return self._call("create")

    def start_update(self, app_id, published_id):
        self.calls.append(("start_update", app_id, published_id))
        return self.handle

    def _set(self, name, *args):
        self.calls.append((name, *args))
        return name not in self.refuse

    def set_title(self, h, t):
        return self._set("set_title", h, t)

    def set_description(self, h, t):
        return self._set("set_description", h, t)

    def set_visibility(self, h, v):
        return self._set("set_visibility", h, v)

    def set_tags(self, h, tags):
        return self._set("set_tags", h, list(tags))

    def set_preview(self, h, p):
        return self._set("set_preview", h, p)

    def add_preview_file(self, h, p):
        return self._set("add_preview_file", h, p)

    def set_content(self, h, folder):
        self.meta_seen_at_content = (Path(folder) / META_NAME).is_file()
        return self._set("set_content", h, folder)

    def submit(self, h, note):
        self.calls.append(("submit", h, note))
        return self._call("submit")

    def progress(self, h):
        return self.progress_seq.pop(0) if self.progress_seq else (5, 10, 10)

    # --- required items
    def add_dependency(self, parent, child):
        self.calls.append(("add_dependency", parent, child))
        return self._call("add_dep", parent, child)

    def remove_dependency(self, parent, child):
        self.calls.append(("remove_dependency", parent, child))
        return self._call("remove_dep", parent, child)

    def create_details_query(self, ids):
        self.calls.append(("details_query", list(ids)))
        return 9

    def set_return_children(self, h, on=True):
        return True

    def send_query(self, h):
        return self._call("query")

    def query_children(self, h, index=0, limit=64):
        return list(self.children)

    def release_query(self, h):
        self.calls.append(("release_query", h))

    # --- the async answers
    def completed(self, call):
        kind = self._pending[call][0]
        if self.fail_call == kind:
            return True, True
        if self._polls[call] < self.pending_polls:
            self._polls[call] += 1
            return False, False
        return True, False

    def failure_reason(self, call):
        return 1

    def result(self, call, struct_cls, callback_id):
        kind, *extra = self._pending[call]
        expected = {"create": CREATE_ITEM_RESULT, "submit": SUBMIT_ITEM_UPDATE_RESULT,
                    "add_dep": ADD_DEPENDENCY_RESULT, "remove_dep": REMOVE_DEPENDENCY_RESULT,
                    "query": UGC_QUERY_COMPLETED}
        assert expected[kind] == callback_id, (kind, callback_id)
        if kind == "create":
            code, pid, legal = self.create_res
            return CreateItemResult(code, pid, legal)
        if kind == "submit":
            code, legal = self.submit_res
            return SubmitItemUpdateResult(code, legal, 0)
        if kind == "query":
            return UGCQueryCompleted(9, 1, 1, 1, False, b"")
        parent, child = extra
        if child in self.dep_refuse:
            return DependencyResult(2, parent, child)
        if kind == "add_dep":
            self.children.append(child)
        else:
            self.children.remove(child)
        return DependencyResult(1, parent, child)

def a_spec(tmp_path: Path, **kw) -> Spec:
    content = tmp_path / "@MyMod"
    content.mkdir(exist_ok=True)
    fields = {"dll": str(tmp_path / "steam_api64.dll"), "content": str(content)} | kw
    return Spec(**fields)


def names(api: FakeApi) -> list[str]:
    return [c[0] for c in api.calls]


# ---------------------------------------------------------------- layouts


def test_result_structs_have_the_measured_layout():
    """24 and 16 bytes, measured 2026-09-19 against the game's DLL under
    pack(8): a wrong size here and GetAPICallResult refuses the buffer."""
    assert ctypes.sizeof(CreateItemResult) == 24
    assert CreateItemResult.published_id.offset == 8
    assert CreateItemResult.needs_legal.offset == 16
    assert ctypes.sizeof(SubmitItemUpdateResult) == 16
    assert SubmitItemUpdateResult.needs_legal.offset == 4
    assert SubmitItemUpdateResult.published_id.offset == 8
    assert CREATE_ITEM_RESULT == 3403 and SUBMIT_ITEM_UPDATE_RESULT == 3404


def test_dependency_structs_have_the_expected_layout():
    """pack(8) again: the two dependency results share one 24-byte layout, and the
    query result of ISteamUGC v017 ends in a 256-byte cursor, 280 bytes in all."""
    assert ctypes.sizeof(DependencyResult) == 24 and DependencyResult.child_id.offset == 16
    assert ctypes.sizeof(UGCQueryCompleted) == 280 and UGCQueryCompleted.cursor.offset == 21
    assert (UGC_QUERY_COMPLETED, ADD_DEPENDENCY_RESULT, REMOVE_DEPENDENCY_RESULT) == (3401, 3412, 3413)


def test_spec_and_outcome_round_trip_through_json(tmp_path):
    spec = a_spec(tmp_path, published_id=42, tags=["Mod"], visibility=0, changenote="v2")
    assert Spec.from_json(spec.to_json()) == spec
    out = Outcome(ok=True, published_id=42, step="done", result=1, seconds=3.5)
    assert Outcome.from_json(out.to_json()) == out


# ----------------------------------------------------------------- create


def test_create_writes_meta_before_content_and_reports_created(tmp_path):
    api = FakeApi(create=(1, 777, False))
    spec = a_spec(tmp_path, title="My Mod", visibility=2, changenote="first")
    log = []

    out = run_upload(api, spec, log=log.append, clock=lambda: 0.0, sleep=lambda s: None)

    assert out.ok and out.created and out.published_id == 777 and out.step == "done"
    assert names(api) == ["init", "create_item", "start_update", "set_title", "set_visibility",
                          "set_content", "submit"]
    assert ("create_item", spec.app_id) in api.calls
    assert ("start_update", spec.app_id, 777) in api.calls
    assert ("submit", 5, "first") in api.calls
    meta = Path(spec.content) / META_NAME
    assert out.meta_written == str(meta)
    assert parse_meta(meta.read_text(encoding="utf-8")).published_id == 777
    assert parse_meta(meta.read_text(encoding="utf-8")).name == "My Mod"
    assert meta.read_bytes().endswith(b'name = "My Mod";\n') and b"\r" not in meta.read_bytes()
    assert api.meta_seen_at_content is True, "meta.cpp must be in the folder when the content is set"
    assert api.shutdown_called
    assert any("created item 777" in line for line in log)


def test_create_failure_carries_steams_own_code(tmp_path):
    api = FakeApi(create=(15, 0, False))
    out = run_upload(api, a_spec(tmp_path, title="X"), log=lambda s: None, clock=lambda: 0.0, sleep=lambda s: None)
    assert not out.ok and out.step == "create" and out.result == 15
    assert "AccessDenied" in out.error and "own" in out.error
    assert not out.created and not (Path(a_spec(tmp_path).content) / META_NAME).exists()
    assert api.shutdown_called


def test_create_passes_the_legal_agreement_flag_through(tmp_path):
    api = FakeApi(create=(1, 778, True))
    log = []
    out = run_upload(api, a_spec(tmp_path, title="X"), log=log.append, clock=lambda: 0.0, sleep=lambda s: None)
    assert out.ok and out.needs_legal
    assert any("legal agreement" in line for line in log)


# ----------------------------------------------------------------- update


def test_update_sends_only_what_was_given(tmp_path):
    api = FakeApi()
    spec = a_spec(tmp_path, published_id=42, changenote="fix")
    out = run_upload(api, spec, log=lambda s: None, clock=lambda: 0.0, sleep=lambda s: None)
    assert out.ok and not out.created and out.published_id == 42
    assert names(api) == ["init", "start_update", "set_content", "submit"]
    assert ("start_update", spec.app_id, 42) in api.calls
    assert ("set_content", 5, spec.content) in api.calls
    assert not (Path(spec.content) / META_NAME).exists(), "an update never rewrites meta.cpp"


def test_update_sets_every_field_it_was_given(tmp_path):
    api = FakeApi()
    preview = tmp_path / "p.png"
    preview.write_bytes(b"png")
    spec = a_spec(tmp_path, published_id=42, title="T", description="D", visibility=0,
                  tags=["Mod", "Tools"], preview=str(preview))
    out = run_upload(api, spec, log=lambda s: None, clock=lambda: 0.0, sleep=lambda s: None)
    assert out.ok
    assert names(api) == ["init", "start_update", "set_title", "set_description", "set_visibility",
                          "set_tags", "set_preview", "set_content", "submit"]
    assert ("set_tags", 5, ["Mod", "Tools"]) in api.calls
    assert ("set_preview", 5, str(preview)) in api.calls


def test_a_listing_only_update_sends_no_content(tmp_path):
    api = FakeApi()
    spec = a_spec(tmp_path, published_id=42, title="T", tags=["Mod"], send_content=False)
    out = run_upload(api, spec, log=lambda s: None, clock=lambda: 0.0, sleep=lambda s: None)
    assert out.ok
    assert names(api) == ["init", "start_update", "set_title", "set_tags", "submit"]


def test_a_listing_only_update_with_nothing_to_set_is_refused_by_the_driver(tmp_path):
    api = FakeApi()
    out = run_upload(api, a_spec(tmp_path, published_id=42, send_content=False),
                     log=lambda s: None, clock=lambda: 0.0, sleep=lambda s: None)
    assert not out.ok and out.step == "update" and "nothing to send" in out.error
    assert "submit" not in names(api)


def test_a_refused_step_names_itself(tmp_path):
    api = FakeApi(refuse={"set_preview"})
    spec = a_spec(tmp_path, published_id=42, preview="x.png")
    out = run_upload(api, spec, log=lambda s: None, clock=lambda: 0.0, sleep=lambda s: None)
    assert not out.ok and out.step == "preview" and "refused the preview" in out.error
    assert "submit" not in names(api)
    assert api.shutdown_called


def test_no_update_handle_is_a_named_failure(tmp_path):
    api = FakeApi(handle=0)
    out = run_upload(api, a_spec(tmp_path, published_id=42), log=lambda s: None, clock=lambda: 0.0, sleep=lambda s: None)
    assert not out.ok and out.step == "start_update"


def test_submit_failure_carries_the_code_and_the_legal_flag(tmp_path):
    api = FakeApi(submit=(2, True))
    out = run_upload(api, a_spec(tmp_path, published_id=42), log=lambda s: None, clock=lambda: 0.0, sleep=lambda s: None)
    assert not out.ok and out.step == "submit" and out.result == 2 and out.needs_legal
    assert "Fail" in out.error


# --------------------------------------------------------- required items


def test_required_items_are_added_only_when_missing_and_read_back(tmp_path):
    api = FakeApi(children=[100])
    spec = a_spec(tmp_path, published_id=42, send_content=False, requires=[100, 200], remove_requires=[300])
    out = run_upload(api, spec, log=lambda s: None, clock=lambda: 0.0, sleep=lambda s: None)
    assert out.ok and out.step == "done"
    assert out.requires_added == [200] and out.requires_removed == [] and out.requires_now == [100, 200]
    assert "start_update" not in names(api) and "submit" not in names(api)
    assert ("add_dependency", 42, 200) in api.calls and ("add_dependency", 42, 100) not in api.calls
    assert ("remove_dependency", 42, 300) not in api.calls, "nothing to remove: it was not there"
    assert api.calls.count(("details_query", [42])) == 2, "read before, read back after"


def test_required_items_are_removed_when_present(tmp_path):
    api = FakeApi(children=[100, 300])
    out = run_upload(api, a_spec(tmp_path, published_id=42, send_content=False, remove_requires=[300]),
                     log=lambda s: None, clock=lambda: 0.0, sleep=lambda s: None)
    assert out.ok and out.requires_removed == [300] and out.requires_now == [100]


def test_a_refused_required_item_fails_the_run_after_the_update(tmp_path):
    api = FakeApi(dep_refuse={200})
    out = run_upload(api, a_spec(tmp_path, published_id=42, title="T", send_content=False, requires=[200]),
                     log=lambda s: None, clock=lambda: 0.0, sleep=lambda s: None)
    assert not out.ok and out.step == "requires"
    assert out.requires_failed == [[200, 2]] and "Fail" in out.error
    assert "submit" in names(api) and "update itself went through" in out.error


# ------------------------------------------------------------------ waits


def test_init_failure_names_the_client_and_never_shuts_down(tmp_path):
    api = FakeApi(init_ok=False)
    out = run_upload(api, a_spec(tmp_path, published_id=42), log=lambda s: None, clock=lambda: 0.0, sleep=lambda s: None)
    assert not out.ok and out.step == "init"
    assert "Steam client is not running" in out.error
    assert names(api) == ["init"]
    assert not api.shutdown_called, "there is nothing to shut down after a failed Init"


def test_a_failed_call_names_steams_reason(tmp_path):
    api = FakeApi(fail_call="create")
    out = run_upload(api, a_spec(tmp_path, title="X"), log=lambda s: None, clock=lambda: 0.0, sleep=lambda s: None)
    assert not out.ok and out.step == "create"
    assert "connection to Steam broke" in out.error


def test_progress_is_logged_only_when_it_changes(tmp_path):
    api = FakeApi(pending_polls=4, progress=[
        (2, 0, 0), (3, 500_000, 1_000_000), (3, 500_000, 1_000_000), (5, 1_000_000, 1_000_000),
    ])
    log = []
    out = run_upload(api, a_spec(tmp_path, published_id=42), log=log.append, clock=lambda: 0.0, sleep=lambda s: None)
    assert out.ok
    assert out.bytes_total == 1_000_000
    progress = [line for line in log if "MB" in line or line == "preparing content"]
    assert progress == ["preparing content", "uploading content 500.0 KB / 1.0 MB", "committing 1.0 MB / 1.0 MB"]


def test_sizes_read_as_bytes_kilobytes_or_megabytes():
    assert steamugc._size(58) == "58 B"
    assert steamugc._size(58_000) == "58.0 KB"
    assert steamugc._size(556_185) == "556.2 KB"
    assert steamugc._size(12_345_678) == "12.3 MB"


def test_a_submit_that_never_answers_times_out_by_the_spec(tmp_path):
    api = FakeApi(pending_polls=10_000)
    ticks = iter(range(0, 100_000))
    slept = []
    out = run_upload(api, a_spec(tmp_path, published_id=42, timeout=3),
                     log=lambda s: None, clock=lambda: float(next(ticks)), sleep=slept.append)
    assert not out.ok and out.step == "submit"
    assert "within 3 s" in out.error
    assert slept and all(s == steamugc.POLL_SECONDS for s in slept)
    assert api.shutdown_called


# ------------------------------------------------------------------- main


def test_main_writes_a_result_even_when_the_dll_cannot_load(tmp_path):
    """The one real subprocess in this file: the tool reads result.json, so
    a run that cannot even load the DLL must still leave one, and exit 1."""
    spec = a_spec(tmp_path, published_id=42)
    spec_path, result_path = tmp_path / "spec.json", tmp_path / "result.json"
    spec_path.write_text(spec.to_json(), encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, "-m", "dayz_mcp.steamugc", str(spec_path), str(result_path)],
        cwd=tmp_path, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 1, proc.stdout + proc.stderr
    out = Outcome.from_json(result_path.read_text(encoding="utf-8"))
    assert not out.ok and out.step == "load"
    assert "steam_api64.dll" in out.error
    assert "[load]" in proc.stdout


def test_main_refuses_a_wrong_argument_count(capsys):
    assert steamugc.main([]) == 2
    assert "usage" in capsys.readouterr().out


def test_main_survives_a_driver_crash(tmp_path, monkeypatch):
    spec = a_spec(tmp_path, published_id=42)
    spec_path, result_path = tmp_path / "spec.json", tmp_path / "result.json"
    spec_path.write_text(spec.to_json(), encoding="utf-8")
    monkeypatch.setattr(steamugc, "SteamApi", lambda dll: object())
    monkeypatch.setattr(steamugc, "run_upload", lambda api, spec, log: (_ for _ in ()).throw(RuntimeError("boom")))
    assert steamugc.main([str(spec_path), str(result_path)]) == 1
    out = json.loads(result_path.read_text(encoding="utf-8"))
    assert out["step"] == "crash" and "boom" in out["error"]


def test_main_logs_a_change_note_the_console_code_page_cannot_spell(tmp_path, monkeypatch):
    """The log is a file the tool reads back as UTF-8, but a redirected stdout
    takes the machine's ANSI code page. Measured on a real update: a change
    note with a Ukrainian paragraph killed the run on the log line announcing
    its own submit -- `UnicodeEncodeError: 'charmap' codec can't encode` --
    after the content was set and before anything was sent. What the console
    can spell must never decide whether an upload happens."""
    note = "New: the old fridge. Нове: старий холодильник."
    spec = a_spec(tmp_path, published_id=42, changenote=note)
    spec_path, result_path = tmp_path / "spec.json", tmp_path / "result.json"
    spec_path.write_text(spec.to_json(), encoding="utf-8")
    api = FakeApi()
    monkeypatch.setattr(steamugc, "SteamApi", lambda dll: api)
    raw = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(raw, encoding="cp1252"))

    code = steamugc.main([str(spec_path), str(result_path)])

    out = Outcome.from_json(result_path.read_text(encoding="utf-8"))
    assert out.ok, out.error
    assert code == 0
    assert ("submit", api.handle, note) in api.calls
    assert "старий холодильник" in raw.getvalue().decode("utf-8")


def test_additional_previews_are_added_after_the_preview_and_before_the_content(tmp_path):
    api = FakeApi()
    main = tmp_path / "p.png"
    main.write_bytes(b"png")
    shots = [tmp_path / "a.jpg", tmp_path / "b.jpg"]
    for shot in shots:
        shot.write_bytes(b"jpg")
    spec = a_spec(tmp_path, published_id=42, preview=str(main), previews=[str(s) for s in shots])
    out = run_upload(api, spec, log=lambda s: None, clock=lambda: 0.0, sleep=lambda s: None)
    assert out.ok
    assert names(api) == ["init", "start_update", "set_preview", "add_preview_file",
                          "add_preview_file", "set_content", "submit"]
    assert ("add_preview_file", 5, str(shots[0])) in api.calls
    assert ("add_preview_file", 5, str(shots[1])) in api.calls


def test_additional_previews_alone_are_a_listing_update(tmp_path):
    api = FakeApi()
    spec = a_spec(tmp_path, published_id=42, previews=[str(tmp_path / "a.jpg")], send_content=False)
    out = run_upload(api, spec, log=lambda s: None, clock=lambda: 0.0, sleep=lambda s: None)
    assert out.ok
    assert names(api) == ["init", "start_update", "add_preview_file", "submit"]


def test_a_refused_additional_preview_names_the_file(tmp_path):
    api = FakeApi(refuse={"add_preview_file"})
    spec = a_spec(tmp_path, published_id=42, previews=[str(tmp_path / "shot.jpg")])
    out = run_upload(api, spec, log=lambda s: None, clock=lambda: 0.0, sleep=lambda s: None)
    assert not out.ok and out.step == "preview shot.jpg" and "refused the preview shot.jpg" in out.error
    assert "submit" not in names(api)
