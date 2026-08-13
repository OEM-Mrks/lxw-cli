from __future__ import annotations

from pathlib import Path

import typer

from lxw_cli.commands._common import state
from lxw_cli.core import services
from lxw_cli.output import render, working, write_binary

app = typer.Typer(no_args_is_help=True)


@app.command(
    "create",
    epilog="""\
[bold cyan]Beispiel[/bold cyan]

Mahnung aus Rechnung: [green]lxw dunnings create FB2600682[/green]
""",
)
def create_dunning(
    ctx: typer.Context,
    invoice: str = typer.Argument(..., help="UUID oder Rechnungsnummer der Rechnung."),
) -> None:
    """Eine Mahnung aus einer überfälligen Rechnung erstellen.

    Führt die Rechnung zur Mahnung fort (POST /v1/dunnings mit
    precedingSalesVoucherId). Die Rechnung muss festgeschrieben sein und noch
    einen offenen Betrag haben — sonst kommt eine klare Meldung. Die Mahnung
    entsteht als Entwurf; ein erneuter Aufruf erhöht die Mahnstufe. Merke dir
    die zurückgegebene id — Mahnungen sind nur über die id abrufbar.
    """
    s = state(ctx)
    with working("Erstelle Mahnung …"):
        result = services.create_dunning(s.client, invoice)
    render(result, s.output_format, output_path=s.output_path)


@app.command("get")
def get_dunning(
    ctx: typer.Context,
    dunning_id: str = typer.Argument(..., help="id der Mahnung (keine Belegnummer)."),
) -> None:
    """Eine Mahnung im Detail abrufen.

    Mahnungen sind nur über ihre id erreichbar (nicht über eine Belegnummer) —
    die id stammt aus dem Erstellen der Mahnung oder den verknüpften Belegen
    der zugehörigen Rechnung.
    """
    s = state(ctx)
    with working("Lade Mahnung …"):
        data = services.get_dunning(s.client, dunning_id)
    render(data, s.output_format, output_path=s.output_path)


@app.command("pdf")
def download_pdf(
    ctx: typer.Context,
    dunning_id: str = typer.Argument(..., help="id der Mahnung."),
    output: Path | None = typer.Option(
        None,
        "--output",
        "-o",
        help="Ziel-Datei oder -Verzeichnis (Standard: aktuelles Verzeichnis).",
    ),
) -> None:
    """PDF einer Mahnung herunterladen (nur festgeschriebene Mahnungen)."""
    s = state(ctx)
    with working("Lade PDF …"):
        data = services.download_dunning_pdf(s.client, dunning_id)
    write_binary(data, output, default_name=f"dunning-{dunning_id}.pdf")
