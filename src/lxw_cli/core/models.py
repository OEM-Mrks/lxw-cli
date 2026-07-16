"""Plain result types returned by the core services (no UI concerns)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ListResult:
    """Outcome of a list/search operation.

    Carries the rows plus enough metadata for any frontend to render a count
    summary, without the frontend knowing how the data was fetched:

    - ``total``: the server's total result count (``totalElements``), or None
      when unknown (e.g. client-side article search).
    - ``hidden``: how many rows were dropped by client-side filtering
      (e.g. archived contacts).
    - ``exhausted``: True if every page was scanned, so ``items``/``hidden``
      are complete (False means a limit was hit before the end).
    - ``search``: True for client-side search mode (count = matches, not a
      server total).
    """

    items: list[dict[str, Any]] = field(default_factory=list)
    total: int | None = None
    hidden: int = 0
    exhausted: bool = True
    search: bool = False
