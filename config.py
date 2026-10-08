"""Environment configuration for TGA Store | API.

No credentials are embedded in source. Copy ``example.env`` to ``.env`` for local
use and keep the real file private.
"""

import os
from dotenv import load_dotenv

load_dotenv()


def _getenv(name: str, default: str | None = None, required: bool = False) -> str:
    value = os.getenv(name, default)
    if required and not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value or ""


BOT_TOKEN = _getenv("BOT_TOKEN", required=True)
ADMIN_IDS = [
    int(item) for item in _getenv("ADMIN_IDS", required=True).replace(" ", "").split(",")
    if item
]
MONGO_URI = _getenv("MONGO_URI", required=True)
DATABASE_URL = _getenv("DATABASE_URL", "sqlite:///data.db")

API_ID = int(_getenv("API_ID", "0") or 0)
API_HASH = _getenv("API_HASH")
TEMPORASMS_API_KEY = _getenv("TEMPORASMS_API_KEY")

BOT_USERNAME = _getenv("BOT_USERNAME", "TGAStore1Bot").lstrip("@")
SUPPORT_USERNAME = _getenv("SUPPORT_USERNAME", "OfcCuteSmile").lstrip("@")
UPDATES_CHANNEL = _getenv("UPDATES_CHANNEL", "TGAForu").lstrip("@")
SALES_LOG_CHANNEL = _getenv("SALES_LOG_CHANNEL", "twalllogs").lstrip("@")
ADMIN_LOG_ID = _getenv("ADMIN_LOG_ID", "-1003441797589")
SALES_LOG_ID = _getenv("SALES_LOG_ID", "-1002254383562")
MUST_JOIN_CHANNEL = _getenv("MUST_JOIN_CHANNEL", "-1003394213814")

DEFAULT_CURRENCY = _getenv("DEFAULT_CURRENCY", "₹")
MIN_BALANCE_REQUIRED = float(_getenv("MIN_BALANCE_REQUIRED", "0"))

UPI_API_KEY = _getenv("UPI_API_KEY")
UPI_API_URL = _getenv("UPI_API_URL", "https://bot.jackdev.tech/paymentlinkbot/api.php")
FAMPAY_UPI = _getenv("FAMPAY_UPI")
FAMPAY_API_KEY = _getenv("FAMPAY_API_KEY")
FAMPAY_QR_API_URL = _getenv("FAMPAY_QR_API_URL", "https://anujbots.xyz/api/qr.php")
FAMPAY_VERIFY_API_URL = _getenv("FAMPAY_VERIFY_API_URL", "https://fampay.anujbots.xyz/verify.php")
BINANCE_PAY_ID = _getenv("BINANCE_PAY_ID")
BINANCE_QR_URL = _getenv("BINANCE_QR_URL")
BINANCE_API_KEY = _getenv("BINANCE_API_KEY")
BINANCE_SECRET = _getenv("BINANCE_SECRET")
