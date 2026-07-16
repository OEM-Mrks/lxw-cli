from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from getpass import getpass
from pathlib import Path

from dotenv import dotenv_values, find_dotenv, load_dotenv

from lexware_cli.core.errors import ConfigError

DEFAULT_BASE_URL = "https://api.lexware.io"
ENV_KEY = "LEXWARE_API_KEY"
ENV_BASE_URL = "LEXWARE_API_BASE_URL"


@dataclass(frozen=True)
class Config:
    api_key: str
    base_url: str = DEFAULT_BASE_URL


def config_dir() -> Path:
    """User-level config directory for lexware-cli.

    Honors `LEXWARE_CONFIG_DIR` (explicit override, mostly for tests), then
    `XDG_CONFIG_HOME`, and finally defaults to ~/.config/lexware. This is the
    one stable location the CLI looks at regardless of the current directory.
    """
    override = os.environ.get("LEXWARE_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    root = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return root / "lexware"


def global_env_path() -> Path:
    return config_dir() / ".env"


def load_config() -> Config:
    """Resolve the API key and base URL — non-interactively.

    Precedence: already-set environment variable > project-local `.env`
    (searched from the CWD upward) > the user-level global `.env`. Raises
    :class:`ConfigError` if no key is found. This function never prompts, so it
    is safe for the MCP server, pipelines and the core layer. Frontends that
    want the first-run prompt call :func:`load_config_interactive`.
    """
    _load_env_files()
    api_key = os.getenv(ENV_KEY, "").strip()
    if not api_key:
        raise ConfigError(
            "LEXWARE_API_KEY ist nicht gesetzt. "
            "Generiere einen API-Key unter https://app.lexware.de/addons/public-api "
            "und exportiere ihn als Environment-Variable oder lege ihn in einer .env-Datei ab. "
            "In einem interaktiven Terminal fragt `lexware` beim ersten Aufruf "
            "automatisch danach und speichert ihn unter ~/.config/lexware/.env."
        )
    base_url = os.getenv(ENV_BASE_URL, DEFAULT_BASE_URL).rstrip("/")
    return Config(api_key=api_key, base_url=base_url)


def load_config_interactive() -> Config:
    """Like :func:`load_config`, but prompt for the key on a TTY.

    Used by the user-facing frontends (CLI, TUI): if no key is configured and
    we're on an interactive terminal, prompt for one, validate and persist it,
    then resolve again. Non-interactive contexts just re-raise the ConfigError.
    """
    try:
        return load_config()
    except ConfigError:
        if not _can_prompt():
            raise
        _prompt_and_store_key()  # validates + persists to the global .env
        return load_config()


def _load_env_files() -> None:
    # Project-local .env (searched from the CWD upward) wins for development,
    # then the user-level global config fills in anything still unset. Neither
    # overrides a variable that is already exported in the environment.
    project = find_dotenv(usecwd=True)
    if project:
        _apply_project_env(Path(project))
    global_env = global_env_path()
    if global_env.is_file():
        load_dotenv(global_env, override=False)


def _apply_project_env(path: Path) -> None:
    """Apply a project-local .env — except the API base URL.

    A `.env` discovered by walking up from the CWD is not necessarily ours:
    running the CLI inside a foreign directory must not let that directory
    redirect API requests (and with them the Bearer key) to another host. The
    base URL is therefore only honored from the real environment or the global
    config; a deviating value here is ignored with a warning. The key itself
    is safe to take — worst case requests fail with 401.
    """
    values = dotenv_values(path)
    foreign_base = (values.pop(ENV_BASE_URL, None) or "").rstrip("/")
    if foreign_base and foreign_base != _trusted_base_url():
        print(
            f"Warnung: {ENV_BASE_URL} aus {path} wird ignoriert — die Base-URL "
            f"wird nur aus der Umgebung oder {global_env_path()} übernommen.",
            file=sys.stderr,
        )
    for key, value in values.items():
        if value is not None and key not in os.environ:
            os.environ[key] = value


def _trusted_base_url() -> str:
    """The base URL from trusted sources only: environment, then global .env."""
    env_value = os.environ.get(ENV_BASE_URL)
    if env_value:
        return env_value.rstrip("/")
    global_env = global_env_path()
    if global_env.is_file():
        stored = dotenv_values(global_env).get(ENV_BASE_URL)
        if stored:
            return stored.rstrip("/")
    return DEFAULT_BASE_URL


def _can_prompt() -> bool:
    """True only in an interactive session.

    Guards against prompting in non-interactive contexts — the MCP stdio
    server, pipelines, cron — where a blocking prompt would hang or corrupt
    the protocol.
    """
    return sys.stdin.isatty() and sys.stderr.isatty()


def _prompt_and_store_key() -> str:
    # Imported lazily: client imports this module at import time, so importing
    # it here avoids a circular import while still letting us validate the key.
    from lexware_cli.core.client import LexwareClient
    from lexware_cli.core.errors import LexwareAPIError
    from lexware_cli.output import err_console

    err_console.print(
        "[yellow]Kein Lexware API-Key gefunden.[/yellow]\n"
        "Erzeuge einen unter https://app.lexware.de/addons/public-api "
        "und füge ihn unten ein (die Eingabe wird nicht angezeigt)."
    )
    base_url = os.getenv(ENV_BASE_URL, DEFAULT_BASE_URL).rstrip("/")

    for attempt in range(1, 4):
        try:
            api_key = getpass("Lexware API-Key: ").strip()
        except (EOFError, KeyboardInterrupt):
            raise ConfigError("Abgebrochen — kein API-Key eingegeben.") from None
        if not api_key:
            err_console.print("[red]Leere Eingabe.[/red] Bitte erneut versuchen.")
            continue

        # Validate before persisting so we never store a key that doesn't work.
        profile: dict | None = None
        try:
            with LexwareClient(Config(api_key=api_key, base_url=base_url)) as probe:
                profile = probe.get("/v1/profile")
        except LexwareAPIError as exc:
            if exc.status_code in (401, 403):
                err_console.print(
                    f"[red]Key abgelehnt (HTTP {exc.status_code}).[/red] "
                    f"Versuch {attempt}/3."
                )
                continue
            # Network / server-side problem — let the user save it anyway.
            err_console.print(f"[yellow]Konnte den Key nicht prüfen:[/yellow] {exc}")
            if not _confirm("Trotzdem speichern?"):
                continue

        path = store_key(api_key)
        os.environ[ENV_KEY] = api_key
        who = ""
        if isinstance(profile, dict):
            name = profile.get("companyName") or profile.get("organizationId")
            if name:
                who = f" — angemeldet als [bold]{name}[/bold]"
        err_console.print(f"[green]✓[/green] API-Key gespeichert in {path}{who}.")
        return api_key

    raise ConfigError("Kein gültiger API-Key nach 3 Versuchen eingegeben.")


def _confirm(question: str) -> bool:
    try:
        answer = input(f"{question} [j/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer in ("j", "ja", "y", "yes")


def store_key(api_key: str) -> Path:
    """Persist the API key to the global `.env` with owner-only permissions.

    Existing entries other than the key line are preserved, so a manually
    added `LEXWARE_API_BASE_URL` survives a re-prompt.
    """
    path = global_env_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass

    lines: list[str] = []
    if path.is_file():
        lines = [
            line
            for line in path.read_text(encoding="utf-8").splitlines()
            if not line.strip().startswith(f"{ENV_KEY}=")
        ]
    lines.append(f"{ENV_KEY}={api_key}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)
    return path
