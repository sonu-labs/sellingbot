"""
SMM reseller module for aiogram 3.

Wire-up (unchanged):

    checker, smm_send_menu = register_smm_handlers(
        dp, bot, db, users_col, admin_ids, admin_log_id, public_log_id
    )
    asyncio.create_task(checker())   # do not await — it loops forever

Panel key is NOT stored in this file. First-run seed reads:
    SMM_PANEL_URL   (default https://cheapestsmmpanels.com/api/v2)
    SMM_PANEL_KEY
Rotate any key that was ever pasted into source or chat.
"""

import asyncio
import logging
import os
import re
from datetime import datetime, timedelta
from html import escape

import aiohttp
from aiogram import F
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from bson import ObjectId
from pymongo import ReturnDocument

logger = logging.getLogger("smm")

# First-run seed only. Live panels already saved in Mongo are not touched.
DEFAULT_PANEL_URL = os.getenv("SMM_PANEL_URL", "https://cheapestsmmpanels.com/api/v2")
DEFAULT_PANEL_KEY = os.getenv("SMM_PANEL_KEY", "")
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
    waiting_platform_name = State()
    waiting_platform_emoji = State()
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
    if not isinstance(when, datetime):
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

    def ensure_default_panel():
        if smm_panels.count_documents({}) == 0 and DEFAULT_PANEL_KEY and "YOUR_" not in DEFAULT_PANEL_KEY:
            result = smm_panels.insert_one({
                "name": "Panel 1",
                "api_url": DEFAULT_PANEL_URL,
                "api_key": DEFAULT_PANEL_KEY,
                "added_at": datetime.utcnow(),
            })
            smm_panels_id = result.inserted_id
            smm_services.update_many(
                {"panel_id": {"$exists": False}},
                {"$set": {"panel_id": smm_panels_id}},
            )

    ensure_default_panel()

    def is_admin(uid: int) -> bool:
        return uid in admin_ids

    def get_config() -> dict:
        return smm_settings.find_one({"_id": "config"}) or {}

    def public_log_enabled() -> bool:
        return get_config().get("public_log_enabled", True)

    def smm_enabled() -> bool:
        return get_config().get("smm_enabled", True)

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
                "<h2>🌐 Social Media Services</h2>"
                + card(
                    "🚧 <b>Ordering is paused.</b> Existing orders are safe.",
                    f"👤 <b>User ID:</b> <code>{h(user_id)}</code>",
                    f"💰 <b>Balance:</b> <code>{h(inr(balance))}</code>",
                    f"📦 <b>Open orders:</b> <code>{open_orders}</code>",
                )
                + "<p><i>Use Status or History below.</i></p>"
            )
        else:
            platforms = list(smm_platforms.find({}))
            text = (
                "<h2>🌐 Social Media Services</h2>"
                + card(
                    "✨ Fast delivery · you are charged only after confirmation",
                    "👇 Choose a platform below to start",
                    f"👤 <b>User ID:</b> <code>{h(user_id)}</code>",
                    f"💰 <b>Balance:</b> <code>{h(inr(balance))}</code>",
                    f"📦 <b>Open orders:</b> <code>{open_orders}</code>",
                )
            )
            if not platforms:
                text += "<p>😔 <i>No platforms available yet. Please try again later.</i></p>"
            else:
                for platform in platforms:
                    kb.button(
                        text=button_label(f"{platform.get('emoji', '📱')} {platform['name']}"),
                        callback_data=f"smm_plat:{platform['_id']}",
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
        platform_id = cq.data.split(":", 1)[1]
        try:
            platform = smm_platforms.find_one({"_id": ObjectId(platform_id)})
        except Exception:
            platform = None
        if not platform:
            return await cq.answer("❌ Platform not found.", show_alert=True)

        services = list(smm_services.find({"platform_id": ObjectId(platform_id)}))
        kb = InlineKeyboardBuilder()
        title = f"{platform.get('emoji', '📱')} {platform['name']}"
        if not services:
            text = (
                f"<h2>{h(title)}</h2>"
                + card("😔 <i>No services on this platform yet. Try another one.</i>")
            )
        else:
            disc = get_user_discount(cq.from_user.id)
            card_lines = [
                "👇 Pick a service below. Rate is <b>per 1,000</b>.",
                "Nothing is charged until you confirm.",
                f"📦 <b>Available Services:</b> <code>{len(services)}</code>",
            ]
            if disc > 0:
                card_lines.append(f"🏷️ <b>VIP Discount Active:</b> <code>{disc:g}% OFF</code>")
            text = f"<h2>{h(title)}</h2>" + card(*card_lines)
            for svc in services:
                rate_1k = get_service_rate_1k(svc, cq.from_user.id)
                kb.button(
                    text=button_label(f"{svc['name']} — ₹{rate_1k:.2f}/1k"),
                    callback_data=f"smm_svc:{svc['_id']}",
                )
            kb.adjust(1)
        kb.row(InlineKeyboardButton(text="🔙 Back", callback_data="smm_open"))
        await edit_html(cq.message, text, kb.as_markup())
        await cq.answer()

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
        svc_status = "🟢 ON" if cfg.get("smm_enabled", True) else "🔴 OFF"
        last_run = cfg.get("checker_last_run")
        checker_line = age_label(last_run) if last_run else "not running"
        stale = (not last_run) or (datetime.utcnow() - last_run > timedelta(minutes=5))
        kb = InlineKeyboardBuilder()
        kb.row(
            InlineKeyboardButton(text="➕ Add Platform", callback_data="smma_addplat"),
            InlineKeyboardButton(text="📂 Manage Platforms", callback_data="smma_listplat"),
        )
        kb.row(
            InlineKeyboardButton(text="🏢 Manage Panels", callback_data="smma_listpanels"),
            InlineKeyboardButton(text="➕ Add Panel", callback_data="smma_addpanel"),
        )
        kb.row(
            InlineKeyboardButton(text=f"📢 Public Log: {pub_status}", callback_data="smma_togglepublic"),
            InlineKeyboardButton(text=f"🌐 Services: {svc_status}", callback_data="smma_togglesmm"),
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
            warn = "<blockquote>⚠️ <i>Auto-check has not run for 5+ minutes.</i></blockquote>"
        if smm_panels.count_documents({}) == 0:
            warn += "<blockquote>🏢 <i>No panel saved. Add one below.</i></blockquote>"
        text = (
            "<h2>🛠️ SMM Admin Control Center</h2>"
            + card(
                f"🌐 <b>Ordering:</b> {svc_status}",
                f"📢 <b>Public log:</b> {pub_status}",
                f"🔄 <b>Auto-check:</b> <code>{h(checker_line)}</code>",
                f"📦 <b>Last cycle:</b> <code>{h(cfg.get('checker_last_count', 0))}</code> orders",
            )
            + warn
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
        smm_settings.update_one({"_id": "config"}, {"$set": {"smm_enabled": not current}}, upsert=True)
        await cq.answer(f"Ordering {'OFF' if current else 'ON'}", show_alert=True)
        await smmadmin_menu(cq.message, edit=True)

    @dp.callback_query(F.data == "smma_addpanel")
    async def smma_addpanel(cq: CallbackQuery, state: FSMContext):
        if not is_admin(cq.from_user.id):
            return
        await state.set_state(SMMAdmin.waiting_panel_name)
        kb = InlineKeyboardBuilder()
        kb.row(InlineKeyboardButton(text="🔙 Back", callback_data="smma_back"))
        await edit_html(
            cq.message,
            "<h2>📝 Panel Name</h2><blockquote><i>For example <u>Panel 2</u>. This name is only shown in the admin list.</i></blockquote>",
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
        await state.update_data(panel_name=(msg.text or "").strip())
        await state.set_state(SMMAdmin.waiting_panel_url)
        await msg.answer(
            "<h2>🔗 API URL</h2><blockquote><i>Example: <u>https://panelsite.com/api/v2</u></i></blockquote>",
            parse_mode="HTML",
        )

    @dp.message(SMMAdmin.waiting_panel_url)
    async def smma_panelurl(msg: Message, state: FSMContext):
        if not is_admin(msg.from_user.id):
            return
        if msg.text and msg.text.startswith("/"):
            await state.clear()
            raise SkipHandler()
        await state.update_data(panel_url=(msg.text or "").strip())
        await state.set_state(SMMAdmin.waiting_panel_key)
        await msg.answer(
            "<h2>🔑 API Key</h2><blockquote><i>Send the key. The message is deleted after it is saved. Do not put the key in source code.</i></blockquote>",
            parse_mode="HTML",
        )

    @dp.message(SMMAdmin.waiting_panel_key)
    async def smma_panelkey(msg: Message, state: FSMContext):
        if not is_admin(msg.from_user.id):
            return
        data = await state.get_data()
        panel = {
            "name": data["panel_name"],
            "api_url": data["panel_url"],
            "api_key": (msg.text or "").strip(),
            "added_at": datetime.utcnow(),
        }
        success, resp, raw = await smm_api_call(panel, "balance")
        smm_panels.insert_one(panel)
        await state.clear()
        try:
            await msg.delete()
        except Exception:
            pass
        kb = InlineKeyboardBuilder()
        kb.row(InlineKeyboardButton(text="🔙 SMM Admin", callback_data="smma_back"))
        if success:
            await msg.answer(
                f"<h2>✅ Panel {h(panel['name'])} Added</h2>"
                + card(f"💰 <b>Balance:</b> <code>{h(resp.get('balance'))} {h(resp.get('currency'))}</code>"),
                parse_mode="HTML",
                reply_markup=kb.as_markup(),
            )
        else:
            await msg.answer(
                f"<h2>⚠️ Panel {h(panel['name'])} Saved</h2>"
                "<blockquote><i>Balance check failed — the URL or key may be wrong. Check again from Manage Panels.</i></blockquote>",
                parse_mode="HTML",
                reply_markup=kb.as_markup(),
            )

    @dp.callback_query(F.data == "smma_listpanels")
    async def smma_listpanels(cq: CallbackQuery):
        if not is_admin(cq.from_user.id):
            return
        panels = list(smm_panels.find({}))
        kb = InlineKeyboardBuilder()
        if not panels:
            kb.row(InlineKeyboardButton(text="🔙 Back", callback_data="smma_back"))
            return await edit_html(cq.message, "<h2>🏢 Panels</h2><blockquote><i>No panels yet.</i></blockquote>", kb.as_markup())
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
        panel = smm_panels.find_one({"_id": ObjectId(pid)})
        if not panel:
            return await cq.answer("Panel not found.", show_alert=True)
        svc_count = smm_services.count_documents({"panel_id": ObjectId(pid)})
        kb = InlineKeyboardBuilder()
        kb.button(text="💰 Check Balance", callback_data=f"smma_panelbal:{pid}")
        kb.button(text="🗑️ Remove Panel", callback_data=f"smma_rmpanel:{pid}")
        kb.adjust(1)
        kb.row(InlineKeyboardButton(text="🔙 Back", callback_data="smma_listpanels"))
        text = (
            f"<h2>🏢 {h(panel['name'])}</h2>"
            + card(
                f"🔗 <b>URL:</b> <code>{h(panel.get('api_url'))}</code>",
                f"📦 <b>Services:</b> <code>{svc_count}</code>",
            )
        )
        await edit_html(cq.message, text, kb.as_markup())
        await cq.answer()

    @dp.callback_query(F.data.startswith("smma_panelbal:"))
    async def smma_panelbal(cq: CallbackQuery):
        if not is_admin(cq.from_user.id):
            return
        pid = cq.data.split(":", 1)[1]
        panel = smm_panels.find_one({"_id": ObjectId(pid)})
        if not panel:
            return await cq.answer("Panel not found.", show_alert=True)
        success, resp, raw = await smm_api_call(panel, "balance")
        if success:
            await cq.answer(f"{panel['name']}: {resp.get('balance')} {resp.get('currency')}", show_alert=True)
        else:
            await cq.answer("Balance check failed. The URL or key may be wrong.", show_alert=True)

    @dp.callback_query(F.data.startswith("smma_rmpanel:"))
    async def smma_rmpanel(cq: CallbackQuery):
        if not is_admin(cq.from_user.id):
            return
        pid = cq.data.split(":", 1)[1]
        linked = smm_services.count_documents({"panel_id": ObjectId(pid)})
        if linked > 0:
            return await cq.answer(f"Remove the {linked} service(s) still using this panel first.", show_alert=True)
        smm_panels.delete_one({"_id": ObjectId(pid)})
        await cq.answer("Panel removed.", show_alert=True)
        await smma_listpanels(cq)

    @dp.callback_query(F.data == "smma_balance")
    async def smma_balance(cq: CallbackQuery):
        await smma_listpanels(cq)

    @dp.callback_query(F.data == "smma_addplat")
    async def smma_addplat(cq: CallbackQuery, state: FSMContext):
        if not is_admin(cq.from_user.id):
            return
        await state.set_state(SMMAdmin.waiting_platform_name)
        kb = InlineKeyboardBuilder()
        kb.row(InlineKeyboardButton(text="🔙 Back", callback_data="smma_back"))
        await edit_html(
            cq.message,
            "<h2>📝 Platform Name</h2><blockquote><i>For example <u>Telegram</u> or <u>Instagram</u>.</i></blockquote>",
            kb.as_markup(),
        )
        await cq.answer()

    @dp.message(SMMAdmin.waiting_platform_name)
    async def smma_platname(msg: Message, state: FSMContext):
        if not is_admin(msg.from_user.id):
            return
        if msg.text and msg.text.startswith("/"):
            await state.clear()
            raise SkipHandler()
        await state.update_data(platform_name=(msg.text or "").strip())
        await state.set_state(SMMAdmin.waiting_platform_emoji)
        await msg.answer("<h2>😀 Platform Emoji</h2><blockquote><i>Send one emoji, for example ✈️ or 📸.</i></blockquote>", parse_mode="HTML")

    @dp.message(SMMAdmin.waiting_platform_emoji)
    async def smma_platemoji(msg: Message, state: FSMContext):
        if not is_admin(msg.from_user.id):
            return
        data = await state.get_data()
        smm_platforms.insert_one({"name": data["platform_name"], "emoji": (msg.text or "📱").strip()[:8]})
        await state.clear()
        kb = InlineKeyboardBuilder()
        kb.row(InlineKeyboardButton(text="🔙 SMM Admin", callback_data="smma_back"))
        await msg.answer(f"<h2>✅ Platform {h(data['platform_name'])} Added</h2>", parse_mode="HTML", reply_markup=kb.as_markup())

    @dp.callback_query(F.data == "smma_listplat")
    async def smma_listplat(cq: CallbackQuery):
        if not is_admin(cq.from_user.id):
            return
        platforms = list(smm_platforms.find({}))
        kb = InlineKeyboardBuilder()
        for platform in platforms:
            kb.button(
                text=button_label(f"{platform.get('emoji', '📱')} {platform['name']}"),
                callback_data=f"smma_plat:{platform['_id']}",
            )
        kb.adjust(1)
        kb.row(InlineKeyboardButton(text="🔙 Back", callback_data="smma_back"))
        text = "<h2>📂 Manage Platforms</h2><p>Select a platform below to manage its services:</p>" if platforms else "<h2>📂 Manage Platforms</h2><blockquote><i>No platforms yet.</i></blockquote>"
        await edit_html(cq.message, text, kb.as_markup())
        await cq.answer()

    async def render_platform_admin(cq: CallbackQuery, pid: str):
        platform = smm_platforms.find_one({"_id": ObjectId(pid)})
        if not platform:
            return await cq.answer("Platform not found.", show_alert=True)
        services = list(smm_services.find({"platform_id": ObjectId(pid)}))
        kb = InlineKeyboardBuilder()
        kb.button(text="➕ Add Service", callback_data=f"smma_addsvc:{pid}")
        for svc in services:
            kb.button(
                text=button_label(f"⚙️ {svc['name']} (₹{svc['price_per_1000']})"),
                callback_data=f"smma_svc:{svc['_id']}",
            )
        kb.button(text="🗑️ Remove Platform", callback_data=f"smma_rmplat:{pid}")
        kb.adjust(1)
        kb.row(InlineKeyboardButton(text="🔙 Back", callback_data="smma_listplat"))
        text = (
            f"<h2>{h(platform.get('emoji', '📱'))} {h(platform['name'])}</h2>"
            + card(f"📦 <b>Services:</b> <code>{len(services)}</code>")
        )
        await edit_html(cq.message, text, kb.as_markup())

    @dp.callback_query(F.data.startswith("smma_plat:"))
    async def smma_plat_manage(cq: CallbackQuery):
        if not is_admin(cq.from_user.id):
            return
        await render_platform_admin(cq, cq.data.split(":", 1)[1])
        await cq.answer()

    @dp.callback_query(F.data.startswith("smma_rmplat:"))
    async def smma_rmplat(cq: CallbackQuery):
        if not is_admin(cq.from_user.id):
            return
        pid = cq.data.split(":", 1)[1]
        smm_services.delete_many({"platform_id": ObjectId(pid)})
        smm_platforms.delete_one({"_id": ObjectId(pid)})
        await cq.answer("Platform and its services removed.", show_alert=True)
        await smma_listplat(cq)

    @dp.callback_query(F.data.startswith("smma_addsvc:"))
    async def smma_addsvc(cq: CallbackQuery, state: FSMContext):
        if not is_admin(cq.from_user.id):
            return
        pid = cq.data.split(":", 1)[1]
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
        if not (msg.text or "").strip().isdigit():
            return await msg.answer("<h2>❌ Send a Number</h2>", parse_mode="HTML")
        await state.update_data(provider_service_id=int(msg.text.strip()))
        await state.set_state(SMMAdmin.waiting_service_name)
        await msg.answer("<h2>📝 Display Name</h2><blockquote><i>Users will see this name. For example <u>Telegram Post Views — Instant</u>.</i></blockquote>", parse_mode="HTML")

    @dp.message(SMMAdmin.waiting_service_name)
    async def smma_svc_name(msg: Message, state: FSMContext):
        if not is_admin(msg.from_user.id):
            return
        await state.update_data(service_name=(msg.text or "").strip())
        await state.set_state(SMMAdmin.waiting_service_price)
        await msg.answer("<h2>💰 Price per 1,000</h2><blockquote><i>Send an INR number, for example <u>4</u> or <u>4.5</u>.</i></blockquote>", parse_mode="HTML")

    @dp.message(SMMAdmin.waiting_service_price)
    async def smma_svc_price(msg: Message, state: FSMContext):
        if not is_admin(msg.from_user.id):
            return
        try:
            price = float((msg.text or "").strip())
        except ValueError:
            return await msg.answer("<h2>❌ Send a Valid Number</h2>", parse_mode="HTML")
        await state.update_data(price=price)
        await state.set_state(SMMAdmin.waiting_service_min)
        await msg.answer("<h2>📉 Minimum Quantity</h2>", parse_mode="HTML")

    @dp.message(SMMAdmin.waiting_service_min)
    async def smma_svc_min(msg: Message, state: FSMContext):
        if not is_admin(msg.from_user.id):
            return
        if not (msg.text or "").strip().isdigit():
            return await msg.answer("<h2>❌ Send a Number</h2>", parse_mode="HTML")
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
            "min": data["min_qty"],
            "max": max_qty,
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
        smm_services.update_one({"_id": ObjectId(data["edit_service_id"])}, {"$set": {"price_per_1000": price}})
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
