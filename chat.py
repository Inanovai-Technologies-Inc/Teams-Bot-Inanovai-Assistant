"""
Talk to your bot from the terminal, exactly the way Teams will.

This pretends to be Microsoft Teams, so every message goes through the
real path: your bot, the typing indicator, the agent, the reply.
If it works here, it will work in Teams.

Start the bot in one terminal:   python app.py
Then run this in another:        python chat.py

Type 'quit' to leave.
"""

import asyncio
import sys
import uuid

import aiohttp
from aiohttp import web

BOT_URL = "http://localhost:3978/api/messages"
FAKE_TEAMS_PORT = 3979
SERVICE_URL = f"http://localhost:{FAKE_TEAMS_PORT}"
CONVERSATION = "terminal-chat"

replies: asyncio.Queue = asyncio.Queue()


async def handle(request: web.Request) -> web.Response:
    """Receives whatever the bot sends back."""
    if request.method == "GET" and "/members/" in request.path:
        return web.json_response({"id": "user-vishal", "name": "Vishal"})
    if request.can_read_body:
        body = await request.json()
        if body.get("type") == "message":
            await replies.put(body.get("text", ""))
    return web.json_response({"id": str(uuid.uuid4())})


def make_message(text: str) -> dict:
    return {
        "type": "message",
        "id": str(uuid.uuid4()),
        "channelId": "msteams",
        "serviceUrl": SERVICE_URL,
        "from": {"id": "user-vishal", "name": "Vishal"},
        "conversation": {"id": CONVERSATION},
        "recipient": {"id": "bot-1", "name": "Bot"},
        "channelData": {"tenant": {"id": "tenant-1"}},
        "text": text,
    }


async def main() -> None:
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "localhost", FAKE_TEAMS_PORT).start()

    async with aiohttp.ClientSession() as session:
        # Confirm the bot is up, and say which brain it is using.
        try:
            async with session.get("http://localhost:3978/") as resp:
                agent = (await resp.json()).get("agent")
        except Exception:
            print("\n  The bot is not running.")
            print("  Open another terminal and run:  python app.py\n")
            await runner.cleanup()
            sys.exit(1)

        print("=" * 62)
        print(f"  Talking to your bot  (agent: {agent})")
        print("  Type a message and press Enter. Type 'quit' to leave.")
        print("=" * 62)

        loop = asyncio.get_event_loop()
        while True:
            try:
                text = await loop.run_in_executor(None, input, "\nYou > ")
            except (EOFError, KeyboardInterrupt):
                break

            text = text.strip()
            if not text:
                continue
            if text.lower() in ("quit", "exit", "bye"):
                break

            async with session.post(BOT_URL, json=make_message(text)) as resp:
                if resp.status not in (200, 201, 202):
                    print(f"  ! bot returned HTTP {resp.status}")
                    continue

            print("Bot > ", end="", flush=True)
            try:
                answer = await asyncio.wait_for(replies.get(), timeout=90)
                print(answer)
            except asyncio.TimeoutError:
                print("(no reply within 90 seconds)")

    await runner.cleanup()
    print("\nBye.\n")


if __name__ == "__main__":
    asyncio.run(main())
