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


def _parse_day(value: str) -> date | None:
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
        cache[article_id] = _ARTICLE_LINE_TYPES.get(
            str(article.get("type") or "").upper(), "material"
        )
    return cache[article_id]


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
    if not isinstance(tax, dict) or not tax.get("taxType"):
        out["taxConditions"] = {**(tax if isinstance(tax, dict) else {}), "taxType": "net"}
    tax_type = str(out["taxConditions"]["taxType"])
    default_rate = DEFAULT_TAX_RATE if tax_type in _TAXED_TYPES else 0

    if kind in _NEEDS_SHIPPING:
        ship = out.get("shippingConditions")
        if not isinstance(ship, dict) or not ship.get("shippingType"):
            out["shippingConditions"] = {
                **(ship if isinstance(ship, dict) else {}),
                "shippingType": "none",
            }
    ship = out.get("shippingConditions")
    if isinstance(ship, dict):
        for key in ("shippingDate", "shippingEndDate"):
            if ship.get(key):
                ship[key] = document_timestamp(ship[key])

    article_types: dict[str, str] = {}
    for item in out.get("lineItems") or []:
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type") or "")
        if item_type in ("material", "service"):
            article_id = str(item.get("id") or "").strip()
            if not article_id:
                item["type"] = "custom"
                item.pop("id", None)
            elif client is not None:
                item["type"] = _article_line_type(client, article_id, article_types)
        if item.get("type") == "text":
            continue
        price = item.get("unitPrice")
        if isinstance(price, dict):
            price.setdefault("currency", currency)
            if price.get("taxRatePercentage") is None:
                price["taxRatePercentage"] = default_rate
    return out


# -- Bookkeeping vouchers (/v1/vouchers) -------------------------------------

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


def issue_keys(exc: LexwareAPIError) -> set[str]:
    """The ``i18nKey`` values of a Lexware IssueList error body."""
    body = exc.body
    issues = body.get("IssueList") if isinstance(body, dict) else None
    if not isinstance(issues, list):
        return set()
    return {str(i.get("i18nKey")) for i in issues if isinstance(i, dict) and i.get("i18nKey")}
