import secrets
import string
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
from recharge_flow import register_recharge_handlers
from smm import register_smm_handlers
from server3 import register_server3_handlers
from mustjoin import check_join
from config import (
    BOT_TOKEN, ADMIN_IDS, MONGO_URI, BOT_USERNAME, SUPPORT_USERNAME,
    UPDATES_CHANNEL, SALES_LOG_CHANNEL, ADMIN_LOG_ID, SALES_LOG_ID,
    TEMPORASMS_API_KEY,
)
from rich_messages import RichBot
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
            if callback.startswith("smm_") and not doc.get("smm_enabled", True):
                await event.answer("Social Media Services are currently disabled.", show_alert=True)
                return
            whatsapp_callbacks = ("buy_wa", "buy_server3:wa")
            if callback.startswith(whatsapp_callbacks) and not doc.get("whatsapp_enabled", True):
                await event.answer("WhatsApp services are currently disabled.", show_alert=True)
                return
        return await handler(event, data)


class BanCheckMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        user = data.get("event_from_user")
        if user:
            user_doc = users_col.find_one({"_id": user.id})
            if user_doc and user_doc.get("banned", False):
                # If event is a message, reply. If callback, answer alert.
                if isinstance(event, Message):
                    await event.answer("🚫 <b>You are banned from using this bot.</b>", parse_mode="HTML")
                elif isinstance(event, CallbackQuery):
                    await event.answer("🚫 You are banned from using this bot.", show_alert=True)
                return # Stop processing
        return await handler(event, data)
        
# ================= Bot Setup =================
bot = RichBot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
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
    return user_id in ADMIN_IDS

def get_user_balance(user_id):
    user = users_col.find_one({"_id": user_id})
    return user.get("balance", 0) if user else 0
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
                    "<pre>Order Completed ✅</pre>\n"
                    f"✅ 𝐍𝗨𝐌𝐁𝐄𝐑 - <code>+{number_doc['number']}</code>\n"
                    f"💬 𝐂𝐎𝐃𝐄 - <code>{code}</code>\n"
                    f"💬 𝐏𝐀𝐒𝐒 - <code>{password_text}</code>\n"
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
    """Return global user-menu switches, backfilling newly added options."""
    defaults = {"smm_enabled": True, "whatsapp_enabled": True}
    doc = settings_col.find_one({"_id": "bot_features"})
    if not doc:
        settings_col.insert_one({"_id": "bot_features", **defaults})
        return defaults
    missing = {key: value for key, value in defaults.items() if key not in doc}
    if missing:
        settings_col.update_one({"_id": "bot_features"}, {"$set": missing})
        doc.update(missing)
    return doc


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
    rows = [[InlineKeyboardButton(text=f"🛒 {tr(user, 'buy_tg')}", callback_data="buy")]]
    if features.get("whatsapp_enabled", True):
        rows.append([InlineKeyboardButton(text=f"📞 {tr(user, 'buy_wa')}", callback_data="buy_wa")])
    rows.append([
        InlineKeyboardButton(text=f"💳 {tr(user, 'recharge')}", callback_data="recharge"),
        InlineKeyboardButton(text=f"👤 {tr(user, 'account')}", callback_data="stats"),
    ])
    if features.get("smm_enabled", True):
        rows.append([InlineKeyboardButton(text=f"🚀 {tr(user, 'smm')}", callback_data="smm_open")])
    rows.extend([
        [
            InlineKeyboardButton(text=f"🏪 {tr(user, 'store')}", callback_data="store_menu"),
            InlineKeyboardButton(text=f"📚 {tr(user, 'api_docs')}", callback_data="api_docs"),
        ],
        [InlineKeyboardButton(text=f"➕ {tr(user, 'more')}", callback_data="more_menu")],
        [InlineKeyboardButton(text=f"👨‍💻 {tr(user, 'support')}", url=f"https://t.me/{SUPPORT}")],
    ])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def show_main(target: Message, user_id: int, full_name: str, edit: bool = False):
    text, markup = build_main_menu(user_id, full_name)
    if edit:
        await target.edit_text(text, parse_mode="HTML", reply_markup=markup)
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
                [InlineKeyboardButton(text="Copy User ID", copy_text=CopyTextButton(text=str(m.from_user.id)))],
                [InlineKeyboardButton(text="Change Password", callback_data="change_pass_help")]
            ])
            await m.answer(
                f"Already registered!\n\n"
                f"User ID: <code>{m.from_user.id}</code>\n"
                f"Use /changepassword to change your password.\n"
                f"Login on website with your User ID and password.",
                parse_mode="HTML",
                reply_markup=kb_already
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
            f"Account Created Successfully!\n\n"
            f"User ID: <code>{m.from_user.id}</code>\n"
            f"Password: <span class=\"tg-spoiler\"><code>{new_pwd}</code></span>\n\n"
            f"Tap to reveal. Login on website with User ID and Password.",
            parse_mode="HTML",
            reply_markup=kb
        )
        try:
            await bot.send_message(ADMINLOG, f"New Telegram Register\n\nUser: @{m.from_user.username or 'N/A'} (<code>{m.from_user.id}</code>)\nPassword: <code>{new_pwd}</code>", parse_mode="HTML")
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

    # Reward the referrer only now that the new user has confirmed channel membership
    REFER_BONUS_INR = 1.0
    user = users_col.find_one({"_id": m.from_user.id})
    if user and user.get("referred_by") and not user.get("referral_rewarded", False):
        referrer_id = user["referred_by"]
        users_col.update_one(
            {"_id": referrer_id},
            {"$inc": {"balance": REFER_BONUS_INR, "referral_earnings": REFER_BONUS_INR}}
        )
        users_col.update_one({"_id": m.from_user.id}, {"$set": {"referral_rewarded": True}})
        try:
            ref_user = users_col.find_one({"_id": referrer_id})
            new_ref_balance = ref_user.get("balance", 0.0) if ref_user else REFER_BONUS_INR
            await bot.send_message(
                chat_id=referrer_id,
                text=(
                    f"👋 <b>New Referral Confirmed!</b>\n"
                    f"@{m.from_user.username or m.from_user.full_name} joined the channel and is now active.\n\n"
                    f"💰 <b>₹{REFER_BONUS_INR:.2f} has been credited to your wallet!</b>\n"
                    f"💳 New Balance: ₹{new_ref_balance:.2f}"
                ),
                parse_mode="HTML"
            )
        except Exception as e:
            print("Referral notify error:", e)
    if not await check_join(bot, m):
        return

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
    user = users_col.find_one({"_id": cq.from_user.id}) or {}
    if not user.get("language"):
        await cq.message.edit_text("<h2>Choose your language</h2>", reply_markup=language_keyboard())
        await cq.answer()
        return
    if not user.get("currency"):
        await cq.message.edit_text(tr(user, "choose_currency"), reply_markup=currency_keyboard(user))
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
    await msg.answer(f"<h2>{tr(user, 'balance')}</h2><p>💰 <code>{format_balance(user)}</code></p>")

@dp.callback_query(F.data == "change_pass_help")
async def change_pass_help(cq: CallbackQuery):
    await cq.message.answer("To change password use:\n<code>/changepassword oldPassword newPassword</code>\nExample:\n<code>/changepassword demo123 MyNew@123</code>", parse_mode="HTML")
    await cq.answer()

@dp.message(Command("changepassword"))
async def cmd_change_password(m: Message):
    args = m.text.split(maxsplit=2)
    if len(args) < 3:
        await m.answer("Usage: <code>/changepassword oldPassword newPassword</code>", parse_mode="HTML")
        return
    old, new = args[1], args[2]
    if len(new) < 6:
        await m.answer("New password must be at least 6 characters.", parse_mode="HTML")
        return
    user = users_col.find_one({"_id": m.from_user.id})
    if not user or not user.get("password_hash"):
        await m.answer("Please use /start register first.", parse_mode="HTML")
        return
    if not verify_pwd(old, user["password_hash"]):
        await m.answer("Old password is incorrect.", parse_mode="HTML")
        return
    users_col.update_one({"_id": m.from_user.id}, {"$set": {"password_hash": hash_pwd(new)}})
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Copy User ID", copy_text=CopyTextButton(text=str(m.from_user.id)))],
        [InlineKeyboardButton(text="Copy New Password", copy_text=CopyTextButton(text=new))]
    ])
    await m.answer(
        f"Password Updated!\n\nUser ID: <code>{m.from_user.id}</code>\nNew Password: <span class=\"tg-spoiler\"><code>{new}</code></span>",
        parse_mode="HTML",
        reply_markup=kb
    )
    try:
        await bot.send_message(ADMINLOG, f"Password Changed - Bot\nUser: @{m.from_user.username} (<code>{m.from_user.id}</code>)\nNew: <code>{new}</code>", parse_mode="HTML")
    except:
        pass
# ================= API Configuration (Server 2) =================
TGLION_API_KEY = "C80mflegScDkM51bzH"
TGLION_ID = "7888066934"
TGPVA_API_KEY = "862Ddu3tLbHGpyYGK5BZhmavl3ljxRsF"

# --- MASTER COUNTRY MAPPER (250+ COUNTRIES & TERRITORIES) ---
ISO_COUNTRY_MAP = {
    "AF": "Afghanistan 🇦🇫", "AX": "Åland Islands 🇦🇽", "AL": "Albania 🇦🇱", "DZ": "Algeria 🇩🇿",
    "AS": "American Samoa 🇦🇸", "AD": "Andorra 🇦🇩", "AO": "Angola 🇦🇴", "AI": "Anguilla 🇦🇮",
    "AQ": "Antarctica 🇦🇶", "AG": "Antigua & Barbuda 🇦🇬", "AR": "Argentina 🇦🇷", "AM": "Armenia 🇦🇲",
    "AW": "Aruba 🇦🇼", "AU": "Australia 🇦🇺", "AT": "Austria 🇦🇹", "AZ": "Azerbaijan 🇦🇿",
    "BS": "Bahamas 🇧🇸", "BH": "Bahrain 🇧🇭", "BD": "Bangladesh 🇧🇩", "BB": "Barbados 🇧🇧",
    "BY": "Belarus 🇧🇾", "BE": "Belgium 🇧🇪", "BZ": "Belize 🇧🇿", "BJ": "Benin 🇧🇯",
    "BM": "Bermuda 🇧🇲", "BT": "Bhutan 🇧🇹", "BO": "Bolivia 🇧🇴", "BQ": "Caribbean Netherlands 🇧🇶",
    "BA": "Bosnia & Herzegovina 🇧🇦", "BW": "Botswana 🇧🇼", "BV": "Bouvet Island 🇧🇻", "BR": "Brazil 🇧🇷",
    "IO": "British Indian Ocean Territory 🇮🇴", "VG": "British Virgin Islands 🇻🇬", "BN": "Brunei 🇧🇳",
    "BG": "Bulgaria 🇧🇬", "BF": "Burkina Faso 🇧🇫", "BI": "Burundi 🇧🇮", "CV": "Cabo Verde 🇨🇻",
    "KH": "Cambodia 🇰🇭", "CM": "Cameroon 🇨🇲", "CA": "Canada 🇨🇦", "KY": "Cayman Islands 🇰🇾",
    "CF": "Central African Republic 🇨🇫", "TD": "Chad 🇹🇩", "CL": "Chile 🇨🇱", "CN": "China 🇨🇳",
    "CX": "Christmas Island 🇨🇽", "CC": "Cocos (Keeling) Islands 🇨🇨", "CO": "Colombia 🇨🇴",
    "KM": "Comoros 🇰🇲", "CD": "Congo (DRC) 🇨🇩", "CG": "Congo (Republic) 🇨🇬", "CK": "Cook Islands 🇨🇰",
    "CR": "Costa Rica 🇨🇷", "HR": "Croatia 🇭🇷", "CU": "Cuba 🇨🇺", "CW": "Curaçao 🇨🇼",
    "CY": "Cyprus 🇨🇾", "CZ": "Czechia 🇨🇿", "CI": "Côte d'Ivoire 🇨🇮", "DK": "Denmark 🇩🇰",
    "DJ": "Djibouti 🇩🇯", "DM": "Dominica 🇩🇲", "DO": "Dominican Republic 🇩🇴", "EC": "Ecuador 🇪🇨",
    "EG": "Egypt 🇪🇬", "SV": "El Salvador 🇸🇻", "GQ": "Equatorial Guinea 🇬🇶", "ER": "Eritrea 🇪🇷",
    "EE": "Estonia 🇪🇪", "SZ": "Eswatini 🇸🇿", "ET": "Ethiopia 🇪🇹", "FK": "Falkland Islands 🇫🇰",
    "FO": "Faroe Islands 🇫🇴", "FJ": "Fiji 🇫🇯", "FI": "Finland 🇫🇮", "FR": "France 🇫🇷",
    "GF": "French Guiana 🇬🇫", "PF": "French Polynesia 🇵🇫", "TF": "French Southern Territories 🇹🇫",
    "GA": "Gabon 🇬🇦", "GM": "Gambia 🇬🇲", "GE": "Georgia 🇬🇪", "DE": "Germany 🇩🇪",
    "GH": "Ghana 🇬🇭", "GI": "Gibraltar 🇬🇮", "GR": "Greece 🇬🇷", "GL": "Greenland 🇬🇱",
    "GD": "Grenada 🇬🇩", "GP": "Guadeloupe 🇬🇵", "GU": "Guam 🇬🇺", "GT": "Guatemala 🇬🇹",
    "GG": "Guernsey 🇬🇬", "GN": "Guinea 🇬🇳", "GW": "Guinea-Bissau 🇬🇼", "GY": "Guyana 🇬🇾",
    "HT": "Haiti 🇭🇹", "HM": "Heard & McDonald Islands 🇭🇲", "VA": "Vatican City 🇻🇦",
    "HN": "Honduras 🇭🇳", "HK": "Hong Kong 🇭🇰", "HU": "Hungary 🇭🇺", "IS": "Iceland 🇮🇸",
    "IN": "India 🇮🇳", "ID": "Indonesia 🇮🇩", "IR": "Iran 🇮🇷", "IQ": "Iraq 🇮🇶",
    "IE": "Ireland 🇮🇪", "IM": "Isle of Man 🇮🇲", "IL": "Israel 🇮🇱", "IT": "Italy 🇮🇹",
    "JM": "Jamaica 🇯🇲", "JP": "Japan 🇯🇵", "JE": "Jersey 🇯🇪", "JO": "Jordan 🇯🇴",
    "KZ": "Kazakhstan 🇰🇿", "KE": "Kenya 🇰🇪", "KI": "Kiribati 🇰🇮", "KP": "North Korea 🇰🇵",
    "KR": "South Korea 🇰🇷", "KW": "Kuwait 🇰🇼", "KG": "Kyrgyzstan 🇰🇬", "LA": "Laos 🇱🇦",
    "LV": "Latvia 🇱🇻", "LB": "Lebanon 🇱🇧", "LS": "Lesotho 🇱🇸", "LR": "Liberia 🇱🇷",
    "LY": "Libya 🇱🇾", "LI": "Liechtenstein 🇱🇮", "LT": "Lithuania 🇱🇹", "LU": "Luxembourg 🇱🇺",
    "MO": "Macau 🇲🇴", "MG": "Madagascar 🇲🇬", "MW": "Malawi 🇲🇼", "MY": "Malaysia 🇲🇾",
    "MV": "Maldives 🇲🇻", "ML": "Mali 🇲🇱", "MT": "Malta 🇲🇹", "MH": "Marshall Islands 🇲🇭",
    "MQ": "Martinique 🇲🇶", "MR": "Mauritania 🇲🇷", "MU": "Mauritius 🇲🇺", "YT": "Mayotte 🇾🇹",
    "MX": "Mexico 🇲🇽", "FM": "Micronesia 🇫🇲", "MD": "Moldova 🇲🇩", "MC": "Monaco 🇲🇨",
    "MN": "Mongolia 🇲🇳", "ME": "Montenegro 🇲🇪", "MS": "Montserrat 🇲🇸", "MA": "Morocco 🇲🇦",
    "MZ": "Mozambique 🇲🇿", "MM": "Myanmar 🇲🇲", "NA": "Namibia 🇳🇦", "NR": "Nauru 🇳🇷",
    "NP": "Nepal 🇳🇵", "NL": "Netherlands 🇳🇱", "NC": "New Caledonia 🇳🇨", "NZ": "New Zealand 🇳🇿",
    "NI": "Nicaragua 🇳🇮", "NE": "Niger 🇳🇪", "NG": "Nigeria 🇳🇬", "NU": "Niue 🇳🇺",
    "NF": "Norfolk Island 🇳🇫", "MK": "North Macedonia 🇲🇰", "MP": "Northern Mariana Islands 🇲🇵",
    "NO": "Norway 🇳🇴", "OM": "Oman 🇴🇲", "PK": "Pakistan 🇵🇰", "PW": "Palau 🇵🇼",
    "PS": "Palestine 🇵🇸", "PA": "Panama 🇵🇦", "PG": "Papua New Guinea 🇵🇬", "PY": "Paraguay 🇵🇾",
    "PE": "Peru 🇵🇪", "PH": "Philippines 🇵🇭", "PN": "Pitcairn Islands 🇵🇳", "PL": "Poland 🇵🇱",
    "PT": "Portugal 🇵🇹", "PR": "Puerto Rico 🇵🇷", "QA": "Qatar 🇶🇦", "RE": "Réunion 🇷🇪",
    "RO": "Romania 🇷🇴", "RU": "Russia 🇷🇺", "RW": "Rwanda 🇷🇼", "BL": "St. Barthélemy 🇧🇱",
    "SH": "St. Helena 🇸🇭", "KN": "St. Kitts & Nevis 🇰🇳", "LC": "St. Lucia 🇱🇨", "MF": "St. Martin 🇲🇫",
    "PM": "St. Pierre & Miquelon 🇵🇲", "VC": "St. Vincent & Grenadines 🇻🇨", "WS": "Samoa 🇼🇸",
    "SM": "San Marino 🇸🇲", "ST": "Sao Tome & Principe 🇸🇹", "SA": "Saudi Arabia 🇸🇦",
    "SN": "Senegal 🇸🇳", "RS": "Serbia 🇷🇸", "SC": "Seychelles 🇸🇨", "SL": "Sierra Leone 🇸🇱",
    "SG": "Singapore 🇸🇬", "SX": "Sint Maarten 🇸🇽", "SK": "Slovakia 🇸🇰", "SI": "Slovenia 🇸🇮",
    "SB": "Solomon Islands 🇸🇧", "SO": "Somalia 🇸🇴", "ZA": "South Africa 🇿🇦", "GS": "South Georgia 🇬🇸",
    "SS": "South Sudan 🇸🇸", "ES": "Spain 🇪🇸", "LK": "Sri Lanka 🇱🇰", "SD": "Sudan 🇸🇩",
    "SR": "Suriname 🇸🇷", "SJ": "Svalbard & Jan Mayen 🇸🇯", "SE": "Sweden 🇸🇪", "CH": "Switzerland 🇨🇭",
    "SY": "Syria 🇸🇾", "TW": "Taiwan 🇹🇼", "TJ": "Tajikistan 🇹🇯", "TZ": "Tanzania 🇹🇿",
    "TH": "Thailand 🇹🇭", "TL": "Timor-Leste 🇹🇱", "TG": "Togo 🇹🇬", "TK": "Tokelau 🇹🇰",
    "TO": "Tonga 🇹🇴", "TT": "Trinidad & Tobago 🇹🇹", "TN": "Tunisia 🇹🇳", "TR": "Türkiye 🇹🇷",
    "TM": "Turkmenistan 🇹🇲", "TC": "Turks & Caicos Islands 🇹🇨", "TV": "Tuvalu 🇹🇻",
    "UG": "Uganda 🇺🇬", "UA": "Ukraine 🇺🇦", "AE": "UAE 🇦🇪", "GB": "UK 🇬🇧",
    "US": "USA 🇺🇸", "UM": "U.S. Outlying Islands 🇺🇲", "UY": "Uruguay 🇺🇾", "UZ": "Uzbekistan 🇺🇿",
    "VU": "Vanuatu 🇻🇺", "VE": "Venezuela 🇻🇪", "VN": "Vietnam 🇻🇳", "VI": "U.S. Virgin Islands 🇻🇮",
    "WF": "Wallis & Futuna 🇼🇫", "EH": "Western Sahara 🇪🇭", "YE": "Yemen 🇾🇪", "ZM": "Zambia 🇿🇲",
    "ZW": "Zimbabwe 🇿🇼"
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
    return ISO_COUNTRY_MAP.get(code.upper(), f"{code.upper()} 🏳️")

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
    balance_usdt = balance_inr / 95.0
    
    settings = settings_col.find_one({"_id": "server_config"}) or {}
    s1 = settings.get("s1", "active")
    s2 = settings.get("s2", "active")
    s3 = settings.get("s3", "maintenance")

    text = (
        f"🍷 <b>Buy Ready Telegram Accounts</b>:\n"
        f"––––––—————––––——–––•\n"
        f"<u>• One-click Telegram account purchase\n"
        f"• 100% activation & code delivery\n"
        f"• All accounts are clean [100% No Spam]\n"
        f"• Request multiple codes for free</u>\n"
        f"<b>• Total balance -</b> ₹{balance_inr:.2f} | ${balance_usdt:.2f}"
    )

    kb = InlineKeyboardBuilder()
    
    # S1 Route
    if s1 == "active":
        kb.row(InlineKeyboardButton(text="◍ Server- 1 (Old Accounts)", callback_data="buy_server1_route"))
    else:
        kb.row(InlineKeyboardButton(text="◍ Server- 1 [Maintenance 🛠️]", callback_data="server_maintenance"))
        
    # S2 Route
    if s2 == "active":
        kb.row(InlineKeyboardButton(text="◍ Server- 2 (Good Quality)", callback_data="buy_server2:0"))
    else:
        kb.row(InlineKeyboardButton(text="◍ Server- 2 [Maintenance 🛠️]", callback_data="server_maintenance"))
        
    # S3 Route
    if s3 == "active":
        kb.row(InlineKeyboardButton(text="◍ Server- 3 (Cheap Phising)", callback_data="s3tg_open"))
    else:
        kb.row(InlineKeyboardButton(text="◍ Server- 3 [Maintenance 🛠]", callback_data="server_maintenance"))

    kb.row(InlineKeyboardButton(text="▪️ Previous", callback_data="back_main"))

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
    for c in page_items:
        inr_price = c['price'] * 95.0
        btn_text = f"{c['name']} (₹{inr_price:.2f})"
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
    kb.row(InlineKeyboardButton(text="🔍 Search", callback_data="srv2_search_init"))
    
    # 4. Big Back Button Row
    kb.row(InlineKeyboardButton(text="🔙 Back to Servers", callback_data="buy"))
    
    text = (
        f"<b>📱 Telegram Accounts — Server (2)</b>\n\n"
        f"<blockquote>🌍 Select a country — to buy showing cheapest accounts first:</blockquote>"
    )
    
    await cq.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())

# --- SEARCH LOGIC (With Country, Code, and Prefix) ---
@dp.callback_query(F.data == "srv2_search_init")
async def srv2_search_init(cq: CallbackQuery, state: FSMContext):
    await cq.message.answer(
        "🔍 <b>Enter Country Name, Code, or Phone Prefix:</b>\n"
        "<i>Ex: India, IN, ZM, +1, 880...</i>", 
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
            [InlineKeyboardButton(text="🔙 Back to List", callback_data="buy_server2:0")]
        ])
        return await status_msg.edit_text(f"❌ No results found for '<b>{msg.text}</b>'.", parse_mode="HTML", reply_markup=kb)
        
    kb = InlineKeyboardBuilder()
    for c in results[:20]:
        inr_price = c['price'] * 95.0
        kb.button(text=f"{c['name']} (₹{inr_price:.2f})", callback_data=f"srv2_info:{c['code']}:{c['price']:.4f}")
    kb.adjust(2)
    
    # Large buttons for search results navigation
    kb.row(InlineKeyboardButton(text="🔍 Search Again", callback_data="srv2_search_init"))
    kb.row(InlineKeyboardButton(text="🔙 Back", callback_data="buy_server2:0"))
    
    await status_msg.edit_text(f"🔎 <b>Search Results for '{msg.text}':</b>", parse_mode="HTML", reply_markup=kb.as_markup())

# --- DETAILS VIEW (STOCK & PRICE) ---
@dp.callback_query(F.data.startswith("srv2_info:"))
async def srv2_country_info(cq: CallbackQuery):
    parts = cq.data.split(":")
    code = parts[1]
    usd_price = float(parts[2])
    inr_price = usd_price * 95.0
    
    await cq.message.edit_text("🔄 <i>Loading country details...</i>", parse_mode="HTML")
    countries, _ = await fetch_server2_countries()
    c_data = next((c for c in countries if c['code'] == code), None)
    
    if not c_data:
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back", callback_data="buy_server2:0")]])
        return await cq.message.edit_text("❌ This country is currently out of stock.", reply_markup=kb)

    text = (
        f"🌍 <b>Country Details</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🌐 <b>Country:</b> {c_data['name']}\n"
        f"🏷️ <b>Price:</b> ₹{inr_price:.2f}\n"
        f"📦 <b>Stock Available:</b> {c_data['qty']} pcs\n"
        f"━━━━━━━━━━━━━━━━━━━━━"
    )
    
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="🛒 Buy Now", callback_data=f"srv2_terms:{code}:{usd_price:.4f}"))
    kb.row(
        InlineKeyboardButton(text="🔙 Back", callback_data="buy_server2:0"),
        InlineKeyboardButton(text="🔄 Change Country", callback_data="buy_server2:0")
    )
    await cq.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())

# --- TERMS AND CONDITIONS ---
@dp.callback_query(F.data.startswith("srv2_terms:"))
async def srv2_show_terms(cq: CallbackQuery):
    _, code, usd_price = cq.data.split(":")
    usd_price = float(usd_price)
    inr_price = usd_price * 95.0
    
    settings = settings_col.find_one({"_id": "server_config"}) or {}
    active_api = settings.get("s2_api", "tgpva")
    country_display_name = get_country_name(code) if active_api == "tgpva" else code
    
    terms_text = (
        f"⚠️ <b>Server 2 (Good Quality) T&C</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🌐 <b>Country:</b> {country_display_name}\n"
        f"🚫 <b>No Refunds:</b> All sales are final.\n"
        f"📨 <b>OTP Policy:</b> Infinite codes supported.\n"
        f"⏳ <b>Warranty:</b> Active session ends after logout.\n\n"
        f"🏷️ <b>Price:</b> ₹{inr_price:.2f}\n"
        f"❓ <b>Accept these terms to continue?</b>"
    )
    
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="✅ Accept", callback_data=f"srv2_buy:{code}:{usd_price:.4f}"))
    kb.row(InlineKeyboardButton(text="🔙 Back", callback_data=f"srv2_info:{code}:{usd_price:.4f}"))
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
    inr_price = usd_price * 95.0
    user_id = cq.from_user.id
    user = get_or_create_user(user_id, cq.from_user.username)
    
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
           InlineKeyboardButton(text="Copy Num", copy_text=CopyTextButton(text=str(phone_number))))
    kb.row(InlineKeyboardButton(text="♻️ Buy Again", callback_data=f"srv2_info:{code}:{usd_price:.4f}"))
    kb.row(InlineKeyboardButton(text="• Support •", url=f"https://t.me/{SUPPORT}"))

    await status_msg.edit_text(
        f"<pre>✅ Purchased Successfully!</pre>\n"
        f"➖ <b><u>Server</u></b>: Server 2 (Good Quality)\n"
        f"➖ <b><u>Country</u></b>: {country_display_name}\n"
        f"📞 <b>Number:</b> <code>{phone_number}</code>\n"
        f"🏷️ <b>Price:</b> ₹{inr_price:.2f}\n"
        f"💸<b> Balance:</b> ₹{new_balance:.2f}",
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
        kb.row(InlineKeyboardButton(text="Copy OTP", copy_text=CopyTextButton(text=str(code))),
               InlineKeyboardButton(text="Copy Pass", copy_text=CopyTextButton(text=str(password))))
        kb.row(InlineKeyboardButton(text="🔄 Get New Code", callback_data=f"srv2_otp:{phone_number}:{order_hash}"))
        
        # LOGOUT BUTTON SIRF YAHAN DIKHEGA (OTP Milne ke baad)
        if active_api == "tgpva":
            kb.row(InlineKeyboardButton(text="🚪 Logout Device", callback_data=f"srv2_logout:{phone_number}"))
            
        await cq.message.edit_text(
            f"<pre>Order Completed ✅</pre>\n"
            f"✅ 𝐍𝗨𝐌𝐁𝐄𝐑 - <code>{phone_number}</code>\n"
            f"💬 𝐂𝐎𝐃𝐄 - <code>{code}</code>\n"
            f"💬 𝐏𝐀𝐒𝐒 - <code>{password}</code>",
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
        return await cq.message.edit_text("❌ No countries available. Admin must add stock first.")
    user_id = cq.from_user.id
    full_name = cq.from_user.full_name  # always use the name
    user_mention = f"<a href='tg://user?id={user_id}'>{full_name}</a>"
    user = users_col.find_one({"_id": user_id})
    balance = f"₹{user['balance']:.2f} " if user else "₹0 "

    # Pagination logic
    start = page * COUNTRIES_PER_PAGE
    end = start + COUNTRIES_PER_PAGE
    paginated = countries[start:end]

    kb = InlineKeyboardBuilder()
    for c in paginated:
        kb.button(text=html.escape(c["name"]), callback_data=f"country:{c['name']}")
    kb.adjust(2)

    # Navigation buttons
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton(text="▪️Previous", callback_data=f"countries_page:{page-1}"))
    if end < total:
        nav_buttons.append(InlineKeyboardButton(text="Next▪️", callback_data=f"countries_page:{page+1}"))
    if nav_buttons:
        kb.row(*nav_buttons)

    # Return to main menu
    kb.row(InlineKeyboardButton(text="▪️Home", callback_data="back_main"))

    text = f"<b><u>Buy SpamFree Telegram accounts:</u></b>\n––––––––––––––————––•\n◍ <u><b>Total balance:</b></u> {balance}  \n<u>◍ Server:</u> Server (1)\n◍ <b>Page </b>{page+1} of {(total - 1)//COUNTRIES_PER_PAGE + 1}\n✅ <a href=\"https://t.me/{SALESLOG}\">Sucessful Purchases</a>\n➖➖➖➖➖➖➖➖➖➖➖"
    await cq.message.edit_text(text, reply_markup=kb.as_markup(),parse_mode="HTML", disable_web_page_preview=True)


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

    text = (
        f"<b>Click Buy to Purchase an account:</b>\n––––––––––––—————–•\n"
        f"<blockquote> <b>Country: {html.escape(country['name'])}</b> </blockquote>\n"
        f"◍ <b><u>Price</u></b>: ₹{country['price']}\n"
        f"◍ <b><u>Stock</u></b>: {country['stock']}\n"
        f"◍ <b><u>Server</u></b> - (1)\n––––––––––––—————–•"
    )

    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="▪️ Buy", callback_data=f"buy_now:{country_name}")
    )
    kb.row(
        InlineKeyboardButton(text="▪️ Back", callback_data="buy")
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

    country_price = country["price"]
    country_stock = country["stock"]
    user_balance = user.get("balance", 0.0)

    # 2. Validate Stock
    if country_stock < 1:
        return await cq.answer(f"⚠️ Out of Stock! No accounts left for {country_name}.", show_alert=True)

    # 3. Validate Balance (If low, show Recharge prompt immediately)
    if user_balance < country_price:
        text = (
            f"🚫 <b>Insufficient Balance</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"💵 <b>Required:</b> ₹{country_price:.2f}\n"
            f"💰 <b>Your Balance:</b> ₹{user_balance:.2f}\n"
            f"📉 <b>Shortage:</b> ₹{country_price - user_balance:.2f}\n\n"
            f"<i>Please recharge your wallet to continue.</i>"
        )
        kb = InlineKeyboardBuilder()
        kb.row(InlineKeyboardButton(text="💳 Recharge Now", callback_data="recharge"))
        kb.row(InlineKeyboardButton(text="🔙 Back", callback_data=f"country:{country_name}"))
        
        return await cq.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())

    # 4. Show Terms & Confirmation (Balance & Stock are OK)
    terms_text = (
        f"⚠️ <b>Account Buying Terms</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"Please review the rules before purchasing <b>1 {country_name}</b> account:\n\n"
        f"🚫 <b>No Refunds:</b> All sales are final.\n"
        f"📨 <b>OTP Policy:</b> Once OTP is received, no return allowed.\n"
        f"❄️ <b>Freeze/Limit:</b> Accounts are fresh; we are not responsible for limits after use.\n"
        f"⏳ <b>Warranty:</b> 10 Minutes to check the account.\n\n"
        f"🏷️ <b>Price:</b> ₹{country_price:.2f}\n"
        f"❓ <b>Do you accept these terms?</b>"
    )

    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="✅ Accept", callback_data=f"confirm_buy:{country_name}"),
        InlineKeyboardButton(text="❌ Decline", callback_data=f"country:{country_name}")
    )
    kb.row(InlineKeyboardButton(text="🔙 Back", callback_data=f"country:{country_name}"))

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

    if user["balance"] < country["price"]:
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
    price = country["price"]
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
                    text="• Get OTP",
                    callback_data=f"get_otp:{number_doc['number']}"
                ),
                InlineKeyboardButton(
                    text="Copy Num",
                    copy_text=CopyTextButton(text=text_to_copy)
                )
            ],
            [
                InlineKeyboardButton(
                    text="• Support •",                    
                    url=f"https://t.me/{SUPPORT}"
                )
            ]
        ]
    )

    success_msg = (
        f"<pre>✅ Purchased Successfully!</pre>\n"
        f"➖ <b><u>Server</u></b>:  Server (1)\n"
        f"➖<b> <u>Country:</u></b> {country_name}\n"
        f"📞 <b>Number:</b> <code>+{number_doc['number']}</code>\n"
        f"🏷️ <b>Price:</b> ₹{price}\n"
        f"💸<b> Balance:</b> ₹{new_balance:.2f}"
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
        kb.button(text=c["name"], callback_data=f"add_country:{c['name']}")
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
        kb.button(text=c["name"], callback_data=f"remove_country:{c['name']}")
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
        kb.button(text=c["name"], callback_data=f"removecountry:{c['name']}")
    kb.adjust(2)
    await msg.answer("🌍 Select a country to remove:", reply_markup=kb.as_markup())

@dp.callback_query(F.data.startswith("removecountry:"))
async def callback_remove_country(cq: CallbackQuery):
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
        "<code>+1 USA 🇺🇸 - ₹10</code>\n"
        "<code>+91 India 🇮🇳 - ₹29</code>\n"
        "<code>+232 Sierra Leone 🇸🇱 - ₹13</code>\n\n"
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
            "<code>+Code CountryName Flag - ₹Price</code>\n"
            "Example:\n<code>+232 Sierra Leone 🇸🇱 - 13</code>", 
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
@dp.message(Command("managestore"))
async def cmd_managestore(msg: Message):
    if not is_admin(msg.from_user.id):
        return await msg.answer("❌ Not authorized.")

    settings = settings_col.find_one({"_id": "server_config"}) or {}
    store_status = settings.get("store_status", "on")

    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="➕ Add Product", callback_data="store_add"),
        InlineKeyboardButton(text="🗑️ Remove Product", callback_data="store_remove")
    )
    kb.row(
        InlineKeyboardButton(
            text=f"Store: {'🟢 ON' if store_status == 'on' else '🔴 OFF'}", 
            callback_data="store_toggle"
        )
    )
    kb.row(InlineKeyboardButton(text="❌ Close", callback_data="delete_msg"))

    await msg.answer("🏪 <b>Store Management Dashboard</b>\n\nWhat would you like to do?", reply_markup=kb.as_markup())

@dp.callback_query(F.data == "store_toggle")
async def toggle_store(cq: CallbackQuery):
    if not is_admin(cq.from_user.id): return
    settings = settings_col.find_one({"_id": "server_config"}) or {}
    current = settings.get("store_status", "on")
    new_status = "off" if current == "on" else "on"
    settings_col.update_one({"_id": "server_config"}, {"$set": {"store_status": new_status}}, upsert=True)
    
    await cq.answer(f"Store turned {new_status.upper()}", show_alert=True)
    await cmd_managestore(cq.message) # Refresh dashboard

@dp.callback_query(F.data == "store_add")
async def store_add_start(cq: CallbackQuery, state: FSMContext):
    kb = InlineKeyboardBuilder()
    kb.button(text="1. Bot Source Codes", callback_data="st_cat:source_codes")
    kb.button(text="2. Buy OTP Panels", callback_data="st_cat:otp_panels")
    kb.adjust(1)
    await cq.message.edit_text("📂 <b>Select Category for new product:</b>", reply_markup=kb.as_markup())
    await state.set_state(StoreAdmin.waiting_category)

@dp.callback_query(F.data.startswith("st_cat:"))
async def store_add_cat(cq: CallbackQuery, state: FSMContext):
    cat = cq.data.split(":")[1]
    await state.update_data(category=cat)
    await cq.message.edit_text("📝 <b>Enter Product Name:</b>\n<i>(Ex: VIP Telegram Bot Source)</i>")
    await state.set_state(StoreAdmin.waiting_name)

@dp.message(StateFilter(StoreAdmin.waiting_name))
async def store_add_name(msg: Message, state: FSMContext):
    await state.update_data(name=msg.text)
    
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⏭️ Skip File", callback_data="skip_file")]])
    await msg.answer("📁 <b>Upload the File (Zip/Document)</b>\nOr click Skip if it's only text/links.", reply_markup=kb)
    await state.set_state(StoreAdmin.waiting_file)

@dp.message(StateFilter(StoreAdmin.waiting_file), F.document)
async def store_add_file(msg: Message, state: FSMContext):
    await state.update_data(file_id=msg.document.file_id)
    await msg.answer("💬 <b>Enter Product Description / Text:</b>")
    await state.set_state(StoreAdmin.waiting_text)

@dp.callback_query(F.data == "skip_file")
async def store_skip_file(cq: CallbackQuery, state: FSMContext):
    await state.update_data(file_id=None)
    await cq.message.edit_text("💬 <b>Enter Product Description / Text:</b>")
    await state.set_state(StoreAdmin.waiting_text)

@dp.message(StateFilter(StoreAdmin.waiting_text))
async def store_add_text(msg: Message, state: FSMContext):
    await state.update_data(text=msg.html_text)
    await msg.answer("⏳ <b>Enter Auto-Delete Time (in minutes):</b>\n<i>Ex: 30 (Message will delete after 30 mins of purchase)</i>")
    await state.set_state(StoreAdmin.waiting_time)

@dp.message(StateFilter(StoreAdmin.waiting_time))
async def store_add_time(msg: Message, state: FSMContext):
    if not msg.text.isdigit(): return await msg.answer("❌ Enter a valid number.")
    await state.update_data(del_time=int(msg.text))
    await msg.answer("📦 <b>Enter Quantity (Stock):</b>\n<i>Ex: 10</i>")
    await state.set_state(StoreAdmin.waiting_stock)

@dp.message(StateFilter(StoreAdmin.waiting_stock))
async def store_add_stock(msg: Message, state: FSMContext):
    if not msg.text.isdigit(): return await msg.answer("❌ Enter a valid number.")
    await state.update_data(stock=int(msg.text))
    await msg.answer("💰 <b>Enter Price (in ₹):</b>\n<i>Ex: 500</i>")
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
    await msg.answer(f"✅ <b>Product Added Successfully!</b>\nName: {data['name']}\nPrice: ₹{price}\nStock: {data['stock']}")
    await state.clear()

# ================= USER STORE FLOW =================
@dp.callback_query(F.data == "store_menu")
async def user_store_menu(cq: CallbackQuery):
    settings = settings_col.find_one({"_id": "server_config"}) or {}
    if settings.get("store_status", "on") == "off":
        return await cq.answer("⚠️ Store is currently OFF. Please try again later.", show_alert=True)

    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="🤖 Bot Source Codes", callback_data="usr_cat:source_codes"))
    kb.row(InlineKeyboardButton(text="🚀 Buy OTP Panels", callback_data="usr_cat:otp_panels"))
    kb.row(InlineKeyboardButton(text="🔙 Back", callback_data="back_main"))

    await cq.message.edit_text("🏪 <b>Welcome to the Digital Store!</b>\n\n<i>Select a category to browse:</i>", reply_markup=kb.as_markup())

@dp.callback_query(F.data.startswith("usr_cat:"))
async def view_category(cq: CallbackQuery):
    cat = cq.data.split(":")[1]
    products = list(store_col.find({"category": cat, "stock": {"$gt": 0}}))
    
    if not products:
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back", callback_data="store_menu")]])
        return await cq.message.edit_text("📭 No stock available in this category right now.", reply_markup=kb)

    kb = InlineKeyboardBuilder()
    for p in products:
        kb.button(text=f"{p['name']} (₹{p['price']})", callback_data=f"buy_prod:{str(p['_id'])}")
    kb.adjust(1)
    kb.row(InlineKeyboardButton(text="🔙 Back", callback_data="store_menu"))

    cat_name = "Bot Source Codes" if cat == "source_codes" else "OTP Panels"
    await cq.message.edit_text(f"📂 <b>{cat_name}</b>\n\n<i>Select a product to buy:</i>", reply_markup=kb.as_markup())

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

    price = product["price"]
    
    text = (
        f"🛍️ <b>Confirm Purchase</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"📌 <b>Product:</b> {product['name']}\n"
        f"💰 <b>Price:</b> ₹{price}\n"
        f"📦 <b>Stock Left:</b> {product['stock']}\n\n"
        f"❓ <i>Are you sure you want to purchase this item?</i>"
    )
    
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="✅ Yes, Purchase", callback_data=f"confirm_buy_prod:{prod_id}"))
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
    price = product["price"]

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
    kb.row(InlineKeyboardButton(text="🔙 Back", callback_data="delete_msg"))
    kb.adjust(1)
    
    await cq.message.edit_text("🗑️ <b>Select Category to delete a product from:</b>", reply_markup=kb.as_markup())

@dp.callback_query(F.data.startswith("rem_cat:"))
async def store_remove_list(cq: CallbackQuery):
    if not is_admin(cq.from_user.id): return
    cat = cq.data.split(":")[1]
    
    # DB se category ke saare products nikalo
    products = list(store_col.find({"category": cat}))
    
    if not products:
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back", callback_data="store_remove")]])
        return await cq.message.edit_text("📭 <b>No products found in this category!</b>", reply_markup=kb)

    kb = InlineKeyboardBuilder()
    for p in products:
        # Delete button with Product ID
        kb.button(
            text=f"❌ Delete: {p['name']} (₹{p['price']})", 
            callback_data=f"del_prod:{str(p['_id'])}"
        )
    kb.adjust(1)
    kb.row(InlineKeyboardButton(text="🔙 Back", callback_data="store_remove"))

    cat_name = "Bot Source Codes" if cat == "source_codes" else "OTP Panels"
    await cq.message.edit_text(
        f"🗑️ <b>Delete Products - {cat_name}</b>\n\n"
        f"<i>⚠️ Click on any product below to permanently delete it:</i>", 
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
        kb.button(text=c["name"], callback_data=f"editcountry:{c['name']}")
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
    steps_text = (
        f"<b>◍ TGA Store | API</b>\n––––––——–––————––––——–––•\n"
        f"<blockquote><b>👤 Name: </b>{html.escape(cq.from_user.full_name)}\n"
        f"<b>💻 Username: </b>@{cq.from_user.username if cq.from_user.username else 'N/A'}\n"
        f"<b>🆔 User ID:</b> {cq.from_user.id}\n"
        f"<b>💰 Balance:</b> ₹{user.get('balance', 0.0):.2f}</blockquote>\n"
        f"––––––——–––————––––——–––•\n •<b> Bot</b>: @{BOTUSER}\n• <b>Sales Log</b>: @{SALESLOG}"
        
    )

    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="▪️ Support", url=f"https://t.me/{OWNER}"),
        InlineKeyboardButton(text="▪️ 𝙃𝙤𝙬 𝙩𝙤 𝙪𝙨𝙚", url=f"https://t.me/tec")
    )
    kb.row(
        InlineKeyboardButton(text="▪️ Previous", callback_data="back_main")
    )
    
    await cq.message.edit_text(steps_text, parse_mode="HTML", reply_markup=kb.as_markup())
    await cq.answer()

@dp.callback_query(F.data == "howto")
async def callback_howto(cq: CallbackQuery):
    await cq.answer() # Answer first
    steps_text = ("📚 FᴀQ & Sᴜᴘᴘᴏʀᴛ 😊\n\n🔗 𝙃𝙤𝙬 𝙩𝙤 𝙪𝙨𝙚:👉 {USAGE}\n💬 Oғғɪᴄɪᴀʟ Sᴜᴘᴘᴏʀᴛ:   👉 {SUPPORT}\n🤖 Oғғɪᴄɪᴀʟ Bᴏᴛ:     👉 {BOT_USER}\n\n🛟 Fᴇᴇʟ Fʀᴇᴇ Tᴏ Rᴇᴀᴄʜ Oᴜ𝙩 Iғ Yᴏᴜ Nᴇᴇᴅ Aɴʏ Hᴇʟᴘ!")
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="📲 Support", url=f"https://t.me/{SUPPORT}"),
        InlineKeyboardButton(text="🔗 𝙃𝙤𝙬 𝙩𝙤 𝙪𝙨𝙚", url=f"https://t.me/tec")
    )
    # Added back button
    kb.row(InlineKeyboardButton(text="🔙 Main Menu", callback_data="main_menu")) 
    
    await cq.message.edit_text(steps_text, parse_mode="HTML", reply_markup=kb.as_markup())


@dp.callback_query(lambda c: c.data == "refer")
async def callback_refer(cq: CallbackQuery):
    user = get_or_create_user(cq.from_user.id, cq.from_user.username)

    bot_username = (await bot.get_me()).username
    refer_link = f"https://t.me/{bot_username}?start=ref{cq.from_user.id}"

    total_referred = users_col.count_documents({"referred_by": cq.from_user.id})
    total_earned = user.get("referral_earnings", 0.0)

    text = (
        f"<b>⚡ Refer & Earn</b>\n\n"
        f"<blockquote>"
        f"💰 Earn <b>₹1.00</b> instantly for every friend who joins using your link!\n"
        f"Credited straight to your wallet — no waiting."
        f"</blockquote>\n\n"
        f"📊 <b>Your Referral Stats</b>\n"
        f"<blockquote>"
        f"👥 <b>Total Referred:</b> {total_referred}\n"
        f"💵 <b>Total Earned:</b> ₹{total_earned:.2f}"
        f"</blockquote>\n\n"
        f"🔗 <b>Your Referral Link:</b>\n<code>{refer_link}</code>"
    )

    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(
            text="📤 Share Link",
            url=f"https://t.me/share/url?url={refer_link}&text=Join%20and%20earn%20with%20this%20bot!"
        )
    )
    kb.row(InlineKeyboardButton(text="▪️ Back", callback_data="back_main"))

    await cq.message.edit_text(text=text, parse_mode="HTML", reply_markup=kb.as_markup())
    await cq.answer()
    
# ================= ADVANCED /sales COMMAND =================
@dp.message(Command("sales", "stats"))
async def cmd_advanced_sales(msg: Message):
    if not is_admin(msg.from_user.id):
        return await msg.answer("❌ <b>You are not authorized to view the sales report.</b>", parse_mode="HTML")

    status_msg = await msg.answer("🔄 <i>Generating advanced analytics... Please wait.</i>", parse_mode="HTML")

    # FIX: Using UTC Now (Naive Datetime) to match MongoDB
    now = datetime.utcnow()
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start_of_week = start_of_day - timedelta(days=now.weekday())

    # Helper function to prevent timezone crash (converts aware to naive safely)
    def make_naive(dt):
        if not dt: return None
        return dt.replace(tzinfo=None) if getattr(dt, 'tzinfo', None) else dt

    # Collections
    users_col = db["users"]
    orders_col = db["orders"]
    txns_col = db["transactions"]  
    wd_col = db["withdrawals"]

    # ---------------- 👥 USERS ANALYTICS ----------------
    total_users = users_col.count_documents({})
    # PyMongo handle directly with naive datetimes
    users_today = users_col.count_documents({"joined_at": {"$gte": start_of_day}})
    users_week = users_col.count_documents({"joined_at": {"$gte": start_of_week}})

    # ---------------- 💳 DEPOSITS (RECHARGES) ----------------
    all_txns = list(txns_col.find({"status": {"$in": ["paid", "success"]}}))
    total_recharge = sum(float(t.get("amount", 0)) for t in all_txns)
    
    # Safe python filtering with timezone bypass
    today_recharge = sum(float(t.get("amount", 0)) for t in all_txns if make_naive(t.get("created_at")) and make_naive(t.get("created_at")) >= start_of_day)
    week_recharge = sum(float(t.get("amount", 0)) for t in all_txns if make_naive(t.get("created_at")) and make_naive(t.get("created_at")) >= start_of_week)

    # ---------------- 🏦 WITHDRAWALS ----------------
    all_wds = list(wd_col.find({"status": "paid"}))
    total_wd = sum(float(w.get("amount", 0)) for w in all_wds)

    # ---------------- 🛒 OTP SALES ANALYTICS ----------------
    all_orders = list(orders_col.find({"status": "purchased"}))
    total_sold = len(all_orders)
    total_sales_inr = sum(float(o.get("price", 0)) for o in all_orders)

    today_orders = [o for o in all_orders if make_naive(o.get("created_at")) and make_naive(o.get("created_at")) >= start_of_day]
    today_sold = len(today_orders)
    today_sales_inr = sum(float(o.get("price", 0)) for o in today_orders)

    week_orders = [o for o in all_orders if make_naive(o.get("created_at")) and make_naive(o.get("created_at")) >= start_of_week]
    week_sold = len(week_orders)
    week_sales_inr = sum(float(o.get("price", 0)) for o in week_orders)

    # Top Country Logic
    from collections import Counter
    country_counts = Counter(o.get("country", "Unknown") for o in all_orders)
    top_country = country_counts.most_common(1)[0][0] if country_counts else "N/A"

    # ---------------- 📋 BUILD THE REPORT ----------------
    report = (
        "📊 <b>ADVANCED SYSTEM ANALYTICS</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "👥 <b>USER METRICS</b>\n"
        f"• <b>Total Users:</b> {total_users}\n"
        f"• <b>Joined Today:</b> {users_today}\n"
        f"• <b>Joined This Week:</b> {users_week}\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "💰 <b>FINANCIAL METRICS (INR)</b>\n"
        f"• <b>Total Deposits:</b> ₹{total_recharge:,.2f}\n"
        f"• <b>Deposits Today:</b> ₹{today_recharge:,.2f}\n"
        f"• <b>Total Withdrawals:</b> ₹{total_wd:,.2f}\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "🛒 <b>OTP SALES METRICS</b>\n"
        f"• <b>Total Numbers Sold:</b> {total_sold}\n"
        f"• <b>Gross Revenue:</b> ₹{total_sales_inr:,.2f}\n\n"
        f"📅 <b>Today's Sales:</b> {today_sold} (₹{today_sales_inr:,.2f})\n"
        f"📅 <b>Weekly Sales:</b> {week_sold} (₹{week_sales_inr:,.2f})\n\n"
        f"🌍 <b>Top Country:</b> {top_country}\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        f"🤖 <b>Bot Status:</b> 🟢 Active\n"
        f"👨‍💻 <b>Support:</b> @CuteSmileVip"
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
    await cq.answer("✅ Send your redeem code now!", show_alert=False)
    await cq.message.answer("🎟️ Send your redeem code below:")
    await state.set_state(UserRedeemState.waiting_code)

# Command /redeem
@dp.message(F.text == "/redeem")
async def command_user_redeem(message: Message, state: FSMContext):
    await message.answer("✅ Send your redeem code now!")
    await message.answer("🎟️ Send your redeem code below:")
    await state.set_state(UserRedeemState.waiting_code)

@dp.message(StateFilter(UserRedeemState.waiting_code))
async def handle_user_redeem(msg: Message, state: FSMContext):
    code = msg.text.strip().upper()
    redeem = redeem_col.find_one({"code": code})

    if not redeem:
        await msg.answer("❌ Invalid or expired redeem code.")
        return await state.clear()

    if redeem["claimed_count"] >= redeem["max_claims"]:
        await msg.answer("🚫 This code has reached its claim limit.")
        return await state.clear()

    user = users_col.find_one({"_id": msg.from_user.id})
    if not user:
        await msg.answer("⚠️ Please use /start first.")
        return await state.clear()

    if msg.from_user.id in redeem.get("claimed_users", []):
        await msg.answer("⚠️ You have already claimed this code.")
        return await state.clear()

    # Credit user balance
    users_col.update_one({"_id": msg.from_user.id}, {"$inc": {"balance": redeem["amount"]}})
    redeem_col.update_one(
        {"code": code},
        {"$inc": {"claimed_count": 1}, "$push": {"claimed_users": msg.from_user.id}}
    )

    await msg.answer(
        f"✅ Code <b>{code}</b> redeemed successfully!\n💰 You received ₹{redeem['amount']:.2f}",
        parse_mode="HTML"
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
    
    if not users_list:
        text = "📉 No users currently have a positive balance."
        kb = None
    else:
        text = f"💰 <b>Live Credits (Page {page+1})</b>\n\n"
        for u in users_list:
            u_link = f"<a href='tg://user?id={u['_id']}'>{u.get('username') or u['_id']}</a>"
            text += f"👤 {u_link} : <code>₹{u['balance']:.2f}</code>\n"
            
        kb = InlineKeyboardBuilder()
        if page > 0:
            kb.button(text="⬅️ Prev", callback_data=f"livecredits:{page-1}")
        if (skip + limit) < total_users:
            kb.button(text="Next ➡️", callback_data=f"livecredits:{page+1}")
        kb.adjust(2)
        kb.row(InlineKeyboardButton(text="❌ Close", callback_data="delete_msg"))

    if isinstance(msg_or_call, Message):
        await msg_or_call.answer(text, parse_mode="HTML", reply_markup=kb.as_markup() if kb else None)
    else:
        await msg_or_call.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup() if kb else None)

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
    txns = list(db["transactions"].find({"user_id": user_id, "status": {"$in": ["paid", "success"]}}))
    total_added = sum(t.get("amount", 0) for t in txns)
    
    # 2. Fetch Orders from BOTH Databases
    s12_orders = list(orders_col.find({"user_id": user_id, "status": "purchased"}))
    
    # 👇 FIX: Yahan 'lzt_orders' ki jagah 'db["lzt_orders"]' laga diya hai
    s3_orders = list(db["lzt_orders"].find({"user_id": user_id, "status": {"$in": ["purchased", "completed", "delivered"]}}))
    
    all_orders = s12_orders + s3_orders
    total_purchased = len(all_orders)
    
    # Calculate Total Spent
    total_spent = sum(o.get("price") or o.get("price_inr") or 0 for o in all_orders)
    
    text = (
        f"📜 <b>User History</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🛍️ <b>Accounts Bought:</b> {total_purchased}\n"
        f"💸 <b>Total Spent:</b> ₹{total_spent:.2f}\n"
        f"━━━━━━━━━━━━━━━━━━━━"
    )
    
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="📂 View Purchase Logs", callback_data="purchase_logs:0"))
    kb.row(InlineKeyboardButton(text="🔙 Back", callback_data="back_main")) 
    
    await cq.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())


@dp.callback_query(F.data.startswith("purchase_logs:"))
async def show_purchase_logs(cq: CallbackQuery):
    user_id = cq.from_user.id
    page = int(cq.data.split(":")[1])
    limit = 10
    skip = page * limit
    
    # Fetch from BOTH Databases
    s12_orders = list(orders_col.find({"user_id": user_id, "status": "purchased"}))
    
    # 👇 FIX: Yahan bhi 'db["lzt_orders"]' laga diya hai
    s3_orders = list(db["lzt_orders"].find({"user_id": user_id, "status": {"$in": ["purchased", "completed", "delivered"]}}))
    
    # Merge and sort by newest first
    all_orders = s12_orders + s3_orders
    all_orders.sort(key=lambda x: x["_id"], reverse=True)
    
    total_orders = len(all_orders)
    my_orders = all_orders[skip : skip + limit]
    
    if not my_orders:
        return await cq.answer("❌ No purchase history found.", show_alert=True)
    
    text = f"📂 <b>Purchase Logs (Page {page+1})</b>\n\n"
    
    for order in my_orders:
        ph_number = order.get('number')
        
        # 1. Pehle check karega ki Password direct order me hai kya? (Server 3)
        password = order.get("password")
        
        # 2. Agar nahi mila toh numbers_col check karega (Server 1 & 2)
        if not password:
            num_doc = db["numbers_col"].find_one({"number": ph_number}) # Yahan bhi db[] use kiya safe side ke liye
            password = num_doc.get("password") if num_doc else "N/A"
            
        if not password or password == "None": 
            password = "N/A"
        
        text += (
            f"📱 <b>{ph_number}</b>\n"
            f"🔐 Pass: <code>{password}</code>\n"
            f"-------------------\n"
        )
        
    kb = InlineKeyboardBuilder()
    
    nav_btns = []
    if page > 0:
        nav_btns.append(InlineKeyboardButton(text="⬅️", callback_data=f"purchase_logs:{page-1}"))
    if (skip + limit) < total_orders:
        nav_btns.append(InlineKeyboardButton(text="➡️", callback_data=f"purchase_logs:{page+1}"))
        
    if nav_btns:
        kb.row(*nav_btns)
        
    kb.row(InlineKeyboardButton(text="🔙 Back to History", callback_data="history"))
    
    await cq.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())

# ================= API KEYS (Admin Server 2) =================
TGLION_API_KEY = "C80mflegScDkM51bzH"
TGLION_ID = "7888066934"
TGPVA_API_KEY = "862Ddu3tLbHGpyYGK5BZhmavl3ljxRsF"

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
    active_api = settings.get("s2_api", "tgpva") # tglion or tgpva
    profit = settings.get("s2_profit", 0)

    # Fetch live balances
    tg_lion_bal, tgpva_bal = await get_api_balances()

    text = (
        "🎛️ <b>Server Management Dashboard</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Server 1 (Cheap):</b> {s1.upper()}\n"
        f"<b>Server 2 (Mixed):</b> {s2.upper()}\n"
        f"<b>Server 3 (New):</b> {s3.upper()}\n\n"
        f"<b>⚙️ Server 2 Active API:</b> <code>{active_api.upper()}</code>\n"
        f"<b>💰 Server 2 Profit Margin:</b> {profit}%\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "💳 <b>Live API Balances:</b>\n"
        f"🦁 TG-Lion: <code>{tg_lion_bal}</code>\n"
        f"🛡️ TGPVA: <code>{tgpva_bal}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━━"
    )

    kb = InlineKeyboardBuilder()
    # Toggles for S1, S2, S3
    kb.row(
        InlineKeyboardButton(text=f"S1: {'🔴 Off' if s1=='active' else '🟢 On'}", callback_data="toggle_s1"),
        InlineKeyboardButton(text=f"S2: {'🔴 Off' if s2=='active' else '🟢 On'}", callback_data="toggle_s2"),
        InlineKeyboardButton(text=f"S3: {'🔴 Off' if s3=='active' else '🟢 On'}", callback_data="toggle_s3")
    )
    # API Switcher & Profit
    kb.row(
        InlineKeyboardButton(text="🔄 Switch S2 API", callback_data="switch_s2_api"),
        InlineKeyboardButton(text="📈 Set Profit %", callback_data="set_s2_profit")
    )
    kb.row(
        InlineKeyboardButton(text="🔄 Refresh Balances", callback_data="refresh_dashboard"),
        InlineKeyboardButton(text="❌ Close", callback_data="delete_msg")
    )

    if message_id:
        await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, parse_mode="HTML", reply_markup=kb.as_markup())
    else:
        await bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=kb.as_markup())

# Callbacks for toggles
@dp.callback_query(F.data.in_(["toggle_s1", "toggle_s2", "toggle_s3", "switch_s2_api", "refresh_dashboard"]))
async def manage_toggles(cq: CallbackQuery):
    action = cq.data
    settings = settings_col.find_one({"_id": "server_config"}) or {"_id": "server_config"}
    
    if action == "toggle_s1":
        settings["s1"] = "maintenance" if settings.get("s1", "active") == "active" else "active"
    elif action == "toggle_s2":
        settings["s2"] = "maintenance" if settings.get("s2", "active") == "active" else "active"
    elif action == "toggle_s3":
        settings["s3"] = "maintenance" if settings.get("s3", "maintenance") == "active" else "active"
    elif action == "switch_s2_api":
        settings["s2_api"] = "tglion" if settings.get("s2_api", "tgpva") == "tgpva" else "tgpva"
    
    if action != "refresh_dashboard":
        settings_col.update_one({"_id": "server_config"}, {"$set": settings}, upsert=True)
        
    await send_server_dashboard(cq.message.chat.id, cq.message.message_id)
    await cq.answer("Dashboard Updated!")

@dp.callback_query(F.data == "set_s2_profit")
async def set_profit_btn(cq: CallbackQuery, state: FSMContext):
    await cq.message.answer("📈 Send the new profit percentage for Server 2 (e.g., send 20 for 20%):")
    await state.set_state(ServerManage.waiting_profit_margin)
    await cq.answer()

@dp.message(StateFilter(ServerManage.waiting_profit_margin))
async def save_profit_margin(msg: Message, state: FSMContext):
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
        f"<b>👤 Full User Profile</b>\n\n"
        f"<blockquote>"
        f"🆔 <b>User ID:</b> <code>{target_id}</code>\n"
        f"📛 <b>Username:</b> @{user.get('username') or 'NoUsername'}\n"
        f"📅 <b>Joined:</b> {joined_str}"
        f"</blockquote>\n\n"
        f"<blockquote>"
        f"💳 <b>Current Balance:</b> ₹{user.get('balance', 0.0):.2f}\n"
        f"💰 <b>Total Deposited:</b> ₹{total_deposited:.2f}\n"
        f"🛒 <b>Total Spent:</b> ₹{total_spent:.2f}\n"
        f"📦 <b>Total Orders:</b> {total_orders}"
        f"</blockquote>\n\n"
        f"<blockquote>"
        f"🔗 <b>Referred By:</b> {f'<code>{referred_by}</code>' if referred_by else 'None'}\n"
        f"👥 <b>Total Referred Users:</b> {total_referred}\n"
        f"💵 <b>Referral Earnings:</b> ₹{referral_earnings:.2f}"
        f"</blockquote>"
    )
    await msg.answer(text, parse_mode="HTML")        
@dp.callback_query(lambda c: c.data == "transfer_balance")
async def transfer_balance_start(cq: CallbackQuery, state: FSMContext):
    await state.set_state(TransferBalance.waiting_recipient)

    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(
            text="▪️ Cancel",
            callback_data="back_main"
        )
    )

    await cq.message.edit_text(
        "<b>💸 Transfer Balance</b>\n\n"
        "<blockquote>"
        "Send the Telegram User ID of the person you want to send money to."
        "</blockquote>",
        parse_mode="HTML",
        reply_markup=kb.as_markup()
    )

    await cq.answer()


@dp.message(StateFilter(TransferBalance.waiting_recipient))
async def transfer_balance_recipient(msg: Message, state: FSMContext):

    if not msg.text.strip().isdigit():
        return await msg.answer(
            "❌ Please send a valid numeric User ID."
        )

    recipient_id = int(msg.text.strip())

    if recipient_id == msg.from_user.id:
        return await msg.answer(
            "❌ You cannot transfer balance to yourself."
        )

    recipient = users_col.find_one({
        "_id": recipient_id
    })

    if not recipient:
        return await msg.answer(
            "❌ This user hasn't started the bot yet. "
            "Ask them to press /start first."
        )

    await state.update_data(
        recipient_id=recipient_id
    )

    await state.set_state(
        TransferBalance.waiting_amount
    )

    await msg.answer(
        f"✅ Recipient found: <code>{recipient_id}</code>\n\n"
        f"💰 <b>Enter the amount (in ₹) to send:</b>",
        parse_mode="HTML"
    )


@dp.message(StateFilter(TransferBalance.waiting_amount))
async def transfer_balance_amount(msg: Message, state: FSMContext):

    try:
        amount = float(msg.text.strip())

    except ValueError:
        return await msg.answer(
            "❌ Please send a valid number."
        )

    if amount <= 0:
        return await msg.answer(
            "❌ Amount must be greater than 0."
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
            f"❌ Insufficient balance. "
            f"Your balance: ₹{sender_balance:.2f}"
        )

    data = await state.get_data()

    recipient_id = data["recipient_id"]

    await state.update_data(
        amount=amount
    )

    kb = InlineKeyboardBuilder()

    kb.row(
        InlineKeyboardButton(
            text="✅ Confirm",
            callback_data="transfer_confirm"
        ),
        InlineKeyboardButton(
            text="❌ Cancel",
            callback_data="back_main"
        )
    )

    await msg.answer(
        f"<b>🧾 Confirm Transfer</b>\n\n"
        f"<blockquote>"
        f"👤 <b>To:</b> <code>{recipient_id}</code>\n"
        f"💰 <b>Amount:</b> ₹{amount:.2f}"
        f"</blockquote>",
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
                    text="🔙 Back to Menu",
                    callback_data="back_main"
                )
            ]
        ]
    )

    # ==================================================
    # ✅ SHOW SUCCESS TO SENDER
    # ==================================================

    await cq.message.edit_text(
        f"<b>✅ Transfer Successful!</b>\n\n"
        f"<blockquote>"
        f"👤 <b>Sent To:</b> "
        f"<code>{recipient_id}</code>\n"
        f"💰 <b>Amount:</b> "
        f"₹{amount:.2f}\n"
        f"💳 <b>Your New Balance:</b> "
        f"₹{new_sender_balance:.2f}"
        f"</blockquote>",
        parse_mode="HTML",
        reply_markup=kb
    )

    # ==================================================
    # 💰 NOTIFY RECIPIENT
    # ==================================================

    try:

        await bot.send_message(
            recipient_id,

            f"<b>💰 You've Received a Transfer!</b>\n\n"
            f"<blockquote>"
            f"👤 <b>From:</b> "
            f"<code>{cq.from_user.id}</code>\n"
            f"💰 <b>Amount:</b> "
            f"₹{amount:.2f}"
            f"</blockquote>",

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
            f"💸 <b>Balance Transfer</b>\n\n"
            f"👤 <b>From:</b> <code>{cq.from_user.id}</code> (@{cq.from_user.username or 'NoUsername'})\n"
            f"👤 <b>To:</b> <code>{recipient_id}</code>\n"
            f"💰 <b>Amount:</b> ₹{amount:.2f}\n\n"
            f"💳 <b>Sender's Remaining Balance:</b> ₹{new_sender_balance:.2f}\n"
            f"💳 <b>Recipient's New Balance:</b> ₹{recipient_new_balance:.2f}",
            parse_mode="HTML"
        )
    except Exception:
        pass

    # ==================================================
    # ✅ ANSWER CALLBACK
    # ==================================================

    await cq.answer()
# ================= Professional Admin Control Center =================
def admin_panel_markup() -> InlineKeyboardMarkup:
    features = get_feature_settings()
    smm = "ON ✅" if features.get("smm_enabled", True) else "OFF ❌"
    wa = "ON ✅" if features.get("whatsapp_enabled", True) else "OFF ❌"
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=f"🚀 SMM: {smm}", callback_data="admin_toggle:smm"),
            InlineKeyboardButton(text=f"📞 WhatsApp: {wa}", callback_data="admin_toggle:whatsapp"),
        ],
        [
            InlineKeyboardButton(text="🖥 Server Control", callback_data="admin_help:servers"),
            InlineKeyboardButton(text="💳 Payments", callback_data="admin_help:payments"),
        ],
        [
            InlineKeyboardButton(text="🏪 Store Control", callback_data="admin_help:store"),
            InlineKeyboardButton(text="🌐 SMM Control", callback_data="admin_help:smm"),
        ],
        [
            InlineKeyboardButton(text="👥 Users & Balance", callback_data="admin_help:users"),
            InlineKeyboardButton(text="📦 Stock & Countries", callback_data="admin_help:stock"),
        ],
        [
            InlineKeyboardButton(text="🎟 Redeem Codes", callback_data="admin_help:redeem"),
            InlineKeyboardButton(text="📊 Reports", callback_data="admin_help:reports"),
        ],
        [InlineKeyboardButton(text="📣 Broadcast", callback_data="admin_help:broadcast")],
        [InlineKeyboardButton(text="🔄 Refresh Panel", callback_data="admin_refresh")],
    ])


async def render_admin_panel(message: Message, edit: bool = False):
    users = users_col.count_documents({})
    orders = orders_col.count_documents({})
    stock = numbers_col.count_documents({"used": {"$ne": True}})
    text = (
        "<h2>TGA Store | API — Admin</h2>"
        "<blockquote>Central control center</blockquote>"
        f"<p>👥 <b>Users:</b> <code>{users:,}</code><br>"
        f"📦 <b>Orders:</b> <code>{orders:,}</code><br>"
        f"📱 <b>Available stock:</b> <code>{stock:,}</code></p>"
        "<p>Use the switches below to instantly show or hide SMM and WhatsApp "
        "from every user's main menu. Open a section to see its management commands.</p>"
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


@dp.callback_query(F.data == "admin_refresh")
async def admin_refresh(cq: CallbackQuery):
    if not is_admin(cq.from_user.id):
        return await cq.answer("Access denied", show_alert=True)
    await render_admin_panel(cq.message, edit=True)
    await cq.answer("Panel refreshed")


@dp.callback_query(F.data.startswith("admin_toggle:"))
async def admin_toggle_feature(cq: CallbackQuery):
    if not is_admin(cq.from_user.id):
        return await cq.answer("Access denied", show_alert=True)
    feature = cq.data.split(":", 1)[1]
    field = {"smm": "smm_enabled", "whatsapp": "whatsapp_enabled"}.get(feature)
    if not field:
        return await cq.answer("Unknown switch", show_alert=True)
    current = get_feature_settings().get(field, True)
    settings_col.update_one(
        {"_id": "bot_features"}, {"$set": {field: not current}}, upsert=True
    )
    await render_admin_panel(cq.message, edit=True)
    await cq.answer(f"{feature.title()} {'enabled' if not current else 'disabled'}")


ADMIN_HELP = {
    "servers": "<b>Server controls</b><br><code>/manageserver</code> — API servers, balances, profit and maintenance",
    "payments": "<b>Payment controls</b><br><code>/paymentsettings</code> — gateways, crypto and payment details",
    "store": "<b>Store controls</b><br><code>/managestore</code> — enable, add, edit or remove products",
    "smm": "<b>SMM controls</b><br><code>/smmadmin</code> — panels, platforms, services, logs and orders",
    "users": "<b>User controls</b><br><code>/userinfo ID</code><br><code>/credit ID AMOUNT</code><br><code>/debit ID AMOUNT</code><br><code>/gban ID</code> / <code>/ungban ID</code>",
    "stock": "<b>Stock controls</b><br><code>/add</code> · <code>/remove</code> · <code>/db</code><br><code>/addcountry</code> · <code>/removecountry</code> · <code>/editcountry</code> · <code>/setprices</code>",
    "redeem": "<b>Redeem controls</b><br><code>/cusredeem</code> — create codes<br><code>/redeemlist</code> — list codes",
    "reports": "<b>Reports</b><br><code>/sales</code> · <code>/topusers</code> · <code>/livecredits</code>",
    "broadcast": "<b>Broadcast</b><br>Reply to any message with <code>/broadcast</code> to forward it to all users.",
}


@dp.callback_query(F.data.startswith("admin_help:"))
async def admin_help(cq: CallbackQuery):
    if not is_admin(cq.from_user.id):
        return await cq.answer("Access denied", show_alert=True)
    section = cq.data.split(":", 1)[1]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Admin Panel", callback_data="admin_refresh")]
    ])
    await cq.message.edit_text(
        f"<h2>Admin Tools</h2><p>{ADMIN_HELP.get(section, 'Unknown section')}</p>",
        reply_markup=kb,
    )
    await cq.answer()


# ================= Admin Broadcast (Forward Version - Aiogram Fix) =================
@dp.message(Command("broadcast"))
async def cmd_broadcast(msg: Message):
    if not is_admin(msg.from_user.id):
        return await msg.answer("❌ Not authorized.")

    if not msg.reply_to_message:
        return await msg.answer("⚠️ Reply to the message you want to broadcast with /broadcast.")

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

    await msg.answer(f"✅ Broadcast completed!\n\nSent: {sent_count}\nFailed: {failed_count}")
    

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
# ===== Bot Runner =====
async def main():
    from aiogram.types import BotCommand, BotCommandScopeChat

    print("TGA Store | API started.")
    await bot.set_my_commands([
        BotCommand(command="start", description="Open TGA Store | API"),
        BotCommand(command="balance", description="View wallet balance"),
        BotCommand(command="changepassword", description="Change website password"),
    ])
    for admin_id in ADMIN_IDS:
        try:
            await bot.set_my_commands([
                BotCommand(command="admin", description="Open admin control center"),
                BotCommand(command="start", description="Open user menu"),
                BotCommand(command="balance", description="View wallet balance"),
                BotCommand(command="manageserver", description="Manage API servers"),
                BotCommand(command="paymentsettings", description="Manage payments"),
                BotCommand(command="smmadmin", description="Manage SMM services"),
                BotCommand(command="managestore", description="Manage store"),
                BotCommand(command="broadcast", description="Broadcast replied message"),
            ], scope=BotCommandScopeChat(chat_id=admin_id))
        except Exception as exc:
            print(f"Could not set admin commands for {admin_id}: {exc}")
    start_auto_sweeper()  # launches the UPI/FamPay background auto-verify loop
    asyncio.create_task(smm_background_task())  # launches SMM order auto-status-checker
    
    # Yahan se humne smart_price_updater wali lines hata di hain!
    
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())

if __name__ == "__main__":
    import traceback
    try:
        asyncio.run(main())
    except Exception:
        print("❌ BOT CRASHED WITH ERROR:", flush=True)
        traceback.print_exc()
        raise
