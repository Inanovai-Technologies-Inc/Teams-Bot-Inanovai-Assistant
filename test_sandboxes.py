"""
Tests one sandbox per person -- WITHOUT a NemoClaw server.

A stand-in NemoClaw records what it was asked, so this checks the part
that matters: that every message reaches the right person's sandbox, that
one person keeps one sandbox across chat apps, and that people from
unregistered organizations get none.

Run:  python test_sandboxes.py
"""

import asyncio
import json
import sys
import tempfile
from pathlib import Path

from cryptography.fernet import Fernet

import orgs
import sandboxes
from agent import AgentContext, NemoClawAgent

results = []


def check(label, condition, detail=""):
    results.append(bool(condition))
    print(f"  {'PASS' if condition else 'FAIL'}  {label}")
    if not condition and detail:
        print(f"        {detail}")


class Boom:
    """A NemoClaw that is down."""

    async def ensure_sandbox(self, person):
        raise RuntimeError("connection refused")

    async def ask(self, sandbox_id, message):
        raise RuntimeError("connection refused")


class PlainAgent:
    name = "plain"

    def __init__(self):
        self.asked = []

    async def ask(self, message, context):
        self.asked.append(message)
        return "from the plain agent"


def ctx(channel, tenant, user_id, name="Ravi", email=""):
    extra = {"channel": channel}
    if tenant:
        extra["tenant"] = tenant
    if email:
        extra["email"] = email
    return AgentContext(user_id=user_id, user_name=name,
                        conversation_id=f"{channel}:{user_id}", extra=extra)


async def main():
    print("=" * 62)
    print("  Sandbox-per-person tests (stand-in NemoClaw)")
    print("=" * 62)

    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        reg = orgs.Registry(tmp / "orgs.json", Fernet.generate_key().decode())
        acme, _ = reg.create("Acme Ltd", ["google:acme.com", "teams:acme-tenant"])
        other, _ = reg.create("Other Co", ["google:other.com"])

        book = sandboxes.SandboxBook(tmp / "sandboxes.json")
        claw = sandboxes.PretendNemoClaw()
        plain = PlainAgent()
        agent = NemoClawAgent(claw, book, registry=reg, fallback=plain,
                              home_tenants={"teams:home"})

        print("\n  -- one person, one sandbox --")
        a1 = await agent.ask("hello", ctx("googlechat", "google:acme.com", "u1",
                                          email="Ravi@Acme.com"))
        sb_ravi = claw.asked[-1][0]
        check("their message reaches a sandbox", a1.startswith(f"[{sb_ravi}]"), a1)
        await agent.ask("again", ctx("googlechat", "google:acme.com", "u1", email="ravi@acme.com"))
        check("the same person keeps the same sandbox", claw.asked[-1][0] == sb_ravi)
        await agent.ask("from teams now", ctx("teams", "teams:acme-tenant", "teams-999",
                                              email="RAVI@acme.com"))
        check("same sandbox from a different chat app", claw.asked[-1][0] == sb_ravi)

        await agent.ask("hi", ctx("googlechat", "google:acme.com", "u2", "Meera",
                                  email="meera@acme.com"))
        check("a colleague gets their own sandbox", claw.asked[-1][0] != sb_ravi)

        await agent.ask("hi", ctx("googlechat", "google:other.com", "u9", "Same Name",
                                  email="ravi@other.com"))
        check("another company is kept separate", claw.asked[-1][0] not in
              (sb_ravi, claw.asked[-2][0]))

        print("\n  -- without an email --")
        await agent.ask("hi", ctx("teams", "teams:acme-tenant", "teams-555", "No Email"))
        by_id = claw.asked[-1][0]
        check("they still get a sandbox", by_id and by_id != sb_ravi)
        await agent.ask("again", ctx("teams", "teams:acme-tenant", "teams-555", "No Email"))
        check("and keep it", claw.asked[-1][0] == by_id)
        rows = {r.identity for r in book.for_org(acme.id)}
        check("identities are the email, or the chat account",
              "ravi@acme.com" in rows and "teams:teams-555" in rows, rows)

        print("\n  -- who does not get a sandbox --")
        before = len(claw.asked)
        answer = await agent.ask("hi", ctx("googlechat", "google:stranger.com", "u5"))
        check("unregistered company falls back to the plain agent",
              answer == "from the plain agent" and len(claw.asked) == before)
        answer = await agent.ask("hi", ctx("telegram", "", "555"))
        check("chat apps without a company do too", answer == "from the plain agent")

        reg.set_policy(True, "email sales@inanovai.com")
        answer = await agent.ask("hi", ctx("googlechat", "google:stranger.com", "u5"))
        check("with registered-only on, they are told instead",
              "not registered" in answer and "sales@inanovai.com" in answer, answer)
        answer = await agent.ask("hi", ctx("teams", "teams:home", "boss"))
        check("our own company is still allowed", answer == "from the plain agent")
        reg.set_policy(False, "")

        print("\n  -- the book on disk --")
        row = book.get(sandboxes.Person(acme.id, "Acme Ltd", "ravi@acme.com"))
        check("counts their messages", row.messages == 3, row.messages)
        check("records when they were last seen", bool(row.last_seen))
        saved = json.loads((tmp / "sandboxes.json").read_text())["sandboxes"]
        check("saved to disk", any(v["identity"] == "ravi@acme.com" for v in saved.values()))
        again = sandboxes.SandboxBook(tmp / "sandboxes.json")
        check("read back in a new process",
              again.get(sandboxes.Person(acme.id, "Acme Ltd", "ravi@acme.com")).sandbox_id == sb_ravi)
        check("one organization cannot see another's people",
              len(again.for_org(acme.id)) == 3 and len(again.for_org(other.id)) == 1)

        print("\n  -- when NemoClaw is down --")
        broken = NemoClawAgent(Boom(), book, registry=reg, fallback=plain)
        plain_calls = len(plain.asked)
        answer = await broken.ask("hi", ctx("googlechat", "google:acme.com", "u1",
                                            email="ravi@acme.com"))
        check("a known person is told to try again, not sent to our AI",
              "try again" in answer.lower() and len(plain.asked) == plain_calls, answer)
        answer = await broken.ask("hi", ctx("googlechat", "google:acme.com", "new",
                                            email="new@acme.com"))
        check("a new person is told to try again", "try again" in answer.lower(), answer)
        check("nothing broken was written to the book",
              again.get(sandboxes.Person(acme.id, "Acme", "new@acme.com")) is None)

        print("\n  -- identity helper --")
        check("an email is cleaned up", sandboxes.clean_email("  Ravi@Acme.COM ") == "ravi@acme.com")
        check("junk is not an email", sandboxes.clean_email("not-an-email") == "")
        person = sandboxes.identify(ctx("teams", "teams:acme-tenant", "u1", email="a@b.com"),
                                    reg.get(acme.id))
        check("identity prefers the email", person.identity == "a@b.com" and person.by_email)
        check("no organization, no person", sandboxes.identify(ctx("teams", "", "u1"), None) is None)

    passed = sum(results)
    print("\n" + "=" * 62)
    print(f"  {passed}/{len(results)} passed")
    print("=" * 62)
    return passed == len(results)


if __name__ == "__main__":
    sys.exit(0 if asyncio.run(main()) else 1)
