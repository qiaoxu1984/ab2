"""分支版本解析与分支过滤的单元测试（标准库 unittest）。"""

import unittest
from datetime import datetime

from agent.service import AgentService


class BranchVersionTests(unittest.TestCase):
    """覆盖日期分支的版本排序键，包括日里的小数点。"""

    def test_dotted_day_keeps_hotfix_in_order(self) -> None:
        """dev/11-1.5-26 比 dev/11-1-26 新，点后的数字参与排序。"""
        self.assertEqual((26, 11, 1, 5), AgentService._branch_version("dev/11-1.5-26"))
        self.assertEqual((26, 11, 1, 0), AgentService._branch_version("dev/11-1-26"))
        self.assertLess(AgentService._branch_version("dev/11-1.5-26"), AgentService._branch_version("dev/11-2-26"))
        self.assertLess(AgentService._branch_version("dev/11-1-26"), AgentService._branch_version("dev/11-1.5-26"))

    def test_unparseable_versions_sort_last(self) -> None:
        """非日期分支排在所有正常版本之后。"""
        self.assertEqual((-1, -1, -1, -1), AgentService._branch_version("main"))


class DottedBranchFilterTests(unittest.TestCase):
    """覆盖带点日期分支在各类分支过滤下的可见性与排序。"""

    def setUp(self) -> None:
        """按当前时间构造当月分支，避免用例随月份失效。"""
        self.service = AgentService.__new__(AgentService)
        now = datetime.now()
        self.dotted = f"dev/{now.month}-1.5-{now.year % 100:02d}"
        self.plain = f"dev/{now.month}-1-{now.year % 100:02d}"

    def test_all_dev_lists_dotted_day_branch(self) -> None:
        """all_dev 必须能列出带点日期的 dev 分支。"""
        self.assertEqual([self.dotted], self.service._filter_branches([self.dotted], "all_dev"))

    def test_month_dev_lists_dotted_day_branch(self) -> None:
        """month_dev 同样接受带点日期。"""
        self.assertEqual([self.dotted], self.service._filter_branches([self.dotted], "month_dev"))

    def test_default_filter_keeps_suffixed_dotted_branch(self) -> None:
        """default 模式原样返回配置的默认分支，_4200 后缀不影响。"""
        name = f"{self.dotted}_4200"
        self.assertEqual([name], self.service._filter_branches([name], "default", name))

    def test_dotted_hotfix_sorts_before_plain(self) -> None:
        """同一天有多个后缀时，点后数字更大的分支排在前面。"""
        self.assertEqual([self.dotted, self.plain], self.service._filter_branches([self.plain, self.dotted], "all_dev"))


if __name__ == "__main__":
    unittest.main()
