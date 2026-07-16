"""Subcommand `lxw mcp …` — manages Claude Code/Desktop integration."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import typer
from dotenv import dotenv_values

from lxw_cli.config import ENV_KEY, global_env_path, load_config, store_key
from lxw_cli.output import console, err_console

app = typer.Typer(no_args_is_help=True)

MCP_NAME = "lexware"


def _check_claude() -> None:
    if not shutil.which("claude"):
        err_console.print(
            "[red]Fehler:[/red] `claude` CLI nicht gefunden. "
            "Installiere zuerst Claude Code (https://claude.com/claude-code)."
        )
        raise typer.Exit(code=2)


def _mcp_command() -> list[str]:
    """Build the command Claude will use to launch the MCP server.

    Prefer the `lxw-mcp` console script when it's on PATH (works for
    pipx-installed setups). Otherwise fall back to the current interpreter
    plus `-m lxw_cli.mcp_server` so the venv path is captured even when
    `.venv/bin` isn't on the system PATH.
    """
    on_path = shutil.which("lxw-mcp")
    if on_path:
        return [on_path]
    # Look for the entry-point next to the running python (typical venv layout;
    # on Windows console scripts get an .exe suffix)
    exe_suffix = ".exe" if sys.platform == "win32" else ""
    candidate = Path(sys.executable).parent / f"lxw-mcp{exe_suffix}"
    if candidate.exists():
        return [str(candidate)]
    return [sys.executable, "-m", "lxw_cli.mcp_server"]


def _ensure_global_key(api_key: str) -> None:
    """Make sure the MCP server can resolve the key from the global config.

    The key is deliberately NOT passed via `claude mcp add -e …` — that would
    expose it in the process list and store a second plaintext copy in the
    Claude config. The server resolves it itself from the global `.env`
    (chmod 600), so we only have to guarantee it exists there.
    """
    path = global_env_path()
    existing = (dotenv_values(path).get(ENV_KEY) or "").strip() if path.is_file() else ""
    if not existing:
        stored = store_key(api_key)
        err_console.print(
            f"[dim]API-Key in {stored} hinterlegt (wird vom MCP-Server gelesen).[/dim]"
        )
    elif existing != api_key:
        err_console.print(
            f"[yellow]Hinweis:[/yellow] {path} enthält einen anderen API-Key — "
            "der MCP-Server verwendet den dort hinterlegten."
        )


@app.command("install-claude")
def install_claude(
    scope: str = typer.Option(
        "user", "--scope", help="user (global), project (per repo), oder local."
    ),
    force: bool = typer.Option(
        False, "--force", help="Vorher entfernen, falls bereits registriert."
    ),
) -> None:
    """Registriert den Lexware-MCP-Server bei Claude Code."""
    _check_claude()
    config = load_config()  # validates LEXWARE_API_KEY
    _ensure_global_key(config.api_key)

    server_cmd = _mcp_command()

    if force:
        subprocess.run(
            ["claude", "mcp", "remove", MCP_NAME, "--scope", scope],
            check=False,
            capture_output=True,
        )

    cmd = [
        "claude",
        "mcp",
        "add",
        MCP_NAME,
        "--scope",
        scope,
        "--",
        *server_cmd,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        err_console.print(f"[red]claude mcp add fehlgeschlagen:[/red]\n{result.stderr}")
        if "already exists" in (result.stderr or "").lower():
            err_console.print(
                "[yellow]Hinweis:[/yellow] Mit `--force` kannst du die "
                "bestehende Registrierung überschreiben."
            )
        raise typer.Exit(code=result.returncode)
    console.print(
        f"[green]✓[/green] Lexware MCP-Server bei Claude Code registriert "
        f"(scope={scope})."
    )
    console.print(
        "Test: Öffne Claude Code und frag z.B. "
        "[bold]\"Wie viele offene Rechnungen habe ich?\"[/bold]"
    )


@app.command("uninstall-claude")
def uninstall_claude(
    scope: str = typer.Option("user", "--scope", help="Gleicher Scope wie beim Install."),
) -> None:
    """Entfernt den Lexware-MCP-Server aus Claude Code."""
    _check_claude()
    result = subprocess.run(
        ["claude", "mcp", "remove", MCP_NAME, "--scope", scope],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        err_console.print(f"[red]Fehler:[/red] {result.stderr}")
        raise typer.Exit(code=result.returncode)
    console.print(f"[green]✓[/green] Lexware MCP-Server entfernt (scope={scope}).")


@app.command("status")
def status() -> None:
    """Zeigt, ob der Lexware-MCP-Server aktuell bei Claude Code registriert ist."""
    _check_claude()
    result = subprocess.run(
        ["claude", "mcp", "list"], capture_output=True, text=True
    )
    if result.returncode != 0:
        err_console.print(f"[red]Fehler:[/red] {result.stderr}")
        raise typer.Exit(code=result.returncode)
    if MCP_NAME in result.stdout:
        for line in result.stdout.splitlines():
            if MCP_NAME in line:
                console.print(f"[green]✓[/green] {line.strip()}")
    else:
        console.print(
            "[yellow]✗[/yellow] Lexware ist nicht registriert. "
            "Setup: [bold]lxw mcp install-claude[/bold]"
        )


@app.command("serve")
def serve() -> None:
    """Startet den MCP-Server (intern von Claude Code aufgerufen — selten manuell nötig)."""
    from lxw_cli.mcp_server import run as run_server

    run_server()
