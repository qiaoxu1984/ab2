"""正式包自动建单开关与提单人的单元测试（标准库 unittest）。"""

import json
import shutil
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from agent.config import AgentConfig
from agent.executor import BuildExecutor
from agent.ticket import current_submitter, submit_ticket


def make_temp_dir() -> Path:
    """Create a uniquely named temporary directory writable in restricted sandboxes.

    tempfile.mkdtemp uses mode 0o700, which strips inherited write permissions on
    Windows sandboxes; a plain mkdir keeps the inherited permissions intact.
    """
    directory = Path(tempfile.gettempdir()) / f"ab2-ticket-{uuid.uuid4().hex}"
    directory.mkdir()
    return directory


class TicketCommandTests(unittest.TestCase):
    """覆盖提单人参数的选择：项目配置优先，未配置回退当前系统用户名。"""

    def _run(self, submitter: str):
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="created", stderr="")
        with patch("agent.ticket.binary_path", return_value=Path("autoApproval")), patch("agent.ticket.subprocess.run", return_value=completed) as run:
            ok, summary = submit_ticket("game_1.0.zip", submitter)
        self.assertTrue(ok)
        self.assertEqual("created", summary)
        return run.call_args.args[0]

    def test_configured_submitter_is_used(self) -> None:
        """项目里配置了提单人时必须使用配置值，而不是系统用户名。"""
        command = self._run("alice")
        self.assertEqual("alice", command[command.index("-u") + 1])

    def test_blank_submitter_falls_back_to_system_user(self) -> None:
        """提单人为空时回退到当前系统用户名。"""
        with patch("agent.ticket.current_submitter", return_value="sysuser"):
            command = self._run("  ")
        self.assertEqual("sysuser", command[command.index("-u") + 1])

    def test_current_submitter_is_never_empty(self) -> None:
        """默认提单人必须有值，避免工单工具收到空用户名。"""
        self.assertTrue(current_submitter().strip())


class ReleaseTicketSwitchTests(unittest.TestCase):
    """覆盖项目级开关对正式渠道建单行为的控制。"""

    def setUp(self) -> None:
        """准备事件收集器和一份含 SVN 包名的构建日志。"""
        self.events: list[dict] = []
        self.executor = BuildExecutor(self.events.append)
        self.temp = make_temp_dir()
        self.log_path = self.temp / "build.log"
        self.log_path.write_text("SVN commit succeeded: D:/pkg/game_1.0.zip\n", encoding="utf-8")
        self.project = {"id": "p1", "log_path": str(self.log_path)}
        self.channel = {"switch_to": "android_release"}

    def tearDown(self) -> None:
        """清理临时日志目录。"""
        shutil.rmtree(self.temp, ignore_errors=True)

    def test_switch_off_skips_ticket(self) -> None:
        """关闭开关后正式渠道不再调用工单工具。"""
        self.project["auto_ticket"] = False
        with patch("agent.executor.submit_ticket") as submit:
            self.executor._submit_release_ticket("t1", self.project, self.channel, [0])
        submit.assert_not_called()
        self.assertTrue(any("跳过" in str(event.get("message", "")) for event in self.events))

    def test_switch_on_passes_project_submitter(self) -> None:
        """开关打开时使用日志中的包名和项目配置的提单人建单。"""
        self.project["auto_ticket"] = True
        self.project["ticket_submitter"] = "alice"
        with patch("agent.executor.submit_ticket", return_value=(True, "ok")) as submit:
            self.executor._submit_release_ticket("t2", self.project, self.channel, [0])
        submit.assert_called_once_with("game_1.0.zip", "alice")

    def test_missing_switch_defaults_to_on(self) -> None:
        """旧配置没有该字段时保持原有行为：正式渠道自动建单。"""
        with patch("agent.executor.submit_ticket", return_value=(True, "ok")) as submit:
            self.executor._submit_release_ticket("t3", self.project, self.channel, [0])
        submit.assert_called_once()

    def test_dev_channel_still_skips_ticket(self) -> None:
        """开发渠道即使开关打开也不建单。"""
        self.project["auto_ticket"] = True
        with patch("agent.executor.submit_ticket") as submit:
            self.executor._submit_release_ticket("t4", self.project, {"switch_to": "android_dev"}, [0])
        submit.assert_not_called()


class ProjectTicketConfigTests(unittest.TestCase):
    """覆盖 Agent 配置保存时对开关和提单人的校验。"""

    def setUp(self) -> None:
        """每个用例使用独立的临时配置文件。"""
        self.temp = make_temp_dir()
        self.config = AgentConfig(str(self.temp / "agent.json"))
        self.base = {"id": "p1", "name": "demo", "path": str(self.temp / "project"), "unity_path": "Unity", "log_path": "log", "channels": []}

    def tearDown(self) -> None:
        """清理临时目录。"""
        shutil.rmtree(self.temp, ignore_errors=True)

    def test_switch_and_submitter_are_saved(self) -> None:
        """合法的布尔开关和提单人字符串会写入配置文件。"""
        self.config.save_project({**self.base, "auto_ticket": False, "ticket_submitter": "alice"})
        saved = json.loads(Path(self.config.path).read_text(encoding="utf-8"))["projects"][0]
        self.assertFalse(saved["auto_ticket"])
        self.assertEqual("alice", saved["ticket_submitter"])

    def test_non_boolean_switch_is_rejected(self) -> None:
        """开关必须是真正的布尔值，避免 "false" 字符串被当成真值。"""
        with self.assertRaises(ValueError):
            self.config.save_project({**self.base, "auto_ticket": "false"})

    def test_non_string_submitter_is_rejected(self) -> None:
        """提单人必须是字符串。"""
        with self.assertRaises(ValueError):
            self.config.save_project({**self.base, "ticket_submitter": 123})


if __name__ == "__main__":
    unittest.main()
