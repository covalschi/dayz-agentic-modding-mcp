"""Process plumbing: the only module allowed to start and kill things."""
from __future__ import annotations

import csv
import os
import re
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
    cmd: list[str], cwd: Path, log_path: Path, timeout: float | None = None,
    env: dict[str, str] | None = None,
) -> tuple[int, str]:
    """Run one command to completion, waiting on the process HANDLE.

    `env` is an OVERLAY on this process's environment, never a replacement:
    handed to Popen bare it would strip PATH, SystemRoot and TEMP, and a
    Windows program started without those fails in ways that read like the
    program is broken. Nothing is written back into this process's own
    environment, because these runs happen on job threads and a global
    mutation would leak into whatever else is running at the time.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    child_env = {**os.environ, **env} if env else None
    with log_path.open("w", encoding="utf-8", errors="replace") as fh:
        try:
            proc = subprocess.Popen(  # noqa: S603 - command is assembled by us
                cmd, cwd=str(cwd), stdout=fh, stderr=subprocess.STDOUT, env=child_env,
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


def udp_port_holders(port: int) -> list[int]:
    """Pids holding UDP `port`, as reported by netstat. Empty when nothing does.

    A readiness signal that depends on neither our tracked pid nor the mod: a
    DayZ server binds its game port, so something holding it is something
    listening. `expect.ready_line` answers a different question (has the MOD
    finished loading) and is not available at all to a project that declares
    none.

    Read out of netstat rather than by binding the port ourselves. Binding is
    the technique that first suggests itself, and it is the one that can break
    the very thing it measures: a probe that holds the port for even a moment
    while the server is trying to bind it makes the server fail to start.
    Reading a table cannot do that.

    Returns [] on any failure (netstat missing, unparseable, non-Windows) --
    this is evidence FOR liveness when it finds something, never evidence
    against when it finds nothing, and every caller is written that way.
    """
    if os.name != "nt" or port <= 0:
        return []
    try:
        out = subprocess.run(  # noqa: S603 - fixed command, no user input
            ["netstat", "-ano", "-p", "UDP"], capture_output=True, text=True,
            check=False, timeout=15,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    holders: list[int] = []
    for line in out.splitlines():
        parts = line.split()
        # "UDP  0.0.0.0:2302  *:*  <pid>" -- the local address is the second
        # column and the pid the last. Matched on the ":port" suffix so an
        # address of 0.0.0.0, 127.0.0.1 or a specific interface all count,
        # while a port that merely CONTAINS these digits (12302) does not.
        if len(parts) < 4 or parts[0].upper() != "UDP":
            continue
        if not parts[1].endswith(f":{port}"):
            continue
        try:
            holders.append(int(parts[-1]))
        except ValueError:
            continue
    return sorted(set(holders))


def process_mods_tail(pid: int) -> str:
    """The @Name basenames of `pid`'s -mod= argument, ";"-joined, or "".

    The one cheap field that tells two DayZ stands apart on one machine: this
    project learned that the hard way, when a neighbouring stand's server on
    the same port and the same -profiles directory was mistaken for an engine
    relaunch of ours -- every field of its command line matched except -mod=.
    Used to IDENTIFY a process to a human deciding whether to stop it, so it
    must never guess: evidence when present, "" on any failure (dead pid,
    access denied, no -mod= argument, no PowerShell), and never an exception
    on the refusal path that calls it.

    Basenames only. The full paths are noise for recognition; the @Name
    segments are what a stand is known by.
    """
    if os.name != "nt" or pid <= 0:
        return ""
    exe = shutil.which("pwsh") or "powershell.exe"
    try:
        out = subprocess.run(  # noqa: S603 - pid is an int, no user input
            [exe, "-NoProfile", "-Command",
             f"(Get-CimInstance Win32_Process -Filter 'ProcessId = {int(pid)}').CommandLine"],
            capture_output=True, text=True, check=False, timeout=15,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""
    if not out:
        return ""
    matched = re.search(r'-mod=("([^"]*)"|\S+)', out)
    if not matched:
        return ""
    value = matched.group(2) if matched.group(2) is not None else matched.group(1)
    names = [seg.rstrip("\\/").replace("/", "\\").rsplit("\\", 1)[-1]
             for seg in value.split(";") if seg.strip()]
    return ";".join(n for n in names if n)


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
        # /FO CSV, not the default table format: the default truncates the
        # Image Name column at 25 characters (confirmed against real
        # tasklist output on this machine with a 51-character executable
        # name), so a substring match against it would falsely report a
        # genuinely running long-named process as dead. CSV rows also give
        # each field its own column instead of a fixed-width one, so
        # matching by field is exact rather than "somewhere in the line".
        out = subprocess.run(  # noqa: S603
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, check=False,
        ).stdout
        rows = [row for row in csv.reader(out.splitlines()) if len(row) >= 2]
        matched = [row for row in rows if row[1] == str(pid)]
        if not matched:
            return False
        if image and not any(row[0].lower() == image.lower() for row in matched):
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
