from __future__ import annotations

import csv
import io
import json
import re
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from enum import StrEnum
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.table import Table

from lxw_cli.core.errors import LexwareError

console = Console()
err_console = Console(stderr=True)


@contextmanager
def working(message: str = "Arbeitet …") -> Iterator[None]:
    """Show an animated spinner on stderr while the wrapped block runs.

    Renders nothing when stderr is not a terminal (pipes, redirects, the MCP
    server), so machine-readable output on stdout is never disturbed. The
    spinner is transient — it disappears once the block completes.
    """
    if not err_console.is_terminal:
        yield
        return
    with err_console.status(f"[cyan]{message}[/cyan]", spinner="dots"):
        yield


class OutputFormat(StrEnum):
    TABLE = "table"
    JSON = "json"
    CSV = "csv"


def render(
    data: Any,
    fmt: OutputFormat,
    *,
    columns: list[str] | None = None,
    title: str | None = None,
    output_path: Path | None = None,
) -> None:
    if fmt is OutputFormat.JSON:
        _emit(json.dumps(data, indent=2, ensure_ascii=False, default=str), output_path)
        return

    rows = _coerce_rows(data)

    if fmt is OutputFormat.CSV:
        _emit(_to_csv(rows, columns), output_path)
        return

    _render_table(rows, columns=columns, title=title)


def print_count(
    shown: int,
    total: int | None = None,
    *,
    noun: str = "Datensätze",
    note: str | None = None,
) -> None:
    """Print a record-count summary to stderr (keeps stdout pipe-clean).

    Always "X von Y" when the total is known, so it is immediately visible
    whether more records exist than were fetched:

    - capped → "25 von 1234 <noun> angezeigt (mehr mit --all)"
    - complete → "1234 von 1234 <noun>"
    - total unknown → "<shown> <noun>"

    `note` appends a trailing hint, e.g. "ohne archivierte".
    """
    if total is None:
        msg = f"{shown} {noun}"
    elif shown < total:
        msg = f"{shown} von {total} {noun} angezeigt (mehr mit --all)"
    else:
        msg = f"{shown} von {total} {noun}"
    if note:
        msg += f" · {note}"
    err_console.print(f"[dim]→ {msg}[/dim]")


def safe_filename(name: str) -> str:
    """Neutralize an untrusted string for use as a single filename component.

    Filenames are built from user/LLM-supplied identifiers (e.g.
    ``invoice-{identifier}.pdf``); path separators and control characters are
    replaced and leading dots stripped so the result can never traverse out of
    the target directory or hide itself.
    """
    cleaned = re.sub(r"[\\/\x00-\x1f]", "_", name).strip().lstrip(".")
    return cleaned or "datei"


def write_binary(
    data: bytes, output_path: Path | None, *, default_name: str
) -> None:
    """Write binary data to disk, accepting a file path, a directory, or None.

    - ``None``           → ``default_name`` in the current directory
    - an existing dir    → ``default_name`` inside that directory
    - any other path     → used verbatim as the target file

    Missing parent directories are created. Filesystem errors (missing or
    read-only locations) are surfaced as a clean :class:`LexwareError` instead
    of an unhandled traceback.
    """
    default_name = safe_filename(default_name)
    if output_path is None:
        target = Path.cwd() / default_name
    else:
        candidate = output_path.expanduser()
        target = candidate / default_name if candidate.is_dir() else candidate

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    except OSError as exc:
        reason = exc.strerror or str(exc)
        raise LexwareError(f"Konnte Datei nicht schreiben: {target} ({reason}).") from exc
    err_console.print(f"[green]✓[/green] {len(data):,} bytes → {target}")


def _coerce_rows(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        if isinstance(data.get("content"), list):
            return data["content"]
        return [data]
    return []


def _to_csv(rows: list[dict[str, Any]], columns: list[str] | None) -> str:
    if not rows:
        return ""
    cols = columns or list(rows[0].keys())
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({c: _stringify(row.get(c)) for c in cols})
    return buf.getvalue()


def _render_table(
    rows: list[dict[str, Any]], *, columns: list[str] | None, title: str | None
) -> None:
    if not rows:
        err_console.print("[yellow]Keine Daten.[/yellow]")
        return
    cols = columns or list(rows[0].keys())
    table = Table(title=title, show_header=True, header_style="bold cyan")
    for col in cols:
        table.add_column(col)
    for row in rows:
        table.add_row(*[_stringify(row.get(c)) for c in cols])
    console.print(table)


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dict | list):
        return json.dumps(value, ensure_ascii=False, default=str)
    return str(value)


def _emit(text: str, output_path: Path | None) -> None:
    if output_path is None:
        sys.stdout.write(text)
        if not text.endswith("\n"):
            sys.stdout.write("\n")
    else:
        output_path.write_text(text, encoding="utf-8")
        err_console.print(f"[green]✓[/green] → {output_path}")
