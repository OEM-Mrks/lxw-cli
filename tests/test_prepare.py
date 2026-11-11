"""Defaults and repairs applied to bodies before they reach Lexware (v0.9.8).

Each case mirrors a rejection seen in production (Nov 2026): plain dates,
missing totalPrice/taxConditions/expirationDate, article line items without id
or with the wrong type, voucher tax rounding, a delivery date on a purchase
voucher and paging past Lexware's search window.
"""

from __future__ import annotations

import json
from datetime import datetime

import httpx
import pytest
import respx

from lxw_cli.core import services
from lxw_cli.core.client import LexwareClient
from lxw_cli.core.errors import LexwareError
from lxw_cli.core.prepare import (
    INVOICE,
    QUOTATION,
    document_timestamp,
    prepare_document,
    prepare_voucher,
)

_API = "https://api.lexware.io"
_ART_ID = "f206c866-40e1-4d49-8973-43f1554e648d"
_CONTACT = "927a7696-576c-4bf1-837d-73465fa0cff4"


def _sent(route: respx.Route) -> dict:
    return json.loads(route.calls.last.request.content)


# -- dates --------------------------------------------------------------------


@pytest.mark.parametrize("value", ["2026-11-04", "04.11.2026", "4.11.2026"])
def test_plain_day_becomes_lexware_timestamp(value: str) -> None:
    out = document_timestamp(value)
    assert out.startswith("2026-11-04T00:00:00.000")
    assert datetime.fromisoformat(out).utcoffset() is not None


def test_timestamp_without_millis_or_offset_is_completed() -> None:
    out = document_timestamp("2026-11-04T10:30:00")
    assert out.startswith("2026-11-04T10:30:00.000")
    # Day and time as written (Berlin) — a "Z" must not move the date
    # (Stresstest 10.11.: 23:30Z became the next day).
    assert document_timestamp("2026-11-04T10:30:00Z").startswith("2026-11-04T10:30:00.000")
    assert document_timestamp("2026-11-10T23:30:00Z").startswith("2026-11-10T23:30")


def test_unparseable_date_is_left_for_lexware() -> None:
    assert document_timestamp("irgendwann") == "irgendwann"
    assert document_timestamp("morgen").startswith("20")  # relative words work
    assert document_timestamp(None) is None


# -- sales documents ----------------------------------------------------------


def _quotation(**extra) -> dict:
    return {
        "address": {"contactId": _CONTACT},
        "lineItems": [
            {"type": "custom", "name": "Beratung", "quantity": 1, "unitPrice": {"netAmount": 100}}
        ],
        **extra,
    }


def test_quotation_gets_all_defaults() -> None:
    out = prepare_document(None, _quotation(voucherDate="2026-11-04"), QUOTATION)
    assert out["voucherDate"].startswith("2026-11-04T00:00:00.000")
    assert out["expirationDate"].startswith("2026-12-04T00:00:00.000")
    assert out["totalPrice"] == {"currency": "EUR"}
    assert out["taxConditions"] == {"taxType": "net"}
    assert "shippingConditions" not in out  # quotations have none
    price = out["lineItems"][0]["unitPrice"]
    assert price == {"netAmount": 100, "currency": "EUR", "taxRatePercentage": 19}


def test_given_values_are_kept_and_input_not_mutated() -> None:
    body = _quotation(
        voucherDate="2026-11-04T09:00:00.000+01:00",
        expirationDate="2026-11-10",
        taxConditions={"taxType": "gross"},
        totalPrice={"currency": "EUR", "totalGrossAmount": 5},
    )
    body["lineItems"][0]["unitPrice"]["taxRatePercentage"] = 7
    snapshot = json.dumps(body, sort_keys=True)
    out = prepare_document(None, body, QUOTATION)
    assert json.dumps(body, sort_keys=True) == snapshot
    assert out["voucherDate"] == "2026-11-04T09:00:00.000+01:00"
    assert out["expirationDate"].startswith("2026-11-10T")
    assert out["taxConditions"] == {"taxType": "gross"}
    assert out["lineItems"][0]["unitPrice"]["taxRatePercentage"] == 7


def test_missing_voucher_date_defaults_to_now() -> None:
    from lxw_cli.core.prepare import _now

    out = prepare_document(None, _quotation(), QUOTATION)
    # Compare with the server's own clock (Europe/Berlin), not the runner's
    # local time: between 23:00 and 24:00 UTC the two dates differ.
    assert datetime.fromisoformat(out["voucherDate"]).date() == _now().date()


def test_tax_free_type_defaults_rate_to_zero() -> None:
    out = prepare_document(
        None, _quotation(taxConditions={"taxType": "vatfree"}), QUOTATION
    )
    assert out["lineItems"][0]["unitPrice"]["taxRatePercentage"] == 0


def test_invoice_gets_shipping_conditions_and_dates_normalized() -> None:
    out = prepare_document(None, _quotation(), INVOICE)
    # date of service defaults to the invoice date (§ 14 UStG), like the web app
    assert out["shippingConditions"] == {
        "shippingType": "service", "shippingDate": out["voucherDate"]
    }
    assert "expirationDate" not in out
    out = prepare_document(
        None,
        _quotation(shippingConditions={"shippingType": "service", "shippingDate": "03.11.2026"}),
        INVOICE,
    )
    assert out["shippingConditions"]["shippingDate"].startswith("2026-11-03T00:00:00.000")


def test_text_items_get_no_price_fields() -> None:
    body = _quotation()
    body["lineItems"].append({"type": "text", "name": "Hinweis", "description": "x"})
    out = prepare_document(None, body, QUOTATION)
    assert out["lineItems"][1] == {"type": "text", "name": "Hinweis", "description": "x"}


def test_article_type_without_id_becomes_custom() -> None:
    body = _quotation()
    body["lineItems"][0]["type"] = "service"
    out = prepare_document(None, body, QUOTATION)
    assert out["lineItems"][0]["type"] == "custom"


@respx.mock
def test_article_line_type_taken_from_article(client: LexwareClient) -> None:
    route = respx.get(f"{_API}/v1/articles/{_ART_ID}").mock(
        return_value=httpx.Response(200, json={"id": _ART_ID, "type": "SERVICE"})
    )
    body = _quotation()
    body["lineItems"] = [
        {"type": "material", "id": _ART_ID, "quantity": 1},
        {"type": "material", "id": _ART_ID, "quantity": 2},
    ]
    out = prepare_document(client, body, QUOTATION)
    assert [i["type"] for i in out["lineItems"]] == ["service", "service"]
    assert route.call_count == 1  # one lookup per article


@respx.mock
def test_unknown_article_id_gives_clear_error(client: LexwareClient) -> None:
    respx.get(f"{_API}/v1/articles/{_ART_ID}").mock(return_value=httpx.Response(404))
    body = _quotation()
    body["lineItems"] = [{"type": "service", "id": _ART_ID, "quantity": 1}]
    with pytest.raises(LexwareError, match="gibt es nicht"):
        prepare_document(client, body, QUOTATION)


@respx.mock
def test_create_quotation_sends_completed_body(client: LexwareClient) -> None:
    route = respx.post(f"{_API}/v1/quotations").mock(
        return_value=httpx.Response(201, json={"id": "q1"})
    )
    services.create_quotation(client, _quotation(voucherDate="2026-11-04"))
    sent = _sent(route)
    assert sent["voucherDate"].startswith("2026-11-04T")
    assert sent["totalPrice"] == {"currency": "EUR"}
    assert "expirationDate" in sent


# -- bookkeeping vouchers -----------------------------------------------------


def _voucher(tax_type: str, amount: float, rate: float = 19, **extra) -> dict:
    return {
        "type": "purchaseinvoice",
        "voucherNumber": "R-1",
        "voucherDate": "2026-11-04",
        "taxType": tax_type,
        "voucherItems": [
            {"amount": amount, "taxRatePercent": rate, "taxAmount": 0, "categoryId": "c"}
        ],
        **extra,
    }


def test_gross_voucher_tax_is_computed() -> None:
    out = prepare_voucher(_voucher("gross", 119.0, totalGrossAmount=119.0, totalTaxAmount=1))
    assert out["voucherItems"][0]["taxAmount"] == 19.0
    assert out["totalTaxAmount"] == 19.0
    assert out["totalGrossAmount"] == 119.0


def test_net_voucher_tax_is_computed_with_rounding() -> None:
    out = prepare_voucher(_voucher("net", 10.05, rate=7))
    assert out["voucherItems"][0]["taxAmount"] == 0.70  # 0.7035 → 0.70
    assert out["totalGrossAmount"] == 10.75


def test_voucher_total_that_does_not_fit_is_reported() -> None:
    with pytest.raises(LexwareError, match="Positionen ergeben 119.00"):
        prepare_voucher(_voucher("net", 100.0, totalGrossAmount=100.0))


def test_incomplete_voucher_is_left_for_lexware() -> None:
    body = _voucher("gross", 119.0)
    del body["voucherItems"][0]["taxRatePercent"]
    assert prepare_voucher(body) == body


@respx.mock
def test_voucher_shipping_date_dropped_when_lexware_refuses(client: LexwareClient) -> None:
    issue = {
        "IssueList": [
            {
                "i18nKey": "expected_no_shipping_date_for_given_voucher_type",
                "source": "shippingDate",
                "type": "validation_failure",
            }
        ]
    }
    route = respx.post(f"{_API}/v1/vouchers").mock(
        side_effect=[httpx.Response(406, json=issue), httpx.Response(201, json={"id": "v1"})]
    )
    created = services.create_voucher(client, _voucher("gross", 119.0, shippingDate="2026-11-01"))
    assert created == {"id": "v1"}
    assert route.call_count == 2
    assert "shippingDate" not in _sent(route)


@respx.mock
def test_voucher_other_406_is_not_retried(client: LexwareClient) -> None:
    issue = {"IssueList": [{"i18nKey": "invalid_value", "source": "x"}]}
    route = respx.post(f"{_API}/v1/vouchers").mock(return_value=httpx.Response(406, json=issue))
    with pytest.raises(LexwareError):
        services.create_voucher(client, _voucher("gross", 119.0, shippingDate="2026-11-01"))
    assert route.call_count == 1


# -- paging past the search window --------------------------------------------


@respx.mock
def test_paging_stops_at_search_window(client: LexwareClient) -> None:
    def page(request: httpx.Request) -> httpx.Response:
        if request.url.params["page"] == "0":
            return httpx.Response(200, json={"content": [{"id": "a"}], "last": False})
        return httpx.Response(400, json={"message": "Maximum search window size exceeded"})

    respx.get(f"{_API}/v1/voucherlist").mock(side_effect=page)
    meta: dict = {}
    items = list(client.paginate("/v1/voucherlist", meta=meta))
    assert items == [{"id": "a"}]
    assert meta["truncated"] is True


@respx.mock
def test_search_window_on_first_page_is_explained(client: LexwareClient) -> None:
    respx.get(f"{_API}/v1/voucherlist").mock(
        return_value=httpx.Response(400, json={"message": "Maximum search window size exceeded"})
    )
    with pytest.raises(LexwareError, match="enger filtern"):
        list(client.paginate("/v1/voucherlist"))
