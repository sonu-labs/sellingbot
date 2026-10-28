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

import provider
import i18n as _i18n
from tempora import (
    TemporaClient, ProviderUnavailable, retail_price, cost_inr, search_services, popular_services,
    sid_of, norm, PRIORITY_COUNTRIES, LEGACY_NAMES,
)

# ---------------- tunables ----------------
OTP_TIMEOUT = 300          # seconds until auto-cancel + refund
CANCEL_LOCK = 180          # user may cancel only after this many seconds without OTP
POLL_EVERY = 4             # seconds between provider polls
UI_REFRESH_EVERY = 10      # seconds between countdown edits (avoids Telegram flood limits)
SERVICES_PER_PAGE = 20
COUNTRIES_PER_PAGE = 16
BUY_COOLDOWN = 3
WARM_EVERY = int(os.getenv("S3_WARM_EVERY", "300"))   # seconds between background rate refreshes (0 = off)
WARM_TOP = int(os.getenv("S3_WARM_TOP", "3"))        # how many popular services are kept warm

import logfmt as _lf
SALES_CHANNEL = "-1001518758186"
ADMIN_LOG_CHANNEL = "-1003208353049"

API_KEY = os.getenv("TEMPORASMS_API_KEY") or provider.API_KEY
RATES_KEY = os.getenv("TEMPORASMS_RATES_API_KEY") or getattr(provider, "RATES_API_KEY", None) or API_KEY
# purchase lane = API_KEY, live-rates lane = RATES_KEY (separate pools, separate concurrency limits)
client = TemporaClient(API_KEY, provider.BASE_URL, rates_key=RATES_KEY)

# ---------------------------------------------------------------------
# Server 3 TemporaSMS rate cache
# Normal browsing uses these snapshots. The final Purchase click below
# performs a forced live check and then refreshes the matching cache row.
# ---------------------------------------------------------------------
RATE_CACHE_TTL = int(os.getenv("S3_RATE_CACHE_TTL", "300"))
OPERATOR_CACHE_TTL = int(os.getenv("S3_OPERATOR_CACHE_TTL", "180"))

# A freshly displayed operator offer can be reused for a very short time
# during the Purchase click, avoiding a duplicate getPricesV3 request.
LIVE_REUSE_TTL = int(os.getenv("S3_LIVE_REUSE_TTL", "15"))
_s3_offer_cache = {}       # (sid, full) -> {t, rows, names}
_s3_operator_cache = {}    # (sid, cid) -> {t, cname, rows}

def _cache_fresh(entry, ttl):
    return bool(entry) and (time.time() - float(entry.get("t", 0))) < ttl

async def cached_service_offers(sid, full=False, force=False):
    key = (sid, bool(full))
    entry = _s3_offer_cache.get(key)
    if not force and _cache_fresh(entry, RATE_CACHE_TTL):
        return entry["rows"], entry["names"]
    rows = await client.service_offers(sid, full=full, force=force)
    names = await client.countries()
    _s3_offer_cache[key] = {"t": time.time(), "rows": rows, "names": names}
    return rows, names

_co_refreshing = set()

def _rows_from_offer_cache(sid, cid):
    """Operator rows for one country taken from the country-list scan we already did (no provider call)."""
    for (s_, _full), e in list(_s3_offer_cache.items()):
        if s_ != sid or not _cache_fresh(e, RATE_CACHE_TTL):
            continue
        rows = [dict(r) for r in e["rows"] if str(r.get("country_id")) == str(cid)]
        if rows:
            return rows, e["names"]
    return None

def co_ready(sid, cid):
    """True when opening this country needs NO provider call (instant)."""
    return (_cache_fresh(_s3_operator_cache.get((sid, cid)), OPERATOR_CACHE_TTL)
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
            _s3_operator_cache[k] = {"t": time.time(), "rows": rows, "cname": names.get(cid, f"Country {cid}")}
    except Exception:
        pass
    finally:
        _co_refreshing.discard(k)

async def cached_country_offers(sid, cid, force=False):
    key = (sid, cid)
    entry = _s3_operator_cache.get(key)
    if not force and _cache_fresh(entry, OPERATOR_CACHE_TTL):
        return entry["rows"], entry["cname"]
    if not force:
        # FAST PATH: the country list was built from the same provider data -> reuse it instantly
        # (no 1 call per operator), then refresh live rates in the background.
        hit = _rows_from_offer_cache(sid, cid)
        if hit:
            rows, names = hit
            cname = names.get(cid, f"Country {cid}")
            _s3_operator_cache[key] = {"t": time.time() - max(0, OPERATOR_CACHE_TTL - 20), "rows": rows, "cname": cname}
            asyncio.ensure_future(_refresh_country_bg(sid, cid))
            return rows, cname
    rows = await client.country_offers(sid, cid)
    names = await client.countries()
    cname = names.get(cid, f"Country {cid}")
    _s3_operator_cache[key] = {"t": time.time(), "rows": rows, "cname": cname}
    return rows, cname

def clear_s3_rate_cache(sid=None, cid=None):
    if sid is None:
        _s3_offer_cache.clear()
        _s3_operator_cache.clear()
        return
    for key in list(_s3_offer_cache):
        if key[0] == sid:
            _s3_offer_cache.pop(key, None)
    if cid is None:
        for key in list(_s3_operator_cache):
            if key[0] == sid:
                _s3_operator_cache.pop(key, None)
    else:
        _s3_operator_cache.pop((sid, cid), None)

def sync_s3_live_offer(sid, cid, op, live):
    """Push the final live purchase check into all matching local snapshots."""
    key = (sid, cid)
    entry = _s3_operator_cache.get(key)
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

    for key, entry in list(_s3_offer_cache.items()):
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
    "unit_to_inr": 1.0,
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
# Put your Telegram custom-emoji IDs here. Empty values keep Unicode.
# Country flags use ISO alpha-2 codes in CUSTOM_FLAG_EMOJI_IDS.
# =====================================================================
CUSTOM_EMOJI_IDS = {
    "❤️‍🔥": "5255861796350224063","⚠️": "6129782440157256336", "❌": "5974083768233760323", "⬅️": "5409284148491726576", "✅": "5980930633298350051", "🔄": "5346269127059196142", "🛡️": "5042328396193864923",
    "🔎": "5039649904264217620", "🌍": "5346024644635804737", "💳": "5911101139444567404", "💰": "5296355151743838259", "📦": "5409230963911701228", "🟢": "5913241115489734452", "⏳": "6084396322444544568",
    "📈": "5039759318556083411", "🔥": "5408849420491962048", "📂": "5409150592188690356", "✖️": "6267000941547885720", "📞": "5172893417717367746", "💬": "6129939837823753679", "🔑": "5278573677900752088",
    "🔴": "5915991999093149658", "🧪": "5134183530313548836", "◀️": "5287416809404770049", "▶️": "5287267692435218919", "📶": "5469680227716189235", "🛒": "5985605142559329776", "🚨": "5765005232710882842",
    "⏱️": "4904882772637648609", "🛑": "6084515769780013003", "📋": "5269381442864963988", "➖": "5346176879751612829", "➕": "5192839301674584945", "⚙️": "4967490064234840998", "🛠": "5258023599419171861",
    "📌": "5409337058193847247", "🗑": "6129486856212979482", "📊": "5042290883949495533", "⚡️": "6323425039311967877", "💯": "6181465091595049176", "🚀": "5254000426243604006", "⭐️": "5388909270316114329",
    "✨": "5226928895189598791", "📭": "5472239203590888751", "🟡": "5915735340437474837", "🔁": "5346269127059196142", "🔒": "5330066942755615469", "📢": "5399967660052081305", "👤": "5258011929993026890",
    "📱": "5172893417717367746", "💡": "6014859899979175860", "ℹ️": "5042306247047513767", "🏠": "5321429707688801442", "🌐": "5346024644635804737", "📝": "5273951446981683649", "🔢": "5226702984204797593",
}
CUSTOM_FLAG_EMOJI_IDS = {
    "AD": "5294215205763434181",
    "AE": "5294314831824835370",
    "AF": "5291937511591925566",
    "AG": "5294005972136647964",
    "AI": "5292186323342350940",
    "AL": "5294202819077756005",
    "AM": "5291978717508164018",
    "AO": "5294516785482062829",
    "AR": "5292208210495689627",
    "AS": "5291994273879709721",
    "AT": "5291975174160145850",
    "AU": "5294444247779399477",
    "AW": "5294007002928798927",
    "AX": "5294077418917616055",
    "AZ": "5294323533428579078",
    "BB": "5294526187165471742",
    "BD": "5291824687096027834",
    "BE": "5291774466043435275",
    "BF": "5294153164960848949",
    "BG": "5294308947719640437",
    "BH": "5294108398516720753",
    "BI": "5294051631933967760",
    "BJ": "5293984969746566866",
    "BN": "5292098293692650297",
    "BO": "5294201479047957700",
    "BR": "5291892229751723900",
    "BS": "5294031587321600012",
    "BT": "5294121983498277263",
    "BW": "5294026179957772585",
    "BY": "5294134426018536120",
    "BZ": "5294171848068584842",
    "CA": "5292290347450259214",
    "CF": "5294210571493724819",
    "CG": "5294035229453865597",
    "CH": "5291791748991835084",
    "CI": "5293991322003200135",
    "CK": "5292098684534675100",
    "CL": "5294231037012888049",
    "CM": "5291997306126626950",
    "CN": "5294068833277990704",
    "CO": "5294010206974397371",
    "CR": "5292063805105263554",
    "CU": "5291963947115631526",
    "CV": "5292203503211535593",
    "CY": "5294062721539526918",
    "CZ": "5294242852467923382",
    "DE": "5292013274815028523",
    "DJ": "5294127214768468283",
    "DK": "5294531860817268837",
    "DM": "5294485513825178032",
    "DO": "5294522197140857947",
    "DZ": "5294048127240655242",
    "EC": "5292083733753517221",
    "EE": "5291951143818123103",
    "EG": "5293992082212409502",
    "ER": "5291922054004625949",
    "ES": "5294513087515216901",
    "ET": "5292245976143124155",
    "EU": "5291992809295861098",
    "FI": "5294049961191690629",
    "FM": "5291838156113470124",
    "FR": "5291817660529533837",
    "GA": "5294321325815389139",
    "GB": "5293993521026453119",
    "GE": "5294349389131697267",
    "GH": "5294347396266873249",
    "GI": "5292055799286224027",
    "GL": "5292014752283774878",
    "GM": "5294399820637688352",
    "GN": "5291892096607739008",
    "GQ": "5292170045416297012",
    "GR": "5291948395039054764",
    "GT": "5294336633078831209",
    "GW": "5294409819321550432",
    "GY": "5292062692708736193",
    "HK": "5292166459118606932",
    "HN": "5291901034434682297",
    "HR": "5291999676948569127",
    "HT": "5292045130587462814",
    "HU": "5294229581018975260",
    "IE": "5294471971793293647",
    "IL": "5294069056616289553",
    "IM": "5294318478252070646",
    "IN": "5291933173674957761",
    "IQ": "5294325010897327367",
    "IR": "5294220170745630736",
    "IS": "5294354358408859664",
    "IT": "5291826830284709120",
    "JE": "5291950280529697493",
    "JM": "5294505107465982830",
    "JO": "5291988613112814801",
    "JP": "5291799063321139445",
    "KE": "5292111852904416801",
    "KG": "5292091954320922577",
    "KH": "5294225191562400452",
    "KI": "5294538934628405146",
    "KM": "5294351381996521508",
    "KP": "5294193812531333564",
    "KR": "5294408281723262763",
    "KW": "5292066437920218075",
    "KZ": "5294227175837290463",
    "LA": "5291981530711746037",
    "LB": "5294193108156699621",
    "LI": "5292048742654957785",
    "LK": "5292102670264328257",
    "LR": "5291793810576137439",
    "LS": "5292040693886247604",
    "LT": "5294343084119708700",
    "LU": "5294423709245787718",
    "LV": "5292236016113966127",
    "LY": "5291858711826946840",
    "MA": "5292108962391414885",
    "MC": "5294378161117614233",
    "MD": "5294158486425325375",
    "MG": "5291991568050312348",
    "MH": "5294180730060954484",
    "MK": "5294023611567332075",
    "ML": "5292086972158858331",
    "MM": "5294254478944393569",
    "MN": "5294316532631883496",
    "MR": "5294429743674840973",
    "MT": "5294532213004588353",
    "MU": "5294127824653797277",
    "MV": "5292004203844097218",
    "MW": "5294241881805312589",
    "MX": "5294535073452809778",
    "MY": "5291858351049696702",
    "MZ": "5294086708931874940",
    "NA": "5292021761670404922",
    "NE": "5291809418487290691",
    "NG": "5294456308047563965",
    "NI": "5294240825243358100",
    "NL": "5291917797692042265",
    "NO": "5291761718580502030",
    "NP": "5294458756178924088",
    "NR": "5294463274484521342",
    "NU": "5294471336138134209",
    "NZ": "5294189019347833274",
    "OM": "5291813666209946812",
    "PA": "5291959935616178405",
    "PE": "5292099427564018941",
    "PG": "5291917995260533077",
    "PH": "5291798075478661634",
    "PK": "5291825606219029010",
    "PL": "5292190970496963836",
    "PR": "5292121516580820347",
    "PS": "5294289826525238172",
    "PT": "5294436555492973610",
    "PY": "5294525611639852679",
    "QA": "5292166360334357676",
    "RO": "5294107724206856227",
    "RS": "5294458584380230360",
    "RU": "5294335323113807278",
    "RW": "5294191265615729158",
    "SA": "5294163983983463099",
    "SB": "5294283890880433237",
    "SC": "5291891186074672309",
    "SD": "5294177148058228060",
    "SE": "5291737091238026321",
    "SG": "5294451304410663668",
    "SI": "5294279359689938006",
    "SK": "5294538440707166931",
    "SL": "5294494314213167952",
    "SM": "5292147350809106831",
    "SN": "5292087023698466689",
    "SO": "5294058817414255960",
    "SR": "5294396668131692138",
    "ST": "5292183188016222701",
    "SV": "5294337307388695687",
    "SY": "5294013428199869487",
    "SZ": "5294312482477724867",
    "TC": "5294320866253884749",
    "TD": "5291780728105753403",
    "TG": "5294097669688415562",
    "TH": "5293994384314882755",
    "TJ": "5294120269806328883",
    "TM": "5294098958178603764",
    "TN": "5294484680601521871",
    "TO": "5294283689016973348",
    "TR": "5293993400767367408",
    "TT": "5294362935458548705",
    "TW": "5294095745543069603",
    "TZ": "5292146096678658977",
    "UA": "5294263837678131580",
    "UG": "5294192317882716626",
    "US": "5294244076533600593",
    "UY": "5291928449210932974",
    "UZ": "5294217645304864345",
    "VE": "5294476442854247878",
    "VI": "5294228039125718124",
    "VN": "5294235963340379688",
    "VU": "5294448585696368047",
    "YE": "5294058972033076492",
    "ZA": "5294325281480266304",
    "ZM": "5294100109229838880",
    "ZW": "5294422158762592930",
}

# The pack also includes the three UK constituent flags separately from GB.
# These are matched by exact Unicode flag/name when a service uses England/Scotland/Wales.
CUSTOM_FLAG_UNICODE_EMOJI_IDS = {
    "🏴\U000e0067\U000e0062\U000e0065\U000e006e\U000e0067\U000e006c\U000e0061\U000e006e\U000e0064\U000e007f": "5294410107084365278",
    "🏴\U000e0067\U000e0062\U000e0073\U000e0063\U000e006f\U000e0074\U000e006c\U000e0061\U000e006e\U000e0064\U000e007f": "5294434665707368018",
    "🏴\U000e0067\U000e0062\U000e0077\U000e0061\U000e006c\U000e0065\U000e0073\U000e007f": "5294139949346476093",
    "🚩": "5294236848103643477",
}
CUSTOM_FLAG_NAME_ALIASES = {
    "england": "🏴\U000e0067\U000e0062\U000e0065\U000e006e\U000e0067\U000e006c\U000e0061\U000e006e\U000e0064\U000e007f",
    "scotland": "🏴\U000e0067\U000e0062\U000e0073\U000e0063\U000e006f\U000e0074\U000e006c\U000e0061\U000e006e\U000e0064\U000e007f",
    "wales": "🏴\U000e0067\U000e0062\U000e0077\U000e0061\U000e006c\U000e0065\U000e0073\U000e007f",
}

# Home-screen Server 3 app icons, in the exact order supplied by the user:
# 1 Telegram, 2 WhatsApp, 3 Google/Gmail, 4 Instagram, 5 ChatGPT/OpenAI,
# 6 Twitter/X, 7 Facebook, 8 TikTok, 9 every other app.
APP_CUSTOM_EMOJI_IDS = {
    "telegram": "5345965137863928359",
    "whatsapp": "5935973359480213803",
    "google / gmail": "5796209712009581332",
    "instagram": "5348533850429470257",
    "chatgpt / openai": "5796185041717433060",
    "twitter / x": "4997271698177787448",
    "facebook": "5393310276059678201",
    "tiktok": "5397672154651181662",
}
OTHER_APP_CUSTOM_EMOJI_ID = "5409119256107297715"


def app_custom_emoji_id(label: str) -> str:
    """Return the fixed premium icon for a Server 3 home-screen app."""
    key = norm(label)
    if key in APP_CUSTOM_EMOJI_IDS:
        return APP_CUSTOM_EMOJI_IDS[key]
    # Also handle common provider labels such as Google/Gmail or ChatGPT/OpenAI.
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
        return f"\x00S3PE{len(protected)-1}\x00"
    out = re.sub(r'<tg-emoji\b[^>]*>.*?</tg-emoji>', protect, str(text), flags=re.S)
    for emoji in sorted(CUSTOM_EMOJI_IDS, key=len, reverse=True):
        eid = str(CUSTOM_EMOJI_IDS.get(emoji) or "").strip()
        if eid:
            out = out.replace(emoji, _premium_tag(emoji, eid))
    for i, value in enumerate(protected):
        out = out.replace(f"\x00S3PE{i}\x00", value)
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


class Server3UserState(StatesGroup):
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


# Shown right after the OTP of a WhatsApp order (Server 3).
WHATSAPP_SECURITY_TIPS = (
    "<b>🔐 Secure Your WhatsApp Acc0unt</b>\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "<blockquote expandable>"
    "• after login do not use the account for atleast 3-4 days  "
    "do not message to anyone if u do that , your account will ban instantly \n"
    "• do not create channel / group until 3 days \n"
    "• do not login again and again , whenever u login the acc in new device repeat these steps to prevent high risk of account ban \n"
    "• add your recovery mail , and 2 step verification pass after 24 hours\n"
    "• Virtual numbers can be recycled later, so the recovery email + 2-step PIN are your real protection.\n"
    "</blockquote>\n"
    "<blockquote expandable><i>⚠️ Follow these tips to keep your account safe and avoid bans , we do not give any guarantee or provide refunds , these details are shared with you so u can prevent your acc and there is no full guarantee that your wp acc will still survive  it is platform security system .. by following these steps risk of getting ban reduces a lot.</i></blockquote>"
)

# Shown above the country list when the user opens the Telegram service (Server 3).
TELEGRAM_SERVICE_NOTE = (
    "<blockquote>⚠️ <b>Use this service only for Telegram number change.</b>\n"
    "🛒 To buy ready-made Telegram accounts use <b>Server 2</b> (good quality) "
    "or <b>Server 4</b> (cheap price, low quality).</blockquote>"
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
            # Server 3 can be imported either as Bot.server3 or as a top-level module.
            try:
                from .server4 import LZT_COUNTRY_MAP, flag_emoji
            except (ImportError, ValueError):
                from server4 import LZT_COUNTRY_MAP, flag_emoji
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
            print(f"[server3] country flag map load failed: {e!r}")
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
    m = target.message if isinstance(target, CallbackQuery) else target
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
    clear_s3_rate_cache(sid)


def short(n: float) -> str:
    from utils import fmt_short  # viewer's chosen currency
    return fmt_short(n)


# =====================================================================
def register_server3_handlers(dp: Dispatcher, bot: Bot, db, users_col, orders_col, is_admin_func, fmt_curr_func):
    s3_settings = db["server3_settings"]
    tasks_col = db["server3_tasks"]
    _bg = set()

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
                    print(f"[server3] ERROR in {fn.__name__} (data={getattr(call, 'data', None)})")
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
            doc = s3_settings.find_one({"_id": "config"}) or {}
            m = s3_settings.find_one({"_id": "maintenance_config"}) or {}
            _sc.update(t=time.time(), maint=bool(m.get("enabled", False)),
                       cfg={**DEFAULT_CFG, **{k: v for k, v in doc.items() if k != "_id"}})
        except Exception as e:
            print(f"[server3] settings read failed: {e!r}")
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
        doc = s3_settings.find_one({"_id": "custom_services"}) or {}
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
        ids = [cid for cid, n in names.items() if q in norm(n)][:8]
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
        return ("🔑 Keys: <b>split</b> ✅ (purchase key + separate live-rates key)" if client.separate_keys
                else "🔑 Keys: <b>shared</b> ⚠️ — set <code>TEMPORASMS_RATES_API_KEY</code> to give live rates their own key")

    def admin_kb():
        active = not is_maintenance_active()
        status = "🟢 ACTIVE" if active else "🔴 DISABLED"
        return InlineKeyboardMarkup(inline_keyboard=[
            [btn("📊 Overview", "s3_adm_home"), btn("💰 Profit %", "s3_adm_pricing")],
            [btn("📈 Sales & Profit", "s3_adm_stats"), btn("💳 Provider Balance", "s3_adm_balance")],
            [btn("🔄 Refresh Catalog", "s3_adm_refresh")],
            [btn(f"{status} — Click to {'Disable' if active else 'Enable'}", "s3_adm_toggle_maint")],
            [btn("🧪 Probe API", "s3_adm_probe")],
            [btn("❌ Close", "admin_back")],
        ])

    def admin_text():
        c = cfg()
        status = "🟢 ACTIVE" if not is_maintenance_active() else "🔴 DISABLED"
        return (
            "⚙️ <b>Server 3 Admin Panel</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>Status:</b> {status}\n"
            f"<b>Telegram profit:</b> {float(c.get('tg_markup_pct', c.get('markup_pct', 100))):g}%\n"
            f"<b>WhatsApp profit:</b> {float(c.get('wa_markup_pct', c.get('markup_pct', 100))):g}%\n"
            f"<b>Other apps profit:</b> {float(c.get('other_markup_pct', c.get('markup_pct', 100))):g}%\n"
            f"<b>Minimum profit:</b> ₹{float(c.get('min_profit', 10)):g}\n\n"
            f"{keys_line()}\n\n"
            "<i>Use the buttons below to manage Server 3. No pricing command is required.</i>"
        )

    @dp.message(Command("server3"))
    async def admin_panel(msg: Message):
        if not is_admin_func(msg.from_user.id):
            return await msg.answer(premiumize_text("❌ Not authorized."))
        await msg.answer(premiumize_text(admin_text()), reply_markup=admin_kb(), parse_mode="HTML")

    @scb(F.data == "s3_adm_home")
    async def s3_adm_home(call: CallbackQuery):
        if not is_admin_func(call.from_user.id):
            return await call.answer("Not authorized", show_alert=True)
        await call.answer()
        await edit(call, admin_text(), admin_kb())

    @scb(F.data == "s3_adm_balance")
    async def s3_adm_balance(call: CallbackQuery):
        if not is_admin_func(call.from_user.id):
            return await call.answer("Not authorized", show_alert=True)
        bal = await client.balance()
        await call.answer(f"💳 Provider balance: {bal}" if bal is not None else "⚠️ Could not fetch balance", show_alert=True)

    @scb(F.data == "s3_adm_toggle_maint")
    async def s3_adm_toggle_maint(call: CallbackQuery):
        if not is_admin_func(call.from_user.id):
            return await call.answer("Not authorized", show_alert=True)
        new_disabled = not is_maintenance_active()
        await dbq(s3_settings.update_one, {"_id": "maintenance_config"},
                  {"$set": {"enabled": new_disabled}}, upsert=True)
        bump_settings()
        await call.answer("Server 3 disabled" if new_disabled else "Server 3 enabled", show_alert=False)
        await edit(call, admin_text(), admin_kb())

    @scb(F.data == "s3_adm_refresh")
    async def s3_adm_refresh(call: CallbackQuery):
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
            "💰 <b>Server 3 Profit Percentages</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"📱 <b>Telegram:</b> {float(c.get('tg_markup_pct', c.get('markup_pct', 100))):g}%\n"
            f"💬 <b>WhatsApp:</b> {float(c.get('wa_markup_pct', c.get('markup_pct', 100))):g}%\n"
            f"🌐 <b>Other apps:</b> {float(c.get('other_markup_pct', c.get('markup_pct', 100))):g}%\n"
            f"💵 <b>Minimum profit:</b> ₹{float(c.get('min_profit', 10)):g}\n"
            f"💱 <b>Provider unit → INR:</b> {float(c.get('unit_to_inr', 1)):g}\n\n"
            "<i>Final price = provider cost converted to INR, plus the selected percentage, "
            "with the minimum-profit floor applied.</i>"
        )

    def pricing_kb():
        return InlineKeyboardMarkup(inline_keyboard=[
            [btn("📱 Telegram −5%", "s3pr:m:tg:-5"), btn("Set Telegram %", "s3pr:set:tg"), btn("Telegram +5%", "s3pr:m:tg:5")],
            [btn("💬 WhatsApp −5%", "s3pr:m:wa:-5"), btn("Set WhatsApp %", "s3pr:set:wa"), btn("WhatsApp +5%", "s3pr:m:wa:5")],
            [btn("🌐 Other −5%", "s3pr:m:other:-5"), btn("Set Other %", "s3pr:set:other"), btn("Other +5%", "s3pr:m:other:5")],
            [btn("💵 Min Profit −₹5", "s3pr:p:-5"), btn("Min Profit +₹5", "s3pr:p:5")],
            [btn("⬅️ Back", "s3_adm_home")],
        ])

    @scb(F.data == "s3_adm_pricing")
    async def s3_adm_pricing(call: CallbackQuery):
        if not is_admin_func(call.from_user.id):
            return await call.answer("Not authorized", show_alert=True)
        await call.answer()
        await edit(call, pricing_text(), pricing_kb())

    @scb(F.data.startswith("s3pr:"))
    async def s3_pricing_buttons(call: CallbackQuery, state: FSMContext):
        if not is_admin_func(call.from_user.id):
            return await call.answer("Not authorized", show_alert=True)
        parts = call.data.split(":")
        kind = parts[1]
        if kind == "set":
            category = parts[2]
            await state.set_state(Server3UserState.waiting_admin_pct)
            await state.update_data(admin_pct_category=category)
            label = {"tg": "Telegram", "wa": "WhatsApp", "other": "Other apps"}[category]
            await call.answer()
            return await edit(
                call,
                f"💰 <b>Set {label} profit percentage</b>\n\n"
                "Send only the percentage number, for example <code>75</code> for 75%.\n"
                "Allowed range: <code>0</code>–<code>1000</code>.",
                InlineKeyboardMarkup(inline_keyboard=[[btn("✖️ Cancel", "s3_adm_pricing", style="danger")]])
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

        await dbq(s3_settings.update_one, {"_id": "config"}, {"$set": {field: v}}, upsert=True)
        bump_settings()
        await call.answer("✅ Saved & live")
        await edit(call, pricing_text(), pricing_kb())

    @dp.message(StateFilter(Server3UserState.waiting_admin_pct), F.text)
    async def s3_admin_pct_input(msg: Message, state: FSMContext):
        if not is_admin_func(msg.from_user.id):
            return
        data = await state.get_data()
        category = data.get("admin_pct_category")
        field = {"tg": "tg_markup_pct", "wa": "wa_markup_pct", "other": "other_markup_pct"}.get(category)
        if not field:
            await state.clear()
            return await msg.answer(premiumize_text("⚠️ Pricing session expired. Open /server3 again."))
        try:
            value = float((msg.text or "").strip().replace(",", "."))
            if not 0 <= value <= 1000:
                raise ValueError
        except Exception:
            return await msg.answer(premiumize_text("❌ Enter a number from 0 to 1000, for example 75."))
        await dbq(s3_settings.update_one, {"_id": "config"},
                  {"$set": {field: value, "category_pricing": True}}, upsert=True)
        # Keep the old global field synchronized for compatibility with older tooling.
        await dbq(s3_settings.update_one, {"_id": "config"}, {"$set": {"markup_pct": value}}, upsert=True)
        bump_settings()
        await state.clear()
        try:
            await msg.delete()
        except Exception:
            pass
        await msg.answer(premiumize_text(
            f"✅ <b>{ {'tg':'Telegram','wa':'WhatsApp','other':'Other apps'}[category] }</b> profit set to <b>{value:g}%</b>."
        ), reply_markup=pricing_kb(), parse_mode="HTML")

    # Old commands are retained as compatibility shortcuts; /server3 is the main panel.
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
            await dbq(s3_settings.update_one, {"_id": "config"}, {"$set": sets}, upsert=True)
            bump_settings()
            await msg.answer(premiumize_text(f"✅ Saved {field} = <b>{v:g}</b>%\n\nOpen /server3 for the full panel."), parse_mode="HTML")
        return handler

    dp.message.register(_setter("markup_pct"), Command("s3markup"))
    dp.message.register(_setter("min_profit"), Command("s3minprofit"))
    dp.message.register(_setter("unit_to_inr"), Command("s3unit"))

    @dp.message(Command("s3legacy"))
    async def s3_legacy(msg: Message):
        if not is_admin_func(msg.from_user.id):
            return
        await msg.answer(premiumize_text(
            "ℹ️ Fixed legacy Telegram/WhatsApp prices are no longer used for live Server 3 pricing. "
            "Set Telegram/WhatsApp/Other percentages from /server3."
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
            pct = f" ({profit / a['cost'] * 100:.0f}% on cost)" if a.get("cost") else ""
            return f"<b>{label}</b>: {a.get('n', 0)} sales · revenue {short(round(a.get('rev', 0), 2))} · profit {short(round(profit, 2))}{pct}"
        t = (
            "📈 <b>Server 3 — Sales & Profit</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"{line('Today (IST)', today)}\n"
            f"{line('Last 7 days', week)}\n"
            f"{line('All time', allt)}\n\n"
            f"🟢 <b>Active orders:</b> {active}\n"
        )
        if cats:
            t += "\n<b>By application</b>\n"
            for name in ("Telegram", "WhatsApp", "Other"):
                t += line(name, cats[name]) + "\n"
        t += "\n<i>Profit = sale price − provider cost recorded when the number was purchased.</i>"
        return t

    @scb(F.data == "s3_adm_stats")
    async def s3_adm_stats(call: CallbackQuery):
        if not is_admin_func(call.from_user.id):
            return await call.answer("Not authorized", show_alert=True)
        await call.answer("Loading…")
        t, w, a, act = await dbq(_stats_sync)
        cats = await dbq(_category_stats_sync)
        await edit(call, stats_text(t, w, a, act, cats),
                   InlineKeyboardMarkup(inline_keyboard=[
                       [btn("🔄 Refresh", "s3_adm_stats"), btn("⬅️ Back", "s3_adm_home")]
                   ]))

    @dp.message(Command("s3stats"))
    async def s3_stats_cmd(msg: Message):
        if not is_admin_func(msg.from_user.id):
            return
        t, w, a, act = await dbq(_stats_sync)
        cats = await dbq(_category_stats_sync)
        await msg.answer(premiumize_text(stats_text(t, w, a, act, cats)), parse_mode="HTML")

    @scb(F.data == "s3_adm_probe")
    async def s3_adm_probe(call: CallbackQuery):
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
        raws = await asyncio.gather(*[client.call(**p) for _, p in checks])
        await call.message.answer(premiumize_text(f"🧪 Probe finished in {time.time() - t0:.1f}s"), parse_mode="HTML")
        for (label, _), raw in zip(checks, raws):
            await call.message.answer(
                premiumize_text(f"<b>{escape(label)}</b>\n<pre>{escape((raw or 'NO RESPONSE')[:700])}</pre>"),
                parse_mode="HTML"
            )

    @dp.message(Command("s3addsvc"))
    async def s3_addsvc(msg: Message):
        if not is_admin_func(msg.from_user.id):
            return
        parts = (msg.text or "").split()[1:]
        pairs = [p for p in parts if "=" in p]
        name = " ".join(p for p in parts if "=" not in p).strip()
        try:
            ops = dict(p.split("=", 1) for p in pairs)
            assert name and ops and all(k and v for k, v in ops.items())
        except Exception:
            return await msg.answer(premiumize_text("Usage: <code>/s3addsvc Netflix 1=code1 9=code2</code>"), parse_mode="HTML")
        sid = sid_of(name)
        doc = await dbq(s3_settings.find_one, {"_id": "custom_services"}) or {}
        items = dict(doc.get("items", {}))
        cur = items.get(sid, {"name": name, "ops": {}})
        cur["ops"].update(ops)
        items[sid] = cur
        await dbq(s3_settings.update_one, {"_id": "custom_services"}, {"$set": {"items": items}}, upsert=True)
        client.invalidate_catalog()
        await msg.answer(premiumize_text(f"✅ <b>{escape(name)}</b> saved: {escape(str(cur['ops']))}"), parse_mode="HTML")

    @dp.message(Command("s3delsvc"))
    async def s3_delsvc(msg: Message):
        if not is_admin_func(msg.from_user.id):
            return
        name = " ".join((msg.text or "").split()[1:]).strip()
        doc = await dbq(s3_settings.find_one, {"_id": "custom_services"}) or {}
        items = dict(doc.get("items", {}))
        if not name or items.pop(sid_of(name), None) is None:
            return await msg.answer(premiumize_text("Usage: <code>/s3delsvc Netflix</code>"), parse_mode="HTML")
        await dbq(s3_settings.update_one, {"_id": "custom_services"}, {"$set": {"items": items}}, upsert=True)
        client.invalidate_catalog()
        await msg.answer(premiumize_text(f"🗑️ {escape(name)} removed."), parse_mode="HTML")

    # =================================================================
    #                       USER: SCREENS
    # =================================================================
    _pop = {"id": None, "v": None}

    def popular_cached(cat):
        if _pop["id"] != id(cat):
            _pop.update(id=id(cat), v=popular_services(cat))
        return _pop["v"]

    def busy_kb(retry_cb, back_cb):
        return InlineKeyboardMarkup(inline_keyboard=[[btn("🔄 Try again", retry_cb, style="success")],
                                                     [btn("⬅️ Back", back_cb, style="danger")]])

    BUSY_TXT = "⚠️ <b>Provider is busy right now</b>\nLive rates could not be loaded. You were not charged — please try again in a moment."

    def primary_service_sid(cat, wanted):
        target = norm(wanted)
        # Exact name wins. This avoids showing a similarly named WhatsApp
        # provider entry with only one country when the real "WhatsApp"
        # service is available in the catalog.
        for sid, entry in cat.items():
            if norm(entry.get("name", "")) == target:
                return sid
        matches = [(sid, e) for sid, e in cat.items() if target in norm(e.get("name", ""))]
        return matches[0][0] if matches else None

    async def render_root(target):
        cat = await client.services()
        pop = popular_cached(cat)

        # Always pin the main Telegram + WhatsApp services, then fill the
        # remaining slots with the existing popular list (9 apps total).
        pinned = []
        seen = set()
        for label, wanted in (("📱 Telegram", "Telegram"), ("💬 WhatsApp", "WhatsApp")):
            sid = primary_service_sid(cat, wanted)
            if sid and sid not in seen:
                pinned.append((label, sid))
                seen.add(sid)
        for label, sid in pop:
            # Keep the dedicated/main WhatsApp button on the home screen,
            # but do not show the separate WhatsApp Business service there.
            # Users can still find it through Search All Services if it exists
            # in the provider catalog.
            entry = cat.get(sid, {}) or {}
            service_name = norm(entry.get("name", "") or label)
            if service_name in {"whatsapp business", "wa business", "whatsappbusiness"}:
                continue
            if sid not in seen:
                pinned.append((label, sid))
                seen.add(sid)
            if len(pinned) >= 9:
                break
        pinned = pinned[:9]

        text = (
            "❤️‍🔥 <b>VTH PANEL</b>"
            "<b> SMS &amp; Virtual Number Activation</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "<blockquote>⚡ <b>{n}+</b> apps &amp; platforms .\n"
            "🌍 Numbers from many countries, live stock &amp; prices .\n"
            "💯 <b>Auto refund</b> if no SMS arrives.\n"
            "🚀 OTP delivered straight into this chat.</blockquote>\n"
            "⭐ <b>Popular Services:</b>"
        ).format(n=f"{len(cat):,}")
        kb = InlineKeyboardBuilder()
        for label, sid in pinned:
            # Inline buttons cannot contain <tg-emoji> markup. Use the Bot API
            # custom-emoji icon field so these premium app icons render correctly.
            clean_label = re.sub(r"^[^A-Za-z0-9]+\s*", "", label).strip()
            kb.add(btn(
                clean_label,
                f"s3s:{sid}:0",
                icon_custom_emoji_id=app_custom_emoji_id(label),
            ))
        kb.adjust(2)
        kb.row(btn(f"🔎 Search All {len(cat):,} Services", "s3q", style="success"))
        kb.row(btn("📂 Browse All Services", "s3pg:0"), btn("ℹ️ Help", "s3help"))
        kb.row(btn("⬅️ Back", "back_main", style="danger"))
        await edit(target, text, kb.as_markup())

    async def open_service_direct(msg: Message, wanted: str):
        if is_maintenance_active():
            return await msg.answer(premiumize_text("⚠️ Server 3 is under maintenance. Please try again later!"), parse_mode="HTML")
        cat = await client.services()
        sid = primary_service_sid(cat, wanted)
        if not sid:
            return await msg.answer(premiumize_text(f"❌ {escape(wanted)} is not available on Server 3 right now."), parse_mode="HTML")
        try:
            await msg.delete()
        except Exception:
            pass
        screen = await msg.answer(premiumize_text(f"⏳ <i>Opening {escape(wanted)}…</i>"), parse_mode="HTML")
        key = (sid, True)
        if _cache_fresh(_s3_offer_cache.get(key), RATE_CACHE_TTL):
            await render_countries(screen, sid, 0, full=True, fast=True)
            return
        await edit(screen, f"⏳ <i>Fetching live rates for {escape(wanted)}…</i>")

        async def _load_direct_service():
            try:
                await render_countries(screen, sid, 0, full=True, fast=True)
            except Exception as e:
                print(f"[server3] direct {wanted} load error: {e!r}")
                try:
                    await edit(screen,
                               f"⚠️ <b>{escape(wanted)}</b> rates are temporarily unavailable.\n"
                               "Please tap Refresh to try again.",
                               busy_kb(f"s3r:{sid}", "s3_user_root"))
                except Exception:
                    pass
        spawn(_load_direct_service())

    @dp.message(Command("wp"))
    async def direct_whatsapp(msg: Message):
        await open_service_direct(msg, "WhatsApp")

    @dp.message(Command("tg"))
    async def direct_telegram(msg: Message):
        await open_service_direct(msg, "Telegram")

    @dp.message(Command("allapp"))
    async def direct_all_apps(msg: Message):
        if is_maintenance_active():
            return await msg.answer(premiumize_text("⚠️ Server 3 is under maintenance. Please try again later!"), parse_mode="HTML")
        try:
            await msg.delete()
        except Exception:
            pass
        screen = await msg.answer(premiumize_text("⏳ <i>Opening Server 3…</i>"), parse_mode="HTML")
        await render_root(screen)

    # "Buy WhatsApp Account" on the home screen -> terms page -> "Agree & Continue" lands here.
    # Opens the WhatsApp section of the All-apps OTP panel in place (no typing, no /wp needed).
    @scb(F.data == "s3_open_wp")
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
        if _cache_fresh(_s3_offer_cache.get((sid, True)), RATE_CACHE_TTL):
            return await render_countries(call, sid, 0, full=True, fast=True)
        await edit(call, f"⏳ <i>{_i18n.t('wp_opening')}</i>")

        async def _load_whatsapp():
            try:
                await render_countries(call, sid, 0, full=True, fast=True)
            except Exception as e:
                print(f"[server3] open WhatsApp load error: {e!r}")
                try:
                    await edit(call, "⚠️ " + _i18n.t("wp_rates_busy"), busy_kb("s3_open_wp", "back_main"))
                except Exception:
                    pass
        spawn(_load_whatsapp())

    @scb(F.data == "s3_user_root")
    async def user_root(call: CallbackQuery, state: FSMContext):
        await state.clear()
        if is_maintenance_active():
            return await call.answer("⚠️ Server 3 is under maintenance. Please try again later!", show_alert=True)
        await call.answer()
        await render_root(call)

    # legacy buttons from messages sent before the redesign -> just show the new menu
    @scb(F.data.startswith(("s3_usrsrv:", "s3_country:", "s3_buy_ask:", "s3_buy_terms:", "s3_search_prompt:")))
    async def legacy_redirect(call: CallbackQuery):
        await call.answer("Server 3 has been upgraded ✨", show_alert=False)
        await render_root(call)

    @scb(F.data == "s3help")
    async def s3_help(call: CallbackQuery):
        await call.answer()

        help_text = (
            "ℹ️ <b>About Server 3/ vth panel Virtual Number Activation</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"

            "<b>What Services Are Provided?</b>\n"
            "Server 3 / All apps acc connects directly to <b>VTH Panel</b> global carrier "
            "telecom routes, providing fresh, private, disposable mobile "
            "numbers across <b>200+ countries</b> to receive SMS "
            "verification codes &amp; OTPs.\n\n"

            "<b>Key Capabilities &amp; Highlights:</b>\n"

            "• 📱 <b>Telegram:</b> Seamlessly change your account number "
            "or register fresh Telegram accounts with clean foreign numbers.\n"

            "• 💬 <b>WhatsApp &amp; WA Business:</b> Instant verification "
            "codes for WhatsApp personal or business accounts.\n"

            "• 💡 <b>AI &amp; Cloud:</b> Verification support for OpenAI, "
            "ChatGPT, Claude, Google Cloud, AWS, Microsoft.\n"

            "• 🌐 <b>Social Networks:</b> Instagram, Threads, TikTok, "
            "Twitter / X, Discord, Facebook, Snapchat, Reddit.\n"

            "• 💰 <b>Shopping &amp; Finance:</b> Amazon, Netflix, Steam, "
            "Tinder, Uber, Binance, PayPal, Bybit, Web3Auth.\n\n"

            "🛡️ <b>100% Zero-Risk Money-Back Guarantee:</b>\n"
            "<blockquote>• Numbers are allocated for <b>5 minutes</b>.\n"
            "• If no SMS arrives within 5 minutes, the order is "
            "automatically cancelled and your wallet is <b>100% refunded</b>.\n"
            "• You can also cancel manually after <b>3 minutes</b> if needed.</blockquote>\n"
           
        )

        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [btn("⬅️ Back","s3_user_root",style="danger")]])

        await edit(call, help_text, kb)


    @scb(F.data == "s3n")
    async def s3_noop(call: CallbackQuery):
        await call.answer()

    # ---- browse all ----
    async def render_browse(target, page):
        cat = await client.services()
        items = sorted(cat.items(), key=lambda kv: kv[1]["name"].lower())
        pages = max(1, (len(items) + SERVICES_PER_PAGE - 1) // SERVICES_PER_PAGE)
        page = max(0, min(page, pages - 1))
        chunk = items[page * SERVICES_PER_PAGE:(page + 1) * SERVICES_PER_PAGE]
        text = (f"📂 <b>All Available Services ({len(items):,} total)</b>\n"
                f"<i>Page {page + 1} of {pages}</i>\nTap any service to see countries &amp; live pricing:")
        kb = InlineKeyboardBuilder()
        for sid, e in chunk:
            kb.button(text=e["name"][:28], callback_data=f"s3s:{sid}:0")
        kb.adjust(2)
        nav = []
        if page > 0:
            nav.append(btn("◀️ Prev", f"s3pg:{page - 1}"))
        nav.append(btn(f"{page + 1}/{pages}", "s3n"))
        if page < pages - 1:
            nav.append(btn("Next ▶️", f"s3pg:{page + 1}"))
        kb.row(*nav)
        kb.row(btn("🔎 Search", "s3q", style="success"), btn("⬅️ Back", "s3_user_root", style="danger"))
        await edit(target, text, kb.as_markup())

    @scb(F.data.startswith("s3pg:"))
    async def browse_cb(call: CallbackQuery):
        await call.answer()
        await render_browse(call, int(call.data.split(":")[1]))

    # ---- service search ----
    @scb(F.data == "s3q")
    async def search_prompt(call: CallbackQuery, state: FSMContext):
        await call.answer()
        await state.set_state(Server3UserState.waiting_service_search)
        await state.update_data(prompt_id=call.message.message_id, chat_id=call.message.chat.id)
        await edit(call,
                   "🔎 <b>Search Any Service</b>\n\nSend the name of the app you want to activate.\n"
                   "<i>Examples:</i> <code>Netflix</code>, <code>OpenAI</code>, <code>Binance</code>, "
                   "<code>Amazon</code>, <code>Uber</code>, <code>Apple</code>",
                   InlineKeyboardMarkup(inline_keyboard=[[btn("✖️ Cancel Search", "s3_user_root", style="danger")]]))

    @dp.message(StateFilter(Server3UserState.waiting_service_search), F.text)
    async def search_text(msg: Message, state: FSMContext):
        data = await state.get_data()
        q = (msg.text or "").strip()
        try:
            await msg.delete()  # keep the chat clean
        except Exception:
            pass
        cat = await client.services()
        results = search_services(cat, q)
        cancel_row = [btn("✖️ Cancel Search", "s3_user_root", style="danger")]
        if not results:
            text = (f"🔎 <b>Search Any Service</b>\n\n❌ Nothing found for <b>{escape(q[:40])}</b>.\n"
                    "Try another name:")
            kb = InlineKeyboardMarkup(inline_keyboard=[cancel_row])
        else:
            await state.clear()
            text = f"🔎 <b>Results for “{escape(q[:40])}”</b> — {len(results)} found\nTap a service:"
            b = InlineKeyboardBuilder()
            for sid, e in results[:20]:
                b.button(text=e["name"][:28], callback_data=f"s3s:{sid}:0")
            b.adjust(2)
            b.row(btn("🔎 Search Again", "s3q", style="success"), btn("⬅️ Back", "s3_user_root", style="danger"))
            kb = b.as_markup()
        try:
            await bot.edit_message_text(premiumize_text(text), chat_id=data["chat_id"], message_id=data["prompt_id"],
                                        reply_markup=kb, parse_mode="HTML")
        except TelegramBadRequest as e:
            if "not modified" not in str(e).lower():
                await msg.answer(text, reply_markup=kb, parse_mode="HTML")

    # ---- countries ----
    async def _warm_full(sid):
        """Background full-country scan so the 'All Countries' page is instant next time."""
        try:
            await cached_service_offers(sid, full=True)
        except Exception as e:
            print(f"[server3] full warm {sid} skipped: {e!r}")

    async def render_countries(target, sid, page=0, query=None, full=False, fast=False):
        cat = await client.services()
        svc = cat.get(sid)
        if not svc:
            return await render_root(target)
        title = escape(svc["name"])
        # fast=True (WhatsApp / direct opens): never make the user wait for the ~200-country scan.
        # Show the priority countries instantly and warm the full list in the background.
        if full and fast and not _cache_fresh(_s3_offer_cache.get((sid, True)), RATE_CACHE_TTL) \
                and not client.has_cached(f"offers:{sid}:1"):
            spawn(_warm_full(sid))
            full = False
        try:
            if query:
                rows = await asyncio.wait_for(country_rows_query(sid, query), 20)
            else:
                if not client.has_cached(f"offers:{sid}:{int(full)}"):   # only show the spinner when we really must wait
                    await edit(target, f"⏳ <i>Fetching live rates for {title}…</i>")
                # hard cap: the screen can never sit on "Fetching live rates" forever
                rows = await asyncio.wait_for(country_rows(sid, full=full), 25)
        except (ProviderUnavailable, asyncio.TimeoutError):
            return await edit(target, f"<b>Service: {title}</b>\n━━━━━━━━━━━━━━━━━━━━\n{BUSY_TXT}",
                              busy_kb(f"s3r:{sid}", "s3_user_root"))
        if not rows:
            rows_kb = []
            if query:
                rows_kb.append([btn("🔎 Search Again", f"s3cq:{sid}", style="success")])
            rows_kb.append([btn("⬅️ Back", f"s3s:{sid}:0" if query else "s3_user_root", style="danger")])
            msg = ("❌ No matching country in stock." if query
                   else "📭 <b>Out of stock</b> — no numbers for this service right now.")
            return await edit(target, f"<b>Service: {title}</b>\n━━━━━━━━━━━━━━━━━━━━\n{msg}",
                              InlineKeyboardMarkup(inline_keyboard=rows_kb))
        pages = max(1, (len(rows) + COUNTRIES_PER_PAGE - 1) // COUNTRIES_PER_PAGE)
        page = max(0, min(page, pages - 1))
        chunk = rows if query else rows[page * COUNTRIES_PER_PAGE:(page + 1) * COUNTRIES_PER_PAGE]
        text = (f"📦 <b>Service: {title}</b>\n━━━━━━━━━━━━━━━━━━━━\n"
                f"Select a country to view available virtual numbers &amp; live rates:\n"
                f"<i>{len(rows)} countries in stock</i>")
        if norm(svc["name"]) == "telegram":
            text = TELEGRAM_SERVICE_NOTE + "\n\n" + text
        pfx = "s3a" if full else "s3s"
        kb = InlineKeyboardBuilder()
        for r in chunk:
            # Country flags are rendered as Telegram custom-emoji entities in text,
            # but inline-button labels do not parse HTML/entities.  Use btn() so
            # the leading <tg-emoji> is converted to icon_custom_emoji_id and
            # the raw entity markup never reaches the Telegram button text.
            kb.add(btn(
                f"{r['flag']} {r['name'][:20]} · {short(r['min_price'])}",
                f"s3c:{sid}:{r['cid']}"
            ))
        kb.adjust(2)
        if not query and pages > 1:
            nav = []
            if page > 0:
                nav.append(btn("◀️", f"{pfx}:{sid}:{page - 1}"))
            nav.append(btn(f"{page + 1}/{pages}", "s3n"))
            if page < pages - 1:
                nav.append(btn("▶️", f"{pfx}:{sid}:{page + 1}"))
            kb.row(*nav)
        kb.row(btn("🔎 Search Country", f"s3cq:{sid}", style="success"), btn("🔄 Refresh", f"s3r:{sid}"))
        if not full and not query:
            kb.row(btn("🌍 All Countries", f"s3a:{sid}:0"))
        kb.row(btn("⬅️ Back", "s3_user_root", style="danger"))
        await edit(target, text, kb.as_markup())

    @scb(F.data.startswith("s3s:"))
    async def countries_cb(call: CallbackQuery, state: FSMContext):
        if is_maintenance_active():
            return await call.answer("⚠️ Server 3 is under maintenance.", show_alert=True)
        await state.clear()
        _, sid, page = call.data.split(":")
        await call.answer()
        await render_countries(call, sid, int(page))

    @scb(F.data.startswith("s3a:"))
    async def countries_full_cb(call: CallbackQuery):
        if is_maintenance_active():
            return await call.answer("⚠️ Server 3 is under maintenance.", show_alert=True)
        _, sid, page = call.data.split(":")
        await call.answer()
        await render_countries(call, sid, int(page), full=True)

    @scb(F.data.startswith("s3r:"))
    async def refresh_cb(call: CallbackQuery):
        sid = call.data.split(":")[1]
        drop_offer_cache(sid)
        await call.answer("🔄 Updated")
        await render_countries(call, sid, 0)

    @scb(F.data.startswith("s3cq:"))
    async def country_search_prompt(call: CallbackQuery, state: FSMContext):
        sid = call.data.split(":")[1]
        await call.answer()
        await state.set_state(Server3UserState.waiting_country_search)
        await state.update_data(sid=sid, prompt_id=call.message.message_id, chat_id=call.message.chat.id)
        await edit(call, "🔎 <b>Search Country</b>\n\nSend a country name (e.g. <code>India</code>, <code>Brazil</code>):",
                   InlineKeyboardMarkup(inline_keyboard=[[btn("✖️ Cancel", f"s3s:{sid}:0", style="danger")]]))

    @dp.message(StateFilter(Server3UserState.waiting_country_search), F.text)
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
            return await edit(target, BUSY_TXT, busy_kb(f"s3c:{sid}:{cid}", f"s3s:{sid}:0"))
        if not ops:
            return await render_countries(target, sid, 0)
        text = (f"📶 <b>Select Operator</b>\n━━━━━━━━━━━━━━━━━━━━\n"
                f"• <b>Service:</b> {escape(svc['name'])}\n"
                f"• <b>Country:</b> {flag_for(cname)} {escape(cname)}\n"
                "<blockquote>Prices &amp; stock are live. 🔥 = best price. If an operator runs out, you will be told and brought back here.</blockquote>")
        if note:
            text = f"{note}\n\n{text}"
        kb = InlineKeyboardBuilder()
        for i, o in enumerate(ops):
            stock = f"{o['count']}" if o["count"] < 1000 else "999+"
            best = "🔥 " if i == 0 and len(ops) > 1 else ""
            kb.row(btn(f"{best}{stock_dot(o['count'])} Operator {o['op']} · {short(o['price'])} · {stock} left",
                       f"s3o:{sid}:{cid}:{o['op']}"))
        kb.row(btn("🔄 Refresh", f"s3rc:{sid}:{cid}"), btn("🌍 Change Country", f"s3s:{sid}:0"))
        kb.row(btn("⬅️ Back", f"s3s:{sid}:0", style="danger"))
        await edit(target, text, kb.as_markup())

    @scb(F.data.startswith("s3c:"))
    async def operators_cb(call: CallbackQuery):
        _, sid, cid = call.data.split(":")
        await call.answer()
        if not co_ready(sid, cid):      # slow path: give instant feedback instead of a frozen screen
            try:
                await edit(call, "⏳ <b>Loading operators…</b>\n<i>Fetching live rates, one moment.</i>", None)
            except Exception:
                pass
        await render_operators(call, sid, cid)

    @scb(F.data.startswith("s3rc:"))
    async def operators_refresh_cb(call: CallbackQuery):
        _, sid, cid = call.data.split(":")
        client._cache.pop(f"co:{sid}:{cid}", None)
        clear_s3_rate_cache(sid, cid)
        await call.answer("🔄 Updated")
        await render_operators(call, sid, cid)

    # ---- confirm (does a LIVE stock check first) ----
    async def go_back_no_stock(call, sid, cid, op, toast=True):
        drop_offer_cache(sid)
        if toast:
            await call.answer(f"❌ Operator {op} has no numbers right now.", show_alert=False)
        await render_operators(call, sid, cid, note=f"⚠️ <b>Operator {op}</b> is out of stock.")

    @scb(F.data.startswith("s3o:"))
    async def confirm_cb(call: CallbackQuery):
        if is_maintenance_active():
            return await call.answer("⚠️ Server 3 is under maintenance.", show_alert=True)
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
            snapshot = _s3_operator_cache.get((sid, cid))
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
        price = price_for(sid, cname, op, live["cost"])
        bal = float((u or {}).get("balance", 0.0))
        enough = bal >= price
        text = (
            "🛒 <b>Virtual Number Activation</b>\n━━━━━━━━━━━━━━━━━━━━\n"
            "<blockquote>"
            f"• <b>Service:</b> {escape(cat[sid]['name'])}\n"
            f"• <b>Country:</b> {flag_for(cname)} {escape(cname)}\n"
            f"• <b>Operator:</b> Operator {op}\n"
            f"• <b>Available:</b> {live['count']} numbers\n"
            f"• <b>Price:</b> {_fmt(price)}"
            "</blockquote>\n"
            f"💳 <b>Your balance:</b> {_fmt(bal)}"
            + (f"  →  after: {_fmt(bal - price)}\n" if enough else f"  ❌ short by {_fmt(price - bal)}\n") +
            "\n⚠️ <b>Rules</b>\n"
            f"• Number is reserved for {OTP_TIMEOUT // 60} minutes to receive your OTP\n"
            "• 100% auto refund if no SMS arrives\n"
            "• Non-refundable once the verification code has arrived\n"
            f"• Cancel unlocks after {CANCEL_LOCK // 60} minutes without OTP"
        )
        buy_row = ([btn(f"✅ Purchase — {short(price)}", f"s3b:{sid}:{cid}:{op}:{int(price)}", style="success")] if enough
                   else [btn("💰 Recharge", "recharge", style="success")])
        kb = InlineKeyboardMarkup(inline_keyboard=[
            buy_row,
            [btn("🔁 Switch Operator", f"s3c:{sid}:{cid}"), btn("🌍 Change Country", f"s3s:{sid}:0")],
        ])
        await edit(call, text, kb)

    # ---- buy ----
    @scb(F.data.startswith("s3b:"))
    async def buy_cb(call: CallbackQuery):
        if is_maintenance_active():
            return await call.answer("⚠️ Server 3 is under maintenance.", show_alert=True)
        if is_spamming(call.from_user.id):
            return await call.answer("⏳ Please wait a few seconds…", show_alert=False)
        _, sid, cid, op, shown = call.data.split(":")
        shown = int(shown)
        uid = call.from_user.id

        # FINAL PRICE/STOCK GUARD: force a fresh TemporaSMS check only here.
        # This is the authoritative price used for the debit and provider order.
        live = await client.live_offer(sid, cid, op, max_age=LIVE_REUSE_TTL)
        if not live:
            clear_s3_rate_cache(sid, cid)
            return await go_back_no_stock(call, sid, cid, op)
        sync_s3_live_offer(sid, cid, op, live)
        cat = await client.services()
        if sid not in cat:
            return await render_root(call)
        svc_name = cat[sid]["name"]
        cname = (await client.countries()).get(cid, f"Country {cid}")
        price = price_for(sid, cname, op, live["cost"])
        if price > shown:
            await call.answer(f"Price changed to {short(price)}. Please confirm again.", show_alert=True)
            return await render_confirm(call, sid, cid, op)

        # atomic debit BEFORE calling the provider (no double-spend)
        res = await dbq(users_col.update_one, {"_id": uid, "balance": {"$gte": price}}, {"$inc": {"balance": -price}})
        if res.modified_count == 0:
            u = await dbq(users_col.find_one, {"_id": uid}) or {}
            need = price - float(u.get("balance", 0.0))
            kb = InlineKeyboardMarkup(inline_keyboard=[[btn("💰 Recharge", "recharge", style="success")],
                                                       [btn("⬅️ Back", f"s3c:{sid}:{cid}")]])
            await call.answer()
            return await edit(call, f"❌ <b>Insufficient balance</b>\nYou need <b>{_fmt(need)}</b> more.", kb)

        await loyalty.aspend(uid, price, "s3")      # VIP: counts as spend (refund below takes it back)
        # From here on the user has been charged: EVERY path that does not end in a running order refunds.
        outcome, status, reply, act = "error", "error", "", None
        try:
            try:
                await call.answer()
                await edit(call, "⏳ <i>Assigning your number…</i>")
            except Exception as e:      # a UI hiccup must never cost the user money
                print(f"[server3] pre-buy UI edit failed: {e!r}")
            max_price = round(live["cost"] * 1.10 + 0.01, 2)
            status, a, b = await client.get_number(live["code"], cid, op, max_price)
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
            print("[server3] purchase flow crashed")
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
                    await loyalty.arefund(uid, price, "s3")
                except Exception:
                    print(f"[server3] !!! REFUND FAILED for user {uid} amount {price}")
                    try:
                        await bot.send_message(ADMIN_LOG_CHANNEL,
                            f"🚨 <b>Server 3: REFUND FAILED</b> user <code>{uid}</code> amount ₹{price:g}", parse_mode="HTML")
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
            alert = "🚨 <b>Server 3: provider balance too low!</b>"
        elif reply == "NO_RESPONSE":
            alert = (f"⚠️ <b>Server 3:</b> getNumber timed out for user <code>{uid}</code> ({escape(svc_name)}, {escape(cname)}, op {op}). "
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
        filled = round(10 * left / OTP_TIMEOUT)
        bar = "▰" * filled + "▱" * (10 - filled)
        text = (
            "<blockquote>✅ <b>Number Reserved</b></blockquote>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"📞 <b>Number:</b> <code>+{task['phone']}</code>\n"
            f"📦 <b>Service:</b> {escape(task['service'])}\n"
            f"🌍 <b>Country:</b> {flag_for(task['country'])} {escape(task['country'])} · Operator {task['operator']}\n"
            f"💬 <b>OTP:</b> <code>Waiting for message…</code>\n"
            f"⏱ <b>Time left:</b> {left // 60}:{left % 60:02d}  {bar}\n\n"
            "<blockquote expandable><i>The OTP is delivered here automatically. "
            f"Cancel unlocks after {CANCEL_LOCK // 60} minutes without OTP; "
            f"after {OTP_TIMEOUT // 60} minutes the order is cancelled and refunded automatically.</i></blockquote>"
        )
        cancel_txt = (f"🔒 Cancel in {int(CANCEL_LOCK - elapsed)}s" if elapsed < CANCEL_LOCK else "🛑 Cancel & Refund")
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [btn("📋 Copy Number", copy_text=CopyTextButton(text=f"+{task['phone']}"))],
            [btn(cancel_txt, f"s3x:{task['activation_id']}", style="danger" if elapsed < CANCEL_LOCK else "success")],
        ])
        return text, kb

    async def safe_edit(task, text, kb=None):
        try:
            await bot.edit_message_text(premiumize_text(text), chat_id=task["chat_id"], message_id=task["msg_id"],
                                        reply_markup=kb, parse_mode="HTML")
        except Exception as e:
            if "not modified" not in str(e).lower():
                print(f"[server3] edit failed: {e}")

    def end_kb(task):
        return InlineKeyboardMarkup(inline_keyboard=[
            [btn("🔄 Buy Again", f"s3o:{task['sid']}:{task['country_id']}:{task['operator']}")],
            [btn("⬅️ Server 3 Menu", "s3_user_root")],
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
        await loyalty.arefund(task["user_id"], task["price"], "s3")
        await safe_edit(task, f"{reason_text}\n💰 <b>Refunded:</b> {_fmt(task['price'])}", end_kb(task))

    def mask_purchase_number(phone):
        """Show exactly the first half of the number and blur the rest in logs."""
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
        await dbq(orders_col.insert_one, {
            "user_id": task["user_id"], "number": task["phone"], "service": task["service"],
            "price": task["price"], "cost_inr": task.get("cost_inr"), "otp": otp,
            "server": "Server 3", "status": "purchased", "date": datetime.utcnow(),
        })
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [btn("📋 Copy OTP", copy_text=CopyTextButton(text=str(otp)))],
            [btn("🔄 Buy Again", f"s3o:{task['sid']}:{task['country_id']}:{task['operator']}"),
             btn("⬅️ Menu", "s3_user_root")],
        ])
        await safe_edit(task,
            "✅ <b>Order Completed</b>\n━━━━━━━━━━━━━━━━━━━━\n"
            f"📞 <b>Number:</b> <code>+{task['phone']}</code>\n"
            f"📦 <b>Service:</b> {escape(task['service'])}\n"
            f"🌍 <b>Country:</b> {flag_for(task['country'])} {escape(task['country'])} · Operator {task['operator']}\n"
            f"💬 <b>OTP:</b> <code>{escape(str(otp))}</code>", kb)
        # extra message after the OTP alert: security tips for WhatsApp accounts
        if norm(task.get("service", "")).startswith("whatsapp"):
            try:
                await bot.send_message(task["chat_id"], premiumize_text(WHATSAPP_SECURITY_TIPS), parse_mode="HTML")
            except Exception as e:
                print(f"[server3] whatsapp tips failed: {e}")
        try:
            u = await dbq(users_col.find_one, {"_id": task["user_id"]}) or {}
            uname = u.get("username") or f"User_{task['user_id']}"
            me = await bot.get_me()
            sales_kb = InlineKeyboardMarkup(inline_keyboard=[[btn(
                f"🛒 Buy {task['country']} Now",
                url=f"https://t.me/{me.username}?start=buy_s3_{task['sid']}_{task['country_id']}", style="success")]])
            masked_number = mask_purchase_number(task["phone"])
            country_display = f"{flag_for(task['country'])} {escape(task['country'])}"
            # Successful-purchase logs intentionally never expose the full phone number or OTP.
            sales_text = (
                "✅ <b>Successful Server 3 Purchase</b>\n━━━━━━━━━━━━━━━━━━━━\n"
                f"{_lf.role('application')} <b>Application:</b> {_lf.app_html(task['service'], app_custom_emoji_id(task['service']))}\n"
                f"{_lf.role('server')} <b>Server:</b> Server 3\n"
                f"{_lf.role('country')} <b>Country:</b> {country_display}\n"
                f"{_lf.role('number')} <b>Number:</b> <code>{escape(masked_number)}</code>\n"
                f"{_lf.role('sold')} <b>Sold For:</b> {_fmt(task['price'])}"
            )
            await bot.send_message(SALES_CHANNEL, premiumize_text(sales_text),
                                   reply_markup=sales_kb, parse_mode="HTML")
            profit = ""
            if task.get("cost_inr") is not None:
                pf = task["price"] - task["cost_inr"]
                profit = f"\n📈 <b>Estimated profit:</b> ₹{pf:.2f}"
            admin_text = (
                "📢 <b>Successful Server 3 Purchase</b>\n━━━━━━━━━━━━━━━━━━━━\n"
                f"👤 <b>User:</b> @{escape(uname)} (<code>{task['user_id']}</code>)\n"
                f"{_lf.role('application')} <b>Application:</b> {_lf.app_html(task['service'], app_custom_emoji_id(task['service']))}\n"
                f"{_lf.role('server')} <b>Server:</b> Server 3\n"
                f"{_lf.role('country')} <b>Country:</b> {country_display} · Op {escape(str(task['operator']))}\n"
                f"{_lf.role('number')} <b>Number:</b> <code>{escape(masked_number)}</code>\n"
                f"{_lf.role('sold')} <b>Sold For:</b> {_fmt(task['price'])}{profit}"
            )
            await bot.send_message(ADMIN_LOG_CHANNEL, premiumize_text(admin_text), parse_mode="HTML")
        except Exception as e:
            print(f"[server3] log dispatch failed: {e}")

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
                    f"⚠️ Server 3: could not confirm cancel for <code>{task['activation_id']}</code>; user was refunded.",
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
                print(f"[server3] worker error for {act}: {e!r}")
            await asyncio.sleep(POLL_EVERY)

    @scb(F.data.startswith("s3x:"))
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
        """Keeps the popular services' live rates fresh on the RATES key, so user clicks are instant
        and never wait on (or compete with) the purchase key."""
        await asyncio.sleep(1)
        while True:
            try:
                cat = await client.services()
                await client.countries()
                for _label, sid in popular_cached(cat)[:WARM_TOP]:
                    try:
                        rows, names = await cached_service_offers(sid, full=False, force=True)
                        # Keep the full-country snapshot in sync with the same provider refresh.
                        _s3_offer_cache[(sid, False)] = {"t": time.time(), "rows": rows, "names": names}
                    except ProviderUnavailable:
                        pass
            except Exception as e:
                print(f"[server3] rates warmer error: {e!r}")
            await asyncio.sleep(max(15, WARM_EVERY))

    async def start_background():
        await resume_active()
        if WARM_EVERY > 0:
            spawn(rates_loop())

    dp.startup.register(start_background)
    dp.shutdown.register(client.close)


# =================================================================
#     Deep link entry (used by bot.py: /start buy_s3_<sid>_<countryId>)
# =================================================================
def _fmt(x):
    from utils import fmt_curr  # viewer's chosen currency
    return fmt_curr(x)


async def send_s3_operator_menu_direct(target_msg: Message, service: str, country_name: str, fmt_curr_func=None):
    """service = service id ('tg'/'wa' legacy aliases accepted); country_name = provider country id or name."""
    cat = await client.services()
    sid = service
    if service in LEGACY_NAMES:
        sid = sid_of(LEGACY_NAMES[service])
    if sid not in cat:
        return await target_msg.answer("❌ This service is not available on Server 3 right now.")
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
        kb = InlineKeyboardMarkup(inline_keyboard=[[btn("📂 Choose another country", f"s3s:{sid}:0")]])
        return await target_msg.answer(f"❌ <b>{escape(country_name)}</b> is currently out of stock on Server 3.", reply_markup=kb)
    kb = InlineKeyboardMarkup(inline_keyboard=[[btn("📶 Choose operator", f"s3c:{sid}:{cid}", style="success")],
                                               [btn("⬅️ Server 3 Menu", "s3_user_root")]])
    await target_msg.answer(
        f"📦 <b>{escape(cat[sid]['name'])}</b> · {flag_for(names[cid])} {escape(names[cid])}\nTap below to see live operators &amp; prices.",
        reply_markup=kb)
