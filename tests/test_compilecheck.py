from pathlib import Path
from dayz_mcp.compilecheck import client_cmd, judge
from dayz_mcp.profile import ExpectCfg


def expect(**kw) -> ExpectCfg:
    base = dict(ready_line="", max_warnings=None, forbid=[], error_regex=[], counters={}, noise=[])
    base.update(kw)
    return ExpectCfg(**base)


def test_client_command_has_no_server_flag_and_uses_the_temp_profile():
    cmd = client_cmd(Path("C:/DayZ"), "C:/a;@B", Path("C:/tmp/prof"))
    joined = " ".join(cmd)
    assert "DayZDiag_x64.exe" in cmd[0]
    assert "-server" not in joined
    assert "-mod=C:/a;@B" in joined
    assert "-nolauncher" in joined


def test_clean_run_that_reached_mission_is_ok():
    got = judge(["nothing bad"], ["Module: Mission"], expect())
    assert got["status"] == "ok"


def test_fatal_compile_error_fails():
    got = judge(["Can't compile \"Thing\" script module!"], ["Module: Mission"], expect())
    assert got["status"] == "fail"
    assert any("compile" in e for e in got["errors"])


def test_project_error_regex_is_honoured():
    e = expect(error_regex=[r"SCRIPT\s+\(E\).*MyPrefix_"])
    got = judge(["SCRIPT (E): MyPrefix_Thing.c(12): bad"], ["Module: Mission"], e)
    assert got["status"] == "fail"


def test_clean_run_that_never_reached_mission_is_unknown_not_ok():
    got = judge(["nothing bad"], ["Module: Core"], expect())
    assert got["status"] == "unknown"
    assert "Mission" in got["reason"]


def test_error_before_mission_is_a_failure_not_unknown():
    got = judge([], ["Can't compile \"Thing\" script module!"], expect())
    assert got["status"] == "fail"


def test_missing_rpt_is_a_failure():
    got = judge([], [], expect())
    assert got["status"] in ("fail", "unknown")
