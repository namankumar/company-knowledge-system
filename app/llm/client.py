"""
Single entry point for every LLM call. Routes the model, enforces the daily
budget, records a model_calls row with cost/latency/validation, and parses
JSON outputs.

Nothing else in the codebase should construct an Anthropic client.
"""
import json
import re
import time
import uuid
import asyncio
from datetime import date, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import structlog

from app.core.config import get_settings
from app.core.db import acquire, _current_tenant_id
from app.core.redis import get_redis
from app.core.tenant_settings import get_tenant_settings, llm_daily_budget_usd
from app.llm.catalog import MODEL_CATALOG, ModelSpec, pricing_spec_for_model
from app.llm.catalog import UnknownModelCostError
from app.llm.failures import FailureReason, ProviderCallError
from app.llm.fallback import CandidateOutput, RouteResult, run_capped_route
from app.llm.policies import TASK_POLICIES, get_task_policy
from app.llm.providers.baseten import BasetenAdapter
from app.llm.providers.deepinfra import DeepInfraAdapter
from app.llm.providers.groq import GroqAdapter
from app.llm.providers.lilac import LilacAdapter
from app.llm.providers.openai import OpenAIAdapter
from app.llm.providers.openrouter import OpenRouterAdapter
from app.llm.providers.anthropic import AnthropicAdapter
from app.llm.providers.claude_cli import ClaudeCliAdapter
from app.llm.providers.base import ProviderAdapter, ProviderRequest
from app.llm.providers.wafer import WaferAdapter
from app.llm.router import ROUTE_POLICY_VERSION, resolve_model_override

logger = structlog.get_logger()

INFERENCEHQ_CHIEF_AGENT_SYSTEM_PROMPT = (
    "You are InferenceHQ's chief agent. You are world-class at execution. "
    "Help with planning as much as you can while still following the task's "
    "specific output format, schema, citation, and evidence instructions."
)


class BudgetExhausted(Exception):
    pass


class AnthropicNotAllowedError(Exception):
    """A policy resolution (no explicit override) tried to select an Anthropic
    model. Anthropic may only be reached via an explicit model_override
    (MODEL_OVERRIDE_GLOBAL, MODEL_OVERRIDE_TASKS, or a tenant model_override).
    Deliberately NOT a ProviderCallError subclass: several call sites catch
    ProviderCallError to degrade gracefully, and a misconfigured policy must
    surface loudly instead of being silently swallowed into a fallback path.
    """

    def __init__(self, task: str, model: str) -> None:
        super().__init__(
            f"Task {task!r} resolved to Anthropic model {model!r} without an "
            "explicit override. Anthropic can only be selected via "
            "MODEL_OVERRIDE_GLOBAL, MODEL_OVERRIDE_TASKS, or a tenant "
            "model_override."
        )
        self.task = task
        self.model = model


def _assert_no_anthropic(aliases: tuple[str, ...], task: str) -> None:
    for alias in aliases:
        spec = MODEL_CATALOG[alias]
        if spec.provider == "anthropic":
            raise AnthropicNotAllowedError(task, spec.model)


def _budget_day_value(tenant_settings: dict | None) -> date:
    tz_name = (tenant_settings or {}).get("timezone") or (tenant_settings or {}).get("brief_timezone") or "UTC"
    try:
        return datetime.now(ZoneInfo(tz_name)).date()
    except ZoneInfoNotFoundError:
        return date.today()


def _budget_day(tenant_settings: dict | None) -> str:
    return _budget_day_value(tenant_settings).isoformat()


async def _db_spend_today(tenant_id: str | None, tenant_settings: dict | None = None) -> float:
    if tenant_id and _current_tenant_id.get() is None:
        _current_tenant_id.set(tenant_id)
    tz_name = (tenant_settings or {}).get("timezone") or (tenant_settings or {}).get("brief_timezone") or "UTC"
    async with acquire() as conn:
        return float(await conn.fetchval(
            """
            SELECT COALESCE(sum(cost_usd), 0)
            FROM model_calls
            WHERE ($1::uuid IS NULL OR tenant_id = $1::uuid)
              AND (created_at AT TIME ZONE $2)::date = $3::date
            """,
            tenant_id, tz_name, _budget_day_value(tenant_settings),
        ) or 0)


async def _seeded_spend(redis, key: str, tenant_id: str | None, tenant_settings: dict | None = None) -> float:
    cached = await redis.get(key)
    if cached is not None:
        return float(cached or 0)
    spent = await _db_spend_today(tenant_id, tenant_settings)
    await redis.set(key, spent, ex=172800)
    return spent


async def _reserve_budget(
    tenant_id: str | None,
    estimated_cost_usd: float,
    *,
    daily_budget_usd: float,
    tenant_settings: dict | None = None,
) -> float:
    """Atomically reserve estimated spend before making the provider call."""
    redis = await get_redis()
    key = f"llm_budget:{tenant_id or 'global'}:{_budget_day(tenant_settings)}"
    spent = await _seeded_spend(redis, key, tenant_id, tenant_settings)
    if daily_budget_usd >= 0 and spent >= daily_budget_usd:
        raise BudgetExhausted(f"Daily LLM budget exhausted: ${spent:.2f} spent")
    if estimated_cost_usd <= 0:
        return 0.0
    next_spend = float(await redis.incrbyfloat(key, estimated_cost_usd))
    await redis.expire(key, 172800)
    if daily_budget_usd >= 0 and next_spend > daily_budget_usd:
        await redis.incrbyfloat(key, -estimated_cost_usd)
        await redis.expire(key, 172800)  # 2 days
        raise BudgetExhausted(
            f"Daily LLM budget would be exceeded: ${next_spend:.2f} > ${daily_budget_usd:.2f}"
        )
    return estimated_cost_usd


async def _adjust_budget(
    tenant_id: str | None,
    delta_cost_usd: float,
    *,
    tenant_settings: dict | None = None,
) -> None:
    if not delta_cost_usd:
        return
    redis = await get_redis()
    key = f"llm_budget:{tenant_id or 'global'}:{_budget_day(tenant_settings)}"
    await redis.incrbyfloat(key, delta_cost_usd)
    await redis.expire(key, 172800)


async def _record_model_call(
    *,
    tenant_id: str | None,
    task: str,
    provider: str,
    model: str,
    override_source: str,
    prompt_version: str | None,
    context_pack_id: str | None,
    request_body: str,
    response_body: str,
    parsed,
    validation_status: str,
    input_tokens: int,
    output_tokens: int,
    cost_usd: float,
    latency_ms: int,
    pipeline_run_id: str | None,
    logical_call_id: str,
    attempt_index: int,
    fallback_reason: str | None,
    cache_status: str | None,
    route_policy_version: str,
    cache_creation_input_tokens: int = 0,
    cache_read_input_tokens: int = 0,
) -> str:
    model_call_id = str(uuid.uuid4())
    async with acquire() as conn:
        await conn.execute(
            """
            INSERT INTO model_calls (
                id, tenant_id, task, provider, model, override_source, prompt_version,
                context_pack_id, request_body, response_body, parsed,
                validation_status, input_tokens, output_tokens, cost_usd, latency_ms, pipeline_run_id,
                logical_call_id, attempt_index, fallback_reason, cache_status, route_policy_version,
                cache_creation_input_tokens, cache_read_input_tokens
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11::jsonb,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21,$22,$23,$24)
            """,
            model_call_id, tenant_id, task, provider, model, override_source,
            prompt_version, context_pack_id,
            request_body, response_body,
            json.dumps(parsed) if parsed is not None else None,
            validation_status,
            input_tokens, output_tokens,
            round(cost_usd, 6), latency_ms, pipeline_run_id,
            logical_call_id, attempt_index, fallback_reason, cache_status, route_policy_version,
            cache_creation_input_tokens, cache_read_input_tokens,
        )
    return model_call_id


async def call_llm(
    *,
    task: str,
    prompt: str,
    tenant_id: str | None = None,
    tenant_settings: dict | None = None,
    system: str | None = None,
    max_tokens: int = 2048,
    expect_json: bool = True,
    prompt_version: str | None = None,
    context_pack_id: str | None = None,
    pipeline_run_id: str | None = None,
    logical_call_id: str | None = None,
    attempt_index: int = 0,
    fallback_reason: str | None = None,
    cache_status: str | None = "uncached",
    route_policy_version: str | None = None,
    validation_pack_items: list[dict] | None = None,
    prompt_has_content: bool = False,
    expected_shape: dict[str, type] | None = None,
) -> dict:
    """
    Returns {"parsed": dict|list|None, "text": str, "model_call_id": str}.
    parsed is None when expect_json is True but the output failed to parse.
    """
    if attempt_index < 0:
        raise ValueError("attempt_index must be non-negative")

    settings = get_settings()
    logical_call_id = logical_call_id or str(uuid.uuid4())
    if tenant_id and tenant_settings is None:
        tenant_settings = await get_tenant_settings(tenant_id)
    daily_budget = llm_daily_budget_usd(
        tenant_settings or {},
        fallback=settings.llm_daily_budget_usd,
    )
    system = _with_inferencehq_chief_agent_system(system)

    if settings.llm_fixture_mode:
        return await _call_llm_fixture(
            task=task,
            prompt=prompt,
            tenant_id=tenant_id,
            tenant_settings=tenant_settings,
            daily_budget=daily_budget,
            prompt_version=prompt_version,
            context_pack_id=context_pack_id,
            pipeline_run_id=pipeline_run_id,
            logical_call_id=logical_call_id,
            attempt_index=attempt_index,
            fallback_reason=fallback_reason,
            cache_status=cache_status,
            expect_json=expect_json,
        )

    if task not in TASK_POLICIES:
        raise ProviderCallError("policy_blocked", f"No TaskPolicy registered for task {task!r}")

    return await _call_llm_routed(
        task=task,
        prompt=prompt,
        tenant_id=tenant_id,
        tenant_settings=tenant_settings,
        system=system,
        max_tokens=max_tokens,
        expect_json=expect_json,
        prompt_version=prompt_version,
        context_pack_id=context_pack_id,
        pipeline_run_id=pipeline_run_id,
        logical_call_id=logical_call_id,
        cache_status=cache_status,
        route_policy_version=route_policy_version or ROUTE_POLICY_VERSION,
        validation_pack_items=validation_pack_items,
        prompt_has_content=prompt_has_content,
        expected_shape=expected_shape,
        daily_budget=daily_budget,
    )


async def _call_llm_fixture(
    *,
    task: str,
    prompt: str,
    tenant_id: str | None,
    tenant_settings: dict | None,
    daily_budget: float,
    prompt_version: str | None,
    context_pack_id: str | None,
    pipeline_run_id: str | None,
    logical_call_id: str,
    attempt_index: int,
    fallback_reason: str | None,
    cache_status: str | None,
    expect_json: bool,
) -> dict:
    """Deterministic fixture path for tests. Never available in production
    (Settings.fixture_model_is_test_only enforces APP_ENV=test)."""
    reserved_cost = await _reserve_budget(
        tenant_id, 0.0, daily_budget_usd=daily_budget, tenant_settings=tenant_settings,
    )
    try:
        start = time.perf_counter()
        provider = "fixture"
        model = "fixture-v1"
        override_source = "test_fixture"
        text = _fixture_response(task, prompt)
        input_tokens = max(1, len(prompt) // 4)
        output_tokens = max(1, len(text) // 4)
        cost = 0.0
        latency_ms = int((time.perf_counter() - start) * 1000)
    except BaseException:
        await _adjust_budget(tenant_id, -reserved_cost, tenant_settings=tenant_settings)
        raise
    await _adjust_budget(tenant_id, cost - reserved_cost, tenant_settings=tenant_settings)

    parsed = None
    validation_status = "n/a"
    if expect_json:
        parsed = _parse_json(text)
        validation_status = "valid" if parsed is not None else "unparsed"

    route_policy_version = "fixture-v1"
    model_call_id = await _record_model_call(
        tenant_id=tenant_id,
        task=task,
        provider=provider,
        model=model,
        override_source=override_source,
        prompt_version=prompt_version,
        context_pack_id=context_pack_id,
        request_body=prompt,
        response_body=text,
        parsed=parsed,
        validation_status=validation_status,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost,
        latency_ms=latency_ms,
        pipeline_run_id=pipeline_run_id,
        logical_call_id=logical_call_id,
        attempt_index=attempt_index,
        fallback_reason=fallback_reason,
        cache_status=cache_status,
        route_policy_version=route_policy_version,
    )

    logger.info(
        "llm_call", task=task, model=model, override=override_source,
        cost_usd=round(cost, 5), latency_ms=latency_ms, validation=validation_status,
        logical_call_id=logical_call_id, attempt_index=attempt_index,
        fallback_reason=fallback_reason, cache_status=cache_status,
        route_policy_version=route_policy_version,
    )
    return {
        "parsed": parsed,
        "text": text,
        "model_call_id": model_call_id,
        "logical_call_id": logical_call_id,
        "attempt_index": attempt_index,
        "fallback_reason": fallback_reason,
        "cache_status": cache_status,
        "route_policy_version": route_policy_version,
    }


def _with_inferencehq_chief_agent_system(system: str | None) -> str:
    if system and system.strip():
        return f"{INFERENCEHQ_CHIEF_AGENT_SYSTEM_PROMPT}\n\n{system.strip()}"
    return INFERENCEHQ_CHIEF_AGENT_SYSTEM_PROMPT


async def _call_llm_routed(
    *,
    task: str,
    prompt: str,
    tenant_id: str | None,
    tenant_settings: dict | None,
    system: str | None,
    max_tokens: int,
    expect_json: bool,
    prompt_version: str | None,
    context_pack_id: str | None,
    pipeline_run_id: str | None,
    logical_call_id: str,
    cache_status: str | None,
    route_policy_version: str,
    validation_pack_items: list[dict] | None,
    prompt_has_content: bool,
    expected_shape: dict[str, type] | None,
    daily_budget: float,
) -> dict:
    settings = get_settings()
    policy = get_task_policy(task)
    aliases, override_source = _resolve_candidate_aliases(task, tenant_settings, policy)

    reserved_cost = await _reserve_budget(
        tenant_id,
        policy.max_cost_per_call_usd,
        daily_budget_usd=daily_budget,
        tenant_settings=tenant_settings,
    )

    async def call_candidate(alias: str, attempt_index: int, reason: FailureReason | None) -> CandidateOutput:
        spec = MODEL_CATALOG[alias]
        adapter = _provider_adapter_for_model(spec, settings)
        try:
            response = await asyncio.wait_for(
                adapter.complete(
                    ProviderRequest(
                        model=spec.model,
                        prompt=prompt,
                        system=system,
                        max_tokens=max_tokens,
                    )
                ),
                timeout=policy.timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            raise ProviderCallError("timeout", f"Provider attempt timed out for {alias}", original=exc) from exc
        parsed = _parse_json(response.text) if expect_json else None
        return CandidateOutput(
            alias=alias,
            provider=response.provider,
            model=response.model,
            text=response.text,
            parsed=parsed,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            cost_usd=spec.estimated_cost(
                response.input_tokens,
                response.output_tokens,
                cache_creation_input_tokens=response.cache_creation_input_tokens,
                cache_read_input_tokens=response.cache_read_input_tokens,
            ),
            latency_ms=response.latency_ms,
            cache_status=response.cache_status,
            cache_creation_input_tokens=response.cache_creation_input_tokens,
            cache_read_input_tokens=response.cache_read_input_tokens,
        )

    try:
        route = await run_capped_route(
            task=task,
            aliases=aliases,
            call_candidate=call_candidate,
            max_attempts=_effective_max_attempts(task, policy.max_attempts, aliases),
            max_cost_usd=policy.max_cost_per_call_usd,
            pack_items=validation_pack_items or [],
            prompt_has_content=prompt_has_content,
            expected_shape=expected_shape or {},
        )
    except BaseException:
        await _adjust_budget(tenant_id, -reserved_cost, tenant_settings=tenant_settings)
        raise

    actual_cost = sum(attempt.cost_usd for attempt in route.attempts)
    await _adjust_budget(tenant_id, actual_cost - reserved_cost, tenant_settings=tenant_settings)
    model_call_ids = await _record_route_attempts(
        route=route,
        task=task,
        prompt=prompt,
        tenant_id=tenant_id,
        prompt_version=prompt_version,
        context_pack_id=context_pack_id,
        pipeline_run_id=pipeline_run_id,
        logical_call_id=logical_call_id,
        cache_status=cache_status,
        route_policy_version=route_policy_version,
        expect_json=expect_json,
        override_source=override_source,
    )

    valid_attempt = next((attempt for attempt in route.attempts if attempt.validation and attempt.validation.valid), None)
    if valid_attempt and valid_attempt.output:
        model_call_id = model_call_ids[valid_attempt.attempt_index]
        output = valid_attempt.output
        logger.info(
            "llm_call", task=task, model=output.model, override=override_source,
            cost_usd=round(output.cost_usd, 5), latency_ms=output.latency_ms,
            validation="valid", logical_call_id=logical_call_id,
            attempt_index=valid_attempt.attempt_index,
            fallback_reason=valid_attempt.fallback_reason, cache_status=output.cache_status or cache_status,
            route_policy_version=route_policy_version,
        )
        return {
            "parsed": output.parsed,
            "text": output.text,
            "model_call_id": model_call_id,
            "logical_call_id": logical_call_id,
            "attempt_index": valid_attempt.attempt_index,
            "fallback_reason": valid_attempt.fallback_reason,
            "cache_status": output.cache_status or cache_status,
            "route_policy_version": route_policy_version,
        }

    reason = route.stopped_reason or (route.attempts[-1].error_reason if route.attempts else None) or "provider_error"
    if reason == "budget_exhausted":
        raise BudgetExhausted("LLM router logical-call budget exhausted")
    raise ProviderCallError(reason, f"LLM router failed for task {task}")


async def _record_route_attempts(
    *,
    route: RouteResult,
    task: str,
    prompt: str,
    tenant_id: str | None,
    prompt_version: str | None,
    context_pack_id: str | None,
    pipeline_run_id: str | None,
    logical_call_id: str,
    cache_status: str | None,
    route_policy_version: str,
    expect_json: bool,
    override_source: str = "policy",
) -> dict[int, str]:
    ids: dict[int, str] = {}
    for attempt in route.attempts:
        spec = MODEL_CATALOG[attempt.alias]
        output = attempt.output
        parsed = output.parsed if output else None
        text = output.text if output else ""
        if attempt.validation and attempt.validation.valid:
            validation_status = "valid"
        elif attempt.error_reason:
            validation_status = attempt.error_reason
        elif expect_json and parsed is None:
            validation_status = "unparsed"
        else:
            validation_status = "invalid"
        ids[attempt.attempt_index] = await _record_model_call(
            tenant_id=tenant_id,
            task=task,
            provider=output.provider if output else spec.provider,
            model=output.model if output else spec.model,
            override_source=override_source,
            prompt_version=prompt_version,
            context_pack_id=context_pack_id,
            request_body=prompt,
            response_body=text,
            parsed=parsed,
            validation_status=validation_status,
            input_tokens=output.input_tokens if output else 0,
            output_tokens=output.output_tokens if output else 0,
            cost_usd=attempt.cost_usd,
            latency_ms=output.latency_ms if output else 0,
            pipeline_run_id=pipeline_run_id,
            logical_call_id=logical_call_id,
            attempt_index=attempt.attempt_index,
            fallback_reason=attempt.fallback_reason,
            cache_status=(output.cache_status if output else None) or cache_status,
            route_policy_version=route_policy_version,
            cache_creation_input_tokens=output.cache_creation_input_tokens if output else 0,
            cache_read_input_tokens=output.cache_read_input_tokens if output else 0,
        )
    return ids


def _resolve_candidate_aliases(
    task: str,
    tenant_settings: dict | None,
    policy,
) -> tuple[tuple[str, ...], str]:
    """The only two ways to pick a model: an explicit override, or the task's
    TaskPolicy chain. A policy chain resolution may never land on Anthropic --
    that only happens through an explicit override naming an Anthropic model."""
    override = resolve_model_override(task, tenant_settings)
    settings = get_settings()
    if override is not None:
        try:
            spec = pricing_spec_for_model(override.model)
        except UnknownModelCostError as exc:
            logger.error(
                "invalid_model_override",
                task=task,
                model=override.model,
                override_source=override.override_source,
                error=str(exc),
            )
        else:
            if spec.provider == "claude_cli":
                if not settings.claude_cli_enabled or task != "chat":
                    logger.error(
                        "invalid_model_override",
                        task=task,
                        model=override.model,
                        override_source=override.override_source,
                        error="claude_cli override is only allowed for local chat when CLAUDE_CLI_ENABLED=true",
                    )
                else:
                    return (spec.alias,), override.override_source
            else:
                return (spec.alias,), override.override_source

    aliases = _dedupe_aliases(policy.production_candidates + policy.fallback_aliases)
    if task == "chat" and settings.claude_cli_enabled and settings.claude_cli_chat_first:
        aliases = _dedupe_aliases(("claude_cli_sonnet",) + aliases)
    _assert_no_anthropic(aliases, task)
    return aliases, "policy"


def _dedupe_aliases(aliases: tuple[str, ...]) -> tuple[str, ...]:
    out = []
    for alias in aliases:
        if alias not in out:
            out.append(alias)
    return tuple(out)


def _effective_max_attempts(task: str, base_max_attempts: int, aliases: tuple[str, ...]) -> int:
    if task == "chat" and aliases and aliases[0] == "claude_cli_sonnet":
        return max(base_max_attempts + 1, len(aliases))
    return base_max_attempts


def _provider_adapter_for_model(spec: ModelSpec, settings) -> ProviderAdapter:
    if spec.provider == "anthropic":
        return AnthropicAdapter(api_key=settings.anthropic_api_key)
    if spec.provider == "claude_cli":
        return ClaudeCliAdapter(
            command=settings.claude_cli_command,
            runtime_home=settings.claude_cli_home,
        )
    if spec.provider == "groq":
        return GroqAdapter(api_key=settings.groq_api_key, base_url=settings.groq_base_url)
    if spec.provider == "deepinfra":
        return DeepInfraAdapter(api_key=settings.deepinfra_api_key, base_url=settings.deepinfra_base_url)
    if spec.provider == "lilac":
        return LilacAdapter(api_key=settings.lilac_api_key, base_url=settings.lilac_base_url)
    if spec.provider == "wafer":
        return WaferAdapter(
            api_key=settings.wafer_api_key,
            base_url=settings.wafer_base_url,
            zdr_required=settings.wafer_zdr_required,
        )
    if spec.provider == "baseten":
        return BasetenAdapter(api_key=settings.baseten_api_key, base_url=settings.baseten_base_url)
    if spec.provider == "openrouter":
        return OpenRouterAdapter(
            api_key=settings.openrouter_api_key,
            base_url=settings.openrouter_base_url,
            extra_body=spec.extra_body,
        )
    if spec.provider == "openai":
        return OpenAIAdapter(api_key=settings.openai_api_key)
    raise ProviderCallError("policy_blocked", f"Unsupported provider: {spec.provider}")


def _parse_json(raw: str):
    text = raw.strip()
    if text.lower() in ("null", "none", ""):
        return None
    if "```" in text:
        # take the first fenced block
        parts = text.split("```")
        if len(parts) >= 2:
            text = parts[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # last resort: find the outermost bracket pair
        for open_c, close_c in (("{", "}"), ("[", "]")):
            start, end = text.find(open_c), text.rfind(close_c)
            if start != -1 and end > start:
                try:
                    return json.loads(text[start : end + 1])
                except json.JSONDecodeError:
                    continue
    return None


def _fixture_response(task: str, prompt: str) -> str:
    """Deterministic model for acceptance tests. Never available in production."""
    lowered = prompt.lower()
    if task == "triage":
        urgent = (
            "have not arrived" in lowered
            or "earliest launch date" in lowered
            or "nobody has been assigned" in lowered
        )
        return json.dumps({
            "decision": "extract_and_synthesize" if urgent else "extract",
            "reason": "fixture drift signal" if urgent else "fixture durable context",
            "confidence": 1.0,
        })

    if task == "extraction":
        if "the api credentials have not arrived" in lowered:
            return json.dumps({
                "entities": [
                    {"type": "customer", "name": "Acme Radiology"},
                ],
                "objects": [
                    {
                        "object_type": "deadline",
                        "key_hint": "phoenix-launch-deadline",
                        "title": "Project Phoenix launch deadline",
                        "state": {"owner": None, "due_date": "2026-07-08", "status_detail": "delayed"},
                        "label": "stated",
                        "confidence": 1.0,
                        "quote": "Engineering now expects the earliest launch date to be July 8.",
                    },
                    {
                        "object_type": "commitment",
                        "key_hint": "acme-radiology-pilot",
                        "title": "Deliver Acme Radiology pilot",
                        "state": {"owner": None, "due_date": "2026-06-30", "status_detail": "at risk"},
                        "label": "stated",
                        "confidence": 1.0,
                        "quote": "Acme Radiology still expects the pilot on June 30.",
                    },
                    {
                        "object_type": "blocker",
                        "key_hint": "phoenix-api-credentials",
                        "title": "API credentials missing",
                        "state": {"owner": None, "due_date": None, "status_detail": "unresolved"},
                        "label": "stated",
                        "confidence": 1.0,
                        "quote": "The API credentials have not arrived.",
                    },
                    {
                        "object_type": "open_question",
                        "key_hint": "acme-notification-owner",
                        "title": "Who will notify Acme Radiology?",
                        "state": {"owner": None, "due_date": None, "status_detail": "unassigned"},
                        "label": "stated",
                        "confidence": 1.0,
                        "quote": "Nobody has been assigned to notify Acme.",
                    },
                ],
                "edges": [
                    {
                        "from_hint": "phoenix-api-credentials",
                        "to_hint": "phoenix-launch-deadline",
                        "edge_type": "blocks",
                    }
                ],
                "facts": [
                    {
                        "claim": "Project Phoenix cannot launch before July 8, 2026.",
                        "label": "stated",
                        "confidence": 1.0,
                        "about_hint": "phoenix-launch-deadline",
                        "quote": "Engineering now expects the earliest launch date to be July 8.",
                    }
                ],
            })
        return json.dumps({
            "entities": [
                {"type": "person", "name": "Maya"},
                {"type": "customer", "name": "Acme Radiology"},
            ],
            "objects": [
                {
                    "object_type": "project",
                    "key_hint": "project-phoenix",
                    "title": "Project Phoenix",
                    "state": {"owner": "Maya", "due_date": None, "status_detail": "on track"},
                    "label": "stated",
                    "confidence": 1.0,
                    "quote": "Project Phoenix",
                },
                {
                    "object_type": "deadline",
                    "key_hint": "phoenix-launch-deadline",
                    "title": "Project Phoenix launch deadline",
                    "state": {"owner": "Maya", "due_date": "2026-06-30", "status_detail": "on track"},
                    "label": "stated",
                    "confidence": 1.0,
                    "quote": "Launch deadline: June 30, 2026",
                },
                {
                    "object_type": "commitment",
                    "key_hint": "acme-radiology-pilot",
                    "title": "Deliver Acme Radiology pilot",
                    "state": {"owner": "Maya", "due_date": "2026-06-30", "status_detail": "on track"},
                    "label": "stated",
                    "confidence": 1.0,
                    "quote": "Commitment: Deliver the pilot by June 30, 2026",
                },
                {
                    "object_type": "blocker",
                    "key_hint": "phoenix-api-credentials",
                    "title": "API credentials dependency",
                    "state": {"owner": None, "due_date": "2026-06-20", "status_detail": "expected"},
                    "label": "stated",
                    "confidence": 1.0,
                    "quote": "Dependency: API credentials must arrive by June 20, 2026",
                },
            ],
            "edges": [
                {
                    "from_hint": "Maya",
                    "to_hint": "project-phoenix",
                    "edge_type": "owns",
                },
                {
                    "from_hint": "Acme Radiology",
                    "to_hint": "acme-radiology-pilot",
                    "edge_type": "has_commitment",
                },
                {
                    "from_hint": "project-phoenix",
                    "to_hint": "phoenix-launch-deadline",
                    "edge_type": "has_deadline",
                },
                {
                    "from_hint": "phoenix-api-credentials",
                    "to_hint": "phoenix-launch-deadline",
                    "edge_type": "blocks",
                },
            ],
            "facts": [
                {
                    "claim": "Project Phoenix is currently on track.",
                    "label": "stated",
                    "confidence": 1.0,
                    "about_hint": "project-phoenix",
                    "quote": "Current status: On track",
                }
            ],
        })

    if task == "drift":
        source_match = re.search(
            r'<source ref="([^"]+)"[^>]*>\s*(.*?)\s*</source>',
            prompt,
            re.DOTALL,
        )
        if source_match and "the api credentials have not arrived" in lowered:
            ref, source_text = source_match.groups()
            quote = "The API credentials have not arrived."
            if quote.lower() not in source_text.lower():
                quote = source_text.strip()[:160]
            return json.dumps({
                "decision": "alert",
                "drift_type": "commitment_risk",
                "severity": "high",
                "confidence": 1.0,
                "claim": "Project Phoenix cannot meet Acme Radiology's June 30 pilot commitment.",
                "why_now": "The dependency is missing and engineering moved the earliest launch to July 8.",
                "evidence": [{"ref": ref, "quote": quote}],
                "missing_evidence": [],
                "recommended_action": "Assign an owner to notify Acme Radiology today.",
                "related": {
                    "people": [],
                    "projects": ["Project Phoenix"],
                    "commitments": ["Acme Radiology pilot"],
                },
            })
        return json.dumps({
            "decision": "suppress",
            "drift_type": "other",
            "severity": "low",
            "confidence": 1.0,
            "claim": "No material drift.",
            "why_now": "Fixture found no drift signal.",
            "evidence": [],
            "missing_evidence": [],
            "recommended_action": "none",
            "related": {"people": [], "projects": [], "commitments": []},
        })

    if task == "chat":
        source_match = re.search(r'<source ref="([^"]+)"[^>]*>', prompt)
        if not source_match:
            return "The available company sources do not contain the answer.\n\nSources used:\nMissing: company activity relevant to this question."
        ref = source_match.group(1)
        return f"Project Phoenix has a documented risk. [ref:{ref}]\n\nSources used:\nMissing: nothing material."

    if task == "brief_convergence":
        refs = re.findall(r'<source ref="([^"]+)"', prompt)
        return json.dumps({
            "stories": [
                {"title": "Fixture story", "evidence_refs": refs, "notes": "fixture grouping"},
            ],
        })

    if task == "comms_draft":
        # Deterministic stub: echo back every literal [ref:kind:id] tag
        # already present in the assembled brief + tenant-context material
        # (app.synthesis.comms_draft embeds refs this way, the same
        # convention brief.py's _render_block_line uses), one claim per
        # distinct ref, in first-seen order. Proves the "reuse the
        # material's own refs, never invent one" shape deterministically,
        # without a live model call.
        seen: list[str] = []
        for ref in re.findall(r"\[ref:([A-Za-z][A-Za-z0-9_]*:[A-Za-z0-9_.:-]+)\]", prompt):
            if ref not in seen:
                seen.append(ref)
        return json.dumps({
            "claims": [{"text": f"Update covering {ref}.", "refs": [ref]} for ref in seen],
        })

    if task == "workstream_digest_draft":
        refs = re.findall(r'<source ref="([^"]+)"', prompt)
        evidence_lines = []
        for match in re.finditer(r'<source ref="([^"]+)"[^>]*>\n?(.*?)\n?</source>', prompt, re.S):
            evidence_lines.append(match.group(2).strip())
        summary = " ".join(line for line in evidence_lines[:2] if line).strip() or "Fixture digest summary."
        if len(summary.split()) < 45 and evidence_lines:
            summary = (
                summary
                + " Current state remains active, open items stay attached to the same workstream, "
                  "and the latest material change is captured in the cited evidence."
            )
        return json.dumps({
            "summary_text": summary,
            "evidence_refs": refs,
        })

    if task == "brief":
        refs = re.findall(r'<source ref="([^"]+)"', prompt)
        block = {"text": "Fixture brief content.", "refs": refs}
        return json.dumps({
            "stories": [
                {
                    "title": "Fixture story",
                    "leverage": "medium",
                    "focus_today": block,
                    "what_changed": block,
                    "blocked_or_at_risk": {"text": "No risk evidence found in the provided sources.", "refs": []},
                    "needs_decision": {"text": "No decision evidence found in the provided sources.", "refs": []},
                    "next_action": {"text": "No action recommended.", "refs": []},
                },
            ],
            "coverage": {"text": "Fixture coverage.", "refs": refs},
        })

    if task == "brief_weekly":
        refs = re.findall(r'<source ref="([^"]+)"', prompt)
        daily_refs = [ref for ref in refs if ref.startswith("daily_brief:")]
        okr_refs = [ref for ref in refs if ref.startswith("okr_context:")]
        anchor_refs = daily_refs[:1] or okr_refs[:1] or refs[:1]
        trends_text = (
            f"Insufficient history: only {len(daily_refs)} delivered daily briefs are available."
            if len(daily_refs) < 7
            else "Velocity held steady across the seven daily briefs."
        )
        return json.dumps({
            "sections": [
                {"title": "Two-week arc summary", "text": "Fixture two-week arc.", "refs": anchor_refs},
                {"title": "What shipped", "text": "Fixture shipped summary.", "refs": anchor_refs},
                {"title": "Trends", "text": trends_text, "refs": anchor_refs},
                {"title": "Next week", "text": "Fixture next-week plan.", "refs": anchor_refs},
            ],
            "coverage": {"text": "Fixture weekly coverage.", "refs": anchor_refs},
        })

    if task == "plan_actual":
        # Fixture mode has no ground truth for what "matches a plan" means,
        # so default to the common case (no drift synthesis needed): a
        # fully-consistent pack.
        return json.dumps({"mismatches": []})

    if task == "personalization_seed":
        refs = re.findall(r'<source ref="([^"]+)"', prompt)
        entry = [{"text": "Fixture priority", "refs": refs}] if refs else []
        return json.dumps({
            "priorities": entry,
            "watchlist": [],
            "important_people": [],
            "current_plan": {"text": "Fixture current plan.", "refs": refs} if refs else None,
        })

    if task == "tenant_context_seed":
        refs = re.findall(r'<source ref="([^"]+)"', prompt)
        entry = [{"text": "Fixture company priority", "refs": refs}] if refs else []
        return json.dumps({
            "priorities": entry,
            "active_narratives": [{"text": "Fixture narrative", "refs": refs}] if refs else [],
            "cadences": [{"text": "Monday operating review", "refs": refs}] if refs else [],
            "current_plan": {"text": "Fixture company plan.", "refs": refs} if refs else None,
        })

    if task == "tenant_context_update":
        refs = re.findall(r'<source ref="([^"]+)"', prompt)
        return json.dumps({
            "operations": [
                {
                    "op": "add",
                    "field": "active_narratives",
                    "entry": {"text": "Fixture update narrative", "refs": refs},
                }
            ] if refs else [],
        })

    if task == "workspace":
        return _fixture_workspace_response(prompt)

    if task == "session_state_maintain":
        return _fixture_session_state_response(prompt)

    if task == "ledger":
        return _fixture_ledger_response(prompt)

    return "{}"


def _fixture_session_state_response(prompt: str) -> str:
    """Deterministic fixture for Slice 3.3's "regenerate state" action.
    app.sessions.state renders each thread message as
    `<message ref="message:{id}">{role}: {text}</message>`; this fixture
    extracts those tags and sorts each message into factors/decisions/
    open_questions by a cheap keyword heuristic, always citing the
    message's own ref back -- same "no ground truth, degrade to a
    deterministic default" convention as plan_actual/personalization_seed's
    fixture branches above."""
    matches = re.findall(r'<message ref="(message:[^"]+)">(.*?)</message>', prompt, re.DOTALL)
    factors: list[dict] = []
    decisions: list[dict] = []
    open_questions: list[dict] = []
    for ref, text in matches:
        text = text.strip()
        if not text:
            continue
        lowered = text.lower()
        if "decided" in lowered or "decision:" in lowered:
            decisions.append({"text": text[:200], "decided_at": None, "source_refs": [ref]})
        elif text.rstrip().endswith("?"):
            open_questions.append({"text": text[:200], "raised_at": None, "source_refs": [ref]})
        else:
            factors.append({"text": text[:200], "source_refs": [ref]})
    return json.dumps({"factors": factors, "decisions": decisions, "open_questions": open_questions})


def _fixture_ledger_response(prompt: str) -> str:
    """Deterministic fixture for the judgment-ledger eval bucket (Track D,
    L5). `ledger` is not a registered TaskPolicy task (there is no
    call_llm(task="ledger") call site in production -- entries are drafted
    by the model inside a `workspace` turn via the ledger_write tool); this
    branch exists ONLY so the eval harness itself has a known-good input to
    execute against in fixture mode, per the "before recording any eval
    baseline, push one known-good input through the harness and confirm it
    PASSES" gate. It proves the harness plumbing (sample loading, grading,
    reporting) works, NOT model quality -- see
    tests/golden/ledger-eval-samples-2026-07-11.samples.json's docstring-
    equivalent note and TRACKD_LEDGER.md for the explicit honesty statement
    that --live-providers is the real verification this stands in for."""
    refs = re.findall(r'<entity ref="([^"]+)"/>', prompt)
    entry_type_match = re.search(r"<expected_entry_type>([^<]+)</expected_entry_type>", prompt)
    entry_type = entry_type_match.group(1) if entry_type_match else "assumption"
    statement_match = re.search(r"<expected_statement>([^<]+)</expected_statement>", prompt)
    statement = statement_match.group(1) if statement_match else "Fixture-drafted ledger statement."
    return json.dumps({
        "entry_type": entry_type,
        "statement": statement,
        "confidence": "stated",
        "depends_on_entity_ids": refs,
        "evidence_refs": [f"object:{r}" for r in refs],
    })


def _fixture_workspace_response(prompt: str) -> str:
    """Deterministic fixture for the session tool loop (Slice 3.2).

    Fixture mode has no real model to decide "call a tool vs. answer", so
    tests drive the loop explicitly: app.sessions.loop embeds a
    <scripted_plan> JSON array (each entry a full workspace-action dict) and
    a <call_index> in the prompt. This fixture just plays back
    scripted_plan[call_index]. This is a test-only protocol -- production
    routes to a real model, never this path (see Settings.llm_fixture_mode's
    APP_ENV=test enforcement above)."""
    plan_match = re.search(r"<scripted_plan>(.*?)</scripted_plan>", prompt, re.DOTALL)
    index_match = re.search(r"<call_index>(\d+)</call_index>", prompt)
    call_index = int(index_match.group(1)) if index_match else 0
    if plan_match:
        try:
            plan = json.loads(plan_match.group(1))
        except json.JSONDecodeError:
            plan = []
    else:
        plan = []
    if call_index < len(plan):
        return json.dumps(plan[call_index])
    return json.dumps({"action": "final", "text": "Fixture ran out of scripted actions; ending turn."})
