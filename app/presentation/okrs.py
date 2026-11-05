"""Founder-facing OKR projection helpers."""
from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from app.core.destination_permissions import filter_visible_sources

MILESTONE_STATUS_SCORES = {
    "not_started": 0.0,
    "not started": 0.0,
    "moving": 25.0,
    "in_progress": 25.0,
    "in progress": 25.0,
    "needs_proof": 40.0,
    "needs proof": 40.0,
    "at_risk": 25.0,
    "at risk": 25.0,
    "overdue": 25.0,
    "blocked": 10.0,
    "done": 100.0,
    "complete": 100.0,
    "completed": 100.0,
    "unknown": None,
}

DONE_STATUSES = {"done", "complete", "completed"}

TV_OKR_FIELDS = {
    "tenant_name",
    "cycle",
    "headline",
    "generated_at",
    "objectives",
    "visible_contribution",
    "source_coverage",
    "summary",
    "accountability",
    "this_week",
}

TV_VISIBLE_CONTRIBUTION_SOURCE_TYPES = {"linear", "github", "hubspot"}
TV_VISIBLE_CONTRIBUTION_SOURCE_WEIGHTS = {
    "github": 0.5,
    "hubspot": 1.0,
    "linear": 0.5,
}
TV_ACCEPTED_CONTRIBUTION_REVIEW_STATUSES = {
    "confirmed",
    "corrected",
    "approved",
    "manager_approved",
    "verified",
}

MANAGER_REVIEW_QUEUE_STATES = {
    "manager_approved": "approved",
    "manager_rejected": "rejected",
}

TV_OBJECTIVE_FIELDS = {
    "id",
    "code",
    "title",
    "final_deadline",
    "percent_complete",
    "confidence",
    "confidence_explanation",
    "state",
    "missing_evidence_count",
    "calculation_method",
    "key_results",
}

TV_KEY_RESULT_FIELDS = {
    "id",
    "code",
    "title",
    "final_deadline",
    "percent_complete",
    "confidence",
    "confidence_explanation",
    "state",
    "first_checkpoint",
    "owner_names",
    "done_count",
    "total_count",
    "source_count",
    "missing_evidence_count",
    "calculation_method",
}

TV_FORBIDDEN_KEYS = {
    "evidence_refs",
    "source_refs",
    "source_chips",
    "mappings",
    "contributions",
    "dri_questions",
    "review_events",
    "manager_review_flags",
    "caveat",
    "note",
    "review_note",
    "correction_reason",
}

TV_FORBIDDEN_STRING_MARKERS = (
    "http://",
    "https://",
    "linear.app/",
    "mail.google.com/",
    "github.com/",
    "/users/",
    "/tmp/",
    "/private/",
    "/var/folders/",
)
TV_FORBIDDEN_STRING_PATTERNS = (
    re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE),
)


def _tv_public_person_name(value: Any) -> str:
    """Render source/person identifiers as public-safe TV labels."""
    text = str(value or "").strip()
    if not text:
        return "Unknown"
    text = re.sub(r"^(email|github|linear|slack|hubspot_owner):", "", text, flags=re.IGNORECASE).strip()
    email_match = TV_FORBIDDEN_STRING_PATTERNS[0].search(text)
    if email_match:
        text = email_match.group(0).split("@", 1)[0]
    elif "@" in text:
        text = text.split("@", 1)[0]
    text = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", text)
    if re.search(r"\bexampleco\b$", text, flags=re.IGNORECASE):
        text = re.sub(r"\bexampleco\b$", "", text, flags=re.IGNORECASE).strip()
    text = re.sub(r"\b[-_. ]*at[-_. ].*$", "", text, flags=re.IGNORECASE).strip()
    text = re.sub(r"[-_.]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return "Team member"
    if text.islower() or text.isupper() or re.search(r"\d", text):
        return " ".join(part.capitalize() for part in text.split())
    return text


def _tv_public_owner_names(value: Any) -> list[str]:
    seen: set[str] = set()
    names: list[str] = []
    for owner in _split_owner_names(value):
        name = _tv_public_person_name(owner)
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        names.append(name)
    return names


def _jsonb(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return default
    return value


def _iso(value: Any) -> str | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _number(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _source_chips(refs: Any, destination_principals: list[str] | None = None) -> list[dict]:
    items = _visible_source_refs(refs, destination_principals)
    chips: list[dict] = []
    seen: set[tuple[str, str | None, str | None, str | None]] = set()
    if not isinstance(items, list):
        return []
    for item in items:
        if not isinstance(item, dict):
            continue
        label = item.get("label") or item.get("source_title") or item.get("source_type") or item.get("ref") or "Source"
        url = item.get("url") or item.get("source_url")
        ref = item.get("ref") or item.get("source_ref")
        source_type = item.get("source_type")
        key = (
            str(label),
            str(url) if url else None,
            str(ref) if ref else None,
            str(source_type) if source_type else None,
        )
        if key in seen:
            continue
        seen.add(key)
        chip = {
            "label": str(label),
            "url": url,
            "ref": ref,
        }
        if source_type:
            chip["source_type"] = source_type
        if _source_ref_is_fixture(item):
            chip["is_fixture"] = True
        chips.append(chip)
    return chips


def _source_ref_is_fixture(ref: dict) -> bool:
    if ref.get("is_fixture") is True:
        return True
    if str(ref.get("fixture") or "").strip().lower() == "exampleco_okr_scoreboard":
        return True
    haystack = " ".join(str(ref.get(key) or "").lower() for key in ("ref", "source_ref", "url", "source_url"))
    return "fixture-exampleco" in haystack or "exampleco-okr-fixture" in haystack


def _visible_source_refs(refs: Any, destination_principals: list[str] | None = None) -> list[dict]:
    items = _jsonb(refs, []) or []
    if not isinstance(items, list):
        return []
    return filter_visible_sources(items, destination_principals)


def _normalize_status(value: Any) -> str:
    return str(value or "unknown").strip().lower().replace("-", "_")


def _milestone_due_date(milestone: dict) -> date | None:
    value = milestone.get("due_date") or milestone.get("date")
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC).date()
    except ValueError:
        pass
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _milestone_due_date_has_passed(milestone: dict) -> bool:
    due_date = _milestone_due_date(milestone)
    return bool(due_date and due_date < datetime.now(UTC).date())


def _row(row: Any) -> dict:
    return dict(row)


def _snapshot_payload(snapshot: dict | None, destination_principals: list[str] | None = None) -> dict | None:
    if not snapshot:
        return None
    return {
        "id": str(snapshot["id"]),
        "state": snapshot.get("state"),
        "percent_complete": _number(snapshot.get("percent_complete")),
        "calculation_method": snapshot.get("calculation_method"),
        "calculation_inputs": _jsonb(snapshot.get("calculation_inputs"), {}) or {},
        "summary": snapshot.get("summary"),
        "changed_reason": snapshot.get("changed_reason"),
        "next_step": snapshot.get("next_step"),
        "evidence_refs": _visible_source_refs(snapshot.get("evidence_refs"), destination_principals),
        "source_chips": _source_chips(snapshot.get("evidence_refs"), destination_principals),
        "missing_evidence": _jsonb(snapshot.get("missing_evidence"), []) or [],
        "confidence": _number(snapshot.get("confidence")),
        "created_at": _iso(snapshot.get("created_at")),
    }


def _planning_payload(kr: dict, destination_principals: list[str] | None = None) -> dict:
    milestones = kr.get("milestones")
    return {
        "trackability_state": kr.get("trackability_state") or "unknown",
        "planning_status": kr.get("planning_status") or "needs_review",
        "definition_of_done": kr.get("definition_of_done"),
        "milestones": [
            _milestone_payload(item, destination_principals)
            for item in (_jsonb(milestones, []) or [])
        ],
        "missing_inputs": _jsonb(kr.get("planning_missing_inputs"), []) or [],
    }


def _mapping_payload(mapping: dict, destination_principals: list[str] | None = None) -> dict:
    return {
        "id": str(mapping["id"]),
        "okr_id": str(mapping["okr_id"]),
        "key_result_id": str(mapping["key_result_id"]) if mapping.get("key_result_id") else None,
        "okr_milestone_id": str(mapping["okr_milestone_id"]) if mapping.get("okr_milestone_id") else None,
        "subject_type": mapping.get("subject_type"),
        "subject_id": mapping.get("subject_id"),
        "relationship_type": mapping.get("relationship_type"),
        "confidence": _number(mapping.get("confidence")),
        "review_status": mapping.get("review_status"),
        "evidence_refs": _visible_source_refs(mapping.get("evidence_refs"), destination_principals),
        "source_chips": _source_chips(mapping.get("evidence_refs"), destination_principals),
        "missing_evidence": _jsonb(mapping.get("missing_evidence"), []) or [],
        "review_note": mapping.get("review_note"),
        "correction_reason": mapping.get("correction_reason"),
    }


def project_single_mapping(mapping: Any, destination_principals: list[str] | None = None) -> dict:
    return _mapping_payload(_row(mapping), destination_principals)


def _milestone_row_as_payload(row: dict) -> dict:
    raw_payload = _jsonb(row.get("raw_payload"), {}) or {}
    if not isinstance(raw_payload, dict):
        raw_payload = {}
    payload = dict(raw_payload)
    payload.update({
        "id": str(row["id"]),
        "milestone_db_id": str(row["id"]),
        "milestone_id": row.get("milestone_key"),
        "milestone_key": row.get("milestone_key"),
        "okr_id": str(row["okr_id"]),
        "key_result_id": str(row["key_result_id"]),
        "title": row.get("title"),
        "owner": row.get("owner"),
        "due_date": row.get("due_date"),
        "definition_of_done": row.get("definition_of_done"),
        "measurement_rule": row.get("measurement_rule"),
        "weight": _number(row.get("weight")),
        "status": row.get("status"),
        "source_refs": _jsonb(row.get("source_refs"), []) or [],
        "evidence_refs": _jsonb(row.get("evidence_refs"), []) or [],
        "missing_evidence": _jsonb(row.get("missing_evidence"), []) or [],
        "confidence": _number(row.get("confidence")),
        "review_status": row.get("review_status"),
        "notes_actual": row.get("notes_actual"),
        "sort_order": row.get("sort_order"),
        "row_type": row.get("row_type"),
        "source_tab": row.get("source_tab"),
        "source_row_index": row.get("source_row_index"),
        "owner_names": _jsonb(row.get("owner_names"), []) or [],
        "strategy_text": row.get("strategy_text"),
        "updated_at": _iso(row.get("updated_at")),
    })
    return payload


def _contribution_payload(
    contribution: dict,
    destination_principals: list[str] | None = None,
    *,
    tv_countable_source_types: set[str] | None = None,
    tv_countable_source_refs: set[str] | None = None,
    tv_allow_fixture_source_refs: bool = False,
) -> dict:
    evidence_refs = _visible_source_refs(contribution.get("evidence_refs"), destination_principals)
    source_chips = _source_chips(contribution.get("evidence_refs"), destination_principals)
    confidence = _number(contribution.get("confidence"))
    person_entity_id = contribution.get("person_entity_id")
    return {
        "id": str(contribution["id"]),
        "okr_id": str(contribution["okr_id"]),
        "key_result_id": str(contribution["key_result_id"]) if contribution.get("key_result_id") else None,
        "person_entity_id": str(person_entity_id) if person_entity_id else None,
        "person_id": str(person_entity_id) if person_entity_id else None,
        "person_name": contribution.get("person_name"),
        "contribution_type": contribution.get("contribution_type"),
        "contribution_summary": contribution.get("contribution_summary"),
        "related_subject_type": contribution.get("related_subject_type"),
        "related_subject_id": contribution.get("related_subject_id"),
        "evidence_refs": evidence_refs,
        "source_chips": source_chips,
        "confidence": confidence,
        "caveat": _contribution_caveat(contribution, source_chips=source_chips, confidence=confidence),
        "tv_countability": _tv_contribution_countability(
            contribution,
            source_chips,
            countable_source_types=tv_countable_source_types,
            countable_source_refs=tv_countable_source_refs,
            allow_fixture_source_refs=tv_allow_fixture_source_refs,
        ),
        "visibility": contribution.get("visibility"),
        "review_status": contribution.get("review_status"),
        "execution_metrics": _jsonb(contribution.get("execution_metrics"), {}) or {},
        "right_work_status": contribution.get("right_work_status"),
        "manager_review_flags": _jsonb(contribution.get("manager_review_flags"), []) or [],
    }


def _contribution_caveat(contribution: dict, *, source_chips: list[dict], confidence: float | None) -> str | None:
    explicit = str(contribution.get("caveat") or "").strip()
    if explicit:
        return explicit
    if not source_chips:
        return "No visible source evidence backs this contribution yet. Missing source coverage can make real work look absent."
    if confidence is None or confidence < 0.75:
        return "This is visible contribution only. Low confidence can mean missing source coverage, unmapped work, or incomplete evidence."
    return None


def _dri_question_payload(question: dict, destination_principals: list[str] | None = None) -> dict:
    return {
        "id": str(question["id"]),
        "okr_id": str(question["okr_id"]),
        "key_result_id": str(question["key_result_id"]) if question.get("key_result_id") else None,
        "recipient": question.get("recipient"),
        "question": question.get("question"),
        "reason": question.get("reason"),
        "missing_evidence": _jsonb(question.get("missing_evidence"), []) or [],
        "evidence_refs": _visible_source_refs(question.get("source_refs"), destination_principals),
        "source_chips": _source_chips(question.get("source_refs"), destination_principals),
        "status": question.get("status"),
        "created_at": _iso(question.get("created_at")),
        "updated_at": _iso(question.get("updated_at")),
    }


def project_okrs(
    okr_rows,
    key_result_rows,
    snapshot_rows,
    mapping_rows,
    contribution_rows,
    dri_question_rows=None,
    review_event_rows=None,
    milestone_rows=None,
    *,
    include_private_contributions: bool,
    destination_principals: list[str] | None = None,
    tv_countable_source_types: set[str] | None = None,
    tv_countable_source_refs: set[str] | None = None,
    tv_allow_fixture_source_refs: bool = False,
) -> list[dict]:
    key_results_by_okr: dict[str, list[dict]] = defaultdict(list)
    for raw in key_result_rows:
        row = _row(raw)
        key_results_by_okr[str(row["okr_id"])].append(row)

    milestones_by_kr: dict[str, list[dict]] = defaultdict(list)
    for raw in milestone_rows or []:
        row = _row(raw)
        key_result_id = str(row.get("key_result_id") or "")
        if key_result_id:
            milestones_by_kr[key_result_id].append(_milestone_row_as_payload(row))

    snapshots_by_target: dict[tuple[str, str | None], dict] = {}
    for raw in snapshot_rows:
        row = _row(raw)
        key = (str(row["okr_id"]), str(row["key_result_id"]) if row.get("key_result_id") else None)
        current = snapshots_by_target.get(key)
        if not current or str(row.get("created_at") or "") > str(current.get("created_at") or ""):
            snapshots_by_target[key] = row

    mappings_by_target: dict[tuple[str, str | None], list[dict]] = defaultdict(list)
    for raw in mapping_rows:
        row = _row(raw)
        key = (str(row["okr_id"]), str(row["key_result_id"]) if row.get("key_result_id") else None)
        mappings_by_target[key].append(row)

    contributions_by_target: dict[tuple[str, str | None], list[dict]] = defaultdict(list)
    for raw in contribution_rows:
        row = _row(raw)
        if row.get("visibility") == "private" and not include_private_contributions:
            continue
        key = (str(row["okr_id"]), str(row["key_result_id"]) if row.get("key_result_id") else None)
        contributions_by_target[key].append(row)

    dri_questions_by_target: dict[tuple[str, str | None], list[dict]] = defaultdict(list)
    for raw in dri_question_rows or []:
        row = _row(raw)
        key = (str(row["okr_id"]), str(row["key_result_id"]) if row.get("key_result_id") else None)
        dri_questions_by_target[key].append(row)

    review_event_rows = [_row(raw) for raw in (review_event_rows or [])]
    review_event_queue_states = _review_event_queue_states(review_event_rows)
    review_events_by_target: dict[tuple[str, str | None], list[dict]] = defaultdict(list)
    for row in review_event_rows:
        key = (str(row["okr_id"]) if row.get("okr_id") else "", str(row["key_result_id"]) if row.get("key_result_id") else None)
        review_events_by_target[key].append(row)

    projected: list[dict] = []
    for raw_okr in okr_rows:
        okr = _row(raw_okr)
        okr_id = str(okr["id"])
        kr_payloads = []
        for kr in key_results_by_okr.get(okr_id, []):
            kr_id = str(kr["id"])
            if milestones_by_kr.get(kr_id):
                kr = {**kr, "milestones": milestones_by_kr[kr_id]}
            key = (okr_id, kr_id)
            kr_payload = {
                "id": kr_id,
                "code": kr.get("code"),
                "sort_order": kr.get("sort_order"),
                "title": kr.get("title"),
                "target_value": _number(kr.get("target_value")),
                "current_value": _number(kr.get("current_value")),
                "unit": kr.get("unit"),
                "calculation_method": kr.get("calculation_method"),
                "first_check": kr.get("first_check"),
                "check_count": kr.get("check_count"),
                "source_tab": kr.get("source_tab"),
                "plan_context": _jsonb(kr.get("plan_context"), {}) or {},
                "status": kr.get("status"),
                "confidence": _number(kr.get("confidence")),
                "source_refs": _visible_source_refs(kr.get("source_refs"), destination_principals),
                "source_chips": _source_chips(kr.get("source_refs"), destination_principals),
                "planning": _planning_payload(kr, destination_principals),
                "snapshot": _snapshot_payload(snapshots_by_target.get(key), destination_principals),
                "mappings": [_mapping_payload(item, destination_principals) for item in mappings_by_target.get(key, [])],
                "contributions": [
                    _contribution_payload(
                        item,
                        destination_principals,
                        tv_countable_source_types=tv_countable_source_types,
                        tv_countable_source_refs=tv_countable_source_refs,
                        tv_allow_fixture_source_refs=tv_allow_fixture_source_refs,
                    )
                    for item in contributions_by_target.get(key, [])
                ],
                "dri_questions": [_dri_question_payload(item, destination_principals) for item in dri_questions_by_target.get(key, [])],
                "review_events": [
                    _review_event_payload(item, destination_principals, queue_states=review_event_queue_states)
                    for item in review_events_by_target.get(key, [])
                ],
                "updated_at": _iso(kr.get("updated_at")),
            }
            _attach_mapped_evidence_to_milestones(kr_payload)
            kr_payload["snapshot"] = _best_kr_snapshot(
                kr_payload.get("snapshot"),
                _source_backed_kr_snapshot(kr_payload),
            )
            kr_payloads.append(kr_payload)
        okr_snapshot = _best_okr_snapshot(
            _snapshot_payload(snapshots_by_target.get((okr_id, None)), destination_principals),
            _source_backed_okr_snapshot(okr, kr_payloads),
        )
        projected.append({
            "id": okr_id,
            "code": okr.get("code"),
            "sort_order": okr.get("sort_order"),
            "title": okr.get("title"),
            "cycle": okr.get("cycle"),
            "status": okr.get("status"),
            "visibility": okr.get("visibility"),
            "confidence": _number(okr.get("confidence")),
            "plan_context": _jsonb(okr.get("plan_context"), {}) or {},
            "source_refs": _visible_source_refs(okr.get("source_refs"), destination_principals),
            "source_chips": _source_chips(okr.get("source_refs"), destination_principals),
            "snapshot": okr_snapshot,
            "key_results": kr_payloads,
            "mappings": [_mapping_payload(item, destination_principals) for item in mappings_by_target.get((okr_id, None), [])],
            "contributions": [
                _contribution_payload(
                    item,
                    destination_principals,
                    tv_countable_source_types=tv_countable_source_types,
                    tv_countable_source_refs=tv_countable_source_refs,
                    tv_allow_fixture_source_refs=tv_allow_fixture_source_refs,
                )
                for item in contributions_by_target.get((okr_id, None), [])
            ],
            "dri_questions": [_dri_question_payload(item, destination_principals) for item in dri_questions_by_target.get((okr_id, None), [])],
            "review_events": [
                _review_event_payload(item, destination_principals, queue_states=review_event_queue_states)
                for item in review_events_by_target.get((okr_id, None), [])
            ],
            "updated_at": _iso(okr.get("updated_at")),
        })
    return projected


def _review_event_payload(
    event: dict,
    destination_principals: list[str] | None = None,
    *,
    queue_states: dict[str, str] | None = None,
) -> dict:
    item = {
        "id": str(event["id"]),
        "okr_id": str(event["okr_id"]) if event.get("okr_id") else None,
        "key_result_id": str(event["key_result_id"]) if event.get("key_result_id") else None,
        "mapping_id": str(event["mapping_id"]) if event.get("mapping_id") else None,
        "contribution_id": str(event["contribution_id"]) if event.get("contribution_id") else None,
        "actor_user_id": str(event["actor_user_id"]) if event.get("actor_user_id") else None,
        "event_type": event.get("event_type"),
        "note": event.get("note"),
        "previous_state": _jsonb(event.get("previous_state"), {}) or {},
        "new_state": _jsonb(event.get("new_state"), {}) or {},
        "source_chips": _source_chips(event.get("source_refs"), destination_principals),
        "created_at": _iso(event.get("created_at")),
    }
    if _is_employee_review_event(event):
        item["queue_state"] = (queue_states or {}).get(str(event["id"]), "pending")
    return item


def _review_event_queue_states(events: list[dict]) -> dict[str, str]:
    employee_events = [event for event in events if _is_employee_review_event(event)]
    manager_events = [
        event for event in events
        if str(event.get("event_type") or "") in MANAGER_REVIEW_QUEUE_STATES
    ]
    states: dict[str, str] = {}
    for employee_event in employee_events:
        employee_id = str(employee_event.get("id") or "")
        resolver = _resolving_manager_review_event(employee_event, manager_events)
        states[employee_id] = (
            MANAGER_REVIEW_QUEUE_STATES[str(resolver.get("event_type"))]
            if resolver
            else "pending"
        )
    return states


def _resolving_manager_review_event(employee_event: dict, manager_events: list[dict]) -> dict | None:
    matches = [
        manager_event for manager_event in manager_events
        if _manager_event_resolves_employee_event(manager_event, employee_event)
    ]
    if not matches:
        return None
    return max(matches, key=_review_event_sort_key)


def _manager_event_resolves_employee_event(manager_event: dict, employee_event: dict) -> bool:
    employee_id = str(employee_event.get("id") or "")
    if _review_event_note_references_event_id(manager_event.get("note"), employee_id):
        return True
    if _review_event_target_key(manager_event) != _review_event_target_key(employee_event):
        return False
    manager_created_at = _review_event_created_at(manager_event)
    employee_created_at = _review_event_created_at(employee_event)
    return bool(manager_created_at and employee_created_at and manager_created_at >= employee_created_at)


def _review_event_note_references_event_id(note: Any, event_id: str) -> bool:
    if not event_id:
        return False
    pattern = rf"(?<![A-Za-z0-9_-]){re.escape(event_id.lower())}(?![A-Za-z0-9_-])"
    return bool(re.search(pattern, str(note or "").lower()))


def _review_event_target_key(event: dict) -> tuple[str, str] | None:
    for field, label in [
        ("contribution_id", "contribution"),
        ("key_result_id", "key_result"),
        ("okr_id", "okr"),
    ]:
        value = str(event.get(field) or "").strip()
        if value:
            return (label, value)
    return None


def _review_event_created_at(event: dict) -> datetime | None:
    value = event.get("created_at")
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _review_event_sort_key(event: dict) -> tuple[datetime, str]:
    return (_review_event_created_at(event) or datetime.min.replace(tzinfo=UTC), str(event.get("id") or ""))


def _is_employee_review_event(event: dict) -> bool:
    return str(event.get("event_type") or "").startswith("employee_")


def _milestone_payload(milestone: Any, destination_principals: list[str] | None = None) -> dict:
    if not isinstance(milestone, dict):
        title = str(milestone or "").strip() or "Unnamed milestone"
        return {
            "title": title,
            "state": "unknown",
            "status": "unknown",
            "percent_complete": None,
            "calculation_method": "milestone status plus milestone source evidence",
            "source_chips": [],
            "missing_evidence": ["Milestone needs structured owner, date, measurement rule, and status."],
            "confidence": None,
        }
    item = dict(milestone)
    status = _normalize_status(item.get("status") or item.get("on_track") or item.get("state"))
    source_chips = _source_chips(item.get("evidence_refs") or item.get("source_refs"), destination_principals)
    score_status = status
    changed_reason = item.get("changed_reason")
    if status in DONE_STATUSES and not _definition_of_done_satisfied(item, source_chips):
        score_status = "needs_proof"
        changed_reason = (
            "Done status needs proof that the milestone definition of done was satisfied "
            "before it can count as complete."
        )
    elif status == "overdue" or _should_mark_milestone_at_risk(item, status, source_chips):
        score_status = "at_risk"
        changed_reason = changed_reason or "Due date has passed without source proof. Progress stays conservative until source evidence or DRI confirmation is added."
    score = MILESTONE_STATUS_SCORES.get(score_status)
    missing = _milestone_missing_evidence(item, source_chips)
    confidence = _milestone_confidence(item, source_chips, missing)
    item.update({
        "state": score_status or "unknown",
        "status": score_status or item.get("status") or "unknown",
        "percent_complete": score,
        "calculation_method": "milestone status plus milestone source evidence",
        "source_chips": source_chips,
        "missing_evidence": missing,
        "confidence": confidence,
    })
    if changed_reason:
        item["changed_reason"] = changed_reason
    return item


def _has_done_confirmation(milestone: dict) -> bool:
    if milestone.get("dri_confirmed") is True or milestone.get("done_confirmed") is True:
        return True
    for field in ("dri_confirmation", "dri_confirmed_at", "confirmed_at", "confirmed_by", "done_confirmed_by"):
        if milestone.get(field):
            return True
    review_status = str(milestone.get("review_status") or "").strip().lower()
    return review_status in {"confirmed", "confirmed_done", "done_confirmed", "manager_confirmed_done"}


def _definition_of_done_satisfied(milestone: dict, source_chips: list[dict]) -> bool:
    if _has_done_confirmation(milestone):
        return True
    for field in (
        "definition_of_done_satisfied",
        "done_criteria_satisfied",
        "completion_criteria_satisfied",
        "measurement_verified",
    ):
        if milestone.get(field) is True:
            return True
    requirement_text = _milestone_completion_requirement_text(milestone)
    if not requirement_text or not source_chips:
        return False
    requirement_tokens = _text_tokens(requirement_text)
    if not requirement_tokens:
        return False
    evidence_tokens = _milestone_evidence_tokens(source_chips)
    if not evidence_tokens:
        return False
    overlap = requirement_tokens & evidence_tokens
    required_overlap = 1 if len(requirement_tokens) <= 2 else 2
    return len(overlap) >= required_overlap


def _milestone_completion_requirement_text(milestone: dict) -> str:
    parts = [
        str(milestone.get("definition_of_done") or "").strip(),
        str(milestone.get("measurement_rule") or "").strip(),
    ]
    return " ".join(part for part in parts if part)


def _milestone_evidence_tokens(source_chips: list[dict]) -> set[str]:
    parts: list[str] = []
    for chip in source_chips:
        if not isinstance(chip, dict):
            continue
        parts.extend(
            str(chip.get(field) or "")
            for field in ("label", "url", "ref", "source_type")
        )
    return _text_tokens(" ".join(parts))


def _should_mark_milestone_at_risk(milestone: dict, status: str, source_chips: list[dict]) -> bool:
    if status in DONE_STATUSES or status in {"blocked", "at_risk", "needs_proof"}:
        return False
    if source_chips or _has_done_confirmation(milestone):
        return False
    return _milestone_due_date_has_passed(milestone)


def _milestone_missing_evidence(milestone: dict, source_chips: list[dict]) -> list[str]:
    missing = []
    if not milestone.get("due_date") and not milestone.get("date"):
        missing.append("Milestone is missing a due date.")
    if not milestone.get("owner"):
        missing.append("Milestone is missing an owner.")
    if not milestone.get("measurement_rule") and not milestone.get("definition_of_done"):
        missing.append("Milestone is missing a measurement rule.")
    if not source_chips:
        missing.append("No milestone-specific source evidence yet.")
    state = _normalize_status(milestone.get("status") or milestone.get("on_track") or milestone.get("state"))
    if state in {"done", "complete", "completed"} and not _definition_of_done_satisfied(milestone, source_chips):
        missing.append("Done status needs proof that the milestone definition of done was satisfied.")
    if _milestone_due_date_has_passed(milestone) and not source_chips and not _has_done_confirmation(milestone):
        missing.append("Milestone is overdue and still missing source proof.")
    return missing


def _milestone_confidence(milestone: dict, source_chips: list[dict], missing: list[str]) -> float:
    base = _number(milestone.get("confidence")) or 0.35
    if source_chips:
        base = max(base, 0.55)
    if missing:
        base = min(base, 0.65)
    if not source_chips:
        base = min(base, 0.45)
    return round(base, 2)


def _attach_mapped_evidence_to_milestones(kr: dict) -> None:
    planning = kr.get("planning") or {}
    milestones = planning.get("milestones") or []
    if not milestones:
        return
    mappings = kr.get("mappings") or []
    for milestone in milestones:
        if not isinstance(milestone, dict):
            continue
        matched_chips = []
        for mapping in mappings:
            if _mapping_mentions_milestone(mapping, milestone):
                matched_chips.extend(mapping.get("source_chips") or [])
        if not matched_chips:
            continue
        milestone["source_chips"] = _dedupe_chips(list(milestone.get("source_chips") or []) + matched_chips)
        milestone["missing_evidence"] = [
            item for item in (milestone.get("missing_evidence") or [])
            if item != "No milestone-specific source evidence yet."
        ]
        blocking_mappings = [
            mapping for mapping in mappings
            if _mapping_mentions_milestone(mapping, milestone) and mapping.get("relationship_type") == "blocks"
        ]
        if blocking_mappings:
            milestone["percent_complete"] = MILESTONE_STATUS_SCORES["blocked"]
            milestone["state"] = "blocked"
            milestone["status"] = "blocked"
            milestone["changed_reason"] = "Mapped source evidence indicates this milestone is blocked. Progress stays conservative until the blocker is resolved."
        if milestone.get("percent_complete") is None or milestone.get("percent_complete") == 0:
            milestone["percent_complete"] = MILESTONE_STATUS_SCORES["moving"]
            milestone["state"] = "moving"
            milestone["status"] = "moving"
            milestone["changed_reason"] = "Mapped source evidence suggests this milestone has started. Done still needs proof or DRI confirmation."
        milestone["confidence"] = max(_number(milestone.get("confidence")) or 0.0, _average([
            mapping.get("confidence") for mapping in mappings
            if _mapping_mentions_milestone(mapping, milestone)
        ]) or 0.0)
        if milestone["missing_evidence"]:
            milestone["confidence"] = min(milestone["confidence"], 0.65)
        milestone["confidence"] = round(milestone["confidence"], 2)


def _mapping_mentions_milestone(mapping: dict, milestone: dict) -> bool:
    milestone_id = str(milestone.get("milestone_id") or milestone.get("id") or "").strip()
    milestone_title = str(milestone.get("title") or milestone.get("milestone") or "").strip().lower()
    for ref in mapping.get("evidence_refs") or []:
        if not isinstance(ref, dict):
            continue
        matched_id = str(ref.get("matched_milestone_id") or "").strip()
        if milestone_id and matched_id and matched_id == milestone_id:
            return True
        matched_title = str(ref.get("matched_milestone_title") or "").strip().lower()
        if milestone_title and matched_title and matched_title == milestone_title:
            return True
    milestone_tokens = _text_tokens(" ".join(str(value or "") for value in [
        milestone.get("title"),
        milestone.get("milestone"),
        milestone.get("measurement_rule"),
        milestone.get("definition_of_done"),
    ]))
    mapping_tokens = _text_tokens(" ".join(str(value or "") for value in [
        mapping.get("subject_type"),
        mapping.get("subject_id"),
        mapping.get("relationship_type"),
        " ".join(str(chip.get("label") or "") for chip in (mapping.get("source_chips") or [])),
    ]))
    return bool(milestone_tokens and mapping_tokens and len(milestone_tokens & mapping_tokens) >= 2)


def _text_tokens(text: str) -> set[str]:
    return {
        token for token in "".join(ch.lower() if ch.isalnum() else " " for ch in text).split()
        if len(token) > 2
    }


def _source_backed_kr_snapshot(kr: dict) -> dict | None:
    score = _score_from_milestones(kr)
    if score is None:
        return None
    planning = kr.get("planning") or {}
    milestones = [item for item in (planning.get("milestones") or []) if isinstance(item, dict)]
    source_chips = _dedupe_chips(_mapped_source_chips_for_kr(kr))
    missing = _missing_evidence_for_kr(kr)
    if not source_chips:
        missing.append("No mapped source evidence yet.")
    state = _state_from_milestones(kr)
    confidence = _synthetic_confidence(kr, source_chips, missing)
    return {
        "id": f"computed:{kr.get('id')}",
        "state": state,
        "percent_complete": score,
        "calculation_method": kr.get("calculation_method") or "milestone status plus mapped source evidence",
        "calculation_inputs": {
            "milestone_count": len(milestones),
            "known_milestone_count": len([
                item for item in milestones
                if MILESTONE_STATUS_SCORES.get(_normalize_status(item.get("status") or item.get("on_track") or item.get("state"))) is not None
            ]),
            "source_types": sorted(_chip_source_types(source_chips)),
            "status_score_scale": "not_started 0, moving 25, needs_proof 40, at_risk 25, blocked 10, done 100",
        },
        "summary": _synthetic_summary(kr, score, state),
        "changed_reason": "Computed from the approved milestone plan and mapped source evidence because no stored progress snapshot exists yet.",
        "next_step": _synthetic_next_step(missing),
        "evidence_refs": [],
        "source_chips": source_chips,
        "missing_evidence": sorted(set(missing)),
        "confidence": confidence,
        "created_at": None,
    }


def _best_kr_snapshot(stored: dict | None, source_backed: dict | None) -> dict | None:
    if source_backed is None:
        return stored
    if stored is None:
        return source_backed
    if _snapshot_has_source_evidence(stored):
        return stored
    if _snapshot_has_source_evidence(source_backed):
        return source_backed
    return stored


def _best_okr_snapshot(stored: dict | None, source_backed: dict | None) -> dict | None:
    return _best_kr_snapshot(stored, source_backed)


def _snapshot_has_source_evidence(snapshot: dict) -> bool:
    if snapshot.get("source_chips"):
        return True
    return bool(_visible_source_refs(snapshot.get("evidence_refs"), None))


def _source_backed_okr_snapshot(okr: dict, key_results: list[dict]) -> dict | None:
    score = _average([(kr.get("snapshot") or {}).get("percent_complete") for kr in key_results])
    if score is None:
        return None
    missing = sorted({
        item
        for kr in key_results
        for item in ((kr.get("snapshot") or {}).get("missing_evidence") or [])
    })
    confidence = _average([(kr.get("snapshot") or {}).get("confidence") for kr in key_results])
    states = [(kr.get("snapshot") or {}).get("state") for kr in key_results]
    state = _rollup_state(states) or okr.get("status") or "unknown"
    return {
        "id": f"computed:{okr.get('id')}",
        "state": state,
        "percent_complete": score,
        "calculation_method": "weighted average of key result progress",
        "calculation_inputs": {
            "key_result_count": len(key_results),
            "scored_key_result_count": len([
                kr for kr in key_results
                if (kr.get("snapshot") or {}).get("percent_complete") is not None
            ]),
        },
        "summary": f"{okr.get('title') or 'Objective'} is {score}% complete based on scored key results.",
        "changed_reason": "Computed from key result progress and mapped source evidence.",
        "next_step": _synthetic_next_step(missing),
        "evidence_refs": [],
        "source_chips": _dedupe_chips([
            chip
            for kr in key_results
            for chip in ((kr.get("snapshot") or {}).get("source_chips") or [])
        ]),
        "missing_evidence": missing,
        "confidence": confidence,
        "created_at": None,
    }


def _mapped_source_chips_for_kr(kr: dict) -> list[dict]:
    chips = list(kr.get("source_chips") or [])
    for mapping in kr.get("mappings") or []:
        chips.extend(mapping.get("source_chips") or [])
    return chips


def _missing_evidence_for_kr(kr: dict) -> list[str]:
    planning = kr.get("planning") or {}
    missing = list(planning.get("missing_inputs") or [])
    for mapping in kr.get("mappings") or []:
        missing.extend(mapping.get("missing_evidence") or [])
    return [str(item) for item in missing if item]


def _synthetic_confidence(kr: dict, source_chips: list[dict], missing: list[str]) -> float:
    values = [_number(kr.get("confidence"))]
    values.extend(_number(mapping.get("confidence")) for mapping in (kr.get("mappings") or []))
    base = _average(values) or 0.35
    if not source_chips:
        base = min(base, 0.35)
    if missing:
        base = min(base, 0.65)
    return round(base, 2)


def _synthetic_summary(kr: dict, score: float, state: str) -> str:
    return f"{kr.get('title') or 'Key result'} is {score}% complete from milestone status and mapped source evidence. Current state: {state}."


def _synthetic_next_step(missing: list[str]) -> str | None:
    if missing:
        return f"Add or confirm evidence for: {missing[0]}"
    return "Keep source mappings current so the score can be verified."


def _rollup_state(states: list[Any]) -> str | None:
    normalized = {_normalize_status(state) for state in states if state}
    if not normalized:
        return None
    for state in ["blocked", "at_risk", "needs_proof", "moving"]:
        if state in normalized:
            return state
    if normalized == {"done"}:
        return "done"
    return sorted(normalized)[0]


def project_okrs_for_employee(
    okrs: list[dict],
    *,
    person_name: str | None,
    email: str | None = None,
    user_id: str | None = None,
    person_entity_ids: set[str] | list[str] | tuple[str, ...] | None = None,
) -> list[dict]:
    """Return member-safe OKRs with only the actor's own contribution details."""
    names = _person_match_keys(person_name, email)
    entity_ids = _person_entity_match_keys(person_entity_ids)
    actor_user_id = str(user_id or "").strip()
    output = []
    for okr in okrs:
        next_okr = _without_manager_fields(okr)
        next_okr["contributions"] = [
            _employee_contribution(item)
            for item in okr.get("contributions") or []
            if _matches_person(item, names, entity_ids)
        ]
        next_okr["mappings"] = []
        next_okr["dri_questions"] = []
        next_okr["review_events"] = [
            _employee_review_event(item) for item in (okr.get("review_events") or [])
            if _is_employee_review_event_visible_to_actor(item, actor_user_id)
        ]
        next_krs = []
        for kr in okr.get("key_results") or []:
            next_kr = _without_manager_fields(kr)
            next_kr["contributions"] = [
                _employee_contribution(item)
                for item in kr.get("contributions") or []
                if _matches_person(item, names, entity_ids)
            ]
            next_kr["mappings"] = []
            next_kr["dri_questions"] = []
            next_kr["review_events"] = [
                _employee_review_event(item) for item in (kr.get("review_events") or [])
                if _is_employee_review_event_visible_to_actor(item, actor_user_id)
            ]
            next_krs.append(next_kr)
        next_okr["key_results"] = next_krs
        output.append(next_okr)
    return output


def project_tv_scoreboard(
    okrs: list[dict],
    *,
    tenant_name: str = "Company",
    generated_at: datetime | None = None,
    countable_source_types: set[str] | list[str] | tuple[str, ...] | None = None,
    countable_source_refs: set[str] | list[str] | tuple[str, ...] | None = None,
    allow_fixture_source_refs: bool = False,
) -> dict:
    """Create the one shared, public-safe team projection for the office TV."""
    generated = generated_at or datetime.now(UTC)
    countable = _normalize_countable_source_types(countable_source_types)
    countable_refs = _normalize_countable_source_refs(countable_source_refs)
    objectives = [
        _tv_objective(
            okr,
            objective_index=index,
            kr_start_index=sum(len(previous.get("key_results") or []) for previous in okrs[:index]),
            generated=generated,
            countable_source_types=countable,
            countable_source_refs=countable_refs,
            allow_fixture_source_refs=allow_fixture_source_refs,
        )
        for index, okr in enumerate(okrs)
    ]
    scoreboard = {
        "tenant_name": tenant_name,
        "cycle": _first_non_empty([okr.get("cycle") for okr in okrs]),
        "headline": "OKRs are on the board. Now every week should move the numbers.",
        "generated_at": _iso(generated),
        "objectives": objectives,
        "visible_contribution": _tv_visible_contribution(
            okrs,
            countable_source_types=countable,
            countable_source_refs=countable_refs,
            allow_fixture_source_refs=allow_fixture_source_refs,
        ),
        "source_coverage": _tv_source_coverage(
            okrs,
            countable_source_types=countable,
            countable_source_refs=countable_refs,
            allow_fixture_source_refs=allow_fixture_source_refs,
        ),
        "summary": _tv_summary(okrs, objectives, generated=generated),
        "accountability": _tv_accountability(okrs),
        "this_week": _tv_this_week(okrs, generated=generated),
    }
    _assert_tv_payload_safe(scoreboard)
    return scoreboard


def _normalize_countable_source_types(source_types: set[str] | list[str] | tuple[str, ...] | None) -> set[str] | None:
    if source_types is None:
        return None
    return {str(source_type).strip().lower() for source_type in source_types if str(source_type).strip()}


def _normalize_countable_source_refs(source_refs: set[str] | list[str] | tuple[str, ...] | None) -> set[str] | None:
    if source_refs is None:
        return None
    return {str(source_ref).strip() for source_ref in source_refs if str(source_ref).strip()}


def _without_manager_fields(item: dict) -> dict:
    stripped = dict(item)
    stripped.pop("manager_review_flags", None)
    stripped.pop("dri_questions", None)
    stripped.pop("review_events", None)
    return stripped


def _employee_contribution(contribution: dict) -> dict:
    item = dict(contribution)
    item.pop("manager_review_flags", None)
    item["caveat"] = _contribution_caveat(
        item,
        source_chips=item.get("source_chips") or [],
        confidence=_number(item.get("confidence")),
    )
    return item


def _employee_review_event(event: dict) -> dict:
    item = dict(event)
    item.pop("actor_user_id", None)
    item["source_chips"] = []
    return item


def _is_employee_review_event_visible_to_actor(event: dict, actor_user_id: str) -> bool:
    if not str(event.get("event_type") or "").startswith("employee_"):
        return False
    event_actor_id = str(event.get("actor_user_id") or "").strip()
    return bool(actor_user_id) and bool(event_actor_id) and event_actor_id == actor_user_id


def _person_match_keys(person_name: str | None, email: str | None) -> set[str]:
    keys = {str(person_name or "").strip().lower(), str(email or "").strip().lower()}
    if email and "@" in email:
        keys.add(email.split("@", 1)[0].strip().lower())
    return {key for key in keys if key}


def _person_entity_match_keys(person_entity_ids: set[str] | list[str] | tuple[str, ...] | None) -> set[str]:
    return {str(entity_id).strip() for entity_id in (person_entity_ids or []) if str(entity_id).strip()}


def _matches_person(contribution: dict, names: set[str], entity_ids: set[str] | None = None) -> bool:
    contribution_entity_id = str(
        contribution.get("person_entity_id") or contribution.get("person_id") or ""
    ).strip()
    if contribution_entity_id:
        return bool(entity_ids) and contribution_entity_id in entity_ids
    if not names:
        return False
    person = str(contribution.get("person_name") or "").strip().lower()
    return person in names


def _tv_objective(
    okr: dict,
    *,
    objective_index: int = 0,
    kr_start_index: int = 0,
    generated: datetime | None = None,
    countable_source_types: set[str] | None = None,
    countable_source_refs: set[str] | None = None,
    allow_fixture_source_refs: bool = False,
) -> dict:
    snapshot = okr.get("snapshot") or {}
    key_results = [
        _tv_key_result(
            kr,
            code=kr.get("code") or f"KR{kr_start_index + key_result_index + 1}",
            generated=generated,
            countable_source_types=countable_source_types,
            countable_source_refs=countable_source_refs,
            allow_fixture_source_refs=allow_fixture_source_refs,
        )
        for key_result_index, kr in enumerate(okr.get("key_results") or [])
    ]
    percent = _number(snapshot.get("percent_complete"))
    if percent is None:
        percent = _average([kr.get("percent_complete") for kr in key_results])
    missing = set(snapshot.get("missing_evidence") or [])
    for kr in key_results:
        if kr.get("missing_evidence_count"):
            missing.add(f"kr:{kr.get('id')}:missing_evidence")
    objective = {
        "id": okr.get("id"),
        "code": okr.get("code"),
        "title": okr.get("title"),
        "final_deadline": okr.get("final_deadline"),
        "percent_complete": percent,
        "confidence": _number(snapshot.get("confidence") if snapshot else okr.get("confidence")),
        "state": snapshot.get("state") or okr.get("status") or "unknown",
        "missing_evidence_count": len(missing),
        "calculation_method": snapshot.get("calculation_method") or "weighted average of key result progress",
        "key_results": key_results,
    }
    objective["confidence_explanation"] = _tv_confidence_explanation(
        confidence=objective["confidence"],
        source_count=sum(kr.get("source_count") or 0 for kr in key_results),
        missing_evidence_count=objective["missing_evidence_count"],
        unit="objective",
    )
    return _allowlist(objective, TV_OBJECTIVE_FIELDS)


def _tv_key_result(
    kr: dict,
    *,
    code: str | None = None,
    generated: datetime | None = None,
    countable_source_types: set[str] | None = None,
    countable_source_refs: set[str] | None = None,
    allow_fixture_source_refs: bool = False,
) -> dict:
    snapshot = kr.get("snapshot") or {}
    score = _number(snapshot.get("percent_complete")) if snapshot else None
    if score is None:
        score = _score_from_milestones(kr)
    missing = set(snapshot.get("missing_evidence") or [])
    planning = kr.get("planning") or {}
    missing.update(planning.get("missing_inputs") or [])
    for milestone in planning.get("milestones") or []:
        if isinstance(milestone, dict):
            missing.update(milestone.get("missing_evidence") or [])
    source_types = _source_types_for_kr(
        kr,
        countable_source_types=countable_source_types,
        countable_source_refs=countable_source_refs,
        allow_fixture_source_refs=allow_fixture_source_refs,
    )
    item = {
        "id": kr.get("id"),
        "code": kr.get("code") or code,
        "title": kr.get("title"),
        "final_deadline": kr.get("final_deadline"),
        "percent_complete": score,
        "confidence": _number(snapshot.get("confidence") if snapshot else kr.get("confidence")),
        "state": snapshot.get("state") or kr.get("status") or _state_from_milestones(kr),
        "first_checkpoint": _first_checkpoint(planning.get("milestones") or []),
        "owner_names": _milestone_owner_names(planning.get("milestones") or []),
        "done_count": _milestone_done_count(planning.get("milestones") or []),
        "total_count": _milestone_total_count(planning.get("milestones") or []),
        "source_count": len(source_types),
        "missing_evidence_count": len(missing),
        "calculation_method": snapshot.get("calculation_method") or kr.get("calculation_method") or "milestone status plus mapped source evidence",
    }
    item["confidence_explanation"] = _tv_confidence_explanation(
        confidence=item["confidence"],
        source_count=item["source_count"],
        missing_evidence_count=item["missing_evidence_count"],
        unit="KR",
    )
    return _allowlist(item, TV_KEY_RESULT_FIELDS)


def _score_from_milestones(kr: dict) -> float | None:
    planning = kr.get("planning") or {}
    milestones = planning.get("milestones") or []
    scores = []
    for milestone in milestones:
        if not isinstance(milestone, dict):
            continue
        status = _normalize_status(milestone.get("status") or milestone.get("on_track") or milestone.get("state"))
        score = MILESTONE_STATUS_SCORES.get(status)
        if score is None and milestone.get("source_chips"):
            score = MILESTONE_STATUS_SCORES["needs_proof"]
        if score is not None:
            scores.append(score)
    return _average(scores)


def _state_from_milestones(kr: dict) -> str:
    planning = kr.get("planning") or {}
    statuses = [
        _normalize_status(milestone.get("status") or milestone.get("on_track") or milestone.get("state"))
        for milestone in (planning.get("milestones") or [])
        if isinstance(milestone, dict)
    ]
    if "blocked" in statuses:
        return "blocked"
    if "at_risk" in statuses or "overdue" in statuses:
        return "at_risk"
    if "needs_proof" in statuses:
        return "needs_proof"
    if "moving" in statuses or "in_progress" in statuses:
        return "moving"
    if statuses and all(status in {"done", "complete", "completed"} for status in statuses):
        return "done"
    return kr.get("status") or "unknown"


def _first_checkpoint(milestones: list) -> str | None:
    due_dates = sorted(
        str(milestone.get("due_date") or milestone.get("date") or "").strip()
        for milestone in milestones
        if isinstance(milestone, dict) and (milestone.get("due_date") or milestone.get("date"))
    )
    return due_dates[0] if due_dates else None


def _milestone_owner_names(milestones: list) -> list[str]:
    seen = set()
    names = []
    for milestone in milestones:
        if not isinstance(milestone, dict):
            continue
        for owner in _tv_public_owner_names(milestone.get("owner")):
            if owner in seen:
                continue
            seen.add(owner)
            names.append(owner)
    return names


def _milestone_done_count(milestones: list) -> int:
    return sum(
        1
        for milestone in milestones
        if isinstance(milestone, dict)
        and _normalize_status(milestone.get("status") or milestone.get("on_track") or milestone.get("state")) in DONE_STATUSES
    )


def _milestone_total_count(milestones: list) -> int:
    return sum(1 for milestone in milestones if isinstance(milestone, dict))


def _source_types_for_kr(
    kr: dict,
    *,
    countable_source_types: set[str] | None = None,
    countable_source_refs: set[str] | None = None,
    allow_fixture_source_refs: bool = False,
) -> set[str]:
    chips = list(kr.get("source_chips") or [])
    snapshot = kr.get("snapshot") or {}
    chips.extend(snapshot.get("source_chips") or [])
    for mapping in kr.get("mappings") or []:
        chips.extend(mapping.get("source_chips") or [])
    for contribution in kr.get("contributions") or []:
        if contribution.get("visibility") != "company":
            continue
        chips.extend(contribution.get("source_chips") or [])
    return _chip_source_types(
        chips,
        countable_source_types=countable_source_types,
        countable_source_refs=countable_source_refs,
        allow_fixture_source_refs=allow_fixture_source_refs,
    )


def _tv_visible_contribution(
    okrs: list[dict],
    *,
    countable_source_types: set[str] | None = None,
    countable_source_refs: set[str] | None = None,
    allow_fixture_source_refs: bool = False,
) -> list[dict]:
    by_person: dict[str, dict] = {}
    total_weighted_items = 0.0
    saw_any_contribution_rows = False
    for okr in okrs:
        contributions = list(okr.get("contributions") or [])
        for kr in okr.get("key_results") or []:
            contributions.extend(kr.get("contributions") or [])
        for contribution in contributions:
            saw_any_contribution_rows = True
            tv_countability = _tv_contribution_countability(
                contribution,
                contribution.get("source_chips") or [],
                countable_source_types=countable_source_types,
                countable_source_refs=countable_source_refs,
                allow_fixture_source_refs=allow_fixture_source_refs,
            )
            if not tv_countability["countable"]:
                continue
            source_chips = _countable_source_chips_for_tv(
                contribution.get("source_chips") or [],
                countable_source_types=countable_source_types,
                countable_source_refs=countable_source_refs,
                allow_fixture_source_refs=allow_fixture_source_refs,
            )
            if not source_chips:
                continue
            weight = _tv_contribution_weight(contribution, source_chips)
            person = _tv_public_person_name(contribution.get("person_name") or "Unknown")
            item = by_person.setdefault(person, {
                "person_name": person,
                "counted_items": 0,
                "weighted_counted_items": 0.0,
                "visible_work_share_percent": 0,
                "confidence": 0.0,
                "source_count": 0,
                "missing_evidence_count": 0,
                "confidence_explanation": "",
                "calculation_method": "weighted share of source-visible counted contribution rows",
            })
            item["counted_items"] += 1
            item["weighted_counted_items"] = round(float(item["weighted_counted_items"]) + weight, 2)
            total_weighted_items += weight
            item["confidence"] = max(item["confidence"], _number(contribution.get("confidence")) or 0.0)
            item["source_count"] += len(source_chips)
            item["missing_evidence_count"] += _tv_contribution_missing_evidence_count(contribution, source_chips)
    if total_weighted_items:
        for item in by_person.values():
            item["visible_work_share_percent"] = round((item["weighted_counted_items"] / total_weighted_items) * 100)
            item["confidence_explanation"] = _tv_confidence_explanation(
                confidence=item["confidence"],
                source_count=item["source_count"],
                missing_evidence_count=item["missing_evidence_count"],
                unit="contribution",
                source_label="source-backed item",
            )
    published = sorted(by_person.values(), key=lambda item: (-item["weighted_counted_items"], -item["counted_items"], item["person_name"]))[:8]
    if published:
        return published
    if saw_any_contribution_rows:
        return []
    return _tv_visible_contribution_from_milestones(okrs)


def _tv_visible_contribution_from_milestones(okrs: list[dict]) -> list[dict]:
    by_person: dict[str, dict] = {}
    for _, _, milestone in _tv_milestones(okrs):
        owners = _tv_public_owner_names(milestone.get("owner"))
        if not owners:
            continue
        status = _normalize_status(milestone.get("status") or milestone.get("on_track") or milestone.get("state"))
        is_done = status in DONE_STATUSES
        source_count = len(milestone.get("source_chips") or [])
        missing_count = len(milestone.get("missing_evidence") or [])
        confidence = _number(milestone.get("confidence")) or 0.0
        for owner in owners:
            item = by_person.setdefault(owner, {
                "person_name": owner,
                "counted_items": 0,
                "weighted_counted_items": 0,
                "visible_work_share_percent": 0,
                "confidence": 0.0,
                "source_count": 0,
                "missing_evidence_count": 0,
                "confidence_explanation": "",
                "calculation_method": "milestone completion share from OKR owners and milestone states",
            })
            item["weighted_counted_items"] += 1
            if is_done:
                item["counted_items"] += 1
            item["confidence"] = max(item["confidence"], confidence)
            item["source_count"] += source_count
            item["missing_evidence_count"] += missing_count
    results = []
    for item in by_person.values():
        assigned = int(item["weighted_counted_items"] or 0)
        done = int(item["counted_items"] or 0)
        item["visible_work_share_percent"] = round((done / assigned) * 100) if assigned else 0
        item["confidence_explanation"] = _tv_confidence_explanation(
            confidence=item["confidence"],
            source_count=item["source_count"],
            missing_evidence_count=item["missing_evidence_count"],
            unit="contribution",
            source_label="source-backed item",
        )
        results.append(item)
    return sorted(results, key=lambda item: (-item["visible_work_share_percent"], -item["weighted_counted_items"], item["person_name"]))[:8]


def _tv_summary(okrs: list[dict], objectives: list[dict], *, generated: datetime) -> dict:
    all_key_results = [kr for okr in okrs for kr in (okr.get("key_results") or [])]
    done_percent = round(_average([objective.get("percent_complete") for objective in objectives]) or 0)
    on_track_count = sum(
        1
        for kr in all_key_results
        if str((kr.get("snapshot") or {}).get("state") or kr.get("status") or "").strip().lower() in {"moving", "needs_proof"}
    )
    at_risk_count = sum(
        1
        for kr in all_key_results
        if str((kr.get("snapshot") or {}).get("state") or kr.get("status") or "").strip().lower() in {"at_risk", "blocked", "needs_review", "overdue"}
    )
    deadlines = []
    for okr in okrs:
        if okr.get("final_deadline"):
            parsed = _coerce_date(okr.get("final_deadline"))
            if parsed:
                deadlines.append(parsed)
        for kr in okr.get("key_results") or []:
            parsed = _coerce_date(kr.get("final_deadline"))
            if parsed:
                deadlines.append(parsed)
            for milestone in (kr.get("planning") or {}).get("milestones") or []:
                if isinstance(milestone, dict):
                    parsed = _coerce_date(milestone.get("due_date") or milestone.get("date"))
                    if parsed:
                        deadlines.append(parsed)
    final_deadline = max(deadlines) if deadlines else None
    return {
        "heading": build_tv_heading(okrs),
        "days_to_ship": (final_deadline - generated.date()).days if final_deadline else None,
        "final_deadline_label": _format_short_date(final_deadline) if final_deadline else None,
        "done_percent": done_percent,
        "on_track_count": on_track_count,
        "at_risk_count": at_risk_count,
    }


def _tv_accountability(okrs: list[dict]) -> dict:
    missing_owner = []
    owner_load: defaultdict[str, int] = defaultdict(int)
    for kr_code, _, milestone in _tv_milestones(okrs):
        owners = _tv_public_owner_names(milestone.get("owner"))
        if owners:
            for owner in owners:
                owner_load[owner] += 1
        elif _normalize_status(milestone.get("status") or milestone.get("on_track") or milestone.get("state")) not in DONE_STATUSES:
            missing_owner.append(kr_code)
    top_owner = max(owner_load.items(), key=lambda item: item[1], default=None)
    detail = None
    if missing_owner:
        preview = ", ".join(missing_owner[:2])
        extra = max(0, len(missing_owner) - 2)
        detail = preview if not extra else f"{preview} (+{extra})"
    tail = None
    if top_owner:
        tail = f"{top_owner[0]} on {top_owner[1]} milestones"
    return {
        "count": len(missing_owner),
        "detail": detail,
        "tail": tail,
    }


def _tv_this_week(okrs: list[dict], *, generated: datetime) -> list[dict]:
    today = generated.date()
    week_end = today.fromordinal(today.toordinal() + 7)
    items = []
    for kr_code, kr_title, milestone in _tv_milestones(okrs):
        due = _coerce_date(milestone.get("due_date") or milestone.get("date"))
        if not due or due < today or due >= week_end:
            continue
        owners = _tv_public_owner_names(milestone.get("owner"))
        lead_owner = owners[0] if owners else "Unassigned"
        status = _normalize_status(milestone.get("status") or milestone.get("on_track") or milestone.get("state"))
        days_until_due = (due - today).days
        items.append({
            "sort_due": due,
            "label": f"{_format_short_date(due)} - {kr_code} - {lead_owner}",
            "text": str(milestone.get("title") or kr_title or "").strip(),
            "color": "warn" if days_until_due <= 3 and status not in DONE_STATUSES else _tv_status_color_token(status),
        })
    items.sort(key=lambda item: (item["sort_due"], item["label"]))
    return [{"label": item["label"], "text": item["text"], "color": item["color"]} for item in items[:8]]


def _tv_milestones(okrs: list[dict]) -> list[tuple[str, str, dict]]:
    items: list[tuple[str, str, dict]] = []
    kr_index = 0
    for okr in okrs:
        for kr in okr.get("key_results") or []:
            kr_index += 1
            kr_code = f"KR{kr_index}"
            kr_title = str(kr.get("title") or "").strip()
            for milestone in (kr.get("planning") or {}).get("milestones") or []:
                if isinstance(milestone, dict):
                    items.append((kr_code, kr_title, milestone))
    return items


def _split_owner_names(value: Any) -> list[str]:
    raw = str(value or "").strip()
    if not raw:
        return []
    parts = re.split(r"\s*[+,/]\s*", raw)
    return [part.strip() for part in parts if part.strip()]


def _coerce_date(value: Any) -> date | None:
    clean = str(value or "").strip()
    if not clean:
        return None
    try:
        return date.fromisoformat(clean[:10])
    except ValueError:
        return None


def _format_short_date(value: date) -> str:
    return f"{value.strftime('%b')} {value.day}"


def _tv_status_color_token(status: str) -> str:
    normalized = _normalize_status(status)
    if normalized in DONE_STATUSES:
        return "done"
    if normalized in {"moving", "on_track", "in_progress", "in progress"}:
        return "ok"
    if normalized in {"at_risk", "overdue"}:
        return "warn"
    if normalized in {"blocked"}:
        return "bad"
    return "accent"


def build_tv_heading(okrs: list[dict]) -> str:
    first_kr = next(
        (str(kr.get("title") or "").strip() for okr in okrs for kr in (okr.get("key_results") or []) if str(kr.get("title") or "").strip()),
        "",
    )
    clean = first_kr or (str(okrs[0].get("title") or "").strip() if okrs else "")
    if not clean:
        return "EXECUTION"
    ship_match = re.match(r"^ship\s+(.+)$", clean, re.IGNORECASE)
    if ship_match:
        target = ship_match.group(1).strip()
        if re.match(r"^the\b", target, re.IGNORECASE):
            return f"SHIPPING {target.upper()}"
        return f"SHIPPING THE {target.upper()}"
    return clean.upper()


def _tv_contribution_countability(
    contribution: dict,
    source_chips: list[dict],
    *,
    countable_source_types: set[str] | None = None,
    countable_source_refs: set[str] | None = None,
    allow_fixture_source_refs: bool = False,
) -> dict:
    reasons: list[dict[str, str]] = []
    countable_source_chips = _countable_source_chips_for_tv(
        source_chips,
        countable_source_types=countable_source_types,
        countable_source_refs=countable_source_refs,
        allow_fixture_source_refs=allow_fixture_source_refs,
    )
    source_types = sorted({
        _chip_source_type(chip)
        for chip in countable_source_chips
        if _chip_source_type(chip)
    })
    if contribution.get("visibility") != "company":
        reasons.append({
            "code": "private_visibility",
            "label": "Private contribution, manager-only for now.",
        })
    if not countable_source_chips:
        if any(_chip_source_type(chip) in TV_VISIBLE_CONTRIBUTION_SOURCE_TYPES and chip.get("is_fixture") is True for chip in source_chips):
            reasons.append({
                "code": "fixture_source",
                "label": "Only fixture evidence is attached. TV needs real source evidence.",
            })
        elif source_chips:
            reasons.append({
                "code": "source_not_tv_ready",
                "label": "Attached sources do not count for TV contribution. Use Linear, GitHub PR metadata, or HubSpot.",
            })
        else:
            reasons.append({
                "code": "missing_countable_source",
                "label": "No countable source evidence yet. TV contribution needs Linear, GitHub PR metadata, or HubSpot.",
            })
    if not _contribution_review_ready_for_tv(contribution):
        reasons.append({
            "code": "review_not_ready",
            "label": "Needs accepted review status or verified source-mapping evidence before TV counts it.",
        })
    countable = not reasons
    return {
        "countable": countable,
        "status": "counts_on_tv" if countable else "not_counted_on_tv",
        "reasons": reasons,
        "countable_source_count": len(countable_source_chips),
        "countable_source_types": source_types,
        "weight": _tv_contribution_weight(contribution, countable_source_chips) if countable else 0.0,
        "calculation_method": (
            "TV counts company-visible contribution rows backed by Linear, GitHub PR metadata, or HubSpot "
            "after manager acceptance or verified source mapping."
        ),
    }


def _tv_contribution_missing_evidence_count(contribution: dict, source_chips: list[dict]) -> int:
    gaps = set()
    if not source_chips:
        gaps.add("missing_source_evidence")
    confidence = _number(contribution.get("confidence"))
    if confidence is None or confidence < 0.75:
        gaps.add("low_confidence")
    if str(contribution.get("caveat") or "").strip():
        gaps.add("caveat")
    for flag in _jsonb(contribution.get("manager_review_flags"), []) or []:
        if flag:
            gaps.add(str(flag))
    return len(gaps)


def _tv_contribution_weight(contribution: dict, source_chips: list[dict]) -> float:
    source_types = {
        str(chip.get("source_type") or "").strip().lower()
        for chip in source_chips
        if str(chip.get("source_type") or "").strip().lower() in TV_VISIBLE_CONTRIBUTION_SOURCE_TYPES
    }
    if not source_types:
        return 0.0
    base = max(TV_VISIBLE_CONTRIBUTION_SOURCE_WEIGHTS.get(source_type, 0.0) for source_type in source_types)
    if "github" in source_types and _github_contribution_is_merged(contribution):
        base = max(base, 1.0)
    if "linear" in source_types and _linear_contribution_is_completed(contribution):
        base = max(base, 1.0)
    return round(base, 2)


def _github_contribution_is_merged(contribution: dict) -> bool:
    metrics = _jsonb(contribution.get("execution_metrics"), {}) or {}
    if isinstance(metrics, dict):
        for key in ("merged", "merged_prs", "prs_merged"):
            value = metrics.get(key)
            if value is True:
                return True
            if (_number(value) or 0) > 0:
                return True
    return str(contribution.get("contribution_type") or "").strip().lower() in {"implemented", "merged_pr", "shipped"}


def _linear_contribution_is_completed(contribution: dict) -> bool:
    metrics = _jsonb(contribution.get("execution_metrics"), {}) or {}
    if not isinstance(metrics, dict):
        return False
    for key in ("completed", "done", "tasks_completed", "issues_completed", "completed_items"):
        value = metrics.get(key)
        if value is True:
            return True
        if (_number(value) or 0) > 0:
            return True
    return str(contribution.get("contribution_type") or "").strip().lower() in {"completed", "implemented", "shipped"}


def _contribution_review_ready_for_tv(contribution: dict) -> bool:
    status = str(contribution.get("review_status") or "").strip().lower()
    if status in TV_ACCEPTED_CONTRIBUTION_REVIEW_STATUSES:
        return True
    return False


def _tv_contribution_source_types(countable_source_types: set[str] | None) -> set[str]:
    if countable_source_types is None:
        return set(TV_VISIBLE_CONTRIBUTION_SOURCE_TYPES)
    return set(countable_source_types) & TV_VISIBLE_CONTRIBUTION_SOURCE_TYPES


def _countable_source_chips_for_tv(
    source_chips: list[dict],
    *,
    countable_source_types: set[str] | None = None,
    countable_source_refs: set[str] | None = None,
    allow_fixture_source_refs: bool = False,
) -> list[dict]:
    return _countable_source_chips(
        source_chips,
        countable_source_types=_tv_contribution_source_types(countable_source_types),
        countable_source_refs=countable_source_refs,
        allow_fixture_source_refs=allow_fixture_source_refs,
    )


def _tv_source_coverage(
    okrs: list[dict],
    *,
    countable_source_types: set[str] | None = None,
    countable_source_refs: set[str] | None = None,
    allow_fixture_source_refs: bool = False,
) -> dict:
    source_types = set()
    missing = set()
    for okr in okrs:
        snapshot = okr.get("snapshot") or {}
        missing.update(snapshot.get("missing_evidence") or [])
        source_types.update(_chip_source_types(
            okr.get("source_chips") or [],
            countable_source_types=countable_source_types,
            countable_source_refs=countable_source_refs,
            allow_fixture_source_refs=allow_fixture_source_refs,
        ))
        source_types.update(_chip_source_types(
            snapshot.get("source_chips") or [],
            countable_source_types=countable_source_types,
            countable_source_refs=countable_source_refs,
            allow_fixture_source_refs=allow_fixture_source_refs,
        ))
        for kr in okr.get("key_results") or []:
            kr_snapshot = kr.get("snapshot") or {}
            planning = kr.get("planning") or {}
            missing.update(kr_snapshot.get("missing_evidence") or [])
            missing.update(planning.get("missing_inputs") or [])
            source_types.update(_source_types_for_kr(
                kr,
                countable_source_types=countable_source_types,
                countable_source_refs=countable_source_refs,
                allow_fixture_source_refs=allow_fixture_source_refs,
            ))
    return {
        "connected_source_types": sorted(item for item in source_types if item),
        "connected_count": len([item for item in source_types if item]),
        "missing_evidence_count": len(missing),
        "approval_policy": {
            "mode": "audit_only_until_source_verified",
            "tv_rule": (
                "Manager review events are audit records. Public TV contribution counts only company-visible "
                "contribution rows backed by allowed source evidence and an accepted or verified review status."
            ),
        },
        "confidence_explanation": _tv_source_coverage_explanation(
            connected_count=len([item for item in source_types if item]),
            missing_evidence_count=len(missing),
            allow_fixture_source_refs=allow_fixture_source_refs,
        ),
    }


def _tv_confidence_explanation(
    *,
    confidence: float | None,
    source_count: int,
    missing_evidence_count: int,
    unit: str,
    source_label: str = "source type",
) -> str:
    if confidence is None:
        return f"{unit} confidence is unknown because Alloy has no scored source-backed snapshot yet."
    label = "high" if confidence >= 0.75 else "medium" if confidence >= 0.5 else "low"
    if missing_evidence_count and source_count:
        return (
            f"{unit} confidence is {label} because {source_count} {source_label}"
            f"{'' if source_count == 1 else 's'} counted, with {missing_evidence_count} evidence gap"
            f"{'' if missing_evidence_count == 1 else 's'} still open."
        )
    if missing_evidence_count:
        return (
            f"{unit} confidence is {label} because {missing_evidence_count} evidence gap"
            f"{'' if missing_evidence_count == 1 else 's'} still need source proof."
        )
    if source_count:
        return (
            f"{unit} confidence is {label} because {source_count} {source_label}"
            f"{'' if source_count == 1 else 's'} currently support the score."
        )
    return f"{unit} confidence is {label} because the score is still mostly plan-based."


def _tv_source_coverage_explanation(
    *,
    connected_count: int,
    missing_evidence_count: int,
    allow_fixture_source_refs: bool = False,
) -> str:
    source_label = "allowed demo/test source type" if allow_fixture_source_refs else "allowed non-fixture source type"
    if connected_count and missing_evidence_count:
        return (
            f"TV coverage counts {connected_count} {source_label}"
            f"{'' if connected_count == 1 else 's'} and still has {missing_evidence_count} evidence gap"
            f"{'' if missing_evidence_count == 1 else 's'}."
        )
    if connected_count:
        return (
            f"TV coverage counts {connected_count} {source_label}"
            f"{'' if connected_count == 1 else 's'}."
        )
    source_kind = "demo/test source" if allow_fixture_source_refs else "non-fixture source"
    return f"TV coverage has no allowed {source_kind} evidence yet, so scores should stay conservative."


def _chip_source_types(
    chips: list[dict],
    *,
    countable_source_types: set[str] | None = None,
    countable_source_refs: set[str] | None = None,
    allow_fixture_source_refs: bool = False,
) -> set[str]:
    return {
        source_type
        for source_type in (
            _chip_source_type(chip)
            for chip in chips
            if chip and _chip_is_countable(
                chip,
                countable_source_types,
                countable_source_refs,
                allow_fixture_source_refs=allow_fixture_source_refs,
            )
        )
        if source_type and (countable_source_types is None or source_type in countable_source_types)
    }


def _countable_source_chips(
    chips: list[dict],
    *,
    countable_source_types: set[str] | None = None,
    countable_source_refs: set[str] | None = None,
    allow_fixture_source_refs: bool = False,
) -> list[dict]:
    if countable_source_types is None and countable_source_refs is None:
        return list(chips)
    return [
        chip for chip in chips
        if _chip_is_countable(
            chip,
            countable_source_types,
            countable_source_refs,
            allow_fixture_source_refs=allow_fixture_source_refs,
        )
    ]


def _chip_source_type(chip: dict) -> str:
    return str(chip.get("source_type") or chip.get("label") or "").lower()


def _chip_is_countable(
    chip: dict,
    countable_source_types: set[str] | None,
    countable_source_refs: set[str] | None,
    *,
    allow_fixture_source_refs: bool = False,
) -> bool:
    if (
        not allow_fixture_source_refs
        and (countable_source_types is not None or countable_source_refs is not None)
        and chip.get("is_fixture") is True
    ):
        return False
    source_type = _chip_source_type(chip)
    if countable_source_types is not None and source_type not in countable_source_types:
        return False
    if countable_source_refs is not None:
        ref = str(chip.get("ref") or chip.get("source_ref") or "").strip()
        return bool(ref and ref in countable_source_refs)
    return True


def _average(values: list[Any]) -> float | None:
    nums = [_number(value) for value in values]
    nums = [value for value in nums if value is not None]
    if not nums:
        return None
    return round(sum(nums) / len(nums), 2)


def _first_non_empty(values: list[Any]) -> str | None:
    for value in values:
        if value:
            return str(value)
    return None


def _allowlist(item: dict, allowed: set[str]) -> dict:
    return {key: value for key, value in item.items() if key in allowed}


def _assert_tv_payload_safe(payload: Any) -> None:
    if isinstance(payload, dict):
        forbidden = TV_FORBIDDEN_KEYS.intersection(payload.keys())
        if forbidden:
            raise ValueError(f"TV payload contains private fields: {sorted(forbidden)}")
        for value in payload.values():
            _assert_tv_payload_safe(value)
    elif isinstance(payload, list):
        for item in payload:
            _assert_tv_payload_safe(item)
    elif isinstance(payload, str):
        normalized = payload.lower()
        for marker in TV_FORBIDDEN_STRING_MARKERS:
            if marker in normalized:
                raise ValueError(f"TV payload contains private string marker: {marker}")
        for pattern in TV_FORBIDDEN_STRING_PATTERNS:
            if pattern.search(payload):
                raise ValueError("TV payload contains private string pattern")


def project_team_profiles(okrs: list[dict]) -> list[dict]:
    by_person: dict[str, dict] = {}
    mapped_subjects = set()
    for okr in okrs:
        for mapping in okr.get("mappings") or []:
            mapped_subjects.add((mapping.get("subject_type"), mapping.get("subject_id")))
        for kr in okr.get("key_results") or []:
            for mapping in kr.get("mappings") or []:
                mapped_subjects.add((mapping.get("subject_type"), mapping.get("subject_id")))
    for okr in okrs:
        contributions = list(okr.get("contributions") or [])
        for kr in okr.get("key_results") or []:
            contributions.extend(kr.get("contributions") or [])
        for contribution in contributions:
            person = contribution.get("person_name") or "Unknown"
            profile = by_person.setdefault(person, {
                "person_name": person,
                "mapped_okrs": [],
                "visible_work": [],
                "source_chips": [],
                "metrics": {},
                "review_flags": [],
                "right_work_status": "missing_evidence",
                "confidence_gaps": [],
            })
            target = {
                "okr_id": contribution.get("okr_id"),
                "key_result_id": contribution.get("key_result_id"),
                "okr_title": okr.get("title"),
                "related_subject_type": contribution.get("related_subject_type"),
                "related_subject_id": contribution.get("related_subject_id"),
                "summary": contribution.get("contribution_summary"),
                "contribution_type": contribution.get("contribution_type"),
            }
            profile["visible_work"].append(target)
            profile["mapped_okrs"].append({"id": okr.get("id"), "title": okr.get("title")})
            profile["source_chips"].extend(contribution.get("source_chips") or [])
            profile["review_flags"].extend(contribution.get("manager_review_flags") or [])
            if contribution.get("caveat"):
                profile["confidence_gaps"].append(contribution["caveat"])
            for key, value in (contribution.get("execution_metrics") or {}).items():
                if isinstance(value, (int, float)):
                    profile["metrics"][key] = profile["metrics"].get(key, 0) + value
                else:
                    profile["metrics"][key] = value
            related_key = (contribution.get("related_subject_type"), contribution.get("related_subject_id"))
            explicit_status = contribution.get("right_work_status")
            if explicit_status:
                profile["right_work_status"] = _stronger_right_work(profile["right_work_status"], explicit_status)
            elif related_key in mapped_subjects:
                profile["right_work_status"] = _stronger_right_work(profile["right_work_status"], "aligned")

    profiles = []
    for profile in by_person.values():
        profile["mapped_okrs"] = _dedupe_by_id(profile["mapped_okrs"])
        profile["source_chips"] = _dedupe_chips(profile["source_chips"])
        profile["review_flags"] = list(dict.fromkeys(profile["review_flags"]))
        profile["confidence_gaps"] = list(dict.fromkeys(profile["confidence_gaps"]))
        profiles.append(profile)
    return sorted(profiles, key=lambda item: (item["right_work_status"] != "needs_review", item["person_name"]))


def collect_dri_questions(okrs: list[dict]) -> list[dict]:
    questions = []
    for okr in okrs:
        questions.extend(okr.get("dri_questions") or [])
        for kr in okr.get("key_results") or []:
            questions.extend(kr.get("dri_questions") or [])
    return questions


def _stronger_right_work(current: str, incoming: str) -> str:
    priority = {
        "needs_review": 4,
        "missing_evidence": 3,
        "unclear": 2,
        "aligned": 1,
    }
    return incoming if priority.get(incoming, 0) > priority.get(current, 0) else current


def _dedupe_by_id(items: list[dict]) -> list[dict]:
    seen: set[str] = set()
    output = []
    for item in items:
        key = str(item.get("id"))
        if key in seen:
            continue
        seen.add(key)
        output.append(item)
    return output


def _dedupe_chips(chips: list[dict]) -> list[dict]:
    seen: set[tuple[str, str | None, str | None, str | None]] = set()
    output = []
    for chip in chips:
        if not isinstance(chip, dict):
            continue
        key = (
            str(chip.get("label") or ""),
            str(chip.get("url")) if chip.get("url") else None,
            str(chip.get("ref")) if chip.get("ref") else None,
            str(chip.get("source_type")) if chip.get("source_type") else None,
        )
        if key in seen:
            continue
        seen.add(key)
        output.append(chip)
    return output
