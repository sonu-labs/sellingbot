"""Helpers for importing standard SMM-panel service catalogs.

The bot's order flow accepts the common ``link + quantity`` API order shape.
Catalog entries that require comments, subscriptions, packages, or other
specialized inputs are intentionally reported as skipped instead of being
shown to customers as broken services.
"""

from __future__ import annotations

import math
import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

USD_TO_INR = Decimal("100")
INR_CURRENCIES = {"INR", "RS", "RUPEE", "RUPEES", "₹"}
USD_CURRENCIES = {"USD", "US$", "DOLLAR", "DOLLARS", "$"}
SUPPORTED_ORDER_TYPES = {"", "default", "standard", "basic", "normal"}

_PLATFORM_PATTERNS = {
    "telegram": re.compile(r"\b(?:telegram|tg)\b", re.IGNORECASE),
    "instagram": re.compile(r"\b(?:instagram|insta|ig)\b", re.IGNORECASE),
}


def detect_platform(service: dict[str, Any]) -> str | None:
    """Return the supported platform mentioned by a provider service, if any."""
    explicit = str(service.get("platform") or "").strip().casefold()
    if explicit in _PLATFORM_PATTERNS:
        return explicit

    category = str(service.get("category") or "")
    name = str(service.get("name") or "")
    text = f"{category} {name}"
    matches = [key for key, pattern in _PLATFORM_PATTERNS.items() if pattern.search(text)]
    return matches[0] if len(matches) == 1 else None


def _extract_rows(payload: Any) -> list[dict[str, Any]] | None:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if not isinstance(payload, dict):
        return None

    for key in ("services", "data", "result", "items"):
        value = payload.get(key)
        if isinstance(value, list):
            return [row for row in value if isinstance(row, dict)]
        if isinstance(value, dict):
            rows = [row for row in value.values() if isinstance(row, dict)]
            if rows:
                return rows

    if any(payload.get(key) not in (None, "") for key in ("service", "service_id", "id")):
        return [payload]

    # A few API-compatible panels return an object keyed by provider ID.
    rows = [row for row in payload.values() if isinstance(row, dict)]
    if rows and all(any(row.get(key) not in (None, "") for key in ("service", "service_id", "id")) for row in rows):
        return rows
    return None


def _first_value(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return None


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _integer(value: Any) -> int | None:
    number = _number(value)
    if number is None or not number.is_integer():
        return None
    return int(number)


def normalize_provider_services(payload: Any) -> tuple[list[dict[str, Any]], int, int] | None:
    """Normalize an API v2 ``services`` response.

    Returns ``(services, invalid_count, unsupported_type_count)``. ``None``
    means the response did not contain a recognizable catalog shape.
    """
    rows = _extract_rows(payload)
    if rows is None:
        return None

    services: list[dict[str, Any]] = []
    invalid_count = 0
    unsupported_count = 0
    for row in rows:
        service_id = _first_value(row, "service", "service_id", "id")
        description_value = _first_value(row, "description", "desc", "details")
        name = _first_value(row, "name", "title") or description_value
        rate = _number(_first_value(row, "rate", "price", "cost"))
        minimum = _integer(_first_value(row, "min", "minimum", "min_qty"))
        maximum = _integer(_first_value(row, "max", "maximum", "max_qty"))
        service_type = str(_first_value(row, "type", "order_type") or "").strip()

        if service_type.casefold() not in SUPPORTED_ORDER_TYPES:
            unsupported_count += 1
            continue
        if (
            service_id in (None, "")
            or not str(service_id).strip()
            or len(str(service_id).strip()) > 64
            or name in (None, "")
            or rate is None
            or rate <= 0
            or minimum is None
            or maximum is None
            or minimum <= 0
            or maximum < minimum
        ):
            invalid_count += 1
            continue

        services.append({
            "service_id": str(service_id).strip(),
            "name": str(name).strip()[:180],
            "description": str(description_value or "").strip()[:2000],
            "category": str(_first_value(row, "category", "group") or "").strip()[:180],
            "platform": str(row.get("platform") or "").strip(),
            "type": service_type or "Default",
            "rate": rate,
            "min": minimum,
            "max": maximum,
        })

    return services, invalid_count, unsupported_count


def normalize_currency(currency: Any, default: str = "USD") -> str:
    """Normalize the rate currency reported by a panel's balance endpoint."""
    value = str(currency or "").strip().upper()
    compact = re.sub(r"\s+", "", value)
    if compact in INR_CURRENCIES or "INR" in compact or "RUPEE" in compact or "₹" in compact:
        return "INR"
    if compact in USD_CURRENCIES or "USD" in compact or "DOLLAR" in compact:
        return "USD"
    return "INR" if str(default).upper() == "INR" else "USD"


def selling_rate_inr(provider_rate: Any, currency: Any = "USD", markup_percent: Any = 20) -> float:
    """Convert a provider rate per 1,000 to a customer-facing INR rate."""
    try:
        rate = Decimal(str(provider_rate))
        markup = Decimal(str(markup_percent))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("Provider rate and markup must be valid numbers") from None
    if not rate.is_finite() or rate <= 0:
        raise ValueError("Provider rate must be greater than zero")
    if not markup.is_finite() or markup < 0 or markup > 1000:
        raise ValueError("Markup must be between 0 and 1000 percent")

    multiplier = Decimal("1") if normalize_currency(currency) == "INR" else USD_TO_INR
    sell_rate = rate * multiplier * (Decimal("1") + markup / Decimal("100"))
    return float(sell_rate.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
