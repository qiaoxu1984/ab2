"""Command-line Agent service that connects to a central Manager."""

import argparse
import asyncio
import json
import platform
import re
import socket
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from websockets.asyncio.client import connect

from agent.config import AgentConfig
from agent.executor import BuildExecutor
from agent.git import run_git
from agent.notify import build_feishu_text, send_feishu_text
from agent.web import start_config_server


def schedule_due(schedule: dict[str, Any], now: datetime) -> bool:
    """Return True when an enabled daily schedule matches the current minute."""
    if not schedule.get("enabled"):
        return False
    return str(schedule.get("time", "")) == now.strftime("%H:%M")


class AgentService:
    """Maintain one outbound Manager connection and execute received tasks."""

    def __init__(self, manager_url: str, config_path: str, web_host: str = "127.0.0.1", web_port: int = 8020):
        """Initialize identity, local configuration, and the build executor."""
        self.manager_url = manager_url
        self.config = AgentConfig(config_path)
        if not self.config.data["id"]:
            self.config.data["id"] = socket.gethostname()
        if not self.config.data["name"]:
            self.config.data["name"] = socket.gethostname()
        self.socket = None
        # Track Manager reachability so scheduled builds can decide whether to stream events.
        self.connected = False
        # Remember one fired date per project and every project with a running build.
        self.scheduled_dates: dict[str, str] = {}
        self.active_projects: set[str] = set()
        # Keep scheduled tasks in memory so a late Manager connection can still record them.
        self.local_tasks: dict[str, dict[str, Any]] = {}
        self.executor = BuildExecutor(lambda event: None)
        self.task_cancellations: dict[str, threading.Event] = {}
        self.web_server = start_config_server(self.config, web_host, web_port, Path(__file__).with_name("config.html"))

    def identity(self) -> dict[str, Any]:
        """Build the registration payload describing this machine and projects."""
        local_ip = self._local_ip()
        projects = []
        for project in self.config.data["projects"]:
            item = dict(project)
            item["branches"] = self._branches(project)
            # Return only the configured default branch when a channel uses the default filter.
            item["channel_branches"] = {channel["name"]: self._filter_branches(item["branches"], channel.get("branch_filter", "all_dev"), project.get("default_branch", "")) for channel in project.get("channels", [])}
            projects.append(item)
        return {"id": local_ip, "name": self.config.data["name"], "ip": local_ip, "platform": platform.system(), "hostname": socket.gethostname(), "version": "0.1.0", "projects": projects}

    def _branches(self, project: dict[str, Any]) -> list[str]:
        """Return the local and remote branch names reported for one project."""
        code, output = run_git(project["path"], "for-each-ref", "--format=%(refname:short)", "refs/heads", "refs/remotes")
        return sorted({line.removeprefix("origin/") for line in output.splitlines() if code == 0 and line.strip()})

    def _local_ip(self) -> str:
        """Resolve the LAN address used as the stable Agent identity."""
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("8.8.8.8", 80))
            return probe.getsockname()[0]
        except OSError:
            return socket.gethostbyname(socket.gethostname())
        finally:
            probe.close()

    @staticmethod
    def _branch_version(branch: str) -> tuple[int, int, int]:
        """Extract the numeric (year, month, day) version encoded in a branch name."""
        match = re.match(r"(?:dev|feature)/(\d{1,2})-(\d{1,2})-(\d{2})", branch)
        if not match:
            # Unparseable names sort behind every real version.
            return (-1, -1, -1)
        month, day, year = (int(part) for part in match.groups())
        return (year, month, day)

    def _filter_branches(self, branches: list[str], branch_filter: str, default_branch: str = "") -> list[str]:
        """Return the configured default branch or newest branches for the filter."""
        # Default mode intentionally does not apply the date-based branch naming rule.
        if branch_filter == "default":
            return [default_branch] if default_branch and default_branch in branches else []
        # Harmony mode lists only dev branches marked with 鸿蒙.
        if branch_filter == "harmony":
            matched = [branch for branch in branches if branch.startswith("dev/") and "鸿蒙" in branch]
            return sorted(matched, key=self._branch_version, reverse=True)
        now = datetime.now()
        current_month_index = now.year * 12 + now.month - 1
        allowed_months = {(((current_month_index - offset) // 12) % 100, (current_month_index - offset) % 12 + 1) for offset in range(3)}
        result = []
        for branch in branches:
            match = re.fullmatch(r"(dev|feature)/(\d{1,2})-(\d{1,2})-(\d{2})(?:/.*)?", branch)
            if not match:
                continue
            kind, month, _, year = match.groups()
            if (int(year), int(month)) not in allowed_months:
                continue
            if branch_filter == "all_dev" and kind != "dev":
                continue
            if branch_filter == "all_feature" and kind != "feature":
                continue
            if branch_filter == "month_dev" and (kind != "dev" or int(month) != now.month or int(year) != now.year % 100):
                continue
            if branch_filter == "month_feature" and (kind != "feature" or int(month) != now.month or int(year) != now.year % 100):
                continue
            result.append(branch)
        # Newest version first so the Manager preselects the latest branch by default.
        result.sort(key=self._branch_version, reverse=True)
        return result

    async def run(self) -> None:
        """Reconnect forever while the local scheduler keeps running without Manager."""
        # The scheduler owns Agent-side daily builds, so it must not live inside the connection loop.
        scheduler = asyncio.create_task(self._schedule_loop())
        try:
            while True:
                try:
                    async with connect(self.manager_url) as socket_connection:
                        self.socket = socket_connection
                        await socket_connection.send(json.dumps({"type": "register", "agent": self.identity()}))
                        self.connected = True
                        # Scheduled builds that started before this connection must still be recorded.
                        await self._register_local_tasks(socket_connection)
                        await self._receive_loop(socket_connection)
                except Exception as error:
                    print(f"Manager connection lost: {error}")
                    await asyncio.sleep(3)
                finally:
                    # Scheduled events are dropped rather than queued while the Manager is unreachable.
                    self.connected = False
        finally:
            scheduler.cancel()

    def _due_schedules(self, now: datetime) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        """Return every project whose daily schedule fires at this minute."""
        due: list[tuple[dict[str, Any], dict[str, Any]]] = []
        today = now.strftime("%Y-%m-%d")
        for project in list(self.config.data.get("projects", [])):
            if not schedule_due(project.get("schedule") or {}, now):
                continue
            # One run per project per day; a build already running also blocks the trigger.
            if self.scheduled_dates.get(project["id"]) == today or project["id"] in self.active_projects:
                continue
            channel = next((item for item in project.get("channels", []) if item.get("enabled", True)), None)
            if not channel:
                continue
            self.scheduled_dates[project["id"]] = today
            due.append((project, channel))
        return due

    async def _schedule_loop(self) -> None:
        """Fire enabled daily project schedules on the Agent's own clock."""
        while True:
            for project, channel in self._due_schedules(datetime.now()):
                asyncio.create_task(self._run_scheduled(project, channel))
            await asyncio.sleep(20)

    async def _run_scheduled(self, project: dict[str, Any], channel: dict[str, Any]) -> None:
        """Run one Agent-triggered daily build and stream it to Manager when connected."""
        options = self._filter_branches(self._branches(project), channel.get("branch_filter", "all_dev"), project.get("default_branch", ""))
        if not options:
            print(f"schedule skipped {project['id']}: no branch matches the channel filter")
            return
        task = {"task_id": uuid.uuid4().hex[:12], "project_id": project["id"], "channel": channel.get("name", ""), "branch": options[0]}
        # Hold the payload until the build ends so the connection loop can retry registration.
        self.local_tasks[task["task_id"]] = task
        if self.connected and self.socket is not None:
            try:
                # Register the Agent-owned task so Manager persistence accepts its follow-up events.
                await self.socket.send(json.dumps({"type": "task_created", "task": task}))
            except Exception:
                # A failed registration must not stop the local scheduled build.
                self.connected = False
        self.active_projects.add(project["id"])
        started = time.time()
        loop = asyncio.get_running_loop()
        events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        cancellation = threading.Event()
        self.task_cancellations[task["task_id"]] = cancellation
        self.executor.set_emitter(task["task_id"], lambda event: loop.call_soon_threadsafe(events.put_nowait, event))
        try:
            build_future = loop.run_in_executor(None, self.executor.run, task, project, channel, cancellation)
            status, message, commit_sha = await self._consume_events(events, self._send_event_when_connected)
            await build_future
            # Scheduled builds notify through the same channel switch as manual builds.
            await self._notify(project, channel, task.get("branch", ""), commit_sha, status, message, started)
        finally:
            self.local_tasks.pop(task["task_id"], None)
            self.active_projects.discard(project["id"])
            self.task_cancellations.pop(task["task_id"], None)
            self.executor.clear_emitter(task["task_id"])

    async def _register_local_tasks(self, socket_connection: Any) -> None:
        """Register scheduled builds that started before the Manager was reachable."""
        for task in list(self.local_tasks.values()):
            try:
                await socket_connection.send(json.dumps({"type": "task_created", "task": task}))
            except Exception:
                # Stop retrying on a broken socket; the next connection tries again.
                return

    async def _send_event_when_connected(self, event: dict[str, Any]) -> None:
        """Forward a scheduled-task event, dropping it while the Manager is offline."""
        if not self.connected or self.socket is None:
            return
        try:
            await self._send_event(self.socket, event)
        except Exception:
            # A dying socket must not abort the local scheduled build.
            self.connected = False

    async def _consume_events(self, events: asyncio.Queue[dict[str, Any]], send: Any) -> tuple[str, str, str]:
        """Forward build events until the terminal analysis event arrives.

        Returns the terminal (status, message, commit_sha) for the notification step.
        """
        terminal_status = ""
        terminal_message = ""
        commit_sha = ""
        while True:
            event = await events.get()
            if event.get("kind") == "analysis_done":
                break
            await send(event)
            commit_sha = event.get("commit_sha") or commit_sha
            if event.get("kind") == "status" and event.get("status") in ("success", "failed", "cancelled"):
                terminal_status = event.get("status", "")
                terminal_message = event.get("message", "")
            # Successful builds always have an analysis; cancellation before AB has none.
            if terminal_status == "cancelled":
                break
        return terminal_status, terminal_message, commit_sha

    async def _notify(self, project: dict[str, Any], channel: dict[str, Any], branch: str, commit_sha: str,
                      status: str, message: str, started: float) -> None:
        """Send the Feishu notification for a finished build when the channel enables it."""
        webhook = str(self.config.data.get("feishu_webhook", "")).strip()
        if status not in ("success", "failed") or not channel.get("notify_feishu") or not webhook:
            return
        text = build_feishu_text(status, project.get("name") or project["id"], channel.get("name", ""), branch,
                                 commit_sha, time.time() - started, message, self._manager_http_url())
        try:
            await asyncio.to_thread(send_feishu_text, webhook, text)
        except Exception as error:
            # A notification failure must never change the build result.
            print(f"feishu notify failed: {error}")

    def _manager_http_url(self) -> str:
        """Return the Manager dashboard URL derived from the WebSocket address."""
        return re.sub(r"^ws", "http", self.manager_url).split("/ws")[0] + "/"

    async def _receive_loop(self, socket_connection: Any) -> None:
        """Handle Manager commands and send periodic heartbeats on one socket."""
        async def heartbeat() -> None:
            """Send current identity periodically to update Manager liveness."""
            while True:
                await asyncio.sleep(5)
                await socket_connection.send(json.dumps({"type": "heartbeat", "agent": self.identity()}))

        heartbeat_task = asyncio.create_task(heartbeat())
        try:
            async for raw in socket_connection:
                data = json.loads(raw)
                if data.get("type") == "config_project":
                    self.config.save_project(data["project"])
                    await socket_connection.send(json.dumps({"type": "register", "agent": self.identity()}))
                elif data.get("type") == "build_task":
                    # Each project task gets its own worker while BuildExecutor shares project locks.
                    asyncio.create_task(self._start_task(socket_connection, data))
                elif data.get("type") == "analyze_task":
                    # Manual analysis is restricted to terminal tasks so it cannot replace a live build callback.
                    asyncio.create_task(self._start_manual_analysis(socket_connection, data["task"]))
                elif data.get("type") == "cancel_task":
                    cancellation = self.task_cancellations.get(data["task_id"])
                    if cancellation:
                        cancellation.set()
                    self.executor.cancel(data["task_id"])
        finally:
            heartbeat_task.cancel()

    async def _start_task(self, socket_connection: Any, task: dict[str, Any]) -> None:
        """Locate configuration and run a blocking build off the event loop."""
        project = next((item for item in self.config.data["projects"] if item["id"] == task["project_id"]), None)
        channel = next((item for item in (project or {}).get("channels", []) if item["name"] == task["channel"]), None)
        if not project or not channel:
            await self._send_event(socket_connection, {"task_id": task["task_id"], "kind": "status", "status": "failed", "message": "project or channel not configured", "sequence": 1})
            return

        self.active_projects.add(project["id"])
        started = time.time()
        loop = asyncio.get_running_loop()
        events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        cancellation = threading.Event()
        self.task_cancellations[task["task_id"]] = cancellation
        self.executor.set_emitter(task["task_id"], lambda event: loop.call_soon_threadsafe(events.put_nowait, event))
        try:
            build_future = loop.run_in_executor(None, self.executor.run, task, project, channel, cancellation)
            status, message, commit_sha = await self._consume_events(events, lambda event: self._send_event(socket_connection, event))
            await build_future
            await self._notify(project, channel, task.get("branch", ""), commit_sha, status, message, started)
        finally:
            self.active_projects.discard(project["id"])
            self.task_cancellations.pop(task["task_id"], None)
            self.executor.clear_emitter(task["task_id"])

    async def _send_event(self, socket_connection: Any, event: dict[str, Any]) -> None:
        """Send one task event using the protocol envelope."""
        await socket_connection.send(json.dumps({"type": "event", "event": event}))

    async def _start_manual_analysis(self, socket_connection: Any, task: dict[str, Any]) -> None:
        """Run a fresh analysis for a finished task and stream it back to Manager."""
        project = next((item for item in self.config.data["projects"] if item["id"] == task["project_id"]), None)
        if not project:
            await self._send_event(socket_connection, {"task_id": task["id"], "kind": "log", "stage": "analysis", "sequence": 1000001, "message": "工程配置不存在，无法主动分析"})
            return
        # Reuse the exact task log instead of the project's legacy shared log path.
        task_project = dict(project)
        log_candidates = sorted((Path(project["path"]) / "Log").glob(f"AB2-build-{task['id']}-*.log"), key=lambda path: path.stat().st_mtime, reverse=True)
        if log_candidates:
            task_project["log_path"] = str(log_candidates[0])
        loop = asyncio.get_running_loop()
        events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        task_id = task["id"]
        # Keep manual-analysis sequence numbers away from the build event sequence range.
        sequence = [1_000_000]
        self.executor.set_emitter(task_id, lambda event: loop.call_soon_threadsafe(events.put_nowait, event))
        try:
            # The stored task status decides whether the report reads as success or failure.
            self.executor._start_failure_analysis(task_id, task_project, task, sequence, task.get("status", ""))
            while True:
                event = await events.get()
                if event.get("kind") == "analysis_done":
                    break
                await self._send_event(socket_connection, event)
        finally:
            self.executor.clear_emitter(task_id)


def main() -> None:
    """Parse Agent startup options and run the asynchronous service."""
    parser = argparse.ArgumentParser(description="AB2 Unity build Agent")
    parser.add_argument("--manager", default="ws://172.18.67.71:8000/ws/agent")
    parser.add_argument("--config", default="data/agent.json")
    parser.add_argument("--web-host", default="127.0.0.1")
    parser.add_argument("--web-port", type=int, default=8020)
    args = parser.parse_args()
    asyncio.run(AgentService(args.manager, args.config, args.web_host, args.web_port).run())


if __name__ == "__main__":
    main()
