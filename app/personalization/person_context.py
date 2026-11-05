"""Owner-private person-scoped relationship context."""
from __future__ import annotations

from collections import defaultdict
import hashlib
import json
import uuid
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import structlog

from app.brain.entities import resolve_or_create
from app.core.db import acquire
from app.core.principals import effective_principals_for_user
from app.core.tenant_settings import get_tenant_settings
from app.llm.client import call_llm
from app.llm.validators import validate_person_context_seed, validate_person_context_update

logger = structlog.get_logger()

ACTIVE_STATUSES = {"active", "stale_flagged"}
STALE_AFTER_DAYS = 14
PACK_TOKEN_BUDGET = 30_000
SEED_PROMPT_VERSION = "v1.0"
UPDATE_PROMPT_VERSION = "v1.0"


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


def _text_from_value(value) -> str | None:
    if isinstance(value, dict):
        return _clean_text(value.get("text") or value.get("stance") or value.get("topic"))
    return _clean_text(value) or None


def _clean_refs(refs) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for ref in refs or []:
        cleaned = _clean_text(ref)
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            out.append(cleaned)
    return out


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _isoformat_value(value):
    if not value:
        return None
    if isinstance(value, str):
        return value
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _claim_entry(raw, *, default_label: str) -> dict | None:
    if isinstance(raw, str):
        cleaned = _clean_text(raw)
        if not cleaned:
            return None
        return {
            "id": str(uuid.uuid4()),
            "text": cleaned,
            "label": default_label,
            "refs": [],
            "status": "active",
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
        }
    if not isinstance(raw, dict):
        return None
    text = _clean_text(raw.get("text") or raw.get(default_label) or raw.get("stance") or raw.get("topic"))
    if not text:
        return None
    label = _clean_text(raw.get("label") or default_label) or default_label
    entry = {
        "id": str(raw.get("id") or uuid.uuid4()),
        "text": text,
        "label": label,
        "refs": _clean_refs(raw.get("refs") or raw.get("source_refs")),
        "status": str(raw.get("status") or "active"),
        "created_at": str(raw.get("created_at") or _now_iso()),
        "updated_at": str(raw.get("updated_at") or _now_iso()),
    }
    topic = _clean_text(raw.get("topic"))
    stance = _clean_text(raw.get("stance"))
    if topic:
        entry["topic"] = topic
    if stance:
        entry["stance"] = stance
    return entry


def _claim_list(value, *, default_label: str) -> list[dict]:
    raw = _jsonb(value, [])
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for item in raw:
        entry = _claim_entry(item, default_label=default_label)
        if not entry:
            continue
        if entry["id"] in seen:
            continue
        seen.add(entry["id"])
        out.append(entry)
    return out


def _stale_status(last_confirmed_at: str | None, *, status: str) -> str:
    if status == "retired":
        return status
    if not last_confirmed_at:
        return "stale_flagged"
    try:
        confirmed_at = datetime.fromisoformat(last_confirmed_at.replace("Z", "+00:00"))
    except ValueError:
        return "stale_flagged"
    if datetime.now(timezone.utc) - confirmed_at >= timedelta(days=STALE_AFTER_DAYS):
        return "stale_flagged"
    return "active"


def normalize_person_context(row: dict | None, *, tenant_id: str, owner_user_id: str, subject_entity_id: str) -> dict:
    row = row or {}
    relationship_kind = _clean_text(row.get("relationship_kind")) or None
    sentiment_trend = _clean_text(row.get("sentiment_trend")) or None
    last_confirmed_at = _isoformat_value(row.get("last_confirmed_at"))
    status = _stale_status(last_confirmed_at, status=str(row.get("status") or "active"))
    topic_stances = _claim_list(row.get("topic_stances"), default_label="topic_stance")
    communication_preferences = _claim_list(row.get("communication_preferences"), default_label="communication_preference")
    sentiment_history = _claim_list(row.get("sentiment_history"), default_label="sentiment_history")
    evidence_refs = _clean_refs(_jsonb(row.get("evidence_refs"), []))
    return {
        "id": str(row["id"]) if row.get("id") else None,
        "tenant_id": str(row.get("tenant_id") or tenant_id),
        "owner_user_id": str(row.get("owner_user_id") or owner_user_id),
        "subject_entity_id": str(row.get("subject_entity_id") or subject_entity_id),
        "subject_entity_name": row.get("subject_entity_name"),
        "relationship_kind": relationship_kind,
        "topic_stances": [{**entry, "status": status if entry.get("status") != "retired" else "retired"} for entry in topic_stances],
        "communication_preferences": [{**entry, "status": status if entry.get("status") != "retired" else "retired"} for entry in communication_preferences],
        "sentiment_trend": sentiment_trend,
        "sentiment_history": [{**entry, "status": status if entry.get("status") != "retired" else "retired"} for entry in sentiment_history],
        "evidence_refs": evidence_refs,
        "last_confirmed_at": last_confirmed_at,
        "status": status,
        "updated_at": _isoformat_value(row.get("updated_at")),
        "updated_by": str(row.get("updated_by")) if row.get("updated_by") else None,
    }


def _context_active(context: dict) -> bool:
    return (
        bool(context.get("relationship_kind"))
        or bool(context.get("topic_stances"))
        or bool(context.get("communication_preferences"))
        or bool(context.get("sentiment_trend"))
        or bool(context.get("sentiment_history"))
    ) and context.get("status") in ACTIVE_STATUSES


def person_context_bundle(context: dict) -> dict:
    return {
        "relationship_kind": context.get("relationship_kind"),
        "topic_stances": [entry.get("text") for entry in context.get("topic_stances") or [] if entry.get("status") in ACTIVE_STATUSES],
        "communication_preferences": [entry.get("text") for entry in context.get("communication_preferences") or [] if entry.get("status") in ACTIVE_STATUSES],
        "sentiment_trend": context.get("sentiment_trend") if context.get("status") in ACTIVE_STATUSES else None,
        "sentiment_history": [entry.get("text") for entry in context.get("sentiment_history") or [] if entry.get("status") in ACTIVE_STATUSES],
        "subject_entity_name": context.get("subject_entity_name"),
    }


def person_context_pack_item(context: dict) -> dict | None:
    bundle = person_context_bundle(context)
    lines: list[str] = []
    subject_name = bundle["subject_entity_name"] or f"entity {context.get('subject_entity_id')}"
    if bundle["relationship_kind"]:
        lines.append(f"Relationship kind: {bundle['relationship_kind']}.")
    if bundle["topic_stances"]:
        lines.append(f"Topic stances: {', '.join(bundle['topic_stances'])}.")
    if bundle["communication_preferences"]:
        lines.append(f"Communication preferences: {', '.join(bundle['communication_preferences'])}.")
    if bundle["sentiment_trend"]:
        lines.append(f"Sentiment trend: {bundle['sentiment_trend']}.")
    if bundle["sentiment_history"]:
        lines.append(f"Sentiment history: {', '.join(bundle['sentiment_history'])}.")
    if not lines:
        return None
    owner_user_id = str(context["owner_user_id"])
    subject_entity_id = str(context["subject_entity_id"])
    return {
        "kind": "person_context",
        "id": str(context.get("id") or f"{owner_user_id}:{subject_entity_id}"),
        "text": f"Private relationship model for {subject_name}. " + " ".join(lines),
        "source_url": None,
        "priority": 5,
        "score": 1.0,
        "visibility": "private",
        "allowed_principals": [f"user:{owner_user_id}"],
        "acl_state": "stamped",
        "acl_freshness_state": "fresh",
        "acl_basis": "owner_private_person_context",
        "acl_confidence": "high",
    }


def _proposal_fingerprint(proposal: dict, source_refs: list[str]) -> str:
    payload = json.dumps({"proposal": proposal, "source_refs": sorted(_clean_refs(source_refs))}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _evidence_ref_hash(source_refs: list[str]) -> str:
    payload = json.dumps(sorted(_clean_refs(source_refs)), separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _proposal_source_refs(proposal: dict) -> list[str]:
    refs: list[str] = []
    for field in ("relationship_kind", "topic_stances", "communication_preferences", "sentiment_trend", "sentiment_history"):
        value = proposal.get(field)
        if isinstance(value, dict):
            refs.extend(value.get("refs") or value.get("source_refs") or [])
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    refs.extend(item.get("refs") or item.get("source_refs") or [])
    return _clean_refs(refs)


def _participant_candidate_keys(candidate: dict) -> set[str]:
    keys: set[str] = set()
    handle_type = _clean_text(candidate.get("handle_type")).lower()
    handle = _clean_text(candidate.get("handle")).lower()
    name = _clean_text(candidate.get("name")).lower()
    actor_label = _clean_text(candidate.get("actor_label")).lower()
    if handle_type == "email" and handle:
        keys.add(handle)
    if handle_type == "slack" and handle:
        keys.add(handle)
    if actor_label:
        for prefix in ("email:", "slack:"):
            if actor_label.startswith(prefix):
                keys.add(actor_label.removeprefix(prefix))
    if name:
        keys.update(part for part in name.split() if len(part) >= 2)
    return {key for key in keys if key}


async def _recent_person_context_event_rows(
    *,
    tenant_id: str,
    source_id: str | None,
    recent_hours: int,
) -> list[dict]:
    async with acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT ne.id, ne.raw_event_id, ne.source_type, ne.actor_label, ne.occurred_at, ne.text,
                   ne.entities, ne.metadata, ne.allowed_principals
            FROM normalized_events ne
            JOIN raw_events re ON re.id = ne.raw_event_id
            WHERE ne.tenant_id = $1
              AND ($2::uuid IS NULL OR re.source_id = $2::uuid)
              AND ne.acl_state = 'stamped'
              AND ne.acl_freshness_state = 'fresh'
              AND re.acl_state = 'stamped'
              AND re.acl_freshness_state = 'fresh'
              AND ne.allowed_principals ?| $3::text[]
              AND re.allowed_principals ?| $3::text[]
              AND ne.occurred_at >= now() - ($4 || ' hours')::interval
            ORDER BY ne.occurred_at DESC
            """,
            tenant_id,
            source_id,
            [f"company:{tenant_id}"],
            recent_hours,
        )
    return [dict(row) for row in rows]


async def _participant_user_lookup(*, tenant_id: str) -> dict[str, str]:
    async with acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, email, slack_user_id, name
            FROM users
            WHERE tenant_id = $1
            """,
            tenant_id,
        )
    lookup: dict[str, str] = {}
    for row in rows:
        user_id = str(row["id"])
        if row.get("email"):
            lookup.setdefault(_clean_text(row["email"]).lower(), user_id)
        if row.get("slack_user_id"):
            lookup.setdefault(_clean_text(row["slack_user_id"]).lower(), user_id)
        if row.get("name"):
            for key in _participant_candidate_keys({"handle_type": "slack", "handle": row["name"], "name": row["name"]}):
                lookup.setdefault(key, user_id)
        lookup.setdefault(f"user:{user_id}", user_id)
    return lookup


def _row_participant_candidates(row: dict) -> list[dict]:
    candidates: list[dict] = []
    actor_label = _clean_text(row.get("actor_label"))
    if actor_label:
        candidates.append({"actor_label": actor_label})
    for entry in _jsonb(row.get("entities"), []) or []:
        if not isinstance(entry, dict):
            continue
        handle = _clean_text(entry.get("handle"))
        handle_type = _clean_text(entry.get("handle_type"))
        name = _clean_text(entry.get("name"))
        if handle or name:
            candidates.append({
                "handle_type": handle_type,
                "handle": handle,
                "name": name,
                "actor_label": actor_label if entry.get("is_actor") else "",
            })
    return candidates


async def maybe_seed_person_contexts_for_source(
    *,
    tenant_id: str,
    source_id: str | None = None,
    pipeline_run_id: str | None = None,
    recent_hours: int = 48,
    per_owner_cap: int = 5,
) -> list[dict]:
    rows = await _recent_person_context_event_rows(tenant_id=tenant_id, source_id=source_id, recent_hours=recent_hours)
    if not rows:
        return []
    user_lookup = await _participant_user_lookup(tenant_id=tenant_id)
    proposals: list[dict] = []
    seen_pairs: set[tuple[str, str]] = set()
    per_owner_counts: dict[str, int] = defaultdict(int)

    for row in rows:
        row_candidates = _row_participant_candidates(row)
        matched_users: list[tuple[str, dict]] = []
        subject_candidates: list[dict] = []
        for candidate in row_candidates:
            participant_keys = _participant_candidate_keys(candidate)
            user_id = next((user_lookup.get(key) for key in participant_keys if user_lookup.get(key)), None)
            if user_id:
                matched_users.append((user_id, candidate))
            subject_candidates.append(candidate)
        if len(matched_users) < 1 or len(subject_candidates) < 2:
            continue

        fresh_text = str(row.get("text") or "").strip()
        for owner_user_id, owner_candidate in matched_users:
            if per_owner_counts[owner_user_id] >= per_owner_cap:
                continue
            owner_keys = _participant_candidate_keys(owner_candidate)
            for subject_candidate in subject_candidates:
                if subject_candidate is owner_candidate:
                    continue
                subject_keys = _participant_candidate_keys(subject_candidate)
                if owner_keys & subject_keys:
                    continue
                subject_entity_id = await resolve_or_create(
                    tenant_id,
                    {
                        "type": "person",
                        "name": subject_candidate.get("name") or None,
                        "handle_type": subject_candidate.get("handle_type") or None,
                        "handle": subject_candidate.get("handle") or None,
                    },
                )
                if not subject_entity_id:
                    continue
                pair = (owner_user_id, subject_entity_id)
                if pair in seen_pairs or owner_user_id == subject_entity_id:
                    continue
                seen_pairs.add(pair)
                current = await get_person_context(
                    tenant_id=tenant_id,
                    owner_user_id=owner_user_id,
                    subject_entity_id=subject_entity_id,
                )
                if _context_active(current):
                    proposal = await maybe_propose_person_context_update(
                        tenant_id=tenant_id,
                        owner_user_id=owner_user_id,
                        subject_entity_id=subject_entity_id,
                        context_pack={"text": fresh_text},
                        pipeline_run_id=pipeline_run_id,
                    )
                else:
                    proposal = await maybe_seed_person_context(
                        tenant_id=tenant_id,
                        owner_user_id=owner_user_id,
                        subject_entity_id=subject_entity_id,
                        pipeline_run_id=pipeline_run_id,
                    )
                if proposal:
                    per_owner_counts[owner_user_id] += 1
                    proposals.append(proposal)
                    if per_owner_counts[owner_user_id] >= per_owner_cap:
                        break
    return proposals


async def _fetch_person_context_row(conn, *, tenant_id: str, owner_user_id: str, subject_entity_id: str, for_update: bool = False):
    query = """
        SELECT pc.id, pc.tenant_id, pc.owner_user_id, pc.subject_entity_id, er.canonical_name AS subject_entity_name,
               pc.relationship_kind, pc.topic_stances, pc.communication_preferences, pc.sentiment_trend,
               pc.sentiment_history, pc.evidence_refs, pc.last_confirmed_at, pc.status, pc.updated_by, pc.updated_at
        FROM person_contexts pc
        JOIN entity_registry er ON er.id = pc.subject_entity_id
        WHERE pc.tenant_id = $1 AND pc.owner_user_id = $2 AND pc.subject_entity_id = $3
    """
    if for_update:
        query += " FOR UPDATE"
    return await conn.fetchrow(query, tenant_id, owner_user_id, subject_entity_id)


async def get_person_context(*, tenant_id: str, owner_user_id: str, subject_entity_id: str) -> dict:
    async with acquire() as conn:
        row = await _fetch_person_context_row(
            conn,
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            subject_entity_id=subject_entity_id,
        )
    return normalize_person_context(dict(row) if row else None, tenant_id=tenant_id, owner_user_id=owner_user_id, subject_entity_id=subject_entity_id)


async def list_person_contexts(*, tenant_id: str, owner_user_id: str, status: str | None = None) -> list[dict]:
    query = """
        SELECT pc.id, pc.tenant_id, pc.owner_user_id, pc.subject_entity_id, er.canonical_name AS subject_entity_name,
               pc.relationship_kind, pc.topic_stances, pc.communication_preferences, pc.sentiment_trend,
               pc.sentiment_history, pc.evidence_refs, pc.last_confirmed_at, pc.status, pc.updated_by, pc.updated_at
        FROM person_contexts pc
        JOIN entity_registry er ON er.id = pc.subject_entity_id
        WHERE pc.tenant_id = $1 AND pc.owner_user_id = $2
    """
    args: list = [tenant_id, owner_user_id]
    if status:
        query += " AND pc.status = $3"
        args.append(status)
    query += " ORDER BY pc.updated_at DESC, pc.created_at DESC"
    async with acquire() as conn:
        rows = await conn.fetch(query, *args)
    return [
        normalize_person_context(dict(row), tenant_id=tenant_id, owner_user_id=owner_user_id, subject_entity_id=str(row["subject_entity_id"]))
        for row in rows
    ]


def _merge_context_patch(current: dict, patch: dict) -> dict:
    next_context = deepcopy(current)
    if "relationship_kind" in patch:
        next_context["relationship_kind"] = _text_from_value(patch.get("relationship_kind"))
    if "topic_stances" in patch:
        next_context["topic_stances"] = []
        for item in patch.get("topic_stances") or []:
            entry = _claim_entry(item, default_label="topic_stance")
            if entry:
                next_context["topic_stances"].append(entry)
    if "communication_preferences" in patch:
        next_context["communication_preferences"] = []
        for item in patch.get("communication_preferences") or []:
            entry = _claim_entry(item, default_label="communication_preference")
            if entry:
                next_context["communication_preferences"].append(entry)
    if "sentiment_trend" in patch:
        next_context["sentiment_trend"] = _text_from_value(patch.get("sentiment_trend"))
    if "sentiment_history" in patch:
        next_context["sentiment_history"] = []
        for item in patch.get("sentiment_history") or []:
            entry = _claim_entry(item, default_label="sentiment_history")
            if entry:
                next_context["sentiment_history"].append(entry)
    if "evidence_refs" in patch:
        next_context["evidence_refs"] = _clean_refs(patch.get("evidence_refs"))
    return next_context


async def record_person_context_event(
    *,
    tenant_id: str,
    owner_user_id: str,
    subject_entity_id: str,
    actor_user_id: str,
    event_type: str,
    previous_state: dict,
    new_state: dict,
    source_refs: list[str] | None = None,
    proposal_id: str | None = None,
    note: str | None = None,
) -> None:
    async with acquire() as conn:
        await conn.execute(
            """
            INSERT INTO person_context_events (
                tenant_id, owner_user_id, subject_entity_id, proposal_id, event_type,
                actor_user_id, note, previous_state, new_state, source_refs
            )
            VALUES ($1, $2, $3, $4, $5, $6, NULLIF($7, ''), $8::jsonb, $9::jsonb, $10::jsonb)
            """,
            tenant_id,
            owner_user_id,
            subject_entity_id,
            proposal_id,
            event_type,
            actor_user_id,
            note or "",
            json.dumps(previous_state),
            json.dumps(new_state),
            json.dumps(_clean_refs(source_refs)),
        )


async def _persist_person_context_conn(conn, *, tenant_id: str, owner_user_id: str, subject_entity_id: str, updated_by: str | None, context: dict):
    return await conn.fetchrow(
        """
        INSERT INTO person_contexts (
            tenant_id, owner_user_id, subject_entity_id, relationship_kind, topic_stances,
            communication_preferences, sentiment_trend, sentiment_history, evidence_refs,
            last_confirmed_at, status, updated_by
        )
        VALUES (
            $1, $2, $3, $4, $5::jsonb, $6::jsonb, $7, $8::jsonb, $9::jsonb,
            $10, $11, $12
        )
        ON CONFLICT (tenant_id, owner_user_id, subject_entity_id) DO UPDATE
        SET relationship_kind = EXCLUDED.relationship_kind,
            topic_stances = EXCLUDED.topic_stances,
            communication_preferences = EXCLUDED.communication_preferences,
            sentiment_trend = EXCLUDED.sentiment_trend,
            sentiment_history = EXCLUDED.sentiment_history,
            evidence_refs = EXCLUDED.evidence_refs,
            last_confirmed_at = EXCLUDED.last_confirmed_at,
            status = EXCLUDED.status,
            updated_by = EXCLUDED.updated_by,
            updated_at = now()
        RETURNING id, tenant_id, owner_user_id, subject_entity_id, relationship_kind, topic_stances,
                  communication_preferences, sentiment_trend, sentiment_history, evidence_refs,
                  last_confirmed_at, status, updated_by, updated_at
        """,
        tenant_id,
        owner_user_id,
        subject_entity_id,
        context.get("relationship_kind"),
        json.dumps(context.get("topic_stances") or []),
        json.dumps(context.get("communication_preferences") or []),
        context.get("sentiment_trend"),
        json.dumps(context.get("sentiment_history") or []),
        json.dumps(context.get("evidence_refs") or []),
        context.get("last_confirmed_at"),
        context.get("status") or "active",
        updated_by,
    )


async def upsert_person_context(*, tenant_id: str, owner_user_id: str, subject_entity_id: str, updated_by: str, patch: dict) -> dict:
    async with acquire() as conn:
        current_row = await _fetch_person_context_row(
            conn,
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            subject_entity_id=subject_entity_id,
            for_update=True,
        )
        current = normalize_person_context(
            dict(current_row) if current_row else None,
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            subject_entity_id=subject_entity_id,
        )
        next_context = _merge_context_patch(current, patch)
        next_context["status"] = "active"
        next_context["last_confirmed_at"] = _now_iso()
        row = await _persist_person_context_conn(
            conn,
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            subject_entity_id=subject_entity_id,
            updated_by=updated_by,
            context=next_context,
        )
        normalized = normalize_person_context(
            dict(row),
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            subject_entity_id=subject_entity_id,
        )
        await record_person_context_event(
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            subject_entity_id=subject_entity_id,
            actor_user_id=updated_by,
            event_type="settings_edit",
            previous_state=current,
            new_state=normalized,
            source_refs=[],
        )
    return normalized


def normalize_person_context_proposal(row: dict) -> dict:
    proposal = row.get("proposal")
    if isinstance(proposal, str):
        proposal = json.loads(proposal)
    source_refs = row.get("source_refs")
    if isinstance(source_refs, str):
        source_refs = json.loads(source_refs)
    return {
        "id": str(row["id"]),
        "tenant_id": str(row["tenant_id"]),
        "owner_user_id": str(row["owner_user_id"]),
        "subject_entity_id": str(row["subject_entity_id"]),
        "status": row.get("status"),
        "proposal": proposal or {},
        "source_refs": source_refs or [],
        "proposal_fingerprint": row.get("proposal_fingerprint"),
        "evidence_ref_hash": row.get("evidence_ref_hash"),
        "context_pack_id": str(row["context_pack_id"]) if row.get("context_pack_id") else None,
        "model_call_id": str(row["model_call_id"]) if row.get("model_call_id") else None,
        "decided_by": str(row["decided_by"]) if row.get("decided_by") else None,
        "decided_at": _isoformat_value(row.get("decided_at")),
        "review_note": row.get("review_note"),
        "created_at": _isoformat_value(row.get("created_at")),
    }


async def list_person_context_proposals(*, tenant_id: str, owner_user_id: str, subject_entity_id: str | None = None, status: str | None = None) -> list[dict]:
    query = """
        SELECT id, tenant_id, owner_user_id, subject_entity_id, status, proposal, source_refs,
               proposal_fingerprint, evidence_ref_hash, context_pack_id, model_call_id,
               decided_by, decided_at, review_note, created_at
        FROM person_context_proposals
        WHERE tenant_id = $1 AND owner_user_id = $2
    """
    args: list = [tenant_id, owner_user_id]
    if subject_entity_id:
        query += " AND subject_entity_id = $3"
        args.append(subject_entity_id)
    if status:
        query += f" AND status = ${len(args) + 1}"
        args.append(status)
    query += " ORDER BY created_at DESC"
    async with acquire() as conn:
        rows = await conn.fetch(query, *args)
    return [normalize_person_context_proposal(dict(row)) for row in rows]


async def get_person_context_proposal(*, tenant_id: str, owner_user_id: str, subject_entity_id: str, proposal_id: str) -> dict | None:
    async with acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, tenant_id, owner_user_id, subject_entity_id, status, proposal, source_refs,
                   proposal_fingerprint, evidence_ref_hash, context_pack_id, model_call_id,
                   decided_by, decided_at, review_note, created_at
            FROM person_context_proposals
            WHERE tenant_id = $1 AND owner_user_id = $2 AND subject_entity_id = $3 AND id = $4
            """,
            tenant_id,
            owner_user_id,
            subject_entity_id,
            proposal_id,
        )
    return normalize_person_context_proposal(dict(row)) if row else None


async def create_person_context_proposal(
    *,
    tenant_id: str,
    owner_user_id: str,
    subject_entity_id: str,
    proposal: dict,
    context_pack_id: str | None,
    model_call_id: str | None,
    pipeline_run_id: str | None,
    origin: str,
) -> dict | None:
    if not proposal:
        return None
    source_refs = _proposal_source_refs(proposal)
    fingerprint = _proposal_fingerprint(proposal, source_refs)
    evidence_hash = _evidence_ref_hash(source_refs)
    async with acquire() as conn:
        await conn.fetchval(
            "SELECT id FROM users WHERE tenant_id = $1 AND id = $2",
            tenant_id,
            owner_user_id,
        )
        rejected = await conn.fetchval(
            """
            SELECT 1
            FROM person_context_rejection_keys
            WHERE tenant_id = $1
              AND owner_user_id = $2
              AND subject_entity_id = $3
              AND proposal_fingerprint = $4
              AND evidence_ref_hash = $5
            LIMIT 1
            """,
            tenant_id,
            owner_user_id,
            subject_entity_id,
            fingerprint,
            evidence_hash,
        )
        if rejected:
            return None
        existing = await conn.fetchval(
            """
            SELECT id
            FROM person_context_proposals
            WHERE tenant_id = $1 AND owner_user_id = $2 AND subject_entity_id = $3 AND status = 'pending_review'
            LIMIT 1
            """,
            tenant_id,
            owner_user_id,
            subject_entity_id,
        )
        if existing:
            return None
        row = await conn.fetchrow(
            """
            INSERT INTO person_context_proposals (
                tenant_id, owner_user_id, subject_entity_id, status, proposal, source_refs,
                proposal_fingerprint, evidence_ref_hash, context_pack_id, model_call_id
            )
            VALUES ($1, $2, $3, 'pending_review', $4::jsonb, $5::jsonb, $6, $7, $8, $9)
            RETURNING id, tenant_id, owner_user_id, subject_entity_id, status, proposal, source_refs,
                      proposal_fingerprint, evidence_ref_hash, context_pack_id, model_call_id,
                      decided_by, decided_at, review_note, created_at
            """,
            tenant_id,
            owner_user_id,
            subject_entity_id,
            json.dumps(proposal),
            json.dumps(source_refs),
            fingerprint,
            evidence_hash,
            context_pack_id,
            model_call_id,
        )
    result = normalize_person_context_proposal(dict(row)) if row else None
    if result:
        logger.info(
            "person_context_proposed",
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            subject_entity_id=subject_entity_id,
            origin=origin,
            pipeline_run_id=pipeline_run_id,
        )
    return result


async def approve_person_context_proposal(
    *,
    tenant_id: str,
    owner_user_id: str,
    subject_entity_id: str,
    proposal_id: str,
    reviewed_by: str,
    review_note: str | None = None,
) -> dict:
    async with acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, tenant_id, owner_user_id, subject_entity_id, status, proposal, source_refs,
                   proposal_fingerprint, evidence_ref_hash, context_pack_id, model_call_id,
                   decided_by, decided_at, review_note, created_at
            FROM person_context_proposals
            WHERE tenant_id = $1 AND owner_user_id = $2 AND subject_entity_id = $3 AND id = $4
            FOR UPDATE
            """,
            tenant_id,
            owner_user_id,
            subject_entity_id,
            proposal_id,
        )
        if not row:
            raise LookupError("person context proposal not found")
        proposal = normalize_person_context_proposal(dict(row))
        if proposal["status"] != "pending_review":
            raise ValueError(f"proposal already {proposal['status']}")
        current_row = await _fetch_person_context_row(
            conn,
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            subject_entity_id=subject_entity_id,
            for_update=True,
        )
        current = normalize_person_context(
            dict(current_row) if current_row else None,
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            subject_entity_id=subject_entity_id,
        )
        next_context = _merge_context_patch(current, proposal["proposal"])
        next_context["evidence_refs"] = proposal["source_refs"] or _proposal_source_refs(proposal["proposal"])
        next_context["status"] = "active"
        next_context["last_confirmed_at"] = _now_iso()
        persisted_row = await _persist_person_context_conn(
            conn,
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            subject_entity_id=subject_entity_id,
            updated_by=reviewed_by,
            context=next_context,
        )
        persisted = normalize_person_context(
            dict(persisted_row),
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            subject_entity_id=subject_entity_id,
        )
        await conn.execute(
            """
            UPDATE person_context_proposals
            SET status = 'approved', decided_by = $5, decided_at = now(), review_note = NULLIF($6, '')
            WHERE tenant_id = $1 AND owner_user_id = $2 AND subject_entity_id = $3 AND id = $4
            """,
            tenant_id,
            owner_user_id,
            subject_entity_id,
            proposal_id,
            reviewed_by,
            review_note or "",
        )
        await record_person_context_event(
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            subject_entity_id=subject_entity_id,
            actor_user_id=reviewed_by,
            event_type="proposal_approved",
            previous_state=current,
            new_state=persisted,
            source_refs=proposal["source_refs"],
            proposal_id=proposal_id,
            note=review_note,
        )
        # Slice 4 (packpolicy-digests packet): populate context_reference_index
        # from the just-approved row's cited refs, delete-then-reinsert, same
        # transaction as the approval itself. Derived via
        # context_refs_from_entries (each entry's own refs/source_refs field),
        # NOT the row's top-level evidence_refs column -- that column is set
        # from the originating proposal's refs at proposal-creation time and
        # can drift from the final merged entry set once _merge_context_patch
        # combines it with pre-existing entries. context_refs_from_entries
        # matches what tenant_context.py/channel_context.py already do and
        # what rebuild_context_reference_index re-derives from, so live-write
        # and rebuild are guaranteed to agree for this context kind too.
        # Note: normalize_person_context flattens relationship_kind/
        # sentiment_trend to plain strings (refs don't survive that
        # normalization step), so those two object_fields are a no-op today
        # in practice -- kept for parity with the rebuild call site rather
        # than silently dropped, since fixing the underlying normalization
        # gap is out of scope for this invalidation slice.
        from app.brain.context_invalidation import context_refs_from_entries, replace_context_reference_index

        await replace_context_reference_index(
            conn,
            tenant_id=tenant_id,
            context_kind="person_context",
            context_id=persisted["id"],
            refs=context_refs_from_entries(
                persisted,
                list_fields=("topic_stances", "communication_preferences", "sentiment_history"),
                object_fields=("relationship_kind", "sentiment_trend"),
            ),
        )
    return persisted


async def reject_person_context_proposal(
    *,
    tenant_id: str,
    owner_user_id: str,
    subject_entity_id: str,
    proposal_id: str,
    reviewed_by: str,
    review_note: str | None = None,
) -> None:
    async with acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, tenant_id, owner_user_id, subject_entity_id, status, proposal, source_refs,
                   proposal_fingerprint, evidence_ref_hash, context_pack_id, model_call_id,
                   decided_by, decided_at, review_note, created_at
            FROM person_context_proposals
            WHERE tenant_id = $1 AND owner_user_id = $2 AND subject_entity_id = $3 AND id = $4
            FOR UPDATE
            """,
            tenant_id,
            owner_user_id,
            subject_entity_id,
            proposal_id,
        )
        if not row:
            raise LookupError("person context proposal not found")
        proposal = normalize_person_context_proposal(dict(row))
        if proposal["status"] != "pending_review":
            raise ValueError(f"proposal already {proposal['status']}")
        await conn.execute(
            """
            UPDATE person_context_proposals
            SET status = 'rejected', decided_by = $5, decided_at = now(), review_note = NULLIF($6, '')
            WHERE tenant_id = $1 AND owner_user_id = $2 AND subject_entity_id = $3 AND id = $4
            """,
            tenant_id,
            owner_user_id,
            subject_entity_id,
            proposal_id,
            reviewed_by,
            review_note or "",
        )
        await conn.execute(
            """
            INSERT INTO person_context_rejection_keys (
                tenant_id, owner_user_id, subject_entity_id, proposal_fingerprint, evidence_ref_hash, rejection_reason
            )
            VALUES ($1, $2, $3, $4, $5, NULLIF($6, ''))
            ON CONFLICT (tenant_id, owner_user_id, subject_entity_id, proposal_fingerprint, evidence_ref_hash)
            DO UPDATE SET rejected_at = now(), rejection_reason = EXCLUDED.rejection_reason
            """,
            tenant_id,
            owner_user_id,
            subject_entity_id,
            proposal["proposal_fingerprint"],
            proposal["evidence_ref_hash"],
            review_note or "",
        )
        current = await get_person_context(tenant_id=tenant_id, owner_user_id=owner_user_id, subject_entity_id=subject_entity_id)
        await record_person_context_event(
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            subject_entity_id=subject_entity_id,
            actor_user_id=reviewed_by,
            event_type="proposal_rejected",
            previous_state=current,
            new_state=current,
            source_refs=proposal["source_refs"],
            proposal_id=proposal_id,
            note=review_note,
        )


async def confirm_person_context(*, tenant_id: str, owner_user_id: str, subject_entity_id: str, actor_user_id: str | None) -> dict | None:
    async with acquire() as conn:
        current_row = await _fetch_person_context_row(
            conn,
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            subject_entity_id=subject_entity_id,
            for_update=True,
        )
        current = normalize_person_context(
            dict(current_row) if current_row else None,
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            subject_entity_id=subject_entity_id,
        )
        if not _context_active(current):
            return None
        next_context = deepcopy(current)
        now = _now_iso()
        next_context["status"] = "active"
        next_context["last_confirmed_at"] = now
        row = await _persist_person_context_conn(
            conn,
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            subject_entity_id=subject_entity_id,
            updated_by=actor_user_id or current.get("updated_by"),
            context=next_context,
        )
    return normalize_person_context(
        dict(row),
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        subject_entity_id=subject_entity_id,
    )


SYSTEM_PERSON_CONTEXT_SEED = """You draft a private relationship model for one user about one person they work through. Use ONLY the provided sources. This model is owner-private and must never be generalized beyond the named owner. Identify the person's relationship kind, topic stances, communication preferences, and sentiment trend/history when the evidence supports it. Return fewer, well-supported entries over speculative ones. Every claim must cite at least one source. If the sources do not support a category, return an empty list or null rather than guessing."""

PROMPT_PERSON_CONTEXT_SEED = """<subject>
{subject}
</subject>

<context>
{context}
</context>

Draft the private relationship model from the sources above.

Return ONLY JSON matching this shape:
{{
  "relationship_kind": {{"text": "<manager|peer|report|customer|investor|other>", "refs": ["kind:id", ...]}} or null,
  "topic_stances": [{{"topic": "<topic>", "stance": "<short phrase>", "refs": ["kind:id", ...]}}],
  "communication_preferences": [{{"text": "<short phrase>", "refs": ["kind:id", ...]}}],
  "sentiment_trend": {{"text": "<warming|cooling|stable>", "refs": ["kind:id", ...]}} or null,
  "sentiment_history": [{{"text": "<short phrase>", "refs": ["kind:id", ...]}}]
}}

Rules:
- Every entry needs at least one ref copied exactly from a source tag.
- Empty lists and null values are valid when the sources do not support a category.
- Do not invent a relationship kind or a sentiment trend."""

SYSTEM_PERSON_CONTEXT_UPDATE = """You review a private relationship model against fresh evidence and propose a bounded update. Use ONLY the provided sources and the current context. Keep the model owner-private. Only propose a change when the fresh evidence materially supports it. Every claim must cite at least one source."""

PROMPT_PERSON_CONTEXT_UPDATE = """<subject>
{subject}
</subject>

<current_context>
{current_context}
</current_context>

<fresh_context>
{fresh_context}
</fresh_context>

Propose updates to the private relationship model.

Return ONLY JSON matching this shape:
{{
  "relationship_kind": {{"text": "<manager|peer|report|customer|investor|other>", "refs": ["kind:id", ...]}} or null,
  "topic_stances": [{{"topic": "<topic>", "stance": "<short phrase>", "refs": ["kind:id", ...]}}],
  "communication_preferences": [{{"text": "<short phrase>", "refs": ["kind:id", ...]}}],
  "sentiment_trend": {{"text": "<warming|cooling|stable>", "refs": ["kind:id", ...]}} or null,
  "sentiment_history": [{{"text": "<short phrase>", "refs": ["kind:id", ...]}}]
}}

Rules:
- Empty lists and null values are valid when the sources do not support a category.
- Do not invent a relationship kind or sentiment trend.
- Return only the fields that should change; unchanged categories may be omitted."""


def _proposal_to_patch(proposal: dict) -> dict:
    patch: dict = {}
    for field in ("relationship_kind", "sentiment_trend"):
        if field in proposal:
            value = proposal.get(field)
            patch[field] = value.get("text") if isinstance(value, dict) else value
    for field in ("topic_stances", "communication_preferences", "sentiment_history"):
        if field in proposal:
            patch[field] = proposal.get(field) or []
    if "evidence_refs" in proposal:
        patch["evidence_refs"] = proposal.get("evidence_refs") or []
    return patch


def _person_summary(context: dict) -> str:
    bundle = person_context_bundle(context)
    subject = bundle["subject_entity_name"] or context.get("subject_entity_id")
    lines: list[str] = []
    if bundle["relationship_kind"]:
        lines.append(f"Relationship kind: {bundle['relationship_kind']}")
    if bundle["topic_stances"]:
        lines.append(f"Topic stances: {', '.join(bundle['topic_stances'])}")
    if bundle["communication_preferences"]:
        lines.append(f"Communication preferences: {', '.join(bundle['communication_preferences'])}")
    if bundle["sentiment_trend"]:
        lines.append(f"Sentiment trend: {bundle['sentiment_trend']}")
    if bundle["sentiment_history"]:
        lines.append(f"Sentiment history: {', '.join(bundle['sentiment_history'])}")
    return f"{subject}\n" + ("\n".join(lines) if lines else "No approved person context yet.")


async def maybe_seed_person_context(
    *,
    tenant_id: str,
    owner_user_id: str,
    subject_entity_id: str,
    pipeline_run_id: str | None = None,
) -> dict | None:
    current = await get_person_context(tenant_id=tenant_id, owner_user_id=owner_user_id, subject_entity_id=subject_entity_id)
    if _context_active(current):
        return None
    if await list_person_context_proposals(tenant_id=tenant_id, owner_user_id=owner_user_id, subject_entity_id=subject_entity_id, status="pending_review"):
        return None
    settings = await get_tenant_settings(tenant_id)
    actor_principals = [f"company:{tenant_id}", *(await effective_principals_for_user(tenant_id=tenant_id, user_id=owner_user_id))]
    from app.retrieval.retrieve import build_authorized_context_pack

    pack = await build_authorized_context_pack(
        tenant_id=tenant_id,
        actor_user_id=owner_user_id,
        target_audience="private_user",
        purpose="person_context_seed",
        destination="person_context_seed",
        actor_principals=actor_principals,
        recent_hours=72,
        token_budget=PACK_TOKEN_BUDGET,
    )
    if not pack["items"]:
        return None
    subject = await _subject_label(tenant_id=tenant_id, subject_entity_id=subject_entity_id)
    result = await call_llm(
        task="person_context_seed",
        system=SYSTEM_PERSON_CONTEXT_SEED,
        prompt=PROMPT_PERSON_CONTEXT_SEED.format(subject=subject, context=pack["text"]),
        tenant_id=tenant_id,
        tenant_settings=settings,
        max_tokens=1500,
        expect_json=True,
        prompt_version=SEED_PROMPT_VERSION,
        context_pack_id=pack["context_pack_id"],
        pipeline_run_id=pipeline_run_id,
        validation_pack_items=pack["items"],
    )
    parsed = result["parsed"] or {
        "relationship_kind": None,
        "topic_stances": [],
        "communication_preferences": [],
        "sentiment_trend": None,
        "sentiment_history": [],
    }
    check = validate_person_context_seed(parsed, pack_items=pack["items"])
    if not check.valid:
        return None
    return await create_person_context_proposal(
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        subject_entity_id=subject_entity_id,
        proposal=parsed,
        context_pack_id=pack["context_pack_id"],
        model_call_id=result["model_call_id"],
        pipeline_run_id=pipeline_run_id,
        origin="seed",
    )


async def maybe_propose_person_context_update(
    *,
    tenant_id: str,
    owner_user_id: str,
    subject_entity_id: str,
    context_pack: dict,
    pipeline_run_id: str | None = None,
) -> dict | None:
    current = await get_person_context(tenant_id=tenant_id, owner_user_id=owner_user_id, subject_entity_id=subject_entity_id)
    if not _context_active(current):
        return None
    if await list_person_context_proposals(tenant_id=tenant_id, owner_user_id=owner_user_id, subject_entity_id=subject_entity_id, status="pending_review"):
        return None
    settings = await get_tenant_settings(tenant_id)
    actor_principals = [f"company:{tenant_id}", *(await effective_principals_for_user(tenant_id=tenant_id, user_id=owner_user_id))]
    from app.retrieval.retrieve import build_authorized_context_pack

    pack = await build_authorized_context_pack(
        tenant_id=tenant_id,
        actor_user_id=owner_user_id,
        target_audience="private_user",
        purpose="person_context_update",
        destination="person_context_update",
        actor_principals=actor_principals,
        query=None,
        recent_hours=72,
        token_budget=PACK_TOKEN_BUDGET,
    )
    fresh_text = context_pack["text"] if isinstance(context_pack, dict) else ""
    if not pack["items"] or not fresh_text.strip():
        return None
    subject = await _subject_label(tenant_id=tenant_id, subject_entity_id=subject_entity_id)
    result = await call_llm(
        task="person_context_update",
        system=SYSTEM_PERSON_CONTEXT_UPDATE,
        prompt=PROMPT_PERSON_CONTEXT_UPDATE.format(
            subject=subject,
            current_context=_person_summary(current),
            fresh_context=fresh_text,
        ),
        tenant_id=tenant_id,
        tenant_settings=settings,
        max_tokens=1500,
        expect_json=True,
        prompt_version=UPDATE_PROMPT_VERSION,
        context_pack_id=pack["context_pack_id"],
        pipeline_run_id=pipeline_run_id,
        validation_pack_items=pack["items"],
    )
    parsed = result["parsed"] or {}
    check = validate_person_context_update(parsed, pack_items=pack["items"])
    if not check.valid:
        return None
    return await create_person_context_proposal(
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        subject_entity_id=subject_entity_id,
        proposal=parsed,
        context_pack_id=pack["context_pack_id"],
        model_call_id=result["model_call_id"],
        pipeline_run_id=pipeline_run_id,
        origin="post_brief",
    )


async def _subject_label(*, tenant_id: str, subject_entity_id: str) -> str:
    async with acquire() as conn:
        row = await conn.fetchrow(
            "SELECT canonical_name FROM entity_registry WHERE tenant_id = $1 AND id = $2",
            tenant_id,
            subject_entity_id,
        )
    return str(row["canonical_name"]) if row and row.get("canonical_name") else subject_entity_id
