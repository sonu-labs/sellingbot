"""Runtime settings for the "Buy OTPs (All Apps)" TemporaSMS storefront.

The document lives in the shared ``settings`` collection under
``_id="otp_services"`` and follows the same pattern as
``payment_settings.py`` (one document, backfilled defaults, targeted
``$set`` updates) so an admin can change behaviour from the bot without a
restart or deployment.

API-key storage note: the bot has no encryption-at-rest layer for provider
keys.  The existing SMM module already stores provider API keys in MongoDB
after private-chat entry and never displays them; this module follows that
same established pattern.  The key is therefore only as safe as the MongoDB
deployment itself — it is *not* encrypted by this code, and the admin UI
says so.  Only a masked form (last 4 characters) is ever shown, and the
full key is never written to logs, audit records or error messages.

Precedence rules (checked on every use, no restart needed):

- API key: database value first, ``TEMPORASMS_API_KEY`` environment value
  as fallback.
- Markup: database value first (when not ``None``), ``OTP_MARKUP_PERCENT``
  environment value as fallback.
- USD→INR rate is *not* stored here: the whole bot uses the single global
  fixed rate from ``config.USD_TO_INR``.
"""

import math
from datetime import datetime

SETTINGS_ID = "otp_services"

# Operators offered by the TemporaSMS routes used by this storefront.
# ``operator`` is a *numeric direct route* only (provider docs).
ALL_OPERATORS = ("1", "2", "3", "4", "7", "8", "9", "10")

# Automatic operator selection (provider docs): sent as ``type=<value>``
# instead of ``operator``, and routed by the provider across the operators
# eligible for the submitted service + country.
#
#   smart — eligible operators for this exact service/country, chosen from
#           current delivery evidence
#   auto  — lowest final price first, trying the next operator if unavailable
#   cheap — only the single lowest-priced eligible operator
#   best  — highest success-rate operator first, then the next if unavailable
AUTO_ROUTE_TYPES = ("smart", "auto", "cheap", "best")

# Everything an admin can switch on/off: numeric lines + automatic routing.
ALL_ROUTES = AUTO_ROUTE_TYPES + ALL_OPERATORS

# Built-in popular-app ordering (name fragments) used until an admin
# configures an explicit popular list.  Matches the historic behaviour.
DEFAULT_POPULAR_NAMES = ("telegram", "instagram", "facebook", "whatsapp", "google", "tiktok")

MAX_MARKUP_PERCENT = 10000.0
MAX_API_KEY_LENGTH = 512

DEFAULTS = {
    "_id": SETTINGS_ID,
    "enabled": True,
    # None => fall back to the TEMPORASMS_API_KEY environment variable.
    "tempora_api_key": None,
    # None => fall back to the OTP_MARKUP_PERCENT environment variable.
    "markup_percent": None,
    "disabled_operators": [],
    # Explicit admin ordering: [{"code": "tg", "name": "Telegram"}, ...].
    # Empty => DEFAULT_POPULAR_NAMES ordering is used.
    "popular_apps": [],
    # Apps hidden from the user catalog: [{"code": ..., "name": ...}, ...].
    "hidden_apps": [],
    # Bumped on every price/availability-relevant change so quotes created
    # under older settings are rejected instead of charging a stale price.
    "settings_version": 0,
    "updated_at": None,
    "updated_by": None,
}


def _copy_defaults():
    fresh = dict(DEFAULTS)
    fresh["disabled_operators"] = []
    fresh["popular_apps"] = []
    fresh["hidden_apps"] = []
    return fresh


def get_settings(settings_col):
    """Return the settings document and backfill keys added in later versions."""
    doc = settings_col.find_one({"_id": SETTINGS_ID})
    if not doc:
        fresh = _copy_defaults()
        settings_col.insert_one(dict(fresh))
        return fresh

    changed = False
    patch = {}
    for key, value in DEFAULTS.items():
        if key not in doc:
            doc[key] = list(value) if isinstance(value, list) else value
            patch[key] = doc[key]
            changed = True
    if changed:
        # Never include _id in a $set document (immutable field).
        settings_col.update_one({"_id": SETTINGS_ID}, {"$set": patch}, upsert=True)
    return doc


def _touch(settings_col, admin_id, *, bump_version=False, extra=None):
    patch = {"updated_at": datetime.utcnow(), "updated_by": admin_id}
    if extra:
        patch.update(extra)
    update = {"$set": patch}
    if bump_version:
        update["$inc"] = {"settings_version": 1}
    settings_col.update_one({"_id": SETTINGS_ID}, update, upsert=True)
    return get_settings(settings_col)


# --------------------------------------------------------------------------
# API key handling
# --------------------------------------------------------------------------

def validate_api_key(raw):
    """Return the cleaned key or raise ``ValueError`` with a safe message."""
    key = str(raw or "").strip()
    if not key:
        raise ValueError("API key cannot be empty.")
    if len(key) > MAX_API_KEY_LENGTH:
        raise ValueError("API key is too long.")
    if any(ch.isspace() for ch in key):
        raise ValueError("API key must not contain spaces. Send only the key.")
    return key


def mask_key(key):
    """Masked display form.  Never returns more than the last 4 characters."""
    key = str(key or "")
    if not key:
        return "Not set"
    if len(key) < 8:
        return "••••••••"
    return f"••••••••{key[-4:]}"


def resolve_api_key(settings, env_key):
    """Return ``(key, source)`` where source is "database", "environment" or None."""
    db_key = str((settings or {}).get("tempora_api_key") or "").strip()
    if db_key:
        return db_key, "database"
    env = str(env_key or "").strip()
    if env:
        return env, "environment"
    return "", None


def set_api_key(settings_col, raw_key, admin_id):
    key = validate_api_key(raw_key)
    get_settings(settings_col)
    # A key change can alter catalog/prices, so outstanding quotes must die.
    return _touch(settings_col, admin_id, bump_version=True,
                  extra={"tempora_api_key": key})


def remove_api_key(settings_col, admin_id):
    get_settings(settings_col)
    return _touch(settings_col, admin_id, bump_version=True,
                  extra={"tempora_api_key": None})


# --------------------------------------------------------------------------
# Service switch + markup
# --------------------------------------------------------------------------

def set_enabled(settings_col, enabled, admin_id):
    get_settings(settings_col)
    return _touch(settings_col, admin_id, bump_version=True,
                  extra={"enabled": bool(enabled)})


def validate_markup(raw):
    """Parse and validate a markup percentage (finite, non-negative)."""
    if isinstance(raw, bool):
        raise ValueError("Markup must be a number.")
    try:
        value = float(str(raw).strip().rstrip("%"))
    except (TypeError, ValueError):
        raise ValueError("Markup must be a number, e.g. 25 or 12.5") from None
    if not math.isfinite(value):
        raise ValueError("Markup must be a finite number.")
    if value < 0:
        raise ValueError("Markup cannot be negative.")
    if value > MAX_MARKUP_PERCENT:
        raise ValueError(f"Markup cannot exceed {MAX_MARKUP_PERCENT:g}%.")
    return value


def resolve_markup(settings, env_markup):
    """Return ``(markup, source)``; invalid stored values fall back safely."""
    stored = (settings or {}).get("markup_percent")
    if stored is not None:
        try:
            return validate_markup(stored), "database"
        except ValueError:
            pass  # Corrupt stored value must never break pricing.
    try:
        return validate_markup(env_markup), "environment"
    except ValueError:
        return 0.0, "default"


def set_markup(settings_col, raw_value, admin_id):
    value = validate_markup(raw_value)
    get_settings(settings_col)
    return _touch(settings_col, admin_id, bump_version=True,
                  extra={"markup_percent": value})


def clear_markup(settings_col, admin_id):
    """Remove the database override so the environment value applies again."""
    get_settings(settings_col)
    return _touch(settings_col, admin_id, bump_version=True,
                  extra={"markup_percent": None})


# --------------------------------------------------------------------------
# Operators (numeric direct routes) + automatic routing types
# --------------------------------------------------------------------------

def normalize_route(raw):
    """Return a canonical route value, or ``None`` when it is not a route.

    Numeric strings are direct operator routes; the documented automatic
    selection values (``smart``/``auto``/``cheap``/``best``) are matched
    case-insensitively.  Anything else — including provider error tokens —
    is rejected so it can never be sent to the API.
    """
    text = str(raw or "").strip()
    if not text:
        return None
    lowered = text.lower()
    if lowered in AUTO_ROUTE_TYPES:
        return lowered
    if text.isdigit():
        number = int(text)
        return str(number) if 1 <= number <= 999999 else None
    return None


def is_auto_route(route) -> bool:
    return str(route or "").strip().lower() in AUTO_ROUTE_TYPES


def disabled_routes(settings):
    return {str(route) for route in (settings or {}).get("disabled_operators") or []}


def enabled_operators(settings):
    """Enabled *numeric* operators, in the documented order."""
    disabled = disabled_routes(settings)
    return tuple(op for op in ALL_OPERATORS if op not in disabled)


def enabled_auto_types(settings):
    """Enabled automatic routing types, in the documented order."""
    disabled = disabled_routes(settings)
    return tuple(route for route in AUTO_ROUTE_TYPES if route not in disabled)


def enabled_routes(settings):
    """Every selectable route: automatic types first, then numeric lines."""
    return enabled_auto_types(settings) + enabled_operators(settings)


def set_operator_enabled(settings_col, operator, enabled, admin_id):
    """Enable/disable a numeric operator *or* an automatic routing type."""
    route = normalize_route(operator)
    if route is None or route not in ALL_ROUTES:
        raise ValueError("Unknown operator.")
    settings = get_settings(settings_col)
    disabled = disabled_routes(settings)
    if enabled:
        disabled.discard(route)
    else:
        disabled.add(route)
    ordered = [entry for entry in ALL_ROUTES if entry in disabled]
    return _touch(settings_col, admin_id, bump_version=True,
                  extra={"disabled_operators": ordered})


# --------------------------------------------------------------------------
# Popular apps ordering + visibility
# --------------------------------------------------------------------------

def _clean_app_entries(raw):
    entries, seen = [], set()
    for item in raw or []:
        if isinstance(item, dict):
            code = str(item.get("code") or "").strip()
            name = str(item.get("name") or code).strip()
        else:  # tolerate a legacy plain-code list
            code = str(item or "").strip()
            name = code
        if code and code not in seen:
            seen.add(code)
            entries.append({"code": code, "name": name[:80] or code})
    return entries


def popular_apps(settings):
    return _clean_app_entries((settings or {}).get("popular_apps"))


def hidden_apps(settings):
    return _clean_app_entries((settings or {}).get("hidden_apps"))


def hidden_codes(settings):
    return {entry["code"] for entry in hidden_apps(settings)}


def set_popular_apps(settings_col, entries, admin_id):
    get_settings(settings_col)
    return _touch(settings_col, admin_id,
                  extra={"popular_apps": _clean_app_entries(entries)})


def set_app_hidden(settings_col, code, name, hidden, admin_id):
    settings = get_settings(settings_col)
    entries = hidden_apps(settings)
    code = str(code)
    if hidden:
        if code not in {e["code"] for e in entries}:
            entries.append({"code": code, "name": str(name or code)[:80]})
    else:
        entries = [e for e in entries if e["code"] != code]
    # Hiding an app must also kill quotes that reference it via "open"
    # flows; version bump makes any outstanding quote stale.
    return _touch(settings_col, admin_id, bump_version=True,
                  extra={"hidden_apps": entries})


def sort_services(items, settings):
    """Order ``(code, name)`` catalog items for the user storefront.

    Admin-configured popular apps (by code) come first in admin order;
    when none are configured the historic built-in name ordering applies.
    Hidden apps are removed entirely.
    """
    hidden = hidden_codes(settings)
    visible = [(code, name) for code, name in items if code not in hidden]
    popular = popular_apps(settings)
    if popular:
        rank = {entry["code"]: i for i, entry in enumerate(popular)}
        visible.sort(key=lambda x: (rank.get(x[0], 10000), x[1].casefold()))
    else:
        visible.sort(key=lambda x: (
            next((i for i, frag in enumerate(DEFAULT_POPULAR_NAMES) if frag in x[1].casefold()), 99),
            x[1].casefold(),
        ))
    return visible


# --------------------------------------------------------------------------
# Effective runtime view
# --------------------------------------------------------------------------

def effective_settings(settings_col, env_key, env_markup, rate):
    """Resolve everything the storefront needs at runtime in one place."""
    settings = get_settings(settings_col) if settings_col is not None else _copy_defaults()
    key, key_source = resolve_api_key(settings, env_key)
    markup, markup_source = resolve_markup(settings, env_markup)
    return {
        "raw": settings,
        "enabled": bool(settings.get("enabled", True)),
        "api_key": key,
        "key_source": key_source,
        "markup": markup,
        "markup_source": markup_source,
        "rate": float(rate),
        # Numeric direct routes only (getServices/getCountries need these).
        "operators": enabled_operators(settings),
        # Documented automatic selection values, sent as ``type=<value>``.
        "auto_types": enabled_auto_types(settings),
        # Everything the storefront may offer as a line choice.
        "routes": enabled_routes(settings),
        "version": int(settings.get("settings_version") or 0),
    }
