"""ADR-11 P4: image limits/fitting, image wire contracts, box mapping, cache identity."""

from __future__ import annotations

import base64
import io
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
from PIL import Image

from agent.judgment import chain as chain_module
from agent.judgment.cache import JudgmentCache, state_identity
from agent.judgment.config import JudgmentSettings
from agent.judgment.images import (
    JEV_LIMITS,
    WHOLE_IMAGE,
    WORKERS_AI_LIMITS,
    fit_image,
    fit_images,
    images_acceptable,
    unfit_box,
)
from agent.judgment.native import NativeJudge, NativeRoute
from agent.judgment.questions import question_from_spec
from agent.judgment.types import (
    BoxAnswer,
    BoxEdges,
    BoxQuestion,
    JudgmentImage,
    JudgmentRequest,
    JudgmentUnavailable,
    JudgmentValidationError,
    NoulQuestion,
    image_data_url,
)
from agent.judgment.wires import WIRES


def _png(width: int, height: int, color: tuple[int, int, int] = (12, 34, 56)) -> str:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _gif(width: int = 320, height: int = 320) -> str:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (1, 2, 3)).save(buffer, format="GIF")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _image(width: int = 320, height: int = 320, **kwargs) -> JudgmentImage:
    return JudgmentImage(data=_png(width, height), mime_type="image/png", **kwargs)


# ── validation ─────────────────────────────────────────────────────────────


def test_image_validation_rejects_malformed_input():
    from agent.judgment.types import validate_image

    with pytest.raises(JudgmentValidationError, match="base64"):
        validate_image(JudgmentImage("not base64!", "image/png"))
    with pytest.raises(JudgmentValidationError, match="MIME"):
        validate_image(JudgmentImage("AAAA", "text/plain"))
    with pytest.raises(JudgmentValidationError, match="detail"):
        validate_image(JudgmentImage("AAAA", "image/png", detail="huge"))
    request = JudgmentRequest(
        state="x", questions={"q0": NoulQuestion("Is it?")},
        images=(JudgmentImage("AAAA", "image/png"),))
    from agent.judgment.types import validate_request

    with pytest.raises(JudgmentValidationError, match="not enabled"):
        validate_request(request)


def test_box_question_comes_from_an_image_index():
    question = question_from_spec("q0", {"instructions": "Find the button", "image": 1})
    assert isinstance(question, BoxQuestion) and question.image == 1
    with pytest.raises(JudgmentValidationError, match="non-negative"):
        question_from_spec("q0", {"instructions": "Find it", "image": -1})
    with pytest.raises(JudgmentValidationError, match="not criteria"):
        question_from_spec("q0", {"instructions": "Find it", "image": 0, "criteria": ["a", "b"]})


def test_box_question_index_must_exist_in_request():
    from agent.judgment.types import validate_request

    request = JudgmentRequest(
        state="x", questions={"q0": BoxQuestion("Find it", 1)},
        images=(_image(),))
    with pytest.raises(JudgmentValidationError, match="names image 1"):
        validate_request(request, allow_images=True, allow_box=True)


def test_image_data_url_normalizes_base64_and_mime():
    image = JudgmentImage(_png(280, 280).rstrip("="), "IMAGE/PNG")
    url = image_data_url(image)
    assert url.startswith("data:image/png;base64,")
    assert url.endswith("=") or len(url.split(",", 1)[1]) % 4 == 0


# ── cache identity ─────────────────────────────────────────────────────────


def test_multimodal_state_identity_merges_padding_but_not_detail():
    data = _png(300, 300)
    padded = JudgmentImage(data, "image/png")
    unpadded = JudgmentImage(data.rstrip("="), "IMAGE/PNG")
    assert state_identity("s", (padded,)) == state_identity("s", (unpadded,))
    assert state_identity("s", (padded,))[0].startswith("multimodal-judgment-cache-v1:")
    detailed = JudgmentImage(data, "image/png", detail="high")
    assert state_identity("s", (padded,))[0] != state_identity("s", (detailed,))[0]
    # TypeSafe drops detail on the wire, so its cache must too.
    assert state_identity("s", (padded,), ignores_image_detail=True) == state_identity(
        "s", (detailed,), ignores_image_detail=True)
    assert len(state_identity("s")[0]) == 64


# ── fitting ────────────────────────────────────────────────────────────────


def test_fit_pads_an_extreme_aspect_ratio_and_maps_boxes_back():
    fitted = fit_image(_image(401, 41), JEV_LIMITS, max_bytes=JEV_LIMITS.max_bytes)
    with Image.open(io.BytesIO(base64.b64decode(fitted.image.data))) as canvas:
        assert canvas.size == (1568, 280)
    assert fitted.content.left == 0.0 and fitted.content.right == 1.0
    assert fitted.content.top == pytest.approx(60 / 280)
    assert fitted.content.bottom == pytest.approx(220 / 280)
    unmapped = unfit_box(
        BoxEdges(top=0.2, bottom=0.8, left=0.1, right=0.9),
        fitted.content)
    assert unmapped.top == 0.0  # padding clamps onto the source
    assert unmapped.bottom == 1.0
    assert (unmapped.left, unmapped.right) == (0.1, 0.9)


def test_fit_image_within_bounds_is_sent_as_is():
    image = _image(320, 320)
    fitted = fit_images((image,), JEV_LIMITS)
    assert fitted[0].image is image
    assert fitted[0].content == WHOLE_IMAGE


def test_small_image_is_scaled_up_to_the_minimum_side():
    fitted = fit_image(_image(1, 1), JEV_LIMITS, max_bytes=JEV_LIMITS.max_bytes)
    with Image.open(io.BytesIO(base64.b64decode(fitted.image.data))) as canvas:
        width, height = canvas.size
    assert width >= 280 and height >= 280
    assert width * height >= 78_400


def test_jev_fitting_reencodes_an_unsupported_mime_to_png():
    image = JudgmentImage(_gif(), "image/gif")
    assert images_acceptable((image,), JEV_LIMITS) is True
    fitted = fit_images((image,), JEV_LIMITS)[0]
    assert fitted.image.mime_type == "image/png"
    with Image.open(io.BytesIO(base64.b64decode(fitted.image.data))) as canvas:
        assert canvas.size == (320, 320)
        assert canvas.format == "PNG"


def test_workers_ai_caps_reject_instead_of_fitting():
    images = tuple(_image() for _ in range(5))
    with pytest.raises(JudgmentValidationError, match="at most 4"):
        fit_images(images, WORKERS_AI_LIMITS)
    with pytest.raises(JudgmentValidationError, match="16000000"):
        fit_images((_image(5000, 4000),), WORKERS_AI_LIMITS)
    with pytest.raises(JudgmentValidationError, match="does not accept image/gif"):
        fit_images((JudgmentImage(_gif(), "image/gif"),), WORKERS_AI_LIMITS)


# ── wire contracts ─────────────────────────────────────────────────────────


def _image_request(*, box: bool = False, detail: str | None = None) -> JudgmentRequest:
    questions: dict[str, Any] = {"q0": NoulQuestion("Is it?")}
    if box:
        questions["q1"] = BoxQuestion("Where is the button?", 0)
    return JudgmentRequest(
        state={"content": "hello"}, questions=questions,
        images=(_image(320, 320, detail=detail),))


def test_typesafe_encodes_assets_and_references():
    body = WIRES["typesafe"].encode(_image_request(box=True), "jev-1.14.0")
    references = body["state"]["__omp_judgment_images"]
    assert set(body["assets"]) == set(references)
    asset = next(iter(body["assets"].values()))
    assert asset["type"] == "image" and asset["format"] == "png"
    assert body["questions"]["q0"] == {"type": "noul", "instructions": "Is it?"}
    assert body["questions"]["q1"] == {"type": "box", "instructions": "Where is the button?"}
    plain = WIRES["typesafe"].encode(
        JudgmentRequest(state={"content": "hello"}, questions={"q0": NoulQuestion("Is it?")}),
        "jev-1.14.0")
    assert plain == {
        "state": {"content": "hello"},
        "model": "jev-1.14.0",
        "questions": {"q0": {"type": "noul", "instructions": "Is it?"}},
    }


def test_openrouter_appends_image_parts_and_maps_original_detail():
    body = WIRES["openrouter-decisions"].encode(
        _image_request(box=True, detail="original"), "typesafe/jev-1.14")
    part = body["state"]["__omp_judgment_images"][0]
    assert part["type"] == "image_url"
    assert part["image_url"]["url"].startswith("data:image/png;base64,")
    assert part["image_url"]["detail"] == "auto"
    assert "image" not in body["questions"]["q1"]


def test_openai_decisions_sends_input_image_blocks():
    body = WIRES["openai-decisions"].encode(_image_request(detail="high"), "gpt-6-luna")
    content = body["input"][0]["content"]
    assert content[0] == {"type": "input_text", "text": json.dumps({"content": "hello"})}
    assert content[1]["type"] == "input_image"
    assert content[1]["detail"] == "high"
    assert body["questions"] == [{
        "type": "predicate", "name": "q0", "instructions": "Is it?"}]


def test_workers_ai_sends_top_level_data_urls():
    body = WIRES["workers-ai-systemone"].encode(_image_request(), "@cf/cloudflare/clef")
    assert body["model"] == "clef"
    assert body["images"][0].startswith("data:image/png;base64,")
    assert "assets" not in body


# ── native wire + cache + box mapping ──────────────────────────────────────


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        record = {
            "path": self.path,
            "headers": dict(self.headers),
            "body": json.loads(self.rfile.read(length)),
        }
        self.server.records.append(record)  # type: ignore[attr-defined]
        payload = self.server.payload  # type: ignore[attr-defined]
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode())

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    httpd.records = []  # type: ignore[attr-defined]
    httpd.payload = {}  # type: ignore[attr-defined]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd
    finally:
        httpd.shutdown()
        thread.join(timeout=5)


def _route(server) -> NativeRoute:
    return NativeRoute(
        api="typesafe", provider="typesafe", model="jev-test",
        base_url=f"http://127.0.0.1:{server.server_address[1]}",
        api_key="test-key", timeout_ms=1000)


def _box_payload() -> dict:
    return {
        "model": "jev-test",
        "answers": {"q0": {
            "type": "box",
            "box": {"top": 0.4, "bottom": 0.6, "left": 0.5, "right": 0.6},
            "confidence": 0.9,
        }},
        "usage": {"input_tokens": 100, "output_tokens": 1},
    }


def test_native_sends_images_with_the_danger_zone_header_and_maps_boxes(server, tmp_path):
    server.payload = _box_payload()
    cache = JudgmentCache(tmp_path / "judgment.db")
    try:
        image = _image(401, 41)
        request = JudgmentRequest(
            state="x", questions={"q0": BoxQuestion("Find the strip", 0)},
            images=(image,))
        result = NativeJudge(
            _route(server), JudgmentSettings(), cache=cache).judge(request)
        record = server.records[0]
        headers = {key.lower(): value for key, value in record["headers"].items()}
        assert headers["typesafe-danger-zone-images"] == "true"
        assert record["body"]["assets"]
        answer = result.answers["q0"]
        assert isinstance(answer, BoxAnswer)
        # 401x41 fits to 1568x280 with the source centered at top 60/280..220/280.
        assert answer.box.top == pytest.approx(0.325)
        assert answer.box.bottom == pytest.approx(0.675)
        assert (answer.box.left, answer.box.right) == (0.5, 0.6)

        # The same request is a cache hit: no second HTTP call, same source-space answer.
        again = NativeJudge(
            _route(server), JudgmentSettings(), cache=cache).judge(request)
        assert len(server.records) == 1
        assert again.cached_questions == 1
        assert again.answers["q0"] == answer
    finally:
        cache.close()


def test_native_without_images_keeps_the_plain_body(server, tmp_path):
    server.payload = {
        "model": "jev-test",
        "answers": {"q0": {"type": "noul", "noul": 0.5}},
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    judge = NativeJudge(_route(server), JudgmentSettings(cache=False, audit=False))
    judge.judge(JudgmentRequest(state="x", questions={"q0": NoulQuestion("Is it?")}))
    record = server.records[0]
    assert "typesafe-danger-zone-images" not in {
        key.lower() for key in record["headers"]}
    assert record["body"] == {
        "state": "x", "model": "jev-test",
        "questions": {"q0": {"type": "noul", "instructions": "Is it?"}},
    }


def test_openai_decisions_rejects_more_than_128_images():
    images = tuple(_image(280, 280) for _ in range(129))
    with pytest.raises(JudgmentValidationError, match="at most 128"):
        fit_images(images, WIRES["openai-decisions"].images)


# ── text vision ────────────────────────────────────────────────────────────


def test_text_vision_requires_opt_in_and_known_model_support(monkeypatch):
    from types import SimpleNamespace

    import agent.image_routing as routing
    import agent.judgment.chat as chat_module

    monkeypatch.setattr(routing, "_supports_vision_override", lambda *a, **k: True)
    calls: list[dict] = []

    def fake_call_llm(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="q0: yes"))],
            usage=None, model="gpt-4o")

    monkeypatch.setattr(chat_module, "call_llm", fake_call_llm)
    config = {"auxiliary": {"judge": {
        "provider": "openai-api", "model": "gpt-4o", "images": True}}}
    backend = chat_module.ChatTextBackend(JudgmentSettings(), config)
    assert backend.supports_images is True
    result = backend.judge(_image_request())
    assert result.answers["q0"].noul == 1.0
    content = calls[0]["messages"][1]["content"]
    assert content[0]["type"] == "text"
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")

    monkeypatch.setattr(routing, "_supports_vision_override", lambda *a, **k: False)
    assert chat_module.ChatTextBackend(JudgmentSettings(), config).supports_images is False
    default = chat_module.ChatTextBackend(JudgmentSettings(), {})
    assert default.supports_images is False


# ── chain compatibility ────────────────────────────────────────────────────


def _cfg(**judgment) -> dict:
    config = {
        "mode": "auto", "cache": False, "audit": False, "native": [],
    }
    config.update(judgment)
    return {"judgment": config, "auxiliary": {"judge": {"provider": "auto", "model": ""}}}


def test_chain_reports_no_compatible_candidate(monkeypatch):
    class _TextOnly:
        supports_images = False

        def __init__(self, settings, config=None):
            pass

        def judge(self, request, **kwargs):  # pragma: no cover - never reached
            raise AssertionError("must not call a candidate that cannot take images")

    monkeypatch.setattr(chain_module, "ChatTextBackend", _TextOnly)
    from agent.judgment.chain import ChainJudge
    from agent.judgment.config import load_judgment_settings

    config = _cfg(mode="text")
    with pytest.raises(JudgmentUnavailable, match="no compatible judge candidate"):
        ChainJudge(load_judgment_settings(config), config).judge(_image_request())


def test_chain_requires_images_enabled(monkeypatch):
    from agent.judgment.chain import ChainJudge
    from agent.judgment.config import load_judgment_settings

    config = _cfg(mode="text", images=False)
    with pytest.raises(JudgmentValidationError, match="not enabled"):
        ChainJudge(load_judgment_settings(config), config).judge(_image_request())


def test_chain_skips_a_route_whose_image_cap_is_exceeded(monkeypatch):
    from types import SimpleNamespace

    from agent.judgment.chain import ChainJudge
    from agent.judgment.config import load_judgment_settings
    from agent.judgment.types import JudgmentResult, NoulAnswer

    calls: list[str] = []
    constructed: list[str] = []

    class FakeNative:
        def __init__(self, route, settings, cache=None):
            constructed.append(route.model)
            self.route = route
            self.label = route.label
            self.supports_images = True
            self.supports_boxes = False
            self.wire = SimpleNamespace(
                images=WORKERS_AI_LIMITS if route.model == "workers" else JEV_LIMITS)

        def judge(self, request, **kwargs):
            calls.append(self.route.model)
            return JudgmentResult(
                api=self.route.api, provider=self.route.provider, model=self.route.model,
                answers={"q0": NoulAnswer(0.5)})

    monkeypatch.setattr(chain_module, "NativeJudge", FakeNative)
    monkeypatch.setattr(
        chain_module, "get_env_value_prefer_dotenv",
        lambda name: {"TYPESAFE_API_KEY": "typesafe-key",
                      "CLOUDFLARE_API_TOKEN": "cf-token",
                      "CLOUDFLARE_ACCOUNT_ID": "acct"}.get(name, ""))
    config = _cfg(native=[
        {"api": "workers-ai-systemone", "provider": "cloudflare-workers-ai",
         "model": "workers"},
        {"api": "typesafe", "provider": "typesafe", "model": "jev"},
    ])
    request = JudgmentRequest(
        state="x", questions={"q0": NoulQuestion("Is it?")},
        images=tuple(_image() for _ in range(5)))
    result = ChainJudge(load_judgment_settings(config), config).judge(request)
    assert constructed == ["workers", "jev"]
    assert calls == ["jev"]
    assert result.model == "jev"
