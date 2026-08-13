from __future__ import annotations

import base64
import json
from pathlib import Path

import httpx
import pytest
import respx
from fastmcp import Client

from lxw_cli.mcp_server import mcp


@pytest.fixture(autouse=True)
def _reset_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset the module-level lazy client between tests."""
    import lxw_cli.mcp_server as mod

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
        assert "update_contact" in names
        assert "update_article" in names
        assert "request_feature" in names
        assert "version" in names
        assert "continue_document" in names
        assert "upload_voucher_file" in names
        assert "download_voucher_file" in names
        assert "list_posting_categories" in names
        assert "get_payment_status" in names
        assert "create_dunning" in names
        assert "get_dunning" in names
        assert "download_dunning_pdf" in names


@pytest.mark.asyncio
async def test_version_tool_reports_package_version() -> None:
    from lxw_cli import __version__

    async with Client(mcp) as c:
        result = await c.call_tool("version", {})
    assert result.data["version"] == __version__
    assert result.data["produkt"] == "Lexware-Assistent"


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
    # Return type is `str | File` (File over HTTP), so the path arrives as
    # text content rather than structured data.
    saved_path = Path(result.content[0].text)
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


_UUID = "1a3c79ca-1804-4ab6-a3a5-915cc762b2ce"


@respx.mock
@pytest.mark.asyncio
async def test_upload_voucher_file_via_base64() -> None:
    route = respx.post(f"https://api.lexware.io/v1/vouchers/{_UUID}/files").mock(
        return_value=httpx.Response(201, json={"id": "file-1"})
    )
    payload = base64.b64encode(b"%PDF-1.7 x").decode()
    async with Client(mcp) as c:
        result = await c.call_tool(
            "upload_voucher_file",
            {"identifier": _UUID, "file_base64": payload, "filename": "beleg.pdf"},
        )
    assert result.data == {"id": "file-1"}
    sent = route.calls.last.request.content
    assert b"beleg.pdf" in sent
    assert b"%PDF-1.7 x" in sent


@pytest.mark.asyncio
async def test_upload_voucher_file_requires_a_source() -> None:
    async with Client(mcp) as c:
        with pytest.raises(Exception, match="file_path|file_base64"):
            await c.call_tool("upload_voucher_file", {"identifier": _UUID})


@respx.mock
@pytest.mark.asyncio
async def test_download_voucher_file_saves_to_path(tmp_path: Path) -> None:
    respx.get("https://api.lexware.io/v1/files/f1").mock(
        return_value=httpx.Response(
            200,
            content=b"\x89PNG fake",
            headers={
                "Content-Type": "image/png",
                "Content-Disposition": 'attachment; filename="scan.png"',
            },
        )
    )
    async with Client(mcp) as c:
        result = await c.call_tool(
            "download_voucher_file", {"file_id": "f1", "output_dir": str(tmp_path)}
        )
    saved_path = Path(result.content[0].text)
    assert saved_path.exists()
    assert saved_path.name == "scan.png"
    assert saved_path.read_bytes() == b"\x89PNG fake"


@respx.mock
@pytest.mark.asyncio
async def test_list_posting_categories_tool() -> None:
    respx.get("https://api.lexware.io/v1/posting-categories").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"id": "c1", "name": "Erlöse", "type": "income"},
                {"id": "c2", "name": "Wareneinkauf", "type": "expense"},
            ],
        )
    )
    async with Client(mcp) as c:
        result = await c.call_tool("list_posting_categories", {"category_type": "income"})
    assert [c["id"] for c in result.data] == ["c1"]


@respx.mock
@pytest.mark.asyncio
async def test_get_payment_status_tool() -> None:
    respx.get(f"https://api.lexware.io/v1/payments/{_UUID}").mock(
        return_value=httpx.Response(
            200, json={"paymentStatus": "open", "openAmount": 71.4, "currency": "EUR"}
        )
    )
    async with Client(mcp) as c:
        result = await c.call_tool("get_payment_status", {"identifier": _UUID})
    assert result.data["paymentStatus"] == "open"
    assert result.data["openAmount"] == 71.4


@respx.mock
@pytest.mark.asyncio
async def test_create_dunning_pursues_invoice() -> None:
    src = "aaaa1111-2222-3333-4444-555566667777"
    respx.get("https://api.lexware.io/v1/voucherlist").mock(
        return_value=httpx.Response(
            200,
            json={
                "content": [
                    {"id": src, "voucherNumber": "FB1", "voucherType": "salesinvoice"}
                ]
            },
        )
    )
    respx.get(f"https://api.lexware.io/v1/invoices/{src}").mock(
        return_value=httpx.Response(
            200,
            json={"id": src, "lineItems": [], "totalPrice": {"currency": "EUR"}},
        )
    )
    route = respx.post("https://api.lexware.io/v1/dunnings").mock(
        return_value=httpx.Response(201, json={"id": "dun-1"})
    )
    async with Client(mcp) as c:
        result = await c.call_tool("create_dunning", {"identifier": "FB1"})
    assert result.data["id"] == "dun-1"
    assert f"precedingSalesVoucherId={src}" in str(route.calls.last.request.url)


@respx.mock
@pytest.mark.asyncio
async def test_get_dunning_tool_by_id() -> None:
    respx.get(f"https://api.lexware.io/v1/dunnings/{_UUID}").mock(
        return_value=httpx.Response(200, json={"id": _UUID, "voucherStatus": "draft"})
    )
    async with Client(mcp) as c:
        result = await c.call_tool("get_dunning", {"dunning_id": _UUID})
    assert result.data["id"] == _UUID
