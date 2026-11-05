from app.llm.providers.openai_compatible import OpenAICompatibleAdapter


class GroqAdapter(OpenAICompatibleAdapter):
    def __init__(self, *, api_key: str, base_url: str, client=None) -> None:
        super().__init__(provider="groq", api_key=api_key, base_url=base_url, client=client)
