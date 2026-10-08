import asyncio
import base64
import datetime
import hashlib
import math
import secrets

from aiogram import F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, CopyTextButton, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from html import escape

# OxaPay remains the existing all-coins automatic payment method.
from oxapay import check_invoice, create_invoice

# INR gateways and crypto verification helpers.
from auto_pay import (
    create_upi_invoice,
    verify_cwallet_payment,
    verify_binance_order,
    verify_fampay_invoice,
    verify_upi_invoice,
)
from payment_settings import (
    SUPPORTED_USDT_NETWORKS,
    get_settings,
    get_usdt_networks,
    remove_usdt_network,
    set_cwallet_manual_id,
    set_inr_gateway,
    set_usdt_network,
    toggle_binance,
    toggle_cwallet_manual,
    toggle_crypto,
    toggle_usdt,
)
from config import BINANCE_PAY_ID, BINANCE_QR_URL


# CONFIG
USDT_TO_INR = 90
MIN_USDT = 0.1
MIN_INR_DEPOSIT = 10
UPI_INVOICE_EXPIRY_MIN = 60
AUTO_SWEEP_INTERVAL_SEC = 60
HISTORY_LIMIT = 10

# Fixed QR supplied by the owner. This PNG snapshot is embedded so Telegram
# can display it (Telegram does not render the source SVG as a photo). It is
# reused for every FamPay payment; no per-payment QR or link button is generated.
FAMPAY_QR_PNG_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAWgAAAFoCAAAAABfjj4JAAAEDklEQVR42u3aQXbDIAwFQN//0u0JUqMv4aRhtEufi2HIIh90/ahH"
    "6kIAGrQCDRq0Ag1agQYNWoEGrUCDBq1Ag1agQYNWoEEr0KBBK9CgFWjQoNXboa+wXv3/3bjpfF49t/p5ar2gQYMGDRo0aNCgQf81"
    "oerzd+NUwe42MN3QqfWCBg0aNGjQoEGDBt1ZaBe6GjC6MFPrBQ0aNGjQoEGDBg16IjBMwaQbOzVf0KBBgwYNGjRo0KDfCZ2CrAaO"
    "9DnQoEGDBg0aNGjQoD+hgSZdUPfvq/ApHGjQoEGDBg0aNGjQKwNVazrAPP05XS9o0KBBgwYNGjRo0BM1DbF6ID8VSMYcQIMGDRo0"
    "aNCgQR8FvdqIMgWwC7DaUJPOAzRo0KBBgwYNGjTozgKqE51qMO8e8KcbDxo0aNCgQYMGDRp0JVjEP+DDcdLgUQ0m3fWBBg0aNGjQ"
    "oEGDBl0JFNUFVw/g0+d2BTHQoEGDBg0aNGjQoJMXdoNIN+BUgbvzid8DGjRo0KBBgwYN+kjo6gVAeoGQjps2zOz6YoAGDRo0aNCg"
    "QYM+G7r64qlAkh7gVy8MuoELNGjQoEGDBg0aNOhKIOhOLG2g2X3wnzqABg0aNGjQoEGDBp2ApAf86XvTA/5qUErHBQ0aNGjQoEGD"
    "Bg16MiBUJz7VUJNeMExdAIAGDRo0aNCgQYM+C3rqgDw9ON918L86XvcLBRo0aNCgQYMGDfps6Kkf8N2AUIWeunAADRo0aNCgQYMG"
    "DTo5+E8bWtIAMfV8Ok63MQc0aNCgQYMGDRr02dB3oNUNmG4krwab7kbFFxigQYMGDRo0aNCgj4SuTqDbgDK1MVMN7+mBP2jQoEGD"
    "Bg0aNGjQHYBdAaV7gD+9btCgQYMGDRo0aNCgdy6g2iiTzutug7sXAWOBBTRo0KBBgwYNGvRXQ1cntgqWXjh0LyaqGxLPGzRo0KBB"
    "gwYNGvRR0FNw1Y2YarTpflHSiwLQoEGDBg0aNGjQoCfA04VNgaUbNQUPGjRo0KBBgwYN+izo7oKnLgzShVfH39bIDho0aNCgQYMG"
    "Dfoo6KlAsxoMdgeQ7kbHFxSgQYMGDRo0aNCgj4K+wlqFvHtuKnisXlykkKBBgwYNGjRo0KBBVw7O0+enGmmqwebjAwto0KBBgwYN"
    "GjTor4SeDihTUN2AkgawbYEFNGjQoEGDBg0a9NHQqwFld2BanXf3ogA0aNCgQYMGDRo06Cehu4011cCRBp9/H1hAgwYNGjRo0KBB"
    "vwW6+3y6gHSjqoFjumEHNGjQoEGDBg0a9NnQ6QTig/IwCG0PHmnjD2jQoEGDBg0aNOijoNVMgQYNWoEGDRoBaNAKNGjQCjRoBRo0"
    "aAUatAINGrQCDVqBBg1agQatQIMGrbbXL5RXg9MjxivYAAAAAElFTkSuQmCC"
)


class RechargeState(StatesGroup):
    choose_method = State()
    upi_amount = State()
    crypto_amount = State()
    binance_orderid = State()
    fampay_reference = State()

    # Cwallet automatic USDT flow
    cwallet_usdt_txid = State()

    # Cwallet manual flow
    cwallet_manual_amount = State()
    cwallet_manual_proof = State()

    # Manual UPI: user amount/proof and admin configuration
    manual_upi_amount = State()
    manual_upi_proof = State()
    admin_manual_upi_id = State()
    admin_manual_upi_name = State()
    admin_manual_upi_qr = State()

    # Admin payment settings flows
    admin_usdt_network_name = State()
    admin_usdt_address = State()
    admin_cwallet_id = State()


def register_recharge_handlers(dp, bot, users_col, txns_col, crypto_col, settings_col, ADMIN_IDS):
    """Register recharge handlers and return the automatic sweeper starter.

    ``txns_col`` contains INR gateway, manual UPI, and Cwallet transactions.
    ``crypto_col`` is kept for the pre-existing OxaPay flow so
    old invoices and history continue to work exactly as before.
    """

    def is_admin(uid: int) -> bool:
        return uid in ADMIN_IDS

    # Avoid crediting the same FamPay transaction/UTR twice in this bot.
    # The API's already_verified flag is also checked before any credit.
    for index_name, index_field in (
        ("uniq_fampay_transaction", "claimed_txn_normalized"),
        ("uniq_fampay_utr", "claimed_utr_normalized"),
    ):
        try:
            txns_col.create_index(index_field, unique=True, sparse=True, name=index_name)
        except Exception as exc:
            print(f"[fampay] Could not create {index_name} index: {exc}")

    def get_balance(user_id: int) -> float:
        user = users_col.find_one({"_id": user_id})
        return user.get("balance", 0) if user else 0

    def _fmt_amount(value, decimals: int = 8) -> str:
        """Format a number without noisy trailing zeroes for Telegram text."""
        try:
            number = float(value)
        except (TypeError, ValueError):
            return "0"
        formatted = f"{number:.{decimals}f}".rstrip("0").rstrip(".")
        return formatted or "0"

    def _fmt_dt(value) -> str:
        """Format a datetime for Telegram text (IST-safe, just readable)."""
        if not value:
            return "N/A"
        if isinstance(value, datetime.datetime):
            return value.strftime("%d %b %Y, %I:%M %p")
        return str(value)

    def _parse_positive_amount(value):
        try:
            amount = float(str(value).strip())
        except (TypeError, ValueError):
            return None
        if not math.isfinite(amount) or amount <= 0:
            return None
        return amount

    def _back_kb(callback_data: str = "recharge", text: str = "🔙 Back"):
        kb = InlineKeyboardBuilder()
        kb.button(text=text, callback_data=callback_data)
        kb.adjust(1)
        return kb

    def _cancel_kb(callback_data: str = "recharge"):
        """Small, consistent back keyboard used by every user prompt."""
        return _back_kb(callback_data)

    async def _delete_message(message):
        try:
            await message.delete()
        except Exception:
            pass

    async def _delete_saved_prompt(user_id: int, data: dict):
        prompt_id = data.get("last_msg")
        if prompt_id:
            try:
                await bot.delete_message(user_id, prompt_id)
            except Exception:
                pass

    def _network_token(network: str) -> str:
        """Short stable callback token; network labels need not be URL-safe."""
        return hashlib.sha256(str(network).encode("utf-8")).hexdigest()[:12]

    def _known_networks(settings_col):
        configured = get_usdt_networks(settings_col)
        names = []
        for network in SUPPORTED_USDT_NETWORKS:
            if network not in names:
                names.append(network)
        for network in configured:
            if network not in names:
                names.append(network)
        return names

    def _resolve_network(settings_col, token: str, include_unconfigured: bool = False):
        names = _known_networks(settings_col) if include_unconfigured else list(get_usdt_networks(settings_col))
        for network in names:
            if _network_token(network) == token:
                return network
        return None

    async def _send_admin_message(text: str):
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_message(admin_id, text, parse_mode="HTML")
            except Exception as exc:
                print(f"Error sending admin notification to {admin_id}: {exc}")

    def _remember_user_profile(user_id: int, from_user=None):
        """Cache the user's name/username on their doc so later admin logs
        (auto-sweep, manual review, etc.) can show full details even when no
        live Telegram user object is available."""
        if from_user is None:
            return
        first = getattr(from_user, "first_name", "") or ""
        last = getattr(from_user, "last_name", "") or ""
        name = f"{first} {last}".strip() or None
        username = getattr(from_user, "username", None) or None
        if not name and not username:
            return
        try:
            update = {"$set": {}}
            if name:
                update["$set"]["name"] = name
                update["$set"]["full_name"] = name
            if username:
                update["$set"]["username"] = str(username).lstrip("@")
            users_col.update_one({"_id": user_id}, update)
        except Exception:
            pass

    def _user_details_text(user_id: int, from_user=None) -> str:
        """Full user details block (name / username / id) for admin logs.

        Prefers the live Telegram profile when available, otherwise reads the
        stored user doc so every admin notification can show them."""
        name = None
        username = None
        doc = users_col.find_one({"_id": user_id}) or {}
        if doc:
            name = (
                doc.get("name")
                or doc.get("full_name")
                or (
                    f"{doc.get('first_name', '') or ''} {doc.get('last_name', '') or ''}".strip()
                    or None
                )
            )
            username = doc.get("username") or None
            if username:
                username = str(username).lstrip("@")
        if from_user is not None:
            first = getattr(from_user, "first_name", "") or ""
            last = getattr(from_user, "last_name", "") or ""
            live_name = f"{first} {last}".strip() or None
            live_username = getattr(from_user, "username", None) or None
            if live_name:
                name = live_name
            if live_username:
                username = str(live_username).lstrip("@")
        lines = [f"👤 <b>Name:</b> {escape(name or 'N/A')}"]
        lines.append(
            f"🔗 <b>Username:</b> @{escape(username)}"
            if username
            else "🔗 <b>Username:</b> <i>None</i>"
        )
        lines.append(f"🆔 <b>User ID:</b> <code>{user_id}</code>")
        return "\n".join(lines)

    # ==============================
    # TERMS & CONDITIONS MENU
    # ==============================
    async def show_recharge_terms(target, edit=False):
        text = (
            "⚠️ <b>Recharge Terms &amp; Conditions</b>\n\n"
            "<blockquote>"
            "• <b>All recharges are 100% NON-REFUNDABLE.</b>\n"
            "• Once the amount is added to your bot wallet, it <b>cannot be withdrawn</b> or transferred back to your bank or crypto wallet under any circumstances.\n"
            "• <u>Never send a payment on an incorrect network.</u>\n"
            "• <b>Do not ask for a refund if any item/service is out of stock.</b> Please try another country/operator or wait for restock."
            "</blockquote>\n\n"
            "<i>By clicking <b>✅ I Accept</b>, you agree to these terms.</i>\n\n"
            "🎧 <b>Note:</b> If you face any issue during recharge, please contact our <u>Support</u> team."
        )
        kb = InlineKeyboardBuilder()
        kb.button(text="✅ I Accept", callback_data="recharge_accept_terms")
        kb.button(text="❌ Cancel", callback_data="back_main")
        kb.adjust(1)

        if edit:
            try:
                await target.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())
            except Exception:
                await _delete_message(target)
                await target.answer(text, parse_mode="HTML", reply_markup=kb.as_markup())
        else:
            await target.answer(text, parse_mode="HTML", reply_markup=kb.as_markup())

    # ==============================
    # MAIN RECHARGE MENU
    # ==============================
    async def show_recharge_menu(target, state: FSMContext, edit=False):
        await state.clear()
        settings = get_settings(settings_col)

        kb = InlineKeyboardBuilder()
        available = 0

        if settings.get("active_inr_gateway"):
            kb.button(text="💳 UPI (Auto)", callback_data="recharge_upi_auto")
            available += 1
        if settings.get("manual_upi_enabled", False):
            kb.button(text="🏦 UPI (Manual)", callback_data="recharge_upi_manual")
            available += 1
        if settings.get("binance_enabled", True):
            kb.button(text="🅱️ Binance Pay (Auto)", callback_data="recharge_binance")
            available += 1
        if settings.get("crypto_enabled", True):
            kb.button(text="🪙 Crypto Deposits (Auto)", callback_data="recharge_crypto")
            available += 1
        if settings.get("cwallet_manual_enabled", True):
            # Cwallet (Manual) sits directly under Crypto Deposits and above
            # Recharge History in the main menu, as requested.
            kb.button(text="🧾 Cwallet (Manual)", callback_data="recharge_cwallet_manual")
            available += 1
        kb.button(text="📜 Recharge History", callback_data="recharge_history")
        kb.button(text="🔙 Back", callback_data="back_main")
        kb.adjust(1)

        if available == 0:
            text = (
                "💰 <b>Add Balance</b>\n\n"
                "<blockquote>⚠️ All payment methods are currently disabled. "
                "Please contact <u>Support</u>.</blockquote>"
            )
        else:
            text = (
                "💰 <b>Add Balance</b>\n\n"
                "<blockquote><i>Choose a secure payment method below.</i>\n"
                "Your payment details stay private.</blockquote>"
            )

        if edit:
            try:
                await target.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())
            except Exception:
                await _delete_message(target)
                await target.answer(text, parse_mode="HTML", reply_markup=kb.as_markup())
        else:
            await target.answer(text, parse_mode="HTML", reply_markup=kb.as_markup())
        await state.set_state(RechargeState.choose_method)

    # ==============================
    # TRIGGERS / TERMS
    # ==============================
    @dp.callback_query(F.data == "recharge")
    async def recharge_btn(cq: CallbackQuery, state: FSMContext):
        user_id = cq.from_user.id
        _remember_user_profile(user_id, cq.from_user)
        user = users_col.find_one({"_id": user_id}) or {}
        if user.get("recharge_terms_accepted"):
            await show_recharge_menu(cq.message, state, edit=True)
        else:
            await show_recharge_terms(cq.message, edit=True)
        await cq.answer()

    @dp.message(Command("recharge"))
    async def recharge_cmd(message: Message, state: FSMContext):
        user_id = message.from_user.id
        _remember_user_profile(user_id, message.from_user)
        user = users_col.find_one({"_id": user_id}) or {}
        if user.get("recharge_terms_accepted"):
            await show_recharge_menu(message, state, edit=False)
        else:
            await show_recharge_terms(message, edit=False)

    @dp.callback_query(F.data == "recharge_accept_terms")
    async def recharge_accept_terms(cq: CallbackQuery, state: FSMContext):
        users_col.update_one(
            {"_id": cq.from_user.id},
            {"$set": {"recharge_terms_accepted": True}},
            upsert=True,
        )
        await show_recharge_menu(cq.message, state, edit=True)
        await cq.answer("Terms accepted")

    # ==============================
    # CRYPTO METHOD SUBMENU
    # ==============================
    async def show_crypto_menu(target, state: FSMContext, edit=False):
        await state.clear()
        settings = get_settings(settings_col)
        kb = InlineKeyboardBuilder()
        available = 0

        # Deliberately call the automatic Cwallet method simply "USDT" in the
        # user UI, as requested.  The provider name is not exposed here.
        if settings.get("usdt_enabled", True):
            kb.button(text="💵 USDT", callback_data="recharge_usdt")
            available += 1
        if settings.get("crypto_enabled", True):
            kb.button(text="🌐 Other Crypto Coins", callback_data="recharge_oxapay")
            available += 1
        kb.button(text="🔙 Back", callback_data="recharge")
        kb.adjust(1)

        if available:
            text = (
                "🪙 <b>Crypto Deposits</b>\n\n"
                "<blockquote><i>Select the coin/payment route you want to use.</i>\n"
                "USDT verification is automatic.</blockquote>"
            )
        else:
            text = (
                "🪙 <b>Crypto Deposits</b>\n\n"
                "<blockquote>⚠️ Crypto deposits are currently unavailable. "
                "Please contact <u>Support</u>.</blockquote>"
            )

        if edit:
            try:
                await target.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())
            except Exception:
                await _delete_message(target)
                await target.answer(text, parse_mode="HTML", reply_markup=kb.as_markup())
        else:
            await target.answer(text, parse_mode="HTML", reply_markup=kb.as_markup())
        await state.set_state(RechargeState.choose_method)

    @dp.callback_query(F.data == "recharge_crypto")
    async def recharge_crypto(cq: CallbackQuery, state: FSMContext):
        settings = get_settings(settings_col)
        if not settings.get("crypto_enabled", True):
            await cq.answer("⚠️ Crypto payment is currently unavailable.", show_alert=True)
            return
        await show_crypto_menu(cq.message, state, edit=True)
        await cq.answer()

    # ==============================
    # UPI invoice flow + static FamPay QR / reference verification
    # ==============================
    @dp.callback_query(F.data == "recharge_upi_auto")
    async def recharge_upi_auto(cq: CallbackQuery, state: FSMContext):
        settings = get_settings(settings_col)
        gateway = settings.get("active_inr_gateway")

        if gateway == "fampay":
            await _delete_message(cq.message)
            await state.clear()

            # This is only an internal tracking ID. FamPay is verified using
            # the UTR/transaction ID submitted by the user, not an order ID.
            payment_id = f"fampay_{cq.from_user.id}_{secrets.token_hex(8)}"
            now = datetime.datetime.utcnow()
            txns_col.insert_one({
                "_id": payment_id,
                "user_id": cq.from_user.id,
                "method": "fampay",
                "amount": None,
                "status": "pending",
                "created_at": now,
            })

            kb = InlineKeyboardBuilder()
            kb.button(
                text="✅ I Paid — Verify UTR / Transaction ID",
                callback_data=f"verify_fampay:{payment_id}",
            )
            kb.button(text="🔙 Back", callback_data="recharge")
            kb.adjust(1)
            caption = (
                "📲 <b>FamPay / UPI (Auto)</b>\n\n"
                "👤 Name: <b>Swati Kumari</b>\n"
                "🆔 UPI ID: <code>swatireceiver@fam</code>\n\n"
                "<blockquote>After paying, tap <b>Verify UTR / Transaction ID</b> and "
                "send the UTR or FamPay transaction ID from your payment receipt. "
                "No amount or screenshot is needed. The verified amount will be credited "
                f"automatically (minimum ₹{MIN_INR_DEPOSIT}).</blockquote>"
            )
            qr_photo = BufferedInputFile(
                base64.b64decode(FAMPAY_QR_PNG_BASE64),
                filename="fampay-static-qr.png",
            )
            await cq.message.answer_photo(
                qr_photo, caption=caption, parse_mode="HTML", reply_markup=kb.as_markup()
            )

        elif gateway == "upi":
            await _delete_message(cq.message)
            msg = await cq.message.answer(
                "💰 <b>Enter deposit amount</b>\n\n"
                "<i>Minimum deposit: ₹10</i>",
                parse_mode="HTML",
                reply_markup=_cancel_kb().as_markup(),
            )
            await state.update_data(last_msg=msg.message_id)
            await state.set_state(RechargeState.upi_amount)
        else:
            await cq.answer("⚠️ UPI payment is currently unavailable.", show_alert=True)
            return
        await cq.answer()

    @dp.callback_query(F.data == "recharge_upi_manual")
    async def recharge_upi_manual(cq: CallbackQuery, state: FSMContext):
        settings = get_settings(settings_col)
        if not settings.get("manual_upi_enabled", False):
            await cq.answer("⚠️ Manual UPI is currently unavailable.", show_alert=True)
            return
        if not all((
            settings.get("manual_upi_id"),
            settings.get("manual_upi_name"),
            settings.get("manual_upi_qr_file_id"),
        )):
            await cq.answer("⚠️ Manual UPI is not fully configured yet. Contact Support.", show_alert=True)
            return

        await _delete_message(cq.message)
        await state.clear()
        prompt = await cq.message.answer(
            "💰 <b>Manual UPI — Enter Amount</b>\n\n"
            f"Minimum deposit: <b>₹{MIN_INR_DEPOSIT}</b>. After payment, send a screenshot "
            "for admin approval.",
            parse_mode="HTML",
            reply_markup=_cancel_kb().as_markup(),
        )
        await state.update_data(last_msg=prompt.message_id)
        await state.set_state(RechargeState.manual_upi_amount)
        await cq.answer()

    @dp.message(StateFilter(RechargeState.manual_upi_amount))
    async def manual_upi_amount_entered(message: Message, state: FSMContext):
        data = await state.get_data()
        amount = _parse_positive_amount(message.text)
        await _delete_message(message)
        await _delete_saved_prompt(message.chat.id, data)

        if amount is None or amount < MIN_INR_DEPOSIT:
            prompt = await message.answer(
                f"❌ <b>Invalid amount.</b> Enter ₹{MIN_INR_DEPOSIT} or more.",
                parse_mode="HTML",
                reply_markup=_cancel_kb().as_markup(),
            )
            await state.update_data(last_msg=prompt.message_id)
            return

        settings = get_settings(settings_col)
        upi_id = str(settings.get("manual_upi_id") or "").strip()
        payee_name = str(settings.get("manual_upi_name") or "").strip()
        qr_file_id = settings.get("manual_upi_qr_file_id")
        if (
            not settings.get("manual_upi_enabled", False)
            or not upi_id or not payee_name or not qr_file_id
        ):
            await state.clear()
            await message.answer(
                "⚠️ Manual UPI is not available right now. Please contact Support.",
                reply_markup=_cancel_kb().as_markup(),
            )
            return

        now = datetime.datetime.utcnow()
        request_id = (
            f"upm_{message.from_user.id}_{int(now.timestamp())}_{secrets.token_hex(3)}"
        )
        txns_col.insert_one({
            "_id": request_id,
            "user_id": message.from_user.id,
            "method": "upi_manual",
            "upi_id": upi_id,
            "payee_name": payee_name,
            "amount": amount,
            "amount_inr": amount,
            "currency": "INR",
            "status": "pending",
            "created_at": now,
        })

        kb = InlineKeyboardBuilder()
        kb.button(text="📋 Copy UPI ID", copy_text=CopyTextButton(text=upi_id))
        kb.button(text="✅ I Have Paid — Send Screenshot", callback_data=f"manual_upi_paid:{request_id}")
        kb.button(text="🔙 Back", callback_data="recharge")
        kb.adjust(1)
        caption = (
            "🏦 <b>UPI (Manual)</b>\n\n"
            f"👤 <b>Name:</b> {escape(payee_name)}\n"
            f"🆔 <b>UPI ID:</b> <code>{escape(upi_id)}</code>\n"
            f"💰 <b>Amount to pay:</b> ₹{_fmt_amount(amount, 2)}\n\n"
            "<blockquote>Pay the exact amount using this QR or UPI ID. Then tap "
            "<b>I Have Paid</b> and send the payment screenshot. An admin will check "
            "it and approve or reject the request.</blockquote>"
        )
        try:
            await message.answer_photo(
                qr_file_id, caption=caption, parse_mode="HTML", reply_markup=kb.as_markup()
            )
        except Exception as exc:
            print(f"[manual_upi] Could not send configured QR: {exc}")
            await message.answer(caption, parse_mode="HTML", reply_markup=kb.as_markup())
        await state.clear()

    @dp.callback_query(F.data.startswith("manual_upi_paid:"))
    async def manual_upi_paid(cq: CallbackQuery, state: FSMContext):
        request_id = cq.data.split(":", 1)[1]
        tx = txns_col.find_one({
            "_id": request_id,
            "user_id": cq.from_user.id,
            "method": "upi_manual",
            "status": "pending",
        })
        if not tx:
            await cq.answer("Manual UPI request not found or already processed.", show_alert=True)
            return

        await state.clear()
        prompt = await cq.message.answer(
            "📸 <b>Send payment screenshot</b>\n\n"
            "Upload a clear screenshot as a photo or image. No UTR is required; "
            "the admin will review the screenshot and the amount you entered.",
            parse_mode="HTML",
            reply_markup=_cancel_kb().as_markup(),
        )
        await state.update_data(manual_upi_request_id=request_id, last_msg=prompt.message_id)
        await state.set_state(RechargeState.manual_upi_proof)
        await cq.answer()

    @dp.message(StateFilter(RechargeState.manual_upi_proof))
    async def manual_upi_proof_received(message: Message, state: FSMContext):
        data = await state.get_data()
        file_id = None
        file_type = None
        if getattr(message, "photo", None):
            file_id = message.photo[-1].file_id
            file_type = "photo"
        elif getattr(message, "document", None) and str(
            getattr(message.document, "mime_type", "")
        ).startswith("image/"):
            file_id = message.document.file_id
            file_type = "document"

        if not file_id:
            await _delete_message(message)
            prompt = await message.answer(
                "❌ Please send the payment screenshot as a photo or image file.",
                reply_markup=_cancel_kb().as_markup(),
            )
            await state.update_data(last_msg=prompt.message_id)
            return

        await _delete_message(message)
        await _delete_saved_prompt(message.chat.id, data)
        request_id = data.get("manual_upi_request_id")
        tx = txns_col.find_one({
            "_id": request_id,
            "user_id": message.from_user.id,
            "method": "upi_manual",
            "status": "pending",
        }) if request_id else None
        if not tx:
            await state.clear()
            await message.answer(
                "❌ This Manual UPI request was not found or is already processed.",
                reply_markup=_cancel_kb().as_markup(),
            )
            return

        now = datetime.datetime.utcnow()
        result = txns_col.update_one(
            {"_id": request_id, "method": "upi_manual", "status": "pending"},
            {"$set": {
                "proof_file_id": file_id,
                "proof_file_type": file_type,
                "proof_submitted_at": now,
            }},
        )
        if getattr(result, "modified_count", 1) == 0:
            await state.clear()
            await message.answer("This Manual UPI request was already processed.")
            return

        tx["proof_file_id"] = file_id
        tx["proof_file_type"] = file_type
        tx["proof_submitted_at"] = now
        user_id = message.from_user.id
        _remember_user_profile(user_id, message.from_user)
        admin_kb = InlineKeyboardBuilder()
        admin_kb.button(text="✅ Accept", callback_data=f"manual_upi_approve:{request_id}")
        admin_kb.button(text="❌ Reject", callback_data=f"manual_upi_reject:{request_id}")
        admin_kb.adjust(2)
        current_balance = get_balance(user_id)
        admin_caption = (
            "🏦 <b>Manual UPI — Review Required</b>\n\n"
            f"{_user_details_text(user_id, message.from_user)}\n"
            f"👤 <b>Payee:</b> {escape(str(tx.get('payee_name') or ''))}\n"
            f"🆔 <b>UPI ID:</b> <code>{escape(str(tx.get('upi_id') or ''))}</code>\n"
            f"💰 <b>Amount requested:</b> ₹{_fmt_amount(tx.get('amount_inr') or tx.get('amount'), 2)}\n"
            f"🏦 <b>Current balance:</b> ₹{_fmt_amount(current_balance, 2)}\n"
            f"🔖 <b>Request ID:</b> <code>{escape(request_id)}</code>\n"
            f"🕐 <b>Submitted:</b> <code>{_fmt_dt(now)}</code>\n\n"
            "<i>Verify the screenshot and amount, then Accept or Reject.</i>"
        )
        sent_to_admin = False
        for admin_id in ADMIN_IDS:
            try:
                if file_type == "document":
                    await bot.send_document(
                        admin_id, file_id, caption=admin_caption, parse_mode="HTML",
                        reply_markup=admin_kb.as_markup(),
                    )
                else:
                    await bot.send_photo(
                        admin_id, file_id, caption=admin_caption, parse_mode="HTML",
                        reply_markup=admin_kb.as_markup(),
                    )
                sent_to_admin = True
            except Exception as exc:
                print(f"Error sending Manual UPI proof to admin {admin_id}: {exc}")
                try:
                    await bot.send_message(
                        admin_id,
                        admin_caption + "\n\n⚠️ Screenshot could not be forwarded automatically.",
                        parse_mode="HTML",
                        reply_markup=admin_kb.as_markup(),
                    )
                    sent_to_admin = True
                except Exception:
                    pass

        await message.answer(
            "⏳ <b>Manual UPI proof submitted</b>\n\n"
            f"💰 Amount: <b>₹{_fmt_amount(tx.get('amount_inr') or tx.get('amount'), 2)}</b>\n\n"
            + (
                "<blockquote>An admin will review your screenshot and notify you after approval.</blockquote>"
                if sent_to_admin
                else "<blockquote>Your request is saved, but no admin notification was delivered. Please contact Support.</blockquote>"
            ),
            parse_mode="HTML",
            reply_markup=_cancel_kb().as_markup(),
        )
        await state.clear()

    @dp.message(StateFilter(RechargeState.upi_amount))
    async def upi_amount_entered(message: Message, state: FSMContext):
        data = await state.get_data()
        await _delete_message(message)
        await _delete_saved_prompt(message.chat.id, data)

        amount = _parse_positive_amount(message.text)
        if amount is None or amount < MIN_INR_DEPOSIT:
            msg = await message.answer(
                "❌ <b>Invalid amount.</b>\n\nPlease enter an amount of <u>₹10 or more</u>.",
                parse_mode="HTML",
                reply_markup=_cancel_kb().as_markup(),
            )
            await state.update_data(last_msg=msg.message_id)
            return

        wait_msg = await message.answer("⌛ <i>Generating QR…</i>", parse_mode="HTML")
        res = create_upi_invoice(amount)
        if not res["success"]:
            await wait_msg.edit_text(
                f"❌ <b>Error:</b> {escape(str(res.get('error', 'Unable to create QR')))}",
                parse_mode="HTML",
                reply_markup=_cancel_kb().as_markup(),
            )
            await state.clear()
            return

        order_id = str(res["order_id"])
        now = datetime.datetime.utcnow()
        txns_col.insert_one({
            "_id": order_id,
            "user_id": message.from_user.id,
            "method": "upi",
            "amount": amount,
            "status": "pending",
            "created_at": now,
            "expires_at": now + datetime.timedelta(minutes=UPI_INVOICE_EXPIRY_MIN),
        })

        kb = InlineKeyboardBuilder()
        kb.button(text="✅ Verify Payment", callback_data=f"verify_upi:{order_id}")
        kb.button(text="🔙 Back", callback_data="recharge")
        kb.adjust(1)
        caption = (
            "📲 <b>UPI (Auto)</b>\n\n"
            f"💵 Amount: <b>₹{_fmt_amount(amount, 2)}</b>\n"
            f"🆔 Order ID: <tg-spoiler><code>{escape(order_id)}</code></tg-spoiler>\n\n"
            "<blockquote>Scan with any UPI app. We auto-check every minute, or tap "
            "<b>Verify Payment</b> manually.</blockquote>"
        )
        await wait_msg.delete()
        await message.answer_photo(
            res["qr_url"], caption=caption, parse_mode="HTML", reply_markup=kb.as_markup()
        )
        await state.clear()

    @dp.callback_query(F.data.startswith("verify_upi:"))
    async def verify_upi_cb(cq: CallbackQuery):
        order_id = cq.data.split(":", 1)[1]
        tx = txns_col.find_one({"_id": order_id})
        if not tx:
            await cq.answer("Invoice not found.", show_alert=True)
            return
        if tx.get("status") == "paid":
            await cq.answer("✅ Already credited!", show_alert=True)
            return

        if verify_upi_invoice(order_id):
            await _credit_and_notify(cq, tx, method_label="UPI")
        else:
            await cq.answer("⏳ Payment not received yet. Try again in a moment.", show_alert=True)

    @dp.callback_query(F.data.startswith("verify_fampay:"))
    async def verify_fampay_cb(cq: CallbackQuery, state: FSMContext):
        payment_id = cq.data.split(":", 1)[1]
        tx = txns_col.find_one({
            "_id": payment_id,
            "user_id": cq.from_user.id,
            "method": "fampay",
        })
        if not tx:
            await cq.answer("Payment request not found.", show_alert=True)
            return
        if tx.get("status") == "paid":
            await cq.answer("✅ Already credited!", show_alert=True)
            return
        if tx.get("status") != "pending":
            await cq.answer("This payment request is no longer pending.", show_alert=True)
            return

        await state.clear()
        prompt = await cq.message.answer(
            "🧾 <b>Send your UTR or FamPay Transaction ID</b>\n\n"
            "Paste the reference exactly as shown in your payment receipt. "
            "No screenshot or amount is needed.",
            parse_mode="HTML",
            reply_markup=_cancel_kb().as_markup(),
        )
        await state.update_data(fampay_tx_id=payment_id, last_msg=prompt.message_id)
        await state.set_state(RechargeState.fampay_reference)
        await cq.answer()

    @dp.message(StateFilter(RechargeState.fampay_reference))
    async def fampay_reference_entered(message: Message, state: FSMContext):
        data = await state.get_data()
        reference = (message.text or "").strip()
        await _delete_message(message)
        await _delete_saved_prompt(message.chat.id, data)

        if not reference or len(reference) > 128 or any(ch.isspace() for ch in reference):
            prompt = await message.answer(
                "❌ <b>Invalid reference.</b> Send the UTR or transaction ID without spaces.",
                parse_mode="HTML",
                reply_markup=_cancel_kb().as_markup(),
            )
            await state.update_data(last_msg=prompt.message_id)
            return

        payment_id = data.get("fampay_tx_id")
        tx = txns_col.find_one({
            "_id": payment_id,
            "user_id": message.from_user.id,
            "method": "fampay",
        }) if payment_id else None
        if not tx:
            await state.clear()
            await message.answer(
                "❌ Payment session not found. Please start FamPay recharge again.",
                reply_markup=_cancel_kb().as_markup(),
            )
            return
        if tx.get("status") == "paid":
            await state.clear()
            await message.answer("✅ This payment has already been credited.")
            return
        if tx.get("status") != "pending":
            await state.clear()
            await message.answer(
                "This payment request is no longer pending.",
                reply_markup=_cancel_kb().as_markup(),
            )
            return

        normalized_input = reference.casefold()
        existing = txns_col.find_one({
            "method": "fampay",
            "$or": [
                {"claimed_txn_normalized": normalized_input},
                {"claimed_utr_normalized": normalized_input},
            ],
        })
        if existing and existing.get("_id") != payment_id:
            await state.clear()
            await message.answer(
                "⚠️ This FamPay payment reference has already been claimed. "
                "The same payment cannot be credited twice.",
                reply_markup=_cancel_kb().as_markup(),
            )
            return

        wait_msg = await message.answer("⌛ <i>Verifying payment…</i>", parse_mode="HTML")
        res = verify_fampay_invoice(reference)

        if res.get("already_verified") is True:
            txns_col.update_one(
                {"_id": payment_id, "status": "pending"},
                {"$set": {"status": "duplicate", "verification_reference": reference}},
            )
            await state.clear()
            await wait_msg.edit_text(
                "⚠️ <b>This payment was already verified/claimed.</b>\n\n"
                "The same UTR or transaction cannot be credited again. If you believe "
                "this is a mistake, contact Support.",
                parse_mode="HTML",
                reply_markup=_cancel_kb().as_markup(),
            )
            return

        if not res.get("success") or res.get("received") is not True:
            status_code = int(res.get("status_code") or 0)
            if status_code == 401:
                await state.clear()
                result_text = (
                    "❌ <b>FamPay verification is not configured correctly.</b> "
                    "Please contact Support."
                )
            elif status_code == 400:
                result_text = (
                    "❌ <b>That UTR/transaction ID is invalid.</b> "
                    "Check it and send the correct reference again."
                )
            elif status_code >= 500:
                result_text = (
                    "⚠️ <b>Verification service is temporarily unavailable.</b> "
                    "You can send the same reference again in a little while."
                )
            elif res.get("received") is False:
                result_text = (
                    "⏳ <b>Payment not found yet.</b> Check the reference and wait for "
                    "the payment to appear, then send the UTR/transaction ID again."
                )
            else:
                result_text = (
                    "⏳ <b>Payment could not be verified yet.</b> "
                    "Please check the reference and try again shortly."
                )
            await wait_msg.edit_text(
                result_text,
                parse_mode="HTML",
                reply_markup=_cancel_kb().as_markup(),
            )
            if status_code != 401:
                await state.update_data(last_msg=wait_msg.message_id)
            return

        if str(res.get("currency") or "").upper() != "INR":
            await state.clear()
            await wait_msg.edit_text(
                "❌ <b>Payment currency could not be confirmed as INR.</b> "
                "No credit was added. Please contact Support.",
                parse_mode="HTML",
                reply_markup=_cancel_kb().as_markup(),
            )
            return

        # The static QR has no fixed amount; credit the exact amount reported by the API.
        amount = _parse_positive_amount(res.get("amount"))
        if amount is None:
            await state.clear()
            await wait_msg.edit_text(
                "❌ <b>Payment was found, but the verification service returned an invalid amount.</b> "
                "No credit was added. Please contact Support.",
                parse_mode="HTML",
                reply_markup=_cancel_kb().as_markup(),
            )
            return
        if amount < MIN_INR_DEPOSIT:
            txns_col.update_one(
                {"_id": payment_id, "status": "pending"},
                {"$set": {
                    "status": "rejected",
                    "rejection_reason": "below_minimum",
                    "verification_reference": reference,
                    "amount": amount,
                }},
            )
            await state.clear()
            await wait_msg.edit_text(
                f"⚠️ <b>Received amount is below the ₹{MIN_INR_DEPOSIT} minimum deposit.</b> "
                "No balance was added. Please contact Support if you need help.",
                parse_mode="HTML",
                reply_markup=_cancel_kb().as_markup(),
            )
            return

        transaction_id = str(res.get("transaction_id") or "").strip()
        utr = str(res.get("utr") or "").strip()
        returned_references = {
            value.casefold()
            for value in (transaction_id, utr)
            if value
        }
        if normalized_input not in returned_references:
            await state.clear()
            await wait_msg.edit_text(
                "❌ <b>The returned payment reference did not match.</b> "
                "No credit was added; please contact Support.",
                parse_mode="HTML",
                reply_markup=_cancel_kb().as_markup(),
            )
            return

        claim_values = list({
            value.casefold()
            for value in (reference, transaction_id, utr)
            if value
        })
        duplicate = txns_col.find_one({
            "method": "fampay",
            "$or": [
                {"claimed_txn_normalized": {"$in": claim_values}},
                {"claimed_utr_normalized": {"$in": claim_values}},
            ],
        })
        if duplicate and duplicate.get("_id") != payment_id:
            await state.clear()
            await wait_msg.edit_text(
                "⚠️ <b>This FamPay payment has already been claimed.</b> "
                "The same payment cannot be credited twice.",
                parse_mode="HTML",
                reply_markup=_cancel_kb().as_markup(),
            )
            return

        claimed_txn = transaction_id or utr or reference
        extra_set = {
            "amount": amount,
            "claimed_txn": claimed_txn,
            "claimed_txn_normalized": claimed_txn.casefold(),
            "verification_reference": reference,
            "currency": str(res.get("currency") or "INR").upper(),
        }
        if transaction_id:
            extra_set["transaction_id"] = transaction_id
        if utr:
            extra_set["utr"] = utr
            extra_set["claimed_utr_normalized"] = utr.casefold()
        if res.get("date"):
            extra_set["fampay_date"] = res.get("date")
        if res.get("sender"):
            extra_set["sender"] = res.get("sender")
        tx.update(extra_set)

        await _credit_and_notify(
            message,
            tx,
            method_label="FamPay",
            sender=res.get("sender"),
            extra_set=extra_set,
        )
        await _delete_message(wait_msg)
        await state.clear()

    # ==============================
    # BINANCE PAY (AUTO)
    # ==============================
    @dp.callback_query(F.data == "recharge_binance")
    async def recharge_binance(cq: CallbackQuery, state: FSMContext):
        settings = get_settings(settings_col)
        if not settings.get("binance_enabled", True):
            await cq.answer("⚠️ Binance Pay is currently unavailable.", show_alert=True)
            return

        await _delete_message(cq.message)
        kb = InlineKeyboardBuilder()
        kb.button(text="Copy Pay ID", copy_text=CopyTextButton(text=str(BINANCE_PAY_ID)))
        kb.button(text="✅ Verify Payment", callback_data="binance_verify_start")
        kb.button(text="🔙 Back", callback_data="recharge")
        kb.adjust(1)
        caption = (
            "🅱️ <b>Binance Pay (Auto)</b>\n\n"
            f"🆔 Pay ID: <code>{escape(str(BINANCE_PAY_ID))}</code>\n\n"
            "<blockquote>Send payment to this Pay ID via Binance. Once done, tap "
            "<b>Verify Payment</b> and send your Order ID or Transaction ID.</blockquote>"
        )
        if BINANCE_QR_URL:
            await cq.message.answer_photo(
                BINANCE_QR_URL, caption=caption, parse_mode="HTML", reply_markup=kb.as_markup()
            )
        else:
            await cq.message.answer(caption, parse_mode="HTML", reply_markup=kb.as_markup())
        await cq.answer()

    @dp.callback_query(F.data == "binance_verify_start")
    async def binance_verify_start(cq: CallbackQuery, state: FSMContext):
        msg = await cq.message.answer(
            "🆔 <b>Send your Binance Pay Order ID or Transaction ID</b>:",
            parse_mode="HTML",
            reply_markup=_cancel_kb().as_markup(),
        )
        await state.update_data(last_msg=msg.message_id)
        await state.set_state(RechargeState.binance_orderid)
        await cq.answer()

    @dp.message(StateFilter(RechargeState.binance_orderid))
    async def binance_orderid_entered(message: Message, state: FSMContext):
        data = await state.get_data()
        target = message.text.strip() if message.text else ""
        await _delete_message(message)
        await _delete_saved_prompt(message.chat.id, data)
        await state.clear()

        if not target:
            await message.answer("❌ Invalid Order ID.", reply_markup=_cancel_kb().as_markup())
            return

        if txns_col.find_one({"_id": f"bnc_{target}"}):
            await message.answer(
                "⚠️ This Order ID was already claimed.", reply_markup=_cancel_kb().as_markup()
            )
            return

        wait_msg = await message.answer("⌛ <i>Checking Binance Pay…</i>", parse_mode="HTML")
        res = verify_binance_order(target)
        if not res.get("success"):
            await wait_msg.edit_text(
                "❌ <b>Payment not found.</b> Make sure the ID is correct and try again in a minute.",
                parse_mode="HTML",
                reply_markup=_cancel_kb().as_markup(),
            )
            return

        amount_value = _parse_positive_amount(res.get("amount"))
        if amount_value is None:
            await wait_msg.edit_text(
                "❌ Invalid payment amount returned by Binance.",
                reply_markup=_cancel_kb().as_markup(),
            )
            return
        amount_inr = amount_value if str(res.get("currency")).upper() == "INR" else round(
            amount_value * USDT_TO_INR, 2
        )
        now = datetime.datetime.utcnow()
        txns_col.insert_one({
            "_id": f"bnc_{target}",
            "user_id": message.from_user.id,
            "method": "binance",
            "amount": amount_inr,
            "amount_received": amount_value,
            "currency": res.get("currency"),
            "status": "paid",
            "created_at": now,
            "settled_at": now,
        })
        users_col.update_one(
            {"_id": message.from_user.id}, {"$inc": {"balance": amount_inr}}, upsert=True
        )
        new_balance = get_balance(message.from_user.id)

        await wait_msg.edit_text(
            "✅ <b>Binance Pay Verified!</b>\n\n"
            f"💰 Amount Credited: <b>₹{_fmt_amount(amount_inr, 2)}</b>\n"
            f"🏦 New Balance: <b>₹{_fmt_amount(new_balance, 2)}</b>\n"
            f"👤 Payer: <code>{escape(str(res.get('payer_name', 'N/A')))}</code>\n"
            f"🆔 Order ID: <tg-spoiler><code>{escape(target)}</code></tg-spoiler>",
            parse_mode="HTML",
            reply_markup=_cancel_kb().as_markup(),
        )
        _remember_user_profile(message.from_user.id, message.from_user)
        await _send_admin_message(
            "🅱️ <b>Binance Pay Received</b>\n\n"
            f"{_user_details_text(message.from_user.id, message.from_user)}\n"
            f"💵 <b>Amount:</b> {_fmt_amount(amount_value)} {escape(str(res.get('currency') or 'INR'))}\n"
            f"💰 <b>Total (INR):</b> ₹{_fmt_amount(amount_inr, 2)}\n"
            f"🏦 <b>New Balance:</b> ₹{_fmt_amount(new_balance, 2)}\n"
            f"🆔 <b>Order ID:</b> <code>{escape(target)}</code>"
        )

    # ==============================
    # CRYPTO: ALL COINS / CURRENCY (OXAPAY)
    # ==============================
    @dp.callback_query(F.data == "recharge_oxapay")
    async def recharge_oxapay(cq: CallbackQuery, state: FSMContext):
        settings = get_settings(settings_col)
        if not settings.get("crypto_enabled", True):
            await cq.answer("⚠️ Crypto payment is currently unavailable.", show_alert=True)
            return
        await _delete_message(cq.message)
        msg = await cq.message.answer(
            f"🌐 <b>Other Crypto Coins</b>\n\n"
            f"Enter amount in <b>USDT</b>\n<i>Minimum: {MIN_USDT} USDT</i>",
            parse_mode="HTML",
            reply_markup=_cancel_kb("recharge_crypto").as_markup(),
        )
        await state.update_data(last_msg=msg.message_id)
        await state.set_state(RechargeState.crypto_amount)
        await cq.answer()

    # Compatibility for invoices created by an older deployment that pointed
    # the OxaPay button at this callback name.
    @dp.callback_query(F.data == "recharge_crypto_oxapay")
    async def recharge_crypto_oxapay_compat(cq: CallbackQuery, state: FSMContext):
        await recharge_oxapay(cq, state)

    @dp.message(StateFilter(RechargeState.crypto_amount))
    async def crypto_amt(message: Message, state: FSMContext):
        data = await state.get_data()
        await _delete_message(message)
        await _delete_saved_prompt(message.chat.id, data)

        usdt = _parse_positive_amount(message.text)
        if usdt is None or usdt < MIN_USDT:
            msg = await message.answer(
                f"❌ <b>Invalid amount.</b> Enter at least <u>{MIN_USDT} USDT</u>.",
                parse_mode="HTML",
                reply_markup=_cancel_kb("recharge_crypto").as_markup(),
            )
            await state.update_data(last_msg=msg.message_id)
            return

        wait_msg = await message.answer("⌛ <i>Generating invoice…</i>", parse_mode="HTML")
        order_id = f"Oxa_{message.from_user.id}_{int(datetime.datetime.now().timestamp())}"
        res = create_invoice(usdt, order_id)
        if not res["success"]:
            await wait_msg.edit_text(
                f"❌ <b>API Error:</b> {escape(str(res.get('error', 'Unable to create invoice')))}",
                parse_mode="HTML",
                reply_markup=_cancel_kb("recharge_crypto").as_markup(),
            )
            await state.clear()
            return

        inv_data = res["data"]
        track_id = str(inv_data["track_id"])
        pay_url = inv_data["payment_url"]
        inr_val = round(usdt * USDT_TO_INR, 2)
        crypto_col.insert_one({
            "user_id": message.from_user.id,
            "track_id": track_id,
            "method": "oxapay",
            "amount_usdt": usdt,
            "amount_inr": inr_val,
            "status": "pending",
            "created_at": datetime.datetime.utcnow(),
        })

        kb = InlineKeyboardBuilder()
        kb.button(text="💳 Pay Now", url=pay_url)
        kb.button(text="✅ I Have Paid", callback_data=f"check_crypto:{track_id}")
        kb.button(text="❌ Cancel", callback_data=f"cancel_crypto:{track_id}")
        kb.button(text="🔙 Back", callback_data="recharge_crypto")
        kb.adjust(1)
        await wait_msg.delete()
        await message.answer(
            "🌐 <b>All Crypto Coins</b>\n\n"
            f"💵 Amount: <b>{_fmt_amount(usdt)} USDT</b>\n"
            f"💰 Wallet credit: ₹{_fmt_amount(inr_val, 2)}\n"
            "⏳ Expires in: <i>30 minutes</i>\n\n"
            "<blockquote>Send the <u>exact amount</u> shown on the payment page."
            " Then tap <b>I Have Paid</b>.</blockquote>",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
        await state.clear()

    @dp.callback_query(F.data.startswith("check_crypto:"))
    async def check_crypto(cq: CallbackQuery):
        track_id = cq.data.split(":", 1)[1]
        inv = crypto_col.find_one({"track_id": track_id})
        if not inv:
            await cq.answer("Invoice not found.", show_alert=True)
            return
        if inv.get("status") == "paid":
            await cq.answer("✅ Already credited!", show_alert=True)
            return

        api_res = check_invoice(track_id)
        remote_status = "unknown"
        if isinstance(api_res.get("data"), dict) and "status" in api_res["data"]:
            remote_status = str(api_res["data"]["status"]).lower()
        elif "status" in api_res:
            remote_status = str(api_res["status"]).lower()

        if remote_status in ["paid", "complete", "confirmed"]:
            result = crypto_col.update_one(
                {"track_id": track_id, "status": {"$ne": "paid"}},
                {"$set": {"status": "paid", "settled_at": datetime.datetime.utcnow()}},
            )
            if getattr(result, "modified_count", 1) == 0:
                await cq.answer("✅ Already credited!", show_alert=True)
                return
            users_col.update_one(
                {"_id": inv["user_id"]}, {"$inc": {"balance": inv["amount_inr"]}}, upsert=True
            )
            usdt = inv["amount_usdt"]
            inr = inv["amount_inr"]
            new_balance = get_balance(inv["user_id"])
            await _delete_message(cq.message)
            await bot.send_message(
                inv["user_id"],
                "✅ <b>Payment Received!</b>\n\n"
                f"💵 Amount Added: <b>{_fmt_amount(usdt)} USDT</b>\n"
                f"💰 INR Value: ₹{_fmt_amount(inr, 2)}\n"
                f"🏦 New Balance: ₹{_fmt_amount(new_balance, 2)}\n"
                f"➕ Track ID: <code>{escape(track_id)}</code>",
                parse_mode="HTML",
                reply_markup=_cancel_kb().as_markup(),
            )
            _remember_user_profile(inv["user_id"], cq.from_user)
            await _send_admin_message(
                "🚀 <b>New All Coins Payment Received</b>\n\n"
                f"{_user_details_text(inv['user_id'], cq.from_user)}\n"
                f"🧾 <b>Method:</b> All Coins / Currency\n"
                f"🪙 <b>Amount:</b> {_fmt_amount(usdt)} USDT\n"
                f"💰 <b>Total (INR):</b> ₹{_fmt_amount(inr, 2)}\n"
                f"🏦 <b>New Balance:</b> ₹{_fmt_amount(new_balance, 2)}\n"
                f"🧾 <b>Track ID:</b> <code>{escape(track_id)}</code>"
            )
        elif remote_status == "confirming":
            await cq.answer("⏳ Payment detected but still confirming on blockchain.", show_alert=True)
        else:
            await cq.answer(
                f"Status: {escape(remote_status.capitalize())}\nPayment not confirmed yet.",
                show_alert=True,
            )

    @dp.callback_query(F.data.startswith("cancel_crypto:"))
    async def cancel_crypto(cq: CallbackQuery, state: FSMContext):
        await show_crypto_menu(cq.message, state, edit=True)
        await cq.answer()

    # ==============================
    # USDT (CWALLET API, AUTOMATIC)
    # ==============================
    async def show_usdt_networks(target, state: FSMContext, edit=False):
        await state.clear()
        networks = get_usdt_networks(settings_col)
        kb = InlineKeyboardBuilder()

        # Keep the configured order for custom deployments, but put common
        # networks in the same predictable order as the admin screen.
        ordered = [n for n in SUPPORTED_USDT_NETWORKS if n in networks]
        ordered.extend(n for n in networks if n not in ordered)
        for network in ordered:
            kb.button(
                text=f"💵 {network}",
                callback_data=f"usdt_network:{_network_token(network)}",
            )
        kb.button(text="🔙 Back", callback_data="recharge_crypto")
        kb.adjust(1)

        if networks:
            text = (
                "💵 <b>USDT Deposit</b>\n\n"
                "<blockquote><i>Select the network you will use.</i>\n"
                "Only send USDT on the selected network; sending on another network may cause permanent loss.</blockquote>"
            )
        else:
            text = (
                "💵 <b>USDT Deposit</b>\n\n"
                "<blockquote>⚠️ No USDT network is configured yet. Please contact "
                "<u>Support</u>.</blockquote>"
            )

        if edit:
            try:
                await target.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())
            except Exception:
                await _delete_message(target)
                await target.answer(text, parse_mode="HTML", reply_markup=kb.as_markup())
        else:
            await target.answer(text, parse_mode="HTML", reply_markup=kb.as_markup())
        await state.set_state(RechargeState.choose_method)

    @dp.callback_query(F.data == "recharge_usdt")
    async def recharge_usdt(cq: CallbackQuery, state: FSMContext):
        settings = get_settings(settings_col)
        if not settings.get("crypto_enabled", True) or not settings.get("usdt_enabled", True):
            await cq.answer("⚠️ USDT deposits are currently unavailable.", show_alert=True)
            return
        await show_usdt_networks(cq.message, state, edit=True)
        await cq.answer()

    @dp.callback_query(F.data.startswith("usdt_network:"))
    async def usdt_network_selected(cq: CallbackQuery, state: FSMContext):
        token = cq.data.split(":", 1)[1]
        network = _resolve_network(settings_col, token)
        networks = get_usdt_networks(settings_col)
        if not network or network not in networks:
            await cq.answer("This network is no longer available.", show_alert=True)
            return

        address = networks[network]
        await _delete_message(cq.message)
        kb = InlineKeyboardBuilder()
        kb.button(text="Copy Address", copy_text=CopyTextButton(text=address))
        kb.button(text="✅ I Have Paid", callback_data=f"usdt_paid:{_network_token(network)}")
        kb.button(text="🔙 Back", callback_data="recharge_usdt")
        kb.adjust(1)
        text = (
            "💵 <b>USDT Deposit</b>\n\n"
            f"🌐 <b>Network:</b> <code>{escape(network)}</code>\n\n"
            f"📍 <b>Address:</b> <tg-spoiler><code>{escape(address)}</code></tg-spoiler>\n\n"
            "<blockquote>1. Copy the address.\n"
            "2. Pay USDT on the <u>same network</u>.\n"
            "3. After the payment arrives, tap <b>I Have Paid</b> and send the TXID.</blockquote>\n"
            "⚠️ <i>Minimum deposit: 0.1 USDT. Network mismatch is not refundable.</i>"
        )
        await cq.message.answer(text, parse_mode="HTML", reply_markup=kb.as_markup())
        await cq.answer()

    @dp.callback_query(F.data.startswith("usdt_paid:"))
    async def usdt_paid(cq: CallbackQuery, state: FSMContext):
        token = cq.data.split(":", 1)[1]
        network = _resolve_network(settings_col, token)
        if not network or network not in get_usdt_networks(settings_col):
            await cq.answer("This network is no longer available.", show_alert=True)
            return
        msg = await cq.message.answer(
            "🧾 <b>Send your USDT Transaction ID</b>\n\n"
            "<i>Paste the TXID from your wallet. It will be checked automatically.</i>",
            parse_mode="HTML",
            reply_markup=_cancel_kb("recharge_usdt").as_markup(),
        )
        await state.update_data(last_msg=msg.message_id, usdt_network=network)
        await state.set_state(RechargeState.cwallet_usdt_txid)
        await cq.answer()

    @dp.message(StateFilter(RechargeState.cwallet_usdt_txid))
    async def cwallet_usdt_txid_entered(message: Message, state: FSMContext):
        data = await state.get_data()
        txid = message.text.strip() if message.text else ""
        await _delete_message(message)
        await _delete_saved_prompt(message.chat.id, data)
        await state.clear()

        if not txid or len(txid) > 256 or any(ch.isspace() for ch in txid):
            await message.answer(
                "❌ <b>Invalid Transaction ID.</b> Please send the complete TXID without spaces.",
                parse_mode="HTML",
                reply_markup=_cancel_kb("recharge_usdt").as_markup(),
            )
            return

        normalized_txid = txid.casefold()
        # Check both the current schema and names used by possible earlier
        # integrations before making another provider request.
        existing = txns_col.find_one({
            "method": "cwallet_usdt",
            "transaction_id_normalized": normalized_txid,
        })
        if not existing:
            existing = txns_col.find_one({"claimed_txid": normalized_txid})
        if not existing:
            # Also catch records made by a deployment that stored the TXID
            # without the normalized helper field.
            existing = txns_col.find_one({"transaction_id": txid})
        if existing:
            await message.answer(
                "⚠️ <b>This transaction has already been claimed.</b>\n"
                "The same TXID cannot be used twice.",
                parse_mode="HTML",
                reply_markup=_cancel_kb("recharge_usdt").as_markup(),
            )
            return

        wait_msg = await message.answer("⌛ <i>Verifying USDT payment…</i>", parse_mode="HTML")
        api_res = verify_cwallet_payment(txid, expected_currency="USDT")

        if api_res.get("received") is not True or api_res.get("success") is False:
            status_code = api_res.get("status_code") or 200
            if status_code == 401:
                result_text = "❌ <b>Verification configuration error.</b> Please contact Support."
            elif status_code >= 500:
                result_text = "⚠️ <b>Verification service is temporarily unavailable.</b> Try again shortly."
            else:
                result_text = (
                    "⏳ <b>Payment not found yet.</b>\n"
                    "Check the TXID and wait for the payment confirmation email to arrive, then try again."
                )
            await wait_msg.edit_text(
                result_text,
                parse_mode="HTML",
                reply_markup=_cancel_kb("recharge_usdt").as_markup(),
            )
            return

        matched_id = str(api_res.get("transaction_id") or txid).strip()
        if matched_id.casefold() != normalized_txid:
            await wait_msg.edit_text(
                "❌ The returned transaction ID did not match the TXID you sent. No credit was added.",
                parse_mode="HTML",
                reply_markup=_cancel_kb("recharge_usdt").as_markup(),
            )
            return

        currency = str(api_res.get("currency") or "").upper()
        amount_usdt = _parse_positive_amount(api_res.get("amount"))
        if currency != "USDT" or amount_usdt is None or amount_usdt < MIN_USDT:
            await wait_msg.edit_text(
                "❌ The payment was found, but it is not a valid USDT amount for this deposit.",
                parse_mode="HTML",
                reply_markup=_cancel_kb("recharge_usdt").as_markup(),
            )
            return

        amount_inr = round(amount_usdt * USDT_TO_INR, 2)
        now = datetime.datetime.utcnow()
        claim_id = "cwallet_usdt_" + hashlib.sha256(normalized_txid.encode()).hexdigest()
        claim = {
            "_id": claim_id,
            "user_id": message.from_user.id,
            "method": "cwallet_usdt",
            "network": data.get("usdt_network"),
            "transaction_id": txid,
            "transaction_id_normalized": normalized_txid,
            "claimed_txid": normalized_txid,
            "amount_usdt": amount_usdt,
            # Wallet balance is INR in the existing bot, so retain the same
            # conversion used by the existing OxaPay and Binance flows.
            "amount": amount_inr,
            "amount_inr": amount_inr,
            "currency": "USDT",
            "status": "paid",
            "created_at": now,
            "settled_at": now,
            "cwallet_message": api_res.get("message"),
        }
        try:
            txns_col.insert_one(claim)
        except Exception:
            # A deterministic _id makes a concurrent/repeated submission safe.
            already_claimed = txns_col.find_one({"_id": claim_id}) or txns_col.find_one({
                "claimed_txid": normalized_txid
            })
            if already_claimed:
                await wait_msg.edit_text(
                    "⚠️ <b>This transaction has already been claimed.</b>",
                    parse_mode="HTML",
                    reply_markup=_cancel_kb("recharge_usdt").as_markup(),
                )
                return
            await wait_msg.edit_text(
                "❌ Could not save this transaction safely. Please try again.",
                reply_markup=_cancel_kb("recharge_usdt").as_markup(),
            )
            return

        users_col.update_one(
            {"_id": message.from_user.id}, {"$inc": {"balance": amount_inr}}, upsert=True
        )
        new_balance = get_balance(message.from_user.id)
        network = escape(str(data.get("usdt_network") or "USDT"))
        await wait_msg.edit_text(
            "✅ <b>USDT Payment Received!</b>\n\n"
            f"🌐 Network: <code>{network}</code>\n"
            f"💵 Received: <b>{_fmt_amount(amount_usdt)} USDT</b>\n"
            f"💰 Wallet credit: <b>₹{_fmt_amount(amount_inr, 2)}</b>\n"
            f"🏦 New Balance: <b>₹{_fmt_amount(new_balance, 2)}</b>\n"
            f"🧾 TXID: <tg-spoiler><code>{escape(txid)}</code></tg-spoiler>",
            parse_mode="HTML",
            reply_markup=_cancel_kb().as_markup(),
        )
        _remember_user_profile(message.from_user.id, message.from_user)
        await _send_admin_message(
            "💵 <b>USDT Payment Received</b>\n\n"
            f"{_user_details_text(message.from_user.id, message.from_user)}\n"
            f"🌐 <b>Network:</b> <code>{network}</code>\n"
            f"💵 <b>Amount:</b> {_fmt_amount(amount_usdt)} USDT\n"
            f"💰 <b>Total (INR):</b> ₹{_fmt_amount(amount_inr, 2)}\n"
            f"🏦 <b>New Balance:</b> ₹{_fmt_amount(new_balance, 2)}\n"
            f"🧾 <b>TXID:</b> <code>{escape(txid)}</code>"
        )

    # ==============================
    # CWALLET (MANUAL)
    # ==============================
    @dp.callback_query(F.data == "recharge_cwallet_manual")
    async def recharge_cwallet_manual(cq: CallbackQuery, state: FSMContext):
        settings = get_settings(settings_col)
        if not settings.get("cwallet_manual_enabled", True):
            await cq.answer("⚠️ Manual Cwallet payment is currently unavailable.", show_alert=True)
            return

        cwallet_id = str(settings.get("cwallet_manual_id") or "47588130")
        await _delete_message(cq.message)
        kb = InlineKeyboardBuilder()
        kb.button(text="Copy Cwallet ID", copy_text=CopyTextButton(text=cwallet_id))
        kb.button(text="🔙 Back", callback_data="recharge")
        kb.adjust(1)
        msg = await cq.message.answer(
            "🧾 <b>Cwallet (Manual)</b>\n\n"
            f"🆔 Cwallet ID: <code>{escape(cwallet_id)}</code>\n\n"
            "<blockquote>Pay USDT to this Cwallet ID.\n"
            "Then enter the <u>exact amount paid</u>. Your screenshot will be reviewed by an admin.</blockquote>\n\n"
            f"💵 Enter amount in <b>USDT</b> (minimum {MIN_USDT} USDT):",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
        await state.update_data(last_msg=msg.message_id)
        await state.set_state(RechargeState.cwallet_manual_amount)
        await cq.answer()

    @dp.message(StateFilter(RechargeState.cwallet_manual_amount))
    async def cwallet_manual_amount_entered(message: Message, state: FSMContext):
        data = await state.get_data()
        await _delete_message(message)
        await _delete_saved_prompt(message.chat.id, data)
        amount_usdt = _parse_positive_amount(message.text)
        if amount_usdt is None or amount_usdt < MIN_USDT:
            msg = await message.answer(
                f"❌ <b>Invalid amount.</b> Enter at least <u>{MIN_USDT} USDT</u>.",
                parse_mode="HTML",
                reply_markup=_cancel_kb("recharge_cwallet_manual").as_markup(),
            )
            await state.update_data(last_msg=msg.message_id)
            return

        settings = get_settings(settings_col)
        cwallet_id = str(settings.get("cwallet_manual_id") or "47588130")
        msg = await message.answer(
            "📸 <b>Send payment screenshot</b>\n\n"
            f"🆔 Pay to: <code>{escape(cwallet_id)}</code>\n"
            f"💵 Amount: <b>{_fmt_amount(amount_usdt)} USDT</b>\n\n"
            "<blockquote>Upload a clear screenshot showing the payment details. "
            "An admin will <u>Accept</u> or <u>Reject</u> the request.</blockquote>",
            parse_mode="HTML",
            reply_markup=_cancel_kb("recharge_cwallet_manual").as_markup(),
        )
        await state.update_data(last_msg=msg.message_id, manual_amount_usdt=amount_usdt)
        await state.set_state(RechargeState.cwallet_manual_proof)

    @dp.message(StateFilter(RechargeState.cwallet_manual_proof))
    async def cwallet_manual_proof_received(message: Message, state: FSMContext):
        data = await state.get_data()
        file_id = None
        if getattr(message, "photo", None):
            file_id = message.photo[-1].file_id
        elif getattr(message, "document", None) and str(
            getattr(message.document, "mime_type", "")
        ).startswith("image/"):
            file_id = message.document.file_id

        if not file_id:
            await _delete_message(message)
            msg = await message.answer(
                "❌ Please send the screenshot as an image/photo.",
                reply_markup=_cancel_kb("recharge_cwallet_manual").as_markup(),
            )
            await state.update_data(last_msg=msg.message_id)
            return

        await _delete_message(message)
        await _delete_saved_prompt(message.chat.id, data)
        amount_usdt = _parse_positive_amount(data.get("manual_amount_usdt"))
        if amount_usdt is None:
            await state.clear()
            await message.answer(
                "❌ The amount session expired. Please start again.",
                reply_markup=_cancel_kb("recharge").as_markup(),
            )
            return

        amount_inr = round(amount_usdt * USDT_TO_INR, 2)
        request_id = f"cwm_{message.from_user.id}_{int(datetime.datetime.now().timestamp())}_{secrets.token_hex(3)}"
        settings = get_settings(settings_col)
        cwallet_id = str(settings.get("cwallet_manual_id") or "47588130")
        now = datetime.datetime.utcnow()
        tx = {
            "_id": request_id,
            "user_id": message.from_user.id,
            "method": "cwallet_manual",
            "cwallet_id": cwallet_id,
            "amount_usdt": amount_usdt,
            "amount": amount_inr,
            "amount_inr": amount_inr,
            "currency": "USDT",
            "proof_file_id": file_id,
            "status": "pending",
            "created_at": now,
        }
        txns_col.insert_one(tx)

        user_id = message.from_user.id
        current_balance = get_balance(user_id)
        _remember_user_profile(user_id, message.from_user)
        admin_kb = InlineKeyboardBuilder()
        admin_kb.button(text="✅ Accept", callback_data=f"cwm_approve:{request_id}")
        admin_kb.button(text="❌ Reject", callback_data=f"cwm_reject:{request_id}")
        admin_kb.adjust(2)
        admin_caption = (
            "🧾 <b>Cwallet (Manual) — Review Required</b>\n\n"
            f"{_user_details_text(user_id, message.from_user)}\n"
            f"🧾 <b>Method:</b> Cwallet (Manual)\n"
            f"💵 <b>Amount:</b> {_fmt_amount(amount_usdt)} USDT\n"
            f"💰 <b>Total (INR):</b> ₹{_fmt_amount(amount_inr, 2)}\n"
            f"🏦 <b>Current Balance:</b> ₹{_fmt_amount(current_balance, 2)}\n"
            f"🆔 <b>Cwallet ID:</b> <code>{escape(cwallet_id)}</code>\n"
            f"🔖 <b>Request ID:</b> <code>{escape(request_id)}</code>\n"
            f"🕐 <b>Submitted:</b> <code>{_fmt_dt(now)}</code>\n\n"
            "<i>Check the screenshot, then choose Accept or Reject.</i>"
        )
        sent_to_admin = False
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_photo(
                    admin_id,
                    file_id,
                    caption=admin_caption,
                    parse_mode="HTML",
                    reply_markup=admin_kb.as_markup(),
                )
                sent_to_admin = True
            except Exception as exc:
                print(f"Error sending manual proof to admin {admin_id}: {exc}")
                try:
                    await bot.send_message(
                        admin_id,
                        admin_caption + "\n\n⚠️ Screenshot could not be forwarded automatically.",
                        parse_mode="HTML",
                        reply_markup=admin_kb.as_markup(),
                    )
                    sent_to_admin = True
                except Exception:
                    pass

        await message.answer(
            "⏳ <b>Payment proof submitted</b>\n\n"
            f"💵 Amount: <b>{_fmt_amount(amount_usdt)} USDT</b>\n"
            f"🔖 Request ID: <tg-spoiler><code>{escape(request_id)}</code></tg-spoiler>\n\n"
            + (
                "<blockquote>An admin will review your screenshot and notify you after approval.</blockquote>"
                if sent_to_admin
                else "<blockquote>Your request is saved. Please contact Support because no admin notification was delivered.</blockquote>"
            ),
            parse_mode="HTML",
            reply_markup=_cancel_kb().as_markup(),
        )
        await state.clear()

    async def _edit_manual_review_message(message: Message, text: str):
        try:
            if getattr(message, "photo", None) or getattr(message, "document", None):
                await message.edit_caption(caption=text, parse_mode="HTML", reply_markup=None)
            else:
                await message.edit_text(text, parse_mode="HTML", reply_markup=None)
        except Exception:
            pass

    async def _manual_action(cq: CallbackQuery, approve: bool):
        if not is_admin(cq.from_user.id):
            await cq.answer("Not authorized.", show_alert=True)
            return
        request_id = cq.data.split(":", 1)[1]
        tx = txns_col.find_one({"_id": request_id, "method": "cwallet_manual"})
        if not tx:
            await cq.answer("Manual request not found.", show_alert=True)
            return
        if tx.get("status") != "pending":
            await cq.answer(f"Already {tx.get('status', 'processed')}.", show_alert=True)
            return

        now = datetime.datetime.utcnow()
        if approve:
            result = txns_col.update_one(
                {"_id": request_id, "method": "cwallet_manual", "status": "pending"},
                {
                    "$set": {
                        "status": "paid",
                        "review_status": "approved",
                        "reviewed_by": cq.from_user.id,
                        "reviewed_at": now,
                        "settled_at": now,
                    }
                },
            )
            if getattr(result, "modified_count", 1) == 0:
                await cq.answer("This request was already processed.", show_alert=True)
                return

            amount_inr = float(tx.get("amount_inr") or tx.get("amount") or 0)
            users_col.update_one(
                {"_id": tx["user_id"]}, {"$inc": {"balance": amount_inr}}, upsert=True
            )
            new_balance = get_balance(tx["user_id"])
            try:
                await bot.send_message(
                    tx["user_id"],
                    "✅ <b>Manual Payment Approved</b>\n\n"
                    f"💵 Amount: <b>{_fmt_amount(tx.get('amount_usdt'))} USDT</b>\n"
                    f"💰 Wallet credit: <b>₹{_fmt_amount(amount_inr, 2)}</b>\n"
                    f"🏦 New Balance: <b>₹{_fmt_amount(new_balance, 2)}</b>\n"
                    f"🔖 Request ID: <code>{escape(request_id)}</code>",
                    parse_mode="HTML",
                    reply_markup=_cancel_kb().as_markup(),
                )
            except Exception:
                pass
            admin_text = (
                "✅ <b>Cwallet (Manual) Accepted</b>\n\n"
                f"{_user_details_text(tx['user_id'])}\n"
                f"💵 <b>Amount:</b> {_fmt_amount(tx.get('amount_usdt'))} USDT\n"
                f"💰 <b>Total Credited:</b> ₹{_fmt_amount(amount_inr, 2)}\n"
                f"🏦 <b>New Balance:</b> ₹{_fmt_amount(new_balance, 2)}\n"
                f"🔖 <b>Request ID:</b> <code>{escape(request_id)}</code>\n"
                f"👮 <b>Reviewed by:</b> <code>{cq.from_user.id}</code>"
            )
            await _edit_manual_review_message(cq.message, admin_text)
            await cq.answer("✅ Accepted and credited")
        else:
            result = txns_col.update_one(
                {"_id": request_id, "method": "cwallet_manual", "status": "pending"},
                {
                    "$set": {
                        "status": "rejected",
                        "review_status": "rejected",
                        "reviewed_by": cq.from_user.id,
                        "reviewed_at": now,
                    }
                },
            )
            if getattr(result, "modified_count", 1) == 0:
                await cq.answer("This request was already processed.", show_alert=True)
                return

            try:
                await bot.send_message(
                    tx["user_id"],
                    "❌ <b>Manual Payment Rejected</b>\n\n"
                    "No balance was added. Please check that the screenshot is clear and the payment was sent to the correct Cwallet ID, then contact Support.",
                    parse_mode="HTML",
                    reply_markup=_cancel_kb().as_markup(),
                )
            except Exception:
                pass
            admin_text = (
                "❌ <b>Cwallet (Manual) Rejected</b>\n\n"
                f"{_user_details_text(tx['user_id'])}\n"
                f"💵 <b>Amount:</b> {_fmt_amount(tx.get('amount_usdt'))} USDT\n"
                f"💰 <b>INR Value:</b> ₹{_fmt_amount(tx.get('amount_inr') or tx.get('amount') or 0, 2)}\n"
                f"🔖 <b>Request ID:</b> <code>{escape(request_id)}</code>\n"
                f"👮 <b>Reviewed by:</b> <code>{cq.from_user.id}</code>"
            )
            await _edit_manual_review_message(cq.message, admin_text)
            await cq.answer("❌ Rejected")

    async def _manual_upi_action(cq: CallbackQuery, approve: bool):
        if not is_admin(cq.from_user.id):
            await cq.answer("Not authorized.", show_alert=True)
            return
        request_id = cq.data.split(":", 1)[1]
        tx = txns_col.find_one({"_id": request_id, "method": "upi_manual"})
        if not tx:
            await cq.answer("Manual UPI request not found.", show_alert=True)
            return
        if tx.get("status") != "pending":
            await cq.answer(f"Already {tx.get('status', 'processed')}.", show_alert=True)
            return
        if not tx.get("proof_file_id"):
            await cq.answer("Screenshot proof is missing.", show_alert=True)
            return

        amount = _parse_positive_amount(tx.get("amount_inr") or tx.get("amount"))
        if amount is None:
            await cq.answer("Invalid request amount. Please contact Support.", show_alert=True)
            return

        now = datetime.datetime.utcnow()
        if approve:
            result = txns_col.update_one(
                {"_id": request_id, "method": "upi_manual", "status": "pending"},
                {"$set": {
                    "status": "paid",
                    "review_status": "approved",
                    "reviewed_by": cq.from_user.id,
                    "reviewed_at": now,
                    "settled_at": now,
                }},
            )
            if getattr(result, "modified_count", 1) == 0:
                await cq.answer("This request was already processed.", show_alert=True)
                return

            users_col.update_one(
                {"_id": tx["user_id"]}, {"$inc": {"balance": amount}}, upsert=True
            )
            new_balance = get_balance(tx["user_id"])
            try:
                await bot.send_message(
                    tx["user_id"],
                    "✅ <b>Manual UPI Payment Approved</b>\n\n"
                    f"💰 Amount Added: <b>₹{_fmt_amount(amount, 2)}</b>\n"
                    f"🏦 New Balance: <b>₹{_fmt_amount(new_balance, 2)}</b>\n"
                    f"🔖 Request ID: <code>{escape(request_id)}</code>",
                    parse_mode="HTML",
                    reply_markup=_cancel_kb().as_markup(),
                )
            except Exception:
                pass
            admin_text = (
                "✅ <b>Manual UPI Payment Accepted</b>\n\n"
                f"{_user_details_text(tx['user_id'])}\n"
                f"💰 <b>Amount credited:</b> ₹{_fmt_amount(amount, 2)}\n"
                f"🏦 <b>New balance:</b> ₹{_fmt_amount(new_balance, 2)}\n"
                f"🔖 <b>Request ID:</b> <code>{escape(request_id)}</code>\n"
                f"👮 <b>Reviewed by:</b> <code>{cq.from_user.id}</code>"
            )
            await _edit_manual_review_message(cq.message, admin_text)
            await cq.answer("✅ Accepted and credited")
        else:
            result = txns_col.update_one(
                {"_id": request_id, "method": "upi_manual", "status": "pending"},
                {"$set": {
                    "status": "rejected",
                    "review_status": "rejected",
                    "reviewed_by": cq.from_user.id,
                    "reviewed_at": now,
                }},
            )
            if getattr(result, "modified_count", 1) == 0:
                await cq.answer("This request was already processed.", show_alert=True)
                return
            try:
                await bot.send_message(
                    tx["user_id"],
                    "❌ <b>Manual UPI Payment Rejected</b>\n\n"
                    "No balance was added. Please contact Support if you believe this was a mistake.",
                    parse_mode="HTML",
                    reply_markup=_cancel_kb().as_markup(),
                )
            except Exception:
                pass
            admin_text = (
                "❌ <b>Manual UPI Payment Rejected</b>\n\n"
                f"{_user_details_text(tx['user_id'])}\n"
                f"💰 <b>Amount requested:</b> ₹{_fmt_amount(amount, 2)}\n"
                f"🔖 <b>Request ID:</b> <code>{escape(request_id)}</code>\n"
                f"👮 <b>Reviewed by:</b> <code>{cq.from_user.id}</code>"
            )
            await _edit_manual_review_message(cq.message, admin_text)
            await cq.answer("❌ Rejected")

    @dp.callback_query(F.data.startswith("manual_upi_approve:"))
    async def manual_upi_approve(cq: CallbackQuery):
        await _manual_upi_action(cq, approve=True)

    @dp.callback_query(F.data.startswith("manual_upi_reject:"))
    async def manual_upi_reject(cq: CallbackQuery):
        await _manual_upi_action(cq, approve=False)

    @dp.callback_query(F.data.startswith("cwm_approve:"))
    async def cwallet_manual_approve(cq: CallbackQuery):
        await _manual_action(cq, approve=True)

    @dp.callback_query(F.data.startswith("cwm_reject:"))
    async def cwallet_manual_reject(cq: CallbackQuery):
        await _manual_action(cq, approve=False)

    # ==============================
    # RECHARGE HISTORY
    # ==============================
    @dp.callback_query(F.data == "recharge_history")
    async def recharge_history(cq: CallbackQuery, state: FSMContext):
        user_id = cq.from_user.id
        gateway_txns = list(
            txns_col.find({"user_id": user_id, "status": "paid"})
            .sort("settled_at", -1)
            .limit(HISTORY_LIMIT)
        )
        crypto_txns = list(
            crypto_col.find({"user_id": user_id, "status": "paid"})
            .sort("settled_at", -1)
            .limit(HISTORY_LIMIT)
        )

        method_labels = {
            "upi": "UPI",
            "upi_manual": "UPI (MANUAL)",
            "fampay": "FAMPAY",
            "binance": "BINANCE PAY",
            "cwallet_usdt": "USDT",
            "cwallet_manual": "CWALLET MANUAL",
            "oxapay": "ALL COINS",
        }
        combined = []
        for tx in gateway_txns:
            method = tx.get("method", "unknown")
            combined.append({
                "method": method_labels.get(method, str(method).upper()),
                "amount": tx.get("amount_inr") or tx.get("amount") or 0,
                "date": tx.get("settled_at") or tx.get("created_at"),
            })
        for tx in crypto_txns:
            combined.append({
                "method": method_labels.get(tx.get("method"), "ALL COINS"),
                "amount": tx.get("amount_inr") or 0,
                "date": tx.get("settled_at") or tx.get("created_at"),
            })

        combined.sort(key=lambda item: item["date"] or datetime.datetime.min, reverse=True)
        combined = combined[:HISTORY_LIMIT]
        if not combined:
            text = "📜 <b>Recharge History</b>\n\n<blockquote>No successful recharges yet.</blockquote>"
        else:
            lines = ["📜 <b>Recharge History</b>\n"]
            for tx in combined:
                date_value = tx["date"]
                date_str = date_value.strftime("%d %b %Y, %I:%M %p") if date_value else "N/A"
                lines.append(
                    f"• <b>{escape(str(tx['method']))}</b> — ₹{_fmt_amount(tx['amount'], 2)}\n"
                    f"  <code>{escape(date_str)}</code>"
                )
            text = "\n".join(lines)

        await state.clear()
        kb = _back_kb("recharge")
        try:
            await cq.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())
        except Exception:
            await cq.message.answer(text, parse_mode="HTML", reply_markup=kb.as_markup())
        await cq.answer()

    # ==============================
    # SHARED CREDIT HELPER (UPI / FAMPAY)
    # ==============================
    async def _credit_and_notify(
        event: CallbackQuery | Message,
        tx: dict,
        method_label: str,
        sender: str | None = None,
        extra_set: dict | None = None,
    ) -> bool:
        now = datetime.datetime.utcnow()
        update_fields = {"status": "paid", "settled_at": now}
        if extra_set:
            update_fields.update(extra_set)

        try:
            result = txns_col.update_one(
                {"_id": tx["_id"], "status": "pending"},
                {"$set": update_fields},
            )
        except Exception as exc:
            duplicate = None
            for field in ("claimed_txn_normalized", "claimed_utr_normalized"):
                value = (extra_set or {}).get(field)
                if value:
                    try:
                        candidate = txns_col.find_one({field: value})
                    except Exception:
                        candidate = None
                    if candidate and candidate.get("_id") != tx["_id"]:
                        duplicate = candidate
                        break
            if duplicate:
                error_text = "⚠️ This FamPay payment has already been claimed. No balance was added."
            else:
                print(f"[payment_credit] failed to save {method_label} payment: {exc}")
                error_text = (
                    "❌ Verification completed, but the payment could not be saved safely. "
                    "Please contact Support and do not submit this payment again."
                )
            if isinstance(event, CallbackQuery):
                await event.answer(error_text, show_alert=True)
            else:
                await event.answer(error_text, reply_markup=_cancel_kb().as_markup())
            return False

        if getattr(result, "modified_count", 1) == 0:
            if isinstance(event, CallbackQuery):
                await event.answer("✅ Already credited!", show_alert=True)
            else:
                await event.answer("✅ This payment has already been credited.")
            return False

        if extra_set:
            tx.update(extra_set)
        users_col.update_one(
            {"_id": tx["user_id"]}, {"$inc": {"balance": tx["amount"]}}, upsert=True
        )
        new_balance = get_balance(tx["user_id"])
        from_user = event.from_user
        _remember_user_profile(tx["user_id"], from_user)
        source_message = event.message if isinstance(event, CallbackQuery) else event
        await _delete_message(source_message)

        if method_label == "FamPay":
            payment_reference = (
                tx.get("utr") or tx.get("transaction_id") or tx.get("claimed_txn")
            )
            reference_line = (
                f"🧾 Reference: <code>{escape(str(payment_reference))}</code>\n"
                if payment_reference else ""
            )
            order_line = reference_line
            admin_reference_line = (
                f"🧾 <b>UTR / Transaction ID:</b> "
                f"<code>{escape(str(payment_reference))}</code>\n"
                if payment_reference else ""
            )
        else:
            order_line = f"🆔 Order ID: <code>{escape(str(tx['_id']))}</code>\n"
            admin_reference_line = f"🆔 <b>Order ID:</b> <code>{escape(str(tx['_id']))}</code>\n"

        await bot.send_message(
            tx["user_id"],
            "✅ <b>Payment Verified!</b>\n\n"
            f"💳 Method: <b>{escape(method_label)}</b>\n"
            f"💰 Amount Added: <b>₹{_fmt_amount(tx['amount'], 2)}</b>\n"
            f"🏦 New Balance: <b>₹{_fmt_amount(new_balance, 2)}</b>\n"
            f"{order_line}",
            parse_mode="HTML",
            reply_markup=_cancel_kb().as_markup(),
        )
        admin_text = (
            f"💳 <b>{escape(method_label)} Payment Received</b>\n\n"
            f"{_user_details_text(tx['user_id'], from_user)}\n"
            f"💰 <b>Total (INR):</b> ₹{_fmt_amount(tx['amount'], 2)}\n"
            f"🏦 <b>New Balance:</b> ₹{_fmt_amount(new_balance, 2)}\n"
            f"{admin_reference_line}"
        )
        if sender:
            admin_text += f"🧾 <b>Sender:</b> {escape(str(sender))}\n"
        await _send_admin_message(admin_text)
        if isinstance(event, CallbackQuery):
            await event.answer("✅ Payment verified")
        return True

    # ==============================
    # ADMIN: PAYMENT SETTINGS PANEL
    # ==============================
    def _settings_kb(settings: dict) -> InlineKeyboardBuilder:
        kb = InlineKeyboardBuilder()
        gateway = settings.get("active_inr_gateway")
        kb.button(
            text=f"💳 INR: {'UPI ✅' if gateway == 'upi' else 'UPI'}",
            callback_data="pset_inr:upi",
        )
        kb.button(
            text=f"💳 INR: {'FamPay ✅' if gateway == 'fampay' else 'FamPay'}",
            callback_data="pset_inr:fampay",
        )
        kb.button(
            text="🚫 Disable INR ✅" if not gateway else "🚫 Disable INR",
            callback_data="pset_inr:off",
        )
        kb.button(
            text=f"🅱️ Binance: {'ON ✅' if settings.get('binance_enabled', True) else 'OFF ❌'}",
            callback_data="pset_toggle:binance",
        )
        kb.button(
            text=f"🪙 Crypto menu: {'ON ✅' if settings.get('crypto_enabled', True) else 'OFF ❌'}",
            callback_data="pset_toggle:crypto",
        )
        kb.button(
            text=f"💵 USDT auto: {'ON ✅' if settings.get('usdt_enabled', True) else 'OFF ❌'}",
            callback_data="pset_toggle:usdt",
        )
        kb.button(
            text=f"🧾 Cwallet manual: {'ON ✅' if settings.get('cwallet_manual_enabled', True) else 'OFF ❌'}",
            callback_data="pset_toggle:cwallet_manual",
        )
        kb.button(
            text=f"🏦 Manual UPI: {'ON ✅' if settings.get('manual_upi_enabled', False) else 'OFF ❌'}",
            callback_data="pset_toggle:manual_upi",
        )
        kb.button(text="⚙️ USDT network addresses", callback_data="pset_usdt_networks")
        kb.button(
            text=f"🆔 Manual Cwallet ID: {str(settings.get('cwallet_manual_id', '47588130'))[:20]}",
            callback_data="pset_manual_id",
        )
        kb.button(
            text=f"💳 Manual UPI ID: {str(settings.get('manual_upi_id') or 'Not set')[:20]}",
            callback_data="pset_manual_upi_id",
        )
        kb.button(
            text=f"👤 Manual UPI name: {str(settings.get('manual_upi_name') or 'Not set')[:20]}",
            callback_data="pset_manual_upi_name",
        )
        kb.button(
            text="📷 Set/Replace Manual UPI QR",
            callback_data="pset_manual_upi_qr",
        )
        kb.button(text="🔙 Close", callback_data="pset_close")
        kb.adjust(2, 1, 2, 2, 1, 1, 1, 2, 1, 1)
        return kb

    async def show_payment_settings(target, state: FSMContext | None = None, edit=False):
        if state:
            await state.clear()
        settings = get_settings(settings_col)
        configured_count = len(get_usdt_networks(settings_col))
        manual_qr_status = "Set ✅" if settings.get("manual_upi_qr_file_id") else "Not set ❌"
        text = (
            "⚙️ <b>Payment Gateway Settings</b>\n\n"
            "<blockquote>"
            f"INR gateway: <b>{escape(str(settings.get('active_inr_gateway') or 'Disabled').upper())}</b>\n"
            f"USDT networks configured: <b>{configured_count}</b>\n"
            f"Manual Cwallet ID: <code>{escape(str(settings.get('cwallet_manual_id', '47588130')))}</code>\n"
            f"Manual UPI status: <b>{'ON' if settings.get('manual_upi_enabled', False) else 'OFF'}</b>\n"
            f"Manual UPI ID: <code>{escape(str(settings.get('manual_upi_id') or 'Not set'))}</code>\n"
            f"Manual UPI payee: <b>{escape(str(settings.get('manual_upi_name') or 'Not set'))}</b>\n"
            f"Manual UPI QR: <b>{manual_qr_status}</b>"
            "</blockquote>\n"
            "<i>Tap a switch to turn a method on or off. Secrets such as the API key stay in config/env.</i>"
        )
        kb = _settings_kb(settings)
        if edit:
            try:
                await target.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())
            except Exception:
                await _delete_message(target)
                await target.answer(text, parse_mode="HTML", reply_markup=kb.as_markup())
        else:
            await target.answer(text, parse_mode="HTML", reply_markup=kb.as_markup())

    async def show_usdt_settings(target, state: FSMContext | None = None, edit=False):
        if state:
            await state.clear()
        configured = get_usdt_networks(settings_col)
        names = _known_networks(settings_col)
        lines = [
            "⚙️ <b>USDT Network Addresses</b>",
            "",
            "<blockquote>Only networks with an address are shown to users. "
            "Choose a network to add or replace its address.</blockquote>",
            "",
        ]
        for network in names:
            if network in configured:
                address = escape(configured[network])
                lines.append(f"• <b>{escape(network)}</b> — <tg-spoiler><code>{address}</code></tg-spoiler>")
            else:
                lines.append(f"• <b>{escape(network)}</b> — <i>Not configured</i>")

        kb = InlineKeyboardBuilder()
        for network in names:
            icon = "✅" if network in configured else "➕"
            kb.button(
                text=f"{icon} {network}",
                callback_data=f"pset_usdt_network:{_network_token(network)}",
            )
            if network in configured:
                kb.button(
                    text=f"🗑 {network}",
                    callback_data=f"pset_usdt_remove:{_network_token(network)}",
                )
        kb.button(text="➕ Add custom network", callback_data="pset_usdt_custom")
        kb.button(text="🔙 Back to settings", callback_data="pset_back")
        kb.adjust(2)
        text = "\n".join(lines)
        if edit:
            try:
                await target.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())
            except Exception:
                await _delete_message(target)
                await target.answer(text, parse_mode="HTML", reply_markup=kb.as_markup())
        else:
            await target.answer(text, parse_mode="HTML", reply_markup=kb.as_markup())

    async def ask_admin_network_address(message: Message, state: FSMContext, network: str):
        current = get_usdt_networks(settings_col).get(network)
        current_text = (
            f"\nCurrent: <tg-spoiler><code>{escape(current)}</code></tg-spoiler>"
            if current
            else "\nCurrent: <i>Not configured</i>"
        )
        kb = InlineKeyboardBuilder()
        kb.button(text="🗑 Remove", callback_data=f"pset_usdt_remove:{_network_token(network)}")
        kb.button(text="🔙 Back", callback_data="pset_usdt_networks")
        kb.adjust(1)
        prompt = await message.answer(
            "📍 <b>USDT address</b>\n\n"
            f"Network: <code>{escape(network)}</code>{current_text}\n\n"
            "Send the deposit address now, or use <b>Remove</b> to disable this network.",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
        await state.update_data(last_msg=prompt.message_id, admin_network=network)
        await state.set_state(RechargeState.admin_usdt_address)

    @dp.message(Command("paymentsettings"))
    async def payment_settings_cmd(message: Message, state: FSMContext):
        if not is_admin(message.from_user.id):
            return
        await show_payment_settings(message, state=state, edit=False)

    @dp.callback_query(F.data.startswith("pset_inr:"))
    async def pset_inr(cq: CallbackQuery):
        if not is_admin(cq.from_user.id):
            await cq.answer("Not authorized.", show_alert=True)
            return
        choice = cq.data.split(":", 1)[1]
        set_inr_gateway(settings_col, None if choice == "off" else choice)
        await show_payment_settings(cq.message, edit=True)
        await cq.answer(f"INR gateway set to: {choice}")

    @dp.callback_query(F.data.startswith("pset_toggle:"))
    async def pset_toggle(cq: CallbackQuery):
        if not is_admin(cq.from_user.id):
            await cq.answer("Not authorized.", show_alert=True)
            return
        which = cq.data.split(":", 1)[1]
        if which == "binance":
            toggle_binance(settings_col)
        elif which == "crypto":
            toggle_crypto(settings_col)
        elif which == "usdt":
            toggle_usdt(settings_col)
        elif which == "cwallet_manual":
            toggle_cwallet_manual(settings_col)
        elif which == "manual_upi":
            current = get_settings(settings_col).get("manual_upi_enabled", False)
            settings_col.update_one(
                {"_id": "payment_gateways"},
                {"$set": {"manual_upi_enabled": not current}},
                upsert=True,
            )
        await show_payment_settings(cq.message, edit=True)
        await cq.answer("Updated")

    @dp.callback_query(F.data == "pset_usdt_networks")
    async def pset_usdt_networks(cq: CallbackQuery, state: FSMContext):
        if not is_admin(cq.from_user.id):
            await cq.answer("Not authorized.", show_alert=True)
            return
        await show_usdt_settings(cq.message, state=state, edit=True)
        await cq.answer()

    @dp.callback_query(F.data.startswith("pset_usdt_network:"))
    async def pset_usdt_network(cq: CallbackQuery, state: FSMContext):
        if not is_admin(cq.from_user.id):
            await cq.answer("Not authorized.", show_alert=True)
            return
        token = cq.data.split(":", 1)[1]
        network = _resolve_network(settings_col, token, include_unconfigured=True)
        if not network:
            await cq.answer("Network not found.", show_alert=True)
            return
        await state.clear()
        await ask_admin_network_address(cq.message, state, network)
        await cq.answer()

    @dp.callback_query(F.data.startswith("pset_usdt_remove:"))
    async def pset_usdt_remove(cq: CallbackQuery, state: FSMContext):
        if not is_admin(cq.from_user.id):
            await cq.answer("Not authorized.", show_alert=True)
            return
        token = cq.data.split(":", 1)[1]
        network = _resolve_network(settings_col, token, include_unconfigured=True)
        if network:
            remove_usdt_network(settings_col, network)
        await show_usdt_settings(cq.message, state=state, edit=True)
        await cq.answer("Network removed")

    @dp.callback_query(F.data == "pset_usdt_custom")
    async def pset_usdt_custom(cq: CallbackQuery, state: FSMContext):
        if not is_admin(cq.from_user.id):
            await cq.answer("Not authorized.", show_alert=True)
            return
        await state.clear()
        msg = await cq.message.answer(
            "✏️ <b>Custom network name</b>\n\nSend a short label such as <code>TRC20</code> or <code>BASE</code>.",
            parse_mode="HTML",
            reply_markup=_back_kb("pset_usdt_networks").as_markup(),
        )
        await state.update_data(last_msg=msg.message_id)
        await state.set_state(RechargeState.admin_usdt_network_name)
        await cq.answer()

    @dp.message(StateFilter(RechargeState.admin_usdt_network_name))
    async def admin_usdt_network_name(message: Message, state: FSMContext):
        if not is_admin(message.from_user.id):
            return
        data = await state.get_data()
        network = message.text.strip() if message.text else ""
        await _delete_message(message)
        await _delete_saved_prompt(message.chat.id, data)
        if not network or len(network) > 48 or ":" in network:
            msg = await message.answer(
                "❌ Invalid network name. Use 1–48 characters without a colon.",
                reply_markup=_back_kb("pset_usdt_networks").as_markup(),
            )
            await state.update_data(last_msg=msg.message_id)
            return
        await ask_admin_network_address(message, state, network)

    @dp.message(StateFilter(RechargeState.admin_usdt_address))
    async def admin_usdt_address(message: Message, state: FSMContext):
        if not is_admin(message.from_user.id):
            return
        data = await state.get_data()
        network = data.get("admin_network")
        address = message.text.strip() if message.text else ""
        await _delete_message(message)
        await _delete_saved_prompt(message.chat.id, data)
        if not network or not address:
            msg = await message.answer(
                "❌ Address cannot be empty. Send the address as text.",
                reply_markup=_back_kb("pset_usdt_networks").as_markup(),
            )
            await state.update_data(last_msg=msg.message_id)
            return
        try:
            set_usdt_network(settings_col, network, address)
        except ValueError as exc:
            msg = await message.answer(
                f"❌ {escape(str(exc))}",
                parse_mode="HTML",
                reply_markup=_back_kb("pset_usdt_networks").as_markup(),
            )
            await state.update_data(last_msg=msg.message_id)
            return
        await show_usdt_settings(message, state=state, edit=False)

    @dp.callback_query(F.data == "pset_manual_upi_id")
    async def pset_manual_upi_id(cq: CallbackQuery, state: FSMContext):
        if not is_admin(cq.from_user.id):
            await cq.answer("Not authorized.", show_alert=True)
            return
        await state.clear()
        current = get_settings(settings_col).get("manual_upi_id") or "Not set"
        prompt = await cq.message.answer(
            "💳 <b>Manual UPI ID</b>\n\n"
            f"Current: <code>{escape(str(current))}</code>\n\n"
            "Send the UPI ID now, for example <code>name@bank</code>.",
            parse_mode="HTML",
            reply_markup=_back_kb("pset_back").as_markup(),
        )
        await state.update_data(last_msg=prompt.message_id)
        await state.set_state(RechargeState.admin_manual_upi_id)
        await cq.answer()

    @dp.message(StateFilter(RechargeState.admin_manual_upi_id))
    async def admin_manual_upi_id(message: Message, state: FSMContext):
        if not is_admin(message.from_user.id):
            return
        data = await state.get_data()
        upi_id = message.text.strip() if message.text else ""
        await _delete_message(message)
        await _delete_saved_prompt(message.chat.id, data)
        parts = upi_id.rsplit("@", 1)
        if (
            len(parts) != 2 or not parts[0] or not parts[1]
            or len(upi_id) > 128 or any(ch.isspace() for ch in upi_id)
        ):
            prompt = await message.answer(
                "❌ Invalid UPI ID. Send a valid ID such as <code>name@bank</code>.",
                parse_mode="HTML",
                reply_markup=_back_kb("pset_back").as_markup(),
            )
            await state.update_data(last_msg=prompt.message_id)
            return
        settings_col.update_one(
            {"_id": "payment_gateways"},
            {"$set": {"manual_upi_id": upi_id}},
            upsert=True,
        )
        await show_payment_settings(message, state=state, edit=False)

    @dp.callback_query(F.data == "pset_manual_upi_name")
    async def pset_manual_upi_name(cq: CallbackQuery, state: FSMContext):
        if not is_admin(cq.from_user.id):
            await cq.answer("Not authorized.", show_alert=True)
            return
        await state.clear()
        current = get_settings(settings_col).get("manual_upi_name") or "Not set"
        prompt = await cq.message.answer(
            "👤 <b>Manual UPI Payee Name</b>\n\n"
            f"Current: <b>{escape(str(current))}</b>\n\nSend the name shown to users.",
            parse_mode="HTML",
            reply_markup=_back_kb("pset_back").as_markup(),
        )
        await state.update_data(last_msg=prompt.message_id)
        await state.set_state(RechargeState.admin_manual_upi_name)
        await cq.answer()

    @dp.message(StateFilter(RechargeState.admin_manual_upi_name))
    async def admin_manual_upi_name(message: Message, state: FSMContext):
        if not is_admin(message.from_user.id):
            return
        data = await state.get_data()
        payee_name = message.text.strip() if message.text else ""
        await _delete_message(message)
        await _delete_saved_prompt(message.chat.id, data)
        if not payee_name or len(payee_name) > 100:
            prompt = await message.answer(
                "❌ Name cannot be empty and must be at most 100 characters.",
                reply_markup=_back_kb("pset_back").as_markup(),
            )
            await state.update_data(last_msg=prompt.message_id)
            return
        settings_col.update_one(
            {"_id": "payment_gateways"},
            {"$set": {"manual_upi_name": payee_name}},
            upsert=True,
        )
        await show_payment_settings(message, state=state, edit=False)

    @dp.callback_query(F.data == "pset_manual_upi_qr")
    async def pset_manual_upi_qr(cq: CallbackQuery, state: FSMContext):
        if not is_admin(cq.from_user.id):
            await cq.answer("Not authorized.", show_alert=True)
            return
        await state.clear()
        prompt = await cq.message.answer(
            "📷 <b>Manual UPI QR</b>\n\n"
            "Send the ready-made QR as a Telegram photo (not as a document). This replaces the current QR.",
            parse_mode="HTML",
            reply_markup=_back_kb("pset_back").as_markup(),
        )
        await state.update_data(last_msg=prompt.message_id)
        await state.set_state(RechargeState.admin_manual_upi_qr)
        await cq.answer()

    @dp.message(StateFilter(RechargeState.admin_manual_upi_qr))
    async def admin_manual_upi_qr(message: Message, state: FSMContext):
        if not is_admin(message.from_user.id):
            return
        data = await state.get_data()
        file_id = message.photo[-1].file_id if getattr(message, "photo", None) else None
        await _delete_message(message)
        await _delete_saved_prompt(message.chat.id, data)
        if not file_id:
            prompt = await message.answer(
                "❌ Send the QR as a photo/image, not as text or a document.",
                reply_markup=_back_kb("pset_back").as_markup(),
            )
            await state.update_data(last_msg=prompt.message_id)
            return
        settings_col.update_one(
            {"_id": "payment_gateways"},
            {"$set": {"manual_upi_qr_file_id": file_id}},
            upsert=True,
        )
        await show_payment_settings(message, state=state, edit=False)

    @dp.callback_query(F.data == "pset_manual_id")
    async def pset_manual_id(cq: CallbackQuery, state: FSMContext):
        if not is_admin(cq.from_user.id):
            await cq.answer("Not authorized.", show_alert=True)
            return
        await state.clear()
        current = get_settings(settings_col).get("cwallet_manual_id", "47588130")
        msg = await cq.message.answer(
            "🆔 <b>Manual Cwallet ID</b>\n\n"
            f"Current: <code>{escape(str(current))}</code>\n\nSend the new Cwallet ID.",
            parse_mode="HTML",
            reply_markup=_back_kb("pset_back").as_markup(),
        )
        await state.update_data(last_msg=msg.message_id)
        await state.set_state(RechargeState.admin_cwallet_id)
        await cq.answer()

    @dp.message(StateFilter(RechargeState.admin_cwallet_id))
    async def admin_cwallet_id(message: Message, state: FSMContext):
        if not is_admin(message.from_user.id):
            return
        data = await state.get_data()
        cwallet_id = message.text.strip() if message.text else ""
        await _delete_message(message)
        await _delete_saved_prompt(message.chat.id, data)
        try:
            set_cwallet_manual_id(settings_col, cwallet_id)
        except ValueError as exc:
            msg = await message.answer(
                f"❌ {escape(str(exc))}",
                parse_mode="HTML",
                reply_markup=_back_kb("pset_back").as_markup(),
            )
            await state.update_data(last_msg=msg.message_id)
            return
        await show_payment_settings(message, state=state, edit=False)

    @dp.callback_query(F.data == "pset_back")
    async def pset_back(cq: CallbackQuery, state: FSMContext):
        if not is_admin(cq.from_user.id):
            await cq.answer("Not authorized.", show_alert=True)
            return
        await show_payment_settings(cq.message, state=state, edit=True)
        await cq.answer()

    @dp.callback_query(F.data == "pset_close")
    async def pset_close(cq: CallbackQuery, state: FSMContext):
        if is_admin(cq.from_user.id):
            await state.clear()
            await _delete_message(cq.message)
        await cq.answer()

    # ==============================
    # BACKGROUND AUTO-SWEEPER
    # ==============================
    async def _auto_notify(tx: dict, amount: float, method_label: str):
        users_col.update_one(
            {"_id": tx["user_id"]}, {"$inc": {"balance": amount}}, upsert=True
        )
        new_balance = get_balance(tx["user_id"])
        await bot.send_message(
            tx["user_id"],
            "✅ <b>Auto-Verified!</b>\n\n"
            f"💳 Method: <b>{escape(method_label)}</b>\n"
            f"💰 Amount Added: <b>₹{_fmt_amount(amount, 2)}</b>\n"
            f"🏦 New Balance: <b>₹{_fmt_amount(new_balance, 2)}</b>\n"
            f"🆔 Order ID: <code>{escape(str(tx['_id']))}</code>",
            parse_mode="HTML",
            reply_markup=_cancel_kb().as_markup(),
        )
        await _send_admin_message(
            f"💳 <b>{escape(method_label)} Payment Received (Auto-Sweep)</b>\n\n"
            f"{_user_details_text(tx['user_id'])}\n"
            f"💰 <b>Total (INR):</b> ₹{_fmt_amount(amount, 2)}\n"
            f"🏦 <b>New Balance:</b> ₹{_fmt_amount(new_balance, 2)}\n"
            f"🆔 <b>Order ID:</b> <code>{escape(str(tx['_id']))}</code>"
        )

    async def _auto_sweep_loop():
        while True:
            try:
                now = datetime.datetime.utcnow()
                pending = list(txns_col.find({
                    "status": "pending",
                    "method": "upi",
                    "expires_at": {"$gt": now},
                }))

                for tx in pending:
                    if verify_upi_invoice(tx["_id"]):
                        result = txns_col.update_one(
                            {"_id": tx["_id"], "status": "pending"},
                            {"$set": {"status": "paid", "settled_at": now}},
                        )
                        if getattr(result, "modified_count", 1) != 0:
                            await _auto_notify(tx, float(tx["amount"]), "UPI")

            except Exception as exc:
                print(f"[auto_sweep] error: {exc}")

            await asyncio.sleep(AUTO_SWEEP_INTERVAL_SEC)

    def start_auto_sweeper():
        return asyncio.create_task(_auto_sweep_loop())

    return start_auto_sweeper