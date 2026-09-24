"""Feishu bot notifications for finished builds."""

import json
import time
import urllib.request


# 飞书自定义机器人文本消息有长度上限，超出可能被拒绝，这里做保守封顶。
MAX_NOTIFY_CHARS = 4000
MAX_SUMMARY_CHARS = 1000
MAX_DETAIL_LINES = 20
TRUNCATED_SUFFIX = "\n…（消息过长已截断）"


def format_duration(seconds: float) -> str:
    """Format build duration as X分Y秒 for the notification text."""
    total = max(0, int(seconds))
    return f"{total // 60}分{total % 60}秒"


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
