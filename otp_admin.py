"""Admin panel for the "Buy OTPs (All Apps)" TemporaSMS integration.

Entry points: the **📲 OTP Services** button in the Admin Control Center
and the ``/otpadmin`` command.  Every callback and every FSM message
re-checks admin authorization; nothing here trusts the origin of a button.

Safety invariants:

- The full TemporaSMS API key is never rendered, logged or audited — only
  a masked form (last 4 chars) and its source (database/environment).
- Key entry is private-chat only and the key message is deleted
  best-effort immediately, mirroring the SMM panel key flow.
- Destructive actions (remove key, provider-cancel an order, manual
  refund) require an explicit single-use confirmation token; manual
  reconciliation/refund additionally requires a written reason.
- Money movement reuses the storefront's locked, idempotent
  debit/refund ledger helpers — this module never edits balances itself.
- Ambiguous ``allocating``/``review`` orders are never auto-refunded or
  blind-retried from here; an admin either attaches the provider
  activation found in the provider account, or refunds with a reason
  after confirming no paid allocation exists.
- Every mutating action writes a secret-free record to
  ``otp_admin_audit`` (and to the admin log chat when configured).
"""

import asyncio
import logging
from datetime import datetime, timedelta
from html import escape

from aiogram import F
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup, Message
from bson import ObjectId
from bson.errors import InvalidId

import otp_settings as cfg
from otp import ProviderError, TemporaClient, catalog_items
from rich_messages import premium_button, safe_edit_or_send

log = logging.getLogger(__name__)

CONFIRM_TTL = timedelta(minutes=5)
ORDERS_PER_PAGE = 8
PICKER_PER_PAGE = 8

FILTERS = {
    "a": ("All", {}),
    "w": ("Waiting", {"status": "waiting"}),
    "c": ("Completed", {"status": "completed"}),
    "x": ("Cancelled/Refunded", {"status": "cancelled"}),
    "u": ("Unpaid", {"status": "unpaid"}),
    "l": ("Allocating", {"status": "allocating"}),
    "r": ("Review", {"status": {"$in": ["review", "refunding"]}}),
}
STATUS_ICON = {
    "waiting": "⏳", "completed": "✅", "cancelled": "❌", "unpaid": "💤",
    "allocating": "🔄", "review": "⚠️", "refunding": "💸",
}

_PROVIDER_FAILURES = {
    "BAD_KEY": "Provider rejected the API key (BAD_KEY).",
    "USER_BANNED": "Provider account is banned/suspended (USER_BANNED).",
    "NOT_CONFIGURED": "No API key is configured yet.",
    "CONNECTION": "Could not reach the provider (connection/timeout). Check network and retry.",
    "BAD_RESPONSE": "Provider returned an unexpected response.",
    "NO_BALANCE": "Provider account balance is empty (NO_BALANCE).",
    "TOO_MANY_REQUESTS": "Provider rate limit hit (TOO_MANY_REQUESTS). Wait and retry.",
}


def friendly_provider_error(exc: ProviderError) -> str:
    return _PROVIDER_FAILURES.get(exc.code or "", str(exc) or "Provider request failed.")


class OtpAdminStates(StatesGroup):
    waiting_api_key = State()
    waiting_markup = State()
    waiting_reason = State()       # manual refund / admin note (action in FSM data)
    waiting_activation = State()   # attach provider activation to an order


def register_otp_admin_handlers(*, dp, bot, db, users_col, settings_col, admin_ids,
                                env_api_key, env_markup, rate, otp_ctx, admin_log_id=None):
    api = otp_ctx["api"]
    orders = db["otp_orders"]
    audit_col = db["otp_admin_audit"]
    aviews = db["otp_admin_views"]
    locks = otp_ctx["locks"]

    def is_admin(uid) -> bool:
        return uid in admin_ids

    def eff():
        return cfg.effective_settings(settings_col, env_api_key, env_markup, rate)

    def settings():
        return cfg.get_settings(settings_col)

    # ------------------------------------------------------------------
    # Audit (secret-free by construction + defensive scrubbing)
    # ------------------------------------------------------------------
    _SCRUB = {"api_key", "key", "tempora_api_key", "otp", "number", "phone", "activation_id"}

    async def audit(admin_id, action, **fields):
        safe = {k: v for k, v in fields.items() if k not in _SCRUB}
        record = {"ts": datetime.utcnow(), "admin_id": admin_id, "action": action, **safe}
        try:
            audit_col.insert_one(record)
        except Exception:
            log.error("OTP admin audit write failed (details suppressed)")
        if admin_log_id:
            detail = " · ".join(f"{k}={v}" for k, v in safe.items())
            try:
                await bot.send_message(
                    admin_log_id,
                    f"📲 <b>OTP Admin</b> · <code>{escape(action)}</code>\n"
                    f"Admin: <code>{admin_id}</code>" + (f"\n{escape(detail)}" if detail else ""),
                )
            except Exception:
                pass

    # ------------------------------------------------------------------
    # UI helpers
    # ------------------------------------------------------------------
    def btn(text, data):
        return premium_button(text=text, callback_data=data)

    def kb(rows):
        return InlineKeyboardMarkup(inline_keyboard=rows)

    async def show(target_message, text, rows):
        await safe_edit_or_send(bot, target_message, text, reply_markup=kb(rows))

    def back_home():
        return [btn("⬅️ OTP Admin Home", "otpa:home")]

    async def delete_quietly(message):
        try:
            await message.delete()
        except Exception:
            pass

    async def deny(cq: CallbackQuery):
        try:
            await cq.answer("Access denied", show_alert=True)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Confirmation tokens (single-use, bound to the requesting admin)
    # ------------------------------------------------------------------
    def make_confirm(admin_id, action, **payload):
        try:
            aviews.create_index("created_at", expireAfterSeconds=86400)
        except Exception:
            pass
        doc = {"kind": "confirm", "admin_id": admin_id, "action": action,
               "payload": payload, "created_at": datetime.utcnow()}
        return str(aviews.insert_one(doc).inserted_id)

    def take_confirm(token, admin_id):
        try:
            oid = ObjectId(token)
        except (InvalidId, TypeError):
            return None
        doc = aviews.find_one({"_id": oid, "kind": "confirm"})
        if not doc:
            return None
        aviews.delete_one({"_id": oid})  # single-use either way
        if doc.get("admin_id") != admin_id:
            return None
        if datetime.utcnow() - doc["created_at"] > CONFIRM_TTL:
            return None
        return doc

    # ------------------------------------------------------------------
    # Screens
    # ------------------------------------------------------------------
    def key_status_lines(e):
        if e["key_source"] == "database":
            source = "Database (admin panel)"
        elif e["key_source"] == "environment":
            source = "Environment TEMPORASMS_API_KEY"
        else:
            source = "Not configured"
        return source, cfg.mask_key(e["api_key"])

    async def render_home(message):
        e = eff()
        source, masked = key_status_lines(e)
        counts = {s: orders.count_documents({"status": s})
                  for s in ("waiting", "allocating", "review", "refunding")}
        attention = counts["allocating"] + counts["review"] + counts["refunding"]
        text = (
            "<h2>📲 OTP Services — Admin</h2>"
            "<blockquote>"
            f"• <b>Service:</b> <code>{'ON ✅' if e['enabled'] else 'OFF ❌'}</code><br>"
            f"• <b>API Key:</b> <code>{escape(masked)}</code> ({escape(source)})<br>"
            f"• <b>Markup:</b> <code>{e['markup']:g}%</code> ({escape(e['markup_source'])})<br>"
            f"• <b>USD→INR:</b> <code>₹{e['rate']:g}</code> (global fixed rate)<br>"
            f"• <b>Operators enabled:</b> <code>{', '.join(e['operators']) or 'none'}</code><br>"
            f"• <b>Waiting orders:</b> <code>{counts['waiting']}</code> · "
            f"<b>Need attention:</b> <code>{attention}</code>"
            "</blockquote>"
            "<p>Changes apply instantly — no restart needed. Existing orders keep "
            "working (OTP check / cancel / refund) even when the service is OFF.</p>"
        )
        rows = [
            [btn("🔑 API Key", "otpa:key"), btn("⚙️ Settings", "otpa:settings")],
            [btn("🛠 Provider Tools", "otpa:tools"), btn("📦 OTP Orders", "otpa:orders")],
            [btn("⭐ Popular & Hidden Apps", "otpa:pop")],
            [btn("⬅️ Admin Control Center", "admin_refresh")],
        ]
        await show(message, text, rows)

    async def render_key(message):
        e = eff()
        source, masked = key_status_lines(e)
        db_set = bool((settings() or {}).get("tempora_api_key"))
        text = (
            "<h2>🔑 TemporaSMS API Key</h2>"
            "<blockquote>"
            f"• <b>Active key:</b> <code>{escape(masked)}</code><br>"
            f"• <b>Source:</b> <code>{escape(source)}</code><br>"
            f"• <b>Environment fallback:</b> <code>{'Set (' + escape(cfg.mask_key(env_api_key)) + ')' if env_api_key else 'Not set'}</code>"
            "</blockquote>"
            "<p>A database key always wins over the environment key and applies "
            "without a restart. Keys are stored in MongoDB following the bot's "
            "existing provider-key pattern (same as SMM panels) — they are "
            "<b>not encrypted at rest by the bot</b>, so protect database access. "
            "Only the masked form above is ever shown.</p>"
        )
        rows = [[btn("✏️ Set / Update Key", "otpa:keyset")]]
        if db_set:
            rows.append([btn("🗑 Remove Database Key", "otpa:keyrm")])
        rows += [
            [btn("🧪 Test Connection", "otpa:test"), btn("💰 Provider Balance", "otpa:bal")],
            back_home(),
        ]
        await show(message, text, rows)

    async def render_settings(message):
        e = eff()
        raw = e["raw"]
        disabled = [op for op in cfg.ALL_OPERATORS if op not in e["operators"]]
        text = (
            "<h2>⚙️ OTP Service Settings</h2>"
            "<blockquote>"
            f"• <b>Service:</b> <code>{'ON ✅' if e['enabled'] else 'OFF ❌'}</code><br>"
            f"• <b>Markup (OTP_MARKUP_PERCENT):</b> <code>{e['markup']:g}%</code> "
            f"via <code>{escape(e['markup_source'])}</code>"
            + (f" · env fallback: <code>{env_markup:g}%</code>" if e["markup_source"] == "database" else "") + "<br>"
            f"• <b>USD→INR rate:</b> <code>₹{e['rate']:g}</code> — the bot-wide fixed "
            "rate from config; it is not changed here.<br>"
            f"• <b>Operators OFF:</b> <code>{', '.join(disabled) or 'none'}</code><br>"
            f"• <b>Hidden apps:</b> <code>{len(cfg.hidden_apps(raw))}</code> · "
            f"<b>Popular (custom):</b> <code>{len(cfg.popular_apps(raw))}</code>"
            "</blockquote>"
            "<p>Turning the service or an operator OFF blocks <b>new</b> purchases "
            "only. Price changes invalidate outstanding quotes, so buyers always "
            "re-confirm the current price.</p>"
        )
        rows = [
            [btn(f"{'🔴 Switch Service OFF' if e['enabled'] else '🟢 Switch Service ON'}", "otpa:togsvc")],
            [btn(f"📈 Markup: {e['markup']:g}% — Change", "otpa:markup")],
        ]
        if e["markup_source"] == "database":
            rows.append([btn("♻️ Reset Markup to Environment", "otpa:markupreset")])
        rows.append([btn("📡 Operators", "otpa:ops"), btn("⭐ Popular & Hidden", "otpa:pop")])
        rows.append(back_home())
        await show(message, text, rows)

    async def render_operators(message):
        e = eff()
        rows = []
        for i in range(0, len(cfg.ALL_OPERATORS), 2):
            rows.append([
                btn(f"{'✅' if op in e['operators'] else '❌'} Operator {op}", f"otpa:op:{op}")
                for op in cfg.ALL_OPERATORS[i:i + 2]
            ])
        rows.append([btn("⬅️ Settings", "otpa:settings")])
        rows.append(back_home())
        await show(message,
                   "<h2>📡 Operators</h2><p>Tap to enable/disable. Disabled operators "
                   "disappear from new purchases immediately; existing orders are unaffected.</p>",
                   rows)

    async def render_tools(message, note=None):
        e = eff()
        source, masked = key_status_lines(e)
        text = (
            "<h2>🛠 Provider Tools</h2>"
            f"<blockquote>Active key: <code>{escape(masked)}</code> ({escape(source)})</blockquote>"
            + (f"<p>{note}</p>" if note else
               "<p>Test the key, check the provider balance, or refresh the "
               "services/countries catalog cache (normally cached for 5 minutes).</p>")
        )
        rows = [
            [btn("🧪 Test Connection / Validate Key", "otpa:test")],
            [btn("💰 Provider Balance", "otpa:bal")],
            [btn("🔄 Refresh Catalog Cache", "otpa:refreshcat")],
            back_home(),
        ]
        await show(message, text, rows)

    # ------------------------- orders --------------------------------
    async def render_orders_home(message):
        rows = []
        line = []
        for code, (label, query) in FILTERS.items():
            line.append(btn(f"{label} ({orders.count_documents(query)})", f"otpa:ords:{code}:0"))
            if len(line) == 2:
                rows.append(line); line = []
        if line:
            rows.append(line)
        rows.append(back_home())
        await show(message, "<h2>📦 OTP Orders</h2><p>Choose a filter. "
                            "<b>Review</b> holds orders needing reconciliation.</p>", rows)

    def _fmt_dt(value):
        return value.strftime("%d %b %Y %H:%M UTC") if isinstance(value, datetime) else "—"

    async def render_orders_list(message, fcode, page):
        label, query = FILTERS[fcode]
        total = orders.count_documents(query)
        pages = max(1, (total + ORDERS_PER_PAGE - 1) // ORDERS_PER_PAGE)
        page = max(0, min(page, pages - 1))
        docs = list(orders.find(query).sort("created_at", -1)
                    .skip(page * ORDERS_PER_PAGE).limit(ORDERS_PER_PAGE))
        rows = []
        for o in docs:
            icon = STATUS_ICON.get(o.get("status"), "•")
            created = o["created_at"].strftime("%d %b %H:%M") if isinstance(o.get("created_at"), datetime) else ""
            rows.append([btn(
                f"{icon} {str(o.get('service_name', '?'))[:16]} · ₹{float(o.get('price', 0)):.2f} · {created}",
                f"otpa:ord:{o['_id']}:{fcode}:{page}")])
        nav = []
        if page:
            nav.append(btn("‹ Previous", f"otpa:ords:{fcode}:{page - 1}"))
        if page + 1 < pages:
            nav.append(btn("Next ›", f"otpa:ords:{fcode}:{page + 1}"))
        if nav:
            rows.append(nav)
        rows.append([btn("⬅️ Order Filters", "otpa:orders")])
        text = (f"<h2>📦 {escape(label)} Orders</h2>"
                f"<p>{total} order(s) · Page {page + 1}/{pages}. "
                "Numbers/OTPs are visible only inside an order's detail screen.</p>")
        await show(message, text, rows)

    async def render_order_detail(message, order, fcode="a", page=0, note=None):
        oid = str(order["_id"])
        lines = [
            f"<h2>📲 OTP Order <code>{oid}</code></h2>",
            "<blockquote>",
            f"• <b>Status:</b> <code>{escape(str(order.get('status')))}</code>"
            + (" · <b>Refunded ✅</b>" if order.get("refunded") else "") + "<br>",
            f"• <b>User ID:</b> <code>{order.get('user_id')}</code><br>",
            f"• <b>App:</b> {escape(str(order.get('service_name')))} (<code>{escape(str(order.get('service')))}</code>)<br>",
            f"• <b>Country:</b> {escape(str(order.get('country_name')))} (<code>{escape(str(order.get('country')))}</code>) · "
            f"<b>Operator:</b> <code>{escape(str(order.get('operator')))}</code><br>",
            f"• <b>Sell price:</b> <code>₹{float(order.get('price', 0)):.2f}</code> · "
            f"<b>Provider cost:</b> <code>${float(order.get('cost', 0))}</code>"
            + (f" · <b>Markup:</b> <code>{float(order['markup']):g}%</code>" if order.get("markup") is not None else "") + "<br>",
            f"• <b>Activation ID:</b> <code>{escape(str(order.get('activation_id') or '—'))}</code><br>",
            f"• <b>Created:</b> {_fmt_dt(order.get('created_at'))}<br>",
            f"• <b>Updated:</b> {_fmt_dt(order.get('updated_at') or order.get('created_at'))}",
        ]
        if order.get("number"):
            lines.append(f"<br>• <b>Number:</b> <code>{escape(str(order['number']))}</code>")
        if order.get("otp"):
            lines.append(f"<br>• <b>OTP:</b> <code>{escape(str(order['otp']))}</code>")
        if order.get("review_reason"):
            lines.append(f"<br>• <b>Review reason:</b> <code>{escape(str(order['review_reason']))}</code>")
        if order.get("refund_note"):
            lines.append(f"<br>• <b>Refund note:</b> {escape(str(order['refund_note']))}")
        lines.append("</blockquote>")
        notes = order.get("admin_notes") or []
        if notes:
            lines.append("<blockquote><b>Admin notes</b><br>" + "<br>".join(
                f"• {escape(str(n.get('text', '')))[:200]} — <code>{n.get('admin_id')}</code>"
                for n in notes[-3:]) + "</blockquote>")
        if note:
            lines.append(f"<p>{note}</p>")
        rows = []
        if order.get("number"):
            rows.append([InlineKeyboardButton(text="📋 Copy Number", copy_text=CopyTextButton(text=str(order["number"])))])
        if order.get("otp"):
            rows.append([InlineKeyboardButton(text="📋 Copy OTP", copy_text=CopyTextButton(text=str(order["otp"])[:256]))])
        status = order.get("status")
        if status == "waiting":
            rows.append([btn("🔄 Refresh Provider Status", f"otpa:oref:{oid}")])
            rows.append([btn("❌ Cancel via Provider & Refund", f"otpa:ocancel:{oid}")])
        elif status == "refunding":
            rows.append([btn("💸 Complete Pending Refund", f"otpa:oref:{oid}")])
        elif status in ("allocating", "review"):
            if order.get("activation_id"):
                rows.append([btn("🔄 Refresh Provider Status", f"otpa:oref:{oid}")])
            rows.append([btn("🔗 Attach Provider Activation", f"otpa:oattach:{oid}")])
            rows.append([btn("💸 Refund — No Allocation Found", f"otpa:orefund:{oid}")])
        if status not in ("cancelled", "completed", "unpaid"):
            rows.append([btn("📝 Add Admin Note", f"otpa:onote:{oid}")])
        rows.append([btn("⬅️ Orders", f"otpa:ords:{fcode}:{page}"), btn("📦 Filters", "otpa:orders")])
        await show(message, "".join(lines), rows)

    def load_order(raw_id):
        try:
            return orders.find_one({"_id": ObjectId(raw_id)})
        except (InvalidId, TypeError):
            return None

    # --------------------- popular / hidden --------------------------
    async def render_popular(message):
        raw = settings()
        popular = cfg.popular_apps(raw)
        hidden = cfg.hidden_apps(raw)
        if popular:
            listing = "<br>".join(f"{i + 1}. {escape(e['name'])}" for i, e in enumerate(popular))
        else:
            listing = ("<i>Built-in defaults in use:</i> " +
                       ", ".join(escape(n.title()) for n in cfg.DEFAULT_POPULAR_NAMES))
        text = ("<h2>⭐ Popular &amp; Hidden Apps</h2>"
                f"<blockquote><b>Popular order</b><br>{listing}</blockquote>"
                f"<p>Hidden apps: <code>{len(hidden)}</code>. Popular apps appear first "
                "in the user catalog; hidden apps are removed from it.</p>")
        rows = []
        for i, entry in enumerate(popular):
            row = []
            if i > 0:
                row.append(btn("⬆️", f"otpa:popmv:{i}:u"))
            if i < len(popular) - 1:
                row.append(btn("⬇️", f"otpa:popmv:{i}:d"))
            row.append(btn(f"❌ {entry['name'][:28]}", f"otpa:poprm:{i}"))
            rows.append(row)
        rows.append([btn("➕ Add / Hide Apps from Catalog", "otpa:popadd")])
        if popular:
            rows.append([btn("♻️ Reset Popular to Built-in Defaults", "otpa:popreset")])
        if hidden:
            rows.append([btn(f"🚫 Hidden Apps ({len(hidden)})", "otpa:hidden")])
        rows.append([btn("⬅️ Settings", "otpa:settings")])
        rows.append(back_home())
        await show(message, text, rows)

    async def render_hidden(message):
        hidden = cfg.hidden_apps(settings())
        rows = [[btn(f"👁 Unhide {e['name'][:40]}", f"otpa:unhide:{i}")] for i, e in enumerate(hidden)]
        rows.append([btn("⬅️ Popular & Hidden", "otpa:pop")])
        await show(message, "<h2>🚫 Hidden Apps</h2><p>Tap an app to make it visible again.</p>", rows)

    async def render_picker(message, token, page):
        try:
            doc = aviews.find_one({"_id": ObjectId(token), "kind": "picker"})
        except (InvalidId, TypeError):
            doc = None
        if not doc:
            raise ProviderError("Catalog snapshot expired. Reopen the app picker.")
        raw = settings()
        pop_codes = {e["code"] for e in cfg.popular_apps(raw)}
        hid_codes = cfg.hidden_codes(raw)
        items = doc["items"]
        pages = max(1, (len(items) + PICKER_PER_PAGE - 1) // PICKER_PER_PAGE)
        page = max(0, min(page, pages - 1))
        rows = []
        for i in range(page * PICKER_PER_PAGE, min(len(items), (page + 1) * PICKER_PER_PAGE)):
            code, name = items[i]
            rows.append([
                btn(f"{'⭐' if code in pop_codes else '➕'} {name[:34]}", f"otpa:pickadd:{token}:{i}"),
                btn("🚫" if code in hid_codes else "👁", f"otpa:pickvis:{token}:{i}"),
            ])
        nav = []
        if page:
            nav.append(btn("‹ Previous", f"otpa:pick:{token}:{page - 1}"))
        if page + 1 < pages:
            nav.append(btn("Next ›", f"otpa:pick:{token}:{page + 1}"))
        if nav:
            rows.append(nav)
        rows.append([btn("✅ Done", "otpa:pop")])
        text = (f"<h2>📲 App Catalog</h2><p>{len(items)} apps · Page {page + 1}/{pages}. "
                "⭐/➕ toggles Popular · 👁 visible, 🚫 hidden.</p>")
        await show(message, text, rows)

    # ------------------------------------------------------------------
    # Command + callback router (authorization on EVERY entry)
    # ------------------------------------------------------------------
    @dp.message(Command("otpadmin"))
    async def otpadmin_cmd(message: Message, state: FSMContext):
        if not is_admin(message.from_user.id):
            return await message.answer("⛔ Access denied.")
        await state.clear()
        await render_home(message)

    @dp.callback_query(F.data.startswith("otpa:"))
    async def otp_admin_router(cq: CallbackQuery, state: FSMContext):
        if not is_admin(cq.from_user.id):
            return await deny(cq)
        try:
            await cq.answer()
        except Exception:
            pass
        # Any panel navigation safely exits a pending FSM input; actions
        # that start an FSM set their state after this point.
        await state.clear()
        try:
            await dispatch(cq, state)
        except ProviderError as exc:
            await show(cq.message, f"⚠️ {escape(friendly_provider_error(exc))}",
                       [[btn("⬅️ Provider Tools", "otpa:tools")], back_home()])
        except Exception:
            log.error("OTP admin action failed (details suppressed)")
            await show(cq.message, "⚠️ Something went wrong. No changes were forced; please retry.",
                       [back_home()])

    async def dispatch(cq: CallbackQuery, state: FSMContext):
        parts = cq.data.split(":")
        action = parts[1]
        admin_id = cq.from_user.id
        msg = cq.message

        if action == "home":
            return await render_home(msg)
        if action == "key":
            return await render_key(msg)
        if action == "settings":
            return await render_settings(msg)
        if action == "ops":
            return await render_operators(msg)
        if action == "tools":
            return await render_tools(msg)
        if action == "orders":
            return await render_orders_home(msg)
        if action == "pop":
            return await render_popular(msg)
        if action == "hidden":
            return await render_hidden(msg)

        # ------------------------- settings -------------------------
        if action == "togsvc":
            new_val = not eff()["enabled"]
            cfg.set_enabled(settings_col, new_val, admin_id)
            await audit(admin_id, "service_toggled", enabled=new_val)
            return await render_settings(msg)
        if action == "op":
            op = parts[2]
            enabled_now = op in eff()["operators"]
            cfg.set_operator_enabled(settings_col, op, not enabled_now, admin_id)
            await audit(admin_id, "operator_toggled", operator=op, enabled=not enabled_now)
            return await render_operators(msg)
        if action == "markup":
            e = eff()
            prompt = await msg.answer(
                "📈 <b>Set OTP Markup %</b>\n\n"
                f"Current: <code>{e['markup']:g}%</code> (via {escape(e['markup_source'])}) · "
                f"Rate: ₹{e['rate']:g}/USD\n\n"
                "Send the new markup percentage (e.g. <code>25</code>). "
                "Must be a finite number ≥ 0. Outstanding quotes will be invalidated.",
                reply_markup=kb([[btn("✖️ Cancel", "otpa:settings")]]))
            await state.update_data(prompt_id=prompt.message_id)
            await state.set_state(OtpAdminStates.waiting_markup)
            return
        if action == "markupreset":
            cfg.clear_markup(settings_col, admin_id)
            await audit(admin_id, "markup_cleared", env_markup=env_markup)
            return await render_settings(msg)

        # ------------------------- API key --------------------------
        if action == "keyset":
            prompt = await msg.answer(
                "🔑 <b>Set TemporaSMS API Key</b>\n\n"
                "Send the new API key as a single message <b>in this private chat</b>. "
                "Your message will be deleted immediately and the key is only ever "
                "shown masked. It will be validated against the provider before saving "
                "whenever possible.",
                reply_markup=kb([[btn("✖️ Cancel", "otpa:key")]]))
            await state.update_data(prompt_id=prompt.message_id)
            await state.set_state(OtpAdminStates.waiting_api_key)
            return
        if action == "keyrm":
            fallback = (f"The environment key <code>{escape(cfg.mask_key(env_api_key))}</code> "
                        "will become active immediately." if env_api_key else
                        "⚠️ <b>No environment fallback is set</b> — OTP purchases will stop "
                        "until a new key is configured. Existing orders can still be viewed, "
                        "but provider checks/cancels will fail.")
            token = make_confirm(admin_id, "remove_key")
            return await show(msg,
                              "<h2>🗑 Remove Database API Key?</h2>"
                              f"<p>{fallback}</p><p>This takes effect instantly, without a restart.</p>",
                              [[btn("✅ Confirm Remove", f"otpa:cf:{token}"), btn("✖️ Keep Key", "otpa:key")]])

        # ------------------------- provider tools -------------------
        if action in ("test", "bal"):
            e = eff()
            try:
                balance = await api.get_balance()
            except ProviderError as exc:
                await audit(admin_id, "connection_test", ok=False, code=exc.code or "UNKNOWN")
                return await render_tools(msg, note=f"❌ <b>Connection test failed:</b> {escape(friendly_provider_error(exc))}")
            await audit(admin_id, "connection_test", ok=True)
            src, masked = key_status_lines(e)
            return await render_tools(msg, note=(
                f"✅ <b>Connection OK</b> — key <code>{escape(masked)}</code> ({escape(src)}) is valid.<br>"
                f"💰 <b>Provider balance:</b> <code>{balance:g}</code> (provider account currency)"))
        if action == "refreshcat":
            api.clear_cache()
            services = catalog_items(await api.catalog("getServices"), "services")
            operator = (eff()["operators"] or ("1",))[0]
            countries = catalog_items(await api.catalog("getCountries", operator=operator), "countries")
            await audit(admin_id, "catalog_refreshed", services=len(services), countries=len(countries))
            return await render_tools(msg, note=(
                f"🔄 <b>Catalog cache refreshed:</b> <code>{len(services)}</code> services · "
                f"<code>{len(countries)}</code> countries (operator {operator})."))

        # ------------------------- orders ---------------------------
        if action == "ords":
            return await render_orders_list(msg, parts[2] if parts[2] in FILTERS else "a", int(parts[3]))
        if action == "ord":
            order = load_order(parts[2])
            if not order:
                raise ProviderError("Order not found.")
            return await render_order_detail(msg, order, parts[3], int(parts[4]))
        if action == "oref":
            order = load_order(parts[2])
            if not order:
                raise ProviderError("Order not found.")
            updated = await otp_ctx["refresh_locked"](order["_id"], cancel=False)
            await audit(admin_id, "order_refreshed", order_id=parts[2],
                        status=(updated or {}).get("status"))
            return await render_order_detail(msg, updated or order, note="🔄 Provider status refreshed.")
        if action == "ocancel":
            order = load_order(parts[2])
            if not order or order.get("status") != "waiting":
                raise ProviderError("Only a waiting order can be cancelled via the provider.")
            token = make_confirm(admin_id, "cancel_order", order_id=parts[2])
            return await show(msg,
                              "<h2>❌ Cancel Order via Provider?</h2>"
                              f"<p>Order <code>{parts[2]}</code> · ₹{float(order.get('price', 0)):.2f} will be "
                              "refunded <b>only if the provider confirms cancellation</b> "
                              "(setStatus 8 → ACCESS_CANCEL). If unconfirmed, nothing is refunded.</p>",
                              [[btn("✅ Confirm Cancel", f"otpa:cf:{token}")],
                               [btn("⬅️ Back to Order", f"otpa:ord:{parts[2]}:a:0")]])
        if action == "orefund":
            order = load_order(parts[2])
            if not order or order.get("status") not in ("allocating", "review"):
                raise ProviderError("Manual refund is only available for allocating/review orders.")
            prompt = await msg.answer(
                "💸 <b>Manual Refund — reason required</b>\n\n"
                f"Order <code>{parts[2]}</code> · ₹{float(order.get('price', 0)):.2f}\n"
                "First verify in the provider account that <b>no paid activation exists</b> "
                "for this order. Then send the reconciliation reason (stored in the audit trail).",
                reply_markup=kb([[btn("✖️ Cancel", f"otpa:ord:{parts[2]}:a:0")]]))
            await state.update_data(prompt_id=prompt.message_id, pending="manual_refund", order_id=parts[2])
            await state.set_state(OtpAdminStates.waiting_reason)
            return
        if action == "onote":
            order = load_order(parts[2])
            if not order:
                raise ProviderError("Order not found.")
            prompt = await msg.answer(
                "📝 <b>Add Admin Note</b>\n\nSend the note text. It is stored on the order "
                "and in the audit trail (never include OTPs, numbers or keys).",
                reply_markup=kb([[btn("✖️ Cancel", f"otpa:ord:{parts[2]}:a:0")]]))
            await state.update_data(prompt_id=prompt.message_id, pending="note", order_id=parts[2])
            await state.set_state(OtpAdminStates.waiting_reason)
            return
        if action == "oattach":
            order = load_order(parts[2])
            if not order or order.get("status") not in ("allocating", "review"):
                raise ProviderError("Activations can only be attached to allocating/review orders.")
            prompt = await msg.answer(
                "🔗 <b>Attach Provider Activation</b>\n\n"
                "Find the activation for this purchase in the provider account, then send:\n"
                "<code>activation_id</code> on line 1\n<code>phone_number</code> on line 2 (optional)\n\n"
                "The activation is verified with the provider before the order is updated.",
                reply_markup=kb([[btn("✖️ Cancel", f"otpa:ord:{parts[2]}:a:0")]]))
            await state.update_data(prompt_id=prompt.message_id, order_id=parts[2])
            await state.set_state(OtpAdminStates.waiting_activation)
            return

        # --------------------- popular / hidden ---------------------
        if action == "popmv":
            idx, direction = int(parts[2]), parts[3]
            entries = cfg.popular_apps(settings())
            swap = idx - 1 if direction == "u" else idx + 1
            if 0 <= idx < len(entries) and 0 <= swap < len(entries):
                entries[idx], entries[swap] = entries[swap], entries[idx]
                cfg.set_popular_apps(settings_col, entries, admin_id)
                await audit(admin_id, "popular_updated", count=len(entries))
            return await render_popular(msg)
        if action == "poprm":
            idx = int(parts[2])
            entries = cfg.popular_apps(settings())
            if 0 <= idx < len(entries):
                removed = entries.pop(idx)
                cfg.set_popular_apps(settings_col, entries, admin_id)
                await audit(admin_id, "popular_updated", removed=removed["code"], count=len(entries))
            return await render_popular(msg)
        if action == "popreset":
            cfg.set_popular_apps(settings_col, [], admin_id)
            await audit(admin_id, "popular_updated", count=0)
            return await render_popular(msg)
        if action == "unhide":
            idx = int(parts[2])
            hidden = cfg.hidden_apps(settings())
            if 0 <= idx < len(hidden):
                entry = hidden[idx]
                cfg.set_app_hidden(settings_col, entry["code"], entry["name"], False, admin_id)
                await audit(admin_id, "app_visibility", code=entry["code"], hidden=False)
            return await render_hidden(msg)
        if action == "popadd":
            items = catalog_items(await api.catalog("getServices"), "services")
            if not items:
                raise ProviderError("No catalog available from provider. Please try later.")
            token = str(aviews.insert_one({"kind": "picker", "admin_id": admin_id,
                                           "items": [list(i) for i in items],
                                           "created_at": datetime.utcnow()}).inserted_id)
            try:
                aviews.create_index("created_at", expireAfterSeconds=86400)
            except Exception:
                pass
            return await render_picker(msg, token, 0)
        if action == "pick":
            return await render_picker(msg, parts[2], int(parts[3]))
        if action in ("pickadd", "pickvis"):
            try:
                doc = aviews.find_one({"_id": ObjectId(parts[2]), "kind": "picker"})
            except (InvalidId, TypeError):
                doc = None
            if not doc:
                raise ProviderError("Catalog snapshot expired. Reopen the app picker.")
            idx = int(parts[3])
            code, name = doc["items"][idx]
            raw = settings()
            if action == "pickadd":
                entries = cfg.popular_apps(raw)
                if code in {e["code"] for e in entries}:
                    entries = [e for e in entries if e["code"] != code]
                else:
                    entries.append({"code": code, "name": name})
                cfg.set_popular_apps(settings_col, entries, admin_id)
                await audit(admin_id, "popular_updated", code=code, count=len(entries))
            else:
                now_hidden = code not in cfg.hidden_codes(raw)
                cfg.set_app_hidden(settings_col, code, name, now_hidden, admin_id)
                await audit(admin_id, "app_visibility", code=code, hidden=now_hidden)
            return await render_picker(msg, parts[2], idx // PICKER_PER_PAGE)

        # --------------------- confirmations ------------------------
        if action == "cf":
            doc = take_confirm(parts[2], admin_id)
            if not doc:
                return await show(msg, "⚠️ This confirmation expired or was already used. No action taken.",
                                  [back_home()])
            payload = doc.get("payload") or {}
            if doc["action"] == "remove_key":
                cfg.remove_api_key(settings_col, admin_id)
                api.clear_cache()
                await audit(admin_id, "key_removed", env_fallback=bool(env_api_key))
                return await render_key(msg)
            if doc["action"] == "cancel_order":
                order = load_order(payload.get("order_id"))
                if not order:
                    raise ProviderError("Order not found.")
                updated = await otp_ctx["refresh_locked"](order["_id"], cancel=True)
                await audit(admin_id, "order_cancelled", order_id=payload.get("order_id"),
                            status=(updated or {}).get("status"), refunded=bool((updated or {}).get("refunded")))
                return await render_order_detail(msg, updated or order, note="❌ Cancellation processed.")
            if doc["action"] == "manual_refund":
                order = load_order(payload.get("order_id"))
                if not order:
                    raise ProviderError("Order not found.")
                reason = str(payload.get("reason") or "")[:300]
                updated, refunded = await otp_ctx["manual_refund_locked"](order["_id"])
                if updated is not None:
                    orders.update_one({"_id": order["_id"]}, {
                        "$set": {"reconciled_by": admin_id, "reconciled_at": datetime.utcnow(),
                                 "reconcile_reason": reason},
                        "$push": {"admin_notes": {"text": f"Manual refund: {reason}", "admin_id": admin_id,
                                                   "ts": datetime.utcnow()}},
                    })
                    updated = orders.find_one({"_id": order["_id"]})
                await audit(admin_id, "order_manual_refund", order_id=payload.get("order_id"),
                            refunded=refunded, reason=reason)
                note = ("💸 Wallet refunded exactly once via the idempotent ledger." if refunded
                        else "ℹ️ No new refund was issued (already refunded or never debited).")
                return await render_order_detail(msg, updated or order, note=note)
            return await show(msg, "⚠️ Unknown confirmation. No action taken.", [back_home()])

        return await render_home(msg)

    # ------------------------------------------------------------------
    # FSM message handlers (authorization re-checked on every input)
    # ------------------------------------------------------------------
    async def _fsm_guard(message: Message, state: FSMContext):
        """Return True when the message may proceed as FSM input."""
        if not is_admin(message.from_user.id):
            await state.clear()
            return False
        if message.text and message.text.startswith("/"):
            await state.clear()
            raise SkipHandler()
        return True

    async def _cleanup_prompt(message: Message, data):
        prompt_id = data.get("prompt_id")
        if prompt_id:
            try:
                await bot.delete_message(message.chat.id, prompt_id)
            except Exception:
                pass

    @dp.message(StateFilter(OtpAdminStates.waiting_api_key))
    async def on_api_key_input(message: Message, state: FSMContext):
        if not await _fsm_guard(message, state):
            return
        data = await state.get_data()
        raw = (message.text or "")
        await delete_quietly(message)  # the key must not linger in chat history
        await _cleanup_prompt(message, data)
        if message.chat.type != "private":
            await state.clear()
            return await message.answer("For safety, set the API key in the bot's private chat via /otpadmin.")
        try:
            candidate = cfg.validate_api_key(raw)
        except ValueError as exc:
            prompt = await message.answer(
                f"❌ {escape(str(exc))}\nSend the API key again, or cancel.",
                reply_markup=kb([[btn("✖️ Cancel", "otpa:key")]]))
            await state.update_data(prompt_id=prompt.message_id)
            return
        admin_id = message.from_user.id
        validated, balance = False, None
        try:
            balance = await TemporaClient(candidate).get_balance()
            validated = True
        except ProviderError as exc:
            if exc.code in ("BAD_KEY", "USER_BANNED"):
                prompt = await message.answer(
                    f"❌ <b>Key not saved:</b> {escape(friendly_provider_error(exc))}\n"
                    "Send a different key, or cancel.",
                    reply_markup=kb([[btn("✖️ Cancel", "otpa:key")]]))
                await state.update_data(prompt_id=prompt.message_id)
                return
            # Network/other failures: save as explicitly requested, but say
            # plainly that it could not be verified right now.
        cfg.set_api_key(settings_col, candidate, admin_id)
        api.clear_cache()
        await audit(admin_id, "key_set", source="database", validated=validated,
                    masked=cfg.mask_key(candidate))
        await state.clear()
        note = (f"✅ Key saved and validated. Provider balance: <code>{balance:g}</code>."
                if validated else
                "⚠️ Key saved but <b>could not be verified</b> just now (provider unreachable). "
                "Use Test Connection before relying on it.")
        await render_tools(message, note=note + " The new key is active immediately.")

    @dp.message(StateFilter(OtpAdminStates.waiting_markup))
    async def on_markup_input(message: Message, state: FSMContext):
        if not await _fsm_guard(message, state):
            return
        data = await state.get_data()
        await _cleanup_prompt(message, data)
        try:
            value = cfg.validate_markup(message.text or "")
        except ValueError as exc:
            prompt = await message.answer(
                f"❌ {escape(str(exc))}\nSend the markup again, or cancel.",
                reply_markup=kb([[btn("✖️ Cancel", "otpa:settings")]]))
            await state.update_data(prompt_id=prompt.message_id)
            return
        cfg.set_markup(settings_col, value, message.from_user.id)
        await audit(message.from_user.id, "markup_set", markup=value)
        await state.clear()
        await render_settings(message)

    @dp.message(StateFilter(OtpAdminStates.waiting_reason))
    async def on_reason_input(message: Message, state: FSMContext):
        if not await _fsm_guard(message, state):
            return
        data = await state.get_data()
        await _cleanup_prompt(message, data)
        reason = (message.text or "").strip()[:300]
        pending, order_id = data.get("pending"), data.get("order_id")
        if not reason or not pending or not order_id:
            prompt = await message.answer(
                "❌ A short reason is required. Send it as text, or cancel.",
                reply_markup=kb([[btn("✖️ Cancel", f"otpa:ord:{order_id}:a:0")]]))
            await state.update_data(prompt_id=prompt.message_id)
            return
        admin_id = message.from_user.id
        await state.clear()
        if pending == "note":
            orders.update_one({"_id": ObjectId(order_id)}, {
                "$push": {"admin_notes": {"text": reason, "admin_id": admin_id, "ts": datetime.utcnow()}},
                "$set": {"updated_at": datetime.utcnow()},
            })
            await audit(admin_id, "order_note", order_id=order_id, reason=reason)
            order = load_order(order_id)
            return await render_order_detail(message, order, note="📝 Note saved.")
        # manual_refund → second, explicit confirmation step
        order = load_order(order_id)
        if not order or order.get("status") not in ("allocating", "review"):
            return await message.answer("⚠️ This order is no longer awaiting reconciliation. No refund issued.",
                                        reply_markup=kb([back_home()]))
        token = make_confirm(admin_id, "manual_refund", order_id=order_id, reason=reason)
        await message.answer(
            "<h2>💸 Confirm Manual Refund</h2>"
            f"<p>Order <code>{order_id}</code> · ₹{float(order.get('price', 0)):.2f} to user "
            f"<code>{order.get('user_id')}</code><br>Reason: {escape(reason)}</p>"
            "<p>The refund uses the idempotent wallet ledger, so it can never be paid twice. "
            "Only confirm after verifying no paid activation exists at the provider.</p>",
            reply_markup=kb([[btn("✅ Confirm Refund", f"otpa:cf:{token}")],
                             [btn("✖️ Abort", f"otpa:ord:{order_id}:a:0")]]))

    @dp.message(StateFilter(OtpAdminStates.waiting_activation))
    async def on_activation_input(message: Message, state: FSMContext):
        if not await _fsm_guard(message, state):
            return
        data = await state.get_data()
        await _cleanup_prompt(message, data)
        order_id = data.get("order_id")
        lines = [ln.strip() for ln in (message.text or "").splitlines() if ln.strip()]
        if not lines or not order_id:
            prompt = await message.answer(
                "❌ Send the activation ID on line 1 (phone number on line 2, optional), or cancel.",
                reply_markup=kb([[btn("✖️ Cancel", f"otpa:ord:{order_id}:a:0")]]))
            await state.update_data(prompt_id=prompt.message_id)
            return
        activation_id = lines[0][:64]
        number = lines[1][:32] if len(lines) > 1 else None
        admin_id = message.from_user.id
        await state.clear()
        # Verify with the provider first — never attach blind.
        try:
            provider_state = await api.call("getStatus", id=activation_id)
        except ProviderError as exc:
            return await message.answer(
                f"⚠️ Could not verify that activation: {escape(friendly_provider_error(exc))}\nNothing was changed.",
                reply_markup=kb([[btn("⬅️ Back to Order", f"otpa:ord:{order_id}:a:0")]]))
        if provider_state == "STATUS_CANCEL":
            return await message.answer(
                "⚠️ Provider says this activation is already cancelled, so it was not attached. "
                "If the wallet was debited and no other activation exists, use the manual-refund action with a reason.",
                reply_markup=kb([[btn("⬅️ Back to Order", f"otpa:ord:{order_id}:a:0")]]))
        patch = {"activation_id": activation_id, "status": "waiting",
                 "updated_at": datetime.utcnow(), "reconciled_by": admin_id,
                 "reconciled_at": datetime.utcnow(),
                 "reconcile_reason": "Activation attached by admin after provider verification."}
        if number:
            patch["number"] = number
        if isinstance(provider_state, str) and provider_state.startswith("STATUS_OK:"):
            patch.update(status="completed", otp=provider_state.split(":", 1)[1][:256])
        order = load_order(order_id)
        if not order:
            return await message.answer("⚠️ Order not found. Nothing was changed.", reply_markup=kb([back_home()]))
        async with locks.setdefault(order["user_id"], asyncio.Lock()):
            fresh = load_order(order_id)
            if not fresh or fresh.get("status") not in ("allocating", "review"):
                return await message.answer("⚠️ Order state changed meanwhile; activation not attached.",
                                            reply_markup=kb([[btn("⬅️ Back to Order", f"otpa:ord:{order_id}:a:0")]]))
            orders.update_one({"_id": fresh["_id"]}, {
                "$set": patch,
                "$push": {"admin_notes": {"text": "Activation attached (provider-verified).",
                                           "admin_id": admin_id, "ts": datetime.utcnow()}},
            })
        await audit(admin_id, "order_activation_attached", order_id=order_id)
        await render_order_detail(message, load_order(order_id),
                                  note="🔗 Activation attached and verified with the provider. "
                                       "The buyer can now check this order normally.")

    return None
