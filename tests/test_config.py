from __future__ import annotations

import stat
import sys

import httpx
import pytest
import respx

import lxw_cli.config as config_mod
from lxw_cli.config import (
    Config,
    config_dir,
    global_env_path,
    load_config,
    load_config_interactive,
)


@pytest.fixture(autouse=True)
def _isolate_config(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Point the global config dir at a throwaway path and start without a key.

    The autouse `_env` fixture in conftest sets LEXWARE_API_KEY; here we drop it
    so the global-config / prompt code paths are actually exercised, and chdir
    to an empty dir so the project's real .env isn't discovered.
    """
    monkeypatch.delenv("LEXWARE_API_KEY", raising=False)
    monkeypatch.setenv("LEXWARE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.chdir(tmp_path)


def test_config_dir_honors_override(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("LEXWARE_CONFIG_DIR", str(tmp_path / "x"))
    assert config_dir() == tmp_path / "x"


def test_global_env_used_when_var_absent(tmp_path) -> None:
    env_file = global_env_path()
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text("LEXWARE_API_KEY=from-global\n", encoding="utf-8")

    cfg = load_config()
    assert cfg.api_key == "from-global"


@respx.mock
def test_prompt_validates_and_persists_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config_mod, "getpass", lambda *_a, **_k: "pasted-key")
    monkeypatch.setattr(config_mod, "_can_prompt", lambda: True)
    route = respx.get("https://api.lexware.io/v1/profile").mock(
        return_value=httpx.Response(200, json={"companyName": "Acme GmbH"})
    )

    cfg = load_config_interactive()

    assert route.called
    assert cfg.api_key == "pasted-key"
    env_file = global_env_path()
    assert env_file.is_file()
    assert "LEXWARE_API_KEY=pasted-key" in env_file.read_text(encoding="utf-8")
    # Owner-only permissions on the file holding the secret (POSIX only —
    # Windows has no meaningful chmod; the user profile's ACLs apply there).
    if sys.platform != "win32":
        assert stat.S_IMODE(env_file.stat().st_mode) == 0o600


@respx.mock
def test_prompt_rejects_invalid_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config_mod, "getpass", lambda *_a, **_k: "bad-key")
    monkeypatch.setattr(config_mod, "_can_prompt", lambda: True)
    respx.get("https://api.lexware.io/v1/profile").mock(
        return_value=httpx.Response(401, json={"message": "unauthorized"})
    )

    with pytest.raises(config_mod.ConfigError):
        load_config_interactive()
    # A rejected key is never written to disk.
    assert not global_env_path().exists()


def test_store_key_preserves_other_lines() -> None:
    path = global_env_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "LEXWARE_API_BASE_URL=https://example.test\nLEXWARE_API_KEY=old\n",
        encoding="utf-8",
    )

    config_mod.store_key("new")

    text = path.read_text(encoding="utf-8")
    assert "LEXWARE_API_BASE_URL=https://example.test" in text
    assert "LEXWARE_API_KEY=new" in text
    assert "old" not in text


def test_no_prompt_when_non_interactive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config_mod, "_can_prompt", lambda: False)

    def _fail(*_a, **_k):
        raise AssertionError("getpass must not be called when non-interactive")

    monkeypatch.setattr(config_mod, "getpass", _fail)
    with pytest.raises(config_mod.ConfigError):
        load_config_interactive()


def test_validated_config_roundtrip() -> None:
    # Sanity: Config is a plain frozen dataclass; base_url default holds.
    cfg = Config(api_key="k")
    assert cfg.base_url == config_mod.DEFAULT_BASE_URL


def test_project_env_cannot_redirect_base_url(
    tmp_path, capsys: pytest.CaptureFixture
) -> None:
    """A CWD-discovered .env must not point the Bearer key at a foreign host."""
    (tmp_path / ".env").write_text(
        "LEXWARE_API_KEY=from-project\nLEXWARE_API_BASE_URL=https://evil.example\n",
        encoding="utf-8",
    )

    cfg = load_config()

    # The key is taken (development convenience), the base URL is not.
    assert cfg.api_key == "from-project"
    assert cfg.base_url == config_mod.DEFAULT_BASE_URL
    assert "wird ignoriert" in capsys.readouterr().err


def test_project_env_base_url_ok_when_matching_environment(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """No warning when the project .env merely repeats the trusted base URL."""
    monkeypatch.setenv("LEXWARE_API_BASE_URL", "https://sandbox.example")
    (tmp_path / ".env").write_text(
        "LEXWARE_API_KEY=k\nLEXWARE_API_BASE_URL=https://sandbox.example\n",
        encoding="utf-8",
    )

    cfg = load_config()

    assert cfg.base_url == "https://sandbox.example"
    assert "wird ignoriert" not in capsys.readouterr().err
