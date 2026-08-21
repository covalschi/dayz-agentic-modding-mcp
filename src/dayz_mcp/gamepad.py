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

THE PROPERTY THIS MODULE IS BUILT AROUND: a stick that stays engaged is a
character running forever with nobody watching. Every engagement therefore
lives inside a `try/finally` (`_run_hold`), the release has a fallback that
resets the whole report, the fallback has a fallback that drops the device off
the bus entirely, and `atexit` neutralises anything still held when the
interpreter exits. Process death is itself a release: ViGEmBus removes a
virtual device when the process that created it goes away, so a hard kill
leaves the game seeing no pad rather than a held one.

Analog triggers (LT/RT) are deliberately not here. They are axes, not buttons,
so they do not fit `press`, and nothing in this phase needs them; adding them
means adding a `trigger()` of their own, not stretching the button set.
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

# One pad per process. Creating a virtual device per call is slow and churns a
# kernel driver for no reason. The lock covers both this reference and the
# report mutations below, which are read-modify-write on a shared device.
_pad = None
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
    """`(pad, None)` when a device is available, `(None, refusal)` when not.

    The failure is NOT cached. The refusal exists to get the driver installed,
    and a cached "no driver" would mean the owner doing exactly what the
    message asked and still seeing it fail until the server is restarted.
    """
    global _pad
    with _lock:
        if _pad is not None:
            return _pad, None
        try:
            # Creating it is the whole probe: vgamepad attaches the device to
            # the bus here and ships a neutral report of its own, so a device
            # that comes back exists and is at rest. Sending another report to
            # confirm that would prove nothing the constructor has not.
            pad = _new_pad()
        except ImportError as exc:
            return None, fail(
                f"the python package vgamepad is not importable: {exc}", INSTALL_HINT
            )
        except Exception as exc:
            # What a missing driver actually looks like: vgamepad raises a
            # bare Exception carrying VIGEM_ERROR_BUS_NOT_FOUND, or asserts
            # that the device could not attach. Neither is an ImportError,
            # and neither may reach a caller as a traceback.
            return None, fail(
                f"no virtual gamepad: ViGEmBus would not accept a virtual device ({exc})",
                INSTALL_HINT,
            )
        _pad = pad
        return pad, None


def _drop(pad) -> None:
    """Forget `pad` without touching it. Dropping the last reference makes
    vgamepad remove the target from the bus, which is itself a release: a game
    sees no pad at all rather than one with an input held down.

    Only if it is still the current one -- a caller cleaning up after a device
    that failed must not throw away a replacement someone else has since
    acquired.
    """
    global _pad
    with _lock:
        if _pad is pad:
            _pad = None


def shutdown() -> None:
    """Neutralise the pad and let the device go. Registered with `atexit`, and
    used by tests to get back to a known state. A no-op when no device was
    ever created."""
    global _pad
    with _lock:
        pad, _pad = _pad, None
    if pad is None:
        return
    try:
        pad.reset()
        pad.update()
    except Exception:
        # Already on the way out, and the reference is gone either way -- so
        # the device is removed from the bus regardless.
        pass


atexit.register(shutdown)


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
        _drop(pad)
        return (
            f"the release failed ({surgical}) and resetting the report failed too "
            f"({second}); the virtual device was removed from the bus to make sure "
            f"nothing stayed held"
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
    "also fails, restart this server -- the device disappears with the process."
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

    pad, refusal = _acquire()
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
    return _verdict(data, failure, release_error)


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
    pad, refusal = _acquire()
    if refusal is not None:
        return refusal
    measured, failure, release_error = _run_hold(
        pad,
        lambda: pad.press_button(bit),
        lambda: pad.release_button(bit),
        held_for,
    )
    data = {"button": name, "seconds": held_for, "held": round(measured, 3)}
    return _verdict(data, failure, release_error)


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
