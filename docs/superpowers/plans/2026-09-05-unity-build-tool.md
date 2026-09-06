# Unity Build Tool Implementation Plan

> **For agentic workers:** Implement this plan task-by-task. No test source files are required for this first landing; use static checks and manual smoke checks instead.

**Goal:** Deliver a Python Manager and Agent MVP for registering build machines, configuring local Unity projects/channels, manually dispatching branch/channel AB builds, and viewing live task logs.

**Architecture:** Manager owns SQLite state and a small web UI. Agents maintain local project configuration, connect outbound to Manager through a WebSocket, execute one task per project, and upload status/log/artifact events. The existing `asset_builder/build.py` logic is adapted behind an Agent executor rather than copied into the Manager.

**Tech Stack:** Python 3.11+, FastAPI, Uvicorn, WebSocket, SQLite, Pydantic, subprocess, vanilla HTML/CSS/JavaScript.

---

### Task 1: Bootstrap Shared Domain And Storage

**Files:**
- Create: `requirements.txt`
- Create: `shared/__init__.py`
- Create: `shared/models.py`
- Create: `shared/protocol.py`
- Create: `manager/__init__.py`
- Create: `manager/db.py`

- [ ] Define shared enums and Pydantic models for Agent, Project, Channel, BuildTask, artifact records, and log events.
- [ ] Define JSON message envelopes for registration, heartbeat, task dispatch, task event, branch listing, and configuration synchronization.
- [ ] Implement SQLite schema creation and CRUD helpers with foreign keys, task uniqueness by active `agent_id/project_id`, and append-only task logs.
- [ ] Add dependency pins for FastAPI, Uvicorn, WebSocket support, and Pydantic.
- [ ] Run `python -m compileall shared manager` and require zero diagnostics.

### Task 2: Implement Agent Configuration And Build Executor

**Files:**
- Create: `agent/__init__.py`
- Create: `agent/config.py`
- Create: `agent/git.py`
- Create: `agent/executor.py`
- Create: `agent/service.py`

- [ ] Persist Agent configuration as JSON under the Agent data directory, including local project paths, Unity paths, default branches, channel methods, and artifact globs.
- [ ] Implement platform-neutral Git subprocess helpers for fetch, branch checkout, reset, clean, pull, SHA lookup, branch listing, and porcelain status.
- [ ] Implement Unity batchmode invocation with streamed stdout/stderr, per-task log files, configured `switch_method`, configured `build_method`, and platform-specific executable checks.
- [ ] Port critical-log and artifact verification behavior from `asset_builder/build.py`, including Bee/Tundra retry with a maximum of two Unity attempts.
- [ ] Implement per-project locks so tasks for the same project are rejected while different projects can run concurrently.
- [ ] Implement outbound WebSocket registration, heartbeat, task receive, event upload, reconnect, and offline event replay.
- [ ] Run `python -m compileall agent` and manually invoke `python -m agent.service --help`.

### Task 3: Implement Manager API And Task Scheduler

**Files:**
- Create: `manager/main.py`
- Create: `manager/scheduler.py`
- Create: `manager/web.py`

- [ ] Implement Agent registration, heartbeat, online-state timeout, project/channel configuration endpoints, and branch-list request endpoint.
- [ ] Implement task creation with an atomic `agent_id/project_id` active-task guard, explicit branch/channel validation, queueing, and dispatch to the connected Agent.
- [ ] Implement task event ingestion with log sequence de-duplication, status transitions, final SHA, retry information, and artifacts.
- [ ] Implement task snapshot and paged log endpoints for reconnect and Manager restart recovery.
- [ ] On startup, mark previously running tasks as `unknown` until the Agent reports a final state.
- [ ] Serve the Manager web UI from the same FastAPI process.
- [ ] Run `python -m compileall manager` and start `uvicorn manager.main:app` for an HTTP smoke check.

### Task 4: Build Manager Web UI

**Files:**
- Create: `manager/static/index.html`
- Create: `manager/static/app.js`
- Create: `manager/static/style.css`

- [ ] Implement Agent overview cards showing online state, platform, Unity version, active task, and latest result.
- [ ] Implement Agent/project/channel configuration forms and configuration validation feedback.
- [ ] Implement task creation form with branch and channel selection.
- [ ] Implement task detail view with stage progress, WebSocket log stream, reconnect-to-snapshot behavior, retry information, and artifact list.
- [ ] Disable duplicate task submission when the same Agent/project is active; allow different projects to run in parallel.
- [ ] Keep critical configuration and task state server-side rather than in browser localStorage.

### Task 5: Packaging And Manual Smoke Verification

**Files:**
- Create: `run_manager.bat`
- Create: `run_agent.bat`
- Create: `README.md`

- [ ] Document installation, Manager startup, Agent registration, local project setup, channel method configuration, shared-key configuration, and manual task flow.
- [ ] Provide Windows launch scripts and platform-neutral Python module commands for macOS.
- [ ] Run `python -m compileall .` and start Manager and a local Agent with a temporary data directory.
- [ ] Manually verify registration, heartbeat, project/channel visibility, task dispatch, live logs, duplicate-project rejection, different-project concurrency, and Manager restart recovery.
- [ ] Run one real Unity AB build using the existing project configuration and confirm task SHA, final status, log, and non-empty artifact records.
