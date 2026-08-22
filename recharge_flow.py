import asyncio
import datetime
from bson import ObjectId

from aiogram import F
from aiogram.types import (
    CallbackQuery,
    Message,
    FSInputFile,
    CopyTextButton
)
from html import escape
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.filters import StateFilter, Command

# Crypto (unchanged, already automatic)
from oxapay import create_invoice, check_invoice

# UPI / FamPay / Binance (automatic gateways)
from auto_pay import (
    create_upi_invoice, verify_upi_invoice,
    create_fampay_invoice, verify_fampay_invoice,
    verify_binance_order,
)
from payment_settings import (
    get_settings, set_inr_gateway, toggle_binance, toggle_crypto,
)
from config import BINANCE_PAY_ID, BINANCE_QR_URL

# CONFIG
USDT_TO_INR = 90
MIN_USDT = 0.1
UPI_INVOICE_EXPIRY_MIN = 60          # UPI/FamPay invoices expire after 1 hour
AUTO_SWEEP_INTERVAL_SEC = 60          # background auto-verify loop interval
HISTORY_LIMIT = 10


class RechargeState(StatesGroup):
    choose_method = State()
    upi_amount = State()
    crypto_amount = State()
    binance_orderid = State()


def register_recharge_handlers(dp, bot, users_col, txns_col, crypto_col, settings_col, ADMIN_IDS):

    def is_admin(uid: int) -> bool:
        return uid in ADMIN_IDS

    def get_balance(user_id: int) -> float:
        user = users_col.find_one({"_id": user_id})
        return user.get("balance", 0) if user else 0

    # ==============================
    # TERMS & CONDITIONS MENU
    # ==============================
    async def show_recharge_terms(target, edit=False):
        text = (
            "⚠️ <b>Recharge Terms & Conditions</b>\n\n"
            "<blockquote>"
            "• <b>All recharges are 100% NON-REFUNDABLE.</b>\n"
            "• Once the amount is added to your bot wallet, it <b>cannot be withdrawn</b> or transferred back to your bank or crypto wallet under any circumstances.\n"
            "• <b>Do not ask for refund if any item/service is out of stock.</b> Please try another country/operator or wait for restock."
            "</blockquote>\n\n"
            "<i>By clicking '✅ I Accept', you agree to these terms.</i>\n\n"
            "🎧 <b>Note:</b> If you face any issues during the recharge, please contact our Support team."
        )
        kb = InlineKeyboardBuilder()
        kb.button(text="✅ I Accept", callback_data="recharge_accept_terms")
        kb.button(text="❌ Cancel", callback_data="back_main")
        kb.adjust(1)

        if edit:
            try:
                await target.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())
            except Exception:
                try:
                    await target.delete()
                except Exception:
                    pass
                await target.answer(text, parse_mode="HTML", reply_markup=kb.as_markup())
        else:
            await target.answer(text, parse_mode="HTML", reply_markup=kb.as_markup())

    # ==============================
    # MAIN RECHARGE MENU (Shows method buttons)
    # ==============================
    async def show_recharge_menu(target, state: FSMContext, edit=False):
        await state.clear()
        settings = get_settings(settings_col)

        kb = InlineKeyboardBuilder()
        available = 0

        if settings.get("active_inr_gateway"):
            kb.button(text="💳 UPI (Auto)", callback_data="recharge_upi_auto")
            available += 1
        if settings.get("binance_enabled", True):
            kb.button(text="🅱️ Binance (Auto)", callback_data="recharge_binance")
            available += 1
        if settings.get("crypto_enabled", True):
            kb.button(text="🪙 Crypto (Auto)", callback_data="recharge_crypto")
            available += 1
        kb.button(text="📜 Recharge History", callback_data="recharge_history")
        kb.button(text="🔙 Back", callback_data="back_main")
        kb.adjust(1)

        if available == 0:
            text = "💰 <b>Add Balance</b>\n\n⚠️ All payment methods are currently disabled. Please contact support."
        else:
            text = "💰 <b>Add Balance</b>\n\nChoose a payment method:"

        if edit:
            try:
                await target.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())
            except Exception:
                try:
                    await target.delete()
                except Exception:
                    pass
                await target.answer(text, parse_mode="HTML", reply_markup=kb.as_markup())
        else:
            await target.answer(text, parse_mode="HTML", reply_markup=kb.as_markup())
        await state.set_state(RechargeState.choose_method)

    # ==============================
    # TRIGGERS (With One-Time Check Logic)
    # ==============================
    @dp.callback_query(F.data == "recharge")
    async def recharge_btn(cq: CallbackQuery, state: FSMContext):
        user_id = cq.from_user.id
        user = users_col.find_one({"_id": user_id}) or {}

        # 🚀 Check: Agar pehle se accept kar rakha hai toh direct menu
        if user.get("recharge_terms_accepted"):
            await show_recharge_menu(cq.message, state, edit=True)
        else:
            # First time user: show Terms
            await show_recharge_terms(cq.message, edit=True)

    @dp.message(Command("recharge"))
    async def recharge_cmd(message: Message, state: FSMContext):
        user_id = message.from_user.id
        user = users_col.find_one({"_id": user_id}) or {}

        # 🚀 Check: Agar pehle se accept kar rakha hai toh direct menu
        if user.get("recharge_terms_accepted"):
            await show_recharge_menu(message, state, edit=False)
        else:
            # First time user: show Terms
            await show_recharge_terms(message, edit=False)

    @dp.callback_query(F.data == "recharge_accept_terms")
    async def recharge_accept_terms(cq: CallbackQuery, state: FSMContext):
        user_id = cq.from_user.id
        
        # 💾 Database me save kar lo taaki agli baar na maange
        users_col.update_one(
            {"_id": user_id},
            {"$set": {"recharge_terms_accepted": True}},
            upsert=True
        )
        
        # Ab main recharge menu khol do
        await show_recharge_menu(cq.message, state, edit=True)

    # ==============================
    # Small helper: cancel button used on all "please type something" prompts
    # ==============================
    def _cancel_kb():
        kb = InlineKeyboardBuilder()
        kb.button(text="🔙 Back", callback_data="recharge")
        kb.adjust(1)
        return kb

    # ==============================
    # UPI (Auto) — merged UPI Gateway / FamPay, admin picks the active one
    # ==============================
    @dp.callback_query(F.data == "recharge_upi_auto")
    async def recharge_upi_auto(cq: CallbackQuery, state: FSMContext):
        settings = get_settings(settings_col)
        gateway = settings.get("active_inr_gateway")

        if gateway == "fampay":
            await cq.message.delete()
            wait_msg = await cq.message.answer("⌛ Generating QR...")
            res = create_fampay_invoice()
            if not res["success"]:
                await wait_msg.edit_text(f"❌ Error: {res['error']}", reply_markup=_cancel_kb().as_markup())
                return

            order_id = res["order_id"]
            txns_col.insert_one({
                "_id": order_id,
                "user_id": cq.from_user.id,
                "method": "fampay",
                "amount": None,
                "status": "pending",
                "created_at": datetime.datetime.utcnow(),
                "expires_at": datetime.datetime.utcnow() + datetime.timedelta(minutes=UPI_INVOICE_EXPIRY_MIN),
            })

            kb = InlineKeyboardBuilder()
            kb.button(text="✅ Verify Payment", callback_data=f"verify_fampay:{order_id}")
            kb.button(text="🔙 Back", callback_data="recharge")
            kb.adjust(1)

            caption = (
                "📲 <b>UPI Payment (Auto)</b>\n\n"
                f"🆔 Order ID: <tg-spoiler>{order_id}</tg-spoiler>\n"
                f"⏳ Expires: <code>{res.get('expire', 'N/A')}</code>\n\n"
                "<blockquote>Scan the QR and pay any amount. Once done, tap 'Verify Payment' — it's fully automatic.</blockquote>"
            )
            await wait_msg.delete()
            await cq.message.answer_photo(res["qr_url"], caption=caption, parse_mode="HTML", reply_markup=kb.as_markup())

        elif gateway == "upi":
            await cq.message.delete()
            msg = await cq.message.answer(
                "💰 Enter amount to deposit (INR):",
                reply_markup=_cancel_kb().as_markup()
            )
            await state.update_data(last_msg=msg.message_id)
            await state.set_state(RechargeState.upi_amount)

        else:
            await cq.answer("⚠️ UPI payment is currently unavailable.", show_alert=True)

    @dp.message(StateFilter(RechargeState.upi_amount))
    async def upi_amount_entered(message: Message, state: FSMContext):
        await message.delete()
        data = await state.get_data()
        try:
            await bot.delete_message(message.chat.id, data.get("last_msg"))
        except Exception:
            pass

        if not message.text or not message.text.isdigit():
            msg = await message.answer("❌ Invalid amount. Please enter a number.", reply_markup=_cancel_kb().as_markup())
            await state.update_data(last_msg=msg.message_id)
            return

        amount = float(message.text)
        wait_msg = await message.answer("⌛ Generating QR...")

        res = create_upi_invoice(amount)
        if not res["success"]:
            await wait_msg.edit_text(f"❌ Error: {res['error']}", reply_markup=_cancel_kb().as_markup())
            await state.clear()
            return

        order_id = res["order_id"]
        txns_col.insert_one({
            "_id": order_id,
            "user_id": message.from_user.id,
            "method": "upi",
            "amount": amount,
            "status": "pending",
            "created_at": datetime.datetime.utcnow(),
            "expires_at": datetime.datetime.utcnow() + datetime.timedelta(minutes=UPI_INVOICE_EXPIRY_MIN),
        })

        kb = InlineKeyboardBuilder()
        kb.button(text="✅ Verify Payment", callback_data=f"verify_upi:{order_id}")
        kb.button(text="🔙 Back", callback_data="recharge")
        kb.adjust(1)

        caption = (
            "📲 <b>UPI Payment (Auto)</b>\n\n"
            f"💵 Amount: ₹{amount}\n"
            f"🆔 Order ID: <tg-spoiler>{order_id}</tg-spoiler>\n\n"
            "<blockquote>Scan with any UPI app. We auto-check every minute, or tap 'Verify Payment' manually.</blockquote>"
        )
        await wait_msg.delete()
        await message.answer_photo(res["qr_url"], caption=caption, parse_mode="HTML", reply_markup=kb.as_markup())
        await state.clear()

    @dp.callback_query(F.data.startswith("verify_upi:"))
    async def verify_upi_cb(cq: CallbackQuery):
        order_id = cq.data.split(":", 1)[1]
        tx = txns_col.find_one({"_id": order_id})
        if not tx:
            await cq.answer("Invoice not found.", show_alert=True)
            return
        if tx["status"] == "paid":
            await cq.answer("✅ Already credited!", show_alert=True)
            return

        if verify_upi_invoice(order_id):
            await _credit_and_notify(cq, tx, method_label="UPI")
        else:
            await cq.answer("⏳ Payment not received yet. Try again in a moment.", show_alert=True)

    @dp.callback_query(F.data.startswith("verify_fampay:"))
    async def verify_fampay_cb(cq: CallbackQuery):
        order_id = cq.data.split(":", 1)[1]
        tx = txns_col.find_one({"_id": order_id})
        if not tx:
            await cq.answer("Invoice not found.", show_alert=True)
            return
        if tx["status"] == "paid":
            await cq.answer("✅ Already credited!", show_alert=True)
            return

        res = verify_fampay_invoice(order_id)
        if res["success"]:
            if txns_col.find_one({"claimed_txn": res["txn"]}):
                await cq.answer("⚠️ This payment was already claimed.", show_alert=True)
                return
            txns_col.update_one({"_id": order_id}, {"$set": {"amount": res["amount"], "claimed_txn": res["txn"]}})
            tx["amount"] = res["amount"]
            await _credit_and_notify(cq, tx, method_label="FamPay", sender=res.get("sender"))
        else:
            await cq.answer("⏳ Payment not received yet. Try again in a moment.", show_alert=True)

    # ==============================
    # BINANCE (Auto)
    # ==============================
    @dp.callback_query(F.data == "recharge_binance")
    async def recharge_binance(cq: CallbackQuery, state: FSMContext):
        settings = get_settings(settings_col)
        if not settings.get("binance_enabled", True):
            await cq.answer("⚠️ Binance Pay is currently unavailable.", show_alert=True)
            return

        await cq.message.delete()
        kb = InlineKeyboardBuilder()
        kb.button(text="📋 Copy Pay ID", copy_text=CopyTextButton(text=BINANCE_PAY_ID))
        kb.button(text="✅ Verify Payment", callback_data="binance_verify_start")
        kb.button(text="🔙 Back", callback_data="recharge")
        kb.adjust(1)

        caption = (
            "🅱️ <b>Binance Pay (Auto)</b>\n\n"
            f"🆔 Pay ID: <code>{BINANCE_PAY_ID}</code>\n\n"
            "<blockquote>Send payment to this Pay ID via Binance app. Once done, tap 'Verify Payment' and send your Order ID / Transaction ID.</blockquote>"
        )
        if BINANCE_QR_URL:
            await cq.message.answer_photo(BINANCE_QR_URL, caption=caption, parse_mode="HTML", reply_markup=kb.as_markup())
        else:
            await cq.message.answer(caption, parse_mode="HTML", reply_markup=kb.as_markup())

    @dp.callback_query(F.data == "binance_verify_start")
    async def binance_verify_start(cq: CallbackQuery, state: FSMContext):
        msg = await cq.message.answer(
            "🆔 Send your Binance Pay <b>Order ID</b> or <b>Transaction ID</b>:",
            parse_mode="HTML",
            reply_markup=_cancel_kb().as_markup()
        )
        await state.update_data(last_msg=msg.message_id)
        await state.set_state(RechargeState.binance_orderid)

    @dp.message(StateFilter(RechargeState.binance_orderid))
    async def binance_orderid_entered(message: Message, state: FSMContext):
        target = message.text.strip() if message.text else ""
        await state.clear()

        if not target:
            await message.answer("❌ Invalid Order ID.", reply_markup=_cancel_kb().as_markup())
            return

        if txns_col.find_one({"_id": f"bnc_{target}"}):
            await message.answer("⚠️ This Order ID was already claimed.", reply_markup=_cancel_kb().as_markup())
            return

        wait_msg = await message.answer("⌛ Checking Binance Pay...")
        res = verify_binance_order(target)

        if not res["success"]:
            await wait_msg.edit_text(
                "❌ Payment not found. Make sure the Order ID is correct and try again in a minute.",
                reply_markup=_cancel_kb().as_markup()
            )
            return

        amount_inr = float(res["amount"]) if res.get("currency") == "INR" else round(float(res["amount"]) * USDT_TO_INR, 2)

        txns_col.insert_one({
            "_id": f"bnc_{target}",
            "user_id": message.from_user.id,
            "method": "binance",
            "amount": amount_inr,
            "status": "paid",
            "created_at": datetime.datetime.utcnow(),
            "settled_at": datetime.datetime.utcnow(),
        })
        users_col.update_one({"_id": message.from_user.id}, {"$inc": {"balance": amount_inr}}, upsert=True)
        new_balance = get_balance(message.from_user.id)

        await wait_msg.edit_text(
            f"✅ <b>Binance Pay Verified!</b>\n\n"
            f"💰 Amount Credited: ₹{amount_inr}\n"
            f"🏦 New Balance: ₹{new_balance}\n"
            f"👤 Payer: <code>{escape(str(res.get('payer_name', 'N/A')))}</code>\n"
            f"🆔 Order ID: <tg-spoiler>{target}</tg-spoiler>",
            parse_mode="HTML",
            reply_markup=_cancel_kb().as_markup()
        )

        for admin in ADMIN_IDS:
            try:
                await bot.send_message(
                    admin,
                    f"🅱️ <b>Binance Pay Received</b>\n\n"
                    f"👤 User: <code>{message.from_user.id}</code>\n"
                    f"💰 Amount: ₹{amount_inr}\n"
                    f"🏦 New Balance: ₹{new_balance}\n"
                    f"🆔 Order ID: <code>{target}</code>",
                    parse_mode="HTML"
                )
            except Exception:
                pass

    # ==============================
    # CRYPTO (OxaPay) — unchanged flow, already automatic
    # ==============================
    @dp.callback_query(F.data == "recharge_crypto")
    async def recharge_crypto(cq: CallbackQuery, state: FSMContext):
        settings = get_settings(settings_col)
        if not settings.get("crypto_enabled", True):
            await cq.answer("⚠️ Crypto payment is currently unavailable.", show_alert=True)
            return
        await cq.message.delete()
        msg = await cq.message.answer(
            f"🪙 Enter amount in USDT (Min: {MIN_USDT}):",
            reply_markup=_cancel_kb().as_markup()
        )
        await state.update_data(last_msg=msg.message_id)
        await state.set_state(RechargeState.crypto_amount)

    @dp.message(StateFilter(RechargeState.crypto_amount))
    async def crypto_amt(message: Message, state: FSMContext):
        await message.delete()
        data = await state.get_data()
        try:
            await bot.delete_message(message.chat.id, data.get("last_msg"))
        except Exception:
            pass

        try:
            usdt = float(message.text)
            if usdt < MIN_USDT:
                raise ValueError
        except Exception:
            msg = await message.answer("❌ Invalid amount.", reply_markup=_cancel_kb().as_markup())
            await state.update_data(last_msg=msg.message_id)
            return

        wait_msg = await message.answer("⌛ Generating Invoice...")
        order_id = f"Oxa_{message.from_user.id}_{int(datetime.datetime.now().timestamp())}"

        res = create_invoice(usdt, order_id)
        if not res["success"]:
            await wait_msg.edit_text(f"❌ API Error: {res['error']}", reply_markup=_cancel_kb().as_markup())
            await state.clear()
            return

        inv_data = res["data"]
        track_id = str(inv_data["track_id"])
        pay_url = inv_data["payment_url"]
        inr_val = round(usdt * USDT_TO_INR, 2)

        crypto_col.insert_one({
            "user_id": message.from_user.id,
            "track_id": track_id,
            "amount_usdt": usdt,
            "amount_inr": inr_val,
            "status": "pending",
            "created_at": datetime.datetime.utcnow(),
        })

        kb = InlineKeyboardBuilder()
        kb.button(text="💳 Pay Now", url=pay_url)
        kb.button(text="✅ I Have Paid", callback_data=f"check_crypto:{track_id}")
        kb.button(text="❌ Cancel", callback_data=f"cancel_crypto:{track_id}")
        kb.button(text="🔙 Back", callback_data="recharge")
        kb.adjust(1)

        await wait_msg.delete()
        await message.answer(
            f"📋 <b>Crypto Invoice</b>\n\n"
            f"💵 Amount: <b>{usdt} USDT</b>\n"
            f"💰 INR Value: ₹{inr_val}\n"
            f"⏳ Expires in: 30 Mins\n\n"
            f"⚠️ Send EXACT amount shown on the link.",
            parse_mode="HTML",
            reply_markup=kb.as_markup()
        )
        await state.clear()

    @dp.callback_query(F.data.startswith("check_crypto"))
    async def check_crypto(cq: CallbackQuery):
        track_id = cq.data.split(":")[1]
        inv = crypto_col.find_one({"track_id": track_id})
        if not inv:
            await cq.answer("Invoice not found.", show_alert=True)
            return
        if inv["status"] == "paid":
            await cq.answer("Already Paid!", show_alert=True)
            return

        api_res = check_invoice(track_id)
        remote_status = "unknown"
        if "data" in api_res and "status" in api_res["data"]:
            remote_status = str(api_res["data"]["status"]).lower()
        elif "status" in api_res:
            remote_status = str(api_res["status"]).lower()

        if remote_status in ["paid", "complete", "confirmed"]:
            crypto_col.update_one(
                {"track_id": track_id},
                {"$set": {"status": "paid", "settled_at": datetime.datetime.utcnow()}}
            )
            users_col.update_one({"_id": inv["user_id"]}, {"$inc": {"balance": inv["amount_inr"]}}, upsert=True)
            usdt = inv["amount_usdt"]
            inr = inv["amount_inr"]
            new_balance = get_balance(inv["user_id"])

            await cq.message.delete()
            await bot.send_message(
                inv["user_id"],
                f"✅ <b>Payment Received!</b>\n\n"
                f"💵 Amount Added: <b>{usdt} USDT</b>\n"
                f"💰 INR Value: ₹{inr}\n"
                f"🏦 New Balance: ₹{new_balance}\n"
                f"➕ Track ID - <code>{track_id}</code> ",
                parse_mode="HTML",
                reply_markup=_cancel_kb().as_markup()
            )
            admin_log_text = (
                f"🚀 <b>New Crypto Payment Received</b>\n\n"
                f"👤 <b>User:</b> {escape(cq.from_user.full_name)}\n"
                f"🆔 <b>User ID:</b> <code>{inv['user_id']}</code>\n"
                f"🔗 <b>Username:</b> @{cq.from_user.username or 'N/A'}\n"
                f"🪙 <b>Amount Added:</b> {usdt} USDT\n"
                f"🇮🇳 <b>INR Value:</b> ₹{inr}\n"
                f"🏦 <b>New Balance:</b> ₹{new_balance}\n"
                f"🧾 <b>Track ID:</b> <code>{track_id}</code>\n"
            )
            for admin_id in ADMIN_IDS:
                try:
                    await bot.send_message(admin_id, admin_log_text, parse_mode="HTML")
                except Exception as e:
                    print(f"Error sending log to admin {admin_id}: {e}")
        elif remote_status == "confirming":
            await cq.answer("⏳ Payment detected but confirming on blockchain. Please wait.", show_alert=True)
        else:
            await cq.answer(f"Status: {remote_status.capitalize()}\nPayment not confirmed yet.", show_alert=True)

    @dp.callback_query(F.data.startswith("cancel_crypto"))
    async def cancel_crypto(cq: CallbackQuery, state: FSMContext):
        await show_recharge_menu(cq.message, state, edit=True)

    # ==============================
    # RECHARGE HISTORY
    # ==============================
    @dp.callback_query(F.data == "recharge_history")
    async def recharge_history(cq: CallbackQuery, state: FSMContext):
        user_id = cq.from_user.id

        gateway_txns = list(
            txns_col.find({"user_id": user_id, "status": "paid"}).sort("settled_at", -1).limit(HISTORY_LIMIT)
        )
        crypto_txns = list(
            crypto_col.find({"user_id": user_id, "status": "paid"}).sort("settled_at", -1).limit(HISTORY_LIMIT)
        )

        combined = []
        for tx in gateway_txns:
            combined.append({
                "method": tx.get("method", "unknown").upper(),
                "amount": tx.get("amount") or 0,
                "date": tx.get("settled_at") or tx.get("created_at"),
            })
        for tx in crypto_txns:
            combined.append({
                "method": "CRYPTO",
                "amount": tx.get("amount_inr") or 0,
                "date": tx.get("settled_at") or tx.get("created_at"),
            })

        combined.sort(key=lambda x: x["date"] or datetime.datetime.min, reverse=True)
        combined = combined[:HISTORY_LIMIT]

        if not combined:
            text = "📜 <b>Recharge History</b>\n\nNo successful recharges yet."
        else:
            lines = ["📜 <b>Recharge History (Last 10)</b>\n"]
            for tx in combined:
                date_str = tx["date"].strftime("%d %b %Y, %I:%M %p") if tx["date"] else "N/A"
                lines.append(f"• <b>{tx['method']}</b> — ₹{tx['amount']}\n  <code>{date_str}</code>")
            text = "\n".join(lines)

        kb = InlineKeyboardBuilder()
        kb.button(text="🔙 Back", callback_data="recharge")
        kb.adjust(1)

        try:
            await cq.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())
        except Exception:
            await cq.message.answer(text, parse_mode="HTML", reply_markup=kb.as_markup())

    # ==============================
    # SHARED CREDIT HELPER (UPI / FamPay)
    # ==============================
    async def _credit_and_notify(cq: CallbackQuery, tx: dict, method_label: str, sender: str | None = None):
        txns_col.update_one({"_id": tx["_id"]}, {"$set": {"status": "paid", "settled_at": datetime.datetime.utcnow()}})
        users_col.update_one({"_id": tx["user_id"]}, {"$inc": {"balance": tx["amount"]}}, upsert=True)
        new_balance = get_balance(tx["user_id"])

        await cq.message.delete()
        await bot.send_message(
            tx["user_id"],
            f"✅ <b>Payment Verified!</b>\n\n"
            f"💳 Method: {method_label}\n"
            f"💰 Amount Added: ₹{tx['amount']}\n"
            f"🏦 New Balance: ₹{new_balance}\n"
            f"🆔 Order ID: <code>{tx['_id']}</code>",
            parse_mode="HTML",
            reply_markup=_cancel_kb().as_markup()
        )

        admin_text = (
            f"💳 <b>{method_label} Payment Received</b>\n\n"
            f"👤 User ID: <code>{tx['user_id']}</code>\n"
            f"💰 Amount: ₹{tx['amount']}\n"
            f"🏦 New Balance: ₹{new_balance}\n"
            f"🆔 Order ID: <code>{tx['_id']}</code>\n"
        )
        if sender:
            admin_text += f"🧾 Sender: {escape(sender)}\n"

        for admin in ADMIN_IDS:
            try:
                await bot.send_message(admin, admin_text, parse_mode="HTML")
            except Exception:
                pass

    # ==============================
    # ADMIN: PAYMENT SETTINGS PANEL
    # ==============================
    def _settings_kb(settings: dict) -> InlineKeyboardBuilder:
        kb = InlineKeyboardBuilder()
        gw = settings.get("active_inr_gateway")
        kb.button(
            text=f"💳 INR Gateway: {'UPI ✅' if gw == 'upi' else 'UPI'}",
            callback_data="pset_inr:upi"
        )
        kb.button(
            text=f"💳 INR Gateway: {'FamPay ✅' if gw == 'fampay' else 'FamPay'}",
            callback_data="pset_inr:fampay"
        )
        kb.button(text="🚫 Disable INR Gateway" if gw else "🚫 INR Gateway Disabled ✅", callback_data="pset_inr:off")
        kb.button(
            text=f"🅱️ Binance: {'ON ✅' if settings.get('binance_enabled', True) else 'OFF ❌'}",
            callback_data="pset_toggle:binance"
        )
        kb.button(
            text=f"🪙 Crypto: {'ON ✅' if settings.get('crypto_enabled', True) else 'OFF ❌'}",
            callback_data="pset_toggle:crypto"
        )
        kb.button(text="🔙 Close", callback_data="pset_close")
        kb.adjust(2, 1, 1, 1, 1)
        return kb

    @dp.message(Command("paymentsettings"))
    async def payment_settings_cmd(message: Message):
        if not is_admin(message.from_user.id):
            return
        settings = get_settings(settings_col)
        await message.answer(
            "⚙️ <b>Payment Gateway Settings</b>\n\n"
            "Only one INR gateway (UPI or FamPay) can be active at a time — this powers the \"UPI (Auto)\" button.\n"
            "Binance and Crypto can be toggled independently.",
            parse_mode="HTML",
            reply_markup=_settings_kb(settings).as_markup()
        )

    @dp.callback_query(F.data.startswith("pset_inr:"))
    async def pset_inr(cq: CallbackQuery):
        if not is_admin(cq.from_user.id):
            await cq.answer("Not authorized.", show_alert=True)
            return
        choice = cq.data.split(":", 1)[1]
        set_inr_gateway(settings_col, None if choice == "off" else choice)
        settings = get_settings(settings_col)
        await cq.message.edit_reply_markup(reply_markup=_settings_kb(settings).as_markup())
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
        settings = get_settings(settings_col)
        await cq.message.edit_reply_markup(reply_markup=_settings_kb(settings).as_markup())
        await cq.answer("Updated.")

    @dp.callback_query(F.data == "pset_close")
    async def pset_close(cq: CallbackQuery):
        await cq.message.delete()

    # ==============================
    # BACKGROUND AUTO-SWEEPER (checks pending UPI/FamPay invoices every 60s)
    # ==============================
    async def _auto_sweep_loop():
        while True:
            try:
                now = datetime.datetime.utcnow()
                pending = list(txns_col.find({
                    "status": "pending",
                    "method": {"$in": ["upi", "fampay"]},
                    "expires_at": {"$gt": now},
                }))

                for tx in pending:
                    if tx["method"] == "upi":
                        if verify_upi_invoice(tx["_id"]):
                            txns_col.update_one({"_id": tx["_id"]}, {"$set": {"status": "paid", "settled_at": now}})
                            users_col.update_one({"_id": tx["user_id"]}, {"$inc": {"balance": tx["amount"]}}, upsert=True)
                            new_balance = get_balance(tx["user_id"])
                            await bot.send_message(
                                tx["user_id"],
                                f"✅ <b>Auto-Verified!</b>\n\n💳 Method: UPI\n💰 Amount Added: ₹{tx['amount']}\n"
                                f"🏦 New Balance: ₹{new_balance}\n🆔 Order ID: <code>{tx['_id']}</code>",
                                parse_mode="HTML"
                            )
                            for admin in ADMIN_IDS:
                                try:
                                    await bot.send_message(
                                        admin,
                                        f"💳 <b>UPI Payment Received (Auto-Sweep)</b>\n\n"
                                        f"👤 User ID: <code>{tx['user_id']}</code>\n💰 Amount: ₹{tx['amount']}\n"
                                        f"🏦 New Balance: ₹{new_balance}\n🆔 Order ID: <code>{tx['_id']}</code>",
                                        parse_mode="HTML"
                                    )
                                except Exception:
                                    pass
                    else:  # fampay
                        res = verify_fampay_invoice(tx["_id"])
                        if res["success"] and not txns_col.find_one({"claimed_txn": res["txn"]}):
                            txns_col.update_one(
                                {"_id": tx["_id"]},
                                {"$set": {"status": "paid", "amount": res["amount"], "claimed_txn": res["txn"], "settled_at": now}}
                            )
                            users_col.update_one({"_id": tx["user_id"]}, {"$inc": {"balance": res["amount"]}}, upsert=True)
                            new_balance = get_balance(tx["user_id"])
                            await bot.send_message(
                                tx["user_id"],
                                f"✅ <b>Auto-Verified!</b>\n\n💳 Method: FamPay\n💰 Amount Added: ₹{res['amount']}\n"
                                f"🏦 New Balance: ₹{new_balance}\n🆔 Order ID: <code>{tx['_id']}</code>",
                                parse_mode="HTML"
                            )
                            for admin in ADMIN_IDS:
                                try:
                                    await bot.send_message(
                                        admin,
                                        f"💳 <b>FamPay Payment Received (Auto-Sweep)</b>\n\n"
                                        f"👤 User ID: <code>{tx['user_id']}</code>\n💰 Amount: ₹{res['amount']}\n"
                                        f"🏦 New Balance: ₹{new_balance}\n🆔 Order ID: <code>{tx['_id']}</code>",
                                        parse_mode="HTML"
                                    )
                                except Exception:
                                    pass
            except Exception as e:
                print(f"[auto_sweep] error: {e}")

            await asyncio.sleep(AUTO_SWEEP_INTERVAL_SEC)

    def start_auto_sweeper():
        return asyncio.create_task(_auto_sweep_loop())

    return start_auto_sweeper
