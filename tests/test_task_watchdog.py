"""Manager 孤儿任务看门狗的单元测试（标准库 unittest）。"""

import tempfile
import time
import unittest
from pathlib import Path

from manager.db import Database


class StaleTaskTests(unittest.TestCase):
    """覆盖 last_event_at 记录与 stale_tasks 超时筛选。"""

    def setUp(self) -> None:
        """为每个用例创建独立临时数据库，避免相互污染。"""
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self.temp_dir.name) / "test.sqlite3"))

    def tearDown(self) -> None:
        """关闭连接并清理临时目录。"""
        self.db.connection.close()
        self.temp_dir.cleanup()

    def _create_task(self) -> str:
        """创建一个处于排队状态的测试任务。"""
        return self.db.create_task({
            "agent_id": "agent-1",
            "project_id": "proj",
            "channel": "android_release",
            "branch": "dev/9-2-26",
        })

    def test_new_queued_task_is_not_stale(self) -> None:
        """刚创建的任务即使还没有事件也不能被判为孤儿。"""
        self._create_task()
        self.assertEqual([], self.db.stale_tasks(time.time() - 60))

    def test_running_task_without_recent_events_is_stale(self) -> None:
        """长时间没有事件的任务应被看门狗筛选出来。"""
        task_id = self._create_task()
        self.db.apply_event({"kind": "status", "task_id": task_id, "stage": "ab", "status": "running", "message": "execute"})
        # 事件刚刚上报，按正常阈值不应命中。
        self.assertEqual([], self.db.stale_tasks(time.time() - 60))
        # 用未来的截止时间模拟超时，任务应被命中并保留原阶段。
        stale = self.db.stale_tasks(time.time() + 1)
        self.assertEqual([task_id], [task["id"] for task in stale])
        self.assertEqual("ab", stale[0]["stage"])

    def test_finished_task_is_never_stale(self) -> None:
        """已结束的任务不再参与孤儿检查。"""
        task_id = self._create_task()
        self.db.apply_event({"kind": "status", "task_id": task_id, "stage": "ab", "status": "success", "message": "build complete"})
        self.assertEqual([], self.db.stale_tasks(time.time() + 10_000))

    def test_cleared_task_is_never_stale(self) -> None:
        """被界面清除的任务不再参与孤儿检查。"""
        task_id = self._create_task()
        self.db.clear_task(task_id)
        self.assertEqual([], self.db.stale_tasks(time.time() + 10_000))


if __name__ == "__main__":
    unittest.main()
