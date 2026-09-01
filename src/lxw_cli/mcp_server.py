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

import asyncio
from pathlib import Path
from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import Middleware
from fastmcp.utilities.types import File

from lxw_cli import __build__, __version__
from lxw_cli.config import load_config
from lxw_cli.core import services, status
from lxw_cli.core.client import LexwareClient
from lxw_cli.feature_request import compose_feature_request
from lxw_cli.output import safe_filename

mcp: FastMCP = FastMCP(
    name="lexware",
    version=__version__,
    instructions=(
        # --- How to talk to the end user (this is an end-customer product) ---
        "You are a Lexware Office assistant inside a finished end-customer "
        "product. Speak plainly and non-technically. "
        "When the user asks what you can do, describe CAPABILITIES in everyday "
        "language — for example: view, create and download invoices, quotations, "
        "order confirmations and delivery notes as PDF; look up, create and edit "
        "customers, vendors and articles. Never expose internal tool names, "
        "endpoint or API names, or technical field names, and do not describe "
        "how the system works under the hood. Say WHAT it can do, not what it is "
        "called. "
        "Present every result in clear, human-friendly language (short sentences "
        "or a simple table) — never raw JSON and never technical field names. "
        # --- Product boundary ---
        "This is a finished product: do NOT offer to develop, extend, change or "
        "'quickly add' functionality yourself and do not speculate about building "
        "features. If the user wants something the product cannot do, use the "
        "feature-request capability to compose a short non-binding request and "
        "show it to the user to email to the vendor themselves. "
        "If the user asks which version is running, use the version capability "
        "and tell them the version. "
        # --- Operating status ---
        "If the user asks whether Lexware Office is down, why something is slow "
        "or failing, or whether there is a known problem, use the service-status "
        "capability and report it in plain language. Do the same when an action "
        "just failed and the user wants to know why. Never invent an outage — "
        "only report what the status capability actually returns. "
        # --- Operational notes for you, never surface these to the user ---
        "(Internal, do not surface: documents can be addressed by their number "
        "such as 'FB2600682' or an id; PDF downloads return the PDF itself. When "
        "a document line item is based on an article, fetch that article first "
        "and copy its description into the line item's description.)"
    ),
)

class StatusHintMiddleware(Middleware):
    """Erklärt Serverfehler mit dem Lexware-Betriebsstatus.

    Scheitert ein Aufruf an Lexware selbst (5xx, Timeout, dauerhaftes 429),
    ist die interessante Frage fast immer "liegt das an mir oder an
    Lexware?". Die Statusseite beantwortet das ohne API-Key, also hängen
    wir die Antwort direkt an die Fehlermeldung — statt sie den Nutzer
    separat suchen zu lassen.

    Läuft alles normal (oder ist der Fehler ein Eingabe-/Auth-Fehler),
    bleibt die Meldung unverändert: ein Statushinweis unter jedem 400 wäre
    nur Rauschen. Der Hinweis darf den eigentlichen Fehler außerdem niemals
    verschlucken — jeder Fehler auf dem Diagnoseweg wird ignoriert.
    """

    async def on_call_tool(self, context: Any, call_next: Any) -> Any:
        try:
            return await call_next(context)
        except Exception as exc:
            hint: str | None = None
            try:
                # Synchroner httpx-Aufruf (meist Cache-Treffer) — im Thread,
                # damit der Event-Loop nicht blockiert.
                hint = await asyncio.to_thread(status.hint_for_exception, exc)
            except Exception:  # noqa: BLE001 — Diagnose nie vor den Fehler stellen
                hint = None
            if not hint:
                raise
            raise ToolError(f"{exc} {hint}") from exc


mcp.add_middleware(StatusHintMiddleware())

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


def _ext_from_content_type(content_type: str | None) -> str | None:
    if not content_type:
        return None
    import mimetypes

    ext = mimetypes.guess_extension(content_type.split(";", 1)[0].strip())
    return ext.lstrip(".") if ext else None


def _file_result(
    data: bytes,
    output_dir: str | None,
    filename: str,
    content_type: str | None = None,
) -> str | File:
    """Return an arbitrary downloaded file — inline over HTTP, saved on stdio.

    Like :func:`_pdf_result` but for any media type (attachments may be PDF,
    JPG or PNG). A missing extension is filled in from the content type so the
    saved file / inline resource carries a sensible name.
    """
    name = safe_filename(filename)
    ext_from_ct = _ext_from_content_type(content_type)
    if "." not in name and ext_from_ct:
        name = f"{name}.{ext_from_ct}"
    if _in_http_request():
        stem, _, ext = name.rpartition(".") if "." in name else (name, "", "")
        return File(data=data, name=stem or name, format=ext or ext_from_ct or "bin")
    target_dir = Path(output_dir).expanduser() if output_dir else _default_download_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / name
    target.write_bytes(data)
    return str(target.resolve())


def _resolve_upload(
    file_path: str | None, file_base64: str | None, filename: str | None
) -> tuple[bytes, str]:
    """Turn the caller's upload inputs into (content_bytes, filename).

    Over stdio the file lives on the local disk (`file_path`); over HTTP the
    server can't see the caller's disk, so the bytes arrive as `file_base64`
    (with an explicit `filename`).
    """
    if file_base64:
        import base64

        try:
            content = base64.b64decode(file_base64, validate=True)
        except Exception as exc:  # noqa: BLE001 — report as a clean tool error
            raise ValueError("file_base64 ist kein gültiges Base64.") from exc
        return content, safe_filename(filename or "anhang")
    if file_path:
        source = Path(file_path).expanduser()
        if not source.is_file():
            raise ValueError(f"Datei nicht gefunden: {source}")
        return source.read_bytes(), safe_filename(filename or source.name)
    raise ValueError(
        "Bitte eine Datei angeben: file_path (lokale Datei) oder file_base64 "
        "zusammen mit filename."
    )


# ---------------------------------------------------------------------------
# Read tools
# ---------------------------------------------------------------------------


@mcp.tool
def version() -> dict[str, str]:
    """Report the running version of the Lexware assistant.

    Use this when the user asks which version is running / deployed. Answers
    live from the server, so it reflects the real deployed build even if a
    client UI shows a cached/older version. `stand` is the build timestamp.
    """
    import os

    info = {"produkt": "Lexware-Assistent", "version": __version__}
    # Ein per Deploy gesetzter Zeitstempel gewinnt (er beschreibt genau
    # diesen Rollout); sonst der ins Paket eingebackene Build-Zeitpunkt.
    build = os.getenv("LXW_MCP_BUILD", "").strip() or __build__
    if build:
        info["stand"] = build
    return info


@mcp.tool
def service_status(include_planned: bool = True) -> dict[str, Any]:
    """Check whether Lexware Office itself is currently having problems.

    Use this when the user asks if Lexware is down, why something is slow or
    keeps failing, or whether a known problem exists — and after an action
    failed unexpectedly. Reads Lexware's public status page, so it also works
    when Lexware itself is unreachable.

    `include_planned`: also report announced future maintenance windows.

    Report the result in plain language. `zustand` is one of 'normal',
    'gestört', 'wartung' or 'unbekannt' ('unbekannt' only means the status
    page could not be reached — never present that as an outage).
    """
    report = status.get_status(include_planned=include_planned)
    if report is None:
        return {
            "zustand": "unbekannt",
            "zusammenfassung": (
                "Die Lexware-Statusseite ist gerade nicht erreichbar — über "
                "Störungen lässt sich daraus nichts ableiten."
            ),
            "statusseite": status.STATUS_BASE_URL,
        }

    zustand = {
        status.STATE_OPERATIONAL: "normal",
        status.STATE_DEGRADED: "gestört",
        status.STATE_MAINTENANCE: "wartung",
    }.get(report.state, report.state)

    def _notice(notice: status.Notice) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "titel": notice.subject,
            "art": "Wartung" if notice.is_maintenance else "Störung",
            "beschreibung": status.describe_notice(notice),
            "link": notice.url,
        }
        if notice.update:
            entry["letztes_update"] = notice.update
        return entry

    result: dict[str, Any] = {
        "zustand": zustand,
        "zusammenfassung": status.summary(report),
        "statusseite": report.url,
    }
    if report.current:
        result["aktuelle_meldungen"] = [_notice(n) for n in report.current]
    if report.planned:
        result["geplante_wartungen"] = [_notice(n) for n in report.planned]
    return result


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
def get_payment_status(identifier: str) -> dict[str, Any]:
    """Show whether an invoice is paid and how much is still open.

    Use this to answer "is invoice X paid?" / "what's still open on X?".
    Accepts an invoice number (e.g. 'FB2600682') or id — also works for other
    sales vouchers like credit notes. Returns the still-open amount, the
    payment state (open / paid off / voided), the date it was paid off and the
    individual recorded payments.
    """
    return services.get_payment_status(_client_get(), identifier)


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
def list_posting_categories(category_type: str | None = None) -> list[dict[str, Any]]:
    """List the bookkeeping categories available in this account.

    These are the categories a bookkeeping voucher is assigned to (e.g.
    revenue/income vs. expense categories). Use this to pick the right category
    when recording a receipt or purchase, and to answer "which categories can I
    book to?". `category_type` optionally narrows the list to one kind (income
    or expense). Returns each category's id and name.
    """
    return services.list_posting_categories(
        _client_get(), category_type=category_type
    )


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


@mcp.tool
def get_dunning(dunning_id: str) -> dict[str, Any]:
    """Get a dunning (payment reminder) by its id.

    A dunning can only be addressed by its id — take it from the result when
    the dunning was created, or from the related documents of the invoice it
    belongs to. (Dunnings cannot be listed or looked up by a number.)
    """
    return services.get_dunning(_client_get(), dunning_id)


@mcp.tool
def download_dunning_pdf(dunning_id: str, output_dir: str | None = None) -> str | File:
    """Download a dunning's PDF by its id.

    stdio: saves to disk and returns the path. HTTP: returns the PDF inline.
    Only finalized dunnings have a PDF.
    """
    data = services.download_dunning_pdf(_client_get(), dunning_id)
    return _pdf_result(data, output_dir, f"dunning-{dunning_id}.pdf")


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

    Längenlimits (sonst lehnt Lexware ab): Belegtitel `title` max. 25 Zeichen;
    Einleitungstext `introduction` und Schlusstext `remark` je max. 2000;
    Positionsbezeichnung `name` max. 100; Positionsbeschreibung `description`
    max. 2000. Bei Überschreitung bitte kürzen (der Server meldet es sonst).
    """
    return services.create_invoice(_client_get(), body)


@mcp.tool
def create_contact(body: dict[str, Any]) -> dict[str, Any]:
    """Create a new contact (master data — not a draft).

    Minimal body example:
    {"roles": {"customer": {}}, "company": {"name": "Acme GmbH"}}
    Längenlimits: Notiz `note` max. 1000 Zeichen; Anrede `salutation` max. 25.
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
    Längenlimits: Notiz `note` max. 1000 Zeichen; Anrede `salutation` max. 25.
    """
    return services.update_contact(_client_get(), contact_id, changes)


@mcp.tool
def create_voucher_draft(body: dict[str, Any]) -> dict[str, Any]:
    """Create a generic voucher (for bookkeeping / purchase invoices)."""
    return services.create_voucher(_client_get(), body)


@mcp.tool
def upload_voucher_file(
    identifier: str,
    file_path: str | None = None,
    file_base64: str | None = None,
    filename: str | None = None,
) -> dict[str, Any]:
    """Attach a file (the original receipt/document) to a bookkeeping voucher.

    Use this to add the scan/PDF of a receipt to a voucher after creating it.
    `identifier` is the voucher's number or id. Provide the file either as a
    local `file_path` (when running on the user's machine) OR as `file_base64`
    together with a `filename`. Accepted files are typically PDF, JPG or PNG;
    the system enforces its own size limit and reports clearly if a file is
    rejected. Returns a reference to the stored attachment.
    """
    content, name = _resolve_upload(file_path, file_base64, filename)
    return services.upload_voucher_file(
        _client_get(), identifier, filename=name, content=content
    )


@mcp.tool
def download_voucher_file(file_id: str, output_dir: str | None = None) -> str | File:
    """Download a stored file (e.g. a voucher's attached receipt) by its file id.

    File ids appear in a voucher's list of attached files. Over stdio the file
    is saved to ~/Downloads/lexware/ (or output_dir) and the path is returned;
    over HTTP the file is returned inline and output_dir is ignored.
    """
    data, content_type, name = services.download_file(_client_get(), file_id)
    return _file_result(data, output_dir, name or f"beleg-{file_id}", content_type)


@mcp.tool
def create_article(body: dict[str, Any]) -> dict[str, Any]:
    """Create a new article (product or service — master data, not a draft).

    Längenlimits: Bezeichnung `title` max. 100 Zeichen; Beschreibung
    `description` max. 2000 (einfache Formatierung wie **fett**, __kursiv__ und
    `- Listen` ist möglich und zählt zu den 2000 Zeichen mit).
    """
    return services.create_article(_client_get(), body)


@mcp.tool
def update_article(article_id: str, changes: dict[str, Any]) -> dict[str, Any]:
    """Update an existing article by UUID — pass only the fields to change.

    Same partial-merge + automatic `version` handling as update_contact.
    Nested objects merge key-by-key; lists are replaced wholesale.
    Examples:
      change title: {"title": "Neuer Titel"}
      change price: {"price": {"netPrice": 19.99, "taxRate": 19}}
    Längenlimits: Bezeichnung `title` max. 100 Zeichen; Beschreibung
    `description` max. 2000 (einfache Formatierung möglich).
    """
    return services.update_article(_client_get(), article_id, changes)


@mcp.tool
def request_feature(description: str, contact_email: str | None = None) -> dict[str, str]:
    """Compose a NON-BINDING feature request for the vendor (oemedia) to copy & send.

    Use this ONLY when the user wants functionality that the tools here do not
    provide. This is an end-customer product — you do not build features
    yourself. The tool does NOT send anything: it returns a ready-to-send
    message (`subject`, `body`) and the vendor address (`to`). Present that to
    the user so they can email it themselves; it is non-binding and makes no
    promise about if or when the feature is built. Summarize the wish clearly in
    `description` (German is fine); `contact_email` is optional.
    """
    company: str | None = None
    try:
        profile = services.get_profile(_client_get())
        company = profile.get("companyName") or profile.get("organizationId")
    except Exception:  # noqa: BLE001 — composing the request is still useful
        company = None
    return compose_feature_request(
        description=description, company=company, contact_email=contact_email
    )


@mcp.tool
def create_quotation_draft(body: dict[str, Any]) -> dict[str, Any]:
    """Create a quotation as draft.

    Line items support an optional `description` (long text shown under the
    position name on the PDF). When a line item is based on an article, copy
    the article's `description` into it.

    Längenlimits (sonst lehnt Lexware ab): Belegtitel `title` max. 25 Zeichen;
    Einleitungstext `introduction` und Schlusstext `remark` je max. 2000;
    Positionsbezeichnung `name` max. 100; Positionsbeschreibung `description`
    max. 2000. Bei Überschreitung bitte kürzen (der Server meldet es sonst).
    """
    return services.create_quotation(_client_get(), body)


@mcp.tool
def create_order_confirmation_draft(body: dict[str, Any]) -> dict[str, Any]:
    """Create an order confirmation (Auftrag) as draft.

    Line items support an optional `description` (long text shown under the
    position name on the PDF). When a line item is based on an article, copy
    the article's `description` into it.

    Längenlimits (sonst lehnt Lexware ab): Belegtitel `title` max. 25 Zeichen;
    Einleitungstext `introduction` und Schlusstext `remark` je max. 2000;
    Positionsbezeichnung `name` max. 100; Positionsbeschreibung `description`
    max. 2000. Bei Überschreitung bitte kürzen (der Server meldet es sonst).
    """
    return services.create_order_confirmation(_client_get(), body)


@mcp.tool
def continue_document(identifier: str, target: str) -> dict[str, Any]:
    """Continue a document into its follow-up, keeping them linked (Belegkette).

    Use when the user wants to carry a document forward to the next step. The
    content (customer, positions, texts) is taken over and both documents stay
    linked. The follow-up is created as a draft. The source type is detected
    automatically from `identifier`.

    `identifier`: the source document's number or id.
    `target`: the follow-up to create. Supported paths (same as in Lexware):
        - from a quotation (Angebot): 'Auftrag', 'Lieferschein' or 'Rechnung'
        - from an order confirmation (Auftrag): 'Lieferschein' or 'Rechnung'
        - from a delivery note (Lieferschein): 'Rechnung'
        - from an invoice (Rechnung): 'Lieferschein' or 'Rechnungskorrektur'
    Notes: continuing to an invoice/credit note requires the source to be
    finalized (festgeschrieben) first — reported clearly if not. 'Abschlags-
    rechnung' and 'Serienrechnung' cannot be created via the interface (only in
    Lexware directly). Unsupported steps are reported clearly.
    """
    return services.continue_document(_client_get(), identifier, target)


@mcp.tool
def create_dunning(identifier: str) -> dict[str, Any]:
    """Create a dunning (Mahnung / payment reminder) from an overdue invoice.

    Use when the user wants to remind a customer about an unpaid invoice.
    `identifier` is the invoice's number (e.g. 'FB2600682') or id. The invoice
    must already be finalized and still have an open amount — otherwise this
    reports the reason clearly. The dunning is created as a draft and stays
    linked to the invoice; creating another one for the same invoice raises the
    reminder to the next level. Note the returned id — a dunning can only be
    opened again by its id, not by a number.
    """
    return services.create_dunning(_client_get(), identifier)


@mcp.tool
def create_delivery_note_draft(body: dict[str, Any]) -> dict[str, Any]:
    """Create a delivery note as draft.

    Line items support an optional `description` (long text shown under the
    position name on the PDF). When a line item is based on an article, copy
    the article's `description` into it.

    Längenlimits (sonst lehnt Lexware ab): Belegtitel `title` max. 25 Zeichen;
    Einleitungstext `introduction` und Schlusstext `remark` je max. 2000;
    Positionsbezeichnung `name` max. 100; Positionsbeschreibung `description`
    max. 2000. Bei Überschreitung bitte kürzen (der Server meldet es sonst).
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
        LXW_MCP_STATUS_REFRESH
                            seconds between background checks of Lexware's
                            status page (default 600, matching its CDN cache;
                            0 disables). Keeps the cache warm so the error
                            path answers instantly, and logs incidents for
                            the operator. Only useful here — the stdio server
                            is short-lived and fills the cache lazily.
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

    try:
        refresh = float(os.environ.get("LXW_MCP_STATUS_REFRESH", "600"))
    except ValueError:
        refresh = 600.0
    if refresh > 0:
        status.start_background_refresh(refresh)

    mcp.auth = LexwareOAuthProvider(public_url=public_url, secret=secret)
    mcp.run(transport="http", host=host, port=port)


if __name__ == "__main__":
    run()
