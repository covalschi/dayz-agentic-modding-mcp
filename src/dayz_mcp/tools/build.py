from __future__ import annotations

import threading
from pathlib import Path

from ..errors import Result, fail, ok
from ..packer import pack_all
from ..procs import powershell_cmd, run_blocking
from ..profile import resolve_mod_dir
from . import session
from .project import require_project


def session_tools_root() -> str | None:
    return session.tools_root()


def mod_build() -> Result:
    guard = require_project()
    if guard:
        return guard
    prof = session.profile()
    tools_root = session_tools_root()
    if not tools_root:
        return fail(
            "DayZ Tools not found",
            hint="install DayZ Tools from Steam, or set machine.tools in dayz-mcp.local.toml",
        )

    store = session.jobs()
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
            summary = ", ".join(
                f"{r.name} {r.size}B{'' if r.signed else ' (unsigned)'}" for r in results
            )
            # A non-empty note is an actionable half of the result (e.g. which key
            # signed it, or why it did not sign) -- dropping it here would make the
            # agent re-derive the reason from raw pack logs instead of the job.
            notes = [f"{r.name}: {r.note}" for r in results if r.note]
            if notes:
                summary = f"{summary} | notes: {'; '.join(notes)}"
            store.finish(job.id, 0, summary=summary)
        except Exception as exc:  # noqa: BLE001 - must reach the job, not just stderr
            store.fail(job.id, f"{type(exc).__name__}: {exc}")

    threading.Thread(target=run, daemon=True).start()
    return ok({"job_id": job.id})
