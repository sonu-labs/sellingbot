"""SMM reseller module for aiogram 3."""

import asyncio
import logging
import math
import os
import re
from datetime import datetime, timedelta, timezone
from html import escape
from urllib.parse import urlparse

import aiohttp
from aiogram import F
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from bson import ObjectId
from pymongo import ReturnDocument, UpdateOne

from smm_catalog import (
    detect_platform,
    normalize_currency,
    normalize_provider_services,
    selling_rate_inr,
)

logger = logging.getLogger("smm")

# The standard API endpoint is a convenience preset; administrators save the
# actual panel URL and API key through the protected SMM admin workflow.
NICE_PANEL_API_URL = "https://nicesmmpanel.com/api/v2"
USD_TO_INR = 100.0
PUBLIC_SERVICES_PER_PAGE = 10
ADMIN_SERVICES_PER_PAGE = 10
PLATFORM_PRESETS = (
    {"slug": "telegram", "name": "Telegram", "emoji": "✈️"},
    {"slug": "instagram", "name": "Instagram", "emoji": "📸"},
)
BOT_USERNAME = os.getenv("SMM_BOT_USERNAME", os.getenv("BOT_USERNAME", "TGAStoreAPIBot")).lstrip("@")
UPDATES_CHANNEL = os.getenv("UPDATES_CHANNEL", "TGAForu").lstrip("@")

CHECK_EVERY_SECONDS = 60
NOTIFY_BACKFILL_HOURS = 72
PARTIAL_POLL_DAYS = 7
STALE_POLL_DAYS = 21
API_TIMEOUT_SECONDS = 25

TERMINAL_NORMS = ("completed", "partial", "canceled", "failed")
REFUND_NORMS = ("canceled", "failed")
STATUS_TEXT = {
    "placing": "Placing",
    "pending": "Pending",
    "in_progress": "In progress",
    "completed": "Completed",
    "partial": "Partial",
    "canceled": "Canceled",
    "failed": "Failed",
    "unknown": "Unknown",
}


class SMMAdmin(StatesGroup):
    waiting_panel_name = State()
    waiting_panel_url = State()
    waiting_panel_key = State()
    waiting_panel_update_key = State()
    waiting_markup = State()
    waiting_service_provider_id = State()
    waiting_service_name = State()
    waiting_service_price = State()
    waiting_service_min = State()
    waiting_service_max = State()
    waiting_edit_price = State()
    waiting_admin_order_id = State()


class SMMUser(StatesGroup):
    waiting_quantity = State()
    waiting_link = State()
    waiting_status_order_id = State()


def h(value) -> str:
    if value is None or value == "":
        return "—"
    return escape(str(value))


def clip(text: str, limit: int = 3900) -> str:
    """Never cut through an HTML tag. A broken tag makes Telegram drop all formatting."""
    if len(text) <= limit:
        return text
    cut = text.rfind("</blockquote>", 0, limit)
    if cut != -1:
        cut += len("</blockquote>")
    else:
        cut = text.rfind("\n", 0, limit)
        if cut == -1:
            cut = limit
    return text[:cut] + "\n\n<i>Older rows are hidden so this message stays readable.</i>"


def link_html(url: str) -> str:
    if not url:
        return "—"
    shown = url if len(url) <= 48 else url[:45] + "…"
    return f'<a href="{h(url)}">{h(shown)}</a>'


def normalize_api_url(raw: str) -> str | None:
    """Accept only absolute HTTPS panel endpoints so API keys stay encrypted in transit."""
    url = (raw or "").strip()
    if not url or len(url) > 500 or any(ch.isspace() for ch in url):
        return None
    try:
        parsed = urlparse(url)
    except Exception:
        return None
    if parsed.scheme.lower() != "https" or not parsed.hostname or parsed.username or parsed.password:
        return None
    if parsed.fragment:
        return None
    return url.rstrip("/")


def naive_utc(value) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def card(*lines) -> str:
    body = "\n".join(line for line in lines if line)
    return f"<blockquote>{body}</blockquote>"


def inr(amount) -> str:
    try:
        return f"₹{float(amount):,.2f}"
    except (TypeError, ValueError):
        return "₹0.00"


def qty_fmt(value) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return h(value)


def strip_tags(text: str) -> str:
    plain = re.sub(r"<[^>]+>", "", text or "")
    return (
        plain.replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
    )


def normalize_status(status: str) -> str:
    s = (status or "").strip().lower()
    if "complet" in s or s in ("success", "done", "successful"):
        return "completed"
    if "partial" in s:
        return "partial"
    if "cancel" in s or "refund" in s:
        return "canceled"
    if "fail" in s or s in ("error", "rejected"):
        return "failed"
    if "progress" in s or "process" in s:
        return "in_progress"
    if "pending" in s or "await" in s or "queue" in s:
        return "pending"
    if "plac" in s:
        return "placing"
    return s or "unknown"


def status_label(status: str) -> str:
    norm = normalize_status(status)
    if norm in STATUS_TEXT and norm != "unknown":
        return STATUS_TEXT[norm]
    return (status or "Unknown").strip() or "Unknown"


def status_emoji(status: str) -> str:
    s = (status or "").lower()
    if "complet" in s:
        return "✅"
    if "partial" in s:
        return "⚠️"
    if any(k in s for k in ("progress", "process", "pending", "plac", "queue", "await")):
        return "🔄"
    if any(k in s for k in ("cancel", "refund", "fail", "error")):
        return "❌"
    return "❔"


def explain_status(norm: str) -> str:
    return {
        "completed": "The provider marked this order complete. The quantity should now be delivered.",
        "partial": "Only part of the quantity was delivered. Remains is what was not delivered. Contact support if you were charged for the rest.",
        "canceled": "The provider canceled this order. The charge is returned to your balance automatically.",
        "failed": "The provider failed this order. The charge is returned to your balance automatically.",
        "in_progress": "Delivery is in progress. Status is checked automatically about every minute.",
        "pending": "The order is queued and has not started yet.",
        "placing": "The order is being sent to the provider.",
        "unknown": "This status came from the provider. Tap Refresh in a moment.",
    }.get(norm, "The latest status has been received from the provider.")


def normalize_link(raw: str):
    link = (raw or "").strip()
    if link.startswith("t.me/") or link.startswith("telegram.me/"):
        link = "https://" + link
    if not link.startswith(("http://", "https://")):
        return None
    if len(link) > 500 or any(ch.isspace() for ch in link):
        return None
    return link


def public_link_label(platform_name: str) -> str:
    """Channel log only: platform name, then a hidden marker. Full URL is never posted."""
    name = (platform_name or "Link").strip()
    return f"{name} [hidden 🫥]"


def button_label(text: str, limit: int = 60) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def quantity_presets(min_qty: int, max_qty: int):
    seeds = [min_qty, 50, 100, 250, 500, 1000, 2000, 5000, 10000, max_qty]
    presets = []
    for qty in seeds:
        if min_qty <= qty <= max_qty and qty not in presets:
            presets.append(qty)
    return presets[:6]


def age_label(when) -> str:
    when = naive_utc(when)
    if when is None:
        return "never"
    seconds = max(0, int((datetime.utcnow() - when).total_seconds()))
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60} min ago"
    if seconds < 86400:
        return f"{seconds // 3600} hr ago"
    return f"{seconds // 86400}d ago"


async def smm_api_call(panel: dict, action: str, **params):
    """Returns (success, data_or_error, raw_text). Never raises."""
    if not panel or not panel.get("api_url") or not panel.get("api_key"):
        return False, {"error": "Panel URL or API key missing"}, "Panel not configured"
    if normalize_api_url(panel.get("api_url")) is None:
        return False, {"error": "Panel API URL must use HTTPS"}, "Unsafe or invalid panel URL"
    payload = {"key": panel["api_key"], "action": action}
    for key, value in params.items():
        if value is not None:
            payload[key] = value
    timeout = aiohttp.ClientTimeout(total=API_TIMEOUT_SECONDS)
    last_error = "Unknown error"
    for attempt in range(2):
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(panel["api_url"], data=payload) as resp:
                    raw_text = await resp.text()
                    try:
                        data = await resp.json(content_type=None)
                    except Exception:
                        return False, {"error": "Invalid JSON response"}, raw_text
                    if isinstance(data, dict) and data.get("error"):
                        return False, data, raw_text
                    return True, data, raw_text
        except Exception as exc:
            last_error = str(exc)
            logger.warning("SMM API %s failed (%s/2): %s", action, attempt + 1, exc)
            await asyncio.sleep(1)
    return False, {"error": last_error}, f"Exception: {last_error}"


def parse_status_map(data, provider_ids):
    """Normalize a single-order or multi-order status payload into {id: dict}."""
    found = {}
    if not isinstance(data, dict):
        return found
    if "status" in data and len(provider_ids) == 1:
        found[str(provider_ids[0])] = data
        return found
    for pid in provider_ids:
        item = data.get(str(pid))
        if item is None:
            item = data.get(pid)
        if isinstance(item, dict):
            found[str(pid)] = item
    if not found and "status" in data and len(provider_ids) == 1:
        found[str(provider_ids[0])] = data
    return found


def register_smm_handlers(dp, bot, db, users_col, admin_ids, admin_log_id, public_log_id):
    smm_platforms = db["smm_platforms"]
    smm_services = db["smm_services"]
    smm_orders = db["smm_orders"]
    smm_settings = db["smm_settings"]
    smm_panels = db["smm_panels"]
    _placing_users = set()

    def platform_slug(name: str) -> str | None:
        compact = re.sub(r"[^a-z]", "", str(name or "").casefold())
        if "instagram" in compact:
            return "instagram"
        if "telegram" in compact:
            return "telegram"
        return None

    def ensure_builtin_platforms():
        """Seed the two supported SMM categories without deleting legacy rows."""
        existing = list(smm_platforms.find({}))
        for preset in PLATFORM_PRESETS:
            slug = preset["slug"]
            candidate = next((doc for doc in existing if doc.get("slug") == slug), None)
            if candidate is None:
                candidate = next((doc for doc in existing if platform_slug(doc.get("name")) == slug), None)
            fields = {"slug": slug, "name": preset["name"], "emoji": preset["emoji"]}
            if candidate:
                smm_platforms.update_one({"_id": candidate["_id"]}, {"$set": fields})
            else:
                result = smm_platforms.insert_one({**fields, "created_at": datetime.utcnow()})
                existing.append({"_id": result.inserted_id, **fields})

    ensure_builtin_platforms()

    def supported_platforms():
        rows = list(smm_platforms.find({"slug": {"$in": [p["slug"] for p in PLATFORM_PRESETS]}}))
        by_slug = {}
        for row in rows:
            slug = row.get("slug")
            if slug in {p["slug"] for p in PLATFORM_PRESETS} and slug not in by_slug:
                by_slug[slug] = row
        return [by_slug[preset["slug"]] for preset in PLATFORM_PRESETS if preset["slug"] in by_slug]

    def supported_platform_by_slug(slug: str):
        if slug not in {preset["slug"] for preset in PLATFORM_PRESETS}:
            return None
        return smm_platforms.find_one({"slug": slug})

    def get_import_markup() -> float:
        cfg = get_config()
        try:
            value = float(cfg.get("import_markup_percent", 20.0))
        except (TypeError, ValueError):
            value = 20.0
        return min(1000.0, max(0.0, value))

    def is_admin(uid: int) -> bool:
        return uid in admin_ids

    def get_config() -> dict:
        doc = smm_settings.find_one({"_id": "config"}) or {"_id": "config"}
        if "import_markup_percent" not in doc:
            doc["import_markup_percent"] = 20.0
            smm_settings.update_one(
                {"_id": "config"}, {"$set": {"import_markup_percent": 20.0}}, upsert=True
            )
        return doc

    def public_log_enabled() -> bool:
        return get_config().get("public_log_enabled", True)

    def smm_enabled() -> bool:
        feature_doc = db["settings"].find_one({"_id": "bot_features"}) or {}
        if "smm_enabled" in feature_doc:
            return bool(feature_doc["smm_enabled"])
        return bool(get_config().get("smm_enabled", True))

    def get_balance_inr(uid: int) -> float:
        user = users_col.find_one({"_id": uid}) or {}
        try:
            return float(user.get("balance", 0.0) or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def get_user_discount(uid: int) -> float:
        if not uid:
            return 0.0
        user = users_col.find_one({"_id": uid}) or {}
        try:
            pct = float(user.get("discount_percent", 0.0) or 0.0)
        except (TypeError, ValueError):
            pct = 0.0
        return max(0.0, min(100.0, pct))

    def get_service_rate_1k(svc: dict, uid: int = None) -> float:
        base = float(svc.get("price_per_1000", 0.0) or 0.0)
        pct = get_user_discount(uid)
        if pct <= 0:
            return round(base, 2)
        return round(base * (1.0 - pct / 100.0), 2)

    def get_panel_for_service(svc: dict):
        pid = svc.get("panel_id") if svc else None
        if pid:
            panel = smm_panels.find_one({"_id": pid})
            if panel:
                return panel
        return smm_panels.find_one({})

    def panel_by_id(pid):
        if not pid:
            return smm_panels.find_one({})
        return smm_panels.find_one({"_id": pid}) or smm_panels.find_one({})

    async def import_provider_catalog(panel: dict, mode: str) -> dict:
        """Import compatible API v2 services into Telegram/Instagram categories."""
        ok, payload, raw = await smm_api_call(panel, "services")
        if not ok:
            return {"ok": False, "error": str((payload or {}).get("error") or "Provider request failed")}
        parsed = normalize_provider_services(payload)
        if parsed is None:
            return {"ok": False, "error": "The provider did not return a recognizable services list."}
        services, invalid_count, unsupported_count = parsed
        platform_docs = {doc["slug"]: doc for doc in supported_platforms()}
        if not platform_docs:
            return {"ok": False, "error": "Telegram and Instagram categories are not initialized."}

        forced_slug = mode.removeprefix("all_") if mode.startswith("all_") else None
        if forced_slug and forced_slug not in platform_docs:
            return {"ok": False, "error": "Invalid platform selection."}
        markup = get_import_markup()
        currency = normalize_currency(panel.get("currency", "USD"))
        existing_docs = list(smm_services.find({"panel_id": panel["_id"]}))
        existing_by_id = {str(doc.get("provider_service_id")): doc for doc in existing_docs if doc.get("provider_service_id") is not None}
        operations = []
        new_count = 0
        updated_count = 0
        unmatched_count = 0
        invalid_price_count = 0

        for service in services:
            slug = forced_slug or detect_platform(service)
            if mode in {"telegram", "instagram"} and slug != mode:
                unmatched_count += 1
                continue
            if mode == "both" and slug not in {"telegram", "instagram"}:
                unmatched_count += 1
                continue
            if slug not in platform_docs:
                unmatched_count += 1
                continue

            provider_id = service["service_id"]
            existing = existing_by_id.get(provider_id)
            try:
                selling_price = selling_rate_inr(service["rate"], currency, markup)
            except ValueError:
                invalid_price_count += 1
                continue

            if existing:
                stored_provider_id = existing.get("provider_service_id", provider_id)
                updated_count += 1
            else:
                stored_provider_id = int(provider_id) if provider_id.isdigit() else provider_id
                new_count += 1

            fields = {
                "platform_id": platform_docs[slug]["_id"],
                "panel_id": panel["_id"],
                "provider_service_id": stored_provider_id,
                "provider_rate": service["rate"],
                "provider_currency": currency,
                "provider_category": service["category"],
                "provider_type": service["type"],
                "description": service.get("description", ""),
                "min": service["min"],
                "max": service["max"],
                "catalog_imported": True,
                "imported_at": datetime.utcnow(),
                "import_markup_percent": markup,
                "active": True,
            }
            if not existing or existing.get("catalog_imported"):
                fields["name"] = service["name"]
            else:
                # Keep the admin's manually curated display name on matched rows.
                fields["name"] = existing.get("name") or service["name"]
            if not existing or existing.get("price_source") != "manual":
                fields["price_per_1000"] = selling_price
                fields["price_source"] = "import"
            else:
                fields["price_source"] = "manual"
            query = {"panel_id": panel["_id"], "provider_service_id": stored_provider_id}
            operations.append(UpdateOne(query, {"$set": fields}, upsert=True))
            existing_by_id[provider_id] = {**(existing or {}), **fields}

        for offset in range(0, len(operations), 500):
            smm_services.bulk_write(operations[offset:offset + 500], ordered=False)

        return {
            "ok": True,
            "new": new_count,
            "updated": updated_count,
            "unmatched": unmatched_count,
            "invalid": invalid_count + invalid_price_count,
            "unsupported": unsupported_count,
            "margin": markup,
            "currency": currency,
            "catalog_count": len(services) + invalid_count + unsupported_count,
        }

    async def send_html(chat_id, text, reply_markup=None) -> bool:
        text = clip(text)
        try:
            await bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=reply_markup)
            return True
        except Exception as first_error:
            logger.warning("HTML send failed to %s: %s", chat_id, first_error)
            try:
                await bot.send_message(chat_id, strip_tags(text)[:4000] or "Order update.", reply_markup=reply_markup)
                return True
            except Exception as second_error:
                logger.warning("Plain send failed to %s: %s", chat_id, second_error)
                return False

    async def edit_html(message, text, reply_markup=None) -> bool:
        text = clip(text)
        try:
            await message.edit_text(text, parse_mode="HTML", reply_markup=reply_markup)
            return True
        except Exception as first_error:
            err = str(first_error).lower()
            if "message is not modified" in err:
                return True
            logger.warning("HTML edit failed: %s", first_error)
            try:
                await message.answer(text, parse_mode="HTML", reply_markup=reply_markup)
                return True
            except Exception as second_error:
                logger.warning("HTML answer failed: %s", second_error)
                return False

    async def log_admin(text: str, raw: str = None):
        """User-side events only. Admin button taps are not logged here."""
        full = text
        if raw:
            full += f"\n\n📡 <b>Raw API</b>\n<pre>{h(raw[:1800])}</pre>"
        try:
            await bot.send_message(admin_log_id, clip(full), parse_mode="HTML")
        except Exception:
            try:
                await bot.send_message(admin_log_id, strip_tags(full)[:4000])
            except Exception:
                pass

    async def log_public(text: str, with_order_button=True):
        if not public_log_enabled():
            return
        kb = None
        if with_order_button:
            builder = InlineKeyboardBuilder()
            builder.button(text="• Buy Now •", url=f"https://t.me/{BOT_USERNAME}?start=smm")
            kb = builder.as_markup()
        try:
            await bot.send_message(public_log_id, text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            pass

    def mark_notified(order, norm: str):
        smm_orders.update_one(
            {"_id": order["_id"]},
            {"$set": {"last_notified_status": norm, "notified_at": datetime.utcnow()}},
        )
        order["last_notified_status"] = norm

    def should_push_old(order) -> bool:
        """Don't spam customers for ancient orders that never had a notify flag."""
        if order.get("last_notified_status"):
            return False
        created = order.get("created_at")
        if not isinstance(created, datetime):
            return False
        return created >= datetime.utcnow() - timedelta(hours=NOTIFY_BACKFILL_HOURS)

    def should_refund(order) -> bool:
        if order.get("refunded") is True:
            return False
        if order.get("refunded") is False:
            return True
        created = order.get("created_at")
        return isinstance(created, datetime) and created >= datetime.utcnow() - timedelta(hours=NOTIFY_BACKFILL_HOURS)

    def set_poll_flag(order, norm: str):
        needs = norm not in ("completed", "canceled", "failed")
        created = order.get("created_at") if isinstance(order.get("created_at"), datetime) else datetime.utcnow()
        age = datetime.utcnow() - created
        if norm == "partial":
            needs = age < timedelta(days=PARTIAL_POLL_DAYS)
        if norm in ("pending", "in_progress", "placing", "unknown") and age > timedelta(days=STALE_POLL_DAYS):
            needs = False
        if norm in TERMINAL_NORMS and order.get("last_notified_status") != norm:
            needs = True
        if norm in REFUND_NORMS and order.get("refunded") is not True and should_refund(order):
            needs = True
        smm_orders.update_one({"_id": order["_id"]}, {"$set": {"needs_poll": needs}})
        order["needs_poll"] = needs

    async def refund_order(order) -> bool:
        claimed = smm_orders.find_one_and_update(
            {"_id": order["_id"], "refunded": {"$ne": True}},
            {"$set": {"refunded": True, "refunded_at": datetime.utcnow()}},
            return_document=ReturnDocument.BEFORE,
        )
        if not claimed:
            order["refunded"] = True
            return False
        users_col.update_one({"_id": order["user_id"]}, {"$inc": {"balance": float(order.get("price") or 0)}})
        order["refunded"] = True
        await log_admin(
            "💸 <b>Auto-refund issued</b>\n\n"
            f"👤 <b>User:</b> <code>{h(order['user_id'])}</code>\n"
            f"🆔 <b>Order:</b> <code>{h(order['_id'])}</code>\n"
            f"💰 <b>Amount:</b> <code>{h(inr(order.get('price')))}</code>\n"
            f"📊 <b>Status:</b> {h(order.get('status'))}"
        )
        return True

    def notification_text(order, norm: str, refunded_now: bool) -> str:
        emoji = status_emoji(STATUS_TEXT.get(norm, norm))
        title = {
            "completed": "Order Completed",
            "partial": "Order Partial",
            "canceled": "Order Canceled — Refunded",
            "failed": "Order Failed — Refunded",
        }.get(norm, "Order Update")
        refund_line = ""
        if norm in REFUND_NORMS:
            if refunded_now or order.get("refunded"):
                refund_line = f"💸 <b>Refund:</b> <code>{h(inr(order.get('price')))}</code> added back"
            else:
                refund_line = "💸 <b>Refund:</b> not applied — contact support"
        return (
            f"<h2>{emoji} {title}</h2>"
            + card(
                f"🎯 <b>Service:</b> {h(order.get('service_name'))}",
                f"🚀 <b>Platform:</b> {h(order.get('platform_emoji') or '📱')} {h(order.get('platform_name'))}",
                f"🔢 <b>Quantity:</b> <code>{h(qty_fmt(order.get('quantity')))}</code>",
                f"🏁 <b>Start:</b> <code>{h(order.get('start_count'))}</code>",
                f"⏳ <b>Remains:</b> <code>{h(order.get('remains'))}</code>",
                f"🆔 <b>Order ID:</b> <code>{h(order['_id'])}</code>",
                refund_line,
            )
            + f"<p>ℹ️ <i>{h(explain_status(norm))}</i></p>"
        )

    async def maybe_notify(order, norm: str, refunded_now: bool) -> bool:
        if norm not in TERMINAL_NORMS:
            return True
        if order.get("last_notified_status") == norm:
            return True
        if not should_push_old(order) and order.get("refunded") is not False:
            mark_notified(order, norm)
            return True
        if int(order.get("notify_failures") or 0) >= 12:
            mark_notified(order, norm)
            return False
        ok = await send_html(order["user_id"], notification_text(order, norm, refunded_now))
        if ok:
            mark_notified(order, norm)
        else:
            fails = int(order.get("notify_failures") or 0) + 1
            smm_orders.update_one({"_id": order["_id"]}, {"$set": {"notify_failures": fails}})
            order["notify_failures"] = fails
            if fails in (1, 3, 8, 12):
                await log_admin(
                    "⚠️ <b>User notification failed</b>\n\n"
                    f"👤 <code>{h(order.get('user_id'))}</code>\n"
                    f"🆔 <code>{h(order['_id'])}</code>\n"
                    f"📊 {h(norm)}\n"
                    + (
                        "<i>12 tries failed. The user has likely blocked the bot, so auto-ping stopped.</i>"
                        if fails >= 12
                        else "<i>If HTML fails, a plain-text copy is tried. The next cycle will try again.</i>"
                    )
                )
        return ok

    async def sync_order(order, resp: dict, *, notify_user: bool, raw: str = ""):
        raw_status = str((resp or {}).get("status") or order.get("status") or "Unknown")
        norm = normalize_status(raw_status)
        pretty = STATUS_TEXT.get(norm, raw_status)
        old_norm = order.get("status_norm") or normalize_status(order.get("status"))
        fields = {
            "status": pretty,
            "status_norm": norm,
            "provider_status": raw_status,
            "last_checked": datetime.utcnow(),
            "poll_failures": 0,
            "start_count": (resp or {}).get("start_count", order.get("start_count")),
            "remains": (resp or {}).get("remains", order.get("remains")),
        }
        if (resp or {}).get("charge") is not None:
            fields["provider_charge"] = resp.get("charge")
        smm_orders.update_one({"_id": order["_id"]}, {"$set": fields})
        order.update(fields)

        refunded_now = False
        if norm in REFUND_NORMS and should_refund(order):
            refunded_now = await refund_order(order)

        if old_norm != norm:
            await log_admin(
                "🔄 <b>Order status changed</b>\n\n"
                f"👤 <code>{h(order.get('user_id'))}</code>\n"
                f"🆔 <code>{h(order['_id'])}</code>\n"
                f"🎯 {h(order.get('service_name'))}\n"
                f"📊 <b>{h(old_norm)}</b> → <b>{h(pretty)}</b>\n"
                f"🔢 remains <code>{h(order.get('remains'))}</code>",
                raw,
            )

        if notify_user:
            await maybe_notify(order, norm, refunded_now)
        set_poll_flag(order, norm)
        return pretty, norm, refunded_now

    def render_status(order, pretty: str, extra_note: str = "") -> str:
        norm = normalize_status(pretty)
        when = order.get("created_at")
        placed = when.strftime("%d %b %Y, %H:%M UTC") if isinstance(when, datetime) else "—"
        note = extra_note or explain_status(norm)
        refunded = "Yes" if order.get("refunded") else "No"
        return (
            f"<h2>{status_emoji(pretty)} Order Status</h2>"
            + card(
                f"🎯 <b>Service:</b> {h(order.get('service_name'))}",
                f"🚀 <b>Platform:</b> {h(order.get('platform_emoji') or '📱')} {h(order.get('platform_name'))}",
                f"📊 <b>Status:</b> {h(pretty)}",
                f"🔢 <b>Quantity:</b> <code>{h(qty_fmt(order.get('quantity')))}</code>",
                f"💰 <b>Charged:</b> <code>{h(inr(order.get('price')))}</code>",
                f"🏁 <b>Start:</b> <code>{h(order.get('start_count'))}</code>",
                f"⏳ <b>Remains:</b> <code>{h(order.get('remains'))}</code>",
                f"💸 <b>Refunded:</b> {refunded}",
            )
            + card(
                f"🆔 <b>Order ID:</b> <code>{h(order['_id'])}</code>",
                f"🕒 <b>Placed:</b> <i>{h(placed)}</i>",
                f"🔗 <b>Link:</b> {link_html(order.get('link') or '')}",
            )
            + f"<p>ℹ️ <i>{h(note)}</i></p>"
        )

    def status_keyboard(order_id):
        kb = InlineKeyboardBuilder()
        kb.button(text="🔄 Refresh status", callback_data=f"smm_rf:{order_id}")
        kb.button(text="🔙 Menu", callback_data="smm_open")
        kb.adjust(1)
        return kb.as_markup()

    def close_row(kb: InlineKeyboardBuilder):
        kb.row(
            InlineKeyboardButton(text="🔙 Back", callback_data="back_main"),
            InlineKeyboardButton(text="❌ Close", callback_data="delete_msg"),
        )

    def build_smm_menu_text_and_kb(user_id: int):
        balance = get_balance_inr(user_id)
        kb = InlineKeyboardBuilder()
        open_orders = smm_orders.count_documents({
            "user_id": user_id,
            "$or": [
                {"status_norm": {"$in": ["pending", "in_progress", "placing", "unknown"]}},
                {
                    "status_norm": {"$exists": False},
                    "status": {"$nin": ["Completed", "Complete", "Partial", "Canceled", "Cancelled", "Error", "Failed", "Refunded"]},
                },
            ],
        })
        if not smm_enabled():
            text = (
                "<h2>🚀 Social Boost Center</h2>"
                + card(
                    "🚧 <b>Ordering is paused.</b> Existing orders are safe.",
                    f"💰 <b>Wallet:</b> <code>{h(inr(balance))}</code>",
                    f"📦 <b>Open orders:</b> <code>{open_orders}</code>",
                )
                + "<p><i>You can still check order status or view history.</i></p>"
            )
        else:
            platforms = supported_platforms()
            text = (
                "<h2>🚀 Social Boost Center</h2>"
                + card(
                    "✨ Grow your Telegram and Instagram with a simple, secure order flow.",
                    f"💰 <b>Wallet balance:</b> <code>{h(inr(balance))}</code>",
                    f"📦 <b>Active orders:</b> <code>{open_orders}</code>",
                )
                + "<p>👇 <i>Select a platform to explore available services.</i></p>"
            )
            if not platforms:
                text += "<p>😔 <i>Services are being prepared. Please try again later.</i></p>"
            else:
                for platform in platforms:
                    kb.button(
                        text=button_label(f"{platform.get('emoji', '📱')} {platform['name']}"),
                        callback_data=f"smm_plat:{platform['_id']}:0",
                    )
                kb.adjust(2)
        kb.row(
            InlineKeyboardButton(text="📊 Check Order Status", callback_data="smm_status_menu"),
            InlineKeyboardButton(text="📜 History", callback_data="smm_history"),
        )
        close_row(kb)
        return text, kb.as_markup()

    async def smm_send_menu(chat_id):
        text, markup = build_smm_menu_text_and_kb(chat_id)
        await send_html(chat_id, text, markup)

    async def fetch_provider_statuses(panel, orders):
        ids = [str(o["provider_order_id"]) for o in orders if o.get("provider_order_id")]
        if not ids:
            return {}, ""
        if len(ids) == 1:
            success, data, raw = await smm_api_call(panel, "status", order=ids[0])
            if not success:
                return {}, raw
            return parse_status_map(data, ids), raw
        success, data, raw = await smm_api_call(panel, "status", orders=",".join(ids))
        parsed = parse_status_map(data, ids) if success else {}
        if success and len(parsed) >= max(1, len(ids) // 2):
            return parsed, raw
        # Some panels only support one id per call.
        merged = {}
        raw_parts = [raw]
        for pid in ids:
            one_ok, one_data, one_raw = await smm_api_call(panel, "status", order=pid)
            raw_parts.append(one_raw)
            if one_ok:
                merged.update(parse_status_map(one_data, [pid]))
        return merged, "\n".join(raw_parts)[:1800]

    async def check_active_orders():
        cutoff = datetime.utcnow() - timedelta(hours=NOTIFY_BACKFILL_HOURS)
        query = {
            "$or": [
                {"needs_poll": True},
                {
                    "needs_poll": {"$exists": False},
                    "status": {"$nin": ["Completed", "Complete", "Partial", "Canceled", "Cancelled", "Error", "Failed", "Refunded"]},
                },
                {
                    "needs_poll": {"$exists": False},
                    "last_notified_status": {"$exists": False},
                    "created_at": {"$gte": cutoff},
                    "status": {"$in": ["Completed", "Complete", "Partial", "Canceled", "Cancelled", "Error", "Failed", "Refunded"]},
                },
                {
                    "refunded": False,
                    "status_norm": {"$in": list(REFUND_NORMS)},
                },
            ]
        }
        orders = list(smm_orders.find(query).sort("last_checked", 1).limit(150))
        grouped = {}
        for order in orders:
            grouped.setdefault(str(order.get("panel_id") or ""), []).append(order)

        for group in grouped.values():
            panel = panel_by_id(group[0].get("panel_id"))
            chunk = []
            for order in group:
                chunk.append(order)
                if len(chunk) >= 40:
                    await poll_chunk(panel, chunk)
                    chunk = []
            if chunk:
                await poll_chunk(panel, chunk)

        smm_settings.update_one(
            {"_id": "config"},
            {"$set": {"checker_last_run": datetime.utcnow(), "checker_last_count": len(orders), "checker_last_error": ""}},
            upsert=True,
        )

    async def poll_chunk(panel, orders):
        if not panel:
            for order in orders:
                await note_poll_failure(order, "No panel configured")
            return
        parsed, raw = await fetch_provider_statuses(panel, orders)
        for order in orders:
            try:
                pid = str(order.get("provider_order_id") or "")
                if not pid:
                    await note_poll_failure(order, "Missing provider order id")
                    continue
                resp = parsed.get(pid)
                if not resp:
                    await note_poll_failure(order, "Status missing in provider response")
                    continue
                if resp.get("error"):
                    await note_poll_failure(order, str(resp.get("error")))
                    continue
                await sync_order(order, resp, notify_user=True, raw=raw if len(orders) == 1 else "")
            except Exception as exc:
                logger.exception("Order poll failed for %s", order.get("_id"))
                await note_poll_failure(order, str(exc))

    async def note_poll_failure(order, reason: str):
        fails = int(order.get("poll_failures") or 0) + 1
        # Dead rows (no provider id) must not sit in the hot queue forever.
        stop = "Missing provider" in reason and fails >= 5
        smm_orders.update_one(
            {"_id": order["_id"]},
            {"$set": {
                "poll_failures": fails,
                "last_checked": datetime.utcnow(),
                "last_error": reason[:300],
                "needs_poll": not stop,
            }},
        )
        if fails in (3, 10, 25) or stop:
            await log_admin(
                "⚠️ <b>Status check failing</b>\n\n"
                f"🆔 <code>{h(order.get('_id'))}</code>\n"
                f"👤 <code>{h(order.get('user_id'))}</code>\n"
                f"🔁 <b>Fails:</b> <code>{fails}</code>\n"
                f"<i>{h(reason)}</i>"
                + ("\n<i>Auto-check stopped for this order.</i>" if stop else "")
            )

    # ============================================================
    # USER
    # ============================================================

    @dp.callback_query(F.data == "smm_open")
    async def smm_open(cq: CallbackQuery, state: FSMContext):
        await state.clear()
        text, markup = build_smm_menu_text_and_kb(cq.from_user.id)
        await edit_html(cq.message, text, markup)
        await cq.answer()

    @dp.callback_query(F.data.startswith("smm_plat:"))
    async def smm_platform_services(cq: CallbackQuery, state: FSMContext):
        await state.clear()
        if not smm_enabled():
            return await cq.answer("🚧 Ordering is temporarily unavailable.", show_alert=True)
        parts = cq.data.split(":")
        platform_id = parts[1] if len(parts) > 1 else ""
        try:
            page = max(0, int(parts[2])) if len(parts) > 2 and parts[2].isdigit() else 0
            platform = smm_platforms.find_one({"_id": ObjectId(platform_id)})
        except Exception:
            platform = None
            page = 0
        if not platform or platform.get("slug") not in {p["slug"] for p in PLATFORM_PRESETS}:
            return await cq.answer("❌ Platform not found.", show_alert=True)

        service_filter = {"platform_id": platform["_id"], "active": {"$ne": False}}
        total_services = smm_services.count_documents(service_filter)
        total_pages = max(1, (total_services + PUBLIC_SERVICES_PER_PAGE - 1) // PUBLIC_SERVICES_PER_PAGE)
        page = min(page, total_pages - 1)
        services = list(
            smm_services.find(service_filter)
            .sort("name", 1)
            .skip(page * PUBLIC_SERVICES_PER_PAGE)
            .limit(PUBLIC_SERVICES_PER_PAGE)
        )
        kb = InlineKeyboardBuilder()
        title = f"{platform.get('emoji', '📱')} {platform['name']} Services"
        if not services:
            text = f"<h2>{h(title)}</h2>" + card("😔 <i>No services are available here yet.</i>")
        else:
            disc = get_user_discount(cq.from_user.id)
            card_lines = [
                "👇 Choose a service. All rates are shown per <b>1,000</b>.",
                "Your wallet is charged only after you confirm.",
                f"📦 <b>Available:</b> <code>{total_services:,}</code> · <b>Page:</b> <code>{page + 1}/{total_pages}</code>",
            ]
            if disc > 0:
                card_lines.append(f"🏷️ <b>VIP Discount:</b> <code>{disc:g}% OFF</code>")
            text = f"<h2>{h(title)}</h2>" + card(*card_lines)
            for svc in services:
                rate_1k = get_service_rate_1k(svc, cq.from_user.id)
                kb.button(
                    text=button_label(f"{svc['name']} — ₹{rate_1k:.2f}/1k"),
                    callback_data=f"smm_svc:{svc['_id']}",
                )
            kb.adjust(1)
            nav = []
            if page > 0:
                nav.append(InlineKeyboardButton(text="⬅️ Previous", callback_data=f"smm_plat:{platform_id}:{page - 1}"))
            if page + 1 < total_pages:
                nav.append(InlineKeyboardButton(text="Next ➡️", callback_data=f"smm_plat:{platform_id}:{page + 1}"))
            if nav:
                kb.row(*nav)
        kb.row(InlineKeyboardButton(text="🔙 Platforms", callback_data="smm_open"))
        await edit_html(cq.message, text, kb.as_markup())
        await cq.answer()

    def provider_description_block(svc: dict) -> str:
        description = str(svc.get("description") or "").strip()
        name = str(svc.get("name") or "").strip()
        if not description or description.casefold() == name.casefold():
            return ""
        description_lines = "<br>".join(escape(line) for line in description.splitlines())
        if not description_lines:
            description_lines = escape(description)
        return f"<blockquote>📝 <b>Provider description</b><br>{description_lines}</blockquote>"

    def service_prompt(svc, balance: float, uid: int = None) -> str:
        rate_1k = get_service_rate_1k(svc, uid)
        disc = get_user_discount(uid)
        sample = rate_1k / 1000 * int(svc["min"])
        lines = [
            f"💰 <b>Rate:</b> <code>{h(inr(rate_1k))}</code> <i>per 1,000</i>",
            f"📉 <b>Min:</b> <code>{h(qty_fmt(svc['min']))}</code>",
            f"📈 <b>Max:</b> <code>{h(qty_fmt(svc['max']))}</code>",
            f"💳 <b>Balance:</b> <code>{h(inr(balance))}</code>",
            f"💡 <i>{h(qty_fmt(svc['min']))} = {h(inr(sample))}</i>",
        ]
        if disc > 0:
            lines.append(f"🏷️ <b>VIP Discount:</b> <code>{disc:g}% OFF</code>")
        return (
            f"<h2>🎯 {h(svc['name'])}</h2>"
            + provider_description_block(svc)
            + card(*lines)
            + "<p>🔢 <b>Choose a quantity:</b> <i>Tap a preset button, or tap Custom and type a number.</i></p>"
        )

    @dp.callback_query(F.data.startswith("smm_svc:"))
    async def smm_service_selected(cq: CallbackQuery, state: FSMContext):
        if not smm_enabled():
            return await cq.answer("🚧 Ordering is temporarily unavailable.", show_alert=True)
        service_id = cq.data.split(":", 1)[1]
        try:
            svc = smm_services.find_one({"_id": ObjectId(service_id)})
        except Exception:
            svc = None
        if not svc:
            return await cq.answer("❌ Service not found.", show_alert=True)
        await state.update_data(service_id=service_id)
        await state.set_state(SMMUser.waiting_quantity)
        kb = InlineKeyboardBuilder()
        for qty in quantity_presets(int(svc["min"]), int(svc["max"])):
            kb.button(text=qty_fmt(qty), callback_data=f"smm_qset:{qty}")
        kb.button(text="✏️ Custom", callback_data="smm_qcustom")
        kb.adjust(3)
        kb.row(InlineKeyboardButton(text="❌ Cancel", callback_data="smm_open"))
        await edit_html(cq.message, service_prompt(svc, get_balance_inr(cq.from_user.id), cq.from_user.id), kb.as_markup())
        await cq.answer()

    async def ask_for_link(target, state: FSMContext, qty: int, edit: bool, uid: int = None):
        data = await state.get_data()
        svc = smm_services.find_one({"_id": ObjectId(data["service_id"])})
        if not svc:
            await state.clear()
            text = "<h2>❌ Service Removed</h2><blockquote><i>This service is no longer available. Start again from the menu.</i></blockquote>"
            if edit:
                await edit_html(target, text)
            else:
                await target.answer(text, parse_mode="HTML")
            return
        if qty < int(svc["min"]) or qty > int(svc["max"]):
            warn = (
                "<h2>❌ Quantity Out of Range</h2>"
                + card(
                    f"📉 <b>Min:</b> <code>{h(qty_fmt(svc['min']))}</code>",
            f"📈 <b>Max:</b> <code>{h(qty_fmt(svc['max']))}</code>",
                    f"🔢 <b>You sent:</b> <code>{h(qty_fmt(qty))}</code>",
                )
                + "<p><i>Send a number inside that range.</i></p>"
            )
            if edit:
                await edit_html(target, warn)
            else:
                await target.answer(warn, parse_mode="HTML")
            return
        user_id = uid or (target.chat.id if hasattr(target, "chat") else getattr(target.from_user, "id", None))
        rate_1k = get_service_rate_1k(svc, user_id)
        price = round((rate_1k / 1000) * qty, 2)
        await state.update_data(quantity=qty, price=price)
        await state.set_state(SMMUser.waiting_link)
        text = (
            "<h2>🔗 Send the Public Link</h2>"
            + card(
                f"🎯 <b>Service:</b> {h(svc['name'])}",
                f"🔢 <b>Quantity:</b> <code>{h(qty_fmt(qty))}</code>",
                f"💰 <b>Price:</b> <code>{h(inr(price))}</code>",
            )
            + "<p>📌 <i>Paste a public http(s) or t.me link. Never send a password.</i></p>"
        )
        kb = InlineKeyboardBuilder()
        kb.button(text="❌ Cancel", callback_data="smm_open")
        if edit:
            await edit_html(target, text, kb.as_markup())
        else:
            await target.answer(text, parse_mode="HTML", reply_markup=kb.as_markup())

    @dp.callback_query(F.data.startswith("smm_qset:"))
    async def smm_qset(cq: CallbackQuery, state: FSMContext):
        data = await state.get_data()
        if not data.get("service_id"):
            return await cq.answer("Choose a service first.", show_alert=True)
        try:
            qty = int(cq.data.split(":", 1)[1])
        except ValueError:
            return await cq.answer("Invalid quantity.", show_alert=True)
        await ask_for_link(cq.message, state, qty, edit=True, uid=cq.from_user.id)
        await cq.answer()

    @dp.callback_query(F.data == "smm_qcustom")
    async def smm_qcustom(cq: CallbackQuery, state: FSMContext):
        data = await state.get_data()
        if not data.get("service_id"):
            return await cq.answer("Choose a service first.", show_alert=True)
        await state.set_state(SMMUser.waiting_quantity)
        kb = InlineKeyboardBuilder()
        kb.button(text="❌ Cancel", callback_data="smm_open")
        await edit_html(
            cq.message,
            "<h2>✏️ Custom Quantity</h2>"
            + card("🔢 <i>Send digits only, for example <b>1000</b>. No commas or text.</i>"),
            kb.as_markup(),
        )
        await cq.answer()

    @dp.message(SMMUser.waiting_quantity, F.text.startswith("/"))
    async def smm_qty_command(msg: Message, state: FSMContext):
        await state.clear()
        raise SkipHandler()

    @dp.message(SMMUser.waiting_quantity)
    async def smm_qty_received(msg: Message, state: FSMContext):
        if not msg.text or not msg.text.strip().isdigit():
            return await msg.answer(
                "<h2>❌ Number Required</h2><blockquote>🔢 <i>Send digits only, for example <b>500</b>.</i></blockquote>",
                parse_mode="HTML",
            )
        await ask_for_link(msg, state, int(msg.text.strip()), edit=False, uid=msg.from_user.id)

    @dp.message(SMMUser.waiting_link, F.text.startswith("/"))
    async def smm_link_command(msg: Message, state: FSMContext):
        await state.clear()
        raise SkipHandler()

    @dp.message(SMMUser.waiting_link)
    async def smm_link_received(msg: Message, state: FSMContext):
        link = normalize_link(msg.text or "")
        if not link:
            return await msg.answer(
                "<h2>❌ Invalid Link</h2>"
                "<blockquote>🔗 <i>Start with http://, https://, or t.me/. No spaces.</i></blockquote>",
                parse_mode="HTML",
            )
        data = await state.get_data()
        try:
            svc = smm_services.find_one({"_id": ObjectId(data["service_id"])})
        except Exception:
            svc = None
        if not svc:
            await state.clear()
            return await msg.answer("<h2>❌ Service Removed</h2><p>Start again from the menu.</p>", parse_mode="HTML")
        platform = smm_platforms.find_one({"_id": svc["platform_id"]}) or {"name": "Unknown", "emoji": "📱"}
        qty = int(data["quantity"])
        if qty < int(svc["min"]) or qty > int(svc["max"]):
            await state.clear()
            return await msg.answer("<h2>❌ Invalid Quantity</h2><p>Open the service again.</p>", parse_mode="HTML")
        rate_1k = get_service_rate_1k(svc, msg.from_user.id)
        price = round((rate_1k / 1000) * qty, 2)
        balance = get_balance_inr(msg.from_user.id)
        await state.update_data(link=link, price=price, quantity=qty)
        await state.set_state(None)
        shortfall = max(0.0, round(price - balance, 2))
        money_line = (
            f"💳 <b>Balance after:</b> <code>{h(inr(balance - price))}</code>"
            if shortfall <= 0
            else f"⚠️ <b>Short by:</b> <code>{h(inr(shortfall))}</code>"
        )
        text = (
            "<h2>🧾 Confirm This Order</h2>"
            + provider_description_block(svc)
            + card(
                f"🚀 <b>Platform:</b> {h(platform.get('emoji', '📱'))} {h(platform.get('name'))}",
                f"🎯 <b>Service:</b> {h(svc['name'])}",
                f"🔢 <b>Quantity:</b> <code>{h(qty_fmt(qty))}</code>",
                f"💰 <b>Rate:</b> <code>{h(inr(rate_1k))}</code> <i>/ 1,000</i>",
                f"💵 <b>Total:</b> <code>{h(inr(price))}</code>",
                money_line,
                f"🔗 <b>Link:</b> {link_html(link)}",
            )
            + "<p>ℹ️ <i>Confirm charges your balance. Canceled or failed orders are refunded automatically.</i></p>"
        )
        kb = InlineKeyboardBuilder()
        kb.row(
            InlineKeyboardButton(text="✅ Confirm", callback_data="smm_confirm"),
            InlineKeyboardButton(text="❌ Cancel", callback_data="smm_open"),
        )
        await msg.answer(text, parse_mode="HTML", reply_markup=kb.as_markup())

    @dp.callback_query(F.data == "smm_confirm")
    async def smm_confirm_order(cq: CallbackQuery, state: FSMContext):
        uid = cq.from_user.id
        if uid in _placing_users:
            return await cq.answer("Order already being placed. Wait a second.", show_alert=True)
        data = await state.get_data()
        if not data.get("service_id") or not data.get("quantity") or not data.get("link"):
            await state.clear()
            await cq.answer("Confirmation expired. Start again.", show_alert=True)
            return
        if not smm_enabled():
            await state.clear()
            return await cq.answer("🚧 Ordering was just turned off.", show_alert=True)

        _placing_users.add(uid)
        await cq.answer("Placing order…")
        try:
            svc = smm_services.find_one({"_id": ObjectId(data["service_id"])})
            if not svc:
                await state.clear()
                return await edit_html(cq.message, "<h2>❌ Service No Longer Available</h2>")
            platform = smm_platforms.find_one({"_id": svc["platform_id"]}) or {"name": "Unknown", "emoji": "📱"}
            panel = get_panel_for_service(svc)
            qty = int(data["quantity"])
            link = normalize_link(data.get("link") or "")
            if not link or qty < int(svc["min"]) or qty > int(svc["max"]):
                await state.clear()
                return await edit_html(cq.message, "<h2>❌ Order Details Expired</h2><p>Start again from the menu.</p>")
            rate_1k = get_service_rate_1k(svc, uid)
            price = round((rate_1k / 1000) * qty, 2)
            if not panel:
                await state.clear()
                return await edit_html(
                    cq.message,
                    "<h2>❌ No Panel Configured</h2><blockquote><i>This service has no provider panel behind it. Please contact an admin.</i></blockquote>",
                )

            balance = get_balance_inr(uid)
            if balance < price:
                await state.clear()
                kb = InlineKeyboardBuilder()
                kb.button(text="💳 Recharge", callback_data="recharge")
                kb.button(text="🔙 Back", callback_data="smm_open")
                kb.adjust(1)
                return await edit_html(
                    cq.message,
                    "<h2>❌ Insufficient Balance</h2>"
                    + card(
                        f"💰 <b>Required:</b> <code>{h(inr(price))}</code>",
                        f"💳 <b>Available:</b> <code>{h(inr(balance))}</code>",
                        f"⚠️ <b>Short by:</b> <code>{h(inr(price - balance))}</code>",
                    )
                    + "<p><i>Nothing has been charged. Recharge, then order again.</i></p>",
                    kb.as_markup(),
                )

            await edit_html(
                cq.message,
                "<h2>⏳ Placing Your Order</h2><blockquote><i>Sending the request to the provider. Do not tap the button again.</i></blockquote>",
            )
            success, resp, raw = await smm_api_call(
                panel, "add", service=svc["provider_service_id"], link=link, quantity=qty
            )
            if not success or not isinstance(resp, dict) or resp.get("order") in (None, ""):
                await log_admin(
                    "⚠️ <b>SMM order failed</b>\n\n"
                    f"👤 <code>{h(uid)}</code>\n"
                    f"🏢 {h(panel.get('name'))}\n"
                    f"🔹 {h(platform.get('name'))}\n"
                    f"🎯 {h(svc.get('name'))}\n"
                    f"🔢 <code>{h(qty_fmt(qty))}</code>\n"
                    f"🔗 <code>{h(link)}</code>",
                    raw,
                )
                await state.clear()
                kb = InlineKeyboardBuilder()
                kb.button(text="🔙 Back", callback_data="smm_open")
                return await edit_html(
                    cq.message,
                    "<h2>❌ Order Not Placed</h2>"
                    "<blockquote><i>The provider did not accept the order. Your balance was not charged. "
                    "Try again in a moment, or contact support with this time.</i></blockquote>",
                    kb.as_markup(),
                )

            provider_order_id = str(resp["order"])
            updated_user = users_col.find_one_and_update(
                {"_id": uid, "balance": {"$gte": price}},
                {"$inc": {"balance": -price}},
                return_document=ReturnDocument.AFTER,
            )
            if not updated_user:
                cancel_ok, cancel_resp, cancel_raw = await smm_api_call(panel, "cancel", order=provider_order_id)
                await log_admin(
                    "⚠️ <b>Balance race — provider order may need cancel</b>\n\n"
                    f"👤 <code>{h(uid)}</code>\n"
                    f"🆔 provider <code>{h(provider_order_id)}</code>\n"
                    f"cancel_ok <code>{h(cancel_ok)}</code>",
                    cancel_raw or str(cancel_resp),
                )
                await state.clear()
                kb = InlineKeyboardBuilder()
                kb.button(text="💳 Recharge", callback_data="recharge")
                kb.button(text="🔙 Back", callback_data="smm_open")
                kb.adjust(1)
                return await edit_html(
                    cq.message,
                    "<h2>❌ Insufficient Balance</h2>"
                    "<blockquote><i>Another order used the balance first. A cancel was attempted on the provider order.</i></blockquote>",
                    kb.as_markup(),
                )

            new_balance = float(updated_user.get("balance", 0.0) or 0.0)
            order_doc = {
                "user_id": uid,
                "platform_name": platform.get("name", "Unknown"),
                "platform_emoji": platform.get("emoji", "📱"),
                "service_name": svc["name"],
                "provider_order_id": provider_order_id,
                "panel_id": panel["_id"],
                "quantity": qty,
                "link": link,
                "price": price,
                "status": "Pending",
                "status_norm": "pending",
                "provider_status": "Pending",
                "refunded": False,
                "needs_poll": True,
                "poll_failures": 0,
                "notify_failures": 0,
                "created_at": datetime.utcnow(),
                "last_checked": None,
                "start_count": None,
                "remains": None,
            }
            try:
                result = smm_orders.insert_one(order_doc)
            except Exception as exc:
                users_col.update_one({"_id": uid}, {"$inc": {"balance": price}})
                await smm_api_call(panel, "cancel", order=provider_order_id)
                await log_admin(
                    "⚠️ <b>Order save failed — user refunded, cancel attempted</b>\n\n"
                    f"👤 <code>{h(uid)}</code>\n🆔 provider <code>{h(provider_order_id)}</code>\n<i>{h(exc)}</i>"
                )
                await state.clear()
                return await edit_html(cq.message, "<h2>❌ Could Not Save Order</h2><p>Balance restored. Contact support.</p>")

            order_doc["_id"] = result.inserted_id
            await state.clear()
            text = (
                "<h2>✅ Order Placed</h2>"
                + provider_description_block(svc)
                + card(
                    f"🆔 <b>Order ID:</b> <code>{h(result.inserted_id)}</code>",
                    f"🎯 <b>Service:</b> {h(svc['name'])}",
                    f"🔢 <b>Quantity:</b> <code>{h(qty_fmt(qty))}</code>",
                    f"💰 <b>Charged:</b> <code>{h(inr(price))}</code>",
                    f"💳 <b>Balance left:</b> <code>{h(inr(new_balance))}</code>",
                    f"🔗 <b>Link:</b> {link_html(link)}",
                )
                + "<p>ℹ️ <i>Checked about every minute. You get a message when it completes. Canceled orders are refunded.</i></p>"
            )
            await edit_html(cq.message, text, status_keyboard(result.inserted_id))
            await log_admin(
                "🆕 <b>New SMM order</b>\n\n"
                f"👤 <code>{h(uid)}</code> (@{h(cq.from_user.username or 'NoUsername')})\n"
                f"🏢 {h(panel.get('name'))}\n"
                f"🔹 {h(platform.get('name'))}\n"
                f"🎯 {h(svc.get('name'))}\n"
                f"🔢 <code>{h(qty_fmt(qty))}</code>\n"
                f"💰 <code>{h(inr(price))}</code>\n"
                f"🆔 provider <code>{h(provider_order_id)}</code>\n"
                f"🔗 <code>{h(link)}</code>",
                raw,
            )
            await log_public(
                "<h2>✅ New SMM Order Placed</h2>"
                f"<blockquote>"
                f"🚀 <b>Platform:</b> {h(platform.get('name'))}\n"
                f"🎯 <b>Service:</b> {h(svc.get('name'))}\n"
                f"🔢 <b>Quantity:</b> <code>{qty}</code>\n"
                f"💵 <b>Price:</b> <code>₹{price:.2f}</code>\n"
                f"🔗 <b>Link:</b> {h(public_link_label(platform.get('name')))}"
                f"</blockquote>"
                f"<p><b>• @{BOT_USERNAME} || @{UPDATES_CHANNEL}</b></p>"
            )
        finally:
            _placing_users.discard(uid)

    async def show_live_status(target, order, *, edit: bool, notify_user: bool, actor_id: int):
        panel = panel_by_id(order.get("panel_id"))
        if not panel or not order.get("provider_order_id"):
            text = render_status(order, order.get("status") or "Unknown", "The provider order id is missing, so a live check could not run.")
            if edit:
                await edit_html(target, text, status_keyboard(order["_id"]))
            else:
                await target.answer(text, parse_mode="HTML", reply_markup=status_keyboard(order["_id"]))
            return
        success, resp, raw = await smm_api_call(panel, "status", order=order["provider_order_id"])
        await log_admin(
            "🔍 <b>Status check</b>\n\n"
            f"👤 <code>{h(actor_id)}</code>\n"
            f"🆔 <code>{h(order['_id'])}</code>",
            raw,
        )
        if not success or not isinstance(resp, dict):
            text = (
                "<h2>❌ Live Status Unavailable</h2>"
                + card(
                    "📡 <i>The provider did not respond. Your balance is safe.</i>",
                    f"📊 <b>Last status:</b> {h(order.get('status'))}",
                    f"🆔 <b>Order ID:</b> <code>{h(order['_id'])}</code>",
                )
                + "<p><i>Tap Refresh in a minute.</i></p>"
            )
            kb = status_keyboard(order["_id"])
            if edit:
                await edit_html(target, text, kb)
            else:
                await target.answer(text, parse_mode="HTML", reply_markup=kb)
            return
        pretty, norm, _ = await sync_order(order, resp, notify_user=notify_user, raw=raw)
        fresh = smm_orders.find_one({"_id": order["_id"]}) or order
        text = render_status(fresh, pretty)
        kb = status_keyboard(order["_id"])
        if edit:
            shown = await edit_html(target, text, kb)
        else:
            try:
                await target.answer(clip(text), parse_mode="HTML", reply_markup=kb)
                shown = True
            except Exception:
                shown = await send_html(actor_id, text, kb)
        if shown and not notify_user and norm in TERMINAL_NORMS:
            mark_notified(fresh, norm)
            set_poll_flag(fresh, norm)

    @dp.callback_query(F.data == "smm_status_menu")
    async def smm_status_menu(cq: CallbackQuery, state: FSMContext):
        await state.set_state(SMMUser.waiting_status_order_id)
        kb = InlineKeyboardBuilder()
        kb.button(text="🔙 Back", callback_data="smm_open")
        await edit_html(
            cq.message,
            "<h2>📊 Check Order Status</h2>"
            + card("🆔 <i>Paste the order ID from your receipt. Tap the code to copy it.</i>"),
            kb.as_markup(),
        )
        await cq.answer()

    @dp.message(SMMUser.waiting_status_order_id, F.text.startswith("/"))
    async def smm_status_command(msg: Message, state: FSMContext):
        await state.clear()
        raise SkipHandler()

    @dp.message(SMMUser.waiting_status_order_id)
    async def smm_status_check(msg: Message, state: FSMContext):
        await state.clear()
        try:
            order = smm_orders.find_one({"_id": ObjectId((msg.text or "").strip()), "user_id": msg.from_user.id})
        except Exception:
            order = None
        if not order:
            return await msg.answer(
                "<h2>❌ Order Not Found</h2><blockquote>🆔 <i>Wrong ID, or this order is not on your account. Copy it from History.</i></blockquote>",
                parse_mode="HTML",
            )
        await show_live_status(msg, order, edit=False, notify_user=False, actor_id=msg.from_user.id)

    @dp.callback_query(F.data.startswith("smm_rf:"))
    async def smm_refresh(cq: CallbackQuery, state: FSMContext):
        oid = cq.data.split(":", 1)[1]
        try:
            query = {"_id": ObjectId(oid)}
            if not is_admin(cq.from_user.id):
                query["user_id"] = cq.from_user.id
            order = smm_orders.find_one(query)
        except Exception:
            order = None
        if not order:
            return await cq.answer("Order not found.", show_alert=True)
        await cq.answer("Checking…")
        await show_live_status(cq.message, order, edit=True, notify_user=False, actor_id=cq.from_user.id)

    @dp.callback_query(F.data == "smm_history")
    async def smm_history(cq: CallbackQuery, state: FSMContext):
        await state.clear()
        orders = list(smm_orders.find({"user_id": cq.from_user.id}).sort("created_at", -1).limit(8))
        kb = InlineKeyboardBuilder()
        if not orders:
            kb.button(text="🔙 Back", callback_data="smm_open")
            return await edit_html(
                cq.message,
                "<h2>📜 Order History</h2><blockquote>😔 <i>No orders yet. Pick a platform to place the first one.</i></blockquote>",
                kb.as_markup(),
            )
        lines = ["<h2>📜 Your Recent SMM Orders</h2>"]
        for order in orders:
            when = order.get("created_at")
            date_str = when.strftime("%d %b, %H:%M") if isinstance(when, datetime) else "—"
            lines.append(
                "<blockquote>"
                f"{status_emoji(order.get('status'))} <b>{h(order.get('platform_emoji') or '📱')} {h(order.get('service_name'))}</b>\n"
                f"🆔 <code>{h(order['_id'])}</code>\n"
                f"🔢 <b>Qty:</b> {h(qty_fmt(order.get('quantity')))}   💰 <b>{h(inr(order.get('price')))}</b>\n"
                f"📊 <b>{h(order.get('status'))}</b>   🕒 <i>{h(date_str)} UTC</i>"
                "</blockquote>"
            )
            kb.button(
                text=button_label(f"{status_emoji(order.get('status'))} {order.get('service_name', 'Order')}"),
                callback_data=f"smm_rf:{order['_id']}",
            )
        kb.adjust(1)
        kb.row(InlineKeyboardButton(text="🔙 Back", callback_data="smm_open"))
        await edit_html(cq.message, "".join(lines), kb.as_markup())
        await cq.answer()

    # ============================================================
    # ADMIN
    # ============================================================

    async def smmadmin_menu(msg: Message, edit: bool = False):
        cfg = get_config()
        pub_status = "🟢 ON" if cfg.get("public_log_enabled", True) else "🔴 OFF"
        svc_status = "🟢 ON" if smm_enabled() else "🔴 OFF"
        last_run = naive_utc(cfg.get("checker_last_run"))
        checker_line = age_label(last_run) if last_run else "starting"
        stale = last_run is None or datetime.utcnow() - last_run > timedelta(minutes=5)
        markup = get_import_markup()
        panel_count = smm_panels.count_documents({})
        service_count = smm_services.count_documents({"active": {"$ne": False}})
        kb = InlineKeyboardBuilder()
        kb.row(
            InlineKeyboardButton(text="📂 Manage Telegram / Instagram", callback_data="smma_listplat"),
            InlineKeyboardButton(text="📦 Import Services", callback_data="smma_import"),
        )
        kb.row(
            InlineKeyboardButton(text="🏢 Manage Panels", callback_data="smma_listpanels"),
            InlineKeyboardButton(text="➕ Add Nice Panel", callback_data="smma_addnicepanel"),
        )
        kb.row(
            InlineKeyboardButton(text="➕ Add Other Panel", callback_data="smma_addpanel"),
            InlineKeyboardButton(text=f"💰 Import Markup: {markup:g}%", callback_data="smma_markup"),
        )
        kb.row(
            InlineKeyboardButton(text=f"📢 Public Log: {pub_status}", callback_data="smma_togglepublic"),
            InlineKeyboardButton(text=f"🌐 Ordering: {svc_status}", callback_data="smma_togglesmm"),
        )
        kb.row(
            InlineKeyboardButton(text="📊 Overall History", callback_data="smma_allorders"),
            InlineKeyboardButton(text="🔍 Check Any Order", callback_data="smma_checkorder"),
        )
        kb.row(
            InlineKeyboardButton(text="⬅️ Admin Panel", callback_data="admin_refresh"),
            InlineKeyboardButton(text="❌ Close", callback_data="delete_msg"),
        )
        warn = ""
        if stale:
            warn = "<blockquote>⚠️ <i>Order monitor has not reported a cycle in 5+ minutes.</i></blockquote>"
        if panel_count == 0:
            warn += "<blockquote>🏢 <i>No provider is saved yet. Add NiceSMMPanel or another HTTPS panel, then import its catalog.</i></blockquote>"
        text = (
            "<h2>🚀 Social Media Services · Admin</h2>"
            + card(
                f"🌐 <b>Ordering:</b> {svc_status}",
                f"📦 <b>Catalog:</b> <code>{service_count:,}</code> services · <code>{panel_count}</code> providers",
                f"💰 <b>Default import markup:</b> <code>{markup:g}%</code>",
                f"🔄 <b>Order monitor:</b> <code>{h(checker_line)}</code>",
                f"📊 <b>Last cycle checked:</b> <code>{h(cfg.get('checker_last_count', 0))}</code> orders",
                f"📢 <b>Public log:</b> {pub_status}",
            )
            + warn
            + "<p><i>Customer categories are fixed to Telegram and Instagram. API keys are added and updated here.</i></p>"
        )
        if edit:
            await edit_html(msg, text, kb.as_markup())
        else:
            await msg.answer(text, parse_mode="HTML", reply_markup=kb.as_markup())

    @dp.message(Command("smmadmin"))
    async def smmadmin_cmd(msg: Message):
        if not is_admin(msg.from_user.id):
            return await msg.answer("<h2>❌ Not Authorized</h2>", parse_mode="HTML")
        await smmadmin_menu(msg, edit=False)

    @dp.callback_query(F.data.in_({"smma_back", "smm_adm_back"}))
    async def smma_back(cq: CallbackQuery, state: FSMContext):
        if not is_admin(cq.from_user.id):
            return await cq.answer("Not authorized.", show_alert=True)
        await state.clear()
        await smmadmin_menu(cq.message, edit=True)
        await cq.answer()

    @dp.callback_query(F.data == "smma_togglepublic")
    async def smma_togglepublic(cq: CallbackQuery):
        if not is_admin(cq.from_user.id):
            return
        current = public_log_enabled()
        smm_settings.update_one({"_id": "config"}, {"$set": {"public_log_enabled": not current}}, upsert=True)
        await cq.answer(f"Public log {'OFF' if current else 'ON'}", show_alert=True)
        await smmadmin_menu(cq.message, edit=True)

    @dp.callback_query(F.data == "smma_togglesmm")
    async def smma_togglesmm(cq: CallbackQuery):
        if not is_admin(cq.from_user.id):
            return
        current = smm_enabled()
        new_value = not current
        smm_settings.update_one({"_id": "config"}, {"$set": {"smm_enabled": new_value}}, upsert=True)
        db["settings"].update_one(
            {"_id": "bot_features"}, {"$set": {"smm_enabled": new_value}}, upsert=True
        )
        await cq.answer(f"Ordering {'OFF' if current else 'ON'}", show_alert=True)
        await smmadmin_menu(cq.message, edit=True)

    @dp.callback_query(F.data == "smma_markup")
    async def smma_markup(cq: CallbackQuery, state: FSMContext):
        if not is_admin(cq.from_user.id):
            return
        current = get_import_markup()
        await state.set_state(SMMAdmin.waiting_markup)
        kb = InlineKeyboardBuilder()
        kb.button(text="🔙 Cancel", callback_data="smma_back")
        await edit_html(
            cq.message,
            "<h2>💰 Default Import Markup</h2>"
            + card(
                f"Current: <code>{current:g}%</code>",
                "Enter a markup percentage from 0 to 1000. The provider's rate is converted to INR using the panel currency and the fixed $1 = ₹100 rate.",
                "Example: <code>20</code> adds 20% over provider cost. Imported prices can still be edited per service.",
            ),
            kb.as_markup(),
        )
        await cq.answer()

    @dp.message(SMMAdmin.waiting_markup)
    async def smma_markup_save(msg: Message, state: FSMContext):
        if not is_admin(msg.from_user.id):
            return
        if msg.text and msg.text.startswith("/"):
            await state.clear()
            raise SkipHandler()
        try:
            markup = float((msg.text or "").strip())
            if not math.isfinite(markup) or not 0 <= markup <= 1000:
                raise ValueError
        except (TypeError, ValueError):
            return await msg.answer("<h2>❌ Invalid Markup</h2><p>Send a number from 0 to 1000.</p>", parse_mode="HTML")
        smm_settings.update_one({"_id": "config"}, {"$set": {"import_markup_percent": markup}}, upsert=True)
        await state.clear()
        kb = InlineKeyboardBuilder()
        kb.button(text="🔙 SMM Admin", callback_data="smma_back")
        await msg.answer(
            f"<h2>✅ Import Markup Updated</h2><blockquote>New default: <code>{markup:g}%</code></blockquote>",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )

    async def ask_panel_api_key(target, state: FSMContext, panel_name: str, api_url: str):
        await state.update_data(panel_name=panel_name, panel_url=api_url)
        await state.set_state(SMMAdmin.waiting_panel_key)
        kb = InlineKeyboardBuilder()
        kb.row(InlineKeyboardButton(text="🔙 Cancel", callback_data="smma_back"))
        await edit_html(
            target,
            f"<h2>🔑 API key · {h(panel_name)}</h2>"
            + card(
                f"🔗 <b>Endpoint:</b> <code>{h(api_url)}</code>",
                "Send the provider API key. It is stored in the bot database; this message is deleted after saving.",
                "For privacy, enter keys only in the bot's private chat.",
            ),
            kb.as_markup(),
        )

    @dp.callback_query(F.data == "smma_addnicepanel")
    async def smma_addnicepanel(cq: CallbackQuery, state: FSMContext):
        if not is_admin(cq.from_user.id):
            return
        if cq.message.chat.type != "private":
            return await cq.answer("Open the bot in a private chat before saving API credentials.", show_alert=True)
        await ask_panel_api_key(cq.message, state, "NiceSMMPanel", NICE_PANEL_API_URL)
        await cq.answer()

    @dp.callback_query(F.data == "smma_addpanel")
    async def smma_addpanel(cq: CallbackQuery, state: FSMContext):
        if not is_admin(cq.from_user.id):
            return
        if cq.message.chat.type != "private":
            return await cq.answer("Open the bot in a private chat before saving API credentials.", show_alert=True)
        await state.set_state(SMMAdmin.waiting_panel_name)
        kb = InlineKeyboardBuilder()
        kb.row(InlineKeyboardButton(text="🔙 Back", callback_data="smma_back"))
        await edit_html(
            cq.message,
            "<h2>📝 Add Another SMM Panel</h2>"
            + card("Enter a short admin-only name, for example <b>Instagram Views Provider</b>."),
            kb.as_markup(),
        )
        await cq.answer()

    @dp.message(SMMAdmin.waiting_panel_name)
    async def smma_panelname(msg: Message, state: FSMContext):
        if not is_admin(msg.from_user.id):
            return
        if msg.text and msg.text.startswith("/"):
            await state.clear()
            raise SkipHandler()
        name = (msg.text or "").strip()
        if not name or len(name) > 80:
            return await msg.answer("<h2>❌ Name Required</h2><p>Send a name up to 80 characters.</p>", parse_mode="HTML")
        await state.update_data(panel_name=name)
        await state.set_state(SMMAdmin.waiting_panel_url)
        await msg.answer(
            "<h2>🔗 Panel API URL</h2>"
            + card("Send its HTTPS API v2 endpoint, for example <code>https://provider.example/api/v2</code>."),
            parse_mode="HTML",
        )

    @dp.message(SMMAdmin.waiting_panel_url)
    async def smma_panelurl(msg: Message, state: FSMContext):
        if not is_admin(msg.from_user.id):
            return
        if msg.text and msg.text.startswith("/"):
            await state.clear()
            raise SkipHandler()
        url = normalize_api_url(msg.text or "")
        if not url:
            return await msg.answer(
                "<h2>❌ HTTPS URL Required</h2><p>Use a valid full <code>https://</code> panel API endpoint.</p>",
                parse_mode="HTML",
            )
        data = await state.get_data()
        await ask_panel_api_key(msg, state, data.get("panel_name", "SMM Panel"), url)

    @dp.message(SMMAdmin.waiting_panel_key)
    async def smma_panelkey(msg: Message, state: FSMContext):
        if not is_admin(msg.from_user.id):
            return
        if msg.text and msg.text.startswith("/"):
            await state.clear()
            raise SkipHandler()
        if msg.chat.type != "private":
            return await msg.answer("For safety, send the API key in the bot's private chat.")
        api_key = (msg.text or "").strip()
        if not api_key or len(api_key) > 1000:
            return await msg.answer("<h2>❌ API Key Required</h2><p>Send a valid API key.</p>", parse_mode="HTML")
        data = await state.get_data()
        api_url = normalize_api_url(data.get("panel_url", ""))
        if not api_url:
            await state.clear()
            return await msg.answer("<h2>❌ Panel URL Invalid</h2><p>Start adding the panel again.</p>", parse_mode="HTML")
        panel = {
            "name": data.get("panel_name", "SMM Panel"),
            "api_url": api_url,
            "api_key": api_key,
            "currency": "USD",
            "added_at": datetime.utcnow(),
        }
        try:
            await msg.delete()
        except Exception:
            pass
        await state.clear()
        success, resp, _raw = await smm_api_call(panel, "balance")
        if success and isinstance(resp, dict):
            panel["currency"] = normalize_currency(resp.get("currency"), default="USD")

        existing = smm_panels.find_one({"api_url": api_url})
        if existing:
            smm_panels.update_one({"_id": existing["_id"]}, {"$set": panel})
            panel_id = existing["_id"]
            result_title = "Updated"
        else:
            panel_id = smm_panels.insert_one(panel).inserted_id
            result_title = "Added"

        kb = InlineKeyboardBuilder()
        kb.row(
            InlineKeyboardButton(text="📦 Import Catalog", callback_data=f"smma_importpanel:{panel_id}"),
            InlineKeyboardButton(text="⚙️ Panel Settings", callback_data=f"smma_panel:{panel_id}"),
        )
        kb.row(InlineKeyboardButton(text="🔙 SMM Admin", callback_data="smma_back"))
        if success:
            text = (
                f"<h2>✅ Panel {result_title}</h2>"
                + card(
                    f"🏢 <b>Name:</b> {h(panel['name'])}",
                    f"💰 <b>Provider balance:</b> <code>{h(resp.get('balance', '—'))} {h(resp.get('currency', panel['currency']))}</code>",
                    f"💱 <b>Rate currency:</b> <code>{h(panel['currency'])}</code>",
                )
                + "<p><i>Choose Import Catalog to add matching Telegram/Instagram services.</i></p>"
            )
        else:
            text = (
                f"<h2>⚠️ Panel {result_title}, Connection Not Verified</h2>"
                + card(
                    "The endpoint and key were saved, but the balance check failed. Verify the credentials in Panel Settings before importing services.",
                    f"💱 <b>Assumed rate currency:</b> <code>{h(panel['currency'])}</code>",
                )
            )
        await msg.answer(text, parse_mode="HTML", reply_markup=kb.as_markup())

    @dp.callback_query(F.data == "smma_listpanels")
    async def smma_listpanels(cq: CallbackQuery):
        if not is_admin(cq.from_user.id):
            return
        panels = list(smm_panels.find({}))
        kb = InlineKeyboardBuilder()
        if not panels:
            kb.row(InlineKeyboardButton(text="🔙 Back", callback_data="smma_back"))
            await edit_html(cq.message, "<h2>🏢 Panels</h2><blockquote><i>No panels yet.</i></blockquote>", kb.as_markup())
            await cq.answer()
            return
        for panel in panels:
            kb.button(text=button_label(f"🏢 {panel['name']}"), callback_data=f"smma_panel:{panel['_id']}")
        kb.adjust(1)
        kb.row(InlineKeyboardButton(text="🔙 Back", callback_data="smma_back"))
        await edit_html(cq.message, "<h2>🏢 Manage Panels</h2><blockquote><i>Select a panel below. API keys are kept hidden.</i></blockquote>", kb.as_markup())
        await cq.answer()

    @dp.callback_query(F.data.startswith("smma_panel:"))
    async def smma_panel_manage(cq: CallbackQuery):
        if not is_admin(cq.from_user.id):
            return
        pid = cq.data.split(":", 1)[1]
        try:
            panel = smm_panels.find_one({"_id": ObjectId(pid)})
        except Exception:
            panel = None
        if not panel:
            return await cq.answer("Panel not found.", show_alert=True)
        svc_count = smm_services.count_documents({"panel_id": panel["_id"]})
        kb = InlineKeyboardBuilder()
        kb.row(InlineKeyboardButton(text="📦 Import Services", callback_data=f"smma_importpanel:{pid}"))
        kb.row(InlineKeyboardButton(text="🔑 Update API Key", callback_data=f"smma_setkey:{pid}"))
        kb.row(
            InlineKeyboardButton(text=f"💱 Rates in USD {' (selected)' if panel.get('currency', 'USD') != 'INR' else ''}", callback_data=f"smma_cur:{pid}:USD"),
            InlineKeyboardButton(text=f"Rates in INR {' (selected)' if panel.get('currency') == 'INR' else ''}", callback_data=f"smma_cur:{pid}:INR"),
        )
        kb.row(
            InlineKeyboardButton(text="💰 Check Balance", callback_data=f"smma_panelbal:{pid}"),
            InlineKeyboardButton(text="🗑️ Remove Panel", callback_data=f"smma_rmpanel:{pid}"),
        )
        kb.row(InlineKeyboardButton(text="🔙 Back", callback_data="smma_listpanels"))
        text = (
            f"<h2>🏢 {h(panel['name'])}</h2>"
            + card(
                f"🔗 <b>API endpoint:</b> <code>{h(panel.get('api_url'))}</code>",
                f"📦 <b>Imported services:</b> <code>{svc_count:,}</code>",
                f"💱 <b>Provider rate currency:</b> <code>{h(panel.get('currency', 'USD'))}</code>",
                "🔐 <b>API key:</b> <i>hidden</i>",
            )
        )
        await edit_html(cq.message, text, kb.as_markup())
        await cq.answer()

    @dp.callback_query(F.data.startswith("smma_setkey:"))
    async def smma_setkey(cq: CallbackQuery, state: FSMContext):
        if not is_admin(cq.from_user.id):
            return
        if cq.message.chat.type != "private":
            return await cq.answer("Open the bot in a private chat before saving API credentials.", show_alert=True)
        pid = cq.data.split(":", 1)[1]
        try:
            panel = smm_panels.find_one({"_id": ObjectId(pid)})
        except Exception:
            panel = None
        if not panel:
            return await cq.answer("Panel not found.", show_alert=True)
        await state.update_data(edit_panel_id=pid)
        await state.set_state(SMMAdmin.waiting_panel_update_key)
        cancel_kb = InlineKeyboardBuilder()
        cancel_kb.button(text="🔙 Cancel", callback_data=f"smma_panel:{pid}")
        await edit_html(
            cq.message,
            f"<h2>🔑 Update API Key · {h(panel['name'])}</h2>"
            + card("Send the new key in this private chat. Your message will be deleted after saving; the stored key is never shown."),
            cancel_kb.as_markup(),
        )
        await cq.answer()

    @dp.message(SMMAdmin.waiting_panel_update_key)
    async def smma_setkey_save(msg: Message, state: FSMContext):
        if not is_admin(msg.from_user.id):
            return
        if msg.text and msg.text.startswith("/"):
            await state.clear()
            raise SkipHandler()
        if msg.chat.type != "private":
            return await msg.answer("For safety, send the API key in the bot's private chat.")
        key = (msg.text or "").strip()
        if not key or len(key) > 1000:
            return await msg.answer("<h2>❌ API Key Required</h2><p>Send a valid API key.</p>", parse_mode="HTML")
        data = await state.get_data()
        try:
            panel_id = ObjectId(data["edit_panel_id"])
            panel = smm_panels.find_one({"_id": panel_id})
        except Exception:
            panel = None
        if not panel:
            await state.clear()
            return await msg.answer("<h2>❌ Panel Not Found</h2><p>Open SMM Admin and try again.</p>", parse_mode="HTML")
        test_panel = {**panel, "api_key": key}
        try:
            await msg.delete()
        except Exception:
            pass
        await state.clear()
        success, response, _raw = await smm_api_call(test_panel, "balance")
        fields = {"api_key": key, "updated_at": datetime.utcnow()}
        if success and isinstance(response, dict):
            fields["currency"] = normalize_currency(response.get("currency"), default=panel.get("currency", "USD"))
        smm_panels.update_one({"_id": panel_id}, {"$set": fields})
        text = (
            "<h2>✅ API Key Updated</h2>"
            + card("The new key was saved and passed the balance check." if success else "The new key was saved, but the balance test failed. Double-check the key and panel endpoint.")
        )
        kb = InlineKeyboardBuilder()
        kb.row(InlineKeyboardButton(text="⚙️ Panel Settings", callback_data=f"smma_panel:{panel_id}"))
        await msg.answer(text, parse_mode="HTML", reply_markup=kb.as_markup())

    @dp.callback_query(F.data.startswith("smma_cur:"))
    async def smma_panel_currency(cq: CallbackQuery):
        if not is_admin(cq.from_user.id):
            return
        _, pid, currency = cq.data.split(":", 2)
        if currency not in {"USD", "INR"}:
            return await cq.answer("Unsupported currency.", show_alert=True)
        try:
            result = smm_panels.update_one({"_id": ObjectId(pid)}, {"$set": {"currency": currency}})
        except Exception:
            result = None
        if not result or not result.matched_count:
            return await cq.answer("Panel not found.", show_alert=True)
        await cq.answer(f"Provider rates set to {currency}.")
        panel = smm_panels.find_one({"_id": ObjectId(pid)})
        svc_count = smm_services.count_documents({"panel_id": panel["_id"]})
        kb = InlineKeyboardBuilder()
        kb.row(InlineKeyboardButton(text="📦 Import Services", callback_data=f"smma_importpanel:{pid}"))
        kb.row(InlineKeyboardButton(text="🔑 Update API Key", callback_data=f"smma_setkey:{pid}"))
        kb.row(
            InlineKeyboardButton(text=f"💱 Rates in USD {' (selected)' if currency == 'USD' else ''}", callback_data=f"smma_cur:{pid}:USD"),
            InlineKeyboardButton(text=f"Rates in INR {' (selected)' if currency == 'INR' else ''}", callback_data=f"smma_cur:{pid}:INR"),
        )
        kb.row(
            InlineKeyboardButton(text="💰 Check Balance", callback_data=f"smma_panelbal:{pid}"),
            InlineKeyboardButton(text="🗑️ Remove Panel", callback_data=f"smma_rmpanel:{pid}"),
        )
        kb.row(InlineKeyboardButton(text="🔙 Back", callback_data="smma_listpanels"))
        text = f"<h2>🏢 {h(panel['name'])}</h2>" + card(
            f"🔗 <b>API endpoint:</b> <code>{h(panel.get('api_url'))}</code>",
            f"📦 <b>Imported services:</b> <code>{svc_count:,}</code>",
            f"💱 <b>Provider rate currency:</b> <code>{h(panel.get('currency', currency))}</code>",
            "🔐 <b>API key:</b> <i>hidden</i>",
        )
        await edit_html(cq.message, text, kb.as_markup())

    @dp.callback_query(F.data.startswith("smma_panelbal:"))
    async def smma_panelbal(cq: CallbackQuery):
        if not is_admin(cq.from_user.id):
            return
        pid = cq.data.split(":", 1)[1]
        try:
            panel = smm_panels.find_one({"_id": ObjectId(pid)})
        except Exception:
            panel = None
        if not panel:
            return await cq.answer("Panel not found.", show_alert=True)
        success, resp, _raw = await smm_api_call(panel, "balance")
        if success and isinstance(resp, dict):
            currency = normalize_currency(resp.get("currency"), default=panel.get("currency", "USD"))
            smm_panels.update_one({"_id": panel["_id"]}, {"$set": {"currency": currency}})
            await cq.answer(f"{panel['name']}: {resp.get('balance')} {resp.get('currency', currency)}", show_alert=True)
        else:
            await cq.answer("Balance check failed. The URL or key may be wrong.", show_alert=True)

    @dp.callback_query(F.data.startswith("smma_rmpanel:"))
    async def smma_rmpanel(cq: CallbackQuery):
        if not is_admin(cq.from_user.id):
            return
        try:
            panel_id = ObjectId(cq.data.split(":", 1)[1])
        except Exception:
            return await cq.answer("Invalid panel.", show_alert=True)
        linked = smm_services.count_documents({"panel_id": panel_id})
        historical_orders = smm_orders.count_documents({"panel_id": panel_id})
        if linked > 0 or historical_orders > 0:
            return await cq.answer(
                f"Panel has {linked} service(s) and {historical_orders} order(s) linked. Remove services/orders first; history must keep its provider.",
                show_alert=True,
            )
        smm_panels.delete_one({"_id": panel_id})
        await cq.answer("Panel removed.", show_alert=True)
        kb = InlineKeyboardBuilder()
        kb.button(text="🏢 Manage Panels", callback_data="smma_listpanels")
        kb.button(text="🔙 SMM Admin", callback_data="smma_back")
        kb.adjust(1)
        await edit_html(cq.message, "<h2>✅ Panel Removed</h2><p>Manage another provider or return to SMM Admin.</p>", kb.as_markup())

    @dp.callback_query(F.data == "smma_import")
    async def smma_import(cq: CallbackQuery):
        if not is_admin(cq.from_user.id):
            return
        panels = list(smm_panels.find({}))
        kb = InlineKeyboardBuilder()
        for panel in panels:
            kb.button(text=button_label(f"📦 {panel.get('name', 'Provider')}"), callback_data=f"smma_importpanel:{panel['_id']}")
        kb.adjust(1)
        kb.row(InlineKeyboardButton(text="🔙 SMM Admin", callback_data="smma_back"))
        text = (
            "<h2>📦 Import Service Catalog</h2>"
            + card("Select any saved HTTPS API v2 panel. Catalog imports are matched to the two customer categories: Telegram and Instagram.")
        )
        if not panels:
            text += "<p>No provider panels saved yet. Add NiceSMMPanel or another provider first.</p>"
        await edit_html(cq.message, text, kb.as_markup())
        await cq.answer()

    @dp.callback_query(F.data.startswith("smma_importpanel:"))
    async def smma_importpanel(cq: CallbackQuery):
        if not is_admin(cq.from_user.id):
            return
        pid = cq.data.split(":", 1)[1]
        try:
            panel = smm_panels.find_one({"_id": ObjectId(pid)})
        except Exception:
            panel = None
        if not panel:
            return await cq.answer("Panel not found.", show_alert=True)
        kb = InlineKeyboardBuilder()
        kb.row(
            InlineKeyboardButton(text="✨ Auto-match both", callback_data=f"smma_imp:{pid}:both"),
        )
        kb.row(
            InlineKeyboardButton(text="✈️ Telegram matches", callback_data=f"smma_imp:{pid}:telegram"),
            InlineKeyboardButton(text="📸 Instagram matches", callback_data=f"smma_imp:{pid}:instagram"),
        )
        kb.row(
            InlineKeyboardButton(text="📦 Entire catalog → Telegram", callback_data=f"smma_imp:{pid}:all_telegram"),
            InlineKeyboardButton(text="Entire catalog → Instagram", callback_data=f"smma_imp:{pid}:all_instagram"),
        )
        kb.row(InlineKeyboardButton(text="🔙 Panel Settings", callback_data=f"smma_panel:{pid}"))
        text = (
            f"<h2>📦 Import · {h(panel.get('name'))}</h2>"
            + card(
                f"💰 <b>Markup:</b> <code>{get_import_markup():g}%</code> over provider rate",
                f"💱 <b>Provider currency:</b> <code>{h(panel.get('currency', 'USD'))}</code>",
                "Auto-match imports only catalog entries mentioning Telegram/Instagram in the name or category.",
                "Entire-catalog modes put every compatible link-and-quantity service into the chosen category.",
                "Special service types needing custom comments, packages, or subscriptions are skipped safely.",
            )
        )
        await edit_html(cq.message, text, kb.as_markup())
        await cq.answer()

    @dp.callback_query(F.data.startswith("smma_imp:"))
    async def smma_import_execute(cq: CallbackQuery):
        if not is_admin(cq.from_user.id):
            return
        try:
            _, pid, mode = cq.data.split(":", 2)
            panel = smm_panels.find_one({"_id": ObjectId(pid)})
        except Exception:
            panel = None
            pid = ""
            mode = ""
        if not panel:
            return await cq.answer("Panel not found.", show_alert=True)
        await cq.answer("Fetching catalog…")
        await edit_html(
            cq.message,
            f"<h2>⏳ Importing from {h(panel.get('name'))}</h2>"
            + card("Fetching the provider catalog and updating service rates. This may take a moment."),
        )
        result = await import_provider_catalog(panel, mode)
        if not result.get("ok"):
            text = "<h2>❌ Catalog Import Failed</h2>" + card(h(result.get("error", "Unknown provider error")))
        else:
            platform_label = "both platforms" if mode == "both" else mode.removeprefix("all_").title()
            text = (
                f"<h2>✅ Catalog Import Complete · {h(platform_label)}</h2>"
                + card(
                    f"➕ <b>New services:</b> <code>{result['new']:,}</code>",
                    f"🔄 <b>Updated services:</b> <code>{result['updated']:,}</code>",
                    f"💰 <b>Markup applied:</b> <code>{result['margin']:g}%</code>",
                    f"💱 <b>Rate currency:</b> <code>{h(result['currency'])}</code>",
                    f"↪️ <b>Unmatched for this selection:</b> <code>{result['unmatched']:,}</code>",
                    f"⚠️ <b>Invalid rows:</b> <code>{result['invalid']:,}</code>",
                    f"⏭️ <b>Special order types skipped:</b> <code>{result['unsupported']:,}</code>",
                )
                + "<p><i>Imported entries are now available in paginated Telegram/Instagram service lists.</i></p>"
            )
        kb = InlineKeyboardBuilder()
        kb.row(InlineKeyboardButton(text="🔁 Import Again", callback_data=f"smma_importpanel:{pid}"))
        kb.row(InlineKeyboardButton(text="📂 Manage Categories", callback_data="smma_listplat"))
        kb.row(InlineKeyboardButton(text="🔙 SMM Admin", callback_data="smma_back"))
        await edit_html(cq.message, text, kb.as_markup())

    @dp.callback_query(F.data == "smma_balance")
    async def smma_balance(cq: CallbackQuery):
        if not is_admin(cq.from_user.id):
            return await cq.answer("Not authorized.", show_alert=True)
        await smma_listpanels(cq)

    @dp.callback_query(F.data == "smma_addplat")
    async def smma_addplat(cq: CallbackQuery):
        if not is_admin(cq.from_user.id):
            return await cq.answer("Not authorized.", show_alert=True)
        await cq.answer("SMM categories are fixed to Telegram and Instagram.", show_alert=True)

    @dp.callback_query(F.data == "smma_listplat")
    async def smma_listplat(cq: CallbackQuery):
        if not is_admin(cq.from_user.id):
            return
        platforms = supported_platforms()
        kb = InlineKeyboardBuilder()
        for platform in platforms:
            kb.button(
                text=button_label(f"{platform.get('emoji', '📱')} {platform['name']}"),
                callback_data=f"smma_plat:{platform['_id']}:0",
            )
        kb.adjust(1)
        kb.row(InlineKeyboardButton(text="🔙 Back", callback_data="smma_back"))
        text = (
            "<h2>📂 Telegram &amp; Instagram Categories</h2>"
            + card("These are the only customer-facing SMM categories. Choose one to manage its catalog.")
        )
        await edit_html(cq.message, text, kb.as_markup())
        await cq.answer()

    async def render_platform_admin(cq: CallbackQuery, pid: str, page: int = 0):
        try:
            platform = smm_platforms.find_one({"_id": ObjectId(pid)})
        except Exception:
            platform = None
        if not platform or platform.get("slug") not in {p["slug"] for p in PLATFORM_PRESETS}:
            return await cq.answer("Platform not found.", show_alert=True)
        service_filter = {"platform_id": platform["_id"]}
        total_services = smm_services.count_documents(service_filter)
        total_pages = max(1, (total_services + ADMIN_SERVICES_PER_PAGE - 1) // ADMIN_SERVICES_PER_PAGE)
        page = min(max(0, page), total_pages - 1)
        services = list(
            smm_services.find(service_filter)
            .sort("name", 1)
            .skip(page * ADMIN_SERVICES_PER_PAGE)
            .limit(ADMIN_SERVICES_PER_PAGE)
        )
        kb = InlineKeyboardBuilder()
        kb.row(
            InlineKeyboardButton(text="➕ Add Service", callback_data=f"smma_addsvc:{pid}"),
            InlineKeyboardButton(text="📦 Import Catalog", callback_data="smma_import"),
        )
        for svc in services:
            price = float(svc.get("price_per_1000", 0) or 0)
            kb.button(
                text=button_label(f"⚙️ {svc.get('name', 'Service')} (₹{price:.2f}/1k)"),
                callback_data=f"smma_svc:{svc['_id']}",
            )
        kb.adjust(1)
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="⬅️ Previous", callback_data=f"smma_plat:{pid}:{page - 1}"))
        if page + 1 < total_pages:
            nav.append(InlineKeyboardButton(text="Next ➡️", callback_data=f"smma_plat:{pid}:{page + 1}"))
        if nav:
            kb.row(*nav)
        kb.row(InlineKeyboardButton(text="🔙 Back", callback_data="smma_listplat"))
        text = (
            f"<h2>{h(platform.get('emoji', '📱'))} {h(platform['name'])} Catalog</h2>"
            + card(
                f"📦 <b>Total services:</b> <code>{total_services:,}</code>",
                f"📄 <b>Page:</b> <code>{page + 1}/{total_pages}</code>",
                "Select a service to edit its selling rate or remove it.",
            )
        )
        await edit_html(cq.message, text, kb.as_markup())

    @dp.callback_query(F.data.startswith("smma_plat:"))
    async def smma_plat_manage(cq: CallbackQuery):
        if not is_admin(cq.from_user.id):
            return
        parts = cq.data.split(":")
        pid = parts[1] if len(parts) > 1 else ""
        page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        await render_platform_admin(cq, pid, page)
        await cq.answer()

    @dp.callback_query(F.data.startswith("smma_rmplat:"))
    async def smma_rmplat(cq: CallbackQuery):
        if not is_admin(cq.from_user.id):
            return await cq.answer("Not authorized.", show_alert=True)
        await cq.answer("Telegram and Instagram categories are fixed and cannot be removed.", show_alert=True)

    @dp.callback_query(F.data.startswith("smma_addsvc:"))
    async def smma_addsvc(cq: CallbackQuery, state: FSMContext):
        if not is_admin(cq.from_user.id):
            return
        pid = cq.data.split(":", 1)[1]
        try:
            platform = smm_platforms.find_one({"_id": ObjectId(pid)})
        except Exception:
            platform = None
        if not platform or platform.get("slug") not in {p["slug"] for p in PLATFORM_PRESETS}:
            return await cq.answer("Choose a Telegram or Instagram category.", show_alert=True)
        await state.update_data(platform_id=pid)
        panels = list(smm_panels.find({}))
        if not panels:
            return await cq.answer("Add a panel first.", show_alert=True)
        if len(panels) == 1:
            await state.update_data(panel_id=str(panels[0]["_id"]))
            await state.set_state(SMMAdmin.waiting_service_provider_id)
            await edit_html(
                cq.message,
                f"<h2>🏢 {h(panels[0]['name'])}</h2><blockquote>🆔 <i>Send the provider service ID from that panel's service list.</i></blockquote>",
            )
            await cq.answer()
            return
        kb = InlineKeyboardBuilder()
        for panel in panels:
            kb.button(text=button_label(f"🏢 {panel['name']}"), callback_data=f"smma_addsvc_panel:{panel['_id']}")
        kb.adjust(1)
        await edit_html(cq.message, "<h2>🏢 Select Provider Panel</h2><blockquote><i>Orders for this service will go through that panel's API.</i></blockquote>", kb.as_markup())
        await cq.answer()

    @dp.callback_query(F.data.startswith("smma_addsvc_panel:"))
    async def smma_addsvc_pickpanel(cq: CallbackQuery, state: FSMContext):
        if not is_admin(cq.from_user.id):
            return
        pid = cq.data.split(":", 1)[1]
        panel = smm_panels.find_one({"_id": ObjectId(pid)})
        await state.update_data(panel_id=pid)
        await state.set_state(SMMAdmin.waiting_service_provider_id)
        await edit_html(
            cq.message,
            f"<h2>🏢 {h(panel['name'] if panel else 'Panel')}</h2><blockquote><i>Send the provider service ID.</i></blockquote>",
        )
        await cq.answer()

    @dp.message(SMMAdmin.waiting_service_provider_id)
    async def smma_svc_provider_id(msg: Message, state: FSMContext):
        if not is_admin(msg.from_user.id):
            return
        if msg.text and msg.text.startswith("/"):
            await state.clear()
            raise SkipHandler()
        provider_id = (msg.text or "").strip()
        if not provider_id or len(provider_id) > 64 or any(ch.isspace() for ch in provider_id):
            return await msg.answer("<h2>❌ Invalid Service ID</h2><p>Send the provider's service ID (up to 64 characters).</p>", parse_mode="HTML")
        await state.update_data(provider_service_id=int(provider_id) if provider_id.isdigit() else provider_id)
        await state.set_state(SMMAdmin.waiting_service_name)
        await msg.answer("<h2>📝 Display Name</h2><blockquote><i>Users will see this name. For example <u>Instagram Views — Fast</u>.</i></blockquote>", parse_mode="HTML")

    @dp.message(SMMAdmin.waiting_service_name)
    async def smma_svc_name(msg: Message, state: FSMContext):
        if not is_admin(msg.from_user.id):
            return
        name = (msg.text or "").strip()
        if not name or len(name) > 180:
            return await msg.answer("<h2>❌ Name Required</h2><p>Send a service name up to 180 characters.</p>", parse_mode="HTML")
        await state.update_data(service_name=name)
        await state.set_state(SMMAdmin.waiting_service_price)
        await msg.answer("<h2>💰 Selling Rate per 1,000</h2><blockquote><i>Send a positive INR number, for example <u>4</u> or <u>4.5</u>.</i></blockquote>", parse_mode="HTML")

    @dp.message(SMMAdmin.waiting_service_price)
    async def smma_svc_price(msg: Message, state: FSMContext):
        if not is_admin(msg.from_user.id):
            return
        try:
            price = float((msg.text or "").strip())
            if not math.isfinite(price) or price <= 0:
                raise ValueError
        except (TypeError, ValueError):
            return await msg.answer("<h2>❌ Send a Positive Valid Number</h2>", parse_mode="HTML")
        await state.update_data(price=price)
        await state.set_state(SMMAdmin.waiting_service_min)
        await msg.answer("<h2>📉 Minimum Quantity</h2>", parse_mode="HTML")

    @dp.message(SMMAdmin.waiting_service_min)
    async def smma_svc_min(msg: Message, state: FSMContext):
        if not is_admin(msg.from_user.id):
            return
        if not (msg.text or "").strip().isdigit() or int(msg.text.strip()) <= 0:
            return await msg.answer("<h2>❌ Send a Positive Quantity</h2>", parse_mode="HTML")
        await state.update_data(min_qty=int(msg.text.strip()))
        await state.set_state(SMMAdmin.waiting_service_max)
        await msg.answer("<h2>📈 Maximum Quantity</h2>", parse_mode="HTML")

    @dp.message(SMMAdmin.waiting_service_max)
    async def smma_svc_max(msg: Message, state: FSMContext):
        if not is_admin(msg.from_user.id):
            return
        if not (msg.text or "").strip().isdigit():
            return await msg.answer("<h2>❌ Send a Number</h2>", parse_mode="HTML")
        data = await state.get_data()
        max_qty = int(msg.text.strip())
        if max_qty < data["min_qty"]:
            return await msg.answer("<h2>❌ Max Cannot Be Lower Than Min</h2>", parse_mode="HTML")
        smm_services.insert_one({
            "platform_id": ObjectId(data["platform_id"]),
            "panel_id": ObjectId(data["panel_id"]),
            "provider_service_id": data["provider_service_id"],
            "name": data["service_name"],
            "price_per_1000": data["price"],
            "price_source": "manual",
            "catalog_imported": False,
            "min": data["min_qty"],
            "max": max_qty,
            "active": True,
        })
        await state.clear()
        kb = InlineKeyboardBuilder()
        kb.row(InlineKeyboardButton(text="🔙 Back to Platform", callback_data=f"smma_plat:{data['platform_id']}"))
        await msg.answer(f"<h2>✅ Service {h(data['service_name'])} Added</h2>", parse_mode="HTML", reply_markup=kb.as_markup())

    @dp.callback_query(F.data.startswith("smma_svc:"))
    async def smma_svc_manage(cq: CallbackQuery):
        if not is_admin(cq.from_user.id):
            return
        sid = cq.data.split(":", 1)[1]
        svc = smm_services.find_one({"_id": ObjectId(sid)})
        if not svc:
            return await cq.answer("Service not found.", show_alert=True)
        panel = smm_panels.find_one({"_id": svc.get("panel_id")})
        kb = InlineKeyboardBuilder()
        kb.button(text="✏️ Edit Price", callback_data=f"smma_editprice:{sid}")
        kb.button(text="🗑️ Remove Service", callback_data=f"smma_rmsvc:{sid}")
        kb.adjust(1)
        kb.row(InlineKeyboardButton(text="🔙 Back", callback_data=f"smma_plat:{svc['platform_id']}"))
        text = (
            f"<h2>⚙️ {h(svc['name'])}</h2>"
            + card(
                f"🏢 <b>Panel:</b> {h(panel['name'] if panel else 'Unknown')}",
                f"🆔 <b>Provider ID:</b> <code>{h(svc.get('provider_service_id'))}</code>",
                f"💰 <b>Price:</b> <code>{h(inr(svc.get('price_per_1000')))}</code> <i>/ 1,000</i>",
                f"📉 <b>Min:</b> <code>{h(qty_fmt(svc.get('min')))}</code>",
                f"📈 <b>Max:</b> <code>{h(qty_fmt(svc.get('max')))}</code>",
            )
        )
        await edit_html(cq.message, text, kb.as_markup())
        await cq.answer()

    @dp.callback_query(F.data.startswith("smma_rmsvc:"))
    async def smma_rmsvc(cq: CallbackQuery):
        if not is_admin(cq.from_user.id):
            return
        sid = cq.data.split(":", 1)[1]
        svc = smm_services.find_one({"_id": ObjectId(sid)})
        if not svc:
            return await cq.answer("Already removed.", show_alert=True)
        smm_services.delete_one({"_id": ObjectId(sid)})
        await cq.answer("Service removed.", show_alert=True)
        await render_platform_admin(cq, str(svc["platform_id"]))

    @dp.callback_query(F.data.startswith("smma_editprice:"))
    async def smma_editprice(cq: CallbackQuery, state: FSMContext):
        if not is_admin(cq.from_user.id):
            return
        await state.update_data(edit_service_id=cq.data.split(":", 1)[1])
        await state.set_state(SMMAdmin.waiting_edit_price)
        await edit_html(cq.message, "<h2>💰 New Price per 1,000</h2><blockquote><i>Send an INR number.</i></blockquote>")
        await cq.answer()

    @dp.message(SMMAdmin.waiting_edit_price)
    async def smma_editprice_save(msg: Message, state: FSMContext):
        if not is_admin(msg.from_user.id):
            return
        try:
            price = float((msg.text or "").strip())
        except ValueError:
            return await msg.answer("<h2>❌ Send a Valid Number</h2>", parse_mode="HTML")
        data = await state.get_data()
        if not math.isfinite(price) or price <= 0:
            return await msg.answer("<h2>❌ Price Must Be Positive</h2>", parse_mode="HTML")
        smm_services.update_one(
            {"_id": ObjectId(data["edit_service_id"])},
            {"$set": {"price_per_1000": price, "price_source": "manual"}},
        )
        await state.clear()
        kb = InlineKeyboardBuilder()
        kb.row(InlineKeyboardButton(text="🔙 SMM Admin", callback_data="smma_back"))
        await msg.answer(f"<h2>✅ Price Updated</h2><blockquote>New rate: <code>{h(inr(price))}</code> / 1,000</blockquote>", parse_mode="HTML", reply_markup=kb.as_markup())

    @dp.callback_query(F.data == "smma_allorders")
    async def smma_allorders(cq: CallbackQuery):
        if not is_admin(cq.from_user.id):
            return
        orders = list(smm_orders.find({}).sort("created_at", -1).limit(12))
        kb = InlineKeyboardBuilder()
        kb.row(InlineKeyboardButton(text="🔙 Back", callback_data="smma_back"))
        if not orders:
            return await edit_html(cq.message, "<h2>📊 Overall History</h2><blockquote>😔 <i>No orders yet.</i></blockquote>", kb.as_markup())
        lines = ["<h2>📊 Last 12 SMM Orders</h2>"]
        for order in orders:
            when = order.get("created_at")
            date_str = when.strftime("%d %b, %H:%M") if isinstance(when, datetime) else "—"
            price = order.get("price")
            try:
                price_txt = f"₹{float(price):.2f}"
            except (TypeError, ValueError):
                price_txt = "₹0.00"
            lines.append(
                "<blockquote>"
                f"{status_emoji(order.get('status'))} <b>{h(order.get('platform_emoji') or '📱')} {h(order.get('service_name'))}</b>\n"
                f"👤 <code>{h(order.get('user_id'))}</code>  🆔 <code>{h(order['_id'])}</code>\n"
                f"🔢 {h(qty_fmt(order.get('quantity')))}   💰 {h(price_txt)}   📊 {h(order.get('status'))}   🕒 {h(date_str)}"
                "</blockquote>"
            )
        await edit_html(cq.message, "".join(lines), kb.as_markup())
        await cq.answer()

    @dp.callback_query(F.data == "smma_checkorder")
    async def smma_checkorder(cq: CallbackQuery, state: FSMContext):
        if not is_admin(cq.from_user.id):
            return
        await state.set_state(SMMAdmin.waiting_admin_order_id)
        kb = InlineKeyboardBuilder()
        kb.row(InlineKeyboardButton(text="🔙 Back", callback_data="smma_back"))
        await edit_html(
            cq.message,
            "<h2>🔍 Check Any Order</h2><blockquote><i>Send the user's order ID. If it completed or was canceled and they were not notified, the live status is also sent to them.</i></blockquote>",
            kb.as_markup(),
        )
        await cq.answer()

    @dp.message(SMMAdmin.waiting_admin_order_id)
    async def smma_checkorder_result(msg: Message, state: FSMContext):
        if not is_admin(msg.from_user.id):
            return
        await state.clear()
        try:
            order = smm_orders.find_one({"_id": ObjectId((msg.text or "").strip())})
        except Exception:
            order = None
        if not order:
            return await msg.answer("<b>❌ Order not found.</b>", parse_mode="HTML")
        await show_live_status(msg, order, edit=False, notify_user=True, actor_id=msg.from_user.id)

    async def smm_background_checker():
        """Polls until the process stops. One bad order cannot kill the loop."""
        await asyncio.sleep(8)
        while True:
            try:
                await check_active_orders()
            except Exception as exc:
                logger.exception("SMM checker cycle failed")
                smm_settings.update_one(
                    {"_id": "config"},
                    {"$set": {"checker_last_error": str(exc)[:400], "checker_last_run": datetime.utcnow()}},
                    upsert=True,
                )
                await log_admin(f"⚠️ <b>Checker cycle error</b>\n\n<pre>{h(exc)}</pre>\n<i>The next cycle will still run. The task was not killed.</i>")
            await asyncio.sleep(CHECK_EVERY_SECONDS)

    return smm_background_checker, smm_send_menu
