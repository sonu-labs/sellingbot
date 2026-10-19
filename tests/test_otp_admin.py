"""Offline tests for the OTP admin panel, runtime settings and money safety.

Everything runs against an in-memory fake MongoDB and a stubbed provider:
no network, no real API key and — critically — no real paid activation.
"""

import copy
import pathlib
import sys
import unittest
from datetime import datetime
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from bson import ObjectId  # noqa: E402

import otp_settings as cfg  # noqa: E402
import otp_admin  # noqa: E402
from otp import ProviderError, TemporaClient, parse_balance, register_otp_handlers  # noqa: E402
from otp_admin import register_otp_admin_handlers  # noqa: E402

ADMIN = 999
ADMIN2 = 998
USER = 111
ENV_KEY = "env-secret-key-1234"
DB_KEY = "db-secret-key-9876"


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
        self.state = state.state if state is not None else None

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

    def __init__(self, balance=100.0):
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
        register_otp_handlers(
            dp=self.dp, bot=self.bot, db=self.db, users_col=self.users,
            api_key=ENV_KEY, rate=100.0, markup=10.0,
            settings_col=self.settings_col, registry=self.ctx)
        register_otp_admin_handlers(
            dp=self.dp, bot=self.bot, db=self.db, users_col=self.users,
            settings_col=self.settings_col, admin_ids=[ADMIN, ADMIN2],
            env_api_key=ENV_KEY, env_markup=10.0, rate=100.0, otp_ctx=self.ctx)

        async def fake_call(action, **params):
            if action == "getServices":
                return {"tg": "Telegram", "wa": "WhatsApp", "ig": "Instagram", "xyz": "XYZ App"}
            if action == "getCountries":
                return {"22": "India", "0": "Russia"}
            if action == "getPricesV3":
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

        self.ctx["api"].call = fake_call
        self.user_handler = self.dp.cb("handler")
        self.admin_handler = self.dp.cb("otp_admin_router")

    def balance(self):
        return self.users.find_one({"_id": USER})["balance"]

    async def user_click(self, cq):
        await self.user_handler(cq)
        return cq.message

    async def admin_click(self, data, from_id=ADMIN):
        cq = FakeCQ(data, from_id=from_id)
        await self.admin_handler(cq, FakeState())
        return cq

    async def drive_to_quote(self, operator="1"):
        cq = FakeCQ("otp:open")
        msg = await self.user_click(cq)
        data = find_cb(msg.last_markup, contains="Telegram")
        cq = FakeCQ(data)
        msg = await self.user_click(cq)
        token = find_cb(msg.last_markup, prefix="otp:operator:").split(":")[2]
        cq = FakeCQ(f"otp:operator:{token}:{operator}")
        msg = await self.user_click(cq)
        data = find_cb(msg.last_markup, contains="India")
        cq = FakeCQ(data)
        msg = await self.user_click(cq)
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
