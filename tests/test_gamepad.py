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
    TRIGGER_LIMIT,
    button_names,
    look,
    move,
    neutral,
    press,
    trigger,
    trigger_names,
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
        self.lt = self.rt = 0.0
        self.buttons = 0
        self.updates: list[tuple] = []

    def left_joystick_float(self, x_value_float, y_value_float):
        self.lx, self.ly = x_value_float, y_value_float

    def right_joystick_float(self, x_value_float, y_value_float):
        self.rx, self.ry = x_value_float, y_value_float

    def left_trigger_float(self, value_float):
        self.lt = value_float

    def right_trigger_float(self, value_float):
        self.rt = value_float

    def press_button(self, button):
        self.buttons |= int(button)

    def release_button(self, button):
        self.buttons &= ~int(button)

    def reset(self):
        self.lx = self.ly = self.rx = self.ry = 0.0
        self.lt = self.rt = 0.0
        self.buttons = 0

    def update(self):
        # The triggers go on the END so every existing positional assertion in
        # this file keeps meaning what it did before the axes were added.
        self.updates.append(
            (self.lx, self.ly, self.rx, self.ry, self.buttons, self.lt, self.rt)
        )

    # Convenience for the assertions below, not part of the vgamepad API.
    @property
    def last(self):
        return self.updates[-1]


NEUTRAL = (0.0, 0.0, 0.0, 0.0, 0, 0.0, 0.0)


@pytest.fixture
def pad(monkeypatch):
    """A fake pad installed as the process-wide one, and instant holds.

    `_sleep` is replaced by a recorder so the whole file runs in milliseconds
    while still proving which duration was actually requested.
    """
    gamepad.close_pad()
    fake = FakePad()
    monkeypatch.setattr(gamepad, "_new_pad", lambda: fake)
    monkeypatch.setattr(gamepad, "_sleep", lambda seconds: None)
    yield fake
    gamepad.close_pad()


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
        ("trigger", lambda: trigger("right", 1.0, 0.1)),
    ]


@pytest.mark.parametrize("name,call", _every_entry_point(), ids=lambda v: getattr(v, "__name__", v))
def test_a_missing_driver_refuses_instead_of_raising(monkeypatch, name, call):
    """This is the one place in the whole client phase that needs an act from
    the machine owner, so the refusal has to say what to install and where.
    A traceback reaching the caller is a defect: the pad is constructed lazily
    inside a tool call, and a tool must answer with an envelope.
    """
    gamepad.close_pad()

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
    gamepad.close_pad()

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
    assert pad.updates[0] == (0.0, 1.0, 0.0, 0.0, 0, 0.0, 0.0)
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
    gamepad.close_pad()
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
        gamepad.close_pad()


def test_the_release_is_surgical_so_holds_can_be_combined(pad):
    """Sprint is a stick and a button at the same time. If releasing a stick
    reset the whole report, a movement hold finishing would cancel a button
    another hold is still holding -- so a release touches only what its own
    call engaged. Nothing else on the pad moves.
    """
    pad.press_button(BUTTONS["a"])
    pad.right_joystick_float(0.5, 0.25)
    move(0.0, 1.0, 0.1)
    assert pad.last == (0.0, 0.0, 0.5, 0.25, BUTTONS["a"], 0.0, 0.0)


def test_releasing_a_button_leaves_the_sticks_alone(pad):
    pad.left_joystick_float(0.0, 1.0)
    press("a", 0.1)
    assert pad.last == (0.0, 1.0, 0.0, 0.0, 0, 0.0, 0.0)


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
        result = gamepad.open_pad()
        assert result.ok, result
        gamepad._pad.buttons = 1             # leave the device engaged
        """
    )
    subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                   check=True, cwd=str(tmp_path))
    assert marker.exists(), "nothing was shipped to the device at interpreter exit"
    assert marker.read_text(encoding="utf-8").strip() == "update buttons=0"


def test_neutral_releases_everything_at_once(pad):
    gamepad.open_pad()  # the device has to exist before it can be held
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
    gamepad.close_pad()

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
    gamepad.close_pad()

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
# Triggers: analog, and the only way to make a weapon fire
# --------------------------------------------------------------------------

def test_the_trigger_set_is_closed_and_documented():
    """Same contract as the buttons: a caller writes trigger("right") and never
    imports anything from vgamepad, so the two names are part of this module.
    """
    assert trigger_names() == ["left", "right"]


def test_an_unknown_trigger_is_refused_with_the_valid_names(pad):
    result = trigger("middle", 1.0, 0.1)
    assert result.ok is False
    assert "left" in result.hint and "right" in result.hint
    assert pad.updates == []


def test_an_unknown_trigger_is_refused_without_a_driver(monkeypatch):
    """The name check is pure and happens first, exactly as it does for a
    button: the message is about the name, not about a driver that is beside
    the point.
    """
    gamepad.close_pad()

    def never():
        raise AssertionError("a bad trigger name reached the driver")

    monkeypatch.setattr(gamepad, "_new_pad", never)
    result = trigger("middle", 1.0, 0.1)
    assert result.ok is False
    assert "middle" in result.error


def test_trigger_names_are_case_insensitive_and_forgiving_of_spacing(pad):
    assert trigger("  RIGHT ", 1.0, 0.1).ok is True
    assert pad.updates[0][6] == TRIGGER_LIMIT


def test_a_trigger_engages_then_releases_exactly_that_axis(pad):
    """The release is the property this module is built around, and a trigger
    is its sharpest case: one left down is a weapon firing forever with nobody
    watching.
    """
    trigger("left", 1.0, 0.1)
    assert pad.updates[0][5] == 1.0
    assert pad.last[5] == 0.0
    assert pad.last == NEUTRAL


def test_the_two_triggers_are_independent(pad):
    trigger("right", 0.5, 0.1)
    assert pad.updates[0][5] == 0.0
    assert pad.updates[0][6] == 0.5


def test_trigger_travel_is_analog_and_passes_through_untouched(pad):
    """A light pull is a different input from a full one -- that is the whole
    reason this is an axis and not a button -- so a value in range must reach
    the device as given.
    """
    result = trigger("right", 0.25, 0.1)
    assert result.data["value"] == 0.25
    assert result.data["clamped"] is False
    assert pad.updates[0][6] == 0.25


def test_out_of_range_trigger_values_are_clamped_and_reported(pad):
    """Clamped rather than refused, for the same reason a stick is: hardware
    saturates at its own edge. A trigger has no negative half, so anything
    below zero is at rest.
    """
    high = trigger("right", 5.0, 0.1)
    assert (high.data["value"], high.data["clamped"]) == (TRIGGER_LIMIT, True)
    low = trigger("right", -2.0, 0.1)
    assert (low.data["value"], low.data["clamped"]) == (0.0, True)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), "hard", None])
def test_a_nonsensical_trigger_value_is_refused_before_the_pad_is_touched(pad, bad):
    result = trigger("right", bad, 0.1)
    assert result.ok is False
    assert pad.updates == []


def test_the_ceiling_covers_triggers_too(pad):
    result = trigger("right", 1.0, MAX_HOLD_SECONDS + 0.1)
    assert result.ok is False
    assert pad.updates == []


def test_a_trigger_defaults_to_a_tap(pad, slept):
    """A tap is one shot in semi-auto, which is the common case; a hold is what
    tells a full-auto mode from a semi-auto one, and that is asked for.
    """
    assert trigger("right").ok is True
    assert slept == [gamepad.DEFAULT_PRESS_SECONDS]


def test_our_trigger_methods_are_the_ones_vgamepad_offers():
    """The float API is called by name from this module, so a rename upstream
    would break it silently. Skipped, not failed, where vgamepad is absent.
    """
    vg = pytest.importorskip("vgamepad")
    for name in ("left_trigger_float", "right_trigger_float"):
        assert callable(getattr(vg.VX360Gamepad, name)), name


# --------------------------------------------------------------------------
# The session lifecycle: plugged in once, on purpose, and unplugged on purpose
# --------------------------------------------------------------------------

def test_open_pad_creates_the_device_once_and_reports_what_it_made(monkeypatch):
    """One device per session, not per call. Calling open twice is not an
    error and must not produce a second controller -- the second answer just
    says it created nothing.
    """
    gamepad.close_pad()
    made: list[FakePad] = []

    def factory():
        made.append(FakePad())
        return made[-1]

    monkeypatch.setattr(gamepad, "_new_pad", factory)
    try:
        first = gamepad.open_pad()
        assert first.ok is True
        assert first.data["created"] is True
        assert first.data["pad"] == "open"
        assert "controller" in first.data["device"]
        assert first.data["opened_at"] > 0

        second = gamepad.open_pad()
        assert second.ok is True
        assert second.data["created"] is False
        assert len(made) == 1
    finally:
        gamepad.close_pad()


def test_open_pad_says_that_plugging_in_is_visible_inside_the_game(pad):
    """Attaching a controller is not a neutral act: DayZ switches its on-screen
    hints to controller mode the moment one appears (measured in task 1). That
    is a change to the system under test, like adding a mod to the stand, so
    the answer names it instead of letting it be discovered from a screenshot.
    The idempotent second call does NOT repeat it -- nothing was plugged in.
    """
    first = gamepad.open_pad()
    assert "controller" in first.data["side_effect"]
    assert "close_pad" in first.data["side_effect"]
    assert "side_effect" not in gamepad.open_pad().data


def test_open_pad_refuses_without_a_driver(monkeypatch):
    gamepad.close_pad()

    def no_driver():
        raise Exception("VIGEM_ERROR_BUS_NOT_FOUND")

    monkeypatch.setattr(gamepad, "_new_pad", no_driver)
    result = gamepad.open_pad()
    assert result.ok is False
    assert "github.com/nefarius/ViGEmBus/releases" in result.hint


def test_close_pad_unplugs_neutralises_and_is_idempotent(monkeypatch):
    """A session that is done must not leave a phantom controller attached to
    the owner's machine. Closing twice is harmless by construction -- it has
    to be, because it is also the atexit hook.
    """
    gamepad.close_pad()
    made: list[FakePad] = []

    def factory():
        made.append(FakePad())
        return made[-1]

    monkeypatch.setattr(gamepad, "_new_pad", factory)
    try:
        gamepad.open_pad()
        first = gamepad.close_pad()
        assert first.ok is True
        assert first.data["was_open"] is True
        assert first.data["released"] is True
        assert made[0].last == NEUTRAL

        second = gamepad.close_pad()
        assert second.ok is True
        assert second.data["was_open"] is False
        assert len(made[0].updates) == 1, "a closed device was written to again"
    finally:
        gamepad.close_pad()


def test_close_pad_still_unplugs_a_device_it_cannot_write_to(monkeypatch):
    """Letting the device go removes it from the bus, which is a stronger
    release than any report -- so a final report that will not ship is worth
    reporting but is not a failure.
    """
    gamepad.close_pad()

    class DeadPad(FakePad):
        def update(self):
            raise OSError("device gone")

    monkeypatch.setattr(gamepad, "_new_pad", DeadPad)
    try:
        gamepad.open_pad()
        result = gamepad.close_pad()
        assert result.ok is True
        assert result.data["released"] is False
        assert gamepad.status().data["pad"] == "closed"
    finally:
        gamepad.close_pad()


def test_status_does_not_plug_anything_in(monkeypatch):
    """A status call that attached a controller to the owner's machine in
    order to answer "is a controller attached" would be absurd -- and on a
    machine with no driver it would refuse instead of answering.
    """
    gamepad.close_pad()

    def never():
        raise AssertionError("status() created a device")

    monkeypatch.setattr(gamepad, "_new_pad", never)
    result = gamepad.status()
    assert result.ok is True
    assert result.data == {"pad": "closed", "device": None, "opened_at": None,
                           "open_seconds": None}


def test_status_reports_an_open_device_and_how_long_it_has_been_open(pad):
    opened = gamepad.open_pad()
    result = gamepad.status()
    assert result.ok is True
    assert result.data["pad"] == "open"
    assert result.data["created"] is False  # status never creates anything
    assert result.data["opened_at"] == opened.data["opened_at"]
    assert result.data["open_seconds"] >= 0


def test_a_plain_move_opens_the_device_without_ceremony_and_says_it_did(pad):
    """No ceremony for a caller who just wants to walk -- but the controller
    still appeared, and the answer says which call did it.
    """
    first = move(0.0, 1.0, 0.1)
    assert first.ok is True
    assert first.data["opened"] is True
    assert "controller" in first.data["side_effect"]

    second = move(0.0, 1.0, 0.1)
    assert second.data["opened"] is False
    assert "side_effect" not in second.data
    assert press("a", 0.1).data["opened"] is False


def test_a_finished_call_leaves_the_device_plugged_in(pad):
    """The difference between a session device and a lazy per-call one: after
    a hold the input is released but the controller stays attached, so the
    game does not watch it connect and disconnect all session long.
    """
    move(0.0, 1.0, 0.1)
    assert gamepad.status().data["pad"] == "open"
    assert pad.last == NEUTRAL


def test_an_explicit_close_leaves_nothing_for_atexit_to_redo(tmp_path):
    """The safety net must not fight the explicit close. After close_pad the
    device is already gone, so interpreter exit has nothing to write -- one
    report in the file, not two.
    """
    marker = tmp_path / "reports.txt"
    script = textwrap.dedent(
        f"""
        from dayz_mcp import gamepad

        class Pad:
            def __init__(self):
                self.buttons = 1
            def reset(self):
                self.buttons = 0
            def update(self):
                open(r"{marker}", "a").write("report\\n")
            def left_joystick_float(self, x, y): pass
            def right_joystick_float(self, x, y): pass
            def press_button(self, b): pass
            def release_button(self, b): pass

        gamepad._new_pad = Pad
        gamepad.open_pad()
        closed = gamepad.close_pad()
        assert closed.data["was_open"] is True, closed
        """
    )
    subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                   check=True, cwd=str(tmp_path))
    assert marker.read_text(encoding="utf-8").splitlines() == ["report"]


# --------------------------------------------------------------------------
# One pad per process
# --------------------------------------------------------------------------

def test_the_pad_is_created_once_and_reused(monkeypatch):
    """Creating a virtual device per call is slow and churns a kernel driver
    for no reason.
    """
    gamepad.close_pad()
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
        gamepad.close_pad()


def test_a_closed_device_is_not_reused_by_the_next_call(monkeypatch):
    gamepad.close_pad()
    made: list[FakePad] = []

    def factory():
        made.append(FakePad())
        return made[-1]

    monkeypatch.setattr(gamepad, "_new_pad", factory)
    monkeypatch.setattr(gamepad, "_sleep", lambda seconds: None)
    try:
        move(0.0, 1.0, 0.1)
        gamepad.close_pad()
        assert made[0].last == NEUTRAL
        move(0.0, 1.0, 0.1)
        assert len(made) == 2
    finally:
        gamepad.close_pad()


def test_a_driver_installed_mid_session_starts_working(monkeypatch):
    """The failure is not cached. The owner installing the driver is the whole
    point of the refusal message, and a cached "no driver" would make them
    restart the server to see it take effect.
    """
    gamepad.close_pad()
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
        gamepad.close_pad()


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
    gamepad.close_pad()
    try:
        opened = gamepad.open_pad()
    except Exception as exc:  # pragma: no cover - defensive
        pytest.fail(f"open_pad raised instead of refusing: {exc!r}")
    if not opened.ok:
        pytest.skip(f"no virtual gamepad on this machine: {opened.error}")
    try:
        assert opened.data["created"] is True
        assert "controller" in opened.data["side_effect"]
        pad = gamepad._pad
        for method in ("left_joystick_float", "right_joystick_float",
                       "press_button", "release_button", "reset", "update"):
            assert hasattr(pad, method), method
        assert move(0.0, 0.0, 0.0).ok is True
        assert look(0.0, 0.0, 0.0).ok is True
        assert gamepad.status().data["pad"] == "open"
        result = neutral()
        assert result.ok is True
        assert result.data["pad"] == "neutral"
    finally:
        closed = gamepad.close_pad()
    assert closed.data == {"pad": "closed", "was_open": True, "released": True,
                           "open_seconds": closed.data["open_seconds"]}
    assert gamepad.status().data["pad"] == "closed"
