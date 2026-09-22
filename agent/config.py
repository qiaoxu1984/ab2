"""Local Agent configuration persistence and validation."""

import json
import re
from pathlib import Path
from typing import Any


class AgentConfig:
    """Load and save the machine identity and local Unity projects."""

    # Use the current production resource version when a channel has no override.
    DEFAULT_AB2_VERSION = "3800"

    def __init__(self, path: str):
        """Load existing JSON, migrate legacy project ids, and initialize defaults."""
        self.path = Path(path)
        self.data: dict[str, Any] = {"id": "", "name": "", "projects": []}
        if self.path.is_file():
            self.data.update(json.loads(self.path.read_text(encoding="utf-8")))
            # Rewrite folder-name ids to full paths so same-named project folders cannot collide.
            if self._migrate_project_ids():
                self.write()

    @staticmethod
    def project_id(project_path: str) -> str:
        """Derive the stable project id from a normalized full path.

        Use forward slashes and lower case so Windows separators and letter case
        cannot split one project into two identities.
        """
        return str(project_path).replace("\\", "/").lower()

    def _migrate_project_ids(self) -> bool:
        """Rewrite every legacy project id to its path form and break remaining ties.

        Returns True when the in-memory configuration changed and needs saving.
        """
        changed = False
        used: set[str] = set()
        for project in self.data.get("projects", []):
            project_path = project.get("path", "")
            # Leave entries without a path untouched; save_project rejects them later.
            if not project_path:
                continue
            new_id = self.project_id(project_path)
            # Two entries that truly point at one path only get a numeric suffix.
            base_id = new_id
            suffix = 2
            while new_id in used:
                new_id = f"{base_id}-{suffix}"
                suffix += 1
            used.add(new_id)
            if project.get("id") != new_id:
                project["id"] = new_id
                changed = True
        return changed

    def write(self) -> None:
        """Persist the in-memory Agent configuration as formatted JSON."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2), encoding="utf-8")

    def save_project(self, project: dict[str, Any]) -> None:
        """Validate and persist one project without silently overwriting another."""
        if not project.get("id") or not project.get("path") or not project.get("unity_path"):
            raise ValueError("project id, path, and unity_path are required")
        # Only these two values are accepted; the page and executor enforce the same rule.
        for channel in project.get("channels", []):
            if channel.get("switch_to", "").rsplit("_", 1)[-1] not in ("dev", "release"):
                raise ValueError("channel switch_to must end with dev or release")
            # Allow a channel to lock builds to the project's configured default branch.
            if channel.get("branch_filter", "all_dev") not in ("default", "all_dev", "all_feature", "month_dev", "month_feature", "harmony"):
                raise ValueError("invalid branch filter")
            if channel.get("platform") not in ("Android", "iOS", "OpenHarmony", "HarmonyOS"):
                raise ValueError("channel platform must be detected as Android, iOS, or OpenHarmony")
            # Restrict the optional resource version to safe command-line characters.
            if channel.get("ab2_version", "") and not re.fullmatch(r"[A-Za-z0-9_.-]+", str(channel["ab2_version"])):
                raise ValueError("invalid AB2 resource version")
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", channel.get("build_method", "")):
                raise ValueError("invalid Unity build method")
        # Optional daily schedule: a boolean switch plus a local HH:MM trigger time.
        schedule = project.get("schedule") or {}
        if schedule:
            if not isinstance(schedule.get("enabled", False), bool):
                raise ValueError("schedule enabled must be a boolean")
            if schedule.get("enabled") and not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", str(schedule.get("time", ""))):
                raise ValueError("schedule time must be HH:MM")
        project_ids = [item.get("id") for item in self.data["projects"]]
        if project["id"] in project_ids:
            existing = self.data["projects"][project_ids.index(project["id"])]
            # Reject an id that is already bound to a different project path.
            if existing.get("path") != project.get("path"):
                raise ValueError("project id already used by another project")
            self.data["projects"][project_ids.index(project["id"])] = project
        else:
            self.data["projects"].append(project)
        self.write()
