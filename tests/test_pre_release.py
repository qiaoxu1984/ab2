"""预发布打包流程的单元测试（标准库 unittest）。"""

import shutil
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from agent.executor import (
    BUILD_FROM_AB2_METHOD,
    DIFF_MANIFEST_NAME,
    PREPARE_PUBLISH_DIFF_MANIFEST_NAME,
    PREPARE_PUBLISH_METHOD,
    BuildExecutor,
)
from agent.notify import build_feishu_text
from manager.db import Database
from shared.models import BuildRequest


def make_temp_dir() -> Path:
    """Create a uniquely named temporary directory writable in restricted sandboxes."""
    directory = Path(tempfile.gettempdir()) / f"ab2-pre-release-{uuid.uuid4().hex}"
    directory.mkdir()
    return directory


class BuildMethodTests(unittest.TestCase):
    """覆盖正式打包与预发布打包选择的 Unity 入口方法。"""

    def test_normal_build_uses_channel_method(self) -> None:
        """普通打包仍使用渠道配置的 build_method。"""
        channel = {"build_method": "HLS_Editor.ExportEditor.BuildFromAB2"}
        self.assertEqual("HLS_Editor.ExportEditor.BuildFromAB2", BuildExecutor.resolve_build_method(channel, False))

    def test_pre_release_uses_prepare_publish_method(self) -> None:
        """预发布只把 Unity AB 入口换成预发布函数。"""
        channel = {"build_method": "HLS_Editor.ExportEditor.BuildFromAB2"}
        self.assertEqual(PREPARE_PUBLISH_METHOD, BuildExecutor.resolve_build_method(channel, True))
        self.assertEqual("HLS_Editor.ExportEditor.BuildPreparePublishFromAB2", PREPARE_PUBLISH_METHOD)


class DiffManifestTests(unittest.TestCase):
    """覆盖预发布差异包清单改名后的成功校验。"""

    def setUp(self) -> None:
        """为每个用例准备一个带 Diff 目录的临时工程。"""
        self.project = make_temp_dir()
        self.diff = self.project / "Bundles" / "Diff" / "3800"
        self.diff.mkdir(parents=True)
        self.executor = BuildExecutor(lambda event: None)

    def tearDown(self) -> None:
        """清理临时工程目录。"""
        shutil.rmtree(self.project, ignore_errors=True)

    def test_normal_build_requires_plain_manifest(self) -> None:
        """普通打包必须生成未改名的版本清单。"""
        (self.diff / DIFF_MANIFEST_NAME).write_text("", encoding="utf-8")
        self.executor._verify_diff_manifest(str(self.project))

    def test_pre_release_accepts_prepare_publish_manifest(self) -> None:
        """预发布生成带 _preparepublish 后缀的清单时必须判定成功。"""
        (self.diff / PREPARE_PUBLISH_DIFF_MANIFEST_NAME).write_text("", encoding="utf-8")
        self.executor._verify_diff_manifest(str(self.project), prepare_publish=True)

    def test_pre_release_rejects_plain_manifest(self) -> None:
        """预发布若仍产出普通清单，说明调用了错误的 Unity 方法，应判定失败。"""
        (self.diff / DIFF_MANIFEST_NAME).write_text("", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            self.executor._verify_diff_manifest(str(self.project), prepare_publish=True)

    def test_normal_build_rejects_prepare_publish_manifest(self) -> None:
        """普通打包不接受预发布清单，避免两种流程被混淆。"""
        (self.diff / PREPARE_PUBLISH_DIFF_MANIFEST_NAME).write_text("", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            self.executor._verify_diff_manifest(str(self.project))


class RunMethodWiringTests(unittest.TestCase):
    """覆盖 pre_release 标记真正传递到 Unity 的 executeMethod。"""

    def setUp(self) -> None:
        """准备一个最小工程目录和可记录的假 Unity 调用。"""
        self.project_path = make_temp_dir()
        (self.project_path / "Unity.exe").write_text("", encoding="utf-8")
        self.executor = BuildExecutor(lambda event: None)
        self.calls: list[str] = []

    def tearDown(self) -> None:
        """清理临时工程目录。"""
        shutil.rmtree(self.project_path, ignore_errors=True)

    def _run(self, pre_release: bool) -> list[str]:
        """跑一遍构建流水线，只记录 Unity 实际被调用的方法名。"""
        project = {
            "id": "p1",
            "path": str(self.project_path),
            "unity_path": str(self.project_path / "Unity.exe"),
            "default_branch": "main",
        }
        channel = {"switch_to": "android_release", "build_method": BUILD_FROM_AB2_METHOD, "branch_filter": "all_dev", "ab2_version": "3800"}
        task = {"task_id": "t1", "branch": "dev/9-2-26", "project_id": "p1", "channel": "android_release", "pre_release": pre_release}
        with patch("agent.executor.close_unity_for_project", return_value=[]), \
             patch("agent.executor.sync_branch", return_value="abc123"), \
             patch.object(self.executor, "_clear_build_caches"), \
             patch.object(self.executor, "_prepare_diff_output"), \
             patch.object(self.executor, "_wait_before_next_stage"), \
             patch.object(self.executor, "_invoke_unity", side_effect=lambda *args, **kwargs: self.calls.append(args[2])), \
             patch.object(self.executor, "_submit_release_ticket"), \
             patch.object(self.executor, "_start_failure_analysis"):
            self.executor.run(task, project, channel)
        return self.calls

    def test_pre_release_task_calls_prepare_publish_method(self) -> None:
        """带 pre_release 的任务必须调用预发布入口，而不是正式打包入口。"""
        calls = self._run(True)
        self.assertIn(PREPARE_PUBLISH_METHOD, calls)
        self.assertNotIn(BUILD_FROM_AB2_METHOD, calls)

    def test_normal_task_calls_channel_method(self) -> None:
        """普通任务保持调用渠道配置的打包方法。"""
        calls = self._run(False)
        self.assertIn(BUILD_FROM_AB2_METHOD, calls)
        self.assertNotIn(PREPARE_PUBLISH_METHOD, calls)


class TaskFlagPersistenceTests(unittest.TestCase):
    """覆盖 pre_release 标记在任务创建和查询时的持久化。"""

    def setUp(self) -> None:
        """为每个用例创建独立临时数据库。"""
        self.temp = make_temp_dir()
        self.db = Database(str(self.temp / "test.sqlite3"))
        self.request = {"agent_id": "agent-1", "project_id": "proj", "channel": "android_release", "branch": "dev/9-2-26"}

    def tearDown(self) -> None:
        """关闭连接并清理临时目录。"""
        self.db.connection.close()
        shutil.rmtree(self.temp, ignore_errors=True)

    def test_pre_release_flag_round_trip(self) -> None:
        """预发布任务写入后必须回传布尔 True。"""
        task_id = self.db.create_task({**self.request, "pre_release": True})
        task = self.db.get_task(task_id)
        self.assertIs(True, task["pre_release"])
        self.assertIs(True, self.db.latest_task("agent-1", "proj", "android_release")["pre_release"])

    def test_missing_flag_defaults_to_false(self) -> None:
        """老请求没有该字段时按普通打包处理。"""
        task_id = self.db.create_task(dict(self.request))
        self.assertIs(False, self.db.get_task(task_id)["pre_release"])


class BuildRequestModelTests(unittest.TestCase):
    """覆盖 Manager 请求模型对预发布标记的解析。"""

    def test_default_is_normal_build(self) -> None:
        """未传字段时默认普通打包。"""
        request = BuildRequest(agent_id="a", project_id="p", channel="c", branch="b")
        self.assertFalse(request.pre_release)

    def test_accepts_pre_release_flag(self) -> None:
        """显式传入时解析为 True。"""
        request = BuildRequest(agent_id="a", project_id="p", channel="c", branch="b", pre_release=True)
        self.assertTrue(request.pre_release)


class FeishuPreReleaseTextTests(unittest.TestCase):
    """覆盖飞书通知对预发布结果的区分。"""

    def test_pre_release_title(self) -> None:
        """预发布成功通知标题必须带“预发布”。"""
        text = build_feishu_text("success", "demo", "android_release", "dev/9-2-26", "abc123", 61, pre_release=True)
        self.assertIn("【AB2 预发布打包成功】", text)

    def test_normal_title_unchanged(self) -> None:
        """普通打包通知标题保持原样。"""
        text = build_feishu_text("success", "demo", "android_release", "dev/9-2-26", "abc123", 61)
        self.assertIn("【AB2 打包成功】", text)


if __name__ == "__main__":
    unittest.main()
