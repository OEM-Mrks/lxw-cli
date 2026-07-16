from __future__ import annotations

import json

import httpx
import pytest
import respx
from typer.testing import CliRunner

from lxw_cli.cli import app


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_version(runner: CliRunner) -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "lxw-cli" in result.stdout


def test_wants_tui_decision() -> None:
    from lxw_cli.cli import _wants_tui

    # Bare invocation on an interactive terminal → TUI.
    assert _wants_tui(["lxw"], stdin_tty=True, stdout_tty=True)
    assert _wants_tui([], stdin_tty=True, stdout_tty=True)
    # Any argument → CLI.
    assert not _wants_tui(["lxw", "invoices"], stdin_tty=True, stdout_tty=True)
    assert not _wants_tui(["lxw", "--help"], stdin_tty=True, stdout_tty=True)
    # Non-interactive (piping/scripting) → CLI, never the TUI.
    assert not _wants_tui(["lxw"], stdin_tty=True, stdout_tty=False)
    assert not _wants_tui(["lxw"], stdin_tty=False, stdout_tty=True)


def test_missing_key_errors(
    monkeypatch: pytest.MonkeyPatch, runner: CliRunner, tmp_path
) -> None:
    monkeypatch.delenv("LEXWARE_API_KEY", raising=False)
    # Move CWD away from the real .env in the project root so load_dotenv()
    # can't find the developer's key during this test, and point the global
    # config at an empty dir so a real ~/.config/lexware/.env isn't picked up.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LEXWARE_CONFIG_DIR", str(tmp_path / "cfg"))
    # CliRunner's stdin is not a TTY, so load_config won't prompt.
    result = runner.invoke(app, ["profile"])
    assert result.exit_code != 0
    assert "LEXWARE_API_KEY" in result.stderr


def test_mcp_install_claude_keeps_key_out_of_argv(
    monkeypatch: pytest.MonkeyPatch, runner: CliRunner, tmp_path
) -> None:
    """The API key must never appear in `claude mcp add` process arguments."""
    import subprocess

    import lxw_cli.commands.mcp as mcp_cmd_mod
    from lxw_cli.config import global_env_path

    monkeypatch.setenv("LEXWARE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setattr(mcp_cmd_mod.shutil, "which", lambda name: f"/usr/bin/{name}")
    calls: list[list[str]] = []

    def fake_run(cmd, **_kwargs):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(mcp_cmd_mod.subprocess, "run", fake_run)

    result = runner.invoke(app, ["mcp", "install-claude"])

    assert result.exit_code == 0, result.stderr
    add_cmd = next(c for c in calls if c[:3] == ["claude", "mcp", "add"])
    assert "-e" not in add_cmd
    assert all("test-key" not in arg for arg in add_cmd)
    # Instead the key is persisted to the global .env, where the server reads it.
    assert "LEXWARE_API_KEY=test-key" in global_env_path().read_text(encoding="utf-8")


@pytest.fixture
def desktop_config(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """Point the desktop config at a tmp file pre-filled with foreign entries."""
    import lxw_cli.commands.mcp as mcp_cmd_mod

    monkeypatch.setenv("LEXWARE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setattr(
        mcp_cmd_mod.shutil, "which", lambda name: f"/usr/bin/{name}"
    )
    path = tmp_path / "Claude" / "claude_desktop_config.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "mcpServers": {"other": {"command": "npx", "args": ["x"]}},
                "preferences": {"keep": True},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CLAUDE_DESKTOP_CONFIG", str(path))
    return path


def test_mcp_install_desktop_writes_config(runner: CliRunner, desktop_config) -> None:
    result = runner.invoke(app, ["mcp", "install-desktop"])
    assert result.exit_code == 0, result.stderr

    data = json.loads(desktop_config.read_text(encoding="utf-8"))
    entry = data["mcpServers"]["lexware"]
    # Absolute command path — Desktop doesn't inherit the shell PATH.
    assert entry["command"] == "/usr/bin/lxw-mcp"
    # Foreign servers and unrelated settings survive the merge untouched.
    assert data["mcpServers"]["other"] == {"command": "npx", "args": ["x"]}
    assert data["preferences"] == {"keep": True}
    # The API key never lands in the desktop config …
    assert "test-key" not in desktop_config.read_text(encoding="utf-8")
    # … but in the global .env, where the server reads it.
    from lxw_cli.config import global_env_path

    assert "LEXWARE_API_KEY=test-key" in global_env_path().read_text(encoding="utf-8")


def test_mcp_install_desktop_existing_needs_force(
    runner: CliRunner, desktop_config
) -> None:
    assert runner.invoke(app, ["mcp", "install-desktop"]).exit_code == 0
    # Second run without --force refuses …
    assert runner.invoke(app, ["mcp", "install-desktop"]).exit_code == 1
    # … with --force it overwrites.
    result = runner.invoke(app, ["mcp", "install-desktop", "--force"])
    assert result.exit_code == 0, result.stderr


def test_mcp_uninstall_desktop_removes_only_lexware(
    runner: CliRunner, desktop_config
) -> None:
    assert runner.invoke(app, ["mcp", "install-desktop"]).exit_code == 0
    result = runner.invoke(app, ["mcp", "uninstall-desktop"])
    assert result.exit_code == 0, result.stderr
    data = json.loads(desktop_config.read_text(encoding="utf-8"))
    assert "lexware" not in data["mcpServers"]
    assert "other" in data["mcpServers"]
    # Removing again is a clean no-op error, not a crash.
    assert runner.invoke(app, ["mcp", "uninstall-desktop"]).exit_code == 1


def test_mcp_install_desktop_leaves_broken_json_untouched(
    runner: CliRunner, desktop_config
) -> None:
    desktop_config.write_text("{not json", encoding="utf-8")
    result = runner.invoke(app, ["mcp", "install-desktop"])
    assert result.exit_code == 2
    # The corrupt file was not overwritten.
    assert desktop_config.read_text(encoding="utf-8") == "{not json"


@respx.mock
def test_invoices_list_default_caps_at_25(runner: CliRunner) -> None:
    page0 = [{"id": f"a{i}"} for i in range(100)]
    respx.get("https://api.lexware.io/v1/voucherlist", params={"page": 0}).mock(
        return_value=httpx.Response(200, json={"content": page0, "last": False})
    )
    result = runner.invoke(app, ["--json", "invoices", "list"])
    assert result.exit_code == 0, result.stderr
    # Default --limit 25 caps client-side even though the page held 100.
    assert len(json.loads(result.stdout)) == 25


@respx.mock
def test_invoices_list_hides_archived_by_default(runner: CliRunner) -> None:
    route = respx.get("https://api.lexware.io/v1/voucherlist").mock(
        return_value=httpx.Response(200, json={"content": [], "last": True})
    )
    result = runner.invoke(app, ["invoices", "list"])
    assert result.exit_code == 0, result.stderr
    # Archived filtered out server-side, and the footer says so.
    assert "archived=false" in str(route.calls.last.request.url)
    assert "ohne archivierte" in result.stderr


@respx.mock
def test_invoices_list_include_archived_drops_filter(runner: CliRunner) -> None:
    route = respx.get("https://api.lexware.io/v1/voucherlist").mock(
        return_value=httpx.Response(200, json={"content": [], "last": True})
    )
    result = runner.invoke(app, ["invoices", "list", "--include-archived"])
    assert result.exit_code == 0, result.stderr
    # No archived filter → API returns both archived and non-archived.
    assert "archived" not in str(route.calls.last.request.url)
    assert "ohne archivierte" not in result.stderr


@respx.mock
def test_invoices_list_all_walks_every_page(runner: CliRunner) -> None:
    page0 = [{"id": f"a{i}"} for i in range(20)]
    page1 = [{"id": f"b{i}"} for i in range(15)]
    respx.get("https://api.lexware.io/v1/voucherlist", params={"page": 0}).mock(
        return_value=httpx.Response(200, json={"content": page0, "last": False})
    )
    respx.get("https://api.lexware.io/v1/voucherlist", params={"page": 1}).mock(
        return_value=httpx.Response(200, json={"content": page1, "last": True})
    )
    result = runner.invoke(app, ["--json", "invoices", "list", "--all"])
    assert result.exit_code == 0, result.stderr
    assert len(json.loads(result.stdout)) == 35


@respx.mock
def test_invoices_list_limit_zero_is_unlimited(runner: CliRunner) -> None:
    page0 = [{"id": f"a{i}"} for i in range(20)]
    page1 = [{"id": f"b{i}"} for i in range(15)]
    respx.get("https://api.lexware.io/v1/voucherlist", params={"page": 0}).mock(
        return_value=httpx.Response(200, json={"content": page0, "last": False})
    )
    respx.get("https://api.lexware.io/v1/voucherlist", params={"page": 1}).mock(
        return_value=httpx.Response(200, json={"content": page1, "last": True})
    )
    result = runner.invoke(app, ["--json", "invoices", "list", "--limit", "0"])
    assert result.exit_code == 0, result.stderr
    assert len(json.loads(result.stdout)) == 35


@respx.mock
def test_profile_json_pipe(runner: CliRunner) -> None:
    respx.get("https://api.lexware.io/v1/profile").mock(
        return_value=httpx.Response(200, json={"companyName": "Acme GmbH"})
    )
    result = runner.invoke(app, ["--json", "profile"])
    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout) == {"companyName": "Acme GmbH"}


@respx.mock
def test_invoices_list_uses_voucherlist(runner: CliRunner) -> None:
    route = respx.get("https://api.lexware.io/v1/voucherlist").mock(
        return_value=httpx.Response(
            200,
            json={
                "content": [
                    {
                        "id": "abc",
                        "voucherType": "salesinvoice",
                        "voucherNumber": "R-001",
                        "voucherDate": "2026-01-01",
                        "voucherStatus": "open",
                        "totalAmount": 119.0,
                        "currency": "EUR",
                    }
                ],
                "last": True,
            },
        )
    )
    result = runner.invoke(app, ["invoices", "list", "--limit", "5"])
    assert result.exit_code == 0, result.stderr
    sent_url = str(route.calls.last.request.url)
    assert "voucherType=salesinvoice" in sent_url
    assert "R-001" in result.stdout


@respx.mock
def test_invoices_get_by_number_resolves_uuid(runner: CliRunner) -> None:
    # Lookup by voucherNumber → returns voucher with UUID
    respx.get(
        "https://api.lexware.io/v1/voucherlist",
        params={"voucherNumber": "FB2600682"},
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "content": [{"id": "resolved-uuid", "voucherNumber": "FB2600682"}],
                "last": True,
            },
        )
    )
    # Then GET the actual invoice by resolved UUID
    respx.get("https://api.lexware.io/v1/invoices/resolved-uuid").mock(
        return_value=httpx.Response(
            200, json={"id": "resolved-uuid", "voucherNumber": "FB2600682"}
        )
    )
    result = runner.invoke(app, ["--json", "invoices", "get", "FB2600682"])
    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout)["voucherNumber"] == "FB2600682"


@respx.mock
def test_invoices_pdf_writes_file(runner: CliRunner, tmp_path) -> None:
    uuid = "1a3c79ca-1804-4ab6-a3a5-915cc762b2ce"
    respx.get(f"https://api.lexware.io/v1/invoices/{uuid}/file").mock(
        return_value=httpx.Response(200, content=b"%PDF-1.4 fake")
    )
    target = tmp_path / "out.pdf"
    result = runner.invoke(app, ["invoices", "pdf", uuid, "--output", str(target)])
    assert result.exit_code == 0, result.stderr
    assert target.read_bytes().startswith(b"%PDF")


_CONTACTS = [
    {
        "id": "c1",
        "company": {"name": "Kunde GmbH"},
        "roles": {"customer": {"number": "K1"}},
    },
    {
        "id": "c2",
        "company": {"name": "Lieferant AG"},
        "roles": {"vendor": {"number": "L1"}},
    },
    {
        "id": "c3",
        "company": {"name": "Beide GmbH"},
        "roles": {"customer": {"number": "K3"}, "vendor": {"number": "L3"}},
    },
]


@respx.mock
def test_contacts_list_default_is_flat(runner: CliRunner) -> None:
    respx.get("https://api.lexware.io/v1/contacts").mock(
        return_value=httpx.Response(200, json={"content": _CONTACTS, "last": True})
    )
    result = runner.invoke(app, ["contacts", "list"])
    assert result.exit_code == 0, result.stderr
    # Default: one combined list — no Kunden/Lieferanten section headers, and
    # the dual-role contact appears exactly once.
    assert "Lieferanten" not in result.stdout
    assert result.stdout.count("Beide GmbH") == 1


@respx.mock
def test_contacts_list_grouped_opt_in(runner: CliRunner) -> None:
    respx.get("https://api.lexware.io/v1/contacts").mock(
        return_value=httpx.Response(200, json={"content": _CONTACTS, "last": True})
    )
    result = runner.invoke(app, ["contacts", "list", "--grouped"])
    assert result.exit_code == 0, result.stderr
    out = result.stdout
    assert "Kunden" in out
    assert "Lieferanten" in out
    assert out.index("Kunden") < out.index("Lieferanten")
    # The dual-role contact shows up in both groups.
    assert out.count("Beide GmbH") == 2


@respx.mock
def test_contacts_list_prints_total_count(runner: CliRunner) -> None:
    respx.get("https://api.lexware.io/v1/contacts").mock(
        return_value=httpx.Response(
            200, json={"content": _CONTACTS, "last": True, "totalElements": 87}
        )
    )
    # With archived included, the footer uses the API total (totalElements).
    result = runner.invoke(app, ["contacts", "list", "--include-archived"])
    assert result.exit_code == 0, result.stderr
    assert "87" in result.stderr
    assert "Kontakte" in result.stderr


_CONTACTS_ARCHIVED = [
    {
        "id": "a1",
        "company": {"name": "Aktiv GmbH"},
        "roles": {"customer": {"number": "K1"}},
        "archived": False,
    },
    {
        "id": "a2",
        "company": {"name": "Archiv GmbH"},
        "roles": {"customer": {"number": "K2"}},
        "archived": True,
    },
]


@respx.mock
def test_contacts_list_hides_archived_by_default(runner: CliRunner) -> None:
    respx.get("https://api.lexware.io/v1/contacts").mock(
        return_value=httpx.Response(
            200, json={"content": _CONTACTS_ARCHIVED, "last": True, "totalElements": 2}
        )
    )
    result = runner.invoke(app, ["contacts", "list"])
    assert result.exit_code == 0, result.stderr
    assert "Aktiv GmbH" in result.stdout
    assert "Archiv GmbH" not in result.stdout
    assert "archivierte ausgeblendet" in result.stderr


@respx.mock
def test_contacts_list_include_archived_shows_them(runner: CliRunner) -> None:
    respx.get("https://api.lexware.io/v1/contacts").mock(
        return_value=httpx.Response(
            200, json={"content": _CONTACTS_ARCHIVED, "last": True, "totalElements": 2}
        )
    )
    result = runner.invoke(app, ["contacts", "list", "--include-archived"])
    assert result.exit_code == 0, result.stderr
    assert "Archiv GmbH" in result.stdout


@respx.mock
def test_contacts_list_only_vendors(runner: CliRunner) -> None:
    route = respx.get("https://api.lexware.io/v1/contacts").mock(
        return_value=httpx.Response(
            200, json={"content": [_CONTACTS[1]], "last": True}
        )
    )
    result = runner.invoke(app, ["contacts", "list", "--vendor"])
    assert result.exit_code == 0, result.stderr
    # The vendor filter is passed through to the API.
    assert "vendor=true" in str(route.calls.last.request.url)
    assert "Lieferant AG" in result.stdout


@respx.mock
def test_contacts_list_json_is_flat(runner: CliRunner) -> None:
    respx.get("https://api.lexware.io/v1/contacts").mock(
        return_value=httpx.Response(200, json={"content": _CONTACTS, "last": True})
    )
    result = runner.invoke(app, ["--json", "contacts", "list"])
    assert result.exit_code == 0, result.stderr
    data = json.loads(result.stdout)
    assert isinstance(data, list)
    assert len(data) == 3
    assert {d["role"] for d in data} == {"customer", "vendor", "customer+vendor"}


@respx.mock
def test_contacts_create(runner: CliRunner) -> None:
    route = respx.post("https://api.lexware.io/v1/contacts").mock(
        return_value=httpx.Response(200, json={"id": "new-id"})
    )
    result = runner.invoke(
        app,
        [
            "--json",
            "contacts",
            "create",
            "--body",
            '{"roles":{"customer":{}},"company":{"name":"Test"}}',
        ],
    )
    assert result.exit_code == 0, result.stderr
    assert route.call_count == 1
    body = json.loads(route.calls.last.request.content)
    assert body["company"]["name"] == "Test"
