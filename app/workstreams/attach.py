"""Attachment ladder from signals/alerts to durable workstreams."""

from __future__ import annotations

from dataclasses import replace
import json
import re
from typing import Any

from app.presentation.alerts import workstream_from_alert
from app.workstreams.service import (
    attach_member,
    ensure_workstream,
    get_workstream,
    resolve_active_workstream_id,
)


def _json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")


def _normalize_kind(kind: str | None) -> str:
    raw = str(kind or "workstream").strip().lower()
    if raw in {"initiative", "open_question", "signal"}:
        return "workstream"
    if raw == "deadline":
        return "milestone"
    if raw == "person":
        return "responsibility"
    return raw or "workstream"


def _normalize_anchor(value: Any) -> str | None:
    raw = " ".join(str(value or "").split()).strip().lower()
    if not raw:
        return None
    raw = raw.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    return raw or None


async def resolve_signal_attachment(conn: Any, candidate: Any) -> Any:
    metadata = _json_dict(getattr(candidate, "metadata", None))
    unknown_state = str(metadata.get("unknown_state") or getattr(candidate, "signal_subtype", "") or "").strip()
    tenant_id = getattr(candidate, "tenant_id", None)
    if not tenant_id:
        return candidate

    resolution_state = "attached"
    resolution_reason = "attached via fallback anchor"
    workstream_id: str | None = None
    workstream_key: str | None = None

    if unknown_state == "mapping_uncertain" or len(getattr(candidate, "candidate_object_ids", []) or []) > 1:
        resolution_state = "ambiguous"
        resolution_reason = "signal matched more than one plausible workstream or explicit mapping uncertainty"
    else:
        object_match = await _brain_object_anchor(
            conn,
            tenant_id=tenant_id,
            object_ids=(getattr(candidate, "related_object_ids", None) or []) + (getattr(candidate, "candidate_object_ids", None) or []),
        )
        if object_match:
            workstream_id = object_match["id"]
            workstream_key = object_match["workstream_key"]
            resolution_reason = "attached via related durable object"
        else:
            cluster_key = _normalize_anchor(metadata.get("cluster_key"))
            if cluster_key:
                row = await ensure_workstream(
                    conn,
                    tenant_id=tenant_id,
                    workstream_key=f"cluster:{cluster_key}",
                    title=str(getattr(candidate, "title", None) or "Emerging workstream").strip(),
                    kind="workstream",
                    metadata={"attachment_source": "cluster_key", "cluster_key": cluster_key},
                )
                workstream_id = str(row["id"])
                workstream_key = row["workstream_key"]
                resolution_reason = "attached via replay-stable cluster key"
            else:
                anchor = _signal_source_anchor(candidate)
                if anchor:
                    row = await ensure_workstream(
                        conn,
                        tenant_id=tenant_id,
                        workstream_key=anchor["workstream_key"],
                        title=anchor["title"],
                        kind=anchor["kind"],
                        metadata=anchor["metadata"],
                    )
                    workstream_id = str(row["id"])
                    workstream_key = row["workstream_key"]
                    resolution_reason = "attached via deterministic source anchor"
                else:
                    resolution_state = "ambiguous"
                    resolution_reason = "no durable object, cluster key, or deterministic source anchor was available"

    next_metadata = {
        **metadata,
        "workstream_resolution_state": resolution_state,
        "workstream_resolution_reason": resolution_reason,
    }
    if workstream_key:
        next_metadata["workstream_key"] = workstream_key
    return replace(candidate, workstream_id=workstream_id, metadata=next_metadata)


async def sync_persisted_member_attachment(
    conn: Any,
    *,
    tenant_id: str,
    workstream_id: str | None,
    member_type: str,
    member_id: str | None,
    resolution_method: str,
    reason: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    if not workstream_id or not member_id:
        return
    await attach_member(
        conn,
        tenant_id=tenant_id,
        workstream_id=workstream_id,
        member_type=member_type,
        member_id=member_id,
        resolution_method=resolution_method,
        metadata=metadata or {},
        reason=reason,
    )


async def resolve_alert_attachment(conn: Any, *, tenant_id: str, alert_row: dict[str, Any]) -> str | None:
    workstream = workstream_from_alert(alert_row)
    key = str(workstream.get("key") or "").strip().lower()
    title = str(workstream.get("title") or "").strip() or "Operating signal"
    kind = _normalize_kind(workstream.get("kind"))
    existing = await _existing_alert_workstream(
        conn,
        tenant_id=tenant_id,
        workstream_key=key,
        title=title,
        kind=kind,
    )
    if existing:
        return str(existing["id"])
    if not key:
        key = f"signal:{_slug(str(alert_row.get('drift_type') or 'operating-signal'))}"
    row = await ensure_workstream(
        conn,
        tenant_id=tenant_id,
        workstream_key=key,
        title=title,
        kind=kind,
        metadata={
            "attachment_source": "alert_projection",
            "projects": workstream.get("projects") or [],
            "commitments": workstream.get("commitments") or [],
            "customers": workstream.get("customers") or [],
            "people": workstream.get("people") or [],
            "drift_type": alert_row.get("drift_type"),
        },
    )
    return str(row["id"])


async def _existing_alert_workstream(
    conn: Any,
    *,
    tenant_id: str,
    workstream_key: str,
    title: str,
    kind: str,
) -> dict[str, Any] | None:
    if workstream_key:
        by_key = await conn.fetchrow(
            """
            SELECT id, tenant_id, workstream_key, title, kind, status, merged_into_workstream_id,
                   created_by_user_id, metadata, merged_at, created_at, updated_at
            FROM workstreams
            WHERE tenant_id = $1
              AND workstream_key = $2
              AND status = 'active'
            LIMIT 1
            """,
            tenant_id,
            workstream_key,
        )
        if by_key:
            return dict(by_key)
    title_matches = await conn.fetch(
        """
        SELECT id, tenant_id, workstream_key, title, kind, status, merged_into_workstream_id,
               created_by_user_id, metadata, merged_at, created_at, updated_at
        FROM workstreams
        WHERE tenant_id = $1
          AND lower(title) = lower($2)
          AND kind = $3
          AND status = 'active'
        ORDER BY updated_at DESC, created_at DESC
        LIMIT 2
        """,
        tenant_id,
        title,
        kind,
    )
    if len(title_matches) == 1:
        return dict(title_matches[0])
    return None


async def _brain_object_anchor(conn: Any, *, tenant_id: str, object_ids: list[str]) -> dict[str, Any] | None:
    for object_id in object_ids:
        membership = await conn.fetchrow(
            """
            SELECT workstream_id
            FROM workstream_memberships
            WHERE tenant_id = $1
              AND member_type = 'brain_object'
              AND member_id = $2
              AND active = true
            ORDER BY created_at DESC
            LIMIT 1
            """,
            tenant_id,
            object_id,
        )
        if membership and membership.get("workstream_id"):
            canonical_id = await resolve_active_workstream_id(
                conn,
                tenant_id=tenant_id,
                workstream_id=str(membership["workstream_id"]),
            )
            existing = await get_workstream(conn, tenant_id=tenant_id, workstream_id=canonical_id)
            if existing:
                return existing

        row = await conn.fetchrow(
            """
            SELECT id, object_type, title
            FROM brain_objects
            WHERE tenant_id = $1 AND id = $2
            LIMIT 1
            """,
            tenant_id,
            object_id,
        )
        if not row:
            continue
        object_type = _normalize_kind(row["object_type"])
        title = str(row["title"] or object_id).strip() or object_id
        workstream = await ensure_workstream(
            conn,
            tenant_id=tenant_id,
            workstream_key=f"object:{object_id}",
            title=title,
            kind=object_type,
            metadata={
                "attachment_source": "brain_object",
                "brain_object_id": str(row["id"]),
                "brain_object_type": row["object_type"],
            },
        )
        await sync_persisted_member_attachment(
            conn,
            tenant_id=tenant_id,
            workstream_id=str(workstream["id"]),
            member_type="brain_object",
            member_id=str(row["id"]),
            resolution_method="object_anchor",
            reason="created durable workstream from brain object anchor",
            metadata={
                "brain_object_type": row["object_type"],
                "attachment_source": "brain_object",
            },
        )
        return dict(workstream)
    return None


def _signal_source_anchor(candidate: Any) -> dict[str, Any] | None:
    metadata = _json_dict(getattr(candidate, "metadata", None))
    event_metadata = _json_dict(metadata.get("event_metadata"))
    source_type = " ".join(str(metadata.get("source_type") or "").split()).strip().lower()
    title = str(getattr(candidate, "title", None) or "Untitled project").strip() or "Untitled project"
    signal_type = str(getattr(candidate, "signal_type", None) or "workstream").strip().lower()
    kind = "project"

    thread_key = _normalize_anchor(metadata.get("thread_key"))
    if source_type == "slack" and thread_key:
        return {
            "workstream_key": f"slack-thread:{thread_key}",
            "title": title,
            "kind": kind,
            "metadata": {"attachment_source": "slack_thread", "thread_key": thread_key, "source_type": source_type},
        }
    channel_key = _normalize_anchor(metadata.get("channel_key"))
    if source_type == "slack" and channel_key:
        return {
            "workstream_key": f"slack-channel:{channel_key}",
            "title": title,
            "kind": kind,
            "metadata": {"attachment_source": "slack_channel", "channel_key": channel_key, "source_type": source_type},
        }

    for key in ("issue_id", "issue_key", "pull_request_id", "pull_number", "project_id", "repository", "repo", "project", "team"):
        value = _normalize_anchor(event_metadata.get(key))
        if not value:
            continue
        return {
            "workstream_key": f"{source_type}:{key}:{value}",
            "title": title,
            "kind": kind,
            "metadata": {
                "attachment_source": "source_metadata",
                "source_type": source_type,
                "anchor_field": key,
                "anchor_value": value,
            },
        }

    source_url = _normalize_anchor(metadata.get("source_url"))
    if source_url:
        return {
            "workstream_key": f"{source_type}:url:{source_url}",
            "title": title,
            "kind": kind,
            "metadata": {"attachment_source": "source_url", "source_type": source_type, "source_url": source_url},
        }
    return None
