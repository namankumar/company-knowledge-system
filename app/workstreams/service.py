"""Durable workstream identity, correction, and lineage services."""

from __future__ import annotations

import json
from typing import Any


VALID_MEMBER_TYPES = {"alert", "work_signal", "feed_item", "brief_item", "brain_object"}
VALID_WORKSTREAM_KINDS = {
    "project",
    "commitment",
    "okr",
    "milestone",
    "risk",
    "blocker",
    "decision",
    "customer",
    "responsibility",
    "workstream",
    "personal_signal",
}

_MEMBER_TABLES = {
    "alert": "alerts",
    "work_signal": "work_signals",
    "feed_item": "feed_items",
    "brief_item": "brief_items",
    "brain_object": "brain_objects",
}


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


def _normalize_kind(kind: str | None) -> str:
    cleaned = str(kind or "workstream").strip().lower()
    return cleaned if cleaned in VALID_WORKSTREAM_KINDS else "workstream"


def _clean_reason(reason: str | None) -> str | None:
    text = " ".join(str(reason or "").split()).strip()
    return text or None


async def get_workstream(conn: Any, *, tenant_id: str, workstream_id: str) -> dict[str, Any] | None:
    row = await conn.fetchrow(
        """
        /* workstream:get_by_id */
        SELECT id, tenant_id, workstream_key, title, kind, status, merged_into_workstream_id,
               created_by_user_id, metadata, merged_at, created_at, updated_at
        FROM workstreams
        WHERE tenant_id = $1 AND id = $2
        """,
        tenant_id,
        workstream_id,
    )
    return dict(row) if row else None


async def ensure_workstream(
    conn: Any,
    *,
    tenant_id: str,
    workstream_key: str,
    title: str,
    kind: str,
    created_by_user_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    row = await conn.fetchrow(
        """
        /* workstream:upsert */
        INSERT INTO workstreams (
            tenant_id, workstream_key, title, kind, created_by_user_id, metadata
        )
        VALUES ($1, $2, $3, $4, $5, $6::jsonb)
        ON CONFLICT (tenant_id, workstream_key)
        DO UPDATE SET
            title = EXCLUDED.title,
            kind = EXCLUDED.kind,
            updated_at = now()
        RETURNING id, tenant_id, workstream_key, title, kind, status, merged_into_workstream_id,
                  created_by_user_id, metadata, merged_at, created_at, updated_at
        """,
        tenant_id,
        workstream_key,
        title,
        _normalize_kind(kind),
        created_by_user_id,
        json.dumps(metadata or {}),
    )
    record = dict(row)
    await _record_lineage_event(
        conn,
        tenant_id=tenant_id,
        event_type="created",
        workstream_id=str(record["id"]),
        actor_user_id=created_by_user_id,
        reason=None,
        metadata={"workstream_key": record["workstream_key"]},
        if_missing=True,
    )
    return record


async def resolve_active_workstream_id(conn: Any, *, tenant_id: str, workstream_id: str) -> str:
    current_id = workstream_id
    seen: set[str] = set()
    while current_id and current_id not in seen:
        seen.add(current_id)
        row = await get_workstream(conn, tenant_id=tenant_id, workstream_id=current_id)
        if not row:
            return current_id
        merged_into = row.get("merged_into_workstream_id")
        if row.get("status") != "merged" or not merged_into:
            return str(row["id"])
        current_id = str(merged_into)
    return workstream_id


async def find_active_membership(
    conn: Any,
    *,
    tenant_id: str,
    member_type: str,
    member_id: str,
) -> dict[str, Any] | None:
    if member_type not in VALID_MEMBER_TYPES:
        raise ValueError(f"Unsupported member_type: {member_type}")
    row = await conn.fetchrow(
        """
        /* workstream:find_active_membership */
        SELECT id, tenant_id, workstream_id, member_type, member_id, role, resolution_method,
               confidence, active, metadata, created_at, ended_at
        FROM workstream_memberships
        WHERE tenant_id = $1
          AND member_type = $2
          AND member_id = $3
          AND active = true
        ORDER BY created_at DESC
        LIMIT 1
        """,
        tenant_id,
        member_type,
        member_id,
    )
    return dict(row) if row else None


async def attach_member(
    conn: Any,
    *,
    tenant_id: str,
    workstream_id: str,
    member_type: str,
    member_id: str,
    actor_user_id: str | None = None,
    role: str = "primary",
    resolution_method: str = "manual_attach",
    confidence: float = 1.0,
    metadata: dict[str, Any] | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    if member_type not in VALID_MEMBER_TYPES:
        raise ValueError(f"Unsupported member_type: {member_type}")
    canonical_workstream_id = await resolve_active_workstream_id(
        conn,
        tenant_id=tenant_id,
        workstream_id=workstream_id,
    )
    current = await find_active_membership(
        conn,
        tenant_id=tenant_id,
        member_type=member_type,
        member_id=member_id,
    )
    if current and str(current["workstream_id"]) == canonical_workstream_id:
        await _rebind_member_row(
            conn,
            tenant_id=tenant_id,
            member_type=member_type,
            member_id=member_id,
            workstream_id=canonical_workstream_id,
        )
        return current

    if current:
        await _deactivate_membership(conn, membership_id=str(current["id"]))
        await _record_lineage_event(
            conn,
            tenant_id=tenant_id,
            event_type="member_detached",
            workstream_id=str(current["workstream_id"]),
            related_workstream_id=canonical_workstream_id,
            member_type=member_type,
            member_id=member_id,
            actor_user_id=actor_user_id,
            reason=reason or "moved to another workstream",
            metadata={"from_membership_id": str(current["id"])},
        )

    row = await conn.fetchrow(
        """
        /* workstream:insert_membership */
        INSERT INTO workstream_memberships (
            tenant_id, workstream_id, member_type, member_id, role,
            resolution_method, confidence, metadata
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb)
        RETURNING id, tenant_id, workstream_id, member_type, member_id, role, resolution_method,
                  confidence, active, metadata, created_at, ended_at
        """,
        tenant_id,
        canonical_workstream_id,
        member_type,
        member_id,
        role,
        resolution_method,
        confidence,
        json.dumps(metadata or {}),
    )
    await _rebind_member_row(
        conn,
        tenant_id=tenant_id,
        member_type=member_type,
        member_id=member_id,
        workstream_id=canonical_workstream_id,
    )
    await _record_lineage_event(
        conn,
        tenant_id=tenant_id,
        event_type="member_attached",
        workstream_id=canonical_workstream_id,
        related_workstream_id=str(current["workstream_id"]) if current else None,
        member_type=member_type,
        member_id=member_id,
        actor_user_id=actor_user_id,
        reason=reason,
        metadata={"resolution_method": resolution_method},
    )
    return dict(row)


async def merge_workstreams(
    conn: Any,
    *,
    tenant_id: str,
    source_workstream_id: str,
    target_workstream_id: str,
    actor_user_id: str | None = None,
    reason: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    canonical_source_id = await resolve_active_workstream_id(
        conn,
        tenant_id=tenant_id,
        workstream_id=source_workstream_id,
    )
    canonical_target_id = await resolve_active_workstream_id(
        conn,
        tenant_id=tenant_id,
        workstream_id=target_workstream_id,
    )
    if canonical_source_id == canonical_target_id:
        workstream = await get_workstream(conn, tenant_id=tenant_id, workstream_id=canonical_target_id)
        return {
            "source_workstream_id": canonical_source_id,
            "target_workstream_id": canonical_target_id,
            "canonical_workstream_id": canonical_target_id,
            "moved_memberships": [],
            "target_membership_count": len(await _active_memberships(conn, tenant_id=tenant_id, workstream_id=canonical_target_id)),
            "source_status": workstream.get("status") if workstream else "active",
        }

    async with conn.transaction():
        source = await get_workstream(conn, tenant_id=tenant_id, workstream_id=canonical_source_id)
        target = await get_workstream(conn, tenant_id=tenant_id, workstream_id=canonical_target_id)
        if not source or not target:
            raise ValueError("Both source and target workstreams must exist")

        memberships = await _active_memberships(
            conn,
            tenant_id=tenant_id,
            workstream_id=canonical_source_id,
        )
        moved_memberships: list[dict[str, Any]] = []
        for membership in memberships:
            member_type = str(membership["member_type"])
            member_id = str(membership["member_id"])
            await _deactivate_membership(conn, membership_id=str(membership["id"]))
            moved = await attach_member(
                conn,
                tenant_id=tenant_id,
                workstream_id=canonical_target_id,
                member_type=member_type,
                member_id=member_id,
                actor_user_id=actor_user_id,
                role=str(membership.get("role") or "primary"),
                resolution_method="merge",
                confidence=float(membership.get("confidence") or 1.0),
                metadata={**_json_dict(membership.get("metadata")), "merged_from_workstream_id": canonical_source_id},
                reason=reason or f"Merged into {target.get('title') or canonical_target_id}",
            )
            moved_memberships.append(
                {
                    "member_type": member_type,
                    "member_id": member_id,
                    "new_membership_id": str(moved["id"]),
                }
            )

        await conn.execute(
            """
            /* workstream:mark_merged */
            UPDATE workstreams
            SET status = 'merged',
                merged_into_workstream_id = $3,
                merged_at = now(),
                updated_at = now()
            WHERE tenant_id = $1 AND id = $2
            """,
            tenant_id,
            canonical_source_id,
            canonical_target_id,
        )
        await _record_lineage_event(
            conn,
            tenant_id=tenant_id,
            event_type="merged_into",
            workstream_id=canonical_source_id,
            related_workstream_id=canonical_target_id,
            actor_user_id=actor_user_id,
            reason=reason,
            metadata={
                "moved_memberships": moved_memberships,
                **(metadata or {}),
            },
        )
        target_membership_count = len(
            await _active_memberships(conn, tenant_id=tenant_id, workstream_id=canonical_target_id)
        )

    return {
        "source_workstream_id": canonical_source_id,
        "target_workstream_id": canonical_target_id,
        "canonical_workstream_id": canonical_target_id,
        "moved_memberships": moved_memberships,
        "target_membership_count": target_membership_count,
        "source_status": "merged",
    }


async def split_workstream(
    conn: Any,
    *,
    tenant_id: str,
    source_workstream_id: str,
    new_workstream_key: str,
    new_title: str,
    new_kind: str,
    members: list[dict[str, str]],
    actor_user_id: str | None = None,
    reason: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    canonical_source_id = await resolve_active_workstream_id(
        conn,
        tenant_id=tenant_id,
        workstream_id=source_workstream_id,
    )
    async with conn.transaction():
        source = await get_workstream(conn, tenant_id=tenant_id, workstream_id=canonical_source_id)
        if not source:
            raise ValueError("Source workstream must exist")
        created = await ensure_workstream(
            conn,
            tenant_id=tenant_id,
            workstream_key=new_workstream_key,
            title=new_title,
            kind=new_kind,
            created_by_user_id=actor_user_id,
            metadata=metadata or {},
        )
        moved_memberships: list[dict[str, Any]] = []
        for member in members:
            member_type = str(member.get("member_type") or "")
            member_id = str(member.get("member_id") or "")
            if member_type not in VALID_MEMBER_TYPES or not member_id:
                raise ValueError("Split members must include supported member_type and member_id")
            current = await find_active_membership(
                conn,
                tenant_id=tenant_id,
                member_type=member_type,
                member_id=member_id,
            )
            if not current or str(current["workstream_id"]) != canonical_source_id:
                raise ValueError(f"Member {member_type}:{member_id} is not active on the source workstream")
            await _deactivate_membership(conn, membership_id=str(current["id"]))
            await _record_lineage_event(
                conn,
                tenant_id=tenant_id,
                event_type="member_detached",
                workstream_id=canonical_source_id,
                related_workstream_id=str(created["id"]),
                member_type=member_type,
                member_id=member_id,
                actor_user_id=actor_user_id,
                reason=reason or f"Split into {created['title']}",
                metadata={"from_membership_id": str(current["id"])},
            )
            moved = await attach_member(
                conn,
                tenant_id=tenant_id,
                workstream_id=str(created["id"]),
                member_type=member_type,
                member_id=member_id,
                actor_user_id=actor_user_id,
                role=str(current.get("role") or "primary"),
                resolution_method="split",
                confidence=float(current.get("confidence") or 1.0),
                metadata={**_json_dict(current.get("metadata")), "split_from_workstream_id": canonical_source_id},
                reason=reason or f"Split from {source['title']}",
            )
            moved_memberships.append(
                {
                    "member_type": member_type,
                    "member_id": member_id,
                    "new_membership_id": str(moved["id"]),
                }
            )
        await _record_lineage_event(
            conn,
            tenant_id=tenant_id,
            event_type="split_from",
            workstream_id=str(created["id"]),
            related_workstream_id=canonical_source_id,
            actor_user_id=actor_user_id,
            reason=reason,
            metadata={
                "moved_memberships": moved_memberships,
                **(metadata or {}),
            },
        )

    return {
        "source_workstream_id": canonical_source_id,
        "new_workstream_id": str(created["id"]),
        "new_workstream_key": created["workstream_key"],
        "moved_memberships": moved_memberships,
    }


async def list_workstream_lineage(
    conn: Any,
    *,
    tenant_id: str,
    workstream_id: str,
) -> dict[str, Any]:
    canonical_id = await resolve_active_workstream_id(
        conn,
        tenant_id=tenant_id,
        workstream_id=workstream_id,
    )
    workstream = await get_workstream(conn, tenant_id=tenant_id, workstream_id=workstream_id)
    canonical = await get_workstream(conn, tenant_id=tenant_id, workstream_id=canonical_id)
    memberships = [
        dict(row)
        for row in await conn.fetch(
            """
            /* workstream:list_memberships */
            SELECT id, tenant_id, workstream_id, member_type, member_id, role, resolution_method,
                   confidence, active, metadata, created_at, ended_at
            FROM workstream_memberships
            WHERE tenant_id = $1
              AND (workstream_id = $2 OR workstream_id = $3)
            ORDER BY active DESC, created_at DESC
            """,
            tenant_id,
            workstream_id,
            canonical_id,
        )
    ]
    events = [
        dict(row)
        for row in await conn.fetch(
            """
            /* workstream:list_lineage */
            SELECT id, tenant_id, event_type, workstream_id, related_workstream_id,
                   member_type, member_id, actor_user_id, reason, metadata, created_at
            FROM workstream_lineage_events
            WHERE tenant_id = $1
              AND (
                workstream_id = $2 OR related_workstream_id = $2
                OR workstream_id = $3 OR related_workstream_id = $3
              )
            ORDER BY created_at ASC, id ASC
            """,
            tenant_id,
            workstream_id,
            canonical_id,
        )
    ]
    return {
        "workstream": workstream,
        "canonical_workstream": canonical,
        "memberships": memberships,
        "events": events,
    }


async def _active_memberships(conn: Any, *, tenant_id: str, workstream_id: str) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        """
        /* workstream:list_active_memberships */
        SELECT id, tenant_id, workstream_id, member_type, member_id, role, resolution_method,
               confidence, active, metadata, created_at, ended_at
        FROM workstream_memberships
        WHERE tenant_id = $1
          AND workstream_id = $2
          AND active = true
        ORDER BY created_at ASC, id ASC
        """,
        tenant_id,
        workstream_id,
    )
    return [dict(row) for row in rows]


async def _deactivate_membership(conn: Any, *, membership_id: str) -> None:
    await conn.execute(
        """
        /* workstream:deactivate_membership */
        UPDATE workstream_memberships
        SET active = false,
            ended_at = now()
        WHERE id = $1
        """,
        membership_id,
    )


async def _record_lineage_event(
    conn: Any,
    *,
    tenant_id: str,
    event_type: str,
    workstream_id: str,
    related_workstream_id: str | None = None,
    member_type: str | None = None,
    member_id: str | None = None,
    actor_user_id: str | None = None,
    reason: str | None = None,
    metadata: dict[str, Any] | None = None,
    if_missing: bool = False,
) -> None:
    if if_missing:
        existing = await conn.fetchrow(
            """
            /* workstream:event_exists */
            SELECT id
            FROM workstream_lineage_events
            WHERE tenant_id = $1
              AND event_type = $2
              AND workstream_id = $3
            ORDER BY created_at ASC
            LIMIT 1
            """,
            tenant_id,
            event_type,
            workstream_id,
        )
        if existing:
            return
    await conn.execute(
        """
        /* workstream:insert_event */
        INSERT INTO workstream_lineage_events (
            tenant_id, event_type, workstream_id, related_workstream_id,
            member_type, member_id, actor_user_id, reason, metadata
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb)
        """,
        tenant_id,
        event_type,
        workstream_id,
        related_workstream_id,
        member_type,
        member_id,
        actor_user_id,
        _clean_reason(reason),
        json.dumps(metadata or {}),
    )


async def _rebind_member_row(
    conn: Any,
    *,
    tenant_id: str,
    member_type: str,
    member_id: str,
    workstream_id: str,
) -> None:
    table = _MEMBER_TABLES.get(member_type)
    if not table:
        raise ValueError(f"Unsupported member_type: {member_type}")
    await conn.execute(
        f"""
        /* workstream:rebind_member_row:{member_type} */
        UPDATE {table}
        SET workstream_id = $3
        WHERE tenant_id = $1 AND id = $2
        """,
        tenant_id,
        member_id,
        workstream_id,
    )
