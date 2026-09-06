# Unity 持续构建工具设计规格

## 1. 目标与范围

构建一个由 Manager 和 Agent 组成的 Unity AB 包持续构建工具。

首期范围：

- Manager 部署在一台内网中心机器，管理十台以内 Agent。
- Agent 支持 Windows 和 macOS，作为后台服务常驻。
- Agent 配置本机已有 Unity 工程，不负责首期 clone 远程仓库。
- Manager 手动创建构建任务，不实现定时轮询和 Git Webhook。
- 构建任务支持选择 Git 分支和渠道。
- 渠道由 Unity 切换方法和打包方法组成。
- Manager 展示 Agent 状态、任务状态和实时日志。
- 构建流程复用 `asset_builder`：Git 同步、Unity batchmode、日志检查、AB 产物校验和 Bee/Tundra 重试。

非目标：自动轮询、Webhook、对象存储上传、用户权限体系、运行中 Unity 进程强杀。

## 2. 系统架构

### Manager

Manager 是中心 HTTP 服务，负责：

- Agent 注册、心跳和在线状态。
- Agent 工程与渠道配置的展示和管理入口。
- 构建任务创建、按工程互斥和任务状态持久化。
- 实时日志转发、历史日志查询和产物信息展示。
- 使用 SQLite 保存 Agent、工程、渠道、任务和日志。

### Agent

Agent 是每台构建机上的常驻服务，负责：

- 主动向 Manager 注册并定时发送心跳。
- 保存并校验本机 Unity 工程配置。
- 上报工程分支列表和可用渠道。
- 接收构建任务并在本机执行实际构建。
- 推送阶段状态、日志、最终提交 SHA 和产物信息。
- Manager 断线期间继续构建，恢复后补传状态与日志。

### 通信

Agent 主动连接 Manager，避免中心服务需要访问不同平台构建机。首期使用内网 HTTP(S) 和共享密钥认证，消息均携带 `agent_id` 与 `task_id`。实时事件通过 WebSocket 推送，查询接口提供任务状态和日志快照，用于断线恢复。

## 3. 配置模型

### Agent

字段包括：`id`、名称、平台、主机名、Agent 版本、Unity 版本、能力信息、在线状态、最后心跳时间。

### Project

字段包括：归属 Agent、本地工程路径、工程名称、Unity 可执行文件路径、日志路径和默认分支。

一个 Agent 可以配置多个 Project。

### Channel

字段包括：归属 Project、渠道名称、目标平台、`switch_method`、`build_method`、产物匹配规则和启用状态。

`switch_method` 和 `build_method` 只允许 Unity 方法名格式，不能由 Manager 任意传入 shell 命令。Agent 在保存配置和执行前都校验方法名。

### BuildTask

字段包括：Agent、Project、Channel、目标分支、最终提交 SHA、创建人、状态、创建/开始/结束时间、错误分类、错误摘要、重试次数和产物列表。

任务状态：`queued`、`running`、`success`、`failed`、`cancel_requested`、`cancelled`、`disconnected`、`unknown`。

## 4. 构建流程

1. Manager 根据 Agent、Project、Channel 和目标分支创建任务。
2. Manager 以 `agent_id + project_id` 原子占用构建锁；同一 Agent 的同一工程无论分支或渠道都只能运行一个任务。
3. 不同工程允许在同一 Agent 上并行运行，但每个工程仍由 Agent 单独维护锁和 Unity 子进程。
4. Agent 预检工程目录、Git 仓库、Unity 可执行文件、日志路径和渠道配置。
5. Agent 获取目标分支列表，执行 fetch、切换目标分支、清理并更新工作区，记录最终 SHA。
6. Agent 执行渠道 `switch_method`。
7. Agent 执行渠道 `build_method`，使用 Unity batchmode。
8. Agent 扫描 Unity 日志，识别脚本编译错误、Fatal Error、Crash 和 HybridCLR 错误。
9. 发现 Bee/Tundra 崩溃时清理 Bee 缓存并自动重试，默认最多两轮。
10. 校验本次构建生成的非空 AB/zip 产物。
11. 上报结果、耗时、最终 SHA、日志和产物信息，释放工程锁。

运行中的 Unity 任务首期不强杀。取消只允许排队任务；运行中的任务只记录停止请求，待后续设计安全终止策略。

## 5. Manager 页面

- 总览页：Agent 卡片显示在线状态、平台、Unity 版本、当前任务和最近结果。
- Agent 详情页：显示工程列表、路径、默认分支和构建占用状态。
- Project 详情页：显示渠道列表及 Unity 切换/打包方法。
- 创建任务页：选择工程、分支和渠道，并展示预计执行方法及最新提交 SHA。
- 任务详情页：显示准备、Git、渠道切换、Unity 构建、产物校验阶段，提供实时日志、重试记录和产物信息。
- Agent 配置页：在 Manager 维护期望配置，并同步到 Agent；Agent 负责本机路径可用性校验并回报配置状态。

页面关键配置保存在 Manager，不依赖浏览器 localStorage。任务详情断线后通过任务 ID 获取日志快照并自动重连。

## 6. 错误与恢复

- 工程、Unity 或渠道配置缺失：任务进入失败，归类为 `configuration_error`。
- Git 冲突、网络失败、认证失败或工作区不干净：归类为 `git_error`。
- Unity 编译错误、脚本异常或构建退出码异常：归类为 `unity_error`。
- Bee/Tundra 崩溃：归类为 `unity_infrastructure_error`，按重试策略处理。
- 产物不存在或为空：归类为 `artifact_error`。
- Agent 超过心跳阈值未响应：Manager 标记 Agent 失联；相关运行任务暂不伪造成功或失败，等待 Agent 恢复上报。
- Manager 重启后无法确认的运行任务标记为 `unknown`，等待 Agent 上报最终状态。

日志事件至少包含时间、级别、阶段、消息和序号。序号用于断线补传和去重。

## 7. 测试策略

- 构建器单元测试：配置校验、Git 状态判定、分支处理、日志匹配、产物校验和 Bee 重试。
- 命令适配测试：分别验证 Windows 和 macOS 的 Git/Unity 命令构造，不启动真实 Unity。
- 并发测试：同一 Agent 同一工程的重复任务只能成功占用一个锁；不同工程可并行。
- 通信测试：心跳超时、WebSocket 断线、自动重连、日志序号补传和重复事件去重。
- 持久化测试：Manager 重启后恢复历史任务，并正确标记无法确认的运行任务。
- API 集成测试：注册、心跳、工程/分支/渠道查询、创建任务、状态查询、日志查询和取消排队任务。
- 端到端测试：使用一个真实 Unity 工程完成至少一个渠道的 AB 包构建，并验证最终 SHA、日志和产物记录。

## 8. 实现约束

- 首期优先使用 Python，Manager 与 Agent 共用协议模型；参考 `asset_builder` 的构建逻辑拆分为可测试模块。
- Manager 的任务状态必须持久化，Agent 的本地日志必须可在断线后补传。
- Manager 不直接执行构建机 shell 命令，所有构建动作由 Agent 执行。
- 每个构建任务必须记录目标分支和最终提交 SHA，保证结果可追溯。
- 任务锁必须同时存在于 Manager 和 Agent 两侧，防止重复下发及中心服务重启引发并发构建。
