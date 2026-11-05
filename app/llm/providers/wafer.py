from app.llm.providers.openai_compatible import OpenAICompatibleAdapter


class WaferAdapter(OpenAICompatibleAdapter):
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        client=None,
        zdr_required: bool = False,
    ) -> None:
        headers = {"Wafer-ZDR": "required"} if zdr_required else None
        super().__init__(
            provider="wafer",
            api_key=api_key,
            base_url=base_url,
            client=client,
            extra_headers=headers,
            extra_body={"thinking": {"type": "disabled"}},
        )
