"""Local-only web control panel for starting the AB2 Manager and Agent."""

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


class ServiceController:
    """Own the two approved AB2 child processes and their output files."""

    def __init__(self, root: Path, ports: dict[str, int] | None = None):
        """Initialize process state without executing anything automatically."""
        self.root = root
        self.log_dir = root / "data" / "service-logs"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.processes: dict[str, subprocess.Popen[Any]] = {}
        self.started_at: dict[str, float] = {}
        self.ports = {"manager": 8000, "agent": 8020}
        if ports:
            self.ports.update(ports)
        self.lock = threading.RLock()

    def status(self) -> dict[str, Any]:
        """Return current liveness and process identifiers for both services."""
        with self.lock:
            result = {}
            for name in ("manager", "agent"):
                process = self.processes.get(name)
                running = process is not None and process.poll() is None
                pid = process.pid if running else self._service_pid(name)
                running = running or pid is not None
                result[name] = {"running": running, "pid": pid, "started_at": self.started_at.get(name)}
            return result

    def start(self, name: str, options: dict[str, Any]) -> dict[str, Any]:
        """Start one allow-listed service with arguments supplied as data, not shell text."""
        if name not in ("manager", "agent"):
            raise ValueError("unknown service")
        with self.lock:
            if name == "manager":
                self.ports["manager"] = int(options.get("port", self.ports["manager"]))
            else:
                self.ports["agent"] = int(options.get("web_port", self.ports["agent"]))
            current = self.processes.get(name)
            if current and current.poll() is None:
                return {"ok": True, "message": f"{name} already running", "pid": current.pid}
            existing = self._service_pid(name)
            if existing:
                return {"ok": True, "message": f"{name} already running", "pid": existing}
            if name == "manager":
                command = [sys.executable, "-m", "uvicorn", "manager.main:app", "--host", "0.0.0.0", "--port", str(self.ports["manager"])]
            else:
                command = [sys.executable, "-m", "agent.service", "--manager", str(options.get("manager", "ws://172.18.67.71:8000/ws/agent")), "--config", str(options.get("config", "data/agent.json")), "--web-port", str(self.ports["agent"])]
            log_path = self.log_dir / f"{name}.log"
            log_file = log_path.open("a", encoding="utf-8")
            try:
                process = subprocess.Popen(command, cwd=self.root, stdout=log_file, stderr=subprocess.STDOUT, creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
            finally:
                log_file.close()
            deadline = time.time() + 1.5
            while time.time() < deadline:
                code = process.poll()
                if code is not None:
                    return {"ok": False, "error": self._last_log_error(name) or f"{name} exited with code {code}"}
                time.sleep(0.05)
            self.processes[name] = process
            self.started_at[name] = time.time()
            return {"ok": True, "pid": process.pid}

    def stop(self, name: str) -> dict[str, Any]:
        """Gracefully stop one managed process and fall back to kill after a short wait."""
        with self.lock:
            process = self.processes.get(name)
            if process and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                return {"ok": True}
            pid = self._service_pid(name)
            if not pid:
                return {"ok": True, "message": f"{name} is not running"}
            self._stop_pid(pid)
            return {"ok": True}

    def _service_pid(self, name: str) -> int | None:
        """Find a live Manager or Agent that this control page did not spawn."""
        pid = self._listening_pid(self.ports.get(name, 0))
        if pid is None:
            return None
        marker = "agent.service" if name == "agent" else "manager.main:app"
        return pid if marker in self._command_line(pid) else None

    def _listening_pid(self, port: int) -> int | None:
        """Return the PID listening on a TCP port, if any."""
        if not port:
            return None
        if os.name == "nt":
            result = subprocess.run(["netstat", "-ano", "-p", "tcp"], capture_output=True, text=True, encoding="utf-8", errors="replace")
            for line in result.stdout.splitlines():
                if "LISTENING" not in line.upper():
                    continue
                parts = line.split()
                if len(parts) < 5 or parts[0].upper() != "TCP":
                    continue
                listen_port = parts[1].rsplit(":", 1)[-1].rstrip("]")
                if listen_port == str(port):
                    return int(parts[-1])
            return None
        result = subprocess.run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"], capture_output=True, text=True, encoding="utf-8", errors="replace")
        for line in result.stdout.splitlines():
            if line.strip().isdigit():
                return int(line.strip())
        return None

    def _command_line(self, pid: int) -> str:
        """Read the command line used to launch one process."""
        if os.name == "nt":
            result = subprocess.run(["wmic", "process", "where", f"ProcessId={pid}", "get", "CommandLine"], capture_output=True, text=True, encoding="utf-8", errors="replace")
            lines = [line.strip() for line in result.stdout.splitlines() if line.strip() and "CommandLine" not in line]
            return lines[0] if lines else ""
        result = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True, encoding="utf-8", errors="replace")
        return result.stdout.strip()

    def _stop_pid(self, pid: int) -> None:
        """Stop a discovered service process that is not a child of this controller."""
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, text=True, encoding="utf-8", errors="replace")
            return
        os.kill(pid, signal.SIGTERM)
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                os.kill(pid, 0)
            except OSError:
                return
            time.sleep(0.1)
        os.kill(pid, signal.SIGKILL)

    def _last_log_error(self, name: str) -> str:
        """Return the most recent log line after a service crashes on start."""
        path = self.log_dir / f"{name}.log"
        if not path.is_file():
            return ""
        lines = [line.strip() for line in path.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip()]
        return lines[-1] if lines else ""


class ControlHandler(BaseHTTPRequestHandler):
    """Expose a minimal JSON API and static control page on localhost."""

    controller: ServiceController

    def _json(self, payload: dict[str, Any], status: int = 200) -> None:
        """Write a JSON response with explicit content length."""
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:
        """Allow the local HTML file to call the localhost JSON API."""
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()

    def do_GET(self) -> None:
        """Serve the dashboard or the current process status."""
        if urlparse(self.path).path == "/api/status":
            self._json(self.controller.status())
            return
        page = (Path(__file__).parent / "control.html").read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.end_headers()
        self.wfile.write(page)

    def do_POST(self) -> None:
        """Start or stop only the two known AB2 services."""
        length = int(self.headers.get("Content-Length", "0"))
        data = json.loads(self.rfile.read(length) or b"{}")
        parts = urlparse(self.path).path.strip("/").split("/")
        if len(parts) == 4 and parts[:2] == ["api", "services"]:
            try:
                result = self.controller.start(parts[2], data) if parts[3] == "start" else self.controller.stop(parts[2])
                self._json(result, 400 if result.get("ok") is False else 200)
            except (ValueError, OSError) as error:
                self._json({"ok": False, "error": str(error)}, 400)
            return
        self._json({"ok": False, "error": "not found"}, 404)

    def log_message(self, format_string: str, *args: Any) -> None:
        """Keep request logs concise in the control service console."""
        print(format_string % args)


def main() -> None:
    """Start the localhost-only control server."""
    parser = argparse.ArgumentParser(description="AB2 local service control panel")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8010)
    args = parser.parse_args()
    ControlHandler.controller = ServiceController(Path(__file__).parent.resolve())
    server = ThreadingHTTPServer((args.host, args.port), ControlHandler)
    print(f"AB2 control panel: http://{args.host}:{args.port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
