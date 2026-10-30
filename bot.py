import secrets
import string
import logging
import bcrypt
from bson import ObjectId
import os
import asyncio
import aiohttp
from aiogram.types import InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram import BaseMiddleware
from typing import Callable, Dict, Any, Awaitable
from aiogram.types import TelegramObject
import html
from html import escape
from bson import ObjectId
from aiogram.types import CopyTextButton
from telethon.tl.functions.account import GetAuthorizationsRequest, ResetAuthorizationRequest
from aiogram.fsm.context import FSMContext
from datetime import datetime, timezone, timedelta
from aiogram import Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from pymongo import MongoClient
from telethon import TelegramClient
from telethon.sessions import StringSession
from aiogram.utils.deep_linking import create_start_link
from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError, PasswordHashInvalidError
import re
from aiogram import types
import random
from aiogram.types import InputMediaVideo
from admin_analytics import summarize_analytics, utc_naive
from recharge_flow import register_recharge_handlers
from smm import register_smm_handlers
from server3 import register_server3_handlers
from allapps import (
    register_server3_handlers as register_allapps_handlers,
    send_s3_operator_menu_direct,
    client as _allapps_client,
    clear_s3_rate_cache as _clear_allapps_rate_cache,
)
from server4 import (
    register_server4_handlers,
    send_s4_operator_menu_direct,
    client as _server4_client,
    clear_s4_rate_cache as _clear_server4_rate_cache,
)
from otp import register_otp_handlers
from otp_admin import register_otp_admin_handlers
from mustjoin import (
    check_join,
    get_force_sub_channels,
    is_force_sub_enabled,
    toggle_force_sub,
    add_force_sub_channel,
    remove_force_sub_channel,
)
from config import (
    BOT_TOKEN, ADMIN_IDS, MONGO_URI, BOT_USERNAME, SUPPORT_USERNAME,
    UPDATES_CHANNEL, SALES_LOG_CHANNEL, ADMIN_LOG_ID, SALES_LOG_ID,
    TEMPORASMS_API_KEY, OTP_MARKUP_PERCENT, TGLION_API_KEY, TGLION_ID, TGPVA_API_KEY,
    USD_TO_INR,
)
from rich_messages import (
    RichBot,
    TELEGRAM_EMOJI_ID,
    WHATSAPP_EMOJI_ID,
    API_DOCS_EMOJI_ID,
    UPI_EMOJI_ID,
    tg_icon,
    wa_icon,
    api_icon,
    upi_icon,
    premium_button,
    safe_edit_or_send,
    plain_country_label,
)
from i18n import LANGUAGES, CURRENCIES, tr, format_balance, language_name

# ================= MongoDB Setup =================
client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=15000)
db = client["TGAStoreBot"]
users_col = db["users"]
orders_col = db["orders"]
countries_col = db["countries"]
numbers_col = db["numbers"]
crypto_col = db["crypto_invoices"]
withdrawals_col = db["withdrawals"]
settings_col = db["settings"]
store_col = db["store_products"]
s3_catalog = db["server3_catalog"]

# Runtime branding/configuration (usernames are stored without @).
BOTUSER = BOT_USERNAME
SUPPORT = SUPPORT_USERNAME
USAGE = SALES_LOG_CHANNEL
OWNER = SUPPORT_USERNAME
UPDATES = UPDATES_CHANNEL
CHANNEL = UPDATES_CHANNEL
SALES = SALES_LOG_ID
SALESLOG = SALES_LOG_CHANNEL
ADMINLOG = ADMIN_LOG_ID
logger = logging.getLogger("sellingbot")
BOT_STARTED_AT = datetime.now(timezone.utc)
SMM_BACKGROUND_TASK = None
# ================= STORE DATABASE & FSM =================
class StoreAdmin(StatesGroup):
    waiting_category = State()
    waiting_name = State()
    waiting_file = State()
    waiting_text = State()
    waiting_time = State()
    waiting_stock = State()
    waiting_price = State()

class RemoveProduct(StatesGroup):
    waiting_product = State()
    
class TransferBalance(StatesGroup):
    waiting_recipient = State()
    waiting_amount = State()


# ================= Ban Middleware =================
class FeatureGateMiddleware(BaseMiddleware):
    """Reject stale/deep-link callbacks when an admin disabled a service."""

    async def __call__(self, handler, event, data):
        if isinstance(event, CallbackQuery):
            callback = event.data or ""
            doc = settings_col.find_one({"_id": "bot_features"}) or {}
            srv = settings_col.find_one({"_id": "server_config"}) or {}
            store_on = doc.get("store_enabled", srv.get("store_status", "on") != "off")
            smm_read_only_callbacks = ("smm_open", "smm_status_menu", "smm_history", "smm_rf:")
            if (
                callback.startswith("smm_")
                and not callback.startswith("smm_adm")
                and not callback.startswith(smm_read_only_callbacks)
                and not doc.get("smm_enabled", True)
            ):
                await event.answer("⚠️ Social Media Services are currently disabled.", show_alert=True)
                return
            whatsapp_callbacks = ("buy_wa", "buy_server3:wa")
            if callback.startswith(whatsapp_callbacks) and not doc.get("whatsapp_enabled", True):
                await event.answer("⚠️ WhatsApp services are currently disabled.", show_alert=True)
                return
            store_user_callbacks = ("store_menu", "usr_cat:", "buy_prod:", "confirm_buy_prod:")
            if callback.startswith(store_user_callbacks) and not store_on:
                await event.answer("⚠️ Digital Store is currently disabled.", show_alert=True)
                return
        return await handler(event, data)


ROOT_ADMIN_IDS = tuple(ADMIN_IDS)


def sync_admin_ids() -> list[int]:
    try:
        doc = settings_col.find_one({"_id": "admin_config"}) or {}
    except Exception:
        doc = {}
    extra = [int(x) for x in doc.get("extra_admins", []) if str(x).lstrip("-").isdigit()]
    merged = list(dict.fromkeys([*ROOT_ADMIN_IDS, *extra]))
    ADMIN_IDS[:] = merged
    return merged


def add_admin_id(uid: int) -> list[int]:
    uid = int(uid)
    doc = settings_col.find_one({"_id": "admin_config"}) or {}
    extra = [int(x) for x in doc.get("extra_admins", []) if str(x).lstrip("-").isdigit()]
    if uid not in extra and uid not in ROOT_ADMIN_IDS:
        extra.append(uid)
    settings_col.update_one(
        {"_id": "admin_config"},
        {"$set": {"extra_admins": extra}},
        upsert=True,
    )
    return sync_admin_ids()


def remove_admin_id(uid: int) -> tuple[bool, str]:
    uid = int(uid)
    if uid in ROOT_ADMIN_IDS:
        return False, "Root/config admin cannot be removed from the panel."
    doc = settings_col.find_one({"_id": "admin_config"}) or {}
    extra = [int(x) for x in doc.get("extra_admins", []) if str(x).lstrip("-").isdigit()]
    if uid not in extra:
        return False, "User is not in the extra admins list."
    extra = [x for x in extra if x != uid]
    settings_col.update_one(
        {"_id": "admin_config"},
        {"$set": {"extra_admins": extra}},
        upsert=True,
    )
    sync_admin_ids()
    return True, "Admin removed successfully."


sync_admin_ids()


class BanCheckMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        user = data.get("event_from_user")
        if user:
            sync_admin_ids()
            if user.id not in ADMIN_IDS:
                user_doc = users_col.find_one({"_id": user.id})
                if user_doc and user_doc.get("banned", False):
                    if isinstance(event, Message):
                        await event.answer(
                            "<h2>🚫 Account Suspended</h2>"
                            "<blockquote>You are banned from using this bot. Contact support if you believe this is a mistake.</blockquote>",
                            parse_mode="HTML",
                        )
                    elif isinstance(event, CallbackQuery):
                        await event.answer("🚫 You are banned from using this bot.", show_alert=True)
                    return

                feat = settings_col.find_one({"_id": "bot_features"}) or {}
                if feat.get("maintenance_enabled", False):
                    if isinstance(event, Message):
                        kb = InlineKeyboardMarkup(inline_keyboard=[
                            [premium_button(text="📢 Official Updates", url=f"https://t.me/{UPDATES}")],
                            [premium_button(text="👨‍💻 Contact Support", url=f"https://t.me/{SUPPORT}")],
                        ])
                        await event.answer(
                            f"<h2>🛠️ {tg_icon()} Scheduled Maintenance Mode</h2>"
                            "<blockquote>🚧 <b>System Upgrade in Progress</b><br>"
                            "TGA Store | API is currently undergoing scheduled maintenance and upgrades to serve you better.<br><br>"
                            "⏳ <b>Status:</b> <code>MAINTENANCE ACTIVE</code><br>"
                            "💎 Your wallet balance and active orders remain <b>100% safe</b>.</blockquote>"
                            "<p>📢 <i>Please check our official updates channel or try again shortly!</i></p>",
                            parse_mode="HTML",
                            reply_markup=kb,
                        )
                    elif isinstance(event, CallbackQuery):
                        await event.answer(
                            "🛠️ Bot is currently under maintenance. Please try again shortly!",
                            show_alert=True,
                        )
                    return
        return await handler(event, data)
        
# ================= Bot Setup =================
bot = RichBot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
bot.set_currency_resolver(
    lambda chat_id: (users_col.find_one({"_id": chat_id}) or {}).get("currency", "INR")
    if isinstance(chat_id, int) else "INR"
)
dp = Dispatcher()
dp.update.middleware(BanCheckMiddleware())
dp.callback_query.outer_middleware(FeatureGateMiddleware())


# ================= FSM =================
# ================= SERVER 3 (TEMPORASMS) SYSTEM
class Server3Edit(StatesGroup):
    waiting_new_price = State()
    waiting_new_cap = State()
    
class Server3Admin(StatesGroup):
    waiting_category = State()
    waiting_service_code = State()
    waiting_country_code = State()
    waiting_country_name = State()
    waiting_country_flag = State()      # ← NEW, add this
    waiting_operator = State()
    waiting_price_inr = State()
    waiting_max_price = State()
    
class Server3User(StatesGroup):
    waiting_query = State()

class AddSession(StatesGroup):
    waiting_country = State()
    waiting_number = State()
    waiting_otp = State()
    waiting_password = State()
    waiting_next_action = State()   # ✅ REQUIRED

class SellSession(StatesGroup):
    # ... existing states ...
    waiting_sell_number = State()

class WithdrawState(StatesGroup):
    waiting_upi = State()
    waiting_amount = State()

class AdminTxnState(StatesGroup):
    waiting_txn = State()
    
class ServerManage(StatesGroup):
    waiting_profit_margin = State()


class EditSellRates(StatesGroup):
    waiting_rates = State()

# ================ Helpers =================
def get_or_create_user(user_id: int, username: str | None):
    user = users_col.find_one({"_id": user_id})
    if not user:
        user = {"_id": user_id, "username": username or None, "balance": 0.0}
        users_col.insert_one(user)
    return user

def is_admin(user_id: int) -> bool:
    sync_admin_ids()
    return user_id in ADMIN_IDS

def get_user_balance(user_id):
    user = users_col.find_one({"_id": user_id})
    return user.get("balance", 0) if user else 0

def get_user_discount(user_or_id) -> float:
    if isinstance(user_or_id, dict):
        u = user_or_id
    elif user_or_id:
        u = users_col.find_one({"_id": int(user_or_id)}) or {}
    else:
        u = {}
    try:
        pct = float(u.get("discount_percent", 0.0) or 0.0)
    except (TypeError, ValueError):
        pct = 0.0
    return max(0.0, min(100.0, pct))

def apply_user_discount(amount_inr: float, user_or_id=None) -> float:
    pct = get_user_discount(user_or_id)
    if pct <= 0:
        return round(float(amount_inr), 2)
    return round(float(amount_inr) * (1.0 - pct / 100.0), 2)
def generate_strong_password(length=14):
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*_-+="
    while True:
        pwd = ''.join(secrets.choice(alphabet) for _ in range(length))
        if (any(c.islower() for c in pwd) and any(c.isupper() for c in pwd) and any(c.isdigit() for c in pwd) and any(c in "!@#$%^&*_-+=" for c in pwd)):
            return pwd

def hash_pwd(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()

def verify_pwd(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode(), hashed.encode())
    except:
        return plain == hashed
# ================ Automatic OTP Listener =================
# ================ Automatic OTP Listener =================
async def otp_listener(number_doc, user_id, message_id):
    string_session = number_doc.get("string_session")
    if not string_session:
        return

    api_id = int(os.getenv("API_ID"))
    api_hash = os.getenv("API_HASH")

    client = TelegramClient(StringSession(string_session), api_id, api_hash)
    await client.connect()

    try:
        if not await client.is_user_authorized():
            return

        pattern = re.compile(r"\b\d{5}\b")  # OTP pattern

        async for msg in client.iter_messages(777000, limit=10):
            if not msg.message:
                continue

            match = pattern.search(msg.message)
            if not match:
                continue

            # ===== OTP FOUND =====
            code = match.group(0)
            password_text = number_doc.get("password") or "None"

            

            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="Copy OTP",
                            copy_text=CopyTextButton(text=code)
                        ),
                        InlineKeyboardButton(
                            text="Copy Pass",
                            copy_text=CopyTextButton(text=password_text)
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="• Get Code Again •",
                            callback_data=f"get_otp:{number_doc['number']}"
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="Remove Bot Session",
                            callback_data=f"logout_bot:{number_doc['number']}"
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="📱 Manage Devices",
                            callback_data=f"manage_devices:{number_doc['number']}"
                        )
                    ],
                    
                    
                    
                ]
            )

            await bot.edit_message_text(
                chat_id=user_id,
                message_id=message_id,
                text=(
                    "<h2>✅ Order Completed — OTP Received</h2>"
                    f"<blockquote>📞 <b>Number:</b> <code>+{number_doc['number']}</code><br>"
                    f"💬 <b>Login Code:</b> <code>{code}</code><br>"
                    f"🔐 <b>2FA Password:</b> <code>{password_text}</code></blockquote>"
                    "<p>✨ <i>Use the buttons below to copy credentials or manage sessions:</i></p>"
                ),
                parse_mode="HTML",
                reply_markup=kb
            )

            # ===== USER & LOGGING =====
            user = users_col.find_one({"_id": user_id}) or {}
            buyer_name = user.get("username") or f"User {user_id}"
            balance = user.get("balance", "N/A")

            country = number_doc.get("country", "Unknown")
            price = number_doc.get("price", "N/A")
            number = str(number_doc.get("number", "Unknown"))

            if number != "Unknown":
                if not number.startswith("+"):
                    number = f"+{number}"
                masked_number = number[:6] + "•••••"
            else:
                masked_number = "Hidden"

            channel_message = (
                f"<pre><u>✅ <b>New Number Purchase Successful</b></u></pre>\n\n"
                f"➖ <b><u>Country:</u></b> {country}\n"
                f"➖ <b><u>Application:</u> Теlegгам 🍷</b>\n\n"
                f"➕ <b>Number: {masked_number} 📞</b>\n"
                f"➕ <b>OTP:</b> <span class='tg-spoiler'>{code}</span> 💬\n"
                f"➕ <b>Server:</b> (1) 🥂\n"
                f"➕ <b>Password:</b> <span class='tg-spoiler'>{password_text}</span> 🔐\n\n"
                f"<b>• @{BOTUSER} || @{CHANNEL}</b>"
            )

            buy_button = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="• Buy Now •",
                            url=f"https://t.me/{BOTUSER}?start=starting"
                        )
                    ]
                ]
            )

            await bot.send_message(
                SALES,
                channel_message,
                parse_mode="HTML",
                reply_markup=buy_button
            )

            admin_message = (
                f"<pre>📢 New Purchase Alert</pre>\n\n"
                f"<b>• Application:</b> Telegram\n"
                f"<b>• Country:</b> {country}\n"
                f"<b>• Number:</b> {number}\n"
                f"<b>• OTP:</b> <code>{code}</code>\n"
                f"➖ <b>Password:</b> <span class='tg-spoiler'>{password_text}</span> 🔐\n\n"
                f"<b>👤 User:</b> @{buyer_name} (<code>{user_id}</code>)\n"
                f"<b>💰 Balance:</b> ₹{balance}"
            )
            userbutton = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="USER ID",
                            url=f"tg://openmessage?user_id={user_id}"
                        )
                    ]
                ]
            )

            await bot.send_message(
                ADMINLOG,
                admin_message,
                parse_mode="HTML",
                reply_markup=userbutton
            )

            # ===== SAVE OTP =====
            numbers_col.update_one(
                {"_id": number_doc["_id"]},
                {
                    "$set": {
                        "last_otp": code,
                        "otp_fetched_at": datetime.now(timezone.utc)
                    }
                }
            )

            break  # stop after OTP

        await asyncio.sleep(1)
    except Exception as e:
        print(f"OTP listener failed for {user_id}: {e}")
        try:
            await bot.send_message(user_id, "💬 No new code received. Please request a new code and try again.")
        except Exception:
            pass

    finally:
        await client.disconnect()
        

  

@dp.callback_query(F.data.startswith("manage_devices:"))
async def manage_devices(call: CallbackQuery):
    number = call.data.split(":", 1)[1]
    doc = numbers_col.find_one({"number": number})

    if not doc or not doc.get("string_session"):
        return await call.answer("❌ No active session", show_alert=True)

    api_id = int(os.getenv("API_ID"))
    api_hash = os.getenv("API_HASH")

    client = TelegramClient(
        StringSession(doc["string_session"]),
        api_id,
        api_hash
    )
    await client.connect()

    try:
        sessions = await client(GetAuthorizationsRequest())
    except Exception:
        await client.disconnect()
        return await call.answer("❌ Failed to fetch sessions", show_alert=True)

    kb = InlineKeyboardMarkup(inline_keyboard=[])

    for s in sessions.authorizations:
        if s.current:
            continue  # cannot remove current via hash

        kb.inline_keyboard.append([
            InlineKeyboardButton(
                text=f"{s.device_model} | {s.platform}",
                callback_data=f"kill_session:{number}:{s.hash}"
            )
        ])

    await client.disconnect()

    if not kb.inline_keyboard:
        return await call.message.answer("✅ No removable sessions")

    await call.message.answer(
        "📱 Click any session to remove:",
        reply_markup=kb
    )


#-----Temrinate sessuon
@dp.callback_query(F.data.startswith("kill_session:"))
async def kill_session(call: CallbackQuery):
    _, number, session_hash = call.data.split(":")
    doc = numbers_col.find_one({"number": number})

    api_id = int(os.getenv("API_ID"))
    api_hash = os.getenv("API_HASH")

    client = TelegramClient(
        StringSession(doc["string_session"]),
        api_id,
        api_hash
    )
    await client.connect()

    try:
        await client(ResetAuthorizationRequest(hash=int(session_hash)))
        await call.answer("✅ Session removed", show_alert=True)
    except Exception:
        await call.answer("❌ Cannot remove session", show_alert=True)
    finally:
        await client.disconnect()

    #&------Logout bot

@dp.callback_query(F.data.startswith("logout_bot:"))
async def logout_bot(call: CallbackQuery):
    number = call.data.split(":", 1)[1]
    doc = numbers_col.find_one({"number": number})

    if not doc or not doc.get("string_session"):
        return await call.answer("❌ No active session", show_alert=True)

    api_id = int(os.getenv("API_ID"))
    api_hash = os.getenv("API_HASH")

    client = TelegramClient(StringSession(doc["string_session"]), api_id, api_hash)
    await client.connect()

    try:
        await client.log_out()
    finally:
        await client.disconnect()

    # 🔥 VERY IMPORTANT
    numbers_col.update_one(
        {"number": number},
        {
            "$set": {
                "active": False,   # session dead
                "used": True
            },
            "$unset": {
                "string_session": ""
            }
        }
    )

    await call.message.answer(
        "✅ Bot session has been logged out\nOTP polling closed for this number"
    )

# ================ START =================
def get_feature_settings() -> dict:
    """Return global user-menu switches and referral/maintenance settings, backfilling newly added options."""
    srv = settings_col.find_one({"_id": "server_config"}) or {}
    default_store = srv.get("store_status", "on") != "off"
    defaults = {
        "smm_enabled": True,
        "whatsapp_enabled": True,
        "store_enabled": default_store,
        "maintenance_enabled": False,
        "refer_bonus": 0.1,
        "refer_commission_percent": 2.0,
    }
    doc = settings_col.find_one({"_id": "bot_features"})
    if not doc:
        settings_col.insert_one({"_id": "bot_features", **defaults})
        return defaults
    missing = {key: value for key, value in defaults.items() if key not in doc}
    if missing:
        settings_col.update_one({"_id": "bot_features"}, {"$set": missing})
        doc.update(missing)
    return doc


async def reward_referral_if_eligible(user_id: int, display_name: str):
    """Reward the referrer once the invited user has verified channel membership."""
    user = users_col.find_one({"_id": user_id})
    if not user or not user.get("referred_by") or user.get("referral_rewarded", False):
        return
    res = users_col.update_one(
        {"_id": user_id, "referral_rewarded": {"$ne": True}},
        {"$set": {"referral_rewarded": True}},
    )
    if getattr(res, "modified_count", 1) == 0:
        return

    referrer_id = user["referred_by"]
    try:
        refer_bonus = float(get_feature_settings().get("refer_bonus", 0.1) or 0.0)
    except (TypeError, ValueError):
        refer_bonus = 0.1
    if refer_bonus <= 0:
        return

    users_col.update_one(
        {"_id": referrer_id},
        {"$inc": {"balance": refer_bonus, "referral_earnings": refer_bonus}},
    )
    try:
        ref_user = users_col.find_one({"_id": referrer_id}) or {}
        new_ref_balance = float(ref_user.get("balance", refer_bonus) or 0.0)
        await bot.send_message(
            chat_id=referrer_id,
            text=(
                "<h2>🎁 New Referral Confirmed!</h2>"
                f"<blockquote>👤 <b>Friend:</b> {escape(str(display_name))}<br>"
                f"💰 <b>Join Bonus Credited:</b> <code>+₹{refer_bonus:.2f}</code><br>"
                f"🏦 <b>Updated Balance:</b> <code>₹{new_ref_balance:.2f}</code></blockquote>"
                "<p>✨ <i>Plus you earn Referral Commission on every deposit your referral makes!</i></p>"
            ),
            parse_mode="HTML",
        )
    except Exception as e:
        print("Referral notify error:", e)


def language_keyboard(back: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"{flag} {name}", callback_data=f"set_lang:{code}")]
        for code, (flag, name) in LANGUAGES.items()
    ]
    if back:
        rows.append([InlineKeyboardButton(text="⬅️ Back", callback_data="settings")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def currency_keyboard(user: dict | None = None, back: bool = False) -> InlineKeyboardMarkup:
    rows = [[
        InlineKeyboardButton(text=f"₹ INR", callback_data="set_currency:INR"),
        InlineKeyboardButton(text=f"$ USD", callback_data="set_currency:USD"),
    ]]
    if back:
        rows.append([InlineKeyboardButton(text=f"⬅️ {tr(user, 'back')}", callback_data="settings")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_main_menu(user_id: int, full_name: str) -> tuple[str, InlineKeyboardMarkup]:
    user = users_col.find_one({"_id": user_id}) or {}
    features = get_feature_settings()
    text = tr(user, "welcome").format(
        name=f"<a href='tg://user?id={user_id}'>{escape(full_name)}</a>",
        user_id=user_id,
        balance=format_balance(user),
    )
    rows = [[
        premium_button(
            text=f"✈️ {tr(user, 'buy_tg')}",
            callback_data="buy",
            icon_custom_emoji_id=TELEGRAM_EMOJI_ID,
        )
    ]]
    if features.get("whatsapp_enabled", True):
        rows.append([
            premium_button(
                text=f"💬 {tr(user, 'buy_wa')}",
                callback_data="buy_wa",
                icon_custom_emoji_id=WHATSAPP_EMOJI_ID,
            )
        ])
    rows.append([
        premium_button(text=f"💳 {tr(user, 'recharge')}", callback_data="recharge"),
        premium_button(text=f"👤 {tr(user, 'account')}", callback_data="stats"),
    ])
    rows.append([premium_button(text="📲 Buy OTPs (All Apps)", callback_data="s3_user_root")])
    if features.get("smm_enabled", True):
        rows.append([premium_button(text=f"🚀 {tr(user, 'smm')}", callback_data="smm_open")])

    api_btn = premium_button(
        text=f"📚 {tr(user, 'api_docs')}",
        callback_data="api_docs",
        icon_custom_emoji_id=API_DOCS_EMOJI_ID,
    )
    if features.get("store_enabled", True):
        rows.append([
            premium_button(text=f"🏪 {tr(user, 'store')}", callback_data="store_menu"),
            api_btn,
        ])
    else:
        rows.append([api_btn])

    rows.extend([
        [premium_button(text=f"➕ {tr(user, 'more')}", callback_data="more_menu")],
        [premium_button(text=f"👨‍💻 {tr(user, 'support')}", url=f"https://t.me/{SUPPORT}")],
    ])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def show_main(target: Message, user_id: int, full_name: str, edit: bool = False):
    text, markup = build_main_menu(user_id, full_name)
    if edit:
        await safe_edit_or_send(bot, target, text, reply_markup=markup)
    else:
        await target.answer(text, parse_mode="HTML", reply_markup=markup)


@dp.message(Command("start"))
async def cmd_start(m: Message):
    args = m.text.split()
    payload = args[1] if len(args) > 1 else ""

    # ===== WEBSITE AUTH: register / login / connect =====
    if payload in ("register", "login"):
        user_doc = users_col.find_one({"_id": m.from_user.id})
        if user_doc and user_doc.get("password_hash"):
            kb_already = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📋 Copy User ID", copy_text=CopyTextButton(text=str(m.from_user.id)))],
                [InlineKeyboardButton(text="🌐 Open Website", url="https://tga-store.vercel.app")],
            ])
            await m.answer(
                "<h2>✅ Website Account Already Registered</h2>"
                f"<blockquote>🆔 <b>User ID:</b> <code>{m.from_user.id}</code><br>"
                "🔐 <b>Status:</b> Linked &amp; Active</blockquote>"
                "<p>🌐 <i>Log in on the website using your User ID and password.</i></p>",
                parse_mode="HTML",
                reply_markup=kb_already,
            )
            return
        
        new_pwd = generate_strong_password(14)
        hashed = hash_pwd(new_pwd)
        if user_doc:
            users_col.update_one({"_id": m.from_user.id}, {"$set": {"password_hash": hashed, "telegram_id": m.from_user.id, "bot_connected": True, "username": m.from_user.username or user_doc.get("username")}, "$unset": {"password": ""}})
        else:
            users_col.insert_one({"_id": m.from_user.id, "telegram_id": m.from_user.id, "username": m.from_user.username, "balance": 0.0, "password_hash": hashed, "bot_connected": True, "joined_at": datetime.now(timezone.utc), "referral_rewarded": False})
        
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Copy User ID", copy_text=CopyTextButton(text=str(m.from_user.id)))],
            [InlineKeyboardButton(text="Copy Password", copy_text=CopyTextButton(text=new_pwd))],
            [InlineKeyboardButton(text="Open Website", url="https://tga-store.vercel.app")]
        ])
        await m.answer(
            "<h2>✅ Website Account Created Successfully!</h2>"
            f"<blockquote>🆔 <b>User ID:</b> <code>{m.from_user.id}</code><br>"
            f"🔐 <b>Password:</b> <span class=\"tg-spoiler\"><code>{new_pwd}</code></span></blockquote>"
            "<p>👆 <i>Tap to reveal. Log in on the website with your User ID and Password.</i></p>",
            parse_mode="HTML",
            reply_markup=kb
        )
        try:
            # Never log a user's password — only log that a registration happened.
            await bot.send_message(ADMINLOG, f"<h2>✅ New Website Registration</h2><blockquote>👤 <b>User:</b> @{m.from_user.username or 'N/A'} (<code>{m.from_user.id}</code>)</blockquote>", parse_mode="HTML")
        except:
            pass
        return

    if payload.startswith("connect_"):
        token = payload.split("connect_", 1)[1]
        cdoc = db["connect_tokens"].find_one({"_id": token})
        if not cdoc:
            await m.answer("Invalid or expired link. Generate a new one from Website > Settings > Bot Connect.", parse_mode="HTML")
            return
        created = cdoc.get("created_at")
        if created and (datetime.now(timezone.utc) - created.replace(tzinfo=timezone.utc)).total_seconds() > 900:
            db["connect_tokens"].delete_one({"_id": token})
            await m.answer("Link expired (15 minutes). Generate a new one.", parse_mode="HTML")
            return
        user_id_str = str(cdoc.get("userId"))
        web_user = None
        if len(user_id_str) == 24:
            try:
                web_user = users_col.find_one({"_id": ObjectId(user_id_str)})
            except:
                pass
        if not web_user:
            try:
                web_user = users_col.find_one({"_id": int(user_id_str)})
            except:
                pass
        if not web_user:
            await m.answer("Website user not found.", parse_mode="HTML")
            return
        if web_user.get("telegram_id") and web_user.get("telegram_id") != m.from_user.id:
            await m.answer("This website account is already linked to another Telegram account.", parse_mode="HTML")
            return
        users_col.update_one({"_id": web_user["_id"]}, {"$set": {"telegram_id": m.from_user.id, "bot_connected": True, "username": m.from_user.username or web_user.get("username")}})
        db["connect_tokens"].delete_one({"_id": token})
        await m.answer(
            f"Connected!\n\nWebsite account <b>{web_user.get('username') or web_user.get('email') or web_user.get('phone')}</b> is now linked to Telegram <code>{m.from_user.id}</code>.\nPurchase alerts will be sent here.",
            parse_mode="HTML"
        )
        try:
            await bot.send_message(ADMINLOG, f"Bot Connected\nWeb <code>{user_id_str}</code> -> @{m.from_user.username} (<code>{m.from_user.id}</code>)", parse_mode="HTML")
        except:
            pass
        return
    # ===== END WEBSITE AUTH =====

    referred_by = None
    is_ref_link = False

    if len(args) > 1 and args[1].startswith("ref"):
        is_ref_link = True
        try:
            referred_by = int(args[1][3:])
        except:
            referred_by = None
    user = users_col.find_one({"_id": m.from_user.id})

    if not user:
        user_data = {
            "_id": m.from_user.id,
            "username": m.from_user.username or None,
            "balance": 0.0,
            "joined_at": datetime.now(timezone.utc),
            "referral_rewarded": False,
        }
        if referred_by and referred_by != m.from_user.id:
            user_data["referred_by"] = referred_by
        users_col.insert_one(user_data)
        try:
            await bot.send_message(
                ADMINLOG,
                f"New User Started The Bot\n\n"
                f"User: <code>{m.from_user.id}</code> (@{m.from_user.username or 'NoUsername'})\n"
                f"Referred By: {f'<code>{referred_by}</code>' if referred_by and referred_by != m.from_user.id else 'None'}",
                parse_mode="HTML"
            )
        except Exception as e:
            print("Admin new-user notify error:", e)

    # First-time onboarding: language, then currency. Existing users without
    # preferences are migrated through the same one-time screen.
    user = users_col.find_one({"_id": m.from_user.id}) or {}
    if not user.get("language"):
        await m.answer(
            "<h2>Welcome to TGA Store | API</h2><p>Please choose your language.</p>",
            reply_markup=language_keyboard(),
        )
        return
    if not user.get("currency"):
        await m.answer(tr(user, "choose_currency"), reply_markup=currency_keyboard(user))
        return

    if not await check_join(bot, m):
        return

    # Deep-link services are opened only after onboarding and membership checks.
    if payload in ("s3tg", "s3wa"):
        category = "tg" if payload == "s3tg" else "wa"
        if category == "wa" and not get_feature_settings().get("whatsapp_enabled", True):
            await m.answer("<h2>Service unavailable</h2><p>WhatsApp services are currently disabled.</p>")
            return
        await s3_send_countries_message(m.chat.id, category)
        return
    if payload == "smm":
        if not get_feature_settings().get("smm_enabled", True):
            await m.answer("<h2>Service unavailable</h2><p>Social Media Services are currently disabled.</p>")
            return
        await smm_send_menu(m.chat.id)
        return
    if payload and payload.startswith("buy_s3_"):
        rest = payload[len("buy_s3_"):]
        if "_" in rest:
            sid, cid = rest.rsplit("_", 1)
            await send_s3_operator_menu_direct(m, sid, cid)
            return
    if payload and payload.startswith("buy_s4_"):
        rest = payload[len("buy_s4_"):]
        if "_" in rest:
            sid, cid = rest.rsplit("_", 1)
            await send_s4_operator_menu_direct(m, sid, cid)
            return

    # Reward the referrer only now that the new user has confirmed channel membership
    await reward_referral_if_eligible(
        m.from_user.id,
        f"@{m.from_user.username}" if m.from_user.username else m.from_user.full_name,
    )

    # Ensure user exists in DB
    get_or_create_user(m.from_user.id, m.from_user.username)
    user_id = m.from_user.id
    full_name = m.from_user.full_name  # always use the name
    safe_name = escape(full_name)
    user_mention = f"<a href='tg://user?id={user_id}'>{safe_name}</a>"
    user = users_col.find_one({"_id": user_id})
    balance = f"₹{user['balance']:.2f} " if user else "₹0 "
    
# ================= Main Start Menu =================
    await show_main(m, m.from_user.id, m.from_user.full_name)




# ================= More.. Menu =================
@dp.callback_query(lambda cq: cq.data == "more_menu")
async def more_menu(cq: CallbackQuery):
    user = users_col.find_one({"_id": cq.from_user.id}) or {}
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=f"💸 {tr(user, 'balance')}", callback_data="balance"),
            InlineKeyboardButton(text=f"💸 {tr(user, 'transfer')}", callback_data="transfer_balance"),
        ],
        [InlineKeyboardButton(text=f"🎉 {tr(user, 'redeem')}", callback_data="redeem")],
        [
            InlineKeyboardButton(text=f"📑 {tr(user, 'history')}", callback_data="history"),
            InlineKeyboardButton(text=f"📈 {tr(user, 'sales_log')}", url=f"https://t.me/{SALESLOG}"),
        ],
        [
            InlineKeyboardButton(text=f"👤 {tr(user, 'about')}", callback_data="stats"),
            InlineKeyboardButton(text=f"🎁 {tr(user, 'refer')}", callback_data="refer"),
        ],
        [
            InlineKeyboardButton(text=f"🛒 {tr(user, 'how_buy')}", url=f"https://t.me/{UPDATES}"),
            InlineKeyboardButton(text=f"💵 {tr(user, 'how_sell')}", url=f"https://t.me/{UPDATES}"),
        ],
        [InlineKeyboardButton(text=f"💳 {tr(user, 'how_recharge')}", url=f"https://t.me/{UPDATES}")],
        [InlineKeyboardButton(text=f"⚙️ {tr(user, 'settings')}", callback_data="settings")],
        [InlineKeyboardButton(text=f"⬅️ {tr(user, 'back')}", callback_data="back_main")],
    ])
    await cq.message.edit_text(tr(user, "more_title"), reply_markup=kb)
    await cq.answer()

#=============== Back Button =================
@dp.callback_query(lambda cq: cq.data == "back_main")
async def back_main(cq: CallbackQuery):
    if not await check_join(bot, cq):
        await cq.answer("❗ Join the channel first", show_alert=True)
        return
    await reward_referral_if_eligible(
        cq.from_user.id,
        f"@{cq.from_user.username}" if cq.from_user.username else cq.from_user.full_name,
    )
    user = users_col.find_one({"_id": cq.from_user.id}) or {}
    if not user.get("language"):
        await safe_edit_or_send(bot, cq.message, "<h2>Choose your language</h2>", reply_markup=language_keyboard())
        await cq.answer()
        return
    if not user.get("currency"):
        await safe_edit_or_send(bot, cq.message, tr(user, "choose_currency"), reply_markup=currency_keyboard(user))
        await cq.answer()
        return
    await show_main(cq.message, cq.from_user.id, cq.from_user.full_name, edit=True)
    await cq.answer()

@dp.callback_query(F.data.startswith("set_lang:"))
async def set_language(cq: CallbackQuery):
    language = cq.data.split(":", 1)[1]
    if language not in LANGUAGES:
        return await cq.answer("Invalid language", show_alert=True)
    users_col.update_one(
        {"_id": cq.from_user.id},
        {"$set": {"language": language, "username": cq.from_user.username}},
        upsert=True,
    )
    user = users_col.find_one({"_id": cq.from_user.id}) or {"language": language}
    if not user.get("currency"):
        await cq.message.edit_text(tr(user, "choose_currency"), reply_markup=currency_keyboard(user))
    else:
        await show_settings(cq.message, cq.from_user.id)
    await cq.answer(tr(user, "saved"))


@dp.callback_query(F.data.startswith("set_currency:"))
async def set_currency(cq: CallbackQuery):
    currency = cq.data.split(":", 1)[1]
    if currency not in CURRENCIES:
        return await cq.answer("Invalid currency", show_alert=True)
    users_col.update_one({"_id": cq.from_user.id}, {"$set": {"currency": currency}}, upsert=True)
    user = users_col.find_one({"_id": cq.from_user.id}) or {}
    await show_main(cq.message, cq.from_user.id, cq.from_user.full_name, edit=True)
    await cq.answer(tr(user, "saved"))


async def show_settings(message: Message, user_id: int):
    user = users_col.find_one({"_id": user_id}) or {}
    language = language_name(user.get("language", "en"))
    currency = CURRENCIES.get(user.get("currency", "INR"), CURRENCIES["INR"])[1]
    text = tr(user, "settings_title").format(language=language, currency=currency)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🌐 {tr(user, 'language')}", callback_data="settings_language")],
        [InlineKeyboardButton(text=f"💱 {tr(user, 'currency')}", callback_data="settings_currency")],
        [InlineKeyboardButton(text=f"⬅️ {tr(user, 'back')}", callback_data="more_menu")],
    ])
    await message.edit_text(text, reply_markup=kb)


@dp.callback_query(F.data == "settings")
async def settings_menu(cq: CallbackQuery):
    await show_settings(cq.message, cq.from_user.id)
    await cq.answer()


@dp.callback_query(F.data == "settings_language")
async def settings_language(cq: CallbackQuery):
    user = users_col.find_one({"_id": cq.from_user.id}) or {}
    await cq.message.edit_text(tr(user, "choose_language"), reply_markup=language_keyboard(back=True))
    await cq.answer()


@dp.callback_query(F.data == "settings_currency")
async def settings_currency(cq: CallbackQuery):
    user = users_col.find_one({"_id": cq.from_user.id}) or {}
    await cq.message.edit_text(tr(user, "choose_currency"), reply_markup=currency_keyboard(user, back=True))
    await cq.answer()


@dp.callback_query(F.data == "api_docs")
async def api_docs(cq: CallbackQuery):
    user = users_col.find_one({"_id": cq.from_user.id}) or {}
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"⬅️ {tr(user, 'back')}", callback_data="back_main")]
    ])
    await cq.message.edit_text(tr(user, "coming_soon"), reply_markup=kb)
    await cq.answer()


#================ Balance =================
@dp.callback_query(F.data == "balance")
async def show_balance(cq: CallbackQuery):
    user = users_col.find_one({"_id": cq.from_user.id}) or {}
    await cq.answer(f"💰 {tr(user, 'balance')}: {format_balance(user)}", show_alert=True)

@dp.message(Command("balance"))
async def cmd_balance(msg: Message):
    user = users_col.find_one({"_id": msg.from_user.id}) or {}
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="💳 Recharge Wallet", callback_data="recharge"),
            InlineKeyboardButton(text="💸 Transfer Balance", callback_data="transfer_balance"),
        ],
        [InlineKeyboardButton(text="🏠 Main Menu", callback_data="back_main")],
    ])
    await msg.answer(
        f"<h2>💰 {tr(user, 'balance')}</h2>"
        f"<blockquote>👤 <b>User ID:</b> <code>{msg.from_user.id}</code><br>"
        f"💳 <b>Available Balance:</b> <code>{format_balance(user)}</code></blockquote>"
        "<p>✨ <i>Use the buttons below to top up or transfer funds:</i></p>",
        reply_markup=kb,
    )
# ================= API Configuration (Server 2) =================
# TGLION_API_KEY / TGLION_ID / TGPVA_API_KEY are imported from config.py and
# must only be set through environment variables — never hard-code API keys.

# --- MASTER COUNTRY MAPPER (250+ COUNTRIES & TERRITORIES) ---
ISO_COUNTRY_MAP = {
    "AF": "Afghanistan", "AX": "Åland Islands", "AL": "Albania", "DZ": "Algeria",
    "AS": "American Samoa", "AD": "Andorra", "AO": "Angola", "AI": "Anguilla",
    "AQ": "Antarctica", "AG": "Antigua & Barbuda", "AR": "Argentina", "AM": "Armenia",
    "AW": "Aruba", "AU": "Australia", "AT": "Austria", "AZ": "Azerbaijan",
    "BS": "Bahamas", "BH": "Bahrain", "BD": "Bangladesh", "BB": "Barbados",
    "BY": "Belarus", "BE": "Belgium", "BZ": "Belize", "BJ": "Benin",
    "BM": "Bermuda", "BT": "Bhutan", "BO": "Bolivia", "BQ": "Caribbean Netherlands",
    "BA": "Bosnia & Herzegovina", "BW": "Botswana", "BV": "Bouvet Island", "BR": "Brazil",
    "IO": "British Indian Ocean Territory", "VG": "British Virgin Islands", "BN": "Brunei",
    "BG": "Bulgaria", "BF": "Burkina Faso", "BI": "Burundi", "CV": "Cabo Verde",
    "KH": "Cambodia", "CM": "Cameroon", "CA": "Canada", "KY": "Cayman Islands",
    "CF": "Central African Republic", "TD": "Chad", "CL": "Chile", "CN": "China",
    "CX": "Christmas Island", "CC": "Cocos (Keeling) Islands", "CO": "Colombia",
    "KM": "Comoros", "CD": "Congo (DRC)", "CG": "Congo (Republic)", "CK": "Cook Islands",
    "CR": "Costa Rica", "HR": "Croatia", "CU": "Cuba", "CW": "Curaçao",
    "CY": "Cyprus", "CZ": "Czechia", "CI": "Côte d'Ivoire", "DK": "Denmark",
    "DJ": "Djibouti", "DM": "Dominica", "DO": "Dominican Republic", "EC": "Ecuador",
    "EG": "Egypt", "SV": "El Salvador", "GQ": "Equatorial Guinea", "ER": "Eritrea",
    "EE": "Estonia", "SZ": "Eswatini", "ET": "Ethiopia", "FK": "Falkland Islands",
    "FO": "Faroe Islands", "FJ": "Fiji", "FI": "Finland", "FR": "France",
    "GF": "French Guiana", "PF": "French Polynesia", "TF": "French Southern Territories",
    "GA": "Gabon", "GM": "Gambia", "GE": "Georgia", "DE": "Germany",
    "GH": "Ghana", "GI": "Gibraltar", "GR": "Greece", "GL": "Greenland",
    "GD": "Grenada", "GP": "Guadeloupe", "GU": "Guam", "GT": "Guatemala",
    "GG": "Guernsey", "GN": "Guinea", "GW": "Guinea-Bissau", "GY": "Guyana",
    "HT": "Haiti", "HM": "Heard & McDonald Islands", "VA": "Vatican City",
    "HN": "Honduras", "HK": "Hong Kong", "HU": "Hungary", "IS": "Iceland",
    "IN": "India", "ID": "Indonesia", "IR": "Iran", "IQ": "Iraq",
    "IE": "Ireland", "IM": "Isle of Man", "IL": "Israel", "IT": "Italy",
    "JM": "Jamaica", "JP": "Japan", "JE": "Jersey", "JO": "Jordan",
    "KZ": "Kazakhstan", "KE": "Kenya", "KI": "Kiribati", "KP": "North Korea",
    "KR": "South Korea", "KW": "Kuwait", "KG": "Kyrgyzstan", "LA": "Laos",
    "LV": "Latvia", "LB": "Lebanon", "LS": "Lesotho", "LR": "Liberia",
    "LY": "Libya", "LI": "Liechtenstein", "LT": "Lithuania", "LU": "Luxembourg",
    "MO": "Macau", "MG": "Madagascar", "MW": "Malawi", "MY": "Malaysia",
    "MV": "Maldives", "ML": "Mali", "MT": "Malta", "MH": "Marshall Islands",
    "MQ": "Martinique", "MR": "Mauritania", "MU": "Mauritius", "YT": "Mayotte",
    "MX": "Mexico", "FM": "Micronesia", "MD": "Moldova", "MC": "Monaco",
    "MN": "Mongolia", "ME": "Montenegro", "MS": "Montserrat", "MA": "Morocco",
    "MZ": "Mozambique", "MM": "Myanmar", "NA": "Namibia", "NR": "Nauru",
    "NP": "Nepal", "NL": "Netherlands", "NC": "New Caledonia", "NZ": "New Zealand",
    "NI": "Nicaragua", "NE": "Niger", "NG": "Nigeria", "NU": "Niue",
    "NF": "Norfolk Island", "MK": "North Macedonia", "MP": "Northern Mariana Islands",
    "NO": "Norway", "OM": "Oman", "PK": "Pakistan", "PW": "Palau",
    "PS": "Palestine", "PA": "Panama", "PG": "Papua New Guinea", "PY": "Paraguay",
    "PE": "Peru", "PH": "Philippines", "PN": "Pitcairn Islands", "PL": "Poland",
    "PT": "Portugal", "PR": "Puerto Rico", "QA": "Qatar", "RE": "Réunion",
    "RO": "Romania", "RU": "Russia", "RW": "Rwanda", "BL": "St. Barthélemy",
    "SH": "St. Helena", "KN": "St. Kitts & Nevis", "LC": "St. Lucia", "MF": "St. Martin",
    "PM": "St. Pierre & Miquelon", "VC": "St. Vincent & Grenadines", "WS": "Samoa",
    "SM": "San Marino", "ST": "Sao Tome & Principe", "SA": "Saudi Arabia",
    "SN": "Senegal", "RS": "Serbia", "SC": "Seychelles", "SL": "Sierra Leone",
    "SG": "Singapore", "SX": "Sint Maarten", "SK": "Slovakia", "SI": "Slovenia",
    "SB": "Solomon Islands", "SO": "Somalia", "ZA": "South Africa", "GS": "South Georgia",
    "SS": "South Sudan", "ES": "Spain", "LK": "Sri Lanka", "SD": "Sudan",
    "SR": "Suriname", "SJ": "Svalbard & Jan Mayen", "SE": "Sweden", "CH": "Switzerland",
    "SY": "Syria", "TW": "Taiwan", "TJ": "Tajikistan", "TZ": "Tanzania",
    "TH": "Thailand", "TL": "Timor-Leste", "TG": "Togo", "TK": "Tokelau",
    "TO": "Tonga", "TT": "Trinidad & Tobago", "TN": "Tunisia", "TR": "Türkiye",
    "TM": "Turkmenistan", "TC": "Turks & Caicos Islands", "TV": "Tuvalu",
    "UG": "Uganda", "UA": "Ukraine", "AE": "UAE", "GB": "UK",
    "US": "USA", "UM": "U.S. Outlying Islands", "UY": "Uruguay", "UZ": "Uzbekistan",
    "VU": "Vanuatu", "VE": "Venezuela", "VN": "Vietnam", "VI": "U.S. Virgin Islands",
    "WF": "Wallis & Futuna", "EH": "Western Sahara", "YE": "Yemen", "ZM": "Zambia",
    "ZW": "Zimbabwe"
}

# --- MASTER PREFIX MAPPER (250+ PHONE CODES FOR SEARCH) ---
ISO_TO_PREFIX = {
    "AF": "93", "AX": "358", "AL": "355", "DZ": "213", "AS": "1684", "AD": "376", "AO": "244", 
    "AI": "1264", "AQ": "672", "AG": "1268", "AR": "54", "AM": "374", "AW": "297", "AU": "61", 
    "AT": "43", "AZ": "994", "BS": "1242", "BH": "973", "BD": "880", "BB": "1246", "BY": "375", 
    "BE": "32", "BZ": "501", "BJ": "229", "BM": "1441", "BT": "975", "BO": "591", "BQ": "599", 
    "BA": "387", "BW": "267", "BV": "47", "BR": "55", "IO": "246", "VG": "1284", "BN": "673", 
    "BG": "359", "BF": "226", "BI": "257", "CV": "238", "KH": "855", "CM": "237", "CA": "1", 
    "KY": "1345", "CF": "236", "TD": "235", "CL": "56", "CN": "86", "CX": "61", "CC": "61", 
    "CO": "57", "KM": "269", "CD": "243", "CG": "242", "CK": "682", "CR": "506", "HR": "385", 
    "CU": "53", "CW": "599", "CY": "357", "CZ": "420", "CI": "225", "DK": "45", "DJ": "253", 
    "DM": "1767", "DO": "1809", "EC": "593", "EG": "20", "SV": "503", "GQ": "240", "ER": "291", 
    "EE": "372", "SZ": "268", "ET": "251", "FK": "500", "FO": "298", "FJ": "679", "FI": "358", 
    "FR": "33", "GF": "594", "PF": "689", "TF": "262", "GA": "241", "GM": "220", "GE": "995", 
    "DE": "49", "GH": "233", "GI": "350", "GR": "30", "GL": "299", "GD": "1473", "GP": "590", 
    "GU": "1671", "GT": "502", "GG": "44", "GN": "224", "GW": "245", "GY": "592", "HT": "509", 
    "HM": "672", "VA": "379", "HN": "504", "HK": "852", "HU": "36", "IS": "354", "IN": "91", 
    "ID": "62", "IR": "98", "IQ": "964", "IE": "353", "IM": "44", "IL": "972", "IT": "39", 
    "JM": "1876", "JP": "81", "JE": "44", "JO": "962", "KZ": "7", "KE": "254", "KI": "686", 
    "KP": "850", "KR": "82", "KW": "965", "KG": "996", "LA": "856", "LV": "371", "LB": "961", 
    "LS": "266", "LR": "231", "LY": "218", "LI": "423", "LT": "370", "LU": "352", "MO": "853", 
    "MG": "261", "MW": "265", "MY": "60", "MV": "960", "ML": "223", "MT": "356", "MH": "692", 
    "MQ": "596", "MR": "222", "MU": "230", "YT": "262", "MX": "52", "FM": "691", "MD": "373", 
    "MC": "377", "MN": "976", "ME": "382", "MS": "1664", "MA": "212", "MZ": "258", "MM": "95", 
    "NA": "264", "NR": "674", "NP": "977", "NL": "31", "NC": "687", "NZ": "64", "NI": "505", 
    "NE": "227", "NG": "234", "NU": "683", "NF": "672", "MK": "389", "MP": "1670", "NO": "47", 
    "OM": "968", "PK": "92", "PW": "680", "PS": "970", "PA": "507", "PG": "675", "PY": "595", 
    "PE": "51", "PH": "63", "PN": "870", "PL": "48", "PT": "351", "PR": "1787", "QA": "974", 
    "RE": "262", "RO": "40", "RU": "7", "RW": "250", "BL": "590", "SH": "290", "KN": "1869", 
    "LC": "1758", "MF": "590", "PM": "508", "VC": "1784", "WS": "685", "SM": "378", "ST": "239", 
    "SA": "966", "SN": "221", "RS": "381", "SC": "248", "SL": "232", "SG": "65", "SX": "1721", 
    "SK": "421", "SI": "386", "SB": "677", "SO": "252", "ZA": "27", "GS": "500", "SS": "211", 
    "ES": "34", "LK": "94", "SD": "249", "SR": "597", "SJ": "47", "SE": "46", "CH": "41", 
    "SY": "963", "TW": "886", "TJ": "992", "TZ": "255", "TH": "66", "TL": "670", "TG": "228", 
    "TK": "690", "TO": "676", "TT": "1868", "TN": "216", "TR": "90", "TM": "993", "TC": "1649", 
    "TV": "688", "UG": "256", "UA": "380", "AE": "971", "GB": "44", "US": "1", "UM": "1", 
    "UY": "598", "UZ": "998", "VU": "678", "VE": "58", "VN": "84", "VI": "1340", "WF": "681", 
    "EH": "212", "YE": "967", "ZM": "260", "ZW": "263"
}

def get_country_name(code):
    # No flag emoji: flags have no Premium IDs in the bundled packs.
    return ISO_COUNTRY_MAP.get(code.upper(), f"{code.upper()}")

async def fetch_server2_countries():
    """Fetches and sorts countries from the active API (TGPVA or TG-Lion)"""
    settings = settings_col.find_one({"_id": "server_config"}) or {}
    active_api = settings.get("s2_api", "tgpva")
    profit_margin = float(settings.get("s2_profit", 0.0))
    
    countries_list = []
    
    async with aiohttp.ClientSession() as session:
        if active_api == "tgpva":
            url = f"https://tgpva.com/api/user/getCountries?apiKey={TGPVA_API_KEY}"
            try:
                async with session.get(url, timeout=10) as resp:
                    data = await resp.json()
                    if data.get("ok") and "result" in data:
                        res = data["result"]
                        
                        # Safely find the service key ("1", "tg", "telegram", or first available dict)
                        countries_dict = res.get("countries", {})
                        avail_dict = res.get("availability", {})
                        
                        srv_key = "1" if "1" in countries_dict else ("tg" if "tg" in countries_dict else None)
                        
                        # If standard keys aren't found, try grabbing the first available service key
                        if not srv_key and isinstance(countries_dict, dict) and len(countries_dict) > 0:
                            srv_key = list(countries_dict.keys())[0]
                            
                        if srv_key and srv_key in countries_dict:
                            prices = countries_dict[srv_key]
                            qtys = avail_dict.get(srv_key, {})
                            
                            for code, base_price in prices.items():
                                try:
                                    # Safe type casting to handle string outputs from API
                                    qty = int(float(qtys.get(code, 0)))
                                    price_val = float(base_price)
                                    
                                    if qty > 0 and price_val > 0:
                                        final_usd = price_val + (price_val * profit_margin / 100.0)
                                        full_name = get_country_name(code)
                                        countries_list.append({"code": code, "name": full_name, "price": final_usd, "qty": qty})
                                except (ValueError, TypeError):
                                    continue
            except Exception as e:
                print(f"❌ TGPVA Fetch Error: {e}")
                                
        elif active_api == "tglion":
            url = f"https://TG-Lion.net?action=available_countries&apiKey={TGLION_API_KEY}&YourID={TGLION_ID}"
            try:
                async with session.get(url, timeout=10) as resp:
                    data = await resp.json()
                    if data.get("status") == "ok" and "countries" in data:
                        for c in data["countries"].values():
                            try:
                                base_price = float(c["price"])
                                qty = int(float(c.get("qty", 0)))
                                if qty > 0:
                                    final_usd = base_price + (base_price * profit_margin / 100.0)
                                    countries_list.append({"code": c["code"], "name": c["name"], "price": final_usd, "qty": qty})
                            except (ValueError, TypeError):
                                continue
            except Exception as e:
                print(f"❌ TG-Lion Fetch Error: {e}")

    countries_list.sort(key=lambda x: x["price"])
    return countries_list, active_api


# ================= Buy Flow (A to Z Routing) =================

@dp.callback_query(lambda c: c.data == "buy")
async def callback_buy_main(cq: CallbackQuery):
    await cq.answer()
    user = get_or_create_user(cq.from_user.id, cq.from_user.username)
    balance_inr = user.get('balance', 0.0)
    balance_usdt = balance_inr / USD_TO_INR
    
    settings = settings_col.find_one({"_id": "server_config"}) or {}
    s1 = settings.get("s1", "active")
    s2 = settings.get("s2", "active")
    s3 = settings.get("s3", "maintenance")
    s4 = settings.get("s4", "active")

    text = (
        f"<h2>{tg_icon()} Buy Telegram Accounts</h2>"
        "<blockquote>✨ <b>Premium Instant-Delivery Servers</b><br>"
        "• ⚡ One-click Telegram account purchase<br>"
        "• 📩 100% instant OTP &amp; 2FA code delivery<br>"
        "• 🛡️ Clean, spam-free accounts<br>"
        "• 🔄 Request multiple codes anytime</blockquote>"
        f"<blockquote>💰 <b>Wallet Balance:</b> <code>₹{balance_inr:.2f}</code> · <code>${balance_usdt:.2f}</code></blockquote>"
        "<p>👇 <i>Select a server below to browse available countries:</i></p>"
    )

    kb = InlineKeyboardBuilder()
    
    # S1 Route
    if s1 == "active":
        kb.row(premium_button(text="✈️ Server 1 — Aged Accounts", callback_data="buy_server1_route", icon_custom_emoji_id=TELEGRAM_EMOJI_ID))
    else:
        kb.row(InlineKeyboardButton(text="🛠️ Server 1 [Maintenance]", callback_data="server_maintenance"))
        
    # S2 Route
    if s2 == "active":
        kb.row(premium_button(text="✈️ Server 2 — High Quality", callback_data="buy_server2:0", icon_custom_emoji_id=TELEGRAM_EMOJI_ID))
    else:
        kb.row(InlineKeyboardButton(text="🛠️ Server 2 [Maintenance]", callback_data="server_maintenance"))
        
    # S3 Route
    if s3 == "active":
        kb.row(premium_button(text="✈️ Server 3 — Instant Global & LZT", callback_data="s3tg_open", icon_custom_emoji_id=TELEGRAM_EMOJI_ID))
    else:
        kb.row(InlineKeyboardButton(text="🛠️ Server 3 [Maintenance]", callback_data="server_maintenance"))

    # S4 Route — New Numbers (Telegram-only TemporaSMS)
    if s4 == "active":
        kb.row(premium_button(text="✈️ Server 4 — New Numbers (Telegram)", callback_data="s4_open", icon_custom_emoji_id=TELEGRAM_EMOJI_ID))
    else:
        kb.row(InlineKeyboardButton(text="🛠️ Server 4 [Maintenance]", callback_data="server_maintenance"))

    kb.row(InlineKeyboardButton(text="⬅️ Back to Menu", callback_data="back_main"))

    await cq.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())


# Server 1 Route: Redirect to your existing Pagination flow
@dp.callback_query(F.data == "buy_server1_route")
async def callback_route_server1(cq: CallbackQuery):
    await cq.answer()
    await send_country_menu(cq)

# Maintenance Handler
@dp.callback_query(F.data == "server_maintenance")
async def server_maintenance_alert(cq: CallbackQuery):
    await cq.answer("⚠️ This server is currently under maintenance or temporarily disabled by admin.", show_alert=True)

# ================= SERVER 1 ROUTING =================
@dp.callback_query(lambda c: c.data == "buy_server1_manual")
async def callback_buy_server1_manual(cq: CallbackQuery):
    await cq.answer()
    await send_country_menu(cq)  # Routes to your existing pagination flow


# --- MASTER PREFIX MAPPER (For Number Search) ---
ISO_TO_PREFIX = {
    "AF": "93", "AL": "355", "DZ": "213", "AD": "376", "AO": "244", "AG": "1268", "AR": "54", 
    "AM": "374", "AU": "61", "AT": "43", "AZ": "994", "BS": "1242", "BH": "973", "BD": "880", 
    "BB": "1246", "BY": "375", "BE": "32", "BZ": "501", "BJ": "229", "BT": "975", "BO": "591", 
    "BA": "387", "BW": "267", "BR": "55", "BG": "359", "BF": "226", "BI": "257", "CV": "238", 
    "KH": "855", "CM": "237", "CA": "1", "CF": "236", "TD": "235", "CL": "56", "CN": "86", 
    "CO": "57", "KM": "269", "CD": "243", "CG": "242", "CR": "506", "HR": "385", "CU": "53", 
    "CY": "357", "CZ": "420", "CI": "225", "DK": "45", "DJ": "253", "DO": "1809", "EC": "593", 
    "EG": "20", "SV": "503", "GQ": "240", "EE": "372", "SZ": "268", "ET": "251", "FI": "358", 
    "FR": "33", "GA": "241", "GM": "220", "GE": "995", "DE": "49", "GH": "233", "GR": "30", 
    "GT": "502", "GN": "224", "HT": "509", "HN": "504", "HU": "36", "IS": "354", "IN": "91", 
    "ID": "62", "IR": "98", "IQ": "964", "IE": "353", "IL": "972", "IT": "39", "JM": "1876", 
    "JP": "81", "JO": "962", "KZ": "7", "KE": "254", "KW": "965", "KG": "996", "LA": "856", 
    "LV": "371", "LB": "961", "LS": "266", "LR": "231", "LY": "218", "LT": "370", "LU": "352", 
    "MG": "261", "MW": "265", "MY": "60", "MV": "960", "ML": "223", "MT": "356", "MR": "222", 
    "MU": "230", "MX": "52", "MD": "373", "MN": "976", "ME": "382", "MA": "212", "MZ": "258", 
    "MM": "95", "NA": "264", "NP": "977", "NL": "31", "NZ": "64", "NI": "505", "NE": "227", 
    "NG": "234", "MK": "389", "NO": "47", "OM": "968", "PK": "92", "PA": "507", "PY": "595", 
    "PE": "51", "PH": "63", "PL": "48", "PT": "351", "QA": "974", "RO": "40", "RU": "7", 
    "RW": "250", "SA": "966", "SN": "221", "RS": "381", "SL": "232", "SG": "65", "SK": "421", 
    "SI": "386", "SO": "252", "ZA": "27", "ES": "34", "LK": "94", "SD": "249", "SE": "46", 
    "CH": "41", "SY": "963", "TJ": "992", "TZ": "255", "TH": "66", "TG": "228", "TN": "216", 
    "TR": "90", "TM": "993", "UG": "256", "UA": "380", "AE": "971", "GB": "44", "US": "1", 
    "UY": "598", "UZ": "998", "VE": "58", "VN": "84", "YE": "967", "ZM": "260", "ZW": "263",
    "HK": "852", "MO": "853", "TW": "886", "PR": "1", "PF": "689", "GP": "590"
}

# --- FSM State for Search ---
class Server2Search(StatesGroup):
    waiting_query = State()

# --- Helper: Safe Float Extractor from String (e.g., "3.2 USD" -> 3.2) ---
def safe_extract_float(val):
    if not val: return 0.0
    try:
        return float(val)
    except ValueError:
        match = re.search(r"[-+]?\d*\.\d+|\d+", str(val))
        return float(match.group()) if match else 0.0

# --- Helper: API Balance Check & Admin Low Balance Alert ---
async def check_s2_api_balance():
    settings = settings_col.find_one({"_id": "server_config"}) or {}
    active_api = settings.get("s2_api", "tgpva")
    
    try:
        async with aiohttp.ClientSession() as session:
            if active_api == "tgpva":
                url = f"https://tgpva.com/api/user/getBalance?apiKey={TGPVA_API_KEY}"
                async with session.get(url, timeout=5) as resp:
                    data = await resp.json()
                    if data.get("ok") and "result" in data:
                        bal = safe_extract_float(data["result"].get("balance", 0))
                        if bal < 1.0:
                            try: await bot.send_message(ADMINLOG, f"⚠️ <b>Low Balance Alert:</b> TGPVA balance is {bal} USD!")
                            except Exception: pass
                        return bal > 0.1, active_api
                        
            elif active_api == "tglion":
                url = f"https://TG-Lion.net?action=get_balance&apiKey={TGLION_API_KEY}&YourID={TGLION_ID}"
                async with session.get(url, timeout=5) as resp:
                    data = await resp.json()
                    if data.get("status") == "ok":
                        # Safe extract use kiya taaki "3.2 USD" se sirf 3.2 nikle
                        bal = safe_extract_float(data.get("balance", 0))
                        if bal < 1.0:
                            try: await bot.send_message(ADMINLOG, f"⚠️ <b>Low Balance Alert:</b> TG-Lion balance is {bal} USD!")
                            except Exception: pass
                        return bal > 0.1, active_api
    except Exception as e:
        print(f"❌ Balance Check Error ({active_api}): {e}")
        
    return False, active_api

# ================= SERVER 2 (API AUTO FLOW) =================
@dp.callback_query(F.data.startswith("buy_server2:"))
async def callback_buy_server2(cq: CallbackQuery):
    page = int(cq.data.split(":")[1])
    await cq.message.edit_text("🔄 <i>Fetching live stock from Server 2...</i>", parse_mode="HTML")
    
    countries, active_api = await fetch_server2_countries()
    
    if not countries:
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back", callback_data="buy")]])
        return await cq.message.edit_text("❌ No stock available or API is down right now.", reply_markup=kb)
        
    ITEMS_PER_PAGE = 20 # 10x2 Grid
    total_pages = max(1, (len(countries) + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE)
    
    # Page bounds safety
    page = max(0, min(page, total_pages - 1))
    
    start = page * ITEMS_PER_PAGE
    end = start + ITEMS_PER_PAGE
    page_items = countries[start:end]
    
    kb = InlineKeyboardBuilder()
    
    # 1. 10x2 Country Grid
    disc = get_user_discount(cq.from_user.id)
    for c in page_items:
        inr_price = apply_user_discount(c['price'] * USD_TO_INR, cq.from_user.id)
        btn_text = f"{plain_country_label(c['name'])} (₹{inr_price:.2f})"
        kb.button(text=btn_text, callback_data=f"srv2_info:{c['code']}:{c['price']:.4f}")
    kb.adjust(2)
    
    # 2. LZT Style Navigation Row (1, 2, • 3 •, 4, 5)
    nav = []
    if total_pages > 1:
        for p in range(total_pages):
            if p == page:
                # Current page ko bullet (•) mark karo
                nav.append(InlineKeyboardButton(text=f"• {p+1} •", callback_data="noop"))
            else:
                # Other pages ka direct link
                nav.append(InlineKeyboardButton(text=f"{p+1}", callback_data=f"buy_server2:{p}"))

        # Nav buttons ko 8 columns ke hisaab se break karke row me daalna
        for i in range(0, len(nav), 8):
            kb.row(*nav[i:i+8])
        
    # 3. Big Search Button Row
    kb.row(InlineKeyboardButton(text="🔍 Search Country / Prefix", callback_data="srv2_search_init"))
    
    # 4. Big Back Button Row
    kb.row(InlineKeyboardButton(text="⬅️ Back to Servers", callback_data="buy"))
    
    disc_line = f"<br>🏷️ <b>VIP Discount Active:</b> <code>{disc:g}% OFF</code>" if disc > 0 else ""
    text = (
        f"<h2>{tg_icon()} Telegram Accounts — Server 2</h2>"
        "<blockquote>🌍 <b>Live High-Quality Stock</b><br>"
        f"Countries are sorted by lowest price first. Tap any country below to inspect stock &amp; purchase.{disc_line}</blockquote>"
        f"<p>📄 <i>Page <b>{page + 1}</b> of <b>{total_pages}</b> · Total Countries: <b>{len(countries)}</b></i></p>"
    )
    
    await cq.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())

# --- SEARCH LOGIC (With Country, Code, and Prefix) ---
@dp.callback_query(F.data == "srv2_search_init")
async def srv2_search_init(cq: CallbackQuery, state: FSMContext):
    await cq.message.answer(
        "<h2>🔍 Search Server 2 Catalog</h2>"
        "<blockquote>🌍 <b>Enter Country Name, ISO Code, or Phone Prefix</b><br>"
        "Examples: <code>India</code>, <code>IN</code>, <code>ZM</code>, <code>+1</code>, <code>880</code></blockquote>", 
        parse_mode="HTML"
    )
    await state.set_state(Server2Search.waiting_query)
    await cq.answer()

@dp.message(StateFilter(Server2Search.waiting_query))
async def srv2_do_search(msg: Message, state: FSMContext):
    query = msg.text.strip().lower()
    clean_query = query.replace("+", "") # Automatically handle numbers with +
    await state.clear()
    
    status_msg = await msg.answer("🔄 <i>Searching...</i>", parse_mode="HTML")
    countries, _ = await fetch_server2_countries()
    
    results = []
    for c in countries:
        code = c['code'].upper()
        name = c['name'].lower()
        prefix = ISO_TO_PREFIX.get(code, "")
        
        # Match by name, ISO code, or phone prefix
        if query in name or query == code.lower() or clean_query == prefix:
            results.append(c)
    
    if not results:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔍 Search Again", callback_data="srv2_search_init")],
            [InlineKeyboardButton(text="⬅️ Back to List", callback_data="buy_server2:0")]
        ])
        return await status_msg.edit_text(
            "<h2>❌ No Matching Countries</h2>"
            f"<blockquote>🔎 No active stock found matching <code>{escape(msg.text)}</code>.</blockquote>",
            parse_mode="HTML",
            reply_markup=kb,
        )
        
    kb = InlineKeyboardBuilder()
    for c in results[:20]:
        inr_price = apply_user_discount(c['price'] * USD_TO_INR, msg.from_user.id)
        kb.button(text=f"{plain_country_label(c['name'])} (₹{inr_price:.2f})", callback_data=f"srv2_info:{c['code']}:{c['price']:.4f}")
    kb.adjust(2)
    
    # Large buttons for search results navigation
    kb.row(InlineKeyboardButton(text="🔍 Search Again", callback_data="srv2_search_init"))
    kb.row(InlineKeyboardButton(text="⬅️ Back to List", callback_data="buy_server2:0"))
    
    await status_msg.edit_text(
        f"<h2>🔎 Search Results ({len(results)})</h2>"
        f"<blockquote>🌍 Showing matches for <code>{escape(msg.text)}</code>:</blockquote>",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )

# --- DETAILS VIEW (STOCK & PRICE) ---
@dp.callback_query(F.data.startswith("srv2_info:"))
async def srv2_country_info(cq: CallbackQuery):
    parts = cq.data.split(":")
    code = parts[1]
    usd_price = float(parts[2])
    inr_price = apply_user_discount(usd_price * USD_TO_INR, cq.from_user.id)
    disc = get_user_discount(cq.from_user.id)
    
    await cq.message.edit_text("🔄 <i>Loading country details...</i>", parse_mode="HTML")
    countries, _ = await fetch_server2_countries()
    c_data = next((c for c in countries if c['code'] == code), None)
    
    if not c_data:
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Back", callback_data="buy_server2:0")]])
        return await cq.message.edit_text(
            "<h2>❌ Out of Stock</h2><blockquote>This country is currently out of stock.</blockquote>",
            reply_markup=kb,
        )

    disc_line = f"<br>🏷️ <b>VIP Discount:</b> <code>{disc:g}% OFF</code>" if disc > 0 else ""
    text = (
        f"<h2>{tg_icon()} {c_data['name']} — Server 2</h2>"
        f"<blockquote>🌐 <b>Country:</b> {c_data['name']}<br>"
        f"🏷️ <b>Price:</b> <code>₹{inr_price:.2f}</code>{disc_line}<br>"
        f"📦 <b>Available Stock:</b> <code>{c_data['qty']} pcs</code><br>"
        f"⚡ <b>Delivery:</b> Instant Number + OTP + 2FA</blockquote>"
        "<p>👇 <i>Tap <b>Buy Now</b> below to proceed:</i></p>"
    )
    
    kb = InlineKeyboardBuilder()
    kb.row(premium_button(text="🛒 Buy Now", callback_data=f"srv2_terms:{code}:{usd_price:.4f}"))
    kb.row(
        InlineKeyboardButton(text="⬅️ Back", callback_data="buy_server2:0"),
        InlineKeyboardButton(text="🔄 Change Country", callback_data="buy_server2:0")
    )
    await cq.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())

# --- TERMS AND CONDITIONS ---
@dp.callback_query(F.data.startswith("srv2_terms:"))
async def srv2_show_terms(cq: CallbackQuery):
    _, code, usd_price = cq.data.split(":")
    usd_price = float(usd_price)
    inr_price = apply_user_discount(usd_price * USD_TO_INR, cq.from_user.id)
    
    settings = settings_col.find_one({"_id": "server_config"}) or {}
    active_api = settings.get("s2_api", "tgpva")
    country_display_name = get_country_name(code) if active_api == "tgpva" else code
    
    terms_text = (
        f"<h2>⚠️ Purchase Terms — Server 2</h2>"
        f"<blockquote>🌐 <b>Country:</b> {country_display_name}<br>"
        f"🏷️ <b>Price:</b> <code>₹{inr_price:.2f}</code></blockquote>"
        "<blockquote>📋 <b>Service Policy</b><br>"
        "• 🚫 <b>No Refunds:</b> All completed sales are final.<br>"
        "• 📨 <b>OTP Policy:</b> Multiple OTP requests supported while session is active.<br>"
        "• ⏳ <b>Warranty:</b> Session ends once you log out the bot device.</blockquote>"
        "<p>❓ <i>Do you accept these terms to complete your purchase?</i></p>"
    )
    
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="✅ Accept & Buy", callback_data=f"srv2_buy:{code}:{usd_price:.4f}"))
    kb.row(InlineKeyboardButton(text="⬅️ Back", callback_data=f"srv2_info:{code}:{usd_price:.4f}"))
    await cq.message.edit_text(terms_text, parse_mode="HTML", reply_markup=kb.as_markup())

# ================= API DEBUG LOGGER =================
async def log_api_response(api_name: str, action: str, url: str, raw_response: str, is_error: bool = False):
    """Sends raw API responses to the Admin Log group for debugging."""
    try:
        status_icon = "❌ ERROR / FAIL" if is_error else "✅ SUCCESS"
        # API Keys ko URL se hide kar diya taaki logs me key leak na ho
        safe_url = re.sub(r'(api_key|apiKey)=[^&]+', r'\1=HIDDEN_KEY', url)
        # Message lamba hone par Telegram block na kare, isliye 1000 characters limit
        short_resp = str(raw_response)[:1000] 
        
        log_text = (
            f"🛠 <b>API DEBUG LOG</b> | <code>{api_name}</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>Action:</b> {action}\n"
            f"<b>Status:</b> {status_icon}\n"
            f"<b>Request:</b> <code>{safe_url}</code>\n\n"
            f"<b>Raw Response:</b>\n<pre>{html.escape(short_resp)}</pre>"
        )
        await bot.send_message(ADMINLOG, log_text, parse_mode="HTML")
    except Exception as e:
        print(f"API Logger Failed: {e}")

import urllib.parse
import json
import re

# --- PURCHASE EXECUTION (SERVER 2) ---
@dp.callback_query(F.data.startswith("srv2_buy:"))
async def srv2_buy_number(cq: CallbackQuery):
    code = cq.data.split(":")[1]
    usd_price = float(cq.data.split(":")[2])
    user_id = cq.from_user.id
    user = get_or_create_user(user_id, cq.from_user.username)
    inr_price = apply_user_discount(usd_price * USD_TO_INR, user)
    
    if user.get("balance", 0.0) < inr_price:
        return await cq.answer("❌ Insufficient Balance! Please recharge.", show_alert=True)
        
    status_msg = await cq.message.edit_text("⏳ <i>Purchasing number... Waiting for response (Please do not click anything)...</i>", parse_mode="HTML")
    
    is_ok, active_api = await check_s2_api_balance()
    if not is_ok:
        try: await bot.send_message(ADMINLOG, f"🚨 <b>CRITICAL ALERT:</b> Server 2 API (<code>{active_api.upper()}</code>) Balance is LOW or ZERO!")
        except Exception: pass
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back", callback_data="buy_server2:0")]])
        return await status_msg.edit_text("❌ <b>Failed to purchase. Server API Balance is low.</b>", parse_mode="HTML", reply_markup=kb)
    
    phone_number = None
    order_hash = "None"
    last_error = "Unknown Error"
    
    async with aiohttp.ClientSession() as session:
        try:
            if active_api == "tgpva":
                url = f"https://tgpva.com/api/user/getNumber?apiKey={TGPVA_API_KEY}&country={code}"
                async with session.get(url, timeout=35) as resp:
                    text_resp = await resp.text()
                    is_error = '"ok":true' not in text_resp.replace(" ", "")
                    await log_api_response("TGPVA", "getNumber (Buy)", url, text_resp, is_error)
                    
                    data = json.loads(text_resp)
                    if data.get("ok"):
                        phone_number = str(data["result"]["phone"]).strip()
                        order_hash = str(data["result"]["hash"]).strip()
                    else:
                        last_error = str(data)
                        
            elif active_api == "tglion":
                safe_code = str(code).lower().strip()
                url = f"https://TG-Lion.net?action=getNumber&apiKey={TGLION_API_KEY}&YourID={TGLION_ID}&country_code={safe_code}"
                
                async with session.get(url, timeout=35) as resp:
                    text_resp = await resp.text()
                    is_error = "ok" not in text_resp.lower()
                    await log_api_response("TG-Lion", "getNumber (Buy)", url, text_resp, is_error)
                    
                    try:
                        data = json.loads(text_resp)
                        if data.get("status") == "ok":
                            raw_num = data.get("Number") or data.get("number")
                            phone_number = str(raw_num).strip() if raw_num else None
                        else:
                            last_error = str(data.get("msg", text_resp))
                    except Exception:
                        if "ok" in text_resp.lower():
                            num_match = re.search(r'"[nN]umber"\s*:\s*"?(\+?\d+)"?', text_resp)
                            if num_match: phone_number = num_match.group(1).strip()
                        else:
                            msg_match = re.search(r'"msg"\s*:\s*"([^"]+)"', text_resp)
                            last_error = msg_match.group(1) if msg_match else "Non-JSON API Error."
                            
        except asyncio.TimeoutError:
            last_error = "API Timeout: Server took too long to respond."
        except Exception as e:
            last_error = f"Network Error: {e}"

    if not phone_number:
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back", callback_data="buy_server2:0")]])
        return await status_msg.edit_text(f"❌ <b>Purchase failed.</b>\n\nPlease try again later.", parse_mode="HTML", reply_markup=kb)
        
    new_balance = user.get("balance", 0.0) - inr_price
    users_col.update_one({"_id": user_id}, {"$set": {"balance": new_balance}})
    country_display_name = get_country_name(code)

    orders_col.insert_one({
        "user_id": user_id, "country": country_display_name, "number": phone_number,
        "price": inr_price, "server": 2, "status": "purchased", "created_at": datetime.now(timezone.utc)
    })
    
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="📩 Get Code", callback_data=f"srv2_otp:{phone_number}:{order_hash}"),
           InlineKeyboardButton(text="📋 Copy Number", copy_text=CopyTextButton(text=str(phone_number))))
    kb.row(InlineKeyboardButton(text="♻️ Buy Again", callback_data=f"srv2_info:{code}:{usd_price:.4f}"))
    kb.row(InlineKeyboardButton(text="👨‍💻 Support", url=f"https://t.me/{SUPPORT}"))

    await status_msg.edit_text(
        f"<h2>✅ Account Purchased — Server 2</h2>"
        f"<blockquote>{tg_icon()} <b>Service:</b> Telegram (Server 2)<br>"
        f"🌍 <b>Country:</b> {country_display_name}<br>"
        f"📞 <b>Number:</b> <code>{phone_number}</code><br>"
        f"🏷️ <b>Price Paid:</b> <code>₹{inr_price:.2f}</code><br>"
        f"💰 <b>Remaining Balance:</b> <code>₹{new_balance:.2f}</code></blockquote>"
        "<p>👇 <i>Request a login code in Telegram, then tap <b>Get Code</b> below:</i></p>",
        parse_mode="HTML", reply_markup=kb.as_markup()
    )


# --- GET OTP (SERVER 2) ---
@dp.callback_query(F.data.startswith("srv2_otp:"))
async def srv2_get_otp(cq: CallbackQuery):
    parts = cq.data.split(":")
    phone_number = parts[1]
    order_hash = parts[2] if len(parts) > 2 else "None"
    
    await cq.answer("🔄 Requesting OTP...", show_alert=False)
    
    settings = settings_col.find_one({"_id": "server_config"}) or {}
    active_api = settings.get("s2_api", "tgpva")
    code, password = None, "None"
    
    async with aiohttp.ClientSession() as session:
        if active_api == "tgpva":
            url = f"https://tgpva.com/api/user/getCode?apiKey={TGPVA_API_KEY}&number={phone_number}&hash={order_hash}"
            try:
                async with session.get(url, timeout=15) as resp:
                    text_resp = await resp.text()
                    await log_api_response("TGPVA", "getCode (OTP)", url, text_resp)
                    data = json.loads(text_resp)
                    if data.get("ok") and data["result"]["status"] == "Received":
                        code = str(data["result"]["code"]).strip()
                        password = str(data["result"].get("password", "None")).strip()
            except Exception: pass
            
        elif active_api == "tglion":
            safe_num = urllib.parse.quote(str(phone_number).strip())
            url = f"https://TG-Lion.net?action=getCode&number={safe_num}&apiKey={TGLION_API_KEY}&YourID={TGLION_ID}"
            try:
                async with session.get(url, timeout=15) as resp:
                    text_resp = await resp.text()
                    is_error = "ok" not in text_resp.lower() or "code" not in text_resp.lower()
                    await log_api_response("TG-Lion", "getCode (OTP)", url, text_resp, is_error)
                    try:
                        data = json.loads(text_resp)
                        if data.get("status") == "ok" and "code" in data:
                            code = str(data.get("code")).strip()
                            password = str(data.get("pass", "None")).strip()
                    except Exception:
                        if "ok" in text_resp.lower() and '"code"' in text_resp:
                            c_match = re.search(r'"code"\s*:\s*"?(\d+)"?', text_resp)
                            p_match = re.search(r'"pass"\s*:\s*"([^"]*)"', text_resp)
                            if c_match: code = c_match.group(1).strip()
                            if p_match: password = p_match.group(1).strip()
            except Exception: pass

    if code:
        kb = InlineKeyboardBuilder()
        kb.row(InlineKeyboardButton(text="📋 Copy OTP", copy_text=CopyTextButton(text=str(code))),
               InlineKeyboardButton(text="📋 Copy 2FA Pass", copy_text=CopyTextButton(text=str(password))))
        kb.row(InlineKeyboardButton(text="🔄 Get New Code", callback_data=f"srv2_otp:{phone_number}:{order_hash}"))
        
        # LOGOUT BUTTON SIRF YAHAN DIKHEGA (OTP Milne ke baad)
        if active_api == "tgpva":
            kb.row(InlineKeyboardButton(text="🚪 Logout Bot Device", callback_data=f"srv2_logout:{phone_number}"))
            
        await cq.message.edit_text(
            "<h2>✅ Order Completed — OTP Received</h2>"
            f"<blockquote>📞 <b>Number:</b> <code>{phone_number}</code><br>"
            f"💬 <b>Login Code:</b> <code>{code}</code><br>"
            f"🔐 <b>2FA Password:</b> <code>{password}</code></blockquote>"
            "<p>✨ <i>Use the copy buttons below to sign in quickly.</i></p>",
            parse_mode="HTML", reply_markup=kb.as_markup()
        )
        
        user_id = cq.from_user.id
        user = users_col.find_one({"_id": user_id}) or {}
        buyer_name = user.get("username") or f"User {user_id}"
        balance = user.get("balance", "N/A")
        order_doc = orders_col.find_one({"number": phone_number, "status": "purchased"})
        country_name = order_doc.get("country", "Unknown") if order_doc else "Unknown"
        masked_number = str(phone_number)[:6] + "•••••" if phone_number else "Hidden"

        channel_message = (
            f"<pre><u>✅ <b>New Number Purchase Successful</b></u></pre>\n\n"
            f"➖ <b><u>Country:</u></b> {country_name}\n"
            f"➖ <b><u>Application:</u> Теlegгам 🍷</b>\n\n"
            f"➕ <b>Number: {masked_number} 📞</b>\n"
            f"➕ <b>Code</b> <span class='tg-spoiler'>{code}</span> 💬\n"
            f"➕ <b>Server:</b> (2) 🥂\n"
            f"➕ <b>Password:</b> <span class='tg-spoiler'>{password}</span> 🔐\n\n"
            f"<b>• @{BOTUSER} || @{CHANNEL}</b>"
        )
        buy_button = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="• Buy Now •", url=f"https://t.me/{BOTUSER}?start=starting")]])
        try: await bot.send_message(SALES, channel_message, parse_mode="HTML", reply_markup=buy_button)
        except Exception: pass

        admin_message = (
            f"<pre>📢 New Purchase Alert</pre>\n\n"
            f"<b>• Application:</b> Telegram\n"
            f"<b>• Country:</b> {country_name}\n"
            f"<b>• Number:</b> {phone_number}\n"
            f"<b>• OTP:</b> <code>{code}</code>\n"
            f"➖ <b>Password:</b> <span class='tg-spoiler'>{password}</span> 🔐\n\n"
            f"<b>👤 User:</b> @{buyer_name} (<code>{user_id}</code>)\n"
            f"<b>💰 Balance:</b> ₹{balance}"
        )
        userbutton = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="USER ID", url=f"tg://openmessage?user_id={user_id}")]])
        try: await bot.send_message(ADMINLOG, admin_message, parse_mode="HTML", reply_markup=userbutton)
        except Exception: pass

    else:
        await cq.answer("💬 No code received yet. Try pressing 'Get Code' again after 5 seconds.", show_alert=True)


# --- LOGOUT DEVICE (SERVER 2) ---
@dp.callback_query(F.data.startswith("srv2_logout:"))
async def srv2_logout_device(cq: CallbackQuery):
    phone_number = cq.data.split(":")[1]
    await cq.answer("🔄 Logging out device...", show_alert=False)
    url = f"https://tgpva.com/api/user/getLogout?apiKey={TGPVA_API_KEY}&number={phone_number}"
    
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, timeout=15) as resp:
                text_resp = await resp.text()
                
                # Admin Log tak ki tumhe pata chale API kya bol rahi hai
                is_error = '"ok":true' not in text_resp.replace(" ", "")
                await log_api_response("TGPVA", "getLogout (Logout Device)", url, text_resp, is_error)
                
                data = json.loads(text_resp)
                if data.get("ok"):
                    # Success Msg & Polling Closed Message
                    await cq.message.answer(
                        "✅ <b>Bot session has been logged out successfully!</b>\n"
                        "🚫 OTP polling closed for this number.",
                        parse_mode="HTML"
                    )
                    # Button press hone ke baad wapas click na ho isliye Keyboard se button hatana (Optional, achhi practice hai)
                    new_kb = InlineKeyboardBuilder()
                    new_kb.row(InlineKeyboardButton(text="• Support •", url=f"https://t.me/{SUPPORT}"))
                    try: await cq.message.edit_reply_markup(reply_markup=new_kb.as_markup())
                    except Exception: pass
                else:
                    await cq.message.answer(f"❌ <b>Failed to logout:</b> {data.get('error', 'Unknown Error')}", parse_mode="HTML")
        except Exception:
            await cq.answer("❌ Error connecting to logout.", show_alert=True)

# ================= Country Menu with Pagination =================
COUNTRIES_PER_PAGE = 10

async def send_country_menu(cq: CallbackQuery, page: int = 0):
    countries = await asyncio.to_thread(lambda: list(countries_col.find({})))
    total = len(countries)

    if total == 0:
        kb_empty = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Back to Servers", callback_data="buy")]])
        return await cq.message.edit_text(
            "<h2>❌ No Stock Available</h2><blockquote>No countries are currently stocked on Server 1.</blockquote>",
            reply_markup=kb_empty,
        )
    user_id = cq.from_user.id
    user = users_col.find_one({"_id": user_id}) or {}
    balance = f"₹{user.get('balance', 0.0):.2f}"
    disc = get_user_discount(user)

    # Pagination logic
    start = page * COUNTRIES_PER_PAGE
    end = start + COUNTRIES_PER_PAGE
    paginated = countries[start:end]

    kb = InlineKeyboardBuilder()
    for c in paginated:
        c_price = apply_user_discount(c.get('price', 0), user)
        kb.button(text=f"{html.escape(plain_country_label(c['name']))} (₹{c_price:g})", callback_data=f"country:{c['name']}")
    kb.adjust(2)

    # Navigation buttons
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton(text="⬅️ Prev", callback_data=f"countries_page:{page-1}"))
    if end < total:
        nav_buttons.append(InlineKeyboardButton(text="Next ➡️", callback_data=f"countries_page:{page+1}"))
    if nav_buttons:
        kb.row(*nav_buttons)

    # Return to main menu
    kb.row(InlineKeyboardButton(text="⬅️ Back to Servers", callback_data="buy"))
    kb.row(InlineKeyboardButton(text="🏠 Home", callback_data="back_main"))

    total_pages = (total - 1) // COUNTRIES_PER_PAGE + 1
    disc_line = f"<br>🏷️ <b>VIP Discount Active:</b> <code>{disc:g}% OFF</code>" if disc > 0 else ""
    text = (
        f"<h2>{tg_icon()} Telegram Accounts — Server 1</h2>"
        f"<blockquote>💰 <b>Wallet Balance:</b> <code>{balance}</code>{disc_line}<br>"
        f"🖥️ <b>Server:</b> Server 1 (Aged &amp; Spam-Free)<br>"
        f"📄 <b>Page:</b> <code>{page + 1} / {total_pages}</code><br>"
        f"✅ <a href=\"https://t.me/{SALESLOG}\">Live Successful Purchases</a></blockquote>"
        "<p>🌍 <i>Select a country below to inspect stock &amp; purchase:</i></p>"
    )
    await cq.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="HTML", disable_web_page_preview=True)


# ================= Country Pagination Callback =================
@dp.callback_query(lambda c: c.data.startswith("countries_page:"))
async def paginate_countries(cq: CallbackQuery):
    _, page_str = cq.data.split(":")
    try:
        page = int(page_str)
    except ValueError:
        page = 0
    await send_country_menu(cq, page)
    await cq.answer()

# =============== Country Selection =================
@dp.callback_query(lambda c: c.data.startswith("country:"))
async def callback_country(cq: CallbackQuery):
    await cq.answer()
    _, country_name = cq.data.split(":", 1)

    country = await asyncio.to_thread(lambda: countries_col.find_one({"name": country_name}))
    if not country:
        return await cq.answer("❌ Country not found", show_alert=True)

    c_price = apply_user_discount(country['price'], cq.from_user.id)
    disc = get_user_discount(cq.from_user.id)
    disc_line = f"<br>🏷️ <b>VIP Discount:</b> <code>{disc:g}% OFF</code>" if disc > 0 else ""
    text = (
        f"<h2>{tg_icon()} {html.escape(country['name'])} — Server 1</h2>"
        f"<blockquote>🌍 <b>Country:</b> {html.escape(country['name'])}<br>"
        f"🏷️ <b>Price:</b> <code>₹{c_price:g}</code>{disc_line}<br>"
        f"📦 <b>Available Stock:</b> <code>{country['stock']} pcs</code><br>"
        f"🖥️ <b>Server:</b> Server 1 (Aged Accounts)</blockquote>"
        "<p>👇 <i>Tap <b>Buy Now</b> below to proceed:</i></p>"
    )

    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="🛒 Buy Now", callback_data=f"buy_now:{country_name}")
    )
    kb.row(
        InlineKeyboardButton(text="⬅️ Back to Countries", callback_data="buy_server1_route")
    )

    await cq.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())
# ================= Buy Now Flow =================



# ================= Buy Now: Validation & Terms =================
@dp.callback_query(F.data.startswith("buy_now:"))
async def callback_buy_start(cq: CallbackQuery, state: FSMContext):
    # Clear any previous states to avoid conflicts
    await state.clear()
    
    _, country_name = cq.data.split(":", 1)
    
    # Fetch Data Asynchronously
    country, user = await asyncio.to_thread(lambda: (
        countries_col.find_one({"name": country_name}),
        get_or_create_user(cq.from_user.id, cq.from_user.username)
    ))

    # 1. Validate Country Existence
    if not country:
        return await cq.answer("❌ Country data not found.", show_alert=True)

    country_price = apply_user_discount(country["price"], user)
    country_stock = country["stock"]
    user_balance = user.get("balance", 0.0)

    # 2. Validate Stock
    if country_stock < 1:
        return await cq.answer(f"⚠️ Out of Stock! No accounts left for {country_name}.", show_alert=True)

    # 3. Validate Balance (If low, show Recharge prompt immediately)
    if user_balance < country_price:
        text = (
            "<h2>🚫 Insufficient Wallet Balance</h2>"
            f"<blockquote>🏷️ <b>Required Amount:</b> <code>₹{country_price:.2f}</code><br>"
            f"💰 <b>Your Balance:</b> <code>₹{user_balance:.2f}</code><br>"
            f"📉 <b>Shortfall:</b> <code>₹{country_price - user_balance:.2f}</code></blockquote>"
            "<p>💳 <i>Please recharge your wallet to continue with this purchase.</i></p>"
        )
        kb = InlineKeyboardBuilder()
        kb.row(InlineKeyboardButton(text="💳 Recharge Now", callback_data="recharge"))
        kb.row(InlineKeyboardButton(text="⬅️ Back", callback_data=f"country:{country_name}"))
        
        return await cq.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())

    # 4. Show Terms & Confirmation (Balance & Stock are OK)
    terms_text = (
        "<h2>⚠️ Purchase Terms — Server 1</h2>"
        f"<blockquote>🌍 <b>Country:</b> {html.escape(country_name)}<br>"
        f"🏷️ <b>Price:</b> <code>₹{country_price:.2f}</code></blockquote>"
        "<blockquote>📋 <b>Service Policy</b><br>"
        "• 🚫 <b>No Refunds:</b> All completed sales are final.<br>"
        "• 📨 <b>OTP Policy:</b> Once OTP is delivered, returns are not allowed.<br>"
        "• ❄️ <b>Account Care:</b> Accounts are verified fresh before delivery.<br>"
        "• ⏳ <b>Check Window:</b> 10 minutes to verify login.</blockquote>"
        "<p>❓ <i>Do you accept these terms to complete your purchase?</i></p>"
    )

    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="✅ Accept & Buy", callback_data=f"confirm_buy:{country_name}"),
        InlineKeyboardButton(text="❌ Decline", callback_data=f"country:{country_name}")
    )
    kb.row(InlineKeyboardButton(text="⬅️ Back", callback_data=f"country:{country_name}"))

    await cq.message.edit_text(terms_text, parse_mode="HTML", reply_markup=kb.as_markup())


# ================= Purchase Execution (Quantity = 1) =================
@dp.callback_query(F.data.startswith("confirm_buy:"))
async def callback_process_purchase(cq: CallbackQuery, state: FSMContext):
    _, country_name = cq.data.split(":", 1)
    user_id = cq.from_user.id
    quantity = 1  # Hardcoded as per requirement

    # Re-fetch data to ensure stock/balance didn't change while reading terms
    country = countries_col.find_one({"name": country_name})
    user = users_col.find_one({"_id": user_id})

    # --- Safety Checks ---
    if not country:
        return await cq.answer("❌ Error: Country missing.", show_alert=True)
    
    if country["stock"] < 1:
        await cq.answer("⚠️ Just sold out! Stock is now 0.", show_alert=True)
        return await send_country_menu(cq) # Redirect to menu

    price = apply_user_discount(country["price"], user)
    if user["balance"] < price:
        await cq.answer("❌ Insufficient balance.", show_alert=True)
        return # Optionally redirect to recharge

    # --- Fetch Unused Number ---
    # We use find_one_and_update (Atomic operation) or find then update
    # Here we fetch one unused number
    number_doc = numbers_col.find_one({"country": country_name, "used": False})
    
    if not number_doc:
        # Stock count mismatch safety
        countries_col.update_one({"name": country_name}, {"$set": {"stock": 0}})
        return await cq.answer("⚠️ System Error: Stock mismatch. Contact Admin.", show_alert=True)

    # --- Calculate New Balance ---
    new_balance = user["balance"] - price

    # --- EXECUTE DB TRANSACTION ---
    try:
        # 1. Deduct Balance
        users_col.update_one({"_id": user_id}, {"$set": {"balance": new_balance}})
        
        # 2. Mark Number as Used
        numbers_col.update_one({"_id": number_doc["_id"]}, {"$set": {"used": True}})
        
        # 3. Create Order Log
        orders_col.insert_one({
            "user_id": user_id,
            "country": country_name,
            "number": number_doc["number"],
            "price": price,
            "status": "purchased",
            "created_at": datetime.now(timezone.utc)
        })
        
        # 4. Decrease Stock
        countries_col.update_one({"name": country_name}, {"$inc": {"stock": -1}})
        
    except Exception as e:
        print(f"Transaction Error: {e}")
        return await cq.answer("❌ Transaction failed. Please try again.", show_alert=True)

    # --- Send Success Message ---
    text_to_copy = str(number_doc["number"])
    
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📩 Get OTP",
                    callback_data=f"get_otp:{number_doc['number']}"
                ),
                InlineKeyboardButton(
                    text="📋 Copy Number",
                    copy_text=CopyTextButton(text=text_to_copy)
                )
            ],
            [
                InlineKeyboardButton(
                    text="👨‍💻 Support",                    
                    url=f"https://t.me/{SUPPORT}"
                )
            ]
        ]
    )

    success_msg = (
        f"<h2>✅ Account Purchased — Server 1</h2>"
        f"<blockquote>{tg_icon()} <b>Service:</b> Telegram (Server 1)<br>"
        f"🌍 <b>Country:</b> {html.escape(country_name)}<br>"
        f"📞 <b>Number:</b> <code>+{number_doc['number']}</code><br>"
        f"🏷️ <b>Price Paid:</b> <code>₹{price}</code><br>"
        f"💰 <b>Remaining Balance:</b> <code>₹{new_balance:.2f}</code></blockquote>"
        "<p>👇 <i>Request a login code in Telegram, then tap <b>Get OTP</b> below:</i></p>"
    )

    await cq.message.edit_text(
        success_msg,
        parse_mode="HTML",
        reply_markup=kb
    )    


# ================= Admin Add Number Flow =================
@dp.message(Command("add"))
async def cmd_add_start(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return await msg.answer("❌ Not authorized.")
    countries = list(countries_col.find({}))
    if not countries:
        return await msg.answer("❌ No countries found. Add some countries first in DB.")
    kb = InlineKeyboardBuilder()
    for c in countries:
        kb.button(text=plain_country_label(c["name"]), callback_data=f"add_country:{c['name']}")
    kb.adjust(2)
    await msg.answer("🌍 Select the country you want to add a number for:", reply_markup=kb.as_markup())
    await state.set_state(AddSession.waiting_country)

@dp.callback_query(F.data.startswith("add_country:"))
async def callback_add_country(cq: CallbackQuery, state: FSMContext):
    await cq.answer()
    _, country_name = cq.data.split(":", 1)
    await state.update_data(country=country_name)
    await cq.message.answer(f"📞 Enter the phone number for {country_name} (e.g., +14151234567):")
    await state.set_state(AddSession.waiting_number)

@dp.message(AddSession.waiting_number)
async def add_number_get_code(msg: Message, state: FSMContext):
    data = await state.get_data()
    country = data["country"]
    phone = msg.text.strip()
    await state.update_data(number=phone)

    api_id = int(os.getenv("API_ID"))
    api_hash = os.getenv("API_HASH")

    session = StringSession()
    client = TelegramClient(session, api_id, api_hash)
    await client.connect()
    try:
        sent = await client.send_code_request(phone)
        await msg.answer("📩 Code sent! Please enter the OTP you received on Telegram or SMS:")
        await state.update_data(session=session.save(), phone_code_hash=sent.phone_code_hash)
        await client.disconnect()
        await state.set_state(AddSession.waiting_otp)
    except Exception as e:
        await client.disconnect()
        await msg.answer(f"❌ Failed to send code: {e}")


@dp.message(AddSession.waiting_otp)
async def add_number_verify_code(msg: Message, state: FSMContext):
    data = await state.get_data()
    country = data["country"]
    phone = data["number"]
    session_str = data["session"]
    phone_code_hash = data.get("phone_code_hash")

    api_id = int(os.getenv("API_ID"))
    api_hash = os.getenv("API_HASH")

    client = TelegramClient(StringSession(session_str), api_id, api_hash)
    await client.connect()

    try:
        await client.sign_in(
            phone=phone,
            code=msg.text.strip(),
            phone_code_hash=phone_code_hash
        )

        string_session = client.session.save()
        await client.disconnect()

        # 🔹 SAVE / UPDATE NUMBER
        numbers_col.update_one(
            {"number": phone},
            {
                "$set": {
                    "country": country,
                    "string_session": string_session,
                    "password": None,
                    "used": False
                }
            },
            upsert=True
        )

        # 🔥 ADD TO STOCK (YOU MISSED THIS)
        countries_col.update_one(
            {"name": country},
            {"$inc": {"stock": 1}},
            upsert=True
        )

        await msg.answer(
            f"✅ Session Added Successfully\n\n"
            f"🌍 Country: {country}\n"
            f"📱 Number: <code>{phone}</code>\n"
            f"🔐 Password: <code>None</code>\n\n"
            f"🔑 String Session:\n"
            f"<blockquote expandable><code>{string_session}</code></blockquote>",
            parse_mode="HTML"
        )

        await msg.answer(
            "➕ Send another phone number for this country\n"
            "❌ Or type <b>cancel</b> to stop.",
            parse_mode="HTML"
        )
        await state.set_state(AddSession.waiting_next_action)
    except Exception as e:
        if "PASSWORD" in str(e).upper():
            await msg.answer("🔐 Two-step verification enabled. Send password:")
            await state.set_state(AddSession.waiting_password)
        else:
            await client.disconnect()
            await msg.answer(f"❌ Error verifying OTP: {e}")
            


@dp.message(AddSession.waiting_password)
async def add_number_with_password(msg: Message, state: FSMContext):
    data = await state.get_data()
    country = data["country"]
    phone = data["number"]
    session_str = data["session"]
    password = msg.text.strip()

    api_id = int(os.getenv("API_ID"))
    api_hash = os.getenv("API_HASH")

    client = TelegramClient(StringSession(session_str), api_id, api_hash)
    await client.connect()

    try:
        await client.sign_in(password=password)
        string_session = client.session.save()
        await client.disconnect()

        # 🔹 SAVE / UPDATE NUMBER
        numbers_col.update_one(
            {"number": phone},
            {
                "$set": {
                    "country": country,
                    "string_session": string_session,
                    "password": password,
                    "used": False
                }
            },
            upsert=True
        )

        # 🔥 ADD TO STOCK (CRITICAL)
        countries_col.update_one(
            {"name": country},
            {"$inc": {"stock": 1}},
            upsert=True
        )

        await msg.answer(
            f"✅ Session Added Successfully (2FA)\n\n"
            f"🌍 Country: {country}\n"
            f"📱 Number: <code>{phone}</code>\n"
            f"🔐 Password: <code>{password}</code>\n\n"
            f"🔑 String Session:\n"
            f"<blockquote expandable><code>{string_session}</code></blockquote>",
            parse_mode="HTML"
        )

        await msg.answer(
            "➕ Send another phone number for this country\n"
            "❌ Or type <b>cancel</b> to stop.",
            parse_mode="HTML"
        )
        await state.set_state(AddSession.waiting_next_action)
    except Exception as e:
        await client.disconnect()
        await msg.answer(f"❌ Error signing in with password: {e}")

@dp.message(AddSession.waiting_next_action)
async def add_next_number_or_cancel(msg: Message, state: FSMContext):
    text = msg.text.strip()

    if text.lower() == "cancel":
        await state.clear()
        return await msg.answer("✅ Add number process cancelled.")

    # otherwise assume phone number
    phone = text
    await state.update_data(number=phone)

    api_id = int(os.getenv("API_ID"))
    api_hash = os.getenv("API_HASH")

    session = StringSession()
    client = TelegramClient(session, api_id, api_hash)
    await client.connect()

    try:
        sent = await client.send_code_request(phone)
        await state.update_data(
            session=session.save(),
            phone_code_hash=sent.phone_code_hash
        )
        await msg.answer("📩 Code sent! Enter OTP:")
        await state.set_state(AddSession.waiting_otp)
        await client.disconnect()

    except Exception as e:
        await client.disconnect()
        await msg.answer(f"❌ Failed to send code: {e}")
        
        
class RemoveSession(StatesGroup):
    waiting_country = State()
    waiting_number = State()
#Remove
@dp.message(Command("remove"))
async def cmd_remove_start(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return await msg.answer("❌ Not authorized.")

    countries = list(countries_col.find({}))
    if not countries:
        return await msg.answer("❌ No countries found.")

    kb = InlineKeyboardBuilder()
    for c in countries:
        kb.button(text=plain_country_label(c["name"]), callback_data=f"remove_country:{c['name']}")
    kb.adjust(2)

    await msg.answer("🌍 Select country to remove number from:", reply_markup=kb.as_markup())
    await state.set_state(RemoveSession.waiting_country)

@dp.callback_query(F.data.startswith("remove_country:"))
async def callback_remove_country(cq: CallbackQuery, state: FSMContext):
    await cq.answer()
    _, country = cq.data.split(":", 1)
    await state.update_data(country=country)
    await cq.message.answer(f"📱 Send the phone number to remove from {country}:")
    await state.set_state(RemoveSession.waiting_number)

@dp.message(RemoveSession.waiting_number)
async def remove_number(msg: Message, state: FSMContext):
    data = await state.get_data()
    country = data["country"]
    phone = msg.text.strip()

    doc = numbers_col.find_one({"number": phone, "country": country})
    if not doc:
        await msg.answer("❌ Number not found. Use /remove again.")
        await state.clear()
        return

    numbers_col.delete_one({"_id": doc["_id"]})
    countries_col.update_one(
        {"name": country},
        {"$inc": {"stock": -1}}
    )

    await msg.answer(
        f"✅ Number removed successfully\n\n"
        f"🌍 Country: {country}\n"
        f"📱 Number: <code>{phone}</code>",
        parse_mode="HTML"
    )

    await state.clear()

# ===== Admin Country Commands =====
@dp.message(Command("addcountry"))
async def cmd_add_country(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return await msg.answer("❌ Not authorized.")
    await msg.answer("🌍 Send the country name and price separated by a comma (e.g., India,50):")
    await state.set_state("adding_country")

@dp.message(StateFilter("adding_country"))
async def handle_add_country(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    if "," not in msg.text:
        return await msg.answer("❌ Invalid format. Example: India,50")
    name, price = msg.text.split(",", 1)
    try:
        price = float(price.strip())
    except ValueError:
        return await msg.answer("❌ Invalid price format.")
    countries_col.update_one({"name": name.strip()}, {"$set": {"price": price, "stock": 0}}, upsert=True)
    await msg.answer(f"✅ Country {name.strip()} added/updated with price {price}.")
    await state.clear()

# ================= Admin: Remove Country =================
@dp.message(Command("removecountry"))
async def cmd_remove_country(msg: Message):
    if not is_admin(msg.from_user.id):
        return await msg.answer("❌ Not authorized.")

    countries = list(countries_col.find({}))
    if not countries:
        return await msg.answer("📭 No countries to remove.")

    kb = InlineKeyboardBuilder()
    for c in countries:
        kb.button(text=plain_country_label(c["name"]), callback_data=f"removecountry:{c['name']}")
    kb.adjust(2)
    await msg.answer("🌍 Select a country to remove:", reply_markup=kb.as_markup())

@dp.callback_query(F.data.startswith("removecountry:"))
async def callback_remove_entire_country(cq: CallbackQuery):
    await cq.answer()
    _, country_name = cq.data.split(":", 1)

    result = countries_col.delete_one({"name": country_name})
    if result.deleted_count == 0:
        await cq.message.edit_text(f"❌ Country <b>{country_name}</b> not found.", parse_mode="HTML")
    else:
        await cq.message.edit_text(f"✅ Country <b>{country_name}</b> removed successfully.", parse_mode="HTML")

@dp.message(Command("db"))
async def cmd_db(msg: Message):
    if not is_admin(msg.from_user.id):
        return await msg.answer("❌ Not authorized.")

    countries = list(countries_col.find({}))
    if not countries:
        return await msg.answer("❌ No countries found in DB.")

    text = "📚 <b>Numbers in Database by Country:</b>\n\n"

    for c in countries:
        country_name = c["name"]
        numbers = list(numbers_col.find({"country": country_name}))
        text += f"🌍 <b>{country_name}:</b>\n"
        if numbers:
            for num in numbers:
                text += f"• {num['number']} {'✅' if num.get('used') else ''}\n"
        else:
            text += "No number\n"
        text += "\n"

    await msg.answer(text, parse_mode="HTML")



# --- Admin Command: Set Sell Prices ---

# ====================== SELL ACCOUNT FEATURE (FIXED & FULL) ======================

sell_prices_col = db["sell_prices"]

# --- FSM States ---
class SetPrices(StatesGroup):
    waiting_list = State()

class SellSession(StatesGroup):
    waiting_sell_number = State()
    waiting_sell_otp = State()
    waiting_sell_password = State()


# --- Admin Command: Set Sell Prices ---
@dp.message(Command("setprices"))
async def cmd_set_prices(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return await msg.answer("❌ Not authorized.")
    
    await msg.answer(
        "📋 <b>Send the price list in this format:</b>\n\n"
        "<code>+1 USA - ₹10</code>\n"
        "<code>+91 India - ₹29</code>\n"
        "<code>+232 Sierra Leone - ₹13</code>\n\n"
        "⚠️ <i>Sending a new list will overwrite the old one.</i>",
        parse_mode="HTML"
    )
    await state.set_state(SetPrices.waiting_list)


@dp.message(StateFilter(SetPrices.waiting_list))
async def handle_set_prices(msg: Message, state: FSMContext):
    text = msg.text.strip()
    
    # IMPROVED REGEX EXPLANATION:
    # (\+\d{1,4})  -> Captures country code (e.g., +1, +232)
    # \s+          -> Matches spaces
    # (.*?)        -> Captures ANY text/emoji (Country Name + Flag) non-greedily until the hyphen
    # \s*-\s* -> Matches the hyphen separator
    # ₹?           -> Matches optional Rupee symbol
    # \s* -> Optional space
    # (\d+)        -> Captures the price number
    pattern = re.compile(r"(\+\d{1,4})\s+(.*?)\s*-\s*₹?\s*(\d+)", re.MULTILINE)

    entries = pattern.findall(text)

    # 1. Validation: Don't delete old data if the new list is empty/invalid
    if not entries:
        return await msg.answer(
            "❌ <b>Invalid format detected.</b>\n\n"
            "Make sure you use the format:\n"
            "<code>+Code CountryName - ₹Price</code>\n"
            "Example:\n<code>+232 Sierra Leone - 13</code>", 
            parse_mode="HTML"
        )

    # 2. Database Update: clear old data ONLY after validation passes
    sell_prices_col.delete_many({})
    
    new_data = []
    response_lines = []

    for code, name, price in entries:
        clean_name = name.strip()
        clean_price = int(price)
        
        new_data.append({
            "code": code.strip(),
            "name": clean_name,
            "price": clean_price
        })
        
        response_lines.append(f"{code} {clean_name} - ₹{clean_price}")

    # Bulk insert is faster and safer
    if new_data:
        sell_prices_col.insert_many(new_data)

    # 3. Confirmation
    formatted_list = "\n".join(response_lines)
    await msg.answer(
        f"✅ <b>Price list updated successfully!</b>\n"
        f"<i>Added {len(new_data)} countries.</i>\n\n"
        f"<pre>{formatted_list}</pre>", 
        parse_mode="HTML"
    )
    await state.clear()

# --- Callback for Sell Button ---
# ==========================================
# 💸 SELL ACCOUNT LOGIC (REWRITTEN & FIXED)
# ==========================================

# --- 1. Sell Menu ---
@dp.callback_query(F.data == "sell")
async def callback_sell(cq: CallbackQuery, state: FSMContext):
    await cq.answer()
    prices = list(sell_prices_col.find({}))
    
    if not prices:
        return await cq.message.answer("❌ <b>Sales are currently closed.</b>\nNo price list available.")

    # High UI Price List
    price_list_text = ""
    for p in prices:
        price_list_text += f"🏳️ <code>{p['code']}</code> <b>{p['name']}</b> ➜ ₹{p['price']}\n"

    text = (
        "<b>💸 SELL YOUR TELEGRAM ACCOUNT</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        "<b>📊 Current Buying Rates:</b>\n"
        f"<blockquote expandable>{price_list_text}</blockquote>\n"
        "<b>📝 Instructions:</b>\n"
        "1. Enter your number with country code.\n"
        "2. Send the OTP received.\n"
        "3. If you have a 2FA password, enter it.\n\n"
        "👇 <b>Send your number now:</b>\n"
        "<i>(Example: +14151234567)</i>"
    )

    await cq.message.answer(text, parse_mode="HTML")
    await state.set_state(SellSession.waiting_sell_number)
    
    # --- 2. User Sends Number ---
@dp.message(StateFilter(SellSession.waiting_sell_number))
async def user_sells_number(msg: Message, state: FSMContext):
    phone = msg.text.strip().replace(" ", "")
    
    if not phone.startswith("+") or not phone[1:].isdigit():
        return await msg.answer("❌ <b>Invalid Format!</b>\nPlease start with '+' followed by digits.\n<i>Ex: +14155550199</i>")

    # Match Country and Price
    all_prices = list(sell_prices_col.find({}))
    matched = None
    for p in all_prices:
        if phone.startswith(p["code"]):
            matched = p
            break

    if not matched:
        return await msg.answer("⚠️ <b>Sorry!</b>\nWe are not buying numbers from this country at the moment.")

    country_name = matched["name"]
    price = matched["price"]

    status_msg = await msg.answer(
        f"🌍 <b>Country:</b> {country_name}\n"
        f"💰 <b>Offer Price:</b> ₹{price}\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "🔄 <i>Connecting to Telegram Servers...</i>"
    )

    api_id = int(os.getenv("API_ID"))
    api_hash = os.getenv("API_HASH")

    session = StringSession()
    client = TelegramClient(session, api_id, api_hash)
    
    try:
        await client.connect()
        sent = await client.send_code_request(phone)
        
        # Save session immediately to maintain context
        await state.update_data(
            session=session.save(), # Critical for session continuity
            phone=phone,
            phone_code_hash=sent.phone_code_hash,
            price=price,
            country_name=country_name,
            password_needed=False # Default false
        )
        
        await client.disconnect()
        
        await status_msg.edit_text(
            f"🌍 <b>Country:</b> {country_name}\n"
            f"💰 <b>Offer Price:</b> ₹{price}\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "📩 <b>OTP Sent!</b>\n\n"
            "Please check your Telegram Service notifications or SMS and enter the code below:\n"
            "<i>(Format: 12345)</i>"
        )
        await state.set_state(SellSession.waiting_sell_otp)

    except Exception as e:
        await client.disconnect()
        await status_msg.edit_text(f"❌ <b>Connection Failed:</b>\n<code>{str(e)}</code>")


# --- 3. User Sends OTP ---
@dp.message(StateFilter(SellSession.waiting_sell_otp))
async def user_sells_otp(msg: Message, state: FSMContext):
    otp_code = msg.text.strip()
    
    # Basic validation
    if not otp_code.isdigit():
        return await msg.answer("❌ <b>Invalid OTP.</b> Send numbers only.")

    data = await state.get_data()
    phone = data["phone"]
    session_str = data["session"]
    phone_code_hash = data["phone_code_hash"]

    status_msg = await msg.answer("🔄 <i>Verifying Code...</i>")

    api_id = int(os.getenv("API_ID"))
    api_hash = os.getenv("API_HASH")
    client = TelegramClient(StringSession(session_str), api_id, api_hash)

    try:
        await client.connect()
        
        try:
            # Try logging in
            await client.sign_in(phone=phone, code=otp_code, phone_code_hash=phone_code_hash)
            
            # --- SCENARIO A: Login Successful (No 2FA) ---
            final_string = client.session.save() # CAPTURE FINAL STRING
            await client.disconnect()
            
            await state.update_data(string_session=final_string, password=None)
            
            # Skip password step, go directly to finalize logic
            await finalize_sell(msg, state, phone, final_string, None)
            
        except SessionPasswordNeededError:
            # --- SCENARIO B: 2FA Required ---
            await client.disconnect()
            await state.update_data(password_needed=True)
            await status_msg.delete()
            await msg.answer(
                "🔐 <b>Two-Step Verification Detected</b>\n\n"
                "Please enter your <b>Password</b> to complete the login.\n"
                "<i>We need this to verify the account.</i>"
            )
            await state.set_state(SellSession.waiting_sell_password)

    except PhoneCodeInvalidError:
        await client.disconnect()
        await status_msg.edit_text("❌ <b>Wrong OTP!</b>\nPlease check and send again.")
    except Exception as e:
        await client.disconnect()
        await status_msg.edit_text(f"❌ <b>Error:</b> {e}")
        
        
# --- 4. User Sends Password (If 2FA) ---
@dp.message(StateFilter(SellSession.waiting_sell_password))
async def user_sell_password(msg: Message, state: FSMContext):
    password = msg.text.strip()
    data = await state.get_data()
    
    phone = data["phone"]
    session_str = data["session"] # Use the initial session to resume
    
    status_msg = await msg.answer("🔄 <i>Verifying Password...</i>")

    api_id = int(os.getenv("API_ID"))
    api_hash = os.getenv("API_HASH")
    client = TelegramClient(StringSession(session_str), api_id, api_hash)

    try:
        await client.connect()
        await client.sign_in(password=password)
        
        # --- Login Successful (With 2FA) ---
        final_string = client.session.save() # CAPTURE FINAL STRING
        await client.disconnect()
        
        # Proceed to finalize
        await status_msg.delete()
        await finalize_sell(msg, state, phone, final_string, password)

    except PasswordHashInvalidError:
        await client.disconnect()
        await status_msg.edit_text("❌ <b>Wrong Password!</b>\nPlease try again.")
    except Exception as e:
        await client.disconnect()
        await status_msg.edit_text(f"❌ <b>Error:</b> {e}")


# --- 5. Finalize Sell (Save to DB & Notify Admin) ---
async def finalize_sell(msg: Message, state: FSMContext, phone, string_session, password):
    data = await state.get_data()
    country_name = data["country_name"]
    price = data["price"]
    user_id = msg.from_user.id
    username = msg.from_user.username

    # 1. Update Database
    numbers_col.update_one(
        {"number": phone},
        {
            "$set": {
                "country": country_name,
                "number": phone,
                "string_session": string_session, # The valid authenticated session
                "password": password if password else "None",
                "used": False,
                "added_by": user_id,
                "added_at": datetime.now(timezone.utc)
            }
        },
        upsert=True
    )

    # 2. Notify Admin
    # Using the specific Admin ID provided in your prompt
    ADMIN_CHAT_ID = -1003723243833 

    kb = InlineKeyboardBuilder()
    # Unique callback for selling OTPs
    kb.button(text="📩 Get OTP (Sell)", callback_data=f"get_sell_otp:{phone}")
    kb.button(text=f"✅ Approve ₹{price}", callback_data=f"approve_sell:{user_id}:{phone}:{price}")
    kb.button(text=f"Reject", callback_data=f"reject_sell:{user_id}:{phone}")
    
    kb.adjust(1)

    admin_text = (
        f"<b>📤 NEW ACCOUNT FOR SALE</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>Seller:</b> @{username or 'N/A'} (<code>{user_id}</code>)\n"
        f"🌍 <b>Country:</b> {country_name}\n"
        f"📞 <b>Number:</b> <code>{phone}</code>\n"
        f"💰 <b>Payout:</b> ₹{price}\n"
        f"🔐 <b>2FA Pass:</b> <code>{password if password else 'None'}</code>\n\n"
        f"🔑 <b>Session String:</b>\n"
        f"<blockquote expandable><code>{string_session}</code></blockquote>"
    )

    try:
        await bot.send_message(
            ADMIN_CHAT_ID,
            admin_text,
            reply_markup=kb.as_markup(),
            parse_mode="HTML"
        )
    except Exception as e:
        print(f"Failed to send to admin: {e}")

    # 3. Notify User
    await msg.answer(
        f"✅ <b>Submission Successful!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"📞 <b>Number:</b> {phone}\n"
        f"💰 <b>Pending:</b> ₹{price}\n\n"
        f"<i>Your account is under review. Balance will be credited after admin verification (usually 1-10 mins).</i>"
    )
    
    await state.clear()
    
    # ==========================================
# 📩 DEDICATED SELL OTP LISTENER (FIXED)
# ==========================================

@dp.callback_query(F.data.startswith("get_sell_otp:"))
async def callback_get_sell_otp(cq: CallbackQuery):
    phone = cq.data.split(":")[1]
    
    # 1. Fetch Session from DB
    number_doc = numbers_col.find_one({"number": phone})
    if not number_doc or not number_doc.get("string_session"):
        return await cq.answer("❌ Session not found in Database.", show_alert=True)

    await cq.answer("🔄 Accessing Account...", show_alert=False)
    
    # High UI status message
    status_msg = await cq.message.answer(f"🔍 <b>Searching for OTP on {phone}...</b>")

    api_id = int(os.getenv("API_ID"))
    api_hash = os.getenv("API_HASH")
    string_session = number_doc.get("string_session")
    password_text = number_doc.get("password") or "None"

    client = TelegramClient(StringSession(string_session), api_id, api_hash)
    
    try:
        await client.connect()
        
        if not await client.is_user_authorized():
            await client.disconnect()
            await status_msg.edit_text(f"❌ <b>Session Expired</b>\nAccount {phone} has been logged out.")
            return

        # Use the logic from your working otp_listener
        # Matches any 5-digit number in the message
        pattern = re.compile(r"\b\d{5}\b")
        found_code = None

        # Iterate messages from Telegram Service (777000)
        # Increased limit slightly to ensure we don't miss it
        async for msg in client.iter_messages(777000, limit=15):
            if not msg.message:
                continue

            match = pattern.search(msg.message)
            if match:
                found_code = match.group(0)
                # We stop at the very first (newest) 5-digit code found
                break 
        
        await client.disconnect()

        if found_code:
            # High UI Result Format
            response_text = (
                f"<b>✅ OTP RECEIVED</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"<b>Code -</b> <code>{found_code}</code>\n"
                f"<b>Number -</b> <code>{phone}</code>\n"
                f"<b>Pass -</b> <code>{password_text}</code>\n"
                f"━━━━━━━━━━━━━━━━━━━━━"
            )
            
            await status_msg.delete()
            await bot.send_message(
                chat_id=cq.message.chat.id,
                text=response_text,
                parse_mode="HTML"
            )
        else:
            await status_msg.edit_text(
                f"⚠️ <b>OTP Not Found</b>\n"
                f"No 5-digit code found in the last 15 messages from Telegram on {phone}.\n\n"
                f"<i>Try clicking the button again in a few seconds.</i>"
            )

    except Exception as e:
        if client.is_connected():
            await client.disconnect()
        await status_msg.edit_text(f"❌ <b>Error:</b>\n<code>{str(e)}</code>")


# --- Admin: Get OTP Button ---
@dp.callback_query(F.data.startswith("get_otp:"))
async def callback_get_otp(cq: CallbackQuery):
    phone = cq.data.split(":")[1]

    number_doc = numbers_col.find_one({"number": phone})
    if not number_doc:
        return await cq.answer("❌ Number session not found.", show_alert=True)

    await cq.answer("Waiting for OTP.....")

    # 👇 pass message_id of SAME message
    asyncio.create_task(
        otp_listener(
            number_doc=number_doc,
            user_id=cq.from_user.id,
            message_id=cq.message.message_id
        )
    )
        # --- 2. Admin: Approve Sell ---
@dp.callback_query(F.data.startswith("approve_sell:"))
async def callback_approve_sell(cq: CallbackQuery):
    if not is_admin(cq.from_user.id):
        return await cq.answer("❌ Not authorized.", show_alert=True)

    _, user_id, phone, price = cq.data.split(":")
    user_id, price = int(user_id), int(price)

    # Add Balance
    users_col.update_one({"_id": user_id}, {"$inc": {"balance": price}})

    # Edit Admin Message
    await cq.message.edit_text(
        cq.message.text + f"\n\n✅ <b>Approved by {cq.from_user.first_name}</b>",
        parse_mode="HTML"
    )

    # Notify User with Withdraw Button
    kb = InlineKeyboardBuilder()
    kb.button(text="💸 Withdraw Now", callback_data="init_withdraw")
    
    await bot.send_message(
        user_id,
        f"🎉 <b>Account Approved!</b>\n\n"
        f"✅ Account: <code>{phone}</code>\n"
        f"💰 Added: ₹{price}\n\n"
        f"You can withdraw this amount to your UPI immediately.",
        reply_markup=kb.as_markup(),
        parse_mode="HTML"
    )
    await cq.answer("✅ Approved & Balance Added.")

# --- 3. Admin: Reject Sell ---
@dp.callback_query(F.data.startswith("reject_sell:"))
async def callback_reject_sell(cq: CallbackQuery):
    if not is_admin(cq.from_user.id):
        return await cq.answer("❌ Not authorized.")

    _, user_id, phone = cq.data.split(":")
    user_id = int(user_id)

    await cq.message.edit_text(
        cq.message.text + f"\n\n❌ <b>Rejected by {cq.from_user.first_name}</b>",
        parse_mode="HTML"
    )
    
    await bot.send_message(
        user_id,
        f"⚠️ <b>Account Rejected</b>\n\nYour submission for <code>{phone}</code> was declined by the admin.",
        parse_mode="HTML"
    )
    await cq.answer("❌ Request Rejected.")
    
    
    # --- User clicks Withdraw ---
@dp.callback_query(F.data == "init_withdraw")
async def start_withdraw(cq: CallbackQuery, state: FSMContext):
    user_bal = get_user_balance(cq.from_user.id)
    if user_bal < 1:
        return await cq.answer("❌ Balance too low.", show_alert=True)

    await cq.message.answer(
        "🏦 <b>Withdrawal Setup</b>\n\nPlease enter your <b>UPI ID</b> (e.g., user@oksbi):",
        parse_mode="HTML"
    )
    await state.set_state(WithdrawState.waiting_upi)
    await cq.answer()

# --- User enters UPI ---
@dp.message(StateFilter(WithdrawState.waiting_upi))
async def process_withdraw_upi(msg: Message, state: FSMContext):
    upi_id = msg.text.strip()
    await state.update_data(upi_id=upi_id)
    
    user_bal = get_user_balance(msg.from_user.id)
    
    await msg.answer(
        f"✅ UPI set to: <code>{upi_id}</code>\n\n"
        f"💰 Your Balance: ₹{user_bal}\n"
        f"Enter the amount you want to withdraw:",
        parse_mode="HTML"
    )
    await state.set_state(WithdrawState.waiting_amount)

# --- User enters Amount ---
@dp.message(StateFilter(WithdrawState.waiting_amount))
async def process_withdraw_amount(msg: Message, state: FSMContext):
    try:
        amount = int(msg.text.strip())
    except ValueError:
        return await msg.answer("❌ Please enter a valid number.")

    data = await state.get_data()
    upi_id = data.get('upi_id')
    user_id = msg.from_user.id
    current_bal = get_user_balance(user_id)

    # Validation
    if amount > current_bal:
        return await msg.answer(f"❌ Insufficient funds. Your balance is ₹{current_bal}.")
    if amount < 1:
        return await msg.answer("❌ Minimum withdrawal is ₹1.")

    # Deduct Balance
    users_col.update_one({"_id": user_id}, {"$inc": {"balance": -amount}})
    
    # Save Request to DB
    withdraw_doc = {
        "user_id": user_id,
        "username": msg.from_user.username,
        "amount": amount,
        "upi": upi_id,
        "status": "pending"
    }
    result = withdrawals_col.insert_one(withdraw_doc)
    request_id = str(result.inserted_id)

    # Notify User
    await msg.answer(
        f"✅ <b>Withdrawal Request Submitted!</b>\n"
        f"💸 Amount: ₹{amount}\n"
        f"🏦 UPI: <code>{upi_id}</code>\n\n"
        f"You will receive the funds shortly.",
        parse_mode="HTML"
    )
    await state.clear()

    # Notify Admin Group
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Approve Payment", callback_data=f"pay_wd:{request_id}")
    kb.adjust(1)

    admin_text = (
        f"<b>💸 New Withdrawal Request</b>\n\n"
        f"👤 User: {msg.from_user.full_name} (<code>{user_id}</code>)\n"
        f"💰 Amount: <b>₹{amount}</b>\n"
        f"🏦 UPI: <code>{upi_id}</code>\n"
        f"🆔 Req ID: <code>{request_id}</code>"
    )

    await bot.send_message(
        "-1003723243833", 
        admin_text, 
        reply_markup=kb.as_markup(), 
        parse_mode="HTML"
    )
    
# --- Admin clicks Approve Payment ---
@dp.callback_query(F.data.startswith("pay_wd:"))
async def admin_approve_withdraw(cq: CallbackQuery, state: FSMContext):
    req_id = cq.data.split(":")[1]
    
    # Store request details in FSM to use after getting TXN ID
    await state.update_data(req_id=req_id, message_id=cq.message.message_id, chat_id=cq.message.chat.id)
    
    await cq.message.answer(
        "✍️ <b>Send the Transaction ID (UTR) for this payment:</b>\n"
        "Or type /skip if you don't want to provide one.",
        parse_mode="HTML"
    )
    await state.set_state(AdminTxnState.waiting_txn)
    await cq.answer()

# --- Admin sends TXN ID ---
@dp.message(StateFilter(AdminTxnState.waiting_txn))
async def admin_finalize_withdraw(msg: Message, state: FSMContext):
    txn_id = msg.text.strip()
    data = await state.get_data()
    req_id = data.get('req_id')
    admin_msg_id = data.get('message_id')
    admin_chat_id = data.get('chat_id')

    # Get Request Details
    req_doc = withdrawals_col.find_one({"_id": ObjectId(req_id)})
    if not req_doc:
        await msg.answer("❌ Error: Request not found in DB.")
        return await state.clear()

    # Update DB Status
    withdrawals_col.update_one({"_id": ObjectId(req_id)}, {"$set": {"status": "paid", "txn": txn_id}})

    # 1. Notify User
    user_msg = (
        f"🎉 <b>Withdrawal Approved!</b>\n\n"
        f"💰 Amount: ₹{req_doc['amount']}\n"
        f"🏦 UPI: <code>{req_doc['upi']}</code>\n"
    )
    if txn_id != "/skip":
        user_msg += f"🆔 TXN ID: <code>{txn_id}</code>"
    
    try:
        await bot.send_message(req_doc['user_id'], user_msg, parse_mode="HTML")
    except:
        pass # User might have blocked bot

    # 2. Update Admin Group Message (Strikethrough)
    original_text = (
        f"<b>💸 New Withdrawal Request</b>\n\n"
        f"👤 User: {req_doc['username']} (<code>{req_doc['user_id']}</code>)\n"
        f"💰 Amount: <b>₹{req_doc['amount']}</b>\n"
        f"🏦 UPI: <code>{req_doc['upi']}</code>\n"
        f"🆔 Req ID: <code>{req_id}</code>"
    )

    strikethrough_text = f"<s>{original_text}</s>\n\n✅ <b>PAID by {msg.from_user.first_name}</b>"
    if txn_id != "/skip":
        strikethrough_text += f"\n🆔 Ref: {txn_id}"

    try:
        await bot.edit_message_text(
            chat_id=admin_chat_id,
            message_id=admin_msg_id,
            text=strikethrough_text,
            parse_mode="HTML"
        )
    except Exception as e:
        await msg.answer(f"⚠️ Could not edit original message: {e}")

    await msg.answer("✅ Withdrawal marked as paid.")
    await state.clear()


    
# ================= ADMIN STORE MANAGER =================
async def render_store_dashboard(target: Message, edit: bool = False):
    features = get_feature_settings()
    store_on = features.get("store_enabled", True)
    total_prods = store_col.count_documents({})
    in_stock_prods = store_col.count_documents({"stock": {"$gt": 0}})

    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="➕ Add Product", callback_data="store_add"),
        InlineKeyboardButton(text="🗑️ Remove Product", callback_data="store_remove"),
    )
    kb.row(
        InlineKeyboardButton(text="📦 View All Products", callback_data="store_view_all"),
        InlineKeyboardButton(
            text=f"🏪 Store: {'🟢 ON' if store_on else '🔴 OFF'}",
            callback_data="store_toggle",
        ),
    )
    kb.row(
        InlineKeyboardButton(text="⬅️ Admin Panel", callback_data="admin_refresh"),
        InlineKeyboardButton(text="❌ Close", callback_data="delete_msg"),
    )

    text = (
        "<h2>🏪 Digital Store Management</h2>"
        f"<blockquote>🔘 <b>Store Status:</b> <code>{'ONLINE (Visible)' if store_on else 'OFFLINE (Hidden)'}</code><br>"
        f"📦 <b>Total Products:</b> <code>{total_prods}</code><br>"
        f"✅ <b>In-Stock Items:</b> <code>{in_stock_prods}</code></blockquote>"
        "<p>⚙️ <i>Manage digital products, stock levels, and store visibility below:</i></p>"
    )
    if edit:
        await target.edit_text(text, reply_markup=kb.as_markup())
    else:
        await target.answer(text, reply_markup=kb.as_markup())


@dp.message(Command("managestore"))
async def cmd_managestore(msg: Message):
    if not is_admin(msg.from_user.id):
        return await msg.answer("❌ Not authorized.")
    await render_store_dashboard(msg, edit=False)


@dp.callback_query(F.data == "store_toggle")
async def toggle_store(cq: CallbackQuery):
    if not is_admin(cq.from_user.id):
        return await cq.answer("Access denied", show_alert=True)
    features = get_feature_settings()
    new_enabled = not features.get("store_enabled", True)
    new_status = "on" if new_enabled else "off"
    settings_col.update_one({"_id": "bot_features"}, {"$set": {"store_enabled": new_enabled}}, upsert=True)
    settings_col.update_one({"_id": "server_config"}, {"$set": {"store_status": new_status}}, upsert=True)

    await cq.answer(f"Store turned {'ON' if new_enabled else 'OFF'}", show_alert=True)
    await render_store_dashboard(cq.message, edit=True)


@dp.callback_query(F.data == "store_view_all")
async def store_view_all(cq: CallbackQuery):
    if not is_admin(cq.from_user.id):
        return await cq.answer("Access denied", show_alert=True)
    products = list(store_col.find({}))
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="➕ Add Product", callback_data="store_add"),
            InlineKeyboardButton(text="🗑️ Remove Product", callback_data="store_remove"),
        ],
        [InlineKeyboardButton(text="⬅️ Back to Store Control", callback_data="admin_open:store")],
    ])
    if not products:
        return await cq.message.edit_text(
            "<h2>📦 Store Inventory</h2><blockquote>📭 No digital products in the store yet.</blockquote>",
            reply_markup=kb,
        )
    lines = []
    for p in products:
        cat_label = "Bot Source" if p.get("category") == "source_codes" else "OTP Panel"
        lines.append(
            f"• <b>{escape(str(p.get('name', 'Item')))}</b> (<i>{cat_label}</i>) — "
            f"<code>₹{p.get('price', 0)}</code> · Stock: <code>{p.get('stock', 0)}</code>"
        )
    await cq.message.edit_text(
        f"<h2>📦 Store Inventory ({len(products)})</h2>"
        f"<blockquote>{'<br>'.join(lines)}</blockquote>",
        reply_markup=kb,
    )
    await cq.answer()

@dp.callback_query(F.data == "store_add")
async def store_add_start(cq: CallbackQuery, state: FSMContext):
    kb = InlineKeyboardBuilder()
    kb.button(text="🤖 1. Bot Source Codes", callback_data="st_cat:source_codes")
    kb.button(text="🚀 2. Buy OTP Panels", callback_data="st_cat:otp_panels")
    kb.button(text="⬅️ Back to Store Control", callback_data="admin_open:store")
    kb.adjust(1)
    await cq.message.edit_text(
        "<h2>➕ Add New Store Product</h2>"
        "<blockquote>📂 <b>Step 1:</b> Select a category for the new digital product:</blockquote>",
        reply_markup=kb.as_markup(),
    )
    await state.set_state(StoreAdmin.waiting_category)

@dp.callback_query(F.data.startswith("st_cat:"))
async def store_add_cat(cq: CallbackQuery, state: FSMContext):
    cat = cq.data.split(":")[1]
    await state.update_data(category=cat)
    await cq.message.edit_text(
        "<h2>📝 Product Name</h2>"
        "<blockquote>Enter the product title:<br><i>Example: VIP Telegram Bot Source</i></blockquote>"
    )
    await state.set_state(StoreAdmin.waiting_name)

@dp.message(StateFilter(StoreAdmin.waiting_name))
async def store_add_name(msg: Message, state: FSMContext):
    await state.update_data(name=msg.text)
    
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⏭️ Skip File", callback_data="skip_file")]])
    await msg.answer(
        "<h2>📁 Upload Product File</h2>"
        "<blockquote>Upload the ZIP/Document file for delivery, or tap <b>Skip File</b> if this item is text/link only.</blockquote>",
        reply_markup=kb,
    )
    await state.set_state(StoreAdmin.waiting_file)

@dp.message(StateFilter(StoreAdmin.waiting_file), F.document)
async def store_add_file(msg: Message, state: FSMContext):
    await state.update_data(file_id=msg.document.file_id)
    await msg.answer("<h2>💬 Product Description</h2><blockquote>Enter the product description or delivery instructions:</blockquote>")
    await state.set_state(StoreAdmin.waiting_text)

@dp.callback_query(F.data == "skip_file")
async def store_skip_file(cq: CallbackQuery, state: FSMContext):
    await state.update_data(file_id=None)
    await cq.message.edit_text("<h2>💬 Product Description</h2><blockquote>Enter the product description or delivery instructions:</blockquote>")
    await state.set_state(StoreAdmin.waiting_text)

@dp.message(StateFilter(StoreAdmin.waiting_text))
async def store_add_text(msg: Message, state: FSMContext):
    await state.update_data(text=msg.html_text)
    await msg.answer(
        "<h2>⏳ Auto-Delete Timer</h2>"
        "<blockquote>Enter auto-delete time in minutes:<br><i>Example: <code>30</code> (delivery message deletes 30 mins after purchase)</i></blockquote>"
    )
    await state.set_state(StoreAdmin.waiting_time)

@dp.message(StateFilter(StoreAdmin.waiting_time))
async def store_add_time(msg: Message, state: FSMContext):
    if not msg.text.isdigit(): return await msg.answer("❌ Enter a valid number.")
    await state.update_data(del_time=int(msg.text))
    await msg.answer("<h2>📦 Initial Stock</h2><blockquote>Enter available quantity (e.g. <code>10</code>):</blockquote>")
    await state.set_state(StoreAdmin.waiting_stock)

@dp.message(StateFilter(StoreAdmin.waiting_stock))
async def store_add_stock(msg: Message, state: FSMContext):
    if not msg.text.isdigit(): return await msg.answer("❌ Enter a valid number.")
    await state.update_data(stock=int(msg.text))
    await msg.answer("<h2>💰 Product Price (INR)</h2><blockquote>Enter the price in ₹ (e.g. <code>500</code>):</blockquote>")
    await state.set_state(StoreAdmin.waiting_price)

@dp.message(StateFilter(StoreAdmin.waiting_price))
async def store_add_price(msg: Message, state: FSMContext):
    try: price = float(msg.text)
    except: return await msg.answer("❌ Invalid price.")
    
    data = await state.get_data()
    store_col.insert_one({
        "category": data["category"],
        "name": data["name"],
        "file_id": data.get("file_id"),
        "text": data["text"],
        "del_time": data["del_time"],
        "stock": data["stock"],
        "price": price,
        "created_at": datetime.now(timezone.utc)
    })
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏪 Back to Store Control", callback_data="admin_open:store")]
    ])
    await msg.answer(
        "<h2>✅ Product Added Successfully</h2>"
        f"<blockquote>📌 <b>Name:</b> {escape(data['name'])}<br>"
        f"💰 <b>Price:</b> <code>₹{price:.2f}</code><br>"
        f"📦 <b>Stock:</b> <code>{data['stock']}</code></blockquote>",
        reply_markup=kb,
    )
    await state.clear()

# ================= USER STORE FLOW =================
@dp.callback_query(F.data == "store_menu")
async def user_store_menu(cq: CallbackQuery):
    features = get_feature_settings()
    settings = settings_col.find_one({"_id": "server_config"}) or {}
    if not features.get("store_enabled", True) or settings.get("store_status", "on") == "off":
        return await cq.answer("⚠️ Digital Store is currently OFF. Please try again later.", show_alert=True)

    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="🤖 Bot Source Codes", callback_data="usr_cat:source_codes"))
    kb.row(InlineKeyboardButton(text="🚀 Buy OTP Panels", callback_data="usr_cat:otp_panels"))
    kb.row(InlineKeyboardButton(text="⬅️ Back to Menu", callback_data="back_main"))

    await cq.message.edit_text(
        "<h2>🏪 TGA Digital Store</h2>"
        "<blockquote>✨ <b>Instant Automated Digital Delivery</b><br>"
        "Browse verified bot source codes, turnkey OTP panels, and premium scripts with instant delivery.</blockquote>"
        "<p>👇 <i>Select a category below to explore products:</i></p>",
        reply_markup=kb.as_markup(),
    )

@dp.callback_query(F.data.startswith("usr_cat:"))
async def view_category(cq: CallbackQuery):
    cat = cq.data.split(":")[1]
    products = list(store_col.find({"category": cat, "stock": {"$gt": 0}}))
    
    if not products:
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Back to Store", callback_data="store_menu")]])
        return await cq.message.edit_text(
            "<h2>📭 Category Empty</h2>"
            "<blockquote>No products are currently in stock in this category. Please check back soon!</blockquote>",
            reply_markup=kb,
        )

    disc = get_user_discount(cq.from_user.id)
    kb = InlineKeyboardBuilder()
    for p in products:
        p_price = apply_user_discount(p['price'], cq.from_user.id)
        kb.button(text=f"🛍️ {p['name']} (₹{p_price:g})", callback_data=f"buy_prod:{str(p['_id'])}")
    kb.adjust(1)
    kb.row(InlineKeyboardButton(text="⬅️ Back to Store", callback_data="store_menu"))

    cat_name = "Bot Source Codes" if cat == "source_codes" else "OTP Panels"
    disc_line = f"<br>🏷️ <b>VIP Discount Active:</b> <code>{disc:g}% OFF</code>" if disc > 0 else ""
    await cq.message.edit_text(
        f"<h2>📂 {cat_name}</h2>"
        f"<blockquote>📦 <b>Available Products:</b> <code>{len(products)}</code>{disc_line}</blockquote>"
        "<p>👇 <i>Select a product below to view details &amp; purchase:</i></p>",
        reply_markup=kb.as_markup(),
    )

# Helper to delete message automatically
async def auto_delete_msg(chat_id, msg_id, minutes):
    await asyncio.sleep(minutes * 60)
    try:
        await bot.delete_message(chat_id, msg_id)
    except: pass

# --- 1. CONFIRMATION STEP (Are you sure?) ---
@dp.callback_query(F.data.startswith("buy_prod:"))
async def prompt_buy_product(cq: CallbackQuery):
    prod_id = cq.data.split(":")[1]
    product = store_col.find_one({"_id": ObjectId(prod_id)})
    
    if not product or product["stock"] < 1:
        return await cq.answer("❌ This product is Out of Stock!", show_alert=True)

    price = apply_user_discount(product["price"], cq.from_user.id)
    disc = get_user_discount(cq.from_user.id)
    disc_line = f"<br>🏷️ <b>VIP Discount:</b> <code>{disc:g}% OFF</code>" if disc > 0 else ""
    
    text = (
        "<h2>🛍️ Confirm Store Purchase</h2>"
        f"<blockquote>📌 <b>Product:</b> {escape(str(product['name']))}<br>"
        f"💰 <b>Price:</b> <code>₹{price:g}</code>{disc_line}<br>"
        f"📦 <b>Stock Remaining:</b> <code>{product['stock']}</code><br>"
        f"⏳ <b>Auto-Delete Window:</b> <code>{product.get('del_time', 30)} mins</code></blockquote>"
        "<p>❓ <i>Are you sure you want to purchase this digital product?</i></p>"
    )
    
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="✅ Confirm Purchase", callback_data=f"confirm_buy_prod:{prod_id}"))
    kb.row(InlineKeyboardButton(text="❌ Cancel", callback_data=f"usr_cat:{product['category']}"))
    
    await cq.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())


# --- 2. EXECUTE PURCHASE (Split Delivery & Logs) ---
@dp.callback_query(F.data.startswith("confirm_buy_prod:"))
async def execute_buy_product(cq: CallbackQuery):
    prod_id = cq.data.split(":")[1]
    product = store_col.find_one({"_id": ObjectId(prod_id)})
    
    if not product or product["stock"] < 1:
        return await cq.answer("❌ Too late! This product is Out of Stock.", show_alert=True)

    user_id = cq.from_user.id
    user = get_or_create_user(user_id, cq.from_user.username)
    price = apply_user_discount(product["price"], user)

    # Balance Check
    if user.get("balance", 0.0) < price:
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💳 Recharge", callback_data="recharge")]])
        return await cq.message.answer("❌ <b>Insufficient Balance!</b> Please deposit first.", reply_markup=kb)

    # Deduct Balance & Update Stock
    new_balance = user["balance"] - price
    users_col.update_one({"_id": user_id}, {"$set": {"balance": new_balance}})
    store_col.update_one({"_id": ObjectId(prod_id)}, {"$inc": {"stock": -1}})

    await cq.message.edit_text("✅ <b>Purchase Successful! Sending your product...</b>", parse_mode="HTML")
    
    # Deliver the Product (File and Text Separately)
    del_time = product["del_time"]
    delivery_text = (
        f"🎉 <b>Thank you for purchasing!</b>\n\n"
        f"📌 <b>Product:</b> {product['name']}\n"
        f"💬 <b>Details:</b>\n{product['text']}\n\n"
        f"⚠️ <i>Note: This message will be auto-deleted in {del_time} minutes to protect source leaks. Save it fast!</i>"
    )
    
    if product.get("file_id"):
        # Send File First
        file_msg = await bot.send_document(user_id, document=product["file_id"])
        # Then Send Text
        text_msg = await bot.send_message(user_id, text=delivery_text)
        
        # Trigger Auto-Delete for BOTH messages
        asyncio.create_task(auto_delete_msg(user_id, file_msg.message_id, del_time))
        asyncio.create_task(auto_delete_msg(user_id, text_msg.message_id, del_time))
    else:
        # Only Text
        text_msg = await bot.send_message(user_id, text=delivery_text)
        asyncio.create_task(auto_delete_msg(user_id, text_msg.message_id, del_time))

    # Logs Setup
    masked_user_id = str(user_id)[:4] + "****"
    
    # 3. Public Channel Log (Masked ID)
    public_log = (
        f"<pre><u>✅ <b>New Store Purchase</b></u></pre>\n\n"
        f"🛒 <b>Product:</b> {product['name']}\n"
        f"👤 <b>Buyer ID:</b> <code>{masked_user_id}</code>\n"
        f"💰 <b>Price:</b> ₹{price}\n\n"
        f"<b>• @{BOTUSER} || @{CHANNEL}</b>"
    )
    
    # 4. Private Admin Log (Full Details)
    admin_log = (
        f"📢 <b>STORE SALE ALERT</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🛒 <b>Product:</b> {product['name']}\n"
        f"📦 <b>Category:</b> {product.get('category', 'N/A')}\n"
        f"💰 <b>Price Paid:</b> ₹{price}\n\n"
        f"👤 <b>User ID:</b> <code>{user_id}</code>\n"
        f"🗣️ <b>Username:</b> @{cq.from_user.username or 'N/A'}\n"
        f"💵 <b>User Balance Left:</b> ₹{new_balance:.2f}\n"
        f"⏱️ <b>Auto-Delete Set:</b> {del_time} mins\n"
    )
    
    try:
        await bot.send_message(SALES, public_log, parse_mode="HTML")
        await bot.send_message(ADMINLOG, admin_log, parse_mode="HTML")
    except: 
        pass

# ================= ADMIN: REMOVE PRODUCT FLOW =================

@dp.callback_query(F.data == "store_remove")
async def store_remove_start(cq: CallbackQuery, state: FSMContext):
    if not is_admin(cq.from_user.id): return
    
    kb = InlineKeyboardBuilder()
    kb.button(text="🤖 Bot Source Codes", callback_data="rem_cat:source_codes")
    kb.button(text="🚀 Buy OTP Panels", callback_data="rem_cat:otp_panels")
    kb.row(InlineKeyboardButton(text="⬅️ Back to Store Control", callback_data="admin_open:store"))
    kb.adjust(1)
    
    await cq.message.edit_text(
        "<h2>🗑️ Remove Store Product</h2>"
        "<blockquote>📂 Select a category to view and delete products from:</blockquote>",
        reply_markup=kb.as_markup(),
    )

@dp.callback_query(F.data.startswith("rem_cat:"))
async def store_remove_list(cq: CallbackQuery):
    if not is_admin(cq.from_user.id): return
    cat = cq.data.split(":")[1]
    
    # DB se category ke saare products nikalo
    products = list(store_col.find({"category": cat}))
    
    if not products:
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Back", callback_data="store_remove")]])
        return await cq.message.edit_text(
            "<h2>📭 Category Empty</h2><blockquote>No products found in this category!</blockquote>",
            reply_markup=kb,
        )

    kb = InlineKeyboardBuilder()
    for p in products:
        # Delete button with Product ID
        kb.button(
            text=f"❌ Delete: {p['name']} (₹{p['price']})", 
            callback_data=f"del_prod:{str(p['_id'])}"
        )
    kb.adjust(1)
    kb.row(InlineKeyboardButton(text="⬅️ Back", callback_data="store_remove"))

    cat_name = "Bot Source Codes" if cat == "source_codes" else "OTP Panels"
    await cq.message.edit_text(
        f"<h2>🗑️ Delete Products — {cat_name}</h2>"
        "<blockquote>⚠️ Tap any product below to permanently remove it from the store:</blockquote>", 
        reply_markup=kb.as_markup()
    )

@dp.callback_query(F.data.startswith("del_prod:"))
async def store_delete_execute(cq: CallbackQuery):
    if not is_admin(cq.from_user.id): return
    prod_id = cq.data.split(":")[1]
    
    # Product ko dhundo delete karne se pehle (taaki naam dikha sakein)
    product = store_col.find_one({"_id": ObjectId(prod_id)})
    
    if not product:
        return await cq.answer("❌ Product already deleted or not found!", show_alert=True)
    
    # Database se delete karo
    store_col.delete_one({"_id": ObjectId(prod_id)})
    
    await cq.answer(f"✅ Deleted: {product['name']}", show_alert=True)
    
    # List ko auto-refresh karo taaki bache hue products dikhein
    await store_remove_list(
        types.CallbackQuery(
            id=cq.id, 
            from_user=cq.from_user, 
            chat_instance=cq.chat_instance, 
            message=cq.message, 
            data=f"rem_cat:{product['category']}"
        )
    )


#============== Admin: Edit Country =================
class EditCountry(StatesGroup):
    waiting_new_name = State()
    waiting_new_price = State()

@dp.message(Command("editcountry"))
async def cmd_edit_country(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return await msg.answer("❌ Not authorized.")
    countries = list(countries_col.find({}))
    if not countries:
        return await msg.answer("📭 No countries to edit.")
    kb = InlineKeyboardBuilder()
    for c in countries:
        kb.button(text=plain_country_label(c["name"]), callback_data=f"editcountry:{c['name']}")
    kb.adjust(2)
    await msg.answer("🌍 Select a country to edit:", reply_markup=kb.as_markup())

@dp.callback_query(F.data.startswith("editcountry:"))
async def callback_edit_country(cq: CallbackQuery, state: FSMContext):
    await cq.answer()
    _, country_name = cq.data.split(":", 1)
    country = countries_col.find_one({"name": country_name})
    if not country:
        return await cq.message.edit_text(f"❌ Country {country_name} not found.")

    await state.update_data(country_name=country_name)

    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="✏️ Change Name", callback_data="editcountry_change_name"),
        InlineKeyboardButton(text="💰 Change Price", callback_data="editcountry_change_price")
    )
    kb.row(InlineKeyboardButton(text="❌ Cancel", callback_data="editcountry_cancel"))
    await cq.message.edit_text(
        f"🛠️ Editing Country: <b>{country_name}</b>\n"
        f"💸 Current Price: ₹{country['price']}",
        parse_mode="HTML",
        reply_markup=kb.as_markup()
    )

@dp.callback_query(F.data == "editcountry_change_name")
async def callback_edit_change_name(cq: CallbackQuery, state: FSMContext):
    await cq.answer()
    data = await state.get_data()
    country_name = data.get("country_name")
    await cq.message.answer(f"✏️ Send new name for <b>{country_name}</b>:", parse_mode="HTML")
    await state.set_state(EditCountry.waiting_new_name)

@dp.message(StateFilter(EditCountry.waiting_new_name))
async def handle_new_country_name(msg: Message, state: FSMContext):
    data = await state.get_data()
    old_name = data.get("country_name")
    new_name = msg.text.strip()

    countries_col.update_one({"name": old_name}, {"$set": {"name": new_name}})
    numbers_col.update_many({"country": old_name}, {"$set": {"country": new_name}})
    await msg.answer(f"✅ Country name changed from <b>{old_name}</b> → <b>{new_name}</b>", parse_mode="HTML")
    await state.clear()

@dp.callback_query(F.data == "editcountry_change_price")
async def callback_edit_change_price(cq: CallbackQuery, state: FSMContext):
    await cq.answer()
    data = await state.get_data()
    country_name = data.get("country_name")
    await cq.message.answer(f"💰 Send new price for <b>{country_name}</b>:", parse_mode="HTML")
    await state.set_state(EditCountry.waiting_new_price)

@dp.message(StateFilter(EditCountry.waiting_new_price))
async def handle_new_country_price(msg: Message, state: FSMContext):
    data = await state.get_data()
    country_name = data.get("country_name")
    try:
        price = float(msg.text.strip())
        if price <= 0:
            raise ValueError
    except ValueError:
        return await msg.answer("❌ Invalid price format. Please send a valid number.")

    countries_col.update_one({"name": country_name}, {"$set": {"price": price}})
    await msg.answer(f"✅ Updated price for <b>{country_name}</b> to ₹{price:.2f}", parse_mode="HTML")
    await state.clear()

@dp.callback_query(F.data == "editcountry_cancel")
async def callback_edit_cancel(cq: CallbackQuery, state: FSMContext):
    await cq.answer("❌ Cancelled")
    await state.clear()
    await cq.message.edit_text("❌ Edit cancelled.")


@dp.callback_query(F.data == "stats")
async def callback_stats(cq: CallbackQuery):
    user = users_col.find_one({"_id": cq.from_user.id})
    if not user:
        user = get_or_create_user(cq.from_user.id, cq.from_user.username)
    disc = get_user_discount(user)
    disc_line = f"<br><br>🏷️ <b>VIP Discount:</b> <code>{disc:g}% OFF</code>" if disc > 0 else ""
    currency_code = str(user.get("currency", "INR") or "INR").upper()
    currency_label = "USD ($)" if currency_code == "USD" else "INR (₹)"
    # Owner-requested Account layout: every field on its own line with a
    # blank line between fields — never crammed onto one running line.
    steps_text = (
        f"<h2>{tg_icon()} TGA Store | API</h2>"
        "<h3>👤 Account</h3>"
        f"<blockquote>👤 <b>Name:</b> {html.escape(cq.from_user.full_name)}<br><br>"
        f"💻 <b>Username:</b> @{cq.from_user.username if cq.from_user.username else 'N/A'}<br><br>"
        f"🆔 <b>User ID:</b> <code>{cq.from_user.id}</code><br><br>"
        f"💰 <b>Your Balance:</b> <code>{format_balance(user)}</code><br><br>"
        f"💱 <b>Currency:</b> {currency_label}{disc_line}</blockquote>"
        f"<blockquote>{tg_icon()} <b>Bot:</b> @{BOTUSER}<br><br>"
        f"📈 <b>Sales Log:</b> @{SALESLOG}</blockquote>"
    )

    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="💳 Recharge", callback_data="recharge"),
        InlineKeyboardButton(text="📂 Purchase History", callback_data="history"),
    )
    kb.row(
        InlineKeyboardButton(text="👨‍💻 Support", url=f"https://t.me/{OWNER}"),
        InlineKeyboardButton(text="📖 How to Use", url=f"https://t.me/{UPDATES}"),
    )
    kb.row(
        InlineKeyboardButton(text="⬅️ Back to Menu", callback_data="back_main")
    )
    
    await cq.message.edit_text(steps_text, parse_mode="HTML", reply_markup=kb.as_markup())
    await cq.answer()

@dp.callback_query(F.data == "howto")
async def callback_howto(cq: CallbackQuery):
    await cq.answer()
    steps_text = (
        f"<h2>📖 Help &amp; Official Links</h2>"
        f"<blockquote>🔗 <b>Guides &amp; Updates:</b> @{UPDATES}<br>"
        f"👨‍💻 <b>Official Support:</b> @{SUPPORT}<br>"
        f"{tg_icon()} <b>Official Bot:</b> @{BOTUSER}</blockquote>"
        "<p>🛟 <i>Feel free to reach out anytime if you need assistance!</i></p>"
    )
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="👨‍💻 Support", url=f"https://t.me/{SUPPORT}"),
        InlineKeyboardButton(text="📖 Guides Channel", url=f"https://t.me/{UPDATES}")
    )
    kb.row(InlineKeyboardButton(text="⬅️ Back to Menu", callback_data="back_main")) 
    
    await cq.message.edit_text(steps_text, parse_mode="HTML", reply_markup=kb.as_markup())


@dp.callback_query(lambda c: c.data == "refer")
async def callback_refer(cq: CallbackQuery):
    user = get_or_create_user(cq.from_user.id, cq.from_user.username)

    bot_username = (await bot.get_me()).username
    refer_link = f"https://t.me/{bot_username}?start=ref{cq.from_user.id}"

    total_referred = users_col.count_documents({"referred_by": cq.from_user.id})
    total_earned = user.get("referral_earnings", 0.0)
    features = get_feature_settings()
    try:
        refer_bonus = float(features.get("refer_bonus", 0.1) or 0.0)
    except (TypeError, ValueError):
        refer_bonus = 0.1
    try:
        refer_comm = float(features.get("refer_commission_percent", 2.0) or 0.0)
    except (TypeError, ValueError):
        refer_comm = 2.0

    text = (
        "<h2>🎁 Refer &amp; Earn Program</h2>"
        "<blockquote>💰 <b>Dual Referral Rewards</b><br>"
        f"• 🎁 <b>Instant Join Bonus:</b> <code>₹{refer_bonus:.2f}</code> per verified referral<br>"
        f"• 💎 <b>Referral Commission:</b> <code>{refer_comm:g}%</code> of every deposit made by friends you referred — paid to you, not to the depositor!<br>"
        "Rewards are credited directly to your wallet automatically.</blockquote>"
        "<blockquote>📊 <b>Your Referral Statistics</b><br>"
        f"👥 <b>Total Referred:</b> <code>{total_referred}</code><br>"
        f"💵 <b>Total Earned:</b> <code>₹{total_earned:.2f}</code></blockquote>"
        f"<blockquote>🔗 <b>Your Referral Link:</b><br><code>{refer_link}</code></blockquote>"
    )

    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(
            text="📤 Share Invite Link",
            url=f"https://t.me/share/url?url={refer_link}&text=Join%20TGA%20Store%20and%20get%20instant%20accounts!"
        ),
        InlineKeyboardButton(
            text="📋 Copy Link",
            copy_text=CopyTextButton(text=refer_link),
        ),
    )
    kb.row(InlineKeyboardButton(text="⬅️ Back to Menu", callback_data="back_main"))

    await cq.message.edit_text(text=text, parse_mode="HTML", reply_markup=kb.as_markup())
    await cq.answer()
    
# ================= Admin Analytics & Bot Health =================
def format_uptime(started_at) -> str:
    started = utc_naive(started_at)
    if started is None:
        return "Unknown"
    seconds = max(0, int((datetime.utcnow() - started).total_seconds()))
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, _ = divmod(remainder, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


async def get_live_bot_status() -> dict:
    """Report process, Telegram API, and SMM checker health without hardcoding status."""
    task = SMM_BACKGROUND_TASK
    if task is None:
        checker_status = "Not started"
    elif task.done():
        checker_status = "Stopped"
    else:
        checker_status = "Starting"

    checker_last_run = None
    checker_error = ""
    try:
        smm_config = db["smm_settings"].find_one({"_id": "config"}) or {}
        checker_last_run = utc_naive(smm_config.get("checker_last_run"))
        checker_error = str(smm_config.get("checker_last_error") or "")[:240]
        if task is not None and not task.done():
            if checker_last_run is None:
                checker_status = "Starting"
            else:
                age = max(0, int((datetime.utcnow() - checker_last_run).total_seconds()))
                checker_status = "Running" if age <= 300 else "Stale"
    except Exception as exc:
        logger.exception("Unable to read SMM checker health")
        checker_error = str(exc)[:240]

    telegram_status = "Health check unavailable"
    try:
        me = await asyncio.wait_for(bot.get_me(), timeout=5)
        telegram_status = f"Reachable as @{me.username}" if me.username else "Reachable"
    except Exception as exc:
        logger.warning("Telegram getMe health check failed: %s", exc)
        telegram_status = "API check failed; bot process is still responding"

    # OTP background worker + catalog cache info (best-effort)
    otp_worker_status = "Unknown"
    otp_last_warm = None
    otp_catalog_age = None
    otp_catalog_count = 0
    try:
        # Try to find OTP worker task if registered globally
        otp_catalog_count = db["otp_catalog"].count_documents({}) if "db" in globals() else 0
        # Find most recent catalog fetched_at
        try:
            latest = db["otp_catalog"].find_one(sort=[("fetched_at", -1)]) if "db" in globals() else None
            if latest and latest.get("fetched_at"):
                otp_last_warm = utc_naive(latest.get("fetched_at"))
                otp_catalog_age = (datetime.utcnow() - otp_last_warm).total_seconds() if otp_last_warm else None
        except Exception:
            pass
        otp_worker_status = "Running" if SMM_BACKGROUND_TASK else "Not checked"
    except Exception:
        pass

    # Provider balance (cached only, no new slow call)
    provider_balance = None
    try:
        # Check if otp_catalog has cached balance? We don't have it; show not checked
        provider_balance = "not checked"
    except Exception:
        provider_balance = "not checked"

    # DB collection counts (best-effort, fast)
    db_counts = {}
    try:
        for coll in ["users", "otp_orders", "otp_catalog", "smm_orders"]:
            try:
                db_counts[coll] = db[coll].count_documents({}) if "db" in globals() else 0
            except Exception:
                db_counts[coll] = 0
    except Exception:
        pass

    return {
        "process": "Running",
        "telegram": telegram_status,
        "uptime": format_uptime(BOT_STARTED_AT),
        "started_at": BOT_STARTED_AT.astimezone(timezone.utc).strftime("%d %b %Y, %H:%M UTC"),
        "checker": checker_status,
        "checker_last_run": checker_last_run,
        "checker_error": checker_error,
        "otp_worker": otp_worker_status,
        "otp_last_warm": otp_last_warm,
        "otp_catalog_age": otp_catalog_age,
        "otp_catalog_count": otp_catalog_count,
        "provider_balance": provider_balance,
        "db_counts": db_counts,
    }


def render_bot_health(health: dict) -> str:
    last_run = health.get("checker_last_run")
    last_run_text = last_run.strftime("%d %b %Y, %H:%M:%S UTC") if last_run else "No completed cycle yet"
    error_line = f"<br>⚠️ <b>Last checker error:</b> <code>{escape(health['checker_error'])}</code>" if health.get("checker_error") else ""
    return (
        "<h2>🤖 Bot Health &amp; Runtime Status</h2>"
        "<blockquote>🟢 <b>Bot process:</b> <code>Running</code><br>"
        f"📡 <b>Telegram API:</b> <code>{escape(health.get('telegram', 'Unknown'))}</code><br>"
        f"⏱️ <b>Uptime:</b> <code>{escape(health.get('uptime', 'Unknown'))}</code><br>"
        f"🕒 <b>Started:</b> <code>{escape(health.get('started_at', 'Unknown'))}</code></blockquote>"
        "<blockquote>🔄 <b>SMM order monitor:</b> <code>{}</code><br>"
        "📊 <b>Last checker cycle:</b> <code>{}</code>{}</blockquote>"
    ).format(
        escape(str(health.get("checker", "Unknown"))),
        escape(last_run_text),
        error_line,
    )


def _trend_arrow(delta: float) -> str:
    if delta > 0.5:
        return "📈"
    if delta < -0.5:
        return "📉"
    return "➖"

def _status_dot(status: str) -> str:
    s = str(status or "").lower()
    if "running" in s or "reachable" in s:
        return "🟢"
    if "stale" in s or "starting" in s:
        return "⚪"
    return "🔴"

def _fmt_money(v: float) -> str:
    try:
        return f"₹{float(v):,.2f}"
    except Exception:
        return "₹0.00"

def _fmt_pct(v: float) -> str:
    try:
        return f"{float(v):.1f}%"
    except Exception:
        return "0.0%"

async def build_admin_analytics_report() -> str:
    now = datetime.utcnow()
    now_ist = now + timedelta(hours=5, minutes=30)
    start_of_week = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=now.weekday())
    # Defensive: each collection read is best-effort, empty on failure
    try:
        user_docs = list(users_col.find({"joined_at": {"$gte": start_of_week}}, {"joined_at": 1, "user_id": 1}))
    except Exception:
        user_docs = []
    try:
        total_users = users_col.count_documents({})
    except Exception:
        total_users = 0
    try:
        transactions = list(db["transactions"].find(
            {"status": {"$in": ["paid", "success", "approved"]}},
            {"status": 1, "amount": 1, "created_at": 1, "user_id": 1},
        ))
    except Exception:
        transactions = []
    try:
        all_transactions = list(db["transactions"].find({}, {"status": 1, "amount": 1, "created_at": 1}))
    except Exception:
        all_transactions = transactions
    try:
        withdrawals = list(db["withdrawals"].find(
            {"status": "paid"}, {"status": 1, "amount": 1, "created_at": 1}
        ))
    except Exception:
        withdrawals = []
    try:
        all_withdrawals = list(db["withdrawals"].find({}, {"status": 1, "amount": 1, "created_at": 1}))
    except Exception:
        all_withdrawals = withdrawals
    try:
        account_orders = list(orders_col.find(
            {"status": "purchased"}, {"status": 1, "price": 1, "created_at": 1, "country": 1, "user_id": 1}
        ))
    except Exception:
        account_orders = []
    try:
        smm_orders = list(db["smm_orders"].find(
            {}, {"status": 1, "status_norm": 1, "price": 1, "created_at": 1, "refunded": 1, "category": 1, "service": 1, "user_id": 1}
        ))
    except Exception:
        smm_orders = []
    try:
        otp_orders = list(db["otp_orders"].find(
            {}, {"status": 1, "price": 1, "cost": 1, "live_cost": 1, "created_at": 1, "service": 1, "service_name": 1, "country": 1, "country_name": 1, "route": 1, "route_type": 1, "operator": 1, "provider_reported_price": 1, "refunded": 1, "user_id": 1}
        ))
    except Exception:
        otp_orders = []
    try:
        otp_catalog_docs = list(db["otp_catalog"].find({}, {"fetched_at": 1, "action": 1}))
    except Exception:
        otp_catalog_docs = []
    try:
        otp_settings_doc = db["settings"].find_one({"_id": "otp_services"}) or {}
        # Build effective view if possible
        from otp_settings import effective_settings as otp_eff
        otp_eff_view = otp_eff(db["settings"], TEMPORASMS_API_KEY, 0, USD_TO_INR) if 'TEMPORASMS_API_KEY' in globals() else {"routes": [], "operators": []}
    except Exception:
        otp_eff_view = {"routes": [], "operators": []}
        otp_settings_doc = {}

    # Summarize with extended metrics
    try:
        smm_cfg = db["smm_settings"].find_one({"_id": "config"}) or {}
        checker_last = utc_naive(smm_cfg.get("checker_last_run"))
        checker_err = str(smm_cfg.get("checker_last_error") or "")[:240]
    except Exception:
        checker_last = None
        checker_err = ""
    metrics = summarize_analytics(
        total_users=total_users,
        user_docs=user_docs,
        transactions=transactions,
        withdrawals=withdrawals,
        account_orders=account_orders,
        smm_orders=smm_orders,
        now=now,
        otp_orders=otp_orders,
        all_transactions=all_transactions,
        all_withdrawals=all_withdrawals,
        otp_catalog=otp_catalog_docs,
        otp_settings=otp_eff_view,
        rate=float(USD_TO_INR) if 'USD_TO_INR' in globals() else 100.0,
        smm_checker_last_run=checker_last,
        smm_checker_error=checker_err,
    )
    health = await get_live_bot_status()

    # Helper to escape
    def esc(v):
        return escape(str(v) if v is not None else "—")

    # Build report sections
    # Overview
    overview = (
        "<blockquote>📈 <b>Overview / Snapshot</b><br>"
        f"• <b>Generated:</b> <code>{esc(now.strftime('%d %b %Y %H:%M UTC'))}</code> · <code>{esc(now_ist.strftime('%d %b %Y %H:%M IST'))}</code><br>"
        f"• <b>Window:</b> today from <code>{esc(metrics['day_start'].strftime('%d %b %H:%M UTC'))}</code> · week from <code>{esc(metrics['week_start'].strftime('%d %b %H:%M UTC'))}</code><br>"
        f"• <b>Total revenue (all verticals):</b> <code>{esc(_fmt_money(metrics.get('total_revenue', 0)))}</code> · today <code>{esc(_fmt_money(metrics.get('total_today_revenue', 0)))}</code> · week <code>{esc(_fmt_money(metrics.get('total_week_revenue', 0)))}</code><br>"
        f"• <b>Share:</b> Accounts <code>{esc(_fmt_pct(metrics.get('account_share', 0)))}</code> · SMM <code>{esc(_fmt_pct(metrics.get('smm_share', 0)))}</code> · OTP <code>{esc(_fmt_pct(metrics.get('otp_share', 0)))}</code>"
        "</blockquote>"
    )
    # User Growth
    user_growth = (
        "<blockquote>👥 <b>User Growth</b><br>"
        f"• <b>Total users:</b> <code>{metrics['total_users']:,}</code><br>"
        f"• <b>Joined today / this week:</b> <code>{metrics['users_today']:,}</code> / <code>{metrics['users_week']:,}</code> "
        f"{_trend_arrow(metrics.get('users_today_delta', 0))} <code>{esc(_fmt_pct(metrics.get('users_today_delta', 0)))}</code> vs yesterday<br>"
        f"• <b>Yesterday / last week:</b> <code>{metrics.get('users_yesterday', 0):,}</code> / <code>{metrics.get('users_last_week', 0):,}</code><br>"
        f"• <b>Active buyers:</b> <code>{metrics.get('active_buyers', 0):,}</code> · <b>Conversion:</b> <code>{esc(_fmt_pct(metrics.get('conversion_rate', 0)))}</code><br>"
        f"• <b>Top-up users:</b> <code>{metrics.get('topup_users', 0):,}</code>"
        "</blockquote>"
    )
    # Wallet
    wallet = (
        "<blockquote>💰 <b>Wallet (INR)</b><br>"
        f"• <b>Deposits total:</b> <code>{esc(_fmt_money(metrics['deposit_total']))}</code><br>"
        f"• <b>Deposits today / this week:</b> <code>{esc(_fmt_money(metrics['deposit_today']))}</code> / <code>{esc(_fmt_money(metrics['deposit_week']))}</code><br>"
        f"• <b>Withdrawals paid:</b> <code>{esc(_fmt_money(metrics['withdrawal_total']))}</code> · today <code>{esc(_fmt_money(metrics.get('withdrawal_today_amount', 0)))}</code> · week <code>{esc(_fmt_money(metrics.get('withdrawal_week_amount', 0)))}</code><br>"
        f"• <b>Pending:</b> recharges <code>{metrics.get('pending_deposits', 0):,}</code> · withdrawals <code>{metrics.get('pending_withdrawals', 0):,}</code><br>"
        f"• <b>Net flow:</b> <code>{esc(_fmt_money(metrics.get('net_flow', 0)))}</code> · <b>Avg deposit:</b> <code>{esc(_fmt_money(metrics.get('avg_deposit', 0)))}</code>"
        "</blockquote>"
    )
    # Account Sales
    top_countries = metrics.get('top_countries', [])
    if top_countries:
        tc_text = " · ".join(f"{esc(c)} <code>{n:,}</code>" for c, n in top_countries)
    else:
        tc_text = "<code>—</code>"
    account_sales = (
        "<blockquote>🛒 <b>Account Sales</b><br>"
        f"• <b>Orders:</b> <code>{metrics['account_orders']:,}</code> · <b>Revenue:</b> <code>{esc(_fmt_money(metrics['account_revenue']))}</code><br>"
        f"• <b>Today:</b> <code>{metrics['account_today_count']:,}</code> · <code>{esc(_fmt_money(metrics['account_today_revenue']))}</code> · "
        f"<b>Week:</b> <code>{metrics['account_week_count']:,}</code> · <code>{esc(_fmt_money(metrics['account_week_revenue']))}</code><br>"
        f"• <b>Avg order value:</b> <code>{esc(_fmt_money(metrics.get('account_avg_value', 0)))}</code><br>"
        f"• <b>Refunded / cancelled:</b> <code>{metrics.get('account_refunded', 0):,}</code> / <code>{metrics.get('account_cancelled', 0):,}</code><br>"
        f"• <b>Top 3 countries:</b> {tc_text}"
        "</blockquote>"
    )
    # SMM
    smm = (
        "<blockquote>📱 <b>SMM</b><br>"
        f"• <b>Orders:</b> <code>{metrics['smm_orders']:,}</code> · <b>Open:</b> <code>{metrics['smm_open_orders']:,}</code><br>"
        f"• <b>Gross:</b> <code>{esc(_fmt_money(metrics['smm_gross']))}</code> · <b>Net:</b> <code>{esc(_fmt_money(metrics['smm_net']))}</code> · <b>Refunded:</b> <code>{esc(_fmt_money(metrics['smm_refunds']))}</code><br>"
        f"• <b>Today / week net:</b> <code>{esc(_fmt_money(metrics['smm_today_net']))}</code> / <code>{esc(_fmt_money(metrics['smm_week_net']))}</code><br>"
        f"• <b>Avg order value:</b> <code>{esc(_fmt_money(metrics.get('smm_avg_value', 0)))}</code> · <b>Top category:</b> <code>{esc(metrics.get('smm_top_category', '—'))}</code>"
        "</blockquote>"
    )
    # OTP
    otp_top_app = esc(metrics.get('otp_top_app', '—'))
    otp_top_country = esc(metrics.get('otp_top_country', '—'))
    otp_top_route = esc(metrics.get('otp_top_route', '—'))
    catalog_age = metrics.get('otp_catalog_age')
    if catalog_age is not None:
        catalog_age_text = f"{int(catalog_age//60)}m ago" if catalog_age < 3600 else f"{catalog_age/3600:.1f}h ago"
    else:
        catalog_age_text = "—"
    enabled_lines = metrics.get('otp_enabled_lines', [])
    enabled_text = ", ".join(esc(l) for l in enabled_lines) if enabled_lines else "—"
    otp = (
        "<blockquote>📲 <b>OTP (TemporaSMS)</b><br>"
        f"• <b>Orders:</b> <code>{metrics.get('otp_orders', 0):,}</code> · <b>Gross:</b> <code>{esc(_fmt_money(metrics.get('otp_gross', 0)))}</code> · <b>Net:</b> <code>{esc(_fmt_money(metrics.get('otp_net', 0)))}</code> · <b>Refunded:</b> <code>{esc(_fmt_money(metrics.get('otp_refunded_amount', 0)))}</code> <code>({metrics.get('otp_refunded_count',0):,})</code><br>"
        f"• <b>Today / week:</b> <code>{metrics.get('otp_today_count',0):,}</code> · <code>{esc(_fmt_money(metrics.get('otp_today_revenue',0)))}</code> / <code>{metrics.get('otp_week_count',0):,}</code> · <code>{esc(_fmt_money(metrics.get('otp_week_revenue',0)))}</code><br>"
        f"• <b>Provider cost:</b> <code>${metrics.get('otp_cost_total',0):,.4f}</code> · <b>Live cost:</b> <code>${metrics.get('otp_live_cost_total',0):,.4f}</code> · <b>Retail price (net):</b> <code>{esc(_fmt_money(metrics.get('otp_net',0)))}</code><br>"
        f"• <b>Gross margin:</b> <code>{esc(_fmt_money(metrics.get('otp_margin',0)))}</code> · <code>{esc(_fmt_pct(metrics.get('otp_margin_percent',0)))}</code><br>"
        f"• <b>Statuses:</b> waiting <code>{metrics.get('otp_waiting',0):,}</code> · allocating <code>{metrics.get('otp_allocating',0):,}</code> · review <code>{metrics.get('otp_review',0):,}</code> · refunding <code>{metrics.get('otp_refunding',0):,}</code> · unpaid <code>{metrics.get('otp_unpaid',0):,}</code><br>"
        f"• <b>Top app / country / line:</b> <code>{otp_top_app}</code> / <code>{otp_top_country}</code> / <code>{otp_top_route}</code><br>"
        f"• <b>Catalog:</b> <code>{metrics.get('otp_catalog_count',0):,}</code> entries · age <code>{esc(catalog_age_text)}</code> · enabled <code>{esc(enabled_text)}</code>"
        "</blockquote>"
    )
    # Attention
    alerts = metrics.get('alerts', [])
    if alerts:
        alert_items = "<br>".join(f"• {esc(a)}" for a in alerts)
        attention = f"<blockquote>⚠️ <b>Attention / Alerts</b><br>{alert_items}</blockquote>"
    else:
        attention = "<blockquote>⚠️ <b>Attention / Alerts</b><br>• <code>All clear — no issues detected</code></blockquote>"

    # Bot & Provider Health
    health_otp_last = health.get('otp_last_warm')
    health_otp_text = health_otp_last.strftime('%d %b %H:%M UTC') if isinstance(health_otp_last, datetime) else esc(health_otp_last or "not yet")
    db_counts = health.get('db_counts', {})
    db_text = " · ".join(f"{esc(k)}: <code>{v:,}</code>" for k, v in db_counts.items()) if db_counts else "<code>—</code>"
    bot_health = (
        "<blockquote>🤖 <b>Bot &amp; Provider Health</b><br>"
        f"• <b>Process:</b> <code>{_status_dot(health.get('process'))} {esc(health.get('process'))}</code> · <b>Uptime:</b> <code>{esc(health.get('uptime'))}</code><br>"
        f"• <b>Telegram API:</b> <code>{_status_dot(health.get('telegram'))} {esc(health.get('telegram'))}</code><br>"
        f"• <b>SMM monitor:</b> <code>{_status_dot(health.get('checker'))} {esc(health.get('checker'))}</code> · <b>Last cycle:</b> <code>{esc(health.get('checker_last_run').strftime('%d %b %H:%M UTC') if health.get('checker_last_run') else 'not yet')}</code><br>"
        + (f"• <b>Last SMM error:</b> <code>{esc(health.get('checker_error'))}</code><br>" if health.get('checker_error') else "")
        + f"• <b>OTP worker:</b> <code>{esc(health.get('otp_worker'))}</code> · <b>Last catalog warm:</b> <code>{health_otp_text}</code><br>"
        f"• <b>Provider balance:</b> <code>{esc(health.get('provider_balance'))}</code><br>"
        f"• <b>DB counts:</b> {db_text}"
        "</blockquote>"
    )
    # Footer
    footer = (
        f"<p><i>Generated {esc(now.strftime('%d %b %Y %H:%M UTC'))} · {esc(now_ist.strftime('%d %b %Y %H:%M IST'))} · window: today/week (UTC)</i><br>"
        "<i>Legend: 📈 up · 📉 down · ➖ steady · 🟢 ok · ⚪ stale · 🔴 down · ₹ INR · $ USD</i></p>"
    )

    return (
        "<h2>💎 Premium Admin Analytics — Full Detail</h2>"
        + overview + user_growth + wallet + account_sales + smm + otp + attention + bot_health + footer
    )


@dp.message(Command("sales", "stats"))
async def cmd_advanced_sales(msg: Message):
    if not is_admin(msg.from_user.id):
        return await msg.answer("❌ <b>You are not authorized to view the sales report.</b>", parse_mode="HTML")
    status_msg = await msg.answer("🔄 <i>Loading live analytics and bot health...</i>", parse_mode="HTML")
    try:
        report = await build_admin_analytics_report()
    except Exception as exc:
        logger.exception("Admin analytics command failed")
        report = (
            "<h2>⚠️ Analytics Temporarily Unavailable</h2>"
            f"<blockquote><code>{escape(str(exc)[:500])}</code></blockquote>"
            "<p><i>Try again in a moment. The bot health page remains available from /admin.</i></p>"
        )
    await status_msg.edit_text(report, parse_mode="HTML")

@dp.message(Command("sellcountry"))
async def add_sell_countries(msg: Message):
    if msg.from_user.id not in ADMIN_IDS:
        return await msg.answer("Unauthorized ❌")

    # Remove the command itself and split lines
    lines = msg.text.split("\n")[1:]  # Skip the first line (the command)
    if not lines:
        return await msg.answer(
            "📋 Send like this:\n\n"
            "<code>/sellcountry\n+91 India ₹30\n+1 USA ₹32\n+62 Indonesia ₹28</code>",
            parse_mode="HTML"
        )

    updated = []
    errors = []

    for line in lines:
        line = line.strip()
        if not line:
            continue

        try:
            parts = line.split(" ")
            prefix = parts[0]
            if not prefix.startswith("+"):
                raise ValueError("Missing +country code")

            # Extract price (₹XX)
            match_price = [p for p in parts if "₹" in p]
            if not match_price:
                raise ValueError("Missing price (₹)")
            price = match_price[-1]  # Take last ₹ value
            country = " ".join(parts[1:parts.index(price)]).strip()

            db["sell_countries"].update_one(
                {"prefix": prefix},
                {"$set": {"country": country, "price": price}},
                upsert=True
            )

            updated.append(f"{prefix} {country} → {price}")
        except Exception as e:
            errors.append(f"❌ {line} ({e})")

    text = ""
    if updated:
        text += "✅ <b>Updated Successfully:</b>\n" + "\n".join(updated) + "\n\n"
    if errors:
        text += "⚠️ <b>Errors:</b>\n" + "\n".join(errors)

    await msg.answer(text or "⚙️ Nothing processed.", parse_mode="HTML")


# ================= Admin Credit/Debit Commands =================
@dp.message(Command("credit"))
async def cmd_credit(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return await msg.answer("❌ Not authorized.")
    
    await msg.answer("💰 Send user ID and amount to credit separated by a comma (e.g., 123456789,50):")
    await state.set_state("credit_waiting")

@dp.message(StateFilter("credit_waiting"))
async def handle_credit(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return

    if "," not in msg.text:
        return await msg.answer("❌ Invalid format. Example: 123456789,50")

    user_id_str, amount_str = msg.text.split(",", 1)
    try:
        user_id = int(user_id_str.strip())
        amount = float(amount_str.strip())
    except ValueError:
        return await msg.answer("❌ Invalid user ID or amount format.")

    user = users_col.find_one({"_id": user_id})
    if not user:
        return await msg.answer(f"❌ User with ID {user_id} not found.")

    new_balance = user.get("balance", 0.0) + amount
    users_col.update_one({"_id": user_id}, {"$set": {"balance": new_balance}})
    await msg.answer(f"✅ Credited ₹{amount:.2f} to {user.get('username') or user_id}\n💰 New Balance: ₹{new_balance:.2f}")
    await state.clear()


@dp.message(Command("debit"))
async def cmd_debit(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return await msg.answer("❌ Not authorized.")
    
    await msg.answer("💸 Send user ID and amount to debit separated by a comma (e.g., 123456789,50):")
    await state.set_state("debit_waiting")

@dp.message(StateFilter("debit_waiting"))
async def handle_debit(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return

    if "," not in msg.text:
        return await msg.answer("❌ Invalid format. Example: 123456789,50")

    user_id_str, amount_str = msg.text.split(",", 1)
    try:
        user_id = int(user_id_str.strip())
        amount = float(amount_str.strip())
    except ValueError:
        return await msg.answer("❌ Invalid user ID or amount format.")

    user = users_col.find_one({"_id": user_id})
    if not user:
        return await msg.answer(f"❌ User with ID {user_id} not found.")

    new_balance = max(user.get("balance", 0.0) - amount, 0.0)
    users_col.update_one({"_id": user_id}, {"$set": {"balance": new_balance}})
    await msg.answer(f"✅ Debited ₹{amount:.2f} from {user.get('username') or user_id}\n💰 New Balance: ₹{new_balance:.2f}")
    await state.clear()





    # ================= MongoDB Redeem Collection =================
redeem_col = db["redeem_codes"]  # Add this at top with other collections

# ================= Redeem FSM =================
class RedeemState(StatesGroup):
    # For auto-generated redeem codes
    waiting_amount = State()          # Admin enters amount
    waiting_limit = State()           # Admin selects max users via inline numeric keypad

    # For custom redeem codes
    waiting_code = State()            # Admin enters custom code (e.g. DIWALI100)
    waiting_amount_custom = State()   # Admin enters amount for custom code
    waiting_limit_custom = State()    # Admin selects max users for custom code

class UserRedeemState(StatesGroup):
    waiting_code = State()            # User enters redeem code
    
# ================= Helper =================
import random, string
def generate_code(length=8):
    """Generate code like HEIKE938"""
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))



    
        # ================= Admin: Create Custom Redeem =================
@dp.message(Command("cusredeem"))
async def cmd_custom_redeem(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return await msg.answer("❌ Not authorized.")
    await msg.answer("🎟️ Enter the custom redeem code (e.g. DIWALI100):")
    await state.set_state(RedeemState.waiting_code)

# ================= Admin: Handle Custom Code =================
@dp.message(StateFilter(RedeemState.waiting_code))
async def handle_custom_code(msg: Message, state: FSMContext):
    code = msg.text.strip().upper()
    if redeem_col.find_one({"code": code}):
        return await msg.answer("⚠️ This code already exists. Try another one.")

    await state.update_data(custom_code=code)
    await msg.answer("💰 Enter the amount for this redeem code:")
    await state.set_state(RedeemState.waiting_amount_custom)

# ================= Admin: Handle Custom Amount =================
@dp.message(StateFilter(RedeemState.waiting_amount_custom))
async def handle_custom_amount(msg: Message, state: FSMContext):
    try:
        amount = float(msg.text.strip())
        if amount <= 0:
            raise ValueError
    except ValueError:
        return await msg.answer("❌ Invalid amount. Send a number like 50 or 100.")

    await state.update_data(amount=amount, limit_str="")

    # Inline numeric keypad
    kb = InlineKeyboardBuilder()
    for row in (("1", "2", "3"), ("4", "5", "6"), ("7", "8", "9"), ("0", "❌", "✅")):
        kb.row(*[InlineKeyboardButton(text=btn, callback_data=f"cusredeemnum:{btn}") for btn in row])

    await msg.answer(
        "👥 Select max number of users who can claim this custom code:\n<b>0</b>",
        parse_mode="HTML",
        reply_markup=kb.as_markup()
    )
    await state.set_state(RedeemState.waiting_limit_custom)

# ================= Admin: Handle Custom Inline Number Pad =================
@dp.callback_query(F.data.startswith("cusredeemnum:"))
async def handle_custom_redeem_number(cq: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    current = data.get("limit_str", "")
    value = cq.data.split(":")[1]

    if value == "❌":
        current = current[:-1]
    elif value == "✅":
        if not current:
            await cq.answer("❌ Please select at least one user.", show_alert=True)
            return
        try:
            limit = int(current)
        except ValueError:
            await cq.answer("❌ Invalid number.", show_alert=True)
            return

        code = data.get("custom_code")
        amount = data.get("amount")
        created_at = datetime.utcnow()

        # Insert redeem into MongoDB
        redeem_col.insert_one({
            "code": code,
            "amount": amount,
            "max_claims": limit,
            "claimed_count": 0,
            "claimed_users": [],
            "created_at": created_at
        })

        await cq.message.edit_text(
            f"✅ Custom redeem code created!\n\n"
            f"🎟️ Code: <code>{code}</code>\n"
            f"💰 Amount: ₹{amount:.2f}\n"
            f"👥 Max Claims: {limit}",
            parse_mode="HTML"
        )
        await state.clear()
        return
    else:
        current += value
        if len(current) > 6:
            current = current[:6]

    await state.update_data(limit_str=current)

    # Rebuild keypad dynamically
    kb = InlineKeyboardBuilder()
    for row in (("1", "2", "3"), ("4", "5", "6"), ("7", "8", "9"), ("0", "❌", "✅")):
        kb.row(*[InlineKeyboardButton(text=btn, callback_data=f"cusredeemnum:{btn}") for btn in row])

    await cq.message.edit_text(
        f"👥 Select max number of users who can claim this custom code:\n<b>{current or '0'}</b>",
        parse_mode="HTML",
        reply_markup=kb.as_markup()
    )
    await cq.answer()
        


# ================= Admin: View Redeems =================
@dp.message(Command("redeemlist"))
async def cmd_redeem_list(msg: Message):
    if not is_admin(msg.from_user.id):
        return await msg.answer("❌ Not authorized.")

    redeems = list(redeem_col.find())
    if not redeems:
        return await msg.answer("📭 No redeem codes found.")

    text = "🎟️ <b>Active Redeem Codes:</b>\n\n"
    for r in redeems:
        text += (
            f"Code: <code>{r['code']}</code>\n"
            f"💰 Amount: ₹{r['amount']}\n"
            f"👥 {r['claimed_count']} / {r['max_claims']} claimed\n\n"
        )
    await msg.answer(text, parse_mode="HTML")

# ================= User: Redeem Code =================
@dp.callback_query(F.data == "redeem")
async def callback_user_redeem(cq: CallbackQuery, state: FSMContext):
    await cq.answer()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Cancel", callback_data="more_menu")]
    ])
    await cq.message.edit_text(
        "<h2>🎟️ Redeem Promo Code</h2>"
        "<blockquote>🎁 <b>Instant Wallet Credit</b><br>"
        "Enter your promo or gift code in the chat below to claim your bonus balance:</blockquote>",
        reply_markup=kb,
    )
    await state.set_state(UserRedeemState.waiting_code)

# Command /redeem
@dp.message(F.text == "/redeem")
async def command_user_redeem(message: Message, state: FSMContext):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Cancel", callback_data="back_main")]
    ])
    await message.answer(
        "<h2>🎟️ Redeem Promo Code</h2>"
        "<blockquote>🎁 <b>Instant Wallet Credit</b><br>"
        "Enter your promo or gift code below to claim your bonus balance:</blockquote>",
        reply_markup=kb,
    )
    await state.set_state(UserRedeemState.waiting_code)

@dp.message(StateFilter(UserRedeemState.waiting_code))
async def handle_user_redeem(msg: Message, state: FSMContext):
    code = msg.text.strip().upper()
    redeem = redeem_col.find_one({"code": code})

    if not redeem:
        await msg.answer("<h2>❌ Invalid Code</h2><blockquote>That redeem code is invalid or has expired.</blockquote>")
        return await state.clear()

    if redeem["claimed_count"] >= redeem["max_claims"]:
        await msg.answer("<h2>🚫 Claim Limit Reached</h2><blockquote>This redeem code has already been fully claimed.</blockquote>")
        return await state.clear()

    user = users_col.find_one({"_id": msg.from_user.id})
    if not user:
        await msg.answer("⚠️ Please use /start first.")
        return await state.clear()

    if msg.from_user.id in redeem.get("claimed_users", []):
        await msg.answer("<h2>⚠️ Already Claimed</h2><blockquote>You have already redeemed this promo code.</blockquote>")
        return await state.clear()

    # Credit user balance
    users_col.update_one({"_id": msg.from_user.id}, {"$inc": {"balance": redeem["amount"]}})
    redeem_col.update_one(
        {"code": code},
        {"$inc": {"claimed_count": 1}, "$push": {"claimed_users": msg.from_user.id}}
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏠 Main Menu", callback_data="back_main")]
    ])
    await msg.answer(
        "<h2>🎉 Promo Code Redeemed!</h2>"
        f"<blockquote>🎟️ <b>Code:</b> <code>{code}</code><br>"
        f"💰 <b>Credited Amount:</b> <code>₹{redeem['amount']:.2f}</code></blockquote>",
        parse_mode="HTML",
        reply_markup=kb,
    )
    await state.clear()

@dp.message(Command("editsell"))
async def cmd_editsell(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return await msg.answer("❌ Not authorized.")
    await state.set_state(EditSellRates.waiting_rates)
    await msg.answer("📋 Send the list in format:\n\n<code>USA ₹50\nIndia ₹10\nUK ₹20</code>")


@dp.message(EditSellRates.waiting_rates)
async def handle_sell_edit(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        await state.clear()
        return
    records = []
    for line in (m.text or "").splitlines():
        try:
            country, raw_price = line.rsplit("₹", 1)
            country = country.strip()
            price = float(raw_price.strip())
            if country and price >= 0:
                code = "+1" if "USA" in country else "+91" if "India" in country else ""
                records.append({"country": country, "price": price, "code": code})
        except (ValueError, TypeError):
            continue
    if not records:
        return await m.answer("❌ No valid rates found. Use one <code>Country ₹Price</code> per line.")
    sell_prices_col.delete_many({})
    sell_prices_col.insert_many(records)
    await state.clear()
    await m.answer(f"✅ {len(records)} sell rates updated.")

# ================= Admin Live Credits =================
@dp.message(Command("livecredits"))
async def cmd_livecredits(msg: Message):
    if not is_admin(msg.from_user.id):
        return
    await show_live_credits(msg, page=0)

async def show_live_credits(msg_or_call, page: int):
    limit = 10
    skip = page * limit
    
    # Find users with balance > 0, sort DESC by balance
    cursor = users_col.find({"balance": {"$gt": 0}}).sort("balance", -1)
    total_users = users_col.count_documents({"balance": {"$gt": 0}})
    
    users_list = list(cursor.skip(skip).limit(limit))
    
    kb = InlineKeyboardBuilder()
    if not users_list:
        text = "<h2>💎 Live User Credits</h2><blockquote>📉 No users currently have a positive balance.</blockquote>"
        kb.row(InlineKeyboardButton(text="⬅️ Admin Panel", callback_data="admin_refresh"))
    else:
        rows = []
        for idx, u in enumerate(users_list, start=skip + 1):
            u_link = f"<a href='tg://user?id={u['_id']}'>{escape(str(u.get('username') or u['_id']))}</a>"
            rows.append(f"<b>#{idx}</b> 👤 {u_link} — <code>₹{u['balance']:.2f}</code>")
        text = (
            f"<h2>💎 Live User Credits — Page {page + 1}</h2>"
            f"<blockquote>{'<br>'.join(rows)}</blockquote>"
            f"<p>👥 <i>Total Funded Users: <b>{total_users:,}</b></i></p>"
        )
        if page > 0:
            kb.button(text="⬅️ Prev", callback_data=f"livecredits:{page-1}")
        if (skip + limit) < total_users:
            kb.button(text="Next ➡️", callback_data=f"livecredits:{page+1}")
        kb.adjust(2)
        kb.row(
            InlineKeyboardButton(text="⬅️ Admin Panel", callback_data="admin_refresh"),
            InlineKeyboardButton(text="❌ Close", callback_data="delete_msg"),
        )

    if isinstance(msg_or_call, Message):
        await msg_or_call.answer(text, parse_mode="HTML", reply_markup=kb.as_markup())
    else:
        await msg_or_call.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())

@dp.callback_query(F.data.startswith("livecredits:"))
async def pagination_livecredits(cq: CallbackQuery):
    if not is_admin(cq.from_user.id):
        return await cq.answer("❌", show_alert=True)
    page = int(cq.data.split(":")[1])
    await show_live_credits(cq, page)
    await cq.answer()

@dp.callback_query(F.data == "delete_msg")
async def delete_this_msg(cq: CallbackQuery):
    await cq.message.delete()

# ================= User History & Logs =================

@dp.callback_query(F.data == "history")
async def show_user_history(cq: CallbackQuery):
    user_id = cq.from_user.id
    
    # 1. Calculate Total Recharged
    txns = list(db["transactions"].find({"user_id": user_id, "status": {"$in": ["paid", "success", "approved"]}}))
    total_added = sum(float(t.get("amount", 0)) for t in txns)
    
    # 2. Fetch Orders from BOTH Databases
    s12_orders = list(orders_col.find({"user_id": user_id, "status": "purchased"}))
    s3_orders = list(db["lzt_orders"].find({"user_id": user_id, "status": {"$in": ["purchased", "completed", "delivered"]}}))
    
    all_orders = s12_orders + s3_orders
    total_purchased = len(all_orders)
    
    # Calculate Total Spent
    total_spent = sum(float(o.get("price") or o.get("price_inr") or 0) for o in all_orders)
    
    text = (
        "<h2>📑 Account Activity &amp; History</h2>"
        f"<blockquote>🛍️ <b>Accounts Purchased:</b> <code>{total_purchased}</code><br>"
        f"💸 <b>Total Spent:</b> <code>₹{total_spent:.2f}</code><br>"
        f"💳 <b>Total Recharged:</b> <code>₹{total_added:.2f}</code></blockquote>"
        "<p>📂 <i>Tap below to inspect your purchased numbers &amp; 2FA credentials:</i></p>"
    )
    
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="📂 View Purchase Logs", callback_data="purchase_logs:0"))
    kb.row(InlineKeyboardButton(text="⬅️ Back to Menu", callback_data="back_main")) 
    
    await cq.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())


@dp.callback_query(F.data.startswith("purchase_logs:"))
async def show_purchase_logs(cq: CallbackQuery):
    user_id = cq.from_user.id
    page = int(cq.data.split(":")[1])
    limit = 10
    skip = page * limit
    
    # Fetch from BOTH Databases
    s12_orders = list(orders_col.find({"user_id": user_id, "status": "purchased"}))
    s3_orders = list(db["lzt_orders"].find({"user_id": user_id, "status": {"$in": ["purchased", "completed", "delivered"]}}))
    
    # Merge and sort by newest first
    all_orders = s12_orders + s3_orders
    all_orders.sort(key=lambda x: x["_id"], reverse=True)
    
    total_orders = len(all_orders)
    my_orders = all_orders[skip : skip + limit]
    
    if not my_orders:
        return await cq.answer("❌ No purchase history found.", show_alert=True)
    
    rows_html = []
    for order in my_orders:
        ph_number = order.get('number')
        password = order.get("password")
        if not password:
            num_doc = numbers_col.find_one({"number": ph_number}) or db["numbers_col"].find_one({"number": ph_number})
            password = num_doc.get("password") if num_doc else "N/A"
        if not password or password == "None": 
            password = "N/A"
        rows_html.append(
            f"📱 <b>{escape(str(ph_number))}</b> — 🔐 2FA: <code>{escape(str(password))}</code>"
        )
        
    text = (
        f"<h2>📂 Purchase Logs — Page {page + 1}</h2>"
        f"<blockquote>{'<br>'.join(rows_html)}</blockquote>"
        f"<p>📦 <i>Showing {skip + 1}–{min(skip + limit, total_orders)} of {total_orders} orders</i></p>"
    )
        
    kb = InlineKeyboardBuilder()
    
    nav_btns = []
    if page > 0:
        nav_btns.append(InlineKeyboardButton(text="⬅️ Prev", callback_data=f"purchase_logs:{page-1}"))
    if (skip + limit) < total_orders:
        nav_btns.append(InlineKeyboardButton(text="Next ➡️", callback_data=f"purchase_logs:{page+1}"))
        
    if nav_btns:
        kb.row(*nav_btns)
        
    kb.row(InlineKeyboardButton(text="⬅️ Back to History", callback_data="history"))
    
    await cq.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())

# ================= API KEYS (Admin Server 2) =================
# Same environment-only keys imported from config.py at the top of this file.

# ================= Admin Server Management =================
async def get_api_balances():
    """Fetches live balance from both TG-Lion and TGPVA"""
    tg_lion_bal = "Error"
    tgpva_bal = "Error"
    
    async with aiohttp.ClientSession() as session:
        # Fetch TG-Lion Balance
        try:
            url1 = f"https://TG-Lion.net?action=get_balance&apiKey={TGLION_API_KEY}&YourID={TGLION_ID}"
            async with session.get(url1, timeout=5) as resp:
                data = await resp.json()
                if data.get("status") == "ok":
                    tg_lion_bal = data.get("balance", "0 USD")
        except Exception:
            pass
            
        # Fetch TGPVA Balance
        try:
            url2 = f"https://tgpva.com/api/user/getBalance?apiKey={TGPVA_API_KEY}"
            async with session.get(url2, timeout=5) as resp:
                data = await resp.json()
                if data.get("ok") and "result" in data:
                    # Balance 'result' ke andar se nikalna hai
                    balance_val = data["result"].get("balance", "0")
                    # Agar currency api me nahi aati to default 'USD' lagayenge
                    currency_val = data.get("currency", data["result"].get("currency", "USD"))
                    tgpva_bal = f"{balance_val} {currency_val}"
        except Exception:
            pass
            
    return tg_lion_bal, tgpva_bal

@dp.message(Command("manageserver"))
async def cmd_manage_server(msg: Message):
    if not is_admin(msg.from_user.id):
        return await msg.answer("❌ Not authorized.")
    
    status_msg = await msg.answer("🔄 <i>Fetching Live Server Status & API Balances...</i>", parse_mode="HTML")
    await send_server_dashboard(msg.from_user.id, status_msg.message_id)

async def send_server_dashboard(chat_id, message_id=None):
    # Fetch settings from DB (Defaults if not set)
    settings = settings_col.find_one({"_id": "server_config"}) or {}
    s1 = settings.get("s1", "active")
    s2 = settings.get("s2", "active")
    s3 = settings.get("s3", "maintenance")
    s4 = settings.get("s4", "active")
    active_api = settings.get("s2_api", "tgpva") # tglion or tgpva
    profit = settings.get("s2_profit", 0)

    # Fetch live balances
    tg_lion_bal, tgpva_bal = await get_api_balances()

    text = (
        f"<h2>🎛️ Server Management Dashboard</h2>"
        f"<blockquote>{tg_icon()} <b>Server Routing Status</b><br>"
        f"• <b>Server 1 (Aged):</b> <code>{s1.upper()}</code><br>"
        f"• <b>Server 2 (High Quality):</b> <code>{s2.upper()}</code><br>"
        f"• <b>Server 3 (Global &amp; LZT):</b> <code>{s3.upper()}</code><br>"
        f"• <b>Server 4 (New Numbers):</b> <code>{s4.upper()}</code></blockquote>"
        f"<blockquote>⚙️ <b>Server 2 Configuration</b><br>"
        f"• <b>Active Provider:</b> <code>{active_api.upper()}</code><br>"
        f"• <b>Profit Margin:</b> <code>{profit}%</code></blockquote>"
        f"<blockquote>💳 <b>Live API Provider Balances</b><br>"
        f"• 🦁 <b>TG-Lion:</b> <code>{tg_lion_bal}</code><br>"
        f"• 🛡️ <b>TGPVA:</b> <code>{tgpva_bal}</code></blockquote>"
    )

    kb = InlineKeyboardBuilder()
    # Toggles for S1, S2, S3, S4
    kb.row(
        InlineKeyboardButton(text=f"S1: {'🟢 ON' if s1=='active' else '🔴 OFF'}", callback_data="toggle_s1"),
        InlineKeyboardButton(text=f"S2: {'🟢 ON' if s2=='active' else '🔴 OFF'}", callback_data="toggle_s2"),
        InlineKeyboardButton(text=f"S3: {'🟢 ON' if s3=='active' else '🔴 OFF'}", callback_data="toggle_s3")
    )
    kb.row(
        InlineKeyboardButton(text=f"S4: {'🟢 ON' if s4=='active' else '🔴 OFF'}", callback_data="toggle_s4"),
    )
    # API Switcher & Profit
    kb.row(
        InlineKeyboardButton(text="🔄 Switch S2 API", callback_data="switch_s2_api"),
        InlineKeyboardButton(text="📈 Set S2 Profit %", callback_data="set_s2_profit")
    )
    kb.row(
        InlineKeyboardButton(text="🛰️ Manage Server 3 & LZT", callback_data="s3m_home"),
    )
    kb.row(
        InlineKeyboardButton(text="📲 All-Apps OTP Panel", callback_data="s3_adm_home"),
        InlineKeyboardButton(text="✈️ Server 4 Panel", callback_data="s4_adm_home"),
    )
    kb.row(
        InlineKeyboardButton(text="🔄 Refresh Balances", callback_data="refresh_dashboard"),
        InlineKeyboardButton(text="⬅️ Admin Panel", callback_data="admin_refresh"),
    )
    kb.row(InlineKeyboardButton(text="❌ Close", callback_data="delete_msg"))

    if message_id:
        await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, parse_mode="HTML", reply_markup=kb.as_markup())
    else:
        await bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=kb.as_markup())

# Callbacks for toggles
@dp.callback_query(F.data.in_(["toggle_s1", "toggle_s2", "toggle_s3", "toggle_s4", "switch_s2_api", "refresh_dashboard"]))
async def manage_toggles(cq: CallbackQuery):
    if not is_admin(cq.from_user.id):
        return await cq.answer("Not authorized.", show_alert=True)
    action = cq.data
    settings = settings_col.find_one({"_id": "server_config"}) or {"_id": "server_config"}
    
    if action == "toggle_s1":
        settings["s1"] = "maintenance" if settings.get("s1", "active") == "active" else "active"
    elif action == "toggle_s2":
        settings["s2"] = "maintenance" if settings.get("s2", "active") == "active" else "active"
    elif action == "toggle_s3":
        settings["s3"] = "maintenance" if settings.get("s3", "maintenance") == "active" else "active"
    elif action == "toggle_s4":
        settings["s4"] = "maintenance" if settings.get("s4", "active") == "active" else "active"
        db["server4_settings"].update_one(
            {"_id": "maintenance_config"},
            {"$set": {"enabled": settings["s4"] != "active"}},
            upsert=True,
        )
    elif action == "switch_s2_api":
        settings["s2_api"] = "tglion" if settings.get("s2_api", "tgpva") == "tgpva" else "tgpva"
    
    if action != "refresh_dashboard":
        settings_col.update_one({"_id": "server_config"}, {"$set": settings}, upsert=True)
        
    await send_server_dashboard(cq.message.chat.id, cq.message.message_id)
    await cq.answer("Dashboard Updated!")

@dp.callback_query(F.data == "set_s2_profit")
async def set_profit_btn(cq: CallbackQuery, state: FSMContext):
    if not is_admin(cq.from_user.id):
        return await cq.answer("Not authorized.", show_alert=True)
    await cq.message.answer("📈 Send the new profit percentage for Server 2 (e.g., send 20 for 20%):")
    await state.set_state(ServerManage.waiting_profit_margin)
    await cq.answer()

@dp.message(StateFilter(ServerManage.waiting_profit_margin))
async def save_profit_margin(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        await state.clear()
        return await msg.answer("❌ Not authorized.")
    try:
        profit = float(msg.text.strip())
        settings_col.update_one({"_id": "server_config"}, {"$set": {"s2_profit": profit}}, upsert=True)
        await msg.answer(f"✅ Server 2 Profit Margin set to <b>{profit}%</b>", parse_mode="HTML")
        status_msg = await msg.answer("🔄 <i>Refreshing Dashboard...</i>", parse_mode="HTML")
        await send_server_dashboard(msg.chat.id, status_msg.message_id)
    except Exception:
        await msg.answer("❌ Invalid number. Please send a valid percentage like 15 or 20.5")
    await state.clear()

# ================= Admin Ban Commands (Upgraded) =================
async def get_target_id(msg: Message, args: list) -> int | None:
    """Helper to get user ID from reply, username, or manual ID"""
    # 1. Check if it's a reply
    if msg.reply_to_message:
        return msg.reply_to_message.from_user.id
    
    # 2. Check if an ID or @username was provided
    if len(args) < 2:
        return None
    
    target = args[1]
    
    # If it's a numeric ID
    if target.isdigit():
        return int(target)
    
    # If it's a username (this only works if the user is already in your DB)
    if target.startswith("@"):
        username = target.replace("@", "")
        user_doc = users_col.find_one({"username": username})
        if user_doc:
            return user_doc["_id"]
    
    return None

# ================= Admin: Top Users Command =================

@dp.message(Command("topusers"))
async def cmd_top_users(msg: Message):
    if not is_admin(msg.from_user.id):
        return
    
    # Parse page number from command (e.g., /topusers 2)
    args = msg.text.split()
    page = int(args[1]) if len(args) > 1 and args[1].isdigit() else 1
    limit = 10
    skip = (page - 1) * limit

    # Aggregation to find top spenders
    pipeline = [
        {
            "$group": {
                "_id": "$user_id",
                "total_spend": {"$sum": "$price"}
            }
        },
        {"$sort": {"total_spend": -1}},
        {"$skip": skip},
        {"$limit": limit},
        {
            "$lookup": {
                "from": "users",
                "localField": "_id",
                "foreignField": "_id",
                "as": "user_info"
            }
        }
    ]

    top_spenders = list(orders_col.aggregate(pipeline))

    if not top_spenders:
        return await msg.answer("❌ No spending data found or page out of range.")

    response_text = f"🏆 <b>Top Spending Users (Page {page})</b>\n"
    response_text += "--------------------------------\n"

    for entry in top_spenders:
        user_id = entry["_id"]
        total_spend = entry["total_spend"]
        
        # Get username if available, else use ID
        user_data = entry["user_info"][0] if entry["user_info"] else {}
        username = user_data.get("username")
        name = f"@{username}" if username else f"ID: <code>{user_id}</code>"
        
        response_text += f"👤 {name}\n💰 Total spend: ₹{total_spend:.2f}\n"
        response_text += "---------\n"

    # Add navigation tip
    response_text += f"\n<i>Use <code>/topusers {page + 1}</code> for next page.</i>"
    
    await msg.answer(response_text, parse_mode="HTML")
    

@dp.message(Command("gban"))
async def cmd_gban(msg: Message):
    if not is_admin(msg.from_user.id):
        return
    
    args = msg.text.split()
    target_id = await get_target_id(msg, args)
    
    if not target_id:
        return await msg.answer("⚠️ <b>Usage:</b>\n• Reply to a user with <code>/gban</code>\n• <code>/gban 12345678</code>\n• <code>/gban @username</code> (User must be in DB)", parse_mode="HTML")

    # Update or Create the user with banned status
    users_col.update_one(
        {"_id": target_id},
        {"$set": {"banned": True}},
        upsert=True # This ensures they are added to DB even if they never started the bot
    )
    
    await msg.answer(f"⛔ User <code>{target_id}</code> has been <b>BANNED</b> from the bot.", parse_mode="HTML")
    
    # Try to notify the user
    try:
        await bot.send_message(target_id, "🚫 <b>You have been banned from using this bot by the admin.</b>", parse_mode="HTML")
    except:
        pass

@dp.message(Command("ungban"))
async def cmd_ungban(msg: Message):
    if not is_admin(msg.from_user.id):
        return
    
    args = msg.text.split()
    target_id = await get_target_id(msg, args)
    
    if not target_id:
        return await msg.answer("⚠️ <b>Usage:</b> Reply with <code>/ungban</code> or use ID/Username.")

    result = users_col.update_one({"_id": target_id}, {"$set": {"banned": False}})
    
    if result.matched_count > 0:
        await msg.answer(f"✅ User <code>{target_id}</code> has been <b>UNBANNED</b>.", parse_mode="HTML")
    else:
        await msg.answer("❌ User not found in database.")

@dp.message(Command("userinfo"))
async def cmd_userinfo(msg: Message):
    if not is_admin(msg.from_user.id):
        return await msg.answer("❌ Not authorized.")

    args = msg.text.split()
    if len(args) < 2 or not args[1].isdigit():
        return await msg.answer("Usage: <code>/userinfo &lt;telegram_user_id&gt;</code>", parse_mode="HTML")

    target_id = int(args[1])
    user = users_col.find_one({"_id": target_id})
    if not user:
        return await msg.answer("❌ No user found with this ID.")

    # Total Deposited (approved recharges only)
    total_deposited = sum(
        t.get("amount", 0.0) for t in db["transactions"].find({"user_id": target_id, "status": "approved"})
    )

    # Total Spent — combined across Numbers (Server 1/2/3), SMM, and LZT Telegram accounts
    number_orders = list(orders_col.find({"user_id": target_id}))
    smm_order_docs = list(db["smm_orders"].find({"user_id": target_id}))
    lzt_order_docs = list(db["lzt_orders"].find({"user_id": target_id}))

    total_spent = (
        sum(o.get("price", 0.0) for o in number_orders)
        + sum(o.get("price", 0.0) for o in smm_order_docs)
        + sum(o.get("price_inr", 0.0) for o in lzt_order_docs)
    )
    total_orders = len(number_orders) + len(smm_order_docs) + len(lzt_order_docs)

    referred_by = user.get("referred_by")
    referral_earnings = user.get("referral_earnings", 0.0)
    total_referred = users_col.count_documents({"referred_by": target_id})
    joined_at = user.get("joined_at")
    joined_str = joined_at.strftime("%d %b %Y, %H:%M") if joined_at else "N/A"

    text = (
        f"<h2>👤 Full User Profile</h2>"
        f"<blockquote>"
        f"🆔 <b>User ID:</b> <code>{target_id}</code><br>"
        f"📛 <b>Username:</b> @{user.get('username') or 'NoUsername'}<br>"
        f"📅 <b>Joined:</b> {joined_str}"
        f"</blockquote>"
        f"<blockquote>"
        f"💳 <b>Current Balance:</b> <code>₹{user.get('balance', 0.0):.2f}</code><br>"
        f"💰 <b>Total Deposited:</b> <code>₹{total_deposited:.2f}</code><br>"
        f"🛒 <b>Total Spent:</b> <code>₹{total_spent:.2f}</code><br>"
        f"📦 <b>Total Orders:</b> <code>{total_orders}</code>"
        f"</blockquote>"
        f"<blockquote>"
        f"🔗 <b>Referred By:</b> {f'<code>{referred_by}</code>' if referred_by else 'None'}<br>"
        f"👥 <b>Total Referred Users:</b> <code>{total_referred}</code><br>"
        f"💵 <b>Referral Earnings:</b> <code>₹{referral_earnings:.2f}</code>"
        f"</blockquote>"
    )
    await msg.answer(text, parse_mode="HTML")        
@dp.callback_query(lambda c: c.data == "transfer_balance")
async def transfer_balance_start(cq: CallbackQuery, state: FSMContext):
    await state.set_state(TransferBalance.waiting_recipient)

    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(
            text="⬅️ Cancel",
            callback_data="more_menu"
        )
    )

    await cq.message.edit_text(
        "<h2>💸 Transfer Wallet Balance</h2>"
        "<blockquote>👤 <b>Step 1 — Recipient ID</b><br>"
        "Send the numeric <b>Telegram User ID</b> of the user you want to transfer funds to:</blockquote>",
        parse_mode="HTML",
        reply_markup=kb.as_markup()
    )

    await cq.answer()


@dp.message(StateFilter(TransferBalance.waiting_recipient))
async def transfer_balance_recipient(msg: Message, state: FSMContext):

    if not msg.text.strip().isdigit():
        return await msg.answer(
            "<h2>❌ Invalid User ID</h2><blockquote>Please send a valid numeric Telegram User ID.</blockquote>"
        )

    recipient_id = int(msg.text.strip())

    if recipient_id == msg.from_user.id:
        return await msg.answer(
            "<h2>❌ Invalid Recipient</h2><blockquote>You cannot transfer balance to yourself.</blockquote>"
        )

    recipient = users_col.find_one({
        "_id": recipient_id
    })

    if not recipient:
        return await msg.answer(
            "<h2>❌ User Not Found</h2><blockquote>This user hasn't started the bot yet. Ask them to press <code>/start</code> first.</blockquote>"
        )

    await state.update_data(
        recipient_id=recipient_id
    )

    await state.set_state(
        TransferBalance.waiting_amount
    )

    await msg.answer(
        "<h2>💰 Enter Transfer Amount</h2>"
        f"<blockquote>✅ <b>Recipient Verified:</b> <code>{recipient_id}</code><br>"
        "💵 Enter the amount in <b>₹ (INR)</b> you wish to send:</blockquote>",
        parse_mode="HTML"
    )


@dp.message(StateFilter(TransferBalance.waiting_amount))
async def transfer_balance_amount(msg: Message, state: FSMContext):

    try:
        amount = float(msg.text.strip())

    except ValueError:
        return await msg.answer(
            "<h2>❌ Invalid Amount</h2><blockquote>Please send a valid numeric amount.</blockquote>"
        )

    if amount <= 0:
        return await msg.answer(
            "<h2>❌ Invalid Amount</h2><blockquote>Amount must be greater than 0.</blockquote>"
        )

    sender = users_col.find_one({
        "_id": msg.from_user.id
    }) or {}

    sender_balance = sender.get(
        "balance",
        0.0
    )

    if amount > sender_balance:
        return await msg.answer(
            "<h2>🚫 Insufficient Balance</h2>"
            f"<blockquote>💰 <b>Available Balance:</b> <code>₹{sender_balance:.2f}</code><br>"
            f"💸 <b>Requested Transfer:</b> <code>₹{amount:.2f}</code></blockquote>"
        )

    data = await state.get_data()

    recipient_id = data["recipient_id"]

    await state.update_data(
        amount=amount
    )

    kb = InlineKeyboardBuilder()

    kb.row(
        InlineKeyboardButton(
            text="✅ Confirm Transfer",
            callback_data="transfer_confirm"
        ),
        InlineKeyboardButton(
            text="❌ Cancel",
            callback_data="back_main"
        )
    )

    await msg.answer(
        "<h2>🧾 Confirm Balance Transfer</h2>"
        f"<blockquote>👤 <b>Recipient ID:</b> <code>{recipient_id}</code><br>"
        f"💰 <b>Transfer Amount:</b> <code>₹{amount:.2f}</code><br>"
        f"💳 <b>Balance After Transfer:</b> <code>₹{sender_balance - amount:.2f}</code></blockquote>"
        "<p>❓ <i>Tap <b>Confirm Transfer</b> below to send funds immediately:</i></p>",
        parse_mode="HTML",
        reply_markup=kb.as_markup()
    )


@dp.callback_query(lambda c: c.data == "transfer_confirm")
async def transfer_balance_confirm(
    cq: CallbackQuery,
    state: FSMContext
):

    data = await state.get_data()

    recipient_id = data.get(
        "recipient_id"
    )

    amount = data.get(
        "amount"
    )

    await state.clear()

    if not recipient_id or not amount:
        return await cq.answer(
            "❌ Session expired, please try again.",
            show_alert=True
        )

    sender = users_col.find_one({
        "_id": cq.from_user.id
    }) or {}

    sender_balance = sender.get(
        "balance",
        0.0
    )

    if amount > sender_balance:
        return await cq.answer(
            "❌ Insufficient balance.",
            show_alert=True
        )

    # ==================================================
    # 💸 DEDUCT BALANCE FROM SENDER
    # ==================================================

    users_col.update_one(
        {
            "_id": cq.from_user.id
        },
        {
            "$inc": {
                "balance": -amount
            }
        }
    )

    # ==================================================
    # 💰 ADD BALANCE TO RECIPIENT
    # ==================================================

    users_col.update_one(
        {
            "_id": recipient_id
        },
        {
            "$inc": {
                "balance": amount
            }
        }
    )

    new_sender_balance = (
        sender_balance - amount
    )

    # ==================================================
    # 🔘 BACK BUTTON
    # ==================================================

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⬅️ Back to Menu",
                    callback_data="back_main"
                )
            ]
        ]
    )

    # ==================================================
    # ✅ SHOW SUCCESS TO SENDER
    # ==================================================

    await cq.message.edit_text(
        "<h2>✅ Balance Transfer Successful</h2>"
        f"<blockquote>👤 <b>Sent To:</b> <code>{recipient_id}</code><br>"
        f"💰 <b>Amount Sent:</b> <code>₹{amount:.2f}</code><br>"
        f"💳 <b>Remaining Balance:</b> <code>₹{new_sender_balance:.2f}</code></blockquote>",
        parse_mode="HTML",
        reply_markup=kb
    )

    # ==================================================
    # 💰 NOTIFY RECIPIENT
    # ==================================================

    try:
        await bot.send_message(
            recipient_id,
            "<h2>💰 Wallet Transfer Received!</h2>"
            f"<blockquote>👤 <b>From User:</b> <code>{cq.from_user.id}</code><br>"
            f"💰 <b>Amount Credited:</b> <code>₹{amount:.2f}</code></blockquote>",
            parse_mode="HTML"
        )
    except Exception:
        pass

    # ==================================================
    # 🔔 NOTIFY ADMIN
    # ==================================================

    recipient_after = users_col.find_one({"_id": recipient_id}) or {}
    recipient_new_balance = recipient_after.get("balance", 0.0)

    try:
        await bot.send_message(
            ADMINLOG,
            "<h2>💸 Balance Transfer Log</h2>"
            f"<blockquote>👤 <b>From:</b> <code>{cq.from_user.id}</code> (@{cq.from_user.username or 'NoUsername'})<br>"
            f"👤 <b>To:</b> <code>{recipient_id}</code><br>"
            f"💰 <b>Amount:</b> <code>₹{amount:.2f}</code></blockquote>"
            f"<blockquote>💳 <b>Sender Balance:</b> <code>₹{new_sender_balance:.2f}</code><br>"
            f"💳 <b>Recipient Balance:</b> <code>₹{recipient_new_balance:.2f}</code></blockquote>",
            parse_mode="HTML"
        )
    except Exception:
        pass

    await cq.answer()


# ================= Professional Interactive Admin Control Center =================
class AdminInteractive(StatesGroup):
    waiting_userinfo = State()
    waiting_gban = State()
    waiting_ungban = State()
    waiting_broadcast = State()
    waiting_sellcountry = State()
    waiting_user_discount = State()
    waiting_refer_bonus = State()
    waiting_refer_comm = State()
    waiting_add_admin = State()
    waiting_fsub_channel = State()


def admin_panel_markup() -> InlineKeyboardMarkup:
    features = get_feature_settings()
    smm = "ON ✅" if features.get("smm_enabled", True) else "OFF ❌"
    wa = "ON ✅" if features.get("whatsapp_enabled", True) else "OFF ❌"
    store = "ON ✅" if features.get("store_enabled", True) else "OFF ❌"
    maint = "ON 🚧" if features.get("maintenance_enabled", False) else "OFF 🟢"
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            premium_button(text=f"🚀 SMM: {smm}", callback_data="admin_toggle:smm"),
            premium_button(
                text=f"💬 WhatsApp: {wa}",
                callback_data="admin_toggle:whatsapp",
                icon_custom_emoji_id=WHATSAPP_EMOJI_ID,
            ),
        ],
        [
            premium_button(text=f"🏪 Store: {store}", callback_data="admin_toggle:store"),
            premium_button(text=f"🛠️ Maintenance: {maint}", callback_data="admin_toggle:maintenance"),
        ],
        [
            premium_button(text="🖥 Server Control", callback_data="admin_open:servers"),
            premium_button(text="💳 Payments", callback_data="ps:home"),
        ],
        [premium_button(text="📲 OTP Services", callback_data="otpa:home")],
        [
            premium_button(text="🏪 Store Control", callback_data="admin_open:store"),
            premium_button(text="🌐 SMM Control", callback_data="smm_adm_back"),
        ],
        [
            premium_button(
                text="✈️ Server 3 & LZT",
                callback_data="s3m_home",
                icon_custom_emoji_id=TELEGRAM_EMOJI_ID,
            ),
            premium_button(text="📊 Reports & Analytics", callback_data="admin_open:reports"),
        ],
        [premium_button(text="🤖 Bot Health", callback_data="admin_open:health")],
        [
            premium_button(text="👥 Users & Discounts", callback_data="admin_open:users"),
            premium_button(text="📦 Stock & Countries", callback_data="admin_open:stock"),
        ],
        [
            premium_button(text="🎁 Refer & Commission", callback_data="admin_open:refer"),
            premium_button(text="🎟 Redeem Codes", callback_data="admin_open:redeem"),
        ],
        [
            premium_button(text="👮 Manage Admins", callback_data="admin_open:admins"),
            premium_button(text="📢 Force-Join Channels", callback_data="admin_open:fsub"),
        ],
        [
            premium_button(text="📣 Broadcast", callback_data="admin_open:broadcast"),
            premium_button(text="🔄 Refresh Panel", callback_data="admin_refresh"),
        ],
    ])


async def render_admin_panel(message: Message, edit: bool = False):
    users = users_col.count_documents({})
    orders = orders_col.count_documents({})
    stock = numbers_col.count_documents({"used": {"$ne": True}})
    s3_count = s3_catalog.count_documents({"active": True})
    store_count = store_col.count_documents({"stock": {"$gt": 0}})
    features = get_feature_settings()
    sync_admin_ids()
    fsub_channels = get_force_sub_channels()
    fsub_state = f"ON ({len(fsub_channels)} ch)" if is_force_sub_enabled() else "DISABLED"
    refer_bonus = float(features.get("refer_bonus", 0.1) or 0.0)
    refer_comm = float(features.get("refer_commission_percent", 2.0) or 0.0)
    text = (
        f"<h2>{tg_icon()} TGA Store | API — Admin Control Center</h2>"
        "<blockquote>🛡️ <b>Live Platform Overview</b><br>"
        f"👥 <b>Registered Users:</b> <code>{users:,}</code> · 👮 <b>Admins:</b> <code>{len(ADMIN_IDS)}</code><br>"
        f"📦 <b>Completed Orders:</b> <code>{orders:,}</code><br>"
        f"📱 <b>Server 1 Stock:</b> <code>{stock:,}</code> · 🛰️ <b>S3 Catalog:</b> <code>{s3_count:,}</code><br>"
        f"🏪 <b>Store Products in Stock:</b> <code>{store_count:,}</code></blockquote>"
        "<blockquote>🎛️ <b>Global Menu &amp; System Switches</b><br>"
        f"• 🛠️ <b>Maintenance Mode:</b> <code>{'🚧 ACTIVE (Users Blocked)' if features.get('maintenance_enabled', False) else '🟢 OFF (Bot Live)'}</code><br>"
        f"• 🚀 <b>SMM Services:</b> <code>{'ONLINE' if features.get('smm_enabled', True) else 'DISABLED'}</code><br>"
        f"• {wa_icon()} <b>WhatsApp OTPs:</b> <code>{'ONLINE' if features.get('whatsapp_enabled', True) else 'DISABLED'}</code><br>"
        f"• 🏪 <b>Digital Store:</b> <code>{'ONLINE' if features.get('store_enabled', True) else 'DISABLED'}</code><br>"
        f"• 📢 <b>Force-Join Gate:</b> <code>{fsub_state}</code><br>"
        f"• 🎁 <b>Referral Rewards:</b> <code>₹{refer_bonus:.2f}/join + {refer_comm:g}% referral commission</code></blockquote>"
        "<p>👇 <i>Use the interactive controls below to manage every module or toggle user-menu services in real time:</i></p>"
    )
    if edit:
        await message.edit_text(text, reply_markup=admin_panel_markup())
    else:
        await message.answer(text, reply_markup=admin_panel_markup())


@dp.message(Command("admin"))
async def cmd_admin(msg: Message):
    if not is_admin(msg.from_user.id):
        return await msg.answer("⛔ Access denied.")
    await render_admin_panel(msg)


@dp.callback_query(F.data.in_(["admin_refresh", "admin_back"]))
async def admin_refresh(cq: CallbackQuery, state: FSMContext):
    if not is_admin(cq.from_user.id):
        return await cq.answer("Access denied", show_alert=True)
    await state.clear()
    await render_admin_panel(cq.message, edit=True)
    await cq.answer("Panel refreshed")


@dp.callback_query(F.data.startswith("admin_toggle:"))
async def admin_toggle_feature(cq: CallbackQuery):
    if not is_admin(cq.from_user.id):
        return await cq.answer("Access denied", show_alert=True)
    feature = cq.data.split(":", 1)[1]
    field = {
        "smm": "smm_enabled",
        "whatsapp": "whatsapp_enabled",
        "store": "store_enabled",
        "maintenance": "maintenance_enabled",
    }.get(feature)
    if not field:
        return await cq.answer("Unknown switch", show_alert=True)
    default_val = False if feature == "maintenance" else True
    current = get_feature_settings().get(field, default_val)
    new_val = not current
    settings_col.update_one(
        {"_id": "bot_features"}, {"$set": {field: new_val}}, upsert=True
    )
    if feature == "store":
        settings_col.update_one(
            {"_id": "server_config"},
            {"$set": {"store_status": "on" if new_val else "off"}},
            upsert=True,
        )
    await render_admin_panel(cq.message, edit=True)
    await cq.answer(f"{feature.title()} {'enabled' if new_val else 'disabled'}")


@dp.callback_query(F.data.startswith("admin_open:"))
async def admin_open_section(cq: CallbackQuery, state: FSMContext):
    if not is_admin(cq.from_user.id):
        return await cq.answer("Access denied", show_alert=True)
    await state.clear()
    section = cq.data.split(":", 1)[1]

    if section == "servers":
        await cq.answer("Loading server status...")
        await send_server_dashboard(cq.message.chat.id, cq.message.message_id)
        return

    if section == "health":
        await cq.answer("Checking bot health...")
        health = await get_live_bot_status()
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Refresh Health", callback_data="admin_open:health")],
            [InlineKeyboardButton(text="📊 Open Analytics", callback_data="admin_open:reports")],
            [InlineKeyboardButton(text="⬅️ Admin Panel", callback_data="admin_refresh")],
        ])
        await cq.message.edit_text(render_bot_health(health), parse_mode="HTML", reply_markup=kb)
        return

    if section == "store":
        await cq.answer()
        await render_store_dashboard(cq.message, edit=True)
        return

    if section == "users":
        total_users = users_col.count_documents({})
        banned_users = users_col.count_documents({"banned": True})
        funded_users = users_col.count_documents({"balance": {"$gt": 0}})
        discounted_users = users_col.count_documents({"discount_percent": {"$gt": 0}})
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="🔍 Inspect User", callback_data="adm_usr:info"),
                InlineKeyboardButton(text="🏷️ Set User Discount %", callback_data="adm_usr:discount"),
            ],
            [
                InlineKeyboardButton(text="➕ Credit Balance", callback_data="adm_usr:credit"),
                InlineKeyboardButton(text="➖ Debit Balance", callback_data="adm_usr:debit"),
            ],
            [
                InlineKeyboardButton(text="🚫 Ban User", callback_data="adm_usr:gban"),
                InlineKeyboardButton(text="✅ Unban User", callback_data="adm_usr:ungban"),
            ],
            [
                InlineKeyboardButton(text="🏆 Top Spenders", callback_data="adm_rep:topusers:1"),
                InlineKeyboardButton(text="💎 Live User Credits", callback_data="livecredits:0"),
            ],
            [InlineKeyboardButton(text="⬅️ Admin Panel", callback_data="admin_refresh")],
        ])
        await cq.message.edit_text(
            "<h2>👥 Users, Wallets &amp; VIP Discounts</h2>"
            f"<blockquote>👥 <b>Total Users:</b> <code>{total_users:,}</code><br>"
            f"💰 <b>Users with Balance:</b> <code>{funded_users:,}</code><br>"
            f"🏷️ <b>VIP Discount Users:</b> <code>{discounted_users:,}</code><br>"
            f"🚫 <b>Banned Accounts:</b> <code>{banned_users:,}</code></blockquote>"
            "<p>👇 <i>Select an action below (or use <code>/userinfo</code>, <code>/setdiscount</code>, <code>/credit</code>, <code>/debit</code>, <code>/gban</code>, <code>/ungban</code>):</i></p>",
            reply_markup=kb,
        )
        await cq.answer()
        return

    if section == "refer":
        await render_admin_refer_menu(cq.message, edit=True)
        await cq.answer()
        return

    if section == "admins":
        await render_admin_managers_menu(cq.message, edit=True)
        await cq.answer()
        return

    if section == "fsub":
        await render_admin_fsub_menu(cq.message, edit=True)
        await cq.answer()
        return

    if section == "stock":
        total_countries = countries_col.count_documents({})
        unused_numbers = numbers_col.count_documents({"used": {"$ne": True}})
        sell_rates_count = sell_prices_col.count_documents({})
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="➕ Add TG Number", callback_data="adm_stk:add"),
                InlineKeyboardButton(text="🗑️ Remove TG Number", callback_data="adm_stk:remove"),
            ],
            [
                InlineKeyboardButton(text="📚 View DB Stock", callback_data="adm_stk:db"),
                InlineKeyboardButton(text="🌍 Add Country", callback_data="adm_stk:addcountry"),
            ],
            [
                InlineKeyboardButton(text="✏️ Edit Country", callback_data="adm_stk:editcountry"),
                InlineKeyboardButton(text="🗑️ Remove Country", callback_data="adm_stk:removecountry"),
            ],
            [
                InlineKeyboardButton(text="💵 Edit Sell Rates", callback_data="adm_stk:editsell"),
                InlineKeyboardButton(text="📋 Bulk Sell Countries", callback_data="adm_stk:sellcountry"),
            ],
            [InlineKeyboardButton(text="⬅️ Admin Panel", callback_data="admin_refresh")],
        ])
        await cq.message.edit_text(
            "<h2>📦 Stock &amp; Country Controls</h2>"
            f"<blockquote>🌍 <b>Server 1 Countries:</b> <code>{total_countries:,}</code><br>"
            f"📱 <b>Available Numbers:</b> <code>{unused_numbers:,}</code><br>"
            f"💵 <b>Sell Rate Entries:</b> <code>{sell_rates_count:,}</code></blockquote>"
            "<p>👇 <i>Choose a stock or country management action below:</i></p>",
            reply_markup=kb,
        )
        await cq.answer()
        return

    if section == "redeem":
        total_codes = redeem_col.count_documents({})
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="➕ Create Custom Code", callback_data="adm_red:create"),
                InlineKeyboardButton(text="🎟️ Quick Random Code", callback_data="adm_red:random"),
            ],
            [
                InlineKeyboardButton(text="📋 View Active Codes", callback_data="adm_red:list"),
            ],
            [InlineKeyboardButton(text="⬅️ Admin Panel", callback_data="admin_refresh")],
        ])
        await cq.message.edit_text(
            "<h2>🎟️ Redeem Code Manager</h2>"
            f"<blockquote>🎁 <b>Total Active Codes:</b> <code>{total_codes:,}</code></blockquote>"
            "<p>👇 <i>Create custom or random promo codes and monitor claims below:</i></p>",
            reply_markup=kb,
        )
        await cq.answer()
        return

    if section == "reports":
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📊 Full Sales & Revenue Report", callback_data="adm_rep:sales")],
            [
                InlineKeyboardButton(text="🏆 Top Spenders", callback_data="adm_rep:topusers:1"),
                InlineKeyboardButton(text="💎 Live User Credits", callback_data="livecredits:0"),
            ],
            [InlineKeyboardButton(text="🤖 Bot Health", callback_data="admin_open:health")],
            [InlineKeyboardButton(text="⬅️ Admin Panel", callback_data="admin_refresh")],
        ])
        await cq.message.edit_text(
            "<h2>📊 Reports &amp; System Analytics</h2>"
            "<blockquote>📈 <b>Real-Time Financial &amp; Order Intelligence</b><br>"
            "Inspect daily/weekly revenue, top spending customers, and active wallet liabilities.</blockquote>"
            "<p>👇 <i>Select a report below:</i></p>",
            reply_markup=kb,
        )
        await cq.answer()
        return

    if section == "broadcast":
        await state.set_state(AdminInteractive.waiting_broadcast)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Cancel & Back", callback_data="admin_refresh")]
        ])
        await cq.message.edit_text(
            "<h2>📣 Admin Broadcast Center</h2>"
            "<blockquote>📨 <b>Send or Forward Any Message Now</b><br>"
            "Send or forward the message (text, photo, video, or announcement) you want to broadcast to all registered users.<br><br>"
            "💡 <i>Tip: You can also reply to any message anytime with <code>/broadcast</code>.</i></blockquote>",
            reply_markup=kb,
        )
        await cq.answer()
        return


# ================= Interactive Admin: Users & Balance Callbacks =================
@dp.callback_query(F.data.startswith("adm_usr:"))
async def admin_users_action(cq: CallbackQuery, state: FSMContext):
    if not is_admin(cq.from_user.id):
        return await cq.answer("Access denied", show_alert=True)
    parts = cq.data.split(":")
    action = parts[1]
    back_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Back to Users", callback_data="admin_open:users")]
    ])

    if action == "info":
        await state.set_state(AdminInteractive.waiting_userinfo)
        await cq.message.edit_text(
            "<h2>🔍 Inspect User Profile</h2>"
            "<blockquote>Send the <b>Telegram User ID</b> or <b>@username</b> to inspect:</blockquote>",
            reply_markup=back_kb,
        )
    elif action == "discount":
        await state.set_state(AdminInteractive.waiting_user_discount)
        await state.update_data(discount_target_id=None)
        await cq.message.edit_text(
            "<h2>🏷️ Set Per-User VIP Discount %</h2>"
            "<blockquote>Send <b>User ID</b> and <b>Discount % (0–100)</b> separated by a comma or space:<br><br>"
            "• Example (10% discount): <code>123456789, 10</code><br>"
            "• Example (Remove discount): <code>123456789, 0</code><br><br>"
            "💡 <i>This user will automatically see and pay discounted prices across all servers, Store, and SMM!</i></blockquote>",
            reply_markup=back_kb,
        )
    elif action == "discount_for" and len(parts) > 2:
        target_id = int(parts[2])
        cur_disc = get_user_discount(target_id)
        await state.set_state(AdminInteractive.waiting_user_discount)
        await state.update_data(discount_target_id=target_id)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="0% (Reset)", callback_data=f"adm_usr:set_disc:{target_id}:0"),
                InlineKeyboardButton(text="5% OFF", callback_data=f"adm_usr:set_disc:{target_id}:5"),
                InlineKeyboardButton(text="10% OFF", callback_data=f"adm_usr:set_disc:{target_id}:10"),
            ],
            [
                InlineKeyboardButton(text="15% OFF", callback_data=f"adm_usr:set_disc:{target_id}:15"),
                InlineKeyboardButton(text="20% OFF", callback_data=f"adm_usr:set_disc:{target_id}:20"),
                InlineKeyboardButton(text="25% OFF", callback_data=f"adm_usr:set_disc:{target_id}:25"),
            ],
            [InlineKeyboardButton(text="⬅️ Back to User", callback_data=f"adm_usr:view:{target_id}")],
        ])
        await cq.message.edit_text(
            "<h2>🏷️ Set User VIP Discount</h2>"
            f"<blockquote>👤 <b>Target User:</b> <code>{target_id}</code><br>"
            f"🏷️ <b>Current Discount:</b> <code>{cur_disc:g}% OFF</code></blockquote>"
            "<p>👇 <i>Tap a preset percentage below, or type any custom discount % (0–100) in chat:</i></p>",
            reply_markup=kb,
        )
    elif action == "set_disc" and len(parts) > 3:
        target_id = int(parts[2])
        pct = max(0.0, min(100.0, float(parts[3])))
        await state.clear()
        users_col.update_one({"_id": target_id}, {"$set": {"discount_percent": pct}}, upsert=True)
        await cq.answer(f"✅ Discount set to {pct:g}% for {target_id}!", show_alert=True)
        text, kb = build_userinfo_card(target_id)
        if text:
            await cq.message.edit_text(text, reply_markup=kb)
        return
    elif action == "view" and len(parts) > 2:
        target_id = int(parts[2])
        await state.clear()
        text, kb = build_userinfo_card(target_id)
        if text:
            await cq.message.edit_text(text, reply_markup=kb)
    elif action == "credit":
        await state.set_state("credit_waiting")
        await cq.message.edit_text(
            "<h2>➕ Credit User Wallet</h2>"
            "<blockquote>Send <b>User ID</b> and <b>Amount (₹)</b> separated by a comma:<br>"
            "Example: <code>123456789,50</code></blockquote>",
            reply_markup=back_kb,
        )
    elif action == "debit":
        await state.set_state("debit_waiting")
        await cq.message.edit_text(
            "<h2>➖ Debit User Wallet</h2>"
            "<blockquote>Send <b>User ID</b> and <b>Amount (₹)</b> separated by a comma:<br>"
            "Example: <code>123456789,50</code></blockquote>",
            reply_markup=back_kb,
        )
    elif action == "gban":
        await state.set_state(AdminInteractive.waiting_gban)
        await cq.message.edit_text(
            "<h2>🚫 Ban User</h2>"
            "<blockquote>Send the <b>Telegram User ID</b> or <b>@username</b> to ban from the bot:</blockquote>",
            reply_markup=back_kb,
        )
    elif action == "ungban":
        await state.set_state(AdminInteractive.waiting_ungban)
        await cq.message.edit_text(
            "<h2>✅ Unban User</h2>"
            "<blockquote>Send the <b>Telegram User ID</b> or <b>@username</b> to unban:</blockquote>",
            reply_markup=back_kb,
        )
    elif action == "quick_ban" and len(parts) > 2:
        target_id = int(parts[2])
        user_doc = users_col.find_one({"_id": target_id}) or {}
        new_ban = not user_doc.get("banned", False)
        users_col.update_one({"_id": target_id}, {"$set": {"banned": new_ban}}, upsert=True)
        await cq.answer(f"User {'BANNED' if new_ban else 'UNBANNED'}!", show_alert=True)
        text, kb = build_userinfo_card(target_id)
        if text:
            await cq.message.edit_text(text, reply_markup=kb)
        return
    await cq.answer()


def build_userinfo_card(target_id: int) -> tuple[str | None, InlineKeyboardMarkup | None]:
    user = users_col.find_one({"_id": target_id})
    if not user:
        return None, None
    total_deposited = sum(
        float(t.get("amount", 0.0))
        for t in db["transactions"].find({"user_id": target_id, "status": {"$in": ["approved", "paid", "success"]}})
    )
    number_orders = list(orders_col.find({"user_id": target_id}))
    smm_order_docs = list(db["smm_orders"].find({"user_id": target_id}))
    lzt_order_docs = list(db["lzt_orders"].find({"user_id": target_id}))
    total_spent = (
        sum(float(o.get("price", 0.0)) for o in number_orders)
        + sum(float(o.get("price", 0.0)) for o in smm_order_docs)
        + sum(float(o.get("price_inr", 0.0)) for o in lzt_order_docs)
    )
    total_orders = len(number_orders) + len(smm_order_docs) + len(lzt_order_docs)
    referred_by = user.get("referred_by")
    referral_earnings = user.get("referral_earnings", 0.0)
    total_referred = users_col.count_documents({"referred_by": target_id})
    joined_at = user.get("joined_at")
    joined_str = joined_at.strftime("%d %b %Y, %H:%M") if joined_at else "N/A"
    is_banned = user.get("banned", False)
    disc_pct = get_user_discount(user)

    text = (
        "<h2>👤 Full User Profile</h2>"
        f"<blockquote>🆔 <b>User ID:</b> <code>{target_id}</code><br>"
        f"📛 <b>Username:</b> @{user.get('username') or 'NoUsername'}<br>"
        f"🛡️ <b>Account Status:</b> <code>{'🚫 BANNED' if is_banned else '🟢 ACTIVE'}</code><br>"
        f"🏷️ <b>VIP Discount:</b> <code>{disc_pct:g}% OFF</code><br>"
        f"📅 <b>Joined:</b> <code>{joined_str}</code></blockquote>"
        f"<blockquote>💳 <b>Current Balance:</b> <code>₹{user.get('balance', 0.0):.2f}</code><br>"
        f"💰 <b>Total Deposited:</b> <code>₹{total_deposited:.2f}</code><br>"
        f"🛒 <b>Total Spent:</b> <code>₹{total_spent:.2f}</code><br>"
        f"📦 <b>Total Orders:</b> <code>{total_orders}</code></blockquote>"
        f"<blockquote>🔗 <b>Referred By:</b> {f'<code>{referred_by}</code>' if referred_by else 'None'}<br>"
        f"👥 <b>Total Referred:</b> <code>{total_referred}</code><br>"
        f"💵 <b>Referral Earnings:</b> <code>₹{referral_earnings:.2f}</code></blockquote>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="➕ Credit", callback_data="adm_usr:credit"),
            InlineKeyboardButton(text="➖ Debit", callback_data="adm_usr:debit"),
        ],
        [
            InlineKeyboardButton(
                text=f"🏷️ Set Discount ({disc_pct:g}%)",
                callback_data=f"adm_usr:discount_for:{target_id}",
            ),
            InlineKeyboardButton(
                text="✅ Unban User" if is_banned else "🚫 Ban User",
                callback_data=f"adm_usr:quick_ban:{target_id}",
            ),
        ],
        [InlineKeyboardButton(text="⬅️ Back to Users", callback_data="admin_open:users")],
    ])
    return text, kb


@dp.message(StateFilter(AdminInteractive.waiting_userinfo))
async def handle_interactive_userinfo(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    raw = (msg.text or "").strip()
    target_id = None
    if raw.isdigit():
        target_id = int(raw)
    elif raw.startswith("@"):
        u = users_col.find_one({"username": raw[1:]})
        if u:
            target_id = u["_id"]
    await state.clear()
    back_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Back to Users", callback_data="admin_open:users")]
    ])
    if not target_id:
        return await msg.answer("<h2>❌ User Not Found</h2><blockquote>Please send a valid numeric User ID or known @username.</blockquote>", reply_markup=back_kb)
    text, kb = build_userinfo_card(target_id)
    if not text:
        return await msg.answer("<h2>❌ User Not Found</h2><blockquote>No user exists with that ID.</blockquote>", reply_markup=back_kb)
    await msg.answer(text, reply_markup=kb)


@dp.message(StateFilter(AdminInteractive.waiting_user_discount))
async def handle_interactive_user_discount(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    if msg.text and msg.text.startswith("/"):
        await state.clear()
        return
    data = await state.get_data()
    preset_target = data.get("discount_target_id")
    raw = (msg.text or "").strip()
    back_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Back to Users", callback_data="admin_open:users")]
    ])
    try:
        if preset_target:
            target_id = int(preset_target)
            pct = float(raw.replace("%", "").strip())
        else:
            parts = [p.strip() for p in re.split(r"[,:\s]+", raw) if p.strip()]
            if len(parts) < 2:
                raise ValueError("Need User ID and percentage")
            target_id = int(parts[0])
            pct = float(parts[1].replace("%", ""))
    except Exception:
        return await msg.answer(
            "<h2>❌ Invalid Format</h2>"
            "<blockquote>Send <code>USER_ID, PERCENT</code> (for example <code>123456789, 10</code>) or just the percentage if editing a selected user.</blockquote>",
            reply_markup=back_kb,
        )

    pct = max(0.0, min(100.0, pct))
    await state.clear()
    users_col.update_one({"_id": target_id}, {"$set": {"discount_percent": pct}}, upsert=True)
    try:
        await bot.send_message(
            target_id,
            "<h2>🏷️ VIP Discount Updated!</h2>"
            f"<blockquote>🎉 An administrator has updated your special store-wide discount to <b>{pct:g}% OFF</b>!<br>"
            "All prices across Telegram Accounts, WhatsApp OTPs, Digital Store, and SMM are automatically discounted for your account.</blockquote>",
            parse_mode="HTML",
        )
    except Exception:
        pass
    await msg.answer(
        "<h2>✅ User VIP Discount Saved</h2>"
        f"<blockquote>👤 <b>User ID:</b> <code>{target_id}</code><br>"
        f"🏷️ <b>New Discount:</b> <code>{pct:g}% OFF</code></blockquote>",
        reply_markup=back_kb,
    )


@dp.message(Command("setdiscount"))
async def cmd_setdiscount(msg: Message):
    if not is_admin(msg.from_user.id):
        return await msg.answer("⛔ Access denied.")
    parts = (msg.text or "").split()
    if len(parts) < 3:
        return await msg.answer(
            "<h2>🏷️ Set User Discount</h2>"
            "<blockquote><b>Usage:</b> <code>/setdiscount &lt;user_id&gt; &lt;percent&gt;</code><br>"
            "<b>Example:</b> <code>/setdiscount 123456789 10</code></blockquote>"
        )
    try:
        target_id = int(parts[1])
        pct = max(0.0, min(100.0, float(parts[2].replace("%", ""))))
    except ValueError:
        return await msg.answer("<h2>❌ Invalid Input</h2><blockquote>Provide a numeric User ID and discount percentage (0–100).</blockquote>")
    users_col.update_one({"_id": target_id}, {"$set": {"discount_percent": pct}}, upsert=True)
    await msg.answer(
        "<h2>✅ User Discount Updated</h2>"
        f"<blockquote>👤 <b>User ID:</b> <code>{target_id}</code><br>"
        f"🏷️ <b>Discount:</b> <code>{pct:g}% OFF</code></blockquote>"
    )


@dp.message(Command("maintenance"))
async def cmd_maintenance(msg: Message):
    if not is_admin(msg.from_user.id):
        return await msg.answer("⛔ Access denied.")
    parts = (msg.text or "").split()
    current = bool(get_feature_settings().get("maintenance_enabled", False))
    if len(parts) > 1 and parts[1].lower() in ("on", "true", "1"):
        new_val = True
    elif len(parts) > 1 and parts[1].lower() in ("off", "false", "0"):
        new_val = False
    else:
        new_val = not current
    settings_col.update_one(
        {"_id": "bot_features"}, {"$set": {"maintenance_enabled": new_val}}, upsert=True
    )
    await msg.answer(
        f"<h2>🛠️ Maintenance Mode: {'ON 🚧' if new_val else 'OFF 🟢'}</h2>"
        f"<blockquote>{'Non-admin users are now blocked with a maintenance notice while admins retain full access.' if new_val else 'The bot is now live for all users.'}</blockquote>"
    )


# ================= Interactive Admin: Referral & Commission =================
async def render_admin_refer_menu(message: Message, edit: bool = False):
    features = get_feature_settings()
    refer_bonus = float(features.get("refer_bonus", 0.1) or 0.0)
    refer_comm = float(features.get("refer_commission_percent", 2.0) or 0.0)
    total_referrals = users_col.count_documents({"referred_by": {"$ne": None}})
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=f"✏️ Edit Join Bonus (₹{refer_bonus:.2f})", callback_data="adm_ref:bonus"),
            InlineKeyboardButton(text=f"✏️ Edit Referral Comm ({refer_comm:g}%)", callback_data="adm_ref:comm"),
        ],
        [
            InlineKeyboardButton(text="🎁 ₹0.10/ref", callback_data="adm_ref:set_bonus:0.1"),
            InlineKeyboardButton(text="🎁 ₹0.50/ref", callback_data="adm_ref:set_bonus:0.5"),
            InlineKeyboardButton(text="🎁 ₹1.00/ref", callback_data="adm_ref:set_bonus:1.0"),
        ],
        [
            InlineKeyboardButton(text="💎 1% Comm", callback_data="adm_ref:set_comm:1"),
            InlineKeyboardButton(text="💎 2% Comm", callback_data="adm_ref:set_comm:2"),
            InlineKeyboardButton(text="💎 5% Comm", callback_data="adm_ref:set_comm:5"),
        ],
        [InlineKeyboardButton(text="⬅️ Admin Panel", callback_data="admin_refresh")],
    ])
    text = (
        "<h2>🎁 Refer &amp; Earn — Referral Commission Control</h2>"
        f"<blockquote>🎁 <b>Instant Join Bonus:</b> <code>₹{refer_bonus:.2f}</code> per verified referral<br>"
        f"💎 <b>Referral Commission:</b> <code>{refer_comm:g}%</code> of every deposit made by a referred user — paid only to the referrer<br>"
        f"👥 <b>Total Referred Users:</b> <code>{total_referrals:,}</code></blockquote>"
        "<p>👇 <i>Tap a button below to customize the join bonus or Referral Commission percentage (or use <code>/setreferbonus</code> and <code>/setrefercomm</code>):</i></p>"
    )
    if edit:
        await message.edit_text(text, reply_markup=kb)
    else:
        await message.answer(text, reply_markup=kb)


@dp.callback_query(F.data.startswith("adm_ref:"))
async def admin_refer_action(cq: CallbackQuery, state: FSMContext):
    if not is_admin(cq.from_user.id):
        return await cq.answer("Access denied", show_alert=True)
    parts = cq.data.split(":")
    action = parts[1]
    back_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Back to Refer Settings", callback_data="admin_open:refer")]
    ])
    if action == "bonus":
        await state.set_state(AdminInteractive.waiting_refer_bonus)
        await cq.message.edit_text(
            "<h2>🎁 Set Referral Join Bonus (₹)</h2>"
            "<blockquote>Send the new <b>Referral Join Bonus in INR</b> (e.g. <code>0.1</code>, <code>0.5</code>, <code>1</code>, or <code>0</code> to disable):</blockquote>",
            reply_markup=back_kb,
        )
    elif action == "comm":
        await state.set_state(AdminInteractive.waiting_refer_comm)
        await cq.message.edit_text(
            "<h2>💎 Set Referral Commission (%)</h2>"
            "<blockquote>Send the new <b>Referral Commission percentage</b> — the referrer earns this % of every deposit made by users they referred (e.g. <code>2</code> for 2%, <code>5</code> for 5%, or <code>0</code> to disable):</blockquote>",
            reply_markup=back_kb,
        )
    elif action == "set_bonus" and len(parts) > 2:
        val = max(0.0, float(parts[2]))
        settings_col.update_one({"_id": "bot_features"}, {"$set": {"refer_bonus": val}}, upsert=True)
        await cq.answer(f"✅ Join Bonus set to ₹{val:.2f}!", show_alert=True)
        await render_admin_refer_menu(cq.message, edit=True)
        return
    elif action == "set_comm" and len(parts) > 2:
        val = max(0.0, min(100.0, float(parts[2])))
        settings_col.update_one({"_id": "bot_features"}, {"$set": {"refer_commission_percent": val}}, upsert=True)
        await cq.answer(f"✅ Referral Commission set to {val:g}%!", show_alert=True)
        await render_admin_refer_menu(cq.message, edit=True)
        return
    await cq.answer()


@dp.message(StateFilter(AdminInteractive.waiting_refer_bonus))
async def handle_interactive_refer_bonus(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    try:
        val = max(0.0, float((msg.text or "").replace("₹", "").strip()))
    except ValueError:
        return await msg.answer("<h2>❌ Invalid Amount</h2><blockquote>Send a valid number like <code>0.1</code> or <code>1.0</code>.</blockquote>")
    await state.clear()
    settings_col.update_one({"_id": "bot_features"}, {"$set": {"refer_bonus": val}}, upsert=True)
    await render_admin_refer_menu(msg, edit=False)


@dp.message(StateFilter(AdminInteractive.waiting_refer_comm))
async def handle_interactive_refer_comm(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    try:
        val = max(0.0, min(100.0, float((msg.text or "").replace("%", "").strip())))
    except ValueError:
        return await msg.answer("<h2>❌ Invalid Percentage</h2><blockquote>Send a valid percentage like <code>2</code> or <code>5</code>.</blockquote>")
    await state.clear()
    settings_col.update_one({"_id": "bot_features"}, {"$set": {"refer_commission_percent": val}}, upsert=True)
    await render_admin_refer_menu(msg, edit=False)


@dp.message(Command("setreferbonus"))
async def cmd_setreferbonus(msg: Message):
    if not is_admin(msg.from_user.id):
        return await msg.answer("⛔ Access denied.")
    parts = (msg.text or "").split()
    if len(parts) < 2:
        return await msg.answer("<h2>🎁 Set Refer Bonus</h2><blockquote><b>Usage:</b> <code>/setreferbonus 0.1</code></blockquote>")
    try:
        val = max(0.0, float(parts[1]))
    except ValueError:
        return await msg.answer("<h2>❌ Invalid Amount</h2>")
    settings_col.update_one({"_id": "bot_features"}, {"$set": {"refer_bonus": val}}, upsert=True)
    await msg.answer(f"<h2>✅ Referral Join Bonus Updated</h2><blockquote>New Join Bonus: <code>₹{val:.2f}</code> per referral.</blockquote>")


@dp.message(Command("setrefercomm", "setreferralcommission"))
async def cmd_setrefercomm(msg: Message):
    if not is_admin(msg.from_user.id):
        return await msg.answer("⛔ Access denied.")
    parts = (msg.text or "").split()
    if len(parts) < 2:
        return await msg.answer("<h2>💎 Set Referral Commission</h2><blockquote><b>Usage:</b> <code>/setrefercomm 2</code><br>The referrer earns this % of every deposit made by users they referred.</blockquote>")
    try:
        val = max(0.0, min(100.0, float(parts[1].replace("%", ""))))
    except ValueError:
        return await msg.answer("<h2>❌ Invalid Percentage</h2>")
    settings_col.update_one({"_id": "bot_features"}, {"$set": {"refer_commission_percent": val}}, upsert=True)
    await msg.answer(f"<h2>✅ Referral Commission Updated</h2><blockquote>New Referral Commission: <code>{val:g}%</code> of every referred user's deposit, paid to the referrer.</blockquote>")


# ================= Interactive Admin: Manage Admins =================
async def render_admin_managers_menu(message: Message, edit: bool = False):
    sync_admin_ids()
    doc = settings_col.find_one({"_id": "admin_config"}) or {}
    extra = [int(x) for x in doc.get("extra_admins", []) if str(x).lstrip("-").isdigit()]

    lines = []
    for uid in ADMIN_IDS:
        role = "👑 Root Admin" if uid in ROOT_ADMIN_IDS else "👮 Added Admin"
        lines.append(f"• <code>{uid}</code> — <b>{role}</b>")
    admin_list_html = "<br>".join(lines) if lines else "None"

    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="➕ Add New Admin", callback_data="adm_mgr:add"))
    for uid in extra:
        kb.row(InlineKeyboardButton(text=f"🗑️ Remove Admin {uid}", callback_data=f"adm_mgr:rm:{uid}"))
    kb.row(InlineKeyboardButton(text="⬅️ Admin Panel", callback_data="admin_refresh"))

    text = (
        "<h2>👮 Admin Access Management</h2>"
        f"<blockquote>🛡️ <b>Active Administrators ({len(ADMIN_IDS)}):</b><br>{admin_list_html}</blockquote>"
        "<p>👇 <i>Add a new admin or tap an added admin below to revoke access (or use <code>/addadmin</code> and <code>/removeadmin</code>):</i></p>"
    )
    if edit:
        await message.edit_text(text, reply_markup=kb.as_markup())
    else:
        await message.answer(text, reply_markup=kb.as_markup())


@dp.callback_query(F.data.startswith("adm_mgr:"))
async def admin_managers_action(cq: CallbackQuery, state: FSMContext):
    if not is_admin(cq.from_user.id):
        return await cq.answer("Access denied", show_alert=True)
    parts = cq.data.split(":")
    action = parts[1]
    if action == "add":
        await state.set_state(AdminInteractive.waiting_add_admin)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Back to Admins", callback_data="admin_open:admins")]
        ])
        await cq.message.edit_text(
            "<h2>➕ Add New Administrator</h2>"
            "<blockquote>Send the numeric <b>Telegram User ID</b> of the user you want to grant Admin access to:</blockquote>",
            reply_markup=kb,
        )
    elif action == "rm" and len(parts) > 2:
        target_id = int(parts[2])
        ok, msg_txt = remove_admin_id(target_id)
        await cq.answer(msg_txt, show_alert=True)
        await render_admin_managers_menu(cq.message, edit=True)
        return
    await cq.answer()


@dp.message(StateFilter(AdminInteractive.waiting_add_admin))
async def handle_interactive_add_admin(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    raw = (msg.text or "").strip()
    if not raw.isdigit():
        return await msg.answer("<h2>❌ Invalid User ID</h2><blockquote>Please send a numeric Telegram User ID.</blockquote>")
    await state.clear()
    add_admin_id(int(raw))
    await render_admin_managers_menu(msg, edit=False)


@dp.message(Command("addadmin"))
async def cmd_addadmin(msg: Message):
    if not is_admin(msg.from_user.id):
        return await msg.answer("⛔ Access denied.")
    parts = (msg.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        return await msg.answer("<h2>👮 Add Admin</h2><blockquote><b>Usage:</b> <code>/addadmin &lt;user_id&gt;</code></blockquote>")
    uid = int(parts[1])
    add_admin_id(uid)
    await msg.answer(f"<h2>✅ Admin Added</h2><blockquote>User <code>{uid}</code> now has admin access.</blockquote>")


@dp.message(Command("removeadmin"))
async def cmd_removeadmin(msg: Message):
    if not is_admin(msg.from_user.id):
        return await msg.answer("⛔ Access denied.")
    parts = (msg.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        return await msg.answer("<h2>👮 Remove Admin</h2><blockquote><b>Usage:</b> <code>/removeadmin &lt;user_id&gt;</code></blockquote>")
    ok, note = remove_admin_id(int(parts[1]))
    await msg.answer(f"<h2>{'✅' if ok else '❌'} {escape(note)}</h2>")


# ================= Interactive Admin: Force-Join Channels =================
async def render_admin_fsub_menu(message: Message, edit: bool = False):
    channels = get_force_sub_channels()
    enabled = is_force_sub_enabled()
    lines = []
    for idx, ch in enumerate(channels):
        lines.append(
            f"{idx + 1}. <b>{escape(str(ch.get('name', 'Channel')))}</b> — <code>{escape(str(ch.get('id')))}</code>\n"
            f"   🔗 {escape(str(ch.get('link', '')))}"
        )
    ch_html = "\n".join(lines) if lines else "<i>No channels configured.</i>"

    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(
            text=f"🔒 Force-Join: {'ON ✅' if enabled else 'OFF ❌'}",
            callback_data="adm_fsub:toggle",
        ),
        InlineKeyboardButton(text="➕ Add Channel", callback_data="adm_fsub:add"),
    )
    for idx, ch in enumerate(channels):
        kb.row(
            InlineKeyboardButton(
                text=f"🗑️ Remove: {ch.get('name', 'Channel')} ({ch.get('id')})",
                callback_data=f"adm_fsub:rm:{idx}",
            )
        )
    kb.row(InlineKeyboardButton(text="⬅️ Admin Panel", callback_data="admin_refresh"))

    text = (
        "<h2>📢 Force-Join Channel Manager</h2>"
        f"<blockquote>🔒 <b>Gate Status:</b> <code>{'ENABLED ✅' if enabled else 'DISABLED ❌'}</code>\n"
        f"📡 <b>Required Channels ({len(channels)}):</b>\n{ch_html}</blockquote>"
        "<p>👇 <i>Add or remove mandatory join channels below (make sure the bot is an Admin in each channel):</i></p>"
    )
    if edit:
        await message.edit_text(text, reply_markup=kb.as_markup())
    else:
        await message.answer(text, reply_markup=kb.as_markup())


@dp.callback_query(F.data.startswith("adm_fsub:"))
async def admin_fsub_action(cq: CallbackQuery, state: FSMContext):
    if not is_admin(cq.from_user.id):
        return await cq.answer("Access denied", show_alert=True)
    parts = cq.data.split(":")
    action = parts[1]
    if action == "toggle":
        new_state = toggle_force_sub()
        await cq.answer(f"Force-Join {'ENABLED' if new_state else 'DISABLED'}", show_alert=True)
        await render_admin_fsub_menu(cq.message, edit=True)
        return
    elif action == "add":
        await state.set_state(AdminInteractive.waiting_fsub_channel)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Back to Channels", callback_data="admin_open:fsub")]
        ])
        await cq.message.edit_text(
            "<h2>➕ Add Force-Join Channel</h2>"
            "<blockquote>Send the channel details in this format:<br>"
            "<code>Button Name | Chat ID or @username | Invite Link</code><br><br>"
            "<b>Examples:</b><br>"
            "• <code>Updates | -1001234567890 | https://t.me/+AbCdEfGhIjK</code><br>"
            "• <code>Main Channel | @mychannel | https://t.me/mychannel</code></blockquote>"
            "<p>⚠️ <i>Ensure the bot is promoted as an Administrator in the channel so it can verify members.</i></p>",
            reply_markup=kb,
        )
    elif action == "rm" and len(parts) > 2:
        idx = int(parts[2])
        ok, removed = remove_force_sub_channel(idx)
        await cq.answer(
            f"Removed {removed.get('name')}" if ok and removed else "Channel not found",
            show_alert=True,
        )
        await render_admin_fsub_menu(cq.message, edit=True)
        return
    await cq.answer()


@dp.message(StateFilter(AdminInteractive.waiting_fsub_channel))
async def handle_interactive_fsub_add(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    raw = (msg.text or "").strip()
    parts = [p.strip() for p in (raw.split("|") if "|" in raw else raw.split(",")) if p.strip()]
    if len(parts) < 2:
        return await msg.answer(
            "<h2>❌ Invalid Format</h2>"
            "<blockquote>Send: <code>Name | Chat_ID_or_@username | https://t.me/link</code></blockquote>"
        )
    name = parts[0]
    chat_id = parts[1]
    link = parts[2] if len(parts) > 2 else (f"https://t.me/{chat_id.lstrip('@')}" if not chat_id.lstrip("-").isdigit() else f"https://t.me/{UPDATES}")
    await state.clear()
    add_force_sub_channel(name, chat_id, link)
    await render_admin_fsub_menu(msg, edit=False)


@dp.message(Command("addchannel"))
async def cmd_addchannel(msg: Message):
    if not is_admin(msg.from_user.id):
        return await msg.answer("⛔ Access denied.")
    raw = (msg.text or "").partition(" ")[2].strip()
    parts = [p.strip() for p in (raw.split("|") if "|" in raw else raw.split()) if p.strip()]
    if len(parts) < 2:
        return await msg.answer(
            "<h2>📢 Add Force-Join Channel</h2>"
            "<blockquote><b>Usage:</b> <code>/addchannel Name | -1001234567890 | https://t.me/link</code></blockquote>"
        )
    name = parts[0]
    chat_id = parts[1]
    link = parts[2] if len(parts) > 2 else f"https://t.me/{chat_id.lstrip('@')}"
    add_force_sub_channel(name, chat_id, link)
    await render_admin_fsub_menu(msg, edit=False)


@dp.message(Command("removechannel"))
async def cmd_removechannel(msg: Message):
    if not is_admin(msg.from_user.id):
        return await msg.answer("⛔ Access denied.")
    parts = (msg.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        return await msg.answer(
            "<h2>📢 Remove Force-Join Channel</h2>"
            "<blockquote><b>Usage:</b> <code>/removechannel &lt;number&gt;</code> (1-based index from the Force-Join list)</blockquote>"
        )
    idx = int(parts[1]) - 1
    ok, removed = remove_force_sub_channel(idx)
    if not ok:
        return await msg.answer("<h2>❌ Invalid Channel Number</h2>")
    await render_admin_fsub_menu(msg, edit=False)


@dp.message(StateFilter(AdminInteractive.waiting_gban))
async def handle_interactive_gban(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    raw = (msg.text or "").strip()
    target_id = int(raw) if raw.isdigit() else (users_col.find_one({"username": raw.lstrip("@")}) or {}).get("_id")
    await state.clear()
    back_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Back to Users", callback_data="admin_open:users")]
    ])
    if not target_id:
        return await msg.answer("<h2>❌ Invalid Target</h2><blockquote>Could not resolve that User ID or @username.</blockquote>", reply_markup=back_kb)
    users_col.update_one({"_id": target_id}, {"$set": {"banned": True}}, upsert=True)
    try:
        await bot.send_message(target_id, "<h2>🚫 Account Restricted</h2><blockquote>You have been banned from using this bot by the administrator.</blockquote>")
    except Exception:
        pass
    await msg.answer(
        "<h2>⛔ User Banned</h2>"
        f"<blockquote>User <code>{target_id}</code> has been banned from the bot.</blockquote>",
        reply_markup=back_kb,
    )


@dp.message(StateFilter(AdminInteractive.waiting_ungban))
async def handle_interactive_ungban(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    raw = (msg.text or "").strip()
    target_id = int(raw) if raw.isdigit() else (users_col.find_one({"username": raw.lstrip("@")}) or {}).get("_id")
    await state.clear()
    back_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Back to Users", callback_data="admin_open:users")]
    ])
    if not target_id:
        return await msg.answer("<h2>❌ Invalid Target</h2><blockquote>Could not resolve that User ID or @username.</blockquote>", reply_markup=back_kb)
    users_col.update_one({"_id": target_id}, {"$set": {"banned": False}})
    await msg.answer(
        "<h2>✅ User Unbanned</h2>"
        f"<blockquote>User <code>{target_id}</code> has been unbanned.</blockquote>",
        reply_markup=back_kb,
    )


# ================= Interactive Admin: Stock & Countries Callbacks =================
@dp.callback_query(F.data.startswith("adm_stk:"))
async def admin_stock_action(cq: CallbackQuery, state: FSMContext):
    if not is_admin(cq.from_user.id):
        return await cq.answer("Access denied", show_alert=True)
    action = cq.data.split(":", 1)[1]
    back_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Back to Stock & Countries", callback_data="admin_open:stock")]
    ])

    if action == "add":
        countries = list(countries_col.find({}))
        if not countries:
            return await cq.message.edit_text("<h2>❌ No Countries</h2><blockquote>Add a country first before adding numbers.</blockquote>", reply_markup=back_kb)
        kb = InlineKeyboardBuilder()
        for c in countries:
            kb.button(text=plain_country_label(c["name"]), callback_data=f"add_country:{c['name']}")
        kb.adjust(2)
        kb.row(InlineKeyboardButton(text="⬅️ Back to Stock", callback_data="admin_open:stock"))
        await cq.message.edit_text(
            "<h2>➕ Add Telegram Session</h2>"
            "<blockquote>🌍 Select the country you want to add a number for:</blockquote>",
            reply_markup=kb.as_markup(),
        )
        await state.set_state(AddSession.waiting_country)

    elif action == "remove":
        countries = list(countries_col.find({}))
        if not countries:
            return await cq.message.edit_text("<h2>❌ No Countries</h2><blockquote>No countries found in database.</blockquote>", reply_markup=back_kb)
        kb = InlineKeyboardBuilder()
        for c in countries:
            kb.button(text=plain_country_label(c["name"]), callback_data=f"remove_country:{c['name']}")
        kb.adjust(2)
        kb.row(InlineKeyboardButton(text="⬅️ Back to Stock", callback_data="admin_open:stock"))
        await cq.message.edit_text(
            "<h2>🗑️ Remove Number From Stock</h2>"
            "<blockquote>🌍 Select the country to remove a number from:</blockquote>",
            reply_markup=kb.as_markup(),
        )
        await state.set_state(RemoveSession.waiting_country)

    elif action == "db":
        countries = list(countries_col.find({}))
        if not countries:
            return await cq.message.edit_text("<h2>📭 Database Empty</h2><blockquote>No countries found in DB.</blockquote>", reply_markup=back_kb)
        blocks = []
        for c in countries:
            c_name = c["name"]
            nums = list(numbers_col.find({"country": c_name}))
            avail = sum(1 for n in nums if not n.get("used"))
            blocks.append(f"🌍 <b>{escape(c_name)}</b> — Price: <code>₹{c.get('price', 0)}</code> · Available: <code>{avail}/{len(nums)}</code>")
        await cq.message.edit_text(
            "<h2>📚 Server 1 Database Inventory</h2>"
            f"<blockquote>{'<br>'.join(blocks[:40])}</blockquote>",
            reply_markup=back_kb,
        )

    elif action == "addcountry":
        await state.set_state("adding_country")
        await cq.message.edit_text(
            "<h2>🌍 Add / Update Country</h2>"
            "<blockquote>Send the country name and price separated by a comma:<br>"
            "Example: <code>India,50</code></blockquote>",
            reply_markup=back_kb,
        )

    elif action == "editcountry":
        countries = list(countries_col.find({}))
        if not countries:
            return await cq.message.edit_text("<h2>📭 No Countries</h2><blockquote>No countries available to edit.</blockquote>", reply_markup=back_kb)
        kb = InlineKeyboardBuilder()
        for c in countries:
            kb.button(text=plain_country_label(c["name"]), callback_data=f"editcountry:{c['name']}")
        kb.adjust(2)
        kb.row(InlineKeyboardButton(text="⬅️ Back to Stock", callback_data="admin_open:stock"))
        await cq.message.edit_text(
            "<h2>✏️ Edit Country</h2>"
            "<blockquote>🌍 Select a country below to rename or update its price:</blockquote>",
            reply_markup=kb.as_markup(),
        )

    elif action == "removecountry":
        countries = list(countries_col.find({}))
        if not countries:
            return await cq.message.edit_text("<h2>📭 No Countries</h2><blockquote>No countries available to remove.</blockquote>", reply_markup=back_kb)
        kb = InlineKeyboardBuilder()
        for c in countries:
            kb.button(text=plain_country_label(c["name"]), callback_data=f"removecountry:{c['name']}")
        kb.adjust(2)
        kb.row(InlineKeyboardButton(text="⬅️ Back to Stock", callback_data="admin_open:stock"))
        await cq.message.edit_text(
            "<h2>🗑️ Remove Country</h2>"
            "<blockquote>🌍 Select a country below to delete it from Server 1:</blockquote>",
            reply_markup=kb.as_markup(),
        )

    elif action == "editsell":
        await state.set_state(EditSellRates.waiting_rates)
        await cq.message.edit_text(
            "<h2>💵 Edit Sell Account Rates</h2>"
            "<blockquote>Send the country sell rates list (one per line):<br>"
            "<code>USA ₹50</code><br><code>India ₹10</code><br><code>UK ₹20</code></blockquote>",
            reply_markup=back_kb,
        )

    elif action == "sellcountry":
        await state.set_state(AdminInteractive.waiting_sellcountry)
        await cq.message.edit_text(
            "<h2>📋 Bulk Update Sell Countries</h2>"
            "<blockquote>Send country prefixes and prices (one per line):<br>"
            "<code>+91 India ₹30</code><br><code>+1 USA ₹32</code><br><code>+62 Indonesia ₹28</code></blockquote>",
            reply_markup=back_kb,
        )
    await cq.answer()


@dp.message(StateFilter(AdminInteractive.waiting_sellcountry))
async def handle_interactive_sellcountry(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    await state.clear()
    updated, errors = [], []
    for line in (msg.text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("/sellcountry"):
            continue
        try:
            parts = line.split(" ")
            prefix = parts[0]
            if not prefix.startswith("+"):
                raise ValueError("Missing +country code")
            match_price = [p for p in parts if "₹" in p]
            if not match_price:
                raise ValueError("Missing price (₹)")
            price = match_price[-1]
            country = " ".join(parts[1:parts.index(price)]).strip()
            db["sell_countries"].update_one(
                {"prefix": prefix},
                {"$set": {"country": country, "price": price}},
                upsert=True,
            )
            updated.append(f"{prefix} {country} → {price}")
        except Exception as e:
            errors.append(f"❌ {line} ({e})")
    back_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Back to Stock", callback_data="admin_open:stock")]
    ])
    body = ""
    if updated:
        body += f"<blockquote>✅ <b>Updated ({len(updated)}):</b><br>{'<br>'.join(updated)}</blockquote>"
    if errors:
        body += f"<blockquote>⚠️ <b>Errors:</b><br>{'<br>'.join(errors)}</blockquote>"
    await msg.answer(f"<h2>📋 Sell Countries Updated</h2>{body or '<p>Nothing processed.</p>'}", reply_markup=back_kb)


# ================= Interactive Admin: Redeem Codes Callbacks =================
@dp.callback_query(F.data.startswith("adm_red:"))
async def admin_redeem_action(cq: CallbackQuery, state: FSMContext):
    if not is_admin(cq.from_user.id):
        return await cq.answer("Access denied", show_alert=True)
    parts = cq.data.split(":")
    action = parts[1]
    back_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Back to Redeem Manager", callback_data="admin_open:redeem")]
    ])

    if action == "create":
        await state.set_state(RedeemState.waiting_code)
        await cq.message.edit_text(
            "<h2>➕ Create Custom Redeem Code</h2>"
            "<blockquote>🎟️ Enter the custom promo code name (e.g. <code>DIWALI100</code>):</blockquote>",
            reply_markup=back_kb,
        )
    elif action == "random":
        code = generate_code(8)
        await state.update_data(custom_code=code)
        await state.set_state(RedeemState.waiting_amount_custom)
        await cq.message.edit_text(
            "<h2>🎟️ Random Redeem Code Generated</h2>"
            f"<blockquote>🎟️ <b>Generated Code:</b> <code>{code}</code><br>"
            "💰 Now send the <b>INR amount (₹)</b> per claim for this code:</blockquote>",
            reply_markup=back_kb,
        )
    elif action == "del" and len(parts) > 2:
        code_to_del = parts[2]
        redeem_col.delete_one({"code": code_to_del})
        await cq.answer(f"Deleted code {code_to_del}", show_alert=True)
        action = "list"

    if action == "list":
        redeems = list(redeem_col.find())
        if not redeems:
            return await cq.message.edit_text(
                "<h2>📭 No Active Redeem Codes</h2><blockquote>No promo codes are currently active.</blockquote>",
                reply_markup=back_kb,
            )
        lines = []
        kb = InlineKeyboardBuilder()
        for r in redeems[:20]:
            c_str = r["code"]
            lines.append(
                f"🎟️ <code>{c_str}</code> — <code>₹{r['amount']}</code> "
                f"(👥 <code>{r['claimed_count']}/{r['max_claims']}</code>)"
            )
            kb.button(text=f"🗑️ {c_str}", callback_data=f"adm_red:del:{c_str}")
        kb.adjust(2)
        kb.row(InlineKeyboardButton(text="➕ Create Code", callback_data="adm_red:create"))
        kb.row(InlineKeyboardButton(text="⬅️ Back to Redeem Manager", callback_data="admin_open:redeem"))
        await cq.message.edit_text(
            f"<h2>🎟️ Active Redeem Codes ({len(redeems)})</h2>"
            f"<blockquote>{'<br>'.join(lines)}</blockquote>"
            "<p>🗑️ <i>Tap any code button below to delete it:</i></p>",
            reply_markup=kb.as_markup(),
        )
    await cq.answer()


# ================= Interactive Admin: Reports Callbacks =================
@dp.callback_query(F.data.startswith("adm_rep:"))
async def admin_reports_action(cq: CallbackQuery):
    if not is_admin(cq.from_user.id):
        return await cq.answer("Access denied", show_alert=True)
    parts = cq.data.split(":")
    action = parts[1]

    if action == "sales":
        await cq.answer("Generating live analytics...")
        try:
            report = await build_admin_analytics_report()
        except Exception as exc:
            logger.exception("Interactive admin analytics failed")
            report = (
                "<h2>⚠️ Analytics Temporarily Unavailable</h2>"
                f"<blockquote><code>{escape(str(exc)[:500])}</code></blockquote>"
                "<p><i>Try again in a moment, or open Bot Health to check the runtime.</i></p>"
            )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Refresh Analytics", callback_data="adm_rep:sales")],
            [InlineKeyboardButton(text="🤖 Bot Health", callback_data="admin_open:health")],
            [InlineKeyboardButton(text="⬅️ Back to Reports", callback_data="admin_open:reports")],
        ])
        await cq.message.edit_text(report, parse_mode="HTML", reply_markup=kb)
        return

    if action == "topusers":
        page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 1
        limit = 10
        skip = (page - 1) * limit
        pipeline = [
            {"$group": {"_id": "$user_id", "total_spend": {"$sum": "$price"}}},
            {"$sort": {"total_spend": -1}},
            {"$skip": skip},
            {"$limit": limit},
            {
                "$lookup": {
                    "from": "users",
                    "localField": "_id",
                    "foreignField": "_id",
                    "as": "user_info",
                }
            },
        ]
        top_spenders = list(orders_col.aggregate(pipeline))
        lines = []
        for idx, entry in enumerate(top_spenders, start=skip + 1):
            uid = entry["_id"]
            spend = float(entry.get("total_spend", 0))
            u_data = entry["user_info"][0] if entry.get("user_info") else {}
            uname = u_data.get("username")
            label = f"@{uname}" if uname else f"<code>{uid}</code>"
            lines.append(f"<b>#{idx}</b> 👤 {label} — <code>₹{spend:,.2f}</code>")
        body = "<br>".join(lines) if lines else "No spending records on this page."
        nav = []
        if page > 1:
            nav.append(InlineKeyboardButton(text="⬅️ Prev", callback_data=f"adm_rep:topusers:{page - 1}"))
        if len(top_spenders) == limit:
            nav.append(InlineKeyboardButton(text="Next ➡️", callback_data=f"adm_rep:topusers:{page + 1}"))
        rows = []
        if nav:
            rows.append(nav)
        rows.append([InlineKeyboardButton(text="⬅️ Back to Reports", callback_data="admin_open:reports")])
        await cq.message.edit_text(
            f"<h2>🏆 Top Spending Users — Page {page}</h2>"
            f"<blockquote>{body}</blockquote>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )
        await cq.answer()


# ================= Interactive Admin: Broadcast Handler =================
@dp.message(StateFilter(AdminInteractive.waiting_broadcast))
async def handle_interactive_broadcast(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    await state.clear()
    users = list(users_col.find({}))
    if not users:
        return await msg.answer("⚠️ No users found to broadcast.")

    status_msg = await msg.answer(
        "<h2>📣 Broadcasting Message...</h2>"
        f"<blockquote>⏳ Forwarding to <code>{len(users):,}</code> users...</blockquote>"
    )
    sent_count = 0
    failed_count = 0
    for user in users:
        try:
            await bot.forward_message(
                chat_id=user["_id"],
                from_chat_id=msg.chat.id,
                message_id=msg.message_id,
            )
            sent_count += 1
        except Exception:
            failed_count += 1

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Admin Panel", callback_data="admin_refresh")]
    ])
    await status_msg.edit_text(
        "<h2>✅ Broadcast Completed</h2>"
        f"<blockquote>📨 <b>Delivered:</b> <code>{sent_count:,}</code><br>"
        f"❌ <b>Failed / Blocked:</b> <code>{failed_count:,}</code></blockquote>",
        reply_markup=kb,
    )


# ================= Admin Broadcast (Forward Version - Slash Command) =================
@dp.message(Command("broadcast"))
async def cmd_broadcast(msg: Message):
    if not is_admin(msg.from_user.id):
        return await msg.answer("❌ Not authorized.")

    if not msg.reply_to_message:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📣 Open Interactive Broadcast", callback_data="admin_open:broadcast")]
        ])
        return await msg.answer(
            "<h2>📣 Broadcast Message</h2>"
            "<blockquote>Reply to any message with <code>/broadcast</code>, or tap the button below to broadcast interactively.</blockquote>",
            reply_markup=kb,
        )

    broadcast_msg = msg.reply_to_message
    users = list(users_col.find({}))

    if not users:
        return await msg.answer("⚠️ No users found to broadcast.")

    sent_count = 0
    failed_count = 0

    for user in users:
        user_id = user["_id"]
        try:
            await bot.forward_message(
                chat_id=user_id,
                from_chat_id=broadcast_msg.chat.id,
                message_id=broadcast_msg.message_id
            )
            sent_count += 1
        except Exception as e:
            failed_count += 1
            print(f"Failed to send to {user_id}: {e}")

    await msg.answer(
        "<h2>✅ Broadcast Completed</h2>"
        f"<blockquote>📨 <b>Sent:</b> <code>{sent_count:,}</code><br>"
        f"❌ <b>Failed:</b> <code>{failed_count:,}</code></blockquote>"
    )
    

# ===== Register External Handlers =====
start_auto_sweeper = register_recharge_handlers(
    dp=dp, bot=bot, users_col=users_col, txns_col=db["transactions"],
    crypto_col=crypto_col, settings_col=settings_col, ADMIN_IDS=ADMIN_IDS
)
SMM_PUBLIC_LOG = "-1002254383562"  # Apna public log channel ki actual ID daalo

smm_background_task, smm_send_menu = register_smm_handlers(
    dp=dp, bot=bot, db=db, users_col=users_col,
    admin_ids=ADMIN_IDS, admin_log_id=ADMINLOG, public_log_id=SMM_PUBLIC_LOG
)
s3_send_countries_message = register_server3_handlers(
    dp=dp, bot=bot, db=db, users_col=users_col, orders_col=orders_col,
    settings_col=settings_col, admin_ids=ADMIN_IDS, ADMINLOG=ADMINLOG,
    SALES=SALES, TEMPORASMS_API_KEY=TEMPORASMS_API_KEY,
    BOTUSER=BOTUSER, CHANNEL=CHANNEL
)
# --- All Apps OTP (new allapps.py replaces old otp.py) ---
def _allapps_is_admin(uid: int) -> bool:
    return is_admin(uid)
def _allapps_fmt_curr(v) -> str:
    try:
        return f"₹{float(v):.2f}"
    except Exception:
        return str(v)
allapps_root = register_allapps_handlers(
    dp=dp, bot=bot, db=db, users_col=users_col, orders_col=orders_col,
    is_admin_func=_allapps_is_admin, fmt_curr_func=_allapps_fmt_curr
)
# --- Server 4 — New Numbers (Telegram-only via TemporaSMS) ---
def _s4_is_admin(uid: int) -> bool:
    return is_admin(uid)
def _s4_fmt_curr(v) -> str:
    try:
        return f"₹{float(v):.2f}"
    except Exception:
        return str(v)
server4_root = register_server4_handlers(
    dp=dp, bot=bot, db=db, users_col=users_col, orders_col=orders_col,
    is_admin_func=_s4_is_admin, fmt_curr_func=_s4_fmt_curr
)
# --- OTP Admin Panel + Legacy OTP Order Reconciliation ---
otp_ctx = {}
otp_background_task = register_otp_handlers(
    dp=dp, bot=bot, db=db, users_col=users_col,
    api_key=TEMPORASMS_API_KEY, rate=USD_TO_INR, markup=OTP_MARKUP_PERCENT,
    settings_col=settings_col, registry=otp_ctx,
)
_orig_otp_clear_cache = otp_ctx["api"].clear_cache


def _synced_otp_clear_cache():
    _orig_otp_clear_cache()
    _allapps_client.clear_cache()
    _clear_allapps_rate_cache()
    _server4_client.clear_cache()
    _clear_server4_rate_cache()


otp_ctx["api"].clear_cache = _synced_otp_clear_cache

register_otp_admin_handlers(
    dp=dp, bot=bot, db=db, users_col=users_col, settings_col=settings_col,
    admin_ids=ADMIN_IDS, env_api_key=TEMPORASMS_API_KEY, env_markup=OTP_MARKUP_PERCENT,
    rate=USD_TO_INR, otp_ctx=otp_ctx, admin_log_id=ADMINLOG,
)


async def _start_health_server():
    """Bind 0.0.0.0:$PORT when running as a Render Web Service so port checks succeed."""
    port_raw = (os.getenv("PORT") or "").strip()
    if not port_raw.isdigit():
        return None
    from aiohttp import web

    async def _health(_request):
        return web.json_response({"ok": True, "service": "tga-store-bot"})

    app = web.Application()
    app.router.add_get("/", _health)
    app.router.add_get("/health", _health)
    app.router.add_get("/healthz", _health)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(port_raw))
    await site.start()
    print(f"Health server listening on 0.0.0.0:{port_raw}", flush=True)
    return runner


# ===== Bot Runner =====
async def main():
    global BOT_STARTED_AT, SMM_BACKGROUND_TASK
    from aiogram.types import BotCommand, BotCommandScopeChat

    BOT_STARTED_AT = datetime.now(timezone.utc)
    health_runner = await _start_health_server()
    print("TGA Store | API started.")
    await bot.set_my_commands([
        BotCommand(command="start", description="Open TGA Store | API"),
        BotCommand(command="balance", description="View wallet balance"),
    ])
    for admin_id in ADMIN_IDS:
        try:
            await bot.set_my_commands([
                BotCommand(command="admin", description="Open admin control center"),
                BotCommand(command="start", description="Open user menu"),
                BotCommand(command="balance", description="View wallet balance"),
                BotCommand(command="manageserver", description="Manage API servers"),
                BotCommand(command="server3", description="Manage All-Apps OTP panel"),
                BotCommand(command="server4", description="Manage Server 4 (New Numbers)"),
                BotCommand(command="paymentsettings", description="Manage payments"),
                BotCommand(command="otpadmin", description="Manage OTP services (TemporaSMS)"),
                BotCommand(command="smmadmin", description="Manage SMM services"),
                BotCommand(command="managestore", description="Manage store"),
                BotCommand(command="broadcast", description="Broadcast replied message"),
            ], scope=BotCommandScopeChat(chat_id=admin_id))
        except Exception as exc:
            print(f"Could not set admin commands for {admin_id}: {exc}")
    start_auto_sweeper()  # launches the UPI/FamPay background auto-verify loop
    SMM_BACKGROUND_TASK = asyncio.create_task(smm_background_task(), name="smm-order-status-checker")

    otp_task = None
    if otp_background_task:
        otp_task = asyncio.create_task(otp_background_task(), name="otp-status-checker")
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        if otp_task:
            otp_task.cancel()
            await asyncio.gather(otp_task, return_exceptions=True)
        if health_runner:
            await health_runner.cleanup()

if __name__ == "__main__":
    import traceback
    try:
        asyncio.run(main())
    except Exception:
        print("❌ BOT CRASHED WITH ERROR:", flush=True)
        traceback.print_exc()
        raise
