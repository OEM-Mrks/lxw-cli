"""Mehrere Lexware-Installationen: Profile in Config, CLI und MCP-Server."""

from __future__ import annotations

import json
import stat
import subprocess
import sys

import httpx
import pytest
import respx
from fastmcp import Client
from typer.testing import CliRunner

from lxw_cli.cli import app
from lxw_cli.config import (
    active_profile,
    global_env_path,
    list_profiles,
    load_config,
    profile_env_path,
    store_key,
)
from lxw_cli.core.errors import ConfigError

API = "https://api.lexware.io"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    # conftest exportiert LEXWARE_API_KEY=test-key — genau der Fall, der ein
    # Profil NICHT umlenken darf. Darum bleibt er hier bewusst gesetzt.
    monkeypatch.setenv("LEXWARE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.delenv("LEXWARE_PROFILE", raising=False)
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def two_profiles() -> None:
    store_key("key-oemedia-1234567890", "oemedia")
    store_key("key-demo-1234567890", "demo")


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def test_profile_key_ignores_exported_env_key(two_profiles) -> None:
    cfg = load_config("demo")
    assert cfg.api_key == "key-demo-1234567890"
    assert cfg.profile == "demo"
    # Ohne Profil bleibt alles wie bisher: die Env-Variable gewinnt.
    assert load_config().api_key == "test-key"
    assert load_config().profile is None


def test_profile_from_env_var(monkeypatch: pytest.MonkeyPatch, two_profiles) -> None:
    monkeypatch.setenv("LEXWARE_PROFILE", "oemedia")
    assert load_config().api_key == "key-oemedia-1234567890"
    # Ein explizites Argument gewinnt über die Env-Variable, "" = klassisch.
    assert load_config("demo").api_key == "key-demo-1234567890"
    assert load_config("").api_key == "test-key"


def test_missing_profile_never_falls_back() -> None:
    with pytest.raises(ConfigError, match="lxw profiles add ghost"):
        load_config("ghost")


def test_default_profile_is_classic_resolution() -> None:
    assert active_profile("default") is None
    assert active_profile("Default") is None
    assert load_config("default").api_key == "test-key"


@pytest.mark.parametrize("name", ["../x", "a b", "", "-x", "x" * 33, "ä"])
def test_invalid_profile_names(name: str) -> None:
    with pytest.raises(ConfigError):
        profile_env_path(name or " ")


def test_store_and_list_profiles(two_profiles) -> None:
    path = profile_env_path("demo")
    assert path.read_text(encoding="utf-8") == "LEXWARE_API_KEY=key-demo-1234567890\n"
    if sys.platform != "win32":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert list_profiles() == ["demo", "oemedia"]
    store_key("classic-key-1234567890")
    assert list_profiles() == ["default", "demo", "oemedia"]
    assert global_env_path().is_file()


def test_profile_keeps_own_base_url(two_profiles) -> None:
    path = profile_env_path("demo")
    path.write_text(
        path.read_text(encoding="utf-8") + "LEXWARE_API_BASE_URL=https://sandbox.example/\n",
        encoding="utf-8",
    )
    assert load_config("demo").base_url == "https://sandbox.example"
    assert load_config("oemedia").base_url == API


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


@respx.mock
def test_cli_profile_option_uses_profile_key(runner: CliRunner, two_profiles) -> None:
    route = respx.get(f"{API}/v1/profile").mock(
        return_value=httpx.Response(200, json={"companyName": "Demo GmbH"})
    )
    result = runner.invoke(app, ["--json", "-p", "demo", "profile"])
    assert result.exit_code == 0, result.stderr
    assert route.calls.last.request.headers["authorization"] == "Bearer key-demo-1234567890"

    result = runner.invoke(app, ["--json", "profile"], env={"LEXWARE_PROFILE": "oemedia"})
    assert result.exit_code == 0, result.stderr
    assert route.calls.last.request.headers["authorization"] == "Bearer key-oemedia-1234567890"


def test_cli_unknown_profile_errors(runner: CliRunner) -> None:
    result = runner.invoke(app, ["-p", "ghost", "profile"])
    assert result.exit_code == 2
    assert "lxw profiles add ghost" in result.stderr


def test_cli_invalid_profile_name(runner: CliRunner) -> None:
    result = runner.invoke(app, ["-p", "../etc", "profile"])
    assert result.exit_code != 0


@respx.mock
def test_profiles_add_list_remove(runner: CliRunner) -> None:
    respx.get(f"{API}/v1/profile").mock(
        return_value=httpx.Response(200, json={"companyName": "Demo GmbH"})
    )
    result = runner.invoke(app, ["profiles", "add", "Demo"], input="key-demo-1234567890\n")
    assert result.exit_code == 0, result.stderr
    assert "Demo GmbH" in result.stdout
    assert load_config("demo").api_key == "key-demo-1234567890"

    # Zweites Anlegen nur mit --force.
    again = runner.invoke(app, ["profiles", "add", "demo"], input="x\n")
    assert again.exit_code == 1

    listed = runner.invoke(app, ["profiles", "list", "--check"])
    assert listed.exit_code == 0
    assert "demo" in listed.stdout and "Demo GmbH" in listed.stdout

    removed = runner.invoke(app, ["profiles", "remove", "demo", "--yes"])
    assert removed.exit_code == 0, removed.stderr
    assert list_profiles() == []


@respx.mock
def test_profiles_add_rejects_bad_key(runner: CliRunner) -> None:
    respx.get(f"{API}/v1/profile").mock(return_value=httpx.Response(401, json={}))
    result = runner.invoke(app, ["profiles", "add", "demo"], input="bad-key-1234567890\n")
    assert result.exit_code == 2
    assert list_profiles() == []


def test_wants_tui_with_profile_only() -> None:
    from lxw_cli.cli import _tui_profile_arg, _wants_tui

    assert _wants_tui(["lxw", "-p", "demo"], stdin_tty=True, stdout_tty=True)
    assert _wants_tui(["lxw", "--profile=demo"], stdin_tty=True, stdout_tty=True)
    assert not _wants_tui(["lxw", "-p", "demo", "invoices"], stdin_tty=True, stdout_tty=True)
    assert _tui_profile_arg(["lxw", "--profile", "demo"]) == (True, "demo")


# ---------------------------------------------------------------------------
# MCP-Registrierung: ein Server pro Installation
# ---------------------------------------------------------------------------


@pytest.fixture
def desktop_config(monkeypatch: pytest.MonkeyPatch, tmp_path):
    import lxw_cli.commands.mcp as mcp_cmd_mod

    monkeypatch.setattr(mcp_cmd_mod.shutil, "which", lambda name: f"/usr/bin/{name}")
    path = tmp_path / "Claude" / "claude_desktop_config.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    monkeypatch.setenv("CLAUDE_DESKTOP_CONFIG", str(path))
    return path


def test_install_desktop_side_by_side(runner: CliRunner, desktop_config, two_profiles) -> None:
    assert runner.invoke(app, ["mcp", "install-desktop"]).exit_code == 0
    assert runner.invoke(app, ["mcp", "install-desktop", "--profile", "oemedia"]).exit_code == 0
    # Globales -p wirkt genauso.
    assert runner.invoke(app, ["-p", "demo", "mcp", "install-desktop"]).exit_code == 0

    raw = desktop_config.read_text(encoding="utf-8")
    servers = json.loads(raw)["mcpServers"]
    assert servers["lexware"] == {"command": "/usr/bin/lxw-mcp"}
    assert servers["lexware-oemedia"] == {
        "command": "/usr/bin/lxw-mcp",
        "args": ["--profile", "oemedia"],
    }
    assert servers["lexware-demo"]["args"] == ["--profile", "demo"]
    # Keine Keys in der Claude-Config.
    assert "key-" not in raw and "test-key" not in raw

    status = runner.invoke(app, ["mcp", "status"])
    assert "lexware-demo" in status.stdout and "lexware-oemedia" in status.stdout

    result = runner.invoke(app, ["mcp", "uninstall-desktop", "-p", "demo"])
    assert result.exit_code == 0, result.stderr
    servers = json.loads(desktop_config.read_text(encoding="utf-8"))["mcpServers"]
    assert set(servers) == {"lexware", "lexware-oemedia"}


def test_install_desktop_unknown_profile(runner: CliRunner, desktop_config) -> None:
    result = runner.invoke(app, ["mcp", "install-desktop", "--profile", "ghost"])
    assert result.exit_code == 2
    assert json.loads(desktop_config.read_text(encoding="utf-8"))["mcpServers"] == {}


def test_install_claude_with_profile(
    monkeypatch: pytest.MonkeyPatch, runner: CliRunner, two_profiles
) -> None:
    import lxw_cli.commands.mcp as mcp_cmd_mod

    monkeypatch.setattr(mcp_cmd_mod.shutil, "which", lambda name: f"/usr/bin/{name}")
    calls: list[list[str]] = []

    def fake_run(cmd, **_kwargs):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(mcp_cmd_mod.subprocess, "run", fake_run)
    result = runner.invoke(app, ["mcp", "install-claude", "--profile", "demo", "--force"])
    assert result.exit_code == 0, result.stderr
    assert ["claude", "mcp", "remove", "lexware-demo", "--scope", "user"] in calls
    add = next(c for c in calls if c[:3] == ["claude", "mcp", "add"])
    assert add == [
        "claude", "mcp", "add", "lexware-demo", "--scope", "user", "--",
        "/usr/bin/lxw-mcp", "--profile", "demo",
    ]
    assert all("key-" not in arg for arg in add)
    # Der klassische globale Key wird bei Profilen nicht angefasst.
    assert not global_env_path().exists()


# ---------------------------------------------------------------------------
# stdio-MCP-Server, fest an ein Profil gebunden
# ---------------------------------------------------------------------------


@pytest.fixture
def stdio_server(monkeypatch: pytest.MonkeyPatch):
    import lxw_cli.mcp_server as mod

    base = mod.mcp.instructions
    monkeypatch.setattr(mod.mcp, "run", lambda *a, **k: None)
    yield mod
    mod._profile = None
    mod._client = None
    mod.mcp.instructions = base


@respx.mock
@pytest.mark.asyncio
async def test_stdio_server_pinned_to_profile(stdio_server, two_profiles) -> None:
    route = respx.get(f"{API}/v1/profile").mock(
        return_value=httpx.Response(200, json={"companyName": "Demo GmbH"})
    )
    stdio_server.serve_stdio("demo")
    assert "installation 'demo'" in stdio_server.mcp.instructions

    async with Client(stdio_server.mcp) as c:
        await c.call_tool("profile", {})
        version = await c.call_tool("version", {})
    # Trotz exportiertem LEXWARE_API_KEY geht der Aufruf mit dem Demo-Key raus.
    assert route.calls.last.request.headers["authorization"] == "Bearer key-demo-1234567890"
    assert version.data["installation"] == "demo"


def test_stdio_entrypoint_parses_profile(
    monkeypatch: pytest.MonkeyPatch, stdio_server, two_profiles
) -> None:
    monkeypatch.setattr(sys, "argv", ["lxw-mcp", "--profile", "oemedia"])
    stdio_server.run()
    assert stdio_server._profile == "oemedia"

    monkeypatch.setattr(sys, "argv", ["lxw-mcp"])
    monkeypatch.setenv("LEXWARE_PROFILE", "demo")
    stdio_server.run()
    assert stdio_server._profile == "demo"


def test_stdio_entrypoint_fails_loudly_for_missing_profile(
    monkeypatch: pytest.MonkeyPatch, stdio_server
) -> None:
    monkeypatch.setattr(sys, "argv", ["lxw-mcp", "--profile", "ghost"])
    with pytest.raises(SystemExit) as exc:
        stdio_server.run()
    assert exc.value.code == 2


def test_mcp_serve_command_passes_profile(
    monkeypatch: pytest.MonkeyPatch, runner: CliRunner, stdio_server, two_profiles
) -> None:
    result = runner.invoke(app, ["mcp", "serve", "--profile", "oemedia"])
    assert result.exit_code == 0, result.stderr
    assert stdio_server._profile == "oemedia"
