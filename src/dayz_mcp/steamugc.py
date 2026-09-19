"""Uploading one Workshop item through the running Steam client.

    python -m dayz_mcp.steamugc <spec.json> <result.json>

This is the ONLY module that talks to Steam, and it runs as its own short
process, never inside the MCP server -- `tools/workshop.py` starts it and
reads `result.json` back. Three reasons, each sufficient:

* `SteamAPI_Init` marks the account "in game" for the process's lifetime.
  Done inside the server, the owner would show as playing DayZ for as long
  as the server lives; done here, for as long as the upload takes.
* A fault inside `steam_api64.dll` takes its process down. Here that is one
  failed job with a log; inside the server it would be every stand and
  every job the server was minding.
* `SteamAPI_Shutdown` followed by a second `SteamAPI_Init` in one process is
  not something Valve supports. One process per upload never needs it.

No Steamworks SDK is shipped or needed: the DLL is the game's own
`steam_api64.dll`, found beside the game, exactly as this server finds
FileBank beside the tools. It exports the flat C API (`SteamAPI_ISteamUGC_*`,
`SteamAPI_ISteamUtils_*`) that ctypes binds below; the accessor for each
interface is looked up by trying the versions this code knows, newest first,
so a DLL from a newer game build still answers. Measured 2026-09-19 against
the game's DLL (ISteamUGC v017): with `SteamAppId`/`SteamGameId` in the
environment and the Steam client running, `SteamAPI_Init` succeeds from a
plain Python process, and the result structs are 24 and 16 bytes, as
`#pragma pack(push, 8)` on Windows x64 says they must be.

The app id is the game's, because the Workshop is the game's: Publisher runs
under the same number.
"""
from __future__ import annotations

import ctypes
import json
import sys
import time
from ctypes import POINTER, byref, c_bool, c_char_p, c_int, c_uint32, c_uint64, c_void_p
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .workshop import META_NAME, WORKSHOP_APP_ID, eresult_name, explain, render_meta

#: k_iSteamUGCCallbacks (steam_api_internal.h) and the two call results this
#: module waits for: CreateItemResult_t = +3, SubmitItemUpdateResult_t = +4.
UGC_CALLBACKS = 3400
CREATE_ITEM_RESULT = UGC_CALLBACKS + 3
SUBMIT_ITEM_UPDATE_RESULT = UGC_CALLBACKS + 4
#: The required-items side: SteamUGCQueryCompleted_t = +1 (the details query that
#: reads them back), AddUGCDependencyResult_t = +12, RemoveUGCDependencyResult_t = +13.
UGC_QUERY_COMPLETED = UGC_CALLBACKS + 1
ADD_DEPENDENCY_RESULT = UGC_CALLBACKS + 12
REMOVE_DEPENDENCY_RESULT = UGC_CALLBACKS + 13
#: How many required items one query reads back. Steam's own pages list a handful.
MAX_CHILDREN = 64
#: EWorkshopFileType: a mod is a community file, the first value.
FILE_TYPE_COMMUNITY = 0
#: EItemUpdateStatus, for the progress line.
UPDATE_STATUS = {
    0: "waiting", 1: "preparing config", 2: "preparing content",
    3: "uploading content", 4: "uploading preview", 5: "committing",
}
#: Interface versions to try for the accessors, newest first. v017 is what
#: the game's DLL carries today; older is what an older game build might.
UGC_VERSIONS = tuple(range(21, 9, -1))
UTILS_VERSIONS = tuple(range(11, 7, -1))
#: How long a CreateItem answer may take, and how often the driver looks.
CREATE_TIMEOUT = 60.0
POLL_SECONDS = 0.5


class CreateItemResult(ctypes.Structure):
    _fields_ = [("result", c_int), ("published_id", c_uint64), ("needs_legal", c_bool)]


class SubmitItemUpdateResult(ctypes.Structure):
    _fields_ = [("result", c_int), ("needs_legal", c_bool), ("published_id", c_uint64)]


class StringArray(ctypes.Structure):
    """SteamParamStringArray_t."""
    _fields_ = [("strings", POINTER(c_char_p)), ("count", c_int)]


class DependencyResult(ctypes.Structure):
    """AddUGCDependencyResult_t and RemoveUGCDependencyResult_t: one layout."""
    _fields_ = [("result", c_int), ("published_id", c_uint64), ("child_id", c_uint64)]


class UGCQueryCompleted(ctypes.Structure):
    """SteamUGCQueryCompleted_t of ISteamUGC v017: the cursor field makes it 280 bytes."""
    _fields_ = [("handle", c_uint64), ("result", c_int), ("returned", c_uint32), ("total", c_uint32),
                ("cached", c_bool), ("cursor", ctypes.c_char * 256)]


@dataclass
class Spec:
    """Everything one upload needs, written by the tool, read by this process."""
    dll: str
    content: str
    app_id: int = WORKSHOP_APP_ID
    published_id: int = 0
    title: str = ""
    description: str = ""
    preview: str = ""
    visibility: int | None = None
    tags: list[str] = field(default_factory=list)
    changenote: str = ""
    timeout: float = 1800.0
    #: False sends the fields above and not the folder: the listing, not the files.
    send_content: bool = True
    #: Workshop item ids to put into, and take out of, the item's Required Items.
    requires: list[int] = field(default_factory=list)
    remove_requires: list[int] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=2)

    @classmethod
    def from_json(cls, text: str) -> Spec:
        return cls(**json.loads(text))


@dataclass
class Outcome:
    """What happened, in a shape the tool can judge without reading the log."""
    ok: bool = False
    published_id: int = 0
    created: bool = False
    step: str = ""
    result: int = 0
    error: str = ""
    needs_legal: bool = False
    bytes_total: int = 0
    seconds: float = 0.0
    meta_written: str = ""
    requires_now: list[int] = field(default_factory=list)
    requires_added: list[int] = field(default_factory=list)
    requires_removed: list[int] = field(default_factory=list)
    #: [child id, EResult] per refused change; 0 when Steam gave no answer at all.
    requires_failed: list[list] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=2)

    @classmethod
    def from_json(cls, text: str) -> Outcome:
        return cls(**json.loads(text))


# ------------------------------------------------------------------ the DLL


class SteamApi:
    """The flat Steamworks API, bound with ctypes, narrowed to what an upload
    uses. Every method is a thin call; the driver below is what decides."""

    def __init__(self, dll_path: str | Path) -> None:
        self._dll = ctypes.WinDLL(str(dll_path)) if sys.platform == "win32" else ctypes.CDLL(str(dll_path))
        d = self._dll
        d.SteamAPI_Init.restype = c_bool
        d.SteamAPI_Init.argtypes = []
        d.SteamAPI_Shutdown.restype = None
        d.SteamAPI_RunCallbacks.restype = None
        d.SteamAPI_ISteamUGC_CreateItem.restype = c_uint64
        d.SteamAPI_ISteamUGC_CreateItem.argtypes = [c_void_p, c_uint32, c_int]
        d.SteamAPI_ISteamUGC_StartItemUpdate.restype = c_uint64
        d.SteamAPI_ISteamUGC_StartItemUpdate.argtypes = [c_void_p, c_uint32, c_uint64]
        for name in ("SetItemTitle", "SetItemDescription", "SetItemContent", "SetItemPreview"):
            fn = getattr(d, f"SteamAPI_ISteamUGC_{name}")
            fn.restype = c_bool
            fn.argtypes = [c_void_p, c_uint64, c_char_p]
        d.SteamAPI_ISteamUGC_SetItemVisibility.restype = c_bool
        d.SteamAPI_ISteamUGC_SetItemVisibility.argtypes = [c_void_p, c_uint64, c_int]
        # The fourth argument (bAllowAdminTags) arrived after v017. Under the
        # x64 calling convention an argument the callee never declared is
        # simply not read, so passing it to an older DLL costs nothing, and
        # not passing it to a newer one would hand it whatever was in r9.
        d.SteamAPI_ISteamUGC_SetItemTags.restype = c_bool
        d.SteamAPI_ISteamUGC_SetItemTags.argtypes = [c_void_p, c_uint64, POINTER(StringArray), c_bool]
        d.SteamAPI_ISteamUGC_SubmitItemUpdate.restype = c_uint64
        d.SteamAPI_ISteamUGC_SubmitItemUpdate.argtypes = [c_void_p, c_uint64, c_char_p]
        d.SteamAPI_ISteamUGC_GetItemUpdateProgress.restype = c_int
        d.SteamAPI_ISteamUGC_GetItemUpdateProgress.argtypes = [
            c_void_p, c_uint64, POINTER(c_uint64), POINTER(c_uint64),
        ]
        d.SteamAPI_ISteamUtils_IsAPICallCompleted.restype = c_bool
        d.SteamAPI_ISteamUtils_IsAPICallCompleted.argtypes = [c_void_p, c_uint64, POINTER(c_bool)]
        d.SteamAPI_ISteamUtils_GetAPICallResult.restype = c_bool
        d.SteamAPI_ISteamUtils_GetAPICallResult.argtypes = [
            c_void_p, c_uint64, c_void_p, c_int, c_int, POINTER(c_bool),
        ]
        d.SteamAPI_ISteamUtils_GetAPICallFailureReason.restype = c_int
        d.SteamAPI_ISteamUtils_GetAPICallFailureReason.argtypes = [c_void_p, c_uint64]
        for name in ("AddDependency", "RemoveDependency"):
            fn = getattr(d, f"SteamAPI_ISteamUGC_{name}")
            fn.restype = c_uint64
            fn.argtypes = [c_void_p, c_uint64, c_uint64]
        d.SteamAPI_ISteamUGC_CreateQueryUGCDetailsRequest.restype = c_uint64
        d.SteamAPI_ISteamUGC_CreateQueryUGCDetailsRequest.argtypes = [c_void_p, POINTER(c_uint64), c_uint32]
        d.SteamAPI_ISteamUGC_SetReturnChildren.restype = c_bool
        d.SteamAPI_ISteamUGC_SetReturnChildren.argtypes = [c_void_p, c_uint64, c_bool]
        d.SteamAPI_ISteamUGC_SendQueryUGCRequest.restype = c_uint64
        d.SteamAPI_ISteamUGC_SendQueryUGCRequest.argtypes = [c_void_p, c_uint64]
        d.SteamAPI_ISteamUGC_GetQueryUGCChildren.restype = c_bool
        d.SteamAPI_ISteamUGC_GetQueryUGCChildren.argtypes = [c_void_p, c_uint64, c_uint32, POINTER(c_uint64), c_uint32]
        d.SteamAPI_ISteamUGC_ReleaseQueryUGCRequest.restype = c_bool
        d.SteamAPI_ISteamUGC_ReleaseQueryUGCRequest.argtypes = [c_void_p, c_uint64]
        self._ugc: int | None = None
        self._utils: int | None = None
        self._keep: list = []

    def _accessor(self, prefix: str, versions) -> int:
        for v in versions:
            fn = getattr(self._dll, f"{prefix}_v{v:03d}", None)
            if fn is None:
                continue
            fn.restype = c_void_p
            fn.argtypes = []
            ptr = fn()
            if ptr:
                return ptr
        raise RuntimeError(
            f"no {prefix} accessor this code knows in the DLL "
            f"(tried v{versions[0]:03d}..v{versions[-1]:03d})"
        )

    def init(self) -> bool:
        return bool(self._dll.SteamAPI_Init())

    def shutdown(self) -> None:
        self._dll.SteamAPI_Shutdown()

    def run_callbacks(self) -> None:
        self._dll.SteamAPI_RunCallbacks()

    @property
    def ugc(self) -> int:
        if self._ugc is None:
            self._ugc = self._accessor("SteamAPI_SteamUGC", UGC_VERSIONS)
        return self._ugc

    @property
    def utils(self) -> int:
        if self._utils is None:
            self._utils = self._accessor("SteamAPI_SteamUtils", UTILS_VERSIONS)
        return self._utils

    def create_item(self, app_id: int) -> int:
        return self._dll.SteamAPI_ISteamUGC_CreateItem(self.ugc, app_id, FILE_TYPE_COMMUNITY)

    def start_update(self, app_id: int, published_id: int) -> int:
        return self._dll.SteamAPI_ISteamUGC_StartItemUpdate(self.ugc, app_id, published_id)

    def set_title(self, handle: int, text: str) -> bool:
        return self._dll.SteamAPI_ISteamUGC_SetItemTitle(self.ugc, handle, text.encode("utf-8"))

    def set_description(self, handle: int, text: str) -> bool:
        return self._dll.SteamAPI_ISteamUGC_SetItemDescription(self.ugc, handle, text.encode("utf-8"))

    def set_visibility(self, handle: int, visibility: int) -> bool:
        return self._dll.SteamAPI_ISteamUGC_SetItemVisibility(self.ugc, handle, visibility)

    def set_tags(self, handle: int, tags: list[str]) -> bool:
        encoded = [t.encode("utf-8") for t in tags]
        arr = (c_char_p * len(encoded))(*encoded)
        param = StringArray(ctypes.cast(arr, POINTER(c_char_p)), len(encoded))
        self._keep.extend([encoded, arr, param])
        return self._dll.SteamAPI_ISteamUGC_SetItemTags(self.ugc, handle, byref(param), False)

    def set_content(self, handle: int, folder: str) -> bool:
        return self._dll.SteamAPI_ISteamUGC_SetItemContent(self.ugc, handle, folder.encode("utf-8"))

    def set_preview(self, handle: int, path: str) -> bool:
        return self._dll.SteamAPI_ISteamUGC_SetItemPreview(self.ugc, handle, path.encode("utf-8"))

    def submit(self, handle: int, changenote: str) -> int:
        return self._dll.SteamAPI_ISteamUGC_SubmitItemUpdate(self.ugc, handle, changenote.encode("utf-8"))

    def progress(self, handle: int) -> tuple[int, int, int]:
        done, total = c_uint64(0), c_uint64(0)
        status = self._dll.SteamAPI_ISteamUGC_GetItemUpdateProgress(self.ugc, handle, byref(done), byref(total))
        return int(status), done.value, total.value

    def completed(self, call: int) -> tuple[bool, bool]:
        failed = c_bool(False)
        done = self._dll.SteamAPI_ISteamUtils_IsAPICallCompleted(self.utils, call, byref(failed))
        return bool(done), bool(failed.value)

    def failure_reason(self, call: int) -> int:
        return int(self._dll.SteamAPI_ISteamUtils_GetAPICallFailureReason(self.utils, call))

    def add_dependency(self, parent: int, child: int) -> int:
        return self._dll.SteamAPI_ISteamUGC_AddDependency(self.ugc, parent, child)

    def remove_dependency(self, parent: int, child: int) -> int:
        return self._dll.SteamAPI_ISteamUGC_RemoveDependency(self.ugc, parent, child)

    def create_details_query(self, ids: list[int]) -> int:
        arr = (c_uint64 * len(ids))(*ids)
        self._keep.append(arr)
        return self._dll.SteamAPI_ISteamUGC_CreateQueryUGCDetailsRequest(self.ugc, arr, len(ids))

    def set_return_children(self, handle: int, on: bool = True) -> bool:
        return self._dll.SteamAPI_ISteamUGC_SetReturnChildren(self.ugc, handle, on)

    def send_query(self, handle: int) -> int:
        return self._dll.SteamAPI_ISteamUGC_SendQueryUGCRequest(self.ugc, handle)

    def query_children(self, handle: int, index: int = 0, limit: int = MAX_CHILDREN) -> list[int]:
        arr = (c_uint64 * limit)()
        if not self._dll.SteamAPI_ISteamUGC_GetQueryUGCChildren(self.ugc, handle, index, arr, limit):
            return []
        return [int(v) for v in arr if v]

    def release_query(self, handle: int) -> None:
        self._dll.SteamAPI_ISteamUGC_ReleaseQueryUGCRequest(self.ugc, handle)

    def result(self, call: int, struct_cls, callback_id: int):
        out = struct_cls()
        failed = c_bool(False)
        got = self._dll.SteamAPI_ISteamUtils_GetAPICallResult(
            self.utils, call, byref(out), ctypes.sizeof(out), callback_id, byref(failed),
        )
        if not got or failed.value:
            return None
        return out


# --------------------------------------------------------------- the driver

FAILURE_REASON = {
    -1: "none", 0: "the Steam client went away", 1: "the connection to Steam broke",
    2: "the call handle is not valid", 3: "the result did not match the call",
}


def _wait(api, call: int, struct_cls, callback_id: int, timeout: float, clock, sleep, tick=None):
    """Poll one SteamAPICall_t to its result, or say why there is none."""
    if not call:
        return None, "Steam returned no call handle"
    deadline = clock() + timeout
    while True:
        api.run_callbacks()
        done, failed = api.completed(call)
        if failed:
            reason = api.failure_reason(call)
            return None, f"the call failed: {FAILURE_REASON.get(reason, f'reason {reason}')}"
        if done:
            out = api.result(call, struct_cls, callback_id)
            if out is None:
                reason = api.failure_reason(call)
                return None, f"Steam gave no result: {FAILURE_REASON.get(reason, f'reason {reason}')}"
            return out, ""
        if tick:
            tick()
        if clock() >= deadline:
            return None, f"no answer from Steam within {timeout:.0f} s"
        sleep(POLL_SECONDS)


def _size(n: int) -> str:
    """Steam sends only the files that changed -- the first live update of an
    unchanged mod moved one 58-byte file -- so a line in megabytes would read
    "0.0 MB / 0.0 MB" for most updates."""
    if n < 1000:
        return f"{n} B"
    if n < 1_000_000:
        return f"{n / 1000:.1f} KB"
    return f"{n / 1_000_000:.1f} MB"


def _children(api, item: int, clock, sleep) -> tuple[list[int] | None, str]:
    """The item's Required Items as Steam lists them now, through a details
    query with children, or None and why not."""
    handle = api.create_details_query([item])
    if not handle:
        return None, "CreateQueryUGCDetailsRequest returned no handle"
    api.set_return_children(handle, True)
    res, err = _wait(api, api.send_query(handle), UGCQueryCompleted, UGC_QUERY_COMPLETED,
                     CREATE_TIMEOUT, clock, sleep)
    try:
        if res is None:
            return None, err
        if res.result != 1:
            return None, f"details query: {eresult_name(res.result)}"
        return (api.query_children(handle, 0) if res.returned else []), ""
    finally:
        api.release_query(handle)


def _change_requires(api, spec: Spec, out: Outcome, log, clock, sleep) -> str:
    """Add what is missing, remove what is present, read the list back. Steam
    is asked only about the difference, so a repeated call changes nothing."""
    current, err = _children(api, out.published_id, clock, sleep)
    if current is None:
        return f"cannot read the required items: {err}"
    log(f"required items before: {current or 'none'}")
    for child in spec.requires:
        if child in current or child in out.requires_added:
            continue
        res, err = _wait(api, api.add_dependency(out.published_id, child), DependencyResult,
                         ADD_DEPENDENCY_RESULT, CREATE_TIMEOUT, clock, sleep)
        if res is None or res.result != 1:
            out.requires_failed.append([child, 0 if res is None else int(res.result)])
            log(f"require {child}: {err if res is None else eresult_name(res.result)}")
        else:
            out.requires_added.append(child)
            log(f"required item added: {child}")
    for child in spec.remove_requires:
        if child not in current:
            continue
        res, err = _wait(api, api.remove_dependency(out.published_id, child), DependencyResult,
                         REMOVE_DEPENDENCY_RESULT, CREATE_TIMEOUT, clock, sleep)
        if res is None or res.result != 1:
            out.requires_failed.append([child, 0 if res is None else int(res.result)])
            log(f"unrequire {child}: {err if res is None else eresult_name(res.result)}")
        else:
            out.requires_removed.append(child)
            log(f"required item removed: {child}")
    after, err = _children(api, out.published_id, clock, sleep)
    if after is None:
        log(f"required items could not be read back: {err}")
        out.requires_now = sorted((set(current) | set(out.requires_added)) - set(out.requires_removed))
    else:
        out.requires_now = after
        log(f"required items now: {after or 'none'}")
    if out.requires_failed:
        return "required item change refused: " + ", ".join(
            f"{child} ({eresult_name(code) if code else 'no answer from Steam'})"
            for child, code in out.requires_failed)
    return ""


def run_upload(api, spec: Spec, log=print, clock=time.monotonic, sleep=time.sleep) -> Outcome:
    """Create the item if there is none, send one update with whatever was
    given, then change its required items. Every step that can refuse names
    itself in the outcome."""
    started = clock()
    out = Outcome(published_id=spec.published_id)

    def finish(step: str, error: str, result: int = 0) -> Outcome:
        out.step, out.error, out.result = step, error, result
        out.seconds = clock() - started
        log(f"[{step}] {error}")
        return out

    wants_update = bool(spec.send_content or spec.title or spec.description or spec.preview
                        or spec.tags or spec.visibility is not None)
    wants_requires = bool(spec.requires or spec.remove_requires)
    if not spec.published_id and not spec.send_content:
        return finish("update", "a new item needs its content")
    if not wants_update and not wants_requires:
        return finish("update", "nothing to send: no content, no field to set, no required item to change")

    if not api.init():
        return finish(
            "init",
            "SteamAPI_Init failed: the Steam client is not running, is not logged in, "
            "or the account it is logged into does not own the game",
        )
    try:
        if not out.published_id:
            log(f"creating a new item for app {spec.app_id}")
            res, err = _wait(api, api.create_item(spec.app_id), CreateItemResult, CREATE_ITEM_RESULT,
                             CREATE_TIMEOUT, clock, sleep)
            if res is None:
                return finish("create", err)
            if res.result != 1:
                out.needs_legal = bool(res.needs_legal)
                return finish("create", f"CreateItem: {eresult_name(res.result)} -- {explain(res.result) or 'no detail'}",
                              res.result)
            out.published_id = int(res.published_id)
            out.created = True
            out.needs_legal = bool(res.needs_legal)
            log(f"created item {out.published_id}")
            # meta.cpp BEFORE the content goes up, so what subscribers get
            # carries the id -- the same order Publisher works in.
            meta = Path(spec.content) / META_NAME
            meta.write_text(render_meta(out.published_id, spec.title), encoding="utf-8", newline="\n")
            out.meta_written = str(meta)
            log(f"wrote {meta}")
            if out.needs_legal:
                log("Steam says this account has not accepted the Workshop legal agreement; "
                    "the item stays hidden until it is")

        if wants_update:
            handle = api.start_update(spec.app_id, out.published_id)
            if not handle:
                return finish("start_update", "StartItemUpdate returned no handle")
            steps = []
            if spec.title:
                steps.append(("title", lambda: api.set_title(handle, spec.title)))
            if spec.description:
                steps.append(("description", lambda: api.set_description(handle, spec.description)))
            if spec.visibility is not None:
                steps.append(("visibility", lambda: api.set_visibility(handle, spec.visibility)))
            if spec.tags:
                steps.append(("tags", lambda: api.set_tags(handle, list(spec.tags))))
            if spec.preview:
                steps.append(("preview", lambda: api.set_preview(handle, spec.preview)))
            if spec.send_content:
                steps.append(("content", lambda: api.set_content(handle, spec.content)))
            for name, call in steps:
                if not call():
                    return finish(name, f"Steam refused the {name} for this update")
                log(f"set {name}")

            log(f"submitting item {out.published_id}"
                + ("" if spec.send_content else " (listing only, no content)")
                + (f": {spec.changenote}" if spec.changenote else ""))
            last = {"line": ""}

            def tick() -> None:
                status, done, total = api.progress(handle)
                out.bytes_total = max(out.bytes_total, total)
                line = f"{UPDATE_STATUS.get(status, f'status {status}')} {_size(done)} / {_size(total)}" if total \
                    else UPDATE_STATUS.get(status, f"status {status}")
                if line != last["line"]:
                    last["line"] = line
                    log(line)

            res, err = _wait(api, api.submit(handle, spec.changenote), SubmitItemUpdateResult,
                             SUBMIT_ITEM_UPDATE_RESULT, spec.timeout, clock, sleep, tick)
            if res is None:
                return finish("submit", err)
            out.needs_legal = out.needs_legal or bool(res.needs_legal)
            if res.result != 1:
                return finish("submit", f"SubmitItemUpdate: {eresult_name(res.result)} -- {explain(res.result) or 'no detail'}",
                              res.result)
            log(f"item {out.published_id} {'created' if out.created else 'updated'} in {clock() - started:.0f} s")

        if wants_requires:
            err = _change_requires(api, spec, out, log, clock, sleep)
            if err:
                return finish("requires", err + (" -- the update itself went through" if wants_update else ""))

        out.ok = True
        out.result = 1
        out.step = "done"
        out.seconds = clock() - started
        log(f"done in {out.seconds:.0f} s")
        return out
    finally:
        api.shutdown()


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2:
        print("usage: python -m dayz_mcp.steamugc <spec.json> <result.json>", flush=True)
        return 2
    spec_path, result_path = Path(argv[0]), Path(argv[1])
    spec = Spec.from_json(spec_path.read_text(encoding="utf-8"))

    def log(line: str) -> None:
        print(line, flush=True)

    try:
        api = SteamApi(spec.dll)
    except (OSError, AttributeError) as exc:
        out = Outcome(published_id=spec.published_id, step="load", error=f"cannot use {spec.dll}: {exc}")
        log(f"[load] {out.error}")
    else:
        try:
            out = run_upload(api, spec, log=log)
        except Exception as exc:  # noqa: BLE001 - the result file must still be written
            out = Outcome(published_id=spec.published_id, step="crash", error=f"{type(exc).__name__}: {exc}")
            log(f"[crash] {out.error}")
    result_path.write_text(out.to_json(), encoding="utf-8")
    return 0 if out.ok else 1


if __name__ == "__main__":
    sys.exit(main())
