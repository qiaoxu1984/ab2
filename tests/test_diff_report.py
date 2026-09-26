"""差异详情页与飞书链接的单元测试（标准库 unittest）。"""

import unittest

from agent.notify import build_feishu_text
from manager.db import Database
from shared.report import build_diff_report


class DiffReportRenderTests(unittest.TestCase):
    """覆盖差异页分节、计数和 HTML 转义。"""

    def test_report_contains_sections_and_escapes_html(self) -> None:
        page = build_diff_report(
            "demo", "Android Release", "dev/9-2-26", "aaaa1111", "bbbb2222",
            "1. <script>alert(1)</script>", "abc <b>bold</b>", "3 files changed, 10 insertions(+)", "提示 & 备注", 3,
        )
        self.assertIn("AB2 差异说明", page)
        self.assertIn("共 3 个提交", page)
        self.assertIn("&lt;script&gt;", page)
        self.assertNotIn("<script>", page)
        self.assertIn("&lt;b&gt;bold&lt;/b&gt;", page)
        self.assertIn("提示 &amp; 备注", page)

    def test_report_marks_missing_summary(self) -> None:
        page = build_diff_report("demo", "ch", "dev/9-2-26", "", "", "", "c1 09-20 张三 提交")
        self.assertIn("（未生成 AI 概括）", page)


class FeishuReportLinkTests(unittest.TestCase):
    """覆盖飞书消息中的差异详情链接。"""

    def test_message_includes_report_url(self) -> None:
        text = build_feishu_text(
            "success", "demo", "Android Release", "dev/9-2-26", "abc", 1.0,
            report_url="http://172.18.67.71:8000/reports/abc123-diff.html",
        )
        self.assertIn("差异详情：http://172.18.67.71:8000/reports/abc123-diff.html", text)

    def test_message_without_report_url_omits_link(self) -> None:
        text = build_feishu_text("success", "demo", "Android Release", "dev/9-2-26", "abc", 1.0)
        self.assertNotIn("差异详情：", text)


class DiffReportStoreTests(unittest.TestCase):
    """覆盖 Manager 差异页落库。"""

    def test_apply_event_stores_diff_report(self) -> None:
        db = Database(":memory:")
        try:
            db.apply_event({"kind": "diff_report", "task_id": "t1", "sequence": 1, "html": "<h1>差异</h1>"})
            row = db.connection.execute("SELECT html FROM diff_reports WHERE task_id=?", ("t1",)).fetchone()
            self.assertEqual(row["html"], "<h1>差异</h1>")
        finally:
            db.connection.close()


if __name__ == "__main__":
    unittest.main()
