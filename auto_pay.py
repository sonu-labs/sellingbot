#---------- Auto Payment Helpers: UPI Gateway / FamPay / Binance Pay / Cwallet ----------
import hashlib
import hmac
import logging
import os
import time

import requests

from config import (
    UPI_API_KEY, UPI_API_URL,
    FAMPAY_API_KEY, FAMPAY_VERIFY_API_URL,
    BINANCE_API_KEY, BINANCE_SECRET,
)

try:
    import config as _config
except Exception:  # pragma: no cover - the normal deployment always has config.py
    _config = None


# Cwallet's API key is a secret and should be supplied through config.py or an
# environment variable.  Keeping the URL configurable is useful for staging,
# while the production URL from the API documentation remains the default.
CWALLET_API_KEY = (
    getattr(_config, "CWALLET_API_KEY", None) if _config else None
) or os.getenv("CWALLET_API_KEY", "")
CWALLET_API_URL = (
    getattr(_config, "CWALLET_API_URL", None) if _config else None
) or os.getenv("CWALLET_API_URL", "https://cwallet.rulebreaker.cc")
CWALLET_API_URL = str(CWALLET_API_URL).rstrip("/")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("AutoPay")


# ================= UPI Gateway (jackdev.tech) =================
def create_upi_invoice(amount: float):
    """Create a UPI order. Returns dict with success/order_id/qr_url or error."""
    payload = {"api_key": UPI_API_KEY, "action": "create", "amount": amount}
    try:
        res = requests.post(UPI_API_URL, json=payload, timeout=20).json()
        order_id = res.get("order_id")
        qr_url = res.get("qr_url")
        if not order_id or not qr_url:
            return {"success": False, "error": res.get("message", "Invalid response from UPI gateway")}
        return {"success": True, "order_id": order_id, "qr_url": qr_url}
    except Exception as e:
        logger.error(f"UPI create_invoice error: {e}")
        return {"success": False, "error": str(e)}


def verify_upi_invoice(order_id: str):
    """Verify a UPI order. Returns True if paid."""
    payload = {"api_key": UPI_API_KEY, "action": "verify", "order_id": order_id}
    try:
        res = requests.post(UPI_API_URL, json=payload, timeout=20).json()
        return bool(res.get("status") == "success" or res.get("paid") is True)
    except Exception as e:
        logger.error(f"UPI verify error: {e}")
        return False


# ================= FamPay (UTR / transaction-ID verification) =================
def verify_fampay_invoice(reference: str):
    """Verify a user-submitted FamPay UTR or transaction ID.

    Numeric references are sent as ``utr``; alphanumeric references are sent
    as ``transaction_id``. The static QR has no fixed amount, so the request
    deliberately omits ``amount`` and returns the actual amount received.
    """
    reference = str(reference or "").strip()
    if not reference or len(reference) > 128 or any(ch.isspace() for ch in reference):
        return {
            "success": False,
            "received": False,
            "already_verified": False,
            "status_code": 400,
            "message": "Invalid UTR or transaction ID",
        }

    if not FAMPAY_API_KEY:
        logger.error("FamPay API key is not configured")
        return {
            "success": False,
            "received": False,
            "already_verified": False,
            "status_code": 401,
            "message": "FamPay API key is not configured",
        }

    id_field = "utr" if reference.isdigit() else "transaction_id"
    headers = {
        "Authorization": f"Bearer {FAMPAY_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    try:
        response = requests.post(
            FAMPAY_VERIFY_API_URL,
            headers=headers,
            json={id_field: reference},
            timeout=20,
        )
        status_code = getattr(response, "status_code", 500)
        try:
            body = response.json()
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}

        received = body.get("received") is True
        already_verified = body.get("already_verified") is True
        if (
            status_code != 200
            or body.get("success") is not True
            or not received
            or already_verified
        ):
            return {
                "success": False,
                "received": received,
                "already_verified": already_verified,
                "status_code": status_code,
                "message": body.get("message") or "Payment not verified",
            }

        transaction_id = body.get("transaction_id")
        utr = body.get("utr")
        return {
            "success": True,
            "received": True,
            "already_verified": False,
            "amount": body.get("amount"),
            "currency": body.get("currency"),
            "transaction_id": transaction_id,
            "utr": utr,
            # Keep the older normalized result key for any other callers.
            "txn": transaction_id or utr,
            "sender": body.get("sender_name"),
            "date": body.get("date"),
            "message": body.get("message"),
            "status_code": status_code,
        }
    except Exception as exc:
        logger.exception("FamPay verification request failed")
        return {
            "success": False,
            "received": False,
            "already_verified": False,
            "status_code": 500,
            "message": "FamPay verification is temporarily unavailable",
            "error": str(exc),
        }


# ================= Binance Pay =================
def get_binance_pay_transactions():
    """Fetch last 7 days of Binance Pay transactions."""
    timestamp = int(time.time() * 1000)
    query = f"timestamp={timestamp}&recvWindow=60000&startTime={timestamp - 604800000}&endTime={timestamp}"
    signature = hmac.new(BINANCE_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()
    url = f"https://api.binance.com/sapi/v1/pay/transactions?{query}&signature={signature}"
    headers = {"X-MBX-APIKEY": BINANCE_API_KEY}
    try:
        return requests.get(url, headers=headers, timeout=20).json()
    except Exception as e:
        logger.error(f"Binance fetch error: {e}")
        return {"data": []}


def verify_binance_order(order_or_txn_id: str):
    """
    Look for a matching Binance Pay transaction by orderId or transactionId.
    Returns dict with success/amount/currency/payer_name, or success=False.
    """
    data = get_binance_pay_transactions()
    for tx in data.get("data", []):
        if str(tx.get("orderId")) == order_or_txn_id or str(tx.get("transactionId")) == order_or_txn_id:
            return {
                "success": True,
                "amount": tx.get("amount"),
                "currency": tx.get("currency"),
                "payer_name": tx.get("payerInfo", {}).get("name", "Anonymous Payer"),
            }
    return {"success": False}


# ================= Cwallet Payment Verification =================
def verify_cwallet_payment(
    transaction_id: str,
    *,
    expected_currency: str = "USDT",
    expected_amount: float | None = None,
):
    """Verify one Cwallet transaction through the merchant API.

    The endpoint returns a successful HTTP response even when a payment was
    not found, so callers must inspect ``received`` rather than only the HTTP
    status.  The returned dictionary always contains the useful normalized
    fields and also preserves the API's message for the UI.
    """
    transaction_id = str(transaction_id or "").strip()
    if not transaction_id:
        return {
            "success": False,
            "received": False,
            "message": "Transaction ID is required",
        }

    if not CWALLET_API_KEY:
        logger.error("Cwallet API key is not configured")
        return {
            "success": False,
            "received": False,
            "message": "Cwallet API key is not configured",
        }

    payload = {"transaction_id": transaction_id}
    if expected_currency:
        payload["expected_currency"] = expected_currency
    if expected_amount is not None:
        payload["expected_amount"] = expected_amount

    headers = {
        "Authorization": f"Bearer {CWALLET_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    url = f"{CWALLET_API_URL}/api/v1/payment/verify"

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=20)
        try:
            body = response.json()
        except ValueError:
            body = {}

        if not isinstance(body, dict):
            body = {}

        status_code = getattr(response, "status_code", 200)
        received = body.get("received") is True
        success = body.get("success") is True and status_code < 400
        result = {
            "success": success,
            "received": received,
            "transaction_id": body.get("transaction_id") or transaction_id,
            "amount": body.get("amount"),
            "currency": body.get("currency"),
            "message": body.get("message") or (
                "Payment received successfully" if received else "Payment not found"
            ),
            "status_code": status_code,
        }
        if not success and status_code >= 400:
            result["error"] = result["message"]
        return result
    except Exception as e:
        logger.error(f"Cwallet verify error: {e}")
        return {
            "success": False,
            "received": False,
            "message": "Cwallet verification is temporarily unavailable",
            "error": str(e),
        }


# Descriptive aliases keep integrations that call the operation a transaction
# verification rather than a payment verification compatible.
verify_cwallet_transaction = verify_cwallet_payment
verify_cwallet = verify_cwallet_payment