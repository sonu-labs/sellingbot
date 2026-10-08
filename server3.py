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
from rich_messages import (
    TELEGRAM_EMOJI_ID,
    WHATSAPP_EMOJI_ID,
    tg_icon,
    wa_icon,
    premium_button,
)

TEMPORASMS_BASE = "https://api.temporasms.com/stubs/handler_api.php"
LZT_BASE = "https://prod-api.lzt.market"
LZT_SEARCH_URL = f"{LZT_BASE}/telegram"
LZT_ME_URL = f"{LZT_BASE}/me"

LZT_PER_PAGE = 10
COUNTRY_PER_PAGE = 20     
MAX_RETRY_REQUEST_ATTEMPTS = 15
RETRY_DELAY_SECONDS = 1.5
RATE_LIMIT_RETRY_DELAY = 3.0

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
    return {"wa": {"name": "WhatsApp", "emoji": wa_icon(), "toggle_field": "s3_wa_status"}}.get(
        category, {"name": category.upper(), "emoji": tg_icon(), "toggle_field": "s3_unknown_status"}
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

    # 🔒 Simple per-user debounce lock (prevents double/spam taps on buttons)
    _s3_user_locks = {}
    _S3_LOCK_TTL = 1.5  # seconds

    async def is_locked_for_user(cq):
        uid = cq.from_user.id
        now = time.time()
        last = _s3_user_locks.get(uid, 0)
        if now - last < _S3_LOCK_TTL:
            try:
                await cq.answer("⏳ Please wait...", show_alert=False)
            except Exception:
                pass
            return True
        _s3_user_locks[uid] = now
        return False

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

    def get_user_discount(user_id):
        if not user_id:
            return 0.0
        u = users_col.find_one({"_id": user_id}) or {}
        try:
            pct = float(u.get("discount_percent", 0.0) or 0.0)
        except (TypeError, ValueError):
            pct = 0.0
        return max(0.0, min(100.0, pct))

    def apply_user_discount(amount, user_id=None):
        pct = get_user_discount(user_id)
        if pct <= 0:
            return round(float(amount), 2)
        return round(float(amount) * (1.0 - pct / 100.0), 2)

    def get_lzt_token():
        return get_settings().get("lzt_token")

    def get_lzt_margin():
        return get_settings().get("lzt_margin_percent", 20)

    def usd_to_sell_inr(usd_price, user_id=None):
        margin = get_lzt_margin()
        base = round(usd_price * exchange_rate * (1 + margin / 100), 2)
        return apply_user_discount(base, user_id)

    def format_time_ago(ts):
        """Converts a unix timestamp (telegram_last_seen etc.) into a human readable 'x ago' string."""
        if not ts:
            return "Unknown"
        try:
            ts = int(ts)
        except (TypeError, ValueError):
            return "Unknown"

        now = int(time.time())
        diff = now - ts
        if diff < 0:
            diff = 0

        if diff < 60:
            return "Just now"
        minutes = diff // 60
        if minutes < 60:
            return f"{minutes} min ago"
        hours = diff // 3600
        if hours < 24:
            return f"{hours} hour{'s' if hours != 1 else ''} ago"
        days = diff // 86400
        if days < 30:
            return f"{days} day{'s' if days != 1 else ''} ago"
        months = days // 30
        if months < 12:
            return f"{months} month{'s' if months != 1 else ''} ago"
        years = days // 365
        return f"{years} year{'s' if years != 1 else ''} ago"

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

    async def render_manage3_dashboard(target, edit: bool = False):
        s = get_settings()
        s3_wa = s.get("s3_wa_status", "on")
        lzt_enabled = s.get("lzt_enabled", False)
        usd_disp = s.get("s3_usd_display", "off")
        debug_mode = s.get("s3_debug_mode", "on")
        lzt_margin = get_lzt_margin()
        tempora_bal = await get_tempora_balance()

        kb = InlineKeyboardBuilder()
        kb.row(
            InlineKeyboardButton(
                text=f"💬 WhatsApp: {'🟢 ON' if s3_wa == 'on' else '🔴 OFF'}",
                callback_data="s3_toggle:wa",
                icon_custom_emoji_id=WHATSAPP_EMOJI_ID,
            ),
            InlineKeyboardButton(
                text=f"✈️ Telegram (LZT): {'🟢 ON' if lzt_enabled else '🔴 OFF'}",
                callback_data="s3_toggle:lzt",
                icon_custom_emoji_id=TELEGRAM_EMOJI_ID,
            ),
        )
        kb.row(
            InlineKeyboardButton(text=f"💲 USD Display: {'🟢 ON' if usd_disp == 'on' else '🔴 OFF'}", callback_data="s3_toggle:usd"),
            InlineKeyboardButton(text=f"🐛 Debug Log: {'🟢 ON' if debug_mode == 'on' else '🔴 OFF'}", callback_data="s3_toggle:debug"),
        )
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
        kb.row(
            InlineKeyboardButton(text="🧪 Test LZT API", callback_data="lzt_test"),
            InlineKeyboardButton(text="📊 LZT Balance & Sales", callback_data="lzt_balance_btn"),
        )
        kb.row(
            InlineKeyboardButton(text="⬅️ Admin Panel", callback_data="admin_refresh"),
            InlineKeyboardButton(text="❌ Close", callback_data="delete_msg"),
        )

        text = (
            f"<h2>🛰️ Server 3 & LZT Manager</h2>"
            f"<blockquote>"
            f"{wa_icon()} <b>WhatsApp (TemporaSMS):</b> <code>{tempora_bal:.4f} USDT</code> ({'🟢 ON' if s3_wa == 'on' else '🔴 OFF'})\n"
            f"{tg_icon()} <b>Telegram (LZT Market):</b> Margin <code>{lzt_margin}%</code> ({'🟢 ON' if lzt_enabled else '🔴 OFF'})\n"
            f"⚙️ <b>USD Display:</b> <code>{usd_disp.upper()}</code>  •  🐛 <b>Debug:</b> <code>{debug_mode.upper()}</code>"
            f"</blockquote>"
            f"<p><i>Configure live WhatsApp OTP catalogs, LZT Market API tokens, profit margins, and diagnostics below:</i></p>"
        )
        if edit:
            try:
                await target.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())
                return
            except Exception:
                pass
        await target.answer(text, parse_mode="HTML", reply_markup=kb.as_markup())

    @dp.message(Command("manage3"))
    async def cmd_manage3(msg: Message):
        if not is_admin(msg.from_user.id):
            return await msg.answer("<h2>❌ Unauthorized</h2><p>Admin access required.</p>", parse_mode="HTML")
        await render_manage3_dashboard(msg, edit=False)

    @dp.callback_query(F.data == "s3m_home")
    async def cb_manage3_home(cq: CallbackQuery, state: FSMContext):
        if not is_admin(cq.from_user.id):
            return await cq.answer("Unauthorized", show_alert=True)
        await state.clear()
        await cq.answer()
        await render_manage3_dashboard(cq.message, edit=True)

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
        await render_manage3_dashboard(cq.message, edit=True)

    @dp.callback_query(F.data == "lzt_settoken")
    async def lzt_settoken(cq: CallbackQuery, state: FSMContext):
        if not is_admin(cq.from_user.id): return
        await state.set_state(Server3Admin.waiting_lzt_token)
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back", callback_data="s3m_home")]])
        await cq.message.edit_text(
            "<h2>🔑 Set LZT Market Token</h2>"
            "<blockquote>Send your LZT Market API Bearer token below:</blockquote>",
            parse_mode="HTML",
            reply_markup=kb,
        )
        await cq.answer()

    @dp.message(StateFilter(Server3Admin.waiting_lzt_token))
    async def lzt_settoken_save(msg: Message, state: FSMContext):
        token = msg.text.strip()
        settings_col.update_one({"_id": "server_config"}, {"$set": {"lzt_token": token}}, upsert=True)
        await state.clear()
        try: await msg.delete()
        except Exception: pass
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Server 3 Manager", callback_data="s3m_home")]])
        await msg.answer(
            "<h2>✅ LZT Token Saved</h2>"
            "<blockquote>Your LZT Market token has been updated. Use <b>🧪 Test LZT API</b> to verify connectivity.</blockquote>",
            parse_mode="HTML",
            reply_markup=kb,
        )

    @dp.callback_query(F.data == "lzt_setmargin")
    async def lzt_setmargin(cq: CallbackQuery, state: FSMContext):
        if not is_admin(cq.from_user.id): return
        await state.set_state(Server3Admin.waiting_lzt_margin)
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back", callback_data="s3m_home")]])
        await cq.message.edit_text(
            "<h2>💵 Set LZT Profit Margin</h2>"
            "<blockquote>Enter your profit margin percentage (e.g. <code>20</code> = LZT cost + 20%):</blockquote>",
            parse_mode="HTML",
            reply_markup=kb,
        )
        await cq.answer()

    @dp.message(StateFilter(Server3Admin.waiting_lzt_margin))
    async def lzt_setmargin_save(msg: Message, state: FSMContext):
        try: margin = float(msg.text.strip())
        except ValueError: return await msg.answer("<h2>❌ Invalid Input</h2><p>Please send a valid number.</p>", parse_mode="HTML")
        settings_col.update_one({"_id": "server_config"}, {"$set": {"lzt_margin_percent": margin}}, upsert=True)
        await state.clear()
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Server 3 Manager", callback_data="s3m_home")]])
        await msg.answer(
            f"<h2>✅ Margin Updated</h2><blockquote>LZT profit margin is now set to <b>{margin}%</b>.</blockquote>",
            parse_mode="HTML",
            reply_markup=kb,
        )

    @dp.callback_query(F.data == "lzt_test")
    async def lzt_test(cq: CallbackQuery):
        if not is_admin(cq.from_user.id): return
        await cq.answer("Testing LZT connection...", show_alert=False)
        status, data = await lzt_request("GET", LZT_SEARCH_URL, params={"country[]": "IN", "order_by": "price_to_up", "currency": "usd", "page": 1}, action_label="LZT: Connection Test")
        if status == 200 and "items" in data:
            count = len(data["items"])
            await bot.send_message(
                cq.from_user.id,
                f"<h2>✅ LZT Connection OK</h2>"
                f"<blockquote>India search returned <b>{count}</b> live items right now.</blockquote>"
                f"<p><i>Check ADMINLOG for full raw API response.</i></p>",
                parse_mode="HTML",
            )
        else:
            errs = data.get("errors", ["Unknown"]) if isinstance(data, dict) else ["Unknown"]
            await bot.send_message(
                cq.from_user.id,
                f"<h2>❌ LZT Test Failed (HTTP {status})</h2>"
                f"<blockquote><code>{escape('; '.join(errs))}</code></blockquote>"
                f"<p><i>Full raw response sent to ADMINLOG.</i></p>",
                parse_mode="HTML",
            )

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
            f"<h2>✅ Catalog Saved!</h2>"
            f"<blockquote>"
            f"{wa_icon()} <b>WhatsApp</b> (code: <code>{final_code}</code>)\n"
            f"🌍 <b>{data['country_name']}</b> | Op <code>{data['operator']}</code>\n"
            f"💵 <b>Price:</b> <code>₹{data['sell_price_inr']:.2f}</code>  •  🛡️ <b>Cap:</b> <code>{max_price} USDT</code>\n"
            f"📡 <b>Live Cost:</b> <code>{live_price:.4f} USDT</code> (₹{live_price_inr:.2f})\n"
            f"📈 <b>Margin:</b> <code>₹{margin:.2f}</code>"
            f"</blockquote>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Server 3 Manager", callback_data="s3m_home")]]),
        )
        await state.clear()

    @dp.callback_query(F.data == "s3_rem_item")
    async def s3_rem_start(cq: CallbackQuery):
        if not is_admin(cq.from_user.id): return
        items = list(s3_catalog.find({}))
        if not items:
            return await cq.message.edit_text(
                "<h2>📭 Catalog Empty</h2><p>No WhatsApp Server 3 catalog items found.</p>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back", callback_data="s3m_home")]]),
            )
        kb = InlineKeyboardBuilder()
        for i in items:
            kb.button(text=f"❌ {i['country_name']} Op{i['operator']} ({i['service_code']})", callback_data=f"s3_del:{str(i['_id'])}")
        kb.adjust(1)
        kb.row(InlineKeyboardButton(text="🔙 Back", callback_data="s3m_home"))
        await cq.message.edit_text(
            "<h2>🗑️ Remove WA Catalog Item</h2><p>Tap an entry below to delete it from Server 3:</p>",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )

    @dp.callback_query(F.data.startswith("s3_del:"))
    async def s3_del_execute(cq: CallbackQuery):
        if not is_admin(cq.from_user.id): return
        s3_catalog.delete_one({"_id": ObjectId(cq.data.split(":")[1])})
        await cq.answer("✅ Deleted!", show_alert=True)
        await s3_rem_start(cq)

    @dp.callback_query(F.data == "s3_price_check")
    async def s3_price_checker(cq: CallbackQuery):
        if not is_admin(cq.from_user.id): return
        await cq.message.edit_text(
            "<h2>🔄 Fetching Live API Costs</h2><p><i>Auditing all WhatsApp catalog routes...</i></p>",
            parse_mode="HTML",
        )
        catalog = list(s3_catalog.find({}))
        if not catalog:
            return await cq.message.edit_text(
                "<h2>❌ No Items</h2><p>The WhatsApp catalog is empty.</p>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back", callback_data="s3m_home")]]),
            )
        lines = ["<h2>🔍 WA Live Margin Audit</h2>"]
        for item in catalog:
            is_valid, err, cost = await check_live_combo(item['service_code'], item['country_code'], item['operator'])
            if not is_valid:
                lines.append(
                    f"<blockquote>⚠️ <b>{item['country_name']} Op{item['operator']}</b> — <code>{err}</code> (dead route)</blockquote>"
                )
                continue
            cost_inr = cost * exchange_rate
            margin = item['sell_price_inr'] - cost_inr
            e = "🟢" if margin > 0 else "🔴"
            lines.append(
                f"<blockquote>{e} <b>{item['country_name']} Op{item['operator']}</b>\n"
                f"Cost: <code>₹{cost_inr:.2f}</code> | Sell: <code>₹{item['sell_price_inr']:.2f}</code> | Profit: <b>₹{margin:.2f}</b></blockquote>"
            )
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back", callback_data="s3m_home")]])
        await cq.message.edit_text("".join(lines), parse_mode="HTML", reply_markup=kb)

    @dp.callback_query(F.data == "s3_edit_list")
    async def s3_edit_start(cq: CallbackQuery):
        if not is_admin(cq.from_user.id): return
        items = list(s3_catalog.find({}))
        if not items:
            return await cq.message.edit_text(
                "<h2>📭 Catalog Empty</h2><p>No WhatsApp catalog items found.</p>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back", callback_data="s3m_home")]]),
            )
        kb = InlineKeyboardBuilder()
        for i in items:
            kb.button(text=f"✏️ {i['country_name']} Op{i['operator']} (₹{i['sell_price_inr']})", callback_data=f"s3_ed_item:{str(i['_id'])}")
        kb.adjust(1)
        kb.row(InlineKeyboardButton(text="🔙 Back", callback_data="s3m_home"))
        await cq.message.edit_text(
            "<h2>✏️ Edit WA Catalog Item</h2><p>Select a route below to update its price or USDT cap:</p>",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )

    @dp.callback_query(F.data.startswith("s3_ed_item:"))
    async def s3_edit_options(cq: CallbackQuery, state: FSMContext):
        if not is_admin(cq.from_user.id): return
        item = s3_catalog.find_one({"_id": ObjectId(cq.data.split(":")[1])})
        if not item: return await cq.answer("Not found!", show_alert=True)
        await state.update_data(edit_item_id=str(item["_id"]), item_name=f"{item['country_name']} (Op {item['operator']})")
        text = (
            f"<h2>🛠️ {item['country_name']}</h2>"
            f"<blockquote>"
            f"📶 <b>Operator:</b> <code>{item['operator']}</code>\n"
            f"💰 <b>Selling Price:</b> <code>₹{item['sell_price_inr']:.2f}</code>\n"
            f"🛡️ <b>Max Price Cap:</b> <code>{item['max_price_cap']} USDT</code>"
            f"</blockquote>"
        )
        kb = InlineKeyboardBuilder()
        kb.row(InlineKeyboardButton(text="💰 Change Price", callback_data="s3_chg:price"), InlineKeyboardButton(text="🛡️ Change Cap", callback_data="s3_chg:cap"))
        kb.row(InlineKeyboardButton(text="🔙 Back", callback_data="s3_edit_list"))
        await cq.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())

    @dp.callback_query(F.data.startswith("s3_chg:"))
    async def s3_edit_prompt(cq: CallbackQuery, state: FSMContext):
        action = cq.data.split(":")[1]
        data = await state.get_data()
        if action == "price":
            await cq.message.edit_text(f"<h2>💰 Update Price</h2><blockquote>Enter new INR price for <b>{data['item_name']}</b>:</blockquote>", parse_mode="HTML")
            await state.set_state(Server3Edit.waiting_new_price)
        else:
            await cq.message.edit_text(f"<h2>🛡️ Update Cap</h2><blockquote>Enter new max USDT cap for <b>{data['item_name']}</b>:</blockquote>", parse_mode="HTML")
            await state.set_state(Server3Edit.waiting_new_cap)

    @dp.message(StateFilter(Server3Edit.waiting_new_price))
    async def s3_save_new_price(msg: Message, state: FSMContext):
        try: new_inr = float(msg.text.strip())
        except: return await msg.answer("<h2>❌ Invalid Number</h2>", parse_mode="HTML")
        data = await state.get_data()
        s3_catalog.update_one({"_id": ObjectId(data['edit_item_id'])}, {"$set": {"sell_price_inr": new_inr, "sell_price_usd": new_inr / exchange_rate}})
        await msg.answer(
            f"<h2>✅ Price Updated</h2><blockquote>New selling price: <b>₹{new_inr:.2f}</b></blockquote>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Server 3 Manager", callback_data="s3m_home")]]),
        )
        await state.clear()

    @dp.message(StateFilter(Server3Edit.waiting_new_cap))
    async def s3_save_new_cap(msg: Message, state: FSMContext):
        try: new_cap = float(msg.text.strip())
        except: return await msg.answer("<h2>❌ Invalid Number</h2>", parse_mode="HTML")
        data = await state.get_data()
        s3_catalog.update_one({"_id": ObjectId(data['edit_item_id'])}, {"$set": {"max_price_cap": new_cap}})
        await msg.answer(
            f"<h2>✅ Cap Updated</h2><blockquote>New max API cap: <b>{new_cap} USDT</b></blockquote>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Server 3 Manager", callback_data="s3m_home")]]),
        )
        await state.clear()

    # ============================================================
    # ==================  WA: USER BUY FLOW  =======================
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
            return await cq.message.edit_text(
                "<h2>📭 No Countries Available</h2><p>All regions are temporarily out of stock. Please check back shortly.</p>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back", callback_data="back_main")]]),
            )
        kb = InlineKeyboardBuilder()
        for c in countries:
            kb.button(text=c['name'], callback_data=f"s3_user_ops:{category}:{c['_id']}")
        kb.adjust(2)
        kb.row(InlineKeyboardButton(text="🔙 Back", callback_data="back_main"))
        text = (
            f"<h2>{cm['emoji']} {cm['name']} Registration [Server 3]</h2>"
            f"<blockquote>🌍 <b>Select a Region Below:</b></blockquote>"
            f"<p><i>⚡ Instant automated SMS OTP delivery with auto-refund protection.</i></p>"
        )
        await cq.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())

    @dp.callback_query(F.data.startswith("s3_user_ops:"))
    async def user_s3_operators(cq: CallbackQuery):
        _, category, country_code = cq.data.split(":")
        cm = category_meta(category)
        items = list(s3_catalog.find({"category": category, "country_code": country_code}))
        if not items: return await cq.answer("No operators found.", show_alert=True)
        country_name = items[0]['country_name']
        usd_toggle = get_settings().get("s3_usd_display", "off")
        disc = get_user_discount(cq.from_user.id)
        kb = InlineKeyboardBuilder()
        for item in items:
            inr_p = apply_user_discount(item['sell_price_inr'], cq.from_user.id)
            usd_p = inr_p / exchange_rate
            btn = f"📶 Operator {item['operator']} • ₹{inr_p:.2f}" + (f" (${usd_p:.2f})" if usd_toggle == "on" else "")
            kb.button(text=btn, callback_data=f"s3_user_terms:{str(item['_id'])}")
        kb.adjust(1)
        kb.row(InlineKeyboardButton(text="🔙 Back", callback_data=f"buy_server3:{category}"))
        disc_line = f"\n🏷️ <b>VIP Discount Active:</b> <code>{disc:g}% OFF</code>" if disc > 0 else ""
        text = (
            f"<h2>{cm['emoji']} {cm['name']} — {country_name}</h2>"
            f"<blockquote>📶 <b>Select an operator line below:</b>{disc_line}</blockquote>"
        )
        await cq.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())

    @dp.callback_query(F.data.startswith("s3_user_terms:"))
    async def user_s3_terms(cq: CallbackQuery):
        item = s3_catalog.find_one({"_id": ObjectId(cq.data.split(":")[1])})
        if not item: return await cq.answer("Error.")
        cm = category_meta(item['category'])
        usd_toggle = get_settings().get("s3_usd_display", "off")
        disc = get_user_discount(cq.from_user.id)
        inr_p = apply_user_discount(item['sell_price_inr'], cq.from_user.id)
        usd_p = inr_p / exchange_rate
        price_label = f"₹{inr_p:.2f} | ${usd_p:.2f}" if usd_toggle == "on" else f"₹{inr_p:.2f}"
        disc_line = f"\n🏷️ <b>VIP Discount:</b> <code>{disc:g}% OFF</code>" if disc > 0 else ""
        text = (
            "<h2>🛒 Order Confirmation & Terms</h2>"
            "<blockquote>"
            f"{cm['emoji']} <b>App:</b> {cm['name']}\n"
            f"🌍 <b>Region:</b> {item['country_name']}\n"
            f"📶 <b>Operator:</b> Line {item['operator']}\n"
            f"💰 <b>Price:</b> <code>{price_label}</code>{disc_line}"
            "</blockquote>"
            "<p>📌 <b>Zero-Risk Billing:</b> You are only charged if the OTP is delivered.</p>"
            "<blockquote>"
            "🚨 <b>Terms & Conditions:</b>\n"
            "• If no OTP arrives in time, cancel and try another line at <b>zero cost</b>.\n"
            "• Always use a <b>VPN/Proxy</b> matching the number's region or a clean device.\n"
            "• <b>No refund</b> once the OTP code has been delivered."
            "</blockquote>"
            "<p><i>⚠️ Tap <b>Accept & Reserve</b> to allocate your number immediately.</i></p>"
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
        refund_amount = order.get("price") if order.get("price") is not None else (item["sell_price_inr"] if item else 0.0)
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
                    f"<h2>✅ Order Completed</h2>"
                    f"<blockquote>"
                    f"{cm['emoji']} <b>App:</b> {cm['name']}\n"
                    f"📞 <b>Number:</b> <code>{phone_number}</code>\n"
                    f"🔑 <b>OTP Code:</b> <code>{otp_code}</code>"
                    f"</blockquote>"
                    f"<p><i>Tap the button below to copy your OTP code!</i></p>"
                )
                try:
                    await bot.edit_message_text(text=success_text, chat_id=user_id, message_id=msg_to_edit, parse_mode="HTML", reply_markup=kb)
                except:
                    await bot.send_message(user_id, success_text, parse_mode="HTML", reply_markup=kb)

                public_log = (
                    f"<h2>✅ New {cm['name']} Purchase</h2>"
                    f"<blockquote>"
                    f"🌍 <b>Country:</b> {item['country_name']}\n"
                    f"{cm['emoji']} <b>Application:</b> {cm['name']}\n"
                    f"📞 <b>Number:</b> <code>{mask_phone(phone_number)}</code>\n"
                    f"🔑 <b>Code:</b> <code>••••</code>"
                    f"</blockquote>"
                    f"<p><b>• @{BOTUSER} || @{CHANNEL}</b></p>"
                )
                kb_pub = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="• Buy Now •", url=f"https://t.me/{BOTUSER}?start=s3wa")]])
                try: await bot.send_message(SALES, public_log, parse_mode="HTML", reply_markup=kb_pub)
                except: pass

                admin_log = (
                    f"<h2>📢 Store Sale Alert ({cm['name']})</h2>"
                    f"<blockquote>"
                    f"🌍 <b>Country:</b> {item['country_name']}\n"
                    f"📞 <b>Number:</b> <code>{phone_number}</code>\n"
                    f"🔑 <b>OTP:</b> <code>{otp_code}</code>\n"
                    f"👤 <b>Buyer:</b> <code>{user_id}</code>\n"
                    f"💵 <b>Price:</b> ₹{item['sell_price_inr']}"
                    f"</blockquote>"
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
            cancel_text = (
                f"<h2>⚠️ Order Auto-Cancelled (Timeout)</h2>"
                f"<blockquote>"
                f"No OTP was received in time.\n"
                f"💰 <b>Refunded:</b> <code>₹{refund_amount:.2f}</code>"
                f"</blockquote>"
            )
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
        inr_price = apply_user_discount(item["sell_price_inr"], user_id)
        if user.get("balance", 0.0) < inr_price:
            return await cq.answer("❌ Insufficient Balance!", show_alert=True)

        status_msg = await cq.message.edit_text(
            "<h2>⏳ Allocating Number...</h2><p><i>Connecting to Server 3 and reserving your line...</i></p>",
            parse_mode="HTML",
        )
        api_bal = await get_tempora_balance()
        if api_bal <= 0.1:
            try: await bot.send_message(ADMINLOG, f"🚨 <b>CRITICAL: TemporaSMS Balance Low! ({api_bal} USDT)</b>", parse_mode="HTML")
            except: pass
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back", callback_data=f"buy_server3:{item['category']}")]])
            return await status_msg.edit_text("<h2>❌ Server Busy</h2><p>Please try again shortly.</p>", parse_mode="HTML", reply_markup=kb)

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
            return await status_msg.edit_text("<h2>❌ Out of Stock</h2><p>No numbers available on this operator right now. Please try another line.</p>", parse_mode="HTML", reply_markup=kb)

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
            f"<h2>🟢 {cm['emoji']} {cm['name']} Number Allocated</h2>"
            f"<blockquote>"
            f"🌍 <b>Country:</b> {item['country_name']}\n"
            f"📞 <b>Number:</b> <code>{phone_number}</code>\n"
            f"🏷️ <b>Price:</b> <code>₹{inr_price:.2f}</code>\n"
            f"💸 <b>Remaining Balance:</b> <code>₹{new_balance:.2f}</code>"
            f"</blockquote>"
            f"<p><i>⏳ Waiting for SMS OTP... Auto-polling is active.</i></p>"
            f"<p>🔒 <b>Security Tip:</b> Use a VPN or proxy matching the region while registering.</p>"
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
                f"<h2>❌ Order Cancelled</h2><blockquote>💰 <b>Refunded:</b> <code>₹{refund_amount:.2f}</code></blockquote>", 
                parse_mode="HTML", 
                reply_markup=kb
            )
        else:
            await cq.answer("❌ Already processed.", show_alert=True)


    @dp.callback_query(F.data.startswith("s3_change:"))
    async def srv3_change_number(cq: CallbackQuery):
        order_id, item_id = cq.data.split(":")[1], cq.data.split(":")[2]
        user_id = cq.from_user.id
        
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
            return await cq.message.edit_text("<h2>❌ Catalog Item Unavailable</h2>", parse_mode="HTML")
            
        status_msg = await cq.message.edit_text("<h2>⏳ Replacing Number...</h2><p><i>Previous number cancelled. Reserving a new line...</i></p>", parse_mode="HTML")
        
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
    DEFAULT_FILTERS = {
        "origin": "any",
        "2fa": "any",
        "premium": "any",
        "spam": "no",
        "email": "any"
    }

    def get_user_filters(user_id):
        user = users_col.find_one({"_id": user_id}) or {}
        saved = user.get("lzt_filters") or {}
        merged = {**DEFAULT_FILTERS, **saved}
        return merged

    def update_user_filter(user_id, key, value):
        users_col.update_one({"_id": user_id}, {"$set": {f"lzt_filters.{key}": value}}, upsert=True)

    def build_country_page(page, query=None):
        pool = list(LZT_ALL_COUNTRIES)

        if query:
            q = query.lower()
            pool = [c for c in pool if q in c['name'].lower() or q in c['code'].lower()]

        total_pages = max(1, (len(pool) + COUNTRY_PER_PAGE - 1) // COUNTRY_PER_PAGE)
        page = max(0, min(page, total_pages - 1))
        start = page * COUNTRY_PER_PAGE
        page_items = pool[start:start + COUNTRY_PER_PAGE]

        kb = InlineKeyboardBuilder()
        if not page_items:
            kb.row(InlineKeyboardButton(text="🔍 Search Again", callback_data="s3tg_search"))
            kb.row(InlineKeyboardButton(text="🔙 Back", callback_data="s3tg_cpage:0:"))
            return "<h2>📭 No Countries Found</h2><p>No countries matched your search query.</p>", kb.as_markup()

        for c in page_items:
            btn_text = f"{c['flag']} {c['name']}"
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

        kb.row(
            InlineKeyboardButton(text="⚙️ Filters", callback_data="s3tg_filters"),
            InlineKeyboardButton(text="🔍 Search", callback_data="s3tg_search")
        )

        kb.row(InlineKeyboardButton(text="📋 My Orders", callback_data="s3tg_my_orders"))
        kb.row(InlineKeyboardButton(text="🔙 Back to Servers", callback_data="buy"))

        text = (
            f"<h2>{tg_icon()} Telegram Accounts — Server 3</h2>"
            "<blockquote>🌍 <b>Select a country below:</b>\n"
            "Live stock and pricing are fetched in real time when you open a region.</blockquote>"
        )
        return text, kb.as_markup()

    async def send_lzt_countries_message(chat_id, category="tg"):
        if category == "wa":
            cm = category_meta("wa")
            pipeline = [{"$match": {"category": "wa"}}, {"$group": {"_id": "$country_code", "name": {"$first": "$country_name"}}}]
            countries = list(s3_catalog.aggregate(pipeline))
            kb = InlineKeyboardBuilder()
            for c in countries:
                kb.button(text=c['name'], callback_data=f"s3_user_ops:wa:{c['_id']}")
            kb.adjust(2)
            kb.row(InlineKeyboardButton(text="🔙 Back", callback_data="back_main"))
            text = (
                f"<h2>{cm['emoji']} {cm['name']} Registration [Server 3]</h2>"
                "<blockquote>🌍 <b>Select a Region Below:</b></blockquote>"
                "<p><i>⚡ Instant automated SMS OTP delivery with auto-refund protection.</i></p>"
            )
            try:
                await bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=kb.as_markup())
            except Exception:
                pass
            return
        text, markup = build_country_page(0)
        try:
            await bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=markup)
        except Exception:
            pass

    # ============================================================
    # 🎛️ FILTER MENU & TOGGLES
    # ============================================================
    async def _render_filters_menu(cq: CallbackQuery):
        user_id = cq.from_user.id
        f = get_user_filters(user_id)

        kb = InlineKeyboardBuilder()

        origin_labels = {"any": "Any", "autoreg": "Autoreg 🤖", "phishing": "Phishing 🎣", "resale": "Resale 🔄"}
        tfa_labels = {"any": "Any", "0": "No 🔓", "1": "Yes 🔐"}
        prem_labels = {"any": "Any", "no": "No ❌", "yes": "Yes 🌟"}
        spam_labels = {"any": "Any", "no": "Clean ✅", "yes": "Limited 🚫"}
        email_labels = {"any": "Any", "no": "No ❌", "yes": "Yes 📧"}

        kb.row(InlineKeyboardButton(text=f"Origin: {origin_labels[f['origin']]}", callback_data="s3tg_ft:origin"))
        kb.row(InlineKeyboardButton(text=f"Attached Email: {email_labels[f['email']]}", callback_data="s3tg_ft:email"))
        kb.row(InlineKeyboardButton(text=f"2FA Password: {tfa_labels[f['2fa']]}", callback_data="s3tg_ft:2fa"))
        kb.row(InlineKeyboardButton(text=f"Premium: {prem_labels[f['premium']]}", callback_data="s3tg_ft:premium"))
        kb.row(InlineKeyboardButton(text=f"Spam Status: {spam_labels[f['spam']]}", callback_data="s3tg_ft:spam"))

        kb.row(InlineKeyboardButton(text="🔄 Reset Filters", callback_data="s3tg_freset"))
        kb.row(InlineKeyboardButton(text="🔙 Back", callback_data="s3tg_open"))

        text = (
            "<h2>⚙️ Search Filters</h2>"
            "<blockquote>Configure what types of Telegram accounts you want to see in the catalog.\n"
            "Tap any filter button below to cycle options (<b>Any ➔ Yes ➔ No</b>).</blockquote>"
        )
        await cq.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())

    @dp.callback_query(F.data == "s3tg_filters")
    async def s3tg_filters_menu(cq: CallbackQuery):
        if await is_locked_for_user(cq): return
        await _render_filters_menu(cq)

    @dp.callback_query(F.data.startswith("s3tg_ft:"))
    async def s3tg_filter_toggle(cq: CallbackQuery):
        if await is_locked_for_user(cq): return
        key = cq.data.split(":")[1]
        f = get_user_filters(cq.from_user.id)

        if key == "origin": cycle = ["any", "autoreg", "phishing", "resale"]
        elif key == "2fa": cycle = ["any", "0", "1"]
        elif key == "premium": cycle = ["any", "no", "yes"]
        elif key == "spam": cycle = ["no", "yes", "any"]
        elif key == "email": cycle = ["any", "yes", "no"]

        idx = cycle.index(f[key])
        next_val = cycle[(idx + 1) % len(cycle)]

        update_user_filter(cq.from_user.id, key, next_val)
        await _render_filters_menu(cq)

    @dp.callback_query(F.data == "s3tg_freset")
    async def s3tg_filter_reset(cq: CallbackQuery):
        if await is_locked_for_user(cq): return
        users_col.update_one({"_id": cq.from_user.id}, {"$set": {"lzt_filters": DEFAULT_FILTERS}}, upsert=True)
        await cq.answer("✅ Filters Reset to Default!", show_alert=False)
        await _render_filters_menu(cq)

    # ============================================================
    # 📜 SERVER 3: 24-HOUR TERMS & CONDITIONS + OPEN
    # ============================================================
    @dp.callback_query(F.data == "s3tg_open")
    async def s3tg_open(cq: CallbackQuery):
        if not get_settings().get("lzt_enabled", False):
            return await cq.answer("⚠️ Currently unavailable.", show_alert=True)
        
        user_id = cq.from_user.id
        user_data = users_col.find_one({"_id": user_id}) or {}
        
        last_accepted = user_data.get("s3_tc_accepted_at", 0)
        current_time = time.time()
        
        if current_time - last_accepted > 86400:
            text = (
                f"<h2>🚨 {tg_icon()} Server 3: Terms & Conditions</h2>"
                "<p><i>Please read carefully before accessing the Telegram Market:</i></p>"
                "<blockquote>"
                "🎣 <b>Account Origin:</b> Automated/market accounts — buy at your own risk.\n"
                "📨 <b>OTP Policy:</b> On some accounts, OTP may not arrive. <b>No refunds</b> for missing OTPs.\n"
                "❄️ <b>Limits & Bans:</b> We are not responsible for limitations, freezes, or bans after login.\n"
                "⚙️ <b>Filters & Health Check:</b> Use <b>⚙️ Filters</b> to sort by Premium, 2FA, Email, and Spam status. Account health is verified automatically right before purchase!\n"
                "🚫 <b>Final Sale:</b> All sales are final once purchased."
                "</blockquote>"
                "<p><i>⚠️ Tap <b>I Agree & Continue</b> to accept (shown once every 24 hours).</i></p>"
            )
            
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ I Agree & Continue", callback_data="s3tg_accept_tc")],
                [InlineKeyboardButton(text="🔙 Cancel", callback_data="buy")]
            ])
            return await cq.message.edit_text(text, parse_mode="HTML", reply_markup=kb)

        text, markup = build_country_page(0)
        await cq.message.edit_text(text, parse_mode="HTML", reply_markup=markup)

    @dp.callback_query(F.data == "s3tg_accept_tc")
    async def s3tg_accept_tc(cq: CallbackQuery):
        user_id = cq.from_user.id
        users_col.update_one(
            {"_id": user_id}, 
            {"$set": {"s3_tc_accepted_at": time.time()}}, 
            upsert=True
        )
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
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back", callback_data="s3tg_open")]])
        await cq.message.edit_text(
            "<h2>🔍 Search Country</h2><blockquote>Type a country name or ISO code to filter the Server 3 catalog:</blockquote>",
            parse_mode="HTML",
            reply_markup=kb,
        )
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
        await cq.message.edit_text("<h2>🔎 Fetching Live Listings...</h2><p><i>Querying real-time stock from Server 3...</i></p>", parse_mode="HTML")

        lzt_page = (vpage // 4) + 1

        f = get_user_filters(cq.from_user.id)
        search_params = {
            "country[]": country_code,
            "order_by": "price_to_up",
            "currency": "usd",
            "page": lzt_page
        }
        
        if f["origin"] != "any": search_params["origin[]"] = f["origin"]
        if f["2fa"] != "any": search_params["has_2fa"] = int(f["2fa"])
        if f["premium"] == "no": search_params["not_telegram_premium"] = 1
        elif f["premium"] == "yes": search_params["telegram_premium"] = 1
        if f["spam"] != "any": search_params["spam"] = f["spam"]
        if f["email"] == "yes": search_params["telegram_email"] = 1
        elif f["email"] == "no": search_params["telegram_email"] = 0

        status, data = await lzt_request(
            "GET", LZT_SEARCH_URL,
            params=search_params,
            action_label=f"LZT: Search ({country_code} Page {lzt_page})"
        )

        if status != 200 or "items" not in data:
            errs = data.get("errors", ["Unknown error"]) if isinstance(data, dict) else ["Unknown error"]
            await log_admin(f"⚠️ <b>LZT Search Failed</b> (status {status}): {'; '.join(errs)}", data)
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back", callback_data="s3tg_open")]])
            return await cq.message.edit_text(
                f"<h2>❌ Could Not Fetch Listings</h2><blockquote><code>{escape('; '.join(errs))}</code></blockquote>",
                parse_mode="HTML",
                reply_markup=kb,
            )

        all_items = data["items"]
        start_index = (vpage % 4) * LZT_PER_PAGE
        page_items = all_items[start_index : start_index + LZT_PER_PAGE]

        if not page_items and vpage == 0:
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back", callback_data="s3tg_open")]])
            return await cq.message.edit_text(
                "<h2>📭 Out of Stock</h2><p>No accounts match your current filters in this region right now.</p>",
                parse_mode="HTML",
                reply_markup=kb,
            )

        country_meta = next((c for c in LZT_ALL_COUNTRIES if c['code'] == country_code), {"flag": "🌍", "name": country_code})

        kb = InlineKeyboardBuilder()
        for item in page_items:
            usd_price = item["price"]
            sell_price = usd_to_sell_inr(usd_price, cq.from_user.id)
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

        disc = get_user_discount(cq.from_user.id)
        disc_line = f"\n🏷️ <b>VIP Discount Active:</b> <code>{disc:g}% OFF</code>" if disc > 0 else ""
        text = (
            f"<h2>{tg_icon()} TG Accounts — {country_meta['flag']} {country_meta['name']}</h2>"
            f"<blockquote>🛒 Showing <b>{len(page_items)}</b> live accounts on this page. Tap an account below to preview & buy:{disc_line}</blockquote>"
        )
        await cq.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())


    # ==============================
    # ✨ VIP ACCOUNT PREVIEW MENU ✨
    # ==============================
    @dp.callback_query(F.data.startswith("s3tg_view:"))
    async def s3tg_view(cq: CallbackQuery):
        if await is_locked_for_user(cq): return  
    
        parts = cq.data.split(":")
        item_id = parts[1]
        usd_price = float(parts[2])
        country_code = parts[3]
        vpage = parts[4]
    
        sell_price = usd_to_sell_inr(usd_price, cq.from_user.id)
        disc = get_user_discount(cq.from_user.id)
    
        user = users_col.find_one({"_id": cq.from_user.id}) or {}
        balance = user.get("balance", 0.0)
    
        status_msg = await cq.message.edit_text("<h2>⏳ Loading Account Preview...</h2><p><i>Fetching live account details from Server 3...</i></p>", parse_mode="HTML")
    
        status, data = await lzt_request("GET", f"{LZT_BASE}/{item_id}", action_label=f"LZT: Item Detail ({item_id})")
    
        if status != 200 or "item" not in data:
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back to List", callback_data=f"s3tg_country:{country_code}:{vpage}")]])
            return await status_msg.edit_text(
                "<h2>❌ Account Unavailable</h2><p><i>Someone else just purchased this account or the listing was removed.</i></p>",
                parse_mode="HTML",
                reply_markup=kb,
            )
        
        item = data["item"]
    
        country_meta = next((c for c in LZT_ALL_COUNTRIES if c['code'] == country_code), {"flag": "🌍", "name": country_code})
    
        origin_raw = item.get("item_origin", "Unknown")
        origin_map = {"autoreg": "Autoreg 🤖", "phishing": "Stealer/Phishing 🎣", "resale": "Resale 🔄", "brute": "Brute Force 💥"}
        origin = origin_map.get(origin_raw, origin_raw.capitalize())
    
        password_set = "Yes 🔐" if item.get("telegram_password") == 1 else "No 🔓"
        premium = "Yes 🌟" if item.get("telegram_premium") == 1 else "No ❌"
        email_linked = "Yes 📧" if item.get("telegram_email") == 1 else "No ❌"
    
        contacts = item.get("telegram_contacts_count", 0)
        chats = item.get("telegram_chats_count", 0)
    
        spam_block = item.get("telegram_spam_block")
        spam_status = "✅ <code>Clean</code>" if spam_block == -1 else "🚫 <code>Limited</code>"
    
        last_seen = format_time_ago(item.get("telegram_last_seen"))
        login_type = str(item.get("login_type", "Unknown")).capitalize()
        dc_id = item.get("telegram_dc_id", "Unknown")
        disc_line = f"\n🏷️ <b>VIP Discount:</b> <code>{disc:g}% OFF</code>" if disc > 0 else ""
    
        text = (
            f"<h2>✨ {tg_icon()} Account Preview</h2>"
            f"<blockquote>"
            f"🌍 <b>{country_meta['flag']} {country_meta['name']}</b> (<code>{country_code}</code>)\n"
            f"💰 <b>Price:</b> <code>₹{sell_price:.0f}</code>  •  💎 <b>Your Balance:</b> <code>₹{balance:.2f}</code>{disc_line}"
            f"</blockquote>"
            f"<p><b>📋 Account Specifications:</b></p>"
            f"<blockquote>"
            f"• <b>Origin:</b> <i>{origin}</i>\n"
            f"• <b>Login Type:</b> <code>{login_type}</code>  •  <b>Data Center:</b> <code>DC {dc_id}</code>\n"
            f"• <b>Premium:</b> {premium}  •  <b>2FA:</b> {password_set}\n"
            f"• <b>Attached Email:</b> {email_linked}\n"
            f"• <b>Contacts:</b> <code>{contacts}</code>  •  <b>Chats:</b> <code>{chats}</code>\n"
            f"• <b>Spam Status:</b> {spam_status}\n"
            f"• <b>Last Active:</b> <i>{last_seen}</i>"
            f"</blockquote>"
            f"<p>⚠️ <i>Automated market account — health is verified before purchase. All sales are final.</i></p>"
        )
    
        kb = InlineKeyboardBuilder()
        kb.row(InlineKeyboardButton(text="✅ Accept & Buy", callback_data=f"s3tg_buy:{item_id}:{usd_price}:{country_code}"))
        kb.row(InlineKeyboardButton(text="🔙 Go Back", callback_data=f"s3tg_country:{country_code}:{vpage}"))
    
        await status_msg.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())

    async def find_exact_replacement(country_code, exact_usd_price, user_id, exclude_ids=None):
        exclude_ids = exclude_ids or set()
        f = get_user_filters(user_id)

        params = {
            "country[]": country_code,
            "pmin": exact_usd_price,
            "pmax": exact_usd_price,
            "order_by": "price_to_up",
            "currency": "usd",
            "page": 1
        }

        if f["origin"] != "any": params["origin[]"] = f["origin"]
        if f["2fa"] != "any": params["has_2fa"] = int(f["2fa"])
        if f["premium"] == "no": params["not_telegram_premium"] = 1
        elif f["premium"] == "yes": params["telegram_premium"] = 1
        if f["spam"] != "any": params["spam"] = f["spam"]
        if f["email"] == "yes": params["telegram_email"] = 1
        elif f["email"] == "no": params["telegram_email"] = 0

        status, data = await lzt_request("GET", LZT_SEARCH_URL, params=params, action_label="LZT: Failover Search")
        if status == 200 and data.get("items"):
            for it in data["items"]:
                if str(it["item_id"]) not in exclude_ids:
                    return it["item_id"]
        return None

    @dp.callback_query(F.data.startswith("s3tg_buy_again:"))
    async def s3tg_buy_again(cq: CallbackQuery):
        _, country_code, usd_price = cq.data.split(":")
        usd_price = float(usd_price)
        
        await cq.answer("🔄 Searching stock...", show_alert=False)
        
        status_msg = await cq.message.edit_text("<h2>⏳ Searching Stock...</h2><p><i>Looking for another account at the exact same price...</i></p>", parse_mode="HTML")
        new_item_id = await find_exact_replacement(country_code, usd_price, cq.from_user.id)
        
        if not new_item_id:
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back to Menu", callback_data="s3tg_open")]])
            return await status_msg.edit_text("<h2>❌ Out of Stock</h2><p>No accounts left at this exact price for this country.</p>", parse_mode="HTML", reply_markup=kb)
            
        new_cq = cq.model_copy(update={"data": f"s3tg_buy:{new_item_id}:{usd_price}:{country_code}"})
        await s3tg_buy(new_cq, external_status_msg=status_msg)

    @dp.callback_query(F.data.startswith("s3tg_buy:"))
    async def s3tg_buy(cq: CallbackQuery, external_status_msg=None):
        parts = cq.data.split(":")
        item_id = parts[1]
        usd_price = float(parts[2])
        country_code = parts[3]
        
        user_id = cq.from_user.id
        sell_price = usd_to_sell_inr(usd_price, user_id)
        
        first_name = cq.from_user.first_name or "User"
        username_str = f"@{cq.from_user.username}" if cq.from_user.username else f"<a href='tg://user?id={user_id}'>{first_name}</a>"

        user = users_col.find_one({"_id": user_id}) or {"_id": user_id, "balance": 0.0}
        balance = user.get("balance", 0.0)
        
        if balance < sell_price:
            kb = InlineKeyboardBuilder()
            kb.button(text="💳 Recharge", callback_data="recharge")
            kb.button(text="🔙 Back", callback_data="s3tg_open")
            kb.adjust(1)
            msg_text = (
                f"<h2>❌ Insufficient Balance</h2>"
                f"<blockquote>"
                f"🏷️ <b>Required:</b> <code>₹{sell_price:.2f}</code>\n"
                f"💳 <b>Available:</b> <code>₹{balance:.2f}</code>"
                f"</blockquote>"
            )
            if external_status_msg:
                return await external_status_msg.edit_text(msg_text, parse_mode="HTML", reply_markup=kb.as_markup())
            return await cq.message.edit_text(msg_text, parse_mode="HTML", reply_markup=kb.as_markup())

        status_msg = external_status_msg or await cq.message.edit_text("<h2>⏳ Verifying Account...</h2><p><i>Checking account health & processing purchase...</i></p>", parse_mode="HTML")

        target_item_id = item_id
        tried_ids = set()
        purchase_successful = False
        item_data = {}
        error_text = ""

        MAX_ATTEMPTS = 5
        for attempt in range(1, MAX_ATTEMPTS + 1):
            tried_ids.add(str(target_item_id))

            try:
                await status_msg.edit_text(
                    f"<h2>⏳ Verifying Account Health</h2><p><i>Checking account... (attempt {attempt}/{MAX_ATTEMPTS})</i></p>",
                    parse_mode="HTML"
                )
            except Exception:
                pass

            check_url = f"{LZT_BASE}/{target_item_id}/check-account"
            c_status, c_data = await lzt_request("POST", check_url, json_body={}, action_label=f"LZT: Check Account ({target_item_id})")

            if c_status != 200:
                if attempt == MAX_ATTEMPTS:
                    error_text = "Account failed health check and no working replacement found at this exact price."
                    break
                try:
                    await status_msg.edit_text(
                        f"<h2>⚠️ Finding Replacement...</h2><p><i>That account was unhealthy, searching another at the same price... ({attempt}/{MAX_ATTEMPTS})</i></p>",
                        parse_mode="HTML"
                    )
                except Exception:
                    pass
                replacement = await find_exact_replacement(country_code, usd_price, user_id, exclude_ids=tried_ids)
                if not replacement:
                    error_text = "No other accounts available at this exact price."
                    break
                target_item_id = replacement
                continue

            buy_url = f"{LZT_BASE}/{target_item_id}/fast-buy"
            body = {
                "price": usd_price,
                "buy_without_validation": 1
            }
            
            b_status, b_data = await lzt_request("POST", buy_url, json_body=body, action_label=f"LZT: Fast Buy ({target_item_id})")

            err_str = str(b_data).lower()
            if b_status in (200, 201) or "already" in err_str or "купили" in err_str:
                purchase_successful = True
                item_data = b_data.get("item", {})
                break
            else:
                if attempt == MAX_ATTEMPTS:
                    errs = b_data.get("errors", ["Unknown"]) if isinstance(b_data, dict) else ["Unknown"]
                    error_text = "; ".join(errs)
                    break
                try:
                    await status_msg.edit_text(
                        f"<h2>⚠️ Retrying Purchase...</h2><p><i>Trying another account at the same price... ({attempt}/{MAX_ATTEMPTS})</i></p>",
                        parse_mode="HTML"
                    )
                except Exception:
                    pass
                replacement = await find_exact_replacement(country_code, usd_price, user_id, exclude_ids=tried_ids)
                if not replacement:
                    errs = b_data.get("errors", ["Unknown"]) if isinstance(b_data, dict) else ["Unknown"]
                    error_text = "; ".join(errs)
                    break
                target_item_id = replacement

        if not purchase_successful:
            await log_admin(f"⚠️ <b>LZT Purchase Failed (After {MAX_ATTEMPTS} Failover Attempts)</b>\n\n👤 <b>User:</b> {username_str} (<code>{user_id}</code>)\n🆔 <b>Last Tried Item:</b> {target_item_id}\n❌ <b>Error:</b> {error_text}")
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back", callback_data="s3tg_open")]])
            return await status_msg.edit_text(
                f"<h2>❌ Purchase Failed</h2><p>Could not complete purchase after {MAX_ATTEMPTS} failover attempts at this exact price. Please try another listing.</p>",
                parse_mode="HTML",
                reply_markup=kb,
            )
            
        raw_phone = item_data.get("telegram_phone") or item_data.get("login") or "N/A"
        phone_number = str(raw_phone)
        if phone_number != "N/A" and not phone_number.startswith("+") and not phone_number.isalpha():
            phone_number = f"+{phone_number}"

        new_balance = balance - sell_price
        users_col.update_one({"_id": user_id}, {"$set": {"balance": new_balance}}, upsert=True)

        country_meta = next((c for c in LZT_ALL_COUNTRIES if c['code'] == country_code), {"name": country_code})
        country_display_name = country_meta['name']

        lzt_orders.insert_one({
            "user_id": user_id, "item_id": str(target_item_id), "title": item_data.get("title_en") or item_data.get("title"),
            "price_usd": usd_price, "price_inr": sell_price, "number": phone_number,
            "country_code": country_code, "country_name": country_display_name, "server": 3, "status": "delivered", "created_at": datetime.utcnow(),
            "channel_logged": False
        })

        reset_success = False
        try:
            await status_msg.edit_text("<h2>🛡️ Securing Account...</h2><p><i>Terminating other sessions...</i></p>", parse_mode="HTML")
            reset_url = f"{LZT_BASE}/{target_item_id}/telegram-reset-authorizations"
            r_status, r_data = await lzt_request("POST", reset_url, action_label=f"LZT: Auto Reset Auth ({target_item_id})")
            if r_status == 200:
                reset_success = True
        except Exception:
            pass 

        kb = InlineKeyboardBuilder()
        kb.row(InlineKeyboardButton(text="📋 Copy Number", copy_text=CopyTextButton(text=phone_number)))
        kb.row(InlineKeyboardButton(text="📩 Get Login Code", callback_data=f"s3tg_getcode:{target_item_id}:{country_code}:{usd_price}"))

        security_msg = "<p>🛡️ <i>Account secured! Other sessions were terminated.</i></p>" if reset_success else ""

        success_text = (
            f"<h2>✅ {tg_icon()} Purchased Successfully!</h2>"
            f"<blockquote>"
            f"🌐 <b>Server:</b> Server 3 (Telegram)\n"
            f"🌍 <b>Country:</b> {country_display_name}\n"
            f"📞 <b>Number:</b> <code>{phone_number}</code>\n"
            f"🏷️ <b>Price:</b> <code>₹{sell_price:.2f}</code>\n"
            f"💸 <b>Balance:</b> <code>₹{new_balance:.2f}</code>"
            f"</blockquote>"
            f"{security_msg}"
        )
        await status_msg.edit_text(success_text, parse_mode="HTML", reply_markup=kb.as_markup())

        await log_admin(
            f"<h2>🆕 LZT Account Sold (Server 3 - Telegram)</h2>"
            f"<blockquote>"
            f"👤 <b>User:</b> {username_str} (<code>{user_id}</code>)\n"
            f"🆔 <b>Item ID:</b> <code>{target_item_id}</code>\n"
            f"🌍 <b>Country:</b> {country_display_name} ({country_code})\n"
            f"💰 <b>Cost (LZT):</b> <code>${usd_price:.2f}</code>\n"
            f"💵 <b>Sold For:</b> <code>₹{sell_price:.2f}</code>\n"
            f"🏦 <b>Remaining Balance:</b> <code>₹{new_balance:.2f}</code>\n"
            f"📞 <b>Number:</b> <code>{phone_number}</code>"
            f"</blockquote>"
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
            return await bot.send_message(
                cq.from_user.id,
                f"<h2>⏳ Login Code Not Ready</h2><blockquote><code>{escape('; '.join(errs))}</code></blockquote><p><i>Request the code in Telegram first, then tap <b>Get Login Code</b> again in a few seconds.</i></p>",
                parse_mode="HTML",
            )

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
            return await bot.send_message(
                cq.from_user.id,
                "<h2>⚠️ Code Parsing Issue</h2><p>Code was received but couldn't be read automatically. Please contact support.</p>",
                parse_mode="HTML",
            )

        lzt_orders.update_one({"item_id": str(item_id)}, {"$set": {"status": "completed", "otp": code}})
        
        order_info = lzt_orders.find_one({"item_id": str(item_id)})
        phone_number = order_info.get("number") if order_info else None
        already_logged = order_info.get("channel_logged", False) if order_info else False
        
        if not phone_number or phone_number == "Unknown":
            phone_number = "Unknown"
            masked_number = "Hidden"
        else:
            phone_number = str(phone_number)
            if len(phone_number) > 6:
                masked_number = phone_number[:-4] + "****" + phone_number[-2:]
            else:
                masked_number = phone_number

        country_meta = next((c for c in LZT_ALL_COUNTRIES if c['code'] == country_code), {"name": country_code})
        country_display_name = country_meta['name']

        password_text = "None"
        try:
            if isinstance(data.get("item"), dict) and data["item"].get("telegram_password"):
                password_text = str(data["item"]["telegram_password"])
        except:
            pass

        if not already_logged:
            channel_message = (
                f"<h2>✅ New Number Purchase Successful</h2>"
                f"<blockquote>"
                f"🌍 <b>Country:</b> {country_display_name}\n"
                f"{tg_icon()} <b>Application:</b> Telegram\n"
                f"📞 <b>Number:</b> <code>{masked_number}</code>\n"
                f"🔑 <b>OTP:</b> <span class='tg-spoiler'>{code}</span>\n"
                f"🌐 <b>Server:</b> (3)\n"
                f"🔐 <b>Password:</b> <span class='tg-spoiler'>{password_text}</span>"
                f"</blockquote>"
                f"<p><b>• @{BOTUSER} || @{CHANNEL}</b></p>"
            )
            
            buy_button = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="• Buy Now •", url=f"https://t.me/{BOTUSER}?start=s3tg")]]
            )
            
            try: 
                await bot.send_message(SALES, channel_message, parse_mode="HTML", reply_markup=buy_button)
                lzt_orders.update_one({"item_id": str(item_id)}, {"$set": {"channel_logged": True}})
            except: 
                pass

        success_text = (
            f"<h2>✅ {tg_icon()} Order Completed</h2>"
            f"<blockquote>"
            f"📞 <b>Number:</b> <code>{phone_number}</code>\n"
            f"🔑 <b>OTP Code:</b> <code>{code}</code>\n"
            f"🔐 <b>2FA Password:</b> <code>{password_text}</code>"
            f"</blockquote>"
        )

        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="📋 Copy OTP", copy_text=CopyTextButton(text=code)),
                    InlineKeyboardButton(text="🔐 Copy Pass", copy_text=CopyTextButton(text=password_text))
                ],
                [
                    InlineKeyboardButton(text="🔄 Get Code Again", callback_data=f"s3tg_getcode:{item_id}:{country_code}:{usd_price}")
                ],
                [
                    InlineKeyboardButton(text="🚫 Remove Bot Devices", callback_data=f"s3tg_reset:{item_id}")
                ],
                [
                    InlineKeyboardButton(text=f"🛒 Buy Again ({country_display_name})", callback_data=f"s3tg_buy_again:{country_code}:{usd_price}")
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
            success_text = "<h2>✅ Sessions Terminated</h2><blockquote>Bot session has been logged out and other devices were kicked.</blockquote>"
            await cq.message.answer(success_text, parse_mode="HTML")
        else:
            err_msg = "Unknown Error"
            if isinstance(data, dict) and "errors" in data and len(data["errors"]) > 0:
                err_msg = data["errors"][0]
                
            if "Too new authorization" in err_msg:
                error_text = "<h2>⚠️ Telegram Security Limit</h2><blockquote>Please wait at least 24 hours after login before terminating other sessions.</blockquote>"
            else:
                error_text = f"<h2>❌ Failed to Terminate Sessions</h2><blockquote><code>{escape(str(err_msg))}</code></blockquote>"
                
            await cq.message.answer(error_text, parse_mode="HTML")

    @dp.callback_query(F.data == "s3tg_my_orders")
    async def s3tg_my_orders(cq: CallbackQuery):
        orders = list(lzt_orders.find({"user_id": cq.from_user.id, "server": 3}).sort("created_at", -1).limit(5))
        
        if not orders:
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back", callback_data="s3tg_open")]])
            return await cq.message.edit_text(
                "<h2>📭 No Recent Orders</h2><p>You don't have any recent Server 3 Telegram orders yet.</p>",
                parse_mode="HTML",
                reply_markup=kb,
            )
            
        kb = InlineKeyboardBuilder()
        lines = []
        
        for idx, o in enumerate(orders, 1):
            number = o.get("number", "Unknown")
            country = o.get("country_name", "Unknown")
            item_id = o.get("item_id")
            usd_price = o.get("price_usd", 0)
            
            lines.append(f"<b>{idx}. {country}</b> ➔ <code>{number}</code>")
            kb.row(InlineKeyboardButton(text=f"📩 Get OTP ({number})", callback_data=f"s3tg_getcode:{item_id}:{o.get('country_code')}:{usd_price}"))
            
        kb.row(InlineKeyboardButton(text="🔙 Back", callback_data="s3tg_open"))
        text = (
            f"<h2>📋 Your Recent Orders (Server 3)</h2>"
            f"<blockquote>" + "\n".join(lines) + "</blockquote>"
            f"<p><i>Tap an order below to request a new login OTP if the session is still active:</i></p>"
        )
        await cq.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())

    async def _render_lzt_balance(target, edit: bool = False):
        if edit:
            checking = await target.edit_text("<h2>🔄 Fetching LZT Profile...</h2><p><i>Querying live LZT Market balance & Server 3 sales...</i></p>", parse_mode="HTML")
        else:
            checking = await target.answer("<h2>🔄 Fetching LZT Profile...</h2><p><i>Querying live LZT Market balance & Server 3 sales...</i></p>", parse_mode="HTML")

        status, data = await lzt_request("GET", LZT_ME_URL, action_label="LZT: Get Balance")
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Server 3 Manager", callback_data="s3m_home")],
            [InlineKeyboardButton(text="⬅️ Admin Panel", callback_data="admin_refresh")],
            [InlineKeyboardButton(text="❌ Close", callback_data="delete_msg")],
        ])

        if status != 200 or "user" not in data:
            errs = data.get("errors", ["Unknown API Error"]) if isinstance(data, dict) else ["Unknown API Error"]
            return await checking.edit_text(
                f"<h2>❌ Failed to Fetch LZT Profile</h2><blockquote><code>{escape('; '.join(errs))}</code></blockquote>",
                parse_mode="HTML",
                reply_markup=kb,
            )

        user_data = data["user"]
        username = user_data.get("username", "Unknown")
        user_id = user_data.get("user_id", "N/A")
        balance_usd = user_data.get("convertedBalance", 0.00)
        balance_rub = user_data.get("balance", "0.00")
        hold_rub = user_data.get("hold", "0.00")

        s3_sales = list(lzt_orders.find({"server": 3, "status": {"$in": ["delivered", "completed", "purchased"]}}))
        total_s3_sold = len(s3_sales)
        total_inr_earned = sum(o.get("price_inr", 0) for o in s3_sales)
        total_usd_spent = sum(o.get("price_usd", 0) for o in s3_sales)

        text = (
            f"<h2>📊 LZT Market Admin Dashboard</h2>"
            f"<blockquote>"
            f"👤 <b>Account:</b> <code>{username}</code> (ID: <code>{user_id}</code>)\n"
            f"💵 <b>Available USD:</b> <code>${balance_usd}</code>\n"
            f"🇷🇺 <b>Available RUB:</b> <code>₽{balance_rub}</code>\n"
            f"🔒 <b>Funds on Hold:</b> <code>₽{hold_rub}</code>"
            f"</blockquote>"
            f"<p><b>🤖 Server 3 Bot Sales Summary:</b></p>"
            f"<blockquote>"
            f"🛍️ <b>Total Accounts Sold:</b> <code>{total_s3_sold}</code>\n"
            f"📈 <b>Total INR Earned:</b> <code>₹{total_inr_earned:.2f}</code>\n"
            f"📉 <b>Total LZT Spent:</b> <code>${total_usd_spent:.2f}</code>"
            f"</blockquote>"
        )
        await checking.edit_text(text, parse_mode="HTML", reply_markup=kb)

    @dp.message(Command("lztbalance"))
    async def cmd_lzt_balance(msg: Message):
        if not is_admin(msg.from_user.id): 
            return await msg.answer("<h2>❌ Unauthorized</h2>", parse_mode="HTML")
        await _render_lzt_balance(msg, edit=False)

    @dp.callback_query(F.data == "lzt_balance_btn")
    async def cb_lzt_balance(cq: CallbackQuery):
        if not is_admin(cq.from_user.id):
            return await cq.answer("Unauthorized", show_alert=True)
        await cq.answer()
        await _render_lzt_balance(cq.message, edit=True)

    return send_lzt_countries_message

