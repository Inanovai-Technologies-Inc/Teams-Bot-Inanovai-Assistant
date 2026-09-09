"""
Tells each platform where to deliver messages.

Run this once after deploying, and again any time the public address changes.

    python register_channels.py

Telegram is registered automatically. WhatsApp cannot be — Meta requires you
to paste the URL into their dashboard by hand, so this prints what to paste.
"""

import asyncio
import os
import sys

from dotenv import load_dotenv

load_dotenv(override=True)

PUBLIC_URL = os.environ.get("PUBLIC_URL", "").strip().rstrip("/")


async def register_telegram() -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print("Telegram : skipped, no TELEGRAM_BOT_TOKEN in .env")
        return True

    from channels.telegram import TelegramChannel

    channel = TelegramChannel(
        agent=None,
        token=token,
        secret=os.environ.get("TELEGRAM_WEBHOOK_SECRET", "").strip() or None,
    )
    result = await channel.set_webhook(PUBLIC_URL)
    if not result.get("ok"):
        print(f"Telegram : FAILED -- {result.get('description')}")
        return False

    info = (await channel.get_webhook_info()).get("result", {})
    print(f"Telegram : registered at {info.get('url')}")
    if info.get("last_error_message"):
        print(f"           last error from Telegram: {info['last_error_message']}")
    return True


def show_whatsapp() -> None:
    token = os.environ.get("WHATSAPP_TOKEN", "").strip()
    verify = os.environ.get("WHATSAPP_VERIFY_TOKEN", "").strip()
    if not token:
        print("WhatsApp : skipped, no WHATSAPP_TOKEN in .env")
        return

    print("WhatsApp : paste these into developers.facebook.com")
    print(f"           Callback URL : {PUBLIC_URL}/whatsapp/webhook")
    print(f"           Verify token : {verify or '(set WHATSAPP_VERIFY_TOKEN first)'}")
    print("           Then subscribe to the 'messages' field.")


async def main() -> None:
    if not PUBLIC_URL:
        print("Set PUBLIC_URL in .env first, for example:")
        print("  PUBLIC_URL=https://inanovai-teams-bot.azurewebsites.net")
        sys.exit(1)
    if not PUBLIC_URL.startswith("https://"):
        print(f"PUBLIC_URL must start with https:// -- got {PUBLIC_URL}")
        print("Both Telegram and WhatsApp refuse plain http.")
        sys.exit(1)

    print("=" * 62)
    print(f"  Registering webhooks at {PUBLIC_URL}")
    print("=" * 62)

    ok = await register_telegram()
    show_whatsapp()

    print("=" * 62)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    asyncio.run(main())
