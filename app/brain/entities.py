"""
Entity resolution: deterministic-first.

Join keys across sources: Slack user IDs, email addresses, calendar attendees.
Aliases bridge free-text names. The LLM never invents identities — it only
proposes candidate mentions that resolve (or don't) against this registry.
"""
import json
import uuid

import structlog

from app.core.db import acquire

logger = structlog.get_logger()


async def resolve_or_create(tenant_id: str, candidate: dict) -> str | None:
    """
    candidate: {type, name?, handle_type?, handle?}
    Returns entity_registry.id, creating a row when the candidate has a strong
    identifier (email or slack handle) or a name with no existing match.
    Weak candidates (no name, no handle) return None.
    """
    entity_type = candidate.get("type", "person")
    name = (candidate.get("name") or "").strip()
    handle_type = candidate.get("handle_type")
    handle = (candidate.get("handle") or "").strip()

    async with acquire() as conn:
        # 1. Email match
        if handle_type == "email" and handle:
            row = await conn.fetchrow(
                "SELECT id FROM entity_registry WHERE tenant_id = $1 AND $2 = ANY(emails)",
                tenant_id, handle.lower(),
            )
            if row:
                return str(row["id"])

        # 2. Source handle match (slack, github, ...)
        if handle_type and handle_type != "email" and handle:
            row = await conn.fetchrow(
                "SELECT id FROM entity_registry WHERE tenant_id = $1 AND handles->>$2 = $3",
                tenant_id, handle_type, handle,
            )
            if row:
                return str(row["id"])

        # 3. Name / alias match (case-insensitive)
        if name:
            row = await conn.fetchrow(
                """
                SELECT id FROM entity_registry
                WHERE tenant_id = $1 AND entity_type = $2
                  AND (lower(canonical_name) = lower($3)
                       OR EXISTS (SELECT 1 FROM unnest(aliases) a WHERE lower(a) = lower($3)))
                """,
                tenant_id, entity_type, name,
            )
            if row:
                entity_id = str(row["id"])
                await _merge_identifiers(conn, entity_id, handle_type, handle)
                return entity_id

        # 4. Create when we have something durable to anchor on
        if not name and not handle:
            return None
        canonical = name or f"{handle_type}:{handle}"
        entity_id = str(uuid.uuid4())
        emails = [handle.lower()] if handle_type == "email" and handle else []
        handles = {handle_type: handle} if handle_type and handle_type != "email" and handle else {}
        row = await conn.fetchrow(
            """
            INSERT INTO entity_registry (id, tenant_id, entity_type, canonical_name, emails, handles)
            VALUES ($1, $2, $3, $4, $5, $6::jsonb)
            ON CONFLICT (tenant_id, entity_type, canonical_name)
            DO UPDATE SET updated_at = now()
            RETURNING id
            """,
            entity_id, tenant_id, entity_type, canonical, emails, json.dumps(handles),
        )
        return str(row["id"])


async def _merge_identifiers(conn, entity_id: str, handle_type: str | None, handle: str) -> None:
    """A name match that arrived with a new identifier enriches the entity."""
    if not handle_type or not handle:
        return
    if handle_type == "email":
        await conn.execute(
            """
            UPDATE entity_registry
            SET emails = array_append(emails, $2), updated_at = now()
            WHERE id = $1 AND NOT ($2 = ANY(emails))
            """,
            entity_id, handle.lower(),
        )
    else:
        await conn.execute(
            """
            UPDATE entity_registry
            SET handles = handles || jsonb_build_object($2::text, $3::text), updated_at = now()
            WHERE id = $1 AND (handles->>$2) IS DISTINCT FROM $3
            """,
            entity_id, handle_type, handle,
        )


async def add_alias(tenant_id: str, entity_id: str, alias: str) -> None:
    """Operator correction path: attach a free-text alias to an entity."""
    async with acquire() as conn:
        await conn.execute(
            """
            UPDATE entity_registry
            SET aliases = array_append(aliases, $3), updated_at = now()
            WHERE tenant_id = $1 AND id = $2 AND NOT ($3 = ANY(aliases))
            """,
            tenant_id, entity_id, alias,
        )
