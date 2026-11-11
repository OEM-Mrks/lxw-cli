"""Regressionen aus dem dritten Stresstest (11.11.2026, 0.9.14)."""

from __future__ import annotations

import json
from datetime import timedelta

import httpx
import pytest
import respx

from lxw_cli.core import services
from lxw_cli.core.client import LexwareClient
from lxw_cli.core.errors import LexwareAPIError, LexwareError
from lxw_cli.core.normalize import normalize_contact, strip_control, to_decimal
from lxw_cli.core.prepare import (
    DELIVERY_NOTE,
    INVOICE,
    QUOTATION,
    _now,
    parse_terms,
    prepare_changes,
    prepare_document,
    prepare_voucher,
)

API = "https://api.lexware.io/v1"
U1 = "11111111-1111-4111-8111-111111111111"
U2 = "22222222-2222-4222-8222-222222222222"
U3 = "33333333-3333-4333-8333-333333333333"
U4 = "44444444-4444-4444-8444-444444444444"
LINE = {"type": "custom", "name": "A", "unitPrice": {"netAmount": 10}}
ARTICLE = {"id": U1, "type": "PRODUCT", "title": "Flyer A6", "articleNumber": "ST3-A-001",
           "unitName": "Stück", "price": {"netPrice": 8.9, "grossPrice": 10.59, "taxRate": 19}}


def page(*rows: dict) -> httpx.Response:
    return httpx.Response(200, json={"content": list(rows), "last": True})


def ok(payload: dict) -> httpx.Response:
    return httpx.Response(200, json=payload)


def sent(route: respx.Route, index: int = 0) -> dict:
    return json.loads(route.calls[index].request.content)


# -- 1: Lieferschein-Artikelpreis, 0 % bei steuerfreien Steuerarten -------------


@respx.mock
def test_delivery_note_article_price_and_tax_free_rate(client: LexwareClient) -> None:
    respx.get(f"{API}/articles/{U1}").mock(return_value=ok(ARTICLE))
    out = prepare_document(client, {"taxConditions": "intraCommunitySupply", "lineItems": [
        {"id": U1, "quantity": 2}, {"name": "Fracht", "preis": 5},
        {"type": "custom", "name": "Muster"}]}, DELIVERY_NOTE)
    article, freight, sample = out["lineItems"]
    assert article["unitPrice"] == {"netAmount": 8.9, "taxRatePercentage": 0, "currency": "EUR"}
    assert freight["unitPrice"]["taxRatePercentage"] == 0
    assert sample["unitPrice"]["netAmount"] == 0  # free LS position without price
    out = prepare_document(client, {"taxConditions": {"taxType": "vatfree"},
                                    "lineItems": [{"id": U1}]}, INVOICE)
    assert out["lineItems"][0]["unitPrice"]["taxRatePercentage"] == 0


def test_tax_free_explicit_rate_set_to_zero_with_note() -> None:
    out = prepare_document(None, {"steuerart": "innergemeinschaftliche Lieferung",
                                  "lineItems": [{**LINE, "unitPrice": {
                                      "netAmount": 10, "taxRatePercentage": 19}}]}, INVOICE)
    assert out["taxConditions"]["taxType"] == "intraCommunitySupply"
    assert out["lineItems"][0]["unitPrice"]["taxRatePercentage"] == 0
    assert "0 %" in out["__hinweis"]


# -- 2/3: Zahlen mit Resttext ------------------------------------------------------


@pytest.mark.parametrize("price", ["+5%", "10 % mehr", "plus 3 €"])
def test_price_change_is_no_price(price: str) -> None:
    with pytest.raises(LexwareError, match="Änderung"):
        prepare_document(None, {"lineItems": [{"name": "A", "preis": price}]}, INVOICE)


def test_quantity_units() -> None:
    def qty(value: object, **extra: object) -> object:
        item = {"name": "A", "preis": 10, "menge": value, **extra}
        return prepare_document(None, {"lineItems": [item]}, INVOICE)["lineItems"][0]

    assert qty("3 Stück")["quantity"] == 3
    assert qty("2 Std", einheit="Stunde")["quantity"] == 2
    assert qty("2 Std")["unitName"] == "Stunde"
    with pytest.raises(LexwareError, match="0,5 für eine halbe Stunde"):
        qty("30 min", einheit="Stunde")
    with pytest.raises(LexwareError, match="Menge '10 % mehr'"):
        qty("10 % mehr")


def test_gross_word_in_price_on_net_document() -> None:
    out = prepare_document(None, {"taxConditions": "net", "lineItems": [
        {"name": "A", "preis": "14,90 € brutto"}]}, INVOICE)
    price = out["lineItems"][0]["unitPrice"]
    assert price["netAmount"] == 12.521 and "grossAmount" not in price
    assert "Bruttopreis" in out["__hinweis"]
    out = prepare_document(None, {"lineItems": [{"name": "A", "preis": "11,90 brutto"}]},
                           INVOICE)
    assert out["taxConditions"]["taxType"] == "gross"
    assert out["lineItems"][0]["unitPrice"]["grossAmount"] == 11.9
    with pytest.raises(LexwareError, match="netto"):
        prepare_document(None, {"lineItems": [{"name": "A", "unitPrice": {
            "grossAmount": "10 netto"}}]}, INVOICE)


def test_number_formats_still_work_and_1_000_is_ambiguous() -> None:
    for text, value in (("1.234,56", "1234.56"), ("12,-", "12"), ("EUR 5", "5"),
                        ("119,00 €", "119.00"), ("１２", "12"), ("1 000", "1000"),
                        ("5 Euro", "5"), ("0,500", "0.500")):
        assert str(to_decimal(text, field="x", kind="money")) == value
    assert int(to_decimal("2e3", field="x")) == 2000
    for text in ("1,000", "12,345"):
        with pytest.raises(LexwareError, match="mehrdeutig"):
            to_decimal(text, field="x")


@respx.mock
def test_article_price_change_names_current_price(client: LexwareClient) -> None:
    respx.get(f"{API}/articles/{U1}").mock(return_value=ok({**ARTICLE, "version": 1}))
    with pytest.raises(LexwareError, match=r"aktuell 8,90 €"):
        services.update_article(client, U1, {"price": "+5%"})
    from lxw_cli.core.normalize import normalize_article

    body = normalize_article({"title": "A", "preis": "14,90 € brutto", "steuersatz": "voll"},
                             article_type_map={})
    assert body["price"] == {"grossPrice": 14.9, "taxRate": 19}


# -- 4/5: Zahlungsbedingungen mit Skonto -----------------------------------------


@pytest.mark.parametrize("text", [
    "14 Tage 2 % Skonto, 30 Tage netto",
    "30 Tage netto, 2 % Skonto bei Zahlung innerhalb 14 Tagen",
    "30 Tage, 2 % Skonto bei Zahlung innerhalb von 14 Tagen",
    "2% Skonto 14 Tage, 30 Tage netto",
    "Zahlbar innerhalb von 30 Tagen. Bei Zahlung innerhalb von 14 Tagen 2 % Skonto.",
])
def test_skonto_in_free_text(text: str) -> None:
    out = prepare_document(None, {"zahlungsziel": text, "lineItems": [LINE]}, INVOICE)
    cond = out["paymentConditions"]
    assert cond["paymentTermDuration"] == 30
    assert cond["paymentDiscountConditions"] == {"discountPercentage": 2, "discountRange": 14}
    assert cond["paymentTermLabel"] == ("Zahlbar innerhalb von 30 Tagen. Bei Zahlung "
                                        "innerhalb von 14 Tagen 2 % Skonto.")


def test_payment_label_only_and_net_words() -> None:
    for text, days in (("Zahlbar innerhalb von 21 Tagen", 21), ("netto 30", 30),
                       ("net 30", 30), ("30 days net", 30)):
        out = prepare_document(None, {"paymentConditions": text, "lineItems": [LINE]},
                               INVOICE)
        assert out["paymentConditions"]["paymentTermDuration"] == days
    out = prepare_document(None, {"voucherDate": "10.11.2026", "zahlungsziel":
                                  "Zahlbar bis 01.12.2026", "lineItems": [LINE]}, INVOICE)
    assert out["paymentConditions"]["paymentTermDuration"] == 21
    out = prepare_document(None, {"language": "en", "zahlungsziel": "14 Tage 2 % Skonto, "
                                  "30 Tage netto", "lineItems": [LINE]}, INVOICE)
    assert out["paymentConditions"]["paymentTermLabel"].startswith("Payable within 30 days.")
    assert parse_terms("netto 30, ohne Skonto", None).no_skonto


def test_unclear_or_contradicting_terms_raise() -> None:
    for text in ("2 % Skonto", "Skonto nach Absprache, 30 Tage", "14 oder 30 Tage"):
        with pytest.raises(LexwareError):
            prepare_document(None, {"zahlungsziel": text, "lineItems": [LINE]}, INVOICE)
    with pytest.raises(LexwareError, match="Widersprüchliche Zahlungsfristen"):
        prepare_document(None, {"zahlungsziel": 14, "paymentConditions": {
            "paymentTermLabel": "Zahlbar innerhalb von 30 Tagen"}, "lineItems": [LINE]},
            INVOICE)
    with pytest.raises(LexwareError, match="keine Frist"):
        prepare_document(None, {"paymentConditions": "nach Vereinbarung",
                                "lineItems": [LINE]}, INVOICE)


INV = {"id": U1, "voucherNumber": "RE0005", "voucherStatus": "open",
       "voucherDate": "2026-11-01T00:00:00.000+01:00",
       "address": {"contactId": U2, "name": "Huber GmbH"}, "lineItems": [LINE],
       "totalPrice": {"currency": "EUR", "totalGrossAmount": 119.0,
                      "totalDiscountPercentage": 10},
       "taxConditions": {"taxType": "net"}, "relatedVouchers": [],
       "paymentConditions": {"paymentTermDuration": 30, "paymentTermLabel": "x",
                             "paymentDiscountConditions": {"discountPercentage": 2,
                                                           "discountRange": 10}},
       "createdDate": "2026-11-01T10:00:00.000+01:00"}


def _source(doc: dict, vtype: str = "invoice", path: str = "invoices") -> None:
    respx.get(f"{API}/voucherlist").mock(return_value=page(
        {"id": doc["id"], "voucherNumber": doc["voucherNumber"], "voucherType": vtype,
         "voucherStatus": doc.get("voucherStatus")}))
    respx.get(f"{API}/{path}/{doc['id']}").mock(return_value=ok(doc))


def _target(path: str) -> respx.Route:
    route = respx.post(f"{API}/{path}").mock(return_value=ok({"id": U3}))
    respx.get(f"{API}/{path}/{U3}").mock(return_value=ok({"id": U3, "voucherNumber": "NEU1"}))
    return route


@respx.mock
def test_continue_new_term_keeps_skonto_in_label(client: LexwareClient) -> None:
    _source(INV)
    post = _target("credit-notes")
    result = services.continue_document(client, "RE0005", "Rechnungskorrektur",
                                        changes={"zahlungsziel": 14})
    cond = sent(post)["paymentConditions"]
    assert cond["paymentTermDuration"] == 14 and cond["paymentDiscountConditions"]
    assert "Skonto" in cond["paymentTermLabel"] and "ohne Abzug" not in cond["paymentTermLabel"]
    assert "bleibt erhalten" in result["hinweis"]
    services.continue_document(client, "RE0005", "Rechnungskorrektur", allow_duplicate=True,
                               changes={"zahlungsziel": 14, "skonto": 0})
    cond = sent(post, 1)["paymentConditions"]
    assert "paymentDiscountConditions" not in cond
    assert cond["paymentTermLabel"] == "Zahlbar innerhalb von 14 Tagen ohne Abzug"


# -- 6: Gesamtrabatt umstellen -----------------------------------------------------


@respx.mock
def test_continue_switches_discount_kind(client: LexwareClient) -> None:
    quote = {**INV, "voucherNumber": "AG0001", "voucherStatus": "accepted",
             "expirationDate": None}
    _source(quote, "quotation", "quotations")
    post = _target("invoices")
    for change, expected in (({"rabatt": "50 €"}, {"totalDiscountAbsolute": 50}),
                             ({"totalPrice": {"totalDiscountAbsolute": "50,00 €"}},
                              {"totalDiscountAbsolute": 50}),
                             ({"rabatt": "10 %"}, {"totalDiscountPercentage": 10}),
                             ({"rabatt": 0}, {})):
        services.continue_document(client, "AG0001", "Rechnung", changes=change,
                                   allow_duplicate=True)
        total = sent(post, len(post.calls) - 1)["totalPrice"]
        assert {k: v for k, v in total.items() if k != "currency"} == expected


def test_total_discount_translation() -> None:
    from lxw_cli.core.client import _translate_text

    text = _translate_text("The total discount has to be either absolute or percentage "
                           "but not both.", 406)
    assert "entweder in Prozent ODER als Betrag" in text


# -- 7/8/9: Kontakte ändern ----------------------------------------------------------

CONTACT = {"id": U2, "version": 3, "roles": {"customer": {"number": 10052}},
           "company": {"name": "Huber GmbH", "contactPersons": [
               {"salutation": "Frau", "firstName": "Sabine", "lastName": "Huber",
                "emailAddress": "s@huber.de", "phoneNumber": "0221 1", "primary": True}]},
           "addresses": {"billing": [{"street": "Ring 1", "zip": "50667", "city": "Köln",
                                      "countryCode": "DE"}],
                         "shipping": [{"street": "Hafen 2", "zip": "40210",
                                       "city": "Düsseldorf", "countryCode": "DE"}]}}


def _contact_routes(current: dict = CONTACT) -> respx.Route:
    respx.get(f"{API}/contacts/{U2}").mock(return_value=ok(current))
    return respx.put(f"{API}/contacts/{U2}").mock(return_value=ok({"version": 4}))


@respx.mock
def test_update_shipping_address_partially(client: LexwareClient) -> None:
    put = _contact_routes()
    services.update_contact(client, U2, {"addresses": {"shipping": [
        {"city": "Leverkusen", "zip": "51373"}]}})
    assert sent(put)["addresses"]["shipping"] == [
        {"street": "Hafen 2", "zip": "51373", "city": "Leverkusen", "countryCode": "DE"}]
    services.update_contact(client, U2, {"lieferadresse": {"strasse": "Kai 3"}})
    assert sent(put, 1)["addresses"]["shipping"][0]["street"] == "Kai 3"
    assert sent(put, 1)["addresses"]["shipping"][0]["city"] == "Düsseldorf"
    assert sent(put, 1)["addresses"]["billing"][0]["street"] == "Ring 1"


@respx.mock
def test_update_contact_person_merge_or_replace(client: LexwareClient) -> None:
    put = _contact_routes()
    services.update_contact(client, U2, {"ansprechpartner": {"telefon": "0221 999"}})
    person = sent(put)["company"]["contactPersons"][0]
    assert person["salutation"] == "Frau" and person["emailAddress"] == "s@huber.de"
    assert person["phoneNumber"] == "0221 999"
    result = services.update_contact(client, U2, {"ansprechpartner": {
        "anrede": "Herr", "vorname": "Max", "nachname": "Gruber"}})
    assert "Frau Sabine Huber wurde durch Herrn Max Gruber ersetzt" in result["hinweis"]
    assert sent(put, 1)["company"]["contactPersons"][0]["lastName"] == "Gruber"


@respx.mock
def test_address_as_text(client: LexwareClient) -> None:
    body = normalize_contact({"firma": "A GmbH", "adresse": "Westfalendamm 10, 44141 Dortmund"})
    assert body["addresses"]["billing"] == [
        {"street": "Westfalendamm 10", "zip": "44141", "city": "Dortmund", "countryCode": "DE"}]
    with pytest.raises(LexwareError, match="Straße Nr, PLZ Ort"):
        normalize_contact({"firma": "A GmbH", "adresse": "irgendwo in Dortmund"})
    put = _contact_routes()
    services.update_contact(client, U2, {"adresse": "Graben 1, 1010 Wien, Österreich"})
    assert sent(put)["addresses"]["billing"][0] == {"street": "Graben 1", "zip": "1010",
                                                     "city": "Wien", "countryCode": "AT"}


@respx.mock
def test_update_email_replacement_is_said(client: LexwareClient) -> None:
    put = _contact_routes({**CONTACT, "emailAddresses": {"business": ["alt@huber.de"]}})
    result = services.update_contact(client, U2, {"email": "neu@huber.de"})
    assert sent(put)["emailAddresses"]["business"] == ["neu@huber.de"]
    assert "alt@huber.de wurde durch neu@huber.de ersetzt" in result["hinweis"]


# -- 10: Kurznummer und Belegart-Wort -------------------------------------------------


def _numbers(rows: list[dict]):  # noqa: ANN202
    """Voucherlist mock that filters like Lexware (incl. its loose endings)."""
    def handler(request: httpx.Request) -> httpx.Response:
        types = request.url.params.get("voucherType", "").split(",")
        number = request.url.params.get("voucherNumber")
        hits = [r for r in rows if r["voucherType"] in types]
        if number is not None:
            hits = [r for r in hits if r["voucherNumber"].casefold() == number.casefold()
                    or r["voucherNumber"].endswith(f"-{number}")]
        return page(*hits)
    return handler


ROWS = [
    {"id": U1, "voucherNumber": "RE0001", "voucherType": "invoice", "voucherStatus": "open"},
    {"id": U2, "voucherNumber": "AG0001", "voucherType": "quotation", "voucherStatus": "open"},
    {"id": U3, "voucherNumber": "ST2-V-ER-1", "voucherType": "purchaseinvoice",
     "voucherStatus": "open"},
    {"id": U4, "voucherNumber": "4711", "voucherType": "purchaseinvoice",
     "voucherStatus": "open"},
]


@respx.mock
def test_short_number_with_type_word(client: LexwareClient) -> None:
    respx.get(f"{API}/voucherlist").mock(side_effect=_numbers(ROWS))
    assert services.find_voucher(client, "Rechnung 1", services.ALL_VOUCHER_TYPES).id == U1
    assert services.find_voucher(client, "Angebot 1", services.ALL_VOUCHER_TYPES).id == U2
    assert services.find_voucher(client, "ER 4711", services.ALL_VOUCHER_TYPES).id == U4
    assert services.find_voucher(client, "Eingangsrechnung 4711",
                                 services.ALL_VOUCHER_TYPES).id == U4
    # the caller's own preference does not override the word
    match = services.find_voucher(client, "Angebot 1", services.ALL_VOUCHER_TYPES,
                                  prefer="invoice")
    assert match.id == U2
    with pytest.raises(LexwareError) as exc:
        services.find_voucher(client, "1", services.ALL_VOUCHER_TYPES)
    assert "RE0001" in str(exc.value) and "AG0001" in str(exc.value)
    assert "Gezeigt" not in str(exc.value)


@respx.mock
def test_payment_status_and_dunning_use_the_word(client: LexwareClient) -> None:
    respx.get(f"{API}/voucherlist").mock(side_effect=_numbers(ROWS))
    respx.get(f"{API}/payments/{U1}").mock(return_value=ok({"openAmount": 5}))
    assert services.get_payment_status(client, "Rechnung 1")["openAmount"] == 5
    respx.get(f"{API}/invoices/{U1}").mock(return_value=ok({**INV, "id": U1,
                                                            "voucherNumber": "RE0001"}))
    respx.post(f"{API}/dunnings").mock(return_value=ok({"id": U3}))
    respx.get(f"{API}/dunnings/{U3}").mock(return_value=ok({"id": U3}))
    assert services.create_dunning(client, "Rechnung 1")["id"] == U3


@respx.mock
def test_ambiguous_source_message_shows_candidates(client: LexwareClient) -> None:
    respx.get(f"{API}/voucherlist").mock(return_value=page(
        {"id": U1, "voucherNumber": "AB0109", "voucherType": "orderconfirmation"},
        {"id": U2, "voucherNumber": "AB0109", "voucherType": "orderconfirmation"}))
    with pytest.raises(LexwareError) as exc:
        services.continue_document(client, "AB0109", "Rechnung")
    assert "Mehrere Belege mit der Nummer 'AB0109'" in str(exc.value)
    assert "Gezeigt" not in str(exc.value)


# -- 11: Mahnungen ----------------------------------------------------------------------

DUN_INV = {**INV, "voucherNumber": "RE0003", "relatedVouchers": [
    {"id": "d1", "voucherNumber": "RE0003_1", "voucherType": "dun"},
    {"id": "d2", "voucherNumber": "RE0003_2", "voucherType": "dun"}]}


@respx.mock
def test_dunning_draft_blocks_another(client: LexwareClient) -> None:
    _source(DUN_INV)
    respx.get(f"{API}/dunnings/d1").mock(return_value=ok({"voucherStatus": "open"}))
    respx.get(f"{API}/dunnings/d2").mock(return_value=ok({"voucherStatus": "draft"}))
    post = respx.post(f"{API}/dunnings")
    with pytest.raises(LexwareError, match=r"Mahnungs-Entwurf \(RE0003_2\)"):
        services.create_dunning(client, "RE0003")
    assert not post.called


@respx.mock
def test_dunning_level_counts_finished_ones_and_returns_number(client: LexwareClient) -> None:
    doc = {**DUN_INV, "relatedVouchers": [*DUN_INV["relatedVouchers"],
                                          {"id": U3, "voucherNumber": "RE0003_3",
                                           "voucherType": "dun"}]}
    respx.get(f"{API}/voucherlist").mock(return_value=page(
        {"id": U1, "voucherNumber": "RE0003", "voucherType": "invoice"}))
    respx.get(f"{API}/invoices/{U1}").mock(side_effect=[ok(DUN_INV), ok(doc)])
    respx.get(f"{API}/dunnings/d1").mock(return_value=ok({"voucherStatus": "open"}))
    respx.get(f"{API}/dunnings/d2").mock(return_value=ok({"voucherStatus": "voided"}))
    post = respx.post(f"{API}/dunnings").mock(return_value=ok({"id": U3}))
    respx.get(f"{API}/dunnings/{U3}").mock(return_value=ok({"id": U3}))
    result = services.create_dunning(client, "RE0003")
    assert sent(post)["title"] == "2. Mahnung"
    assert result["voucherNumber"] == "RE0003_3"


@respx.mock
def test_dunning_suggestions_only_dunnable_invoices(client: LexwareClient) -> None:
    def voucherlist(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("voucherNumber"):
            return page()
        if request.url.params.get("contactId"):
            return page({"voucherNumber": "RE0010", "voucherType": "invoice",
                         "voucherStatus": "overdue", "openAmount": 50, "voucherDate": "x"},
                        {"voucherNumber": "ER-77", "voucherType": "salesinvoice",
                         "voucherStatus": "overdue", "openAmount": 9, "voucherDate": "y"})
        return page()

    respx.get(f"{API}/voucherlist").mock(side_effect=voucherlist)
    respx.get(f"{API}/contacts").mock(return_value=page(
        {"id": U2, "person": {"firstName": "Peter", "lastName": "Lang"}}))
    with pytest.raises(LexwareError) as exc:
        services.create_dunning(client, "Peter Lang")
    assert "RE0010" in str(exc.value) and "ER-77" not in str(exc.value)


@respx.mock
def test_dunning_of_quotation_names_the_type(client: LexwareClient) -> None:
    respx.get(f"{API}/voucherlist").mock(side_effect=lambda r: page(
        {"id": U2, "voucherNumber": "AG0092", "voucherType": "quotation"})
        if "quotation" in r.url.params.get("voucherType", "") else page())
    with pytest.raises(LexwareError, match="AG0092 ist ein Angebot – Mahnungen"):
        services.get_dunning(client, "AG0092")


@respx.mock
def test_dunning_number_in_get_voucher_and_payment_status(client: LexwareClient) -> None:
    respx.get(f"{API}/voucherlist").mock(side_effect=lambda r: page(
        {"id": U1, "voucherNumber": "RE0003", "voucherType": "invoice"})
        if r.url.params.get("voucherNumber") == "RE0003" else page())
    respx.get(f"{API}/invoices/{U1}").mock(return_value=ok(DUN_INV))
    respx.get(f"{API}/dunnings/d1").mock(return_value=ok({"id": "d1"}))
    doc = services.get_voucher(client, "RE0003_1")
    assert doc["voucherType"] == "dunning" and "Rechnung RE0003" in doc["hinweis"]
    status = services.get_payment_status(client, "RE0003_1")
    assert status["voucherType"] == "dunning" and status["invoice"] == "RE0003"
    assert "siehe Rechnung RE0003" in status["hinweis"]


# -- 12: netto in Buchhaltungsbeleg-Positionen -----------------------------------------


def test_voucher_item_net_word() -> None:
    with pytest.raises(LexwareError, match="Widerspruch"):
        prepare_voucher({"type": "Eingangsrechnung", "voucherNumber": "1", "taxType": "gross",
                         "voucherItems": [{"netto": 100, "steuersatz": 19,
                                           "kategorie": "x"}]})
    out = prepare_voucher({"type": "Eingangsrechnung", "voucherNumber": "1",
                           "voucherItems": [{"netto": 100, "steuersatz": "ermäßigt",
                                             "kategorie": "x"}]})
    assert out["taxType"] == "net" and out["totalGrossAmount"] == 107


# -- 13/14: Englisch und Leistungsdatum beim Weiterführen -----------------------------


@respx.mock
def test_continue_english_text_and_shipping_date_follows(client: LexwareClient) -> None:
    quote = {**INV, "voucherNumber": "AG0002", "voucherStatus": "accepted", "language": "en",
             "paymentConditions": {"paymentTermDuration": 0,
                                   "paymentTermLabel": "Zahlbar sofort, rein netto"}}
    _source(quote, "quotation", "quotations")
    post = _target("invoices")
    services.continue_document(client, "AG0002", "Rechnung",
                               changes={"voucherDate": "gestern"})
    body = sent(post)
    assert body["paymentConditions"]["paymentTermLabel"] == (
        "Payable immediately without deduction.")
    yesterday = (_now() - timedelta(days=1)).date().isoformat()
    assert body["voucherDate"].startswith(yesterday)
    assert body["shippingConditions"]["shippingDate"].startswith(yesterday)


def test_changes_label_in_english() -> None:
    patch = prepare_changes({"zahlungsziel": 14}, INVOICE, "net",
                            "2026-11-10T00:00:00.000+01:00", language="en")
    assert patch["paymentConditions"]["paymentTermLabel"] == (
        "Payable within 14 days without deduction.")


# -- 15/16: Doppel-Schutz über die Kette, Teilgutschriften ------------------------------


@respx.mock
def test_second_delivery_note_via_the_quotation(client: LexwareClient) -> None:
    invoice = {**INV, "voucherNumber": "RE0040", "createdDate": "2026-11-05T10:00:00.000+01:00",
               "relatedVouchers": [{"id": "ag", "voucherNumber": "AG0040",
                                    "voucherType": "quotation"}]}
    _source(invoice)
    respx.get(f"{API}/quotations/ag").mock(return_value=ok({
        "id": "ag", "createdDate": "2026-11-01T10:00:00.000+01:00", "relatedVouchers": [
            {"id": "ls", "voucherNumber": "LS0037", "voucherType": "deliverynote"},
            {"id": U1, "voucherNumber": "RE0040", "voucherType": "invoice"}]}))
    respx.get(f"{API}/delivery-notes/ls").mock(return_value=ok({
        "id": "ls", "voucherStatus": "open", "createdDate": "2026-11-02T10:00:00.000+01:00",
        "relatedVouchers": [{"id": "ag", "voucherType": "quotation"}]}))
    post = respx.post(f"{API}/delivery-notes")
    with pytest.raises(LexwareError, match="LS0037"):
        services.continue_document(client, "RE0040", "Lieferschein")
    assert not post.called


CREDITED = {**INV, "relatedVouchers": [
    {"id": "g1", "voucherNumber": "GS0014", "voucherType": "creditnote"},
    {"id": "g2", "voucherNumber": "GS0015", "voucherType": "creditnote"}]}


def _credit_notes() -> None:
    for gid, amount in (("g1", 11.9), ("g2", 23.8)):
        respx.get(f"{API}/credit-notes/{gid}").mock(return_value=ok({
            "voucherStatus": "open", "createdDate": "2026-11-03T10:00:00.000+01:00",
            "totalPrice": {"totalGrossAmount": amount}}))


@respx.mock
def test_full_credit_note_refused_when_credits_exist(client: LexwareClient) -> None:
    _source(CREDITED)
    _credit_notes()
    with pytest.raises(LexwareError, match=r"GS0014 \(11.90 €\), GS0015 \(23.80 €\)"):
        services.continue_document(client, "RE0005", "Gutschrift")


@respx.mock
def test_partial_credit_with_german_positions(client: LexwareClient) -> None:
    _source(CREDITED)
    _credit_notes()
    post = _target("credit-notes")
    result = services.continue_document(client, "RE0005", "Gutschrift", changes={
        "positionen": [{"bezeichnung": "Reklamation", "preis": "20,00"}]})
    assert sent(post)["lineItems"][0]["unitPrice"]["netAmount"] == 20
    assert "VOLLE" not in result["hinweis"] and "GS0014" in result["hinweis"]
    with pytest.raises(LexwareError, match="höher als"):
        services.continue_document(client, "RE0005", "Gutschrift", changes={
            "positionen": [{"bezeichnung": "Zu viel", "preis": 80}]})


# -- 17/18: changes wie bei create_*, unpassende changes ---------------------------------


@respx.mock
def test_changes_positions_are_completed_from_article(client: LexwareClient) -> None:
    _source({**INV, "voucherNumber": "AB0001", "voucherStatus": "open"},
            "orderconfirmation", "order-confirmations")
    respx.get(f"{API}/articles").mock(return_value=page(ARTICLE))
    post = _target("invoices")
    services.continue_document(client, "AB0001", "Rechnung", changes={
        "positionen": [{"artikelnummer": "ST3-A-001", "menge": "3 Stück"}]})
    item = sent(post)["lineItems"][0]
    assert item["id"] == U1 and item["name"] == "Flyer A6" and item["quantity"] == 3
    assert item["unitPrice"]["netAmount"] == 8.9 and item["type"] == "material"


@respx.mock
def test_unfit_changes_refused(client: LexwareClient) -> None:
    _source(INV)
    with pytest.raises(LexwareError, match="Lieferscheinen nicht verwendet"):
        services.continue_document(client, "RE0005", "Lieferschein",
                                   changes={"zahlungsziel": 14})
    with pytest.raises(LexwareError, match="Lieferscheinen nicht verwendet"):
        services.continue_document(client, "RE0005", "Lieferschein", changes={"rabatt": 5})
    with pytest.raises(LexwareError, match="Mahnungen nicht verwendet"):
        services.continue_document(client, "RE0005", "Mahnung", changes={"zahlungsziel": 7})
    with pytest.raises(LexwareError, match="nur bei Angeboten"):
        services.continue_document(client, "RE0005", "Rechnungskorrektur",
                                   changes={"gueltig_bis": "30.11.2026"})
    with pytest.raises(LexwareError, match="Kalenderwoche"):
        services.continue_document(client, "RE0005", "Rechnungskorrektur",
                                   changes={"voucherDate": "KW 48"})


# -- 19/29: Kontakt-id mit abweichender Adresse, Einmaladresse ---------------------------


@respx.mock
def test_contact_id_with_other_name_or_part_address(client: LexwareClient) -> None:
    respx.get(f"{API}/contacts/{U2}").mock(return_value=ok(CONTACT))
    with pytest.raises(LexwareError, match="passt nicht zum Kontakt Huber GmbH"):
        services.create_invoice(client, {"address": {"contactId": U2, "name": "Ganz anderer "
                                                     "Name AG", "countryCode": "DE"},
                                         "lineItems": [LINE]})
    post = respx.post(f"{API}/invoices").mock(return_value=ok({"id": U3}))
    respx.get(f"{API}/invoices/{U3}").mock(return_value=ok({
        "address": {"contactPerson": "Frau Sabine Huber"}}))
    result = services.create_invoice(client, {"address": {"contactId": U2,
                                                          "supplement": "z. Hd. Einkauf"},
                                              "ansprechpartner": "Max Gruber",
                                              "lineItems": [LINE]})
    address = sent(post)["address"]
    assert address["name"] == "Huber GmbH" and address["street"] == "Ring 1"
    assert address["supplement"] == "z. Hd. Einkauf" and address["contactPerson"] == "Max Gruber"
    assert "Frau Sabine Huber" in result["hinweis"]


@respx.mock
def test_one_off_address_with_customer_name(client: LexwareClient) -> None:
    respx.get(f"{API}/contacts").mock(return_value=page(CONTACT))
    respx.post(f"{API}/invoices").mock(return_value=ok({"id": U3}))
    respx.get(f"{API}/invoices/{U3}").mock(return_value=ok({}))
    result = services.create_invoice(client, {"address": {
        "name": "Huber GmbH", "street": "Werk 2", "zip": "50668", "city": "Köln"},
        "lineItems": [LINE]})
    assert "Kd. 10052" in result["hinweis"] and "nicht mit ihm verknüpft" in result["hinweis"]


@respx.mock
def test_changes_address_part_merges_with_source(client: LexwareClient) -> None:
    _source(INV)
    respx.get(f"{API}/contacts/{U2}").mock(return_value=ok(CONTACT))
    post = _target("credit-notes")
    services.continue_document(client, "RE0005", "Gutschrift",
                               changes={"address": {"supplement": "Buchhaltung"}})
    address = sent(post)["address"]
    assert address["contactId"] == U2 and address["supplement"] == "Buchhaltung"
    assert address["street"] == "Ring 1"


# -- 20/21: Kontakt-Dubletten, Privatperson -------------------------------------------------


@respx.mock
def test_contact_duplicate_refused(client: LexwareClient) -> None:
    respx.get(f"{API}/contacts").mock(return_value=page({**CONTACT, "company": {
        "name": "Müller GmbH"}}))
    post = respx.post(f"{API}/contacts").mock(return_value=ok({"id": U3}))
    respx.get(f"{API}/contacts/{U3}").mock(return_value=ok({"id": U3}))
    with pytest.raises(LexwareError, match=r"Kd\. 10052 \(Köln\)"):
        services.create_contact(client, {"firma": "Mueller GmbH"})
    assert not post.called
    services.create_contact(client, {"firma": "Mueller GmbH", "allow_duplicate": "ja"})
    assert "allow_duplicate" not in sent(post)


@respx.mock
def test_two_contacts_with_the_same_name(client: LexwareClient) -> None:
    other = {**CONTACT, "id": U3, "roles": {"customer": {"number": 10075}},
             "addresses": {"billing": [{"city": "Bonn"}]}}
    respx.get(f"{API}/contacts").mock(return_value=page(CONTACT, other))
    with pytest.raises(LexwareError, match="Mehrere Kontakte heißen genau 'Huber GmbH'"):
        services.resolve_contact_id(client, "Huber GmbH")


def test_private_person_or_company() -> None:
    with pytest.raises(LexwareError, match="Firma oder eine Privatperson"):
        normalize_contact({"name": "Peter Lang"})
    body = normalize_contact({"name": "Peter Lang", "typ": "Privatperson"})
    assert body["person"] == {"firstName": "Peter", "lastName": "Lang"}
    assert normalize_contact({"name": "Peter Lang", "art": "Firma"})["company"]["name"] == (
        "Peter Lang")
    assert normalize_contact({"name": "Lang Druck GmbH"})["company"]["name"] == "Lang Druck GmbH"
    assert normalize_contact({"name": "Herr Peter Lang"})["person"]["salutation"] == "Herr"


# -- 22/23: Listen -------------------------------------------------------------------------


@respx.mock
def test_list_vouchers_default_includes_unchecked(client: LexwareClient) -> None:
    route = respx.get(f"{API}/voucherlist").mock(return_value=page())
    services.list_vouchers(client)
    assert "unchecked" in route.calls[0].request.url.params["voucherStatus"]


@respx.mock
def test_list_contacts_both_spellings(client: LexwareClient) -> None:
    def contacts(request: httpx.Request) -> httpx.Response:
        name = request.url.params.get("name")
        if name == "mueller":
            return page({"id": "a", "company": {"name": "Mueller GmbH"}})
        if name == "müller":
            return page({"id": "b", "company": {"name": "Müller GmbH"}})
        return page()

    respx.get(f"{API}/contacts").mock(side_effect=contacts)
    ids = [c["id"] for c in services.list_contacts(client, name="mueller").items]
    assert ids == ["a", "b"]
    ids = [c["id"] for c in services.list_contacts(client, name="müller").items]
    assert sorted(ids) == ["a", "b"]


# -- 24: ferne Zukunft, Gutschrift vor Rechnung -------------------------------------------


def test_far_future_dates_refused() -> None:
    with pytest.raises(LexwareError, match="mehr als ein Jahr in der Zukunft"):
        prepare_document(None, {"voucherDate": "01.01.2099", "lineItems": [LINE]}, INVOICE)
    year = _now().year + 2
    with pytest.raises(LexwareError, match="sofort gebucht"):
        prepare_voucher({"type": "Quittung", "voucherNumber": "1", "betrag": 5,
                         "kategorie": "Porto", "datum": f"01.03.{year}"})
    soon = (_now() + timedelta(days=30)).date().isoformat()
    assert prepare_document(None, {"voucherDate": soon, "lineItems": [LINE]}, INVOICE)


@respx.mock
def test_credit_note_before_invoice_date(client: LexwareClient) -> None:
    _source(INV)
    with pytest.raises(LexwareError, match="nicht vor dem Datum der Rechnung"):
        services.continue_document(client, "RE0005", "Gutschrift",
                                   changes={"voucherDate": "15.10.2026"})


# -- 25/26: Steuerarten, Übersetzungen ------------------------------------------------------


def test_tax_type_words_and_rate_words() -> None:
    for word, kind in (("innergemeinschaftlich", "intraCommunitySupply"),
                       ("EU-Lieferung", "intraCommunitySupply"),
                       ("Ausfuhr", "thirdPartyCountryDelivery"),
                       ("Kleinunternehmer", "vatfree"),
                       ("Bauleistung", "constructionService13b")):
        out = prepare_document(None, {"taxConditions": word, "lineItems": [LINE]}, INVOICE)
        assert out["taxConditions"]["taxType"] == kind
    for word in ("Drittland", "13b", "Reverse Charge"):
        with pytest.raises(LexwareError, match="mehrdeutig"):
            prepare_document(None, {"steuerart": word, "lineItems": [LINE]}, INVOICE)
    out = prepare_document(None, {"lineItems": [{"name": "Buch", "preis": 10,
                                                 "steuersatz": "ermäßigt"}]}, INVOICE)
    assert out["lineItems"][0]["unitPrice"]["taxRatePercentage"] == 7
    out = prepare_voucher({"type": "Quittung", "voucherNumber": "1", "betrag": 11.9,
                           "kategorie": "x", "mwst": "regulär"})
    assert out["voucherItems"][0]["taxRatePercent"] == 19


@respx.mock
def test_special_tax_type_error_names_reason_and_prerequisites(client: LexwareClient) -> None:
    respx.post(f"{API}/invoices").mock(return_value=httpx.Response(406, json={
        "message": "Invalid combination of tax type intraCommunitySupply and contact."}))
    with pytest.raises(LexwareError) as exc:
        services.create_invoice(client, {"address": {"name": "X", "countryCode": "AT"},
                                         "steuerart": "intraCommunitySupply",
                                         "lineItems": [LINE]})
    text = str(exc.value)
    assert "Invalid combination of tax type intraCommunitySupply" in text
    assert "USt-IdNr." in text and not text.startswith("HTTP")


def test_translations_and_field_positions() -> None:
    from lxw_cli.core.client import _format_validation_details, _translate_text

    for raw, german in (
        ("Referenced contact needs to have role customer.", "kein Kunde"),
        ("Referenced contact is required for tax type intraCommunitySupply.",
         "an einen Kontakt"),
        ("Name and countryCode are required address fields.", "Name und Ländercode"),
        ("due date before voucher date", "Fälligkeit liegt vor dem Belegdatum"),
        ("Invalid value 'ja' for 'optional'. Value of type Boolean expected.", "ja/nein"),
        ("vat registration id unknown country code", "Ländercode"),
        ("Feld „addresses.billing.size“: invalid value", "nur einen Eintrag"),
    ):
        assert german in _translate_text(raw, 406)
    text = _format_validation_details([{"field": "lineItems[2].unitPrice.netAmount",
                                        "message": "must not be null"}])
    assert text.startswith("Position 3 (unitPrice.netAmount)")
    assert str(LexwareAPIError(406, "Bitte prüfen.")) == "Bitte prüfen."
    assert str(LexwareAPIError(406, "Not acceptable")).startswith("HTTP 406")


# -- 27/28: Booleans, Standardtexte anderer Belegarten --------------------------------------


def test_boolean_words() -> None:
    out = prepare_document(None, {"lineItems": [{**LINE, "optional": "nein"}]}, QUOTATION)
    assert out["lineItems"][0]["optional"] is False
    body = normalize_contact({"firma": "A GmbH", "kunde": "ja", "lieferant": "nein"})
    assert body["roles"] == {"customer": {}}
    body = normalize_contact({"firma": "A GmbH", "roles": {"customer": "ja", "vendor": "nein"},
                              "ansprechpartner": {"nachname": "B", "primary": "ja"}})
    assert body["roles"] == {"customer": {}}
    assert body["company"]["contactPersons"][0]["primary"] is True


def test_quotation_texts_dropped_on_invoice() -> None:
    out = prepare_document(None, {"title": "Angebot", "introduction": "Gerne bieten wir Ihnen "
                                  "an:", "remark": "Danke!", "lineItems": [LINE]}, INVOICE)
    assert "title" not in out and "introduction" not in out and out["remark"] == "Danke!"
    assert "anderen Belegart" in out["__hinweis"]
    out = prepare_document(None, {"title": "Rechnung", "lineItems": [LINE]}, INVOICE)
    assert out["title"] == "Rechnung"


# -- 30: Buchhaltungsbelege ---------------------------------------------------------------

CATEGORIES = [
    {"id": "c1", "name": "Kraftstoff/Ladestrom", "groupName": "Kfz", "type": "outgo"},
    {"id": "c2", "name": "Bürobedarf", "groupName": "Büro", "type": "outgo"},
    {"id": "c3", "name": "Computer", "groupName": "Anlagevermögen", "type": "outgo"},
    {"id": "c4", "name": "Maschinen", "groupName": "Anlagevermögen", "type": "outgo"},
    {"id": "c5", "name": "Erlöse", "groupName": "Einnahmen", "type": "income"},
]


@respx.mock
def test_voucher_hints_and_category(client: LexwareClient) -> None:
    respx.get(f"{API}/posting-categories").mock(return_value=ok(CATEGORIES))
    respx.get(f"{API}/voucherlist").mock(return_value=page())
    respx.get(f"{API}/contacts").mock(return_value=page(
        {"id": U2, "company": {"name": "Lieferant ST3-T Druckerei Weber GmbH"},
         "roles": {"vendor": {"number": 70012}}}))
    post = respx.post(f"{API}/vouchers").mock(return_value=ok({"id": U3}))
    result = services.create_voucher(client, {"type": "Quittung", "voucherNumber": "Q-1",
                                              "betrag": 23.8, "kategorie": "Tank",
                                              "lieferant": "Druckerei Weber"})
    body = sent(post)
    assert body["voucherItems"][0]["categoryId"] == "c1"
    assert body.get("useCollectiveContact") is True
    assert result["kategorie"] == "Kraftstoff/Ladestrom"
    assert "Lief. 70012" in result["hinweis"] and "Barzahlung" in result["hinweis"]
    with pytest.raises(LexwareError, match="Gruppe"):
        services.create_voucher(client, {"type": "Eingangsrechnung", "voucherNumber": "E-1",
                                         "betrag": 119, "kategorie": "Anlagevermögen"})


@respx.mock
def test_generic_credit_note_and_category_search(client: LexwareClient) -> None:
    respx.get(f"{API}/posting-categories").mock(return_value=ok(CATEGORIES))
    respx.get(f"{API}/voucherlist").mock(return_value=page())
    post = respx.post(f"{API}/vouchers").mock(return_value=ok({"id": U3}))
    services.create_voucher(client, {"type": "Gutschrift", "voucherNumber": "G-1",
                                     "betrag": 11.9, "kategorie": "Bürobedarf"})
    assert sent(post)["type"] == "purchasecreditnote"
    out = prepare_voucher({"type": "Gutschrift", "lieferant": "X", "voucherNumber": "G-2",
                           "betrag": 5, "kategorie": "c2"})
    assert out["type"] == "purchasecreditnote"
    assert [c["id"] for c in services.list_posting_categories(client, search="Tank")] == ["c1"]


def test_voucher_paid_status_and_category_on_items() -> None:
    with pytest.raises(LexwareError, match="nicht als bezahlt"):
        prepare_voucher({"type": "Quittung", "voucherNumber": "1", "betrag": 5,
                         "kategorie": "x", "voucherStatus": "bezahlt"})
    out = prepare_voucher({"type": "Eingangsrechnung", "voucherNumber": "1",
                           "kategorie": "Bürobedarf", "taxType": "gross", "voucherItems": [
                               {"amount": 119, "taxRatePercent": 19},
                               {"amount": 107, "taxRatePercent": 7, "categoryId": "y"}]})
    assert [i["categoryId"] for i in out["voucherItems"]] == ["Bürobedarf", "y"]


# -- 31: Artikelnummer vor Titel -------------------------------------------------------------


@respx.mock
def test_article_number_beats_title(client: LexwareClient) -> None:
    respx.get(f"{API}/articles").mock(return_value=page(
        {**ARTICLE, "id": U2, "articleNumber": "ST3-A-009", "title": "ST3-A-002"},
        {**ARTICLE, "id": U3, "articleNumber": "ST3-A-002", "title": "Plakat"}))
    out = prepare_document(client, {"lineItems": [{"artikelnummer": "ST3-A-002"}]}, INVOICE)
    assert out["lineItems"][0]["id"] == U3
    with pytest.raises(LexwareError, match="Ähnliche Artikel: .*Plakat"):
        prepare_document(client, {"lineItems": [{"artikel": "Plak"}]}, INVOICE)


# -- 32: Kleinere Meldungen ------------------------------------------------------------------


@respx.mock
def test_small_messages(client: LexwareClient) -> None:
    respx.get(f"{API}/voucherlist").mock(return_value=page(
        {"id": U1, "voucherNumber": "AG0005", "voucherType": "quotation",
         "voucherStatus": "draft"}))
    with pytest.raises(LexwareError, match="AG0005 ist ein Angebot,"):
        services.download_pdf(client, "AG0005", services.INVOICE_PDF_TYPES)
    with pytest.raises(LexwareError, match="download_quotation_pdf"):
        services.download_pdf(client, "AG0005", "deliverynote")
    with pytest.raises(LexwareError, match="beim Abschließen|wenn er in Lexware"):
        services.get_bookkeeping_voucher(client, "AG0005")
    respx.get(f"{API}/quotations/{U1}").mock(return_value=ok({
        "id": U1, "voucherNumber": "AG0005", "voucherStatus": "voided", "lineItems": []}))
    with pytest.raises(LexwareError, match="^Das Angebot AG0005 ist storniert"):
        services.continue_document(client, "AG0005", "Auftrag")


def test_empty_customer_and_dates() -> None:
    with pytest.raises(LexwareError, match="Bitte einen Kunden angeben"):
        prepare_document(None, {"kunde": "", "lineItems": [LINE]}, INVOICE)
    out = prepare_document(None, {"dueDate": "in 14 Tagen", "lineItems": [LINE]}, INVOICE)
    assert out["paymentConditions"]["paymentTermDuration"] == 14
    with pytest.raises(LexwareError, match="Fälligkeit 'irgendwann' nicht lesbar"):
        prepare_document(None, {"dueDate": "irgendwann", "lineItems": [LINE]}, INVOICE)
    out = prepare_document(None, {"lieferdatum": "nächsten Montag", "lineItems": [LINE]},
                           INVOICE)
    day = out["shippingConditions"]["shippingDate"][:10]
    assert day > _now().date().isoformat()
    from datetime import date

    assert date.fromisoformat(day).weekday() == 0
    out = prepare_document(None, {"lieferdatum": "Monatsende", "lineItems": [LINE]}, INVOICE)
    assert (date.fromisoformat(out["shippingConditions"]["shippingDate"][:10])
            + timedelta(days=1)).day == 1


@respx.mock
def test_date_range_tokens(client: LexwareClient) -> None:
    route = respx.get(f"{API}/voucherlist").mock(return_value=page())
    services.list_invoices(client, date_from="November 2026", date_to="2026-12")
    params = route.calls[0].request.url.params
    assert params["voucherDateFrom"] == "2026-11-01" and params["voucherDateTo"] == "2026-12-31"
    with pytest.raises(LexwareError, match="verdreht"):
        services.list_invoices(client, date_from="30.11.2026", date_to="01.11.2026")


@respx.mock
def test_payment_status_draft_and_credit_offset(client: LexwareClient) -> None:
    respx.get(f"{API}/voucherlist").mock(side_effect=lambda r: page(
        {"id": U1, "voucherNumber": "RE0007", "voucherType": "invoice",
         "voucherStatus": "draft", "totalAmount": 50})
        if r.url.params.get("voucherNumber") == "RE0007" else page(
        {"id": U2, "voucherNumber": "RE0008", "voucherType": "invoice",
         "voucherStatus": "paidoff"}))
    respx.get(f"{API}/payments/{U1}").mock(return_value=httpx.Response(406, json={
        "message": "voucher is in draft state"}))
    status = services.get_payment_status(client, "RE0007")
    assert status["openAmount"] is None and status["totalAmount"] == 50
    respx.get(f"{API}/payments/{U2}").mock(return_value=ok({
        "paymentStatus": "balanced", "voucherStatus": "paidoff", "openAmount": 0}))
    respx.get(f"{API}/invoices/{U2}").mock(return_value=ok({"relatedVouchers": [
        {"id": "g", "voucherNumber": "GS0003", "voucherType": "creditnote"}]}))
    assert "GS0003" in services.get_payment_status(client, "RE0008")["hinweis"]


def test_names_cleaned_rounding_and_nesting() -> None:
    body = normalize_contact({"company": {"name": "Müller‮ GmbH\n& Co"}})
    assert body["company"]["name"] == "Müller GmbH & Co"
    out = prepare_document(None, {"body": {"lineItems": [{"name": "A\tB", "preis": 1.234567,
                                                          "menge": 1}]}}, INVOICE)
    item = out["lineItems"][0]
    assert item["name"] == "A B" and item["unitPrice"]["netAmount"] == 1.2346
    assert "gerundet" in out["__hinweis"]
    patch = prepare_changes({"changes": {"title": "X"}}, INVOICE, "net")
    assert patch == {"title": "X"}
    assert strip_control("a⁦b") == "ab"


@respx.mock
def test_expired_quotation_and_eu_hint(client: LexwareClient) -> None:
    quote = {**INV, "voucherNumber": "AG0003", "voucherStatus": "accepted",
             "expirationDate": "2026-01-31T00:00:00.000+01:00"}
    _source(quote, "quotation", "quotations")
    _target("invoices")
    result = services.continue_document(client, "AG0003", "Rechnung")
    assert "war nur bis 31.01.2026 gültig" in result["hinweis"]
    respx.get(f"{API}/contacts/{U2}").mock(return_value=ok({
        **CONTACT, "company": {"name": "Wien GmbH", "vatRegistrationId": "ATU12345678"},
        "addresses": {"billing": [{"countryCode": "AT", "city": "Wien"}]}}))
    respx.post(f"{API}/invoices").mock(return_value=ok({"id": U4}))
    respx.get(f"{API}/invoices/{U4}").mock(return_value=ok({}))
    result = services.create_invoice(client, {"address": {"contactId": U2},
                                              "lineItems": [LINE]})
    assert "intraCommunitySupply" in result["hinweis"]


@respx.mock
def test_dunning_searches_outgoing_invoices_first(client: LexwareClient) -> None:
    rows = [*ROWS, {"id": "x4", "voucherNumber": "ST2-V-ER-4", "voucherType": "salesinvoice",
                    "voucherStatus": "open"},
            {"id": U4, "voucherNumber": "RE0004", "voucherType": "invoice",
             "voucherStatus": "open"}]
    respx.get(f"{API}/voucherlist").mock(side_effect=_numbers(rows))
    respx.get(f"{API}/invoices/{U4}").mock(return_value=ok({**INV, "id": U4,
                                                            "voucherNumber": "RE0004"}))
    post = respx.post(f"{API}/dunnings").mock(return_value=ok({"id": U3}))
    respx.get(f"{API}/dunnings/{U3}").mock(return_value=ok({"id": U3}))
    services.create_dunning(client, "Rechnung 4")
    assert post.calls[0].request.url.params["precedingSalesVoucherId"] == U4
    with pytest.raises(LexwareError, match="AG0001 ist ein Angebot"):
        services.create_dunning(client, "AG0001")
