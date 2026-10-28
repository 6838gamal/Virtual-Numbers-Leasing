"""كل إعدادات التطبيق ومتغيرات البيئة في مكان واحد."""
from __future__ import annotations

import os
import secrets
from pathlib import Path

# ══════════════════════════════════════════════════════════════
# Paths
# ══════════════════════════════════════════════════════════════
BASE_DIR = Path(__file__).resolve().parent.parent
APP_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = APP_DIR / "static"

DEBUG = os.getenv("DEBUG", "false").lower() == "true"

# ══════════════════════════════════════════════════════════════
# Session
# ══════════════════════════════════════════════════════════════
SECRET_KEY = os.getenv("SECRET_KEY", "").strip()
_SECRET_KEY_FROM_ENV = bool(SECRET_KEY)

if not SECRET_KEY:
    _dev_secret_file = Path(".dev_secret_key")
    if _dev_secret_file.exists():
        SECRET_KEY = _dev_secret_file.read_text().strip()
    else:
        SECRET_KEY = secrets.token_urlsafe(48)
        _dev_secret_file.write_text(SECRET_KEY)
        print(
            "⚠️  SECRET_KEY غير مضبوط في البيئة. تم توليد واحد ثابت في "
            ".dev_secret_key — لا تستخدم هذا في الإنتاج."
        )

SESSION_COOKIE_SECURE = os.getenv("SESSION_COOKIE_SECURE", "false").lower() == "true"
SESSION_MAX_AGE = int(os.getenv("SESSION_MAX_AGE", str(30 * 24 * 3600)))
SESSION_IDLE_TIMEOUT = int(os.getenv("SESSION_IDLE_TIMEOUT", str(7 * 24 * 3600)))

# ══════════════════════════════════════════════════════════════
# Guest Mode
# ══════════════════════════════════════════════════════════════
GUEST_MODE = os.getenv("GUEST_MODE", "true").lower() == "true"
GUEST_REQUIRES_VOUCHER = os.getenv("GUEST_REQUIRES_VOUCHER", "true").lower() == "true"
HIDE_BALANCE_FROM_CUSTOMERS = os.getenv("HIDE_BALANCE_FROM_CUSTOMERS", "true").lower() == "true"
GUEST_CUSTOMER_ID = int(os.getenv("GUEST_CUSTOMER_ID", "0"))

# ══════════════════════════════════════════════════════════════
# 5SIM
# ══════════════════════════════════════════════════════════════
SIM5_BASE = os.getenv("SIM5_BASE", "https://5sim.net/v1").rstrip("/")
SIM5_API_KEY = os.getenv("SIM5_API_KEY", "").strip()
SIM5_ENABLED = bool(SIM5_API_KEY)
FIVESIM_MARKUP_PERCENT = float(os.getenv("FIVESIM_MARKUP_PERCENT", "40"))

HIDE_PRICES_FROM_CUSTOMERS = (
    os.getenv("HIDE_PRICES_FROM_CUSTOMERS", "true").lower() == "true"
    or (GUEST_MODE and HIDE_BALANCE_FROM_CUSTOMERS)
)

ADMIN_SECRET = os.getenv("ADMIN_SECRET", "").strip()
VOUCHER_PREFIX = os.getenv("VOUCHER_PREFIX", "JS").strip().upper()
ACCESS_CODE_PREFIX = os.getenv("ACCESS_CODE_PREFIX", "JS-LOGIN").strip().upper()

# ══════════════════════════════════════════════════════════════
# Blocked countries
# ══════════════════════════════════════════════════════════════
BLOCKED_COUNTRY_SLUGS = {
    s.strip().lower()
    for s in os.getenv("BLOCKED_COUNTRY_SLUGS", "israel").split(",")
    if s.strip()
}
BLOCKED_COUNTRY_CODES = {
    s.strip().upper()
    for s in os.getenv("BLOCKED_COUNTRY_CODES", "IL").split(",")
    if s.strip()
}

# ══════════════════════════════════════════════════════════════
# Keepalive
# ══════════════════════════════════════════════════════════════
KA_INTERVAL = 420
HISTORY_MAX = 20

# ══════════════════════════════════════════════════════════════
# Routes
# ══════════════════════════════════════════════════════════════
PUBLIC_PATHS = (
    "/health", "/status", "/logout", "/auth/logout",
    "/static", "/favicon.ico",
    "/api/v1/system", "/api/v1/prices", "/api/v1/popular",
    "/api/v1/auth/login-with-code", "/api/v1/vouchers/check",
)

PROTECTED_PREFIXES = (
    "/buy", "/dashboard", "/catalog", "/my-numbers", "/orders",
    "/inbox", "/wallet", "/transactions", "/support", "/profile",
    "/security", "/admin-panel",
)
