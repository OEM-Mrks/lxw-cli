"""FastMCP server exposing the Lexware Office API to Claude.

A thin frontend over :mod:`lxw_cli.core.services` — the exact same
UI-agnostic operations the CLI uses, so both stay in sync by construction.

Runs in two modes:

- **stdio** (``lxw-mcp``): single-user, the key comes from the local
  config exactly as for the CLI.
- **HTTP** (``lxw-mcp-http``): multi-user. Each request brings its own
  Lexware API key — either directly as ``Authorization: Bearer <key>``
  or wrapped in an OAuth token issued by :mod:`lxw_cli.mcp_auth`. No
  key is ever stored on the server.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastmcp import FastMCP
from fastmcp.utilities.types import File

from lxw_cli.config import load_config
from lxw_cli.core import services
from lxw_cli.core.client import LexwareClient
from lxw_cli.output import safe_filename

mcp: FastMCP = FastMCP(
    name="lexware",
    instructions=(
        "Lexware Office API. Most document tools accept either a UUID or a "
        "voucher number (e.g. invoice number 'FB2600682'). PDF download tools "
        "save the file to ~/Downloads/lexware/ by default and return the path "
        "(over HTTP they return the PDF itself). "
        "When creating documents (invoice/quotation/order confirmation/delivery "
        "note) with line items based on an article: fetch the article first and "
        "copy its `description` into the line item's optional `description` "
        "field — otherwise the article description is missing on the printed "
        "document."
    ),
)

_client: LexwareClient | None = None


def _client_get() -> LexwareClient:
    # Multi-user HTTP mode: the authenticated request carries the user's
    # own key; each key gets its own (cached) client.
    from lxw_cli.mcp_auth import pool, request_api_key

    api_key = request_api_key()
    if api_key is not None:
        return pool.get(api_key)
    # stdio mode: single user, key from the local config.
    global _client
    if _client is None:
        _client = LexwareClient(load_config())
    return _client


def _default_download_dir() -> Path:
    target = Path.home() / "Downloads" / "lexware"
    target.mkdir(parents=True, exist_ok=True)
    return target


def _in_http_request() -> bool:
    try:
        from fastmcp.server.dependencies import get_http_request

        return get_http_request() is not None
    except Exception:  # noqa: BLE001 — outside any HTTP context
        return False


def _pdf_result(data: bytes, output_dir: str | None, filename: str) -> str | File:
    """Local stdio: save to disk and return the path (as documented).

    Over HTTP the server's filesystem is useless to the caller, so the
    PDF is returned inline as a binary resource instead.
    """
    if _in_http_request():
        # File appends the format as extension to the synthetic resource URI;
        # strip a trailing .pdf from the name so it isn't doubled (foo.pdf.pdf).
        stem = safe_filename(filename).removesuffix(".pdf")
        return File(data=data, name=stem, format="pdf")
    target_dir = Path(output_dir).expanduser() if output_dir else _default_download_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    # filename embeds the caller-supplied identifier — sanitize so it can
    # never escape target_dir.
    target = target_dir / safe_filename(filename)
    target.write_bytes(data)
    return str(target.resolve())


# ---------------------------------------------------------------------------
# Read tools
# ---------------------------------------------------------------------------


@mcp.tool
def profile() -> dict[str, Any]:
    """Return the user's Lexware Office company profile. Useful as an auth test."""
    return services.get_profile(_client_get())


@mcp.tool
def list_invoices(
    limit: int = 25,
    voucher_status: str | None = None,
    voucher_number: str | None = None,
    contact_id: str | None = None,
    include_archived: bool = False,
) -> list[dict[str, Any]]:
    """List invoices (salesinvoice/invoice/downpaymentinvoice).

    voucher_status: comma-separated, e.g. 'open,paid' (default: all non-overdue).
    voucher_number: filter by exact invoice number.
    contact_id: filter by customer UUID.
    include_archived: archived invoices are excluded by default; set True to
        include them (filtered server-side).
    limit: max results to return; set to 0 to fetch all (paginates internally).
    """
    return services.list_invoices(
        _client_get(),
        status=voucher_status,
        number=voucher_number,
        contact_id=contact_id,
        include_archived=include_archived,
        limit=limit,
    ).items


@mcp.tool
def get_invoice(identifier: str) -> dict[str, Any]:
    """Get invoice details by UUID or invoice number (e.g. 'FB2600682')."""
    return services.get_invoice(_client_get(), identifier)


@mcp.tool
def download_invoice_pdf(identifier: str, output_dir: str | None = None) -> str | File:
    """Download an invoice PDF. Accepts UUID or invoice number.

    Over stdio the PDF is saved to ~/Downloads/lexware/ (or output_dir) and the
    absolute path is returned. Over HTTP the PDF is returned inline as binary
    content and output_dir is ignored (the server's disk isn't the caller's).
    Note: drafts cannot be downloaded — the API requires a finalized status.
    """
    data = services.download_invoice_pdf(_client_get(), identifier)
    return _pdf_result(data, output_dir, f"invoice-{identifier}.pdf")


@mcp.tool
def list_contacts(
    limit: int = 25,
    name: str | None = None,
    email: str | None = None,
    number: str | None = None,
    customer: bool = False,
    vendor: bool = False,
    include_archived: bool = False,
) -> list[dict[str, Any]]:
    """List contacts (customers/vendors).

    name/email require >=3 chars. Use customer/vendor flags to filter by role.
    Archived contacts are excluded by default; set include_archived=True to keep
    them (the API has no server-side archived filter, so this filters
    client-side). Set limit=0 to fetch all contacts (paginates internally).
    """
    return services.list_contacts(
        _client_get(),
        name=name,
        email=email,
        number=number,
        customer=customer,
        vendor=vendor,
        include_archived=include_archived,
        limit=limit,
    ).items


@mcp.tool
def get_contact(contact_id: str) -> dict[str, Any]:
    """Get a contact's details by UUID."""
    return services.get_contact(_client_get(), contact_id)


@mcp.tool
def list_vouchers(
    limit: int = 25,
    voucher_type: str | None = None,
    voucher_status: str | None = None,
    voucher_number: str | None = None,
    contact_id: str | None = None,
    include_archived: bool = False,
) -> list[dict[str, Any]]:
    """List vouchers (all types — invoices, credit notes, purchase invoices etc.).

    voucher_type: comma-separated, e.g. 'salesinvoice,purchaseinvoice'.
    Default: all sales/purchase types. Archived vouchers are excluded by default;
    set include_archived=True to include them. Set limit=0 to fetch all
    (paginates internally).
    """
    return services.list_vouchers(
        _client_get(),
        voucher_type=voucher_type,
        status=voucher_status,
        number=voucher_number,
        contact_id=contact_id,
        include_archived=include_archived,
        limit=limit,
    ).items


@mcp.tool
def get_voucher(identifier: str) -> dict[str, Any]:
    """Get a voucher's details by UUID or voucher number. Searches all voucher types."""
    return services.get_voucher(_client_get(), identifier)


@mcp.tool
def list_articles(
    limit: int = 25,
    search: str | None = None,
    article_type: str | None = None,
    article_number: str | None = None,
    gtin: str | None = None,
) -> list[dict[str, Any]]:
    """List or search articles (products and services).

    search: free-text, case-insensitive substring match over the article's
        title, description and article number — use this to find an article by
        name/Bezeichnung or a partial number (the API itself only filters by
        exact article_number/gtin). Done client-side over all articles.
    article_type: 'product' or 'service'.
    article_number: exact article number (server-side, fast).
    Set limit=0 to fetch all articles (paginates internally).
    """
    return services.list_articles(
        _client_get(),
        search=search,
        article_type=article_type,
        article_number=article_number,
        gtin=gtin,
        limit=limit,
    ).items


@mcp.tool
def get_article(article_id: str) -> dict[str, Any]:
    """Get an article's details by UUID."""
    return services.get_article(_client_get(), article_id)


@mcp.tool
def list_quotations(
    limit: int = 25,
    voucher_status: str | None = None,
    include_archived: bool = False,
) -> list[dict[str, Any]]:
    """List quotations. voucher_status: comma-separated filter.

    Archived quotations are excluded by default; set include_archived=True to
    include them. Set limit=0 to fetch all quotations (paginates internally).
    """
    return services.list_quotations(
        _client_get(),
        status=voucher_status,
        include_archived=include_archived,
        limit=limit,
    ).items


@mcp.tool
def get_quotation(identifier: str) -> dict[str, Any]:
    """Get a quotation's details by UUID or quotation number (e.g. 'AG5241415')."""
    return services.get_quotation(_client_get(), identifier)


@mcp.tool
def download_quotation_pdf(identifier: str, output_dir: str | None = None) -> str | File:
    """Download a quotation PDF.

    stdio: saves to disk and returns the path. HTTP: returns the PDF inline.
    """
    data = services.download_quotation_pdf(_client_get(), identifier)
    return _pdf_result(data, output_dir, f"quotation-{identifier}.pdf")


@mcp.tool
def list_order_confirmations(
    limit: int = 25,
    voucher_status: str | None = None,
    include_archived: bool = False,
) -> list[dict[str, Any]]:
    """List order confirmations (Aufträge). voucher_status: comma-separated filter.

    Archived orders are excluded by default; set include_archived=True to
    include them. Set limit=0 to fetch all (paginates internally).
    """
    return services.list_order_confirmations(
        _client_get(),
        status=voucher_status,
        include_archived=include_archived,
        limit=limit,
    ).items


@mcp.tool
def get_order_confirmation(identifier: str) -> dict[str, Any]:
    """Get an order confirmation's details by UUID or order number."""
    return services.get_order_confirmation(_client_get(), identifier)


@mcp.tool
def download_order_confirmation_pdf(
    identifier: str, output_dir: str | None = None
) -> str | File:
    """Download an order confirmation PDF. Returns the saved file path."""
    data = services.download_order_confirmation_pdf(_client_get(), identifier)
    return _pdf_result(data, output_dir, f"order-{identifier}.pdf")


@mcp.tool
def list_delivery_notes(
    limit: int = 25,
    voucher_status: str | None = None,
    include_archived: bool = False,
) -> list[dict[str, Any]]:
    """List delivery notes.

    Archived delivery notes are excluded by default; set include_archived=True
    to include them. Set limit=0 to fetch all (paginates internally).
    """
    return services.list_delivery_notes(
        _client_get(),
        status=voucher_status,
        include_archived=include_archived,
        limit=limit,
    ).items


@mcp.tool
def get_delivery_note(identifier: str) -> dict[str, Any]:
    """Get a delivery note's details by UUID or delivery note number."""
    return services.get_delivery_note(_client_get(), identifier)


@mcp.tool
def download_delivery_note_pdf(identifier: str, output_dir: str | None = None) -> str | File:
    """Download a delivery note PDF.

    stdio: saves to disk and returns the path. HTTP: returns the PDF inline.
    """
    data = services.download_delivery_note_pdf(_client_get(), identifier)
    return _pdf_result(data, output_dir, f"deliverynote-{identifier}.pdf")


# ---------------------------------------------------------------------------
# Write tools — master data is created directly; documents are created as
# drafts (never finalized).
# ---------------------------------------------------------------------------


@mcp.tool
def create_invoice_draft(body: dict[str, Any]) -> dict[str, Any]:
    """Create an invoice as draft. See https://developers.lexware.io/docs/ for body schema.

    Line items support an optional `description` (long text shown under the
    position name on the PDF). When a line item is based on an article, copy
    the article's `description` into it.
    """
    return services.create_invoice(_client_get(), body)


@mcp.tool
def create_contact(body: dict[str, Any]) -> dict[str, Any]:
    """Create a new contact (master data — not a draft).

    Minimal body example:
    {"roles": {"customer": {}}, "company": {"name": "Acme GmbH"}}
    """
    return services.create_contact(_client_get(), body)


@mcp.tool
def update_contact(contact_id: str, changes: dict[str, Any]) -> dict[str, Any]:
    """Update an existing contact by UUID — pass only the fields to change.

    The server fetches the current contact, deep-merges `changes` onto it,
    carries the current `version` automatically (Lexware optimistic locking),
    and saves — so you never fetch/version-juggle yourself. Nested objects
    merge key-by-key; lists are REPLACED wholesale, so to change one entry of
    a list send the full list.
    Examples:
      rename company:      {"company": {"name": "Neuer Name GmbH"}}
      change business mail:{"emailAddresses": {"business": ["neu@example.com"]}}
      change billing addr: {"addresses": {"billing": [{"street": "Weg 1",
                            "zip": "50667", "city": "Köln", "countryCode": "DE"}]}}
    Note: `archived` is read-only in the Lexware API and cannot be changed here.
    """
    return services.update_contact(_client_get(), contact_id, changes)


@mcp.tool
def create_voucher_draft(body: dict[str, Any]) -> dict[str, Any]:
    """Create a generic voucher (for bookkeeping / purchase invoices)."""
    return services.create_voucher(_client_get(), body)


@mcp.tool
def create_article(body: dict[str, Any]) -> dict[str, Any]:
    """Create a new article (product or service — master data, not a draft)."""
    return services.create_article(_client_get(), body)


@mcp.tool
def update_article(article_id: str, changes: dict[str, Any]) -> dict[str, Any]:
    """Update an existing article by UUID — pass only the fields to change.

    Same partial-merge + automatic `version` handling as update_contact.
    Nested objects merge key-by-key; lists are replaced wholesale.
    Examples:
      change title: {"title": "Neuer Titel"}
      change price: {"price": {"netPrice": 19.99, "taxRate": 19}}
    """
    return services.update_article(_client_get(), article_id, changes)


@mcp.tool
def create_quotation_draft(body: dict[str, Any]) -> dict[str, Any]:
    """Create a quotation as draft.

    Line items support an optional `description` (long text shown under the
    position name on the PDF). When a line item is based on an article, copy
    the article's `description` into it.
    """
    return services.create_quotation(_client_get(), body)


@mcp.tool
def create_order_confirmation_draft(body: dict[str, Any]) -> dict[str, Any]:
    """Create an order confirmation (Auftrag) as draft.

    Line items support an optional `description` (long text shown under the
    position name on the PDF). When a line item is based on an article, copy
    the article's `description` into it.
    """
    return services.create_order_confirmation(_client_get(), body)


@mcp.tool
def create_delivery_note_draft(body: dict[str, Any]) -> dict[str, Any]:
    """Create a delivery note as draft.

    Line items support an optional `description` (long text shown under the
    position name on the PDF). When a line item is based on an article, copy
    the article's `description` into it.
    """
    return services.create_delivery_note(_client_get(), body)


def run() -> None:
    """Entry point for `lxw-mcp` — runs the MCP server over stdio."""
    mcp.run()


def run_http() -> None:
    """Entry point for `lxw-mcp-http` — multi-user server over HTTP.

    Environment:
        LXW_MCP_PUBLIC_URL  public base URL (behind the reverse proxy),
                            e.g. https://mcp.example.com — used for the
                            OAuth metadata and the consent redirect.
        LXW_MCP_SECRET      token-sealing secret. Any random string works;
                            without it a fresh one is generated per start
                            and all previously issued tokens stop working.
        LXW_MCP_HOST/PORT   bind address (default 127.0.0.1:8788).
        LXW_MCP_DATA_DIR    where OAuth client registrations live
                            (default: <config dir>/mcp).
    """
    import os
    import secrets
    import sys

    from lxw_cli.mcp_auth import (
        ENV_HOST,
        ENV_PORT,
        ENV_PUBLIC_URL,
        ENV_SECRET,
        LexwareOAuthProvider,
    )

    host = os.environ.get(ENV_HOST, "127.0.0.1")
    port = int(os.environ.get(ENV_PORT, "8788"))
    public_url = os.environ.get(ENV_PUBLIC_URL, f"http://{host}:{port}")
    secret = os.environ.get(ENV_SECRET, "")
    if not secret:
        secret = secrets.token_urlsafe(32)
        print(
            f"Warnung: {ENV_SECRET} ist nicht gesetzt — es wurde ein flüchtiges "
            "Secret erzeugt. Alle ausgestellten Tokens werden beim nächsten "
            "Neustart ungültig. Für den Dauerbetrieb ein festes Secret setzen.",
            file=sys.stderr,
        )

    mcp.auth = LexwareOAuthProvider(public_url=public_url, secret=secret)
    mcp.run(transport="http", host=host, port=port)


if __name__ == "__main__":
    run()
