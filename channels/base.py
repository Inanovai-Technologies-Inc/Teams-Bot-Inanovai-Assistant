"""
Shared plumbing for every non-Teams channel.

A channel's job is small: take a message off a platform, hand the text to
the agent, put the answer back. Everything that is the same for every
platform lives here so each channel file stays short.
"""

import asyncio
import logging
from collections import deque

from agent import AgentContext

log = logging.getLogger("channel")

# Give up on the agent after this long, so a hung call cannot wedge a chat.
AGENT_TIMEOUT = 60.0

# How many recent message ids to remember, to spot platform re-deliveries.
SEEN_MESSAGE_LIMIT = 500


class ChannelBase:
    """Common behaviour: de-duplication, timeouts, error handling."""

    name = "channel"

    def __init__(self, agent, timeout: float = AGENT_TIMEOUT):
        self.agent = agent
        self.timeout = timeout
        # Telegram and WhatsApp both re-send a message when we are slow to
        # acknowledge it. Without this the user gets answered twice.
        self._seen: deque = deque(maxlen=SEEN_MESSAGE_LIMIT)

    def already_handled(self, message_id) -> bool:
        if not message_id:
            return False
        if message_id in self._seen:
            return True
        self._seen.append(message_id)
        return False

    def context_for(self, chat_id, user_id=None, user_name=None,
                    tenant: str = "") -> AgentContext:
        """Build the agent's context.

        The conversation id is prefixed with the channel name so a Telegram
        chat and a WhatsApp chat can never share history, even if the
        platforms happen to hand out the same numeric id. `tenant` names the
        organization the person belongs to, when the platform tells us.
        """
        extra = {"channel": self.name}
        if tenant:
            extra["tenant"] = tenant
        return AgentContext(
            user_id=str(user_id or chat_id),
            user_name=user_name or "there",
            conversation_id=f"{self.name}:{chat_id}",
            extra=extra,
        )

    async def reply_for(self, text: str, context: AgentContext) -> str:
        """Ask the agent, and never raise."""
        try:
            return await asyncio.wait_for(
                self.agent.ask(text, context), timeout=self.timeout
            )
        except asyncio.TimeoutError:
            log.warning("[%s] agent timed out after %ss", self.name, self.timeout)
            return "That took too long, so I stopped. Try asking a simpler way."
        except Exception:
            log.exception("[%s] agent failed", self.name)
            return "Sorry, something went wrong on my side. Try again."
