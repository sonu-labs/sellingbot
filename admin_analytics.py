"""Small, defensive analytics helpers shared by the admin reports.

MongoDB data is long-lived and may contain older or manually edited records,
so report generation must tolerate missing, string, malformed, and timezone-
aware values instead of letting one bad row break the whole admin screen.
"""

from __future__ import annotations

import math
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable


PAID_TRANSACTION_STATUSES = {"paid", "success", "approved"}
PAID_WITHDRAWAL_STATUSES = {"paid"}
PURCHASED_ORDER_STATUSES = {"purchased"}
OPEN_SMM_STATUSES = {"pending", "in_progress", "placing", "unknown"}


def safe_amount(value: Any) -> float:
    """Return a finite float for numeric Mongo values, or zero for bad data."""
    if hasattr(value, "to_decimal"):
        try:
            value = value.to_decimal()
        except Exception:
            return 0.0
    try:
        amount = float(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return amount if math.isfinite(amount) else 0.0


def utc_naive(value: Any) -> datetime | None:
    """Convert a datetime to naive UTC for comparisons with PyMongo dates."""
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _status(doc: dict[str, Any]) -> str:
    return str(doc.get("status") or "").strip().casefold()


def _date_in_range(doc: dict[str, Any], start: datetime) -> bool:
    created_at = utc_naive(doc.get("created_at"))
    return created_at is not None and created_at >= start


def summarize_analytics(
    *,
    total_users: int,
    user_docs: Iterable[dict[str, Any]],
    transactions: Iterable[dict[str, Any]],
    withdrawals: Iterable[dict[str, Any]],
    account_orders: Iterable[dict[str, Any]],
    smm_orders: Iterable[dict[str, Any]],
    now: datetime | None = None,
) -> dict[str, Any]:
    """Summarize report rows using UTC day/week boundaries."""
    current = utc_naive(now) if now is not None else datetime.utcnow()
    if current is None:
        current = datetime.utcnow()
    day_start = current.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = day_start - timedelta(days=current.weekday())

    users = list(user_docs)
    paid_transactions = [doc for doc in transactions if _status(doc) in PAID_TRANSACTION_STATUSES]
    paid_withdrawals = [doc for doc in withdrawals if _status(doc) in PAID_WITHDRAWAL_STATUSES]
    purchased_orders = [doc for doc in account_orders if _status(doc) in PURCHASED_ORDER_STATUSES]
    smm_rows = list(smm_orders)

    account_today = [doc for doc in purchased_orders if _date_in_range(doc, day_start)]
    account_week = [doc for doc in purchased_orders if _date_in_range(doc, week_start)]
    deposit_today = [doc for doc in paid_transactions if _date_in_range(doc, day_start)]
    deposit_week = [doc for doc in paid_transactions if _date_in_range(doc, week_start)]
    smm_today = [doc for doc in smm_rows if _date_in_range(doc, day_start)]
    smm_week = [doc for doc in smm_rows if _date_in_range(doc, week_start)]

    countries = Counter(
        str(doc.get("country") or "Unknown") for doc in purchased_orders
    )
    smm_gross = sum(safe_amount(doc.get("price")) for doc in smm_rows)
    smm_refunds = sum(
        safe_amount(doc.get("price")) for doc in smm_rows if doc.get("refunded") is True
    )
    active_smm = sum(
        1
        for doc in smm_rows
        if not doc.get("refunded") and str(doc.get("status_norm") or _status(doc)) in OPEN_SMM_STATUSES
    )

    return {
        "total_users": max(0, int(total_users or 0)),
        "users_today": sum(1 for doc in users if (utc_naive(doc.get("joined_at")) or datetime.min) >= day_start),
        "users_week": sum(1 for doc in users if (utc_naive(doc.get("joined_at")) or datetime.min) >= week_start),
        "deposit_total": sum(safe_amount(doc.get("amount")) for doc in paid_transactions),
        "deposit_today": sum(safe_amount(doc.get("amount")) for doc in deposit_today),
        "deposit_week": sum(safe_amount(doc.get("amount")) for doc in deposit_week),
        "withdrawal_total": sum(safe_amount(doc.get("amount")) for doc in paid_withdrawals),
        "account_orders": len(purchased_orders),
        "account_revenue": sum(safe_amount(doc.get("price")) for doc in purchased_orders),
        "account_today_count": len(account_today),
        "account_today_revenue": sum(safe_amount(doc.get("price")) for doc in account_today),
        "account_week_count": len(account_week),
        "account_week_revenue": sum(safe_amount(doc.get("price")) for doc in account_week),
        "top_country": countries.most_common(1)[0][0] if countries else "N/A",
        "smm_orders": len(smm_rows),
        "smm_gross": smm_gross,
        "smm_refunds": smm_refunds,
        "smm_net": smm_gross - smm_refunds,
        "smm_today_count": len(smm_today),
        "smm_today_net": sum(
            safe_amount(doc.get("price")) for doc in smm_today if doc.get("refunded") is not True
        ),
        "smm_week_count": len(smm_week),
        "smm_week_net": sum(
            safe_amount(doc.get("price")) for doc in smm_week if doc.get("refunded") is not True
        ),
        "smm_open_orders": active_smm,
        "day_start": day_start,
        "week_start": week_start,
    }
