from __future__ import annotations

from pathlib import Path

import typer

from lxw_cli.commands._common import ARCHIVED_HINT, load_json_arg, state
from lxw_cli.core import services
from lxw_cli.core.constants import DEFAULT_VOUCHER_STATUSES
from lxw_cli.output import print_count, render, working, write_binary

app = typer.Typer(no_args_is_help=True)

LIST_COLUMNS = [
    "id",
    "voucherNumber",
    "voucherDate",
    "voucherStatus",
    "contactName",
    "totalAmount",
    "currency",
]


@app.command(
    "list",
    epilog="""\
[bold cyan]Beispiele[/bold cyan]

Offene Rechnungen: [green]lxw invoices list --status open[/green]

Alle als CSV: [green]lxw --csv invoices list --all[/green]

Nach Rechnungsnummer: [green]lxw invoices list --number FB2600682[/green]

Inkl. archivierte: [green]lxw invoices list --include-archived[/green]
""",
)
def list_invoices(
    ctx: typer.Context,
    limit: int = typer.Option(
        25, "--limit", "-n", help="Maximale Anzahl Einträge (0 = alle)."
    ),
    fetch_all: bool = typer.Option(
        False, "--all", "-a", help="Alle Treffer laden (überschreibt --limit)."
    ),
    voucher_status: str = typer.Option(
        DEFAULT_VOUCHER_STATUSES,
        "--status",
        help="Komma-separiert: draft,open,paid,paidoff,voided,overdue,accepted,rejected.",
    ),
    voucher_number: str | None = typer.Option(
        None, "--number", help="Filter: exakte Rechnungsnummer."
    ),
    contact_id: str | None = typer.Option(
        None, "--contact-id", help="Filter: Kontakt-UUID (Kunde)."
    ),
    include_archived: bool = typer.Option(
        False,
        "--include-archived",
        help="Auch archivierte Rechnungen anzeigen (Standard: ausgeblendet).",
    ),
) -> None:
    """Rechnungen auflisten.

    Listet Rechnungen aus /v1/voucherlist (salesinvoice, invoice,
    downpaymentinvoice). Standardmäßig werden bis zu 25 Einträge gezeigt — mit
    --all bzw. --limit 0 werden alle Seiten geladen. Filtern lässt sich nach
    Status, exakter Rechnungsnummer und Kunde.

    Archivierte Rechnungen sind standardmäßig ausgeblendet; --include-archived
    zeigt sie. Am Ende wird die Gesamtzahl ausgegeben. Tipp: get/pdf akzeptieren
    UUID oder Rechnungsnummer (z.B. FB2600682).
    """
    s = state(ctx)
    with working("Lade Rechnungen …"):
        result = services.list_invoices(
            s.client,
            status=voucher_status,
            number=voucher_number,
            contact_id=contact_id,
            include_archived=include_archived,
            limit=limit,
            fetch_all=fetch_all,
        )
    render(result.items, s.output_format, columns=LIST_COLUMNS, output_path=s.output_path)
    note = None if include_archived else ARCHIVED_HINT
    print_count(len(result.items), result.total, noun="Rechnungen", note=note)


@app.command(
    "get",
    epilog="""\
[bold cyan]Beispiele[/bold cyan]

Nach Rechnungsnummer: [green]lxw invoices get FB2600682[/green]

Betrag via jq: [green]lxw --json invoices get FB2600682 | jq .totalGrossAmount[/green]
""",
)
def get_invoice(
    ctx: typer.Context,
    invoice: str = typer.Argument(..., help="UUID oder Rechnungsnummer (z.B. FB2600682)."),
) -> None:
    """Eine Rechnung im Detail abrufen.

    Akzeptiert die UUID oder die Rechnungsnummer (z.B. FB2600682) — die Nummer
    wird automatisch aufgelöst. Mit --json gibt es die vollständigen Rohdaten
    für die Weiterverarbeitung.
    """
    s = state(ctx)
    with working("Lade Rechnung …"):
        data = services.get_invoice(s.client, invoice)
    render(data, s.output_format, output_path=s.output_path)


@app.command(
    "pdf",
    epilog="""\
[bold cyan]Beispiele[/bold cyan]

Ins aktuelle Verzeichnis: [green]lxw invoices pdf FB2600682[/green]

In ein Verzeichnis (Auto-Name): [green]lxw invoices pdf FB2600682 -o ~/Rechnungen[/green]

Mit festem Dateinamen: [green]lxw invoices pdf FB2600682 -o rechnung.pdf[/green]
""",
)
def download_pdf(
    ctx: typer.Context,
    invoice: str = typer.Argument(..., help="UUID oder Rechnungsnummer."),
    output: Path | None = typer.Option(
        None,
        "--output",
        "-o",
        help="Ziel-Datei oder -Verzeichnis (Standard: aktuelles Verzeichnis).",
    ),
) -> None:
    """PDF einer Rechnung herunterladen.

    Akzeptiert UUID oder Rechnungsnummer. Ohne -o landet die Datei im aktuellen
    Verzeichnis (z.B. invoice-FB2600682.pdf); zeigt -o auf ein Verzeichnis, wird
    der Dateiname automatisch vergeben. Hinweis: Entwürfe haben noch kein PDF.
    """
    s = state(ctx)
    with working("Lade PDF …"):
        data = services.download_invoice_pdf(s.client, invoice)
    write_binary(data, output, default_name=f"invoice-{invoice}.pdf")


@app.command(
    "create-draft",
    epilog="""\
[bold cyan]Beispiele[/bold cyan]

Aus einer Datei: [green]lxw invoices create-draft --body @rechnung.json[/green]

Inline: [green]lxw invoices create-draft --body '{…}'[/green]
""",
)
def create_draft(
    ctx: typer.Context,
    body: str = typer.Option(
        ...,
        "--body",
        help="JSON-Body inline oder @datei.json (siehe API-Doku).",
    ),
) -> None:
    """Neue Rechnung als Draft anlegen (kein Finalize).

    Erwartet den vollständigen JSON-Body der Lexware-API — inline oder als
    @pfad.json. Die Rechnung wird als Entwurf gespeichert (nicht finalisiert).
    Schema siehe https://developers.lexware.io/docs/.
    """
    s = state(ctx)
    payload = load_json_arg(body)
    with working("Lege Rechnung an …"):
        result = services.create_invoice(s.client, payload)
    render(result, s.output_format, output_path=s.output_path)
