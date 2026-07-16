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
]


@app.command(
    "list",
    epilog="""\
[bold cyan]Beispiele[/bold cyan]

Alle Lieferscheine: [green]lexware delivery-notes list --all[/green]

Als CSV exportieren: [green]lexware --csv -o lieferscheine.csv delivery-notes list --all[/green]

Inkl. archivierte: [green]lexware delivery-notes list --include-archived[/green]
""",
)
def list_delivery_notes(
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
        help="Komma-separiert (z.B. draft,open,paid).",
    ),
    include_archived: bool = typer.Option(
        False,
        "--include-archived",
        help="Auch archivierte Lieferscheine anzeigen (Standard: ausgeblendet).",
    ),
) -> None:
    """Lieferscheine auflisten (über /v1/voucherlist).

    Standardmäßig bis zu 25 Lieferscheine (--all bzw. --limit 0 lädt alle),
    filterbar nach Status. Archivierte sind standardmäßig ausgeblendet;
    --include-archived zeigt sie. get/pdf akzeptieren UUID oder Nummer.
    """
    s = state(ctx)
    with working("Lade Lieferscheine …"):
        result = services.list_delivery_notes(
            s.client,
            status=voucher_status,
            include_archived=include_archived,
            limit=limit,
            fetch_all=fetch_all,
        )
    render(result.items, s.output_format, columns=LIST_COLUMNS, output_path=s.output_path)
    note = None if include_archived else ARCHIVED_HINT
    print_count(len(result.items), result.total, noun="Lieferscheine", note=note)


@app.command("get")
def get_delivery_note(
    ctx: typer.Context,
    delivery_note: str = typer.Argument(..., help="UUID oder Lieferschein-Nummer."),
) -> None:
    """Einen Lieferschein im Detail abrufen. Akzeptiert UUID oder Nummer."""
    s = state(ctx)
    with working("Lade Lieferschein …"):
        data = services.get_delivery_note(s.client, delivery_note)
    render(data, s.output_format, output_path=s.output_path)


@app.command("pdf")
def download_pdf(
    ctx: typer.Context,
    delivery_note: str = typer.Argument(..., help="UUID oder Lieferschein-Nummer."),
    output: Path | None = typer.Option(
        None,
        "--output",
        "-o",
        help="Ziel-Datei oder -Verzeichnis (Standard: aktuelles Verzeichnis).",
    ),
) -> None:
    """PDF eines Lieferscheins herunterladen. Akzeptiert UUID oder Nummer."""
    s = state(ctx)
    with working("Lade PDF …"):
        data = services.download_delivery_note_pdf(s.client, delivery_note)
    write_binary(data, output, default_name=f"deliverynote-{delivery_note}.pdf")


@app.command("create-draft")
def create_draft(
    ctx: typer.Context,
    body: str = typer.Option(..., "--body", help="JSON-Body inline oder @datei.json."),
) -> None:
    """Neuen Lieferschein als Draft anlegen."""
    s = state(ctx)
    payload = load_json_arg(body)
    with working("Lege Lieferschein an …"):
        result = services.create_delivery_note(s.client, payload)
    render(result, s.output_format, output_path=s.output_path)
