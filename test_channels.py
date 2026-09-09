"""
Tests the Telegram and WhatsApp channels.

No tokens, no network, no cost. A fake agent answers instantly and every
outbound call is captured instead of sent, so this checks the parts that
are easy to get wrong: payload parsing, duplicate guards, group mentions,
webhook verification, and message splitting.

Run:  python test_channels.py
"""

import asyncio
import json
import logging
import sys
from types import SimpleNamespace

from channels.telegram import TelegramChannel
from channels.whatsapp import WhatsAppChannel

logging.disable(logging.CRITICAL)

results = []


def check(label, condition, detail=""):
    results.append(condition)
    print(f"  {'PASS' if condition else 'FAIL'}  {label}")
    if not condition and detail:
        print(f"        {detail}")


class FakeAgent:
    """Answers instantly and records what it was asked."""

    name = "fake"

    def __init__(self, reply="the answer"):
        self.reply = reply
        self.asked = []

    async def ask(self, message, context):
        self.asked.append((message, context.conversation_id))
        return self.reply


class FakeRequest:
    """Stands in for an aiohttp request."""

    def __init__(self, body=None, headers=None, query=None):
        self._body = json.dumps(body or {}).encode()
        self.headers = headers or {}
        self.rel_url = SimpleNamespace(query=query or {})

    async def json(self):
        return json.loads(self._body)

    async def read(self):
        return self._body


def patch_outbound(channel):
    """Capture what the channel would have sent."""
    sent = []

    async def fake_post(*args, **kwargs):
        sent.append((args, kwargs))
        return {"ok": True}

    if isinstance(channel, TelegramChannel):
        async def tg_post(method, payload):
            sent.append((method, payload))
            return {"ok": True}
        channel._post = tg_post
    else:
        async def wa_post(payload):
            sent.append(("messages", payload))
            return {"messages": [{"id": "x"}]}
        channel._post = wa_post
    return sent


def telegram_update(text, update_id=1, chat_id=555, chat_type="private",
                    entities=None):
    msg = {
        "message_id": 10,
        "text": text,
        "chat": {"id": chat_id, "type": chat_type},
        "from": {"id": 99, "first_name": "Vishal"},
    }
    if entities:
        msg["entities"] = entities
    return {"update_id": update_id, "message": msg}


def whatsapp_body(text, msg_id="wamid.1", msg_type="text"):
    message = {"id": msg_id, "from": "919999999999", "type": msg_type}
    if msg_type == "text":
        message["text"] = {"body": text}
    return {
        "entry": [{
            "changes": [{
                "value": {
                    "contacts": [{"profile": {"name": "Vishal"}}],
                    "messages": [message],
                }
            }]
        }]
    }


async def main():
    print("=" * 62)
    print("  Channel tests (fake agent, no network, no tokens)")
    print("=" * 62)

    # ------------------------------------------------------------ Telegram
    print("\n  Telegram")
    agent = FakeAgent("Tokyo.")
    tg = TelegramChannel(agent, token="fake-token")
    sent = patch_outbound(tg)

    resp = await tg.handle(FakeRequest(telegram_update("capital of Japan?")))
    check("answers a direct message", resp.status == 200)
    check("passed the text to the agent",
          agent.asked and agent.asked[0][0] == "capital of Japan?",
          f"got {agent.asked}")
    check("sent the reply back",
          any(m == "sendMessage" and p.get("text") == "Tokyo." for m, p in sent),
          f"got {sent}")
    check("showed the typing action",
          any(m == "sendChatAction" for m, _ in sent))
    check("conversation id is namespaced",
          agent.asked[0][1] == "telegram:555", f"got {agent.asked[0][1]}")

    # duplicate update
    before = len(agent.asked)
    await tg.handle(FakeRequest(telegram_update("capital of Japan?", update_id=1)))
    check("ignores a repeated update", len(agent.asked) == before)

    # a genuinely new update still works
    await tg.handle(FakeRequest(telegram_update("and Brazil?", update_id=2)))
    check("a new update is answered", len(agent.asked) == before + 1)

    # group chat without a mention is ignored
    before = len(agent.asked)
    await tg.handle(FakeRequest(telegram_update(
        "just chatting", update_id=3, chat_type="group")))
    check("ignores group chatter with no mention", len(agent.asked) == before)

    # group chat with a mention is answered, mention removed
    await tg.handle(FakeRequest(telegram_update(
        "@thebot what is 2+2", update_id=4, chat_type="group",
        entities=[{"type": "mention", "offset": 0, "length": 7}])))
    check("answers when mentioned in a group", len(agent.asked) == before + 1)
    check("strips the mention",
          agent.asked[-1][0] == "what is 2+2", f"got {agent.asked[-1][0]!r}")

    # non-message updates are shrugged off
    resp = await tg.handle(FakeRequest({"update_id": 9, "poll": {}}))
    check("survives a non-message update", resp.status == 200)

    # bad secret is rejected
    tg_secret = TelegramChannel(FakeAgent(), token="t", secret="s3cret")
    patch_outbound(tg_secret)
    resp = await tg_secret.handle(FakeRequest(telegram_update("hi"),
                                              headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"}))
    check("rejects a bad webhook secret", resp.status == 403)

    # long replies are split
    agent_long = FakeAgent("x" * 9000)
    tg_long = TelegramChannel(agent_long, token="t")
    sent_long = patch_outbound(tg_long)
    await tg_long.handle(FakeRequest(telegram_update("long please", update_id=77)))
    msgs = [p for m, p in sent_long if m == "sendMessage"]
    check("splits an over-long reply", len(msgs) == 3, f"got {len(msgs)} messages")

    # ------------------------------------------------------------ WhatsApp
    print("\n  WhatsApp")
    agent = FakeAgent("42.")
    wa = WhatsAppChannel(agent, token="t", phone_number_id="123",
                         verify_token="verify-me")
    sent = patch_outbound(wa)

    # verification handshake
    resp = await wa.verify(FakeRequest(query={
        "hub.mode": "subscribe", "hub.verify_token": "verify-me",
        "hub.challenge": "abc123"}))
    check("passes Meta's verification", resp.text == "abc123", f"got {resp.text}")

    resp = await wa.verify(FakeRequest(query={
        "hub.mode": "subscribe", "hub.verify_token": "wrong",
        "hub.challenge": "abc123"}))
    check("rejects a wrong verify token", resp.status == 403)

    # a real message
    resp = await wa.handle(FakeRequest(whatsapp_body("what is 6 times 7")))
    check("answers a message", resp.status == 200)
    check("passed the text to the agent",
          agent.asked and agent.asked[0][0] == "what is 6 times 7",
          f"got {agent.asked}")
    check("sent the reply back",
          any(p.get("type") == "text" and p["text"]["body"] == "42."
              for _, p in sent), f"got {sent}")
    check("marked it read", any(p.get("status") == "read" for _, p in sent))
    check("conversation id is namespaced",
          agent.asked[0][1] == "whatsapp:919999999999",
          f"got {agent.asked[0][1]}")

    # duplicate delivery
    before = len(agent.asked)
    await wa.handle(FakeRequest(whatsapp_body("what is 6 times 7")))
    check("ignores a repeated message", len(agent.asked) == before)

    # non-text message gets a polite answer, not silence
    sent.clear()
    await wa.handle(FakeRequest(whatsapp_body("", msg_id="wamid.2",
                                              msg_type="image")))
    check("explains it cannot read images",
          any("text messages" in str(p) for _, p in sent), f"got {sent}")

    # empty payload does not explode
    resp = await wa.handle(FakeRequest({"entry": []}))
    check("survives an empty payload", resp.status == 200)

    # bad signature is rejected when a secret is configured
    wa_signed = WhatsAppChannel(FakeAgent(), token="t", phone_number_id="1",
                                verify_token="v", app_secret="shhh")
    patch_outbound(wa_signed)
    resp = await wa_signed.handle(FakeRequest(
        whatsapp_body("hi"), headers={"X-Hub-Signature-256": "sha256=deadbeef"}))
    check("rejects a bad signature", resp.status == 403)

    print("\n" + "=" * 62)
    print(f"  {sum(results)}/{len(results)} passed")
    print("=" * 62)
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    asyncio.run(main())
