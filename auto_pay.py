#---------- Auto Payment Helpers: UPI Gateway / FamPay / Binance Pay ----------
import time
import hmac
import hashlib
import logging
import requests

from config import (
    UPI_API_KEY, UPI_API_URL,
    FAMPAY_UPI, FAMPAY_API_KEY, FAMPAY_QR_API_URL, FAMPAY_VERIFY_API_URL,
    BINANCE_API_KEY, BINANCE_SECRET,
)

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


# ================= FamPay =================
def create_fampay_invoice():
    """Create a FamPay QR order. Returns dict with success/order_id/qr_url/expire or error."""
    try:
        res = requests.get(f"{FAMPAY_QR_API_URL}?upi={FAMPAY_UPI}", timeout=20).json()
        if res.get("status") == "success":
            data = res["data"]
            return {
                "success": True,
                "order_id": data["order_id"],
                "qr_url": data["qr_url"],
                "expire": data.get("expires_at_ist"),
            }
        return {"success": False, "error": res.get("message", "Failed to create FamPay QR")}
    except Exception as e:
        logger.error(f"FamPay create_invoice error: {e}")
        return {"success": False, "error": str(e)}


def verify_fampay_invoice(order_id: str):
    """Verify a FamPay order. Returns dict with success/amount/txn/utr/sender or success=False."""
    try:
        res = requests.get(
            f"{FAMPAY_VERIFY_API_URL}?order_id={order_id}&api_key={FAMPAY_API_KEY}",
            timeout=20,
        ).json()
        if res.get("status") == "success":
            data = res["data"]
            return {
                "success": True,
                "txn": data["transaction_id"],
                "utr": data.get("utr"),
                "amount": float(data["amount"]),
                "sender": data.get("sender_name", "Unknown"),
            }
        return {"success": False}
    except Exception as e:
        logger.error(f"FamPay verify error: {e}")
        return {"success": False}


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
