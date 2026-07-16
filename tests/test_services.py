from __future__ import annotations

import httpx
import respx

from lxw_cli.core import services
from lxw_cli.core.client import LexwareClient

_PROFILE = "https://api.lexware.io/v1/profile"
_VOUCHERLIST = "https://api.lexware.io/v1/voucherlist"
_CONTACTS = "https://api.lexware.io/v1/contacts"
_ARTICLES = "https://api.lexware.io/v1/articles"

_UUID = "1a3c79ca-1804-4ab6-a3a5-915cc762b2ce"


@respx.mock
def test_get_profile(client: LexwareClient) -> None:
    respx.get(_PROFILE).mock(return_value=httpx.Response(200, json={"companyName": "Acme"}))
    assert services.get_profile(client) == {"companyName": "Acme"}


@respx.mock
def test_list_invoices_builds_params_and_total(client: LexwareClient) -> None:
    route = respx.get(_VOUCHERLIST).mock(
        return_value=httpx.Response(
            200, json={"content": [{"id": "a"}], "last": True, "totalElements": 7}
        )
    )
    result = services.list_invoices(client, status="open")
    url = str(route.calls.last.request.url)
    assert "voucherType=salesinvoice" in url
    assert "voucherStatus=open" in url
    assert "archived=false" in url  # archived hidden by default
    assert result.items == [{"id": "a"}]
    assert result.total == 7
    assert result.search is False


@respx.mock
def test_list_invoices_include_archived_drops_filter(client: LexwareClient) -> None:
    route = respx.get(_VOUCHERLIST).mock(
        return_value=httpx.Response(200, json={"content": [], "last": True})
    )
    services.list_invoices(client, include_archived=True)
    assert "archived" not in str(route.calls.last.request.url)


@respx.mock
def test_list_contacts_hides_archived_by_default(client: LexwareClient) -> None:
    respx.get(_CONTACTS).mock(
        return_value=httpx.Response(
            200,
            json={
                "content": [
                    {"id": "a", "archived": False},
                    {"id": "b", "archived": True},
                ],
                "last": True,
                "totalElements": 2,
            },
        )
    )
    result = services.list_contacts(client)
    assert [c["id"] for c in result.items] == ["a"]
    assert result.hidden == 1
    assert result.exhausted is True


@respx.mock
def test_list_contacts_include_archived(client: LexwareClient) -> None:
    respx.get(_CONTACTS).mock(
        return_value=httpx.Response(
            200,
            json={
                "content": [
                    {"id": "a", "archived": False},
                    {"id": "b", "archived": True},
                ],
                "last": True,
                "totalElements": 2,
            },
        )
    )
    result = services.list_contacts(client, include_archived=True)
    assert {c["id"] for c in result.items} == {"a", "b"}
    assert result.hidden == 0


@respx.mock
def test_list_articles_search_mode(client: LexwareClient) -> None:
    respx.get(_ARTICLES).mock(
        return_value=httpx.Response(
            200,
            json={
                "content": [
                    {"id": "1", "title": "Schraube M4"},
                    {"id": "2", "title": "Mutter M4"},
                ],
                "last": True,
            },
        )
    )
    result = services.list_articles(client, search="schraube")
    assert [a["id"] for a in result.items] == ["1"]
    assert result.search is True
    assert result.total is None


@respx.mock
def test_list_articles_plain_reports_total(client: LexwareClient) -> None:
    respx.get(_ARTICLES).mock(
        return_value=httpx.Response(
            200, json={"content": [{"id": "1"}], "last": True, "totalElements": 5}
        )
    )
    result = services.list_articles(client)
    assert result.total == 5
    assert result.search is False


@respx.mock
def test_get_invoice_resolves_number(client: LexwareClient) -> None:
    respx.get(_VOUCHERLIST, params={"voucherNumber": "FB1"}).mock(
        return_value=httpx.Response(
            200, json={"content": [{"id": "uuid1", "voucherNumber": "FB1"}], "last": True}
        )
    )
    respx.get("https://api.lexware.io/v1/invoices/uuid1").mock(
        return_value=httpx.Response(200, json={"id": "uuid1"})
    )
    assert services.get_invoice(client, "FB1")["id"] == "uuid1"


@respx.mock
def test_download_invoice_pdf_returns_bytes(client: LexwareClient) -> None:
    respx.get(f"https://api.lexware.io/v1/invoices/{_UUID}/file").mock(
        return_value=httpx.Response(200, content=b"%PDF-1.4")
    )
    assert services.download_invoice_pdf(client, _UUID) == b"%PDF-1.4"


@respx.mock
def test_download_invoice_pdf_falls_back_to_files(client: LexwareClient) -> None:
    respx.get(f"https://api.lexware.io/v1/invoices/{_UUID}/file").mock(
        return_value=httpx.Response(404, json={"message": "not found"})
    )
    respx.get(f"https://api.lexware.io/v1/vouchers/{_UUID}").mock(
        return_value=httpx.Response(200, json={"files": ["file-1"]})
    )
    respx.get("https://api.lexware.io/v1/files/file-1").mock(
        return_value=httpx.Response(200, content=b"%PDF-fallback")
    )
    assert services.download_invoice_pdf(client, _UUID) == b"%PDF-fallback"


@respx.mock
def test_create_contact_posts_body(client: LexwareClient) -> None:
    route = respx.post(_CONTACTS).mock(
        return_value=httpx.Response(200, json={"id": "new"})
    )
    result = services.create_contact(client, {"company": {"name": "X"}})
    assert result == {"id": "new"}
    assert route.called


# -- Order confirmations (Aufträge) -------------------------------------------


@respx.mock
def test_list_order_confirmations_filters_type(client: LexwareClient) -> None:
    route = respx.get(_VOUCHERLIST).mock(
        return_value=httpx.Response(
            200, json={"content": [{"id": "o1"}], "last": True, "totalElements": 1}
        )
    )
    result = services.list_order_confirmations(client, status="open")
    url = str(route.calls.last.request.url)
    assert "voucherType=orderconfirmation" in url
    assert "voucherStatus=open" in url
    assert result.items == [{"id": "o1"}]


@respx.mock
def test_get_order_confirmation_resolves_number(client: LexwareClient) -> None:
    respx.get(_VOUCHERLIST).mock(
        return_value=httpx.Response(
            200, json={"content": [{"id": _UUID}], "last": True}
        )
    )
    respx.get(f"https://api.lexware.io/v1/order-confirmations/{_UUID}").mock(
        return_value=httpx.Response(200, json={"id": _UUID, "voucherNumber": "AB-1"})
    )
    data = services.get_order_confirmation(client, "AB-1")
    assert data["voucherNumber"] == "AB-1"


@respx.mock
def test_create_order_confirmation_posts_body(client: LexwareClient) -> None:
    route = respx.post("https://api.lexware.io/v1/order-confirmations").mock(
        return_value=httpx.Response(201, json={"id": "new-order"})
    )
    result = services.create_order_confirmation(client, {"lineItems": []})
    assert result == {"id": "new-order"}
    assert route.called


def test_article_line_item_copies_article_data() -> None:
    article = {
        "title": "Schraube M8",
        "unitName": "Stück",
        "price": {"netPrice": 0.12, "grossPrice": 0.14, "taxRate": 19},
    }
    item = services.article_line_item(article, 50)
    assert item == {
        "type": "custom",
        "name": "Schraube M8",
        "quantity": 50,
        "unitName": "Stück",
        "unitPrice": {"currency": "EUR", "netAmount": 0.12, "taxRatePercentage": 19},
    }


def test_build_order_confirmation_body_is_minimal_and_net() -> None:
    items = [services.article_line_item({"title": "X", "price": {"netPrice": 1}}, 2)]
    body = services.build_order_confirmation_body("contact-1", items)
    assert body["address"] == {"contactId": "contact-1"}
    assert body["lineItems"] == items
    assert body["taxConditions"] == {"taxType": "net"}
    assert body["totalPrice"] == {"currency": "EUR"}
    assert body["shippingConditions"] == {"shippingType": "none"}
    # API datetime format: 2026-06-10T12:00:00.000+02:00 (ms precision, offset).
    assert "T" in body["voucherDate"]
    assert body["voucherDate"][-6] in "+-"
    assert len(body["voucherDate"].split(".")[1]) == 9  # 'SSS+HH:MM'


def test_article_line_item_price_override() -> None:
    article = {"title": "Schraube M8", "price": {"netPrice": 0.12, "taxRate": 19}}
    item = services.article_line_item(article, 10, net_price=0.10)
    assert item["unitPrice"]["netAmount"] == 0.10
    assert item["unitPrice"]["taxRatePercentage"] == 19


def test_build_order_confirmation_body_with_texts() -> None:
    body = services.build_order_confirmation_body(
        "c-1",
        [],
        introduction="  Vielen Dank für Ihren Auftrag.  ",
        remark="Lieferung in 2 Wochen.",
    )
    assert body["introduction"] == "Vielen Dank für Ihren Auftrag."
    assert body["remark"] == "Lieferung in 2 Wochen."
    # Empty/None texts are omitted entirely.
    bare = services.build_order_confirmation_body("c-1", [], introduction="   ", remark=None)
    assert "introduction" not in bare
    assert "remark" not in bare
