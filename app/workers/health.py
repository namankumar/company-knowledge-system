"""
Scheduled source health: poll poll-primary sources, gap-check webhook sources,
mark stale connections degraded. Runs every 15 minutes via ARQ cron.
"""
import json
import asyncio
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import structlog

from app.brain.loop_lifecycle import record_loop_events, stale_transition_fields
from app.core.composio_client import SUPPORTED_APPS, get_composio
from app.core.db import acquire, _current_tenant_id, _rls_bypass
from app.core.source_imports import import_enabled
from app.core.tenant_settings import synthesis_paused
from app.synthesis.feedback_tuning import feedback_tuning_cycle
from app.ingestion.backfill import start_backfill

logger = structlog.get_logger()


async def source_health_cycle(ctx: dict) -> None:
    bypass_token = _rls_bypass.set(True)
    try:
        async with acquire() as conn:
            sources = await conn.fetch(
                "SELECT * FROM sources WHERE status IN ('live', 'degraded', 'connected', 'connected_pending_import', 'import_paused')"
            )
    finally:
        _rls_bypass.reset(bypass_token)

    now = datetime.now(timezone.utc)
    for source in sources:
        tenant_id = str(source["tenant_id"])
        _current_tenant_id.set(tenant_id)
        policy = source["sync_policy"]
        policy = json.loads(policy) if isinstance(policy, str) else dict(policy or {})
        if not import_enabled(policy, status=source["status"]):
            logger.info("source_health_skipped_import_disabled", source_id=str(source["id"]), status=source["status"])
            continue

        try:
            if policy.get("trigger_errors") or _policy_needs_repair(source["source_type"], policy):
                policy = await _maybe_repair_triggers(source, policy)
            if policy.get("mode") == "poll":
                await _maybe_poll(source, policy, now)
            else:
                await _maybe_gap_check(source, policy, now)
            await _update_freshness(source, policy, now)
        except Exception as e:
            logger.error("health_cycle_source_failed", source_id=str(source["id"]), error=str(e))


async def _maybe_repair_triggers(source, policy: dict) -> dict:
    app_config = SUPPORTED_APPS.get(source["source_type"])
    if not app_config:
        return policy
    if not app_config.get("triggers"):
        next_policy = {
            **policy,
            **dict(app_config.get("sync_policy") or {}),
            "triggers": [],
            "trigger_errors": [],
        }
        async with acquire() as conn:
            await conn.execute(
                """
                UPDATE sources
                SET sync_policy = $2::jsonb,
                    updated_at = now()
                WHERE id = $1
                """,
                source["id"],
                json.dumps(next_policy),
            )
        logger.info("trigger_repair_cleared_for_poll_source", source_id=str(source["id"]), source_type=source["source_type"])
        return next_policy
    account_id = source.get("composio_connection_id") if hasattr(source, "get") else source["composio_connection_id"]
    if not account_id:
        return policy

    composio = get_composio()
    enabled = []
    errors = []
    for trigger in app_config["triggers"]:
        trigger_slug = trigger if isinstance(trigger, str) else trigger["slug"]
        trigger_config = {} if isinstance(trigger, str) else dict(trigger.get("config") or {})
        if source["source_type"] == "googledocs" and policy.get("document_query"):
            trigger_config["query"] = policy["document_query"]
        try:
            created = await asyncio.to_thread(
                composio.triggers.create,
                slug=trigger_slug,
                connected_account_id=account_id,
                trigger_config=trigger_config,
            )
            enabled.append({
                "slug": trigger_slug,
                "id": str(getattr(created, "trigger_id", None) or getattr(created, "id", "")),
            })
        except Exception as e:
            errors.append({"slug": trigger_slug, "error": str(e)[:500]})

    next_policy = {**policy, "triggers": enabled, "trigger_errors": errors}
    async with acquire() as conn:
        await conn.execute(
            """
            UPDATE sources
            SET sync_policy = $2::jsonb,
                status = CASE WHEN $3::boolean THEN 'degraded' ELSE 'live' END,
                degraded_reason = CASE WHEN $3::boolean THEN 'one or more Composio triggers failed' ELSE NULL END,
                updated_at = now()
            WHERE id = $1
            """,
            source["id"],
            json.dumps(next_policy),
            bool(errors),
        )
    if errors:
        logger.warning("trigger_repair_failed", source_id=str(source["id"]), source_type=source["source_type"], errors=errors)
    else:
        logger.info("trigger_repair_succeeded", source_id=str(source["id"]), source_type=source["source_type"], triggers=enabled)
    return next_policy


def _policy_needs_repair(source_type: str, policy: dict) -> bool:
    """Detect existing sources whose sync policy predates the current source contract."""
    app_config = SUPPORTED_APPS.get(source_type)
    if not app_config:
        return False
    desired = dict(app_config.get("sync_policy") or {})
    for key, value in desired.items():
        if policy.get(key) != value:
            return True

    desired_triggers = app_config.get("triggers") or []
    existing_triggers = policy.get("triggers") or []
    if not desired_triggers and existing_triggers:
        return True
    return False


async def _maybe_poll(source, policy: dict, now: datetime) -> None:
    interval = timedelta(minutes=policy.get("poll_interval_minutes", 15))
    last = source["last_synced_at"]
    if last and now - last < interval:
        return
    # skip if an incremental run is already going
    async with acquire() as conn:
        running = await conn.fetchval(
            "SELECT 1 FROM source_sync_runs WHERE source_id = $1 AND status = 'running'",
            source["id"],
        )
    if running:
        return
    await start_backfill(tenant_id=str(source["tenant_id"]), source_id=str(source["id"]), kind="incremental")
    logger.info("incremental_sync_started", source_type=source["source_type"], source_id=str(source["id"]))


async def _maybe_gap_check(source, policy: dict, now: datetime) -> None:
    """Webhook-primary source with no events for gap_check_hours -> pull to fill the hole."""
    gap_hours = policy.get("gap_check_hours", 6)
    freshest = max(filter(None, [source["last_webhook_at"], source["last_synced_at"]]), default=None)
    if freshest and now - freshest < timedelta(hours=gap_hours):
        return
    async with acquire() as conn:
        running = await conn.fetchval(
            "SELECT 1 FROM source_sync_runs WHERE source_id = $1 AND status = 'running'",
            source["id"],
        )
    if running:
        return
    await start_backfill(tenant_id=str(source["tenant_id"]), source_id=str(source["id"]), kind="incremental")
    logger.info("gap_check_sync_started", source_type=source["source_type"], gap_hours=gap_hours)


async def _update_freshness(source, policy: dict, now: datetime) -> None:
    freshness_hours = policy.get("freshness_hours", 24)
    freshest = max(filter(None, [source["last_webhook_at"], source["last_synced_at"]]), default=None)
    is_stale = freshest is None or now - freshest > timedelta(hours=freshness_hours)
    trigger_errors = policy.get("trigger_errors") or []

    async with acquire() as conn:
        if is_stale and source["status"] == "live":
            await conn.execute(
                """
                UPDATE sources
                SET status = 'degraded', stale_since = COALESCE(stale_since, now()),
                    degraded_reason = $2, last_probe_at = now(), updated_at = now()
                WHERE id = $1
                """,
                source["id"], f"no data for over {freshness_hours}h",
            )
            await conn.execute(
                "INSERT INTO system_issues (tenant_id, kind, detail) VALUES ($1, 'stale_source', $2::jsonb)",
                source["tenant_id"],
                json.dumps({"source_id": str(source["id"]), "source_type": source["source_type"]}),
            )
            logger.warning("source_degraded", source_type=source["source_type"], source_id=str(source["id"]))
        elif not is_stale and source["status"] == "degraded" and not trigger_errors:
            await conn.execute(
                """
                UPDATE sources
                SET status = 'live', stale_since = NULL, degraded_reason = NULL,
                    last_probe_at = now(), updated_at = now()
                WHERE id = $1
                """,
                source["id"],
            )
            logger.info("source_recovered", source_type=source["source_type"])
        elif trigger_errors:
            await conn.execute(
                """
                UPDATE sources
                SET status = 'degraded',
                    degraded_reason = COALESCE(degraded_reason, $2),
                    last_probe_at = now(),
                    updated_at = now()
                WHERE id = $1
                """,
                source["id"],
                "trigger setup failed",
            )
        else:
            await conn.execute("UPDATE sources SET last_probe_at = now() WHERE id = $1", source["id"])


async def daily_brief_cycle(ctx: dict) -> None:
    """Cron: generate a brief for every tenant with live sources."""
    from app.synthesis.brief import run_daily_brief

    bypass_token = _rls_bypass.set(True)
    try:
        async with acquire() as conn:
            tenants = await conn.fetch(
                """
                SELECT DISTINCT t.id, t.settings
                FROM tenants t
                JOIN sources s ON s.tenant_id = t.id
                WHERE s.status IN ('live', 'degraded')
                """
            )
            due_tenant_ids = [
                str(r["id"]) for r in tenants
                if _is_brief_due_now(r["settings"], datetime.now(timezone.utc))
            ]
    finally:
        _rls_bypass.reset(bypass_token)
    for tenant_id in due_tenant_ids:
        try:
            await run_daily_brief(ctx, tenant_id=tenant_id)
        except Exception as e:
            logger.error("daily_brief_failed", tenant_id=tenant_id, error=str(e))


async def weekly_brief_cycle(ctx: dict) -> None:
    """Cron: generate the Monday weekly brief for every tenant with live sources."""
    from app.synthesis.brief_weekly import run_weekly_brief

    bypass_token = _rls_bypass.set(True)
    try:
        async with acquire() as conn:
            tenants = await conn.fetch(
                """
                SELECT DISTINCT t.id, t.settings
                FROM tenants t
                JOIN sources s ON s.tenant_id = t.id
                WHERE s.status IN ('live', 'degraded')
                """
            )
            due_tenant_ids = [
                str(r["id"]) for r in tenants
                if _is_weekly_brief_due_now(r["settings"], datetime.now(timezone.utc))
            ]
    finally:
        _rls_bypass.reset(bypass_token)
    for tenant_id in due_tenant_ids:
        try:
            await run_weekly_brief(ctx, tenant_id=tenant_id)
        except Exception as e:
            logger.error("weekly_brief_failed", tenant_id=tenant_id, error=str(e))


async def feedback_tuning_cron(ctx: dict) -> None:
    try:
        await feedback_tuning_cycle(ctx)
    except Exception as e:
        logger.error("feedback_tuning_cycle_failed", error=str(e))


async def loop_stale_cycle(ctx: dict) -> None:
    now = datetime.now(timezone.utc)
    bypass_token = _rls_bypass.set(True)
    try:
        async with acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT bo.id, bo.tenant_id, bo.loop_status, bo.loop_opened_at,
                       bo.loop_last_activity_at, bo.loop_resolved_at, bo.loop_resolved_ref,
                       bo.loop_stale_after, bo.loop_reopened_count, t.settings
                FROM brain_objects bo
                JOIN tenants t ON t.id = bo.tenant_id
                WHERE bo.status = 'active'
                  AND bo.object_type IN ('commitment', 'deadline')
                  AND bo.loop_status IN ('open', 'reopened')
                  AND bo.loop_stale_after IS NOT NULL
                  AND bo.loop_stale_after <= $1
                """,
                now,
            )
    finally:
        _rls_bypass.reset(bypass_token)

    for row in rows:
        tenant_settings = json.loads(row["settings"]) if isinstance(row["settings"], str) else dict(row["settings"] or {})
        if synthesis_paused(tenant_settings):
            continue
        fields, event = stale_transition_fields(row, occurred_at=now)
        if not event:
            continue
        tenant_token = _current_tenant_id.set(str(row["tenant_id"]))
        try:
            async with acquire() as conn:
                updated = await conn.fetchval(
                    """
                    UPDATE brain_objects
                    SET loop_status = $3,
                        loop_opened_at = $4,
                        loop_last_activity_at = $5,
                        loop_resolved_at = $6,
                        loop_resolved_ref = $7::jsonb,
                        loop_stale_after = $8,
                        loop_reopened_count = $9,
                        updated_at = now()
                    WHERE id = $1
                      AND tenant_id = $2
                      AND loop_status IN ('open', 'reopened')
                      AND loop_stale_after IS NOT DISTINCT FROM $10
                      AND loop_last_activity_at IS NOT DISTINCT FROM $11
                    RETURNING id
                    """,
                    row["id"],
                    row["tenant_id"],
                    fields["loop_status"],
                    fields["loop_opened_at"],
                    fields["loop_last_activity_at"],
                    fields["loop_resolved_at"],
                    json.dumps(fields["loop_resolved_ref"]) if fields["loop_resolved_ref"] else None,
                    fields["loop_stale_after"],
                    fields["loop_reopened_count"],
                    row["loop_stale_after"],
                    row["loop_last_activity_at"],
                )
                if updated:
                    await record_loop_events(
                        conn,
                        tenant_id=str(row["tenant_id"]),
                        object_id=str(row["id"]),
                        events=[event],
                    )
        finally:
            _current_tenant_id.reset(tenant_token)


async def channel_context_scan_cycle(ctx: dict) -> None:
    from app.personalization.channel_context import maybe_propose_channel_context_update

    bypass_token = _rls_bypass.set(True)
    try:
        async with acquire() as conn:
            tenants = await conn.fetch(
                """
                SELECT DISTINCT t.id, t.settings
                FROM tenants t
                JOIN sources s ON s.tenant_id = t.id
                WHERE s.status IN ('live', 'degraded')
                """
            )
    finally:
        _rls_bypass.reset(bypass_token)

    for row in tenants:
        tenant_id = str(row["id"])
        tenant_settings = json.loads(row["settings"]) if isinstance(row["settings"], str) else dict(row["settings"] or {})
        if synthesis_paused(tenant_settings):
            continue
        tenant_token = _current_tenant_id.set(tenant_id)
        try:
            proposals = await maybe_propose_channel_context_update(
                tenant_id=tenant_id,
                recent_hours=7 * 24,
            )
            if proposals:
                logger.info("channel_context_scan_proposed", tenant_id=tenant_id, proposed=len(proposals))
        except Exception as e:
            logger.error("channel_context_scan_failed", tenant_id=tenant_id, error=str(e))
        finally:
            _current_tenant_id.reset(tenant_token)


async def workstream_digest_cycle(ctx: dict) -> None:
    """Scheduled-sweep trigger for workstream digests (Slice 3/4,
    packpolicy-digests packet): without this cron, the only other call
    site for maybe_refresh_workstream_digest is the contradiction-
    invalidation hook in brain/merge.py, so a brand-new active workstream
    that never triggers a contradiction fact would otherwise never get a
    first digest at all. Mirrors channel_context_scan_cycle's per-tenant
    gating (live/degraded sources, synthesis-paused skip)."""
    from app.personalization.workstream_digest import sweep_workstream_digests

    bypass_token = _rls_bypass.set(True)
    try:
        async with acquire() as conn:
            tenants = await conn.fetch(
                """
                SELECT DISTINCT t.id, t.settings
                FROM tenants t
                JOIN sources s ON s.tenant_id = t.id
                WHERE s.status IN ('live', 'degraded')
                """
            )
    finally:
        _rls_bypass.reset(bypass_token)

    for row in tenants:
        tenant_id = str(row["id"])
        tenant_settings = json.loads(row["settings"]) if isinstance(row["settings"], str) else dict(row["settings"] or {})
        if synthesis_paused(tenant_settings):
            continue
        tenant_token = _current_tenant_id.set(tenant_id)
        try:
            digests = await sweep_workstream_digests(tenant_id=tenant_id)
            if digests:
                logger.info("workstream_digest_swept", tenant_id=tenant_id, digest_count=len(digests))
        except Exception as e:
            logger.error("workstream_digest_sweep_failed", tenant_id=tenant_id, error=str(e))
        finally:
            _current_tenant_id.reset(tenant_token)


def _is_brief_due_now(settings_raw, now_utc: datetime) -> bool:
    settings = json.loads(settings_raw) if isinstance(settings_raw, str) else dict(settings_raw or {})
    tz_name = settings.get("brief_timezone") or settings.get("timezone") or "America/Los_Angeles"
    try:
        local_now = now_utc.astimezone(ZoneInfo(tz_name))
    except ZoneInfoNotFoundError:
        local_now = now_utc.astimezone(ZoneInfo("UTC"))
    target_hour = int(settings.get("brief_hour_local") or 4)
    return local_now.hour == target_hour


def _is_weekly_brief_due_now(settings_raw, now_utc: datetime) -> bool:
    settings = json.loads(settings_raw) if isinstance(settings_raw, str) else dict(settings_raw or {})
    tz_name = settings.get("brief_timezone") or settings.get("timezone") or "America/Los_Angeles"
    try:
        local_now = now_utc.astimezone(ZoneInfo(tz_name))
    except ZoneInfoNotFoundError:
        local_now = now_utc.astimezone(ZoneInfo("UTC"))
    target_hour = int(settings.get("brief_hour_local") or 4)
    return local_now.weekday() == 0 and local_now.hour == target_hour


async def okr_pace_cycle(ctx: dict) -> None:
    """Cron: run the deterministic OKR pace check for every tenant whose
    brief is due this hour (same gate as daily_brief_cycle, runs right after
    it so a behind-pace alert lands alongside that day's brief)."""
    from app.synthesis.okr_pace import run_okr_pace_check

    bypass_token = _rls_bypass.set(True)
    try:
        async with acquire() as conn:
            tenants = await conn.fetch(
                """
                SELECT DISTINCT t.id, t.settings
                FROM tenants t
                JOIN sources s ON s.tenant_id = t.id
                WHERE s.status IN ('live', 'degraded')
                """
            )
            due_tenant_ids = [
                str(r["id"]) for r in tenants
                if _is_brief_due_now(r["settings"], datetime.now(timezone.utc))
            ]
    finally:
        _rls_bypass.reset(bypass_token)
    for tenant_id in due_tenant_ids:
        try:
            await run_okr_pace_check(tenant_id)
        except Exception as e:
            logger.error("okr_pace_check_failed", tenant_id=tenant_id, error=str(e))


async def embedding_cycle(ctx: dict) -> None:
    """Cron: embed recent chunks for every active tenant (no-op without OPENAI_API_KEY)."""
    from app.retrieval.embed import backfill_embeddings, embeddings_enabled

    if not embeddings_enabled():
        return
    bypass_token = _rls_bypass.set(True)
    try:
        async with acquire() as conn:
            tenant_ids = [
                str(r["tenant_id"]) for r in await conn.fetch(
                    """
                    SELECT DISTINCT tenant_id FROM content_chunks
                    WHERE embedding IS NULL
                      AND acl_state = 'stamped'
                      AND acl_freshness_state = 'fresh'
                      AND jsonb_array_length(allowed_principals) > 0
                    """
                )
            ]
    finally:
        _rls_bypass.reset(bypass_token)
    for tenant_id in tenant_ids:
        await backfill_embeddings(ctx, tenant_id=tenant_id)
