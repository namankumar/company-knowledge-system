"""Current per-user personalization state.

The event log/provenance layer is intentionally not in P0. This module owns the
current state that feature prompts and UI actions can safely read.
"""
import json
from copy import deepcopy

from app.core.db import acquire

DEFAULT_BRIEF_PREFERENCES = {"brief_grouping_enabled": True}
ARRAY_FIELDS = ("priorities", "watchlist", "muted_topics", "important_people", "responsibilities")
CONTEXT_ACTIONS = {"watch", "mute", "make_priority", "not_important", "wrong", "dismiss"}


def _jsonb(value, fallback):
    if value is None:
        return deepcopy(fallback)
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return deepcopy(fallback)
    return deepcopy(value)


def _clean_text(value: str | None) -> str:
    return " ".join(str(value or "").split()).strip()


def _clean_plan_text(value: str | None) -> str:
    lines: list[str] = []
    for raw_line in str(value or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        cleaned = " ".join(raw_line.split()).strip()
        if cleaned:
            lines.append(cleaned)
    return "\n".join(lines).strip()


def _list(value) -> list[str]:
    raw = _jsonb(value, [])
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        cleaned = _clean_text(item)
        if cleaned and cleaned.lower() not in {existing.lower() for existing in out}:
            out.append(cleaned)
    return out[:50]


def _prefs(value) -> dict:
    raw = _jsonb(value, {})
    if not isinstance(raw, dict):
        raw = {}
    return {**DEFAULT_BRIEF_PREFERENCES, **raw}


def normalize_user_context(row: dict | None, *, tenant_id: str, user_id: str) -> dict:
    row = row or {}
    preferences = _prefs(row.get("brief_preferences"))
    return {
        "id": str(row["id"]) if row.get("id") else None,
        "tenant_id": str(row.get("tenant_id") or tenant_id),
        "user_id": str(row.get("user_id") or user_id),
        "priorities": _list(row.get("priorities")),
        "watchlist": _list(row.get("watchlist")),
        "muted_topics": _list(row.get("muted_topics")),
        "important_people": _list(row.get("important_people")),
        "responsibilities": _list(row.get("responsibilities")),
        "brief_preferences": preferences,
        "current_plan": row.get("current_plan"),
        "updated_at": row.get("updated_at").isoformat() if row.get("updated_at") else None,
    }


async def get_user_context(*, tenant_id: str, user_id: str) -> dict:
    async with acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, tenant_id, user_id, priorities, watchlist, muted_topics,
                   important_people, responsibilities, brief_preferences, current_plan, updated_at
            FROM user_contexts
            WHERE tenant_id = $1 AND user_id = $2
            """,
            tenant_id, user_id,
        )
    return normalize_user_context(dict(row) if row else None, tenant_id=tenant_id, user_id=user_id)


async def _record_user_context_event(
    conn,
    *,
    tenant_id: str,
    user_id: str,
    actor_user_id: str | None,
    event_type: str,
    previous_state: dict,
    new_state: dict,
) -> None:
    await conn.execute(
        """
        INSERT INTO user_context_events (
            tenant_id, user_id, event_type, actor_user_id, previous_state, new_state
        )
        VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb)
        """,
        tenant_id,
        user_id,
        event_type,
        actor_user_id,
        json.dumps(previous_state),
        json.dumps(new_state),
    )


async def upsert_user_context(
    *,
    tenant_id: str,
    user_id: str,
    updated_by: str,
    patch: dict,
) -> dict:
    current = await get_user_context(tenant_id=tenant_id, user_id=user_id)
    next_context = {
        **current,
        **{field: _list(patch.get(field, current[field])) for field in ARRAY_FIELDS},
    }
    if "brief_preferences" in patch:
        next_context["brief_preferences"] = {
            **_prefs(current["brief_preferences"]),
            **_prefs(patch.get("brief_preferences")),
        }
    if "current_plan" in patch:
        next_context["current_plan"] = _clean_plan_text(patch.get("current_plan")) or None

    async with acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO user_contexts (
                tenant_id, user_id, priorities, watchlist, muted_topics,
                important_people, responsibilities, brief_preferences, current_plan, updated_by
            )
            VALUES ($1, $2, $3::jsonb, $4::jsonb, $5::jsonb, $6::jsonb, $7::jsonb, $8::jsonb, $9, $10)
            ON CONFLICT (tenant_id, user_id) DO UPDATE
            SET priorities = EXCLUDED.priorities,
                watchlist = EXCLUDED.watchlist,
                muted_topics = EXCLUDED.muted_topics,
                important_people = EXCLUDED.important_people,
                responsibilities = EXCLUDED.responsibilities,
                brief_preferences = EXCLUDED.brief_preferences,
                current_plan = EXCLUDED.current_plan,
                updated_by = EXCLUDED.updated_by,
                updated_at = now()
            RETURNING id, tenant_id, user_id, priorities, watchlist, muted_topics,
                      important_people, responsibilities, brief_preferences, current_plan, updated_at
            """,
            tenant_id,
            user_id,
            json.dumps(next_context["priorities"]),
            json.dumps(next_context["watchlist"]),
            json.dumps(next_context["muted_topics"]),
            json.dumps(next_context["important_people"]),
            json.dumps(next_context["responsibilities"]),
            json.dumps(next_context["brief_preferences"]),
            next_context["current_plan"],
            updated_by,
        )
        normalized = normalize_user_context(dict(row), tenant_id=tenant_id, user_id=user_id)
        await _record_user_context_event(
            conn,
            tenant_id=tenant_id,
            user_id=user_id,
            actor_user_id=updated_by,
            event_type="settings_edit",
            previous_state=current,
            new_state=normalized,
        )
    return normalized


def user_context_bundle(context: dict) -> dict:
    """Small stable object that every feature can inject before synthesis."""
    preferences = _prefs(context.get("brief_preferences"))
    return {
        "priorities": context.get("priorities") or [],
        "watchlist": context.get("watchlist") or [],
        "muted_topics": context.get("muted_topics") or [],
        "important_people": context.get("important_people") or [],
        "responsibilities": context.get("responsibilities") or [],
        "current_plan": context.get("current_plan"),
        "brief_grouping_enabled": bool(preferences.get("brief_grouping_enabled", True)),
    }


def user_context_pack_item(context: dict) -> dict | None:
    bundle = user_context_bundle(context)
    user_id = str(context["user_id"])
    lines = []
    if bundle["priorities"]:
        lines.append(f"Priorities: {', '.join(bundle['priorities'])}.")
    if bundle["watchlist"]:
        lines.append(f"Watchlist: {', '.join(bundle['watchlist'])}.")
    if bundle["muted_topics"]:
        lines.append(f"Muted topics: {', '.join(bundle['muted_topics'])}.")
    if bundle["important_people"]:
        lines.append(f"Important people: {', '.join(bundle['important_people'])}.")
    if bundle["responsibilities"]:
        lines.append(f"Responsibilities: {', '.join(bundle['responsibilities'])}.")
    if bundle["current_plan"]:
        lines.append(f"Current plan: {bundle['current_plan']}.")
    if not lines:
        return None
    return {
        "kind": "user_context",
        "id": user_id,
        "text": "User context bundle for personalization. " + " ".join(lines),
        "source_url": None,
        "priority": 5,
        "score": 1.0,
        "visibility": "private",
        "allowed_principals": [f"user:{user_id}"],
        "acl_state": "stamped",
        "acl_freshness_state": "fresh",
        "acl_basis": "user_owned_personalization_context",
        "acl_confidence": "high",
    }


def apply_brief_action(context: dict, *, action: str, target: str | None, card_key: str | None = None) -> dict:
    if action not in CONTEXT_ACTIONS:
        raise ValueError(f"unsupported action: {action}")
    target_text = _clean_text(target or card_key)
    patch: dict = {}
    preferences = _prefs(context.get("brief_preferences"))
    if action == "watch" and target_text:
        patch["watchlist"] = [*context.get("watchlist", []), target_text]
    elif action == "mute" and target_text:
        patch["muted_topics"] = [*context.get("muted_topics", []), target_text]
    elif action == "make_priority" and target_text:
        patch["priorities"] = [*context.get("priorities", []), target_text]
    elif action == "not_important" and target_text:
        preferences["not_important"] = _list([*(preferences.get("not_important") or []), target_text])
        patch["brief_preferences"] = preferences
    elif action == "dismiss" and card_key:
        preferences["dismissed_card_keys"] = _list([*(preferences.get("dismissed_card_keys") or []), card_key])
        patch["brief_preferences"] = preferences
    elif action == "wrong" and card_key:
        preferences["wrong_card_keys"] = _list([*(preferences.get("wrong_card_keys") or []), card_key])
        patch["brief_preferences"] = preferences
    return patch
