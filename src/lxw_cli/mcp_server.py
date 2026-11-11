"""FastMCP server exposing the Lexware Office API to Claude.

A thin frontend over :mod:`lxw_cli.core.services` — the exact same
UI-agnostic operations the CLI uses, so both stay in sync by construction.

Runs in two modes:

- **stdio** (``lxw-mcp [--profile NAME]``): single-user, the key comes
  from the local config exactly as for the CLI. One process serves one
  Lexware installation; several installations run as several servers
  (``lexware-oemedia``, ``lexware-demo``), each pinned to its profile.
- **HTTP** (``lxw-mcp-http``): multi-user. Each request brings its own
  Lexware API key — either directly as ``Authorization: Bearer <key>``
  or wrapped in an OAuth token issued by :mod:`lxw_cli.mcp_auth`. No
  key is ever stored on the server. Several installations = several
  connectors, each with its own key; ``?installation=<name>`` on the
  connector URL labels them (and keeps the URLs distinct).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from contextvars import ContextVar
from pathlib import Path
from typing import Any, NotRequired

import httpx
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import Middleware
from fastmcp.utilities.types import File

# Pydantic needs typing_extensions' TypedDict on Python < 3.12.
from typing_extensions import TypedDict

from lxw_cli import __build__, __version__
from lxw_cli.config import load_config
from lxw_cli.core import services, status
from lxw_cli.core.client import LexwareClient
from lxw_cli.feature_request import compose_feature_request
from lxw_cli.output import safe_filename

mcp: FastMCP = FastMCP(
    name="lexware",
    version=__version__,
    instructions=(
        # --- How to talk to the end user (this is an end-customer product) ---
        "You are a Lexware Office assistant inside a finished end-customer "
        "product. Speak plainly and non-technically. "
        "When the user asks what you can do, describe CAPABILITIES in everyday "
        "language — for example: view, create and download invoices, quotations, "
        "order confirmations and delivery notes as PDF; look up, create and edit "
        "customers, vendors and articles. Never expose internal tool names, "
        "endpoint or API names, or technical field names, and do not describe "
        "how the system works under the hood. Say WHAT it can do, not what it is "
        "called. "
        "Present every result in clear, human-friendly language (short sentences "
        "or a simple table) — never raw JSON and never technical field names. "
        # --- Product boundary ---
        "This is a finished product: do NOT offer to develop, extend, change or "
        "'quickly add' functionality yourself and do not speculate about building "
        "features. If the user wants something the product cannot do, use the "
        "feature-request capability to compose a short non-binding request and "
        "show it to the user to email to the vendor themselves. "
        "If the user asks which version is running, use the version capability "
        "and tell them the version. "
        # --- Operating status ---
        "If the user asks whether Lexware Office is down, why something is slow "
        "or failing, or whether there is a known problem, use the service-status "
        "capability and report it in plain language. Do the same when an action "
        "just failed and the user wants to know why. Never invent an outage — "
        "only report what the status capability actually returns. "
        # --- Operational notes for you, never surface these to the user ---
        "(Internal, do not surface: documents can be addressed by their number "
        "such as 'FB2600682' or an id; PDF downloads return the PDF itself. When "
        "a document line item is based on an article, fetch that article first "
        "and copy its description into the line item's description. A receipt "
        "file the user shares must reach Lexware as the unchanged original: "
        "never render, convert, compress or shrink it — use the upload link "
        "returned when the voucher is created and follow its instructions.)"
        # --- Several installations ---
        " Several Lexware installations (companies) may be connected at the "
        "same time as separate servers. Never mix up their data: before "
        "creating or changing anything, make sure you use the installation "
        "the user means — if unclear, ask, and the company profile tells you "
        "which company a server belongs to."
    ),
)
_BASE_INSTRUCTIONS: str = mcp.instructions or ""

# Profile this stdio process is pinned to (None = classic resolution).
_profile: str | None = None

class StatusHintMiddleware(Middleware):
    """Erklärt Serverfehler mit dem Lexware-Betriebsstatus.

    Scheitert ein Aufruf an Lexware selbst (5xx, Timeout, dauerhaftes 429),
    ist die interessante Frage fast immer "liegt das an mir oder an
    Lexware?". Die Statusseite beantwortet das ohne API-Key, also hängen
    wir die Antwort direkt an die Fehlermeldung — statt sie den Nutzer
    separat suchen zu lassen.

    Läuft alles normal (oder ist der Fehler ein Eingabe-/Auth-Fehler),
    bleibt die Meldung unverändert: ein Statushinweis unter jedem 400 wäre
    nur Rauschen. Der Hinweis darf den eigentlichen Fehler außerdem niemals
    verschlucken — jeder Fehler auf dem Diagnoseweg wird ignoriert.
    """

    async def on_call_tool(self, context: Any, call_next: Any) -> Any:
        try:
            return await call_next(context)
        except Exception as exc:
            hint: str | None = None
            try:
                # Synchroner httpx-Aufruf (meist Cache-Treffer) — im Thread,
                # damit der Event-Loop nicht blockiert.
                hint = await asyncio.to_thread(status.hint_for_exception, exc)
            except Exception:  # noqa: BLE001 — Diagnose nie vor den Fehler stellen
                hint = None
            if not hint:
                raise
            raise ToolError(f"{exc} {hint}") from exc


mcp.add_middleware(StatusHintMiddleware())


def _validation_errors(exc: BaseException) -> list[dict[str, Any]] | None:
    """The pydantic error list behind a rejected tool call, if it is one.

    Newer FastMCP wraps pydantic's error in its own ValidationError
    (``__cause__``); older versions raise the pydantic error directly.
    """
    from pydantic import ValidationError as PydanticValidationError

    for candidate in (exc, exc.__cause__):
        if isinstance(candidate, PydanticValidationError):
            return candidate.errors(include_url=False)
    return None


# Geratene Namen, die eindeutig einem echten Parameter entsprechen. Gilt nur,
# wenn das Tool den Zielnamen hat und den geratenen Namen nicht.
_ARGUMENT_ALIASES: dict[str, tuple[str, ...]] = {
    "status": ("voucher_status",),
    "type": ("voucher_type", "article_type"),
    "size": ("limit",),
    "page_size": ("limit",),
    "per_page": ("limit",),
    "max_results": ("limit",),
    "archived": ("include_archived",),
    "query": ("search",),
    "q": ("search",),
    # Belegdatei beim Anlegen/Anhängen.
    **{
        name: ("file_base64",)
        for name in (
            "base64",
            "file_content",
            "file_data",
            "content_base64",
            "pdf_base64",
            "file_content_base64",
        )
    },
    "file_name": ("filename",),
    # Beleg-/Artikel-Kennungen der Einzelabrufe heißen einheitlich identifier.
    **{
        name: ("identifier",)
        for name in (
            "voucher_id",
            "invoice_id",
            "quotation_id",
            "order_confirmation_id",
            "orderconfirmation_id",
            "order_id",
            "delivery_note_id",
            "deliverynote_id",
            "credit_note_id",
            "document_id",
            "voucher_number",
            "invoice_number",
            "document_number",
            "article_number",
            "number",
        )
    },
}

# Ohne Paginierung im Tool: page 0/1 ist die erste Seite und kann weg. Eine
# höhere Seite verwerfen wir nicht still, das würde falsche Ergebnisse liefern.
_FIRST_PAGE = (None, 0, 1, "0", "1")

_CAMEL_RE = re.compile(r"(?<=[a-z0-9])([A-Z])")

# Umbenennungen des laufenden Aufrufs, z. B. für das Aktivitätsprotokoll.
renamed_arguments: ContextVar[dict[str, str] | None] = ContextVar(
    "renamed_arguments", default=None
)

log = logging.getLogger("lxw_cli.mcp")


def _snake(name: str) -> str:
    return _CAMEL_RE.sub(r"_\1", name).lower()


def _alias_targets(name: str, properties: dict[str, Any]) -> list[str]:
    """Mögliche echte Parameternamen für einen geratenen Namen."""
    snake = _snake(name)
    targets = [snake, *_ARGUMENT_ALIASES.get(snake, ())]
    if snake == "id":
        if "identifier" in properties:
            targets.append("identifier")
        else:
            # update_article(id=…) → article_id, get_dunning(id=…) → dunning_id
            ids = [p for p in properties if p.endswith("_id")]
            if len(ids) == 1:
                targets.append(ids[0])
    return [t for t in targets if t != name]


def normalize_arguments(
    arguments: dict[str, Any], properties: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, str]]:
    """Ordnet geratene Parameternamen den echten zu.

    Gibt die bereinigten Argumente und die Umbenennungen (alt → neu, bzw.
    alt → "" für Verworfenes) zurück. Was nicht eindeutig passt, bleibt
    unverändert stehen; die Validierung meldet es dann wie bisher.
    """
    result = dict(arguments)
    renamed: dict[str, str] = {}
    for name, value in arguments.items():
        if name in properties:
            continue
        if name == "page" and "page" not in properties and value in _FIRST_PAGE:
            del result[name]
            renamed[name] = ""
            continue
        for target in _alias_targets(name, properties):
            if target not in properties:
                continue
            if target in result:
                # Doppelt mit gleichem Wert (identifier + invoice_id): harmlos.
                if result[target] == value:
                    del result[name]
                    renamed[name] = target
                break
            result[target] = result.pop(name)
            renamed[name] = target
            break
    return result, renamed


async def _with_normalized_arguments(context: Any) -> Any:
    renamed_arguments.set(None)
    message = context.message
    arguments = getattr(message, "arguments", None)
    if not arguments:
        return context
    tool_name = str(getattr(message, "name", "") or "")
    tool = await mcp.get_tool(tool_name)
    schema = getattr(tool, "parameters", None) or {}
    properties = schema.get("properties") or {}
    if not properties or all(name in properties for name in arguments):
        return context
    fixed, renamed = normalize_arguments(arguments, properties)
    if not renamed:
        return context
    renamed_arguments.set(renamed)
    log.warning(
        "Parameter umbenannt (%s): %s",
        tool_name,
        ", ".join(f"{old}→{new or '(verworfen)'}" for old, new in renamed.items()),
    )
    return context.copy(message=message.model_copy(update={"arguments": fixed}))


class ArgumentHintMiddleware(Middleware):
    """Übersetzt falsch geratene Parameternamen.

    KI-Clients raten Parameternamen gern, meist in der Schreibweise der
    Lexware-API (``voucherNumber``, ``contactId``) oder als ``id`` statt
    ``identifier``. Ist die Zuordnung eindeutig, benennen wir vor dem Aufruf
    still um. Bleibt danach etwas übrig, lehnt FastMCP das mit einem
    technischen Validierungsfehler ab; wir nennen stattdessen auf Deutsch,
    welcher Name unbekannt ist, welcher fehlt und welche Parameter das Tool
    wirklich hat — dann klappt der zweite Versuch sicher.
    """

    async def on_call_tool(self, context: Any, call_next: Any) -> Any:
        try:
            context = await _with_normalized_arguments(context)
        except Exception:  # noqa: BLE001 — im Zweifel unverändert aufrufen
            pass
        try:
            return await call_next(context)
        except Exception as exc:
            errors = _validation_errors(exc)
            message: str | None = None
            if errors:
                try:
                    message = await _argument_hint(
                        str(getattr(context.message, "name", "") or ""), errors
                    )
                except Exception:  # noqa: BLE001 — im Zweifel den Originalfehler zeigen
                    message = None
            if not message:
                raise
            raise ToolError(message) from exc


async def _argument_hint(tool_name: str, errors: list[dict[str, Any]]) -> str:
    unknown: list[str] = []
    missing: list[str] = []
    wrong: list[str] = []
    for err in errors:
        loc = err.get("loc") or ()
        field = str(loc[-1]) if loc else "?"
        kind = err.get("type", "")
        if kind == "unexpected_keyword_argument":
            unknown.append(field)
        elif kind in ("missing_argument", "missing"):
            missing.append(field)
        else:
            wrong.append(f"{field} ({err.get('msg', 'ungültiger Wert')})")

    parts = [f"Aufruf von {tool_name} mit falschen Parametern."]
    if unknown:
        parts.append("Diesen Parameter gibt es nicht: " + ", ".join(unknown) + ".")
    if missing:
        parts.append("Pflichtparameter fehlt: " + ", ".join(missing) + ".")
    if wrong:
        parts.append("Ungültiger Wert: " + "; ".join(wrong) + ".")

    tool = await mcp.get_tool(tool_name)
    schema = getattr(tool, "parameters", None) or {}
    properties = schema.get("properties") or {}
    if properties:
        required = set(schema.get("required") or ())
        allowed = ", ".join(
            f"{name} (Pflicht)" if name in required else name for name in properties
        )
        parts.append(f"Erlaubte Parameter: {allowed}.")
    if unknown or missing:
        parts.append("Bitte genau diese Namen verwenden und erneut aufrufen.")
    else:
        parts.append("Bitte den Wert korrigieren und erneut aufrufen.")
    return " ".join(parts)


# Registered after StatusHintMiddleware, so it runs inside it: the status
# hint only ever sees the (already translated) ToolError and leaves it alone.
mcp.add_middleware(ArgumentHintMiddleware())

_client: LexwareClient | None = None


def _client_get() -> LexwareClient:
    # Multi-user HTTP mode: the authenticated request carries the user's
    # own key; each key gets its own (cached) client.
    from lxw_cli.mcp_auth import pool, request_api_key

    api_key = request_api_key()
    if api_key is not None:
        return pool.get(api_key)
    # stdio mode: single user, key from the local config.
    global _client
    if _client is None:
        # The profile was fully resolved at startup (`serve_stdio`); "" keeps
        # the classic resolution without re-reading LEXWARE_PROFILE.
        _client = LexwareClient(load_config(_profile or ""))
    return _client


def _default_download_dir() -> Path:
    target = Path.home() / "Downloads" / "lexware"
    target.mkdir(parents=True, exist_ok=True)
    return target


# File types the server reads for upload or writes on download (stdio). The
# model chooses paths and file names, and its input can include attacker-
# controlled text (a contact note, an e-mail it was asked to process). These
# limits keep such a prompt injection from attaching e.g. ~/.ssh/id_rsa or a
# .env to a voucher, or dropping files into config/autostart directories.
ALLOWED_FILE_EXTENSIONS = frozenset({"pdf", "jpg", "jpeg", "png", "xml"})


def _hidden_component(path: Path) -> str | None:
    for part in path.parts:
        if part.startswith(".") and part not in (".", ".."):
            return part
    return None


def _output_dir(output_dir: str | None) -> Path:
    """Target directory for a download — default, or a checked custom one."""
    if not output_dir:
        return _default_download_dir()
    target = Path(output_dir).expanduser().resolve()
    hidden = _hidden_component(target)
    if hidden:
        raise ValueError(
            f"output_dir {target} liegt in einem versteckten Verzeichnis ({hidden}) — "
            "bitte ein normales Verzeichnis wählen."
        )
    target.mkdir(parents=True, exist_ok=True)
    return target


def _in_http_request() -> bool:
    try:
        from fastmcp.server.dependencies import get_http_request

        return get_http_request() is not None
    except Exception:  # noqa: BLE001 — outside any HTTP context
        return False


def _pdf_result(data: bytes, output_dir: str | None, filename: str) -> str | File:
    """Local stdio: save to disk and return the path (as documented).

    Over HTTP the server's filesystem is useless to the caller, so the
    PDF is returned inline as a binary resource instead.
    """
    if _in_http_request():
        # File appends the format as extension to the synthetic resource URI;
        # strip a trailing .pdf from the name so it isn't doubled (foo.pdf.pdf).
        stem = safe_filename(filename).removesuffix(".pdf")
        return File(data=data, name=stem, format="pdf")
    target_dir = _output_dir(output_dir)
    # filename embeds the caller-supplied identifier — sanitize so it can
    # never escape target_dir.
    target = target_dir / safe_filename(filename)
    target.write_bytes(data)
    return str(target.resolve())


def _ext_from_content_type(content_type: str | None) -> str | None:
    if not content_type:
        return None
    import mimetypes

    ext = mimetypes.guess_extension(content_type.split(";", 1)[0].strip())
    return ext.lstrip(".") if ext else None


def _file_result(
    data: bytes,
    output_dir: str | None,
    filename: str,
    content_type: str | None = None,
) -> str | File:
    """Return an arbitrary downloaded file — inline over HTTP, saved on stdio.

    Like :func:`_pdf_result` but for any media type (attachments may be PDF,
    JPG or PNG). A missing extension is filled in from the content type so the
    saved file / inline resource carries a sensible name.
    """
    name = safe_filename(filename)
    ext_from_ct = _ext_from_content_type(content_type)
    if "." not in name and ext_from_ct:
        name = f"{name}.{ext_from_ct}"
    # The name comes from the server's Content-Disposition — never let it
    # pick an executable/config extension on the local disk.
    if name.rpartition(".")[2].lower() not in ALLOWED_FILE_EXTENSIONS or "." not in name:
        name = f"{name}.bin"
    if _in_http_request():
        stem, _, ext = name.rpartition(".") if "." in name else (name, "", "")
        return File(data=data, name=stem or name, format=ext or ext_from_ct or "bin")
    target_dir = _output_dir(output_dir)
    target = target_dir / name
    target.write_bytes(data)
    return str(target.resolve())


# Upper bound for one uploaded attachment. Lexware enforces its own (smaller)
# limits; this only keeps a caller from making the server decode and hold
# arbitrarily large payloads in memory.
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
# Whole HTTP request body (JSON-RPC envelope around a base64 upload: 4/3 of
# MAX_UPLOAD_BYTES plus headroom).
MAX_HTTP_BODY_BYTES = 16 * 1024 * 1024


def _too_large(size: int) -> ValueError:
    return ValueError(
        f"Datei zu groß ({size / 1024 / 1024:.1f} MB) — höchstens "
        f"{MAX_UPLOAD_BYTES // 1024 // 1024} MB."
    )


class BodySizeLimit:
    """ASGI middleware rejecting request bodies above a byte limit (413).

    Checks Content-Length up front and counts streamed (chunked) bodies, so
    an oversized request never ends up fully in memory.
    """

    def __init__(self, app: Any, max_bytes: int = MAX_HTTP_BODY_BYTES) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        for name, value in scope.get("headers") or []:
            if name == b"content-length":
                try:
                    too_big = int(value) > self.max_bytes
                except ValueError:
                    too_big = True
                if too_big:
                    await self._reject(send)
                    return

        received = 0
        exceeded = False
        response_started = False

        async def limited_receive() -> Any:
            nonlocal received, exceeded
            if exceeded:
                return {"type": "http.disconnect"}
            message = await receive()
            if message.get("type") == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    # Pretend the client went away: the app stops reading
                    # without buffering the rest, and we answer 413 below.
                    exceeded = True
                    return {"type": "http.disconnect"}
            return message

        async def guarded_send(message: Any) -> None:
            nonlocal response_started
            if exceeded:
                return  # the 413 below is the only answer
            if message.get("type") == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, guarded_send)
        except Exception:
            if not exceeded:
                raise
        if exceeded and not response_started:
            await self._reject(send)

    @staticmethod
    async def _reject(send: Any) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [(b"content-type", b"text/plain; charset=utf-8")],
            }
        )
        await send({"type": "http.response.body", "body": b"Request body too large"})


def _decode_base64(data: str) -> bytes:
    """Decode base64 the way chat assistants actually send it.

    Typical shapes besides the clean form: a ``data:application/pdf;base64,``
    prefix, line breaks every 76 characters (output of ``base64``), missing
    ``=`` padding, or the URL-safe alphabet. All of these are unambiguous, so
    accept them instead of failing the whole booking on formatting.
    """
    import base64
    import binascii

    text = data.strip()
    if text.startswith("data:") and "," in text:
        text = text.split(",", 1)[1]
    text = "".join(text.split())
    if "-" in text or "_" in text:
        text = text.replace("-", "+").replace("_", "/")
    text += "=" * (-len(text) % 4)
    try:
        return base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(
            "file_base64 ist kein gültiges Base64 — bitte den Dateiinhalt "
            "vollständig und unverändert base64-kodiert senden."
        ) from exc


# A receipt image whose longest side is below this is a thumbnail, not a
# readable document (A4 at 100 dpi is 827×1169; phone photos are 3000+).
# Chat assistants that must squeeze a file into a tool call as base64 text
# shrink it to exactly such a thumbnail (seen: 85×120 px, 2 KB).
MIN_RECEIPT_IMAGE_EDGE = 800


class UnreadableFile(ValueError):
    """The supplied file is a shrunken stand-in, not the original receipt."""


def _check_not_thumbnail(content: bytes) -> None:
    size = services.image_size(content)
    if size and max(size) < MIN_RECEIPT_IMAGE_EDGE:
        width, height = size
        raise UnreadableFile(
            f"Das Bild ist nur {width}×{height} Pixel groß — ein verkleinertes "
            "Vorschaubild, auf dem der Beleg nicht lesbar ist. Die Originaldatei "
            "muss unverändert übertragen werden (Upload-Link)."
        )


_UPLOAD_HOWTO = (
    "Attach the ORIGINAL file unchanged — never re-encode, render or shrink it. "
    "If you can run code where the user's file is (e.g. /mnt/user-data/uploads/"
    "<name>), run: curl -sS -F \"file=@<path>\" \"<uploadUrl>\" and check the "
    "JSON for \"ok\": true. If that is not possible (no code environment, "
    "network blocked), give the user the uploadUrl as a clickable link: they "
    "open it and drop the file there. The link is valid for "
    "expiresInMinutes and only for this voucher. In ChatGPT, call "
    "attach_chat_file with this voucher's id and the user's file instead."
)


def _upload_links_available() -> bool:
    """True in an authenticated online (HTTP) call where links can be minted."""
    from lxw_cli.mcp_auth import request_api_key

    return (
        getattr(mcp.auth, "upload_links", None) is not None
        and request_api_key() is not None
    )


def _upload_link(voucher_id: str, label: str = "") -> dict[str, Any]:
    from lxw_cli.mcp_auth import request_api_key

    links = mcp.auth.upload_links  # type: ignore[union-attr]
    api_key = request_api_key()
    assert api_key is not None  # guarded by _upload_links_available()
    return {
        **links.create(api_key=api_key, voucher_id=voucher_id, label=label),
        "howTo": _UPLOAD_HOWTO,
    }


def _resolve_upload(
    file_path: str | None, file_base64: str | None, filename: str | None
) -> tuple[bytes, str]:
    """Turn the caller's upload inputs into (content_bytes, filename).

    Over stdio the file lives on the local disk (`file_path`); over HTTP the
    server can't see the caller's disk, so the bytes arrive as `file_base64`
    (with an explicit `filename`).
    """
    if file_base64:
        # Check before decoding: base64 is 4/3 of the decoded size.
        approx = len(file_base64) * 3 // 4
        if approx > MAX_UPLOAD_BYTES:
            raise _too_large(approx)
        content = _decode_base64(file_base64)
        if not content:
            raise ValueError("file_base64 ist leer — keine Datei zum Anhängen.")
        # base64 is typed out by the assistant — exactly where originals get
        # shrunk to thumbnails. A local file_path is the user's real file.
        _check_not_thumbnail(content)
        return content, safe_filename(filename or "beleg")
    if file_path:
        # Over HTTP `file_path` would name a file on the *server*: any caller
        # with a valid Lexware key could attach server files (secrets, config)
        # to a voucher in their own account and download them again.
        if _in_http_request():
            raise ValueError(
                "file_path ist über die Online-Verbindung nicht möglich — "
                "bitte die Datei über den Upload-Link (create_upload_link) "
                "anhängen."
            )
        source = Path(file_path).expanduser().resolve()
        ext = source.suffix.lstrip(".").lower()
        if ext not in ALLOWED_FILE_EXTENSIONS:
            raise ValueError(
                "Nur Belegdateien können hochgeladen werden ("
                + ", ".join(sorted(ALLOWED_FILE_EXTENSIONS))
                + f") — nicht {source.name}."
            )
        hidden = _hidden_component(source)
        if hidden:
            raise ValueError(
                f"Dateien aus versteckten Verzeichnissen ({hidden}) werden nicht hochgeladen."
            )
        if not source.is_file():
            raise ValueError(f"Datei nicht gefunden: {source}")
        size = source.stat().st_size
        if size > MAX_UPLOAD_BYTES:
            raise _too_large(size)
        return source.read_bytes(), safe_filename(filename or source.name)
    raise ValueError(
        "Bitte eine Datei angeben: file_path (lokale Datei) oder file_base64 "
        "zusammen mit filename."
    )


# ---------------------------------------------------------------------------
# Read tools
# ---------------------------------------------------------------------------


@mcp.tool
def version() -> dict[str, str]:
    """Report the running version of the Lexware assistant.

    Use this when the user asks which version is running / deployed. Answers
    live from the server, so it reflects the real deployed build even if a
    client UI shows a cached/older version. `stand` is the build timestamp.
    """
    import os

    info = {"produkt": "Lexware-Assistent", "version": __version__}
    # Ein per Deploy gesetzter Zeitstempel gewinnt (er beschreibt genau
    # diesen Rollout); sonst der ins Paket eingebackene Build-Zeitpunkt.
    build = os.getenv("LXW_MCP_BUILD", "").strip() or __build__
    if build:
        info["stand"] = build
    installation = _current_installation()
    if installation:
        info["installation"] = installation
    return info


def _current_installation() -> str | None:
    """Which Lexware installation this call talks to (label, if known)."""
    if _in_http_request():
        from lxw_cli.mcp_auth import request_installation

        return request_installation()
    return _profile


@mcp.tool
def service_status(include_planned: bool = True) -> dict[str, Any]:
    """Check whether Lexware Office itself is currently having problems.

    Use this when the user asks if Lexware is down, why something is slow or
    keeps failing, or whether a known problem exists — and after an action
    failed unexpectedly. Reads Lexware's public status page, so it also works
    when Lexware itself is unreachable.

    `include_planned`: also report announced future maintenance windows.

    Report the result in plain language. `zustand` is one of 'normal',
    'gestört', 'wartung' or 'unbekannt' ('unbekannt' only means the status
    page could not be reached — never present that as an outage).
    """
    report = status.get_status(include_planned=include_planned)
    if report is None:
        return {
            "zustand": "unbekannt",
            "zusammenfassung": (
                "Die Lexware-Statusseite ist gerade nicht erreichbar — über "
                "Störungen lässt sich daraus nichts ableiten."
            ),
            "statusseite": status.STATUS_BASE_URL,
        }

    zustand = {
        status.STATE_OPERATIONAL: "normal",
        status.STATE_DEGRADED: "gestört",
        status.STATE_MAINTENANCE: "wartung",
    }.get(report.state, report.state)

    def _notice(notice: status.Notice) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "titel": notice.subject,
            "art": "Wartung" if notice.is_maintenance else "Störung",
            "beschreibung": status.describe_notice(notice),
            "link": notice.url,
        }
        if notice.update:
            entry["letztes_update"] = notice.update
        return entry

    result: dict[str, Any] = {
        "zustand": zustand,
        "zusammenfassung": status.summary(report),
        "statusseite": report.url,
    }
    if report.current:
        result["aktuelle_meldungen"] = [_notice(n) for n in report.current]
    if report.planned:
        result["geplante_wartungen"] = [_notice(n) for n in report.planned]
    return result


@mcp.tool
def profile() -> dict[str, Any]:
    """Return the user's Lexware Office company profile. Useful as an auth test."""
    return services.get_profile(_client_get())


@mcp.tool
def list_invoices(
    limit: int = 25,
    voucher_status: str | None = None,
    voucher_number: str | None = None,
    contact_id: str | None = None,
    include_archived: bool = False,
    date_from: str | None = None,
    date_to: str | None = None,
) -> list[dict[str, Any]]:
    """List invoices (salesinvoice/invoice/downpaymentinvoice).

    Parameters are exactly: limit, voucher_status, voucher_number, contact_id,
    include_archived (there is no `status`, `number`, `customer` or `query`).
    voucher_status: comma-separated, e.g. 'open,paid' (default: all, overdue
        ones included). German or English words work (offen/open, bezahlt/paid, überfällig/overdue,
        Entwurf/draft, storniert/voided, angenommen/accepted, abgelehnt/rejected).
    voucher_number: invoice number; also found without prefix ('6' → RE0006).
    contact_id: the customer's id — a customer number or exact name also works.
    include_archived: archived invoices are excluded by default; set True to
        include them (filtered server-side).
    limit: max results to return; set to 0 to fetch all (paginates internally).
    date_from / date_to: Belegdatum von/bis (e.g. '01.10.2026', '2026-10-31'; a
        month like 'November 2026' or '2026-11' = its first resp. last day).
    """
    return services.list_invoices(
        _client_get(),
        status=voucher_status,
        number=voucher_number,
        contact_id=contact_id,
        include_archived=include_archived,
        limit=limit,
        date_from=date_from,
        date_to=date_to,
    ).items


@mcp.tool
def get_invoice(identifier: str) -> dict[str, Any]:
    """Get invoice details by UUID or invoice number (e.g. 'FB2600682').

    Works for every entry of the invoice list, including down payment
    invoices (Abschlagsrechnungen).

    The only parameter is `identifier` (invoice number or id) — there is no
    `invoice_id`, `id` or `number` parameter.
    """
    return services.get_invoice(_client_get(), identifier)


@mcp.tool
def get_payment_status(identifier: str) -> dict[str, Any]:
    """Show whether an invoice is paid and how much is still open.

    Use this to answer "is invoice X paid?" / "what's still open on X?".
    Accepts a voucher number (e.g. 'FB2600682', also just its digits or with
    the type word, 'Rechnung 1') or id. A dunning has no payment status of its
    own — the result points to its invoice.
    Works for invoices, down payment invoices, bookkeeping vouchers and
    Rechnungskorrekturen (credit notes). Returns the still-open amount, the
    payment state (open / paid off / balanced / voided), the date it was paid
    off and the individual recorded payments; `hinweis` explains special cases
    (e.g. a credit note offset against its invoice). Quotations, order
    confirmations and delivery notes have no payment status.
    """
    return services.get_payment_status(_client_get(), identifier)


@mcp.tool
def download_invoice_pdf(identifier: str, output_dir: str | None = None) -> str | File:
    """Download an invoice PDF — also of a down payment invoice or a
    Rechnungskorrektur (credit note). Accepts UUID or number.

    Over stdio the PDF is saved to ~/Downloads/lexware/ (or output_dir) and the
    absolute path is returned. Over HTTP the PDF is returned inline as binary
    content and output_dir is ignored (the server's disk isn't the caller's).
    Note: drafts cannot be downloaded — the API requires a finalized status.
    """
    data, stem = services.download_pdf(_client_get(), identifier, services.INVOICE_PDF_TYPES)
    return _pdf_result(data, output_dir, f"{stem}.pdf")


@mcp.tool
def list_contacts(
    limit: int = 25,
    name: str | None = None,
    email: str | None = None,
    number: str | None = None,
    customer: bool = False,
    vendor: bool = False,
    include_archived: bool = False,
) -> list[dict[str, Any]]:
    """List contacts (customers/vendors).

    name: part of the company/person name; contact persons, e-mails and other
    spellings (ä = ae) are searched too. number: customer or vendor number
    ('K-10021' works). customer/vendor: both set = contacts with BOTH roles.
    Archived contacts are excluded by default; set include_archived=True to keep
    them (the API has no server-side archived filter, so this filters
    client-side). Set limit=0 to fetch all contacts (paginates internally).
    """
    return services.list_contacts(
        _client_get(),
        name=name,
        email=email,
        number=number,
        customer=customer,
        vendor=vendor,
        include_archived=include_archived,
        limit=limit,
    ).items


@mcp.tool
def get_contact(contact_id: str) -> dict[str, Any]:
    """Get a contact's details by id — a customer/vendor number or the exact
    name works too (ambiguous names list the candidates)."""
    return services.get_contact(_client_get(), contact_id)


@mcp.tool
def list_vouchers(
    limit: int = 25,
    voucher_type: str | None = None,
    voucher_status: str | None = None,
    voucher_number: str | None = None,
    contact_id: str | None = None,
    include_archived: bool = False,
    date_from: str | None = None,
    date_to: str | None = None,
) -> list[dict[str, Any]]:
    """List vouchers (all types — invoices, credit notes, purchase invoices etc.).

    voucher_type: comma-separated, e.g. 'salesinvoice,purchaseinvoice'.
        Valid values only: salesinvoice, salescreditnote, purchaseinvoice,
        purchasecreditnote, invoice, downpaymentinvoice, creditnote,
        orderconfirmation, quotation, deliverynote, any. German words work too
        (Rechnung, Gutschrift, Angebot, Auftrag, Lieferschein, Eingangsrechnung).
        Default: ALL types.
    voucher_status: comma-separated, e.g. 'open,paid' (default: all, incl. 'zu
        prüfen'/unchecked). German or English words work (offen/open, bezahlt/paid,
        überfällig/overdue, Entwurf/draft, storniert/voided, angenommen/accepted,
        abgelehnt/rejected, zu prüfen/unchecked).
    voucher_number: finds the voucher whatever its type ('6' → RE0006).
    Archived vouchers are excluded by default; set include_archived=True to
    include them. Set limit=0 to fetch all (paginates internally).
    date_from / date_to: Belegdatum von/bis (e.g. '01.10.2026', '2026-10-31'; a
        month like 'November 2026' or '2026-11' = its first resp. last day).
    """
    return services.list_vouchers(
        _client_get(),
        voucher_type=voucher_type,
        status=voucher_status,
        number=voucher_number,
        contact_id=contact_id,
        include_archived=include_archived,
        limit=limit,
        date_from=date_from,
        date_to=date_to,
    ).items


@mcp.tool
def get_voucher(identifier: str) -> dict[str, Any]:
    """Get a voucher's details by UUID or voucher number. Searches all voucher types
    (bookkeeping vouchers, invoices, down payment invoices, credit notes,
    quotations, order confirmations, delivery notes; a dunning number like
    'RE0003_1' is recognised too). A type word narrows the search: 'Rechnung 1'
    = RE0001, 'Angebot 109', 'Eingangsrechnung 4711' / 'ER 4711'.

    The only parameter is `identifier` (voucher number or id) — there is no
    `voucher_id`, `id` or `number` parameter.
    """
    return services.get_voucher(_client_get(), identifier)


@mcp.tool
def list_posting_categories(
    category_type: str | None = None, search: str | None = None
) -> list[dict[str, Any]]:
    """List the bookkeeping categories available in this account.

    These are the categories a bookkeeping voucher is assigned to (e.g.
    revenue/income vs. expense categories). Use this to pick the right category
    when recording a receipt or purchase, and to answer "which categories can I
    book to?". `category_type` optionally narrows the list: 'income'
    (Einnahmen) or 'outgo' (Ausgaben; 'expense' works too). Returns each
    category's id, name and type. A voucher may also name its category instead
    of the id — it is looked up here. `search` filters by part of the name or
    group (e.g. 'Kraftstoff', 'Büro', 'Miete').
    """
    return services.list_posting_categories(
        _client_get(), category_type=category_type, search=search
    )


@mcp.tool
def list_articles(
    limit: int = 25,
    search: str | None = None,
    article_type: str | None = None,
    article_number: str | None = None,
    gtin: str | None = None,
) -> list[dict[str, Any]]:
    """List or search articles (products and services).

    Parameters are exactly: limit, search, article_type, article_number, gtin
    (to search by name use `search` — there is no `name`, `query` or `title`).
    search: free-text, case-insensitive substring match over the article's
        title, description and article number — use this to find an article by
        name/Bezeichnung or a partial number (the API itself only filters by
        exact article_number/gtin). Done client-side over all articles.
    article_type: 'product' or 'service' (any case).
    article_number: exact article number (server-side, fast).
    Set limit=0 to fetch all articles (paginates internally).
    """
    return services.list_articles(
        _client_get(),
        search=search,
        article_type=article_type,
        article_number=article_number,
        gtin=gtin,
        limit=limit,
    ).items


@mcp.tool
def get_article(
    identifier: str | None = None, article_id: str | None = None
) -> dict[str, Any]:
    """Get an article's details by article number or UUID.

    Pass `identifier`: the article number (e.g. '10042') or the article id —
    same convention as the document tools. `article_id` is the older name of
    the same parameter and still accepted. To find an article by its name,
    use the article list with `search` instead.
    """
    value = (identifier or article_id or "").strip()
    if not value:
        raise ToolError(
            "Bitte `identifier` angeben: die Artikelnummer oder die ID des Artikels."
        )
    return services.get_article(_client_get(), value)


@mcp.tool
def list_quotations(
    limit: int = 25,
    voucher_status: str | None = None,
    voucher_number: str | None = None,
    contact_id: str | None = None,
    include_archived: bool = False,
    date_from: str | None = None,
    date_to: str | None = None,
) -> list[dict[str, Any]]:
    """List quotations (Angebote).

    voucher_status: comma-separated filter, e.g. 'open,accepted'.
        German or English words work (offen/open, bezahlt/paid,
        überfällig/overdue, Entwurf/draft, storniert/voided,
        angenommen/accepted, abgelehnt/rejected).
    voucher_number: filter by exact quotation number.
    contact_id: filter by customer UUID.
    Archived quotations are excluded by default; set include_archived=True to
    include them. Set limit=0 to fetch all quotations (paginates internally).
    date_from / date_to: Belegdatum von/bis (e.g. '01.10.2026', '2026-10-31'; a
        month like 'November 2026' or '2026-11' = its first resp. last day).
    """
    return services.list_quotations(
        _client_get(),
        status=voucher_status,
        number=voucher_number,
        contact_id=contact_id,
        include_archived=include_archived,
        limit=limit,
        date_from=date_from,
        date_to=date_to,
    ).items


@mcp.tool
def get_quotation(identifier: str) -> dict[str, Any]:
    """Get a quotation's details by UUID or quotation number (e.g. 'AG5241415').

    The only parameter is `identifier` (quotation number or id) — there is no
    `quotation_id`, `id` or `number` parameter.
    """
    return services.get_quotation(_client_get(), identifier)


@mcp.tool
def download_quotation_pdf(identifier: str, output_dir: str | None = None) -> str | File:
    """Download a quotation PDF.

    stdio: saves to disk and returns the path. HTTP: returns the PDF inline.
    """
    data, stem = services.download_pdf(_client_get(), identifier, "quotation")
    return _pdf_result(data, output_dir, f"{stem}.pdf")


@mcp.tool
def list_order_confirmations(
    limit: int = 25,
    voucher_status: str | None = None,
    voucher_number: str | None = None,
    contact_id: str | None = None,
    include_archived: bool = False,
    date_from: str | None = None,
    date_to: str | None = None,
) -> list[dict[str, Any]]:
    """List order confirmations (Aufträge).

    voucher_status: comma-separated filter, e.g. 'draft,open'.
        German or English words work (offen/open, bezahlt/paid,
        überfällig/overdue, Entwurf/draft, storniert/voided,
        angenommen/accepted, abgelehnt/rejected).
    voucher_number: filter by exact order number (e.g. 'AB6246648').
    contact_id: filter by customer UUID.
    Archived orders are excluded by default; set include_archived=True to
    include them. Set limit=0 to fetch all (paginates internally).
    date_from / date_to: Belegdatum von/bis (e.g. '01.10.2026', '2026-10-31'; a
        month like 'November 2026' or '2026-11' = its first resp. last day).
    """
    return services.list_order_confirmations(
        _client_get(),
        status=voucher_status,
        number=voucher_number,
        contact_id=contact_id,
        include_archived=include_archived,
        limit=limit,
        date_from=date_from,
        date_to=date_to,
    ).items


@mcp.tool
def get_order_confirmation(identifier: str) -> dict[str, Any]:
    """Get an order confirmation's details by UUID or order number (e.g. 'AB6246648').

    The only parameter is `identifier` (order number or id) — there is no
    `order_id`, `id` or `number` parameter.
    """
    return services.get_order_confirmation(_client_get(), identifier)


@mcp.tool
def download_order_confirmation_pdf(
    identifier: str, output_dir: str | None = None
) -> str | File:
    """Download an order confirmation PDF by order number or id (`identifier`).

    stdio: saves to disk and returns the path. HTTP: returns the PDF inline.
    Drafts have no PDF yet — they must be finalized in Lexware first.
    """
    data, stem = services.download_pdf(_client_get(), identifier, "orderconfirmation")
    return _pdf_result(data, output_dir, f"{stem}.pdf")


@mcp.tool
def list_delivery_notes(
    limit: int = 25,
    voucher_status: str | None = None,
    voucher_number: str | None = None,
    contact_id: str | None = None,
    include_archived: bool = False,
    date_from: str | None = None,
    date_to: str | None = None,
) -> list[dict[str, Any]]:
    """List delivery notes (Lieferscheine).

    voucher_status: comma-separated filter.
        German or English words work (offen/open, bezahlt/paid,
        überfällig/overdue, Entwurf/draft, storniert/voided,
        angenommen/accepted, abgelehnt/rejected).
    voucher_number: filter by exact delivery note number.
    contact_id: filter by customer UUID.
    Archived delivery notes are excluded by default; set include_archived=True
    to include them. Set limit=0 to fetch all (paginates internally).
    date_from / date_to: Belegdatum von/bis (e.g. '01.10.2026', '2026-10-31'; a
        month like 'November 2026' or '2026-11' = its first resp. last day).
    """
    return services.list_delivery_notes(
        _client_get(),
        status=voucher_status,
        number=voucher_number,
        contact_id=contact_id,
        include_archived=include_archived,
        limit=limit,
        date_from=date_from,
        date_to=date_to,
    ).items


@mcp.tool
def get_delivery_note(identifier: str) -> dict[str, Any]:
    """Get a delivery note's details by UUID or delivery note number.

    The only parameter is `identifier` (delivery note number or id).
    """
    return services.get_delivery_note(_client_get(), identifier)


@mcp.tool
def download_delivery_note_pdf(identifier: str, output_dir: str | None = None) -> str | File:
    """Download a delivery note PDF.

    stdio: saves to disk and returns the path. HTTP: returns the PDF inline.
    """
    data, stem = services.download_pdf(_client_get(), identifier, "deliverynote")
    return _pdf_result(data, output_dir, f"{stem}.pdf")


@mcp.tool
def get_dunning(dunning_id: str) -> dict[str, Any]:
    """Get a dunning (payment reminder) by its id.

    `dunning_id`: the dunning's id, its number (e.g. 'RE0001_1') or the
    number/id of the invoice — then its latest dunning is returned.
    """
    return services.get_dunning(_client_get(), dunning_id)


@mcp.tool
def download_dunning_pdf(dunning_id: str, output_dir: str | None = None) -> str | File:
    """Download a dunning's PDF by its id.

    stdio: saves to disk and returns the path. HTTP: returns the PDF inline.
    Drafts get a preview PDF. Instead of the id, the dunning's number
    (e.g. 'RE0001_1') or the invoice number works too.
    """
    data = services.download_dunning_pdf(_client_get(), dunning_id)
    return _pdf_result(data, output_dir, f"dunning-{dunning_id}.pdf")


# ---------------------------------------------------------------------------
# Write tools — master data is created directly; documents are created as
# drafts (never finalized).
# ---------------------------------------------------------------------------


@mcp.tool
def create_invoice_draft(body: dict[str, Any]) -> dict[str, Any]:
    """Create an invoice as draft. See https://developers.lexware.io/docs/ for body schema.

    Line items support an optional `description` (long text shown under the
    position name on the PDF). When a line item is based on an article, copy
    the article's `description` into it.

    Längenlimits (sonst lehnt Lexware ab): Belegtitel `title` max. 25 Zeichen;
    Einleitungstext `introduction` und Schlusstext `remark` je max. 2000;
    Positionsbezeichnung `name` max. 100; Positionsbeschreibung `description`
    max. 2000. Bei Überschreitung bitte kürzen (der Server meldet es sonst).

    Defaults when omitted: `voucherDate` today (plain dates like 2026-11-04
    are fine), `totalPrice` EUR, `taxConditions.taxType` net, line item
    `taxRatePercentage` 19 (0 for tax-free types); quotations expire after 30
    days. Line item type `custom` = free position with name and unitPrice;
    `material`/`service` only with the article's `id`.
    Free texts work: zahlungsziel "14 Tage 2 % Skonto, 30 Tage netto",
    steuerart "innergemeinschaftliche Lieferung"/"Ausfuhr"/"Kleinunternehmer",
    steuersatz "ermäßigt" (7) / "voll" (19) / "19% MwSt". A quantity may carry
    its unit ("5 Rollen", "2 Std"); a change ("+5%", "10 % mehr") or a unit that
    contradicts the position's ("30 min" on hours) is refused, not guessed. An
    own payment text is kept as written; only standard texts are rebuilt.
    """
    return services.create_invoice(_client_get(), body)


@mcp.tool
def create_contact(body: dict[str, Any]) -> dict[str, Any]:
    """Create a new contact (master data — not a draft).

    Minimal body example:
    {"roles": {"customer": {}}, "company": {"name": "Acme GmbH"}}
    Flat fields work too: {"name": "Acme GmbH", "adresse": "Ring 1, 50667 Köln",
    "email": …, "ansprechpartner": {"vorname": …, "nachname": …}}. For a private
    person give `typ: "privat"` ("firma" for a company) — a name that looks like
    a person is asked back, because the kind cannot be changed later. A contact
    with the same name (ä = ae) is NOT created twice; set `"allow_duplicate":
    true` in the body if a second one is really wanted.
    Längenlimits: Notiz `note` max. 1000 Zeichen; Anrede `salutation` max. 25.
    """
    return services.create_contact(_client_get(), body)


@mcp.tool
def update_contact(contact_id: str, changes: dict[str, Any]) -> dict[str, Any]:
    """Update an existing contact by UUID — pass only the fields to change.

    The server fetches the current contact, deep-merges `changes` onto it,
    carries the current `version` automatically (Lexware optimistic locking),
    and saves — so you never fetch/version-juggle yourself. Nested objects
    merge key-by-key; lists are REPLACED wholesale, so to change one entry of
    a list send the full list.
    Examples:
      rename company:      {"company": {"name": "Neuer Name GmbH"}}
      change business mail:{"emailAddresses": {"business": ["neu@example.com"]}}
      change billing addr: {"addresses": {"billing": [{"street": "Weg 1",
                            "zip": "50667", "city": "Köln", "countryCode": "DE"}]}}
      part of an address:  {"lieferadresse": {"city": "Leverkusen", "zip": "51373"}}
                           (billing and shipping keep their other fields)
      contact person:      {"ansprechpartner": {"telefon": "…"}} merges into the
                           existing one; another name REPLACES it (Lexware keeps
                           only one — the result's `hinweis` says so).
    Note: `archived` is read-only in the Lexware API — a request to archive or
    un-archive a contact is refused with a clear message (only possible in
    the Lexware web app); nothing is changed then.
    Längenlimits: Notiz `note` max. 1000 Zeichen; Anrede `salutation` max. 25.
    """
    return services.update_contact(_client_get(), contact_id, changes)


@mcp.tool
def create_voucher_draft(
    body: dict[str, Any],
    file_path: str | None = None,
    file_base64: str | None = None,
    filename: str | None = None,
) -> dict[str, Any]:
    """Create a bookkeeping voucher (e.g. a purchase invoice / receipt) and
    attach the original document (PDF/JPG/PNG).

    Note: Lexware books it right away with status 'open' (there is no draft
    state for bookkeeping vouchers; 'paid' cannot be set — payments are matched
    in Lexware). `type` may be German (Eingangsrechnung, Quittung,
    Ausgangsrechnung, Gutschrift — with a supplier or an expense category a
    supplier's credit note); `categoryId` may be the category's name (or set
    once for all items); `netto`/`brutto` per item set taxType;
    dates as TT.MM.JJJJ work; without contactId the collective contact is
    used. The same number with the same contact and amount is not booked twice
    — the existing voucher is returned with a `hinweis`.

    Lexware works in two steps — the voucher is created first, and only its
    new id can receive the file. NEVER put the file or its content into
    `body` (no `files`, image or base64 fields there).

    Attaching the receipt the user gave you in the chat:
    - Online connection: call this WITHOUT file arguments. The result's
      `fileUpload` holds an `uploadUrl`; follow its `howTo` so the ORIGINAL
      file reaches Lexware unchanged. Do NOT base64-encode, render, convert
      or shrink the file yourself — that produces an unreadable thumbnail
      (tiny images are rejected).
    - On the user's machine: pass `file_path` (or `file_base64` + `filename`
      for a small file you have byte-exact) and it is attached right after
      creation.

    Each item's `taxAmount` and the totals are calculated from `amount`,
    `taxRatePercent` and `taxType` (gross: amounts incl. tax, net: excl.).

    Result: the created voucher (`id`, `version`, …) plus `fileUpload`
    (`ok: true` when attached; otherwise an `uploadUrl`/`error`). The voucher
    DOES exist even if attaching failed — never create it again.
    """
    files = body.get("files") if isinstance(body, dict) else None
    if files and not all(isinstance(f, str) and services.looks_like_uuid(f) for f in files):
        raise ValueError(
            "`files` im Beleg darf nur ids bereits hochgeladener Dateien enthalten. "
            "Die Belegdatei selbst nicht in den Beleg packen — sie wird nach dem "
            "Anlegen angehängt (Upload-Link bzw. file_base64/file_path)."
        )
    links = _upload_links_available()
    upload = None
    unreadable: UnreadableFile | None = None
    if file_path or file_base64:
        # Check the file before booking, so a bad file never leaves a
        # half-done voucher behind.
        try:
            upload = _resolve_upload(file_path, file_base64, filename)
        except UnreadableFile as exc:
            # Online, the voucher can still be booked and the original
            # attached via the upload link; locally there is no such way.
            if not links:
                raise
            unreadable = exc
    client = _client_get()
    label = str(body.get("voucherNumber") or "") if isinstance(body, dict) else ""
    if upload is None:
        created = services.create_voucher(client, body)
        voucher_id = created.get("id") if isinstance(created, dict) else None
        if not (links and voucher_id):
            return created
        attach: dict[str, Any] = {"attached": False}
        if unreadable is not None:
            attach["error"] = str(unreadable)
        return {**created, "fileUpload": {**attach, **_upload_link(voucher_id, label)}}
    content, name = upload
    result = services.create_voucher_with_file(client, body, filename=name, content=content)
    upload_info = result.get("fileUpload") or {}
    if links and result.get("id") and not upload_info.get("ok"):
        upload_info.update(_upload_link(result["id"], label))
    return result


@mcp.tool
def upload_voucher_file(
    identifier: str,
    file_path: str | None = None,
    file_base64: str | None = None,
    filename: str | None = None,
) -> dict[str, Any]:
    """Attach a file to an EXISTING voucher from bytes you already hold exactly.

    Online, to attach a receipt the user gave you in the chat, use
    create_upload_link instead — it transfers the original file unchanged.
    This tool is for a local `file_path` (user's machine only) or a small
    file you have byte-exact as `file_base64` + `filename`. Never render,
    convert or shrink a file to fit it in here; tiny images are rejected.
    `identifier` is the voucher's id or number. Returns the stored file's id.
    """
    try:
        content, name = _resolve_upload(file_path, file_base64, filename)
    except UnreadableFile as exc:
        if _upload_links_available():
            raise ValueError(f"{exc} Bitte create_upload_link verwenden.") from exc
        raise
    return services.upload_voucher_file(
        _client_get(), identifier, filename=name, content=content
    )


@mcp.tool
def create_upload_link(identifier: str) -> dict[str, Any]:
    """Get a short-lived link that attaches the ORIGINAL receipt file to an
    existing voucher (online connection only).

    `identifier` is the voucher's id or number. Follow the returned `howTo`:
    upload the user's file unchanged from your code environment with curl, or
    give the user the link to open and drop the file there. The link works
    for 30 minutes and only for this one voucher.
    """
    if not _upload_links_available():
        raise ValueError(
            "Upload-Links gibt es nur über die Online-Verbindung — lokal die "
            "Datei bitte mit file_path anhängen."
        )
    voucher = services.get_bookkeeping_voucher(_client_get(), identifier)
    return {
        "voucherId": voucher["id"],
        "voucherNumber": voucher.get("voucherNumber"),
        **_upload_link(voucher["id"], str(voucher.get("voucherNumber") or "")),
    }


class ChatFile(TypedDict):
    """A file the chat app hands to a tool (ChatGPT ``openai/fileParams``)."""

    download_url: str
    file_id: str
    mime_type: NotRequired[str]
    file_name: NotRequired[str]


# Hosts ChatGPT serves attached files from. Nothing else is fetched: the
# server must not turn into a proxy for arbitrary URLs (SSRF). EU workspaces
# get their files from OpenAI's Azure storage account — that one account is
# listed by its full name, never the whole `blob.core.windows.net` (anyone
# can host files there).
DEFAULT_CHAT_FILE_HOSTS = (
    "oaiusercontent.com",
    "openai.com",
    "chatgpt.com",
    "oaisdmntprnortheu.blob.core.windows.net",
)
ENV_CHAT_FILE_HOSTS = "LXW_MCP_CHAT_FILE_HOSTS"
CHAT_FILE_TIMEOUT = 30.0
CHAT_FILE_MAX_REDIRECTS = 3


def _chat_file_hosts() -> tuple[str, ...]:
    raw = os.environ.get(ENV_CHAT_FILE_HOSTS, "")
    hosts = tuple(h.strip().lower().lstrip(".") for h in raw.split(",") if h.strip())
    return hosts or DEFAULT_CHAT_FILE_HOSTS


def _check_chat_file_url(url: str) -> httpx.URL:
    try:
        parsed = httpx.URL(url)
    except (httpx.InvalidURL, TypeError) as exc:
        raise ValueError("Ungültige Datei-Adresse vom Chat.") from exc
    host = (parsed.host or "").lower()
    allowed = any(host == h or host.endswith("." + h) for h in _chat_file_hosts())
    if parsed.scheme != "https" or parsed.port not in (None, 443) or not allowed:
        # Host only — the query string of a signed URL is a credential.
        log.warning("Chat-Datei abgelehnt: Host %r nicht erlaubt", host)
        raise ValueError(
            f"Die Datei-Adresse ({host or 'ohne Host'}) gehört nicht zum Chat-"
            "Dienst — nur vom Chat bereitgestellte Dateien werden geladen. "
            "Sonst bitte den Upload-Link (create_upload_link) verwenden."
        )
    return parsed


def _fetch_chat_file(url: str) -> bytes:
    """Download a chat attachment from the chat app's own file host.

    Redirects are followed by hand so every hop is checked against the host
    allowlist; no credentials of ours are sent, the size is capped while
    streaming.
    """
    target = _check_chat_file_url(url)
    with httpx.Client(timeout=CHAT_FILE_TIMEOUT, follow_redirects=False) as http:
        for _ in range(CHAT_FILE_MAX_REDIRECTS + 1):
            with http.stream("GET", target) as response:
                if response.is_redirect:
                    location = response.headers.get("location", "")
                    target = _check_chat_file_url(str(target.join(location)))
                    continue
                if response.status_code != 200:
                    raise ValueError(
                        "Die Datei konnte beim Chat nicht abgeholt werden "
                        f"(HTTP {response.status_code}). Bitte den Upload-Link "
                        "(create_upload_link) verwenden."
                    )
                data = bytearray()
                for chunk in response.iter_bytes():
                    data += chunk
                    if len(data) > MAX_UPLOAD_BYTES:
                        raise _too_large(len(data))
                return bytes(data)
    raise ValueError("Zu viele Weiterleitungen beim Abholen der Datei.")


@mcp.tool(meta={"openai/fileParams": ["file"]})
def attach_chat_file(identifier: str, file: ChatFile) -> dict[str, Any]:
    """ChatGPT: attach the file the user put into the chat to an EXISTING
    voucher — the original, unchanged.

    Pass the user's attached file as `file` (ChatGPT fills in download_url
    and file_id); never base64-encode, render or shrink it yourself.
    `identifier` is the voucher's id or number, e.g. the `id` returned by
    create_voucher_draft. Other chat apps: use the upload link instead.
    """
    from lxw_cli.mcp_upload import ACCEPTED_TYPES

    url = file.get("download_url") if isinstance(file, dict) else None
    if not url:
        raise ValueError("Keine Datei vom Chat erhalten (download_url fehlt).")
    content = _fetch_chat_file(url)
    if not content:
        raise ValueError("Die Datei vom Chat ist leer.")
    name = safe_filename(file.get("file_name") or "beleg")
    name, ctype = services.upload_name_and_type(name, content, None)
    if ctype not in ACCEPTED_TYPES:
        raise ValueError(
            f"{name}: Nur PDF, JPG, PNG oder XML können an einen Beleg gehängt werden."
        )
    stored = services.upload_voucher_file(
        _client_get(), identifier, filename=name, content=content, content_type=ctype
    )
    return {"ok": True, "filename": name, "bytes": len(content), "file": stored}


@mcp.tool
def download_voucher_file(file_id: str, output_dir: str | None = None) -> str | File:
    """Download a stored file (e.g. a voucher's attached receipt) by its file id.

    File ids appear in a voucher's `files` (or `files.documentFileId`). A
    voucher number or voucher id works too: then that voucher's file/PDF. Over stdio the file
    is saved to ~/Downloads/lexware/ (or output_dir) and the path is returned;
    over HTTP the file is returned inline and output_dir is ignored.
    """
    data, content_type, name = services.download_file(_client_get(), file_id)
    return _file_result(data, output_dir, name or f"beleg-{file_id}", content_type)


@mcp.tool
def create_article(body: dict[str, Any]) -> dict[str, Any]:
    """Create a new article (product or service — master data, not a draft).

    Längenlimits: Bezeichnung `title` max. 100 Zeichen; Beschreibung
    `description` max. 2000 (einfache Formatierung wie **fett**, __kursiv__ und
    `- Listen` ist möglich und zählt zu den 2000 Zeichen mit).
    """
    return services.create_article(_client_get(), body)


@mcp.tool
def update_article(article_id: str, changes: dict[str, Any]) -> dict[str, Any]:
    """Update an existing article by UUID or article number — pass only the
    fields to change. Returns what Lexware actually stored (with `hinweis`
    when nothing changed). Changing only netPrice makes the net price leading.

    Same partial-merge + automatic `version` handling as update_contact.
    Nested objects merge key-by-key; lists are replaced wholesale.
    Examples:
      change title: {"title": "Neuer Titel"}
      change price: {"price": {"netPrice": 19.99, "taxRate": 19}}
    Längenlimits: Bezeichnung `title` max. 100 Zeichen; Beschreibung
    `description` max. 2000 (einfache Formatierung möglich).
    """
    return services.update_article(_client_get(), article_id, changes)


@mcp.tool
def request_feature(description: str, contact_email: str | None = None) -> dict[str, str]:
    """Compose a NON-BINDING feature request for the vendor (oemedia) to copy & send.

    Use this ONLY when the user wants functionality that the tools here do not
    provide. This is an end-customer product — you do not build features
    yourself. The tool does NOT send anything: it returns a ready-to-send
    message (`subject`, `body`) and the vendor address (`to`). Present that to
    the user so they can email it themselves; it is non-binding and makes no
    promise about if or when the feature is built. Summarize the wish clearly in
    `description` (German is fine); `contact_email` is optional.
    """
    company: str | None = None
    try:
        profile = services.get_profile(_client_get())
        company = profile.get("companyName") or profile.get("organizationId")
    except Exception:  # noqa: BLE001 — composing the request is still useful
        company = None
    return compose_feature_request(
        description=description, company=company, contact_email=contact_email
    )


@mcp.tool
def create_quotation_draft(body: dict[str, Any]) -> dict[str, Any]:
    """Create a quotation as draft.

    Line items support an optional `description` (long text shown under the
    position name on the PDF). When a line item is based on an article, copy
    the article's `description` into it.

    Längenlimits (sonst lehnt Lexware ab): Belegtitel `title` max. 25 Zeichen;
    Einleitungstext `introduction` und Schlusstext `remark` je max. 2000;
    Positionsbezeichnung `name` max. 100; Positionsbeschreibung `description`
    max. 2000. Bei Überschreitung bitte kürzen (der Server meldet es sonst).

    Defaults when omitted: `voucherDate` today (plain dates like 2026-11-04
    are fine), `totalPrice` EUR, `taxConditions.taxType` net, line item
    `taxRatePercentage` 19 (0 for tax-free types); quotations expire after 30
    days. Line item type `custom` = free position with name and unitPrice;
    `material`/`service` only with the article's `id`.
    Free texts work: zahlungsziel "14 Tage 2 % Skonto, 30 Tage netto",
    steuerart "innergemeinschaftliche Lieferung"/"Ausfuhr"/"Kleinunternehmer",
    steuersatz "ermäßigt" (7) / "voll" (19) / "19% MwSt". A quantity may carry
    its unit ("5 Rollen", "2 Std"); a change ("+5%", "10 % mehr") or a unit that
    contradicts the position's ("30 min" on hours) is refused, not guessed. An
    own payment text is kept as written; only standard texts are rebuilt.
    """
    return services.create_quotation(_client_get(), body)


@mcp.tool
def create_order_confirmation_draft(body: dict[str, Any]) -> dict[str, Any]:
    """Create an order confirmation (Auftrag) as draft.

    Line items support an optional `description` (long text shown under the
    position name on the PDF). When a line item is based on an article, copy
    the article's `description` into it.

    Längenlimits (sonst lehnt Lexware ab): Belegtitel `title` max. 25 Zeichen;
    Einleitungstext `introduction` und Schlusstext `remark` je max. 2000;
    Positionsbezeichnung `name` max. 100; Positionsbeschreibung `description`
    max. 2000. Bei Überschreitung bitte kürzen (der Server meldet es sonst).

    Defaults when omitted: `voucherDate` today (plain dates like 2026-11-04
    are fine), `totalPrice` EUR, `taxConditions.taxType` net, line item
    `taxRatePercentage` 19 (0 for tax-free types); quotations expire after 30
    days. Line item type `custom` = free position with name and unitPrice;
    `material`/`service` only with the article's `id`.
    Free texts work: zahlungsziel "14 Tage 2 % Skonto, 30 Tage netto",
    steuerart "innergemeinschaftliche Lieferung"/"Ausfuhr"/"Kleinunternehmer",
    steuersatz "ermäßigt" (7) / "voll" (19) / "19% MwSt". A quantity may carry
    its unit ("5 Rollen", "2 Std"); a change ("+5%", "10 % mehr") or a unit that
    contradicts the position's ("30 min" on hours) is refused, not guessed. An
    own payment text is kept as written; only standard texts are rebuilt.
    """
    return services.create_order_confirmation(_client_get(), body)


@mcp.tool
def continue_document(
    identifier: str,
    target: str,
    changes: dict[str, Any] | None = None,
    allow_duplicate: bool = False,
) -> dict[str, Any]:
    """Continue a document into its follow-up, keeping them linked (Belegkette).

    Use when the user wants to carry a document forward to the next step. The
    customer (incl. contact person), positions (incl. article link, description,
    discounts), overall discount, payment and delivery terms are taken over;
    title, introduction and closing text are the standard texts of the new
    type (as in the Lexware web app). The follow-up is created as a draft.

    `identifier`: the source document's number or id.
    `target`: Auftrag/AB, Lieferschein/LS, Rechnung/RE, Rechnungskorrektur/
        Gutschrift/Storno, Mahnung. Paths (same as in Lexware): quotation →
        order/delivery note/invoice; order → delivery note/invoice; delivery
        note → invoice; invoice → delivery note/credit note/dunning.
    `changes` (optional): adjustments for the new document, e.g.
        {"lieferdatum": "20.11.2026"}, {"zahlungsziel": 14},
        {"voucherDate": "heute"}, {"title": "…"} or — for a PARTIAL credit
        note — {"lineItems": [{"type": "custom", "name": "Gutschrift
        Reklamation", "quantity": 1, "unitPrice": {"netAmount": 42.02,
        "taxRatePercentage": 19}}]}. Without lineItems a credit note covers
        the WHOLE invoice (= Storno).
    `changes` positions are completed like on create (article by number/name,
        German field names, price as text); payment terms, Skonto or discount
        for a delivery note or dunning are refused (they have none). A new
        zahlungsziel keeps the source's Skonto (`skonto: 0` removes it);
        `rabatt` switches between % and € (`rabatt: 0` removes it).
    `allow_duplicate`: a second follow-up of the same type (e.g. a second
        invoice from one order, also one next to it in the chain) is refused
        unless this is true. Credit notes: several PARTIAL ones (with
        lineItems) are fine, the result names the existing ones; a FULL one
        when credit notes already exist needs allow_duplicate. A further
        dunning while a dunning draft exists needs allow_duplicate too.
    Notes: an order or delivery note must be finalized in Lexware before it
    can be continued; a quotation draft is finalized by Lexware on the way.
    Quotations with optional/alternative positions cannot be continued via
    the interface. Result: number, type, totals and a `hinweis`.
    """
    return services.continue_document(
        _client_get(), identifier, target, changes=changes, allow_duplicate=allow_duplicate
    )


@mcp.tool
def create_dunning(identifier: str) -> dict[str, Any]:
    """Create a dunning (Mahnung / payment reminder) from an overdue invoice.

    `identifier`: the invoice's number (e.g. 'FB2600682') or id. With a
    customer name instead, the customer's overdue invoices are listed. The
    invoice must be finalized and still open — otherwise the reason is
    reported. The dunning is a draft linked to the invoice; each further one
    is the next level (titled e.g. "2. Mahnung", counted from finished
    dunnings). While a dunning DRAFT exists no new one is created (finish or
    delete it in Lexware). The result carries the dunning number (e.g.
    'RE0001_2'); later retrieval works with the dunning's id, that number or
    the invoice number.
    """
    return services.create_dunning(_client_get(), identifier)


@mcp.tool
def create_delivery_note_draft(body: dict[str, Any]) -> dict[str, Any]:
    """Create a delivery note as draft.

    Line items support an optional `description` (long text shown under the
    position name on the PDF). When a line item is based on an article, copy
    the article's `description` into it.

    Längenlimits (sonst lehnt Lexware ab): Belegtitel `title` max. 25 Zeichen;
    Einleitungstext `introduction` und Schlusstext `remark` je max. 2000;
    Positionsbezeichnung `name` max. 100; Positionsbeschreibung `description`
    max. 2000. Bei Überschreitung bitte kürzen (der Server meldet es sonst).

    Defaults when omitted: `voucherDate` today (plain dates like 2026-11-04
    are fine), `totalPrice` EUR, `taxConditions.taxType` net, line item
    `taxRatePercentage` 19 (0 for tax-free types); quotations expire after 30
    days. Line item type `custom` = free position with name and unitPrice;
    `material`/`service` only with the article's `id`.
    Free texts work: zahlungsziel "14 Tage 2 % Skonto, 30 Tage netto",
    steuerart "innergemeinschaftliche Lieferung"/"Ausfuhr"/"Kleinunternehmer",
    steuersatz "ermäßigt" (7) / "voll" (19) / "19% MwSt". A quantity may carry
    its unit ("5 Rollen", "2 Std"); a change ("+5%", "10 % mehr") or a unit that
    contradicts the position's ("30 min" on hours) is refused, not guessed. An
    own payment text is kept as written; only standard texts are rebuilt.
    """
    return services.create_delivery_note(_client_get(), body)


def serve_stdio(profile: str | None = None) -> None:
    """Run the stdio server, optionally pinned to one Lexware profile.

    The key is resolved (and validated to exist) up front: a server that
    cannot start should fail loudly at launch, not on the first tool call —
    and a pinned profile must never fall back to another installation.
    """
    global _profile, _client
    _profile = profile
    _client = None
    if profile:
        load_config(profile)  # raises ConfigError for a missing profile
        mcp.instructions = (
            f"{_BASE_INSTRUCTIONS} This server is connected to the Lexware "
            f"installation '{profile}'. When talking about results, say which "
            f"installation they come from if more than one is connected."
        )
    mcp.run()


def _parse_profile_arg(argv: list[str]) -> str | None:
    """Minimal `--profile NAME` / `--profile=NAME` / `-p NAME` parsing."""
    import argparse

    parser = argparse.ArgumentParser(prog="lxw-mcp", add_help=True)
    parser.add_argument("--profile", "-p", default=None)
    args, _ = parser.parse_known_args(argv)
    return args.profile


def run() -> None:
    """Entry point for `lxw-mcp` — runs the MCP server over stdio."""
    import sys

    from lxw_cli.config import active_profile
    from lxw_cli.core.errors import ConfigError

    try:
        # `--profile` wins; otherwise LEXWARE_PROFILE (e.g. set via `-e`).
        profile = active_profile(_parse_profile_arg(sys.argv[1:]))
        serve_stdio(profile)
    except ConfigError as exc:
        print(f"Fehler: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


def run_http() -> None:
    """Entry point for `lxw-mcp-http` — multi-user server over HTTP.

    Environment:
        LXW_MCP_PUBLIC_URL  public base URL (behind the reverse proxy),
                            e.g. https://mcp.example.com — used for the
                            OAuth metadata and the consent redirect.
        LXW_MCP_SECRET      token-sealing secret. Any random string works;
                            without it a fresh one is generated per start
                            and all previously issued tokens stop working.
        LXW_MCP_SECRET_PREVIOUS
                            the secret used before the last rotation. Tokens
                            sealed with it keep working (and are re-sealed
                            with LXW_MCP_SECRET on refresh); remove it 30
                            days after the rotation, or at once to log
                            everyone out.
        LXW_MCP_HOST/PORT   bind address (default 127.0.0.1:8788).
        LXW_MCP_DATA_DIR    where OAuth client registrations live
                            (default: <config dir>/mcp).
        LXW_MCP_STATUS_REFRESH
                            seconds between background checks of Lexware's
                            status page (default 600, matching its CDN cache;
                            0 disables). Keeps the cache warm so the error
                            path answers instantly, and logs incidents for
                            the operator. Only useful here — the stdio server
                            is short-lived and fills the cache lazily.
    """
    import os
    import secrets
    import sys

    from starlette.middleware import Middleware as ASGIMiddleware

    from lxw_cli.mcp_auth import (
        ENV_HOST,
        ENV_PORT,
        ENV_PUBLIC_URL,
        ENV_SECRET,
        ENV_SECRET_PREVIOUS,
        LexwareOAuthProvider,
        secret_problem,
    )

    host = os.environ.get(ENV_HOST, "127.0.0.1")
    port = int(os.environ.get(ENV_PORT, "8788"))
    public_url = os.environ.get(ENV_PUBLIC_URL, f"http://{host}:{port}")
    secret = os.environ.get(ENV_SECRET, "")
    previous = os.environ.get(ENV_SECRET_PREVIOUS, "")
    if previous and not secret:
        print(
            f"Fehler: {ENV_SECRET_PREVIOUS} ist gesetzt, {ENV_SECRET} aber "
            f"nicht. Beim Tausch kommt das neue Secret nach {ENV_SECRET}, "
            f"das bisherige nach {ENV_SECRET_PREVIOUS}.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    if not secret:
        secret = secrets.token_urlsafe(32)
        print(
            f"Warnung: {ENV_SECRET} ist nicht gesetzt — es wurde ein flüchtiges "
            "Secret erzeugt. Alle ausgestellten Tokens werden beim nächsten "
            "Neustart ungültig. Für den Dauerbetrieb ein festes Secret setzen.",
            file=sys.stderr,
        )
    else:
        problem = secret_problem(secret)
        if problem:
            print(f"Fehler: {problem}", file=sys.stderr)
            raise SystemExit(2)
    if previous:
        problem = secret_problem(previous, ENV_SECRET_PREVIOUS)
        if problem:
            print(f"Fehler: {problem}", file=sys.stderr)
            raise SystemExit(2)
        if previous == secret:
            print(
                f"Hinweis: {ENV_SECRET_PREVIOUS} ist gleich {ENV_SECRET} und "
                "wird ignoriert.",
                file=sys.stderr,
            )
        else:
            print(
                f"Hinweis: {ENV_SECRET_PREVIOUS} ist gesetzt — Tokens mit dem "
                "vorherigen Secret werden noch angenommen und beim Erneuern "
                "umgestellt. Nach 30 Tagen entfernen.",
                file=sys.stderr,
            )

    try:
        refresh = float(os.environ.get("LXW_MCP_STATUS_REFRESH", "600"))
    except ValueError:
        refresh = 600.0
    if refresh > 0:
        status.start_background_refresh(refresh)

    mcp.auth = LexwareOAuthProvider(
        public_url=public_url,
        secret=secret,
        previous_secrets=[previous] if previous else (),
    )
    mcp.run(
        transport="http",
        host=host,
        port=port,
        middleware=[ASGIMiddleware(BodySizeLimit)],
    )


if __name__ == "__main__":
    run()
