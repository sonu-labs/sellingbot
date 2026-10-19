"""All-app TemporaSMS storefront. Credentials and provider calls stay server-side."""
import asyncio
import json
import logging
import math
import re
import time
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_UP
from html import escape

import aiohttp
from bson import ObjectId
from bson.errors import InvalidId
from aiogram import F
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, CopyTextButton, Message
from rich_messages import safe_edit_or_send
import otp_settings as otp_cfg

BASE_URL = "https://api.temporasms.com/stubs/handler_api.php"
OPERATORS = ("1", "2", "3", "4", "7", "8", "9", "10")
POPULAR = ("telegram", "instagram", "facebook", "whatsapp", "google", "tiktok")
log = logging.getLogger(__name__)

# Automatic operator selection (provider docs).  ``operator`` is a numeric
# direct route only; these are sent as ``type=<value>`` and let the provider
# pick among the operators eligible for the submitted service + country.
AUTO_ROUTE_TYPES = otp_cfg.AUTO_ROUTE_TYPES
ROUTE_LABELS = {"smart": "🤖 Smart", "auto": "⚡ Auto", "cheap": "💰 Cheap", "best": "🏆 Best"}
ROUTE_DESCRIPTIONS = {
    "smart": "eligible operators for this app and country, picked from live delivery evidence",
    "auto": "lowest final price first, moving to the next operator if unavailable",
    "cheap": "only the single lowest-priced eligible operator",
    "best": "highest success-rate operator first, then the next if unavailable",
}

# Catalog presentation, matching the Server 2 / Server 3 country lists.
PER_PAGE = 20            # catalog items per page
GRID_COLUMNS = 2         # 10 rows x 2 columns: 10 left, 10 right
LABEL_CHARS = 30         # item button label length for the two-column grid
PAGER_ROW_WIDTH = 8      # numbered page buttons per keyboard row
PAGE_WINDOW = 7          # numbered buttons kept for very long catalogs
# Current-page marker.  "noop" is the bot-wide dead callback (answered with an
# empty answerCallbackQuery by the Server 3 router), same as Server 2/3 lists.
NOOP_CALLBACK = "noop"

# Catalogs change rarely: fetch once, keep it in memory *and* MongoDB, and
# reuse it instead of asking the provider on every screen.  The admin panel's
# "Refresh Catalog Cache" drops both copies immediately.
CATALOG_TTL = 3600
# Alternative-route probes for one app+country are remembered briefly so a
# known-dead combination is not re-probed on every tap.
ROUTE_PROBE_TTL = 300

# Storefront catalog search.  A plain string state is stored by aiogram's FSM
# per user/chat, so another user's message can never continue this session
# (the stored owner id is re-checked anyway).
SEARCH_STATE = "waiting_search"
SEARCH_MAX_QUERY = 80

# Provider tokens meaning "this selection is not routable".  A TemporaSMS
# service code is operator-specific — the same code can work on one line and
# be rejected on another — so these are answered with the routes that do have
# live stock instead of being shown to the buyer.  ``BAD_TYPE`` covers a
# deployment that does not accept the automatic-selection parameter.
ROUTING_ERROR_CODES = frozenset({"BAD_SERVICE", "BAD_OPERATOR", "BAD_COUNTRY", "BAD_TYPE"})

PROVIDER_ERROR_CODES = {
    "BAD_KEY", "USER_BANNED", "ERROR", "BAD_ACTION", "BAD_SERVICE", "BAD_COUNTRY",
    "BAD_OPERATOR", "BAD_TYPE", "BAD_STATUS", "BAD_MAX_PRICE", "WRONG_MAX_PRICE",
    "NO_BALANCE", "NO_NUMBERS", "NO_ACTIVATION", "SERVICE_BANNED",
    "EARLY_CANCEL_DENIED", "TOO_MANY_REQUESTS", "UNDER_DEVELOPMENT", "BAD_PRICE",
}



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
    apply at runtime without a restart.

    Catalog reads (``getServices``/``getCountries``/``getOperators``) are
    fetched once and reused: an in-memory copy plus a MongoDB copy so a
    restart does not re-download the whole catalog either.  Only pricing and
    allocation stay live.
    """

    def __init__(self, key, response_logger=None, catalog_col=None, catalog_ttl=CATALOG_TTL):
        self._key_source = key
        self.cache = {}
        self.route_cache = {}
        self.catalog_col = catalog_col
        self.catalog_ttl = catalog_ttl
        self.response_logger = response_logger

    def set_response_logger(self, callback):
        """Set an optional async observer for raw provider responses.

        The API key is deliberately never included in observer arguments.
        """
        self.response_logger = callback

    async def _observe(self, action, params, raw, status):
        if self.response_logger is None:
            return
        try:
            result = self.response_logger(action, params, raw, status)
            if hasattr(result, "__await__"):
                await result
        except Exception:
            # Diagnostics must never break provider operations.
            log.warning("OTP provider response logging failed")

    @property
    def key(self):
        source = self._key_source
        return source() if callable(source) else (source or "")

    @key.setter
    def key(self, value):
        self._key_source = value

    def clear_cache(self):
        """Drop every cached catalog and route probe (memory + MongoDB)."""
        self.cache.clear()
        self.route_cache.clear()
        if self.catalog_col is not None:
            try:
                self.catalog_col.delete_many({})
            except Exception:
                log.warning("OTP catalog cache cleanup failed")

    @staticmethod
    def _cache_key(action, params):
        query = json.dumps({k: str(v) for k, v in sorted((params or {}).items())}, sort_keys=True)
        return f"{action}:{query}"

    def _read_stored(self, key):
        """Fresh catalog copy from MongoDB, or ``None``."""
        if self.catalog_col is None:
            return None
        try:
            doc = self.catalog_col.find_one({"_id": key})
        except Exception:
            log.warning("OTP catalog cache read failed")
            return None
        if not doc or "data" not in doc:
            return None
        fetched = doc.get("fetched_at")
        if not isinstance(fetched, datetime):
            return None
        if datetime.utcnow() - fetched > timedelta(seconds=self.catalog_ttl):
            return None
        return doc["data"]

    def _write_stored(self, key, action, params, data):
        if self.catalog_col is None:
            return
        try:
            self.catalog_col.update_one({"_id": key}, {"$set": {
                "action": action,
                "params": {k: str(v) for k, v in (params or {}).items()},
                "data": data,
                "fetched_at": datetime.utcnow(),
            }}, upsert=True)
        except Exception:
            # A cache write must never break a catalog read.
            log.warning("OTP catalog cache write failed")

    def cached_routes(self, service, country):
        """Recently probed route results for one app+country, or ``None``."""
        entry = self.route_cache.get((str(service), str(country)))
        if entry and time.monotonic() - entry[0] < ROUTE_PROBE_TTL:
            return entry[1]
        return None

    def remember_routes(self, service, country, results):
        self.route_cache[(str(service), str(country))] = (time.monotonic(), dict(results))

    async def call(self, action, **params):
        key = self.key
        if not key:
            raise ProviderError("OTP service is not configured yet. Please contact support.", code="NOT_CONFIGURED")
        request_params = {"api_key": key, "action": action,
                          **{k: str(v) for k, v in params.items() if v is not None}}
        try:
            # TemporaSMS documents handler_api.php as a GET endpoint. Using
            # query parameters (rather than POST form data) is required by
            # their gateway and matches the published examples.
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25)) as session:
                async with session.get(BASE_URL, params=request_params) as response:
                    raw = (await response.text()).strip()
                    status = response.status
        except (aiohttp.ClientError, asyncio.TimeoutError):
            # Never log request URLs, exceptions or secrets.
            raise ProviderError("Provider connection interrupted. Please check My OTP Orders before retrying.", code="CONNECTION") from None
        safe_params = {k: str(v) for k, v in params.items() if v is not None}
        await self._observe(action, safe_params, raw, status)
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            data = raw
        error_code = data if isinstance(data, str) else (
            data.get("error") or data.get("code") if isinstance(data, dict) else None
        )
        if isinstance(error_code, str) and error_code in PROVIDER_ERROR_CODES:
            messages = {
                "NO_NUMBERS": "Out of stock. Try another country or operator.",
                "TOO_MANY_REQUESTS": "Provider is busy. Please wait before retrying.",
                "NO_BALANCE": "Provider account has insufficient balance.",
                "BAD_KEY": "Provider rejected the API key. Check that the active key is correct.",
            }
            raise ProviderError(messages.get(error_code, "Provider unavailable for this selection. Please try later or contact support."), code=error_code)
        if status < 200 or status >= 300:
            raise ProviderError("Provider returned an HTTP error. Check the raw response in the admin log.", code="HTTP_ERROR")
        return data

    async def get_balance(self):
        """Provider account balance (also validates the configured key)."""
        return parse_balance(await self.call("getBalance"))

    async def catalog(self, action, **params):
        """Catalog read: memory → MongoDB → network (then stored in both).

        Services, countries and operators change rarely, so the storefront
        reuses one fetch for an hour instead of calling the provider on every
        screen; the admin panel can force a refetch at any time.
        """
        key = self._cache_key(action, params)
        saved = self.cache.get(key)
        if saved and time.monotonic() - saved[0] < self.catalog_ttl:
            return saved[1]
        stored = self._read_stored(key)
        if stored is not None:
            self.cache[key] = (time.monotonic(), stored)
            return stored
        data = await self.call(action, **params)
        self.cache[key] = (time.monotonic(), data)
        self._write_stored(key, action, params, data)
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


_OPERATOR_ENVELOPES = ("operators", "data", "result", "response", "items")
_OPERATOR_ID_FIELDS = ("id", "operator", "operator_id", "operatorId", "code", "value")
_OPERATOR_LABEL = re.compile(r"^(?:operator|op|line)?[\s:_#-]*(\d{1,6})$", re.IGNORECASE)


def _operator_id(value, *, allow_label=False):
    """Return a normalized numeric operator ID string, or ``None``.

    Non-numeric values, booleans, zero/negative numbers and malformed
    floating-point values are rejected so a provider error token can never
    be mistaken for a routable operator.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        number = value
    elif isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            return None
        number = int(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.isdigit():
            number = int(text)
        elif allow_label:
            match = _OPERATOR_LABEL.fullmatch(text)
            if not match:
                return None
            number = int(match.group(1))
        else:
            return None
    else:
        return None
    return str(number) if 1 <= number <= 999999 else None


def parse_operator_ids(data, _depth=0):
    """Extract numeric operator IDs from any live ``getOperators`` payload.

    TemporaSMS (and compatible gateways) may answer with any of these shapes:

    * label -> ID maps: ``{"Operator 1": "1"}``
    * ID -> label maps: ``{"1": "Operator 1"}``
    * ID/label maps with object values: ``{"1": {"name": "..."}}``
    * envelopes: ``{"operators": ...}``/``{"data": ...}``/``{"result": ...}``
    * lists of IDs, labels ("Operator 1") or ``{"id": ...}`` objects
    * JSON-encoded versions of all of the above

    Only positive numeric IDs are returned (sorted numerically). Anything
    else — including provider errors such as ``BAD_OPERATOR`` — yields an
    empty tuple so callers fail closed instead of sending a bad operator.
    """
    if _depth > 5:
        return ()
    if isinstance(data, (bytes, bytearray)):
        try:
            data = bytes(data).decode("utf-8")
        except (UnicodeDecodeError, ValueError):
            return ()
    if isinstance(data, str):
        text = data.strip()
        if not text:
            return ()
        if text[0] in "[{":
            try:
                return parse_operator_ids(json.loads(text), _depth + 1)
            except (TypeError, ValueError):
                return ()
        # A few deployments return a bare comma/semicolon/pipe separated list.
        ids = set()
        for token in re.split(r"[,;|]", text):
            operator = _operator_id(token, allow_label=True)
            if operator:
                ids.add(operator)
        return tuple(sorted(ids, key=int))
    if isinstance(data, dict):
        for envelope in _OPERATOR_ENVELOPES:
            if envelope in data:
                nested = parse_operator_ids(data[envelope], _depth + 1)
                if nested:
                    return nested
        ids = set()
        for key, value in data.items():
            key_id = _operator_id(key)
            if key_id:
                ids.add(key_id)
            if isinstance(value, dict):
                for field in _OPERATOR_ID_FIELDS:
                    if field in value:
                        field_id = _operator_id(value[field])
                        if field_id:
                            ids.add(field_id)
                        break
                else:
                    ids.update(parse_operator_ids(value, _depth + 1))
                    label_id = _operator_id(key, allow_label=True)
                    if label_id:
                        ids.add(label_id)
            elif isinstance(value, (list, tuple, set)):
                ids.update(parse_operator_ids(value, _depth + 1))
            else:
                value_id = _operator_id(value)
                if value_id:
                    ids.add(value_id)
        return tuple(sorted(ids, key=int))
    if isinstance(data, (list, tuple, set)):
        ids = set()
        for entry in data:
            if isinstance(entry, dict):
                field_id = None
                for field in _OPERATOR_ID_FIELDS:
                    if field in entry:
                        field_id = _operator_id(entry[field])
                        break
                if field_id:
                    ids.add(field_id)
                else:
                    ids.update(parse_operator_ids(entry, _depth + 1))
                    label_id = _operator_id(entry.get("name"), allow_label=True)
                    if label_id:
                        ids.add(label_id)
            elif isinstance(entry, (list, tuple, set)):
                ids.update(parse_operator_ids(entry, _depth + 1))
            else:
                entry_id = _operator_id(entry, allow_label=True)
                if entry_id:
                    ids.add(entry_id)
        return tuple(sorted(ids, key=int))
    return ()


def available_operators(data, enabled_operators):
    """Live numeric provider operators intersected with admin-enabled ones."""
    live = set(parse_operator_ids(data))
    available = []
    for raw in enabled_operators or ():
        operator = _operator_id(raw)
        if operator and operator in live and operator not in available:
            available.append(operator)
    return tuple(available)


def pick_operator(data, enabled_operators):
    """Pick the first admin-enabled operator present in a live reply."""
    operators = available_operators(data, enabled_operators)
    if not operators:
        raise ProviderError(
            "No enabled numeric operator is currently offered by the provider. "
            "Please retry later or ask the admin to enable an available operator.",
            code="NO_OPERATORS",
        )
    return operators[0]


async def fetch_services_catalog(api, enabled_operators):
    """Load services using an operator from the live ``getOperators`` reply.

    TemporaSMS answers ``BAD_OPERATOR`` to ``getServices`` without a numeric
    operator, so every storefront/admin catalog load goes through this helper.
    Returns ``(catalog_items, operator)``.
    """
    operator = pick_operator(await api.catalog("getOperators"), enabled_operators)
    items = catalog_items(await api.catalog("getServices", operator=operator), "services")
    return items, operator


def price_options(data, country, service):
    """Normalize TemporaSMS V1/V2/V3 live-price response shapes.

    V3 provider options can expose ``price`` as a list and the routable
    public ID as ``providerIds``; older responses use price-keyed stock maps.
    Prefer provider-specific V3 options so purchases can pass providerIds.
    """
    node = data.get(str(country), {}).get(str(service), {}) if isinstance(data, dict) else {}
    if not isinstance(node, dict):
        return []

    def build(cost, count, provider=None):
        try:
            cost, count = float(cost), int(count)
            if math.isfinite(cost) and cost > 0 and count > 0:
                return {"cost": cost, "stock": count, "provider": provider}
        except (ValueError, TypeError, OverflowError):
            pass
        return None

    options = []
    providers = node.get("providers")
    if isinstance(providers, dict):
        for provider_key, entry in providers.items():
            if not isinstance(entry, dict):
                continue
            ids = entry.get("providerIds") or provider_key
            provider_ids = ",".join(str(x) for x in ids) if isinstance(ids, (list, tuple)) else str(ids)
            prices = entry.get("price", entry.get("cost"))
            if not isinstance(prices, (list, tuple)):
                prices = [prices]
            for cost in prices:
                option = build(cost, entry.get("count", entry.get("stock", 0)), provider_ids)
                if option:
                    options.append(option)
        if options:
            return sorted(options, key=lambda item: item["cost"])

    # V3 without provider breakdown, and V2 object entries.
    cost = node.get("price", node.get("cost"))
    prices = cost if isinstance(cost, (list, tuple)) else [cost]
    for value in prices:
        option = build(value, node.get("count", node.get("stock", 0)))
        if option:
            options.append(option)

    # Legacy getPrices/getPricesV2 shape: {"2.99": "467"}.
    if not options:
        for price, stock in node.items():
            if price == "providers":
                continue
            option = build(price, stock)
            if option:
                options.append(option)
    return sorted(options, key=lambda item: item["cost"])


def retail_price(cost, rate, markup):
    return float((Decimal(str(cost)) * Decimal(str(rate)) * (1 + Decimal(str(markup)) / 100)).quantize(Decimal("0.01"), rounding=ROUND_UP))


def positive_cost(value):
    """Finite provider cost above zero, or ``None``.

    A purchase never proceeds without one: it is the value sent as
    ``maxPrice``, the cap that keeps the provider charge at or below the price
    the buyer confirmed.
    """
    try:
        cost = float(value)
    except (TypeError, ValueError):
        return None
    return cost if math.isfinite(cost) and cost > 0 else None


def normalize_route(raw):
    """Canonical route value (numeric operator or automatic type) or ``None``."""
    return otp_cfg.normalize_route(raw)


def is_auto_route(route):
    """True for the documented automatic-selection values (``type=<value>``)."""
    return otp_cfg.is_auto_route(route)


def route_label(route):
    """Buyer-facing label: ``Operator 9`` for a line, ``⚡ Auto`` for routing."""
    route = str(route)
    return ROUTE_LABELS.get(route.lower(), route.title()) if is_auto_route(route) else f"Operator {route}"


def route_params(route):
    """Provider parameters for a route.

    ``operator`` is a numeric direct route only; automatic selection is sent
    as ``type``.  Never both — an invalid combination is rejected outright.
    """
    route = str(route)
    return {"type": route.lower()} if is_auto_route(route) else {"operator": route}


def route_enabled(route, eff):
    """Is this route still offered? Numeric lines must also be a known route."""
    if is_auto_route(route):
        return route in eff["auto_types"]
    return route in OPERATORS and route in eff["operators"]


def catalog_label(name, limit=LABEL_CHARS, byte_limit=64):
    """Short button label for the two-column catalog grid.

    Telegram rejects button text over 64 *bytes*, so multi-byte names are
    trimmed by bytes and never mid-character.
    """
    text = str(name or "").strip()[:limit]
    while text and len(text.encode("utf-8")) > byte_limit:
        text = text[:-1]
    return text or "…"


def normalize_search(raw):
    """Trim and cap a user-supplied catalog query (max ``SEARCH_MAX_QUERY``)."""
    text = str(raw or "").strip()
    return text[:SEARCH_MAX_QUERY].strip() if text else ""


def filter_catalog(items, query, *, numeric_codes=False):
    """Return ``[(index, code, name)]`` for items matching a catalog query.

    Matching is casefolded on the display name **or** the code; country lists
    (``numeric_codes=True``) additionally match a numeric query against the
    numeric country code.  Indices point into the *unfiltered* catalog so the
    stored snapshot and its ``otp:select`` callbacks stay valid.
    """
    needle = normalize_search(query).casefold()
    if not needle:
        return [(index, code, name) for index, (code, name) in enumerate(items)]
    digits = "".join(char for char in needle if char.isdigit())
    matches = []
    for index, (code, name) in enumerate(items):
        code_text, name_text = str(code).casefold(), str(name).casefold()
        hit = needle in name_text or needle in code_text
        if not hit and numeric_codes and digits and code_text.isdigit():
            hit = code_text == digits or code_text.startswith(digits)
        if hit:
            matches.append((index, code, name))
    return matches


def page_numbers(page, pages, window=PAGE_WINDOW):
    """Page indices for the numbered pager; ``None`` marks a gap (rendered …).

    Short catalogs list every page; long ones keep a window around the current
    page plus the first and last page.
    """
    if pages <= 1:
        return []
    if pages <= window + 2:
        return list(range(pages))
    half = max(1, window // 2)
    wanted = {0, pages - 1, page}
    for offset in range(1, half + 1):
        wanted.update({page - offset, page + offset})
    out = []
    for index in sorted(wanted):
        if not 0 <= index < pages:
            continue
        if out and index > out[-1] + 1:
            out.append(None)
        out.append(index)
    return out


def pager_buttons(token, page, pages, *, prefix="otp:page", noop=NOOP_CALLBACK):
    """``[(label, callback_data)]`` pager: current page is a noop, rest jump."""
    out = []
    for entry in page_numbers(page, pages):
        if entry is None:
            out.append(("…", noop))
        elif entry == page:
            out.append((f"• {entry + 1} •", noop))
        else:
            out.append((str(entry + 1), f"{prefix}:{token}:{entry}"))
    return out


def page_arg(raw):
    """Tolerant page argument parse (malformed callbacks fall back to 0)."""
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 0


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
                    "operators": OPERATORS, "auto_types": AUTO_ROUTE_TYPES,
                    "routes": AUTO_ROUTE_TYPES + OPERATORS, "version": 0}
        return otp_cfg.effective_settings(settings_col, env_key, env_markup, rate)

    def require_purchase_open(eff):
        if not eff["enabled"]:
            raise ProviderError("OTP service is temporarily disabled by the admin. Existing orders remain available in My OTP Orders.")

    catalog_col = db["otp_catalog"]
    api = TemporaClient(lambda: current()["api_key"] if settings_col is not None else env_key,
                        catalog_col=catalog_col)
    views, orders = db["otp_views"], db["otp_orders"]
    locks = {}  # Serialize check/cancel/purchase per user within this worker.

    def button(text, data):
        return InlineKeyboardButton(text=text, callback_data=data)

    def view(uid, **data):
        return str(views.insert_one({"user_id": uid, "created_at": datetime.utcnow(), **data}).inserted_id)

    def keyboard(rows):
        return InlineKeyboardMarkup(inline_keyboard=rows)

    async def show(target_message, text, rows):
        await safe_edit_or_send(bot, target_message, text, reply_markup=keyboard(rows))

    def load_view(raw_id, uid):
        """Owned navigation/quote snapshot, or None when expired/invalid.

        ``ObjectId`` raises ``InvalidId`` for a malformed or truncated
        callback payload; that is an expired selection, never a crash.
        """
        try:
            return views.find_one({"_id": ObjectId(raw_id), "user_id": uid})
        except (InvalidId, TypeError):
            return None

    def load_order(raw_id, uid):
        """Owned order document, or None when the id is unknown/malformed."""
        try:
            return orders.find_one({"_id": ObjectId(raw_id), "user_id": uid})
        except (InvalidId, TypeError):
            return None

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
            if result in ("ACCESS_CANCEL", "ACCESS_CANCEL_ALREADY", "STATUS_CANCEL"):
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

    async def render_order(target_message, order):
        oid = str(order["_id"])
        route = normalize_route(order.get("route") or order.get("route_type") or order.get("operator"))
        text = (f"<b>📲 OTP Order</b>\n\nApp: {escape(order['service_name'])}\nCountry: {escape(order['country_name'])}\n"
                f"Price: ₹{order['price']:.2f}\nStatus: <b>{escape(order['status'])}</b>\n")
        if route:
            text += f"Line: {escape(route_label(route))}\n"
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
        await show(target_message, text, rows)

    async def country_catalog(route):
        """Country list for a route.

        ``operator`` is a numeric direct route only, so automatic routing uses
        the shared country list — with the numeric catalog operator as a
        fallback for deployments that reject a bare ``getCountries``.
        """
        if not is_auto_route(route):
            return await api.catalog("getCountries", operator=route)
        try:
            data = await api.catalog("getCountries")
        except ProviderError:
            data = None
        if data is not None and catalog_items(data, "countries"):
            return data
        operator = pick_operator(await api.catalog("getOperators"), current()["operators"])
        return await api.catalog("getCountries", operator=operator)

    async def listing(target_message, uid, kind, context=None, page=0, search=None):
        """Render one catalog page (apps or countries), optionally filtered.

        20 items per page as a two-column grid (10 left, 10 right — like the
        Server 2 / Server 3 lists), with a numbered pager (current page marked
        ``• n •`` and wired to a noop callback), ⬅️/➡️ step buttons, and a
        search button whose query survives page changes.
        """
        context = context or {}
        eff = current()
        if kind == "services":
            # TemporaSMS requires a numeric operator on getServices; it is
            # resolved from the live getOperators reply and admin toggles,
            # then reused from the catalog cache for the next hour.
            items, _operator = await fetch_services_catalog(api, eff["operators"])
            # Admin popular ordering / hidden apps (falls back to the
            # historic built-in ordering when nothing is configured).
            items = otp_cfg.sort_services(items, eff["raw"])
            title, numeric_codes = "📲 Buy OTPs (All Apps)", False
        else:
            items = catalog_items(await country_catalog(context.get("route") or context.get("operator")), "countries")
            title, numeric_codes = f"🌍 Select Country · {escape(context['service_name'])}", True
        if not items:
            raise ProviderError("No catalog available from provider. Please try later.")
        query = normalize_search(search)
        matches = filter_catalog(items, query, numeric_codes=numeric_codes)
        pages = max(1, (len(matches) + PER_PAGE - 1) // PER_PAGE)
        page = max(0, min(page_arg(page), pages - 1))
        token = view(uid, kind=kind, context=context, items=items, page=page, search=query)
        page_items = matches[page * PER_PAGE:(page + 1) * PER_PAGE]
        rows = []
        for start in range(0, len(page_items), GRID_COLUMNS):
            rows.append([button(catalog_label(name), f"otp:select:{token}:{index}")
                         for index, _code, name in page_items[start:start + GRID_COLUMNS]])
        if matches:
            pager = pager_buttons(token, page, pages)
            for start in range(0, len(pager), PAGER_ROW_WIDTH):
                rows.append([button(label, data) for label, data in pager[start:start + PAGER_ROW_WIDTH]])
            step = []
            if page:
                step.append(button("⬅️", f"otp:page:{token}:{page - 1}"))
            if page + 1 < pages:
                step.append(button("➡️", f"otp:page:{token}:{page + 1}"))
            if step:
                rows.append(step)
            summary = ((f"Search: <code>{escape(query)}</code> · " if query else "")
                       + f"{len(matches)} available · Page {page + 1}/{pages}\n"
                         "Choose below. Legitimate use only; follow each platform's rules.")
        else:
            summary = (f"Search: <code>{escape(query)}</code> · No matches\n"
                       "Nothing in this list matched that query. Try a shorter word, the "
                       "exact code, or clear the search and browse the full list again.")
        search_row = [button("🔍 Search", f"otp:search:{token}")]
        if query:
            search_row.append(button("✖️ Clear Search", f"otp:searchclear:{token}"))
        rows.append(search_row)
        rows.append([button("📦 My OTP Orders", "otp:history"), button("⬅️ All Apps", "otp:open")])
        rows.append([button("🏠 Main Menu", "back_main")])
        await show(target_message, f"<b>{title}</b>\n\n{summary}", rows)

    async def probe_route_stock(service, country, routes):
        """Live-price a selection on several routes at once.

        Returns ``[(route, best_option)]`` for the routes that actually have
        positive stock, in the caller's order.  Results (including rejections)
        are remembered for ``ROUTE_PROBE_TTL`` so a known-dead app/country
        combination is not re-probed on every tap.  Each probe goes through
        the shared client, so the admin response log still receives the exact
        raw provider body for every call (never the API key).
        """
        wanted = []
        for raw in routes:
            route = normalize_route(raw)
            if route and route not in wanted:
                wanted.append(route)
        if not wanted:
            return []
        known = api.cached_routes(service, country) or {}
        missing = [route for route in wanted if route not in known]

        async def probe(route):
            try:
                data = await api.call("getPricesV3", country=country, service=service,
                                      **route_params(route))
            except ProviderError:
                return route, None
            except Exception:
                # One bad probe must not abort the others inside gather().
                log.warning("OTP route probe failed for route %s (details suppressed)", route)
                return route, None
            options = price_options(data, country, service)
            return route, (options[0] if options else None)

        if missing:
            known = {**known, **dict(await asyncio.gather(*(probe(route) for route in missing)))}
            api.remember_routes(service, country, known)
        return [(route, known[route]) for route in wanted if known.get(route)]

    async def show_route_alternatives(target_message, uid, ctx, eff, listing_doc=None):
        """Answer a routing rejection with the routes that have live stock.

        The raw provider token is never shown.  When another enabled route has
        live stock the buyer gets one button per line and the normal quote
        after re-pricing; otherwise this is a plain, actionable message.  No
        order is created and no wallet debit happens here.
        """
        app, country_name = str(ctx["service_name"]), str(ctx["country_name"])
        failing = normalize_route(ctx.get("route") or ctx.get("operator")) or str(ctx.get("route"))
        candidates = [route for route in eff["routes"] if route != failing]
        working = await probe_route_stock(ctx["service"], ctx["country"], candidates)
        # Automatic routes usually price identically (they differ only in how
        # the provider picks the operator), so keep the first of each distinct
        # price/stock: real choices for the buyer, no duplicate buttons.
        chosen, seen = [], set()
        for route, option in working:
            if is_auto_route(route):
                marker = (round(float(option["cost"]), 6), option["stock"])
                if marker in seen:
                    continue
                seen.add(marker)
            chosen.append((route, option))
        back = [[button("⬅️ Countries", f"otp:page:{listing_doc['_id']}:{page_arg(listing_doc.get('page'))}")]] \
            if listing_doc is not None else []
        if not chosen:
            rows = back + [[button("📲 All Apps", "otp:open"), button("📦 My OTP Orders", "otp:history")],
                           [button("🏠 Main Menu", "back_main")]]
            return await show(target_message,
                              f"{escape(app)} is not available on {escape(route_label(failing))} for "
                              f"{escape(country_name)}. Try another app, operator or country.", rows)
        # The token carries the selection only; pricing always happens live.
        token = view(uid, service=ctx["service"], service_name=app,
                     country=ctx["country"], country_name=country_name)
        lines = "\n".join(f"• {route_label(route)} — {option['stock']} in stock"
                          for route, option in chosen)
        rows = []
        for start in range(0, len(chosen), GRID_COLUMNS):
            rows.append([button(route_label(route), f"otp:alt:{token}:{route}")
                         for route, _option in chosen[start:start + GRID_COLUMNS]])
        rows += back + [[button("📲 All Apps", "otp:open"), button("📦 My OTP Orders", "otp:history")]]
        await show(target_message,
                   f"<b>{escape(app)} · {escape(route_label(failing))} unavailable</b>\n\n"
                   f"{escape(app)} is not available on {escape(route_label(failing))} for "
                   f"{escape(country_name)} right now. These lines have live stock — tap one "
                   f"to see the current price:\n{lines}", rows)

    async def show_quote(target_message, uid, ctx, eff, listing_doc=None):
        """Live-price a selection and render the standard confirmation screen."""
        route = ctx["route"]
        try:
            data = await api.call("getPricesV3", country=ctx["country"],
                                  service=ctx["service"], **route_params(route))
        except ProviderError as exc:
            if (exc.code or "") not in ROUTING_ERROR_CODES:
                raise
            return await show_route_alternatives(target_message, uid, ctx, eff, listing_doc)
        options = price_options(data, ctx["country"], ctx["service"])
        if not options:
            raise ProviderError("No live stock for this country/operator. Try another selection.")
        option = options[0]
        price = retail_price(option["cost"], eff["rate"], eff["markup"])
        auto = is_auto_route(route)
        line = f"Line: {escape(route_label(route))}" + (
            f"\n<i>{escape(ROUTE_DESCRIPTIONS.get(route, ''))}</i>" if auto else "")
        # Stamp the quote with the settings it was priced under; any
        # admin change bumps the version and forces a fresh quote.  Fields are
        # listed explicitly (not ``**ctx``) so a navigation snapshot that still
        # carries the legacy ``operator`` key can never collide with it.
        token = view(uid, service=ctx["service"], service_name=ctx["service_name"],
                     country=ctx["country"], country_name=ctx["country_name"],
                     route=route, operator=None if auto else route,
                     route_type=route if auto else None,
                     cost=option["cost"], provider=option["provider"], price=price,
                     settings_version=eff["version"], markup=eff["markup"], rate=eff["rate"])
        await show(target_message, f"<b>Confirm OTP Purchase</b>\n\nApp: {escape(ctx['service_name'])}\nCountry: {escape(ctx['country_name'])}\n{line}\nStock: {option['stock']}\n<b>Wallet charge: ₹{price:.2f}</b>\n\nQuote valid for 2 minutes. Provider cost is capped at ${option['cost']} (maxPrice), so you are never charged beyond this quote. No OTP? Cancel for a refund once confirmed by provider.", [[button(f"✅ Buy · ₹{price:.2f}", f"otp:buy:{token}")], [button("⬅️ Apps", "otp:open")]])

    async def validate_purchase(doc, route, quoted_cost, eff):
        """Live check at the moment of purchase — before any money moves.

        Returns ``(live_cost, provider_ids)``.  Raises instead of charging when
        the selection is no longer routable, the stock is gone, or the provider
        price rose above the quote (the buyer must confirm a new price).  The
        allocation afterwards still sends ``maxPrice = quoted_cost``, so the
        provider can never cut more than the buyer paid for.
        """
        app, country_name = str(doc.get("service_name")), str(doc.get("country_name"))
        try:
            data = await api.call("getPricesV3", country=doc["country"],
                                  service=doc["service"], **route_params(route))
        except ProviderError as exc:
            if (exc.code or "") in ROUTING_ERROR_CODES:
                raise ProviderError(f"{app} is not available on {route_label(route)} for "
                                    f"{country_name} right now. Nothing was charged — try another "
                                    "app, operator or country.") from None
            raise
        options = price_options(data, doc["country"], doc["service"])
        if not options:
            raise ProviderError("Out of stock right now. Nothing was charged — try another "
                                "country or operator.")
        option = options[0]
        if option["cost"] > quoted_cost + 1e-9:
            new_price = retail_price(option["cost"], eff["rate"], eff["markup"])
            raise ProviderError(f"The provider price just changed: {app} on {route_label(route)} "
                                f"is now ₹{new_price:.2f}. Nothing was charged — please select the "
                                "country again to confirm the new price.")
        return option["cost"], option["provider"] or doc.get("provider")

    @dp.callback_query(F.data.startswith("otp:"))
    async def handler(cq, state: FSMContext):
        await cq.answer()
        uid = cq.from_user.id
        # Any button navigation ends a pending search prompt; only the search
        # action sets the FSM state again (same rule as the admin panel).
        await state.clear()
        async with locks.setdefault(uid, asyncio.Lock()):
            try:
                await dispatch(cq, state)
            except ProviderError as exc:
                await show(cq.message, escape(str(exc)), [[button("📦 My OTP Orders", "otp:history"), button("🔄 All Apps", "otp:open")]])
            except Exception:
                log.error("OTP action failed for user %s (details suppressed to protect credentials)", uid)
                await show(cq.message, "Unable to process this action. Check My OTP Orders before retrying, or contact support.", [[button("📦 My OTP Orders", "otp:history")]])

    @dp.message(StateFilter(SEARCH_STATE))
    async def on_search_input(message: Message, state: FSMContext):
        """Filter the stored catalog snapshot — for the session owner only."""
        uid = message.from_user.id
        data = await state.get_data()
        token = data.get("otp_search_token")
        if data.get("otp_search_uid") != uid or not token:
            # FSM state is stored per user/chat, and the owner id is checked
            # again here: another user's text never drives this session.
            return
        await state.clear()
        async with locks.setdefault(uid, asyncio.Lock()):
            try:
                # Same gate as the buttons: a service switched off mid-search
                # must not keep serving catalog pages from a pending prompt.
                require_purchase_open(current())
                raw = (message.text or "").strip()
                if not raw or raw.startswith("/"):
                    return await message.answer("Search cancelled. Tap 🔍 Search to start again.")
                doc = load_view(token, uid)
                if not doc or doc.get("kind") not in ("services", "countries"):
                    return await message.answer("Search session expired. Please reopen All Apps and search again.")
                query = normalize_search(raw)
                if not query:
                    return await message.answer("Send a word or code to filter the list.")
                prompt = await message.answer("🔎 Searching the catalog…")
                await listing(prompt, uid, doc["kind"], doc.get("context") or {}, page=0, search=query)
            except ProviderError as exc:
                await message.answer(escape(str(exc)))
            except Exception:
                log.error("OTP catalog search failed for user %s (details suppressed)", uid)
                await message.answer("Search failed. Please try again.")

    async def dispatch(cq, state):
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
            return await listing(cq.message, uid, "services")
        if action == "history":
            page = page_arg(parts[2]) if len(parts) > 2 else 0
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
            return await show(cq.message, "<b>📦 My OTP Orders</b>\nSelect an order to view its number, OTP or refund status.", rows)
        if action in ("check", "cancel"):
            order = load_order(parts[2], uid) if len(parts) > 2 else None
            if not order:
                raise ProviderError("Order not found.")
            await refresh(order, action == "cancel")
            return await render_order(cq.message, orders.find_one({"_id": order["_id"]}))
        if len(parts) < 3:
            raise ProviderError("Selection expired. Please reopen All Apps.")
        doc = load_view(parts[2], uid)
        if not doc:
            raise ProviderError("Selection expired. Please reopen All Apps.")
        if action == "page":
            # The active search query lives on the snapshot, so paging keeps it.
            return await listing(cq.message, uid, doc["kind"], doc.get("context") or {},
                                 page_arg(parts[3]) if len(parts) > 3 else 0, search=doc.get("search"))
        if action == "search":
            if doc.get("kind") not in ("services", "countries"):
                raise ProviderError("Selection expired. Please reopen All Apps.")
            hint = ("Send the app name or its code, e.g. <code>telegram</code> or <code>tss</code>."
                    if doc["kind"] == "services" else
                    "Send the country name or its numeric code, e.g. <code>India</code> or <code>22</code>.")
            await cq.message.answer(
                f"<b>🔍 Search {'apps' if doc['kind'] == 'services' else 'countries'}</b>\n\n{hint}\n\n"
                f"Up to {SEARCH_MAX_QUERY} characters — send it as your next message in this chat.",
                parse_mode="HTML",
                reply_markup=keyboard([[button("✖️ Cancel", f"otp:searchclear:{doc['_id']}")]]))
            # FSM state is stored for this user/chat only; the owner id is
            # stored as well and re-checked when the text arrives.
            await state.update_data(otp_search_token=str(doc["_id"]), otp_search_uid=uid)
            await state.set_state(SEARCH_STATE)
            return
        if action == "searchclear":
            if doc.get("kind") not in ("services", "countries"):
                raise ProviderError("Selection expired. Please reopen All Apps.")
            await state.clear()
            return await listing(cq.message, uid, doc["kind"], doc.get("context") or {},
                                 page_arg(doc.get("page")))
        if action == "select":
            try:
                index = int(parts[3])
            except (TypeError, ValueError):
                index = -1
            if not 0 <= index < len(doc.get("items") or []):
                raise ProviderError("Selection expired. Please reopen All Apps.")
            code, name = doc["items"][index]
            if doc["kind"] == "services":
                if code in otp_cfg.hidden_codes(eff["raw"]):
                    raise ProviderError("This app is currently unavailable. Please choose another app.")
                # Automatic routing types first (the provider picks the best
                # eligible operator), then the numeric direct routes that the
                # live getOperators reply and the admin toggles allow.
                routes = list(eff["auto_types"])
                routes += [op for op in available_operators(await api.catalog("getOperators"),
                                                            eff["operators"]) if op not in routes]
                if not routes:
                    raise ProviderError("No supported operators are currently available. Refresh the provider catalog or try later.")
                token = view(uid, service=code, service_name=name)
                hints = "\n".join(f"• <b>{escape(route_label(route))}</b> — {escape(ROUTE_DESCRIPTIONS[route])}"
                                  for route in routes if route in ROUTE_DESCRIPTIONS)
                text = f"<b>{escape(name)} · Select Line</b>\n"
                if hints:
                    text += (f"\n{hints}\n\n<i>Automatic routing picks the operator for this app "
                             "and country; you always confirm the price before anything is charged.</i>\n")
                text += ("\nOperator buttons are direct numeric routes. Live country availability "
                         "and stock depend on the selected line.")
                rows = []
                for start in range(0, len(routes), GRID_COLUMNS):
                    rows.append([button(route_label(route), f"otp:operator:{token}:{route}")
                                 for route in routes[start:start + GRID_COLUMNS]])
                rows.append([button("⬅️ Apps", "otp:open")])
                return await show(cq.message, text, rows)
            route = normalize_route(doc["context"].get("route") or doc["context"].get("operator"))
            if not route or not route_enabled(route, eff):
                raise ProviderError("This operator is currently unavailable. Please choose another operator.")
            ctx = {**doc["context"], "route": route, "country": code, "country_name": name}
            return await show_quote(cq.message, uid, ctx, eff, listing_doc=doc)
        if action == "operator":
            route = normalize_route(parts[3] if len(parts) > 3 else "")
            if not route or not route_enabled(route, eff):
                raise ProviderError("This operator is currently unavailable. Please choose another operator.")
            # The token must be an app selection; a listing/quote token (stale
            # or replayed button) is an expired selection, never a crash.
            if not all(doc.get(field) for field in ("service", "service_name")):
                raise ProviderError("Selection expired. Please reopen All Apps.")
            return await listing(cq.message, uid, "countries", {"service": doc["service"],
                                                                "service_name": doc["service_name"],
                                                                "route": route})
        if action == "alt":
            # An alternative line offered after a routing rejection: validated
            # like any route choice, then re-priced live before the quote.
            route = normalize_route(parts[3] if len(parts) > 3 else "")
            if not route or not route_enabled(route, eff):
                raise ProviderError("This operator is currently unavailable. Please choose another operator.")
            if not all(doc.get(field) for field in ("service", "service_name", "country", "country_name")):
                raise ProviderError("Selection expired. Please reopen All Apps.")
            ctx = {"service": doc["service"], "service_name": doc["service_name"],
                   "country": doc["country"], "country_name": doc["country_name"],
                   "route": route}
            return await show_quote(cq.message, uid, ctx, eff)
        if action == "buy":
            existing = orders.find_one({"_id": doc["_id"], "user_id": uid})
            if existing:
                return await render_order(cq.message, existing)
            if datetime.utcnow() - doc["created_at"] > timedelta(minutes=2):
                raise ProviderError("Quote expired. Please select your country again for current pricing.")
            route = normalize_route(doc.get("route") or doc.get("operator"))
            # Re-validate against *current* settings: a markup/key/operator
            # change (or service switch-off, checked above) since the quote
            # was shown must produce a fresh confirmation, never a charge
            # at a price the user did not just confirm.
            if int(doc.get("settings_version") or 0) != eff["version"] or not route or not route_enabled(route, eff):
                raise ProviderError("Pricing or availability just changed. Please select your country again to confirm the current price.")
            quoted_cost = positive_cost(doc.get("cost"))
            if quoted_cost is None or abs(retail_price(quoted_cost, eff["rate"], eff["markup"]) - float(doc["price"])) > 0.001:
                raise ProviderError("Pricing just changed. Please select your country again to confirm the current price.")
            # LIVE validation at the moment of purchase, before any money
            # moves: the catalog is cached, so this single provider call is
            # what decides whether the buy may proceed.
            live_cost, provider_ids = await validate_purchase(doc, route, quoted_cost, eff)
            # Persist intent BEFORE touching money or allocating a paid number.
            order = {k: doc.get(k) for k in ("service", "service_name", "country", "country_name",
                                             "price", "cost", "provider")}
            order.update(_id=doc["_id"], user_id=uid, status="allocating", created_at=datetime.utcnow(),
                         updated_at=datetime.utcnow(), markup=eff["markup"], rate=eff["rate"],
                         route=route, operator=None if is_auto_route(route) else route,
                         route_type=route if is_auto_route(route) else None, live_cost=live_cost)
            if provider_ids:
                order["provider"] = provider_ids
            orders.insert_one(order)
            oid = str(order["_id"])
            charged = users_col.update_one({"_id": uid, "balance": {"$gte": order["price"]}, "otp_debits": {"$ne": oid}}, {"$inc": {"balance": -order["price"]}, "$addToSet": {"otp_debits": oid}})
            if not charged.modified_count:
                orders.update_one({"_id": order["_id"]}, {"$set": {"status": "unpaid", "updated_at": datetime.utcnow()}})
                raise ProviderError("Insufficient wallet balance. Recharge and create a new quote.")
            # maxPrice is mandatory on every allocation and equals the cost the
            # retail price was computed from, so the provider can never charge
            # more than the buyer paid for.  ``operator`` is sent only for a
            # numeric direct route; automatic routing is sent as ``type``.
            params = {"service": order["service"], "country": order["country"],
                      **route_params(route), "maxPrice": order["cost"]}
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
            return await render_order(cq.message, orders.find_one({"_id": order["_id"]}))

    async def warm_catalog():
        """Fetch the catalog once at startup so the first tap needs no API call.

        Best-effort: a missing key or an unreachable provider only skips the
        warm-up, and the cached copy is reused for ``CATALOG_TTL`` afterwards.
        """
        try:
            eff = current()
            if not eff["api_key"] or not eff["enabled"]:
                return
            _items, operator = await fetch_services_catalog(api, eff["operators"])
            await api.catalog("getCountries", operator=operator)
            # Automatic routing reads the country list without an operator.
            await api.catalog("getCountries")
            log.info("OTP catalog cache warmed")
        except Exception:
            log.warning("OTP catalog warm-up skipped (details suppressed)")

    async def worker():
        # Persistent orders survive restarts; polling is not attached to a message.
        await warm_catalog()
        while True:
            try:
                views.create_index("created_at", expireAfterSeconds=86400)
                orders.create_index([("user_id", 1), ("created_at", -1)])
                orders.create_index([("status", 1), ("created_at", -1)])
                catalog_col.create_index("fetched_at", expireAfterSeconds=86400)
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
