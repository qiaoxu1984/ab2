"""Small SQLite persistence layer for the Manager MVP."""

import json
import sqlite3
import threading
import time
import uuid
from typing import Any


class Database:
    """Persist tasks, logs, reports, and build artifacts in SQLite."""

    def __init__(self, path: str):
        """Open the database and create the schema needed by the service."""
        self.connection = sqlite3.connect(path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self._create_schema()

    def _create_schema(self) -> None:
        """Create tables and indexes while preserving data across restarts."""
        with self.lock, self.connection:
            self.connection.executescript(
                """
                DROP TABLE IF EXISTS agents;
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, project_id TEXT NOT NULL,
                    channel TEXT NOT NULL, branch TEXT NOT NULL, status TEXT NOT NULL,
                    stage TEXT NOT NULL DEFAULT '', commit_sha TEXT NOT NULL DEFAULT '',
                    error_code TEXT NOT NULL DEFAULT '', error_message TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL, started_at REAL, finished_at REAL,
                    phase_times_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS logs (
                    task_id TEXT NOT NULL, sequence INTEGER NOT NULL, stage TEXT NOT NULL,
                    message TEXT NOT NULL, created_at REAL NOT NULL,
                    PRIMARY KEY(task_id, sequence)
                );
                CREATE TABLE IF NOT EXISTS artifacts (
                    task_id TEXT NOT NULL, path TEXT NOT NULL, name TEXT NOT NULL,
                    size INTEGER NOT NULL, sha256 TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS reports (
                    task_id TEXT PRIMARY KEY, html TEXT NOT NULL, created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_tasks_project_status
                    ON tasks(agent_id, project_id, status);
                """
            )
            columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(tasks)").fetchall()}
            if "phase_times_json" not in columns:
                self.connection.execute("ALTER TABLE tasks ADD COLUMN phase_times_json TEXT NOT NULL DEFAULT '{}'")

    def create_task(self, request: dict[str, str]) -> str:
        """Atomically create a task unless its Agent/project already runs one."""
        task_id = uuid.uuid4().hex[:12]
        with self.lock, self.connection:
            active = self.connection.execute(
                "SELECT id FROM tasks WHERE agent_id=? AND project_id=? AND status IN ('queued','running','cancel_requested')",
                (request["agent_id"], request["project_id"]),
            ).fetchone()
            if active:
                raise ValueError(f"project already has active task {active['id']}")
            self.connection.execute(
                "INSERT INTO tasks(id,agent_id,project_id,channel,branch,status,created_at) VALUES(?,?,?,?,?,?,?)",
                (task_id, request["agent_id"], request["project_id"], request["channel"],
                 request["branch"], "queued", time.time()),
            )
        return task_id

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        """Return one task with its ordered logs and artifacts."""
        with self.lock:
            row = self.connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not row:
                return None
            logs = self.connection.execute("SELECT * FROM logs WHERE task_id=? AND stage!='unity' ORDER BY sequence", (task_id,)).fetchall()
            artifacts = self.connection.execute("SELECT path,name,size,sha256 FROM artifacts WHERE task_id=?", (task_id,)).fetchall()
            report = self.connection.execute("SELECT 1 FROM reports WHERE task_id=?", (task_id,)).fetchone()
        result = dict(row)
        result["phase_times"] = json.loads(result.pop("phase_times_json", "{}"))
        result["logs"] = [dict(item) for item in logs if not (item["stage"] == "analysis" and ("[0m" in item["message"] or item["message"].lstrip().startswith("> build")))]
        result["artifacts"] = [dict(item) for item in artifacts]
        result["report_available"] = report is not None
        return result

    def latest_task(self, agent_id: str, project_id: str, channel: str) -> dict[str, Any] | None:
        """Return the newest persisted task for one Agent project channel."""
        with self.lock:
            row = self.connection.execute(
                "SELECT id FROM tasks WHERE agent_id=? AND project_id=? AND channel=? ORDER BY created_at DESC LIMIT 1",
                (agent_id, project_id, channel),
            ).fetchone()
        return self.get_task(row["id"]) if row else None

    def active_tasks(self) -> list[dict[str, Any]]:
        """Return tasks waiting or running so startup can restore scheduler state."""
        with self.lock:
            rows = self.connection.execute("SELECT * FROM tasks WHERE status IN ('queued','running')").fetchall()
        return [dict(row) for row in rows]

    def apply_event(self, event: dict[str, Any]) -> None:
        """Apply an idempotent Agent event to task, log, and artifact tables."""
        now = time.time()
        with self.lock, self.connection:
            row = self.connection.execute("SELECT phase_times_json FROM tasks WHERE id=?", (event["task_id"],)).fetchone()
            phase_times = json.loads(row["phase_times_json"] or "{}") if row else {}
            stage = event.get("stage", "")
            if stage and event.get("kind") == "status":
                previous_stage = self.connection.execute("SELECT stage FROM tasks WHERE id=?", (event["task_id"],)).fetchone()
                if previous_stage and previous_stage["stage"] and previous_stage["stage"] != stage:
                    phase_times.setdefault(previous_stage["stage"], {"started_at": now})
                    phase_times[previous_stage["stage"]].setdefault("finished_at", now)
                phase_times.setdefault(stage, {"started_at": now})
                if event.get("status") in ("success", "failed", "cancelled", "unknown"):
                    phase_times[stage]["finished_at"] = now
            if stage == "analysis" and event.get("kind") == "log":
                phase_times.setdefault("analysis", {"started_at": now})
                if event.get("message") != "__AB2_ANALYSIS_STARTED__":
                    phase_times["analysis"]["finished_at"] = now
            if event["kind"] == "log" and event.get("stage") != "unity":
                self.connection.execute(
                    "INSERT OR IGNORE INTO logs VALUES(?,?,?,?,?)",
                    (event["task_id"], event["sequence"], event.get("stage", ""), event.get("message", ""), now),
                )
            if event["kind"] == "report":
                self.connection.execute("INSERT OR REPLACE INTO reports(task_id,html,created_at) VALUES(?,?,?)", (event["task_id"], event.get("html", ""), now))
            self.connection.execute(
                """UPDATE tasks SET stage=?, commit_sha=COALESCE(NULLIF(?,''),commit_sha),
                 error_code=COALESCE(NULLIF(?,''),error_code), error_message=COALESCE(NULLIF(?,''),error_message),
                 status=COALESCE(NULLIF(?,''),status), started_at=COALESCE(started_at,?),
                 finished_at=CASE WHEN ? IN ('success','failed','cancelled','unknown') THEN ? ELSE finished_at END,
                 phase_times_json=? WHERE id=?""",
                (event.get("stage", "") if event.get("kind") == "status" else "", event.get("commit_sha", ""), event.get("error_code", ""),
                 event.get("message", "") if event.get("kind") == "status" else "",
                  event.get("status", ""), now, event.get("status", ""), now, json.dumps(phase_times), event["task_id"]),
            )
            for artifact in event.get("artifacts", []):
                self.connection.execute(
                    "INSERT INTO artifacts(task_id,path,name,size,sha256) VALUES(?,?,?,?,?)",
                    (event["task_id"], artifact.get("path", ""), artifact.get("name", ""), artifact.get("size", 0), artifact.get("sha256", "")),
                )
