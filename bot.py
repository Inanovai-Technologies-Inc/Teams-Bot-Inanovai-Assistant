"""
THE TEAMS LAYER.

This file is the translator. It speaks Microsoft Teams on one side
and plain strings on the other. It contains no bot intelligence --
all of that lives in agent.py.

Stage 4 added the "slow reply" handling: the typing bubble, a
"still working" nudge, a timeout, and a guard against Teams
delivering the same message twice.
"""

import asyncio
import logging
from collections import deque

from botbuilder.core import MessageFactory, TurnContext
from botbuilder.core.teams import TeamsActivityHandler
from botbuilder.schema import Activity, ActivityTypes, ChannelAccount

from agent import AgentContext, get_agent

log = logging.getLogger("bot")


# How often to re-send the typing bubble. Teams fades it after about
# 5 seconds, so we refresh a little sooner than that.
TYPING_INTERVAL = 4.0

# If the agent is still thinking after this long, tell the user.
SLOW_REPLY_AFTER = 8.0

# Give up on the agent after this long, so a hung call can't wedge the chat.
AGENT_TIMEOUT = 60.0

# How many recent message ids to remember, to spot Teams re-deliveries.
SEEN_MESSAGE_LIMIT = 500


async def keep_typing(turn_context: TurnContext, done: asyncio.Event) -> None:
    """Keep the '...' bubble alive in Teams until `done` is set."""
    while not done.is_set():
        try:
            await turn_context.send_activity(Activity(type=ActivityTypes.typing))
        except Exception:
            # A failed typing indicator must never break the real reply.
            log.debug("typing indicator failed", exc_info=True)
            return
        try:
            await asyncio.wait_for(done.wait(), timeout=TYPING_INTERVAL)
        except asyncio.TimeoutError:
            pass  # still working -- send another one


async def say_if_slow(turn_context: TurnContext, done: asyncio.Event) -> None:
    """Send one 'still working' note if the agent takes a while."""
    try:
        await asyncio.wait_for(done.wait(), timeout=SLOW_REPLY_AFTER)
    except asyncio.TimeoutError:
        try:
            await turn_context.send_activity(
                MessageFactory.text("Still working on this one...")
            )
        except Exception:
            log.debug("slow-reply notice failed", exc_info=True)


class TeamsBot(TeamsActivityHandler):
    def __init__(self):
        self.agent = get_agent()
        # Teams re-sends a message if we are slow to acknowledge it.
        # Without this the user would get answered twice.
        self._seen_ids: deque = deque(maxlen=SEEN_MESSAGE_LIMIT)
        log.info("Bot started with agent: %s", self.agent.name)

    def _already_handled(self, activity_id: str | None) -> bool:
        if not activity_id:
            return False
        if activity_id in self._seen_ids:
            return True
        self._seen_ids.append(activity_id)
        return False

    async def on_message_activity(self, turn_context: TurnContext):
        """Runs every time someone sends the bot a message."""
        if self._already_handled(turn_context.activity.id):
            log.info("Ignoring duplicate delivery of %s", turn_context.activity.id)
            return

        incoming = turn_context.activity.text or ""

        # Teams prefixes messages in a channel with the bot's @mention.
        # Strip it so the agent sees only what the human actually typed.
        TurnContext.remove_recipient_mention(turn_context.activity)
        incoming = (turn_context.activity.text or incoming).strip()

        sender = turn_context.activity.from_property
        context = AgentContext(
            user_id=getattr(sender, "id", "unknown"),
            user_name=getattr(sender, "name", None) or "there",
            conversation_id=turn_context.activity.conversation.id,
        )

        log.info("[%s] said: %s", context.user_name, incoming)

        # Show the typing bubble and warn about slowness while we wait.
        done = asyncio.Event()
        typing = asyncio.create_task(keep_typing(turn_context, done))
        slow_notice = asyncio.create_task(say_if_slow(turn_context, done))

        try:
            reply = await asyncio.wait_for(
                self.agent.ask(incoming, context), timeout=AGENT_TIMEOUT
            )
        except asyncio.TimeoutError:
            log.warning("Agent timed out after %ss", AGENT_TIMEOUT)
            reply = "That took too long, so I stopped. Try asking a simpler way."
        except Exception:
            log.exception("Agent failed")
            reply = "Sorry, something went wrong on my side. Try again."
        finally:
            done.set()
            await asyncio.gather(typing, slow_notice, return_exceptions=True)

        await turn_context.send_activity(MessageFactory.text(reply))

    async def on_conversation_update_activity(self, turn_context: TurnContext):
        """Guard for a crash in the base Teams handler.

        The base class does TeamsChannelData().deserialize(activity.channel_data)
        and then reads .team off the result. When channel_data is absent that
        deserialize returns None, so the read raises AttributeError before any
        of our code runs. Real Teams almost always sends channel_data, but the
        Emulator and test tools do not always, and an empty dict deserializes
        into a valid object with team=None.
        """
        if turn_context.activity.channel_data is None:
            turn_context.activity.channel_data = {}
        return await super().on_conversation_update_activity(turn_context)

    async def on_members_added_activity(
        self, members_added: list[ChannelAccount], turn_context: TurnContext
    ):
        """Runs once when the bot is first added to a chat, team or channel."""
        if self.agent.name == "echo":
            greeting = (
                "Hi! I'm your bot. Send me anything and I'll echo it back. "
                "Type `help` to see what I can do."
            )
        else:
            greeting = (
                "Hi! I'm your assistant. Ask me anything. "
                "I remember our recent messages -- type `reset` to clear that."
            )

        for member in members_added:
            if member.id != turn_context.activity.recipient.id:
                await turn_context.send_activity(MessageFactory.text(greeting))
