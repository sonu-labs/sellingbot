"""Offline tests for the OTP admin panel, runtime settings and money safety.

Everything runs against an in-memory fake MongoDB and a stubbed provider:
no network, no real API key and — critically — no real paid activation.
"""

import asyncio
import copy
import html
import pathlib
import sys
import unittest
from datetime import datetime
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from bson import ObjectId  # noqa: E402

import otp_settings as cfg  # noqa: E402
import otp_admin  # noqa: E402
from otp import (  # noqa: E402
    ProviderError, TemporaClient, parse_balance, register_otp_handlers, retail_price,
)
from otp_admin import register_otp_admin_handlers  # noqa: E402

ADMIN = 999
ADMIN2 = 998
USER = 111
USER2 = 222
ENV_KEY = "env-secret-key-1234"
DB_KEY = "db-secret-key-9876"
DEFAULT_SERVICES = {"tg": "Telegram", "wa": "WhatsApp", "ig": "Instagram", "xyz": "XYZ App"}
DEFAULT_COUNTRIES = {"22": "India", "0": "Russia"}


# ---------------------------------------------------------------------------
# In-memory Mongo stand-in (only the operators the OTP modules actually use)
# ---------------------------------------------------------------------------
def _matches(doc, flt):
    for key, cond in (flt or {}).items():
        value = doc.get(key)
        if isinstance(cond, dict):
            for op, operand in cond.items():
                if op == "$ne":
                    if isinstance(value, list):
                        if operand in value:
                            return False
                    elif value == operand:
                        return False
                elif op == "$gte":
                    if value is None or value < operand:
                        return False
                elif op == "$lt":
                    if value is None or not value < operand:
                        return False
                elif op == "$in":
                    if value not in operand:
                        return False
                else:  # pragma: no cover
                    raise AssertionError(f"unsupported operator {op}")
        elif isinstance(value, list):
            if cond not in value:
                return False
        elif value != cond:
            return False
    return True


class FakeCursor:
    def __init__(self, docs):
        self._docs = docs

    def sort(self, key, direction=1):
        self._docs.sort(key=lambda d: (d.get(key) is None, d.get(key)), reverse=direction < 0)
        return self

    def skip(self, n):
        self._docs = self._docs[n:]
        return self

    def limit(self, n):
        self._docs = self._docs[:n]
        return self

    def __iter__(self):
        return iter(self._docs)


class FakeCollection:
    def __init__(self):
        self.docs = []

    def find_one(self, flt=None, *args, **kwargs):
        for doc in self.docs:
            if _matches(doc, flt):
                return doc
        return None

    def find(self, flt=None, *args, **kwargs):
        return FakeCursor([d for d in self.docs if _matches(d, flt)])

    def insert_one(self, doc):
        doc = copy.deepcopy(doc)
        doc.setdefault("_id", ObjectId())
        self.docs.append(doc)
        return SimpleNamespace(inserted_id=doc["_id"])

    def _apply(self, doc, update):
        for op, fields in update.items():
            if op == "$set":
                doc.update(fields)
            elif op == "$inc":
                for k, v in fields.items():
                    doc[k] = (doc.get(k) or 0) + v
            elif op == "$addToSet":
                for k, v in fields.items():
                    cur = doc.setdefault(k, [])
                    if v not in cur:
                        cur.append(v)
            elif op == "$push":
                for k, v in fields.items():
                    doc.setdefault(k, []).append(v)
            else:  # pragma: no cover
                raise AssertionError(op)

    def update_one(self, flt, update, upsert=False):
        for doc in self.docs:
            if _matches(doc, flt):
                self._apply(doc, update)
                return SimpleNamespace(modified_count=1, matched_count=1)
        if upsert:
            doc = {k: v for k, v in (flt or {}).items() if not isinstance(v, dict)}
            self._apply(doc, update)
            doc.setdefault("_id", ObjectId())
            self.docs.append(doc)
            return SimpleNamespace(modified_count=1, matched_count=0)
        return SimpleNamespace(modified_count=0, matched_count=0)

    def update_many(self, flt, update):
        n = 0
        for doc in self.docs:
            if _matches(doc, flt):
                self._apply(doc, update)
                n += 1
        return SimpleNamespace(modified_count=n)

    def delete_one(self, flt):
        for i, doc in enumerate(self.docs):
            if _matches(doc, flt):
                del self.docs[i]
                return SimpleNamespace(deleted_count=1)
        return SimpleNamespace(deleted_count=0)

    def count_documents(self, flt=None):
        return sum(1 for d in self.docs if _matches(d, flt))

    def create_index(self, *args, **kwargs):
        return "ok"


class FakeDB:
    def __init__(self):
        self.cols = {}

    def __getitem__(self, name):
        return self.cols.setdefault(name, FakeCollection())


# ---------------------------------------------------------------------------
# Fake aiogram plumbing
# ---------------------------------------------------------------------------
class FakeDispatcher:
    def __init__(self):
        self.callback_handlers = []
        self.message_handlers = []

    def callback_query(self, *filters):
        def deco(fn):
            self.callback_handlers.append(fn)
            return fn
        return deco

    def message(self, *filters):
        def deco(fn):
            self.message_handlers.append(fn)
            return fn
        return deco

    def cb(self, name):
        return next(f for f in self.callback_handlers if f.__name__ == name)

    def msg(self, name):
        return next(f for f in self.message_handlers if f.__name__ == name)


class FakeState:
    def __init__(self):
        self.state = None
        self.data = {}

    async def set_state(self, state):
        # Accepts aiogram State objects and plain string states alike
        # (the storefront search uses the plain "waiting_search" string).
        self.state = getattr(state, "state", state)

    async def get_data(self):
        return dict(self.data)

    async def update_data(self, **kw):
        self.data.update(kw)

    async def clear(self):
        self.state = None
        self.data = {}


class FakeMessage:
    sent = []  # every FakeMessage produced by answer(), for leak assertions
    _seq = 1000

    def __init__(self, from_id=ADMIN, text=None, chat_type="private"):
        FakeMessage._seq += 1
        self.message_id = FakeMessage._seq
        self.from_user = SimpleNamespace(id=from_id)
        self.chat = SimpleNamespace(id=from_id, type=chat_type)
        self.text = text
        self.last_text = None
        self.last_markup = None
        self.deleted = False

    async def edit_text(self, text, parse_mode=None, reply_markup=None, **kw):
        self.last_text, self.last_markup = text, reply_markup
        return self

    async def answer(self, text, parse_mode=None, reply_markup=None, **kw):
        msg = FakeMessage(from_id=0)
        msg.last_text, msg.last_markup = text, reply_markup
        msg.chat = self.chat
        FakeMessage.sent.append(msg)
        return msg

    async def delete(self):
        self.deleted = True


class FakeCQ:
    def __init__(self, data, from_id=USER):
        self.data = data
        self.from_user = SimpleNamespace(id=from_id)
        self.message = FakeMessage(from_id=from_id)
        self.answers = []

    async def answer(self, text=None, show_alert=False, **kw):
        self.answers.append((text, show_alert))


class FakeBot:
    def __init__(self):
        self.sent = []
        self.deleted = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))
        return FakeMessage(from_id=0)

    async def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))


def buttons(markup):
    out = []
    for row in (markup.inline_keyboard if markup else []):
        out.extend(row)
    return out


def find_cb(markup, prefix=None, contains=None):
    for b in buttons(markup):
        data = b.callback_data or ""
        if prefix and data.startswith(prefix):
            return data
        if contains and contains in b.text:
            return data
    raise AssertionError(f"button not found prefix={prefix} contains={contains}: "
                         f"{[(b.text, b.callback_data) for b in buttons(markup)]}")


def all_cbs(markup, prefix):
    return [b.callback_data for b in buttons(markup) if (b.callback_data or "").startswith(prefix)]


def pager_labels(markup):
    """Numbered pager labels: ``1``, ``• 2 •`` (current) and ``…`` gaps."""
    return [(b.text, b.callback_data or "") for b in buttons(markup)
            if b.text == "…" or b.text.isdigit()
            or (b.text.startswith("• ") and b.text.endswith(" •"))]


def step_labels(markup):
    """⬅️/➡️ step buttons of the pager."""
    return [(b.text, b.callback_data or "") for b in buttons(markup)
            if b.text in ("⬅️", "➡️") and (b.callback_data or "").startswith("otp:page:")]


def item_buttons(markup):
    """Catalog item buttons on the current page: ``[(label, callback_data)]``."""
    return [(b.text, b.callback_data or "") for b in buttons(markup)
            if (b.callback_data or "").startswith("otp:select:")]


def item_labels(markup):
    return [text for text, _cb in item_buttons(markup)]


# ---------------------------------------------------------------------------
# Settings unit tests
# ---------------------------------------------------------------------------
class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.col = FakeCollection()

    def test_defaults_and_backfill(self):
        doc = cfg.get_settings(self.col)
        self.assertTrue(doc["enabled"])
        self.assertIsNone(doc["tempora_api_key"])
        again = cfg.get_settings(self.col)
        self.assertEqual(again["_id"], "otp_services")
        self.assertEqual(self.col.count_documents({}), 1)

    def test_key_precedence_db_over_env(self):
        cfg.set_api_key(self.col, DB_KEY, ADMIN)
        key, source = cfg.resolve_api_key(cfg.get_settings(self.col), ENV_KEY)
        self.assertEqual((key, source), (DB_KEY, "database"))
        cfg.remove_api_key(self.col, ADMIN)
        key, source = cfg.resolve_api_key(cfg.get_settings(self.col), ENV_KEY)
        self.assertEqual((key, source), (ENV_KEY, "environment"))
        self.assertEqual(cfg.resolve_api_key(cfg.get_settings(self.col), ""), ("", None))

    def test_mask_never_reveals_key(self):
        self.assertNotIn(DB_KEY, cfg.mask_key(DB_KEY))
        self.assertTrue(cfg.mask_key(DB_KEY).endswith(DB_KEY[-4:]))
        self.assertEqual(cfg.mask_key("short"), "••••••••")
        self.assertEqual(cfg.mask_key(""), "Not set")

    def test_api_key_validation(self):
        for bad in ("", "   ", "has space", "x" * 513, None):
            with self.assertRaises(ValueError):
                cfg.validate_api_key(bad)
        self.assertEqual(cfg.validate_api_key("  abc123  "), "abc123")

    def test_markup_validation(self):
        self.assertEqual(cfg.validate_markup("25"), 25.0)
        self.assertEqual(cfg.validate_markup("12.5%"), 12.5)
        self.assertEqual(cfg.validate_markup(0), 0.0)
        for bad in ("-1", "nan", "inf", "-inf", "abc", "", "1e400", "10001"):
            with self.assertRaises(ValueError, msg=bad):
                cfg.validate_markup(bad)

    def test_markup_precedence_and_corrupt_fallback(self):
        cfg.set_markup(self.col, 30, ADMIN)
        self.assertEqual(cfg.resolve_markup(cfg.get_settings(self.col), 10), (30.0, "database"))
        cfg.clear_markup(self.col, ADMIN)
        self.assertEqual(cfg.resolve_markup(cfg.get_settings(self.col), 10), (10.0, "environment"))
        self.col.update_one({"_id": "otp_services"}, {"$set": {"markup_percent": "garbage"}})
        self.assertEqual(cfg.resolve_markup(cfg.get_settings(self.col), 10), (10.0, "environment"))

    def test_operator_toggle_bumps_version(self):
        before = cfg.get_settings(self.col)["settings_version"]
        cfg.set_operator_enabled(self.col, "3", False, ADMIN)
        doc = cfg.get_settings(self.col)
        self.assertNotIn("3", cfg.enabled_operators(doc))
        self.assertEqual(doc["settings_version"], before + 1)
        cfg.set_operator_enabled(self.col, "3", True, ADMIN)
        self.assertIn("3", cfg.enabled_operators(cfg.get_settings(self.col)))
        with self.assertRaises(ValueError):
            cfg.set_operator_enabled(self.col, "42", True, ADMIN)

    def test_sort_services_defaults_custom_and_hidden(self):
        items = [("xyz", "XYZ App"), ("wa", "WhatsApp"), ("tg", "Telegram")]
        default = cfg.sort_services(items, cfg.get_settings(self.col))
        self.assertEqual([c for c, _ in default], ["tg", "wa", "xyz"])
        cfg.set_popular_apps(self.col, [{"code": "xyz", "name": "XYZ App"}], ADMIN)
        custom = cfg.sort_services(items, cfg.get_settings(self.col))
        self.assertEqual(custom[0][0], "xyz")
        cfg.set_app_hidden(self.col, "wa", "WhatsApp", True, ADMIN)
        visible = cfg.sort_services(items, cfg.get_settings(self.col))
        self.assertEqual([c for c, _ in visible], ["xyz", "tg"])


class ClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_dynamic_key_reflects_runtime_changes(self):
        holder = {"key": "first"}
        client = TemporaClient(lambda: holder["key"])
        self.assertEqual(client.key, "first")
        holder["key"] = "second"
        self.assertEqual(client.key, "second")
        holder["key"] = ""
        with self.assertRaises(ProviderError) as ctx:
            await client.call("getBalance")  # must fail before any network use
        self.assertEqual(ctx.exception.code, "NOT_CONFIGURED")

    def test_parse_balance(self):
        self.assertEqual(parse_balance("ACCESS_BALANCE:12.50"), 12.5)
        for bad in ("ACCESS_BALANCE:xyz", "WAIT", {}, None):
            with self.assertRaises(ProviderError):
                parse_balance(bad)


# ---------------------------------------------------------------------------
# Integrated storefront + admin tests
# ---------------------------------------------------------------------------
class Harness:
    """Wires both handler sets against fakes with a stubbed provider."""

    def __init__(self, balance=100.0, admin_log_id=None):
        FakeMessage.sent = []
        self.db = FakeDB()
        self.settings_col = self.db["settings"]
        self.users = self.db["users"]
        self.users.insert_one({"_id": USER, "balance": balance})
        self.orders = self.db["otp_orders"]
        self.dp = FakeDispatcher()
        self.bot = FakeBot()
        self.ctx = {}
        self.number_calls = 0
        self.getnumber_error = None
        self.status_response = "STATUS_WAIT"
        self.provider_calls = []
        self.operators_response = {"Operator 1": "1", "Operator 2": "2", "Operator 3": "3"}
        self.services_response = dict(DEFAULT_SERVICES)
        self.countries_response = dict(DEFAULT_COUNTRIES)
        self.prices_response = None
        # (service, operator) -> provider error code raised by getPricesV3,
        # e.g. {("tss", "9"): "BAD_SERVICE"} for operator-specific routing.
        self.price_failures = {}
        self.inflight = 0
        self.peak_inflight = 0
        self.states = {}
        register_otp_handlers(
            dp=self.dp, bot=self.bot, db=self.db, users_col=self.users,
            api_key=ENV_KEY, rate=100.0, markup=10.0,
            settings_col=self.settings_col, registry=self.ctx)
        register_otp_admin_handlers(
            dp=self.dp, bot=self.bot, db=self.db, users_col=self.users,
            settings_col=self.settings_col, admin_ids=[ADMIN, ADMIN2],
            env_api_key=ENV_KEY, env_markup=10.0, rate=100.0, otp_ctx=self.ctx,
            admin_log_id=admin_log_id)

        async def fake_call(action, **params):
            self.provider_calls.append((action, dict(params)))
            self.inflight += 1
            self.peak_inflight = max(self.peak_inflight, self.inflight)
            try:
                if action == "getServices":
                    return dict(self.services_response)
                if action == "getOperators":
                    return self.operators_response
                if action == "getCountries":
                    return dict(self.countries_response)
                if action == "getPricesV3":
                    failure = self.price_failures.get((str(params["service"]), str(params["operator"])))
                    if failure:
                        raise ProviderError("Provider unavailable for this selection. "
                                            "Please try later or contact support.", code=failure)
                    # Yield so parallel probes actually overlap in the loop.
                    await asyncio.sleep(0)
                    if self.prices_response is not None:
                        return copy.deepcopy(self.prices_response)
                    return {str(params["country"]): {str(params["service"]): {"price": 0.10, "count": 5}}}
                if action == "getNumber":
                    self.number_calls += 1
                    if self.getnumber_error:
                        raise self.getnumber_error
                    return "ACCESS_NUMBER:ACT123:+919812345678"
                if action == "getStatus":
                    return self.status_response
                if action == "setStatus":
                    return "ACCESS_CANCEL"
                if action == "getBalance":
                    return "ACCESS_BALANCE:12.5"
                raise AssertionError(action)  # pragma: no cover
            finally:
                self.inflight -= 1

        self.ctx["api"].call = fake_call
        self.user_handler = self.dp.cb("handler")
        self.search_handler = self.dp.msg("on_search_input")
        self.admin_handler = self.dp.cb("otp_admin_router")

    def balance(self):
        return self.users.find_one({"_id": USER})["balance"]

    def set_catalog(self, services=None, countries=None):
        """Swap the stubbed catalog and drop the client's five-minute cache."""
        if services is not None:
            self.services_response = dict(services)
        if countries is not None:
            self.countries_response = dict(countries)
        self.ctx["api"].clear_cache()

    def state_for(self, uid):
        """Per-user FSM state, mirroring aiogram's (chat, user) storage key."""
        return self.states.setdefault(uid, FakeState())

    async def user_click(self, cq):
        await self.user_handler(cq, self.state_for(cq.from_user.id))
        return cq.message

    async def search_input(self, text, from_id=USER, state=None):
        """Send search text; returns the rendered message (None if ignored)."""
        before = len(FakeMessage.sent)
        message = FakeMessage(from_id=from_id, text=text)
        await self.search_handler(message, state if state is not None else self.state_for(from_id))
        return FakeMessage.sent[before] if len(FakeMessage.sent) > before else None

    async def admin_click(self, data, from_id=ADMIN):
        cq = FakeCQ(data, from_id=from_id)
        await self.admin_handler(cq, FakeState())
        return cq

    async def drive_to_countries(self, app="Telegram", operator="1"):
        """Apps → app → operator; returns the country listing message."""
        msg = await self.user_click(FakeCQ("otp:open"))
        msg = await self.user_click(FakeCQ(find_cb(msg.last_markup, contains=app)))
        token = find_cb(msg.last_markup, prefix="otp:operator:").split(":")[2]
        return await self.user_click(FakeCQ(f"otp:operator:{token}:{operator}"))

    async def drive_to_country(self, app="Telegram", operator="1", country="India"):
        """Apps → app → operator → country selection; returns that message."""
        msg = await self.drive_to_countries(app=app, operator=operator)
        return await self.user_click(FakeCQ(find_cb(msg.last_markup, contains=country)))

    async def drive_to_quote(self, operator="1"):
        msg = await self.drive_to_country(operator=operator)
        return find_cb(msg.last_markup, prefix="otp:buy:"), msg

    async def buy(self):
        buy_data, _ = await self.drive_to_quote()
        cq = FakeCQ(buy_data)
        return await self.user_click(cq)


class StorefrontRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_happy_path_debits_once_and_repeat_click_is_safe(self):
        h = Harness()
        buy_data, quote_msg = await h.drive_to_quote()
        self.assertIn("₹11.00", quote_msg.last_text)
        cq = FakeCQ(buy_data)
        msg = await h.user_click(cq)
        self.assertEqual(h.balance(), 89.0)
        self.assertEqual(h.number_calls, 1)
        order = h.orders.find_one({"user_id": USER})
        self.assertEqual(order["status"], "waiting")
        self.assertEqual(order["activation_id"], "ACT123")
        self.assertIn("waiting", msg.last_text)
        # Repeated click on the same quote button: no second debit/activation.
        await h.user_click(FakeCQ(buy_data))
        self.assertEqual(h.balance(), 89.0)
        self.assertEqual(h.number_calls, 1)

    async def test_storefront_services_use_live_enabled_numeric_operator(self):
        h = Harness()
        h.operators_response = {"Operator 2": "2", "Operator 9": "9"}
        cfg.set_operator_enabled(h.settings_col, "2", False, ADMIN)
        await h.user_click(FakeCQ("otp:open"))
        self.assertEqual(
            [params for action, params in h.provider_calls if action == "getServices"],
            [{"operator": "9"}],
        )

    async def test_storefront_fails_closed_on_non_numeric_operator_response(self):
        h = Harness()
        h.operators_response = {"error": "BAD_OPERATOR"}
        msg = await h.user_click(FakeCQ("otp:open"))
        self.assertIn("No enabled numeric operator", msg.last_text)
        self.assertEqual([action for action, _ in h.provider_calls if action == "getServices"], [])

    async def test_runtime_key_changes_apply_without_restart(self):
        h = Harness()
        self.assertEqual(h.ctx["api"].key, ENV_KEY)
        cfg.set_api_key(h.settings_col, DB_KEY, ADMIN)
        self.assertEqual(h.ctx["api"].key, DB_KEY)
        cfg.remove_api_key(h.settings_col, ADMIN)
        self.assertEqual(h.ctx["api"].key, ENV_KEY)

    async def test_disabled_service_blocks_new_purchase_but_not_existing_orders(self):
        h = Harness()
        await h.buy()
        order = h.orders.find_one({"user_id": USER})
        cfg.set_enabled(h.settings_col, False, ADMIN)
        # New purchase flow blocked, including from a stale "open" button.
        msg = await h.user_click(FakeCQ("otp:open"))
        self.assertIn("temporarily disabled", msg.last_text)
        # Existing order still completes via check.
        h.status_response = "STATUS_OK:654321"
        msg = await h.user_click(FakeCQ(f"otp:check:{order['_id']}"))
        order = h.orders.find_one({"user_id": USER})
        self.assertEqual(order["status"], "completed")
        self.assertIn("654321", msg.last_text)

    async def test_operator_disabled_after_quote_blocks_buy(self):
        h = Harness()
        buy_data, _ = await h.drive_to_quote(operator="1")
        cfg.set_operator_enabled(h.settings_col, "1", False, ADMIN)
        msg = await h.user_click(FakeCQ(buy_data))
        self.assertIn("changed", msg.last_text)
        self.assertEqual(h.balance(), 100.0)
        self.assertEqual(h.number_calls, 0)

    async def test_markup_change_invalidates_stale_quote(self):
        h = Harness()
        buy_data, _ = await h.drive_to_quote()
        cfg.set_markup(h.settings_col, 50, ADMIN)
        msg = await h.user_click(FakeCQ(buy_data))
        self.assertIn("changed", msg.last_text)
        self.assertEqual(h.balance(), 100.0)
        # A fresh quote carries the new price (0.10*100*1.5 = ₹15.00).
        _, quote_msg = await h.drive_to_quote()
        self.assertIn("₹15.00", quote_msg.last_text)

    async def test_insufficient_balance_marks_unpaid_without_provider_call(self):
        h = Harness(balance=5.0)
        buy_data, _ = await h.drive_to_quote()
        msg = await h.user_click(FakeCQ(buy_data))
        self.assertIn("Insufficient", msg.last_text)
        self.assertEqual(h.number_calls, 0)
        order = h.orders.find_one({"user_id": USER})
        self.assertEqual(order["status"], "unpaid")
        user = h.users.find_one({"_id": USER})
        self.assertFalse(user.get("otp_debits"))

    async def test_ambiguous_allocation_goes_to_review_without_auto_refund(self):
        h = Harness()
        h.getnumber_error = ProviderError("Provider connection interrupted.", code="CONNECTION")
        await h.buy()
        order = h.orders.find_one({"user_id": USER})
        self.assertEqual(order["status"], "review")
        self.assertEqual(h.balance(), 89.0)  # NOT auto-refunded

    async def test_cancel_refunds_exactly_once(self):
        h = Harness()
        await h.buy()
        order = h.orders.find_one({"user_id": USER})
        await h.user_click(FakeCQ(f"otp:cancel:{order['_id']}"))
        self.assertEqual(h.balance(), 100.0)
        order = h.orders.find_one({"user_id": USER})
        self.assertEqual(order["status"], "cancelled")
        self.assertTrue(order["refunded"])
        # Double-click / replay must not credit twice.
        await h.user_click(FakeCQ(f"otp:cancel:{order['_id']}"))
        updated, refunded = await h.ctx["manual_refund_locked"](order["_id"])
        self.assertFalse(refunded)
        self.assertEqual(h.balance(), 100.0)


class OperatorRoutingTests(unittest.IsolatedAsyncioTestCase):
    """A getServices code is not routable on every operator (BAD_SERVICE)."""

    LIVE_OPERATORS = {"Operator 1": "1", "Operator 3": "3", "Operator 9": "9"}

    def dead_line(self, code="BAD_SERVICE"):
        """Storefront where WhatsApp is rejected on operator 9 only."""
        h = Harness()
        h.operators_response = dict(self.LIVE_OPERATORS)
        h.price_failures = {("wa", "9"): code}
        return h

    async def test_bad_service_offers_operators_with_live_stock_in_parallel(self):
        h = self.dead_line()
        msg = await h.drive_to_country(app="WhatsApp", operator="9")
        # The raw provider token never reaches the buyer.
        self.assertNotIn("BAD_SERVICE", msg.last_text)
        self.assertIn("not available on Operator 9", msg.last_text)
        alts = [(b.text, b.callback_data) for b in buttons(msg.last_markup)
                if (b.callback_data or "").startswith("otp:alt:")]
        self.assertEqual([text for text, _cb in alts],
                         [f"Operator {op}" for op in ("1", "2", "3", "4", "7", "8", "10")])
        # Every other admin-enabled operator was probed live with getPricesV3.
        probed = sorted(params["operator"] for action, params in h.provider_calls
                        if action == "getPricesV3")
        self.assertEqual(probed, ["1", "10", "2", "3", "4", "7", "8", "9"])
        self.assertGreaterEqual(h.peak_inflight, 2)      # probes run concurrently
        # Offering alternatives sells nothing and charges nothing.
        self.assertEqual(h.number_calls, 0)
        self.assertEqual(h.orders.count_documents({}), 0)
        self.assertEqual(h.balance(), 100.0)
        self.assertIsNone(h.users.find_one({"_id": USER}).get("otp_debits"))
        # The country list stays reachable from the alternatives screen.
        self.assertTrue(all_cbs(msg.last_markup, "otp:page:"))
        # A line switched off at runtime is refused, even from an old button.
        cfg.set_operator_enabled(h.settings_col, "3", False, ADMIN)
        refused = await h.user_click(FakeCQ(next(cb for _t, cb in alts if cb.endswith(":3"))))
        self.assertIn("currently unavailable", refused.last_text)
        self.assertEqual(h.number_calls, 0)
        self.assertEqual(h.balance(), 100.0)

    async def test_alt_operator_buy_reprices_charges_once_and_stores_the_alt_line(self):
        h = self.dead_line()
        msg = await h.drive_to_country(app="WhatsApp", operator="9")
        token = all_cbs(msg.last_markup, "otp:alt:")[0].split(":")[2]
        # The alternatives token stores the selection only — never a price.
        snapshot = h.db["otp_views"].find_one({"_id": ObjectId(token)})
        self.assertEqual({key: snapshot[key] for key in
                          ("service", "service_name", "country", "country_name")},
                         {"service": "wa", "service_name": "WhatsApp",
                          "country": "22", "country_name": "India"})
        self.assertNotIn("price", snapshot)
        quote = await h.user_click(FakeCQ(f"otp:alt:{token}:3"))
        self.assertIn("Confirm OTP Purchase", quote.last_text)
        self.assertIn("Operator: 3", quote.last_text)
        self.assertIn("₹11.00", quote.last_text)          # live re-price + markup
        buy = find_cb(quote.last_markup, prefix="otp:buy:")
        # The alt quote is stamped like any other: live cost, retail price and
        # the settings version it was priced under.
        quote_doc = h.db["otp_views"].find_one({"_id": ObjectId(buy.split(":")[2])})
        self.assertEqual((quote_doc["operator"], quote_doc["cost"], quote_doc["price"]),
                         ("3", 0.10, retail_price(0.10, 100.0, 10.0)))
        self.assertEqual(quote_doc["settings_version"],
                         cfg.get_settings(h.settings_col)["settings_version"])
        msg = await h.user_click(FakeCQ(buy))
        self.assertEqual(h.number_calls, 1)
        self.assertEqual(h.balance(), 89.0)               # charged exactly once
        order = h.orders.find_one({"user_id": USER})
        self.assertEqual(order["operator"], "3")          # the alternative line
        self.assertEqual((order["service"], order["country"], order["status"]),
                         ("wa", "22", "waiting"))
        self.assertEqual([params for action, params in h.provider_calls if action == "getNumber"],
                         [{"service": "wa", "country": "22", "operator": "3", "maxPrice": 0.10}])
        self.assertIn("waiting", msg.last_text)
        # A replayed quote cannot charge or allocate again.
        await h.user_click(FakeCQ(buy))
        self.assertEqual(h.number_calls, 1)
        self.assertEqual(h.balance(), 89.0)
        # Operators outside OPERATORS are refused without touching money.
        for bad in ("42", "0", "abc"):
            refused = await h.user_click(FakeCQ(f"otp:alt:{token}:{bad}"))
            self.assertIn("currently unavailable", refused.last_text)
        self.assertEqual(h.number_calls, 1)
        self.assertEqual(h.balance(), 89.0)

    async def test_no_working_line_shows_a_friendly_message_and_never_allocates(self):
        for code in ("BAD_SERVICE", "BAD_OPERATOR", "BAD_COUNTRY"):
            with self.subTest(code=code):
                h = self.dead_line(code)
                # Other lines answer, but without positive stock.
                h.prices_response = {"22": {"wa": {"price": 0.10, "count": 0}}}
                msg = await h.drive_to_country(app="WhatsApp", operator="9")
                self.assertEqual(
                    msg.last_text,
                    "WhatsApp is not available on Operator 9 for India. "
                    "Try another app, operator or country.")
                self.assertNotIn(code, msg.last_text)
                self.assertEqual(all_cbs(msg.last_markup, "otp:alt:"), [])
                self.assertEqual(all_cbs(msg.last_markup, "otp:buy:"), [])
                self.assertEqual(h.number_calls, 0)          # zero getNumber calls
                self.assertEqual(h.orders.count_documents({}), 0)
                self.assertEqual(h.balance(), 100.0)
                self.assertIsNone(h.users.find_one({"_id": USER}).get("otp_debits"))


class CatalogNavigationTests(unittest.IsolatedAsyncioTestCase):
    async def test_catalogs_page_twelve_items_with_a_numbered_windowed_pager(self):
        h = Harness()
        h.set_catalog(services={f"app{i:02d}": f"App {i:02d}" for i in range(30)})
        msg = await h.user_click(FakeCQ("otp:open"))
        self.assertEqual(len(item_buttons(msg.last_markup)), 12)
        self.assertIn("30 available · Page 1/3", msg.last_text)
        pager = pager_labels(msg.last_markup)
        self.assertEqual([text for text, _cb in pager], ["• 1 •", "2", "3"])
        self.assertEqual(pager[0][1], "noop")               # current page is a noop
        self.assertTrue(pager[1][1].startswith("otp:page:"))
        self.assertTrue(pager[1][1].endswith(":1"))
        self.assertEqual([text for text, _cb in step_labels(msg.last_markup)], ["➡️"])
        page2 = await h.user_click(FakeCQ(pager[1][1]))
        self.assertIn("Page 2/3", page2.last_text)
        self.assertEqual(len(item_buttons(page2.last_markup)), 12)
        self.assertEqual([text for text, _cb in pager_labels(page2.last_markup)],
                         ["1", "• 2 •", "3"])
        self.assertEqual([text for text, _cb in step_labels(page2.last_markup)], ["⬅️", "➡️"])
        page3 = await h.user_click(FakeCQ(step_labels(page2.last_markup)[1][1]))
        self.assertIn("Page 3/3", page3.last_text)
        self.assertEqual(len(item_buttons(page3.last_markup)), 6)
        self.assertEqual([text for text, _cb in step_labels(page3.last_markup)], ["⬅️"])
        # Paged indexes still resolve to the right catalog entry.
        self.assertIn("Select Operator",
                      (await h.user_click(FakeCQ(item_buttons(page3.last_markup)[0][1]))).last_text)
        # Very long catalogs get a window instead of one button per page.
        h.set_catalog(services={f"app{i:03d}": f"App {i:03d}" for i in range(200)})
        msg = await h.user_click(FakeCQ("otp:open"))
        self.assertIn("Page 1/17", msg.last_text)
        window = pager_labels(msg.last_markup)
        self.assertLess(len(window), 17)
        self.assertIn("…", [text for text, _cb in window])
        self.assertEqual(window[0][0], "• 1 •")
        self.assertEqual(window[-1][0], "17")               # last page stays reachable
        for _text, data in window:
            self.assertLessEqual(len(data.encode()), 64)
        # Country lists page the same way.
        h.set_catalog(services=dict(DEFAULT_SERVICES),
                      countries={str(i): f"Country {i:02d}" for i in range(25)})
        msg = await h.drive_to_countries()
        self.assertEqual(len(item_buttons(msg.last_markup)), 12)
        self.assertIn("25 available · Page 1/3", msg.last_text)
        self.assertEqual([text for text, _cb in pager_labels(msg.last_markup)], ["• 1 •", "2", "3"])
        # Malformed or expired ids are an expired selection, never a crash.
        for data in ("otp:page:zzz:1", "otp:select:notanobjectid:0", "otp:alt::3",
                     "otp:buy:deadbeef", "otp:search"):
            stale = await h.user_click(FakeCQ(data))
            self.assertIn("Selection expired", stale.last_text, data)
            self.assertNotIn("Unable to process", stale.last_text)
        self.assertIn("Selection expired",
                      (await h.user_click(FakeCQ(f"otp:page:{ObjectId()}:0"))).last_text)
        for data in ("otp:check:notanobjectid", "otp:cancel:zzz"):
            self.assertIn("Order not found", (await h.user_click(FakeCQ(data))).last_text, data)
        self.assertEqual(h.number_calls, 0)
        self.assertEqual(h.orders.count_documents({}), 0)


class CatalogSearchTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_filters_by_name_or_code_and_persists_across_pages(self):
        h = Harness()
        msg = await h.user_click(FakeCQ("otp:open"))
        msg = await h.user_click(FakeCQ(find_cb(msg.last_markup, prefix="otp:search:")))
        self.assertEqual(h.state_for(USER).state, "waiting_search")
        self.assertIn("Search apps", FakeMessage.sent[-1].last_text)   # the prompt
        result = await h.search_input("tele")
        self.assertEqual(item_labels(result.last_markup), ["Telegram"])
        self.assertIn("Search: <code>tele</code>", result.last_text)
        self.assertIsNone(h.state_for(USER).state)          # single-shot input
        # Case-insensitive code match, run on the filtered snapshot.
        await h.user_click(FakeCQ(find_cb(result.last_markup, prefix="otp:search:")))
        result = await h.search_input("XYZ")
        self.assertEqual(item_labels(result.last_markup), ["XYZ App"])
        # No match echoes the query and offers a retry (plus a way out).
        await h.user_click(FakeCQ(find_cb(result.last_markup, prefix="otp:search:")))
        result = await h.search_input("qqqq")
        self.assertEqual(item_buttons(result.last_markup), [])
        self.assertIn("Search: <code>qqqq</code>", result.last_text)
        self.assertIn("No matches", result.last_text)
        self.assertTrue(all_cbs(result.last_markup, "otp:search:"))
        self.assertTrue(all_cbs(result.last_markup, "otp:searchclear:"))
        await h.user_click(FakeCQ(find_cb(result.last_markup, prefix="otp:search:")))
        self.assertEqual(h.state_for(USER).state, "waiting_search")
        result = await h.search_input("instagram")
        self.assertEqual(item_labels(result.last_markup), ["Instagram"])
        cleared = await h.user_click(FakeCQ(find_cb(result.last_markup, prefix="otp:searchclear:")))
        self.assertEqual(len(item_buttons(cleared.last_markup)), 4)
        self.assertNotIn("Search:", cleared.last_text)
        # Countries match on name and on their numeric code.
        h.set_catalog(countries={"22": "India", "0": "Russia", "7": "Indonesia", "3": "China"})
        msg = await h.drive_to_countries()
        await h.user_click(FakeCQ(find_cb(msg.last_markup, prefix="otp:search:")))
        result = await h.search_input("22")
        self.assertEqual(item_labels(result.last_markup), ["India"])
        await h.user_click(FakeCQ(find_cb(result.last_markup, prefix="otp:search:")))
        result = await h.search_input("in")
        self.assertEqual(sorted(item_labels(result.last_markup)), ["China", "India", "Indonesia"])
        # A long filtered result keeps the query while paging.
        h.set_catalog(services={f"app{i:02d}": f"App {i:02d}" for i in range(30)})
        msg = await h.user_click(FakeCQ("otp:open"))
        await h.user_click(FakeCQ(find_cb(msg.last_markup, prefix="otp:search:")))
        result = await h.search_input("app")
        self.assertIn("Search: <code>app</code>", result.last_text)
        self.assertIn("30 available · Page 1/3", result.last_text)
        self.assertEqual(len(item_buttons(result.last_markup)), 12)
        page2 = await h.user_click(FakeCQ(
            next(cb for cb in all_cbs(result.last_markup, "otp:page:") if cb.endswith(":1"))))
        self.assertIn("Search: <code>app</code>", page2.last_text)
        self.assertIn("Page 2/3", page2.last_text)
        self.assertEqual(len(item_buttons(page2.last_markup)), 12)
        # Over-long queries are capped instead of stored/rendered verbatim.
        await h.user_click(FakeCQ(find_cb(page2.last_markup, prefix="otp:search:")))
        result = await h.search_input("a" * 500)
        self.assertIn("Search: <code>" + "a" * 80 + "</code>", result.last_text)
        self.assertIn("No matches", result.last_text)

    async def test_search_session_is_bound_to_the_initiating_user(self):
        h = Harness()
        msg = await h.user_click(FakeCQ("otp:open"))
        await h.user_click(FakeCQ(find_cb(msg.last_markup, prefix="otp:search:")))
        self.assertEqual(h.state_for(USER).state, "waiting_search")
        # Another user's text cannot continue this session, even holding a copy
        # of the FSM data (aiogram stores state per user/chat).
        stolen = FakeState()
        await stolen.update_data(**h.state_for(USER).data)
        await stolen.set_state("waiting_search")
        calls_before, sent_before = len(h.provider_calls), len(FakeMessage.sent)
        self.assertIsNone(await h.search_input("tele", from_id=USER2, state=stolen))
        self.assertIsNone(await h.search_input("tele", from_id=USER2))
        self.assertEqual(len(h.provider_calls), calls_before)
        self.assertEqual(len(FakeMessage.sent), sent_before)
        # The owner's own session is untouched and still works.
        self.assertEqual(h.state_for(USER).state, "waiting_search")
        result = await h.search_input("tele", from_id=USER)
        self.assertEqual(item_labels(result.last_markup), ["Telegram"])


class AdminAuthorizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_non_admin_callback_denied_and_changes_nothing(self):
        h = Harness()
        cq = await h.admin_click("otpa:togsvc", from_id=USER)
        self.assertTrue(any(a[0] == "Access denied" and a[1] for a in cq.answers))
        self.assertTrue(cfg.get_settings(h.settings_col)["enabled"])
        cq = await h.admin_click("otpa:home", from_id=USER)
        self.assertIsNone(cq.message.last_text)

    async def test_non_admin_command_and_fsm_input_rejected(self):
        h = Harness()
        msg = FakeMessage(from_id=USER, text="/otpadmin")
        replies = []
        msg.answer = lambda text, **kw: replies.append(text) or _done()
        async def _done():
            return msg
        await h.dp.msg("otpadmin_cmd")(msg, FakeState())
        self.assertIn("Access denied", replies[0])
        state = FakeState()
        await state.set_state(otp_admin.OtpAdminStates.waiting_api_key)
        key_msg = FakeMessage(from_id=USER, text=DB_KEY)
        await h.dp.msg("on_api_key_input")(key_msg, state)
        self.assertIsNone(cfg.get_settings(h.settings_col)["tempora_api_key"])
        self.assertIsNone(state.state)

    async def test_admin_home_renders_without_secrets(self):
        h = Harness()
        cq = await h.admin_click("otpa:home")
        self.assertIn("OTP Services", cq.message.last_text)
        self.assertNotIn(ENV_KEY, cq.message.last_text)


class AdminFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_provider_raw_response_is_sent_to_admin_log_without_credentials(self):
        h = Harness(admin_log_id=-100123)
        raw = '{"22":{"wa":{"price":2.99,"count":467}}}'
        await h.ctx["api"].response_logger(
            "getPricesV3", {"country": "22", "api_key": ENV_KEY}, raw, 200)
        chat_id, message = h.bot.sent[-1]
        self.assertEqual(chat_id, -100123)
        self.assertIn(raw, html.unescape(message))
        self.assertIn("HTTP <code>200</code>", message)
        self.assertNotIn(ENV_KEY, message)

    async def test_service_and_operator_toggles_persist_and_audit(self):
        h = Harness()
        await h.admin_click("otpa:togsvc")
        self.assertFalse(cfg.get_settings(h.settings_col)["enabled"])
        await h.admin_click("otpa:op:3")
        doc = cfg.get_settings(h.settings_col)
        self.assertIn("3", doc["disabled_operators"])
        audits = h.db["otp_admin_audit"].docs
        self.assertEqual({a["action"] for a in audits}, {"service_toggled", "operator_toggled"})
        self.assertNotIn(ENV_KEY, str(audits))

    async def test_markup_fsm_validation(self):
        h = Harness()
        state = FakeState()
        cq = FakeCQ("otpa:markup", from_id=ADMIN)
        await h.admin_handler(cq, state)
        self.assertEqual(state.state, otp_admin.OtpAdminStates.waiting_markup.state)
        bad = FakeMessage(from_id=ADMIN, text="abc")
        await h.dp.msg("on_markup_input")(bad, state)
        self.assertIsNone(cfg.get_settings(h.settings_col)["markup_percent"])
        self.assertEqual(state.state, otp_admin.OtpAdminStates.waiting_markup.state)
        good = FakeMessage(from_id=ADMIN, text="25")
        await h.dp.msg("on_markup_input")(good, state)
        self.assertEqual(cfg.get_settings(h.settings_col)["markup_percent"], 25.0)
        self.assertIsNone(state.state)

    async def test_key_fsm_saves_validated_key_and_deletes_message(self):
        h = Harness()
        original = otp_admin.TemporaClient
        async def _good_balance():
            return 7.5
        otp_admin.TemporaClient = lambda key: SimpleNamespace(get_balance=_good_balance)
        try:
            state = FakeState()
            cq = FakeCQ("otpa:keyset", from_id=ADMIN)
            await h.admin_handler(cq, state)
            key_msg = FakeMessage(from_id=ADMIN, text=DB_KEY)
            await h.dp.msg("on_api_key_input")(key_msg, state)
        finally:
            otp_admin.TemporaClient = original
        self.assertTrue(key_msg.deleted)
        self.assertEqual(cfg.get_settings(h.settings_col)["tempora_api_key"], DB_KEY)
        self.assertEqual(h.ctx["api"].key, DB_KEY)  # runtime pickup
        shown = " ".join((m.last_text or "") for m in FakeMessage.sent)
        self.assertNotIn(DB_KEY, shown)
        self.assertNotIn(DB_KEY, str(h.db["otp_admin_audit"]).replace(DB_KEY[-4:], ""))
        audit = h.db["otp_admin_audit"].find_one({"action": "key_set"})
        self.assertNotIn(DB_KEY, str(audit))

    async def test_key_fsm_rejects_bad_key(self):
        h = Harness()
        original = otp_admin.TemporaClient

        async def bad_balance():
            raise ProviderError("Provider unavailable.", code="BAD_KEY")

        otp_admin.TemporaClient = lambda key: SimpleNamespace(get_balance=bad_balance)
        try:
            state = FakeState()
            await state.set_state(otp_admin.OtpAdminStates.waiting_api_key)
            await h.dp.msg("on_api_key_input")(FakeMessage(from_id=ADMIN, text=DB_KEY), state)
        finally:
            otp_admin.TemporaClient = original
        self.assertIsNone(cfg.get_settings(h.settings_col)["tempora_api_key"])

    async def test_remove_key_confirmation_is_single_use_and_admin_bound(self):
        h = Harness()
        cfg.set_api_key(h.settings_col, DB_KEY, ADMIN)
        cq = await h.admin_click("otpa:keyrm")
        self.assertIn("environment", cq.message.last_text.lower())
        token_cb = find_cb(cq.message.last_markup, prefix="otpa:cf:")
        # Another admin cannot spend someone else's confirmation token.
        await h.admin_click(token_cb, from_id=ADMIN2)
        self.assertEqual(cfg.get_settings(h.settings_col)["tempora_api_key"], DB_KEY)
        # Fresh token spent by its owner works exactly once.
        cq = await h.admin_click("otpa:keyrm")
        token_cb = find_cb(cq.message.last_markup, prefix="otpa:cf:")
        await h.admin_click(token_cb)
        self.assertIsNone(cfg.get_settings(h.settings_col)["tempora_api_key"])
        self.assertEqual(h.ctx["api"].key, ENV_KEY)
        cq = await h.admin_click(token_cb)
        self.assertIn("expired or was already used", cq.message.last_text)

    async def test_orders_dashboard_filters_and_detail_privacy(self):
        h = Harness()
        base = {"user_id": USER, "service": "tg", "service_name": "Telegram",
                "country": "22", "country_name": "India", "operator": "1",
                "price": 11.0, "cost": 0.1, "created_at": datetime.utcnow()}
        h.orders.insert_one({**base, "status": "waiting", "number": "+919812345678",
                             "activation_id": "ACT9"})
        h.orders.insert_one({**base, "status": "completed", "otp": "112233",
                             "number": "+919800000000", "activation_id": "ACT8"})
        cq = await h.admin_click("otpa:ords:w:0")
        listed = [b.text for b in buttons(cq.message.last_markup)]
        self.assertEqual(len([t for t in listed if "Telegram" in t]), 1)
        everything = (cq.message.last_text or "") + " ".join(listed)
        self.assertNotIn("+919812345678", everything)
        self.assertNotIn("112233", everything)
        detail_cb = find_cb(cq.message.last_markup, prefix="otpa:ord:")
        cq = await h.admin_click(detail_cb)
        self.assertIn("+919812345678", cq.message.last_text)  # admin detail only
        self.assertIn("ACT9", cq.message.last_text)

    async def test_admin_provider_cancel_refunds_once(self):
        h = Harness()
        await h.buy()
        order = h.orders.find_one({"user_id": USER})
        cq = await h.admin_click(f"otpa:ocancel:{order['_id']}")
        token_cb = find_cb(cq.message.last_markup, prefix="otpa:cf:")
        await h.admin_click(token_cb)
        self.assertEqual(h.balance(), 100.0)
        order = h.orders.find_one({"user_id": USER})
        self.assertEqual(order["status"], "cancelled")
        self.assertTrue(order["refunded"])


class CatalogAdminTests(unittest.IsolatedAsyncioTestCase):
    async def test_catalog_refresh_and_app_picker_use_live_enabled_operator(self):
        h = Harness()
        h.operators_response = {"Operator 4": "4", "Operator 7": 7}
        cq = await h.admin_click("otpa:refreshcat")
        self.assertIn("operator 4", cq.message.last_text)
        self.assertEqual(
            [params for action, params in h.provider_calls if action == "getServices"],
            [{"operator": "4"}],
        )
        self.assertEqual(
            [params for action, params in h.provider_calls if action == "getCountries"],
            [{"operator": "4"}],
        )
        h.ctx["api"].clear_cache()
        await h.admin_click("otpa:popadd")
        self.assertEqual(
            [params for action, params in h.provider_calls if action == "getServices"],
            [{"operator": "4"}, {"operator": "4"}],
        )

    async def test_catalog_refresh_fails_closed_without_live_numeric_operator(self):
        h = Harness()
        h.operators_response = {"Operator 3": "three"}
        cq = await h.admin_click("otpa:refreshcat")
        self.assertIn("no enabled numeric operator", cq.message.last_text.lower())
        self.assertEqual([action for action, _ in h.provider_calls if action == "getServices"], [])


class ReconciliationTests(unittest.IsolatedAsyncioTestCase):
    async def _review_order(self, h):
        h.getnumber_error = ProviderError("Provider connection interrupted.", code="CONNECTION")
        await h.buy()
        h.getnumber_error = None
        return h.orders.find_one({"user_id": USER})

    async def test_manual_refund_requires_reason_and_confirmation(self):
        h = Harness()
        order = await self._review_order(h)
        state = FakeState()
        cq = FakeCQ(f"otpa:orefund:{order['_id']}", from_id=ADMIN)
        await h.admin_handler(cq, state)
        self.assertEqual(state.state, otp_admin.OtpAdminStates.waiting_reason.state)
        self.assertEqual(h.balance(), 89.0)  # nothing refunded yet
        reason_msg = FakeMessage(from_id=ADMIN, text="No activation in provider account")
        await h.dp.msg("on_reason_input")(reason_msg, state)
        self.assertEqual(h.balance(), 89.0)  # still needs explicit confirmation
        confirm_msg = FakeMessage.sent[-1]
        token_cb = find_cb(confirm_msg.last_markup, prefix="otpa:cf:")
        await h.admin_click(token_cb)
        self.assertEqual(h.balance(), 100.0)
        order = h.orders.find_one({"user_id": USER})
        self.assertEqual(order["status"], "cancelled")
        self.assertEqual(order["reconcile_reason"], "No activation in provider account")
        audit = h.db["otp_admin_audit"].find_one({"action": "order_manual_refund"})
        self.assertEqual(audit["reason"], "No activation in provider account")
        # Replay protection: another attempt cannot credit again.
        _, refunded = await h.ctx["manual_refund_locked"](order["_id"])
        self.assertFalse(refunded)
        self.assertEqual(h.balance(), 100.0)

    async def test_attach_activation_verifies_with_provider(self):
        h = Harness()
        order = await self._review_order(h)
        h.status_response = "STATUS_WAIT"
        state = FakeState()
        cq = FakeCQ(f"otpa:oattach:{order['_id']}", from_id=ADMIN)
        await h.admin_handler(cq, state)
        attach_msg = FakeMessage(from_id=ADMIN, text="ACT777\n+919812300000")
        await h.dp.msg("on_activation_input")(attach_msg, state)
        order = h.orders.find_one({"user_id": USER})
        self.assertEqual(order["status"], "waiting")
        self.assertEqual(order["activation_id"], "ACT777")
        audit = h.db["otp_admin_audit"].find_one({"action": "order_activation_attached"})
        self.assertNotIn("ACT777", str(audit))  # sensitive ids scrubbed from audit
        # Buyer flow resumes normally.
        h.status_response = "STATUS_OK:246810"
        await h.user_click(FakeCQ(f"otp:check:{order['_id']}"))
        self.assertEqual(h.orders.find_one({"user_id": USER})["status"], "completed")

    async def test_attach_rejects_cancelled_activation(self):
        h = Harness()
        order = await self._review_order(h)
        h.status_response = "STATUS_CANCEL"
        state = FakeState()
        await state.update_data(order_id=str(order["_id"]))
        await state.set_state(otp_admin.OtpAdminStates.waiting_activation)
        await h.dp.msg("on_activation_input")(
            FakeMessage(from_id=ADMIN, text="ACT_DEAD"), state)
        order = h.orders.find_one({"user_id": USER})
        self.assertEqual(order["status"], "review")
        self.assertNotEqual(order.get("activation_id"), "ACT_DEAD")



class WiringTests(unittest.TestCase):
    def _src(self, name):
        return (pathlib.Path(__file__).resolve().parents[1] / name).read_text(encoding="utf-8")

    def test_admin_panel_button_and_registration(self):
        src = self._src("bot.py")
        self.assertIn('text="📲 OTP Services", callback_data="otpa:home"', src)
        self.assertIn("register_otp_admin_handlers(", src)
        self.assertIn('BotCommand(command="otpadmin"', src)

    def test_callback_lengths_fit_telegram_limit(self):
        samples = [
            "otpa:ord:" + "a" * 24 + ":r:123",
            "otpa:pickadd:" + "a" * 24 + ":9999",
            "otpa:cf:" + "a" * 24,
            "otpa:orefund:" + "a" * 24,
        ]
        for data in samples:
            self.assertLessEqual(len(data.encode()), 64, data)

    def test_no_hardcoded_secret_in_new_modules(self):
        for name in ("otp_admin.py", "otp_settings.py"):
            src = self._src(name)
            self.assertNotIn("api_key=", src.replace("api_key\": ", ""), name)


if __name__ == "__main__":
    unittest.main()
