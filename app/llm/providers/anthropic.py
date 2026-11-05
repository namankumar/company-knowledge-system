"""Anthropic provider adapter."""
import time
from typing import Any

from anthropic import AsyncAnthropic

from app.llm.failures import ProviderCallError, normalize_provider_exception
from app.llm.providers.base import ProviderRequest, ProviderResponse

ANTHROPIC_PROMPT_CACHE_MIN_TOKENS = 1024


class AnthropicAdapter:
    provider = "anthropic"

    def __init__(self, *, api_key: str, client: Any | None = None) -> None:
        self._client = client or AsyncAnthropic(api_key=api_key)

    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        prompt_block, prompt_cacheable = _cacheable_text_block(request.prompt)
        kwargs: dict = {
            "model": request.model,
            "max_tokens": request.max_tokens,
            "messages": [{"role": "user", "content": [prompt_block] if prompt_cacheable else request.prompt}],
        }
        system_cacheable = False
        if request.system:
            system_block, system_cacheable = _cacheable_text_block(request.system)
            kwargs["system"] = [system_block]

        start = time.perf_counter()
        try:
            response = await self._client.messages.create(**kwargs)
        except ProviderCallError:
            raise
        except Exception as exc:
            raise normalize_provider_exception(exc) from exc
        latency_ms = int((time.perf_counter() - start) * 1000)
        cache_creation_tokens = int(getattr(response.usage, "cache_creation_input_tokens", 0) or 0)
        cache_read_tokens = int(getattr(response.usage, "cache_read_input_tokens", 0) or 0)

        return ProviderResponse(
            provider=self.provider,
            model=request.model,
            text=response.content[0].text if response.content else "",
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            latency_ms=latency_ms,
            cache_status=_cache_status(
                cache_creation_input_tokens=cache_creation_tokens,
                cache_read_input_tokens=cache_read_tokens,
                cacheable=system_cacheable or prompt_cacheable,
                cache_requested=bool(request.system or request.prompt),
            ),
            cache_creation_input_tokens=cache_creation_tokens,
            cache_read_input_tokens=cache_read_tokens,
            raw_response=response,
        )


def _cacheable_text_block(text: str) -> tuple[dict[str, Any], bool]:
    cacheable = _rough_token_count(text) >= ANTHROPIC_PROMPT_CACHE_MIN_TOKENS
    block = {"type": "text", "text": text}
    if cacheable:
        block["cache_control"] = {"type": "ephemeral"}
    return block, cacheable


def _rough_token_count(text: str) -> int:
    return max(1, len(text) // 4)


def _cache_status(
    *,
    cache_creation_input_tokens: int,
    cache_read_input_tokens: int,
    cacheable: bool,
    cache_requested: bool,
) -> str | None:
    if cache_read_input_tokens > 0:
        return "hit"
    if cache_creation_input_tokens > 0:
        return "write"
    if not cacheable:
        return "ineligible" if cache_requested else None
    return "miss"
