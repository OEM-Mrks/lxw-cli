"""Compose a non-binding feature request for the user to send to the vendor.

This is an **end-customer product**: the assistant must not offer to build or
extend the software itself. When a user wants functionality the tools don't
cover, the ``request_feature`` MCP tool returns a ready-to-send message (subject
+ body) and the vendor's address (oemedia) — the user copies it and sends the
email themselves. The server does **not** send any mail. No promise is made
about whether or when the feature is built.
"""

from __future__ import annotations

DEFAULT_TO = "david@oemedia.de"
SUBJECT = "Lexware-MCP — Funktionsanfrage (unverbindlich)"


class FeatureRequestError(Exception):
    """Raised when a feature request cannot be composed."""


def compose_feature_request(
    *,
    description: str,
    company: str | None = None,
    contact_email: str | None = None,
) -> dict[str, str]:
    """Build a copy-paste-ready feature request. Sends nothing.

    Returns ``{to, subject, body, hinweis}``. Raises
    :class:`FeatureRequestError` on empty input.
    """
    text = (description or "").strip()
    if not text:
        raise FeatureRequestError("Bitte den gewünschten Funktionswunsch beschreiben.")

    subject = SUBJECT + (f" — {company}" if company else "")
    lines = [
        "Unverbindliche Funktionsanfrage zum Lexware-MCP-Tool.",
        "",
        f"Kunde:        {company or '(bitte ergänzen)'}",
        f"Kontakt-Mail: {contact_email or '(bitte ergänzen)'}",
        "",
        "Wunsch:",
        text,
        "",
        "Hinweis: unverbindlich, keine Zusage zur Umsetzung.",
    ]
    return {
        "to": DEFAULT_TO,
        "subject": subject,
        "body": "\n".join(lines),
        "hinweis": (
            "Dies ist ein unverbindlicher Funktionswunsch. Bitte den obigen Text "
            f"per E-Mail an {DEFAULT_TO} senden — der Server verschickt nichts "
            "selbst."
        ),
    }
