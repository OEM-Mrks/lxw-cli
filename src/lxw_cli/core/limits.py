"""Length limits for Lexware text fields, checked before the API call.

Lexware enforces maximum lengths on several text fields and rejects an
over-long value with an opaque HTTP 400/406. We validate the well-known limits
up front and raise a clear, human-friendly message instead of letting the
caller burn an API round-trip on a cryptic error.

Limits Lexware does *not* publish a number for are caught reactively by the
client's error translation (see ``core.client._extract_message``); this module
only carries the ones we have verified.

The limits are context-dependent, not global by field name: a *voucher* title
may be 25 characters, but an *article* title (Artikelbezeichnung) may be 100 —
hence one validator per entity rather than a flat field→limit map.
"""

from __future__ import annotations

from typing import Any

from lxw_cli.core.errors import LexwareError

# -- Verified character limits ---------------------------------------------
# Voucher = invoice / quotation / order-confirmation / delivery-note.
VOUCHER_TITLE_MAX = 25
VOUCHER_INTRODUCTION_MAX = 2000
VOUCHER_REMARK_MAX = 2000
LINE_ITEM_NAME_MAX = 100
LINE_ITEM_DESCRIPTION_MAX = 2000

# Article master data.
ARTICLE_TITLE_MAX = 100
ARTICLE_DESCRIPTION_MAX = 2000

# Contact / contact person.
CONTACT_NOTE_MAX = 1000
SALUTATION_MAX = 25


def _check(
    value: Any, maximum: int, label: str, violations: list[str]
) -> None:
    """Record a violation when ``value`` is a string longer than ``maximum``."""
    if isinstance(value, str) and len(value) > maximum:
        violations.append(
            f"{label} darf höchstens {maximum} Zeichen haben "
            f"(aktuell {len(value)})."
        )


def _raise(violations: list[str]) -> None:
    if not violations:
        return
    if len(violations) == 1:
        raise LexwareError(violations[0] + " Bitte den Text kürzen.")
    joined = "\n- ".join(violations)
    raise LexwareError(
        "Einige Texte überschreiten die zulässige Länge:\n- "
        + joined
        + "\nBitte die Texte kürzen."
    )


def validate_voucher(body: dict[str, Any]) -> None:
    """Check the text fields of a sales document body before POSTing it.

    Only keys that are present are checked, so this is safe to call on any
    document body (invoice, quotation, order-confirmation, delivery-note).
    """
    if not isinstance(body, dict):
        return
    violations: list[str] = []
    _check(body.get("title"), VOUCHER_TITLE_MAX, "Der Belegtitel", violations)
    _check(
        body.get("introduction"),
        VOUCHER_INTRODUCTION_MAX,
        "Der Einleitungstext",
        violations,
    )
    _check(
        body.get("remark"), VOUCHER_REMARK_MAX, "Die Nachbemerkung", violations
    )
    items = body.get("lineItems")
    if isinstance(items, list):
        for index, item in enumerate(items, start=1):
            if not isinstance(item, dict):
                continue
            _check(
                item.get("name"),
                LINE_ITEM_NAME_MAX,
                f"Die Bezeichnung von Position {index}",
                violations,
            )
            _check(
                item.get("description"),
                LINE_ITEM_DESCRIPTION_MAX,
                f"Die Beschreibung von Position {index}",
                violations,
            )
    _raise(violations)


def validate_article(body: dict[str, Any]) -> None:
    """Check an article body (``title`` = Bezeichnung, ``description``)."""
    if not isinstance(body, dict):
        return
    violations: list[str] = []
    _check(
        body.get("title"), ARTICLE_TITLE_MAX, "Die Artikelbezeichnung", violations
    )
    _check(
        body.get("description"),
        ARTICLE_DESCRIPTION_MAX,
        "Die Artikelbeschreibung",
        violations,
    )
    _raise(violations)


def validate_contact(body: dict[str, Any]) -> None:
    """Check a contact body: ``note`` plus each contact person's ``salutation``."""
    if not isinstance(body, dict):
        return
    violations: list[str] = []
    _check(body.get("note"), CONTACT_NOTE_MAX, "Die Notiz", violations)

    person = body.get("person")
    if isinstance(person, dict):
        _check(person.get("salutation"), SALUTATION_MAX, "Die Anrede", violations)

    company = body.get("company")
    if isinstance(company, dict):
        persons = company.get("contactPersons")
        if isinstance(persons, list):
            multiple = len(persons) > 1
            for index, contact_person in enumerate(persons, start=1):
                if not isinstance(contact_person, dict):
                    continue
                label = (
                    f"Die Anrede von Ansprechpartner {index}"
                    if multiple
                    else "Die Anrede des Ansprechpartners"
                )
                _check(
                    contact_person.get("salutation"),
                    SALUTATION_MAX,
                    label,
                    violations,
                )
    _raise(violations)
