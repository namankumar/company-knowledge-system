"""
Explicit LLM model-override resolution.

Production routing always goes through a task's TaskPolicy (app/llm/policies.py):
call_llm() runs the policy's production_candidates + fallback_aliases and never
picks a default model on its own. The only way to steer a call to a different
model -- including an Anthropic model -- is one of these three explicit overrides,
checked in this precedence order: per-task env > tenant setting > global env.

app.llm.client enforces that a policy resolution (no override) can never land on
an Anthropic model; only an explicit override may.
"""
from dataclasses import dataclass

from app.core.config import get_settings

ROUTE_POLICY_VERSION = "router-mvp-v1"


@dataclass(frozen=True)
class ModelOverride:
    model: str
    task: str
    override_source: str  # "task" | "tenant" | "global"


def _task_overrides() -> dict[str, str]:
    raw = get_settings().model_override_tasks
    out = {}
    for pair in raw.split(","):
        if "=" in pair:
            k, v = pair.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def resolve_model_override(task: str, tenant_settings: dict | None = None) -> ModelOverride | None:
    """Returns the explicit override for this call, or None if none applies --
    in which case the caller must use the task's TaskPolicy candidate chain."""
    task_override = _task_overrides().get(task)
    if task_override:
        return ModelOverride(model=task_override, task=task, override_source="task")

    tenant_override = (tenant_settings or {}).get("model_override")
    if tenant_override:
        return ModelOverride(model=tenant_override, task=task, override_source="tenant")

    settings = get_settings()
    if settings.model_override_global:
        return ModelOverride(model=settings.model_override_global, task=task, override_source="global")

    return None
