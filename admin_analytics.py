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
OTP_STATUSES = {"waiting", "completed", "cancelled", "unpaid", "allocating", "review", "refunding"}


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
    # New optional collections for premium report — backwards compat
    otp_orders: Iterable[dict[str, Any]] | None = None,
    all_transactions: Iterable[dict[str, Any]] | None = None,
    all_withdrawals: Iterable[dict[str, Any]] | None = None,
    otp_catalog: Iterable[dict[str, Any]] | None = None,
    otp_settings: dict[str, Any] | None = None,
    rate: float = 100.0,
    smm_checker_last_run: datetime | None = None,
    smm_checker_error: str | None = None,
) -> dict[str, Any]:
    """Summarize report rows using UTC day/week boundaries.

    Extended with OTP, overview, wallet extras, and attention metrics while
    keeping the original keyword-only signature for backwards compatibility.
    """
    current = utc_naive(now) if now is not None else datetime.utcnow()
    if current is None:
        current = datetime.utcnow()
    day_start = current.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = day_start - timedelta(days=current.weekday())
    yesterday_start = day_start - timedelta(days=1)
    yesterday_end = day_start
    last_week_start = week_start - timedelta(days=7)
    last_week_end = week_start

    users = list(user_docs)
    paid_transactions = [doc for doc in transactions if _status(doc) in PAID_TRANSACTION_STATUSES]
    paid_withdrawals = [doc for doc in withdrawals if _status(doc) in PAID_WITHDRAWAL_STATUSES]
    purchased_orders = [doc for doc in account_orders if _status(doc) in PURCHASED_ORDER_STATUSES]
    smm_rows = list(smm_orders)
    otp_rows = list(otp_orders) if otp_orders is not None else []
    # All transactions for pending counts if provided, else use filtered
    all_tx = list(all_transactions) if all_transactions is not None else list(transactions)
    all_wd = list(all_withdrawals) if all_withdrawals is not None else list(withdrawals)

    # --- User growth with yesterday/last-week ---
    users_today = sum(1 for doc in users if (utc_naive(doc.get("joined_at")) or datetime.min) >= day_start)
    users_week = sum(1 for doc in users if (utc_naive(doc.get("joined_at")) or datetime.min) >= week_start)
    users_yesterday = sum(1 for doc in users if (utc_naive(doc.get("joined_at")) or datetime.min) >= yesterday_start and (utc_naive(doc.get("joined_at")) or datetime.min) < yesterday_end)
    users_last_week = sum(1 for doc in users if (utc_naive(doc.get("joined_at")) or datetime.min) >= last_week_start and (utc_naive(doc.get("joined_at")) or datetime.min) < last_week_end)
    # % deltas
    def _delta_pct(current_val, previous_val):
        if previous_val == 0:
            return 0.0 if current_val == 0 else 100.0
        return ((current_val - previous_val) / previous_val) * 100

    users_yesterday_delta = _delta_pct(users_today, users_yesterday)
    users_week_delta = _delta_pct(users_week, users_last_week)

    # Active buyers: users with >=1 paid order (account or OTP or SMM non-refunded)
    buyer_ids = set()
    for doc in purchased_orders:
        uid = doc.get("user_id") or doc.get("userId") or doc.get("userID")
        if uid is not None:
            buyer_ids.add(str(uid))
    for doc in otp_rows:
        # Consider waiting/completed as active; exclude cancelled/unpaid/refunded
        if _status(doc) in ("waiting", "completed", "allocating", "review", "refunding") and not doc.get("refunded"):
            uid = doc.get("user_id") or doc.get("userId")
            if uid is not None:
                buyer_ids.add(str(uid))
            elif doc.get("price"):
                # If no user_id but price present, still count as buyer via distinct?
                pass
    # For SMM, count non-refunded
    for doc in smm_rows:
        if not doc.get("refunded"):
            uid = doc.get("user_id") or doc.get("userId")
            if uid is not None:
                buyer_ids.add(str(uid))
    active_buyers = len(buyer_ids)
    # Fallback: if no user_id tracking in orders, estimate from order counts
    if active_buyers == 0 and (len(purchased_orders) + len([d for d in otp_rows if not d.get("refunded")])) > 0:
        active_buyers = min(total_users, len(purchased_orders) + len([d for d in otp_rows if _status(d) in ("waiting", "completed")]))
    conversion_rate = (active_buyers / total_users * 100) if total_users else 0.0

    # Top-up users: distinct users with paid deposit
    topup_ids = set()
    for doc in paid_transactions:
        uid = doc.get("user_id") or doc.get("userId")
        if uid is not None:
            topup_ids.add(str(uid))
    topup_users = len(topup_ids) if topup_ids else len(paid_transactions)  # fallback count

    # --- Wallet extras ---
    account_today = [doc for doc in purchased_orders if _date_in_range(doc, day_start)]
    account_week = [doc for doc in purchased_orders if _date_in_range(doc, week_start)]
    deposit_today = [doc for doc in paid_transactions if _date_in_range(doc, day_start)]
    deposit_week = [doc for doc in paid_transactions if _date_in_range(doc, week_start)]
    deposit_yesterday = [doc for doc in paid_transactions if utc_naive(doc.get("created_at")) is not None and yesterday_start <= utc_naive(doc.get("created_at")) < yesterday_end]
    smm_today = [doc for doc in smm_rows if _date_in_range(doc, day_start)]
    smm_week = [doc for doc in smm_rows if _date_in_range(doc, week_start)]

    # Pending recharges/withdrawals
    pending_deposits = sum(1 for doc in all_tx if _status(doc) in ("pending", "awaiting", "processing"))
    pending_withdrawals = sum(1 for doc in all_wd if _status(doc) in ("pending", "awaiting", "processing"))
    # Withdrawals today/week if we have created_at
    withdrawal_today = sum(1 for doc in paid_withdrawals if _date_in_range(doc, day_start))
    withdrawal_week = sum(1 for doc in paid_withdrawals if _date_in_range(doc, week_start))
    withdrawal_today_amount = sum(safe_amount(doc.get("amount")) for doc in paid_withdrawals if _date_in_range(doc, day_start))
    withdrawal_week_amount = sum(safe_amount(doc.get("amount")) for doc in paid_withdrawals if _date_in_range(doc, week_start))
    net_flow = sum(safe_amount(doc.get("amount")) for doc in paid_transactions) - sum(safe_amount(doc.get("amount")) for doc in paid_withdrawals)
    avg_deposit = (sum(safe_amount(doc.get("amount")) for doc in paid_transactions) / len(paid_transactions)) if paid_transactions else 0.0

    countries = Counter(
        str(doc.get("country") or "Unknown") for doc in purchased_orders
    )
    # Top 3 countries
    top_countries = countries.most_common(3)
    # For compatibility, top_country is first
    top_country = top_countries[0][0] if top_countries else "N/A"

    # Account avg order value
    account_avg_value = (sum(safe_amount(doc.get("price")) for doc in purchased_orders) / len(purchased_orders)) if purchased_orders else 0.0
    account_refunded = sum(1 for doc in account_orders if doc.get("refunded") is True or _status(doc) in ("refunded", "cancelled"))
    account_cancelled = sum(1 for doc in account_orders if _status(doc) in ("cancelled", "refunded"))

    # SMM metrics
    smm_gross = sum(safe_amount(doc.get("price")) for doc in smm_rows)
    smm_refunds = sum(
        safe_amount(doc.get("price")) for doc in smm_rows if doc.get("refunded") is True
    )
    active_smm = sum(
        1
        for doc in smm_rows
        if not doc.get("refunded") and str(doc.get("status_norm") or _status(doc)) in OPEN_SMM_STATUSES
    )
    smm_avg_value = (smm_gross / len(smm_rows)) if smm_rows else 0.0
    # Top SMM category/service (if available)
    smm_categories = Counter(str(doc.get("category") or doc.get("service") or "Unknown") for doc in smm_rows if doc.get("category") or doc.get("service"))
    smm_top_category = smm_categories.most_common(1)[0][0] if smm_categories else "N/A"

    # --- OTP metrics ---
    otp_total = len(otp_rows)
    # Filter for today/week
    otp_today = [d for d in otp_rows if _date_in_range(d, day_start)]
    otp_week = [d for d in otp_rows if _date_in_range(d, week_start)]
    # Gross / refunded (exclude refunded from net)
    otp_gross = sum(safe_amount(d.get("price")) for d in otp_rows)
    otp_refunded_amount = sum(safe_amount(d.get("price")) for d in otp_rows if d.get("refunded") is True or _status(d) == "cancelled")
    otp_refunded_count = sum(1 for d in otp_rows if d.get("refunded") is True or _status(d) == "cancelled")
    otp_net = otp_gross - otp_refunded_amount
    otp_today_count = len(otp_today)
    otp_today_revenue = sum(safe_amount(d.get("price")) for d in otp_today if d.get("refunded") is not True and _status(d) != "cancelled")
    otp_week_count = len(otp_week)
    otp_week_revenue = sum(safe_amount(d.get("price")) for d in otp_week if d.get("refunded") is not True and _status(d) != "cancelled")
    # Provider cost totals
    otp_cost_total = sum(safe_amount(d.get("cost")) for d in otp_rows if d.get("cost") is not None)
    # live_cost missing on legacy orders — fallback to cost
    otp_live_cost_total = sum(safe_amount(d.get("live_cost") if d.get("live_cost") is not None else d.get("cost")) for d in otp_rows)
    # Margin in INR: retail price (INR) - cost USD * rate
    otp_provider_cost_inr = otp_cost_total * float(rate)
    otp_margin = otp_net - otp_provider_cost_inr
    otp_margin_percent = (otp_margin / otp_net * 100) if otp_net else 0.0
    # Statuses needing attention
    otp_waiting = sum(1 for d in otp_rows if _status(d) == "waiting")
    otp_allocating = sum(1 for d in otp_rows if _status(d) == "allocating")
    otp_review = sum(1 for d in otp_rows if _status(d) == "review")
    otp_refunding = sum(1 for d in otp_rows if _status(d) == "refunding")
    otp_unpaid = sum(1 for d in otp_rows if _status(d) == "unpaid")
    otp_completed = sum(1 for d in otp_rows if _status(d) == "completed")
    otp_cancelled = sum(1 for d in otp_rows if _status(d) == "cancelled")
    # Stale allocating/review older than 10 min
    stale_threshold = current - timedelta(minutes=10)
    otp_stale = sum(1 for d in otp_rows if _status(d) in ("allocating", "review") and (utc_naive(d.get("created_at")) or datetime.max) < stale_threshold)
    # Price mismatches from WRONG_MAX_PRICE corrections
    otp_price_mismatches = sum(1 for d in otp_rows if d.get("provider_reported_price") is not None)
    # Top app / country / line
    otp_apps = Counter(str(d.get("service_name") or d.get("service") or "Unknown") for d in otp_rows)
    otp_countries = Counter(str(d.get("country_name") or d.get("country") or "Unknown") for d in otp_rows)
    otp_routes = Counter(str(d.get("route") or d.get("route_type") or d.get("operator") or "Unknown") for d in otp_rows)
    otp_top_app = otp_apps.most_common(1)[0][0] if otp_apps else "N/A"
    otp_top_country = otp_countries.most_common(1)[0][0] if otp_countries else "N/A"
    otp_top_route = otp_routes.most_common(1)[0][0] if otp_routes else "N/A"
    # Catalog cache age + enabled lines
    catalog_age = None
    catalog_count = 0
    if otp_catalog is not None:
        try:
            docs = list(otp_catalog)
            catalog_count = len(docs)
            if docs:
                # Find most recent fetched_at
                latest = None
                for doc in docs:
                    ft = utc_naive(doc.get("fetched_at"))
                    if ft and (latest is None or ft > latest):
                        latest = ft
                if latest:
                    catalog_age = (current - latest).total_seconds()
        except Exception:
            pass
    enabled_lines = []
    if otp_settings is not None:
        try:
            # otp_settings is effective_settings dict
            enabled_lines = list(otp_settings.get("routes", []))
        except Exception:
            enabled_lines = []

    # Overview total revenue across verticals
    account_revenue_total = sum(safe_amount(doc.get("price")) for doc in purchased_orders)
    smm_net_total = smm_gross - smm_refunds
    # Use otp_net for OTP contribution
    total_revenue = account_revenue_total + smm_net_total + otp_net
    total_today_revenue = sum(safe_amount(doc.get("price")) for doc in account_today if doc.get("refunded") is not True) + \
                          sum(safe_amount(doc.get("price")) for doc in smm_today if doc.get("refunded") is not True) + \
                          otp_today_revenue
    total_week_revenue = sum(safe_amount(doc.get("price")) for doc in account_week if doc.get("refunded") is not True) + \
                         sum(safe_amount(doc.get("price")) for doc in smm_week if doc.get("refunded") is not True) + \
                         otp_week_revenue
    # Share %
    def _share(part, total):
        return (part / total * 100) if total else 0.0
    account_share = _share(account_revenue_total, total_revenue)
    smm_share = _share(smm_net_total, total_revenue)
    otp_share = _share(otp_net, total_revenue)

    # Attention alerts (list of strings)
    alerts = []
    if otp_stale:
        alerts.append(f"{otp_stale} OTP order(s) in allocating/review older than 10m")
    if otp_price_mismatches:
        alerts.append(f"{otp_price_mismatches} price mismatch(es) from WRONG_MAX_PRICE")
    if otp_refunding:
        alerts.append(f"{otp_refunding} OTP refund(s) pending")
    if active_smm is not None and smm_checker_last_run is not None:
        try:
            age = (current - utc_naive(smm_checker_last_run)).total_seconds() if smm_checker_last_run else 999999
            if age > 600:
                alerts.append("SMM monitor stale (>10m since last cycle)")
        except Exception:
            pass
    if smm_checker_error:
        alerts.append(f"Last SMM cycle error: {smm_checker_error[:80]}")
    if catalog_age is not None and catalog_age > 3600:
        alerts.append(f"OTP catalog cache stale ({int(catalog_age//60)}m old)")
    if pending_deposits:
        alerts.append(f"{pending_deposits} pending recharge(s) awaiting approval")
    if pending_withdrawals:
        alerts.append(f"{pending_withdrawals} withdrawal(s) awaiting approval")

    return {
        "total_users": max(0, int(total_users or 0)),
        "users_today": users_today,
        "users_week": users_week,
        "users_yesterday": users_yesterday,
        "users_last_week": users_last_week,
        "users_today_delta": users_yesterday_delta,
        "users_week_delta": users_week_delta,
        "active_buyers": active_buyers,
        "conversion_rate": conversion_rate,
        "topup_users": topup_users,
        "deposit_total": sum(safe_amount(doc.get("amount")) for doc in paid_transactions),
        "deposit_today": sum(safe_amount(doc.get("amount")) for doc in deposit_today),
        "deposit_week": sum(safe_amount(doc.get("amount")) for doc in deposit_week),
        "deposit_yesterday": sum(safe_amount(doc.get("amount")) for doc in deposit_yesterday),
        "withdrawal_total": sum(safe_amount(doc.get("amount")) for doc in paid_withdrawals),
        "withdrawal_today": withdrawal_today,
        "withdrawal_week": withdrawal_week,
        "withdrawal_today_amount": withdrawal_today_amount,
        "withdrawal_week_amount": withdrawal_week_amount,
        "pending_deposits": pending_deposits,
        "pending_withdrawals": pending_withdrawals,
        "net_flow": net_flow,
        "avg_deposit": avg_deposit,
        "account_orders": len(purchased_orders),
        "account_revenue": account_revenue_total,
        "account_today_count": len(account_today),
        "account_today_revenue": sum(safe_amount(doc.get("price")) for doc in account_today),
        "account_week_count": len(account_week),
        "account_week_revenue": sum(safe_amount(doc.get("price")) for doc in account_week),
        "account_avg_value": account_avg_value,
        "account_refunded": account_refunded,
        "account_cancelled": account_cancelled,
        "top_country": top_country,
        "top_countries": top_countries,
        "smm_orders": len(smm_rows),
        "smm_gross": smm_gross,
        "smm_refunds": smm_refunds,
        "smm_net": smm_gross - smm_refunds,
        "smm_avg_value": smm_avg_value,
        "smm_top_category": smm_top_category,
        "smm_today_count": len(smm_today),
        "smm_today_net": sum(
            safe_amount(doc.get("price")) for doc in smm_today if doc.get("refunded") is not True
        ),
        "smm_week_count": len(smm_week),
        "smm_week_net": sum(
            safe_amount(doc.get("price")) for doc in smm_week if doc.get("refunded") is not True
        ),
        "smm_open_orders": active_smm,
        "otp_orders": otp_total,
        "otp_gross": otp_gross,
        "otp_refunded_amount": otp_refunded_amount,
        "otp_refunded_count": otp_refunded_count,
        "otp_net": otp_net,
        "otp_today_count": otp_today_count,
        "otp_today_revenue": otp_today_revenue,
        "otp_week_count": otp_week_count,
        "otp_week_revenue": otp_week_revenue,
        "otp_cost_total": otp_cost_total,
        "otp_live_cost_total": otp_live_cost_total,
        "otp_provider_cost_inr": otp_provider_cost_inr,
        "otp_margin": otp_margin,
        "otp_margin_percent": otp_margin_percent,
        "otp_waiting": otp_waiting,
        "otp_allocating": otp_allocating,
        "otp_review": otp_review,
        "otp_refunding": otp_refunding,
        "otp_unpaid": otp_unpaid,
        "otp_completed": otp_completed,
        "otp_cancelled": otp_cancelled,
        "otp_stale": otp_stale,
        "otp_price_mismatches": otp_price_mismatches,
        "otp_top_app": otp_top_app,
        "otp_top_country": otp_top_country,
        "otp_top_route": otp_top_route,
        "otp_catalog_count": catalog_count,
        "otp_catalog_age": catalog_age,
        "otp_enabled_lines": enabled_lines,
        "total_revenue": total_revenue,
        "total_today_revenue": total_today_revenue,
        "total_week_revenue": total_week_revenue,
        "account_share": account_share,
        "smm_share": smm_share,
        "otp_share": otp_share,
        "alerts": alerts,
        "day_start": day_start,
        "week_start": week_start,
        "yesterday_start": yesterday_start,
        "current": current,
    }
