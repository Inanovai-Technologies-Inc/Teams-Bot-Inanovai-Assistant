"""
Test the bot WITHOUT Teams, WITHOUT Azure, WITHOUT a tunnel.

This pretends to be Microsoft Teams:
  1. It starts a tiny server that catches whatever the bot replies.
  2. It sends the bot a series of realistic Teams events.
  3. It prints what came back, and says PASS or FAIL.

Run it in a SECOND terminal while `python app.py` is running:

    python test_local.py
"""

import asyncio
import sys
import uuid

import aiohttp
from aiohttp import web

BOT_URL = "http://localhost:3978/api/messages"
FAKE_TEAMS_PORT = 3979
SERVICE_URL = f"http://localhost:{FAKE_TEAMS_PORT}"

replies: asyncio.Queue = asyncio.Queue()
typing_seen: list = []


# --------------------------------------------------------------------------
# The fake Teams service
# --------------------------------------------------------------------------

async def handle(request: web.Request) -> web.Response:
    """Stands in for the Teams REST API the bot calls back into."""
    # The bot looks up member details on this route. Answer like Teams would.
    if request.method == "GET" and "/members/" in request.path:
        return web.json_response(
            {"id": "user-vishal", "name": "Vishal", "objectId": "aad-1"}
        )

    # Everything else is the bot posting a reply to the conversation.
    if request.can_read_body:
        body = await request.json()
        if body.get("type") == "message":
            await replies.put(body.get("text", "<no text>"))
        elif body.get("type") == "typing":
            typing_seen.append(True)

    return web.json_response({"id": str(uuid.uuid4())})


async def start_fake_teams() -> web.AppRunner:
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "localhost", FAKE_TEAMS_PORT).start()
    return runner


# --------------------------------------------------------------------------
# Activities, shaped the way real Teams sends them
# --------------------------------------------------------------------------

def base_activity() -> dict:
    return {
        "id": str(uuid.uuid4()),
        "timestamp": "2026-01-01T00:00:00.000Z",
        "channelId": "msteams",
        "serviceUrl": SERVICE_URL,
        "from": {"id": "user-vishal", "name": "Vishal"},
        "conversation": {"id": "conversation-1"},
        "recipient": {"id": "bot-1", "name": "EchoBot"},
        "channelData": {"tenant": {"id": "tenant-1"}},
        "locale": "en-US",
    }


def message(text: str) -> dict:
    return dict(base_activity(), type="message", text=text)


def channel_mention(text: str) -> dict:
    """A message in a channel. Teams prefixes it with the bot's @mention."""
    activity = dict(
        base_activity(), type="message", text=f"<at>EchoBot</at> {text}"
    )
    activity["entities"] = [
        {
            "type": "mention",
            "text": "<at>EchoBot</at>",
            "mentioned": {"id": "bot-1", "name": "EchoBot"},
        }
    ]
    return activity


def bot_added() -> dict:
    """Sent once, when the bot is first installed into a chat."""
    return dict(
        base_activity(),
        type="conversationUpdate",
        membersAdded=[{"id": "user-vishal", "name": "Vishal"}],
    )


def bot_added_no_channel_data() -> dict:
    """Same, but with channelData missing -- as some clients send it."""
    activity = bot_added()
    activity.pop("channelData")
    return activity


# --------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------

async def check(session, label: str, activity: dict, expect: str) -> bool:
    while not replies.empty():          # drain anything left over
        replies.get_nowait()

    async with session.post(BOT_URL, json=activity) as resp:
        if resp.status not in (200, 201, 202):
            print(f"  FAIL  {label}\n        HTTP {resp.status}: {await resp.text()}")
            return False

    try:
        answer = await asyncio.wait_for(replies.get(), timeout=10)
    except asyncio.TimeoutError:
        print(f"  FAIL  {label}\n        no reply within 10s")
        return False

    ok = expect.lower() in answer.lower()
    flat = answer.replace("\n", " ")
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    print(f"        bot said: {flat[:90]}")
    if not ok:
        print(f"        expected to contain: {expect}")
    return ok


async def which_agent(session) -> str:
    """Ask the running bot which agent is live, so we assert the right thing."""
    try:
        async with session.get("http://localhost:3978/") as resp:
            return (await resp.json()).get("agent", "unknown")
    except Exception:
        return "unknown"


async def main() -> None:
    runner = await start_fake_teams()
    print("=" * 62)
    print("  Pretending to be Microsoft Teams")
    print(f"  Talking to: {BOT_URL}")
    print("=" * 62)

    results = []
    async with aiohttp.ClientSession() as session:
        agent = await which_agent(session)
        print(f"  bot is running the '{agent}' agent")
        print("-" * 62)

        # These hold no matter which agent is live.
        cases = [
            ("empty message",            message("   "),               "empty message"),
            ("welcome on install",       bot_added(),                  "I'm your"),
            ("welcome without channelData", bot_added_no_channel_data(), "I'm your"),
        ]

        if agent == "echo":
            # The echo bot's replies are fixed, so assert them exactly.
            cases += [
                ("greeting",              message("hi"),                    "Hello Vishal"),
                ("ping command",          message("ping"),                  "pong"),
                ("help command",          message("help"),                  "echo bot"),
                ("echoes arbitrary text", message("Book a meeting at 4pm"),  "You said: Book a meeting at 4pm"),
                ("strips @mention",       channel_mention("what is status"), "You said: what is status"),
            ]
        else:
            # A real model writes its own words, so only assert it answered.
            cases += [
                ("answers a question",  message("What is 2+2? Reply with just the number."), "4"),
                ("remembers context",   message("Add 3 to that. Reply with just the number."), "7"),
                ("reset clears memory", message("reset"),                                     "fresh"),
                ("strips @mention",     channel_mention("say the word banana and nothing else"), "banana"),
            ]

        for label, activity, expect in cases:
            results.append(await check(session, label, activity, expect))
    await runner.cleanup()

    # The typing bubble is sent as a separate activity before the reply.
    saw_typing = len(typing_seen) > 0
    results.append(saw_typing)
    print(f"  {'PASS' if saw_typing else 'FAIL'}  sends the typing bubble")
    print(f"        {len(typing_seen)} typing activities received")

    passed, total = sum(results), len(results)
    print("=" * 62)
    print(f"  {passed}/{total} passed")
    print("=" * 62)
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    asyncio.run(main())
