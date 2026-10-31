"""End-to-end tests for tempora.py, allapps.py (Buy OTPs All Apps), and server4.py (Telegram Server 4)."""

import asyncio
import copy
import json
import pathlib
import sys
import time
import unittest
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from aiogram import Dispatcher  # noqa: E402
from aiogram.filters import Command  # noqa: E402
from aiogram.fsm.storage.memory import MemoryStorage  # noqa: E402
from aiogram.fsm.storage.base import StorageKey  # noqa: E402
from aiogram.fsm.context import FSMContext  # noqa: E402

import allapps  # noqa: E402
import server4  # noqa: E402
import i18n  # noqa: E402
import loyalty  # noqa: E402
from tempora import (  # noqa: E402
    ProviderUnavailable,
    TemporaClient,
    cost_inr,
    popular_services,
    retail_price,
    search_services,
    sid_of,
)


# ---------------------------------------------------------------------------
# Lightweight in-memory MongoDB + Aiogram test harness
# ---------------------------------------------------------------------------
def _matches(doc, flt):
    for key, cond in (flt or {}).items():
        val = doc.get(key)
        if isinstance(cond, dict):
            for op, operand in cond.items():
                if op == "$gte":
                    if val is None or val < operand:
                        return False
                elif op == "$gt":
                    if val is None or val <= operand:
                        return False
                elif op == "$lt":
                    if val is None or val >= operand:
                        return False
                elif op == "$in":
                    if val not in operand:
                        return False
                elif op == "$ne":
                    if val == operand:
                        return False
        else:
            if val != cond:
                return False
    return True


class FakeCollection:
    def __init__(self):
        self.docs = []
        self._seq = 0

    def find_one(self, flt=None, proj=None):
        for doc in self.docs:
            if _matches(doc, flt):
                return copy.deepcopy(doc)
        return None

    def find(self, flt=None, proj=None):
        return [copy.deepcopy(d) for d in self.docs if _matches(d, flt)]

    def count_documents(self, flt=None):
        return len(self.find(flt))

    def insert_one(self, doc):
        d = copy.deepcopy(doc)
        if "_id" not in d:
            self._seq += 1
            d["_id"] = self._seq
        self.docs.append(d)
        return SimpleNamespace(inserted_id=d["_id"])

    def update_one(self, flt, upd, upsert=False):
        for doc in self.docs:
            if _matches(doc, flt):
                self._apply(doc, upd)
                return SimpleNamespace(modified_count=1, matched_count=1)
        if upsert:
            base = {k: v for k, v in (flt or {}).items() if not isinstance(v, dict)}
            self._apply(base, upd)
            self.insert_one(base)
            return SimpleNamespace(modified_count=1, matched_count=0)
        return SimpleNamespace(modified_count=0, matched_count=0)

    def find_one_and_update(self, flt, upd):
        for doc in self.docs:
            if _matches(doc, flt):
                before = copy.deepcopy(doc)
                self._apply(doc, upd)
                return before
        return None

    def aggregate(self, pipeline):
        rows = list(self.docs)
        for stage in pipeline:
            if "$match" in stage:
                rows = [r for r in rows if _matches(r, stage["$match"])]
            elif "$group" in stage:
                rev = sum(float(r.get("price", 0) or 0) for r in rows)
                cost = sum(float(r.get("cost_inr", 0) or 0) for r in rows)
                rev_c = sum(float(r.get("price", 0) or 0) for r in rows if float(r.get("cost_inr", 0) or 0) > 0)
                return [{"_id": None, "n": len(rows), "rev": rev, "rev_c": rev_c, "cost": cost}] if rows else []
        return rows

    @staticmethod
    def _apply(doc, upd):
        if "$set" in upd:
            doc.update(copy.deepcopy(upd["$set"]))
        if "$inc" in upd:
            for k, delta in upd["$inc"].items():
                doc[k] = float(doc.get(k, 0.0)) + float(delta)


class FakeDB:
    def __init__(self):
        self.cols = {}

    def __getitem__(self, name):
        return self.cols.setdefault(name, FakeCollection())


class FakeBot:
    def __init__(self):
        self.sent = []
        self.edits = []

    async def get_me(self):
        return SimpleNamespace(id=777, username="TGAStoreAPIBot")

    async def send_message(self, chat_id, text, reply_markup=None, parse_mode=None):
        item = {"chat_id": chat_id, "text": text, "reply_markup": reply_markup, "parse_mode": parse_mode}
        self.sent.append(item)
        return FakeMessage(chat_id=chat_id, text=text, reply_markup=reply_markup, bot=self)

    async def edit_message_text(self, text, chat_id=None, message_id=None, reply_markup=None, parse_mode=None):
        item = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "reply_markup": reply_markup,
            "parse_mode": parse_mode,
        }
        self.edits.append(item)
        return item


class FakeMessage:
    _next_id = 100

    def __init__(self, chat_id=1001, user_id=1001, text="", reply_markup=None, bot=None):
        FakeMessage._next_id += 1
        self.message_id = FakeMessage._next_id
        self.chat = SimpleNamespace(id=chat_id)
        self.from_user = SimpleNamespace(id=user_id, username="tester", full_name="Test User")
        self.text = text
        self.caption = None
        self.entities = None
        self.caption_entities = None
        self.reply_markup = reply_markup
        self.edits = []
        self.answers = []
        self._bot = bot

    async def edit_text(self, text, reply_markup=None, parse_mode=None):
        self.text = text
        self.reply_markup = reply_markup
        rec = {"text": text, "reply_markup": reply_markup, "parse_mode": parse_mode}
        self.edits.append(rec)
        if self._bot:
            self._bot.edits.append({**rec, "chat_id": self.chat.id, "message_id": self.message_id})
        return self

    async def answer(self, text, reply_markup=None, parse_mode=None):
        child = FakeMessage(chat_id=self.chat.id, user_id=self.from_user.id, text=text, reply_markup=reply_markup, bot=self._bot)
        self.answers.append(child)
        return child

    async def delete(self):
        return True


class FakeCallback:
    def __init__(self, data, user_id=1001, message=None, bot=None):
        self.id = f"cb-{time.monotonic()}"
        self.data = data
        self.from_user = SimpleNamespace(id=user_id, username="tester", full_name="Test User")
        self.message = message or FakeMessage(chat_id=user_id, user_id=user_id, bot=bot)
        self.toasts = []

    async def answer(self, text=None, show_alert=False):
        self.toasts.append((text, show_alert))


def _buttons(markup):
    if not markup or not getattr(markup, "inline_keyboard", None):
        return []
    return [b for row in markup.inline_keyboard for b in row]


def _find_handler(dp: Dispatcher, *, callback_data=None, command=None):
    if callback_data is not None:
        ev = SimpleNamespace(data=callback_data, from_user=SimpleNamespace(id=1001))
        obs = dp.callback_query
    else:
        ev = SimpleNamespace(text=f"/{command}", from_user=SimpleNamespace(id=1001))
        obs = dp.message
    for h in obs.handlers:
        return h
    raise AssertionError("No handler registered")


async def _invoke_callback(dp: Dispatcher, storage: MemoryStorage, bot: FakeBot, data: str, user_id: int = 1001, message=None):
    call = FakeCallback(data=data, user_id=user_id, message=message, bot=bot)
    state = FSMContext(storage=storage, key=StorageKey(bot_id=777, chat_id=call.message.chat.id, user_id=user_id))
    for h in dp.callback_query.handlers:
        matched = True
        for f in h.filters:
            res = await f.call(call)
            if not res:
                matched = False
                break
        if matched:
            kwargs = {"state": state} if "state" in h.params else {}
            await h.call(call, **kwargs)
            return call
    raise AssertionError(f"No callback handler matched {data!r}")


async def _invoke_command(dp: Dispatcher, storage: MemoryStorage, bot: FakeBot, text: str, user_id: int = 1001):
    msg = FakeMessage(chat_id=user_id, user_id=user_id, text=text, bot=bot)
    state = FSMContext(storage=storage, key=StorageKey(bot_id=777, chat_id=msg.chat.id, user_id=user_id))
    cmd_token = text.lstrip("/").split()[0]
    for h in dp.message.handlers:
        matched = True
        for f in h.filters:
            if isinstance(f.callback, Command):
                if cmd_token not in f.callback.commands:
                    matched = False
                    break
                continue
            f_kw = {}
            if "bot" in f.params:
                f_kw["bot"] = bot
            if "raw_state" in f.params:
                f_kw["raw_state"] = await state.get_state()
            res = await f.call(msg, **f_kw)
            if not res:
                matched = False
                break
        if matched:
            kwargs = {"state": state} if "state" in h.params else {}
            await h.call(msg, **kwargs)
            return msg
    raise AssertionError(f"No message handler matched {text!r}")


# ---------------------------------------------------------------------------
# Unit tests for tempora.py
# ---------------------------------------------------------------------------
class TemporaUnitTests(unittest.IsolatedAsyncioTestCase):
    def test_sid_and_popular_and_pricing_contracts(self):
        self.assertEqual(sid_of("Telegram"), "tg")
        self.assertEqual(sid_of("WhatsApp"), "wa")
        long_sid = sid_of("Super Long Enterprise Verification Platform Name 2026 Edition")
        self.assertLessEqual(len(long_sid), 24)
        self.assertTrue(long_sid.isalnum())

        cat = {
            "tg": {"name": "Telegram", "ops": {"1": "edg", "9": "ehbg"}},
            "wa": {"name": "WhatsApp", "ops": {"1": "tss"}},
            "whatsappbusiness": {"name": "WhatsApp Business", "ops": {"1": "wab1"}},
            "go": {"name": "Google", "ops": {"1": "go1"}},
            "ig": {"name": "Instagram", "ops": {"1": "ig1"}},
            "openaichatgpt": {"name": "OpenAI / ChatGPT", "ops": {"1": "oa1"}},
            "twitterx": {"name": "Twitter / X", "ops": {"1": "tw1"}},
            "fb": {"name": "Facebook", "ops": {"1": "fb1"}},
            "tt": {"name": "TikTok", "ops": {"1": "tt1"}},
            "discord": {"name": "Discord", "ops": {"1": "dc1"}},
        }
        pop = popular_services(cat)
        # Must return (label, sid) pairs so ``for label, sid in pop:`` never unpacks dicts
        for label, sid in pop:
            self.assertIsInstance(label, str)
            self.assertIn(sid, cat)
        top_sids = [sid for _lbl, sid in pop[:9]]
        self.assertNotIn("whatsappbusiness", top_sids)
        self.assertEqual(top_sids[0], "tg")
        self.assertEqual(top_sids[1], "wa")

        # WhatsApp Business remains discoverable via search_services
        found = [sid for sid, _e in search_services(cat, "whatsapp business")]
        self.assertIn("whatsappbusiness", found)

        # Pricing: $0.255 * 100 INR/USD = ₹25.50 cost; +100% markup = ₹51.00
        cfg = {"unit_to_inr": 100.0, "markup_pct": 100.0, "min_profit": 10.0}
        self.assertEqual(cost_inr(0.255, cfg), 25.5)
        self.assertEqual(retail_price(0.255, cfg), 51.0)
        # Min-profit floor: $0.02 * 100 = ₹2.00 cost; +100% = ₹4.00, floored to ₹2 + ₹10 = ₹12.00
        self.assertEqual(retail_price(0.02, cfg), 12.0)

    async def test_tempora_client_multi_operator_catalog_v3_prices_and_lifecycle(self):
        client = TemporaClient("test-api-key")

        async def fake_raw(action: str, *, lane: str = "rates", **params) -> str:
            if action == "getOperators":
                return json.dumps({"operators": ["1", "9"]})
            if action == "getServices":
                op = params.get("operator")
                if op == "1":
                    return json.dumps({"edg": "Telegram", "tss": "WhatsApp", "go1": "Google"})
                if op == "9":
                    return json.dumps({"ehbg": "Telegram", "tpgs": "WhatsApp", "nf9": "Netflix"})
                return "BAD_OPERATOR"
            if action == "getCountries":
                return json.dumps({
                    "countries": [
                        {"id": 22, "eng": "India"},
                        {"id": 12, "eng": "USA"},
                    ]
                })
            if action == "getPricesV3":
                cid = params.get("country")
                svc = params.get("service")
                op = params.get("operator")
                if not cid:
                    return "BAD_COUNTRY"
                if cid == "22" and op == "1" and svc == "edg":
                    # Cheap tier has 0 stock; in-stock tier is 0.1275
                    return json.dumps({
                        "22": {
                            "edg": {
                                "price": [0.05, 0.1275],
                                "count": [0, 34],
                                "providers": {
                                    "1": {"count": 0, "price": [0.05], "providerIds": "11"},
                                    "2": {"count": 34, "price": [0.1275], "providerIds": "22"},
                                },
                            }
                        }
                    })
                if cid == "22" and op == "9" and svc == "ehbg":
                    return json.dumps({"22": {"ehbg": {"price": 0.18, "count": 12}}})
                if cid == "12" and op == "1" and svc == "edg":
                    return json.dumps({"12": {"edg": {"price": 0.30, "count": 90}}})
                return "{}"
            if action in ("getPrices", "getTopCountriesByService"):
                return "BAD_ACTION"
            if action == "getNumber":
                if params.get("service") == "edg" and params.get("country") == "22":
                    return "ACCESS_NUMBER:555001:+919876543210"
                return "NO_NUMBERS"
            if action == "getStatus":
                return "STATUS_OK:654321"
            if action == "setStatus":
                if params.get("status") == "8":
                    return "ACCESS_CANCEL"
                return "ACCESS_ACTIVATION"
            if action == "getBalance":
                return "ACCESS_BALANCE:42.75"
            return "ERROR"

        client._raw_request = fake_raw
        self.assertIs(client._cache, client.cache)

        cat = await client.services()
        self.assertIn("tg", cat)
        self.assertEqual(cat["tg"]["name"], "Telegram")
        self.assertEqual(cat["tg"]["ops"], {"1": "edg", "9": "ehbg"})
        self.assertIn("netflix", cat)

        countries = await client.countries()
        self.assertEqual(countries, {"22": "India", "12": "USA"})

        # service_offers returns list[dict] (never a tuple) and skips 0-stock cheap tier
        s_rows = await client.service_offers("tg", full=False)
        self.assertIsInstance(s_rows, list)
        india_op1 = next(r for r in s_rows if r["country_id"] == "22" and r["operator"] == "1")
        self.assertAlmostEqual(india_op1["cost"], 0.1275)
        self.assertEqual(india_op1["count"], 34)
        self.assertEqual(india_op1["provider_ids"], "22")

        c_rows = await client.country_offers("tg", "22")
        self.assertEqual(len(c_rows), 2)
        self.assertEqual([r["operator"] for r in c_rows], ["1", "9"])

        bal = await client.balance()
        self.assertEqual(bal, 42.75)

        st, act_id, phone = await client.get_number("edg", "22", "1", max_price=0.20, provider_ids="22")
        self.assertEqual((st, act_id, phone), ("ok", "555001", "919876543210"))

        otp_state, otp_code = await client.get_status("555001")
        self.assertEqual((otp_state, otp_code), ("otp", "654321"))

        cancel_res = await client.cancel("555001")
        self.assertEqual(cancel_res, "cancelled")


# ---------------------------------------------------------------------------
# End-to-end flow tests for All Apps (allapps.py) & Server 4 (server4.py)
# ---------------------------------------------------------------------------
def _install_mock_provider(target_client: TemporaClient):
    state = {"otp_reply": "STATUS_OK:884422", "get_number_reply": "ACCESS_NUMBER:900100:919988776655"}

    async def fake_raw(action: str, *, lane: str = "rates", **params) -> str:
        if action == "getOperators":
            return json.dumps({"operators": ["1", "9"]})
        if action == "getServices":
            op = params.get("operator")
            if op == "1":
                return json.dumps({
                    "edg": "Telegram",
                    "tss": "WhatsApp",
                    "wab": "WhatsApp Business",
                    "go1": "Google",
                    "ig1": "Instagram",
                })
            if op == "9":
                return json.dumps({
                    "ehbg": "Telegram",
                    "tpgs": "WhatsApp",
                    "oa9": "OpenAI",
                })
            return "{}"
        if action == "getCountries":
            return json.dumps({"22": "India", "12": "USA"})
        if action == "getPricesV3":
            cid = params.get("country")
            svc = params.get("service")
            op = params.get("operator")
            if not cid:
                # Simulate bulk country response on op 1 & 9
                if svc in ("edg", "tss"):
                    # 0.1275 * 100 * 2.0 = ₹25.50 (non-integer retail price to test decimal confirmation!)
                    return json.dumps({
                        "22": {svc: {"price": 0.1275, "count": 25}},
                        "12": {svc: {"price": 0.3525, "count": 60}},
                    })
                if svc in ("ehbg", "tpgs"):
                    return json.dumps({
                        "22": {svc: {"price": 0.1775, "count": 15}},
                        "12": {svc: {"price": 0.3225, "count": 40}},
                    })
                return "{}"
            if cid == "22" and op == "1" and svc in ("edg", "tss"):
                return json.dumps({"22": {svc: {"price": 0.1275, "count": 25}}})
            if cid == "22" and op == "9" and svc in ("ehbg", "tpgs"):
                return json.dumps({"22": {svc: {"price": 0.1775, "count": 15}}})
            if cid == "12" and op == "9" and svc == "ehbg":
                return json.dumps({"12": {"ehbg": {"price": 0.3225, "count": 40}}})
            return "{}"
        if action == "getNumber":
            return state["get_number_reply"]
        if action == "getStatus":
            return state["otp_reply"]
        if action == "setStatus":
            return "ACCESS_CANCEL" if params.get("status") == "8" else "ACCESS_ACTIVATION"
        if action == "getBalance":
            return "ACCESS_BALANCE:19.50"
        return "{}"

    target_client.clear_cache()
    target_client._raw_request = fake_raw
    return state


class AllAppsAndServer4FlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        allapps.clear_s3_rate_cache()
        server4.clear_s4_rate_cache()
        allapps.last_click_time.clear()
        server4.last_click_time.clear()
        self.db = FakeDB()
        self.users = self.db["users"]
        self.orders = self.db["orders"]
        self.users.insert_one({"_id": 1001, "balance": 200.0, "username": "buyer1", "language": "en", "currency": "INR"})
        self.bot = FakeBot()
        self.storage = MemoryStorage()
        self.dp = Dispatcher(storage=self.storage)

        allapps.register_server3_handlers(
            dp=self.dp,
            bot=self.bot,
            db=self.db,
            users_col=self.users,
            orders_col=self.orders,
            is_admin_func=lambda uid: uid == 9999,
            fmt_curr_func=lambda v: f"₹{float(v):.2f}",
        )
        server4.register_server4_handlers(
            dp=self.dp,
            bot=self.bot,
            db=self.db,
            users_col=self.users,
            orders_col=self.orders,
            is_admin_func=lambda uid: uid == 9999,
            fmt_curr_func=lambda v: f"₹{float(v):.2f}",
        )
        self.s3_state = _install_mock_provider(allapps.client)
        self.s4_state = _install_mock_provider(server4.client)

    async def test_allapps_full_purchase_and_otp_delivery_with_decimal_price(self):
        # 1. Open "Buy OTPs (All Apps)" root menu — previously crashed with AttributeError
        root_call = await _invoke_callback(self.dp, self.storage, self.bot, "s3_user_root")
        self.assertIn("TGA Store", root_call.message.text)
        cbs = [b.callback_data for b in _buttons(root_call.message.reply_markup)]
        self.assertIn("s3s:tg:0", cbs)
        self.assertIn("s3s:wa:0", cbs)

        # 2. WhatsApp direct callback (from Buy WhatsApp -> Agree & Continue)
        wp_call = await _invoke_callback(self.dp, self.storage, self.bot, "s3_open_wp")
        await asyncio.sleep(0.05)
        self.assertIn("WhatsApp", wp_call.message.text)

        # 3. Select India for WhatsApp -> Select Operator 1 -> Confirm ₹25.50 (decimal!)
        op_call = await _invoke_callback(self.dp, self.storage, self.bot, "s3c:wa:22", message=wp_call.message)
        self.assertIn("Select Operator", op_call.message.text)
        op_cbs = [b.callback_data for b in _buttons(op_call.message.reply_markup)]
        self.assertIn("s3o:wa:22:1", op_cbs)

        cfm_call = await _invoke_callback(self.dp, self.storage, self.bot, "s3o:wa:22:1", message=op_call.message)
        self.assertIn("₹25.50", cfm_call.message.text)
        buy_btn_cb = next(b.callback_data for b in _buttons(cfm_call.message.reply_markup) if b.callback_data.startswith("s3b:"))
        self.assertEqual(buy_btn_cb, "s3b:wa:22:1:25.50")
        self.assertLessEqual(len(buy_btn_cb.encode("utf-8")), 64)

        # 4. Click Purchase — must NOT loop with "Price changed" on decimal ₹25.50!
        buy_call = await _invoke_callback(self.dp, self.storage, self.bot, buy_btn_cb, message=cfm_call.message)
        await asyncio.sleep(0.15)  # allow otp_worker to poll STATUS_OK:884422 and deliver OTP

        user_after = self.users.find_one({"_id": 1001})
        self.assertAlmostEqual(user_after["balance"], 200.0 - 25.50, places=2)

        completed_orders = self.orders.find({"user_id": 1001, "status": "purchased"})
        self.assertEqual(len(completed_orders), 1)
        self.assertEqual(completed_orders[0]["otp"], "884422")
        self.assertEqual(completed_orders[0]["service"], "WhatsApp")
        self.assertEqual(completed_orders[0]["country"], "India")
        self.assertIsNotNone(completed_orders[0].get("created_at"))
        self.assertTrue(any("884422" in e["text"] for e in self.bot.edits))
        # Verify standardized purchase logs in both public sales & admin log channels
        self.assertGreaterEqual(len(self.bot.sent), 2)
        self.assertTrue(all("New Virtual Number Purchased" in s["text"] for s in self.bot.sent[-2:]))

    async def test_allapps_no_stock_refunds_user_100_percent(self):
        self.s3_state["get_number_reply"] = "NO_NUMBERS"
        msg = FakeMessage(chat_id=1001, user_id=1001, bot=self.bot)
        await _invoke_callback(self.dp, self.storage, self.bot, "s3c:tg:22", message=msg)
        await _invoke_callback(self.dp, self.storage, self.bot, "s3o:tg:22:1", message=msg)
        await _invoke_callback(self.dp, self.storage, self.bot, "s3b:tg:22:1:25.50", message=msg)

        user_after = self.users.find_one({"_id": 1001})
        self.assertAlmostEqual(user_after["balance"], 200.0, places=2)
        self.assertEqual(self.orders.count_documents({"user_id": 1001}), 0)

    async def test_server4_open_navigation_purchase_and_maintenance_sync(self):
        # 1. Open Server 4 ("New Numbers (Telegram)") via s4_open
        s4_call = await _invoke_callback(self.dp, self.storage, self.bot, "s4_open")
        self.assertIn("Telegram", s4_call.message.text)
        s4_cbs = [b.callback_data for b in _buttons(s4_call.message.reply_markup)]
        self.assertIn("s4c:tg:12", s4_cbs)
        # Back button on root country list must return to "buy" (Servers menu), NOT self-loop to s4_user_root!
        self.assertIn("buy", s4_cbs)
        self.assertNotIn("s4_user_root", s4_cbs)

        # 2. Choose USA (cid=12) -> Operator 9 ($0.3225 * 100 * 2 = ₹64.50) -> Purchase -> OTP delivered
        op_call = await _invoke_callback(self.dp, self.storage, self.bot, "s4c:tg:12", message=s4_call.message)
        self.assertIn("Select Operator", op_call.message.text)

        cfm_call = await _invoke_callback(self.dp, self.storage, self.bot, "s4o:tg:12:9", message=op_call.message)
        self.assertIn("₹64.50", cfm_call.message.text)
        buy_cb_data = next(b.callback_data for b in _buttons(cfm_call.message.reply_markup) if b.callback_data.startswith("s4b:"))
        self.assertEqual(buy_cb_data, "s4b:tg:12:9:64.50")

        await _invoke_callback(self.dp, self.storage, self.bot, buy_cb_data, message=cfm_call.message)
        await asyncio.sleep(0.15)

        user_after = self.users.find_one({"_id": 1001})
        self.assertAlmostEqual(user_after["balance"], 200.0 - 64.50, places=2)
        s4_orders = self.orders.find({"user_id": 1001, "server": "Server 4"})
        self.assertEqual(len(s4_orders), 1)
        self.assertEqual(s4_orders[0]["otp"], "884422")

        # 3. Admin /server4 command works and maintenance sync keeps server_config.s4 as string ("maintenance"/"active")
        adm_msg = await _invoke_command(self.dp, self.storage, self.bot, "/server4", user_id=9999)
        self.assertTrue(any("Server 4 Admin Panel" in a.text for a in adm_msg.answers))

        toggle_call = await _invoke_callback(self.dp, self.storage, self.bot, "s4_adm_toggle_maint", user_id=9999)
        srv_cfg = self.db["settings"].find_one({"_id": "server_config"})
        self.assertEqual(srv_cfg["s4"], "maintenance")
        self.assertIsInstance(srv_cfg["s4"], str)

        # User trying s4_open while disabled gets maintenance alert
        blocked_call = await _invoke_callback(self.dp, self.storage, self.bot, "s4_open", user_id=1001)
        self.assertTrue(any("maintenance" in (t[0] or "").lower() for t in blocked_call.toasts))

        # Re-enable Server 4 -> back to "active"
        await _invoke_callback(self.dp, self.storage, self.bot, "s4_adm_toggle_maint", user_id=9999, message=toggle_call.message)
        self.assertEqual(self.db["settings"].find_one({"_id": "server_config"})["s4"], "active")

    async def test_no_duplicate_slash_commands_between_allapps_and_server4(self):
        # Verify every command is registered at most once across both modules
        seen_cmds = []
        for h in self.dp.message.handlers:
            for f in h.filters:
                if isinstance(f.callback, Command):
                    seen_cmds.extend(f.callback.commands)
        self.assertEqual(len(seen_cmds), len(set(seen_cmds)), f"Duplicate commands found: {seen_cmds}")
        self.assertIn("server3", seen_cmds)
        self.assertIn("server4", seen_cmds)
        self.assertIn("s4", seen_cmds)

        # /server3 should hit All Apps admin panel; /server4 should hit Server 4 admin panel
        m3 = await _invoke_command(self.dp, self.storage, self.bot, "/server3", user_id=9999)
        m4 = await _invoke_command(self.dp, self.storage, self.bot, "/server4", user_id=9999)
        self.assertIn("Server 3 Admin Panel", m3.answers[0].text)
        self.assertIn("Server 4 Admin Panel", m4.answers[0].text)

        # Helper modules must never raise AttributeError
        self.assertTrue(i18n.t("wp_maint"))
        await loyalty.aspend(1001, 10.0, "s3")
        await loyalty.arefund(1001, 10.0, "s3")

    async def test_services_and_countries_render_10x2_grid_of_20_items(self):
        # Populate 25 services and 25 countries so page 0 must render 10 rows x 2 columns = 20 items
        svcs = {"edg": "Telegram", "tss": "WhatsApp"}
        for idx in range(1, 25):
            svcs[f"app{idx}"] = f"App {idx:02d}"
        ctries = {str(i): f"Country {i:02d}" for i in range(1, 26)}

        async def fake_raw_25(action: str, *, lane: str = "rates", **params) -> str:
            if action == "getOperators":
                return json.dumps({"operators": ["1"]})
            if action == "getServices":
                return json.dumps(svcs)
            if action == "getCountries":
                return json.dumps(ctries)
            if action == "getPricesV3" and not params.get("country"):
                svc = params.get("service", "edg")
                return json.dumps({cid: {svc: {"price": 0.2, "count": 10}} for cid in ctries})
            return "{}"

        allapps.clear_s3_rate_cache()
        allapps.client.clear_cache()
        allapps.client._raw_request = fake_raw_25

        # 1. All-OTP Services root: first 10 rows are 2 columns each = 20 service buttons
        svc_call = await _invoke_callback(self.dp, self.storage, self.bot, "s3_user_root")
        svc_rows = [
            row for row in svc_call.message.reply_markup.inline_keyboard
            if all((b.callback_data or "").startswith("s3s:") for b in row)
        ]
        self.assertEqual(len(svc_rows), 10)
        self.assertTrue(all(len(row) == 2 for row in svc_rows))
        self.assertEqual(sum(len(row) for row in svc_rows), 20)

        # 2. All-OTP Countries list: first 10 rows are 2 columns each = 20 country buttons
        ctry_call = await _invoke_callback(self.dp, self.storage, self.bot, "s3a:tg:0", message=svc_call.message)
        ctry_rows = [
            row for row in ctry_call.message.reply_markup.inline_keyboard
            if all((b.callback_data or "").startswith("s3c:") for b in row)
        ]
        self.assertEqual(len(ctry_rows), 10)
        self.assertTrue(all(len(row) == 2 for row in ctry_rows))
        self.assertEqual(sum(len(row) for row in ctry_rows), 20)


class BotStartupImportTests(unittest.TestCase):
    def test_bot_and_config_import_cleanly_from_fresh_process(self):
        """Regression test: importing config.py + bot.py on Render must never fail with ImportError."""
        import os
        import subprocess

        env = dict(os.environ)
        env.update({
            "BOT_TOKEN": "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi",
            "ADMIN_IDS": "123456789",
            "MONGO_URI": "mongodb://localhost:27017",
        })
        code = (
            "import unittest.mock as m\n"
            "from tests.test_allapps_and_server4 import FakeDB\n"
            "class DummyMongo:\n"
            "    def __init__(self, *a, **kw): self._d = {}\n"
            "    def __getitem__(self, k): return self._d.setdefault(k, FakeDB())\n"
            "with m.patch('pymongo.MongoClient', DummyMongo):\n"
            "    import config, bot\n"
            "    assert hasattr(config, 'OTP_MARKUP_PERCENT')\n"
            "    assert hasattr(config, 'TEMPORASMS_MARKUP_PCT')\n"
            "    assert set(bot.dp.resolve_used_update_types()) >= {'message', 'callback_query'}\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(pathlib.Path(__file__).resolve().parent.parent),
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(proc.returncode, 0, f"bot.py import failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")


class RestrictedFlagsAndRichTransportTests(unittest.IsolatedAsyncioTestCase):
    def test_restricted_emoji_flags_replace_legacy_pack_everywhere(self):
        import rich_messages as rm

        root = pathlib.Path(__file__).resolve().parent.parent
        raw_items = json.loads((root / "EMOJI_IDS" / "RestrictedEmoji.txt").read_text(encoding="utf-8"))
        expected_a2 = {}
        for item in raw_items:
            em = item.get("emoji") or ""
            cid = str(item.get("custom_emoji_id") or "")
            if len(em) == 2 and all(0x1F1E6 <= ord(ch) <= 0x1F1FF for ch in em):
                a2 = "".join(chr(ord("A") + ord(ch) - 0x1F1E6) for ch in em)
                expected_a2[a2] = cid

        self.assertGreaterEqual(len(expected_a2), 240)
        for a2, cid in expected_a2.items():
            self.assertEqual(rm.RESTRICTED_FLAG_EMOJI_IDS.get(a2), cid)
            self.assertEqual(allapps.CUSTOM_FLAG_EMOJI_IDS.get(a2), cid)
            self.assertEqual(server4.CUSTOM_FLAG_EMOJI_IDS.get(a2), cid)
            flag_uni = "".join(chr(0x1F1E6 + ord(c) - ord("A")) for c in a2)
            self.assertEqual(rm.emoji_id(flag_uni), cid)

        # UK constituent subdivision flags must resolve to RestrictedEmoji.txt IDs
        self.assertIn("5229192892710402006", allapps.flag_for("England"))
        self.assertIn("5226852401822057871", allapps.flag_for("Scotland"))
        self.assertIn("5228957348113955582", allapps.flag_for("Wales"))
        self.assertIn("5229192892710402006", server4.flag_for("England"))

        # provider.py must have no hardcoded API key
        prov_src = (root / "provider.py").read_text(encoding="utf-8")
        self.assertNotRegex(prov_src, r'os\.getenv\(\s*"TEMPORASMS_API_KEY"\s*,\s*"[A-Za-z0-9]{16,}"')

        # Virtual-number modules use "New Virtual Number Purchased"; Server 1/2/3 keep their original log text with <h2> Rich header
        for mod_name in ("allapps.py", "server4.py"):
            src = (root / mod_name).read_text(encoding="utf-8")
            self.assertIn("<h2>✅ New Virtual Number Purchased</h2>", src, mod_name)
        bot_src = (root / "bot.py").read_text(encoding="utf-8")
        self.assertIn("<h2>✅ New Number Purchase Successful</h2>", bot_src)
        self.assertIn("<h2>📢 New Purchase Alert</h2>", bot_src)
        self.assertNotIn("<pre><u>✅", bot_src)
        s3_src = (root / "server3.py").read_text(encoding="utf-8")
        self.assertIn("<h2>✅ New Number Purchase Successful</h2>", s3_src)

        # 20 items per page (10 left, 10 right) for both services and countries
        self.assertEqual(allapps.SERVICES_PER_PAGE, 20)
        self.assertEqual(allapps.COUNTRIES_PER_PAGE, 20)
        self.assertEqual(server4.SERVICES_PER_PAGE, 20)
        self.assertEqual(server4.COUNTRIES_PER_PAGE, 20)

    def test_rich_html_never_injects_tags_inside_code_or_pre(self):
        import rich_messages as rm

        sample = (
            "<h2>📊 Status</h2>"
            "<blockquote><code>🟢 Running</code> · <pre><u>✅ <b>Alert</b></u></pre></blockquote>"
        )
        rich = rm.to_rich_html(sample)
        for inner in __import__("re").findall(r"<(?:code|pre)\b[^>]*>(.*?)</(?:code|pre)>", rich, flags=__import__("re").S):
            self.assertNotIn("<tg-emoji", inner)
            self.assertNotIn("<b>", inner)
            self.assertNotIn("<u>", inner)

    async def test_unreachable_admin_log_chat_suppresses_repeated_errors(self):
        from aiogram.exceptions import TelegramBadRequest
        from aiogram.methods import SendMessage
        from otp_admin import register_otp_admin_handlers

        calls = []

        class FailingLogBot:
            async def send_message(self, chat_id, text, **kwargs):
                calls.append(chat_id)
                raise TelegramBadRequest(
                    method=SendMessage(chat_id=chat_id, text="x"),
                    message="Bad Request: chat not found",
                )

            async def send_document(self, chat_id, document, **kwargs):
                calls.append(chat_id)
                raise TelegramBadRequest(
                    method=SendMessage(chat_id=chat_id, text="x"),
                    message="Bad Request: chat not found",
                )

        captured = {}

        class DummyAPI:
            def set_response_logger(self, cb):
                captured["logger"] = cb

            def clear_cache(self):
                pass

        dp = Dispatcher(storage=MemoryStorage())
        db = FakeDB()
        register_otp_admin_handlers(
            dp=dp,
            bot=FailingLogBot(),
            db=db,
            users_col=db["users"],
            settings_col=db["settings"],
            admin_ids=[9999],
            env_api_key="test-key-1234",
            env_markup=50.0,
            rate=100.0,
            otp_ctx={"api": DummyAPI(), "orders": db["otp_orders"], "views": db["otp_views"], "locks": {}},
            admin_log_id="-1003441797589",
        )
        logger_cb = captured["logger"]
        # First call catches chat not found cleanly without raising; second call within backoff makes 0 API calls
        await logger_cb("getServices", {"operator": "1"}, '{"edg":"Telegram"}', 200, "catalog")
        await logger_cb("getCountries", {"operator": "1"}, '{"22":"India"}', 200, "catalog")
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
