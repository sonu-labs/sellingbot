import requests
import logging

from config import OXAPAY_API_KEY

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("OxaPay")

API_KEY = OXAPAY_API_KEY
# ENDPOINTS
CREATE_URL = "https://api.oxapay.com/v1/payment/invoice"
STATUS_URL = "https://api.oxapay.com/v1/payment/status"

def create_invoice(amount_usd: float, order_id: str):
    """Create an OxaPay invoice."""
    if not API_KEY:
        return {"success": False, "error": "OxaPay API key is not configured"}
    payload = {
        "amount": amount_usd,
        "currency": "USD", # Input currency
        "to_currency": "USDT", # Target currency (can also be 'USDT,TRX,LTC' etc)
        "lifetime": 30,
        "fee_paid_by_payer": 1,
        "under_paid_coverage": 2.5,
        "auto_withdrawal": False,
        "mixed_payment": True,
        "order_id": order_id,
        "description": f"Recharge {order_id}",
        "sandbox": False
    }

    headers = {
        "merchant_api_key": API_KEY,
        "Content-Type": "application/json"
    }

    try:
        response = requests.post(CREATE_URL, json=payload, headers=headers, timeout=20)
        res_data = response.json()
        
        # Check if the HTTP status is 200 (OK)
        if res_data.get("status") == 200 or res_data.get("message") == "Operation completed successfully!":
            return {
                "success": True,
                "data": res_data.get("data") # Contains payment_url, track_id
            }
        else:
            logger.error(f"OxaPay Error: {res_data}")
            return {"success": False, "error": res_data.get("message", "Unknown API Error")}
            
    except Exception as e:
        logger.error(f"Connection Error: {e}")
        return {"success": False, "error": str(e)}

def check_invoice(track_id: str):
    """Check an OxaPay invoice status."""
    if not API_KEY:
        return {"status": 500, "message": "OxaPay API key is not configured"}
    url = f"https://api.oxapay.com/v1/payment/{track_id}"
    
    headers = {
        "merchant_api_key": API_KEY,
        "Content-Type": "application/json"
    }

    try:
        response = requests.get(url, headers=headers, timeout=20)
        return response.json()
    except Exception as e:
        logger.error(f"Check Status Error: {e}")
        return {"status": 500, "message": str(e)}
