"""Detail view for one record — human-readable fields with a JSON toggle.

`flatten_fields` projects an arbitrary API record into (label, value) rows:
nested objects become breadcrumb labels ("Adressen › Rechnungsadresse › Ort"),
known keys get German labels, dates/booleans/None are humanized. The screen
shows these rows as a two-column table; `j` switches to pretty-printed JSON
and back.
"""

from __future__ import annotations

import json
import re
from typing import Any

from rich.json import JSON
from rich.table import Table
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Footer, Static

# German labels for the most common Lexware API fields. Unknown keys fall back
# to the raw key name, so new API fields never disappear from the view.
KEY_LABELS = {
    "id": "ID",
    "voucherNumber": "Belegnummer",
    "voucherDate": "Belegdatum",
    "voucherType": "Belegart",
    "voucherStatus": "Status",
    "contactName": "Kontakt",
    "contactId": "Kontakt-ID",
    "totalAmount": "Gesamtbetrag",
    "openAmount": "Offener Betrag",
    "currency": "Währung",
    "dueDate": "Fällig am",
    "createdDate": "Angelegt am",
    "updatedDate": "Geändert am",
    "archived": "Archiviert",
    "company": "Firma",
    "person": "Person",
    "salutation": "Anrede",
    "name": "Name",
    "firstName": "Vorname",
    "lastName": "Nachname",
    "emailAddresses": "E-Mail",
    "phoneNumbers": "Telefon",
    "business": "geschäftlich",
    "office": "Büro",
    "mobile": "mobil",
    "private": "privat",
    "fax": "Fax",
    "other": "sonstige",
    "addresses": "Adressen",
    "billing": "Rechnungsadresse",
    "shipping": "Lieferadresse",
    "supplement": "Adresszusatz",
    "street": "Straße",
    "zip": "PLZ",
    "city": "Ort",
    "countryCode": "Land",
    "roles": "Rollen",
    "customer": "Kunde",
    "vendor": "Lieferant",
    "number": "Nummer",
    "note": "Notiz",
    "lineItems": "Position",
    "quantity": "Menge",
    "unitName": "Einheit",
    "unitPrice": "Einzelpreis",
    "netAmount": "Netto",
    "grossAmount": "Brutto",
    "taxRatePercentage": "USt. %",
    "taxAmount": "Steuerbetrag",
    "totalPrice": "Gesamtpreis",
    "totalNetAmount": "Gesamt netto",
    "totalGrossAmount": "Gesamt brutto",
    "totalTaxAmount": "Gesamt Steuer",
    "taxConditions": "Steuer",
    "taxType": "Besteuerung",
    "shippingConditions": "Versand",
    "shippingType": "Versandart",
    "shippingDate": "Versanddatum",
    "paymentConditions": "Zahlungsbedingungen",
    "paymentTermLabel": "Zahlungsziel",
    "paymentTermDuration": "Zahlungsfrist (Tage)",
    "introduction": "Einleitungstext",
    "remark": "Schlusstext",
    "title": "Bezeichnung",
    "description": "Beschreibung",
    "articleNumber": "Artikelnummer",
    "gtin": "GTIN",
    "type": "Typ",
    "price": "Preis",
    "netPrice": "Nettopreis",
    "grossPrice": "Bruttopreis",
    "leadingPrice": "Preisbasis",
    "taxRate": "Steuersatz",
    "companyName": "Firmenname",
    "organizationId": "Organisations-ID",
    "version": "Version",
    "language": "Sprache",
    "files": "Dateien",
    "documentFileId": "Dokument-Datei-ID",
}

_DATE_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})(?:T(\d{2}):(\d{2})(?::\d{2}(?:\.\d+)?)?)?"
)


def humanize_value(value: Any) -> str:
    """Render one scalar for humans: dates as DD.MM.YYYY, ja/nein, '—' for None."""
    if value is None or value == "":
        return "—"
    if value is True:
        return "ja"
    if value is False:
        return "nein"
    if isinstance(value, str):
        match = _DATE_RE.match(value)
        if match:
            year, month, day, hour, minute = match.groups()
            date = f"{day}.{month}.{year}"
            if hour is not None and (hour, minute) != ("00", "00"):
                return f"{date} {hour}:{minute}"
            return date
    return str(value)


def flatten_fields(data: Any, prefix: str = "") -> list[tuple[str, str]]:
    """Flatten a record into (label, value) rows in original field order.

    Nested dicts extend the label as a breadcrumb ("A › B"); list entries are
    numbered when there is more than one. Empty containers collapse to '—'.
    """
    rows: list[tuple[str, str]] = []
    if isinstance(data, dict):
        if not data and prefix:
            rows.append((prefix, "—"))
        for key, value in data.items():
            label = KEY_LABELS.get(key, key)
            path = f"{prefix} › {label}" if prefix else label
            if isinstance(value, dict | list):
                rows.extend(flatten_fields(value, path))
            else:
                rows.append((path, humanize_value(value)))
    elif isinstance(data, list):
        if not data and prefix:
            rows.append((prefix, "—"))
        for index, value in enumerate(data, start=1):
            path = f"{prefix} {index}" if len(data) > 1 else prefix
            if isinstance(value, dict | list):
                rows.extend(flatten_fields(value, path))
            else:
                rows.append((path, humanize_value(value)))
    else:
        rows.append((prefix or "Wert", humanize_value(data)))
    return rows


class DetailScreen(ModalScreen[None]):
    """Modal: lesbare Feldansicht, mit `j` umschaltbar auf das rohe JSON."""

    BINDINGS = [
        Binding("escape,q", "close", "Schließen"),
        Binding("j", "toggle_json", "Felder/JSON"),
    ]

    DEFAULT_CSS = """
    DetailScreen { align: center middle; }
    #detail-box {
        width: 80%; height: 80%;
        background: $surface; border: round $primary; padding: 1 2;
    }
    #detail-title { text-style: bold; padding-bottom: 1; }
    """

    def __init__(self, heading: str, data: Any) -> None:
        super().__init__()
        self._heading = heading
        self._data = data
        self._show_json = False

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="detail-box"):
            yield Static(self._title_text(), id="detail-title")
            yield Static(self._fields_table(), id="detail-content")
        yield Footer()

    def action_close(self) -> None:
        self.dismiss()

    def action_toggle_json(self) -> None:
        self._show_json = not self._show_json
        content = self.query_one("#detail-content", Static)
        if self._show_json:
            content.update(
                JSON(
                    json.dumps(
                        self._data, indent=2, ensure_ascii=False, default=str
                    )
                )
            )
        else:
            content.update(self._fields_table())
        self.query_one("#detail-title", Static).update(self._title_text())

    def _title_text(self) -> str:
        view = "JSON" if self._show_json else "Felder"
        return f"{self._heading} · Ansicht: {view} (j wechselt)"

    def _fields_table(self) -> Table:
        table = Table(show_header=False, box=None, padding=(0, 1))
        table.add_column("Feld", style="bold cyan", no_wrap=True)
        table.add_column("Wert", overflow="fold")
        for label, value in flatten_fields(self._data):
            table.add_row(label, value)
        return table
