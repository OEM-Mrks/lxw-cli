"""Characterization snapshots of the *exact* CLI rendering.

These lock the current human-facing output (Rich tables, grouped contact
tables, and the print_count/ARCHIVED_HINT footers) byte-for-byte, so the
upcoming core-layer refactor stays output-identical. The table layout depends
on the terminal width, so COLUMNS is pinned to a wide, deterministic value.

Snapshots live as files under tests/snapshots/. Regenerate intentionally with:

    UPDATE_SNAPSHOTS=1 pytest tests/test_cli_snapshots.py
"""

from __future__ import annotations

import os
from pathlib import Path

import httpx
import pytest
import respx
from typer.testing import CliRunner

from lxw_cli.cli import app

SNAP_DIR = Path(__file__).parent / "snapshots"

_VOUCHERLIST = "https://api.lexware.io/v1/voucherlist"
_CONTACTS = "https://api.lexware.io/v1/contacts"

_INVOICE_ROW = {
    "id": "u1",
    "voucherNumber": "RG-001",
    "voucherDate": "2026-06-01",
    "voucherStatus": "open",
    "contactName": "Acme GmbH",
    "totalAmount": 119.0,
    "currency": "EUR",
}

_CONTACT_ROWS = [
    {
        "id": "c1",
        "company": {"name": "Kunde GmbH"},
        "roles": {"customer": {"number": "K-1"}},
        "emailAddresses": {"business": ["a@k.de"]},
        "archived": False,
    },
    {
        "id": "c2",
        "company": {"name": "Lieferant AG"},
        "roles": {"vendor": {"number": "L-1"}},
        "archived": False,
    },
    {
        "id": "c3",
        "company": {"name": "Beide GmbH"},
        "roles": {"customer": {"number": "K-3"}, "vendor": {"number": "L-3"}},
        "archived": False,
    },
]


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(autouse=True)
def _pin_width(monkeypatch: pytest.MonkeyPatch) -> None:
    # Rich reads COLUMNS dynamically; pinning it makes table widths
    # deterministic regardless of the runner's environment.
    monkeypatch.setenv("COLUMNS", "200")


def _assert_snapshot(name: str, actual: str) -> None:
    path = SNAP_DIR / name
    if os.environ.get("UPDATE_SNAPSHOTS"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(actual, encoding="utf-8")
    expected = path.read_text(encoding="utf-8")
    assert actual == expected, f"Snapshot mismatch for {name}"


@respx.mock
def test_snapshot_invoices_list_table(runner: CliRunner) -> None:
    respx.get(_VOUCHERLIST).mock(
        return_value=httpx.Response(
            200, json={"content": [_INVOICE_ROW], "last": True, "totalElements": 1}
        )
    )
    result = runner.invoke(app, ["invoices", "list"])
    assert result.exit_code == 0, result.stderr
    _assert_snapshot("invoices_list.txt", result.stdout)
    assert result.stderr == (
        "→ 1 von 1 Rechnungen · ohne archivierte (--include-archived zeigt alle)\n"
    )


@respx.mock
def test_snapshot_invoices_list_footer_capped(runner: CliRunner) -> None:
    respx.get(_VOUCHERLIST).mock(
        return_value=httpx.Response(
            200, json={"content": [_INVOICE_ROW], "last": False, "totalElements": 5}
        )
    )
    result = runner.invoke(app, ["invoices", "list", "--limit", "1"])
    assert result.exit_code == 0, result.stderr
    # Locks the capped count + archived hint exactly.
    assert result.stderr == (
        "→ 1 von 5 Rechnungen angezeigt (mehr mit --all) "
        "· ohne archivierte (--include-archived zeigt alle)\n"
    )


@respx.mock
def test_snapshot_contacts_list_flat(runner: CliRunner) -> None:
    respx.get(_CONTACTS).mock(
        return_value=httpx.Response(
            200, json={"content": _CONTACT_ROWS, "last": True, "totalElements": 3}
        )
    )
    result = runner.invoke(app, ["contacts", "list"])
    assert result.exit_code == 0, result.stderr
    _assert_snapshot("contacts_flat.txt", result.stdout)
    assert result.stderr == "→ 3 von 3 aktiven Kontakten\n"


@respx.mock
def test_snapshot_contacts_list_grouped(runner: CliRunner) -> None:
    respx.get(_CONTACTS).mock(
        return_value=httpx.Response(
            200, json={"content": _CONTACT_ROWS, "last": True, "totalElements": 3}
        )
    )
    result = runner.invoke(app, ["contacts", "list", "--grouped"])
    assert result.exit_code == 0, result.stderr
    _assert_snapshot("contacts_grouped.txt", result.stdout)
    assert result.stderr == "→ 3 von 3 aktiven Kontakten\n"
