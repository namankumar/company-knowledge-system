from __future__ import annotations

import json
import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Any


LOOP_OBJECT_TYPES = ("commitment", "deadline")
DEFAULT_LOOP_STALE_DAYS = 3
COMPLETION_MARKERS = (
    "done",
    "complete",
    "completed",
    "closed",
    "resolved",
    "merged",
    "shipped",
    "sent",
)
COMPLETION_PATTERN = re.compile(r"\b(?:done|complete|completed|closed|resolved|merged|shipped|sent)\b")


def is_loop_object_type(object_type: str | None) -> bool:
    return str(object_type or "").strip().lower() in LOOP_OBJECT_TYPES


def completion_signal(state: dict[str, Any] | None) -> bool:
    payload = state or {}
    haystack = " ".join(
        str(payload.get(key) or "")
        for key in ("status_detail", "detail", "claim_label")
    ).lower()
    return bool(COMPLETION_PATTERN.search(haystack))


def compute_stale_after(state: dict[str, Any] | None, occurred_at: datetime) -> datetime:
    payload = state or {}
    due_date = parse_due_date(payload.get("due_date"))
    if due_date:
        return datetime.combine(due_date + timedelta(days=1), time.min, tzinfo=timezone.utc)
    return occurred_at.astimezone(timezone.utc) + timedelta(days=DEFAULT_LOOP_STALE_DAYS)


def initial_loop_fields(
    *,
    object_type: str | None,
    state: dict[str, Any] | None,
    occurred_at: datetime,
    source_ref: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not is_loop_object_type(object_type):
        return {}, []
    next_status = "resolved" if completion_signal(state) else "open"
    fields = {
        "loop_status": next_status,
        "loop_opened_at": occurred_at,
        "loop_last_activity_at": occurred_at,
        "loop_resolved_at": occurred_at if next_status == "resolved" else None,
        "loop_resolved_ref": source_ref if next_status == "resolved" else None,
        "loop_stale_after": compute_stale_after(state, occurred_at),
        "loop_reopened_count": 0,
    }
    events = [
        {
            "transition": "opened",
            "cause": "merge_signal",
            "source_refs": [source_ref],
            "previous_status": None,
            "new_status": "open",
        }
    ]
    if next_status == "resolved":
        events.append(
            {
                "transition": "resolved",
                "cause": "merge_signal",
                "source_refs": [source_ref],
                "previous_status": "open",
                "new_status": "resolved",
            }
        )
    return fields, events


def apply_loop_lifecycle(
    *,
    existing: Any,
    merged_state: dict[str, Any] | None,
    new_state: dict[str, Any] | None,
    occurred_at: datetime,
    source_ref: dict[str, Any],
    contradictions: list[tuple[str, Any, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    object_type = row_value(existing, "object_type")
    if not is_loop_object_type(object_type):
        return {}, []

    current_status = str(row_value(existing, "loop_status") or "").strip() or "open"
    next_status = current_status
    events: list[dict[str, Any]] = []
    next_resolved_at = row_value(existing, "loop_resolved_at")
    next_resolved_ref = row_value(existing, "loop_resolved_ref")
    reopened_count = int(row_value(existing, "loop_reopened_count") or 0)
    signal_is_complete = completion_signal(merged_state)
    saw_new_signal = bool(new_state) or bool(contradictions)

    if signal_is_complete:
        if current_status != "resolved":
            next_status = "resolved"
            next_resolved_at = occurred_at
            next_resolved_ref = source_ref
            events.append(
                {
                    "transition": "resolved",
                    "cause": "merge_signal",
                    "source_refs": [source_ref],
                    "previous_status": current_status,
                    "new_status": "resolved",
                }
            )
    else:
        if current_status == "stale" and saw_new_signal:
            next_status = "reopened"
            reopened_count += 1
            next_resolved_at = None
            next_resolved_ref = None
            events.append(
                {
                    "transition": "reopened",
                    "cause": "merge_signal",
                    "source_refs": [source_ref],
                    "previous_status": "stale",
                    "new_status": "reopened",
                }
            )
        elif current_status == "resolved" and contradictions:
            next_status = "reopened"
            reopened_count += 1
            next_resolved_at = None
            next_resolved_ref = None
            events.append(
                {
                    "transition": "reopened",
                    "cause": "contradiction",
                    "source_refs": [source_ref],
                    "previous_status": "resolved",
                    "new_status": "reopened",
                }
            )
        elif current_status == "reopened":
            next_status = "open"

    fields = {
        "loop_status": next_status,
        "loop_opened_at": row_value(existing, "loop_opened_at") or occurred_at,
        "loop_last_activity_at": occurred_at,
        "loop_resolved_at": next_resolved_at,
        "loop_resolved_ref": next_resolved_ref,
        "loop_stale_after": compute_stale_after(merged_state, occurred_at),
        "loop_reopened_count": reopened_count,
    }
    return fields, events


def stale_transition_fields(
    row: Any,
    *,
    occurred_at: datetime,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    if str(row_value(row, "loop_status") or "") not in {"open", "reopened"}:
        return {}, None
    fields = {
        "loop_status": "stale",
        "loop_last_activity_at": row_value(row, "loop_last_activity_at") or occurred_at,
        "loop_opened_at": row_value(row, "loop_opened_at"),
        "loop_resolved_at": row_value(row, "loop_resolved_at"),
        "loop_resolved_ref": row_value(row, "loop_resolved_ref"),
        "loop_stale_after": row_value(row, "loop_stale_after") or occurred_at,
        "loop_reopened_count": int(row_value(row, "loop_reopened_count") or 0),
    }
    event = {
        "transition": "went_stale",
        "cause": "timeout",
        "source_refs": [],
        "previous_status": str(row_value(row, "loop_status") or ""),
        "new_status": "stale",
    }
    return fields, event


async def record_loop_events(
    conn,
    *,
    tenant_id: str,
    object_id: str,
    events: list[dict[str, Any]],
    actor_user_id: str | None = None,
) -> None:
    for event in events:
        await conn.execute(
            """
            INSERT INTO brain_loop_events
                (tenant_id, object_id, transition, cause, source_refs,
                 actor_user_id, previous_status, new_status)
            VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $8)
            """,
            tenant_id,
            object_id,
            event["transition"],
            event["cause"],
            json.dumps(event.get("source_refs") or []),
            actor_user_id,
            event.get("previous_status"),
            event["new_status"],
        )


def row_value(row: Any, key: str) -> Any:
    if row is None:
        return None
    try:
        return row[key]
    except Exception:
        return row.get(key)


def parse_due_date(value: Any) -> date | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None
