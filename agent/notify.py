"""Feishu bot notifications for finished builds."""

import json
import time
import urllib.request


def format_duration(seconds: float) -> str:
    """Format build duration as X分Y秒 for the notification text."""
    total = max(0, int(seconds))
    return f"{total // 60}分{total % 60}秒"


def build_feishu_text(status: str, project_name: str, channel: str, branch: str, commit_sha: str,
                      duration: float, error_message: str = "", manager_url: str = "") -> str:
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
    if manager_url:
        lines.append(f"管理页面：{manager_url}")
    return "\n".join(lines)


def send_feishu_text(webhook: str, text: str) -> None:
    """POST one text message to a Feishu custom bot webhook."""
    payload = {"msg_type": "text", "content": {"text": text}}
    request = urllib.request.Request(
        webhook,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        body = response.read().decode("utf-8", errors="replace")
    # Feishu replies with a JSON code; a non-zero code means the bot rejected the message.
    try:
        result = json.loads(body)
    except json.JSONDecodeError:
        result = {}
    if isinstance(result, dict) and result.get("code") not in (0, None):
        raise RuntimeError(f"feishu rejected the message: {result.get('code')} {result.get('msg', '')}")
