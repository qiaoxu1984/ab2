"""Safe Git command adapter used by the Agent build executor."""

import subprocess
from typing import Callable


def run_git(project_path: str, *args: str) -> tuple[int, str]:
    """Run Git without a shell and return its exit code and combined output."""
    result = subprocess.run(["git", *args], cwd=project_path, text=True, capture_output=True, encoding="utf-8", errors="replace")
    return result.returncode, (result.stdout + result.stderr).strip()


def sync_branch(project_path: str, branch: str, on_output: Callable[[str], None] | None = None) -> str:
    """Fetch and reset a clean local branch, returning its resulting commit SHA."""
    if not branch or branch.startswith("-"):
        raise ValueError("invalid branch")
    commands = [("fetch", "--all", "--prune"), ("checkout", branch), ("reset", "--hard", f"origin/{branch}"), ("clean", "-df")]
    for args in commands:
        code, output = run_git(project_path, *args)
        if on_output:
            for line in output.splitlines():
                on_output(line)
        if code:
            raise RuntimeError(f"git {' '.join(args)} failed: {output}")
    code, sha = run_git(project_path, "rev-parse", "HEAD")
    if code:
        raise RuntimeError(f"cannot resolve commit: {sha}")
    return sha


def file_changed(project_path: str, old_sha: str, new_sha: str, file_path: str) -> bool:
    """Check whether one tracked file changed between the pre-sync and post-sync commits."""
    code, output = run_git(project_path, "diff", "--name-only", old_sha, new_sha, "--", file_path)
    if code != 0:
        raise RuntimeError(f"cannot inspect Git file change: {output}")
    return any(line.replace("\\", "/") == file_path for line in output.splitlines())
