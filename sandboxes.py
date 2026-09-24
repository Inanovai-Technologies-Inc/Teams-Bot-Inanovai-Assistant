"""
ONE SANDBOX PER PERSON.

NemoClaw gives each person a private workspace -- a sandbox -- where the
assistant can keep files, run tools and remember their work. This file
decides WHOSE sandbox a message belongs to, and remembers the answer.

    person -> identity -> sandbox

Identity is their work email when the chat app tells us one
(ravi@acme.com), so the same person keeps one sandbox whether they write
from Microsoft Teams or Google Chat. When there is no email, their chat
account id is used instead, which means one sandbox per chat app.

Only people from registered organizations get a sandbox, and sandboxes
are never deleted automatically -- their work is kept until someone
removes it on purpose.

Nothing here talks to an AI. It maps people to sandboxes and asks
whichever NemoClaw server is configured (NEMOCLAW_URL) to do the work.
"""

import asyncio
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("sandboxes")

# How long to wait for NemoClaw before giving up on one message.
NEMOCLAW_TIMEOUT = 120.0

EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def clean_email(value: str) -> str:
    value = (value or "").strip().lower()
    return value if EMAIL.match(value) else ""


@dataclass
class Person:
    """Who is asking, and which sandbox is theirs."""
    org_id: str
    org_name: str
    identity: str               # their email, or "<chat app>:<account id>"
    display_name: str = "there"
    by_email: bool = False

    @property
    def key(self) -> str:
        """One person in one organization. Kept apart from every other."""
        return f"{self.org_id}|{self.identity}"


def identify(context, org) -> Person | None:
    """Work out whose sandbox this message belongs to.

    Returns None when the message is not from a registered organization,
    because only their people get a sandbox.
    """
    if org is None:
        return None
    extra = context.extra or {}
    email = clean_email(extra.get("email", ""))
    identity = email or f"{extra.get('channel', 'chat')}:{context.user_id}"
    return Person(org_id=org.id, org_name=org.name, identity=identity,
                  display_name=context.user_name or "there", by_email=bool(email))


@dataclass
class Sandbox:
    key: str
    org_id: str
    identity: str
    sandbox_id: str
    display_name: str = ""
    created: str = ""
    last_seen: str = ""
    messages: int = 0


class SandboxBook:
    """Remembers which sandbox belongs to which person.

    One JSON file, re-read when it changes on disk and replaced in one
    step when written, the same way orgs.json works.
    """

    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._rows: dict = {}
        self._mtime = None
        self._load()

    def _load(self) -> None:
        try:
            mtime = self.path.stat().st_mtime
        except FileNotFoundError:
            self._rows, self._mtime = {}, None
            return
        if mtime == self._mtime:
            return
        with self.path.open(encoding="utf-8") as f:
            raw = json.load(f).get("sandboxes", {})
        self._rows = {k: Sandbox(key=k, **v) for k, v in raw.items()}
        self._mtime = mtime

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"sandboxes": {k: {f: getattr(row, f) for f in
                                  ("org_id", "identity", "sandbox_id", "display_name",
                                   "created", "last_seen", "messages")}
                              for k, row in self._rows.items()}}
        tmp = self.path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
        os.replace(tmp, self.path)
        self._mtime = self.path.stat().st_mtime

    def get(self, person: Person) -> Sandbox | None:
        with self._lock:
            self._load()
            return self._rows.get(person.key)

    def remember(self, person: Person, sandbox_id: str) -> Sandbox:
        with self._lock:
            self._load()
            now = time.strftime("%Y-%m-%d %H:%M:%S")
            row = self._rows.get(person.key)
            if row is None:
                row = Sandbox(key=person.key, org_id=person.org_id, identity=person.identity,
                              sandbox_id=sandbox_id, display_name=person.display_name,
                              created=now)
                self._rows[person.key] = row
                log.info("new sandbox %s for %s", sandbox_id, person.key)
            row.sandbox_id = sandbox_id
            row.display_name = person.display_name or row.display_name
            row.last_seen = now
            row.messages += 1
            self._save()
            return row

    def for_org(self, org_id: str) -> list:
        with self._lock:
            self._load()
            return sorted((r for r in self._rows.values() if r.org_id == org_id),
                          key=lambda r: r.identity)

    def all(self) -> list:
        with self._lock:
            self._load()
            return list(self._rows.values())

    def forget(self, key: str) -> None:
        """Drop our note of a sandbox. Does not delete it in NemoClaw."""
        with self._lock:
            self._load()
            self._rows.pop(key, None)
            self._save()


# ---------------------------------------------------------------------------
# Talking to NemoClaw
# ---------------------------------------------------------------------------

class NemoClaw:
    """A NemoClaw server, reached over HTTP.

    Two calls are used: make sure a person's sandbox exists, and ask it a
    question. The exact addresses are settings, so they can be pointed at
    the real server once it is running.
    """

    def __init__(self, base_url: str, api_key: str = "",
                 timeout: float = NEMOCLAW_TIMEOUT):
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    async def _post(self, path: str, payload: dict) -> dict:
        import aiohttp

        url = f"{self.base_url}{path}"
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload, headers=self._headers()) as resp:
                body = await resp.json(content_type=None)
                if resp.status >= 400:
                    raise RuntimeError(f"NemoClaw said {resp.status}: {str(body)[:200]}")
                return body or {}

    async def ensure_sandbox(self, person: Person) -> str:
        """Create the person's sandbox if it is not there, and return its id."""
        body = await self._post("/sandboxes", {
            "name": person.key,
            "owner": person.identity,
            "organization": person.org_id,
            "display_name": person.display_name,
        })
        sandbox_id = body.get("id") or body.get("sandbox_id") or ""
        if not sandbox_id:
            raise RuntimeError("NemoClaw did not return a sandbox id")
        return sandbox_id

    async def ask(self, sandbox_id: str, message: str) -> str:
        body = await self._post(f"/sandboxes/{sandbox_id}/messages", {"message": message})
        reply = body.get("reply") or body.get("text") or ""
        return reply.strip()


class PretendNemoClaw:
    """Stands in for NemoClaw until a real server exists.

    Gives every person a sandbox id and repeats their message back, so the
    whole path -- identity, sandbox, reply -- can be tested for free.
    """

    def __init__(self):
        self.created: dict = {}
        self.asked: list = []

    async def ensure_sandbox(self, person: Person) -> str:
        self.created.setdefault(person.key, f"sb-{len(self.created) + 1}")
        return self.created[person.key]

    async def ask(self, sandbox_id: str, message: str) -> str:
        self.asked.append((sandbox_id, message))
        return f"[{sandbox_id}] {message}"


def build_nemoclaw():
    """The NemoClaw server from the settings, or None when there is none."""
    url = os.environ.get("NEMOCLAW_URL", "").strip()
    if not url:
        return None
    return NemoClaw(url, os.environ.get("NEMOCLAW_API_KEY", "").strip())


def default_book_path() -> Path:
    import orgs

    return orgs.default_data_dir() / "sandboxes.json"
