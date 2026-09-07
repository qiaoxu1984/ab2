"""Unity AB build execution adapted from the existing asset_builder workflow."""

import json
import html
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

from agent.git import run_git, sync_branch
from agent.process import close_unity_for_project


class BuildExecutor:
    """Execute a configured branch/channel build and emit structured events."""

    METHOD = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")

    def __init__(self, emit: Callable[[dict[str, Any]], None]):
        """Store the event callback used for live and replayable progress."""
        self.emit = emit
        self.locks: dict[str, threading.Lock] = {}
        self.processes: dict[str, subprocess.Popen[Any]] = {}
        self.process_lock = threading.Lock()

    def cancel(self, task_id: str) -> None:
        """Terminate the Unity process immediately when a running task is cancelled."""
        with self.process_lock:
            process = self.processes.get(task_id)
        if process and process.poll() is None:
            process.terminate()

    def _event(self, task_id: str, kind: str, sequence: list[int], **data: Any) -> None:
        """Emit a monotonically numbered event for reconnect de-duplication."""
        sequence[0] += 1
        self.emit({"task_id": task_id, "kind": kind, "sequence": sequence[0], **data})

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
            sha = sync_branch(project["path"], task["branch"], lambda line: self._important_log(task_id, sequence, "git", line))
            self._event(task_id, "status", sequence, stage="git", commit_sha=sha, message=f"checked out {sha}")
            current_stage = "xlua"
            self._wait_before_next_stage()
            self._clear_xlua_gen(project["path"], task_id, sequence)
            self._invoke_unity(task_id, project, "HLS_Editor.ExportEditor.ResetXLua", sequence, cancel_event=cancel_event, stage=current_stage)
            current_stage = "ab"
            self._wait_before_next_stage()
            # Forward version 3800 by default while allowing a channel-specific override.
            ab2_version = channel.get("ab2_version") or "3800"
            self._invoke_unity(task_id, project, channel["build_method"], sequence, self._agent_type(channel), cancel_event, current_stage, ab2_version)
            self._event(task_id, "log", sequence, stage="ab", message="generated Bundles/Diff/PackageManifest_DefaultPackage.version")
            self._event(task_id, "status", sequence, status="success", stage="ab", commit_sha=sha, message="build complete")
            # Always start the AI analysis after the AB stage, including successful builds.
            self._start_failure_analysis(task_id, project, task, sequence)
        except Exception as error:
            status = "cancelled" if cancel_event and cancel_event.is_set() else "failed"
            self._event(task_id, "status", sequence, status=status, stage=current_stage, error_code="" if status == "cancelled" else "build_error", message=str(error))
            if status == "failed":
                self._start_failure_analysis(task_id, project, task, sequence)
        finally:
            lock.release()

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
        # Async editor entry points exit themselves after compilation and the AB callback finish.
        if method not in ("HLS_Editor.ExportEditor.WaitForCompilation", "HLS_Editor.ExportEditor.BuildFromAB2"):
            command.insert(2, "-quit")
        if agent_type:
            # Unity exposes custom command-line values through Environment.GetCommandLineArgs().
            command.extend(["-ab2Agent", agent_type])
        # Forward an explicitly configured resource version only to the AB2 build command.
        if method == "HLS_Editor.ExportEditor.BuildFromAB2" and ab2_version:
            command.extend(["-ab2Version", str(ab2_version)])
        Path(project["log_path"]).parent.mkdir(parents=True, exist_ok=True)
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
        with self.process_lock:
            self.processes[task_id] = process
        assert process.stdout is not None
        for line in process.stdout:
            if cancel_event and cancel_event.is_set():
                process.terminate()
                raise RuntimeError("build cancellation requested")
        exit_code = process.wait()
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

    def _start_failure_analysis(self, task_id: str, project: dict[str, Any], task: dict[str, Any], sequence: list[int]) -> None:
        """Start a read-only OpenCode build analysis without blocking the build worker."""
        def analyze() -> None:
            """Ask OpenCode to inspect the failed branch and Unity log, then stream its answer."""
            try:
                self._event(task_id, "log", sequence, stage="analysis", message="__AB2_ANALYSIS_STARTED__")
                executable = shutil.which("opencode.cmd") if __import__("os").name == "nt" else shutil.which("opencode")
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
                if __import__("os").name == "nt":
                    native_executable = Path(executable).parent / "node_modules" / "opencode-ai" / "bin" / "opencode.exe"
                    executable = str(native_executable) if native_executable.is_file() else executable
                command = [executable, "run", "--pure", "--dir", str(code_directory), "--file", project["log_path"], "--format", "json", prompt]
                process = subprocess.Popen(command, cwd=str(code_directory), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
                result_count = 0
                final_text = []
                try:
                    output, _ = process.communicate(timeout=600)
                except subprocess.TimeoutExpired as timeout_error:
                    if __import__("os").name == "nt":
                        subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)], capture_output=True, text=True)
                    else:
                        process.kill()
                    output, _ = process.communicate()
                    self._event(task_id, "log", sequence, stage="analysis", message="OpenCode 分析超时，已终止分析进程")
                for line in output.splitlines():
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    part = payload.get("part", {})
                    metadata = part.get("metadata", {}).get("openai", {})
                    if payload.get("type") == "text" and metadata.get("phase") == "final_answer" and part.get("text"):
                        result_count += 1
                        final_text.append(part["text"].strip())
                exit_code = process.returncode
                if exit_code != 0:
                    self._event(task_id, "log", sequence, stage="analysis", message=f"OpenCode 分析失败，退出码 {exit_code}")
                elif result_count == 0:
                    self._event(task_id, "log", sequence, stage="analysis", message="OpenCode 未返回分析结果")
                if final_text:
                    report_html = "<!doctype html><meta charset='utf-8'><title>AB2 AI 分析报告</title><style>body{background:#111827;color:#dbeafe;font:15px system-ui;padding:32px;line-height:1.7}pre{white-space:pre-wrap}</style><h1>AB2 AI 失败原因分析</h1><pre>" + html.escape("\n\n".join(final_text)) + "</pre>"
                    report_path = Path(project["path"]) / "AB2Reports" / f"{task_id}.html"
                    report_path.parent.mkdir(parents=True, exist_ok=True)
                    report_path.write_text(report_html, encoding="utf-8")
                    self.emit({"task_id": task_id, "kind": "report", "html": report_html})
            except Exception as analysis_error:
                self._event(task_id, "log", sequence, stage="analysis", message=f"OpenCode 分析失败：{analysis_error}")
            finally:
                self.emit({"task_id": task_id, "kind": "analysis_done"})
        threading.Thread(target=analyze, name=f"ab2-analysis-{task_id}", daemon=True).start()
