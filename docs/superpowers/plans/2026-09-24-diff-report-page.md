# 差异详情页实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 release 渠道打包生成 Manager 托管的差异详情页，并把链接附到飞书通知。

**Architecture:** Agent 概括线程生成 HTML 并发 `diff_report` 事件；Manager 落库 `diff_reports` 表并立即物化 `data/reports/<task_id>-diff.html`；飞书通知由 service 拼接 Manager URL。

**Tech Stack:** Python 3、标准库 unittest、FastAPI、SQLite。

**版本控制说明：** 用户未要求提交，执行时只落盘代码和测试，不执行 git commit。

---

### Task 1: 页面渲染器（shared/report.py）

**Files:**
- Modify: `shared/report.py`
- Test: `tests/test_diff_report.py`（新建）

- [ ] **Step 1: 写失败测试**

创建 `tests/test_diff_report.py`：

```python
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
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m unittest discover -s tests -t . -v`
Expected: ERROR，提示 `ImportError: cannot import name 'build_diff_report'`

- [ ] **Step 3: 实现渲染器**

`shared/report.py` 顶部导入改为：

```python
import html
import time
```

文件末尾追加：

```python
def build_diff_report(project_name: str, channel: str, branch: str, baseline_sha: str, commit_sha: str,
                      summary: str, detail: str, stat: str = "", note: str = "", count: int = 0) -> str:
    """Render the per-build change-notes page linked from the Feishu notification."""
    generated = time.strftime("%Y-%m-%d %H:%M:%S")
    # 所有外部内容都转义，避免提交信息里的尖括号破坏页面结构。
    summary_html = html.escape(summary or "（未生成 AI 概括）")
    detail_html = html.escape(detail or "（无提交明细）")
    stat_html = f"<p class='stat'>改动量：{html.escape(stat)}</p>" if stat else ""
    note_html = f"<p class='note'>{html.escape(note)}</p>" if note else ""
    baseline_short = html.escape((baseline_sha or "未知")[:10])
    commit_short = html.escape((commit_sha or "未知")[:10])
    return (
        "<!doctype html><meta charset='utf-8'>"
        "<title>AB2 差异说明</title>"
        "<style>body{background:#111827;color:#dbeafe;font:15px system-ui;padding:32px;line-height:1.7;max-width:960px;margin:0 auto}"
        "pre{white-space:pre-wrap}h1{color:#38bdf8}h2{color:#e5e7eb;font-size:16px;margin-top:24px}"
        ".meta{color:#9ca3af;font-size:13px}.stat{color:#9ca3af}.note{color:#fbbf24}</style>"
        "<h1>AB2 差异说明</h1>"
        f"<p class='meta'>工程：{html.escape(project_name)}　渠道：{html.escape(channel)}　分支：{html.escape(branch)}<br>"
        f"上次打包：{baseline_short} → 本次：{commit_short}<br>生成时间：{generated}</p>"
        f"<h2>本次重要变更</h2><pre>{summary_html}</pre>"
        f"<h2>提交明细（共 {count} 个提交，已排除合并提交）</h2>"
        f"<pre>{detail_html}</pre>"
        f"{stat_html}{note_html}"
    )
```

- [ ] **Step 4: 运行测试**

Run: `python -m unittest discover -s tests -t . -v`
Expected: 差异页渲染与飞书链接用例 PASS；落库用例仍失败（Task 3 实现）

---

### Task 2: Agent 上报页面与飞书链接

**Files:**
- Modify: `agent/executor.py`、`agent/service.py`、`agent/notify.py`

- [ ] **Step 1: executor 增加 task/channel 参数并生成页面**

导入行改为：

```python
from shared.report import build_ai_report, build_diff_report
```

`run()` 中调用点改为：

```python
    if track_changes:
        try:
            self._collect_changes(task_id, project, task, channel, sequence, baseline_branch, baseline_sha)
        except Exception as error:
            # 差异说明属于附加信息，任何异常都不能影响构建结果。
            self._event(task_id, "log", sequence, stage="git", message=f"差异说明采集异常：{error}")
```

`_collect_changes` 签名和线程启动改为：

```python
def _collect_changes(self, task_id: str, project: dict[str, Any], task: dict[str, Any], channel: dict[str, Any], sequence: list[int], baseline_branch: str, baseline_sha: str) -> None:
```

（函数体末尾替换为：）

```python
    # 差异详情页上下文：AI 概括线程据此生成可分享的 HTML 页面。
    context = {
        "project_name": project.get("name") or project["id"],
        "channel": channel.get("name", ""),
        "branch": task.get("branch", ""),
        "baseline_sha": baseline_sha,
        "commit_sha": new_sha,
        "note": note,
    }
    thread = threading.Thread(target=self._summarize_changes, args=(task_id, project, context, detail, stat, total, sequence), name=f"ab2-changes-{task_id}", daemon=True)
    with self.change_lock:
        self.change_threads[task_id] = thread
    thread.start()
```

`_summarize_changes` 签名改为：

```python
def _summarize_changes(self, task_id: str, project: dict[str, Any], context: dict[str, Any], detail: str, stat: str, total: int, sequence: list[int]) -> None:
```

`finally` 块改为：

```python
finally:
    # 摘要先上报再摘除登记，保证等待方 join 后一定已经拿到事件。
    self._event(task_id, "diff_summary", sequence, summary=summary)
    # 差异详情页随通知一起提供，链接由 Agent 服务层拼成。
    report_html = build_diff_report(context["project_name"], context["channel"], context["branch"], context["baseline_sha"], context["commit_sha"], summary, detail, stat, context["note"], total)
    self._event(task_id, "diff_report", sequence, html=report_html)
    with self.change_lock:
        if self.change_threads.get(task_id) is threading.current_thread():
            self.change_threads.pop(task_id, None)
```

- [ ] **Step 2: service 捕获事件并拼链接**

`_consume_events` 的 change 初始化改为：

```python
    change: dict[str, Any] = {"raw": "", "stat": "", "count": 0, "note": "", "summary": "", "report": False}
```

事件分支追加：

```python
        elif event.get("kind") == "diff_report":
            change["report"] = True
```

两个调用点改为：

```python
await self._notify(project, channel, task.get("branch", ""), commit_sha, status, message, started, change, task["task_id"])
```

（`_run_scheduled` 与 `_start_task` 各一处。）

`_notify` 签名与 URL 拼接改为：

```python
async def _notify(self, project: dict[str, Any], channel: dict[str, Any], branch: str, commit_sha: str,
                  status: str, message: str, started: float, change: dict[str, Any] | None = None,
                  task_id: str = "") -> None:
    """Send the Feishu notification for a finished build when the channel enables it."""
    webhook = str(self.config.data.get("feishu_webhook", "")).strip()
    if status not in ("success", "failed") or not channel.get("notify_feishu") or not webhook:
        return
    change = change or {}
    # 差异详情页由 Manager 托管，只有收到页面事件时才把链接放进通知。
    report_url = f"{self._manager_http_url()}reports/{task_id}-diff.html" if change.get("report") and task_id else ""
    text = build_feishu_text(status, project.get("name") or project["id"], channel.get("name", ""), branch,
                             commit_sha, time.time() - started, message, self._manager_http_url(),
                             feature_summary=change.get("summary", ""), commit_detail=change.get("raw", ""),
                             commit_stat=change.get("stat", ""), commit_count=change.get("count", 0),
                             change_note=change.get("note", ""), report_url=report_url)
```

- [ ] **Step 3: notify 输出链接行**

`build_feishu_text` 签名追加参数：

```python
                      commit_count: int = 0, change_note: str = "", report_url: str = "") -> str:
```

差异段落后追加：

```python
    # 差异详情页链接指向 Manager 托管的静态页面。
    if report_url:
        lines.append(f"差异详情：{report_url}")
```

- [ ] **Step 4: 静态检查**

Run: `python -m compileall agent shared`
Expected: 无错误

---

### Task 3: Manager 落库与物化页面

**Files:**
- Modify: `manager/db.py`、`manager/main.py`

- [ ] **Step 1: db 建表与事件处理**

`_create_schema` 的 `executescript` 中 `reports` 表定义后追加：

```sql
                CREATE TABLE IF NOT EXISTS diff_reports (
                    task_id TEXT PRIMARY KEY, html TEXT NOT NULL, created_at REAL NOT NULL
                );
```

`apply_event` 的 `report` 分支后追加：

```python
            if event["kind"] == "diff_report":
                # 差异详情页独立存储，避免覆盖同一任务的 AI 分析报告。
                self.connection.execute("INSERT OR REPLACE INTO diff_reports(task_id,html,created_at) VALUES(?,?,?)", (event["task_id"], event.get("html", ""), now))
```

- [ ] **Step 2: main 物化页面文件**

在 `write` 前定义帮助函数（`AgentConnection` 类之前）：

```python
def write_diff_report(event: dict[str, Any]) -> None:
    """Materialize a change-notes page so the Feishu link opens immediately."""
    report_dir = Path(os.getenv("AB2_DATA_DIR", "data")) / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    # 任务号由 Agent 生成（十六进制），Path.name 再次确保不越出报告目录。
    file_name = Path(f"{event.get('task_id', '')}-diff.html").name
    (report_dir / file_name).write_text(event.get("html", ""), encoding="utf-8")
```

WebSocket 事件分支改为：

```python
            elif data.get("type") == "event":
                event = data["event"]
                state.db.apply_event(event)
                if event.get("kind") == "diff_report":
                    write_diff_report(event)
```

- [ ] **Step 3: 静态检查与测试**

Run: `python -m compileall manager shared agent; python -m unittest discover -s tests -t . -v`
Expected: 全部用例 PASS

---

### Task 4: 全量验证与端到端冒烟

- [ ] **Step 1: 单元测试与导入冒烟**

Run: `python -m unittest discover -s tests -t . -v; python -c "import manager.main, agent.service; print('ok')"`
Expected: 测试全 PASS，输出 `ok`

- [ ] **Step 2: 端到端冒烟（临时仓库，模拟无 OpenCode）**

复用 `C:\Users\qiaoxu\AppData\Local\Temp\opencode\ab2_smoke_changes.py` 扩展：采集后事件顺序为 `diff → log → diff_summary → diff_report`，`change["report"]` 为 True，飞书文本包含 `差异详情：`；再调用 `manager.main.write_diff_report`（把 `AB2_DATA_DIR` 指到临时目录）验证文件落盘且内容含页面标题。

- [ ] **Step 3: 手动验证清单**

1. 重启 Manager 和 Agent（各一次）。
2. release 渠道打包，飞书消息出现"差异详情"链接，群内其他机器可打开，页面含 AI 概括与完整提交明细。
3. 无新增提交的打包不出现链接。
