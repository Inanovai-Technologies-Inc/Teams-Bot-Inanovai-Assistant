"""
THE AGENT LAYER.

This is the ONLY file you change as the bot gets smarter. It knows
nothing about Microsoft Teams.

The contract every agent follows is one method:

    async def ask(message: str, context: AgentContext) -> str

Give it text, get text back. That's it.

Stage 1: EchoAgent        -- repeats you (no API key needed)
Stage 3: HuggingFaceAgent -- real models, picked per conversation  <-- YOU ARE HERE
Stage 5: NemoClawAgent
"""

import logging
import os
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Dict

import models

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

# The app each person is typing in, named the way they would name it.
# Every channel puts its name in AgentContext.extra, and the prompt is
# filled in per message, so nobody is told they are in the wrong app.
APP_NAMES = {
    "teams": "Microsoft Teams",
    "telegram": "Telegram",
    "whatsapp": "WhatsApp",
    "googlechat": "Google Chat",
}

SYSTEM_PROMPT = """You are Inanovai Assistant, a helpful assistant that people talk to in {app}.

Keep replies short and conversational -- usually two or three sentences.
People are reading you in a chat window, not a document. Use a bullet
list only when you are genuinely listing things. Skip greetings and
sign-offs; get straight to the answer.

If you do not know something, say so plainly."""


def system_prompt_for(context) -> str:
    """The system prompt, naming the app this person is actually using."""
    channel = (context.extra or {}).get("channel", "")
    return SYSTEM_PROMPT.format(app=APP_NAMES.get(channel, "a chat app"))


class HuggingFaceAgent:
    """Sends the message to a model and returns the reply.

    Remembers the last few messages per conversation, and which model
    that conversation chose. Both are kept per conversation, so two
    chats never share either.
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
        # conversation_id -> chosen model key
        self.chosen: Dict[str, str] = {}
        # conversations that just saw the menu, so a bare "3" means "pick 3"
        self.awaiting_choice: set = set()

    # ------------------------------------------------------------ helpers

    def model_for(self, conversation_id: str) -> "models.Model":
        key = self.chosen.get(conversation_id, models.DEFAULT_KEY)
        return models.BY_KEY.get(key) or models.BY_KEY[models.DEFAULT_KEY]

    def _switch(self, conversation_id: str, wanted: str) -> str | None:
        """Switch model if `wanted` names one. Returns the reply, or None."""
        picked = models.get(wanted)
        if not picked:
            return None
        self.chosen[conversation_id] = picked.key
        self.awaiting_choice.discard(conversation_id)
        return f"Switched to {picked.label}. {picked.note.capitalize()}."

    # --------------------------------------------------------------- ask

    async def ask(self, message: str, context: AgentContext) -> str:
        text = (message or "").strip()
        cid = context.conversation_id

        if not text:
            return "I got an empty message. Try typing something."

        lowered = text.lower().lstrip("/")

        # `reset` gives the user a way out of a confused conversation.
        if lowered in ("reset", "clear", "forget"):
            self.history.pop(cid, None)
            self.awaiting_choice.discard(cid)
            return "Cleared. Starting fresh."

        # Show the menu.
        if lowered in ("model", "models", "model?"):
            self.awaiting_choice.add(cid)
            return models.menu(self.chosen.get(cid, models.DEFAULT_KEY))

        # "model qwen" / "/model 3" -- pick directly.
        if lowered.startswith(("model ", "use ", "switch to ")):
            wanted = lowered.split(" ", 1)[1].replace("switch to ", "")
            reply = self._switch(cid, wanted)
            if reply:
                return reply
            return (f"I do not have a model called '{wanted}'. "
                    f"Type `model` to see the list.")

        # A bare "3" or "qwen" right after the menu means they are choosing.
        if cid in self.awaiting_choice:
            reply = self._switch(cid, lowered)
            if reply:
                return reply
            # Not a choice after all -- they moved on. Treat it as a question.
            self.awaiting_choice.discard(cid)

        chosen = self.model_for(cid)
        turns = self.history[cid]
        turns.append({"role": "user", "content": text})

        # A thinking model spends tokens reasoning before it writes anything,
        # so a normal budget can end the reply before it has begun.
        budget = MAX_TOKENS * 3 if chosen.thinks else MAX_TOKENS

        try:
            response = await self.client.chat.completions.create(
                model=chosen.model,
                temperature=TEMPERATURE,
                max_tokens=budget,
                messages=[{"role": "system", "content": system_prompt_for(context)}, *turns],
            )
        except Exception as error:
            turns.pop()
            detail = str(error)
            # The router returns 402 when that provider's allowance is spent.
            # It is model-specific, so another one usually still works.
            if "402" in detail or "depleted" in detail.lower():
                log.warning("no credits left for %s", chosen.model)
                return (f"{chosen.label} has run out of credits for now. "
                        f"Type `model` to pick a different one.")
            log.exception("call to %s failed", chosen.model)
            raise

        choice = response.choices[0]

        # A safety block comes back normally -- check before reading content.
        if choice.finish_reason == "content_filter":
            turns.pop()
            return "I can't help with that one. Ask me something else."

        reply = (choice.message.content or "").strip()

        if not reply:
            turns.pop()
            if choice.finish_reason == "length":
                return ("That answer was cut short before it started. "
                        "Try a shorter question, or `model` to pick a faster one.")
            return "I didn't have anything to say to that. Try rephrasing?"

        turns.append({"role": "assistant", "content": reply})

        usage = response.usage
        if usage:
            log.info(
                "%s reply: %s in / %s out tokens",
                chosen.key, usage.prompt_tokens, usage.completion_tokens,
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
            log.info("Using HuggingFaceAgent (%s models, default: %s)",
                     len(models.CATALOGUE), models.DEFAULT_KEY)
            return agent
        except Exception:
            log.exception("Could not start HuggingFaceAgent -- falling back to echo")

    log.warning(
        "No HF_TOKEN found -- running the echo bot. "
        "Add the token to .env to switch on the real agent."
    )
    return EchoAgent()
