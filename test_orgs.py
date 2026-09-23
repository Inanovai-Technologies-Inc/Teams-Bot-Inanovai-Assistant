"""
Tests organizations bringing their own AI -- WITHOUT calling any real AI.

Checks the registry (storage, encryption, access codes, tenants), that the
agent answers each organization with its own model and never falls back
to ours, and that Teams and Google Chat say which organization a message
is from.

Run:  python test_orgs.py
"""

import asyncio
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

from cryptography.fernet import Fernet

import models
import orgs
from agent import AgentContext, HuggingFaceAgent, org_completion
from bot import tenant_id_of

results = []


def check(label, condition, detail=""):
    results.append(bool(condition))
    print(f"  {'PASS' if condition else 'FAIL'}  {label}")
    if not condition and detail:
        print(f"        {detail}")


class FakeClient:
    """Stands in for AsyncOpenAI. Records calls; can fail on demand."""

    def __init__(self, reply="ok", error=None, fail_first=None):
        self.reply, self.error, self.fail_first = reply, error, fail_first
        self.calls = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail_first is not None and len(self.calls) == 1:
            raise self.fail_first
        if self.error is not None:
            raise self.error
        return SimpleNamespace(
            choices=[SimpleNamespace(finish_reason="stop",
                                     message=SimpleNamespace(content=self.reply))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1))


class ApiError(Exception):
    def __init__(self, status, text):
        super().__init__(text)
        self.status_code = status


def new_registry(tmp: Path) -> orgs.Registry:
    return orgs.Registry(tmp / "orgs.json", Fernet.generate_key().decode())


def registry_tests(tmp: Path):
    print("\n  -- registry --")
    reg = new_registry(tmp)
    check("enabled when a secret key is given", reg.enabled)
    check("disabled without one", not orgs.Registry(tmp / "x.json").enabled)

    org, code = reg.create("Acme Ltd", ["Teams:ABC-123", "google:Acme.com"])
    check("tenants are normalised", org.tenants == ["teams:abc-123", "google:acme.com"], org.tenants)
    check("access code is long", len(code) >= 16)
    check("finds by Teams tenant", reg.find("teams:ABC-123").id == org.id)
    check("finds by Google domain", reg.find("google:acme.com").id == org.id)
    check("unknown tenant finds nothing", reg.find("google:other.com") is None)
    check("unknown tenant is listed for the admin", "google:other.com" in reg.unregistered)

    check("right code signs in", reg.check_code(org.id, code))
    check("wrong code refused", not reg.check_code(org.id, code + "x"))
    check("wrong org refused", not reg.check_code("org-nope", code))
    stored = (tmp / "orgs.json").read_text()
    check("code is never stored readable", code not in stored)

    try:
        reg.create("Other", ["google:acme.com"])
        check("a tenant cannot belong to two organizations", False)
    except ValueError:
        check("a tenant cannot belong to two organizations", True)
    try:
        reg.create("Bad", ["acme.com"])
        check("tenant without a kind is refused", False)
    except ValueError:
        check("tenant without a kind is refused", True)

    check("no own AI to begin with", reg.ais_for(org) == [])
    first = reg.save_ai(org.id, "", "", "openai", "https://api.openai.com/v1",
                        ["gpt-4o", "gpt-4o-mini"], "sk-secret-123")
    ais = reg.ais_for(reg.get(org.id))
    check("saved key comes back decrypted", ais[0].api_key == "sk-secret-123")
    check("models kept in order", ais[0].models == ["gpt-4o", "gpt-4o-mini"])
    check("a blank name becomes the provider's name", ais[0].label == "OpenAI", ais[0].label)
    check("key is encrypted on disk", "sk-secret-123" not in (tmp / "orgs.json").read_text())

    reg.save_ai(org.id, first, "OpenAI main", "openai", "https://api.openai.com/v1", ["gpt-4o"], None)
    ais = reg.ais_for(reg.get(org.id))
    check("blank key keeps the saved one", ais[0].api_key == "sk-secret-123" and ais[0].label == "OpenAI main")

    second = reg.save_ai(org.id, "", "Gemini", "gemini",
                         "https://generativelanguage.googleapis.com/v1beta/openai", ["gemini-2.5-flash"], "g-key-456")
    ais = reg.ais_for(reg.get(org.id))
    check("a second key is kept alongside the first",
          [a.id for a in ais] == [first, second] and ais[1].api_key == "g-key-456")
    reg.make_default(org.id, second)
    check("make default moves it to the top", reg.ais_for(reg.get(org.id))[0].id == second)
    reg.remove_ai(org.id, second)
    check("removing one key keeps the other",
          [a.id for a in reg.ais_for(reg.get(org.id))] == [first]
          and "g-key-456" not in (tmp / "orgs.json").read_text())
    for n in range(orgs.MAX_AIS - 1):
        reg.save_ai(org.id, "", f"k{n}", "openai", "https://api.openai.com/v1", ["m"], "k")
    try:
        reg.save_ai(org.id, "", "one too many", "openai", "https://api.openai.com/v1", ["m"], "k")
        check("there is a limit on saved keys", False)
    except ValueError:
        check("there is a limit on saved keys", True)
    for a in reg.ais_for(reg.get(org.id))[1:]:
        reg.remove_ai(org.id, a.id)

    other_key = orgs.Registry(tmp / "orgs.json", Fernet.generate_key().decode())
    check("a different secret cannot read the key", other_key.ais_for(other_key.get(org.id)) == [])

    fresh = orgs.Registry(tmp / "orgs.json")
    check("file reloads in a new process", fresh.get(org.id).name == "Acme Ltd")

    new_code = reg.reset_code(org.id)
    check("reset makes the old code stop working", not reg.check_code(org.id, code))
    check("and the new one work", reg.check_code(org.id, new_code))

    reg.remove_ai(org.id, first)
    check("removing the last AI forgets the key", reg.ais_for(reg.get(org.id)) == []
          and json.loads((tmp / "orgs.json").read_text())["orgs"][org.id]["ais"] == [])

    reg.delete(org.id)
    check("delete removes the organization", reg.get(org.id) is None and reg.find("google:acme.com") is None)


def upgrade_tests(tmp: Path):
    print("\n  -- organizations saved before several keys were allowed --")
    secret = Fernet.generate_key().decode()
    token = Fernet(secret.encode()).encrypt(b"old-key").decode()
    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / "orgs.json").write_text(json.dumps({"orgs": {"org-old": {
        "name": "Old Co", "tenants": ["google:old.com"], "created": "2026-09-21",
        "llm": {"provider": "openai", "base_url": "https://api.openai.com/v1",
                "models": ["gpt-4o"], "key": token, "updated": "x"}}}}))
    reg = orgs.Registry(tmp / "orgs.json", secret)
    ais = reg.ais_for(reg.get("org-old"))
    check("their single key becomes a list of one",
          len(ais) == 1 and ais[0].api_key == "old-key" and ais[0].models == ["gpt-4o"])
    reg.save_ai("org-old", "", "", "gemini", "https://generativelanguage.googleapis.com/v1beta/openai",
                ["gemini-2.5-flash"], "new")
    check("and they can add more", len(reg.ais_for(reg.get("org-old"))) == 2)


def helper_tests():
    print("\n  -- helpers --")
    check("model list parsed and deduplicated",
          orgs.parse_models("gpt-4o, gpt-4o\n mini ,") == ["gpt-4o", "mini"])
    check("Google tenant from email", orgs.google_tenant("Ravi@Client.CO.in") == "google:client.co.in")
    check("no email, no tenant", orgs.google_tenant("") == "")
    check("http refused", orgs.check_base_url("http://api.openai.com/v1") is not None)
    check("localhost refused", orgs.check_base_url("https://127.0.0.1/v1") is not None)
    check("cloud metadata address refused", orgs.check_base_url("https://169.254.169.254/") is not None)
    check("private network refused", orgs.check_base_url("https://10.0.0.5/v1") is not None)
    check("local testing may allow a private address",
          orgs.check_base_url("http://localhost:11434/v1", allow_private=True) is None)

    activity = SimpleNamespace(conversation=SimpleNamespace(tenant_id="T-1"), channel_data={})
    check("Teams tenant from the conversation", tenant_id_of(activity) == "T-1")
    activity = SimpleNamespace(conversation=SimpleNamespace(tenant_id=None),
                               channel_data={"tenant": {"id": "T-2"}})
    check("Teams tenant from channel data", tenant_id_of(activity) == "T-2")


async def agent_tests(tmp: Path):
    print("\n  -- agent routing --")
    reg = new_registry(tmp)
    own, _ = reg.create("Own AI Co", ["google:own.com"])
    reg.save_ai(own.id, "", "Main", "custom", "https://ai.own.com/v1", ["their-model", "their-small"], "k-1")
    plain, _ = reg.create("Plain Co", ["teams:plain-tenant"])

    agent = HuggingFaceAgent(api_key="fake-default", registry=reg)
    ours = FakeClient(reply="from ours")
    theirs = FakeClient(reply="from theirs")
    agent.client = ours
    agent._org_client = lambda org_id, ai: theirs

    def ctx(tenant, cid):
        return AgentContext(conversation_id=cid, extra={"channel": "googlechat", "tenant": tenant})

    answer = await agent.ask("hello", ctx("google:own.com", "c-own"))
    check("organization with own AI gets its answer", answer == "from theirs", answer)
    check("its first model is the default", theirs.calls[-1]["model"] == "their-model")
    check("our AI was not used", not ours.calls)

    menu = await agent.ask("model", ctx("google:own.com", "c-own"))
    check("model menu shows only their models", "their-small" in menu and "Llama" not in menu, menu)
    switched = await agent.ask("2", ctx("google:own.com", "c-own"))
    check("they can switch between their models", "their-small" in switched, switched)
    await agent.ask("next question", ctx("google:own.com", "c-own"))
    check("switch sticks", theirs.calls[-1]["model"] == "their-small")

    answer = await agent.ask("hello", ctx("teams:plain-tenant", "c-plain"))
    check("organization without own AI uses ours", answer == "from ours")
    answer = await agent.ask("hello", ctx("google:stranger.com", "c-x"))
    check("unregistered organization uses ours", answer == "from ours")
    answer = await agent.ask("hello", AgentContext(conversation_id="c-none"))
    check("message with no organization uses ours", answer == "from ours")
    check("our default model used there",
          ours.calls[-1]["model"] == models.BY_KEY[models.DEFAULT_KEY].model)

    failing = FakeClient(error=ApiError(401, "Incorrect API key provided"))
    agent._org_client = lambda org_id, ai: failing
    before = len(ours.calls)
    answer = await agent.ask("hello", ctx("google:own.com", "c-fail"))
    check("their failure is explained in plain words", "API key was rejected" in answer, answer)
    check("and never falls back to our AI", len(ours.calls) == before)

    no_default = HuggingFaceAgent(api_key="", registry=reg)
    no_default.client = FakeClient()
    answer = await no_default.ask("hello", ctx("teams:plain-tenant", "c-nd"))
    check("without our token, orgs without own AI are told to set one",
          "settings page" in answer and not no_default.client.calls, answer)

    print("\n  -- several keys: each model uses its own --")
    multi, _ = reg.create("Two Keys Co", ["google:two.com"])
    a1 = reg.save_ai(multi.id, "", "OpenAI", "openai", "https://api.openai.com/v1", ["gpt-4o", "shared"], "k-a")
    a2 = reg.save_ai(multi.id, "", "Gemini", "gemini", "https://g.example/v1", ["gemini-2.5-flash", "shared"], "k-b")
    by_ai = {a1: FakeClient(reply="via openai"), a2: FakeClient(reply="via gemini")}
    agent._org_client = lambda org_id, ai: by_ai[ai.id]
    c2 = lambda: ctx("google:two.com", "c-two")
    answer = await agent.ask("hi", c2())
    check("default is the first model of the first key", answer == "via openai"
          and by_ai[a1].calls[-1]["model"] == "gpt-4o")
    menu = await agent.ask("model", c2())
    check("menu lists models from every key", "gemini-2.5-flash (Gemini)" in menu
          and "gpt-4o (OpenAI)" in menu, menu)
    await agent.ask("model gemini-2.5-flash", c2())
    answer = await agent.ask("hi", c2())
    check("picking a model uses that model's key", answer == "via gemini"
          and by_ai[a2].calls[-1]["model"] == "gemini-2.5-flash")
    await agent.ask("model gemini/shared", c2())
    await agent.ask("hi", c2())
    check("the same model name under two keys can still be picked",
          by_ai[a2].calls[-1]["model"] == "shared")

    print("\n  -- registered only --")
    guard = HuggingFaceAgent(api_key="fake-default", registry=reg, home_tenants={"teams:home-tenant"})
    ours2 = FakeClient(reply="from ours")
    guard.client = ours2
    guard._org_client = lambda org_id, ai: FakeClient(reply="from theirs")

    answer = await guard.ask("hi", ctx("google:stranger-co.com", "c-s1"))
    check("off by default: unregistered companies still get answers", answer == "from ours")

    reg.set_policy(True, "email sales@inanovai.com")
    check("switch is saved in the file", orgs.Registry(reg.path).policy["registered_only"])
    before = len(ours2.calls)
    answer = await guard.ask("hi", ctx("google:stranger-co.com", "c-s2"))
    check("on: unregistered company gets the not-registered reply",
          "not registered" in answer and "email sales@inanovai.com" in answer, answer)
    check("and no AI is called for them", len(ours2.calls) == before)
    answer = await guard.ask("model", ctx("google:stranger-co.com", "c-s2"))
    check("commands do nothing for them either", "not registered" in answer)
    check("they show up for the admin to register", "google:stranger-co.com" in reg.unregistered)

    answer = await guard.ask("hi", ctx("teams:plain-tenant", "c-s3"))
    check("registered company without own AI still uses ours", answer == "from ours")
    answer = await guard.ask("hi", ctx("google:own.com", "c-s4"))
    check("registered company with own AI still uses theirs", answer == "from theirs")
    answer = await guard.ask("hi", ctx("teams:home-tenant", "c-s5"))
    check("our own company is always allowed", answer == "from ours")
    answer = await guard.ask("hi", AgentContext(conversation_id="c-s6", extra={"channel": "telegram"}))
    check("Telegram and WhatsApp are not affected", answer == "from ours")

    reg.set_policy(True, "")
    answer = await guard.ask("hi", ctx("google:stranger-co.com", "c-s7"))
    check("a sensible message when no contact is set", "contact Inanovai" in answer, answer)
    reg.set_policy(False, "")

    print("\n  -- every connection is checked, not just the saved address --")
    import httpx2
    guarded = orgs.public_http_client()
    for target in ("http://127.0.0.1:9/", "http://169.254.169.254/latest/meta-data/",
                   "http://localhost:9/"):
        try:
            await guarded.get(target)
            check(f"connection to {target} refused", False)
        except httpx2.ConnectError as error:
            check(f"connection to {target} refused", "not a public address" in str(error), str(error))
    await guarded.aclose()

    print("\n  -- providers that refuse max_tokens --")
    picky = FakeClient(fail_first=ApiError(400, "Unsupported parameter: 'max_tokens'"))
    await org_completion(picky, "o-model", [{"role": "user", "content": "hi"}], 100)
    check("retries with max_completion_tokens",
          "max_completion_tokens" in picky.calls[-1] and "temperature" not in picky.calls[-1],
          picky.calls[-1])


async def main():
    print("=" * 62)
    print("  Organization AI tests (no real AI calls)")
    print("=" * 62)
    with tempfile.TemporaryDirectory() as d:
        registry_tests(Path(d) / "a")
        upgrade_tests(Path(d) / "c")
        helper_tests()
        await agent_tests(Path(d) / "b")
    passed = sum(results)
    print("\n" + "=" * 62)
    print(f"  {passed}/{len(results)} passed")
    print("=" * 62)
    return passed == len(results)


if __name__ == "__main__":
    sys.exit(0 if asyncio.run(main()) else 1)
