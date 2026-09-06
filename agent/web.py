"""Local Agent configuration page and JSON API."""

import json
import os
import platform
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from agent.config import AgentConfig


class AgentWebHandler(BaseHTTPRequestHandler):
    """Serve the Agent project editor without exposing arbitrary commands."""

    config: AgentConfig
    page: bytes

    def _write_json(self, data: dict[str, Any], status: int = 200) -> None:
        """Write a UTF-8 JSON response to the browser."""
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        """Return the editor page or the current local Agent configuration."""
        if self.path == "/api/config":
            self._write_json(self._enriched_config())
            return
        if self.path.startswith("/api/browse"):
            self._write_json(self._browse())
            return
        if self.path.startswith("/api/project-draft"):
            self._write_json(self._project_draft())
            return
        if self.path == "/pick":
            page = (Path(__file__).with_name("project_picker.html")).read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(self.page)))
        self.end_headers()
        self.wfile.write(self.page)

    def _enriched_config(self) -> dict[str, Any]:
        """Refresh platform metadata for saved projects before displaying the form."""
        result = json.loads(json.dumps(self.config.data))
        for project in result.get("projects", []):
            # Keep generated Unity logs in the project root for easy inspection.
            project["log_path"] = str(Path(project.get("path", "")) / "Log" / "AB2-build.log")
            project["unity_path"] = self._find_unity(project.get("unity_version", "")) or project.get("unity_path", "")
            project["engine"] = "团结引擎" if "tuanjie" in project.get("unity_path", "").lower() else "Unity"
            detected = self._detect_platforms(Path(project.get("path", "")))
            project["platforms"] = detected
            for channel in project.get("channels", []):
                if channel.get("platform") == "HarmonyOS" and channel.get("switch_to", "").startswith("harmony_"):
                    channel["switch_to"] = "openharmony_" + channel["switch_to"].split("_", 1)[1]
                    channel["name"] = channel["switch_to"]
                if channel.get("platform") not in detected:
                    switch_to = channel.get("switch_to", "")
                    prefix = switch_to.split("_", 1)[0] if "_" in switch_to else ""
                    channel["platform"] = {"android": "Android", "ios": "iOS", "harmony": "HarmonyOS"}.get(prefix, detected[0])
        return result

    def _query_value(self, key: str) -> str:
        """Read one safely decoded query value without introducing a URL dependency."""
        from urllib.parse import parse_qs, urlparse
        return parse_qs(urlparse(self.path).query).get(key, [""])[0]

    def _browse(self) -> dict[str, Any]:
        """List local directories and mark folders containing a Unity project."""
        requested = self._query_value("path") or ("C:\\" if os.name == "nt" else "/")
        current = Path(requested).expanduser().resolve()
        if not current.is_dir():
            return {"path": str(current), "parent": str(current.parent), "entries": [], "error": "directory does not exist"}
        entries = []
        try:
            for child in sorted(current.iterdir(), key=lambda item: item.name.lower()):
                if not child.is_dir() or child.name.startswith("."):
                    continue
                is_unity = (child / "ProjectSettings" / "ProjectVersion.txt").is_file()
                entries.append({"name": child.name, "path": str(child), "is_unity_project": is_unity})
        except OSError as error:
            return {"path": str(current), "parent": str(current.parent), "entries": [], "error": str(error)}
        return {"path": str(current), "parent": str(current.parent), "entries": entries}

    def _project_draft(self) -> dict[str, Any]:
        """Inspect a selected Unity project and create editable initial settings."""
        path = Path(self._query_value("path")).expanduser().resolve()
        version_file = path / "ProjectSettings" / "ProjectVersion.txt"
        if not version_file.is_file():
            return {"error": "selected directory is not a Unity project"}
        version = ""
        for line in version_file.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("m_EditorVersion:"):
                version = line.split(":", 1)[1].strip()
                break
        branch = "main"
        try:
            result = subprocess.run(["git", "branch", "--show-current"], cwd=path, capture_output=True, text=True, encoding="utf-8", errors="replace")
            if result.returncode == 0 and result.stdout.strip():
                branch = result.stdout.strip()
        except OSError:
            pass
        engine_path = self._find_unity(version)
        return {"id": path.name.lower().replace(" ", "-"), "name": path.name, "path": str(path), "unity_version": version, "engine": "团结引擎" if engine_path and "tuanjie" in engine_path.lower() else "Unity", "platforms": self._detect_platforms(path), "unity_path": engine_path, "log_path": str(path / "Log" / "AB2-build.log"), "default_branch": branch, "channels": []}

    def _detect_platforms(self, project_path: Path) -> list[str]:
        """Read project settings and package metadata to determine configured platforms."""
        cache_root = project_path / "Library" / "PlayerDataCache"
        cache_candidates = {"Android": cache_root / "Android", "iOS": cache_root / "iOS", "HarmonyOS": cache_root / "HarmonyOS"}
        existing = [(name, path.stat().st_mtime) for name, path in cache_candidates.items() if path.is_dir()]
        if existing:
            # The newest platform cache is Unity's most recently used build target.
            return [max(existing, key=lambda item: item[1])[0]]
        files = [project_path / "ProjectSettings" / "ProjectSettings.asset", project_path / "Packages" / "manifest.json"]
        content = "\n".join(file.read_text(encoding="utf-8", errors="replace").lower() for file in files if file.is_file())
        platforms = []
        if "android" in content or "androidbundleversioncode" in content:
            platforms.append("Android")
        if "iphone" in content or "ios" in content or "com.unity.mobile.ios" in content:
            platforms.append("iOS")
        if "harmony" in content or "openharmony" in content or "ohos" in content:
            platforms.append("HarmonyOS")
        return [platforms[0]] if platforms else ["Unknown"]

    def _find_unity(self, version: str) -> str:
        """Find Unity or Tuanjie with the selected version in common installation layouts."""
        candidates = []
        if platform.system() == "Windows":
            roots = [Path(os.environ.get("ProgramFiles", "C:/Program Files")), Path("C:/Program Files"), Path("D:/Program Files")]
            for root in roots:
                for product in ("Unity", "Tuanjie", "TuanJie"):
                    base = root / product / "Hub" / "Editor" / version
                    candidates.extend([base / "Editor" / "Unity.exe", base / "Editor" / "Tuanjie.exe", base / "Tuanjie.exe"])
        else:
            candidates.extend([Path("/Applications/Unity/Hub/Editor") / version / "Unity.app/Contents/MacOS/Unity", Path("/Applications/Tuanjie/Hub/Editor") / version / "Tuanjie.app/Contents/MacOS/Tuanjie"])
        return next((str(candidate) for candidate in candidates if candidate.is_file()), "")

    def do_POST(self) -> None:
        """Validate and save the complete Agent configuration submitted by the page."""
        if self.path == "/api/select-folder":
            self._write_json(self._select_folder())
            return
        if self.path != "/api/config":
            self._write_json({"ok": False, "error": "not found"}, 404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
            self.config.data["id"] = str(payload.get("id", self.config.data["id"]))
            self.config.data["name"] = str(payload.get("name", self.config.data["name"]))
            for project in payload.get("projects", []):
                self.config.save_project(project)
            self.config.data["projects"] = payload.get("projects", [])
            self.config.path.parent.mkdir(parents=True, exist_ok=True)
            self.config.path.write_text(json.dumps(self.config.data, indent=2), encoding="utf-8")
            self._write_json({"ok": True})
        except (ValueError, TypeError, json.JSONDecodeError) as error:
            self._write_json({"ok": False, "error": str(error)}, 400)

    def _select_folder(self) -> dict[str, Any]:
        """Open the native desktop folder dialog and inspect the chosen Unity project."""
        try:
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            selected = filedialog.askdirectory(title="选择 Unity 工程文件夹")
            root.destroy()
        except Exception as error:
            return {"ok": False, "error": f"无法打开系统文件夹选择器: {error}"}
        if not selected:
            return {"ok": False, "cancelled": True}
        self.path = "/api/project-draft?path=" + selected
        draft = self._project_draft()
        if "error" in draft:
            return {"ok": False, "error": draft["error"]}
        return {"ok": True, "project": draft}


def start_config_server(config: AgentConfig, host: str, port: int, page_path: Path) -> ThreadingHTTPServer:
    """Start the local configuration server in a daemon thread."""
    AgentWebHandler.config = config
    AgentWebHandler.page = page_path.read_bytes()
    server = ThreadingHTTPServer((host, port), AgentWebHandler)
    # A daemon thread lets the Agent process exit normally with the WebSocket loop.
    import threading
    threading.Thread(target=server.serve_forever, name="agent-config-web", daemon=True).start()
    return server
