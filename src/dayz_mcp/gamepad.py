"""Virtual gamepad: the only input channel the DayZ engine actually accepts.

WHY THIS EXISTS, measured against a live client on 2026-08-21 (phase 3, task 1;
see specs/2026-08-21-dayz-mcp-phase3-client.md Sec 2 in the hub repo):

    SendInput scancodes, foreground verified, 25 s of forward   ->  0.000000 units
    PostMessage/SendMessage WM_KEYDOWN, main and child windows  ->  0 units, and
                                                                    no UI reaction
    virtual gamepad, left stick forward                         ->  24 m travelled

Enfusion reads movement from raw input and ignores emulated keystrokes
entirely; a virtual pad is read as real hardware, so it is not filtered. Two
further measurements shape the whole design:

  * it works with the game window UNFOCUSED -- 13.06 m travelled while a
    third-party application held the foreground;
  * buttons drive the interface too, not just movement: RB switched the
    options tab, BACK opened the inventory, B left the menu -- all unfocused.

Typing text is the one thing a pad cannot do (DayZ has no on-screen keyboard),
and it is the only job left for keyboard injection.

VIGEM EMULATES GAMEPADS AND NOTHING ELSE. The bus offers exactly two device
types -- `VX360Gamepad` and `VDS4Gamepad` -- and there is no virtual keyboard
or virtual mouse to be had this way, however natural the question is. The
alternatives are a filter driver (Interception) or a custom signed HID driver.
Interception was tried on this machine on 2026-08-21 and took away ALL
keyboard and mouse input until it was unwound; it is not a route this project
takes. Text therefore goes through injection into the focused window, and
there is no pad-shaped path to a keystroke.

THE PROPERTY THIS MODULE IS BUILT AROUND: a stick that stays engaged is a
character running forever with nobody watching. Every engagement therefore
lives inside a `try/finally` (`_run_hold`), the release has a fallback that
resets the whole report, the fallback has a fallback that drops the device off
the bus entirely, and `atexit` neutralises anything still held when the
interpreter exits. Process death is itself a release: ViGEmBus removes a
virtual device when the process that created it goes away, so a hard kill
leaves the game seeing no pad rather than a held one.

THE DEVICE HAS A SESSION LIFECYCLE, one per process rather than one per call:
`open_pad` plugs it in, `status` says whether it is plugged in and since when,
`close_pad` unplugs it. Plugging in is OBSERVABLE INSIDE THE GAME -- DayZ
switches its on-screen hints to controller mode the moment a controller
appears (measured in task 1) -- so it is a change to the system under test,
like adding a mod to the stand, and `open_pad` says so in its answer instead
of doing it quietly. `move`/`look`/`press` still open the device themselves
when nothing is open, so a caller who only wants to walk is not made to
perform ceremony; what they must never do is create a SECOND device, or hand
one back at the end of every call and make the game watch a controller
connect and disconnect all session long.

Analog triggers (LT/RT) are axes, not buttons, so they do not fit `press` and
were left out of the original button set. They are here now as a `trigger()`
of their own, because DayZ binds FIRE to RT and RAISE/AIM to LT: with only
buttons, a pad can walk the character and drive every menu but can never make
the weapon under test discharge, which is the one act a firearm mod exists to
produce. Same contract as the sticks -- clamped to [0, 1], bounded hold,
released in a `finally`.
"""
from __future__ import annotations

import atexit
import math
import threading
import time

from .errors import Result, fail, ok

# Indirected so tests can make a hold instantaneous, and so every wait in this
# module goes through one place.
_sleep = time.sleep

# Nothing may hold an input longer than this. An unbounded hold is the same
# failure as a stuck stick with extra steps -- the ceiling is what makes
# "forever" unrepresentable rather than merely unlikely. Thirty seconds is
# comfortably longer than any single measured leg (the 24 m run took 6 s) and
# short enough that a runaway is over before it matters.
MAX_HOLD_SECONDS = 30.0

# A button "tap": long enough for the engine to see the report change, short
# enough not to register as a hold.
DEFAULT_PRESS_SECONDS = 0.1

# Stick range, both axes. x is positive to the RIGHT, y is positive FORWARD
# (up), matching the XInput convention the engine reads.
STICK_LIMIT = 1.0

# The closed, documented button set. A caller writes press("back") and never
# imports an enum from a third-party package, so these names are part of this
# module's contract.
#
# The values are the XInput ABI bits, written out here rather than read from
# vgamepad so this module imports on a machine that has neither the package
# nor the driver (name validation must work there -- see `press`). They cannot
# drift: they are fixed by the ABI, and a test cross-checks them against
# vgamepad's own enum wherever it is installed.
#
# GUIDE (0x0400) is deliberately absent: it belongs to the shell, not to the
# game -- pressing it opens the Xbox/Steam overlay and can take the foreground
# away from whoever is at the machine.
BUTTONS: dict[str, int] = {
    "dpad_up": 0x0001,
    "dpad_down": 0x0002,
    "dpad_left": 0x0004,
    "dpad_right": 0x0008,
    "start": 0x0010,
    "back": 0x0020,
    "left_thumb": 0x0040,
    "right_thumb": 0x0080,
    "left_shoulder": 0x0100,
    "right_shoulder": 0x0200,
    "a": 0x1000,
    "b": 0x2000,
    "x": 0x4000,
    "y": 0x8000,
}

# Analog trigger travel, 0.0 at rest to 1.0 fully depressed. The XInput ABI
# carries a trigger as a byte, but vgamepad's float API takes this range and
# does the conversion, so this module speaks the same units as the sticks.
TRIGGER_LIMIT = 1.0

# The closed trigger name set, for the same reason BUTTONS is closed: a caller
# writes trigger("right") and never imports anything from vgamepad.
TRIGGERS: tuple[str, ...] = ("left", "right")

# The one act in this phase that only the machine owner can perform, so the
# refusal has to be a set of instructions rather than a diagnosis. Both halves
# are named on purpose: the kernel driver and the pip package are separate
# installs, and a message naming only one gets half the work done.
INSTALL_HINT = (
    "Install the ViGEmBus driver from https://github.com/nefarius/ViGEmBus/releases "
    "(signed by Nefarius Software Solutions), reboot if the installer asks, then "
    "`pip install vgamepad` into the environment running this server. Both are "
    "needed: the driver is the virtual USB bus, vgamepad is how Python talks to "
    "it. Installing a kernel driver is an act for the machine owner -- this "
    "server cannot do it, and nothing else it offers requires one."
)

# Said out loud in `open_pad`'s answer, because plugging a controller in
# changes the thing being measured.
PLUG_IN_NOTICE = (
    "A virtual controller is now attached to this machine, and that is visible "
    "inside the game: DayZ switches its on-screen hints to controller mode as soon "
    "as a controller appears (measured 2026-08-21). Treat it as a change to the "
    "system under test and call `close_pad` when the session is done, so the owner "
    "is not left with a phantom controller plugged in."
)

#: The other thing WINDOWS does when a controller appears, and the reason this
#: is worth a sentence rather than a shrug: it tries to open Xbox Game Bar. On a
#: machine that HAS Game Bar nothing is seen. On one that does not -- an LTSC or
#: Server install, or a machine somebody stripped the Xbox packages from -- the
#: shell cannot resolve the `ms-gamebar` URI and puts up a dialog offering to
#: search the Microsoft Store, over and over, once per attach.
#:
#: Nothing on this side can prevent it: the shell reacts to the device arriving,
#: not to anything this server sends, and the same dialog appears for a physical
#: Xbox pad on the same machine. So it is NAMED instead, with the remedy, on the
#: one call that plugs the device in -- because an unexplained Windows dialog
#: during an automated run reads as this tool malfunctioning.
GAME_BAR_NOTICE = (
    "If Windows puts up \"Get an app to open this ms-gamebar link\", that is the "
    "shell reacting to a controller appearing, not this server failing -- it "
    "happens for a physical Xbox pad too, on any machine without Xbox Game Bar "
    "installed (LTSC, Server, or the Xbox packages removed). Two remedies, both "
    "the machine owner's to apply and both reversible: turn off Settings > Gaming "
    "> Xbox Game Bar > \"Allow your controller to open Xbox Game Bar\" (registry: "
    "HKCU\\Software\\Microsoft\\GameBar\\UseNexusForGameBarEnabled = 0), or give the "
    "URI a handler that does nothing, which is what silences it when Game Bar is "
    "not installed at all: create HKCU\\Software\\Classes\\ms-gamebar (and "
    "ms-gamebarservices, ms-gamingoverlay) each with a URL Protocol value and a "
    "shell\\open\\command default pointing at a no-op."
)

# One device per SESSION, not per call. Creating a virtual device per call is
# slow, churns a kernel driver, and makes the game watch a controller connect
# and disconnect over and over. The lock covers this reference, the timestamps
# beside it, and the report mutations below, which are read-modify-write on a
# shared device.
_pad = None
_opened_at = 0.0  # wall clock, for reporting when the device was plugged in
_opened_monotonic = 0.0  # for measuring how long it has been plugged in
_lock = threading.RLock()


def button_names() -> list[str]:
    """Every accepted `press` name, sorted. The tool layer shows this list to
    the agent, and the refusal for an unknown name repeats it."""
    return sorted(BUTTONS)


def _new_pad():
    """Create the virtual device. The ONLY place vgamepad is imported.

    The import is deliberately in here rather than at module scope: vgamepad
    connects to ViGEmBus in its own module body (it constructs a global VBus),
    so importing it eagerly would make this whole server fail to start on a
    machine without the driver -- an import-time crash instead of a refusal,
    taking every unrelated tool down with it.
    """
    import vgamepad

    return vgamepad.VX360Gamepad()


def _acquire():
    """`(pad, None, created)` when a device is available, `(None, refusal,
    False)` when not. `created` is True only for the call that actually
    plugged the device in -- the one whose caller needs to hear about the
    side effect.

    The failure is NOT cached. The refusal exists to get the driver installed,
    and a cached "no driver" would mean the owner doing exactly what the
    message asked and still seeing it fail until the server is restarted.
    """
    global _pad, _opened_at, _opened_monotonic
    with _lock:
        if _pad is not None:
            return _pad, None, False
        try:
            # Creating it is the whole probe: vgamepad attaches the device to
            # the bus here and ships a neutral report of its own, so a device
            # that comes back exists and is at rest. Sending another report to
            # confirm that would prove nothing the constructor has not.
            pad = _new_pad()
        except ImportError as exc:
            return None, fail(
                f"the python package vgamepad is not importable: {exc}", INSTALL_HINT
            ), False
        except Exception as exc:
            # What a missing driver actually looks like: vgamepad raises a
            # bare Exception carrying VIGEM_ERROR_BUS_NOT_FOUND, or asserts
            # that the device could not attach. Neither is an ImportError,
            # and neither may reach a caller as a traceback.
            return None, fail(
                f"no virtual gamepad: ViGEmBus would not accept a virtual device ({exc})",
                INSTALL_HINT,
            ), False
        _pad = pad
        _opened_at = time.time()
        _opened_monotonic = time.monotonic()
        return pad, None, True


def _forget(pad) -> bool:
    """Forget `pad` without touching it, and say whether it was the live one.

    Dropping the last reference makes vgamepad remove the target from the bus,
    which is itself a release: a game sees no pad at all rather than one with
    an input held down.

    Only if it is still the current one -- a caller cleaning up after a device
    that failed must not throw away a replacement someone else has since
    acquired.
    """
    global _pad, _opened_at, _opened_monotonic
    with _lock:
        if _pad is not pad:
            return False
        _pad = None
        _opened_at = 0.0
        _opened_monotonic = 0.0
        return True


def open_pad() -> Result:
    """Plug the virtual controller in for this session, once.

    Calling it twice is not an error and does not make a second device: the
    answer just says `created: false`. The call that actually created one
    carries `side_effect`, because the game can see a controller appear.
    """
    pad, refusal, created = _acquire()
    if refusal is not None:
        return refusal
    return ok(_device_data(created))


def close_pad() -> Result:
    """Unplug the device: neutralise it, then remove it from the bus.

    Also the `atexit` hook, and the two must not fight -- so it is idempotent
    by construction. An explicit close leaves `_pad` empty, which makes the
    one at interpreter exit a no-op rather than a second attempt on a device
    that is already gone.

    Always `ok`. If the last neutral report cannot be shipped it says so in
    `released`, but it is not a failure: letting the device go removes it from
    the bus, which is a stronger release than any report.
    """
    global _pad, _opened_at, _opened_monotonic
    with _lock:
        pad, _pad = _pad, None
        opened_monotonic, _opened_monotonic = _opened_monotonic, 0.0
        _opened_at = 0.0
    if pad is None:
        return ok({"pad": "closed", "was_open": False, "released": False})
    released = True
    try:
        pad.reset()
        pad.update()
    except Exception:
        released = False
    return ok({
        "pad": "closed",
        "was_open": True,
        "released": released,
        "open_seconds": round(max(0.0, time.monotonic() - opened_monotonic), 1),
    })


def status() -> Result:
    """Is a device plugged in, and since when.

    Deliberately does NOT create one: creating it is the observable act this
    lifecycle exists to make explicit, and a status call that plugged a
    controller into the owner's machine to answer "is one plugged in" would be
    absurd. On a machine with no driver this therefore answers "closed" rather
    than refusing -- it reports the session, not the hardware.
    """
    with _lock:
        pad = _pad
    if pad is None:
        return ok({"pad": "closed", "device": None, "opened_at": None,
                   "open_seconds": None})
    return ok(_device_data(False))


def _device_data(created: bool) -> dict:
    with _lock:
        opened_at, opened_monotonic = _opened_at, _opened_monotonic
    data = {
        "pad": "open",
        "device": "virtual Xbox 360 controller",
        "created": created,
        "opened_at": round(opened_at, 3),
        "open_seconds": round(max(0.0, time.monotonic() - opened_monotonic), 1),
    }
    if created:
        data["side_effect"] = PLUG_IN_NOTICE
        data["windows_note"] = GAME_BAR_NOTICE
    return data


atexit.register(close_pad)


def _release(pad, release) -> str:
    """Ship the release. Returns "" on success, else what went wrong.

    Three attempts at decreasing precision, because "the stick is still down"
    is the one outcome this module may not produce:
      1. the surgical release -- undo only what this call engaged, so a
         concurrent hold (sprint = stick AND button) is not cancelled;
      2. reset the entire report -- coarse, but everything comes up;
      3. drop the device -- the game stops seeing a pad at all.
    """
    try:
        with _lock:
            release()
            pad.update()
        return ""
    except Exception as first:
        surgical = first
    try:
        with _lock:
            pad.reset()
            pad.update()
        return f"the release failed ({surgical}); the whole report was reset instead"
    except Exception as second:
        removed = _forget(pad)
        fate = (
            "the virtual device was removed from the bus to make sure nothing stayed held"
            if removed
            else "the device had already been replaced, so there was nothing to remove"
        )
        return (
            f"the release failed ({surgical}) and resetting the report failed too "
            f"({second}); {fate}"
        )


def _run_hold(pad, engage, release, seconds) -> tuple[float, str, str]:
    """Engage, wait, release. Returns `(held, failure, release_error)`.

    The `finally` is the whole point: the release happens on every exit path,
    including exceptions, including KeyboardInterrupt and SystemExit. Ordinary
    exceptions are turned into a `failure` string for the envelope; the two
    that mean "this process is stopping" are allowed through afterwards,
    because a caller asking the interpreter to end should not have that
    swallowed -- but not before the device has been released.
    """
    started = time.monotonic()
    failure = ""
    try:
        with _lock:
            engage()
            pad.update()
        _sleep(seconds)
    except Exception as exc:
        failure = f"the hold failed: {exc}"
    finally:
        release_error = _release(pad, release)
    return time.monotonic() - started, failure, release_error


def _number(value, label: str) -> tuple[float, str]:
    """`(number, "")` or `(0.0, complaint)`.

    Non-finite values are refused rather than clamped: every comparison
    against NaN is false, so a clamp written the obvious way passes it
    straight through to vgamepad, where `round(nan)` raises -- inside the
    hold, where the failure is least welcome.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0, f"{label} must be a number, got {value!r}"
    if not math.isfinite(number):
        return 0.0, f"{label} must be a finite number, got {number!r}"
    return number, ""


def _seconds(value) -> tuple[float, str]:
    number, complaint = _number(value, "seconds")
    if complaint:
        return 0.0, complaint
    if number < 0:
        return 0.0, f"seconds must not be negative, got {number}"
    if number > MAX_HOLD_SECONDS:
        return 0.0, (
            f"a hold of {number}s is longer than the {MAX_HOLD_SECONDS}s ceiling"
        )
    return number, ""


_CEILING_HINT = (
    f"Hold for at most {MAX_HOLD_SECONDS} seconds at a time and repeat the call if "
    f"more is needed. The ceiling exists so a hold nobody is watching cannot run "
    f"forever."
)

_STICK_HINT = (
    f"x and y are stick deflections between -{STICK_LIMIT} and {STICK_LIMIT}: x is "
    f"positive to the right, y positive forward."
)

_RELEASE_HINT = (
    "Check that the virtual device is still present (ViGEmBus running, no driver "
    "update in progress). Call `neutral` to force everything back to rest; if that "
    "also fails, `close_pad` unplugs the device entirely, which stops the game from "
    "seeing any input at all. The device also disappears with this process."
)


def _stick(which: str, x, y, seconds) -> Result:
    """Both sticks, one implementation. `which` is "left" or "right"."""
    x_value, complaint = _number(x, "x")
    if complaint:
        return fail(complaint, _STICK_HINT)
    y_value, complaint = _number(y, "y")
    if complaint:
        return fail(complaint, _STICK_HINT)
    held_for, complaint = _seconds(seconds)
    if complaint:
        return fail(complaint, _CEILING_HINT)

    # Clamped, not refused: a real stick saturates at its own edge, and there
    # is no other sensible reading of "push it harder than all the way". It is
    # never silent though -- `clamped` and the values actually sent are both
    # in the result, so a caller who meant a different unit can see it.
    clamped_x = max(-STICK_LIMIT, min(STICK_LIMIT, x_value))
    clamped_y = max(-STICK_LIMIT, min(STICK_LIMIT, y_value))
    clamped = (clamped_x, clamped_y) != (x_value, y_value)

    pad, refusal, created = _acquire()
    if refusal is not None:
        return refusal
    apply = pad.left_joystick_float if which == "left" else pad.right_joystick_float
    measured, failure, release_error = _run_hold(
        pad,
        lambda: apply(clamped_x, clamped_y),
        lambda: apply(0.0, 0.0),
        held_for,
    )
    data = {
        "stick": which,
        "x": clamped_x,
        "y": clamped_y,
        "clamped": clamped,
        "seconds": held_for,
        "held": round(measured, 3),
    }
    return _verdict(_with_open_notice(data, created), failure, release_error)


def _with_open_notice(data: dict, created: bool) -> dict:
    """Say when THIS call was the one that plugged the controller in.

    A caller who never called `open_pad` still deserves to know a controller
    just appeared -- the game's hints change, and that is a side effect on the
    system under test, not an implementation detail.
    """
    data["opened"] = created
    if created:
        data["side_effect"] = PLUG_IN_NOTICE
        data["windows_note"] = GAME_BAR_NOTICE
    return data


def _verdict(data: dict, failure: str, release_error: str) -> Result:
    """One envelope out of the two things that can go wrong.

    A release that had to fall back is reported as a FAILURE even though the
    input did reach the game: the caller has to hear that the device did not
    behave, and this module would rather cry wolf than be quiet about the one
    thing it exists to guarantee.
    """
    if failure and release_error:
        return fail(f"{failure}; {release_error}", _RELEASE_HINT)
    if failure:
        return fail(failure, "The input was released before returning; the device is neutral.")
    if release_error:
        return fail(release_error, _RELEASE_HINT)
    return ok(data)


def move(x, y, seconds) -> Result:
    """Left stick: locomotion. x positive right, y positive forward.

    `move(0, 1, 6)` is the measured 24 m walk. Values outside [-1, 1] are
    clamped (and reported as clamped); `seconds` above `MAX_HOLD_SECONDS` is
    refused. The stick is back at rest when this returns, on every path.

    Opens the session's device if none is open yet and reports that it did
    (`opened`, plus the side effect the game can see); the device stays open
    afterwards -- `close_pad` is what unplugs it.
    """
    return _stick("left", x, y, seconds)


def look(x, y, seconds) -> Result:
    """Right stick: camera. x positive right, y positive up. Same rules as
    `move`."""
    return _stick("right", x, y, seconds)


def press(button, seconds=DEFAULT_PRESS_SECONDS) -> Result:
    """Hold one button by name, then release it.

    Names come from `button_names()` and are matched case-insensitively with
    surrounding spaces ignored. Buttons drive the interface as well as the
    character: BACK opens the inventory, the shoulder buttons move between
    menu tabs, B leaves a menu -- all measured, all with the window unfocused.

    Opens the session's device if none is open yet, exactly as `move` does.
    """
    if not isinstance(button, str):
        return fail(
            f"button must be one of the names, got {button!r}",
            f"Valid buttons: {', '.join(button_names())}.",
        )
    name = button.strip().lower()
    if name not in BUTTONS:
        # Deliberately before `_acquire`: a caller who mistyped a name should
        # be told about the name, not about a driver that is beside the point
        # -- and this way the check works on a machine with no driver at all.
        return fail(
            f"unknown button {button!r}",
            f"Valid buttons: {', '.join(button_names())}.",
        )
    held_for, complaint = _seconds(seconds)
    if complaint:
        return fail(complaint, _CEILING_HINT)

    bit = BUTTONS[name]
    pad, refusal, created = _acquire()
    if refusal is not None:
        return refusal
    measured, failure, release_error = _run_hold(
        pad,
        lambda: pad.press_button(bit),
        lambda: pad.release_button(bit),
        held_for,
    )
    data = {"button": name, "seconds": held_for, "held": round(measured, 3)}
    return _verdict(_with_open_notice(data, created), failure, release_error)


_TRIGGER_HINT = (
    f"value is trigger travel between 0 and {TRIGGER_LIMIT}: 0 is at rest, 1 is "
    f"fully depressed. Triggers are named {' and '.join(TRIGGERS)}."
)


def trigger_names() -> list[str]:
    """Every accepted `trigger` name. The tool layer shows this to the agent."""
    return list(TRIGGERS)


def trigger(which, value=TRIGGER_LIMIT, seconds=DEFAULT_PRESS_SECONDS) -> Result:
    """Hold one analog trigger at `value` for `seconds`, then let it go.

    In DayZ this is the only path to FIRE (right trigger) and to RAISE/AIM
    (left trigger). The travel is analog on purpose: a light pull is a
    different input from a full one, and a weapon that only ever sees 1.0
    cannot be tested for anything in between.

    Same guarantees as the sticks: the value is clamped rather than refused
    (and the clamp is reported), the hold is bounded by MAX_HOLD_SECONDS, and
    the trigger is back at rest when this returns on every exit path --
    including exceptions. A trigger left down is a weapon firing forever with
    nobody watching, which is the sharper form of the stuck-stick failure this
    module is built around.
    """
    if not isinstance(which, str):
        return fail(f"trigger must be one of the names, got {which!r}", _TRIGGER_HINT)
    name = which.strip().lower()
    if name not in TRIGGERS:
        # Before `_acquire`, exactly as `press` does it: a mistyped name is a
        # complaint about the name, and it must work on a machine with no
        # driver at all.
        return fail(f"unknown trigger {which!r}", _TRIGGER_HINT)
    amount, complaint = _number(value, "value")
    if complaint:
        return fail(complaint, _TRIGGER_HINT)
    held_for, complaint = _seconds(seconds)
    if complaint:
        return fail(complaint, _CEILING_HINT)

    clamped_value = max(0.0, min(TRIGGER_LIMIT, amount))
    clamped = clamped_value != amount

    pad, refusal, created = _acquire()
    if refusal is not None:
        return refusal
    apply = pad.left_trigger_float if name == "left" else pad.right_trigger_float
    measured, failure, release_error = _run_hold(
        pad,
        lambda: apply(clamped_value),
        lambda: apply(0.0),
        held_for,
    )
    data = {
        "trigger": name,
        "value": clamped_value,
        "clamped": clamped,
        "seconds": held_for,
        "held": round(measured, 3),
    }
    return _verdict(_with_open_notice(data, created), failure, release_error)


def neutral() -> Result:
    """Force everything on the pad back to rest: both sticks, every button.

    The manual escape hatch, for when something is held and nobody knows what.
    It does not CREATE a device -- nothing can be engaged on a pad that was
    never made, and conjuring one here would turn a no-op into a refusal on a
    machine with no driver.
    """
    with _lock:
        pad = _pad
    if pad is None:
        return ok({"pad": "none"})
    release_error = _release(pad, pad.reset)
    if release_error:
        return fail(release_error, _RELEASE_HINT)
    return ok({"pad": "neutral"})
