"""Shared HTML rendering for the AI build analysis report."""

import html


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
