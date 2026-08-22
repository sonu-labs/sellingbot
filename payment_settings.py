#---------- Payment Gateway Settings (Admin controlled ON/OFF) ----------
# Stored as a single document in the "settings" collection.
# active_inr_gateway: "upi" or "fampay" (only ONE runs at a time for the merged "UPI (Auto)" button)
# binance_enabled / crypto_enabled: independent toggles

SETTINGS_ID = "payment_gateways"

DEFAULTS = {
    "_id": SETTINGS_ID,
    "active_inr_gateway": "upi",   # "upi" | "fampay" | None (None = INR button hidden)
    "binance_enabled": True,
    "crypto_enabled": True,
}


def get_settings(settings_col):
    doc = settings_col.find_one({"_id": SETTINGS_ID})
    if not doc:
        settings_col.insert_one(DEFAULTS)
        return dict(DEFAULTS)
    # backfill any missing keys (e.g. after adding a new toggle later)
    changed = False
    for k, v in DEFAULTS.items():
        if k not in doc:
            doc[k] = v
            changed = True
    if changed:
        settings_col.update_one({"_id": SETTINGS_ID}, {"$set": doc}, upsert=True)
    return doc


def set_inr_gateway(settings_col, gateway: str | None):
    """gateway: 'upi', 'fampay', or None to hide the INR button entirely."""
    settings_col.update_one({"_id": SETTINGS_ID}, {"$set": {"active_inr_gateway": gateway}}, upsert=True)


def toggle_binance(settings_col):
    s = get_settings(settings_col)
    new_val = not s.get("binance_enabled", True)
    settings_col.update_one({"_id": SETTINGS_ID}, {"$set": {"binance_enabled": new_val}}, upsert=True)
    return new_val


def toggle_crypto(settings_col):
    s = get_settings(settings_col)
    new_val = not s.get("crypto_enabled", True)
    settings_col.update_one({"_id": SETTINGS_ID}, {"$set": {"crypto_enabled": new_val}}, upsert=True)
    return new_val
