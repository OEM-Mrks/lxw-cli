from __future__ import annotations

import httpx
import respx
from textual.widgets import DataTable

from lxw_cli.core.client import LexwareClient
from lxw_cli.core.models import ListResult
from lxw_cli.tui.app import LexwareTUI, _summary
from lxw_cli.tui.entities import build_entities, contact_row

_PROFILE = "https://api.lexware.io/v1/profile"
_VOUCHERLIST = "https://api.lexware.io/v1/voucherlist"


# -- pure unit tests --------------------------------------------------------


def test_entities_registry() -> None:
    keys = [e.key for e in build_entities()]
    assert keys == [
        "invoices",
        "contacts",
        "vouchers",
        "articles",
        "quotations",
        "orders",
        "delivery_notes",
    ]


def test_contact_row_projects_nested_fields() -> None:
    record = {
        "id": "c1",
        "company": {"name": "Acme GmbH"},
        "roles": {"customer": {"number": "K-1"}, "vendor": {"number": "L-1"}},
        "emailAddresses": {"business": ["a@k.de"]},
        "archived": False,
    }
    assert contact_row(record) == ["Acme GmbH", "customer+vendor", "K-1", "a@k.de", "False"]


def test_contact_row_stringifies_int_numbers() -> None:
    """Regression: the API delivers contact numbers as ints — filtering rows
    must never crash on non-string cells (cell.casefold on int 70077)."""
    record = {
        "id": "39673dd8",
        "roles": {"vendor": {"number": 70077}},
        "company": {"name": "Anthropic, Pbc"},
        "archived": False,
    }
    cells = contact_row(record)
    assert cells == ["Anthropic, Pbc", "vendor", "70077", "", "False"]
    assert all(isinstance(c, str) for c in cells)


def test_summary_variants() -> None:
    entity = build_entities()[0]  # Rechnungen
    # Always 'X von Y', so it is obvious whether further pages exist.
    assert _summary(entity, ListResult(items=[{}], total=1)) == "1 von 1 Rechnungen"
    partial = _summary(entity, ListResult(items=[{}], total=9))
    assert partial.startswith("1 von 9 Rechnungen")
    assert "m lädt mehr" in partial
    assert _summary(entity, ListResult(items=[{}, {}], search=True)) == "2 Treffer"
    # Hidden (client-side filtered) rows count toward the page math: 1 shown +
    # 8 hidden = 9 fetched of 9 → complete, no load-more hint.
    complete = _summary(entity, ListResult(items=[{}], total=9, hidden=8))
    assert "m lädt mehr" not in complete
    assert "8 archivierte ausgeblendet" in complete


# -- Pilot integration tests ------------------------------------------------


@respx.mock
async def test_tui_loads_first_entity_and_profile(config) -> None:
    respx.get(_PROFILE).mock(
        return_value=httpx.Response(200, json={"companyName": "Acme GmbH"})
    )
    respx.get(_VOUCHERLIST).mock(
        return_value=httpx.Response(
            200,
            json={
                "content": [
                    {
                        "id": "u1",
                        "voucherNumber": "RG-001",
                        "voucherDate": "2026-06-01",
                        "voucherStatus": "open",
                        "contactName": "Kunde",
                        "totalAmount": 119.0,
                        "currency": "EUR",
                    }
                ],
                "last": True,
                "totalElements": 1,
            },
        )
    )
    client = LexwareClient(config)
    app = LexwareTUI(client)
    try:
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            table = app.query_one("#table", DataTable)
            assert table.row_count == 1
            assert app.sub_title == "Acme GmbH"
    finally:
        client.close()


@respx.mock
async def test_tui_surfaces_core_errors(config) -> None:
    respx.get(_PROFILE).mock(
        return_value=httpx.Response(200, json={"companyName": "Acme GmbH"})
    )
    respx.get(_VOUCHERLIST).mock(
        return_value=httpx.Response(500, json={"message": "boom"})
    )
    client = LexwareClient(config)
    app = LexwareTUI(client)
    try:
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert "Fehler" in app._last_status
            assert app.query_one("#table", DataTable).row_count == 0
    finally:
        client.close()


# -- Auftrag anlegen ----------------------------------------------------------


def test_order_form_helpers() -> None:
    from lxw_cli.tui.order_form import format_position, parse_price, parse_quantity

    assert parse_quantity("2") == 2.0
    assert parse_quantity("1,5") == 1.5
    assert parse_quantity("0") is None
    assert parse_quantity("abc") is None

    # Prices may be zero (free positions), but never negative.
    assert parse_price("0") == 0.0
    assert parse_price("12,50") == 12.5
    assert parse_price("-1") is None
    assert parse_price("abc") is None

    article = {"title": "Schraube M8", "price": {"netPrice": 0.12}}
    assert format_position(article, 50) == "50 × Schraube M8 (à 0.12 € netto)"
    # An edited price overrides the article's list price in the display.
    assert format_position(article, 50, 0.10) == "50 × Schraube M8 (à 0.10 € netto)"
    assert format_position({"articleNumber": "A-1"}, 1) == "1 × A-1"


@respx.mock
async def test_tui_creates_order_draft_and_switches_to_orders(config) -> None:
    from lxw_cli.tui.order_form import OrderCreateScreen

    respx.get(_PROFILE).mock(
        return_value=httpx.Response(200, json={"companyName": "Acme GmbH"})
    )
    respx.get(_VOUCHERLIST).mock(
        return_value=httpx.Response(200, json={"content": [], "last": True})
    )
    create_route = respx.post(
        "https://api.lexware.io/v1/order-confirmations"
    ).mock(return_value=httpx.Response(201, json={"id": "new-order"}))

    client = LexwareClient(config)
    app = LexwareTUI(client)
    try:
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            await pilot.press("n")
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, OrderCreateScreen)

            # Fill the form state directly; the search round-trips are covered
            # by the service tests. The price is user-edited (0.10 instead of
            # the article's 0.12) and an introduction text is set. Then submit.
            from textual.widgets import TextArea

            screen._contact = {"id": "c-1", "company": {"name": "Acme"}}
            screen._positions = [
                (
                    {"title": "Schraube", "price": {"netPrice": 0.12, "taxRate": 19}},
                    50.0,
                    0.10,
                )
            ]
            screen.query_one("#introduction", TextArea).text = "Danke für Ihren Auftrag!"
            screen._submit()
            await app.workers.wait_for_complete()
            await pilot.pause()

            assert create_route.called
            import json

            body = json.loads(create_route.calls.last.request.content)
            assert body["address"] == {"contactId": "c-1"}
            assert body["lineItems"][0]["name"] == "Schraube"
            assert body["lineItems"][0]["quantity"] == 50.0
            assert body["lineItems"][0]["unitPrice"]["netAmount"] == 0.10
            assert body["introduction"] == "Danke für Ihren Auftrag!"
            assert "remark" not in body  # leer gelassen → weggelassen
            # Modal closed; the app jumped to the orders list.
            assert not isinstance(app.screen, OrderCreateScreen)
            await app.workers.wait_for_complete()
            assert app._current is not None and app._current.key == "orders"
    finally:
        client.close()


@respx.mock
async def test_order_form_escape_cancels(config) -> None:
    from lxw_cli.tui.order_form import OrderCreateScreen

    respx.get(_PROFILE).mock(
        return_value=httpx.Response(200, json={"companyName": "Acme GmbH"})
    )
    respx.get(_VOUCHERLIST).mock(
        return_value=httpx.Response(200, json={"content": [], "last": True})
    )
    client = LexwareClient(config)
    app = LexwareTUI(client)
    try:
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            await pilot.press("n")
            await pilot.pause()
            assert isinstance(app.screen, OrderCreateScreen)
            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, OrderCreateScreen)
    finally:
        client.close()


@respx.mock
async def test_article_search_shows_loading_animation(config) -> None:
    """While the article search thread runs, the result list shows the
    loading indicator — the UI must never look frozen during slow searches."""
    import time

    from textual.widgets import OptionList

    from lxw_cli.tui.order_form import OrderCreateScreen

    respx.get(_PROFILE).mock(
        return_value=httpx.Response(200, json={"companyName": "Acme GmbH"})
    )
    respx.get(_VOUCHERLIST).mock(
        return_value=httpx.Response(200, json={"content": [], "last": True})
    )

    def slow_articles(_request):
        time.sleep(0.1)  # simulate a slow paging search
        return httpx.Response(
            200,
            json={
                "content": [{"id": "a1", "title": "Schraube", "price": {"netPrice": 0.12}}],
                "last": True,
            },
        )

    respx.get("https://api.lexware.io/v1/articles").mock(side_effect=slow_articles)

    client = LexwareClient(config)
    app = LexwareTUI(client)
    try:
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            await pilot.press("n")
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, OrderCreateScreen)

            screen._search_articles("schraube")
            await pilot.pause(0.02)  # worker started, HTTP thread still sleeping
            results = screen.query_one("#article-results", OptionList)
            assert results.loading is True

            await app.workers.wait_for_complete()
            await pilot.pause()
            assert results.loading is False
            assert results.option_count == 1
    finally:
        client.close()


# -- Suche im Kundenstamm ------------------------------------------------------


@respx.mock
def test_search_contacts_picks_name_or_email_param(config) -> None:
    from lxw_cli.tui.entities import search_contacts

    route = respx.get("https://api.lexware.io/v1/contacts").mock(
        return_value=httpx.Response(200, json={"content": [], "last": True})
    )
    client = LexwareClient(config)
    try:
        search_contacts(client, "Acme")
        assert "name=Acme" in str(route.calls.last.request.url)
        search_contacts(client, "info@acme.de")
        url = str(route.calls.last.request.url)
        assert "email=info%40acme.de" in url
        assert "name=" not in url
    finally:
        client.close()


@respx.mock
async def test_local_filter_narrows_loaded_rows(config) -> None:
    """Typing in the search bar filters the already-loaded data client-side."""
    from textual.widgets import Input

    respx.get(_PROFILE).mock(
        return_value=httpx.Response(200, json={"companyName": "Acme GmbH"})
    )
    respx.get(_VOUCHERLIST).mock(
        return_value=httpx.Response(
            200,
            json={
                "content": [
                    {"id": "u1", "voucherNumber": "RG-001", "contactName": "Alpha"},
                    {"id": "u2", "voucherNumber": "RG-002", "contactName": "Beta"},
                ],
                "last": True,
                "totalElements": 2,
            },
        )
    )
    client = LexwareClient(config)
    app = LexwareTUI(client)
    try:
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            table = app.query_one("#table", DataTable)
            assert table.row_count == 2

            await pilot.press("slash")
            search = app.query_one("#search", Input)
            assert search.has_class("visible")
            search.value = "beta"  # matches contactName, case-insensitive
            await pilot.pause()
            assert table.row_count == 1
            assert "Filter" in app._last_status

            # Esc closes the bar and restores the full list (no refetch needed).
            await pilot.press("escape")
            await pilot.pause()
            assert not search.has_class("visible")
            assert table.row_count == 2
    finally:
        client.close()


@respx.mock
async def test_contacts_api_search_replaces_rows(config) -> None:
    """Enter in the search bar runs the server-side contact search."""
    from textual.widgets import Input

    respx.get(_PROFILE).mock(
        return_value=httpx.Response(200, json={"companyName": "Acme GmbH"})
    )
    respx.get(_VOUCHERLIST).mock(
        return_value=httpx.Response(200, json={"content": [], "last": True})
    )

    def contacts_response(request):
        if "name=acme" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "content": [
                        {"id": "c9", "company": {"name": "Acme GmbH"}, "roles": {}}
                    ],
                    "last": True,
                },
            )
        return httpx.Response(
            200,
            json={
                "content": [
                    {"id": "c1", "company": {"name": "Alpha"}, "roles": {}},
                    {"id": "c2", "company": {"name": "Beta"}, "roles": {}},
                ],
                "last": True,
                "totalElements": 2,
            },
        )

    respx.get("https://api.lexware.io/v1/contacts").mock(side_effect=contacts_response)

    client = LexwareClient(config)
    app = LexwareTUI(client)
    try:
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            app._select(1)  # Kontakte
            await app.workers.wait_for_complete()
            await pilot.pause()
            table = app.query_one("#table", DataTable)
            assert table.row_count == 2

            await pilot.press("slash")
            app.query_one("#search", Input).value = "acme"
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert table.row_count == 1
            assert "Treffer" in app._last_status

            # Esc restores the browse list (refetches, since rows came from
            # the API search).
            await pilot.press("escape")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert table.row_count == 2
    finally:
        client.close()


# -- Detailansicht: Felder + JSON ---------------------------------------------


def test_humanize_value() -> None:
    from lxw_cli.tui.detail import humanize_value

    assert humanize_value(None) == "—"
    assert humanize_value("") == "—"
    assert humanize_value(True) == "ja"
    assert humanize_value(False) == "nein"
    assert humanize_value("2026-06-01") == "01.06.2026"
    assert humanize_value("2026-06-01T00:00:00.000+02:00") == "01.06.2026"
    assert humanize_value("2026-06-01T14:30:00.000+02:00") == "01.06.2026 14:30"
    assert humanize_value(119.0) == "119.0"
    assert humanize_value("Acme GmbH") == "Acme GmbH"


def test_flatten_fields_builds_readable_rows() -> None:
    from lxw_cli.tui.detail import flatten_fields

    data = {
        "voucherNumber": "RG-1",
        "archived": False,
        "addresses": {"billing": [{"city": "Köln"}]},
        "lineItems": [{"name": "A"}, {"name": "B"}],
        "files": [],
        "unknownField": 7,
    }
    rows = flatten_fields(data)
    assert ("Belegnummer", "RG-1") in rows
    assert ("Archiviert", "nein") in rows
    # Nested structures become breadcrumb labels with German names.
    assert ("Adressen › Rechnungsadresse › Ort", "Köln") in rows
    # Multiple list entries are numbered.
    assert ("Position 1 › Name", "A") in rows
    assert ("Position 2 › Name", "B") in rows
    # Empty containers collapse, unknown keys keep their raw name.
    assert ("Dateien", "—") in rows
    assert ("unknownField", "7") in rows


@respx.mock
async def test_detail_screen_toggles_between_fields_and_json(config) -> None:
    from lxw_cli.tui.detail import DetailScreen

    respx.get(_PROFILE).mock(
        return_value=httpx.Response(200, json={"companyName": "Acme GmbH"})
    )
    respx.get(_VOUCHERLIST).mock(
        return_value=httpx.Response(
            200,
            json={
                "content": [{"id": "u1", "voucherNumber": "RG-001"}],
                "last": True,
                "totalElements": 1,
            },
        )
    )
    respx.get("https://api.lexware.io/v1/invoices/u1").mock(
        return_value=httpx.Response(
            200,
            json={"id": "u1", "voucherNumber": "RG-001", "archived": False},
        )
    )

    client = LexwareClient(config)
    app = LexwareTUI(client)
    try:
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            entity = app._current
            assert entity is not None
            app._open_detail(entity, "u1", {"id": "u1"})
            await app.workers.wait_for_complete()
            await pilot.pause()

            screen = app.screen
            assert isinstance(screen, DetailScreen)
            assert screen._show_json is False
            assert "Felder" in str(screen.query_one("#detail-title").render())

            await pilot.press("j")
            await pilot.pause()
            assert screen._show_json is True
            assert "JSON" in str(screen.query_one("#detail-title").render())

            await pilot.press("j")
            await pilot.pause()
            assert screen._show_json is False

            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, DetailScreen)
    finally:
        client.close()


# -- Paginierung (Mehr laden) ---------------------------------------------------


@respx.mock
async def test_tui_paginates_with_load_more(config) -> None:
    """First load fetches one full API page (250); scrolling to the last row
    (or pressing m) loads the next batch and keeps the cursor position."""
    page0 = [{"id": f"u{i}", "voucherNumber": f"RG-{i:03d}"} for i in range(250)]
    page1 = [{"id": "u250", "voucherNumber": "RG-250"}]

    respx.get(_PROFILE).mock(
        return_value=httpx.Response(200, json={"companyName": "Acme GmbH"})
    )
    respx.get(_VOUCHERLIST, params={"page": 0}).mock(
        return_value=httpx.Response(
            200, json={"content": page0, "last": False, "totalElements": 251}
        )
    )
    page1_route = respx.get(_VOUCHERLIST, params={"page": 1}).mock(
        return_value=httpx.Response(
            200, json={"content": page1, "last": True, "totalElements": 251}
        )
    )

    client = LexwareClient(config)
    app = LexwareTUI(client)
    try:
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            table = app.query_one("#table", DataTable)
            # One full page loaded; page 1 not yet requested.
            assert table.row_count == 250
            assert not page1_route.called
            assert "von 251" in app._last_status
            assert "m lädt mehr" in app._last_status

            # Scrolling onto the last row auto-loads the next batch.
            table.move_cursor(row=249)
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert page1_route.called
            assert table.row_count == 251
            # Cursor stays where the user was instead of jumping to the top.
            assert table.cursor_row == 249
            assert "251 Rechnungen" in app._last_status

            # Everything loaded — m reports completeness, no further request.
            calls_before = page1_route.call_count
            await pilot.press("m")
            await pilot.pause()
            assert page1_route.call_count == calls_before
            assert "bereits geladen" in app._last_status
    finally:
        client.close()


@respx.mock
async def test_status_line_stays_visible_with_many_rows(config) -> None:
    """Regression: with a full 250-row page the table must scroll internally —
    it must not push the record counter (#status) below the visible screen."""
    from textual.widgets import Static

    rows = [{"id": f"u{i}", "voucherNumber": f"RG-{i:03d}"} for i in range(250)]
    respx.get(_PROFILE).mock(
        return_value=httpx.Response(200, json={"companyName": "Acme GmbH"})
    )
    respx.get(_VOUCHERLIST).mock(
        return_value=httpx.Response(
            200, json={"content": rows, "last": True, "totalElements": 250}
        )
    )
    client = LexwareClient(config)
    app = LexwareTUI(client)
    try:
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            status = app.query_one("#status", Static)
            body = app.query_one("#body")
            # The counter must end inside the body container — without the
            # #table height constraint it lands on the footer row and is
            # clipped out of view.
            assert status.region.height >= 1
            assert (
                status.region.y + status.region.height
                <= body.region.y + body.region.height
            )
            assert "250 von 250" in app._last_status
    finally:
        client.close()
