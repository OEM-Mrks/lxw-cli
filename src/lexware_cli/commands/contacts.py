from __future__ import annotations

from typing import Any

import typer

from lexware_cli.commands._common import load_json_arg, state
from lexware_cli.core import services
from lexware_cli.output import OutputFormat, err_console, print_count, render, working

app = typer.Typer(no_args_is_help=True)

LIST_COLUMNS = ["id", "name", "role", "number", "email", "archived"]


@app.command(
    "list",
    epilog="""\
[bold cyan]Beispiele[/bold cyan]

Nur Kunden: [green]lexware contacts list --customer[/green]

Nur Lieferanten: [green]lexware contacts list --vendor[/green]

Nach Name suchen: [green]lexware contacts list --name Müller[/green]

Getrennt nach Rolle: [green]lexware contacts list --grouped[/green]

Inkl. archivierte (CSV): [green]lexware --csv contacts list --all --include-archived[/green]
""",
)
def list_contacts(
    ctx: typer.Context,
    limit: int = typer.Option(
        25, "--limit", "-n", help="Maximale Anzahl Einträge (0 = alle)."
    ),
    fetch_all: bool = typer.Option(
        False, "--all", "-a", help="Alle Treffer laden (überschreibt --limit)."
    ),
    name: str | None = typer.Option(None, "--name", help="Filter nach Name (min. 3 Zeichen)."),
    email: str | None = typer.Option(None, "--email", help="Filter nach E-Mail (min. 3 Zeichen)."),
    number: str | None = typer.Option(None, "--number", help="Kunden-/Lieferantennummer."),
    customer: bool = typer.Option(False, "--customer", help="Nur Kunden."),
    vendor: bool = typer.Option(False, "--vendor", help="Nur Lieferanten."),
    grouped: bool = typer.Option(
        False,
        "--grouped/--flat",
        help="Tabelle nach Kunden/Lieferanten gruppieren (Standard: aus).",
    ),
    include_archived: bool = typer.Option(
        False,
        "--include-archived",
        help="Auch archivierte Kontakte anzeigen (Standard: ausgeblendet).",
    ),
) -> None:
    """Kontakte auflisten.

    Zeigt standardmäßig bis zu 25 Kontakte als eine zusammenhängende Liste
    (--all bzw. --limit 0 lädt alle). Filtern nach Name/E-Mail (je min. 3
    Zeichen), Nummer oder Rolle: --customer zeigt nur Kunden, --vendor nur
    Lieferanten.

    Mit --grouped wird die Tabelle in zwei Abschnitte geteilt (zuerst Kunden,
    dann Lieferanten, je mit passender Nummer). Archivierte Kontakte sind
    standardmäßig ausgeblendet; --include-archived zeigt sie. JSON/CSV sind
    immer eine flache Liste mit role-Spalte.
    """
    s = state(ctx)
    with working("Lade Kontakte …"):
        result = services.list_contacts(
            s.client,
            name=name,
            email=email,
            number=number,
            customer=customer,
            vendor=vendor,
            include_archived=include_archived,
            limit=limit,
            fetch_all=fetch_all,
        )
    raw = result.items

    if grouped and s.output_format is OutputFormat.TABLE:
        _render_grouped(raw)
    else:
        items = [_flatten_contact(c) for c in raw]
        render(items, s.output_format, columns=LIST_COLUMNS, output_path=s.output_path)

    if include_archived:
        print_count(len(raw), result.total, noun="Kontakte")
    elif result.exhausted:
        suffix = f" ({result.hidden} archivierte ausgeblendet)" if result.hidden else ""
        err_console.print(
            f"[dim]→ {len(raw)} von {len(raw)} aktiven Kontakten{suffix}[/dim]"
        )
    else:
        err_console.print(
            f"[dim]→ {len(raw)} aktive Kontakte angezeigt "
            "(archivierte ausgeblendet · --all für Gesamtzahl, "
            "--include-archived zeigt alle)[/dim]"
        )


def _render_grouped(raw: list[Any]) -> None:
    """Render contacts as separate Kunden / Lieferanten tables.

    A contact with both roles appears in both tables (its `role` column shows
    'customer+vendor'), and each table shows the number for its own role.
    Contacts without any role land in a trailing 'Ohne Rolle' table.
    """
    sections = (("Kunden", "customer"), ("Lieferanten", "vendor"))
    rendered = False
    for title, role in sections:
        rows = [
            _flatten_contact(c, number_role=role)
            for c in raw
            if role in (c.get("roles") or {})
        ]
        if rows:
            render(rows, OutputFormat.TABLE, columns=LIST_COLUMNS, title=title)
            rendered = True
    others = [_flatten_contact(c) for c in raw if not (c.get("roles") or {})]
    if others:
        render(others, OutputFormat.TABLE, columns=LIST_COLUMNS, title="Ohne Rolle")
        rendered = True
    if not rendered:
        render([], OutputFormat.TABLE, columns=LIST_COLUMNS)


@app.command("get")
def get_contact(ctx: typer.Context, contact_id: str) -> None:
    """Einen Kontakt im Detail abrufen."""
    s = state(ctx)
    with working("Lade Kontakt …"):
        data = services.get_contact(s.client, contact_id)
    render(data, s.output_format, output_path=s.output_path)


@app.command(
    "create",
    epilog="""\
[bold cyan]Beispiele[/bold cyan]

Kunde aus Datei: [green]lexware contacts create --body @kunde.json[/green]

Lieferant aus Datei: [green]lexware contacts create --body @lieferant.json[/green]
""",
)
def create(
    ctx: typer.Context,
    body: str = typer.Option(..., "--body", help="JSON-Body inline oder @datei.json."),
) -> None:
    """Neuen Kontakt anlegen (Stammdaten, kein Draft).

    Erwartet den JSON-Body der Lexware-API — inline oder als @pfad.json. Mit
    'roles' wird die Rolle gesetzt: {"customer":{}} und/oder {"vendor":{}}.
    Minimal: {"roles":{"customer":{}},"company":{"name":"…"}}.
    """
    s = state(ctx)
    payload = load_json_arg(body)
    with working("Lege Kontakt an …"):
        result = services.create_contact(s.client, payload)
    render(result, s.output_format, output_path=s.output_path)


def _flatten_contact(
    c: dict[str, Any], number_role: str | None = None
) -> dict[str, Any]:
    """Project nested contact fields into a flat row for table/CSV display.

    `number_role` ('customer' or 'vendor') picks which role's number to show —
    used by the grouped view so the Lieferanten table shows vendor numbers and
    the Kunden table shows customer numbers. When None, the customer number is
    preferred, then the vendor number.
    """
    company = c.get("company") or {}
    person = c.get("person") or {}
    roles = c.get("roles") or {}
    emails = (c.get("emailAddresses") or {}).get("business") or []

    if company.get("name"):
        name = company["name"]
    elif person:
        name = " ".join(
            p for p in (person.get("firstName"), person.get("lastName")) if p
        ).strip()
    else:
        name = ""

    if "customer" in roles and "vendor" in roles:
        role = "customer+vendor"
    elif "customer" in roles:
        role = "customer"
    elif "vendor" in roles:
        role = "vendor"
    else:
        role = ""

    if number_role is not None:
        number = (roles.get(number_role) or {}).get("number") or ""
    else:
        number = (
            (roles.get("customer") or {}).get("number")
            or (roles.get("vendor") or {}).get("number")
            or ""
        )

    return {
        "id": c.get("id"),
        "name": name,
        "role": role,
        "number": number,
        "email": emails[0] if emails else "",
        "archived": c.get("archived"),
    }
