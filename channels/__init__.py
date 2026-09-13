"""
Channels -- the places people can talk to the bot.

Each channel translates one platform into plain text and hands it to the
agent. None of them contain any intelligence, and the agent knows about
none of them.

Add a channel by writing a class with `register(app)` and listing it in
`build_channels()` below.
"""

import logging
import os

log = logging.getLogger("channels")


def build_channels(agent):
    """Return the channels that have credentials configured.

    A channel with no token is skipped silently, so the bot always starts
    with whatever is available.
    """
    active = []

    telegram_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if telegram_token:
        from channels.telegram import TelegramChannel
        active.append(TelegramChannel(
            agent,
            token=telegram_token,
            secret=os.environ.get("TELEGRAM_WEBHOOK_SECRET", "").strip() or None,
        ))

    wa_token = os.environ.get("WHATSAPP_TOKEN", "").strip()
    wa_phone = os.environ.get("WHATSAPP_PHONE_NUMBER_ID", "").strip()
    wa_verify = os.environ.get("WHATSAPP_VERIFY_TOKEN", "").strip()
    if wa_token and wa_phone and wa_verify:
        from channels.whatsapp import WhatsAppChannel
        active.append(WhatsAppChannel(
            agent,
            token=wa_token,
            phone_number_id=wa_phone,
            verify_token=wa_verify,
            app_secret=os.environ.get("WHATSAPP_APP_SECRET", "").strip() or None,
        ))
    elif wa_token or wa_phone or wa_verify:
        log.warning(
            "WhatsApp is only half configured -- needs WHATSAPP_TOKEN, "
            "WHATSAPP_PHONE_NUMBER_ID and WHATSAPP_VERIFY_TOKEN. Skipping."
        )

    gchat_audience = os.environ.get("GOOGLE_CHAT_AUDIENCE", "").strip()
    if gchat_audience:
        from channels.googlechat import GoogleChatChannel, load_service_account
        key = load_service_account(
            os.environ.get("GOOGLE_CHAT_SERVICE_ACCOUNT_JSON", ""))
        if key is None:
            log.warning(
                "Google Chat has no service account -- answers slower than "
                "Google's 30 second window get a 'still thinking' note."
            )
        active.append(GoogleChatChannel(
            agent, audience=gchat_audience, service_account_info=key,
            project_number=os.environ.get("GOOGLE_CHAT_PROJECT_NUMBER", "").strip() or None))

    return active
