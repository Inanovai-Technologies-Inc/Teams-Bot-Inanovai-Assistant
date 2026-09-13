"""
Tests the model picker.

A fake client stands in for the provider, so this costs nothing and needs
no token. It checks the parts that are easy to get wrong: picking by
number, by name and by loose spelling; the choice sticking to one
conversation; a bare number only counting right after the menu; and the
out-of-credits message.

Run:  python test_models.py
"""

import asyncio
import logging
import sys
from types import SimpleNamespace

import models
from agent import AgentContext, HuggingFaceAgent

logging.disable(logging.CRITICAL)

results = []


def check(label, condition, detail=""):
    results.append(condition)
    print(f"  {'PASS' if condition else 'FAIL'}  {label}")
    if not condition and detail:
        print(f"        {detail}")


class FakeClient:
    """Records which model was asked, and can be told to fail."""

    def __init__(self, reply="an answer", error=None):
        self.reply = reply
        self.error = error
        self.calls = []
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self._create)
        )

    async def _create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return SimpleNamespace(
            choices=[SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(content=self.reply),
            )],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
        )


def make(**kw):
    agent = HuggingFaceAgent(api_key="fake-token-never-used")
    agent.client = FakeClient(**kw)
    return agent


A = AgentContext(user_id="u1", user_name="Vishal", conversation_id="chat-a")
B = AgentContext(user_id="u2", user_name="Asha", conversation_id="chat-b")


async def main():
    print("=" * 62)
    print(f"  Model picker tests ({len(models.CATALOGUE)} models, no network)")
    print("=" * 62)

    # ---------------------------------------------------------- the menu
    agent = make()
    reply = await agent.ask("model", A)
    check("`model` shows the menu", "Pick a model" in reply)
    check("menu lists every model",
          all(m.label in reply for m in models.CATALOGUE))
    check("menu marks the current one", "(using now)" in reply)
    check("showing the menu costs no API call", agent.client.calls == [])

    # ------------------------------------------------------ picking by number
    reply = await agent.ask("3", A)
    third = models.CATALOGUE[2]
    check("a bare number picks from the menu", third.label in reply,
          f"got {reply!r}")
    await agent.ask("hello", A)
    check("the next question uses the picked model",
          agent.client.calls[-1]["model"] == third.model,
          f"got {agent.client.calls[-1]['model']}")

    # -------------------------------------------------------- picking by name
    agent = make()
    reply = await agent.ask("model coder", A)
    check("`model <name>` switches", "Coder" in reply, f"got {reply!r}")
    await agent.ask("hello", A)
    check("uses the named model",
          agent.client.calls[-1]["model"] == models.BY_KEY["coder"].model)

    # ---------------------------------------------------- loose spelling
    check("finds 'Qwen 2.5 72B' from 'qwen'", models.get("qwen").key == "qwen")
    check("finds it from 'QWEN'", models.get("QWEN").key == "qwen")
    check("finds gpt-oss from 'gpt oss'", models.get("gptoss").key == "gptoss")
    check("rejects an unknown name", models.get("banana") is None)
    check("rejects an out-of-range number", models.get("99") is None)

    # ------------------------------------------- a bare number is not a pick
    agent = make()
    await agent.ask("hello", A)          # no menu shown
    before = len(agent.client.calls)
    reply = await agent.ask("3", A)
    check("a bare number without the menu is a question",
          len(agent.client.calls) == before + 1, f"got {reply!r}")

    # ------------------------------------ a non-number after the menu passes through
    agent = make()
    await agent.ask("model", A)
    before = len(agent.client.calls)
    await agent.ask("what is the capital of Japan?", A)
    check("a real question after the menu is answered",
          len(agent.client.calls) == before + 1)

    # ------------------------------------------------ choice is per conversation
    agent = make()
    await agent.ask("model coder", A)
    await agent.ask("hello", A)
    a_model = agent.client.calls[-1]["model"]
    await agent.ask("hello", B)
    b_model = agent.client.calls[-1]["model"]
    check("another chat is unaffected", b_model != a_model,
          f"A={a_model} B={b_model}")
    check("the other chat gets the default",
          b_model == models.BY_KEY[models.DEFAULT_KEY].model)

    # ------------------------------------------------- unknown model name
    agent = make()
    reply = await agent.ask("model banana", A)
    check("unknown name is explained", "do not have a model" in reply.lower(),
          f"got {reply!r}")

    # ------------------------------------------ thinking models get more room
    agent = make()
    await agent.ask("model glm", A)
    await agent.ask("hello", A)
    thinking_budget = agent.client.calls[-1]["max_tokens"]
    await agent.ask("model llama", A)
    await agent.ask("hello", A)
    normal_budget = agent.client.calls[-1]["max_tokens"]
    check("a thinking model gets a bigger token budget",
          thinking_budget > normal_budget,
          f"thinks={thinking_budget} normal={normal_budget}")

    # ------------------------------------------------------ out of credits
    agent = make(error=Exception("Error code: 402 - you have depleted your monthly credits"))
    reply = await agent.ask("hello", A)
    check("explains when credits run out", "credits" in reply.lower(),
          f"got {reply!r}")
    check("suggests picking another model", "model" in reply.lower())
    check("the failed turn is not kept", len(agent.history["chat-a"]) == 0)

    # ------------------------------------------------------------- reset
    agent = make()
    await agent.ask("model coder", A)
    await agent.ask("hello", A)
    reply = await agent.ask("reset", A)
    check("reset clears the history", "fresh" in reply.lower())
    await agent.ask("hello", A)
    check("reset keeps the chosen model",
          agent.client.calls[-1]["model"] == models.BY_KEY["coder"].model,
          "a reset should clear what was said, not what was chosen")

    print("=" * 62)
    print(f"  {sum(results)}/{len(results)} passed")
    print("=" * 62)
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    asyncio.run(main())
