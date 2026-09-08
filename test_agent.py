"""
Tests the agent logic WITHOUT calling the real model.

It swaps in a fake AI client, so this costs nothing, needs no API
key, and runs in a second. It checks the parts that are easy to get
wrong: conversation memory, the reset command, refusals, empty replies.

Run:  python test_agent.py
"""

import asyncio
import sys
from types import SimpleNamespace

from agent import MODEL, AgentContext, EchoAgent, HuggingFaceAgent


class FakeClient:
    """Stands in for AsyncOpenAI. Records what it was sent."""

    def __init__(self, reply="Sure, here you go.", finish_reason="stop"):
        self.reply = reply
        self.finish_reason = finish_reason
        self.calls = []
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self._create)
        )

    async def _create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason=self.finish_reason,
                    message=SimpleNamespace(content=self.reply),
                )
            ],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
        )


def make_agent(**kwargs) -> HuggingFaceAgent:
    agent = HuggingFaceAgent(api_key="fake-key-never-used")
    agent.client = FakeClient(**kwargs)
    return agent


CTX = AgentContext(user_id="u1", user_name="Vishal", conversation_id="c1")

results = []


def check(label, condition, detail=""):
    results.append(condition)
    print(f"  {'PASS' if condition else 'FAIL'}  {label}")
    if not condition and detail:
        print(f"        {detail}")


async def main():
    print("=" * 62)
    print("  Agent logic tests (fake model, no API calls)")
    print("=" * 62)

    # --- the model actually gets called, and its text comes back ---
    agent = make_agent(reply="Paris.")
    answer = await agent.ask("What is the capital of France?", CTX)
    check("returns the model's reply", answer == "Paris.", f"got {answer!r}")
    check("used the configured model", agent.client.calls[0]["model"] == MODEL)
    check("sent a system prompt",
          agent.client.calls[0]["messages"][0]["role"] == "system")

    # --- memory: the second question must carry the first exchange ---
    agent = make_agent(reply="ok")
    await agent.ask("My name is Vishal", CTX)
    await agent.ask("What is my name?", CTX)
    sent = agent.client.calls[1]["messages"]
    check("remembers earlier turns", len(sent) == 4, f"sent {len(sent)} messages")
    check("first turn is the user's", sent[1]["content"] == "My name is Vishal")
    check("keeps the assistant reply", sent[2]["role"] == "assistant")

    # --- separate conversations must not bleed into each other ---
    other = AgentContext(user_id="u2", user_name="Asha", conversation_id="c2")
    await agent.ask("totally different topic", other)
    check("conversations stay separate", len(agent.client.calls[2]["messages"]) == 2)

    # --- reset clears memory ---
    reply = await agent.ask("reset", CTX)
    check("reset confirms", "fresh" in reply.lower(), f"got {reply!r}")
    await agent.ask("hello again", CTX)
    check("reset really cleared it", len(agent.client.calls[-1]["messages"]) == 2)

    # --- refusal is handled, and not stored in history ---
    agent = make_agent(finish_reason="content_filter")
    reply = await agent.ask("something disallowed", CTX)
    check("handles a content filter", "can't help" in reply.lower(), f"got {reply!r}")
    check("filtered reply not kept in memory", len(agent.history["c1"]) == 0)

    # --- empty reply from the model ---
    agent = make_agent(reply="   ")
    reply = await agent.ask("hi", CTX)
    check("handles an empty reply", "rephrasing" in reply.lower(), f"got {reply!r}")

    # --- empty user message never reaches the model ---
    agent = make_agent()
    reply = await agent.ask("   ", CTX)
    check("blocks empty input", "empty message" in reply.lower())
    check("did not call the model", agent.client.calls == [])

    # --- the echo fallback still works ---
    echo = EchoAgent()
    check("echo fallback alive", await echo.ask("ping", CTX) == "pong")

    print("=" * 62)
    print(f"  {sum(results)}/{len(results)} passed")
    print("=" * 62)
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    asyncio.run(main())
