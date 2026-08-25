"""The client's window: find it, focus it (and verify), capture it, address it.

Everything here was measured against a live windowed client rather than reasoned
about, and three of the measurements decide the shape of the module:

* **Eyes do not need focus.** A capture is live with the window at the very
  bottom of the z-order (lit_fraction 0.985) and live when focused (0.987). It
  is IMPOSSIBLE only when the window is MINIMIZED -- the client area collapses
  to 0x0 and there is nothing to copy. So `shot` refuses a minimized window
  instead of writing a valid-looking empty image.
* **A black frame is the failure mode that reports success.** An earlier probe
  captured a D3D client from a separate Windows desktop, got a fully black
  image, and `PrintWindow` still returned 1. So `shot` measures whether the
  frame is actually non-black and reports the number. It does NOT fail on a
  dark frame -- night scenes are legitimately dark -- but a caller that gets
  0.0 can see the eyes were shut.
* **Focus is needed by exactly one thing: typing.** Windows refuses to give the
  foreground to a background process, and `SendInput` then delivers the
  keystrokes to whatever window the person at the machine is using. That
  accident is why input automation was banned here once. `focus` therefore
  verifies with `GetForegroundWindow` and returns the truth; False is a normal
  answer, and every input path in this module refuses on it rather than typing
  blind.

Background operation itself rests on a client setting, `pauseMode` -- see
`read_pause_mode`. It is READ here, never written: it belongs to the machine's
owner.
"""
from __future__ import annotations

import os
import re
import struct
import time
import zlib
from pathlib import Path

from .errors import Result, fail, ok

IS_WINDOWS = os.name == "nt"

# How long focus is given to settle before it is checked. Taken from the
# working scripts this module replaces; a foreground change is asynchronous, so
# checking immediately reads the previous foreground and reports a false refusal.
FOCUS_SETTLE_SECONDS = 0.4

# A pixel counts as lit when any channel is ABOVE this. Not zero: video
# hardware and compression leave near-black noise, and "not literally 0" would
# call a dead frame alive.
NEAR_BLACK = 8

# lit_fraction samples every Nth pixel. A prime stride so a sample never lands
# on the same column of every row (a screen full of vertical UI edges would
# otherwise be measured through a single column). 97 keeps a 3840x1600 frame at
# ~63k samples, which is a millisecond of work for a number that only has to
# answer "black or not".
LIT_SAMPLE_STRIDE = 97

# Below this the frame is essentially black and `shot` says so in a warning.
# Deliberately not a failure: a night scene is legitimately dark, and refusing
# would make the eyes useless exactly when they are needed.
BLACK_FRAME_FRACTION = 0.01

# zlib level for the PNG. The frame is a screenshot, not an archive: 6 is the
# usual quality/speed knee and keeps a 1600x900 capture well under a second.
PNG_COMPRESS_LEVEL = 6

# The pauseMode value measured on this machine as the in-game setting
# GAME -> UPDATE IN BACKGROUND = "Graphics and sound", under which the
# background capture and the background gamepad were both measured working.
# No other value's meaning was measured, so no other value is interpreted here.
BACKGROUND_PAUSE_MODE = 2

# Between key events. From the working scripts: the client drops keystrokes
# sent faster than this, and a dropped character in a chat command fails the
# test for a reason that has nothing to do with the mod.
KEY_DOWN_SECONDS = 0.03
KEY_UP_SECONDS = 0.035
SHIFT_SECONDS = 0.025

# US-layout scancodes. Scancodes, not virtual keys: the client starts on the
# machine owner's Ukrainian layout, where a virtual-key code for a letter
# produces a Cyrillic character.
SCANCODES = {
    "1": 0x02, "2": 0x03, "3": 0x04, "4": 0x05, "5": 0x06,
    "6": 0x07, "7": 0x08, "8": 0x09, "9": 0x0A, "0": 0x0B,
    "-": 0x0C, "=": 0x0D,
    "q": 0x10, "w": 0x11, "e": 0x12, "r": 0x13, "t": 0x14,
    "y": 0x15, "u": 0x16, "i": 0x17, "o": 0x18, "p": 0x19,
    "[": 0x1A, "]": 0x1B,
    "a": 0x1E, "s": 0x1F, "d": 0x20, "f": 0x21, "g": 0x22,
    "h": 0x23, "j": 0x24, "k": 0x25, "l": 0x26, ";": 0x27,
    "'": 0x28, "`": 0x29, "\\": 0x2B,
    "z": 0x2C, "x": 0x2D, "c": 0x2E, "v": 0x2F, "b": 0x30,
    "n": 0x31, "m": 0x32, ",": 0x33, ".": 0x34, "/": 0x35,
    " ": 0x39,
}

# Characters that need Shift held. Their absence is not cosmetic: an underscore
# typed as a hyphen turned a class name in a chat command into a different
# string, and the run failed as if the mod were at fault.
SHIFTED = {
    "!": 0x02, "\"": 0x28, "#": 0x04, "$": 0x05, "%": 0x06, "&": 0x08,
    "(": 0x0A, ")": 0x0B, "*": 0x09, "+": 0x0D, ":": 0x27, "<": 0x33,
    ">": 0x34, "?": 0x35, "_": 0x0C, "{": 0x1A, "|": 0x2B, "}": 0x1B,
    "~": 0x29, "^": 0x07,
}
SHIFTED["@"] = 0x03  # written apart so the line is not a mod-name shape

SHIFT_SCAN = 0x2A

# Named keys. The extended flag matters for the editing keys: their scancodes
# are shared with the numeric keypad, and without the flag the client reads
# Home/End/arrows as digits.
NAMED_KEYS = {
    "enter": (0x1C, False),
    "esc": (0x01, False),
    "escape": (0x01, False),
    "tab": (0x0F, False),
    "backspace": (0x0E, False),
    "space": (0x39, False),
    "delete": (0x53, True),
    "home": (0x47, True),
    "end": (0x4F, True),
    "left": (0x4B, True),
    "right": (0x4D, True),
    "up": (0x48, True),
    "down": (0x50, True),
    # Modifier keys, for mods that hold one rather than tap it. Caps Lock is
    # DayZ's own push-to-talk, which is why it is here at all.
    "capslock": (0x3A, False),
    "lshift": (0x2A, False),
    "lctrl": (0x1D, False),
    "lalt": (0x38, False),
}

_PAUSE_MODE = re.compile(r"(?m)^\s*pauseMode\s*=\s*(-?\d+)")

if IS_WINDOWS:  # pragma: no cover - the import itself is the platform guard
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    SW_RESTORE = 9
    GW_OWNER = 4
    PW_CLIENTONLY = 0x00000001
    PW_RENDERFULLCONTENT = 0x00000002
    # BOTH flags, and the client-only half is not optional. PrintWindow draws
    # the WHOLE window, frame included, into the top-left of the target bitmap:
    # with RENDERFULLCONTENT alone, a client-sized capture of the live client
    # (client 1600x900 inside a 1616x939 frame) came back holding 31 rows of
    # title bar at the top and missing 39 rows of HUD at the bottom -- the
    # picture no longer meant the rectangle the click coordinates mean.
    # Measured on the live D3D client: both variants return 1 and both are
    # non-black (0.438 with the frame, 0.417 client-only), so nothing but the
    # picture itself says which one is right.
    CAPTURE_FLAGS = PW_CLIENTONLY | PW_RENDERFULLCONTENT
    WM_INPUTLANGCHANGEREQUEST = 0x0050
    LANG_EN_US = 0x04090409

    INPUT_MOUSE = 0
    INPUT_KEYBOARD = 1
    KEYEVENTF_EXTENDEDKEY = 0x0001
    KEYEVENTF_KEYUP = 0x0002
    KEYEVENTF_SCANCODE = 0x0008
    MOUSEEVENTF_MOVE = 0x0001
    MOUSEEVENTF_LEFTDOWN = 0x0002
    MOUSEEVENTF_LEFTUP = 0x0004
    MOUSEEVENTF_ABSOLUTE = 0x8000

    SM_XVIRTUALSCREEN = 76
    SM_YVIRTUALSCREEN = 77
    SM_CXVIRTUALSCREEN = 78
    SM_CYVIRTUALSCREEN = 79

    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
            ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
            ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
            ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
            ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
            ("biClrImportant", wintypes.DWORD),
        ]

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx", wintypes.LONG), ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_void_p),
        ]

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.c_void_p),
        ]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [
            ("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
            ("wParamH", wintypes.WORD),
        ]

    class _INPUTBODY(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("body", _INPUTBODY)]

    # Prototypes, not defaults. On 64-bit Windows a handle is a pointer, and
    # ctypes' default int return truncates it -- a window handle above 2^31
    # then comes back mangled and every later call on it fails for no visible
    # reason. Declaring them is what makes the handles round-trip.
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindow.restype = wintypes.HWND
    user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.BringWindowToTop.argtypes = [wintypes.HWND]
    user32.SetActiveWindow.restype = wintypes.HWND
    user32.SetActiveWindow.argtypes = [wintypes.HWND]
    user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
    user32.GetDC.restype = wintypes.HDC
    user32.GetDC.argtypes = [wintypes.HWND]
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
    user32.PostMessageW.argtypes = [
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
    ]
    user32.SendInput.restype = wintypes.UINT
    user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
    user32.GetSystemMetrics.argtypes = [ctypes.c_int]
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
    gdi32.SelectObject.restype = wintypes.HGDIOBJ
    gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    gdi32.DeleteDC.argtypes = [wintypes.HDC]
    gdi32.GetDIBits.argtypes = [
        wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
        ctypes.c_void_p, ctypes.POINTER(BITMAPINFOHEADER), wintypes.UINT,
    ]
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD


def _no_windows() -> Result:
    return fail(
        "the window layer is only implemented on Windows",
        hint="the client, its window and SendInput are Windows-only; run the server on the "
             "machine that runs the game",
    )


# ---------------------------------------------------------------------------
# finding, focusing, measuring
# ---------------------------------------------------------------------------


def find_window(pid: int) -> int | None:
    """The main window of `pid`, or None when it has none.

    Visible and titled and unowned, in that order, and the FIRST such window in
    z-order: `EnumWindows` walks top-down, so a tool tip or a splash the client
    put up would be found before the real one if the owner test were dropped.

    A MINIMIZED window is still returned -- it is visible in the Windows sense,
    and `shot` has to be able to tell "minimized" from "no window at all" to
    give the caller a hint worth acting on.
    """
    if not IS_WINDOWS or pid <= 0:
        return None
    found: list[int] = []

    def collect(hwnd, _lparam):  # noqa: ANN001 - a Windows callback signature
        owner_pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner_pid))
        if owner_pid.value != pid:
            return True
        if not user32.IsWindowVisible(hwnd):
            return True
        if user32.GetWindowTextLengthW(hwnd) <= 0:
            return True
        if user32.GetWindow(hwnd, GW_OWNER):
            return True
        found.append(int(hwnd))
        return False  # stop the enumeration

    # Kept in a local so the trampoline outlives the call: a callback object
    # collected mid-enumeration crashes the process, not the call.
    callback = WNDENUMPROC(collect)
    user32.EnumWindows(callback, 0)
    return found[0] if found else None


def foreground_hwnd() -> int:
    """The window Windows currently considers foreground, 0 when there is none."""
    if not IS_WINDOWS:
        return 0
    return int(user32.GetForegroundWindow() or 0)


def foreground_pid() -> int:
    """The process owning the foreground window, 0 when unknown."""
    if not IS_WINDOWS:
        return 0
    hwnd = foreground_hwnd()
    if not hwnd:
        return 0
    owner = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
    return int(owner.value)


def is_minimized(hwnd: int) -> bool:
    if not IS_WINDOWS or not hwnd:
        return False
    return bool(user32.IsIconic(hwnd))


def client_size(hwnd: int) -> tuple[int, int]:
    """(width, height) of the CLIENT area -- (0, 0) when there is none.

    The client area, never the window rectangle: the frame carries a title bar
    and borders, so a coordinate measured against it lands above and left of
    the widget it was meant for, by whatever the current theme's decoration
    happens to be. A minimized window reports 0x0 here, which is exactly what
    was measured and why `shot` refuses one.
    """
    if not IS_WINDOWS or not hwnd:
        return (0, 0)
    rect = wintypes.RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
        return (0, 0)
    return (rect.right - rect.left, rect.bottom - rect.top)


def window_rect(hwnd: int) -> tuple[int, int, int, int]:
    """The window FRAME in screen coordinates. Reported for orientation only --
    nothing in this module computes an input coordinate from it."""
    if not IS_WINDOWS or not hwnd:
        return (0, 0, 0, 0)
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return (0, 0, 0, 0)
    return (rect.left, rect.top, rect.right, rect.bottom)


def client_origin(hwnd: int) -> tuple[int, int]:
    """Screen coordinates of the client area's (0, 0)."""
    if not IS_WINDOWS or not hwnd:
        return (0, 0)
    point = wintypes.POINT(0, 0)
    if not user32.ClientToScreen(hwnd, ctypes.byref(point)):
        return (0, 0)
    return (int(point.x), int(point.y))


def client_to_screen(hwnd: int, x: int, y: int) -> tuple[int, int]:
    """A client-area point in screen coordinates."""
    origin = client_origin(hwnd)
    return (origin[0] + int(x), origin[1] + int(y))


def geometry(pid: int) -> Result:
    """Where the client's window is and how big its client area is."""
    if not IS_WINDOWS:
        return _no_windows()
    hwnd = find_window(pid)
    if hwnd is None:
        return fail(
            f"no visible window belongs to pid {pid}",
            hint="the client may still be starting, or the pid may be wrong -- check it is "
                 "alive first",
        )
    width, height = client_size(hwnd)
    return ok({
        "hwnd": hwnd,
        "client_width": width,
        "client_height": height,
        "client_origin": list(client_origin(hwnd)),
        "window_rect": list(window_rect(hwnd)),
        "minimized": is_minimized(hwnd),
        "foreground": foreground_pid() == pid,
    })


def focus(pid: int, settle: float = FOCUS_SETTLE_SECONDS) -> bool:
    """Bring the client's window to the foreground and report whether it WORKED.

    Returning False is a normal answer, not an error. Windows refuses to hand
    the foreground to a background process, and it refuses silently:
    `SetForegroundWindow` can return success having done nothing at all. The
    only trustworthy answer is `GetForegroundWindow` afterwards, which is what
    this returns -- and the reason it must be believed is that the alternative
    is `SendInput` typing into whatever window the person at the machine has
    open. That happened here once; it is why every input path in this module
    refuses on False instead of proceeding.

    `AttachThreadInput` is what makes the request permissible at all, and the
    partner thread must be THE ONE THAT OWNS THE CURRENT FOREGROUND WINDOW --
    not the target's. Windows grants `SetForegroundWindow` to a caller that
    shares an input queue with the process currently holding the foreground;
    sharing one with the window being raised grants nothing, because that
    process has no such right to lend. Measured on this machine from a process
    holding no foreground, against a live game client (2026-08-21):

        attach to the TARGET's thread      -> foreground NOT taken
        attach to the FOREGROUND's thread  -> foreground taken

    The first is what this function did until that measurement, so `focus`
    always returned False against another process's window and the one tool
    that needs the foreground could never work. It is detached again on every
    path, including the failing one: a thread left attached to another
    process's input queue makes this process share that process's input state.
    """
    if not IS_WINDOWS:
        return False
    hwnd = find_window(pid)
    if hwnd is None:
        return False
    if foreground_pid() == pid:
        return True
    user32.ShowWindow(hwnd, SW_RESTORE)
    holder = user32.GetForegroundWindow()
    holder_thread = int(user32.GetWindowThreadProcessId(holder, None)) if holder else 0
    mine = int(kernel32.GetCurrentThreadId())
    attached = False
    if holder_thread and holder_thread != mine:
        attached = bool(user32.AttachThreadInput(mine, holder_thread, True))
    try:
        user32.SetForegroundWindow(hwnd)
        # Only meaningful while attached -- both are gated by the same
        # foreground rules, and SetActiveWindow acts within the calling
        # thread's input queue, which is the shared one for as long as the
        # attachment lasts.
        user32.BringWindowToTop(hwnd)
        user32.SetActiveWindow(hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(mine, holder_thread, False)
    time.sleep(max(0.0, settle))
    # By pid, not by handle: the question this guard answers is "will keystrokes
    # reach the client process", and a client that put up a second window of its
    # own would fail a handle comparison while the answer is still yes.
    return foreground_pid() == pid


# ---------------------------------------------------------------------------
# the frame
# ---------------------------------------------------------------------------


def lit_fraction(
    bgra: bytes, stride_pixels: int = LIT_SAMPLE_STRIDE, threshold: int = NEAR_BLACK
) -> float:
    """Fraction of sampled pixels with any channel above `threshold`.

    The measure that tells a real capture from the failure that reports
    success. On this machine a working capture reads 0.2-0.99 depending on the
    scene; a capture of a D3D window from a separate desktop read 0.0 while
    `PrintWindow` returned 1.
    """
    step = max(1, int(stride_pixels)) * 4
    lit = 0
    total = 0
    for i in range(0, len(bgra) - 3, step):
        total += 1
        if bgra[i] > threshold or bgra[i + 1] > threshold or bgra[i + 2] > threshold:
            lit += 1
    return lit / total if total else 0.0


def _chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def png_bytes(
    bgra: bytes, width: int, height: int, compress_level: int = PNG_COMPRESS_LEVEL
) -> bytes:
    """Encode a top-down BGRA buffer as a PNG, using the standard library only.

    Written out by hand rather than through an imaging package on purpose: a
    screenshot is the one thing that must work on a machine set up to run a
    game, not to run Python, and an optional dependency that is missing turns
    the eyes off entirely.

    Alpha is forced opaque. `PrintWindow` leaves the alpha channel at zero, and
    a straight copy of the DIB therefore produces a fully transparent image --
    correct bytes, invisible picture.
    """
    if width <= 0 or height <= 0:
        raise ValueError(f"a {width}x{height} image cannot be encoded")
    expected = width * height * 4
    if len(bgra) != expected:
        raise ValueError(
            f"buffer is {len(bgra)} bytes, but {width}x{height} BGRA needs {expected} -- "
            "encoding it anyway would produce a torn image and no error"
        )
    pixels = bytearray(bgra)
    # Whole-channel slice assignment, which is one C-level pass per channel.
    # The right-hand side is evaluated before either assignment, so this is a
    # real swap and not a copy of an already-overwritten channel.
    pixels[0::4], pixels[2::4] = bytes(pixels[2::4]), bytes(pixels[0::4])
    pixels[3::4] = b"\xff" * (width * height)

    stride = width * 4
    raw = bytearray()
    for row in range(height):
        raw += b"\x00"  # filter: None
        raw += pixels[row * stride:(row + 1) * stride]

    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(bytes(raw), compress_level))
        + _chunk(b"IEND", b"")
    )


def _capture(
    hwnd: int, width: int, height: int, flags: int | None = None
) -> tuple[bytes, int, int]:
    """(pixels, PrintWindow result, GetDIBits scan lines). GDI objects are
    always released -- a leaked DC or bitmap accumulates for the life of the
    server process, one per screenshot.

    `flags` defaults to CAPTURE_FLAGS and exists so a caller can ask for the
    frame as well; nothing in the tool path does."""
    window_dc = user32.GetDC(hwnd)
    mem_dc = gdi32.CreateCompatibleDC(window_dc)
    bitmap = gdi32.CreateCompatibleBitmap(window_dc, width, height)
    previous = gdi32.SelectObject(mem_dc, bitmap)
    try:
        printed = int(user32.PrintWindow(
            hwnd, mem_dc, CAPTURE_FLAGS if flags is None else flags))
        info = BITMAPINFOHEADER()
        info.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        info.biWidth = width
        info.biHeight = -height  # negative: top-down rows, as PNG wants them
        info.biPlanes = 1
        info.biBitCount = 32
        info.biCompression = 0  # BI_RGB
        buffer = ctypes.create_string_buffer(width * height * 4)
        lines = int(gdi32.GetDIBits(mem_dc, bitmap, 0, height, buffer,
                                    ctypes.byref(info), 0))
        return bytes(buffer), printed, lines
    finally:
        gdi32.SelectObject(mem_dc, previous)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(hwnd, window_dc)


def shot(pid: int, path: str | Path) -> Result:
    """Capture the client's window to a PNG. Focus is NOT required.

    Measured, so it does not have to be guessed at: the frame is live with the
    window at the bottom of the z-order and live when focused. What it cannot
    survive is the window being MINIMIZED -- the client area is 0x0 then, and
    the capture would be an empty image reported as a success. That case is
    refused with a hint saying to restore the window.

    `lit_fraction` in the reply is the honest half of the answer: this never
    fails a dark frame, because night is dark, but a caller that reads 0.0
    knows it captured nothing, which `PrintWindow`'s own return value would not
    have told it.
    """
    if not IS_WINDOWS:
        return _no_windows()
    hwnd = find_window(pid)
    if hwnd is None:
        return fail(
            f"no visible window belongs to pid {pid}",
            hint="the client may still be starting, or the pid may be wrong -- check it is "
                 "alive first",
        )
    if is_minimized(hwnd):
        return fail(
            "the client window is minimized, so there is nothing to capture "
            "(its client area collapses to 0x0)",
            hint="restore the window and try again -- the capture does NOT need focus, only a "
                 "window that is not minimized: it works with the client at the very bottom "
                 "of the z-order",
        )
    width, height = client_size(hwnd)
    if width <= 0 or height <= 0:
        return fail(
            f"the window of pid {pid} has a {width}x{height} client area",
            hint="the window exists but has no drawable area yet -- wait for the client to "
                 "finish opening it",
        )

    pixels, printed, lines = _capture(hwnd, width, height)
    if not printed:
        return fail(
            "PrintWindow refused to draw the window",
            hint="this is what a window on another Windows desktop, or one being torn down, "
                 "does -- check the client is still running on the ordinary desktop",
        )
    if lines <= 0:
        return fail(
            "the captured bitmap could not be read back (GetDIBits returned no scan lines)",
            hint="retry once; if it persists the window was destroyed between finding it and "
                 "capturing it",
        )

    frame = lit_fraction(pixels)
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(png_bytes(pixels, width, height))
    data = {
        "path": str(out),
        "width": width,
        "height": height,
        "bytes": out.stat().st_size,
        "lit_fraction": round(frame, 4),
        "foreground": foreground_pid() == pid,
    }
    if frame < BLACK_FRAME_FRACTION:
        # Only when it is actually black: a field that is always there and
        # almost always empty stops being read.
        data["warning"] = (
            f"only {frame:.4f} of sampled pixels are above near-black -- the frame is "
            "essentially black. A dark scene is legitimate, but a black frame is also what a "
            "window on a separate Windows desktop produces while PrintWindow still reports "
            "success"
        )
    return ok(data)


# ---------------------------------------------------------------------------
# the client's pauseMode -- read, never written
# ---------------------------------------------------------------------------


def parse_pause_mode(text: str) -> int | None:
    """The pauseMode value in a *_settings.DayZProfile, or None when absent.

    Anchored at the start of a line, which is how the file is written
    (`pauseMode=2;`), so a different key that merely ends in the same word is
    not mistaken for it.
    """
    found = _PAUSE_MODE.search(text)
    return int(found.group(1)) if found else None


def newest_settings_file(profile_dir: str | Path) -> Path | None:
    """The live client's settings file under `<profile_dir>/Users/*/`, or None.

    The newest FILE, not the file in the newest directory. Measured on this
    machine: several Users/* directories exist, and the newest of them held
    only DayZ.cfg (which is where the window size lives -- a different file
    entirely) and no settings at all. Picking the newest directory would have
    read the wrong client's settings, or none.
    """
    users = Path(profile_dir) / "Users"
    if not users.is_dir():
        return None
    candidates = [p for p in users.glob("*/*_settings.DayZProfile") if p.is_file()]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def read_pause_mode(profile_dir: str | Path) -> Result:
    """Read the client's UPDATE IN BACKGROUND setting. Reads only, never writes.

    This is the setting the whole background half of the client tooling rests
    on: with it at the measured value the client keeps rendering and simulating
    while another application holds the foreground, which is why a capture is
    live from the background and why the gamepad moves the character from the
    background. With "no graphics" both would stop, silently -- the frame would
    be frozen or black and nothing would say why.

    It belongs to the machine's owner, and it is set from inside the game
    (GAME -> UPDATE IN BACKGROUND). So this reports it and lets the caller warn;
    it does not rewrite somebody's settings file to make a test convenient.

    Only the value measured here is interpreted. The mapping from the other
    numbers to menu entries was never measured, so `background_verified` is
    False for them rather than a guess dressed up as a fact.
    """
    settings = newest_settings_file(profile_dir)
    if settings is None:
        return fail(
            f"no client settings file under {Path(profile_dir) / 'Users'}",
            hint="point this at the client's -profiles directory, and start the client once "
                 "if it has never run: the file is written by the game, not by this server",
        )
    try:
        text = settings.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return fail(
            f"cannot read {settings}: {exc}",
            hint="the client may be holding the file while it writes -- retry in a moment",
        )
    value = parse_pause_mode(text)
    verified = value == BACKGROUND_PAUSE_MODE
    if verified:
        note = (
            f"pauseMode={value} is the value measured on this machine as GAME -> UPDATE IN "
            "BACKGROUND = 'Graphics and sound': the client keeps drawing and simulating while "
            "another window has the foreground"
        )
    elif value is None:
        note = (
            "this client has never written pauseMode, so the in-game default applies and "
            "background capture cannot be counted on -- check GAME -> UPDATE IN BACKGROUND "
            "in the client"
        )
    else:
        note = (
            f"pauseMode={value} was never measured here; only {BACKGROUND_PAUSE_MODE} was. If "
            "it means 'no graphics', the client stops drawing while unfocused and a capture "
            "returns a frozen or black frame -- check GAME -> UPDATE IN BACKGROUND in the "
            "client. This server does not change the setting"
        )
    return ok({
        "pause_mode": value,
        "background_verified": verified,
        "settings_file": str(settings),
        "note": note,
    })


# ---------------------------------------------------------------------------
# input -- the one layer that needs the foreground
# ---------------------------------------------------------------------------


def unsupported_characters(text: str) -> list[str]:
    """Characters this module has no scancode for, in order, without repeats."""
    missing: list[str] = []
    for char in text:
        if char.lower() in SCANCODES or char in SHIFTED:
            continue
        if char not in missing:
            missing.append(char)
    return missing


def virtual_to_absolute(
    x: int, y: int, vx: int, vy: int, vw: int, vh: int
) -> tuple[int, int]:
    """A screen point as SendInput's absolute 0..65535 coordinates.

    Relative to the VIRTUAL desktop's origin, which is negative when a second
    monitor sits left of the primary one -- ignore that and every click lands
    on the wrong screen.
    """
    ax = round((x - vx) * 65535 / max(1, vw - 1))
    ay = round((y - vy) * 65535 / max(1, vh - 1))
    return (min(65535, max(0, ax)), min(65535, max(0, ay)))


def _key_event(scan: int, up: bool, extended: bool = False) -> None:
    """One keyboard event, assembled whole.

    The struct is built in one expression on purpose. The equivalent of this in
    PowerShell -- assigning to a nested field of a value type -- writes into a
    copy, the assignment is lost, and SendInput sends an EMPTY event with no
    error at all. That is what made input "not work" here the first time, and
    the lesson is written down rather than re-learned.
    """
    flags = KEYEVENTF_SCANCODE
    if extended:
        flags |= KEYEVENTF_EXTENDEDKEY
    if up:
        flags |= KEYEVENTF_KEYUP
    event = INPUT(
        type=INPUT_KEYBOARD,
        body=_INPUTBODY(ki=KEYBDINPUT(wVk=0, wScan=scan, dwFlags=flags, time=0,
                                      dwExtraInfo=None)),
    )
    user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(INPUT))


def _mouse_event(flags: int, ax: int = 0, ay: int = 0) -> None:
    event = INPUT(
        type=INPUT_MOUSE,
        body=_INPUTBODY(mi=MOUSEINPUT(dx=ax, dy=ay, mouseData=0, dwFlags=flags, time=0,
                                      dwExtraInfo=None)),
    )
    user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(INPUT))


def _tap(scan: int, shift: bool = False, extended: bool = False) -> None:
    if shift:
        _key_event(SHIFT_SCAN, up=False)
        time.sleep(SHIFT_SECONDS)
    _key_event(scan, up=False, extended=extended)
    time.sleep(KEY_DOWN_SECONDS)
    _key_event(scan, up=True, extended=extended)
    time.sleep(KEY_UP_SECONDS)
    if shift:
        _key_event(SHIFT_SCAN, up=True)
        time.sleep(SHIFT_SECONDS)


def _require_focus(pid: int) -> Result | None:
    """None when the client is verifiably in the foreground, a refusal otherwise."""
    if not IS_WINDOWS:
        return _no_windows()
    if find_window(pid) is None:
        return fail(
            f"no visible window belongs to pid {pid}",
            hint="the client may still be starting, or the pid may be wrong -- check it is "
                 "alive first",
        )
    if focus(pid):
        return None
    holder = foreground_pid()
    return fail(
        f"could not verify focus: the foreground window belongs to pid {holder}, not to the "
        f"client (pid {pid})",
        hint="Windows refuses to hand the foreground to a background process, and typing "
             "anyway would send the keystrokes into whatever window is in front. Click the "
             "client window (or leave the machine to this session) and retry. Movement, "
             "menus and the screenshot do NOT need focus -- only typing does",
    )


def _force_en_layout(hwnd: int) -> None:
    """Ask the window for the US layout before typing.

    The client starts on the machine owner's Ukrainian layout, where a
    scancode for a letter produces Cyrillic. Asked EVERY time, because the
    game can switch back. Taken from the owner's working scripts; not
    re-measured here, and harmless when it has no effect -- this is a window
    message the OS handles, not something the game's script layer reads.
    """
    user32.PostMessageW(hwnd, WM_INPUTLANGCHANGEREQUEST, 0, LANG_EN_US)


def type_text(pid: int, text: str) -> Result:
    """Type `text` into the client. Requires -- and verifies -- the foreground.

    Text is the only thing the virtual gamepad cannot do (DayZ has no on-screen
    keyboard), which makes this the one path that needs the window in front.

    Characters with no scancode are refused BEFORE anything is typed, naming
    them. Dropping them silently is how a chat command arrives mangled and the
    run fails as if the mod were at fault -- an underscore that arrived as a
    hyphen cost exactly that here once.
    """
    missing = unsupported_characters(text)
    if missing:
        return fail(
            "cannot type " + ", ".join(repr(c) for c in missing)
            + ": no scancode for these characters",
            hint="this types US-layout scancodes because the client starts on another "
                 "layout; send the command in ASCII, or set the value through the bridge "
                 "instead of through the chat line",
        )
    refusal = _require_focus(pid)
    if refusal:
        return refusal
    hwnd = find_window(pid)
    _force_en_layout(hwnd)
    for char in text:
        if char in SHIFTED:
            _tap(SHIFTED[char], shift=True)
            continue
        lower = char.lower()
        _tap(SCANCODES[lower], shift=char.isupper())
    return ok({"typed": text, "characters": len(text)})


def press_key(pid: int, name: str, hold_ms: int = 0) -> Result:
    """Press one named key in the client. Requires -- and verifies -- the foreground.

    `hold_ms` holds the key down for that long before releasing it. A tap is
    not the same event as a hold: a mod that polls the key state once a frame
    can miss a press and release that happen inside one frame, and push-to-talk
    is exactly that kind of mod.
    """
    key = name.strip().lower()
    if key not in NAMED_KEYS:
        return fail(
            f"unknown key {name!r}",
            hint="known keys: " + ", ".join(sorted(NAMED_KEYS)),
        )
    if hold_ms < 0 or hold_ms > 10_000:
        return fail(
            f"hold_ms {hold_ms} is outside 0..10000",
            hint="a hold longer than ten seconds is almost certainly a mistake",
        )
    refusal = _require_focus(pid)
    if refusal:
        return refusal

    scan, extended = NAMED_KEYS[key]
    if hold_ms <= 0:
        _tap(scan, extended=extended)
        return ok({"key": key, "held_ms": 0})

    _key_event(scan, up=False, extended=extended)
    time.sleep(hold_ms / 1000.0)
    _key_event(scan, up=True, extended=extended)
    return ok({"key": key, "held_ms": hold_ms})


def click(pid: int, x: int, y: int) -> Result:
    """Click at a CLIENT-AREA coordinate. Requires -- and verifies -- the foreground.

    Honest about its standing: the mouse is not one of the tracks this phase
    measured. Menus and the inventory were driven by the virtual gamepad, from
    the background, and that is the way to reach them. This exists because a
    widget at a coordinate has no gamepad equivalent, and it carries the same
    focus guard as typing.

    The coordinate is measured from the client area, so it stays the same
    wherever the window has been dragged to.
    """
    refusal = _require_focus(pid)
    if refusal:
        return refusal
    hwnd = find_window(pid)
    width, height = client_size(hwnd)
    if not (0 <= x < width and 0 <= y < height):
        return fail(
            f"({x}, {y}) is outside the client area, which is {width}x{height}",
            hint="coordinates are measured from the top-left of the CLIENT area, not the "
                 "screen and not the window frame",
        )
    sx, sy = client_to_screen(hwnd, x, y)
    ax, ay = virtual_to_absolute(
        sx, sy,
        user32.GetSystemMetrics(SM_XVIRTUALSCREEN),
        user32.GetSystemMetrics(SM_YVIRTUALSCREEN),
        user32.GetSystemMetrics(SM_CXVIRTUALSCREEN),
        user32.GetSystemMetrics(SM_CYVIRTUALSCREEN),
    )
    # The move goes as its own event, with a pause: the game reads the cursor
    # position on a later frame than the button, and events glued together are
    # how a click lands where the cursor used to be.
    _mouse_event(MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_MOVE, ax, ay)
    time.sleep(0.15)
    _mouse_event(MOUSEEVENTF_LEFTDOWN)
    time.sleep(0.06)
    _mouse_event(MOUSEEVENTF_LEFTUP)
    return ok({"client": [int(x), int(y)], "screen": [sx, sy]})
