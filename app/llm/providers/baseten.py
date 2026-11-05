from app.llm.providers.openai_compatible import OpenAICompatibleAdapter


class BasetenAdapter(OpenAICompatibleAdapter):
    def __init__(self, *, api_key: str, base_url: str, client=None) -> None:
        super().__init__(provider="baseten", api_key=api_key, base_url=base_url, client=client)
