"""Multi-user auth for the HTTP MCP server — no keys stored server-side.

Two ways for a user to bring their own Lexware API key:

1. **Direct Bearer** (Claude Code, Cursor, scripts): the client sends
   ``Authorization: Bearer <lexware-api-key>`` on every request. The key
   lives only in the user's local MCP config.

2. **OAuth** (claude.ai, ChatGPT — clients that cannot send custom
   headers): the standard MCP OAuth flow with Dynamic Client
   Registration. On ``/consent`` the user pastes their Lexware API key
   once; the server validates it against the Lexware API and issues an
   access token that is simply the key **encrypted with the server
   secret** (Fernet). The token is stored by the *client*, so the key
   keeps living on the user's device — the server only decrypts it per
   request and persists nothing but the OAuth client registrations
   (redirect URIs, no secrets of value) and a denylist of revoked grants.

Every token this module mints (transaction, authorization code, access
and refresh token) is such a sealed, self-contained Fernet blob — the
server needs no session store and survives restarts as long as
``LXW_MCP_SECRET`` stays the same. Only what must stop working (revoked
grants, spent refresh tokens) is recorded, see :class:`TokenDenylist`.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import secrets
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
from cryptography.fernet import Fernet, InvalidToken
from fastmcp.server.auth import OAuthProvider
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    RegistrationError,
    construct_redirect_uri,
)
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyUrl
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from lxw_cli.config import DEFAULT_BASE_URL, config_dir
from lxw_cli.mcp_allowlist import AllowList

# Token lifetimes (seconds). Enforced via Fernet's built-in timestamp.
# Refresh tokens are single-use (rotated on every refresh); presenting one a
# second time revokes the whole grant ("token family"), see TokenDenylist.
ACCESS_TOKEN_TTL = 24 * 3600
REFRESH_TOKEN_TTL = 30 * 24 * 3600
AUTH_CODE_TTL = 10 * 60
TXN_TTL = 10 * 60

ENV_SECRET = "LXW_MCP_SECRET"
ENV_PUBLIC_URL = "LXW_MCP_PUBLIC_URL"
ENV_HOST = "LXW_MCP_HOST"
ENV_PORT = "LXW_MCP_PORT"
ENV_DATA_DIR = "LXW_MCP_DATA_DIR"
ENV_REDIRECT_ALLOW = "LXW_MCP_REDIRECT_ALLOW"

# A passphrase secret is stretched with a single SHA-256 (see
# fernet_from_secret), so it must carry enough entropy on its own to resist
# offline guessing against a token an attacker obtained for their own key.
MIN_SECRET_LENGTH = 32

# Dynamic Client Registration is unauthenticated; bound it so nobody can fill
# the disk or make every registration rewrite a huge file.
REGISTRATION_RATE_LIMIT = 10  # registrations per client IP …
REGISTRATION_RATE_WINDOW = 600  # … within this many seconds
MAX_REGISTERED_CLIENTS = 2000  # oldest registrations are dropped beyond this

# OAuth redirect targets a client may register. Dynamic Client Registration is
# open to anyone, so without this list an attacker could register a client
# that redirects to their own host, send a victim the /authorize link and
# collect the authorization code — i.e. the victim's Lexware key — after the
# victim pasted it on our genuine consent page. Allowed by default:
# - loopback (any port) for native clients such as Claude Code or Cursor,
# - the callback hosts of the hosted clients (https only),
# - the private-use schemes of desktop editors.
# LXW_MCP_REDIRECT_ALLOW adds entries (comma-separated): a bare host allows
# https on that host, "scheme:" allows a private-use scheme.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})
DEFAULT_REDIRECT_HOSTS = frozenset(
    {
        "claude.ai",
        "claude.com",
        "chatgpt.com",
        "chat.openai.com",
        "vscode.dev",
        "insiders.vscode.dev",
    }
)
DEFAULT_REDIRECT_SCHEMES = frozenset({"cursor", "vscode", "vscode-insiders"})

CLAIM_KEY = "lexware_api_key"
CLAIM_INSTALLATION = "lexware_installation"

# Query parameter on the connector URL that names the Lexware installation,
# e.g. https://mcp.example.com/mcp?installation=demo. It only labels the
# connection (and makes the URL distinct, so a client can hold one connector
# per installation) — which installation is used is decided solely by the key.
INSTALLATION_PARAMS = ("installation", "profile")

# Single coarse scope. claude.ai's OAuth expects a granted scope to be
# advertised (scopes_supported) and echoed in the token response; a server
# with no scope at all is a known trigger for "Authorization failed" even
# though the token exchange itself succeeds. One scope keeps that happy
# without adding real authorization granularity (the Lexware key is the
# actual capability).
DEFAULT_SCOPE = "mcp"


# ---------------------------------------------------------------------------
# Secret / Fernet helpers
# ---------------------------------------------------------------------------


def secret_problem(secret: str) -> str | None:
    """Why `secret` is unfit for sealing tokens, or None if it is fine.

    A proper Fernet key (32 random bytes) is always fine; a passphrase must be
    at least MIN_SECRET_LENGTH characters.
    """
    try:
        Fernet(secret.encode())
        return None
    except (ValueError, TypeError):
        pass
    if len(secret) < MIN_SECRET_LENGTH:
        return (
            f"{ENV_SECRET} ist zu kurz ({len(secret)} Zeichen, mindestens "
            f"{MIN_SECRET_LENGTH}). Ein geeignetes Secret erzeugen mit: "
            "python -c \"import secrets; print(secrets.token_urlsafe(48))\""
        )
    return None


def fernet_from_secret(secret: str) -> Fernet:
    """Build a Fernet from either a proper Fernet key or any passphrase.

    A passphrase is stretched via SHA-256 so operators can put a plain
    random string into ``LXW_MCP_SECRET`` without worrying about the
    exact Fernet key format.
    """
    try:
        return Fernet(secret.encode())
    except (ValueError, TypeError):
        digest = hashlib.sha256(secret.encode()).digest()
        return Fernet(base64.urlsafe_b64encode(digest))


# ---------------------------------------------------------------------------
# Per-key client pool
# ---------------------------------------------------------------------------


class ClientPool:
    """Cache one LexwareClient per API key.

    Reusing the client keeps the 2 req/s rate limiter correct per key
    (it is enforced per client instance). Idle clients are evicted so a
    busy multi-user server does not accumulate connections forever.
    """

    def __init__(self, max_idle: float = 15 * 60, max_size: int = 64) -> None:
        self._lock = threading.Lock()
        self._clients: dict[str, tuple[Any, float]] = {}
        self._max_idle = max_idle
        self._max_size = max_size

    def get(self, api_key: str) -> Any:
        # Imported lazily to avoid a circular import (client -> config).
        from lxw_cli.config import Config
        from lxw_cli.core.client import LexwareClient

        digest = hashlib.sha256(api_key.encode()).hexdigest()
        now = time.monotonic()
        with self._lock:
            self._evict(now)
            entry = self._clients.get(digest)
            if entry is None:
                base_url = os.getenv("LEXWARE_API_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
                client = LexwareClient(Config(api_key=api_key, base_url=base_url))
                self._clients[digest] = (client, now)
                return client
            client, _ = entry
            self._clients[digest] = (client, now)
            return client

    def _evict(self, now: float) -> None:
        stale = [k for k, (_, ts) in self._clients.items() if now - ts > self._max_idle]
        # If still over capacity, drop the least recently used entries.
        if len(self._clients) - len(stale) >= self._max_size:
            by_age = sorted(self._clients.items(), key=lambda kv: kv[1][1])
            for k, _ in by_age:
                if len(self._clients) - len(stale) < self._max_size:
                    break
                if k not in stale:
                    stale.append(k)
        for k in stale:
            client, _ = self._clients.pop(k)
            try:
                client.close()
            except Exception:  # noqa: BLE001 — eviction must never fail a request
                pass


pool = ClientPool()


def _redirect_allow_extra() -> tuple[set[str], set[str]]:
    hosts: set[str] = set()
    schemes: set[str] = set()
    for raw in os.environ.get(ENV_REDIRECT_ALLOW, "").split(","):
        entry = raw.strip().lower()
        if not entry:
            continue
        if entry.endswith(":"):
            schemes.add(entry[:-1])
        else:
            hosts.add(entry)
    return hosts, schemes


def redirect_uri_allowed(uri: str) -> bool:
    """True if an OAuth redirect URI points to a trusted target (see above)."""
    try:
        parts = urlsplit(str(uri))
        host = (parts.hostname or "").lower()
    except ValueError:
        return False
    scheme = parts.scheme.lower()
    extra_hosts, extra_schemes = _redirect_allow_extra()
    if scheme in ("http", "https"):
        # Userinfo ("https://claude.ai@evil.example") is never legitimate here.
        if parts.username is not None or parts.password is not None:
            return False
        if host in _LOOPBACK_HOSTS:
            return True
        return scheme == "https" and host in (DEFAULT_REDIRECT_HOSTS | extra_hosts)
    return scheme in (DEFAULT_REDIRECT_SCHEMES | extra_schemes)


def _client_redirects_allowed(info: OAuthClientInformationFull) -> bool:
    uris = info.redirect_uris or []
    return bool(uris) and all(redirect_uri_allowed(str(u)) for u in uris)


def _redirect_target(uri: str) -> str:
    """Short, human-readable form of a redirect URI for the consent page."""
    parts = urlsplit(uri)
    if parts.scheme in ("http", "https"):
        return parts.hostname or uri
    return f"{parts.scheme}:"


def installation_from_url(url: str | None) -> str | None:
    """Extract a valid installation label from a connector/resource URL."""
    if not url:
        return None
    from lxw_cli.config import validate_profile_name
    from lxw_cli.core.errors import ConfigError

    query = parse_qs(urlsplit(url).query)
    for param in INSTALLATION_PARAMS:
        for value in query.get(param, []):
            try:
                return validate_profile_name(value)
            except ConfigError:
                continue
    return None


def request_installation() -> str | None:
    """Installation label of the current HTTP request, if any.

    From the OAuth token (label captured at connect time) or, for direct
    Bearer clients, from the request URL's `?installation=` parameter.
    """
    try:
        from fastmcp.server.dependencies import get_access_token

        token = get_access_token()
    except Exception:  # noqa: BLE001 — no request context (stdio)
        return None
    claims = (getattr(token, "claims", None) or {}) if token is not None else {}
    label = claims.get(CLAIM_INSTALLATION)
    if isinstance(label, str) and label:
        return label
    try:
        from fastmcp.server.dependencies import get_http_request

        return installation_from_url(str(get_http_request().url))
    except Exception:  # noqa: BLE001
        return None


def request_api_key() -> str | None:
    """The per-request Lexware key, or None outside an authenticated HTTP call.

    In HTTP mode the auth provider stashes the (decrypted) key in the
    access token's claims; over stdio there is no token and callers fall
    back to the local single-user config.
    """
    try:
        from fastmcp.server.dependencies import get_access_token

        token = get_access_token()
    except Exception:  # noqa: BLE001 — no request context (stdio)
        return None
    if token is None:
        return None
    claims = getattr(token, "claims", None) or {}
    key = claims.get(CLAIM_KEY)
    return key if isinstance(key, str) and key else None


# ---------------------------------------------------------------------------
# OAuth client registry (the only persisted state — no user keys in here)
# ---------------------------------------------------------------------------


class ClientRegistry:
    """File-backed store for dynamically registered OAuth clients."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()

    def get(self, client_id: str) -> OAuthClientInformationFull | None:
        with self._lock:
            data = self._read()
        raw = data.get(client_id)
        if raw is None:
            return None
        return OAuthClientInformationFull.model_validate(raw)

    def put(
        self, info: OAuthClientInformationFull, max_clients: int | None = None
    ) -> None:
        with self._lock:
            data = self._read()
            data[info.client_id] = json.loads(info.model_dump_json(exclude_none=True))
            if max_clients is not None and len(data) > max_clients:
                # Drop the oldest registrations; a legitimate client that is
                # hit re-registers on its next connect.
                by_age = sorted(
                    data, key=lambda k: data[k].get("client_id_issued_at") or 0
                )
                for stale in by_age[: len(data) - max_clients]:
                    if stale != info.client_id:
                        del data[stale]
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            tmp.replace(self._path)

    def _read(self) -> dict[str, Any]:
        if not self._path.is_file():
            return {}
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}


class TokenDenylist:
    """Revoked grants and spent refresh tokens (the only token state kept).

    Tokens stay self-contained; this list only records what must no longer
    work: whole grants ("families", revoked via /revoke or on refresh-token
    reuse) and individual refresh tokens already exchanged. Entries carry an
    expiry and vanish once the tokens they block would have expired anyway,
    so the file stays small. Single-process, like the auth-code replay guard.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._entries: dict[str, float] = self._read()

    def contains(self, entry: str) -> bool:
        with self._lock:
            expiry = self._entries.get(entry)
            return expiry is not None and expiry > time.time()

    def add(self, entry: str, ttl: float) -> bool:
        """Add `entry`; returns False if it was already present (atomic)."""
        now = time.time()
        with self._lock:
            expiry = self._entries.get(entry)
            if expiry is not None and expiry > now:
                return False
            self._entries = {k: v for k, v in self._entries.items() if v > now}
            self._entries[entry] = now + ttl
            self._write()
            return True

    def _read(self) -> dict[str, float]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(raw, dict):
            return {}
        now = time.time()
        return {
            str(k): float(v)
            for k, v in raw.items()
            if isinstance(v, (int, float)) and v > now
        }

    def _write(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._entries), encoding="utf-8")
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            tmp.replace(self._path)
        except OSError:
            # The in-memory list still protects this process.
            pass


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class RateLimiter:
    """Sliding-window counter per key (client IP)."""

    def __init__(self, limit: int, window: float) -> None:
        self._limit = limit
        self._window = window
        self._lock = threading.Lock()
        self._hits: dict[str, deque[float]] = {}

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            # Forget idle keys so the map can't grow without bound.
            for k in [k for k, q in self._hits.items() if not q or now - q[-1] > self._window]:
                del self._hits[k]
            hits = self._hits.setdefault(key, deque())
            while hits and now - hits[0] > self._window:
                hits.popleft()
            if len(hits) >= self._limit:
                return False
            hits.append(now)
            return True


class _RateLimitedEndpoint:
    """ASGI wrapper that answers 429 once a client IP exceeds the limit."""

    def __init__(self, app: ASGIApp, limiter: RateLimiter) -> None:
        self._app = app
        self._limiter = limiter

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") == "http" and scope.get("method") == "POST":
            client = scope.get("client")
            ip = client[0] if client else "unknown"
            if not self._limiter.allow(ip):
                response = JSONResponse(
                    {
                        "error": "invalid_client_metadata",
                        "error_description": "too many registrations, retry later",
                    },
                    status_code=429,
                    headers={"Retry-After": str(REGISTRATION_RATE_WINDOW)},
                )
                await response(scope, receive, send)
                return
        await self._app(scope, receive, send)


# ---------------------------------------------------------------------------
# Authorization code with the API key riding along (sealed, never stored)
# ---------------------------------------------------------------------------


def _txn_installation(data: dict[str, Any]) -> str | None:
    return installation_from_url(data.get("resource"))


class LexwareAuthCode(AuthorizationCode):
    api_key: str
    installation: str | None = None


class LexwareOAuthProvider(OAuthProvider):
    """Stateless OAuth provider: the Lexware key travels inside the tokens."""

    def __init__(
        self,
        *,
        public_url: str,
        secret: str,
        data_dir: Path | None = None,
        lexware_base_url: str | None = None,
    ) -> None:
        super().__init__(
            base_url=public_url,
            client_registration_options=ClientRegistrationOptions(
                enabled=True,
                valid_scopes=[DEFAULT_SCOPE],
                default_scopes=[DEFAULT_SCOPE],
            ),
            revocation_options=RevocationOptions(enabled=True),
        )
        self._fernet = fernet_from_secret(secret)
        directory = data_dir or _default_data_dir()
        self._clients = ClientRegistry(directory / "oauth-clients.json")
        self._denylist = TokenDenylist(directory / "revoked.json")
        self._register_limiter = RateLimiter(
            REGISTRATION_RATE_LIMIT, REGISTRATION_RATE_WINDOW
        )
        self._lexware_base_url = (
            lexware_base_url
            or os.getenv("LEXWARE_API_BASE_URL", DEFAULT_BASE_URL)
        ).rstrip("/")
        # KYC allow-list: only known Lexware installations may use the server.
        # Inert (mode 'off') unless configured — see lxw_cli.mcp_allowlist.
        self._allow = AllowList(directory, self._lexware_base_url)
        # Best-effort replay guard for auth codes (single process).
        self._used_codes: dict[str, float] = {}
        self._used_lock = threading.Lock()

    # -- sealing helpers ----------------------------------------------------

    def _seal(self, kind: str, payload: dict[str, Any]) -> str:
        body = json.dumps({"t": kind, **payload}, separators=(",", ":"))
        return self._fernet.encrypt(body.encode()).decode()

    def _unseal(self, kind: str, token: str, max_age: int) -> dict[str, Any] | None:
        try:
            raw = self._fernet.decrypt(token.encode(), ttl=max_age)
        except (InvalidToken, UnicodeEncodeError):
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return data if data.get("t") == kind else None

    # -- DCR ------------------------------------------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        info = self._clients.get(client_id)
        # Re-checked on every lookup so registrations stored before the
        # allow-list existed (or entries removed from it later) stop working.
        if info is None or not _client_redirects_allowed(info):
            return None
        return info

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        if not _client_redirects_allowed(client_info):
            raise RegistrationError(
                error="invalid_redirect_uri",
                error_description=(
                    "redirect_uri not allowed on this server (allowed: loopback, "
                    f"known MCP clients, or entries in {ENV_REDIRECT_ALLOW})"
                ),
            )
        self._clients.put(client_info, max_clients=MAX_REGISTERED_CLIENTS)

    # -- authorize → consent page --------------------------------------------

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        txn = self._seal(
            "txn",
            {
                "client_id": client.client_id,
                "redirect_uri": str(params.redirect_uri),
                "explicit": params.redirect_uri_provided_explicitly,
                "state": params.state,
                "challenge": params.code_challenge,
                # Always grant the single coarse scope so the token response
                # carries a scope (claude.ai expects one).
                "scopes": params.scopes or [DEFAULT_SCOPE],
                "resource": str(params.resource) if params.resource else None,
            },
        )
        query = urlencode({"txn": txn})
        return f"{str(self.base_url).rstrip('/')}/consent?{query}"

    def get_routes(self, mcp_path: str | None = None) -> list[Route]:
        routes = [
            Route(
                r.path,
                endpoint=_RateLimitedEndpoint(r.endpoint, self._register_limiter),
                methods=list(r.methods or []),
            )
            if isinstance(r, Route) and r.path == "/register"
            else r
            for r in super().get_routes(mcp_path)
        ]
        routes.append(Route("/consent", self._consent_get, methods=["GET"]))
        routes.append(Route("/consent", self._consent_post, methods=["POST"]))
        return routes

    # -- consent page ----------------------------------------------------------

    async def _consent_get(self, request: Request) -> Response:
        txn = request.query_params.get("txn", "")
        data = self._unseal("txn", txn, TXN_TTL)
        if data is None:
            return _error_page(
                "Der Anmelde-Link ist abgelaufen oder ungültig. "
                "Bitte starte die Verbindung in deiner App erneut."
            )
        client = await self.get_client(data["client_id"])
        if client is None:
            return _error_page("Diese App ist für die Anmeldung nicht zugelassen.")
        client_name = client.client_name or "Deine App"
        return _consent_page(
            txn=txn,
            client_name=client_name,
            installation=_txn_installation(data),
            redirect_target=_redirect_target(data["redirect_uri"]),
        )

    async def _consent_post(self, request: Request) -> Response:
        form = await request.form()
        txn = str(form.get("txn", ""))
        api_key = str(form.get("api_key", "")).strip()
        data = self._unseal("txn", txn, TXN_TTL)
        if data is None:
            return _error_page(
                "Die Sitzung ist abgelaufen. Bitte starte die Verbindung "
                "in deiner App erneut."
            )
        client = await self.get_client(data["client_id"])
        if client is None or not redirect_uri_allowed(data["redirect_uri"]):
            return _error_page("Diese App ist für die Anmeldung nicht zugelassen.")
        client_name = client.client_name or "Deine App"
        installation = _txn_installation(data)
        redirect_target = _redirect_target(data["redirect_uri"])
        if not api_key:
            return _consent_page(
                txn=txn,
                client_name=client_name,
                installation=installation,
                redirect_target=redirect_target,
                error="Bitte einen API-Key eingeben.",
            )

        ok, detail = await self._validate_key(api_key)
        if not ok:
            return _consent_page(
                txn=txn,
                client_name=client_name,
                installation=installation,
                redirect_target=redirect_target,
                error=detail,
            )

        code = self._seal(
            "code",
            {
                "client_id": data["client_id"],
                "redirect_uri": data["redirect_uri"],
                "explicit": data["explicit"],
                "challenge": data["challenge"],
                "scopes": data["scopes"],
                "resource": data.get("resource"),
                "key": api_key,
                "installation": installation,
            },
        )
        redirect = construct_redirect_uri(data["redirect_uri"], code=code, state=data["state"])
        return RedirectResponse(redirect, status_code=302)

    async def _validate_key(self, api_key: str) -> tuple[bool, str]:
        """Probe /v1/profile so we never hand out tokens for a dead key."""
        try:
            async with httpx.AsyncClient(timeout=15) as probe:
                resp = await probe.get(
                    f"{self._lexware_base_url}/v1/profile",
                    headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
                )
        except httpx.HTTPError as exc:
            return False, (
                f"Lexware-API nicht erreichbar ({exc.__class__.__name__}). "
                "Bitte später erneut versuchen."
            )
        if resp.status_code in (401, 403):
            return False, (
                "Der API-Key wurde von Lexware abgelehnt. Bitte prüfen und erneut eingeben."
            )
        if resp.status_code >= 400:
            return False, f"Unerwartete Antwort von Lexware (HTTP {resp.status_code})."
        # Key is valid — now the KYC gate: is this Lexware installation known?
        # Uses the profile we just fetched (no extra call) and warms the cache
        # for the following per-request check. No-op when the gate is 'off'.
        try:
            profile = resp.json()
        except ValueError:
            profile = {}
        decision = self._allow.evaluate_profile(profile, api_key)
        if not decision.allowed:
            return False, decision.user_message
        return True, ""

    # -- code → tokens ---------------------------------------------------------

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> LexwareAuthCode | None:
        data = self._unseal("code", authorization_code, AUTH_CODE_TTL)
        if data is None or data["client_id"] != client.client_id:
            return None
        # Mark used atomically here (not in exchange): the SDK releases the
        # lock between load and exchange, so recording on load is what makes
        # a code truly single-use even under concurrent /token requests.
        with self._used_lock:
            self._prune_used()
            if authorization_code in self._used_codes:
                return None
            self._used_codes[authorization_code] = time.time()
        return LexwareAuthCode(
            code=authorization_code,
            scopes=data["scopes"],
            expires_at=time.time() + AUTH_CODE_TTL,
            client_id=data["client_id"],
            code_challenge=data["challenge"],
            redirect_uri=AnyUrl(data["redirect_uri"]),
            redirect_uri_provided_explicitly=data["explicit"],
            # The SDK's AuthorizationCode.resource is a plain string (RFC 8707
            # audience), NOT an AnyUrl — wrapping it makes pydantic reject it.
            # claude.ai always sends this, so a wrong type here 500s /token.
            resource=data.get("resource") or None,
            api_key=data["key"],
            installation=data.get("installation"),
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: LexwareAuthCode
    ) -> OAuthToken:
        # The code was already marked used in load_authorization_code.
        return self._issue_tokens(
            client_id=client.client_id,
            api_key=authorization_code.api_key,
            scopes=authorization_code.scopes,
            installation=authorization_code.installation,
            family=secrets.token_urlsafe(16),
        )

    def _issue_tokens(
        self,
        *,
        client_id: str,
        api_key: str,
        scopes: list[str],
        installation: str | None = None,
        family: str | None = None,
    ) -> OAuthToken:
        payload: dict[str, Any] = {"client_id": client_id, "key": api_key, "scopes": scopes}
        if installation:
            payload["installation"] = installation
        # The grant ("token family") all tokens of one connection share — the
        # unit that /revoke and refresh-token reuse detection switch off.
        if family:
            payload["fam"] = family
        access = self._seal("access", payload)
        refresh = self._seal("refresh", payload)
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=ACCESS_TOKEN_TTL,
            scope=" ".join(scopes) if scopes else None,
            refresh_token=refresh,
        )

    def _prune_used(self) -> None:
        cutoff = time.time() - 2 * AUTH_CODE_TTL
        for code, ts in list(self._used_codes.items()):
            if ts < cutoff:
                del self._used_codes[code]

    # -- refresh ----------------------------------------------------------------

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        data = self._unseal("refresh", refresh_token, REFRESH_TOKEN_TTL)
        if data is None or data["client_id"] != client.client_id:
            return None
        if self._revoked(refresh_token, data):
            return None
        # Rotation: each refresh token works exactly once. Marked here (like
        # auth codes) so concurrent /token calls can't both pass. A second
        # use means the token leaked — revoke the whole grant, which also
        # kills the access/refresh tokens the legitimate exchange produced.
        if not self._denylist.add(f"rt:{_token_digest(refresh_token)}", REFRESH_TOKEN_TTL):
            self._revoke_family(refresh_token, data)
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=data["client_id"],
            scopes=data["scopes"],
            expires_at=None,
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        data = self._unseal("refresh", refresh_token.token, REFRESH_TOKEN_TTL)
        if data is None:
            raise ValueError("invalid refresh token")
        return self._issue_tokens(
            client_id=client.client_id,
            api_key=data["key"],
            scopes=scopes or data["scopes"],
            installation=data.get("installation"),
            # Tokens minted before families existed start one now.
            family=data.get("fam") or secrets.token_urlsafe(16),
        )

    # -- revocation ------------------------------------------------------------

    def _revoked(self, token: str, data: dict[str, Any]) -> bool:
        fam = data.get("fam")
        if fam and self._denylist.contains(f"fam:{fam}"):
            return True
        return self._denylist.contains(f"tok:{_token_digest(token)}")

    def _revoke_family(self, token: str, data: dict[str, Any]) -> None:
        fam = data.get("fam")
        # Legacy tokens without a family can only be blocked individually.
        entry = f"fam:{fam}" if fam else f"tok:{_token_digest(token)}"
        self._denylist.add(entry, REFRESH_TOKEN_TTL)

    # -- per-request verification -------------------------------------------------

    async def load_access_token(self, token: str) -> AccessToken | None:
        data = self._unseal("access", token, ACCESS_TOKEN_TTL)
        if data is not None:
            if self._revoked(token, data):
                return None
            # KYC gate (cached). Also re-checked here so an org removed from
            # the allow-list is locked out even with a still-valid token.
            if not (await self._allow.evaluate(data["key"])).allowed:
                return None
            claims: dict[str, Any] = {CLAIM_KEY: data["key"]}
            if data.get("installation"):
                claims[CLAIM_INSTALLATION] = data["installation"]
            return AccessToken(
                token=token,
                client_id=data["client_id"],
                scopes=data["scopes"],
                expires_at=None,
                claims=claims,
            )
        # Direct mode: the Bearer value IS the Lexware API key (clients that
        # can send custom headers skip OAuth entirely). We accept it here and
        # let the Lexware API reject invalid keys with 401 on first use — the
        # server never needs to know valid keys up front.
        if _looks_like_api_key(token):
            if not (await self._allow.evaluate(token)).allowed:
                return None
            return AccessToken(
                token=token,
                client_id="direct-bearer",
                scopes=[DEFAULT_SCOPE],
                expires_at=None,
                claims={CLAIM_KEY: token},
            )
        return None

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        # Revoking either token of a grant switches off the whole grant
        # (RFC 7009 allows revoking related tokens). A raw Lexware key used
        # as direct Bearer is not ours to revoke — that happens at Lexware.
        for kind, ttl in (("access", ACCESS_TOKEN_TTL), ("refresh", REFRESH_TOKEN_TTL)):
            data = self._unseal(kind, token.token, ttl)
            if data is not None:
                self._revoke_family(token.token, data)
                return


def _looks_like_api_key(token: str) -> bool:
    """Cheap plausibility check so garbage doesn't reach the Lexware API.

    A valid OAuth token was already handled by _unseal before this is
    reached. A token that *failed* to unseal but still starts with Fernet's
    version byte ("gAAAAA…" — base64 of 0x80) is a corrupt/foreign OAuth
    token, not a raw key: reject it so it can't be forwarded to Lexware and
    produce a confusing upstream 401. Anything else that resembles real key
    material (UUID-ish etc.) passes.
    """
    if not 16 <= len(token) <= 200:
        return False
    if token.startswith("gAAAAA"):
        return False
    return all(c.isalnum() or c in "-_." for c in token)


def _default_data_dir() -> Path:
    override = os.environ.get(ENV_DATA_DIR)
    if override:
        return Path(override).expanduser()
    return config_dir() / "mcp"


# ---------------------------------------------------------------------------
# Consent page HTML
# ---------------------------------------------------------------------------

# The consent page takes the API key: never frame it (clickjacking), never
# cache it, never leak the txn in a Referer. No `form-action` on purpose —
# browsers apply it to the redirect after the POST, which legitimately goes
# to the client's (cross-origin) redirect URI.
_PAGE_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; "
        "frame-ancestors 'none'; base-uri 'none'"
    ),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}

_PAGE_STYLE = """
  :root { color-scheme: light dark; }
  body { font-family: -apple-system, 'Segoe UI', Roboto, sans-serif; margin: 0;
         min-height: 100vh; display: grid; place-items: center;
         background: light-dark(#f4f5f7, #111418); color: light-dark(#1a1c1f, #e8eaed); }
  .card { max-width: 26rem; width: calc(100% - 2rem); padding: 2rem;
          border-radius: 12px; background: light-dark(#fff, #1c2127);
          box-shadow: 0 4px 24px rgba(0,0,0,.12); }
  h1 { font-size: 1.15rem; margin: 0 0 .5rem; }
  p { font-size: .9rem; line-height: 1.45; color: light-dark(#4b5563, #9aa4b2); }
  input[type=password] { width: 100%; box-sizing: border-box; padding: .6rem .7rem;
          border-radius: 8px; border: 1px solid light-dark(#d1d5db, #374151);
          background: transparent; color: inherit; font-size: .95rem; }
  button { margin-top: 1rem; width: 100%; padding: .65rem; border: 0; border-radius: 8px;
           background: #2563eb; color: #fff; font-size: .95rem; cursor: pointer; }
  button:hover { background: #1d4ed8; }
  .err { color: #dc2626; font-size: .85rem; margin: .5rem 0 0; }
  a { color: #2563eb; }
"""


def _consent_page(
    *,
    txn: str,
    client_name: str,
    installation: str | None = None,
    redirect_target: str = "",
    error: str = "",
) -> HTMLResponse:
    error_html = f'<p class="err">{html.escape(error)}</p>' if error else ""
    # The client name is self-declared at registration; the redirect target
    # is what actually receives the access, so show it explicitly.
    target_html = (
        f"<p>Nach dem Verbinden geht der Zugriff an "
        f"<strong>{html.escape(redirect_target)}</strong>. Gib deinen Key nur "
        f"ein, wenn du die Verbindung gerade selbst gestartet hast.</p>"
        if redirect_target
        else ""
    )
    installation_html = (
        f"<p>Diese Verbindung ist für die Lexware-Installation "
        f"<strong>{html.escape(installation)}</strong> — gib den API-Key "
        f"genau dieser Installation ein.</p>"
        if installation
        else ""
    )
    body = f"""<!doctype html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>Lexware MCP verbinden</title><style>{_PAGE_STYLE}</style></head><body>
<form class="card" method="post" action="consent" autocomplete="off">
  <h1>Lexware Office verbinden</h1>
  <p><strong>{html.escape(client_name)}</strong> möchte auf Lexware Office zugreifen.
     Gib dazu deinen persönlichen Lexware-API-Key ein. Du bekommst ihn unter
     <a href="https://app.lexware.de/addons/public-api" target="_blank"
        rel="noopener">app.lexware.de/addons/public-api</a>.</p>
  {installation_html}
  {target_html}
  <input type="hidden" name="txn" value="{html.escape(txn)}">
  <input type="password" name="api_key" placeholder="Lexware API-Key" required autofocus>
  {error_html}
  <p>Der Key wird nicht auf dem Server gespeichert — er wird verschlüsselt in dein
     Zugriffstoken eingebettet und bleibt damit auf deinem Gerät.</p>
  <button type="submit">Verbinden</button>
</form></body></html>"""
    return HTMLResponse(body, headers=_PAGE_HEADERS)


def _error_page(message: str) -> HTMLResponse:
    body = f"""<!doctype html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>Lexware MCP</title><style>{_PAGE_STYLE}</style></head><body>
<div class="card"><h1>Verbindung nicht möglich</h1><p>{html.escape(message)}</p></div>
</body></html>"""
    return HTMLResponse(body, status_code=400, headers=_PAGE_HEADERS)
