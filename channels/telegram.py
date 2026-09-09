"""
Telegram channel.

Telegram posts every message to a webhook you register with it. This file
receives that webhook, asks the agent, and sends the answer back.

Setup:
  1. Message @BotFather on Telegram, send /newbot, copy the token.
  2. Put it in .env as TELEGRAM_BOT_TOKEN.
  3. Point Telegram at this server:
       python register_channels.py
"""

import logging

import aiohttp
from aiohttp import web

from channels.base import ChannelBase

log = logging.getLogger("telegram")

API = "https://api.telegram.org"
WEBHOOK_PATH = "/telegram/webhook"


class TelegramChannel(ChannelBase):
    name = "telegram"

    def __init__(self, agent, token: str, secret: str | None = None, **kw):
        super().__init__(agent, **kw)
        self.token = token
        # Telegram echoes this header back on every webhook call, which is
        # how we know the request really came from Telegram.
        self.secret = secret

    # ---------------------------------------------------------------- routes

    def register(self, app: web.Application) -> None:
        app.router.add_post(WEBHOOK_PATH, self.handle)

    # ---------------------------------------------------------------- inbound

    async def handle(self, request: web.Request) -> web.Response:
        if self.secret:
            sent = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
            if sent != self.secret:
                log.warning("rejected a webhook call with a bad secret")
                return web.Response(status=403, text="forbidden")

        try:
            update = await request.json()
        except Exception:
            return web.Response(status=400, text="bad json")

        message = update.get("message") or update.get("edited_message")
        if not message:
            # Could be a join event, a reaction, an inline query. Nothing to do,
            # but answer 200 or Telegram keeps retrying.
            return web.Response(text="ok")

        text = (message.get("text") or "").strip()
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if not text or chat_id is None:
            return web.Response(text="ok")

        # Telegram re-sends an update if we do not answer quickly enough.
        update_id = update.get("update_id")
        if self.already_handled(update_id):
            log.info("ignoring duplicate update %s", update_id)
            return web.Response(text="ok")

        sender = message.get("from") or {}
        name = sender.get("first_name") or sender.get("username") or "there"

        # In a group, Telegram delivers every message. Only answer when the
        # bot is addressed, otherwise it would reply to the whole room.
        if chat.get("type") in ("group", "supergroup"):
            text = self._strip_mention(text, message)
            if text is None:
                return web.Response(text="ok")

        log.info("[%s] said: %s", name, text)

        await self.typing(chat_id)
        context = self.context_for(chat_id, sender.get("id"), name)
        reply = await self.reply_for(text, context)
        await self.send(chat_id, reply)

        return web.Response(text="ok")

    @staticmethod
    def _strip_mention(text: str, message: dict) -> str | None:
        """In groups, keep only messages that mention the bot, minus the mention.

        Returns None when the message was not addressed to the bot.
        """
        entities = message.get("entities") or []
        mentions = [e for e in entities if e.get("type") == "mention"]
        if not mentions:
            return None
        cleaned = text
        for e in sorted(mentions, key=lambda x: -x.get("offset", 0)):
            start, length = e.get("offset", 0), e.get("length", 0)
            cleaned = cleaned[:start] + cleaned[start + length:]
        cleaned = cleaned.strip()
        return cleaned or None

    # --------------------------------------------------------------- outbound

    async def _post(self, method: str, payload: dict) -> dict:
        url = f"{API}/bot{self.token}/{method}"
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, timeout=30) as resp:
                body = await resp.json(content_type=None)
                if not body.get("ok", False):
                    log.warning("%s failed: %s", method, body.get("description"))
                return body

    async def typing(self, chat_id) -> None:
        """Show '...' while the model thinks. Never breaks the real reply."""
        try:
            await self._post("sendChatAction", {"chat_id": chat_id, "action": "typing"})
        except Exception:
            log.debug("typing indicator failed", exc_info=True)

    async def send(self, chat_id, text: str) -> None:
        # Telegram rejects messages over 4096 characters.
        for chunk in [text[i:i + 4000] for i in range(0, len(text), 4000)] or [""]:
            await self._post("sendMessage", {"chat_id": chat_id, "text": chunk})

    # ------------------------------------------------------------- one-time

    async def set_webhook(self, public_url: str) -> dict:
        """Tell Telegram where to deliver messages."""
        payload = {
            "url": f"{public_url.rstrip('/')}{WEBHOOK_PATH}",
            "allowed_updates": ["message", "edited_message"],
        }
        if self.secret:
            payload["secret_token"] = self.secret
        return await self._post("setWebhook", payload)

    async def get_webhook_info(self) -> dict:
        return await self._post("getWebhookInfo", {})
