# 打包前同步主线设计规格

## 1. 目标与范围

对带后缀的分支（如 `dev/9-2-26_鸿蒙`），支持在打包前先把对应主线分支（`dev/9-2-26`）的最新提交合并进当前分支，再执行打包。

范围：

- 开关放在渠道配置（Agent 配置页），对手动打包和定时打包同时生效。
- 主线推导：分支名以 `dev/月-日-年` 或 `feature/月-日-年` 开头且后面还有后缀时，前缀即主线；纯主线分支不触发。
- 复用现有构建流程：Git 同步之后、XLua 清理之前执行合并。

非目标：自动推送合并结果、处理非日期命名分支的继承关系、在 Manager 页面逐次覆盖开关。

## 2. 配置模型

渠道新增可选字段：

```json
"sync_mainline": true
```

- 默认关闭；必须是布尔（`AgentConfig.save_project` 校验）。
- Agent 配置页每个渠道显示"打包前同步主线"开关。

## 3. 构建流程变更

Git 阶段在 `sync_branch` 完成后追加：

1. 由当前分支推导主线（`mainline_branch`）：`dev/9-2-26_鸿蒙` → `dev/9-2-26`，`feature/7-1-26/S35赛季` → `feature/7-1-26`；无后缀则跳过。
2. 检查 `origin/主线` 是否存在；不存在则记录日志并跳过合并，不失败构建。
3. 执行 `git merge --no-ff --no-edit origin/主线`：
   - 在本地分支产生一个 merge commit，打包内容即主线合并后的代码，提交记录可在任务和本机仓库中查看。
   - 机器未配置 Git 身份时使用 `-c user.email=ab2@local -c user.name=AB2` 兜底提交。
   - 冲突：执行 `git merge --abort` 清理现场，构建按 Git 阶段失败。
4. 把 merge commit SHA 写入任务记录（替换原有的分支 SHA），任务页面"Commit"显示实际打包的合并版本。

日志会输出"merging mainline origin/xxx into yyy"和"mainline merged: origin/xxx -> <merge sha>"。

## 4. 错误与恢复

- 合并冲突：`git merge --abort` 后构建失败，下次构建由 `sync_branch` 的 `reset --hard` 恢复干净状态。
- 主线不存在：跳过合并并记录日志。
- 分支被强推导致 `sync_branch` 重置：属于既有行为，不受本功能影响。

## 5. 测试策略

- `mainline_branch` 单元测试：带后缀 / 纯主线 / feature 斜杠后缀 / 非日期分支。
- Git 集成测试（临时仓库）：主线前进后合并进后缀分支、冲突自动 abort、主线缺失返回跳过信号。
- 配置校验：`sync_mainline` 非布尔被拒绝。
