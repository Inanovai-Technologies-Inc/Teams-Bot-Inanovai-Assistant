"""
ORGANIZATIONS AND THEIR OWN AI KEYS.

Each client organization can plug in its own AI -- one provider and key,
or several (say an OpenAI key and a Gemini key). When one of their people
sends a message, the bot answers with THEIR model and THEIR key, and
never falls back to ours.

How a message is matched to an organization:

    Microsoft Teams   teams:<tenant id>        from the Teams activity
    Google Chat       google:<email domain>    from the sender's email

Everything is kept in one JSON file (orgs.json in DATA_DIR). API keys in
it are encrypted with ORG_SECRETS_KEY, and access codes are stored only
as salted hashes, so the file on its own gives nothing away.

    python orgs.py new-secret-key     print a fresh ORG_SECRETS_KEY
"""

import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import socket
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

log = logging.getLogger("orgs")


# ---------------------------------------------------------------------------
# AI providers a client can choose from. All of them speak the OpenAI chat
# API, so one client library talks to every one -- only the address differs.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Provider:
    key: str
    label: str
    base_url: str          # blank = the client must type their own address
    url_hint: str
    model_hint: str


PROVIDERS = {
    "openai": Provider(
        "openai", "OpenAI", "https://api.openai.com/v1",
        "Leave blank", "gpt-4o-mini"),
    "azure": Provider(
        "azure", "Azure OpenAI", "",
        "https://YOUR-RESOURCE.openai.azure.com/openai/v1", "your deployment name"),
    "gemini": Provider(
        "gemini", "Google Gemini", "https://generativelanguage.googleapis.com/v1beta/openai",
        "Leave blank", "gemini-2.5-flash"),
    "custom": Provider(
        "custom", "Other (any OpenAI-compatible API, vLLM, Ollama, OpenRouter)", "",
        "https://ai.your-company.com/v1", "the model name your server uses"),
}

MAX_MODELS = 10
MAX_AIS = 10                    # saved AIs (keys) per organization
ACCESS_CODE_BYTES = 12          # 16 characters once encoded
HASH_ITERATIONS = 200_000


@dataclass
class LLMConfig:
    """One of an organization's saved AIs, with the key decrypted."""
    id: str
    label: str
    provider: str
    base_url: str
    models: list
    api_key: str
    version: str                # changes whenever the settings change


@dataclass
class Org:
    id: str
    name: str
    tenants: list = field(default_factory=list)
    # Saved AIs, in order; the first one's first model is the default.
    # Stored form: each key is still encrypted.
    ais: list = field(default_factory=list)
    created: str = ""

    @property
    def has_own_ai(self) -> bool:
        return any(ai.get("key") for ai in self.ais)

    @property
    def all_models(self) -> list:
        return [m for ai in self.ais for m in ai.get("models") or []]


def _ais_from_stored(d: dict) -> list:
    """Saved AIs from the file. Organizations saved before several keys
    were allowed have a single "llm" entry; it becomes a list of one."""
    if "ais" in d:
        return list(d.get("ais") or [])
    old = d.get("llm")
    if old and old.get("key"):
        label = PROVIDERS.get(old.get("provider"), PROVIDERS["custom"]).label.split(" (")[0]
        return [{"id": "ai-1", "label": label, **old}]
    return []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def normalise_tenant(value: str) -> str:
    """'Teams:ABC' -> 'teams:abc', 'google:Acme.com ' -> 'google:acme.com'."""
    value = (value or "").strip().lower()
    if ":" not in value:
        return ""
    kind, _, ident = value.partition(":")
    kind, ident = kind.strip(), ident.strip()
    if kind not in ("teams", "google") or not ident:
        return ""
    return f"{kind}:{ident}"


def google_tenant(email: str) -> str:
    """The Google Chat tenant for a sender's email address."""
    if not email or "@" not in email:
        return ""
    return normalise_tenant("google:" + email.rsplit("@", 1)[1])


def teams_tenant(tenant_id: str) -> str:
    return normalise_tenant("teams:" + tenant_id) if tenant_id else ""


def parse_models(text: str) -> list:
    """'gpt-4o, gpt-4o-mini' -> ['gpt-4o', 'gpt-4o-mini'] (deduplicated)."""
    seen, out = set(), []
    for part in re.split(r"[,\n]", text or ""):
        name = part.strip()
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return out[:MAX_MODELS]


def _hash_code(code: str, salt: bytes) -> str:
    digest = hashlib.pbkdf2_hmac("sha256", code.encode(), salt, HASH_ITERATIONS)
    return base64.b64encode(digest).decode()


def new_access_code() -> str:
    return secrets.token_urlsafe(ACCESS_CODE_BYTES)


def is_public_ip(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    return not (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified)


def check_base_url(url: str, allow_private: bool = False) -> str | None:
    """Return a reason the address is unsafe to call, or None when it is fine.

    The bot calls whatever address a client saves, so an address that
    points back inside our own network (the cloud metadata service,
    localhost) must be refused.
    """
    parsed = urlparse(url or "")
    if parsed.scheme != "https" and not (allow_private and parsed.scheme == "http"):
        return "The address must start with https://"
    host = parsed.hostname
    if not host:
        return "That does not look like a web address."
    if allow_private:
        return None
    try:
        infos = socket.getaddrinfo(host, parsed.port or 443, proto=socket.IPPROTO_TCP)
    except OSError:
        return "That address could not be found. Check it for typos."
    if not all(is_public_ip(info[4][0]) for info in infos):
        return "That address points to a private network. Use a public https address."
    return None


# ---------------------------------------------------------------------------
# Calling a client's address safely
#
# check_base_url runs when the address is saved, but a web address can be
# re-pointed afterwards. So every connection to a client's AI -- redirects
# included -- looks the address up again, refuses anything private, and
# connects to the exact IP it checked, so it cannot change in between.
# HTTPS still verifies the certificate against the real hostname.
# ---------------------------------------------------------------------------

def public_http_client(allow_private: bool = False):
    """An HTTP client for the AI library that only ever reaches public IPs."""
    import asyncio

    import httpcore2
    import httpx2

    if allow_private:
        return httpx2.AsyncClient()

    class PublicOnlyBackend(httpcore2.AsyncNetworkBackend):
        def __init__(self):
            self._inner = httpcore2.AnyIOBackend()

        async def connect_tcp(self, host, port, timeout=None, local_address=None,
                              socket_options=None):
            loop = asyncio.get_running_loop()
            infos = await loop.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
            addresses = [info[4][0] for info in infos]
            if not addresses or not all(is_public_ip(a) for a in addresses):
                # A normal connection error, so callers treat it like any
                # unreachable address.
                raise httpcore2.ConnectError(f"refused to connect to {host}: not a public address")
            return await self._inner.connect_tcp(addresses[0], port, timeout=timeout,
                                                 local_address=local_address,
                                                 socket_options=socket_options)

        async def connect_unix_socket(self, path, timeout=None, socket_options=None):
            raise httpcore2.ConnectError("local sockets are not allowed")

        async def sleep(self, seconds):
            await self._inner.sleep(seconds)

    class PublicOnlyTransport(httpx2.AsyncHTTPTransport):
        def __init__(self):
            # No proxies from the environment: every hop must be checked here.
            super().__init__(trust_env=False)
            ssl_context = getattr(self._pool, "_ssl_context", None)
            self._pool = httpcore2.AsyncConnectionPool(
                ssl_context=ssl_context, network_backend=PublicOnlyBackend(),
                max_connections=20, keepalive_expiry=5.0)

    return httpx2.AsyncClient(transport=PublicOnlyTransport(), trust_env=False)


def default_data_dir() -> Path:
    override = os.environ.get("DATA_DIR", "").strip()
    if override:
        return Path(override)
    # On Azure App Service only /home survives restarts and redeploys.
    if os.environ.get("WEBSITE_SITE_NAME"):
        return Path("/home/data")
    return Path(__file__).with_name("data")


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------

class Registry:
    """Every organization, their tenants and their AI settings.

    Backed by one JSON file. It is re-read when the file changes on disk,
    and every write replaces the file in one step so a crash can never
    leave it half written.
    """

    def __init__(self, path: Path, secret_key: str = ""):
        self.path = Path(path)
        self._fernet = None
        if secret_key:
            from cryptography.fernet import Fernet
            self._fernet = Fernet(secret_key.encode() if isinstance(secret_key, str) else secret_key)
        self._lock = threading.Lock()
        self._orgs: dict = {}
        self._codes: dict = {}
        self._mtime = None
        # Teams tenants and Google domains that messaged us but are not
        # registered yet, newest first. Shown on the admin page so the team
        # can register a new client without hunting for ids.
        self.unregistered: deque = deque(maxlen=25)
        self._load()

    # ------------------------------------------------------------ storage

    @property
    def enabled(self) -> bool:
        """True when keys can be saved: ORG_SECRETS_KEY is set."""
        return self._fernet is not None

    def _load(self) -> None:
        try:
            mtime = self.path.stat().st_mtime
        except FileNotFoundError:
            self._orgs, self._codes, self._mtime = {}, {}, None
            return
        if mtime == self._mtime:
            return
        with self.path.open(encoding="utf-8") as f:
            raw = json.load(f).get("orgs", {})
        # Access-code hashes are kept off the Org object, so nothing that
        # displays an organization can leak them.
        self._codes = {oid: {k: d[k] for k in ("code_hash", "code_salt") if k in d}
                       for oid, d in raw.items()}
        self._orgs = {oid: Org(id=oid, name=d.get("name", ""), tenants=d.get("tenants", []),
                               ais=_ais_from_stored(d), created=d.get("created", ""))
                      for oid, d in raw.items()}
        self._mtime = mtime

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"orgs": {o.id: {"name": o.name, "tenants": o.tenants, "ais": o.ais,
                                "created": o.created, **self._codes.get(o.id, {})}
                         for o in self._orgs.values()}}
        tmp = self.path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
        os.replace(tmp, self.path)
        self._mtime = self.path.stat().st_mtime

    def _write_code(self, org_id: str, code: str) -> None:
        salt = secrets.token_bytes(16)
        self._codes[org_id] = {"code_hash": _hash_code(code, salt),
                               "code_salt": base64.b64encode(salt).decode()}
        self._save()

    # ------------------------------------------------------------ reading

    def all(self) -> list:
        with self._lock:
            self._load()
            return sorted(self._orgs.values(), key=lambda o: o.name.lower())

    def get(self, org_id: str) -> Org | None:
        with self._lock:
            self._load()
            return self._orgs.get(org_id or "")

    def find(self, tenant: str) -> Org | None:
        """The organization a Teams tenant or Google domain belongs to."""
        tenant = normalise_tenant(tenant)
        if not tenant:
            return None
        with self._lock:
            self._load()
            for org in self._orgs.values():
                if tenant in org.tenants:
                    return org
        if tenant not in self.unregistered:
            self.unregistered.appendleft(tenant)
            log.info("message from an unregistered organization: %s", tenant)
        return None

    def ais_for(self, org: Org | None) -> list:
        """The organization's own AIs, keys decrypted. Empty = use our default."""
        if not org or not self.enabled:
            return []
        out = []
        for ai in org.ais:
            if not ai.get("key"):
                continue
            try:
                key = self._fernet.decrypt(ai["key"].encode()).decode()
            except Exception:
                log.error("could not decrypt AI key %s for %s -- was ORG_SECRETS_KEY changed?",
                          ai.get("id"), org.id)
                continue
            out.append(LLMConfig(id=ai["id"], label=ai.get("label") or ai["id"],
                                 provider=ai.get("provider", "custom"),
                                 base_url=ai.get("base_url", ""),
                                 models=list(ai.get("models") or []),
                                 api_key=key, version=ai.get("updated", "")))
        return out

    def check_code(self, org_id: str, code: str) -> bool:
        with self._lock:
            self._load()
            stored = self._codes.get(org_id or "")
        if not stored or not code:
            # Spend the same time either way, so a wrong org id and a wrong
            # code look identical from outside.
            _hash_code(code or "x", b"0" * 16)
            return False
        salt = base64.b64decode(stored["code_salt"])
        return hmac.compare_digest(_hash_code(code, salt), stored["code_hash"])

    # ------------------------------------------------------------ writing

    def create(self, name: str, tenants: list) -> tuple:
        """Add an organization. Returns (org, access code). The code is
        shown once and never stored in readable form."""
        name = (name or "").strip()
        if not name:
            raise ValueError("Give the organization a name.")
        clean = self._clean_tenants(tenants)
        with self._lock:
            self._load()
            org = Org(id="org-" + secrets.token_hex(4), name=name, tenants=clean,
                      created=time.strftime("%Y-%m-%d"))
            self._orgs[org.id] = org
            code = new_access_code()
            self._write_code(org.id, code)
        for t in clean:
            if t in self.unregistered:
                self.unregistered.remove(t)
        return org, code

    def update(self, org_id: str, name: str, tenants: list) -> Org:
        clean = self._clean_tenants(tenants, ignore=org_id)
        with self._lock:
            self._load()
            org = self._orgs[org_id]
            org.name = (name or "").strip() or org.name
            org.tenants = clean
            self._save()
        for t in clean:
            if t in self.unregistered:
                self.unregistered.remove(t)
        return org

    def reset_code(self, org_id: str) -> str:
        with self._lock:
            self._load()
            if org_id not in self._orgs:
                raise KeyError(org_id)
            code = new_access_code()
            self._write_code(org_id, code)
            return code

    def delete(self, org_id: str) -> None:
        with self._lock:
            self._load()
            self._orgs.pop(org_id, None)
            self._codes.pop(org_id, None)
            self._save()

    def save_ai(self, org_id: str, ai_id: str, label: str, provider: str,
                base_url: str, models: list, api_key: str | None) -> str:
        """Add a new AI (blank ai_id) or change a saved one.

        A blank api_key keeps the saved key. Returns the AI's id.
        """
        if not self.enabled:
            raise RuntimeError("ORG_SECRETS_KEY is not set, so keys cannot be saved.")
        with self._lock:
            self._load()
            org = self._orgs[org_id]
            existing = next((a for a in org.ais if a["id"] == ai_id), None) if ai_id else None
            if ai_id and existing is None:
                raise KeyError(ai_id)
            if existing is None and len(org.ais) >= MAX_AIS:
                raise ValueError(f"You can save up to {MAX_AIS} AIs. Remove one first.")
            key_token = (existing or {}).get("key", "")
            if api_key:
                key_token = self._fernet.encrypt(api_key.encode()).decode()
            if not key_token:
                raise ValueError("Paste your API key.")
            label = (label or "").strip()[:40] or PROVIDERS[provider].label.split(" (")[0]
            record = {"id": ai_id or self._next_ai_id(org), "label": label,
                      "provider": provider, "base_url": base_url, "models": models,
                      "key": key_token, "updated": time.strftime("%Y-%m-%d %H:%M:%S")}
            if existing is None:
                org.ais.append(record)
            else:
                org.ais[org.ais.index(existing)] = record
            self._save()
            return record["id"]

    def remove_ai(self, org_id: str, ai_id: str) -> None:
        with self._lock:
            self._load()
            org = self._orgs[org_id]
            org.ais = [a for a in org.ais if a["id"] != ai_id]
            self._save()

    def make_default(self, org_id: str, ai_id: str) -> None:
        """Move an AI to the top, so its first model is the default."""
        with self._lock:
            self._load()
            org = self._orgs[org_id]
            chosen = [a for a in org.ais if a["id"] == ai_id]
            org.ais = chosen + [a for a in org.ais if a["id"] != ai_id]
            self._save()

    @staticmethod
    def _next_ai_id(org: Org) -> str:
        taken = {a["id"] for a in org.ais}
        n = 1
        while f"ai-{n}" in taken:
            n += 1
        return f"ai-{n}"

    def _clean_tenants(self, tenants: list, ignore: str = "") -> list:
        clean = []
        for raw in tenants or []:
            t = normalise_tenant(raw)
            if not t:
                raise ValueError(f"'{raw}' is not valid. Use teams:<tenant id> or google:<domain>.")
            if t not in clean:
                clean.append(t)
        with self._lock:
            self._load()
            for org in self._orgs.values():
                if org.id == ignore:
                    continue
                taken = set(clean) & set(org.tenants)
                if taken:
                    raise ValueError(f"{', '.join(sorted(taken))} already belongs to {org.name}.")
        return clean


# ---------------------------------------------------------------------------
# One shared registry for the running server
# ---------------------------------------------------------------------------

_REGISTRY: Registry | None = None


def get_registry() -> Registry:
    global _REGISTRY
    if _REGISTRY is None:
        key = os.environ.get("ORG_SECRETS_KEY", "").strip()
        _REGISTRY = Registry(default_data_dir() / "orgs.json", key)
        if not key:
            log.warning("ORG_SECRETS_KEY is not set -- organizations cannot save their own AI keys.")
    return _REGISTRY


if __name__ == "__main__":
    import sys

    if sys.argv[1:] == ["new-secret-key"]:
        from cryptography.fernet import Fernet
        print(Fernet.generate_key().decode())
    else:
        print(__doc__)
