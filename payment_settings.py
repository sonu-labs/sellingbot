"""Payment gateway settings stored in the ``settings`` collection.

The bot keeps gateway switches and public payment details in one document so an
admin can change them without a deployment.  Secrets (for example the Cwallet
API key) are deliberately *not* stored here; those belong in the bot's config
or environment.
"""

SETTINGS_ID = "payment_gateways"

# These are the networks that are offered by the settings screen first.  A
# custom network can also be added, because the exact network list can differ
# between Cwallet accounts.
SUPPORTED_USDT_NETWORKS = (
    "TRC20",
    "BEP20",
    "ERC20",
    "Polygon",
    "Arbitrum One",
    "Optimism",
    "Solana",
    "Avalanche C-Chain",
    "TON",
)

DEFAULTS = {
    "_id": SETTINGS_ID,
    "active_inr_gateway": "upi",   # "upi" | "fampay" | None
    "binance_enabled": True,
    # This is the parent Crypto menu.  The individual crypto methods below
    # can be switched off independently as well.
    "crypto_enabled": True,
    "usdt_enabled": True,
    "cwallet_manual_enabled": True,
    "cwallet_manual_id": "47588130",
    # Filled by the admin from /paymentsettings.  The keys are network labels
    # and the values are the corresponding USDT deposit addresses.
    "usdt_networks": {},
}


def get_settings(settings_col):
    """Return the settings document and backfill keys added in later versions."""
    doc = settings_col.find_one({"_id": SETTINGS_ID})
    if not doc:
        # Do not mutate DEFAULTS when a caller changes the returned document.
        fresh = dict(DEFAULTS)
        fresh["usdt_networks"] = {}
        settings_col.insert_one(fresh)
        return fresh

    changed = False
    patch = {}
    for key, value in DEFAULTS.items():
        if key not in doc:
            # Copy the mapping so a caller cannot mutate the module constant.
            doc[key] = dict(value) if isinstance(value, dict) else value
            patch[key] = doc[key]
            changed = True
    if changed:
        # Never include _id in a $set document: MongoDB treats it as an
        # immutable field and rejects the update.
        settings_col.update_one({"_id": SETTINGS_ID}, {"$set": patch}, upsert=True)
    return doc


def set_inr_gateway(settings_col, gateway: str | None):
    """Set the active INR gateway (``upi``, ``fampay`` or ``None``)."""
    if gateway not in {"upi", "fampay", None}:
        raise ValueError("gateway must be 'upi', 'fampay', or None")
    settings_col.update_one(
        {"_id": SETTINGS_ID},
        {"$set": {"active_inr_gateway": gateway}},
        upsert=True,
    )


def toggle_binance(settings_col):
    settings = get_settings(settings_col)
    new_value = not settings.get("binance_enabled", True)
    settings_col.update_one(
        {"_id": SETTINGS_ID},
        {"$set": {"binance_enabled": new_value}},
        upsert=True,
    )
    return new_value


def toggle_crypto(settings_col):
    settings = get_settings(settings_col)
    new_value = not settings.get("crypto_enabled", True)
    settings_col.update_one(
        {"_id": SETTINGS_ID},
        {"$set": {"crypto_enabled": new_value}},
        upsert=True,
    )
    return new_value


def toggle_usdt(settings_col):
    settings = get_settings(settings_col)
    new_value = not settings.get("usdt_enabled", True)
    settings_col.update_one(
        {"_id": SETTINGS_ID},
        {"$set": {"usdt_enabled": new_value}},
        upsert=True,
    )
    return new_value


def toggle_cwallet_manual(settings_col):
    settings = get_settings(settings_col)
    new_value = not settings.get("cwallet_manual_enabled", True)
    settings_col.update_one(
        {"_id": SETTINGS_ID},
        {"$set": {"cwallet_manual_enabled": new_value}},
        upsert=True,
    )
    return new_value


def get_usdt_networks(settings_col) -> dict[str, str]:
    """Return configured ``network -> address`` pairs.

    Older deployments may have no ``usdt_networks`` field yet.  Invalid or
    blank entries are ignored for the user-facing payment screen but are left
    untouched in the database by the admin helpers.
    """
    settings = get_settings(settings_col)
    raw = settings.get("usdt_networks") or {}

    # The normal representation is a mapping.  Accept a list of records too,
    # which makes migrations from an earlier prototype harmless.
    if isinstance(raw, dict):
        items = raw.items()
    elif isinstance(raw, list):
        items = (
            (item.get("network"), item.get("address"))
            for item in raw
            if isinstance(item, dict)
        )
    else:
        items = ()

    networks = {}
    for network, address in items:
        network = str(network or "").strip()
        address = str(address or "").strip()
        if network and address:
            networks[network] = address
    return networks


def set_usdt_network(settings_col, network: str, address: str):
    """Add or replace a USDT network address."""
    network = str(network or "").strip()
    address = str(address or "").strip()
    if not network:
        raise ValueError("network is required")
    if not address:
        raise ValueError("address is required")
    if len(network) > 48:
        raise ValueError("network name is too long")
    if len(address) > 256:
        raise ValueError("address is too long")

    # Read through the normalizer so a list-shaped value from an older
    # prototype is converted to the current mapping representation.
    get_settings(settings_col)
    networks = get_usdt_networks(settings_col)
    networks[network] = address
    settings_col.update_one(
        {"_id": SETTINGS_ID},
        {"$set": {"usdt_networks": networks}},
        upsert=True,
    )
    return networks


def remove_usdt_network(settings_col, network: str):
    """Remove a configured network, if it exists."""
    get_settings(settings_col)
    networks = get_usdt_networks(settings_col)
    networks.pop(str(network or "").strip(), None)
    settings_col.update_one(
        {"_id": SETTINGS_ID},
        {"$set": {"usdt_networks": networks}},
        upsert=True,
    )
    return networks


def set_cwallet_manual_id(settings_col, cwallet_id: str):
    cwallet_id = str(cwallet_id or "").strip()
    if not cwallet_id or len(cwallet_id) > 64:
        raise ValueError("invalid Cwallet ID")
    get_settings(settings_col)
    settings_col.update_one(
        {"_id": SETTINGS_ID},
        {"$set": {"cwallet_manual_id": cwallet_id}},
        upsert=True,
    )
    return cwallet_id