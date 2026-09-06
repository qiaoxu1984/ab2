"""Local Agent configuration persistence and validation."""

import json
import re
from pathlib import Path
from typing import Any


class AgentConfig:
    """Load and save the machine identity and local Unity projects."""

    def __init__(self, path: str):
        """Load existing JSON or initialize an empty Agent configuration."""
        self.path = Path(path)
        self.data: dict[str, Any] = {"id": "", "name": "", "projects": []}
        if self.path.is_file():
            self.data.update(json.loads(self.path.read_text(encoding="utf-8")))

    def save_project(self, project: dict[str, Any]) -> None:
        """Validate and persist one project without silently overwriting another."""
        if not project.get("id") or not project.get("path") or not project.get("unity_path"):
            raise ValueError("project id, path, and unity_path are required")
        # Only these two values are accepted; the page and executor enforce the same rule.
        for channel in project.get("channels", []):
            if channel.get("switch_to", "").rsplit("_", 1)[-1] not in ("dev", "release"):
                raise ValueError("channel switch_to must end with dev or release")
            # Allow a channel to lock builds to the project's configured default branch.
            if channel.get("branch_filter", "all_dev") not in ("default", "all_dev", "all_feature", "month_dev", "month_feature"):
                raise ValueError("invalid branch filter")
            if channel.get("platform") not in ("Android", "iOS", "OpenHarmony", "HarmonyOS"):
                raise ValueError("channel platform must be detected as Android, iOS, or OpenHarmony")
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", channel.get("build_method", "")):
                raise ValueError("invalid Unity build method")
        project_ids = [item.get("id") for item in self.data["projects"]]
        if project["id"] in project_ids:
            self.data["projects"][project_ids.index(project["id"])] = project
        else:
            self.data["projects"].append(project)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
