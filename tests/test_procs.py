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
