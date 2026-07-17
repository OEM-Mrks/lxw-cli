from __future__ import annotations

import pytest

from lxw_cli import feature_request as fr


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        fr.ENV_SMTP_HOST,
        fr.ENV_SMTP_PORT,
        fr.ENV_SMTP_USER,
        fr.ENV_SMTP_PASSWORD,
        fr.ENV_SMTP_STARTTLS,
        fr.ENV_FEATURE_TO,
        fr.ENV_FEATURE_FROM,
    ):
        monkeypatch.delenv(name, raising=False)


def test_empty_description_rejected() -> None:
    with pytest.raises(fr.FeatureRequestError, match="beschreiben"):
        fr.send_feature_request(description="   ")


def test_missing_smtp_config_gives_clear_error() -> None:
    with pytest.raises(fr.FeatureRequestError, match="nicht konfiguriert"):
        fr.send_feature_request(description="Bitte CSV-Export einbauen")


def test_sends_via_starttls_and_builds_message(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(fr.ENV_SMTP_HOST, "smtp.example.com")
    monkeypatch.setenv(fr.ENV_SMTP_USER, "bot@oemedia.de")
    monkeypatch.setenv(fr.ENV_SMTP_PASSWORD, "secret")

    sent: dict[str, object] = {}

    class FakeSMTP:
        def __init__(self, host: str, port: int) -> None:
            sent["host"] = host
            sent["port"] = port

        def __enter__(self) -> FakeSMTP:
            return self

        def __exit__(self, *a: object) -> None:
            return None

        def starttls(self, context: object) -> None:
            sent["starttls"] = True

        def login(self, user: str, password: str) -> None:
            sent["login"] = (user, password)

        def send_message(self, msg: object) -> None:
            sent["msg"] = msg

    monkeypatch.setattr(fr.smtplib, "SMTP", FakeSMTP)

    result = fr.send_feature_request(
        description="Bitte Sammelrechnungen unterstützen",
        company="Muster GmbH",
        contact_email="kunde@muster.de",
    )
    assert result == {"status": "sent", "to": fr.DEFAULT_TO}
    assert sent["host"] == "smtp.example.com"
    assert sent["port"] == 587
    assert sent["starttls"] is True
    assert sent["login"] == ("bot@oemedia.de", "secret")
    msg = sent["msg"]
    assert msg["To"] == "david@oemedia.de"
    assert msg["Reply-To"] == "kunde@muster.de"
    assert "Muster GmbH" in msg["Subject"]
    body = msg.get_content()
    assert "Sammelrechnungen" in body
    assert "unverbindlich" in body.lower()


def test_port_465_uses_ssl(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(fr.ENV_SMTP_HOST, "smtp.example.com")
    monkeypatch.setenv(fr.ENV_SMTP_PORT, "465")
    monkeypatch.setenv(fr.ENV_SMTP_USER, "bot@oemedia.de")
    monkeypatch.setenv(fr.ENV_SMTP_PASSWORD, "secret")
    monkeypatch.setenv(fr.ENV_FEATURE_TO, "empf@oemedia.de")

    used = {}

    class FakeSSL:
        def __init__(self, host: str, port: int, context: object) -> None:
            used["ssl"] = (host, port)

        def __enter__(self) -> FakeSSL:
            return self

        def __exit__(self, *a: object) -> None:
            return None

        def login(self, *a: object) -> None:
            pass

        def send_message(self, msg: object) -> None:
            used["to"] = msg["To"]

    monkeypatch.setattr(fr.smtplib, "SMTP_SSL", FakeSSL)
    result = fr.send_feature_request(description="Test")
    assert result["to"] == "empf@oemedia.de"
    assert used["ssl"] == ("smtp.example.com", 465)
    assert used["to"] == "empf@oemedia.de"
