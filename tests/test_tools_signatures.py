"""`server_signatures`: read, and deliberately change, the stand's signature policy.

This is the one tool in the set that edits a file the machine's owner wrote, and
the one setting in that file with a security meaning. So the whole design is
about being unable to do it by accident and being able to undo it in one call:

* it touches only the config the profile names as the stand's, and refuses one
  that resolves outside `machine.stand_root`;
* it reports the value it found before it writes, so the previous state is in
  the answer rather than in somebody's memory;
* it reads the file back afterwards, because "it was written" is otherwise this
  tool's own claim about itself;
* it refuses while a server is running against that config, where the change
  would silently not apply.
"""
import textwrap
from pathlib import Path

import pytest

from dayz_mcp import tools
from dayz_mcp.tools import session

PROFILE = """
[project]
name = "my-mod"

[build]
mods = ["MyMod"]
"""

CONFIG = (
    "hostname = \"stand\";\r\n"
    "verifySignatures = 2;       // Verifies .pbos against .bisign files. (only 2 is supported)\r\n"
    "forceSameBuild = 1;\r\n"
)


def make_project(tmp_path: Path, config_text: str = CONFIG, name: str = "serverDZ.cfg"):
    root = tmp_path / "project"
    root.mkdir(parents=True)
    (root / "dayz-mcp.toml").write_text(textwrap.dedent(PROFILE), encoding="utf-8")
    (root / "MyMod").mkdir()
    (root / "MyMod" / "config.cpp").write_text("", encoding="utf-8")

    stand = tmp_path / "stand"
    (stand / "profiles").mkdir(parents=True)
    game = tmp_path / "game"
    game.mkdir()
    (game / "DayZDiag_x64.exe").write_bytes(b"")
    (stand / name).write_bytes(config_text.encode("utf-8"))

    lines = ["[machine]", f'stand_root = "{stand.as_posix()}"', f'game = "{game.as_posix()}"']
    if name != "serverDZ.cfg":
        lines.append(f'config = "{name}"')
    (root / "dayz-mcp.local.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")

    session.reset()
    opened = tools.project_open(str(root))
    assert opened.ok, opened.error
    return root, stand


# ------------------------------------------------------------------- reading


def test_it_refuses_without_a_project(tmp_path):
    session.reset()
    result = tools.server_signatures()
    assert not result.ok
    assert result.hint


def test_with_no_argument_it_only_reports(tmp_path):
    _root, stand = make_project(tmp_path)
    before = (stand / "serverDZ.cfg").read_bytes()

    result = tools.server_signatures()
    assert result.ok, result.error
    assert result.data["value"] == 2
    assert result.data["changed"] is False
    assert str(stand / "serverDZ.cfg") == result.data["config"]
    assert (stand / "serverDZ.cfg").read_bytes() == before, "a read must not write"


def test_a_config_that_states_nothing_says_so(tmp_path):
    _root, stand = make_project(tmp_path, "hostname = \"stand\";\r\n")
    result = tools.server_signatures()
    assert result.ok, result.error
    assert result.data["value"] is None
    assert "does not state" in result.data["note"]


# ------------------------------------------------------------------- writing


def test_setting_it_changes_the_value_and_reads_it_back(tmp_path):
    _root, stand = make_project(tmp_path)
    result = tools.server_signatures(0)
    assert result.ok, result.error
    assert result.data["was"] == 2
    assert result.data["value"] == 0
    assert result.data["changed"] is True

    on_disk = (stand / "serverDZ.cfg").read_text(encoding="utf-8")
    assert "verifySignatures = 0;" in on_disk


def test_everything_else_in_the_file_survives(tmp_path):
    """The owner wrote this file. A tool that rewrote its comments or dropped a
    key it did not understand would be a tool nobody lets near it twice."""
    _root, stand = make_project(tmp_path)
    tools.server_signatures(0)
    on_disk = (stand / "serverDZ.cfg").read_text(encoding="utf-8")
    assert 'hostname = "stand";' in on_disk
    assert "forceSameBuild = 1;" in on_disk
    assert "only 2 is supported" in on_disk, "the comment on the line itself survives"


def test_line_endings_survive(tmp_path):
    """The stand's real config is CRLF. Rewriting it as LF would show up as a
    whole-file change in every diff the owner ever looks at."""
    _root, stand = make_project(tmp_path)
    tools.server_signatures(0)
    raw = (stand / "serverDZ.cfg").read_bytes()
    assert raw.count(b"\r\n") == 3
    assert raw.count(b"\n") == raw.count(b"\r\n"), "no bare LF was introduced"


def test_setting_the_value_it_already_has_writes_nothing(tmp_path):
    _root, stand = make_project(tmp_path)
    before = (stand / "serverDZ.cfg").read_bytes()
    result = tools.server_signatures(2)
    assert result.ok, result.error
    assert result.data["changed"] is False
    assert (stand / "serverDZ.cfg").read_bytes() == before


def test_a_config_that_states_nothing_gets_the_key_added(tmp_path):
    _root, stand = make_project(tmp_path, "hostname = \"stand\";\r\n")
    result = tools.server_signatures(2)
    assert result.ok, result.error
    assert result.data["added"] is True
    assert result.data["value"] == 2
    on_disk = (stand / "serverDZ.cfg").read_text(encoding="utf-8")
    assert "verifySignatures = 2;" in on_disk
    assert 'hostname = "stand";' in on_disk


# ------------------------------------------------------------------ refusals


@pytest.mark.parametrize("bad", [1, 3, -1, 42])
def test_only_the_values_the_engine_supports_are_accepted(tmp_path, bad):
    """DayZ's own config comment says only 2 is supported; 0 is off. 1 is a
    legacy value that means neither, and writing it would leave a stand nobody
    can reason about."""
    _root, stand = make_project(tmp_path)
    before = (stand / "serverDZ.cfg").read_bytes()
    result = tools.server_signatures(bad)
    assert not result.ok
    assert "0" in result.hint and "2" in result.hint
    assert (stand / "serverDZ.cfg").read_bytes() == before


def test_it_refuses_while_a_server_is_running(tmp_path, monkeypatch):
    """The change would not reach a server that already read this file, and a
    tool that reported success for a setting with no effect is the exact kind
    of quiet lie this project exists to abolish."""
    _root, stand = make_project(tmp_path)
    session.set_server_pid(4321, "DayZDiag_x64.exe")
    monkeypatch.setattr("dayz_mcp.tools.lifecycle.is_alive", lambda pid, image="": True)
    before = (stand / "serverDZ.cfg").read_bytes()

    result = tools.server_signatures(0)
    assert not result.ok
    assert "4321" in result.error
    assert "server_stop" in result.hint
    assert (stand / "serverDZ.cfg").read_bytes() == before


def test_reading_is_allowed_while_a_server_is_running(tmp_path, monkeypatch):
    _root, _stand = make_project(tmp_path)
    session.set_server_pid(4321, "DayZDiag_x64.exe")
    monkeypatch.setattr("dayz_mcp.tools.lifecycle.is_alive", lambda pid, image="": True)
    result = tools.server_signatures()
    assert result.ok, result.error
    assert result.data["value"] == 2


def test_a_missing_config_is_named(tmp_path):
    _root, stand = make_project(tmp_path)
    (stand / "serverDZ.cfg").unlink()
    result = tools.server_signatures()
    assert not result.ok
    assert "serverDZ.cfg" in result.error


def test_turning_it_off_says_what_that_means(tmp_path):
    """Not a warning for its own sake: this is the one setting in the file with
    a security meaning, and the answer has to say which way it was moved."""
    _root, _stand = make_project(tmp_path)
    result = tools.server_signatures(0)
    assert result.ok, result.error
    assert "unsigned" in result.data["note"].lower()


# --- A commented-out line is not configuration ---


COMMENTED = (
    "hostname = \"stand\";\r\n"
    "// verifySignatures = 2;   an example somebody left behind\r\n"
    "verifySignatures = 0;\r\n"
)


def test_a_commented_out_line_is_not_the_policy(tmp_path):
    _root, _stand = make_project(tmp_path, COMMENTED)
    result = tools.server_signatures()
    assert result.ok, result.error
    assert result.data["value"] == 0, "the live line decides, not the example above it"


def test_the_live_line_is_the_one_that_changes(tmp_path):
    """The failure this guards is silent both ways: rewriting the comment
    reports success while the setting the engine honours stays exactly as it
    was."""
    _root, stand = make_project(tmp_path, COMMENTED)
    result = tools.server_signatures(2)
    assert result.ok, result.error
    assert result.data["was"] == 0
    assert result.data["value"] == 2

    on_disk = (stand / "serverDZ.cfg").read_text(encoding="utf-8")
    assert "// verifySignatures = 2;   an example somebody left behind" in on_disk
    assert "\nverifySignatures = 2;" in on_disk.replace("\r\n", "\n")


def test_a_config_outside_the_stand_is_refused(tmp_path):
    """It edits a file. The one containment it has is that the file must live
    inside the stand this project already boots."""
    outside = tmp_path / "outside.cfg"
    outside.write_bytes(b"verifySignatures = 2;\r\n")
    _root, stand = make_project(tmp_path)
    (stand / "..").resolve()

    import textwrap as _tw
    root = tmp_path / "project"
    (root / "dayz-mcp.local.toml").write_text(
        _tw.dedent(
            f"""
            [machine]
            stand_root = "{(tmp_path / 'stand').as_posix()}"
            game = "{(tmp_path / 'game').as_posix()}"
            config = "../outside.cfg"
            """
        ).strip() + "\n",
        encoding="utf-8",
    )
    session.reset()
    assert tools.project_open(str(root)).ok

    before = outside.read_bytes()
    result = tools.server_signatures(0)
    assert not result.ok
    assert "outside" in result.error
    assert outside.read_bytes() == before


def test_a_stray_line_ending_does_not_move_the_edit(tmp_path):
    """The stand's real config on this machine is CRLF with two bare LFs in it.
    Splitting on the dominant ending while indexing by str.splitlines() looks
    equivalent and is not: splitlines() breaks on the bare LF too, and every
    index after it points one line further down."""
    mixed = (
        "hostname = \"stand\";\r\n"
        "a = 1;\n"                      # a bare LF, exactly as the real config has
        "b = 2;\r\n"
        "verifySignatures = 2;\r\n"
        "forceSameBuild = 1;\r\n"
    )
    _root, stand = make_project(tmp_path, mixed)
    result = tools.server_signatures(0)
    assert result.ok, result.error

    on_disk = (stand / "serverDZ.cfg").read_bytes().decode("utf-8")
    assert "verifySignatures = 0;" in on_disk
    # Nothing else moved or was overwritten.
    assert "a = 1;" in on_disk
    assert "b = 2;" in on_disk
    assert "forceSameBuild = 1;" in on_disk
