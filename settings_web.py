"""
THE SETTINGS PAGES.

    /settings   for a client's admin: add one or more AIs (provider, API
                key, model names), test them, save them. Signed in with
                their organization id and access code.
    /admin      for the Inanovai team: add organizations, link their Teams
                tenant or Google domain, and hand out access codes.
                Signed in with ADMIN_PASSWORD.

Both pages are plain server-rendered HTML with no outside scripts. API
keys are never shown back once saved -- not even to the admin who saved
them.
"""

import base64
import hashlib
import hmac
import html
import json
import logging
import os
import secrets
import time
from collections import defaultdict, deque

from aiohttp import web

import orgs
from agent import explain_ai_error, org_completion

log = logging.getLogger("settings")

SESSION_COOKIE = "inanovai_session"
SESSION_HOURS = 8

# Wrong passwords or codes allowed from one address in LOCKOUT_WINDOW seconds.
MAX_FAILED_LOGINS = 8
LOCKOUT_WINDOW = 15 * 60

TEST_TIMEOUT = 25.0


def e(value) -> str:
    return html.escape(str(value or ""), quote=True)


# ---------------------------------------------------------------------------
# Checking an organization's AI works before it is saved
# ---------------------------------------------------------------------------

async def test_ai(base_url: str, api_key: str, model: str,
                  allow_private: bool = False) -> tuple:
    """Send one tiny question. Returns (ok, message for the admin)."""
    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=api_key, base_url=base_url,
                         timeout=TEST_TIMEOUT, max_retries=0,
                         http_client=orgs.public_http_client(allow_private))
    try:
        await org_completion(client, model,
                             [{"role": "user", "content": "Reply with one word: ready"}],
                             max_tokens=16)
    except Exception as error:
        return False, f"Test failed: {explain_ai_error(error)}."
    finally:
        await client.close()
    return True, f"Connected. {model} answered."


# ---------------------------------------------------------------------------
# The site
# ---------------------------------------------------------------------------

class SettingsSite:
    def __init__(self, registry, admin_password: str = "", secret: str = "",
                 public_url: str = "", allow_private_urls: bool = False,
                 secure_cookies: bool = True, tester=test_ai):
        self.registry = registry
        self.admin_password = admin_password
        self.public_url = public_url.rstrip("/")
        self.allow_private_urls = allow_private_urls
        self.secure_cookies = secure_cookies
        self.tester = tester
        self._signing_key = hashlib.sha256(b"inanovai-session|" + secret.encode()).digest() \
            if secret else b""
        self._failures: dict = defaultdict(deque)

    @property
    def ready(self) -> bool:
        return self.registry.enabled and bool(self._signing_key)

    def register(self, app: web.Application) -> None:
        app.router.add_get("/settings", self.settings_page)
        app.router.add_post("/settings/login", self.settings_login)
        app.router.add_post("/settings/save", self.settings_save)
        app.router.add_post("/settings/remove", self.settings_remove)
        app.router.add_post("/settings/default", self.settings_default)
        app.router.add_post("/settings/new-code", self.settings_new_code)
        app.router.add_get("/admin", self.admin_page)
        app.router.add_post("/admin/login", self.admin_login)
        app.router.add_post("/admin/create", self.admin_create)
        app.router.add_post("/admin/update", self.admin_update)
        app.router.add_post("/admin/reset-code", self.admin_reset_code)
        app.router.add_post("/admin/delete", self.admin_delete)
        app.router.add_post("/logout", self.logout)

    # ------------------------------------------------------------ sessions

    def _sign(self, payload: dict) -> str:
        body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
        mac = hmac.new(self._signing_key, body.encode(), hashlib.sha256).hexdigest()
        return f"{body}.{mac}"

    def _session(self, request) -> dict:
        raw = request.cookies.get(SESSION_COOKIE, "")
        if not raw or "." not in raw or not self._signing_key:
            return {}
        body, _, mac = raw.rpartition(".")
        good = hmac.new(self._signing_key, body.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(mac, good):
            return {}
        try:
            data = json.loads(base64.urlsafe_b64decode(body.encode()))
        except Exception:
            return {}
        if data.get("exp", 0) < time.time():
            return {}
        return data

    def _start_session(self, response, role: str, org_id: str = "") -> None:
        token = self._sign({"role": role, "org": org_id,
                            "exp": time.time() + SESSION_HOURS * 3600})
        response.set_cookie(SESSION_COOKIE, token, httponly=True, secure=self.secure_cookies,
                            samesite="Strict", max_age=SESSION_HOURS * 3600, path="/")

    # ------------------------------------------------------------ guards

    def _client_ip(self, request) -> str:
        forwarded = request.headers.get("X-Forwarded-For", "")
        return (forwarded.split(",")[0].strip() if forwarded else request.remote) or "?"

    def _locked_out(self, request) -> bool:
        attempts = self._failures[self._client_ip(request)]
        while attempts and attempts[0] < time.time() - LOCKOUT_WINDOW:
            attempts.popleft()
        return len(attempts) >= MAX_FAILED_LOGINS

    def _failed(self, request) -> None:
        self._failures[self._client_ip(request)].append(time.time())

    def _same_origin(self, request) -> bool:
        """Refuse form posts sent from another website."""
        # Modern browsers say outright where a request came from.
        site = request.headers.get("Sec-Fetch-Site")
        if site:
            return site in ("same-origin", "none")
        origin = request.headers.get("Origin")
        if not origin or origin == "null":
            return origin is None
        host = origin.split("://", 1)[-1]
        return host == request.host

    async def _form(self, request):
        if not self._same_origin(request):
            raise web.HTTPForbidden(text="Blocked: this form was sent from another site.")
        return await request.post()

    def _page(self, title: str, body: str, status: int = 200) -> web.Response:
        nonce = secrets.token_urlsafe(12)
        doc = PAGE.format(title=e(title), body=body, nonce=nonce, css=CSS)
        resp = web.Response(text=doc, content_type="text/html", status=status)
        resp.headers["Content-Security-Policy"] = (
            "default-src 'none'; img-src 'self' data:; "
            f"style-src 'nonce-{nonce}' https://fonts.googleapis.com; "
            f"script-src 'nonce-{nonce}'; font-src https://fonts.gstatic.com; "
            "form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "same-origin"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        return resp

    def _not_ready(self) -> web.Response:
        return self._page("Settings unavailable", card(
            "<h1>Settings are not switched on</h1>"
            "<p class='muted'>The server needs ORG_SECRETS_KEY and ADMIN_PASSWORD "
            "before these pages can be used.</p>"), status=503)

    def _base_url(self, request) -> str:
        if self.public_url:
            return self.public_url
        scheme = request.headers.get("X-Forwarded-Proto", request.scheme)
        return f"{scheme}://{request.host}"

    # =============================================================== CLIENT

    async def settings_page(self, request, notice: str = "", error: str = "",
                            form: dict | None = None, new_code: str = "",
                            org_prefill: str | None = None):
        if not self.ready:
            return self._not_ready()
        session = self._session(request)
        org = self.registry.get(session.get("org")) if session.get("role") == "org" else None
        if not org:
            return self._page("Sign in", login_form(
                "/settings/login", "Organization settings",
                "Sign in to choose the AI your organization uses.",
                org_field=org_prefill if org_prefill is not None else request.query.get("org", ""),
                error=error))
        return self._page(f"{org.name} settings",
                          settings_body(org, form or {}, notice, error, new_code))

    async def settings_login(self, request):
        if not self.ready:
            return self._not_ready()
        form = await self._form(request)
        org_id = (form.get("org") or "").strip()
        if self._locked_out(request):
            return await self.settings_page(request, org_prefill=org_id,
                                            error="Too many attempts. Try again in 15 minutes.")
        if not self.registry.check_code(org_id, (form.get("code") or "").strip()):
            self._failed(request)
            log.warning("failed settings sign-in for %s", org_id[:40])
            return await self.settings_page(request, org_prefill=org_id,
                                            error="That organization ID or access code is not right.")
        resp = web.HTTPSeeOther("/settings")
        self._start_session(resp, "org", org_id)
        log.info("settings sign-in for %s", org_id)
        raise resp

    def _org_from(self, request):
        session = self._session(request)
        if session.get("role") != "org":
            raise web.HTTPSeeOther("/settings")
        org = self.registry.get(session.get("org"))
        if not org:
            raise web.HTTPSeeOther("/settings")
        return org

    async def settings_save(self, request):
        if not self.ready:
            return self._not_ready()
        org = self._org_from(request)
        form = await self._form(request)
        action = form.get("action", "test")
        ai_id = (form.get("ai_id") or "").strip()
        label = (form.get("label") or "").strip()
        provider_key = form.get("provider", "")
        provider = orgs.PROVIDERS.get(provider_key)
        typed_url = (form.get("base_url") or "").strip().rstrip("/")
        model_list = orgs.parse_models(form.get("models", ""))
        api_key = (form.get("api_key") or "").strip()
        keep = {"ai_id": ai_id, "label": label, "provider": provider_key,
                "base_url": typed_url, "models": "\n".join(model_list)}

        def fail(message):
            return self.settings_page(request, error=message, form=keep)

        saved = next((a for a in self.registry.ais_for(org) if a.id == ai_id), None)
        if ai_id and saved is None:
            return await self.settings_page(request, error="That AI was removed. Add it again.")
        if not ai_id and len(org.ais) >= orgs.MAX_AIS:
            return await fail(f"You can save up to {orgs.MAX_AIS} AIs. Remove one first.")
        if not provider:
            return await fail("Choose your AI provider.")
        base_url = typed_url or provider.base_url
        if not base_url:
            return await fail(f"{provider.label} needs its web address.")
        problem = orgs.check_base_url(base_url, self.allow_private_urls)
        if problem:
            return await fail(problem)
        if not model_list:
            return await fail("Type at least one model name.")

        key_to_use = api_key or (saved.api_key if saved else "")
        if not key_to_use:
            return await fail("Paste your API key.")

        if self.tester is test_ai:
            ok, message = await test_ai(base_url, key_to_use, model_list[0],
                                        allow_private=self.allow_private_urls)
        else:
            ok, message = await self.tester(base_url, key_to_use, model_list[0])
        if not ok:
            return await fail(message + " Nothing was saved.")
        if action != "save":
            return await self.settings_page(request, notice=message + " Press Test and save to use it.",
                                            form=keep)

        self.registry.save_ai(org.id, ai_id, label, provider_key, base_url, model_list,
                              api_key or None)
        log.info("%s saved an AI (%s, %d models)", org.id, provider_key, len(model_list))
        return await self.settings_page(
            request, notice="Saved. Your people now get answers from your own AI.")

    async def settings_remove(self, request):
        if not self.ready:
            return self._not_ready()
        org = self._org_from(request)
        form = await self._form(request)
        self.registry.remove_ai(org.id, form.get("ai_id", ""))
        log.info("%s removed an AI", org.id)
        left = self.registry.get(org.id)
        return await self.settings_page(request, notice=(
            "Removed. That key is deleted." if left and left.has_own_ai else
            "Removed. That key is deleted and the bot now uses Inanovai's default AI."))

    async def settings_default(self, request):
        if not self.ready:
            return self._not_ready()
        org = self._org_from(request)
        form = await self._form(request)
        self.registry.make_default(org.id, form.get("ai_id", ""))
        return await self.settings_page(
            request, notice="Done. Its first model is now the one people get by default.")

    async def settings_new_code(self, request):
        if not self.ready:
            return self._not_ready()
        org = self._org_from(request)
        await self._form(request)
        code = self.registry.reset_code(org.id)
        log.info("%s changed its access code", org.id)
        return await self.settings_page(request, new_code=code)

    # ================================================================ ADMIN

    def _is_admin(self, request) -> bool:
        return self._session(request).get("role") == "admin"

    async def admin_page(self, request, notice: str = "", error: str = "", created=None):
        if not self.ready or not self.admin_password:
            return self._not_ready()
        if not self._is_admin(request):
            return self._page("Admin sign in", login_form(
                "/admin/login", "Inanovai admin", "For the Inanovai team only.",
                org_field=None, error=error))
        return self._page("Organizations", admin_body(
            self.registry, self._base_url(request), notice, error, created))

    async def admin_login(self, request):
        if not self.ready or not self.admin_password:
            return self._not_ready()
        form = await self._form(request)
        if self._locked_out(request):
            return await self.admin_page(request, error="Too many attempts. Try again in 15 minutes.")
        if not hmac.compare_digest((form.get("code") or "").encode(), self.admin_password.encode()):
            self._failed(request)
            log.warning("failed admin sign-in")
            return await self.admin_page(request, error="Wrong password.")
        resp = web.HTTPSeeOther("/admin")
        self._start_session(resp, "admin")
        log.info("admin sign-in")
        raise resp

    async def _admin_form(self, request):
        if not self.ready or not self.admin_password:
            raise web.HTTPServiceUnavailable()
        if not self._is_admin(request):
            raise web.HTTPSeeOther("/admin")
        return await self._form(request)

    async def admin_create(self, request):
        form = await self._admin_form(request)
        try:
            org, code = self.registry.create(form.get("name", ""),
                                             split_lines(form.get("tenants", "")))
        except ValueError as error:
            return await self.admin_page(request, error=str(error))
        log.info("admin created %s", org.id)
        return await self.admin_page(request, created=(org, code))

    async def admin_update(self, request):
        form = await self._admin_form(request)
        try:
            self.registry.update(form.get("org", ""), form.get("name", ""),
                                 split_lines(form.get("tenants", "")))
        except (ValueError, KeyError) as error:
            return await self.admin_page(request, error=str(error).strip("'"))
        return await self.admin_page(request, notice="Saved.")

    async def admin_reset_code(self, request):
        form = await self._admin_form(request)
        org = self.registry.get(form.get("org", ""))
        if not org:
            return await self.admin_page(request, error="That organization no longer exists.")
        code = self.registry.reset_code(org.id)
        log.info("admin reset the access code for %s", org.id)
        return await self.admin_page(request, created=(org, code))

    async def admin_delete(self, request):
        form = await self._admin_form(request)
        org = self.registry.get(form.get("org", ""))
        if org:
            self.registry.delete(org.id)
            log.info("admin deleted %s", org.id)
        return await self.admin_page(request, notice="Organization removed, with its saved key.")

    async def logout(self, request):
        await self._form(request)
        target = "/admin" if self._is_admin(request) else "/settings"
        resp = web.HTTPSeeOther(target)
        resp.del_cookie(SESSION_COOKIE, path="/")
        raise resp


def split_lines(text: str) -> list:
    return [line.strip() for line in (text or "").replace(",", "\n").splitlines() if line.strip()]


def build_settings_site(registry) -> SettingsSite:
    """The site as configured by environment variables."""
    on_azure = bool(os.environ.get("WEBSITE_SITE_NAME"))
    return SettingsSite(
        registry,
        admin_password=os.environ.get("ADMIN_PASSWORD", "").strip(),
        secret=os.environ.get("ORG_SECRETS_KEY", "").strip(),
        public_url=os.environ.get("PUBLIC_URL", "").strip(),
        # Local testing may point at Ollama on this machine; the live
        # server never may.
        allow_private_urls=(not on_azure
                            and os.environ.get("ALLOW_PRIVATE_AI_URLS", "") == "1"),
        secure_cookies=on_azure or os.environ.get("PUBLIC_URL", "").startswith("https"),
    )


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

def card(inner: str, cls: str = "") -> str:
    return f"<section class='card {cls}'>{inner}</section>"


def alert(message: str, kind: str) -> str:
    return f"<div class='alert {kind}' role='status'>{e(message)}</div>" if message else ""


def login_form(action, title, lede, org_field, error) -> str:
    org_input = ""
    if org_field is not None:
        org_input = (f"<label for='org'>Organization ID</label>"
                     f"<input id='org' name='org' value='{e(org_field)}' required autocomplete='username'>")
    label = "Access code" if org_field is not None else "Password"
    return card(f"""
      <p class='eyebrow'>Inanovai Assistant</p>
      <h1>{e(title)}</h1>
      <p class='muted'>{e(lede)}</p>
      {alert(error, 'bad')}
      <form method='post' action='{action}' class='stack'>
        {org_input}
        <label for='code'>{label}</label>
        <input id='code' name='code' type='password' required autocomplete='current-password'>
        <button class='primary' type='submit'>Sign in</button>
      </form>""", "narrow")


def provider_options(selected: str) -> str:
    out = ["<option value=''>Choose…</option>"]
    for p in orgs.PROVIDERS.values():
        sel = " selected" if p.key == selected else ""
        out.append(f"<option value='{e(p.key)}' data-url='{e(p.base_url)}' "
                   f"data-url-hint='{e(p.url_hint)}' data-model-hint='{e(p.model_hint)}'{sel}>"
                   f"{e(p.label)}</option>")
    return "".join(out)


def ai_form(ai: dict | None, form: dict, is_default: bool, can_move: bool) -> str:
    """One saved AI (or the empty "add" form when ai is None)."""
    ai = ai or {}
    ai_id = ai.get("id", "")
    mine = form if form.get("ai_id", None) == ai_id and form else {}
    provider = mine.get("provider", ai.get("provider", ""))
    label = mine.get("label", ai.get("label", ""))
    base_url = mine.get("base_url", ai.get("base_url", "") if provider in ("azure", "custom") else "")
    models_text = mine.get("models", "\n".join(ai.get("models") or []))
    fid = ai_id or "new"
    key_placeholder = "A key is saved. Leave blank to keep it." if ai_id else "Paste your API key"

    if ai_id:
        badge = " <span class='pill on'>Default</span>" if is_default else ""
        heading = f"<h2>{e(ai.get('label'))}{badge}</h2><p class='muted small'>Saved {e(ai.get('updated', ''))}</p>"
    else:
        heading = "<h2>Add your AI</h2>" if not can_move else "<h2>Add another AI</h2>"
        heading += ("<p class='muted'>Your key is stored encrypted and is never shown again, not even "
                    "to Inanovai staff. Questions from your people go to this provider with your key.</p>")

    extra = ""
    if ai_id:
        default_btn = ("" if is_default else f"""
          <form method='post' action='/settings/default'><input type='hidden' name='ai_id' value='{e(ai_id)}'>
            <button class='ghost' type='submit'>Make default</button></form>""")
        extra = f"""<div class='row'>{default_btn}
          <form method='post' action='/settings/remove' data-confirm='Delete this key? Its models stop working.'>
            <input type='hidden' name='ai_id' value='{e(ai_id)}'>
            <button class='danger' type='submit'>Remove</button></form></div>"""

    return card(f"""
      {heading}
      <form method='post' action='/settings/save' class='stack ai-form'>
        <input type='hidden' name='ai_id' value='{e(ai_id)}'>
        <label for='label-{fid}'>Name <span class='muted small'>so you can tell your keys apart, e.g. OpenAI main</span></label>
        <input id='label-{fid}' name='label' value='{e(label)}' maxlength='40'>

        <label for='provider-{fid}'>Provider</label>
        <select id='provider-{fid}' name='provider' required>{provider_options(provider)}</select>

        <label for='base_url-{fid}'>Web address <span class='muted small url-hint'></span></label>
        <input id='base_url-{fid}' name='base_url' value='{e(base_url)}' placeholder='https://…' inputmode='url'>

        <label for='models-{fid}'>Model names <span class='muted small'>one per line</span></label>
        <textarea id='models-{fid}' name='models' rows='3' required>{e(models_text)}</textarea>

        <label for='api_key-{fid}'>API key</label>
        <input id='api_key-{fid}' name='api_key' type='password' autocomplete='off' placeholder='{e(key_placeholder)}'>

        <div class='row'>
          <button class='ghost' type='submit' name='action' value='test'>Test connection</button>
          <button class='primary' type='submit' name='action' value='save'>Test and save</button>
        </div>
      </form>
      {extra}""")


def settings_body(org, form, notice, error, new_code) -> str:
    ais = org.ais
    if org.has_own_ai:
        names = ", ".join(f"{a.get('label')} ({len(a.get('models') or [])} models)" for a in ais)
        default = (ais[0].get("models") or ["?"])[0]
        status = (f"<span class='pill on'>Own AI</span> {e(names)}. "
                  f"New chats start on <strong>{e(default)}</strong>; people type "
                  f"<span class='code'>model</span> in the chat to switch.")
    else:
        status = ("<span class='pill off'>Default AI</span> Your people get answers from "
                  "Inanovai's default models. Add your AI below to use your own key.")
    code_box = ""
    if new_code:
        code_box = card(f"<h2>Your new access code</h2><p class='muted'>Copy it now. It will not be shown again, "
                        f"and the old code no longer works.</p><p class='code'>{e(new_code)}</p>", "highlight")

    saved = "".join(ai_form(a, form, i == 0, True) for i, a in enumerate(ais))
    add = ai_form(None, form, False, bool(ais)) if len(ais) < orgs.MAX_AIS else ""
    return f"""
    <header class='top'><div><p class='eyebrow'>Inanovai Assistant · Organization settings</p>
      <h1>{e(org.name)}</h1></div>
      <form method='post' action='/logout'><button class='ghost' type='submit'>Sign out</button></form></header>
    {alert(notice, 'good')}{alert(error, 'bad')}
    {code_box}
    {card(f"<p class='status'>{status}</p>")}
    {saved}
    {add}
    {card('''
      <h2>Access code</h2>
      <p class='muted'>Anyone with your organization ID and access code can change these settings.
      Get a new code if it may have been shared.</p>
      <form method='post' action='/settings/new-code' data-confirm='Create a new access code? The current one stops working.'>
        <button class='ghost' type='submit'>Get a new access code</button>
      </form>''')}
    """


def admin_body(registry, base_url, notice, error, created) -> str:
    created_box = ""
    if created:
        org, code = created
        link = f"{base_url}/settings?org={org.id}"
        created_box = card(f"""
          <h2>Send this to {e(org.name)}'s admin</h2>
          <p class='muted'>The access code is shown only now. Send the link and the code separately.</p>
          <dl class='kv'>
            <dt>Settings page</dt><dd class='code'>{e(link)}</dd>
            <dt>Organization ID</dt><dd class='code'>{e(org.id)}</dd>
            <dt>Access code</dt><dd class='code'>{e(code)}</dd>
          </dl>""", "highlight")

    seen = ""
    if registry.unregistered:
        items = "".join(f"<li class='code'>{e(t)}</li>" for t in registry.unregistered)
        seen = card(f"<h2>Recently seen, not registered</h2><p class='muted'>Organizations that "
                    f"messaged the bot but are not added yet. Copy a line into a new organization.</p>"
                    f"<ul class='plain'>{items}</ul>")

    rows = []
    for org in registry.all():
        ai = (f"<span class='pill on'>Own AI</span> {len(org.ais)} saved · "
              f"{e(', '.join(org.all_models))}"
              if org.has_own_ai else "<span class='pill off'>Default AI</span>")
        rows.append(f"""
        <details class='org'>
          <summary><strong>{e(org.name)}</strong> <span class='muted small code'>{e(org.id)}</span> {ai}</summary>
          <form method='post' action='/admin/update' class='stack'>
            <input type='hidden' name='org' value='{e(org.id)}'>
            <label>Name</label><input name='name' value='{e(org.name)}' required>
            <label>Teams tenants and Google domains <span class='muted small'>one per line</span></label>
            <textarea name='tenants' rows='3'>{e(chr(10).join(org.tenants))}</textarea>
            <div class='row'><button class='primary' type='submit'>Save</button></div>
          </form>
          <div class='row'>
            <form method='post' action='/admin/reset-code' data-confirm='Make a new access code for {e(org.name)}? Their current code stops working.'>
              <input type='hidden' name='org' value='{e(org.id)}'><button class='ghost' type='submit'>New access code</button></form>
            <form method='post' action='/admin/delete' data-confirm='Remove {e(org.name)} and its saved AI key?'>
              <input type='hidden' name='org' value='{e(org.id)}'><button class='danger' type='submit'>Remove organization</button></form>
          </div>
        </details>""")
    listing = "".join(rows) or "<p class='muted'>No organizations yet.</p>"

    return f"""
    <header class='top'><div><p class='eyebrow'>Inanovai Assistant · Admin</p><h1>Organizations</h1></div>
      <form method='post' action='/logout'><button class='ghost' type='submit'>Sign out</button></form></header>
    {alert(notice, 'good')}{alert(error, 'bad')}
    {created_box}
    {card(f"<h2>All organizations</h2>{listing}")}
    {card('''
      <h2>Add an organization</h2>
      <form method='post' action='/admin/create' class='stack'>
        <label for='name'>Organization name</label>
        <input id='name' name='name' required>
        <label for='tenants'>Teams tenants and Google domains <span class='muted small'>one per line</span></label>
        <textarea id='tenants' name='tenants' rows='3' placeholder='teams:00000000-0000-0000-0000-000000000000&#10;google:client-company.com'></textarea>
        <p class='muted small'>Teams: <span class='code'>teams:</span> + their Microsoft tenant ID.
        Google Chat: <span class='code'>google:</span> + their email domain.</p>
        <div class='row'><button class='primary' type='submit'>Add organization</button></div>
      </form>''')}
    {seen}
    """


CSS = """
:root{--ink:#14181f;--muted:#5d6675;--line:#e3e6eb;--soft:#f6f7f9;--accent:#1b4dff;--accent-ink:#fff;
--good:#146c43;--good-bg:#e7f5ee;--bad:#b42318;--bad-bg:#fdecea;--hl:#fffbea;--hl-line:#f0d98a}
*{box-sizing:border-box}
body{margin:0;background:#fff;color:var(--ink);font:15px/1.55 "Public Sans",system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:760px;margin:0 auto;padding:32px 16px 64px;display:grid;gap:16px}
h1,h2{font-family:"Sora",system-ui,sans-serif;line-height:1.2;margin:0 0 6px;text-wrap:balance}
h1{font-size:26px;font-weight:600}h2{font-size:18px;font-weight:600}
p{margin:0 0 10px}
.eyebrow{font-size:12px;letter-spacing:.06em;text-transform:uppercase;color:var(--accent);font-weight:600;margin-bottom:6px}
.muted{color:var(--muted)}.small{font-size:13px}
.card{border:1px solid var(--line);border-radius:12px;padding:20px}
.card.narrow{max-width:420px;margin:48px auto 0;width:100%}
.card.highlight{background:var(--hl);border-color:var(--hl-line)}
.top{display:flex;justify-content:space-between;align-items:flex-start;gap:12px;flex-wrap:wrap}
.stack{display:grid;gap:6px;margin-top:8px}
label{font-weight:600;font-size:14px;margin-top:8px}
input,select,textarea{font:inherit;padding:10px 12px;border:1px solid #cdd2da;border-radius:8px;width:100%;background:#fff;color:var(--ink)}
textarea{resize:vertical}
input:focus,select:focus,textarea:focus,button:focus-visible,summary:focus-visible{outline:2px solid var(--accent);outline-offset:1px}
.row{display:flex;gap:10px;flex-wrap:wrap;margin-top:12px}
button{font:600 14px/1 "Public Sans",system-ui,sans-serif;padding:11px 16px;border-radius:8px;border:1px solid #cdd2da;background:#fff;color:var(--ink);cursor:pointer}
button.primary{background:var(--accent);border-color:var(--accent);color:var(--accent-ink)}
button.danger{color:var(--bad);border-color:#f1b8b2}
button.ghost:hover{background:var(--soft)}
.alert{padding:12px 14px;border-radius:10px;font-weight:500}
.alert.good{background:var(--good-bg);color:var(--good)}.alert.bad{background:var(--bad-bg);color:var(--bad)}
.pill{display:inline-block;font-size:12px;font-weight:600;padding:3px 9px;border-radius:999px;margin-right:6px}
.pill.on{background:var(--good-bg);color:var(--good)}.pill.off{background:var(--soft);color:var(--muted)}
.status{margin:0}
.code{font-family:ui-monospace,Consolas,monospace;font-size:14px;word-break:break-all}
p.code{font-size:20px;background:#fff;border:1px dashed var(--hl-line);padding:10px 12px;border-radius:8px}
.kv{display:grid;grid-template-columns:max-content 1fr;gap:8px 16px;margin:8px 0 0}
.kv dt{font-weight:600}.kv dd{margin:0}
details.org{border-top:1px solid var(--line);padding:12px 0}
details.org summary{cursor:pointer;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
ul.plain{list-style:none;padding:0;margin:0;display:grid;gap:4px}
@media (max-width:520px){.kv{grid-template-columns:1fr}.kv dt{margin-top:6px}}
"""

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>{title} · Inanovai Assistant</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Public+Sans:wght@400;500;600&family=Sora:wght@600&display=swap">
<style nonce="{nonce}">{css}</style></head>
<body><main>{body}</main>
<script nonce="{nonce}">
document.querySelectorAll('form[data-confirm]').forEach(function(f){{
  f.addEventListener('submit',function(ev){{ if(!confirm(f.dataset.confirm)) ev.preventDefault(); }});
}});
document.querySelectorAll('form.ai-form').forEach(function(f){{
  var sel=f.querySelector('[name=provider]'), url=f.querySelector('[name=base_url]'),
      hint=f.querySelector('.url-hint'), models=f.querySelector('[name=models]');
  function sync(){{
    var o=sel.options[sel.selectedIndex];
    var fixed=o.dataset.url||'';
    hint.textContent = fixed ? '(leave blank to use '+fixed+')' : (o.value ? '(required)' : '');
    url.placeholder = o.dataset.urlHint || 'https://…';
    url.required = !fixed && !!o.value;
    models.placeholder = o.dataset.modelHint ? 'e.g. '+o.dataset.modelHint : '';
  }}
  sel.addEventListener('change',sync); sync();
}});
</script></body></html>"""
