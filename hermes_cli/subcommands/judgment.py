"""``hermes judgment ask|doctor|log`` — run and inspect typed judgments (ADR-11 P1)."""

from __future__ import annotations

import base64
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from hermes_cli.subcommands._shared import add_json_flag

logger = logging.getLogger(__name__)


def build_judgment_parser(subparsers, *, cmd_judgment: Callable) -> None:
    """Attach the ``judgment`` subcommand to ``subparsers``."""
    parser = subparsers.add_parser(
        "judgment",
        help="Run and inspect typed side-channel judgments",
        description=(
            "Judgments are typed questions answered by native System One/Decisions routes "
            "or by the auxiliary.judge text model. They never become transcript rows."),
    )
    actions = parser.add_subparsers(dest="judgment_action")

    ask = actions.add_parser("ask", help="Answer a question file about a state")
    source = ask.add_mutually_exclusive_group(required=True)
    source.add_argument("--state", help="Inline state text")
    source.add_argument("--state-file", help="JSON state file")
    ask.add_argument("--questions-file", required=True, help="JSON questions file")
    ask.add_argument(
        "--image", action="append", default=[], metavar="PATH",
        help="Image for image/box questions (repeatable; the index is the flag order)")
    ask.add_argument("--purpose", default="cli", help="Audit label (default: cli)")
    add_json_flag(ask, "Emit machine-readable JSON")
    ask.set_defaults(func=cmd_judgment)

    doctor = actions.add_parser("doctor", help="Show routes, credentials, cache, and audit state")
    add_json_flag(doctor, "Emit machine-readable JSON")
    doctor.set_defaults(func=cmd_judgment)

    log = actions.add_parser("log", help="Show recent judgment runs")
    log.add_argument("--limit", type=int, default=50, help="Rows to show (default: 50)")
    log.add_argument("--purpose", default="", help="Filter by purpose")
    log.add_argument("--status", default="", choices=["", "ok", "partial", "error", "aborted"],
                     help="Filter by status")
    add_json_flag(log, "Emit machine-readable JSON")
    log.set_defaults(func=cmd_judgment)


def _questions_path(raw: str) -> Path:
    candidate = Path(raw).expanduser()
    if candidate.exists():
        return candidate
    from agent.judgment.cache import judgment_home

    if not candidate.is_absolute():
        name = raw if raw.endswith(".json") else f"{raw}.json"
        nested = judgment_home() / "eval" / "questions" / name
        if nested.exists():
            return nested
    raise FileNotFoundError(f"questions file not found: {raw}")


def _load_questions(raw: str) -> dict[str, Any]:
    from agent.judgment.questions import questions_from_file

    path = _questions_path(raw)
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read questions file {path}: {exc}") from None
    return dict(questions_from_file(payload))


def _load_state(args) -> Any:
    if args.state is not None:
        return str(args.state)
    path = Path(str(args.state_file)).expanduser()
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read state file {path}: {exc}") from None


def _sniff_image_mime(raw: bytes) -> str:
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    raise ValueError("unsupported image format (use PNG, JPEG, or WebP)")


def _load_images(args) -> tuple[Any, ...]:
    from agent.judgment.types import JudgmentImage

    images = []
    for raw_path in getattr(args, "image", None) or []:
        path = Path(str(raw_path)).expanduser()
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise ValueError(f"cannot read image {path}: {exc}") from None
        if not raw:
            raise ValueError("image file is empty")
        images.append(JudgmentImage(
            data=base64.b64encode(raw).decode("ascii"),
            mime_type=_sniff_image_mime(raw)))
    return tuple(images)


def _format_answer(answer: Any) -> str:
    from agent.judgment.types import (
        BoxAnswer,
        ChoiceAnswer,
        NoulAnswer,
        ScoreAnswer,
    )

    if isinstance(answer, NoulAnswer):
        return f"noul {answer.noul:g}"
    if isinstance(answer, ChoiceAnswer):
        return f"choice {answer.choice} (confidence {answer.confidence:g})"
    if isinstance(answer, ScoreAnswer):
        return f"score {answer.score} (confidence {answer.confidence:g})"
    if isinstance(answer, BoxAnswer):
        box = answer.box
        return (f"box top={box.top:g} bottom={box.bottom:g} "
                f"left={box.left:g} right={box.right:g}")
    return str(answer)


def _usage_cost(result: Any) -> float | None:
    if getattr(result, "cost_usd", None) is not None:
        return float(result.cost_usd)
    usage = result.usage
    if not (usage.input_tokens or usage.output_tokens):
        return None
    try:
        from agent.usage_pricing import estimate_usage_cost

        estimate = estimate_usage_cost(result.model, usage, provider=result.provider)
        return float(estimate.amount_usd) if estimate.amount_usd is not None else None
    except Exception:
        logger.debug("judgment usage cost estimation failed", exc_info=True)
        return None


def _cmd_ask(args) -> int:
    from agent.judgment import (
        JudgmentError,
        JudgmentRequest,
        JudgmentUnavailable,
        JudgmentValidationError,
        answer_to_json,
        resolve_judge,
    )

    try:
        questions = _load_questions(args.questions_file)
        state = _load_state(args)
        request = JudgmentRequest(state=state, questions=questions, images=_load_images(args))
        result = resolve_judge(purpose=args.purpose).judge(
            request, purpose=args.purpose)
    except (ValueError, JudgmentValidationError, FileNotFoundError) as exc:
        if getattr(args, "json", False):
            print(json.dumps({"success": False, "error": str(exc)}))
        else:
            print(f"Error: {exc}")
        return 2
    except JudgmentUnavailable as exc:
        if getattr(args, "json", False):
            print(json.dumps({"success": False, "error": str(exc)}))
        else:
            print(f"Judgment unavailable: {exc}")
        return 1
    except JudgmentError as exc:
        if getattr(args, "json", False):
            print(json.dumps({"success": False, "error": str(exc)}))
        else:
            print(f"Judgment failed: {exc}")
        return 1

    cost = _usage_cost(result)
    if getattr(args, "json", False):
        print(json.dumps({
            "success": True,
            "api": result.api,
            "provider": result.provider,
            "model": result.model,
            "answers": {
                question_id: answer_to_json(answer)
                for question_id, answer in result.answers.items()
            },
            "usage": {
                "input_tokens": result.usage.input_tokens,
                "output_tokens": result.usage.output_tokens,
                "cost_usd": cost,
            },
            "cache_hit_questions": result.cached_questions,
        }, ensure_ascii=False, indent=2))
        return 0

    print(f"api={result.api} provider={result.provider} model={result.model}")
    for question_id, answer in result.answers.items():
        print(f"{question_id}: {_format_answer(answer)}")
    cost_text = f"${cost:.4f}" if cost is not None else "n/a"
    print(f"usage: input={result.usage.input_tokens} "
          f"output={result.usage.output_tokens} cost={cost_text}")
    print(f"cache: {result.cached_questions}/{len(result.answers)} hit")
    return 0


def _text_images_capability(settings: Any) -> bool:
    from agent.judgment.chat import ChatTextBackend

    try:
        return bool(ChatTextBackend(settings).supports_images)
    except Exception:  # health: allow BLE001 -- doctor must not fail on capability probing
        return False


def _cmd_doctor(args) -> int:
    from agent.judgment.cache import judgment_home, shared_cache
    from agent.judgment.chain import doctor_routes
    from agent.judgment.config import (
        auxiliary_judge_config,
        is_local_provider,
        load_judgment_settings,
    )

    settings = load_judgment_settings()
    aux = auxiliary_judge_config()
    routes = doctor_routes()
    cache = shared_cache()
    home = judgment_home()
    eval_dir = home / "eval"
    eval_bytes = 0
    if eval_dir.exists():
        for path in eval_dir.rglob("*"):
            if path.is_file():
                eval_bytes += path.stat().st_size

    local_endpoint: dict[str, Any] | None = None
    provider = str(aux.get("provider") or "auto")
    if is_local_provider(provider):
        try:
            from hermes_cli.config import load_config
            from hermes_cli.local_runtime.endpoint import resolve_llamacpp_endpoint

            endpoint = resolve_llamacpp_endpoint(config=load_config(), wait_for_boot_s=0)
            local_endpoint = {
                "base_url": str((endpoint or {}).get("base_url") or ""),
                "available": bool((endpoint or {}).get("base_url")),
            }
        except Exception as exc:
            logger.debug("judgment doctor local endpoint probe failed", exc_info=True)
            local_endpoint = {"base_url": "", "available": False, "error": str(exc)}

    payload = {
        "mode": settings.mode,
        "text_after_native": settings.text_after_native,
        "routes": routes,
        "text": {
            "provider": provider,
            "model": str(aux.get("model") or ""),
            "base_url": str(aux.get("base_url") or ""),
            "images": _text_images_capability(settings),
            "local_endpoint": local_endpoint,
        },
        "tools": {
            "enabled": settings.tools_enabled,
            "reachable_with_native": bool(routes) and any(row["available"] for row in routes),
        },
        "images": settings.images,
        "cache": {
            "path": str(cache.db_path) if cache is not None else str(home / "judgment.db"),
            "available": bool(cache and cache.available),
            "reason": cache.disabled_reason if cache is not None else "store unavailable",
            "max_states": settings.cache_max_states,
        },
        "audit": {
            "enabled": settings.audit,
            "runs": cache.run_count() if cache is not None else 0,
            "max_runs": settings.audit_max_runs,
        },
        "eval": {
            "path": str(eval_dir),
            "bytes": eval_bytes,
            "max_bytes": settings.eval_max_bytes,
        },
        "problems": list(settings.problems),
    }

    if getattr(args, "json", False):
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print(f"mode: {payload['mode']}  (text_after_native={payload['text_after_native']})")
    if not routes:
        print("native routes: none configured (judgment.native is empty)")
    else:
        print("native routes:")
        for row in routes:
            state = "available" if row["available"] else f"unavailable: {row['reason']}"
            media = []
            if row["supports_images"]:
                media.append("images")
            if row["supports_boxes"]:
                media.append("box")
            suffix = f"  [{'+'.join(media)}]" if media else ""
            print(f"  {row['api']}:{row['provider']}/{row['model']}  {state}{suffix}")
            if row["base_url"]:
                print(f"    base_url: {row['base_url']}")
    text = payload["text"]
    text_info: dict[str, Any] = text if isinstance(text, dict) else {}
    print(f"text route: {text_info.get('provider')}/{text_info.get('model') or 'auto'}")
    local = text_info.get("local_endpoint")
    if isinstance(local, dict):
        print(f"  local endpoint: {local.get('base_url') or 'not running'}")
    print(f"tools: judgment.tools.enabled={payload['tools']['enabled']} "
          f"(native reachable: {payload['tools']['reachable_with_native']})")
    image_native = sum(1 for row in routes if row["supports_images"])
    print(f"images: {'enabled' if payload['images'] else 'disabled'}  "
          f"(native routes: {image_native}; text: "
          f"{'on' if text_info.get('images') else 'off'})")
    cache_info = payload["cache"]
    audit = payload["audit"]
    cache_view: dict[str, Any] = cache_info if isinstance(cache_info, dict) else {}
    audit_view: dict[str, Any] = audit if isinstance(audit, dict) else {}
    reason = str(cache_view.get("reason") or "")
    print(f"cache: {cache_view.get('path')} "
          f"({'available' if cache_view.get('available') else 'unavailable: ' + reason})")
    print(f"audit: {'on' if audit_view.get('enabled') else 'off'}  "
          f"runs={audit_view.get('runs')}")
    print(f"eval: {payload['eval']['path']}  {payload['eval']['bytes']} bytes")
    for problem in payload["problems"]:
        print(f"config: {problem}")
    return 0


def _format_ts(epoch: Any) -> str:
    try:
        return datetime.fromtimestamp(int(epoch), tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError, OSError):
        return str(epoch)


def _cmd_log(args) -> int:
    from agent.judgment.cache import shared_cache

    cache = shared_cache()
    if cache is None or not cache.available:
        message = cache.disabled_reason if cache is not None else "judgment store unavailable"
        if getattr(args, "json", False):
            print(json.dumps({"success": False, "error": message}))
        else:
            print(f"Judgment store unavailable: {message}")
        return 1
    rows = cache.recent_runs(
        limit=max(1, int(args.limit)), purpose=args.purpose, status=args.status)
    if getattr(args, "json", False):
        print(json.dumps({"success": True, "runs": rows}, ensure_ascii=False, indent=2))
        return 0
    if not rows:
        print("No judgment runs recorded.")
        return 0
    for row in rows:
        model = f"{row['api']}:{row['provider']}/{row['model']}"
        cached = f"cached={row['cached']}/{row['questions']}"
        tokens = f"{row['input_tokens']}in/{row['output_tokens']}out"
        cost = f"${row['cost_usd']:.4f}"
        latency = f"{row['latency_ms'] / 1000:.1f}s"
        line = (f"{_format_ts(row['created_at'])} {row['purpose'] or '-'} {model} "
                f"{row['status']} {cached} {tokens} {cost} {latency}")
        if row["error"]:
            line += f"  error={row['error']}"
        print(line)
    return 0


def cmd_judgment(args) -> int:
    """Dispatch ``hermes judgment <action>``."""
    action = getattr(args, "judgment_action", None) or "doctor"
    if action == "ask":
        return _cmd_ask(args)
    if action == "log":
        return _cmd_log(args)
    return _cmd_doctor(args)
