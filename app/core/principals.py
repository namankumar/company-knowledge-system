"""Effective Alloy principal resolution."""

from __future__ import annotations

from app.core.db import acquire


def user_principal_key(user_id: str) -> str:
    return f"user:{user_id}"


async def effective_principals_for_user(*, tenant_id: str, user_id: str | None) -> list[str]:
    """Return active non-user principals the actor may use for permission checks."""
    if not user_id:
        return []
    async with acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT DISTINCT principal_key
            FROM principal_memberships
            WHERE tenant_id = $1
              AND member_principal_key = $2
              AND membership_state = 'active'
              AND confidence IN ('medium', 'high')
              AND principal_key IS NOT NULL
              AND principal_key <> ''
            ORDER BY principal_key
            """,
            tenant_id,
            user_principal_key(user_id),
        )
    return [str(row["principal_key"]) for row in rows]
