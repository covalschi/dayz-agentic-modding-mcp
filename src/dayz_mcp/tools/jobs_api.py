from __future__ import annotations

from ..errors import Result, fail, ok
from . import session
from .project import require_project


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
    job, err = _job_or_error(job_id)
    if err:
        return err
    return ok(session.jobs().wait(job_id, timeout).to_dict())


def job_artifacts(job_id: str) -> Result:
    job, err = _job_or_error(job_id)
    return err or ok({"artifacts": job.artifacts, "dir": str(session.jobs().artifacts_dir(job_id))})
