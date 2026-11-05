from app.llm.providers.openai_compatible import OpenAICompatibleAdapter


class OpenRouterAdapter(OpenAICompatibleAdapter):
    def __init__(self, *, api_key: str, base_url: str, client=None, extra_body: dict | None = None) -> None:
        super().__init__(
            provider="openrouter",
            api_key=api_key,
            base_url=base_url,
            client=client,
            extra_body=extra_body,
        )
