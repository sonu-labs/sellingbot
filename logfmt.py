"""Formatting helpers for virtual-number service labels and purchase logs."""
from html import escape


def role(_name: str) -> str:
    return ""


def app_html(name: str, emoji_id: str | None = None) -> str:
    safe_name = escape(str(name or ""))
    eid = str(emoji_id or "").strip()
    if eid:
        return f'<tg-emoji emoji-id="{eid}">📱</tg-emoji> {safe_name}'
    return safe_name
