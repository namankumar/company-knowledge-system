from app.llm.providers.openai_compatible import OpenAICompatibleAdapter


class OpenAIAdapter(OpenAICompatibleAdapter):
    def __init__(self, *, api_key: str, base_url: str = "https://api.openai.com/v1", client=None) -> None:
        super().__init__(provider="openai", api_key=api_key, base_url=base_url, client=client)
