"""Safe Git command adapter used by the Agent build executor."""

import re
import subprocess
from typing import Callable

# A suffixed branch like dev/9-2-26_鸿蒙 carries its mainline as the date-encoded prefix.
MAINLINE_PATTERN = re.compile(r"(?P<mainline>(?:dev|feature)/\d{1,2}-\d{1,2}-\d{2})(?P<suffix>.+)$")


def run_git(project_path: str, *args: str) -> tuple[int, str]:
    """Run Git without a shell and return its exit code and combined output."""
    result = subprocess.run(["git", *args], cwd=project_path, text=True, capture_output=True, encoding="utf-8", errors="replace")
    return result.returncode, (result.stdout + result.stderr).strip()


def sync_branch(project_path: str, branch: str, on_output: Callable[[str], None] | None = None) -> str:
    """Fetch and reset a clean local branch, returning its resulting commit SHA."""
    if not branch or branch.startswith("-"):
        raise ValueError("invalid branch")
    # Clear build-generated worktree changes before checkout, otherwise Git refuses to overwrite tracked files.
    # Preserve the legacy shared log because another process may still hold it; new tasks use per-task logs.
    clean_args = ("clean", "-df", "-e", "Log/AB2-build.log")
    commands = [("reset", "--hard"), clean_args, ("fetch", "--all", "--prune"), ("checkout", branch), ("reset", "--hard", f"origin/{branch}"), clean_args]
    for args in commands:
        code, output = run_git(project_path, *args)
        if on_output:
            for line in output.splitlines():
                on_output(line)
        if code:
            raise RuntimeError(f"git {' '.join(args)} failed: {output}")
    code, current_branch = run_git(project_path, "branch", "--show-current")
    if code or current_branch != branch:
        raise RuntimeError(f"git checkout did not select requested branch: expected {branch}, got {current_branch}")
    code, sha = run_git(project_path, "rev-parse", "HEAD")
    if code:
        raise RuntimeError(f"cannot resolve commit: {sha}")
    return sha


def mainline_branch(branch: str) -> str:
    """Return the mainline branch behind a suffixed branch name, or an empty string."""
    name = branch.strip()
    match = MAINLINE_PATTERN.fullmatch(name)
    # A plain mainline branch has no suffix and therefore nothing to merge.
    return match.group("mainline") if match else ""


def merge_mainline(project_path: str, mainline: str, on_output: Callable[[str], None] | None = None) -> None:
    """Merge the fetched origin mainline into the current branch without committing.

    The merge result stays in the worktree and index, so the packed data contains
    the mainline changes while the local branch keeps its original commit.
    """
    if not mainline or mainline.startswith("-"):
        raise ValueError("invalid mainline branch")
    code, _ = run_git(project_path, "rev-parse", "--verify", f"refs/remotes/origin/{mainline}")
    if code:
        # A missing mainline is a caller-side skip instead of a build failure.
        raise LookupError(f"origin/{mainline} does not exist")
    args = ("merge", "--no-ff", "--no-commit", f"origin/{mainline}")
    code, output = run_git(project_path, *args)
    if on_output:
        for line in output.splitlines():
            on_output(line)
    if code:
        # Abort so a conflicted merge cannot leak into the next build.
        run_git(project_path, "merge", "--abort")
        raise RuntimeError(f"git {' '.join(args)} failed: {output}")


def file_changed(project_path: str, old_sha: str, new_sha: str, file_path: str) -> bool:
    """Check whether one tracked file changed between the pre-sync and post-sync commits."""
    code, output = run_git(project_path, "diff", "--name-only", old_sha, new_sha, "--", file_path)
    if code != 0:
        raise RuntimeError(f"cannot inspect Git file change: {output}")
    return any(line.replace("\\", "/") == file_path for line in output.splitlines())
