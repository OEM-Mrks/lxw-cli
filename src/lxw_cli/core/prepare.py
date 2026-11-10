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
from lxw_cli.core.normalize import country_code, fold, to_number

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
        if not m:
            return None
        d, mo, y = (int(g) for g in m.groups())
    try:
        return date(y, mo, d)
    except ValueError:
        return None


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
    return _format(parsed)


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
    "footer": "remark",
}
_CONTACT_KEYS = ("contactId", "customerId", "contact_id", "customer_id", "kundeId")
_LINE_ALIASES = {
    "title": "name", "bezeichnung": "name", "text": "name",
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


def _apply_aliases(out: dict[str, Any]) -> None:
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
    customer = out.pop("customer", None) or out.pop("kunde", None)
    if customer and not out.get("address"):
        out["address"] = (
            customer if isinstance(customer, dict) else {"name": str(customer)}
        )


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
    for alias, target in _LINE_ALIASES.items():
        if alias in item and target not in item:
            item[target] = item.pop(alias)
    price = item.get("unitPrice")
    if not isinstance(price, dict):
        price = {"amount": price} if price is not None else {}
    for alias, target in _LINE_PRICE_KEYS.items():
        if alias in item:
            price.setdefault(target, item.pop(alias))
    for alias, target in list(_LINE_PRICE_KEYS.items()):
        if alias in price and alias != target:
            price.setdefault(target, price.pop(alias))
    if "amount" in price:
        amount = price.pop("amount")
        key = "grossAmount" if tax_type == "gross" else "netAmount"
        price.setdefault(key, amount)
    for key in ("netAmount", "grossAmount", "taxRatePercentage"):
        if key in price:
            price[key] = to_number(price[key], field=f"unitPrice.{key}")
    if price:
        item["unitPrice"] = price
    for key in ("quantity", "discountPercentage"):
        if key in item:
            item[key] = to_number(item[key], field=key)
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
    out = copy.deepcopy(body)
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
    if not isinstance(tax, dict) or not tax.get("taxType"):
        # only gross prices given → the caller meant gross
        prices = [
            i.get("unitPrice") for i in raw_items or []
            if isinstance(i, dict) and isinstance(i.get("unitPrice"), dict)
        ]
        only_gross = bool(prices) and all(
            p.get("grossAmount") is not None and p.get("netAmount") is None for p in prices
        )
        out["taxConditions"] = {
            **(tax if isinstance(tax, dict) else {}),
            "taxType": "gross" if only_gross else "net",
        }
    tax_type = str(out["taxConditions"]["taxType"])
    if isinstance(raw_items, list):
        out["lineItems"] = [
            _normalize_line(i, tax_type) if isinstance(i, dict) else i for i in raw_items
        ]
    if not out.get("lineItems"):
        raise LexwareError(
            "Der Beleg braucht mindestens eine Position (lineItems), z. B. "
            '[{"type": "custom", "name": "Beratung", "quantity": 1, "unitName": "Stunde", '
            '"unitPrice": {"netAmount": 95, "taxRatePercentage": 19}}].'
        )
    _due_and_delivery(out, kind)
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
    out = copy.deepcopy(body)
    for alias, target in (("date", "voucherDate"), ("datum", "voucherDate"),
                          ("number", "voucherNumber"), ("belegnummer", "voucherNumber"),
                          ("items", "voucherItems"), ("positions", "voucherItems"),
                          ("total", "totalGrossAmount"), ("amount", "totalGrossAmount"),
                          ("contact_id", "contactId"), ("vendorId", "contactId"),
                          ("customerId", "contactId")):
        if alias in out and target not in out:
            out[target] = out.pop(alias)
    for key in ("voucherDate", "shippingDate", "dueDate"):
        if isinstance(out.get(key), str):
            day = _parse_day(out[key]) or _day_of(document_timestamp(out[key]))
            if day is not None:
                out[key] = day.isoformat()
    if out.get("type"):
        word = str(out["type"]).strip().casefold()
        out["type"] = _VOUCHER_KINDS.get(word, out["type"])
    raw_tax = str(out.get("taxType") or "").strip().casefold()
    if raw_tax:
        out["taxType"] = _TAX_TYPES.get(raw_tax, out.get("taxType"))
    else:
        guessed = _guess_tax_type(out)
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
                item[key] = to_number(item[key], field=f"voucherItems.{key}")
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
