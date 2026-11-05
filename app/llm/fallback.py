"""Capped fallback execution for router attempts."""
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from app.llm.failures import FailureReason, ProviderCallError
from app.llm.validators import ValidatorResult, validate_task_output


@dataclass(frozen=True)
class CandidateOutput:
    alias: str
    provider: str
    model: str
    text: str
    parsed: Any
    input_tokens: int
    output_tokens: int
    cost_usd: float
    latency_ms: int
    cache_status: str | None = None
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


@dataclass(frozen=True)
class RouteAttempt:
    alias: str
    attempt_index: int
    fallback_reason: FailureReason | None
    validation: ValidatorResult | None = None
    output: CandidateOutput | None = None
    error_reason: FailureReason | None = None
    cost_usd: float = 0.0


@dataclass(frozen=True)
class RouteResult:
    output: CandidateOutput | None
    attempts: tuple[RouteAttempt, ...]
    stopped_reason: FailureReason | None = None


CallCandidate = Callable[[str, int, FailureReason | None], Awaitable[CandidateOutput]]


async def run_capped_route(
    *,
    task: str,
    aliases: tuple[str, ...],
    call_candidate: CallCandidate,
    max_attempts: int,
    max_cost_usd: float,
    pack_items: list[dict] | None = None,
    prompt_has_content: bool = False,
    expected_shape: dict[str, type] | None = None,
) -> RouteResult:
    attempts: list[RouteAttempt] = []
    spent = 0.0
    fallback_reason: FailureReason | None = None

    for alias in aliases[:max_attempts]:
        if spent >= max_cost_usd:
            return RouteResult(None, tuple(attempts), "budget_exhausted")

        attempt_index = len(attempts)
        try:
            output = await call_candidate(alias, attempt_index, fallback_reason)
        except ProviderCallError as exc:
            attempts.append(
                RouteAttempt(
                    alias=alias,
                    attempt_index=attempt_index,
                    fallback_reason=fallback_reason,
                    error_reason=exc.reason,
                )
            )
            fallback_reason = exc.reason
            continue

        spent += output.cost_usd
        if spent > max_cost_usd:
            attempts.append(
                RouteAttempt(
                    alias=alias,
                    attempt_index=attempt_index,
                    fallback_reason=fallback_reason,
                    output=output,
                    error_reason="budget_exhausted",
                    cost_usd=output.cost_usd,
                )
            )
            return RouteResult(None, tuple(attempts), "budget_exhausted")

        validation = validate_task_output(
            task,
            parsed=output.parsed,
            text=output.text,
            pack_items=pack_items or [],
            prompt_has_content=prompt_has_content,
            expected_shape=expected_shape or {},
        )
        attempts.append(
            RouteAttempt(
                alias=alias,
                attempt_index=attempt_index,
                fallback_reason=fallback_reason,
                validation=validation,
                output=output,
                error_reason=None if validation.valid else validation.reason,
                cost_usd=output.cost_usd,
            )
        )
        if validation.valid:
            return RouteResult(output, tuple(attempts))
        fallback_reason = validation.reason

    return RouteResult(None, tuple(attempts), fallback_reason)
