"""
THE AGENT LAYER.

This is the ONLY file you change as the bot gets smarter. It knows
nothing about Microsoft Teams.

The contract every agent follows is one method:

    async def ask(message: str, context: AgentContext) -> str

Give it text, get text back. That's it.

Stage 1: EchoAgent        -- repeats you (no API key needed)
Stage 3: HuggingFaceAgent -- a real model  <-- YOU ARE HERE
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

# Hugging Face's router speaks the OpenAI API, so the `openai` SDK works
# unchanged -- only the base URL and the model name are different.
BASE_URL = "https://router.huggingface.co/v1"

MODEL = "meta-llama/Llama-3.3-70B-Instruct"

# Other models on the router: openai/gpt-oss-120b, Qwen/Qwen3-14B,
# deepseek-ai/DeepSeek-R1. Prefer a plain instruct model: reasoning models
# put their answer in `reasoning_content` and leave `content` empty, which
# the code below reads as "no reply".
# Full list:  GET https://router.huggingface.co/v1/models

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


class HuggingFaceAgent:
    """Sends the message to Hugging Face and returns the reply.

    Remembers the last few messages per conversation, so follow-up
    questions like "and what about the other one?" work.
    """

    name = "huggingface"

    def __init__(self, api_key: str | None = None):
        from openai import AsyncOpenAI

        # Left to itself the SDK looks for OPENAI_API_KEY, which this
        # project no longer sets, so always hand it the token explicitly.
        self.client = AsyncOpenAI(
            api_key=api_key or os.environ.get("HF_TOKEN", ""),
            base_url=BASE_URL,
        )

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
                "huggingface reply: %s in / %s out tokens",
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

    Uses Hugging Face when a token is available, and falls back to the
    echo bot when there isn't one -- so the project always starts.
    """
    if os.environ.get("HF_TOKEN"):
        try:
            agent = HuggingFaceAgent()
            log.info("Using HuggingFaceAgent (model: %s)", MODEL)
            return agent
        except Exception:
            log.exception("Could not start HuggingFaceAgent -- falling back to echo")

    log.warning(
        "No HF_TOKEN found -- running the echo bot. "
        "Add the token to .env to switch on the real agent."
    )
    return EchoAgent()
