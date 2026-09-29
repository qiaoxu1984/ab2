"""FastAPI Manager for Agent registration, task dispatch, and log viewing."""

import asyncio
import os
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

from manager.db import Database
from shared.models import BuildRequest
from shared.protocol import message
from shared.report import build_ai_report


def write_diff_report(event: dict[str, Any]) -> None:
    """Materialize a change-notes page so the Feishu link opens immediately."""
    report_dir = Path(os.getenv("AB2_DATA_DIR", "data")) / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    # 任务号由 Agent 生成（十六进制），Path.name 再次确保不越出报告目录。
    file_name = Path(f"{event.get('task_id', '')}-diff.html").name
    (report_dir / file_name).write_text(event.get("html", ""), encoding="utf-8")


# 任务超过该时长没有任何 Agent 事件即判定为孤儿任务，由看门狗兜底标记结束（秒）。
STALE_TASK_SECONDS = 2 * 3600


async def _stale_task_watchdog() -> None:
    """Periodically close tasks whose Agent stopped reporting events.

    Agent 正常断连时由 Agent 侧排队重发补上终态事件；这里只兜底 Agent 崩溃或
    进程重启后永远不会再上报的任务，避免界面永久停在"执行中"。
    """
    while True:
        await asyncio.sleep(60)
        try:
            cutoff = time.time() - STALE_TASK_SECONDS
            for task in state.db.stale_tasks(cutoff):
                # 保留原阶段信息，让界面仍能定位卡住的阶段。
                state.db.apply_event({
                    "kind": "status",
                    "task_id": task["id"],
                    "stage": task["stage"],
                    "status": "failed",
                    "error_code": "stale_task",
                    "message": f"任务超过 {STALE_TASK_SECONDS // 3600} 小时无 Agent 事件，已由 Manager 标记结束",
                })
        except Exception as error:
            # 看门狗自身异常不能中断后续检查。
            print(f"stale task watchdog failed: {error}")


class AgentConnection:
    """Track one connected Agent socket and its latest registration."""

    def __init__(self, socket: WebSocket):
        """Initialize a connection wrapper around an accepted WebSocket."""
        self.socket = socket
        self.info: dict[str, Any] = {}


class ConfigRequest(BaseModel):
    """Carry a complete project configuration update from the Manager UI."""

    project: dict[str, Any]


class ReleaseUnlock(BaseModel):
    """Carry the release build password sent by the dashboard."""

    password: str


class ManagerState:
    """Own live Agent sockets while durable task state remains in SQLite."""

    def __init__(self) -> None:
        """Create the database and connection registry for this process."""
        data_dir = Path(os.getenv("AB2_DATA_DIR", "data"))
        data_dir.mkdir(parents=True, exist_ok=True)
        self.db = Database(str(data_dir / "manager.sqlite3"))
        self.connections: dict[str, AgentConnection] = {}
        self.lock = asyncio.Lock()
        # Keep release unlock tokens in memory; they expire with the Manager process.
        self.release_tokens: dict[str, float] = {}

    def valid_release_token(self, token: str) -> bool:
        """Accept a release token issued within the last 12 hours."""
        issued = self.release_tokens.get(token or "")
        return bool(issued and time.time() - issued < 12 * 3600)

    def find_channel(self, agent_id: str, project_id: str, channel_name: str) -> dict[str, Any]:
        """Locate one configured channel from the live Agent snapshot."""
        agent = next((item for item in self.live_agents() if item["id"] == agent_id), None)
        project = next((item for item in (agent or {}).get("projects", []) if item.get("id") == project_id), None)
        return next((item for item in (project or {}).get("channels", []) if item.get("name") == channel_name), {}) or {}

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


@asynccontextmanager
async def lifespan(_: FastAPI):
    """启动后台看门狗，Manager 进程退出时随之取消。"""
    watchdog = asyncio.create_task(_stale_task_watchdog())
    try:
        yield
    finally:
        watchdog.cancel()


state = ManagerState()
app = FastAPI(title="AB2 Build Manager", lifespan=lifespan)
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


@app.post("/api/release/unlock")
async def unlock_release(request: ReleaseUnlock) -> dict[str, str]:
    """Validate the release password and issue a short-lived build token."""
    expected = os.getenv("AB2_RELEASE_PASSWORD", "123456")
    if not secrets.compare_digest(request.password, expected):
        raise HTTPException(status_code=403, detail="release password is incorrect")
    token = secrets.token_hex(16)
    state.release_tokens[token] = time.time()
    return {"token": token}


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
    # Release channels require the token issued by the release password prompt.
    channel = state.find_channel(request.agent_id, request.project_id, payload["channel"])
    if str(channel.get("switch_to", "")).endswith("_release") and not state.valid_release_token(request.release_token):
        raise HTTPException(status_code=403, detail="release password is required or expired")
    # The token must never reach the Agent or the task database.
    payload.pop("release_token", None)
    try:
        task_id = state.db.create_task(payload)
    except ValueError as error:
        # The per-project exclusivity check must surface as a client error, not a server crash.
        raise HTTPException(status_code=409, detail=str(error))
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
            build_ai_report(result["status"], analysis, f"任务：{task_id}"),
            encoding="utf-8",
        )
    return {"url": f"/reports/{task_id}.html"}


@app.post("/api/tasks/{task_id}/analyze")
async def analyze_task(task_id: str) -> dict[str, bool]:
    """Ask the owning Agent to run a fresh read-only AI analysis for a finished task."""
    result = state.db.get_task(task_id)
    if not result:
        raise HTTPException(status_code=404, detail="task not found")
    if result["status"] in ("queued", "running", "cancel_requested"):
        raise HTTPException(status_code=409, detail="task is still running")
    await state.send(result["agent_id"], message("analyze_task", task=result))
    return {"ok": True}


@app.post("/api/tasks/{task_id}/clear")
async def clear_task(task_id: str) -> dict[str, bool]:
    """Clear a task from the channel's current view without deleting its history."""
    cleared = state.db.clear_task(task_id)
    if not cleared:
        raise HTTPException(status_code=404, detail="task not found")
    if cleared["status"] in ("queued", "running", "cancel_requested"):
        try:
            await state.send(cleared["agent_id"], message("cancel_task", task_id=task_id))
        except HTTPException:
            # Keep the local clear operation successful when the Agent is already offline.
            pass
    return {"ok": True}


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
            elif data.get("type") == "task_created":
                # Agent-side scheduled builds register themselves before streaming events.
                if agent_id:
                    state.db.register_task(data.get("task") or {}, agent_id)
            elif data.get("type") == "event":
                event = data["event"]
                state.db.apply_event(event)
                if event.get("kind") == "diff_report":
                    write_diff_report(event)
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
    # Prevent browsers from keeping stale Manager JavaScript after UI behavior changes.
    return FileResponse(Path(__file__).parent / "static" / safe_name, headers={"Cache-Control": "no-store"})
