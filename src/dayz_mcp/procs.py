"""Process plumbing: the only module allowed to start and kill things."""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

TAIL_CHARS = 4000


def powershell_cmd(script: Path, args: list[str] | None = None) -> list[str]:
    """PowerShell 7 if present, Windows PowerShell otherwise.

    -File rather than -Command: a script with a BOM and non-ASCII text is then read
    by the interpreter itself instead of being assembled on the command line.
    """
    exe = shutil.which("pwsh") or "powershell.exe"
    return [exe, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), *(args or [])]


def run_blocking(
    cmd: list[str], cwd: Path, log_path: Path, timeout: float | None = None
) -> tuple[int, str]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8", errors="replace") as fh:
        try:
            proc = subprocess.Popen(  # noqa: S603 - command is assembled by us
                cmd, cwd=str(cwd), stdout=fh, stderr=subprocess.STDOUT,
                # This server talks MCP over stdio: its own stdin is a live
                # JSON-RPC pipe. Without this, a child inherits that handle and
                # can read from it, stealing input meant for the server.
                stdin=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            )
            code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            stop(proc.pid)
            fh.write(f"\n[dayz-mcp] timeout after {timeout}s\n")
            code = 124
        except OSError as exc:
            fh.write(f"\n[dayz-mcp] cannot start: {exc}\n")
            code = 127
    text = log_path.read_text(encoding="utf-8", errors="replace")
    return code, text[-TAIL_CHARS:]


def spawn(cmd: list[str], cwd: Path) -> int:
    proc = subprocess.Popen(  # noqa: S603
        cmd, cwd=str(cwd), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        # See run_blocking: never let a spawned process (the test server, the
        # diagnostic client) inherit this server's own live stdin pipe.
        stdin=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )
    return proc.pid


def is_alive(pid: int, image: str = "") -> bool:
    """True if `pid` is a running process.

    Windows recycles process ids: a pid recorded for a server that has since
    died can be handed out to something completely unrelated, and a bare pid
    check would then report our server as up when it is not -- exactly the
    direction this project cannot afford to lie in (see `server_status`).
    When `image` is given (a caller that knows what it spawned, e.g. the
    exe name `server_start` recorded), the process must also carry that
    image name in `tasklist`, not just occupy the pid.
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        out = subprocess.run(  # noqa: S603
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True, text=True, check=False,
        ).stdout
        if str(pid) not in out:
            return False
        if image and image.lower() not in out.lower():
            return False
        return True
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def stop(pid: int, grace: float = 3.0) -> bool:
    if not is_alive(pid):
        return True
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"],  # noqa: S603
                       capture_output=True, check=False)
    else:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            return True
    deadline = time.time() + grace
    while time.time() < deadline:
        if not is_alive(pid):
            return True
        time.sleep(0.2)
    return not is_alive(pid)
