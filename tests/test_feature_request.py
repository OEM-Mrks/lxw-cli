from __future__ import annotations

import pytest

from lxw_cli import feature_request as fr


def test_empty_description_rejected() -> None:
    with pytest.raises(fr.FeatureRequestError, match="beschreiben"):
        fr.compose_feature_request(description="   ")


def test_composes_message_with_address_and_no_send() -> None:
    out = fr.compose_feature_request(
        description="Bitte Sammelrechnungen unterstützen",
        company="Muster GmbH",
        contact_email="kunde@muster.de",
    )
    assert out["to"] == "david@oemedia.de"
    assert "Muster GmbH" in out["subject"]
    assert "Sammelrechnungen" in out["body"]
    assert "Muster GmbH" in out["body"]
    assert "kunde@muster.de" in out["body"]
    # Explicitly non-binding and clearly a manual-send flow.
    assert "unverbindlich" in out["body"].lower()
    assert "david@oemedia.de" in out["hinweis"]


def test_compose_without_optionals_has_placeholders() -> None:
    out = fr.compose_feature_request(description="Kleiner Wunsch")
    assert out["to"] == "david@oemedia.de"
    assert "(bitte ergänzen)" in out["body"]  # company + contact prompted
