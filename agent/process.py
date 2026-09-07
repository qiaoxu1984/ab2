"""Operating-system process helpers for protecting Unity project builds."""

import os
import signal
import subprocess
import time
from pathlib import Path


def _unity_pids(project_path: str) -> list[int]:
    """Find Unity processes whose command line references the selected project."""
    # Keep Windows paths in their native form because Unity command lines use backslashes.
    normalized = str(Path(project_path).resolve()).lower()
    if os.name == "nt":
        # Match both Unity and Tuanjie editor processes because either can own the project lock.
        # Pass the trusted normalized path in the script instead of through a second shell environment.
        escaped_path = normalized.replace("'", "''")
        script = f"$p='{escaped_path}'; Get-CimInstance Win32_Process | Where-Object {{ $_.Name -match '^(Unity|Tuanjie)\\.exe$' -and $_.CommandLine -and $_.CommandLine.ToLower().Contains($p) }} | ForEach-Object {{ $_.ProcessId }}"
        # Use the shell name so Python resolves the same system executable as the terminal.
        executable = "powershell.exe"
        if not executable:
            return []
        # Invoke PowerShell directly so cmd.exe cannot reinterpret the project path or script arguments.
        result = subprocess.run([executable, "-NoProfile", "-Command", script], capture_output=True, text=True, encoding="utf-8", errors="replace")
    else:
        result = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True, encoding="utf-8", errors="replace")
    pids = []
    for line in result.stdout.splitlines():
        if os.name == "nt":
            try:
                pids.append(int(line.strip()))
            except ValueError:
                continue
        elif ("unity" in line.lower() or "tuanjie" in line.lower()) and normalized in line.lower().replace("/", "\\"):
            try:
                pids.append(int(line.strip().split(None, 1)[0]))
            except (ValueError, IndexError):
                continue
    return sorted(set(pid for pid in pids if pid != os.getpid()))


def _is_running(pid: int) -> bool:
    """Check whether a process still exists on the current operating system."""
    if os.name == "nt":
        result = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True, encoding="utf-8", errors="replace")
        return str(pid) in result.stdout
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def close_unity_for_project(project_path: str, wait_seconds: float = 8.0) -> list[int]:
    """Gracefully close associated Unity editors and force-close only after timeout."""
    pids = _unity_pids(project_path)
    if not pids:
        return []
    for pid in pids:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T"], capture_output=True, text=True)
        else:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
    deadline = time.time() + wait_seconds
    remaining = list(pids)
    while remaining and time.time() < deadline:
        time.sleep(0.25)
        remaining = [pid for pid in remaining if _is_running(pid)]
    for pid in remaining:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/PID", str(pid), "/T"], capture_output=True, text=True)
        else:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    return pids
