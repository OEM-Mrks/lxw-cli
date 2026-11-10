"""0.9.11: Belegnummern toleranter auflösen, Zahlungsstatus von Rechnungskorrekturen.

Anlass Protokoll 09./10.11.2026: Floor-Tek fragte '2616' statt 'RE2616' ab,
'RE2616' gab es zweimal, und der Zahlungsstatus einer Rechnungskorrektur
endete mit HTTP 406 (nachgestellt in Demo: GS0001 zu RE0006).
"""

from __future__ import annotations

import httpx
import pytest
import respx

from lxw_cli.core.client import LexwareClient
from lxw_cli.core.constants import ALL_VOUCHER_TYPES, INVOICE_LIKE_TYPES
from lxw_cli.core.errors import LexwareAPIError, LexwareError
from lxw_cli.core.services import (
    find_voucher,
    get_payment_status,
    get_voucher,
    resolve_voucher_id,
)

BASE = "https://api.lexware.io"
GS_ID = "e3334457-3ea0-4f29-90c7-f02bd6b4d14f"
RE_ID = "b0dc36ec-7a88-4e24-bc0d-23c43f0fa170"


def _page(*rows: dict) -> httpx.Response:
    return httpx.Response(200, json={"content": list(rows), "last": True})


def _voucherlist(by_number: dict[str, list[dict]], sample: list[dict] | None = None):
    """Mock /v1/voucherlist: exact (case-insensitive) number match, else sample."""

    def handler(request: httpx.Request) -> httpx.Response:
        number = request.url.params.get("voucherNumber")
        if number is None:
            return _page(*(sample or []))
        for key, rows in by_number.items():
            if key.lower() == number.lower():
                return _page(*rows)
        return _page()

    return respx.get(f"{BASE}/v1/voucherlist").mock(side_effect=handler)


def _row(id_: str, number: str, vtype: str, status: str = "open", **extra) -> dict:
    return {"id": id_, "voucherNumber": number, "voucherType": vtype,
            "voucherStatus": status, "voucherDate": "2026-11-10T00:00:00.000+01:00",
            **extra}


# -- Nummer ohne Präfix -------------------------------------------------------


@respx.mock
def test_digits_only_number_gets_account_prefix(client: LexwareClient) -> None:
    _voucherlist(
        {"RE2616": [_row("u-2616", "RE2616", "invoice")]},
        sample=[_row("a", "RE2610", "invoice"), _row("b", "RE2611", "invoice")],
    )
    match = find_voucher(client, "2616", INVOICE_LIKE_TYPES)
    assert match.id == "u-2616"
    assert "als RE2616 gelesen" in (match.note or "")


@respx.mock
def test_digits_only_takes_over_zero_padding(client: LexwareClient) -> None:
    _voucherlist(
        {"RE0006": [_row("u6", "RE0006", "invoice")]},
        sample=[_row("a", "RE0005", "invoice"), _row("b", "GS0001", "creditnote")],
    )
    assert resolve_voucher_id(client, "6", ALL_VOUCHER_TYPES) == "u6"


@respx.mock
def test_number_with_inner_space(client: LexwareClient) -> None:
    _voucherlist({"RE0006": [_row("u6", "RE0006", "invoice")]})
    assert resolve_voucher_id(client, "RE 0006", INVOICE_LIKE_TYPES) == "u6"


@respx.mock
def test_not_found_names_types_and_examples(client: LexwareClient) -> None:
    _voucherlist({}, sample=[_row("a", "RE0005", "invoice"), _row("b", "GS0001", "creditnote")])
    with pytest.raises(LexwareError) as err:
        find_voucher(client, "iFiNPI220722142501", ALL_VOUCHER_TYPES)
    text = str(err.value)
    assert "Kein Beleg mit Nummer 'iFiNPI220722142501'" in text
    assert "Rechnungskorrektur" in text and "salesinvoice" not in text
    assert "RE0005" in text and "GS0001" in text
    assert "Transaktions" in text


# -- dieselbe Nummer mehrfach -------------------------------------------------


@respx.mock
def test_same_number_prefers_invoicing_document(client: LexwareClient) -> None:
    _voucherlist({"RE2616": [
        _row("bk", "RE2616", "purchaseinvoice", contactName="Lieferant GmbH"),
        _row("inv", "RE2616", "invoice", contactName="Kunde AG"),
    ]})
    respx.get(f"{BASE}/v1/invoices/inv").mock(
        return_value=httpx.Response(200, json={"id": "inv", "voucherNumber": "RE2616"})
    )
    data = get_voucher(client, "RE2616")
    assert data["id"] == "inv"
    assert "2-mal" in data["hinweis"] and "id bk" in data["hinweis"]


@respx.mock
def test_same_number_prefers_not_voided(client: LexwareClient) -> None:
    _voucherlist({"RE1": [
        _row("old", "RE1", "invoice", status="voided"),
        _row("new", "RE1", "invoice", status="open"),
    ]})
    assert find_voucher(client, "RE1", INVOICE_LIKE_TYPES).id == "new"


@respx.mock
def test_same_number_tie_lists_candidates(client: LexwareClient) -> None:
    _voucherlist({"RE1": [
        _row("one", "RE1", "invoice", contactName="A"),
        _row("two", "RE1", "invoice", contactName="B"),
    ]})
    with pytest.raises(LexwareError) as err:
        find_voucher(client, "RE1", INVOICE_LIKE_TYPES)
    assert "id one" in str(err.value) and "id two" in str(err.value)


@respx.mock
def test_bookkeeping_preferred_for_attachments(client: LexwareClient) -> None:
    _voucherlist({"RE2616": [
        _row("inv", "RE2616", "invoice"),
        _row("bk", "RE2616", "salesinvoice"),
    ]})
    from lxw_cli.core.services import BOOKKEEPING_TYPES

    match = find_voucher(client, "RE2616", ALL_VOUCHER_TYPES, prefer=BOOKKEEPING_TYPES)
    assert match.id == "bk"


# -- Zahlungsstatus Rechnungskorrektur ---------------------------------------

CREDIT_NOTE = {
    "id": GS_ID, "voucherStatus": "paidoff", "voucherNumber": "GS0001",
    "totalPrice": {"currency": "EUR", "totalGrossAmount": 1020.16},
    "relatedVouchers": [{"id": RE_ID, "voucherNumber": "RE0006", "voucherType": "invoice"}],
}


@respx.mock
def test_payment_status_of_credit_note_by_number(client: LexwareClient) -> None:
    _voucherlist({"GS0001": [_row(GS_ID, "GS0001", "creditnote", status="paidoff",
                                  totalAmount=1020.16, currency="EUR")]})
    payments = respx.get(f"{BASE}/v1/payments/{GS_ID}")
    respx.get(f"{BASE}/v1/credit-notes/{GS_ID}").mock(
        return_value=httpx.Response(200, json=CREDIT_NOTE)
    )
    data = get_payment_status(client, "GS0001")
    assert not payments.called
    assert data["paymentStatus"] == "balanced"
    assert data["openAmount"] == 0
    assert data["totalAmount"] == 1020.16
    assert data["relatedVouchers"][0]["voucherNumber"] == "RE0006"
    assert "RE0006" in data["hinweis"]


@respx.mock
def test_payment_status_of_credit_note_by_uuid_after_406(client: LexwareClient) -> None:
    respx.get(f"{BASE}/v1/payments/{GS_ID}").mock(
        return_value=httpx.Response(406, json={
            "message": "No payment information for this voucher/voucher type creditnote"
        })
    )
    respx.get(f"{BASE}/v1/credit-notes/{GS_ID}").mock(
        return_value=httpx.Response(200, json={**CREDIT_NOTE, "voucherStatus": "open"})
    )
    data = get_payment_status(client, GS_ID)
    assert data["paymentStatus"] == "open"
    assert data["openAmount"] is None
    assert "offen" in data["hinweis"]


@respx.mock
def test_payment_status_other_406_is_german(client: LexwareClient) -> None:
    uuid = "11111111-2222-4333-8444-555555555555"
    respx.get(f"{BASE}/v1/payments/{uuid}").mock(
        return_value=httpx.Response(406, json={
            "message": "No payment information for this voucher/voucher type quotation"
        })
    )
    with pytest.raises(LexwareError, match="Angebot"):
        get_payment_status(client, uuid)


@respx.mock
def test_payment_status_quotation_by_number_without_api_call(client: LexwareClient) -> None:
    _voucherlist({"AG0001": [_row("q1", "AG0001", "quotation")]})
    payments = respx.get(f"{BASE}/v1/payments/q1")
    with pytest.raises(LexwareError, match="Für Angebote gibt es"):
        get_payment_status(client, "AG0001")
    assert not payments.called


@respx.mock
def test_payment_status_prefers_invoice_over_quotation_same_number(client: LexwareClient) -> None:
    _voucherlist({"2026-17": [_row("q", "2026-17", "quotation"), _row("i", "2026-17", "invoice")]})
    respx.get(f"{BASE}/v1/payments/i").mock(
        return_value=httpx.Response(200, json={"paymentStatus": "open", "openAmount": 5})
    )
    data = get_payment_status(client, "2026-17")
    assert data["openAmount"] == 5 and "2-mal" in data["hinweis"]


@respx.mock
def test_payment_status_other_errors_pass_through(client: LexwareClient) -> None:
    uuid = "11111111-2222-4333-8444-555555555555"
    respx.get(f"{BASE}/v1/payments/{uuid}").mock(return_value=httpx.Response(500, json={}))
    with pytest.raises(LexwareAPIError):
        get_payment_status(client, uuid)


# -- 403 ohne Rechte ----------------------------------------------------------


@respx.mock
def test_access_denied_is_german(client: LexwareClient) -> None:
    respx.post(f"{BASE}/v1/vouchers").mock(return_value=httpx.Response(403, json={
        "i18nKey": "access_denied",
        "source": "Insufficient scope or user access rights for this resource.",
        "type": "user_not_authorized",
    }))
    with pytest.raises(LexwareAPIError) as err:
        client.post("/v1/vouchers", {})
    assert err.value.status_code == 403
    assert "Keine Berechtigung" in str(err.value) and "Tarif" in str(err.value)
