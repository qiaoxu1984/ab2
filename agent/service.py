"""Command-line Agent service that connects to a central Manager."""

import argparse
import asyncio
import json
import platform
import re
import socket
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from websockets.asyncio.client import connect

from agent.config import AgentConfig
from agent.executor import BuildExecutor
from agent.git import run_git
from agent.web import start_config_server


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
        self.executor = BuildExecutor(lambda event: None)
        self.task_cancellations: dict[str, threading.Event] = {}
        self.web_server = start_config_server(self.config, web_host, web_port, Path(__file__).with_name("config.html"))

    def identity(self) -> dict[str, Any]:
        """Build the registration payload describing this machine and projects."""
        local_ip = self._local_ip()
        projects = []
        for project in self.config.data["projects"]:
            item = dict(project)
            code, output = run_git(project["path"], "for-each-ref", "--format=%(refname:short)", "refs/heads", "refs/remotes")
            item["branches"] = sorted({line.removeprefix("origin/") for line in output.splitlines() if code == 0 and line.strip()})
            # Return only the configured default branch when a channel uses the default filter.
            item["channel_branches"] = {channel["name"]: self._filter_branches(item["branches"], channel.get("branch_filter", "all_dev"), project.get("default_branch", "")) for channel in project.get("channels", [])}
            projects.append(item)
        return {"id": local_ip, "name": self.config.data["name"], "ip": local_ip, "platform": platform.system(), "hostname": socket.gethostname(), "version": "0.1.0", "projects": projects}

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

    def _filter_branches(self, branches: list[str], branch_filter: str, default_branch: str = "") -> list[str]:
        """Return the configured default branch or standard branches from recent months."""
        # Default mode intentionally does not apply the date-based branch naming rule.
        if branch_filter == "default":
            return [default_branch] if default_branch and default_branch in branches else []
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
        return result

    async def run(self) -> None:
        """Reconnect forever and keep the Agent available through transient outages."""
        while True:
            try:
                async with connect(self.manager_url) as socket_connection:
                    self.socket = socket_connection
                    await socket_connection.send(json.dumps({"type": "register", "agent": self.identity()}))
                    await self._receive_loop(socket_connection)
            except Exception as error:
                print(f"Manager connection lost: {error}")
                await asyncio.sleep(3)

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

        loop = asyncio.get_running_loop()
        events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        cancellation = threading.Event()
        self.task_cancellations[task["task_id"]] = cancellation
        self.executor.emit = lambda event: loop.call_soon_threadsafe(events.put_nowait, event)
        build_future = loop.run_in_executor(None, self.executor.run, task, project, channel, cancellation)
        while True:
            event = await events.get()
            if event.get("kind") == "analysis_done":
                break
            await self._send_event(socket_connection, event)
            if event.get("kind") == "status" and event.get("status") in ("success", "cancelled"):
                break
        await build_future
        self.task_cancellations.pop(task["task_id"], None)

    async def _send_event(self, socket_connection: Any, event: dict[str, Any]) -> None:
        """Send one task event using the protocol envelope."""
        await socket_connection.send(json.dumps({"type": "event", "event": event}))


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
