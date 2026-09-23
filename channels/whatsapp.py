"""
WhatsApp channel, using the Meta WhatsApp Cloud API.

Meta posts every message to a webhook you register in the Meta dashboard.
This file answers Meta's verification challenge, receives the webhook,
asks the agent, and sends the answer back.

Setup:
  1. developers.facebook.com -> create an app -> add WhatsApp.
  2. Copy the temporary access token and the Phone number ID.
  3. Put them in .env as WHATSAPP_TOKEN and WHATSAPP_PHONE_NUMBER_ID.
  4. Invent any string for WHATSAPP_VERIFY_TOKEN and put it in .env too.
  5. In the dashboard, set the callback URL to
       https://<your-host>/whatsapp/webhook
     and the verify token to the same string. Subscribe to "messages".
"""

import hashlib
import hmac
import logging

import aiohttp
from aiohttp import web

from channels.base import ChannelBase

log = logging.getLogger("whatsapp")

GRAPH = "https://graph.facebook.com/v21.0"
WEBHOOK_PATH = "/whatsapp/webhook"


class WhatsAppChannel(ChannelBase):
    name = "whatsapp"

    def __init__(self, agent, token: str, phone_number_id: str,
                 verify_token: str, app_secret: str | None = None, **kw):
        super().__init__(agent, **kw)
        self.token = token
        self.phone_number_id = phone_number_id
        self.verify_token = verify_token
        # Optional but recommended: Meta signs every webhook body with this.
        self.app_secret = app_secret

    # ---------------------------------------------------------------- routes

    def register(self, app: web.Application) -> None:
        app.router.add_get(WEBHOOK_PATH, self.verify)
        app.router.add_post(WEBHOOK_PATH, self.handle)

    # ------------------------------------------------------------ verification

    async def verify(self, request: web.Request) -> web.Response:
        """Meta calls this once, when you save the webhook URL."""
        params = request.rel_url.query
        if (params.get("hub.mode") == "subscribe"
                and params.get("hub.verify_token") == self.verify_token):
            log.info("webhook verified by Meta")
            return web.Response(text=params.get("hub.challenge", ""))
        log.warning("webhook verification failed")
        return web.Response(status=403, text="verification failed")

    def _signature_ok(self, raw: bytes, header: str | None) -> bool:
        if not self.app_secret:
            return True  # not configured, so nothing to check
        if not header or not header.startswith("sha256="):
            return False
        expected = hmac.new(
            self.app_secret.encode(), raw, hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected, header[7:])

    # ---------------------------------------------------------------- inbound

    async def handle(self, request: web.Request) -> web.Response:
        raw = await request.read()
        if not self._signature_ok(raw, request.headers.get("X-Hub-Signature-256")):
            log.warning("rejected a webhook call with a bad signature")
            return web.Response(status=403, text="bad signature")

        try:
            body = await request.json()
        except Exception:
            return web.Response(status=400, text="bad json")

        for message, contact in self._messages(body):
            await self._handle_one(message, contact)

        # Always 200, or Meta retries and eventually disables the webhook.
        return web.Response(text="ok")

    def _messages(self, body: dict):
        """Walk Meta's deeply nested payload and yield (message, contact).

        A WhatsApp Business account can hold several numbers -- ours may
        share one with the ERP integration -- and Meta sends every app the
        messages for all of them. Only messages sent to the bot's own
        number are answered; everything else is left alone.
        """
        for entry in body.get("entry", []) or []:
            for change in entry.get("changes", []) or []:
                value = change.get("value") or {}
                to_number = (value.get("metadata") or {}).get("phone_number_id")
                if to_number and str(to_number) != str(self.phone_number_id):
                    if value.get("messages"):
                        log.info("ignoring a message sent to another number (%s)", to_number)
                    continue
                contacts = value.get("contacts") or []
                contact = contacts[0] if contacts else {}
                for message in value.get("messages", []) or []:
                    yield message, contact

    async def _handle_one(self, message: dict, contact: dict) -> None:
        if message.get("type") != "text":
            # Images, audio, stickers -- tell the user rather than ignore them.
            sender = message.get("from")
            if sender:
                await self.send(sender, "I can only read text messages right now.")
            return

        message_id = message.get("id")
        if self.already_handled(message_id):
            log.info("ignoring duplicate message %s", message_id)
            return

        text = ((message.get("text") or {}).get("body") or "").strip()
        sender = message.get("from")
        if not text or not sender:
            return

        name = ((contact.get("profile") or {}).get("name")) or "there"
        log.info("[%s] said: %s", name, text)

        await self.mark_read(message_id)
        context = self.context_for(sender, sender, name)
        reply = await self.reply_for(text, context)
        await self.send(sender, reply)

    # --------------------------------------------------------------- outbound

    async def _post(self, payload: dict) -> dict:
        url = f"{GRAPH}/{self.phone_number_id}/messages"
        headers = {"Authorization": f"Bearer {self.token}"}
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=headers,
                                    timeout=30) as resp:
                body = await resp.json(content_type=None)
                if resp.status >= 400:
                    log.warning("send failed (%s): %s", resp.status, body)
                return body

    async def mark_read(self, message_id: str) -> None:
        """Show the blue ticks. Cosmetic, so never let it break a reply."""
        try:
            await self._post({
                "messaging_product": "whatsapp",
                "status": "read",
                "message_id": message_id,
            })
        except Exception:
            log.debug("mark read failed", exc_info=True)

    async def send(self, to: str, text: str) -> None:
        # WhatsApp rejects bodies over 4096 characters.
        for chunk in [text[i:i + 4000] for i in range(0, len(text), 4000)] or [""]:
            await self._post({
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": to,
                "type": "text",
                "text": {"preview_url": False, "body": chunk},
            })
