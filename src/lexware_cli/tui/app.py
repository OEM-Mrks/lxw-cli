"""Textual TUI application — browses the Lexware data and creates order drafts.

Talks only to ``lexware_cli.core`` (services/client/config). Blocking HTTP calls
run in worker threads so the event loop stays responsive; every core error is
surfaced as a visible notification + status line rather than swallowed. Textual
manages the alternate screen and restores the terminal on exit and on crash.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    Input,
    OptionList,
    Static,
)

from lexware_cli.config import load_config_interactive
from lexware_cli.core import services
from lexware_cli.core.client import LexwareClient
from lexware_cli.core.errors import LexwareError
from lexware_cli.tui.detail import DetailScreen
from lexware_cli.tui.entities import BROWSE_LIMIT, Entity, build_entities
from lexware_cli.tui.order_form import OrderCreateScreen


class LexwareTUI(App[None]):
    TITLE = "lexware"

    CSS = """
    #menu { width: 24; border-right: solid $panel; }
    #body { height: 1fr; }
    #search { display: none; }
    #search.visible { display: block; }
    /* Constrain the table to the remaining space so it scrolls internally —
       otherwise it grows with its rows and pushes the status line below the
       visible screen. */
    #table { height: 1fr; }
    #status { height: 1; color: $text-muted; padding: 0 1; background: $panel; }
    """

    BINDINGS = [
        Binding("q", "quit", "Beenden"),
        Binding("r", "refresh", "Aktualisieren"),
        Binding("n", "new_order", "Neuer Auftrag"),
        Binding("m", "load_more", "Mehr laden"),
        Binding("slash", "search", "Suchen", key_display="/"),
        Binding("escape", "close_search", "Suche schließen", show=False),
        Binding("tab", "focus_next", "Fokus wechseln", show=False),
    ]

    def __init__(self, client: LexwareClient) -> None:
        super().__init__()
        self._client = client
        self._entities: list[Entity] = build_entities()
        self._current: Entity | None = None
        # _all_records: last fetched set (browse or API search); _records: the
        # currently displayed subset after the local filter (row index ↔ table).
        self._all_records: list[dict[str, Any]] = []
        self._records: list[dict[str, Any]] = []
        self._last_result: Any = None
        self._filter: str = ""
        self._searched: bool = False  # current rows come from an API search
        self._limit: int = BROWSE_LIMIT  # grows page by page via load-more
        self._loading_more: bool = False
        self._restore_row: int | None = None  # cursor position across reloads
        self._last_status: str = ""

    # -- layout -------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Horizontal(id="body"):
            yield OptionList(*[e.title for e in self._entities], id="menu")
            with Vertical():
                yield Input(
                    placeholder=(
                        "Suchen — tippen filtert die geladenen Zeilen, "
                        "Enter sucht über die API (Esc schließt)"
                    ),
                    id="search",
                )
                yield DataTable(id="table", zebra_stripes=True)
                yield Static("", id="status")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#table", DataTable)
        table.cursor_type = "row"
        self.sub_title = "verbinde …"
        self._load_profile()
        menu = self.query_one("#menu", OptionList)
        menu.focus()
        menu.highlighted = 0
        self._select(0)

    # -- navigation ---------------------------------------------------------

    def on_option_list_option_selected(
        self, event: OptionList.OptionSelected
    ) -> None:
        self._select(event.option_index)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        idx = event.cursor_row
        if self._current is None or idx is None or idx >= len(self._records):
            return
        record = self._records[idx]
        self._open_detail(self._current, str(record.get("id") or ""), record)

    def _select(self, index: int) -> None:
        if 0 <= index < len(self._entities):
            self._current = self._entities[index]
            # A filter/search/page depth belongs to one entity — start fresh.
            search = self.query_one("#search", Input)
            if search.value:
                search.value = ""
            self._filter = ""
            self._limit = BROWSE_LIMIT
            self._load_entity(self._current)

    def action_refresh(self) -> None:
        if self._current is not None:
            self._load_entity(self._current)

    # -- pagination (load more) ------------------------------------------------

    def _has_more(self) -> bool:
        """True if the API likely holds more records than currently loaded."""
        total = getattr(self._last_result, "total", None)
        if total is not None:
            return len(self._all_records) < total
        # Total unknown: if we got as many as we asked for, assume more exist.
        return len(self._all_records) >= self._limit

    def action_load_more(self) -> None:
        if self._current is None or self._searched or self._loading_more:
            return
        if not self._has_more():
            self._set_status("Alle Datensätze sind bereits geladen.")
            return
        self._loading_more = True
        self._limit += BROWSE_LIMIT
        self._restore_row = self.query_one("#table", DataTable).cursor_row
        self._load_entity(self._current)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        """Infinite scroll: reaching the last row loads the next batch."""
        row = event.cursor_row
        table = event.data_table
        if (
            row is not None
            and table.row_count
            and row >= table.row_count - 1
            and self._has_more()
            and not self._searched
            and not self._loading_more
        ):
            self.action_load_more()

    # -- search (lokal filtern + API-Suche) -----------------------------------

    def action_search(self) -> None:
        search = self.query_one("#search", Input)
        search.add_class("visible")
        search.focus()

    def action_close_search(self) -> None:
        search = self.query_one("#search", Input)
        if not search.has_class("visible"):
            return
        search.value = ""
        self._filter = ""
        search.remove_class("visible")
        self.query_one("#table", DataTable).focus()
        if self._searched and self._current is not None:
            # An API search replaced the rows — restore the browse list.
            self._load_entity(self._current)
        else:
            self._render_rows()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "search":
            return
        self._filter = event.value.strip()
        self._render_rows()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "search":
            return
        entity = self._current
        if entity is None:
            return
        query = event.value.strip()
        if entity.search_fn is None:
            self._set_status(
                f"Keine API-Suche für {entity.title} — der lokale Filter bleibt aktiv."
            )
            return
        if len(query) < 3:
            self._set_status("API-Suche: mindestens 3 Zeichen eingeben.")
            return
        self._server_search(entity, query)

    def action_new_order(self) -> None:
        self.push_screen(OrderCreateScreen(self._client), self._on_order_created)

    def _on_order_created(self, created: dict[str, Any] | None) -> None:
        if created is None:  # cancelled
            return
        self.notify(
            f"Auftrag als Entwurf angelegt (ID {created.get('id', '?')}).",
            title="Auftrag angelegt",
        )
        # Jump to the orders list so the new draft is immediately visible.
        index = next(
            (i for i, e in enumerate(self._entities) if e.key == "orders"), None
        )
        if index is not None:
            self.query_one("#menu", OptionList).highlighted = index
            self._select(index)

    # -- workers (blocking core calls off the event loop) -------------------

    @work(exclusive=True, group="profile")
    async def _load_profile(self) -> None:
        try:
            profile = await asyncio.to_thread(services.get_profile, self._client)
        except Exception as exc:  # noqa: BLE001 - surfaced to the user, not swallowed
            self.sub_title = "nicht verbunden"
            self._error(f"Profil: {exc}")
            return
        self.sub_title = str(
            profile.get("companyName") or profile.get("organizationId") or "verbunden"
        )

    @work(exclusive=True, group="fetch")
    async def _load_entity(self, entity: Entity) -> None:
        table = self.query_one("#table", DataTable)
        table.loading = True
        self._set_status(f"Lade {entity.title} …")
        try:
            result = await asyncio.to_thread(
                entity.list_fn, self._client, self._limit
            )
        except Exception as exc:  # noqa: BLE001 - shown as a visible error
            self._error(f"{entity.title}: {exc}")
            return
        finally:
            table.loading = False
            self._loading_more = False
        self._searched = False
        self._populate(entity, result)

    @work(exclusive=True, group="fetch")
    async def _server_search(self, entity: Entity, query: str) -> None:
        """API search ('nativ über den Request') — replaces the loaded rows."""
        assert entity.search_fn is not None
        table = self.query_one("#table", DataTable)
        table.loading = True
        self._set_status(f"Suche „{query}“ über die API …")
        try:
            result = await asyncio.to_thread(entity.search_fn, self._client, query)
        except Exception as exc:  # noqa: BLE001 - shown as a visible error
            self._error(f"Suche: {exc}")
            return
        finally:
            table.loading = False
        self._searched = True
        # The server already matched the query — show its results unfiltered;
        # further typing filters within them ('nachträglich in den Daten').
        self._filter = ""
        self._populate(entity, result)
        self._set_status(f"API-Suche „{query}“: {len(result.items)} Treffer")

    @work(exclusive=True, group="detail")
    async def _open_detail(
        self, entity: Entity, identifier: str, fallback: dict[str, Any]
    ) -> None:
        self._set_status("Lade Details …")
        data: Any = fallback
        try:
            data = await asyncio.to_thread(entity.get_fn, self._client, identifier)
        except Exception as exc:  # noqa: BLE001 - show what we have + the error
            self._error(f"Details: {exc}")
        self._set_status(self._summary_for_current())
        await self.push_screen(DetailScreen(f"{entity.title} · {identifier}", data))

    # -- rendering ----------------------------------------------------------

    def _populate(self, entity: Entity, result: Any) -> None:
        self._all_records = list(result.items)
        self._last_result = result
        self._render_rows()

    def _render_rows(self) -> None:
        """Fill the table from `_all_records`, applying the local filter.

        The filter matches case-insensitively against the *visible* cells, so
        what you see is what you can search. `_records` keeps the displayed
        subset so the row index still maps to the right record for details.
        """
        entity = self._current
        if entity is None:
            return
        table = self.query_one("#table", DataTable)
        table.clear(columns=True)
        table.add_columns(*entity.columns)
        needle = self._filter.casefold()
        self._records = []
        for record in self._all_records:
            # str() defensively: row_fn implementations should return strings,
            # but raw API values (int numbers etc.) must never crash the filter.
            cells = [str(cell) for cell in entity.row_fn(record)]
            if needle and not any(needle in cell.casefold() for cell in cells):
                continue
            self._records.append(record)
            table.add_row(*cells)
        if self._records:
            row = 0
            if self._restore_row is not None:
                row = min(self._restore_row, len(self._records) - 1)
            table.move_cursor(row=row)
        self._restore_row = None
        if needle:
            self._set_status(
                f"{len(self._records)} von {len(self._all_records)} "
                f"{entity.title} (Filter: „{self._filter}“)"
            )
        elif self._last_result is not None:
            self._set_status(_summary(entity, self._last_result))

    def _summary_for_current(self) -> str:
        n = len(self._records)
        title = self._current.title if self._current else "Einträge"
        return f"{n} {title}"

    def _set_status(self, message: str) -> None:
        self._last_status = message
        self.query_one("#status", Static).update(message)

    def _error(self, message: str) -> None:
        self._set_status(f"[red]Fehler:[/red] {message}")
        self.notify(message, title="Fehler", severity="error", timeout=8)


def _summary(entity: Entity, result: Any) -> str:
    """Status line, always as 'X von Y <Entität>' so it is obvious whether
    further pages exist; partial loads carry the load-more hint."""
    n = len(result.items)
    if getattr(result, "search", False):
        return f"{n} Treffer"
    total = getattr(result, "total", None)
    hidden = getattr(result, "hidden", 0)
    note = f" ({hidden} archivierte ausgeblendet)" if hidden else ""
    if total is None:
        return f"{n} {entity.title}{note}"
    base = f"{n} von {total} {entity.title}"
    # `hidden` rows were fetched but filtered out client-side — only when
    # fetched + hidden is still short of the server total are there more pages.
    if n + hidden < total:
        base += " — ans Ende scrollen oder m lädt mehr"
    return base + note


def run() -> None:
    """Entry point for `lexware-tui` and the no-args `lexware` auto-launch."""
    try:
        config = load_config_interactive()
    except LexwareError as exc:
        print(f"Fehler: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    client = LexwareClient(config)
    try:
        LexwareTUI(client).run()
    finally:
        client.close()


if __name__ == "__main__":  # pragma: no cover
    run()
