"""Regressionen aus dem Stresstest gegen die Demo (10.11.2026, 0.9.12)."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from lxw_cli.core import services
from lxw_cli.core.client import LexwareClient
from lxw_cli.core.errors import LexwareError
from lxw_cli.core.normalize import normalize_article, normalize_contact
from lxw_cli.core.prepare import INVOICE, QUOTATION, prepare_document, prepare_voucher

API = "https://api.lexware.io/v1"
U1 = "11111111-1111-4111-8111-111111111111"
U2 = "22222222-2222-4222-8222-222222222222"
LINE = {"type": "custom", "name": "A", "unitPrice": {"netAmount": 10}}


def page(*rows: dict) -> httpx.Response:
    return httpx.Response(200, json={"content": list(rows), "last": True})


# -- Belege anlegen -----------------------------------------------------------


def test_customer_string_is_a_reference_not_an_address_name() -> None:
    out = prepare_document(None, {"kunde": "Kodewerk", "lineItems": [LINE]}, QUOTATION)
    assert out["address"] == {"contactId": "Kodewerk"}  # resolved (or refused) later


def test_customer_postal_string_becomes_one_off_address() -> None:
    out = prepare_document(
        None, {"kunde": "Malerbetrieb Meier, Lindenweg 3, 53113 Bonn", "lineItems": [LINE]},
        QUOTATION,
    )
    assert out["address"] == {"name": "Malerbetrieb Meier", "street": "Lindenweg 3",
                              "zip": "53113", "city": "Bonn", "countryCode": "DE"}


def test_german_document_fields() -> None:
    out = prepare_document(None, {
        "titel": "STRESS", "rechnungsdatum": "01.11.2026", "zahlbar_bis": "15.11.2026",
        "rabatt_prozent": "3", "lineItems": [LINE],
    }, INVOICE)
    assert out["title"] == "STRESS"
    assert out["voucherDate"].startswith("2026-11-01")
    assert out["paymentConditions"]["paymentTermDuration"] == 14
    assert out["totalPrice"]["totalDiscountPercentage"] == 3
    out = prepare_document(None, {"gueltig_bis": "08.12.2026", "lineItems": [LINE]}, QUOTATION)
    assert out["expirationDate"].startswith("2026-12-08")


def test_unknown_document_field_is_reported() -> None:
    with pytest.raises(LexwareError, match="Unbekannte Felder im Beleg: lieferant"):
        prepare_document(None, {"lieferant": "x", "lineItems": [LINE]}, QUOTATION)
    with pytest.raises(LexwareError, match="Position 1: farbe"):
        prepare_document(None, {"lineItems": [{**LINE, "farbe": "rot"}]}, QUOTATION)


def test_mixed_net_and_gross_prices_refused() -> None:
    with pytest.raises(LexwareError, match="Gemischte Preisangaben"):
        prepare_document(None, {"lineItems": [
            {"name": "Beratung", "unitPrice": 95, "quantity": 2},
            {"name": "Druck", "unitPrice": {"grossAmount": 119}},
        ]}, INVOICE)
    out = prepare_document(None, {"lineItems": [
        {"name": "Beratung", "unitPrice": 95, "quantity": 2},
    ]}, INVOICE)
    assert out["taxConditions"]["taxType"] == "net"
    assert out["lineItems"][0]["unitPrice"]["netAmount"] == 95


# -- Kontakte und Artikel -----------------------------------------------------


def test_german_contact_fields() -> None:
    body = normalize_contact({
        "firma": "STRESS Bäckerei", "ansprechpartner": "Frau Petra Klein",
        "strasse": "Hauptstr. 5", "plz": "50667", "ort": "Köln", "kunde": True,
    })
    assert body["company"]["name"] == "STRESS Bäckerei"
    assert body["company"]["contactPersons"][0] == {
        "salutation": "Frau", "firstName": "Petra", "lastName": "Klein", "primary": True
    }
    assert body["addresses"]["billing"][0]["city"] == "Köln"
    assert body["roles"] == {"customer": {}}
    person = normalize_contact({"vorname": "Max", "nachname": "Muster", "anrede": "Herr"})
    assert person["person"] == {"firstName": "Max", "lastName": "Muster", "salutation": "Herr"}


def test_article_price_as_number() -> None:
    from lxw_cli.core.services import _ARTICLE_TYPES

    out = normalize_article({"name": "Webhosting", "preis": 19.9, "typ": "Dienstleistung"},
                            article_type_map=_ARTICLE_TYPES)
    assert out["price"] == {"netPrice": 19.9} and out["type"] == "SERVICE"


# -- Buchhaltungsbelege ------------------------------------------------------


def test_receipt_with_one_amount() -> None:
    out = prepare_voucher({"art": "Eingangsrechnung", "nummer": "T-1", "betrag": "68,40",
                           "kategorie": "Kraftstoff", "lieferant": "Aral"})
    assert out["voucherItems"] == [{"amount": 68.4, "taxRatePercent": 19,
                                    "categoryId": "Kraftstoff", "taxAmount": 10.92}]
    assert out["contactName"] == "Aral" and out["taxType"] == "gross"


def test_voucher_missing_fields_all_at_once() -> None:
    with pytest.raises(LexwareError) as err:
        prepare_voucher({"voucherItems": [{"amount": 10, "taxRatePercent": 19}]})
    text = str(err.value)
    assert "type" in text and "voucherNumber" in text and "taxType" in text


# -- Weiterführen -------------------------------------------------------------

QUOTE = {
    "id": U1, "voucherNumber": "AG0030", "voucherStatus": "open",
    "address": {"contactId": U2},
    "lineItems": [{"type": "custom", "name": "A", "quantity": 1,
                   "unitPrice": {"netAmount": 100, "taxRatePercentage": 19}}],
    "totalPrice": {"currency": "EUR", "totalDiscountPercentage": 3},
    "taxConditions": {"taxType": "net"}, "relatedVouchers": [],
    "introduction": "Gerne bieten wir an",
}


def _source(doc: dict, kind: str = "quotation", path: str = "quotations"):
    respx.get(f"{API}/voucherlist").mock(return_value=page(
        {"id": doc["id"], "voucherNumber": doc["voucherNumber"], "voucherType": kind}
    ))
    respx.get(f"{API}/{path}/{doc['id']}").mock(return_value=httpx.Response(200, json=doc))


@respx.mock
def test_continue_keeps_discount_sets_service_date_and_reports(client: LexwareClient) -> None:
    _source(QUOTE)
    post = respx.post(f"{API}/invoices").mock(return_value=httpx.Response(200, json={"id": U2}))
    respx.get(f"{API}/invoices/{U2}").mock(return_value=httpx.Response(200, json={
        "id": U2, "voucherNumber": "RE0050", "voucherStatus": "draft",
        "totalPrice": {"totalGrossAmount": 115.43}}))
    result = services.continue_document(client, "AG0030", "RE",
                                        changes={"zahlungsziel": 14})
    body = json.loads(post.calls[0].request.content)
    assert body["totalPrice"]["totalDiscountPercentage"] == 3
    assert body["shippingConditions"]["shippingType"] == "service"
    assert body["paymentConditions"]["paymentTermDuration"] == 14
    assert "introduction" not in body
    assert result["voucherNumber"] == "RE0050"


@respx.mock
def test_continue_refuses_second_invoice(client: LexwareClient) -> None:
    doc = {**QUOTE, "relatedVouchers": [{"id": "x", "voucherNumber": "RE0041",
                                         "voucherType": "invoice"}]}
    _source(doc)
    post = respx.post(f"{API}/invoices")
    with pytest.raises(LexwareError, match="RE0041"):
        services.continue_document(client, "AG0030", "Rechnung")
    assert not post.called


@respx.mock
def test_continue_optional_positions_explained(client: LexwareClient) -> None:
    doc = {**QUOTE, "lineItems": [{**QUOTE["lineItems"][0], "optional": True}]}
    _source(doc)
    with pytest.raises(LexwareError, match="optionalen oder alternativen"):
        services.continue_document(client, "AG0030", "Auftrag")


@respx.mock
def test_continue_grammar_and_quotation_target(client: LexwareClient) -> None:
    inv = {**QUOTE, "voucherNumber": "RE0004", "voucherStatus": "open"}
    _source(inv, "invoice", "invoices")
    with pytest.raises(LexwareError, match="^Eine Rechnung kann nicht"):
        services.continue_document(client, "RE0004", "Auftrag")
    with pytest.raises(LexwareError, match="am Anfang der Kette"):
        services.continue_document(client, "RE0004", "Angebot")


@respx.mock
def test_partial_credit_note_via_changes(client: LexwareClient) -> None:
    inv = {**QUOTE, "voucherNumber": "RE0001", "voucherStatus": "open"}
    _source(inv, "invoice", "invoices")
    post = respx.post(f"{API}/credit-notes").mock(
        return_value=httpx.Response(200, json={"id": U2})
    )
    respx.get(f"{API}/credit-notes/{U2}").mock(return_value=httpx.Response(200, json={"id": U2}))
    result = services.continue_document(client, "RE0001", "Gutschrift", changes={
        "lineItems": [{"name": "Gutschrift Reklamation", "preis": 42.02}]})
    items = json.loads(post.calls[0].request.content)["lineItems"]
    assert items == [{"name": "Gutschrift Reklamation", "type": "custom", "quantity": 1,
                      "unitName": "Stück", "unitPrice": {"netAmount": 42.02,
                      "currency": "EUR", "taxRatePercentage": 19}}]
    assert "hinweis" not in result


@respx.mock
def test_second_dunning_is_titled_with_level(client: LexwareClient) -> None:
    inv = {**QUOTE, "voucherNumber": "RE0002", "voucherStatus": "open", "relatedVouchers": [
        {"id": "d1", "voucherNumber": "RE0002_1", "voucherType": "dun"}]}
    _source(inv, "invoice", "invoices")
    post = respx.post(f"{API}/dunnings").mock(return_value=httpx.Response(200, json={"id": U2}))
    respx.get(f"{API}/dunnings/{U2}").mock(return_value=httpx.Response(200, json={"id": U2}))
    result = services.create_dunning(client, "RE0002")
    assert json.loads(post.calls[0].request.content)["title"] == "2. Mahnung"
    assert "Mahnstufe 2" in result["hinweis"]


# -- Listen -------------------------------------------------------------------


@respx.mock
def test_list_invoices_date_range(client: LexwareClient) -> None:
    route = respx.get(f"{API}/voucherlist").mock(return_value=page())
    services.list_invoices(client, date_from="01.10.2026", date_to="2026-10-31")
    params = route.calls[0].request.url.params
    assert params["voucherDateFrom"] == "2026-10-01"
    assert params["voucherDateTo"] == "2026-10-31"


@respx.mock
def test_posting_category_search(client: LexwareClient) -> None:
    respx.get(f"{API}/posting-categories").mock(return_value=httpx.Response(200, json=[
        {"id": "a", "name": "Kraftstoff/Ladestrom", "groupName": "Fahrzeug", "type": "outgo"},
        {"id": "b", "name": "Bürobedarf", "groupName": "Sonstige", "type": "outgo"},
    ]))
    assert [c["id"] for c in services.list_posting_categories(client, search="kraft")] == ["a"]
    assert [c["id"] for c in services.list_posting_categories(client, search="Buero")] == ["b"]


# -- Robustheit (Stresstest 10.11., zweite Runde) ------------------------------


@respx.mock
def test_empty_identifier_never_picks_a_voucher(client: LexwareClient) -> None:
    route = respx.get(f"{API}/voucherlist").mock(return_value=page({"id": U1}))
    for value in ("", "   ", "''"):
        with pytest.raises(LexwareError, match="Feld war leer"):
            services.get_invoice(client, value)
        with pytest.raises(LexwareError, match="Feld war leer"):
            services.create_dunning(client, value)
    assert not route.called


def test_roles_list_of_objects_and_nan() -> None:
    from lxw_cli.core.normalize import to_decimal

    body = normalize_contact({"company": {"name": "A"}, "roles": [{"customer": {}}]})
    assert body["roles"] == {"customer": {}}
    for bad in ("NaN", "Infinity", float("nan")):
        with pytest.raises(LexwareError, match="endliche Zahl|muss eine Zahl"):
            to_decimal(bad, field="netPrice")
    assert str(to_decimal("100.-", field="x")) == "100"
    assert str(to_decimal("2 Stück", field="x")) == "2"


def test_control_characters_removed() -> None:
    out = prepare_document(None, {"lineItems": [
        {**LINE, "name": "Fehler\u0000hier\u0007", "description": "a\nb\tc"}]}, QUOTATION)
    assert out["lineItems"][0]["name"] == "Fehlerhier"
    assert out["lineItems"][0]["description"] == "a\nb\tc"
    assert normalize_contact({"company": {"name": "A"}, "note": "x\u0000y"})["note"] == "xy"


def test_dates_month_names_and_impossible_days() -> None:
    from lxw_cli.core.prepare import document_timestamp

    assert document_timestamp("Nov 10, 2026").startswith("2026-11-10")
    assert document_timestamp("10. November 2026").startswith("2026-11-10")
    with pytest.raises(LexwareError, match="gibt es nicht"):
        document_timestamp("2026-11-31")
    with pytest.raises(LexwareError, match="gibt es nicht"):
        document_timestamp("29.02.2027")
    with pytest.raises(LexwareError, match="vor dem Belegdatum"):
        prepare_document(None, {"voucherDate": "20.11.2026", "expirationDate": "15.11.2026",
                                "lineItems": [LINE]}, QUOTATION)


def test_uppercase_keys_rounding_language() -> None:
    out = prepare_document(None, {"Positionen": [{"Name": "A", "Preis": "1,23456",
                                                  "Rabatt": 33.333}],
                                  "Titel": "X", "language": "englisch"}, QUOTATION)
    line = out["lineItems"][0]
    assert line["unitPrice"]["netAmount"] == 1.2346
    assert line["discountPercentage"] == 33.33
    assert out["title"] == "X" and out["language"] == "en"


@respx.mock
def test_article_number_as_position_id(client: LexwareClient) -> None:
    respx.get(f"{API}/articles").mock(return_value=page(
        {"id": U1, "articleNumber": "STR-002", "title": "Dienst", "type": "SERVICE",
         "unitName": "Std", "price": {"netPrice": 50, "taxRate": 19}}))
    respx.get(f"{API}/articles/{U1}").mock(return_value=httpx.Response(200, json={
        "id": U1, "type": "SERVICE", "title": "Dienst", "unitName": "Std",
        "price": {"netPrice": 50, "taxRate": 19}}))
    out = prepare_document(client, {"lineItems": [{"articleNumber": "str-002", "menge": 2}]},
                           QUOTATION)
    line = out["lineItems"][0]
    assert line["id"] == U1 and line["type"] == "service"
    assert line["unitPrice"]["netAmount"] == 50


def test_voucher_kind_words() -> None:
    out = prepare_voucher({"type": "Quittung", "voucherNumber": "Q1", "betrag": "12,345",
                           "kategorie": "Porto"})
    assert out["type"] == "purchaseinvoice"
    assert out["voucherItems"][0]["amount"] == 12.35
    with pytest.raises(LexwareError, match="Unbekannte Belegart 'Spende'"):
        prepare_voucher({"type": "Spende", "voucherNumber": "Q1"})


def test_contact_nested_german_keys() -> None:
    body = normalize_contact({
        "company": "STRESS GmbH",
        "contactPersons": [{"vorname": "Eva", "nachname": "Roth", "email": "e@x.de",
                            "telefon": "0221 1"}],
    })
    person = body["company"]["contactPersons"][0]
    assert body["company"]["name"] == "STRESS GmbH"
    assert person["emailAddress"] == "e@x.de" and person["phoneNumber"] == "0221 1"
    assert person["firstName"] == "Eva" and person["lastName"] == "Roth"
    private = normalize_contact({"person": {"vorname": "Max", "nachname": "M"}})
    assert private["person"] == {"firstName": "Max", "lastName": "M"}


@respx.mock
def test_collective_duplicate_not_booked_twice(client: LexwareClient) -> None:
    existing = {"id": U2, "voucherNumber": "EB-1", "contactName": "Sammellieferant",
                "totalAmount": 23.8, "voucherStatus": "open", "voucherType": "purchaseinvoice",
                "voucherDate": "2026-11-10T00:00:00.000+01:00"}
    respx.get(f"{API}/voucherlist").mock(return_value=page(existing))
    respx.get(f"{API}/posting-categories").mock(return_value=httpx.Response(200, json=[
        {"id": U1, "name": "Bürobedarf", "type": "outgo"}]))
    post = respx.post(f"{API}/vouchers")
    result = services.create_voucher(client, {
        "type": "purchaseinvoice", "voucherNumber": "EB-1", "voucherDate": "2026-11-10",
        "totalGrossAmount": 23.8, "taxType": "gross",
        "voucherItems": [{"amount": 23.8, "taxRatePercent": 19, "categoryId": "Bürobedarf"}]})
    assert not post.called and "nicht doppelt" in result["hinweis"]


@respx.mock
def test_category_of_wrong_kind_explained(client: LexwareClient) -> None:
    respx.get(f"{API}/posting-categories").mock(return_value=httpx.Response(200, json=[
        {"id": U1, "name": "Taxi", "type": "outgo"}]))
    respx.get(f"{API}/voucherlist").mock(return_value=page())
    with pytest.raises(LexwareError, match="keine Einnahmen-Kategorie"):
        services.create_voucher(client, {
            "type": "salesinvoice", "voucherNumber": "A1", "taxType": "gross",
            "voucherItems": [{"amount": 10, "taxRatePercent": 19, "categoryId": "Taxi"}]})


def test_article_net_and_gross_must_fit() -> None:
    with pytest.raises(LexwareError, match="passen bei 19 % nicht"):
        services._check_price_consistency({"netPrice": 5, "grossPrice": 100, "taxRate": 19},
                                          None)
    services._check_price_consistency({"netPrice": 100, "grossPrice": 119, "taxRate": 19},
                                      None)


@respx.mock
def test_prefixed_numbers_with_separator(client: LexwareClient) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        number = request.url.params.get("voucherNumber")
        if number == "LS0011":
            return page({"id": U1, "voucherNumber": "LS0011", "voucherType": "deliverynote"})
        if number is None:
            return page({"id": "x", "voucherNumber": "LS0010", "voucherType": "deliverynote"})
        return page()

    respx.get(f"{API}/voucherlist").mock(side_effect=handler)
    for text in ("ls 11", "Lieferschein LS0011", "LS-0011"):
        match = services.find_voucher(client, text, services.ALL_VOUCHER_TYPES)
        assert match.id == U1, text


# -- Review 0.9.13 -------------------------------------------------------------


def test_zero_tax_rate_kept_for_receipt() -> None:
    out = prepare_voucher({"type": "purchaseinvoice", "voucherNumber": "1", "betrag": 5,
                           "kategorie": "Porto", "taxRatePercent": 0})
    assert out["voucherItems"][0]["taxRatePercent"] == 0
    assert out["voucherItems"][0]["taxAmount"] == 0


def test_price_alias_with_special_tax_type() -> None:
    out = prepare_document(None, {"taxConditions": {"taxType": "intraCommunitySupply"},
                                  "lineItems": [{"name": "A", "price": 100}]}, INVOICE)
    assert out["lineItems"][0]["unitPrice"]["netAmount"] == 100


def test_copy_of_read_document_is_accepted() -> None:
    read = {"id": U1, "voucherNumber": "RE0016", "voucherStatus": "open", "version": 3,
            "claimedGrossAmount": 1, "downPaymentDeductions": [], "taxAmounts": [],
            "relatedVouchers": [], "hinweis": "x", "lineItems": [{**LINE, "lineItemAmount": 10}]}
    out = prepare_document(None, read, INVOICE)
    assert "claimedGrossAmount" not in out and "hinweis" not in out and "id" not in out
    assert "lineItemAmount" not in out["lineItems"][0]


@respx.mock
def test_due_date_in_changes_becomes_payment_term(client: LexwareClient) -> None:
    order = {**QUOTE, "id": U1, "voucherNumber": "AB0005", "voucherStatus": "open",
             "createdDate": "2026-11-01T00:00:00.000+01:00"}
    _source(order, "orderconfirmation", "order-confirmations")
    post = respx.post(f"{API}/invoices").mock(return_value=httpx.Response(200, json={"id": U2}))
    respx.get(f"{API}/invoices/{U2}").mock(return_value=httpx.Response(200, json={"id": U2}))
    from datetime import date, timedelta

    due = (date.today() + timedelta(days=20)).strftime("%d.%m.%Y")
    services.continue_document(client, "AB0005", "Rechnung", changes={"zahlbar_bis": due})
    body = json.loads(post.calls[0].request.content)
    assert body["paymentConditions"]["paymentTermDuration"] in (19, 20, 21)


@respx.mock
def test_unknown_customer_name_becomes_one_off_address(client: LexwareClient) -> None:
    respx.get(f"{API}/contacts").mock(return_value=page())
    post = respx.post(f"{API}/quotations").mock(
        return_value=httpx.Response(200, json={"id": U2})
    )
    result = services.create_quotation(client, {"kunde": "Max Mustermann", "lineItems": [LINE]})
    assert json.loads(post.calls[0].request.content)["address"] == {
        "name": "Max Mustermann", "countryCode": "DE"}
    assert "Kundenstamm" in result["hinweis"]


def test_austrian_postal_string() -> None:
    out = prepare_document(None, {"kunde": "Huber GmbH, Ring 1, 1010 Wien",
                                  "lineItems": [LINE]}, QUOTATION)
    assert out["address"]["zip"] == "1010" and out["address"]["city"] == "Wien"


@respx.mock
def test_predecessor_is_not_a_duplicate(client: LexwareClient) -> None:
    inv = {**QUOTE, "voucherNumber": "RE0050", "voucherStatus": "open",
           "createdDate": "2026-11-10T10:00:00.000+01:00",
           "relatedVouchers": [{"id": "ls1", "voucherNumber": "LS0001",
                                "voucherType": "deliverynote"}]}
    _source(inv, "invoice", "invoices")
    respx.get(f"{API}/delivery-notes/ls1").mock(return_value=httpx.Response(200, json={
        "id": "ls1", "createdDate": "2026-11-09T10:00:00.000+01:00", "voucherStatus": "open"}))
    post = respx.post(f"{API}/delivery-notes").mock(
        return_value=httpx.Response(200, json={"id": U2})
    )
    respx.get(f"{API}/delivery-notes/{U2}").mock(return_value=httpx.Response(200, json={}))
    services.continue_document(client, "RE0050", "Lieferschein")
    body = json.loads(post.calls[0].request.content)
    assert post.called and "totalDiscountPercentage" not in body["totalPrice"]


@respx.mock
def test_mistyped_number_is_not_a_customer(client: LexwareClient) -> None:
    respx.get(f"{API}/voucherlist").mock(return_value=page())
    contacts = respx.get(f"{API}/contacts").mock(return_value=page({"id": U1}))
    with pytest.raises(LexwareError, match="Kein Beleg"):
        services.create_dunning(client, "RE10005")
    assert not contacts.called


@respx.mock
def test_receipts_without_number_are_not_deduplicated(client: LexwareClient) -> None:
    other = {"id": U2, "voucherNumber": "Kassenbon", "contactName": "Sammellieferant",
             "totalAmount": 3.5, "voucherStatus": "open", "voucherType": "purchaseinvoice",
             "voucherDate": "2026-11-10T00:00:00.000+01:00"}
    respx.get(f"{API}/voucherlist").mock(return_value=page(other))
    post = respx.post(f"{API}/vouchers").mock(return_value=httpx.Response(200, json={"id": U1}))
    services.create_voucher(client, {
        "type": "purchaseinvoice", "voucherNumber": "Kassenbon", "voucherDate": "2026-11-10",
        "totalGrossAmount": 3.5, "taxType": "gross",
        "voucherItems": [{"amount": 3.5, "taxRatePercent": 19, "categoryId": U1}]})
    assert post.called


def test_article_requires_tax_rate(client: LexwareClient) -> None:
    with pytest.raises(LexwareError, match="Steuersatz"):
        services.create_article(client, {"title": "A", "type": "SERVICE", "unitName": "Std",
                                         "price": 19.9})


def test_huge_number_is_a_clear_error() -> None:
    with pytest.raises(LexwareError):
        prepare_document(None, {"lineItems": [{**LINE, "quantity": 1.5e300}]}, QUOTATION)
