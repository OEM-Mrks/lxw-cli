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

Offene Aufträge: [green]lexware orders list --status open[/green]

Alle als CSV: [green]lexware --csv -o auftraege.csv orders list --all[/green]

Inkl. archivierte: [green]lexware orders list --include-archived[/green]
""",
)
def list_orders(
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
        help="Auch archivierte Aufträge anzeigen (Standard: ausgeblendet).",
    ),
) -> None:
    """Aufträge (Auftragsbestätigungen) auflisten (über /v1/voucherlist).

    Standardmäßig bis zu 25 Aufträge (--all bzw. --limit 0 lädt alle),
    filterbar nach Status. Archivierte Aufträge sind standardmäßig
    ausgeblendet; --include-archived zeigt sie. get/pdf akzeptieren UUID oder
    Auftragsnummer.
    """
    s = state(ctx)
    with working("Lade Aufträge …"):
        result = services.list_order_confirmations(
            s.client,
            status=voucher_status,
            include_archived=include_archived,
            limit=limit,
            fetch_all=fetch_all,
        )
    render(result.items, s.output_format, columns=LIST_COLUMNS, output_path=s.output_path)
    note = None if include_archived else ARCHIVED_HINT
    print_count(len(result.items), result.total, noun="Aufträge", note=note)


@app.command("get")
def get_order(
    ctx: typer.Context,
    order: str = typer.Argument(..., help="UUID oder Auftragsnummer."),
) -> None:
    """Einen Auftrag im Detail abrufen. Akzeptiert UUID oder Auftragsnummer."""
    s = state(ctx)
    with working("Lade Auftrag …"):
        data = services.get_order_confirmation(s.client, order)
    render(data, s.output_format, output_path=s.output_path)


@app.command("pdf")
def download_pdf(
    ctx: typer.Context,
    order: str = typer.Argument(..., help="UUID oder Auftragsnummer."),
    output: Path | None = typer.Option(
        None,
        "--output",
        "-o",
        help="Ziel-Datei oder -Verzeichnis (Standard: aktuelles Verzeichnis).",
    ),
) -> None:
    """PDF eines Auftrags herunterladen. Akzeptiert UUID oder Auftragsnummer."""
    s = state(ctx)
    with working("Lade PDF …"):
        data = services.download_order_confirmation_pdf(s.client, order)
    write_binary(data, output, default_name=f"order-{order}.pdf")


@app.command("create-draft")
def create_draft(
    ctx: typer.Context,
    body: str = typer.Option(..., "--body", help="JSON-Body inline oder @datei.json."),
) -> None:
    """Neuen Auftrag als Draft anlegen."""
    s = state(ctx)
    payload = load_json_arg(body)
    with working("Lege Auftrag an …"):
        result = services.create_order_confirmation(s.client, payload)
    render(result, s.output_format, output_path=s.output_path)
