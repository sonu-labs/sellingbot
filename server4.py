import loyalty   # VIP / loyalty hooks
import asyncio
import functools
import math
import os
import time
import traceback
from datetime import datetime
from html import escape
import re

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, CopyTextButton,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

try:
    from config import (
        ADMIN_LOG_ID as _ADMIN_LOG_ID,
        BOT_USERNAME,
        SALES_LOG_ID as _SALES_LOG_ID,
        UPDATES_CHANNEL,
        USD_TO_INR,
    )
    SALES_CHANNEL = _SALES_LOG_ID
    ADMIN_LOG_CHANNEL = _ADMIN_LOG_ID
except Exception:
    SALES_CHANNEL = os.getenv("SALES_LOG_ID", "-1002254383562")
    ADMIN_LOG_CHANNEL = os.getenv("ADMIN_LOG_ID", "-1003441797589")
    BOT_USERNAME = os.getenv("BOT_USERNAME", "TGAStoreAPIBot").lstrip("@")
    UPDATES_CHANNEL = os.getenv("UPDATES_CHANNEL", "TGAForu").lstrip("@")
    USD_TO_INR = float(os.getenv("USD_TO_INR", "100"))
import provider
import i18n as _i18n
from rich_messages import (
    EMOJI_IDS as _RM_EMOJI_IDS,
    RESTRICTED_FLAG_EMOJI_IDS,
    RESTRICTED_FLAG_UNICODE_IDS,
    TELEGRAM_EMOJI_ID,
    WHATSAPP_EMOJI_ID,
)
from tempora import (
    TemporaClient, ProviderUnavailable, retail_price, cost_inr, search_services, popular_services,
    sid_of, norm, entry_name, PRIORITY_COUNTRIES, LEGACY_NAMES,
)

# ---------------- tunables ----------------
OTP_TIMEOUT = 300          # seconds until auto-cancel + refund
CANCEL_LOCK = 180          # user may cancel only after this many seconds without OTP
POLL_EVERY = 4             # seconds between provider polls
UI_REFRESH_EVERY = 10      # seconds between countdown edits (avoids Telegram flood limits)
SERVICES_PER_PAGE = 20
COUNTRIES_PER_PAGE = 20
BUY_COOLDOWN = 3
WARM_EVERY = int(os.getenv("S4_WARM_EVERY", "300"))   # seconds between background rate refreshes (0 = off)
WARM_TOP = int(os.getenv("S4_WARM_TOP", "3"))        # how many popular services are kept warm

import logfmt as _lf

_db_api_key_getter = None


def _resolve_purchase_key() -> str:
    if callable(_db_api_key_getter):
        try:
            k = (_db_api_key_getter() or "").strip()
            if k:
                return k
        except Exception:
            pass
    return (os.getenv("TEMPORASMS_API_KEY") or provider.API_KEY or "").strip()


def _resolve_rates_key() -> str:
    rk = (os.getenv("TEMPORASMS_RATES_API_KEY") or getattr(provider, "RATES_API_KEY", None) or "").strip()
    return rk or _resolve_purchase_key()


API_KEY = _resolve_purchase_key()
RATES_KEY = _resolve_rates_key()
# purchase lane = API_KEY, live-rates lane = RATES_KEY (separate pools, separate concurrency limits)
client = TemporaClient(_resolve_purchase_key, provider.BASE_URL, rates_key=_resolve_rates_key)

# ---------------------------------------------------------------------
# Server 4 TemporaSMS rate cache
# Normal browsing uses these snapshots. The final Purchase click below
# performs a forced live check and then refreshes the matching cache row.
# ---------------------------------------------------------------------
RATE_CACHE_TTL = int(os.getenv("S4_RATE_CACHE_TTL", "300"))
OPERATOR_CACHE_TTL = int(os.getenv("S4_OPERATOR_CACHE_TTL", "180"))

# A freshly displayed operator offer can be reused for a very short time
# during the Purchase click, avoiding a duplicate getPricesV3 request.
LIVE_REUSE_TTL = int(os.getenv("S4_LIVE_REUSE_TTL", "15"))
_s4_offer_cache = {}       # (sid, full) -> {t, rows, names}
_s4_operator_cache = {}    # (sid, cid) -> {t, cname, rows}

def _cache_fresh(entry, ttl):
    return bool(entry) and (time.time() - float(entry.get("t", 0))) < ttl

async def cached_service_offers(sid, full=False, force=False):
    key = (sid, bool(full))
    entry = _s4_offer_cache.get(key)
    if not force and _cache_fresh(entry, RATE_CACHE_TTL):
        return entry["rows"], entry["names"]
    rows = await client.service_offers(sid, full=full, force=force)
    names = await client.countries()
    _s4_offer_cache[key] = {"t": time.time(), "rows": rows, "names": names}
    return rows, names

_co_refreshing = set()

def _rows_from_offer_cache(sid, cid):
    """Operator rows for one country taken from the country-list scan we already did (no provider call)."""
    for (s_, _full), e in list(_s4_offer_cache.items()):
        if s_ != sid or not _cache_fresh(e, RATE_CACHE_TTL):
            continue
        rows = [dict(r) for r in e["rows"] if str(r.get("country_id")) == str(cid)]
        if rows:
            return rows, e["names"]
    return None

def co_ready(sid, cid):
    """True when opening this country needs NO provider call (instant)."""
    return (_cache_fresh(_s4_operator_cache.get((sid, cid)), OPERATOR_CACHE_TTL)
            or _rows_from_offer_cache(sid, cid) is not None
            or client.has_cached(f"co:{sid}:{cid}", max_age=300))

async def _refresh_country_bg(sid, cid):
    """refresh live operator rates in the background so the NEXT click is fresh and instant."""
    k = (sid, cid)
    if k in _co_refreshing:
        return
    _co_refreshing.add(k)
    try:
        rows = await client.country_offers(sid, cid)
        names = await client.countries()
        if rows:
            _s4_operator_cache[k] = {"t": time.time(), "rows": rows, "cname": names.get(cid, f"Country {cid}")}
    except Exception:
        pass
    finally:
        _co_refreshing.discard(k)

async def cached_country_offers(sid, cid, force=False):
    key = (sid, cid)
    entry = _s4_operator_cache.get(key)
    if not force and _cache_fresh(entry, OPERATOR_CACHE_TTL):
        return entry["rows"], entry["cname"]
    if not force:
        # FAST PATH: the country list was built from the same provider data -> reuse it instantly
        # (no 1 call per operator), then refresh live rates in the background.
        hit = _rows_from_offer_cache(sid, cid)
        if hit:
            rows, names = hit
            cname = names.get(cid, f"Country {cid}")
            _s4_operator_cache[key] = {"t": time.time() - max(0, OPERATOR_CACHE_TTL - 20), "rows": rows, "cname": cname}
            asyncio.ensure_future(_refresh_country_bg(sid, cid))
            return rows, cname
    rows = await client.country_offers(sid, cid)
    names = await client.countries()
    cname = names.get(cid, f"Country {cid}")
    _s4_operator_cache[key] = {"t": time.time(), "rows": rows, "cname": cname}
    return rows, cname

def clear_s4_rate_cache(sid=None, cid=None):
    if sid is None:
        _s4_offer_cache.clear()
        _s4_operator_cache.clear()
        return
    for key in list(_s4_offer_cache):
        if key[0] == sid:
            _s4_offer_cache.pop(key, None)
    if cid is None:
        for key in list(_s4_operator_cache):
            if key[0] == sid:
                _s4_operator_cache.pop(key, None)
    else:
        _s4_operator_cache.pop((sid, cid), None)

def sync_s4_live_offer(sid, cid, op, live):
    """Push the final live purchase check into all matching local snapshots."""
    key = (sid, cid)
    entry = _s4_operator_cache.get(key)
    if entry:
        rows = [dict(r) for r in entry["rows"] if str(r.get("operator")) != str(op)]
        if live:
            rows.append({
                "country_id": cid,
                "operator": op,
                "cost": live["cost"],
                "count": live.get("count", 0),
                "code": live.get("code") or live.get("service_code"),
            })
        entry["rows"] = rows
        entry["t"] = time.time()

    for key, entry in list(_s4_offer_cache.items()):
        if key[0] != sid:
            continue
        rows = [dict(r) for r in entry["rows"]
                if not (str(r.get("country_id")) == str(cid) and str(r.get("operator")) == str(op))]
        if live:
            rows.append({
                "country_id": cid,
                "operator": op,
                "cost": live["cost"],
                "count": live.get("count", 0),
                "code": live.get("code") or live.get("service_code"),
            })
        entry["rows"] = rows
        entry["t"] = time.time()


DEFAULT_CFG = {
    "markup_pct": 100.0,
    "tg_markup_pct": 100.0,
    "wa_markup_pct": 100.0,
    "other_markup_pct": 100.0,
    "min_profit": 10.0,
    "unit_to_inr": USD_TO_INR,
    "legacy_prices": False,
    "category_pricing": True,
}
LIMITS = {
    "markup_pct": (0, 1000),
    "tg_markup_pct": (0, 1000),
    "wa_markup_pct": (0, 1000),
    "other_markup_pct": (0, 1000),
    "min_profit": (0, 10000),
    "unit_to_inr": (0.0001, 10000),
}

# Old hand-set prices keep priority: (alias, country_lower, operator) -> bot price
LEGACY_ALIAS = {sid_of(n): a for a, n in LEGACY_NAMES.items()}
LEGACY_PRICE = {(srv, c.lower(), op): bot_p for (srv, c, op, _mp, bot_p) in provider.MANUAL_CONFIG}

# =====================================================================
# Premium emoji configuration
# UI icons come from rich_messages.EMOJI_IDS and all country/subdivision
# flags come strictly from EMOJI_IDS/RestrictedEmoji.txt.
# =====================================================================
CUSTOM_EMOJI_IDS = dict(_RM_EMOJI_IDS)
CUSTOM_FLAG_EMOJI_IDS = dict(RESTRICTED_FLAG_EMOJI_IDS)
CUSTOM_FLAG_UNICODE_EMOJI_IDS = dict(RESTRICTED_FLAG_UNICODE_IDS)
CUSTOM_FLAG_NAME_ALIASES = {
    "england": "🏴\U000e0067\U000e0062\U000e0065\U000e006e\U000e0067\U000e007f",
    "scotland": "🏴\U000e0067\U000e0062\U000e0073\U000e0063\U000e0074\U000e007f",
    "wales": "🏴\U000e0067\U000e0062\U000e0077\U000e006c\U000e0073\U000e007f",
}

APP_CUSTOM_EMOJI_IDS = {
    "telegram": TELEGRAM_EMOJI_ID,
    "whatsapp": WHATSAPP_EMOJI_ID,
    "google / gmail": "5796209712009581332",
    "instagram": "5348533850429470257",
    "chatgpt / openai": "5796185041717433060",
    "twitter / x": "4997271698177787448",
    "facebook": "5393310276059678201",
    "tiktok": "5397672154651181662",
}
OTHER_APP_CUSTOM_EMOJI_ID = "5409119256107297715"


def app_custom_emoji_id(label: str) -> str:
    """Return the fixed premium icon for a Server 4 app."""
    key = norm(label)
    if key in APP_CUSTOM_EMOJI_IDS:
        return APP_CUSTOM_EMOJI_IDS[key]
    aliases = {
        "google gmail": "google / gmail",
        "gmail": "google / gmail",
        "google": "google / gmail",
        "chatgpt openai": "chatgpt / openai",
        "chatgpt": "chatgpt / openai",
        "openai": "chatgpt / openai",
        "twitter x": "twitter / x",
        "twitter": "twitter / x",
        "x": "twitter / x",
        "wa": "whatsapp",
    }
    return APP_CUSTOM_EMOJI_IDS.get(aliases.get(key, key), OTHER_APP_CUSTOM_EMOJI_ID)


_flag_cache = {}
_flag_alpha2_cache = {}


def _premium_tag(emoji: str, emoji_id: str) -> str:
    return f'<tg-emoji emoji-id="{emoji_id}">{emoji}</tg-emoji>' if emoji_id else emoji


def premiumize_text(text: str) -> str:
    if not text or not CUSTOM_EMOJI_IDS:
        return text
    protected = []

    def protect(m):
        protected.append(m.group(0))
        return f"\x00S4PE{len(protected)-1}\x00"

    out = re.sub(
        r"<pre\b[^>]*>.*?</pre>|<code\b[^>]*>.*?</code>|<tg-emoji\b[^>]*>.*?</tg-emoji>",
        protect,
        str(text),
        flags=re.I | re.S,
    )
    for emoji in sorted(CUSTOM_EMOJI_IDS, key=len, reverse=True):
        eid = str(CUSTOM_EMOJI_IDS.get(emoji) or "").strip()
        if eid and emoji in out:
            out = out.replace(emoji, _premium_tag(emoji, eid))
    for i, value in enumerate(protected):
        out = out.replace(f"\x00S4PE{i}\x00", value)
    return out


def _flag_unicode_to_id():
    out = dict(CUSTOM_FLAG_UNICODE_EMOJI_IDS)
    for a2, eid in CUSTOM_FLAG_EMOJI_IDS.items():
        eid = str(eid or "").strip()
        if len(a2) == 2 and eid:
            try:
                out["".join(chr(0x1F1E6 + ord(c.upper()) - ord("A")) for c in a2)] = eid
            except Exception:
                pass
    return out


def _button_parts(text: str):
    text = str(text or "")
    m = re.match(r'^<tg-emoji\s+emoji-id="([^"]+)"[^>]*>(.*?)</tg-emoji>\s*', text, re.S)
    if m:
        return text[m.end():], m.group(1)
    for emoji in sorted(CUSTOM_EMOJI_IDS, key=len, reverse=True):
        eid = str(CUSTOM_EMOJI_IDS.get(emoji) or "").strip()
        if eid and text.startswith(emoji):
            return text[len(emoji):].lstrip(), eid
    for flag, eid in _flag_unicode_to_id().items():
        if text.startswith(flag):
            return text[len(flag):].lstrip(), eid
    return text, None


class Server4UserState(StatesGroup):
    waiting_service_search = State()
    waiting_country_search = State()
    waiting_admin_pct = State()


last_click_time = {}


def is_spamming(user_id: int, cooldown: int = BUY_COOLDOWN) -> bool:
    now = time.time()
    if len(last_click_time) > 5000:  # never grow forever
        for k in [k for k, t in last_click_time.items() if now - t > 60]:
            last_click_time.pop(k, None)
    if now - last_click_time.get(user_id, 0) < cooldown:
        return True
    last_click_time[user_id] = now
    return False


WHATSAPP_SECURITY_TIPS = (
    "<h2>🔐 WhatsApp Account Safety</h2>"
    "<blockquote expandable>"
    "• Let the new account rest before heavy messaging or group creation.<br>"
    "• Enable Two-Step Verification and link a recovery email after 24 hours.<br>"
    "• Avoid repeated sign-ins across multiple devices."
    "</blockquote>"
)

TELEGRAM_SERVICE_NOTE = (
    "<blockquote>✈️ <b>Fresh Virtual Numbers</b> · Auto-refund if no OTP arrives in 5m.</blockquote>"
)


_BASIC_FLAGS = {"usa": "🇺🇸", "india": "🇮🇳", "united kingdom": "🇬🇧", "indonesia": "🇮🇩", "philippines": "🇵🇭",
                "vietnam": "🇻🇳", "canada": "🇨🇦", "ukraine": "🇺🇦", "kazakhstan": "🇰🇿", "argentina": "🇦🇷",
                "iran": "🇮🇷", "brazil": "🇧🇷"}


def flag_for(name: str) -> str:
    if not _flag_cache:
        _flag_cache.update(_BASIC_FLAGS)
        for _n, _flag in _BASIC_FLAGS.items():
            try:
                _letters = [chr(ord("A") + (ord(c) - 0x1F1E6)) for c in _flag if 0x1F1E6 <= ord(c) <= 0x1F1FF]
                if len(_letters) == 2:
                    _flag_alpha2_cache.setdefault(_n, "".join(_letters))
            except Exception:
                pass
        try:
            # Load comprehensive country flag map from server3.py
            try:
                from .server3 import LZT_COUNTRY_MAP, flag_emoji
            except (ImportError, ValueError):
                from server3 import LZT_COUNTRY_MAP, flag_emoji
            for _code, (a2, n) in LZT_COUNTRY_MAP.items():
                nn = norm(n)
                _flag_cache.setdefault(nn, flag_emoji(a2))
                _flag_alpha2_cache.setdefault(nn, a2.upper())
            _flag_cache["united states"] = _flag_cache.get("usa", "🇺🇸")
            _flag_cache["united states virtual"] = _flag_cache["united states"]
            _flag_alpha2_cache.setdefault("usa", "US")
            _flag_alpha2_cache.setdefault("united states", "US")
            _flag_alpha2_cache.setdefault("united states virtual", "US")
        except Exception as e:
            print(f"[server4] country flag map load failed: {e!r}")
    n = norm(name)
    raw = _flag_cache.get(n) or _flag_cache.get(n.replace(" virtual", "")) or "🌍"
    raw = CUSTOM_FLAG_NAME_ALIASES.get(n, raw)
    unicode_map = _flag_unicode_to_id()
    eid = str(unicode_map.get(raw) or "").strip()
    if not eid:
        a2 = _flag_alpha2_cache.get(n) or _flag_alpha2_cache.get(n.replace(" virtual", ""))
        eid = str(CUSTOM_FLAG_EMOJI_IDS.get(a2 or "") or "").strip()
    return _premium_tag(raw, eid) if eid else raw



def btn(text, cb=None, style=None, **kw):
    text, detected_icon = _button_parts(text)
    text = str(text or "").strip()
    # Telegram rejects buttons whose visible text becomes empty after stripping
    # a premium/custom-emoji prefix. Keep a tiny visible fallback for icon-only labels.
    if not text:
        text = "•"
    d = {"text": text}
    if cb is not None:
        d["callback_data"] = cb
    if style:
        d["style"] = style
    if detected_icon and not kw.get("icon_custom_emoji_id"):
        kw["icon_custom_emoji_id"] = detected_icon
    d.update(kw)
    return InlineKeyboardButton(**d)

def premium_button_parts(text, **kwargs):
    """Public helper used by bot.py so every inline button can use premium icons."""
    clean, icon = _button_parts(text)
    out = dict(kwargs)
    if icon and not out.get("icon_custom_emoji_id"):
        out["icon_custom_emoji_id"] = icon
    return clean, out



async def edit(target, text, kb=None):
    """Edit in place; ignore 'message is not modified'."""
    m = target.message if isinstance(target, CallbackQuery) or (hasattr(target, "message") and not hasattr(target, "edit_text")) else target
    try:
        await m.edit_text(premiumize_text(text), reply_markup=kb, parse_mode="HTML")
    except TelegramBadRequest as e:
        err = str(e).lower()
        if "not modified" in err:
            return
        if isinstance(target, CallbackQuery) and ("no text in the message" in err or "message can't be edited" in err):
            try:
                await m.delete()
            except Exception:
                pass
            await m.answer(premiumize_text(text), reply_markup=kb, parse_mode="HTML")
            return
        raise


def drop_offer_cache(sid):
    client.drop_service(sid)
    clear_s4_rate_cache(sid)


def short(n: float) -> str:
    from utils import fmt_short  # viewer's chosen currency
    return fmt_short(n)


# =====================================================================
def register_server4_handlers(dp: Dispatcher, bot: Bot, db, users_col, orders_col, is_admin_func, fmt_curr_func):
    s4_settings = db["server4_settings"]
    global_settings_col = db["settings"]
    tasks_col = db["server4_tasks"]
    _bg = set()

    _key_cache = {"t": 0.0, "k": ""}

    def _read_db_tempora_key() -> str:
        now = time.time()
        if now - _key_cache["t"] < 5.0:
            return _key_cache["k"]
        try:
            doc = global_settings_col.find_one({"_id": "otp_services"}) or {}
            k = str(doc.get("tempora_api_key") or "").strip()
        except Exception:
            k = ""
        _key_cache["t"] = now
        _key_cache["k"] = k
        return k

    global _db_api_key_getter
    _db_api_key_getter = _read_db_tempora_key

    def spawn(coro):
        """create_task that cannot be garbage-collected mid-flight (paid OTP workers must never vanish)."""
        t = asyncio.create_task(coro)
        _bg.add(t)
        t.add_done_callback(_bg.discard)
        return t

    async def dbq(fn, *a, **k):
        """pymongo is synchronous: run it in a thread so one slow query never freezes every user."""
        return await asyncio.to_thread(fn, *a, **k)

    def scb(*flts):
        """dp.callback_query + logging: any crash is printed with traceback and the user gets a message."""
        def deco(fn):
            @functools.wraps(fn)
            async def wrapper(call, *a, **k):
                try:
                    return await fn(call, *a, **k)
                except ProviderUnavailable:
                    try:
                        await call.answer("⚠️ Provider is busy right now. Nothing was charged — please retry.", show_alert=True)
                    except Exception:
                        pass
                except Exception:
                    print(f"[server4] ERROR in {fn.__name__} (data={getattr(call, 'data', None)})")
                    traceback.print_exc()
                    try:
                        await call.answer("⚠️ Something went wrong. Please try again.", show_alert=True)
                    except Exception:
                        pass
            return dp.callback_query(*flts)(wrapper)
        return deco
    global _fmt
    _fmt = fmt_curr_func

    # ---------- settings: read from Mongo at most once / 5s (was: one DB hit PER PRICE ROW) ----------
    _sc = {"t": 0.0, "cfg": None, "maint": False}

    def _load_settings():
        if _sc["cfg"] is not None and time.time() - _sc["t"] < 5:
            return
        try:
            doc = s4_settings.find_one({"_id": "config"}) or {}
            m = s4_settings.find_one({"_id": "maintenance_config"}) or {}
            srv_cfg = global_settings_col.find_one({"_id": "server_config"}) or {}
            s4_srv_val = srv_cfg.get("s4", "active")
            s4_disabled_in_srv_cfg = (s4_srv_val is False) or (
                isinstance(s4_srv_val, str) and s4_srv_val.lower() in ("maintenance", "off", "disabled", "false")
            )
            _sc.update(t=time.time(), maint=bool(m.get("enabled", False)) or s4_disabled_in_srv_cfg,
                       cfg={**DEFAULT_CFG, **{k: v for k, v in doc.items() if k != "_id"}})
        except Exception as e:
            print(f"[server4] settings read failed: {e!r}")
            if _sc["cfg"] is None:
                _sc.update(t=time.time(), cfg=dict(DEFAULT_CFG))

    def cfg() -> dict:
        _load_settings()
        return _sc["cfg"]

    def is_maintenance_active() -> bool:
        _load_settings()
        return _sc["maint"]

    def bump_settings():
        _sc["cfg"] = None   # next read goes to Mongo -> admin changes apply instantly

    def extra_services():
        doc = s4_settings.find_one({"_id": "custom_services"}) or {}
        return doc.get("items", {})

    client.extra_services = extra_services

    def price_for(sid, country_name, op, cost) -> float:
        """Use a separate profit percentage for Telegram, WhatsApp and all other apps."""
        c = cfg()
        alias = LEGACY_ALIAS.get(sid)
        if alias == "tg":
            pct = float(c.get("tg_markup_pct", c.get("markup_pct", 100.0)))
        elif alias == "wa":
            pct = float(c.get("wa_markup_pct", c.get("markup_pct", 100.0)))
        else:
            pct = float(c.get("other_markup_pct", c.get("markup_pct", 100.0)))
        return retail_price(cost, {**c, "markup_pct": pct})

    # -------------------- data views --------------------
    def aggregate_rows(sid, rows, names):
        agg = {}
        for r in rows:
            cname = names.get(r["country_id"], f"Country {r['country_id']}")
            p = price_for(sid, cname, r["operator"], r["cost"])
            a = agg.setdefault(r["country_id"], {"cid": r["country_id"], "name": cname,
                                                 "flag": flag_for(cname), "min_price": p, "stock": 0})
            a["min_price"] = min(a["min_price"], p)
            a["stock"] += r["count"]

        def order(a):
            n = norm(a["name"]).replace(" virtual", "")
            return (PRIORITY_COUNTRIES.index(n) if n in PRIORITY_COUNTRIES else 999, n)
        return sorted(agg.values(), key=order)

    async def country_rows(sid, full=False):
        """[{cid,name,flag,min_price,stock}] for countries that have stock."""
        rows, names = await cached_service_offers(sid, full=full)
        return aggregate_rows(sid, rows, names)

    async def country_rows_query(sid, query):
        """Country search: ask only for the countries whose NAME matches (a handful of calls,
        instead of scanning all ~200 countries x every operator)."""
        names = await client.countries()
        q = norm(query)
        ids = [cid for cid, n in names.items() if q in norm(n)][:COUNTRIES_PER_PAGE]
        if not ids:
            return []
        res = await asyncio.gather(*[cached_country_offers(sid, cid) for cid in ids], return_exceptions=True)
        rows, errs = [], 0
        for r in res:
            if isinstance(r, ProviderUnavailable):
                errs += 1
            elif isinstance(r, Exception):
                raise r
            else:
                rows += r[0]
        if errs and not rows:
            raise ProviderUnavailable("country search failed")
        return aggregate_rows(sid, rows, names)

    async def operator_rows(sid, cid):
        rows, cname = await cached_country_offers(sid, cid)
        out = [{"op": r["operator"], "price": price_for(sid, cname, r["operator"], r["cost"]),
                "count": r["count"]} for r in rows]
        return cname, sorted(out, key=lambda x: (x["price"], -x["count"]))

    # =================================================================
    #                           ADMIN
    # =================================================================
    def keys_line():
        return (
            "🔑 <b>API Keys:</b> <code>Split</code> ✅"
            if client.separate_keys
            else "🔑 <b>API Keys:</b> <code>Shared</code>"
        )

    def admin_kb():
        active = not is_maintenance_active()
        status = "🟢 ACTIVE" if active else "🔴 DISABLED"
        return InlineKeyboardMarkup(inline_keyboard=[
            [btn("📊 Overview", "s4_adm_home"), btn("💰 Profit %", "s4_adm_pricing")],
            [btn("📈 Sales & Profit", "s4_adm_stats"), btn("💳 Provider Balance", "s4_adm_balance")],
            [btn("🔄 Refresh Catalog", "s4_adm_refresh")],
            [btn(f"{status} — {'Disable' if active else 'Enable'}", "s4_adm_toggle_maint")],
            [btn("🧪 Probe API", "s4_adm_probe")],
            [btn("⬅️ Admin Panel", "admin_refresh")],
        ])

    def admin_text():
        c = cfg()
        status = "🟢 <code>ACTIVE</code>" if not is_maintenance_active() else "🔴 <code>DISABLED</code>"
        return (
            "<h2>⚙️ Server 4 Admin Panel</h2>"
            "<blockquote>"
            f"📡 <b>Status:</b> {status}<br>"
            f"📱 <b>Telegram Markup:</b> <code>{float(c.get('tg_markup_pct', c.get('markup_pct', 100))):g}%</code><br>"
            f"💬 <b>WhatsApp Markup:</b> <code>{float(c.get('wa_markup_pct', c.get('markup_pct', 100))):g}%</code><br>"
            f"🌐 <b>Other Apps Markup:</b> <code>{float(c.get('other_markup_pct', c.get('markup_pct', 100))):g}%</code><br>"
            f"💵 <b>Minimum Profit:</b> <code>₹{float(c.get('min_profit', 10)):g}</code><br>"
            f"{keys_line()}"
            "</blockquote>"
        )

    @dp.message(Command("server4"))
    async def admin_panel(msg: Message):
        if not is_admin_func(msg.from_user.id):
            return await msg.answer(premiumize_text("❌ Not authorized."))
        await msg.answer(premiumize_text(admin_text()), reply_markup=admin_kb(), parse_mode="HTML")

    @scb(F.data == "s4_adm_home")
    async def s4_adm_home(call: CallbackQuery):
        if not is_admin_func(call.from_user.id):
            return await call.answer("Not authorized", show_alert=True)
        await call.answer()
        await edit(call, admin_text(), admin_kb())

    @scb(F.data == "s4_adm_balance")
    async def s4_adm_balance(call: CallbackQuery):
        if not is_admin_func(call.from_user.id):
            return await call.answer("Not authorized", show_alert=True)
        bal = await client.balance()
        await call.answer(f"💳 Provider balance: {bal}" if bal is not None else "⚠️ Could not fetch balance", show_alert=True)

    @scb(F.data == "s4_adm_toggle_maint")
    async def s4_adm_toggle_maint(call: CallbackQuery):
        if not is_admin_func(call.from_user.id):
            return await call.answer("Not authorized", show_alert=True)
        new_disabled = not is_maintenance_active()
        await dbq(s4_settings.update_one, {"_id": "maintenance_config"},
                  {"$set": {"enabled": new_disabled}}, upsert=True)
        await dbq(global_settings_col.update_one, {"_id": "server_config"},
                  {"$set": {"s4": "maintenance" if new_disabled else "active"}}, upsert=True)
        bump_settings()
        await call.answer("Server 4 disabled" if new_disabled else "Server 4 enabled", show_alert=False)
        await edit(call, admin_text(), admin_kb())

    @scb(F.data == "s4_adm_refresh")
    async def s4_adm_refresh(call: CallbackQuery):
        if not is_admin_func(call.from_user.id):
            return await call.answer("Not authorized", show_alert=True)
        await call.answer("Reloading catalog…")
        client.clear_cache()
        cat = await client.services()
        await edit(call, admin_text() + f"\n\n✅ <b>Catalog refreshed:</b> {len(cat):,} services.", admin_kb())

    # ---------------- pricing / profit percentages ----------------
    def pricing_text():
        c = cfg()
        return (
            "<h2>💰 Server 4 Markup Controls</h2>"
            "<blockquote>"
            f"📱 <b>Telegram:</b> <code>{float(c.get('tg_markup_pct', c.get('markup_pct', 100))):g}%</code><br>"
            f"💬 <b>WhatsApp:</b> <code>{float(c.get('wa_markup_pct', c.get('markup_pct', 100))):g}%</code><br>"
            f"🌐 <b>Other Apps:</b> <code>{float(c.get('other_markup_pct', c.get('markup_pct', 100))):g}%</code><br>"
            f"💵 <b>Min Profit Floor:</b> <code>₹{float(c.get('min_profit', 10)):g}</code><br>"
            f"💱 <b>USD → INR:</b> <code>₹{float(c.get('unit_to_inr', 1)):g}</code>"
            "</blockquote>"
        )

    def pricing_kb():
        return InlineKeyboardMarkup(inline_keyboard=[
            [btn("📱 Telegram −5%", "s4pr:m:tg:-5"), btn("Set Telegram %", "s4pr:set:tg"), btn("Telegram +5%", "s4pr:m:tg:5")],
            [btn("💬 WhatsApp −5%", "s4pr:m:wa:-5"), btn("Set WhatsApp %", "s4pr:set:wa"), btn("WhatsApp +5%", "s4pr:m:wa:5")],
            [btn("🌐 Other −5%", "s4pr:m:other:-5"), btn("Set Other %", "s4pr:set:other"), btn("Other +5%", "s4pr:m:other:5")],
            [btn("💵 Min Profit −₹5", "s4pr:p:-5"), btn("Min Profit +₹5", "s4pr:p:5")],
            [btn("⬅️ Back", "s4_adm_home")],
        ])

    @scb(F.data == "s4_adm_pricing")
    async def s4_adm_pricing(call: CallbackQuery):
        if not is_admin_func(call.from_user.id):
            return await call.answer("Not authorized", show_alert=True)
        await call.answer()
        await edit(call, pricing_text(), pricing_kb())

    @scb(F.data.startswith("s4pr:"))
    async def s4_pricing_buttons(call: CallbackQuery, state: FSMContext):
        if not is_admin_func(call.from_user.id):
            return await call.answer("Not authorized", show_alert=True)
        parts = call.data.split(":")
        kind = parts[1]
        if kind == "set":
            category = parts[2]
            await state.set_state(Server4UserState.waiting_admin_pct)
            await state.update_data(admin_pct_category=category)
            label = {"tg": "Telegram", "wa": "WhatsApp", "other": "Other apps"}[category]
            await call.answer()
            return await edit(
                call,
                f"💰 <b>Set {label} profit percentage</b>\n\n"
                "Send only the percentage number, for example <code>75</code> for 75%.\n"
                "Allowed range: <code>0</code>–<code>1000</code>.",
                InlineKeyboardMarkup(inline_keyboard=[[btn("✖️ Cancel", "s4_adm_pricing", style="danger")]])
            )

        c = cfg()
        if kind == "m":
            category, delta = parts[2], float(parts[3])
            field = {"tg": "tg_markup_pct", "wa": "wa_markup_pct", "other": "other_markup_pct"}[category]
            v = min(1000.0, max(0.0, float(c.get(field, c.get("markup_pct", 100.0))) + delta))
        elif kind == "p":
            field = "min_profit"
            v = min(10000.0, max(0.0, float(c.get(field, 10.0)) + float(parts[2])))
        else:
            return await call.answer("Unknown pricing action.", show_alert=True)

        await dbq(s4_settings.update_one, {"_id": "config"}, {"$set": {field: v}}, upsert=True)
        bump_settings()
        await call.answer("✅ Saved & live")
        await edit(call, pricing_text(), pricing_kb())

    @dp.message(StateFilter(Server4UserState.waiting_admin_pct), F.text)
    async def s4_admin_pct_input(msg: Message, state: FSMContext):
        if not is_admin_func(msg.from_user.id):
            return
        data = await state.get_data()
        category = data.get("admin_pct_category")
        field = {"tg": "tg_markup_pct", "wa": "wa_markup_pct", "other": "other_markup_pct"}.get(category)
        if not field:
            await state.clear()
            return await msg.answer(premiumize_text("⚠️ Pricing session expired. Open /server4 again."))
        try:
            value = float((msg.text or "").strip().replace(",", "."))
            if not 0 <= value <= 1000:
                raise ValueError
        except Exception:
            return await msg.answer(premiumize_text("❌ Enter a number from 0 to 1000, for example 75."))
        await dbq(s4_settings.update_one, {"_id": "config"},
                  {"$set": {field: value, "category_pricing": True}}, upsert=True)
        # Keep the old global field synchronized for compatibility with older tooling.
        await dbq(s4_settings.update_one, {"_id": "config"}, {"$set": {"markup_pct": value}}, upsert=True)
        bump_settings()
        await state.clear()
        try:
            await msg.delete()
        except Exception:
            pass
        await msg.answer(premiumize_text(
            f"✅ <b>{ {'tg':'Telegram','wa':'WhatsApp','other':'Other apps'}[category] }</b> profit set to <b>{value:g}%</b>."
        ), reply_markup=pricing_kb(), parse_mode="HTML")

    # Old commands are retained as compatibility shortcuts; /server4 is the main panel.
    def _setter(field):
        async def handler(msg: Message):
            if not is_admin_func(msg.from_user.id):
                return
            cmd = (msg.text or "").split()[0]
            lo, hi = LIMITS[field]
            try:
                v = float(msg.text.split()[1].replace(",", "."))
                assert lo <= v <= hi
            except Exception:
                return await msg.answer(premiumize_text(f"Usage: <code>{cmd} 100</code>\nAllowed range: {lo:g} – {hi:g}"), parse_mode="HTML")
            sets = {field: v}
            if field == "markup_pct":
                sets.update({"tg_markup_pct": v, "wa_markup_pct": v, "other_markup_pct": v})
            await dbq(s4_settings.update_one, {"_id": "config"}, {"$set": sets}, upsert=True)
            bump_settings()
            await msg.answer(premiumize_text(f"✅ Saved {field} = <b>{v:g}</b>%\n\nOpen /server4 for the full panel."), parse_mode="HTML")
        return handler

    dp.message.register(_setter("markup_pct"), Command("s4markup"))
    dp.message.register(_setter("min_profit"), Command("s4minprofit"))
    dp.message.register(_setter("unit_to_inr"), Command("s4unit"))

    @dp.message(Command("s4legacy"))
    async def s4_legacy(msg: Message):
        if not is_admin_func(msg.from_user.id):
            return
        await msg.answer(premiumize_text(
            "ℹ️ Fixed legacy Telegram/WhatsApp prices are no longer used for live Server 4 pricing. "
            "Set Telegram/WhatsApp/Other percentages from /server4."
        ), parse_mode="HTML")

    # ---------------- sales / profit ----------------
    def _stats_sync():
        now = time.time()
        today0 = ((now + 19800) // 86400) * 86400 - 19800

        def agg(since):
            m = {"status": "completed"}
            if since:
                m["timestamp"] = {"$gte": since}
            r = list(tasks_col.aggregate([{"$match": m}, {"$group": {
                "_id": None, "n": {"$sum": 1}, "rev": {"$sum": "$price"},
                "rev_c": {"$sum": {"$cond": [{"$gt": ["$cost_inr", 0]}, "$price", 0]}},
                "cost": {"$sum": {"$ifNull": ["$cost_inr", 0]}}}}]))
            return r[0] if r else {"n": 0, "rev": 0, "rev_c": 0, "cost": 0}

        return agg(today0), agg(now - 7 * 86400), agg(None), tasks_col.count_documents({"status": "active"})

    def _category_stats_sync(since=None):
        rows = []
        try:
            rows = list(tasks_col.find({"status": "completed"}))
        except Exception:
            return {}
        out = {
            "Telegram": {"n": 0, "rev": 0.0, "cost": 0.0},
            "WhatsApp": {"n": 0, "rev": 0.0, "cost": 0.0},
            "Other": {"n": 0, "rev": 0.0, "cost": 0.0},
        }
        for row in rows:
            if since and float(row.get("timestamp", 0) or 0) < since:
                continue
            alias = LEGACY_ALIAS.get(row.get("sid"))
            key = "Telegram" if alias == "tg" else ("WhatsApp" if alias == "wa" else "Other")
            out[key]["n"] += 1
            out[key]["rev"] += float(row.get("price", 0) or 0)
            out[key]["cost"] += float(row.get("cost_inr", 0) or 0)
        return out

    def stats_text(today, week, allt, active, cats=None):
        def line(label, a):
            profit = float(a.get("rev", 0) or 0) - float(a.get("cost", 0) or 0)
            return (
                f"• <b>{label}:</b> <code>{a.get('n', 0)}</code> · "
                f"Rev <code>{short(round(a.get('rev', 0), 2))}</code> · "
                f"Profit <code>{short(round(profit, 2))}</code>"
            )

        t = (
            "<h2>📈 Server 4 — Sales &amp; Profit</h2>"
            "<blockquote>"
            f"{line('Today', today)}<br>"
            f"{line('7 Days', week)}<br>"
            f"{line('All Time', allt)}<br>"
            f"🟢 <b>Active Orders:</b> <code>{active}</code>"
            "</blockquote>"
        )
        if cats:
            cat_lines = "<br>".join(line(name, cats[name]) for name in ("Telegram", "WhatsApp", "Other"))
            t += f"<blockquote>📦 <b>By App</b><br>{cat_lines}</blockquote>"
        return t

    @scb(F.data == "s4_adm_stats")
    async def s4_adm_stats(call: CallbackQuery):
        if not is_admin_func(call.from_user.id):
            return await call.answer("Not authorized", show_alert=True)
        await call.answer("Loading…")
        t, w, a, act = await dbq(_stats_sync)
        cats = await dbq(_category_stats_sync)
        await edit(call, stats_text(t, w, a, act, cats),
                   InlineKeyboardMarkup(inline_keyboard=[
                       [btn("🔄 Refresh", "s4_adm_stats"), btn("⬅️ Back", "s4_adm_home")]
                   ]))

    @dp.message(Command("s4stats"))
    async def s4_stats_cmd(msg: Message):
        if not is_admin_func(msg.from_user.id):
            return
        t, w, a, act = await dbq(_stats_sync)
        cats = await dbq(_category_stats_sync)
        await msg.answer(premiumize_text(stats_text(t, w, a, act, cats)), parse_mode="HTML")

    @scb(F.data == "s4_adm_probe")
    async def s4_adm_probe(call: CallbackQuery):
        if not is_admin_func(call.from_user.id):
            return await call.answer("Not authorized", show_alert=True)
        await call.answer("Probing…")
        checks = [
            ("getServices op=1", dict(action="getServices", operator="1")),
            ("getServicesAndCost op=1", dict(action="getServicesAndCost", operator="1")),
            ("getPrices country=12 op=1", dict(action="getPrices", country="12", operator="1")),
            ("getPricesV3 country=12 op=1 (no service)", dict(action="getPricesV3", country="12", operator="1")),
            ("getNumbersStatus country=12 op=1", dict(action="getNumbersStatus", country="12", operator="1")),
            ("getTopCountriesByService tg", dict(action="getTopCountriesByService", service="edg", operator="1")),
            ("getServicesList (no operator)", dict(action="getServicesList")),
            ("getServiceList op=1", dict(action="getServiceList", operator="1")),
        ]
        t0 = time.time()
        raws = await asyncio.gather(*[client.call(**p) for _, p in checks], return_exceptions=True)
        await call.message.answer(premiumize_text(f"🧪 Probe finished in {time.time() - t0:.1f}s"), parse_mode="HTML")
        for (label, _), raw in zip(checks, raws):
            if isinstance(raw, Exception):
                display = f"ERROR: {type(raw).__name__}: {raw}"
                if hasattr(raw, "code") and raw.code:
                    display += f" (code={raw.code})"
            else:
                display = (raw or 'NO RESPONSE')[:700]
            await call.message.answer(
                premiumize_text(f"<b>{escape(label)}</b>\n<pre>{escape(display)}</pre>"),
                parse_mode="HTML"
            )

    @dp.message(Command("s4addsvc"))
    async def s4_addsvc(msg: Message):
        if not is_admin_func(msg.from_user.id):
            return
        parts = (msg.text or "").split()[1:]
        pairs = [p for p in parts if "=" in p]
        name = " ".join(p for p in parts if "=" not in p).strip()
        try:
            ops = dict(p.split("=", 1) for p in pairs)
            assert name and ops and all(k and v for k, v in ops.items())
        except Exception:
            return await msg.answer(premiumize_text("Usage: <code>/s4addsvc Netflix 1=code1 9=code2</code>"), parse_mode="HTML")
        sid = sid_of(name)
        doc = await dbq(s4_settings.find_one, {"_id": "custom_services"}) or {}
        items = dict(doc.get("items", {}))
        cur = items.get(sid, {"name": name, "ops": {}})
        cur["ops"].update(ops)
        items[sid] = cur
        await dbq(s4_settings.update_one, {"_id": "custom_services"}, {"$set": {"items": items}}, upsert=True)
        client.invalidate_catalog()
        await msg.answer(premiumize_text(f"✅ <b>{escape(name)}</b> saved: {escape(str(cur['ops']))}"), parse_mode="HTML")

    @dp.message(Command("s4delsvc"))
    async def s4_delsvc(msg: Message):
        if not is_admin_func(msg.from_user.id):
            return
        name = " ".join((msg.text or "").split()[1:]).strip()
        doc = await dbq(s4_settings.find_one, {"_id": "custom_services"}) or {}
        items = dict(doc.get("items", {}))
        if not name or items.pop(sid_of(name), None) is None:
            return await msg.answer(premiumize_text("Usage: <code>/s4delsvc Netflix</code>"), parse_mode="HTML")
        await dbq(s4_settings.update_one, {"_id": "custom_services"}, {"$set": {"items": items}}, upsert=True)
        client.invalidate_catalog()
        await msg.answer(premiumize_text(f"🗑️ {escape(name)} removed."), parse_mode="HTML")

    # =================================================================
    #                       USER: SCREENS
    # =================================================================
    def popular_cached(cat):
        return popular_services(cat)

    def busy_kb(retry_cb, back_cb):
        return InlineKeyboardMarkup(inline_keyboard=[
            [btn("🔄 Retry", retry_cb, style="success")],
            [btn("⬅️ Back", back_cb, style="danger")],
        ])

    BUSY_TXT = (
        "<h2>⚠️ Route Busy</h2>"
        "<blockquote>Live carrier rates are refreshing. Your wallet was not charged.</blockquote>"
    )

    def _entry_name(entry):
        return entry_name(entry)

    def primary_service_sid(cat, wanted):
        target = norm(wanted)
        # Exact name wins. This avoids showing a similarly named WhatsApp
        # provider entry with only one country when the real "WhatsApp"
        # service is available in the catalog.
        for sid, entry in cat.items():
            if norm(_entry_name(entry)) == target:
                return sid
        matches = [(sid, e) for sid, e in cat.items() if target in norm(_entry_name(e))]
        return matches[0][0] if matches else None

    async def render_root(target):
        """Server 4 (New Numbers) is Telegram-only — directly show Telegram countries."""
        try:
            cat = await client.services()
        except Exception as e:
            code = getattr(e, "code", None)
            if code == "BAD_KEY":
                msg = "❌ <b>Provider API key invalid</b>\nCheck TEMPORASMS_API_KEY and refresh."
            elif code == "NOT_CONFIGURED":
                msg = "❌ <b>Provider API key not configured</b>\nSet TEMPORASMS_API_KEY (or configure via /otpadmin) and retry."
            else:
                msg = BUSY_TXT
            return await edit(target, msg, busy_kb("s4_open", "buy"))
        sid = primary_service_sid(cat, "Telegram")
        if not sid:
            return await edit(target, "❌ Telegram service is currently unavailable on Server 4.\nPlease try again later.",
                              InlineKeyboardMarkup(inline_keyboard=[[btn("⬅️ Back", "buy", style="danger")]]))
        # Delegate to country list (full scan, fast display)
        await render_countries(target, sid, 0, full=True, fast=True)

    async def open_service_direct(msg: Message, wanted: str):
        if is_maintenance_active():
            return await msg.answer(premiumize_text("⚠️ Server 4 is under maintenance. Please try again later!"), parse_mode="HTML")
        try:
            cat = await client.services()
        except ProviderUnavailable:
            return await msg.answer(premiumize_text(BUSY_TXT), reply_markup=busy_kb("s4_open", "buy"), parse_mode="HTML")
        sid = primary_service_sid(cat, wanted)
        if not sid:
            return await msg.answer(premiumize_text(f"❌ {escape(wanted)} is not available on Server 4 right now."), parse_mode="HTML")
        try:
            await msg.delete()
        except Exception:
            pass
        screen = await msg.answer(premiumize_text(f"⏳ <i>Opening {escape(wanted)}…</i>"), parse_mode="HTML")
        key = (sid, True)
        if _cache_fresh(_s4_offer_cache.get(key), RATE_CACHE_TTL):
            await render_countries(screen, sid, 0, full=True, fast=True)
            return
        await edit(screen, f"⏳ <i>Fetching live rates for {escape(wanted)}…</i>")

        async def _load_direct_service():
            try:
                await render_countries(screen, sid, 0, full=True, fast=True)
            except Exception as e:
                print(f"[server4] direct {wanted} load error: {e!r}")
                try:
                    await edit(screen,
                               f"⚠️ <b>{escape(wanted)}</b> rates are temporarily unavailable.\n"
                               "Please tap Refresh to try again.",
                               busy_kb(f"s4r:{sid}", "buy"))
                except Exception:
                    pass
        spawn(_load_direct_service())

    @dp.message(Command("s4"))
    async def direct_server4_telegram(msg: Message):
        await open_service_direct(msg, "Telegram")

    # "Buy WhatsApp Account" on the home screen -> terms page -> "Agree & Continue" lands here.
    # Opens the WhatsApp section of the All-apps OTP panel in place (no typing, no /wp needed).
    @scb(F.data == "s4_open_wp")
    async def open_whatsapp_cb(call: CallbackQuery, state: FSMContext):
        await state.clear()
        if is_maintenance_active():
            return await call.answer(_i18n.t("wp_maint"), show_alert=True)
        await call.answer()
        cat = await client.services()
        sid = primary_service_sid(cat, "WhatsApp")
        if not sid:
            return await edit(call, "❌ " + _i18n.t("wp_unavailable"),
                              InlineKeyboardMarkup(inline_keyboard=[[btn("⬅️ Back", "back_main", style="danger")]]))
        if _cache_fresh(_s4_offer_cache.get((sid, True)), RATE_CACHE_TTL):
            return await render_countries(call, sid, 0, full=True, fast=True)
        await edit(call, f"⏳ <i>{_i18n.t('wp_opening')}</i>")

        async def _load_whatsapp():
            try:
                await render_countries(call, sid, 0, full=True, fast=True)
            except Exception as e:
                print(f"[server4] open WhatsApp load error: {e!r}")
                try:
                    await edit(call, "⚠️ " + _i18n.t("wp_rates_busy"), busy_kb("s4_open_wp", "back_main"))
                except Exception:
                    pass
        spawn(_load_whatsapp())

    @scb(F.data == "s4_user_root")
    async def user_root(call: CallbackQuery, state: FSMContext):
        await state.clear()
        if is_maintenance_active():
            return await call.answer("⚠️ Server 4 is under maintenance. Please try again later!", show_alert=True)
        await call.answer()
        await render_root(call)

    @scb(F.data == "s4_open")
    async def s4_open(call: CallbackQuery, state: FSMContext):
        await state.clear()
        if is_maintenance_active():
            return await call.answer("⚠️ Server 4 is under maintenance. Please try again later!", show_alert=True)
        await call.answer()
        await render_root(call)

    # legacy buttons from messages sent before the redesign -> just show the new menu
    @scb(F.data.startswith(("s4_usrsrv:", "s4_country:", "s4_buy_ask:", "s4_buy_terms:", "s4_search_prompt:")))
    async def legacy_redirect(call: CallbackQuery):
        await call.answer("Server 4 has been upgraded ✨", show_alert=False)
        await render_root(call)

    @scb(F.data == "s4help")
    async def s4_help(call: CallbackQuery):
        await call.answer()
        help_text = (
            "<h2>ℹ️ Virtual Number Guide</h2>"
            "<blockquote>"
            "• <b>Coverage:</b> 200+ countries across global apps<br>"
            "• <b>Window:</b> 5-minute live SMS session<br>"
            "• <b>Cancel:</b> Manual cancel unlocks after 3 minutes<br>"
            "• <b>Protection:</b> 100% auto-refund if no code arrives"
            "</blockquote>"
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[btn("⬅️ Back", "s4_user_root", style="danger")]]
        )
        await edit(call, help_text, kb)

    @scb(F.data == "s4n")
    async def s4_noop(call: CallbackQuery):
        await call.answer()

    # ---- browse all ----
    async def render_browse(target, page):
        cat = await client.services()
        items = sorted(cat.items(), key=lambda kv: entry_name(kv[1], kv[0]).lower())
        pages = max(1, (len(items) + SERVICES_PER_PAGE - 1) // SERVICES_PER_PAGE)
        page = max(0, min(page, pages - 1))
        chunk = items[page * SERVICES_PER_PAGE:(page + 1) * SERVICES_PER_PAGE]
        text = (
            "<h2>📂 All Services</h2>"
            f"<blockquote>📦 <b>Catalog:</b> <code>{len(items):,}</code> apps · Page <code>{page + 1}/{pages}</code></blockquote>"
        )
        kb = InlineKeyboardBuilder()
        for sid, e in chunk:
            label = entry_name(e, sid)[:28]
            kb.add(btn(label, f"s4s:{sid}:0", icon_custom_emoji_id=app_custom_emoji_id(label)))
        kb.adjust(2)
        nav = []
        if page > 0:
            nav.append(btn("◀️ Prev", f"s4pg:{page - 1}"))
        nav.append(btn(f"{page + 1}/{pages}", "s4n"))
        if page < pages - 1:
            nav.append(btn("Next ▶️", f"s4pg:{page + 1}"))
        kb.row(*nav)
        kb.row(btn("🔎 Search", "s4q", style="success"), btn("⬅️ Back", "s4_user_root", style="danger"))
        await edit(target, text, kb.as_markup())

    @scb(F.data.startswith("s4pg:"))
    async def browse_cb(call: CallbackQuery):
        await call.answer()
        await render_browse(call, int(call.data.split(":")[1]))

    # ---- service search ----
    @scb(F.data == "s4q")
    async def search_prompt(call: CallbackQuery, state: FSMContext):
        await call.answer()
        await state.set_state(Server4UserState.waiting_service_search)
        await state.update_data(prompt_id=call.message.message_id, chat_id=call.message.chat.id)
        await edit(
            call,
            "<h2>🔎 Search App</h2>"
            "<blockquote>Send the app name (e.g. <code>Netflix</code>, <code>OpenAI</code>, <code>Discord</code>).</blockquote>",
            InlineKeyboardMarkup(inline_keyboard=[[btn("✖️ Cancel", "s4_user_root", style="danger")]]),
        )

    @dp.message(StateFilter(Server4UserState.waiting_service_search), F.text)
    async def search_text(msg: Message, state: FSMContext):
        data = await state.get_data()
        q = (msg.text or "").strip()
        try:
            await msg.delete()
        except Exception:
            pass
        cat = await client.services()
        results = search_services(cat, q)
        cancel_row = [btn("✖️ Cancel", "s4_user_root", style="danger")]
        if not results:
            text = (
                "<h2>🔎 Search App</h2>"
                f"<blockquote>❌ No match for <b>{escape(q[:40])}</b>. Send another name:</blockquote>"
            )
            kb = InlineKeyboardMarkup(inline_keyboard=[cancel_row])
        else:
            await state.clear()
            text = (
                f"<h2>🔎 Results — {escape(q[:40])}</h2>"
                f"<blockquote>✅ Found <code>{len(results)}</code> matching services</blockquote>"
            )
            b = InlineKeyboardBuilder()
            for sid, e in results[:20]:
                label = entry_name(e, sid)[:28]
                b.add(btn(label, f"s4s:{sid}:0", icon_custom_emoji_id=app_custom_emoji_id(label)))
            b.adjust(2)
            b.row(btn("🔎 Search Again", "s4q", style="success"), btn("⬅️ Back", "s4_user_root", style="danger"))
            kb = b.as_markup()
        try:
            await bot.edit_message_text(premiumize_text(text), chat_id=data["chat_id"], message_id=data["prompt_id"],
                                        reply_markup=kb, parse_mode="HTML")
        except TelegramBadRequest as e:
            if "not modified" not in str(e).lower():
                await msg.answer(premiumize_text(text), reply_markup=kb, parse_mode="HTML")

    # ---- countries ----
    async def _warm_full(sid):
        """Background full-country scan so the 'All Countries' page is instant next time."""
        try:
            await cached_service_offers(sid, full=True)
        except Exception as e:
            print(f"[server4] full warm {sid} skipped: {e!r}")

    async def render_countries(target, sid, page=0, query=None, full=False, fast=False):
        cat = await client.services()
        svc = cat.get(sid)
        if not svc:
            return await render_root(target)
        svc_title = entry_name(svc, sid)
        title = escape(svc_title)
        # fast=True (WhatsApp / direct opens): never make the user wait for the ~200-country scan.
        # Show the priority countries instantly and warm the full list in the background.
        if full and fast and not _cache_fresh(_s4_offer_cache.get((sid, True)), RATE_CACHE_TTL) \
                and not client.has_cached(f"offers:{sid}:1"):
            spawn(_warm_full(sid))
            full = False
        try:
            if query:
                rows = await asyncio.wait_for(country_rows_query(sid, query), 20)
            else:
                if not client.has_cached(f"offers:{sid}:{int(full)}"):
                    await edit(target, f"<h2>⏳ {title}</h2><blockquote>Loading live countries &amp; rates…</blockquote>")
                rows = await asyncio.wait_for(country_rows(sid, full=full), 25)
        except (ProviderUnavailable, asyncio.TimeoutError):
            return await edit(target, BUSY_TXT, busy_kb(f"s4r:{sid}", "buy"))
        svc_icon = _lf.app_html(svc_title, app_custom_emoji_id(svc_title))
        if not rows:
            rows_kb = []
            if query:
                rows_kb.append([btn("🔎 Search Again", f"s4cq:{sid}", style="success")])
            rows_kb.append([btn("⬅️ Back", f"s4s:{sid}:0" if query else "buy", style="danger")])
            msg = "❌ No matching country in stock." if query else "📭 Out of stock right now."
            return await edit(
                target,
                f"<h2>{svc_icon}</h2><blockquote>{msg}</blockquote>",
                InlineKeyboardMarkup(inline_keyboard=rows_kb),
            )
        pages = max(1, (len(rows) + COUNTRIES_PER_PAGE - 1) // COUNTRIES_PER_PAGE)
        page = max(0, min(page, pages - 1))
        chunk = rows[:COUNTRIES_PER_PAGE] if query else rows[page * COUNTRIES_PER_PAGE:(page + 1) * COUNTRIES_PER_PAGE]
        extra_note = f"{TELEGRAM_SERVICE_NOTE}" if norm(svc_title) == "telegram" else ""
        text = (
            f"<h2>{svc_icon}</h2>"
            "<blockquote>"
            f"🌍 <b>In Stock:</b> <code>{len(rows)}</code> countries · Page <code>{page + 1}/{pages}</code><br>"
            "⚡ Select a country to view live routes"
            "</blockquote>"
            f"{extra_note}"
        )
        pfx = "s4a" if full else "s4s"
        kb = InlineKeyboardBuilder()
        for r in chunk:
            # Country flags are rendered as Telegram custom-emoji entities in text,
            # but inline-button labels do not parse HTML/entities.  Use btn() so
            # the leading <tg-emoji> is converted to icon_custom_emoji_id and
            # the raw entity markup never reaches the Telegram button text.
            kb.add(btn(
                f"{r['flag']} {r['name'][:20]} · {short(r['min_price'])}",
                f"s4c:{sid}:{r['cid']}"
            ))
        kb.adjust(2)
        if not query and pages > 1:
            nav = []
            if page > 0:
                nav.append(btn("◀️", f"{pfx}:{sid}:{page - 1}"))
            nav.append(btn(f"{page + 1}/{pages}", "s4n"))
            if page < pages - 1:
                nav.append(btn("▶️", f"{pfx}:{sid}:{page + 1}"))
            kb.row(*nav)
        kb.row(btn("🔎 Search Country", f"s4cq:{sid}", style="success"), btn("🔄 Refresh", f"s4r:{sid}"))
        if not full and not query:
            kb.row(btn("🌍 All Countries", f"s4a:{sid}:0"))
        kb.row(btn("⬅️ Back", f"s4s:{sid}:0" if query else "buy", style="danger"))
        await edit(target, text, kb.as_markup())

    @scb(F.data.startswith("s4s:"))
    async def countries_cb(call: CallbackQuery, state: FSMContext):
        if is_maintenance_active():
            return await call.answer("⚠️ Server 4 is under maintenance.", show_alert=True)
        await state.clear()
        _, sid, page = call.data.split(":")
        await call.answer()
        await render_countries(call, sid, int(page))

    @scb(F.data.startswith("s4a:"))
    async def countries_full_cb(call: CallbackQuery):
        if is_maintenance_active():
            return await call.answer("⚠️ Server 4 is under maintenance.", show_alert=True)
        _, sid, page = call.data.split(":")
        await call.answer()
        await render_countries(call, sid, int(page), full=True)

    @scb(F.data.startswith("s4r:"))
    async def refresh_cb(call: CallbackQuery):
        sid = call.data.split(":")[1]
        drop_offer_cache(sid)
        await call.answer("🔄 Updated")
        await render_countries(call, sid, 0)

    @scb(F.data.startswith("s4cq:"))
    async def country_search_prompt(call: CallbackQuery, state: FSMContext):
        sid = call.data.split(":")[1]
        await call.answer()
        await state.set_state(Server4UserState.waiting_country_search)
        await state.update_data(sid=sid, prompt_id=call.message.message_id, chat_id=call.message.chat.id)
        await edit(
            call,
            "<h2>🔎 Search Country</h2>"
            "<blockquote>Send a country name (e.g. <code>India</code>, <code>United States</code>, <code>Brazil</code>).</blockquote>",
            InlineKeyboardMarkup(inline_keyboard=[[btn("✖️ Cancel", f"s4s:{sid}:0", style="danger")]]),
        )

    @dp.message(StateFilter(Server4UserState.waiting_country_search), F.text)
    async def country_search_text(msg: Message, state: FSMContext):
        data = await state.get_data()
        await state.clear()
        try:
            await msg.delete()
        except Exception:
            pass

        class _PromptMsg:  # lets render_countries edit the stored prompt message
            async def edit_text(self, text, reply_markup=None, parse_mode=None):
                try:
                    await bot.edit_message_text(premiumize_text(text), chat_id=data["chat_id"], message_id=data["prompt_id"],
                                                reply_markup=reply_markup, parse_mode="HTML")
                except TelegramBadRequest as e:
                    if "not modified" not in str(e).lower():
                        raise

        await render_countries(_PromptMsg(), data["sid"], 0, query=msg.text.strip())

    # ---- operators ----
    def stock_dot(n):
        return "🟢" if n >= 50 else ("🟡" if n >= 10 else "🔴")

    async def render_operators(target, sid, cid, note=None):
        cat = await client.services()
        svc = cat.get(sid)
        if not svc:
            return await render_root(target)
        try:
            cname, ops = await operator_rows(sid, cid)
        except ProviderUnavailable:
            return await edit(target, BUSY_TXT, busy_kb(f"s4c:{sid}:{cid}", f"s4s:{sid}:0"))
        if not ops:
            return await render_countries(target, sid, 0)
        svc_label = entry_name(svc, sid)
        note_block = f"<blockquote>{note}</blockquote>" if note else ""
        text = (
            "<h2>📶 Select Operator</h2>"
            f"{note_block}"
            "<blockquote>"
            f"📦 <b>App:</b> {_lf.app_html(svc_label, app_custom_emoji_id(svc_label))}<br>"
            f"🌍 <b>Country:</b> {flag_for(cname)} <b>{escape(cname)}</b><br>"
            f"⚡ <b>Routes:</b> <code>{len(ops)}</code> live · 🔥 lowest rate"
            "</blockquote>"
        )
        kb = InlineKeyboardBuilder()
        for i, o in enumerate(ops):
            stock = f"{o['count']}" if o["count"] < 1000 else "999+"
            best = "🔥 " if i == 0 and len(ops) > 1 else ""
            kb.row(btn(f"{best}{stock_dot(o['count'])} Route {o['op']} · {short(o['price'])} · {stock}",
                       f"s4o:{sid}:{cid}:{o['op']}"))
        kb.row(btn("🔄 Refresh", f"s4rc:{sid}:{cid}"), btn("🌍 Countries", f"s4s:{sid}:0"))
        kb.row(btn("⬅️ Back", f"s4s:{sid}:0", style="danger"))
        await edit(target, text, kb.as_markup())

    @scb(F.data.startswith("s4c:"))
    async def operators_cb(call: CallbackQuery):
        _, sid, cid = call.data.split(":")
        await call.answer()
        if not co_ready(sid, cid):      # slow path: give instant feedback instead of a frozen screen
            try:
                await edit(call, "⏳ <b>Loading operators…</b>\n<i>Fetching live rates, one moment.</i>", None)
            except Exception:
                pass
        await render_operators(call, sid, cid)

    @scb(F.data.startswith("s4rc:"))
    async def operators_refresh_cb(call: CallbackQuery):
        _, sid, cid = call.data.split(":")
        client._cache.pop(f"co:{sid}:{cid}", None)
        clear_s4_rate_cache(sid, cid)
        await call.answer("🔄 Updated")
        await render_operators(call, sid, cid)

    # ---- confirm (does a LIVE stock check first) ----
    async def go_back_no_stock(call, sid, cid, op, toast=True):
        drop_offer_cache(sid)
        if toast:
            await call.answer(f"❌ Operator {op} has no numbers right now.", show_alert=False)
        await render_operators(call, sid, cid, note=f"⚠️ <b>Operator {op}</b> is out of stock.")

    @scb(F.data.startswith("s4o:"))
    async def confirm_cb(call: CallbackQuery):
        if is_maintenance_active():
            return await call.answer("⚠️ Server 4 is under maintenance.", show_alert=True)
        _, sid, cid, op = call.data.split(":")
        await render_confirm(call, sid, cid, op)

    async def render_confirm(call, sid, cid, op):
        # Confirmation uses the operator snapshot already loaded while browsing.
        # The final Purchase click performs the authoritative live check.
        rows, cname = await cached_country_offers(sid, cid)
        live = next((dict(r) for r in rows
                     if str(r.get("operator")) == str(op) and int(r.get("count", 0) or 0) > 0), None)
                # If the operator snapshot was fetched very recently, reuse it for
        # the immediate Purchase click instead of calling getPricesV3 twice.
        if live and LIVE_REUSE_TTL > 0:
            snapshot = _s4_operator_cache.get((sid, cid))
            if snapshot and (time.time() - float(snapshot.get("t", 0))) <= LIVE_REUSE_TTL:
                live_key = f"live:{sid}:{cid}:{op}"
                client._cache[live_key] = (time.time(), dict(live))

        u = await dbq(users_col.find_one, {"_id": call.from_user.id})
        if not live:
            return await go_back_no_stock(call, sid, cid, op)
        live["code"] = live.get("code") or live.get("service_code")
        try:
            await call.answer()
        except Exception:
            pass
        cat = await client.services()
        if sid not in cat:
            return await render_root(call)
        svc_label = entry_name(cat[sid], sid)
        price = price_for(sid, cname, op, live["cost"])
        bal = float((u or {}).get("balance", 0.0))
        enough = bal >= price - 1e-6
        wallet_line = (
            f"💳 <b>Wallet:</b> <code>{_fmt(bal)}</code> → <code>{_fmt(max(0.0, bal - price))}</code>"
            if enough
            else f"💳 <b>Wallet:</b> <code>{_fmt(bal)}</code> · ❌ Need <code>{_fmt(price - bal)}</code>"
        )
        text = (
            "<h2>🛒 Confirm Order</h2>"
            "<blockquote>"
            f"📦 <b>App:</b> {_lf.app_html(svc_label, app_custom_emoji_id(svc_label))}<br>"
            f"🌍 <b>Country:</b> {flag_for(cname)} <b>{escape(cname)}</b><br>"
            f"📶 <b>Route:</b> <code>Operator {op}</code> (<code>{live['count']}</code> in stock)<br>"
            f"💰 <b>Price:</b> <code>{_fmt(price)}</code><br>"
            f"{wallet_line}"
            "</blockquote>"
            f"<p>🛡️ {OTP_TIMEOUT // 60}m OTP window · 100% auto-refund if no SMS · Cancel after {CANCEL_LOCK // 60}m</p>"
        )
        buy_row = ([btn(f"✅ Buy Now · {short(price)}", f"s4b:{sid}:{cid}:{op}:{price:.2f}", style="success")] if enough
                   else [btn("💰 Add Funds", "recharge", style="success")])
        kb = InlineKeyboardMarkup(inline_keyboard=[
            buy_row,
            [btn("🔁 Routes", f"s4c:{sid}:{cid}"), btn("🌍 Countries", f"s4s:{sid}:0")],
        ])
        await edit(call, text, kb)

    # ---- buy ----
    @scb(F.data.startswith("s4b:"))
    async def buy_cb(call: CallbackQuery):
        if is_maintenance_active():
            return await call.answer("⚠️ Server 4 is under maintenance.", show_alert=True)
        if is_spamming(call.from_user.id):
            return await call.answer("⏳ Please wait a few seconds…", show_alert=False)
        _, sid, cid, op, shown = call.data.split(":")
        shown = float(shown)
        uid = call.from_user.id

        # FINAL PRICE/STOCK GUARD: force a fresh TemporaSMS check only here.
        # This is the authoritative price used for the debit and provider order.
        live = await client.live_offer(sid, cid, op, max_age=LIVE_REUSE_TTL)
        if not live:
            clear_s4_rate_cache(sid, cid)
            return await go_back_no_stock(call, sid, cid, op)
        sync_s4_live_offer(sid, cid, op, live)
        cat = await client.services()
        if sid not in cat:
            return await render_root(call)
        svc_name = entry_name(cat[sid], sid)
        cname = (await client.countries()).get(cid, f"Country {cid}")
        price = price_for(sid, cname, op, live["cost"])
        if price > shown + 0.005:
            await call.answer(f"Price changed to {short(price)}. Please confirm again.", show_alert=True)
            return await render_confirm(call, sid, cid, op)

        # atomic debit BEFORE calling the provider (no double-spend)
        res = await dbq(users_col.update_one, {"_id": uid, "balance": {"$gte": price}}, {"$inc": {"balance": -price}})
        if res.modified_count == 0:
            u = await dbq(users_col.find_one, {"_id": uid}) or {}
            need = price - float(u.get("balance", 0.0))
            kb = InlineKeyboardMarkup(inline_keyboard=[[btn("💰 Recharge", "recharge", style="success")],
                                                       [btn("⬅️ Back", f"s4c:{sid}:{cid}")]])
            await call.answer()
            return await edit(call, f"❌ <b>Insufficient balance</b>\nYou need <b>{_fmt(need)}</b> more.", kb)

        await loyalty.aspend(uid, price, "s4")      # VIP: counts as spend (refund below takes it back)
        # From here on the user has been charged: EVERY path that does not end in a running order refunds.
        outcome, status, reply, act = "error", "error", "", None
        try:
            try:
                await call.answer()
                await edit(call, "⏳ <i>Assigning your number…</i>")
            except Exception as e:      # a UI hiccup must never cost the user money
                print(f"[server4] pre-buy UI edit failed: {e!r}")
            max_price = round(live["cost"] * 1.10 + 0.01, 4)
            status, a, b = await client.get_number(
                live["code"], cid, op, max_price, provider_ids=live.get("provider_ids")
            )
            reply = a
            if status == "ok":
                act = a
                cfg_now = cfg()
                task = {
                    "activation_id": a, "user_id": uid, "chat_id": call.message.chat.id,
                    "msg_id": call.message.message_id, "phone": b, "price": price,
                    "cost": live["cost"], "cost_inr": round(cost_inr(live["cost"], cfg_now), 4),
                    "sid": sid, "service": svc_name, "country_id": cid, "country": cname,
                    "operator": op, "timestamp": time.time(), "status": "active",
                }
                await dbq(tasks_col.insert_one, dict(task))
                spawn(otp_worker(task))
                outcome = "ok"
        except Exception:
            print("[server4] purchase flow crashed")
            traceback.print_exc()
            outcome = "error"
            if act:   # number was bought but we could not record it -> give it back to the provider
                try:
                    await client.cancel(act)
                except Exception:
                    pass
        finally:
            if outcome != "ok":
                try:
                    await dbq(users_col.update_one, {"_id": uid}, {"$inc": {"balance": price}})
                    await loyalty.arefund(uid, price, "s4")
                except Exception:
                    print(f"[server4] !!! REFUND FAILED for user {uid} amount {price}")
                    try:
                        await bot.send_message(ADMIN_LOG_CHANNEL,
                            f"🚨 <b>Server 4: REFUND FAILED</b> user <code>{uid}</code> amount ₹{price:g}", parse_mode="HTML")
                    except Exception:
                        pass
        if outcome == "ok":
            return

        if status == "no_stock":
            drop_offer_cache(sid)
            await edit(call, f"❌ <b>Operator {op}</b> has no numbers available right now.\n<i>Refunded. Taking you back…</i>")
            await asyncio.sleep(1.5)
            return await render_operators(call, sid, cid, note=f"⚠️ <b>Operator {op}</b> is out of stock.")
        alert = None
        if status == "no_balance":
            alert = "🚨 <b>Server 4: provider balance too low!</b>"
        elif reply == "NO_RESPONSE":
            alert = (f"⚠️ <b>Server 4:</b> getNumber timed out for user <code>{uid}</code> ({escape(svc_name)}, {escape(cname)}, op {op}). "
                     "User was refunded — check the provider panel for an orphan activation.")
        if alert:
            try:
                await bot.send_message(ADMIN_LOG_CHANNEL, alert, parse_mode="HTML")
            except Exception:
                pass
        await edit(call, "⚠️ <b>Server busy.</b> You were not charged. Taking you back…")
        await asyncio.sleep(1.5)
        return await render_operators(call, sid, cid)

    # =================================================================
    #                       OTP WORKER
    # =================================================================
    def wait_view(task, elapsed):
        left = int(max(0, OTP_TIMEOUT - elapsed))
        svc_label = task["service"]
        text = (
            "<h2>📲 Number Assigned</h2>"
            "<blockquote>"
            f"📞 <b>Number:</b> <code>+{task['phone']}</code><br>"
            f"📦 <b>App:</b> {_lf.app_html(svc_label, app_custom_emoji_id(svc_label))}<br>"
            f"🌍 <b>Country:</b> {flag_for(task['country'])} <b>{escape(task['country'])}</b> · Route {task['operator']}<br>"
            f"💬 <b>Status:</b> <code>Waiting for SMS…</code><br>"
            f"⏱️ <b>Remaining:</b> <code>{left // 60}:{left % 60:02d}</code>"
            "</blockquote>"
            f"<p>🛡️ Auto-refunds if no SMS arrives in {OTP_TIMEOUT // 60}m.</p>"
        )
        cancel_txt = (f"🔒 Cancel ({int(CANCEL_LOCK - elapsed)}s)" if elapsed < CANCEL_LOCK else "🛑 Cancel & Refund")
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [btn("📋 Copy Number", copy_text=CopyTextButton(text=f"+{task['phone']}"))],
            [btn(cancel_txt, f"s4x:{task['activation_id']}", style="danger" if elapsed < CANCEL_LOCK else "success")],
        ])
        return text, kb

    async def safe_edit(task, text, kb=None):
        try:
            await bot.edit_message_text(premiumize_text(text), chat_id=task["chat_id"], message_id=task["msg_id"],
                                        reply_markup=kb, parse_mode="HTML")
        except Exception as e:
            if "not modified" not in str(e).lower():
                print(f"[server4] edit failed: {e}")

    def end_kb(task):
        return InlineKeyboardMarkup(inline_keyboard=[
            [btn("🔄 Buy Again", f"s4o:{task['sid']}:{task['country_id']}:{task['operator']}")],
            [btn("⬅️ Server 4 Menu", "s4_user_root")],
        ])

    async def close_task(act_id, new_status, **extra):
        """Atomic active -> new_status. Only the winner gets the doc (prevents double refund)."""
        return await dbq(tasks_col.find_one_and_update,
                         {"activation_id": act_id, "status": "active"},
                         {"$set": {"status": new_status, **extra}})

    async def refund_and_notify(task, reason_text):
        if await close_task(task["activation_id"], "cancelled") is None:
            return
        await dbq(users_col.update_one, {"_id": task["user_id"]}, {"$inc": {"balance": task["price"]}})
        await loyalty.arefund(task["user_id"], task["price"], "s4")
        await safe_edit(
            task,
            f"<h2>🔄 Order Refunded</h2>"
            f"<blockquote>{reason_text}<br>💰 <b>Refunded:</b> <code>{_fmt(task['price'])}</code></blockquote>",
            end_kb(task),
        )

    def mask_purchase_number(phone):
        """Show the first half of the phone number and mask the rest for logs."""
        raw = str(phone or "").strip()
        digits = re.sub(r"\D", "", raw)
        if not digits:
            return "••••"
        visible = max(1, (len(digits) + 1) // 2)
        return "+" + digits[:visible] + ("•" * max(4, len(digits) - visible))

    async def deliver_otp(task, otp):
        if await close_task(task["activation_id"], "completed", otp=otp) is None:
            return
        await client.finish(task["activation_id"])
        now_utc = datetime.utcnow()
        await dbq(orders_col.insert_one, {
            "user_id": task["user_id"],
            "number": task["phone"],
            "service": task["service"],
            "country": task["country"],
            "price": task["price"],
            "cost_inr": task.get("cost_inr"),
            "otp": otp,
            "server": "Server 4",
            "status": "purchased",
            "date": now_utc,
            "created_at": now_utc,
        })
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [btn("📋 Copy OTP", copy_text=CopyTextButton(text=str(otp)[:256]))],
            [btn("🔄 Buy Again", f"s4o:{task['sid']}:{task['country_id']}:{task['operator']}"),
             btn("⬅️ Menu", "s4_user_root")],
        ])
        svc_label = task["service"]
        await safe_edit(
            task,
            "<h2>✅ OTP Received</h2>"
            "<blockquote>"
            f"📞 <b>Number:</b> <code>+{task['phone']}</code><br>"
            f"📦 <b>App:</b> {_lf.app_html(svc_label, app_custom_emoji_id(svc_label))}<br>"
            f"🌍 <b>Country:</b> {flag_for(task['country'])} <b>{escape(task['country'])}</b><br>"
            f"🔑 <b>OTP Code:</b> <code>{escape(str(otp))}</code>"
            "</blockquote>",
            kb,
        )
        if norm(task.get("service", "")).startswith("whatsapp"):
            try:
                await bot.send_message(task["chat_id"], premiumize_text(WHATSAPP_SECURITY_TIPS), parse_mode="HTML")
            except Exception as e:
                print(f"[server4] whatsapp tips failed: {e}")
        try:
            u = await dbq(users_col.find_one, {"_id": task["user_id"]}) or {}
            uname = u.get("username") or f"User_{task['user_id']}"
            try:
                me_username = (await bot.get_me()).username or BOT_USERNAME
            except Exception:
                me_username = BOT_USERNAME
            sales_kb = InlineKeyboardMarkup(inline_keyboard=[
                [btn(
                    f"🛒 Buy {task['country']} Now",
                    url=f"https://t.me/{me_username}?start=buy_s4_{task['sid']}_{task['country_id']}",
                    style="success",
                )],
                [btn("📢 Updates", url=f"https://t.me/{UPDATES_CHANNEL}")],
            ])
            masked_number = mask_purchase_number(task["phone"])
            country_display = f"{flag_for(task['country'])} <b>{escape(task['country'])}</b>"
            app_display = _lf.app_html(task["service"], app_custom_emoji_id(task["service"]))
            sales_text = (
                "<h2>✅ New Virtual Number Purchased</h2>"
                "<blockquote>"
                f"📦 <b>App:</b> {app_display}<br>"
                f"🌍 <b>Country:</b> {country_display}<br>"
                f"📞 <b>Number:</b> <code>{escape(masked_number)}</code><br>"
                f"💰 <b>Price:</b> <code>{_fmt(task['price'])}</code><br>"
                "🖥️ <b>Server:</b> <code>Server 4</code>"
                "</blockquote>"
            )
            await bot.send_message(SALES_CHANNEL, premiumize_text(sales_text),
                                   reply_markup=sales_kb, parse_mode="HTML")
            profit_line = ""
            if task.get("cost_inr") is not None:
                pf = task["price"] - task["cost_inr"]
                profit_line = f"<br>📈 <b>Profit:</b> <code>₹{pf:.2f}</code>"
            admin_log_text = (
                "<h2>✅ New Virtual Number Purchased</h2>"
                "<blockquote>"
                f"👤 <b>Buyer:</b> @{escape(uname)} (<code>{task['user_id']}</code>)<br>"
                f"📦 <b>App:</b> {app_display}<br>"
                f"🌍 <b>Country:</b> {country_display} · Route <code>{escape(str(task['operator']))}</code><br>"
                f"📞 <b>Number:</b> <code>{escape(masked_number)}</code><br>"
                f"💰 <b>Price:</b> <code>{_fmt(task['price'])}</code>{profit_line}<br>"
                "🖥️ <b>Server:</b> <code>Server 4</code>"
                "</blockquote>"
            )
            await bot.send_message(ADMIN_LOG_CHANNEL, premiumize_text(admin_log_text), parse_mode="HTML")
        except Exception as e:
            print(f"[server4] log dispatch failed: {e}")

    async def timeout_cancel(task):
        for _ in range(3):
            r = await client.cancel(task["activation_id"])
            if r == "cancelled":
                break
            state, val = await client.get_status(task["activation_id"])
            if state == "otp":
                return await deliver_otp(task, val)
            if state == "cancelled":
                break
            await asyncio.sleep(3)
        else:
            try:
                await bot.send_message(ADMIN_LOG_CHANNEL,
                    f"⚠️ Server 4: could not confirm cancel for <code>{task['activation_id']}</code>; user was refunded.",
                    parse_mode="HTML")
            except Exception:
                pass
        await refund_and_notify(task, f"⏱ <b>Order timed out.</b> No OTP within {OTP_TIMEOUT // 60} minutes.")

    async def otp_worker(task):
        act = task["activation_id"]
        last_ui = 0.0
        while True:
            try:
                doc = await dbq(tasks_col.find_one, {"activation_id": act}, {"status": 1})
                if not doc or doc.get("status") != "active":
                    return
                elapsed = time.time() - task["timestamp"]
                if elapsed >= OTP_TIMEOUT:
                    return await timeout_cancel(task)
                state, val = await client.get_status(act)
                if state == "otp":
                    return await deliver_otp(task, val)
                if state == "cancelled":
                    return await refund_and_notify(task, "❌ <b>Order was cancelled by the provider.</b>")
                if time.time() - last_ui >= UI_REFRESH_EVERY:
                    text, kb = wait_view(task, elapsed)
                    await safe_edit(task, text, kb)
                    last_ui = time.time()
            except Exception as e:  # never let one bad iteration strand a paid order
                print(f"[server4] worker error for {act}: {e!r}")
            await asyncio.sleep(POLL_EVERY)

    @scb(F.data.startswith("s4x:"))
    async def user_cancel(call: CallbackQuery):
        act = call.data.split(":")[1]
        task = await dbq(tasks_col.find_one, {"activation_id": act, "status": "active"})
        if not task or task["user_id"] != call.from_user.id:
            return await call.answer("This order is already finished.", show_alert=True)
        elapsed = time.time() - task["timestamp"]
        if elapsed < CANCEL_LOCK:
            return await call.answer(
                f"⚠️ You can cancel after {CANCEL_LOCK // 60} minutes without OTP (wait {int(CANCEL_LOCK - elapsed)}s).",
                show_alert=True)
        r = await client.cancel(act)
        if r == "early":
            return await call.answer("⚠️ The provider does not allow cancelling yet. Try again shortly.", show_alert=True)
        if r == "error":
            state, val = await client.get_status(act)
            if state == "otp":
                await call.answer("OTP just arrived!", show_alert=False)
                return await deliver_otp(task, val)
            return await call.answer("⚠️ Provider error. Please try again in a moment.", show_alert=True)
        await call.answer("✅ Cancelled & refunded", show_alert=False)
        await refund_and_notify(task, "🛑 <b>Order cancelled.</b>")

    # =================================================================
    #           BACKGROUND: resume orders + keep live rates warm
    # =================================================================
    async def resume_active():
        for t in await dbq(lambda: list(tasks_col.find({"status": "active"}))):
            if "msg_id" not in t or "sid" not in t:   # legacy task from the old module
                continue
            spawn(otp_worker(t))

    async def rates_loop():
        """Keeps Telegram live rates fresh on the RATES key, so Server 4 clicks are instant."""
        await asyncio.sleep(1)
        while True:
            try:
                cat = await client.services()
                await client.countries()
                tg_sid = primary_service_sid(cat, "Telegram")
                warm_sids = [tg_sid] if tg_sid else []
                for sid in warm_sids:
                    try:
                        rows, names = await cached_service_offers(sid, full=False, force=True)
                        _s4_offer_cache[(sid, False)] = {"t": time.time(), "rows": rows, "names": names}
                    except ProviderUnavailable:
                        pass
            except Exception as e:
                print(f"[server4] rates warmer error: {e!r}")
            await asyncio.sleep(max(15, WARM_EVERY))

    async def start_background():
        await resume_active()
        if WARM_EVERY > 0:
            spawn(rates_loop())

    dp.startup.register(start_background)
    dp.shutdown.register(client.close)


# =================================================================
#     Deep link entry (used by bot.py: /start buy_s4_<sid>_<countryId>)
# =================================================================
def _fmt(x):
    from utils import fmt_curr  # viewer's chosen currency
    return fmt_curr(x)


async def send_s4_operator_menu_direct(target_msg: Message, service: str, country_name: str, fmt_curr_func=None):
    """service = service id ('tg'/'wa' legacy aliases accepted); country_name = provider country id or name."""
    cat = await client.services()
    sid = service
    if service in LEGACY_NAMES:
        sid = sid_of(LEGACY_NAMES[service])
    if sid not in cat:
        return await target_msg.answer("❌ This service is not available on Server 4 right now.")
    names = await client.countries()
    cid = country_name if country_name in names else next(
        (k for k, v in names.items() if norm(v) == norm(country_name)), None)
    if cid is None:
        cid = next((k for k, v in names.items() if norm(v) == "usa" and norm(country_name) in ("united states", "us")), None)
    try:
        rows = [r for r in await client.service_offers(sid) if r["country_id"] == cid] if cid else []
    except ProviderUnavailable:
        return await target_msg.answer("⚠️ Provider is busy right now. Please try again in a moment.")
    if not rows:
        kb = InlineKeyboardMarkup(inline_keyboard=[[btn("📂 Choose Country", f"s4s:{sid}:0")]])
        return await target_msg.answer(
            premiumize_text(
                f"<h2>📭 Out of Stock</h2><blockquote><b>{escape(country_name)}</b> is currently unavailable on Server 4.</blockquote>"
            ),
            reply_markup=kb,
            parse_mode="HTML",
        )
    svc_label = entry_name(cat[sid], sid)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [btn("📶 View Routes", f"s4c:{sid}:{cid}", style="success")],
        [btn("⬅️ Server 4 Menu", "s4_user_root")],
    ])
    await target_msg.answer(
        premiumize_text(
            f"<h2>{_lf.app_html(svc_label, app_custom_emoji_id(svc_label))}</h2>"
            f"<blockquote>🌍 <b>Country:</b> {flag_for(names[cid])} <b>{escape(names[cid])}</b></blockquote>"
        ),
        reply_markup=kb,
        parse_mode="HTML",
    )
