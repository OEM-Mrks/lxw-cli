from __future__ import annotations

from pathlib import Path

import typer

from lexware_cli.commands._common import ARCHIVED_HINT, load_json_arg, state
from lexware_cli.core import services
from lexware_cli.core.constants import DEFAULT_VOUCHER_STATUSES
from lexware_cli.output import print_count, render, working, write_binary

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

Offene Angebote: [green]lexware quotations list --status open[/green]

Alle als CSV: [green]lexware --csv -o angebote.csv quotations list --all[/green]

Inkl. archivierte: [green]lexware quotations list --include-archived[/green]
""",
)
def list_quotations(
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
        help="Komma-separiert (z.B. draft,open,accepted,rejected).",
    ),
    include_archived: bool = typer.Option(
        False,
        "--include-archived",
        help="Auch archivierte Angebote anzeigen (Standard: ausgeblendet).",
    ),
) -> None:
    """Angebote auflisten (über /v1/voucherlist).

    Standardmäßig bis zu 25 Angebote (--all bzw. --limit 0 lädt alle),
    filterbar nach Status. Archivierte Angebote sind standardmäßig
    ausgeblendet; --include-archived zeigt sie. get/pdf akzeptieren UUID oder
    Angebotsnummer (z.B. AG5241415).
    """
    s = state(ctx)
    with working("Lade Angebote …"):
        result = services.list_quotations(
            s.client,
            status=voucher_status,
            include_archived=include_archived,
            limit=limit,
            fetch_all=fetch_all,
        )
    render(result.items, s.output_format, columns=LIST_COLUMNS, output_path=s.output_path)
    note = None if include_archived else ARCHIVED_HINT
    print_count(len(result.items), result.total, noun="Angebote", note=note)


@app.command("get")
def get_quotation(
    ctx: typer.Context,
    quotation: str = typer.Argument(..., help="UUID oder Angebotsnummer."),
) -> None:
    """Ein Angebot im Detail abrufen. Akzeptiert UUID oder Angebotsnummer."""
    s = state(ctx)
    with working("Lade Angebot …"):
        data = services.get_quotation(s.client, quotation)
    render(data, s.output_format, output_path=s.output_path)


@app.command("pdf")
def download_pdf(
    ctx: typer.Context,
    quotation: str = typer.Argument(..., help="UUID oder Angebotsnummer."),
    output: Path | None = typer.Option(
        None,
        "--output",
        "-o",
        help="Ziel-Datei oder -Verzeichnis (Standard: aktuelles Verzeichnis).",
    ),
) -> None:
    """PDF eines Angebots herunterladen. Akzeptiert UUID oder Angebotsnummer."""
    s = state(ctx)
    with working("Lade PDF …"):
        data = services.download_quotation_pdf(s.client, quotation)
    write_binary(data, output, default_name=f"quotation-{quotation}.pdf")


@app.command("create-draft")
def create_draft(
    ctx: typer.Context,
    body: str = typer.Option(..., "--body", help="JSON-Body inline oder @datei.json."),
) -> None:
    """Neues Angebot als Draft anlegen."""
    s = state(ctx)
    payload = load_json_arg(body)
    with working("Lege Angebot an …"):
        result = services.create_quotation(s.client, payload)
    render(result, s.output_format, output_path=s.output_path)
