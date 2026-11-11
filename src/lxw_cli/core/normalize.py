"""Turn what language models typically send into what Lexware accepts.

Collected from a full test run of every MCP tool against a demo account
(2026-11-10): German words for states and types, prices as "49,90 €", country
names instead of ISO codes, roles as a list, flat `email`/`phone` fields and so
on. Every function either returns a value Lexware accepts or raises
:class:`LexwareError` with a German message naming the valid values — never a
bare HTTP 400/406 from Lexware.
"""

from __future__ import annotations

import re
import unicodedata
from decimal import Decimal, InvalidOperation
from typing import Any

from lxw_cli.core.errors import LexwareError

# -- text hygiene ------------------------------------------------------------

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def strip_control(value: Any) -> Any:
    """Remove control characters (keep tab/newline) from all strings, deeply.

    Lexware answers a NUL byte with HTTP 500 and stores others (e.g. BEL)
    that later break PDFs/XRechnung.
    """
    if isinstance(value, str):
        return _CONTROL.sub("", value)
    if isinstance(value, dict):
        return {k: strip_control(v) for k, v in value.items()}
    if isinstance(value, list):
        return [strip_control(v) for v in value]
    return value


# -- voucher status ----------------------------------------------------------

# Values /v1/voucherlist accepts for voucherStatus.
VOUCHER_STATUSES = (
    "draft",
    "open",
    "overdue",
    "paid",
    "paidoff",
    "voided",
    "transferred",
    "sepadebit",
    "accepted",
    "rejected",
    "unchecked",
)

_STATUS_ALIASES: dict[str, tuple[str, ...]] = {
    "offen": ("open",),
    "unbezahlt": ("open",),
    "unpaid": ("open",),
    "ausstehend": ("open",),
    "pending": ("open",),
    "outstanding": ("open",),
    "bezahlt": ("paid", "paidoff"),
    "beglichen": ("paid", "paidoff"),
    "ausgeglichen": ("paidoff",),
    "balanced": ("paidoff",),
    "überfällig": ("overdue",),
    "ueberfaellig": ("overdue",),
    "fällig": ("overdue",),
    "faellig": ("overdue",),
    "due": ("overdue",),
    "late": ("overdue",),
    "entwurf": ("draft",),
    "entwürfe": ("draft",),
    "entwuerfe": ("draft",),
    "drafts": ("draft",),
    "storniert": ("voided",),
    "storno": ("voided",),
    "cancelled": ("voided",),
    "canceled": ("voided",),
    "void": ("voided",),
    "angenommen": ("accepted",),
    "akzeptiert": ("accepted",),
    "abgelehnt": ("rejected",),
    "declined": ("rejected",),
    "übertragen": ("transferred",),
    "uebertragen": ("transferred",),
    "überwiesen": ("transferred",),
    "lastschrift": ("sepadebit",),
    "sepa": ("sepadebit",),
    "wird abgebucht": ("sepadebit",),
    "ungeprüft": ("unchecked",),
    "zu prüfen": ("unchecked",),
}
_ALL_WORDS = {"alle", "all", "any", "*", "jede", "egal"}
_SEPARATORS = re.compile(r"[,;|/+]|\bund\b|\band\b|\boder\b|\bor\b")


def normalize_voucher_status(status: str | None) -> str | None:
    """Map a status filter to Lexware values ("offen, Bezahlt" → "open,paid,paidoff").

    ``None``/empty/"alle" → ``None`` (the caller's default). Unknown words raise
    a German error listing the valid values instead of Lexware's HTTP 400.
    """
    if status is None or not str(status).strip():
        return None
    out: list[str] = []
    unknown: list[str] = []
    for raw in _SEPARATORS.split(str(status)):
        word = raw.strip().strip("'\"").casefold()
        if not word:
            continue
        if word in _ALL_WORDS:
            return None
        if word in VOUCHER_STATUSES:
            mapped = _STATUS_ALIASES.get(word, (word,))
        else:
            mapped = _STATUS_ALIASES.get(word, ())
        if not mapped:
            unknown.append(raw.strip())
        out.extend(mapped)
    if unknown:
        raise LexwareError(
            f"Unbekannter Status {', '.join(repr(u) for u in unknown)}. Gültig sind "
            "(kommagetrennt kombinierbar): open (offen), overdue (überfällig), "
            "paid (bezahlt), paidoff (ausgeglichen), draft (Entwurf), voided "
            "(storniert), accepted (angenommen), rejected (abgelehnt), "
            "transferred (überwiesen), sepadebit (Lastschrift)."
        )
    return ",".join(dict.fromkeys(out)) or None


# -- voucher type ------------------------------------------------------------

VOUCHER_TYPE_ALIASES: dict[str, str] = {
    "rechnung": "invoice,salesinvoice,downpaymentinvoice",
    "rechnungen": "invoice,salesinvoice,downpaymentinvoice",
    "invoices": "invoice,salesinvoice,downpaymentinvoice",
    "ausgangsrechnung": "salesinvoice,invoice",
    "ausgangsrechnungen": "salesinvoice,invoice",
    "eingangsrechnung": "purchaseinvoice",
    "eingangsrechnungen": "purchaseinvoice",
    "lieferantenrechnung": "purchaseinvoice",
    "purchase invoice": "purchaseinvoice",
    "abschlagsrechnung": "downpaymentinvoice",
    "abschlagsrechnungen": "downpaymentinvoice",
    "anzahlungsrechnung": "downpaymentinvoice",
    "gutschrift": "creditnote,salescreditnote",
    "gutschriften": "creditnote,salescreditnote",
    "credit note": "creditnote,salescreditnote",
    "creditnotes": "creditnote,salescreditnote",
    "rechnungskorrektur": "creditnote",
    "rechnungskorrekturen": "creditnote",
    "verkaufsgutschrift": "salescreditnote",
    "einkaufsgutschrift": "purchasecreditnote",
    "angebot": "quotation",
    "angebote": "quotation",
    "offer": "quotation",
    "quote": "quotation",
    "quotations": "quotation",
    "auftrag": "orderconfirmation",
    "aufträge": "orderconfirmation",
    "auftragsbestätigung": "orderconfirmation",
    "auftragsbestaetigung": "orderconfirmation",
    "auftragsbestätigungen": "orderconfirmation",
    "order": "orderconfirmation",
    "order confirmation": "orderconfirmation",
    "lieferschein": "deliverynote",
    "lieferscheine": "deliverynote",
    "delivery note": "deliverynote",
    "beleg": "any",
    "belege": "any",
    "alle": "any",
    "all": "any",
    "voucher": "any",
    "vouchers": "any",
    "buchhaltungsbeleg": "salesinvoice,salescreditnote,purchaseinvoice,purchasecreditnote",
}


def expand_voucher_type_word(word: str) -> str | None:
    """German/English word for a voucher type → voucherlist value(s), else None."""
    return VOUCHER_TYPE_ALIASES.get(word.strip().casefold())


# -- numbers -----------------------------------------------------------------

_MONEY_JUNK = re.compile(r"[€$\s]|eur|euro|chf|usd", re.I)


def to_decimal(value: Any, *, field: str) -> Decimal | None:
    """Parse 49.9, "49,90 €", "1.234,56", "19%" into a Decimal (None stays None)."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        number = Decimal(str(value))
        if not number.is_finite():
            raise LexwareError(f"„{field}“ muss eine endliche Zahl sein (erhalten: {value!r}).")
        return number
    if not isinstance(value, str):
        raise LexwareError(f"„{field}“ muss eine Zahl sein (erhalten: {type(value).__name__}).")
    text = _MONEY_JUNK.sub("", str(value)).replace("%", "").strip()
    # "100.-" / "100,-" / "2 Stück" / "3 h"
    text = re.sub(r"[.,]-$", "", text)
    text = re.sub(r"(?<=\d)[A-Za-zÄÖÜäöüß]{2,}\.?$", "", text)
    if not text:
        return None
    if "," in text and "." in text:
        # the later one is the decimal separator
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        text = text.replace(",", ".")
    elif re.fullmatch(r"-?[1-9]\d{0,2}(\.\d{3}){2,}", text):
        text = text.replace(".", "")  # 1.234.567 – German thousands
    elif re.fullmatch(r"-?[1-9]\d{0,2}\.\d{3}", text):
        # "1.500" is 1500 in German but 1.5 in English — never guess
        raise LexwareError(
            f"„{field}“: '{value}' ist mehrdeutig (1.500 = tausendfünfhundert oder "
            "eins Komma fünf?). Bitte als Zahl ohne Tausenderpunkt angeben, z. B. "
            "1500 oder 1.5."
        )
    try:
        number = Decimal(text)
    except InvalidOperation as exc:
        raise LexwareError(
            f"„{field}“ muss eine Zahl sein, z. B. 19 oder 49.90 (erhalten: {value!r})."
        ) from exc
    if not number.is_finite():
        raise LexwareError(f"„{field}“ muss eine endliche Zahl sein (erhalten: {value!r}).")
    return number


def to_number(value: Any, *, field: str) -> Any:
    """Like :func:`to_decimal`, but returns int/float for the JSON body."""
    dec = to_decimal(value, field=field)
    if dec is None:
        return value
    return int(dec) if dec == dec.to_integral_value() else float(dec)


ARTICLE_TAX_RATES = (0, 7, 19)


# -- countries ---------------------------------------------------------------

_COUNTRIES: dict[str, str] = {
    "deutschland": "DE",
    "germany": "DE",
    "brd": "DE",
    "deutsch": "DE",
    "österreich": "AT",
    "oesterreich": "AT",
    "austria": "AT",
    "schweiz": "CH",
    "switzerland": "CH",
    "suisse": "CH",
    "niederlande": "NL",
    "holland": "NL",
    "netherlands": "NL",
    "belgien": "BE",
    "belgium": "BE",
    "frankreich": "FR",
    "france": "FR",
    "italien": "IT",
    "italy": "IT",
    "spanien": "ES",
    "spain": "ES",
    "polen": "PL",
    "poland": "PL",
    "dänemark": "DK",
    "daenemark": "DK",
    "denmark": "DK",
    "luxemburg": "LU",
    "luxembourg": "LU",
    "tschechien": "CZ",
    "czechia": "CZ",
    "schweden": "SE",
    "sweden": "SE",
    "norwegen": "NO",
    "norway": "NO",
    "finnland": "FI",
    "finland": "FI",
    "portugal": "PT",
    "irland": "IE",
    "ireland": "IE",
    "ungarn": "HU",
    "hungary": "HU",
    "griechenland": "GR",
    "greece": "GR",
    "großbritannien": "GB",
    "grossbritannien": "GB",
    "england": "GB",
    "vereinigtes königreich": "GB",
    "united kingdom": "GB",
    "uk": "GB",
    "usa": "US",
    "vereinigte staaten": "US",
    "united states": "US",
    "liechtenstein": "LI",
    "slowenien": "SI",
    "slowakei": "SK",
    "kroatien": "HR",
    "rumänien": "RO",
    "bulgarien": "BG",
    "türkei": "TR",
    "tuerkei": "TR",
}


def country_code(value: Any) -> Any:
    """ "Deutschland"/"germany"/"de" → "DE"; leaves other values unchanged."""
    if not isinstance(value, str):
        return value
    text = value.strip()
    if len(text) == 2 and text.isalpha():
        return text.upper()
    return _COUNTRIES.get(text.casefold(), text)


# -- contacts ----------------------------------------------------------------

CONTACT_KEYS = frozenset(
    {
        "id",
        "organizationId",
        "version",
        "roles",
        "company",
        "person",
        "addresses",
        "xRechnung",
        "emailAddresses",
        "phoneNumbers",
        "note",
        "archived",
        "createdDate",
        "updatedDate",
    }
)
_ROLE_WORDS = {
    "customer": "customer",
    "kunde": "customer",
    "kunden": "customer",
    "client": "customer",
    "debitor": "customer",
    "vendor": "vendor",
    "lieferant": "vendor",
    "lieferanten": "vendor",
    "supplier": "vendor",
    "kreditor": "vendor",
}
_LIST_GROUPS = ("emailAddresses", "phoneNumbers")
_SUB_KEYS = {
    "emailAddresses": ("business", "office", "private", "other"),
    "phoneNumbers": ("business", "office", "mobile", "private", "fax", "other"),
}


def _role_name(word: Any) -> str | None:
    return _ROLE_WORDS.get(str(word).strip().casefold())


def normalize_roles(roles: Any, *, keep_existing: bool = False) -> Any:
    """Accept "customer", ["Kunde"], {"Lieferant": {}}, {"customer": true}."""
    if roles is None:
        return None
    if isinstance(roles, str):
        roles = [part for part in re.split(r"[,;/+]|\bund\b", roles) if part.strip()]
    if isinstance(roles, (list, tuple)):
        merged: dict[str, Any] = {}
        for entry in roles:
            if isinstance(entry, dict):
                merged.update(entry)  # [{"customer": {}}] or [{"vendor": true}]
            elif entry is not None:
                merged[str(entry)] = {}
        roles = merged
    if not isinstance(roles, dict):
        return roles
    out: dict[str, Any] = {}
    unknown: list[str] = []
    for key, value in roles.items():
        name = _role_name(key)
        if not name:
            unknown.append(str(key))
            continue
        if value is True or value == "" or value == []:
            value = {}
        if value is False:
            continue
        if isinstance(value, dict) and "number" in value and not keep_existing:
            # Lexware assigns customer/vendor numbers itself.
            value = {k: v for k, v in value.items() if k != "number"}
        out[name] = value
    if unknown:
        raise LexwareError(
            f"Unbekannte Rolle {', '.join(repr(u) for u in unknown)}. Gültig sind "
            "'customer' (Kunde) und 'vendor' (Lieferant), z. B. "
            '{"customer": {}}.'
        )
    return out


def _as_list(value: Any) -> Any:
    if isinstance(value, (str, dict)):
        return [value]
    return value


_GERMAN_FLAT = {
    "strasse": "street", "straße": "street", "str": "street", "adresse": "street",
    "plz": "zip", "postleitzahl": "zip", "postcode": "zip", "postal_code": "zip",
    "ort": "city", "stadt": "city", "town": "city", "land": "country",
    "adresszusatz": "supplement", "zusatz": "supplement",
    "mail": "email", "e-mail": "email", "emailadresse": "email",
    "tel": "phone", "telefonnummer": "phone", "mobil": "mobile",
    "notiz": "note", "notes": "note", "bemerkung": "note",
    "rollen": "roles", "rolle": "roles", "role": "roles",
}
_PERSON_FLAT = {"vorname": "firstName", "first_name": "firstName", "firstname": "firstName",
                "nachname": "lastName", "last_name": "lastName", "lastname": "lastName",
                "anrede": "salutation"}
_COMPANY_NAME = ("firma", "firmenname", "company_name", "companyName", "unternehmen")
_PERSON_SALUTATION = re.compile(r"^(Herr|Frau|Hr\.|Fr\.)\s+", re.I)


_PERSON_KEYS = {
                "vorname": "firstName", "nachname": "lastName", "anrede": "salutation",
                "first_name": "firstName", "last_name": "lastName",
                "email": "emailAddress", "e-mail": "emailAddress", "mail": "emailAddress",
                "e_mail": "emailAddress", "telefon": "phoneNumber", "phone": "phoneNumber",
                "tel": "phoneNumber", "mobil": "phoneNumber", "handy": "phoneNumber"}


_ADDRESS_KEYS = {"strasse": "street", "straße": "street", "plz": "zip",
                 "postleitzahl": "zip", "ort": "city", "stadt": "city", "land": "country",
                 "zusatz": "supplement", "adresszusatz": "supplement"}


def _address_keys(entry: dict[str, Any]) -> dict[str, Any]:
    out = dict(entry)
    for alias, target in _ADDRESS_KEYS.items():
        if alias in out and target not in out:
            out[target] = out.pop(alias)
    return out


def _person_keys(person: dict[str, Any]) -> dict[str, Any]:
    """German/flat keys inside a person or contact person."""
    out = dict(person)
    for alias, target in _PERSON_KEYS.items():
        if alias in out and target not in out:
            out[target] = out.pop(alias)
    return out


def _person_from_text(text: str) -> dict[str, Any]:
    """"Frau Petra Klein" → {salutation: Frau, firstName: Petra, lastName: Klein}."""
    text = text.strip()
    person: dict[str, Any] = {}
    m = _PERSON_SALUTATION.match(text)
    if m:
        word = m.group(1).rstrip(".").lower()
        person["salutation"] = "Herr" if word.startswith("h") else "Frau"
        text = text[m.end():]
    parts = text.split()
    if parts:
        person["lastName"] = parts[-1]
        if len(parts) > 1:
            person["firstName"] = " ".join(parts[:-1])
    return person


def _german_contact_fields(body: dict[str, Any]) -> dict[str, Any]:
    """German/flat field names (strasse, plz, ort, vorname, firma, ansprechpartner,
    kunde: true …) → the structure Lexware expects."""
    if isinstance(body.get("company"), str):
        body["company"] = {"name": body["company"]}
    for key in ("adresse", "anschrift", "address", "rechnungsadresse"):
        if isinstance(body.get(key), dict):  # one address object → billing address
            entry = _address_keys(body.pop(key))
            addresses = dict(body.get("addresses") or {})
            addresses["billing"] = [entry]
            body["addresses"] = addresses
    for alias, target in _GERMAN_FLAT.items():
        if alias in body and target not in body:
            body[target] = body.pop(alias)
    for key in _COMPANY_NAME:
        if key in body:
            body["company"] = {**(body.get("company") or {}), "name": body.pop(key)}
    for keys, target in ((("ustid", "ust_id", "ust-id", "ustidnr", "ust-idnr", "vat_id",
                           "vatId", "umsatzsteuer_id", "vatRegistrationId"),
                          "vatRegistrationId"),
                         (("steuernummer", "tax_number", "taxNumber"), "taxNumber")):
        for key in keys:
            if key in body:
                body["company"] = {**(body.get("company") or {}), target: body.pop(key)}
    person_bits = {_PERSON_FLAT[k]: body.pop(k) for k in list(body) if k in _PERSON_FLAT}
    contact_person = None
    for key in ("ansprechpartner", "contactPerson", "contact_person", "contactPersons",
                "ansprechpartnerin", "kontaktperson"):
        if key in body:
            contact_person = body.pop(key)
    if isinstance(contact_person, str):
        contact_person = _person_from_text(contact_person)
    if isinstance(contact_person, dict):
        contact_person = [contact_person]
    if isinstance(body.get("company"), dict) or contact_person:
        company = dict(body.get("company") or {})
        persons = contact_person or company.get("contactPersons")
        if person_bits:
            persons = [{**((persons or [{}])[0]), **person_bits}]
        if persons:
            company["contactPersons"] = persons
        body["company"] = company
    elif person_bits:
        body["person"] = {**(body.get("person") or {}), **person_bits}
    roles = body.get("roles")
    for flag, role in (("customer", "customer"), ("kunde", "customer"),
                       ("vendor", "vendor"), ("lieferant", "vendor"),
                       ("isCustomer", "customer"), ("isVendor", "vendor")):
        if flag in body and isinstance(body[flag], bool | str | dict):
            value = body.pop(flag)
            if value is False or str(value).lower() in ("false", "nein", "no"):
                continue
            roles = {**(roles if isinstance(roles, dict) else {}), role: {}}
    if roles is not None:
        body["roles"] = roles
    return body


def normalize_contact(body: dict[str, Any], *, partial: bool = False) -> dict[str, Any]:
    """Repair a contact body before it goes to Lexware.

    Aliases: flat ``email``/``phone``/``mobile``/``name`` → their Lexware place,
    a plain ``street``/``zip``/``city``/``country`` → billing address; strings
    and single objects become the lists Lexware expects; country names →
    ISO codes; roles in any common form. `partial` (update) skips defaults.
    Unknown top-level fields raise instead of being dropped silently.
    """
    if not isinstance(body, dict):
        return body
    body = _german_contact_fields(strip_control(dict(body)))
    for key in ("id", "version", "organizationId", "createdDate", "updatedDate"):
        if partial:
            body.pop(key, None)  # echoed from a read — the server sets these
    if isinstance(body.get("company"), str):
        body["company"] = {"name": body["company"]}
    if isinstance(body.get("person"), dict):
        body["person"] = _person_keys(body["person"])
    company = body.get("company")
    if isinstance(company, dict):
        for flag in ("allowTaxFreeInvoices",):
            if isinstance(company.get(flag), str):
                company[flag] = company[flag].strip().casefold() in ("ja", "yes", "true", "1")
        persons = company.get("contactPersons")
        if isinstance(persons, list):
            company["contactPersons"] = [
                _person_keys(p) if isinstance(p, dict) else p for p in persons
            ]
    # flat contact data
    for flat, group, sub in (
        ("email", "emailAddresses", "business"),
        ("e_mail", "emailAddresses", "business"),
        ("emailAddress", "emailAddresses", "business"),
        ("phone", "phoneNumbers", "business"),
        ("telefon", "phoneNumbers", "business"),
        ("phoneNumber", "phoneNumbers", "business"),
        ("mobile", "phoneNumbers", "mobile"),
        ("handy", "phoneNumbers", "mobile"),
        ("fax", "phoneNumbers", "fax"),
    ):
        if flat in body:
            value = body.pop(flat)
            if value not in (None, ""):
                block = dict(body.get(group) or {})
                block[sub] = list(_as_list(block.get(sub)) or []) + list(_as_list(value))
                body[group] = block
    for group in _LIST_GROUPS:
        block = body.get(group)
        if isinstance(block, (str, list)):
            block = {"business": block}
        if isinstance(block, dict):
            body[group] = {k: _as_list(v) for k, v in block.items()}
    # flat address
    address_keys = {"street", "zip", "city", "country", "countryCode", "supplement"}
    flat_address = {k: body.pop(k) for k in list(body) if k in address_keys}
    if flat_address:
        if "country" in flat_address:
            flat_address["countryCode"] = flat_address.pop("country")
        addresses = dict(body.get("addresses") or {})
        addresses["billing"] = [{**(_as_list(addresses.get("billing")) or [{}])[0], **flat_address}]
        body["addresses"] = addresses
    addresses = body.get("addresses")
    if isinstance(addresses, list):
        addresses = {"billing": addresses}
    if isinstance(addresses, dict):
        fixed: dict[str, Any] = {}
        for kind, entries in addresses.items():
            entries = _as_list(entries)
            if isinstance(entries, list):
                entries = [_address_keys(e) if isinstance(e, dict) else e for e in entries]
                entries = [
                    {**e, "countryCode": country_code(e.get("countryCode") or e.get("country"))}
                    if isinstance(e, dict) and (e.get("countryCode") or e.get("country"))
                    else e
                    for e in entries
                ]
                entries = [
                    {k: v for k, v in e.items() if k != "country"} if isinstance(e, dict) else e
                    for e in entries
                ]
            fixed[kind] = entries
        body["addresses"] = fixed
    # name alias
    if "name" in body:
        name = body.pop("name")
        if isinstance(body.get("person"), dict):
            body["person"] = {**body["person"], "lastName": body["person"].get("lastName") or name}
        else:
            body["company"] = {**(body.get("company") or {}), "name": name}
    if "roles" in body:
        body["roles"] = normalize_roles(body["roles"])
    elif not partial:
        body["roles"] = {"customer": {}}
    company = body.get("company")
    if isinstance(company, dict):
        if company.get("vatRegistrationId"):
            company["vatRegistrationId"] = re.sub(
                r"\s+", "", str(company["vatRegistrationId"])
            ).upper()
        persons = company.get("contactPersons")
        if isinstance(persons, dict):
            company["contactPersons"] = persons = [persons]
        if isinstance(persons, list) and len(persons) == 1 and isinstance(persons[0], dict):
            person = dict(persons[0])
            if not person.get("salutation"):
                person.pop("salutation", None)
            person.setdefault("primary", True)
            company["contactPersons"] = [person]
        if isinstance(persons, list) and len(persons) > 1:
            raise LexwareError(
                "Die Lexware-API erlaubt pro Firmen-Kontakt nur einen Ansprechpartner. "
                "Bitte nur einen angeben; weitere Ansprechpartner lassen sich in der "
                "Lexware-Weboberfläche ergänzen oder als eigener Kontakt anlegen."
            )
    if (
        not partial
        and isinstance(body.get("company"), dict)
        and isinstance(body.get("person"), dict)
    ):
        raise LexwareError(
            "Ein Kontakt ist entweder eine Firma (`company`) oder eine Privatperson "
            "(`person`), nicht beides. Für eine Firma mit Ansprechpartner: "
            '{"company": {"name": …, "contactPersons": [{"firstName": …, "lastName": …}]}}.'
        )
    unknown = sorted(k for k in body if k not in CONTACT_KEYS)
    if unknown:
        raise LexwareError(
            f"Unbekannte Felder für einen Kontakt: {', '.join(unknown)}. Erlaubt sind: "
            "roles, company, person, addresses, emailAddresses, phoneNumbers, note "
            '(E-Mail z. B. als {"emailAddresses": {"business": ["a@b.de"]}}).'
        )
    return body


# -- articles ----------------------------------------------------------------

ARTICLE_KEYS = frozenset(
    {
        "id",
        "organizationId",
        "version",
        "archived",
        "title",
        "description",
        "type",
        "articleNumber",
        "gtin",
        "note",
        "unitName",
        "price",
        "createdDate",
        "updatedDate",
    }
)
_ARTICLE_ALIASES = {
    "name": "title",
    "bezeichnung": "title",
    "titel": "title",
    "unit": "unitName",
    "einheit": "unitName",
    "unit_name": "unitName",
    "number": "articleNumber",
    "article_number": "articleNumber",
    "artikelnummer": "articleNumber",
    "beschreibung": "description",
    "articleType": "type",
    "article_type": "type",
    "typ": "type",
    "artikelart": "type",
    "gtin_ean": "gtin",
    "ean": "gtin",
    "art": "type",
}
_PRICE_KEYS = {"netPrice", "grossPrice", "leadingPrice", "taxRate"}
_PRICE_ALIASES = {
    "net_price": "netPrice",
    "nettopreis": "netPrice",
    "netto": "netPrice",
    "gross_price": "grossPrice",
    "bruttopreis": "grossPrice",
    "brutto": "grossPrice",
    "tax_rate": "taxRate",
    "steuersatz": "taxRate",
    "ust": "taxRate",
    "vat": "taxRate",
    "leading_price": "leadingPrice",
}


def normalize_article(body: dict[str, Any], *, article_type_map: dict[str, str]) -> dict[str, Any]:
    """Repair an article body: aliases, PRODUCT/SERVICE, numbers, NET/GROSS.

    `article_type_map` is the same word → PRODUCT/SERVICE table the article
    list filter uses. A price with only a net (gross) amount gets
    leadingPrice NET (GROSS); tax rates outside 0/7/19 raise.
    """
    if not isinstance(body, dict):
        return body
    body = strip_control(body)
    out: dict[str, Any] = {}
    raw_price = body.get("price", body.get("preis"))
    if isinstance(raw_price, dict):
        price = dict(raw_price)
    elif raw_price is not None and not isinstance(raw_price, bool):
        # "price": 19.9 → net price (19 % unless a taxRate is given)
        price = {"netPrice": raw_price}
    else:
        price = {}
    for key, value in body.items():
        if key in ("price", "preis"):
            continue
        target = _ARTICLE_ALIASES.get(key, key)
        if key in _PRICE_KEYS or key in _PRICE_ALIASES:
            price[_PRICE_ALIASES.get(key, key)] = value
            continue
        out[target] = value
    for key in list(price):
        if key in _PRICE_ALIASES:
            price[_PRICE_ALIASES[key]] = price.pop(key)
    if "type" in out and out["type"] is not None:
        word = str(out["type"]).strip().casefold()
        if word not in article_type_map:
            raise LexwareError(
                f"Ungültige Artikelart '{out['type']}'. Gültig sind 'PRODUCT' "
                "(Produkt/Ware) und 'SERVICE' (Dienstleistung)."
            )
        out["type"] = article_type_map[word]
    if price:
        for key in ("netPrice", "grossPrice"):
            if key in price:
                price[key] = to_number(price[key], field=f"price.{key}")
        if "taxRate" in price:
            rate = to_number(price["taxRate"], field="price.taxRate")
            if isinstance(rate, (int, float)) and rate not in ARTICLE_TAX_RATES:
                raise LexwareError(
                    f"Steuersatz {rate} % ist für Artikel nicht erlaubt. Möglich sind "
                    "0, 7 oder 19 %."
                )
            price["taxRate"] = rate
        if price.get("leadingPrice"):
            lead = str(price["leadingPrice"]).strip().upper()
            lead = {"NETTO": "NET", "BRUTTO": "GROSS"}.get(lead, lead)
            if lead not in ("NET", "GROSS"):
                raise LexwareError(
                    "leadingPrice muss 'NET' (Netto führt) oder 'GROSS' (Brutto führt) sein."
                )
            price["leadingPrice"] = lead
        out["price"] = price
    unknown = sorted(k for k in out if k not in ARTICLE_KEYS)
    if unknown:
        raise LexwareError(
            f"Unbekannte Felder für einen Artikel: {', '.join(unknown)}. Erlaubt sind: "
            "title, description, type, articleNumber, gtin, note, unitName, price "
            "(price: netPrice/grossPrice, leadingPrice NET|GROSS, taxRate 0/7/19)."
        )
    return out


def lead_from_given(price: dict[str, Any], given: dict[str, Any]) -> dict[str, Any]:
    """Pick leadingPrice from which amount the caller actually sent.

    Only a net amount → NET (gross recomputed by Lexware), only a gross amount
    → GROSS. Without that Lexware silently ignores the non-leading amount.
    """
    if given.get("leadingPrice"):
        return price
    has_net, has_gross = "netPrice" in given, "grossPrice" in given
    if has_net and not has_gross:
        return {**{k: v for k, v in price.items() if k != "grossPrice"}, "leadingPrice": "NET"}
    if has_gross and not has_net:
        return {**{k: v for k, v in price.items() if k != "netPrice"}, "leadingPrice": "GROSS"}
    return price


def fold(text: str) -> str:
    """Compare-key: casefold, ä→ae etc., accents and punctuation removed."""
    text = text.casefold()
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        text = text.replace(a, b)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", text).strip()
