"""
Tests the Google Chat channel.

No Google project, no token, no network. Verification is stubbed where a
real check would need Google's certificates, and outbound posts are
captured instead of sent. It checks the parts that are easy to get wrong:
both event formats Google can send, who is allowed to sign a request, the
30-second window, the fallback when a model is slow, duplicate deliveries,
mention stripping, and reading the service account key.

Run:  python test_googlechat.py
"""

import asyncio
import base64
import json
import logging
import sys

from channels.googlechat import (
    CHAT_ISSUER, CHUNK, STILL_THINKING, WELCOME, GoogleChatChannel,
    load_service_account, parse_event, reply_body, split_text, trusted_sender,
)

logging.disable(logging.CRITICAL)

results = []


def check(label, condition, detail=""):
    results.append(condition)
    print(f"  {'PASS' if condition else 'FAIL'}  {label}")
    if not condition and detail:
        print(f"        {detail}")


class FakeAgent:
    name = "fake"

    def __init__(self, reply="the answer", delay=0.0):
        self.reply = reply
        self.delay = delay
        self.asked = []

    async def ask(self, message, context):
        self.asked.append((message, context.conversation_id))
        await asyncio.sleep(self.delay)
        return self.reply


class FakeRequest:
    def __init__(self, body=None, headers=None):
        self._body = json.dumps(body or {}).encode()
        self.headers = headers or {}

    async def json(self):
        return json.loads(self._body)

    async def read(self):
        return self._body


FAKE_KEY = {
    "type": "service_account",
    "private_key": "-----BEGIN PRIVATE KEY-----\nnot-real\n-----END PRIVATE KEY-----\n",
    "client_email": "bot@example.iam.gserviceaccount.com",
}


def make(agent, *, service_account=False, budget=5.0):
    channel = GoogleChatChannel(
        agent,
        audience="https://example.test/googlechat/webhook",
        service_account_info=FAKE_KEY if service_account else None,
        sync_budget=budget,
    )

    async def allow(request):
        return True

    channel._verify = allow

    posted = []

    async def capture(space_name, chunks, thread_name):
        posted.append((space_name, list(chunks), thread_name))

    channel._post_chunks = capture
    return channel, posted


def body_of(resp):
    return json.loads(resp.text) if resp.text else {}


def addon_text(resp):
    """Pull the reply text out of the Workspace add-on response shape."""
    action = (body_of(resp).get("hostAppDataAction") or {}).get("chatDataAction") or {}
    return ((action.get("createMessageAction") or {}).get("message") or {}).get("text")


# ------------------------------------------------------------- event builders

def message(text, *, argument_text=None, msg_name="spaces/AAA/messages/1",
            space="spaces/AAA", space_type="DM", thread="spaces/AAA/threads/t1"):
    """A plain Chat app event."""
    msg = {"name": msg_name, "text": text, "thread": {"name": thread}}
    if argument_text is not None:
        msg["argumentText"] = argument_text
    return {
        "type": "MESSAGE",
        "message": msg,
        "space": {"name": space, "type": space_type},
        "user": {"name": "users/42", "displayName": "Vishal"},
    }


def addon_message(text, *, argument_text=None, msg_name="spaces/BBB/messages/1",
                  space="spaces/BBB", thread="spaces/BBB/threads/t1"):
    """The same message as a Workspace add-on sends it."""
    msg = {"name": msg_name, "text": text, "thread": {"name": thread},
           "sender": {"name": "users/42", "displayName": "Vishal"}}
    if argument_text is not None:
        msg["argumentText"] = argument_text
    return {
        "commonEventObject": {"hostApp": "CHAT"},
        "chat": {
            "user": {"name": "users/42", "displayName": "Vishal"},
            "eventTime": "2026-09-13T10:00:00Z",
            "messagePayload": {"message": msg, "space": {"name": space}},
        },
    }


async def main():
    print("=" * 62)
    print("  Google Chat tests (no Google project, no network)")
    print("=" * 62)

    # --------------------------------------------------------- verification
    print("\n  Who may sign a request")
    check("Google Chat itself is trusted", trusted_sender(CHAT_ISSUER))
    check("a Workspace add-on service agent is trusted",
          trusted_sender("service-123456@gcp-sa-gsuiteaddons.iam.gserviceaccount.com"))
    check("an add-on from another project is refused when pinned",
          not trusted_sender("service-123456@gcp-sa-gsuiteaddons.iam.gserviceaccount.com",
                             project_number="999"))
    check("an add-on from our project passes when pinned",
          trusted_sender("service-999@gcp-sa-gsuiteaddons.iam.gserviceaccount.com",
                         project_number="999"))
    check("an ordinary account is refused", not trusted_sender("attacker@gmail.com"))
    check("a look-alike domain is refused",
          not trusted_sender("service-1@gcp-sa-gsuiteaddons.iam.gserviceaccount.com.evil.com"))
    check("no email is refused", not trusted_sender(None))

    real = GoogleChatChannel(FakeAgent(), audience="https://example.test/x")
    resp = await real.handle(FakeRequest(message("hi")))
    check("no Authorization header is refused", resp.status == 401)
    resp = await real.handle(FakeRequest(message("hi"), headers={"Authorization": "Basic abc"}))
    check("a non-Bearer header is refused", resp.status == 401)

    denied, _ = make(FakeAgent())

    async def deny(request):
        return False

    denied._verify = deny
    resp = await denied.handle(FakeRequest(message("hi")))
    check("an invalid token is refused", resp.status == 401)
    check("a refused request never reaches the agent", denied.agent.asked == [])

    # ------------------------------------------------------- reading events
    print("\n  Reading both event formats")
    p = parse_event(message("hello"))
    check("a Chat app event is recognised",
          p["format"] == "chat" and p["kind"] == "MESSAGE")
    p = parse_event(addon_message("hello"))
    check("an add-on event is recognised",
          p["format"] == "addon" and p["kind"] == "MESSAGE")
    check("the add-on message text is found", p["message"].get("text") == "hello")
    check("the add-on space is found", p["space"].get("name") == "spaces/BBB")
    p = parse_event({"chat": {"addedToSpacePayload": {"space": {"name": "spaces/C"}}}})
    check("an add-on 'added to space' is recognised", p["kind"] == "ADDED_TO_SPACE")
    p = parse_event({"chat": {"removedFromSpacePayload": {"space": {"name": "spaces/C"}}}})
    check("an add-on 'removed' is recognised", p["kind"] == "REMOVED_FROM_SPACE")
    check("an empty body is unrecognised", parse_event({})["kind"] is None)

    check("a Chat app reply is plain text", reply_body("chat", "x") == {"text": "x"})
    check("an add-on reply uses createMessageAction",
          reply_body("addon", "x")["hostAppDataAction"]["chatDataAction"]
          ["createMessageAction"]["message"]["text"] == "x")

    # ------------------------------------------------------ plain Chat app
    print("\n  Plain Chat app")
    channel, _ = make(FakeAgent())
    resp = await channel.handle(FakeRequest({"type": "ADDED_TO_SPACE",
                                             "space": {"name": "spaces/AAA"}}))
    check("greets when added to a space", body_of(resp).get("text") == WELCOME)
    resp = await channel.handle(FakeRequest({"type": "REMOVED_FROM_SPACE"}))
    check("stays quiet when removed", body_of(resp) == {})
    resp = await channel.handle(FakeRequest({"type": "SOMETHING_NEW"}))
    check("ignores an event type it does not know", body_of(resp) == {})
    resp = await channel.handle(FakeRequest(message("   ")))
    check("ignores an empty message", body_of(resp) == {})

    agent = FakeAgent("Tokyo.")
    channel, posted = make(agent)
    resp = await channel.handle(FakeRequest(message("capital of Japan?")))
    check("answers directly in the response", body_of(resp).get("text") == "Tokyo.",
          f"got {body_of(resp)}")
    check("sends nothing afterwards", posted == [])
    check("conversation id is namespaced",
          agent.asked[0][1] == "googlechat:spaces/AAA", f"got {agent.asked[0][1]}")

    agent = FakeAgent("4")
    channel, _ = make(agent)
    await channel.handle(FakeRequest(message(
        "@Inanovai Assistant what is 2+2", argument_text=" what is 2+2",
        msg_name="spaces/ROOM/messages/9", space="spaces/ROOM", space_type="ROOM")))
    check("reads the text without the @mention",
          agent.asked[0][0] == "what is 2+2", f"got {agent.asked[0][0]!r}")

    agent = FakeAgent()
    channel, _ = make(agent)
    await channel.handle(FakeRequest(message("hello", msg_name="spaces/AAA/messages/7")))
    resp = await channel.handle(FakeRequest(message("hello", msg_name="spaces/AAA/messages/7")))
    check("ignores a repeated delivery", len(agent.asked) == 1 and body_of(resp) == {})

    # -------------------------------------------------- Workspace add-on
    print("\n  Workspace add-on")
    agent = FakeAgent("Paris.")
    channel, posted = make(agent)
    resp = await channel.handle(FakeRequest(addon_message("capital of France?")))
    check("answers in the add-on reply shape", addon_text(resp) == "Paris.",
          f"got {body_of(resp)}")
    check("does not send a plain-text reply to an add-on", "text" not in body_of(resp))
    check("add-on conversation id is namespaced",
          agent.asked and agent.asked[0][1] == "googlechat:spaces/BBB",
          f"got {agent.asked}")

    agent = FakeAgent("ok")
    channel, _ = make(agent)
    await channel.handle(FakeRequest(addon_message(
        "@Inanovai Assistant hello", argument_text=" hello",
        msg_name="spaces/BBB/messages/2")))
    check("add-on reads the text without the @mention",
          agent.asked and agent.asked[0][0] == "hello", f"got {agent.asked}")

    channel, _ = make(FakeAgent())
    resp = await channel.handle(FakeRequest(
        {"chat": {"addedToSpacePayload": {"space": {"name": "spaces/BBB"}}}}))
    check("add-on greets when added", addon_text(resp) == WELCOME, f"got {body_of(resp)}")
    resp = await channel.handle(FakeRequest(
        {"chat": {"removedFromSpacePayload": {"space": {"name": "spaces/BBB"}}}}))
    check("add-on stays quiet when removed", body_of(resp) == {})

    agent = FakeAgent("late", delay=0.3)
    channel, _ = make(agent, service_account=False, budget=0.05)
    resp = await channel.handle(FakeRequest(addon_message("think", msg_name="spaces/BBB/messages/3")))
    check("add-on 'still thinking' uses the add-on shape",
          addon_text(resp) == STILL_THINKING, f"got {body_of(resp)}")
    await asyncio.sleep(0.5)

    # --------------------------------------------------------- slow replies
    print("\n  Replies slower than Google's window")
    agent = FakeAgent("a slow answer", delay=0.3)
    channel, posted = make(agent, service_account=True, budget=0.05)
    resp = await channel.handle(FakeRequest(message("think hard")))
    check("answers Google at once with nothing", body_of(resp) == {},
          f"got {body_of(resp)}")
    await asyncio.sleep(0.6)
    check("posts the answer afterwards",
          posted and posted[0][1] == ["a slow answer"], f"got {posted}")
    check("posts into the same thread",
          posted and posted[0][2] == "spaces/AAA/threads/t1")
    check("posts into the same space", posted and posted[0][0] == "spaces/AAA")
    check("nothing is left dangling", len(channel._pending) == 0)

    agent = FakeAgent("too late", delay=0.3)
    channel, posted = make(agent, service_account=False, budget=0.05)
    resp = await channel.handle(FakeRequest(message("think hard")))
    check("without a service account, says it is still thinking",
          body_of(resp).get("text") == STILL_THINKING, f"got {body_of(resp)}")
    await asyncio.sleep(0.6)
    check("and does not try to post", posted == [])

    agent = FakeAgent("x" * (CHUNK * 2 + 10))
    channel, posted = make(agent, service_account=True)
    resp = await channel.handle(FakeRequest(message("long please")))
    await asyncio.sleep(0.1)
    check("first part goes back directly", len(body_of(resp).get("text", "")) == CHUNK)
    check("the rest is posted afterwards",
          posted and len(posted[0][1]) == 2,
          f"got {[len(c) for c in posted[0][1]] if posted else posted}")

    # ------------------------------------------------------ service account
    print("\n  Service account key")
    raw = json.dumps(FAKE_KEY)
    check("reads raw JSON", load_service_account(raw) == FAKE_KEY)
    check("reads base64 JSON",
          load_service_account(base64.b64encode(raw.encode()).decode()) == FAKE_KEY)
    check("rejects garbage", load_service_account("not a key at all") is None)
    check("rejects a key that is not a service account",
          load_service_account(json.dumps({"type": "authorized_user"})) is None)
    check("treats blank as absent", load_service_account("   ") is None)
    check("splits long text into parts",
          [len(p) for p in split_text("y" * (CHUNK + 1))] == [CHUNK, 1])

    print("\n" + "=" * 62)
    print(f"  {sum(results)}/{len(results)} passed")
    print("=" * 62)
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    asyncio.run(main())
