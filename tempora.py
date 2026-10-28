"""Compatibility shim for allapps.py — wraps otp.py's TemporaClient and helpers.

All log channel IDs and API keys now come from config/env, never hardcoded.
"""
import re
import math
from decimal import Decimal, ROUND_UP

# Reuse otp's rate/markup math but adapt to allapps's cfg-dict signature
from config import USD_TO_INR

# ProviderUnavailable alias — allapps expects this, otp uses ProviderError
class ProviderUnavailable(Exception):
    pass

# Priority ordering and legacy names — minimal sane defaults
PRIORITY_COUNTRIES = [
    "united states", "usa", "india", "indonesia", "philippines", "vietnam",
    "canada", "ukraine", "kazakhstan", "argentina", "iran", "brazil",
    "united kingdom", "russia", "nigeria", "turkey", "pakistan", "bangladesh"
]
LEGACY_NAMES = {
    "tg": "Telegram",
    "wa": "WhatsApp",
    "ig": "Instagram",
    "fb": "Facebook",
    "go": "Google",
    "tt": "TikTok",
}

def norm(s: str) -> str:
    return re.sub(r'[^a-z0-9]+', ' ', str(s).lower()).strip()

def sid_of(name: str) -> str:
    n = norm(name)
    # Known display names -> short codes
    mapping = {norm(v): k for k, v in LEGACY_NAMES.items()}
    # also direct code match
    if n in LEGACY_NAMES:
        return n
    return mapping.get(n, n.replace(' ', '_'))

# Bridge retail_price / cost_inr: allapps passes cfg dict, otp passes (cost, rate, markup)
def _extract_rate_markup(cfg):
    if isinstance(cfg, dict):
        markup = float(cfg.get("markup_pct", cfg.get("markup", 100)) or 0)
        rate = float(cfg.get("unit_to_inr", cfg.get("rate", USD_TO_INR)) or USD_TO_INR)
        return rate, markup
    # fallback if someone passes numeric
    return float(USD_TO_INR), float(cfg) if cfg is not None else 0

def retail_price(cost, cfg):
    rate, markup = _extract_rate_markup(cfg)
    return float((Decimal(str(cost)) * Decimal(str(rate)) * (Decimal(1) + Decimal(str(markup))/100)).quantize(Decimal("0.01"), rounding=ROUND_UP))

def cost_inr(cost, cfg):
    rate, _ = _extract_rate_markup(cfg)
    return float(Decimal(str(cost)) * Decimal(str(rate)))

def search_services(catalog: dict, query: str):
    q = norm(query)
    out = []
    for sid, entry in (catalog or {}).items():
        name = norm(entry.get("name", "") if isinstance(entry, dict) else str(entry))
        if q in name:
            out.append((sid, entry))
    return out

def popular_services(catalog: dict):
    # Return popular in PRIORITY-like order plus catalog order
    if not catalog:
        return []
    # Use LEGACY_NAMES order then alphabetical
    popular = []
    norm_catalog = {norm(v.get("name","") if isinstance(v, dict) else str(v)): (k,v) for k,v in catalog.items()}
    for code in ["tg","wa"]:
        disp = LEGACY_NAMES.get(code, "")
        n = norm(disp)
        if n in norm_catalog:
            popular.append(norm_catalog[n])
    # Fill rest
    for k,v in catalog.items():
        if (k,v) not in popular:
            popular.append((k,v))
    return popular

# Re-export TemporaClient from otp but with allapps-compatible constructor
from otp import TemporaClient as _BaseClient

class TemporaClient(_BaseClient):
    """Adapter with separate purchase/rates keys and allapps method names."""
    def __init__(self, api_key, base_url=None, rates_key=None, **kwargs):
        # base_url ignored — otp uses constant BASE_URL
        # rates_key: separate key for live rates (if different)
        # api_key may be string or callable
        from config import TEMPORASMS_API_KEY
        # Let otp handle catalog_col via passed kwargs if any
        super().__init__(key=api_key or TEMPORASMS_API_KEY, **kwargs)
        self._rates_key_source = rates_key or api_key
        # expose attributes expected by allapps
        self.BASE_URL = base_url
        self._api_key_raw = api_key
        self._rates_key_raw = rates_key

    @property
    def separate_keys(self):
        a = self._api_key_raw() if callable(self._api_key_raw) else self._api_key_raw
        r = self._rates_key_source() if callable(self._rates_key_source) else self._rates_key_source
        return bool(r) and str(a).strip() != str(r).strip()

    # Compatibility shims for allapps method names
    async def service_offers(self, sid, full=False, force=False):
        # Map to otp's pricing lookup: get price options for service across countries
        # For compatibility, use catalog + per-country price_options gathering
        # Simplify: return empty list if not cached — caller will handle
        cache_key = f"offers:{sid}:{int(full)}"
        if not force and self.has_cached(cache_key):
            return self.cache.get(cache_key, ([], {}))[0] if isinstance(self.cache.get(cache_key), tuple) else self.cache.get(cache_key, [])
        # Fallback: ask provider for available countries for this service?
        # Use generic call to get services then synthesize rows
        try:
            # try to use otp's cached catalog
            result = await self.call(action="getPrices", service=sid)
        except Exception:
            result = {}
        rows = []
        # Parse result into rows with country_id, operator, cost, count
        if isinstance(result, dict):
            for k, v in result.items():
                if k in ("providers","price","cost"):
                    continue
                # k may be price, v stock count?
                try:
                    cost = float(k)
                    count = int(v) if isinstance(v, (int,str)) and str(v).isdigit() else 0
                    rows.append({"country_id": k, "operator": "1", "cost": cost, "count": count, "code": sid})
                except Exception:
                    continue
        self.cache[cache_key] = (rows, {})
        return rows, {}

    async def countries(self):
        # Return dict of country_id -> name, from catalog
        try:
            data = await self.call(action="getCountries", service="wa")
            # Data may be dict of id->name or string
            if isinstance(data, dict):
                return {str(k): str(v) for k,v in data.items()}
        except Exception:
            pass
        return {}

    async def country_offers(self, sid, cid):
        # Per-country operators/prices
        cache_key = f"co:{sid}:{cid}"
        if self.has_cached(cache_key):
            val = self.cache.get(cache_key)
            if isinstance(val, tuple) and len(val)==2:
                return val
            if isinstance(val, list):
                return val, {}
        try:
            res = await self.call(action="getPricesV2", service=sid, country=cid)
        except Exception:
            res = {}
        rows = []
        if isinstance(res, dict):
            # Try price_options shape
            for price, stock in res.items():
                if price in ("providers","price","cost","count"):
                    continue
                try:
                    cost = float(price)
                    cnt = int(stock) if str(stock).isdigit() else 0
                    rows.append({"country_id": cid, "operator": "1", "cost": cost, "count": cnt, "code": sid})
                except Exception:
                    continue
        return rows, {}

    def has_cached(self, key, max_age=300):
        # Simple: check if key in cache (otp caches catalog)
        return key in self.cache

    def drop_service(self, sid):
        self.clear_cache()

    async def balance(self):
        try:
            raw = await self.call(action="getBalance")
            if isinstance(raw, str) and raw.startswith("ACCESS_BALANCE:"):
                return float(raw.split(":",1)[1])
            if isinstance(raw, dict) and "balance" in raw:
                return float(raw["balance"])
        except Exception:
            pass
        return None

    def invalidate_catalog(self):
        self.clear_cache()

    async def services(self):
        try:
            data = await self.call(action="getServices")
            if isinstance(data, dict):
                return data
        except Exception:
            pass
        return {}

    async def live_offer(self, sid, cid, op, max_age=15):
        rows, _ = await self.country_offers(sid, cid)
        for r in rows:
            if str(r.get("operator")) == str(op):
                return r
        return rows[0] if rows else None

    async def get_number(self, service_code, country, operator, max_price=None):
        params = {"action":"getNumber", "service": service_code, "country": country, "operator": operator}
        if max_price is not None:
            params["maxPrice"] = str(max_price)
        raw = await self.call(**params)
        # Parse ACCESS_NUMBER: id:phone
        if isinstance(raw, str) and raw.startswith("ACCESS_NUMBER:"):
            parts = raw.split(":")
            if len(parts) >=3:
                return "ok", parts[1], parts[2]
        return raw, None, None

    async def get_status(self, activation_id):
        raw = await self.call(action="getStatus", id=activation_id)
        if isinstance(raw, str):
            if raw.startswith("STATUS_OK:"):
                return "ok", raw.split(":",1)[1]
            if raw.startswith("STATUS_WAIT_CODE"):
                return "wait", None
            if raw.startswith("STATUS_CANCEL"):
                return "cancel", None
        return raw, None

    async def cancel(self, activation_id):
        raw = await self.call(action="setStatus", status=8, id=activation_id)
        return raw

    async def finish(self, activation_id):
        raw = await self.call(action="setStatus", status=6, id=activation_id)
        return raw

    # For live purchase path
    async def call(self, **kwargs):
        # Map to base class call with action
        action = kwargs.pop("action", None) or kwargs.pop("a", None)
        if action is None and len(kwargs)==0:
            return None
        if action is None:
            # positional service like getNumber passed as service parameter
            action = "getNumber"
        return await super().call(action=action, **kwargs)

