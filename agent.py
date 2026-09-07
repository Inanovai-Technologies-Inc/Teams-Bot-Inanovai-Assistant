"""
THE AGENT LAYER.

This is the ONLY file you change as the bot gets smarter. It knows
nothing about Microsoft Teams.

The contract every agent follows is one method:

    async def ask(message: str, context: AgentContext) -> str

Give it text, get text back. That's it.

Stage 1: EchoAgent   -- repeats you (no API key needed)
Stage 3: OpenAIAgent -- a real model  <-- YOU ARE HERE
Stage 5: NemoClawAgent
"""

import logging
import os
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Dict

log = logging.getLogger("agent")


@dataclass
class AgentContext:
    """Who is talking to us, and where.

    Nothing here is Teams-specific on purpose -- Slack or a CLI could
    fill in the same fields.
    """

    user_id: str = "unknown"
    user_name: str = "there"
    conversation_id: str = "unknown"
    extra: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Stage 1 -- the echo bot. Kept as the fallback when there is no API key.
# ---------------------------------------------------------------------------

class EchoAgent:
    """Repeats what you said. Proves the plumbing works."""

    name = "echo"

    async def ask(self, message: str, context: AgentContext) -> str:
        text = (message or "").strip()

        if not text:
            return "I got an empty message. Try typing something."

        lowered = text.lower()
        if lowered in ("hi", "hello", "hey"):
            return f"Hello {context.user_name}! I'm alive. Say anything and I'll echo it back."
        if lowered == "help":
            return (
                "I'm an echo bot for now.\n\n"
                "- Send any text and I repeat it.\n"
                "- `ping` -> I reply `pong`.\n"
                "- Later this same slot will be a real agent."
            )
        if lowered == "ping":
            return "pong"

        return f"You said: {text}"


# ---------------------------------------------------------------------------
# Stage 3 -- a real model.
# ---------------------------------------------------------------------------

MODEL = "gpt-4o"

# Other models this key can use: gpt-4o-mini (cheaper/faster),
# gpt-4.1, gpt-4-turbo. Change the line above to switch.

# How creative the replies are. 0 = predictable, 1 = chatty.
TEMPERATURE = 0.7

# Longest reply, in tokens. ~1000 tokens is about 750 words.
MAX_TOKENS = 1000

# How many past messages to remember per conversation (10 = 5 back-and-forths).
MEMORY_TURNS = 10

SYSTEM_PROMPT = """You are a helpful assistant inside Microsoft Teams.

Keep replies short and conversational -- usually two or three sentences.
People are reading you in a chat window, not a document. Use a bullet
list only when you are genuinely listing things. Skip greetings and
sign-offs; get straight to the answer.

If you do not know something, say so plainly."""


class OpenAIAgent:
    """Sends the message to OpenAI and returns the reply.

    Remembers the last few messages per conversation, so follow-up
    questions like "and what about the other one?" work.
    """

    name = "openai"

    def __init__(self, api_key: str | None = None):
        from openai import AsyncOpenAI

        # No api_key argument -> the SDK reads OPENAI_API_KEY itself.
        self.client = AsyncOpenAI(api_key=api_key) if api_key else AsyncOpenAI()

        # conversation_id -> recent messages
        self.history: Dict[str, deque] = defaultdict(
            lambda: deque(maxlen=MEMORY_TURNS)
        )

    async def ask(self, message: str, context: AgentContext) -> str:
        text = (message or "").strip()
        if not text:
            return "I got an empty message. Try typing something."

        # `reset` gives the user a way out of a confused conversation.
        if text.lower() in ("reset", "clear", "forget"):
            self.history.pop(context.conversation_id, None)
            return "Cleared. Starting fresh."

        turns = self.history[context.conversation_id]
        turns.append({"role": "user", "content": text})

        response = await self.client.chat.completions.create(
            model=MODEL,
            temperature=TEMPERATURE,
            max_tokens=MAX_TOKENS,
            messages=[{"role": "system", "content": SYSTEM_PROMPT}, *turns],
        )

        choice = response.choices[0]

        # A safety block comes back normally -- check before reading content.
        if choice.finish_reason == "content_filter":
            turns.pop()
            return "I can't help with that one. Ask me something else."

        reply = (choice.message.content or "").strip()

        if not reply:
            turns.pop()
            return "I didn't have anything to say to that. Try rephrasing?"

        turns.append({"role": "assistant", "content": reply})

        usage = response.usage
        if usage:
            log.info(
                "openai reply: %s in / %s out tokens",
                usage.prompt_tokens,
                usage.completion_tokens,
            )
        return reply


# ---------------------------------------------------------------------------
# STAGE 5 -- NemoClaw slots in exactly the same way:
#
# class NemoClawAgent:
#     name = "nemoclaw"
#
#     async def ask(self, message: str, context: AgentContext) -> str:
#         ...  # call NemoClaw, return its text
# ---------------------------------------------------------------------------


def get_agent():
    """Single place that decides which agent is live.

    Uses the AI API when a key is available, and falls back to the
    echo bot when there isn't one -- so the project always starts.
    """
    if os.environ.get("OPENAI_API_KEY"):
        try:
            agent = OpenAIAgent()
            log.info("Using OpenAIAgent (model: %s)", MODEL)
            return agent
        except Exception:
            log.exception("Could not start OpenAIAgent -- falling back to echo")

    log.warning(
        "No OPENAI_API_KEY found -- running the echo bot. "
        "Add the key to .env to switch on the real agent."
    )
    return EchoAgent()
