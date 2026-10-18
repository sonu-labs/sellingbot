"""Helpers for SMM provider-panel settings and service descriptions."""

from __future__ import annotations

import re
from html import escape
from typing import Any

INR_CURRENCIES = {"INR", "RS", "RUPEE", "RUPEES", "₹"}
USD_CURRENCIES = {"USD", "US$", "DOLLAR", "DOLLARS", "$"}


def normalize_currency(currency: Any, default: str = "USD") -> str:
    """Normalize the rate currency reported by a panel's balance endpoint."""
    value = str(currency or "").strip().upper()
    compact = re.sub(r"\s+", "", value)
    if compact in INR_CURRENCIES or "INR" in compact or "RUPEE" in compact or "₹" in compact:
        return "INR"
    if compact in USD_CURRENCIES or "USD" in compact or "DOLLAR" in compact:
        return "USD"
    return "INR" if str(default).upper() == "INR" else "USD"


def render_description_block(description: Any, name: Any = "") -> str:
    """Render an admin-written service description as a safe HTML blockquote.

    The text is treated as plain text: HTML is escaped and line breaks are
    preserved. An empty description, or one that only repeats the service
    name, renders nothing.
    """
    text = str(description or "").strip()
    if not text or text.casefold() == str(name or "").strip().casefold():
        return ""
    lines = "\n".join(escape(line) for line in text.splitlines())
    if not lines:
        lines = escape(text)
    return f"<blockquote>📝 <b>Service description</b>\n{lines}</blockquote>"
