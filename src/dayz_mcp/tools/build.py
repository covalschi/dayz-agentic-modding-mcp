from __future__ import annotations

import threading
from pathlib import Path

from ..errors import Result, fail, ok
from ..jobs import QUEUED, RUNNING
from ..packer import ensure_patch_link, pack_all
from ..procs import powershell_cmd, run_blocking
from ..profile import resolve_mod_dir
from . import session
from .lint import mod_lint
from .project import require_project


def session_tools_root() -> str | None:
    return session.tools_root()


def mod_build(skip_lint: bool = False) -> Result:
    """Pack this project's mods. Returns a `job_id`.

    Runs `mod_lint` first and refuses on what it refuses. That check costs
    milliseconds and the boot it replaces costs a minute -- and two of the
    defects it names would not appear in that boot's log at all, because a
    `modded class` that modifies nothing loads and reports success.

    `skip_lint=True` packs anyway. It exists because a refusal that cannot be
    overridden is a refusal that gets worked around by not using the tool.
    """
    guard = require_project()
    if guard:
        return guard
    prof = session.profile()

    if not skip_lint:
        judged = mod_lint()
        if not judged.ok:
            return Result(
                False, judged.data,
                "not packed -- " + judged.error,
                (judged.hint + "; " if judged.hint else "")
                + "fix it, or pass skip_lint=True to pack anyway",
            )
    tools_root = session_tools_root()
    if not tools_root:
        return fail(
            "DayZ Tools not found",
            hint="install DayZ Tools from Steam, or set machine.tools in dayz-mcp.local.toml",
        )

    store = session.jobs()

    # server_start refuses a second server for the same session; a second build
    # of the same project deserves the same answer. Both would run FileBank
    # into one output directory, writing the same pbo and unlinking the same
    # .bisign -- contention at best, a half-written artifact at worst. Tools
    # run on worker threads (see server.py), so two builds in flight takes one
    # impatient retry, not an exotic sequence.
    #
    # Per project, because the store is per project: another project's build is
    # not this project's problem. A job left "running" by a dead process cannot
    # block anything either -- JobStore.load() marks those failed on the way in.
    in_flight = [j for j in store.all() if j.kind == "build" and j.status in (QUEUED, RUNNING)]
    if in_flight:
        busy = in_flight[-1].id
        return fail(
            f"a build is already running for this project (job {busy})",
            hint=f"wait for it with job_wait('{busy}'), or look at it with job_status('{busy}')",
        )

    job = store.create("build")
    log_dir = store.artifacts_dir(job.id)

    def run() -> None:
        store.start(job.id)
        # An uncaught exception here must still resolve the job -- otherwise it is
        # stuck at "running" forever (the traceback goes to the server's stderr,
        # which the calling agent never sees) and a later restart mislabels it as
        # merely lost, not actually failed.
        try:
            if prof.build.pre_script:
                pre_log = log_dir / "pre.log"
                code, tail = run_blocking(
                    powershell_cmd(prof.root / prof.build.pre_script), prof.root, pre_log, timeout=900
                )
                store.add_artifact(job.id, pre_log)
                if code != 0:
                    store.fail(job.id, f"pre_script failed with {code}: {tail[-300:]}")
                    return

            sources = {mod: resolve_mod_dir(prof.root, prof.build.sources, mod) for mod in prof.build.mods}
            results = pack_all(
                prof.build.mods, prof.root, Path(tools_root), log_dir,
                exclude=prof.build.exclude, sources=sources, stage=prof.build.stage,
            )
            for log in sorted(log_dir.glob("pack-*.log")):
                store.add_artifact(job.id, log)

            broken = [r for r in results if r.error]
            if broken:
                store.fail(job.id, "; ".join(f"{r.name}: {r.error}" for r in broken))
                return

            link_notes = []
            if prof.client.file_patching:
                # The engine reads -filePatching's loose files from
                # <game directory>/<pbo prefix>/..., not from inside the
                # built @MyMod folder (measured 2026-09-03, spec F6) -- so the
                # junction goes under the GAME directory this session
                # resolved, the same one client_start launches from.
                game = session.game()
                if not game:
                    link_notes.append(
                        "file_patching is on, but no game directory is known -- open the "
                        "project once machine.game resolves (or start the client so this "
                        "session discovers it) so mod_build knows where -filePatching reads "
                        "loose files from; no junction was made"
                    )
                else:
                    game_dir = Path(game)
                    for name in prof.build.mods:
                        linked, note = ensure_patch_link(game_dir, name, sources[name])
                        if not linked:
                            store.fail(job.id, f"{name}: {note}")
                            return
                        link_notes.append(f"{name}: {note}")

            summary = ", ".join(
                f"{r.name} {r.size}B{'' if r.signed else ' (unsigned)'}" for r in results
            )
            # A non-empty note is an actionable half of the result (e.g. which key
            # signed it, or why it did not sign) -- dropping it here would make the
            # agent re-derive the reason from raw pack logs instead of the job.
            notes = [f"{r.name}: {r.note}" for r in results if r.note] + link_notes
            if notes:
                summary = f"{summary} | notes: {'; '.join(notes)}"
            store.finish(job.id, 0, summary=summary)
        except Exception as exc:  # noqa: BLE001 - must reach the job, not just stderr
            store.fail(job.id, f"{type(exc).__name__}: {exc}")

    threading.Thread(target=run, daemon=True).start()
    return ok({"job_id": job.id})
