"""Git 差异采集的单元测试（标准库 unittest）。"""

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent.git import current_revision, is_ancestor, mainline_branch, revision_changes


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


class MainlineBranchTests(unittest.TestCase):
    """覆盖带后缀分支推导主线（父分支）的规则。"""

    def test_suffixed_branch_returns_mainline(self) -> None:
        """普通后缀（鸿蒙、资源版本）去掉后缀后即主线。"""
        self.assertEqual("dev/9-2-26", mainline_branch("dev/9-2-26_鸿蒙"))
        self.assertEqual("dev/11-1-26", mainline_branch("dev/11-1-26_4200"))

    def test_dotted_day_branch_returns_mainline(self) -> None:
        """日期中的日带小数点时，点号属于主线名而不是后缀。"""
        self.assertEqual("dev/11-1.5-26", mainline_branch("dev/11-1.5-26_4200"))
        self.assertEqual("dev/11-1.5-26", mainline_branch("dev/11-1.5-26_鸿蒙"))

    def test_multiple_suffixes_drop_only_the_last_one(self) -> None:
        """多级后缀只去掉最后一级，父分支保留前一级后缀。"""
        self.assertEqual("dev/11-1-26_4200", mainline_branch("dev/11-1-26_4200_鸿蒙"))
        self.assertEqual("dev/11-1.5-26_4200", mainline_branch("dev/11-1.5-26_4200_鸿蒙"))
        self.assertEqual("dev/11-1-26_鸿蒙", mainline_branch("dev/11-1-26_鸿蒙_4200"))
        self.assertEqual("feature/7-1-26/S35", mainline_branch("feature/7-1-26/S35/S36"))

    def test_dotted_feature_branch_returns_mainline(self) -> None:
        """feature 分支同样支持带点日期和斜杠后缀。"""
        self.assertEqual("feature/7-1.5-26", mainline_branch("feature/7-1.5-26/S35赛季"))

    def test_plain_mainline_has_no_parent(self) -> None:
        """纯主线分支没有可合并的父分支。"""
        self.assertEqual("", mainline_branch("dev/9-2-26"))
        self.assertEqual("", mainline_branch("dev/11-1.5-26"))
        self.assertEqual("", mainline_branch("main"))

    def test_non_date_branch_has_no_parent(self) -> None:
        """非日期命名的分支不推导主线。"""
        self.assertEqual("", mainline_branch("dev/feature-x_4200"))
        self.assertEqual("", mainline_branch("release/11-1.5-26_4200"))


if __name__ == "__main__":
    unittest.main()
