from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from typing import Any

import httpx
from tenacity import (
    RetryCallState,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from lexware_cli import __version__
from lexware_cli.config import Config
from lexware_cli.core.errors import LexwareAPIError, RateLimitError

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
                "User-Agent": f"lexware-cli/{__version__}",
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
        accept: str = "application/json",
    ) -> httpx.Response:
        self._throttle()
        headers = {"Accept": accept} if accept != "application/json" else None
        cleaned_params = (
            {k: v for k, v in params.items() if v is not None} if params else None
        )
        response = self._http.request(
            method, path, params=cleaned_params, json=json, headers=headers
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

    def post(self, path: str, json_body: Any) -> Any:
        return self._request("POST", path, json=json_body).json()

    def put(self, path: str, json_body: Any) -> Any:
        return self._request("PUT", path, json=json_body).json()

    def get_binary(self, path: str, accept: str = "application/pdf") -> bytes:
        return self._request("GET", path, accept=accept).content

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
            payload = self.get(path, params=params)
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
        for key in ("message", "error_description", "error", "title"):
            if key in body and body[key]:
                return str(body[key])
    return response.text or response.reason_phrase or "API error"


def _safe_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None
