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

    # Port zuerst festlegen, dann den Provider damit bauen. Ein nachträglich
    # gesetztes `base_url` reicht seit fastmcp 4.0 nicht mehr — die
    # OAuth-Metadaten (issuer) werden daraus nicht mehr abgeleitet. Der
    # Produktivpfad kennt die URL ohnehin schon im Konstruktor (run_http),
    # also bildet der Test das jetzt genauso ab.
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    base = f"http://127.0.0.1:{port}"

    provider = LexwareOAuthProvider(
        public_url=base,
        secret="e2e-secret",
        data_dir=tmp_path_factory.mktemp("mcp-auth"),
    )
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
                # claude.ai always sends scope + resource (RFC 8707). A wrong
                # resource type in the code model 500s /token — regression guard.
                "scope": "mcp",
                "resource": server + "/mcp",
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

    # 7. Revoking the token (RFC 7009) locks it out of the MCP endpoint.
    async with httpx.AsyncClient(base_url=server) as c:
        rev = await c.post(
            "/revoke",
            # The SDK's RevocationRequest requires the field even for public
            # clients; an empty value is accepted.
            data={"token": access, "client_id": client["client_id"], "client_secret": ""},
        )
        assert rev.status_code == 200, rev.text
        denied = await c.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "initialize", "id": 1, "params": {}},
            headers={
                "Accept": "application/json, text/event-stream",
                "Authorization": f"Bearer {access}",
            },
        )
        assert denied.status_code == 401


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


async def _oauth_access_token(c: httpx.AsyncClient, server: str, *, resource: str, key: str):
    """Run DCR → authorize → consent → token; return (consent html, access token)."""
    reg = await c.post(
        "/register",
        json={
            "client_name": "multi client",
            "redirect_uris": ["http://127.0.0.1:9/callback"],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        },
    )
    client = reg.json()
    verifier, challenge = _pkce()
    auth = await c.get(
        "/authorize",
        params={
            "client_id": client["client_id"],
            "response_type": "code",
            "redirect_uri": "http://127.0.0.1:9/callback",
            "state": "s",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "scope": "mcp",
            "resource": resource,
        },
    )
    consent_url = auth.headers["location"]
    page = (await c.get(consent_url)).text
    txn = parse_qs(urlparse(consent_url).query)["txn"][0]
    with respx.mock:
        respx.route(host="127.0.0.1").pass_through()
        respx.get(f"{LEXWARE_API}/v1/profile").mock(
            return_value=httpx.Response(200, json={"companyName": "X"})
        )
        submit = await c.post("/consent", data={"txn": txn, "api_key": key})
    code = parse_qs(urlparse(submit.headers["location"]).query)["code"][0]
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
    tokens = tok.json()
    # Das Label muss auch einen Refresh überleben.
    refreshed = await c.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
            "client_id": client["client_id"],
        },
    )
    return page, refreshed.json()["access_token"]


@pytest.mark.asyncio
async def test_two_installations_as_separate_connectors(server: str) -> None:
    """oemedia + demo: zwei Verbindungen, je eigener Key, sauber getrennt."""
    from fastmcp import Client

    async with httpx.AsyncClient(base_url=server, follow_redirects=False) as c:
        page_demo, token_demo = await _oauth_access_token(
            c, server, resource=f"{server}/mcp?installation=demo", key="demo-key-1234567890"
        )
        _, token_oem = await _oauth_access_token(
            c, server, resource=f"{server}/mcp?installation=oemedia", key="oem-key-1234567890"
        )
    # Die Consent-Seite sagt, für welche Installation der Key gedacht ist.
    assert "<strong>demo</strong>" in page_demo

    with respx.mock:
        respx.route(host="127.0.0.1").pass_through()
        route = respx.get(f"{LEXWARE_API}/v1/profile").mock(
            return_value=httpx.Response(200, json={"companyName": "X"})
        )
        for label, token, key in (
            ("demo", token_demo, "demo-key-1234567890"),
            ("oemedia", token_oem, "oem-key-1234567890"),
        ):
            async with Client(f"{server}/mcp?installation={label}", auth=token) as mc:
                version = await mc.call_tool("version", {})
                await mc.call_tool("profile", {})
            assert version.data["installation"] == label
            assert route.calls.last.request.headers["authorization"] == f"Bearer {key}"


@pytest.mark.asyncio
async def test_direct_bearer_installation_label_from_url(server: str) -> None:
    from fastmcp import Client

    async with Client(f"{server}/mcp?installation=demo", auth="raw-key-abcdef-123456") as mc:
        version = await mc.call_tool("version", {})
    assert version.data["installation"] == "demo"
    async with Client(f"{server}/mcp", auth="raw-key-abcdef-123456") as mc:
        version = await mc.call_tool("version", {})
    assert "installation" not in version.data


def test_installation_from_url() -> None:
    from lxw_cli.mcp_auth import installation_from_url

    assert installation_from_url("https://x/mcp?installation=Demo") == "demo"
    assert installation_from_url("https://x/mcp?profile=oemedia") == "oemedia"
    assert installation_from_url("https://x/mcp?installation=../bad") is None
    assert installation_from_url("https://x/mcp") is None
    assert installation_from_url(None) is None


# ---------------------------------------------------------------------------
# Redirect-URI allow-list (DCR phishing guard)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "uri",
    [
        "http://127.0.0.1:9/cb",
        "http://localhost:33418/callback",
        "http://[::1]:8080/cb",
        "https://claude.ai/api/mcp/auth_callback",
        "https://claude.com/api/mcp/auth_callback",
        "https://chatgpt.com/connector_platform_oauth_redirect",
        "cursor://anysphere.cursor-retrieval/oauth/callback",
    ],
)
def test_redirect_uri_allowed(uri: str) -> None:
    from lxw_cli.mcp_auth import redirect_uri_allowed

    assert redirect_uri_allowed(uri)


@pytest.mark.parametrize(
    "uri",
    [
        "https://evil.example/cb",
        "http://claude.ai/api/mcp/auth_callback",  # not https
        "https://claude.ai.evil.example/cb",
        "https://claude.ai@evil.example/cb",
        "javascript:alert(1)",
        "data:text/html,x",
    ],
)
def test_redirect_uri_rejected(uri: str) -> None:
    from lxw_cli.mcp_auth import redirect_uri_allowed

    assert not redirect_uri_allowed(uri)


def test_redirect_allow_env_extends_list(monkeypatch: pytest.MonkeyPatch) -> None:
    from lxw_cli.mcp_auth import redirect_uri_allowed

    monkeypatch.setenv("LXW_MCP_REDIRECT_ALLOW", "app.example.com, myapp:")
    assert redirect_uri_allowed("https://app.example.com/cb")
    assert redirect_uri_allowed("myapp://oauth")
    assert not redirect_uri_allowed("http://app.example.com/cb")


@pytest.mark.asyncio
async def test_registration_with_foreign_redirect_is_rejected(server: str) -> None:
    async with httpx.AsyncClient(base_url=server, follow_redirects=False) as c:
        reg = await c.post(
            "/register",
            json={
                "client_name": "Claude",
                "redirect_uris": ["https://evil.example/cb"],
                "token_endpoint_auth_method": "none",
            },
        )
        assert reg.status_code == 400, reg.text
        assert reg.json()["error"] == "invalid_redirect_uri"


@pytest.mark.asyncio
async def test_stored_client_with_foreign_redirect_is_ignored(tmp_path) -> None:
    """Registrations from before the allow-list must not keep working."""
    from mcp.shared.auth import OAuthClientInformationFull

    provider = _provider(data_dir=tmp_path)
    info = OAuthClientInformationFull(
        client_id="legacy",
        redirect_uris=["https://evil.example/cb"],
        token_endpoint_auth_method="none",
    )
    provider._clients.put(info)  # bypasses register_client on purpose
    assert await provider.get_client("legacy") is None


@pytest.mark.asyncio
async def test_consent_page_shows_redirect_target(server: str) -> None:
    async with httpx.AsyncClient(base_url=server, follow_redirects=False) as c:
        reg = await c.post(
            "/register",
            json={
                "client_name": "target client",
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
        page = await c.get(auth.headers["location"])
        assert page.status_code == 200
        assert "<strong>127.0.0.1</strong>" in page.text
        assert page.headers["x-frame-options"] == "DENY"
        assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
        assert page.headers["cache-control"] == "no-store"


# ---------------------------------------------------------------------------
# Refresh rotation, revocation, secret strength, registration limits
# ---------------------------------------------------------------------------


def _oauth_client(client_id: str = "c1"):
    from mcp.shared.auth import OAuthClientInformationFull

    return OAuthClientInformationFull(
        client_id=client_id,
        redirect_uris=["http://127.0.0.1:9/cb"],
        token_endpoint_auth_method="none",
    )


async def _grant(provider: LexwareOAuthProvider):
    return provider._issue_tokens(
        client_id="c1", api_key="k" * 20, scopes=["mcp"], family="fam-1"
    )


@pytest.mark.asyncio
async def test_refresh_token_is_single_use_and_reuse_revokes_grant(tmp_path) -> None:
    provider = _provider(data_dir=tmp_path)
    client = _oauth_client()
    first = await _grant(provider)

    rt = await provider.load_refresh_token(client, first.refresh_token)
    assert rt is not None
    second = await provider.exchange_refresh_token(client, rt, [])
    assert await provider.load_access_token(second.access_token) is not None

    # Replaying the spent refresh token fails …
    assert await provider.load_refresh_token(client, first.refresh_token) is None
    # … and switches off the whole grant, including the freshly issued tokens.
    assert await provider.load_access_token(second.access_token) is None
    assert await provider.load_refresh_token(client, second.refresh_token) is None


@pytest.mark.asyncio
async def test_revoke_token_switches_off_grant_and_persists(tmp_path) -> None:
    provider = _provider(data_dir=tmp_path)
    tokens = await _grant(provider)
    access = await provider.load_access_token(tokens.access_token)
    assert access is not None

    await provider.revoke_token(access)
    assert await provider.load_access_token(tokens.access_token) is None
    assert await provider.load_refresh_token(_oauth_client(), tokens.refresh_token) is None

    # Survives a restart (same data dir, same secret).
    restarted = _provider(data_dir=tmp_path)
    assert await restarted.load_access_token(tokens.access_token) is None


@pytest.mark.asyncio
async def test_revoke_ignores_direct_bearer_key(tmp_path) -> None:
    provider = _provider(data_dir=tmp_path)
    raw = "a2b4c6d8-0000-4444-8888-abcdefabcdef"
    token = await provider.load_access_token(raw)
    assert token is not None
    await provider.revoke_token(token)
    assert await provider.load_access_token(raw) is not None


def test_secret_problem() -> None:
    from cryptography.fernet import Fernet

    from lxw_cli.mcp_auth import secret_problem

    assert secret_problem(Fernet.generate_key().decode()) is None
    assert secret_problem("x" * 32) is None
    assert "zu kurz" in (secret_problem("ein-langes-zufaelliges-secret") or "")
    assert "LXW_MCP_SECRET_PREVIOUS ist zu kurz" in (
        secret_problem("kurz", "LXW_MCP_SECRET_PREVIOUS") or ""
    )


# ---------------------------------------------------------------------------
# Secret rotation: LXW_MCP_SECRET_PREVIOUS
# ---------------------------------------------------------------------------

OLD_SECRET = "o" * 40
NEW_SECRET = "n" * 40


@pytest.mark.asyncio
async def test_rotation_keeps_old_tokens_and_seals_new_ones_with_new_secret(
    tmp_path,
) -> None:
    old = await _grant(_provider(secret=OLD_SECRET, data_dir=tmp_path))
    rotated = _provider(
        secret=NEW_SECRET, previous_secrets=[OLD_SECRET], data_dir=tmp_path
    )
    # Tokens from before the rotation still work …
    access = await rotated.load_access_token(old.access_token)
    assert access is not None and access.claims["lexware_api_key"] == "k" * 20
    # … but new ones are sealed with the new secret only.
    fresh = await _grant(rotated)
    assert await _provider(secret=NEW_SECRET).load_access_token(fresh.access_token)
    assert (
        await _provider(secret=OLD_SECRET).load_access_token(fresh.access_token)
        is None
    )


@pytest.mark.asyncio
async def test_refresh_moves_a_grant_to_the_new_secret(tmp_path) -> None:
    client = _oauth_client()
    old = await _grant(_provider(secret=OLD_SECRET, data_dir=tmp_path))
    rotated = _provider(
        secret=NEW_SECRET, previous_secrets=[OLD_SECRET], data_dir=tmp_path
    )
    rt = await rotated.load_refresh_token(client, old.refresh_token)
    assert rt is not None
    renewed = await rotated.exchange_refresh_token(client, rt, [])
    # Once the previous secret is dropped, the renewed grant keeps working.
    after = _provider(secret=NEW_SECRET, data_dir=tmp_path)
    assert await after.load_access_token(renewed.access_token) is not None
    assert await after.load_refresh_token(client, renewed.refresh_token) is not None


@pytest.mark.asyncio
async def test_dropping_previous_secret_logs_out_unrenewed_tokens(tmp_path) -> None:
    old = await _grant(_provider(secret=OLD_SECRET, data_dir=tmp_path))
    after = _provider(secret=NEW_SECRET, data_dir=tmp_path)
    assert await after.load_access_token(old.access_token) is None
    assert await after.load_refresh_token(_oauth_client(), old.refresh_token) is None


def test_old_secret_tokens_still_expire() -> None:
    import json

    from lxw_cli.mcp_auth import REFRESH_TOKEN_TTL, fernet_from_secret

    body = json.dumps({"t": "refresh", "client_id": "c1", "key": "k" * 20})
    stale = fernet_from_secret(OLD_SECRET).encrypt_at_time(
        body.encode(), int(time.time()) - REFRESH_TOKEN_TTL - 60
    )
    rotated = _provider(secret=NEW_SECRET, previous_secrets=[OLD_SECRET])
    assert rotated._unseal("refresh", stale.decode(), REFRESH_TOKEN_TTL) is None


def test_token_sealer_ignores_empty_and_duplicate_previous() -> None:
    from lxw_cli.mcp_auth import token_sealer

    sealer = token_sealer(NEW_SECRET, ["", NEW_SECRET, OLD_SECRET, OLD_SECRET])
    assert len(sealer._fernets) == 2
    assert len(token_sealer(NEW_SECRET)._fernets) == 1


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"LXW_MCP_SECRET_PREVIOUS": OLD_SECRET}, "LXW_MCP_SECRET aber nicht"),
        (
            {"LXW_MCP_SECRET": NEW_SECRET, "LXW_MCP_SECRET_PREVIOUS": "kurz"},
            "LXW_MCP_SECRET_PREVIOUS ist zu kurz",
        ),
    ],
)
def test_run_http_rejects_bad_previous_secret(monkeypatch, capsys, env, message):
    from lxw_cli import mcp_server

    monkeypatch.delenv("LXW_MCP_SECRET", raising=False)
    monkeypatch.delenv("LXW_MCP_SECRET_PREVIOUS", raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(mcp_server.mcp, "run", lambda **kw: pytest.fail("ran"))
    with pytest.raises(SystemExit) as exc:
        mcp_server.run_http()
    assert exc.value.code == 2
    assert message in capsys.readouterr().err


def test_run_http_passes_previous_secret(monkeypatch, capsys) -> None:
    from lxw_cli import mcp_server

    monkeypatch.setenv("LXW_MCP_SECRET", NEW_SECRET)
    monkeypatch.setenv("LXW_MCP_SECRET_PREVIOUS", OLD_SECRET)
    monkeypatch.setenv("LXW_MCP_STATUS_REFRESH", "0")
    monkeypatch.setattr(mcp_server.mcp, "run", lambda **kw: None)
    monkeypatch.setattr(mcp_server.mcp, "auth", mcp_server.mcp.auth)
    mcp_server.run_http()
    assert len(mcp_server.mcp.auth._fernet._fernets) == 2
    assert "werden noch angenommen" in capsys.readouterr().err


def test_rate_limiter() -> None:
    from lxw_cli.mcp_auth import RateLimiter

    limiter = RateLimiter(limit=2, window=60)
    assert limiter.allow("1.2.3.4")
    assert limiter.allow("1.2.3.4")
    assert not limiter.allow("1.2.3.4")
    assert limiter.allow("5.6.7.8")


def test_client_registry_caps_and_drops_oldest(tmp_path) -> None:
    from mcp.shared.auth import OAuthClientInformationFull

    from lxw_cli.mcp_auth import ClientRegistry

    reg = ClientRegistry(tmp_path / "clients.json")
    for i in range(4):
        reg.put(
            OAuthClientInformationFull(
                client_id=f"c{i}",
                client_id_issued_at=1000 + i,
                redirect_uris=["http://127.0.0.1:9/cb"],
            ),
            max_clients=3,
        )
    assert reg.get("c0") is None
    assert all(reg.get(f"c{i}") is not None for i in (1, 2, 3))


@pytest.mark.asyncio
async def test_registration_is_rate_limited(server: str) -> None:
    from lxw_cli.mcp_auth import REGISTRATION_RATE_LIMIT, RateLimiter
    from lxw_cli.mcp_server import mcp

    statuses = []
    try:
        async with httpx.AsyncClient(base_url=server) as c:
            for _ in range(REGISTRATION_RATE_LIMIT + 1):
                reg = await c.post(
                    "/register",
                    json={
                        "client_name": "flood",
                        "redirect_uris": ["http://127.0.0.1:9/cb"],
                        "token_endpoint_auth_method": "none",
                    },
                )
                statuses.append(reg.status_code)
    finally:
        # The module-scoped server is shared: don't starve later tests.
        limiter = mcp.auth._register_limiter
        fresh = RateLimiter(REGISTRATION_RATE_LIMIT, 600)
        limiter.__dict__.update(fresh.__dict__)
    assert statuses[-1] == 429


@pytest.mark.asyncio
async def test_revocation_endpoint_is_advertised(server: str) -> None:
    async with httpx.AsyncClient(base_url=server) as c:
        meta = (await c.get("/.well-known/oauth-authorization-server")).json()
        assert meta.get("revocation_endpoint")


# ---------------------------------------------------------------------------
# Upload links: the original receipt reaches Lexware without the model
# ---------------------------------------------------------------------------

_VID = "2cdcdd47-e557-4ff4-9645-931bfd200292"
_PDF = b"%PDF-1.7\n" + bytes(range(256)) * 40 + b"\n%%EOF\n"  # binary-safe check


def _thumbnail_jpeg(width: int = 85, height: int = 120) -> bytes:
    """Smallest JPEG header image_size() can read (SOF0 with the frame size)."""
    sof = b"\xff\xc0\x00\x11\x08" + height.to_bytes(2, "big") + width.to_bytes(2, "big")
    return b"\xff\xd8" + sof + b"\x03" + b"\x00" * 9 + b"\xff\xd9"


async def test_voucher_upload_link_end_to_end(server: str) -> None:
    from fastmcp import Client

    raw_key = "raw-key-upload-123456"
    with respx.mock:
        respx.route(host="127.0.0.1").pass_through()
        respx.post(f"{LEXWARE_API}/v1/vouchers").mock(
            return_value=httpx.Response(201, json={"id": _VID, "version": 1})
        )
        files_route = respx.post(f"{LEXWARE_API}/v1/vouchers/{_VID}/files").mock(
            return_value=httpx.Response(202, json={"id": "file-1"})
        )
        async with Client(f"{server}/mcp", auth=raw_key) as mc:
            result = await mc.call_tool(
                "create_voucher_draft",
                {"body": {"type": "purchaseinvoice", "voucherNumber": "100123"}},
            )
        info = result.data["fileUpload"]
        assert result.data["id"] == _VID
        assert info["attached"] is False
        url = info["uploadUrl"]
        assert url.startswith(f"{server}/upload/")
        assert raw_key not in url  # the key travels sealed only
        assert "curl" in info["howTo"]

        async with httpx.AsyncClient() as http:
            page = await http.get(url)
            assert page.status_code == 200
            assert 'type="file"' in page.text and "100123" in page.text
            assert page.headers["x-frame-options"] == "DENY"

            # What a code sandbox does: curl -F file=@original.pdf <url>
            resp = await http.post(url, files={"file": ("Hotel Rechnung", _PDF, "application/pdf")})
        assert resp.status_code == 200, resp.text
        assert resp.json()["ok"] is True
        sent = files_route.calls.last.request
        assert sent.headers["authorization"] == f"Bearer {raw_key}"
        assert _PDF in sent.content  # byte-identical original
        assert b'filename="Hotel Rechnung.pdf"' in sent.content


async def test_upload_link_rejects_bad_tokens_and_types(server: str) -> None:
    from fastmcp import Client

    with respx.mock:
        respx.route(host="127.0.0.1").pass_through()
        respx.get(f"{LEXWARE_API}/v1/vouchers/{_VID}").mock(
            return_value=httpx.Response(200, json={"id": _VID, "voucherNumber": "R-7"})
        )
        files_route = respx.post(f"{LEXWARE_API}/v1/vouchers/{_VID}/files").mock(
            return_value=httpx.Response(202, json={"id": "f"})
        )
        async with Client(f"{server}/mcp", auth="raw-key-upload-777777") as mc:
            link = (await mc.call_tool("create_upload_link", {"identifier": _VID})).data
        assert link["voucherNumber"] == "R-7"

        async with httpx.AsyncClient() as http:
            bogus = f"{server}/upload/gAAAAAinvalid"
            assert (await http.get(bogus)).status_code == 410
            assert (await http.post(bogus, files={"file": ("a.pdf", _PDF)})).status_code == 410

            wrong = await http.post(link["uploadUrl"], files={"file": ("x.exe", b"MZ\x90\x00")})
            assert wrong.status_code == 400
            assert wrong.json()["files"][0]["ok"] is False

            # Browser form post gets an HTML answer.
            html_resp = await http.post(
                link["uploadUrl"],
                files={"file": ("scan.pdf", _PDF, "application/pdf")},
                headers={"Accept": "text/html"},
            )
            assert html_resp.status_code == 200
            assert "angehängt" in html_resp.text
        assert files_route.call_count == 1


async def test_upload_link_token_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    import lxw_cli.mcp_upload as up

    provider = _provider()
    link = provider.upload_links.create(api_key="k" * 20, voucher_id=_VID)
    token = link["uploadUrl"].rsplit("/", 1)[1]
    assert provider.upload_links._unseal(token)["v"] == _VID
    # An access token sealed with the same secret is not an upload token.
    monkeypatch.setattr(up, "UPLOAD_LINK_TTL", -1)
    assert provider.upload_links._unseal(token) is None


async def test_thumbnail_base64_books_voucher_and_offers_link(server: str) -> None:
    """The 85×120 px stand-in is refused, the booking still happens + link."""
    from fastmcp import Client

    with respx.mock:
        respx.route(host="127.0.0.1").pass_through()
        respx.post(f"{LEXWARE_API}/v1/vouchers").mock(
            return_value=httpx.Response(201, json={"id": _VID, "version": 1})
        )
        files_route = respx.post(f"{LEXWARE_API}/v1/vouchers/{_VID}/files").mock(
            return_value=httpx.Response(202, json={"id": "f"})
        )
        async with Client(f"{server}/mcp", auth="raw-key-thumb-123456") as mc:
            result = await mc.call_tool(
                "create_voucher_draft",
                {
                    "body": {"type": "purchaseinvoice"},
                    "file_base64": base64.b64encode(_thumbnail_jpeg()).decode(),
                    "filename": "beleg.jpg",
                },
            )
    info = result.data["fileUpload"]
    assert result.data["id"] == _VID
    assert "85×120" in info["error"]
    assert info["uploadUrl"].startswith(f"{server}/upload/")
    assert not files_route.called
