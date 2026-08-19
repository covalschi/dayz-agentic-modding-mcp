from dayz_mcp.errors import Result, ok, fail


def test_ok_carries_data():
    r = ok({"n": 1})
    assert r.ok is True
    assert r.data == {"n": 1}
    assert r.error == ""


def test_fail_carries_error_and_hint():
    r = fail("profile not found", hint="expected dayz-mcp.toml in the repo root")
    assert r.ok is False
    assert r.data is None
    assert "profile" in r.error
    assert "dayz-mcp.toml" in r.hint


def test_to_dict_is_flat_and_json_ready():
    assert ok(1).to_dict() == {"ok": True, "data": 1, "error": "", "hint": ""}
