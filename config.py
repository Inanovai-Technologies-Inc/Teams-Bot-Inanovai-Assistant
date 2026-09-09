"""Configuration, read from environment variables (.env file is loaded for you)."""

import os

from dotenv import load_dotenv

# override=True so this project's .env wins over any stale system-wide
# variable of the same name. Without it, a leftover placeholder in the
# Windows user environment silently shadows the real key.
load_dotenv(override=True)


class Config:
    PORT = int(os.environ.get("PORT", 3978))

    # Leave these blank while testing locally.
    # Fill them in at Step 3 of the README, from the Azure Bot registration.
    APP_ID = os.environ.get("MICROSOFT_APP_ID", "")
    APP_PASSWORD = os.environ.get("MICROSOFT_APP_PASSWORD", "")
    APP_TYPE = os.environ.get("MICROSOFT_APP_TYPE", "MultiTenant")
    APP_TENANTID = os.environ.get("MICROSOFT_APP_TENANT_ID", "")

    # Stage 3: the agent. Without this the bot falls back to echo.
    HF_TOKEN = os.environ.get("HF_TOKEN", "")

    # --- Telegram (optional) ---
    TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")

    # --- WhatsApp, via the Meta Cloud API (optional) ---
    WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN", "")
    WHATSAPP_PHONE_NUMBER_ID = os.environ.get("WHATSAPP_PHONE_NUMBER_ID", "")
    WHATSAPP_VERIFY_TOKEN = os.environ.get("WHATSAPP_VERIFY_TOKEN", "")
    WHATSAPP_APP_SECRET = os.environ.get("WHATSAPP_APP_SECRET", "")

    # The public https address this bot is reachable at. Used when
    # registering the Telegram webhook.
    PUBLIC_URL = os.environ.get("PUBLIC_URL", "")
