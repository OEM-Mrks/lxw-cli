"""Subcommand `lxw profiles …` — several Lexware installations side by side.

Each profile is its own `.env` under `<config dir>/profiles/<name>.env`
(chmod 600). The classic global `.env` is the implicit profile `default`,
so existing single-key setups keep working unchanged.
"""

from __future__ import annotations

import sys
from getpass import getpass

import typer

from lxw_cli.config import (
    DEFAULT_PROFILE,
    Config,
    _trusted_base_url,
    list_profiles,
    load_config,
    profile_env_path,
    store_key,
    validate_profile_name,
)
from lxw_cli.core.client import LexwareClient
from lxw_cli.core.errors import LexwareAPIError, LexwareError
from lxw_cli.output import console, err_console

app = typer.Typer(no_args_is_help=True)


def _name_or_exit(name: str) -> str:
    try:
        return validate_profile_name(name)
    except LexwareError as exc:
        err_console.print(f"[red]Fehler:[/red] {exc}")
        raise typer.Exit(code=2) from exc


def _read_key() -> str:
    """Prompt on a TTY, otherwise take the key from stdin (for scripts)."""
    if sys.stdin.isatty():
        try:
            return getpass("Lexware API-Key: ").strip()
        except (EOFError, KeyboardInterrupt):
            return ""
    return sys.stdin.readline().strip()


def _company(config: Config) -> str:
    with LexwareClient(config) as client:
        profile = client.get("/v1/profile")
    if isinstance(profile, dict):
        return str(profile.get("companyName") or profile.get("organizationId") or "")
    return ""


@app.command("add")
def add(
    name: str = typer.Argument(..., help="Profilname, z.B. 'oemedia' oder 'demo'."),
    force: bool = typer.Option(False, "--force", help="Bestehenden Key ersetzen."),
    no_check: bool = typer.Option(
        False, "--no-check", help="Key nicht gegen die Lexware-API prüfen."
    ),
) -> None:
    """Legt ein Profil an (API-Key wird abgefragt oder von stdin gelesen)."""
    name = _name_or_exit(name)
    if name in list_profiles() and not force:
        err_console.print(
            f"[yellow]Hinweis:[/yellow] Profil '{name}' existiert bereits. "
            "Mit `--force` ersetzen."
        )
        raise typer.Exit(code=1)

    err_console.print(
        f"API-Key für Profil [bold]{name}[/bold] — erzeugen unter "
        "https://app.lexware.de/addons/public-api (Eingabe wird nicht angezeigt)."
    )
    api_key = _read_key()
    if not api_key:
        err_console.print("[red]Fehler:[/red] Kein API-Key eingegeben.")
        raise typer.Exit(code=2)

    who = ""
    if not no_check:
        try:
            who = _company(Config(api_key=api_key, base_url=_trusted_base_url()))
        except LexwareAPIError as exc:
            err_console.print(f"[red]Key abgelehnt:[/red] {exc}")
            raise typer.Exit(code=2) from exc
        except LexwareError as exc:
            err_console.print(f"[red]Key konnte nicht geprüft werden:[/red] {exc}")
            raise typer.Exit(code=2) from exc

    path = store_key(api_key, name)
    suffix = f" — {who}" if who else ""
    console.print(f"[green]✓[/green] Profil '{name}' gespeichert in {path}{suffix}.")
    console.print(
        f"Nutzen: [bold]lxw -p {name} invoices list[/bold] · "
        f"Claude: [bold]lxw mcp install-claude --profile {name}[/bold]"
    )


@app.command("list")
def list_cmd(
    check: bool = typer.Option(
        False, "--check", help="Jeden Key prüfen und den Firmennamen anzeigen."
    ),
) -> None:
    """Zeigt alle Profile mit hinterlegtem API-Key."""
    names = list_profiles()
    if not names:
        console.print(
            "[yellow]Keine Profile.[/yellow] Anlegen mit [bold]lxw profiles add <name>[/bold]."
        )
        return
    for name in names:
        line = f"{name}  [dim]{profile_env_path(name)}[/dim]"
        if check:
            try:
                company = _company(load_config(name))
                line += f"  [green]✓ {company}[/green]"
            except LexwareError as exc:
                line += f"  [red]✗ {exc}[/red]"
        console.print(line)


@app.command("remove")
def remove(
    name: str = typer.Argument(..., help="Zu löschendes Profil."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Ohne Rückfrage löschen."),
) -> None:
    """Löscht ein Profil (die Datei mit dem API-Key)."""
    name = _name_or_exit(name)
    if name == DEFAULT_PROFILE:
        err_console.print(
            "[red]Fehler:[/red] Das Standard-Profil liegt in der globalen .env "
            f"({profile_env_path(name)}) und wird hier nicht gelöscht."
        )
        raise typer.Exit(code=2)
    path = profile_env_path(name)
    if not path.is_file():
        console.print(f"[yellow]✗[/yellow] Profil '{name}' existiert nicht.")
        raise typer.Exit(code=1)
    if not yes and not typer.confirm(f"Profil '{name}' ({path}) löschen?"):
        raise typer.Exit(code=1)
    path.unlink()
    console.print(
        f"[green]✓[/green] Profil '{name}' gelöscht. Eine MCP-Registrierung "
        f"entfernst du mit [bold]lxw mcp uninstall-claude --profile {name}[/bold] "
        "bzw. [bold]uninstall-desktop[/bold]."
    )
