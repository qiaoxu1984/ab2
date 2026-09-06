"""FastAPI Manager for Agent registration, task dispatch, and log viewing."""

import asyncio
import html
import os
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

from manager.db import Database
from shared.models import BuildRequest
from shared.protocol import message


class AgentConnection:
    """Track one connected Agent socket and its latest registration."""

    def __init__(self, socket: WebSocket):
        """Initialize a connection wrapper around an accepted WebSocket."""
        self.socket = socket
        self.info: dict[str, Any] = {}


class ConfigRequest(BaseModel):
    """Carry a complete project configuration update from the Manager UI."""

    project: dict[str, Any]


class ManagerState:
    """Own live Agent sockets while durable task state remains in SQLite."""

    def __init__(self) -> None:
        """Create the database and connection registry for this process."""
        data_dir = Path(os.getenv("AB2_DATA_DIR", "data"))
        data_dir.mkdir(parents=True, exist_ok=True)
        self.db = Database(str(data_dir / "manager.sqlite3"))
        self.connections: dict[str, AgentConnection] = {}
        self.lock = asyncio.Lock()

    async def send(self, agent_id: str, payload: dict[str, Any]) -> None:
        """Send a command to an online Agent or fail with a clear API error."""
        connection = self.connections.get(agent_id)
        if not connection:
            raise HTTPException(status_code=409, detail="agent is offline")
        await connection.socket.send_json(payload)

    def live_agents(self) -> list[dict[str, Any]]:
        """Return only Agent data from currently connected WebSocket sessions."""
        return sorted(
            [{**connection.info, "online": True} for connection in self.connections.values()],
            key=lambda item: str(item.get("name", "")),
        )


state = ManagerState()
app = FastAPI(title="AB2 Build Manager")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.get("/api/agents")
async def agents() -> list[dict[str, Any]]:
    """Return only currently connected Agents and their latest project configuration."""
    return state.live_agents()


@app.post("/api/projects")
async def update_project(agent_id: str, request: ConfigRequest) -> dict[str, bool]:
    """Send a project configuration to an Agent for local validation and storage."""
    await state.send(agent_id, message("config_project", project=request.project))
    return {"ok": True}


@app.post("/api/tasks")
async def create_task(request: BuildRequest) -> dict[str, str]:
    """Create and dispatch one task while enforcing per-project exclusivity."""
    payload = request.model_dump()
    if not payload["channel"]:
        agent = next((item for item in state.live_agents() if item["id"] == request.agent_id), None)
        project = next((item for item in (agent or {}).get("projects", []) if item.get("id") == request.project_id), None)
        payload["channel"] = ((project or {}).get("channels") or [{}])[0].get("name", "")
    if not payload["channel"]:
        raise HTTPException(status_code=400, detail="project has no configured channel")
    task_id = state.db.create_task(payload)
    try:
        await state.send(request.agent_id, message("build_task", task_id=task_id, **payload))
    except Exception:
        state.db.apply_event({"kind": "status", "task_id": task_id, "status": "failed", "error_code": "dispatch_error", "message": "Agent dispatch failed"})
        raise
    return {"task_id": task_id}


@app.get("/api/tasks/latest")
async def latest_task(agent_id: str, project_id: str, channel: str) -> dict[str, Any]:
    """Restore the latest task when a Manager page is opened again."""
    result = state.db.latest_task(agent_id, project_id, channel)
    return result or {}


@app.get("/api/tasks/{task_id}")
async def task(task_id: str) -> dict[str, Any]:
    """Return one task snapshot with logs and artifacts."""
    result = state.db.get_task(task_id)
    if not result:
        raise HTTPException(status_code=404, detail="task not found")
    analysis_start = result.get("phase_times", {}).get("analysis", {}).get("started_at")
    analysis_result = any(log["stage"] == "analysis" and log["message"] != "__AB2_ANALYSIS_STARTED__" for log in result["logs"])
    if result["status"] == "failed" and analysis_start and not analysis_result and time.time() - analysis_start > 300:
        state.db.apply_event({"kind": "log", "task_id": task_id, "sequence": 900000000, "stage": "analysis", "message": "OpenCode 分析超时，已由 Manager 标记结束"})
        result = state.db.get_task(task_id)
    return result


@app.get("/api/tasks/{task_id}/report")
async def task_report(task_id: str) -> dict[str, str]:
    """Generate a durable HTML report from the task's AI analysis logs."""
    result = state.db.get_task(task_id)
    if not result:
        raise HTTPException(status_code=404, detail="task not found")
    report_dir = Path(os.getenv("AB2_DATA_DIR", "data")) / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    stored = state.db.connection.execute("SELECT html FROM reports WHERE task_id=?", (task_id,)).fetchone()
    if stored:
        (report_dir / f"{task_id}.html").write_text(stored["html"], encoding="utf-8")
    else:
        analysis = "\n\n".join(log["message"] for log in result["logs"] if log["stage"] == "analysis" and log["message"] != "__AB2_ANALYSIS_STARTED__")
        (report_dir / f"{task_id}.html").write_text(
        "<!doctype html><meta charset='utf-8'><title>AB2 AI 分析报告</title>"
        "<style>body{background:#111827;color:#dbeafe;font:15px system-ui;padding:32px;line-height:1.7}pre{white-space:pre-wrap}</style>"
        f"<h1>AB2 AI 失败原因分析</h1><p>任务：{html.escape(task_id)}</p><pre>{html.escape(analysis or '暂无分析结果')}</pre>",
            encoding="utf-8",
        )
    return {"url": f"/reports/{task_id}.html"}


@app.get("/reports/{name}")
async def report_file(name: str) -> FileResponse:
    """Serve only a generated task report from the report directory."""
    safe_name = Path(name).name
    report_path = Path(os.getenv("AB2_DATA_DIR", "data")) / "reports" / safe_name
    if not report_path.is_file():
        raise HTTPException(status_code=404, detail="report not found")
    return FileResponse(report_path, media_type="text/html")


@app.websocket("/ws/agent")
async def agent_socket(socket: WebSocket) -> None:
    """Accept Agent registration, heartbeat, and task event messages."""
    await socket.accept()
    agent_id = ""
    connection = AgentConnection(socket)
    try:
        while True:
            data = await socket.receive_json()
            if data.get("type") in ("register", "heartbeat"):
                agent = dict(data["agent"])
                agent_id = agent["id"]
                for existing in state.live_agents():
                    if existing["id"] == agent_id and existing["hostname"] != agent["hostname"]:
                        agent_id = f"{agent_id}-{agent['hostname']}"
                        break
                agent["id"] = agent_id
                # A heartbeat replaces the live snapshot without writing Agent data to disk.
                connection.info = agent
                state.connections[agent_id] = connection
                await socket.send_json(message("registered", server_time=time.time()))
            elif data.get("type") == "event":
                state.db.apply_event(data["event"])
    except WebSocketDisconnect:
        if agent_id and state.connections.get(agent_id) is connection:
            state.connections.pop(agent_id, None)


@app.get("/")
async def index() -> FileResponse:
    """Serve the browser dashboard from the Manager package."""
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/static/{name}")
async def static_file(name: str) -> FileResponse:
    """Serve a dashboard asset without exposing arbitrary filesystem paths."""
    safe_name = Path(name).name
    return FileResponse(Path(__file__).parent / "static" / safe_name)
