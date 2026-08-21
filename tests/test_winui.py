"""Tests for the window layer: find, focus (verified), capture, client-area coordinates.

None of these need a running game. The Windows-path tests use a real top-level
window this process creates and destroys itself -- that is what makes "the
coordinates come from the client area, not the window frame" provable rather
than merely asserted, and it is also the only honest way to exercise the
minimized-window refusal.
"""
import ctypes
import itertools
import os
import zlib

import pytest

from dayz_mcp import winui

WINDOWS_ONLY = pytest.mark.skipif(os.name != "nt", reason="Windows window APIs")

# A pid no process on this machine can hold (Windows pids stay far below this),
# used to prove the "no window" paths answer with an envelope instead of raising.
DEAD_PID = 4_000_000_000

_CLASS_SERIAL = itertools.count()


def _bgra(*pixels: tuple[int, int, int]) -> bytes:
    """Pack (b, g, r) triples into the BGRA buffer shape GetDIBits produces."""
    return b"".join(bytes((b, g, r, 0)) for b, g, r in pixels)


# --------------------------------------------------------------------------
# the non-black measure -- the whole point of it is that a black capture is
# the failure mode that otherwise reports success
# --------------------------------------------------------------------------


def test_lit_fraction_is_zero_for_a_black_frame():
    black = _bgra((0, 0, 0), (0, 0, 0), (0, 0, 0), (0, 0, 0))
    assert winui.lit_fraction(black, stride_pixels=1) == 0.0


def test_lit_fraction_is_one_for_a_fully_lit_frame():
    lit = _bgra((200, 200, 200), (10, 90, 30), (255, 0, 0), (0, 0, 255))
    assert winui.lit_fraction(lit, stride_pixels=1) == 1.0


def test_lit_fraction_counts_only_pixels_above_the_near_black_threshold():
    """A JPEG-ish dark grey is not content. The threshold is what separates a
    genuinely dark night scene from a frame that never rendered."""
    frame = _bgra((8, 8, 8), (9, 0, 0), (0, 0, 8), (0, 9, 0))
    assert winui.lit_fraction(frame, stride_pixels=1, threshold=8) == 0.5


def test_lit_fraction_of_an_empty_buffer_is_zero_not_a_crash():
    assert winui.lit_fraction(b"", stride_pixels=1) == 0.0


def test_lit_fraction_samples_sparsely_when_asked():
    """Sampling every pixel of a 3840x1600 frame in Python is seconds of work
    for a number that only has to say "black or not"."""
    frame = _bgra((255, 255, 255), (0, 0, 0), (0, 0, 0), (0, 0, 0))
    assert winui.lit_fraction(frame, stride_pixels=4) == 1.0


# --------------------------------------------------------------------------
# PNG encoding -- stdlib only, so a screenshot never depends on an optional
# imaging package being installed on the machine running the server
# --------------------------------------------------------------------------


def _chunks(png: bytes):
    assert png[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG signature"
    pos = 8
    while pos < len(png):
        length = int.from_bytes(png[pos:pos + 4], "big")
        kind = png[pos + 4:pos + 8]
        payload = png[pos + 8:pos + 8 + length]
        crc = int.from_bytes(png[pos + 8 + length:pos + 12 + length], "big")
        assert crc == zlib.crc32(kind + payload) & 0xFFFFFFFF, f"bad CRC on {kind!r}"
        yield kind, payload
        pos += 12 + length


def test_png_bytes_writes_a_header_a_decoder_would_accept():
    png = winui.png_bytes(_bgra((1, 2, 3), (4, 5, 6)), 2, 1)
    kinds = []
    header = None
    for kind, payload in _chunks(png):
        kinds.append(kind)
        if kind == b"IHDR":
            header = payload
    assert kinds[0] == b"IHDR" and kinds[-1] == b"IEND"
    width = int.from_bytes(header[0:4], "big")
    height = int.from_bytes(header[4:8], "big")
    assert (width, height) == (2, 1)
    assert header[8] == 8, "bit depth"
    assert header[9] == 6, "colour type RGBA"
    assert header[10:13] == b"\x00\x00\x00", "deflate, standard filters, no interlace"


def test_png_bytes_swaps_bgra_into_rgba_and_forces_alpha_opaque():
    """PrintWindow leaves the alpha channel at zero, so a straight copy of the
    DIB produces a fully transparent -- i.e. invisible -- image."""
    png = winui.png_bytes(_bgra((0x10, 0x20, 0x30), (0x00, 0x00, 0xFF)), 2, 1)
    idat = b"".join(payload for kind, payload in _chunks(png) if kind == b"IDAT")
    raw = zlib.decompress(idat)
    assert raw[0] == 0, "scanline filter must be None"
    assert raw[1:] == bytes((0x30, 0x20, 0x10, 0xFF, 0xFF, 0x00, 0x00, 0xFF))


def test_png_bytes_keeps_rows_in_top_down_order():
    two_rows = _bgra((0, 0, 0), (0, 0, 0), (0, 0, 255), (0, 0, 255))
    png = winui.png_bytes(two_rows, 2, 2)
    idat = b"".join(payload for kind, payload in _chunks(png) if kind == b"IDAT")
    raw = zlib.decompress(idat)
    stride = 1 + 2 * 4
    assert raw[0:stride] == b"\x00" + bytes((0, 0, 0, 255)) * 2
    assert raw[stride:2 * stride] == b"\x00" + bytes((255, 0, 0, 255)) * 2


def test_png_bytes_refuses_a_buffer_that_does_not_match_the_size():
    """A short buffer would encode as a torn image with no error anywhere --
    the same class of silent success this module exists to prevent."""
    with pytest.raises(ValueError):
        winui.png_bytes(_bgra((1, 2, 3)), 4, 4)


def test_the_png_is_readable_by_a_real_decoder():
    """Structural assertions above prove the bytes are shaped like a PNG. This
    proves an actual decoder agrees, which is what the caller will do."""
    image = pytest.importorskip("PIL.Image")
    png = winui.png_bytes(_bgra((0x10, 0x20, 0x30), (0x00, 0x00, 0xFF)), 2, 1)
    import io

    img = image.open(io.BytesIO(png))
    assert img.size == (2, 1)
    assert img.convert("RGBA").getpixel((0, 0)) == (0x30, 0x20, 0x10, 255)
    assert img.convert("RGBA").getpixel((1, 0)) == (0xFF, 0x00, 0x00, 255)


# --------------------------------------------------------------------------
# coordinates
# --------------------------------------------------------------------------


def test_virtual_to_absolute_maps_the_corners_of_the_virtual_desktop():
    assert winui.virtual_to_absolute(0, 0, 0, 0, 1920, 1080) == (0, 0)
    assert winui.virtual_to_absolute(1919, 1079, 0, 0, 1920, 1080) == (65535, 65535)


def test_virtual_to_absolute_handles_a_monitor_left_of_the_primary():
    """SendInput's absolute coordinates are relative to the virtual desktop's
    ORIGIN, which is negative when a second monitor sits left of the primary --
    ignoring it puts every click on the wrong screen."""
    x, y = winui.virtual_to_absolute(-1920, 0, -1920, 0, 3840, 1080)
    assert (x, y) == (0, 0)
    assert winui.virtual_to_absolute(0, 0, -1920, 0, 3840, 1080)[0] == pytest.approx(32768, abs=20)


def test_virtual_to_absolute_clamps_a_point_outside_the_desktop():
    assert winui.virtual_to_absolute(9999, 9999, 0, 0, 1920, 1080) == (65535, 65535)
    assert winui.virtual_to_absolute(-50, -50, 0, 0, 1920, 1080) == (0, 0)


# --------------------------------------------------------------------------
# pauseMode -- read, never rewritten: it is the machine owner's setting
# --------------------------------------------------------------------------


def _settings(text: str = "pauseMode=2;\n") -> str:
    return "version=1;\nblood=1;\n" + text + "shadowQuality=2;\n"


def test_parse_pause_mode_reads_the_value():
    assert winui.parse_pause_mode(_settings()) == 2
    assert winui.parse_pause_mode(_settings("pauseMode=0;\n")) == 0


def test_parse_pause_mode_returns_none_when_the_client_never_wrote_it():
    assert winui.parse_pause_mode("version=1;\nblood=1;\n") is None


def test_parse_pause_mode_does_not_match_a_different_key_ending_in_the_name():
    assert winui.parse_pause_mode("myPauseModeExtra=7;\n") is None


def _write_profile(root, user, text, mtime):
    user_dir = root / "Users" / user
    user_dir.mkdir(parents=True, exist_ok=True)
    settings = user_dir / "someuser_settings.DayZProfile"
    settings.write_text(text, encoding="utf-8")
    os.utime(settings, (mtime, mtime))
    return settings


def test_the_newest_settings_file_wins_not_the_newest_directory(tmp_path):
    """Measured on this machine: the newest Users/* directory held only
    DayZ.cfg (the window size) and no settings file at all, while the live
    settings lived in an older directory. Picking the newest DIRECTORY reads
    the wrong client, or nothing."""
    _write_profile(tmp_path, "older", _settings("pauseMode=1;\n"), mtime=1_000_000)
    live = _write_profile(tmp_path, "live", _settings("pauseMode=2;\n"), mtime=2_000_000)
    empty_but_newest = tmp_path / "Users" / "newestdir"
    empty_but_newest.mkdir(parents=True)
    (empty_but_newest / "DayZ.cfg").write_text("resolution=1600x900;\n", encoding="utf-8")
    os.utime(empty_but_newest, (3_000_000, 3_000_000))

    assert winui.newest_settings_file(tmp_path) == live


def test_read_pause_mode_reports_the_measured_background_capable_value(tmp_path):
    _write_profile(tmp_path, "live", _settings("pauseMode=2;\n"), mtime=2_000_000)
    got = winui.read_pause_mode(tmp_path)
    assert got.ok
    assert got.data["pause_mode"] == 2
    assert got.data["background_verified"] is True
    assert "someuser_settings.DayZProfile" in got.data["settings_file"]


def test_read_pause_mode_flags_a_value_nobody_measured(tmp_path):
    """Only 2 was measured as the value under which the background frame and
    background gamepad work. Anything else is reported as unverified rather
    than guessed at -- the number-to-menu-item mapping was never measured."""
    _write_profile(tmp_path, "live", _settings("pauseMode=1;\n"), mtime=2_000_000)
    got = winui.read_pause_mode(tmp_path)
    assert got.ok
    assert got.data["pause_mode"] == 1
    assert got.data["background_verified"] is False
    assert got.data["note"]


def test_read_pause_mode_never_writes_to_the_owners_file(tmp_path):
    settings = _write_profile(tmp_path, "live", _settings(), mtime=2_000_000)
    before = settings.read_bytes(), settings.stat().st_mtime
    winui.read_pause_mode(tmp_path)
    assert (settings.read_bytes(), settings.stat().st_mtime) == before


def test_read_pause_mode_fails_with_a_hint_when_there_is_no_settings_file(tmp_path):
    got = winui.read_pause_mode(tmp_path)
    assert not got.ok
    assert got.hint


def test_read_pause_mode_reports_a_file_that_carries_no_such_key(tmp_path):
    """A settings file exists but the client never wrote the key -- the read
    succeeded and the value is absent, which is not the same as a failure."""
    _write_profile(tmp_path, "live", "version=1;\n", mtime=2_000_000)
    got = winui.read_pause_mode(tmp_path)
    assert got.ok
    assert got.data["pause_mode"] is None
    assert got.data["background_verified"] is False


# --------------------------------------------------------------------------
# typing -- the one layer that needs the foreground, so the one that must
# refuse rather than type blind
# --------------------------------------------------------------------------


def test_unsupported_characters_names_what_cannot_be_typed():
    assert winui.unsupported_characters("hello 1!") == []
    assert winui.unsupported_characters("привіт") == ["п", "р", "и", "в", "і", "т"]


def test_type_text_refuses_unspellable_text_before_it_touches_a_window():
    """Dropping characters silently is how a command arrives mangled and the
    test fails for a reason that has nothing to do with the mod. Checked
    BEFORE the focus attempt, so the refusal names the real problem."""
    got = winui.type_text(0, "привіт")
    assert not got.ok
    assert "п" in got.error
    assert got.hint


# --------------------------------------------------------------------------
# the Windows paths, against no window at all
# --------------------------------------------------------------------------


@WINDOWS_ONLY
def test_find_window_returns_none_for_a_pid_with_no_window():
    assert winui.find_window(0) is None
    assert winui.find_window(DEAD_PID) is None


@WINDOWS_ONLY
def test_shot_of_a_missing_window_returns_an_envelope_not_an_exception(tmp_path):
    got = winui.shot(DEAD_PID, tmp_path / "shot.png")
    assert not got.ok
    assert got.hint
    assert not (tmp_path / "shot.png").exists()


@WINDOWS_ONLY
def test_geometry_of_a_missing_window_returns_an_envelope(tmp_path):
    got = winui.geometry(DEAD_PID)
    assert not got.ok
    assert got.hint


@WINDOWS_ONLY
def test_focus_of_a_missing_window_is_false_not_an_exception():
    assert winui.focus(DEAD_PID) is False


@WINDOWS_ONLY
@pytest.mark.parametrize(
    "call",
    [
        lambda: winui.type_text(DEAD_PID, "hello"),
        lambda: winui.press_key(DEAD_PID, "enter"),
        lambda: winui.click(DEAD_PID, 10, 10),
    ],
)
def test_input_refuses_when_focus_cannot_be_verified(call):
    """Windows legitimately refuses to hand the foreground to a background
    process. Typing anyway sends the keystrokes into whatever window the owner
    is using -- which is the accident that got input automation banned once."""
    got = call()
    assert not got.ok
    assert "focus" in got.error.lower() or "window" in got.error.lower()
    assert got.hint


# --------------------------------------------------------------------------
# the Windows paths, against a real window this process owns
# --------------------------------------------------------------------------


def _wait_until_painted(hwnd, deadline_seconds=5.0):
    """Block until the window's client area has actually been composed.

    This process runs no message loop, so a freshly shown window is not painted
    the instant ShowWindow returns: measured on this machine, the client area
    starts capturing as its own background colour 60-75 ms later, whether or not
    UpdateWindow is called (it is DWM composition, not a pending WM_PAINT).
    Without this wait the capture tests pass or fail by timing.

    Deliberately probed with PW_RENDERFULLCONTENT ALONE, over the whole window
    frame, and checked at the client area's offset inside it -- i.e. through
    neither the flags nor the rectangle the tests are about. An implementation
    that captured the wrong rectangle would then still fail its assertion
    instead of hanging this fixture until it errored out.
    """
    import time

    left, top, right, bottom = winui.window_rect(hwnd)
    frame_width, frame_height = right - left, bottom - top
    origin_x, origin_y = winui.client_origin(hwnd)
    offset_x, offset_y = origin_x - left, origin_y - top
    end = time.time() + deadline_seconds
    while time.time() < end:
        pixels, _printed, _lines = winui._capture(
            hwnd, frame_width, frame_height, flags=0x2  # PW_RENDERFULLCONTENT
        )
        at = ((offset_y + 4) * frame_width + offset_x + 4) * 4
        if tuple(pixels[at:at + 4]) == (0, 0, 255, 255):  # BGRA: the red brush
            return
        time.sleep(0.02)
    raise AssertionError("the test window never painted its client area")


@pytest.fixture
def own_window():
    """A real top-level window belonging to this process.

    Shown with SW_SHOWNOACTIVATE so the test never steals the foreground from
    whoever is at the machine, and destroyed on the way out.
    """
    if os.name != "nt":
        pytest.skip("Windows window APIs")
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    WNDPROC = ctypes.WINFUNCTYPE(
        ctypes.c_longlong, wintypes.HWND, ctypes.c_uint, ctypes.c_size_t, ctypes.c_longlong
    )

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [
            ("style", ctypes.c_uint),
            ("lpfnWndProc", WNDPROC),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", wintypes.HINSTANCE),
            ("hIcon", wintypes.HICON),
            ("hCursor", wintypes.HANDLE),
            ("hbrBackground", wintypes.HBRUSH),
            ("lpszMenuName", wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR),
        ]

    kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    user32.DefWindowProcW.restype = ctypes.c_longlong
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
    ]
    user32.DestroyWindow.argtypes = [wintypes.HWND]
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]

    hinstance = kernel32.GetModuleHandleW(None)
    proc = ctypes.cast(user32.DefWindowProcW, WNDPROC)
    cls = WNDCLASSW()
    cls.lpfnWndProc = proc
    cls.hInstance = hinstance
    # Unique per fixture use: a class left registered by a failing teardown
    # makes every later test fail with ERROR_CLASS_ALREADY_EXISTS, which hides
    # whatever the real failure was.
    cls.lpszClassName = f"dayz_mcp_winui_test_{os.getpid()}_{next(_CLASS_SERIAL)}"
    # A pure red client area, so a capture can be checked against a colour
    # nothing else on the desktop has: the window frame is not red, and an
    # unpainted bitmap is black.
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    gdi32.CreateSolidBrush.restype = wintypes.HBRUSH
    gdi32.CreateSolidBrush.argtypes = [wintypes.DWORD]
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    brush = gdi32.CreateSolidBrush(0x0000FF)  # COLORREF is 0x00BBGGRR
    cls.hbrBackground = brush
    atom = user32.RegisterClassW(ctypes.byref(cls))
    assert atom, ctypes.get_last_error()

    hwnd = user32.CreateWindowExW(
        0, cls.lpszClassName, "dayz-mcp winui test", 0x00CF0000,  # WS_OVERLAPPEDWINDOW
        120, 120, 320, 240, None, None, hinstance, None,
    )
    assert hwnd, ctypes.get_last_error()
    user32.ShowWindow(hwnd, 4)  # SW_SHOWNOACTIVATE
    try:
        _wait_until_painted(hwnd)
        yield hwnd
    finally:
        user32.DestroyWindow(hwnd)
        user32.UnregisterClassW(cls.lpszClassName, hinstance)
        gdi32.DeleteObject(brush)


@WINDOWS_ONLY
def test_find_window_finds_this_processs_own_window(own_window):
    assert winui.find_window(os.getpid()) == own_window


@WINDOWS_ONLY
def test_geometry_measures_the_client_area_not_the_window_frame(own_window):
    """The frame carries a title bar and borders. Clicking at a widget's
    coordinate through frame-relative numbers lands high and left of it, by
    however much the current theme's decoration happens to be."""
    got = winui.geometry(os.getpid())
    assert got.ok, got.error
    left, top, right, bottom = got.data["window_rect"]
    assert got.data["client_width"] < right - left
    assert got.data["client_height"] < bottom - top
    origin_x, origin_y = got.data["client_origin"]
    assert origin_x >= left and origin_y > top, "client origin sits inside the frame"
    assert got.data["minimized"] is False


@WINDOWS_ONLY
def test_client_to_screen_offsets_a_point_by_the_client_origin(own_window):
    origin = winui.client_origin(own_window)
    assert winui.client_to_screen(own_window, 17, 23) == (origin[0] + 17, origin[1] + 23)


def _pixel(png: bytes, x: int, y: int) -> tuple[int, int, int, int]:
    """One RGBA pixel out of a PNG this module wrote (filter 0, colour type 6).

    A decoder small enough to be obviously right, so the capture tests do not
    depend on an imaging package being installed."""
    header = next(payload for kind, payload in _chunks(png) if kind == b"IHDR")
    width = int.from_bytes(header[0:4], "big")
    raw = zlib.decompress(b"".join(p for kind, p in _chunks(png) if kind == b"IDAT"))
    stride = 1 + width * 4
    start = y * stride + 1 + x * 4
    return tuple(raw[start:start + 4])


@WINDOWS_ONLY
def test_shot_captures_the_client_area_and_reports_how_lit_it_is(tmp_path, own_window):
    out = tmp_path / "frame.png"
    got = winui.shot(os.getpid(), out)
    assert got.ok, got.error
    assert (got.data["width"], got.data["height"]) == winui.client_size(own_window)
    assert out.exists() and out.stat().st_size == got.data["bytes"]
    assert out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    # The client area of this window is entirely the red class brush, so a
    # capture that really holds the client area is entirely lit. A range check
    # would have passed on an all-black bitmap too.
    assert got.data["lit_fraction"] == 1.0
    assert "warning" not in got.data


@WINDOWS_ONLY
def test_shot_captures_the_client_area_not_the_window_frame(tmp_path, own_window):
    """PrintWindow draws the WHOLE window, frame included, into the top-left of
    the target bitmap. Reading client-sized bytes out of that puts the title bar
    in the picture and cuts the same number of rows off the bottom of the real
    content -- measured on the live client: 31 rows of title bar in, 39 rows of
    HUD (quickbar, stamina) out. The capture has to mean the same rectangle the
    click coordinates mean."""
    out = tmp_path / "frame.png"
    assert winui.shot(os.getpid(), out).ok
    png = out.read_bytes()
    width, height = winui.client_size(own_window)
    for point in ((0, 0), (width // 2, height // 2), (width - 1, height - 1)):
        assert _pixel(png, *point) == (255, 0, 0, 255), point


@WINDOWS_ONLY
def test_shot_refuses_a_minimized_window_instead_of_saving_an_empty_image(tmp_path, own_window):
    """Measured: a minimized window's client area collapses to 0x0. Without
    this refusal the caller gets a valid-looking file and no way to tell that
    the eyes were shut."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.ShowWindow(own_window, 6)  # SW_MINIMIZE
    try:
        got = winui.shot(os.getpid(), tmp_path / "frame.png")
        assert not got.ok
        assert "minimi" in got.error.lower()
        assert "restore" in got.hint.lower()
        assert not (tmp_path / "frame.png").exists()
    finally:
        user32.ShowWindow(own_window, 9)  # SW_RESTORE


@WINDOWS_ONLY
def test_focus_reports_the_verified_truth_not_the_api_return_code(own_window):
    """SetForegroundWindow returns success while doing nothing when Windows
    refuses the change. The only trustworthy answer is what
    GetForegroundWindow says afterwards -- and False is a normal answer."""
    result = winui.focus(os.getpid())
    assert result is (winui.foreground_pid() == os.getpid())
