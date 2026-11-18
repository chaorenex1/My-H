"""Ordered title-model candidates (ADR-21 D3) and their one-call-per-candidate executor.

omp's title generator is not one auxiliary slot: it walks a role chain (``tiny`` → ``commit`` →
``smol``), recursively expands every configured retry chain, and appends the current session model
last. ``title_model_plan`` is that expansion here — it resolves each candidate into an explicit
route at plan time so ``call_llm`` never re-walks a chain, and ``run_title_plan`` spends at most
one call per candidate, in order, stopping at the first accepted ``<title>``.

This module is the stage-1 primitive only. Consumers (session titles today; task labels in a later
sprint) keep their own entry points and call in from there, which is why nothing imports this yet.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional

from agent.auxiliary_client import (
    _DEFAULT_AUX_TIMEOUT,
    _normalize_main_runtime,
    call_llm,
)
from agent.auxiliary_fallback_chains import (
    auxiliary_section,
    coerce_positive_timeout,
    model_fallback_chains_for_route,
)
from agent.auxiliary_task_config import _get_auxiliary_task_config
from agent.title_parser import parse_title_output

logger = logging.getLogger(__name__)

_TITLE_TASK = "title_generation"
# omp's role order (``collectOnlineTinyCandidates(["tiny", "commit", "smol"])``): cheap lane first,
# then the more capable roles. ``commit_message`` is this repo's slot for omp's ``commit`` role
# (``hermes_cli/aux_slots.py``).
_ROLE_TASKS: tuple[str, ...] = ("tiny", "commit_message", "smol")


@dataclass(frozen=True)
class TitleCandidate:
    """One explicit route in a title plan: what to call, for whose budget, with what deadline.

    ``task`` owns the call (limits, billing and logs attribute there), ``provider``/``model``/
    ``base_url`` are the resolved route — ``provider`` is never ``auto``, because the plan decides
    the route — and ``timeout`` is that task's ``auxiliary.<task>.timeout``.
    ``model`` may be empty: ``call_llm`` then resolves the provider's default aux model, then the
    main model, through its existing ladder. ``base_url`` is ``None`` when the route has none.
    """

    task: str
    provider: str
    model: str
    base_url: Optional[str]
    timeout: float


def _route_key(
    provider: str, model: str, base_url: Optional[str]
) -> tuple[str, str, str]:
    """Dedupe identity: the route triple, normalised the way the resolver compares routes."""
    return (
        (provider or "").strip().lower(),
        (model or "").strip(),
        (base_url or "").strip().rstrip("/"),
    )


def _route_fields(config: dict[str, Any]) -> tuple[str, str, Optional[str]]:
    """``(provider, model, base_url)`` of one ``auxiliary.<task>`` block; ``""``/``auto`` kept."""
    return (
        str(config.get("provider") or "").strip().lower(),
        str(config.get("model") or "").strip(),
        str(config.get("base_url") or "").strip() or None,
    )


def _task_timeout(config: dict[str, Any]) -> float:
    return coerce_positive_timeout(config.get("timeout")) or _DEFAULT_AUX_TIMEOUT


def _is_pinned(config: dict[str, Any]) -> bool:
    """True when a route field is deliberately set; ``provider: auto`` plus blanks is the default."""
    provider, model, base_url = _route_fields(config)
    return provider not in ("", "auto") or bool(
        model or base_url or str(config.get("api_key") or "").strip()
    )


def _title_slot_route(
    config: dict[str, Any], main_runtime: dict[str, Any]
) -> tuple[str, str, Optional[str]]:
    """The ``title_generation`` slot's explicit route.

    A pinned provider keeps its own block verbatim — an explicit override must never borrow the
    session's model or endpoint. Only an ``auto``/empty provider is backfilled from
    ``main_runtime``, and only for the fields the slot left empty.
    """
    provider, model, base_url = _route_fields(config)
    if provider not in ("", "auto"):
        return provider, model, base_url
    return (
        str(main_runtime.get("provider") or "").strip(),
        model or str(main_runtime.get("model") or "").strip(),
        base_url or main_runtime.get("base_url") or None,
    )


class _Plan:
    """Depth-first candidate accumulator; ``seen`` is the dedupe key set that also cuts cycles."""

    def __init__(self, auxiliary: dict[str, Any]) -> None:
        self._auxiliary = auxiliary
        self._seen: set[tuple[str, str, str]] = set()
        self.candidates: list[TitleCandidate] = []

    def add(
        self,
        task: str,
        provider: str,
        model: str,
        base_url: Optional[str],
        timeout: float,
    ) -> None:
        """Append one explicit route (once) and expand the chains configured for it."""
        key = _route_key(provider, model, base_url)
        if key in self._seen:
            return
        self._seen.add(key)
        candidate = TitleCandidate(
            task=task,
            provider=provider,
            model=model,
            base_url=base_url,
            timeout=timeout,
        )
        self.candidates.append(candidate)
        self._expand(candidate)

    def _expand(self, owner: TitleCandidate) -> None:
        """The route's own retry lanes, most specific first: exact key, provider wildcard, task."""
        for _key, chain in model_fallback_chains_for_route(
            owner.provider, owner.model, auxiliary=self._auxiliary
        ):
            for entry in chain:
                self._add_entry(entry, owner)
        for entry in _get_auxiliary_task_config(owner.task).get("fallback_chain") or ():
            self._add_entry(entry, owner)

    def _add_entry(self, entry: Any, owner: TitleCandidate) -> None:
        """Add one chain entry, inheriting ``owner``'s task/timeout (the chain is not a new owner)."""
        if not isinstance(entry, dict):
            return
        provider = str(entry.get("provider") or "").strip().lower()
        model = str(entry.get("model") or "").strip()
        if not provider or not model:
            # Mirrors ``_resolve_fallback_entry``: a provider-only entry is marked tried but never
            # dialled, so planning it would add a call today's recovery walk never makes.
            return
        self.add(
            owner.task,
            provider,
            model,
            str(entry.get("base_url") or "").strip() or None,
            owner.timeout,
        )


def title_model_plan(
    main_runtime: Optional[dict[str, Any]] = None,
    *,
    require_model_upgrade: bool = True,
) -> list[TitleCandidate]:
    """Build the ordered, deduplicated title candidates for the live session (ADR-21 D3).

    Order: an explicit ``auxiliary.title_generation`` override (only when one of its route fields
    is pinned) → ``tiny`` → ``commit_message`` → ``smol`` → the current session model. Each
    candidate is expanded depth-first into explicit routes: the candidate itself, then
    ``auxiliary.fallback_chains[provider/model]``, then ``auxiliary.fallback_chains[provider/*]``,
    then ``auxiliary.<task>.fallback_chain``. Routes a chain adds are expanded the same way, so a
    self-reference costs nothing beyond its first visit.

    Dedupe key is the route triple ``(provider.lower(), model, base_url.rstrip("/"))`` — an
    equivalent route is planned once, keeping the candidate that reached it first and its owning
    task, so a role pinned to the session's own route absorbs the trailing current-model candidate.
    A chain entry inherits the task and timeout of the candidate that expanded it.

    Gating is evaluated per call (never at import): ``auxiliary.title_generation.enabled`` always
    feeds the plan, and ``model_upgrade_enabled`` does too unless the caller is an explicit naming
    request (``require_model_upgrade=False``, e.g. ``generate_title`` from ``hermes sessions
    retitle-skills``, which is not the automatic upgrade the toggle silences). Either gate off
    yields ``[]``. An empty plan also covers "nothing usable to call": roles gate on ``provider``
    alone (``auto``/empty means unconfigured, even with a model filled in) and the current-model
    candidate needs both a provider and a model from ``main_runtime``. ``[]`` means zero calls.
    """
    from agent.title_generator import _auto_title_enabled, _model_title_upgrade_enabled

    if not _auto_title_enabled():
        return []
    if require_model_upgrade and not _model_title_upgrade_enabled():
        return []
    title_config = _get_auxiliary_task_config(_TITLE_TASK)
    title_timeout = _task_timeout(title_config)
    runtime = _normalize_main_runtime(main_runtime)
    plan = _Plan(auxiliary_section())

    if _is_pinned(title_config):
        provider, model, base_url = _title_slot_route(title_config, runtime)
        if provider:
            plan.add(_TITLE_TASK, provider, model, base_url, title_timeout)

    for task in _ROLE_TASKS:
        config = _get_auxiliary_task_config(task)
        provider, model, base_url = _route_fields(config)
        if provider in ("", "auto"):
            continue
        plan.add(task, provider, model, base_url, _task_timeout(config))

    provider = str(runtime.get("provider") or "").strip()
    model = str(runtime.get("model") or "").strip()
    if provider and model:
        plan.add(_TITLE_TASK, provider, model, runtime.get("base_url"), title_timeout)
    return plan.candidates


def run_title_plan(
    plan: Iterable[TitleCandidate],
    messages: list[dict[str, Any]],
    *,
    call: Optional[Callable[..., Any]] = None,
    request_options: Optional[dict[str, Any]] = None,
) -> Optional[str]:
    """Run ``plan`` serially, one ``call_llm`` per candidate, and return the first accepted title.

    Every call carries the candidate's explicit route, its ``task`` (limits, billing and log
    attribution), its ``timeout`` and ``fallback=False``: the plan already expanded every
    configured chain, so the auxiliary client must not walk one a second time. A candidate that
    raises, answers empty, or answers unusably falls through to the next one; a terminal decline
    (``<title/>`` or ``<title>none</title>``, parsed by ``agent.title_parser``) ends the attempt
    with ``None``. No retries, no concurrency, no best-of — ``None`` leaves stage 0 in place.

    ``call`` is the transport seam (defaults to this module's ``call_llm``, so a test can stand in
    one recorder) and ``request_options`` are the caller's own request settings — the session
    titler's token ceiling, omitted temperature and disabled reasoning ride there rather than
    being re-stated per candidate.
    """
    invoke = call or call_llm
    options = dict(request_options or {})
    # A caller-supplied deadline (``generate_title(timeout=…)``) wins over the candidate's
    # ``auxiliary.<task>.timeout``: it is the caller's budget, not a route property.
    timeout_override = options.pop("timeout", None)
    for candidate in plan or ():
        try:
            response = invoke(
                task=candidate.task,
                provider=candidate.provider,
                model=candidate.model,
                base_url=candidate.base_url,
                messages=messages,
                timeout=timeout_override or candidate.timeout,
                fallback=False,
                **options,
            )
            parsed = parse_title_output(response.choices[0].message.content or "")
        except Exception as exc:  # health: allow BLE001 -- failure advances plan
            logger.warning(
                "Title candidate %s/%s failed: %s",
                candidate.provider,
                candidate.model or "<default>",
                exc,
            )
            continue
        if parsed.terminal:
            logger.debug(
                "Title model declined to name this turn (%s/%s); plan ends",
                candidate.provider,
                candidate.model or "<default>",
            )
            return None
        if parsed.title:
            return parsed.title
    return None
