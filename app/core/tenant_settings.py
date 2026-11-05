"""Tenant settings helpers for safety gates and pilot defaults."""
import json

from app.core.db import acquire, _current_tenant_id

DEFAULT_LLM_DAILY_BUDGET_USD = 10.0
DEFAULT_GITHUB_ACCESS_MODE = "pr_metadata"
GITHUB_ACCESS_MODES = {"off", "pr_metadata"}


async def get_tenant_settings(tenant_id: str) -> dict:
    if tenant_id and _current_tenant_id.get() is None:
        _current_tenant_id.set(tenant_id)
    async with acquire() as conn:
        raw = await conn.fetchval("SELECT settings FROM tenants WHERE id = $1", tenant_id)
    if raw is None:
        return {}
    return json.loads(raw) if isinstance(raw, str) else dict(raw)


def autopublish_read_only_enabled(settings: dict) -> bool:
    return settings.get("autopublish_read_only") is not False


def brain_updates_paused(settings: dict) -> bool:
    return bool(settings.get("pause_brain_updates"))


def synthesis_paused(settings: dict) -> bool:
    return bool(settings.get("pause_synthesis"))


def client_publishing_paused(settings: dict) -> bool:
    return permission_shadow_mode_enabled(settings) or bool(settings.get("pause_client_publishing"))


def outbound_delivery_paused(settings: dict) -> bool:
    return permission_shadow_mode_enabled(settings) or bool(settings.get("pause_outbound_delivery"))


def permission_shadow_mode_enabled(settings: dict) -> bool:
    return bool(settings.get("permission_shadow_mode"))


def customer_visible_actions_blocked(settings: dict) -> bool:
    """Permission shadow mode blocks every customer-visible action by default."""
    return permission_shadow_mode_enabled(settings) or (
        client_publishing_paused(settings) and outbound_delivery_paused(settings)
    )


def llm_daily_budget_usd(settings: dict, *, fallback: float | None = None) -> float:
    value = settings.get("llm_daily_budget_usd")
    if value is None:
        return float(DEFAULT_LLM_DAILY_BUDGET_USD if fallback is None else fallback)
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return float(DEFAULT_LLM_DAILY_BUDGET_USD if fallback is None else fallback)
    return max(0.0, parsed)


def github_access_mode(settings: dict) -> str:
    value = str(settings.get("github_access_mode") or "").strip()
    if value in GITHUB_ACCESS_MODES:
        return value
    return DEFAULT_GITHUB_ACCESS_MODE


# Slice 3.2/3.1: threads/sessions. Per the architecture packet's Review
# Verdict #2, dormancy/archive thresholds are code constants with defaults,
# read from tenant settings, with a settings-override path -- not a
# tenant-configurable UI in v1.
DEFAULT_THREAD_DORMANT_AFTER_DAYS = 14
DEFAULT_THREAD_ARCHIVED_AFTER_DAYS = 90

# Slice 3.2: session tool-loop budgets. Code-enforced, not a soft LLM
# instruction -- a turn that exceeds any one of these halts cleanly with a
# partial trace (see app.sessions.loop.LoopBudgetExceeded).
DEFAULT_SESSION_MAX_TOOL_CALLS_PER_TURN = 8
DEFAULT_SESSION_MAX_COST_PER_TURN_USD = 0.50
DEFAULT_SESSION_MAX_WALL_CLOCK_SECONDS_PER_TURN = 60.0


def thread_dormant_after_days(settings: dict) -> int:
    value = settings.get("thread_dormant_after_days")
    try:
        parsed = int(value) if value is not None else DEFAULT_THREAD_DORMANT_AFTER_DAYS
    except (TypeError, ValueError):
        parsed = DEFAULT_THREAD_DORMANT_AFTER_DAYS
    return max(1, parsed)


def thread_archived_after_days(settings: dict) -> int:
    value = settings.get("thread_archived_after_days")
    try:
        parsed = int(value) if value is not None else DEFAULT_THREAD_ARCHIVED_AFTER_DAYS
    except (TypeError, ValueError):
        parsed = DEFAULT_THREAD_ARCHIVED_AFTER_DAYS
    return max(1, parsed)


def session_max_tool_calls_per_turn(settings: dict) -> int:
    value = settings.get("session_max_tool_calls_per_turn")
    try:
        parsed = int(value) if value is not None else DEFAULT_SESSION_MAX_TOOL_CALLS_PER_TURN
    except (TypeError, ValueError):
        parsed = DEFAULT_SESSION_MAX_TOOL_CALLS_PER_TURN
    return max(1, parsed)


def session_max_cost_per_turn_usd(settings: dict) -> float:
    value = settings.get("session_max_cost_per_turn_usd")
    try:
        parsed = float(value) if value is not None else DEFAULT_SESSION_MAX_COST_PER_TURN_USD
    except (TypeError, ValueError):
        parsed = DEFAULT_SESSION_MAX_COST_PER_TURN_USD
    return max(0.0, parsed)


def session_max_wall_clock_seconds_per_turn(settings: dict) -> float:
    value = settings.get("session_max_wall_clock_seconds_per_turn")
    try:
        parsed = float(value) if value is not None else DEFAULT_SESSION_MAX_WALL_CLOCK_SECONDS_PER_TURN
    except (TypeError, ValueError):
        parsed = DEFAULT_SESSION_MAX_WALL_CLOCK_SECONDS_PER_TURN
    return max(1.0, parsed)
