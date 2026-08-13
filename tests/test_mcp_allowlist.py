"""KYC allow-list gate: modes, hot-reload, pending queue, per-request path."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import respx

from lxw_cli.mcp_allowlist import AllowList

LEXWARE_API = "https://api.lexware.io"
KNOWN = "bf687ad5-fc5e-4bcf-9e09-c5472bfcf022"
UNKNOWN = "99999999-dead-beef-0000-000000000000"

PROFILE_KNOWN = {
    "organizationId": KNOWN,
    "companyName": "OE Media",
    "created": {"userEmail": "markus@oemedia.de"},
}
PROFILE_UNKNOWN = {
    "organizationId": UNKNOWN,
    "companyName": "Fremde GmbH",
    "created": {"userEmail": "x@fremd.de"},
}


def _gate(monkeypatch, tmp_path: Path, mode: str, **env) -> AllowList:
    for key in list(env):
        monkeypatch.setenv(key, env[key])
    monkeypatch.setenv("LXW_MCP_ALLOWLIST_MODE", mode)
    return AllowList(tmp_path, LEXWARE_API)


# ---------------------------------------------------------------------------
# Modes via evaluate_profile (no network)
# ---------------------------------------------------------------------------


def test_off_is_inert(monkeypatch, tmp_path) -> None:
    gate = _gate(monkeypatch, tmp_path, "off")
    assert not gate.enabled
    assert gate.evaluate_profile(PROFILE_UNKNOWN, "k").allowed
    # off never touches disk
    assert not (tmp_path / "pending.json").exists()


def test_log_allows_and_records_pending(monkeypatch, tmp_path) -> None:
    gate = _gate(monkeypatch, tmp_path, "log")
    d = gate.evaluate_profile(PROFILE_UNKNOWN, "k")
    assert d.allowed and d.reason == "logged"
    pending = json.loads((tmp_path / "pending.json").read_text())
    assert UNKNOWN in pending
    assert pending[UNKNOWN]["companyName"] == "Fremde GmbH"
    assert pending[UNKNOWN]["userEmail"] == "x@fremd.de"
    assert pending[UNKNOWN]["count"] == 1
    assert pending[UNKNOWN]["last_decision"] == "allowed"
    # seen again -> counter increments, one entry per org
    gate.evaluate_profile(PROFILE_UNKNOWN, "k")
    pending = json.loads((tmp_path / "pending.json").read_text())
    assert pending[UNKNOWN]["count"] == 2


def test_enforce_blocks_unknown(monkeypatch, tmp_path) -> None:
    gate = _gate(monkeypatch, tmp_path, "enforce", LXW_MCP_ALLOWLIST_CONTACT="hi@oemedia.de")
    d = gate.evaluate_profile(PROFILE_UNKNOWN, "k")
    assert not d.allowed
    assert "freigeschaltet" in d.user_message
    assert "hi@oemedia.de" in d.user_message
    # blocked orgs still land in the KYC queue
    pending = json.loads((tmp_path / "pending.json").read_text())
    assert pending[UNKNOWN]["last_decision"] == "blocked"


def test_enforce_allows_known_via_env(monkeypatch, tmp_path) -> None:
    gate = _gate(monkeypatch, tmp_path, "enforce", LXW_MCP_ALLOWLIST=KNOWN)
    assert gate.evaluate_profile(PROFILE_KNOWN, "k").allowed


def test_enforce_hot_reloads_allowlist_file(monkeypatch, tmp_path) -> None:
    gate = _gate(monkeypatch, tmp_path, "enforce")
    assert not gate.evaluate_profile(PROFILE_KNOWN, "k").allowed
    # add the org to the file — no restart
    (tmp_path / "allowlist.json").write_text(json.dumps({KNOWN: {"note": "OE Media"}}))
    import os

    # bump mtime deterministically (no sleep)
    st = (tmp_path / "allowlist.json").stat()
    os.utime(tmp_path / "allowlist.json", (st.st_atime, st.st_mtime + 10))
    assert gate.evaluate_profile(PROFILE_KNOWN, "k").allowed


def test_enforce_no_org_fails_closed_but_fail_open_overrides(monkeypatch, tmp_path) -> None:
    gate = _gate(monkeypatch, tmp_path, "enforce")
    assert not gate.evaluate_profile({"companyName": "NoOrg"}, "k").allowed
    gate2 = _gate(monkeypatch, tmp_path, "enforce", LXW_MCP_ALLOWLIST_FAIL_OPEN="1")
    assert gate2.evaluate_profile({"companyName": "NoOrg"}, "k").allowed


def test_accepts_json_array_and_object_files(monkeypatch, tmp_path) -> None:
    (tmp_path / "allowlist.json").write_text(json.dumps([KNOWN]))
    gate = _gate(monkeypatch, tmp_path, "enforce")
    assert gate.evaluate_profile(PROFILE_KNOWN, "k").allowed


# ---------------------------------------------------------------------------
# Per-request path evaluate() — resolves the org from Lexware (mocked)
# ---------------------------------------------------------------------------


@respx.mock
def test_evaluate_resolves_org_and_caches(monkeypatch, tmp_path) -> None:
    route = respx.get(f"{LEXWARE_API}/v1/profile").mock(
        return_value=httpx.Response(200, json=PROFILE_KNOWN)
    )
    gate = _gate(monkeypatch, tmp_path, "enforce", LXW_MCP_ALLOWLIST=KNOWN)
    d1 = asyncio.run(gate.evaluate("secret-key"))
    d2 = asyncio.run(gate.evaluate("secret-key"))
    assert d1.allowed and d2.allowed
    # second call served from cache -> only one upstream profile fetch
    assert route.call_count == 1


@respx.mock
def test_evaluate_blocks_unknown_org(monkeypatch, tmp_path) -> None:
    respx.get(f"{LEXWARE_API}/v1/profile").mock(
        return_value=httpx.Response(200, json=PROFILE_UNKNOWN)
    )
    gate = _gate(monkeypatch, tmp_path, "enforce", LXW_MCP_ALLOWLIST=KNOWN)
    assert not asyncio.run(gate.evaluate("secret-key")).allowed


@respx.mock
def test_evaluate_off_makes_no_request(monkeypatch, tmp_path) -> None:
    route = respx.get(f"{LEXWARE_API}/v1/profile").mock(
        return_value=httpx.Response(200, json=PROFILE_KNOWN)
    )
    gate = _gate(monkeypatch, tmp_path, "off")
    assert asyncio.run(gate.evaluate("secret-key")).allowed
    assert route.call_count == 0


@respx.mock
def test_evaluate_unreachable_fails_closed_in_enforce(monkeypatch, tmp_path) -> None:
    respx.get(f"{LEXWARE_API}/v1/profile").mock(side_effect=httpx.ConnectError("boom"))
    gate = _gate(monkeypatch, tmp_path, "enforce")
    d = asyncio.run(gate.evaluate("secret-key"))
    assert not d.allowed and d.reason == "unreachable"
    # log mode stays available during an outage
    gate_log = _gate(monkeypatch, tmp_path, "log")
    assert asyncio.run(gate_log.evaluate("secret-key")).allowed
