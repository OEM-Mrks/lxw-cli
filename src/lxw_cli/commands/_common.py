"""CLI glue shared by the command modules.

The UI-agnostic logic lives in :mod:`lxw_cli.core`. This module keeps only
the Typer-coupled helpers (``state``, ``load_json_arg``) and the archived-footer
display string.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import typer

if TYPE_CHECKING:
    from lxw_cli.cli import AppState

# Footer hint shown when archived records are filtered out by default.
ARCHIVED_HINT = "ohne archivierte (--include-archived zeigt alle)"


def state(ctx: typer.Context) -> AppState:
    return ctx.obj


def load_json_arg(value: str) -> Any:
    """Load a JSON value either inline or from @path."""
    if value.startswith("@"):
        path = Path(value[1:]).expanduser()
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise typer.BadParameter(f"Datei nicht gefunden: {path}") from exc
        except json.JSONDecodeError as exc:
            raise typer.BadParameter(f"Ungültiges JSON in {path}: {exc}") from exc
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise typer.BadParameter(f"Ungültiges JSON: {exc}") from exc
