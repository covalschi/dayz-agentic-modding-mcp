import os
import re
import shutil
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from dayz_mcp.procs import run_blocking, spawn, stop, is_alive, powershell_cmd


@contextmanager
def _stdin_replaced_with_a_never_closing_pipe():
    """Point *this* process's stdin at a pipe nobody writes to or closes.

    A child that inherits this handle (i.e. is not given stdin=DEVNULL) blocks
    on readline() forever, because there is neither data nor EOF coming. This
    is what makes the stdin-leak tests below deterministic regardless of what
    this test process's own real stdin happens to be (already at EOF, a tty,
    redirected from the test runner, ...) -- without it, the hazard the fix
    guards against would not reliably reproduce under pytest.
    """
    read_fd, write_fd = os.pipe()
    saved = os.dup(0)
    os.dup2(read_fd, 0)
    os.close(read_fd)
    try:
        yield
    finally:
        os.dup2(saved, 0)
        os.close(saved)
        os.close(write_fd)


def test_run_blocking_captures_output_and_code(tmp_path):
    log = tmp_path / "out.log"
    code, tail = run_blocking(
        [sys.executable, "-c", "print('hello'); raise SystemExit(3)"], tmp_path, log
    )
    assert code == 3
    assert "hello" in log.read_text(encoding="utf-8")
    assert "hello" in tail


def test_run_blocking_times_out(tmp_path):
    log = tmp_path / "out.log"
    code, tail = run_blocking(
        [sys.executable, "-c", "import time; time.sleep(30)"], tmp_path, log, timeout=1
    )
    assert code != 0
    assert "timeout" in tail.lower()


def test_missing_executable_is_reported_not_raised(tmp_path):
    log = tmp_path / "out.log"
    code, tail = run_blocking(["definitely-not-a-real-binary-xyz"], tmp_path, log)
    assert code == 127
    assert "cannot start" in tail


def test_spawn_stop_and_liveness(tmp_path):
    pid = spawn([sys.executable, "-c", "import time; time.sleep(30)"], tmp_path)
    assert pid > 0
    assert is_alive(pid)
    assert stop(pid)
    assert not is_alive(pid)


def test_run_blocking_does_not_leak_stdin_to_child(tmp_path):
    """This server talks MCP over stdio: its own stdin is a live JSON-RPC pipe.
    A child process that inherits it can read from it, stealing input meant
    for the server. Without stdin=DEVNULL, a child that calls readline() blocks
    forever (nothing ever sends it a line or closes the handle) and this call
    times out, returning code 124 -- it does not return 0 quickly as asserted
    below."""
    log = tmp_path / "out.log"
    with _stdin_replaced_with_a_never_closing_pipe():
        code, tail = run_blocking(
            [sys.executable, "-c", "import sys; sys.stdin.readline(); print('past readline')"],
            tmp_path, log, timeout=5,
        )
    assert code == 0
    assert "past readline" in log.read_text(encoding="utf-8")


def test_spawn_does_not_leak_stdin_to_child(tmp_path):
    """Same hazard as run_blocking, for the fire-and-forget spawn() path used
    to start the test server and the diagnostic client. spawn() has no output
    channel to assert on directly, so the child proves it got past readline()
    by writing a marker file -- without stdin=DEVNULL, readline() blocks
    forever and the marker never appears."""
    marker = tmp_path / "past_readline.marker"
    with _stdin_replaced_with_a_never_closing_pipe():
        pid = spawn(
            [
                sys.executable, "-c",
                f"import sys, time; sys.stdin.readline(); "
                f"open(r'{marker}', 'w').close(); time.sleep(30)",
            ],
            tmp_path,
        )
        try:
            deadline = time.time() + 5
            while time.time() < deadline and not marker.exists():
                time.sleep(0.1)
            assert marker.exists(), "child never got past sys.stdin.readline() -- stdin was inherited"
        finally:
            stop(pid)


def test_is_alive_checks_image_name_not_just_pid(tmp_path):
    """Windows recycles pids. A recorded pid whose real process died can be
    handed to something unrelated, and a pid-only check would report the
    (wrong) process as our server. Passing the expected image name catches
    that: the running interpreter's own image matches, an unrelated one
    (chosen to obviously not be this process) does not -- for the exact same
    pid."""
    real_image = Path(sys.executable).name
    pid = spawn([sys.executable, "-c", "import time; time.sleep(30)"], tmp_path)
    try:
        assert is_alive(pid)  # backward compatible: no image means pid-only
        assert is_alive(pid, image=real_image)
        assert not is_alive(pid, image="definitely-not-this-image.exe")
    finally:
        stop(pid)


def test_is_alive_is_not_fooled_by_tasklists_25_char_image_truncation(tmp_path):
    """tasklist's default table format (what /NH alone produces) truncates
    the Image Name column at 25 characters -- confirmed against real
    tasklist output on this machine with a 51-character executable name. A
    substring match against that truncated output would falsely report a
    genuinely running long-named process as dead, and that is exactly the
    direction is_alive must never lie in (see server_status)."""
    long_name = "this_is_a_very_long_executable_name_for_testing.exe"
    assert len(long_name) > 25
    cmd_exe = shutil.which("cmd") or r"C:\Windows\System32\cmd.exe"
    long_exe = tmp_path / long_name
    shutil.copy(cmd_exe, long_exe)

    # A self-contained, long-lived child under that long name -- cmd.exe does
    # not depend on sibling files the way a copied python.exe would.
    pid = spawn([str(long_exe), "/c", "ping", "127.0.0.1", "-n", "20"], tmp_path)
    try:
        assert is_alive(pid)
        assert is_alive(pid, image=long_name)
        assert not is_alive(pid, image="definitely-not-this-image.exe")
    finally:
        stop(pid)


def test_powershell_command_uses_bypass_and_file():
    cmd = powershell_cmd(Path("C:/x/gen.ps1"), ["-Foo", "1"])
    exe_lower = cmd[0].lower()
    assert exe_lower.endswith("powershell.exe") or exe_lower.endswith("pwsh.exe") or exe_lower == "pwsh"
    assert "-File" in cmd
    assert cmd[-2:] == ["-Foo", "1"]


def test_run_blocking_kills_grandchild_on_timeout(tmp_path):
    """Verify that timeout kills the entire process tree, not just the direct child.

    Reproduces the scenario where a wrapper spawns a long-lived grandchild.
    Without proper tree killing, the grandchild would survive the timeout.
    """
    log = tmp_path / "out.log"
    # Create a wrapper script that spawns a grandchild and prints its PID
    wrapper_code = (
        "import sys, subprocess, time; "
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
        "print(f'GRANDCHILD_PID={p.pid}', flush=True); "
        "time.sleep(30)"
    )

    code, tail = run_blocking(
        [sys.executable, "-c", wrapper_code], tmp_path, log, timeout=1
    )

    # Should timeout, not succeed
    assert code == 124
    assert "timeout" in tail.lower()

    # Extract the grandchild PID from the output. This match must be an
    # assertion, not a condition: guarded by `if match:`, the whole point of
    # the test evaporates the moment the wrapper fails to print its pid --
    # nothing is verified and the test still passes green, which is the one
    # outcome a test guarding against orphaned game processes must not have.
    log_text = log.read_text(encoding="utf-8")
    match = re.search(r"GRANDCHILD_PID=(\d+)", log_text)
    assert match, f"the wrapper never reported a grandchild pid, so nothing was proved; log was: {log_text!r}"

    grandchild_pid = int(match.group(1))
    # The grandchild should be dead (killed by the timeout's process tree termination)
    try:
        assert not is_alive(grandchild_pid), f"Grandchild process {grandchild_pid} still alive after timeout"
    finally:
        # Cleanup: ensure grandchild is truly dead in case assertion failed
        if is_alive(grandchild_pid):
            stop(grandchild_pid)
