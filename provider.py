import os

# Purchase key (getNumber / getStatus / cancel / balance) comes strictly from environment or DB settings.
API_KEY = os.getenv("TEMPORASMS_API_KEY", "")
# Optional second key dedicated to live rates / stock scanning.
RATES_API_KEY = os.getenv("TEMPORASMS_RATES_API_KEY") or API_KEY
BASE_URL = "https://api.temporasms.com/stubs/handler_api.php"

# Static mappings for operator-specific platform service codes
OPERATOR_SERVICES = {
    "1": {"tg": "edg", "wa": "tss"},
    "9": {"tg": "ehbg", "wa": "tpgs"},
    "15": {"tg": "ewfg", "wa": "tias"},
    "16": {"tg": "edg", "wa": "tas"},
}

COUNTRY_IDS = {
    "usa": {"1": "12", "9": "12"},
    "united kingdom": {"9": "16"},
    "iran": {"9": "57", "15": "57"},
    "argentina": {"9": "39"},
    "indonesia": {"9": "6"},
    "philippines": {"9": "4", "16": "4"},
    "canada": {"9": "36"},
    "vietnam": {"1": "10", "9": "10"},
}

COUNTRY_FLAGS = {
    "usa": "🇺🇸",
    "united kingdom": "🇬🇧",
    "iran": "🇮🇷",
    "argentina": "🇦🇷",
    "indonesia": "🇮🇩",
    "philippines": "🇵🇭",
    "canada": "🇨🇦",
    "vietnam": "🇻🇳",
}

MANUAL_CONFIG = [
    ("tg", "USA", "9", 37, 80),
    ("tg", "USA", "1", 46, 90),
    ("tg", "United Kingdom", "9", 60, 100),
    ("tg", "United Kingdom", "9", 69, 110),
    ("tg", "Iran", "9", 37, 80),
    ("tg", "Iran", "15", 52, 95),
    ("tg", "Argentina", "9", 52, 95),
    ("tg", "Indonesia", "9", 35, 75),
    ("wa", "Philippines", "16", 26, 55),
    ("wa", "Philippines", "9", 34, 65),
    ("wa", "USA", "9", 18, 60),
    ("wa", "Indonesia", "16", 30, 70),
    ("wa", "Indonesia", "9", 22, 65),
    ("wa", "Canada", "9", 32, 85),
    ("wa", "Canada", "9", 30, 80),
    ("wa", "Vietnam", "1", 33, 85),
    ("wa", "Vietnam", "9", 30, 80),
]


def get_active_offers(service: str):
    """Return configured static offers matching ``service``."""
    offers = []
    for idx, (srv, country, op, max_p, bot_p) in enumerate(MANUAL_CONFIG):
        if srv == service:
            c_key = country.lower()
            c_id = COUNTRY_IDS.get(c_key, {}).get(op)
            srv_code = OPERATOR_SERVICES.get(op, {}).get(service)
            if c_id and srv_code:
                offers.append({
                    "index": idx,
                    "country": country,
                    "operator": op,
                    "max_price": max_p,
                    "bot_price": bot_p,
                    "country_id": c_id,
                    "service_code": srv_code,
                })
    return offers
