from __future__ import annotations

from ..errors import Result, fail, ok
from . import session
from .project import require_project

# Even off the event loop (see server.py's anyio.to_thread wrapping), an
# unbounded wait is a footgun: nothing else in this call can be cancelled once
# issued, so a mistyped huge timeout ties up a worker thread for that long.
MAX_WAIT_SECONDS = 600


def _job_or_error(job_id: str):
    guard = require_project()
    if guard:
        return None, guard
    job = session.jobs().get(job_id)
    if job is None:
        return None, fail(f"unknown job {job_id}", hint="job ids come from mod_build or server_start")
    return job, None


def job_status(job_id: str) -> Result:
    job, err = _job_or_error(job_id)
    return err or ok(job.to_dict())


def job_wait(job_id: str, timeout: float = 60) -> Result:
    """Wait for a job to finish, or until `timeout` seconds pass.

    `timeout` is clamped to at most MAX_WAIT_SECONDS (600s) regardless of what
    is requested.
    """
    job, err = _job_or_error(job_id)
    if err:
        return err
    capped = max(0.0, min(timeout, MAX_WAIT_SECONDS))
    return ok(session.jobs().wait(job_id, capped).to_dict())


def job_artifacts(job_id: str) -> Result:
    job, err = _job_or_error(job_id)
    return err or ok({"artifacts": job.artifacts, "dir": str(session.jobs().artifacts_dir(job_id))})
