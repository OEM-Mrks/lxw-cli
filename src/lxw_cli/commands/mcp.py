"""Subcommand `lxw mcp …` — manages Claude Code/Desktop integration."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import typer
from dotenv import dotenv_values

from lxw_cli.config import (
    ENV_KEY,
    active_profile,
    global_env_path,
    load_config,
    store_key,
)
from lxw_cli.core.errors import LexwareError
from lxw_cli.output import console, err_console

app = typer.Typer(no_args_is_help=True)

MCP_NAME = "lexware"

PROFILE_HELP = (
    "Lexware-Installation (Profil, siehe `lxw profiles`). Registriert einen "
    "eigenen Server 'lexware-<profil>' — mehrere Installationen laufen so "
    "parallel. Ohne Angabe: globales --profile bzw. der Standard-Key."
)


def _resolve_profile(ctx: typer.Context, profile: str | None) -> str | None:
    """Local `--profile` wins over the global `lxw --profile`; `default` → None."""
    if profile is None:
        profile = getattr(ctx.obj, "profile", None)
    try:
        return active_profile(profile or "")
    except LexwareError as exc:
        err_console.print(f"[red]Fehler:[/red] {exc}")
        raise typer.Exit(code=2) from exc


def mcp_name(profile: str | None) -> str:
    """Server name in the Claude config: `lexware` or `lexware-<profile>`."""
    return f"{MCP_NAME}-{profile}" if profile else MCP_NAME


def _is_lexware_name(name: str) -> bool:
    return name == MCP_NAME or name.startswith(f"{MCP_NAME}-")


def _load_or_exit(profile: str | None):
    try:
        return load_config(profile or "")
    except LexwareError as exc:
        err_console.print(f"[red]Fehler:[/red] {exc}")
        raise typer.Exit(code=2) from exc


def _desktop_config_dirs() -> list[Path]:
    """Candidate directories for Claude Desktop's configuration, per platform.

    On Windows the classic install uses %APPDATA%\\Claude; the Microsoft-Store
    build (MSIX) virtualizes AppData under
    %LOCALAPPDATA%\\Packages\\Claude_<hash>\\LocalCache\\Roaming\\Claude.
    """
    if sys.platform == "darwin":
        return [Path.home() / "Library" / "Application Support" / "Claude"]
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        root = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
        dirs = [root / "Claude"]
        local = os.environ.get("LOCALAPPDATA")
        lroot = Path(local) if local else Path.home() / "AppData" / "Local"
        dirs += sorted((lroot / "Packages").glob("Claude_*/LocalCache/Roaming/Claude"))
        return dirs
    return [Path.home() / ".config" / "Claude"]


def desktop_config_path() -> Path:
    """Location of Claude Desktop's `claude_desktop_config.json`.

    Claude Desktop (and Cowork) read MCP servers from this file — registering
    via `claude mcp add` only covers Claude Code. `CLAUDE_DESKTOP_CONFIG`
    overrides the path (mostly for tests). Among the platform candidates the
    one already holding a config file wins, then the first existing directory,
    then the classic default.
    """
    override = os.environ.get("CLAUDE_DESKTOP_CONFIG")
    if override:
        return Path(override).expanduser()
    candidates = _desktop_config_dirs()
    for d in candidates:
        if (d / "claude_desktop_config.json").is_file():
            return d / "claude_desktop_config.json"
    for d in candidates:
        if d.is_dir():
            return d / "claude_desktop_config.json"
    return candidates[0] / "claude_desktop_config.json"


def _load_desktop_config(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        err_console.print(
            f"[red]Fehler:[/red] {path} enthält kein gültiges JSON ({exc}). "
            "Bitte die Datei prüfen — sie wird nicht angefasst."
        )
        raise typer.Exit(code=2) from exc
    if not isinstance(data, dict):
        err_console.print(f"[red]Fehler:[/red] {path} ist kein JSON-Objekt.")
        raise typer.Exit(code=2)
    return data


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


def _server_command(profile: str | None) -> list[str]:
    """The launch command, pinned to a profile via `--profile` if given.

    The profile travels as a visible argument (not an env var in the Claude
    config), so `lxw mcp status` and the config file show at a glance which
    installation each server talks to. The key itself stays in the profile
    file and never enters the Claude config.
    """
    cmd = _mcp_command()
    return [*cmd, "--profile", profile] if profile else cmd


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
    ctx: typer.Context,
    profile: str | None = typer.Option(None, "--profile", "-p", help=PROFILE_HELP),
    scope: str = typer.Option(
        "user", "--scope", help="user (global), project (per repo), oder local."
    ),
    force: bool = typer.Option(
        False, "--force", help="Vorher entfernen, falls bereits registriert."
    ),
) -> None:
    """Registriert den Lexware-MCP-Server bei Claude Code."""
    _check_claude()
    profile = _resolve_profile(ctx, profile)
    name = mcp_name(profile)
    config = _load_or_exit(profile)  # validates the key
    if profile is None:
        _ensure_global_key(config.api_key)

    server_cmd = _server_command(profile)

    if force:
        subprocess.run(
            ["claude", "mcp", "remove", name, "--scope", scope],
            check=False,
            capture_output=True,
        )

    cmd = [
        "claude",
        "mcp",
        "add",
        name,
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
        f"[green]✓[/green] Lexware MCP-Server '{name}' bei Claude Code registriert "
        f"(scope={scope})."
    )
    console.print(
        "Test: Öffne Claude Code und frag z.B. "
        "[bold]\"Wie viele offene Rechnungen habe ich?\"[/bold]"
    )


@app.command("uninstall-claude")
def uninstall_claude(
    ctx: typer.Context,
    profile: str | None = typer.Option(None, "--profile", "-p", help=PROFILE_HELP),
    scope: str = typer.Option("user", "--scope", help="Gleicher Scope wie beim Install."),
) -> None:
    """Entfernt den Lexware-MCP-Server aus Claude Code."""
    _check_claude()
    name = mcp_name(_resolve_profile(ctx, profile))
    result = subprocess.run(
        ["claude", "mcp", "remove", name, "--scope", scope],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        err_console.print(f"[red]Fehler:[/red] {result.stderr}")
        raise typer.Exit(code=result.returncode)
    console.print(f"[green]✓[/green] Lexware MCP-Server '{name}' entfernt (scope={scope}).")


@app.command("install-desktop")
def install_desktop(
    ctx: typer.Context,
    profile: str | None = typer.Option(None, "--profile", "-p", help=PROFILE_HELP),
    force: bool = typer.Option(
        False, "--force", help="Bestehenden Eintrag überschreiben."
    ),
) -> None:
    """Registriert den Lexware-MCP-Server bei Claude Desktop (und damit Cowork).

    Claude Desktop liest MCP-Server aus `claude_desktop_config.json` —
    `install-claude` deckt nur Claude Code ab. Wichtig: Claude Desktop vorher
    komplett beenden — die laufende App schreibt die Datei aus dem
    Arbeitsspeicher zurück und überschreibt externe Änderungen.
    """
    profile = _resolve_profile(ctx, profile)
    name = mcp_name(profile)
    config = _load_or_exit(profile)  # validates the key
    if profile is None:
        _ensure_global_key(config.api_key)

    path = desktop_config_path()
    if not path.parent.is_dir():
        err_console.print(
            f"[red]Fehler:[/red] {path.parent} existiert nicht — ist Claude "
            "Desktop installiert? (https://claude.com/download)"
        )
        raise typer.Exit(code=2)

    data = _load_desktop_config(path)
    servers = data.setdefault("mcpServers", {})
    if name in servers and not force:
        err_console.print(
            f"[yellow]Hinweis:[/yellow] '{name}' ist bereits in {path} "
            "eingetragen. Mit `--force` überschreiben."
        )
        raise typer.Exit(code=1)

    # Absolute path: GUI apps don't inherit the shell PATH, so a bare
    # 'lxw-mcp' would not resolve when Desktop spawns the server.
    command, *args = _server_command(profile)
    entry: dict[str, Any] = {"command": command}
    if args:
        entry["args"] = args
    servers[name] = entry

    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    console.print(
        f"[green]✓[/green] Lexware MCP-Server '{name}' in {path} eingetragen."
    )
    console.print(
        "[bold]Wichtig:[/bold] Läuft Claude Desktop gerade, kann es diese "
        "Änderung beim nächsten Speichern überschreiben — den Befehl am besten "
        "bei beendeter App ausführen (Quit, nicht nur Fenster schließen) und "
        "Desktop danach neu starten. Gilt auch für Cowork-Sessions."
    )


@app.command("uninstall-desktop")
def uninstall_desktop(
    ctx: typer.Context,
    profile: str | None = typer.Option(None, "--profile", "-p", help=PROFILE_HELP),
) -> None:
    """Entfernt den Lexware-MCP-Server aus Claude Desktop."""
    name = mcp_name(_resolve_profile(ctx, profile))
    path = desktop_config_path()
    data = _load_desktop_config(path)
    servers = data.get("mcpServers") or {}
    if name not in servers:
        console.print(f"[yellow]✗[/yellow] '{name}' ist in {path} nicht eingetragen.")
        raise typer.Exit(code=1)
    del servers[name]
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    console.print(f"[green]✓[/green] Lexware MCP-Server '{name}' aus {path} entfernt.")


@app.command("status")
def status() -> None:
    """Zeigt, wo der Lexware-MCP-Server registriert ist (Claude Code + Desktop)."""
    # Claude Code (via `claude mcp list`)
    if shutil.which("claude"):
        result = subprocess.run(["claude", "mcp", "list"], capture_output=True, text=True)
        lines = [
            line.strip()
            for line in (result.stdout or "").splitlines()
            if _is_lexware_name(line.strip().split(":", 1)[0].strip())
        ]
        if result.returncode == 0 and lines:
            for line in lines:
                console.print(f"[green]✓[/green] Claude Code: {line}")
        else:
            console.print(
                "[yellow]✗[/yellow] Claude Code: nicht registriert. "
                "Setup: [bold]lxw mcp install-claude[/bold]"
            )
    else:
        console.print("[dim]– Claude Code: `claude` CLI nicht gefunden.[/dim]")

    # Claude Desktop (claude_desktop_config.json)
    path = desktop_config_path()
    if not path.is_file():
        console.print(f"[dim]– Claude Desktop: keine Konfiguration ({path}).[/dim]")
        return
    servers = _load_desktop_config(path).get("mcpServers") or {}
    names = sorted(n for n in servers if _is_lexware_name(n))
    for name in names:
        entry = servers[name]
        cmd = " ".join([entry.get("command", "?"), *entry.get("args", [])])
        console.print(f"[green]✓[/green] Claude Desktop: {name}: {cmd}")
    if not names:
        console.print(
            "[yellow]✗[/yellow] Claude Desktop: nicht registriert. "
            "Setup: [bold]lxw mcp install-desktop[/bold]"
        )


@app.command("serve")
def serve(
    ctx: typer.Context,
    profile: str | None = typer.Option(None, "--profile", "-p", help=PROFILE_HELP),
) -> None:
    """Startet den MCP-Server (intern von Claude Code aufgerufen — selten manuell nötig)."""
    from lxw_cli.mcp_server import serve_stdio

    serve_stdio(_resolve_profile(ctx, profile))
