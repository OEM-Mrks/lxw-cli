from __future__ import annotations

import httpx
import pytest
import respx

from lexware_cli.core.client import LexwareClient
from lexware_cli.core.errors import LexwareAPIError


@respx.mock
def test_get_sends_bearer_token(client: LexwareClient) -> None:
    route = respx.get("https://api.lexware.io/v1/profile").mock(
        return_value=httpx.Response(200, json={"companyName": "Acme GmbH"})
    )
    data = client.get("/v1/profile")
    assert data == {"companyName": "Acme GmbH"}
    assert route.calls.last.request.headers["Authorization"] == "Bearer test-key"
    assert route.calls.last.request.headers["User-Agent"].startswith("lexware-cli/")


@respx.mock
def test_get_binary_uses_accept_header(client: LexwareClient) -> None:
    route = respx.get("https://api.lexware.io/v1/invoices/123/file").mock(
        return_value=httpx.Response(200, content=b"%PDF-1.4 fake")
    )
    data = client.get_binary("/v1/invoices/123/file")
    assert data == b"%PDF-1.4 fake"
    assert route.calls.last.request.headers["Accept"] == "application/pdf"


@respx.mock
def test_paginate_walks_pages(client: LexwareClient) -> None:
    respx.get("https://api.lexware.io/v1/invoices", params={"page": 0}).mock(
        return_value=httpx.Response(
            200, json={"content": [{"id": "a"}, {"id": "b"}], "last": False}
        )
    )
    respx.get("https://api.lexware.io/v1/invoices", params={"page": 1}).mock(
        return_value=httpx.Response(200, json={"content": [{"id": "c"}], "last": True})
    )
    items = list(client.paginate("/v1/invoices"))
    assert [i["id"] for i in items] == ["a", "b", "c"]


@respx.mock
def test_paginate_clamps_page_size_to_api_bounds(client: LexwareClient) -> None:
    route = respx.get("https://api.lexware.io/v1/articles").mock(
        return_value=httpx.Response(200, json={"content": [], "last": True})
    )
    # Default: always request the API maximum (250) per page.
    list(client.paginate("/v1/articles", max_items=3))
    assert route.calls.last.request.url.params["size"] == "250"
    # Larger requests are capped at the server maximum …
    list(client.paginate("/v1/articles", page_size=1000))
    assert route.calls.last.request.url.params["size"] == "250"
    # … and tiny ones raised to the minimum some endpoints enforce.
    list(client.paginate("/v1/articles", page_size=10))
    assert route.calls.last.request.url.params["size"] == "25"


@respx.mock
def test_paginate_reports_total_in_meta(client: LexwareClient) -> None:
    respx.get("https://api.lexware.io/v1/contacts").mock(
        return_value=httpx.Response(
            200,
            json={"content": [{"id": "a"}], "last": True, "totalElements": 42},
        )
    )
    meta: dict = {}
    list(client.paginate("/v1/contacts", meta=meta))
    assert meta["total"] == 42


@respx.mock
def test_paginate_respects_max_items(client: LexwareClient) -> None:
    respx.get("https://api.lexware.io/v1/invoices").mock(
        return_value=httpx.Response(
            200,
            json={"content": [{"id": "a"}, {"id": "b"}, {"id": "c"}], "last": False},
        )
    )
    items = list(client.paginate("/v1/invoices", max_items=2))
    assert [i["id"] for i in items] == ["a", "b"]


@respx.mock
def test_filters_drop_none_params(client: LexwareClient) -> None:
    route = respx.get("https://api.lexware.io/v1/invoices").mock(
        return_value=httpx.Response(200, json={"content": [], "last": True})
    )
    list(client.paginate("/v1/invoices", params={"voucherStatus": None, "size": 10}))
    sent_url = str(route.calls.last.request.url)
    assert "voucherStatus" not in sent_url


@respx.mock
def test_429_retries_then_succeeds(client: LexwareClient) -> None:
    route = respx.get("https://api.lexware.io/v1/profile").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "0"}, json={"message": "slow down"}),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    data = client.get("/v1/profile")
    assert data == {"ok": True}
    assert route.call_count == 2


def test_wait_for_retry_uses_retry_after_once() -> None:
    """The retry wait honors Retry-After (capped) — no double waiting."""
    from types import SimpleNamespace

    from lexware_cli.core.client import MAX_RETRY_AFTER, _wait_for_retry
    from lexware_cli.core.errors import RateLimitError

    def state(exc):
        return SimpleNamespace(
            attempt_number=1, outcome=SimpleNamespace(exception=lambda: exc)
        )

    assert _wait_for_retry(state(RateLimitError("x", retry_after=2.5))) == 2.5
    # A bogus huge header is capped.
    assert _wait_for_retry(state(RateLimitError("x", retry_after=9999.0))) == MAX_RETRY_AFTER
    # No Retry-After → exponential backoff (>= its 1s minimum).
    assert _wait_for_retry(state(RateLimitError("x"))) >= 1.0


@respx.mock
def test_non_2xx_raises(client: LexwareClient) -> None:
    respx.get("https://api.lexware.io/v1/invoices/xxx").mock(
        return_value=httpx.Response(404, json={"message": "not found"})
    )
    with pytest.raises(LexwareAPIError) as excinfo:
        client.get("/v1/invoices/xxx")
    assert excinfo.value.status_code == 404
