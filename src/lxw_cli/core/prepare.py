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
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

from lxw_cli.core.errors import LexwareAPIError, LexwareError
from lxw_cli.core.normalize import country_code, fold, strip_control, to_number

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
DEFAULT_TAX_RATE = 19

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


def _parse_day(value: str) -> date | None:
    word = value.strip().casefold()
    if word in _RELATIVE_DAYS:
        return _now().date() + timedelta(days=_RELATIVE_DAYS[word])
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
    # Day and time as written count (no time-zone shift — "2026-11-10T23:30Z"
    # stays the 10th, not 00:30 on the 11th in Berlin).
    return _format(parsed.replace(tzinfo=None))


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
    "nextReminderDate", "eInvoiceProfile", "hinweis",
})
_LINE_FIELDS = frozenset({
    "id", "type", "name", "description", "quantity", "unitName", "unitPrice",
    "discountPercentage", "lineItemAmount", "optional", "alternative", "subItems",
})
_DISCOUNT_KEYS = ("rabatt_prozent", "rabattProzent", "gesamtrabatt", "rabatt",
                  "discount", "discountPercentage", "totalDiscountPercentage")
_CUSTOMER_KEYS = ("customer", "kunde", "kunde_name", "kundenname", "customerName",
                  "customer_name", "empfaenger", "empfänger", "recipient")
_ZIP_RE = re.compile(r"\b(\d{5})\s+([^,]+)$")
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
}
_SHIPPING_TYPES = {
    "lieferung": "delivery", "lieferdatum": "delivery", "delivery": "delivery",
    "leistung": "service", "leistungsdatum": "service", "service": "service",
    "leistungszeitraum": "serviceperiod", "serviceperiod": "serviceperiod",
    "lieferzeitraum": "deliveryperiod", "deliveryperiod": "deliveryperiod",
    "keine": "none", "ohne": "none", "none": "none",
}
_DAYS_RE = re.compile(r"(\d+)\s*(tag|tage|days?|d)\b", re.I)


def _days(value: Any) -> Any:
    """"14 Tage" / "netto 30 days" / "14" → 14 (else unchanged)."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    if isinstance(value, str):
        m = _DAYS_RE.search(value) or re.fullmatch(r"\s*(\d+)\s*", value)
        if m:
            return int(m.group(1))
    return value


def _lower_keys(out: dict[str, Any], known: Any) -> None:
    """"Positionen", "Titel", "VoucherDate" → the spelling the tables use."""
    lookup = {k.casefold(): k for k in known}
    for key in list(out):
        canon = lookup.get(key.casefold())
        if canon and canon != key and canon not in out:
            out[canon] = out.pop(key)


def _apply_aliases(out: dict[str, Any]) -> None:
    _lower_keys(out, [*_DOC_ALIASES, *_DOC_FIELDS, *_CUSTOMER_KEYS, *_CONTACT_KEYS,
                      *_DISCOUNT_KEYS, "kundennummer", "customerNumber"])
    for key in ("kundennummer", "customerNumber"):
        if key in out:
            out.setdefault("customer", out.pop(key))
    if isinstance(out.get("language"), str):
        out["language"] = {"deutsch": "de", "german": "de", "englisch": "en",
                           "english": "en"}.get(out["language"].strip().casefold(),
                                                out["language"])
    for alias, target in _DOC_ALIASES.items():
        if alias in out and target not in out:
            out[target] = out.pop(alias)
        elif alias in out and alias != target:
            out.pop(alias)
    for key in _CONTACT_KEYS:
        if key in out:
            value = out.pop(key)
            address = out.get("address") if isinstance(out.get("address"), dict) else {}
            if value and not address.get("contactId"):
                out["address"] = {**address, "contactId": value}
    customer = None
    for key in _CUSTOMER_KEYS:
        if key in out:
            customer = customer or out.pop(key)
            out.pop(key, None)
    if customer and not out.get("address"):
        out["address"] = _customer_address(customer)
    for key in _DISCOUNT_KEYS:
        if key in out:
            value = out.pop(key)
            total = out.get("totalPrice") if isinstance(out.get("totalPrice"), dict) else {}
            out["totalPrice"] = {
                **total,
                "totalDiscountPercentage": to_number(value, field="totalDiscountPercentage"),
            }


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
    if len(parts) >= 2:
        m = _ZIP_RE.search(parts[-1])
        if m:
            address: dict[str, Any] = {"name": parts[0], "zip": m.group(1),
                                       "city": m.group(2).strip()}
            if len(parts) >= 3:
                address["street"] = parts[1]
            return address
    return {"contactId": text}


def _due_and_delivery(out: dict[str, Any], kind: str) -> None:
    """dueDate → paymentTermDuration, deliveryDate → shippingConditions."""
    due = out.pop("dueDate", None) or out.pop("faelligkeit", None) or out.pop("fälligkeit", None)
    terms = out.get("paymentConditions")
    if isinstance(terms, str):
        days = _days(terms)
        terms = {"paymentTermLabel": terms}
        if isinstance(days, int):
            terms["paymentTermDuration"] = days
        out["paymentConditions"] = terms
    for key in ("paymentTermDuration", "zahlungsziel", "paymentTerms"):
        if key in out:
            days = _days(out.pop(key))
            if isinstance(days, int):
                block = _block(out, "paymentConditions")
                out["paymentConditions"] = {**block, "paymentTermDuration": days}
    if isinstance(out.get("paymentConditions"), dict):
        block = out["paymentConditions"]
        if "paymentTermDuration" in block:
            block["paymentTermDuration"] = _days(block["paymentTermDuration"])
        if "paymentTermLabel" not in block and isinstance(block.get("paymentTermDuration"), int):
            n = block["paymentTermDuration"]
            block["paymentTermLabel"] = "Zahlbar sofort, rein netto" if n == 0 else (
                f"Zahlbar innerhalb von {n} Tagen ohne Abzug"
            )
    if due and kind == INVOICE:
        start = _day_of(out.get("voucherDate"))
        end = _day_of(document_timestamp(due))
        if start and end and end >= start:
            n = (end - start).days
            block = _block(out, "paymentConditions")
            out["paymentConditions"] = {
                **block,
                "paymentTermDuration": n,
                "paymentTermLabel": block.get("paymentTermLabel")
                or (
                    "Zahlbar sofort, rein netto"
                    if n == 0
                    else f"Zahlbar bis {end:%d.%m.%Y} ohne Abzug"
                ),
            }
    delivery = None
    for key in ("deliveryDate", "shippingDate", "lieferdatum", "leistungsdatum", "serviceDate"):
        if key in out:
            delivery = out.pop(key)
    if delivery and kind in _NEEDS_SHIPPING:
        ship = _block(out, "shippingConditions")
        if not ship.get("shippingDate"):
            out["shippingConditions"] = {
                "shippingType": ship.get("shippingType")
                or ("delivery" if kind == DELIVERY_NOTE else "service"),
                **ship,
                "shippingDate": delivery,
            }


def prepare_changes(changes: dict[str, Any], kind: str, tax_type: str) -> dict[str, Any]:
    """Normalize adjustments for a follow-up document (continue_document)."""
    out = strip_control(copy.deepcopy(changes))
    _apply_aliases(out)
    if out.get("voucherDate"):
        out["voucherDate"] = document_timestamp(out["voucherDate"])
    if isinstance(out.get("lineItems"), (list, dict)):
        items = out["lineItems"] if isinstance(out["lineItems"], list) else [out["lineItems"]]
        out["lineItems"] = [
            _normalize_line(i, tax_type) if isinstance(i, dict) else i for i in items
        ]
        for item in out["lineItems"]:
            if isinstance(item, dict) and item.get("type") != "text":
                item.setdefault("quantity", 1)
                if item.get("type") == "custom":
                    item.setdefault("unitName", "Stück")
                price = item.get("unitPrice")
                if isinstance(price, dict):
                    price.setdefault("currency", "EUR")
                    price.setdefault("taxRatePercentage", DEFAULT_TAX_RATE)
    known = kind in (INVOICE, ORDER_CONFIRMATION, DELIVERY_NOTE)
    _due_and_delivery(out, kind if known else INVOICE)
    ship = out.get("shippingConditions")
    if isinstance(ship, dict):
        for key in ("shippingDate", "shippingEndDate"):
            if ship.get(key):
                ship[key] = document_timestamp(ship[key])
    _check_fields(out)
    return out


def _check_fields(out: dict[str, Any]) -> None:
    """Unknown fields raise — Lexware would drop them silently (e.g. a
    validity date under a made-up name) and the document comes out wrong."""
    unknown = sorted(k for k in out if k not in _DOC_FIELDS)
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
        bad = sorted(k for k in item if k not in _LINE_FIELDS)
        if bad:
            raise LexwareError(
                f"Unbekannte Felder in Position {index}: {', '.join(bad)}. Erlaubt sind: "
                "type, id (Artikel), name, description, quantity, unitName, unitPrice "
                "{netAmount|grossAmount, taxRatePercentage}, discountPercentage."
            )


def _block(out: dict[str, Any], key: str) -> dict[str, Any]:
    value = out.get(key)
    return value if isinstance(value, dict) else {}


def _day_of(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value).date()
    except ValueError:
        return None


def _round(value: Any, places: int) -> Any:
    """Lexware takes at most 4 (prices, quantity) resp. 2 (discount) decimals."""
    if isinstance(value, float):
        rounded = float(Decimal(str(value)).quantize(Decimal(1).scaleb(-places),
                                                     rounding=ROUND_HALF_UP))
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


def _normalize_line(item: dict[str, Any], tax_type: str) -> dict[str, Any]:
    """Aliases and number formats of one line item (no API calls)."""
    item = dict(item)
    _lower_keys(item, [*_LINE_ALIASES, *_LINE_FIELDS, *_LINE_PRICE_KEYS])
    for alias, target in _LINE_ALIASES.items():
        if alias in item and target not in item:
            item[target] = item.pop(alias)
    for key in ("articleNumber", "artikelnummer", "article_number", "artikelNr"):
        if key in item:
            number = item.pop(key)
            item.setdefault("id", str(number))  # resolved to the article id later
    price = item.get("unitPrice")
    if not isinstance(price, dict):
        price = {"amount": price} if price is not None else {}
    for alias, target in _LINE_PRICE_KEYS.items():
        if alias in item:
            price.setdefault(target, item.pop(alias))
    for alias, target in list(_LINE_PRICE_KEYS.items()):
        if alias in price and alias != target:
            price.setdefault(target, price.pop(alias))
    if "amount" in price and tax_type in ("gross", "net", "vatfree"):
        amount = price.pop("amount")
        key = "grossAmount" if tax_type == "gross" else "netAmount"
        price.setdefault(key, amount)
    for key in ("netAmount", "grossAmount", "taxRatePercentage", "amount"):
        if key in price:
            price[key] = _round(to_number(price[key], field=f"unitPrice.{key}"), 4)
    if price:
        item["unitPrice"] = price
    for key, places in (("quantity", 4), ("discountPercentage", 2)):
        if key in item:
            item[key] = _round(to_number(item[key], field=key), places)
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
    out = strip_control(copy.deepcopy(body))
    _apply_aliases(out)

    if out.get("voucherDate"):
        out["voucherDate"] = document_timestamp(out["voucherDate"])
    else:
        out["voucherDate"] = _format(_now())

    if kind == QUOTATION:
        if out.get("expirationDate"):
            out["expirationDate"] = document_timestamp(out["expirationDate"])
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
    if isinstance(tax, str):
        tax = {"taxType": tax}
        out["taxConditions"] = tax
    if isinstance(tax, dict) and tax.get("taxType"):
        tax["taxType"] = _TAX_TYPES.get(str(tax["taxType"]).strip().casefold(), tax["taxType"])
    raw_items = out.get("lineItems")
    if isinstance(raw_items, dict):
        raw_items = [raw_items]
    explicit = str(tax["taxType"]) if isinstance(tax, dict) and tax.get("taxType") else None
    if isinstance(raw_items, list):
        raw_items = [
            _normalize_line(i, explicit or "?") if isinstance(i, dict) else i
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
    _due_and_delivery(out, kind)
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
    default_rate = DEFAULT_TAX_RATE if tax_type in _TAXED_TYPES else 0

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
    ship = out.get("shippingConditions")
    if isinstance(ship, dict):
        for key in ("shippingDate", "shippingEndDate"):
            if ship.get(key):
                ship[key] = document_timestamp(ship[key])

    articles: dict[str, dict[str, Any]] = {}
    for item in out.get("lineItems") or []:
        if not isinstance(item, dict):
            continue
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
                if not item.get("description") and article.get("description"):
                    item["description"] = article["description"]
                if not isinstance(item.get("unitPrice"), dict) or (
                    item["unitPrice"].get("netAmount") is None
                    and item["unitPrice"].get("grossAmount") is None
                ):
                    from_article = _price_from_article(article, tax_type)
                    if from_article:
                        item["unitPrice"] = {**from_article, **(item.get("unitPrice") or {})}
        if item.get("type") == "text":
            continue
        item.setdefault("quantity", 1)
        if item.get("type") == "custom" and not item.get("unitName"):
            item["unitName"] = "Stück"
        price = item.get("unitPrice")
        if not isinstance(price, dict) and kind == DELIVERY_NOTE:
            zero_key = "grossAmount" if tax_type == "gross" else "netAmount"
            price = item["unitPrice"] = {zero_key: 0}
        if isinstance(price, dict):
            price.setdefault("currency", currency)
            if price.get("taxRatePercentage") is None:
                price["taxRatePercentage"] = default_rate
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
    hits = [a for a in cache["__all__"]["items"]
            if str(a.get("articleNumber") or "").casefold() == wanted]
    if not hits:
        hits = [a for a in cache["__all__"]["items"]
                if fold(str(a.get("title") or "")) == fold(number)]
    if len(hits) != 1:
        raise LexwareError(
            f"Artikel '{number}' nicht eindeutig gefunden (gesucht als Artikelnummer und "
            "Bezeichnung). Bitte die Artikel-id aus der Artikelliste verwenden."
        )
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
    out = strip_control(copy.deepcopy(body))
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
                          ("notiz", "remark")):
        if alias in out and target not in out:
            out[target] = out.pop(alias)
    # supplier/customer as a name → resolved to a contact later (services)
    for alias in ("lieferant", "supplier", "haendler", "händler", "vendor", "kunde",
                  "customer", "shop", "geschaeft", "geschäft"):
        if alias in out and "contactName" not in out and "contactId" not in out:
            out["contactName"] = out.pop(alias)
    # a receipt with one amount: build the single item
    category = (out.pop("kategorie", None) or out.pop("category", None)
                or out.pop("categoryId", None))
    rate = (out.pop("taxRatePercent", None) or out.pop("steuersatz", None)
            or out.pop("taxRate", None))
    if not out.get("voucherItems") and out.get("totalGrossAmount") is not None and category:
        out["voucherItems"] = [{
            "amount": out["totalGrossAmount"],
            "taxRatePercent": rate if rate is not None else 19,
            "categoryId": category,
        }]
        out.setdefault("taxType", "gross")
    for key in ("voucherDate", "shippingDate", "dueDate"):
        if isinstance(out.get(key), str):
            day = _parse_day(out[key]) or _day_of(document_timestamp(out[key]))
            if day is not None:
                out[key] = day.isoformat()
    if out.get("type"):
        word = str(out["type"]).strip().casefold()
        out["type"] = _VOUCHER_KINDS.get(word, out["type"])
        if out["type"] not in set(_VOUCHER_KINDS.values()):
            raise LexwareError(
                f"Unbekannte Belegart '{body.get('type')}'. Möglich: purchaseinvoice "
                "(Eingangsrechnung, Quittung, Kassenbon), salesinvoice (Ausgangsrechnung), "
                "salescreditnote (Gutschrift an Kunden), purchasecreditnote "
                "(Gutschrift vom Lieferanten)."
            )
    if not out.get("voucherDate"):
        out["voucherDate"] = _now().date().isoformat()
    raw_tax = str(out.get("taxType") or "").strip().casefold()
    if raw_tax:
        out["taxType"] = _TAX_TYPES.get(raw_tax, out.get("taxType"))
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
    for key in ("totalGrossAmount", "totalTaxAmount"):
        if key in out:
            out[key] = to_number(out[key], field=key)
    items = out.get("voucherItems")
    if isinstance(items, dict):
        out["voucherItems"] = items = [items]
    for item in items or []:
        if not isinstance(item, dict):
            continue
        for alias, target in (("taxRate", "taxRatePercent"), ("vat", "taxRatePercent"),
                              ("steuersatz", "taxRatePercent"), ("betrag", "amount"),
                              ("category", "categoryId"), ("kategorie", "categoryId")):
            if alias in item and target not in item:
                item[target] = item.pop(alias)
        for key in ("amount", "taxAmount", "taxRatePercent"):
            if key in item:
                item[key] = _round(to_number(item[key], field=f"voucherItems.{key}"), 2)
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
    out["totalGrossAmount"] = _money(total_gross)
    out["totalTaxAmount"] = _money(total_tax)
    return out


def voucher_missing(body: dict[str, Any]) -> list[str]:
    """Required bookkeeping-voucher fields that are still missing."""
    missing: list[str] = []
    if not body.get("type"):
        missing.append("type (Eingangsrechnung/purchaseinvoice, Ausgangsrechnung/"
                       "salesinvoice, Gutschrift …)")
    if not body.get("voucherNumber"):
        missing.append("voucherNumber (Belegnummer, z. B. Rechnungs- oder Quittungsnummer)")
    if not body.get("voucherItems"):
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
        gross = sum(a for a, _ in amounts)
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
