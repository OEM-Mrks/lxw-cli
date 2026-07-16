"""FastMCP server exposing the Lexware Office API to Claude.

A thin frontend over :mod:`lexware_cli.core.services` — the exact same
UI-agnostic operations the CLI uses, so both stay in sync by construction.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastmcp import FastMCP

from lexware_cli.config import load_config
from lexware_cli.core import services
from lexware_cli.core.client import LexwareClient
from lexware_cli.output import safe_filename

mcp: FastMCP = FastMCP(
    name="lexware",
    instructions=(
        "Lexware Office API. Most document tools accept either a UUID or a "
        "voucher number (e.g. invoice number 'FB2600682'). PDF download tools "
        "save the file to ~/Downloads/lexware/ by default and return the path."
    ),
)

_client: LexwareClient | None = None


def _client_get() -> LexwareClient:
    global _client
    if _client is None:
        _client = LexwareClient(load_config())
    return _client


def _default_download_dir() -> Path:
    target = Path.home() / "Downloads" / "lexware"
    target.mkdir(parents=True, exist_ok=True)
    return target


def _save_pdf(data: bytes, output_dir: str | None, filename: str) -> str:
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
def download_invoice_pdf(identifier: str, output_dir: str | None = None) -> str:
    """Download an invoice PDF. Accepts UUID or invoice number.

    Saves to ~/Downloads/lexware/ by default and returns the absolute file path.
    Note: drafts cannot be downloaded — the API requires a finalized status.
    """
    data = services.download_invoice_pdf(_client_get(), identifier)
    return _save_pdf(data, output_dir, f"invoice-{identifier}.pdf")


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
def download_quotation_pdf(identifier: str, output_dir: str | None = None) -> str:
    """Download a quotation PDF. Returns the saved file path."""
    data = services.download_quotation_pdf(_client_get(), identifier)
    return _save_pdf(data, output_dir, f"quotation-{identifier}.pdf")


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
) -> str:
    """Download an order confirmation PDF. Returns the saved file path."""
    data = services.download_order_confirmation_pdf(_client_get(), identifier)
    return _save_pdf(data, output_dir, f"order-{identifier}.pdf")


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
def download_delivery_note_pdf(identifier: str, output_dir: str | None = None) -> str:
    """Download a delivery note PDF. Returns the saved file path."""
    data = services.download_delivery_note_pdf(_client_get(), identifier)
    return _save_pdf(data, output_dir, f"deliverynote-{identifier}.pdf")


# ---------------------------------------------------------------------------
# Write tools — master data is created directly; documents are created as
# drafts (never finalized).
# ---------------------------------------------------------------------------


@mcp.tool
def create_invoice_draft(body: dict[str, Any]) -> dict[str, Any]:
    """Create an invoice as draft. See https://developers.lexware.io/docs/ for body schema."""
    return services.create_invoice(_client_get(), body)


@mcp.tool
def create_contact(body: dict[str, Any]) -> dict[str, Any]:
    """Create a new contact (master data — not a draft).

    Minimal body example:
    {"roles": {"customer": {}}, "company": {"name": "Acme GmbH"}}
    """
    return services.create_contact(_client_get(), body)


@mcp.tool
def create_voucher_draft(body: dict[str, Any]) -> dict[str, Any]:
    """Create a generic voucher (for bookkeeping / purchase invoices)."""
    return services.create_voucher(_client_get(), body)


@mcp.tool
def create_article(body: dict[str, Any]) -> dict[str, Any]:
    """Create a new article (product or service — master data, not a draft)."""
    return services.create_article(_client_get(), body)


@mcp.tool
def create_quotation_draft(body: dict[str, Any]) -> dict[str, Any]:
    """Create a quotation as draft."""
    return services.create_quotation(_client_get(), body)


@mcp.tool
def create_order_confirmation_draft(body: dict[str, Any]) -> dict[str, Any]:
    """Create an order confirmation (Auftrag) as draft."""
    return services.create_order_confirmation(_client_get(), body)


@mcp.tool
def create_delivery_note_draft(body: dict[str, Any]) -> dict[str, Any]:
    """Create a delivery note as draft."""
    return services.create_delivery_note(_client_get(), body)


def run() -> None:
    """Entry point for `lexware-mcp` — runs the MCP server over stdio."""
    mcp.run()


if __name__ == "__main__":
    run()
