"""Shared HTML rendering for the AI build analysis report."""

import html
import time


def build_ai_report(status: str, body: str, note: str = "") -> str:
    """Render the AI report page with a green success or red failure heading."""
    succeeded = status == "success"
    heading = "AB2 AI 成功报告" if succeeded else "AB2 AI 失败报告"
    color = "#22c55e" if succeeded else "#ef4444"
    note_html = f"<p>{html.escape(note)}</p>" if note else ""
    return (
        "<!doctype html><meta charset='utf-8'>"
        f"<title>{heading}</title>"
        f"<style>body{{background:#111827;color:#dbeafe;font:15px system-ui;padding:32px;line-height:1.7}}"
        f"pre{{white-space:pre-wrap}}h1{{color:{color}}}</style>"
        f"<h1>{heading}</h1>{note_html}<pre>{html.escape(body or '暂无分析结果')}</pre>"
    )


def build_diff_report(project_name: str, channel: str, branch: str, baseline_sha: str, commit_sha: str,
                      summary: str, detail: str, stat: str = "", note: str = "", count: int = 0) -> str:
    """Render the per-build change-notes page linked from the Feishu notification."""
    generated = time.strftime("%Y-%m-%d %H:%M:%S")
    # 所有外部内容都转义，避免提交信息里的尖括号破坏页面结构。
    summary_html = html.escape(summary or "（未生成 AI 概括）")
    detail_html = html.escape(detail or "（无提交明细）")
    stat_html = f"<p class='stat'>改动量：{html.escape(stat)}</p>" if stat else ""
    note_html = f"<p class='note'>{html.escape(note)}</p>" if note else ""
    baseline_short = html.escape((baseline_sha or "未知")[:10])
    commit_short = html.escape((commit_sha or "未知")[:10])
    return (
        "<!doctype html><meta charset='utf-8'>"
        "<title>AB2 差异说明</title>"
        "<style>body{background:#111827;color:#dbeafe;font:15px system-ui;padding:32px;line-height:1.7;max-width:960px;margin:0 auto}"
        "pre{white-space:pre-wrap}h1{color:#38bdf8}h2{color:#e5e7eb;font-size:16px;margin-top:24px}"
        ".meta{color:#9ca3af;font-size:13px}.stat{color:#9ca3af}.note{color:#fbbf24}</style>"
        "<h1>AB2 差异说明</h1>"
        f"<p class='meta'>工程：{html.escape(project_name)}　渠道：{html.escape(channel)}　分支：{html.escape(branch)}<br>"
        f"上次打包：{baseline_short} → 本次：{commit_short}<br>生成时间：{generated}</p>"
        f"<h2>本次重要变更</h2><pre>{summary_html}</pre>"
        f"<h2>提交明细（共 {count} 个提交，已排除合并提交）</h2>"
        f"<pre>{detail_html}</pre>"
        f"{stat_html}{note_html}"
    )
