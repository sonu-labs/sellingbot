import asyncio
import aiohttp
from html import escape
from datetime import datetime
from aiogram import F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from bson import ObjectId

# ================= LEGACY DEFAULT PANEL (used only to seed the first panel on first run) =================
DEFAULT_PANEL_URL = "https://cheapestsmmpanels.com/api/v2"
DEFAULT_PANEL_KEY = "063d056b89487f11cd72ec908ecfce4b"
BOT_USERNAME = "TGAStoreBot"  # <-- Confirm this matches your actual bot username


# ================= FSM STATES =================
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


# ================= CORE API CALLER (now panel-aware) =================
async def smm_api_call(panel: dict, action: str, **params):
    """Every SMM panel API call goes through here. Returns (success, data_or_error, raw_text)."""
    payload = {"key": panel["api_key"], "action": action}
    payload.update(params)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(panel["api_url"], data=payload, timeout=15) as resp:
                raw_text = await resp.text()
                try:
                    data = await resp.json(content_type=None)
                except Exception:
                    return False, {"error": "Invalid JSON response"}, raw_text
                if isinstance(data, dict) and "error" in data:
                    return False, data, raw_text
                return True, data, raw_text
    except Exception as e:
        return False, {"error": str(e)}, f"Exception: {e}"


def status_emoji(status: str) -> str:
    s = (status or "").lower()
    if "complet" in s: return "✅"
    if "partial" in s: return "⚠️"
    if "progress" in s or "process" in s or "pending" in s: return "🔄"
    if "cancel" in s: return "❌"
    return "❔"


def mask_link(link: str) -> str:
    """Shows only the domain publicly, hides the full path."""
    try:
        domain = link.split("//")[-1].split("/")[0]
        return f"https://{domain}/••••••••"
    except Exception:
        return "••••••••"


# ================= MAIN REGISTER FUNCTION =================
def register_smm_handlers(dp, bot, db, users_col, admin_ids, admin_log_id, public_log_id):
    smm_platforms = db["smm_platforms"]
    smm_services = db["smm_services"]
    smm_orders = db["smm_orders"]
    smm_settings = db["smm_settings"]
    smm_panels = db["smm_panels"]

    # ---- One-time migration: seed a default panel from the old hardcoded key, and
    #      backfill panel_id on any pre-existing services that don't have one yet ----
    def ensure_default_panel():
        if smm_panels.count_documents({}) == 0 and DEFAULT_PANEL_KEY and "YOUR_" not in DEFAULT_PANEL_KEY:
            result = smm_panels.insert_one({
                "name": "Panel 1",
                "api_url": DEFAULT_PANEL_URL,
                "api_key": DEFAULT_PANEL_KEY,
                "added_at": datetime.utcnow()
            })
            smm_services.update_many({"panel_id": {"$exists": False}}, {"$set": {"panel_id": result.inserted_id}})

    ensure_default_panel()

    def is_admin(uid: int) -> bool:
        return uid in admin_ids

    def get_config() -> dict:
        return smm_settings.find_one({"_id": "config"}) or {}

    def public_log_enabled() -> bool:
        return get_config().get("public_log_enabled", True)

    def smm_enabled() -> bool:
        return get_config().get("smm_enabled", True)

    async def log_admin(text: str, raw: str = None):
        """Only for USER-side actions (orders, status checks). Admin actions are never logged here."""
        full = text
        if raw:
            raw_clip = escape(raw[:2500])
            full += f"\n\n📡 <b>Raw API Response</b>\n<pre>{raw_clip}</pre>"
        try:
            await bot.send_message(admin_log_id, full[:4090], parse_mode="HTML")
        except Exception:
            pass

    async def log_public(text: str, with_order_button=True):
        if not public_log_enabled():
            return
        kb = None
        if with_order_button:
            b = InlineKeyboardBuilder()
            b.button(text="• Buy Now •", url=f"https://t.me/{BOT_USERNAME}?start=smm")
            kb = b.as_markup()
        try:
            await bot.send_message(public_log_id, text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            pass

    def get_balance_inr(uid: int) -> float:
        u = users_col.find_one({"_id": uid})
        return u.get("balance", 0.0) if u else 0.0

    def get_panel_for_service(svc: dict):
        pid = svc.get("panel_id")
        if not pid:
            return smm_panels.find_one({})  # fallback to any panel for legacy services
        return smm_panels.find_one({"_id": pid})

    def build_smm_menu_text_and_kb(user_id: int):
        if not smm_enabled():
            kb = InlineKeyboardBuilder()
            kb.row(InlineKeyboardButton(text="❌ Close", callback_data="delete_msg"))
            return "<b>🌐 Social Media Services</b>\n\n<blockquote>🚧 This service is temporarily unavailable. Please check back later.</blockquote>", kb.as_markup()

        platforms = list(smm_platforms.find({}))
        balance = get_balance_inr(user_id)
        kb = InlineKeyboardBuilder()
        header = (
            "<b>🌐 Social Media Services Panel</b>\n\n"
            "<blockquote>✨ Fastest delivery, best prices in the market\n"
            "👇 Choose your platform to get started</blockquote>\n"
            f"• <b>User ID:</b> <code>{user_id}</code>\n"
            f"• <b>Balance:</b> ₹{balance:.2f}"
        )
        if not platforms:
            text = header + "\n\n<blockquote>😔 No platforms available right now.</blockquote>"
        else:
            text = header
            for p in platforms:
                kb.button(text=f"{p.get('emoji','📱')} {p['name']}", callback_data=f"smm_plat:{p['_id']}")
            kb.adjust(2)
        kb.row(
            InlineKeyboardButton(text="📊 Check Order Status", callback_data="smm_status_menu"),
            InlineKeyboardButton(text="📜 History", callback_data="smm_history")
        )
        kb.row(InlineKeyboardButton(text="❌ Close", callback_data="delete_msg"))
        return text, kb.as_markup()

    async def smm_send_menu(chat_id):
        """Used for the /start?start=smm deep-link redirect from the public log button."""
        text, markup = build_smm_menu_text_and_kb(chat_id)
        try:
            await bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=markup)
        except Exception:
            pass

    # ============================================================
    # =====================  USER SIDE  ==========================
    # ============================================================

    @dp.callback_query(F.data == "smm_open")
    async def smm_open(cq: CallbackQuery, state: FSMContext):
        await state.clear()
        text, markup = build_smm_menu_text_and_kb(cq.from_user.id)
        await cq.message.edit_text(text, parse_mode="HTML", reply_markup=markup)
        await cq.answer()

    @dp.callback_query(F.data.startswith("smm_plat:"))
    async def smm_platform_services(cq: CallbackQuery, state: FSMContext):
        if not smm_enabled():
            return await cq.answer("🚧 This service is temporarily unavailable.", show_alert=True)
        platform_id = cq.data.split(":")[1]
        platform = smm_platforms.find_one({"_id": ObjectId(platform_id)})
        if not platform:
            return await cq.answer("❌ Platform not found.", show_alert=True)

        services = list(smm_services.find({"platform_id": ObjectId(platform_id)}))
        kb = InlineKeyboardBuilder()
        if not services:
            text = f"<b>{platform.get('emoji','📱')} {platform['name']}</b>\n\n<blockquote>😔 No services available for this platform yet.</blockquote>"
        else:
            text = f"<b>{platform.get('emoji','📱')} {platform['name']} — Services</b>\n\n<blockquote>👇 Select a service to continue</blockquote>"
            for s in services:
                kb.button(
                    text=f"{s['name']} — 💰₹{s['price_per_1000']:.2f}/1000",
                    callback_data=f"smm_svc:{s['_id']}"
                )
            kb.adjust(1)
        kb.row(InlineKeyboardButton(text="🔙 Back", callback_data="smm_open"))
        await cq.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())
        await cq.answer()

    @dp.callback_query(F.data.startswith("smm_svc:"))
    async def smm_service_selected(cq: CallbackQuery, state: FSMContext):
        service_id = cq.data.split(":")[1]
        svc = smm_services.find_one({"_id": ObjectId(service_id)})
        if not svc:
            return await cq.answer("❌ Service not found.", show_alert=True)

        await state.update_data(service_id=service_id)
        await state.set_state(SMMUser.waiting_quantity)

        text = (
            f"<b>{svc['name']}</b>\n\n"
            "<blockquote>"
            f"💰 <b>Rate:</b> ₹{svc['price_per_1000']:.2f} per 1000\n"
            f"📉 <b>Min:</b> {svc['min']}   📈 <b>Max:</b> {svc['max']}"
            "</blockquote>\n"
            "🔢 <b>Enter the quantity you need:</b>"
        )
        kb = InlineKeyboardBuilder()
        kb.button(text="❌ Cancel", callback_data="smm_open")
        await cq.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())
        await cq.answer()

    @dp.message(SMMUser.waiting_quantity)
    async def smm_qty_received(msg: Message, state: FSMContext):
        if not msg.text or not msg.text.strip().isdigit():
            return await msg.answer("❌ Please send a valid number. Try again:")
        qty = int(msg.text.strip())

        data = await state.get_data()
        svc = smm_services.find_one({"_id": ObjectId(data["service_id"])})
        if not svc:
            await state.clear()
            return await msg.answer("❌ This service is no longer available.")

        if qty < svc["min"] or qty > svc["max"]:
            return await msg.answer(f"❌ Quantity must be between {svc['min']} and {svc['max']}. Try again:")

        await state.update_data(quantity=qty)
        await state.set_state(SMMUser.waiting_link)

        text = (
            "<b>🔗 Send Your Link</b>\n\n"
            "<blockquote>📌 Paste the post/profile/video link where the service should be delivered</blockquote>"
        )
        kb = InlineKeyboardBuilder()
        kb.button(text="❌ Cancel", callback_data="smm_open")
        await msg.answer(text, parse_mode="HTML", reply_markup=kb.as_markup())

    @dp.message(SMMUser.waiting_link)
    async def smm_link_received(msg: Message, state: FSMContext):
        link = (msg.text or "").strip()
        if not link.startswith("http"):
            return await msg.answer("❌ Please send a valid link (must start with http/https). Try again:")

        data = await state.get_data()
        svc = smm_services.find_one({"_id": ObjectId(data["service_id"])})
        platform = smm_platforms.find_one({"_id": svc["platform_id"]})
        qty = data["quantity"]
        price = round((svc["price_per_1000"] / 1000) * qty, 2)

        await state.update_data(link=link, price=price)
        await state.set_state(None)

        text = (
            "<b>🧾 Order Confirmation</b>\n\n"
            "<blockquote>"
            f"🔹 <b>Platform:</b> {platform.get('emoji','📱')} {platform['name']}\n"
            f"🎯 <b>Service:</b> {svc['name']}\n"
            f"🔢 <b>Quantity:</b> {qty}\n"
            f"🔗 <b>Link:</b> {link}\n"
            f"💰 <b>Total Price:</b> ₹{price:.2f}"
            "</blockquote>\n"
            "✅ Confirm this order?"
        )
        kb = InlineKeyboardBuilder()
        kb.row(
            InlineKeyboardButton(text="✅ Confirm", callback_data="smm_confirm"),
            InlineKeyboardButton(text="❌ Cancel", callback_data="smm_open")
        )
        await msg.answer(text, parse_mode="HTML", reply_markup=kb.as_markup())

    @dp.callback_query(F.data == "smm_confirm")
    async def smm_confirm_order(cq: CallbackQuery, state: FSMContext):
        data = await state.get_data()
        uid = cq.from_user.id
        svc = smm_services.find_one({"_id": ObjectId(data["service_id"])})
        platform = smm_platforms.find_one({"_id": svc["platform_id"]})
        panel = get_panel_for_service(svc)
        qty = data["quantity"]
        link = data["link"]
        price = data["price"]

        if not panel:
            await state.clear()
            return await cq.message.edit_text("❌ <b>No panel configured for this service.</b> Contact admin.", parse_mode="HTML")

        balance = get_balance_inr(uid)
        if balance < price:
            await state.clear()
            kb = InlineKeyboardBuilder()
            kb.button(text="💳 Recharge", callback_data="recharge")
            kb.button(text="🔙 Back", callback_data="smm_open")
            kb.adjust(1)
            return await cq.message.edit_text(
                (
                    "<b>❌ Insufficient Balance!</b>\n\n"
                    "<blockquote>"
                    f"💰 <b>Required:</b> ₹{price:.2f}\n"
                    f"💳 <b>Available:</b> ₹{balance:.2f}"
                    "</blockquote>"
                ),
                parse_mode="HTML", reply_markup=kb.as_markup()
            )

        success, resp, raw = await smm_api_call(panel, "add", service=svc["provider_service_id"], link=link, quantity=qty)

        if not success or "order" not in resp:
            await log_admin(
                "⚠️ <b>SMM Order Failed</b>\n\n"
                f"👤 <b>User:</b> <code>{uid}</code>\n"
                f"🔹 <b>Platform:</b> {platform['name']}\n"
                f"🎯 <b>Service:</b> {svc['name']}\n"
                f"🔢 <b>Quantity:</b> {qty}\n"
                f"🔗 <b>Link:</b> {link}",
                raw
            )
            await state.clear()
            kb = InlineKeyboardBuilder()
            kb.button(text="🔙 Back", callback_data="smm_open")
            return await cq.message.edit_text(
                "<b>❌ Order placement failed.</b> Please contact support.",
                parse_mode="HTML", reply_markup=kb.as_markup()
            )

        provider_order_id = resp["order"]
        users_col.update_one({"_id": uid}, {"$inc": {"balance": -price}})
        new_balance = get_balance_inr(uid)

        order_doc = {
            "user_id": uid,
            "platform_name": platform["name"],
            "platform_emoji": platform.get("emoji", "📱"),
            "service_name": svc["name"],
            "provider_order_id": provider_order_id,
            "panel_id": panel["_id"],
            "quantity": qty,
            "link": link,
            "price": price,
            "status": "Pending",
            "refunded": False,
            "created_at": datetime.utcnow(),
            "last_checked": None
        }
        result = smm_orders.insert_one(order_doc)
        await state.clear()

        text = (
            "<b>✅ Order Placed Successfully</b>\n\n"
            "<blockquote>"
            f"🆔 <b>Order ID:</b> <code>{result.inserted_id}</code>\n"
            f"🎯 <b>Service:</b> {svc['name']}\n"
            f"🔢 <b>Quantity:</b> {qty}\n"
            f"💰 <b>Charged:</b> ₹{price:.2f}\n"
            f"💳 <b>Remaining Balance:</b> ₹{new_balance:.2f}"
            "</blockquote>\n"
            "⏳ Status is auto-checked periodically. You'll be notified once it's complete."
        )
        kb = InlineKeyboardBuilder()
        kb.button(text="🔙 Back to Menu", callback_data="smm_open")
        await cq.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())

        await log_admin(
            "🆕 <b>New SMM Order Placed</b>\n\n"
            f"👤 <b>User:</b> <code>{uid}</code> (@{cq.from_user.username or 'NoUsername'})\n"
            f"🏢 <b>Panel:</b> {panel['name']}\n"
            f"🔹 <b>Platform:</b> {platform['name']}\n"
            f"🎯 <b>Service:</b> {svc['name']}\n"
            f"🔢 <b>Quantity:</b> {qty}\n"
            f"💰 <b>Price:</b> ₹{price:.2f}\n"
            f"🆔 <b>Provider Order ID:</b> {provider_order_id}\n"
            f"🔗 <b>Link:</b> {link}",
            raw
        )

        await log_public(
            f"<pre>✅ New SMM Order Placed Successfully</pre>\n\n"
            f"➖ <b>Platform:</b> {platform['name']}\n"
            f"➖ <b>Service:</b> {svc['name']}\n\n"
            f"➕ <b>Quantity:</b> {qty}\n"
            f"➕ <b>Price:</b> ₹{price:.2f}\n"
            f"➕ <b>Link:</b> {mask_link(link)}\n\n"
            f"<b>• @{BOT_USERNAME} || @TechnoWorldOfficial</b>"
        )

    # ---------------- ORDER STATUS / HISTORY ----------------

    @dp.callback_query(F.data == "smm_status_menu")
    async def smm_status_menu(cq: CallbackQuery, state: FSMContext):
        await state.set_state(SMMUser.waiting_status_order_id)
        kb = InlineKeyboardBuilder()
        kb.button(text="🔙 Back", callback_data="smm_open")
        await cq.message.edit_text(
            "<b>📊 Check Order Status</b>\n\n<blockquote>🆔 Send your Order ID</blockquote>",
            parse_mode="HTML", reply_markup=kb.as_markup()
        )
        await cq.answer()

    @dp.message(SMMUser.waiting_status_order_id)
    async def smm_status_check(msg: Message, state: FSMContext):
        await state.clear()
        try:
            order = smm_orders.find_one({"_id": ObjectId(msg.text.strip()), "user_id": msg.from_user.id})
        except Exception:
            order = None
        if not order:
            return await msg.answer("❌ Order not found. Please check your Order ID.")

        panel = smm_panels.find_one({"_id": order.get("panel_id")}) or smm_panels.find_one({})
        success, resp, raw = await smm_api_call(panel, "status", order=order["provider_order_id"])
        await log_admin(f"🔍 <b>Status Check</b>\n\n👤 <b>User:</b> <code>{msg.from_user.id}</code>\n🆔 <b>Order:</b> <code>{order['_id']}</code>", raw)

        if not success:
            return await msg.answer("❌ Could not fetch status right now, try again later.")

        status = resp.get("status", "Unknown")
        smm_orders.update_one({"_id": order["_id"]}, {"$set": {"status": status, "last_checked": datetime.utcnow()}})

        text = (
            f"<b>{status_emoji(status)} Order Status</b>\n\n"
            "<blockquote>"
            f"🎯 <b>Service:</b> {order['service_name']}\n"
            f"📊 <b>Status:</b> {status}\n"
            f"🔢 <b>Start Count:</b> {resp.get('start_count','N/A')}\n"
            f"⏳ <b>Remains:</b> {resp.get('remains','N/A')}\n"
            f"💰 <b>Charge:</b> ₹{order['price']:.2f}"
            "</blockquote>"
        )
        await msg.answer(text, parse_mode="HTML")

    @dp.callback_query(F.data == "smm_history")
    async def smm_history(cq: CallbackQuery, state: FSMContext):
        orders = list(smm_orders.find({"user_id": cq.from_user.id}).sort("created_at", -1).limit(10))
        kb = InlineKeyboardBuilder()
        kb.button(text="🔙 Back", callback_data="smm_open")
        if not orders:
            return await cq.message.edit_text(
                "<b>📜 Order History</b>\n\n<blockquote>😔 No orders found yet.</blockquote>",
                parse_mode="HTML", reply_markup=kb.as_markup()
            )
        lines = ["<b>📜 Your Last 10 Orders</b>"]
        for o in orders:
            date_str = o.get("created_at").strftime("%d %b, %H:%M") if o.get("created_at") else "N/A"
            lines.append(
                "<blockquote>"
                f"{status_emoji(o['status'])} <b>{o.get('platform_emoji','📱')} {o['service_name']}</b>\n"
                f"🆔 <code>{o['_id']}</code>\n"
                f"🔢 <b>Qty:</b> {o['quantity']}   💰 <b>₹{o['price']:.2f}</b>\n"
                f"📊 <b>Status:</b> {o['status']}   🕒 {date_str}"
                "</blockquote>"
            )
        await cq.message.edit_text("\n\n".join(lines), parse_mode="HTML", reply_markup=kb.as_markup())
        await cq.answer()

    # ============================================================
    # =====================  ADMIN SIDE  ==========================
    # ============================================================

    @dp.message(Command("smmadmin"))
    async def smmadmin_menu(msg: Message):
        if not is_admin(msg.from_user.id):
            return await msg.answer("❌ Not authorized.")
        pub_status = "🟢 ON" if public_log_enabled() else "🔴 OFF"
        svc_status = "🟢 ON" if smm_enabled() else "🔴 OFF"
        kb = InlineKeyboardBuilder()
        kb.button(text="➕ Add Platform", callback_data="smma_addplat")
        kb.button(text="📂 Manage Platforms", callback_data="smma_listplat")
        kb.button(text="🏢 Manage Panels", callback_data="smma_listpanels")
        kb.button(text="➕ Add Panel", callback_data="smma_addpanel")
        kb.button(text=f"📢 Public Log: {pub_status}", callback_data="smma_togglepublic")
        kb.button(text=f"🌐 Social Media Services: {svc_status}", callback_data="smma_togglesmm")
        kb.button(text="📊 Overall History", callback_data="smma_allorders")
        kb.button(text="🔍 Check Any Order Status", callback_data="smma_checkorder")
        kb.adjust(1)
        await msg.answer(
            "<b>🛠️ SMM Panel — Admin Control</b>",
            parse_mode="HTML", reply_markup=kb.as_markup()
        )

    @dp.callback_query(F.data == "smma_togglepublic")
    async def smma_togglepublic(cq: CallbackQuery):
        if not is_admin(cq.from_user.id): return
        current = public_log_enabled()
        smm_settings.update_one({"_id": "config"}, {"$set": {"public_log_enabled": not current}}, upsert=True)
        new_state = "ENABLED 🟢" if not current else "DISABLED 🔴"
        await cq.answer(f"Public logging is now {new_state}", show_alert=True)
        await smmadmin_menu(cq.message)

    @dp.callback_query(F.data == "smma_togglesmm")
    async def smma_togglesmm(cq: CallbackQuery):
        if not is_admin(cq.from_user.id): return
        current = smm_enabled()
        smm_settings.update_one({"_id": "config"}, {"$set": {"smm_enabled": not current}}, upsert=True)
        new_state = "ENABLED 🟢" if not current else "DISABLED 🔴"
        await cq.answer(f"Social Media Services are now {new_state}", show_alert=True)
        await smmadmin_menu(cq.message)

    # ---------------- Panel Management ----------------

    @dp.callback_query(F.data == "smma_addpanel")
    async def smma_addpanel(cq: CallbackQuery, state: FSMContext):
        if not is_admin(cq.from_user.id): return
        await state.set_state(SMMAdmin.waiting_panel_name)
        await cq.message.edit_text("📝 Send a name for this panel (e.g. Panel 2 - Backup):")
        await cq.answer()

    @dp.message(SMMAdmin.waiting_panel_name)
    async def smma_panelname(msg: Message, state: FSMContext):
        await state.update_data(panel_name=msg.text.strip())
        await state.set_state(SMMAdmin.waiting_panel_url)
        await msg.answer("🔗 Send the panel's API URL (e.g. https://panelsite.com/api/v2):")

    @dp.message(SMMAdmin.waiting_panel_url)
    async def smma_panelurl(msg: Message, state: FSMContext):
        await state.update_data(panel_url=msg.text.strip())
        await state.set_state(SMMAdmin.waiting_panel_key)
        await msg.answer("🔑 Send the panel's API key:")

    @dp.message(SMMAdmin.waiting_panel_key)
    async def smma_panelkey(msg: Message, state: FSMContext):
        data = await state.get_data()
        panel = {
            "name": data["panel_name"],
            "api_url": data["panel_url"],
            "api_key": msg.text.strip(),
            "added_at": datetime.utcnow()
        }
        # Verify it works before saving
        success, resp, raw = await smm_api_call(panel, "balance")
        smm_panels.insert_one(panel)
        await state.clear()
        try: await msg.delete()
        except Exception: pass
        if success:
            await msg.answer(f"✅ Panel <b>{panel['name']}</b> added and verified! Balance: {resp.get('balance')} {resp.get('currency')}", parse_mode="HTML")
        else:
            await msg.answer(f"⚠️ Panel <b>{panel['name']}</b> added, but balance check failed — double check the URL/key.", parse_mode="HTML")

    @dp.callback_query(F.data == "smma_listpanels")
    async def smma_listpanels(cq: CallbackQuery):
        if not is_admin(cq.from_user.id): return
        panels = list(smm_panels.find({}))
        kb = InlineKeyboardBuilder()
        if not panels:
            kb.row(InlineKeyboardButton(text="🔙 Back", callback_data="smma_back"))
            return await cq.message.edit_text("📭 No panels added yet.", reply_markup=kb.as_markup())
        for p in panels:
            kb.button(text=f"🏢 {p['name']}", callback_data=f"smma_panel:{p['_id']}")
        kb.adjust(1)
        kb.row(InlineKeyboardButton(text="🔙 Back", callback_data="smma_back"))
        await cq.message.edit_text("<b>🏢 Manage Panels</b>", parse_mode="HTML", reply_markup=kb.as_markup())
        await cq.answer()

    @dp.callback_query(F.data.startswith("smma_panel:"))
    async def smma_panel_manage(cq: CallbackQuery):
        if not is_admin(cq.from_user.id): return
        pid = cq.data.split(":")[1]
        panel = smm_panels.find_one({"_id": ObjectId(pid)})
        if not panel: return await cq.answer("Panel not found.", show_alert=True)
        svc_count = smm_services.count_documents({"panel_id": ObjectId(pid)})

        kb = InlineKeyboardBuilder()
        kb.button(text="💰 Check Balance", callback_data=f"smma_panelbal:{pid}")
        kb.button(text="🗑️ Remove Panel", callback_data=f"smma_rmpanel:{pid}")
        kb.adjust(1)
        kb.row(InlineKeyboardButton(text="🔙 Back", callback_data="smma_listpanels"))
        text = (
            f"<b>🏢 {panel['name']}</b>\n\n<blockquote>"
            f"🔗 <b>URL:</b> <code>{panel['api_url']}</code>\n"
            f"📦 <b>Services using this panel:</b> {svc_count}"
            "</blockquote>"
        )
        await cq.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())
        await cq.answer()

    @dp.callback_query(F.data.startswith("smma_panelbal:"))
    async def smma_panelbal(cq: CallbackQuery):
        if not is_admin(cq.from_user.id): return
        pid = cq.data.split(":")[1]
        panel = smm_panels.find_one({"_id": ObjectId(pid)})
        if not panel: return await cq.answer("Panel not found.", show_alert=True)
        success, resp, raw = await smm_api_call(panel, "balance")
        if success:
            await cq.answer(f"💰 {panel['name']}: {resp.get('balance')} {resp.get('currency')}", show_alert=True)
        else:
            await cq.answer("❌ Failed to fetch balance for this panel.", show_alert=True)

    @dp.callback_query(F.data.startswith("smma_rmpanel:"))
    async def smma_rmpanel(cq: CallbackQuery):
        if not is_admin(cq.from_user.id): return
        pid = cq.data.split(":")[1]
        linked = smm_services.count_documents({"panel_id": ObjectId(pid)})
        if linked > 0:
            return await cq.answer(f"❌ Cannot remove — {linked} service(s) still use this panel. Remove those services first.", show_alert=True)
        smm_panels.delete_one({"_id": ObjectId(pid)})
        await cq.answer("✅ Panel removed.", show_alert=True)
        await smma_listpanels(cq)

    @dp.callback_query(F.data == "smma_balance")
    async def smma_balance(cq: CallbackQuery):
        # Legacy single-button balance check — now redirects to panel list
        await smma_listpanels(cq)

    # ---------------- Platform Management ----------------

    @dp.callback_query(F.data == "smma_addplat")
    async def smma_addplat(cq: CallbackQuery, state: FSMContext):
        if not is_admin(cq.from_user.id): return
        await state.set_state(SMMAdmin.waiting_platform_name)
        await cq.message.edit_text("📝 Send the platform name (e.g. Instagram):")
        await cq.answer()

    @dp.message(SMMAdmin.waiting_platform_name)
    async def smma_platname(msg: Message, state: FSMContext):
        await state.update_data(platform_name=msg.text.strip())
        await state.set_state(SMMAdmin.waiting_platform_emoji)
        await msg.answer("😀 Send an emoji for this platform (e.g. 📸):")

    @dp.message(SMMAdmin.waiting_platform_emoji)
    async def smma_platemoji(msg: Message, state: FSMContext):
        data = await state.get_data()
        smm_platforms.insert_one({"name": data["platform_name"], "emoji": msg.text.strip()})
        await state.clear()
        await msg.answer(f"✅ Platform <b>{data['platform_name']}</b> added!", parse_mode="HTML")

    @dp.callback_query(F.data == "smma_listplat")
    async def smma_listplat(cq: CallbackQuery):
        if not is_admin(cq.from_user.id): return
        platforms = list(smm_platforms.find({}))
        kb = InlineKeyboardBuilder()
        for p in platforms:
            kb.button(text=f"{p.get('emoji','📱')} {p['name']}", callback_data=f"smma_plat:{p['_id']}")
        kb.adjust(1)
        kb.row(InlineKeyboardButton(text="🔙 Back", callback_data="smma_back"))
        await cq.message.edit_text("<b>📂 Manage Platforms</b>", parse_mode="HTML", reply_markup=kb.as_markup())
        await cq.answer()

    @dp.callback_query(F.data == "smma_back")
    async def smma_back(cq: CallbackQuery):
        await smmadmin_menu(cq.message)
        await cq.answer()

    @dp.callback_query(F.data.startswith("smma_plat:"))
    async def smma_plat_manage(cq: CallbackQuery):
        if not is_admin(cq.from_user.id): return
        pid = cq.data.split(":")[1]
        platform = smm_platforms.find_one({"_id": ObjectId(pid)})
        services = list(smm_services.find({"platform_id": ObjectId(pid)}))

        kb = InlineKeyboardBuilder()
        kb.button(text="➕ Add Service", callback_data=f"smma_addsvc:{pid}")
        for s in services:
            kb.button(text=f"⚙️ {s['name']} (₹{s['price_per_1000']})", callback_data=f"smma_svc:{s['_id']}")
        kb.button(text="🗑️ Remove Platform", callback_data=f"smma_rmplat:{pid}")
        kb.adjust(1)
        kb.row(InlineKeyboardButton(text="🔙 Back", callback_data="smma_listplat"))

        text = f"<b>{platform.get('emoji','📱')} {platform['name']}</b>\n\n<blockquote>📦 Services: {len(services)}</blockquote>"
        await cq.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())
        await cq.answer()

    @dp.callback_query(F.data.startswith("smma_rmplat:"))
    async def smma_rmplat(cq: CallbackQuery):
        if not is_admin(cq.from_user.id): return
        pid = cq.data.split(":")[1]
        smm_services.delete_many({"platform_id": ObjectId(pid)})
        smm_platforms.delete_one({"_id": ObjectId(pid)})
        await cq.answer("✅ Platform removed with all its services.", show_alert=True)
        await smma_listplat(cq)

    # ---------------- Service Management (now panel-aware) ----------------

    @dp.callback_query(F.data.startswith("smma_addsvc:"))
    async def smma_addsvc(cq: CallbackQuery, state: FSMContext):
        if not is_admin(cq.from_user.id): return
        pid = cq.data.split(":")[1]
        await state.update_data(platform_id=pid)

        panels = list(smm_panels.find({}))
        if not panels:
            return await cq.answer("❌ No panels configured. Add a panel first via 'Manage Panels'.", show_alert=True)

        if len(panels) == 1:
            await state.update_data(panel_id=str(panels[0]["_id"]))
            await state.set_state(SMMAdmin.waiting_service_provider_id)
            await cq.message.edit_text(f"🏢 Panel: <b>{panels[0]['name']}</b>\n\n🆔 Send the Provider's Service ID:", parse_mode="HTML")
            await cq.answer()
            return

        kb = InlineKeyboardBuilder()
        for p in panels:
            kb.button(text=f"🏢 {p['name']}", callback_data=f"smma_addsvc_panel:{p['_id']}")
        kb.adjust(1)
        await cq.message.edit_text("🏢 <b>Select which panel this service belongs to:</b>", parse_mode="HTML", reply_markup=kb.as_markup())
        await cq.answer()

    @dp.callback_query(F.data.startswith("smma_addsvc_panel:"))
    async def smma_addsvc_pickpanel(cq: CallbackQuery, state: FSMContext):
        if not is_admin(cq.from_user.id): return
        pid = cq.data.split(":")[1]
        panel = smm_panels.find_one({"_id": ObjectId(pid)})
        await state.update_data(panel_id=pid)
        await state.set_state(SMMAdmin.waiting_service_provider_id)
        await cq.message.edit_text(f"🏢 Panel: <b>{panel['name']}</b>\n\n🆔 Send the Provider's Service ID:", parse_mode="HTML")
        await cq.answer()

    @dp.message(SMMAdmin.waiting_service_provider_id)
    async def smma_svc_provider_id(msg: Message, state: FSMContext):
        if not msg.text.strip().isdigit():
            return await msg.answer("❌ Send a valid number. Try again:")
        await state.update_data(provider_service_id=int(msg.text.strip()))
        await state.set_state(SMMAdmin.waiting_service_name)
        await msg.answer("📝 Send the display name (e.g. Instagram Followers):")

    @dp.message(SMMAdmin.waiting_service_name)
    async def smma_svc_name(msg: Message, state: FSMContext):
        await state.update_data(service_name=msg.text.strip())
        await state.set_state(SMMAdmin.waiting_service_price)
        await msg.answer("💰 Send the price per 1000 (in INR, e.g. 10):")

    @dp.message(SMMAdmin.waiting_service_price)
    async def smma_svc_price(msg: Message, state: FSMContext):
        try:
            price = float(msg.text.strip())
        except ValueError:
            return await msg.answer("❌ Send a valid number. Try again:")
        await state.update_data(price=price)
        await state.set_state(SMMAdmin.waiting_service_min)
        await msg.answer("📉 Send the minimum quantity:")

    @dp.message(SMMAdmin.waiting_service_min)
    async def smma_svc_min(msg: Message, state: FSMContext):
        if not msg.text.strip().isdigit():
            return await msg.answer("❌ Send a valid number. Try again:")
        await state.update_data(min_qty=int(msg.text.strip()))
        await state.set_state(SMMAdmin.waiting_service_max)
        await msg.answer("📈 Send the maximum quantity:")

    @dp.message(SMMAdmin.waiting_service_max)
    async def smma_svc_max(msg: Message, state: FSMContext):
        if not msg.text.strip().isdigit():
            return await msg.answer("❌ Send a valid number. Try again:")
        data = await state.get_data()
        max_qty = int(msg.text.strip())

        smm_services.insert_one({
            "platform_id": ObjectId(data["platform_id"]),
            "panel_id": ObjectId(data["panel_id"]),
            "provider_service_id": data["provider_service_id"],
            "name": data["service_name"],
            "price_per_1000": data["price"],
            "min": data["min_qty"],
            "max": max_qty
        })
        await state.clear()
        await msg.answer(f"✅ Service <b>{data['service_name']}</b> added!", parse_mode="HTML")

    @dp.callback_query(F.data.startswith("smma_svc:"))
    async def smma_svc_manage(cq: CallbackQuery):
        if not is_admin(cq.from_user.id): return
        sid = cq.data.split(":")[1]
        svc = smm_services.find_one({"_id": ObjectId(sid)})
        panel = smm_panels.find_one({"_id": svc.get("panel_id")})
        kb = InlineKeyboardBuilder()
        kb.button(text="✏️ Edit Price", callback_data=f"smma_editprice:{sid}")
        kb.button(text="🗑️ Remove Service", callback_data=f"smma_rmsvc:{sid}")
        kb.adjust(1)
        kb.row(InlineKeyboardButton(text="🔙 Back", callback_data=f"smma_plat:{svc['platform_id']}"))
        text = (
            f"<b>⚙️ {svc['name']}</b>\n\n"
            "<blockquote>"
            f"🏢 <b>Panel:</b> {panel['name'] if panel else 'Unknown'}\n"
            f"🆔 <b>Provider ID:</b> {svc['provider_service_id']}\n"
            f"💰 <b>Price:</b> ₹{svc['price_per_1000']}/1000\n"
            f"📉 <b>Min:</b> {svc['min']} &nbsp; 📈 <b>Max:</b> {svc['max']}"
            "</blockquote>"
        )
        await cq.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())
        await cq.answer()

    @dp.callback_query(F.data.startswith("smma_rmsvc:"))
    async def smma_rmsvc(cq: CallbackQuery):
        if not is_admin(cq.from_user.id): return
        sid = cq.data.split(":")[1]
        svc = smm_services.find_one({"_id": ObjectId(sid)})
        smm_services.delete_one({"_id": ObjectId(sid)})
        await cq.answer("✅ Service removed.", show_alert=True)
        cq.data = f"smma_plat:{svc['platform_id']}"
        await smma_plat_manage(cq)

    @dp.callback_query(F.data.startswith("smma_editprice:"))
    async def smma_editprice(cq: CallbackQuery, state: FSMContext):
        if not is_admin(cq.from_user.id): return
        sid = cq.data.split(":")[1]
        await state.update_data(edit_service_id=sid)
        await state.set_state(SMMAdmin.waiting_edit_price)
        await cq.message.edit_text("💰 Send the new price per 1000 (INR):")
        await cq.answer()

    @dp.message(SMMAdmin.waiting_edit_price)
    async def smma_editprice_save(msg: Message, state: FSMContext):
        try:
            price = float(msg.text.strip())
        except ValueError:
            return await msg.answer("❌ Send a valid number. Try again:")
        data = await state.get_data()
        smm_services.update_one({"_id": ObjectId(data["edit_service_id"])}, {"$set": {"price_per_1000": price}})
        await state.clear()
        await msg.answer(f"✅ Price updated: ₹{price}/1000")

    # ---------------- Admin: Overall History & Any-Order Status ----------------

    @dp.callback_query(F.data == "smma_allorders")
    async def smma_allorders(cq: CallbackQuery):
        if not is_admin(cq.from_user.id): return
        orders = list(smm_orders.find({}).sort("created_at", -1).limit(20))
        kb = InlineKeyboardBuilder()
        kb.row(InlineKeyboardButton(text="🔙 Back", callback_data="smma_back"))
        if not orders:
            return await cq.message.edit_text("<b>📊 Overall History</b>\n\n<blockquote>😔 No orders yet.</blockquote>", parse_mode="HTML", reply_markup=kb.as_markup())

        lines = ["<b>📊 Last 20 Orders (All Users)</b>"]
        for o in orders:
            date_str = o.get("created_at").strftime("%d %b, %H:%M") if o.get("created_at") else "N/A"
            lines.append(
                "<blockquote>"
                f"{status_emoji(o['status'])} <b>{o.get('platform_emoji','📱')} {o['service_name']}</b>\n"
                f"👤 <code>{o['user_id']}</code>  🆔 <code>{o['_id']}</code>\n"
                f"🔢 {o['quantity']}   💰 ₹{o['price']:.2f}   📊 {o['status']}   🕒 {date_str}"
                "</blockquote>"
            )
        await cq.message.edit_text("\n\n".join(lines), parse_mode="HTML", reply_markup=kb.as_markup())
        await cq.answer()

    @dp.callback_query(F.data == "smma_checkorder")
    async def smma_checkorder(cq: CallbackQuery, state: FSMContext):
        if not is_admin(cq.from_user.id): return
        await state.set_state(SMMAdmin.waiting_admin_order_id)
        await cq.message.edit_text("🔍 <b>Send the Order ID to check</b> (any user's order):", parse_mode="HTML")
        await cq.answer()

    @dp.message(SMMAdmin.waiting_admin_order_id)
    async def smma_checkorder_result(msg: Message, state: FSMContext):
        if not is_admin(msg.from_user.id): return
        await state.clear()
        try:
            order = smm_orders.find_one({"_id": ObjectId(msg.text.strip())})
        except Exception:
            order = None
        if not order:
            return await msg.answer("❌ Order not found.")

        panel = smm_panels.find_one({"_id": order.get("panel_id")}) or smm_panels.find_one({})
        success, resp, raw = await smm_api_call(panel, "status", order=order["provider_order_id"])

        if not success:
            return await msg.answer(f"❌ Could not fetch live status.\n\n👤 User: <code>{order['user_id']}</code>\n📊 Last known status: {order['status']}", parse_mode="HTML")

        status = resp.get("status", "Unknown")
        smm_orders.update_one({"_id": order["_id"]}, {"$set": {"status": status, "last_checked": datetime.utcnow()}})

        text = (
            f"<b>{status_emoji(status)} Order Status (Admin View)</b>\n\n"
            "<blockquote>"
            f"👤 <b>User:</b> <code>{order['user_id']}</code>\n"
            f"🎯 <b>Service:</b> {order['service_name']}\n"
            f"📊 <b>Status:</b> {status}\n"
            f"🔢 <b>Start Count:</b> {resp.get('start_count','N/A')}\n"
            f"⏳ <b>Remains:</b> {resp.get('remains','N/A')}\n"
            f"💰 <b>Charge:</b> ₹{order['price']:.2f}\n"
            f"🔄 <b>Refunded:</b> {'Yes' if order.get('refunded') else 'No'}"
            "</blockquote>"
        )
        await msg.answer(text, parse_mode="HTML")

    # ============================================================
    # ================  AUTO STATUS CHECKER + REFUND  =============
    # ============================================================

    async def refund_order(order):
        users_col.update_one({"_id": order["user_id"]}, {"$inc": {"balance": order["price"]}})
        smm_orders.update_one({"_id": order["_id"]}, {"$set": {"refunded": True}})
        try:
            await bot.send_message(
                order["user_id"],
                (
                    "<b>💸 Order Cancelled — Refund Issued</b>\n\n"
                    "<blockquote>"
                    f"🎯 <b>Service:</b> {order['service_name']}\n"
                    f"🆔 <b>Order ID:</b> <code>{order['_id']}</code>\n"
                    f"💰 <b>Refunded:</b> ₹{order['price']:.2f}"
                    "</blockquote>\n"
                    "The provider cancelled this order, so your balance has been restored automatically."
                ),
                parse_mode="HTML"
            )
        except Exception:
            pass
        await log_admin(
            "💸 <b>Auto-Refund Issued</b>\n\n"
            f"👤 <b>User:</b> <code>{order['user_id']}</code>\n"
            f"🆔 <b>Order:</b> <code>{order['_id']}</code>\n"
            f"💰 <b>Refunded:</b> ₹{order['price']:.2f}"
        )

    async def smm_background_checker():
        """Checks all active orders every 10 minutes, notifies on completion, and auto-refunds cancellations."""
        while True:
            await asyncio.sleep(600)  # 10 minutes
            active_orders = list(smm_orders.find({"status": {"$nin": ["Completed", "Canceled", "Error"]}}))
            for order in active_orders:
                panel = smm_panels.find_one({"_id": order.get("panel_id")}) or smm_panels.find_one({})
                if not panel:
                    continue
                success, resp, raw = await smm_api_call(panel, "status", order=order["provider_order_id"])
                if not success:
                    continue
                new_status = resp.get("status", order["status"])
                smm_orders.update_one(
                    {"_id": order["_id"]},
                    {"$set": {"status": new_status, "last_checked": datetime.utcnow()}}
                )
                if new_status == order["status"]:
                    continue

                await log_admin(
                    "🔄 <b>Order Status Updated</b>\n\n"
                    f"👤 <b>User:</b> <code>{order['user_id']}</code>\n"
                    f"🆔 <b>Order:</b> <code>{order['_id']}</code>\n"
                    f"📊 <b>New Status:</b> {new_status}",
                    raw
                )

                if "complet" in new_status.lower():
                    try:
                        await bot.send_message(
                            order["user_id"],
                            (
                                "<b>✅ Order Completed</b>\n\n"
                                "<blockquote>"
                                f"🎯 <b>Service:</b> {order['service_name']}\n"
                                f"🔢 <b>Quantity:</b> {order['quantity']}\n"
                                f"🆔 <b>Order ID:</b> <code>{order['_id']}</code>"
                                "</blockquote>\n"
                                "🎉 Thank you for using our service!"
                            ),
                            parse_mode="HTML"
                        )
                    except Exception:
                        pass

                elif "cancel" in new_status.lower() and not order.get("refunded"):
                    await refund_order(order)

    return smm_background_checker, smm_send_menu
