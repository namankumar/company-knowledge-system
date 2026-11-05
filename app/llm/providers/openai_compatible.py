"""OpenAI-compatible chat-completions adapter."""
import time
from typing import Any

import httpx

from app.llm.failures import ProviderCallError, normalize_provider_exception
from app.llm.providers.base import ProviderRequest, ProviderResponse


class OpenAICompatibleAdapter:
    def __init__(
        self,
        *,
        provider: str,
        api_key: str,
        base_url: str,
        client: httpx.AsyncClient | None = None,
        extra_headers: dict[str, str] | None = None,
        extra_body: dict[str, Any] | None = None,
    ) -> None:
        self.provider = provider
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._client = client
        self._extra_headers = extra_headers or {}
        self._extra_body = extra_body or {}

    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        if not self._api_key:
            raise ProviderCallError("policy_blocked", f"{self.provider} API key is not configured")

        payload: dict[str, Any] = {
            "model": request.model,
            "messages": _messages(request),
            "max_tokens": request.max_tokens,
        }
        payload.update(self._extra_body)
        headers = {"Authorization": f"Bearer {self._api_key}", **self._extra_headers}

        start = time.perf_counter()
        try:
            if self._client is not None:
                response = await self._client.post(
                    f"{self._base_url}/chat/completions",
                    json=payload,
                    headers=headers,
                )
            else:
                async with httpx.AsyncClient(timeout=60.0) as client:
                    response = await client.post(
                        f"{self._base_url}/chat/completions",
                        json=payload,
                        headers=headers,
                    )
            response.raise_for_status()
            data = response.json()
        except ProviderCallError:
            raise
        except httpx.TimeoutException as exc:
            raise ProviderCallError("timeout", str(exc), original=exc) from exc
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            reason = "rate_limited" if status == 429 else "provider_error"
            raise ProviderCallError(reason, str(exc), original=exc) from exc
        except Exception as exc:
            raise normalize_provider_exception(exc) from exc

        latency_ms = int((time.perf_counter() - start) * 1000)
        try:
            text = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderCallError("provider_error", "Malformed chat completion response", original=exc) from exc
        if not text.strip():
            raise ProviderCallError("empty_output", "Provider returned empty output")

        usage = data.get("usage") or {}
        input_tokens = int(usage.get("prompt_tokens") or _estimate_tokens(request.prompt, request.system))
        output_tokens = int(usage.get("completion_tokens") or _estimate_tokens(text))
        return ProviderResponse(
            provider=self.provider,
            model=request.model,
            text=text,
            input_tokens=max(1, input_tokens),
            output_tokens=max(1, output_tokens),
            latency_ms=latency_ms,
            raw_response=data,
        )


def _messages(request: ProviderRequest) -> list[dict[str, str]]:
    messages = []
    if request.system:
        messages.append({"role": "system", "content": request.system})
    messages.append({"role": "user", "content": request.prompt})
    return messages


def _estimate_tokens(*parts: str | None) -> int:
    text = "\n".join(part or "" for part in parts)
    return max(1, len(text) // 4)
