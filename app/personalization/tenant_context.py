"""Durable company-level operating context.

Slice 2.7 stores approved tenant-wide priorities, narratives, cadences, and a
current plan outside the model, injects that state into company-visible packs,
and routes machine-proposed changes through a review queue before anything
lands in the durable row.
"""
import json
import uuid
from copy import deepcopy
from datetime import datetime, timedelta, timezone

from app.core.db import acquire

ENTRY_FIELDS = ("priorities", "active_narratives", "cadences")
ACTIVE_STATUSES = {"active", "stale_flagged"}
STALE_AFTER_DAYS = 21


def _jsonb(value, fallback):
    if value is None:
        return deepcopy(fallback)
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return deepcopy(fallback)
    return deepcopy(value)


def _jsonb_param(value):
    return None if value is None else json.dumps(value)


def _clean_text(value: str | None) -> str:
    return " ".join(str(value or "").split()).strip()


def _clean_plan_text(value: str | None) -> str:
    lines: list[str] = []
    for raw_line in str(value or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        cleaned = " ".join(raw_line.split()).strip()
        if cleaned:
            lines.append(cleaned)
    return "\n".join(lines).strip()


def _dedupe_refs(refs) -> list[str]:
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


def _entry(
    *,
    kind: str,
    text: str,
    source_refs: list[str] | None = None,
    entry_id: str | None = None,
    status: str = "active",
    last_confirmed_at: str | None = None,
    added_by: str = "settings",
    added_at: str | None = None,
    okr_ref: str | None = None,
) -> dict:
    cleaned = _clean_text(text)
    return {
        "id": entry_id or str(uuid.uuid4()),
        "kind": kind,
        "text": cleaned,
        "source_refs": _dedupe_refs(source_refs),
        "okr_ref": _clean_text(okr_ref) or None,
        "status": status if status in {"active", "stale_flagged", "retired"} else "active",
        "last_confirmed_at": last_confirmed_at or _now_iso(),
        "added_by": added_by,
        "added_at": added_at or _now_iso(),
    }


def _normalize_entry(raw, *, kind: str) -> dict | None:
    if isinstance(raw, str):
        cleaned = _clean_text(raw)
        return _entry(kind=kind, text=cleaned) if cleaned else None
    if not isinstance(raw, dict):
        return None
    cleaned = _clean_text(raw.get("text"))
    if not cleaned:
        return None
    return _entry(
        kind=kind,
        text=cleaned,
        source_refs=raw.get("source_refs") or raw.get("refs") or [],
        entry_id=str(raw.get("id") or "") or None,
        status=str(raw.get("status") or "active"),
        last_confirmed_at=str(raw.get("last_confirmed_at") or "") or None,
        added_by=str(raw.get("added_by") or "settings"),
        added_at=str(raw.get("added_at") or "") or None,
        okr_ref=str(raw.get("okr_ref") or "") or None,
    )


def _normalize_entry_list(value, *, kind: str) -> list[dict]:
    raw = _jsonb(value, [])
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for item in raw:
        entry = _normalize_entry(item, kind=kind)
        if not entry:
            continue
        key = entry["id"]
        if key in seen:
            continue
        seen.add(key)
        out.append(entry)
    return out


def _normalize_plan(value) -> dict | None:
    value = _jsonb(value, None)
    if value is None:
        return None
    if isinstance(value, str):
        cleaned = _clean_plan_text(value)
        return _entry(kind="current_plan", text=cleaned) if cleaned else None
    if not isinstance(value, dict):
        return None
    cleaned = _clean_plan_text(value.get("text"))
    if not cleaned:
        return None
    return _entry(
        kind="current_plan",
        text=cleaned,
        source_refs=value.get("source_refs") or value.get("refs") or [],
        entry_id=str(value.get("id") or "") or None,
        status=str(value.get("status") or "active"),
        last_confirmed_at=str(value.get("last_confirmed_at") or "") or None,
        added_by=str(value.get("added_by") or "settings"),
        added_at=str(value.get("added_at") or "") or None,
    )


def _stale_status(entry: dict) -> str:
    status = str(entry.get("status") or "active")
    if status == "retired":
        return status
    confirmed = str(entry.get("last_confirmed_at") or "") or None
    if not confirmed:
        return "stale_flagged"
    try:
        confirmed_at = datetime.fromisoformat(confirmed.replace("Z", "+00:00"))
    except ValueError:
        return "stale_flagged"
    if datetime.now(timezone.utc) - confirmed_at >= timedelta(days=STALE_AFTER_DAYS):
        return "stale_flagged"
    return "active"


def normalize_tenant_context(row: dict | None, *, tenant_id: str) -> dict:
    row = row or {}
    context = {
        "id": str(row["id"]) if row.get("id") else None,
        "tenant_id": str(row.get("tenant_id") or tenant_id),
        "priorities": _normalize_entry_list(row.get("priorities"), kind="priority"),
        "active_narratives": _normalize_entry_list(row.get("active_narratives"), kind="narrative"),
        "cadences": _normalize_entry_list(row.get("cadences"), kind="cadence"),
        "current_plan": _normalize_plan(row.get("current_plan")),
        "updated_at": row.get("updated_at").isoformat() if row.get("updated_at") else None,
        "updated_by": str(row.get("updated_by")) if row.get("updated_by") else None,
    }
    for field in ENTRY_FIELDS:
        context[field] = [{**entry, "status": _stale_status(entry)} for entry in context[field]]
    if context["current_plan"]:
        context["current_plan"] = {**context["current_plan"], "status": _stale_status(context["current_plan"])}
    return context


async def get_tenant_context(*, tenant_id: str) -> dict:
    async with acquire() as conn:
        row = await _fetch_tenant_context_row(conn, tenant_id=tenant_id)
    return normalize_tenant_context(dict(row) if row else None, tenant_id=tenant_id)


def active_entries(context: dict, field: str) -> list[dict]:
    return [entry for entry in (context.get(field) or []) if entry.get("status") in ACTIVE_STATUSES]


def has_real_tenant_context(context: dict) -> bool:
    return any(active_entries(context, field) for field in ENTRY_FIELDS) or bool(
        context.get("current_plan") and context["current_plan"].get("status") in ACTIVE_STATUSES
    )


def tenant_context_bundle(context: dict) -> dict:
    return {
        "priorities": [entry["text"] for entry in active_entries(context, "priorities")],
        "active_narratives": [entry["text"] for entry in active_entries(context, "active_narratives")],
        "cadences": [entry["text"] for entry in active_entries(context, "cadences")],
        "current_plan": context["current_plan"]["text"] if context.get("current_plan") and context["current_plan"].get("status") in ACTIVE_STATUSES else None,
        "stale_items": [
            entry["text"]
            for field in ENTRY_FIELDS
            for entry in (context.get(field) or [])
            if entry.get("status") == "stale_flagged"
        ] + ([
            context["current_plan"]["text"],
        ] if context.get("current_plan") and context["current_plan"].get("status") == "stale_flagged" else []),
    }


def tenant_context_pack_item(context: dict) -> dict | None:
    bundle = tenant_context_bundle(context)
    lines: list[str] = []
    if bundle["priorities"]:
        lines.append(f"Priorities: {', '.join(bundle['priorities'])}.")
    if bundle["active_narratives"]:
        lines.append(f"Active narratives: {', '.join(bundle['active_narratives'])}.")
    if bundle["cadences"]:
        lines.append(f"Cadences: {', '.join(bundle['cadences'])}.")
    if bundle["current_plan"]:
        lines.append(f"Current plan: {bundle['current_plan']}.")
    if bundle["stale_items"]:
        lines.append(f"Needs reconfirm: {', '.join(bundle['stale_items'])}.")
    if not lines:
        return None
    tenant_id = str(context["tenant_id"])
    return {
        "kind": "tenant_context",
        "id": str(context.get("id") or tenant_id),
        "text": "Company operating context. " + " ".join(lines),
        "source_url": None,
        "priority": 5,
        "score": 1.0,
        "visibility": "company",
        "allowed_principals": [f"company:{tenant_id}"],
        "acl_state": "stamped",
        "acl_freshness_state": "fresh",
        "acl_basis": "tenant_operating_context",
        "acl_confidence": "high",
    }


def _merge_manual_entries(existing: list[dict], texts: list[str], *, kind: str) -> list[dict]:
    now = _now_iso()
    by_text = {_clean_text(entry.get("text")).lower(): entry for entry in existing}
    next_entries: list[dict] = []
    kept_ids: set[str] = set()
    for raw_text in texts:
        cleaned = _clean_text(raw_text)
        if not cleaned:
            continue
        current = by_text.get(cleaned.lower())
        if current:
            kept_ids.add(current["id"])
            next_entries.append({
                **current,
                "kind": kind,
                "text": cleaned,
                "status": "active",
                "last_confirmed_at": now,
            })
            continue
        next_entries.append(_entry(kind=kind, text=cleaned, source_refs=[], added_by="settings", added_at=now))
    for entry in existing:
        if entry["id"] in kept_ids:
            continue
        next_entries.append({**entry, "status": "retired"})
    return next_entries


def _cap_retired_entries(entries: list[dict], *, limit: int = 50) -> list[dict]:
    retired_indexes = [index for index, entry in enumerate(entries) if entry.get("status") == "retired"]
    if len(retired_indexes) <= limit:
        return list(entries)
    drop = set(retired_indexes[:-limit])
    return [entry for index, entry in enumerate(entries) if index not in drop]


def _next_current_plan(existing: dict | None, text: str | None) -> dict | None:
    cleaned = _clean_plan_text(text)
    if not cleaned:
        return None
    now = _now_iso()
    if existing and _clean_plan_text(existing.get("text")) == cleaned:
        return {**existing, "text": cleaned}
    return _entry(
        kind="current_plan",
        text=cleaned,
        source_refs=[],
        entry_id=str(existing.get("id")) if existing else None,
        added_by="settings",
        added_at=now,
    )


async def record_tenant_context_event(
    *,
    tenant_id: str,
    actor_user_id: str,
    event_type: str,
    previous_state: dict,
    new_state: dict,
    source_refs: list[str] | None = None,
    proposal_id: str | None = None,
    note: str | None = None,
) -> None:
    async with acquire() as conn:
        await _record_tenant_context_event_conn(
            conn,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            event_type=event_type,
            previous_state=previous_state,
            new_state=new_state,
            source_refs=source_refs,
            proposal_id=proposal_id,
            note=note,
        )


async def upsert_tenant_context(*, tenant_id: str, updated_by: str, patch: dict) -> dict:
    async with acquire() as conn:
        await _lock_tenant_row(conn, tenant_id=tenant_id)
        current_row = await _fetch_tenant_context_row(conn, tenant_id=tenant_id, for_update=True)
        current = normalize_tenant_context(dict(current_row) if current_row else None, tenant_id=tenant_id)
        next_context = deepcopy(current)
        if "priorities" in patch:
            next_context["priorities"] = _merge_manual_entries(current.get("priorities") or [], patch.get("priorities") or [], kind="priority")
        if "active_narratives" in patch:
            next_context["active_narratives"] = _merge_manual_entries(
                current.get("active_narratives") or [], patch.get("active_narratives") or [], kind="narrative"
            )
        if "cadences" in patch:
            next_context["cadences"] = _merge_manual_entries(current.get("cadences") or [], patch.get("cadences") or [], kind="cadence")
        if "current_plan" in patch:
            next_context["current_plan"] = _next_current_plan(current.get("current_plan"), patch.get("current_plan"))
        row = await _persist_tenant_context_conn(
            conn,
            tenant_id=tenant_id,
            updated_by=updated_by,
            context=next_context,
        )
        normalized = normalize_tenant_context(dict(row), tenant_id=tenant_id)
        await _record_tenant_context_event_conn(
            conn,
            tenant_id=tenant_id,
            actor_user_id=updated_by,
            event_type="settings_edit",
            previous_state=current,
            new_state=normalized,
            source_refs=[],
        )
    return normalized


def normalize_tenant_context_proposal(row: dict) -> dict:
    operations = row.get("operations")
    if isinstance(operations, str):
        operations = json.loads(operations)
    source_refs = row.get("source_refs")
    if isinstance(source_refs, str):
        source_refs = json.loads(source_refs)
    return {
        "id": str(row["id"]),
        "tenant_id": str(row["tenant_id"]),
        "origin": row.get("origin"),
        "operations": operations or [],
        "source_refs": source_refs or [],
        "status": row.get("status"),
        "review_note": row.get("review_note"),
        "reviewed_by": str(row["reviewed_by"]) if row.get("reviewed_by") else None,
        "reviewed_at": row["reviewed_at"].isoformat() if row.get("reviewed_at") else None,
        "context_pack_id": str(row["context_pack_id"]) if row.get("context_pack_id") else None,
        "model_call_id": str(row["model_call_id"]) if row.get("model_call_id") else None,
        "pipeline_run_id": str(row["pipeline_run_id"]) if row.get("pipeline_run_id") else None,
        "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
    }


async def list_tenant_context_proposals(*, tenant_id: str, status: str | None = None) -> list[dict]:
    query = """
        SELECT id, tenant_id, origin, operations, source_refs, pipeline_run_id,
               context_pack_id, model_call_id, status, reviewed_by, reviewed_at,
               review_note, created_at
        FROM tenant_context_proposals
        WHERE tenant_id = $1
    """
    args: list = [tenant_id]
    if status:
        query += " AND status = $2"
        args.append(status)
    query += " ORDER BY created_at DESC"
    async with acquire() as conn:
        rows = await conn.fetch(query, *args)
    return [normalize_tenant_context_proposal(dict(row)) for row in rows]


async def get_tenant_context_proposal(*, tenant_id: str, proposal_id: str) -> dict | None:
    async with acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, tenant_id, origin, operations, source_refs, pipeline_run_id,
                   context_pack_id, model_call_id, status, reviewed_by, reviewed_at,
                   review_note, created_at
            FROM tenant_context_proposals
            WHERE tenant_id = $1 AND id = $2
            """,
            tenant_id,
            proposal_id,
        )
    return normalize_tenant_context_proposal(dict(row)) if row else None


def _proposal_source_refs(operations: list[dict]) -> list[str]:
    refs: list[str] = []
    for op in operations:
        entry = op.get("entry") or {}
        refs.extend(entry.get("source_refs") or entry.get("refs") or [])
    return _dedupe_refs(refs)


def _apply_operations_to_context(context: dict, operations: list[dict]) -> dict:
    next_context = deepcopy(context)
    entry_maps = {field: {entry["id"]: {**entry} for entry in next_context.get(field) or []} for field in ENTRY_FIELDS}
    for op in operations:
        action = str(op.get("op") or "").strip().lower()
        field = str(op.get("field") or "").strip()
        if field == "current_plan":
            if action == "clear":
                next_context["current_plan"] = None
            elif action in {"set", "amend"}:
                plan = _normalize_plan(op.get("entry"))
                if plan:
                    next_context["current_plan"] = plan
            continue
        if field not in ENTRY_FIELDS:
            continue
        items = entry_maps[field]
        entry_id = str(op.get("entry_id") or (op.get("entry") or {}).get("id") or "")
        if action == "add":
            entry = _normalize_entry(op.get("entry"), kind={"priorities": "priority", "active_narratives": "narrative", "cadences": "cadence"}[field])
            if entry:
                items[entry["id"]] = entry
        elif action == "retire" and entry_id in items:
            items[entry_id] = {**items[entry_id], "status": "retired"}
        elif action == "amend" and entry_id in items:
            entry = _normalize_entry({**items[entry_id], **(op.get("entry") or {}), "id": entry_id}, kind=items[entry_id]["kind"])
            if entry:
                items[entry_id] = entry
        next_context[field] = list(items.values())
    return next_context


async def _persist_tenant_context(*, tenant_id: str, updated_by: str | None, context: dict) -> dict:
    async with acquire() as conn:
        row = await _persist_tenant_context_conn(
            conn,
            tenant_id=tenant_id,
            updated_by=updated_by,
            context=context,
        )
    return normalize_tenant_context(dict(row), tenant_id=tenant_id)


async def _lock_tenant_row(conn, *, tenant_id: str) -> None:
    await conn.fetchval("SELECT id FROM tenants WHERE id = $1 FOR UPDATE", tenant_id)


async def _fetch_tenant_context_row(conn, *, tenant_id: str, for_update: bool = False):
    query = """
        SELECT id, tenant_id, priorities, active_narratives, cadences, current_plan, updated_by, updated_at
        FROM tenant_contexts
        WHERE tenant_id = $1
    """
    if for_update:
        query += " FOR UPDATE"
    return await conn.fetchrow(query, tenant_id)


async def _record_tenant_context_event_conn(
    conn,
    *,
    tenant_id: str,
    actor_user_id: str | None,
    event_type: str,
    previous_state: dict,
    new_state: dict,
    source_refs: list[str] | None = None,
    proposal_id: str | None = None,
    note: str | None = None,
) -> None:
    await conn.execute(
        """
        INSERT INTO tenant_context_events (
            tenant_id, proposal_id, event_type, actor_user_id, note,
            previous_state, new_state, source_refs
        )
        VALUES ($1, $2, $3, $4, NULLIF($5, ''), $6::jsonb, $7::jsonb, $8::jsonb)
        """,
        tenant_id,
        proposal_id,
        event_type,
        actor_user_id,
        note or "",
        json.dumps(previous_state),
        json.dumps(new_state),
        json.dumps(_dedupe_refs(source_refs)),
    )


async def _persist_tenant_context_conn(conn, *, tenant_id: str, updated_by: str | None, context: dict):
    persisted_context = {
        **context,
        "priorities": _cap_retired_entries(context.get("priorities") or []),
        "active_narratives": _cap_retired_entries(context.get("active_narratives") or []),
        "cadences": _cap_retired_entries(context.get("cadences") or []),
    }
    return await conn.fetchrow(
        """
        INSERT INTO tenant_contexts (
            tenant_id, priorities, active_narratives, cadences, current_plan, updated_by
        )
        VALUES ($1, $2::jsonb, $3::jsonb, $4::jsonb, $5::jsonb, $6)
        ON CONFLICT (tenant_id) DO UPDATE
        SET priorities = EXCLUDED.priorities,
            active_narratives = EXCLUDED.active_narratives,
            cadences = EXCLUDED.cadences,
            current_plan = EXCLUDED.current_plan,
            updated_by = EXCLUDED.updated_by,
            updated_at = now()
        RETURNING id, tenant_id, priorities, active_narratives, cadences, current_plan, updated_by, updated_at
        """,
        tenant_id,
        _jsonb_param(persisted_context.get("priorities") or []),
        _jsonb_param(persisted_context.get("active_narratives") or []),
        _jsonb_param(persisted_context.get("cadences") or []),
        _jsonb_param(persisted_context.get("current_plan")),
        updated_by,
    )


async def create_tenant_context_proposal(
    *,
    tenant_id: str,
    origin: str,
    operations: list[dict],
    pipeline_run_id: str | None,
    context_pack_id: str | None,
    model_call_id: str | None,
) -> dict | None:
    if not operations:
        return None
    source_refs = _proposal_source_refs(operations)
    async with acquire() as conn:
        await _lock_tenant_row(conn, tenant_id=tenant_id)
        pending = await conn.fetchval(
            """
            SELECT id
            FROM tenant_context_proposals
            WHERE tenant_id = $1 AND status = 'pending_review'
            LIMIT 1
            """,
            tenant_id,
        )
        if pending:
            return None
        row = await conn.fetchrow(
            """
            INSERT INTO tenant_context_proposals (
                tenant_id, origin, operations, source_refs, pipeline_run_id, context_pack_id, model_call_id, status
            )
            VALUES ($1, $2, $3::jsonb, $4::jsonb, $5, $6, $7, 'pending_review')
            RETURNING id, tenant_id, origin, operations, source_refs, pipeline_run_id,
                      context_pack_id, model_call_id, status, reviewed_by, reviewed_at,
                      review_note, created_at
            """,
            tenant_id,
            origin,
            json.dumps(operations),
            json.dumps(source_refs),
            pipeline_run_id,
            context_pack_id,
            model_call_id,
        )
    return normalize_tenant_context_proposal(dict(row)) if row else None


async def approve_tenant_context_proposal(*, tenant_id: str, proposal_id: str, reviewed_by: str, review_note: str | None = None) -> dict:
    async with acquire() as conn:
        await _lock_tenant_row(conn, tenant_id=tenant_id)
        row = await conn.fetchrow(
            """
            SELECT id, tenant_id, origin, operations, source_refs, pipeline_run_id,
                   context_pack_id, model_call_id, status, reviewed_by, reviewed_at,
                   review_note, created_at
            FROM tenant_context_proposals
            WHERE tenant_id = $1 AND id = $2
            FOR UPDATE
            """,
            tenant_id,
            proposal_id,
        )
        if not row:
            raise LookupError("tenant context proposal not found")
        proposal = normalize_tenant_context_proposal(dict(row))
        if proposal["status"] != "pending_review":
            raise ValueError(f"proposal already {proposal['status']}")
        current_row = await _fetch_tenant_context_row(conn, tenant_id=tenant_id, for_update=True)
        current = normalize_tenant_context(dict(current_row) if current_row else None, tenant_id=tenant_id)
        next_context = _apply_operations_to_context(current, proposal["operations"])
        persisted_row = await _persist_tenant_context_conn(
            conn,
            tenant_id=tenant_id,
            updated_by=reviewed_by,
            context=next_context,
        )
        persisted = normalize_tenant_context(dict(persisted_row), tenant_id=tenant_id)
        await conn.execute(
            """
            UPDATE tenant_context_proposals
            SET status = 'approved', reviewed_by = $3, reviewed_at = now(), review_note = NULLIF($4, '')
            WHERE tenant_id = $1 AND id = $2
            """,
            tenant_id,
            proposal_id,
            reviewed_by,
            review_note or "",
        )
        await _record_tenant_context_event_conn(
            conn,
            tenant_id=tenant_id,
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
        # transaction as the approval itself.
        from app.brain.context_invalidation import context_refs_from_entries, replace_context_reference_index

        await replace_context_reference_index(
            conn,
            tenant_id=tenant_id,
            context_kind="tenant_context",
            context_id=persisted["id"],
            refs=context_refs_from_entries(
                persisted,
                list_fields=("priorities", "active_narratives", "cadences"),
                object_fields=("current_plan",),
            ),
        )
    return persisted


async def reject_tenant_context_proposal(*, tenant_id: str, proposal_id: str, reviewed_by: str, review_note: str | None = None) -> None:
    async with acquire() as conn:
        await _lock_tenant_row(conn, tenant_id=tenant_id)
        row = await conn.fetchrow(
            """
            SELECT id, tenant_id, origin, operations, source_refs, pipeline_run_id,
                   context_pack_id, model_call_id, status, reviewed_by, reviewed_at,
                   review_note, created_at
            FROM tenant_context_proposals
            WHERE tenant_id = $1 AND id = $2
            FOR UPDATE
            """,
            tenant_id,
            proposal_id,
        )
        if not row:
            raise LookupError("tenant context proposal not found")
        proposal = normalize_tenant_context_proposal(dict(row))
        if proposal["status"] != "pending_review":
            raise ValueError(f"proposal already {proposal['status']}")
        await conn.execute(
            """
            UPDATE tenant_context_proposals
            SET status = 'rejected', reviewed_by = $3, reviewed_at = now(), review_note = NULLIF($4, '')
            WHERE tenant_id = $1 AND id = $2
            """,
            tenant_id,
            proposal_id,
            reviewed_by,
            review_note or "",
        )


async def confirm_tenant_context(*, tenant_id: str, actor_user_id: str | None) -> dict | None:
    async with acquire() as conn:
        await _lock_tenant_row(conn, tenant_id=tenant_id)
        current_row = await _fetch_tenant_context_row(conn, tenant_id=tenant_id, for_update=True)
        current = normalize_tenant_context(dict(current_row) if current_row else None, tenant_id=tenant_id)
        if not has_real_tenant_context(current):
            return None
        now = _now_iso()
        next_context = deepcopy(current)
        for field in ENTRY_FIELDS:
            next_context[field] = [
                {**entry, "last_confirmed_at": now, "status": "active"}
                if entry.get("status") in ACTIVE_STATUSES else entry
                for entry in next_context.get(field) or []
            ]
        if next_context.get("current_plan") and next_context["current_plan"].get("status") in ACTIVE_STATUSES:
            next_context["current_plan"] = {
                **next_context["current_plan"],
                "last_confirmed_at": now,
                "status": "active",
            }
        row = await _persist_tenant_context_conn(
            conn,
            tenant_id=tenant_id,
            updated_by=actor_user_id or current.get("updated_by"),
            context=next_context,
        )
        persisted = normalize_tenant_context(dict(row), tenant_id=tenant_id)
        await _record_tenant_context_event_conn(
            conn,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id or current.get("updated_by"),
            event_type="confirmed",
            previous_state=current,
            new_state=persisted,
            source_refs=[],
        )
    return persisted


TENANT_CONTEXT_PACK_TOKEN_BUDGET = 30_000
TENANT_CONTEXT_SEED_PROMPT_VERSION = "v1.0"
TENANT_CONTEXT_UPDATE_PROMPT_VERSION = "v1.0"

SYSTEM_TENANT_CONTEXT_SEED = """You draft a first-pass company operating
context from imported company history. Use ONLY the provided sources.
Identify durable company priorities, active narratives, recurring cadences,
and a short current_plan. Each entry must be grounded in source refs.
Return fewer, well-supported entries over speculative ones."""

PROMPT_TENANT_CONTEXT_SEED = """<context>
{context}
</context>

Draft the tenant-level operating context from the sources above.

Return ONLY JSON matching this shape:
{{
  "priorities": [{{"text": "<short phrase>", "refs": ["kind:id", ...]}}],
  "active_narratives": [{{"text": "<short phrase>", "refs": ["kind:id", ...]}}],
  "cadences": [{{"text": "<recurring meeting or date>", "refs": ["kind:id", ...]}}],
  "current_plan": {{"text": "<1-3 sentences>", "refs": ["kind:id", ...]}} or null
}}

Rules:
- Empty lists and null current_plan are valid.
- Every entry needs at least one ref copied exactly from a source tag.
- Do not invent company plans, launch dates, or meetings."""

SYSTEM_TENANT_CONTEXT_UPDATE = """You review a company's current operating
context against fresh evidence and propose bounded changes. Use ONLY the
provided sources and current context. Propose add, retire, amend, or clear
operations. Each proposed new or amended entry must cite source refs."""

PROMPT_TENANT_CONTEXT_UPDATE = """<current_context>
{current_context}
</current_context>

<fresh_context>
{fresh_context}
</fresh_context>

Propose updates to the company operating context.

Return ONLY JSON matching this shape:
{{
  "operations": [
    {{
      "op": "add|retire|amend|set|clear",
      "field": "priorities|active_narratives|cadences|current_plan",
      "entry_id": "<required for retire/amend when field is not current_plan>",
      "entry": {{"text": "<new text>", "refs": ["kind:id", ...]}}
    }}
  ]
}}

Rules:
- Only propose a change when the fresh evidence materially supports it.
- Use "retire" for no-longer-true entries, "amend" for meaningful wording changes, "add" for new durable context.
- For current_plan use "set" with an entry or "clear".
- Do not emit duplicate adds for entries already in current context."""


def _draft_to_operations(draft: dict) -> list[dict]:
    operations: list[dict] = []
    for field in ENTRY_FIELDS:
        for raw in draft.get(field) or []:
            entry = _normalize_entry(raw, kind={"priorities": "priority", "active_narratives": "narrative", "cadences": "cadence"}[field])
            if entry:
                operations.append({"op": "add", "field": field, "entry": entry})
    current_plan = _normalize_plan(draft.get("current_plan"))
    if current_plan:
        operations.append({"op": "set", "field": "current_plan", "entry": current_plan})
    return operations


def _context_summary(context: dict) -> str:
    bundle = tenant_context_bundle(context)
    lines: list[str] = []
    if bundle["priorities"]:
        lines.append(f"Priorities: {', '.join(bundle['priorities'])}")
    if bundle["active_narratives"]:
        lines.append(f"Active narratives: {', '.join(bundle['active_narratives'])}")
    if bundle["cadences"]:
        lines.append(f"Cadences: {', '.join(bundle['cadences'])}")
    if bundle["current_plan"]:
        lines.append(f"Current plan: {bundle['current_plan']}")
    if not lines:
        return "No approved tenant context yet."
    return "\n".join(lines)


async def maybe_seed_tenant_context(*, tenant_id: str, pipeline_run_id: str | None = None) -> dict | None:
    current = await get_tenant_context(tenant_id=tenant_id)
    if has_real_tenant_context(current):
        return None
    if await list_tenant_context_proposals(tenant_id=tenant_id, status="pending_review"):
        return None
    from app.core.config import get_settings
    from app.core.tenant_settings import get_tenant_settings
    from app.llm.client import call_llm
    from app.retrieval.retrieve import build_authorized_context_pack
    from app.llm.validators import validate_tenant_context_seed

    settings = get_settings()
    pack = await build_authorized_context_pack(
        tenant_id=tenant_id,
        actor_user_id=None,
        target_audience="company",
        purpose="tenant_context_seed",
        destination="tenant_context_seed",
        recent_hours=settings.backfill_days * 24,
        token_budget=TENANT_CONTEXT_PACK_TOKEN_BUDGET,
    )
    if not pack["items"]:
        return None
    tenant_settings = await get_tenant_settings(tenant_id)
    result = await call_llm(
        task="tenant_context_seed",
        system=SYSTEM_TENANT_CONTEXT_SEED,
        prompt=PROMPT_TENANT_CONTEXT_SEED.format(context=pack["text"]),
        tenant_id=tenant_id,
        tenant_settings=tenant_settings,
        max_tokens=1500,
        expect_json=True,
        prompt_version=TENANT_CONTEXT_SEED_PROMPT_VERSION,
        context_pack_id=pack["context_pack_id"],
        pipeline_run_id=pipeline_run_id,
        validation_pack_items=pack["items"],
    )
    parsed = result["parsed"] or {"priorities": [], "active_narratives": [], "cadences": [], "current_plan": None}
    check = validate_tenant_context_seed(parsed, pack_items=pack["items"])
    if not check.valid:
        return None
    return await create_tenant_context_proposal(
        tenant_id=tenant_id,
        origin="seed",
        operations=_draft_to_operations(parsed),
        pipeline_run_id=pipeline_run_id,
        context_pack_id=pack["context_pack_id"],
        model_call_id=result["model_call_id"],
    )


async def maybe_propose_tenant_context_update(
    *,
    tenant_id: str,
    context_pack: dict,
    pipeline_run_id: str | None = None,
) -> dict | None:
    current = await get_tenant_context(tenant_id=tenant_id)
    if not has_real_tenant_context(current):
        return None
    if await list_tenant_context_proposals(tenant_id=tenant_id, status="pending_review"):
        return None
    from app.core.tenant_settings import get_tenant_settings
    from app.llm.client import call_llm
    from app.llm.validators import validate_tenant_context_update

    tenant_settings = await get_tenant_settings(tenant_id)
    result = await call_llm(
        task="tenant_context_update",
        system=SYSTEM_TENANT_CONTEXT_UPDATE,
        prompt=PROMPT_TENANT_CONTEXT_UPDATE.format(
            current_context=_context_summary(current),
            fresh_context=context_pack["text"],
        ),
        tenant_id=tenant_id,
        tenant_settings=tenant_settings,
        max_tokens=1500,
        expect_json=True,
        prompt_version=TENANT_CONTEXT_UPDATE_PROMPT_VERSION,
        context_pack_id=context_pack["context_pack_id"],
        pipeline_run_id=pipeline_run_id,
        validation_pack_items=context_pack["items"],
    )
    parsed = result["parsed"] or {"operations": []}
    check = validate_tenant_context_update(parsed, pack_items=context_pack["items"], current_context=current)
    if not check.valid:
        return None
    return await create_tenant_context_proposal(
        tenant_id=tenant_id,
        origin="post_brief",
        operations=parsed.get("operations") or [],
        pipeline_run_id=pipeline_run_id,
        context_pack_id=context_pack["context_pack_id"],
        model_call_id=result["model_call_id"],
    )
