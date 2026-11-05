"""
Slack DM delivery via the tenant's Composio Slack connection.

Approved alerts and briefs go to the founder's DM. Inbound DMs to the bot are
chat questions — handled in the ingest path by checking channel_type == 'im'.
"""
import asyncio
import json

import structlog

from app.core.composio_client import execute_composio_tool, get_composio
from app.core.db import acquire
from app.core import trace
from app.core.tenant_settings import (
    client_publishing_paused,
    get_tenant_settings,
    outbound_delivery_paused,
)
from app.core.destination_permissions import (
    filter_claim_text_for_destination,
    filter_visible_sources,
    verify_destination_visibility,
)
from app.presentation.alerts import project_alert, resolve_source_observed_at

logger = structlog.get_logger()


async def _delivery_issue(
    tenant_id: str,
    kind: str,
    detail: dict,
    *,
    pipeline_run_id: str | None = None,
) -> None:
    """Slack delivery failures downgrade to app-only silently; make them
    operator-visible as system_issues so a misconfigured tenant screams.

    Best-effort by design: sets its own tenant context (callers may run in a
    worker with none) and never raises — a failed issue INSERT must not block
    the app-delivery fallback or the status update that follows it."""
    from app.core.db import _current_tenant_id
    token = _current_tenant_id.set(tenant_id)
    try:
        async with acquire() as conn:
            await conn.execute(
                """
                INSERT INTO system_issues (tenant_id, kind, detail, pipeline_run_id)
                VALUES ($1, $2, $3::jsonb, $4)
                """,
                tenant_id, kind, json.dumps(detail), pipeline_run_id,
            )
    except Exception as e:
        logger.error("delivery_issue_write_failed", tenant_id=tenant_id, kind=kind, error=str(e))
    finally:
        _current_tenant_id.reset(token)


def _jsonb(value) -> dict:
    if value is None:
        return {}
    return json.loads(value) if isinstance(value, str) else dict(value)


def _source_composio_entity_id(source) -> str | None:
    policy = _jsonb(source["sync_policy"])
    entity_id = policy.get("composio_entity_id")
    if entity_id:
        return entity_id
    user_id = policy.get("connected_user_id")
    tenant_id = source["tenant_id"]
    if tenant_id and user_id:
        return f"{tenant_id}:{user_id}"
    return None


async def _slack_connection(tenant_id: str) -> dict | None:
    async with acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT tenant_id, composio_connection_id, sync_policy FROM sources
            WHERE tenant_id = $1 AND source_type = 'slack' AND status IN ('connected', 'backfilling', 'live', 'degraded')
            ORDER BY updated_at DESC LIMIT 1
            """,
            tenant_id,
        )
        return dict(row) if row else None


async def send_dm(*, tenant_id: str, slack_user_id: str, text: str) -> bool:
    settings = await get_tenant_settings(tenant_id)
    if outbound_delivery_paused(settings):
        logger.info("slack_dm_send_paused", tenant_id=tenant_id)
        return False

    source = await _slack_connection(tenant_id)
    if not source:
        logger.warning("slack_dm_no_connection", tenant_id=tenant_id)
        return False

    composio = get_composio()
    try:
        response = await asyncio.to_thread(
            execute_composio_tool,
            composio,
            "SLACK_SEND_MESSAGE",
            arguments={"channel": slack_user_id, "text": text[:39000], "markdown_text": text[:39000]},
            connected_account_id=source["composio_connection_id"],
            user_id=_source_composio_entity_id(source),
        )
        ok = not (isinstance(response, dict) and response.get("successful") is False)
        if not ok:
            logger.error("slack_dm_failed", tenant_id=tenant_id, error=str(response.get("error"))[:300])
        return ok
    except Exception as e:
        logger.error("slack_dm_error", tenant_id=tenant_id, error=str(e))
        return False


async def founder_slack_id(tenant_id: str) -> str | None:
    async with acquire() as conn:
        return await conn.fetchval(
            """
            SELECT slack_user_id FROM users
            WHERE tenant_id = $1 AND role = 'founder' AND slack_user_id IS NOT NULL
            ORDER BY created_at LIMIT 1
            """,
            tenant_id,
        )


def render_alert(alert: dict) -> str:
    severity_icon = {"high": ":red_circle:", "medium": ":large_orange_circle:", "low": ":large_yellow_circle:"}
    icon = severity_icon.get(alert.get("severity") or "", ":large_blue_circle:")
    lines = [f"{icon} *{alert['claim']}*"]
    if alert.get("why_now"):
        lines.append(f"_Why now:_ {alert['why_now']}")
    evidence = alert.get("evidence") or []
    if isinstance(evidence, str):
        evidence = json.loads(evidence)
    evidence = filter_visible_sources(evidence, _json_list(alert.get("allowed_principals")))
    for ev in evidence[:3]:
        quote = (ev.get("quote") or "").strip()
        url = ev.get("source_url")
        if quote:
            lines.append(f"> {quote[:200]}" + (f" (<{url}|source>)" if url else ""))
    if alert.get("recommended_action"):
        lines.append(f"_Suggested:_ {alert['recommended_action']}")
    return "\n".join(lines)


async def deliver_alert(*, tenant_id: str, alert_id: str) -> bool:
    async with acquire() as conn:
        alert = await conn.fetchrow(
            """
            SELECT * FROM alerts
            WHERE id = $1 AND tenant_id = $2
              AND acl_state = 'stamped'
              AND acl_freshness_state = 'fresh'
              AND allowed_principals ?| $3::text[]
            """,
            alert_id, tenant_id, _company_principals(tenant_id),
        )
    if not alert:
        return False
    alert = dict(alert)
    destination_principals = _company_principals(tenant_id)
    run_id = str(alert["pipeline_run_id"]) if alert["pipeline_run_id"] else None
    visibility_check = _alert_destination_visibility(alert, destination_principals)
    if not visibility_check["ok"]:
        await _mark_alert_delivery_blocked(tenant_id, alert_id)
        await _delivery_issue(
            tenant_id,
            "destination_visibility_blocked",
            _destination_visibility_issue_detail(alert_id, visibility_check),
            pipeline_run_id=run_id,
        )
        if run_id:
            await trace.finish_run(run_id, status="delivery_blocked")
        return False

    settings = await get_tenant_settings(tenant_id)
    outbound_paused = outbound_delivery_paused(settings)
    client_paused = client_publishing_paused(settings)
    slack_id = await founder_slack_id(tenant_id)
    delivered = False
    if outbound_paused:
        logger.info("slack_alert_delivery_paused", tenant_id=tenant_id, alert_id=alert_id)
    elif slack_id:
        delivered = await send_dm(tenant_id=tenant_id, slack_user_id=slack_id, text=render_alert(alert))
        if not delivered:
            await _delivery_issue(
                tenant_id,
                "delivery_failed",
                {"alert_id": alert_id, "reason": "slack send failed"},
                pipeline_run_id=run_id,
            )
    else:
        await _delivery_issue(
            tenant_id,
            "delivery_failed",
            {"alert_id": alert_id, "reason": "no founder slack_user_id set"},
            pipeline_run_id=run_id,
        )
    if run_id:
        async with trace.step(run_id, tenant_id, "slack_delivery", input_ref=alert_id) as st:
            st.status = "completed" if delivered else "skipped"
            st.detail = {
                "delivered": delivered,
                "paused": outbound_paused,
                "fallback": "app_only" if not delivered and not client_paused else None,
            }

    delivered_via = ["slack_dm"] if delivered else []
    if not client_paused:
        delivered_via.append("app_only" if not delivered else "app")
    async with acquire() as conn:
        await conn.execute(
            """
            UPDATE alerts
            SET status = CASE WHEN $3::boolean THEN 'delivery_paused' ELSE 'delivered' END,
                delivered_via = delivered_via || $2::jsonb
            WHERE id = $1
            """,
            alert_id, json.dumps(delivered_via), client_paused,
        )

    if not client_paused:
        async with acquire() as conn:
            alert_with_freshness = alert.copy()
            alert_with_freshness["source_observed_at"] = await resolve_source_observed_at(
                conn, tenant_id, alert_with_freshness.get("evidence"), _company_principals(tenant_id),
            )
        projected = project_alert(alert_with_freshness)
        receivers = await _publish_to_app(tenant_id, {
            "type": "alert",
            "alert_id": alert_id,
            "claim": alert["claim"],
            "severity": alert["severity"],
            "confidence": alert["confidence"],
            "drift_type": alert["drift_type"],
            "why_now": alert["why_now"],
            "recommended_action": alert["recommended_action"],
            "workstream": projected["workstream"],
            "source_chips": projected["source_chips"],
            "source_observed_at": projected["source_observed_at"],
            "missing_evidence": projected["missing_evidence"],
        })
    else:
        receivers = 0
    if run_id:
        async with trace.step(run_id, tenant_id, "app_delivery", input_ref=alert_id) as st:
            st.status = "skipped" if client_paused else "completed"
            st.detail = {
                "channel": f"deliveries:{tenant_id}",
                "paused": client_paused,
                "redis_receivers": receivers,
                "pull_api_visible": not client_paused,
            }
        await trace.finish_run(run_id, status="paused" if client_paused else "delivered")
    return delivered


async def resume_alert_app_delivery(*, tenant_id: str, alert_id: str) -> bool:
    settings = await get_tenant_settings(tenant_id)
    if client_publishing_paused(settings):
        return False
    async with acquire() as conn:
        alert = await conn.fetchrow(
            """
            SELECT * FROM alerts
            WHERE id = $1 AND tenant_id = $2 AND status = 'delivery_paused'
              AND acl_state = 'stamped'
              AND acl_freshness_state = 'fresh'
              AND allowed_principals ?| $3::text[]
            """,
            alert_id, tenant_id, _company_principals(tenant_id),
        )
        if alert:
            alert = dict(alert)
            alert["source_observed_at"] = await resolve_source_observed_at(
                conn, tenant_id, alert.get("evidence"), _company_principals(tenant_id)
            )
    if not alert:
        return False
    destination_principals = _company_principals(tenant_id)
    visibility_check = _alert_destination_visibility(alert, destination_principals)
    if not visibility_check["ok"]:
        await _mark_alert_delivery_blocked(tenant_id, alert_id)
        await _delivery_issue(
            tenant_id,
            "destination_visibility_blocked",
            _destination_visibility_issue_detail(alert_id, visibility_check, resume=True),
            pipeline_run_id=str(alert["pipeline_run_id"]) if alert.get("pipeline_run_id") else None,
        )
        return False
    projected = project_alert(alert)
    await _publish_to_app(tenant_id, {
        "type": "alert",
        "alert_id": alert_id,
        "claim": alert["claim"],
        "severity": alert["severity"],
        "confidence": alert["confidence"],
        "drift_type": alert["drift_type"],
        "why_now": alert["why_now"],
        "recommended_action": alert["recommended_action"],
        "workstream": projected["workstream"],
        "source_chips": projected["source_chips"],
        "source_observed_at": projected["source_observed_at"],
        "missing_evidence": projected["missing_evidence"],
    })
    async with acquire() as conn:
        await conn.execute(
            """
            UPDATE alerts
            SET status = 'delivered',
                delivered_via = delivered_via || '["app_resume"]'::jsonb
            WHERE id = $1 AND tenant_id = $2 AND status = 'delivery_paused'
            """,
            alert_id, tenant_id,
        )
    return True


async def _publish_to_app(tenant_id: str, message: dict) -> int:
    settings = await get_tenant_settings(tenant_id)
    if client_publishing_paused(settings):
        logger.info(
            "app_publish_paused",
            tenant_id=tenant_id,
            message_type=message.get("type"),
        )
        return 0

    from app.core.redis import get_redis
    redis = await get_redis()
    return int(await redis.publish(f"deliveries:{tenant_id}", json.dumps(message, default=str)))


async def deliver_brief(*, tenant_id: str, brief_id: str) -> bool:
    async with acquire() as conn:
        brief = await conn.fetchrow(
            """
            SELECT b.*, COALESCE(cp.items, '[]'::jsonb) AS context_items
            FROM briefs b
            LEFT JOIN context_packs cp ON cp.id = b.context_pack_id
            WHERE b.id = $1 AND b.tenant_id = $2
              AND b.acl_state = 'stamped'
              AND b.acl_freshness_state = 'fresh'
              AND b.allowed_principals ?| $3::text[]
            """,
            brief_id, tenant_id, _company_principals(tenant_id),
        )
    if not brief:
        return False

    settings = await get_tenant_settings(tenant_id)
    outbound_paused = outbound_delivery_paused(settings)
    client_paused = client_publishing_paused(settings)
    run_id = str(brief["pipeline_run_id"]) if brief["pipeline_run_id"] else None
    from app.synthesis.chat import strip_citations
    safe_body = _safe_brief_body(dict(brief), _company_principals(tenant_id))
    text = f":newspaper: *Daily brief — {brief['brief_date']:%b %d}*\n\n{strip_citations(safe_body)}"

    slack_id = await founder_slack_id(tenant_id)
    delivered = False
    if outbound_paused:
        logger.info("slack_brief_delivery_paused", tenant_id=tenant_id, brief_id=brief_id)
    elif slack_id:
        delivered = await send_dm(tenant_id=tenant_id, slack_user_id=slack_id, text=text)
        if not delivered:
            await _delivery_issue(
                tenant_id,
                "delivery_failed",
                {"brief_id": brief_id, "reason": "slack send failed"},
                pipeline_run_id=run_id,
            )
    else:
        await _delivery_issue(
            tenant_id,
            "delivery_failed",
            {"brief_id": brief_id, "reason": "no founder slack_user_id set"},
            pipeline_run_id=run_id,
        )
    if run_id:
        async with trace.step(run_id, tenant_id, "slack_delivery", input_ref=brief_id) as st:
            st.status = "completed" if delivered else "skipped"
            st.detail = {
                "delivered": delivered,
                "paused": outbound_paused,
                "fallback": "app_only" if not delivered and not client_paused else None,
            }

    delivered_via = ["slack_dm"] if delivered else []
    if not client_paused:
        delivered_via.append("app_only" if not delivered else "app")
    async with acquire() as conn:
        await conn.execute(
            """
            UPDATE briefs
            SET status = CASE WHEN $3::boolean THEN 'delivery_paused' ELSE 'delivered' END,
                delivered_via = delivered_via || $2::jsonb
            WHERE id = $1
            """,
            brief_id, json.dumps(delivered_via), client_paused,
        )
    receivers = 0
    if not client_paused:
        receivers = await _publish_to_app(tenant_id, {
            "type": "brief",
            "brief_id": brief_id,
            "body": safe_body,
            "brief_date": str(brief["brief_date"]),
        })
    if run_id:
        async with trace.step(run_id, tenant_id, "app_delivery", input_ref=brief_id) as st:
            st.status = "skipped" if client_paused else "completed"
            st.detail = {
                "channel": f"deliveries:{tenant_id}",
                "paused": client_paused,
                "redis_receivers": receivers,
                "pull_api_visible": not client_paused,
            }
        await trace.finish_run(run_id, status="paused" if client_paused else "delivered")
    return delivered


async def resume_brief_app_delivery(*, tenant_id: str, brief_id: str) -> bool:
    settings = await get_tenant_settings(tenant_id)
    if client_publishing_paused(settings):
        return False
    async with acquire() as conn:
        brief = await conn.fetchrow(
            """
            SELECT b.*, COALESCE(cp.items, '[]'::jsonb) AS context_items
            FROM briefs b
            LEFT JOIN context_packs cp ON cp.id = b.context_pack_id
            WHERE b.id = $1 AND b.tenant_id = $2 AND b.status = 'delivery_paused'
              AND b.acl_state = 'stamped'
              AND b.acl_freshness_state = 'fresh'
              AND b.allowed_principals ?| $3::text[]
            """,
            brief_id, tenant_id,
            _company_principals(tenant_id),
        )
    if not brief:
        return False
    await _publish_to_app(tenant_id, {
        "type": "brief",
        "brief_id": brief_id,
        "body": _safe_brief_body(dict(brief), _company_principals(tenant_id)),
        "brief_date": str(brief["brief_date"]),
    })
    async with acquire() as conn:
        await conn.execute(
            """
            UPDATE briefs
            SET status = 'delivered',
                delivered_via = delivered_via || '["app_resume"]'::jsonb
            WHERE id = $1 AND tenant_id = $2 AND status = 'delivery_paused'
            """,
            brief_id, tenant_id,
        )
    return True


def _company_principals(tenant_id: str) -> list[str]:
    return [f"company:{tenant_id}"]


def _safe_brief_body(brief: dict, destination_principals: list[str]) -> str:
    content = str(brief.get("content_md") or "")
    gate = filter_claim_text_for_destination(
        content,
        _sources_from_context_items(_json_array(brief.get("context_items"))),
        destination_principals,
    )
    if gate["blocked_claims"] and "[ref:" not in gate["text"]:
        return "## Coverage\n- Missing: destination-visible evidence for this brief."
    return gate["text"] or "## Coverage\n- Missing: destination-visible evidence for this brief."


def _alert_destination_visibility(alert: dict, destination_principals: list[str]) -> dict:
    evidence = _json_array(alert.get("evidence"))
    if not evidence:
        return {"ok": False, "blocked_refs": ["missing_evidence"]}
    return verify_destination_visibility(sources=evidence, destination_principals=destination_principals)


async def _mark_alert_delivery_blocked(tenant_id: str, alert_id: str) -> None:
    async with acquire() as conn:
        await conn.execute(
            """
            UPDATE alerts
            SET status = 'delivery_blocked'
            WHERE id = $1 AND tenant_id = $2
              AND status IN ('approved', 'publishing', 'delivery_paused')
            """,
            alert_id, tenant_id,
        )


def _destination_visibility_issue_detail(alert_id: str, visibility_check: dict, **extra) -> dict:
    return {
        "alert_id": alert_id,
        "blocked_refs": _safe_blocked_refs(visibility_check.get("blocked_refs") or []),
        "blocked_ref_count": len(visibility_check.get("blocked_refs") or []),
        **extra,
    }


def _safe_blocked_refs(blocked_refs: list) -> list[str]:
    safe_refs: list[str] = []
    for ref in blocked_refs:
        value = str(ref)
        if "://" in value or "/" in value:
            safe_refs.append("(redacted-source-ref)")
            continue
        safe_refs.append(value[:160])
    return safe_refs[:20]


def _sources_from_context_items(items: list) -> list[dict]:
    sources: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        kind = item.get("kind")
        item_id = item.get("id")
        if not kind or not item_id:
            continue
        source = {
            "ref": f"{kind}:{item_id}",
            "source_url": item.get("source_url"),
            "source_title": item.get("source_title") or item.get("title"),
            "source_type": item.get("source_type"),
            "source_id": item.get("source_id"),
            "source_external_id": item.get("source_external_id"),
            "source_identity_hints": item.get("source_identity_hints") or [],
            "source_observed_at": item.get("source_observed_at"),
        }
        if any(key in item for key in ("visibility", "allowed_principals", "acl_state", "acl_freshness_state")):
            source.update({
                "visibility": item.get("visibility"),
                "allowed_principals": item.get("allowed_principals") or [],
                "acl_state": item.get("acl_state"),
                "acl_freshness_state": item.get("acl_freshness_state"),
            })
        sources.append(source)
    return sources


def _json_array(value) -> list:
    if value is None:
        return []
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return list(value) if isinstance(value, (list, tuple, set)) else []


def _json_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return [str(item) for item in parsed] if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value]
    return []
