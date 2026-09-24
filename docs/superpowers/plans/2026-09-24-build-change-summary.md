# 打包差异说明（飞书）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** release 渠道打包时把"自上次打包以来的提交明细 + AI 功能概括"随飞书通知发出，merge 提交不参与。

**Architecture:** git 阶段记录同步前基准，同步后用 `git log --no-merges` + `git diff --shortstat` 采集明细并通过 `diff` 事件立即上报；后台线程调用 OpenCode 生成功能概括，通过 `diff_summary` 事件补发；分析线程在 `analysis_done` 前有界等待概括线程；service 汇总两个事件后拼进飞书文本。Manager 对未知事件类型天然忽略，无需改动。

**Tech Stack:** Python 3、标准库 unittest、OpenCode CLI（沿用现有失败分析调用方式）、飞书自定义机器人 webhook。

**版本控制说明：** 用户未要求提交，执行时只落盘代码和测试，不执行 git commit。

---

### Task 1: Git 采集函数（agent/git.py）

**Files:**
- Modify: `agent/git.py`（文件末尾追加）
- Test: `tests/test_git_changes.py`（新建）

- [ ] **Step 1: 写失败测试**

创建 `tests/test_git_changes.py`：

```python
"""Git 差异采集的单元测试（标准库 unittest）。"""

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent.git import current_revision, is_ancestor, revision_changes


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


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m unittest discover -s tests -t . -v`
Expected: ERROR，提示 `ImportError: cannot import name 'current_revision'`

- [ ] **Step 3: 实现 git.py 采集函数**

在 `agent/git.py` 顶部常量区（`MAINLINE_PATTERN` 之后）追加：

```python
# 差异说明的采集上限：明细最多保留最新 80 条、总长不超过 6000 字符，避免飞书消息和 AI 提示词过长。
MAX_CHANGE_COMMITS = 80
MAX_CHANGE_CHARS = 6000
```

在文件末尾追加：

```python
def current_revision(project_path: str) -> tuple[str, str]:
    """Return the checked-out branch and HEAD commit, or empty strings when Git cannot answer."""
    code, branch = run_git(project_path, "branch", "--show-current")
    if code or not branch:
        return "", ""
    code, sha = run_git(project_path, "rev-parse", "HEAD")
    if code or not sha:
        return "", ""
    return branch, sha


def is_ancestor(project_path: str, old_sha: str, new_sha: str) -> bool:
    """Check whether the baseline commit is still an ancestor of the new revision."""
    code, _ = run_git(project_path, "merge-base", "--is-ancestor", old_sha, new_sha)
    return code == 0


def revision_changes(project_path: str, old_sha: str, new_sha: str) -> tuple[str, str, int]:
    """Collect non-merge commits and the diffstat between two revisions for release notes.

    Returns the newest-first commit detail text, the shortstat line, and the untruncated count.
    """
    code, output = run_git(project_path, "log", "--no-merges", "--date=format:%m-%d", "--pretty=format:%h %ad %an %s", f"{old_sha}..{new_sha}")
    if code:
        raise RuntimeError(f"cannot list revision commits: {output}")
    commits = [line.strip() for line in output.splitlines() if line.strip()]
    total = len(commits)
    # 只保留最新的一批提交，防止极端情况下提示词和飞书消息过长。
    detail = "\n".join(commits[:MAX_CHANGE_COMMITS])
    if len(detail) > MAX_CHANGE_CHARS:
        detail = detail[:MAX_CHANGE_CHARS] + "\n…"
    code, stat = run_git(project_path, "diff", "--shortstat", old_sha, new_sha)
    return detail, (stat.strip() if code == 0 else ""), total
```

- [ ] **Step 4: 运行测试**

Run: `python -m unittest discover -s tests -t . -v`
Expected: Git 组 4 个用例 PASS

---

### Task 2: 飞书差异段落（agent/notify.py）

**Files:**
- Modify: `agent/notify.py`
- Test: `tests/test_feishu_text.py`（新建）

- [ ] **Step 1: 写失败测试**

创建 `tests/test_feishu_text.py`：

```python
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

    def test_message_caps_detail_lines_and_total_length(self) -> None:
        detail = "\n".join(f"c{i} 09-20 张三 提交{i}" for i in range(50))
        text = build_feishu_text(
            "success", "demo", "Android Release", "dev/9-2-26", "abc123", 61,
            feature_summary="长" * 5000, commit_detail=detail, commit_count=50,
        )
        self.assertIn("…等共 50 个提交", text)
        self.assertTrue(text.endswith("…（消息过长已截断）"))
        self.assertLessEqual(len(text), MAX_NOTIFY_CHARS + len("\n…（消息过长已截断）"))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m unittest discover -s tests -t . -v`
Expected: ERROR，提示 `ImportError: cannot import name 'MAX_NOTIFY_CHARS'`

- [ ] **Step 3: 实现差异段落和长度封顶**

在 `agent/notify.py` 顶部（`import` 之后、`format_duration` 之前）追加常量：

```python
# 飞书自定义机器人文本消息有长度上限，超出可能被拒绝，这里做保守封顶。
MAX_NOTIFY_CHARS = 4000
MAX_SUMMARY_CHARS = 1000
MAX_DETAIL_LINES = 20
TRUNCATED_SUFFIX = "\n…（消息过长已截断）"
```

替换 `build_feishu_text` 为：

```python
def build_feishu_text(status: str, project_name: str, channel: str, branch: str, commit_sha: str,
                      duration: float, error_message: str = "", manager_url: str = "",
                      feature_summary: str = "", commit_detail: str = "", commit_stat: str = "",
                      commit_count: int = 0, change_note: str = "") -> str:
    """Compose the build result text sent to the Feishu bot."""
    succeeded = status == "success"
    lines = [
        f"【AB2 打包{'成功' if succeeded else '失败'}】",
        f"工程：{project_name}",
        f"渠道：{channel}",
        f"分支：{branch}",
        f"Commit：{commit_sha or '-'}",
        f"耗时：{format_duration(duration)}",
        f"时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
    ]
    # Failure notifications carry a short reason so the group sees the cause immediately.
    if not succeeded and error_message:
        lines.append(f"原因：{error_message[:200]}")
    # 追加"自上次打包以来"的差异说明，让飞书群直接看到重要功能变化。
    lines.extend(_format_change_section(feature_summary, commit_detail, commit_stat, commit_count, change_note))
    if manager_url:
        lines.append(f"管理页面：{manager_url}")
    text = "\n".join(lines)
    if len(text) > MAX_NOTIFY_CHARS:
        text = text[:MAX_NOTIFY_CHARS] + TRUNCATED_SUFFIX
    return text


def _format_change_section(feature_summary: str, commit_detail: str, commit_stat: str, commit_count: int, change_note: str) -> list[str]:
    """Compose the change-notes lines appended to the Feishu message.

    没有任何差异数据（例如 dev 渠道或构建早期失败）时返回空列表，保持原消息不变。
    """
    if not (feature_summary or commit_detail or change_note):
        return []
    lines = ["【本次重要变更】"]
    if feature_summary:
        lines.append(feature_summary[:MAX_SUMMARY_CHARS])
    elif commit_detail:
        lines.append("（AI 概括不可用，以下为提交明细）")
    if change_note:
        lines.append(change_note)
    if commit_detail:
        lines.append(f"【提交明细】（共 {commit_count} 个提交，已排除合并提交）" if commit_count else "【提交明细】（已排除合并提交）")
        if commit_stat:
            lines.append(f"改动量：{commit_stat}")
        detail_lines = [line for line in commit_detail.splitlines() if line.strip()]
        lines.extend(detail_lines[:MAX_DETAIL_LINES])
        if len(detail_lines) > MAX_DETAIL_LINES:
            lines.append(f"…等共 {commit_count or len(detail_lines)} 个提交")
    return lines
```

- [ ] **Step 4: 运行全部测试**

Run: `python -m unittest discover -s tests -t . -v`
Expected: 8 个用例全部 PASS

---

### Task 3: 执行器采集与 AI 概括（agent/executor.py）

**Files:**
- Modify: `agent/executor.py`

- [ ] **Step 1: 更新导入与常量**

导入行改为：

```python
from agent.git import current_revision, is_ancestor, mainline_branch, merge_mainline, revision_changes, run_git, sync_branch
```

顶部新增 `import os`（与 `json`、`re` 等并列），并在 `class BuildExecutor` 前追加：

```python
# 差异概括的 AI 调用超时与分析线程的等待上限（秒）。
CHANGE_SUMMARY_TIMEOUT_SECONDS = 180
CHANGE_SUMMARY_JOIN_TIMEOUT_SECONDS = 200
```

- [ ] **Step 2: 注册线程状态与事件序号锁**

`__init__` 中在 `self.process_lock = threading.Lock()` 后追加：

```python
# 差异概括线程按任务登记，打包结束后的分析线程会有界等待它们。
self.change_threads: dict[str, threading.Thread] = {}
self.change_lock = threading.Lock()
# 事件序号需要跨构建线程和概括线程保持唯一。
self.sequence_lock = threading.Lock()
```

`_event` 替换为：

```python
def _event(self, task_id: str, kind: str, sequence: list[int], **data: Any) -> None:
    """Emit a monotonically numbered event for reconnect de-duplication."""
    # 概括线程与构建线程会并发上报，加锁保证序号唯一。
    with self.sequence_lock:
        sequence[0] += 1
        number = sequence[0]
    self._emit(task_id, {"task_id": task_id, "kind": kind, "sequence": number, **data})
```

- [ ] **Step 3: 在 git 阶段采集基准与差异**

`run()` 中 `self._event(task_id, "status", sequence, stage=current_stage, message="syncing branch")` 之后、`sha = sync_branch(...)` 之前插入：

```python
# release 渠道的飞书通知要带"自上次打包以来"的差异，先记录同步前基准。
track_changes = is_release_channel(channel) and bool(channel.get("notify_feishu"))
baseline_sha = ""
baseline_branch = ""
if track_changes:
    baseline_branch, baseline_sha = current_revision(project["path"])
    if baseline_branch != task["branch"]:
        # 基准不属于本次分支（首次打包或刚切分支），无法比较，只保留分支名用于提示。
        baseline_sha = ""
```

`if channel.get("sync_mainline"): self._merge_mainline(...)` 块之后插入：

```python
if track_changes:
    self._collect_changes(task_id, project, sequence, baseline_branch, baseline_sha)
```

在 `_merge_mainline` 方法前追加 `_collect_changes`、`_summarize_changes`、`_await_change_summary` 三个方法：

```python
def _collect_changes(self, task_id: str, project: dict[str, Any], sequence: list[int], baseline_branch: str, baseline_sha: str) -> None:
    """Report commits since the previous build and start the AI feature summary in background.

    原始明细先立即上报，AI 概括在线程中补发，避免阻塞构建。
    """
    if not baseline_sha:
        self._event(task_id, "diff", sequence, raw="", stat="", count=0, note=f"首次打包，无对比基准（上次分支：{baseline_branch or '未知'}）")
        self._event(task_id, "diff_summary", sequence, summary="")
        return
    code, new_sha = run_git(project["path"], "rev-parse", "HEAD")
    if code or not new_sha:
        self._event(task_id, "diff", sequence, raw="", stat="", count=0, note="无法读取当前提交，已跳过差异说明")
        self._event(task_id, "diff_summary", sequence, summary="")
        return
    if new_sha == baseline_sha:
        self._event(task_id, "diff", sequence, raw="", stat="", count=0, note="本次打包无新增代码变更")
        self._event(task_id, "diff_summary", sequence, summary="")
        return
    try:
        detail, stat, total = revision_changes(project["path"], baseline_sha, new_sha)
    except RuntimeError as error:
        self._event(task_id, "diff", sequence, raw="", stat="", count=0, note=f"差异采集失败：{error}")
        self._event(task_id, "diff_summary", sequence, summary="")
        return
    # 基准被强推或切换后可能不在当前历史中，明细仅供排查参考。
    note = "" if is_ancestor(project["path"], baseline_sha, new_sha) else "基准提交不在当前历史，明细可能包含非本次引入的提交"
    self._event(task_id, "diff", sequence, raw=detail, stat=stat, count=total, note=note)
    thread = threading.Thread(target=self._summarize_changes, args=(task_id, project, detail, stat, total, sequence), name=f"ab2-changes-{task_id}", daemon=True)
    with self.change_lock:
        self.change_threads[task_id] = thread
    thread.start()

def _summarize_changes(self, task_id: str, project: dict[str, Any], detail: str, stat: str, total: int, sequence: list[int]) -> None:
    """Ask OpenCode to turn the commit detail into feature-level bullets for the notification."""
    summary = ""
    try:
        executable = self._opencode_executable()
        if not executable:
            self._event(task_id, "log", sequence, stage="analysis", message="未找到 OpenCode CLI，已跳过差异概括")
        else:
            code_directory = Path(project["path"]) / "Assets" / "Editor" / "Main" / "BuildAPK" / "New"
            if not code_directory.is_dir():
                code_directory = Path(project["path"]) / "Assets" / "Editor"
            # 明细被截断时明确告知模型，避免它把截断当成完整列表。
            truncate_hint = f"注意：提交明细已截断，仅包含最新 {len(detail.splitlines())} 条，共 {total} 条。\n" if total > len(detail.splitlines()) else ""
            prompt = (
                "以下是某个 Unity 游戏项目本次打包相对上次打包的 Git 变更（已排除 merge 提交）：\n"
                f"{truncate_hint}{detail}\n"
                f"改动量：{stat or '未知'}\n"
                "请阅读后输出中文【本次重要功能变更】摘要，要求：3~8 条要点；合并同类改动，突出对玩家或业务可见的功能变化；"
                "忽略纯日志、注释、格式、路径调整等琐碎改动；如果确实没有重要功能变化，只输出\"无重要功能变更\"；"
                "只输出要点本身，不要前言、结语或代码块。只读分析，不要修改任何文件。"
            )
            output, exit_code, timed_out = self._run_opencode(executable, code_directory, prompt, CHANGE_SUMMARY_TIMEOUT_SECONDS)
            if timed_out:
                self._event(task_id, "log", sequence, stage="analysis", message="OpenCode 差异概括超时，已终止概括进程")
            final_text, event_count, text_count = self._parse_analysis_output(output)
            if exit_code != 0 and not timed_out:
                self._event(task_id, "log", sequence, stage="analysis", message=f"OpenCode 差异概括失败，退出码 {exit_code}")
            elif not final_text:
                self._event(task_id, "log", sequence, stage="analysis", message=f"OpenCode 未返回差异概括（事件 {event_count} 条，文本片段 {text_count} 个）")
            summary = "\n".join(text.strip() for text in final_text if text.strip())
    except Exception as error:
        self._event(task_id, "log", sequence, stage="analysis", message=f"OpenCode 差异概括失败：{error}")
    finally:
        # 摘要先上报再摘除登记，保证等待方 join 后一定已经拿到事件。
        self._event(task_id, "diff_summary", sequence, summary=summary)
        with self.change_lock:
            if self.change_threads.get(task_id) is threading.current_thread():
                self.change_threads.pop(task_id, None)

def _await_change_summary(self, task_id: str) -> None:
    """Wait for the change-summary thread so the Feishu notification sees it in order."""
    with self.change_lock:
        thread = self.change_threads.pop(task_id, None)
    if thread:
        # 线程自身先摘除登记时说明摘要事件已经上报，无需等待。
        thread.join(timeout=CHANGE_SUMMARY_JOIN_TIMEOUT_SECONDS)
```

- [ ] **Step 4: 抽取 OpenCode 定位与执行帮助方法**

在 `_parse_analysis_output` 前追加：

```python
def _opencode_executable(self) -> str:
    """Locate the OpenCode CLI and prefer its native executable on Windows."""
    executable = shutil.which("opencode.cmd") if os.name == "nt" else shutil.which("opencode")
    if not executable:
        return ""
    if os.name == "nt":
        # opencode.cmd 只是包装脚本，底层原生 exe 才好捕获输出。
        native_executable = Path(executable).parent / "node_modules" / "opencode-ai" / "bin" / "opencode.exe"
        return str(native_executable) if native_executable.is_file() else executable
    return executable

def _run_opencode(self, executable: str, code_directory: Path, prompt: str, timeout: int, log_path: str = "") -> tuple[str, int, bool]:
    """Run one read-only OpenCode analysis and return its JSON stream, exit code, and timeout flag."""
    command = [executable, "run", "--pure", "--dir", str(code_directory)]
    if log_path:
        command.extend(["--file", log_path])
    command.extend(["--format", "json", prompt])
    process = subprocess.Popen(command, cwd=str(code_directory), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
    timed_out = False
    try:
        output, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        if os.name == "nt":
            # Windows 下 OpenCode 会拉起子进程，必须整棵进程树杀掉。
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)], capture_output=True, text=True)
        else:
            process.kill()
        output, _ = process.communicate()
    return output, process.returncode, timed_out
```

- [ ] **Step 5: 失败分析改用帮助方法并等待概括线程**

`_start_failure_analysis` 内 `analyze()` 的 CLI 定位与执行段替换为：

```python
executable = self._opencode_executable()
if not executable:
    self._event(task_id, "log", sequence, stage="analysis", message="未找到 OpenCode CLI，已跳过自动分析")
    return
code_directory = Path(project["path"]) / "Assets" / "Editor" / "Main" / "BuildAPK" / "New"
if not code_directory.is_dir():
    code_directory = Path(project["path"]) / "Assets" / "Editor"
prompt = (
    "请分析本次 Unity AB 资源打包是否成功（成功或失败都要给出明确结论）。完整 Unity 日志文件路径是："
    f"{project['log_path']}；对应打包工具代码目录是：{code_directory}；工程是：{project['path']}；分支是：{task['branch']}。"
    "请优先读取并完整分析该日志，必要时查阅上述打包工具代码目录。只读分析，不要修改任何文件。"
    "请直接用中文输出最终结论，明确说明是否成功；如果失败，给出根因、关键证据和建议修复步骤。Agent 会把最终回答生成 HTML 报告。"
)
output, exit_code, timed_out = self._run_opencode(executable, code_directory, prompt, 600, project["log_path"])
if timed_out:
    self._event(task_id, "log", sequence, stage="analysis", message="OpenCode 分析超时，已终止分析进程")
```

`finally` 块替换为：

```python
finally:
    # 等差异概括线程结束，保证飞书通知能按顺序拿到它。
    self._await_change_summary(task_id)
    self._emit(task_id, {"task_id": task_id, "kind": "analysis_done"})
```

- [ ] **Step 6: 静态检查**

Run: `python -m compileall agent`
Expected: 无错误

---

### Task 4: 服务接线（agent/service.py）

**Files:**
- Modify: `agent/service.py`

- [ ] **Step 1: 事件汇总**

`_consume_events` 整体替换为：

```python
async def _consume_events(self, events: asyncio.Queue[dict[str, Any]], send: Any) -> tuple[str, str, str, dict[str, Any]]:
    """Forward build events until the terminal analysis event arrives.

    Returns the terminal (status, message, commit_sha) and the collected change summary.
    """
    terminal_status = ""
    terminal_message = ""
    commit_sha = ""
    # 差异说明由 diff / diff_summary 两个事件拼成，最终随飞书通知发出。
    change: dict[str, Any] = {"raw": "", "stat": "", "count": 0, "note": "", "summary": ""}
    while True:
        event = await events.get()
        if event.get("kind") == "analysis_done":
            break
        if event.get("kind") == "diff":
            change["raw"] = event.get("raw", "")
            change["stat"] = event.get("stat", "")
            change["count"] = event.get("count", 0)
            change["note"] = event.get("note", "")
        elif event.get("kind") == "diff_summary":
            change["summary"] = event.get("summary", "")
        await send(event)
        commit_sha = event.get("commit_sha") or commit_sha
        if event.get("kind") == "status" and event.get("status") in ("success", "failed", "cancelled"):
            terminal_status = event.get("status", "")
            terminal_message = event.get("message", "")
        # Successful builds always have an analysis; cancellation before AB has none.
        if terminal_status == "cancelled":
            break
    return terminal_status, terminal_message, commit_sha, change
```

- [ ] **Step 2: 两个调用点接住返回值**

`_run_scheduled` 中：

```python
status, message, commit_sha, change = await self._consume_events(events, self._send_event_when_connected)
await build_future
# Scheduled builds notify through the same channel switch as manual builds.
await self._notify(project, channel, task.get("branch", ""), commit_sha, status, message, started, change)
```

`_start_task` 中：

```python
status, message, commit_sha, change = await self._consume_events(events, lambda event: self._send_event(socket_connection, event))
await build_future
await self._notify(project, channel, task.get("branch", ""), commit_sha, status, message, started, change)
```

- [ ] **Step 3: 通知带差异参数**

`_notify` 替换为：

```python
async def _notify(self, project: dict[str, Any], channel: dict[str, Any], branch: str, commit_sha: str,
                  status: str, message: str, started: float, change: dict[str, Any] | None = None) -> None:
    """Send the Feishu notification for a finished build when the channel enables it."""
    webhook = str(self.config.data.get("feishu_webhook", "")).strip()
    if status not in ("success", "failed") or not channel.get("notify_feishu") or not webhook:
        return
    change = change or {}
    text = build_feishu_text(status, project.get("name") or project["id"], channel.get("name", ""), branch,
                             commit_sha, time.time() - started, message, self._manager_http_url(),
                             feature_summary=change.get("summary", ""), commit_detail=change.get("raw", ""),
                             commit_stat=change.get("stat", ""), commit_count=change.get("count", 0),
                             change_note=change.get("note", ""))
    try:
        await asyncio.to_thread(send_feishu_text, webhook, text)
    except Exception as error:
        # A notification failure must never change the build result.
        print(f"feishu notify failed: {error}")
```

- [ ] **Step 4: 静态检查**

Run: `python -m compileall agent manager shared`
Expected: 无错误

---

### Task 5: 全量验证

- [ ] **Step 1: 单元测试**

Run: `python -m unittest discover -s tests -t . -v`
Expected: 8 个用例全部 PASS

- [ ] **Step 2: 导入冒烟**

Run: `python -c "import agent.service, agent.executor, agent.notify, agent.git; print('ok')"`
Expected: 输出 `ok`

- [ ] **Step 3: 手动冒烟清单（需真实构建机执行）**

1. release 渠道且 `notify_feishu` 开启：打包后飞书消息出现"【本次重要变更】+【提交明细】"，明细中无 merge 提交。
2. 同一 commit 连续打包两次：第二条消息出现"本次打包无新增代码变更"。
3. dev 渠道打包：飞书消息与现状一致，日志无"差异概括"相关输出。
4. 首次打包（基准分支不一致）：出现"首次打包，无对比基准"提示。
