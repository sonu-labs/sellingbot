#---------- © sᴛᴀʟᴋᴇʀ@hehe_stalker
#---------- ᴘʀᴏJᴇᴄᴛ - ᴛᴇʟᴇɢʀᴀᴍ ᴀᴜᴛᴏᴍᴀᴛᴇᴅ ᴀᴄᴄᴏᴜɴᴛ sᴇʟʟɪɴɢ ʙᴏᴛ
#-------------------------------------------------------
import os
from dotenv import load_dotenv

load_dotenv()

def _getenv(name: str, default: str | None = None, required: bool = False) -> str:
    val = os.getenv(name, default)
    if required and (val is None or val == ""):
        raise RuntimeError(f"Missing required env var: {name}")
    return val
    
MUST_JOIN_CHANNEL = "-1003394213814"
BOT_TOKEN = _getenv("BOT_TOKEN", required=True)
ADMIN_IDS = [int(i) for i in _getenv("ADMIN_IDS", "", required=True).replace(" ", "").split(",") if i]
API_ID = "31175288"
API_HASH = "aaf1c01a7336f5de2e85638c1c06b0d0"

DATABASE_URL = _getenv("DATABASE_URL", "sqlite:///data.db")



DEFAULT_CURRENCY = _getenv("DEFAULT_CURRENCY", "₹")
MIN_BALANCE_REQUIRED = float(_getenv("MIN_BALANCE_REQUIRED", "0"))

#---------- Auto Payment Gateways (UPI / FamPay / Binance Pay) ----------
UPI_API_KEY = _getenv("UPI_API_KEY", "")
UPI_API_URL = _getenv("UPI_API_URL", "https://bot.jackdev.tech/paymentlinkbot/api.php")

FAMPAY_UPI = _getenv("FAMPAY_UPI", "")
FAMPAY_API_KEY = _getenv("FAMPAY_API_KEY", "")
FAMPAY_QR_API_URL = _getenv("FAMPAY_QR_API_URL", "https://anujbots.xyz/api/qr.php")
FAMPAY_VERIFY_API_URL = _getenv("FAMPAY_VERIFY_API_URL", "https://fampay.anujbots.xyz/verify.php")

BINANCE_PAY_ID = _getenv("BINANCE_PAY_ID", "")
BINANCE_QR_URL = _getenv("BINANCE_QR_URL", "")
BINANCE_API_KEY = _getenv("BINANCE_API_KEY", "")
BINANCE_SECRET = _getenv("BINANCE_SECRET", "")
