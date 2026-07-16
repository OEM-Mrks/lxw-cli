from __future__ import annotations

import typer

from lxw_cli.commands._common import load_json_arg, state
from lxw_cli.core import services
from lxw_cli.output import print_count, render, working

app = typer.Typer(no_args_is_help=True)

LIST_COLUMNS = ["id", "type", "title", "articleNumber", "gtin", "unitName"]


@app.command(
    "list",
    epilog="""\
[bold cyan]Beispiele[/bold cyan]

Volltextsuche nach Bezeichnung: [green]lxw articles list --search schraube[/green]

Teil-Artikelnummer: [green]lxw articles list -q SCH-[/green]

Exakte Artikelnummer (schnell): [green]lxw articles list --number SCH-001[/green]

Nur Dienstleistungen, alle: [green]lxw articles list --type service --all[/green]
""",
)
def list_articles(
    ctx: typer.Context,
    limit: int = typer.Option(
        25, "--limit", "-n", help="Maximale Anzahl Einträge (0 = alle)."
    ),
    fetch_all: bool = typer.Option(
        False, "--all", "-a", help="Alle Treffer laden (überschreibt --limit)."
    ),
    search: str | None = typer.Option(
        None,
        "--search",
        "-q",
        help="Freitext in Bezeichnung, Beschreibung und Artikelnummer (Teiltreffer).",
    ),
    article_type: str | None = typer.Option(None, "--type", help="product|service"),
    article_number: str | None = typer.Option(
        None, "--number", help="Exakte Artikelnummer (serverseitig)."
    ),
    gtin: str | None = typer.Option(None, "--gtin", help="Exakte GTIN/EAN (serverseitig)."),
) -> None:
    """Artikel auflisten.

    Zeigt standardmäßig bis zu 25 Artikel (--all bzw. --limit 0 lädt alle).
    Die Lexware-API filtert nur exakt: --number (Artikelnummer), --gtin und
    --type (product|service).

    Für eine unscharfe Suche gibt es --search/-q: durchsucht clientseitig
    Bezeichnung, Beschreibung und Artikelnummer nach Teiltreffern (Groß-/
    Kleinschreibung egal) und zeigt am Ende die Trefferzahl.
    """
    s = state(ctx)
    with working("Suche Artikel …" if search else "Lade Artikel …"):
        result = services.list_articles(
            s.client,
            search=search,
            article_type=article_type,
            article_number=article_number,
            gtin=gtin,
            limit=limit,
            fetch_all=fetch_all,
        )
    render(result.items, s.output_format, columns=LIST_COLUMNS, output_path=s.output_path)
    if result.search:
        # Client-side search: the API total counts all articles, not matches.
        print_count(len(result.items), noun="Treffer")
    else:
        print_count(len(result.items), result.total, noun="Artikel")


@app.command("get")
def get_article(ctx: typer.Context, article_id: str) -> None:
    """Einen Artikel im Detail abrufen."""
    s = state(ctx)
    with working("Lade Artikel …"):
        data = services.get_article(s.client, article_id)
    render(data, s.output_format, output_path=s.output_path)


@app.command(
    "create",
    epilog="""\
[bold cyan]Beispiele[/bold cyan]

Aus Datei: [green]lxw articles create --body @artikel.json[/green]

Inline-Minimal: [green]lxw articles create --body '{"type":"SERVICE","title":"…"}'[/green]
""",
)
def create(
    ctx: typer.Context,
    body: str = typer.Option(..., "--body", help="JSON-Body inline oder @datei.json."),
) -> None:
    """Neuen Artikel anlegen (Stammdaten, kein Draft).

    Erwartet den JSON-Body der Lexware-API — inline oder als @pfad.json
    (type PRODUCT|SERVICE, title, unitName, price …). Schema siehe
    https://developers.lexware.io/docs/.
    """
    s = state(ctx)
    payload = load_json_arg(body)
    with working("Lege Artikel an …"):
        result = services.create_article(s.client, payload)
    render(result, s.output_format, output_path=s.output_path)
