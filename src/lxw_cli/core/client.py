from __future__ import annotations

import re
import threading
import time
from collections.abc import Iterator
from typing import Any
from urllib.parse import unquote

import httpx
from tenacity import (
    RetryCallState,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from lxw_cli import __version__
from lxw_cli.config import Config
from lxw_cli.core.errors import LexwareAPIError, LexwareError, RateLimitError

RATE_LIMIT_PER_SECOND = 2
MIN_INTERVAL = 1.0 / RATE_LIMIT_PER_SECOND
# Lexware paging bounds: max 250 per page (size is capped server-side), and
# some endpoints (e.g. /v1/articles) enforce a minimum of 25. With the 2 req/s
# throttle, always requesting the maximum minimizes wall-clock time.
PAGE_SIZE_MIN = 25
PAGE_SIZE_MAX = 250
# Upper bound for honoring a server-sent Retry-After, so a bogus header can't
# stall the client for minutes.
MAX_RETRY_AFTER = 60.0

_BACKOFF = wait_exponential(multiplier=1, min=1, max=30)

# Every API path is "/v1/<segment>/…". Segments carry resource names and ids
# (UUIDs) — often straight from user/LLM input such as an invoice id. Only
# unreserved characters are allowed, so an id like "x/../contacts" or
# "x?foo=bar" can never reach another endpoint or smuggle query parameters
# (those go through `params`).
_PATH_RE = re.compile(r"^/v1(?:/[A-Za-z0-9_~.-]+)+$")


def _check_path(path: str) -> None:
    segments = path.split("/")
    if not _PATH_RE.match(path) or any(seg in (".", "..") for seg in segments):
        raise LexwareError(f"Ungültige Kennung im API-Pfad: {path!r}")


def _wait_for_retry(retry_state: RetryCallState) -> float:
    """Seconds to wait between retries.

    Honors the server's Retry-After (capped) when present, otherwise falls
    back to exponential backoff — exactly one wait per retry, never both.
    """
    exc = retry_state.outcome.exception() if retry_state.outcome else None
    if isinstance(exc, RateLimitError) and exc.retry_after:
        return min(exc.retry_after, MAX_RETRY_AFTER)
    return _BACKOFF(retry_state)


class LexwareClient:
    """HTTP client for the Lexware Office API.

    Enforces the documented 2 req/s rate limit client-side and retries on
    429/5xx with exponential backoff (Retry-After honored when present).
    """

    def __init__(self, config: Config, timeout: float = 30.0) -> None:
        self._config = config
        self._last_request_at = 0.0
        self._throttle_lock = threading.Lock()
        self._http = httpx.Client(
            base_url=config.base_url,
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {config.api_key}",
                "Accept": "application/json",
                "User-Agent": f"lxw-cli/{__version__}",
            },
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> LexwareClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _throttle(self) -> None:
        # The lock is held across the sleep on purpose: concurrent callers
        # (e.g. parallel MCP tool calls sharing this client) are serialized so
        # the 2 req/s budget holds globally, not per thread.
        with self._throttle_lock:
            elapsed = time.monotonic() - self._last_request_at
            if elapsed < MIN_INTERVAL:
                time.sleep(MIN_INTERVAL - elapsed)
            self._last_request_at = time.monotonic()

    @retry(
        retry=retry_if_exception_type(RateLimitError),
        wait=_wait_for_retry,
        stop=stop_after_attempt(5),
        reraise=True,
    )
    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        files: Any = None,
        accept: str = "application/json",
    ) -> httpx.Response:
        _check_path(path)
        self._throttle()
        headers = {"Accept": accept} if accept != "application/json" else None
        cleaned_params = (
            {k: v for k, v in params.items() if v is not None} if params else None
        )
        # With `files`, httpx builds the multipart/form-data body and its
        # boundary Content-Type itself — we must not pass a json body alongside.
        response = self._http.request(
            method, path, params=cleaned_params, json=json, files=files, headers=headers
        )
        if response.status_code == 429:
            # No sleep here — the retry decorator's _wait_for_retry reads
            # retry_after off the exception, so we wait exactly once.
            raise RateLimitError(
                "Rate limit exceeded (2 req/s). Retrying…",
                retry_after=_parse_retry_after(response),
            )
        if response.status_code >= 400:
            raise LexwareAPIError(
                response.status_code,
                _extract_message(response),
                body=_safe_json(response),
            )
        return response

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self._request("GET", path, params=params).json()

    def post(
        self, path: str, json_body: Any, params: dict[str, Any] | None = None
    ) -> Any:
        return self._request("POST", path, params=params, json=json_body).json()

    def put(self, path: str, json_body: Any) -> Any:
        return self._request("PUT", path, json=json_body).json()

    def post_multipart(self, path: str, files: Any) -> Any:
        """POST a multipart/form-data body (file upload) and return the JSON.

        `files` follows httpx's format, e.g.
        ``{"file": (filename, content_bytes, content_type)}``.

        Lexware answers uploads with ``202 Accepted`` — sometimes without a
        JSON body. A successful upload must never surface as an error just
        because there is nothing to parse, so an empty/non-JSON body yields
        ``{"status": <code>}`` instead.
        """
        response = self._request("POST", path, files=files)
        body = _safe_json(response)
        if isinstance(body, dict):
            return body
        return {"status": response.status_code} if body is None else body

    def get_binary(self, path: str, accept: str = "application/pdf") -> bytes:
        return self._request("GET", path, accept=accept).content

    def download_file(self, path: str) -> tuple[bytes, str | None, str | None]:
        """GET a stored file, returning (content, content_type, filename).

        Unlike :meth:`get_binary` this accepts any media type (attachments may
        be PDF, JPG or PNG) and reads the server-suggested filename from the
        Content-Disposition header when present.
        """
        response = self._request("GET", path, accept="*/*")
        content_type = response.headers.get("Content-Type")
        if content_type:
            content_type = content_type.split(";", 1)[0].strip() or None
        return response.content, content_type, _filename_from_disposition(response)

    def paginate(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        max_items: int | None = None,
        page_size: int = PAGE_SIZE_MAX,
        meta: dict[str, Any] | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Yield items from a paginated `content` endpoint.

        Lexware uses Spring-style paging (?page=0&size=N) with a response
        envelope `{content: [...], totalPages, totalElements, last, ...}`. The
        page size defaults to the API maximum (250) so every request fetches
        as many records as allowed; `max_items` is applied client-side.

        If `meta` is given, it is populated with `total` (the server's
        `totalElements`) from the first page — letting callers report the full
        result-set size without an extra request.
        """
        page = 0
        yielded = 0
        params = dict(params or {})
        params["size"] = min(max(page_size, PAGE_SIZE_MIN), PAGE_SIZE_MAX)
        while True:
            params["page"] = page
            try:
                payload = self.get(path, params=params)
            except LexwareAPIError as exc:
                # Lexware caps how deep a list can be paged ("Maximum search
                # window size exceeded", HTTP 400). Stop there and keep what
                # was fetched instead of failing the whole request.
                if page == 0 or exc.status_code != 400 or not _is_window_limit(exc):
                    raise
                if meta is not None:
                    meta["truncated"] = True
                return
            if meta is not None and "total" not in meta:
                total = payload.get("totalElements")
                if total is not None:
                    meta["total"] = total
            for item in payload.get("content", []):
                yield item
                yielded += 1
                if max_items is not None and yielded >= max_items:
                    return
            if payload.get("last", True):
                return
            page += 1


_WINDOW_LIMIT_RE = re.compile(r"(maximum search window|Suchfenster)", re.I)


def _is_window_limit(exc: LexwareAPIError) -> bool:
    return bool(_WINDOW_LIMIT_RE.search(str(exc)) or _WINDOW_LIMIT_RE.search(str(exc.body)))


def _filename_from_disposition(response: httpx.Response) -> str | None:
    """Extract a filename from a Content-Disposition header, if any.

    Handles both ``filename="foo.pdf"`` and the RFC 5987 ``filename*=UTF-8''foo``
    form; returns None when no usable name is present.
    """
    header = response.headers.get("Content-Disposition")
    if not header:
        return None
    ext = re.search(r"filename\*=(?:UTF-8'')?\"?([^\";]+)\"?", header, re.IGNORECASE)
    if ext:
        return unquote(ext.group(1).strip())
    plain = re.search(r'filename="?([^";]+)"?', header, re.IGNORECASE)
    if plain:
        return plain.group(1).strip()
    return None


def _parse_retry_after(response: httpx.Response) -> float | None:
    header = response.headers.get("Retry-After")
    if not header:
        return None
    try:
        return float(header)
    except ValueError:
        return None


def _extract_message(response: httpx.Response) -> str:
    body = _safe_json(response)
    if isinstance(body, dict):
        # HTTP 403 {"i18nKey": "access_denied", "type": "user_not_authorized"}:
        # the key's user lacks the right or the plan lacks the feature.
        if body.get("type") == "user_not_authorized" or body.get("i18nKey") == "access_denied":
            return _ACCESS_DENIED
        # Lexware reports field-level validation problems (e.g. an over-long
        # text) in a `details` list. Surface those first — they are far more
        # specific than the generic top-level message — and phrase length
        # violations in plain German (the API only says "size must be …").
        detail = _format_validation_details(body.get("details"))
        if detail:
            return detail
        issues = _format_issue_list(body.get("IssueList"))
        if issues:
            return issues
        for key in ("message", "error_description", "error", "title"):
            if key in body and body[key]:
                return _translate_known_message(str(body[key]))
    return response.text or response.reason_phrase or "API error"


_ACCESS_DENIED = (
    "Keine Berechtigung: Lexware lässt diese Aktion mit dem API-Schlüssel dieser "
    "Installation nicht zu. Entweder fehlen dem Lexware-Benutzer, der den "
    "Schlüssel erzeugt hat, die Rechte dafür, oder der Lexware-Tarif enthält "
    "diese Funktion nicht. Andere Aktionen funktionieren weiter. Abhilfe: in "
    "Lexware die Benutzerrechte bzw. den Tarif prüfen und danach einen neuen "
    "API-Schlüssel erzeugen und die Verbindung neu einrichten."
)


# Known Lexware error texts that are hard to understand for end users, with a
# plain German replacement. Matched case-insensitively against the message.
_KNOWN_MESSAGES = (
    (
        re.compile(r"voucherStatus filter 'overdue' cannot be used in combination", re.I),
        "Der Status „überfällig“ kann bei Lexware nicht zusammen mit anderen "
        "Status abgefragt werden. Bitte überfällige Belege separat abfragen.",
    ),
    (
        # HTTP 400 when a list is paged too deep (Lexware's search window).
        re.compile(r"Maximum search window size exceeded", re.I),
        "Die Liste ist zu groß, um sie so weit durchzublättern (Lexware-"
        "Suchfenster). Bitte enger filtern, z. B. nach Status, Kontakt oder "
        "Belegnummer, oder weniger Einträge abrufen.",
    ),
    (
        # HTTP 409 on a PDF download: "the sales voucher with id … and voucher
        # type 'Order' is in status 'draft' and therefore cannot be downloaded"
        re.compile(r"is in status 'draft' and therefore cannot be downloaded", re.I),
        "Beleg ist ein Entwurf – erst in Lexware abschließen. Entwürfe haben "
        "noch kein PDF; nach dem Abschließen lässt sich das PDF herunterladen.",
    ),
)


# The bookkeeping-voucher endpoints (/v1/vouchers, …/files) report problems as
# {"IssueList": [{"i18nKey": "...", "source": "field", ...}]} — machine keys
# only. Known keys get a plain German sentence; others stay readable.
_ISSUE_TEXTS = {
    "inacceptable_file_extension": (
        "Dateityp nicht akzeptiert — Lexware nimmt nur PDF, JPG, PNG oder XML "
        "an, und der Dateiname braucht die passende Endung (z. B. „beleg.pdf“)."
    ),
    "expected_no_shipping_date_for_given_voucher_type": (
        "Für diese Belegart ist kein Leistungs-/Lieferdatum erlaubt — bitte "
        "das Lieferdatum weglassen."
    ),
}


def _format_issue_list(issues: object) -> str | None:
    if not isinstance(issues, list):
        return None
    parts: list[str] = []
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        key = str(issue.get("i18nKey") or "").strip()
        source = str(issue.get("source") or "").strip()
        text = _ISSUE_TEXTS.get(key)
        if not text:
            text = key.replace("_", " ") or "ungültige Angabe"
            if source:
                text = f"Feld „{source}“: {text}"
        if text not in parts:
            parts.append(text)
    return " ".join(parts) or None


def _translate_known_message(message: str) -> str:
    for pattern, german in _KNOWN_MESSAGES:
        if pattern.search(message):
            return german
    return message


# Friendly German labels for the technical field paths Lexware returns in
# `details[].field`. The list index (e.g. `lineItems[0].name`) is stripped
# before the lookup. Missing entries fall back to the raw field name.
_FRIENDLY_FIELDS = {
    "title": "Titel",
    "introduction": "Einleitungstext",
    "remark": "Nachbemerkung",
    "note": "Notiz",
    "salutation": "Anrede",
    "lineItems.name": "Positionsbezeichnung",
    "lineItems.description": "Positionsbeschreibung",
    "voucherItems.name": "Positionsbezeichnung",
    "description": "Beschreibung",
    "name": "Bezeichnung",
}

# "size must be between 0 and 25", "must not exceed 100", "maximum length 25" …
_SIZE_HINT_RE = re.compile(
    r"(?:between\s+\d+\s+and\s+(\d+))"
    r"|(?:not\s+exceed\s+(\d+))"
    r"|(?:max(?:imum)?\s+(?:length\s+)?(?:is\s+)?(\d+))",
    re.IGNORECASE,
)


def _format_validation_details(details: object) -> str | None:
    """Render Lexware's `details` list into one clear, human-friendly line."""
    if not isinstance(details, list):
        return None
    parts: list[str] = []
    for entry in details:
        if not isinstance(entry, dict):
            continue
        raw_field = str(entry.get("field") or "").strip()
        message = str(entry.get("message") or "").strip()
        label = _FRIENDLY_FIELDS.get(re.sub(r"\[\d+\]", "", raw_field), raw_field)
        size_match = _SIZE_HINT_RE.search(message)
        if size_match:
            limit = next(g for g in size_match.groups() if g)
            if label:
                parts.append(
                    f"„{label}“ ist zu lang – höchstens {limit} Zeichen erlaubt."
                )
            else:
                parts.append(f"Ein Text ist zu lang – höchstens {limit} Zeichen erlaubt.")
        elif label and message:
            parts.append(f"{label}: {message}")
        elif message:
            parts.append(message)
    if not parts:
        return None
    return " ".join(parts)


def _safe_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None
