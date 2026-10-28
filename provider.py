"""Stub provider module for allapps.py compatibility."""
import os
from config import TEMPORASMS_API_KEY as ENV_KEY

BASE_URL = "https://api.temporasms.com/stubs/handler_api.php"
API_KEY = os.getenv("TEMPORASMS_API_KEY") or ENV_KEY or ""
RATES_API_KEY = os.getenv("TEMPORASMS_RATES_API_KEY") or API_KEY
MANUAL_CONFIG = []
