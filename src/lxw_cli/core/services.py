"""UI-agnostic operations on the Lexware Office API.

Pure logic shared by every frontend (CLI, MCP server, TUI): identifier
resolution, endpoint fallbacks, client-side filtering/search and the
limit/archived helpers. No Typer, no Rich, no print — functions return data and
raise :class:`LexwareError` subclasses on failure.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any

from lxw_cli.core.constants import (
    ALL_VOUCHER_TYPES,
    DEFAULT_VOUCHER_STATUSES,
    DEFAULT_VOUCHER_TYPES,
    INVOICE_LIKE_TYPES,
)
from lxw_cli.core.errors import LexwareAPIError, LexwareError
from lxw_cli.core.models import ListResult

if TYPE_CHECKING:
    from lxw_cli.core.client import LexwareClient

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def looks_like_uuid(value: str) -> bool:
    return bool(_UUID_RE.match(value.strip()))


def resolve_max_items(limit: int, fetch_all: bool = False) -> int | None:
    """Translate a --limit/--all pair into paginate()'s `max_items`.

    Returns ``None`` (fetch every page) when `fetch_all` is set or `limit` is
    <= 0; otherwise the positive `limit`. Centralized so every list command —
    CLI and MCP — interprets "give me all of them" the same way.
    """
    if fetch_all or limit <= 0:
        return None
    return limit


def archived_param(include_archived: bool) -> str | None:
    """voucherlist `archived` value.

    The /v1/voucherlist endpoint filters archived server-side: `archived=false`
    returns only non-archived vouchers, while omitting the parameter returns
    both. So we send 'false' to hide archived by default and None (dropped by
    the client) to include everything on request.
    """
    return None if include_archived else "false"


def resolve_voucher_id(
    client: LexwareClient, identifier: str, voucher_type: str
) -> str:
    """Resolve a voucher identifier (UUID or voucher number) to its UUID.

    If `identifier` already looks like a UUID it is returned unchanged.
    Otherwise the value is treated as a voucher number and looked up via
    /v1/voucherlist with the given `voucher_type` filter. Raises
    LexwareError on no match or ambiguous matches.
    """
    identifier = identifier.strip()
    if looks_like_uuid(identifier):
        return identifier
    matches = list(
        client.paginate(
            "/v1/voucherlist",
            params={
                "voucherType": voucher_type,
                "voucherStatus": DEFAULT_VOUCHER_STATUSES,
                "voucherNumber": identifier,
            },
            max_items=2,
        )
    )
    if not matches:
        raise LexwareError(
            f"Kein Beleg mit Nummer '{identifier}' (Typ: {voucher_type}) gefunden."
        )
    if len(matches) > 1:
        raise LexwareError(
            f"Mehrere Belege mit Nummer '{identifier}' gefunden — "
            "bitte die UUID direkt angeben."
        )
    return matches[0]["id"]


def get_with_voucher_fallback(client: LexwareClient, primary_path: str) -> Any:
    """Try a specific document endpoint, fall back to /v1/vouchers/{id} on 404.

    The voucherlist endpoint returns generic voucher IDs that aren't always
    available via specific resource endpoints (e.g. /v1/invoices/{id}) when
    the document was created outside the Public API. Falling back to the
    generic voucher endpoint surfaces the data either way.
    """
    try:
        return client.get(primary_path)
    except LexwareAPIError as exc:
        if exc.status_code != 404:
            raise
        voucher_id = primary_path.rstrip("/").rsplit("/", 1)[-1]
        return client.get(f"/v1/vouchers/{voucher_id}")


def get_pdf_with_voucher_fallback(
    client: LexwareClient, primary_path: str, doc_id: str
) -> bytes:
    """Download a document PDF, falling back to /v1/files/{fileId} on 404.

    Documents created via the Lexware UI (not the Public API) aren't reachable
    via /v1/invoices/{id}/file etc. — but the underlying voucher still exposes
    file IDs in `files[]`, which can be downloaded from /v1/files/{fileId}.
    """
    try:
        return client.get_binary(primary_path)
    except LexwareAPIError as exc:
        if exc.status_code != 404:
            raise
        voucher = client.get(f"/v1/vouchers/{doc_id}")
        files = voucher.get("files") or []
        if not files:
            raise LexwareAPIError(
                404, f"Kein PDF/File für Beleg {doc_id} verfügbar."
            ) from exc
        return client.get_binary(f"/v1/files/{files[0]}")


def search_articles(
    client: LexwareClient,
    search: str,
    *,
    article_type: str | None = None,
    gtin: str | None = None,
    max_items: int | None = None,
) -> list[dict[str, Any]]:
    """Free-text search over articles, done client-side.

    The Lexware article endpoint only filters by exact `articleNumber`, `gtin`
    and `type` — there is no server-side name search. So we page through the
    articles (optionally narrowed by type/gtin) and keep the ones whose title,
    description or article number contain `search` (case-insensitive). Paging
    stops early once `max_items` matches are collected.
    """
    needle = search.casefold()
    results: list[dict[str, Any]] = []
    for item in client.paginate(
        "/v1/articles", params={"type": article_type, "gtin": gtin}
    ):
        if _article_matches(item, needle):
            results.append(item)
            if max_items is not None and len(results) >= max_items:
                break
    return results


def _article_matches(article: dict[str, Any], needle_casefolded: str) -> bool:
    for key in ("title", "description", "articleNumber"):
        value = article.get(key)
        if value and needle_casefolded in str(value).casefold():
            return True
    return False


def paginate_filtered(
    client: LexwareClient,
    path: str,
    params: dict[str, Any] | None,
    max_items: int | None,
    predicate: Callable[[dict[str, Any]], bool],
    meta: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], int, bool]:
    """Paginate `path`, keeping only items matching `predicate`.

    Used where the API has no server-side filter (e.g. archived contacts), so
    we must page through and filter client-side. Pages are fetched only until
    `max_items` *matching* items are collected.

    Returns `(kept, skipped, exhausted)`:
      - kept: the matching items (up to max_items)
      - skipped: how many items were filtered out along the way
      - exhausted: True if every page was scanned (so `kept` is the complete
        matching set and `skipped` the complete skipped count)
    """
    kept: list[dict[str, Any]] = []
    skipped = 0
    exhausted = True
    for item in client.paginate(path, params=params, meta=meta):
        if not predicate(item):
            skipped += 1
            continue
        kept.append(item)
        if max_items is not None and len(kept) >= max_items:
            exhausted = False
            break
    return kept, skipped, exhausted


# ---------------------------------------------------------------------------
# High-level operations — one cohesive function per CLI/MCP action. These build
# the request, page through results and return data + metadata (ListResult for
# lists, raw dicts/bytes otherwise). No formatting, no print.
# ---------------------------------------------------------------------------


def get_profile(client: LexwareClient) -> dict[str, Any]:
    """Return the company profile (useful as an auth test)."""
    return client.get("/v1/profile")


def _list_voucherlist(
    client: LexwareClient,
    *,
    voucher_type: str,
    status: str | None,
    number: str | None = None,
    contact_id: str | None = None,
    include_archived: bool = False,
    limit: int = 25,
    fetch_all: bool = False,
) -> ListResult:
    """Shared voucherlist query for invoices/vouchers/quotations/delivery-notes."""
    meta: dict[str, Any] = {}
    items = list(
        client.paginate(
            "/v1/voucherlist",
            params={
                "voucherType": voucher_type,
                "voucherStatus": status or DEFAULT_VOUCHER_STATUSES,
                "voucherNumber": number,
                "contactId": contact_id,
                "archived": archived_param(include_archived),
            },
            max_items=resolve_max_items(limit, fetch_all),
            meta=meta,
        )
    )
    return ListResult(items=items, total=meta.get("total"))


# -- Invoices ---------------------------------------------------------------


def list_invoices(
    client: LexwareClient,
    *,
    status: str | None = None,
    number: str | None = None,
    contact_id: str | None = None,
    include_archived: bool = False,
    limit: int = 25,
    fetch_all: bool = False,
) -> ListResult:
    return _list_voucherlist(
        client,
        voucher_type=INVOICE_LIKE_TYPES,
        status=status,
        number=number,
        contact_id=contact_id,
        include_archived=include_archived,
        limit=limit,
        fetch_all=fetch_all,
    )


def get_invoice(client: LexwareClient, identifier: str) -> dict[str, Any]:
    invoice_id = resolve_voucher_id(client, identifier, INVOICE_LIKE_TYPES)
    return get_with_voucher_fallback(client, f"/v1/invoices/{invoice_id}")


def download_invoice_pdf(client: LexwareClient, identifier: str) -> bytes:
    invoice_id = resolve_voucher_id(client, identifier, INVOICE_LIKE_TYPES)
    return get_pdf_with_voucher_fallback(
        client, f"/v1/invoices/{invoice_id}/file", invoice_id
    )


def create_invoice(client: LexwareClient, body: dict[str, Any]) -> dict[str, Any]:
    return client.post("/v1/invoices", body)


# -- Vouchers ---------------------------------------------------------------


def list_vouchers(
    client: LexwareClient,
    *,
    voucher_type: str | None = None,
    status: str | None = None,
    number: str | None = None,
    contact_id: str | None = None,
    include_archived: bool = False,
    limit: int = 25,
    fetch_all: bool = False,
) -> ListResult:
    return _list_voucherlist(
        client,
        voucher_type=voucher_type or DEFAULT_VOUCHER_TYPES,
        status=status,
        number=number,
        contact_id=contact_id,
        include_archived=include_archived,
        limit=limit,
        fetch_all=fetch_all,
    )


def get_voucher(client: LexwareClient, identifier: str) -> dict[str, Any]:
    voucher_id = resolve_voucher_id(client, identifier, ALL_VOUCHER_TYPES)
    return client.get(f"/v1/vouchers/{voucher_id}")


def create_voucher(client: LexwareClient, body: dict[str, Any]) -> dict[str, Any]:
    return client.post("/v1/vouchers", body)


# -- Quotations -------------------------------------------------------------


def list_quotations(
    client: LexwareClient,
    *,
    status: str | None = None,
    include_archived: bool = False,
    limit: int = 25,
    fetch_all: bool = False,
) -> ListResult:
    return _list_voucherlist(
        client,
        voucher_type="quotation",
        status=status,
        include_archived=include_archived,
        limit=limit,
        fetch_all=fetch_all,
    )


def get_quotation(client: LexwareClient, identifier: str) -> dict[str, Any]:
    quotation_id = resolve_voucher_id(client, identifier, "quotation")
    return get_with_voucher_fallback(client, f"/v1/quotations/{quotation_id}")


def download_quotation_pdf(client: LexwareClient, identifier: str) -> bytes:
    quotation_id = resolve_voucher_id(client, identifier, "quotation")
    return get_pdf_with_voucher_fallback(
        client, f"/v1/quotations/{quotation_id}/file", quotation_id
    )


def create_quotation(client: LexwareClient, body: dict[str, Any]) -> dict[str, Any]:
    return client.post("/v1/quotations", body)


# -- Order confirmations (Aufträge) ------------------------------------------


def list_order_confirmations(
    client: LexwareClient,
    *,
    status: str | None = None,
    include_archived: bool = False,
    limit: int = 25,
    fetch_all: bool = False,
) -> ListResult:
    return _list_voucherlist(
        client,
        voucher_type="orderconfirmation",
        status=status,
        include_archived=include_archived,
        limit=limit,
        fetch_all=fetch_all,
    )


def get_order_confirmation(client: LexwareClient, identifier: str) -> dict[str, Any]:
    order_id = resolve_voucher_id(client, identifier, "orderconfirmation")
    return get_with_voucher_fallback(client, f"/v1/order-confirmations/{order_id}")


def download_order_confirmation_pdf(client: LexwareClient, identifier: str) -> bytes:
    order_id = resolve_voucher_id(client, identifier, "orderconfirmation")
    return get_pdf_with_voucher_fallback(
        client, f"/v1/order-confirmations/{order_id}/file", order_id
    )


def create_order_confirmation(
    client: LexwareClient, body: dict[str, Any]
) -> dict[str, Any]:
    return client.post("/v1/order-confirmations", body)


def article_line_item(
    article: dict[str, Any], quantity: float, *, net_price: float | None = None
) -> dict[str, Any]:
    """Build a `custom` line item from a raw article record.

    The Public API has no article-referencing line-item type, so name,
    description, unit and price are copied from the article. Prices are always
    sent net (the article record carries `netPrice` regardless of its leading
    price), which pairs with `taxConditions.taxType = "net"` in the document
    body. `net_price` overrides the article's list price (e.g. a manually
    edited price in the TUI form).
    """
    price = article.get("price") or {}
    item = {
        "type": "custom",
        "name": article.get("title") or article.get("articleNumber") or "Position",
        "quantity": quantity,
        "unitName": article.get("unitName") or "Stück",
        "unitPrice": {
            "currency": "EUR",
            "netAmount": net_price if net_price is not None else price.get("netPrice", 0),
            "taxRatePercentage": price.get("taxRate", 19),
        },
    }
    description = (article.get("description") or "").strip()
    if description:
        item["description"] = description
    return item


def build_order_confirmation_body(
    contact_id: str,
    line_items: list[dict[str, Any]],
    *,
    voucher_date: datetime | None = None,
    introduction: str | None = None,
    remark: str | None = None,
) -> dict[str, Any]:
    """Minimal valid POST body for /v1/order-confirmations (net prices).

    `voucher_date` defaults to now in the local timezone; the API requires the
    `yyyy-MM-ddTHH:mm:ss.SSSXXX` format that `isoformat(timespec="milliseconds")`
    produces for aware datetimes. `introduction`/`remark` are the document's
    Einleitungs- und Schlusstext; empty/None values are omitted.
    """
    when = (voucher_date or datetime.now()).astimezone()
    body = {
        "voucherDate": when.isoformat(timespec="milliseconds"),
        "address": {"contactId": contact_id},
        "lineItems": line_items,
        "totalPrice": {"currency": "EUR"},
        "taxConditions": {"taxType": "net"},
        "shippingConditions": {"shippingType": "none"},
    }
    if introduction and introduction.strip():
        body["introduction"] = introduction.strip()
    if remark and remark.strip():
        body["remark"] = remark.strip()
    return body


# -- Delivery notes ---------------------------------------------------------


def list_delivery_notes(
    client: LexwareClient,
    *,
    status: str | None = None,
    include_archived: bool = False,
    limit: int = 25,
    fetch_all: bool = False,
) -> ListResult:
    return _list_voucherlist(
        client,
        voucher_type="deliverynote",
        status=status,
        include_archived=include_archived,
        limit=limit,
        fetch_all=fetch_all,
    )


def get_delivery_note(client: LexwareClient, identifier: str) -> dict[str, Any]:
    delivery_note_id = resolve_voucher_id(client, identifier, "deliverynote")
    return get_with_voucher_fallback(
        client, f"/v1/delivery-notes/{delivery_note_id}"
    )


def download_delivery_note_pdf(client: LexwareClient, identifier: str) -> bytes:
    delivery_note_id = resolve_voucher_id(client, identifier, "deliverynote")
    return get_pdf_with_voucher_fallback(
        client, f"/v1/delivery-notes/{delivery_note_id}/file", delivery_note_id
    )


def create_delivery_note(
    client: LexwareClient, body: dict[str, Any]
) -> dict[str, Any]:
    return client.post("/v1/delivery-notes", body)


# -- Contacts ---------------------------------------------------------------


def list_contacts(
    client: LexwareClient,
    *,
    name: str | None = None,
    email: str | None = None,
    number: str | None = None,
    customer: bool = False,
    vendor: bool = False,
    include_archived: bool = False,
    limit: int = 25,
    fetch_all: bool = False,
) -> ListResult:
    """List contacts.

    The contacts API has no server-side archived filter, so by default we page
    through and drop archived ones client-side (reported via `hidden` /
    `exhausted`). With `include_archived` we stream the raw page set.
    """
    params: dict[str, Any] = {"name": name, "email": email, "number": number}
    if customer:
        params["customer"] = "true"
    if vendor:
        params["vendor"] = "true"
    max_items = resolve_max_items(limit, fetch_all)
    meta: dict[str, Any] = {}
    if include_archived:
        items = list(
            client.paginate("/v1/contacts", params=params, max_items=max_items, meta=meta)
        )
        return ListResult(items=items, total=meta.get("total"))
    kept, hidden, exhausted = paginate_filtered(
        client,
        "/v1/contacts",
        params,
        max_items,
        lambda c: not c.get("archived"),
        meta=meta,
    )
    return ListResult(
        items=kept, total=meta.get("total"), hidden=hidden, exhausted=exhausted
    )


def get_contact(client: LexwareClient, contact_id: str) -> dict[str, Any]:
    return client.get(f"/v1/contacts/{contact_id}")


def create_contact(client: LexwareClient, body: dict[str, Any]) -> dict[str, Any]:
    return client.post("/v1/contacts", body)


def update_contact(
    client: LexwareClient, contact_id: str, changes: dict[str, Any]
) -> dict[str, Any]:
    """Partially update a contact: fetch, deep-merge, PUT the full object.

    Lexware's PUT replaces the whole contact and requires the current
    ``version`` (optimistic locking). We therefore always fetch the live
    contact first, merge the caller's ``changes`` onto it, and force the
    just-fetched version — so the caller only ever supplies the fields that
    change and can never break the version handshake.
    """
    current = client.get(f"/v1/contacts/{contact_id}")
    merged = _merge_for_update(current, changes)
    return client.put(f"/v1/contacts/{contact_id}", merged)


# -- Articles ---------------------------------------------------------------


def list_articles(
    client: LexwareClient,
    *,
    search: str | None = None,
    article_type: str | None = None,
    article_number: str | None = None,
    gtin: str | None = None,
    limit: int = 25,
    fetch_all: bool = False,
) -> ListResult:
    """List articles, or free-text search them client-side when `search` is set."""
    max_items = resolve_max_items(limit, fetch_all)
    if search:
        items = search_articles(
            client,
            search,
            article_type=article_type,
            gtin=gtin,
            max_items=max_items,
        )
        return ListResult(items=items, search=True)
    meta: dict[str, Any] = {}
    items = list(
        client.paginate(
            "/v1/articles",
            params={
                "type": article_type,
                "articleNumber": article_number,
                "gtin": gtin,
            },
            max_items=max_items,
            meta=meta,
        )
    )
    return ListResult(items=items, total=meta.get("total"))


def get_article(client: LexwareClient, article_id: str) -> dict[str, Any]:
    return client.get(f"/v1/articles/{article_id}")


def create_article(client: LexwareClient, body: dict[str, Any]) -> dict[str, Any]:
    return client.post("/v1/articles", body)


def update_article(
    client: LexwareClient, article_id: str, changes: dict[str, Any]
) -> dict[str, Any]:
    """Partially update an article — same fetch/deep-merge/PUT flow as
    :func:`update_contact` (articles also use ``version`` locking)."""
    current = client.get(f"/v1/articles/{article_id}")
    merged = _merge_for_update(current, changes)
    return client.put(f"/v1/articles/{article_id}", merged)


def _merge_for_update(current: dict[str, Any], changes: dict[str, Any]) -> dict[str, Any]:
    """Deep-merge ``changes`` onto a freshly fetched resource for a PUT.

    Nested objects merge key-by-key; lists and scalars are replaced wholesale
    (a list can't be meaningfully half-merged — pass the full list to change
    one entry). The resource's own ``version`` always wins, so a caller who
    echoes a stale version in ``changes`` can't trigger a false 409.
    """
    merged = _deep_merge(current, changes)
    if "version" in current:
        merged["version"] = current["version"]
    return merged


def _deep_merge(base: Any, changes: Any) -> Any:
    if isinstance(base, dict) and isinstance(changes, dict):
        out = dict(base)
        for key, value in changes.items():
            out[key] = _deep_merge(base[key], value) if key in base else value
        return out
    return changes
