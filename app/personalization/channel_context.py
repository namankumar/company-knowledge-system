"""Durable Slack channel temperature and room-shift context."""
from __future__ import annotations

import hashlib
import json
import uuid
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from collections import Counter, defaultdict
import re

import structlog

from app.core.db import acquire

logger = structlog.get_logger()

ACTIVE_STATUSES = {"active", "stale_flagged"}
STALE_AFTER_DAYS = 21
ROOM_SCAN_WINDOW_DAYS = 7
MIN_ACTIVE_MEMBERS = 3
ROOM_TEMPERATURES = {"warming", "stable", "cooling"}
ROOM_TEMPERATURE_ORDER = {"warming": 0, "stable": 1, "cooling": 2}
CHANNEL_TONE_PATTERNS = (
    ("short async updates", re.compile(r"^\s*[^!?]{0,120}\s*$")),
    ("question-driven coordination", re.compile(r"\?")),
    ("decision-oriented exchanges", re.compile(r"\b(decide|decision|approve|ship|launch|ready)\b", re.I)),
    ("risk-heavy coordination", re.compile(r"\b(blocked|risk|stuck|waiting|delay|slip)\b", re.I)),
)
ROOM_KEYWORDS = (
    ("launch timing", ("launch", "timing", "ship", "deadline")),
    ("blocked work", ("blocked", "stuck", "waiting", "delay", "slip")),
    ("decision needed", ("decision", "decide", "approve", "confirm")),
    ("risk watch", ("risk", "concern", "issue", "warning")),
    ("coordination load", ("thread", "follow up", "sync", "handoff", "mention")),
)


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


def _entry(raw, *, default_label: str) -> dict | None:
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
    text = _clean_text(raw.get("text") or raw.get(default_label) or raw.get("stance") or raw.get("topic") or raw.get("summary"))
    if not text:
        return None
    entry = {
        "id": str(raw.get("id") or uuid.uuid4()),
        "text": text,
        "label": _clean_text(raw.get("label") or default_label) or default_label,
        "refs": _clean_refs(raw.get("refs") or raw.get("source_refs")),
        "status": str(raw.get("status") or "active"),
        "created_at": str(raw.get("created_at") or _now_iso()),
        "updated_at": str(raw.get("updated_at") or _now_iso()),
    }
    if raw.get("window_start"):
        entry["window_start"] = _isoformat_value(raw.get("window_start"))
    if raw.get("window_end"):
        entry["window_end"] = _isoformat_value(raw.get("window_end"))
    if raw.get("temperature"):
        entry["temperature"] = _clean_text(raw.get("temperature")) or None
    if raw.get("source_url"):
        entry["source_url"] = _clean_text(raw.get("source_url")) or None
    return entry


def _entry_list(value, *, default_label: str) -> list[dict]:
    raw = _jsonb(value, [])
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for item in raw:
        entry = _entry(item, default_label=default_label)
        if not entry or entry["id"] in seen:
            continue
        seen.add(entry["id"])
        out.append(entry)
    return out


def _trend_history_list(value) -> list[dict]:
    raw = _jsonb(value, [])
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for item in raw:
        entry = _entry(item, default_label="trend")
        if not entry:
            continue
        marker = f"{entry.get('window_start') or ''}|{entry.get('window_end') or ''}|{entry.get('summary') or entry.get('text') or ''}"
        if marker in seen:
            continue
        seen.add(marker)
        out.append(entry)
    return out


def _jsonb_param(value):
    return None if value is None else json.dumps(value)


def normalize_channel_context(row: dict | None, *, tenant_id: str, channel_key: str) -> dict:
    row = row or {}
    allowed_principals = _clean_refs(_jsonb(row.get("allowed_principals"), []))
    status = _stale_status(_isoformat_value(row.get("last_confirmed_at")), status=str(row.get("status") or "active"))
    return {
        "id": str(row["id"]) if row.get("id") else None,
        "tenant_id": str(row.get("tenant_id") or tenant_id),
        "channel_key": str(row.get("channel_key") or channel_key),
        "source_resource_id": str(row.get("source_resource_id")) if row.get("source_resource_id") else None,
        "channel_name": row.get("channel_name"),
        "channel_type": row.get("channel_type"),
        "temperature": _clean_text(row.get("temperature")) or None,
        "dominant_concerns": _entry_list(row.get("dominant_concerns"), default_label="dominant_concern"),
        "tone_norms": _entry_list(row.get("tone_norms"), default_label="tone_norm"),
        "participant_dynamics": _entry_list(row.get("participant_dynamics"), default_label="participant_dynamics"),
        "trend_history": _trend_history_list(row.get("trend_history")),
        "allowed_principals": allowed_principals,
        "visibility": _clean_text(row.get("visibility")) or ("company" if any(principal.startswith("company:") for principal in allowed_principals) else "restricted"),
        "acl_basis": _clean_text(row.get("acl_basis")) or "channel_temperature_context",
        "acl_confidence": _clean_text(row.get("acl_confidence")) or "high",
        "acl_state": _clean_text(row.get("acl_state")) or "stamped",
        "acl_freshness_state": _clean_text(row.get("acl_freshness_state")) or "fresh",
        "last_confirmed_at": _isoformat_value(row.get("last_confirmed_at")),
        "status": status,
        "updated_at": _isoformat_value(row.get("updated_at")),
        "updated_by": str(row.get("updated_by")) if row.get("updated_by") else None,
    }


async def get_channel_context(*, tenant_id: str, channel_key: str) -> dict:
    async with acquire() as conn:
        row = await _fetch_channel_context_row(conn, tenant_id=tenant_id, channel_key=channel_key)
    return normalize_channel_context(dict(row) if row else None, tenant_id=tenant_id, channel_key=channel_key)


async def list_channel_contexts(*, tenant_id: str, status: str | None = None, channel_key: str | None = None) -> list[dict]:
    query = """
        SELECT id, tenant_id, channel_key, source_resource_id, channel_name, channel_type, temperature,
               dominant_concerns, tone_norms, participant_dynamics, trend_history, allowed_principals,
               visibility, acl_basis, acl_confidence, acl_state, acl_freshness_state, last_confirmed_at,
               status, updated_by, updated_at
        FROM channel_contexts
        WHERE tenant_id = $1
    """
    args: list = [tenant_id]
    if channel_key:
        query += " AND channel_key = $2"
        args.append(channel_key)
    if status:
        query += f" AND status = ${len(args) + 1}"
        args.append(status)
    query += " ORDER BY updated_at DESC, created_at DESC"
    async with acquire() as conn:
        rows = await conn.fetch(query, *args)
    return [normalize_channel_context(dict(row), tenant_id=tenant_id, channel_key=str(row["channel_key"])) for row in rows]


def channel_context_bundle(context: dict) -> dict:
    active_concerns = [entry["text"] for entry in context.get("dominant_concerns") or [] if entry.get("status") in ACTIVE_STATUSES]
    tone_norms = [entry["text"] for entry in context.get("tone_norms") or [] if entry.get("status") in ACTIVE_STATUSES]
    dynamics = [entry["text"] for entry in context.get("participant_dynamics") or [] if entry.get("status") in ACTIVE_STATUSES]
    trend_history = []
    for entry in context.get("trend_history") or []:
        if entry.get("status") not in ACTIVE_STATUSES:
            continue
        window_start = _clean_text(entry.get("window_start"))
        window_end = _clean_text(entry.get("window_end"))
        summary = _clean_text(entry.get("summary") or entry.get("text"))
        parts = []
        if window_start and window_end:
            parts.append(f"{window_start} to {window_end}")
        elif window_start:
            parts.append(window_start)
        if summary:
            parts.append(summary)
        if parts:
            trend_history.append(": ".join(parts) if len(parts) > 1 else parts[0])
    stale_items = [
        entry["text"]
        for field in ("dominant_concerns", "tone_norms", "participant_dynamics", "trend_history")
        for entry in (context.get(field) or [])
        if entry.get("status") == "stale_flagged"
    ]
    if context.get("status") == "stale_flagged":
        stale_items.append(f"{context.get('channel_name') or context.get('channel_key')} needs reconfirmation.")
    return {
        "channel_name": context.get("channel_name"),
        "channel_key": context.get("channel_key"),
        "temperature": context.get("temperature") if context.get("status") in ACTIVE_STATUSES else None,
        "dominant_concerns": active_concerns,
        "tone_norms": tone_norms,
        "participant_dynamics": dynamics,
        "trend_history": trend_history,
        "stale_items": stale_items,
    }


def channel_context_pack_item(context: dict) -> dict | None:
    bundle = channel_context_bundle(context)
    lines: list[str] = []
    channel_name = bundle["channel_name"] or f"channel {bundle['channel_key']}"
    if bundle["temperature"]:
        lines.append(f"Temperature: {bundle['temperature']}.")
    if bundle["dominant_concerns"]:
        lines.append(f"Dominant concerns: {', '.join(bundle['dominant_concerns'])}.")
    if bundle["tone_norms"]:
        lines.append(f"Tone norms: {', '.join(bundle['tone_norms'])}.")
    if bundle["participant_dynamics"]:
        lines.append(f"Participant dynamics: {', '.join(bundle['participant_dynamics'])}.")
    if bundle["trend_history"]:
        lines.append(f"Trend history: {', '.join(bundle['trend_history'])}.")
    if not lines:
        return None
    allowed_principals = [str(principal) for principal in (context.get("allowed_principals") or []) if str(principal).strip()]
    visibility = context.get("visibility") or ("company" if any(principal.startswith("company:") for principal in allowed_principals) else "restricted")
    latest_source_url = None
    if context.get("trend_history"):
        latest = next((entry for entry in reversed(context["trend_history"]) if entry.get("source_url")), None)
        latest_source_url = latest.get("source_url") if latest else None
    return {
        "kind": "channel_context",
        "id": str(context.get("id") or context.get("channel_key")),
        "text": f"Slack room model for {channel_name}. " + " ".join(lines),
        "source_url": latest_source_url,
        "priority": 5,
        "score": 1.0,
        "visibility": visibility,
        "allowed_principals": allowed_principals,
        "acl_state": context.get("acl_state") or "stamped",
        "acl_freshness_state": context.get("acl_freshness_state") or "fresh",
        "acl_basis": context.get("acl_basis") or "channel_temperature_context",
        "acl_confidence": context.get("acl_confidence") or "high",
    }


def _context_patch_from_proposal(proposal: dict) -> dict:
    patch = {
        "channel_name": proposal.get("channel_name"),
        "channel_type": proposal.get("channel_type"),
        "temperature": proposal.get("temperature"),
        "dominant_concerns": [],
        "tone_norms": [],
        "participant_dynamics": [],
        "trend_history": [],
        "allowed_principals": _clean_refs(proposal.get("allowed_principals")),
        "visibility": proposal.get("visibility"),
        "acl_basis": proposal.get("acl_basis"),
        "acl_confidence": proposal.get("acl_confidence"),
        "acl_state": proposal.get("acl_state") or "stamped",
        "acl_freshness_state": proposal.get("acl_freshness_state") or "fresh",
        "last_confirmed_at": proposal.get("last_confirmed_at"),
        "status": proposal.get("status"),
    }
    for field in ("dominant_concerns", "tone_norms", "participant_dynamics"):
        entries = []
        for entry in proposal.get(field) or []:
            text = _clean_text(entry.get("text") if isinstance(entry, dict) else entry)
            if not text:
                continue
            refs = _clean_refs(entry.get("refs") or entry.get("source_refs")) if isinstance(entry, dict) else []
            entries.append({"text": text, "refs": refs})
        patch[field] = entries
    entries = []
    for entry in proposal.get("trend_history") or []:
        if not isinstance(entry, dict):
            continue
        text = _clean_text(entry.get("summary") or entry.get("text"))
        if not text:
            continue
        trend_entry = {"text": text, "refs": _clean_refs(entry.get("refs") or entry.get("source_refs"))}
        for field in ("window_start", "window_end", "temperature", "source_url"):
            if entry.get(field):
                trend_entry[field] = entry.get(field)
        entries.append(trend_entry)
    patch["trend_history"] = entries
    if proposal.get("source_resource_id") is not None:
        patch["source_resource_id"] = proposal.get("source_resource_id")
    if "temperature" in proposal and not proposal.get("temperature"):
        patch["temperature"] = None
    return patch


def _merge_context_patch(current: dict, patch: dict) -> dict:
    next_context = deepcopy(current)
    for field in ("channel_name", "channel_type", "temperature", "visibility", "acl_basis", "acl_confidence", "acl_state", "acl_freshness_state", "source_resource_id"):
        if field in patch:
            next_context[field] = _clean_text(patch.get(field)) if isinstance(patch.get(field), str) else patch.get(field)
    if "allowed_principals" in patch:
        next_context["allowed_principals"] = _clean_refs(patch.get("allowed_principals"))
    if "dominant_concerns" in patch:
        next_context["dominant_concerns"] = []
        for item in patch.get("dominant_concerns") or []:
            entry = _entry(item, default_label="dominant_concern")
            if entry:
                next_context["dominant_concerns"].append(entry)
    if "tone_norms" in patch:
        next_context["tone_norms"] = []
        for item in patch.get("tone_norms") or []:
            entry = _entry(item, default_label="tone_norm")
            if entry:
                next_context["tone_norms"].append(entry)
    if "participant_dynamics" in patch:
        next_context["participant_dynamics"] = []
        for item in patch.get("participant_dynamics") or []:
            entry = _entry(item, default_label="participant_dynamics")
            if entry:
                next_context["participant_dynamics"].append(entry)
    if "trend_history" in patch:
        next_context["trend_history"] = []
        for item in patch.get("trend_history") or []:
            entry = _entry(item, default_label="trend")
            if entry:
                next_context["trend_history"].append(entry)
    if "last_confirmed_at" in patch:
        next_context["last_confirmed_at"] = _isoformat_value(patch.get("last_confirmed_at"))
    if "status" in patch and patch.get("status"):
        next_context["status"] = str(patch.get("status"))
    return next_context


def _proposal_fingerprint(proposal: dict, source_refs: list[str]) -> str:
    payload = json.dumps({"proposal": proposal, "source_refs": sorted(_clean_refs(source_refs))}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _evidence_ref_hash(source_refs: list[str]) -> str:
    payload = json.dumps(sorted(_clean_refs(source_refs)), separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _proposal_source_refs(proposal: dict) -> list[str]:
    refs: list[str] = []
    for field in ("dominant_concerns", "tone_norms", "participant_dynamics", "trend_history"):
        value = proposal.get(field)
        if isinstance(value, dict):
            refs.extend(value.get("refs") or value.get("source_refs") or [])
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    refs.extend(item.get("refs") or item.get("source_refs") or [])
    return _clean_refs(refs)


def _channel_context_fingerprint(context: dict) -> str:
    def _entry_signature(entry: dict | str | None) -> dict:
        if isinstance(entry, str):
            return {"text": _clean_text(entry), "refs": []}
        if not isinstance(entry, dict):
            return {"text": "", "refs": []}
        signature = {
            "text": _clean_text(entry.get("text") or entry.get("summary") or entry.get("name") or entry.get("label")),
            "refs": _clean_refs(entry.get("refs") or entry.get("source_refs")),
        }
        for field in ("window_start", "window_end", "temperature", "source_url"):
            if entry.get(field):
                signature[field] = _clean_text(entry.get(field)) if isinstance(entry.get(field), str) else _isoformat_value(entry.get(field))
        return signature

    return hashlib.sha256(
        json.dumps(
            {
                "channel_key": context.get("channel_key"),
                "channel_name": context.get("channel_name"),
                "channel_type": context.get("channel_type"),
                "temperature": context.get("temperature"),
                "dominant_concerns": [_entry_signature(entry) for entry in (context.get("dominant_concerns") or [])],
                "tone_norms": [_entry_signature(entry) for entry in (context.get("tone_norms") or [])],
                "participant_dynamics": [_entry_signature(entry) for entry in (context.get("participant_dynamics") or [])],
                "trend_history": [_entry_signature(entry) for entry in (context.get("trend_history") or [])],
                "allowed_principals": sorted(_clean_refs(context.get("allowed_principals") or [])),
                "visibility": context.get("visibility"),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


async def _fetch_channel_context_row(conn, *, tenant_id: str, channel_key: str, for_update: bool = False):
    query = """
        SELECT id, tenant_id, channel_key, source_resource_id, channel_name, channel_type, temperature,
               dominant_concerns, tone_norms, participant_dynamics, trend_history, allowed_principals,
               visibility, acl_basis, acl_confidence, acl_state, acl_freshness_state, last_confirmed_at,
               status, updated_by, updated_at
        FROM channel_contexts
        WHERE tenant_id = $1 AND channel_key = $2
    """
    if for_update:
        query += " FOR UPDATE"
    return await conn.fetchrow(query, tenant_id, channel_key)


async def record_channel_context_event(
    *,
    tenant_id: str,
    channel_key: str,
    actor_user_id: str | None,
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
            INSERT INTO channel_context_events (
                tenant_id, channel_context_id, channel_key, proposal_id, event_type,
                actor_user_id, note, previous_state, new_state, source_refs
            )
            VALUES (
                $1,
                (SELECT id FROM channel_contexts WHERE tenant_id = $1 AND channel_key = $2),
                $2, $3, $4, $5, NULLIF($6, ''), $7::jsonb, $8::jsonb, $9::jsonb
            )
            """,
            tenant_id,
            channel_key,
            proposal_id,
            event_type,
            actor_user_id,
            note or "",
            json.dumps(previous_state),
            json.dumps(new_state),
            json.dumps(_clean_refs(source_refs)),
        )


async def _record_channel_context_event_conn(
    conn,
    *,
    tenant_id: str,
    channel_key: str,
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
        INSERT INTO channel_context_events (
            tenant_id, channel_context_id, channel_key, proposal_id, event_type,
            actor_user_id, note, previous_state, new_state, source_refs
        )
        VALUES (
            $1,
            (SELECT id FROM channel_contexts WHERE tenant_id = $1 AND channel_key = $2),
            $2, $3, $4, $5, NULLIF($6, ''), $7::jsonb, $8::jsonb, $9::jsonb
        )
        """,
        tenant_id,
        channel_key,
        proposal_id,
        event_type,
        actor_user_id,
        note or "",
        json.dumps(previous_state),
        json.dumps(new_state),
        json.dumps(_clean_refs(source_refs)),
    )


async def _persist_channel_context_conn(conn, *, tenant_id: str, updated_by: str | None, context: dict):
    return await conn.fetchrow(
        """
        INSERT INTO channel_contexts (
            tenant_id, channel_key, source_resource_id, channel_name, channel_type, temperature,
            dominant_concerns, tone_norms, participant_dynamics, trend_history, allowed_principals,
            visibility, acl_basis, acl_confidence, acl_state, acl_freshness_state, last_confirmed_at,
            status, updated_by
        )
        VALUES (
            $1, $2, $3, $4, $5, $6,
            $7::jsonb, $8::jsonb, $9::jsonb, $10::jsonb, $11::jsonb,
            $12, $13, $14, $15, $16, $17, $18, $19
        )
        ON CONFLICT (tenant_id, channel_key) DO UPDATE
        SET source_resource_id = EXCLUDED.source_resource_id,
            channel_name = EXCLUDED.channel_name,
            channel_type = EXCLUDED.channel_type,
            temperature = EXCLUDED.temperature,
            dominant_concerns = EXCLUDED.dominant_concerns,
            tone_norms = EXCLUDED.tone_norms,
            participant_dynamics = EXCLUDED.participant_dynamics,
            trend_history = EXCLUDED.trend_history,
            allowed_principals = EXCLUDED.allowed_principals,
            visibility = EXCLUDED.visibility,
            acl_basis = EXCLUDED.acl_basis,
            acl_confidence = EXCLUDED.acl_confidence,
            acl_state = EXCLUDED.acl_state,
            acl_freshness_state = EXCLUDED.acl_freshness_state,
            last_confirmed_at = EXCLUDED.last_confirmed_at,
            status = EXCLUDED.status,
            updated_by = EXCLUDED.updated_by,
            updated_at = now()
        RETURNING id, tenant_id, channel_key, source_resource_id, channel_name, channel_type, temperature,
                  dominant_concerns, tone_norms, participant_dynamics, trend_history, allowed_principals,
                  visibility, acl_basis, acl_confidence, acl_state, acl_freshness_state, last_confirmed_at,
                  status, updated_by, updated_at
        """,
        tenant_id,
        context.get("channel_key"),
        context.get("source_resource_id"),
        context.get("channel_name"),
        context.get("channel_type"),
        context.get("temperature"),
        _jsonb_param(context.get("dominant_concerns") or []),
        _jsonb_param(context.get("tone_norms") or []),
        _jsonb_param(context.get("participant_dynamics") or []),
        _jsonb_param(context.get("trend_history") or []),
        _jsonb_param(context.get("allowed_principals") or []),
        context.get("visibility"),
        context.get("acl_basis"),
        context.get("acl_confidence"),
        context.get("acl_state") or "stamped",
        context.get("acl_freshness_state") or "fresh",
        context.get("last_confirmed_at"),
        context.get("status") or "active",
        updated_by,
    )


async def upsert_channel_context(*, tenant_id: str, updated_by: str, patch: dict) -> dict:
    channel_key = _clean_text(patch.get("channel_key")) if patch.get("channel_key") else None
    if not channel_key:
        raise ValueError("channel_key is required")
    async with acquire() as conn:
        current_row = await _fetch_channel_context_row(conn, tenant_id=tenant_id, channel_key=channel_key, for_update=True)
        current = normalize_channel_context(dict(current_row) if current_row else None, tenant_id=tenant_id, channel_key=channel_key)
        next_context = _merge_context_patch(current, patch)
        if not next_context.get("last_confirmed_at"):
            next_context["last_confirmed_at"] = _now_iso()
        next_context["status"] = "active"
        row = await _persist_channel_context_conn(
            conn,
            tenant_id=tenant_id,
            updated_by=updated_by,
            context=next_context,
        )
        normalized = normalize_channel_context(dict(row), tenant_id=tenant_id, channel_key=channel_key)
        await _record_channel_context_event_conn(
            conn,
            tenant_id=tenant_id,
            channel_key=channel_key,
            actor_user_id=updated_by,
            event_type="settings_edit",
            previous_state=current,
            new_state=normalized,
            source_refs=[],
        )
    return normalized


def normalize_channel_context_proposal(row: dict) -> dict:
    proposal = row.get("proposal")
    if isinstance(proposal, str):
        proposal = json.loads(proposal)
    source_refs = row.get("source_refs")
    if isinstance(source_refs, str):
        source_refs = json.loads(source_refs)
    return {
        "id": str(row["id"]),
        "tenant_id": str(row["tenant_id"]),
        "channel_key": str(row["channel_key"]),
        "source_resource_id": str(row["source_resource_id"]) if row.get("source_resource_id") else None,
        "status": row.get("status"),
        "proposal": proposal or {},
        "source_refs": source_refs or [],
        "proposal_fingerprint": row.get("proposal_fingerprint"),
        "evidence_ref_hash": row.get("evidence_ref_hash"),
        "context_pack_id": str(row["context_pack_id"]) if row.get("context_pack_id") else None,
        "model_call_id": str(row["model_call_id"]) if row.get("model_call_id") else None,
        "pipeline_run_id": str(row["pipeline_run_id"]) if row.get("pipeline_run_id") else None,
        "decided_by": str(row["decided_by"]) if row.get("decided_by") else None,
        "decided_at": _isoformat_value(row.get("decided_at")),
        "review_note": row.get("review_note"),
        "created_at": _isoformat_value(row.get("created_at")),
    }


async def list_channel_context_proposals(*, tenant_id: str, status: str | None = None, channel_key: str | None = None) -> list[dict]:
    query = """
        SELECT id, tenant_id, channel_key, source_resource_id, status, proposal, source_refs,
               proposal_fingerprint, evidence_ref_hash, context_pack_id, model_call_id,
               pipeline_run_id, decided_by, decided_at, review_note, created_at
        FROM channel_context_proposals
        WHERE tenant_id = $1
    """
    args: list = [tenant_id]
    if channel_key:
        query += " AND channel_key = $2"
        args.append(channel_key)
    if status:
        query += f" AND status = ${len(args) + 1}"
        args.append(status)
    query += " ORDER BY created_at DESC"
    async with acquire() as conn:
        rows = await conn.fetch(query, *args)
    return [normalize_channel_context_proposal(dict(row)) for row in rows]


async def get_channel_context_proposal(*, tenant_id: str, channel_key: str, proposal_id: str) -> dict | None:
    async with acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, tenant_id, channel_key, source_resource_id, status, proposal, source_refs,
                   proposal_fingerprint, evidence_ref_hash, context_pack_id, model_call_id,
                   pipeline_run_id, decided_by, decided_at, review_note, created_at
            FROM channel_context_proposals
            WHERE tenant_id = $1 AND channel_key = $2 AND id = $3
            """,
            tenant_id,
            channel_key,
            proposal_id,
        )
    return normalize_channel_context_proposal(dict(row)) if row else None


async def create_channel_context_proposal(
    *,
    tenant_id: str,
    channel_key: str,
    proposal: dict,
    source_refs: list[str],
    origin: str,
    pipeline_run_id: str | None = None,
    context_pack_id: str | None = None,
    model_call_id: str | None = None,
) -> dict | None:
    if not channel_key:
        return None
    source_refs = _clean_refs(source_refs)
    proposal_fingerprint = _proposal_fingerprint(proposal, source_refs)
    evidence_ref_hash = _evidence_ref_hash(source_refs)
    async with acquire() as conn:
        pending = await conn.fetchval(
            """
            SELECT id
            FROM channel_context_proposals
            WHERE tenant_id = $1 AND channel_key = $2 AND status = 'pending_review'
            LIMIT 1
            """,
            tenant_id,
            channel_key,
        )
        if pending:
            return None
        current_row = await _fetch_channel_context_row(conn, tenant_id=tenant_id, channel_key=channel_key)
        current = normalize_channel_context(dict(current_row) if current_row else None, tenant_id=tenant_id, channel_key=channel_key)
        normalized_proposal = _context_patch_from_proposal(proposal)
        refreshed = _merge_context_patch(current, normalized_proposal)
        if current and current.get("status") == "active" and _channel_context_fingerprint(current) == _channel_context_fingerprint(refreshed):
            refreshed["last_confirmed_at"] = proposal.get("last_confirmed_at") or _now_iso()
            refreshed["status"] = "active"
            persisted_row = await _persist_channel_context_conn(
                conn,
                tenant_id=tenant_id,
                updated_by=None,
                context=refreshed,
            )
            normalized = normalize_channel_context(dict(persisted_row), tenant_id=tenant_id, channel_key=channel_key)
            await _record_channel_context_event_conn(
                conn,
                tenant_id=tenant_id,
                channel_key=channel_key,
                actor_user_id=None,
                event_type="reconfirmed",
                previous_state=current,
                new_state=normalized,
                source_refs=source_refs,
            )
            return None
        row = await conn.fetchrow(
            """
            INSERT INTO channel_context_proposals (
                tenant_id, channel_key, source_resource_id, origin, proposal, source_refs,
                proposal_fingerprint, evidence_ref_hash, pipeline_run_id, context_pack_id, model_call_id, status
            )
            VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb, $7, $8, $9, $10, $11, 'pending_review')
            ON CONFLICT (tenant_id, channel_key) WHERE status = 'pending_review' DO NOTHING
            RETURNING id, tenant_id, channel_key, source_resource_id, status, proposal, source_refs,
                      proposal_fingerprint, evidence_ref_hash, context_pack_id, model_call_id,
                      pipeline_run_id, decided_by, decided_at, review_note, created_at
            """,
            tenant_id,
            channel_key,
            proposal.get("source_resource_id"),
            origin,
            json.dumps(proposal),
            json.dumps(source_refs),
            proposal_fingerprint,
            evidence_ref_hash,
            pipeline_run_id,
            context_pack_id,
            model_call_id,
        )
    return normalize_channel_context_proposal(dict(row)) if row else None


async def approve_channel_context_proposal(
    *,
    tenant_id: str,
    channel_key: str,
    proposal_id: str,
    reviewed_by: str,
    review_note: str | None = None,
) -> dict:
    async with acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, tenant_id, channel_key, source_resource_id, status, proposal, source_refs,
                   proposal_fingerprint, evidence_ref_hash, context_pack_id, model_call_id,
                   pipeline_run_id, decided_by, decided_at, review_note, created_at
            FROM channel_context_proposals
            WHERE tenant_id = $1 AND channel_key = $2 AND id = $3
            FOR UPDATE
            """,
            tenant_id,
            channel_key,
            proposal_id,
        )
        if not row:
            raise LookupError("channel context proposal not found")
        proposal = normalize_channel_context_proposal(dict(row))
        if proposal["status"] != "pending_review":
            raise ValueError(f"proposal already {proposal['status']}")
        current_row = await _fetch_channel_context_row(conn, tenant_id=tenant_id, channel_key=channel_key, for_update=True)
        current = normalize_channel_context(dict(current_row) if current_row else None, tenant_id=tenant_id, channel_key=channel_key)
        next_context = _merge_context_patch(current, _context_patch_from_proposal(proposal["proposal"]))
        next_context["channel_key"] = channel_key
        next_context["last_confirmed_at"] = _now_iso()
        next_context["status"] = "active"
        persisted_row = await _persist_channel_context_conn(
            conn,
            tenant_id=tenant_id,
            updated_by=reviewed_by,
            context=next_context,
        )
        persisted = normalize_channel_context(dict(persisted_row), tenant_id=tenant_id, channel_key=channel_key)
        await conn.execute(
            """
            UPDATE channel_context_proposals
            SET status = 'approved', decided_by = $4, decided_at = now(), review_note = NULLIF($5, '')
            WHERE tenant_id = $1 AND channel_key = $2 AND id = $3
            """,
            tenant_id,
            channel_key,
            proposal_id,
            reviewed_by,
            review_note or "",
        )
        await _record_channel_context_event_conn(
            conn,
            tenant_id=tenant_id,
            channel_key=channel_key,
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
            context_kind="channel_context",
            context_id=persisted["id"],
            refs=context_refs_from_entries(
                persisted,
                list_fields=("dominant_concerns", "tone_norms", "participant_dynamics", "trend_history"),
            ),
        )
    return persisted


async def reject_channel_context_proposal(
    *,
    tenant_id: str,
    channel_key: str,
    proposal_id: str,
    reviewed_by: str,
    review_note: str | None = None,
) -> None:
    async with acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, tenant_id, channel_key, source_resource_id, status, proposal, source_refs,
                   proposal_fingerprint, evidence_ref_hash, context_pack_id, model_call_id,
                   pipeline_run_id, decided_by, decided_at, review_note, created_at
            FROM channel_context_proposals
            WHERE tenant_id = $1 AND channel_key = $2 AND id = $3
            FOR UPDATE
            """,
            tenant_id,
            channel_key,
            proposal_id,
        )
        if not row:
            raise LookupError("channel context proposal not found")
        proposal = normalize_channel_context_proposal(dict(row))
        if proposal["status"] != "pending_review":
            raise ValueError(f"proposal already {proposal['status']}")
        await conn.execute(
            """
            UPDATE channel_context_proposals
            SET status = 'rejected', decided_by = $4, decided_at = now(), review_note = NULLIF($5, '')
            WHERE tenant_id = $1 AND channel_key = $2 AND id = $3
            """,
            tenant_id,
            channel_key,
            proposal_id,
            reviewed_by,
            review_note or "",
        )


async def _channel_event_rows(
    tenant_id: str,
    *,
    recent_hours: int = ROOM_SCAN_WINDOW_DAYS * 24,
) -> list[dict]:
    async with acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, actor_label, occurred_at, text, source_url, channel_key, metadata, allowed_principals
            FROM normalized_events
            WHERE tenant_id = $1
              AND source_type = 'slack'
              AND channel_key IS NOT NULL
              AND acl_state = 'stamped'
              AND acl_freshness_state = 'fresh'
              AND occurred_at >= now() - ($2 || ' hours')::interval
            ORDER BY occurred_at ASC
            """,
            tenant_id,
            recent_hours,
        )
    return [dict(row) for row in rows]


def _channel_temperature_from_daily(daily_counts: list[int], concern_hits: Counter[str]) -> str:
    if not daily_counts:
        return "stable"
    recent = sum(daily_counts[-2:]) / max(len(daily_counts[-2:]), 1)
    prior = sum(daily_counts[-4:-2]) / max(len(daily_counts[-4:-2]), 1) if len(daily_counts) >= 4 else recent
    concern_score = sum(concern_hits.values())
    if recent < prior * 0.8 and concern_score >= 2:
        return "cooling"
    if recent > prior * 1.25 and concern_score >= 1:
        return "warming"
    return "stable"


def _channel_concern_hits(texts: list[str]) -> Counter[str]:
    hits: Counter[str] = Counter()
    for text in texts:
        lower = text.lower()
        for label, keywords in ROOM_KEYWORDS:
            if all(keyword in lower for keyword in keywords):
                hits[label] += 1
        if "?" in text:
            hits["question traffic"] += 1
        if any(word in lower for word in ("blocked", "stuck", "waiting", "delay", "slip")):
            hits["blocked work"] += 1
        if any(word in lower for word in ("launch", "ship", "release", "deadline")):
            hits["launch timing"] += 1
    return hits


def _tone_norms_from_rows(rows: list[dict]) -> list[dict]:
    texts = [str(row.get("text") or "") for row in rows if str(row.get("text") or "").strip()]
    if not texts:
        return []
    norms: list[dict] = []
    avg_len = sum(len(text) for text in texts) / len(texts)
    if avg_len <= 150:
        norms.append({"text": "Short async updates dominate.", "refs": []})
    question_rate = sum(1 for text in texts if "?" in text) / len(texts)
    if question_rate >= 0.25:
        norms.append({"text": "Question-driven coordination is common.", "refs": []})
    mention_rate = sum(1 for text in texts if "@" in text or "<@" in text) / len(texts)
    if mention_rate >= 0.2:
        norms.append({"text": "Frequent direct mentions are normal here.", "refs": []})
    if any(pattern.search(text) for text in texts for _, pattern in CHANNEL_TONE_PATTERNS[2:]):
        norms.append({"text": "Conversation leans operational and decision-oriented.", "refs": []})
    if not norms:
        norms.append({"text": "Conversation stays steady and operational.", "refs": []})
    return norms[:3]


def _participant_dynamics_from_rows(rows: list[dict]) -> list[dict]:
    counts = Counter(str(row.get("actor_label") or "unknown") for row in rows if str(row.get("actor_label") or "").strip())
    if not counts:
        return []
    top = counts.most_common(3)
    if len(top) == 1:
        return [{"text": f"{top[0][0]} carries most of the channel's updates.", "refs": []}]
    lead_name, lead_count = top[0]
    second_name, second_count = top[1]
    if lead_count >= second_count * 1.5:
        text = f"{lead_name} drives most of the room's cadence, with {second_name} as the main counterweight."
    else:
        text = f"{lead_name} and {second_name} trade the most updates."
    if len(top) >= 3:
        text += f" {top[2][0]} shows up regularly too."
    return [{"text": text, "refs": []}]


def _dominant_concern_entries(rows: list[dict]) -> tuple[list[dict], list[str]]:
    texts = [str(row.get("text") or "") for row in rows if str(row.get("text") or "").strip()]
    hits = _channel_concern_hits(texts)
    if not hits:
        return [], []
    top_entries: list[dict] = []
    source_refs: list[str] = []
    for label, _count in hits.most_common(3):
        matching_row = next(
            (row for row in reversed(rows) if label in _channel_concern_hits([str(row.get("text") or "")])),
            None,
        )
        if not matching_row:
            continue
        source_ref = f"event:{matching_row['id']}"
        source_refs.append(source_ref)
        top_entries.append({
            "text": f"{label.title()} is still showing up in the room.",
            "refs": [source_ref],
        })
    return top_entries, source_refs


def _daily_trend_history(rows: list[dict]) -> tuple[list[dict], list[str], str | None]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        date_key = row["occurred_at"].astimezone(timezone.utc).date().isoformat()
        grouped[date_key].append(row)
    history: list[dict] = []
    evidence_refs: list[str] = []
    daily_counts: list[int] = []
    for day in sorted(grouped):
        day_rows = grouped[day]
        daily_counts.append(len(day_rows))
        concern_entries, concern_refs = _dominant_concern_entries(day_rows)
        evidence_refs.extend(concern_refs)
        text_bits = [f"{len(day_rows)} messages", f"{len({row.get('actor_label') for row in day_rows if row.get('actor_label')})} active members"]
        if concern_entries:
            text_bits.append(concern_entries[0]["text"])
        history.append({
            "window_start": f"{day}T00:00:00+00:00",
            "window_end": f"{day}T23:59:59+00:00",
            "temperature": None,
            "summary": "; ".join(text_bits),
            "refs": sorted(set(f"event:{row['id']}" for row in day_rows)),
            "status": "active",
            "source_url": next((str(row.get("source_url") or "") for row in reversed(day_rows) if row.get("source_url")), None) or None,
        })
    concern_hits = _channel_concern_hits([str(row.get("text") or "") for row in rows])
    temperature = _channel_temperature_from_daily(daily_counts, concern_hits)
    for entry in history:
        entry["temperature"] = temperature
    return history, evidence_refs, temperature


def _channel_context_candidates(rows: list[dict]) -> list[dict]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        channel_key = _clean_text(row.get("channel_key"))
        if not channel_key:
            continue
        channel_type = _clean_text((row.get("metadata") or {}).get("channel_type"))
        if channel_type in {"im", "mpim"}:
            continue
        grouped[channel_key].append(row)

    candidates: list[dict] = []
    for channel_key, channel_rows in grouped.items():
        speakers = {str(row.get("actor_label") or "") for row in channel_rows if str(row.get("actor_label") or "").strip()}
        if len(speakers) < MIN_ACTIVE_MEMBERS:
            continue
        latest = channel_rows[-1]
        channel_name = _clean_text((latest.get("metadata") or {}).get("channel_name")) or channel_key
        channel_type = _clean_text((latest.get("metadata") or {}).get("channel_type")) or None
        allowed_principals = _clean_refs(latest.get("allowed_principals"))
        history, evidence_refs, temperature = _daily_trend_history(channel_rows)
        dominant_concerns, concern_refs = _dominant_concern_entries(channel_rows)
        tone_norms = _tone_norms_from_rows(channel_rows)
        participant_dynamics = _participant_dynamics_from_rows(channel_rows)
        source_refs = _clean_refs([
            *evidence_refs,
            *concern_refs,
            *[ref for entry in tone_norms for ref in entry.get("refs") or []],
            *[ref for entry in participant_dynamics for ref in entry.get("refs") or []],
        ])
        candidates.append({
            "channel_key": channel_key,
            "source_resource_id": None,
            "channel_name": channel_name,
            "channel_type": channel_type,
            "temperature": temperature,
            "dominant_concerns": dominant_concerns,
            "tone_norms": tone_norms,
            "participant_dynamics": participant_dynamics,
            "trend_history": history,
            "allowed_principals": allowed_principals,
            "visibility": "company" if any(principal.startswith("company:") for principal in allowed_principals) else "restricted",
            "acl_basis": "channel_temperature_context",
            "acl_confidence": "high",
            "acl_state": "stamped",
            "acl_freshness_state": "fresh",
            "last_confirmed_at": _now_iso(),
            "status": "active",
            "source_refs": source_refs,
        })
    return candidates


async def _existing_channel_context_fingerprint(tenant_id: str, channel_key: str) -> str | None:
    current = await get_channel_context(tenant_id=tenant_id, channel_key=channel_key)
    if not current or not current.get("id"):
        return None
    return _channel_context_fingerprint(current)


async def maybe_seed_channel_context(*, tenant_id: str, pipeline_run_id: str | None = None, recent_hours: int = ROOM_SCAN_WINDOW_DAYS * 24) -> list[dict]:
    rows = await _channel_event_rows(tenant_id, recent_hours=recent_hours)
    candidates = _channel_context_candidates(rows)
    if not candidates:
        return []
    proposals: list[dict] = []
    for candidate in candidates:
        proposal = {k: v for k, v in candidate.items() if k != "source_refs"}
        created = await create_channel_context_proposal(
            tenant_id=tenant_id,
            channel_key=candidate["channel_key"],
            proposal=proposal,
            source_refs=candidate["source_refs"],
            origin="seed",
            pipeline_run_id=pipeline_run_id,
            context_pack_id=None,
            model_call_id=None,
        )
        if created:
            proposals.append(created)
    return proposals


async def maybe_propose_channel_context_update(
    *,
    tenant_id: str,
    pipeline_run_id: str | None = None,
    recent_hours: int = ROOM_SCAN_WINDOW_DAYS * 24,
) -> list[dict]:
    rows = await _channel_event_rows(tenant_id, recent_hours=recent_hours)
    candidates = _channel_context_candidates(rows)
    if not candidates:
        return []
    proposals: list[dict] = []
    for candidate in candidates:
        proposal = {k: v for k, v in candidate.items() if k != "source_refs"}
        created = await create_channel_context_proposal(
            tenant_id=tenant_id,
            channel_key=candidate["channel_key"],
            proposal=proposal,
            source_refs=candidate["source_refs"],
            origin="periodic_scan",
            pipeline_run_id=pipeline_run_id,
            context_pack_id=None,
            model_call_id=None,
        )
        if created:
            proposals.append(created)
    return proposals


async def list_channel_contexts_for_principals(*, tenant_id: str, allowed_principals: list[str], status: str | None = None) -> list[dict]:
    contexts = await list_channel_contexts(tenant_id=tenant_id, status=status)
    allowed = set(allowed_principals)
    return [
        context
        for context in contexts
        if set(context.get("allowed_principals") or []) & allowed
    ]
