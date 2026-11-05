"""Provider adapter contracts for LLM calls."""
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class ProviderRequest:
    model: str
    prompt: str
    system: str | None = None
    max_tokens: int = 2048


@dataclass(frozen=True)
class ProviderResponse:
    provider: str
    model: str
    text: str
    input_tokens: int
    output_tokens: int
    latency_ms: int
    cache_status: str | None = None
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    raw_response: Any = None


class ProviderAdapter(Protocol):
    provider: str

    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        ...
