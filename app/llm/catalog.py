"""Static model catalog for router policy decisions."""
from dataclasses import dataclass
from typing import Any


PRODUCTION_TASKS = (
    "triage",
    "extraction",
    "drift",
    "brief_convergence",
    "brief",
    "plan_actual",
    "personalization_seed",
    "person_context_seed",
    "person_context_update",
    "tenant_context_seed",
    "tenant_context_update",
    "comms_draft",
    "chat",
    "repair",
    "workstream_digest_draft",
    # Slice 3.2: session tool loop. One call per turn decides the next tool
    # call or produces the final cited answer. Same GLM candidate chain as
    # "chat" -- comparable output shape and size, no reason to route
    # differently.
    "workspace",
)


@dataclass(frozen=True)
class ModelSpec:
    alias: str
    provider: str
    model: str
    input_cost_per_million: float
    output_cost_per_million: float
    context_window: int
    supports_json: bool
    allowed_tasks: tuple[str, ...]
    quality_tier: str
    notes: str = ""
    extra_body: dict[str, Any] | None = None
    cache_creation_input_cost_per_million: float | None = None
    cache_read_input_cost_per_million: float | None = None

    def estimated_cost(
        self,
        input_tokens: int,
        output_tokens: int,
        *,
        cache_creation_input_tokens: int = 0,
        cache_read_input_tokens: int = 0,
    ) -> float:
        cache_creation_rate = self.cache_creation_input_cost_per_million or self.input_cost_per_million
        cache_read_rate = self.cache_read_input_cost_per_million or self.input_cost_per_million
        return (
            input_tokens / 1_000_000 * self.input_cost_per_million
            + cache_creation_input_tokens / 1_000_000 * cache_creation_rate
            + cache_read_input_tokens / 1_000_000 * cache_read_rate
            + output_tokens / 1_000_000 * self.output_cost_per_million
        )


class UnknownModelCostError(ValueError):
    pass


MODEL_CATALOG: dict[str, ModelSpec] = {
    "anthropic_haiku": ModelSpec(
        alias="anthropic_haiku",
        provider="anthropic",
        model="claude-haiku-4-5-20251001",
        input_cost_per_million=1.00,
        output_cost_per_million=5.00,
        context_window=200_000,
        supports_json=True,
        allowed_tasks=("triage", "extraction", "repair"),
        quality_tier="fallback",
        cache_creation_input_cost_per_million=1.25,
        cache_read_input_cost_per_million=0.10,
    ),
    "anthropic_sonnet": ModelSpec(
        alias="anthropic_sonnet",
        provider="anthropic",
        model="claude-sonnet-4-6",
        input_cost_per_million=3.00,
        output_cost_per_million=15.00,
        context_window=200_000,
        supports_json=True,
        allowed_tasks=("drift", "brief_convergence", "brief", "chat", "repair", "extraction", "plan_actual", "personalization_seed", "person_context_seed", "person_context_update", "tenant_context_seed", "tenant_context_update", "workstream_digest_draft", "workspace",),
        quality_tier="fallback",
        cache_creation_input_cost_per_million=3.75,
        cache_read_input_cost_per_million=0.30,
    ),
    "anthropic_opus": ModelSpec(
        alias="anthropic_opus",
        provider="anthropic",
        model="claude-opus-4-8",
        input_cost_per_million=15.00,
        output_cost_per_million=75.00,
        context_window=200_000,
        supports_json=True,
        allowed_tasks=("review", "repair"),
        quality_tier="strong",
        notes="Default strong review model. Keep priced so cost reservation never rejects the repo default.",
        cache_creation_input_cost_per_million=18.75,
        cache_read_input_cost_per_million=1.50,
    ),
    "groq_llama_3_1_8b": ModelSpec(
        alias="groq_llama_3_1_8b",
        provider="groq",
        model="llama-3.1-8b-instant",
        input_cost_per_million=0.05,
        output_cost_per_million=0.08,
        context_window=128_000,
        supports_json=True,
        allowed_tasks=("triage",),
        quality_tier="cheap",
    ),
    "groq_gpt_oss_120b": ModelSpec(
        alias="groq_gpt_oss_120b",
        provider="groq",
        model="openai/gpt-oss-120b",
        input_cost_per_million=0.15,
        output_cost_per_million=0.60,
        context_window=128_000,
        supports_json=True,
        allowed_tasks=("triage", "drift", "brief_convergence", "brief", "plan_actual", "personalization_seed", "person_context_seed", "person_context_update", "tenant_context_seed", "tenant_context_update", "workstream_digest_draft", "comms_draft",),
        quality_tier="cheap_synthesis",
    ),
    "deepinfra_deepseek_v4_flash": ModelSpec(
        alias="deepinfra_deepseek_v4_flash",
        provider="deepinfra",
        model="deepseek-ai/DeepSeek-V4-Flash",
        input_cost_per_million=0.10,
        output_cost_per_million=0.20,
        context_window=1_000_000,
        supports_json=True,
        allowed_tasks=("extraction", "repair"),
        quality_tier="cheap_extraction",
    ),
    "lilac_glm_5_2": ModelSpec(
        alias="lilac_glm_5_2",
        provider="lilac",
        model="zai-org/glm-5.2",
        input_cost_per_million=0.90,
        output_cost_per_million=3.00,
        context_window=524_000,
        supports_json=True,
        allowed_tasks=("chat", "repair", "drift", "brief_convergence", "brief", "plan_actual", "personalization_seed", "person_context_seed", "person_context_update", "tenant_context_seed", "tenant_context_update", "workstream_digest_draft", "workspace", "comms_draft",),
        quality_tier="hard_call",
        notes="Lilac GLM 5.2 FP8/NVFP4 route. Cheap but quantized; keep behind two higher-confidence GLM attempts and skip for context above 524K.",
    ),
    "deepinfra_glm_5_2": ModelSpec(
        alias="deepinfra_glm_5_2",
        provider="deepinfra",
        model="zai-org/GLM-5.2",
        input_cost_per_million=1.00,
        output_cost_per_million=4.00,
        context_window=1_000_000,
        supports_json=True,
        allowed_tasks=("chat", "repair", "drift", "brief_convergence", "brief", "plan_actual", "personalization_seed", "person_context_seed", "person_context_update", "tenant_context_seed", "tenant_context_update", "workstream_digest_draft", "workspace", "comms_draft",),
        quality_tier="hard_call",
        notes="Direct DeepInfra GLM 5.2 route. Second GLM attempt after OpenRouter forced GMICloud.",
    ),
    "wafer_glm_5_2": ModelSpec(
        alias="wafer_glm_5_2",
        provider="wafer",
        model="GLM-5.2",
        input_cost_per_million=1.20,
        output_cost_per_million=4.10,
        context_window=1_000_000,
        supports_json=True,
        allowed_tasks=("chat", "repair", "drift", "brief_convergence", "brief", "plan_actual", "personalization_seed", "person_context_seed", "person_context_update", "tenant_context_seed", "tenant_context_update", "workstream_digest_draft", "workspace", "comms_draft",),
        quality_tier="hard_call",
        notes="Wafer serverless GLM 5.2 route. Use WAFER_ZDR_REQUIRED=true before production customer-data traffic if ZDR is available on the account.",
    ),
    "baseten_glm_5_2": ModelSpec(
        alias="baseten_glm_5_2",
        provider="baseten",
        model="zai-org/GLM-5.2",
        input_cost_per_million=1.50,
        output_cost_per_million=4.50,
        context_window=1_000_000,
        supports_json=True,
        allowed_tasks=("chat", "repair", "drift", "brief_convergence", "brief", "plan_actual", "personalization_seed", "person_context_seed", "person_context_update", "tenant_context_seed", "tenant_context_update", "workstream_digest_draft", "workspace",),
        quality_tier="hard_call_fast",
    ),
    "openrouter_glm_5_2": ModelSpec(
        alias="openrouter_glm_5_2",
        provider="openrouter",
        model="z-ai/glm-5.2",
        input_cost_per_million=1.03,
        output_cost_per_million=3.25,
        context_window=1_000_000,
        supports_json=True,
        allowed_tasks=("chat", "repair", "drift", "brief_convergence", "brief", "plan_actual", "personalization_seed", "person_context_seed", "person_context_update", "tenant_context_seed", "tenant_context_update", "workstream_digest_draft", "workspace",),
        quality_tier="hard_call_last_glm",
        notes="Inactive generic OpenRouter GLM route. Costs include 5.5% OpenRouter VAT. Prefer provider-forced aliases so our router owns failover and logging.",
    ),
    "openrouter_glm_5_2_gmicloud": ModelSpec(
        alias="openrouter_glm_5_2_gmicloud",
        provider="openrouter",
        model="z-ai/glm-5.2",
        input_cost_per_million=1.03,
        output_cost_per_million=3.25,
        context_window=1_048_576,
        supports_json=True,
        allowed_tasks=("chat", "repair", "drift", "brief_convergence", "brief", "plan_actual", "personalization_seed", "person_context_seed", "person_context_update", "tenant_context_seed", "tenant_context_update", "workstream_digest_draft", "workspace", "comms_draft",),
        quality_tier="glm_cost_first",
        notes="Forced OpenRouter GMICloud FP8 route. Costs include 5.5% OpenRouter VAT. OpenRouter fallback disabled so app router records failover.",
        extra_body={"provider": {"only": ["GMICloud"], "allow_fallbacks": False}},
    ),
    "openrouter_gpt_oss_120b": ModelSpec(
        alias="openrouter_gpt_oss_120b",
        provider="openrouter",
        model="openai/gpt-oss-120b",
        input_cost_per_million=0.15825,
        output_cost_per_million=0.633,
        context_window=131_072,
        supports_json=True,
        allowed_tasks=("triage",),
        quality_tier="cheap_triage_fallback",
        notes="OpenRouter failover for Groq GPT-OSS 120B triage attempts. Costs include 5.5% OpenRouter VAT.",
    ),
    "openai_teacher": ModelSpec(
        alias="openai_teacher",
        provider="openai",
        model="gpt-5.5",
        input_cost_per_million=5.00,
        output_cost_per_million=30.00,
        context_window=400_000,
        supports_json=True,
        allowed_tasks=PRODUCTION_TASKS,
        quality_tier="teacher_eval_only",
        notes="Eval teacher only unless a task policy explicitly allows fallback.",
    ),
    "claude_cli_sonnet": ModelSpec(
        alias="claude_cli_sonnet",
        provider="claude_cli",
        model="sonnet",
        input_cost_per_million=0.0,
        output_cost_per_million=0.0,
        context_window=200_000,
        supports_json=True,
        allowed_tasks=("chat",),
        quality_tier="local_subscription_cli",
        notes="Local development-only Claude CLI route for chat. Uses subscription-backed CLI execution instead of HTTP.",
    ),
}


def get_model(alias: str) -> ModelSpec:
    return MODEL_CATALOG[alias]


def pricing_spec_for_model(model: str, *, provider: str | None = None) -> ModelSpec:
    if model in MODEL_CATALOG:
        spec = MODEL_CATALOG[model]
        if provider is None or spec.provider == provider:
            return spec

    matches = [
        spec for spec in MODEL_CATALOG.values()
        if spec.model == model and (provider is None or spec.provider == provider)
    ]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        providers = ", ".join(sorted({spec.provider for spec in matches}))
        raise UnknownModelCostError(
            f"ambiguous model pricing for {model!r}; pass provider. Matching providers: {providers}"
        )
    suffix = f" for provider {provider!r}" if provider else ""
    raise UnknownModelCostError(f"unknown model pricing for {model!r}{suffix}; add it to MODEL_CATALOG")


def normalize_model_override(model: str, *, provider: str | None = None) -> str:
    return pricing_spec_for_model(model, provider=provider).model


def estimate_model_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    *,
    provider: str | None = None,
    cache_creation_input_tokens: int = 0,
    cache_read_input_tokens: int = 0,
) -> float:
    return pricing_spec_for_model(model, provider=provider).estimated_cost(
        input_tokens,
        output_tokens,
        cache_creation_input_tokens=cache_creation_input_tokens,
        cache_read_input_tokens=cache_read_input_tokens,
    )
