import threading
import time
from dayz_mcp.jobs import JobStore


def test_new_job_is_queued(tmp_path):
    s = JobStore(tmp_path)
    j = s.create("build")
    assert j.status == "queued"
    assert s.get(j.id).kind == "build"


def test_lifecycle_transitions(tmp_path):
    s = JobStore(tmp_path)
    j = s.create("build")
    s.start(j.id)
    assert s.get(j.id).status == "running"
    s.finish(j.id, 0, summary="two pbo")
    got = s.get(j.id)
    assert got.status == "done"
    assert got.exit_code == 0
    assert got.finished is not None


def test_nonzero_exit_is_a_failure(tmp_path):
    s = JobStore(tmp_path)
    j = s.create("build")
    s.finish(j.id, 2)
    assert s.get(j.id).status == "failed"


def test_failure_records_reason(tmp_path):
    s = JobStore(tmp_path)
    j = s.create("boot")
    s.fail(j.id, "server never printed the ready line")
    assert "ready line" in s.get(j.id).error


def test_wait_returns_when_job_finishes(tmp_path):
    s = JobStore(tmp_path)
    j = s.create("build")
    s.start(j.id)
    threading.Thread(target=lambda: (time.sleep(0.2), s.finish(j.id, 0)), daemon=True).start()
    assert s.wait(j.id, timeout=5).status == "done"


def test_wait_gives_up_without_hanging(tmp_path):
    s = JobStore(tmp_path)
    j = s.create("build")
    s.start(j.id)
    started = time.time()
    assert s.wait(j.id, timeout=0.3).status == "running"
    assert time.time() - started < 3


def test_jobs_survive_a_restart(tmp_path):
    s = JobStore(tmp_path)
    j = s.create("build")
    s.finish(j.id, 0, summary="done before the crash")
    revived = JobStore(tmp_path)
    revived.load()
    assert revived.get(j.id).summary == "done before the crash"


def test_running_jobs_are_marked_lost_after_a_restart(tmp_path):
    s = JobStore(tmp_path)
    j = s.create("boot")
    s.start(j.id)
    revived = JobStore(tmp_path)
    revived.load()
    got = revived.get(j.id)
    assert got.status == "failed"
    assert "restart" in got.error


def test_id_collision_resistant_within_same_second(tmp_path):
    """Simulate a restart within the same second creating the same job kind.
    The new job must get a unique id that doesn't overwrite the old one."""
    s = JobStore(tmp_path)
    j1 = s.create("build")
    s.finish(j1.id, 0, summary="first job")

    # Simulate a restart: new JobStore, old job record still on disk
    revived = JobStore(tmp_path)
    revived.load()

    # Create another job of the same kind within the same second (simulated)
    # The new id should not collide with j1.id
    j2 = revived.create("build")
    assert j2.id != j1.id, "New job should have unique id"

    # Original job record should be intact
    got_old = revived.get(j1.id)
    assert got_old.status == "done"
    assert got_old.summary == "first job"

    # New job should be separate
    got_new = revived.get(j2.id)
    assert got_new.status == "queued"
    assert got_new.summary == ""


def test_load_skips_malformed_job_json(tmp_path):
    """A job.json with valid JSON but wrong fields (missing key) should not
    crash load() and should not prevent other jobs from being recovered."""
    import json

    s = JobStore(tmp_path)
    j_good = s.create("good")
    s.finish(j_good.id, 0, summary="good job")

    # Write a malformed job.json (missing required 'kind' field)
    bad_dir = tmp_path / "bad-job"
    bad_dir.mkdir()
    (bad_dir / "job.json").write_text(
        json.dumps({"id": "bad-job", "status": "done"}), encoding="utf-8"
    )

    # load() should skip the bad job but recover the good one
    revived = JobStore(tmp_path)
    revived.load()

    # Good job should be recovered
    got_good = revived.get(j_good.id)
    assert got_good is not None
    assert got_good.status == "done"
    assert got_good.summary == "good job"

    # Bad job should not be in the registry (silently skipped)
    got_bad = revived.get("bad-job")
    assert got_bad is None
