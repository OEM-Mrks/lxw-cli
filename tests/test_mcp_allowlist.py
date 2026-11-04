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


def test_enforce_no_org_blocks_even_with_fail_open(monkeypatch, tmp_path) -> None:
    """A profile without organization is not 'unreachable' — no fail-open."""
    gate = _gate(monkeypatch, tmp_path, "enforce")
    assert not gate.evaluate_profile({"companyName": "NoOrg"}, "k").allowed
    gate2 = _gate(monkeypatch, tmp_path, "enforce", LXW_MCP_ALLOWLIST_FAIL_OPEN="1")
    assert not gate2.evaluate_profile({"companyName": "NoOrg"}, "k").allowed


@respx.mock
def test_fail_open_only_when_lexware_unreachable(monkeypatch, tmp_path) -> None:
    route = respx.get(f"{LEXWARE_API}/v1/profile")
    gate = _gate(
        monkeypatch, tmp_path, "enforce",
        LXW_MCP_ALLOWLIST=KNOWN, LXW_MCP_ALLOWLIST_FAIL_OPEN="1",
    )
    route.mock(return_value=httpx.Response(503))
    assert asyncio.run(gate.evaluate("key-a")).allowed
    route.mock(side_effect=httpx.ConnectError("down"))
    assert asyncio.run(gate.evaluate("key-b")).allowed
    # A rejected key is an answer, not an outage: stays blocked.
    route.mock(return_value=httpx.Response(401))
    assert not asyncio.run(gate.evaluate("key-c")).allowed


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


# ---------------------------------------------------------------------------
# Blocking single organizations ("blocked": true)
# ---------------------------------------------------------------------------


def _write_list(tmp_path: Path, data: object, bump: int = 0) -> None:
    import os

    path = tmp_path / "allowlist.json"
    path.write_text(json.dumps(data))
    if bump:
        st = path.stat()
        os.utime(path, (st.st_atime, st.st_mtime + bump))


def test_log_mode_blocks_a_blocked_org(monkeypatch, tmp_path) -> None:
    _write_list(tmp_path, {UNKNOWN: {"note": "Fremde GmbH", "blocked": True}})
    gate = _gate(monkeypatch, tmp_path, "log", LXW_MCP_ALLOWLIST_CONTACT="mo@oemedia.de")
    d = gate.evaluate_profile(PROFILE_UNKNOWN, "k")
    assert not d.allowed and d.reason == "blocked"
    assert "Zugang" in d.user_message and "beendet" in d.user_message
    assert "mo@oemedia.de" in d.user_message
    assert UNKNOWN[:8] in d.user_message
    # other orgs are unaffected in log mode
    assert gate.evaluate_profile(PROFILE_KNOWN, "k2").allowed


def test_enforce_blocked_entry_is_not_listed(monkeypatch, tmp_path) -> None:
    _write_list(
        tmp_path,
        {KNOWN: {"note": "OE Media"}, UNKNOWN: {"note": "Fremde GmbH", "blocked": True}},
    )
    gate = _gate(monkeypatch, tmp_path, "enforce")
    assert gate.evaluate_profile(PROFILE_KNOWN, "k").allowed
    d = gate.evaluate_profile(PROFILE_UNKNOWN, "k2")
    assert not d.allowed and d.reason == "blocked"


def test_block_wins_over_inline_env_list(monkeypatch, tmp_path) -> None:
    _write_list(tmp_path, {KNOWN: {"blocked": True}})
    gate = _gate(monkeypatch, tmp_path, "enforce", LXW_MCP_ALLOWLIST=KNOWN)
    assert gate.evaluate_profile(PROFILE_KNOWN, "k").reason == "blocked"


def test_blocked_false_or_other_values_do_not_block(monkeypatch, tmp_path) -> None:
    _write_list(tmp_path, {KNOWN: {"blocked": False}, UNKNOWN: {"blocked": "ja"}})
    gate = _gate(monkeypatch, tmp_path, "log")
    assert gate.evaluate_profile(PROFILE_KNOWN, "k").reason == "listed"
    assert gate.evaluate_profile(PROFILE_UNKNOWN, "k2").allowed


def test_block_and_unblock_take_effect_without_restart(monkeypatch, tmp_path) -> None:
    _write_list(tmp_path, {KNOWN: {"note": "OE Media"}})
    gate = _gate(monkeypatch, tmp_path, "log")
    assert gate.evaluate_profile(PROFILE_KNOWN, "k").allowed
    _write_list(tmp_path, {KNOWN: {"note": "OE Media", "blocked": True}}, bump=10)
    assert not gate.evaluate_profile(PROFILE_KNOWN, "k").allowed
    _write_list(tmp_path, {KNOWN: {"note": "OE Media"}}, bump=20)
    assert gate.evaluate_profile(PROFILE_KNOWN, "k").allowed


@respx.mock
def test_evaluate_blocks_existing_token_of_blocked_org(monkeypatch, tmp_path) -> None:
    """The per-request path (every tool call, upload link) honours the block."""
    respx.get(f"{LEXWARE_API}/v1/profile").mock(
        return_value=httpx.Response(200, json=PROFILE_UNKNOWN)
    )
    gate = _gate(monkeypatch, tmp_path, "log")
    assert asyncio.run(gate.evaluate("secret-key")).allowed
    _write_list(tmp_path, {UNKNOWN: {"blocked": True}}, bump=10)
    # cached org resolution, decision re-derived from the current list
    assert not asyncio.run(gate.evaluate("secret-key")).allowed
