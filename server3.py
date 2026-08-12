import asyncio
import re
import aiohttp
import json
import time
from html import escape
from datetime import datetime, timezone
from bson import ObjectId
from aiogram import F, types
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, CopyTextButton
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

TEMPORASMS_BASE = "https://api.temporasms.com/stubs/handler_api.php"
LZT_BASE = "https://prod-api.lzt.market"
LZT_SEARCH_URL = f"{LZT_BASE}/telegram"
LZT_ME_URL = f"{LZT_BASE}/me"

LZT_PER_PAGE = 10
COUNTRY_PER_PAGE = 20     
MAX_RETRY_REQUEST_ATTEMPTS = 15
RETRY_DELAY_SECONDS = 1.5
RATE_LIMIT_RETRY_DELAY = 3.0
LIVE_PRICE_CACHE = {}

def flag_emoji(alpha2):
    try:
        return "".join(chr(0x1F1E6 + ord(c) - ord('A')) for c in alpha2.upper())
    except Exception:
        return "🏳️"

LZT_COUNTRY_MAP = {
    "ABW": ("AW", "Aruba"), "AFG": ("AF", "Afghanistan"), "AGO": ("AO", "Angola"),
    "ALB": ("AL", "Albania"), "AND": ("AD", "Andorra"), "ARE": ("AE", "UAE"),
    "ARG": ("AR", "Argentina"), "ARM": ("AM", "Armenia"), "ATG": ("AG", "Antigua & Barbuda"),
    "AUS": ("AU", "Australia"), "AUT": ("AT", "Austria"), "AZE": ("AZ", "Azerbaijan"),
    "BDI": ("BI", "Burundi"), "BEL": ("BE", "Belgium"), "BEN": ("BJ", "Benin"),
    "BFA": ("BF", "Burkina Faso"), "BGD": ("BD", "Bangladesh"), "BGR": ("BG", "Bulgaria"),
    "BHR": ("BH", "Bahrain"), "BHS": ("BS", "Bahamas"), "BIH": ("BA", "Bosnia"),
    "BLR": ("BY", "Belarus"), "BLZ": ("BZ", "Belize"), "BOL": ("BO", "Bolivia"),
    "BRA": ("BR", "Brazil"), "BRB": ("BB", "Barbados"), "BRN": ("BN", "Brunei"),
    "BTN": ("BT", "Bhutan"), "BWA": ("BW", "Botswana"), "CAF": ("CF", "Central African Rep."),
    "CAN": ("CA", "Canada"), "CHE": ("CH", "Switzerland"), "CHL": ("CL", "Chile"),
    "CIV": ("CI", "Ivory Coast"), "CMR": ("CM", "Cameroon"), "COD": ("CD", "DR Congo"),
    "COG": ("CG", "Congo"), "COL": ("CO", "Colombia"), "COM": ("KM", "Comoros"),
    "CPV": ("CV", "Cape Verde"), "CRI": ("CR", "Costa Rica"), "CUB": ("CU", "Cuba"),
    "CYP": ("CY", "Cyprus"), "CZE": ("CZ", "Czech Republic"), "DEU": ("DE", "Germany"),
    "DJI": ("DJ", "Djibouti"), "DMA": ("DM", "Dominica"), "DNK": ("DK", "Denmark"),
    "DOM": ("DO", "Dominican Rep."), "DZA": ("DZ", "Algeria"), "ECU": ("EC", "Ecuador"),
    "EGY": ("EG", "Egypt"), "ERI": ("ER", "Eritrea"), "ESP": ("ES", "Spain"),
    "EST": ("EE", "Estonia"), "ETH": ("ET", "Ethiopia"), "FIN": ("FI", "Finland"),
    "FJI": ("FJ", "Fiji"), "FRA": ("FR", "France"), "FSM": ("FM", "Micronesia"),
    "GAB": ("GA", "Gabon"), "GBR": ("GB", "United Kingdom"), "GEO": ("GE", "Georgia"),
    "GHA": ("GH", "Ghana"), "GIB": ("GI", "Gibraltar"), "GIN": ("GN", "Guinea"),
    "GLP": ("GP", "Guadeloupe"), "GMB": ("GM", "Gambia"), "GNB": ("GW", "Guinea-Bissau"),
    "GNQ": ("GQ", "Equatorial Guinea"), "GRC": ("GR", "Greece"), "GRD": ("GD", "Grenada"),
    "GTM": ("GT", "Guatemala"), "GUF": ("GF", "French Guiana"), "GUM": ("GU", "Guam"),
    "GUY": ("GY", "Guyana"), "HKG": ("HK", "Hong Kong"), "HND": ("HN", "Honduras"),
    "HRV": ("HR", "Croatia"), "HTI": ("HT", "Haiti"), "HUN": ("HU", "Hungary"),
    "IDN": ("ID", "Indonesia"), "IMN": ("IM", "Isle of Man"), "IND": ("IN", "India"),
    "IRL": ("IE", "Ireland"), "IRN": ("IR", "Iran"), "IRQ": ("IQ", "Iraq"),
    "ISL": ("IS", "Iceland"), "ISR": ("IL", "Israel"), "ITA": ("IT", "Italy"),
    "JAM": ("JM", "Jamaica"), "JOR": ("JO", "Jordan"), "JPN": ("JP", "Japan"),
    "KAZ": ("KZ", "Kazakhstan"), "KEN": ("KE", "Kenya"), "KGZ": ("KG", "Kyrgyzstan"),
    "KHM": ("KH", "Cambodia"), "KIR": ("KI", "Kiribati"), "KNA": ("KN", "Saint Kitts"),
    "KOR": ("KR", "South Korea"), "KWT": ("KW", "Kuwait"), "LAO": ("LA", "Laos"),
    "LBN": ("LB", "Lebanon"), "LBR": ("LR", "Liberia"), "LBY": ("LY", "Libya"),
    "LCA": ("LC", "Saint Lucia"), "LIE": ("LI", "Liechtenstein"), "LKA": ("LK", "Sri Lanka"),
    "LSO": ("LS", "Lesotho"), "LTU": ("LT", "Lithuania"), "LUX": ("LU", "Luxembourg"),
    "LVA": ("LV", "Latvia"), "MAC": ("MO", "Macau"), "MAR": ("MA", "Morocco"),
    "MCO": ("MC", "Monaco"), "MDA": ("MD", "Moldova"), "MDG": ("MG", "Madagascar"),
    "MDV": ("MV", "Maldives"), "MEX": ("MX", "Mexico"), "MHL": ("MH", "Marshall Islands"),
    "MKD": ("MK", "North Macedonia"), "MLI": ("ML", "Mali"), "MLT": ("MT", "Malta"),
    "MMR": ("MM", "Myanmar"), "MNE": ("ME", "Montenegro"), "MNG": ("MN", "Mongolia"),
    "MNP": ("MP", "N. Mariana Isl."), "MOZ": ("MZ", "Mozambique"), "MRT": ("MR", "Mauritania"),
    "MTQ": ("MQ", "Martinique"), "MUS": ("MU", "Mauritius"), "MWI": ("MW", "Malawi"),
    "MYS": ("MY", "Malaysia"), "NAM": ("NA", "Namibia"), "NER": ("NE", "Niger"),
    "NGA": ("NG", "Nigeria"), "NIC": ("NI", "Nicaragua"), "NLD": ("NL", "Netherlands"),
    "NOR": ("NO", "Norway"), "NPL": ("NP", "Nepal"), "NRU": ("NR", "Nauru"),
    "NZL": ("NZ", "New Zealand"), "OMN": ("OM", "Oman"), "PAK": ("PK", "Pakistan"),
    "PAN": ("PA", "Panama"), "PER": ("PE", "Peru"), "PHL": ("PH", "Philippines"),
    "PLW": ("PW", "Palau"), "PNG": ("PG", "Papua New Guinea"), "POL": ("PL", "Poland"),
    "PRI": ("PR", "Puerto Rico"), "PRK": ("KP", "North Korea"), "PRT": ("PT", "Portugal"),
    "PRY": ("PY", "Paraguay"), "PSE": ("PS", "Palestine"), "QAT": ("QA", "Qatar"),
    "REU": ("RE", "Reunion"), "ROU": ("RO", "Romania"), "RUS": ("RU", "Russia"),
    "RWA": ("RW", "Rwanda"), "SAU": ("SA", "Saudi Arabia"), "SDN": ("SD", "Sudan"),
    "SEN": ("SN", "Senegal"), "SGP": ("SG", "Singapore"), "SLB": ("SB", "Solomon Islands"),
    "SLE": ("SL", "Sierra Leone"), "SLV": ("SV", "El Salvador"), "SMR": ("SM", "San Marino"),
    "SOM": ("SO", "Somalia"), "SRB": ("RS", "Serbia"), "SSD": ("SS", "South Sudan"),
    "STP": ("ST", "Sao Tome"), "SUR": ("SR", "Suriname"), "SVK": ("SK", "Slovakia"),
    "SVN": ("SI", "Slovenia"), "SWE": ("SE", "Sweden"), "SWZ": ("SZ", "Eswatini"),
    "SYC": ("SC", "Seychelles"), "SYR": ("SY", "Syria"), "TCD": ("TD", "Chad"),
    "TGO": ("TG", "Togo"), "THA": ("TH", "Thailand"), "TJK": ("TJ", "Tajikistan"),
    "TKM": ("TM", "Turkmenistan"), "TLS": ("TL", "Timor-Leste"), "TON": ("TO", "Tonga"),
    "TTO": ("TT", "Trinidad & Tobago"), "TUN": ("TN", "Tunisia"), "TUR": ("TR", "Turkey"),
    "TUV": ("TV", "Tuvalu"), "TWN": ("TW", "Taiwan"), "TZA": ("TZ", "Tanzania"),
    "UGA": ("UG", "Uganda"), "UKR": ("UA", "Ukraine"), "URY": ("UY", "Uruguay"),
    "USA": ("US", "USA"), "UZB": ("UZ", "Uzbekistan"), "VAT": ("VA", "Vatican City"),
    "VCT": ("VC", "St. Vincent & Grenadines"), "VEN": ("VE", "Venezuela"), "VNM": ("VN", "Vietnam"),
    "VUT": ("VU", "Vanuatu"), "WSM": ("WS", "Samoa"), "XKX": ("XK", "Kosovo"),
    "YEM": ("YE", "Yemen"), "ZAF": ("ZA", "South Africa"), "ZMB": ("ZM", "Zambia"),
    "ZWE": ("ZW", "Zimbabwe")
}

LZT_ALL_COUNTRIES = [
    {"code": a2, "flag": flag_emoji(a2), "name": name}
    for code, (a2, name) in sorted(LZT_COUNTRY_MAP.items(), key=lambda kv: kv[1][1])
]


class Server3Admin(StatesGroup):
    waiting_service_code = State()
    waiting_country_name = State()
    waiting_flag = State()
    waiting_price_inr = State()
    waiting_max_price = State()
    waiting_lzt_token = State()
    waiting_lzt_margin = State()


class UserSearch(StatesGroup):
    waiting_country_query = State()


class Server3Edit(StatesGroup):
    waiting_new_price = State()
    waiting_new_cap = State()


def category_meta(category):
    return {"wa": {"name": "WhatsApp", "emoji": "💬", "toggle_field": "s3_wa_status"}}.get(
        category, {"name": category.upper(), "emoji": "📦", "toggle_field": "s3_unknown_status"}
    )


def mask_phone(number):
    if len(number) <= 6:
        return number + "••••"
    return number[:6] + "••••"


def register_server3_handlers(dp, bot, db, users_col, orders_col, settings_col,
                               admin_ids, ADMINLOG, SALES, TEMPORASMS_API_KEY,
                               BOTUSER, CHANNEL, exchange_rate=95.0):

    s3_catalog = db["s3_catalog"]
    lzt_orders = db["lzt_orders"]
    _operator_cache = {"data": None, "fetched_at": 0}

    def is_admin(uid):
        return uid in admin_ids

    def get_settings():
        return settings_col.find_one({"_id": "server_config"}) or {}

    def s3_debug_enabled():
        return get_settings().get("s3_debug_mode", "on") == "on"

    def mask_key(url, key):
        return url.replace(key, "•••" + key[-4:]) if key else url

    async def log_admin_debug(action_label, url, raw, key=""):
        if not s3_debug_enabled():
            return
        try:
            await bot.send_message(
                ADMINLOG,
                f"🛰️ <b>API Debug — {action_label}</b>\n\n"
                f"<b>Request:</b>\n<code>{escape(mask_key(str(url), key))}</code>\n\n"
                f"<b>Raw Response:</b>\n<pre>{escape(str(raw)[:1500])}</pre>",
                parse_mode="HTML"
            )
        except Exception:
            pass

    async def log_admin(text, raw=None):
        full = text
        if raw:
            full += f"\n\n📡 <b>Raw Response</b>\n<pre>{escape(str(raw)[:1500])}</pre>"
        try:
            await bot.send_message(ADMINLOG, full[:4090], parse_mode="HTML")
        except Exception:
            pass

    # ============================================================
    # =====================  TEMPORASMS (WA) CORE  =================
    # ============================================================

    async def s3_api_call(action_label, url):
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(url, timeout=10) as resp:
                    raw = await resp.text()
            except Exception as e:
                raw = f"Exception: {e}"
        await log_admin_debug(action_label, url, raw, TEMPORASMS_API_KEY)
        return raw

    async def get_tempora_balance():
        url = f"{TEMPORASMS_BASE}?api_key={TEMPORASMS_API_KEY}&action=getBalance"
        text = await s3_api_call("TemporaSMS: Get Balance", url)
        if "ACCESS_BALANCE:" in text:
            try: return float(text.split(":")[1])
            except: return 0.0
        return 0.0

    async def get_all_operators():
        now = time.time()
        if _operator_cache["data"] and (now - _operator_cache["fetched_at"]) < 3600:
            return _operator_cache["data"]
        url = f"{TEMPORASMS_BASE}?api_key={TEMPORASMS_API_KEY}&action=getOperators"
        text = await s3_api_call("TemporaSMS: Fetch Operators", url)
        try:
            data = json.loads(text)
            ops = sorted(set(data.values()), key=lambda x: int(x))
            _operator_cache["data"] = ops
            _operator_cache["fetched_at"] = now
            return ops
        except Exception:
            return _operator_cache["data"] or ["1", "2", "4", "7", "8", "9", "10", "11", "12", "15", "16"]

    async def get_country_name(country_code):
        url = f"{TEMPORASMS_BASE}?api_key={TEMPORASMS_API_KEY}&action=getCountries&operator=1"
        text = await s3_api_call("TemporaSMS: Fetch Country Name", url)
        try:
            return json.loads(text).get(country_code)
        except Exception:
            return None

    async def check_live_combo(service_code, country_code, operator):
        url = (
            f"{TEMPORASMS_BASE}?api_key={TEMPORASMS_API_KEY}"
            f"&action=getPricesV3&country={country_code}&service={service_code}&operator={operator}"
        )
        text = await s3_api_call(f"TemporaSMS: Validate (C:{country_code} S:{service_code} Op:{operator})", url)
        cleaned = text.strip()
        KNOWN_ERRORS = {"BAD_ACTION", "BAD_SERVICE", "BAD_OPERATOR", "BAD_COUNTRY", "BAD_KEY",
                         "USER_BANNED", "ERROR", "UNDER_DEVELOPMENT", "TOO_MANY_REQUESTS"}
        if cleaned in KNOWN_ERRORS:
            return False, cleaned, None
        try:
            data = json.loads(text)
        except Exception:
            return False, "INVALID_RESPONSE", None
        price = data.get(country_code, {}).get(service_code, {}).get("price")
        if not price:
            return False, "NO_PRICE_DATA", None
        return True, None, price

    async def validate_with_case_variants(country_code, operator, raw_code):
        candidates = []
        for c in (raw_code, raw_code.lower(), raw_code.upper(), raw_code.capitalize()):
            if c not in candidates:
                candidates.append(c)
        last_error = "UNKNOWN"
        for candidate in candidates:
            is_valid, error_code, price = await check_live_combo(candidate, country_code, operator)
            if is_valid:
                return candidate, price, None
            last_error = error_code
        return None, None, last_error

    # ============================================================
    # =====================  LZT MARKET (TG) CORE  =================
    # ============================================================

    def get_lzt_token():
        return get_settings().get("lzt_token")

    def get_lzt_margin():
        return get_settings().get("lzt_margin_percent", 20)

    def usd_to_sell_inr(usd_price):
        margin = get_lzt_margin()
        return round(usd_price * exchange_rate * (1 + margin / 100), 2)

    async def lzt_request(method, url, params=None, json_body=None, action_label="LZT Call", silent=True):
        """Central LZT caller: handles auth header, 429 backoff, and debug logging."""
        token = get_lzt_token()
        headers = {"Authorization": f"Bearer {token}"}
        if json_body is not None:
            headers["Content-Type"] = "application/json"

        for attempt in range(3):  # basic 429 resilience
            async with aiohttp.ClientSession() as session:
                try:
                    if method == "GET":
                        async with session.get(url, headers=headers, params=params, timeout=30) as resp:
                            status = resp.status
                            try: data = await resp.json()
                            except Exception: data = {"errors": [await resp.text()]}
                    else:
                        async with session.post(url, headers=headers, json=json_body, timeout=60) as resp:
                            status = resp.status
                            try: data = await resp.json()
                            except Exception: data = {"errors": [await resp.text()]}
                except Exception as e:
                    status, data = 0, {"errors": [f"Exception: {e}"]}

            # 🤫 TRICK: Agar silent=True hai, toh debug message nahi jayega!
            if not silent:
                await log_admin_debug(f"{action_label} (attempt {attempt+1})", url, data, token or "")

            if status == 429:
                await asyncio.sleep(RATE_LIMIT_RETRY_DELAY)
                continue
            return status, data

        return status, data

    async def get_lzt_balance_id():
        status, data = await lzt_request("GET", LZT_ME_URL, action_label="LZT: Get Me/Balance")
        if status == 200 and isinstance(data, dict):
            if "balance_id" in data:
                return str(data["balance_id"])
            if isinstance(data.get("balance"), dict) and "id" in data["balance"]:
                return str(data["balance"]["id"])
        return "balance"

    # ============================================================
    # =====================  ADMIN DASHBOARD  =====================
    # ============================================================

    @dp.message(Command("manage3"))
    async def cmd_manage3(msg: Message):
        if not is_admin(msg.from_user.id): return await msg.answer("❌ Unauthorized.")

        s = get_settings()
        s3_wa = s.get("s3_wa_status", "on")
        lzt_enabled = s.get("lzt_enabled", False)
        usd_disp = s.get("s3_usd_display", "off")
        debug_mode = s.get("s3_debug_mode", "on")
        lzt_margin = get_lzt_margin()
        tempora_bal = await get_tempora_balance()

        kb = InlineKeyboardBuilder()
        kb.row(
            InlineKeyboardButton(text=f"💬 WhatsApp: {'🟢 ON' if s3_wa == 'on' else '🔴 OFF'}", callback_data="s3_toggle:wa"),
            InlineKeyboardButton(text=f"📱 Telegram (LZT): {'🟢 ON' if lzt_enabled else '🔴 OFF'}", callback_data="s3_toggle:lzt")
        )
        kb.row(InlineKeyboardButton(text=f"💲 USD Display: {'🟢 ON' if usd_disp == 'on' else '🔴 OFF'}", callback_data="s3_toggle:usd"))
        kb.row(InlineKeyboardButton(text=f"🐛 API Debug Log: {'🟢 ON' if debug_mode == 'on' else '🔴 OFF'}", callback_data="s3_toggle:debug"))
        kb.row(
            InlineKeyboardButton(text="➕ Add WA Catalog", callback_data="s3_add_item"),
            InlineKeyboardButton(text="✏️ Edit WA Catalog", callback_data="s3_edit_list")
        )
        kb.row(
            InlineKeyboardButton(text="🗑️ Remove WA Catalog", callback_data="s3_rem_item"),
            InlineKeyboardButton(text="🔍 WA Margin Audit", callback_data="s3_price_check")
        )
        kb.row(
            InlineKeyboardButton(text="🔑 Set LZT Token", callback_data="lzt_settoken"),
            InlineKeyboardButton(text="💵 Set LZT Margin %", callback_data="lzt_setmargin")
        )
        kb.row(InlineKeyboardButton(text="🧪 Test LZT Connection", callback_data="lzt_test"))
        kb.row(InlineKeyboardButton(text="❌ Close Dashboard", callback_data="delete_msg"))

        text = (
            f"<b>🤖 Server 3 Manager</b>\n\n"
            f"<blockquote>"
            f"💬 <b>WhatsApp (TemporaSMS):</b> {tempora_bal:.4f} USDT\n"
            f"📱 <b>Telegram (LZT):</b> Margin {lzt_margin}%\n\n"
            f"⚙️ USD Display: {usd_disp.upper()} | 🐛 Debug: {debug_mode.upper()}"
            f"</blockquote>"
        )
        await msg.answer(text, parse_mode="HTML", reply_markup=kb.as_markup())

    @dp.callback_query(F.data.startswith("s3_toggle:"))
    async def s3_toggles(cq: CallbackQuery):
        if not is_admin(cq.from_user.id): return
        action = cq.data.split(":")[1]
        if action == "lzt":
            current = get_settings().get("lzt_enabled", False)
            settings_col.update_one({"_id": "server_config"}, {"$set": {"lzt_enabled": not current}}, upsert=True)
        else:
            field_map = {"wa": "s3_wa_status", "usd": "s3_usd_display", "debug": "s3_debug_mode"}
            field = field_map.get(action, "s3_usd_display")
            default_on = {"s3_wa_status", "s3_debug_mode"}
            current = get_settings().get(field, "on" if field in default_on else "off")
            settings_col.update_one({"_id": "server_config"}, {"$set": {field: "off" if current == "on" else "on"}}, upsert=True)
        await cq.answer("✅ Updated!", show_alert=False)
        await cq.message.delete()
        await cmd_manage3(cq.message)

    @dp.callback_query(F.data == "lzt_settoken")
    async def lzt_settoken(cq: CallbackQuery, state: FSMContext):
        if not is_admin(cq.from_user.id): return
        await state.set_state(Server3Admin.waiting_lzt_token)
        await cq.message.edit_text("🔑 <b>Send your LZT Market API token:</b>", parse_mode="HTML")
        await cq.answer()

    @dp.message(StateFilter(Server3Admin.waiting_lzt_token))
    async def lzt_settoken_save(msg: Message, state: FSMContext):
        token = msg.text.strip()
        settings_col.update_one({"_id": "server_config"}, {"$set": {"lzt_token": token}}, upsert=True)
        await state.clear()
        try: await msg.delete()
        except Exception: pass
        await msg.answer("✅ LZT token saved. Test it with 🧪 Test LZT Connection.")

    @dp.callback_query(F.data == "lzt_setmargin")
    async def lzt_setmargin(cq: CallbackQuery, state: FSMContext):
        if not is_admin(cq.from_user.id): return
        await state.set_state(Server3Admin.waiting_lzt_margin)
        await cq.message.edit_text("💵 <b>Enter profit margin percentage:</b>\n<i>(Ex: 20 = LZT cost + 20%)</i>", parse_mode="HTML")
        await cq.answer()

    @dp.message(StateFilter(Server3Admin.waiting_lzt_margin))
    async def lzt_setmargin_save(msg: Message, state: FSMContext):
        try: margin = float(msg.text.strip())
        except ValueError: return await msg.answer("❌ Send a valid number.")
        settings_col.update_one({"_id": "server_config"}, {"$set": {"lzt_margin_percent": margin}}, upsert=True)
        await state.clear()
        await msg.answer(f"✅ Margin set to {margin}%")

    @dp.callback_query(F.data == "lzt_test")
    async def lzt_test(cq: CallbackQuery):
        if not is_admin(cq.from_user.id): return
        await cq.answer("Testing...", show_alert=False)
        status, data = await lzt_request("GET", LZT_SEARCH_URL, params={"country[]": "IN", "order_by": "price_to_up", "currency": "usd", "page": 1}, action_label="LZT: Connection Test")
        if status == 200 and "items" in data:
            count = len(data["items"])
            await bot.send_message(cq.from_user.id, f"✅ LZT connection OK. India search returned {count} live items right now.\n(Check ADMINLOG for full raw response.)")
        else:
            errs = data.get("errors", ["Unknown"]) if isinstance(data, dict) else ["Unknown"]
            await bot.send_message(cq.from_user.id, f"❌ LZT test failed (status {status}): {'; '.join(errs)}\n(Full raw response sent to ADMINLOG.)")

    # ============================================================
    # ===================  WA: ADD CATALOG FLOW  ===================
    # ============================================================

    @dp.callback_query(F.data == "s3_add_item")
    async def s3_add_start(cq: CallbackQuery, state: FSMContext):
        if not is_admin(cq.from_user.id): return
        await state.update_data(category="wa")
        await cq.message.edit_text(
            "🔤 <b>Enter the Service Short Code for this operator:</b>\n"
            "<i>(Exact WhatsApp code for THIS operator, e.g. tss. Case doesn't matter.)</i>",
            parse_mode="HTML"
        )
        await state.set_state(Server3Admin.waiting_service_code)

    @dp.message(StateFilter(Server3Admin.waiting_service_code))
    async def s3_add_svc_code(msg: Message, state: FSMContext):
        await state.update_data(service_code=msg.text.strip())
        await msg.answer("🔢 <b>Enter TemporaSMS Country ID:</b>\n<i>(Ex: 22, 1, 12 — name auto-fetched)</i>", parse_mode="HTML")
        await state.set_state("s3_waiting_country_code")

    @dp.message(F.text, StateFilter("s3_waiting_country_code"))
    async def s3_add_cc(msg: Message, state: FSMContext):
        country_code = msg.text.strip()
        checking = await msg.answer("🔎 <i>Fetching country name...</i>", parse_mode="HTML")
        country_name = await get_country_name(country_code)
        if not country_name:
            await checking.edit_text(f"⚠️ Could not auto-resolve name for {country_code}. Send display name manually:", parse_mode="HTML")
            await state.update_data(country_code=country_code)
            await state.set_state(Server3Admin.waiting_country_name)
            return
        await state.update_data(country_code=country_code, country_name=country_name)
        await checking.edit_text(f"✅ <b>Country:</b> {country_name}", parse_mode="HTML")
        await msg.answer("🚩 <b>Send a flag emoji for this country:</b>\n<i>(or type 'skip')</i>", parse_mode="HTML")
        await state.set_state(Server3Admin.waiting_flag)

    @dp.message(StateFilter(Server3Admin.waiting_country_name))
    async def s3_add_cn(msg: Message, state: FSMContext):
        await state.update_data(country_name=msg.text.strip())
        await msg.answer("🚩 <b>Send a flag emoji for this country:</b>\n<i>(or type 'skip')</i>", parse_mode="HTML")
        await state.set_state(Server3Admin.waiting_flag)

    @dp.message(StateFilter(Server3Admin.waiting_flag))
    async def s3_add_flag(msg: Message, state: FSMContext):
        flag = msg.text.strip()
        data = await state.get_data()
        if flag.lower() != "skip":
            await state.update_data(country_name=f"{data['country_name']} {flag}")
        operators = await get_all_operators()
        kb = InlineKeyboardBuilder()
        for op in operators:
            kb.button(text=f"Op {op}", callback_data=f"s3a_pickop:{op}")
        kb.adjust(4)
        await msg.answer("📶 <b>Select Operator:</b>", parse_mode="HTML", reply_markup=kb.as_markup())
        await state.set_state("s3_waiting_operator")

    @dp.callback_query(F.data.startswith("s3a_pickop:"), StateFilter("s3_waiting_operator"))
    async def s3_add_op_pick(cq: CallbackQuery, state: FSMContext):
        operator = cq.data.split(":")[1]
        await state.update_data(operator=operator)
        await cq.message.edit_text(f"📶 Operator: <b>{operator}</b>", parse_mode="HTML")
        await cq.message.answer("💰 <b>Enter YOUR Selling Price (in INR):</b>", parse_mode="HTML")
        await state.set_state(Server3Admin.waiting_price_inr)
        await cq.answer()

    @dp.message(StateFilter(Server3Admin.waiting_price_inr))
    async def s3_add_price(msg: Message, state: FSMContext):
        try: price_inr = float(msg.text)
        except: return await msg.answer("❌ Please enter a valid number.")
        await state.update_data(sell_price_inr=price_inr, sell_price_usd=price_inr / exchange_rate)
        await msg.answer("⚠️ <b>Enter Maximum API Price Cap (in USDT):</b>\n<i>(Auto-adjusted live. Ex: 2.00)</i>", parse_mode="HTML")
        await state.set_state(Server3Admin.waiting_max_price)

    @dp.message(StateFilter(Server3Admin.waiting_max_price))
    async def s3_add_max_price(msg: Message, state: FSMContext):
        try: max_price = float(msg.text)
        except: return await msg.answer("❌ Please enter a valid number.")

        data = await state.get_data()
        checking_msg = await msg.answer("🔎 <i>Validating this combo (checking case variants too)...</i>", parse_mode="HTML")

        final_code, live_price, error = await validate_with_case_variants(
            data["country_code"], data["operator"], data["service_code"]
        )

        if final_code is None:
            await checking_msg.edit_text(
                f"<b>❌ Validation Failed — Not Saved</b>\n\n<blockquote>"
                f"🔤 <b>Tried:</b> <code>{data['service_code']}</code> (all case variants)\n"
                f"🌍 <b>Country:</b> {data['country_name']}\n📶 <b>Operator:</b> {data['operator']}\n"
                f"⚠️ <b>Response:</b> <code>{error}</code></blockquote>",
                parse_mode="HTML"
            )
            await state.clear()
            return

        live_price_inr = live_price * exchange_rate
        margin = data["sell_price_inr"] - live_price_inr

        s3_catalog.update_one(
            {"category": "wa", "service_code": final_code, "country_code": data["country_code"], "operator": data["operator"]},
            {"$set": {
                "country_name": data["country_name"], "sell_price_inr": data["sell_price_inr"],
                "sell_price_usd": data["sell_price_usd"], "max_price_cap": max_price,
                "updated_at": datetime.now(timezone.utc)
            }}, upsert=True
        )

        await checking_msg.edit_text(
            f"<b>✅ Catalog Saved!</b>\n\n<blockquote>"
            f"💬 WhatsApp (code: <code>{final_code}</code>)\n🌍 <b>{data['country_name']}</b> | Op {data['operator']}\n"
            f"💵 <b>Price:</b> ₹{data['sell_price_inr']:.2f}\n🛡️ <b>Cap:</b> {max_price} USDT\n\n"
            f"📡 <b>Live Cost:</b> {live_price:.4f} USDT (₹{live_price_inr:.2f})\n💵 <b>Margin:</b> ₹{margin:.2f}</blockquote>",
            parse_mode="HTML"
        )
        await state.clear()

    @dp.callback_query(F.data == "s3_rem_item")
    async def s3_rem_start(cq: CallbackQuery):
        if not is_admin(cq.from_user.id): return
        items = list(s3_catalog.find({}))
        if not items:
            return await cq.message.edit_text("📭 Catalog is empty.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back", callback_data="delete_msg")]]))
        kb = InlineKeyboardBuilder()
        for i in items:
            kb.button(text=f"❌ {i['country_name']} Op{i['operator']} ({i['service_code']})", callback_data=f"s3_del:{str(i['_id'])}")
        kb.adjust(1)
        kb.row(InlineKeyboardButton(text="🔙 Back", callback_data="delete_msg"))
        await cq.message.edit_text("🗑️ <b>Tap to remove:</b>", parse_mode="HTML", reply_markup=kb.as_markup())

    @dp.callback_query(F.data.startswith("s3_del:"))
    async def s3_del_execute(cq: CallbackQuery):
        if not is_admin(cq.from_user.id): return
        s3_catalog.delete_one({"_id": ObjectId(cq.data.split(":")[1])})
        await cq.answer("✅ Deleted!", show_alert=True)
        await s3_rem_start(cq)

    @dp.callback_query(F.data == "s3_price_check")
    async def s3_price_checker(cq: CallbackQuery):
        if not is_admin(cq.from_user.id): return
        await cq.message.edit_text("🔄 <i>Fetching live API costs...</i>", parse_mode="HTML")
        catalog = list(s3_catalog.find({}))
        if not catalog: return await cq.message.edit_text("❌ No items in catalog.")
        lines = ["<b>🔍 WA Live Margin Audit</b>\n"]
        for item in catalog:
            is_valid, err, cost = await check_live_combo(item['service_code'], item['country_code'], item['operator'])
            if not is_valid:
                lines.append(f"⚠️ <b>{item['country_name']} Op{item['operator']}</b> — <code>{err}</code> (dead route)\n")
                continue
            cost_inr = cost * exchange_rate
            margin = item['sell_price_inr'] - cost_inr
            e = "🟢" if margin > 0 else "🔴"
            lines.append(f"{e} <b>{item['country_name']} Op{item['operator']}</b>\nCost: ₹{cost_inr:.2f} | Sell: ₹{item['sell_price_inr']:.2f} | Profit: <b>₹{margin:.2f}</b>\n")
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back", callback_data="delete_msg")]])
        await cq.message.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=kb)

    @dp.callback_query(F.data == "s3_edit_list")
    async def s3_edit_start(cq: CallbackQuery):
        if not is_admin(cq.from_user.id): return
        items = list(s3_catalog.find({}))
        if not items:
            return await cq.message.edit_text("📭 Catalog is empty.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back", callback_data="delete_msg")]]))
        kb = InlineKeyboardBuilder()
        for i in items:
            kb.button(text=f"✏️ {i['country_name']} Op{i['operator']} (₹{i['sell_price_inr']})", callback_data=f"s3_ed_item:{str(i['_id'])}")
        kb.adjust(1)
        kb.row(InlineKeyboardButton(text="🔙 Back", callback_data="delete_msg"))
        await cq.message.edit_text("<b>✏️ Select Item:</b>", parse_mode="HTML", reply_markup=kb.as_markup())

    @dp.callback_query(F.data.startswith("s3_ed_item:"))
    async def s3_edit_options(cq: CallbackQuery, state: FSMContext):
        if not is_admin(cq.from_user.id): return
        item = s3_catalog.find_one({"_id": ObjectId(cq.data.split(":")[1])})
        if not item: return await cq.answer("Not found!", show_alert=True)
        await state.update_data(edit_item_id=str(item["_id"]), item_name=f"{item['country_name']} (Op {item['operator']})")
        text = f"<b>🛠️ {item['country_name']}</b>\n\n<blockquote>📶 Op: {item['operator']}\n💰 Price: ₹{item['sell_price_inr']:.2f}\n🛡️ Cap: {item['max_price_cap']} USDT</blockquote>"
        kb = InlineKeyboardBuilder()
        kb.row(InlineKeyboardButton(text="💰 Change Price", callback_data="s3_chg:price"), InlineKeyboardButton(text="🛡️ Change Cap", callback_data="s3_chg:cap"))
        kb.row(InlineKeyboardButton(text="🔙 Back", callback_data="s3_edit_list"))
        await cq.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())

    @dp.callback_query(F.data.startswith("s3_chg:"))
    async def s3_edit_prompt(cq: CallbackQuery, state: FSMContext):
        action = cq.data.split(":")[1]
        data = await state.get_data()
        if action == "price":
            await cq.message.edit_text(f"💰 New price for {data['item_name']}:", parse_mode="HTML")
            await state.set_state(Server3Edit.waiting_new_price)
        else:
            await cq.message.edit_text(f"🛡️ New max cap for {data['item_name']}:", parse_mode="HTML")
            await state.set_state(Server3Edit.waiting_new_cap)

    @dp.message(StateFilter(Server3Edit.waiting_new_price))
    async def s3_save_new_price(msg: Message, state: FSMContext):
        try: new_inr = float(msg.text.strip())
        except: return await msg.answer("❌ Invalid number.")
        data = await state.get_data()
        s3_catalog.update_one({"_id": ObjectId(data['edit_item_id'])}, {"$set": {"sell_price_inr": new_inr, "sell_price_usd": new_inr / exchange_rate}})
        await msg.answer(f"✅ Price updated to ₹{new_inr:.2f}!")
        await state.clear()

    @dp.message(StateFilter(Server3Edit.waiting_new_cap))
    async def s3_save_new_cap(msg: Message, state: FSMContext):
        try: new_cap = float(msg.text.strip())
        except: return await msg.answer("❌ Invalid number.")
        data = await state.get_data()
        s3_catalog.update_one({"_id": ObjectId(data['edit_item_id'])}, {"$set": {"max_price_cap": new_cap}})
        await msg.answer(f"✅ Cap updated to {new_cap} USDT!")
        await state.clear()

    # ============================================================
    # ==================  WA: USER BUY FLOW  =======================
    # (unchanged from before — TemporaSMS number rental)
    # ============================================================

    @dp.callback_query(F.data == "buy_wa")
    async def buy_wa_handler(cq: CallbackQuery):
        new_cq = cq.model_copy(update={"data": "buy_server3:wa"})
        await user_s3_countries(new_cq)

    @dp.callback_query(F.data.startswith("buy_server3:"))
    async def user_s3_countries(cq: CallbackQuery):
        category = cq.data.split(":")[1]
        cm = category_meta(category)
        settings = get_settings()
        if settings.get(cm["toggle_field"], "on") == "off":
            return await cq.answer("⚠️ Currently offline for maintenance.", show_alert=True)
        pipeline = [{"$match": {"category": category}}, {"$group": {"_id": "$country_code", "name": {"$first": "$country_name"}}}]
        countries = list(s3_catalog.aggregate(pipeline))
        if not countries:
            return await cq.message.edit_text("📭 No countries available right now.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back", callback_data="back_main")]]))
        kb = InlineKeyboardBuilder()
        for c in countries:
            kb.button(text=c['name'], callback_data=f"s3_user_ops:{category}:{c['_id']}")
        kb.adjust(2)
        kb.row(InlineKeyboardButton(text="🔙 Back", callback_data="back_main"))
        text = f"<b>{cm['emoji']} {cm['name']} Registration [Server 3]</b>\n\n<blockquote>🌍 Select Region:</blockquote>\n<i>⚡ Instant SMS Delivery</i>"
        await cq.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())

    @dp.callback_query(F.data.startswith("s3_user_ops:"))
    async def user_s3_operators(cq: CallbackQuery):
        _, category, country_code = cq.data.split(":")
        cm = category_meta(category)
        items = list(s3_catalog.find({"category": category, "country_code": country_code}))
        if not items: return await cq.answer("No operators found.", show_alert=True)
        country_name = items[0]['country_name']
        usd_toggle = get_settings().get("s3_usd_display", "off")
        kb = InlineKeyboardBuilder()
        for item in items:
            btn = f"Operator {item['operator']} ➔ ₹{item['sell_price_inr']:.2f}" + (f"/${item['sell_price_usd']:.2f}" if usd_toggle == "on" else "")
            kb.button(text=btn, callback_data=f"s3_user_terms:{str(item['_id'])}")
        kb.adjust(1)
        kb.row(InlineKeyboardButton(text="🔙 Back", callback_data=f"buy_server3:{category}"))
        text = f"<b>{cm['emoji']} {cm['name']} | {country_name}</b>\n\n<blockquote>📶 Select an operator line:</blockquote>"
        await cq.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())

    @dp.callback_query(F.data.startswith("s3_user_terms:"))
    async def user_s3_terms(cq: CallbackQuery):
        item = s3_catalog.find_one({"_id": ObjectId(cq.data.split(":")[1])})
        if not item: return await cq.answer("Error.")
        cm = category_meta(item['category'])
        usd_toggle = get_settings().get("s3_usd_display", "off")
        price_label = f"₹{item['sell_price_inr']:.2f} | ${item['sell_price_usd']:.2f}" if usd_toggle == "on" else f"₹{item['sell_price_inr']:.2f}"
        text = (
            f"<b>⚠️ Purchase Agreement — {cm['name']}</b>\n\n<blockquote>"
            f"{cm['emoji']} <b>App:</b> {cm['name']}\n🌍 <b>Region:</b> {item['country_name']}\n"
            f"📶 <b>Operator:</b> Line {item['operator']}\n💰 <b>Price:</b> {price_label}</blockquote>\n\n"
            f"📌 <b>Note:</b> Charged only if OTP is received. Cancel after 2 mins for a full refund otherwise.\n\n"
            f"❓ <i>Ready to reserve this number?</i>"
        )
        kb = InlineKeyboardBuilder()
        kb.row(InlineKeyboardButton(text="✅ Accept & Reserve", callback_data=f"s3_final_buy:{str(item['_id'])}"))
        kb.row(InlineKeyboardButton(text="🔙 Cancel", callback_data=f"s3_user_ops:{item['category']}:{item['country_code']}"))
        await cq.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())

    async def process_s3_refund(user_id, order_id, item_id):
        order = orders_col.find_one({"order_id": order_id})
        if not order or order.get("status") != "waiting":
            return False, "Already processed"
        item = s3_catalog.find_one({"_id": ObjectId(item_id)})
        refund_amount = item["sell_price_inr"] if item else order.get("price", 0.0)
        orders_col.update_one({"order_id": order_id}, {"$set": {"status": "cancelled"}})
        user = users_col.find_one({"_id": user_id}) or {}
        new_bal = user.get("balance", 0.0) + refund_amount
        users_col.update_one({"_id": user_id}, {"$set": {"balance": new_bal}})
        return True, refund_amount

    async def auto_otp_checker_s3(user_id, order_id, phone_number, item_id, msg_to_edit):
        item = s3_catalog.find_one({"_id": ObjectId(item_id)})
        if not item: return
        cm = category_meta(item['category'])
        url = f"{TEMPORASMS_BASE}?api_key={TEMPORASMS_API_KEY}&action=getStatus&id={order_id}"

        for _ in range(19):
            await asyncio.sleep(60)
            order = orders_col.find_one({"order_id": order_id})
            if not order or order.get("status") != "waiting":
                return
            text = await s3_api_call(f"TemporaSMS: OTP Poll (Order:{order_id})", url)
            if "STATUS_OK:" in text:
                otp_code = text.split(":")[1]
                orders_col.update_one({"order_id": order_id}, {"$set": {"status": "completed", "otp": otp_code}})
                kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📋 Copy OTP Code", copy_text=CopyTextButton(text=otp_code))]])
                success_text = (
                    f"<b>✅ Order Completed</b>\n\n<blockquote>"
                    f"{cm['emoji']} <b>App:</b> {cm['name']}\n📞 <b>Number:</b> <code>{phone_number}</code>\n"
                    f"💬 <b>OTP Code:</b> <code>{otp_code}</code></blockquote>\n<i>Tap below to copy!</i>"
                )
                try:
                    await bot.edit_message_text(text=success_text, chat_id=user_id, message_id=msg_to_edit, parse_mode="HTML", reply_markup=kb)
                except:
                    await bot.send_message(user_id, success_text, parse_mode="HTML", reply_markup=kb)

                public_log = (
                    f"<blockquote>✅ New {cm['name']} Number Purchase Successful</blockquote>\n\n"
                    f"➖ <u><b>Country:</b></u> {item['country_name']}\n➖ <u><b>Application:</b></u> {cm['name']} 🍷\n\n"
                    f"➕ <b>Number:</b> {mask_phone(phone_number)} 📞\n➕ <b>Code:</b> •••• 💬\n\n"
                    f"<b>• @{BOTUSER} || @{CHANNEL}</b>"
                )
                kb_pub = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="• Buy Now •", url=f"https://t.me/{BOTUSER}?start=s3wa")]])
                try: await bot.send_message(SALES, public_log, parse_mode="HTML", reply_markup=kb_pub)
                except: pass

                admin_log = (
                    f"📢 <b>STORE SALE ALERT (WhatsApp)</b>\n\n➖ <b>Country:</b> {item['country_name']}\n"
                    f"➕ <b>Number:</b> {phone_number}\n➕ <b>OTP:</b> {otp_code}\n➕ <b>Buyer:</b> <code>{user_id}</code>\n➕ <b>Price:</b> ₹{item['sell_price_inr']}"
                )
                try: await bot.send_message(ADMINLOG, admin_log, parse_mode="HTML")
                except: pass
                return
            elif "STATUS_CANCEL" in text:
                break

        cancel_url = f"{TEMPORASMS_BASE}?api_key={TEMPORASMS_API_KEY}&action=setStatus&status=8&id={order_id}"
        await s3_api_call(f"TemporaSMS: Auto-Cancel (Order:{order_id})", cancel_url)
        success, refund_amount = await process_s3_refund(user_id, order_id, item_id)
        if success:
            cancel_text = f"<b>⚠️ Order Auto-Cancelled (Timeout)</b>\n\nNo OTP received in time.\n\n💰 <b>Refunded:</b> ₹{refund_amount:.2f}"
            try: await bot.edit_message_text(text=cancel_text, chat_id=user_id, message_id=msg_to_edit, parse_mode="HTML")
            except: await bot.send_message(user_id, cancel_text, parse_mode="HTML")

    @dp.callback_query(F.data.startswith("s3_final_buy:"))
    async def srv3_buy_execution(cq: CallbackQuery):
        item_id = cq.data.split(":")[1]
        item = s3_catalog.find_one({"_id": ObjectId(item_id)})
        if not item: return await cq.answer("Catalog error.")
        cm = category_meta(item['category'])
        user_id = cq.from_user.id
        user = users_col.find_one({"_id": user_id}) or {"_id": user_id, "balance": 0.0}
        inr_price = item["sell_price_inr"]
        if user.get("balance", 0.0) < inr_price:
            return await cq.answer("❌ Insufficient Balance!", show_alert=True)

        status_msg = await cq.message.edit_text("⏳ <i>Connecting to Server 3... Reserving allocation...</i>", parse_mode="HTML")
        api_bal = await get_tempora_balance()
        if api_bal <= 0.1:
            try: await bot.send_message(ADMINLOG, f"🚨 <b>CRITICAL: TemporaSMS Balance Low! ({api_bal} USDT)</b>", parse_mode="HTML")
            except: pass
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back", callback_data=f"buy_server3:{item['category']}")]])
            return await status_msg.edit_text("❌ <b>Server busy. Try later.</b>", parse_mode="HTML", reply_markup=kb)

        safe_max_price = item['max_price_cap']
        price_url = f"{TEMPORASMS_BASE}?api_key={TEMPORASMS_API_KEY}&action=getPricesV3&country={item['country_code']}&service={item['service_code']}&operator={item['operator']}"
        price_text = await s3_api_call(f"TemporaSMS: Pre-Purchase Check (User:{user_id})", price_url)
        try:
            pdata = json.loads(price_text)
            live_price = pdata.get(item['country_code'], {}).get(item['service_code'], {}).get("price", 0.0)
            if live_price and live_price > safe_max_price:
                safe_max_price = round(live_price * 1.15, 2)
        except Exception:
            pass

        url = (
            f"{TEMPORASMS_BASE}?api_key={TEMPORASMS_API_KEY}&action=getNumber&service={item['service_code']}"
            f"&country={item['country_code']}&operator={item['operator']}&maxPrice={safe_max_price}"
        )
        raw = await s3_api_call(f"TemporaSMS: Purchase Number (User:{user_id})", url)
        phone_number, order_id = None, None
        if "ACCESS_NUMBER:" in raw:
            parts = raw.split(":")
            order_id, phone_number = parts[1], parts[2]
        if not phone_number or not order_id:
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back", callback_data=f"buy_server3:{item['category']}")]])
            return await status_msg.edit_text("❌ <b>Out of stock or timed out.</b>", parse_mode="HTML", reply_markup=kb)

        new_balance = user.get("balance", 0.0) - inr_price
        users_col.update_one({"_id": user_id}, {"$set": {"balance": new_balance}}, upsert=True)
        orders_col.insert_one({
            "user_id": user_id, "country": item['country_name'], "number": phone_number, "price": inr_price,
            "server": 3, "order_id": order_id, "status": "waiting", "created_at": datetime.utcnow()
        })

        kb = InlineKeyboardBuilder()
        kb.row(InlineKeyboardButton(text="📋 Copy Number", copy_text=CopyTextButton(text=phone_number)))
        kb.row(InlineKeyboardButton(text="📩 Get OTP", callback_data=f"s3_otp:{order_id}:{phone_number}"))
        kb.row(InlineKeyboardButton(text="🔄 Change Number", callback_data=f"s3_change:{order_id}:{item_id}"), InlineKeyboardButton(text="❌ Cancel", callback_data=f"s3_cancel:{order_id}:{item_id}"))
        kb.row(InlineKeyboardButton(text="🔙 Back", callback_data=f"buy_server3:{item['category']}"))
        ui_text = (
            f"<b>🟢 {cm['name']} Number Allocated</b>\n\n<blockquote>"
            f"🌍 <b>Country:</b> {item['country_name']}\n📞 <b>Number:</b> <code>{phone_number}</code>\n"
            f"🏷️ <b>Price:</b> ₹{inr_price:.2f}\n💸 <b>Remaining Balance:</b> ₹{new_balance:.2f}</blockquote>\n"
            f"<i>⏳ Please wait while we deliver your OTP...</i>\n\n🔒 <b>Security Tip:</b> Use a VPN or proxy while registering."
        )
        await status_msg.edit_text(ui_text, parse_mode="HTML", reply_markup=kb.as_markup())
        asyncio.create_task(auto_otp_checker_s3(user_id, order_id, phone_number, item_id, status_msg.message_id))

    @dp.callback_query(F.data.startswith("s3_cancel:"))
    async def srv3_cancel_number(cq: CallbackQuery):
        order_id, item_id = cq.data.split(":")[1], cq.data.split(":")[2]
        user_id = cq.from_user.id
        
        order = orders_col.find_one({"order_id": order_id})
        if not order:
            return await cq.answer("❌ Order not found.", show_alert=True)
            
        if order.get("status") == "completed":
            return await cq.answer("❌ OTP already received! Cannot cancel.", show_alert=True)

        created_at = order.get("created_at")
        if created_at:
            # Kitne seconds beet chuke hain
            elapsed = (datetime.utcnow() - created_at).total_seconds()
            wait_time = 300  # 5 minutes in seconds
            
            if elapsed < wait_time:
                # Kitna time bacha hai usko calculate karna
                rem = int(wait_time - elapsed)
                m, s = divmod(rem, 60)
                return await cq.answer(
                    f"⏳ Please wait!\n\nYou can cancel this number in {m}m {s}s.\nSMS routing takes time for this region.", 
                    show_alert=True
                )

        await cq.answer("🔄 Cancelling order...", show_alert=False)
        url = f"{TEMPORASMS_BASE}?api_key={TEMPORASMS_API_KEY}&action=setStatus&status=8&id={order_id}"
        text = await s3_api_call(f"TemporaSMS: Manual Cancel (User:{user_id})", url)
        
        if "EARLY_CANCEL_DENIED" in text:
            return await cq.answer("⏳ Provider denied cancellation. Try again in a minute!", show_alert=True)
            
        success, refund_amount = await process_s3_refund(user_id, order_id, item_id)
        if success:
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back to Menu", callback_data="back_main")]])
            await cq.message.edit_text(
                f"<b>❌ Order Cancelled</b>\n\n💰 Refunded: ₹{refund_amount:.2f}", 
                parse_mode="HTML", 
                reply_markup=kb
            )
        else:
            await cq.answer("❌ Already processed.", show_alert=True)


    @dp.callback_query(F.data.startswith("s3_change:"))
    async def srv3_change_number(cq: CallbackQuery):
        order_id, item_id = cq.data.split(":")[1], cq.data.split(":")[2]
        user_id = cq.from_user.id
        
        # Order fetch karo DB se
        order = orders_col.find_one({"order_id": order_id})
        if not order:
            return await cq.answer("❌ Order not found.", show_alert=True)

        if order.get("status") == "completed":
            return await cq.answer("❌ OTP already received! Cannot change.", show_alert=True)

        created_at = order.get("created_at")
        if created_at:
            elapsed = (datetime.utcnow() - created_at).total_seconds()
            wait_time = 300  # 5 minutes
            
            if elapsed < wait_time:
                rem = int(wait_time - elapsed)
                m, s = divmod(rem, 60)
                return await cq.answer(
                    f"⏳ Please wait!\n\nYou can change this number in {m}m {s}s.\nSMS routing takes time for this region.", 
                    show_alert=True
                )

        url = f"{TEMPORASMS_BASE}?api_key={TEMPORASMS_API_KEY}&action=setStatus&status=8&id={order_id}"
        text = await s3_api_call(f"TemporaSMS: Change Cancel (User:{user_id})", url)
        
        if "EARLY_CANCEL_DENIED" in text:
            return await cq.answer("⏳ Provider denied cancellation. Try again in a minute!", show_alert=True)
            
        success, _ = await process_s3_refund(user_id, order_id, item_id)
        if not success:
            return await cq.answer("❌ Could not cancel current number.", show_alert=True)
            
        await cq.answer("🔄 Getting a new number...", show_alert=False)
        item = s3_catalog.find_one({"_id": ObjectId(item_id)})
        if not item:
            return await cq.message.edit_text("❌ Catalog item no longer available.")
            
        status_msg = await cq.message.edit_text("⏳ <i>Previous number cancelled. Reserving a new one...</i>", parse_mode="HTML")
        
        await srv3_buy_execution(types.CallbackQuery(
            id=cq.id, 
            from_user=cq.from_user, 
            chat_instance=cq.chat_instance, 
            message=status_msg, 
            data=f"s3_final_buy:{item_id}"
        ))

    # ============================================================
    # ==================  TG (LZT): USER BUY FLOW  ==================
    # ============================================================

    def build_country_page(page, query=None):
        global LIVE_PRICE_CACHE
        pool = []

        for c in LZT_ALL_COUNTRIES:
            usd_price = LIVE_PRICE_CACHE.get(c['code'])
            if usd_price is not None:
                inr_price = usd_to_sell_inr(usd_price)
                pool.append({
                    "code": c['code'],
                    "name": c['name'],
                    "flag": c['flag'],
                    "inr_price": inr_price
                })

        pool.sort(key=lambda x: x["inr_price"])

        if query:
            q = query.lower()
            pool = [c for c in pool if q in c['name'].lower() or q in c['code'].lower()]

        total_pages = max(1, (len(pool) + COUNTRY_PER_PAGE - 1) // COUNTRY_PER_PAGE)
        page = max(0, min(page, total_pages - 1))
        start = page * COUNTRY_PER_PAGE
        page_items = pool[start:start + COUNTRY_PER_PAGE]

        kb = InlineKeyboardBuilder()
        if not page_items:
            kb.row(InlineKeyboardButton(text="🔙 Back to Servers", callback_data="server_list"))
            return "📭 <b>No countries matched your search or are in stock.</b>", kb.as_markup()

        for c in page_items:
            btn_text = f"{c['flag']} {c['name']} (₹{c['inr_price']:.0f})"
            kb.button(text=btn_text, callback_data=f"s3tg_country:{c['code']}:0")
        kb.adjust(2)

        nav = []
        if total_pages > 1:
            for p in range(total_pages):
                if p == page:
                    nav.append(InlineKeyboardButton(text=f"• {p+1} •", callback_data="noop"))
                else:
                    nav.append(InlineKeyboardButton(text=f"{p+1}", callback_data=f"s3tg_cpage:{p}:{query or ''}"))
            
            for i in range(0, len(nav), 8):
                kb.row(*nav[i:i+8])

        kb.row(InlineKeyboardButton(text="🔍 Search", callback_data="s3tg_search"))
        kb.row(InlineKeyboardButton(text="📋 My Orders", callback_data="s3tg_my_orders"))
        
        kb.row(InlineKeyboardButton(text="🔙 Back to Servers", callback_data="buy"))

        text = (
            "<b>📱 Telegram Accounts — Server (3)</b>\n\n"
            "<blockquote>🌍 Available regions sorted by cheapest price:</blockquote>"
        )
        return text, kb.as_markup()

    async def send_lzt_countries_message(chat_id):
        text, markup = build_country_page(0)
        try: await bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=markup)
        except Exception: pass

    @dp.callback_query(F.data == "s3tg_open")
    async def s3tg_open(cq: CallbackQuery):
        if not get_settings().get("lzt_enabled", False):
            return await cq.answer("⚠️ Currently unavailable.", show_alert=True)
        text, markup = build_country_page(0)
        await cq.message.edit_text(text, parse_mode="HTML", reply_markup=markup)

    @dp.callback_query(F.data.startswith("s3tg_cpage:"))
    async def s3tg_cpage(cq: CallbackQuery):
        _, page, query = cq.data.split(":", 2)
        text, markup = build_country_page(int(page), query or None)
        await cq.message.edit_text(text, parse_mode="HTML", reply_markup=markup)
        await cq.answer()

    @dp.callback_query(F.data == "s3tg_search")
    async def s3tg_search(cq: CallbackQuery, state: FSMContext):
        await state.set_state(UserSearch.waiting_country_query)
        await cq.message.edit_text("🔍 <b>Type a country name to search:</b>", parse_mode="HTML")
        await cq.answer()

    @dp.message(StateFilter(UserSearch.waiting_country_query))
    async def s3tg_search_result(msg: Message, state: FSMContext):
        await state.clear()
        text, markup = build_country_page(0, msg.text.strip())
        await msg.answer(text, parse_mode="HTML", reply_markup=markup)

    @dp.callback_query(F.data == "noop")
    async def s3tg_noop(cq: CallbackQuery):
        await cq.answer()

    @dp.callback_query(F.data.startswith("s3tg_country:"))
    async def s3tg_listings(cq: CallbackQuery):
        _, country_code, vpage = cq.data.split(":")
        vpage = int(vpage)
        await cq.message.edit_text("🔎 <i>Fetching live listings... ⏳</i>", parse_mode="HTML")

        lzt_page = (vpage // 4) + 1

        status, data = await lzt_request(
            "GET", LZT_SEARCH_URL,
            params={"country[]": country_code, "order_by": "price_to_up", "currency": "usd", "spam": "no", "page": lzt_page},
            action_label=f"LZT: Search ({country_code} Page {lzt_page})"
        )

        if status != 200 or "items" not in data:
            errs = data.get("errors", ["Unknown error"]) if isinstance(data, dict) else ["Unknown error"]
            await log_admin(f"⚠️ <b>LZT Search Failed</b> (status {status}): {'; '.join(errs)}", data)
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back", callback_data="s3tg_open")]])
            return await cq.message.edit_text(f"❌ <b>Could not fetch listings.</b>\n<i>{escape('; '.join(errs))}</i>", parse_mode="HTML", reply_markup=kb)

        all_items = data["items"]
        start_index = (vpage % 4) * LZT_PER_PAGE
        page_items = all_items[start_index : start_index + LZT_PER_PAGE]

        if not page_items and vpage == 0:
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back", callback_data="s3tg_open")]])
            return await cq.message.edit_text("📭 <b>No accounts available in this region right now.</b>", parse_mode="HTML", reply_markup=kb)

        country_meta = next((c for c in LZT_ALL_COUNTRIES if c['code'] == country_code), {"flag": "🌍", "name": country_code})

        kb = InlineKeyboardBuilder()
        for item in page_items:
            usd_price = item["price"]
            sell_price = usd_to_sell_inr(usd_price)
            kb.button(text=f"{country_meta['flag']} ₹{sell_price:.0f}", callback_data=f"s3tg_view:{item['item_id']}:{usd_price}:{country_code}:{vpage}")
        kb.adjust(2)

        nav = []
        if vpage > 0:
            nav.append(InlineKeyboardButton(text="⬅️ Prev", callback_data=f"s3tg_country:{country_code}:{vpage-1}"))
        if start_index + LZT_PER_PAGE < len(all_items):
            nav.append(InlineKeyboardButton(text="Next ➡️", callback_data=f"s3tg_country:{country_code}:{vpage+1}"))
        if nav:
            kb.row(*nav)
            
        kb.row(InlineKeyboardButton(text="🔄 Refresh", callback_data=f"s3tg_country:{country_code}:{vpage}"), InlineKeyboardButton(text="🔙 Back", callback_data="s3tg_open"))

        text = f"<b>📱 TG Accounts — {country_meta['name']}</b>\n\n<blockquote>🛒 Showing {len(page_items)} accounts. Tap to buy 👇</blockquote>"
        await cq.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())


    @dp.callback_query(F.data.startswith("s3tg_view:"))
    async def s3tg_view(cq: CallbackQuery):
        parts = cq.data.split(":")
        item_id = parts[1]
        usd_price = float(parts[2])
        country_code = parts[3]
        vpage = parts[4]
        
        sell_price = usd_to_sell_inr(usd_price)
        
        kb = InlineKeyboardBuilder()
        kb.row(InlineKeyboardButton(text="✅ Accept & Buy", callback_data=f"s3tg_buy:{item_id}:{usd_price}:{country_code}"))
        kb.row(InlineKeyboardButton(text="🔙 Back", callback_data=f"s3tg_country:{country_code}:{vpage}"))
        
        text = (
            "<b>🛒 Order Confirmation & Terms</b>\n\n"
            "<blockquote>"
            f"🆔 <b>Item ID:</b> <code>{item_id}</code>\n"
            f"💰 <b>Price:</b> ₹{sell_price:.0f}\n"
            "</blockquote>\n\n"
            "🚨 <b>WARNING:</b> These are cheap phishing accounts. The bot is not responsible for any logouts. <b>Buy at your own risk!</b>\n\n"
            "🚫 <b>No Refunds:</b> All sales are final.\n"
            "📨 <b>OTP Policy:</b> Once the OTP is received, no returns are allowed.\n"
            "❄️ <b>Freeze/Limit:</b> Accounts are fresh; we are not responsible for any limitations after use.\n\n"
            "<i>⚠️ Tap 'Accept & Buy' only if you agree to these terms.</i>"
        )
        await cq.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())

    async def find_exact_replacement(country_code, exact_usd_price):
        params = {
            "country[]": country_code,
            "pmin": exact_usd_price,
            "pmax": exact_usd_price,
            "order_by": "price_to_up",
            "currency": "usd",
            "spam": "no",
            "page": 1
        }
        status, data = await lzt_request("GET", LZT_SEARCH_URL, params=params, action_label="LZT: Failover Search")
        if status == 200 and data.get("items"):
            return data["items"][0]["item_id"]
        return None

    @dp.callback_query(F.data.startswith("s3tg_buy_again:"))
    async def s3tg_buy_again(cq: CallbackQuery):
        _, country_code, usd_price = cq.data.split(":")
        usd_price = float(usd_price)
        
        await cq.answer("🔄 Searching stock...", show_alert=False)
        
        status_msg = await cq.message.edit_text("⏳ <i>Searching for another account at the exact same price...</i>", parse_mode="HTML")
        new_item_id = await find_exact_replacement(country_code, usd_price)
        
        if not new_item_id:
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back to Menu", callback_data="s3tg_open")]])
            return await status_msg.edit_text("❌ <b>Out of stock!</b> No accounts left at this exact price for this country.", parse_mode="HTML", reply_markup=kb)
            
        new_cq = cq.model_copy(update={"data": f"s3tg_buy:{new_item_id}:{usd_price}:{country_code}"})
        
        await s3tg_buy(new_cq, external_status_msg=status_msg)

    @dp.callback_query(F.data.startswith("s3tg_buy:"))
    async def s3tg_buy(cq: CallbackQuery, external_status_msg=None):
        parts = cq.data.split(":")
        item_id = parts[1]
        usd_price = float(parts[2])
        country_code = parts[3]
        
        sell_price = usd_to_sell_inr(usd_price)
        user_id = cq.from_user.id
        
        first_name = cq.from_user.first_name or "User"
        username_str = f"@{cq.from_user.username}" if cq.from_user.username else f"<a href='tg://user?id={user_id}'>{first_name}</a>"

        user = users_col.find_one({"_id": user_id}) or {"_id": user_id, "balance": 0.0}
        balance = user.get("balance", 0.0)
        
        if balance < sell_price:
            kb = InlineKeyboardBuilder()
            kb.button(text="💳 Recharge", callback_data="recharge")
            kb.button(text="🔙 Back", callback_data="s3tg_open")
            kb.adjust(1)
            msg_text = f"<b>❌ Insufficient Balance!</b>\n\n<blockquote>Required: ₹{sell_price:.2f}\nAvailable: ₹{balance:.2f}</blockquote>"
            if external_status_msg:
                return await external_status_msg.edit_text(msg_text, parse_mode="HTML", reply_markup=kb.as_markup())
            return await cq.message.edit_text(msg_text, parse_mode="HTML", reply_markup=kb.as_markup())

        status_msg = external_status_msg or await cq.message.edit_text("⏳ <i>Checking account health & processing...</i>", parse_mode="HTML")

        target_item_id = item_id
        purchase_successful = False
        item_data = {}
        error_text = ""
        balance_id = await get_lzt_balance_id()

        for attempt in range(3):
            check_url = f"{LZT_BASE}/{target_item_id}/check-account"
            c_status, c_data = await lzt_request("POST", check_url, json_body={}, action_label=f"LZT: Check Account ({target_item_id})")
            
            if c_status != 200:
                target_item_id = await find_exact_replacement(country_code, usd_price)
                if not target_item_id:
                    error_text = "Account failed health check and no identical replacements found."
                    break
                continue 
                
            buy_url = f"{LZT_BASE}/{target_item_id}/fast-buy"
            body = {"price": usd_price, "balance_id": balance_id}
            b_status, b_data = await lzt_request("POST", buy_url, json_body=body, action_label=f"LZT: Fast Buy ({target_item_id})")
            
            if b_status == 200:
                purchase_successful = True
                item_data = b_data.get("item", {})
                break 
            else:
                target_item_id = await find_exact_replacement(country_code, usd_price)
                if not target_item_id:
                    errs = b_data.get("errors", ["Unknown"]) if isinstance(b_data, dict) else ["Unknown"]
                    error_text = "; ".join(errs)
                    break

        if not purchase_successful:
            await log_admin(f"⚠️ <b>LZT Purchase Failed (After Failovers)</b>\n\n👤 <b>User:</b> {username_str} (<code>{user_id}</code>)\n🆔 <b>Last Tried Item:</b> {target_item_id}\n❌ <b>Error:</b> {error_text}")
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back", callback_data="s3tg_open")]])
            return await status_msg.edit_text(f"❌ Purchase failed try another account or try later", parse_mode="HTML", reply_markup=kb)

        raw_phone = item_data.get("telegram_phone") or item_data.get("login") or "N/A"
        phone_number = str(raw_phone)
        if phone_number != "N/A" and not phone_number.startswith("+") and not phone_number.isalpha():
            phone_number = f"+{phone_number}"

        new_balance = balance - sell_price
        users_col.update_one({"_id": user_id}, {"$set": {"balance": new_balance}}, upsert=True)

        country_meta = next((c for c in LZT_ALL_COUNTRIES if c['code'] == country_code), {"name": country_code})
        country_display_name = country_meta['name']

        lzt_orders.insert_one({
            "user_id": user_id, "item_id": target_item_id, "title": item_data.get("title_en") or item_data.get("title"),
            "price_usd": usd_price, "price_inr": sell_price, "number": phone_number,
            "country_code": country_code, "country_name": country_display_name, "server": 3, "status": "delivered", "created_at": datetime.utcnow()
        })

        kb = InlineKeyboardBuilder()
        kb.row(InlineKeyboardButton(text="📋 Copy Number", copy_text=CopyTextButton(text=phone_number)))
        kb.row(InlineKeyboardButton(text="📩 Get Login Code", callback_data=f"s3tg_getcode:{target_item_id}:{country_code}:{usd_price}"))

        success_text = (
            f"<pre>✅ Purchased Successfully!</pre>\n"
            f"➖ <b><u>Server</u></b>: Server 3 (Telegram)\n"
            f"➖ <b><u>Country</u></b>: {country_display_name}\n"
            f"📞 <b>Number:</b> <code>{phone_number}</code>\n"
            f"🏷️ <b>Price:</b> ₹{sell_price:.2f}\n"
            f"💸<b> Balance:</b> ₹{new_balance:.2f}"
        )
        await status_msg.edit_text(success_text, parse_mode="HTML", reply_markup=kb.as_markup())

        # ✅ Success Admin Log 
        await log_admin(
            f"🆕 <b>LZT Account Sold (Server 3 - Telegram)</b>\n\n"
            f"👤 <b>User:</b> {username_str} (<code>{user_id}</code>)\n"
            f"🆔 <b>Final Item ID:</b> <code>{target_item_id}</code>\n"
            f"🌍 <b>Country:</b> {country_display_name} ({country_code})\n"
            f"💰 <b>Cost (LZT):</b> ${usd_price:.2f}\n"
            f"💵 <b>Sold For:</b> ₹{sell_price:.2f}\n"
            f"📞 <b>Number:</b> <code>{phone_number}</code>\n"
            f"ℹ️ <i>If failovers were used, this was the successful item.</i>"
        )

    @dp.callback_query(F.data.startswith("s3tg_getcode:"))
    async def s3tg_getcode(cq: CallbackQuery):
        parts = cq.data.split(":")
        item_id = parts[1]
        country_code = parts[2]
        usd_price = float(parts[3])
        
        await cq.answer("🔄 Fetching login code...", show_alert=False)

        code_url = f"{LZT_BASE}/{item_id}/telegram-login-code"
        status, data = await lzt_request("GET", code_url, action_label=f"LZT: Telegram Login Code ({item_id})")

        if status != 200:
            errs = data.get("errors", ["Unknown"]) if isinstance(data, dict) else ["Unknown"]
            return await bot.send_message(cq.from_user.id, f"❌ Could not fetch login code yet: {'; '.join(errs)}\nTry again in a few seconds.")

        code = None
        if 'codes' in data:
            if isinstance(data['codes'], dict) and 'code' in data['codes']:
                code = str(data['codes']['code'])
            elif isinstance(data['codes'], list) and len(data['codes']) > 0:
                code = str(data['codes'][0].get('code'))
                
        if not code:
            raw_text = json.dumps(data) if isinstance(data, dict) else str(data)
            match = re.search(r"\b\d{4,6}\b", raw_text)
            code = match.group(0) if match else None

        if not code:
            await log_admin(f"⚠️ <b>Could not parse login code — check raw response</b>\nItem: {item_id}", data)
            return await bot.send_message(cq.from_user.id, "⚠️ Code received but couldn't be read automatically. Please contact support.")

        lzt_orders.update_one({"item_id": item_id}, {"$set": {"status": "completed", "otp": code}})
        
        order_info = lzt_orders.find_one({"item_id": str(item_id)})
        phone_number = order_info.get("number") if order_info else None
        
        if not phone_number or phone_number == "Unknown":
            phone_number = "Unknown"
            masked_number = "Hidden"
        else:
            phone_number = str(phone_number)
            if len(phone_number) > 6:
                masked_number = phone_number[:-4] + "****" + phone_number[-2:]
            else:
                masked_number = phone_number

        # 2. 🌍 Accurate Country Name Lookup
        country_meta = next((c for c in LZT_ALL_COUNTRIES if c['code'] == country_code), {"name": country_code})
        country_display_name = country_meta['name']

        password_text = "None"
        try:
            if isinstance(data.get("item"), dict) and data["item"].get("telegram_password"):
                password_text = str(data["item"]["telegram_password"])
        except:
            pass

        channel_message = (
            f"<pre><u>✅ <b>New Number Purchase Successful</b></u></pre>\n\n"
            f"➖ <b><u>Country:</u></b> {country_display_name}\n"
            f"➖ <b><u>Application:</u> Теlegгам 🍷</b>\n\n"
            f"➕ <b>Number: {masked_number} 📞</b>\n"
            f"➕ <b>OTP:</b> <span class='tg-spoiler'>{code}</span> 💬\n"
            f"➕ <b>Server:</b> (3) 🥂\n"
            f"➕ <b>Password:</b> <span class='tg-spoiler'>{password_text}</span> 🔐\n\n"
            f"<b>• @{BOTUSER} || @{CHANNEL}</b>"
        )
        
        buy_button = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="• Buy Now •", url=f"https://t.me/{BOTUSER}?start=s3tg")]]
        )
        
        try: await bot.send_message(SALES, channel_message, parse_mode="HTML", reply_markup=buy_button)
        except: pass

        try: await bot.send_message(SALES, channel_message, parse_mode="HTML", reply_markup=buy_button)
        except: pass

        success_text = (
            f"<pre>Order Completed ✅</pre>\n"
            f"✅ 𝐍𝗨𝐌𝐁𝐄𝐑 - <code>{phone_number}</code>\n"
            f"💬 𝐂𝐎𝐃𝐄 - <code>{code}</code>\n"
            f"💬 𝐏𝐀𝐒𝐒 - <code>{password_text}</code>\n"
        )

        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="Copy OTP", copy_text=CopyTextButton(text=code)),
                    InlineKeyboardButton(text="Copy Pass", copy_text=CopyTextButton(text=password_text))
                ],
                [
                    InlineKeyboardButton(text="• Get Code Again •", callback_data=f"s3tg_getcode:{item_id}:{country_code}:{usd_price}")
                ],
                [
                    InlineKeyboardButton(text="🚫 Remove Bot Devices", callback_data=f"s3tg_reset:{item_id}")
                ],
                [
                    InlineKeyboardButton(text=f"🔄 Buy Again ({country_display_name})", callback_data=f"s3tg_buy_again:{country_code}:{usd_price}")
                ]
            ]
        )
        
        await bot.send_message(
            cq.from_user.id,
            success_text,
            parse_mode="HTML", 
            reply_markup=kb
        )

    @dp.callback_query(F.data.startswith("s3tg_reset:"))
    async def s3tg_reset_auth(cq: CallbackQuery):
        item_id = cq.data.split(":")[1]
        
        await cq.answer("🔄 Terminating other sessions, please wait...", show_alert=False)

        reset_url = f"{LZT_BASE}/{item_id}/telegram-reset-authorizations"
        status, data = await lzt_request("POST", reset_url, action_label=f"LZT: Reset Auth ({item_id})")

        if status == 200:
            success_text = "✅ <b>Bot session has been logged out!</b>\nOther devices kicked successfully."
            await cq.message.answer(success_text, parse_mode="HTML")
        else:
            err_msg = "Unknown Error"
            if isinstance(data, dict) and "errors" in data and len(data["errors"]) > 0:
                err_msg = data["errors"][0]
                
            if "Too new authorization" in err_msg:
                error_text = "⚠️ <b>Telegram Security Limit!</b>\n\nWait at least 24 hours after login before kicking other devices."
            else:
                error_text = f"❌ <b>Failed to logout:</b>\n<code>{err_msg}</code>"
                
            await cq.message.answer(error_text, parse_mode="HTML")

    @dp.callback_query(F.data == "s3tg_my_orders")
    async def s3tg_my_orders(cq: CallbackQuery):
        orders = list(lzt_orders.find({"user_id": cq.from_user.id, "server": 3}).sort("created_at", -1).limit(5))
        
        if not orders:
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back", callback_data="s3tg_open")]])
            return await cq.message.edit_text("📭 <b>You don't have any recent Telegram orders.</b>", parse_mode="HTML", reply_markup=kb)
            
        kb = InlineKeyboardBuilder()
        text = "<b>📋 Your Recent Orders (Server 3):</b>\n<i>You can request OTP if the session is still active!</i>\n\n"
        
        for idx, o in enumerate(orders, 1):
            number = o.get("number", "Unknown")
            country = o.get("country_name", "Unknown")
            item_id = o.get("item_id")
            usd_price = o.get("price_usd", 0)
            
            text += f"<b>{idx}. {country}</b> ➔ <code>{number}</code>\n"
            
            # Har order ke liye alag OTP button
            kb.row(InlineKeyboardButton(text=f"📩 Get OTP ({number})", callback_data=f"s3tg_getcode:{item_id}:{o.get('country_code')}:{usd_price}"))
            
        kb.row(InlineKeyboardButton(text="🔙 Back", callback_data="s3tg_open"))
        
        await cq.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())

    @dp.message(Command("lztbalance"))
    async def cmd_lzt_balance(msg: Message):
        # Sirf admin use kar sakta hai
        if not is_admin(msg.from_user.id): 
            return await msg.answer("❌ Unauthorized.")
        
        checking = await msg.answer("🔄 <i>Fetching live Market Profile...</i>", parse_mode="HTML")
        
        status, data = await lzt_request("GET", LZT_ME_URL, action_label="LZT: Get Balance")
        
        if status != 200 or "user" not in data:
            errs = data.get("errors", ["Unknown API Error"]) if isinstance(data, dict) else ["Unknown API Error"]
            return await checking.edit_text(f"❌ <b>Failed to fetch LZT Profile:</b>\n<code>{'; '.join(errs)}</code>", parse_mode="HTML")
            
        user_data = data["user"]
        
        username = user_data.get("username", "Unknown")
        user_id = user_data.get("user_id", "N/A")
        
        balance_usd = user_data.get("convertedBalance", 0.00)
        balance_rub = user_data.get("balance", "0.00")
        hold_rub = user_data.get("hold", "0.00")
        
        active_items = user_data.get("active_items_count", 0)
        sold_items = user_data.get("sold_items_count", 0)
        
        text = (
            f"<b>📊 LZT Market Admin Dashboard</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 <b>Account:</b> <code>{username}</code> (ID: {user_id})\n\n"
            f"💵 <b>Available USD:</b> ${balance_usd}\n"
            f"🇷🇺 <b>Available RUB:</b> ₽{balance_rub}\n"
            f"🔒 <b>Funds on Hold:</b> ₽{hold_rub}\n\n"
            f"📦 <b>Active Listings:</b> {active_items}\n"
            f"🛍️ <b>Total Items Sold:</b> {sold_items}\n"
            f"━━━━━━━━━━━━━━━━━━━━━"
        )
        
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Close", callback_data="delete_msg")]
        ])
        
        await checking.edit_text(text, parse_mode="HTML", reply_markup=kb)

    global LIVE_PRICE_CACHE
    LIVE_PRICE_CACHE = {c['code']: None for c in LZT_ALL_COUNTRIES}

    # ============================================================
    # ========== SMART PRICE UPDATER & AUTO-PROMO LOOP ===========
    # ============================================================

    async def smart_price_updater():
        """Ultra-Fast Chunked Updater with Pre-filled Cache"""
        global LIVE_PRICE_CACHE
        cycle_count = 0  
        
        sem = asyncio.Semaphore(5)

        async def fetch_price(country_code):
            async with sem:
                params = {
                    "country[]": country_code,
                    "order_by": "price_to_up", 
                    "currency": "usd",
                    "spam": "no",
                    "page": 1
                }
                status, data = await lzt_request("GET", LZT_SEARCH_URL, params=params, action_label=f"Cache: {country_code}", silent=True)
                
                if status == 200 and data.get("items"):
                    LIVE_PRICE_CACHE[country_code] = data["items"][0]["price"]
                else:
                    LIVE_PRICE_CACHE[country_code] = None 

        while True:
            cycle_count += 1
            print(f"🌍 [Cycle {cycle_count}] Updating ALL 198+ countries (Fast Chunked Sync)...")
                
            chunk1 = LZT_ALL_COUNTRIES[:100]
            chunk2 = LZT_ALL_COUNTRIES[100:]

            print("🚀 Fetching first 100 countries...")
            tasks1 = [fetch_price(c['code']) for c in chunk1]
            await asyncio.gather(*tasks1)
            
            print("⏳ First 100 done! Waiting 2 minutes to reset API limit...")
            await asyncio.sleep(120)

            print("🚀 Fetching remaining countries...")
            tasks2 = [fetch_price(c['code']) for c in chunk2]
            await asyncio.gather(*tasks2)

            if cycle_count % 4 == 0:
                try:
                    valid_items = {k: v for k, v in LIVE_PRICE_CACHE.items() if v is not None}
                    sorted_cache = sorted(valid_items.items(), key=lambda x: x[1])
                    top_cheapest = sorted_cache[:8]
                    
                    promo_lines = []
                    for code, usd_price in top_cheapest:
                        inr_price = usd_to_sell_inr(usd_price)
                        country_meta = next((c for c in LZT_ALL_COUNTRIES if c['code'] == code), None)
                        if country_meta:
                            promo_lines.append(f"➖ {country_meta['flag']} <b>{country_meta['name']}</b> : ₹{inr_price:.0f}")
                    
                    if promo_lines:
                        alert_text = (
                            "<b>📉 𝗧𝗲𝗹𝗲𝗴𝗿𝗮𝗺 𝗔𝗰𝗰𝗼𝘂𝗻𝘁𝘀 𝗣𝗿𝗶𝗰𝗲 𝗗𝗿𝗼𝗽 𝗔𝗹𝗲𝗿𝘁!</b>\n\n"
                            "<i>Fresh stock has arrived. Buy now before it's gone!</i>\n\n"
                            + "\n".join(promo_lines) +
                            "\n\n⚡️ <b>Instant Delivery | OTP via Bot</b>"
                        )
                        
                        kb = InlineKeyboardBuilder()
                        kb.row(InlineKeyboardButton(text="🛒 Go to Server 3", url=f"https://t.me/{BOTUSER}?start=s3tg"))
                        
                        await bot.send_message("@TechnoAdsChat", alert_text, parse_mode="HTML", reply_markup=kb.as_markup())
                        print("📢 Successfully posted 4-hourly price drop alert to @TechnoAdsChat!")
                except Exception as e:
                    print(f"⚠️ Failed to send group alert: {e}")

            print("✅ Cycle complete. Sleeping for 1 hour...")
            await asyncio.sleep(3600)

    async def on_startup_s3(dispatcher):
        asyncio.create_task(smart_price_updater())
    
    dp.startup.register(on_startup_s3)
