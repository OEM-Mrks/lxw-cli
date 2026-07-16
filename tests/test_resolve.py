from __future__ import annotations

import httpx
import pytest
import respx

from lexware_cli.core.client import LexwareClient
from lexware_cli.core.errors import LexwareError
from lexware_cli.core.services import (
    looks_like_uuid,
    resolve_max_items,
    resolve_voucher_id,
    search_articles,
)

ARTICLES = [
    {"id": "1", "title": "Schraube M4", "articleNumber": "SCH-001"},
    {"id": "2", "title": "Mutter M4", "articleNumber": "MUT-002", "description": "Edelstahl"},
    {"id": "3", "title": "Beratungsstunde", "articleNumber": "DL-100"},
]


@respx.mock
def test_search_articles_matches_title(client: LexwareClient) -> None:
    respx.get("https://api.lexware.io/v1/articles").mock(
        return_value=httpx.Response(200, json={"content": ARTICLES, "last": True})
    )
    results = search_articles(client, "schraube")
    assert [a["id"] for a in results] == ["1"]


@respx.mock
def test_search_articles_matches_number_and_description(client: LexwareClient) -> None:
    respx.get("https://api.lexware.io/v1/articles").mock(
        return_value=httpx.Response(200, json={"content": ARTICLES, "last": True})
    )
    # Partial article number (server-side filter only does exact matches).
    assert [a["id"] for a in search_articles(client, "mut-")] == ["2"]
    # Free text in the description field.
    assert [a["id"] for a in search_articles(client, "edelstahl")] == ["2"]


@respx.mock
def test_search_articles_respects_max_items(client: LexwareClient) -> None:
    respx.get("https://api.lexware.io/v1/articles").mock(
        return_value=httpx.Response(200, json={"content": ARTICLES, "last": True})
    )
    # "M4" matches articles 1 and 2; cap at 1.
    assert len(search_articles(client, "m4", max_items=1)) == 1


def test_resolve_max_items() -> None:
    assert resolve_max_items(25) == 25
    assert resolve_max_items(25, fetch_all=True) is None
    assert resolve_max_items(0) is None
    assert resolve_max_items(-1) is None
    assert resolve_max_items(0, fetch_all=True) is None


def test_looks_like_uuid_recognizes_uuids() -> None:
    assert looks_like_uuid("1a3c79ca-1804-4ab6-a3a5-915cc762b2ce")
    assert looks_like_uuid("1A3C79CA-1804-4AB6-A3A5-915CC762B2CE")
    assert not looks_like_uuid("FB2600682")
    assert not looks_like_uuid("not-a-uuid")
    assert not looks_like_uuid("")


@respx.mock
def test_resolve_returns_uuid_unchanged(client: LexwareClient) -> None:
    uuid = "1a3c79ca-1804-4ab6-a3a5-915cc762b2ce"
    # Should NOT hit the API
    result = resolve_voucher_id(client, uuid, "salesinvoice")
    assert result == uuid


@respx.mock
def test_resolve_looks_up_by_number(client: LexwareClient) -> None:
    route = respx.get("https://api.lexware.io/v1/voucherlist").mock(
        return_value=httpx.Response(
            200,
            json={
                "content": [
                    {"id": "abc-uuid", "voucherNumber": "FB2600682"}
                ],
                "last": True,
            },
        )
    )
    result = resolve_voucher_id(client, "FB2600682", "salesinvoice")
    assert result == "abc-uuid"
    sent_url = str(route.calls.last.request.url)
    assert "voucherType=salesinvoice" in sent_url
    assert "voucherNumber=FB2600682" in sent_url


@respx.mock
def test_resolve_raises_on_no_match(client: LexwareClient) -> None:
    respx.get("https://api.lexware.io/v1/voucherlist").mock(
        return_value=httpx.Response(200, json={"content": [], "last": True})
    )
    with pytest.raises(LexwareError, match="Kein Beleg mit Nummer"):
        resolve_voucher_id(client, "NICHTGEFUNDEN", "salesinvoice")


@respx.mock
def test_resolve_raises_on_ambiguous(client: LexwareClient) -> None:
    respx.get("https://api.lexware.io/v1/voucherlist").mock(
        return_value=httpx.Response(
            200,
            json={
                "content": [
                    {"id": "one"},
                    {"id": "two"},
                ],
                "last": True,
            },
        )
    )
    with pytest.raises(LexwareError, match="Mehrere Belege"):
        resolve_voucher_id(client, "DUPLICATE", "salesinvoice")
