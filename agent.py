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

Organizations can bring their own AI (see orgs.py). A channel that knows
which organization a person belongs to puts it in
AgentContext.extra["tenant"]; the agent then answers with that
organization's model and key, and never falls back to ours.
"""

import logging
import os
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Dict

import models

log = logging.getLogger("agent")

# How long to wait for an organization's own AI before giving up.
ORG_AI_TIMEOUT = 45.0


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


def explain_ai_error(error: Exception) -> str:
    """Turn a failed call to an organization's AI into a plain phrase."""
    status = getattr(error, "status_code", None)
    detail = str(error).lower()
    if status in (401, 403) or "api key" in detail or "unauthorized" in detail:
        return "the API key was rejected"
    if status == 404 or "model_not_found" in detail or "does not exist" in detail:
        return "the model name was not found"
    if status == 429:
        return "the AI provider is busy or the quota is used up"
    if isinstance(error, TimeoutError) or "timed out" in detail or "timeout" in detail:
        return "the AI provider took too long to answer"
    if "connect" in detail or "name or service" in detail or "getaddrinfo" in detail:
        return "the AI address could not be reached"
    return "the AI provider returned an error"


async def org_completion(client, model: str, messages: list, max_tokens: int):
    """Ask an organization's own model.

    Their provider may be anything OpenAI-compatible. Newer reasoning
    models refuse `max_tokens` and a custom temperature, so when the
    provider says so, ask again the way those models want.
    """
    try:
        return await client.chat.completions.create(
            model=model, messages=messages, temperature=TEMPERATURE, max_tokens=max_tokens)
    except Exception as error:
        detail = str(error).lower()
        if getattr(error, "status_code", None) == 400 and (
                "max_tokens" in detail or "temperature" in detail):
            return await client.chat.completions.create(
                model=model, messages=messages, max_completion_tokens=max_tokens * 3)
        raise


@dataclass
class Route:
    """Which models and which connection a message should use."""
    catalogue: list
    default_key: str
    client: Any                 # our default connection
    org_name: str = ""          # set when the organization brought its own AI
    # saved AI id -> connection, when the organization brought its own AI
    clients: Dict[str, Any] = field(default_factory=dict)

    def client_for(self, model) -> Any:
        if self.org_name:
            return self.clients[model.provider.split(":", 1)[1]]
        return self.client


class HuggingFaceAgent:
    """Sends the message to a model and returns the reply.

    Remembers the last few messages per conversation, and which model
    that conversation chose. Both are kept per conversation, so two
    chats never share either.
    """

    name = "huggingface"

    def __init__(self, api_key: str | None = None, registry=None,
                 allow_private_ai_urls: bool = False):
        from openai import AsyncOpenAI

        token = api_key or os.environ.get("HF_TOKEN", "")
        # Organizations that did not bring their own AI use ours. Without a
        # token there is no "ours", only theirs.
        self.has_default = bool(token)

        # Left to itself the SDK looks for OPENAI_API_KEY, which this
        # project no longer sets, so always hand it the token explicitly.
        self.client = AsyncOpenAI(api_key=token or "not-set", base_url=BASE_URL)

        # Where organizations and their own AI keys are kept (orgs.py).
        self.registry = registry
        # (org id, settings version) -> a connection to that organization's AI
        self._org_clients: Dict[tuple, Any] = {}
        # Local testing only: let an organization's AI live on this machine.
        self.allow_private_ai_urls = allow_private_ai_urls

        # conversation_id -> recent messages
        self.history: Dict[str, deque] = defaultdict(
            lambda: deque(maxlen=MEMORY_TURNS)
        )
        # conversation_id -> chosen model key
        self.chosen: Dict[str, str] = {}
        # conversations that just saw the menu, so a bare "3" means "pick 3"
        self.awaiting_choice: set = set()

    # ------------------------------------------------------------ helpers

    def route_for(self, context: AgentContext) -> Route:
        """Our models, or the organization's own when they brought one."""
        tenant = (context.extra or {}).get("tenant", "")
        if self.registry is not None and tenant:
            org = self.registry.find(tenant)
            ais = self.registry.ais_for(org)
            catalogue = models.org_catalogue(ais)
            if catalogue:
                return Route(catalogue=catalogue, default_key=catalogue[0].key,
                             client=None, org_name=org.name,
                             clients={ai.id: self._org_client(org.id, ai) for ai in ais})
        return Route(catalogue=models.CATALOGUE, default_key=models.DEFAULT_KEY,
                     client=self.client)

    def _org_client(self, org_id: str, ai) -> Any:
        from openai import AsyncOpenAI

        from orgs import public_http_client

        cache_key = (org_id, ai.id, ai.version)
        client = self._org_clients.get(cache_key)
        if client is None:
            # Settings changed: drop the old connection for this saved AI.
            for old in [k for k in self._org_clients if k[:2] == (org_id, ai.id)]:
                self._org_clients.pop(old)
            client = AsyncOpenAI(api_key=ai.api_key, base_url=ai.base_url,
                                 timeout=ORG_AI_TIMEOUT, max_retries=1,
                                 http_client=public_http_client(self.allow_private_ai_urls))
            self._org_clients[cache_key] = client
        return client

    def model_for(self, conversation_id: str, route: Route | None = None) -> "models.Model":
        catalogue = route.catalogue if route else models.CATALOGUE
        default = route.default_key if route else models.DEFAULT_KEY
        key = self.chosen.get(conversation_id, default)
        return models.get(key, catalogue) or models.get(default, catalogue)

    def _switch(self, conversation_id: str, wanted: str,
                route: Route | None = None) -> str | None:
        """Switch model if `wanted` names one. Returns the reply, or None."""
        picked = models.get(wanted, route.catalogue if route else None)
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

        route = self.route_for(context)

        # Show the menu.
        if lowered in ("model", "models", "model?"):
            self.awaiting_choice.add(cid)
            return models.menu(self.model_for(cid, route).key, route.catalogue)

        # "model qwen" / "/model 3" -- pick directly.
        if lowered.startswith(("model ", "use ", "switch to ")):
            wanted = lowered.split(" ", 1)[1].replace("switch to ", "")
            reply = self._switch(cid, wanted, route)
            if reply:
                return reply
            return (f"I do not have a model called '{wanted}'. "
                    f"Type `model` to see the list.")

        # A bare "3" or "qwen" right after the menu means they are choosing.
        if cid in self.awaiting_choice:
            reply = self._switch(cid, lowered, route)
            if reply:
                return reply
            # Not a choice after all -- they moved on. Treat it as a question.
            self.awaiting_choice.discard(cid)

        if not route.org_name and not self.has_default:
            return ("No AI is set up for your organization yet. "
                    "Ask your admin to add one on the settings page.")

        chosen = self.model_for(cid, route)
        turns = self.history[cid]
        turns.append({"role": "user", "content": text})

        # A thinking model spends tokens reasoning before it writes anything,
        # so a normal budget can end the reply before it has begun.
        budget = MAX_TOKENS * 3 if chosen.thinks else MAX_TOKENS

        messages = [{"role": "system", "content": system_prompt_for(context)}, *turns]
        try:
            if route.org_name:
                response = await org_completion(route.client_for(chosen), chosen.model,
                                                messages, budget)
            else:
                response = await route.client.chat.completions.create(
                    model=chosen.model,
                    temperature=TEMPERATURE,
                    max_tokens=budget,
                    messages=messages,
                )
        except Exception as error:
            turns.pop()
            if route.org_name:
                # Their AI, their data: never quietly answer with ours instead.
                reason = explain_ai_error(error)
                log.warning("%s own AI failed (%s): %s", route.org_name,
                            chosen.model, type(error).__name__)
                return (f"Your organization's AI could not answer: {reason}. "
                        f"Ask your admin to check it on the settings page.")
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
    Organizations' own AI keys come from orgs.py when ORG_SECRETS_KEY is set.
    """
    import orgs

    registry = orgs.get_registry()
    if os.environ.get("HF_TOKEN") or registry.enabled:
        try:
            agent = HuggingFaceAgent(
                registry=registry,
                allow_private_ai_urls=(not os.environ.get("WEBSITE_SITE_NAME")
                                       and os.environ.get("ALLOW_PRIVATE_AI_URLS", "") == "1"))
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
