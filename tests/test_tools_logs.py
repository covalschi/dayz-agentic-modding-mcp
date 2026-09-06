"""The log tools: which log answers for which run, and what the verdict says.

Split out of test_tools.py, which had grown into six suites in one file.
"""
from pathlib import Path

from dayz_mcp import tools
from dayz_mcp.tools import lifecycle

from conftest import make_project, with_stand, with_stand_and_game


def test_log_verdict_reads_the_newest_log_and_decides(tmp_path):
    root = make_project(tmp_path)
    with_stand(root, tmp_path / "stand", "SCRIPT : [MyMod] loaded: items=12\n")
    tools.project_open(str(root))
    r = tools.log_verdict()
    assert r.ok, r.error
    assert r.data["verdict"] == "pass"


def test_log_verdict_fails_when_a_counter_is_short(tmp_path):
    root = make_project(tmp_path)
    with_stand(root, tmp_path / "stand", "SCRIPT : [MyMod] loaded: items=1\n")
    tools.project_open(str(root))
    r = tools.log_verdict()
    assert r.data["verdict"] == "fail"
    assert any("items" in reason for reason in r.data["reasons"])


def test_server_log_lookup_goes_through_the_one_profiles_dir_owner(tmp_path, monkeypatch):
    """The server's -profiles directory has exactly one definition
    (lifecycle.server_profiles_dir). logs.py held a character-for-character copy
    of its formula, which is the same "two owners for one path" arrangement that
    already broke both client-side log tools once -- so this asserts the copy is
    gone by moving the owner and watching the log tools follow."""
    root = make_project(tmp_path)
    with_stand(root, tmp_path / "stand", "the stand's own log\n")
    tools.project_open(str(root))

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "script_9.log").write_text("moved with the owner\n", encoding="utf-8")
    monkeypatch.setattr("dayz_mcp.tools.logs.server_profiles_dir", lambda: elsewhere)

    r = tools.log_tail()
    assert r.ok, r.error
    assert r.data["lines"] == ["moved with the owner"]


def test_log_tail_filters(tmp_path):
    root = make_project(tmp_path)
    with_stand(root, tmp_path / "stand", "one\ntwo needle\nthree\n")
    tools.project_open(str(root))
    r = tools.log_tail(pattern="needle")
    assert r.data["lines"] == ["two needle"]


# --- Extra requirement 1: the verdict must be tied to the run it judges (`since`) ---


def test_log_verdict_refuses_a_log_older_than_since(tmp_path):
    root = make_project(tmp_path)
    with_stand(root, tmp_path / "stand", "SCRIPT : [MyMod] loaded: items=12\n")
    tools.project_open(str(root))
    log = tmp_path / "stand" / "profiles" / "script_1.log"
    since = log.stat().st_mtime + 1000  # a "run" that supposedly started after this log was written
    r = tools.log_verdict(since=since)
    assert not r.ok
    assert "predates" in r.error
    assert "wait" in r.hint


def test_log_verdict_accepts_a_log_at_or_after_since(tmp_path):
    root = make_project(tmp_path)
    with_stand(root, tmp_path / "stand", "SCRIPT : [MyMod] loaded: items=12\n")
    tools.project_open(str(root))
    log = tmp_path / "stand" / "profiles" / "script_1.log"
    since = log.stat().st_mtime - 1000  # the run started well before the log was last written
    r = tools.log_verdict(since=since)
    assert r.ok, r.error
    assert r.data["verdict"] == "pass"


# --- Final review, item 2: log_verdict(source="client") and log_tail(source=
# "client") looked under machine.stand_root, a directory nothing ever creates.
# The client's logs live with the job that produced them. ---


def _run_fake_client_compile(monkeypatch, log_text: str, rpt_text: str = "clean\n") -> str:
    """Run client_compile_check with a stand-in for the diagnostic client that
    writes its logs where the real one does: the -profiles directory the tool
    hands to the executable. Returns the job id."""

    def fake_spawn(cmd, cwd):
        profiles = Path(next(a for a in cmd if a.startswith("-profiles=")).split("=", 1)[1])
        (profiles / "script_1.log").write_text(log_text, encoding="utf-8")
        (profiles / "crash.RPT").write_text(rpt_text, encoding="utf-8")
        return 4242

    monkeypatch.setattr("dayz_mcp.tools.lifecycle.spawn", fake_spawn)
    monkeypatch.setattr("dayz_mcp.tools.lifecycle.stop", lambda pid: True)
    job_id = tools.client_compile_check(wait_seconds=0).data["job_id"]
    waited = tools.job_wait(job_id, timeout=15)
    # Terminal either way: whether the check itself passed is the business of
    # the test that cares (log_tail, for one, must work on a failing run).
    assert waited.data["status"] in ("done", "failed"), waited.data
    return job_id


def test_log_verdict_judges_the_client_log_the_compile_check_produced(tmp_path, monkeypatch):
    """The whole point of source="client": after a compile check, ask for a
    verdict on what the client wrote. This failed for every project -- the
    lookup went to <stand>/clientprofile while the check writes into the job's
    own artifacts -- and no test ever passed source="client"."""
    root = make_project(tmp_path)
    with_stand_and_game(root, tmp_path / "stand", tmp_path / "game")
    tools.project_open(str(root))
    _run_fake_client_compile(monkeypatch, "SCRIPT : [MyMod] loaded: items=12\nModule: Mission\n")

    r = tools.log_verdict(source="client")

    assert r.ok, f"{r.error} | {r.hint}"
    assert r.data["verdict"] == "pass"
    assert r.data["counters"]["items"] == 12
    assert "clientprofile" in r.data["log"]


def test_log_tail_reads_the_client_log_the_compile_check_produced(tmp_path, monkeypatch):
    root = make_project(tmp_path)
    with_stand_and_game(root, tmp_path / "stand", tmp_path / "game")
    tools.project_open(str(root))
    _run_fake_client_compile(monkeypatch, "one\nSCRIPT (E): boom\ntwo\n")

    r = tools.log_tail(source="client", pattern="SCRIPT (E)")

    assert r.ok, f"{r.error} | {r.hint}"
    assert r.data["lines"] == ["SCRIPT (E): boom"]
    assert "clientprofile" in r.data["log"]


def test_client_log_tools_do_not_send_the_user_to_change_stand_root(tmp_path):
    """With no compile check run yet there is genuinely no client log -- but
    the hint must name the thing that would produce one. It used to say
    "check machine.stand_root", a setting the client side never reads."""
    root = make_project(tmp_path)
    with_stand(root, tmp_path / "stand", "SCRIPT : [MyMod] loaded: items=12\n")
    tools.project_open(str(root))

    for r in (tools.log_verdict(source="client"), tools.log_tail(source="client")):
        assert not r.ok
        assert "stand_root" not in r.hint
        assert "client_compile_check" in r.hint


def test_client_verdict_does_not_answer_for_a_run_that_produced_nothing(tmp_path, monkeypatch):
    """A compile check that died before the client ever wrote a line has no
    log -- and must say so, rather than quietly handing back the PREVIOUS
    run's log as this run's verdict. Same discipline as `since` on the server
    side, and stricter here because nothing in the reply would reveal the
    substitution."""
    root = make_project(tmp_path)
    with_stand_and_game(root, tmp_path / "stand", tmp_path / "game")
    tools.project_open(str(root))
    first = _run_fake_client_compile(monkeypatch, "SCRIPT : [MyMod] loaded: items=12\nModule: Mission\n")
    assert tools.log_verdict(source="client").data["counters"]["items"] == 12

    def boom(cmd, cwd):
        raise RuntimeError("client never started")

    monkeypatch.setattr("dayz_mcp.tools.lifecycle.spawn", boom)
    later = tools.client_compile_check(wait_seconds=0).data["job_id"]
    assert tools.job_wait(later, timeout=10).data["status"] == "failed"
    assert lifecycle.client_profile_dir(later).is_dir()  # created, but empty

    r = tools.log_verdict(source="client")
    assert not r.ok, f"answered with a stale log: {r.data}"
    # The earlier run's log is still on disk and still readable -- through its
    # own job, which is where a question about it belongs.
    assert (lifecycle.client_profile_dir(first) / "script_1.log").exists()
