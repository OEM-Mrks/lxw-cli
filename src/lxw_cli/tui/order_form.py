"""Modal form for creating an order confirmation (Auftrag) as draft.

Pure frontend: searching and creating go through :mod:`lxw_cli.core`
services, blocking calls run in worker threads (same pattern as the main app).
While a search or the create call runs, the affected widgets show Textual's
loading animation so the UI never looks frozen. The screen dismisses with the
API's create response on success, or ``None`` on cancel.
"""

from __future__ import annotations

import asyncio
from typing import Any

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, OptionList, Static, TextArea

from lxw_cli.core import services
from lxw_cli.core.client import LexwareClient
from lxw_cli.tui.entities import contact_row

SEARCH_LIMIT = 15


def format_position(
    article: dict[str, Any], quantity: float, net_price: float | None = None
) -> str:
    """One human-readable line per position, e.g. '2 × Schraube M8 (à 0.12 € netto)'.

    `net_price` is the (possibly user-edited) effective price; without it the
    article's list price is shown.
    """
    if net_price is None:
        net_price = (article.get("price") or {}).get("netPrice")
    qty = f"{quantity:g}"
    name = article.get("title") or article.get("articleNumber") or "Position"
    if net_price is None:
        return f"{qty} × {name}"
    return f"{qty} × {name} (à {net_price:.2f} € netto)"


def parse_quantity(raw: str) -> float | None:
    """Parse a positive quantity; accepts a German decimal comma."""
    try:
        value = float(raw.strip().replace(",", "."))
    except ValueError:
        return None
    return value if value > 0 else None


def parse_price(raw: str) -> float | None:
    """Parse a non-negative net price; accepts a German decimal comma."""
    try:
        value = float(raw.strip().replace(",", "."))
    except ValueError:
        return None
    return value if value >= 0 else None


class OrderCreateScreen(ModalScreen[dict[str, Any] | None]):
    """Kunde suchen → Artikel-Positionen sammeln → Auftrag als Entwurf anlegen."""

    BINDINGS = [Binding("escape", "cancel", "Abbrechen")]

    DEFAULT_CSS = """
    OrderCreateScreen { align: center middle; }
    #order-box {
        width: 80%; height: 90%;
        background: $surface; border: round $primary; padding: 1 2;
    }
    #order-heading { text-style: bold; padding-bottom: 1; }
    #customer-results, #article-results { height: 5; border: solid $panel; }
    #customer-chosen, #positions { color: $text-muted; padding: 0 0 1 0; }
    #order-status { color: $error; }
    #quantity, #unit-price { width: 14; }
    #introduction, #remark { height: 3; }
    .row { height: auto; }
    Button { margin-right: 2; }
    """

    def __init__(self, client: LexwareClient) -> None:
        super().__init__()
        self._client = client
        self._contacts: list[dict[str, Any]] = []
        self._articles: list[dict[str, Any]] = []
        self._contact: dict[str, Any] | None = None
        self._article: dict[str, Any] | None = None
        # (article, quantity, effective net price) per collected position.
        self._positions: list[tuple[dict[str, Any], float, float]] = []

    # -- layout ---------------------------------------------------------------

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="order-box"):
            yield Static("Neuer Auftrag (Entwurf)", id="order-heading")
            yield Label("Kunde suchen — Enter sucht, Auswahl per Liste:")
            yield Input(placeholder="Kundenname …", id="customer-search")
            yield OptionList(id="customer-results")
            yield Static("Noch kein Kunde gewählt.", id="customer-chosen")
            yield Label("Artikel suchen — Enter sucht:")
            yield Input(placeholder="Bezeichnung oder Artikelnummer …", id="article-search")
            yield OptionList(id="article-results")
            with Horizontal(classes="row"):
                yield Label("Menge: ")
                yield Input(value="1", id="quantity")
                yield Label("  Einzelpreis € netto: ")
                yield Input(placeholder="aus Artikel", id="unit-price")
                yield Button("Position hinzufügen", id="add-position")
            yield Static("Noch keine Positionen.", id="positions")
            yield Label("Einleitungstext (optional):")
            yield TextArea(id="introduction", tab_behavior="focus")
            yield Label("Schlusstext (optional):")
            yield TextArea(id="remark", tab_behavior="focus")
            yield Static("", id="order-status")
            with Horizontal(classes="row"):
                yield Button("Auftrag anlegen", variant="success", id="submit")
                yield Button("Abbrechen", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#customer-search", Input).focus()

    # -- events ---------------------------------------------------------------

    def on_input_submitted(self, event: Input.Submitted) -> None:
        query = event.value.strip()
        if event.input.id == "customer-search" and len(query) >= 3:
            self._search_contacts(query)
        elif event.input.id == "article-search" and query:
            self._search_articles(query)
        elif event.input.id in ("quantity", "unit-price"):
            self._add_position()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_list.id == "customer-results":
            if 0 <= event.option_index < len(self._contacts):
                self._contact = self._contacts[event.option_index]
                name = contact_row(self._contact)[0] or self._contact.get("id", "")
                self.query_one("#customer-chosen", Static).update(f"Kunde: {name}")
                self.query_one("#article-search", Input).focus()
        elif event.option_list.id == "article-results":
            if 0 <= event.option_index < len(self._articles):
                self._article = self._articles[event.option_index]
                # Prefill the price from the article — editable before adding.
                net = (self._article.get("price") or {}).get("netPrice")
                self.query_one("#unit-price", Input).value = (
                    "" if net is None else f"{net:g}"
                )
                self.query_one("#quantity", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "add-position":
            self._add_position()
        elif event.button.id == "submit":
            self._submit()
        elif event.button.id == "cancel":
            self.action_cancel()

    def action_cancel(self) -> None:
        self.dismiss(None)

    # -- form logic -------------------------------------------------------------

    def _add_position(self) -> None:
        if self._article is None:
            self._status("Bitte zuerst einen Artikel suchen und auswählen.")
            return
        quantity = parse_quantity(self.query_one("#quantity", Input).value)
        if quantity is None:
            self._status("Ungültige Menge — bitte eine positive Zahl eingeben.")
            return
        raw_price = self.query_one("#unit-price", Input).value
        net_price = parse_price(raw_price) if raw_price.strip() else None
        if net_price is None:
            net_price = (self._article.get("price") or {}).get("netPrice")
        if net_price is None:
            self._status("Ungültiger Preis — bitte eine Zahl ≥ 0 eingeben.")
            return
        self._positions.append((self._article, quantity, float(net_price)))
        self._article = None
        self.query_one("#article-results", OptionList).clear_options()
        self.query_one("#unit-price", Input).value = ""
        article_input = self.query_one("#article-search", Input)
        article_input.value = ""
        article_input.focus()
        lines = [format_position(a, q, p) for a, q, p in self._positions]
        self.query_one("#positions", Static).update("\n".join(lines))
        self._status("")

    def _submit(self) -> None:
        if self._contact is None:
            self._status("Bitte einen Kunden auswählen.")
            return
        if not self._positions:
            self._status("Bitte mindestens eine Position hinzufügen.")
            return
        self._create_order()

    def _status(self, message: str) -> None:
        self.query_one("#order-status", Static).update(message)

    # -- workers ----------------------------------------------------------------

    @work(exclusive=True, group="order-search")
    async def _search_contacts(self, query: str) -> None:
        results = self.query_one("#customer-results", OptionList)
        results.loading = True
        self._status("")
        try:
            found = await asyncio.to_thread(
                services.list_contacts,
                self._client,
                name=query,
                customer=True,
                limit=SEARCH_LIMIT,
            )
        except Exception as exc:  # noqa: BLE001 - surfaced in the form
            self._status(f"Kundensuche fehlgeschlagen: {exc}")
            return
        finally:
            results.loading = False
        self._contacts = found.items
        results.clear_options()
        results.add_options(
            [contact_row(c)[0] or c.get("id", "?") for c in self._contacts]
            or ["— keine Treffer —"]
        )
        if self._contacts:
            results.focus()

    @work(exclusive=True, group="order-search")
    async def _search_articles(self, query: str) -> None:
        # The article search pages through ALL articles client-side (the API
        # has no name filter), so this can take a while — the loading overlay
        # on the result list keeps that visible.
        results = self.query_one("#article-results", OptionList)
        results.loading = True
        self._status("")
        try:
            found = await asyncio.to_thread(
                services.list_articles, self._client, search=query, limit=SEARCH_LIMIT
            )
        except Exception as exc:  # noqa: BLE001 - surfaced in the form
            self._status(f"Artikelsuche fehlgeschlagen: {exc}")
            return
        finally:
            results.loading = False
        self._articles = found.items
        results.clear_options()
        results.add_options(
            [format_position(a, 1).removeprefix("1 × ") for a in self._articles]
            or ["— keine Treffer —"]
        )
        if self._articles:
            results.focus()

    @work(exclusive=True, group="order-create")
    async def _create_order(self) -> None:
        assert self._contact is not None
        submit = self.query_one("#submit", Button)
        box = self.query_one("#order-box", VerticalScroll)
        submit.disabled = True
        box.loading = True
        body = services.build_order_confirmation_body(
            self._contact["id"],
            [
                services.article_line_item(a, q, net_price=p)
                for a, q, p in self._positions
            ],
            introduction=self.query_one("#introduction", TextArea).text,
            remark=self.query_one("#remark", TextArea).text,
        )
        try:
            created = await asyncio.to_thread(
                services.create_order_confirmation, self._client, body
            )
        except Exception as exc:  # noqa: BLE001 - surfaced in the form
            self._status(f"Anlegen fehlgeschlagen: {exc}")
            return
        finally:
            box.loading = False
            submit.disabled = False
        self.dismiss(created)
