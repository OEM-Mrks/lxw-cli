"""Complete and repair request bodies before they reach Lexware.

AI clients regularly send document bodies that are *almost* right: a plain
date instead of Lexware's timestamp format, no ``totalPrice``, no tax
conditions, an article line item without the article id, a voucher whose tax
amounts are rounded differently. Lexware rejects each of these with an opaque
HTTP 400/406, and the model needs several attempts per document.

The helpers here fill in only what is unambiguous (format, currency, the
standard defaults the web app uses as well) and never change amounts the
caller chose. Where a value cannot be derived safely they raise a clear German
:class:`LexwareError` instead of guessing.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

from lxw_cli.core.errors import LexwareAPIError, LexwareError
from lxw_cli.core.normalize import (
    bool_word,
    country_code,
    fold,
    is_change,
    one_line,
    rate_word,
    split_gross_net,
    strip_control,
    to_number,
    unwrap,
)

if TYPE_CHECKING:
    from lxw_cli.core.client import LexwareClient

try:  # The slim container image may lack tz data; fall back to local time.
    from zoneinfo import ZoneInfo

    _TZ: Any = ZoneInfo("Europe/Berlin")
except Exception:  # pragma: no cover - depends on the runtime image
    _TZ = None

# Document kinds (sales documents with lineItems) and what they need.
QUOTATION = "quotation"
INVOICE = "invoice"
ORDER_CONFIRMATION = "orderconfirmation"
DELIVERY_NOTE = "deliverynote"

# Lexware requires shippingConditions on these; quotations have none.
_NEEDS_SHIPPING = {INVOICE, ORDER_CONFIRMATION, DELIVERY_NOTE}
# Days until a quotation expires when the caller gives no date (web app default).
QUOTATION_VALID_DAYS = 30
# Tax types whose line items are taxed at the normal German rates.
_TAXED_TYPES = {"net", "gross"}
# Tax types without German VAT: every position must carry 0 %.
TAX_FREE_TYPES = frozenset({
    "vatfree", "intraCommunitySupply", "constructionService13b", "externalService13b",
    "thirdPartyCountryService", "thirdPartyCountryDelivery", "photovoltaicEquipment",
})
DEFAULT_TAX_RATE = 19
# A Belegdatum this far ahead is almost always a typo in the year.
MAX_FUTURE_DAYS = 366

_GERMAN_DATE_RE = re.compile(r"^\s*(\d{1,2})\.(\d{1,2})\.(\d{4})\s*$")
_ISO_DATE_RE = re.compile(r"^\s*(\d{4})-(\d{2})-(\d{2})\s*$")

# Lexware article type -> document line item type.
_ARTICLE_LINE_TYPES = {"PRODUCT": "material", "SERVICE": "service"}


# -- Dates ------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(_TZ) if _TZ is not None else datetime.now().astimezone()


def _aware(value: datetime) -> datetime:
    if value.tzinfo is not None:
        return value
    return value.replace(tzinfo=_TZ) if _TZ is not None else value.astimezone()


def _format(value: datetime) -> str:
    return _aware(value).isoformat(timespec="milliseconds")


_RELATIVE_DAYS = {
    "heute": 0, "today": 0, "jetzt": 0, "now": 0,
    "gestern": -1, "yesterday": -1, "vorgestern": -2,
    "morgen": 1, "tomorrow": 1, "übermorgen": 2, "uebermorgen": 2,
}


_MONTHS = {
    "jan": 1, "januar": 1, "january": 1, "jänner": 1, "feb": 2, "februar": 2,
    "february": 2, "mär": 3, "mrz": 3, "märz": 3, "maerz": 3, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "mai": 5, "may": 5, "jun": 6, "juni": 6, "june": 6,
    "jul": 7, "juli": 7, "july": 7, "aug": 8, "august": 8, "sep": 9, "sept": 9,
    "september": 9, "okt": 10, "oktober": 10, "oct": 10, "october": 10, "nov": 11,
    "november": 11, "dez": 12, "dezember": 12, "dec": 12, "december": 12,
}


def _named_month(value: str) -> tuple[int, int, int] | None:
    """"10. November 2026", "10 Nov 2026", "Nov 10, 2026" → (y, m, d)."""
    text = value.strip().casefold().replace(",", " ").replace(".", " ")
    parts = text.split()
    if len(parts) != 3:
        return None
    for d_i, m_i in ((0, 1), (1, 0)):
        month = _MONTHS.get(parts[m_i])
        if month and parts[d_i].isdigit() and parts[2].isdigit() and len(parts[2]) == 4:
            return int(parts[2]), month, int(parts[d_i])
    return None


_WEEKDAYS = {"montag": 0, "dienstag": 1, "mittwoch": 2, "donnerstag": 3, "freitag": 4,
             "samstag": 5, "sonntag": 6, "monday": 0, "tuesday": 1, "wednesday": 2,
             "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6}


def month_end(day: date) -> date:
    first_next = (day.replace(day=28) + timedelta(days=4)).replace(day=1)
    return first_next - timedelta(days=1)


def _relative_day(word: str) -> date | None:
    """"in 14 Tagen", "Monatsende", "nächsten Montag" → the day."""
    today = _now().date()
    m = re.fullmatch(r"(?:in|nach)\s+(\d+)\s+(tag\w*|woche\w*|days?|weeks?)", word)
    if m:
        n = int(m.group(1)) * (7 if m.group(2).startswith("w") else 1)
        return today + timedelta(days=n)
    if re.fullmatch(r"(?:zum\s+|am\s+)?(?:ende\s+(?:des\s+)?monats?|monatsende|"
                    r"end\s+of\s+(?:the\s+)?month|ultimo)", word):
        return month_end(today)
    m = re.fullmatch(r"(?:am\s+)?(?:nächsten|naechsten|kommenden|nächster|kommender|next)\s+"
                     r"(\w+)", word)
    if m and m.group(1) in _WEEKDAYS:
        ahead = (_WEEKDAYS[m.group(1)] - today.weekday()) % 7 or 7
        return today + timedelta(days=ahead)
    return None


def _parse_day(value: str) -> date | None:
    word = re.sub(r"\s+", " ", value.strip().casefold())
    if word in _RELATIVE_DAYS:
        return _now().date() + timedelta(days=_RELATIVE_DAYS[word])
    relative = _relative_day(word)
    if relative is not None:
        return relative
    m = re.match(r"^\s*(\d{1,2})\.(\d{1,2})\.(\d{2})\s*$", value)
    if m:  # 10.11.26
        d, mo, y = (int(g) for g in m.groups())
        try:
            return date(2000 + y, mo, d)
        except ValueError:
            return None
    m = _ISO_DATE_RE.match(value)
    if m:
        y, mo, d = (int(g) for g in m.groups())
    else:
        m = _GERMAN_DATE_RE.match(value)
        if m:
            d, mo, y = (int(g) for g in m.groups())
        else:
            named = _named_month(value)
            if not named:
                return None
            y, mo, d = named
    try:
        return date(y, mo, d)
    except ValueError as exc:
        raise LexwareError(
            f"Das Datum '{value.strip()}' gibt es nicht (Tag oder Monat außerhalb des "
            "Kalenders)."
        ) from exc


def document_timestamp(value: Any) -> Any:
    """Bring a date into Lexware's ``yyyy-MM-ddTHH:mm:ss.SSSXXX`` format.

    Accepts ``2026-11-04``, ``04.11.2026`` and ISO timestamps with or without
    milliseconds/offset. A plain day becomes midnight in Berlin time. Anything
    unparseable is returned unchanged so Lexware can report it.
    """
    if not isinstance(value, str) or not value.strip():
        return value
    day = _parse_day(value)
    if day is not None:
        return _format(datetime.combine(day, time()))
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return value
    # Day and time as written count. An explicit offset like +01:00 is kept;
    # "Z"/+00:00 (what models append by habit) is read as Berlin wall-clock
    # time, so "2026-11-10T23:30Z" stays the 10th instead of 00:30 on the 11th.
    offset = parsed.utcoffset()
    wall = parsed.replace(tzinfo=None)
    if offset is None or offset.total_seconds() == 0 or _TZ is None:
        return _format(wall) if offset is None or offset.total_seconds() == 0 else (
            parsed.isoformat(timespec="milliseconds"))
    berlin = _aware(wall).utcoffset()
    if berlin == offset:
        return parsed.isoformat(timespec="milliseconds")
    # another zone (e.g. +05:00) would move the day in Berlin — keep the day as written
    return _format(wall)


def checked_timestamp(value: Any, label: str) -> Any:
    """:func:`document_timestamp`, but text that is no date raises in German."""
    stamp = document_timestamp(value)
    if isinstance(value, str) and value.strip() and not re.match(
            r"^\d{4}-\d{2}-\d{2}", str(stamp)):
        week = re.search(r"\b(?:kw|kalenderwoche|week)\s*\d", value, re.I)
        raise LexwareError(
            f"{label} '{value.strip()}' nicht lesbar – bitte ein Datum wie 24.11.2026, "
            "2026-11-24, „heute“ oder „in 14 Tagen“ angeben"
            + (" (eine Kalenderwoche bitte als Datum, z. B. den Montag der KW)." if week
               else ".")
        )
    return stamp


def check_not_far_future(stamp: Any, extra: str = "") -> None:
    """A Belegdatum more than a year ahead is refused (typo in the year?)."""
    day = _day_of(stamp)
    if day and (day - _now().date()).days > MAX_FUTURE_DAYS:
        raise LexwareError(
            f"Das Belegdatum {day:%d.%m.%Y} liegt mehr als ein Jahr in der Zukunft – bitte "
            f"prüfen (Tippfehler im Jahr?).{extra}"
        )


# -- Sales documents (quotation, invoice, order confirmation, delivery note) --


def _article_line_type(client: LexwareClient, article_id: str, cache: dict) -> str:
    return _ARTICLE_LINE_TYPES.get(
        str(_article(client, article_id, cache).get("type") or "").upper(), "material"
    )


def _article(client: LexwareClient, article_id: str, cache: dict) -> dict[str, Any]:
    if article_id not in cache:
        try:
            article = client.get(f"/v1/articles/{article_id}")
        except LexwareAPIError as exc:
            if exc.status_code in (400, 404):
                raise LexwareError(
                    f"Artikel mit der ID '{article_id}' gibt es nicht. Den Artikel "
                    "bitte über die Artikelliste suchen und dessen id verwenden — "
                    "oder die Position ohne id als freie Position (type „custom“) "
                    "mit Bezeichnung und Preis anlegen."
                ) from exc
            raise
        cache[article_id] = article if isinstance(article, dict) else {}
    return cache[article_id]


# Top-level aliases models use for document fields.
_DOC_ALIASES = {
    "positions": "lineItems", "positionen": "lineItems", "items": "lineItems",
    "line_items": "lineItems", "lines": "lineItems",
    "date": "voucherDate", "datum": "voucherDate", "invoiceDate": "voucherDate",
    "documentDate": "voucherDate", "belegdatum": "voucherDate",
    "validUntil": "expirationDate", "gueltigBis": "expirationDate",
    "gültigBis": "expirationDate", "expiryDate": "expirationDate",
    "valid_until": "expirationDate",
    "subject": "title", "betreff": "title", "intro": "introduction",
    "einleitung": "introduction", "closing": "remark", "schlusstext": "remark",
    "footer": "remark", "titel": "title", "einleitungstext": "introduction",
    "nachbemerkung": "remark", "bemerkung": "remark", "closingText": "remark",
    "gueltig_bis": "expirationDate", "gültig_bis": "expirationDate",
    "gueltigbis": "expirationDate", "gültigbis": "expirationDate",
    "ablaufdatum": "expirationDate", "expiration_date": "expirationDate",
    "rechnungsdatum": "voucherDate", "angebotsdatum": "voucherDate",
    "invoice_date": "voucherDate", "voucher_date": "voucherDate",
    "document_date": "voucherDate", "quotationDate": "voucherDate",
    "zahlbar_bis": "dueDate", "zahlbarBis": "dueDate", "due_date": "dueDate",
    "faellig_am": "dueDate", "fällig_am": "dueDate", "payableUntil": "dueDate",
    "zahlungsbedingungen": "paymentConditions", "payment_conditions": "paymentConditions",
    "lieferbedingungen": "deliveryTerms", "delivery_terms": "deliveryTerms",
    "lieferbedingung": "deliveryTerms", "sprache": "language",
}
# Fields Lexware's document endpoints know (create + what reads return).
_DOC_FIELDS = frozenset({
    "id", "organizationId", "createdDate", "updatedDate", "version", "language",
    "archived", "voucherStatus", "voucherNumber", "voucherDate", "expirationDate",
    "address", "lineItems", "totalPrice", "taxAmounts", "taxConditions",
    "paymentConditions", "shippingConditions", "relatedVouchers", "printLayoutId",
    "introduction", "remark", "title", "files", "electronicDocumentProfile",
    "closingInvoice", "deliveryTerms", "xRechnung", "deliveryDate", "dueDate",
    "nextReminderDate", "eInvoiceProfile",
})
# What a GET returns but a create does not take — dropped silently, so a
# document read with get_* can be sent back as a copy ("wie RE0016").
_READ_ONLY_FIELDS = frozenset({
    "id", "organizationId", "createdDate", "updatedDate", "version", "voucherStatus",
    "voucherNumber", "taxAmounts", "relatedVouchers", "files", "archived",
    "claimedGrossAmount", "downPaymentDeductions", "recurringTemplateId",
    "closingInvoice", "hinweis", "Hinweis", "nextReminderDate",
})
_LINE_FIELDS = frozenset({
    "id", "type", "name", "description", "quantity", "unitName", "unitPrice",
    "discountPercentage", "lineItemAmount", "optional", "alternative", "subItems",
})
_DISCOUNT_KEYS = ("rabatt_prozent", "rabattProzent", "gesamtrabatt", "rabatt",
                  "discount", "discountPercentage", "totalDiscountPercentage")
_CUSTOMER_KEYS = ("customer", "kunde", "kunde_name", "kundenname", "customerName",
                  "customer_name", "empfaenger", "empfänger", "recipient")
_ZIP_RE = re.compile(r"\b(\d{4,5})\s+([^,]+)$")
_CONTACT_KEYS = ("contactId", "customerId", "contact_id", "customer_id", "kundeId")
_LINE_ALIASES = {
    "title": "name", "bezeichnung": "name", "text": "name", "artikel_name": "name",
    "positionsname": "name", "leistung": "name",
    "rabatt_prozent": "discountPercentage", "rabattProzent": "discountPercentage",
    "unit": "unitName", "einheit": "unitName", "unit_name": "unitName",
    "menge": "quantity", "qty": "quantity", "anzahl": "quantity",
    "beschreibung": "description", "articleId": "id", "article_id": "id",
    "rabatt": "discountPercentage", "discount": "discountPercentage",
}
_LINE_PRICE_KEYS = {
    "price": "amount", "preis": "amount", "unit_price": "amount", "amount": "amount",
    "netPrice": "netAmount", "netAmount": "netAmount", "netto": "netAmount",
    "grossPrice": "grossAmount", "grossAmount": "grossAmount", "brutto": "grossAmount",
    "vat": "taxRatePercentage", "tax": "taxRatePercentage", "taxRate": "taxRatePercentage",
    "ust": "taxRatePercentage", "mwst": "taxRatePercentage",
    "einzelpreis": "amount", "stueckpreis": "amount", "stückpreis": "amount",
    "nettopreis": "netAmount", "netto_preis": "netAmount", "bruttopreis": "grossAmount",
    "brutto_preis": "grossAmount", "preis_netto": "netAmount",
    "preis_brutto": "grossAmount", "steuer": "taxRatePercentage",
    "taxRatePercentage": "taxRatePercentage", "steuersatz": "taxRatePercentage",
}
_LINE_TYPES = {
    "product": "material", "produkt": "material", "ware": "material",
    "artikel": "material", "article": "material", "material": "material",
    "service": "service", "dienstleistung": "service", "leistung": "service",
    "custom": "custom", "frei": "custom", "freitext": "custom", "free": "custom",
    "position": "custom", "text": "text", "textzeile": "text", "hinweis": "text",
}
_TAX_TYPES = {
    "netto": "net", "net": "net", "brutto": "gross", "gross": "gross",
    "steuerfrei": "vatfree", "vatfree": "vatfree", "kleinunternehmer": "vatfree",
    "kleinunternehmerregelung": "vatfree", "§19": "vatfree", "§ 19": "vatfree",
    "intracommunitysupply": "intraCommunitySupply",
    "innergemeinschaftlich": "intraCommunitySupply",
    "innergemeinschaftliche lieferung": "intraCommunitySupply",
    "ig lieferung": "intraCommunitySupply", "ig-lieferung": "intraCommunitySupply",
    "eu-lieferung": "intraCommunitySupply", "eu lieferung": "intraCommunitySupply",
    "constructionservice13b": "constructionService13b",
    "bauleistung": "constructionService13b", "bauleistung 13b": "constructionService13b",
    "13b bau": "constructionService13b", "§13b bau": "constructionService13b",
    "externalservice13b": "externalService13b", "13b ausland": "externalService13b",
    "§13b ausland": "externalService13b",
    "ausländischer leistender": "externalService13b",
    "auslaendischer leistender": "externalService13b",
    "thirdpartycountryservice": "thirdPartyCountryService",
    "drittland leistung": "thirdPartyCountryService",
    "drittland dienstleistung": "thirdPartyCountryService",
    "thirdpartycountrydelivery": "thirdPartyCountryDelivery",
    "drittland lieferung": "thirdPartyCountryDelivery",
    "ausfuhr": "thirdPartyCountryDelivery", "ausfuhrlieferung": "thirdPartyCountryDelivery",
    "export": "thirdPartyCountryDelivery",
    "photovoltaicequipment": "photovoltaicEquipment",
    "photovoltaik": "photovoltaicEquipment", "pv-anlage": "photovoltaicEquipment",
}
# Words that fit several tax types: ask instead of guessing.
_AMBIGUOUS_TAX = {
    "drittland": "thirdPartyCountryDelivery (Lieferung in ein Drittland) oder "
                 "thirdPartyCountryService (Dienstleistung für ein Drittland)",
    "13b": "constructionService13b (Bauleistung, § 13b) oder externalService13b "
           "(Leistung eines ausländischen Unternehmers, § 13b)",
    "reverse charge": "constructionService13b (Bauleistung, § 13b), externalService13b "
                      "(ausländischer Leistender, § 13b) oder intraCommunitySupply "
                      "(innergemeinschaftliche Lieferung)",
}
for _word in ("§13b", "§ 13b", "paragraph 13b", "steuerschuldnerschaft"):
    _AMBIGUOUS_TAX[_word] = _AMBIGUOUS_TAX["13b"]
_AMBIGUOUS_TAX["reverse-charge"] = _AMBIGUOUS_TAX["reverse charge"]


def tax_type_word(value: Any) -> Any:
    """"innergemeinschaftlich" → intraCommunitySupply …; ambiguous words raise."""
    if not isinstance(value, str):
        return value
    word = re.sub(r"\s+", " ", value.strip().casefold())
    if word in _AMBIGUOUS_TAX:
        raise LexwareError(
            f"Steuerart '{value}' ist mehrdeutig – bitte genauer angeben: "
            f"{_AMBIGUOUS_TAX[word]}."
        )
    if word in _TAX_TYPES:
        return _TAX_TYPES[word]
    # "Nettopreise", "netto (19%)", "inkl. MwSt", "Brutto inkl. USt": the price side
    if re.search(r"brutto|gross|inkl|incl", word):
        return "gross"
    if re.search(r"netto|\bnet\b|zzgl|exkl|excl|plus\s+(?:mwst|ust)", word):
        return "net"
    raise LexwareError(
        f"Unbekannte Steuerart '{value}'. Möglich: net (netto), gross (brutto), vatfree "
        "(steuerfrei/Kleinunternehmer), intraCommunitySupply (innergemeinschaftliche "
        "Lieferung), constructionService13b (§13b Bauleistung), externalService13b (§13b "
        "ausländischer Leistender), thirdPartyCountryDelivery (Ausfuhr), "
        "thirdPartyCountryService (Drittland-Leistung), photovoltaicEquipment (Photovoltaik)."
    )


_SHIPPING_TYPES = {
    "lieferung": "delivery", "lieferdatum": "delivery", "delivery": "delivery",
    "leistung": "service", "leistungsdatum": "service", "service": "service",
    "leistungszeitraum": "serviceperiod", "serviceperiod": "serviceperiod",
    "lieferzeitraum": "deliveryperiod", "deliveryperiod": "deliveryperiod",
    "keine": "none", "ohne": "none", "none": "none",
}
_DAYS_RE = re.compile(r"(\d+)\s*(?:tag\w*|days?|d)\b", re.I)


_DAY_WORDS = {"sofort": 0, "null": 0, "sieben": 7, "zehn": 10, "vierzehn": 14,
              "einundzwanzig": 21, "achtundzwanzig": 28, "dreißig": 30, "dreissig": 30,
              "fünfundvierzig": 45, "fuenfundvierzig": 45, "sechzig": 60, "neunzig": 90,
              "seven": 7, "ten": 10, "fourteen": 14, "thirty": 30, "sixty": 60,
              "ninety": 90, "immediately": 0}


def _days(value: Any) -> Any:
    """"14 Tage" / "netto 30 days" / "dreißig Tage" / 14 → 14.

    Anything else raises – an unreadable payment term must not vanish silently.
    """
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        if float(value) != int(value) or value < 0:
            raise LexwareError(f"Zahlungsziel '{value}' – bitte ganze Tage angeben (z. B. 14).")
        return int(value)
    text = str(value).strip()
    m = _DAYS_RE.search(text) or re.fullmatch(r"\s*(\d+)\s*", text)
    if m:
        return int(m.group(1))
    if not re.search(r"\d", text):
        for word, days in _DAY_WORDS.items():
            if re.search(rf"\b{word}\b", text, re.I):
                return days
    raise LexwareError(f"Zahlungsziel '{value}' nicht lesbar – bitte Tage als Zahl angeben "
                       "(z. B. 14).")


_TERM_DAYS_RE = re.compile(
    r"(\d+)\s*(?:kalender|werk)?(?:tag\w*|days?)\b|(\d+)\s?d\b(?![-\w])"
    r"|(\d+)\s*(?:woche\w*|weeks?)\b"
    r"|\b(?:netto|net)\s+(\d+)\b(?!\s*%)", re.I)
_TERM_PCT_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:%|prozent|percent)", re.I)
_TERM_UNTIL_RE = re.compile(
    r"\b(?:bis|until|by|spätestens|spaetestens)\s+(?:zum\s+|am\s+)?"
    r"(\d{1,2}\.\d{1,2}\.\d{2,4}|\d{4}-\d{2}-\d{2})", re.I)
_DATE_IN_TEXT_RE = re.compile(r"\d{1,2}\.\d{1,2}\.\d{2,4}|\d{4}-\d{2}-\d{2}")
_NO_SKONTO_RE = re.compile(r"\b(?:ohne|kein\w*|no|without)\s+(?:skonto|discount)\b", re.I)
# Skonto only where the word says so — "Verzugszinsen 9 %", "50 % bei Auftrag" are none.
_SKONTO_WORD_RE = re.compile(r"\b(?:skonto|discount)\b|(?<!ohne )\babzug\b", re.I)
_TERM_ZERO_RE = re.compile(
    r"\b(?:sofort|vorkasse|vorauskasse|immediately|(?:nach|bei)\s+(?:rechnungs)?erhalt|"
    r"nach\s+erhalt|upon\s+receipt|on\s+receipt|bar\s+bei|barzahlung|nachnahme)\b", re.I)
_NET_MARK_RE = re.compile(r"\b(?:netto|net|ohne\s+abzug|rein\s+netto|without\s+deduction)\b",
                          re.I)
# A clause about paying: only there a day count in the caller's own text is a
# term ("Lieferung erfolgt 5 Tage nach Zahlungseingang" is none).
_PAY_CONTEXT_RE = re.compile(
    r"\b(?:zahlbar|zahlungsziel|zahlungsfrist|zahlung\s+(?:innerhalb|binnen|bis)|"
    r"überweis\w*|ueberweis\w*|begleich\w*|bezahl\w*|zu\s+zahlen|fällig\w*|faellig\w*|"
    r"netto|net|ohne\s+abzug|skonto|payable|payment|pay|due|discount)\b", re.I)
# The bare "sofort" texts that may be rebuilt from a given term.
_BARE_ZERO_RE = re.compile(r"(?:vorkasse|sofort|zahlbar sofort(?:,? rein netto)?)\.?", re.I)


@dataclass
class _Terms:
    days: int | None = None
    discount: dict[str, Any] | None = None
    no_skonto: bool = False
    zero: bool = False  # only a "sofort"/"Vorkasse" text, no day count
    skonto_unread: bool = False  # Skonto mentioned but not readable (lenient)


def _clauses(text: str) -> list[str]:
    """Split on , ; . (not inside numbers) and und/oder."""
    parts = re.split(r"(?<!\d)[,;]|[,;](?!\d)|\.(?=\s|$)|\s(?:und|and|oder|or)\s", text)
    return [p for p in parts if p and p.strip()]


def _term_days(clause: str) -> list[tuple[int, int]]:
    """(days, position) of every day count ("14 Tage", "2 Wochen", "netto 30")."""
    out = []
    for m in _TERM_DAYS_RE.finditer(clause):
        if m.group(3):
            out.append((int(m.group(3)) * 7, m.start()))
        else:
            out.append((int(m.group(1) or m.group(2) or m.group(4)), m.start()))
    return out


def parse_terms(text: str, start: date | None, *, strict: bool = True) -> _Terms:
    """Free payment text → net days and Skonto.

    "14 Tage 2 % Skonto, 30 Tage netto", "30 Tage netto, 3 % Skonto bei Zahlung
    innerhalb 10 Tagen", "netto 30", "30 days net", "Zahlbar bis 01.12.2026":
    the day count next to "%"/"Skonto" is the Skonto period, the other one the
    net term. Only numbers with "Tage/days/d" (or "netto 30") are day counts;
    other numbers (Kd.-Nr., Mahngebühr) do not matter. `strict` (an explicit
    zahlungsziel) raises on anything unclear; otherwise (a text of the caller's
    own) only what is clear is taken and the rest is left alone.
    """
    raw = str(text)
    if re.fullmatch(r"\s*\d+\s*", raw):
        return _Terms(days=int(raw))
    terms = _Terms(no_skonto=bool(_NO_SKONTO_RE.search(raw)))

    def unclear(message: str) -> None:
        if strict:
            raise LexwareError(message)

    pct: str | None = None
    skonto_days: list[int] = []
    other: list[tuple[int, bool]] = []  # (days, next to a net word)
    counted = False
    clauses = _clauses(raw)
    for clause in clauses:
        found = _term_days(clause)
        if not strict and not _PAY_CONTEXT_RE.search(clause) and not re.fullmatch(
                r"\s*\d+\s*(?:tage?n?|days?)\s*\.?\s*", clause, re.I):
            continue  # "Reklamationen innerhalb von 8 Tagen" is no payment term
        counted = counted or bool(found)
        m = _TERM_PCT_RE.search(clause)
        until = _TERM_UNTIL_RE.search(clause)
        if m and _SKONTO_WORD_RE.search(clause) and not terms.no_skonto:
            if pct is not None and pct != m.group(1):
                unclear(f"Zahlungsbedingung '{raw}': mehrere Skonto-Sätze – bitte nur einen "
                        "angeben.")
                terms.skonto_unread = True
            pct = m.group(1)
            if len(found) >= 2:
                values = sorted(d for d, _ in found)
                skonto_days.append(values[0])
                other.append((values[-1], True))
            elif found:
                skonto_days += [d for d, _ in found]
            elif until:
                unclear(f"Zahlungsbedingung '{raw}': Skonto bis zu einem Datum lässt sich nicht "
                        "übernehmen – bitte die Skontofrist in Tagen angeben.")
                terms.skonto_unread = True
            continue
        net_word = bool(_NET_MARK_RE.search(clause))
        other += [(d, net_word) for d, _ in found]
        if until:
            day = _parse_day(until.group(1))
            if day is None or start is None:
                unclear(f"Zahlungsbedingung '{raw}': Datum nicht lesbar.")
            elif day < start:
                unclear(f"Die Fälligkeit {day:%d.%m.%Y} liegt vor dem Belegdatum "
                        f"{start:%d.%m.%Y} – bitte ein späteres Datum oder ein Zahlungsziel "
                        "in Tagen angeben.")
            else:
                other.append(((day - start).days, True))
                counted = True
    if re.search(r"\bskonto\b", raw, re.I) and pct is None and not terms.no_skonto:
        unclear(f"Zahlungsbedingung '{raw}': Skonto ohne Prozentsatz – bitte z. B. „14 Tage "
                "2 % Skonto, 30 Tage netto“ angeben.")
        terms.skonto_unread = True
    nets = sorted({d for d, _ in other})
    if len(nets) > 1:
        marked = sorted({d for d, net in other if net})
        if len(marked) == 1:
            nets = marked
        else:
            unclear(f"Zahlungsbedingung '{raw}' ist nicht eindeutig (mehrere Fristen: "
                    f"{', '.join(str(n) for n in nets)} Tage) – bitte z. B. „30 Tage netto“ "
                    "oder „14 Tage 2 % Skonto, 30 Tage netto“.")
            nets = []
    if nets:
        terms.days = nets[0]
    elif not counted and not skonto_days and pct is None and _TERM_ZERO_RE.search(raw):
        terms.days = 0
        terms.zero = True
    elif not counted and not skonto_days and pct is None and not re.search(r"\d", raw):
        for word, days in _DAY_WORDS.items():
            if re.search(rf"\b{word}\b", raw, re.I):
                terms.days = days
                terms.zero = days == 0
                break
    if pct is not None:
        problem = None
        if len(set(skonto_days)) != 1:
            problem = (f"Zahlungsbedingung '{raw}': Skonto braucht genau eine Frist, z. B. „2 % "
                       "Skonto bei Zahlung innerhalb von 10 Tagen, 30 Tage netto“.")
        elif terms.days is None and strict:
            problem = (f"Zahlungsbedingung '{raw}': Neben dem Skonto fehlt die Zahlungsfrist – "
                       "bitte z. B. „…, 30 Tage netto“ ergänzen.")
        elif terms.days is not None and skonto_days[0] >= terms.days:
            problem = (f"Zahlungsbedingung '{raw}' ist nicht eindeutig: Die Skontofrist "
                       f"({skonto_days[0]} Tage) muss kürzer sein als die Zahlungsfrist "
                       f"({terms.days} Tage).")
        if problem:
            unclear(problem)
            terms.skonto_unread = True
        else:
            terms.discount = {
                "discountPercentage": to_number(pct, field="Skonto-Prozent", kind="percent"),
                "discountRange": skonto_days[0],
            }
    return terms


def _disc(pct: str, days: str) -> dict[str, Any]:
    return {"discountPercentage": to_number(pct, field="Skonto-Prozent", kind="percent"),
            "discountRange": int(days)}


# The standard texts of Lexware and of this server (full match only). Such a
# text is rebuilt when term or Skonto change; any other text is the caller's
# own and stays as it is.
_STANDARD_LABELS: tuple[tuple[re.Pattern[str], str, Any], ...] = (
    (re.compile(r"Zahlbar sofort, rein netto\.?"), "de", lambda m: (0, None, None)),
    (re.compile(r"Zahlbar innerhalb von (\d+) Tagen(?: ohne Abzug)?\.?"), "de",
     lambda m: (int(m[1]), None, None)),
    (re.compile(r"Zahlbar innerhalb von (\d+) Tagen\. Bei Zahlung innerhalb von (\d+) Tagen "
                r"(\d+(?:[.,]\d+)?) ?% Skonto\.?"), "de",
     lambda m: (int(m[1]), _disc(m[3], m[2]), None)),
    (re.compile(r"Zahlbar bis (\d{2}\.\d{2}\.\d{4})(?: ohne Abzug)?\.?"), "de",
     lambda m: (None, None, m[1])),
    (re.compile(r"Payable by (\d{2}\.\d{2}\.\d{4}|\d{4}-\d{2}-\d{2})(?: without "
                r"deduction)?\.?"), "en", lambda m: (None, None, m[1])),
    (re.compile(r"(\d+) Tage -\s?(\d+(?:[.,]\d+)?) ?%, (\d+) Tage netto\.?"), "de",
     lambda m: (int(m[3]), _disc(m[2], m[1]), None)),
    (re.compile(r"Payable immediately without deduction\.?"), "en", lambda m: (0, None, None)),
    (re.compile(r"Payable within (\d+) days without deduction\.?"), "en",
     lambda m: (int(m[1]), None, None)),
    (re.compile(r"Payable within (\d+) days\. (\d+(?:[.,]\d+)?) ?% discount if paid within "
                r"(\d+) days\.?"), "en", lambda m: (int(m[1]), _disc(m[2], m[3]), None)),
)


def standard_label(label: Any) -> tuple[str, int | None, Any, str | None] | None:
    """(language, days, Skonto, due date text) of a standard text, else None."""
    text = re.sub(r"\s+", " ", str(label or "")).strip()
    for pattern, lang, read in _STANDARD_LABELS:
        m = pattern.fullmatch(text)
        if m:
            return (lang, *read(m))
    return None


def is_standard_label(label: Any) -> bool:
    return standard_label(label) is not None


def _lower_keys(out: dict[str, Any], known: Any) -> None:
    """"Positionen", "Titel", "VoucherDate" → the spelling the tables use."""
    lookup = {k.casefold(): k for k in known}
    for key in list(out):
        canon = lookup.get(key.casefold())
        if canon and canon != key and canon not in out:
            out[canon] = out.pop(key)


def _apply_aliases(out: dict[str, Any]) -> None:
    _lower_keys(out, [*_DOC_ALIASES, *_DOC_FIELDS, *_CUSTOMER_KEYS, *_CONTACT_KEYS,
                      *_DISCOUNT_KEYS, *_PERSON_KEYS, *_TAX_KEYS, "kundennummer",
                      "customerNumber"])
    for key in ("kundennummer", "customerNumber"):
        if key in out:
            out.setdefault("customer", out.pop(key))
    for alias, target in _DOC_ALIASES.items():
        if alias in out and target not in out:
            out[target] = out.pop(alias)
        elif alias in out and alias != target:
            out.pop(alias)
    if isinstance(out.get("language"), str):
        word = out["language"].strip().casefold()
        out["language"] = ("en" if word.startswith(("en", "eng")) else
                           "de" if word.startswith(("de", "ger")) else out["language"])
    for key in _TAX_KEYS:
        if key in out:
            value = out.pop(key)
            tax = out.get("taxConditions") if isinstance(out.get("taxConditions"), dict) else {}
            out["taxConditions"] = {**tax, "taxType": value}
    tax = out.get("taxConditions")
    if isinstance(tax, str):
        out["taxConditions"] = tax = {"taxType": tax}
    if isinstance(tax, dict) and tax.get("taxType"):
        tax["taxType"] = tax_type_word(tax["taxType"])
    _skonto(out)
    _period(out)
    for key in _CONTACT_KEYS:
        if key in out:
            value = out.pop(key)
            address = out.get("address") if isinstance(out.get("address"), dict) else {}
            if value and not address.get("contactId"):
                out["address"] = {**address, "contactId": value}
    customer = None
    given_customer = False
    for key in _CUSTOMER_KEYS:
        if key in out:
            given_customer = True
            customer = customer or out.pop(key)
            out.pop(key, None)
    if customer and out.get("address"):
        raise LexwareError(
            "Bitte den Kunden nur einmal angeben – entweder kunde/customer ODER address."
        )
    if customer:
        out["address"] = _customer_address(customer)
    elif given_customer and not out.get("address"):
        raise LexwareError(_NO_CUSTOMER)
    person = None
    for key in _PERSON_KEYS:
        if key in out:
            person = person or out.pop(key)
            out.pop(key, None)
    if person:
        address = out.get("address") if isinstance(out.get("address"), dict) else {}
        out["address"] = {**address, "contactPerson": _person_text(person)}
    _discount(out)


_NO_CUSTOMER = ("Bitte einen Kunden angeben (kunde: Name, Kundennummer oder id) – oder eine "
                "Einmaladresse, z. B. address {\"name\": \"Barverkauf\"}.")
_PERSON_KEYS = ("ansprechpartner", "ansprechpartnerin", "contactPerson", "contact_person",
                "kontaktperson")
_TAX_KEYS = ("steuerart", "taxType", "tax_type", "besteuerung")


def _person_text(value: Any) -> str:
    """{"vorname": "Max", "nachname": "Gruber", "anrede": "Herr"} → "Herr Max Gruber"."""
    if isinstance(value, dict):
        parts = [value.get(k) for k in ("salutation", "anrede", "firstName", "vorname",
                                         "lastName", "nachname", "name")]
        return " ".join(str(p).strip() for p in parts if p and str(p).strip())
    return one_line(str(value))


def _discount(out: dict[str, Any]) -> None:
    """rabatt "10 %" / "50 €" / 0 and totalPrice discounts → one clean discount."""
    given = [(k, out.pop(k)) for k in _DISCOUNT_KEYS if k in out]
    given += [(k, out.pop(k)) for k in ("rabatt_betrag", "rabattBetrag",
                                         "totalDiscountAbsolute") if k in out]
    total = out.get("totalPrice") if isinstance(out.get("totalPrice"), dict) else None
    if total is not None:
        inside = [(k, total.pop(k)) for k in ("totalDiscountPercentage", "totalDiscountAbsolute")
                  if k in total]
        inside = [(k, v) for k, v in inside if v is not None]
        if len(inside) == 2 and (any(v in (0, "0") for _, v in inside) or any(
                k in out for k in ("id", "createdDate"))):
            # a read document carries both (Lexware computes the other one)
            inside = [kv for kv in inside if kv[1] not in (0, "0")][:1] or inside[:1]
        given += inside
    if len(given) > 1:
        raise LexwareError(
            "Mehrere Rabattangaben (" + ", ".join(k for k, _ in given) + ") – bitte nur "
            "eine: rabatt_prozent (z. B. 5) ODER rabatt_betrag in Euro (z. B. 50)."
        )
    if not given:
        return
    key, value = given[0]
    if isinstance(value, str) and value.strip().casefold() in ("ohne", "kein", "keiner",
                                                               "none", "nein", "-"):
        value = 0
    is_money = key in ("rabatt_betrag", "rabattBetrag", "totalDiscountAbsolute") or (
        isinstance(value, str) and re.search(r"€|eur", value, re.I))
    field = "totalDiscountAbsolute" if is_money else "totalDiscountPercentage"
    number = to_number(value, field=field, kind="money" if is_money else "percent")
    out["totalPrice"] = {**(total or {}), field: number}


_SKONTO_KEYS = ("skonto", "skonto_prozent", "skontoProzent", "skonto_tage", "skontoTage",
                "cashDiscount", "paymentDiscountConditions")
_NO_WORDS = ("0", "ohne", "kein", "keins", "keines", "nein", "none", "no", "ohne skonto",
             "kein skonto", "false", "-")


def _skonto(out: dict[str, Any]) -> None:
    """skonto "2% 7 Tage" / {prozent, tage} / skonto_prozent + skonto_tage →
    paymentConditions.paymentDiscountConditions; 0/"ohne" → None (= remove)."""
    percent = days = None
    found = False
    for key in _SKONTO_KEYS:
        if key not in out:
            continue
        found = True
        value = out.pop(key)
        if value is None or value is False or value == 0 or (
                isinstance(value, str) and value.strip().casefold() in _NO_WORDS):
            out["paymentConditions"] = {**_block(out, "paymentConditions"),
                                        "paymentDiscountConditions": None}
            return
        if isinstance(value, dict):
            percent = value.get("discountPercentage", value.get("prozent", percent))
            days = value.get("discountRange", value.get("tage", days))
            if percent in (0, "0", None) and days in (0, "0", None):
                out["paymentConditions"] = {**_block(out, "paymentConditions"),
                                            "paymentDiscountConditions": None}
                return
        elif key.lower().endswith(("tage", "range")):
            days = value
        elif isinstance(value, str) and re.search(r"\d", value):
            m_pct = re.search(r"(\d+(?:[.,]\d+)?)\s*%", value)
            m_days = _DAYS_RE.search(value)
            percent = m_pct.group(1) if m_pct else percent
            days = m_days.group(1) if m_days else days
        else:
            percent = value
    if not found:
        return
    if percent is None or days is None:
        raise LexwareError(
            "Skonto braucht Prozent und Tage, z. B. skonto: \"2 % 7 Tage\" oder "
            "{\"prozent\": 2, \"tage\": 7}."
        )
    block = _block(out, "paymentConditions")
    block["paymentDiscountConditions"] = {
        "discountPercentage": to_number(percent, field="Skonto-Prozent", kind="percent"),
        "discountRange": _days(days),
    }
    out["paymentConditions"] = block


_PERIOD_KEYS = {"zeitraum": "serviceperiod", "leistungszeitraum": "serviceperiod",
                "ausfuehrungszeitraum": "serviceperiod",
                "ausführungszeitraum": "serviceperiod", "servicePeriod": "serviceperiod",
                "lieferzeitraum": "deliveryperiod", "deliveryPeriod": "deliveryperiod"}


def _period(out: dict[str, Any]) -> None:
    """"17.11.–21.11.2026" or {von, bis} → serviceperiod/deliveryperiod."""
    for key, kind in _PERIOD_KEYS.items():
        if key not in out:
            continue
        value = out.pop(key)
        if isinstance(value, dict):
            start = value.get("von", value.get("start", value.get("from")))
            end = value.get("bis", value.get("end", value.get("to")))
        else:
            tokens = re.findall(r"\d{4}-\d{2}-\d{2}|\d{1,2}\.\d{1,2}\.(?:\d{2,4})?",
                                str(value))
            start, end = (tokens + [None, None])[:2]
        if not start or not end:
            raise LexwareError(
                f"Zeitraum '{value}' nicht lesbar – bitte z. B. \"17.11.2026 – 21.11.2026\"."
            )
        end_day = _parse_day(str(end).strip().rstrip("."))
        start_text = str(start).strip()
        if re.fullmatch(r"\d{1,2}\.\d{1,2}\.?", start_text) and end_day:
            start_text = f"{start_text.rstrip('.')}.{end_day.year}"
        start_day = _parse_day(start_text)
        if not start_day or not end_day:
            raise LexwareError(f"Zeitraum '{value}' nicht lesbar – bitte TT.MM.JJJJ – TT.MM.JJJJ.")
        out["shippingConditions"] = {
            **_block(out, "shippingConditions"), "shippingType": kind,
            "shippingDate": start_day.isoformat(), "shippingEndDate": end_day.isoformat(),
        }


_COUNTRY_WORDS = {"deutschland": "DE", "germany": "DE", "österreich": "AT",
                  "oesterreich": "AT", "austria": "AT", "schweiz": "CH",
                  "switzerland": "CH", "suisse": "CH", "liechtenstein": "LI",
                  "luxemburg": "LU", "de": "DE", "at": "AT", "ch": "CH", "a": "AT"}


def _customer_address(customer: Any) -> dict[str, Any]:
    """A `customer`/`kunde` value → address.

    A dict is the address itself. A string is a REFERENCE to an existing
    contact (id, number or exact name — resolved later, never stored as a bare
    address name). Only a string that clearly is a postal address ("Name,
    Straße 1, 12345 Ort") becomes a one-off address.
    """
    if isinstance(customer, dict):
        return customer
    text = str(customer).strip()
    parts = [p.strip() for p in text.split(",") if p.strip()]
    country = None
    if len(parts) >= 3 and parts[-1].casefold() in _COUNTRY_WORDS:
        country = _COUNTRY_WORDS[parts.pop().casefold()]
    if len(parts) >= 2:
        prefix = re.match(r"^([A-Z]{1,2})-(?=\d)", parts[-1])  # "A-1010 Wien", "CH-8001"
        if prefix and country is None:
            country = {"A": "AT", "CH": "CH", "D": "DE", "FL": "LI", "L": "LU"}.get(
                prefix.group(1))
        last = re.sub(r"^(?:[A-Z]{1,2}-)", "", parts[-1])
        m = _ZIP_RE.search(last)
        if m:
            if country is None and len(m.group(1)) == 4:
                raise LexwareError(
                    f"Die Postleitzahl {m.group(1)} ist 4-stellig – bitte das Land dazu "
                    "angeben (z. B. „…, 1010 Wien, Österreich“ oder „…, 8001 Zürich, Schweiz“)."
                )
            address: dict[str, Any] = {"name": parts[0], "zip": m.group(1),
                                       "city": m.group(2).strip(),
                                       "countryCode": country or "DE"}
            if len(parts) >= 3:
                address["street"] = parts[1]
            return address
    return {"contactId": text}


_TERM_KEYS = ("paymentTermDuration", "zahlungsziel", "paymentTerms", "zahlungsfrist",
              "payment_terms", "paymentTerm")


def _discount_value(value: Any) -> tuple[Any, Any]:
    if not isinstance(value, dict):
        return None, None
    pct = to_number(value.get("discountPercentage"), field="Skonto-Prozent", kind="percent")
    rng = value.get("discountRange")
    return pct, (_days(rng) if rng is not None else None)


def _payment(
    out: dict[str, Any], *, language: Any, need_days: bool = True, is_copy: bool = False
) -> None:
    """zahlungsziel, paymentConditions (text, label, duration), Skonto and dueDate
    → one consistent paymentConditions block.

    Explicit values (zahlungsziel, paymentTermDuration, Skonto) win. A standard
    text (Lexware's own wording) is rebuilt from them; a "sofort"/"Vorkasse"
    text is rebuilt when a term is given; any other text is the caller's own
    and stays exactly as written — only when it names another day count than
    the given term, that is a contradiction.
    """
    notes: list[str] = out.setdefault("__pay_notes", [])
    start = _day_of(out.get("voucherDate"))
    explicit: list[tuple[str, int]] = []
    discounts: list[tuple[str, dict[str, Any]]] = []
    no_skonto = False
    for key in _TERM_KEYS:
        if key not in out:
            continue
        value = out.pop(key)
        if value is None or value == "":
            continue
        if isinstance(value, str) and not re.fullmatch(r"\s*\d+\s*", value):
            terms = parse_terms(value, start)
            if terms.days is None:
                raise LexwareError(
                    f"Zahlungsziel '{value}' nicht lesbar – bitte Tage als Zahl angeben "
                    "(z. B. 14) oder z. B. „14 Tage 2 % Skonto, 30 Tage netto“."
                )
            explicit.append(("zahlungsziel", terms.days))
            if terms.discount:
                discounts.append(("zahlungsziel", terms.discount))
            no_skonto = no_skonto or terms.no_skonto
        else:
            explicit.append(("zahlungsziel", _days(value)))
    raw = out.get("paymentConditions")
    if raw is None and not explicit:
        return
    if isinstance(raw, str):
        raw = {"paymentTermLabel": raw}
    if raw is not None and not isinstance(raw, dict):
        raise LexwareError("paymentConditions bitte als Text (z. B. „30 Tage netto“) oder als "
                           "Objekt {paymentTermDuration, paymentTermLabel} angeben.")
    block = dict(raw or {})
    removal = "paymentDiscountConditions" in block and block[
        "paymentDiscountConditions"] is None
    if isinstance(block.get("paymentDiscountConditions"), dict):
        pct, rng = _discount_value(block["paymentDiscountConditions"])
        if pct:
            discounts.insert(0, ("skonto", {"discountPercentage": pct, "discountRange": rng}))
        else:
            block.pop("paymentDiscountConditions")  # 0 % / 0 Tage = no Skonto
    if block.get("paymentTermDuration") is not None:
        explicit.append(("paymentTermDuration", _days(block["paymentTermDuration"])))
    if len({d for _, d in explicit}) > 1:
        listing = ", ".join(f"{src} {d} Tage" for src, d in explicit)
        raise LexwareError(
            f"Widersprüchliche Zahlungsfristen ({listing}) – bitte nur eine Frist angeben."
        )
    days = explicit[0][1] if explicit else None
    label = block.get("paymentTermLabel")
    label = label.strip() if isinstance(label, str) and label.strip() else None
    kind = "none"
    label_days: int | None = None
    label_disc: dict[str, Any] | None = None
    regenerate = label is None
    label_zero = False
    until = None
    if label:
        std = standard_label(label)
        if std:
            kind = "standard"
            label_lang, label_days, label_disc, until = std
            if until:
                day = _parse_day(until)
                label_days = (day - start).days if day and start and day >= start else None
            if label_lang == "de" and str(language or "").casefold() == "en":
                regenerate = True  # a German standard text on an English document
        else:
            terms = parse_terms(label, start, strict=False)
            # only the bare short forms ("Vorkasse", "Zahlbar sofort") are rebuilt;
            # a longer text saying "sofort" is the caller's own and stays
            kind = "zero" if _BARE_ZERO_RE.fullmatch(re.sub(r"\s+", " ", label)) else "own"
            label_zero = terms.zero
            label_days, label_disc = terms.days, terms.discount
            no_skonto = no_skonto or terms.no_skonto
            if terms.skonto_unread and not discounts:
                notes.append(f"Das Skonto im Zahlungstext „{label}“ wurde nicht als "
                             "Skonto-Bedingung übernommen – dafür skonto angeben (z. B. "
                             "„2 % 7 Tage“).")
    if kind == "standard" and until and is_copy and days is not None:
        # "Zahlbar bis <Datum>" of the document that was read: the date is old
        regenerate = True
        notes.append(f"Der Zahlungstext „{label}“ nannte das Datum des alten Belegs – neu "
                     f"gebildet aus der Frist ({days} Tage).")
    elif label and days is None:
        days = label_days
    elif label and label_days is not None and label_days != days:
        if kind == "own" and label_zero:
            notes.append(f"Hinweis: Der Zahlungstext „{label}“ sagt „sofort“, die Frist ist "
                         f"aber {days} Tage.")
        elif kind == "own":
            raise LexwareError(
                f"Widersprüchliche Zahlungsfristen: Der Zahlungstext „{label}“ nennt "
                f"{label_days} Tage, die Frist ist {days} Tage – bitte nur eines angeben."
            )
        else:
            regenerate = True
        if kind == "zero":
            notes.append(f"Der Zahlungstext „{label}“ (sofort) passte nicht zur Frist "
                         f"{days} Tage – neu gebildet.")
    if label_disc and days is not None and label_disc["discountRange"] >= days:
        label_disc = None  # Skonto period not shorter than the term: not usable
    if label_disc:
        same = discounts and (float(discounts[0][1]["discountPercentage"]),
                              discounts[0][1]["discountRange"]) == (
            float(label_disc["discountPercentage"]), label_disc["discountRange"])
        if discounts and not same:
            if kind == "own":
                raise LexwareError(
                    f"Widersprüchliches Skonto: Der Zahlungstext „{label}“ nennt "
                    f"{label_disc['discountPercentage']} % / {label_disc['discountRange']} "
                    f"Tage, angegeben ist {discounts[0][1]['discountPercentage']} % / "
                    f"{discounts[0][1]['discountRange']} Tage – bitte nur eines."
                )
            regenerate = True
        elif not discounts:
            discounts.append(("label", label_disc))
    elif kind == "standard" and discounts:
        regenerate = True  # the standard text does not mention the Skonto
    if len({(float(d["discountPercentage"]), d["discountRange"]) for _, d in discounts}) > 1:
        listing = ", ".join(f"{d['discountPercentage']} % / {d['discountRange']} Tage"
                            for _, d in discounts)
        raise LexwareError(f"Widersprüchliche Skonto-Angaben ({listing}) – bitte nur eine.")
    if no_skonto and discounts:
        raise LexwareError("Skonto ist zugleich angegeben und ausgeschlossen („ohne Skonto“) "
                           "– bitte eines von beiden.")
    if days is not None:
        block["paymentTermDuration"] = days
    if discounts:
        block["paymentDiscountConditions"] = discounts[0][1]
        if kind == "standard" and label_disc is None:
            regenerate = True
    elif no_skonto or removal:
        block["paymentDiscountConditions"] = None
        if kind == "standard" and label_disc:
            regenerate = True
    if regenerate and days is not None:
        block["paymentTermLabel"] = _payment_label(
            days, block.get("paymentDiscountConditions"), language)
        block.pop("paymentTermLabelTemplate", None)  # its old wording would win
        block["__autolabel"] = True
    if days is None and need_days and discounts:
        raise LexwareError(
            "Für das Skonto fehlt die Zahlungsfrist – bitte auch zahlungsziel in Tagen "
            "angeben (z. B. 30)."
        )
    out["paymentConditions"] = block


def _due_and_delivery(
    out: dict[str, Any], kind: str, *, language: Any = None, need_days: bool = True,
    is_copy: bool = False,
) -> None:
    """dueDate → paymentTermDuration, deliveryDate → shippingConditions."""
    language = out.get("language") or language
    due = out.pop("dueDate", None) or out.pop("faelligkeit", None) or out.pop("fälligkeit", None)
    if due and kind != DELIVERY_NOTE:
        start = _day_of(out.get("voucherDate"))
        stamp = document_timestamp(due)
        end = _day_of(stamp)
        if end is None:
            raise LexwareError(
                f"Fälligkeit '{due}' nicht lesbar – bitte ein Datum wie 01.12.2026 oder ein "
                "Zahlungsziel in Tagen (zahlungsziel) angeben."
            )
        if start and end < start:
            raise LexwareError(
                f"Die Fälligkeit {end:%d.%m.%Y} liegt vor dem Belegdatum {start:%d.%m.%Y} – "
                "bitte ein späteres Datum oder ein Zahlungsziel in Tagen angeben."
            )
        if start is None:
            raise LexwareError(
                f"Die Fälligkeit '{due}' lässt sich ohne Belegdatum nicht in Tage umrechnen "
                "– bitte ein Zahlungsziel in Tagen (zahlungsziel) angeben."
            )
        n = (end - start).days
        block = _block(out, "paymentConditions")
        if block.get("paymentTermDuration") is not None and _days(
                block["paymentTermDuration"]) != n:
            raise LexwareError(
                f"Fälligkeit {end:%d.%m.%Y} ({n} Tage) und Zahlungsziel "
                f"{block['paymentTermDuration']} Tage widersprechen sich – bitte nur eines."
            )
        out["paymentConditions"] = {**block, "paymentTermDuration": n}
        _payment(out, language=language, need_days=need_days, is_copy=is_copy)
        cond = out["paymentConditions"]
        if cond.pop("__autolabel", False) and not cond.get("paymentDiscountConditions"):
            cond["paymentTermLabel"] = _payment_label(n, None, language, until=end)
            cond["__autolabel"] = True
        elif cond.get("paymentDiscountConditions"):
            cond["__autolabel"] = True
    else:
        _payment(out, language=language, need_days=need_days, is_copy=is_copy)
    delivery = None
    kind_of_date = None
    # The word decides what the document prints: Lieferdatum vs. Leistungsdatum
    # (as the web app does — e.g. an order with "Lieferdatum 13.11.").
    for key, word in (("deliveryDate", "delivery"), ("lieferdatum", "delivery"),
                      ("liefertermin", "delivery"), ("shippingDate", None),
                      ("leistungsdatum", "service"), ("serviceDate", "service"),
                      ("ausfuehrungsdatum", "service"), ("ausführungsdatum", "service")):
        if key in out:
            delivery = out.pop(key)
            kind_of_date = word
    if delivery and kind in _NEEDS_SHIPPING:
        ship = _block(out, "shippingConditions")
        if not ship.get("shippingDate"):
            default = "delivery" if kind == DELIVERY_NOTE else "service"
            out["shippingConditions"] = {
                "shippingType": ship.get("shippingType") or kind_of_date or default,
                **ship,
                "shippingDate": delivery,
            }


def _check_dates(out: dict[str, Any]) -> None:
    """Every date field readable (else a German error), shipping dates formatted."""
    ship = out.get("shippingConditions")
    if isinstance(ship, dict):
        for key, label in (("shippingDate", "Liefer-/Leistungsdatum"),
                           ("shippingEndDate", "Ende des Zeitraums")):
            if ship.get(key):
                ship[key] = checked_timestamp(ship[key], label)


_CHANGE_TARGET_LABEL = {ORDER_CONFIRMATION: "Auftrag", DELIVERY_NOTE: "Lieferschein",
                        INVOICE: "Rechnung", "creditnote": "Rechnungskorrektur",
                        "dunning": "Mahnung"}


def prepare_changes(
    changes: dict[str, Any],
    kind: str,
    tax_type: str,
    base_date: str | None = None,
    *,
    client: LexwareClient | None = None,
    language: Any = None,
    currency: str = "EUR",
) -> dict[str, Any]:
    """Normalize adjustments for a follow-up document (continue_document).

    `base_date` is the follow-up's voucherDate, needed to turn a due date
    into a payment term when the changes don't set a date themselves. Line
    items are completed exactly like on create (article by name/number/id,
    price text, German field names, net/gross to the document's tax type).
    """
    out = strip_control(copy.deepcopy(unwrap(changes, "changes")))
    notes: list[str] = []
    _apply_aliases(out)
    lang = out.get("language") or language
    own_date = bool(out.get("voucherDate"))
    if own_date:
        out["voucherDate"] = checked_timestamp(out["voucherDate"], "Belegdatum")
        check_not_far_future(out["voucherDate"])
    elif base_date:
        out["voucherDate"] = base_date
    label = _CHANGE_TARGET_LABEL.get(kind, "Folgebeleg")
    if out.get("expirationDate"):
        checked_timestamp(out["expirationDate"], "Gültig bis")
        raise LexwareError(
            f"Ein Gültigkeitsdatum (gültig bis) gibt es nur bei Angeboten – die {label} hat "
            "keins. Bitte weglassen (für die Fälligkeit zahlungsziel bzw. dueDate)."
            if kind in (INVOICE, "creditnote", "dunning") else
            f"Ein Gültigkeitsdatum (gültig bis) gibt es nur bei Angeboten, nicht beim {label}. "
            "Bitte weglassen."
        )
    out.pop("expirationDate", None)
    if "lineItems" in out and not isinstance(out["lineItems"], (list, dict)):
        raise LexwareError("changes.lineItems muss eine Liste von Positionen sein.")
    if isinstance(out.get("lineItems"), (list, dict)):
        items = out["lineItems"] if isinstance(out["lineItems"], list) else [out["lineItems"]]
        out["lineItems"] = [
            _normalize_line(i, tax_type, notes) if isinstance(i, dict) else i for i in items
        ]
        _complete_lines(client, out["lineItems"], kind, tax_type, currency, lang, notes)
    due = out.get("dueDate")
    _due_and_delivery(out, kind if kind in (INVOICE, ORDER_CONFIRMATION, DELIVERY_NOTE)
                      else INVOICE, language=lang, need_days=False)
    notes += out.pop("__pay_notes", [])
    if due and "paymentConditions" not in out and kind != DELIVERY_NOTE:
        raise LexwareError(
            f"Die Fälligkeit '{due}' passt nicht zum Belegdatum – bitte ein späteres Datum "
            "oder ein Zahlungsziel in Tagen (zahlungsziel) angeben."
        )
    if not own_date:
        out.pop("voucherDate", None)
    _check_dates(out)
    _check_fields(out)
    if notes:
        out["__hinweis"] = " ".join(notes)
    return out


def _check_fields(out: dict[str, Any]) -> None:
    """Unknown fields raise — Lexware would drop them silently (e.g. a
    validity date under a made-up name) and the document comes out wrong."""
    for key in _READ_ONLY_FIELDS & set(out):
        out.pop(key)
    for item in out.get("lineItems") or []:
        if isinstance(item, dict):
            item.pop("lineItemAmount", None)
            price = item.get("unitPrice")
            if isinstance(price, dict) and "amount" in price:
                raise LexwareError(
                    "Preis einer Position unklar – bitte unitPrice.netAmount (netto) oder "
                    "unitPrice.grossAmount (brutto) angeben."
                )
    unknown = sorted(k for k in out if k not in _DOC_FIELDS and not k.startswith("__"))
    if unknown:
        raise LexwareError(
            f"Unbekannte Felder im Beleg: {', '.join(unknown)} – sie würden von Lexware "
            "ignoriert. Erlaubt sind u. a.: address (oder customer/kunde), voucherDate "
            "(Datum), expirationDate (gültig bis), title, introduction, remark, "
            "lineItems, paymentConditions (oder dueDate/zahlungsziel), "
            "shippingConditions (oder lieferdatum/leistungsdatum), deliveryTerms, "
            "totalPrice.totalDiscountPercentage (Gesamtrabatt)."
        )
    for index, item in enumerate(out.get("lineItems") or [], start=1):
        if not isinstance(item, dict):
            continue
        bad = sorted(k for k in item if k not in _LINE_FIELDS and not k.startswith("__"))
        if bad:
            raise LexwareError(
                f"Unbekannte Felder in Position {index}: {', '.join(bad)}. Erlaubt sind: "
                "type, id (Artikel-id) bzw. artikelnummer, name, description, quantity, "
                "unitName, unitPrice {netAmount|grossAmount, taxRatePercentage}, "
                "discountPercentage."
            )


def _block(out: dict[str, Any], key: str) -> dict[str, Any]:
    value = out.get(key)
    return value if isinstance(value, dict) else {}


def _payment_label(
    days: int, discount: Any, language: Any, until: date | None = None
) -> str:
    """Payment text in the document's language; with Skonto never "ohne Abzug"."""
    english = str(language or "").casefold() == "en"
    if isinstance(discount, dict) and discount.get("discountPercentage"):
        pct, rng = discount.get("discountPercentage"), discount.get("discountRange")
        if isinstance(pct, float) and not english:
            pct = f"{pct:g}".replace(".", ",")  # 2,5 % Skonto
        if english:
            return (f"Payable within {days} days. {pct} % discount if paid within "
                    f"{rng} days.")
        return (f"Zahlbar innerhalb von {days} Tagen. Bei Zahlung innerhalb von {rng} "
                f"Tagen {pct} % Skonto.")
    if english:
        if days == 0:
            return "Payable immediately without deduction."
        return f"Payable within {days} days without deduction."
    if days == 0:
        return "Zahlbar sofort, rein netto"
    if until:
        return f"Zahlbar bis {until:%d.%m.%Y} ohne Abzug"
    return f"Zahlbar innerhalb von {days} Tagen ohne Abzug"


def payment_label(days: int, discount: Any, language: Any) -> str:
    return _payment_label(days, discount, language)


def _day_of(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value).date()
    except ValueError:
        return None


def _first(out: dict[str, Any], *keys: str) -> Any:
    """Pop the first present key (also when its value is 0) and drop the rest."""
    found = None
    for key in keys:
        if key in out:
            value = out.pop(key)
            if found is None and value is not None and value != "":
                found = value
    return found


def _round(value: Any, places: int, notes: list[str] | None = None, label: str = "") -> Any:
    """Lexware takes at most 4 (prices, quantity) resp. 2 (discount) decimals."""
    if isinstance(value, (int, float)) and not isinstance(value, bool) and abs(value) >= 1e12:
        raise LexwareError(f"Die Zahl {value:g} ist zu groß für Lexware.")
    if isinstance(value, float):
        try:
            rounded = float(Decimal(str(value)).quantize(Decimal(1).scaleb(-places),
                                                         rounding=ROUND_HALF_UP))
        except InvalidOperation as exc:
            raise LexwareError(f"Die Zahl {value!r} ist zu groß.") from exc
        if notes is not None and rounded != value:
            notes.append(f"{label or 'Wert'} {value} auf {rounded:g} gerundet (Lexware nimmt "
                         f"höchstens {places} Nachkommastellen).")
        return int(rounded) if rounded.is_integer() else rounded
    return value


def _price_from_article(article: dict[str, Any], tax_type: str) -> dict[str, Any] | None:
    price = article.get("price") if isinstance(article.get("price"), dict) else None
    if not price:
        return None
    out: dict[str, Any] = {"taxRatePercentage": price.get("taxRate")}
    if tax_type == "gross" and price.get("grossPrice") is not None:
        out["grossAmount"] = price["grossPrice"]
    elif price.get("netPrice") is not None:
        out["netAmount"] = price["netPrice"]
    elif price.get("grossPrice") is not None:
        out["grossAmount"] = price["grossPrice"]
    return out


# -- quantities with a unit ("5 Rollen", "2 Std") -------------------------------

# (group, unit) of the units that can contradict each other: "30 min" on a
# position in hours is refused, "5 Rollen", "1000 Etiketten" are just fine.
_UNITS: dict[str, tuple[str, str]] = {}
for _group, _unit, _words in (
    ("time", "second", "s sek sec sekunde sekunden"),
    ("time", "minute", "min mins minute minuten"),
    ("time", "hour", "h std stunde stunden hr hrs hour hours"),
    ("time", "day", "tag tage tagen day days"),
    ("time", "week", "woche wochen week weeks"),
    ("time", "month", "monat monate month months"),
    ("weight", "g", "g gramm"),
    ("weight", "kg", "kg kilo kilogramm"),
    ("weight", "t", "t tonne tonnen"),
    ("length", "mm", "mm millimeter"),
    ("length", "cm", "cm zentimeter"),
    ("length", "m", "m meter lfm"),
    ("length", "km", "km kilometer"),
    ("volume", "ml", "ml milliliter"),
    ("volume", "l", "l liter litre"),
):
    for _w in _words.split():
        _UNITS[_w] = (_group, _unit)
_UNIT_NAMES = {"second": "Sekunde", "minute": "Minute", "hour": "Stunde", "day": "Tag",
               "week": "Woche", "month": "Monat", "l": "Liter"}
_PIECE_WORDS = {"stück", "stueck", "stck", "stk", "st", "x", "pcs", "pc", "piece", "pieces"}
_QTY_UNIT_RE = re.compile(r"\s*([+-]?\d[\d.,\s]*?)\s*([^\W\d_][\w²³/-]*?)\.?\s*")


def _unit_of(word: Any) -> tuple[str, str] | None:
    return _UNITS.get(str(word or "").strip().casefold().rstrip("."))


def _quantity(item: dict[str, Any]) -> None:
    """quantity "5 Rollen" / "2 Std" → number; the unit is checked once the
    position's unit is known (article positions: the article's)."""
    raw = item.get("quantity")
    if not isinstance(raw, str):
        return
    import unicodedata

    text = unicodedata.normalize("NFKC", raw)
    try:
        to_number(text, field="quantity", kind="quantity")
        return  # a plain number ("2,5", "2e3")
    except LexwareError:
        pass
    m = _QTY_UNIT_RE.fullmatch(text)
    if not m or is_change(text):
        return  # not "number + one word": to_number explains it
    item["quantity"] = m.group(1)
    item["__qty_unit"] = (raw, m.group(2))


# Spellings of one unit (abbreviations, singular/plural) → one key.
_UNIT_SAME: dict[str, str] = {}
for _key, _words in (
    ("stück", "stück stueck stk st stck x pcs pc piece pieces"),
    ("stunde", "stunde stunden std h hr hrs hour hours"),
    ("minute", "minute minuten min mins"),
    ("sekunde", "sekunde sekunden s sek sec"),
    ("tag", "tag tage tagen d day days"),
    ("woche", "woche wochen week weeks"),
    ("monat", "monat monate month months"),
    ("meter", "meter m lfm"), ("km", "km kilometer"), ("cm", "cm zentimeter"),
    ("mm", "mm millimeter"), ("liter", "liter l litre"), ("ml", "ml milliliter"),
    ("kg", "kg kilo kilogramm"), ("g", "g gramm"), ("t", "t tonne tonnen"),
    ("m2", "m2 m² qm quadratmeter"), ("m3", "m3 m³ kubikmeter cbm"),
    ("pauschal", "pauschal pauschale psch pausch pauschalen"),
    ("paket", "paket pakete pkg pck"), ("set", "set sets"),
):
    for _w in _words.split():
        _UNIT_SAME[_w] = _key


def _unit_key(word: Any) -> set[str]:
    """Comparable forms of a unit word: table entry, else singular/plural stems."""
    text = str(word or "").strip().casefold().rstrip(".")
    if text in _UNIT_SAME:
        return {_UNIT_SAME[text]}
    forms = {text}
    for ending in ("en", "es", "er", "n", "s", "e"):
        if text.endswith(ending) and len(text) - len(ending) >= 3:
            forms.add(text[: -len(ending)])
    return forms


def _plural(unit_name: str) -> str:
    known = {"stück": "Stück", "stunde": "Stunden", "tag": "Tage", "rolle": "Rollen",
             "karton": "Kartons", "etikett": "Etiketten", "seite": "Seiten",
             "paket": "Pakete", "minute": "Minuten", "woche": "Wochen", "monat": "Monate"}
    key = str(unit_name).strip().casefold().rstrip(".")
    return known.get(key, unit_name + ("n" if key.endswith("e") else ""))


def _finish_unit(item: dict[str, Any], *, article: bool = False) -> None:
    """The word written next to the quantity, against the position's unit
    (an article position: the article's unit)."""
    stash = item.pop("__qty_unit", None)
    if not stash:
        return
    raw, word = stash
    unit_name = item.get("unitName")
    if unit_name:
        if _unit_key(word) & _unit_key(unit_name):
            return
        have, said = _unit_of(unit_name), _unit_of(word)
        if have and said and have[1] == "hour" and said[1] == "minute":
            raise LexwareError(
                f"Menge '{raw}' passt nicht zur Einheit der Position ({unit_name}) – bitte die "
                f"Menge in {unit_name} angeben, z. B. 0,5 für eine halbe Stunde."
            )
        who = "der Artikel wird" if article else "die Position wird"
        raise LexwareError(
            f"Menge '{raw}': {who} in '{unit_name}' geführt – wie viele {_plural(unit_name)}? "
            f"Bitte die Menge in {unit_name} angeben."
        )
    if str(word).casefold().rstrip(".") in _PIECE_WORDS:
        return  # the default unit (Stück)
    said = _unit_of(word)
    item["unitName"] = _UNIT_NAMES.get(said[1], said[1]) if said else str(word).rstrip(".")


def _normalize_line(
    item: dict[str, Any], tax_type: str, notes: list[str] | None = None
) -> dict[str, Any]:
    """Aliases and number formats of one line item (no API calls)."""
    item = dict(item)
    _lower_keys(item, [*_LINE_ALIASES, *_LINE_FIELDS, *_LINE_PRICE_KEYS])
    for alias, target in _LINE_ALIASES.items():
        if alias in item and target not in item:
            item[target] = item.pop(alias)
    for key in ("articleNumber", "artikelnummer", "article_number", "artikelNr", "artikel"):
        if key in item:
            number = item.pop(key)
            item.setdefault("id", str(number))  # resolved to the article id later
    if "name" in item:
        item["name"] = one_line(item["name"])
    for key in ("optional", "alternative"):
        if key in item:
            item[key] = bool_word(item[key])
    price = item.get("unitPrice")
    if not isinstance(price, dict):
        price = {"amount": price} if price is not None else {}
    for alias, target in _LINE_PRICE_KEYS.items():
        if alias in item:
            price.setdefault(target, item.pop(alias))
    for alias, target in list(_LINE_PRICE_KEYS.items()):
        if alias in price and alias != target:
            price.setdefault(target, price.pop(alias))
    for key in ("amount", "netAmount", "grossAmount"):
        value = price.get(key)
        if not isinstance(value, str):
            continue
        if is_change(value):
            raise LexwareError(
                f"Preis '{value}' ist eine Änderung, kein Preis – bitte den neuen Preis als "
                "Betrag angeben (z. B. 9,35)."
            )
        rest, word = split_gross_net(value)
        if word:
            target = "grossAmount" if word == "gross" else "netAmount"
            if key != "amount" and key != target:
                raise LexwareError(
                    f"Preis '{value}' steht als {key}, ist laut Text aber "
                    f"{'brutto' if word == 'gross' else 'netto'} – bitte nur eines angeben."
                )
            price.pop(key)
            if target in price:
                raise LexwareError(
                    f"Zwei Preise für eine Position ('{value}' und {price[target]}) – bitte "
                    "nur einen angeben."
                )
            price[target] = rest
    if "amount" in price and tax_type != "?":
        amount = price.pop("amount")
        key = "grossAmount" if tax_type == "gross" else "netAmount"
        price.setdefault(key, amount)
    for key in ("netAmount", "grossAmount", "amount"):
        if isinstance(price.get(key), str) and "%" in price[key]:
            raise LexwareError(
                f"„{price[key]}“ ist ein Prozentwert, kein Preis. Für einen Rabatt "
                "discountPercentage nehmen, für den Steuersatz taxRatePercentage."
            )
    if "taxRatePercentage" in price:
        price["taxRatePercentage"] = _round(to_number(
            rate_word(price["taxRatePercentage"]), field="unitPrice.taxRatePercentage",
            kind="percent"), 4)
    for key in ("netAmount", "grossAmount", "amount"):
        if key in price:
            price[key] = _round(to_number(price[key], field=f"unitPrice.{key}", kind="money"),
                                4, notes, f"Preis „{item.get('name') or ''}“".replace(" „“", ""))
    net, gross = price.get("netAmount"), price.get("grossAmount")
    if isinstance(net, (int, float)) and isinstance(gross, (int, float)):
        rate = price.get("taxRatePercentage")
        rate = 19 if not isinstance(rate, (int, float)) else rate
        if abs(net * (1 + rate / 100) - gross) > 0.02:
            raise LexwareError(
                f"Netto {net} und Brutto {gross} passen bei {rate} % nicht zusammen – "
                "bitte nur einen Preis angeben (netAmount ODER grossAmount)."
            )
    if price:
        item["unitPrice"] = price
    if isinstance(item.get("discountPercentage"), str) and re.search(
            r"€|eur", item["discountPercentage"], re.I):
        raise LexwareError(
            f"Positionsrabatt „{item['discountPercentage']}“ ist ein Betrag – Lexware kennt "
            "je Position nur Rabatt in Prozent. Für einen Euro-Rabatt rabatt_betrag auf "
            "den ganzen Beleg nehmen oder den Preis anpassen."
        )
    _quantity(item)
    if "quantity" in item:
        item["quantity"] = _round(to_number(item["quantity"], field="quantity",
                                            kind="quantity"), 4, notes, "Menge")
    if "discountPercentage" in item:
        item["discountPercentage"] = _round(to_number(
            item["discountPercentage"], field="discountPercentage", kind="percent"), 2)
    if item.get("type") is not None:
        word = str(item["type"]).strip().casefold()
        item["type"] = _LINE_TYPES.get(word, item["type"])
    elif item.get("id"):
        item["type"] = "material"
    elif item.get("name") and not price and "quantity" not in item:
        item["type"] = "text"
    else:
        item["type"] = "custom"
    return item


_STANDARD_TEXTS: dict[str, tuple[str, ...]] = {
    QUOTATION: ("Angebot", "Gerne bieten wir Ihnen an:",
                "Wir freuen uns auf Ihre Auftragserteilung und sichern eine einwandfreie "
                "Ausführung zu.", "Quotation", "We hereby offer you:"),
    ORDER_CONFIRMATION: ("Auftragsbestätigung", "Gerne bestätigen wir Ihren Auftrag.",
                         "Wir freuen uns auf eine gute Zusammenarbeit.",
                         "Order Confirmation", "We hereby confirm your order."),
    DELIVERY_NOTE: ("Lieferschein", "Wie vereinbart liefern wir Ihnen:",
                    "Vielen Dank für Ihren Auftrag.", "Delivery Note",
                    "As agreed, we are delivering:"),
    INVOICE: ("Rechnung", "Unsere Lieferungen/Leistungen stellen wir Ihnen wie folgt in "
              "Rechnung.", "Vielen Dank für die gute Zusammenarbeit.", "Invoice",
              "For our deliveries/services we kindly request the following payment."),
    "creditnote": ("Rechnungskorrektur", "Folgende Lieferungen/Leistungen schreiben wir "
                   "Ihnen gut."),
}


def _norm_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def drop_foreign_standard_texts(out: dict[str, Any], kind: str) -> str | None:
    """Title/intro/closing text of ANOTHER document type (a quotation read and
    sent back as an invoice) are dropped, so Lexware uses the right ones."""
    own = {_norm_text(t) for t in _STANDARD_TEXTS.get(kind, ())}
    dropped: list[str] = []
    for key, label in (("title", "Titel"), ("introduction", "Einleitung"),
                       ("remark", "Schlusstext")):
        value = out.get(key)
        if not isinstance(value, str) or _norm_text(value) in own:
            continue
        if any(_norm_text(value) in {_norm_text(t) for t in texts}
               for other, texts in _STANDARD_TEXTS.items() if other != kind):
            out.pop(key)
            dropped.append(label)
    if not dropped:
        return None
    return (f"{', '.join(dropped)} stammte(n) aus einer anderen Belegart und wurde(n) "
            "weggelassen – Lexware setzt die Standardtexte dieses Belegs.")


def _complete_lines(
    client: LexwareClient | None,
    items: list[Any],
    kind: str,
    tax_type: str,
    currency: str,
    language: Any,
    notes: list[str],
    articles: dict[str, dict[str, Any]] | None = None,
) -> None:
    """Article positions from the article (name, unit, price, type), defaults,
    0 % in tax-free documents and the price side (net/gross) of the document."""
    articles = articles if articles is not None else {}
    free = tax_type in TAX_FREE_TYPES
    default_rate = DEFAULT_TAX_RATE if tax_type in _TAXED_TYPES else 0
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            continue
        own_rate = (item.get("unitPrice") or {}).get("taxRatePercentage") if isinstance(
            item.get("unitPrice"), dict) else None
        item_type = str(item.get("type") or "")
        if item.get("id") and client is not None and not _is_uuid(str(item["id"])):
            item["id"] = _article_id_by_number(client, str(item["id"]), articles)
            if item.get("type") not in ("material", "service"):
                item["type"] = "material"
            item_type = str(item.get("type"))
        if item_type in ("material", "service"):
            article_id = str(item.get("id") or "").strip()
            if not article_id and client is not None and item.get("name"):
                article_id = _article_id_by_name(client, str(item["name"]), articles)
                if article_id:
                    item["id"] = article_id
            if not article_id:
                item["type"] = "custom"
                item.pop("id", None)
            elif client is not None:
                article = _article(client, article_id, articles)
                item["type"] = _ARTICLE_LINE_TYPES.get(
                    str(article.get("type") or "").upper(), "material"
                )
                # fill what the caller left out from the article
                item.setdefault("name", article.get("title"))
                if not item.get("unitName") and article.get("unitName"):
                    item["unitName"] = article["unitName"]
                if "description" not in item and article.get("description"):
                    item["description"] = article["description"]
                # Lexware wants a unit price on every article position — also
                # on a delivery note (406 "unitPrice must not be null" otherwise)
                if not isinstance(item.get("unitPrice"), dict) or (
                    item["unitPrice"].get("netAmount") is None
                    and item["unitPrice"].get("grossAmount") is None
                ):
                    from_article = _price_from_article(article, tax_type)
                    if from_article:
                        item["unitPrice"] = {**from_article, **(item.get("unitPrice") or {})}
                elif item["unitPrice"].get("taxRatePercentage") is None:
                    # own price, no rate given: the article's rate (7 % book), not 19
                    rate = (article.get("price") or {}).get("taxRate") if isinstance(
                        article.get("price"), dict) else None
                    if rate is not None:
                        item["unitPrice"]["taxRatePercentage"] = rate
        _finish_unit(item, article=item.get("type") in ("material", "service")
                     and bool(item.get("id")))
        if item.get("type") == "text":
            continue
        item.setdefault("quantity", 1)
        if item.get("type") == "custom" and not item.get("unitName"):
            item["unitName"] = "piece" if language == "en" else "Stück"
        price = item.get("unitPrice")
        if not isinstance(price, dict) and kind == DELIVERY_NOTE:
            zero_key = "grossAmount" if tax_type == "gross" else "netAmount"
            price = item["unitPrice"] = {zero_key: 0}
        if not isinstance(price, dict):
            continue
        price.setdefault("currency", currency)
        if free:
            if own_rate not in (None, 0):
                notes.append(f"Position {index}: Steuersatz {own_rate} % auf 0 % gesetzt – "
                             f"bei der Steuerart {tax_type} ist jede Position steuerfrei.")
            price["taxRatePercentage"] = 0
        elif price.get("taxRatePercentage") is None:
            price["taxRatePercentage"] = default_rate
        _price_side(price, tax_type, index, notes)


def _price_side(price: dict[str, Any], tax_type: str, index: int, notes: list[str]) -> None:
    """A gross price in a net document (or the other way round) → converted."""
    rate = Decimal(str(price.get("taxRatePercentage") or 0))
    net, gross = price.get("netAmount"), price.get("grossAmount")
    if tax_type == "gross" and net is not None and gross is None:
        value = (Decimal(str(net)) * (1 + rate / 100)).quantize(Decimal("0.0001"))
        price["grossAmount"] = _plain(value)
        price.pop("netAmount")
        if rate:
            notes.append(f"Position {index}: Nettopreis {net} € in brutto {_plain(value)} € "
                         "umgerechnet (der Beleg rechnet brutto).")
    elif tax_type != "gross" and gross is not None and net is None:
        value = (Decimal(str(gross)) / (1 + rate / 100)).quantize(Decimal("0.0001"))
        price["netAmount"] = _plain(value)
        price.pop("grossAmount")
        if rate:
            notes.append(f"Position {index}: Bruttopreis {gross} € in netto {_plain(value)} € "
                         "umgerechnet (der Beleg rechnet netto).")


def _plain(value: Decimal) -> int | float:
    number = float(value)
    return int(number) if number.is_integer() else number


def prepare_document(
    client: LexwareClient | None, body: dict[str, Any], kind: str
) -> dict[str, Any]:
    """Return a copy of a sales document body with safe defaults filled in.

    - dates in Lexware format; ``voucherDate`` defaults to now
    - quotations: ``expirationDate`` defaults to voucherDate + 30 days
    - ``totalPrice.currency`` EUR, ``taxConditions.taxType`` net
    - ``shippingConditions`` "none" where Lexware requires them
    - line items: unit price currency, tax rate (19 % for net/gross, else 0)
    - article line items: without ``id`` → free position (``custom``); with
      ``id`` → type taken from the article, unknown id → clear error
    """
    if not isinstance(body, dict):
        return body
    out = strip_control(copy.deepcopy(unwrap(body, "body", "document", "beleg")))
    notes: list[str] = []
    _apply_aliases(out)
    if any(k in out for k in ("id", "createdDate", "organizationId")) and out.get("voucherDate"):
        # a document read with get_* sent back as a copy: a new document is
        # dated today; its old due/reminder dates belong to the old one
        for key in ("voucherDate", "expirationDate", "dueDate", "nextReminderDate"):
            out.pop(key, None)
        out["__copy"] = True

    if out.get("voucherDate"):
        out["voucherDate"] = checked_timestamp(out["voucherDate"], "Belegdatum")
        check_not_far_future(out["voucherDate"])
    else:
        out["voucherDate"] = _format(_now())

    if kind == QUOTATION:
        if out.get("expirationDate"):
            out["expirationDate"] = checked_timestamp(out["expirationDate"], "Gültig bis")
        else:
            try:
                start = datetime.fromisoformat(out["voucherDate"])
            except (TypeError, ValueError):
                start = _now()
            out["expirationDate"] = _format(start + timedelta(days=QUOTATION_VALID_DAYS))

    total = out.get("totalPrice")
    if not isinstance(total, dict):
        out["totalPrice"] = {"currency": "EUR"}
    elif not total.get("currency"):
        total["currency"] = "EUR"
    currency = out["totalPrice"]["currency"]

    tax = out.get("taxConditions")
    raw_items = out.get("lineItems")
    if isinstance(raw_items, dict):
        raw_items = [raw_items]
    explicit = str(tax["taxType"]) if isinstance(tax, dict) and tax.get("taxType") else None
    if isinstance(raw_items, list):
        raw_items = [
            _normalize_line(i, explicit or "?", notes) if isinstance(i, dict) else i
            for i in raw_items
        ]
    if explicit is None:
        prices = [
            i["unitPrice"] for i in raw_items or []
            if isinstance(i, dict) and isinstance(i.get("unitPrice"), dict)
        ]
        gross_only = [p for p in prices if p.get("grossAmount") is not None
                      and p.get("netAmount") is None and p.get("amount") is None]
        others = [p for p in prices if p not in gross_only
                  and (p.get("netAmount") is not None or p.get("amount") is not None)]
        if gross_only and others:
            raise LexwareError(
                "Gemischte Preisangaben: Einige Positionen haben einen Bruttopreis "
                "(grossAmount), andere einen Netto- bzw. unklaren Preis. Bitte "
                "einheitlich angeben – alle netAmount (taxConditions.taxType 'net') "
                "oder alle grossAmount (taxType 'gross')."
            )
        explicit = "gross" if gross_only else "net"
        out["taxConditions"] = {**(tax if isinstance(tax, dict) else {}), "taxType": explicit}
        for item in raw_items or []:
            price = item.get("unitPrice") if isinstance(item, dict) else None
            if isinstance(price, dict) and "amount" in price:
                key = "grossAmount" if explicit == "gross" else "netAmount"
                price.setdefault(key, price.pop("amount"))
    tax_type = str(out["taxConditions"]["taxType"])
    if isinstance(raw_items, list):
        out["lineItems"] = raw_items
    if not out.get("lineItems"):
        raise LexwareError(
            "Der Beleg braucht mindestens eine Position (lineItems), z. B. "
            '[{"type": "custom", "name": "Beratung", "quantity": 1, "unitName": "Stunde", '
            '"unitPrice": {"netAmount": 95, "taxRatePercentage": 19}}].'
        )
    _due_and_delivery(out, kind, is_copy=bool(out.get("__copy")))
    notes += out.pop("__pay_notes", [])
    if isinstance(out.get("paymentConditions"), dict):
        out["paymentConditions"].pop("__autolabel", None)
        if out["paymentConditions"].get("paymentDiscountConditions", "") is None:
            out["paymentConditions"].pop("paymentDiscountConditions")
        if not out["paymentConditions"]:
            out.pop("paymentConditions")
    _check_fields(out)
    start, end = _day_of(out.get("voucherDate")), _day_of(out.get("expirationDate"))
    if start and end and end < start:
        raise LexwareError(
            f"Das Angebot wäre nur bis {end:%d.%m.%Y} gültig, also vor dem Belegdatum "
            f"{start:%d.%m.%Y}. Bitte ein späteres Gültigkeitsdatum angeben."
        )
    address = out.get("address")
    if isinstance(address, dict) and not address.get("contactId"):
        if address.get("countryCode") or address.get("country"):
            given = address.pop("country", None) or address.get("countryCode")
            address["countryCode"] = country_code(given)
        else:
            address["countryCode"] = "DE"
    # only the texts of a document read and sent back as a copy — a text the
    # caller wrote stays, even if it happens to be another type's standard text
    foreign = drop_foreign_standard_texts(out, kind) if out.get("__copy") else None
    if foreign:
        notes.append(foreign)

    if kind in _NEEDS_SHIPPING:
        ship = out.get("shippingConditions")
        if isinstance(ship, str):
            ship = {"shippingType": ship}
            out["shippingConditions"] = ship
        if isinstance(ship, dict) and ship.get("shippingType"):
            ship["shippingType"] = _SHIPPING_TYPES.get(
                str(ship["shippingType"]).strip().casefold(), ship["shippingType"]
            )
        if not isinstance(ship, dict) or not ship.get("shippingType"):
            ship = ship if isinstance(ship, dict) else {}
            if kind == INVOICE:
                # An invoice needs a date of service (§ 14 UStG); like the web
                # app, it defaults to the invoice date.
                out["shippingConditions"] = {
                    **ship,
                    "shippingType": "service",
                    "shippingDate": ship.get("shippingDate") or out["voucherDate"],
                }
            else:
                out["shippingConditions"] = {**ship, "shippingType": "none"}
    _check_dates(out)
    _complete_lines(client, out.get("lineItems") or [], kind, tax_type, currency,
                    out.get("language"), notes)
    if notes:
        out["__hinweis"] = " ".join(notes)
    return out


_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I
)


def _is_uuid(value: str) -> bool:
    return bool(_UUID_RE.match(value.strip()))


def _article_id_by_number(
    client: LexwareClient, number: str, cache: dict[str, dict[str, Any]]
) -> str:
    """Article number (any case) or exact title in a position's `id` → the id."""
    if "__all__" not in cache:
        cache["__all__"] = {"items": list(client.paginate("/v1/articles"))}
    wanted = number.strip().casefold()
    by_number = [a for a in cache["__all__"]["items"]
                 if str(a.get("articleNumber") or "").casefold() == wanted]
    by_title = [a for a in cache["__all__"]["items"]
                if fold(str(a.get("title") or "")) == fold(number)]
    # an exact article number wins over an article that happens to be TITLED so
    hits = list({a["id"]: a for a in (by_number or by_title)}.values())
    if not hits:
        needle = fold(number)
        near = [a for a in cache["__all__"]["items"]
                if needle and (needle in fold(str(a.get("title") or ""))
                               or needle in fold(str(a.get("articleNumber") or "")))][:5]
        hint = ("; ".join(f"{a.get('articleNumber')} „{a.get('title')}“" for a in near))
        raise LexwareError(
            f"Artikel '{number}' nicht gefunden (gesucht als Artikelnummer und Bezeichnung). "
            + (f"Ähnliche Artikel: {hint}. Bitte Artikelnummer oder id angeben."
               if hint else "Bitte in der Artikelliste suchen.")
        )
    if len(hits) > 1:
        listing = "; ".join(f"{a.get('articleNumber')} „{a.get('title')}“ (id {a.get('id')})"
                            for a in hits[:6])
        raise LexwareError(f"Mehrere Artikel passen zu '{number}': {listing}. "
                           "Bitte die Artikel-id angeben.")
    cache[hits[0]["id"]] = hits[0]
    return str(hits[0]["id"])


def _article_id_by_name(
    client: LexwareClient, name: str, cache: dict[str, dict[str, Any]]
) -> str | None:
    """Article id for an exact (ä = ae, any case) title — only if unique."""
    wanted = fold(name)
    if "__all__" not in cache:
        # one scan per document, not per position
        try:
            cache["__all__"] = {"items": list(client.paginate("/v1/articles"))}
        except LexwareError:
            cache["__all__"] = {"items": []}
    hits = [
        a for a in cache["__all__"]["items"]
        if fold(str(a.get("title") or "")) == wanted
    ]
    if len(hits) != 1:
        return None
    cache[hits[0]["id"]] = hits[0]
    return str(hits[0]["id"])


# -- Bookkeeping vouchers (/v1/vouchers) -------------------------------------

_VOUCHER_KINDS = {
    "eingangsrechnung": "purchaseinvoice", "einkauf": "purchaseinvoice",
    "ausgabe": "purchaseinvoice", "lieferantenrechnung": "purchaseinvoice",
    "purchase invoice": "purchaseinvoice", "purchaseinvoice": "purchaseinvoice",
    "ausgangsrechnung": "salesinvoice", "verkauf": "salesinvoice", "einnahme": "salesinvoice",
    "sales invoice": "salesinvoice", "salesinvoice": "salesinvoice", "invoice": "salesinvoice",
    "rechnung": "salesinvoice",
    "gutschrift": "salescreditnote", "verkaufsgutschrift": "salescreditnote",
    "creditnote": "salescreditnote", "salescreditnote": "salescreditnote",
    "einkaufsgutschrift": "purchasecreditnote", "lieferantengutschrift": "purchasecreditnote",
    "purchasecreditnote": "purchasecreditnote",
    "quittung": "purchaseinvoice", "kassenbon": "purchaseinvoice", "bon": "purchaseinvoice",
    "kassenbeleg": "purchaseinvoice", "receipt": "purchaseinvoice",
    "tankquittung": "purchaseinvoice", "ausgabebeleg": "purchaseinvoice",
}
_RECEIPT_WORDS = {"quittung", "kassenbon", "bon", "kassenbeleg", "receipt", "tankquittung",
                  "barbeleg", "barquittung"}
_RATE_KEYS = ("taxRatePercent", "steuersatz", "taxRate", "mwst", "MwSt", "ust", "vat",
              "umsatzsteuer", "steuer", "tax", "taxRatePercentage")
_VOUCHER_FIELDS = frozenset({
    "type", "voucherStatus", "voucherNumber", "voucherDate", "shippingDate", "dueDate",
    "totalGrossAmount", "totalTaxAmount", "taxType", "useCollectiveContact", "contactId",
    "contactName", "remark", "voucherItems", "files",
})
_VOUCHER_ITEM_FIELDS = frozenset({"amount", "taxAmount", "taxRatePercent", "categoryId"})
_COMMON_RATES = (0, 5, 7, 16, 19)


def _tax_rate(value: Any) -> Any:
    """A tax RATE – never a tax amount ("7,00 €") or an odd number like 3.04."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str) and re.search(r"€|eur", value, re.I):
        raise LexwareError(
            f"„{value}“ sieht nach einem Steuerbetrag aus – bitte den Steuersatz angeben "
            "(z. B. 7 oder 19); der Steuerbetrag wird berechnet."
        )
    rate = to_number(rate_word(value), field="Steuersatz", kind="percent")
    if isinstance(rate, (int, float)) and rate not in _COMMON_RATES:
        raise LexwareError(
            f"Steuersatz {rate} ist ungewöhnlich – ist das ein Steuerbetrag? Bitte den "
            "Steuersatz in Prozent angeben (0, 7 oder 19)."
        )
    return rate

_CENT = Decimal("0.01")
# Rounding slack between the caller's total and the sum of the items.
_TOTAL_TOLERANCE = Decimal("0.02")


def _dec(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return Decimal(str(value).replace(",", "."))
    except (InvalidOperation, ValueError):
        return None


def _money(value: Decimal) -> float:
    return float(value.quantize(_CENT, rounding=ROUND_HALF_UP))


def prepare_voucher(body: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of a voucher body with consistent tax amounts.

    Lexware recomputes each item's tax from amount, rate and ``taxType``
    (gross: amounts include tax, net: amounts exclude it) and rejects any
    difference (``invalid_taxamount`` / ``invalid_total_taxamount``). We do the
    same calculation up front. The amounts themselves are never changed: if
    the caller's ``totalGrossAmount`` does not fit the items, a clear error
    explains it instead.
    """
    if not isinstance(body, dict):
        return body
    out = strip_control(copy.deepcopy(unwrap(body, "body", "voucher", "beleg")))
    type_word = str(out.get("type") or out.get("art") or out.get("belegart") or "").strip()
    for alias, target in (("date", "voucherDate"), ("datum", "voucherDate"),
                          ("number", "voucherNumber"), ("belegnummer", "voucherNumber"),
                          ("items", "voucherItems"), ("positions", "voucherItems"),
                          ("total", "totalGrossAmount"), ("amount", "totalGrossAmount"),
                          ("contact_id", "contactId"), ("vendorId", "contactId"),
                          ("customerId", "contactId"), ("betrag", "totalGrossAmount"),
                          ("summe", "totalGrossAmount"), ("gesamtbetrag", "totalGrossAmount"),
                          ("rechnungsdatum", "voucherDate"), ("belegdatum", "voucherDate"),
                          ("rechnungsnummer", "voucherNumber"), ("nummer", "voucherNumber"),
                          ("art", "type"), ("belegart", "type"), ("voucherType", "type"),
                          ("steuerart", "taxType"), ("faelligkeit", "dueDate"),
                          ("fälligkeit", "dueDate"), ("bemerkung", "remark"),
                          ("notiz", "remark"), ("faellig", "dueDate"), ("fällig", "dueDate"),
                          ("zahlbar_bis", "dueDate"), ("faellig_am", "dueDate"),
                          ("lieferdatum", "shippingDate"), ("leistungsdatum", "shippingDate"),
                          ("positionen", "voucherItems"), ("netto", "__net"),
                          ("betrag_netto", "__net"), ("nettobetrag", "__net"),
                          ("status", "voucherStatus")):
        if alias in out and target not in out:
            out[target] = out.pop(alias)
    # supplier/customer as a name → resolved to a contact later (services)
    supplier_word = any(k in out for k in ("lieferant", "supplier", "haendler", "händler",
                                            "vendor"))
    for alias in ("lieferant", "supplier", "haendler", "händler", "vendor", "kunde",
                  "customer", "shop", "geschaeft", "geschäft"):
        if alias in out and "contactName" not in out and "contactId" not in out:
            out["contactName"] = out.pop(alias)
    status = str(out.get("voucherStatus") or "").strip().casefold()
    if status:
        mapped = {"offen": "open", "open": "open", "zu prüfen": "unchecked",
                  "zu pruefen": "unchecked", "ungeprüft": "unchecked",
                  "ungeprueft": "unchecked", "prüfen": "unchecked",
                  "unchecked": "unchecked"}.get(status)
        if mapped:
            out["voucherStatus"] = mapped
        else:
            # e.g. "paid" of a voucher read and sent back: Lexware sets it itself
            out["__hinweis"] = (
                f"Status „{out.pop('voucherStatus')}“ nicht übernommen – Lexware bucht den "
                "Beleg offen und setzt den Status selbst (Zahlung per Bankabgleich oder in "
                "Lexware „als bezahlt markieren“).")
    # a receipt with one amount: build the single item
    for key in ("bezahlt", "paid", "zahlungsart", "bar", "paymentMethod", "zahlung"):
        if key in out:
            raise LexwareError(
                "Eine Zahlung lässt sich über die Schnittstelle nicht erfassen – der Beleg "
                "bleibt offen, bis die Zahlung in Lexware (z. B. per Bankabgleich oder "
                "„als bezahlt markieren“) zugeordnet ist. Bitte das Feld weglassen."
            )
    net = out.pop("__net", None)
    for key in ("betrag", "totalGrossAmount"):
        if isinstance(out.get(key), str):
            rest, word = split_gross_net(out[key])
            if word == "net" and net is None:
                net = rest
                out.pop(key)
            elif word:
                out[key] = rest
    category = _first(out, "kategorie", "category", "categoryId")
    rate = _tax_rate(_first(out, *_RATE_KEYS))  # 0 is a valid rate
    if _first(out, "steuerfrei", "taxFree", "taxfree") in (True, "ja", "true", 1):
        rate = 0
    if net is not None and not out.get("voucherItems") and category:
        # a net amount: the item is net, the total is computed (never "net = gross")
        out.pop("totalGrossAmount", None)
        out["voucherItems"] = [{"amount": net, "taxRatePercent": rate if rate is not None
                                else 19, "categoryId": category}]
        out["taxType"] = "net"
    if not out.get("voucherItems") and out.get("totalGrossAmount") is not None and category:
        out["voucherItems"] = [{
            "amount": out["totalGrossAmount"],
            "taxRatePercent": rate if rate is not None else 19,
            "categoryId": category,
        }]
        out.setdefault("taxType", "gross")
    items = out.get("voucherItems")
    if isinstance(items, dict):
        out["voucherItems"] = items = [items]
    item_sides: list[str] = []  # "net"/"gross" as written in the positions
    for item in items or []:
        if not isinstance(item, dict):
            continue
        if category and not item.get("categoryId") and not item.get("kategorie") \
                and not item.get("category"):
            item["categoryId"] = category  # the voucher's category for every position
        net_value = gross_value = None
        for alias in ("netto", "betrag_netto", "nettobetrag", "net", "amount_net",
                      "netAmount"):
            if alias in item:
                net_value = item.pop(alias)
        for alias in ("brutto", "betrag_brutto", "bruttobetrag", "gross", "amount_gross",
                      "grossAmount"):
            if alias in item:
                gross_value = item.pop(alias)
        if isinstance(item.get("amount"), str):
            rest, word = split_gross_net(item["amount"])
            if word:
                item_sides.append(word)
                item["amount"] = rest
        for alias in _RATE_KEYS:
            if alias in item and alias != "taxRatePercent" and "taxRatePercent" not in item:
                item["taxRatePercent"] = item.pop(alias)
        if "taxRatePercent" in item:
            item["taxRatePercent"] = _tax_rate(item["taxRatePercent"])
        if item.pop("steuerfrei", None) in (True, "ja", "true", 1):
            item["taxRatePercent"] = 0
        if net_value is not None and gross_value is not None:
            # both given: fine when they fit the rate — the side of the voucher wins
            net_n = to_number(net_value, field="netto", kind="money")
            gross_n = to_number(gross_value, field="brutto", kind="money")
            rate_n = item.get("taxRatePercent")
            if not isinstance(rate_n, (int, float)):
                raise LexwareError(
                    f"Position mit netto {net_n} und brutto {gross_n}: bitte den Steuersatz "
                    "dazu angeben – oder nur einen der beiden Beträge."
                )
            if abs(net_n * (1 + rate_n / 100) - gross_n) > 0.02:
                raise LexwareError(
                    f"Netto {net_n} und Brutto {gross_n} passen bei {rate_n} % nicht "
                    "zusammen – bitte nur einen Betrag angeben (netto ODER brutto)."
                )
            side = _TAX_TYPES.get(str(out.get("taxType") or "").strip().casefold(), "gross")
            item.setdefault("amount", net_n if side == "net" else gross_n)
            item_sides.append("net" if side == "net" else "gross")
        elif net_value is not None:
            item_sides.append("net")
            item.setdefault("amount", net_value)
        elif gross_value is not None:
            item_sides.append("gross")
            item.setdefault("amount", gross_value)
        for alias, target in (("betrag", "amount"),
                              ("category", "categoryId"), ("kategorie", "categoryId"),
                              ("steuerbetrag", "taxAmount"), ("tax_amount", "taxAmount")):
            if alias in item and target not in item:
                item[target] = item.pop(alias)
        bad = sorted(k for k in item if k not in _VOUCHER_ITEM_FIELDS)
        if bad:
            raise LexwareError(
                f"Unbekannte Felder in einer Belegposition: {', '.join(bad)}. Erlaubt sind "
                "amount (Betrag), taxRatePercent (Steuersatz), categoryId (Kategorie, auch "
                "als Name), taxAmount."
            )
    for key, label in (("voucherDate", "Belegdatum"), ("shippingDate", "Lieferdatum"),
                       ("dueDate", "Fälligkeit")):
        if isinstance(out.get(key), str) and out[key].strip():
            day = _parse_day(out[key]) or _day_of(checked_timestamp(out[key], label))
            if day is not None:
                out[key] = day.isoformat()
    check_not_far_future(out.get("voucherDate"), " Buchhaltungsbelege werden sofort gebucht.")
    if out.get("type"):
        word = str(out["type"]).strip().casefold()
        out["type"] = _VOUCHER_KINDS.get(word, out["type"])
        if word in ("gutschrift", "creditnote") and supplier_word:
            out["type"] = "purchasecreditnote"  # a credit note FROM a supplier
        elif word in ("gutschrift", "creditnote"):
            out["__generic_credit"] = True  # decided by the category (services)
        if out["type"] not in set(_VOUCHER_KINDS.values()):
            raise LexwareError(
                f"Unbekannte Belegart '{body.get('type')}'. Möglich: purchaseinvoice "
                "(Eingangsrechnung, Quittung, Kassenbon), salesinvoice (Ausgangsrechnung), "
                "salescreditnote (Gutschrift an Kunden), purchasecreditnote "
                "(Gutschrift vom Lieferanten)."
            )
    for key in ("id", "organizationId", "createdDate", "updatedDate", "version", "hinweis"):
        out.pop(key, None)  # read-only (a voucher read with get_* sent back)
    if type_word.casefold() in _RECEIPT_WORDS:
        out["__receipt"] = True
    unknown = sorted(k for k in out if k not in _VOUCHER_FIELDS and not k.startswith("__"))
    if unknown:
        raise LexwareError(
            f"Unbekannte Felder im Buchhaltungsbeleg: {', '.join(unknown)} – sie würden "
            "ignoriert. Erlaubt sind u. a.: type, voucherNumber, voucherDate, dueDate "
            "(fällig), shippingDate, totalGrossAmount (Betrag), taxType (brutto/netto), "
            "contactId oder lieferant/contactName, voucherItems bzw. kategorie + "
            "steuersatz, remark, voucherStatus."
        )
    if not out.get("voucherDate") and out.get("voucherStatus") != "unchecked":
        out["voucherDate"] = _now().date().isoformat()
    for key in ("totalGrossAmount", "totalTaxAmount"):
        if key in out:
            out[key] = to_number(out[key], field=key, kind="money")
    for item in out.get("voucherItems") or []:
        if isinstance(item, dict):
            for key in ("amount", "taxAmount", "taxRatePercent"):
                if key in item:
                    item[key] = _round(to_number(
                        item[key], field=f"voucherItems.{key}",
                        kind="percent" if key == "taxRatePercent" else "money"), 2)
            if str(out.get("type", "")).endswith("creditnote") and isinstance(
                    item.get("amount"), (int, float)) and item["amount"] < 0:
                item["amount"] = -item["amount"]  # Lexware wants positive amounts
    if str(out.get("type", "")).endswith("creditnote") and isinstance(
            out.get("totalGrossAmount"), (int, float)) and out["totalGrossAmount"] < 0:
        out["totalGrossAmount"] = -out["totalGrossAmount"]
    raw_tax = str(out.get("taxType") or "").strip().casefold()
    sides = set(item_sides)
    if len(sides) > 1:
        raise LexwareError(
            "Die Positionen mischen Netto- und Bruttobeträge – bitte einheitlich angeben "
            "(alle netto oder alle brutto)."
        )
    if raw_tax:
        out["taxType"] = tax_type_word(out.get("taxType"))
        if sides and out["taxType"] in _TAXED_TYPES and sides != {out["taxType"]}:
            side = "Nettobeträge (netto)" if "net" in sides else "Bruttobeträge (brutto)"
            raise LexwareError(
                f"Widerspruch: Die Positionen haben {side}, der Beleg aber taxType "
                f"'{out['taxType']}' – bitte einheitlich angeben."
            )
    elif sides:
        out["taxType"] = sides.pop()
    else:
        try:
            guessed = _guess_tax_type(out)
        except LexwareError as exc:
            missing = voucher_missing(out)
            if missing:  # say everything at once
                raise LexwareError(
                    "Für den Buchhaltungsbeleg fehlt: " + "; ".join(missing)
                    + "; taxType ('gross' = Beträge inkl. Steuer, 'net' = ohne)."
                ) from exc
            raise
        if guessed:
            out["taxType"] = guessed
    tax_type = str(out.get("taxType") or "").lower()
    items = out.get("voucherItems")
    if tax_type not in _TAXED_TYPES or not isinstance(items, list) or not items:
        return out

    total_tax = Decimal(0)
    total_gross = Decimal(0)
    for item in items:
        if not isinstance(item, dict):
            return out
        amount = _dec(item.get("amount"))
        rate = _dec(item.get("taxRatePercent"))
        if amount is None or rate is None:
            return out  # incomplete — let Lexware name the missing field
        if tax_type == "gross":
            tax = (amount * rate / (100 + rate)).quantize(_CENT, rounding=ROUND_HALF_UP)
            gross = amount
        else:
            tax = (amount * rate / 100).quantize(_CENT, rounding=ROUND_HALF_UP)
            gross = amount + tax
        given_tax = _dec(item.get("taxAmount"))
        if given_tax is not None and abs(given_tax - tax) <= _CENT:
            tax = given_tax  # line-rounded receipts may differ by a cent
            if tax_type == "net":
                gross = amount + tax
        item["taxAmount"] = _money(tax)
        total_tax += tax
        total_gross += gross

    given = _dec(out.get("totalGrossAmount"))
    if given is not None and abs(given - total_gross) > _TOTAL_TOLERANCE:
        kind = "brutto (inkl. Steuer)" if tax_type == "gross" else "netto (ohne Steuer)"
        raise LexwareError(
            f"Die Positionen ergeben {_money(total_gross):.2f} € brutto, als "
            f"Gesamtbetrag ist aber {_money(given):.2f} € angegeben. Bei "
            f"taxType „{tax_type}“ sind die Positionsbeträge {kind}. Bitte die "
            "Beträge bzw. taxType prüfen."
        )
    if given is not None and _money(given) != _money(total_gross):
        out["__hinweis"] = " ".join(n for n in (out.get("__hinweis"), (
            f"Gesamtbetrag laut Angabe {_money(given):.2f} €, gebucht "
            f"{_money(total_gross):.2f} € (Summe der Positionen, Rundungsdifferenz).")) if n)
    out["totalGrossAmount"] = _money(total_gross)
    out["totalTaxAmount"] = _money(total_tax)
    return out


def voucher_missing(body: dict[str, Any]) -> list[str]:
    """Required bookkeeping-voucher fields that are still missing."""
    missing: list[str] = []
    if str(body.get("voucherStatus") or "").casefold() == "unchecked":
        return missing  # Lexware: number/date/items optional for "zu prüfen"
    if not body.get("voucherItems") and body.get("totalGrossAmount") is not None:
        missing.append("kategorie (Buchungskategorie für den Betrag, z. B. „Bürobedarf“)")
    if not body.get("type"):
        missing.append("type (Eingangsrechnung/purchaseinvoice, Ausgangsrechnung/"
                       "salesinvoice, Gutschrift …)")
    if not body.get("voucherNumber"):
        missing.append("voucherNumber (Belegnummer, z. B. Rechnungs- oder Quittungsnummer)")
    if not body.get("voucherItems") and body.get("totalGrossAmount") is None:
        missing.append("voucherItems (Positionen mit amount, taxRatePercent, categoryId) "
                       "– oder einfach totalGrossAmount + kategorie für einen einfachen Beleg")
    return missing


def _guess_tax_type(body: dict[str, Any]) -> str | None:
    """taxType from the total: gross if the items add up to it, net if items
    plus tax do. Without a total there is nothing to tell them apart."""
    items = body.get("voucherItems")
    total = _dec(body.get("totalGrossAmount"))
    amounts: list[tuple[Decimal, Decimal]] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        amount, rate = _dec(item.get("amount")), _dec(item.get("taxRatePercent"))
        if amount is None or rate is None:
            amounts = []
            break
        amounts.append((amount, rate))
    if not amounts:
        return None  # nothing to calculate with — Lexware names what is missing
    if total is not None:
        gross_sum = sum(a for a, _ in amounts)
        net_sum = sum(a + (a * r / 100).quantize(_CENT, rounding=ROUND_HALF_UP)
                      for a, r in amounts)
        if abs(gross_sum - total) > _TOTAL_TOLERANCE and abs(net_sum - total) > _TOTAL_TOLERANCE:
            raise LexwareError(
                f"Der Gesamtbetrag {total} passt weder zur Bruttosumme der Positionen "
                f"({_money(gross_sum):.2f}) noch zu Netto + Steuer ({_money(net_sum):.2f}). "
                "Bitte Beträge prüfen bzw. taxType (gross/net) angeben."
            )
        gross = gross_sum
        net = sum(a + (a * r / 100).quantize(_CENT, rounding=ROUND_HALF_UP) for a, r in amounts)
        if abs(gross - total) <= _TOTAL_TOLERANCE:
            return "gross"
        if abs(net - total) <= _TOTAL_TOLERANCE:
            return "net"
    raise LexwareError(
        "taxType fehlt: 'gross' (Positionsbeträge inkl. Umsatzsteuer, z. B. Quittung "
        "über 119 €) oder 'net' (Beträge ohne Steuer). Mit totalGrossAmount wird es "
        "automatisch erkannt."
    )


def issue_keys(exc: LexwareAPIError) -> set[str]:
    """The ``i18nKey`` values of a Lexware IssueList error body."""
    body = exc.body
    issues = body.get("IssueList") if isinstance(body, dict) else None
    if not isinstance(issues, list):
        return set()
    return {str(i.get("i18nKey")) for i in issues if isinstance(i, dict) and i.get("i18nKey")}
