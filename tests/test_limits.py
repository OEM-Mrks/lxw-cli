from __future__ import annotations

import httpx
import pytest
import respx

from lxw_cli.core import services
from lxw_cli.core.client import LexwareClient
from lxw_cli.core.errors import LexwareError
from lxw_cli.core.limits import (
    validate_article,
    validate_contact,
    validate_voucher,
)

# -- Voucher --------------------------------------------------------------


def test_voucher_title_over_25_raises() -> None:
    with pytest.raises(LexwareError) as exc:
        validate_voucher({"title": "x" * 26})
    assert "Belegtitel" in str(exc.value)
    assert "25" in str(exc.value)


def test_voucher_title_exactly_25_ok() -> None:
    validate_voucher({"title": "x" * 25})  # no raise


def test_voucher_introduction_and_remark_limit_2000() -> None:
    validate_voucher({"introduction": "x" * 2000, "remark": "y" * 2000})
    with pytest.raises(LexwareError):
        validate_voucher({"introduction": "x" * 2001})
    with pytest.raises(LexwareError):
        validate_voucher({"remark": "y" * 2001})


def test_voucher_line_item_name_100_and_description_2000() -> None:
    validate_voucher(
        {"lineItems": [{"name": "n" * 100, "description": "d" * 2000}]}
    )
    with pytest.raises(LexwareError) as exc:
        validate_voucher({"lineItems": [{"name": "n" * 101}]})
    assert "Position 1" in str(exc.value)


def test_voucher_reports_the_offending_position_number() -> None:
    with pytest.raises(LexwareError) as exc:
        validate_voucher(
            {
                "lineItems": [
                    {"name": "ok"},
                    {"name": "d" * 2001, "description": "d" * 2001},
                ]
            }
        )
    assert "Position 2" in str(exc.value)


def test_voucher_multiple_violations_listed_together() -> None:
    with pytest.raises(LexwareError) as exc:
        validate_voucher({"title": "x" * 30, "remark": "y" * 2001})
    message = str(exc.value)
    assert "Belegtitel" in message
    assert "Nachbemerkung" in message


# -- Article --------------------------------------------------------------


def test_article_title_limit_100_not_25() -> None:
    validate_article({"title": "x" * 100})  # article title may be 100
    with pytest.raises(LexwareError) as exc:
        validate_article({"title": "x" * 101})
    assert "Artikelbezeichnung" in str(exc.value)


def test_article_description_limit_2000() -> None:
    validate_article({"description": "x" * 2000})
    with pytest.raises(LexwareError):
        validate_article({"description": "x" * 2001})


# -- Contact --------------------------------------------------------------


def test_contact_note_limit_1000() -> None:
    validate_contact({"note": "x" * 1000})
    with pytest.raises(LexwareError) as exc:
        validate_contact({"note": "x" * 1001})
    assert "Notiz" in str(exc.value)


def test_contact_person_salutation_limit_25() -> None:
    validate_contact({"person": {"salutation": "x" * 25}})
    with pytest.raises(LexwareError) as exc:
        validate_contact({"person": {"salutation": "x" * 26}})
    assert "Anrede" in str(exc.value)


def test_company_contact_person_salutation_checked() -> None:
    with pytest.raises(LexwareError):
        validate_contact(
            {"company": {"contactPersons": [{"salutation": "x" * 26}]}}
        )


def test_empty_or_missing_fields_pass() -> None:
    validate_voucher({})
    validate_article({})
    validate_contact({})
    validate_voucher({"title": None, "lineItems": []})


# -- Wired into the service layer (fails before any API call) --------------


@respx.mock
def test_create_invoice_rejects_long_title_without_posting(
    client: LexwareClient,
) -> None:
    route = respx.post("https://api.lexware.io/v1/invoices").mock(
        return_value=httpx.Response(201, json={})
    )
    with pytest.raises(LexwareError):
        services.create_invoice(client, {"title": "x" * 40})
    assert not route.called  # validation happened before the request


@respx.mock
def test_create_article_rejects_long_description_without_posting(
    client: LexwareClient,
) -> None:
    route = respx.post("https://api.lexware.io/v1/articles").mock(
        return_value=httpx.Response(201, json={})
    )
    with pytest.raises(LexwareError):
        services.create_article(client, {"title": "ok", "description": "d" * 2001})
    assert not route.called
