"""Shared request and event models used by Manager and Agent."""

from enum import Enum
from pydantic import BaseModel, Field


class TaskStatus(str, Enum):
    """Define the lifecycle states persisted for a build task."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class ChannelConfig(BaseModel):
    """Describe the Unity methods and artifact rule for one build channel."""

    name: str
    platform: str = "unknown"
    switch_to: str
    build_method: str
    enabled: bool = True
    # Select the project's default branch instead of exposing a branch list.
    branch_filter: str = "all_dev"
    # Optional resource version forwarded to the Unity AB2 entry point.
    ab2_version: str = "3800"


class ProjectConfig(BaseModel):
    """Describe one local Unity project hosted by an Agent."""

    id: str
    name: str
    path: str
    unity_path: str
    log_path: str
    default_branch: str = "main"
    channels: list[ChannelConfig] = Field(default_factory=list)


class AgentInfo(BaseModel):
    """Describe a registered build machine."""

    id: str
    name: str
    platform: str
    hostname: str
    unity_version: str = ""
    version: str = "0.1.0"
    projects: list[ProjectConfig] = Field(default_factory=list)


class BuildRequest(BaseModel):
    """Represent a Manager request to build one branch and channel."""

    agent_id: str
    project_id: str
    channel: str = ""
    branch: str


class TaskEvent(BaseModel):
    """Carry an Agent status, log, or artifact update to Manager."""

    task_id: str
    kind: str
    status: TaskStatus | None = None
    stage: str = ""
    message: str = ""
    sequence: int = 0
    commit_sha: str = ""
    error_code: str = ""
