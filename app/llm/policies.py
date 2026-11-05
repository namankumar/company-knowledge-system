"""Task model policies for the router MVP."""
from dataclasses import dataclass, field
from typing import Literal

from app.llm.catalog import MODEL_CATALOG, PRODUCTION_TASKS, ModelSpec
from app.llm.failures import FailureReason

RouteMode = Literal["production", "eval"]


@dataclass(frozen=True)
class TaskPolicy:
    task: str
    production_candidates: tuple[str, ...]
    eval_candidates: tuple[str, ...]
    fallback_aliases: tuple[str, ...]
    max_attempts: int
    timeout_seconds: float
    validator: str
    max_cost_per_call_usd: float

    def aliases_for_mode(self, mode: RouteMode) -> tuple[str, ...]:
        return self.production_candidates if mode == "production" else self.eval_candidates


@dataclass(frozen=True)
class SkippedModel:
    alias: str
    reason: FailureReason
    detail: str = ""


@dataclass(frozen=True)
class RouteSelection:
    model: ModelSpec | None
    skipped: tuple[SkippedModel, ...] = field(default_factory=tuple)


TASK_POLICIES: dict[str, TaskPolicy] = {
    "triage": TaskPolicy(
        task="triage",
        production_candidates=("groq_gpt_oss_120b",),
        eval_candidates=("groq_gpt_oss_120b", "openrouter_gpt_oss_120b", "groq_llama_3_1_8b"),
        fallback_aliases=("openrouter_gpt_oss_120b", "groq_llama_3_1_8b"),
        max_attempts=3,
        timeout_seconds=8.0,
        validator="triage",
        max_cost_per_call_usd=0.01,
    ),
    "extraction": TaskPolicy(
        task="extraction",
        production_candidates=("deepinfra_deepseek_v4_flash",),
        eval_candidates=("deepinfra_deepseek_v4_flash",),
        fallback_aliases=(),
        max_attempts=1,
        timeout_seconds=20.0,
        validator="extraction",
        max_cost_per_call_usd=0.05,
    ),
    "drift": TaskPolicy(
        task="drift",
        production_candidates=("openrouter_glm_5_2_gmicloud",),
        eval_candidates=(
            "openrouter_glm_5_2_gmicloud",
            "deepinfra_glm_5_2",
            "lilac_glm_5_2",
            "wafer_glm_5_2",
            "groq_gpt_oss_120b",
        ),
        fallback_aliases=(
            "deepinfra_glm_5_2",
            "lilac_glm_5_2",
            "wafer_glm_5_2",
        ),
        max_attempts=4,
        timeout_seconds=30.0,
        validator="drift",
        max_cost_per_call_usd=0.15,
    ),
    # Slice 2.1: two-call structured brief synthesis. brief_convergence (call 1)
    # groups pack evidence into operating stories; brief (call 2) writes each
    # story as JSON with per-block citations, rendered to markdown
    # deterministically. Costs split across both calls stay within the old
    # single-call ceiling combined (0.15 + 0.30 vs the prior 0.20), offset by
    # phase 5.4's prompt caching per the build plan's cost note.
    "brief_convergence": TaskPolicy(
        task="brief_convergence",
        production_candidates=("openrouter_glm_5_2_gmicloud",),
        eval_candidates=(
            "openrouter_glm_5_2_gmicloud",
            "deepinfra_glm_5_2",
            "lilac_glm_5_2",
            "wafer_glm_5_2",
            "groq_gpt_oss_120b",
        ),
        fallback_aliases=(
            "deepinfra_glm_5_2",
            "lilac_glm_5_2",
            "wafer_glm_5_2",
        ),
        max_attempts=4,
        timeout_seconds=30.0,
        validator="brief_convergence",
        max_cost_per_call_usd=0.15,
    ),
    "brief": TaskPolicy(
        task="brief",
        production_candidates=("openrouter_glm_5_2_gmicloud",),
        eval_candidates=(
            "openrouter_glm_5_2_gmicloud",
            "deepinfra_glm_5_2",
            "lilac_glm_5_2",
            "wafer_glm_5_2",
            "groq_gpt_oss_120b",
        ),
        fallback_aliases=(
            "deepinfra_glm_5_2",
            "lilac_glm_5_2",
            "wafer_glm_5_2",
        ),
        max_attempts=4,
        timeout_seconds=40.0,
        validator="brief_structured",
        max_cost_per_call_usd=0.30,
    ),
    "brief_weekly": TaskPolicy(
        task="brief_weekly",
        production_candidates=("openrouter_glm_5_2_gmicloud",),
        eval_candidates=(
            "openrouter_glm_5_2_gmicloud",
            "deepinfra_glm_5_2",
            "lilac_glm_5_2",
            "wafer_glm_5_2",
            "groq_gpt_oss_120b",
        ),
        fallback_aliases=(
            "deepinfra_glm_5_2",
            "lilac_glm_5_2",
            "wafer_glm_5_2",
        ),
        max_attempts=4,
        timeout_seconds=40.0,
        validator="brief_weekly",
        max_cost_per_call_usd=0.30,
    ),
    # Slice 2.4: independent third call over the same brief pack (not the
    # convergence call's grouped stories) -- which stated plans have no
    # matching activity, and which activities match no plan. Same GLM
    # candidate chain and cost tier as brief_convergence: comparable output
    # size, no reason to route it differently.
    "plan_actual": TaskPolicy(
        task="plan_actual",
        production_candidates=("openrouter_glm_5_2_gmicloud",),
        eval_candidates=(
            "openrouter_glm_5_2_gmicloud",
            "deepinfra_glm_5_2",
            "lilac_glm_5_2",
            "wafer_glm_5_2",
            "groq_gpt_oss_120b",
        ),
        fallback_aliases=(
            "deepinfra_glm_5_2",
            "lilac_glm_5_2",
            "wafer_glm_5_2",
        ),
        max_attempts=4,
        timeout_seconds=30.0,
        validator="plan_actual",
        max_cost_per_call_usd=0.15,
    ),
    # Track B, artifacts-lifecycle packet (2026-07-11), slice A.3: comms_draft
    # re-targets brief's already-verified grouped stories for a
    # leadership/investor audience -- same synthesis maturity, same GLM
    # candidate chain and cost tier as brief_convergence/plan_actual, no new
    # model bakeoff required before shipping (per the packet's Model
    # Capability Evidence table).
    "comms_draft": TaskPolicy(
        task="comms_draft",
        production_candidates=("openrouter_glm_5_2_gmicloud",),
        eval_candidates=(
            "openrouter_glm_5_2_gmicloud",
            "deepinfra_glm_5_2",
            "lilac_glm_5_2",
            "wafer_glm_5_2",
            "groq_gpt_oss_120b",
        ),
        fallback_aliases=(
            "deepinfra_glm_5_2",
            "lilac_glm_5_2",
            "wafer_glm_5_2",
        ),
        max_attempts=4,
        timeout_seconds=30.0,
        validator="comms_draft",
        max_cost_per_call_usd=0.15,
    ),
    "chat": TaskPolicy(
        task="chat",
        production_candidates=("openrouter_glm_5_2_gmicloud",),
        eval_candidates=(
            "openrouter_glm_5_2_gmicloud",
            "deepinfra_glm_5_2",
            "lilac_glm_5_2",
            "wafer_glm_5_2",
        ),
        fallback_aliases=(
            "deepinfra_glm_5_2",
            "lilac_glm_5_2",
            "wafer_glm_5_2",
        ),
        max_attempts=4,
        timeout_seconds=30.0,
        validator="chat",
        max_cost_per_call_usd=0.20,
    ),
    # Slice 2.6: cold-start seeding. One-shot per user (see
    # app.personalization.seed), same GLM candidate chain as the other
    # structured-JSON tasks.
    "personalization_seed": TaskPolicy(
        task="personalization_seed",
        production_candidates=("openrouter_glm_5_2_gmicloud",),
        eval_candidates=(
            "openrouter_glm_5_2_gmicloud",
            "deepinfra_glm_5_2",
            "lilac_glm_5_2",
            "wafer_glm_5_2",
            "groq_gpt_oss_120b",
        ),
        fallback_aliases=(
            "deepinfra_glm_5_2",
            "lilac_glm_5_2",
            "wafer_glm_5_2",
        ),
        max_attempts=4,
        timeout_seconds=30.0,
        validator="personalization_seed",
        max_cost_per_call_usd=0.15,
    ),
    "person_context_seed": TaskPolicy(
        task="person_context_seed",
        production_candidates=("openrouter_glm_5_2_gmicloud",),
        eval_candidates=(
            "openrouter_glm_5_2_gmicloud",
            "deepinfra_glm_5_2",
            "lilac_glm_5_2",
            "wafer_glm_5_2",
            "groq_gpt_oss_120b",
        ),
        fallback_aliases=(
            "deepinfra_glm_5_2",
            "lilac_glm_5_2",
            "wafer_glm_5_2",
        ),
        max_attempts=4,
        timeout_seconds=30.0,
        validator="person_context_seed",
        max_cost_per_call_usd=0.15,
    ),
    "person_context_update": TaskPolicy(
        task="person_context_update",
        production_candidates=("openrouter_glm_5_2_gmicloud",),
        eval_candidates=(
            "openrouter_glm_5_2_gmicloud",
            "deepinfra_glm_5_2",
            "lilac_glm_5_2",
            "wafer_glm_5_2",
            "groq_gpt_oss_120b",
        ),
        fallback_aliases=(
            "deepinfra_glm_5_2",
            "lilac_glm_5_2",
            "wafer_glm_5_2",
        ),
        max_attempts=4,
        timeout_seconds=30.0,
        validator="person_context_update",
        max_cost_per_call_usd=0.15,
    ),
    "tenant_context_seed": TaskPolicy(
        task="tenant_context_seed",
        production_candidates=("openrouter_glm_5_2_gmicloud",),
        eval_candidates=(
            "openrouter_glm_5_2_gmicloud",
            "deepinfra_glm_5_2",
            "lilac_glm_5_2",
            "wafer_glm_5_2",
            "groq_gpt_oss_120b",
        ),
        fallback_aliases=(
            "deepinfra_glm_5_2",
            "lilac_glm_5_2",
            "wafer_glm_5_2",
        ),
        max_attempts=4,
        timeout_seconds=30.0,
        validator="tenant_context_seed",
        max_cost_per_call_usd=0.15,
    ),
    "tenant_context_update": TaskPolicy(
        task="tenant_context_update",
        production_candidates=("openrouter_glm_5_2_gmicloud",),
        eval_candidates=(
            "openrouter_glm_5_2_gmicloud",
            "deepinfra_glm_5_2",
            "lilac_glm_5_2",
            "wafer_glm_5_2",
            "groq_gpt_oss_120b",
        ),
        fallback_aliases=(
            "deepinfra_glm_5_2",
            "lilac_glm_5_2",
            "wafer_glm_5_2",
        ),
        max_attempts=4,
        timeout_seconds=30.0,
        validator="tenant_context_update",
        max_cost_per_call_usd=0.15,
    ),
    # Slice 3 (packpolicy-digests packet): a short extractive workstream
    # digest draft, cheaper than a full brief/personalization_seed -- same
    # GLM candidate chain, lower cost cap.
    "workstream_digest_draft": TaskPolicy(
        task="workstream_digest_draft",
        production_candidates=("openrouter_glm_5_2_gmicloud",),
        eval_candidates=(
            "openrouter_glm_5_2_gmicloud",
            "deepinfra_glm_5_2",
            "lilac_glm_5_2",
            "wafer_glm_5_2",
            "groq_gpt_oss_120b",
        ),
        fallback_aliases=(
            "deepinfra_glm_5_2",
            "lilac_glm_5_2",
            "wafer_glm_5_2",
        ),
        max_attempts=4,
        timeout_seconds=20.0,
        validator="workstream_digest",
        max_cost_per_call_usd=0.08,
    ),
    # Slice 3.2: session tool loop. One call per turn -- decides the next
    # tool call (search/dig/artifact_create/artifact_edit/
    # propose_context_update) or produces the final cited answer. Same GLM
    # candidate chain and cost tier as "chat": comparable per-call size, and
    # the loop budget (not per-call cost) is the real backstop against
    # runaway spend for a multi-call turn.
    "workspace": TaskPolicy(
        task="workspace",
        production_candidates=("openrouter_glm_5_2_gmicloud",),
        eval_candidates=(
            "openrouter_glm_5_2_gmicloud",
            "deepinfra_glm_5_2",
            "lilac_glm_5_2",
            "wafer_glm_5_2",
        ),
        fallback_aliases=(
            "deepinfra_glm_5_2",
            "lilac_glm_5_2",
            "wafer_glm_5_2",
        ),
        max_attempts=4,
        timeout_seconds=30.0,
        validator="workspace",
        max_cost_per_call_usd=0.20,
    ),
    # Slice 3.3 (threads-sessions packet, Review Verdict Q3): the "regenerate
    # state" v1 correction action re-runs this task over the FULL thread
    # history to redrive session_state's factors/decisions/open_questions
    # from scratch. Same GLM candidate chain/cost tier as
    # workstream_digest_draft -- a comparably-sized extractive-summary task,
    # not a full brief/personalization_seed-scale call.
    "session_state_maintain": TaskPolicy(
        task="session_state_maintain",
        production_candidates=("openrouter_glm_5_2_gmicloud",),
        eval_candidates=(
            "openrouter_glm_5_2_gmicloud",
            "deepinfra_glm_5_2",
            "lilac_glm_5_2",
            "wafer_glm_5_2",
            "groq_gpt_oss_120b",
        ),
        fallback_aliases=(
            "deepinfra_glm_5_2",
            "lilac_glm_5_2",
            "wafer_glm_5_2",
        ),
        max_attempts=4,
        timeout_seconds=25.0,
        validator="session_state_maintain",
        max_cost_per_call_usd=0.10,
    ),
    "repair": TaskPolicy(
        task="repair",
        production_candidates=("deepinfra_deepseek_v4_flash",),
        eval_candidates=("deepinfra_deepseek_v4_flash",),
        fallback_aliases=(),
        max_attempts=1,
        timeout_seconds=20.0,
        validator="repair",
        max_cost_per_call_usd=0.10,
    ),
}


def get_task_policy(task: str) -> TaskPolicy:
    return TASK_POLICIES[task]


def select_model_for_task(
    task: str,
    *,
    mode: RouteMode = "production",
    required_context_tokens: int = 1,
    require_json: bool = False,
    disabled_providers: set[str] | None = None,
    provider_allowlist: set[str] | None = None,
    estimated_input_tokens: int = 1_000,
    estimated_output_tokens: int = 1_000,
) -> RouteSelection:
    policy = get_task_policy(task)
    disabled_providers = disabled_providers or set()
    candidates: list[ModelSpec] = []
    skipped: list[SkippedModel] = []

    for alias in policy.aliases_for_mode(mode):
        model = MODEL_CATALOG[alias]
        if task not in model.allowed_tasks:
            skipped.append(SkippedModel(alias, "policy_blocked", "task not allowed"))
            continue
        if model.provider in disabled_providers:
            skipped.append(SkippedModel(alias, "policy_blocked", "provider disabled"))
            continue
        if provider_allowlist is not None and model.provider not in provider_allowlist:
            skipped.append(SkippedModel(alias, "policy_blocked", "provider not allowlisted"))
            continue
        if required_context_tokens > model.context_window:
            skipped.append(SkippedModel(alias, "policy_blocked", "context window too small"))
            continue
        if require_json and not model.supports_json:
            skipped.append(SkippedModel(alias, "policy_blocked", "json support required"))
            continue
        candidates.append(model)

    if not candidates:
        return RouteSelection(None, tuple(skipped))

    return RouteSelection(candidates[0], tuple(skipped))


def assert_task_policy_coverage() -> None:
    missing = [task for task in PRODUCTION_TASKS if task not in TASK_POLICIES]
    if missing:
        raise AssertionError(f"Missing LLM task policies: {missing}")
