from pathlib import Path
from dayz_mcp.logparse import (
    DEFAULT_NOISE, classify, find_ready_line, group_key, parse_counters,
)

FIX = Path(__file__).parent / "fixtures"


def lines(name: str) -> list[str]:
    return (FIX / name).read_text(encoding="utf-8").splitlines()


def test_finds_the_ready_line_by_marker():
    got = find_ready_line(lines("boot_ok.log"), "[MyMod] loaded")
    assert got is not None and "items=12" in got


def test_missing_marker_returns_none():
    assert find_ready_line(lines("boot_ok.log"), "nothing like this") is None


def test_counters_are_parsed_without_knowing_their_names():
    got = parse_counters("11:03:44 SCRIPT : [MyMod] loaded: items=12 recipes=3 tiers=2")
    assert got == {"items": 12, "recipes": 3, "tiers": 2}


def test_counters_ignore_non_numeric_pairs():
    assert parse_counters("state=on items=5") == {"items": 5}


def test_errors_are_picked_up_from_forbidden_strings():
    got = classify(lines("boot_broken.log"), forbid=["Bad type", "Can't compile"], noise=[])
    texts = " ".join(e["text"] for e in got["errors"])
    assert "Bad type" in texts and "Can't compile" in texts


def test_warnings_with_the_same_shape_are_grouped():
    got = classify(lines("boot_ok.log"), forbid=[], noise=[])
    assert len(got["warnings"]) == 1
    assert got["warnings"][0]["count"] == 2


def test_group_key_ignores_quoted_names_and_numbers():
    a = group_key("WARNING: MyMod: group 'alpha' has no owner")
    b = group_key("WARNING: MyMod: group 'beta' has no owner")
    assert a == b


def test_known_engine_noise_is_separated_from_warnings():
    got = classify(lines("boot_noise.log"), forbid=[], noise=DEFAULT_NOISE)
    assert got["warnings"] == []
    assert sum(n["count"] for n in got["noise"]) == 3
