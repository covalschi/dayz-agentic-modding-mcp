from pathlib import Path
from dayz_mcp.profile import ExpectCfg
from dayz_mcp.verdict import build_verdict

FIX = Path(__file__).parent / "fixtures"


def lines(name: str) -> list[str]:
    return (FIX / name).read_text(encoding="utf-8").splitlines()


def expect(**kw) -> ExpectCfg:
    base = dict(
        ready_line="[MyMod] loaded", max_warnings=None,
        forbid=["Bad type", "Can't compile"], error_regex=[], counters={}, noise=[],
    )
    base.update(kw)
    return ExpectCfg(**base)


def test_clean_boot_passes():
    v = build_verdict(lines("boot_ok.log"), expect(counters={"items": 12, "recipes": 3}))
    assert v["verdict"] == "pass"
    assert v["reasons"] == []


def test_counter_below_expectation_fails_even_though_the_line_is_there():
    v = build_verdict(lines("boot_broken.log"), expect(counters={"recipes": 3}))
    assert v["verdict"] == "fail"
    assert v["counters_unexpected"]["recipes"] == {"expected": 3, "actual": 0}


def test_forbidden_string_fails():
    v = build_verdict(lines("boot_broken.log"), expect())
    assert v["verdict"] == "fail"
    assert any("Bad type" in r for r in v["reasons"])


def test_missing_ready_line_fails_with_a_clear_reason():
    v = build_verdict(["nothing useful here"], expect())
    assert v["verdict"] == "fail"
    assert any("ready line" in r for r in v["reasons"])


def test_warning_count_within_the_declared_budget_passes():
    assert build_verdict(lines("boot_ok.log"), expect(max_warnings=2))["verdict"] == "pass"


def test_more_warnings_than_declared_fails():
    v = build_verdict(lines("boot_ok.log"), expect(max_warnings=1))
    assert v["verdict"] == "fail"
    assert any("warning" in r.lower() for r in v["reasons"])


def test_engine_noise_never_counts_against_the_budget():
    v = build_verdict(lines("boot_noise.log"), expect(max_warnings=0))
    assert v["verdict"] == "pass"
    assert sum(n["count"] for n in v["noise"]) == 3


def test_unexpected_counters_are_reported_but_do_not_fail():
    v = build_verdict(lines("boot_ok.log"), expect(counters={"items": 12}))
    assert v["verdict"] == "pass"
    assert v["counters"]["tiers"] == 2


def test_huge_error_count_is_capped_but_the_true_total_is_reported():
    v = build_verdict(lines("boot_manyerrors.log"), expect())
    assert v["verdict"] == "fail"
    assert v["errors_total"] == 200
    assert len(v["errors"]) < 200
    assert len(v["errors"]) <= 25
    assert v["reasons_total"] >= 200
    assert len(v["reasons"]) < 200
    assert len(v["reasons"]) <= 25


# --- Engine statements that make a run bad whatever the mod declares ---


def test_a_mission_with_no_main_function_fails_the_verdict(tmp_path):
    """The line another session's stand produced while every other signal said
    the boot was fine: the port bound, no error was logged, and the verdict
    passed -- on a server that had already decided nobody may connect."""
    from dayz_mcp.profile import ExpectCfg
    from dayz_mcp.verdict import build_verdict

    lines = [
        "SCRIPT       : Module: Mission; loaded 216x files; 450x classes;",
        "SCRIPT       : Mission script has no main function, player connect will stay disabled!",
    ]
    got = build_verdict(lines, ExpectCfg())
    assert got["verdict"] == "fail"
    assert any("no main function" in r for r in got["reasons"])


def test_the_engine_defaults_do_not_replace_what_a_profile_declares(tmp_path):
    """Same shape as noise: the profile's list ADDS to the built-in one. A
    profile that declared its own forbidden string and thereby switched off the
    engine's would be a trap nobody would notice until the boot it was meant to
    catch."""
    from dayz_mcp.profile import ExpectCfg
    from dayz_mcp.verdict import build_verdict

    expect = ExpectCfg(forbid=["MyModExploded"])
    lines = ["SCRIPT       : Mission script has no main function, player connect will stay disabled!"]
    got = build_verdict(lines, expect)
    assert got["verdict"] == "fail"

    lines = ["SCRIPT       : MyModExploded"]
    got = build_verdict(lines, expect)
    assert got["verdict"] == "fail"
