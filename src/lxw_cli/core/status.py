"""Lexware-Betriebsstatus von https://status.lexware.de.

Die Statusseite läuft auf Sorry™ und hat eine öffentliche, **schlüssellose**
API — sie funktioniert also auch dann noch, wenn der API-Key abgelaufen ist
oder Lexware selbst nicht antwortet. Genau deshalb ist sie als Fehler-
Diagnose brauchbar: ein 503 aus der Lexware-API sagt nichts, ein 503 *plus*
"laufende Störung im Bereich Belegerstellung" beantwortet die Frage des
Nutzers sofort.

Genutzte Endpunkte::

    GET /api/v1/status    Gesamtzustand (operational | degraded | under-maintenance)
    GET /api/v1/notices?filter[timeline_state_eq]=present    laufende Meldungen
    GET /api/v1/notices?filter[timeline_state_eq]=future&filter[type_eq]=planned

Der Gesamtzustand kostet genau einen Request; Meldungen werden nur geholt,
wenn der Zustand nicht ``operational`` ist. Im Normalfall (alles läuft) ist
das also **ein** Request pro TTL-Fenster.

Wichtige Eigenschaften dieses Moduls:

- **Wirft nie.** Jede Störung beim Statusabruf (Timeout, DNS, kaputtes JSON)
  endet in ``None``. Ein Diagnose-Helfer darf niemals selbst zur Fehler-
  quelle werden.
- **Cached mit TTL.** Der Upstream liegt hinter einem CDN mit
  ``s-maxage=600`` — frischer als ~10 Minuten existiert die Information
  gar nicht. Häufigeres Abfragen bringt null zusätzliche Aussage.
- **Kein Hintergrund-Polling im stdio-Modus.** Der Cache füllt sich faul,
  wenn jemand ihn braucht. Nur der langlaufende HTTP-Server hält ihn
  optional per :func:`start_background_refresh` warm.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime

import httpx

log = logging.getLogger("lxw_cli.status")

STATUS_BASE_URL = "https://status.lexware.de"
STATUS_API_URL = f"{STATUS_BASE_URL}/api/v1"

STATE_OPERATIONAL = "operational"
STATE_DEGRADED = "degraded"
STATE_MAINTENANCE = "under-maintenance"

# Der Upstream cached 600 s im CDN — kürzer abzufragen liefert dieselbe
# Antwort. 300 s hält uns reaktionsschnell ohne sinnlosen Traffic.
DEFAULT_TTL = 300.0
# Auf dem Fehlerpfad zählt Aktualität mehr: eine Störung, die vor 4 Minuten
# begann, soll die Fehlermeldung schon erklären.
ERROR_TTL = 60.0
# Kurz: der Statusabruf hängt an einem Fehlerpfad, den der Nutzer bereits
# als "es hakt" erlebt. Er darf ihn nicht zusätzlich verlängern.
TIMEOUT = 3.0

_PLANNED = "planned"
_UNPLANNED = "unplanned"


@dataclass(frozen=True)
class Notice:
    """Eine Meldung der Statusseite (Störung, Wartung oder Hinweis)."""

    subject: str
    type: str
    state: str
    url: str
    began_at: str | None = None
    begins_at: str | None = None
    ends_at: str | None = None
    update: str | None = None

    @property
    def is_maintenance(self) -> bool:
        return self.type == _PLANNED

    @property
    def is_incident(self) -> bool:
        return self.type == _UNPLANNED


@dataclass(frozen=True)
class StatusReport:
    """Momentaufnahme der Lexware-Statusseite."""

    state: str
    state_text: str
    current: tuple[Notice, ...] = ()
    planned: tuple[Notice, ...] = ()
    url: str = STATUS_BASE_URL
    checked_at: float = 0.0

    @property
    def operational(self) -> bool:
        return self.state == STATE_OPERATIONAL

    @property
    def incidents(self) -> tuple[Notice, ...]:
        return tuple(n for n in self.current if n.is_incident)


# --------------------------------------------------------------------------
# Abruf + Cache
# --------------------------------------------------------------------------

_cache: dict[bool, StatusReport] = {}
_cache_lock = threading.Lock()


def clear_cache() -> None:
    """Cache leeren (Tests, und nach einem manuellen `--refresh`)."""
    with _cache_lock:
        _cache.clear()


def get_status(
    *,
    ttl: float = DEFAULT_TTL,
    include_planned: bool = False,
    timeout: float = TIMEOUT,
) -> StatusReport | None:
    """Aktuellen Betriebsstatus liefern — oder ``None``, wenn nicht abrufbar.

    `ttl` ist das maximal akzeptierte Alter des zwischengespeicherten
    Ergebnisses. Der Fehlerpfad fragt mit kleinerem TTL an als das
    Status-Tool, teilt sich aber denselben Cache.
    """
    with _cache_lock:
        cached = _cache.get(include_planned)
        if cached is not None and (time.monotonic() - cached.checked_at) < ttl:
            return cached
        # Eine Anfrage mit Wartungsterminen deckt den einfachen Fall mit ab.
        if not include_planned:
            richer = _cache.get(True)
            if richer is not None and (time.monotonic() - richer.checked_at) < ttl:
                return richer

    try:
        report = _fetch(include_planned=include_planned, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 — Diagnose darf nie selbst failen
        log.debug("Statusabruf fehlgeschlagen: %s", exc)
        return None

    with _cache_lock:
        _cache[include_planned] = report
    return report


def _fetch(*, include_planned: bool, timeout: float) -> StatusReport:
    with httpx.Client(
        base_url=STATUS_API_URL,
        timeout=timeout,
        headers={"Accept": "application/json"},
        follow_redirects=True,
    ) as http:
        page = http.get("/status").raise_for_status().json().get("page", {})
        state = str(page.get("state") or STATE_OPERATIONAL)
        state_text = str(page.get("state_text") or "")

        current: tuple[Notice, ...] = ()
        # Läuft alles, gibt es per Definition keine laufende Meldung — der
        # zweite Request wäre garantiert leer.
        if state != STATE_OPERATIONAL:
            current = _notices(http, {"filter[timeline_state_eq]": "present"})

        planned: tuple[Notice, ...] = ()
        if include_planned:
            planned = _notices(
                http,
                {
                    "filter[timeline_state_eq]": "future",
                    "filter[type_eq]": _PLANNED,
                },
            )

    return StatusReport(
        state=state,
        state_text=state_text,
        current=current,
        planned=planned,
        url=str(page.get("url") or STATUS_BASE_URL),
        checked_at=time.monotonic(),
    )


def _notices(http: httpx.Client, params: dict[str, str]) -> tuple[Notice, ...]:
    """Erste Seite der Meldungen holen (25 Stück — mehr braucht niemand)."""
    payload = http.get("/notices", params=params).raise_for_status().json()
    out: list[Notice] = []
    for raw in payload.get("notices", []):
        if not isinstance(raw, dict):
            continue
        update = raw.get("latest_update")
        out.append(
            Notice(
                subject=str(raw.get("subject") or "Meldung ohne Betreff"),
                type=str(raw.get("type") or ""),
                state=str(raw.get("state") or ""),
                url=str(raw.get("url") or STATUS_BASE_URL),
                began_at=raw.get("began_at"),
                begins_at=raw.get("begins_at"),
                ends_at=raw.get("ends_at"),
                update=(
                    str(update.get("content"))
                    if isinstance(update, dict) and update.get("content")
                    else None
                ),
            )
        )
    return tuple(out)


# --------------------------------------------------------------------------
# Formulierung (deutsch, ohne technische Begriffe)
# --------------------------------------------------------------------------

# Die Zustände der Statusseite in Alltagssprache. Der Nutzer soll nicht
# raten müssen, was "identified" bedeutet.
_NOTICE_STATE_LABEL = {
    "investigating": "wird untersucht",
    "identified": "Ursache gefunden, Behebung läuft",
    "recovering": "Behebung läuft, Systeme erholen sich",
    "resolved": "behoben",
    "false_alarm": "Fehlalarm",
    "scheduled": "geplant",
    "underway": "läuft gerade",
    "complete": "abgeschlossen",
    "cancelled": "abgesagt",
}


def _local_time(value: str | None) -> str | None:
    """ISO-8601-Zeitstempel als lokale Uhrzeit ("14:07 Uhr") formatieren."""
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    local = moment.astimezone()
    if local.date() == datetime.now().astimezone().date():
        return f"{local:%H:%M} Uhr"
    return f"{local:%d.%m.%Y, %H:%M} Uhr"


def describe_notice(notice: Notice) -> str:
    """Eine Meldung als ein vollständiger deutscher Satz."""
    parts = [notice.subject]
    detail: list[str] = []
    since = _local_time(notice.began_at or notice.begins_at)
    if since:
        # Eine noch nicht begonnene Wartung hat nur `begins_at` — dort wäre
        # "seit" schlicht falsch.
        upcoming = notice.is_maintenance and not notice.began_at
        detail.append(("geplant ab " if upcoming else "seit ") + since)
    label = _NOTICE_STATE_LABEL.get(notice.state)
    if label:
        detail.append(label)
    if detail:
        parts.append(f"({', '.join(detail)})")
    return " ".join(parts)


def summary(report: StatusReport) -> str:
    """Den Gesamtbericht in ein bis drei Sätzen zusammenfassen."""
    if report.operational and not report.planned:
        return "Lexware Office läuft laut Statusseite normal — keine bekannten Störungen."

    lines: list[str] = []
    if report.operational:
        lines.append("Lexware Office läuft laut Statusseite normal — keine bekannten Störungen.")
    elif report.state == STATE_MAINTENANCE:
        lines.append("Bei Lexware Office läuft gerade eine geplante Wartung.")
    else:
        lines.append("Lexware Office meldet gerade eine Störung.")

    for notice in report.current:
        lines.append(f"• {describe_notice(notice)}")
        if notice.update:
            lines.append(f"  {notice.update}")
    for notice in report.planned:
        lines.append(f"• Angekündigte Wartung: {describe_notice(notice)}")

    lines.append(f"Details: {report.url}")
    return "\n".join(lines)


def incident_hint(report: StatusReport | None) -> str | None:
    """Einzeiler für eine Fehlermeldung — oder ``None``, wenn alles läuft.

    Bewusst knapp: der Satz wird an eine bereits vorhandene Fehlermeldung
    angehängt, nicht als eigene Antwort ausgegeben.
    """
    if report is None or report.operational:
        return None
    if report.current:
        first = report.current[0]
        kind = "eine geplante Wartung" if first.is_maintenance else "eine Störung"
        return (
            f"Lexware meldet derzeit {kind}: {describe_notice(first)} — "
            f"das ist vermutlich die Ursache. Details: {report.url}"
        )
    kind = (
        "eine geplante Wartung"
        if report.state == STATE_MAINTENANCE
        else "eine Störung"
    )
    return (
        f"Lexware meldet derzeit {kind} — das ist vermutlich die Ursache. "
        f"Details: {report.url}"
    )


# --------------------------------------------------------------------------
# Fehlerpfad
# --------------------------------------------------------------------------


def looks_like_outage(exc: BaseException) -> bool:
    """Rechtfertigt dieser Fehler einen Blick auf die Statusseite?

    Nur serverseitige und Transportfehler. Ein 400 (falsche Eingabe) oder
    401 (falscher Schlüssel) hat nichts mit Lexwares Betriebslage zu tun —
    ein Statushinweis wäre dort schlicht irreführend.

    Die Ausnahme wird samt ``__cause__``/``__context__``-Kette geprüft, weil
    Framework-Schichten den ursprünglichen Fehler gern einpacken.
    """
    from lxw_cli.core.errors import LexwareAPIError

    seen: set[int] = set()
    node: BaseException | None = exc
    while node is not None and id(node) not in seen:
        seen.add(id(node))
        if isinstance(node, LexwareAPIError):
            # 429 zählt: hier ist bereits fünfmal erfolglos wiederholt
            # worden, das ist kein normales Throttling mehr.
            if node.status_code >= 500 or node.status_code == 429:
                return True
        elif isinstance(node, httpx.TransportError):
            # Deckt Timeout, Verbindungsabbruch, DNS und Pool-Fehler ab.
            return True
        node = node.__cause__ or node.__context__
    return False


def hint_for_exception(
    exc: BaseException, *, ttl: float = ERROR_TTL, timeout: float = TIMEOUT
) -> str | None:
    """Statushinweis zu einem Fehler — ``None``, wenn keiner angebracht ist.

    Wirft nie und fragt die Statusseite nur bei passenden Fehlern ab.
    """
    if not looks_like_outage(exc):
        return None
    return incident_hint(get_status(ttl=ttl, timeout=timeout))


# --------------------------------------------------------------------------
# Optionaler Hintergrund-Refresh (nur langlaufender HTTP-Server)
# --------------------------------------------------------------------------


def start_background_refresh(interval: float = 600.0) -> threading.Thread:
    """Cache in einem Daemon-Thread warm halten und Störungen protokollieren.

    Nur für ``lxw-mcp-http`` sinnvoll: dort läuft der Prozess dauerhaft und
    es gibt mit dem Betreiber-Log tatsächlich einen Konsumenten. Im
    stdio-Modus lebt der Prozess oft kürzer als ein Intervall — dort genügt
    der faule Cache.

    Das Intervall ist auf die CDN-Cachedauer (600 s) abgestimmt; kürzer
    liefert dieselbe Antwort.
    """
    interval = max(interval, 60.0)
    last_state: dict[str, str] = {}

    def _loop() -> None:
        while True:
            report = get_status(ttl=0.0, include_planned=True)
            if report is not None and report.state != last_state.get("state"):
                last_state["state"] = report.state
                if report.operational:
                    log.info("Lexware-Status: wieder normal")
                else:
                    log.warning("Lexware-Status: %s — %s", report.state, summary(report))
            time.sleep(interval)

    thread = threading.Thread(
        target=_loop, name="lxw-status-refresh", daemon=True
    )
    thread.start()
    return thread
