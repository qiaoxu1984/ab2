"""飞书通知文本的单元测试（标准库 unittest）。"""

import unittest

from agent.notify import MAX_NOTIFY_CHARS, build_feishu_text


class FeishuTextTests(unittest.TestCase):
    """覆盖飞书消息的基本结构、差异链接和长度封顶。"""

    def test_message_without_report_omits_change_lines(self) -> None:
        text = build_feishu_text("success", "demo", "Android Dev", "dev/9-2-26", "abc123", 61)
        self.assertIn("【AB2 打包成功】", text)
        self.assertNotIn("本次重要变更", text)
        self.assertNotIn("提交明细", text)
        self.assertNotIn("差异详情：", text)

    def test_message_includes_report_link(self) -> None:
        text = build_feishu_text(
            "success", "demo", "Android Release", "dev/9-2-26", "abc123", 61,
            report_url="http://172.18.67.71:8000/reports/abc123-diff.html",
        )
        self.assertIn("差异详情：http://172.18.67.71:8000/reports/abc123-diff.html", text)

    def test_message_is_capped_to_total_length(self) -> None:
        text = build_feishu_text(
            "success", "demo", "Android Release", "dev/9-2-26", "abc123", 61,
            manager_url="http://x/" + "a" * 5000,
        )
        self.assertTrue(text.endswith("…（消息过长已截断）"))
        self.assertLessEqual(len(text), MAX_NOTIFY_CHARS + len("\n…（消息过长已截断）"))


if __name__ == "__main__":
    unittest.main()
