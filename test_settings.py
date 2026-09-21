"""
Tests the /admin and /settings pages -- WITHOUT calling any real AI.

Drives the pages the way a browser would: sign in, add an organization,
sign in as that organization, test and save a key, remove it. The AI
test is faked, so nothing leaves this machine.

Run:  python test_settings.py
"""

import asyncio
import re
import sys
import tempfile
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from cryptography.fernet import Fernet

import orgs
from settings_web import SettingsSite

results = []


def check(label, condition, detail=""):
    results.append(bool(condition))
    print(f"  {'PASS' if condition else 'FAIL'}  {label}")
    if not condition and detail:
        print(f"        {detail}")


ADMIN_PASSWORD = "admin-pass-for-tests"
SECRET = Fernet.generate_key().decode()


class FakeTester:
    def __init__(self):
        self.ok = True
        self.calls = []

    async def __call__(self, base_url, api_key, model):
        self.calls.append((base_url, api_key, model))
        if self.ok:
            return True, f"Connected. {model} answered."
        return False, "Test failed: the API key was rejected."


def make_site(tmp: Path, ready=True):
    registry = orgs.Registry(tmp / "orgs.json", SECRET if ready else "")
    tester = FakeTester()
    site = SettingsSite(registry, admin_password=ADMIN_PASSWORD if ready else "",
                        secret=SECRET if ready else "", secure_cookies=False, tester=tester)
    app = web.Application()
    site.register(app)
    return app, registry, tester


def grab(pattern, text):
    match = re.search(pattern, text)
    return match.group(1) if match else ""


async def main():
    print("=" * 62)
    print("  Settings pages tests (fake AI test, no network)")
    print("=" * 62)

    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)

        # --- switched off until the server has its secrets ---
        app, _, _ = make_site(tmp / "off", ready=False)
        async with TestClient(TestServer(app)) as c:
            r = await c.get("/settings")
            check("pages are off without ORG_SECRETS_KEY", r.status == 503)

        app, registry, tester = make_site(tmp / "on")
        async with TestClient(TestServer(app)) as c:
            origin = {"Origin": f"http://{c.server.host}:{c.server.port}"}

            # --- admin ---
            r = await c.get("/admin")
            body = await r.text()
            check("admin page asks for a password", "Password" in body and r.status == 200)
            check("pages send a strict security policy",
                  "frame-ancestors 'none'" in r.headers.get("Content-Security-Policy", ""))

            r = await c.post("/admin/login", data={"code": "wrong"}, headers=origin)
            check("wrong admin password refused", "Wrong password" in await r.text())

            r = await c.post("/admin/login", data={"code": ADMIN_PASSWORD}, headers=origin)
            body = await r.text()
            check("right admin password signs in", "Add an organization" in body, body[:200])

            r = await c.post("/admin/create", headers=origin, data={
                "name": "Acme Ltd", "tenants": "google:acme.com\nteams:tenant-1"})
            body = await r.text()
            org_id = grab(r"Organization ID</dt><dd class='code'>([^<]+)<", body)
            code = grab(r"Access code</dt><dd class='code'>([^<]+)<", body)
            check("creating an organization shows its ID and code once", org_id and code)
            check("with a ready-made settings link", f"/settings?org={org_id}" in body)
            check("organization saved", registry.find("google:acme.com") is not None)

            r = await c.post("/admin/create", headers=origin,
                             data={"name": "Copycat", "tenants": "google:acme.com"})
            check("a taken domain is refused with a clear message",
                  "already belongs to Acme Ltd" in await r.text())

            r = await c.post("/admin/create", data={"name": "X"},
                             headers={"Origin": "https://evil.example"})
            check("posts from another website are blocked", r.status == 403)
            r = await c.post("/admin/create", data={"name": "X"},
                             headers={"Sec-Fetch-Site": "cross-site"})
            check("browser-reported cross-site posts are blocked", r.status == 403)
            r = await c.post("/admin/create", headers={"Sec-Fetch-Site": "same-origin", "Origin": "null"},
                             data={"name": "Via browser", "tenants": "google:browser.test"})
            check("same-site browser posts are allowed", registry.find("google:browser.test") is not None)
            registry.delete(registry.find("google:browser.test").id)

            r = await c.post("/logout", headers=origin)
            check("sign out", r.status == 200)
            r = await c.post("/admin/create", headers=origin, data={"name": "Sneaky"})
            check("signed out admin cannot add organizations",
                  registry.find("google:sneaky.com") is None and len(registry.all()) == 1)

            # --- client admin ---
            r = await c.get(f"/settings?org={org_id}")
            body = await r.text()
            check("settings link fills in the organization ID", f"value='{org_id}'" in body)

            r = await c.post("/settings/login", headers=origin, data={"org": org_id, "code": "nope"})
            check("wrong access code refused", "not right" in await r.text())

            r = await c.post("/settings/login", headers=origin, data={"org": org_id, "code": code})
            body = await r.text()
            check("right access code signs in", "Add your AI" in body and "Acme Ltd" in body)
            check("starts on the default AI", "Default AI" in body)

            r = await c.post("/settings/save", headers=origin, data={
                "provider": "custom", "base_url": "https://127.0.0.1/v1",
                "models": "m1", "api_key": "k", "action": "save"})
            check("private addresses are refused", "private network" in await r.text())
            check("and never tested", not tester.calls)

            r = await c.post("/settings/save", headers=origin, data={
                "provider": "openai", "base_url": "", "models": "gpt-4o-mini",
                "api_key": "", "action": "save"})
            check("a key is required the first time", "Paste your API key" in await r.text())

            tester.ok = False
            r = await c.post("/settings/save", headers=origin, data={
                "provider": "openai", "base_url": "", "models": "gpt-4o-mini",
                "api_key": "sk-bad", "action": "save"})
            body = await r.text()
            check("a failing key is not saved", "Nothing was saved" in body
                  and registry.ais_for(registry.get(org_id)) == [])
            check("typed key is not echoed back", "sk-bad" not in body)

            tester.ok = True
            r = await c.post("/settings/save", headers=origin, data={
                "provider": "openai", "base_url": "", "models": "gpt-4o-mini\ngpt-4o",
                "api_key": "sk-good-123", "action": "test"})
            check("Test connection does not save", "Press Test and save" in await r.text()
                  and registry.ais_for(registry.get(org_id)) == [])
            check("provider's own address is used when left blank",
                  tester.calls[-1][0] == "https://api.openai.com/v1")

            r = await c.post("/settings/save", headers=origin, data={
                "provider": "openai", "base_url": "", "models": "gpt-4o-mini\ngpt-4o",
                "api_key": "sk-good-123", "action": "save"})
            body = await r.text()
            ais = registry.ais_for(registry.get(org_id))
            check("Test and save stores the settings", len(ais) == 1 and ais[0].models == ["gpt-4o-mini", "gpt-4o"])
            first_id = ais[0].id
            check("page confirms it", "Saved." in body and "Own AI" in body)
            check("the saved key is never shown on the page", "sk-good-123" not in body)

            r = await c.post("/settings/save", headers=origin, data={
                "ai_id": first_id, "provider": "openai", "base_url": "", "models": "gpt-4o",
                "api_key": "", "action": "save"})
            ais = registry.ais_for(registry.get(org_id))
            check("editing with a blank key keeps the saved one",
                  len(ais) == 1 and ais[0].api_key == "sk-good-123" and ais[0].models == ["gpt-4o"]
                  and tester.calls[-1][1] == "sk-good-123")

            r = await c.post("/settings/save", headers=origin, data={
                "ai_id": "", "label": "Gemini", "provider": "gemini", "base_url": "",
                "models": "gemini-2.5-flash", "api_key": "g-key-789", "action": "save"})
            body = await r.text()
            ais = registry.ais_for(registry.get(org_id))
            check("Add another AI keeps both keys", [a.label for a in ais] == ["OpenAI", "Gemini"], ais)
            check("page shows both, keys hidden", "Gemini" in body and "Add another AI" in body
                  and "g-key-789" not in body and "sk-good-123" not in body)

            r = await c.post("/settings/default", headers=origin, data={"ai_id": ais[1].id})
            check("make default", registry.ais_for(registry.get(org_id))[0].label == "Gemini")

            r = await c.post("/settings/save", headers=origin, data={
                "ai_id": "ai-99", "provider": "openai", "models": "x", "api_key": "k", "action": "save"})
            check("editing a removed AI is refused", "was removed" in await r.text())

            r = await c.post("/settings/remove", headers=origin, data={"ai_id": ais[1].id})
            check("removing one key keeps the other", "Removed." in await r.text()
                  and [a.label for a in registry.ais_for(registry.get(org_id))] == ["OpenAI"])

            r = await c.post("/settings/new-code", headers=origin)
            new_code = grab(r"<p class='code'>([^<]+)</p>", await r.text())
            check("client can change their own access code",
                  new_code and registry.check_code(org_id, new_code)
                  and not registry.check_code(org_id, code))

            r = await c.post("/settings/remove", headers=origin, data={"ai_id": first_id})
            check("removing the last key goes back to the default AI", "default AI" in await r.text()
                  and registry.ais_for(registry.get(org_id)) == [])

            # --- another organization's session cannot touch this one ---
            other, other_code = registry.create("Other Co", ["google:other.com"])
            await c.post("/logout", headers=origin)
            await c.post("/settings/login", headers=origin, data={"org": other.id, "code": other_code})
            await c.post("/settings/save", headers=origin, data={
                "provider": "openai", "models": "gpt-4o", "api_key": "sk-other", "action": "save"})
            check("each admin only changes their own organization",
                  registry.ais_for(registry.get(org_id)) == []
                  and registry.ais_for(registry.get(other.id))[0].api_key == "sk-other")

            # --- too many wrong codes ---
            await c.post("/logout", headers=origin)
            for _ in range(8):
                await c.post("/settings/login", headers=origin, data={"org": org_id, "code": "bad"})
            r = await c.post("/settings/login", headers=origin, data={"org": org_id, "code": new_code})
            check("locks out after repeated wrong codes", "Too many attempts" in await r.text())

    passed = sum(results)
    print("\n" + "=" * 62)
    print(f"  {passed}/{len(results)} passed")
    print("=" * 62)
    return passed == len(results)


if __name__ == "__main__":
    sys.exit(0 if asyncio.run(main()) else 1)
