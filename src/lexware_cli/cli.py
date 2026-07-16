from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import typer

from lexware_cli import __version__
from lexware_cli.commands import (
    articles,
    contacts,
    delivery_notes,
    invoices,
    orders,
    quotations,
    vouchers,
)
from lexware_cli.commands import (
    mcp as mcp_cmd,
)
from lexware_cli.config import load_config_interactive
from lexware_cli.core import services
from lexware_cli.core.client import LexwareClient
from lexware_cli.core.errors import LexwareAPIError, LexwareError
from lexware_cli.output import OutputFormat, err_console, render, working

app = typer.Typer(
    name="lexware",
    help=(
        "CLI für die Lexware Office API (https://developers.lexware.io/).\n\n"
        "Liest Rechnungen, Kontakte, Belege, Artikel, Angebote und Lieferscheine "
        "und legt Drafts/Stammdaten an. Globale Ausgabe-Optionen (--json, --csv, "
        "--output) stehen vor dem Befehl; Detailhilfe je Befehl mit "
        "'lexware <befehl> <unterbefehl> --help'."
    ),
    no_args_is_help=True,
    rich_markup_mode="rich",
    pretty_exceptions_show_locals=False,
    epilog="""\
[bold cyan]Beispiele[/bold cyan]

Auth-Test (Firmenprofil): [green]lexware profile[/green]

Offene Rechnungen: [green]lexware invoices list --status open[/green]

Artikel suchen: [green]lexware articles list --search schraube[/green]

Kunden als CSV: [green]lexware --csv -o kunden.csv contacts list --customer --all[/green]

Claude-Integration einrichten: [green]lexware mcp install-claude[/green]
""",
)


@dataclass
class AppState:
    output_format: OutputFormat
    output_path: Path | None
    _client: LexwareClient | None = None

    @property
    def client(self) -> LexwareClient:
        if self._client is None:
            try:
                config = load_config_interactive()
            except LexwareError as exc:
                err_console.print(f"[red]Fehler:[/red] {exc}")
                raise typer.Exit(code=2) from exc
            self._client = LexwareClient(config)
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"lexware-cli {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    ctx: typer.Context,
    json_out: bool = typer.Option(
        False, "--json", help="JSON statt Tabelle ausgeben.", rich_help_panel="Output"
    ),
    csv_out: bool = typer.Option(
        False, "--csv", help="CSV statt Tabelle ausgeben.", rich_help_panel="Output"
    ),
    output: Path | None = typer.Option(
        None,
        "--output",
        "-o",
        help="In Datei schreiben statt auf stdout.",
        rich_help_panel="Output",
    ),
    _version: bool = typer.Option(
        False,
        "--version",
        "-V",
        callback=_version_callback,
        is_eager=True,
        help="Version anzeigen und beenden.",
    ),
) -> None:
    if json_out and csv_out:
        raise typer.BadParameter("--json und --csv schließen sich gegenseitig aus.")
    fmt = (
        OutputFormat.JSON
        if json_out
        else OutputFormat.CSV
        if csv_out
        else OutputFormat.TABLE
    )

    state = AppState(output_format=fmt, output_path=output)
    ctx.obj = state
    ctx.call_on_close(state.close)


@app.command("profile")
def profile_cmd(ctx: typer.Context) -> None:
    """Firmenprofil abrufen (Auth-Test)."""
    state: AppState = ctx.obj
    with working("Lade Firmenprofil …"):
        data = services.get_profile(state.client)
    render(data, state.output_format, output_path=state.output_path)


@app.command("version")
def version_cmd() -> None:
    """Version anzeigen."""
    typer.echo(__version__)


app.add_typer(
    invoices.app,
    name="invoices",
    help="Rechnungen: list/get/pdf/create-draft. Listen mit --status/--number/--all.",
)
app.add_typer(
    contacts.app,
    name="contacts",
    help="Kontakte: list/get/create. Nur Kunden/Lieferanten mit --customer/--vendor.",
)
app.add_typer(
    vouchers.app,
    name="vouchers",
    help="Belege: list/get/create-draft. Listen mit --type/--status/--number, --all.",
)
app.add_typer(
    articles.app,
    name="articles",
    help="Artikel: list/get/create. Volltextsuche mit 'list --search/-q'.",
)
app.add_typer(
    quotations.app, name="quotations", help="Angebote: list/get/pdf/create-draft, --all."
)
app.add_typer(
    orders.app,
    name="orders",
    help="Aufträge (Auftragsbestätigungen): list/get/pdf/create-draft, --all.",
)
app.add_typer(
    delivery_notes.app,
    name="delivery-notes",
    help="Lieferscheine: list/get/pdf/create-draft, --all.",
)
app.add_typer(mcp_cmd.app, name="mcp", help="MCP-Server-Integration für Claude.")


def _wants_tui(argv: list[str], *, stdin_tty: bool, stdout_tty: bool) -> bool:
    """Decide whether the bare `lexware` invocation should open the TUI.

    Only when there are no subcommands/arguments AND we're on a real interactive
    terminal (both stdin and stdout). With arguments it's the CLI; when stdout
    isn't a TTY (piping, scripting) it stays the CLI (Typer prints help) — never
    the TUI, never an error.
    """
    return len(argv) <= 1 and stdin_tty and stdout_tty


def _run() -> None:  # pragma: no cover
    if _wants_tui(
        sys.argv, stdin_tty=sys.stdin.isatty(), stdout_tty=sys.stdout.isatty()
    ):
        from lexware_cli.tui.app import run as run_tui

        run_tui()
        return
    try:
        app()
    except LexwareAPIError as exc:
        err_console.print(f"[red]API-Fehler:[/red] {exc}")
        if isinstance(exc.body, dict) and exc.body:
            err_console.print(exc.body)
        raise SystemExit(1) from exc
    except LexwareError as exc:
        err_console.print(f"[red]Fehler:[/red] {exc}")
        raise SystemExit(1) from exc


if __name__ == "__main__":  # pragma: no cover
    _run()
