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
