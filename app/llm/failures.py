"""Shared LLM failure reasons for router decisions."""
from dataclasses import dataclass
from typing import Literal


FailureReason = Literal[
    "timeout",
    "rate_limited",
    "provider_error",
    "invalid_json",
    "schema_invalid",
    "empty_output",
    "evidence_invalid",
    "low_confidence",
    "policy_blocked",
    "budget_exhausted",
]


class ProviderCallError(Exception):
    def __init__(self, reason: FailureReason, message: str, *, original: BaseException | None = None) -> None:
        super().__init__(message)
        self.reason = reason
        self.original = original


@dataclass(frozen=True)
class Failure:
    reason: FailureReason
    detail: str = ""


def provider_failure_reason(exc: BaseException) -> FailureReason:
    name = type(exc).__name__
    status_code = getattr(exc, "status_code", None)

    if isinstance(exc, TimeoutError) or name in {"APITimeoutError", "Timeout"}:
        return "timeout"
    if status_code == 429 or name == "RateLimitError":
        return "rate_limited"
    return "provider_error"


def normalize_provider_exception(exc: BaseException) -> ProviderCallError:
    reason = provider_failure_reason(exc)
    return ProviderCallError(reason, str(exc), original=exc)
