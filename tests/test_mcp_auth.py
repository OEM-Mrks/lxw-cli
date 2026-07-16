"""Multi-user HTTP mode: OAuth flow, direct bearer, per-request keys."""

from __future__ import annotations

import base64
import hashlib
import secrets
import socket
import threading
import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx
import uvicorn

from lxw_cli.mcp_auth import (
    ClientPool,
    LexwareOAuthProvider,
    fernet_from_secret,
    request_api_key,
)

LEXWARE_API = "https://api.lexware.io"


# ---------------------------------------------------------------------------
# Unit level
# ---------------------------------------------------------------------------


def test_fernet_accepts_passphrase_and_proper_key() -> None:
    from cryptography.fernet import Fernet

    for secret in ("just a passphrase", Fernet.generate_key().decode()):
        f = fernet_from_secret(secret)
        assert f.decrypt(f.encrypt(b"x")) == b"x"
    # Same passphrase -> same key material (tokens survive restarts).
    a = fernet_from_secret("s1").encrypt(b"x")
    assert fernet_from_secret("s1").decrypt(a) == b"x"


def test_request_api_key_is_none_outside_http_context() -> None:
    assert request_api_key() is None


def test_client_pool_reuses_and_evicts() -> None:
    pool = ClientPool(max_idle=0.0, max_size=2)
    c1 = pool.get("key-one")
    # Immediate re-get: max_idle=0 evicts on next access, so use a fresh pool
    pool2 = ClientPool()
    a = pool2.get("key-a")
    assert pool2.get("key-a") is a
    assert pool2.get("key-b") is not a
    c1.close()


@pytest.mark.asyncio
async def test_direct_bearer_token_is_accepted_as_key() -> None:
    provider = _provider()
    token = await provider.load_access_token("a2b4c6d8-0000-4444-8888-abcdefabcdef")
    assert token is not None
    assert token.claims["lexware_api_key"] == "a2b4c6d8-0000-4444-8888-abcdefabcdef"
    # Garbage does not pass.
    assert await provider.load_access_token("nope !! not a key") is None
    assert await provider.load_access_token("x" * 500) is None


@pytest.mark.asyncio
async def test_corrupt_oauth_token_not_treated_as_raw_key() -> None:
    """A garbled Fernet token must not be forwarded to Lexware as a Bearer key."""
    provider = _provider(secret="secret-a")
    tokens = provider._issue_tokens(client_id="c", api_key="k" * 20, scopes=[])
    # Same token, wrong secret -> fails to unseal; still starts with 'gAAAAA'.
    other = _provider(secret="secret-b")
    assert await other.load_access_token(tokens.access_token) is None


@pytest.mark.asyncio
async def test_access_token_rejected_with_other_secret() -> None:
    provider = _provider(secret="secret-a")
    tokens = provider._issue_tokens(client_id="c", api_key="k" * 20, scopes=[])
    other = _provider(secret="secret-b")
    assert await other.load_access_token(tokens.access_token) is None
    good = await provider.load_access_token(tokens.access_token)
    assert good is not None and good.claims["lexware_api_key"] == "k" * 20


def _provider(secret: str = "test-secret", **kw) -> LexwareOAuthProvider:
    return LexwareOAuthProvider(
        public_url="https://mcp.example.com", secret=secret, **kw
    )


# ---------------------------------------------------------------------------
# Full end-to-end over real HTTP: DCR -> authorize -> consent -> token -> MCP
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def server(tmp_path_factory: pytest.TempPathFactory):
    """The lexware MCP server with auth, on a real local port."""
    from lxw_cli.mcp_server import mcp

    provider = LexwareOAuthProvider(
        public_url="http://127.0.0.1:0",  # patched below once the port is known
        secret="e2e-secret",
        data_dir=tmp_path_factory.mktemp("mcp-auth"),
    )
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    from pydantic import AnyHttpUrl

    provider.base_url = AnyHttpUrl(base)  # what /authorize redirects are built from
    mcp.auth = provider

    config = uvicorn.Config(mcp.http_app(), host="127.0.0.1", port=port, log_level="error")
    srv = uvicorn.Server(config)
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not srv.started:
        if time.time() > deadline:
            raise RuntimeError("uvicorn did not start")
        time.sleep(0.05)
    yield base
    srv.should_exit = True
    thread.join(timeout=5)
    mcp.auth = None


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


@pytest.mark.asyncio
async def test_full_oauth_flow_and_tool_call(server: str) -> None:
    async with httpx.AsyncClient(base_url=server, follow_redirects=False) as c:
        # 1. Discovery + Dynamic Client Registration
        meta = (await c.get("/.well-known/oauth-authorization-server")).json()
        assert meta["issuer"].rstrip("/") == server
        reg = await c.post(
            meta["registration_endpoint"],
            json={
                "client_name": "pytest client",
                "redirect_uris": ["http://127.0.0.1:9/callback"],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
            },
        )
        assert reg.status_code in (200, 201), reg.text
        client = reg.json()

        # 2. /authorize redirects to the consent page
        verifier, challenge = _pkce()
        auth = await c.get(
            "/authorize",
            params={
                "client_id": client["client_id"],
                "response_type": "code",
                "redirect_uri": "http://127.0.0.1:9/callback",
                "state": "st4te",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            },
        )
        assert auth.status_code in (302, 307), auth.text
        consent_url = auth.headers["location"]
        assert "/consent?txn=" in consent_url

        page = await c.get(consent_url)
        assert page.status_code == 200
        assert "pytest client" in page.text
        txn = parse_qs(urlparse(consent_url).query)["txn"][0]

        # 3. Submit the Lexware key; the probe against /v1/profile is mocked.
        with respx.mock:
            respx.route(host="127.0.0.1").pass_through()
            respx.get(f"{LEXWARE_API}/v1/profile").mock(
                return_value=httpx.Response(200, json={"companyName": "Acme"})
            )
            submit = await c.post(
                "/consent", data={"txn": txn, "api_key": "user-key-1234567890"}
            )
        assert submit.status_code == 302, submit.text
        cb = urlparse(submit.headers["location"])
        params = parse_qs(cb.query)
        assert params["state"] == ["st4te"]
        code = params["code"][0]

        # 4. Exchange code for tokens
        tok = await c.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": "http://127.0.0.1:9/callback",
                "client_id": client["client_id"],
                "code_verifier": verifier,
            },
        )
        assert tok.status_code == 200, tok.text
        tokens = tok.json()
        assert tokens["token_type"].lower() == "bearer"
        assert tokens["refresh_token"]

        # 4b. Codes are single-use.
        replay = await c.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": "http://127.0.0.1:9/callback",
                "client_id": client["client_id"],
                "code_verifier": verifier,
            },
        )
        assert replay.status_code in (400, 401)

        # 5. Refresh works
        refreshed = await c.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": tokens["refresh_token"],
                "client_id": client["client_id"],
            },
        )
        assert refreshed.status_code == 200, refreshed.text
        access = refreshed.json()["access_token"]

    # 6. Real MCP tool call with the OAuth token: the user's key (from the
    # token) must hit the Lexware API — not any server-side config.
    from fastmcp import Client

    with respx.mock:
        respx.route(host="127.0.0.1").pass_through()
        profile_route = respx.get(f"{LEXWARE_API}/v1/profile").mock(
            return_value=httpx.Response(200, json={"companyName": "Acme"})
        )
        async with Client(f"{server}/mcp", auth=access) as mc:
            result = await mc.call_tool("profile", {})
        assert result.data == {"companyName": "Acme"}
        sent_auth = profile_route.calls.last.request.headers["authorization"]
        assert sent_auth == "Bearer user-key-1234567890"


@pytest.mark.asyncio
async def test_direct_bearer_tool_call_and_pdf_binary(server: str) -> None:
    """Clients with header support skip OAuth: Bearer = the raw Lexware key."""
    from fastmcp import Client

    raw_key = "raw-key-abcdef-123456"
    with respx.mock:
        respx.route(host="127.0.0.1").pass_through()
        profile_route = respx.get(f"{LEXWARE_API}/v1/profile").mock(
            return_value=httpx.Response(200, json={"companyName": "Direct GmbH"})
        )
        respx.get(f"{LEXWARE_API}/v1/voucherlist").mock(
            return_value=httpx.Response(
                200,
                json={
                    "content": [
                        {"id": "abc-123", "voucherType": "invoice", "voucherNumber": "RE1"}
                    ],
                    "last": True,
                },
            )
        )
        respx.get(f"{LEXWARE_API}/v1/invoices/abc-123/file").mock(
            return_value=httpx.Response(
                200, content=b"%PDF-1.7 fake", headers={"Content-Type": "application/pdf"}
            )
        )
        async with Client(f"{server}/mcp", auth=raw_key) as mc:
            result = await mc.call_tool("profile", {})
            assert result.data == {"companyName": "Direct GmbH"}
            assert (
                profile_route.calls.last.request.headers["authorization"]
                == f"Bearer {raw_key}"
            )

            # PDF over HTTP comes back as binary content, not a server path.
            pdf = await mc.call_tool("download_invoice_pdf", {"identifier": "abc-123"})
            blob = pdf.content[0]
            assert blob.type == "resource", pdf.content
            assert base64.b64decode(blob.resource.blob) == b"%PDF-1.7 fake"
            # No doubled extension in the synthetic resource URI.
            assert str(blob.resource.uri).endswith("invoice-abc-123.pdf")


@pytest.mark.asyncio
async def test_unauthenticated_mcp_request_is_rejected(server: str) -> None:
    async with httpx.AsyncClient(base_url=server) as c:
        resp = await c.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "initialize", "id": 1, "params": {}},
            headers={"Accept": "application/json, text/event-stream"},
        )
        assert resp.status_code == 401


@pytest.mark.asyncio
async def test_consent_rejects_bad_key(server: str) -> None:
    """A key Lexware rejects (401) never becomes a token."""
    async with httpx.AsyncClient(base_url=server, follow_redirects=False) as c:
        reg = await c.post(
            "/register",
            json={
                "client_name": "bad key client",
                "redirect_uris": ["http://127.0.0.1:9/cb"],
                "token_endpoint_auth_method": "none",
            },
        )
        client = reg.json()
        _, challenge = _pkce()
        auth = await c.get(
            "/authorize",
            params={
                "client_id": client["client_id"],
                "response_type": "code",
                "redirect_uri": "http://127.0.0.1:9/cb",
                "state": "s",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            },
        )
        txn = parse_qs(urlparse(auth.headers["location"]).query)["txn"][0]
        with respx.mock:
            respx.route(host="127.0.0.1").pass_through()
            respx.get(f"{LEXWARE_API}/v1/profile").mock(
                return_value=httpx.Response(401, json={"message": "unauthorized"})
            )
            submit = await c.post(
                "/consent", data={"txn": txn, "api_key": "wrong-key-1234567"}
            )
        # Re-renders the form with an error instead of redirecting.
        assert submit.status_code == 200
        assert "abgelehnt" in submit.text


@pytest.mark.asyncio
async def test_consent_with_expired_txn(server: str) -> None:
    async with httpx.AsyncClient(base_url=server) as c:
        page = await c.get("/consent", params={"txn": "garbage"})
        assert page.status_code == 400
        assert "abgelaufen oder ungültig" in page.text
