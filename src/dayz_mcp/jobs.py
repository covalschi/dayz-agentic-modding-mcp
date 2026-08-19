"""Background job registry.

Every operation that can outlive a single agent turn goes through here, so the
agent gets an id immediately instead of blocking for minutes. State is mirrored
to disk: an MCP server restart must not lose the record of what ran.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

QUEUED, RUNNING, DONE, FAILED = "queued", "running", "done", "failed"


@dataclass
class Job:
    id: str
    kind: str
    status: str = QUEUED
    started: float = field(default_factory=time.time)
    finished: float | None = None
    exit_code: int | None = None
    artifacts: list[str] = field(default_factory=list)
    summary: str = ""
    error: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class JobStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._seq = 0

    def _file(self, job_id: str) -> Path:
        return self.artifacts_dir(job_id) / "job.json"

    def _persist(self, job: Job) -> None:
        self._file(job.id).write_text(
            json.dumps(job.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def load(self) -> None:
        """A job still marked running never survived the restart: the process that
        owned it is gone. Mark it failed rather than leave a lie on disk."""
        for jf in sorted(self.root.glob("*/job.json")):
            try:
                data = json.loads(jf.read_text(encoding="utf-8"))
                job = Job(**data)
                if job.status in (QUEUED, RUNNING):
                    job.status = FAILED
                    job.error = "lost: the server restarted while this job was running"
                    job.finished = time.time()
                    self._persist(job)
                self._jobs[job.id] = job
                # Seed _seq to avoid id collisions on next create()
                if "-" in job.id:
                    try:
                        seq = int(job.id.rsplit("-", 1)[-1])
                        if seq >= self._seq:
                            self._seq = seq
                    except ValueError:
                        pass
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                continue

    def artifacts_dir(self, job_id: str) -> Path:
        d = self.root / job_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def create(self, kind: str) -> Job:
        with self._lock:
            # Ensure id is unique on disk, not just in memory
            while True:
                self._seq += 1
                job_id = f"{kind}-{int(time.time())}-{self._seq}"
                if not (self.root / job_id).exists():
                    break
            job = Job(id=job_id, kind=kind)
            self._jobs[job_id] = job
        self.artifacts_dir(job_id)
        self._persist(job)
        return job

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def all(self) -> list[Job]:
        return list(self._jobs.values())

    def _update(self, job_id: str, **fields) -> Job | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            for k, v in fields.items():
                setattr(job, k, v)
        self._persist(job)
        return job

    def start(self, job_id: str) -> Job | None:
        return self._update(job_id, status=RUNNING)

    def finish(self, job_id: str, exit_code: int, summary: str = "") -> Job | None:
        status = DONE if exit_code == 0 else FAILED
        return self._update(
            job_id, status=status, exit_code=exit_code, summary=summary, finished=time.time()
        )

    def fail(self, job_id: str, error: str) -> Job | None:
        return self._update(job_id, status=FAILED, error=error, finished=time.time())

    def add_artifact(self, job_id: str, path: str | Path) -> None:
        job = self._jobs.get(job_id)
        if job is None:
            return
        with self._lock:
            job.artifacts.append(str(path))
        self._persist(job)

    def wait(self, job_id: str, timeout: float) -> Job | None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            job = self._jobs.get(job_id)
            if job is None or job.status in (DONE, FAILED):
                return job
            time.sleep(0.1)
        return self._jobs.get(job_id)
