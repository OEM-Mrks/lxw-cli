from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx
from fastmcp import Client

from lexware_cli.mcp_server import mcp


@pytest.fixture(autouse=True)
def _reset_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset the module-level lazy client between tests."""
    import lexware_cli.mcp_server as mod

    mod._client = None


@pytest.mark.asyncio
async def test_tools_are_registered() -> None:
    async with Client(mcp) as c:
        tools = await c.list_tools()
        names = {t.name for t in tools}
        assert "profile" in names
        assert "list_invoices" in names
        assert "get_invoice" in names
        assert "download_invoice_pdf" in names
        assert "create_contact" in names


@respx.mock
@pytest.mark.asyncio
async def test_profile_tool() -> None:
    respx.get("https://api.lexware.io/v1/profile").mock(
        return_value=httpx.Response(200, json={"companyName": "Acme GmbH"})
    )
    async with Client(mcp) as c:
        result = await c.call_tool("profile", {})
    assert result.data == {"companyName": "Acme GmbH"}


@respx.mock
@pytest.mark.asyncio
async def test_list_invoices_uses_voucherlist_with_invoice_types() -> None:
    route = respx.get("https://api.lexware.io/v1/voucherlist").mock(
        return_value=httpx.Response(
            200,
            json={
                "content": [
                    {
                        "id": "abc",
                        "voucherType": "salesinvoice",
                        "voucherNumber": "FB2600682",
                        "totalAmount": 71.4,
                    }
                ],
                "last": True,
            },
        )
    )
    async with Client(mcp) as c:
        result = await c.call_tool("list_invoices", {"limit": 5})
    sent_url = str(route.calls.last.request.url)
    assert "voucherType=salesinvoice%2Cinvoice%2Cdownpaymentinvoice" in sent_url
    # Archived excluded by default (server-side).
    assert "archived=false" in sent_url
    assert len(result.data) == 1
    assert result.data[0]["voucherNumber"] == "FB2600682"


@respx.mock
@pytest.mark.asyncio
async def test_list_contacts_excludes_archived_by_default() -> None:
    respx.get("https://api.lexware.io/v1/contacts").mock(
        return_value=httpx.Response(
            200,
            json={
                "content": [
                    {"id": "active", "archived": False},
                    {"id": "old", "archived": True},
                ],
                "last": True,
            },
        )
    )
    async with Client(mcp) as c:
        default = await c.call_tool("list_contacts", {})
        with_archived = await c.call_tool("list_contacts", {"include_archived": True})
    assert [c["id"] for c in default.data] == ["active"]
    assert {c["id"] for c in with_archived.data} == {"active", "old"}


@respx.mock
@pytest.mark.asyncio
async def test_get_invoice_resolves_voucher_number() -> None:
    # First the resolver looks up by voucherNumber
    respx.get(
        "https://api.lexware.io/v1/voucherlist",
        params={"voucherNumber": "FB2600682"},
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "content": [{"id": "resolved-uuid", "voucherNumber": "FB2600682"}],
                "last": True,
            },
        )
    )
    # Then the specific invoice endpoint is hit
    respx.get("https://api.lexware.io/v1/invoices/resolved-uuid").mock(
        return_value=httpx.Response(
            200, json={"id": "resolved-uuid", "voucherNumber": "FB2600682"}
        )
    )
    async with Client(mcp) as c:
        result = await c.call_tool("get_invoice", {"identifier": "FB2600682"})
    assert result.data["voucherNumber"] == "FB2600682"


@respx.mock
@pytest.mark.asyncio
async def test_download_invoice_pdf_saves_to_path(tmp_path: Path) -> None:
    uuid = "1a3c79ca-1804-4ab6-a3a5-915cc762b2ce"
    respx.get(f"https://api.lexware.io/v1/invoices/{uuid}/file").mock(
        return_value=httpx.Response(200, content=b"%PDF-1.7 fake")
    )
    async with Client(mcp) as c:
        result = await c.call_tool(
            "download_invoice_pdf",
            {"identifier": uuid, "output_dir": str(tmp_path)},
        )
    saved_path = Path(result.data)
    assert saved_path.exists()
    assert saved_path.read_bytes().startswith(b"%PDF")
    assert saved_path.parent == tmp_path


@respx.mock
@pytest.mark.asyncio
async def test_create_contact_posts_body() -> None:
    route = respx.post("https://api.lexware.io/v1/contacts").mock(
        return_value=httpx.Response(200, json={"id": "new-uuid"})
    )
    async with Client(mcp) as c:
        result = await c.call_tool(
            "create_contact",
            {"body": {"roles": {"customer": {}}, "company": {"name": "Acme"}}},
        )
    assert result.data == {"id": "new-uuid"}
    body = json.loads(route.calls.last.request.content)
    assert body["company"]["name"] == "Acme"
