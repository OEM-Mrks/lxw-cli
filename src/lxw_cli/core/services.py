"""UI-agnostic operations on the Lexware Office API.

Pure logic shared by every frontend (CLI, MCP server, TUI): identifier
resolution, endpoint fallbacks, client-side filtering/search and the
limit/archived helpers. No Typer, no Rich, no print — functions return data and
raise :class:`LexwareError` subclasses on failure.
"""

from __future__ import annotations

import mimetypes
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from lxw_cli.core.constants import (
    ALL_VOUCHER_TYPES,
    DEFAULT_VOUCHER_STATUSES,
    DEFAULT_VOUCHER_TYPES,
    INVOICE_LIKE_TYPES,
    VOUCHER_TYPE_LABELS,
)
from lxw_cli.core.errors import LexwareAPIError, LexwareError
from lxw_cli.core.limits import validate_article, validate_contact, validate_voucher
from lxw_cli.core.models import ListResult
from lxw_cli.core.normalize import (
    CONTACT_KIND_KEYS,
    bool_word,
    contact_kind,
    expand_voucher_type_word,
    fold,
    lead_from_given,
    normalize_article,
    normalize_contact,
    normalize_voucher_status,
    unwrap,
)
from lxw_cli.core.prepare import (
    DELIVERY_NOTE,
    INVOICE,
    ORDER_CONFIRMATION,
    QUOTATION,
    issue_keys,
    prepare_document,
    prepare_voucher,
    voucher_missing,
)

if TYPE_CHECKING:
    from lxw_cli.core.client import LexwareClient

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def looks_like_uuid(value: str) -> bool:
    return bool(_UUID_RE.match(value.strip()))


def require_contact_id(contact_id: str) -> str:
    """Check a contact id before it reaches Lexware.

    Lexware answers a shortened or otherwise malformed id only with a bare
    HTTP 400 ("Invalid value"). Say clearly what is expected instead.
    """
    value = contact_id.strip()
    if not looks_like_uuid(value):
        raise LexwareError(
            f"Ungültige Kontakt-ID '{value}': erwartet wird die vollständige ID "
            "(36 Zeichen, z. B. 8c026ea9-0000-4000-8000-000000000000). "
            "Den Kontakt bitte zuerst über die Kontaktliste suchen "
            "(list_contacts mit name oder number) und dessen id verwenden."
        )
    return value


def resolve_contact_id(client: LexwareClient, value: str) -> str:
    """Contact UUID from a UUID, a customer/vendor number or a unique name.

    Models often pass what they see ("10022", "K-10022", "Musterbau GmbH")
    instead of the id. A number is looked up exactly, a name via the contact
    search; only an unambiguous hit is used, otherwise the candidates are named.
    """
    text = str(value or "").strip()
    if looks_like_uuid(text):
        return text
    if not text or (
        re.fullmatch(r"[0-9a-fA-F]{8}(-[0-9a-fA-F]{0,12}){0,4}", text)
        and re.search(r"[a-fA-F]", text)
        and re.search(r"\d", text)
    ):
        # empty, or a cut-off/garbled id like "8c026ea9" — not a name
        return require_contact_id(text)
    digits = re.sub(r"\D", "", text)
    if digits and re.fullmatch(r"[A-Za-z]{0,3}[-\s]?\d+", text):
        rows = list(client.paginate("/v1/contacts", params={"number": digits}, max_items=3))
        if len(rows) == 1:
            return rows[0]["id"]
    if "@" in text:
        rows = _list_contacts_raw(
            client, name=None, email=text, number=None, customer=False, vendor=False,
            include_archived=False, limit=3, fetch_all=False,
        ).items
        exact = [r for r in rows if text.casefold() in {
            str(e).casefold() for vals in (r.get("emailAddresses") or {}).values()
            for e in (vals or [])}]
        if len(rows) == 1:
            return rows[0]["id"]
        if len(exact) == 1:
            return exact[0]["id"]
    rows = list_contacts(client, name=text, limit=10).items
    exact = [r for r in rows if fold(_contact_label(r)) == fold(text) or (
        r.get("person") and fold(f"{r['person'].get('lastName') or ''} "
                                 f"{r['person'].get('firstName') or ''}") == fold(text))]
    if len(exact) == 1:
        return exact[0]["id"]
    if exact:
        raise LexwareError(_several_named(text, exact))
    if rows:
        listing = "; ".join(f"{_contact_label(r)} (id {r.get('id')})" for r in rows)
        lead = (f"Die E-Mail '{text}' gehört zu mehreren Kontakten" if "@" in text
                else f"Kein Kontakt heißt genau '{text}'. Ähnlich")
        raise LexwareError(f"{lead}: {listing}. Bitte die id des richtigen Kontakts verwenden.")
    raise LexwareError(
        f"Kein Kontakt zu '{text}' gefunden (gesucht als id, Kunden-/Lieferantennummer "
        "und Name). Bitte den Kontakt über die Kontaktsuche finden und seine id verwenden."
    )


def _contact_label(contact: dict[str, Any]) -> str:
    company = contact.get("company") or {}
    person = contact.get("person") or {}
    name = company.get("name") or " ".join(
        p for p in (person.get("firstName"), person.get("lastName")) if p
    )
    return name or str(contact.get("id"))


def resolve_max_items(limit: int, fetch_all: bool = False) -> int | None:
    """Translate a --limit/--all pair into paginate()'s `max_items`.

    Returns ``None`` (fetch every page) when `fetch_all` is set or `limit` is
    0, 25 for a negative `limit` (a slip), otherwise the positive `limit`.
    Centralized so every list command — CLI and MCP — interprets "give me all
    of them" the same way.
    """
    if fetch_all or limit == 0:
        return None
    if limit < 0:
        return 25  # a negative limit is a slip, not "everything"
    return limit


def archived_param(include_archived: bool) -> str | None:
    """voucherlist `archived` value.

    The /v1/voucherlist endpoint filters archived server-side: `archived=false`
    returns only non-archived vouchers, while omitting the parameter returns
    both. So we send 'false' to hide archived by default and None (dropped by
    the client) to include everything on request.
    """
    return None if include_archived else "false"


def resolve_voucher_id(
    client: LexwareClient, identifier: str, voucher_type: str
) -> str:
    """Resolve a voucher identifier (UUID or voucher number) to its UUID.

    If `identifier` already looks like a UUID it is returned unchanged.
    Otherwise the value is treated as a voucher number and looked up via
    /v1/voucherlist with the given `voucher_type` filter. Raises
    LexwareError on no match or ambiguous matches.
    """
    return resolve_voucher(client, identifier, voucher_type)[0]


def resolve_voucher(
    client: LexwareClient,
    identifier: str,
    voucher_type: str,
    *,
    prefer: str | None = None,
) -> tuple[str, str | None]:
    """Like :func:`resolve_voucher_id`, but also return the voucherlist type.

    The type is only known when `identifier` is a voucher number (the
    voucherlist row carries it); for a UUID it is ``None``.
    """
    match = find_voucher(client, identifier, voucher_type, prefer=prefer)
    return match.id, match.type


@dataclass
class VoucherMatch:
    """Result of :func:`find_voucher`.

    `row` is the voucherlist entry (``None`` for a bare UUID), `note` a German
    hint for the caller when the number had to be interpreted (prefix added,
    several vouchers with the same number) — tools attach it as ``hinweis``.
    """

    id: str
    type: str | None = None
    row: dict[str, Any] | None = None
    note: str | None = None
    ambiguous: bool = False  # several vouchers share the number
    rows: list[dict[str, Any]] | None = None  # all vouchers that matched


# Documents written in Lexware's invoicing part. When a number exists twice,
# such a document wins over a bookkeeping voucher with the same number (that
# is usually a supplier's invoice or an imported copy that happens to share it).
_INVOICING_TYPES = frozenset(
    {"invoice", "downpaymentinvoice", "creditnote", "quotation",
     "orderconfirmation", "deliverynote"}
)
# Bookkeeping vouchers (/v1/vouchers) — the only ones files can be attached to.
BOOKKEEPING_TYPES = DEFAULT_VOUCHER_TYPES
_LOOKUP_STATUSES = DEFAULT_VOUCHER_STATUSES  # incl. unchecked
# How many vouchers with the same number we look at (more never happens in
# practice; enough to list them all in a hint).
_MAX_SAME_NUMBER = 10
# Number shapes like "RE0006", "AG-2026-17", "GS0001": letters/prefix, digits,
# optional suffix.
_NUMBER_SHAPE_RE = re.compile(r"^(\D*?)(\d+)(\D*)$")


def _type_label(voucher_type: str | None) -> str:
    return VOUCHER_TYPE_LABELS.get(voucher_type or "", voucher_type or "Beleg")


def _types_label(voucher_type: str) -> str:
    labels = [_type_label(t.strip()) for t in voucher_type.split(",") if t.strip()]
    return ", ".join(dict.fromkeys(labels))


_WORD_PREFIX = re.compile(
    r"^(?:eingangsrechnung|lieferantenrechnung|ausgangsrechnung|rechnung(?:snummer)?|"
    r"angebot(?:snummer)?|auftrag(?:sbest[äa]tigung)?|"
    r"lieferschein|gutschrift|rechnungskorrektur|beleg(?:nummer)?|nr\.?|nummer|"
    r"no\.?|invoice|quotation|order)\s*[:#]?\s+",
    re.I,
)


_TYPE_WORD = re.compile(
    r"^(eingangsrechnung|lieferantenrechnung|ausgangsrechnung|rechnungskorrektur|"
    r"auftragsbest[äa]tigung|rechnung|angebot|auftrag|lieferschein|"
    r"gutschrift|invoice|quotation|order)\w*\s*[:#]?\s+", re.I)
_WORD_TYPES = {
    "rechnung": "invoice,salesinvoice,downpaymentinvoice,purchaseinvoice",
    "invoice": "invoice,salesinvoice,downpaymentinvoice,purchaseinvoice",
    "ausgangsrechnung": "invoice,salesinvoice,downpaymentinvoice",
    "eingangsrechnung": "purchaseinvoice", "lieferantenrechnung": "purchaseinvoice",
    "angebot": "quotation", "quotation": "quotation",
    "auftrag": "orderconfirmation", "auftragsbestätigung": "orderconfirmation",
    "auftragsbestaetigung": "orderconfirmation", "order": "orderconfirmation",
    "lieferschein": "deliverynote",
    "gutschrift": "creditnote,salescreditnote,purchasecreditnote",
    "rechnungskorrektur": "creditnote",
}
# "ER 4711", "AG 109": the usual abbreviations, tried when nothing else matched.
_SHORT_WORD = re.compile(r"^(ER|RE|AG|AB|LS|GS|RK)\s*[:#]?\s+(\S.*)$", re.I)
_SHORT_TYPES = {"er": "eingangsrechnung", "re": "rechnung", "ag": "angebot",
                "ab": "auftrag", "ls": "lieferschein", "gs": "gutschrift",
                "rk": "rechnungskorrektur"}
_INVISIBLE = re.compile(r"[\u200b-\u200f\u2060\ufeff]")


def _exact(rows: list[dict[str, Any]], number: str) -> list[dict[str, Any]]:
    """Rows whose number IS the number (any case, blanks ignored)."""
    wanted = re.sub(r"\s+", "", number).casefold()
    return [r for r in rows if not r.get("voucherNumber")  # nothing to compare
            or re.sub(r"\s+", "", str(r["voucherNumber"])).casefold() == wanted]


def _clean_number_keep_words(value: Any) -> str:
    import unicodedata

    text = _INVISIBLE.sub("", unicodedata.normalize("NFKC", str(value or "")))
    return text.strip().strip("'\"„“‚‘`").strip()


def _clean_number(value: Any) -> str:
    """"Rechnung RE0016" → "RE0016"; quotes, invisible characters and blanks
    removed, other digit scripts (full-width, Arabic-Indic) made ASCII."""
    import unicodedata

    text = _INVISIBLE.sub("", unicodedata.normalize("NFKC", str(value if value is not None
                                                                else "")))
    text = re.sub(r"\d", lambda m: str(unicodedata.digit(m.group(), 0)), text)
    text = text.strip().strip("'\"„“‚‘`").strip()
    return _WORD_PREFIX.sub("", text).strip()


def _lookup_number(
    client: LexwareClient, number: str, voucher_type: str
) -> list[dict[str, Any]]:
    if not number.strip():
        return []
    return list(
        client.paginate(
            "/v1/voucherlist",
            params={
                "voucherType": voucher_type,
                # + unchecked: bookkeeping vouchers lacking data (e.g. created
                # from an upload) — must stay findable by number for attaching.
                "voucherStatus": _LOOKUP_STATUSES,
                "voucherNumber": number,
            },
            max_items=_MAX_SAME_NUMBER,
        )
    )


def _describe_row(row: dict[str, Any]) -> str:
    parts = [f"{_type_label(row.get('voucherType'))} {row.get('voucherNumber') or ''}".strip()]
    date = str(row.get("voucherDate") or "")[:10]
    if date:
        parts.append(f"vom {date}")
    if row.get("contactName"):
        parts.append(str(row["contactName"]))
    if row.get("totalAmount") is not None:
        parts.append(f"{row['totalAmount']} {row.get('currency') or 'EUR'}")
    if row.get("voucherStatus"):
        parts.append(f"Status {row['voucherStatus']}")
    parts.append(f"id {row.get('id')}")
    return ", ".join(parts)


def _rank(row: dict[str, Any], prefer: set[str]) -> tuple[int, int, int, int]:
    """Lower is better: preferred type, not voided, not a draft, invoicing doc."""
    vtype = row.get("voucherType")
    status = row.get("voucherStatus")
    return (
        0 if (not prefer or vtype in prefer) else 1,
        1 if status == "voided" else 0,
        1 if status in ("draft", "unchecked") else 0,
        0 if vtype in _INVOICING_TYPES else 1,
    )


def _pick(
    rows: list[dict[str, Any]], identifier: str, prefer: str | None
) -> tuple[dict[str, Any], str | None]:
    """Choose one voucher from several with the same number, or explain.

    Raises when the ranking cannot tell them apart — then the model gets the
    candidates with their ids and can ask again with the right one.
    """
    if len(rows) == 1:
        return rows[0], None
    wanted = {t.strip() for t in (prefer or "").split(",") if t.strip()}
    ranked = sorted(rows, key=lambda r: _rank(r, wanted))
    best, runner_up = ranked[0], ranked[1]
    if _rank(best, wanted) == _rank(runner_up, wanted):
        listing = "; ".join(_describe_row(r) for r in ranked)
        raise LexwareError(
            f"Mehrere Belege mit der Nummer '{identifier}' ({len(rows)}): {listing}. "
            "Bitte den gewünschten Beleg über seine id abrufen oder die Belegart dazuschreiben "
            "(z. B. „Rechnung 1“, „Angebot 1“)."
        )
    others = "; ".join(_describe_row(r) for r in ranked[1:])
    note = (
        f"Die Nummer '{identifier}' gibt es {len(rows)}-mal. Verwendet: "
        f"{_describe_row(best)}. Ebenfalls vorhanden: {others}."
    )
    return best, note


def _number_candidates(identifier: str, sample: list[dict[str, Any]]) -> list[str]:
    """Full voucher numbers the account uses that end in the digits given.

    "2616" with numbers like "RE2610" in the account → "RE2616"; zero padding
    is taken over ("6" with "RE0005" → "RE0006"). At most three shapes, most
    frequent first (at most six), so a miss costs only a few extra requests.
    """
    digits = identifier.lstrip("0") or "0"
    shapes: dict[tuple[str, int, str], int] = {}
    for row in sample:
        m = _NUMBER_SHAPE_RE.match(str(row.get("voucherNumber") or ""))
        if not m or not m.group(1):
            continue
        key = (m.group(1), len(m.group(2)), m.group(3))
        shapes[key] = shapes.get(key, 0) + 1
    out: list[str] = []
    # plain document prefixes (RE, AG, LS …) first, then the most frequent rest
    ranked = sorted(shapes.items(), key=lambda kv: (
        0 if re.fullmatch(r"[A-Za-z]{1,4}", kv[0][0]) else 1, -kv[1]))
    for (prefix, width, suffix), _count in ranked:
        candidate = f"{prefix}{digits.zfill(width)}{suffix}"
        if candidate not in out:
            out.append(candidate)
        if len(out) == 6:
            break
    return out


def find_voucher(
    client: LexwareClient,
    identifier: str,
    voucher_type: str,
    *,
    prefer: str | None = None,
) -> VoucherMatch:
    """Resolve a voucher number (or UUID) to the voucher it means.

    Exact number first (Lexware matches case-insensitively). If that fails:
    without inner spaces ("RE 2616"), and for a number without letters the
    account's own number shapes ("2616" → "RE2616"). Several vouchers with
    one number: the best fit wins (`prefer` types, not voided, not a draft,
    invoicing document before bookkeeping voucher) and `note` names the others;
    a tie raises with the list of candidates.
    """
    original = str(identifier or "").strip()
    word = _TYPE_WORD.match(_clean_number_keep_words(original))
    if word:
        # The word ("Rechnung 40", "Angebot 109") limits the search to its
        # types — in every tool, also when the caller has its own preference.
        # "Rechnung" includes supplier invoices (they are invoices too).
        word_types = _WORD_TYPES.get(word.group(1).casefold(), "")
        wanted = [t for t in word_types.split(",") if t]
        if voucher_type not in ("any", ALL_VOUCHER_TYPES):
            limited = [t for t in voucher_type.split(",") if t in wanted]
        else:
            limited = wanted
        if limited:
            voucher_type = ",".join(limited)
            own = [t for t in (prefer or "").split(",") if t in limited]
            prefer = ",".join(own) if own else ",".join(limited)
    identifier = _clean_number(identifier)
    if not identifier:
        # An empty voucherNumber filter makes Lexware return EVERY voucher —
        # never let that pick "the newest one" (e.g. dunning a random invoice).
        raise LexwareError(
            "Bitte eine Belegnummer (z. B. RE0006) oder eine Beleg-id angeben – das "
            "Feld war leer."
        )
    if looks_like_uuid(identifier):
        return VoucherMatch(id=identifier)
    rows_all = _lookup_number(client, identifier, voucher_type)
    rows = _exact(rows_all, identifier)
    # Lexware also answers "4" with "ST2-V-ER-4": such endings only count
    # when no voucher has the number itself or as a short number (RE0004).
    loose = [r for r in rows_all if r not in rows]
    used = identifier
    note: str | None = None
    if not rows and re.search(r"\s", identifier):
        compact = re.sub(r"\s+", "", identifier)
        found_compact = _lookup_number(client, compact, voucher_type)
        rows = _exact(found_compact, compact)
        if rows:
            used = compact
    sample: list[dict[str, Any]] | None = None
    split = re.fullmatch(r"([A-Za-z]{1,4})[\s._/-]*0*(\d+)", identifier)
    if not rows and split and not identifier.isdigit():
        sample = list(
            client.paginate(
                "/v1/voucherlist",
                params={"voucherType": voucher_type, "voucherStatus": _LOOKUP_STATUSES},
                max_items=250,
            )
        )
        prefix = split.group(1).casefold()
        shaped = [r for r in sample if str(r.get("voucherNumber") or "").casefold()
                  .startswith(prefix)]
        found: dict[str, dict[str, Any]] = {}
        for candidate in _number_candidates(split.group(2), shaped):
            for row in _exact(_lookup_number(client, candidate, voucher_type), candidate):
                found.setdefault(str(row.get("id")), row)
        rows = list(found.values())
        if rows:
            numbers = ", ".join(dict.fromkeys(str(r.get("voucherNumber")) for r in rows))
            note = f"Die Nummer '{identifier}' wurde als {numbers} gelesen."
            used = numbers if len(rows) == 1 else identifier
    if not rows and identifier.isdigit():
        sample = list(
            client.paginate(
                "/v1/voucherlist",
                params={"voucherType": voucher_type, "voucherStatus": _LOOKUP_STATUSES},
                max_items=250,
            )
        )
        found: dict[str, dict[str, Any]] = {}
        for candidate in _number_candidates(identifier, sample):
            for row in _exact(_lookup_number(client, candidate, voucher_type), candidate):
                found.setdefault(str(row.get("id")), row)
        rows = list(found.values())
        if rows:
            numbers = ", ".join(dict.fromkeys(str(r.get("voucherNumber")) for r in rows))
            note = f"Die Nummer '{identifier}' wurde als {numbers} gelesen."
            used = numbers if len(rows) == 1 else identifier
    short = _SHORT_WORD.match(original)
    if not rows and short and not word:
        try:
            return find_voucher(
                client, f"{_SHORT_TYPES[short.group(1).casefold()]} {short.group(2)}",
                voucher_type, prefer=prefer)
        except LexwareError:
            pass
    if not rows and loose:
        rows = loose
        numbers = ", ".join(dict.fromkeys(str(r.get("voucherNumber")) for r in rows))
        note = f"Keine Belegnummer lautet genau '{identifier}'; gefunden: {numbers}."
    if not rows:
        raise LexwareError(_not_found_message(client, identifier, voucher_type, sample))
    best, pick_note = _pick(rows, used, prefer)
    note = " ".join(n for n in (note, pick_note) if n) or None
    wanted = {t.strip() for t in (prefer or "").split(",") if t.strip()}
    decided = bool(wanted) and best.get("voucherType") in wanted and sum(
        1 for r in rows if r.get("voucherType") in wanted
    ) == 1
    return VoucherMatch(
        id=best["id"], type=best.get("voucherType"), row=best, note=note,
        ambiguous=len(rows) > 1 and not decided, rows=rows,
    )


def _not_found_message(
    client: LexwareClient,
    identifier: str,
    voucher_type: str,
    sample: list[dict[str, Any]] | None,
) -> str:
    text = (
        f"Kein Beleg mit Nummer '{identifier}' gefunden (gesucht: "
        f"{_types_label(voucher_type)}). Gemeint ist die Belegnummer aus "
        "Lexware, keine Bestell-, Transaktions- oder Lieferantennummer."
    )
    if sample is None:
        try:
            sample = list(
                client.paginate(
                    "/v1/voucherlist",
                    params={"voucherType": voucher_type, "voucherStatus": _LOOKUP_STATUSES},
                    max_items=25,
                )
            )
        except LexwareError:
            sample = []
    examples = list(
        dict.fromkeys(str(r["voucherNumber"]) for r in sample if r.get("voucherNumber"))
    )[:4]
    if examples:
        text += f" Nummern in dieser Installation sehen z. B. so aus: {', '.join(examples)}."
    return text + (
        " Die Belegliste (nach Kontakt, Status oder Art gefiltert) zeigt die richtige Nummer."
    )


# Where Lexware serves a voucher's details, by voucherlist type. Bookkeeping
# vouchers (sales*/purchase*) live under /v1/vouchers; documents written in
# Lexware's invoicing part each have their own endpoint — an id from the
# voucherlist answers 404 everywhere else. Order = probing order for a bare
# UUID (most common first).
_DETAIL_PATHS: dict[str, str] = {
    "invoice": "/v1/invoices/{id}",
    "salesinvoice": "/v1/vouchers/{id}",
    "downpaymentinvoice": "/v1/down-payment-invoices/{id}",
    "salescreditnote": "/v1/vouchers/{id}",
    "purchaseinvoice": "/v1/vouchers/{id}",
    "purchasecreditnote": "/v1/vouchers/{id}",
    "creditnote": "/v1/credit-notes/{id}",
    "quotation": "/v1/quotations/{id}",
    "orderconfirmation": "/v1/order-confirmations/{id}",
    "deliverynote": "/v1/delivery-notes/{id}",
}


def get_voucher_details(
    client: LexwareClient, identifier: str, voucher_type: str
) -> dict[str, Any]:
    """Fetch a voucher's details from whichever endpoint holds its type.

    `voucher_type` is the comma-separated voucherlist filter (e.g.
    INVOICE_LIKE_TYPES). A voucher number tells us the type, so its endpoint
    is asked first; a UUID is probed against the endpoints of the allowed
    types, then of all others (only 404 moves on). A voucher of another type
    than asked for is still returned — with a `hinweis` saying what it is.
    """
    try:
        match = locate_voucher(client, identifier, voucher_type)
    except LexwareError:
        dunning = _dunning_by_number(client, identifier)
        if dunning is None:
            raise
        return dunning
    allowed = [t.strip() for t in voucher_type.split(",")]
    paths = [p for t, p in _DETAIL_PATHS.items() if t in allowed]
    if match.type in _DETAIL_PATHS:
        paths.insert(0, _DETAIL_PATHS[match.type])
    others = [p for t, p in _DETAIL_PATHS.items() if t not in allowed]
    for path in dict.fromkeys(paths + others):
        try:
            doc = client.get(path.format(id=match.id))
        except LexwareAPIError as exc:
            if exc.status_code != 404:
                raise
            continue
        note = match.note
        if match.type is None and path in others and path not in paths:
            kind = next(t for t, p in _DETAIL_PATHS.items() if p == path)
            note = _join(note, _other_type_note(doc.get("voucherNumber") or identifier,
                                                kind, voucher_type))
        row = match.row or {}
        if row.get("voucherStatus") == "overdue" and doc.get("voucherStatus") == "open":
            note = _join(note, "Laut Belegliste ist der Beleg überfällig "
                               f"(fällig seit {str(row.get('dueDate') or '')[:10]}).")
        return _with_note(doc, note)
    try:
        # a dunning id (dunnings have no list entry) — only when any type is asked
        doc = client.get(f"/v1/dunnings/{match.id}") if voucher_type == ALL_VOUCHER_TYPES \
            else None
    except LexwareAPIError as exc:
        if exc.status_code not in (400, 404):
            raise
        doc = None
    if doc is not None:
        return _with_note(doc, "Das ist eine Mahnung – Rechnungsdaten und Zahlungsstand "
                               "stehen bei der gemahnten Rechnung.")
    raise LexwareError(
        f"Unter '{identifier.strip()}' gibt es in Lexware keinen Beleg (gesucht als "
        "Rechnung, Abschlagsrechnung, Rechnungskorrektur, Angebot, Auftrag, "
        "Lieferschein und Buchhaltungsbeleg). Bitte Nummer oder id prüfen."
    )


_DUNNING_NUMBER = re.compile(r"^(.+?)_(\d{1,2})$")


def _dunning_by_number(client: LexwareClient, identifier: str) -> dict[str, Any] | None:
    """"RE0003_1" is a dunning, not "no voucher" (dunnings are not in the list)."""
    text = _clean_number(identifier)
    m = _DUNNING_NUMBER.match(text)
    if not m:
        return None
    try:
        ref = _dunning_ref(client, text)
    except LexwareError:
        return None
    doc = client.get(f"/v1/dunnings/{ref.id}")
    out = {**doc, "voucherNumber": doc.get("voucherNumber") or ref.number,
           "voucherType": "dunning"}
    return _with_note(out, f"{ref.number} ist eine Mahnung zur Rechnung {ref.invoice} – "
                           f"Zahlungsstand siehe Rechnung {ref.invoice}.")


def _join(*parts: str | None) -> str | None:
    return " ".join(p for p in parts if p) or None


def _other_type_note(number: str, found: str | None, asked: str) -> str:
    return (
        f"{number} ist ein Beleg der Art {_type_label(found)} "
        f"(gesucht war: {_types_label(asked)}). Gezeigt wird dieser Beleg."
    )


def locate_voucher(
    client: LexwareClient, identifier: str, voucher_type: str, *, prefer: str | None = None
) -> VoucherMatch:
    """find_voucher, and if the number is another type, find it there (with note)."""
    try:
        return find_voucher(client, identifier, voucher_type, prefer=prefer)
    except LexwareError as first:
        if voucher_type == ALL_VOUCHER_TYPES or looks_like_uuid(identifier.strip()):
            raise
        try:
            match = find_voucher(client, identifier, ALL_VOUCHER_TYPES)
        except LexwareError:
            raise first from None
    number = (match.row or {}).get("voucherNumber") or identifier.strip()
    match.note = _join(match.note, _other_type_note(number, match.type, voucher_type))
    return match


def _with_note(data: Any, note: str | None) -> Any:
    """Attach a resolver hint (``hinweis``) to a dict result."""
    if note and isinstance(data, dict):
        merged = " ".join(t for t in (note, data.get("hinweis")) if t)
        return {**data, "hinweis": merged}
    return data


_NO_CUSTOMER = ("Bitte einen Kunden angeben (kunde: Name, Kundennummer oder id) – oder eine "
                "Einmaladresse, z. B. address {\"name\": \"Barverkauf\"}.")
_EU_COUNTRIES = frozenset(
    "AT BE BG CY CZ DK EE ES FI FR GR HR HU IE IT LT LU LV MT NL PL PT RO SE SI SK".split())
# What Lexware needs for the special tax types (its own 406 does not say).
_TAX_PREREQUISITES = {
    "intraCommunitySupply": (
        "Innergemeinschaftliche Lieferung setzt voraus: eigene USt-IdNr. in den "
        "Lexware-Einstellungen, Beleg an einen Kontakt (kunde/contactId) mit USt-IdNr. und "
        "einem EU-Land außer DE, Steuersatz 0 % bei allen Positionen und "
        "Lieferbedingungen (shippingConditions, z. B. Lieferdatum)."),
    "thirdPartyCountryDelivery": (
        "Ausfuhr/Drittland-Lieferung setzt voraus: Kunde bzw. Adresse mit einem Land "
        "außerhalb der EU, Steuersatz 0 % bei allen Positionen und ein Lieferdatum "
        "(shippingConditions)."),
    "thirdPartyCountryService": (
        "Drittland-Leistung setzt voraus: Kunde bzw. Adresse mit einem Land außerhalb der "
        "EU, Steuersatz 0 % bei allen Positionen und ein Leistungsdatum."),
    "constructionService13b": (
        "§13b-Bauleistung setzt voraus: Kunde ist ein Unternehmer (Firmenkontakt, "
        "kunde/contactId), Steuersatz 0 % bei allen Positionen, Leistungsdatum."),
    "externalService13b": (
        "§13b (ausländischer Leistender) betrifft Leistungen eines ausländischen "
        "Unternehmers; Voraussetzung: Kontakt mit Land außerhalb DE, Steuersatz 0 % bei "
        "allen Positionen."),
    "vatfree": (
        "Steuerfrei setzt voraus: steuerfreie Belege sind für die Firma erlaubt (z. B. "
        "Kleinunternehmer in den Lexware-Einstellungen), Steuersatz 0 % bei allen Positionen."),
    "photovoltaicEquipment": (
        "Photovoltaik (0 %) gilt nur für begünstigte PV-Anlagen; Steuersatz 0 % bei allen "
        "Positionen, Leistungsdatum."),
}


def _tax_failure(exc: LexwareAPIError, tax_type: str) -> str:
    """Lexware's reason (translated or verbatim) plus what the tax type needs."""
    reason = getattr(exc, "message", None) or str(exc)
    return f"{reason} {_TAX_PREREQUISITES.get(tax_type, '')}".strip()


def _contact_number(contact: dict[str, Any]) -> str | None:
    roles = contact.get("roles") or {}
    for role, prefix in (("customer", "Kd."), ("vendor", "Lief.")):
        entry = roles.get(role) if isinstance(roles, dict) else None
        if isinstance(entry, dict) and entry.get("number"):
            return f"{prefix} {entry['number']}"
    return None


def _billing(contact: dict[str, Any]) -> dict[str, Any]:
    entries = (contact.get("addresses") or {}).get("billing") or [{}]
    return entries[0] if isinstance(entries[0], dict) else {}


def _contact_brief(contact: dict[str, Any]) -> str:
    """"Müller GmbH, Kd. 10052 (Dortmund), id …"."""
    number, city = _contact_number(contact), _billing(contact).get("city")
    parts = [_contact_label(contact)]
    if number:
        parts.append(number + (f" ({city})" if city else ""))
    elif city:
        parts.append(f"({city})")
    return ", ".join(parts) + f", id {contact.get('id')}"


def _exact_contacts(client: LexwareClient, name: str) -> list[dict[str, Any]]:
    """Contacts whose name is exactly `name` (ä = ae, any case, "Lang, Peter")."""
    wanted = fold(name)
    rows: list[dict[str, Any]] = []
    try:  # Lexware's own search (both spellings) — no full scan for one-offs
        for variant in [name.strip(), *_spellings(name.strip())]:
            rows += _list_contacts_raw(
                client, name=variant, email=None, number=None, customer=False,
                vendor=False, include_archived=False, limit=10, fetch_all=False,
            ).items
    except Exception:  # best effort
        return []
    rows = list({str(r.get("id")): r for r in rows}.values())
    out = []
    for row in rows:
        person = row.get("person") or {}
        reverse = fold(f"{person.get('lastName') or ''} {person.get('firstName') or ''}")
        if fold(_contact_label(row)) == wanted or (person and reverse == wanted):
            out.append(row)
    return out


def _prepare_doc(client: LexwareClient, body: dict[str, Any], kind: str) -> dict[str, Any]:
    """prepare_document plus the customer: id, number or name → contactId."""
    body = prepare_document(client, body, kind)
    if not isinstance(body, dict):
        return body
    notes = [n for n in (body.pop("__hinweis", None),) if n]
    is_copy = bool(body.pop("__copy", None))
    if is_copy:
        notes.insert(0, "Kopie eines vorhandenen Belegs: Belegdatum auf heute gesetzt "
                        "(Leistungs-/Lieferdatum bitte prüfen).")
    address = body.get("address")
    if not isinstance(address, dict) or not any(
            v for k, v in address.items() if k != "countryCode"):
        raise LexwareError(_NO_CUSTOMER)
    full_one_off = not address.get("contactId") and bool(
        set(address) - {"name", "countryCode", "contactPerson"})
    contact = str(address.get("contactId") or "").strip()
    if contact and not looks_like_uuid(contact):
        try:
            address["contactId"] = resolve_contact_id(client, contact)
        except LexwareError as exc:
            if ("Ähnlich" in str(exc) or "Mehrere Kontakte" in str(exc)
                    or re.fullmatch(r"[A-Za-z]{0,3}[-\s]?\d+", contact)):
                raise  # similar customers exist / a number: never guess a one-off
            near = _near_contacts(client, contact)
            if near:
                raise LexwareError(
                    f"Kein Kontakt heißt genau '{contact}'. Meinten Sie: "
                    + "; ".join(f"{_contact_label(r)} (id {r.get('id')})" for r in near)
                    + "? Für einen neuen Kunden ohne Stammdaten address {name, …} angeben."
                ) from exc
            # nobody by that name: a one-off customer, as the caller wrote it
            rest = {k: v for k, v in address.items() if k != "contactId"}
            body["address"] = {"name": contact, "countryCode": "DE", **rest}
            notes.append(
                f"'{contact}' gibt es nicht im Kundenstamm – der Beleg geht an eine "
                "einmalige Adresse ohne Anschrift. Für einen Stammkunden erst den "
                "Kontakt anlegen; für eine Anschrift address {name, street, zip, city}."
            )
    elif not contact and address.get("name") and not full_one_off:
        # only a bare name is a reference; a full address is a deliberate
        # one-off address (e.g. same company, other site)
        same = _exact_contacts(client, str(address["name"]))
        if len(same) > 1:
            raise LexwareError(_several_named(str(address["name"]), same))
        if same:
            # Only the id: with a name next to it Lexware treats the address as
            # a full custom one and demands countryCode etc. (seen live).
            keep = {"contactPerson"}
            body["address"] = {
                "contactId": same[0]["id"],
                **{k: v for k, v in address.items() if k in keep},
            }
    address = body["address"]
    if looks_like_uuid(str(address.get("contactId") or "")):
        # a copy carries the name printed back then — the contact may be renamed
        _complete_contact_address(client, body, notes, check_name=not is_copy)
    elif full_one_off and address.get("name"):
        same = _exact_contacts(client, str(address["name"]))
        if same:
            notes.append(
                f"'{address['name']}' gibt es als Kontakt ({_contact_brief(same[0])}) – der "
                "Beleg ist nicht mit ihm verknüpft (Einmaladresse). Für die Verknüpfung "
                "kunde bzw. contactId angeben."
            )
    if notes:
        body["__hinweis"] = " ".join(notes)
    return body


def _several_named(name: str, rows: list[dict[str, Any]]) -> str:
    return (f"Mehrere Kontakte heißen genau '{name}': "
            + "; ".join(_contact_brief(r) for r in rows)
            + " – bitte die Kundennummer (oder id) angeben.")


def _complete_contact_address(
    client: LexwareClient, body: dict[str, Any], notes: list[str], *, check_name: bool = True
) -> None:
    """contactId plus some address fields: the missing ones come from the
    contact (Lexware would print the partial address otherwise); a different
    name raises. Also: EU customer with VAT id but 19 % → hint."""
    address = body["address"]
    extra = set(address) - {"contactId", "contactPerson", "countryCode"}
    tax = str((body.get("taxConditions") or {}).get("taxType") or "")
    taxed = tax in ("net", "gross") and any(
        isinstance(i, dict) and isinstance(i.get("unitPrice"), dict)
        and (i["unitPrice"].get("taxRatePercentage") or 0) > 0
        for i in body.get("lineItems") or [])
    if not extra and not taxed:
        return
    try:
        contact = client.get(f"/v1/contacts/{address['contactId']}")
    except Exception:  # best effort — Lexware names a wrong id itself
        return
    if not isinstance(contact, dict):
        return
    label = _contact_label(contact)
    name = address.get("name")
    if check_name and name and fold(str(name)) != fold(label) and _loose(str(name)) != _loose(
            label):
        raise LexwareError(
            f"Der Name '{name}' passt nicht zum Kontakt {_contact_brief(contact)}. Soll der "
            "Beleg an diesen Kontakt gehen (dann den Namen weglassen) oder an eine "
            "Einmaladresse (dann ohne contactId, mit voller Anschrift)?"
        )
    billing = _billing(contact)
    if extra:
        for key, value in (("name", label), ("supplement", billing.get("supplement")),
                           ("street", billing.get("street")), ("zip", billing.get("zip")),
                           ("city", billing.get("city")),
                           ("countryCode", billing.get("countryCode"))):
            if value and not address.get(key):
                address[key] = value
    company = contact.get("company") or {}
    country = billing.get("countryCode")
    if taxed and company.get("vatRegistrationId") and country in _EU_COUNTRIES:
        notes.append(
            f"{label} sitzt in {country} und hat eine USt-IdNr. – ggf. innergemeinschaftliche "
            "Lieferung (Steuerart intraCommunitySupply, 0 %) statt deutscher Umsatzsteuer?"
        )


def _post_doc(client: LexwareClient, path: str, body: dict[str, Any]) -> Any:
    """Validate, POST, and pass on a note from the preparation (``hinweis``)."""
    note = body.pop("__hinweis", None) if isinstance(body, dict) else None
    validate_voucher(body)
    try:
        created = client.post(path, body)
    except LexwareAPIError as exc:
        tax = str(((body.get("taxConditions") or {}) if isinstance(body, dict) else {})
                  .get("taxType") or "")
        if exc.status_code in (400, 406) and tax in _TAX_PREREQUISITES:
            raise LexwareError(_tax_failure(exc, tax)) from exc
        raise
    if isinstance(created, dict) and created.get("id"):
        try:
            doc = client.get(f"{path}/{created['id']}")
            total = doc.get("totalPrice") or {}
            created = {**created, **{k: v for k, v in {
                "voucherNumber": doc.get("voucherNumber"),
                "voucherStatus": doc.get("voucherStatus"),
                "totalNetAmount": total.get("totalNetAmount"),
                "totalGrossAmount": total.get("totalGrossAmount"),
            }.items() if v is not None}}
            wanted = (body.get("address") or {}).get("contactPerson")
            got = (doc.get("address") or {}).get("contactPerson")
            if wanted and got and fold(str(got)) != fold(str(wanted)):
                note = _join(note, (
                    f"Lexware hat als Ansprechpartner „{got}“ eingetragen (der "
                    f"Ansprechpartner des Kontakts) statt „{wanted}“ – ein anderer "
                    "Ansprechpartner geht nur über eine Einmaladresse oder in Lexware."))
        except Exception:  # best effort — the document exists
            pass
    if note and isinstance(created, dict):
        return {**created, "hinweis": note}
    return created


def _loose(text: str) -> str:
    """Even looser than fold: ue/u, oe/o, ae/a, ß/ss and spacing don't matter."""
    out = fold(text).replace("ue", "u").replace("oe", "o").replace("ae", "a")
    return re.sub(r"\s+", "", out)


def _near_contacts(client: LexwareClient, name: str) -> list[dict[str, Any]]:
    """Contacts whose name is almost the given one (typo, Umlaut spelling)."""
    import difflib

    wanted = _loose(name)
    near: list[dict[str, Any]] = []
    try:
        for index, contact in enumerate(client.paginate("/v1/contacts")):
            if index >= _CONTACT_SCAN_MAX:
                break
            have = _loose(_contact_label(contact))
            if have and (have == wanted or difflib.SequenceMatcher(None, have, wanted)
                         .ratio() >= 0.9):
                near.append(contact)
                if len(near) >= 5:
                    break
    except Exception:  # best effort
        return []
    return near


def _unique_contact_by_name(client: LexwareClient, name: str) -> str | None:
    """Id of the one contact whose name equals `name` (ä = ae, any case)."""
    wanted = fold(name)
    try:  # Lexware's own search only — no full scan for one-off customers
        rows = _list_contacts_raw(
            client, name=name.strip(), email=None, number=None, customer=False,
            vendor=False, include_archived=False, limit=10, fetch_all=False,
        ).items
    except LexwareError:
        return None
    exact = [r for r in rows if fold(_contact_label(r)) == wanted]
    return exact[0]["id"] if len(exact) == 1 else None


def get_with_voucher_fallback(client: LexwareClient, primary_path: str) -> Any:
    """Try a specific document endpoint, fall back to /v1/vouchers/{id} on 404.

    The voucherlist endpoint returns generic voucher IDs that aren't always
    available via specific resource endpoints (e.g. /v1/invoices/{id}) when
    the document was created outside the Public API. Falling back to the
    generic voucher endpoint surfaces the data either way.
    """
    try:
        return client.get(primary_path)
    except LexwareAPIError as exc:
        if exc.status_code != 404:
            raise
        voucher_id = primary_path.rstrip("/").rsplit("/", 1)[-1]
        return client.get(f"/v1/vouchers/{voucher_id}")


def get_pdf_with_voucher_fallback(
    client: LexwareClient, primary_path: str, doc_id: str
) -> bytes:
    """Download a document PDF, falling back to /v1/files/{fileId} on 404.

    Documents created via the Lexware UI (not the Public API) aren't reachable
    via /v1/invoices/{id}/file etc. — but the underlying voucher still exposes
    file IDs in `files[]`, which can be downloaded from /v1/files/{fileId}.
    """
    try:
        return client.get_binary(primary_path)
    except LexwareAPIError as exc:
        if exc.status_code != 404:
            raise
        voucher = client.get(f"/v1/vouchers/{doc_id}")
        files = voucher.get("files") or []
        if not files:
            raise LexwareAPIError(
                404, f"Kein PDF/File für Beleg {doc_id} verfügbar."
            ) from exc
        return client.get_binary(f"/v1/files/{files[0]}")


def search_articles(
    client: LexwareClient,
    search: str,
    *,
    article_type: str | None = None,
    gtin: str | None = None,
    max_items: int | None = None,
) -> list[dict[str, Any]]:
    """Free-text search over articles, done client-side.

    The Lexware article endpoint only filters by exact `articleNumber`, `gtin`
    and `type` — there is no server-side name search. So we page through the
    articles (optionally narrowed by type/gtin) and keep the ones whose title,
    description or article number contain `search` (case-insensitive). Paging
    stops early once `max_items` matches are collected.
    """
    needle = search.casefold()
    article_type = normalize_article_type(article_type)
    results: list[dict[str, Any]] = []
    for item in client.paginate(
        "/v1/articles", params={"type": article_type, "gtin": gtin}
    ):
        if _article_matches(item, needle):
            results.append(item)
            if max_items is not None and len(results) >= max_items:
                break
    return results


# Lexware's article filter only accepts PRODUCT/SERVICE in capitals and answers
# anything else with a bare HTTP 406. Callers (and our own docs) say
# "product"/"service"; German words are common too.
_ARTICLE_TYPES = {
    "product": "PRODUCT",
    "produkt": "PRODUCT",
    "produkte": "PRODUCT",
    "ware": "PRODUCT",
    "waren": "PRODUCT",
    "material": "PRODUCT",
    "artikel": "PRODUCT",
    "service": "SERVICE",
    "services": "SERVICE",
    "dienstleistung": "SERVICE",
    "dienstleistungen": "SERVICE",
    "leistung": "SERVICE",
    "leistungen": "SERVICE",
}


def normalize_article_type(article_type: str | None) -> str | None:
    """Map an article type filter to Lexware's PRODUCT/SERVICE (None/"" → None)."""
    if article_type is None or not str(article_type).strip():
        return None
    value = str(article_type).strip().casefold()
    if value in _ARTICLE_TYPES:
        return _ARTICLE_TYPES[value]
    parts = {p.strip() for p in re.split(r"[,;|/]|\bund\b|\boder\b", value) if p.strip()}
    if parts and all(p in _ARTICLE_TYPES for p in parts):
        kinds = {_ARTICLE_TYPES[p] for p in parts}
        return kinds.pop() if len(kinds) == 1 else None
    if value in ("alle", "all", "any", "beide", "both"):
        return None
    raise LexwareError(
        f"Ungültige Artikelart '{article_type}'. Gültig sind 'product' "
        "(Produkt/Ware) und 'service' (Dienstleistung)."
    )


def _article_matches(article: dict[str, Any], needle_casefolded: str) -> bool:
    for key in ("title", "description", "articleNumber"):
        value = article.get(key)
        if value and needle_casefolded in str(value).casefold():
            return True
    return False


def paginate_filtered(
    client: LexwareClient,
    path: str,
    params: dict[str, Any] | None,
    max_items: int | None,
    predicate: Callable[[dict[str, Any]], bool],
    meta: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], int, bool]:
    """Paginate `path`, keeping only items matching `predicate`.

    Used where the API has no server-side filter (e.g. archived contacts), so
    we must page through and filter client-side. Pages are fetched only until
    `max_items` *matching* items are collected.

    Returns `(kept, skipped, exhausted)`:
      - kept: the matching items (up to max_items)
      - skipped: how many items were filtered out along the way
      - exhausted: True if every page was scanned (so `kept` is the complete
        matching set and `skipped` the complete skipped count)
    """
    kept: list[dict[str, Any]] = []
    skipped = 0
    exhausted = True
    for item in client.paginate(path, params=params, meta=meta):
        if not predicate(item):
            skipped += 1
            continue
        kept.append(item)
        if max_items is not None and len(kept) >= max_items:
            exhausted = False
            break
    return kept, skipped, exhausted


# ---------------------------------------------------------------------------
# High-level operations — one cohesive function per CLI/MCP action. These build
# the request, page through results and return data + metadata (ListResult for
# lists, raw dicts/bytes otherwise). No formatting, no print.
# ---------------------------------------------------------------------------


def get_profile(client: LexwareClient) -> dict[str, Any]:
    """Return the company profile (useful as an auth test)."""
    return client.get("/v1/profile")


def _list_voucherlist(
    client: LexwareClient,
    *,
    voucher_type: str,
    status: str | None,
    number: str | None = None,
    contact_id: str | None = None,
    include_archived: bool = False,
    limit: int = 25,
    fetch_all: bool = False,
    date_from: str | None = None,
    date_to: str | None = None,
) -> ListResult:
    """Shared voucherlist query for invoices/vouchers/quotations/delivery-notes.

    Lexware rejects 'overdue' combined with other states (HTTP 400). A combined
    filter such as 'open,overdue' is therefore split into two queries — first
    'overdue', then the remaining states — and merged without duplicates.
    """
    max_items = resolve_max_items(limit, fetch_all)
    if contact_id:
        contact_id = resolve_contact_id(client, contact_id)
    status = normalize_voucher_status(status)
    dates = _date_filters(date_from, date_to)
    if number and re.search(r"[,;]", number):
        rows: list[dict[str, Any]] = []
        for part in [p.strip() for p in re.split(r"[,;]", number) if p.strip()][:10]:
            rows += _list_voucherlist(
                client, voucher_type=voucher_type, status=status, number=part,
                contact_id=contact_id, include_archived=include_archived, limit=limit,
                fetch_all=fetch_all, date_from=date_from, date_to=date_to,
            ).items
        return ListResult(items=rows, total=len(rows))
    if number:
        number = number.strip()
    result = _query_voucherlist(
        client, voucher_type, status, number, contact_id, include_archived, max_items,
        dates,
    )
    if number and not result.items:
        # "6", "RE 0006", or a number of another type: same tolerant lookup
        # as the single-document tools, the hit carries a `hinweis`.
        try:
            match = find_voucher(client, number, voucher_type)
        except LexwareError:
            try:
                match = find_voucher(client, number, ALL_VOUCHER_TYPES)
            except LexwareError:
                return result
            if match.row and _row_passes(match.row, status, contact_id, include_archived):
                note = (
                    f"'{number}' ist ein Beleg der Art {_type_label(match.type)}, "
                    "nicht der gesuchten Art."
                )
                return ListResult(items=[{**match.row, "hinweis": note}], total=1)
            return result
        rows = [r for r in (match.rows or [match.row]) if r
                and _row_passes(r, status, contact_id, include_archived)]
        rows.sort(key=lambda r: r is not match.row)  # the best match first
        if rows:
            if match.note:
                rows[0] = {**rows[0], "hinweis": match.note}
            return ListResult(items=rows, total=len(rows))
    return result


def _row_passes(
    row: dict[str, Any], status: str | None, contact_id: str | None, include_archived: bool
) -> bool:
    """Does a fallback hit satisfy the caller's other filters?"""
    if row.get("archived") and not include_archived:
        return False
    if contact_id and row.get("contactId") != contact_id:
        return False
    if status:
        wanted = set(status.split(","))
        state = row.get("voucherStatus")
        if state not in wanted and not (state == "overdue" and "open" in wanted):
            return False
    return True


def _query_voucherlist(
    client: LexwareClient,
    voucher_type: str,
    status: str | None,
    number: str | None,
    contact_id: str | None,
    include_archived: bool,
    max_items: int | None,
    dates: dict[str, str] | None = None,
) -> ListResult:
    params: dict[str, Any] = {
        "voucherType": voucher_type,
        "voucherNumber": number,
        "contactId": contact_id,
        "archived": archived_param(include_archived),
        **(dates or {}),
    }
    groups = split_voucher_status(status or DEFAULT_VOUCHER_STATUSES)
    if len(groups) == 1:
        meta: dict[str, Any] = {}
        items = list(
            client.paginate(
                "/v1/voucherlist",
                params={**params, "voucherStatus": groups[0]},
                max_items=max_items,
                meta=meta,
            )
        )
        return ListResult(items=items, total=meta.get("total"))

    merged: list[dict[str, Any]] = []
    seen: set[Any] = set()
    complete = True
    for group in groups:
        rows = list(
            client.paginate(
                "/v1/voucherlist",
                params={**params, "voucherStatus": group},
                max_items=max_items,
            )
        )
        if max_items is not None and len(rows) >= max_items:
            complete = False
        for row in rows:
            key = row.get("id")
            if key is not None and key in seen:
                continue
            if key is not None:
                seen.add(key)
            merged.append(row)
    # newest first, like a single query — otherwise all overdue rows would
    # come before every other state and newer vouchers fall off the limit.
    merged.sort(key=lambda r: str(r.get("voucherDate") or ""), reverse=True)
    if max_items is not None and len(merged) > max_items:
        merged = merged[:max_items]
        complete = False
    # The real size of the merged set is only known when nothing was cut off.
    return ListResult(items=merged, total=len(merged) if complete else None)


def _date_filters(date_from: str | None, date_to: str | None) -> dict[str, str]:
    """Belegdatum von/bis ("01.10.2026", "2026-10-01", "heute", "November 2026",
    "2026-11", "Oktober") → voucherlist params; a month means its first resp.
    last day."""
    from lxw_cli.core.prepare import document_timestamp

    out: dict[str, str] = {}
    for key, value, first in (("voucherDateFrom", date_from, True),
                              ("voucherDateTo", date_to, False)):
        if not value or not str(value).strip():
            continue
        month = _month_range(str(value))
        if month:
            out[key] = (month[0] if first else month[1]).isoformat()
            continue
        stamp = str(document_timestamp(str(value)))
        if not re.match(r"^\d{4}-\d{2}-\d{2}", stamp):
            raise LexwareError(
                f"Datum '{value}' nicht lesbar – bitte TT.MM.JJJJ, JJJJ-MM-TT oder einen "
                "Monat wie „November 2026“."
            )
        out[key] = stamp[:10]
    if out.get("voucherDateFrom") and out.get("voucherDateTo") and (
            out["voucherDateFrom"] > out["voucherDateTo"]):
        raise LexwareError(
            f"Der Zeitraum ist verdreht: date_from ({out['voucherDateFrom']}) liegt nach "
            f"date_to ({out['voucherDateTo']}) – bitte die Daten tauschen."
        )
    return out


def _month_range(value: str) -> tuple[Any, Any] | None:
    """"November 2026", "Nov. 2026", "2026-11", "11/2026", "Oktober" → (1st, last)."""
    from datetime import date

    from lxw_cli.core.prepare import _MONTHS, _now, month_end

    text = value.strip().casefold().replace(".", " ").replace(",", " ")
    m = re.fullmatch(r"(\d{4})-(\d{1,2})", text) or None
    year = month = None
    if m:
        year, month = int(m.group(1)), int(m.group(2))
    elif (m := re.fullmatch(r"(\d{1,2})\s*[/ ]\s*(\d{4})", text)):
        month, year = int(m.group(1)), int(m.group(2))
    else:
        parts = text.split()
        if len(parts) in (1, 2) and parts[0] in _MONTHS and (
                len(parts) == 1 or re.fullmatch(r"\d{4}", parts[1])):
            month = _MONTHS[parts[0]]
            year = int(parts[1]) if len(parts) == 2 else _now().year
    if not year or not month or not 1 <= month <= 12:
        return None
    first = date(year, month, 1)
    return first, month_end(first)


def split_voucher_status(status: str) -> list[str]:
    """Split a voucherStatus filter so 'overdue' is never combined with others.

    'overdue' is a derived state the API only accepts on its own. Returns one
    group for a plain filter, or ['overdue', '<rest>'] when it was combined.
    """
    states = [s.strip() for s in status.split(",") if s.strip()]
    overdue = [s for s in states if s.casefold() == "overdue"]
    if not overdue or len(states) == 1:
        return [",".join(states) or status]
    rest = [s for s in states if s.casefold() != "overdue"]
    return ["overdue", ",".join(dict.fromkeys(rest))] if rest else ["overdue"]


# -- Invoices ---------------------------------------------------------------


def list_invoices(
    client: LexwareClient,
    *,
    status: str | None = None,
    number: str | None = None,
    contact_id: str | None = None,
    include_archived: bool = False,
    limit: int = 25,
    fetch_all: bool = False,
    date_from: str | None = None,
    date_to: str | None = None,
) -> ListResult:
    return _list_voucherlist(
        client,
        voucher_type=INVOICE_LIKE_TYPES,
        status=status,
        number=number,
        contact_id=contact_id,
        include_archived=include_archived,
        limit=limit,
        fetch_all=fetch_all,
        date_from=date_from,
        date_to=date_to,
    )


def get_invoice(client: LexwareClient, identifier: str) -> dict[str, Any]:
    """Invoice details — also for bookkeeping sales invoices and down payment
    invoices (Abschlagsrechnungen), which the invoice list includes."""
    return get_voucher_details(client, identifier, INVOICE_LIKE_TYPES)


def download_invoice_pdf(client: LexwareClient, identifier: str) -> bytes:
    return download_pdf(client, identifier, INVOICE_PDF_TYPES)[0]


INVOICE_PDF_TYPES = f"{INVOICE_LIKE_TYPES},creditnote"
_PDF_PATHS = {
    "invoice": "/v1/invoices/{id}",
    "quotation": "/v1/quotations/{id}",
    "orderconfirmation": "/v1/order-confirmations/{id}",
    "deliverynote": "/v1/delivery-notes/{id}",
}
_PDF_NAMES = {
    "invoice": "Rechnung", "salesinvoice": "Rechnung", "downpaymentinvoice":
    "Abschlagsrechnung", "creditnote": "Rechnungskorrektur", "quotation": "Angebot",
    "orderconfirmation": "Auftrag", "deliverynote": "Lieferschein",
}


def download_pdf(
    client: LexwareClient, identifier: str, voucher_type: str
) -> tuple[bytes, str]:
    """PDF of a document and a file stem like "Rechnung-RE0006".

    Works for invoices, down payment invoices, Rechnungskorrekturen,
    quotations, orders, delivery notes and bookkeeping vouchers (their stored
    file). A number of another type than asked still yields its PDF.
    """
    match = locate_voucher(client, identifier, voucher_type)
    _refuse_silent_pick(match)
    number = (match.row or {}).get("voucherNumber") or identifier.strip()
    if (
        match.type and voucher_type != ALL_VOUCHER_TYPES
        and match.type not in voucher_type.split(",")
    ):
        tool = _PDF_TOOLS.get(match.type or "", "download_voucher_file")
        raise LexwareError(
            f"{number} ist {_kind_phrase(match.type)}, "
            f"kein Beleg der gesuchten Art ({_types_label(voucher_type)}). Für dieses "
            f"PDF bitte {tool} verwenden."
        )
    kinds = [match.type] if match.type else [
        t.strip() for t in f"{voucher_type},{ALL_VOUCHER_TYPES}".split(",")
    ]
    last: LexwareAPIError | None = None
    for kind in dict.fromkeys(kinds):
        try:
            data = _pdf_for(client, kind, match.id)
        except LexwareAPIError as exc:
            if exc.status_code != 404 or match.type:
                raise
            last = exc
            continue
        name = _PDF_NAMES.get(kind, "Beleg")
        return data, safe_stem(f"{name}-{number}")
    raise LexwareError(
        f"Zu '{identifier.strip()}' gibt es kein PDF (Beleg nicht gefunden)."
    ) from last


_PDF_TOOLS = {
    "invoice": "download_invoice_pdf", "salesinvoice": "download_invoice_pdf",
    "downpaymentinvoice": "download_invoice_pdf", "creditnote": "download_invoice_pdf",
    "quotation": "download_quotation_pdf",
    "orderconfirmation": "download_order_confirmation_pdf",
    "deliverynote": "download_delivery_note_pdf",
}


def _refuse_silent_pick(match: VoucherMatch) -> None:
    """A file can't carry a hint — with several candidates, ask instead."""
    if match.ambiguous:
        raise LexwareError(
            f"{_candidates_text(match)} Für die Datei bitte den gewünschten Beleg über seine "
            "id oder die volle Nummer angeben."
        )


def _candidates_text(match: VoucherMatch) -> str:
    """The candidates of an ambiguous number — for a message that shows none."""
    rows = match.rows or ([match.row] if match.row else [])
    number = (match.row or {}).get("voucherNumber") or ""
    numbers = {str(r.get("voucherNumber")) for r in rows}
    lead = (f"Die Nummer '{number}' gibt es {len(rows)}-mal" if len(numbers) == 1
            else f"Mehrere Belege passen ({len(rows)})")
    return f"{lead}: " + "; ".join(_describe_row(r) for r in rows) + "."


def safe_stem(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-") or "beleg"


def _pdf_for(client: LexwareClient, kind: str, doc_id: str) -> bytes:
    if kind == "downpaymentinvoice":
        return _down_payment_invoice_pdf(client, doc_id)
    if kind == "creditnote":
        return _rendered_pdf(client, f"/v1/credit-notes/{doc_id}", "Rechnungskorrektur")
    if kind == "salesinvoice":
        kind = "invoice"
    if kind in _PDF_PATHS:
        base = _PDF_PATHS[kind].format(id=doc_id)
        return get_pdf_with_voucher_fallback(client, f"{base}/file", doc_id)
    # bookkeeping voucher: its stored receipt
    voucher = client.get(f"/v1/vouchers/{doc_id}")
    files = voucher.get("files") or []
    label = voucher.get("voucherNumber") or doc_id
    if not files:
        raise LexwareError(f"Der Buchhaltungsbeleg {label} hat keine angehängte Datei.")
    data, ctype, _name = client.download_file(f"/v1/files/{files[0]}")
    if ctype and "pdf" not in ctype.lower():
        raise LexwareError(
            f"Die Datei am Buchhaltungsbeleg {label} ist kein PDF ({ctype}) – bitte "
            "über „Datei herunterladen“ (download_voucher_file) holen."
        )
    return data


def _rendered_pdf(client: LexwareClient, base: str, label: str) -> bytes:
    """``{base}/file``, else ``{base}/document`` → documentFileId → /v1/files."""
    try:
        return client.get_binary(f"{base}/file")
    except LexwareAPIError as exc:
        if exc.status_code != 404:
            raise
        rendered = client.get(f"{base}/document")
        file_id = rendered.get("documentFileId") if isinstance(rendered, dict) else None
        if not file_id:
            raise LexwareAPIError(404, f"Kein PDF für {label} verfügbar.") from exc
        return client.get_binary(f"/v1/files/{file_id}")


def _down_payment_invoice_pdf(client: LexwareClient, invoice_id: str) -> bytes:
    """PDF of a down payment invoice (Abschlagsrechnung).

    Prefers ``/v1/down-payment-invoices/{id}/file``; on 404 falls back to the
    older render route ``…/document``, whose ``documentFileId`` is then
    downloaded from ``/v1/files/{fileId}``.
    """
    base = f"/v1/down-payment-invoices/{invoice_id}"
    try:
        return client.get_binary(f"{base}/file")
    except LexwareAPIError as exc:
        if exc.status_code != 404:
            raise
        rendered = client.get(f"{base}/document")
        file_id = rendered.get("documentFileId") if isinstance(rendered, dict) else None
        if not file_id:
            raise LexwareAPIError(
                404, f"Kein PDF für Abschlagsrechnung {invoice_id} verfügbar."
            ) from exc
        return client.get_binary(f"/v1/files/{file_id}")


def create_invoice(client: LexwareClient, body: dict[str, Any]) -> dict[str, Any]:
    body = _prepare_doc(client, body, INVOICE)
    return _post_doc(client, "/v1/invoices", body)


# -- Vouchers ---------------------------------------------------------------


def list_vouchers(
    client: LexwareClient,
    *,
    voucher_type: str | None = None,
    status: str | None = None,
    number: str | None = None,
    contact_id: str | None = None,
    include_archived: bool = False,
    limit: int = 25,
    fetch_all: bool = False,
    date_from: str | None = None,
    date_to: str | None = None,
) -> ListResult:
    return _list_voucherlist(
        client,
        voucher_type=(
            normalize_voucher_type(voucher_type) if voucher_type else ALL_VOUCHER_TYPES
        ),
        status=status,
        number=number,
        contact_id=contact_id,
        include_archived=include_archived,
        limit=limit,
        fetch_all=fetch_all,
        date_from=date_from,
        date_to=date_to,
    )


def normalize_voucher_type(voucher_type: str) -> str:
    """Check a comma-separated voucherType filter before it reaches Lexware.

    Lexware answers an unknown type (e.g. 'voucher') with a bare HTTP 400.
    Catch that up front with a German message that lists the valid values —
    the caller (often an AI) can then correct itself in one step. Returns the
    filter lower-cased and without duplicates.
    """
    types: list[str] = []
    for raw in re.split(r"[,;|/]", voucher_type):
        word = raw.strip().lower()
        if not word:
            continue
        expanded = None if word in VOUCHER_TYPE_LABELS else expand_voucher_type_word(word)
        types.extend(expanded.split(",") if expanded else [word])
    if "any" in types:
        return "any"
    invalid = [t for t in types if t not in VOUCHER_TYPE_LABELS]
    if not types or invalid:
        valid = ", ".join(f"{t} ({label})" for t, label in VOUCHER_TYPE_LABELS.items())
        shown = ", ".join(f"'{t}'" for t in invalid) or f"'{voucher_type}'"
        raise LexwareError(
            f"Ungültiger Belegtyp {shown}. Gültige Werte (kommagetrennt "
            f"kombinierbar): {valid}."
        )
    return ",".join(dict.fromkeys(types))


def get_voucher(client: LexwareClient, identifier: str) -> dict[str, Any]:
    """Any voucher's details — bookkeeping vouchers as well as invoices, down
    payment invoices, credit notes, quotations, orders and delivery notes."""
    return get_voucher_details(client, identifier, ALL_VOUCHER_TYPES)


def get_bookkeeping_voucher(client: LexwareClient, identifier: str) -> dict[str, Any]:
    """A bookkeeping voucher from /v1/vouchers only (e.g. to attach files)."""
    voucher_id = _bookkeeping_id(client, identifier)
    try:
        return client.get(f"/v1/vouchers/{voucher_id}")
    except LexwareAPIError as exc:
        if exc.status_code == 404:
            raise LexwareError(_NO_BOOKKEEPING.format(id=voucher_id)) from exc
        raise


def _bookkeeping_id(client: LexwareClient, identifier: str) -> str:
    """Id of a bookkeeping voucher — files can only be attached there."""
    match = find_voucher(client, identifier, ALL_VOUCHER_TYPES, prefer=BOOKKEEPING_TYPES)
    if match.type in _INVOICING_TYPES:
        number = (match.row or {}).get("voucherNumber") or identifier.strip()
        draft = (match.row or {}).get("voucherStatus") == "draft"
        raise LexwareError(
            f"{number} ist ein Beleg aus dem Rechnungsteil ({_type_label(match.type)}). "
            "Dateien lassen sich nur an Buchhaltungsbelege (z. B. Eingangsrechnungen, "
            "Quittungen) anhängen; "
            + ("der Entwurf bekommt sein PDF, wenn er in Lexware abgeschlossen wird."
               if draft else "dieser Beleg hat sein PDF bereits.")
        )
    return match.id


_NO_BOOKKEEPING = (
    "Unter der id {id} gibt es keinen Buchhaltungsbeleg. Dateien lassen sich nur an "
    "Buchhaltungsbelege anhängen (nicht an Rechnungen, Angebote usw. aus dem "
    "Rechnungsteil – die haben ihr PDF schon)."
)


_INCOME_KINDS = {"salesinvoice", "salescreditnote"}
# everyday words → Lexware category names (only used when given as a name)
_CATEGORY_WORDS = {
    "tanken": "Kraftstoff/Ladestrom", "benzin": "Kraftstoff/Ladestrom",
    "diesel": "Kraftstoff/Ladestrom", "sprit": "Kraftstoff/Ladestrom",
    "kraftstoff": "Kraftstoff/Ladestrom", "telefon": "Festnetz", "handy": "Mobil",
    "mobilfunk": "Mobil", "miete": "Miete/Pacht", "bueromaterial": "Bürobedarf",
    "buero material": "Bürobedarf", "porto kosten": "Porto", "briefmarken": "Porto",
    "bewirtung": "Bewirtungskosten (mit Geschäftspartnern)",
    "geschaeftsessen": "Bewirtungskosten (mit Geschäftspartnern)",
    "hotel": "Übernachtungskosten", "uebernachtung": "Übernachtungskosten",
    "bahn": "Bahn-/Flugticket, Mietwagen", "flug": "Bahn-/Flugticket, Mietwagen",
    "software abo": "Software", "werbung": "Werbung", "reparatur": "Sonstige Reparaturen",
}


def _require_voucher_fields(body: dict[str, Any]) -> None:
    """Name every missing field at once instead of one Lexware error each."""
    missing = voucher_missing(body)
    if missing:
        raise LexwareError("Für den Buchhaltungsbeleg fehlt: " + "; ".join(missing) + ".")


def _category_word(value: str) -> str:
    """Everyday word → Lexware category name ("Tank" → Kraftstoff/Ladestrom)."""
    key = fold(value)
    if key in _CATEGORY_WORDS:
        return _CATEGORY_WORDS[key]
    if len(key) >= 4:
        for word, name in _CATEGORY_WORDS.items():
            if word.startswith(key) or key.startswith(word):
                return name
    return value


def _resolve_categories(
    client: LexwareClient, body: dict[str, Any], *, generic_credit: bool = False
) -> list[str]:
    """categoryId per item: id, or category name → id; wrong kind → German error.

    Returns the category names used (for the result). A plain "Gutschrift"
    becomes a supplier's credit note when the category is an expense one.
    """
    items = body.get("voucherItems")
    if not isinstance(items, list) or not items:
        return []
    kind = str(body.get("type") or "")
    wanted_type = "income" if kind in _INCOME_KINDS else "outgo" if kind else None
    if generic_credit:
        wanted_type = None
    for item in items:
        if isinstance(item, dict) and not str(item.get("categoryId") or "").strip():
            raise LexwareError(
                "Buchungskategorie fehlt: Jede Position braucht eine categoryId "
                f"({'Einnahmen' if wanted_type == 'income' else 'Ausgaben'}-Kategorie, "
                "z. B. aus der Liste der Buchungskategorien – der Name geht auch)."
            )
    needs_lookup = any(
        isinstance(i, dict) and not looks_like_uuid(str(i.get("categoryId") or ""))
        for i in items
    ) or generic_credit
    if not needs_lookup:
        return []  # a wrong kind is translated from Lexware's answer
    try:
        categories = list_posting_categories(client)
    except Exception:  # best effort — Lexware will name the problem itself
        return []
    by_id = {str(c.get("id")): c for c in categories}
    used: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        value = str(item.get("categoryId") or "").strip()
        if not looks_like_uuid(value) and not any(
                fold(str(c.get("name") or "")) == fold(value) for c in categories):
            value = _category_word(value)
        if not looks_like_uuid(value):
            hits = [
                c for c in categories
                if fold(str(c.get("name") or "")) == fold(value)
                and (wanted_type is None or str(c.get("type")) == wanted_type)
            ] or [
                c for c in categories
                if fold(value) in fold(str(c.get("name") or ""))
                and (wanted_type is None or str(c.get("type")) == wanted_type)
            ]
            if not hits and wanted_type:
                other = [c for c in categories if fold(str(c.get("name") or "")) == fold(value)]
                if other:
                    kind_word = "Einnahmen" if wanted_type == "income" else "Ausgaben"
                    raise LexwareError(
                        f"„{other[0].get('name')}“ ist keine {kind_word}-Kategorie und passt "
                        f"nicht zur Belegart {_type_label(kind)}. Bitte eine "
                        f"{kind_word}-Kategorie wählen (Liste der Buchungskategorien)."
                    )
            if not hits:
                group = [c for c in categories if fold(str(c.get("groupName") or ""))
                         == fold(value) and (wanted_type is None
                                             or str(c.get("type")) == wanted_type)]
                if group:
                    names = ", ".join(str(c.get("name")) for c in group[:12])
                    raise LexwareError(
                        f"„{value}“ ist eine Gruppe von Buchungskategorien – bitte eine daraus "
                        f"wählen: {names}."
                    )
            if len(hits) != 1:
                if not hits:
                    import difflib

                    pool = [
                        str(c.get("name")) for c in categories
                        if wanted_type is None or str(c.get("type")) == wanted_type
                    ]
                    close = difflib.get_close_matches(value, pool, n=5, cutoff=0.5)
                    words = [w for w in fold(value).split() if len(w) >= 4]
                    close += [n for n in pool if any(w[:5] in fold(n) for w in words)]
                    hits = [{"name": n} for n in dict.fromkeys(close)][:6]
                names = ", ".join(str(c.get("name")) for c in hits[:8])
                raise LexwareError(
                    f"Buchungskategorie '{value}' nicht eindeutig gefunden"
                    + (f" – meinten Sie: {names}?" if names else "")
                    + " Der Name darf auch so angegeben werden."
                )
            item["categoryId"] = hits[0]["id"]
            used.append(str(hits[0].get("name")))
            _credit_side(body, hits[0], generic_credit)
            continue
        category = by_id.get(value)
        if category:
            used.append(str(category.get("name")))
            _credit_side(body, category, generic_credit)
        if category and wanted_type and str(category.get("type")) != wanted_type:
            raise LexwareError(
                f"Die Kategorie „{category.get('name')}“ ist eine "
                f"{'Einnahmen' if category.get('type') == 'income' else 'Ausgaben'}-"
                f"Kategorie, der Beleg ist aber eine {_type_label(kind)}. Bitte eine "
                f"{'Einnahmen' if wanted_type == 'income' else 'Ausgaben'}-Kategorie wählen."
            )
    return used


def _credit_side(body: dict[str, Any], category: dict[str, Any], generic: bool) -> None:
    """"Gutschrift" with an expense category is one FROM a supplier."""
    if generic:
        body["type"] = ("purchasecreditnote" if str(category.get("type")) == "outgo"
                        else "salescreditnote")


def _existing_voucher(client: LexwareClient, body: dict[str, Any]) -> dict[str, Any] | None:
    """Same number, same contact and same total already booked? Then that one."""
    number = str(body.get("voucherNumber") or "").strip()
    if not number:
        return None
    try:
        rows = _lookup_number(client, number, BOOKKEEPING_TYPES)
    except Exception:  # best effort — never block a booking on the check
        return None
    contact = body.get("contactId")
    name = str(body.get("contactName") or "").strip().casefold()
    collective = not contact and not name
    total = to_decimal_safe(body.get("totalGrossAmount"))
    day = str(body.get("voucherDate") or "")[:10]
    if total is None or not day:
        return None  # not enough to be sure — never swallow a real booking
    if collective and not re.search(r"\d", number):
        return None  # "Kassenbon"/"ohne Nr." — two real receipts may share it
    for row in rows:
        if contact:
            same_contact = row.get("contactId") == contact
        elif name:
            same_contact = _loose(str(row.get("contactName") or "")) == _loose(name)
        else:  # both on the collective contact (Sammellieferant/-kunde)
            same_contact = collective and str(row.get("contactName") or "").startswith(
                "Sammel")
        if (
            same_contact
            and to_decimal_safe(row.get("totalAmount")) == total
            and str(row.get("voucherDate") or "")[:10] == day
            and (not body.get("type") or row.get("voucherType") == body.get("type"))
            and row.get("voucherStatus") != "voided"
        ):
            return row
    return None


def _same_number_other_date(client: LexwareClient, body: dict[str, Any]) -> str | None:
    number = str(body.get("voucherNumber") or "").strip()
    if not number or not re.search(r"\d", number):
        return None
    try:
        rows = _lookup_number(client, number, BOOKKEEPING_TYPES)
    except Exception:
        return None
    if body.get("contactId"):
        same = [r for r in rows if r.get("contactId") == body.get("contactId")]
    elif body.get("contactName"):
        same = [r for r in rows if _loose(str(r.get("contactName") or ""))
                == _loose(str(body["contactName"]))]
    else:
        same = [r for r in rows if str(r.get("contactName") or "").startswith("Sammel")]
    if not same:
        return None
    dates = ", ".join(sorted({str(r.get("voucherDate") or "")[:10] for r in same}))
    return (f"Achtung: Die Belegnummer {number} gibt es beim selben Kontakt schon "
            f"(vom {dates}). Angelegt, weil Datum bzw. Betrag abweichen – bitte prüfen, "
            "ob es kein Doppel ist.")


def to_decimal_safe(value: Any) -> Any:
    try:
        return round(float(value), 2) if value is not None else None
    except (TypeError, ValueError):
        return None


# Lexware refuses a delivery date on some voucher types (e.g. purchase
# invoices) — the date carries no information there, so it is dropped.
_NO_SHIPPING_DATE = "expected_no_shipping_date_for_given_voucher_type"


def create_voucher(client: LexwareClient, body: dict[str, Any]) -> dict[str, Any]:
    body = prepare_voucher(body)
    categories: list[str] = []
    if isinstance(body, dict):
        generic_credit = bool(body.pop("__generic_credit", False))
        receipt = bool(body.pop("__receipt", False))
        _require_voucher_fields(body)
        if body.get("contactId") and not looks_like_uuid(str(body["contactId"])):
            body["contactId"] = resolve_contact_id(client, str(body["contactId"]))
        if body.get("contactName") and not body.get("contactId"):
            # a known supplier/customer by exact name → link it
            hit = _unique_contact_by_name(client, str(body["contactName"]))
            if hit:
                body["contactId"] = hit
                body.pop("contactName")
            else:
                similar = _similar_contacts(client, str(body["contactName"]))
                if similar:
                    body["__hinweis"] = _join(body.get("__hinweis"), (
                        f"Gebucht auf den Sammelkontakt mit dem Namen „{body['contactName']}“. "
                        f"Ähnliche Kontakte: {'; '.join(similar)} – für die Zuordnung "
                        "contactId bzw. den genauen Namen angeben."))
        if not body.get("contactId") and "useCollectiveContact" not in body:
            body["useCollectiveContact"] = True
        categories = _resolve_categories(client, body, generic_credit=generic_credit)
        due, day = str(body.get("dueDate") or "")[:10], str(body.get("voucherDate") or "")[:10]
        if body.get("type") == "purchaseinvoice" and (receipt or (due and due == day)):
            body["__hinweis"] = _join(body.get("__hinweis"), (
                "Lexware führt den Beleg als offen (ab Fälligkeit als überfällig), bis eine "
                "Zahlung zugeordnet ist – eine Barzahlung lässt sich über die Schnittstelle "
                "nicht erfassen (in Lexware „als bezahlt markieren“ oder per Bankabgleich)."))
        existing = _existing_voucher(client, body)
        earlier = None if existing is not None else _same_number_other_date(client, body)
        if earlier:
            body["__hinweis"] = " ".join(n for n in (body.get("__hinweis"), earlier) if n)
        if existing is not None:
            return {
                **existing,
                "hinweis": (
                    f"Diesen Beleg gibt es schon (Nummer {existing.get('voucherNumber')}, "
                    "gleicher Kontakt, Betrag und Belegdatum) – er wurde nicht doppelt "
                    "angelegt."
                ),
            }
    note = body.pop("__hinweis", None) if isinstance(body, dict) else None
    validate_voucher(body)
    try:
        created = client.post("/v1/vouchers", body)
    except LexwareAPIError as exc:
        if not (isinstance(body, dict) and body.get("shippingDate")):
            raise
        if _NO_SHIPPING_DATE not in issue_keys(exc):
            raise
        retry = {k: v for k, v in body.items() if k != "shippingDate"}
        created = client.post("/v1/vouchers", retry)
    if isinstance(created, dict):
        if categories:
            created = {**created, "kategorie": ", ".join(dict.fromkeys(categories))}
        if note:
            created = {**created, "hinweis": note}
    return created


def _similar_contacts(client: LexwareClient, name: str) -> list[str]:
    """Contacts whose name contains the given one (for a hint, never linked)."""
    try:
        rows = list_contacts(client, name=name, limit=5).items
    except Exception:  # best effort
        return []
    wanted = fold(name)
    out = []
    for row in rows:
        label = _contact_label(row)
        if wanted and wanted in fold(label) and fold(label) != wanted:
            number = _contact_number(row)
            out.append(f"{label}" + (f" ({number})" if number else ""))
    return out


# -- Beleg-Anhänge (voucher file attachments) -------------------------------


def upload_voucher_file(
    client: LexwareClient,
    identifier: str,
    *,
    filename: str,
    content: bytes,
    content_type: str | None = None,
) -> dict[str, Any]:
    """Attach a file (receipt/original document) to a voucher.

    `identifier` is a voucher UUID or number; it is resolved to the voucher's
    id and the file is POSTed as multipart/form-data to
    ``/v1/vouchers/{id}/files``. The content type is guessed from `filename`
    when not given. Lexware enforces its own size/type limits (typically PDF,
    JPG, PNG) and returns the created file's id — surfaced verbatim so the API
    stays the single source of truth for what is accepted.
    """
    voucher_id = _bookkeeping_id(client, identifier)
    filename, ctype = upload_name_and_type(filename, content, content_type)
    try:
        return client.post_multipart(
            f"/v1/vouchers/{voucher_id}/files",
            files={"file": (filename, content, ctype)},
        )
    except LexwareAPIError as exc:
        if exc.status_code == 404:
            raise LexwareError(_NO_BOOKKEEPING.format(id=voucher_id)) from exc
        raise


# Magic bytes of the receipt formats Lexware accepts (pdf, jpg, png, xml).
_SNIFF = (
    (b"%PDF", "pdf", "application/pdf"),
    (b"\x89PNG\r\n\x1a\n", "png", "image/png"),
    (b"\xff\xd8\xff", "jpg", "image/jpeg"),
    (b"<?xml", "xml", "application/xml"),
)


def upload_name_and_type(
    filename: str, content: bytes, content_type: str | None
) -> tuple[str, str]:
    """Pick the multipart filename and content type for an upload.

    Callers (especially chat assistants) often pass a bare name like
    "Rechnung Muster" without extension — the content type would then fall
    back to application/octet-stream and Lexware rejects the file. The bytes
    themselves are authoritative: sniff them, and append the matching
    extension when the name lacks one.
    """
    head = content[:64].lstrip()
    sniffed = next(
        ((ext, ctype) for magic, ext, ctype in _SNIFF if head.startswith(magic)),
        None,
    )
    guessed = mimetypes.guess_type(filename)[0]
    if sniffed and guessed != sniffed[1]:
        # No or wrong extension (e.g. a PDF named "scan.jpg"): Lexware checks
        # the extension, so make it match the actual content.
        stem = filename.rsplit(".", 1)[0] if guessed else filename
        filename = f"{stem}.{sniffed[0]}"
    ctype = content_type or (sniffed[1] if sniffed else None) or guessed
    return filename, ctype or "application/octet-stream"


def image_size(content: bytes) -> tuple[int, int] | None:
    """Pixel size (width, height) of a PNG or JPEG, else None.

    Header parsing only — no imaging library needed. Used to spot thumbnails
    a chat assistant produced instead of the real receipt.
    """
    if content.startswith(b"\x89PNG\r\n\x1a\n") and len(content) >= 24:
        return int.from_bytes(content[16:20], "big"), int.from_bytes(content[20:24], "big")
    if not content.startswith(b"\xff\xd8"):
        return None
    i = 2
    while i + 9 < len(content):
        if content[i] != 0xFF:
            i += 1
            continue
        marker = content[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7 or marker == 0xFF:
            i += 1 if marker == 0xFF else 2
            continue
        length = int.from_bytes(content[i + 2 : i + 4], "big")
        # SOF0..SOF15 except DHT (C4), JPG (C8), DAC (CC) carry the frame size.
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height = int.from_bytes(content[i + 5 : i + 7], "big")
            width = int.from_bytes(content[i + 7 : i + 9], "big")
            return width, height
        i += 2 + length
    return None


def create_voucher_with_file(
    client: LexwareClient,
    body: dict[str, Any],
    *,
    filename: str,
    content: bytes,
) -> dict[str, Any]:
    """Create a bookkeeping voucher and attach its receipt in one go.

    Lexware needs two steps: the voucher is created first and only its new id
    can receive the file. If the upload fails *after* the voucher exists, this
    must not raise — the caller would believe nothing was booked and create a
    duplicate. Instead the created voucher is returned together with
    ``fileUpload.error`` so the upload alone can be retried.
    """
    created = create_voucher(client, body)
    voucher_id = created.get("id") if isinstance(created, dict) else None
    if not voucher_id:
        return {
            **(created if isinstance(created, dict) else {}),
            "fileUpload": {
                "ok": False,
                "error": "Lexware hat keine Beleg-id zurückgegeben — Datei nicht angehängt.",
            },
        }
    try:
        uploaded = upload_voucher_file(
            client, voucher_id, filename=filename, content=content
        )
    except Exception as exc:  # noqa: BLE001 — incl. timeouts; voucher already exists
        return {
            **created,
            "fileUpload": {
                "ok": False,
                "error": str(exc) or type(exc).__name__,
                "hint": (
                    "Der Beleg wurde angelegt. Nur die Datei erneut mit "
                    f"upload_voucher_file an id {voucher_id} senden — den Beleg "
                    "NICHT neu anlegen."
                ),
            },
        }
    return {**created, "fileUpload": {"ok": True, "file": uploaded}}


def download_file(
    client: LexwareClient, file_id: str
) -> tuple[bytes, str | None, str | None]:
    """Download a stored file by its id, returning (bytes, content_type, name).

    File ids are the entries in a voucher's/document's ``files`` list (e.g. an
    attached receipt or the rendered document PDF).
    A voucher number or voucher id works too: then the voucher's own file
    (receipt or rendered document PDF) is returned.
    """
    ident = str(file_id or "").strip()
    try:
        return client.download_file(f"/v1/files/{ident}")
    except LexwareAPIError as exc:
        if exc.status_code not in (400, 404):
            raise
    match = locate_voucher(client, ident, ALL_VOUCHER_TYPES, prefer=BOOKKEEPING_TYPES)
    _refuse_silent_pick(match)
    if match.type in BOOKKEEPING_TYPES.split(",") or match.type is None:
        try:
            voucher = client.get(f"/v1/vouchers/{match.id}")
            files = voucher.get("files") or []
            if files:
                return client.download_file(f"/v1/files/{files[0]}")
        except LexwareAPIError as exc:
            if exc.status_code != 404:
                raise
    data, stem = download_pdf(client, match.id, match.type or ALL_VOUCHER_TYPES)
    number = (match.row or {}).get("voucherNumber")
    if number:
        stem = safe_stem(f"{_PDF_NAMES.get(match.type or '', 'Beleg')}-{number}")
    return data, "application/pdf", f"{stem}.pdf"


# -- Zahlstatus (payments) --------------------------------------------------


# Voucher types Lexware keeps payments for (/v1/payments/{id}). Credit notes
# from the invoicing part (Rechnungskorrekturen) are not among them — the API
# answers 406 "No payment information for this voucher/voucher type
# creditnote"; their state comes from the document itself.
PAYMENT_TYPES = (
    "invoice,downpaymentinvoice,salesinvoice,salescreditnote,"
    "purchaseinvoice,purchasecreditnote"
)
_NO_PAYMENT_TYPES = frozenset({"quotation", "orderconfirmation", "deliverynote"})
_NO_PAYMENT_RE = re.compile(r"no payment information.*?voucher type (\w+)", re.I | re.S)


def get_payment_status(client: LexwareClient, identifier: str) -> dict[str, Any]:
    """Return the payment status of a voucher (invoice, credit note, …).

    Resolves `identifier` (voucher number like 'FB2600682' or a UUID) and
    reads ``/v1/payments/{id}``: open amount, ``paymentStatus``, ``paidDate``
    and the individual ``paymentItems``. Rechnungskorrekturen (credit notes
    from the invoicing part) have no payment record at Lexware; their status
    is taken from the document (``paidoff`` = offset against the invoice).
    Quotations, orders and delivery notes have no payment status at all.
    """
    parts = [p.strip() for p in re.split(r"[,;]|\bund\b", identifier) if p.strip()]
    if len(parts) > 1:
        results: list[dict[str, Any]] = []
        for part in parts[:20]:
            try:
                results.append({"beleg": part, **get_payment_status(client, part)})
            except LexwareError as exc:
                results.append({"beleg": part, "fehler": str(exc)})
        return {"belege": results}
    try:
        match = find_voucher(
            client, identifier, ALL_VOUCHER_TYPES, prefer=f"{PAYMENT_TYPES},creditnote"
        )
    except LexwareError:
        dunning = _dunning_payment(client, identifier)
        if dunning is None:
            raise
        return dunning
    if match.type in _NO_PAYMENT_TYPES:
        plural = {"quotation": "Angebote", "orderconfirmation": "Auftragsbestätigungen",
                  "deliverynote": "Lieferscheine"}[match.type]
        raise LexwareError(
            f"Für {plural} gibt es bei Lexware keinen Zahlungsstatus – nur für "
            "Rechnungen, Abschlagsrechnungen, Rechnungskorrekturen und "
            "Buchhaltungsbelege."
        )
    if (match.row or {}).get("voucherStatus") == "overdue":
        match.note = _join(match.note, "Laut Belegliste überfällig (fällig seit "
                                       f"{str(match.row.get('dueDate') or '')[:10]}).")
    if match.type == "creditnote":
        return _with_note(_credit_note_payment_status(client, match), match.note)
    try:
        data = client.get(f"/v1/payments/{match.id}")
    except LexwareAPIError as exc:
        if exc.status_code == 404 and match.type is None:
            dunning = _dunning_payment(client, match.id)
            if dunning is not None:
                return dunning
        if exc.status_code == 406 and "draft state" in str(exc):
            row = match.row or {}
            return _with_note({
                "voucherNumber": row.get("voucherNumber"),
                "voucherType": match.type,
                "voucherStatus": "draft",
                "paymentStatus": "draft",
                # a draft owes nothing yet — same as for Rechnungskorrekturen
                "openAmount": None,
                "totalAmount": row.get("totalAmount"),
                "currency": row.get("currency") or "EUR",
                "paymentItems": [],
                "hinweis": "Der Beleg ist noch ein Entwurf – Zahlungen gibt es erst, "
                           "wenn er in Lexware abgeschlossen ist.",
            }, match.note)
        found = _NO_PAYMENT_RE.search(str(exc)) if exc.status_code == 406 else None
        if not found:
            raise
        if found.group(1).lower() == "creditnote":
            return _credit_note_payment_status(client, match)
        raise LexwareError(
            f"Für diesen Beleg ({_type_label(found.group(1).lower())}) führt Lexware "
            "keinen Zahlungsstatus."
        ) from exc
    return _with_note(data, _join(match.note, _balanced_by_credit(client, match, data)))


def _balanced_by_credit(client: LexwareClient, match: VoucherMatch, data: Any) -> str | None:
    """An invoice offset by a Rechnungskorrektur: name it."""
    if not isinstance(data, dict) or match.type not in ("invoice", None):
        return None
    state = str(data.get("paymentStatus") or "").lower()
    status = str(data.get("voucherStatus") or (match.row or {}).get("voucherStatus") or "")
    if state != "balanced" and status != "paidoff":
        return None
    try:
        invoice = client.get(f"/v1/invoices/{match.id}")
    except Exception:  # best effort
        return None
    credits = [str(r.get("voucherNumber") or r.get("id")) for r in invoice.get(
        "relatedVouchers") or [] if isinstance(r, dict)
        and str(r.get("voucherType")).lower() == "creditnote"]
    if not credits:
        return None
    return f"Ausgeglichen durch Rechnungskorrektur {', '.join(credits)}."


def _dunning_payment(client: LexwareClient, identifier: str) -> dict[str, Any] | None:
    """A dunning has no payment status of its own — point to the invoice."""
    text = _clean_number(identifier)
    if looks_like_uuid(text):
        try:
            doc = client.get(f"/v1/dunnings/{text}")
        except Exception:  # not a dunning either
            return None
        number = doc.get("voucherNumber") or _dunning_number_of(client, doc, text)
        invoice = str(number or "").rsplit("_", 1)[0] or None
    else:
        if not _DUNNING_NUMBER.match(text):
            return None
        try:
            ref = _dunning_ref(client, text)
        except LexwareError:
            return None
        number, invoice = ref.number, ref.invoice
    return {
        "voucherType": "dunning",
        "voucherNumber": number,
        "paymentStatus": None,
        "openAmount": None,
        "invoice": invoice,
        "hinweis": f"{number or 'Das'} ist eine Mahnung – Mahnungen haben keinen eigenen "
                   "Zahlungsstand. Zahlungsstand siehe Rechnung "
                   f"{invoice or '(gemahnte Rechnung)'}.",
    }


def _credit_note_payment_status(
    client: LexwareClient, match: VoucherMatch
) -> dict[str, Any]:
    """Payment view of a Rechnungskorrektur, built from the document."""
    doc = client.get(f"/v1/credit-notes/{match.id}")
    row = match.row or {}
    status = doc.get("voucherStatus") or row.get("voucherStatus")
    total = (doc.get("totalPrice") or {}).get("totalGrossAmount", row.get("totalAmount"))
    currency = (doc.get("totalPrice") or {}).get("currency") or row.get("currency") or "EUR"
    open_amount = row.get("openAmount")
    if open_amount is None and status in ("paidoff", "paid", "voided"):
        open_amount = 0
    if status == "draft":
        open_amount = None
    related = [
        {k: v for k, v in r.items() if k in ("id", "voucherNumber", "voucherType")}
        for r in doc.get("relatedVouchers") or []
    ]
    invoices = ", ".join(r["voucherNumber"] for r in related if r.get("voucherNumber"))
    if status == "paidoff":
        state = "balanced"
        text = (
            "Die Rechnungskorrektur ist verrechnet bzw. ausgeglichen"
            + (f" (bei der Rechnung {invoices} als Teilzahlung erfasst)." if invoices else ".")
        )
    elif status == "voided":
        state = "voided"
        text = "Die Rechnungskorrektur ist storniert."
    elif status == "draft":
        state = "draft"
        text = "Die Rechnungskorrektur ist noch ein Entwurf."
    else:
        state = "open"
        text = "Die Rechnungskorrektur ist offen, also noch nicht ausgezahlt oder verrechnet."
    return {
        "voucherType": "creditnote",
        "voucherNumber": doc.get("voucherNumber") or row.get("voucherNumber"),
        "voucherStatus": status,
        "paymentStatus": state,
        "totalAmount": total,
        "openAmount": open_amount,
        "currency": currency,
        "paymentItems": [],
        "relatedVouchers": related,
        "hinweis": text
        + " Lexware führt für Rechnungskorrekturen keine eigenen Zahlungsdaten; "
        "der Stand kommt aus dem Beleg selbst.",
    }


# -- Buchungskategorien (posting categories) --------------------------------


def list_posting_categories(
    client: LexwareClient, *, category_type: str | None = None, search: str | None = None
) -> list[dict[str, Any]]:
    """List the account's bookkeeping categories (Buchungskategorien).

    Returns the fixed catalog Lexware exposes at ``/v1/posting-categories``
    (a plain array, not paginated). Each entry carries at least ``id``,
    ``name`` and ``type`` (income vs. expense). `category_type`, when given,
    filters client-side by exact ``type`` match. These ids are what a
    bookkeeping voucher's items reference via ``categoryId``.
    """
    data = client.get("/v1/posting-categories")
    items = data if isinstance(data, list) else (data.get("content") or [])
    if category_type and category_type.strip():
        word = category_type.strip().casefold()
        wanted = _CATEGORY_TYPES.get(word, word)
        known = {str(c.get("type", "")).casefold() for c in items}
        if wanted not in known:
            raise LexwareError(
                f"Unbekannte Kategorieart '{category_type}'. Gültig sind 'income' "
                "(Einnahmen) und 'outgo' (Ausgaben)."
            )
        items = [c for c in items if str(c.get("type", "")).casefold() == wanted]
    if search and search.strip():
        needles = {fold(search), fold(_category_word(search))} - {""}
        items = [
            c for c in items
            if any(n in fold(f"{c.get('name', '')} {c.get('groupName', '')}") for n in needles)
        ]
    return items


_CATEGORY_TYPES = {
    "expense": "outgo", "expenses": "outgo", "ausgabe": "outgo", "ausgaben": "outgo",
    "aufwand": "outgo", "aufwendungen": "outgo", "kosten": "outgo", "outgo": "outgo",
    "einnahme": "income", "einnahmen": "income", "erlös": "income", "erlöse": "income",
    "erloese": "income", "revenue": "income", "ertrag": "income", "income": "income",
}


# -- Quotations -------------------------------------------------------------


def list_quotations(
    client: LexwareClient,
    *,
    status: str | None = None,
    number: str | None = None,
    contact_id: str | None = None,
    include_archived: bool = False,
    limit: int = 25,
    fetch_all: bool = False,
    date_from: str | None = None,
    date_to: str | None = None,
) -> ListResult:
    return _list_voucherlist(
        client,
        voucher_type="quotation",
        status=status,
        number=number,
        contact_id=contact_id,
        include_archived=include_archived,
        limit=limit,
        fetch_all=fetch_all,
        date_from=date_from,
        date_to=date_to,
    )


def get_quotation(client: LexwareClient, identifier: str) -> dict[str, Any]:
    return get_voucher_details(client, identifier, "quotation")


def download_quotation_pdf(client: LexwareClient, identifier: str) -> bytes:
    return download_pdf(client, identifier, "quotation")[0]


def create_quotation(client: LexwareClient, body: dict[str, Any]) -> dict[str, Any]:
    body = _prepare_doc(client, body, QUOTATION)
    dropped = [k for k in ("shippingConditions", "deliveryTerms") if k in body]
    for key in dropped:
        body.pop(key)  # Lexware ignores them on quotations
    created = _post_doc(client, "/v1/quotations", body)
    if dropped and isinstance(created, dict):
        created = {**created, "hinweis": (created.get("hinweis", "") + " " if created.get(
            "hinweis") else "") + (
            "Angebote haben in Lexware kein Lieferdatum und keine Lieferbedingung – "
            "diese Angaben wurden weggelassen. Sie lassen sich beim Weiterführen zum "
            "Auftrag mitgeben (changes: lieferdatum, deliveryTerms)."
        )}
    return created


# -- Order confirmations (Aufträge) ------------------------------------------


def list_order_confirmations(
    client: LexwareClient,
    *,
    status: str | None = None,
    number: str | None = None,
    contact_id: str | None = None,
    include_archived: bool = False,
    limit: int = 25,
    fetch_all: bool = False,
    date_from: str | None = None,
    date_to: str | None = None,
) -> ListResult:
    return _list_voucherlist(
        client,
        voucher_type="orderconfirmation",
        status=status,
        number=number,
        contact_id=contact_id,
        include_archived=include_archived,
        limit=limit,
        fetch_all=fetch_all,
        date_from=date_from,
        date_to=date_to,
    )


def get_order_confirmation(client: LexwareClient, identifier: str) -> dict[str, Any]:
    return get_voucher_details(client, identifier, "orderconfirmation")


def download_order_confirmation_pdf(client: LexwareClient, identifier: str) -> bytes:
    return download_pdf(client, identifier, "orderconfirmation")[0]


def create_order_confirmation(
    client: LexwareClient, body: dict[str, Any]
) -> dict[str, Any]:
    body = _prepare_doc(client, body, ORDER_CONFIRMATION)
    return _post_doc(client, "/v1/order-confirmations", body)


def article_line_item(
    article: dict[str, Any], quantity: float, *, net_price: float | None = None
) -> dict[str, Any]:
    """Build a `custom` line item from a raw article record.

    The Public API has no article-referencing line-item type, so name,
    description, unit and price are copied from the article. Prices are always
    sent net (the article record carries `netPrice` regardless of its leading
    price), which pairs with `taxConditions.taxType = "net"` in the document
    body. `net_price` overrides the article's list price (e.g. a manually
    edited price in the TUI form).
    """
    price = article.get("price") or {}
    item = {
        "type": "custom",
        "name": article.get("title") or article.get("articleNumber") or "Position",
        "quantity": quantity,
        "unitName": article.get("unitName") or "Stück",
        "unitPrice": {
            "currency": "EUR",
            "netAmount": net_price if net_price is not None else price.get("netPrice", 0),
            "taxRatePercentage": price.get("taxRate", 19),
        },
    }
    description = (article.get("description") or "").strip()
    if description:
        item["description"] = description
    return item


def build_order_confirmation_body(
    contact_id: str,
    line_items: list[dict[str, Any]],
    *,
    voucher_date: datetime | None = None,
    introduction: str | None = None,
    remark: str | None = None,
) -> dict[str, Any]:
    """Minimal valid POST body for /v1/order-confirmations (net prices).

    `voucher_date` defaults to now in the local timezone; the API requires the
    `yyyy-MM-ddTHH:mm:ss.SSSXXX` format that `isoformat(timespec="milliseconds")`
    produces for aware datetimes. `introduction`/`remark` are the document's
    Einleitungs- und Schlusstext; empty/None values are omitted.
    """
    when = (voucher_date or datetime.now()).astimezone()
    body = {
        "voucherDate": when.isoformat(timespec="milliseconds"),
        "address": {"contactId": contact_id},
        "lineItems": line_items,
        "totalPrice": {"currency": "EUR"},
        "taxConditions": {"taxType": "net"},
        "shippingConditions": {"shippingType": "none"},
    }
    if introduction and introduction.strip():
        body["introduction"] = introduction.strip()
    if remark and remark.strip():
        body["remark"] = remark.strip()
    return body


# -- Delivery notes ---------------------------------------------------------


def list_delivery_notes(
    client: LexwareClient,
    *,
    status: str | None = None,
    number: str | None = None,
    contact_id: str | None = None,
    include_archived: bool = False,
    limit: int = 25,
    fetch_all: bool = False,
    date_from: str | None = None,
    date_to: str | None = None,
) -> ListResult:
    return _list_voucherlist(
        client,
        voucher_type="deliverynote",
        status=status,
        number=number,
        contact_id=contact_id,
        include_archived=include_archived,
        limit=limit,
        fetch_all=fetch_all,
        date_from=date_from,
        date_to=date_to,
    )


def get_delivery_note(client: LexwareClient, identifier: str) -> dict[str, Any]:
    return get_voucher_details(client, identifier, "deliverynote")


def download_delivery_note_pdf(client: LexwareClient, identifier: str) -> bytes:
    return download_pdf(client, identifier, "deliverynote")[0]


def create_delivery_note(
    client: LexwareClient, body: dict[str, Any]
) -> dict[str, Any]:
    body = _prepare_doc(client, body, DELIVERY_NOTE)
    return _post_doc(client, "/v1/delivery-notes", body)


# -- Belegkette: einen Beleg in den Folgebeleg fortführen (pursue) -----------

# Human labels + the resource endpoint per document type.
_DOC_META: dict[str, dict[str, str]] = {
    "quotation": {"label": "Angebot", "art": "ein", "path": "/v1/quotations/{id}"},
    "orderconfirmation": {"label": "Auftrag", "art": "ein",
                          "path": "/v1/order-confirmations/{id}"},
    "deliverynote": {"label": "Lieferschein", "art": "ein",
                     "path": "/v1/delivery-notes/{id}"},
    "invoice": {"label": "Rechnung", "art": "eine", "path": "/v1/invoices/{id}"},
    "creditnote": {"label": "Rechnungskorrektur", "art": "eine",
                   "path": "/v1/credit-notes/{id}"},
    "dunning": {"label": "Mahnung", "art": "eine", "path": "/v1/dunnings/{id}"},
}


def _kind_phrase(doc_type: str | None) -> str:
    """"eine Rechnung", "ein Angebot", else "ein Beleg der Art …"."""
    if doc_type in _DOC_META:
        meta = _DOC_META[doc_type]
        return f"{meta['art']} {meta['label']}"
    if doc_type in ("salesinvoice", "salescreditnote", "purchaseinvoice",
                    "purchasecreditnote", "downpaymentinvoice"):
        return f"eine {_type_label(doc_type)}"
    return f"ein Beleg der Art {_type_label(doc_type)}"


_DEFINITE = {"quotation": "Das Angebot", "orderconfirmation": "Der Auftrag",
             "deliverynote": "Der Lieferschein", "invoice": "Die Rechnung",
             "creditnote": "Die Rechnungskorrektur", "dunning": "Die Mahnung"}


def _definite(doc_type: str | None) -> str:
    return _DEFINITE.get(doc_type or "", "Der Beleg")


def _with_article(doc_type: str) -> str:
    meta = _DOC_META.get(doc_type, {})
    article = meta.get("art", "ein")
    return f"{article[0].upper()}{article[1:]} {meta.get('label', doc_type)}"

# The follow-up transitions Lexware's API supports (mirrors the web app's
# "Weiterführen" menu). Nested: source type -> target type -> create endpoint.
# Abschlagsrechnung (down payment) and Serienrechnung are intentionally absent —
# the API cannot create those (down payment invoices are read-only).
_PURSUE_MATRIX: dict[str, dict[str, str]] = {
    "quotation": {
        "orderconfirmation": "/v1/order-confirmations",
        "deliverynote": "/v1/delivery-notes",
        "invoice": "/v1/invoices",
    },
    "orderconfirmation": {
        "deliverynote": "/v1/delivery-notes",
        "invoice": "/v1/invoices",
    },
    "deliverynote": {
        "invoice": "/v1/invoices",
    },
    "invoice": {
        "deliverynote": "/v1/delivery-notes",
        "creditnote": "/v1/credit-notes",
        "dunning": "/v1/dunnings",
    },
}

# Map human/German synonyms (and API type strings) to a normalized target type.
_TARGET_ALIASES = {
    "auftrag": "orderconfirmation",
    "auftragsbestätigung": "orderconfirmation",
    "auftragsbestaetigung": "orderconfirmation",
    "orderconfirmation": "orderconfirmation",
    "order_confirmation": "orderconfirmation",
    "order-confirmation": "orderconfirmation",
    "lieferschein": "deliverynote",
    "deliverynote": "deliverynote",
    "delivery_note": "deliverynote",
    "rechnung": "invoice",
    "invoice": "invoice",
    "rechnungskorrektur": "creditnote",
    "gutschrift": "creditnote",
    "korrektur": "creditnote",
    "creditnote": "creditnote",
    "credit_note": "creditnote",
    "invoices": "invoice",
    "schlussrechnung": "invoice",
    "teilrechnung": "invoice",
    "abschlussrechnung": "invoice",
    "endrechnung": "invoice",
    "ab": "orderconfirmation",
    "ls": "deliverynote",
    "re": "invoice",
    "rg": "invoice",
    "gs": "creditnote",
    "storno": "creditnote",
    "stornieren": "creditnote",
    "stornierung": "creditnote",
    "stornorechnung": "creditnote",
    "cancel": "creditnote",
    "cancellation": "creditnote",
    "ma": "dunning",
    "mahnen": "dunning",
    "erinnerung": "dunning",
    "rechnungen": "invoice",
    "lieferscheine": "deliverynote",
    "orderconfirmations": "orderconfirmation",
    "order": "orderconfirmation",
    "reminder": "dunning",
    "paymentreminder": "dunning",
    "mahnung": "dunning",
    "zahlungserinnerung": "dunning",
    "dunning": "dunning",
}

# Targets a user may ask for that the API cannot create (only the web app can).
_TARGET_UNSUPPORTED = {
    "abschlagsrechnung": "Abschlagsrechnung",
    "downpaymentinvoice": "Abschlagsrechnung",
    "down_payment_invoice": "Abschlagsrechnung",
    "serienrechnung": "Serienrechnung",
    "recurring": "Serienrechnung",
}

# voucherlist reports finalized invoices as "salesinvoice"; normalize to our key.
_SOURCE_TYPE_NORMALIZE = {"salesinvoice": "invoice"}

# Server-managed / computed fields, plus type-specific fields that must not be
# carried into the follow-up (e.g. a quotation's expirationDate has no place on
# an order confirmation or invoice).
_PURSUE_DROP_DOC = {
    "id",
    "organizationId",
    "version",
    "voucherNumber",
    "voucherStatus",
    "createdDate",
    "updatedDate",
    "files",
    "relatedVouchers",
    "archived",
    "taxAmounts",
    "expirationDate",
    "electronicDocumentProfile",
}
_PURSUE_DROP_LINEITEM = {"id", "lineItemAmount"}
# Document-level texts that must not carry over onto a dunning — Lexware fills
# its own dunning title/wording, so the invoice's would otherwise leak through.
_PURSUE_DROP_DUNNING = frozenset({"title", "introduction", "remark"})


def continue_document(
    client: LexwareClient,
    identifier: str,
    target: str,
    *,
    changes: dict[str, Any] | None = None,
    allow_duplicate: bool = False,
) -> dict[str, Any]:
    """Continue a sales voucher into its follow-up, preserving the chain.

    Detects the source document's type from `identifier` (UUID or voucher
    number), copies its content into a fresh draft of `target`, and POSTs it
    with ``precedingSalesVoucherId`` so Lexware links both in the Belegkette.
    Supported transitions mirror the Lexware web app (Angebot→Auftrag/
    Lieferschein/Rechnung, Auftrag→Lieferschein/Rechnung, Lieferschein→Rechnung,
    Rechnung→Lieferschein/Rechnungskorrektur). Invalid or unsupported steps are
    reported clearly instead of as a raw HTTP 406.
    """
    raw = target.strip().lower()
    compact = re.sub(r"[\s_-]+", "", raw)
    raw = raw if raw in _TARGET_ALIASES or raw in _TARGET_UNSUPPORTED else compact
    if raw in _TARGET_UNSUPPORTED:
        name = _TARGET_UNSUPPORTED[raw]
        raise LexwareError(
            f"{name} kann über die Schnittstelle nicht erstellt werden — das geht "
            "nur direkt in Lexware."
        )
    target_type = _TARGET_ALIASES.get(raw)
    if target_type is None and raw in ("angebot", "quotation", "ag", "offer", "quote"):
        raise LexwareError(
            "Ein Angebot kann nicht das Ziel einer Weiterführung sein – es steht am "
            "Anfang der Kette. Für ein ähnliches Angebot bitte ein neues anlegen."
        )
    if target_type is None:
        raise LexwareError(
            f"Unbekanntes Ziel '{target}'. Möglich sind: Auftrag, Lieferschein, "
            "Rechnung, Rechnungskorrektur oder Mahnung."
        )
    changes = _canonical_changes(changes)

    source_type, source_id, source = _resolve_source(client, identifier, target_type)
    _check_source_state(source, source_type, target_type, identifier)
    allowed = _PURSUE_MATRIX.get(source_type, {})
    target_path = allowed.get(target_type)
    if target_path is None:
        options = ", ".join(_DOC_META[t]["label"] for t in allowed) or "keine"
        raise LexwareError(
            f"{_with_article(source_type)} kann nicht zu "
            f"'{_DOC_META[target_type]['label']}' weitergeführt werden. Von hier "
            f"möglich: {options}."
        )
    _check_optional_positions(source, source_type)
    _check_unfit_changes(changes, target_type)
    number = source.get("voucherNumber") or identifier.strip()
    notes: list[str] = []
    partial_credit = target_type == "creditnote" and bool((changes or {}).get("lineItems"))
    is_dunning = target_type == "dunning"
    level = 0
    credits: list[dict[str, Any]] = []
    if is_dunning:
        dunnings = _dunning_states(client, source)
        drafts = [n for n, state in dunnings if state == "draft"]
        if drafts and not allow_duplicate:
            raise LexwareError(
                f"Zur Rechnung {number} gibt es schon einen Mahnungs-Entwurf "
                f"({', '.join(drafts)}). Es wurde keine weitere Mahnung angelegt – bitte den "
                "Entwurf in Lexware abschließen oder löschen. Für eine weitere Mahnung "
                f"trotzdem: continue_document({number}, \"Mahnung\", allow_duplicate=true)."
            )
        level = sum(1 for _, state in dunnings if state not in ("draft", "voided")) + 1
    elif target_type == "creditnote":
        credits = _real_followups(client, source, "creditnote", details=True)
        if credits and not partial_credit and not allow_duplicate:
            raise LexwareError(
                f"Zu {number} gibt es schon Rechnungskorrekturen: {_credit_list(credits)}. "
                "Eine weitere Voll-Gutschrift (ohne lineItems) über die ganze Rechnung wurde "
                "NICHT angelegt. Für eine Teilgutschrift changes.lineItems mitgeben; wenn es "
                "ausdrücklich gewünscht ist, mit allow_duplicate=true erneut aufrufen."
            )
        if credits:
            notes.append(f"Zu {number} gibt es bereits Rechnungskorrekturen: "
                         f"{_credit_list(credits)}.")
    elif not allow_duplicate:
        blocking = _chain_followups(client, source, source_id, target_type)
        if blocking:
            raise LexwareError(
                f"Zu {number} gibt es in der Belegkette schon: "
                f"{_DOC_META[target_type]['label']} {', '.join(blocking)}. Ein weiterer "
                "Folgebeleg wurde NICHT angelegt (Gefahr doppelter Rechnung/Lieferung). "
                "Für eine Teil-Lieferung/-Rechnung oder wenn es ausdrücklich gewünscht ist, "
                "mit allow_duplicate=true erneut aufrufen."
            )

    # A dunning is special: it references the invoice's existing line items by
    # their id (so the per-item `id` must be kept), and it must NOT inherit the
    # invoice's title/intro/closing text — Lexware applies its own dunning
    # wording, so those are dropped to avoid "Rechnung" leaking onto a Mahnung.
    body = _pursue_body(
        source,
        keep_line_item_ids=is_dunning,
        # the source's own wording ("Gerne bieten wir Ihnen an") does not fit
        # the follow-up — Lexware fills the target's standard texts instead
        extra_doc_drops=_PURSUE_DROP_DUNNING,
    )
    auto_ship = False
    if target_type == "invoice":
        ship = body.get("shippingConditions") or {}
        if not ship.get("shippingType") or ship.get("shippingType") == "none":
            # date of service is mandatory on an invoice (§ 14 UStG)
            body["shippingConditions"] = {
                "shippingType": "service", "shippingDate": body["voucherDate"]
            }
            auto_ship = True
    _english_payment_text(body)
    if is_dunning and level >= 2:
        body["title"] = f"{level}. Mahnung"
    if target_type in ("deliverynote", "dunning") or partial_credit:
        # no totals (delivery note) / own amounts (partial credit note) /
        # Lexware's own dunning amounts — the discount does not belong there
        for key in ("totalDiscountPercentage", "totalDiscountAbsolute"):
            (body.get("totalPrice") or {}).pop(key, None)
    if changes and any(k.casefold() in ("kundennummer", "customernumber")
                       for k in changes):
        raise LexwareError(
            "„kundennummer“ in changes ist mehrdeutig (Kunde wechseln oder Referenz beim "
            "Kunden?). Für einen anderen Kunden kunde/address angeben; eine Referenz bitte "
            "in den Text (introduction/remark) schreiben."
        )
    if changes and "lineItems" in changes:
        if is_dunning:
            raise LexwareError(
                "Eine Mahnung bezieht sich immer auf die Positionen der Rechnung – eigene "
                "Positionen (z. B. eine Mahngebühr) lassen sich über die Schnittstelle "
                "nicht anhängen. Mahngebühr bitte in Lexware ergänzen."
            )
        if not changes.get("lineItems"):
            raise LexwareError(
                "changes.lineItems ist leer – das ergäbe einen Beleg über 0 €. Positionen "
                "weglassen (dann werden die des Ausgangsbelegs übernommen) oder angeben."
            )
    if changes:
        old_address = dict(body.get("address") or {})
        old_contact = old_address.get("contactId")
        ship_given = _changes_ship(changes)
        body, change_notes = _apply_changes(client, body, changes, target_type)
        notes += change_notes
        _resolve_address_in(client, body)
        address = body.get("address") or {}
        if address != old_address and looks_like_uuid(str(address.get("contactId") or "")):
            # a name only counts when the caller wrote it (the source's printed
            # name may be older than the contact's current one)
            _complete_contact_address(
                client, body, notes,
                check_name=address.get("name") != old_address.get("name"))
        new_contact = (body.get("address") or {}).get("contactId")
        if new_contact != old_contact:
            changes = {**changes, "__customer_changed": True}
        if auto_ship and not ship_given:
            # the date of service follows the (changed) invoice date
            body["shippingConditions"] = {**body["shippingConditions"],
                                          "shippingDate": body["voucherDate"]}
    if target_type == "creditnote":
        _check_credit(body, source, credits, partial_credit)
    if source_type == "quotation":
        expires = _day(source.get("expirationDate"))
        if expires and expires < _today():
            notes.append(f"Hinweis: Das Angebot {number} war nur bis {expires:%d.%m.%Y} "
                         "gültig.")
    try:
        created = client.post(
            target_path, body, params={"precedingSalesVoucherId": source_id}
        )
    except LexwareAPIError as exc:
        if exc.status_code in (400, 406):
            message = _pursue_error(exc, source_type, target_type)
            tax = str((body.get("taxConditions") or {}).get("taxType") or "")
            if tax in _TAX_PREREQUISITES:
                message = f"{message} {_TAX_PREREQUISITES[tax]}"
            raise LexwareError(message) from exc
        raise
    return _pursue_result(client, created, source, source_type, target_type, level, changes,
                          notes, source_id)


def _canonical_changes(changes: Any) -> dict[str, Any] | None:
    """{"changes": {…}} unwrapped; positionen/positions/items → lineItems, so
    every check below sees the positions under one name."""
    if changes is None:
        return None
    if not isinstance(changes, dict):
        raise LexwareError("changes muss ein Objekt sein, z. B. {\"lieferdatum\": \"20.11.2026\"}.")
    out = dict(unwrap(changes, "changes"))
    for key in list(out):
        if key != "lineItems" and key.casefold() in (
                "lineitems", "positions", "positionen", "items", "line_items", "lines"):
            value = out.pop(key)
            out.setdefault("lineItems", value)
    return out


_PAYMENT_CHANGE_KEYS = frozenset({
    "zahlungsziel", "zahlungsbedingungen", "zahlungsbedingung", "paymentconditions",
    "payment_conditions", "paymenttermduration", "paymentterms", "payment_terms",
    "zahlungsfrist", "skonto", "skonto_prozent", "skontoprozent", "skonto_tage",
    "skontotage", "cashdiscount", "duedate", "due_date", "faelligkeit", "fälligkeit",
    "zahlbar_bis", "zahlbarbis", "faellig_am", "fällig_am", "payableuntil",
    "paymentdiscountconditions",
})
_DISCOUNT_CHANGE_KEYS = frozenset({
    "rabatt", "rabatt_prozent", "rabattprozent", "gesamtrabatt", "discount",
    "discountpercentage", "totaldiscountpercentage", "rabatt_betrag", "rabattbetrag",
    "totaldiscountabsolute",
})


def _check_unfit_changes(changes: dict[str, Any] | None, target_type: str) -> None:
    """Fields the target type has no place for must not vanish silently."""
    if not changes or target_type not in ("deliverynote", "dunning"):
        return
    keys = {k.casefold(): k for k in changes}
    bad = [orig for low, orig in keys.items()
           if low in _PAYMENT_CHANGE_KEYS or low in _DISCOUNT_CHANGE_KEYS]
    total = changes.get("totalPrice")
    if isinstance(total, dict) and any(k in total for k in ("totalDiscountPercentage",
                                                           "totalDiscountAbsolute")):
        bad.append("totalPrice (Rabatt)")
    if not bad:
        return
    if target_type == "deliverynote":
        raise LexwareError(
            f"{', '.join(bad)} wird bei Lieferscheinen nicht verwendet – ein Lieferschein hat "
            "keine Zahlungsbedingungen und keinen Rabatt. Bitte weglassen und beim "
            "Weiterführen zur Rechnung angeben."
        )
    raise LexwareError(
        f"{', '.join(bad)} wird bei Mahnungen nicht verwendet – eine Mahnung übernimmt "
        "Beträge und Zahlungsbedingungen der Rechnung. Bitte weglassen."
    )


def _changes_ship(changes: dict[str, Any]) -> bool:
    return any(k.casefold() in (
        "shippingconditions", "lieferdatum", "leistungsdatum", "deliverydate",
        "shippingdate", "liefertermin", "servicedate", "ausfuehrungsdatum",
        "ausführungsdatum", "zeitraum", "leistungszeitraum", "lieferzeitraum",
        "ausfuehrungszeitraum", "ausführungszeitraum", "serviceperiod", "deliveryperiod")
        for k in changes)


def _english_payment_text(body: dict[str, Any]) -> None:
    """An English document carrying our German standard payment text → English."""
    from lxw_cli.core.prepare import is_standard_label, payment_label

    cond = body.get("paymentConditions")
    if str(body.get("language") or "").casefold() != "en" or not isinstance(cond, dict):
        return
    label = str(cond.get("paymentTermLabel") or "")
    if label and not label.startswith("Payable") and is_standard_label(label) and isinstance(
            cond.get("paymentTermDuration"), int):
        cond["paymentTermLabel"] = payment_label(
            cond["paymentTermDuration"], cond.get("paymentDiscountConditions"), "en")


def _day(value: Any) -> Any:
    from lxw_cli.core.prepare import _day_of

    return _day_of(value)


def _today() -> Any:
    from lxw_cli.core.prepare import _now

    return _now().date()


def _credit_list(credits: list[dict[str, Any]]) -> str:
    parts = []
    for c in credits:
        amount = c.get("gross")
        parts.append(f"{c['number']}" + (f" ({amount:.2f} €)" if isinstance(
            amount, (int, float)) else ""))
    return ", ".join(parts)


def _line_gross(item: dict[str, Any], tax_type: str) -> float | None:
    price = item.get("unitPrice") if isinstance(item.get("unitPrice"), dict) else None
    if item.get("type") == "text":
        return 0.0
    if not price:
        return None
    quantity = item.get("quantity", 1)
    rate = price.get("taxRatePercentage") or 0
    if not all(isinstance(v, (int, float)) for v in (quantity, rate)):
        return None
    if price.get("grossAmount") is not None and tax_type == "gross":
        unit = price["grossAmount"]
    elif price.get("netAmount") is not None:
        unit = price["netAmount"] * (1 + rate / 100)
    elif price.get("grossAmount") is not None:
        unit = price["grossAmount"]
    else:
        return None
    if not isinstance(unit, (int, float)):
        return None
    discount = item.get("discountPercentage") or 0
    return float(unit) * float(quantity) * (1 - float(discount) / 100)


def _check_credit(
    body: dict[str, Any], source: dict[str, Any], credits: list[dict[str, Any]],
    partial: bool,
) -> None:
    """Before Lexware answers a bare "A validation error occurred.": credit note
    dated before the invoice, or more than is left of the invoice."""
    number = source.get("voucherNumber") or "der Rechnung"
    start, day = _day(source.get("voucherDate")), _day(body.get("voucherDate"))
    if start and day and day < start:
        raise LexwareError(
            f"Die Rechnungskorrektur ({day:%d.%m.%Y}) darf nicht vor dem Datum der Rechnung "
            f"{number} ({start:%d.%m.%Y}) liegen – bitte ein späteres Belegdatum angeben."
        )
    if not partial:
        return
    tax_type = str((body.get("taxConditions") or {}).get("taxType") or "net")
    lines = [_line_gross(i, tax_type) for i in body.get("lineItems") or []
             if isinstance(i, dict)]
    invoice_total = (source.get("totalPrice") or {}).get("totalGrossAmount")
    if None in lines or not isinstance(invoice_total, (int, float)):
        return
    already = sum(c["gross"] for c in credits if isinstance(c.get("gross"), (int, float)))
    left = round(invoice_total - already, 2)
    wanted = round(sum(lines), 2)
    if wanted > left + 0.01:
        detail = (f" (Rechnung {invoice_total:.2f} € abzüglich {_credit_list(credits)})"
                  if credits else "")
        raise LexwareError(
            f"Die Gutschrift ({wanted:.2f} € brutto) ist höher als der noch nicht "
            f"gutgeschriebene Betrag von {number}: {left:.2f} €{detail}. Lexware lehnt das ab "
            "– bitte den Betrag prüfen."
        )


_OPTIONAL_KEYS = ("optional", "alternative")


def _check_optional_positions(source: dict[str, Any], source_type: str) -> None:
    flagged = [
        str(i.get("name") or f"Position {n}")
        for n, i in enumerate(source.get("lineItems") or [], start=1)
        if isinstance(i, dict) and any(i.get(k) for k in _OPTIONAL_KEYS)
    ]
    if flagged:
        raise LexwareError(
            f"{_with_article(source_type)} mit optionalen oder alternativen Positionen "
            f"({', '.join(flagged[:5])}) lässt Lexware nicht über die Schnittstelle "
            "weiterführen. Bitte in Lexware weiterführen oder die Positionen dort "
            "entfernen bzw. zu festen Positionen machen."
        )


def _related_doc(client: LexwareClient, rel: dict[str, Any]) -> dict[str, Any] | None:
    meta = _DOC_META.get({"dun": "dunning"}.get(str(rel.get("voucherType", "")).lower(),
                                                str(rel.get("voucherType", "")).lower()))
    if not meta or not rel.get("id"):
        return None
    try:
        doc = client.get(meta["path"].format(id=rel["id"]))
    except Exception:  # unreadable
        return None
    return doc if isinstance(doc, dict) else None


def _real_followups(
    client: LexwareClient, source: dict[str, Any], target_type: str, *, details: bool = False
) -> list[Any]:
    """Related vouchers of the target type that really FOLLOW the source.

    relatedVouchers has no direction: an invoice made from LS0001 lists LS0001
    too. Predecessors (created before the source) and voided documents are
    left out. With `details` each hit is {number, gross} instead of the number.
    """
    found: list[Any] = []
    source_created = str(source.get("createdDate") or "")
    wanted = {"dunning": ("dun", "dunning")}.get(target_type, (target_type,))
    for rel in source.get("relatedVouchers") or []:
        if not isinstance(rel, dict) or str(rel.get("voucherType", "")).lower() not in wanted:
            continue
        doc = _related_doc(client, rel) or {}  # unreadable → count it, safer
        if doc.get("voucherStatus") == "voided":
            continue
        created = str(doc.get("createdDate") or "")
        if created and source_created and created < source_created:
            continue  # predecessor, e.g. the delivery note this invoice came from
        label = str(rel.get("voucherNumber") or rel.get("id"))
        if details:
            found.append({"number": label,
                          "gross": (doc.get("totalPrice") or {}).get("totalGrossAmount")})
        else:
            found.append(label)
    return found


def _chain_followups(
    client: LexwareClient, source: dict[str, Any], source_id: str, target_type: str
) -> list[str]:
    """Follow-ups of the target type next to the source in the chain: the
    source's own and those of its predecessors (AG → LS0037 and AG → RE:
    RE → Lieferschein would be a second delivery note). The source's own
    predecessor (RE made from LS0001) is no duplicate."""
    found = list(_real_followups(client, source, target_type))
    if target_type not in ("deliverynote", "invoice"):
        return found
    source_created = str(source.get("createdDate") or "")
    for rel in source.get("relatedVouchers") or []:
        if not isinstance(rel, dict) or rel.get("id") == source_id:
            continue
        rtype = str(rel.get("voucherType", "")).lower()
        if rtype not in ("quotation", "orderconfirmation", "deliverynote", "invoice"):
            continue
        doc = _related_doc(client, rel)
        if not doc or doc.get("voucherStatus") == "voided":
            continue
        created = str(doc.get("createdDate") or "")
        if not (created and source_created and created < source_created):
            continue  # only predecessors
        for rel2 in doc.get("relatedVouchers") or []:
            if (not isinstance(rel2, dict) or rel2.get("id") == source_id
                    or str(rel2.get("voucherType", "")).lower() != target_type):
                continue
            label2 = str(rel2.get("voucherNumber") or rel2.get("id"))
            if label2 in found:
                continue
            doc2 = _related_doc(client, rel2)
            if doc2 is None or doc2.get("voucherStatus") == "voided":
                continue
            linked = {str(r.get("id")) for r in doc2.get("relatedVouchers") or []
                      if isinstance(r, dict)}
            if source_id in linked:
                continue  # in the source's own line (its predecessor), no sibling
            if str(doc2.get("createdDate") or "") >= created:
                found.append(label2)
    return found


def _existing_followups(source: dict[str, Any], target_type: str) -> list[str]:
    """Numbers of follow-ups of `target_type` the source already has."""
    wanted = {"dunning": ("dun", "dunning")}.get(target_type, (target_type,))
    return [
        str(r.get("voucherNumber") or r.get("id"))
        for r in source.get("relatedVouchers") or []
        if isinstance(r, dict) and str(r.get("voucherType", "")).lower() in wanted
    ]


def _dunning_states(client: LexwareClient, source: dict[str, Any]) -> list[tuple[str, str]]:
    """(number, status) of the invoice's dunnings; unreadable → "unbekannt"."""
    out = []
    for rel in source.get("relatedVouchers") or []:
        if not isinstance(rel, dict) or str(rel.get("voucherType", "")).lower() not in (
                "dun", "dunning"):
            continue
        doc = _related_doc(client, rel)
        state = str((doc or {}).get("voucherStatus") or "unbekannt")
        out.append((str(rel.get("voucherNumber") or rel.get("id")), state))
    return out


def _resolve_address_in(client: LexwareClient, body: dict[str, Any]) -> None:
    """A customer given in `changes` by number/name → contactId."""
    address = body.get("address")
    if isinstance(address, dict):
        contact = str(address.get("contactId") or "").strip()
        if contact and not looks_like_uuid(contact):
            address["contactId"] = resolve_contact_id(client, contact)


def _apply_changes(
    client: LexwareClient, body: dict[str, Any], changes: dict[str, Any], target_type: str
) -> tuple[dict[str, Any], list[str]]:
    """Caller's adjustments for the follow-up (date, delivery date, payment
    terms, positions, texts) — normalized like a new document."""
    if not isinstance(changes, dict):
        raise LexwareError("changes muss ein Objekt sein, z. B. {\"lieferdatum\": \"20.11.2026\"}.")
    from lxw_cli.core.prepare import prepare_changes

    tax_type = str((body.get("taxConditions") or {}).get("taxType") or "net")
    language = body.get("language")
    currency = str((body.get("totalPrice") or {}).get("currency") or "EUR")
    patch = prepare_changes(
        {k: v for k, v in changes.items() if not k.startswith("__")}, target_type, tax_type,
        body.get("voucherDate"), client=client, language=language, currency=currency)
    notes = [n for n in (patch.pop("__hinweis", None),) if n]
    out = dict(body)
    for key, value in patch.items():
        if key == "address":
            out[key] = _merge_address(out.get("address"), value)
        elif key == "paymentConditions" and isinstance(value, dict):
            out[key] = _merge_payment(out.get(key), value, language, notes)
        elif key == "totalPrice" and isinstance(value, dict):
            out[key] = _merge_total(out.get(key), value)
        elif isinstance(value, dict) and isinstance(out.get(key), dict) and key != "lineItems":
            out[key] = {**out[key], **value}
        else:
            out[key] = value
    return out, notes


def _merge_address(old: Any, new: Any) -> Any:
    """changes.address: a new customer replaces it, a full one-off address too;
    single fields (supplement, contact person) are merged into the source's."""
    if not isinstance(new, dict) or not isinstance(old, dict):
        return new
    if new.get("contactId"):
        if new.get("contactId") == old.get("contactId"):
            return {**old, **new}
        return new
    if new.get("name") and (new.get("zip") or new.get("city") or new.get("street")):
        return new  # a complete one-off address
    return {**old, **new}


def _merge_payment(old: Any, new: dict[str, Any], language: Any, notes: list[str]) -> Any:
    """Payment changes onto the source's terms: Skonto stays unless removed,
    the label is rebuilt from term + Skonto (never "ohne Abzug" with Skonto)."""
    from lxw_cli.core.prepare import payment_label

    new = dict(new)
    auto = bool(new.pop("__autolabel", False))
    old = dict(old) if isinstance(old, dict) else {}
    merged = {**old, **new}
    removed = "paymentDiscountConditions" in new and new["paymentDiscountConditions"] is None
    if removed:
        merged.pop("paymentDiscountConditions", None)
        if old.get("paymentDiscountConditions"):
            notes.append("Skonto des Ausgangsbelegs entfernt.")
    kept = (not removed and "paymentDiscountConditions" not in new
            and isinstance(old.get("paymentDiscountConditions"), dict)
            and old["paymentDiscountConditions"].get("discountPercentage"))
    duration = merged.get("paymentTermDuration")
    if not isinstance(duration, int):
        raise LexwareError(
            "Für die Zahlungsbedingung fehlt die Frist – bitte zahlungsziel in Tagen angeben "
            "(z. B. 14)."
        )
    if "paymentTermLabel" not in new or auto:
        # not the caller's own text: rebuilt from term + (kept/new/removed) Skonto
        merged["paymentTermLabel"] = payment_label(
            duration, merged.get("paymentDiscountConditions"), language)
    if kept and "paymentTermDuration" in new:
        disc = old["paymentDiscountConditions"]
        notes.append(
            f"Skonto aus dem Ausgangsbeleg bleibt erhalten ({disc.get('discountPercentage')} % "
            f"bei Zahlung innerhalb von {disc.get('discountRange')} Tagen) – ohne Skonto: "
            "changes skonto: 0."
        )
    return merged


def _merge_total(old: Any, new: dict[str, Any]) -> dict[str, Any]:
    """Overall discount switched between % and € (never both); 0 removes it."""
    merged = {**(old if isinstance(old, dict) else {}),
              **{k: v for k, v in new.items() if k != "currency" or v}}
    if "totalDiscountAbsolute" in new:
        merged.pop("totalDiscountPercentage", None)
    if "totalDiscountPercentage" in new:
        merged.pop("totalDiscountAbsolute", None)
    for key in ("totalDiscountPercentage", "totalDiscountAbsolute"):
        if key in merged and merged[key] in (0, None):
            merged.pop(key)
    return merged


def _pursue_result(
    client: LexwareClient,
    created: Any,
    source: dict[str, Any],
    source_type: str,
    target_type: str,
    level: int,
    changes: dict[str, Any] | None,
    notes: list[str] | None = None,
    source_id: str | None = None,
) -> Any:
    """The new document's key data plus what the caller should know."""
    if not isinstance(created, dict) or not created.get("id"):
        return created
    try:
        doc = client.get(_DOC_META[target_type]["path"].format(id=created["id"]))
    except Exception:  # best effort — the document exists
        return created
    total = doc.get("totalPrice") or {}
    notes = list(notes or [])
    number = source.get("voucherNumber")
    if source_type == "quotation" and source.get("voucherStatus") == "draft":
        notes.append(f"Lexware hat das Angebot {number} dabei festgeschrieben "
                     "(Status „angenommen“).")
    if target_type == "creditnote" and not (changes or {}).get("lineItems"):
        notes.append(
            "Die Rechnungskorrektur übernimmt die VOLLE Rechnung "
            f"({total.get('totalGrossAmount')} € brutto) – das entspricht einem Storno. "
            "Für eine Teilgutschrift beim Weiterführen changes.lineItems mitgeben."
        )
    voucher_number = doc.get("voucherNumber")
    if target_type == "dunning":
        notes.append(f"Mahnstufe {level}.")
        if not voucher_number and source_id:
            voucher_number = _dunning_number_from_invoice(client, source_id, created["id"])
    if (changes or {}).get("__customer_changed"):
        notes.append("Achtung: Der Folgebeleg geht an einen ANDEREN Kunden als der "
                     f"Ausgangsbeleg {number}.")
    result = {
        "id": doc.get("id"),
        "voucherNumber": voucher_number,
        "voucherType": target_type,
        "voucherStatus": doc.get("voucherStatus"),
        "voucherDate": doc.get("voucherDate"),
        "title": doc.get("title"),
        "totalNetAmount": total.get("totalNetAmount"),
        "totalGrossAmount": total.get("totalGrossAmount"),
        "totalDiscountPercentage": total.get("totalDiscountPercentage"),
        "totalDiscountAbsolute": total.get("totalDiscountAbsolute"),
        "precedingVoucher": number,
        "resourceUri": created.get("resourceUri"),
    }
    result = {k: v for k, v in result.items() if v is not None}
    if notes:
        result["hinweis"] = " ".join(notes)
    return result


def _dunning_number_from_invoice(
    client: LexwareClient, invoice_id: str, dunning_id: str
) -> Any:
    """The dunning number ("RE0003_2") as the invoice lists it."""
    try:
        invoice = client.get(f"/v1/invoices/{invoice_id}")
    except Exception:  # best effort
        return None
    for rel in invoice.get("relatedVouchers") or []:
        if isinstance(rel, dict) and rel.get("id") == dunning_id:
            return rel.get("voucherNumber")
    return None


_STATE_TEXT = {"paid": "bereits bezahlt", "paidoff": "bereits ausgeglichen",
               "voided": "storniert", "draft": "noch ein Entwurf"}


def _check_source_state(
    source: dict[str, Any], source_type: str, target_type: str, identifier: str
) -> None:
    """Say why Lexware will refuse before it answers with a bare 406."""
    status = str(source.get("voucherStatus") or "")
    number = source.get("voucherNumber") or identifier.strip()
    the = _definite(source_type)
    if source_type == "dunning":
        raise LexwareError(
            "Zu einer Mahnung gibt es keine Rechnungskorrektur – bitte die gemahnte Rechnung "
            "angeben." if target_type == "creditnote" else
            "Eine Mahnung lässt sich nicht weiterführen – bitte die gemahnte Rechnung angeben."
        )
    if target_type == "dunning" and source_type != "invoice":
        raise LexwareError(
            f"Nur Rechnungen lassen sich mahnen – {number} ist {_kind_phrase(source_type)}."
            if source_type in _DOC_META else
            f"Nur Ausgangsrechnungen lassen sich mahnen – {number} ist kein solcher Beleg."
        )
    if target_type == "dunning" and status in _STATE_TEXT:
        hint = (
            " Erst in Lexware abschließen, dann mahnen." if status == "draft"
            else " Eine Mahnung ist nur für offene Rechnungen möglich."
        )
        raise LexwareError(f"{the} {number} ist {_STATE_TEXT[status]}.{hint}")
    if status == "voided":
        raise LexwareError(
            f"{the} {number} ist storniert und kann nicht weitergeführt werden."
        )
    if target_type == "creditnote" and status == "paidoff":
        raise LexwareError(
            f"{the} {number} ist bereits ausgeglichen (z. B. durch eine Rechnungskorrektur) – "
            "eine weitere Rechnungskorrektur lehnt Lexware ab."
        )
    if target_type == "creditnote" and status == "draft":
        raise LexwareError(
            f"{the} {number} ist noch ein Entwurf – eine Rechnungskorrektur geht "
            "nur zu einer abgeschlossenen Rechnung. Den Entwurf stattdessen in Lexware "
            "ändern oder löschen."
        )


def _resolve_source(
    client: LexwareClient, identifier: str, target_hint: str | None = None
) -> tuple[str, str, dict[str, Any]]:
    """Determine a source document's (normalized type, id, full body).

    A voucher number is looked up in the voucherlist (which reports the type).
    A UUID is probed against each sales-voucher endpoint until one answers.
    """
    ident = identifier.strip()
    if _looks_like_uuid(ident):
        for source_type, meta in _DOC_META.items():
            try:
                doc = client.get(meta["path"].format(id=ident))
            except LexwareAPIError as exc:
                if exc.status_code == 404:
                    continue
                raise
            return source_type, ident, doc
        raise LexwareError(
            f"Kein Beleg mit der ID '{identifier}' gefunden."
        )

    # Voucher number: the voucherlist row carries the type and the real id
    # (tolerant lookup: spaces, missing prefix, duplicates).
    prefer = ("invoice" if target_hint in ("dunning", "creditnote")
              else "quotation,orderconfirmation,deliverynote,invoice,creditnote")
    if target_hint == "dunning":
        # a dunning only ever belongs to an outgoing invoice — an ending like
        # "…-4" of a bookkeeping voucher must not win over RE0004
        try:
            match = find_voucher(client, ident, "invoice,salesinvoice", prefer="invoice")
        except LexwareError as first:
            try:
                other = find_voucher(client, ident, ALL_VOUCHER_TYPES, prefer=prefer)
            except LexwareError:
                raise first from None
            other_type = _SOURCE_TYPE_NORMALIZE.get(other.type or "", other.type)
            label = (other.row or {}).get("voucherNumber") or ident
            raise LexwareError(
                f"Nur Rechnungen lassen sich mahnen – {label} ist {_kind_phrase(other_type)}."
            ) from None
    else:
        match = find_voucher(client, ident, ALL_VOUCHER_TYPES, prefer=prefer)
    if match.ambiguous:
        raise LexwareError(
            f"{_candidates_text(match)} Bitte den Ausgangsbeleg mit voller Nummer oder id "
            "angeben (oder mit der Belegart, z. B. „Angebot 109“)."
        )
    row = match.row or {"id": match.id, "voucherType": match.type}
    raw_type = row.get("voucherType", "")
    source_type = _SOURCE_TYPE_NORMALIZE.get(raw_type, raw_type)
    meta = _DOC_META.get(source_type)
    if meta is None and target_hint == "dunning":
        raise LexwareError(
            f"{_type_label(raw_type)} {row.get('voucherNumber') or identifier} lässt "
            "sich nicht mahnen – nur eigene Ausgangsrechnungen."
        )
    if meta is None:
        raise LexwareError(
            f"{_type_label(raw_type)} {row.get('voucherNumber') or identifier} kann "
            "über die Schnittstelle nicht weitergeführt werden (nur Angebot, Auftrag, "
            "Lieferschein, Rechnung)."
        )
    try:
        doc = client.get(meta["path"].format(id=row["id"]))
    except LexwareAPIError as exc:
        if exc.status_code != 404 or raw_type != "salesinvoice":
            raise
        raise LexwareError(
            f"{row.get('voucherNumber') or identifier} ist ein Buchhaltungsbeleg "
            "(erfasste Ausgangsrechnung), keine in Lexware geschriebene Rechnung – er "
            "lässt sich nicht mahnen oder weiterführen."
        ) from exc
    return source_type, row["id"], doc


def _looks_like_uuid(value: str) -> bool:
    parts = value.split("-")
    return len(parts) == 5 and all(
        c in "0123456789abcdefABCDEF" for c in value.replace("-", "")
    )


def _pursue_error(exc: LexwareAPIError, source_type: str, target_type: str) -> str:
    """Turn a 400/406 from a pursue POST into a clear German message.

    Lexware uses 406 for three cases: body validation (with a `details` list),
    a not-yet-finalized preceding document, and a plain invalid transition.
    Surface the most specific explanation available.
    """
    to_label = _DOC_META[target_type]["label"]
    from_label = _DOC_META.get(source_type, {}).get("label", source_type)
    body = exc.body if isinstance(exc.body, dict) else {}

    details = body.get("details")
    if details:
        parts = [
            f"{d.get('field', '?')}: {d.get('message', '')}".strip(": ")
            for d in details
            if isinstance(d, dict)
        ]
        joined = "; ".join(p for p in parts if p)
        if joined:
            return f"Weiterführen zu '{to_label}' nicht möglich: {joined}."

    message = (body.get("message") or "").strip()
    if target_type == "creditnote" and re.match(r"^\s*A validation error occurred", message):
        return (
            "Lexware hat die Rechnungskorrektur abgelehnt (Prüffehler ohne Details) – "
            "häufig, weil sie höher ist als der noch offene bzw. nicht gutgeschriebene Betrag "
            "der Rechnung oder die Rechnung schon ausgeglichen ist. Bitte Betrag und "
            "Zahlungsstand der Rechnung prüfen."
        )
    if "finali" in message.lower():
        return (
            f"Der Ausgangsbeleg ({from_label}) ist noch ein Entwurf. Er muss in "
            f"Lexware erst festgeschrieben werden, bevor er zu '{to_label}' "
            "weitergeführt werden kann."
        )
    if message:
        from lxw_cli.core.client import _translate_text

        return f"Weiterführen zu '{to_label}' nicht möglich: {_translate_text(message, 406)}"

    return (
        f"Dieser {from_label} lässt sich gerade nicht zu '{to_label}' weiterführen "
        "(z. B. weil er storniert oder bereits weitergeführt ist)."
    )


def _pursue_body(
    source: dict[str, Any],
    *,
    keep_line_item_ids: bool = False,
    extra_doc_drops: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Turn a fetched document into a fresh create-body for its follow-up.

    Drops server-managed/computed fields, re-dates the document to today, keeps
    only the currency on ``totalPrice`` (amounts are recomputed server-side),
    and strips per-line-item ids/amounts. Content (address, line items, tax /
    shipping / payment conditions, texts, language) carries over unchanged.

    ``keep_line_item_ids`` retains each line item's ``id`` — required when
    pursuing to a dunning, which references the invoice's existing positions by
    id rather than creating new ones (Lexware rejects empty line-item ids there).
    ``extra_doc_drops`` removes additional document-level fields (e.g. a
    dunning must not inherit the invoice's title/introduction/remark).
    """
    drop_line_item = _PURSUE_DROP_LINEITEM
    if keep_line_item_ids:
        drop_line_item = drop_line_item - {"id"}
    doc_drops = _PURSUE_DROP_DOC | extra_doc_drops
    from lxw_cli.core.prepare import _format, _now

    body = {k: v for k, v in source.items() if k not in doc_drops}
    body["voucherDate"] = _format(_now())  # Berlin time, like a new document
    # For article positions (material/service) `id` is the ARTICLE id and must
    # stay — only free positions lose theirs. (Dropping it made every follow-up
    # of a document with an article fail: "lineItems[0].id darf nicht leer sein".)
    body["lineItems"] = [
        {
            k: v for k, v in item.items()
            if k not in drop_line_item
            or (k == "id" and item.get("type") in ("material", "service"))
        }
        for item in (body.get("lineItems") or [])
    ]
    source_total = source.get("totalPrice") or {}
    body["totalPrice"] = {"currency": source_total.get("currency", "EUR")}
    for key in ("totalDiscountPercentage", "totalDiscountAbsolute"):
        if source_total.get(key):
            body["totalPrice"][key] = source_total[key]
    # Ensure the follow-up's required blocks exist even when the source type
    # doesn't carry them (a quotation has no shippingConditions, but an order
    # confirmation / invoice requires one).
    if not body.get("taxConditions"):
        body["taxConditions"] = {"taxType": "net"}
    if not body.get("shippingConditions"):
        body["shippingConditions"] = {"shippingType": "none"}
    return body


# -- Dunnings (Mahnungen) ---------------------------------------------------
#
# The Lexware API has no way to *list* dunnings: /v1/voucherlist rejects
# voucherType=dunning (HTTP 400) and there is no /v1/dunnings collection GET.
# So dunnings are addressable only by id — obtained when the dunning is created
# or from the originating invoice's related documents. Creating one is a
# Belegkette pursue (invoice → dunning), reusing continue_document.


def create_dunning(client: LexwareClient, identifier: str) -> dict[str, Any]:
    """Create a dunning (Mahnung) from an overdue invoice, keeping both linked.

    `identifier` is the invoice's number or id. Delegates to the pursue logic
    (invoice → dunning): the invoice must be finalized and still have an open
    amount, otherwise a clear message is returned instead of a raw HTTP error.
    The dunning is created as a draft; while a dunning draft exists no further
    one is created. Returns the new dunning with its number ("RE0003_2").
    """
    try:
        return continue_document(client, identifier, "dunning")
    except LexwareError as exc:
        if "Kein Beleg" not in str(exc) or re.search(r"\d", identifier):
            raise  # a mistyped number is no customer name
        hint = _overdue_of_customer(client, identifier)
        if hint:
            raise LexwareError(hint) from exc
        raise


def _overdue_of_customer(client: LexwareClient, name: str) -> str | None:
    """For "Mahnung an Pixelwerk": list that customer's open (dunnable) invoices."""
    try:
        contact_id = resolve_contact_id(client, name)
        rows = list_invoices(client, status="overdue,open", contact_id=contact_id,
                             limit=25).items
    except LexwareError:
        return None
    # only invoices written in Lexware can be dunned — no bookkeeping vouchers
    rows = [r for r in rows if r.get("voucherType") == "invoice"][:10]
    if not rows:
        return f"'{name}' ist ein Kunde, aber er hat keine offene Rechnung, die sich mahnen lässt."
    listing = ", ".join(
        f"{r.get('voucherNumber')} ({r.get('openAmount', r.get('totalAmount'))} € offen, "
        f"fällig {str(r.get('dueDate') or '')[:10]}"
        + (", überfällig" if r.get("voucherStatus") == "overdue" else "") + ")"
        for r in rows
    )
    return (
        f"'{name}' ist ein Kunde, keine Rechnungsnummer. Offene Rechnungen: {listing}. "
        "Bitte die Mahnung mit der Rechnungsnummer anlegen."
    )


def get_dunning(client: LexwareClient, dunning_id: str) -> dict[str, Any]:
    """A dunning by its id, its number ("RE0001_2") or the invoice it belongs to."""
    text = str(dunning_id or "").strip()
    if looks_like_uuid(text):
        try:
            doc = client.get(f"/v1/dunnings/{text}")
        except LexwareAPIError as exc:
            if exc.status_code != 404:
                raise
        else:
            number = doc.get("voucherNumber") or _dunning_number_of(client, doc, text)
            return {**doc, "voucherNumber": number} if number else doc
    ref = _dunning_ref(client, text)
    doc = client.get(f"/v1/dunnings/{ref.id}")
    return _with_note({**doc, "voucherNumber": doc.get("voucherNumber") or ref.number},
                      ref.note)


def _dunning_number_of(client: LexwareClient, doc: dict[str, Any], dunning_id: str) -> Any:
    """A dunning's number ("RE0003_2") from the invoice's related vouchers."""
    for rel in doc.get("relatedVouchers") or []:
        if not isinstance(rel, dict) or str(rel.get("voucherType")).lower() != "invoice":
            continue
        try:
            invoice = client.get(f"/v1/invoices/{rel['id']}")
        except Exception:  # best effort
            return None
        for dun in invoice.get("relatedVouchers") or []:
            if isinstance(dun, dict) and dun.get("id") == dunning_id:
                return dun.get("voucherNumber")
    return None


def download_dunning_pdf(client: LexwareClient, dunning_id: str) -> bytes:
    """A dunning's PDF (id, number like "RE0001_2" or the invoice's number)."""
    text = str(dunning_id or "").strip()
    if not looks_like_uuid(text):
        text = _dunning_ref(client, text).id
    try:
        return get_pdf_with_voucher_fallback(client, f"/v1/dunnings/{text}/file", text)
    except LexwareAPIError as exc:
        if exc.status_code != 404:
            raise
        resolved = _dunning_ref(client, text).id
        if resolved == text:
            raise
        return get_pdf_with_voucher_fallback(
            client, f"/v1/dunnings/{resolved}/file", resolved
        )


@dataclass
class DunningRef:
    id: str
    number: str | None
    invoice: str
    note: str | None
    all: list[dict[str, Any]]


_DUNNABLE_TYPES = ("invoice", "salesinvoice", "downpaymentinvoice")


def _dunning_id(client: LexwareClient, value: str) -> tuple[str, str | None]:
    ref = _dunning_ref(client, value)
    return ref.id, ref.note


def _dunning_ref(client: LexwareClient, value: str) -> DunningRef:
    """Dunning from an id, a dunning number or the invoice it belongs to.

    Dunnings are not in the voucherlist, but the invoice lists them among its
    related vouchers ("RE0001_1", "RE0001_2"). Without a level the latest is
    taken.
    """
    text = str(value or "").strip()
    if not looks_like_uuid(text):
        _require_text(text)
    invoice_ref, wanted = text, None
    try:
        try:
            match = locate_voucher(client, invoice_ref, INVOICE_LIKE_TYPES)
        except LexwareError:
            m = _DUNNING_NUMBER.match(_clean_number(text))  # dunning number "RE0001_2"
            if not m:
                raise
            invoice_ref, wanted = m.group(1), _clean_number(text)
            match = locate_voucher(client, invoice_ref, INVOICE_LIKE_TYPES)
    except LexwareError as exc:
        raise LexwareError(
            f"Keine Mahnung zu '{text}' gefunden. Erwartet wird die id der Mahnung, "
            "ihre Nummer (z. B. RE0001_1) oder die Nummer der gemahnten Rechnung."
        ) from exc
    number = (match.row or {}).get("voucherNumber") or invoice_ref
    if match.type and match.type not in _DUNNABLE_TYPES:
        raise LexwareError(
            f"{number} ist {_kind_phrase(match.type)} – Mahnungen gibt es nur zu Rechnungen."
        )
    invoice = get_voucher_details(client, match.id, match.type or INVOICE_LIKE_TYPES)
    number = invoice.get("voucherNumber") or number
    dunnings = [
        r for r in invoice.get("relatedVouchers") or []
        # Lexware lists them as voucherType "dun" (seen live 10.11.2026)
        if isinstance(r, dict) and str(r.get("voucherType")).lower() in ("dun", "dunning")
    ]
    hint = re.search(r"ist ein Beleg der Art (.+?) \(", str(invoice.get("hinweis") or ""))
    found_type = next((t for t, label in VOUCHER_TYPE_LABELS.items()
                       if hint and label == hint.group(1)), None)
    if not dunnings and found_type and found_type not in _DUNNABLE_TYPES:
        raise LexwareError(
            f"{number} ist {_kind_phrase(found_type)} – Mahnungen gibt es nur zu Rechnungen."
        )
    if not dunnings:
        raise LexwareError(f"Zur Rechnung {number} gibt es keine Mahnung.")
    if wanted:
        exact = [r for r in dunnings if str(r.get("voucherNumber", "")).casefold()
                 == wanted.casefold()]
        if not exact:
            have = ", ".join(str(r.get("voucherNumber")) for r in dunnings)
            raise LexwareError(f"Mahnung {wanted} gibt es nicht. Vorhanden: {have}.")
        chosen = exact
    else:
        chosen = dunnings
    pick = chosen[-1]
    note = None if wanted else (
        f"Mahnung {pick.get('voucherNumber')} zur Rechnung {number}"
        + (
            f" (insgesamt {len(dunnings)} Mahnungen, gezeigt die letzte)."
            if len(dunnings) > 1
            else "."
        )
    )
    return DunningRef(id=str(pick["id"]), number=pick.get("voucherNumber"),
                      invoice=str(number), note=note, all=dunnings)


def _require_text(value: str) -> None:
    if not value:
        raise LexwareError(
            "Bitte die Mahnung angeben: ihre id, ihre Nummer (z. B. RE0001_1) oder die "
            "Nummer der gemahnten Rechnung."
        )


def _require_dunning_id(value: str) -> None:
    """Guard: dunnings are addressable only by id, never by a voucher number.

    Unlike invoices etc., dunnings are absent from /v1/voucherlist, so a number
    cannot be resolved to an id. Fail early with guidance instead of letting a
    number fall through to a confusing 404.
    """
    if not looks_like_uuid(value):
        raise LexwareError(
            "Eine Mahnung lässt sich nur über ihre id abrufen (nicht über eine "
            "Belegnummer). Die id steht im Ergebnis beim Erstellen der Mahnung "
            "oder bei den verknüpften Belegen der zugehörigen Rechnung."
        )


# -- Contacts ---------------------------------------------------------------


def list_contacts(
    client: LexwareClient,
    *,
    name: str | None = None,
    email: str | None = None,
    number: str | None = None,
    customer: bool = False,
    vendor: bool = False,
    include_archived: bool = False,
    limit: int = 25,
    fetch_all: bool = False,
) -> ListResult:
    """List contacts.

    The contacts API has no server-side archived filter, so by default we page
    through and drop archived ones client-side (reported via `hidden` /
    `exhausted`). With `include_archived` we stream the raw page set.
    """
    name = name.strip() if isinstance(name, str) and name.strip() else None
    email = email.strip() if isinstance(email, str) and email.strip() else None
    if isinstance(number, str) and number.strip():
        digits = re.sub(r"\D", "", number)
        number = digits or number.strip()
    else:
        number = None
    if (name and len(name) < 3) or (email and len(email) < 3):
        # Lexware's search needs >= 3 characters; search ourselves instead.
        return _search_contacts(
            client, name or email or "", customer=customer, vendor=vendor,
            include_archived=include_archived, limit=limit, fetch_all=fetch_all,
        )
    result = _list_contacts_raw(
        client, name=name, email=email, number=number, customer=customer,
        vendor=vendor, include_archived=include_archived, limit=limit,
        fetch_all=fetch_all,
    )
    variants = _spellings(name) if name else []
    if variants:
        # Lexware matches literally: "mueller" misses "Müller GmbH" — ask for
        # every spelling (ä/ae, ö/oe, ü/ue, ß/ss) and merge.
        max_items = resolve_max_items(limit, fetch_all)
        merged = {str(r.get("id")): r for r in result.items}
        for variant in variants:
            if max_items is not None and len(merged) >= max_items:
                break
            for row in _list_contacts_raw(
                client, name=variant, email=email, number=number, customer=customer,
                vendor=vendor, include_archived=include_archived, limit=limit,
                fetch_all=fetch_all,
            ).items:
                merged.setdefault(str(row.get("id")), row)
        if len(merged) > len(result.items):
            items = list(merged.values())
            if max_items is not None:
                items = items[:max_items]
            result = ListResult(items=items, search=True)
    if not result.items and (name or email):
        # Lexware matches the company/person name only, literally: no contact
        # persons, "Groesse" ≠ "Größe". Fall back to our own search.
        return _search_contacts(
            client, name or email or "", customer=customer, vendor=vendor,
            include_archived=include_archived, limit=limit, fetch_all=fetch_all,
        )
    return result


_UMLAUTS = (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("Ä", "Ae"), ("Ö", "Oe"), ("Ü", "Ue"),
            ("ß", "ss"))


def _spellings(name: str) -> list[str]:
    """The other spellings of a name: "Müller" ↔ "Mueller" (without the name itself)."""
    ascii_ = name
    for umlaut, plain in _UMLAUTS:
        ascii_ = ascii_.replace(umlaut, plain)
    umlaut_ = name
    for umlaut, plain in _UMLAUTS[:6]:
        umlaut_ = umlaut_.replace(plain, umlaut)
    return [v for v in dict.fromkeys((ascii_, umlaut_)) if v != name]


# Lexware stores contact names/e-mails HTML-escaped and expects the filter
# escaped the same way — "Mesh & Mörtel" only matches as "Mesh &amp; Mörtel".
def _html_escape(value: str | None) -> str | None:
    if value is None:
        return None
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


_CONTACT_SCAN_MAX = 2000


def _contact_texts(contact: dict[str, Any]) -> list[str]:
    company = contact.get("company") or {}
    person = contact.get("person") or {}
    full = " ".join(p for p in (person.get("firstName"), person.get("lastName")) if p)
    texts = [company.get("name"), full]
    for cp in company.get("contactPersons") or []:
        if isinstance(cp, dict):
            cp_full = " ".join(p for p in (cp.get("firstName"), cp.get("lastName")) if p)
            texts += [cp_full, cp.get("emailAddress")]
    for group in ("emailAddresses",):
        for values in (contact.get(group) or {}).values():
            texts += list(values or [])
    return [str(t).replace("&amp;", "&") for t in texts if t]


def _search_contacts(
    client: LexwareClient,
    needle: str,
    *,
    customer: bool,
    vendor: bool,
    include_archived: bool,
    limit: int,
    fetch_all: bool,
) -> ListResult:
    """Client-side contact search (names, contact persons, e-mails; ä = ae)."""
    wanted = fold(needle)
    max_items = resolve_max_items(limit, fetch_all)
    params: dict[str, Any] = {}
    if customer:
        params["customer"] = "true"
    if vendor:
        params["vendor"] = "true"
    found: list[dict[str, Any]] = []
    for index, contact in enumerate(client.paginate("/v1/contacts", params=params)):
        if index >= _CONTACT_SCAN_MAX:
            break
        if contact.get("archived") and not include_archived:
            continue
        haystack = " | ".join(fold(t) for t in _contact_texts(contact))
        if wanted and wanted in haystack:
            found.append(contact)
            if max_items is not None and len(found) >= max_items:
                break
    return ListResult(items=found, search=True)


def _list_contacts_raw(
    client: LexwareClient,
    *,
    name: str | None,
    email: str | None,
    number: str | None,
    customer: bool,
    vendor: bool,
    include_archived: bool,
    limit: int,
    fetch_all: bool,
) -> ListResult:
    params: dict[str, Any] = {
        "name": _html_escape(name), "email": _html_escape(email), "number": number
    }
    if customer:
        params["customer"] = "true"
    if vendor:
        params["vendor"] = "true"
    max_items = resolve_max_items(limit, fetch_all)
    meta: dict[str, Any] = {}
    if include_archived:
        items = list(
            client.paginate("/v1/contacts", params=params, max_items=max_items, meta=meta)
        )
        return ListResult(items=items, total=meta.get("total"))
    kept, hidden, exhausted = paginate_filtered(
        client,
        "/v1/contacts",
        params,
        max_items,
        lambda c: not c.get("archived"),
        meta=meta,
    )
    return ListResult(
        items=kept, total=meta.get("total"), hidden=hidden, exhausted=exhausted
    )


def get_contact(client: LexwareClient, contact_id: str) -> dict[str, Any]:
    """Contact by id — or by customer/vendor number or unique name."""
    contact_id = resolve_contact_id(client, contact_id)
    return _get_or_404(client, f"/v1/contacts/{contact_id}", "Kontakt")


def _get_or_404(client: LexwareClient, path: str, what: str) -> dict[str, Any]:
    try:
        return client.get(path)
    except LexwareAPIError as exc:
        if exc.status_code == 404:
            ident = path.rstrip("/").rsplit("/", 1)[-1]
            raise LexwareError(
                f"Kein {what} mit der id {ident} gefunden. Die id ist vermutlich die "
                f"eines anderen Datensatzes (z. B. eines Belegs) oder falsch kopiert."
            ) from exc
        raise


def create_contact(client: LexwareClient, body: dict[str, Any]) -> dict[str, Any]:
    allow = False
    if isinstance(body, dict):
        body = dict(unwrap(body, "body", "contact", "kontakt"))
        for key in ("allow_duplicate", "allowDuplicate", "doppelt_erlaubt"):
            if key in body:
                allow = bool_word(body.pop(key)) is True
    body = normalize_contact(body)
    validate_contact(body)
    if not allow:
        name = _contact_label(body) if (body.get("company") or body.get("person")) else ""
        same = _exact_contacts(client, name) if name and name != "None" else []
        if same:
            raise LexwareError(
                f"Einen Kontakt '{name}' gibt es schon: "
                + "; ".join(_contact_brief(r) for r in same)
                + ". Er wurde NICHT neu angelegt. Bitte den vorhandenen verwenden (bzw. mit "
                "update_contact ändern) – für einen weiteren Kontakt mit demselben Namen im "
                "body \"allow_duplicate\": true angeben."
            )
    created = client.post("/v1/contacts", body)
    return _reread(client, "/v1/contacts", created)


def _reread(client: LexwareClient, base: str, created: Any) -> Any:
    """Return the stored object after create (Lexware only answers with ids)."""
    if isinstance(created, dict) and created.get("id"):
        try:
            return client.get(f"{base}/{created['id']}")
        except Exception:  # best effort — the create itself succeeded
            return created
    return created


def update_contact(
    client: LexwareClient, contact_id: str, changes: dict[str, Any]
) -> dict[str, Any]:
    """Partially update a contact: fetch, deep-merge, PUT the full object.

    Lexware's PUT replaces the whole contact and requires the current
    ``version`` (optimistic locking). We therefore always fetch the live
    contact first, merge the caller's ``changes`` onto it, and force the
    just-fetched version — so the caller only ever supplies the fields that
    change and can never break the version handshake.
    """
    contact_id = resolve_contact_id(client, contact_id)
    current = _get_or_404(client, f"/v1/contacts/{contact_id}", "Kontakt")
    changes = dict(unwrap(changes, "changes")) if isinstance(changes, dict) else changes
    if not changes:
        return {**current, "hinweis": "Keine Änderungen übergeben – nichts gespeichert."}
    changes = _guard_archived_unchanged(current, changes)
    for key in CONTACT_KIND_KEYS:
        if key in changes:
            kind = contact_kind(changes.pop(key))
            is_person = bool(current.get("person")) and not current.get("company")
            if kind and (kind == "person") != is_person:
                raise LexwareError(
                    "Die Art des Kontakts (Privatperson/Firma) lässt sich über die Lexware-API "
                    "nicht ändern. Bitte einen neuen Kontakt der richtigen Art anlegen."
                )
    if "name" in changes and current.get("person") and not current.get("company"):
        full = str(changes.pop("name") or "").strip().split()
        person: dict[str, Any] = {"lastName": full[-1]} if full else {}
        if len(full) > 1:
            person["firstName"] = " ".join(full[:-1])
        changes["person"] = {**(changes.get("person") or {}), **person}
    changes = _merge_billing(current, changes)
    changes = normalize_contact(changes, partial=True)
    notes = _merge_contact_person(current, changes) + _replaced_contact_data(current, changes)
    _guard_roles_kept(current, changes)
    if "roles" in changes and isinstance(changes["roles"], dict):
        # roles merge: an existing role keeps its number
        changes["roles"] = {**(current.get("roles") or {}), **changes["roles"]}
    if "company" in changes and current.get("person") and not current.get("company"):
        raise LexwareError(
            "Dieser Kontakt ist eine Privatperson; die Art (Person/Firma) lässt sich "
            "über die Lexware-API nicht ändern. Bitte einen neuen Firmenkontakt anlegen."
        )
    if "person" in changes and current.get("company") and not current.get("person"):
        raise LexwareError(
            "Dieser Kontakt ist eine Firma; die Art (Person/Firma) lässt sich über die "
            "Lexware-API nicht ändern. Ansprechpartner gehören unter "
            "company.contactPersons."
        )
    merged = _merge_for_update(current, changes)
    _guard_single_contact_person(merged)
    validate_contact(merged)
    result = _put_and_reread(client, f"/v1/contacts/{contact_id}", merged, current)
    if notes and isinstance(result, dict):
        result = _with_note(result, " ".join(notes))
    return result


def _person_name(person: dict[str, Any], *, accusative: bool = False) -> str:
    salutation = str(person.get("salutation") or "").strip()
    if accusative and salutation == "Herr":
        salutation = "Herrn"
    return " ".join(p for p in (salutation, person.get("firstName"), person.get("lastName"))
                    if p)


def _merge_contact_person(current: dict[str, Any], changes: dict[str, Any]) -> list[str]:
    """Lexware keeps ONE contact person: the same person is merged (salutation,
    e-mail, phone stay), another one replaces the old one — said in a note."""
    new_list = (changes.get("company") or {}).get("contactPersons")
    old_list = (current.get("company") or {}).get("contactPersons") or []
    if not (isinstance(new_list, list) and len(new_list) == 1 and len(old_list) == 1
            and isinstance(new_list[0], dict) and isinstance(old_list[0], dict)):
        return []
    new, old = new_list[0], old_list[0]
    named = new.get("firstName") or new.get("lastName")
    same = not named or (
        fold(str(new.get("lastName") or old.get("lastName") or ""))
        == fold(str(old.get("lastName") or ""))
        and fold(str(new.get("firstName") or old.get("firstName") or ""))
        == fold(str(old.get("firstName") or "")))
    if same:
        changes["company"]["contactPersons"] = [{**old, **new}]
        return []
    return [f"Lexware erlaubt nur einen Ansprechpartner: {_person_name(old)} wurde durch "
            f"{_person_name(new, accusative=True)} ersetzt."]


def _replaced_contact_data(current: dict[str, Any], changes: dict[str, Any]) -> list[str]:
    """A new e-mail/phone of a kind replaces the old one (one per kind)."""
    notes = []
    for group, what in (("emailAddresses", "E-Mail"), ("phoneNumbers", "Telefonnummer")):
        new_block = changes.get(group)
        old_block = current.get(group) or {}
        if not isinstance(new_block, dict) or not isinstance(old_block, dict):
            continue
        for sub, values in new_block.items():
            old = [str(v) for v in old_block.get(sub) or []]
            new = [str(v) for v in values or []]
            gone = [v for v in old if v not in new]
            if gone and new:
                notes.append(f"Die bisherige {what} ({sub}) {', '.join(gone)} wurde durch "
                             f"{', '.join(new)} ersetzt.")
    return notes


_FLAT_ADDRESS = ("street", "zip", "city", "country", "countryCode", "supplement")


def _merge_billing(current: dict[str, Any], changes: dict[str, Any]) -> dict[str, Any]:
    """A partial billing or shipping address (flat, as one object or as text
    "Straße 1, 12345 Ort") keeps the other fields.

    Lists are replaced wholesale on update, so {"city": "Köln"} alone would
    drop street, zip and country — merge it onto the current first address.
    """
    from lxw_cli.core.normalize import (
        BILLING_KEYS,
        SHIPPING_KEYS,
        address_entry,
        country_code,
    )

    changes = dict(changes)
    parts: dict[str, dict[str, Any]] = {}
    for keys, kind in ((BILLING_KEYS, "billing"), (SHIPPING_KEYS, "shipping")):
        for key in keys:
            if key in changes and isinstance(changes[key], (dict, str, list)):
                entry = address_entry(changes.pop(key))
                if isinstance(entry, dict):
                    parts[kind] = {**parts.get(kind, {}), **entry}
    addresses_in = changes.get("addresses")
    if isinstance(addresses_in, list):
        addresses_in = {"billing": addresses_in}
    if isinstance(addresses_in, dict):
        addresses_in = dict(addresses_in)
        for kind in ("billing", "shipping"):
            if isinstance(addresses_in.get(kind), (dict, list, str)):
                entry = address_entry(addresses_in[kind])
                if isinstance(entry, dict):
                    addresses_in.pop(kind)
                    parts[kind] = {**parts.get(kind, {}), **entry}
    for german, key in (("strasse", "street"), ("straße", "street"), ("plz", "zip"),
                        ("postleitzahl", "zip"), ("ort", "city"), ("stadt", "city"),
                        ("land", "country"), ("adresszusatz", "supplement")):
        if german in changes and key not in changes:
            changes[key] = changes.pop(german)
    flat = {k: changes[k] for k in _FLAT_ADDRESS if k in changes}
    if flat:
        parts["billing"] = {**parts.get("billing", {}), **flat}
    if not parts:
        return changes
    current_addresses = current.get("addresses") or {}
    out = {k: v for k, v in changes.items() if k not in _FLAT_ADDRESS}
    merged_addresses = dict(addresses_in) if isinstance(addresses_in, dict) else {}
    for kind, part in parts.items():
        part = dict(part)
        if "country" in part:
            part["countryCode"] = part.pop("country")
        if part.get("countryCode"):
            part["countryCode"] = country_code(part["countryCode"])
        have = current_addresses.get(kind) or []
        base = have[0] if have and isinstance(have[0], dict) else {}
        if not base and kind == "shipping":
            part.setdefault("countryCode", _billing(current).get("countryCode") or "DE")
        merged_addresses[kind] = [{**base, **part}]
    out["addresses"] = merged_addresses
    return out


def _guard_roles_kept(current: dict[str, Any], changes: dict[str, Any]) -> None:
    """Refuse removing a role: Lexware would drop the customer/vendor number."""
    roles = changes.get("roles")
    if not isinstance(roles, dict):
        return
    for name, value in roles.items():
        if value is None and name in (current.get("roles") or {}):
            label = "Kunden" if name == "customer" else "Lieferanten"
            raise LexwareError(
                f"Die {label}-Rolle wird nicht entfernt: Lexware würde dabei die "
                f"{label}nummer verwerfen und beim erneuten Setzen eine neue vergeben. "
                "Es wurde nichts geändert. Rollen lassen sich in der "
                "Lexware-Weboberfläche anpassen."
            )


def _guard_archived_unchanged(
    current: dict[str, Any], changes: dict[str, Any]
) -> dict[str, Any]:
    """Refuse an attempt to (un)archive a contact instead of faking success.

    `archived` is read-only in the Lexware API: a PUT carrying a different
    value is accepted with HTTP 200 but silently ignored, so the update would
    report success while nothing changed. A value equal to the current one
    (e.g. a full contact passed back as `changes`) is a harmless no-op and is
    simply dropped.
    """
    if "archived" not in changes:
        return changes
    if bool(changes["archived"]) != bool(current.get("archived")):
        raise LexwareError(
            "Kontakte lassen sich über die Lexware-API nicht archivieren oder "
            "wiederherstellen — das geht nur direkt in der Lexware-Weboberfläche. "
            "Es wurde nichts geändert."
        )
    return {k: v for k, v in changes.items() if k != "archived"}


def _guard_single_contact_person(contact: dict[str, Any]) -> None:
    """Fail early on contacts the Lexware REST API refuses to update.

    A company contact may hold multiple contact persons in the Lexware web
    app, but the REST API accepts **at most one** on create/update — a PUT with
    more returns an opaque HTTP 406 (`invalid_value` on `contactPersons.size`).
    Since our update always PUTs the full object, such a contact can't be
    edited via the API at all, even for an unrelated field. Turn that into a
    clear, actionable message instead of a raw 406.
    """
    persons = (contact.get("company") or {}).get("contactPersons") or []
    if len(persons) > 1:
        raise LexwareError(
            "Dieser Kontakt hat mehrere Ansprechpartner. Die Lexware-API erlaubt "
            "pro Firmen-Kontakt nur einen, deshalb lassen sich solche Kontakte "
            "nicht über die API bearbeiten (auch nicht für andere Felder). Bitte "
            "die Änderung direkt in der Lexware-Weboberfläche vornehmen. Für "
            "weitere Ansprechpartner empfiehlt sich ein eigener Kontakt."
        )


# -- Articles ---------------------------------------------------------------


def list_articles(
    client: LexwareClient,
    *,
    search: str | None = None,
    article_type: str | None = None,
    article_number: str | None = None,
    gtin: str | None = None,
    limit: int = 25,
    fetch_all: bool = False,
) -> ListResult:
    """List articles, or free-text search them client-side when `search` is set."""
    max_items = resolve_max_items(limit, fetch_all)
    article_type = normalize_article_type(article_type)
    if search:
        items = search_articles(
            client,
            search,
            article_type=article_type,
            gtin=gtin,
            max_items=max_items,
        )
        return ListResult(items=items, search=True)
    meta: dict[str, Any] = {}
    article_number = article_number.strip() if article_number else None
    items = list(
        client.paginate(
            "/v1/articles",
            params={
                "type": article_type,
                "articleNumber": article_number,
                "gtin": gtin,
            },
            max_items=max_items,
            meta=meta,
        )
    )
    if not items and article_number:
        items = [
            a for a in _articles_by_number_or_title(client, article_number)
            if not article_type or a.get("type") == article_type
        ]
        return ListResult(items=items, search=True)
    return ListResult(items=items, total=meta.get("total"))


def resolve_article_id(client: LexwareClient, identifier: str) -> str:
    text = str(identifier or "").strip().strip("'\"„“‚‘`").strip()
    if not text:
        raise LexwareError("Bitte eine Artikelnummer, eine Artikel-id oder den Namen angeben.")
    return _resolve_article_id(client, text)


def _resolve_article_id(client: LexwareClient, identifier: str) -> str:
    """Resolve an article identifier (UUID or article number) to its UUID.

    Mirrors :func:`resolve_voucher_id`: a UUID is returned unchanged, anything
    else is looked up as an exact article number.
    """
    identifier = identifier.strip()
    if looks_like_uuid(identifier):
        return identifier
    matches = [] if len(identifier) > 18 else list(  # numbers have at most 18 chars
        client.paginate(
            "/v1/articles", params={"articleNumber": identifier}, max_items=2
        )
    )
    if not matches:
        matches = _articles_by_number_or_title(client, identifier)
    if not matches:
        raise LexwareError(
            f"Kein Artikel mit Artikelnummer oder Bezeichnung '{identifier}' gefunden. "
            "Für eine Teilsuche die Artikelliste mit Suchbegriff verwenden."
        )
    _article_conflict(matches, identifier)
    return matches[0]["id"]


def _article_conflict(hits: list[dict[str, Any]], identifier: str) -> None:
    if len(hits) > 1:
        listing = "; ".join(f"{a.get('articleNumber')} „{a.get('title')}“ (id {a.get('id')})"
                            for a in hits[:6])
        raise LexwareError(
            f"Mehrere Artikel passen zu '{identifier}': {listing}. Bitte die id angeben."
        )


def _articles_by_number_or_title(
    client: LexwareClient, identifier: str
) -> list[dict[str, Any]]:
    """Second try: article number ignoring case, then exact title (ä = ae)."""
    wanted = identifier.casefold()
    wanted_fold = fold(identifier)
    by_number: list[dict[str, Any]] = []
    by_title: list[dict[str, Any]] = []
    for article in client.paginate("/v1/articles"):
        if str(article.get("articleNumber") or "").casefold() == wanted:
            by_number.append(article)
        elif fold(str(article.get("title") or "")) == wanted_fold:
            by_title.append(article)
    return by_number or by_title


def get_article(client: LexwareClient, identifier: str) -> dict[str, Any]:
    """Article details by UUID, article number (any case) or exact title."""
    article_id = resolve_article_id(client, identifier)
    return _get_or_404(client, f"/v1/articles/{article_id}", "Artikel")


def _price_change_check(changes: Any, current: Any) -> None:
    """"+5%" / "10 % mehr" as a price: a change, never silently a price of 5 €."""
    from lxw_cli.core.normalize import is_change

    if not isinstance(changes, dict):
        return
    price = changes.get("price", changes.get("preis"))
    values = dict(price) if isinstance(price, dict) else (
        {"netPrice": price} if price is not None else {})
    for key in ("netPrice", "grossPrice", "netto", "brutto", "nettopreis", "bruttopreis"):
        if key in changes:
            values[key] = changes[key]
    now = current.get("price") if isinstance(current, dict) else None
    for key, value in values.items():
        if isinstance(value, str) and (is_change(value) or "%" in value):
            side = "grossPrice" if "gross" in key or "brutto" in key else "netPrice"
            have = (now or {}).get(side) if isinstance(now, dict) else None
            current_text = (f" (aktuell {have:.2f} €)".replace(".", ",")
                            if isinstance(have, (int, float)) else "")
            raise LexwareError(
                f"Preis '{value}' ist eine Änderung, kein Preis – bitte den neuen Preis "
                f"angeben{current_text}."
            )


def create_article(client: LexwareClient, body: dict[str, Any]) -> dict[str, Any]:
    body = unwrap(body, "body", "article", "artikel")
    _price_change_check(body, None)
    body = normalize_article(body, article_type_map=_ARTICLE_TYPES)
    _check_price_consistency(body.get("price"), None)
    if isinstance(body.get("price"), dict) and "taxRate" not in body["price"]:
        raise LexwareError(
            "Bitte den Steuersatz des Artikels angeben: price.taxRate 19, 7 oder 0 "
            "(z. B. {\"netPrice\": 19.9, \"taxRate\": 19})."
        )
    if isinstance(body.get("price"), dict):
        given = dict(body["price"])
        body["price"] = lead_from_given(body["price"], given)
    validate_article(body)
    created = client.post("/v1/articles", body)
    return _reread(client, "/v1/articles", created)


def update_article(
    client: LexwareClient, article_id: str, changes: dict[str, Any]
) -> dict[str, Any]:
    """Partially update an article — same fetch/deep-merge/PUT flow as
    :func:`update_contact` (articles also use ``version`` locking)."""
    article_id = resolve_article_id(client, article_id)
    current = _get_or_404(client, f"/v1/articles/{article_id}", "Artikel")
    if not changes:
        return {**current, "hinweis": "Keine Änderungen übergeben – nichts gespeichert."}
    if "archived" in changes:
        if bool(changes["archived"]) != bool(current.get("archived")):
            raise LexwareError(
                "Artikel lassen sich über die Lexware-API nicht archivieren oder "
                "wiederherstellen — das geht nur in der Lexware-Weboberfläche. Es wurde "
                "nichts geändert."
            )
        changes = {k: v for k, v in changes.items() if k != "archived"}
    changes = unwrap(changes, "changes")
    _price_change_check(changes, current)
    changes = normalize_article(changes, article_type_map=_ARTICLE_TYPES)
    _drop_echoed_price(changes.get("price"), current.get("price"))
    _check_price_consistency(changes.get("price"), current.get("price"))
    merged = _merge_for_update(current, changes)
    if isinstance(changes.get("price"), dict) and isinstance(merged.get("price"), dict):
        merged["price"] = lead_from_given(merged["price"], changes["price"])
    validate_article(merged)
    return _put_and_reread(client, f"/v1/articles/{article_id}", merged, current)


def _merge_for_update(current: dict[str, Any], changes: dict[str, Any]) -> dict[str, Any]:
    """Deep-merge ``changes`` onto a freshly fetched resource for a PUT.

    Nested objects merge key-by-key; lists and scalars are replaced wholesale
    (a list can't be meaningfully half-merged — pass the full list to change
    one entry). The resource's own ``version`` always wins, so a caller who
    echoes a stale version in ``changes`` can't trigger a false 409.
    """
    merged = _deep_merge(current, changes)
    if "version" in current:
        merged["version"] = current["version"]
    return merged


def _put_and_return_object(
    client: LexwareClient, path: str, body: dict[str, Any]
) -> dict[str, Any]:
    """PUT ``body`` and return the full updated object, not Lexware's envelope.

    Lexware's PUT responds with only a confirmation ({id, resourceUri, version,
    createdDate, updatedDate}). The full object we just stored is far more
    useful to the caller, so we return ``body`` with the new version stamped
    in — no extra GET needed.
    """
    result = client.put(path, body)
    updated = dict(body)
    if isinstance(result, dict):
        for key in ("version", "updatedDate", "id", "resourceUri"):
            if key in result:
                updated[key] = result[key]
    return updated


def _drop_echoed_price(price: Any, current: Any) -> None:
    """A full article echoed back carries the old non-leading price — Lexware
    ignores it (it is computed), so it must not count as a contradiction."""
    if not isinstance(price, dict) or not isinstance(current, dict):
        return
    if "netPrice" not in price or "grossPrice" not in price:
        return
    leading = str(price.get("leadingPrice") or current.get("leadingPrice") or "").upper()
    computed = "grossPrice" if leading == "NET" else "netPrice" if leading == "GROSS" else None
    if computed and price.get(computed) == current.get(computed):
        price.pop(computed)


def _check_price_consistency(price: Any, current: Any) -> None:
    """Net and gross given together must match the tax rate."""
    if not isinstance(price, dict):
        return
    net, gross = price.get("netPrice"), price.get("grossPrice")
    rate = price.get("taxRate", (current or {}).get("taxRate") if isinstance(current, dict)
                     else None)
    if not all(isinstance(v, (int, float)) for v in (net, gross, rate)):
        return
    expected = round(net * (1 + rate / 100), 2)
    if abs(expected - gross) > 0.02:
        raise LexwareError(
            f"Netto {net} und Brutto {gross} passen bei {rate} % nicht zusammen "
            f"(netto {net} ergibt brutto {expected}). Bitte nur einen der beiden Preise "
            "angeben – der andere wird berechnet."
        )


def _put_and_reread(
    client: LexwareClient, path: str, body: dict[str, Any], before: dict[str, Any]
) -> dict[str, Any]:
    """PUT, then return what Lexware actually stored (not what we sent).

    Lexware silently ignores some fields (e.g. a net price when the gross
    price leads). Returning the stored object — and saying when nothing
    changed — keeps the caller from reporting a change that never happened.
    """
    result = client.put(path, body)
    try:
        stored = client.get(path)
    except Exception:  # best effort — the PUT itself succeeded
        return _put_result(body, result)
    if _comparable(stored) == _comparable(before):
        return {
            **stored,
            "hinweis": "Lexware hat nichts geändert – die übergebenen Werte "
            "entsprechen dem bisherigen Stand oder werden von Lexware für diesen "
            "Datensatz nicht übernommen.",
        }
    return stored


def _put_result(body: dict[str, Any], result: Any) -> dict[str, Any]:
    updated = dict(body)
    if isinstance(result, dict):
        for key in ("version", "updatedDate", "id", "resourceUri"):
            if key in result:
                updated[key] = result[key]
    return updated


_VOLATILE = {"version", "updatedDate", "createdDate"}


def _comparable(obj: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in obj.items() if k not in _VOLATILE}


def _deep_merge(base: Any, changes: Any) -> Any:
    if isinstance(base, dict) and isinstance(changes, dict):
        out = dict(base)
        for key, value in changes.items():
            out[key] = _deep_merge(base[key], value) if key in base else value
        return out
    return changes
