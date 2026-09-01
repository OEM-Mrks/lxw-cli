from __future__ import annotations

import pytest

from lxw_cli.config import Config
from lxw_cli.core import status as _status
from lxw_cli.core.client import LexwareClient

# Hier gegriffen, weil conftest garantiert vor jedem Testmodul importiert wird
# — und damit vor der Sperre in _offline_status.
_REAL_STATUS_FETCH = _status._fetch


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
    import lxw_cli.core.client as client_mod

    monkeypatch.setattr(client_mod, "MIN_INTERVAL", 0.0)


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEXWARE_API_KEY", "test-key")


@pytest.fixture(autouse=True)
def _offline_status(monkeypatch: pytest.MonkeyPatch):
    """Keine echten Abrufe der Lexware-Statusseite aus Tests heraus.

    Der Fehlerpfad schlägt bei 5xx/Timeout auf status.lexware.de nach — ohne
    diese Sperre machte jeder Fehlertest einen echten Netzwerkaufruf und
    ginge je nach Lexwares Betriebslage anders aus. Der Abruf schlägt hier
    fehl, `get_status` liefert damit None: genau der Fall "Statusseite nicht
    erreichbar", der ohnehin sauber abgefangen sein muss.

    tests/test_status.py hebt die Sperre gezielt wieder auf.
    """
    from lxw_cli.core import status

    def _blocked(**_: object) -> None:
        raise RuntimeError("Statusabruf im Test gesperrt")

    status.clear_cache()
    monkeypatch.setattr(status, "_fetch", _blocked)
    yield
    status.clear_cache()


@pytest.fixture
def status_online(monkeypatch: pytest.MonkeyPatch) -> None:
    """Die Sperre aus :func:`_offline_status` gezielt aufheben.

    Der echte Abruf läuft dann gegen die respx-Mocks des Tests — nie gegen
    das Netz.
    """
    monkeypatch.setattr(_status, "_fetch", _REAL_STATUS_FETCH)
    _status.clear_cache()
