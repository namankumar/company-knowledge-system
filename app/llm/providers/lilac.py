from app.llm.providers.openai_compatible import OpenAICompatibleAdapter


class LilacAdapter(OpenAICompatibleAdapter):
    def __init__(self, *, api_key: str, base_url: str, client=None) -> None:
        super().__init__(provider="lilac", api_key=api_key, base_url=base_url, client=client)
