"""Tests for the virtual gamepad wrapper.

None of these require the ViGEmBus driver, a virtual pad or a running game --
that is a hard requirement, not a convenience: the suite has to stay runnable
on a machine where nobody has installed a kernel driver, and the no-driver
REFUSAL is itself one of the things under test. The single test that does
touch real hardware skips itself when the driver is absent, and sends nothing
but a neutral report.
"""
import subprocess
import sys
import textwrap

import pytest

from dayz_mcp import gamepad
from dayz_mcp.gamepad import (
    BUTTONS,
    MAX_HOLD_SECONDS,
    button_names,
    look,
    move,
    neutral,
    press,
)


class FakePad:
    """Stands in for `vgamepad.VX360Gamepad`, with the same five methods this
    wrapper is allowed to call.

    `updates` is the only thing worth asserting on: a virtual pad changes
    nothing in the game until `update()` ships the current report, so the
    sequence of snapshots taken at each `update()` IS what the engine saw.
    Reading the pad's final field values instead would pass happily for a
    wrapper that engaged a stick and never sent the release.
    """

    def __init__(self):
        self.lx = self.ly = self.rx = self.ry = 0.0
        self.buttons = 0
        self.updates: list[tuple] = []

    def left_joystick_float(self, x_value_float, y_value_float):
        self.lx, self.ly = x_value_float, y_value_float

    def right_joystick_float(self, x_value_float, y_value_float):
        self.rx, self.ry = x_value_float, y_value_float

    def press_button(self, button):
        self.buttons |= int(button)

    def release_button(self, button):
        self.buttons &= ~int(button)

    def reset(self):
        self.lx = self.ly = self.rx = self.ry = 0.0
        self.buttons = 0

    def update(self):
        self.updates.append((self.lx, self.ly, self.rx, self.ry, self.buttons))

    # Convenience for the assertions below, not part of the vgamepad API.
    @property
    def last(self):
        return self.updates[-1]


NEUTRAL = (0.0, 0.0, 0.0, 0.0, 0)


@pytest.fixture
def pad(monkeypatch):
    """A fake pad installed as the process-wide one, and instant holds.

    `_sleep` is replaced by a recorder so the whole file runs in milliseconds
    while still proving which duration was actually requested.
    """
    gamepad.shutdown()
    fake = FakePad()
    monkeypatch.setattr(gamepad, "_new_pad", lambda: fake)
    monkeypatch.setattr(gamepad, "_sleep", lambda seconds: None)
    yield fake
    gamepad.shutdown()


@pytest.fixture
def slept(monkeypatch):
    recorded: list[float] = []
    monkeypatch.setattr(gamepad, "_sleep", recorded.append)
    return recorded


# --------------------------------------------------------------------------
# No driver: a refusal that names the act the machine owner has to perform
# --------------------------------------------------------------------------

def _every_entry_point():
    return [
        ("move", lambda: move(0.0, 1.0, 0.1)),
        ("look", lambda: look(1.0, 0.0, 0.1)),
        ("press", lambda: press("back", 0.1)),
    ]


@pytest.mark.parametrize("name,call", _every_entry_point(), ids=lambda v: getattr(v, "__name__", v))
def test_a_missing_driver_refuses_instead_of_raising(monkeypatch, name, call):
    """This is the one place in the whole client phase that needs an act from
    the machine owner, so the refusal has to say what to install and where.
    A traceback reaching the caller is a defect: the pad is constructed lazily
    inside a tool call, and a tool must answer with an envelope.
    """
    gamepad.shutdown()

    def no_driver():
        # What vgamepad actually raises with the package installed and the
        # driver absent: a bare Exception out of its own check_err, thrown
        # while the module body runs (it connects to the bus at import).
        raise Exception("VIGEM_ERROR_BUS_NOT_FOUND")

    monkeypatch.setattr(gamepad, "_new_pad", no_driver)
    result = call()
    assert result.ok is False, name
    assert "vigembus" in (result.error + result.hint).lower(), result
    assert "github.com/nefarius/ViGEmBus/releases" in result.hint, result
    assert "nefarius" in result.hint.lower(), result


def test_a_missing_package_names_the_package_too(monkeypatch):
    """The other half of the same act. The driver and the pip package are two
    separate installs and a caller who sees only one of them named will do
    only half the work.
    """
    gamepad.shutdown()

    def no_package():
        raise ImportError("No module named 'vgamepad'")

    monkeypatch.setattr(gamepad, "_new_pad", no_package)
    result = move(0.0, 1.0, 0.1)
    assert result.ok is False
    assert "vgamepad" in result.hint
    assert "github.com/nefarius/ViGEmBus/releases" in result.hint


def test_importing_the_module_never_touches_vgamepad(tmp_path):
    """vgamepad connects to ViGEmBus in its MODULE BODY (a global VBus is
    constructed on import), so importing it eagerly would make this whole
    server fail to start on a machine without the driver -- an import-time
    crash, not a refusal, and nothing else in the server would work either.
    Run in a subprocess because by this point in the session vgamepad may
    already be in sys.modules for an entirely different reason.
    """
    script = textwrap.dedent(
        """
        import sys
        import dayz_mcp.gamepad  # noqa: F401
        print("vgamepad" in sys.modules)
        """
    )
    out = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True,
        check=True, cwd=str(tmp_path),
    ).stdout.strip()
    assert out == "False", out


# --------------------------------------------------------------------------
# Stick values: clamped, and said so
# --------------------------------------------------------------------------

def test_out_of_range_stick_values_are_clamped_and_reported(pad):
    """Saturation is what real hardware does -- a stick cannot be pushed past
    its own edge -- so an out-of-range value is applied at the edge rather
    than refused. It is never applied SILENTLY though: the result carries the
    values that actually went to the device, plus the fact that they were
    clamped, so a caller who meant a different unit can see it.
    """
    result = move(5.0, -3.0, 0.1)
    assert result.ok is True
    assert result.data["x"] == 1.0
    assert result.data["y"] == -1.0
    assert result.data["clamped"] is True
    assert pad.updates[0][:2] == (1.0, -1.0)


def test_in_range_values_pass_through_untouched(pad):
    result = look(-0.25, 0.5, 0.1)
    assert result.ok is True
    assert (result.data["x"], result.data["y"]) == (-0.25, 0.5)
    assert result.data["clamped"] is False
    assert pad.updates[0][2:4] == (-0.25, 0.5)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_stick_values_are_refused_before_the_pad_is_touched(pad, bad):
    """NaN has no meaningful clamp: every comparison against it is false, so a
    naive clamp passes it straight through to vgamepad, where round(nan)
    raises. Refuse it, and refuse it before anything is engaged.
    """
    result = move(bad, 0.0, 0.1)
    assert result.ok is False
    assert pad.updates == []


@pytest.mark.parametrize("bad", ["forward", None, object()])
def test_non_numeric_stick_values_are_refused(pad, bad):
    result = move(bad, 0.0, 0.1)
    assert result.ok is False
    assert pad.updates == []


# --------------------------------------------------------------------------
# The ceiling on a hold
# --------------------------------------------------------------------------

def test_a_hold_longer_than_the_ceiling_is_refused(pad):
    """An unbounded hold is a character running forever with nobody watching,
    just with extra steps. Refused rather than silently shortened: a caller
    who asked for five minutes and got thirty seconds would measure the wrong
    distance and never learn why.
    """
    result = move(0.0, 1.0, MAX_HOLD_SECONDS + 0.1)
    assert result.ok is False
    assert str(int(MAX_HOLD_SECONDS)) in (result.error + result.hint)
    assert pad.updates == []


def test_the_ceiling_itself_is_allowed(pad, slept):
    assert move(0.0, 1.0, MAX_HOLD_SECONDS).ok is True
    assert slept == [MAX_HOLD_SECONDS]


def test_the_ceiling_covers_buttons_too(pad):
    result = press("a", MAX_HOLD_SECONDS + 0.1)
    assert result.ok is False
    assert pad.updates == []


@pytest.mark.parametrize("bad", [-1.0, float("nan"), "long", None])
def test_a_nonsensical_duration_is_refused(pad, bad):
    result = move(0.0, 1.0, bad)
    assert result.ok is False
    assert pad.updates == []


def test_the_requested_duration_is_the_one_slept(pad, slept):
    move(0.0, 1.0, 1.5)
    press("b", 0.25)
    assert slept == [1.5, 0.25]


# --------------------------------------------------------------------------
# The property this module exists for: the stick always comes back
# --------------------------------------------------------------------------

def test_a_normal_hold_ends_neutral(pad):
    move(0.0, 1.0, 0.1)
    assert pad.updates[0] == (0.0, 1.0, 0.0, 0.0, 0)
    assert pad.last == NEUTRAL


def test_the_stick_is_released_when_the_hold_raises(pad, monkeypatch):
    """The failure this module is built around. Something raising mid-hold is
    exactly when a stick gets left engaged, and a stuck stick is a character
    running until someone notices.
    """
    def boom(seconds):
        raise RuntimeError("something blew up mid-hold")

    monkeypatch.setattr(gamepad, "_sleep", boom)
    result = move(0.0, 1.0, 0.1)
    assert result.ok is False
    assert "something blew up mid-hold" in result.error
    assert pad.last == NEUTRAL


def test_the_stick_is_released_even_when_the_interpreter_is_interrupted(pad, monkeypatch):
    """KeyboardInterrupt and SystemExit are NOT swallowed into an envelope --
    a caller asking the process to stop gets what it asked for -- but the
    release still has to happen on the way out.
    """
    def interrupt(seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(gamepad, "_sleep", interrupt)
    with pytest.raises(KeyboardInterrupt):
        move(0.0, 1.0, 0.1)
    assert pad.last == NEUTRAL


def test_the_button_is_released_when_the_hold_raises(pad, monkeypatch):
    def boom(seconds):
        raise RuntimeError("mid-press")

    monkeypatch.setattr(gamepad, "_sleep", boom)
    result = press("back", 0.1)
    assert result.ok is False
    assert pad.last == NEUTRAL


def test_a_release_that_itself_fails_is_retried_with_a_full_reset(pad, monkeypatch):
    """Last line of defence. If the surgical release cannot be shipped, the
    wrapper falls back to resetting the whole report -- anything is better
    than returning with the stick still engaged.
    """
    calls = {"n": 0}
    real_update = pad.update

    def flaky_update():
        calls["n"] += 1
        if calls["n"] == 2:  # the release update, right after the engage one
            raise OSError("device write failed")
        real_update()

    monkeypatch.setattr(pad, "update", flaky_update)
    result = move(0.0, 1.0, 0.1)
    assert result.ok is False
    assert pad.last == NEUTRAL
    assert (pad.lx, pad.ly, pad.buttons) == (0.0, 0.0, 0)


def test_a_device_that_cannot_be_released_at_all_is_torn_down(monkeypatch):
    """The last resort. When neither the surgical release nor the full reset
    can be shipped, the virtual device itself is dropped -- removing it from
    the bus is the only remaining way to make the game stop seeing a held
    input, and it beats keeping a pad nobody can talk to.
    """
    gamepad.shutdown()
    made: list[FakePad] = []

    class DeadPad(FakePad):
        def update(self):
            raise OSError("device gone")

    def factory():
        made.append(DeadPad())
        return made[-1]

    monkeypatch.setattr(gamepad, "_new_pad", factory)
    monkeypatch.setattr(gamepad, "_sleep", lambda seconds: None)
    try:
        assert move(0.0, 1.0, 0.1).ok is False
        move(0.0, 1.0, 0.1)
        assert len(made) == 2, "an unreleasable device was kept and reused"
    finally:
        gamepad.shutdown()


def test_the_release_is_surgical_so_holds_can_be_combined(pad):
    """Sprint is a stick and a button at the same time. If releasing a stick
    reset the whole report, a movement hold finishing would cancel a button
    another hold is still holding -- so a release touches only what its own
    call engaged. Nothing else on the pad moves.
    """
    pad.press_button(BUTTONS["a"])
    pad.right_joystick_float(0.5, 0.25)
    move(0.0, 1.0, 0.1)
    assert pad.last == (0.0, 0.0, 0.5, 0.25, BUTTONS["a"])


def test_releasing_a_button_leaves_the_sticks_alone(pad):
    pad.left_joystick_float(0.0, 1.0)
    press("a", 0.1)
    assert pad.last == (0.0, 1.0, 0.0, 0.0, 0)


def test_the_atexit_safety_net_releases_on_interpreter_shutdown(tmp_path):
    """Belt and braces for a pad that ends up engaged by a path nobody
    foresaw: the module registers a release with atexit, so an ordinary
    interpreter exit still neutralises the device. Proved in a subprocess
    because it can only be observed by actually letting a process end.
    """
    marker = tmp_path / "released.txt"
    script = textwrap.dedent(
        f"""
        from dayz_mcp import gamepad

        class Pad:
            def __init__(self):
                self.buttons = 1
            def reset(self):
                self.buttons = 0
            def update(self):
                open(r"{marker}", "a").write(f"update buttons={{self.buttons}}\\n")
            def left_joystick_float(self, x, y): pass
            def right_joystick_float(self, x, y): pass
            def press_button(self, b): pass
            def release_button(self, b): pass

        gamepad._new_pad = Pad
        pad, refusal = gamepad._acquire()
        assert refusal is None, refusal
        pad.buttons = 1                      # leave the device engaged
        open(r"{marker}", "w").close()       # forget the report _acquire sent
        """
    )
    subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                   check=True, cwd=str(tmp_path))
    assert marker.exists(), "nothing was shipped to the device at interpreter exit"
    assert marker.read_text(encoding="utf-8").strip() == "update buttons=0"


def test_neutral_releases_everything_at_once(pad):
    gamepad._acquire()  # the device has to exist before it can be held
    pad.press_button(BUTTONS["a"])
    pad.left_joystick_float(1.0, 1.0)
    result = neutral()
    assert result.ok is True
    assert pad.last == NEUTRAL


def test_neutral_does_not_conjure_a_pad_that_does_not_exist(monkeypatch):
    """Nothing can be engaged on a device that was never created, so the panic
    release must not create one -- least of all on a machine with no driver,
    where it would turn a no-op into a refusal.
    """
    gamepad.shutdown()

    def never():
        raise AssertionError("neutral() created a pad")

    monkeypatch.setattr(gamepad, "_new_pad", never)
    result = neutral()
    assert result.ok is True
    assert result.data["pad"] == "none"


# --------------------------------------------------------------------------
# Buttons: a closed, documented set
# --------------------------------------------------------------------------

def test_the_button_set_is_closed_and_documented():
    """A caller writes press("back"); it does not import an enum from a
    third-party package. That means this set is part of this module's
    contract, and changing it is a deliberate act.
    """
    assert set(button_names()) == {
        "a", "b", "x", "y",
        "back", "start",
        "left_shoulder", "right_shoulder",
        "left_thumb", "right_thumb",
        "dpad_up", "dpad_down", "dpad_left", "dpad_right",
    }
    assert button_names() == sorted(button_names())


def test_an_unknown_button_is_refused_with_the_valid_names(pad):
    result = press("triangle", 0.1)
    assert result.ok is False
    assert "dpad_up" in result.hint and "back" in result.hint
    assert pad.updates == []


def test_an_unknown_button_is_refused_without_a_driver(monkeypatch):
    """Name validation is pure and happens first, so the message a caller gets
    is about the name they got wrong -- not about a driver that is beside the
    point.
    """
    gamepad.shutdown()

    def never():
        raise AssertionError("a bad button name reached the driver")

    monkeypatch.setattr(gamepad, "_new_pad", never)
    result = press("triangle", 0.1)
    assert result.ok is False
    assert "triangle" in result.error


def test_button_names_are_case_insensitive_and_forgiving_of_spacing(pad):
    assert press("  BACK ", 0.1).ok is True
    assert pad.updates[0][4] == BUTTONS["back"]


def test_a_button_defaults_to_a_tap(pad, slept):
    """A stick hold's duration is the whole point of the call, so `move` and
    `look` demand one. A button's usually is not -- "press BACK" means tap it
    -- so `press` has a default short enough to read as a tap.
    """
    assert press("a").ok is True
    assert slept == [gamepad.DEFAULT_PRESS_SECONDS]
    assert 0 < gamepad.DEFAULT_PRESS_SECONDS < 1


def test_a_button_press_engages_then_releases_exactly_that_bit(pad):
    press("right_shoulder", 0.1)
    assert pad.updates[0][4] == BUTTONS["right_shoulder"]
    assert pad.last[4] == 0


def test_our_button_bits_match_the_ones_vgamepad_uses():
    """The table is written out here rather than imported so the set stays
    closed and the module imports on a machine with no vgamepad at all. That
    is only safe while the two agree, which is what this checks -- skipped,
    not failed, where vgamepad is not installed.
    """
    vg = pytest.importorskip("vgamepad")
    for name, bit in BUTTONS.items():
        member = getattr(vg.XUSB_BUTTON, "XUSB_GAMEPAD_" + name.upper())
        assert bit == member.value, name


# --------------------------------------------------------------------------
# One pad per process
# --------------------------------------------------------------------------

def test_the_pad_is_created_once_and_reused(monkeypatch):
    """Creating a virtual device per call is slow and churns a kernel driver
    for no reason.
    """
    gamepad.shutdown()
    made: list[FakePad] = []

    def factory():
        made.append(FakePad())
        return made[-1]

    monkeypatch.setattr(gamepad, "_new_pad", factory)
    monkeypatch.setattr(gamepad, "_sleep", lambda seconds: None)
    try:
        move(0.0, 1.0, 0.1)
        look(1.0, 0.0, 0.1)
        press("a", 0.1)
        assert len(made) == 1
    finally:
        gamepad.shutdown()


def test_shutdown_neutralises_and_drops_the_pad(monkeypatch):
    gamepad.shutdown()
    made: list[FakePad] = []

    def factory():
        made.append(FakePad())
        return made[-1]

    monkeypatch.setattr(gamepad, "_new_pad", factory)
    monkeypatch.setattr(gamepad, "_sleep", lambda seconds: None)
    try:
        move(0.0, 1.0, 0.1)
        gamepad.shutdown()
        assert made[0].last == NEUTRAL
        move(0.0, 1.0, 0.1)
        assert len(made) == 2
    finally:
        gamepad.shutdown()


def test_a_driver_installed_mid_session_starts_working(monkeypatch):
    """The failure is not cached. The owner installing the driver is the whole
    point of the refusal message, and a cached "no driver" would make them
    restart the server to see it take effect.
    """
    gamepad.shutdown()
    state = {"driver": False}
    fake = FakePad()

    def factory():
        if not state["driver"]:
            raise Exception("VIGEM_ERROR_BUS_NOT_FOUND")
        return fake

    monkeypatch.setattr(gamepad, "_new_pad", factory)
    monkeypatch.setattr(gamepad, "_sleep", lambda seconds: None)
    try:
        assert move(0.0, 1.0, 0.1).ok is False
        state["driver"] = True
        assert move(0.0, 1.0, 0.1).ok is True
    finally:
        gamepad.shutdown()


# --------------------------------------------------------------------------
# The one test that touches real hardware
# --------------------------------------------------------------------------

def test_a_real_pad_accepts_neutral_reports(monkeypatch):
    """Everything above runs against a stand-in, which proves the wrapper's
    logic and nothing about vgamepad's actual API. This creates a real virtual
    device and drives it through the real code path -- but only with NEUTRAL
    values, which a game cannot tell apart from a pad sitting at rest, so it
    is safe to run while a client is connected. Skips itself wherever the
    driver is absent, since the suite must stay runnable without one.

    A real button press or a deflected stick is deliberately NOT tested here:
    it would reach whatever game happens to be running. The methods those
    paths call are checked for existence instead, which is the part a
    stand-in cannot vouch for.
    """
    gamepad.shutdown()
    try:
        pad, refusal = gamepad._acquire()
    except Exception as exc:  # pragma: no cover - defensive
        pytest.fail(f"_acquire raised instead of refusing: {exc!r}")
    if refusal is not None:
        pytest.skip(f"no virtual gamepad on this machine: {refusal.error}")
    try:
        for method in ("left_joystick_float", "right_joystick_float",
                       "press_button", "release_button", "reset", "update"):
            assert hasattr(pad, method), method
        assert move(0.0, 0.0, 0.0).ok is True
        assert look(0.0, 0.0, 0.0).ok is True
        result = neutral()
        assert result.ok is True
        assert result.data["pad"] == "neutral"
    finally:
        gamepad.shutdown()
