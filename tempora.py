"""TemporaSMS client and catalog/pricing helpers for allapps.py and server4.py."""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import time
from decimal import Decimal, ROUND_UP

import aiohttp

from otp import catalog_items, parse_operator_ids, price_options

try:
    from config import TEMPORASMS_API_KEY, USD_TO_INR
except Exception:
    TEMPORASMS_API_KEY = os.getenv("TEMPORASMS_API_KEY", "")
    USD_TO_INR = float(os.getenv("USD_TO_INR", "100"))

DEFAULT_BASE_URL = "https://api.temporasms.com/stubs/handler_api.php"
DEFAULT_OPERATORS = ("1", "2", "3", "4", "7", "8", "9", "10", "15", "16")

CATALOG_TTL = 3600
COUNTRIES_TTL = 3600
OFFER_TTL = 300
COUNTRY_OFFER_TTL = 180
NEGATIVE_TTL = 1800
PRICE_CORRECTION_TTL = 600


class ProviderUnavailable(Exception):
    """Raised when TemporaSMS cannot serve a catalog/rate request right now."""

    def __init__(self, message: str = "Provider is temporarily unavailable.", code: str | None = None,
                 reported_price: float | None = None):
        super().__init__(message)
        self.code = code
        self.reported_price = reported_price


PRIORITY_COUNTRIES = [
    "united states", "usa", "india", "indonesia", "philippines", "vietnam",
    "canada", "ukraine", "kazakhstan", "argentina", "iran", "brazil",
    "united kingdom", "england", "russia", "nigeria", "turkey", "pakistan",
    "bangladesh", "mexico", "south africa", "colombia", "malaysia", "thailand",
]

LEGACY_NAMES = {
    "tg": "Telegram",
    "wa": "WhatsApp",
    "ig": "Instagram",
    "fb": "Facebook",
    "go": "Google",
    "tt": "TikTok",
}

# Ordered home-screen popular candidates matching APP_CUSTOM_EMOJI_IDS in allapps.py:
# (display label, exact normalized service names, fallback prefix/word tokens)
_POPULAR_SPECS = (
    ("Telegram", ("telegram", "telegram messenger"), ("telegram",)),
    ("WhatsApp", ("whatsapp", "whatsapp messenger"), ("whatsapp",)),
    ("Google / Gmail", ("google", "gmail", "google gmail", "google youtube", "googlegmail"), ("google", "gmail")),
    ("Instagram", ("instagram", "instagram threads"), ("instagram",)),
    ("ChatGPT / OpenAI", ("openai", "chatgpt", "openai chatgpt", "chatgpt openai"), ("openai", "chatgpt")),
    ("Twitter / X", ("twitter", "x", "twitter x"), ("twitter",)),
    ("Facebook", ("facebook", "facebook meta"), ("facebook",)),
    ("TikTok", ("tiktok", "tiktok douyin"), ("tiktok",)),
    ("Discord", ("discord",), ("discord",)),
    ("Amazon", ("amazon",), ("amazon",)),
    ("Netflix", ("netflix",), ("netflix",)),
    ("Snapchat", ("snapchat",), ("snapchat",)),
    ("Microsoft", ("microsoft", "outlook", "hotmail"), ("microsoft",)),
    ("Apple", ("apple", "icloud"), ("apple",)),
    ("Uber", ("uber",), ("uber",)),
    ("PayPal", ("paypal",), ("paypal",)),
    ("Spotify", ("spotify",), ("spotify",)),
    ("Steam", ("steam",), ("steam",)),
    ("Reddit", ("reddit",), ("reddit",)),
    ("Tinder", ("tinder",), ("tinder",)),
)

_ROUTING_ERRORS = frozenset({"BAD_OPERATOR", "BAD_SERVICE", "BAD_COUNTRY", "BAD_TYPE", "NO_NUMBERS"})
_FATAL_ERRORS = frozenset({"BAD_KEY", "USER_BANNED", "NOT_CONFIGURED"})
_TRANSIENT_ERRORS = frozenset({"CONNECTION", "TOO_MANY_REQUESTS", "HTTP_ERROR", "ERROR", "NO_RESPONSE"})


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).strip()


_NAME_TO_LEGACY = {norm(v): k for k, v in LEGACY_NAMES.items()}
_EXTRA_LEGACY_SYNONYMS = {
    "telegram messenger": "tg",
    "whatsapp messenger": "wa",
}


def sid_of(name: str) -> str:
    """Compact deterministic service ID safe for callbacks (<=64B) and deep links."""
    n = norm(name)
    if not n:
        return "svc"
    if n in LEGACY_NAMES:
        return n
    if n in _NAME_TO_LEGACY:
        return _NAME_TO_LEGACY[n]
    if n in _EXTRA_LEGACY_SYNONYMS:
        return _EXTRA_LEGACY_SYNONYMS[n]
    compact = n.replace(" ", "")
    if len(compact) <= 24:
        return compact
    digest = hashlib.sha1(n.encode("utf-8")).hexdigest()[:6]
    return f"{compact[:18]}{digest}"


def entry_name(entry, fallback: str = "") -> str:
    if isinstance(entry, dict):
        return str(entry.get("name") or entry.get("title") or entry.get("service") or fallback)
    if entry is None:
        return fallback
    return str(entry)


def normalize_service_entry(sid: str, entry) -> dict:
    """Guarantee every catalog value has shape ``{'name': str, 'ops': dict}``."""
    if isinstance(entry, dict):
        name = str(entry.get("name") or entry.get("title") or entry.get("service") or LEGACY_NAMES.get(sid, sid)).strip()
        raw_ops = entry.get("ops")
        ops = {str(k): str(v) for k, v in raw_ops.items() if k and v} if isinstance(raw_ops, dict) else {}
        if not ops and entry.get("code"):
            ops = {"1": str(entry["code"])}
        return {"name": name or sid, "ops": ops}
    name = str(entry or LEGACY_NAMES.get(sid, sid)).strip() or sid
    return {"name": name, "ops": {"1": str(sid)}}


def _extract_pricing_params(cfg) -> tuple[float, float, float]:
    if isinstance(cfg, dict):
        raw_rate = cfg.get("unit_to_inr", cfg.get("rate", USD_TO_INR))
        rate = float(raw_rate if raw_rate not in (None, "", 0) else USD_TO_INR)
        raw_markup = cfg.get("markup_pct", cfg.get("markup", 100.0))
        markup = float(raw_markup if raw_markup is not None else 100.0)
        raw_min = cfg.get("min_profit", 0.0)
        min_profit = max(0.0, float(raw_min or 0.0))
        return rate, markup, min_profit
    return float(USD_TO_INR), (float(cfg) if cfg is not None else 0.0), 0.0


def retail_price(cost, cfg) -> float:
    rate, markup, min_profit = _extract_pricing_params(cfg)
    base_inr = Decimal(str(cost)) * Decimal(str(rate))
    with_markup = base_inr * (Decimal("1") + Decimal(str(markup)) / Decimal("100"))
    with_floor = base_inr + Decimal(str(min_profit))
    final = max(with_markup, with_floor)
    return float(final.quantize(Decimal("0.01"), rounding=ROUND_UP))


def cost_inr(cost, cfg) -> float:
    rate, _markup, _min_profit = _extract_pricing_params(cfg)
    return float(Decimal(str(cost)) * Decimal(str(rate)))


def search_services(catalog: dict, query: str) -> list[tuple[str, dict]]:
    q = norm(query)
    if not q or not catalog:
        return []
    ranked: list[tuple[int, str, str, dict]] = []
    for sid, raw_entry in catalog.items():
        entry = normalize_service_entry(str(sid), raw_entry)
        name_norm = norm(entry["name"])
        sid_norm = norm(sid)
        codes_norm = {norm(c) for c in (entry.get("ops") or {}).values()}
        if q == name_norm or q == sid_norm or q in codes_norm:
            rank = 0
        elif name_norm.startswith(q) or sid_norm.startswith(q):
            rank = 1
        elif q in name_norm.split():
            rank = 2
        elif q in name_norm or q in sid_norm:
            rank = 3
        else:
            continue
        ranked.append((rank, name_norm, str(sid), entry))
    ranked.sort(key=lambda item: (item[0], len(item[1]), item[1]))
    return [(sid, entry) for _rank, _nn, sid, entry in ranked]


def popular_services(catalog: dict) -> list[tuple[str, str]]:
    """Return ``[(label, sid), ...]`` ordered for the All-Apps home screen."""
    if not catalog:
        return []

    normalized: dict[str, dict] = {
        str(sid): normalize_service_entry(str(sid), raw_entry)
        for sid, raw_entry in catalog.items()
    }
    by_norm_name: dict[str, str] = {}
    for sid, entry in normalized.items():
        nn = norm(entry["name"])
        if nn and nn not in by_norm_name:
            by_norm_name[nn] = sid

    out: list[tuple[str, str]] = []
    seen: set[str] = set()

    for label, exact_names, prefixes in _POPULAR_SPECS:
        matched_sid = None
        for cand in exact_names:
            if cand in by_norm_name:
                matched_sid = by_norm_name[cand]
                break
        if matched_sid is None:
            for sid, entry in normalized.items():
                nn = norm(entry["name"])
                if nn in {"whatsapp business", "wa business", "whatsappbusiness"}:
                    continue
                words = nn.split()
                if any(nn.startswith(pfx + " ") or (words and words[0] == pfx) for pfx in prefixes):
                    matched_sid = sid
                    break
        if matched_sid and matched_sid not in seen:
            out.append((label, matched_sid))
            seen.add(matched_sid)

    # Append remaining services alphabetically so callers always have enough items.
    remaining = sorted(normalized.items(), key=lambda kv: kv[1]["name"].lower())
    for sid, entry in remaining:
        if sid not in seen:
            out.append((entry["name"], sid))
            seen.add(sid)
    return out


def _extract_error_token(raw_or_data) -> tuple[str | None, float | None]:
    """Return ``(error_code, reported_price)`` if payload represents a provider error."""
    token = None
    if isinstance(raw_or_data, str):
        token = raw_or_data.strip()
    elif isinstance(raw_or_data, dict):
        err = raw_or_data.get("error") or raw_or_data.get("code") or raw_or_data.get("msg")
        if isinstance(err, str):
            token = err.strip()
    if not token:
        return None, None
    base = token.split(":", 1)[0].strip()
    known = {
        "BAD_KEY", "USER_BANNED", "ERROR", "BAD_ACTION", "BAD_SERVICE", "BAD_COUNTRY",
        "BAD_OPERATOR", "BAD_TYPE", "BAD_STATUS", "BAD_MAX_PRICE", "WRONG_MAX_PRICE",
        "NO_BALANCE", "NO_NUMBERS", "NO_ACTIVATION", "SERVICE_BANNED",
        "EARLY_CANCEL_DENIED", "TOO_MANY_REQUESTS", "UNDER_DEVELOPMENT", "BAD_PRICE",
    }
    if base not in known:
        return None, None
    reported = None
    if ":" in token:
        try:
            cand = float(token.split(":", 1)[1].strip())
            if math.isfinite(cand) and cand > 0:
                reported = cand
        except Exception:
            reported = None
    return base, reported


def _extract_country_service_options(data, cid: str, service_code: str) -> list[dict]:
    """Normalize single-country TemporaSMS V1/V2/V3 payloads into sorted ``[{cost, stock, provider}]``."""
    if not isinstance(data, dict) or not data:
        return []
    cid_s, svc_s = str(cid), str(service_code)

    if cid_s in data and isinstance(data[cid_s], dict):
        inner = data[cid_s]
        if svc_s in inner and isinstance(inner[svc_s], dict):
            wrapped = data
        elif any(k in inner for k in ("price", "cost", "providers", "count", "stock")):
            wrapped = {cid_s: {svc_s: inner}}
        else:
            # Possibly case-variant service key or flat price->stock map
            matched_key = next((k for k in inner if str(k).lower() == svc_s.lower()), None)
            if matched_key is not None and isinstance(inner[matched_key], dict):
                wrapped = {cid_s: {svc_s: inner[matched_key]}}
            else:
                wrapped = {cid_s: {svc_s: inner}}
    elif svc_s in data and isinstance(data[svc_s], dict):
        wrapped = {cid_s: {svc_s: data[svc_s]}}
    elif any(k in data for k in ("price", "cost", "providers")):
        wrapped = {cid_s: {svc_s: data}}
    else:
        return []

    return price_options(wrapped, cid_s, svc_s)


def _extract_bulk_service_rows(data, op: str, service_code: str) -> list[dict] | None:
    """Parse multi-country payloads from getPricesV3/getPrices/getTopCountriesByService.

    Returns ``None`` when ``data`` is not a recognizable multi-country payload,
    or a list of ``{country_id, operator, cost, count, code, provider_ids}`` rows.
    """
    svc_s = str(service_code)
    op_s = str(op)

    # Shape A: list of {"country": 12, "count": 150, "price": 0.37, ...} (getTopCountriesByService)
    if isinstance(data, list):
        rows = []
        for item in data:
            if not isinstance(item, dict):
                continue
            cid = item.get("country", item.get("country_id"))
            raw_cost = item.get("price", item.get("cost"))
            raw_cnt = item.get("count", item.get("stock", 0))
            if cid is None or raw_cost is None:
                continue
            opts = price_options({str(cid): {svc_s: {"price": raw_cost, "count": raw_cnt}}}, str(cid), svc_s)
            if opts:
                best = opts[0]
                rows.append({
                    "country_id": str(cid),
                    "operator": op_s,
                    "cost": float(best["cost"]),
                    "count": int(best["stock"]),
                    "code": svc_s,
                    "provider_ids": best.get("provider"),
                })
        return rows

    if not isinstance(data, dict) or not data:
        return None
    if any(k in data for k in ("error", "code")) and len(data) <= 3:
        return None

    rows = []
    recognized_entries = 0
    for key, val in data.items():
        if not isinstance(val, dict):
            continue
        # Shape B: getTopCountriesByService indexed dict {"0": {"country": 12, "count": 50, "price": 0.3}}
        if "country" in val and ("price" in val or "cost" in val):
            recognized_entries += 1
            cid = str(val["country"])
            opts = price_options(
                {cid: {svc_s: {"price": val.get("price", val.get("cost")), "count": val.get("count", val.get("stock", 0))}}},
                cid,
                svc_s,
            )
            if opts:
                best = opts[0]
                rows.append({
                    "country_id": cid,
                    "operator": op_s,
                    "cost": float(best["cost"]),
                    "count": int(best["stock"]),
                    "code": svc_s,
                    "provider_ids": best.get("provider"),
                })
            continue

        # Shape C: country-keyed map {"12": {"edg": {...}}, "22": {"edg": {...}}} or {"12": {"cost": ..., "count": ...}}
        cid = str(key)
        if not cid.isdigit():
            continue
        recognized_entries += 1
        opts = _extract_country_service_options({cid: val}, cid, svc_s)
        if opts:
            best = opts[0]
            rows.append({
                "country_id": cid,
                "operator": op_s,
                "cost": float(best["cost"]),
                "count": int(best["stock"]),
                "code": svc_s,
                "provider_ids": best.get("provider"),
            })

    return rows if recognized_entries > 0 else None


class TemporaClient:
    """Async TemporaSMS client with separate purchase/rates lanes and caching."""

    def __init__(self, api_key=None, base_url: str | None = None, rates_key=None, **kwargs):
        self.BASE_URL = base_url or DEFAULT_BASE_URL
        self._api_key_source = api_key
        self._rates_key_source = rates_key
        self.extra_services = kwargs.get("extra_services")

        # Shared cache dict exposed as both ``cache`` and ``_cache`` (allapps/server4 use ``_cache``).
        self.cache: dict = {}
        self._cache = self.cache
        self._neg_cache: dict[str, tuple[float, str]] = {}
        self._price_corrections: dict[tuple[str, str, str], tuple[float, float]] = {}
        self._bulk_unsupported: set[str] = set()

        self._sessions: dict[tuple[int, str], aiohttp.ClientSession] = {}
        self._sems: dict[tuple[int, str], asyncio.Semaphore] = {}
        self._last_seen_keys: tuple[str, str] | None = None

    # ------------------------------------------------------------------
    # Key & session management
    # ------------------------------------------------------------------
    def _resolve_key(self, lane: str = "purchase") -> str:
        src = self._rates_key_source if (lane == "rates" and self._rates_key_source is not None) else self._api_key_source
        val = src() if callable(src) else src
        key = str(val or "").strip()
        if not key:
            key = str(os.getenv("TEMPORASMS_API_KEY") or TEMPORASMS_API_KEY or "").strip()
        return key

    @property
    def key(self) -> str:
        return self._resolve_key("purchase")

    @property
    def separate_keys(self) -> bool:
        p = self._resolve_key("purchase")
        r = self._resolve_key("rates")
        return bool(p and r and p != r)

    def _sync_key_change(self) -> None:
        cur = (self._resolve_key("purchase"), self._resolve_key("rates"))
        if self._last_seen_keys is not None and self._last_seen_keys != cur:
            self.clear_cache()
        self._last_seen_keys = cur

    def _get_session_and_sem(self, lane: str) -> tuple[aiohttp.ClientSession, asyncio.Semaphore]:
        loop = asyncio.get_running_loop()
        loop_id = id(loop)
        bucket = "rates" if (lane == "rates" and self.separate_keys) else "purchase"
        skey = (loop_id, bucket)

        sess = self._sessions.get(skey)
        if sess is None or sess.closed:
            connector = aiohttp.TCPConnector(limit=24, ttl_dns_cache=300)
            timeout = aiohttp.ClientTimeout(total=18, connect=8)
            sess = aiohttp.ClientSession(connector=connector, timeout=timeout)
            self._sessions[skey] = sess

        sem = self._sems.get(skey)
        if sem is None:
            sem = asyncio.Semaphore(14 if bucket == "rates" else 8)
            self._sems[skey] = sem

        return sess, sem

    async def close(self, *args, **kwargs) -> None:
        sessions = list(self._sessions.values())
        self._sessions.clear()
        self._sems.clear()
        for sess in sessions:
            try:
                if sess and not sess.closed:
                    await sess.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Cache management
    # ------------------------------------------------------------------
    def has_cached(self, key: str, max_age: float = OFFER_TTL) -> bool:
        entry = self._cache.get(key)
        if entry is None:
            return False
        if isinstance(entry, tuple) and len(entry) == 2 and isinstance(entry[0], (int, float)):
            return (time.time() - float(entry[0])) < max_age
        return True

    def clear_cache(self) -> None:
        self._cache.clear()
        self._neg_cache.clear()
        self._price_corrections.clear()
        self._bulk_unsupported.clear()

    def invalidate_catalog(self) -> None:
        self.clear_cache()

    def drop_service(self, sid: str) -> None:
        if not sid:
            self.clear_cache()
            return
        prefixes = (f"offers:{sid}:", f"co:{sid}:", f"live:{sid}:")
        for k in list(self._cache.keys()):
            if isinstance(k, str) and k.startswith(prefixes):
                self._cache.pop(k, None)

    def _neg_get(self, key: str) -> str | None:
        item = self._neg_cache.get(key)
        if not item:
            return None
        ts, code = item
        if time.time() - ts < NEGATIVE_TTL:
            return code
        self._neg_cache.pop(key, None)
        return None

    def _neg_set(self, key: str, code: str) -> None:
        self._neg_cache[key] = (time.time(), code)

    def _remember_price_correction(self, code: str, cid: str, op: str, price: float) -> None:
        if math.isfinite(price) and price > 0:
            self._price_corrections[(str(code), str(cid), str(op))] = (time.time(), float(price))

    def _get_price_correction(self, code: str, cid: str, op: str) -> float | None:
        item = self._price_corrections.get((str(code), str(cid), str(op)))
        if not item:
            return None
        ts, val = item
        if time.time() - ts < PRICE_CORRECTION_TTL:
            return val
        self._price_corrections.pop((str(code), str(cid), str(op)), None)
        return None

    # ------------------------------------------------------------------
    # Low-level HTTP transport
    # ------------------------------------------------------------------
    async def _raw_request(self, action: str, *, lane: str = "rates", **params) -> str:
        self._sync_key_change()
        api_key = self._resolve_key(lane)
        if not api_key:
            raise ProviderUnavailable("TemporaSMS API key is not configured.", code="NOT_CONFIGURED")

        qparams = {"api_key": api_key, "action": str(action)}
        for k, v in params.items():
            if v is not None and not str(k).startswith("_"):
                qparams[str(k)] = str(v)

        sess, sem = self._get_session_and_sem(lane)
        async with sem:
            try:
                async with sess.get(self.BASE_URL, params=qparams) as resp:
                    raw = (await resp.text()).strip()
                    status = resp.status
            except (aiohttp.ClientError, asyncio.TimeoutError):
                return "NO_RESPONSE"

        if status < 200 or status >= 300:
            if not raw:
                return f"HTTP_{status}"
        return raw

    async def call(self, **kwargs) -> str:
        """Raw string provider call (used directly by Admin Probe UI and helpers)."""
        params = dict(kwargs)
        action = params.pop("action", None) or params.pop("a", None)
        lane = params.pop("_lane", "rates")
        if not action:
            return ""
        try:
            raw = await self._raw_request(str(action), lane=lane, **params)
        except ProviderUnavailable as exc:
            return exc.code or str(exc)
        return raw or "NO_RESPONSE"

    async def _json_request(self, action: str, *, lane: str = "rates", **params):
        """Perform an action and parse JSON, returning ``(data, err_code, reported_price)``."""
        raw = await self._raw_request(action, lane=lane, **params)
        if raw == "NO_RESPONSE" or raw.startswith("HTTP_"):
            return None, ("CONNECTION" if raw == "NO_RESPONSE" else "HTTP_ERROR"), None

        err_code, reported = _extract_error_token(raw)
        if err_code:
            return None, err_code, reported

        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return raw, None, None

        err_code, reported = _extract_error_token(parsed)
        if err_code:
            return None, err_code, reported
        return parsed, None, None

    # ------------------------------------------------------------------
    # Catalog & balance methods
    # ------------------------------------------------------------------
    async def balance(self) -> float | None:
        try:
            raw = await self._raw_request("getBalance", lane="purchase")
        except ProviderUnavailable:
            return None
        if isinstance(raw, str) and raw.startswith("ACCESS_BALANCE:"):
            try:
                val = float(raw.split(":", 1)[1].strip())
                return val if math.isfinite(val) else None
            except ValueError:
                return None
        try:
            data = json.loads(raw)
            if isinstance(data, dict) and "balance" in data:
                val = float(data["balance"])
                return val if math.isfinite(val) else None
        except Exception:
            pass
        return None

    async def _operators(self) -> tuple[str, ...]:
        if self.has_cached("operators_list", max_age=CATALOG_TTL):
            return self._cache["operators_list"][1]
        data, err, _ = await self._json_request("getOperators", lane="rates")
        if err in _FATAL_ERRORS:
            raise ProviderUnavailable(f"Provider rejected API key ({err}).", code=err)
        ops = parse_operator_ids(data) if data is not None else ()
        if not ops:
            ops = DEFAULT_OPERATORS
        self._cache["operators_list"] = (time.time(), ops)
        return ops

    def _merge_extra_services(self, base_cat: dict[str, dict]) -> dict[str, dict]:
        merged = {
            sid: {"name": info["name"], "ops": dict(info["ops"])}
            for sid, info in base_cat.items()
        }
        extra = {}
        if callable(self.extra_services):
            try:
                extra = self.extra_services() or {}
            except Exception:
                extra = {}
        elif isinstance(self.extra_services, dict):
            extra = self.extra_services

        for raw_sid, raw_entry in (extra or {}).items():
            norm_entry = normalize_service_entry(str(raw_sid), raw_entry)
            sid = str(raw_sid)
            if sid in merged:
                merged[sid]["ops"].update(norm_entry["ops"])
                if norm_entry["name"]:
                    merged[sid]["name"] = norm_entry["name"]
            else:
                merged[sid] = norm_entry
        return merged

    async def services(self) -> dict[str, dict]:
        """Return ``{sid: {'name': str, 'ops': {operator_id: service_code}}}``."""
        self._sync_key_change()
        if self.has_cached("services_cat", max_age=CATALOG_TTL):
            return self._merge_extra_services(self._cache["services_cat"][1])

        ops = await self._operators()

        async def _fetch_for_op(op: str | None):
            nkey = f"svc_op:{op or 'bare'}"
            cached_err = self._neg_get(nkey)
            if cached_err:
                return op, None, cached_err
            kw = {"operator": op} if op is not None else {}
            data, err, _ = await self._json_request("getServices", lane="rates", **kw)
            if err in _ROUTING_ERRORS:
                self._neg_set(nkey, err)
            return op, data, err

        results = await asyncio.gather(*[_fetch_for_op(op) for op in ops])

        cat: dict[str, dict] = {}
        fatal_err = None
        transient_errs = 0

        def _ingest(op_label: str, payload) -> int:
            items = catalog_items(payload, "services")
            for code, name in items:
                clean_name = str(name or "").strip()
                if not clean_name or not code:
                    continue
                sid = sid_of(clean_name)
                canonical_name = LEGACY_NAMES.get(sid, clean_name) if norm(clean_name) in _NAME_TO_LEGACY else clean_name
                entry = cat.setdefault(sid, {"name": canonical_name, "ops": {}})
                entry["ops"].setdefault(str(op_label), str(code))
            return len(items)

        for op, data, err in results:
            if err in _FATAL_ERRORS:
                fatal_err = err
                continue
            if err in _TRANSIENT_ERRORS:
                transient_errs += 1
                continue
            if data is not None:
                _ingest(str(op), data)

        # Fallback: try bare getServices if every operator-specific call was rejected/empty
        if not cat and fatal_err is None:
            _op, bare_data, bare_err = await _fetch_for_op(None)
            if bare_err in _FATAL_ERRORS:
                fatal_err = bare_err
            elif bare_err in _TRANSIENT_ERRORS:
                transient_errs += 1
            elif bare_data is not None:
                default_op = ops[0] if ops else "1"
                _ingest(default_op, bare_data)

        if not cat:
            if fatal_err:
                raise ProviderUnavailable(f"Provider rejected API key ({fatal_err}).", code=fatal_err)
            if transient_errs > 0:
                raise ProviderUnavailable("Provider is busy or unreachable right now.", code="CONNECTION")

        if cat:
            self._cache["services_cat"] = (time.time(), cat)
        return self._merge_extra_services(cat)

    async def countries(self) -> dict[str, str]:
        """Return ``{country_id_str: country_name_str}``."""
        self._sync_key_change()
        if self.has_cached("countries_map", max_age=COUNTRIES_TTL):
            return dict(self._cache["countries_map"][1])

        ops = await self._operators()
        candidates: list[str | None] = list(ops[:4]) + [None]
        merged: dict[str, str] = {}
        fatal_err = None
        transient_errs = 0

        for op in candidates:
            nkey = f"cnt_op:{op or 'bare'}"
            if self._neg_get(nkey):
                continue
            kw = {"operator": op} if op is not None else {}
            data, err, _ = await self._json_request("getCountries", lane="rates", **kw)
            if err in _FATAL_ERRORS:
                fatal_err = err
                break
            if err in _ROUTING_ERRORS:
                self._neg_set(nkey, err)
                continue
            if err in _TRANSIENT_ERRORS:
                transient_errs += 1
                continue
            items = catalog_items(data, "countries") if data is not None else []
            for cid, cname in items:
                merged.setdefault(str(cid), str(cname).strip())
            if merged:
                break

        if not merged:
            if fatal_err:
                raise ProviderUnavailable(f"Provider rejected API key ({fatal_err}).", code=fatal_err)
            if transient_errs > 0:
                raise ProviderUnavailable("Provider countries list is temporarily unavailable.", code="CONNECTION")

        if merged:
            self._cache["countries_map"] = (time.time(), merged)
        return dict(merged)

    # ------------------------------------------------------------------
    # Live rate & stock discovery
    # ------------------------------------------------------------------
    async def _fetch_single_op_offer(self, cid: str, op: str, service_code: str) -> tuple[dict | None, str | None]:
        """Fetch live price/stock for one ``(country, operator, service_code)``."""
        cid_s, op_s, svc_s = str(cid), str(op), str(service_code)
        bad_pair = self._neg_get(f"bad_svc_op:{op_s}:{svc_s}")
        if bad_pair:
            return None, bad_pair
        bad_combo = self._neg_get(f"bad_combo:{cid_s}:{op_s}:{svc_s}")
        if bad_combo:
            return None, bad_combo

        for action in ("getPricesV3", "getPricesV2", "getPrices"):
            if f"act:{action}" in self._bulk_unsupported:
                continue
            data, err, _ = await self._json_request(
                action, lane="rates", country=cid_s, service=svc_s, operator=op_s
            )
            if err == "BAD_ACTION":
                self._bulk_unsupported.add(f"act:{action}")
                continue
            if err in ("BAD_OPERATOR", "BAD_SERVICE"):
                self._neg_set(f"bad_svc_op:{op_s}:{svc_s}", err)
                return None, err
            if err in ("BAD_COUNTRY", "BAD_TYPE"):
                self._neg_set(f"bad_combo:{cid_s}:{op_s}:{svc_s}", err)
                return None, err
            if err == "NO_NUMBERS":
                return None, None
            if err:
                return None, err

            opts = _extract_country_service_options(data, cid_s, svc_s)
            if not opts:
                return None, None
            best = dict(opts[0])
            corr = self._get_price_correction(svc_s, cid_s, op_s)
            if corr and corr > best["cost"] + 1e-9:
                best["cost"] = corr
            return {
                "country_id": cid_s,
                "operator": op_s,
                "cost": float(best["cost"]),
                "count": int(best["stock"]),
                "code": svc_s,
                "provider_ids": best.get("provider"),
            }, None

        return None, None

    async def country_offers(self, sid: str, cid: str, force: bool = False) -> list[dict]:
        """Return ``[{country_id, operator, cost, count, code, ...}]`` for one service + country."""
        cid_s = str(cid)
        cache_key = f"co:{sid}:{cid_s}"
        if not force and self.has_cached(cache_key, max_age=COUNTRY_OFFER_TTL):
            return [dict(r) for r in self._cache[cache_key][1]]

        cat = await self.services()
        svc = cat.get(sid)
        if not svc:
            return []
        ops_map = dict(svc.get("ops") or {})
        if not ops_map:
            ops = await self._operators()
            ops_map = {op: sid for op in ops[:4]}

        probes = await asyncio.gather(
            *[self._fetch_single_op_offer(cid_s, op, code) for op, code in ops_map.items()]
        )

        rows: list[dict] = []
        transient_errs = 0
        fatal_err = None
        for row, err in probes:
            if row is not None:
                rows.append(row)
            elif err in _FATAL_ERRORS:
                fatal_err = err
            elif err in _TRANSIENT_ERRORS:
                transient_errs += 1

        if not rows:
            if fatal_err:
                raise ProviderUnavailable(f"Provider rejected API key ({fatal_err}).", code=fatal_err)
            if transient_errs > 0 and transient_errs >= len(probes):
                raise ProviderUnavailable("Provider is busy right now.", code="CONNECTION")

        rows.sort(key=lambda r: (r["cost"], -r["count"]))
        self._cache[cache_key] = (time.time(), rows)
        return [dict(r) for r in rows]

    async def _try_bulk_service_offers(self, ops_map: dict[str, str]) -> list[dict] | None:
        """Attempt 1-call-per-operator multi-country fetch if supported by deployment."""
        for bulk_action in ("getPricesV3", "getPrices", "getTopCountriesByService"):
            marker = f"bulk:{bulk_action}"
            if marker in self._bulk_unsupported:
                continue

            async def _call_op(op: str, code: str):
                data, err, _ = await self._json_request(
                    bulk_action, lane="rates", service=str(code), operator=str(op)
                )
                return op, code, data, err

            res = await asyncio.gather(*[_call_op(op, code) for op, code in ops_map.items()])

            action_rejected = 0
            parsed_any_payload = False
            collected: list[dict] = []
            fatal_err = None

            for op, code, data, err in res:
                if err in _FATAL_ERRORS:
                    fatal_err = err
                    continue
                if err in ("BAD_COUNTRY", "BAD_ACTION", "BAD_TYPE", "UNDER_DEVELOPMENT", "ERROR"):
                    action_rejected += 1
                    continue
                if data is not None:
                    extracted = _extract_bulk_service_rows(data, op, code)
                    if extracted is not None:
                        parsed_any_payload = True
                        for r in extracted:
                            corr = self._get_price_correction(r["code"], r["country_id"], r["operator"])
                            if corr and corr > r["cost"] + 1e-9:
                                r = dict(r, cost=corr)
                            collected.append(r)

            if fatal_err and not parsed_any_payload:
                raise ProviderUnavailable(f"Provider rejected API key ({fatal_err}).", code=fatal_err)

            if parsed_any_payload:
                return collected

            if action_rejected == len(res) and len(res) > 0:
                self._bulk_unsupported.add(marker)

        return None

    def _select_country_ids(self, names: dict[str, str], full: bool) -> list[str]:
        priority_rank: dict[str, int] = {name: idx for idx, name in enumerate(PRIORITY_COUNTRIES)}
        pri_items: list[tuple[int, str]] = []
        other_items: list[tuple[str, str]] = []

        for cid, cname in names.items():
            cleaned = norm(cname).replace(" virtual", "").strip()
            if cleaned in priority_rank:
                pri_items.append((priority_rank[cleaned], str(cid)))
            else:
                matched_idx = next(
                    (idx for idx, pname in enumerate(PRIORITY_COUNTRIES) if len(pname) >= 4 and pname in cleaned),
                    None,
                )
                if matched_idx is not None:
                    pri_items.append((matched_idx, str(cid)))
                else:
                    other_items.append((cleaned, str(cid)))

        pri_items.sort(key=lambda x: x[0])
        other_items.sort(key=lambda x: x[0])
        pri_cids = [cid for _, cid in pri_items]
        if not pri_cids:
            pri_cids = [cid for _, cid in other_items[:18]]
            other_cids = [cid for _, cid in other_items[18:]]
        else:
            other_cids = [cid for _, cid in other_items]

        return (pri_cids + other_cids) if full else pri_cids

    async def service_offers(self, sid: str, full: bool = False, force: bool = False) -> list[dict]:
        """Return ``[{country_id, operator, cost, count, code, ...}]`` across countries for ``sid``."""
        cache_key = f"offers:{sid}:{int(bool(full))}"
        if not force and self.has_cached(cache_key, max_age=OFFER_TTL):
            return [dict(r) for r in self._cache[cache_key][1]]

        cat = await self.services()
        svc = cat.get(sid)
        if not svc:
            return []
        ops_map = dict(svc.get("ops") or {})
        if not ops_map:
            return []

        # Fast path: 1 request per operator if supported by TemporaSMS gateway
        bulk_rows = await self._try_bulk_service_offers(ops_map)
        if bulk_rows is not None:
            by_cid: dict[str, list[dict]] = {}
            for r in bulk_rows:
                by_cid.setdefault(str(r["country_id"]), []).append(dict(r))
            now = time.time()
            for cid_k, crows in by_cid.items():
                crows.sort(key=lambda x: (x["cost"], -x["count"]))
                self._cache[f"co:{sid}:{cid_k}"] = (now, crows)
            self._cache[f"offers:{sid}:0"] = (now, list(bulk_rows))
            self._cache[f"offers:{sid}:1"] = (now, list(bulk_rows))
            return [dict(r) for r in bulk_rows]

        # Per-country scan path (bounded concurrency + deadline so UI never hangs)
        names = await self.countries()
        if not names:
            return []

        target_cids = self._select_country_ids(names, full=bool(full))
        rows: list[dict] = []
        transient_failures = 0
        scanned = 0
        deadline = time.monotonic() + (18.0 if full else 14.0)
        batch_size = 18

        for idx in range(0, len(target_cids), batch_size):
            if idx > 0 and time.monotonic() >= deadline:
                break
            batch = target_cids[idx:idx + batch_size]
            scanned += len(batch)
            batch_res = await asyncio.gather(
                *[self.country_offers(sid, cid, force=force) for cid in batch],
                return_exceptions=True,
            )
            for item in batch_res:
                if isinstance(item, ProviderUnavailable):
                    if item.code in _FATAL_ERRORS:
                        raise item
                    transient_failures += 1
                elif isinstance(item, Exception):
                    transient_failures += 1
                elif item:
                    rows.extend(item)

        if not rows and scanned > 0 and transient_failures >= scanned:
            raise ProviderUnavailable("Provider live rates are temporarily busy.", code="CONNECTION")

        self._cache[cache_key] = (time.time(), rows)
        return [dict(r) for r in rows]

    async def live_offer(self, sid: str, cid: str, op: str, max_age: float = 15) -> dict | None:
        """Authoritative live stock/price check right before purchase."""
        cid_s, op_s = str(cid), str(op)
        live_key = f"live:{sid}:{cid_s}:{op_s}"
        if max_age > 0 and self.has_cached(live_key, max_age=max_age):
            cached = self._cache[live_key][1]
            if isinstance(cached, dict) and int(cached.get("count", 0) or 0) > 0:
                return dict(cached)

        cat = await self.services()
        svc = cat.get(sid)
        if not svc:
            return None
        code = (svc.get("ops") or {}).get(op_s)
        if not code:
            return None

        row, err = await self._fetch_single_op_offer(cid_s, op_s, code)
        if err in _FATAL_ERRORS or err in _TRANSIENT_ERRORS:
            raise ProviderUnavailable("Provider is busy right now. Nothing was charged — please retry.", code=err)
        if row and int(row.get("count", 0) or 0) > 0:
            self._cache[live_key] = (time.time(), dict(row))
            return dict(row)
        self._cache.pop(live_key, None)
        return None

    # ------------------------------------------------------------------
    # Purchase, OTP polling & cancellation
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_access_number(raw: str) -> tuple[str, str] | None:
        if not isinstance(raw, str):
            return None
        text = raw.strip()
        if text.startswith("ACCESS_NUMBER:"):
            parts = text.split(":")
            if len(parts) >= 3 and parts[1].strip() and parts[2].strip():
                return parts[1].strip(), parts[2].strip().lstrip("+")
        if text.startswith("{"):
            try:
                obj = json.loads(text)
                act = obj.get("activationId") or obj.get("id")
                ph = obj.get("phoneNumber") or obj.get("phone") or obj.get("number")
                if act and ph:
                    return str(act).strip(), str(ph).strip().lstrip("+")
            except Exception:
                pass
        return None

    async def get_number(self, service_code: str, country: str, operator: str,
                         max_price: float | None = None, provider_ids: str | None = None) -> tuple[str, str | None, str | None]:
        params: dict[str, str] = {
            "service": str(service_code),
            "country": str(country),
            "operator": str(operator),
        }
        if max_price is not None:
            params["maxPrice"] = f"{float(max_price):.4f}".rstrip("0").rstrip(".")
        if provider_ids:
            params["providerIds"] = str(provider_ids)

        raw = await self._raw_request("getNumber", lane="purchase", **params)
        if raw == "NO_RESPONSE":
            return "error", "NO_RESPONSE", None

        parsed = self._parse_access_number(raw)
        if parsed:
            return "ok", parsed[0], parsed[1]

        err_code, reported = _extract_error_token(raw)
        if err_code in ("WRONG_MAX_PRICE", "BAD_MAX_PRICE"):
            if reported is not None:
                self._remember_price_correction(str(service_code), str(country), str(operator), reported)
                # If provider's real price is still within our maxPrice cap, retry once without providerIds pinning
                if max_price is not None and reported <= float(max_price) + 1e-9:
                    retry_params = {
                        "service": str(service_code),
                        "country": str(country),
                        "operator": str(operator),
                        "maxPrice": str(reported),
                    }
                    retry_raw = await self._raw_request("getNumber", lane="purchase", **retry_params)
                    retry_parsed = self._parse_access_number(retry_raw)
                    if retry_parsed:
                        return "ok", retry_parsed[0], retry_parsed[1]
            return "error", err_code, None

        if err_code in ("NO_NUMBERS", "BAD_SERVICE", "BAD_COUNTRY", "BAD_OPERATOR", "BAD_TYPE"):
            return "no_stock", err_code, None
        if err_code == "NO_BALANCE":
            return "no_balance", "NO_BALANCE", None
        return "error", (err_code or raw or "ERROR"), None

    async def get_status(self, activation_id: str) -> tuple[str, str | None]:
        try:
            raw = await self._raw_request("getStatus", lane="purchase", id=str(activation_id))
        except ProviderUnavailable:
            return "wait", None

        if not isinstance(raw, str) or raw == "NO_RESPONSE":
            return "wait", None

        text = raw.strip()
        if text.startswith("STATUS_OK:"):
            code = text.split(":", 1)[1].strip()
            return ("otp", code) if code else ("wait", None)
        if text.startswith("STATUS_CANCEL") or text == "NO_ACTIVATION":
            return "cancelled", None
        if text.startswith("STATUS_WAIT"):
            return "wait", None

        # JSON fallback if a gateway returns {"status": "...", "code": "..."}
        if text.startswith("{"):
            try:
                obj = json.loads(text)
                st = str(obj.get("status") or "").upper()
                code = obj.get("code") or obj.get("sms")
                if code:
                    return "otp", str(code).strip()
                if "CANCEL" in st:
                    return "cancelled", None
            except Exception:
                pass
        return "wait", None

    async def cancel(self, activation_id: str) -> str:
        try:
            raw = await self._raw_request("setStatus", lane="purchase", status="8", id=str(activation_id))
        except ProviderUnavailable:
            return "error"

        text = (raw or "").strip()
        if text in ("ACCESS_CANCEL", "ACCESS_CANCEL_ALREADY", "STATUS_CANCEL", "NO_ACTIVATION"):
            return "cancelled"
        if "EARLY_CANCEL_DENIED" in text:
            return "early"
        return "error"

    async def finish(self, activation_id: str) -> str:
        try:
            return await self._raw_request("setStatus", lane="purchase", status="6", id=str(activation_id))
        except Exception:
            return ""
