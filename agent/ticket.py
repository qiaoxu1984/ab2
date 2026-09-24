"""Release build ticket submission through the bundled autoApproval CLI.

正式（release）渠道打包成功后，Agent 调用 tool/ 目录下的 autoApproval 工具
在发布平台自动创建"客户端资源 / 定向环境"工单。
"""

import os
import platform
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

# 需求约定：所有 release 渠道的工单固定参数，写死在代码里，不随项目/机器变化。
TICKET_SUBMITTER = "乔旭"
TICKET_MODULE = "byhls-客户端"
TICKET_REMARK = "AB2 定向资源发布"

# 需求约定：所有 release 渠道的工单一律按"客户端资源 + 定向环境"提交。
TICKET_SERVICE_TYPE = "客户端资源"
TICKET_ENVIRONMENT = "定向环境"

# 单次工单提交的最长等待时间，防止工具阻塞构建线程。
TICKET_TIMEOUT_SECONDS = 120

# 各平台对应的 autoApproval 可执行文件（随仓库 tool/ 目录分发）。
BINARY_BY_PLATFORM = {
    "win32": "autoApproval-windows-amd64.exe",
    "darwin": "autoApproval-mac64",
    "linux": "autoApproval-linux",
}

# Unity 构建日志里记录已上传 SVN 包名的两种行格式，优先匹配提交成功行。
ZIP_PATTERNS = (
    re.compile(r"SVN commit succeeded:\s*(?P<path>.+?\.zip)\s*$", re.IGNORECASE),
    re.compile(r"RunSvn svn commit\s+(?P<path>.+?\.zip)\s+-m\s", re.IGNORECASE),
)


def is_release_channel(channel: dict[str, Any]) -> bool:
    """Return True when the channel is a release channel (switch_to ends with _release)."""
    return str(channel.get("switch_to", "")).rsplit("_", 1)[-1] == "release"


def find_zip_name(log_path: str) -> str:
    """Extract the uploaded SVN package file name from the Unity build log.

    从日志末尾往前找，重复打包时以最近一次提交的包名为准；找不到返回空串。
    """
    try:
        lines = Path(log_path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    for line in reversed(lines):
        for pattern in ZIP_PATTERNS:
            match = pattern.search(line)
            if match:
                # Unity 记录的是完整路径，工单只需要文件名。
                return Path(match.group("path").strip()).name
    return ""


def binary_path() -> Path | None:
    """Return the autoApproval executable for the current platform, if it exists."""
    name = BINARY_BY_PLATFORM.get(sys.platform)
    if not name:
        return None
    candidate = Path(__file__).resolve().parent.parent / "tool" / name
    return candidate if candidate.is_file() else None


def submit_ticket(version: str) -> tuple[bool, str]:
    """Run the autoApproval CLI for one release package and return success plus summary.

    提交人/模块/备注以及服务类型/环境均为代码内固定值，只有版本号（包名）随构建变化。
    """
    executable = binary_path()
    if not executable:
        return False, f"未找到 {platform.system()} 平台的工单工具"
    # Git 在 Windows 上提交的二进制不带执行位，macOS/Linux 运行前补齐。
    if os.name != "nt":
        try:
            os.chmod(executable, 0o755)
        except OSError:
            pass
    command = [
        str(executable),
        "-u", TICKET_SUBMITTER,
        "-p", TICKET_MODULE,
        "-v", version,
        "-t", TICKET_SERVICE_TYPE,
        "-e", TICKET_ENVIRONMENT,
        "-m", TICKET_REMARK,
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=TICKET_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return False, f"工单提交超时（{TICKET_TIMEOUT_SECONDS}s）"
    except OSError as error:
        return False, f"工单工具启动失败: {error}"
    summary = " ".join((result.stdout + "\n" + result.stderr).split())[:500]
    if result.returncode != 0:
        return False, f"退出码 {result.returncode}: {summary or '无输出'}"
    return True, summary or "无输出"
