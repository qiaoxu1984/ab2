"""Safe Git command adapter used by the Agent build executor."""

import re
import subprocess
from typing import Callable

# A suffixed branch like dev/9-2-26_鸿蒙 carries its mainline as the date-encoded prefix.
MAINLINE_PATTERN = re.compile(r"(?P<mainline>(?:dev|feature)/\d{1,2}-\d{1,2}-\d{2})(?P<suffix>.+)$")

# 差异说明的采集上限：明细最多保留最新 80 条、总长不超过 6000 字符，避免飞书消息和 AI 提示词过长。
MAX_CHANGE_COMMITS = 80
MAX_CHANGE_CHARS = 6000


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


def merge_mainline(project_path: str, mainline: str, on_output: Callable[[str], None] | None = None) -> str:
    """Merge the fetched origin mainline into the current branch and commit the result.

    Returns the merge commit SHA so the task record can show the synced revision.
    """
    if not mainline or mainline.startswith("-"):
        raise ValueError("invalid mainline branch")
    code, _ = run_git(project_path, "rev-parse", "--verify", f"refs/remotes/origin/{mainline}")
    if code:
        # A missing mainline is a caller-side skip instead of a build failure.
        raise LookupError(f"origin/{mainline} does not exist")
    # Build machines may have no Git identity, so fall back to a stable AB2 author.
    code, email = run_git(project_path, "config", "user.email")
    setup = () if code == 0 and email else ("-c", "user.email=ab2@local", "-c", "user.name=AB2")
    args = (*setup, "merge", "--no-ff", "--no-edit", f"origin/{mainline}")
    code, output = run_git(project_path, *args)
    if on_output:
        for line in output.splitlines():
            on_output(line)
    if code:
        # Abort so a conflicted merge cannot leak into the next build.
        run_git(project_path, "merge", "--abort")
        raise RuntimeError(f"git {' '.join(args)} failed: {output}")
    code, sha = run_git(project_path, "rev-parse", "HEAD")
    if code:
        raise RuntimeError(f"cannot resolve merge commit: {sha}")
    return sha


def file_changed(project_path: str, old_sha: str, new_sha: str, file_path: str) -> bool:
    """Check whether one tracked file changed between the pre-sync and post-sync commits."""
    code, output = run_git(project_path, "diff", "--name-only", old_sha, new_sha, "--", file_path)
    if code != 0:
        raise RuntimeError(f"cannot inspect Git file change: {output}")
    return any(line.replace("\\", "/") == file_path for line in output.splitlines())


def current_revision(project_path: str) -> tuple[str, str]:
    """Return the checked-out branch and HEAD commit, or empty strings when Git cannot answer."""
    code, branch = run_git(project_path, "branch", "--show-current")
    if code or not branch:
        return "", ""
    code, sha = run_git(project_path, "rev-parse", "HEAD")
    if code or not sha:
        return "", ""
    return branch, sha


def is_ancestor(project_path: str, old_sha: str, new_sha: str) -> bool:
    """Check whether the baseline commit is still an ancestor of the new revision."""
    code, _ = run_git(project_path, "merge-base", "--is-ancestor", old_sha, new_sha)
    return code == 0


def revision_changes(project_path: str, old_sha: str, new_sha: str) -> tuple[str, str, int]:
    """Collect non-merge commits and the diffstat between two revisions for release notes.

    Returns the newest-first commit detail text, the shortstat line, and the untruncated count.
    """
    code, output = run_git(project_path, "log", "--no-merges", "--date=format:%m-%d", "--pretty=format:%h %ad %an %s", f"{old_sha}..{new_sha}")
    if code:
        raise RuntimeError(f"cannot list revision commits: {output}")
    commits = [line.strip() for line in output.splitlines() if line.strip()]
    total = len(commits)
    # 只保留最新的一批提交，防止极端情况下提示词和飞书消息过长。
    detail = "\n".join(commits[:MAX_CHANGE_COMMITS])
    if len(detail) > MAX_CHANGE_CHARS:
        detail = detail[:MAX_CHANGE_CHARS] + "\n…"
    code, stat = run_git(project_path, "diff", "--shortstat", old_sha, new_sha)
    return detail, (stat.strip() if code == 0 else ""), total
