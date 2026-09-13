"""
Tests that the assistant is told which app the person is using.

Every channel used to send the same instructions naming Microsoft Teams,
so people on Telegram and Google Chat were told they were in Teams. This
checks each channel names its own app, never another one, and that the
prompt the model actually receives is the right one.

No token, no network.

Run:  python test_prompt.py
"""

import asyncio
import logging
import sys
from types import SimpleNamespace

from agent import APP_NAMES, AgentContext, HuggingFaceAgent, system_prompt_for

logging.disable(logging.CRITICAL)

results = []


def check(label, condition, detail=""):
    results.append(condition)
    print(f"  {'PASS' if condition else 'FAIL'}  {label}")
    if not condition and detail:
        print(f"        {detail}")


class FakeClient:
    """Stands in for the model provider and records what it was sent."""

    def __init__(self):
        self.calls = []
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self._create)
        )

    async def _create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(content="hello there"),
            )],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
        )


def ctx(channel=None):
    extra = {"channel": channel} if channel else {}
    return AgentContext(user_id="u1", user_name="Vishal",
                        conversation_id=f"chat-{channel}", extra=extra)


async def main():
    print("=" * 62)
    print(f"  Prompt names the right app ({len(APP_NAMES)} channels, no network)")
    print("=" * 62)

    for channel, app in APP_NAMES.items():
        prompt = system_prompt_for(ctx(channel))
        others = [a for c, a in APP_NAMES.items() if c != channel]
        check(f"{channel} is told it is in {app}", app in prompt, prompt[:90])
        check(f"{channel} is never told another app name",
              not any(other in prompt for other in others),
              f"found one of {others}")

    neutral = system_prompt_for(ctx(None))
    check("no channel label gets a neutral prompt",
          "a chat app" in neutral and not any(a in neutral for a in APP_NAMES.values()),
          neutral[:90])
    check("an unfamiliar channel gets a neutral prompt",
          "a chat app" in system_prompt_for(ctx("slack")))

    agent = HuggingFaceAgent(api_key="fake-token-never-used")
    agent.client = FakeClient()

    await agent.ask("hello", ctx("googlechat"))
    sent = agent.client.calls[-1]["messages"][0]
    check("the model receives the Google Chat prompt",
          sent["role"] == "system" and "Google Chat" in sent["content"],
          sent["content"][:90])
    check("the Google Chat prompt does not mention Teams",
          "Teams" not in sent["content"])

    await agent.ask("hello", ctx("teams"))
    check("Teams users are still told they are in Microsoft Teams",
          "Microsoft Teams" in agent.client.calls[-1]["messages"][0]["content"])

    await agent.ask("hello", ctx("telegram"))
    check("Telegram users are told they are in Telegram",
          "Telegram" in agent.client.calls[-1]["messages"][0]["content"])

    print("=" * 62)
    print(f"  {sum(results)}/{len(results)} passed")
    print("=" * 62)
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    asyncio.run(main())
