"""飞书通知差异段落的单元测试（标准库 unittest）。"""

import unittest

from agent.notify import MAX_NOTIFY_CHARS, build_feishu_text


class FeishuTextTests(unittest.TestCase):
    """覆盖飞书消息差异段落和长度封顶。"""

    def test_message_without_change_data_keeps_existing_shape(self) -> None:
        text = build_feishu_text("success", "demo", "Android Dev", "dev/9-2-26", "abc123", 61)
        self.assertIn("【AB2 打包成功】", text)
        self.assertNotIn("本次重要变更", text)

    def test_message_appends_summary_and_detail(self) -> None:
        text = build_feishu_text(
            "success", "demo", "Android Release", "dev/9-2-26", "abc123", 61,
            feature_summary="1. 新增鸿蒙分支选择器",
            commit_detail="a1b2c3d 09-20 张三 添加鸿蒙分支选择器",
            commit_stat="3 files changed, 10 insertions(+)",
            commit_count=1,
        )
        self.assertIn("【本次重要变更】", text)
        self.assertIn("新增鸿蒙分支选择器", text)
        self.assertIn("【提交明细】（共 1 个提交，已排除合并提交）", text)
        self.assertIn("改动量：3 files changed, 10 insertions(+)", text)

    def test_message_reports_no_change(self) -> None:
        text = build_feishu_text(
            "success", "demo", "Android Release", "dev/9-2-26", "abc123", 61,
            change_note="本次打包无新增代码变更",
        )
        self.assertIn("本次打包无新增代码变更", text)

    def test_message_caps_detail_lines(self) -> None:
        detail = "\n".join(f"c{i} 09-20 张三 提交{i}" for i in range(50))
        text = build_feishu_text(
            "success", "demo", "Android Release", "dev/9-2-26", "abc123", 61,
            commit_detail=detail, commit_count=50,
        )
        self.assertIn("…等共 50 个提交", text)
        self.assertEqual(sum(1 for line in text.splitlines() if line.startswith("c")), 20)

    def test_message_is_capped_to_total_length(self) -> None:
        detail = "\n".join(f"c{i} " + "x" * 400 for i in range(50))
        text = build_feishu_text(
            "success", "demo", "Android Release", "dev/9-2-26", "abc123", 61,
            feature_summary="长" * 5000, commit_detail=detail, commit_count=50,
        )
        self.assertTrue(text.endswith("…（消息过长已截断）"))
        self.assertLessEqual(len(text), MAX_NOTIFY_CHARS + len("\n…（消息过长已截断）"))


if __name__ == "__main__":
    unittest.main()
