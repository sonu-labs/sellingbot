"""All-app TemporaSMS storefront. Credentials and provider calls stay server-side."""
import asyncio
import json
import logging
import math
import time
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_UP
from html import escape

import aiohttp
from bson import ObjectId
from aiogram import F
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, CopyTextButton
from rich_messages import safe_edit_or_send
import otp_settings as otp_cfg

BASE_URL = "https://api.temporasms.com/stubs/handler_api.php"
OPERATORS = ("1", "2", "3", "4", "7", "8", "9", "10")
POPULAR = ("telegram", "instagram", "facebook", "whatsapp", "google", "tiktok")
log = logging.getLogger(__name__)

PROVIDER_ERROR_CODES = {"BAD_KEY", "USER_BANNED", "ERROR", "BAD_ACTION", "BAD_SERVICE", "BAD_COUNTRY", "BAD_OPERATOR", "NO_BALANCE", "NO_NUMBERS", "TOO_MANY_REQUESTS", "UNDER_DEVELOPMENT", "WRONG_MAX_PRICE", "BAD_PRICE"}


class ProviderError(Exception):
    """User-safe provider failure.

    ``code`` carries the provider's non-secret error token (e.g. ``BAD_KEY``)
    or ``CONNECTION``/``NOT_CONFIGURED`` so admin tooling can explain *why*
    a validation failed without ever exposing credentials.
    """

    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


def parse_balance(data):
    """Parse a ``getBalance`` response (``ACCESS_BALANCE:<amount>``)."""
    if isinstance(data, str) and data.startswith("ACCESS_BALANCE:"):
        try:
            value = float(data.split(":", 1)[1])
        except ValueError:
            raise ProviderError("Provider returned an unreadable balance.", code="BAD_RESPONSE") from None
        if math.isfinite(value):
            return value
    raise ProviderError("Provider did not return a balance.", code="BAD_RESPONSE")


class TemporaClient:
    """TemporaSMS client. ``key`` may be a static string or a zero-argument
    callable returning the current key, which is how admin-panel key changes
    apply at runtime without a restart."""

    def __init__(self, key):
        self._key_source = key
        self.cache = {}

    @property
    def key(self):
        source = self._key_source
        return source() if callable(source) else (source or "")

    @key.setter
    def key(self, value):
        self._key_source = value

    def clear_cache(self):
        self.cache.clear()

    async def call(self, action, **params):
        key = self.key
        if not key:
            raise ProviderError("OTP service is not configured yet. Please contact support.", code="NOT_CONFIGURED")
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25)) as session:
                async with session.post(BASE_URL, data={"api_key": key, "action": action, **{k: str(v) for k, v in params.items()}}) as response:
                    response.raise_for_status()
                    raw = (await response.text()).strip()
        except (aiohttp.ClientError, asyncio.TimeoutError):
            # Never log request URLs, exceptions or secrets.
            raise ProviderError("Provider connection interrupted. Please check My OTP Orders before retrying.", code="CONNECTION") from None
        try:
            data = json.loads(raw)
        except ValueError:
            data = raw
        if isinstance(data, str) and data in PROVIDER_ERROR_CODES:
            messages = {"NO_NUMBERS": "Out of stock. Try another country or operator.", "TOO_MANY_REQUESTS": "Provider is busy. Please wait before retrying."}
            raise ProviderError(messages.get(data, "Provider unavailable for this selection. Please try later or contact support."), code=data)
        return data

    async def get_balance(self):
        """Provider account balance (also validates the configured key)."""
        return parse_balance(await self.call("getBalance"))

    async def catalog(self, action, **params):
        key = (action, tuple(sorted(params.items())))
        saved = self.cache.get(key)
        if saved and time.monotonic() - saved[0] < 300:
            return saved[1]
        data = await self.call(action, **params)
        self.cache[key] = (time.monotonic(), data)
        return data


def catalog_items(data, kind):
    """Accept ID/name maps, ID/object maps and services/countries envelopes."""
    if isinstance(data, dict):
        data = data.get(kind, data)
    result = []
    entries = data.items() if isinstance(data, dict) else enumerate(data) if isinstance(data, list) else []
    for key, value in entries:
        if isinstance(value, dict):
            code = value.get("id", value.get("code", key))
            name = value.get("name") or value.get("eng") or value.get("english") or value.get("service") or str(code)
        else:
            code, name = key, value
        if isinstance(name, str) and str(code) and name:
            result.append((str(code), name))
    return sorted(result, key=lambda item: item[1].casefold())


def price_options(data, country, service):
    node = data.get(str(country), {}).get(str(service), {}) if isinstance(data, dict) else {}
    options = []
    def visit(value, provider=None):
        if not isinstance(value, dict):
            return
        cost = value.get("price", value.get("cost"))
        count = value.get("count", value.get("stock", 0))
        try:
            cost, count = float(cost), int(count)
            if math.isfinite(cost) and cost > 0 and count > 0:
                options.append({"cost": cost, "stock": count, "provider": provider})
        except (ValueError, TypeError):
            pass
        for key, child in value.items():
            if isinstance(child, dict):
                if key == "providers":
                    for pid, option in child.items():
                        visit(option, str(pid))
                else:
                    visit(child, str(key) if str(key).isdigit() and provider is None else provider)
    visit(node)
    return sorted(options, key=lambda x: x["cost"])


def retail_price(cost, rate, markup):
    return float((Decimal(str(cost)) * Decimal(str(rate)) * (1 + Decimal(str(markup)) / 100)).quantize(Decimal("0.01"), rounding=ROUND_UP))


def register_otp_handlers(*, dp, bot, db, users_col, api_key, rate, markup=0, settings_col=None, registry=None):
    if not math.isfinite(float(markup)) or float(markup) < 0:
        raise ValueError("OTP_MARKUP_PERCENT must be a finite, non-negative percentage")
    env_key, env_markup = api_key, float(markup)

    def current():
        """Effective runtime settings.

        With a settings collection, admin-panel changes (DB key, markup,
        service switch, operator toggles) apply immediately; the static
        constructor arguments act as the environment fallback.  Without one
        the historic static behaviour is preserved exactly.
        """
        if settings_col is None:
            return {"raw": {}, "enabled": True, "api_key": env_key or "", "key_source": "environment" if env_key else None,
                    "markup": env_markup, "markup_source": "environment", "rate": float(rate),
                    "operators": OPERATORS, "version": 0}
        return otp_cfg.effective_settings(settings_col, env_key, env_markup, rate)

    def require_purchase_open(eff):
        if not eff["enabled"]:
            raise ProviderError("OTP service is temporarily disabled by the admin. Existing orders remain available in My OTP Orders.")

    api = TemporaClient(lambda: current()["api_key"] if settings_col is not None else env_key)
    views, orders = db["otp_views"], db["otp_orders"]
    locks = {}  # Serialize check/cancel/purchase per user within this worker.

    def button(text, data):
        return InlineKeyboardButton(text=text, callback_data=data)

    def view(uid, **data):
        return str(views.insert_one({"user_id": uid, "created_at": datetime.utcnow(), **data}).inserted_id)

    def keyboard(rows):
        return InlineKeyboardMarkup(inline_keyboard=rows)

    async def show(cq, text, rows):
        await safe_edit_or_send(bot, cq.message, text, reply_markup=keyboard(rows))

    def credit(order):
        """Refund an order exactly once and return True if money was returned.

        Crash-safety: the wallet credit and its idempotency marker live in a
        single user document, so that one update is atomic on every MongoDB
        deployment (standalone included — no multi-document transaction
        support required).  The guarded filter (``otp_refunds: {$ne: oid}``)
        makes repeated calls, retries and post-crash replays no-ops.  The
        order document is only marked refunded after the ledger state is
        re-read, so a crash between the two writes is healed by the next
        ``credit``/worker pass instead of double-paying.
        """
        uid, oid = order["user_id"], str(order["_id"])
        users_col.update_one({"_id": uid, "otp_debits": oid, "otp_refunds": {"$ne": oid}}, {"$inc": {"balance": order["price"]}, "$addToSet": {"otp_refunds": oid}})
        user = users_col.find_one({"_id": uid}) or {}
        refunded = oid in (user.get("otp_refunds") or [])
        debited = oid in (user.get("otp_debits") or [])
        patch = {"status": "cancelled", "refunded": refunded, "updated_at": datetime.utcnow()}
        if refunded:
            patch["refunded_at"] = datetime.utcnow()
        elif not debited:
            patch["refund_note"] = "No wallet debit found for this order; nothing to refund."
        orders.update_one({"_id": order["_id"]}, {"$set": patch})
        return refunded

    async def refresh(order, cancel=False):
        if order["status"] != "waiting":
            if order["status"] == "refunding":
                credit(order)
            return
        data = await api.call("getStatus", id=order["activation_id"])
        if isinstance(data, str) and data.startswith("STATUS_OK:"):
            orders.update_one({"_id": order["_id"], "status": "waiting"}, {"$set": {"status": "completed", "otp": data.split(":", 1)[1], "updated_at": datetime.utcnow()}})
        elif data == "STATUS_CANCEL":
            orders.update_one({"_id": order["_id"]}, {"$set": {"status": "refunding", "updated_at": datetime.utcnow()}})
            credit(order)
        elif cancel:
            result = await api.call("setStatus", id=order["activation_id"], status=8)
            if result in ("ACCESS_CANCEL", "STATUS_CANCEL"):
                orders.update_one({"_id": order["_id"]}, {"$set": {"status": "refunding", "updated_at": datetime.utcnow()}})
                credit(order)
            else:
                raise ProviderError("Cancellation not confirmed. Wait a little and check again. No refund issued yet.")

    async def refresh_locked(order_id, cancel=False):
        """Reload an order under its per-user lock and refresh provider state.

        Used by the admin panel so admin refresh/cancel can never race the
        buyer or the background worker into a double refund.
        """
        order = orders.find_one({"_id": order_id})
        if not order:
            return None
        async with locks.setdefault(order["user_id"], asyncio.Lock()):
            fresh = orders.find_one({"_id": order_id})
            if fresh:
                await refresh(fresh, cancel)
            return orders.find_one({"_id": order_id})

    async def manual_refund_locked(order_id):
        """Idempotent admin refund for orders with no confirmed allocation.

        Only ``allocating``/``review``/``refunding`` orders qualify; a
        ``waiting`` order has a live provider activation and must go
        through the provider-confirmed cancellation path instead.
        Returns ``(order, refunded_bool)``.
        """
        order = orders.find_one({"_id": order_id})
        if not order:
            return None, False
        async with locks.setdefault(order["user_id"], asyncio.Lock()):
            fresh = orders.find_one({"_id": order_id})
            if not fresh or fresh["status"] not in ("allocating", "review", "refunding"):
                return fresh, False
            orders.update_one({"_id": order_id}, {"$set": {"status": "refunding", "updated_at": datetime.utcnow()}})
            refunded = credit(fresh)
            return orders.find_one({"_id": order_id}), refunded

    async def render_order(cq, order):
        oid = str(order["_id"])
        text = (f"<b>📲 OTP Order</b>\n\nApp: {escape(order['service_name'])}\nCountry: {escape(order['country_name'])}\n"
                f"Price: ₹{order['price']:.2f}\nStatus: <b>{escape(order['status'])}</b>\n")
        rows = []
        if order.get("number"):
            text += f"Number: <code>{escape(order['number'])}</code>\n"
            rows.append([InlineKeyboardButton(text="📋 Copy Number", copy_text=CopyTextButton(text=order["number"]))])
        if order.get("otp"):
            text += f"\nOTP: <code>{escape(order['otp'])}</code>"
            rows.append([InlineKeyboardButton(text="📋 Copy OTP", copy_text=CopyTextButton(text=order["otp"][:256]))])
        if order["status"] == "waiting":
            text += "\nAuto-check active. Refund only after provider confirms cancellation."
            rows.append([button("📩 Check OTP", f"otp:check:{oid}"), button("❌ Cancel", f"otp:cancel:{oid}")])
        if order["status"] in ("allocating", "review"):
            text += "\nAllocation awaiting reconciliation. Do not repurchase; contact support with this order ID."
        text += f"\n\nOrder ID: <code>{oid}</code>"
        rows.append([button("📦 My OTP Orders", "otp:history"), button("📲 All Apps", "otp:open")])
        await show(cq, text, rows)

    async def listing(cq, kind, context=None, page=0):
        uid = cq.from_user.id
        context = context or {}
        if kind == "services":
            items = catalog_items(await api.catalog("getServices"), "services")
            # Admin popular ordering / hidden apps (falls back to the
            # historic built-in ordering when nothing is configured).
            items = otp_cfg.sort_services(items, current()["raw"])
            title = "📲 Buy OTPs (All Apps)"
        else:
            items = catalog_items(await api.catalog("getCountries", operator=context["operator"]), "countries")
            title = f"🌍 Select Country · {escape(context['service_name'])}"
        if not items:
            raise ProviderError("No catalog available from provider. Please try later.")
        page = max(0, min(page, (len(items)-1)//12))
        token = view(uid, kind=kind, context=context, items=items, page=page)
        rows = [[button(name[:55], f"otp:select:{token}:{i}")] for i, (_, name) in enumerate(items) if page*12 <= i < (page+1)*12]
        nav = []
        if page:
            nav.append(button("‹ Previous", f"otp:page:{token}:{page-1}"))
        if (page+1)*12 < len(items):
            nav.append(button("View More ›", f"otp:page:{token}:{page+1}"))
        if nav:
            rows.append(nav)
        rows.append([button("📦 My OTP Orders", "otp:history"), button("⬅️ All Apps", "otp:open")])
        rows.append([button("🏠 Main Menu", "back_main")])
        await show(cq, f"<b>{title}</b>\n\n{len(items)} available · Page {page+1}/{(len(items)+11)//12}\nChoose below. Legitimate use only; follow each platform's rules.", rows)

    @dp.callback_query(F.data.startswith("otp:"))
    async def handler(cq):
        await cq.answer()
        uid = cq.from_user.id
        async with locks.setdefault(uid, asyncio.Lock()):
            try:
                await dispatch(cq)
            except ProviderError as exc:
                await show(cq, escape(str(exc)), [[button("📦 My OTP Orders", "otp:history"), button("🔄 All Apps", "otp:open")]])
            except Exception:
                log.error("OTP action failed for user %s (details suppressed to protect credentials)", uid)
                await show(cq, "Unable to process this action. Check My OTP Orders before retrying, or contact support.", [[button("📦 My OTP Orders", "otp:history")]])

    async def dispatch(cq):
        parts, uid = cq.data.split(":"), cq.from_user.id
        action = parts[1]
        eff = current()
        # Existing orders (history/check/cancel) stay reachable even when
        # the service is switched off; only new purchases are gated.  The
        # gate runs here, at dispatch time, so stale buttons on old
        # messages cannot start a purchase either.
        if action not in ("history", "check", "cancel"):
            require_purchase_open(eff)
        if action == "open":
            return await listing(cq, "services")
        if action == "history":
            page = max(0, int(parts[2])) if len(parts) > 2 else 0
            docs = list(orders.find({"user_id": uid}).sort("created_at", -1).skip(page*10).limit(11))
            rows = [[button(f"{o['service_name'][:20]} · {o['status']} · ₹{o['price']:.2f}", f"otp:check:{o['_id']}")] for o in docs[:10]]
            nav = []
            if page:
                nav.append(button("‹ Previous", f"otp:history:{page-1}"))
            if len(docs) > 10:
                nav.append(button("More ›", f"otp:history:{page+1}"))
            if nav:
                rows.append(nav)
            rows.append([button("📲 Buy OTPs", "otp:open")])
            return await show(cq, "<b>📦 My OTP Orders</b>\nSelect an order to view its number, OTP or refund status.", rows)
        if action in ("check", "cancel"):
            order = orders.find_one({"_id": ObjectId(parts[2]), "user_id": uid})
            if not order:
                raise ProviderError("Order not found.")
            await refresh(order, action == "cancel")
            return await render_order(cq, orders.find_one({"_id": order["_id"]}))
        doc = views.find_one({"_id": ObjectId(parts[2]), "user_id": uid})
        if not doc:
            raise ProviderError("Selection expired. Please reopen All Apps.")
        if action == "page":
            return await listing(cq, doc["kind"], doc["context"], int(parts[3]))
        if action == "select":
            code, name = doc["items"][int(parts[3])]
            if doc["kind"] == "services":
                if code in otp_cfg.hidden_codes(eff["raw"]):
                    raise ProviderError("This app is currently unavailable. Please choose another app.")
                ops = eff["operators"]
                if not ops:
                    raise ProviderError("No operators are currently available. Please try later.")
                token = view(uid, service=code, service_name=name)
                return await show(cq, f"<b>{escape(name)} · Select Operator</b>\nLive country availability and stock depend on the selected operator.", [[button(f"Operator {op}", f"otp:operator:{token}:{op}")] for op in ops] + [[button("⬅️ Apps", "otp:open")]])
            ctx = {**doc["context"], "country": code, "country_name": name}
            if ctx["operator"] not in eff["operators"]:
                raise ProviderError("This operator is currently unavailable. Please choose another operator.")
            options = price_options(await api.call("getPricesV3", country=code, service=ctx["service"], operator=ctx["operator"]), code, ctx["service"])
            if not options:
                raise ProviderError("No live stock for this country/operator. Try another selection.")
            option = options[0]
            price = retail_price(option["cost"], eff["rate"], eff["markup"])
            # Stamp the quote with the settings it was priced under; any
            # admin change bumps the version and forces a fresh quote.
            token = view(uid, **ctx, cost=option["cost"], provider=option["provider"], price=price,
                         settings_version=eff["version"], markup=eff["markup"], rate=eff["rate"])
            return await show(cq, f"<b>Confirm OTP Purchase</b>\n\nApp: {escape(ctx['service_name'])}\nCountry: {escape(name)}\nOperator: {ctx['operator']}\nStock: {option['stock']}\n<b>Wallet charge: ₹{price:.2f}</b>\n\nQuote valid for 2 minutes. No OTP? Cancel for a refund once confirmed by provider.", [[button(f"✅ Buy · ₹{price:.2f}", f"otp:buy:{token}")], [button("⬅️ Apps", "otp:open")]])
        if action == "operator":
            if parts[3] not in OPERATORS or parts[3] not in eff["operators"]:
                raise ProviderError("This operator is currently unavailable. Please choose another operator.")
            return await listing(cq, "countries", {"service": doc["service"], "service_name": doc["service_name"], "operator": parts[3]})
        if action == "buy":
            existing = orders.find_one({"_id": doc["_id"], "user_id": uid})
            if existing:
                return await render_order(cq, existing)
            if datetime.utcnow() - doc["created_at"] > timedelta(minutes=2):
                raise ProviderError("Quote expired. Please select your country again for current pricing.")
            # Re-validate against *current* settings: a markup/key/operator
            # change (or service switch-off, checked above) since the quote
            # was shown must produce a fresh confirmation, never a charge
            # at a price the user did not just confirm.
            if int(doc.get("settings_version") or 0) != eff["version"] or doc.get("operator") not in eff["operators"]:
                raise ProviderError("Pricing or availability just changed. Please select your country again to confirm the current price.")
            if abs(retail_price(doc["cost"], eff["rate"], eff["markup"]) - float(doc["price"])) > 0.001:
                raise ProviderError("Pricing just changed. Please select your country again to confirm the current price.")
            # Persist intent BEFORE touching money or allocating a paid number.
            order = {k: doc[k] for k in ("service", "service_name", "country", "country_name", "operator", "price", "cost", "provider")}
            order.update(_id=doc["_id"], user_id=uid, status="allocating", created_at=datetime.utcnow(),
                         updated_at=datetime.utcnow(), markup=eff["markup"], rate=eff["rate"])
            orders.insert_one(order)
            oid = str(order["_id"])
            charged = users_col.update_one({"_id": uid, "balance": {"$gte": order["price"]}, "otp_debits": {"$ne": oid}}, {"$inc": {"balance": -order["price"]}, "$addToSet": {"otp_debits": oid}})
            if not charged.modified_count:
                orders.update_one({"_id": order["_id"]}, {"$set": {"status": "unpaid", "updated_at": datetime.utcnow()}})
                raise ProviderError("Insufficient wallet balance. Recharge and create a new quote.")
            params = {"service": order["service"], "country": order["country"], "operator": order["operator"], "maxPrice": order["cost"]}
            if order.get("provider"):
                params["providerIds"] = order["provider"]
            try:
                result = await api.call("getNumber", **params)
            except ProviderError as exc:
                # Connection ambiguity must NOT trigger a free refund + duplicate purchase.
                if str(exc).startswith("Out of stock") or str(exc).startswith("Provider unavailable") or str(exc).startswith("Provider is busy"):
                    orders.update_one({"_id": order["_id"]}, {"$set": {"status": "refunding", "updated_at": datetime.utcnow()}})
                    credit(order)
                else:
                    orders.update_one({"_id": order["_id"]}, {"$set": {"status": "review", "updated_at": datetime.utcnow()}})
                raise
            if isinstance(result, str) and result.startswith("ACCESS_NUMBER:") and len(result.split(":")) == 3:
                _, activation, number = result.split(":")
                orders.update_one({"_id": order["_id"]}, {"$set": {"status": "waiting", "activation_id": activation, "number": number, "updated_at": datetime.utcnow()}})
            else:
                orders.update_one({"_id": order["_id"]}, {"$set": {"status": "review", "updated_at": datetime.utcnow()}})
            return await render_order(cq, orders.find_one({"_id": order["_id"]}))

    async def worker():
        # Persistent orders survive restarts; polling is not attached to a message.
        while True:
            try:
                views.create_index("created_at", expireAfterSeconds=86400)
                orders.create_index([("user_id", 1), ("created_at", -1)])
                orders.create_index([("status", 1), ("created_at", -1)])
                # A crash between wallet debit and the provider response
                # leaves an order in "allocating".  Surface it for admin
                # reconciliation — never auto-refund or blind-retry, because
                # the provider may have allocated (and charged for) a number.
                stale_before = datetime.utcnow() - timedelta(minutes=10)
                orders.update_many(
                    {"status": "allocating", "created_at": {"$lt": stale_before}},
                    {"$set": {"status": "review", "review_reason": "stale_allocating", "updated_at": datetime.utcnow()}},
                )
                for order in orders.find({"status": {"$in": ["waiting", "refunding"]}}):
                    async with locks.setdefault(order["user_id"], asyncio.Lock()):
                        current = orders.find_one({"_id": order["_id"]})
                        try:
                            await refresh(current, datetime.utcnow() - current["created_at"] > timedelta(minutes=20))
                            updated = orders.find_one({"_id": order["_id"]})
                            if updated["status"] != current["status"]:
                                text = "OTP received! Open My OTP Orders to copy it." if updated["status"] == "completed" else "OTP order cancelled. Wallet refunded."
                                await bot.send_message(order["user_id"], text, reply_markup=keyboard([[button("📦 View Order", f"otp:check:{order['_id']}")]]))
                        except ProviderError:
                            pass
                        except Exception:
                            log.error("OTP order polling failed (details suppressed)")
                    await asyncio.sleep(1)
            except Exception:
                log.error("OTP worker iteration failed (details suppressed)")
            await asyncio.sleep(30)

    if registry is not None:
        # Shared handles for the admin panel: same client (runtime key +
        # catalog cache), same per-user locks, same idempotent money paths.
        registry.update({
            "api": api,
            "orders": orders,
            "views": views,
            "locks": locks,
            "current": current,
            "refresh_locked": refresh_locked,
            "manual_refund_locked": manual_refund_locked,
        })
    return worker
