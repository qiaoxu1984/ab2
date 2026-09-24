"""Git 差异采集的单元测试（标准库 unittest）。"""

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent.git import current_revision, is_ancestor, revision_changes


def run(command: list[str], cwd: str) -> str:
    """在临时仓库里执行一条 Git 命令并返回标准输出。"""
    result = subprocess.run(["git", *command], cwd=cwd, text=True, capture_output=True, encoding="utf-8", errors="replace")
    if result.returncode:
        raise RuntimeError(result.stdout + result.stderr)
    return result.stdout.strip()


def commit(cwd: str, message: str) -> str:
    """用唯一命名的文件提交一次，避免分支合并时产生冲突。"""
    (Path(cwd) / f"{message.replace(' ', '_')}.txt").write_text(message, encoding="utf-8")
    run(["add", "-A"], cwd)
    run(["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", message], cwd)
    return run(["rev-parse", "HEAD"], cwd)


class GitChangeTests(unittest.TestCase):
    """覆盖基准读取、祖先判断和提交明细采集。"""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = self.temp.name
        run(["init", "-b", "main"], self.path)
        self.first = commit(self.path, "first")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_current_revision_reads_branch_and_sha(self) -> None:
        branch, sha = current_revision(self.path)
        self.assertEqual(branch, "main")
        self.assertEqual(sha, self.first)

    def test_revision_changes_excludes_merge_commits(self) -> None:
        commit(self.path, "second")
        run(["checkout", "-b", "side", self.first], self.path)
        commit(self.path, "third")
        run(["checkout", "main"], self.path)
        run(["-c", "user.email=t@t", "-c", "user.name=t", "merge", "--no-ff", "--no-edit", "side"], self.path)
        merge_sha = run(["rev-parse", "HEAD"], self.path)
        detail, stat, total = revision_changes(self.path, self.first, merge_sha)
        self.assertEqual(total, 2)
        self.assertIn("second", detail)
        self.assertIn("third", detail)
        self.assertNotIn("merge", detail.lower())
        self.assertTrue(stat)

    def test_revision_changes_caps_detail_but_keeps_total(self) -> None:
        commit(self.path, "second")
        commit(self.path, "third")
        head = run(["rev-parse", "HEAD"], self.path)
        with mock.patch("agent.git.MAX_CHANGE_COMMITS", 1):
            detail, _, total = revision_changes(self.path, self.first, head)
        self.assertEqual(total, 2)
        self.assertEqual(len(detail.splitlines()), 1)

    def test_is_ancestor_detects_rewritten_history(self) -> None:
        second = commit(self.path, "second")
        self.assertTrue(is_ancestor(self.path, self.first, second))
        run(["checkout", "-B", "main", self.first], self.path)
        rewritten = commit(self.path, "rewritten")
        self.assertFalse(is_ancestor(self.path, second, rewritten))


if __name__ == "__main__":
    unittest.main()
