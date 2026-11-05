"""Durable permission decisions and incidents."""

from __future__ import annotations

import json

from app.core.db import acquire

POLICY_VERSION = "permissioned-company-brain-v1"


async def record_permission_check(
    *,
    tenant_id: str,
    target_audience: str,
    action: str,
    object_kind: str,
    decision: str,
    actor_user_id: str | None = None,
    object_id: str | None = None,
    source_resource_id: str | None = None,
    reason: str | None = None,
    decision_trace: dict | None = None,
    policy_version: str = POLICY_VERSION,
) -> str:
    async with acquire() as conn:
        return str(await conn.fetchval(
            """
            INSERT INTO permission_checks
                (tenant_id, actor_user_id, target_audience, action, object_kind,
                 object_id, source_resource_id, decision, reason, policy_version,
                 decision_trace)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb)
            RETURNING id
            """,
            tenant_id,
            actor_user_id,
            target_audience,
            action,
            object_kind,
            object_id,
            source_resource_id,
            decision,
            reason,
            policy_version,
            json.dumps(decision_trace or {}),
        ))


async def report_permission_incident(
    *,
    tenant_id: str,
    incident_type: str,
    object_kind: str,
    expected_decision: str,
    actual_decision: str,
    reported_by: str | None = None,
    object_id: str | None = None,
    source_resource_id: str | None = None,
    target_audience: str | None = None,
    adapter_version: str | None = None,
    decision_trace: dict | None = None,
    safe_debug_context: dict | None = None,
    policy_version: str = POLICY_VERSION,
) -> str:
    async with acquire() as conn:
        return str(await conn.fetchval(
            """
            INSERT INTO permission_incidents
                (tenant_id, reported_by, incident_type, object_kind, object_id,
                 source_resource_id, target_audience, expected_decision,
                 actual_decision, policy_version, adapter_version, decision_trace,
                 safe_debug_context)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12::jsonb, $13::jsonb)
            RETURNING id
            """,
            tenant_id,
            reported_by,
            incident_type,
            object_kind,
            object_id,
            source_resource_id,
            target_audience,
            expected_decision,
            actual_decision,
            policy_version,
            adapter_version,
            json.dumps(decision_trace or {}),
            json.dumps(safe_debug_context or {}),
        ))
