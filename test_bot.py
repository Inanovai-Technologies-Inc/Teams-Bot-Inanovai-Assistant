"""
Tests the Teams layer -- the typing bubble, the "still working" note,
the timeout, and the duplicate-message guard.

Uses a fake Teams conversation and a fake agent whose speed we control,
so nothing here needs Teams, Azure, or an API key. Runs in a few seconds.

Run:  python test_bot.py
"""

import asyncio
import logging
import sys

from botbuilder.schema import Activity, ActivityTypes, ChannelAccount, ConversationAccount

import bot as bot_module
from bot import TeamsBot

# Several tests deliberately crash or hang the agent. Silence the
# resulting log noise so the PASS/FAIL list stays readable.
logging.disable(logging.CRITICAL)

# Shrink the waits so the tests finish quickly instead of in real time.
bot_module.TYPING_INTERVAL = 0.05
bot_module.SLOW_REPLY_AFTER = 0.20
bot_module.AGENT_TIMEOUT = 0.50


class SlowAgent:
    """A fake agent that takes exactly as long as we tell it to."""

    name = "slow"

    def __init__(self, delay=0.0, reply="done", raises=False):
        self.delay = delay
        self.reply = reply
        self.raises = raises
        self.calls = 0

    async def ask(self, message, context):
        self.calls += 1
        await asyncio.sleep(self.delay)
        if self.raises:
            raise RuntimeError("agent exploded")
        return self.reply


class FakeTurnContext:
    """Records everything the bot tries to send."""

    def __init__(self, activity):
        self.activity = activity
        self.sent = []

    async def send_activity(self, activity):
        self.sent.append(activity)
        return None

    @property
    def typing_count(self):
        return sum(1 for a in self.sent if a.type == ActivityTypes.typing)

    @property
    def texts(self):
        return [a.text for a in self.sent if a.type == ActivityTypes.message]


def make_activity(text="hello", activity_id="a1"):
    return Activity(
        type=ActivityTypes.message,
        id=activity_id,
        text=text,
        channel_id="msteams",
        from_property=ChannelAccount(id="user-vishal", name="Vishal"),
        recipient=ChannelAccount(id="bot-1", name="EchoBot"),
        conversation=ConversationAccount(id="c1"),
    )


def make_bot(agent):
    bot = TeamsBot()
    bot.agent = agent
    return bot


results = []


def check(label, condition, detail=""):
    results.append(condition)
    print(f"  {'PASS' if condition else 'FAIL'}  {label}")
    if not condition and detail:
        print(f"        {detail}")


async def main():
    print("=" * 62)
    print("  Teams-layer tests (fake Teams, fake agent, no API key)")
    print("=" * 62)

    # --- a fast answer: typing shown, no "still working" noise ---
    ctx = FakeTurnContext(make_activity())
    await make_bot(SlowAgent(delay=0, reply="quick answer")).on_message_activity(ctx)
    check("shows the typing bubble", ctx.typing_count >= 1,
          f"typing sent {ctx.typing_count} times")
    check("sends the answer", ctx.texts == ["quick answer"], f"got {ctx.texts}")
    check("no 'still working' when fast",
          not any("Still working" in t for t in ctx.texts))

    # --- a slow answer: bubble refreshes, user gets told ---
    ctx = FakeTurnContext(make_activity())
    await make_bot(SlowAgent(delay=0.35, reply="slow answer")).on_message_activity(ctx)
    check("refreshes the typing bubble", ctx.typing_count >= 2,
          f"typing sent {ctx.typing_count} times")
    check("says 'still working' when slow",
          any("Still working" in t for t in ctx.texts), f"got {ctx.texts}")
    check("still delivers the answer", ctx.texts[-1] == "slow answer",
          f"got {ctx.texts}")

    # --- an agent that hangs forever must not wedge the chat ---
    ctx = FakeTurnContext(make_activity())
    await make_bot(SlowAgent(delay=10, reply="never seen")).on_message_activity(ctx)
    check("times out instead of hanging",
          "took too long" in ctx.texts[-1], f"got {ctx.texts}")

    # --- an agent that crashes ---
    ctx = FakeTurnContext(make_activity())
    await make_bot(SlowAgent(raises=True)).on_message_activity(ctx)
    check("survives an agent crash",
          "went wrong" in ctx.texts[-1], f"got {ctx.texts}")

    # --- Teams re-delivering the same message must not answer twice ---
    agent = SlowAgent(delay=0, reply="answered once")
    bot = make_bot(agent)
    ctx1 = FakeTurnContext(make_activity(activity_id="same-id"))
    ctx2 = FakeTurnContext(make_activity(activity_id="same-id"))
    await bot.on_message_activity(ctx1)
    await bot.on_message_activity(ctx2)
    check("ignores a duplicate delivery", ctx2.texts == [], f"got {ctx2.texts}")
    check("agent ran only once", agent.calls == 1, f"ran {agent.calls} times")

    # --- but a genuinely new message still gets through ---
    ctx3 = FakeTurnContext(make_activity(activity_id="different-id"))
    await bot.on_message_activity(ctx3)
    check("a new message still answered", ctx3.texts == ["answered once"],
          f"got {ctx3.texts}")

    print("=" * 62)
    print(f"  {sum(results)}/{len(results)} passed")
    print("=" * 62)
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    asyncio.run(main())
