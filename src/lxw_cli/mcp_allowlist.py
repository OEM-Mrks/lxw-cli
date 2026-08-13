"""KYC allow-list gate for the HTTP MCP server.

The multi-user server is a stateless proxy: anyone with a *valid* Lexware
API key can use it. That is fine for a personal tool, but as soon as the
service should only serve **known** Lexware installations (KYC — and, on
top of that, a paid offering) we need a gate that lets a request through
only if its Lexware *organization* is on an allow-list.

The natural identity of an installation is the ``organizationId`` returned
by ``GET /v1/profile`` — a stable UUID per Lexware Office company,
independent of which API key/connection is used. This module resolves that
id for a given key (cached) and decides whether the caller is allowed.

Three modes (env ``LXW_MCP_ALLOWLIST_MODE``):

- ``off``    — default. The gate does nothing at all (not even a profile
               lookup), so behaviour is identical to a server without this
               module. Deploying the code changes nothing until you opt in.
- ``log``    — every calling organization is recorded to ``pending.json``
               but **nothing is blocked**. Use this first to discover which
               installations actually use the service and build the
               allow-list from real data, without risking a lockout.
- ``enforce``— organizations not on the allow-list are rejected. Unknown
               orgs are still recorded to ``pending.json`` so you can review
               and approve them (the KYC onboarding queue).

Configuration (all optional; the gate is inert unless mode != off):

- ``LXW_MCP_ALLOWLIST``          comma-separated organizationIds (inline).
- ``<data_dir>/allowlist.json``  a JSON array of organizationIds, or an
                                 object ``{orgId: {"note": "..."}}``.
                                 Hot-reloaded on change — add a customer
                                 without restarting the server. Merged with
                                 the inline env list.
- ``<data_dir>/pending.json``    written by the gate: unknown orgs seen,
                                 with company name / user email / counters.
- ``LXW_MCP_ALLOWLIST_CONTACT``  address shown in the rejection message so a
                                 blocked user knows where to ask for access.
- ``LXW_MCP_ALLOWLIST_TTL``      seconds to cache a key→org resolution
                                 (default 600). The *decision* is always
                                 re-derived from the current list, so adding
                                 an org takes effect on the next request even
                                 within the TTL.
- ``LXW_MCP_ALLOWLIST_FAIL_OPEN````1`` to allow through when the profile
                                 lookup itself fails (Lexware unreachable).
                                 Default is fail-closed in ``enforce`` mode.
                                 Note the tools would fail anyway if Lexware
                                 is truly down, and an attacker still needs a
                                 *valid* key to read anything — this only
                                 trades a bit of strictness for availability.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from lxw_cli.config import DEFAULT_BASE_URL

log = logging.getLogger("lxw_cli.allowlist")

ENV_MODE = "LXW_MCP_ALLOWLIST_MODE"
ENV_LIST = "LXW_MCP_ALLOWLIST"
ENV_CONTACT = "LXW_MCP_ALLOWLIST_CONTACT"
ENV_TTL = "LXW_MCP_ALLOWLIST_TTL"
ENV_FAIL_OPEN = "LXW_MCP_ALLOWLIST_FAIL_OPEN"

MODE_OFF = "off"
MODE_LOG = "log"
MODE_ENFORCE = "enforce"
_VALID_MODES = {MODE_OFF, MODE_LOG, MODE_ENFORCE}


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _parse_ids(raw: str) -> set[str]:
    return {part.strip() for part in raw.replace("\n", ",").split(",") if part.strip()}


def _key_digest(api_key: str) -> str:
    return hashlib.sha256(api_key.encode()).hexdigest()


@dataclass(frozen=True)
class Decision:
    """Outcome of the gate for one request."""

    allowed: bool
    mode: str
    org_id: str | None = None
    company: str | None = None
    reason: str = ""

    @property
    def user_message(self) -> str:
        """A friendly German message for the OAuth consent page on rejection."""
        contact = os.getenv(ENV_CONTACT, "").strip()
        hint = f" Kennung: {self.org_id[:8]}…" if self.org_id else ""
        where = f" Bitte wende dich an {contact} zur Freischaltung." if contact else (
            " Bitte wende dich an den Anbieter zur Freischaltung."
        )
        if self.reason == "unreachable":
            return (
                "Die Freischaltung konnte gerade nicht geprüft werden "
                "(Lexware nicht erreichbar). Bitte später erneut versuchen."
            )
        return (
            "Diese Lexware-Installation ist für den Dienst noch nicht "
            f"freigeschaltet.{where}{hint}"
        )


class AllowList:
    """Resolve a key's organizationId and decide whether it may use the server.

    Thread-safe. One instance lives on the OAuth provider; both the consent
    flow and the per-request token check consult it.
    """

    def __init__(self, data_dir: Path, lexware_base_url: str | None = None) -> None:
        mode = (os.getenv(ENV_MODE, MODE_OFF) or MODE_OFF).strip().lower()
        if mode not in _VALID_MODES:
            log.warning("Unbekannter %s=%r — falle auf 'off' zurück.", ENV_MODE, mode)
            mode = MODE_OFF
        self._mode = mode
        self._fail_open = _env_bool(ENV_FAIL_OPEN, default=False)
        self._contact = os.getenv(ENV_CONTACT, "").strip()
        try:
            self._ttl = float(os.getenv(ENV_TTL, "600"))
        except ValueError:
            self._ttl = 600.0
        self._base_url = (
            lexware_base_url or os.getenv("LEXWARE_API_BASE_URL", DEFAULT_BASE_URL)
        ).rstrip("/")

        self._inline = _parse_ids(os.getenv(ENV_LIST, ""))
        self._path = data_dir / "allowlist.json"
        self._pending_path = data_dir / "pending.json"

        self._lock = threading.Lock()
        self._file_ids: set[str] = set()
        self._file_mtime: float | None = None
        # key_hash -> (org_id | None, company, email, resolved_at)
        self._cache: dict[str, tuple[str | None, str | None, str | None, float]] = {}

        if self._mode != MODE_OFF:
            self._load_file()
            log.info(
                "Allow-List-Gate aktiv: Modus=%s, %d Org(s) freigeschaltet, "
                "fail_open=%s.",
                self._mode,
                len(self._inline | self._file_ids),
                self._fail_open,
            )

    # -- public API ---------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self._mode != MODE_OFF

    async def evaluate(self, api_key: str) -> Decision:
        """Gate a request given only the API key (resolves the org, cached)."""
        if self._mode == MODE_OFF:
            return Decision(allowed=True, mode=MODE_OFF)
        org_id, company, email = await self._resolve(api_key)
        if org_id is None:
            # Could not determine the org (Lexware unreachable / odd response).
            allowed = self._fail_open or self._mode == MODE_LOG
            return Decision(allowed=allowed, mode=self._mode, reason="unreachable")
        return self._decide(org_id, company, email)

    def evaluate_profile(self, profile: dict[str, Any], api_key: str) -> Decision:
        """Gate using an already-fetched profile (consent flow, no extra HTTP).

        Also warms the resolver cache so the following per-request check for
        the same key needs no profile lookup.
        """
        if self._mode == MODE_OFF:
            return Decision(allowed=True, mode=MODE_OFF)
        org_id = _org_id_of(profile)
        company = profile.get("companyName") if isinstance(profile, dict) else None
        email = _user_email_of(profile)
        if org_id is None:
            allowed = self._fail_open or self._mode == MODE_LOG
            return Decision(allowed=allowed, mode=self._mode, reason="unreachable")
        with self._lock:
            self._cache[_key_digest(api_key)] = (org_id, company, email, time.monotonic())
        return self._decide(org_id, company, email)

    # -- internals ----------------------------------------------------------

    def _decide(self, org_id: str, company: str | None, email: str | None) -> Decision:
        listed = self._is_listed(org_id)
        if listed:
            return Decision(True, self._mode, org_id, company, reason="listed")
        # Unknown org: always record it for the KYC queue; block only in enforce.
        self._record_pending(org_id, company, email, blocked=self._mode == MODE_ENFORCE)
        if self._mode == MODE_LOG:
            log.info("Allow-List (log): unbekannte Org %s (%s) — durchgelassen.",
                     org_id, company or "?")
            return Decision(True, self._mode, org_id, company, reason="logged")
        log.warning("Allow-List (enforce): Org %s (%s) NICHT freigeschaltet — blockiert.",
                    org_id, company or "?")
        return Decision(False, self._mode, org_id, company, reason="not_listed")

    async def _resolve(
        self, api_key: str
    ) -> tuple[str | None, str | None, str | None]:
        digest = _key_digest(api_key)
        now = time.monotonic()
        with self._lock:
            entry = self._cache.get(digest)
            if entry is not None and now - entry[3] <= self._ttl:
                return entry[0], entry[1], entry[2]
        # Cache miss (or stale): ask Lexware who this key belongs to.
        org_id = company = email = None
        try:
            async with httpx.AsyncClient(timeout=15) as probe:
                resp = await probe.get(
                    f"{self._base_url}/v1/profile",
                    headers={"Authorization": f"Bearer {api_key}",
                             "Accept": "application/json"},
                )
            if resp.status_code < 400:
                profile = resp.json()
                org_id = _org_id_of(profile)
                company = profile.get("companyName") if isinstance(profile, dict) else None
                email = _user_email_of(profile)
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("Profil-Abruf für Allow-List fehlgeschlagen: %s", exc)
            return None, None, None
        if org_id is not None:
            with self._lock:
                self._cache[digest] = (org_id, company, email, now)
        return org_id, company, email

    def _is_listed(self, org_id: str) -> bool:
        self._load_file()  # cheap: only re-reads on mtime change
        return org_id in self._inline or org_id in self._file_ids

    def _load_file(self) -> None:
        try:
            mtime = self._path.stat().st_mtime
        except OSError:
            with self._lock:
                self._file_ids = set()
                self._file_mtime = None
            return
        with self._lock:
            if self._file_mtime == mtime:
                return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("allowlist.json nicht lesbar: %s", exc)
            return
        if isinstance(raw, dict):
            ids = {str(k).strip() for k in raw if str(k).strip()}
        elif isinstance(raw, list):
            ids = {str(k).strip() for k in raw if str(k).strip()}
        else:
            ids = set()
        with self._lock:
            self._file_ids = ids
            self._file_mtime = mtime
        log.info("allowlist.json neu geladen: %d Org(s).", len(ids))

    def _record_pending(
        self, org_id: str, company: str | None, email: str | None, *, blocked: bool
    ) -> None:
        now = int(time.time())
        with self._lock:
            data = self._read_pending()
            entry = data.get(org_id) or {}
            entry.setdefault("first_seen", now)
            entry["last_seen"] = now
            entry["count"] = int(entry.get("count", 0)) + 1
            if company:
                entry["companyName"] = company
            if email:
                entry["userEmail"] = email
            entry["last_decision"] = "blocked" if blocked else "allowed"
            data[org_id] = entry
            self._write_pending(data)

    def _read_pending(self) -> dict[str, Any]:
        if not self._pending_path.is_file():
            return {}
        try:
            return json.loads(self._pending_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _write_pending(self, data: dict[str, Any]) -> None:
        try:
            self._pending_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._pending_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            tmp.replace(self._pending_path)
        except OSError as exc:
            log.warning("pending.json konnte nicht geschrieben werden: %s", exc)


def _org_id_of(profile: Any) -> str | None:
    if isinstance(profile, dict):
        val = profile.get("organizationId")
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def _user_email_of(profile: Any) -> str | None:
    if isinstance(profile, dict):
        created = profile.get("created")
        if isinstance(created, dict):
            val = created.get("userEmail")
            if isinstance(val, str) and val.strip():
                return val.strip()
    return None
