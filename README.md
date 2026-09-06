# AB2 Unity Build Tool

AB2 consists of a central Manager and one background Agent per Unity build machine.

## Install

```text
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

On macOS use `.venv/bin/pip` and `.venv/bin/python`.

## Start

Start the Manager with `run_manager.bat`, or run:

```text
python -m uvicorn manager.main:app --host 0.0.0.0 --port 8000
```

Start an Agent on each build machine. `run_agent.bat` automatically installs the Python dependencies on first launch. Replace the Manager address with the Manager machine's actual LAN IP:

```text
python -m agent.service --manager ws://172.18.67.71:8000/ws/agent --config data/agent.json
```

The Agent configuration page is available at `http://127.0.0.1:8020/`. Add Unity projects and channels there. The project platform is read from the newest Unity `Library/PlayerDataCache` target directory and is read-only. Generated Unity logs are saved under `<project>/Log/AB2-build.log`. A channel requires a name, `SwitchTo` (`android_dev`, `android_release`, `ios_dev`, `ios_release`, `openharmony_dev`, or `openharmony_release`), and Unity build method. AB2 invokes `HLS_Editor.ExportEditor.BuildFromAB2` and passes the selected `SwitchTo` value as `-ab2Agent`. AB2 does not inspect or upload output files; the configured Unity method owns its default output behavior. The supported platform label is `OpenHarmony`.

The Agent configuration file is created after the first project is saved in the Agent page. A project contains `id`, `name`, `path`, `unity_path`, `log_path`, and `default_branch`; each channel contains `name`, read-only `platform` (`Android`, `iOS`, or `OpenHarmony`), `switch_to` (`android_dev`, `android_release`, `ios_dev`, `ios_release`, `openharmony_dev`, or `openharmony_release`), and `build_method`.

The Manager dashboard is available at `http://localhost:8000/`. The current MVP exposes registration, heartbeat, configuration synchronization, task dispatch, task persistence, and task log snapshots. Build task creation is available through `POST /api/tasks` with `agent_id`, `project_id`, `channel`, and `branch`.

## 接入其他机器的 Agent

1. 在 Manager 机器上运行 `run_manager.bat`，确认 Manager 监听 `0.0.0.0:8000`。
2. 在 Manager 机器上运行 `open_firewall_8000.bat`，允许内网 Agent 连接 TCP 8000。
3. 在 Manager 机器执行 `ipconfig`，当前示例使用内网 IP `172.18.67.71`；如果 IP 变化，请替换下面命令中的地址。
4. 在远程构建机设置 Manager 地址并启动 Agent：

```text
set AB2_MANAGER_URL=ws://172.18.67.71:8000/ws/agent
run_agent.bat
```

macOS 或不使用批处理文件时直接运行：

```text
python -m agent.service --manager ws://172.18.67.71:8000/ws/agent --config data/agent.json --web-port 8020
```

5. 在 Manager 页面打开 `http://172.18.67.71:8000/`，刷新后即可看到远程 Agent。

Agent 使用本机局域网 IP 作为 Manager 中的唯一 Agent ID，避免复制配置文件导致机器互相覆盖。远程 Agent 的配置页仍在它自己的机器上：`http://127.0.0.1:8020/`。当前版本适用于可信内网，尚未启用 Manager/Agent 身份认证，不要直接暴露到公网。

## 本机服务控制页

可以先启动控制页，再通过浏览器启停本机服务：

```text
python control.py --host 127.0.0.1 --port 8010
```

打开 `http://127.0.0.1:8010/`，页面提供 Manager 和 Agent 的启动、停止、状态刷新。Windows 也可以直接运行 `run_control.bat`。控制页只监听本机地址，且只允许启动项目内预设的 Manager/Agent 模块，不接受任意命令。
