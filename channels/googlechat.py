"""
Google Chat channel.

Google Chat posts every event to an HTTP endpoint configured in Google
Cloud. This file checks the request really came from Google, asks the
agent, and replies.

Google sends one of two formats, depending on how the app was created in
Google Cloud. Both are accepted:

  Chat app          {"type": "MESSAGE", "message": {...}, "space": {...}}
                    signed by chat@system.gserviceaccount.com
                    reply {"text": "..."}

  Workspace add-on  {"chat": {"messagePayload": {"message": {...}}}}
                    signed by service-<project>@gcp-sa-gsuiteaddons...
                    reply {"hostAppDataAction": {"chatDataAction": ...}}

Google waits 30 seconds for an answer. Most replies arrive well inside
that and go straight back in the response. When a model is slower, the
channel answers Google at once with nothing, keeps working, and posts the
reply into the conversation afterwards through the Chat API. That second
path needs a service account; without one, a slow answer gets a short
"still thinking" note instead.

Setup:
  1. console.cloud.google.com -> new project -> enable "Google Chat API".
  2. Google Chat API -> Configuration:
       Connection settings      HTTP endpoint URL
       URL                      https://<your-host>/googlechat/webhook
       Authentication audience  HTTP endpoint URL
  3. Put that same URL in .env as GOOGLE_CHAT_AUDIENCE.
  4. Optional: GOOGLE_CHAT_PROJECT_NUMBER limits add-on requests to your
     own Google Cloud project.
  5. For slow replies: IAM -> Service accounts -> create one -> Keys ->
     Add key -> JSON. Put the file's contents in
     GOOGLE_CHAT_SERVICE_ACCOUNT_JSON, either as-is or base64-encoded.
"""

import asyncio
import base64
import json
import logging
import re

import aiohttp
from aiohttp import web

from channels.base import ChannelBase
from orgs import google_tenant

log = logging.getLogger("googlechat")

WEBHOOK_PATH = "/googlechat/webhook"

# Who signs genuine requests. A plain Chat app is signed by the first; an
# app built as a Workspace add-on by a service agent tied to its project.
CHAT_ISSUER = "chat@system.gserviceaccount.com"
ADDON_ISSUER = re.compile(
    r"^service-(\d+)@gcp-sa-gsuiteaddons\.iam\.gserviceaccount\.com$")

CHAT_SCOPE = "https://www.googleapis.com/auth/chat.bot"
CHAT_API = "https://chat.googleapis.com/v1"

# Google gives up after 30 seconds. Stop waiting a little before that so
# there is still time to hand the reply back.
SYNC_BUDGET = 25.0

# Keep each posted message comfortably inside Google's size limit.
CHUNK = 4000

WELCOME = (
    "Hi! I'm Inanovai Assistant. Ask me anything. "
    "Type `model` to choose which AI answers, or `reset` to start over."
)
STILL_THINKING = (
    "Still thinking - this model is slow right now. "
    "Ask again in a moment, or type `model` to pick a faster one."
)


def split_text(text: str) -> list:
    return [text[i:i + CHUNK] for i in range(0, len(text), CHUNK)] or [""]


def load_service_account(raw: str):
    """Read a service account key given as raw JSON or as base64 of it.

    Returns the parsed key, or None when it is missing or unusable. A bad
    value is logged rather than raised, so a typo in one setting never
    stops the whole bot from starting.
    """
    if not raw or not raw.strip():
        return None
    raw = raw.strip()
    if not raw.startswith("{"):
        try:
            raw = base64.b64decode(raw, validate=True).decode("utf-8")
        except Exception:
            log.warning("GOOGLE_CHAT_SERVICE_ACCOUNT_JSON is neither JSON nor base64")
            return None
    try:
        info = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("GOOGLE_CHAT_SERVICE_ACCOUNT_JSON is not valid JSON")
        return None
    if info.get("type") != "service_account" or "private_key" not in info:
        log.warning("GOOGLE_CHAT_SERVICE_ACCOUNT_JSON is not a service account key")
        return None
    return info


def trusted_sender(email, project_number=None) -> bool:
    """True when a verified token was issued to Google Chat itself.

    The signature and audience are checked before this runs, so only
    Google can have produced the token. This decides whether it was the
    Chat service that did, and not some other Google account.
    """
    if not email:
        return False
    if email == CHAT_ISSUER:
        return True
    match = ADDON_ISSUER.match(email)
    if not match:
        return False
    return project_number is None or match.group(1) == str(project_number)


def parse_event(event: dict) -> dict:
    """Read either event format into one shape.

    Returns `format` ("chat" or "addon"), `kind` (MESSAGE, ADDED_TO_SPACE,
    REMOVED_FROM_SPACE, the event's own type, or None when unrecognised),
    and the `message`, `space` and `user` objects.
    """
    chat = event.get("chat")
    if isinstance(chat, dict):
        user = chat.get("user") or {}
        if "messagePayload" in chat:
            payload = chat.get("messagePayload") or {}
            message = payload.get("message") or {}
            return {
                "format": "addon",
                "kind": "MESSAGE",
                "message": message,
                "space": (payload.get("space") or message.get("space")
                          or chat.get("space") or {}),
                "user": user or message.get("sender") or {},
            }
        for key, kind in (("addedToSpacePayload", "ADDED_TO_SPACE"),
                          ("removedFromSpacePayload", "REMOVED_FROM_SPACE")):
            if key in chat:
                payload = chat.get(key) or {}
                return {"format": "addon", "kind": kind, "message": {},
                        "space": payload.get("space") or chat.get("space") or {},
                        "user": user}
        return {"format": "addon", "kind": None, "message": {},
                "space": chat.get("space") or {}, "user": user}

    return {"format": "chat", "kind": event.get("type"),
            "message": event.get("message") or {},
            "space": event.get("space") or {},
            "user": event.get("user") or {}}


def reply_body(fmt: str, text: str) -> dict:
    """Wrap a reply the way each format expects it."""
    if fmt == "addon":
        return {"hostAppDataAction": {"chatDataAction": {
            "createMessageAction": {"message": {"text": text}}}}}
    return {"text": text}


class GoogleChatChannel(ChannelBase):
    name = "googlechat"

    def __init__(self, agent, audience: str, service_account_info=None,
                 project_number=None, sync_budget: float = SYNC_BUDGET, **kw):
        super().__init__(agent, **kw)
        # The endpoint URL exactly as typed into Google Cloud. Google puts it
        # in every token, and a mismatch means the token was meant elsewhere.
        self.audience = audience
        self.service_account_info = service_account_info
        self.project_number = project_number
        self.sync_budget = sync_budget
        self._credentials = None
        # Replies finished after Google stopped waiting. They must stay
        # referenced here, or the event loop can drop them mid-flight.
        self._pending: set = set()

    @property
    def can_reply_later(self) -> bool:
        return self.service_account_info is not None

    # ---------------------------------------------------------------- routes

    def register(self, app: web.Application) -> None:
        app.router.add_post(WEBHOOK_PATH, self.handle)

    # ------------------------------------------------------------ verification

    async def _verify(self, request) -> bool:
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return False
        token = header[len("Bearer "):].strip()
        if not token:
            return False
        try:
            from google.auth.transport import requests as google_requests
            from google.oauth2 import id_token

            # Checking the signature fetches Google's public certificates,
            # which is a blocking call -- keep it off the event loop.
            claims = await asyncio.to_thread(
                id_token.verify_oauth2_token,
                token, google_requests.Request(), self.audience,
            )
        except Exception as error:
            log.warning("rejected a request with an invalid token: %s", error)
            return False

        email = claims.get("email")
        if not trusted_sender(email, self.project_number):
            # Never log the token itself -- only who it was issued to.
            log.warning("rejected a token issued to %s for audience %s",
                        email, claims.get("aud"))
            return False
        log.info("verified request from %s",
                 "Chat app" if email == CHAT_ISSUER else "Workspace add-on")
        return True

    # ---------------------------------------------------------------- inbound

    async def handle(self, request: web.Request) -> web.Response:
        log.info("request received (auth header present: %s)",
                 request.headers.get("Authorization", "").startswith("Bearer "))
        if not await self._verify(request):
            return web.Response(status=401, text="unauthorized")

        try:
            event = await request.json()
        except Exception:
            return web.Response(status=400, text="bad json")

        parsed = parse_event(event)
        fmt, kind = parsed["format"], parsed["kind"]
        log.info("event format=%s kind=%s", fmt, kind)

        if kind == "ADDED_TO_SPACE":
            return web.json_response(reply_body(fmt, WELCOME))

        if kind != "MESSAGE":
            if kind is None:
                # Keys only, never content.
                log.warning("unrecognised event, top-level keys: %s",
                            sorted(event.keys()))
            # Removed from a space, a card click, or something newer.
            return web.json_response({})

        message = parsed["message"]
        space = parsed["space"]
        user = parsed["user"]

        # argumentText is the message with the bot's @mention removed, which
        # is what the agent should read in a shared space.
        text = (message.get("argumentText") or message.get("text") or "").strip()
        space_name = space.get("name")
        if not text or not space_name:
            return web.json_response({})

        if self.already_handled(message.get("name")):
            log.info("ignoring duplicate message %s", message.get("name"))
            return web.json_response({})

        name = user.get("displayName") or "there"
        thread_name = (message.get("thread") or {}).get("name")
        log.info("[%s] said: %s", name, text)

        # The sender's email domain says which organization they are from.
        context = self.context_for(space_name, user.get("name"), name,
                                   tenant=google_tenant(user.get("email", "")))
        work = asyncio.ensure_future(self.reply_for(text, context))

        # Wait, but never past Google's window. asyncio.wait does not cancel
        # the work when the timeout passes, so it carries on either way.
        done, _ = await asyncio.wait({work}, timeout=self.sync_budget)

        if work in done:
            chunks = split_text(work.result())
            if len(chunks) > 1 and self.can_reply_later:
                self._keep(asyncio.ensure_future(
                    self._post_chunks(space_name, chunks[1:], thread_name)))
            return web.json_response(reply_body(fmt, chunks[0]))

        self._keep(work)
        if self.can_reply_later:
            work.add_done_callback(
                lambda task: self._post_when_ready(task, space_name, thread_name))
            return web.json_response({})

        return web.json_response(reply_body(fmt, STILL_THINKING))

    def _keep(self, task: asyncio.Future) -> None:
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    def _post_when_ready(self, task: asyncio.Future, space_name, thread_name):
        if task.cancelled():
            return
        # reply_for never raises, so the result is always a string.
        self._keep(asyncio.ensure_future(
            self._post_chunks(space_name, split_text(task.result()), thread_name)))

    # --------------------------------------------------------------- outbound

    async def _access_token(self) -> str:
        from google.auth.transport import requests as google_requests
        from google.oauth2 import service_account

        if self._credentials is None:
            self._credentials = service_account.Credentials.from_service_account_info(
                self.service_account_info, scopes=[CHAT_SCOPE])
        if not self._credentials.valid:
            await asyncio.to_thread(self._credentials.refresh,
                                    google_requests.Request())
        return self._credentials.token

    async def _post_chunks(self, space_name: str, chunks: list,
                           thread_name) -> None:
        """Post messages into a space after Google has stopped waiting."""
        try:
            token = await self._access_token()
        except Exception:
            log.exception("could not get a Chat API token -- reply not sent")
            return

        url = f"{CHAT_API}/{space_name}/messages"
        params = {}
        if thread_name:
            params["messageReplyOption"] = "REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"
        headers = {"Authorization": f"Bearer {token}"}
        timeout = aiohttp.ClientTimeout(total=30)

        async with aiohttp.ClientSession(timeout=timeout) as session:
            for chunk in chunks:
                body = {"text": chunk}
                if thread_name:
                    body["thread"] = {"name": thread_name}
                async with session.post(url, params=params, json=body,
                                        headers=headers) as resp:
                    if resp.status >= 400:
                        log.warning("Chat API refused the reply (%s): %s",
                                    resp.status, await resp.text())
                        return
