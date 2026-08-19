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


def test_crash_line_containing_noise_substring_lands_in_crashes_not_noise():
    """Crashes must take priority over noise. A crash line containing a noise
    substring must not be silently bucketed as noise."""
    got = classify(
        ["Exception Code 0xC0000005: primary storage handle was not closed before shutdown"],
        forbid=[],
        noise=DEFAULT_NOISE,
    )
    assert len(got["crashes"]) == 1
    assert "Exception Code" in got["crashes"][0]["text"]
    assert got["noise"] == []


def test_crash_regex_does_not_false_positive_on_benign_words():
    """Words like 'Crash' and 'Fatal' appear in normal log messages.
    Only specific markers indicate an actual crash."""
    # These should NOT be detected as crashes
    benign_lines = [
        "Crash reporter initialized, dump folder set",
        "Fatal errors so far: 0",
        "CrashDumps directory: /some/path",
    ]
    got = classify(benign_lines, forbid=[], noise=[])
    assert got["crashes"] == []
    # They should be treated as warnings instead (contain no ERROR keyword)
    # or noise/nothing, depending on other patterns


def test_exception_code_marker_is_detected_as_crash():
    """Exception Code is a specific marker of an actual crash."""
    got = classify(
        ["11:15:30 SCRIPT    : Exception Code 0xC0000005, memory address 0x12345678"],
        forbid=[],
        noise=[],
    )
    assert len(got["crashes"]) == 1
    assert "Exception Code" in got["crashes"][0]["text"]


def test_access_violation_code_is_detected_as_crash():
    """ACCESS_VIOLATION is a specific marker of an actual crash."""
    got = classify(
        ["11:15:30 SCRIPT    : Segmentation fault: ACCESS_VIOLATION at address 0x00000000"],
        forbid=[],
        noise=[],
    )
    assert len(got["crashes"]) == 1
    assert "ACCESS_VIOLATION" in got["crashes"][0]["text"]


def test_0xc0000005_code_is_detected_as_crash():
    """0xC0000005 is a Windows access violation code."""
    got = classify(
        ["11:15:30 SCRIPT    : Exception 0xC0000005 in module",],
        forbid=[],
        noise=[],
    )
    assert len(got["crashes"]) == 1
    assert "0xC0000005" in got["crashes"][0]["text"]


def test_group_key_preserves_identity_for_long_lines_differing_after_120_chars():
    """Two long lines identical for the first 116 chars but different afterwards
    must produce different groups, not collapse into one."""
    base = "WARNING: MyMod: this is a very long warning message about some configuration issue that spans more than"
    long_a = base + " one hundred twenty characters and ends with A"
    long_b = base + " one hundred twenty characters and ends with B"

    got = classify([long_a, long_b], forbid=[], noise=[])
    # Should have 2 distinct warning groups
    assert len(got["warnings"]) == 2
    # Each should have count=1 (not merged)
    assert got["warnings"][0]["count"] == 1
    assert got["warnings"][1]["count"] == 1


def test_engine_storage_file_not_closed_is_noise():
    """The real engine message when storage files are not properly closed must
    still be classified as noise with the narrowed pattern."""
    got = classify(
        ['WARNING]	File "$mission:storage_1/x/modstorageplayers.bin" was not closed.'],
        forbid=[],
        noise=DEFAULT_NOISE,
    )
    assert len(got["noise"]) == 1
    assert 'modstorageplayers.bin" was not closed' in got["noise"][0]["sample"]


def test_fatal_with_generic_was_not_closed_is_not_noise():
    """A FATAL line containing the generic phrase 'was not closed' must not
    be classified as noise, because the narrowed pattern only matches
    '.bin" was not closed' (engine storage files)."""
    got = classify(
        ["FATAL: primary storage handle was not closed before shutdown, data loss occurred"],
        forbid=[],
        noise=DEFAULT_NOISE,
    )
    # Should NOT be in noise
    assert got["noise"] == []
    # Should be in errors (FATAL keyword)
    assert len(got["errors"]) == 1
    assert got["errors"][0]["kind"] == "error"
    assert "FATAL:" in got["errors"][0]["text"]


def test_fatal_keyword_is_classified_as_error():
    """Lines marked FATAL must be classified as errors at error level,
    not swallowed by noise or any other bucket."""
    got = classify(
        ["11:15:30 SCRIPT    : FATAL: Something went wrong and cannot be recovered"],
        forbid=[],
        noise=[],
    )
    assert len(got["errors"]) == 1
    assert got["errors"][0]["kind"] == "error"
    assert "FATAL:" in got["errors"][0]["text"]
    assert got["crashes"] == []  # FATAL is error-level, not crash-level
