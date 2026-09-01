from __future__ import annotations

import httpx
import pytest
import respx

from lxw_cli.core import status
from lxw_cli.core.errors import LexwareAPIError, RateLimitError

STATUS_URL = "https://status.lexware.de/api/v1/status"
NOTICES_URL = "https://status.lexware.de/api/v1/notices"


def _page(state: str) -> dict:
    return {
        "page": {
            "name": "Lexware Status [Prod]",
            "state": state,
            "state_text": "Alle Systeme funktionieren einwandfrei!",
            "url": "https://status.lexware.de",
        }
    }


def _notice(**over) -> dict:
    base = {
        "id": 508347,
        "type": "unplanned",
        "state": "investigating",
        "timeline_state": "present",
        "subject": "Technische Störung im Bereich Belegerstellung",
        "url": "https://status.lexware.de/notices/abc-stoerung",
        "began_at": "2026-09-01T08:14:51.580Z",
        "latest_update": {"state": "investigating", "content": "Wir untersuchen."},
    }
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# Abruf
# ---------------------------------------------------------------------------


@respx.mock
def test_operational_costs_a_single_request(status_online: None) -> None:
    """Läuft alles, sparen wir uns den Meldungs-Request — er wäre leer."""
    page = respx.get(STATUS_URL).mock(
        return_value=httpx.Response(200, json=_page("operational"))
    )
    notices = respx.get(NOTICES_URL).mock(return_value=httpx.Response(200, json={}))

    report = status.get_status()

    assert report is not None
    assert report.operational
    assert page.call_count == 1
    assert notices.call_count == 0


@respx.mock
def test_degraded_loads_current_notices(status_online: None) -> None:
    respx.get(STATUS_URL).mock(return_value=httpx.Response(200, json=_page("degraded")))
    respx.get(NOTICES_URL).mock(
        return_value=httpx.Response(200, json={"notices": [_notice()]})
    )

    report = status.get_status()

    assert report is not None
    assert not report.operational
    assert len(report.current) == 1
    notice = report.current[0]
    assert notice.subject == "Technische Störung im Bereich Belegerstellung"
    assert notice.is_incident
    assert notice.update == "Wir untersuchen."


@respx.mock
def test_planned_maintenance_is_fetched_on_request(status_online: None) -> None:
    respx.get(STATUS_URL).mock(
        return_value=httpx.Response(200, json=_page("operational"))
    )
    route = respx.get(NOTICES_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "notices": [
                    _notice(
                        type="planned",
                        state="scheduled",
                        subject="Geplante Wartung",
                        began_at=None,
                        begins_at="2026-09-05T22:00:00.000Z",
                    )
                ]
            },
        )
    )

    report = status.get_status(include_planned=True)

    assert report is not None
    assert report.operational  # Wartung angekündigt, aber noch nichts kaputt
    assert len(report.planned) == 1
    assert report.planned[0].is_maintenance
    assert route.call_count == 1


@respx.mock
def test_cache_prevents_a_second_request(status_online: None) -> None:
    route = respx.get(STATUS_URL).mock(
        return_value=httpx.Response(200, json=_page("operational"))
    )

    first = status.get_status()
    second = status.get_status()

    assert first is second
    assert route.call_count == 1


@respx.mock
def test_zero_ttl_bypasses_the_cache(status_online: None) -> None:
    route = respx.get(STATUS_URL).mock(
        return_value=httpx.Response(200, json=_page("operational"))
    )

    status.get_status()
    status.get_status(ttl=0.0)

    assert route.call_count == 2


@respx.mock
def test_planned_result_also_serves_the_simple_query(status_online: None) -> None:
    """Die reichere Antwort deckt die einfache Frage mit ab — kein Extra-Abruf."""
    route = respx.get(STATUS_URL).mock(
        return_value=httpx.Response(200, json=_page("operational"))
    )
    respx.get(NOTICES_URL).mock(return_value=httpx.Response(200, json={"notices": []}))

    status.get_status(include_planned=True)
    status.get_status(include_planned=False)

    assert route.call_count == 1


@respx.mock
@pytest.mark.parametrize(
    "outcome",
    [
        httpx.Response(500),
        httpx.Response(200, text="kein JSON"),
        httpx.ConnectTimeout("zu langsam"),
    ],
    ids=["serverfehler", "kaputtes-json", "timeout"],
)
def test_unreachable_status_page_returns_none(status_online: None, outcome) -> None:
    """Ein Diagnosehelfer darf niemals selbst zur Fehlerquelle werden."""
    if isinstance(outcome, Exception):
        respx.get(STATUS_URL).mock(side_effect=outcome)
    else:
        respx.get(STATUS_URL).mock(return_value=outcome)

    assert status.get_status() is None


# ---------------------------------------------------------------------------
# Fehlerpfad: wann lohnt der Blick auf die Statusseite?
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (LexwareAPIError(503, "Service Unavailable"), True),
        (LexwareAPIError(500, "Internal Server Error"), True),
        (RateLimitError("zu viele Anfragen"), True),
        (LexwareAPIError(400, "Titel zu lang"), False),
        (LexwareAPIError(401, "Ungültiger Schlüssel"), False),
        (LexwareAPIError(404, "Nicht gefunden"), False),
        (httpx.ConnectError("kein Netz"), True),
        (httpx.ReadTimeout("zu langsam"), True),
        (ValueError("etwas ganz anderes"), False),
    ],
)
def test_looks_like_outage(exc: BaseException, expected: bool) -> None:
    assert status.looks_like_outage(exc) is expected


def test_looks_like_outage_sees_through_wrappers() -> None:
    """FastMCP verpackt Tool-Fehler — der Ursprung muss trotzdem gefunden werden."""
    try:
        try:
            raise LexwareAPIError(503, "Service Unavailable")
        except LexwareAPIError as inner:
            raise RuntimeError("Error calling tool 'create_invoice_draft'") from inner
    except RuntimeError as wrapped:
        assert status.looks_like_outage(wrapped)


def test_looks_like_outage_survives_a_cycle() -> None:
    """Selbstbezügliche __context__-Ketten dürfen nicht zur Endlosschleife führen."""
    a = ValueError("a")
    b = ValueError("b")
    a.__context__ = b
    b.__context__ = a
    assert status.looks_like_outage(a) is False


def test_hint_for_input_error_makes_no_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unter einem 400 wäre ein Statushinweis irreführend — gar nicht erst fragen."""
    called = False

    def _spy(**_: object) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(status, "get_status", _spy)
    assert status.hint_for_exception(LexwareAPIError(400, "Titel zu lang")) is None
    assert called is False


@respx.mock
def test_hint_for_server_error_names_the_incident(status_online: None) -> None:
    respx.get(STATUS_URL).mock(return_value=httpx.Response(200, json=_page("degraded")))
    respx.get(NOTICES_URL).mock(
        return_value=httpx.Response(200, json={"notices": [_notice()]})
    )

    hint = status.hint_for_exception(LexwareAPIError(503, "Service Unavailable"))

    assert hint is not None
    assert "Belegerstellung" in hint
    assert "status.lexware.de" in hint


@respx.mock
def test_no_hint_while_everything_runs(status_online: None) -> None:
    """Ein 503 bei grüner Statusseite bleibt unkommentiert — kein Rauschen."""
    respx.get(STATUS_URL).mock(
        return_value=httpx.Response(200, json=_page("operational"))
    )

    assert status.hint_for_exception(LexwareAPIError(503, "Boom")) is None


def test_hint_is_silent_when_the_status_page_is_down() -> None:
    # Die conftest-Sperre lässt den Abruf fehlschlagen — genau dieser Fall.
    assert status.hint_for_exception(LexwareAPIError(503, "Boom")) is None


# ---------------------------------------------------------------------------
# Formulierung
# ---------------------------------------------------------------------------


def test_summary_is_reassuring_when_all_is_well() -> None:
    report = status.StatusReport(state="operational", state_text="")
    assert "normal" in status.summary(report)


def test_summary_lists_incident_and_link() -> None:
    report = status.StatusReport(
        state="degraded",
        state_text="",
        current=(
            status.Notice(
                subject="Störung bei Ausgangsbelegen",
                type="unplanned",
                state="identified",
                url="https://status.lexware.de/notices/x",
                began_at="2026-09-01T08:14:51.580Z",
                update="Wir arbeiten daran.",
            ),
        ),
    )

    text = status.summary(report)

    assert "Störung bei Ausgangsbelegen" in text
    assert "Ursache gefunden" in text  # 'identified' übersetzt
    assert "Wir arbeiten daran." in text
    assert "status.lexware.de" in text


def test_upcoming_maintenance_says_geplant_ab_not_seit() -> None:
    notice = status.Notice(
        subject="Wartung",
        type="planned",
        state="scheduled",
        url="https://status.lexware.de/notices/y",
        begins_at="2026-09-05T22:00:00.000Z",
    )
    text = status.describe_notice(notice)
    assert "geplant ab" in text
    assert "seit" not in text


def test_unparsable_timestamp_is_dropped_not_crashed() -> None:
    notice = status.Notice(
        subject="Störung",
        type="unplanned",
        state="investigating",
        url="https://status.lexware.de/notices/z",
        began_at="übermorgen",
    )
    assert status.describe_notice(notice) == "Störung (wird untersucht)"
