from __future__ import annotations

import typer

from lxw_cli.commands._common import ARCHIVED_HINT, load_json_arg, state
from lxw_cli.core import services
from lxw_cli.core.constants import DEFAULT_VOUCHER_STATUSES, DEFAULT_VOUCHER_TYPES
from lxw_cli.output import print_count, render, working

app = typer.Typer(no_args_is_help=True)

LIST_COLUMNS = [
    "id",
    "voucherType",
    "voucherStatus",
    "voucherNumber",
    "voucherDate",
    "contactName",
    "totalAmount",
    "currency",
]


@app.command(
    "list",
    epilog="""\
[bold cyan]Beispiele[/bold cyan]

Nur Eingangsrechnungen: [green]lxw vouchers list --type purchaseinvoice[/green]

Offene & überfällige: [green]lxw vouchers list --status open,overdue[/green]

Alle eines Kontakts: [green]lxw vouchers list --contact-id <uuid> --all[/green]

Inkl. archivierte: [green]lxw vouchers list --include-archived[/green]
""",
)
def list_vouchers(
    ctx: typer.Context,
    limit: int = typer.Option(
        25, "--limit", "-n", help="Maximale Anzahl Einträge (0 = alle)."
    ),
    fetch_all: bool = typer.Option(
        False, "--all", "-a", help="Alle Treffer laden (überschreibt --limit)."
    ),
    voucher_type: str = typer.Option(
        DEFAULT_VOUCHER_TYPES,
        "--type",
        help=(
            "Komma-separiert: salesinvoice,salescreditnote,purchaseinvoice,"
            "purchasecreditnote,invoice,downpaymentinvoice,creditnote,"
            "orderconfirmation,quotation,deliverynote."
        ),
    ),
    voucher_status: str = typer.Option(
        DEFAULT_VOUCHER_STATUSES,
        "--status",
        help="Komma-separiert (z.B. open,paid,paidoff).",
    ),
    voucher_number: str | None = typer.Option(
        None, "--number", help="Filter: exakte Belegnummer."
    ),
    contact_id: str | None = typer.Option(
        None, "--contact-id", help="Filter: Kontakt-UUID."
    ),
    include_archived: bool = typer.Option(
        False,
        "--include-archived",
        help="Auch archivierte Belege anzeigen (Standard: ausgeblendet).",
    ),
) -> None:
    """Belege auflisten (alle voucher-types).

    Anders als 'invoices' deckt dieser Befehl alle Beleg-Typen ab — von
    salesinvoice über purchaseinvoice bis creditnote (siehe --type).
    Standardmäßig bis zu 25 Einträge (--all lädt alle), filterbar nach Typ,
    Status, exakter Belegnummer und Kontakt.

    Archivierte Belege sind standardmäßig ausgeblendet; --include-archived
    zeigt sie. Am Ende wird die Gesamtzahl ausgegeben.
    """
    s = state(ctx)
    with working("Lade Belege …"):
        result = services.list_vouchers(
            s.client,
            voucher_type=voucher_type,
            status=voucher_status,
            number=voucher_number,
            contact_id=contact_id,
            include_archived=include_archived,
            limit=limit,
            fetch_all=fetch_all,
        )
    render(result.items, s.output_format, columns=LIST_COLUMNS, output_path=s.output_path)
    note = None if include_archived else ARCHIVED_HINT
    print_count(len(result.items), result.total, noun="Belege", note=note)


@app.command("get")
def get_voucher(
    ctx: typer.Context,
    voucher: str = typer.Argument(..., help="UUID oder Belegnummer."),
) -> None:
    """Einen Beleg im Detail abrufen. Akzeptiert UUID oder Belegnummer."""
    s = state(ctx)
    with working("Lade Beleg …"):
        data = services.get_voucher(s.client, voucher)
    render(data, s.output_format, output_path=s.output_path)


@app.command("create-draft")
def create_draft(
    ctx: typer.Context,
    body: str = typer.Option(..., "--body", help="JSON-Body inline oder @datei.json."),
) -> None:
    """Neuen Beleg anlegen."""
    s = state(ctx)
    payload = load_json_arg(body)
    with working("Lege Beleg an …"):
        result = services.create_voucher(s.client, payload)
    render(result, s.output_format, output_path=s.output_path)
