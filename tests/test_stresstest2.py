"""Regressionen aus dem zweiten Stresstest (10.11.2026, 0.9.13)."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from lxw_cli.core import services
from lxw_cli.core.client import LexwareClient
from lxw_cli.core.errors import LexwareError
from lxw_cli.core.normalize import normalize_contact
from lxw_cli.core.prepare import (
    DELIVERY_NOTE,
    INVOICE,
    document_timestamp,
    prepare_document,
    prepare_voucher,
)

API = "https://api.lexware.io/v1"
U1 = "11111111-1111-4111-8111-111111111111"
U2 = "22222222-2222-4222-8222-222222222222"
LINE = {"type": "custom", "name": "A", "unitPrice": {"netAmount": 10}}


def page(*rows: dict) -> httpx.Response:
    return httpx.Response(200, json={"content": list(rows), "last": True})


# -- Buchhaltungsbelege --------------------------------------------------------


@pytest.mark.parametrize("extra", [{"mwst": 7}, {"vat": 7}, {"ust": 7}, {"MwSt": 7}])
def test_receipt_tax_rate_words(extra: dict) -> None:
    out = prepare_voucher({"type": "Quittung", "voucherNumber": "B1", "betrag": "10,70",
                           "kategorie": "Porto", **extra})
    assert out["voucherItems"][0]["taxRatePercent"] == 7


def test_receipt_tax_free_and_item_aliases() -> None:
    out = prepare_voucher({"type": "Quittung", "voucherNumber": "P1", "betrag": "2,71",
                           "kategorie": "Porto", "steuerfrei": True})
    assert out["voucherItems"][0]["taxRatePercent"] == 0
    out = prepare_voucher({"type": "Eingangsrechnung", "voucherNumber": "G1",
                           "taxType": "gross", "voucherItems": [
                               {"betrag": 107, "mwst": 7, "kategorie": "Waren"}]})
    assert out["voucherItems"][0] == {"amount": 107, "taxRatePercent": 7,
                                      "categoryId": "Waren", "taxAmount": 7.0}


def test_receipt_unknown_and_payment_fields() -> None:
    with pytest.raises(LexwareError, match="Unbekannte Felder im Buchhaltungsbeleg: farbe"):
        prepare_voucher({"type": "Quittung", "voucherNumber": "1", "farbe": "rot"})
    with pytest.raises(LexwareError, match="Zahlung lässt sich"):
        prepare_voucher({"type": "Quittung", "voucherNumber": "1", "bezahlt": True})
    out = prepare_voucher({"type": "Quittung", "voucherNumber": "1", "betrag": 5,
                           "kategorie": "Porto", "faellig": "19.11.2026"})
    assert out["dueDate"] == "2026-11-19"


def test_unchecked_voucher_needs_no_number() -> None:
    from lxw_cli.core.prepare import voucher_missing

    assert voucher_missing({"voucherStatus": "unchecked", "type": "purchaseinvoice"}) == []
    out = prepare_voucher({"voucherStatus": "unchecked", "type": "purchaseinvoice",
                           "taxType": "gross"})
    assert "voucherDate" not in out


def test_line_rounded_tax_amount_kept() -> None:
    out = prepare_voucher({"type": "purchaseinvoice", "voucherNumber": "K1", "taxType": "gross",
                           "totalGrossAmount": 0.30, "voucherItems": [
                               {"amount": 0.30, "taxRatePercent": 19, "taxAmount": 0.06,
                                "categoryId": "x"}]})
    assert out["voucherItems"][0]["taxAmount"] == 0.06 and out["totalTaxAmount"] == 0.06


# -- Belege ------------------------------------------------------------------------


@respx.mock
def test_full_one_off_address_is_not_linked(client: LexwareClient) -> None:
    respx.get(f"{API}/contacts").mock(
        return_value=page({"id": U1, "company": {"name": "Schweiz AG"}})
    )
    post = respx.post(f"{API}/invoices").mock(return_value=httpx.Response(200, json={"id": U2}))
    address = {"name": "Schweiz AG", "street": "Bahnhofstr. 1", "zip": "8001",
               "city": "Zürich", "countryCode": "CH"}
    services.create_invoice(client, {"address": address, "lineItems": [LINE]})
    assert json.loads(post.calls[0].request.content)["address"] == address


@respx.mock
def test_article_line_keeps_empty_description_and_delivery_note_has_no_price(
    client: LexwareClient,
) -> None:
    respx.get(f"{API}/articles/{U1}").mock(return_value=httpx.Response(200, json={
        "id": U1, "type": "PRODUCT", "title": "Ware", "unitName": "Stk",
        "description": "Langtext", "price": {"netPrice": 65, "taxRate": 19}}))
    out = prepare_document(client, {"lineItems": [{"id": U1, "description": ""}]}, INVOICE)
    assert out["lineItems"][0]["description"] == ""
    out = prepare_document(client, {"lineItems": [{"id": U1, "quantity": 2}]}, DELIVERY_NOTE)
    assert "unitPrice" not in out["lineItems"][0]


def test_skonto_label_and_english() -> None:
    out = prepare_document(None, {"zahlungsziel": 14, "skonto": "2 % 7 Tage",
                                  "lineItems": [LINE]}, INVOICE)
    cond = out["paymentConditions"]
    assert cond["paymentDiscountConditions"] == {"discountPercentage": 2, "discountRange": 7}
    assert "ohne Abzug" not in cond["paymentTermLabel"] and "Skonto" in cond["paymentTermLabel"]
    out = prepare_document(None, {"sprache": "englisch", "zahlungsziel": 30,
                                  "lineItems": [{"name": "Design", "preis": 100}]}, INVOICE)
    assert out["language"] == "en"
    assert out["paymentConditions"]["paymentTermLabel"].startswith("Payable within 30 days")
    assert out["lineItems"][0]["unitName"] == "piece"


def test_period_strings() -> None:
    out = prepare_document(None, {"ausfuehrungszeitraum": "17.11.–21.11.2026",
                                  "lineItems": [LINE]}, INVOICE)
    ship = out["shippingConditions"]
    assert ship["shippingType"] == "serviceperiod"
    assert ship["shippingDate"].startswith("2026-11-17")
    assert ship["shippingEndDate"].startswith("2026-11-21")


def test_copy_gets_todays_date() -> None:
    out = prepare_document(None, {"id": U1, "voucherNumber": "RE0004",
                                  "voucherDate": "2026-09-22T00:00:00.000+02:00",
                                  "lineItems": [LINE]}, INVOICE)
    assert not out["voucherDate"].startswith("2026-09-22")
    assert out.get("__copy")


def test_foreign_offset_keeps_the_day() -> None:
    assert document_timestamp("2026-11-10T02:00:00+05:00").startswith("2026-11-10")


# -- Weiterführen ----------------------------------------------------------------

INV = {"id": U1, "voucherNumber": "RE0005", "voucherStatus": "open",
       "address": {"contactId": U2}, "lineItems": [LINE], "totalPrice": {"currency": "EUR"},
       "taxConditions": {"taxType": "net"}, "relatedVouchers": [],
       "createdDate": "2026-09-01T00:00:00.000+02:00"}


def _source(doc: dict) -> None:
    respx.get(f"{API}/voucherlist").mock(return_value=page(
        {"id": doc["id"], "voucherNumber": doc["voucherNumber"], "voucherType": "invoice"}))
    respx.get(f"{API}/invoices/{doc['id']}").mock(return_value=httpx.Response(200, json=doc))


@respx.mock
def test_dunning_refuses_own_positions(client: LexwareClient) -> None:
    _source(INV)
    with pytest.raises(LexwareError, match="Mahngebühr"):
        services.continue_document(client, "RE0005", "Mahnung",
                                   changes={"lineItems": [{"name": "Gebühr", "preis": 5}]})


@respx.mock
def test_empty_positions_in_changes_refused(client: LexwareClient) -> None:
    _source(INV)
    with pytest.raises(LexwareError, match="0 €"):
        services.continue_document(client, "RE0005", "Lieferschein", changes={"lineItems": []})


@respx.mock
def test_credit_note_from_draft_explained(client: LexwareClient) -> None:
    _source({**INV, "voucherStatus": "draft"})
    with pytest.raises(LexwareError, match="noch ein Entwurf"):
        services.continue_document(client, "RE0005", "Gutschrift")


def test_final_invoice_words_are_invoices() -> None:
    assert services._TARGET_ALIASES["schlussrechnung"] == "invoice"


# -- Kontakte / Artikel -----------------------------------------------------------


@respx.mock
def test_update_contact_german_address_keeps_rest(client: LexwareClient) -> None:
    current = {"id": U1, "version": 1, "company": {"name": "A"}, "addresses": {"billing": [
        {"street": "Graben 1", "zip": "1010", "city": "Wien", "countryCode": "AT"}]}}
    respx.get(f"{API}/contacts/{U1}").mock(return_value=httpx.Response(200, json=current))
    put = respx.put(f"{API}/contacts/{U1}").mock(return_value=httpx.Response(200, json={}))
    services.update_contact(client, U1, {"strasse": "Graben 2"})
    billing = json.loads(put.calls[0].request.content)["addresses"]["billing"][0]
    assert billing == {"street": "Graben 2", "zip": "1010", "city": "Wien", "countryCode": "AT"}


def test_contact_vat_id_aliases() -> None:
    body = normalize_contact({"firma": "A GmbH", "ustid": "ATU12345678",
                              "steuernummer": "12/345"})
    assert body["company"]["vatRegistrationId"] == "ATU12345678"
    assert body["company"]["taxNumber"] == "12/345"


@respx.mock
def test_update_article_full_echo_with_new_leading_price(client: LexwareClient) -> None:
    current = {"id": U1, "version": 0, "title": "A", "type": "PRODUCT", "unitName": "Stk",
               "price": {"netPrice": 84.03, "grossPrice": 100, "leadingPrice": "GROSS",
                         "taxRate": 19}}
    respx.get(f"{API}/articles/{U1}").mock(return_value=httpx.Response(200, json=current))
    put = respx.put(f"{API}/articles/{U1}").mock(return_value=httpx.Response(200, json={}))
    echo = {**current, "price": {**current["price"], "grossPrice": 119}}
    services.update_article(client, U1, echo)
    assert json.loads(put.calls[0].request.content)["price"]["grossPrice"] == 119


# -- Angriff/Grenzfälle (Stresstest 2) ----------------------------------------


def test_euro_discount_is_absolute_and_conflicts_refused() -> None:
    out = prepare_document(None, {"rabatt": "50 €", "lineItems": [LINE]}, INVOICE)
    assert out["totalPrice"] == {"currency": "EUR", "totalDiscountAbsolute": 50}
    with pytest.raises(LexwareError, match="Mehrere Rabattangaben"):
        prepare_document(None, {"rabatt_prozent": 5, "rabatt": "50 €", "lineItems": [LINE]},
                         INVOICE)
    with pytest.raises(LexwareError, match="nur Rabatt in Prozent"):
        prepare_document(None, {"lineItems": [{**LINE, "rabatt": "20 EUR"}]}, INVOICE)


def test_payment_term_words() -> None:
    out = prepare_document(None, {"zahlungsziel": "dreißig Tage", "lineItems": [LINE]},
                           INVOICE)
    assert out["paymentConditions"]["paymentTermDuration"] == 30
    with pytest.raises(LexwareError, match="nicht lesbar"):
        prepare_document(None, {"zahlungsziel": "irgendwann", "lineItems": [LINE]}, INVOICE)
    with pytest.raises(LexwareError, match="vor dem Belegdatum"):
        prepare_document(None, {"voucherDate": "10.11.2026", "dueDate": "01.11.2026",
                                "lineItems": [LINE]}, INVOICE)


def test_price_contradictions() -> None:
    with pytest.raises(LexwareError, match="passen bei 19 % nicht"):
        prepare_document(None, {"lineItems": [{"name": "A", "unitPrice": {
            "netAmount": 50, "grossAmount": 119, "taxRatePercentage": 19}}]}, INVOICE)
    with pytest.raises(LexwareError, match="Prozentwert"):
        prepare_document(None, {"lineItems": [{"name": "A", "unitPrice": {
            "netAmount": "12,5%"}}]}, INVOICE)
    with pytest.raises(LexwareError, match="nur einmal"):
        prepare_document(None, {"kunde": "X", "address": {"contactId": U1},
                                "lineItems": [LINE]}, INVOICE)


@respx.mock
def test_type_word_picks_the_right_document(client: LexwareClient) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        types = request.url.params.get("voucherType", "")
        number = request.url.params.get("voucherNumber")
        if number == "RE0040" and "invoice" in types:
            return page({"id": U1, "voucherNumber": "RE0040", "voucherType": "invoice"})
        if number == "AG0040" and "quotation" in types:
            return page({"id": U2, "voucherNumber": "AG0040", "voucherType": "quotation"})
        if number is None:
            rows = [{"id": "a", "voucherNumber": "AG0039", "voucherType": "quotation"},
                    {"id": "r", "voucherNumber": "RE0039", "voucherType": "invoice"}]
            return page(*[r for r in rows if r["voucherType"] in types])
        return page()

    respx.get(f"{API}/voucherlist").mock(side_effect=handler)
    assert services.find_voucher(client, "Rechnung 40", services.ALL_VOUCHER_TYPES).id == U1
    assert services.find_voucher(client, "Angebot 40", services.ALL_VOUCHER_TYPES).id == U2


def test_receipt_amounts_as_text_and_negative_credit() -> None:
    out = prepare_voucher({"type": "Eingangsrechnung", "voucherNumber": "T1",
                           "totalGrossAmount": "1.000,00", "voucherItems": [
                               {"amount": "1.000,00", "taxRatePercent": "19 %",
                                "categoryId": "x"}]})
    assert out["taxType"] == "gross" and out["totalTaxAmount"] == 159.66
    out = prepare_voucher({"type": "purchasecreditnote", "voucherNumber": "G1",
                           "taxType": "gross", "voucherItems": [
                               {"amount": -23.8, "taxRatePercent": 19, "categoryId": "x"}]})
    assert out["voucherItems"][0]["amount"] == 23.8
    with pytest.raises(LexwareError, match="passt weder"):
        prepare_voucher({"type": "Eingangsrechnung", "voucherNumber": "T2",
                         "totalGrossAmount": 200, "voucherItems": [
                             {"amount": 100, "taxRatePercent": 19, "categoryId": "x"}]})


def test_contact_address_object_with_german_keys() -> None:
    body = normalize_contact({"firma": "A", "adresse": {"strasse": "Weg 1", "plz": "50667",
                                                        "ort": "Köln", "land": "Deutschland"}})
    assert body["addresses"]["billing"] == [
        {"street": "Weg 1", "zip": "50667", "city": "Köln", "countryCode": "DE"}]


def test_identifier_cleaning() -> None:
    assert services._clean_number("​RE００４０") == "RE0040"
    assert services._clean_number("'RE0078'") == "RE0078"
