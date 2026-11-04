"""Upload links: the original receipt reaches Lexware without passing the model.

A chat assistant can only hand a file to a tool by writing its bytes as
base64 *text* into the tool call. For a real PDF that is tens of thousands of
characters — assistants shrink the file to a thumbnail first, and Lexware ends
up with an unreadable 85×120 px JPEG. An upload link avoids that: the tool
returns a short-lived URL bound to one voucher, and the original file is
POSTed there directly — by the assistant's code sandbox (``curl -F
file=@…``) or by the user in the browser (plain HTML form, no JavaScript).

The link token is a sealed Fernet blob like every other token of the HTTP
server (see :mod:`lxw_cli.mcp_auth`): it carries the user's Lexware key and
the voucher id, so the server stores nothing. What a leaked link allows is
deliberately narrow: for UPLOAD_LINK_TTL, attach files to that one voucher —
it can't read, list or change anything.
"""

from __future__ import annotations

import asyncio
import html
import json
from typing import TYPE_CHECKING, Any

from cryptography.fernet import InvalidToken, MultiFernet
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

if TYPE_CHECKING:
    from lxw_cli.mcp_allowlist import AllowList

UPLOAD_LINK_TTL = 30 * 60
UPLOAD_PATH = "/upload/{token}"
# Several files per link are fine (multi-page scans), but bounded.
MAX_FILES_PER_REQUEST = 5
UPLOAD_RATE_LIMIT = 30  # POSTs per client IP …
UPLOAD_RATE_WINDOW = 600  # … within this many seconds
_KIND = "upload"

# Formats Lexware accepts on vouchers, identified by content (magic bytes).
ACCEPTED_TYPES = frozenset({"application/pdf", "image/jpeg", "image/png", "application/xml"})


class UploadLinks:
    """Mint upload links and serve the upload page/endpoint."""

    def __init__(
        self,
        *,
        sealer: MultiFernet,
        public_url: str,
        allow: AllowList | None = None,
    ) -> None:
        from lxw_cli.mcp_auth import RateLimiter

        self._sealer = sealer
        self._public_url = public_url.rstrip("/")
        self._allow = allow
        self._limiter = RateLimiter(UPLOAD_RATE_LIMIT, UPLOAD_RATE_WINDOW)

    # -- tokens -----------------------------------------------------------------

    def create(self, *, api_key: str, voucher_id: str, label: str = "") -> dict[str, Any]:
        body = json.dumps(
            {"t": _KIND, "key": api_key, "v": voucher_id, "l": label[:80]},
            separators=(",", ":"),
        )
        token = self._sealer.encrypt(body.encode()).decode()
        return {
            "uploadUrl": f"{self._public_url}/upload/{token}",
            "expiresInMinutes": UPLOAD_LINK_TTL // 60,
        }

    def _unseal(self, token: str) -> dict[str, Any] | None:
        try:
            raw = self._sealer.decrypt(token.encode(), ttl=UPLOAD_LINK_TTL)
            data = json.loads(raw)
        except (InvalidToken, UnicodeEncodeError, ValueError):
            return None
        if not isinstance(data, dict) or data.get("t") != _KIND:
            return None
        return data

    # -- HTTP -------------------------------------------------------------------

    def routes(self) -> list[Route]:
        return [
            Route(UPLOAD_PATH, self._get, methods=["GET"]),
            Route(UPLOAD_PATH, self._post, methods=["POST"]),
        ]

    async def _get(self, request: Request) -> Response:
        data = self._unseal(request.path_params["token"])
        if data is None:
            return _page(_EXPIRED_TITLE, _EXPIRED_TEXT, status=410)
        return _form_page(data.get("l") or "")

    async def _post(self, request: Request) -> Response:
        wants_html = "text/html" in request.headers.get("accept", "")

        def fail(message: str, status: int) -> Response:
            if wants_html:
                return _page("Upload nicht möglich", message, status=status)
            return JSONResponse({"ok": False, "error": message}, status_code=status)

        client = request.client
        if not self._limiter.allow(client.host if client else "unknown"):
            return fail("Zu viele Uploads — bitte in ein paar Minuten erneut.", 429)
        data = self._unseal(request.path_params["token"])
        if data is None:
            return fail(_EXPIRED_TEXT, 410)
        if self._allow is not None and not (await self._allow.evaluate(data["key"])).allowed:
            return fail("Diese Lexware-Installation ist für den Server nicht freigegeben.", 403)

        from lxw_cli.mcp_server import MAX_UPLOAD_BYTES

        try:
            form = await request.form(max_files=MAX_FILES_PER_REQUEST, max_fields=5)
        except Exception:  # noqa: BLE001 — malformed multipart / too many parts
            return fail(
                f"Ungültige Anfrage — bitte bis zu {MAX_FILES_PER_REQUEST} Dateien "
                "im Feld „file“ senden.",
                400,
            )
        uploads = [
            item
            for item in form.getlist("file")
            if hasattr(item, "read") and getattr(item, "filename", None)
        ]
        if not uploads:
            return fail("Keine Datei erhalten — bitte eine Datei im Feld „file“ senden.", 400)

        from lxw_cli.core import services
        from lxw_cli.mcp_auth import pool
        from lxw_cli.output import safe_filename

        lexware = pool.get(data["key"])
        results: list[dict[str, Any]] = []
        for item in uploads:
            content = await item.read(MAX_UPLOAD_BYTES + 1)
            name = safe_filename(item.filename or "beleg")
            if len(content) > MAX_UPLOAD_BYTES:
                results.append({"filename": name, "ok": False, "error": "Datei zu groß."})
                continue
            name, ctype = services.upload_name_and_type(name, content, None)
            if ctype not in ACCEPTED_TYPES:
                results.append(
                    {
                        "filename": name,
                        "ok": False,
                        "error": "Nur PDF, JPG, PNG oder XML werden angenommen.",
                    }
                )
                continue
            try:
                stored = await asyncio.to_thread(
                    services.upload_voucher_file,
                    lexware,
                    data["v"],
                    filename=name,
                    content=content,
                    content_type=ctype,
                )
            except Exception as exc:  # noqa: BLE001 — report per file
                results.append({"filename": name, "ok": False, "error": str(exc)})
                continue
            results.append({"filename": name, "ok": True, "file": stored})

        ok = all(r["ok"] for r in results)
        status = 200 if ok else (207 if any(r["ok"] for r in results) else 400)
        if not wants_html:
            return JSONResponse({"ok": ok, "files": results}, status_code=status)
        lines = "".join(
            f"<li>{html.escape(r['filename'])}: "
            + ("angehängt ✓" if r["ok"] else html.escape(r["error"]))
            + "</li>"
            for r in results
        )
        title = "Beleg angehängt" if ok else "Nicht alles hat geklappt"
        text = (
            "Die Datei ist jetzt in Lexware am Beleg. Du kannst dieses Fenster schließen."
            if ok
            else "Bitte die fehlgeschlagenen Dateien erneut versuchen."
        )
        return _page(title, text, extra=f"<ul>{lines}</ul>", status=status)


# ---------------------------------------------------------------------------
# HTML (no JavaScript — CSP default-src 'none'; a file input accepts drops)
# ---------------------------------------------------------------------------

_EXPIRED_TITLE = "Link abgelaufen"
_EXPIRED_TEXT = (
    "Dieser Upload-Link ist abgelaufen oder ungültig. Bitte im Chat einen "
    "neuen Upload-Link für den Beleg anfordern."
)


def _headers() -> dict[str, str]:
    from lxw_cli.mcp_auth import _PAGE_HEADERS

    return dict(_PAGE_HEADERS)


def _style() -> str:
    from lxw_cli.mcp_auth import _PAGE_STYLE

    return _PAGE_STYLE + """
  .drop { display: block; margin-top: 1rem; padding: 1.5rem 1rem; text-align: center;
          border: 2px dashed light-dark(#cbd5e1, #374151); border-radius: 10px; }
  .drop input { width: 100%; font-size: .9rem; }
  ul { font-size: .9rem; padding-left: 1.2rem; }
"""


def _page(
    title: str, text: str, *, extra: str = "", status: int = 200
) -> HTMLResponse:
    body = f"""<!doctype html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>Beleg hochladen</title><style>{_style()}</style></head><body>
<div class="card"><h1>{html.escape(title)}</h1><p>{html.escape(text)}</p>{extra}</div>
</body></html>"""
    return HTMLResponse(body, status_code=status, headers=_headers())


def _form_page(label: str) -> HTMLResponse:
    which = (
        f" für Beleg <strong>{html.escape(label)}</strong>" if label else ""
    )
    body = f"""<!doctype html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>Beleg hochladen</title><style>{_style()}</style></head><body>
<form class="card" method="post" enctype="multipart/form-data">
  <h1>Belegdatei hochladen</h1>
  <p>Original-PDF oder Foto{which} auswählen oder hierher ziehen. Die Datei wird
     unverändert in Lexware an den Beleg gehängt (PDF, JPG, PNG — max. 5 MB).</p>
  <label class="drop"><input type="file" name="file" multiple required
     accept=".pdf,.jpg,.jpeg,.png,.xml,application/pdf,image/jpeg,image/png"></label>
  <button type="submit">Hochladen</button>
  <p>Der Link gilt {UPLOAD_LINK_TTL // 60} Minuten und kann nur Dateien an diesen
     einen Beleg hängen.</p>
</form></body></html>"""
    return HTMLResponse(body, headers=_headers())
