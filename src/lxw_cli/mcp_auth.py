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
   (redirect URIs, no secrets of value).

Every token this module mints (transaction, authorization code, access
and refresh token) is such a sealed, self-contained Fernet blob — the
server needs no session store and survives restarts as long as
``LXW_MCP_SECRET`` stays the same.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx
from cryptography.fernet import Fernet, InvalidToken
from fastmcp.server.auth import OAuthProvider
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    construct_redirect_uri,
)
from mcp.server.auth.settings import ClientRegistrationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyUrl
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route

from lxw_cli.config import DEFAULT_BASE_URL, config_dir

# Token lifetimes (seconds). Enforced via Fernet's built-in timestamp.
# Tokens are stateless and cannot be individually revoked, so the refresh
# TTL is kept modest: a leaked refresh token stays usable at most this long
# (rotating LXW_MCP_SECRET or revoking the Lexware key kills it sooner). A
# client used regularly keeps refreshing well within the window.
ACCESS_TOKEN_TTL = 24 * 3600
REFRESH_TOKEN_TTL = 30 * 24 * 3600
AUTH_CODE_TTL = 10 * 60
TXN_TTL = 10 * 60

ENV_SECRET = "LXW_MCP_SECRET"
ENV_PUBLIC_URL = "LXW_MCP_PUBLIC_URL"
ENV_HOST = "LXW_MCP_HOST"
ENV_PORT = "LXW_MCP_PORT"
ENV_DATA_DIR = "LXW_MCP_DATA_DIR"

CLAIM_KEY = "lexware_api_key"


# ---------------------------------------------------------------------------
# Secret / Fernet helpers
# ---------------------------------------------------------------------------


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

    def put(self, info: OAuthClientInformationFull) -> None:
        with self._lock:
            data = self._read()
            data[info.client_id] = json.loads(info.model_dump_json(exclude_none=True))
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


# ---------------------------------------------------------------------------
# Authorization code with the API key riding along (sealed, never stored)
# ---------------------------------------------------------------------------


class LexwareAuthCode(AuthorizationCode):
    api_key: str


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
            client_registration_options=ClientRegistrationOptions(enabled=True),
        )
        self._fernet = fernet_from_secret(secret)
        directory = data_dir or _default_data_dir()
        self._clients = ClientRegistry(directory / "oauth-clients.json")
        self._lexware_base_url = (
            lexware_base_url
            or os.getenv("LEXWARE_API_BASE_URL", DEFAULT_BASE_URL)
        ).rstrip("/")
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
        return self._clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self._clients.put(client_info)

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
                "scopes": params.scopes or [],
                "resource": str(params.resource) if params.resource else None,
            },
        )
        query = urlencode({"txn": txn})
        return f"{str(self.base_url).rstrip('/')}/consent?{query}"

    def get_routes(self, mcp_path: str | None = None) -> list[Route]:
        routes = super().get_routes(mcp_path)
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
        client = self._clients.get(data["client_id"])
        client_name = client.client_name if client and client.client_name else "Deine App"
        return _consent_page(txn=txn, client_name=client_name)

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
        client = self._clients.get(data["client_id"])
        client_name = client.client_name if client and client.client_name else "Deine App"
        if not api_key:
            return _consent_page(
                txn=txn, client_name=client_name, error="Bitte einen API-Key eingeben."
            )

        ok, detail = await self._validate_key(api_key)
        if not ok:
            return _consent_page(txn=txn, client_name=client_name, error=detail)

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
            resource=AnyUrl(data["resource"]) if data.get("resource") else None,
            api_key=data["key"],
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: LexwareAuthCode
    ) -> OAuthToken:
        # The code was already marked used in load_authorization_code.
        return self._issue_tokens(
            client_id=client.client_id,
            api_key=authorization_code.api_key,
            scopes=authorization_code.scopes,
        )

    def _issue_tokens(self, *, client_id: str, api_key: str, scopes: list[str]) -> OAuthToken:
        access = self._seal("access", {"client_id": client_id, "key": api_key, "scopes": scopes})
        refresh = self._seal("refresh", {"client_id": client_id, "key": api_key, "scopes": scopes})
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
        )

    # -- per-request verification -------------------------------------------------

    async def load_access_token(self, token: str) -> AccessToken | None:
        data = self._unseal("access", token, ACCESS_TOKEN_TTL)
        if data is not None:
            return AccessToken(
                token=token,
                client_id=data["client_id"],
                scopes=data["scopes"],
                expires_at=None,
                claims={CLAIM_KEY: data["key"]},
            )
        # Direct mode: the Bearer value IS the Lexware API key (clients that
        # can send custom headers skip OAuth entirely). We accept it here and
        # let the Lexware API reject invalid keys with 401 on first use — the
        # server never needs to know valid keys up front.
        if _looks_like_api_key(token):
            return AccessToken(
                token=token,
                client_id="direct-bearer",
                scopes=[],
                expires_at=None,
                claims={CLAIM_KEY: token},
            )
        return None

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        # Tokens are self-contained; there is nothing server-side to revoke.
        # A leaked token dies with the Lexware key it wraps (revoke it at
        # https://app.lexware.de/addons/public-api) or a rotated LXW_MCP_SECRET.
        return None


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


def _consent_page(*, txn: str, client_name: str, error: str = "") -> HTMLResponse:
    error_html = f'<p class="err">{html.escape(error)}</p>' if error else ""
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
  <input type="hidden" name="txn" value="{html.escape(txn)}">
  <input type="password" name="api_key" placeholder="Lexware API-Key" required autofocus>
  {error_html}
  <p>Der Key wird nicht auf dem Server gespeichert — er wird verschlüsselt in dein
     Zugriffstoken eingebettet und bleibt damit auf deinem Gerät.</p>
  <button type="submit">Verbinden</button>
</form></body></html>"""
    return HTMLResponse(body)


def _error_page(message: str) -> HTMLResponse:
    body = f"""<!doctype html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>Lexware MCP</title><style>{_PAGE_STYLE}</style></head><body>
<div class="card"><h1>Verbindung nicht möglich</h1><p>{html.escape(message)}</p></div>
</body></html>"""
    return HTMLResponse(body, status_code=400)
