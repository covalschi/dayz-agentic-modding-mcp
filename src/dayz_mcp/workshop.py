"""The Workshop side of a built mod, without the Steam client.

Everything here is pure: reading a built mod folder, reading and writing the
`meta.cpp` Publisher leaves behind, naming Steam's result codes, and asking the
public Web API what an item currently looks like. Talking to the Steam client
is `steamugc.py`; deciding whether to publish at all is `tools/workshop.py`.
"""
from __future__ import annotations

import json
import os
import re
import stat
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path

#: DayZ. The game's own Workshop, and the app id Publisher itself runs under
#: (its steam_appid.txt says so). The dedicated server and the tools are
#: separate Steam apps with no Workshop of their own.
WORKSHOP_APP_ID = 221100
META_NAME = "meta.cpp"
#: Steam's ceiling for a preview image. Over it Steam answers with a bare
#: LimitExceeded after the whole content has been uploaded, so it is refused
#: here, before anything is, with the number in the sentence.
MAX_PREVIEW_BYTES = 1_000_000
PREVIEW_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif")

#: ERemoteStoragePublishedFileVisibility, by the word an agent would use.
VISIBILITY = {"public": 0, "friends": 1, "private": 2, "unlisted": 3}
VISIBILITY_NAMES = {v: k for k, v in VISIBILITY.items()}

DETAILS_URL = "https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/"
ITEM_URL = "https://steamcommunity.com/sharedfiles/filedetails/?id={id}"
LEGAL_URL = "https://steamcommunity.com/sharedfiles/workshoplegalagreement"

# EResult, steamclientpublic.h. Only the range an upload can plausibly come
# back with; anything else is named by number.
ERESULT = {
    1: "OK", 2: "Fail", 3: "NoConnection", 5: "InvalidPassword", 6: "LoggedInElsewhere",
    7: "InvalidProtocolVer", 8: "InvalidParam", 9: "FileNotFound", 10: "Busy",
    11: "InvalidState", 12: "InvalidName", 13: "InvalidEmail", 14: "DuplicateName",
    15: "AccessDenied", 16: "Timeout", 17: "Banned", 18: "AccountNotFound",
    19: "InvalidSteamID", 20: "ServiceUnavailable", 21: "NotLoggedOn", 22: "Pending",
    23: "EncryptionFailure", 24: "InsufficientPrivilege", 25: "LimitExceeded",
    26: "Revoked", 27: "Expired", 28: "AlreadyRedeemed", 29: "DuplicateRequest",
    30: "AlreadyOwned", 31: "IPNotFound", 32: "PersistFailed", 33: "LockingFailed",
    34: "LogonSessionReplaced", 35: "ConnectFailed", 36: "HandshakeFailed",
}

# What the likely ones mean for an upload -- Steam's own name is a word, and
# a word is not a next step.
EXPLAIN = {
    2: "Steam gave no reason; try once more, then look at the item's page",
    3: "the Steam client has no connection to Steam",
    8: "a title, description or change note over Steam's limits, or a tag Steam does not accept",
    9: "the item id does not exist, or the content folder or preview vanished under the upload",
    10: "Steam is busy with another update of this item; wait and try again",
    15: "this Steam account does not own the item, or does not own the game",
    16: "Steam timed out; try again",
    20: "Steam's Workshop service is unavailable right now",
    21: "the Steam client is not logged on",
    25: "Steam's limit -- a preview over 1 MB, or the account's Workshop quota",
    29: "Steam already holds an identical pending request for this item",
    33: "Steam could not lock the item for update; try again",
}


def eresult_name(code: int) -> str:
    return ERESULT.get(code, f"EResult {code}")


def explain(code: int) -> str:
    return EXPLAIN.get(code, "")


def item_url(published_id: int) -> str:
    return ITEM_URL.format(id=published_id)


# ---------------------------------------------------------------- meta.cpp
#
# Publisher writes three lines into the folder it published from, and DayZ's
# launcher reads the id back out of them. The same three lines, byte for
# byte, are what this server writes after creating an item -- a folder this
# server created is one Publisher can carry on with, and the other way round.
# The copy subscribers receive carries a fourth line, `timestamp = <int64>;`,
# and CRLF (read off a subscribed item 2026-09-19): the reader takes either
# shape, the writer sticks to the three lines the id lives in. The fourth is
# Publisher's own bookkeeping: a .NET DateTime.ToBinary() (UTC) of the
# moment it wrote the file -- decoded 2026-09-19, 15 s before that item's
# time_updated -- and nothing reads it back. Steam took the three-line file
# for an update the same day, and the item's listed size shrank by exactly
# the 37 bytes the fourth line and the CRLFs had added.

_META_ID = re.compile(r"\bpublishedid\s*=\s*(\d+)\s*;")
_META_NAME = re.compile(r'\bname\s*=\s*"([^"]*)"\s*;')


@dataclass
class Meta:
    published_id: int = 0
    name: str = ""

    @property
    def valid(self) -> bool:
        return self.published_id > 0


def parse_meta(text: str) -> Meta:
    """`publishedid` and `name` out of a meta.cpp. A file with no readable
    id parses to `published_id == 0`, and the caller decides what that means
    -- it is NOT the same as no file at all."""
    found = _META_ID.search(text)
    named = _META_NAME.search(text)
    return Meta(int(found.group(1)) if found else 0, named.group(1) if named else "")


def render_meta(published_id: int, name: str) -> str:
    """Publisher's own format: LF line ends, a trailing newline, nothing else.
    A double quote in the name would break the one string the file holds,
    so it becomes a single quote -- the name here is a label, not the title
    Steam shows."""
    safe = name.replace('"', "'")
    return f'protocol = 1;\npublishedid = {published_id};\nname = "{safe}";\n'


# ------------------------------------------------------------- the folder


@dataclass
class Folder:
    """What is in a built mod folder, as Steam would upload it: every file
    under it, whole. `links` names junctions and symlinks found inside,
    because Steam follows them and would upload whatever they point at."""

    path: str
    exists: bool = False
    files: int = 0
    bytes: int = 0
    pbos: list[str] = field(default_factory=list)
    unsigned: list[str] = field(default_factory=list)
    keys: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)
    newest_pbo: float = 0.0
    meta: Meta | None = None
    meta_error: str = ""

    @property
    def built(self) -> bool:
        return bool(self.pbos)


def _is_link(path: Path) -> bool:
    try:
        st = os.lstat(path)
    except OSError:
        return False
    if stat.S_ISLNK(st.st_mode):
        return True
    attrs = getattr(st, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attrs & reparse)


def inspect_folder(path: str | Path) -> Folder:
    root = Path(path)
    out = Folder(path=str(root))
    if not root.is_dir():
        return out
    out.exists = True
    if _is_link(root):
        out.links.append(".")
        return out

    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        # Prune links before os.walk descends into them: a junction inside
        # the folder is reported, never walked.
        kept = []
        for d in dirnames:
            if _is_link(here / d):
                out.links.append((here / d).relative_to(root).as_posix())
            else:
                kept.append(d)
        dirnames[:] = kept
        for f in filenames:
            p = here / f
            rel = p.relative_to(root).as_posix()
            if _is_link(p):
                out.links.append(rel)
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            out.files += 1
            out.bytes += st.st_size
            low = f.lower()
            if low.endswith(".pbo"):
                out.pbos.append(rel)
                out.newest_pbo = max(out.newest_pbo, st.st_mtime)
                signed = any(
                    other.lower().startswith(low + ".") and other.lower().endswith(".bisign")
                    for other in filenames
                )
                if not signed:
                    out.unsigned.append(rel)
            elif low.endswith(".bikey"):
                out.keys.append(rel)

    meta_path = root / META_NAME
    if meta_path.is_file():
        try:
            out.meta = parse_meta(meta_path.read_text(encoding="utf-8", errors="replace"))
        except OSError as exc:
            out.meta_error = f"cannot read {META_NAME}: {exc}"
        else:
            if not out.meta.valid:
                out.meta_error = f"{META_NAME} carries no readable publishedid"
    out.pbos.sort()
    out.unsigned.sort()
    out.keys.sort()
    out.links.sort()
    return out


# ------------------------------------------------------ the public listing
#
# GetPublishedFileDetails needs no key and no login. It is how a published
# upload is read back: Steam's own answer to "what does this item look like
# now", from outside the account that owns it. A private item is not in that
# answer at all -- `result` 9 -- which is a fact about the item, not an error.


@dataclass
class Item:
    id: int
    result: int = 0
    title: str = ""
    time_created: int = 0
    time_updated: int = 0
    file_size: int = 0
    visibility: int | None = None
    subscriptions: int = 0
    tags: list[str] = field(default_factory=list)
    preview_url: str = ""
    url: str = ""

    @property
    def visible(self) -> bool:
        return self.result == 1

    def to_dict(self) -> dict:
        d = asdict(self)
        d["visibility_name"] = VISIBILITY_NAMES.get(self.visibility, None)
        return d


def _post(url: str, data: bytes, timeout: float) -> bytes:
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "dayz-agentic-modding-mcp",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed https URL
        return resp.read()


def parse_details(body: bytes | str, published_id: int) -> Item:
    """One item out of a GetPublishedFileDetails answer. Raises ValueError
    when the body is not that answer at all."""
    text = body.decode("utf-8", "replace") if isinstance(body, bytes) else body
    payload = json.loads(text)
    entries = payload.get("response", {}).get("publishedfiledetails") or []
    if not entries:
        raise ValueError("no publishedfiledetails in the answer")
    raw = entries[0]
    item = Item(id=published_id, url=item_url(published_id))
    item.result = int(raw.get("result", 0) or 0)
    item.title = str(raw.get("title", "") or "")
    item.time_created = int(raw.get("time_created", 0) or 0)
    item.time_updated = int(raw.get("time_updated", 0) or 0)
    item.file_size = int(raw.get("file_size", 0) or 0)
    vis = raw.get("visibility")
    item.visibility = int(vis) if vis is not None else None
    item.subscriptions = int(raw.get("subscriptions", 0) or 0)
    item.tags = [str(t.get("tag", "")) for t in raw.get("tags", []) or [] if isinstance(t, dict)]
    item.preview_url = str(raw.get("preview_url", "") or "")
    return item


def read_item(published_id: int, post=_post, timeout: float = 10.0) -> tuple[Item | None, str]:
    """The item as the public API shows it, or None and why not. Never
    raises: a read-back that fails is a note in an answer, not the answer."""
    data = urllib.parse.urlencode(
        {"itemcount": 1, "publishedfileids[0]": published_id}
    ).encode("ascii")
    try:
        body = post(DETAILS_URL, data, timeout)
        return parse_details(body, published_id), ""
    except (OSError, ValueError, TypeError) as exc:
        return None, f"{type(exc).__name__}: {exc}"


def stale(folder: Folder, item: Item | None) -> bool | None:
    """Built after the last upload? None when either side is unknown."""
    if not folder.built or item is None or not item.visible or not item.time_updated:
        return None
    return folder.newest_pbo > item.time_updated
