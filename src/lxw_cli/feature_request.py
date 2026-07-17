"""Forward non-binding feature requests to the vendor by email.

This is an **end-customer product**: the assistant must not offer to build or
extend the software itself. When a user wants functionality the tools don't
cover, the ``request_feature`` MCP tool routes a short, non-binding wish to the
vendor (oemedia) via SMTP. No promise is made about if or when it is built.

SMTP is configured entirely through environment variables so no mail
credentials live in code. If SMTP is not configured, the tool fails with a
clear message rather than silently dropping the request.
"""

from __future__ import annotations

import os
import smtplib
import ssl
from datetime import datetime
from email.message import EmailMessage

ENV_SMTP_HOST = "LXW_MCP_SMTP_HOST"
ENV_SMTP_PORT = "LXW_MCP_SMTP_PORT"  # default 587
ENV_SMTP_USER = "LXW_MCP_SMTP_USER"
ENV_SMTP_PASSWORD = "LXW_MCP_SMTP_PASSWORD"
ENV_SMTP_STARTTLS = "LXW_MCP_SMTP_STARTTLS"  # "true" (default) / "false"
ENV_FEATURE_TO = "LXW_MCP_FEATURE_TO"  # default DEFAULT_TO
ENV_FEATURE_FROM = "LXW_MCP_FEATURE_FROM"  # default: SMTP user

DEFAULT_TO = "david@oemedia.de"
SUBJECT = "Lexware-MCP — Funktionsanfrage (unverbindlich)"


class FeatureRequestError(Exception):
    """Raised when a feature request cannot be sent."""


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "nein", "off")


def send_feature_request(
    *,
    description: str,
    company: str | None = None,
    contact_email: str | None = None,
    now: datetime | None = None,
) -> dict[str, str]:
    """Send one feature request email. Returns a small status dict.

    Raises :class:`FeatureRequestError` on empty input or missing/failing SMTP.
    """
    text = (description or "").strip()
    if not text:
        raise FeatureRequestError("Bitte den gewünschten Funktionswunsch beschreiben.")

    host = os.getenv(ENV_SMTP_HOST, "").strip()
    user = os.getenv(ENV_SMTP_USER, "").strip()
    password = os.getenv(ENV_SMTP_PASSWORD, "")
    if not host or not user or not password:
        raise FeatureRequestError(
            "Funktionsanfragen sind auf diesem Server nicht konfiguriert "
            "(SMTP fehlt). Bitte den Wunsch direkt an " + DEFAULT_TO + " senden."
        )
    port = int(os.getenv(ENV_SMTP_PORT, "587"))
    to_addr = os.getenv(ENV_FEATURE_TO, "").strip() or DEFAULT_TO
    from_addr = os.getenv(ENV_FEATURE_FROM, "").strip() or user
    stamp = (now or datetime.now()).strftime("%Y-%m-%d %H:%M")

    lines = [
        "Unverbindliche Funktionsanfrage über den Lexware-MCP-Server.",
        "",
        f"Kunde:        {company or 'unbekannt'}",
        f"Kontakt-Mail: {contact_email or '(nicht angegeben)'}",
        f"Zeitpunkt:    {stamp}",
        "",
        "Wunsch:",
        text,
        "",
        "— Diese Nachricht wurde automatisch erzeugt. Sie ist unverbindlich und",
        "  stellt keine Zusage zur Umsetzung dar.",
    ]
    msg = EmailMessage()
    msg["Subject"] = SUBJECT + (f" — {company}" if company else "")
    msg["From"] = from_addr
    msg["To"] = to_addr
    if contact_email:
        msg["Reply-To"] = contact_email
    msg.set_content("\n".join(lines))

    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context()) as s:
                s.login(user, password)
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port) as s:
                if _bool_env(ENV_SMTP_STARTTLS, True):
                    s.starttls(context=ssl.create_default_context())
                s.login(user, password)
                s.send_message(msg)
    except (smtplib.SMTPException, OSError) as exc:
        raise FeatureRequestError(
            f"Die Funktionsanfrage konnte nicht per E-Mail versendet werden ({exc})."
        ) from exc

    return {"status": "sent", "to": to_addr}
