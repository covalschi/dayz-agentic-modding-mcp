import re
import sys
from pathlib import Path
from dayz_mcp.procs import run_blocking, spawn, stop, is_alive, powershell_cmd


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

    # Extract the grandchild PID from the output
    log_text = log.read_text(encoding="utf-8")
    match = re.search(r"GRANDCHILD_PID=(\d+)", log_text)

    if match:
        grandchild_pid = int(match.group(1))
        # The grandchild should be dead (killed by the timeout's process tree termination)
        try:
            assert not is_alive(grandchild_pid), f"Grandchild process {grandchild_pid} still alive after timeout"
        finally:
            # Cleanup: ensure grandchild is truly dead in case assertion failed
            if is_alive(grandchild_pid):
                stop(grandchild_pid)
