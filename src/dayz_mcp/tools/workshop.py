"""The Workshop: publishing a built mod, and reading what is there.

The last step of a mod's life on this machine, and the only tool in the set
whose effect is somewhere else. So it is built the way `server_signatures`
is: it refuses everything that could publish the wrong thing before a byte
leaves, says what it is about to do in its answer, and reads the item back
afterwards instead of taking Steam's word for it.

What it publishes is exactly what `mod_build` made: the `@Name` folder --
addons, signatures, keys, mod.cpp, meta.cpp -- whole, as Publisher would.
Which item it goes to is what Publisher itself decides it by: `meta.cpp` in
that folder. Present with an id, the item is updated; absent, an item is
created and `meta.cpp` written there first, in Publisher's own three lines,
so the two tools can carry on from each other's folders.
"""
from __future__ import annotations

import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from ..errors import Result, fail, ok
from ..jobs import QUEUED, RUNNING
from ..procs import run_blocking
from ..steamugc import Outcome, Spec
from ..workshop import (
    LEGAL_URL, MAX_PREVIEW_BYTES, META_NAME, PREVIEW_SUFFIXES, VISIBILITY, VISIBILITY_NAMES,
    WORKSHOP_APP_ID, Folder, Item, inspect_folder, item_url, read_item, render_meta, stale,
)
from . import session
from .build import EXCLUSIVE_KINDS, EXCLUSIVE_NOUNS
from .project import require_project

STEAM_DLL = "steam_api64.dll"
KIND = "workshop"
#: How long one upload may run. Generous, because a large mod on a slow link
#: is real; finite, because a job that ends with a reason beats one that
#: does not end.
UPLOAD_TIMEOUT = 1800.0


def mod_folder(root: Path, mod: str) -> Path:
    """The packer's own formula for its output folder."""
    return root / f"@{mod}"


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def _declared(prof, mod: str) -> Result | None:
    if mod not in prof.build.mods:
        return fail(
            f"{mod!r} is not a mod this project declares",
            hint=f"build.mods names {prof.build.mods}; the Workshop tools publish those and nothing else",
        )
    return None


def _busy(store) -> list:
    # A build rewrites the pbo an upload is reading; an upload reads the pbo
    # a build would rewrite. `mod_build` refuses the same way round.
    return [j for j in store.all() if j.kind in EXCLUSIVE_KINDS and j.status in (QUEUED, RUNNING)]


def run_uploader(spec_path: Path, result_path: Path, cwd: Path, log_path: Path) -> tuple[int, str]:
    """Run the uploader as its own process -- `steamugc.py` says why --
    under the game's app id, given both ways the Steam DLL looks for one:
    the environment, and `steam_appid.txt` in the working directory."""
    cmd = [sys.executable, "-m", "dayz_mcp.steamugc", str(spec_path), str(result_path)]
    env = {"SteamAppId": str(WORKSHOP_APP_ID), "SteamGameId": str(WORKSHOP_APP_ID)}
    return run_blocking(cmd, cwd, log_path, timeout=UPLOAD_TIMEOUT + 120, env=env)


def workshop_publish(
    mod: str, changenote: str = "", title: str = "", description: str = "", preview: str = "",
    visibility: str = "", tags: list[str] | None = None, content: bool = True,
    requires: list[int] | None = None, remove_requires: list[int] | None = None,
) -> Result:
    """Upload a built mod to the Steam Workshop. Returns a `job_id`.

    The content is the mod's built folder, whole, as `mod_build` left it.
    `meta.cpp` in that folder decides between the two things this can do:
    with a `publishedid`, that item is UPDATED with the folder and the
    `changenote`; without the file, a new item is CREATED (`title` is then
    required), and `meta.cpp` is written into the folder before the upload
    so subscribers get the id -- keep that file with your sources, a
    deleted build folder takes the item id with it.

    `title`, `description`, `preview` (an image under 1 MB, path relative to
    the project or absolute), `visibility` and `tags` are sent only when
    given; an update leaves untouched whatever it was not given. A new item
    is `private` unless `visibility` says otherwise -- `public`, `friends`,
    `private` or `unlisted` -- so nothing is public before its page has been
    looked at; publish again with `visibility="public"` to open it.

    `requires` puts Workshop items into the item's Required Items -- the section
    the DayZ Launcher reads to pull dependencies, distinct from the description
    -- and `remove_requires` takes them off; ids are the numbers in the items'
    page URLs. The job reads the item's list first and changes only what
    differs, so a repeated call is a no-op, and the summary reads the list back.

    `content=False` sends only what was given -- title, description, preview,
    visibility, tags -- and leaves the item's files alone: the folder need
    not be built, only its `meta.cpp` is read, for the item id, so nothing
    that could ship an unreleased build is touched. It cannot create an
    item, and refuses when nothing was given.

    It refuses, before any byte leaves: a mod the project does not declare;
    a folder with no pbo; a junction or symlink inside the folder (Steam
    follows it and uploads what is behind); a `meta.cpp` it cannot read (a
    guess there creates a duplicate item); a missing or oversized preview;
    a build or another upload already running for this project; and a
    machine where the game -- and so its `steam_api64.dll`, the one library
    this uses -- is unknown. It needs the Steam client running and logged
    into the account that owns the item and the game; that is checked by
    the job, not here, because only Steam can answer it.

    The job's summary names the item, its page, and what the public listing
    showed when read back afterwards. Watch it with `job_wait`; the log is
    in `job_artifacts`.
    """
    guard = require_project()
    if guard:
        return guard
    prof = session.profile()
    refused = _declared(prof, mod)
    if refused:
        return refused
    if visibility and visibility not in VISIBILITY:
        return fail(
            f"{visibility!r} is not a visibility",
            hint=f"one of {', '.join(VISIBILITY)}; leave it empty to keep an item's visibility "
                 "(a new item is private then)",
        )

    store = session.jobs()
    busy = _busy(store)
    if busy:
        job = busy[-1]
        return fail(
            f"a {EXCLUSIVE_NOUNS.get(job.kind, job.kind)} is already running for this project "
            f"(job {job.id})",
            hint=f"wait for it with job_wait('{job.id}') -- a build rewrites the very pbo an "
                 "upload reads",
        )

    folder_path = mod_folder(prof.root, mod)
    folder = inspect_folder(folder_path)
    if content and not folder.built:
        return fail(
            f"{mod} is not built: no pbo under {folder_path / 'addons'}",
            hint="run mod_build first; the Workshop gets the built folder, never the sources",
        )
    if content and folder.links:
        return fail(
            f"{folder_path.name} contains a junction or symlink: {', '.join(folder.links)}",
            hint="Steam follows links and would upload everything behind them -- remove the "
                 "link from the built folder and publish again",
        )
    if folder.meta_error:
        return fail(
            f"{folder.meta_error} in {folder_path}",
            hint=f"fix {META_NAME} (protocol = 1; publishedid = <id>; name = \"...\";) or delete "
                 "it to create a new item -- guessing here would publish a duplicate",
        )
    creating = folder.meta is None
    title = title.strip()
    if not content:
        if creating:
            return fail(
                f"no {META_NAME} in {folder_path}, and content=False cannot create an item",
                hint="a listing-only update needs the item id meta.cpp carries; build and "
                     "package the mod once, or publish with the content to create the item",
            )
        if not (title or description or preview or visibility or tags or requires or remove_requires):
            return fail(
                "nothing to update: content=False and no title, description, preview, "
                "visibility, tags or required items",
                hint="pass what should change, or leave content=True to upload the folder",
            )
    require_ids, refused = _item_ids("requires", requires)
    if refused:
        return refused
    unrequire_ids, refused = _item_ids("remove_requires", remove_requires)
    if refused:
        return refused
    own = folder.meta.published_id if folder.meta else 0
    if own and own in require_ids:
        return fail(f"item {own} cannot require itself")
    if creating and not title:
        return fail(
            "a new item needs a title",
            hint=f"pass title=...; an existing item is recognised by {META_NAME} in "
                 f"{folder_path.name}, and there is none",
        )

    preview_path = ""
    if preview:
        candidate = Path(preview)
        if not candidate.is_absolute():
            candidate = prof.root / candidate
        if not candidate.is_file():
            return fail(f"preview not found: {candidate}",
                        hint="a path relative to the project, or absolute")
        if candidate.suffix.lower() not in PREVIEW_SUFFIXES:
            return fail(f"preview must be one of {', '.join(PREVIEW_SUFFIXES)}, not {candidate.suffix!r}")
        size = candidate.stat().st_size
        if size > MAX_PREVIEW_BYTES:
            return fail(
                f"preview is {size} B; Steam's ceiling is {MAX_PREVIEW_BYTES} B",
                hint="shrink the image -- Steam would refuse it after the whole content had "
                     "been uploaded, with a bare LimitExceeded",
            )
        preview_path = str(candidate.resolve())

    game = session.game()
    if not game:
        return fail(
            f"the game directory is unknown, and {STEAM_DLL} lives there",
            hint="set machine.game in dayz-mcp.local.toml, or open the project on a machine "
                 "where the game is installed",
        )
    dll = Path(game) / STEAM_DLL
    if not dll.is_file():
        return fail(
            f"{STEAM_DLL} not found in {game}",
            hint="the game's own copy is the only Steam library this server uses; verify the "
                 "game's files in Steam if it is missing",
        )

    if visibility:
        vis: int | None = VISIBILITY[visibility]
    else:
        vis = VISIBILITY["private"] if creating else None
    published_id = folder.meta.published_id if folder.meta else 0
    spec = Spec(
        dll=str(dll), content=str(folder_path.resolve()), app_id=WORKSHOP_APP_ID,
        published_id=published_id, title=title, description=description, preview=preview_path,
        visibility=vis, tags=[t.strip() for t in (tags or []) if t.strip()],
        changenote=changenote, timeout=UPLOAD_TIMEOUT, send_content=content,
        requires=require_ids, remove_requires=unrequire_ids,
    )

    job = store.create(KIND)
    log_dir = store.artifacts_dir(job.id)

    def run() -> None:
        store.start(job.id)
        try:
            spec_path = log_dir / "spec.json"
            result_path = log_dir / "result.json"
            log_path = log_dir / f"workshop-{mod}.log"
            spec_path.write_text(spec.to_json(), encoding="utf-8")
            (log_dir / "steam_appid.txt").write_text(f"{WORKSHOP_APP_ID}\n", encoding="ascii")
            submitted_at = time.time()
            code, tail = run_uploader(spec_path, result_path, log_dir, log_path)
            store.add_artifact(job.id, spec_path)
            store.add_artifact(job.id, log_path)
            if not result_path.is_file():
                store.fail(
                    job.id,
                    f"{mod}: the uploader ended (exit {code}) without a result: {tail[-300:].strip()}",
                )
                return
            store.add_artifact(job.id, result_path)
            out = Outcome.from_json(result_path.read_text(encoding="utf-8"))
            if not out.ok:
                message = f"{mod}: {out.error} (at {out.step})"
                if out.needs_legal:
                    message += (f" -- this account has not accepted the Workshop legal agreement: "
                                f"{LEGAL_URL}, then publish again")
                if out.created:
                    message += (f" -- item {out.published_id} exists now and {META_NAME} in "
                                f"{folder_path.name} names it, so the next publish is an update")
                store.fail(job.id, message)
                return
            store.finish(job.id, 0, summary=_summary(mod, folder_path, folder, spec, out, submitted_at))
        except Exception as exc:  # noqa: BLE001 - must reach the job, not just stderr
            store.fail(job.id, f"{type(exc).__name__}: {exc}")

    threading.Thread(target=run, daemon=True).start()
    return ok({
        "job_id": job.id, "mod": mod, "folder": str(folder_path), "item": published_id,
        "creating": creating, "url": item_url(published_id) if published_id else None,
        "visibility": VISIBILITY_NAMES[vis] if vis is not None else "unchanged",
        "files": folder.files, "bytes": folder.bytes, "unsigned": folder.unsigned,
        "content": content, "requires": require_ids, "remove_requires": unrequire_ids,
    })


def _item_ids(name: str, values) -> tuple[list[int], Result | None]:
    """Workshop item ids, or the refusal that names the first thing that is not one."""
    out: list[int] = []
    for value in values or []:
        try:
            number = int(value)
        except (TypeError, ValueError):
            number = 0
        if number <= 0:
            return [], fail(f"{name} needs Workshop item ids, not {value!r}",
                            hint="the number in an item's page URL, e.g. 1559212036")
        if number not in out:
            out.append(number)
    return out, None


def _summary(mod: str, folder_path: Path, folder: Folder, spec: Spec, out: Outcome,
             submitted_at: float) -> str:
    if out.created:
        verb = "created"
    elif spec.send_content:
        verb = "updated"
    else:
        verb = "updated the listing of"
    head = f"{mod}: {verb} item {out.published_id}"
    if spec.send_content or out.created:
        head += f" -- {folder.files} files, {folder.bytes} B, {out.seconds:.0f} s"
    else:
        head += f" -- no content sent, {out.seconds:.0f} s"
    parts = [head]
    if spec.visibility is not None:
        parts.append(f"visibility {VISIBILITY_NAMES[spec.visibility]}")
    if out.created:
        # Read the folder again rather than trust the uploader's word: the
        # file is the one thing a subscriber and the next publish both need.
        after = inspect_folder(folder_path)
        if after.meta is not None and after.meta.published_id == out.published_id:
            parts.append(f"{META_NAME} written to {folder_path.name} -- keep it with the sources, "
                         "a deleted build folder takes the item id with it")
        else:
            parts.append(f"{META_NAME} is MISSING from {folder_path.name}: write it by hand -- "
                         + render_meta(out.published_id, spec.title).replace("\n", " ").strip())
    parts.append(item_url(out.published_id))
    parts.append(_readback(out.published_id, submitted_at, spec.send_content, bool(spec.description)))
    if spec.requires or spec.remove_requires or out.requires_now:
        parts.append(f"requires now [{', '.join(str(i) for i in out.requires_now) or 'none'}]"
                     + (f" (added {out.requires_added})" if out.requires_added else "")
                     + (f" (removed {out.requires_removed})" if out.requires_removed else ""))
    if out.needs_legal:
        parts.append(f"Steam says the Workshop legal agreement is not accepted for this account; "
                     f"the item stays hidden until it is: {LEGAL_URL}")
    if folder.unsigned:
        parts.append(f"unsigned: {', '.join(folder.unsigned)} -- a server with verifySignatures = 2 "
                     "refuses these")
    return " | ".join(parts)


def _readback(published_id: int, submitted_at: float, content_sent: bool = True,
              description_sent: bool = False) -> str:
    item, err = read_item(published_id)
    if item is None:
        return f"readback unavailable ({err})"
    if not item.visible:
        return "readback: not visible to the public listing (private, or just created)"
    # The public API showed every new description as empty for minutes after
    # the item's own page already rendered it, while tags showed at once
    # (measured 2026-09-19, thirteen items). Say so rather than report a
    # number that reads as "the description did not take".
    if description_sent and not item.description:
        described = "description not yet in the public listing (the item's page shows it first)"
    else:
        described = f"description {len(item.description)} chars"
    listing = f"{described}, tags [{', '.join(item.tags) or 'none'}]"
    when = _iso(item.time_updated) if item.time_updated else "unknown"
    if not content_sent:
        return f"readback: {item.title!r}, {listing}"
    if item.time_updated >= submitted_at - 120:
        return f"readback: {item.title!r}, updated {when}, matches this upload; {listing}"
    return (f"readback: {item.title!r}, updated {when} -- still the previous upload; the public "
            f"listing lags a little, workshop_status will show it; {listing}")


def workshop_status(mod: str) -> Result:
    """What the Workshop holds for a built mod, against what is on disk.

    On disk: the built folder -- pbos, which of them are unsigned, keys,
    links, size, and when the newest pbo was built -- and the item id
    `meta.cpp` carries, if any. On Steam: the item as the public listing
    shows it -- title, last update, size, visibility, subscribers -- read
    without a key or a login, so a private item comes back as "not visible"
    rather than as its details. `stale` is the one derived fact: built
    after the last upload, so the item does not carry this build.

    Nothing here touches Steam's client or changes anything; it answers
    without a network too, saying so.
    """
    guard = require_project()
    if guard:
        return guard
    prof = session.profile()
    refused = _declared(prof, mod)
    if refused:
        return refused

    folder_path = mod_folder(prof.root, mod)
    folder = inspect_folder(folder_path)
    published_id = folder.meta.published_id if folder.meta else 0
    item: Item | None = None
    err = ""
    if published_id:
        item, err = read_item(published_id)
    return ok({
        "mod": mod,
        "folder": str(folder_path),
        "exists": folder.exists,
        "built": folder.built,
        "files": folder.files,
        "bytes": folder.bytes,
        "pbos": folder.pbos,
        "unsigned": folder.unsigned,
        "keys": folder.keys,
        "links": folder.links,
        "newest_pbo": _iso(folder.newest_pbo) if folder.newest_pbo else None,
        "item": published_id,
        "meta_name": folder.meta.name if folder.meta else "",
        "meta_error": folder.meta_error,
        "url": item_url(published_id) if published_id else None,
        "remote": item.to_dict() if item else None,
        "remote_error": err,
        "stale": stale(folder, item),
        "note": _status_note(mod, folder, published_id, item, err),
    })


def _status_note(mod: str, folder: Folder, published_id: int, item: Item | None, err: str) -> str:
    if not folder.built:
        return f"{mod} is not built -- run mod_build first"
    if folder.meta_error:
        return f"{folder.meta_error} -- workshop_publish will refuse until it is fixed or removed"
    if not published_id:
        return f"never published -- workshop_publish creates the item ({META_NAME} is absent)"
    if item is None:
        return f"item {published_id} could not be read back ({err})"
    if not item.visible:
        return f"item {published_id} is not visible to the public listing -- private, or just created"
    if stale(folder, item):
        return (f"built {_iso(folder.newest_pbo)}, uploaded {_iso(item.time_updated)} -- the item does "
                "not carry this build; workshop_publish brings it up to date")
    return f"the item carries this build (uploaded {_iso(item.time_updated)})"
