from __future__ import annotations

import pytest

from lexware_cli.config import Config
from lexware_cli.core.client import LexwareClient


@pytest.fixture
def config() -> Config:
    return Config(api_key="test-key", base_url="https://api.lexware.io")


@pytest.fixture
def client(config: Config) -> LexwareClient:
    c = LexwareClient(config)
    yield c
    c.close()


@pytest.fixture(autouse=True)
def _fast_throttle(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable the 2 req/s rate-limit sleep during tests."""
    import lexware_cli.core.client as client_mod

    monkeypatch.setattr(client_mod, "MIN_INTERVAL", 0.0)


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEXWARE_API_KEY", "test-key")
