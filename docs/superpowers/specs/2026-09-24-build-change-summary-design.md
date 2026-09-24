# 打包差异说明（飞书）设计规格

## 1. 目标与范围

release 渠道打包时，自动生成"自上次打包以来"的代码变更说明，随构建结果的飞书通知一起发送，让测试和策划直接在飞书看到本次的重要功能变化。

范围：

- 触发条件：渠道为 release（`switch_to` 以 `_release` 结尾）且开启 `notify_feishu`；其他渠道不采集、不调用 AI。
- 对比基准：本机 git 同步前的 HEAD（同一分支时即上次打包检出的 commit）。
- 内容：原始提交明细（`--no-merges` 排除合并提交，附改动量统计）+ OpenCode AI 功能概括。
- 输出：仅飞书通知消息，不改 Manager 页面、数据库和任务报告。
- 定时打包与手动打包走同一路径，自动生效。

非目标：Manager 页面存档差异说明、dev 渠道发送、写入 release 工单备注、在飞书中提供差异详情链接。

## 2. 流程与数据流

1. git 阶段开始前（`sync_branch` 之前）记录基准 `(baseline_branch, baseline_sha)`：
   - 只有 release 且开启飞书通知的渠道才记录。
   - `baseline_branch` 与本次目标分支不一致（首次打包或刚切分支）时丢弃基准，按"无对比基准"处理。
2. git 同步（以及可选的 `sync_mainline` 合并）完成后，读取最终 HEAD：
   - 与基准相同：上报"本次打包无新增代码变更"，不调用 AI。
   - 不同：采集非 merge 提交明细与 `git diff --shortstat`，立即上报 `diff` 事件；同时启动后台线程调用 OpenCode 生成功能概括，完成后上报 `diff_summary` 事件。
3. AI 概括与构建并行执行，不阻塞构建；分析线程在发送 `analysis_done` 前有界等待概括线程，保证事件顺序。
4. Agent 的 `_consume_events` 捕获 `diff` / `diff_summary` 事件，`_notify` 把它们拼进飞书文本。
5. Manager 收到未知事件类型时直接忽略，无需改动。

## 3. Git 采集（agent/git.py）

新增纯函数：

- `current_revision(project_path) -> (branch, sha)`：读取当前分支与 HEAD，Git 失败返回空串。
- `is_ancestor(project_path, old_sha, new_sha) -> bool`：判断基准提交是否仍在当前历史中（强推场景需要提示）。
- `revision_changes(project_path, old_sha, new_sha) -> (detail_text, stat_line, total)`：
  - `git log --no-merges --date=format:%m-%d --pretty=format:'%h %ad %an %s' old..new`；
  - `git diff --shortstat old new` 作为改动量；
  - 明细最多保留最新 `MAX_CHANGE_COMMITS = 80` 条、总长最多 `MAX_CHANGE_CHARS = 6000` 字符，超出截断并追加省略标记；
  - `total` 为未截断的提交总数。

## 4. AI 功能概括（agent/executor.py）

- 复用现有 OpenCode CLI 定位逻辑（Windows 优先 `opencode.cmd` 及其原生 exe 兜底）与 `_parse_analysis_output`；把 CLI 定位和执行抽成 `_opencode_executable` / `_run_opencode` 两个帮助方法，供打包失败分析和差异概括共用。
- prompt 内联提交明细与改动量，要求：
  - 中文输出 3~8 条"重要功能变更"要点；
  - 合并同类改动，突出对玩家或业务可见的功能变化；
  - 忽略纯日志、注释、格式、路径调整等琐碎改动；
  - 没有重要变化时只输出"无重要功能变更"；
  - 只输出要点本身，不要前言、结语或代码块。
- 超时 `CHANGE_SUMMARY_TIMEOUT_SECONDS = 180` 秒，超时终止进程；CLI 不存在、超时、解析失败都只记录日志，摘要留空，飞书退化为只发提交明细。
- 概括线程结束前一定补发 `diff_summary` 事件（失败时摘要为空），保证通知拿到数据。

## 5. 事件与线程顺序

- 新增事件类型：`diff`（字段 `raw`、`stat`、`count`、`note`）与 `diff_summary`（字段 `summary`）。
- 概括线程登记在 `BuildExecutor.change_threads[task_id]`，线程自己结束时摘除登记。
- `_start_failure_analysis` 在 `finally` 中调用 `_await_change_summary(task_id)`，有界等待（`CHANGE_SUMMARY_JOIN_TIMEOUT_SECONDS = 200`）后再发 `analysis_done`；没有概括线程（dev 渠道/取消）时立即返回。
- `_event` 增加序号锁，避免构建线程与概括线程并发取号产生重复 sequence。
- 事件未被消费或线程超时后补发时，回调已被清理，事件安全丢弃，不影响任务状态。

## 6. 飞书消息（agent/notify.py）

`build_feishu_text` 增加可选参数：`feature_summary`、`commit_detail`、`commit_stat`、`commit_count`、`change_note`。

消息在原有构建结果后追加：

```text
【本次重要变更】
1. ...
【提交明细】（共 12 个提交，已排除合并提交）
改动量：4 files changed, 100 insertions(+), 5 deletions(-)
a1b2c3d 09-20 张三 添加鸿蒙分支选择器
...
…等共 12 个提交
```

- 无变更："本次打包无新增代码变更"。
- 无基准："首次打包，无对比基准"。
- 有明细但 AI 概括为空："（AI 概括不可用，以下为提交明细）"。
- 基准不在当前历史：附一行"基准提交不在当前历史，明细可能包含非本次引入的提交"。
- 长度控制：AI 摘要最多 1000 字符、明细最多 20 行、整条消息最多 4000 字符，超出截断并追加"…（消息过长已截断）"。

## 7. 错误与边界

- 构建在 git 阶段前失败：没有概括线程，飞书消息不含差异段落，保持现状。
- 构建取消：不发飞书，差异线程结果被丢弃。
- `sync_mainline` 合并场景：明细包含合并进来的主线提交（它们是本次实际打包内容），merge commit 本身被 `--no-merges` 排除。
- 基准提交不在当前历史（强推/切换）：照常输出 `old..new` 差异，附提示。
- OpenCode 或飞书不可用：只影响差异段落和通知，绝不影响构建结果和任务状态。

## 8. 验证策略

- 标准库 `unittest`：
  - 临时 Git 仓库构造含 merge commit 的历史，断言 `revision_changes` 排除 merge、统计正确、截断生效；
  - `build_feishu_text` 在各分支（无变更/无基准/有摘要/超长）下的输出与封顶；
  - `is_ancestor` 在强推前后的结果。
- 手动冒烟：
  - release + `notify_feishu` 渠道打包，确认飞书消息带摘要和明细；
  - dev 渠道或关闭通知的渠道打包，确认无额外 AI 调用、消息不变；
  - 连续两次打包同一 commit，确认提示"无新增代码变更"。

## 9. 涉及文件

- `agent/git.py`：基准读取、提交与改动量采集。
- `agent/executor.py`：采集时机、概括线程、事件上报、AI 调用抽取。
- `agent/service.py`：事件捕获与飞书参数传递。
- `agent/notify.py`：差异段落格式化与长度控制。
- `tests/test_git_changes.py`、`tests/test_feishu_text.py`：新增单元测试（标准库 unittest）。
