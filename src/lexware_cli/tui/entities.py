"""Browsable entity registry for the TUI.

Each :class:`Entity` knows how to list itself and fetch one record via the core
services, plus how to project a record into table cells. Pure data — no Textual,
no I/O of its own — so it is trivially unit-testable.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from lexware_cli.core import services

if TYPE_CHECKING:
    from lexware_cli.core.client import LexwareClient
    from lexware_cli.core.models import ListResult

# Rows fetched per load in the browse view: one full API page (the server
# maximum), so each request brings in as many records as allowed. Scrolling to
# the end (or pressing m) loads the next batch.
BROWSE_LIMIT = 250


@dataclass(frozen=True)
class Entity:
    key: str
    title: str
    columns: list[str]
    # list_fn(client, limit): fetch up to `limit` records (TUI grows the limit
    # page by page for the load-more pagination).
    list_fn: Callable[[LexwareClient, int], ListResult]
    row_fn: Callable[[dict[str, Any]], list[str]]
    get_fn: Callable[[LexwareClient, str], dict[str, Any]]
    # Server-side search (API request), where the entity's endpoint offers one.
    # None → the TUI search bar only filters the already-loaded rows locally.
    search_fn: Callable[[LexwareClient, str], ListResult] | None = None


def _cell(record: dict[str, Any], key: str) -> str:
    value = record.get(key)
    return "" if value is None else str(value)


def _row(keys: list[str]) -> Callable[[dict[str, Any]], list[str]]:
    return lambda record: [_cell(record, k) for k in keys]


def contact_row(record: dict[str, Any]) -> list[str]:
    """Project a raw contact into [name, role, number, email, archived].

    Mirrors the CLI's flat contact view so both frontends present contacts the
    same way (the core stores raw API records).
    """
    company = record.get("company") or {}
    person = record.get("person") or {}
    roles = record.get("roles") or {}
    emails = (record.get("emailAddresses") or {}).get("business") or []

    if company.get("name"):
        name = company["name"]
    elif person:
        name = " ".join(
            p for p in (person.get("firstName"), person.get("lastName")) if p
        ).strip()
    else:
        name = ""

    if "customer" in roles and "vendor" in roles:
        role = "customer+vendor"
    elif "customer" in roles:
        role = "customer"
    elif "vendor" in roles:
        role = "vendor"
    else:
        role = ""

    number = (
        (roles.get("customer") or {}).get("number")
        or (roles.get("vendor") or {}).get("number")
        or ""
    )
    email = emails[0] if emails else ""
    # The API delivers numbers as ints — the row contract is list[str].
    return [name, role, str(number), str(email), _cell(record, "archived")]


def search_contacts(client: LexwareClient, query: str) -> ListResult:
    """Server-side contact search via /v1/contacts.

    Queries containing '@' search the email filter, everything else the name
    filter (the API offers no combined free-text parameter). Both require at
    least 3 characters — enforced by the caller.
    """
    if "@" in query:
        return services.list_contacts(client, email=query, limit=BROWSE_LIMIT)
    return services.list_contacts(client, name=query, limit=BROWSE_LIMIT)


_VOUCHER_COLUMNS = [
    "voucherNumber",
    "voucherDate",
    "voucherStatus",
    "contactName",
    "totalAmount",
    "currency",
]


def build_entities() -> list[Entity]:
    """The ordered list of entities shown in the TUI's sidebar."""
    return [
        Entity(
            key="invoices",
            title="Rechnungen",
            columns=_VOUCHER_COLUMNS,
            list_fn=lambda c, limit: services.list_invoices(c, limit=limit),
            row_fn=_row(_VOUCHER_COLUMNS),
            get_fn=services.get_invoice,
        ),
        Entity(
            key="contacts",
            title="Kontakte",
            columns=["name", "role", "number", "email", "archived"],
            list_fn=lambda c, limit: services.list_contacts(c, limit=limit),
            row_fn=contact_row,
            get_fn=services.get_contact,
            search_fn=search_contacts,
        ),
        Entity(
            key="vouchers",
            title="Belege",
            columns=["voucherType", *_VOUCHER_COLUMNS],
            list_fn=lambda c, limit: services.list_vouchers(c, limit=limit),
            row_fn=_row(["voucherType", *_VOUCHER_COLUMNS]),
            get_fn=services.get_voucher,
        ),
        Entity(
            key="articles",
            title="Artikel",
            columns=["type", "title", "articleNumber", "gtin", "unitName"],
            list_fn=lambda c, limit: services.list_articles(c, limit=limit),
            row_fn=_row(["type", "title", "articleNumber", "gtin", "unitName"]),
            get_fn=services.get_article,
            search_fn=lambda c, q: services.list_articles(c, search=q, limit=BROWSE_LIMIT),
        ),
        Entity(
            key="quotations",
            title="Angebote",
            columns=_VOUCHER_COLUMNS,
            list_fn=lambda c, limit: services.list_quotations(c, limit=limit),
            row_fn=_row(_VOUCHER_COLUMNS),
            get_fn=services.get_quotation,
        ),
        Entity(
            key="orders",
            title="Aufträge",
            columns=_VOUCHER_COLUMNS,
            list_fn=lambda c, limit: services.list_order_confirmations(c, limit=limit),
            row_fn=_row(_VOUCHER_COLUMNS),
            get_fn=services.get_order_confirmation,
        ),
        Entity(
            key="delivery_notes",
            title="Lieferscheine",
            columns=["voucherNumber", "voucherDate", "voucherStatus", "contactName"],
            list_fn=lambda c, limit: services.list_delivery_notes(c, limit=limit),
            row_fn=_row(
                ["voucherNumber", "voucherDate", "voucherStatus", "contactName"]
            ),
            get_fn=services.get_delivery_note,
        ),
    ]
