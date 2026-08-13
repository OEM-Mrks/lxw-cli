from __future__ import annotations

import typer

from lxw_cli.commands._common import state
from lxw_cli.core import services
from lxw_cli.output import print_count, render, working

app = typer.Typer(no_args_is_help=True)

LIST_COLUMNS = ["id", "name", "type", "contactRequired", "splitAllowed"]


@app.command(
    "list",
    epilog="""\
[bold cyan]Beispiele[/bold cyan]

Alle Kategorien: [green]lxw posting-categories list[/green]

Nur Einnahmen: [green]lxw posting-categories list --type income[/green]
""",
)
def list_posting_categories(
    ctx: typer.Context,
    category_type: str | None = typer.Option(
        None,
        "--type",
        help="Nur ein Typ (z.B. income oder expense) — clientseitig gefiltert.",
    ),
) -> None:
    """Buchungskategorien auflisten.

    Zeigt den festen Kategorienkatalog des Accounts (/v1/posting-categories).
    Die `id` einer Kategorie wird beim Anlegen eines Belegs pro Position als
    `categoryId` referenziert. Der Endpunkt liefert eine einfache Liste (keine
    Paginierung); `--type` filtert clientseitig nach exaktem Typ.
    """
    s = state(ctx)
    with working("Lade Buchungskategorien …"):
        items = services.list_posting_categories(s.client, category_type=category_type)
    render(items, s.output_format, columns=LIST_COLUMNS, output_path=s.output_path)
    print_count(len(items), noun="Buchungskategorien")
