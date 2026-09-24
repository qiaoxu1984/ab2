"""Unity AB build execution adapted from the existing asset_builder workflow."""

import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

from agent.git import current_revision, is_ancestor, mainline_branch, merge_mainline, revision_changes, run_git, sync_branch
from agent.process import close_unity_for_project
from agent.ticket import TICKET_MODULE, find_zip_name, is_release_channel, submit_ticket
from shared.report import build_ai_report

# 差异概括的 AI 调用超时与分析线程的等待上限（秒）。
CHANGE_SUMMARY_TIMEOUT_SECONDS = 180
CHANGE_SUMMARY_JOIN_TIMEOUT_SECONDS = 200


class BuildExecutor:
    """Execute a configured branch/channel build and emit structured events."""

    METHOD = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")

    def __init__(self, emit: Callable[[dict[str, Any]], None]):
        """Store the event callback used for live and replayable progress."""
        # Keep callbacks isolated per task so concurrent projects cannot overwrite each other's event stream.
        self.emitters: dict[str, Callable[[dict[str, Any]], None]] = {}
        self.emitter_lock = threading.Lock()
        self.default_emit = emit
        self.locks: dict[str, threading.Lock] = {}
        self.processes: dict[str, subprocess.Popen[Any]] = {}
        self.process_lock = threading.Lock()
        # 差异概括线程按任务登记，打包结束后的分析线程会有界等待它们。
        self.change_threads: dict[str, threading.Thread] = {}
        self.change_lock = threading.Lock()
        # 事件序号需要跨构建线程和概括线程保持唯一。
        self.sequence_lock = threading.Lock()

    def set_emitter(self, task_id: str, emit: Callable[[dict[str, Any]], None]) -> None:
        """Register the event callback belonging to one running task."""
        with self.emitter_lock:
            self.emitters[task_id] = emit

    def clear_emitter(self, task_id: str) -> None:
        """Remove a task callback after its event consumer has finished."""
        with self.emitter_lock:
            self.emitters.pop(task_id, None)

    def _emit(self, task_id: str, event: dict[str, Any]) -> None:
        """Deliver an event through the callback registered for its task."""
        with self.emitter_lock:
            emit = self.emitters.get(task_id, self.default_emit)
        emit(event)

    def cancel(self, task_id: str) -> None:
        """Terminate the Unity process immediately when a running task is cancelled."""
        with self.process_lock:
            process = self.processes.get(task_id)
        if process and process.poll() is None:
            process.terminate()

    def _event(self, task_id: str, kind: str, sequence: list[int], **data: Any) -> None:
        """Emit a monotonically numbered event for reconnect de-duplication."""
        # 概括线程与构建线程会并发上报，加锁保证序号唯一。
        with self.sequence_lock:
            sequence[0] += 1
            number = sequence[0]
        self._emit(task_id, {"task_id": task_id, "kind": kind, "sequence": number, **data})

    def _important_log(self, task_id: str, sequence: list[int], stage: str, line: str) -> None:
        """Forward actionable logs while dropping noisy file-by-file progress lines."""
        text = line.strip()
        lowered = text.lower()
        if stage == "git":
            keywords = ("invoke_cmd", "head is now", "updating", "fast-forward", "already up to date", "warning:", "error:", "fatal:", "conflict", "automatic merge failed")
        else:
            keywords = ("error cs", "compilation failed", "scripts have compiler errors", "fatal error", "crash!", "unhandled exception", "exception", "tundra build failed", "aborting batchmode", "buildfromab2", "build complete", "build failed", "switchto", "exit code", "success")
        if text and any(keyword in lowered for keyword in keywords):
            self._event(task_id, "log", sequence, stage=stage, message=text)

    def run(self, task: dict[str, Any], project: dict[str, Any], channel: dict[str, Any], cancel_event: threading.Event | None = None) -> None:
        """Run Git, channel switch, Unity build, log scan, and artifact verification."""
        task_id = task["task_id"]
        sequence = [0]
        lock = self.locks.setdefault(project["id"], threading.Lock())
        if not lock.acquire(blocking=False):
            self._event(task_id, "status", sequence, status="failed", error_code="project_busy", message="project already has an active build")
            return
        try:
            current_stage = "preflight"
            # Use an immutable per-task project copy so concurrent builds never share a Unity log file.
            task_project = dict(project)
            task_project["log_path"] = str(Path(project["path"]) / "Log" / f"AB2-build-{task_id}-{time.strftime('%Y%m%d_%H%M%S')}.log")
            if cancel_event and cancel_event.is_set():
                self._event(task_id, "status", sequence, status="cancelled", stage="cancelled", message="build cancelled")
                return
            self._event(task_id, "status", sequence, status="running", stage="preflight", message="preflight")
            if not Path(project["path"]).is_dir() or not Path(project["unity_path"]).is_file():
                raise RuntimeError("project path or Unity executable does not exist")
            if channel.get("switch_to", "").rsplit("_", 1)[-1] not in ("dev", "release"):
                raise RuntimeError("switch_to must end with dev or release")
            if not self.METHOD.fullmatch(channel["build_method"]):
                raise RuntimeError("invalid Unity build method name")
            # Enforce the configured default branch even if a Manager request was altered.
            if channel.get("branch_filter", "all_dev") == "default":
                task["branch"] = project.get("default_branch", "")
                if not task["branch"]:
                    raise RuntimeError("project default branch is not configured")
            closed_pids = close_unity_for_project(project["path"])
            if closed_pids:
                self._event(task_id, "log", sequence, stage="preflight", message=f"closed Unity processes: {', '.join(map(str, closed_pids))}")
            self._clear_build_caches(project["path"], task_id, sequence)
            self._prepare_diff_output(project["path"], task_id, sequence)
            current_stage = "git"
            self._wait_before_next_stage()
            self._event(task_id, "status", sequence, stage=current_stage, message="syncing branch")
            # release 渠道的飞书通知要带"自上次打包以来"的差异，先记录同步前基准。
            track_changes = is_release_channel(channel) and bool(channel.get("notify_feishu"))
            baseline_sha = ""
            baseline_branch = ""
            if track_changes:
                try:
                    baseline_branch, baseline_sha = current_revision(project["path"])
                except Exception as error:
                    # 基准读取失败不能影响构建，按无基准处理。
                    self._event(task_id, "log", sequence, stage="git", message=f"读取差异基准失败：{error}")
                if baseline_branch != task["branch"]:
                    # 基准不属于本次分支（首次打包或刚切分支），无法比较，只保留分支名用于提示。
                    baseline_sha = ""
            sha = sync_branch(project["path"], task["branch"], lambda line: self._important_log(task_id, sequence, "git", line))
            self._event(task_id, "status", sequence, stage="git", commit_sha=sha, message=f"checked out {sha}")
            # Optionally pull the updated mainline into a suffixed branch before packing it.
            if channel.get("sync_mainline"):
                self._merge_mainline(task_id, project, task, sequence)
            if track_changes:
                try:
                    self._collect_changes(task_id, project, sequence, baseline_branch, baseline_sha)
                except Exception as error:
                    # 差异说明属于附加信息，任何异常都不能影响构建结果。
                    self._event(task_id, "log", sequence, stage="git", message=f"差异说明采集异常：{error}")
            current_stage = "xlua"
            self._wait_before_next_stage()
            self._clear_xlua_gen(project["path"], task_id, sequence)
            self._invoke_unity(task_id, task_project, "HLS_Editor.ExportEditor.ResetXLua", sequence, cancel_event=cancel_event, stage=current_stage)
            self._invoke_unity(task_id, task_project, "HLS_Editor.ExportEditor.WaitForCompilation", sequence, cancel_event=cancel_event, stage=current_stage)
            current_stage = "ab"
            self._wait_before_next_stage()
            # Forward version 3800 by default while allowing a channel-specific override.
            ab2_version = channel.get("ab2_version") or "3800"
            self._invoke_unity(task_id, task_project, channel["build_method"], sequence, self._agent_type(channel), cancel_event, current_stage, ab2_version)
            self._event(task_id, "log", sequence, stage="ab", message="generated Bundles/Diff/PackageManifest_DefaultPackage.version")
            self._event(task_id, "status", sequence, status="success", stage="ab", commit_sha=sha, message="build complete")
            # 正式渠道构建成功后自动提交发布工单（同步执行，失败只记日志）。
            self._submit_release_ticket(task_id, task_project, channel, sequence)
            # Always start the AI analysis after the AB stage, including successful builds.
            self._start_failure_analysis(task_id, task_project, task, sequence, status="success")
        except Exception as error:
            status = "cancelled" if cancel_event and cancel_event.is_set() else "failed"
            self._event(task_id, "status", sequence, status=status, stage=current_stage, error_code="" if status == "cancelled" else "build_error", message=str(error))
            if status == "failed":
                self._start_failure_analysis(task_id, task_project, task, sequence, status="failed")
        finally:
            lock.release()

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

    def _merge_mainline(self, task_id: str, project: dict[str, Any], task: dict[str, Any], sequence: list[int]) -> None:
        """Merge the updated origin mainline into a suffixed build branch."""
        mainline = mainline_branch(task.get("branch", ""))
        if not mainline:
            # A plain mainline branch has nothing to merge, so the switch is a no-op.
            return
        self._event(task_id, "log", sequence, stage="git", message=f"merging mainline origin/{mainline} into {task['branch']}")
        try:
            merge_sha = merge_mainline(project["path"], mainline, lambda line: self._important_log(task_id, sequence, "git", line))
        except LookupError as error:
            # A missing mainline is surfaced in the log without failing the build.
            self._event(task_id, "log", sequence, stage="git", message=f"mainline merge skipped: {error}")
            return
        # Report the merge commit so the task record shows the synced revision.
        self._event(task_id, "log", sequence, stage="git", commit_sha=merge_sha, message=f"mainline merged: origin/{mainline} -> {merge_sha[:10]}")

    def _wait_before_next_stage(self) -> None:
        """Leave a short visible gap between successful pipeline stages."""
        time.sleep(1)

    def _clear_build_caches(self, project_path: str, task_id: str, sequence: list[int]) -> None:
        """Remove transient Bee and HybridCLR outputs before starting a clean build."""
        cache_paths = (
            Path(project_path) / "Library" / "Bee",
            Path(project_path) / "Library" / "ScriptAssemblies",
            Path(project_path) / "HybridCLRData" / "StrippedAOTDllsTempProj",
        )
        for cache_path in cache_paths:
            if not cache_path.exists():
                continue
            # A deletion failure indicates that a compiler process still owns this cache.
            try:
                shutil.rmtree(cache_path)
            except OSError as error:
                raise RuntimeError(f"cannot clear build cache {cache_path}: {error}") from error
            self._event(task_id, "log", sequence, stage="preflight", message=f"cleared build cache: {cache_path}")

    def _prepare_diff_output(self, project_path: str, task_id: str, sequence: list[int]) -> None:
        """Create an empty Diff output directory so only this build can produce its manifest."""
        diff_path = Path(project_path) / "Bundles" / "Diff"
        if diff_path.exists():
            try:
                shutil.rmtree(diff_path)
            except OSError as error:
                raise RuntimeError(f"cannot clear Diff output {diff_path}: {error}") from error
        diff_path.mkdir(parents=True, exist_ok=True)
        self._event(task_id, "log", sequence, stage="preflight", message=f"cleared Diff output: {diff_path}")

    def _verify_diff_manifest(self, project_path: str) -> None:
        """Require the current AB build to generate the DefaultPackage version manifest."""
        diff_path = Path(project_path) / "Bundles" / "Diff"
        # YooAsset places the manifest below a version directory, so search the fresh Diff tree recursively.
        if not any(path.is_file() for path in diff_path.rglob("PackageManifest_DefaultPackage.version")):
            raise RuntimeError(f"AB manifest was not generated under: {diff_path}")

    def _clear_xlua_gen(self, project_path: str, task_id: str, sequence: list[int]) -> None:
        """Clear only the generated XLua directory before Unity regenerates it."""
        generated_path = Path(project_path) / "Assets" / "Games_Logic" / "ThirdPlugins" / "XLua" / "Gen"
        if not generated_path.is_dir():
            self._event(task_id, "log", sequence, stage="xlua", message=f"XLua Gen directory not found: {generated_path}")
            return
        removed = 0
        for child in generated_path.iterdir():
            if child.is_dir():
                import shutil
                shutil.rmtree(child)
            else:
                child.unlink()
            removed += 1
        self._event(task_id, "log", sequence, stage="xlua", message=f"cleared XLua Gen entries: {removed}")

    def _agent_type(self, channel: dict[str, Any]) -> str:
        """Map the detected platform and switch type to the Unity proxy enum name."""
        return channel["switch_to"]

    def _invoke_unity(self, task_id: str, project: dict[str, Any], method: str, sequence: list[int], agent_type: str = "", cancel_event: threading.Event | None = None, stage: str = "unity", ab2_version: str = "") -> None:
        """Invoke one configured Unity executeMethod and stream its output."""
        self._event(task_id, "status", sequence, stage=stage, message=f"execute {method}")
        command = [project["unity_path"], "-batchmode", "-projectPath", project["path"], "-executeMethod", method, "-logFile", project["log_path"]]
        # WaitForCompilation exits through its own continuation; the synchronous AB entry point needs -quit here.
        if method != "HLS_Editor.ExportEditor.WaitForCompilation":
            command.insert(2, "-quit")
        if agent_type:
            # Unity exposes custom command-line values through Environment.GetCommandLineArgs().
            command.extend(["-ab2Agent", agent_type])
        # Forward an explicitly configured resource version only to the AB2 build command.
        if method == "HLS_Editor.ExportEditor.BuildFromAB2" and ab2_version:
            command.extend(["-ab2Version", str(ab2_version)])
        Path(project["log_path"]).parent.mkdir(parents=True, exist_ok=True)
        # Do not wait for Unity's stdout pipe: Unity child processes can inherit it
        # after the main process exits and otherwise leave the build worker blocked
        # forever without starting failure analysis.
        process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        with self.process_lock:
            self.processes[task_id] = process
        try:
            while True:
                if cancel_event and cancel_event.is_set():
                    process.terminate()
                    raise RuntimeError("build cancellation requested")
                try:
                    exit_code = process.wait(timeout=10)
                    break
                except subprocess.TimeoutExpired:
                    continue
        finally:
            if process.poll() is None and cancel_event and cancel_event.is_set():
                process.terminate()
        with self.process_lock:
            self.processes.pop(task_id, None)
        # AB success is defined by the fresh Diff manifest, not Unity's textual shutdown output.
        if stage == "ab":
            self._verify_diff_manifest(project["path"])
            return
        if exit_code != 0:
            raise RuntimeError(f"Unity method failed: {method}")
        critical_errors = self._unity_log_errors(project["log_path"])
        if critical_errors:
            raise RuntimeError("Unity log contains critical errors: " + " | ".join(critical_errors[:5]))

    def _unity_log_errors(self, log_path: str) -> list[str]:
        """Detect critical Unity failures that do not propagate through the process exit code."""
        try:
            lines = Path(log_path).read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as error:
            return [f"cannot read Unity log: {error}"]
        patterns = (
            re.compile(r"error CS\d+", re.IGNORECASE),
            re.compile(r"Scripts have compiler errors", re.IGNORECASE),
            re.compile(r"Compilation failed", re.IGNORECASE),
            re.compile(r"Tundra build failed", re.IGNORECASE),
            re.compile(r"Script Compilation Error", re.IGNORECASE),
            re.compile(r"Aborting batchmode due to failure", re.IGNORECASE),
            re.compile(r"Fatal Error", re.IGNORECASE),
            re.compile(r"HybridCLR.*\b(?:failed|failure|error)\b", re.IGNORECASE),
        )
        return [line.strip() for line in lines if any(pattern.search(line) for pattern in patterns)]

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

    def _parse_analysis_output(self, output: str) -> tuple[list[str], int, int]:
        """Collect the OpenCode final answer from a JSON event stream of any CLI version.

        Different build machines may run different OpenCode versions, so the parser must
        not rely on provider-specific metadata:
        - older versions tag the final answer with part.metadata.openai.phase == "final_answer";
        - newer versions drop that metadata and only mark the final answer with a
          step-finish event whose reason is "stop" and a matching messageID.
        Returns the final text parts plus event and text counters for diagnostics.
        """
        # 旧版元数据标记的最终回答，优先级最高
        tagged_final: list[str] = []
        # 新版按 assistant 消息ID暂存文本，等待 step_finish 确认哪条消息才是最终回答
        text_by_message: dict[str, list[str]] = {}
        # 记录所有正常结束(reason=stop)的消息ID，最后一条即最终回答所在消息
        stopped_messages: list[str] = []
        event_count = 0
        text_count = 0
        for line in output.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                # 非 JSON 噪音行（如启动提示）直接跳过
                continue
            event_count += 1
            event_type = payload.get("type")
            part = payload.get("part") or {}
            if event_type == "text":
                text = (part.get("text") or "").strip()
                if not text:
                    continue
                text_count += 1
                metadata = (part.get("metadata") or {}).get("openai") or {}
                if metadata.get("phase") == "final_answer":
                    tagged_final.append(text)
                    continue
                # 兼容 messageID/messageId 两种字段命名
                message_id = str(part.get("messageID") or part.get("messageId") or "")
                text_by_message.setdefault(message_id, []).append(text)
            elif event_type in ("step_finish", "step-finish"):
                if str(part.get("reason") or "") == "stop":
                    message_id = str(part.get("messageID") or part.get("messageId") or "")
                    if message_id:
                        stopped_messages.append(message_id)
        if tagged_final:
            return tagged_final, event_count, text_count
        # 兜底1：取最后一条正常结束消息的全部文本片段
        # 兜底2：缺少 step_finish 时，取最后出现文本的消息
        fallback_order = list(reversed(stopped_messages))
        fallback_order += [message_id for message_id in reversed(list(text_by_message.keys())) if message_id and message_id not in stopped_messages]
        for message_id in fallback_order:
            texts = text_by_message.get(message_id) or []
            if texts:
                return texts, event_count, text_count
        # 兜底3：事件中连消息ID都缺失时，只保留最后一段文本，避免把过程说明当成结论
        all_texts = [text for texts in text_by_message.values() for text in texts]
        return all_texts[-1:], event_count, text_count

    def _submit_release_ticket(self, task_id: str, project: dict[str, Any], channel: dict[str, Any], sequence: list[int]) -> None:
        """Create the release ticket synchronously after a successful build.

        The ticket is an optional follow-up action: every failure is reported as a
        log event and must never change the already finished build result.
        """
        try:
            # 仅正式渠道建单；开发渠道直接跳过。
            if not is_release_channel(channel):
                return
            # 工单版本号使用 Unity 已上传 SVN 的资源包文件名。
            zip_name = find_zip_name(project["log_path"])
            if not zip_name:
                self._event(task_id, "log", sequence, stage="ticket", message="构建日志中未找到资源包名，已跳过自动建单")
                return
            self._event(task_id, "log", sequence, stage="ticket", message=f"开始提交工单: {TICKET_MODULE} {zip_name}")
            ok, summary = submit_ticket(zip_name)
            if ok:
                self._event(task_id, "log", sequence, stage="ticket", message=f"工单提交成功: {summary}")
            else:
                self._event(task_id, "log", sequence, stage="ticket", message=f"工单提交失败: {summary}")
        except Exception as error:
            # 工单异常绝不能影响已经完成的构建结果。
            self._event(task_id, "log", sequence, stage="ticket", message=f"工单提交异常: {error}")

    def _start_failure_analysis(self, task_id: str, project: dict[str, Any], task: dict[str, Any], sequence: list[int], status: str = "") -> None:
        """Start a read-only OpenCode build analysis without blocking the build worker.

        The final status selects the green success or red failure report heading.
        """
        def analyze() -> None:
            """Ask OpenCode to inspect the failed branch and Unity log, then stream its answer."""
            try:
                self._event(task_id, "log", sequence, stage="analysis", message="__AB2_ANALYSIS_STARTED__")
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
                # 解析逻辑兼容新旧 OpenCode 版本，避免打包机版本不一致导致报告丢失
                final_text, event_count, text_count = self._parse_analysis_output(output)
                if exit_code != 0:
                    self._event(task_id, "log", sequence, stage="analysis", message=f"OpenCode 分析失败，退出码 {exit_code}")
                elif not final_text:
                    # 记录事件统计，便于区分版本格式变化与模型未产出结论
                    self._event(task_id, "log", sequence, stage="analysis", message=f"OpenCode 未返回分析结果（事件 {event_count} 条，文本片段 {text_count} 个）")
                if final_text:
                    report_html = build_ai_report(status, "\n\n".join(final_text))
                    report_path = Path(project["path"]) / "AB2Reports" / f"{task_id}.html"
                    report_path.parent.mkdir(parents=True, exist_ok=True)
                    report_path.write_text(report_html, encoding="utf-8")
                    self._emit(task_id, {"task_id": task_id, "kind": "report", "html": report_html})
            except Exception as analysis_error:
                self._event(task_id, "log", sequence, stage="analysis", message=f"OpenCode 分析失败：{analysis_error}")
            finally:
                # 等差异概括线程结束，保证飞书通知能按顺序拿到它。
                self._await_change_summary(task_id)
                self._emit(task_id, {"task_id": task_id, "kind": "analysis_done"})
        threading.Thread(target=analyze, name=f"ab2-analysis-{task_id}", daemon=True).start()
