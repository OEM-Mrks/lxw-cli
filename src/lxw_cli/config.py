from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from getpass import getpass
from pathlib import Path

from dotenv import dotenv_values, find_dotenv, load_dotenv

from lxw_cli.core.errors import ConfigError

DEFAULT_BASE_URL = "https://api.lexware.io"
ENV_KEY = "LEXWARE_API_KEY"
ENV_BASE_URL = "LEXWARE_API_BASE_URL"
ENV_PROFILE = "LEXWARE_PROFILE"

# Name of the implicit profile backed by the classic global `.env`.
DEFAULT_PROFILE = "default"
_PROFILE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


@dataclass(frozen=True)
class Config:
    api_key: str
    base_url: str = DEFAULT_BASE_URL
    # Named profile the key came from; None for the classic single-key setup.
    profile: str | None = None


def config_dir() -> Path:
    """User-level config directory for lxw-cli.

    Honors `LEXWARE_CONFIG_DIR` (explicit override, mostly for tests), then
    `XDG_CONFIG_HOME`, and finally defaults to ~/.config/lexware — on Windows
    to %APPDATA%\\lexware. This is the one stable location the CLI looks at
    regardless of the current directory.
    """
    override = os.environ.get("LEXWARE_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg).expanduser() / "lexware"
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        root = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
        return root / "lexware"
    return Path.home() / ".config" / "lexware"


def global_env_path() -> Path:
    return config_dir() / ".env"


# ---------------------------------------------------------------------------
# Profiles — several Lexware installations side by side
# ---------------------------------------------------------------------------


def profiles_dir() -> Path:
    return config_dir() / "profiles"


def validate_profile_name(name: str) -> str:
    """Normalize and check a profile name (it ends up in file and MCP names)."""
    normalized = name.strip().lower()
    if not _PROFILE_RE.match(normalized):
        raise ConfigError(
            f"Ungültiger Profilname {name!r} — erlaubt sind Kleinbuchstaben, "
            "Ziffern, '-' und '_' (max. 32 Zeichen, Beginn mit Buchstabe/Ziffer)."
        )
    return normalized


def profile_env_path(name: str) -> Path:
    """The `.env` holding a profile's key. `default` is the classic global one."""
    name = validate_profile_name(name)
    if name == DEFAULT_PROFILE:
        return global_env_path()
    return profiles_dir() / f"{name}.env"


def active_profile(explicit: str | None = None) -> str | None:
    """The selected named profile, or None for the classic resolution.

    An explicit argument wins over `LEXWARE_PROFILE`. `default` means the
    classic global `.env` and is reported as None, so the existing
    precedence (env var > project .env > global .env) stays untouched.
    """
    raw = explicit if explicit is not None else os.environ.get(ENV_PROFILE, "")
    if not raw or not raw.strip():
        return None
    name = validate_profile_name(raw)
    return None if name == DEFAULT_PROFILE else name


def list_profiles() -> list[str]:
    """All profiles with a stored key; `default` first if the global key exists."""
    names: list[str] = []
    if _stored_key(global_env_path()):
        names.append(DEFAULT_PROFILE)
    directory = profiles_dir()
    if directory.is_dir():
        for path in sorted(directory.glob("*.env")):
            if _PROFILE_RE.match(path.stem) and _stored_key(path):
                names.append(path.stem)
    return names


def _stored_key(path: Path) -> str:
    if not path.is_file():
        return ""
    return (dotenv_values(path).get(ENV_KEY) or "").strip()


def _load_profile_config(name: str) -> Config:
    """Resolve a named profile — strictly from its own file.

    Deliberately ignores `LEXWARE_API_KEY` from the environment or a project
    `.env`: a globally exported key must never silently redirect a profile
    (e.g. the demo MCP server) to another Lexware installation.
    """
    path = profile_env_path(name)
    values = dotenv_values(path) if path.is_file() else {}
    api_key = (values.get(ENV_KEY) or "").strip()
    if not api_key:
        raise ConfigError(
            f"Profil '{name}' hat keinen API-Key ({path}). "
            f"Anlegen mit: lxw profiles add {name}"
        )
    base_url = (values.get(ENV_BASE_URL) or "").strip() or _trusted_base_url()
    return Config(api_key=api_key, base_url=base_url.rstrip("/"), profile=name)


def load_config(profile: str | None = None) -> Config:
    """Resolve the API key and base URL — non-interactively.

    Precedence: already-set environment variable > project-local `.env`
    (searched from the CWD upward) > the user-level global `.env`. Raises
    :class:`ConfigError` if no key is found. This function never prompts, so it
    is safe for the MCP server, pipelines and the core layer. Frontends that
    want the first-run prompt call :func:`load_config_interactive`.

    With a named profile (argument or `LEXWARE_PROFILE`) the key comes
    exclusively from that profile's file — see :func:`_load_profile_config`.
    """
    name = active_profile(profile)
    if name is not None:
        return _load_profile_config(name)
    _load_env_files()
    api_key = os.getenv(ENV_KEY, "").strip()
    if not api_key:
        raise ConfigError(
            "LEXWARE_API_KEY ist nicht gesetzt. "
            "Generiere einen API-Key unter https://app.lexware.de/addons/public-api "
            "und exportiere ihn als Environment-Variable oder lege ihn in einer .env-Datei ab. "
            "In einem interaktiven Terminal fragt `lxw` beim ersten Aufruf "
            "automatisch danach und speichert ihn unter ~/.config/lexware/.env."
        )
    base_url = os.getenv(ENV_BASE_URL, DEFAULT_BASE_URL).rstrip("/")
    return Config(api_key=api_key, base_url=base_url)


def load_config_interactive(profile: str | None = None) -> Config:
    """Like :func:`load_config`, but prompt for the key on a TTY.

    Used by the user-facing frontends (CLI, TUI): if no key is configured and
    we're on an interactive terminal, prompt for one, validate and persist it,
    then resolve again. Non-interactive contexts just re-raise the ConfigError.
    """
    name = active_profile(profile)
    try:
        return load_config(profile)
    except ConfigError:
        if not _can_prompt():
            raise
        _prompt_and_store_key(name)  # validates + persists the key
        return load_config(profile)


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
        # Skip empty values: a bare `LEXWARE_API_KEY=` line (e.g. a filled-in
        # copy of .env.example left blank) must not mask the global config.
        if value and key not in os.environ:
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


def _prompt_and_store_key(profile: str | None = None) -> str:
    # Imported lazily: client imports this module at import time, so importing
    # it here avoids a circular import while still letting us validate the key.
    from lxw_cli.core.client import LexwareClient
    from lxw_cli.core.errors import LexwareAPIError
    from lxw_cli.output import err_console

    label = f" für Profil '{profile}'" if profile else ""
    err_console.print(
        f"[yellow]Kein Lexware API-Key{label} gefunden.[/yellow]\n"
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
        company: dict | None = None
        try:
            with LexwareClient(Config(api_key=api_key, base_url=base_url)) as probe:
                company = probe.get("/v1/profile")
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

        path = store_key(api_key, profile)
        if profile is None:
            os.environ[ENV_KEY] = api_key
        who = ""
        if isinstance(company, dict):
            name = company.get("companyName") or company.get("organizationId")
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


def store_key(api_key: str, profile: str | None = None) -> Path:
    """Persist the API key with owner-only permissions.

    Without a profile (or with `default`) it goes to the global `.env`,
    otherwise to `profiles/<name>.env`. Existing entries other than the key
    line are preserved, so a manually added `LEXWARE_API_BASE_URL` survives
    a re-prompt.
    """
    path = profile_env_path(profile) if profile else global_env_path()
    for directory in {config_dir(), path.parent}:
        directory.mkdir(parents=True, exist_ok=True)
        try:
            # Owner-only permissions; effectively a no-op on Windows (ACLs of
            # the user profile already restrict access there).
            os.chmod(directory, 0o700)
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
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path
