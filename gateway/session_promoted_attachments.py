"""Promoted attachment verbs: ``image.attach`` and ``pdf.attach``.

The legacy sidecar handlers looked their session up in the sidecar's own table, which never holds
an authority session, so every Desktop / TUI drop failed there. The session authority stages the
attachment itself instead:

* ``image.attach`` takes a host ``path`` (drop syntax with trailing text is accepted) and copies
  the image into the session profile's ``images/`` store.
* ``pdf.attach`` takes base64 ``content_base64`` / ``data`` bytes or a host ``path``, renders the
  requested page range with ``pdftoppm`` and stages each page PNG there.

A host path is only readable when this session's terminal works in this host's cwd
(``terminal_uses_host_cwd``): a remote / sandbox backend's tree is not this host's, so the client
must upload bytes instead. Base64 input always works — those bytes travel in the request. Staging
runs off the event loop under the owner profile's scope; refusals are ``RuntimeStoreError`` s.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from functools import partial
from pathlib import Path

from gateway.session_busy_controls import authorize
from gateway.session_promoted import profile_home, scoped
from hermes_state_runtime import RuntimeStoreError

_IMAGE_FIELDS = {"session_id", "profile", "path"}
_PDF_FIELDS = {"session_id", "profile", "path", "content_base64", "data", "filename",
               "first_page", "last_page"}
_TEXT_FIELDS = {"session_id", "profile", "path", "content_base64", "data", "filename"}
_RENDER_TIMEOUT_SECONDS = 120


def handlers(connection):
    return {"image.attach": partial(attach_image, connection),
            "pdf.attach": partial(attach_pdf, connection)}


async def attach_image(connection, ref, params):
    """``image.attach``: stage one host image file for the next turn of this session."""
    _validate(ref, params, _IMAGE_FIELDS)
    raw = params.get("path")
    if not isinstance(raw, str) or not raw.strip():
        raise RuntimeStoreError("invalid_params", field="path", detail="path is required")
    _subscribed(connection, ref)
    authorize(connection, ref, params, "session:submit")
    policy = _policy(connection, ref)
    _require_host_terminal(policy)
    return await scoped(connection, partial(_stage_image, connection, raw.strip()))


async def attach_pdf(connection, ref, params):
    """``pdf.attach``: render a PDF's pages to PNG and stage them for the next turn."""
    _validate(ref, params, _PDF_FIELDS)
    _subscribed(connection, ref)
    authorize(connection, ref, params, "session:submit")
    raw_b64 = params.get("content_base64") or params.get("data") or ""
    raw_path = params.get("path") or ""
    if not raw_b64 and not raw_path.strip():
        raise RuntimeStoreError("invalid_params", detail="path or content_base64 is required")
    if not raw_b64:
        # Only a host path depends on where the session's terminal runs; bytes never do.
        _require_host_terminal(_policy(connection, ref))
    return await scoped(connection, partial(_stage_pdf, connection, params))


# ── gates ─────────────────────────────────────────────────────────────────────────────────────


def _validate(ref, params, allowed):
    """The handler's own closed key/type set: the dispatcher's contract check is authoritative,
    this keeps a direct call to the handler equally strict."""
    extra = set(params) - allowed
    if extra or not isinstance(ref.session_id, str) or not ref.session_id:
        raise RuntimeStoreError(
            "invalid_params",
            detail=f"unexpected keys: {', '.join(sorted(extra))}" if extra else None)
    if any(value is not None and not isinstance(value, str)
           for key, value in params.items() if key in _TEXT_FIELDS):
        raise RuntimeStoreError("invalid_params", detail="text fields must be strings")


def _subscribed(connection, ref):
    if ref.session_id not in connection.subscriptions:
        raise RuntimeStoreError("permission_denied")


def _policy(connection, ref):
    from gateway.session_policy import policy_for_source

    authority = connection.authority
    live = authority.sessions.get(ref.session_id)
    policy = policy_for_source(getattr(authority, "runner", None), getattr(live, "source", None))
    if policy is None:
        raise RuntimeStoreError("permission_denied")
    return policy


def _require_host_terminal(policy):
    """A host path names a file in THIS process's filesystem; only a terminal that runs here can
    read it. The remote backends (ssh, modal, …) resolve their own tree, so the client must send
    the bytes instead of a path."""
    from gateway.session_policy import terminal_uses_host_cwd

    if terminal_uses_host_cwd(json.loads(policy.terminal_json)):
        return
    raise RuntimeStoreError(
        "invalid_params", field="path",
        detail="this session's terminal runs on another host; upload the file bytes instead of a host path")


# ── image staging (runs under the owner profile scope) ───────────────────────────────────────


def _stage_image(connection, raw):
    from hermes_cli.cli_terminal_input import (
        _IMAGE_EXTENSIONS, _resolve_attachment_path, _split_path_input,
    )
    from tui_gateway.prompt_attachments import _ATTACHMENT_MAX_BYTES, stage_image_bytes

    token, remainder = _split_path_input(raw)
    source = _resolve_attachment_path(token)
    if source is None:
        raise RuntimeStoreError("not_found", field="path", detail=f"image not found: {token}")
    ext = source.suffix.lower()
    if ext not in _IMAGE_EXTENSIONS:
        raise RuntimeStoreError("invalid_params", field="path",
                                detail=f"unsupported image: {source.name}")
    try:
        with source.open("rb") as handle:
            data = handle.read(_ATTACHMENT_MAX_BYTES + 1)
    except OSError as exc:
        raise RuntimeStoreError("storage_unavailable") from exc
    if len(data) > _ATTACHMENT_MAX_BYTES:
        raise RuntimeStoreError(
            "invalid_params", field="path",
            detail=f"image too large; cap is {_ATTACHMENT_MAX_BYTES // (1024 * 1024)} MB")
    try:
        path = stage_image_bytes(_images_dir(connection), data, ext, prefix="upload")
    except OSError as exc:
        raise RuntimeStoreError("storage_unavailable") from exc
    return {"attached": True, "path": str(path), "count": 1, "remainder": remainder,
            "text": remainder or f"[User attached image: {path.name}]",
            "bytes": len(data), **_image_meta(path)}


# ── pdf staging (runs under the owner profile scope) ─────────────────────────────────────────


def _stage_pdf(connection, params):
    raw_b64 = params.get("content_base64") or params.get("data") or ""
    raw_path = params.get("path") or ""
    if shutil.which("pdftoppm") is None:
        raise RuntimeStoreError(
            "storage_unavailable",
            detail="pdftoppm is not installed (the poppler-utils package is required)")
    first_page, last_page = _page_range(params)
    try:
        with tempfile.TemporaryDirectory(prefix="pdf_attach_") as workdir:
            root = Path(workdir)
            if raw_b64:
                pdf_path, display_name = _decoded_pdf(raw_b64, params.get("filename"), root)
            else:
                pdf_path, display_name = _host_pdf(raw_path)
            rendered = _render_pages(pdf_path, first_page, last_page, root)
            pages = _stage_pages(connection, first_page, rendered)
    except OSError as exc:
        raise RuntimeStoreError("storage_unavailable") from exc
    return {"attached": True, "filename": display_name, "pages_attached": len(pages),
            "pages": pages, "count": len(pages),
            "text": f"[User attached PDF: {display_name} ({len(pages)} page(s))]"}


def _decoded_pdf(raw_b64, filename, root):
    from tui_gateway.prompt_attachments import _PDF_ATTACH_MAX_BYTES, _decode_attach_base64

    try:
        data = _decode_attach_base64(raw_b64, mime_prefix="application/pdf",
                                     max_bytes=_PDF_ATTACH_MAX_BYTES, label="PDF")
    except OverflowError as exc:
        raise RuntimeStoreError(
            "invalid_params", field="content_base64",
            detail=f"PDF too large; cap is {_PDF_ATTACH_MAX_BYTES // (1024 * 1024)} MB") from exc
    if not data:
        detail = "data is not valid base64" if data is None else "decoded PDF is empty"
        raise RuntimeStoreError("invalid_params", field="content_base64", detail=detail)
    if data[:5] != b"%PDF-":
        raise RuntimeStoreError("invalid_params", field="content_base64",
                                detail="payload is not a PDF (missing %PDF- magic bytes)")
    path = root / "input.pdf"
    path.write_bytes(data)
    return path, str(filename or "").strip() or "uploaded.pdf"


def _host_pdf(raw_path):
    from hermes_cli.cli_terminal_input import _resolve_attachment_path, _split_path_input
    from tui_gateway.prompt_attachments import _PDF_ATTACH_MAX_BYTES

    token, _remainder = _split_path_input(raw_path)
    source = _resolve_attachment_path(token)
    if source is None or not source.is_file():
        raise RuntimeStoreError("not_found", field="path", detail=f"PDF not found: {token}")
    if source.suffix.lower() != ".pdf":
        raise RuntimeStoreError("invalid_params", field="path", detail=f"not a PDF: {source.name}")
    if source.stat().st_size > _PDF_ATTACH_MAX_BYTES:
        raise RuntimeStoreError(
            "invalid_params", field="path",
            detail=f"PDF too large; cap is {_PDF_ATTACH_MAX_BYTES // (1024 * 1024)} MB")
    return source, source.name


def _page_range(params):
    from tui_gateway.prompt_attachments import _PDF_ATTACH_MAX_PAGES

    try:
        first_page = int(params.get("first_page") or 1)
        last_page = None if params.get("last_page") is None else int(params["last_page"])
    except (TypeError, ValueError) as exc:
        raise RuntimeStoreError("invalid_params", field="first_page",
                                detail="first_page/last_page must be integers") from exc
    if first_page < 1:
        raise RuntimeStoreError("invalid_params", field="first_page",
                                detail="first_page must be >= 1")
    if last_page is None:
        last_page = first_page + _PDF_ATTACH_MAX_PAGES - 1
    if last_page < first_page:
        raise RuntimeStoreError("invalid_params", field="last_page",
                                detail="last_page must be >= first_page")
    if last_page - first_page + 1 > _PDF_ATTACH_MAX_PAGES:
        raise RuntimeStoreError(
            "invalid_params", field="last_page",
            detail=f"page range exceeds cap of {_PDF_ATTACH_MAX_PAGES} pages per attach call")
    return first_page, last_page


def _render_pages(pdf_path, first_page, last_page, root):
    from hermes_cli._subprocess_compat import windows_hide_flags

    argv = ["pdftoppm", "-png", "-r", "150", "-f", str(first_page), "-l", str(last_page),
            str(pdf_path), str(root / "page")]
    try:
        result = subprocess.run(
            argv, capture_output=True, text=True, timeout=_RENDER_TIMEOUT_SECONDS,
            stdin=subprocess.DEVNULL, encoding="utf-8", errors="replace",
            creationflags=windows_hide_flags(), check=False)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeStoreError(
            "storage_unavailable",
            detail=f"pdftoppm timed out (>{_RENDER_TIMEOUT_SECONDS}s)") from exc
    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "").strip().splitlines()[-3:]
        raise RuntimeStoreError("storage_unavailable",
                                detail="pdftoppm failed: " + " | ".join(tail))
    rendered = sorted(root.glob("page-*.png"))
    if not rendered:
        raise RuntimeStoreError("storage_unavailable",
                                detail="pdftoppm produced no pages (corrupt PDF?)")
    return rendered


def _stage_pages(connection, first_page, rendered):
    from tui_gateway.prompt_attachments import stage_image_bytes

    pages = []
    directory = _images_dir(connection)
    for source in rendered:
        page_num = source.stem.split("-", 1)[-1]
        try:
            page = int(page_num)
        except ValueError:
            page = first_page + len(pages)
        path = stage_image_bytes(directory, source.read_bytes(), ".png", prefix=f"pdf_p{page_num}")
        pages.append({"path": str(path), "page": page, **_image_meta(path)})
    return pages


def _images_dir(connection):
    """The session profile's ``images/`` store — what the sidecar's ``_session_images_dir``
    resolved for a profile-home session, and what the sandbox mounts for this agent."""
    return profile_home(connection) / "images"


def _image_meta(path):
    """``server.py::_image_meta`` parity (name + dimensions/token hint when PIL can open it).

    The sidecar copy closes over the sidecar's rebound globals, so a canonical family cannot
    import it without importing the legacy facade; the body is kept byte-for-byte equivalent.
    """
    import contextlib

    meta = {"name": Path(path).name}
    with contextlib.suppress(Exception):
        from PIL import Image

        with Image.open(path) as image:
            width, height = (int(value) for value in image.size)
        tiles = (max(1, (width + 511) // 512) * max(1, (height + 511) // 512)
                 if width > 0 and height > 0 else 0)
        meta.update(width=width, height=height, token_estimate=tiles * 85)
    return meta
