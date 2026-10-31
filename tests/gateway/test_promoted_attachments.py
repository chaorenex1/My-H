"""P1 attachment promotions: ``image.attach`` and ``pdf.attach`` on the session authority.

Both verbs are dispatched through the canonical dispatcher, so the declared params/result
contracts and the refusal envelope are exercised as a client sees them. A host ``path`` is only
readable when the session's terminal works in this host's cwd; base64 bytes always are.
"""
import base64
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio

# ``scoped`` enters the owner profile scope; warm the module here — importing it inside a guarded
# test reads the install payload manifest beside the checkout (tests/home_io_guard.py).
import gateway.run
from gateway import session_policy
from gateway.session_authority import LiveSession, SessionAuthority
from gateway.session_controls import AuthorityConnection
from gateway.session_policy import LocalSessionPolicy
from hermes_state import SessionDB
from hermes_state_runtime import begin_runtime_epoch
from tui_gateway.contracts.registry import CANONICAL_METHODS

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl2nL8AAAAASUVORK5CYII=")
PDF = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"


def _policy(home, terminal=None):
    return LocalSessionPolicy(
        source="test", platform="local", cwd=str(home), model=None, toolsets=(),
        config_json="{}", request_json="{}",
        terminal_json=json.dumps(terminal or {"TERMINAL_ENV": "local"}))


@pytest_asyncio.fixture
async def owner(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(session_policy, "policy_for_source",
                        lambda runner, source: _policy(tmp_path))
    with SessionDB(db_path=tmp_path / "state.db") as db:
        db.create_session("s", source="test")
        epoch = begin_runtime_epoch(db, instance_id="current")
        authority = SessionAuthority(SimpleNamespace(_draining=False, session_authorities=()),
                                     profile_id=str(tmp_path), instance_id="current",
                                     db=db, epoch=epoch)
        authority.sessions["s"] = LiveSession(None, "route")
        connection = AuthorityConnection(authority, SimpleNamespace(write=lambda frame: None),
                                         {"user_id": "owner"})
        connection.subscriptions["s"] = "viewer"
        try:
            yield connection, tmp_path
        finally:
            await connection.close()
            authority.db.close()


async def _call(connection, method, params):
    reply = await connection.dispatch({"id": 1, "method": method, "params": params})
    assert "error" not in reply, reply
    CANONICAL_METHODS[method].result.model_validate(reply["result"])
    return reply["result"]


async def _refusal(connection, method, params):
    reply = await connection.dispatch({"id": 1, "method": method, "params": params})
    assert "error" in reply, reply
    return reply["error"]


@pytest.mark.asyncio
async def test_image_attach_stages_a_host_file_in_the_profile(owner):
    connection, home = owner
    source = home / "photo.png"
    source.write_bytes(PNG)

    result = await _call(connection, "image.attach",
                         {"session_id": "s", "path": f"{source} a caption"})

    staged = Path(result["path"])
    assert staged.parent == home / "images" and staged.read_bytes() == PNG
    assert result["attached"] is True and result["count"] == 1
    assert result["remainder"] == "a caption" and result["text"] == "a caption"
    assert result["bytes"] == len(PNG)

    # The declared ``profile`` selector is nullable: null means this owner, like an omitted key.
    again = await _call(connection, "image.attach",
                        {"session_id": "s", "profile": None, "path": str(source)})
    assert Path(again["path"]).read_bytes() == PNG


@pytest.mark.asyncio
async def test_base64_image_stages_under_the_profile(owner):
    """The bytes route (``image.attach_bytes``) is the one the declared ``image.attach`` contract
    leaves for payloads: its params carry only a host ``path``."""
    connection, home = owner

    result = await _call(connection, "image.attach_bytes",
                         {"session_id": "s", "content_base64": base64.b64encode(PNG).decode(),
                          "filename": "../../shot.png"})

    staged = Path(result["path"])
    assert staged.parent == home / "cache" / "images" and staged.read_bytes() == PNG
    assert result["attached"] is True and result["count"] == 1 and result["mime"] == "image/png"


@pytest.mark.asyncio
async def test_attachment_refusals_name_the_bad_input(owner, monkeypatch):
    connection, home = owner
    (home / "notes.txt").write_text("not an image")
    monkeypatch.setattr("tui_gateway.prompt_attachments._ATTACH_BYTES_MAX_BYTES", 4)

    error = await _refusal(connection, "image.attach_bytes",
                           {"session_id": "s", "content_base64": "not-base64!!"})
    assert error["message"] == "invalid_params", error
    error = await _refusal(connection, "image.attach_bytes",
                           {"session_id": "s", "content_base64": base64.b64encode(PNG).decode()})
    assert error["message"] == "invalid_params", error
    error = await _refusal(connection, "image.attach",
                           {"session_id": "s", "path": str(home / "notes.txt")})
    assert error["message"] == "invalid_params" and error["data"]["field"] == "path", error


@pytest.mark.asyncio
async def test_pdf_attach_refuses_without_pdftoppm(owner, monkeypatch):
    connection, _home = owner
    monkeypatch.setattr(shutil, "which", lambda _name: None)

    error = await _refusal(connection, "pdf.attach",
                           {"session_id": "s",
                            "content_base64": base64.b64encode(PDF).decode()})

    assert error["message"] == "storage_unavailable", error
    assert "pdftoppm" in error["data"]["detail"]


@pytest.mark.asyncio
async def test_pdf_attach_refuses_bad_base64_and_oversized_bytes(owner, monkeypatch):
    connection, _home = owner
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/pdftoppm")

    error = await _refusal(connection, "pdf.attach",
                           {"session_id": "s", "content_base64": "@@@"})
    assert error["message"] == "invalid_params" and error["data"]["field"] == "content_base64", error
    error = await _refusal(connection, "pdf.attach",
                           {"session_id": "s", "content_base64": base64.b64encode(b"no magic").decode()})
    assert error["message"] == "invalid_params" and error["data"]["field"] == "content_base64", error

    monkeypatch.setattr("tui_gateway.prompt_attachments._PDF_ATTACH_MAX_BYTES", 8)
    error = await _refusal(connection, "pdf.attach",
                           {"session_id": "s", "content_base64": base64.b64encode(PDF * 2).decode()})
    assert error["message"] == "invalid_params" and "cap" in error["data"]["detail"], error


@pytest.mark.asyncio
async def test_pdf_attach_renders_and_stages_pages(owner, monkeypatch):
    import gateway.session_promoted_attachments as family

    connection, home = owner
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/pdftoppm")

    def render(argv, **_kwargs):
        prefix = Path(argv[-1])
        for page in (1, 2):
            prefix.with_name(f"{prefix.name}-{page}.png").write_bytes(PNG)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(family.subprocess, "run", render)

    result = await _call(connection, "pdf.attach",
                         {"session_id": "s", "content_base64": base64.b64encode(PDF).decode()})

    assert result["attached"] is True and result["filename"] == "uploaded.pdf"
    assert result["pages_attached"] == 2 and result["count"] == 2
    assert [page["page"] for page in result["pages"]] == [1, 2]
    for page in result["pages"]:
        staged = Path(page["path"])
        assert staged.parent == home / "images" and staged.read_bytes() == PNG
    assert "2 page(s)" in result["text"]


@pytest.mark.asyncio
async def test_host_paths_are_refused_on_a_remote_terminal(owner, monkeypatch):
    connection, home = owner
    monkeypatch.setattr(session_policy, "policy_for_source",
                        lambda runner, source: _policy(home, {"TERMINAL_ENV": "ssh"}))
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/pdftoppm")
    (home / "photo.png").write_bytes(PNG)
    (home / "doc.pdf").write_bytes(PDF)

    for method, path in (("image.attach", home / "photo.png"), ("pdf.attach", home / "doc.pdf")):
        error = await _refusal(connection, method, {"session_id": "s", "path": str(path)})
        assert error["message"] == "invalid_params" and error["data"]["field"] == "path", error
        assert "upload" in error["data"]["detail"], error
    assert not (home / "images").exists()


@pytest.mark.asyncio
async def test_unknown_key_is_refused_with_its_field(owner):
    connection, home = owner

    error = await _refusal(connection, "image.attach",
                           {"session_id": "s", "path": str(home / "photo.png"),
                            "content_base64": "aGk="})

    assert error["code"] == 4001
    assert error["data"]["fields"] == ["content_base64"]
