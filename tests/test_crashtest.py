"""Regressionen aus dem Crashtest aller MCP-Tools gegen die Demo (10.11.2026).

Jeder Test steht für einen Fehlgriff, den ein Sprachmodell tatsächlich gemacht
hat bzw. einen Server-Fehler, der dabei aufgefallen ist.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from lxw_cli.core import services
from lxw_cli.core.client import LexwareClient
from lxw_cli.core.errors import LexwareAPIError, LexwareError
from lxw_cli.core.normalize import (
    country_code,
    normalize_contact,
    normalize_voucher_status,
    to_decimal,
)
from lxw_cli.core.prepare import INVOICE, QUOTATION, prepare_document, prepare_voucher

API = "https://api.lexware.io/v1"
U1 = "11111111-1111-4111-8111-111111111111"
U2 = "22222222-2222-4222-8222-222222222222"
U3 = "33333333-3333-4333-8333-333333333333"


def page(*rows: dict) -> httpx.Response:
    return httpx.Response(200, json={"content": list(rows), "last": True})


# -- Eingaben normalisieren ---------------------------------------------------


def test_status_words() -> None:
    assert normalize_voucher_status("offen, Bezahlt") == "open,paid,paidoff"
    assert normalize_voucher_status("OPEN;überfällig") == "open,overdue"
    assert normalize_voucher_status("alle") is None
    with pytest.raises(LexwareError, match="Unbekannter Status 'kaputt'"):
        normalize_voucher_status("kaputt")


def test_numbers_and_countries() -> None:
    assert str(to_decimal("49,90 €", field="x")) == "49.90"
    assert str(to_decimal("1.234,56", field="x")) == "1234.56"
    assert str(to_decimal("19%", field="x")) == "19"
    assert country_code("Deutschland") == "DE"
    assert country_code("de") == "DE"
    assert country_code("Österreich") == "AT"


def test_contact_body_repaired() -> None:
    body = normalize_contact({
        "roles": ["Kunde", "Lieferant"],
        "name": "CRASHTEST GmbH",
        "email": "info@x.de",
        "phone": "0221 123",
        "addresses": {"billing": {"street": "Weg 1", "countryCode": "Deutschland"}},
    })
    assert body["roles"] == {"customer": {}, "vendor": {}}
    assert body["company"]["name"] == "CRASHTEST GmbH"
    assert body["emailAddresses"] == {"business": ["info@x.de"]}
    assert body["phoneNumbers"] == {"business": ["0221 123"]}
    assert body["addresses"]["billing"][0]["countryCode"] == "DE"


def test_contact_body_limits() -> None:
    with pytest.raises(LexwareError, match="nur einen Ansprechpartner"):
        normalize_contact({"company": {"name": "A", "contactPersons": [{}, {}]}})
    with pytest.raises(LexwareError, match="Unbekannte Felder für einen Kontakt: foo"):
        normalize_contact({"company": {"name": "A"}, "foo": 1})
    with pytest.raises(LexwareError, match="entweder eine Firma"):
        normalize_contact({"company": {"name": "A"}, "person": {"lastName": "B"}})


# -- Listen -------------------------------------------------------------------


@respx.mock
def test_list_invoices_german_status(client: LexwareClient) -> None:
    route = respx.get(f"{API}/voucherlist").mock(return_value=page())
    services.list_invoices(client, status="offen")
    assert route.calls[0].request.url.params["voucherStatus"] == "open"


@respx.mock
def test_list_vouchers_default_all_types_and_german_type(client: LexwareClient) -> None:
    route = respx.get(f"{API}/voucherlist").mock(return_value=page())
    services.list_vouchers(client)
    assert "creditnote" in route.calls[0].request.url.params["voucherType"]
    services.list_vouchers(client, voucher_type="Gutschrift")
    assert route.calls[-1].request.url.params["voucherType"] == "creditnote,salescreditnote"


@respx.mock
def test_posting_category_words(client: LexwareClient) -> None:
    respx.get(f"{API}/posting-categories").mock(return_value=httpx.Response(200, json=[
        {"id": "a", "name": "Bürobedarf", "type": "outgo"},
        {"id": "b", "name": "Erlöse", "type": "income"},
    ]))
    def ids(kind: str) -> list[str]:
        return [c["id"] for c in services.list_posting_categories(client, category_type=kind)]

    assert ids("expense") == ["a"]
    assert ids("Einnahmen") == ["b"]
    with pytest.raises(LexwareError, match="outgo"):
        services.list_posting_categories(client, category_type="xyz")


@respx.mock
def test_contact_search_escapes_ampersand_and_falls_back(client: LexwareClient) -> None:
    contact = {"id": U1, "company": {"name": "Mesh &amp; Mörtel",
                                     "contactPersons": [{"lastName": "Krüger"}]}}

    def handler(request: httpx.Request) -> httpx.Response:
        name = request.url.params.get("name")
        if name == "Mesh &amp; Mörtel":
            return page(contact)
        return page() if name else page(contact)

    respx.get(f"{API}/contacts").mock(side_effect=handler)
    assert services.list_contacts(client, name="Mesh & Mörtel").items == [contact]
    # contact person, other spelling — client-side fallback
    assert services.list_contacts(client, name="Krueger").items == [contact]
    # two letters — Lexware needs 3, we search ourselves
    assert services.list_contacts(client, name="Me").items == [contact]


@respx.mock
def test_contact_number_with_prefix(client: LexwareClient) -> None:
    route = respx.get(f"{API}/contacts").mock(return_value=page({"id": U1}))
    services.list_contacts(client, number="K-10021")
    assert route.calls[0].request.url.params["number"] == "10021"
    assert services.resolve_contact_id(client, "10021") == U1


# -- Kontakte und Artikel schreiben -------------------------------------------


@respx.mock
def test_update_contact_refuses_role_removal(client: LexwareClient) -> None:
    respx.get(f"{API}/contacts/{U1}").mock(return_value=httpx.Response(200, json={
        "id": U1, "version": 1, "roles": {"customer": {"number": 10022}},
        "company": {"name": "A"},
    }))
    put = respx.put(f"{API}/contacts/{U1}")
    with pytest.raises(LexwareError, match="Kundennummer"):
        services.update_contact(client, U1, {"roles": {"customer": None}})
    assert not put.called


@respx.mock
def test_update_contact_without_changes_does_not_save(client: LexwareClient) -> None:
    respx.get(f"{API}/contacts/{U1}").mock(
        return_value=httpx.Response(200, json={"id": U1, "version": 1})
    )
    put = respx.put(f"{API}/contacts/{U1}")
    assert "nichts gespeichert" in services.update_contact(client, U1, {})["hinweis"]
    assert not put.called


@respx.mock
def test_create_article_normalizes(client: LexwareClient) -> None:
    post = respx.post(f"{API}/articles").mock(
        return_value=httpx.Response(200, json={"id": U1})
    )
    respx.get(f"{API}/articles/{U1}").mock(return_value=httpx.Response(200, json={"id": U1}))
    services.create_article(client, {
        "name": "CRASHTEST", "type": "service", "unit": "Stunde",
        "netPrice": "49,90 €", "taxRate": "19%",
    })
    body = json.loads(post.calls[0].request.content)
    assert body["title"] == "CRASHTEST" and body["type"] == "SERVICE"
    assert body["unitName"] == "Stunde"
    assert body["price"] == {"netPrice": 49.9, "taxRate": 19, "leadingPrice": "NET"}
    with pytest.raises(LexwareError, match="0, 7 oder 19"):
        services.create_article(client, {"title": "x", "type": "product", "price": {"taxRate": 16}})


@respx.mock
def test_update_article_net_price_on_gross_article(client: LexwareClient) -> None:
    current = {"id": U1, "version": 0, "title": "A", "type": "PRODUCT", "unitName": "Stk",
               "price": {"netPrice": 10, "grossPrice": 11.9, "leadingPrice": "GROSS",
                         "taxRate": 19}}
    stored = {**current, "version": 1,
              "price": {"netPrice": 30, "grossPrice": 35.7, "leadingPrice": "NET", "taxRate": 19}}
    respx.get(f"{API}/articles/{U1}").mock(side_effect=[
        httpx.Response(200, json=current), httpx.Response(200, json=stored)
    ])
    put = respx.put(f"{API}/articles/{U1}").mock(return_value=httpx.Response(200, json={}))
    result = services.update_article(client, U1, {"price": {"netPrice": 30}})
    sent = json.loads(put.calls[0].request.content)["price"]
    assert sent["leadingPrice"] == "NET" and "grossPrice" not in sent
    assert result["price"]["grossPrice"] == 35.7


@respx.mock
def test_update_article_archive_refused(client: LexwareClient) -> None:
    respx.get(f"{API}/articles/{U1}").mock(
        return_value=httpx.Response(200, json={"id": U1, "archived": False})
    )
    with pytest.raises(LexwareError, match="nicht archivieren"):
        services.update_article(client, U1, {"archived": True})


@respx.mock
def test_get_article_number_any_case(client: LexwareClient) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("articleNumber") == "ct-001":
            return page()
        return page({"id": U1, "articleNumber": "CT-001", "title": "Beratung"})

    respx.get(f"{API}/articles").mock(side_effect=handler)
    respx.get(f"{API}/articles/{U1}").mock(return_value=httpx.Response(200, json={"id": U1}))
    assert services.get_article(client, "ct-001")["id"] == U1


# -- Belege -------------------------------------------------------------------


def test_continue_keeps_article_ids() -> None:
    """Server-Fehler: Weiterführen verlor die Artikel-id → Lexware 406."""
    body = services._pursue_body({
        "id": "doc", "voucherStatus": "open", "introduction": "Gerne bieten wir an",
        "lineItems": [
            {"id": "art-1", "type": "material", "name": "A", "lineItemAmount": 5},
            {"id": "line-2", "type": "custom", "name": "B"},
        ],
    }, extra_doc_drops=services._PURSUE_DROP_DUNNING)
    assert body["lineItems"][0] == {"id": "art-1", "type": "material", "name": "A"}
    assert "id" not in body["lineItems"][1]
    assert "introduction" not in body


def test_title_length_counts_escaped_ampersand() -> None:
    from lxw_cli.core.limits import validate_voucher

    with pytest.raises(LexwareError, match="zählen bei Lexware"):
        validate_voucher({"title": "Angebot Müller & Söhne GmbH"[:24]})


def test_document_aliases_and_defaults() -> None:
    out = prepare_document(None, {
        "customerId": U1, "date": "heute", "dueDate": "morgen",
        "positions": [{"name": "Beratung", "price": "100,50", "vat": "19%", "menge": 2}],
    }, INVOICE)
    assert out["address"] == {"contactId": U1}
    line = out["lineItems"][0]
    assert line["type"] == "custom" and line["unitName"] == "Stück"
    assert line["unitPrice"]["netAmount"] == 100.5 and line["quantity"] == 2
    assert out["paymentConditions"]["paymentTermDuration"] == 1
    assert out["shippingConditions"]["shippingType"] == "service"


def test_document_gross_only_and_empty() -> None:
    out = prepare_document(None, {"lineItems": [
        {"type": "custom", "name": "A", "unitPrice": {"grossAmount": 119}}
    ]}, QUOTATION)
    assert out["taxConditions"]["taxType"] == "gross"
    with pytest.raises(LexwareError, match="mindestens eine Position"):
        prepare_document(None, {"lineItems": []}, QUOTATION)


@respx.mock
def test_article_line_filled_from_article(client: LexwareClient) -> None:
    respx.get(f"{API}/articles/{U1}").mock(return_value=httpx.Response(200, json={
        "id": U1, "type": "SERVICE", "title": "Beratung", "unitName": "Stunde",
        "description": "Text", "price": {"netPrice": 95, "taxRate": 19},
    }))
    out = prepare_document(client, {"lineItems": [{"id": U1, "quantity": 2}]}, QUOTATION)
    line = out["lineItems"][0]
    assert line["type"] == "service" and line["name"] == "Beratung"
    assert line["unitPrice"]["netAmount"] == 95 and line["unitName"] == "Stunde"


def test_bookkeeping_voucher_words() -> None:
    out = prepare_voucher({
        "type": "Eingangsrechnung", "voucherDate": "10.11.2026", "totalGrossAmount": "23,80",
        "voucherItems": [{"amount": "23,80", "taxRatePercent": 19, "categoryId": "x"}],
    })
    assert out["type"] == "purchaseinvoice" and out["voucherDate"] == "2026-11-10"
    assert out["taxType"] == "gross" and out["totalTaxAmount"] == 3.8


@respx.mock
def test_voucher_category_by_name_and_duplicate(client: LexwareClient) -> None:
    respx.get(f"{API}/posting-categories").mock(return_value=httpx.Response(200, json=[
        {"id": U2, "name": "Bürobedarf", "type": "outgo"},
    ]))
    existing = {"id": U3, "voucherNumber": "R-1", "contactId": U1, "totalAmount": 119,
                "voucherStatus": "open", "voucherType": "purchaseinvoice",
                "voucherDate": "2026-11-10T00:00:00.000+01:00"}
    respx.get(f"{API}/voucherlist").mock(return_value=page(existing))
    post = respx.post(f"{API}/vouchers")
    result = services.create_voucher(client, {
        "type": "purchaseinvoice", "voucherNumber": "R-1", "contactId": U1,
        "voucherDate": "2026-11-10", "totalGrossAmount": 119,
        "voucherItems": [{"amount": 119, "taxRatePercent": 19, "categoryId": "Bürobedarf"}],
    })
    assert not post.called and "nicht doppelt" in result["hinweis"]
    with pytest.raises(LexwareError, match="Buchungskategorie fehlt"):
        services.create_voucher(client, {"type": "purchaseinvoice", "taxType": "gross",
                                         "voucherItems": [{"amount": 1, "taxRatePercent": 19}]})


@respx.mock
def test_upload_to_invoice_document_explained(client: LexwareClient) -> None:
    respx.get(f"{API}/voucherlist").mock(
        return_value=page({"id": U1, "voucherNumber": "RE0008", "voucherType": "invoice"})
    )
    with pytest.raises(LexwareError, match="Rechnungsteil"):
        services.upload_voucher_file(client, "RE0008", filename="a.pdf", content=b"%PDF-1.4")


@respx.mock
def test_get_invoice_with_credit_note_number(client: LexwareClient) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        types = request.url.params.get("voucherType", "")
        if request.url.params.get("voucherNumber") == "GS0001" and "creditnote" in types:
            return page({"id": U1, "voucherNumber": "GS0001", "voucherType": "creditnote"})
        return page()

    respx.get(f"{API}/voucherlist").mock(side_effect=handler)
    respx.get(f"{API}/credit-notes/{U1}").mock(
        return_value=httpx.Response(200, json={"id": U1, "voucherNumber": "GS0001"})
    )
    data = services.get_invoice(client, "GS0001")
    assert data["id"] == U1 and "Rechnungskorrektur" in data["hinweis"]
    respx.get(f"{API}/credit-notes/{U1}/file").mock(
        return_value=httpx.Response(200, content=b"%PDF-1.4")
    )
    pdf, stem = services.download_pdf(client, "GS0001", services.INVOICE_PDF_TYPES)
    assert pdf.startswith(b"%PDF") and stem == "Rechnungskorrektur-GS0001"


@respx.mock
def test_download_file_by_voucher_number(client: LexwareClient) -> None:
    respx.get(f"{API}/files/A2025-005").mock(return_value=httpx.Response(400, json={}))
    respx.get(f"{API}/voucherlist").mock(return_value=page(
        {"id": U1, "voucherNumber": "A2025-005", "voucherType": "salescreditnote"}
    ))
    respx.get(f"{API}/vouchers/{U1}").mock(
        return_value=httpx.Response(200, json={"id": U1, "files": [U2]})
    )
    respx.get(f"{API}/files/{U2}").mock(return_value=httpx.Response(
        200, content=b"%PDF", headers={"Content-Type": "application/pdf"}
    ))
    data, ctype, _name = services.download_file(client, "A2025-005")
    assert data == b"%PDF" and ctype == "application/pdf"


@respx.mock
def test_dunning_by_invoice_number(client: LexwareClient) -> None:
    respx.get(f"{API}/voucherlist").mock(side_effect=lambda r: page(
        {"id": U1, "voucherNumber": "RE0001", "voucherType": "invoice"}
    ) if r.url.params.get("voucherNumber") in ("RE0001", None) else page())
    respx.get(f"{API}/invoices/{U1}").mock(return_value=httpx.Response(200, json={
        "id": U1, "voucherNumber": "RE0001", "relatedVouchers": [
            {"id": U2, "voucherNumber": "RE0001_1", "voucherType": "dun"},
            {"id": U3, "voucherNumber": "RE0001_2", "voucherType": "dun"},
        ],
    }))
    respx.get(f"{API}/dunnings/{U3}").mock(return_value=httpx.Response(200, json={"id": U3}))
    respx.get(f"{API}/dunnings/{U2}").mock(return_value=httpx.Response(200, json={"id": U2}))
    assert services.get_dunning(client, "RE0001")["id"] == U3
    assert services.get_dunning(client, "RE0001_1")["id"] == U2


@respx.mock
def test_dunning_on_paid_invoice_explained(client: LexwareClient) -> None:
    respx.get(f"{API}/voucherlist").mock(return_value=page(
        {"id": U1, "voucherNumber": "RE0006", "voucherType": "invoice", "voucherStatus": "paid"}
    ))
    respx.get(f"{API}/invoices/{U1}").mock(return_value=httpx.Response(
        200, json={"id": U1, "voucherNumber": "RE0006", "voucherStatus": "paid"}
    ))
    with pytest.raises(LexwareError, match="bereits bezahlt"):
        services.create_dunning(client, "RE0006")


@respx.mock
def test_payment_status_for_several(client: LexwareClient) -> None:
    respx.get(f"{API}/voucherlist").mock(side_effect=lambda r: page(
        {"id": U1 if r.url.params.get("voucherNumber") == "RE1" else U2,
         "voucherNumber": r.url.params.get("voucherNumber"), "voucherType": "invoice"}
    ) if r.url.params.get("voucherNumber") in ("RE1", "RE2") else page())
    respx.get(f"{API}/payments/{U1}").mock(return_value=httpx.Response(200, json={"openAmount": 1}))
    respx.get(f"{API}/payments/{U2}").mock(return_value=httpx.Response(200, json={"openAmount": 2}))
    result = services.get_payment_status(client, "RE1, RE2")
    assert [b["openAmount"] for b in result["belege"]] == [1, 2]


# -- Lexware-Meldungen auf Deutsch ---------------------------------------------


@pytest.mark.parametrize(("body", "expected"), [
    ({"message": "Invalid value '49,90 €' for 'price.netPrice'. Value of type BigDecimal "
                 "expected."}, "braucht eine Zahl"),
    ({"message": "Type mismatch for given value 'CT-004'. Expected type is 'UUID'."},
     "keine gültige id"),
    ({"message": "The unit price must not be null."}, "keinen Preis"),
    ({"message": "No vatfree invoices allowed for this organization"}, "Steuerfreie"),
])
@respx.mock
def test_english_messages_translated(client: LexwareClient, body: dict, expected: str) -> None:
    respx.get(f"{API}/profile").mock(return_value=httpx.Response(406, json=body))
    with pytest.raises(LexwareAPIError, match=expected):
        client.get("/v1/profile")


@respx.mock
def test_bare_404_is_german(client: LexwareClient) -> None:
    respx.get(f"{API}/profile").mock(return_value=httpx.Response(404, text="Not Found"))
    with pytest.raises(LexwareAPIError, match="Nicht gefunden"):
        client.get("/v1/profile")


# -- Review-Befunde (unabhängiger Review 10.11.2026) ---------------------------


def test_thousands_dot_never_guessed() -> None:
    assert str(to_decimal("1.234.567", field="x")) == "1234567"
    with pytest.raises(LexwareError, match="mehrdeutig"):
        to_decimal("1.000", field="quantity")
    assert str(to_decimal("0.500", field="x")) == "0.500"
    assert str(to_decimal("1.000,50", field="x")) == "1000.50"


def test_exact_status_value_not_widened() -> None:
    assert normalize_voucher_status("paidoff") == "paidoff"
    assert normalize_voucher_status("paid") == "paid"
    assert normalize_voucher_status("bezahlt") == "paid,paidoff"


@respx.mock
def test_contact_by_name_needs_exact_match(client: LexwareClient) -> None:
    other = {"id": U2, "company": {"name": "Schmidt & Söhne GmbH"}}
    respx.get(f"{API}/contacts").mock(return_value=page(other))
    with pytest.raises(LexwareError, match="Kein Kontakt heißt genau 'Schmidt'"):
        services.resolve_contact_id(client, "Schmidt")
    assert services.resolve_contact_id(client, "schmidt & soehne gmbh") == U2


def test_hex_looking_names_are_not_ids() -> None:
    with pytest.raises(LexwareError, match="Ungültige Kontakt-ID"):
        services.resolve_contact_id(None, "8c026ea9")  # type: ignore[arg-type]


@respx.mock
def test_monthly_invoice_same_number_is_booked(client: LexwareClient) -> None:
    january = {"id": U3, "voucherNumber": "LEAS-1", "contactId": U1, "totalAmount": 119,
               "voucherStatus": "open", "voucherType": "purchaseinvoice",
               "voucherDate": "2026-01-10T00:00:00.000+01:00"}
    respx.get(f"{API}/voucherlist").mock(return_value=page(january))
    post = respx.post(f"{API}/vouchers").mock(
        return_value=httpx.Response(200, json={"id": U2})
    )
    result = services.create_voucher(client, {
        "type": "purchaseinvoice", "voucherNumber": "LEAS-1", "contactId": U1,
        "voucherDate": "2026-02-10", "totalGrossAmount": 119, "taxType": "gross",
        "voucherItems": [{"amount": 119, "taxRatePercent": 19, "categoryId": U2}],
    })
    assert post.called and result["id"] == U2


def test_voucher_tax_type_from_total() -> None:
    items = [{"amount": 100, "taxRatePercent": 19, "categoryId": "x"}]
    assert prepare_voucher({"voucherItems": items, "totalGrossAmount": 119})["taxType"] == "net"
    assert prepare_voucher({"voucherItems": items, "totalGrossAmount": 100})["taxType"] == "gross"
    with pytest.raises(LexwareError, match="taxType fehlt"):
        prepare_voucher({"voucherItems": items})


@respx.mock
def test_number_fallback_respects_status(client: LexwareClient) -> None:
    paid = {"id": U1, "voucherNumber": "RE0006", "voucherType": "invoice",
            "voucherStatus": "paid"}
    respx.get(f"{API}/voucherlist").mock(side_effect=lambda r: page(paid) if (
        r.url.params.get("voucherNumber") == "RE0006"
        and "open" not in r.url.params.get("voucherStatus", "").split(",")[:1]
        and r.url.params.get("voucherStatus", "").startswith("draft")
    ) else page())
    assert services.list_invoices(client, number="RE0006", status="open").items == []


@respx.mock
def test_dunning_number_with_hyphen_invoice(client: LexwareClient) -> None:
    respx.get(f"{API}/voucherlist").mock(side_effect=lambda r: page(
        {"id": U1, "voucherNumber": "RE-2026-12", "voucherType": "invoice"}
    ) if r.url.params.get("voucherNumber") == "RE-2026-12" else page())
    respx.get(f"{API}/invoices/{U1}").mock(return_value=httpx.Response(200, json={
        "id": U1, "voucherNumber": "RE-2026-12",
        "relatedVouchers": [{"id": U2, "voucherNumber": "RE-2026-12_1",
                             "voucherType": "dunning"}],
    }))
    respx.get(f"{API}/dunnings/{U2}").mock(return_value=httpx.Response(200, json={"id": U2}))
    assert services.get_dunning(client, "RE-2026-12")["id"] == U2
    with pytest.raises(LexwareError, match="RE-2026-12_5 gibt es nicht"):
        services.get_dunning(client, "RE-2026-12_5")


@respx.mock
def test_partial_billing_address_keeps_other_fields(client: LexwareClient) -> None:
    current = {"id": U1, "version": 2, "company": {"name": "A"}, "addresses": {"billing": [
        {"street": "Weg 1", "zip": "50667", "city": "Köln", "countryCode": "DE"}]}}
    respx.get(f"{API}/contacts/{U1}").mock(return_value=httpx.Response(200, json=current))
    put = respx.put(f"{API}/contacts/{U1}").mock(return_value=httpx.Response(200, json={}))
    services.update_contact(client, U1, {"city": "Bonn"})
    billing = json.loads(put.calls[0].request.content)["addresses"]["billing"]
    assert billing == [{"street": "Weg 1", "zip": "50667", "city": "Bonn", "countryCode": "DE"}]


@respx.mock
def test_pdf_refuses_silent_pick(client: LexwareClient) -> None:
    respx.get(f"{API}/voucherlist").mock(return_value=page(
        {"id": U1, "voucherNumber": "1001", "voucherType": "invoice"},
        {"id": U2, "voucherNumber": "1001", "voucherType": "purchaseinvoice"},
    ))
    with pytest.raises(LexwareError, match="über seine id"):
        services.download_pdf(client, "1001", services.ALL_VOUCHER_TYPES)


@respx.mock
def test_customer_by_name_sends_only_contact_id(client: LexwareClient) -> None:
    """Live 10.11.: Name + contactId → Lexware verlangt die ganze Adresse."""
    respx.get(f"{API}/contacts").mock(
        return_value=page({"id": U1, "company": {"name": "CloudKontor AG"}})
    )
    post = respx.post(f"{API}/invoices").mock(return_value=httpx.Response(200, json={"id": U2}))
    services.create_invoice(client, {"customer": "CloudKontor AG", "lineItems": [
        {"type": "custom", "name": "A", "unitPrice": {"netAmount": 1}}]})
    assert json.loads(post.calls[0].request.content)["address"] == {"contactId": U1}
