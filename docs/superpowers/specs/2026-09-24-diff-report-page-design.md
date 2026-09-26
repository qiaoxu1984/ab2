# 差异详情页设计规格

## 1. 目标与范围

为打包差异说明增加一个可分享的网页：release 渠道打包后，飞书通知里附带该页链接，群内任何人都能打开查看 AI 功能概括和完整提交明细。

范围：

- 触发条件与差异说明一致：release 渠道 + `notify_feishu` 开启 + 确有新增提交（无变更、无基准不建页、不加链接）。
- 页面由 Manager 托管，每个任务一份，URL 形如 `http://<manager>:8000/reports/<task_id>-diff.html`，历史长期保留。
- 页面内容：工程、渠道、分支、上次/本次 commit、生成时间、AI 功能概括、完整提交明细（不受飞书 20 行限制）、改动量、基准提示。
- AI 概括不可用时页面仍生成，只展示提交明细。

非目标：Manager 页面新增"查看差异"按钮、页面自动清理、每任务之外的汇总页、文件级 `--stat` 明细。

## 2. 数据流

1. Agent 差异概括线程结束时生成 HTML，依次上报 `diff_summary` 和 `diff_report` 事件；事件顺序在 `analysis_done` 之前，保证飞书通知发出时 Manager 已收到页面。
2. Manager 收到 `diff_report` 事件后：写入 `diff_reports` 表，并立即把 HTML 落到 `data/reports/<task_id>-diff.html`，使链接即时可用；现有 `/reports/{name}` 路由直接服务该文件。
3. Agent 服务层在发送飞书通知时，若本次收集到了 `diff_report` 事件，就在消息中追加一行 `差异详情：<manager>/reports/<task_id>-diff.html`。
4. Manager 离线期间事件被丢弃（沿用现有语义），页面和链接一起缺失，不影响构建。

## 3. 组件改动

- `shared/report.py`：新增 `build_diff_report(...)`，深色风格与 `build_ai_report` 一致，所有外部内容 HTML 转义。
- `agent/executor.py`：`_collect_changes` 增加 `task`、`channel` 参数并构造页面上下文；概括线程在 `finally` 中先发 `diff_summary` 再发 `diff_report`（带 HTML）。
- `agent/service.py`：`_consume_events` 记录是否收到 `diff_report`；`_notify` 增加 `task_id` 参数并拼出页面 URL。
- `agent/notify.py`：`build_feishu_text` 增加可选 `report_url`，非空时追加"差异详情"行。
- `manager/db.py`：新增 `diff_reports(task_id 主键, html, created_at)` 表和 `diff_report` 事件落库。
- `manager/main.py`：新增 `write_diff_report(event)` 帮助函数，事件处理时立即物化页面文件。

## 4. 错误与边界

- 页面 HTML 生成异常：概括线程里捕获并记录日志，摘要照发，飞书无链接；不影响构建。
- Manager 写文件失败（磁盘权限等）：只影响页面可访问性，任务状态不受影响。
- 任务号由 Agent 生成（十六进制），文件名再经 `Path.name` 兜底，不越出报告目录。

## 5. 验证策略

- 单元测试：`build_diff_report` 转义与分节、飞书 `report_url` 行、`Database.apply_event` 落库。
- 端到端冒烟：临时仓库 + 模拟无 OpenCode，校验 `diff_summary → diff_report` 顺序、`_consume_events` 捕获、飞书文本含链接、Manager 物化文件可读。
- 手动验证：重启 Manager 和 Agent 后，release 渠道打包，飞书链接在群内可打开。

## 6. 涉及文件

- `shared/report.py`、`agent/executor.py`、`agent/service.py`、`agent/notify.py`、`manager/db.py`、`manager/main.py`
- `tests/test_diff_report.py`（新增）
