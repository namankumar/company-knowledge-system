"""
Checkpointed backfill and incremental sync.

One page per worker job: fetch page -> enqueue idempotent ingest jobs ->
advance checkpoint -> re-enqueue next page. A failed page resumes from the
last completed checkpoint. Backfilled events never trigger synthesis.

Composio tool slugs live in FETCHERS; they're the most likely thing to need
adjustment when testing against the live API.
"""
import asyncio
import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone

import structlog

from app.core.composio_client import execute_composio_tool, get_composio
from app.core.config import get_settings
from app.core.db import acquire
from app.core.github_app import create_installation_token, fetch_pull_requests_for_repo
from app.core.source_imports import (
    PENDING_IMPORT_STATUS,
    import_disabled_reason,
    import_enabled,
    with_import_state,
)
from app.core.source_permissions import (
    PermissionDenied,
    assert_tool_allowed,
    json_object,
    log_permission_denied,
    sanitize_github_pull_request,
)
from app.core.tenant_settings import github_access_mode

logger = structlog.get_logger()

PAGE_SIZE = 100
SOURCE_PAGE_SIZES = {
    "slack": 50,
    "gmail": 5,
    "gcal": 25,
    "googledocs": 10,
    "googledrive": 10,
    "googlesheets": 1,
    "linear": 25,
    "github": 50,
    "hubspot": 50,
}
MIN_PAGE_SIZES = {
    "slack": 5,
    "gmail": 1,
    "gcal": 5,
    "googledocs": 1,
    "googledrive": 1,
    "googlesheets": 1,
    "linear": 5,
    "github": 5,
    "hubspot": 5,
}
BACKFILL_KINDS = {"backfill", "deep_backfill"}
INITIAL_BACKFILL_DAYS = {
    "slack": 14,
    "gmail": 14,
    "googledocs": 30,
    "googledrive": 30,
    "googlesheets": 30,
    "gcal": 30,
    "linear": 60,
    "github": 30,
    "hubspot": 90,
}


async def start_backfill(*, tenant_id: str, source_id: str, kind: str = "backfill") -> str:
    """Create a sync run and enqueue the first page. Returns sync_run_id."""
    from app.workers.queue import enqueue_backfill_page

    sync_run_id = str(uuid.uuid4())
    async with acquire() as conn:
        source = await conn.fetchrow("SELECT status, sync_policy FROM sources WHERE id = $1", source_id)
        if not source:
            raise RuntimeError("source not found")
        if not import_enabled(source["sync_policy"], status=source["status"]):
            reason = import_disabled_reason(source["sync_policy"], status=source["status"]) or "source import disabled"
            raise RuntimeError(reason)
        await conn.execute(
            """
            INSERT INTO source_sync_runs (id, tenant_id, source_id, kind, stats)
            VALUES ($1, $2, $3, $4, $5::jsonb)
            """,
            sync_run_id, tenant_id, source_id, kind, json.dumps(_initial_stats(kind=kind)),
        )
        if kind in BACKFILL_KINDS:
            await conn.execute(
                "UPDATE sources SET status = 'backfilling', updated_at = now() WHERE id = $1",
                source_id,
            )
    try:
        await enqueue_backfill_page(tenant_id=tenant_id, source_id=source_id, sync_run_id=sync_run_id)
    except Exception as exc:
        reason = f"backfill queue unavailable: {exc}"
        policy = with_import_state(source["sync_policy"], enabled=False, reason=reason)
        async with acquire() as conn:
            await conn.execute(
                """
                UPDATE source_sync_runs
                SET status = 'failed',
                    error = $2,
                    finished_at = now(),
                    stats = jsonb_set(coalesce(stats, '{}'::jsonb), '{stage}', to_jsonb('queue_unavailable'::text), true)
                WHERE id = $1
                """,
                sync_run_id,
                reason[:1000],
            )
            await conn.execute(
                """
                UPDATE sources
                SET status = $2,
                    sync_policy = $3::jsonb,
                    degraded_reason = $4,
                    updated_at = now()
                WHERE id = $1
                """,
                source_id,
                PENDING_IMPORT_STATUS,
                json.dumps(policy),
                reason[:500],
            )
        raise RuntimeError(reason) from exc
    return sync_run_id


async def resume_backfill(*, tenant_id: str, source_id: str) -> str | None:
    """Re-enqueue the most recent failed/stuck run from its checkpoint."""
    from app.workers.queue import enqueue_backfill_page

    async with acquire() as conn:
        run = await conn.fetchrow(
            """
            SELECT id FROM source_sync_runs
            WHERE source_id = $1 AND status IN ('failed', 'running')
            ORDER BY started_at DESC LIMIT 1
            """,
            source_id,
        )
        if not run:
            return None
        await conn.execute(
            "UPDATE source_sync_runs SET status = 'running', error = NULL WHERE id = $1",
            run["id"],
        )
    sync_run_id = str(run["id"])
    await enqueue_backfill_page(tenant_id=tenant_id, source_id=source_id, sync_run_id=sync_run_id)
    return sync_run_id


async def process_page(ctx: dict, *, tenant_id: str, source_id: str, sync_run_id: str) -> None:
    """Worker job: one page of a backfill/incremental run."""
    from app.core.db import _current_tenant_id
    from app.workers.queue import enqueue_ingest, enqueue_backfill_page, enqueue_source_synthesis

    _current_tenant_id.set(tenant_id)

    async with acquire() as conn:
        run = await conn.fetchrow("SELECT * FROM source_sync_runs WHERE id = $1", sync_run_id)
        source = await conn.fetchrow("SELECT * FROM sources WHERE id = $1", source_id)
    if not run or not source or run["status"] != "running":
        logger.info("backfill_page_skipped", sync_run_id=sync_run_id, reason="run not active")
        return
    try:
        source_status = source["status"]
    except (KeyError, TypeError):
        source_status = None
    if not import_enabled(source["sync_policy"], status=source_status):
        reason = import_disabled_reason(source["sync_policy"], status=source_status) or "source import disabled"
        async with acquire() as conn:
            await conn.execute(
                "UPDATE source_sync_runs SET status = 'paused', error = COALESCE(error, $2) WHERE id = $1",
                sync_run_id,
                reason,
            )
        logger.info("backfill_page_skipped", sync_run_id=sync_run_id, reason=reason)
        return

    checkpoint = _jsonb(run["checkpoint"])
    stats = _jsonb(run["stats"])
    fetcher = FETCHERS.get(source["source_type"])
    if not fetcher:
        await _fail_run(sync_run_id, f"no fetcher for {source['source_type']}")
        return

    settings = get_settings()
    max_pages = max(1, int(settings.backfill_max_pages_per_run))
    current_page_count = int(stats.get("pages", 0))
    if current_page_count >= max_pages:
        error = f"backfill exceeded max pages ({max_pages})"
        await _fail_run(sync_run_id, error)
        logger.error("backfill_page_limit_exceeded_before_fetch", sync_run_id=sync_run_id, max_pages=max_pages)
        return

    try:
        events, next_checkpoint, done = await fetcher(source, checkpoint, kind=run["kind"])
    except Exception as e:
        await _fail_run(sync_run_id, f"{type(e).__name__}: {e}")
        logger.error("backfill_fetch_failed", sync_run_id=sync_run_id, error=str(e), exc_info=True)
        raise

    page_fingerprint = _page_fingerprint(events)
    if page_fingerprint and not done and stats.get("last_page_fingerprint") == page_fingerprint:
        error = "backfill fetched the same non-terminal page twice"
        await _fail_run(sync_run_id, error)
        logger.error("backfill_duplicate_page_detected", sync_run_id=sync_run_id)
        return

    spacing = max(0.0, float(get_settings().backfill_ingest_spacing_seconds))
    for idx, ev in enumerate(events):
        await enqueue_ingest(
            tenant_id=tenant_id,
            source_id=source_id,
            external_event_id=ev["external_event_id"],
            payload=ev["payload"],
            event_type=ev.get("event_type"),
            is_backfill=(run["kind"] in BACKFILL_KINDS),
            sync_run_id=sync_run_id,
            defer_seconds=(idx * spacing) or None,
        )

    stats["fetched"] = stats.get("fetched", 0) + len(events)
    stats["enqueued"] = stats.get("enqueued", 0) + len(events)
    stats["pending"] = max(0, stats.get("enqueued", 0) - stats.get("processed", 0) - stats.get("failed", 0))
    stats["pages"] = stats.get("pages", 0) + 1
    stats["stage"] = "processing" if done else "fetching"
    if page_fingerprint:
        stats["last_page_fingerprint"] = page_fingerprint

    async with acquire() as conn:
        if done:
            await conn.execute(
                """
                UPDATE source_sync_runs
                SET status = 'processing', checkpoint = $2::jsonb, stats = $3::jsonb
                WHERE id = $1
                """,
                sync_run_id, json.dumps(next_checkpoint), json.dumps(stats),
            )
            await conn.execute(
                """
                UPDATE sources
                SET status = CASE
                        WHEN COALESCE(jsonb_array_length(sync_policy->'trigger_errors'), 0) > 0
                            THEN 'degraded'
                        WHEN status = 'backfilling' THEN 'processing'
                        ELSE status
                    END,
                    last_synced_at = now(), stale_since = NULL, updated_at = now()
                WHERE id = $1
                """,
                source_id,
            )
            logger.info("backfill_completed", sync_run_id=sync_run_id, **stats)
        else:
            await conn.execute(
                """
                UPDATE source_sync_runs SET checkpoint = $2::jsonb, stats = $3::jsonb WHERE id = $1
                """,
                sync_run_id, json.dumps(next_checkpoint), json.dumps(stats),
            )

    if not done:
        await enqueue_backfill_page(tenant_id=tenant_id, source_id=source_id, sync_run_id=sync_run_id)
    elif run["kind"] in BACKFILL_KINDS:
        delay = max(5.0, min(300.0, (len(events) * spacing) + 10.0))
        await enqueue_source_synthesis(
            tenant_id=tenant_id,
            source_id=source_id,
            sync_run_id=sync_run_id,
            reason=f"{source['source_type']} {run['kind']} fetched and waiting for processing drain",
            defer_seconds=delay,
        )


async def _fail_run(sync_run_id: str, error: str) -> None:
    async with acquire() as conn:
        await conn.execute(
            "UPDATE source_sync_runs SET status = 'failed', error = $2 WHERE id = $1",
            sync_run_id, error,
        )
        await conn.execute(
            """
            UPDATE sources
            SET status = 'degraded', degraded_reason = $2, updated_at = now()
            WHERE id = (
                SELECT source_id FROM source_sync_runs WHERE id = $1
            )
            """,
            sync_run_id, error,
        )


def _page_fingerprint(events: list[dict]) -> str | None:
    ids = sorted(str(event.get("external_event_id") or "") for event in events)
    ids = [item for item in ids if item]
    if not ids:
        return None
    return hashlib.sha256("\n".join(ids).encode("utf-8")).hexdigest()


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
    tenant_id = source.get("tenant_id") if hasattr(source, "get") else source["tenant_id"]
    if tenant_id and user_id:
        return f"{tenant_id}:{user_id}"
    return None


async def _execute_tool(slug: str, source, arguments: dict) -> dict:
    """Composio tool execution against the source's connected account. The SDK
    is synchronous, so it runs in a thread to keep the worker loop free."""
    try:
        assert_tool_allowed(source, slug, arguments)
    except PermissionDenied as exc:
        await log_permission_denied(
            tenant_id=str(_source_value(source, "tenant_id") or ""),
            source_id=str(_source_value(source, "id") or ""),
            source_type=exc.source_type or _source_value(source, "source_type"),
            tool_slug=slug,
            reason=exc.reason,
        )
        raise
    composio = get_composio()
    response = await asyncio.to_thread(
        execute_composio_tool,
        composio,
        slug,
        arguments=arguments,
        connected_account_id=source["composio_connection_id"],
        user_id=_source_composio_entity_id(source),
    )
    if isinstance(response, dict):
        if response.get("successful") is False:
            raise RuntimeError(f"{slug} failed: {response.get('error')}")
        return response.get("data", response)
    return getattr(response, "data", {}) or {}


async def _execute_paged_tool(slug: str, source, arguments: dict, *, source_type: str, size_key: str) -> dict:
    """Run a paged tool with automatic downshift if Composio rejects payload size."""
    args = dict(arguments)
    current_size = int(args.get(size_key) or _page_size(source_type))
    min_size = max(1, int(MIN_PAGE_SIZES.get(source_type, 1)))

    while True:
        args[size_key] = current_size
        try:
            data = await _execute_tool(slug, source, args)
            if isinstance(data, dict):
                data.setdefault("__inferencehq_page_size", current_size)
            return data
        except Exception as exc:
            if not _is_payload_too_large(exc) or current_size <= min_size:
                raise
            next_size = max(min_size, current_size // 2)
            if next_size == current_size:
                raise
            logger.warning(
                "backfill_page_size_downshifted",
                source_type=source_type,
                tool_slug=slug,
                size_key=size_key,
                from_size=current_size,
                to_size=next_size,
                reason=str(exc)[:240],
            )
            current_size = next_size


def _is_payload_too_large(exc: Exception) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    return "413" in text or "payloadtoolarge" in text or "payload too large" in text


def _since(source, checkpoint: dict, kind: str) -> datetime:
    last_synced_at = _source_value(source, "last_synced_at")
    if kind == "incremental" and last_synced_at:
        return last_synced_at - timedelta(hours=1)  # overlap; dedup handles repeats
    source_type = _source_value(source, "source_type")
    full_days = get_settings().backfill_days
    initial_days = min(full_days, INITIAL_BACKFILL_DAYS.get(source_type, full_days))
    if kind == "backfill":
        return datetime.now(timezone.utc) - timedelta(days=initial_days)
    return datetime.now(timezone.utc) - timedelta(days=full_days)


def _backfill_window(source, checkpoint: dict, kind: str) -> tuple[datetime, datetime | None]:
    since = _since(source, checkpoint, kind)
    if kind != "deep_backfill":
        return since, None
    source_type = _source_value(source, "source_type")
    full_days = get_settings().backfill_days
    initial_days = min(full_days, INITIAL_BACKFILL_DAYS.get(source_type, full_days))
    if full_days <= initial_days:
        return since, None
    return since, datetime.now(timezone.utc) - timedelta(days=initial_days)


def _source_value(source, key: str, default=None):
    if hasattr(source, "get"):
        return source.get(key, default)
    try:
        return source[key]
    except (KeyError, TypeError):
        return default


def _page_size(source_type: str) -> int:
    return SOURCE_PAGE_SIZES.get(source_type, PAGE_SIZE)


def _initial_stats(*, kind: str) -> dict:
    return {
        "pages": 0,
        "fetched": 0,
        "enqueued": 0,
        "raw_stored": 0,
        "deduped": 0,
        "normalized": 0,
        "chunked": 0,
        "suppressed": 0,
        "extracted": 0,
        "merged": 0,
        "processed": 0,
        "failed": 0,
        "pending": 0,
        "stage": "fetching",
        "kind": kind,
    }


# ---------------------------------------------------------------------------
# Per-source fetchers: (source_row, checkpoint, kind) -> (events, next_checkpoint, done)
# ---------------------------------------------------------------------------

async def _fetch_slack(source, checkpoint: dict, *, kind: str):
    """Phase 1: list channels. Phase 2: walk each channel's history with cursors."""
    since, until = _backfill_window(source, checkpoint, kind)

    if "channels" not in checkpoint:
        channel_types = _slack_allowed_channel_types(source)
        data = await _execute_paged_tool(
            "SLACK_LIST_ALL_CHANNELS",
            source,
            {
                "limit": _page_size("slack"),
                "types": ",".join(channel_types),
                "exclude_archived": True,
            },
            source_type="slack",
            size_key="limit",
        )
        policy = json_object(_jsonb(source["sync_policy"]).get("permission_policy"))
        allowlist = json_object(policy.get("allowed_resources")).get("slack_channel_ids") or []
        channels = []
        channel_metadata = {}
        for channel in data.get("channels", []):
            channel_id = str(channel.get("id") or "")
            if not channel_id or channel.get("is_archived"):
                continue
            if channel_id.startswith("D") and "im" not in channel_types:
                continue
            if allowlist and channel_id not in allowlist:
                continue
            channels.append(channel_id)
            channel_metadata[channel_id] = _slack_channel_metadata(channel, source)
        return [], {"channels": channels, "channel_metadata": channel_metadata, "idx": 0, "cursor": None}, len(channels) == 0

    channels, idx, cursor = checkpoint["channels"], checkpoint["idx"], checkpoint.get("cursor")
    channel_metadata = checkpoint.get("channel_metadata") or {}
    if idx >= len(channels):
        return [], checkpoint, True

    channel = channels[idx]
    metadata = channel_metadata.get(channel) or {}
    args = {"channel": channel, "limit": _page_size("slack"), "oldest": str(since.timestamp())}
    if until:
        args["latest"] = str(until.timestamp())
    if cursor:
        args["cursor"] = cursor
    data = await _execute_paged_tool(
        "SLACK_FETCH_CONVERSATION_HISTORY",
        source,
        args,
        source_type="slack",
        size_key="limit",
    )

    events = []
    for msg in data.get("messages", []):
        ts = msg.get("ts")
        if not ts:
            continue
        msg.setdefault("channel", channel)
        payload = {"event": msg}
        if metadata:
            channel_payload = _compact_dict({
                "id": metadata.get("id") or channel,
                "name": metadata.get("name"),
                "is_private": metadata.get("is_private"),
            })
            if channel_payload:
                payload["channel"] = channel_payload
            if metadata.get("team"):
                payload["team"] = {"id": metadata["team"]}
        events.append({
            "external_event_id": f"{channel}:{ts}",
            "payload": payload,
            "event_type": "message",
        })

    next_cursor = (data.get("response_metadata") or {}).get("next_cursor") or None
    if next_cursor:
        return events, {**checkpoint, "cursor": next_cursor}, False
    return events, _slack_checkpoint(channels, idx + 1, None, channel_metadata), idx + 1 >= len(channels)


def _slack_channel_metadata(channel: dict, source: dict) -> dict:
    return _compact_dict({
        "id": channel.get("id"),
        "name": channel.get("name"),
        "is_private": channel.get("is_private"),
        "team": channel.get("context_team_id") or channel.get("team") or channel.get("team_id") or source.get("external_id"),
    })


def _slack_allowed_channel_types(source: dict) -> list[str]:
    policy = json_object(_jsonb(_source_value(source, "sync_policy")).get("permission_policy"))
    allowed = json_object(policy.get("allowed_resources"))
    channel_types = allowed.get("slack_channel_types")
    if not isinstance(channel_types, list) or not channel_types:
        return ["public_channel", "private_channel"]
    supported = ["public_channel", "private_channel", "mpim", "im"]
    requested = {str(value) for value in channel_types}
    return [value for value in supported if value in requested] or ["public_channel", "private_channel"]


def _slack_checkpoint(channels: list, idx: int, cursor: str | None, channel_metadata: dict) -> dict:
    checkpoint = {"channels": channels, "idx": idx, "cursor": cursor}
    if channel_metadata:
        checkpoint["channel_metadata"] = channel_metadata
    return checkpoint


def _compact_dict(value: dict) -> dict:
    return {key: item for key, item in value.items() if item not in (None, "", [], {})}


async def _fetch_gmail(source, checkpoint: dict, *, kind: str):
    since, until = _backfill_window(source, checkpoint, kind)
    days = max(1, (datetime.now(timezone.utc) - since).days)
    query = f"newer_than:{days}d -in:spam -in:trash -larger:500K"
    if until:
        older_than_days = max(1, (datetime.now(timezone.utc) - until).days)
        query += f" older_than:{older_than_days}d"
    args = {"query": query, "max_results": _page_size("gmail")}
    if checkpoint.get("page_token"):
        args["page_token"] = checkpoint["page_token"]
    data = await _execute_paged_tool(
        "GMAIL_FETCH_EMAILS",
        source,
        args,
        source_type="gmail",
        size_key="max_results",
    )

    events = []
    for msg in data.get("messages", []):
        msg_id = msg.get("message_id") or msg.get("messageId") or msg.get("id")
        if not msg_id:
            continue
        events.append({
            "external_event_id": str(msg_id),
            "payload": msg,
            "event_type": "email",
        })

    next_token = data.get("next_page_token") or data.get("nextPageToken") or None
    return events, {"page_token": next_token}, next_token is None


async def _fetch_gcal(source, checkpoint: dict, *, kind: str):
    since, historical_until = _backfill_window(source, checkpoint, kind)
    until = historical_until or (datetime.now(timezone.utc) + timedelta(days=90))  # planned meetings matter for drift
    args = {
        "calendar_id": "primary",
        "time_min": since.isoformat(),
        "time_max": until.isoformat(),
        "max_results": _page_size("gcal"),
        "single_events": True,
    }
    if checkpoint.get("page_token"):
        args["page_token"] = checkpoint["page_token"]
    data = await _execute_paged_tool(
        "GOOGLECALENDAR_EVENTS_LIST",
        source,
        args,
        source_type="gcal",
        size_key="max_results",
    )

    items = data.get("items", data.get("events", []))
    events = []
    for ev in items:
        ev_id = ev.get("id")
        if not ev_id:
            continue
        # updated timestamp in the id makes edits re-ingest as new versions
        updated = (ev.get("updated") or "").replace(":", "").replace("-", "")[:15]
        events.append({
            "external_event_id": f"{ev_id}:{updated}" if updated else str(ev_id),
            "payload": ev,
            "event_type": "calendar_event",
        })

    next_token = data.get("next_page_token") or data.get("nextPageToken") or None
    return events, {"page_token": next_token}, next_token is None


async def _fetch_googledocs(source, checkpoint: dict, *, kind: str):
    policy = _jsonb(source["sync_policy"])
    since, until = _backfill_window(source, checkpoint, kind)
    permission_policy = json_object(policy.get("permission_policy"))
    allowed_resources = json_object(permission_policy.get("allowed_resources"))
    allowed_doc_ids = allowed_resources.get("google_document_ids") or policy.get("google_document_ids") or []
    allowed_folder_ids = _string_list(allowed_resources.get("google_folder_ids") or policy.get("google_folder_ids"))
    folder_query = _drive_resource_query([], allowed_folder_ids)
    configured_query = (
        policy.get("document_query")
        or policy.get("document_name")
        or allowed_resources.get("document_query")
        or folder_query
    )
    if allowed_doc_ids and "doc_idx" not in checkpoint:
        return [], {"doc_idx": 0, "allowed_doc_ids": allowed_doc_ids}, len(allowed_doc_ids) == 0
    if "allowed_doc_ids" in checkpoint:
        doc_ids = checkpoint.get("allowed_doc_ids") or []
        idx = int(checkpoint.get("doc_idx") or 0)
        if idx >= len(doc_ids):
            return [], checkpoint, True
        document_id = doc_ids[idx]
        content = await _execute_tool(
            "GOOGLEDOCS_GET_DOCUMENT_PLAINTEXT",
            source,
            {"document_id": document_id, "include_tables": True},
        )
        events = [{
            "external_event_id": f"{document_id}:snapshot",
            "payload": {
                "event_type": "document_snapshot",
                "document": {"id": document_id},
                "document_content": content,
                "search_scope": "explicitly_allowed_document_ids",
                "permission_provenance": "tenant_allowlist",
            },
            "event_type": "document_snapshot",
        }]
        return events, {"doc_idx": idx + 1, "allowed_doc_ids": doc_ids}, idx + 1 >= len(doc_ids)

    query = configured_query
    search_scope = "configured_document_query" if configured_query else "explicitly_shared_documents"
    if not query:
        return [], {"page_token": None, "manual_share_required": True}, True

    args = {
        "query": query,
        "max_results": _page_size("googledocs"),
        "order_by": "modifiedTime desc",
        "response_detail": "minimal",
        "fields": "id,name,modifiedTime,webViewLink,lastModifyingUser",
    }
    if checkpoint.get("page_token"):
        args["page_token"] = checkpoint["page_token"]
    data = await _execute_paged_tool(
        "GOOGLEDOCS_SEARCH_DOCUMENTS",
        source,
        args,
        source_type="googledocs",
        size_key="max_results",
    )

    documents = (
        data.get("documents")
        or data.get("files")
        or data.get("items")
        or data.get("results")
        or []
    )
    events = []
    for document in documents:
        document_id = document.get("id") or document.get("document_id")
        if not document_id:
            continue
        content = await _execute_tool(
            "GOOGLEDOCS_GET_DOCUMENT_PLAINTEXT",
            source,
            {"document_id": document_id, "include_tables": True},
        )
        modified = document.get("modifiedTime") or document.get("modified_time") or ""
        events.append({
            "external_event_id": f"{document_id}:{modified or 'snapshot'}",
            "payload": {
                "event_type": "document_snapshot",
                "document": document,
                "document_content": content,
                "search_scope": search_scope,
                "permission_provenance": "configured_document_filter",
            },
            "event_type": "document_snapshot",
        })

    next_token = data.get("next_page_token") or data.get("nextPageToken") or None
    return events, {"page_token": next_token}, next_token is None


def _modified_after_query(since: datetime) -> str:
    cutoff = since.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return f"modifiedTime > '{cutoff}'"


def _modified_window_query(since: datetime, until: datetime | None) -> str:
    query = _modified_after_query(since)
    if until:
        cutoff = until.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        query += f" and modifiedTime < '{cutoff}'"
    return query


def _drive_query_literal(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace("'", "\\'")


def _string_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return [str(item).strip() for item in parsed if str(item).strip()] if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _drive_resource_query(file_ids: list[str], folder_ids: list[str]) -> str | None:
    clauses = []
    for file_id in file_ids:
        item = str(file_id).strip()
        if item:
            clauses.append(f"id = '{_drive_query_literal(item)}'")
    for folder_id in folder_ids:
        item = str(folder_id).strip()
        if item:
            clauses.append(f"'{_drive_query_literal(item)}' in parents")
    if not clauses:
        return None
    return "(" + " or ".join(clauses) + ")"


async def _fetch_googledrive(source, checkpoint: dict, *, kind: str):
    policy = _jsonb(source["sync_policy"])
    permission_policy = json_object(policy.get("permission_policy"))
    allowed_resources = json_object(permission_policy.get("allowed_resources"))
    allowed_file_ids = _string_list(allowed_resources.get("google_file_ids") or policy.get("google_file_ids"))
    allowed_folder_ids = _string_list(allowed_resources.get("google_folder_ids") or policy.get("google_folder_ids"))
    since, until = _backfill_window(source, checkpoint, kind)
    resource_query = _drive_resource_query(allowed_file_ids, allowed_folder_ids)
    query_parts = ["trashed = false"]
    if resource_query:
        query_parts.append(resource_query)
    query_parts.append(_modified_window_query(since, until))
    args = {
        "query": " and ".join(query_parts),
        "max_results": _page_size("googledrive"),
        "order_by": "modifiedTime desc",
        "response_detail": "minimal",
        "fields": "id,name,mimeType,modifiedTime,webViewLink,owners",
    }
    if checkpoint.get("page_token"):
        args["page_token"] = checkpoint["page_token"]
    data = await _execute_paged_tool(
        "GOOGLEDRIVE_FIND_FILE",
        source,
        args,
        source_type="googledrive",
        size_key="max_results",
    )
    files = (
        data.get("files")
        or data.get("items")
        or data.get("documents")
        or data.get("results")
        or []
    )
    events = []
    for file_data in files:
        file_id = file_data.get("id") or file_data.get("file_id") or file_data.get("fileId")
        if not file_id:
            continue
        modified = (
            file_data.get("modifiedTime")
            or file_data.get("modified_time")
            or file_data.get("modifiedDate")
            or file_data.get("updated_at")
            or file_data.get("updatedAt")
            or ""
        )
        events.append({
            "external_event_id": f"{file_id}:{modified or 'snapshot'}",
            "payload": {
                "event_type": "file_snapshot",
                "file": file_data,
            },
            "event_type": "file_snapshot",
        })
    next_token = data.get("next_page_token") or data.get("nextPageToken") or data.get("nextpagetoken") or None
    return events, {"page_token": next_token}, next_token is None


DEFAULT_GOOGLE_SHEET_RANGES = (
    "Overview!A1:Z80",
    "KR1!A1:Z80",
    "KR2!A1:Z80",
    "KR3!A1:Z80",
    "KR4!A1:Z80",
    "KR5!A1:Z80",
    "KR6!A1:Z80",
    "KR7!A1:Z80",
)


async def _fetch_googlesheets(source, checkpoint: dict, *, kind: str):
    allowed_resources = _googlesheets_allowed_resources(source)
    spreadsheet_ids = [
        str(item).strip()
        for item in (allowed_resources.get("google_document_ids") or [])
        if str(item).strip()
    ]
    if not spreadsheet_ids:
        return [], {"sheet_idx": 0, "manual_spreadsheet_id_required": True}, True

    sheet_idx = int(checkpoint.get("sheet_idx") or 0)
    if sheet_idx >= len(spreadsheet_ids):
        return [], {"sheet_idx": sheet_idx}, True

    spreadsheet_id = spreadsheet_ids[sheet_idx]
    ranges = _googlesheets_ranges(allowed_resources)
    data = await _execute_tool(
        "GOOGLESHEETS_BATCH_GET",
        source,
        {"spreadsheet_id": spreadsheet_id, "ranges": ranges},
    )
    value_ranges = _googlesheets_value_ranges(data)
    fingerprint = _stable_hash({"spreadsheet_id": spreadsheet_id, "valueRanges": value_ranges})
    title = _googlesheets_title(data) or "Google Sheet"
    events = [{
        "external_event_id": f"{spreadsheet_id}:{fingerprint}",
        "payload": {
            "event_type": "spreadsheet_snapshot",
            "spreadsheet_id": spreadsheet_id,
            "spreadsheet": {
                "id": spreadsheet_id,
                "title": title,
                "url": f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit",
            },
            "title": title,
            "url": f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit",
            "valueRanges": value_ranges,
            "tabs": _googlesheets_tabs(value_ranges),
            "permission_provenance": "tenant_allowlist",
        },
        "event_type": "spreadsheet_snapshot",
    }]
    next_idx = sheet_idx + 1
    return events, {"sheet_idx": next_idx}, next_idx >= len(spreadsheet_ids)


def _googlesheets_allowed_resources(source) -> dict:
    policy = _jsonb(source["sync_policy"])
    permission_policy = json_object(policy.get("permission_policy"))
    allowed_resources = {
        **json_object(permission_policy.get("allowed_resources")),
        **json_object(policy.get("allowed_resources")),
    }
    for key in ("google_document_ids", "google_folder_ids", "google_sheet_ranges"):
        if policy.get(key) and not allowed_resources.get(key):
            allowed_resources[key] = policy.get(key)
    return allowed_resources


def _googlesheets_ranges(allowed_resources: dict) -> list[str]:
    configured = [
        str(item).strip()
        for item in (allowed_resources.get("google_sheet_ranges") or [])
        if str(item).strip()
    ]
    return configured or list(DEFAULT_GOOGLE_SHEET_RANGES)


def _googlesheets_value_ranges(data: dict) -> list[dict]:
    if isinstance(data.get("valueRanges"), list):
        return [item for item in data["valueRanges"] if isinstance(item, dict)]
    if isinstance(data.get("value_ranges"), list):
        return [item for item in data["value_ranges"] if isinstance(item, dict)]
    if isinstance(data.get("data"), dict):
        return _googlesheets_value_ranges(data["data"])
    return []


def _googlesheets_title(data: dict) -> str:
    properties = data.get("properties") if isinstance(data.get("properties"), dict) else {}
    spreadsheet = data.get("spreadsheet") if isinstance(data.get("spreadsheet"), dict) else {}
    return str(data.get("title") or properties.get("title") or spreadsheet.get("title") or "").strip()


def _googlesheets_tabs(value_ranges: list[dict]) -> list[dict]:
    tabs = []
    for item in value_ranges:
        range_name = str(item.get("range") or item.get("name") or item.get("title") or "Sheet").strip()
        tab_name = range_name.split("!", 1)[0].strip("'") if range_name else "Sheet"
        values = item.get("values") or item.get("rows") or []
        if isinstance(values, list):
            tabs.append({"title": tab_name, "values": values})
    return tabs


def _stable_hash(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()[:16]


async def _fetch_linear(source, checkpoint: dict, *, kind: str):
    args = {"first": _page_size("linear")}
    if checkpoint.get("cursor"):
        args["after"] = checkpoint["cursor"]
    data = await _execute_paged_tool(
        "LINEAR_LIST_LINEAR_ISSUES",
        source,
        args,
        source_type="linear",
        size_key="first",
    )

    issues = (
        data.get("issues")
        or data.get("items")
        or data.get("nodes")
        or data.get("results")
        or []
    )
    events = []
    for issue in issues:
        issue_id = issue.get("id") or issue.get("issue_id") or issue.get("issueId")
        if not issue_id:
            continue
        updated = issue.get("updatedAt") or issue.get("updated_at") or issue.get("createdAt") or ""
        events.append({
            "external_event_id": f"{issue_id}:{updated or 'snapshot'}",
            "payload": issue,
            "event_type": "issue_snapshot",
        })

    page_info = data.get("pageInfo") or data.get("page_info") or {}
    next_cursor = (
        data.get("next_cursor")
        or data.get("nextCursor")
        or page_info.get("endCursor")
        or page_info.get("end_cursor")
        or None
    )
    has_next = bool(
        data.get("has_next_page")
        or data.get("hasNextPage")
        or page_info.get("hasNextPage")
        or page_info.get("has_next_page")
    )
    return events, {"cursor": next_cursor}, not (has_next and next_cursor)


async def _fetch_github(source, checkpoint: dict, *, kind: str):
    mode = github_access_mode(_jsonb(source["sync_policy"]))
    if mode == "off":
        return [], checkpoint, True
    policy = _jsonb(source["sync_policy"])
    if policy.get("connector_provider") == "github_app" or policy.get("github_installation_id"):
        return await _fetch_github_app(source, checkpoint)

    page = int(checkpoint.get("page") or 1)
    data = await _execute_paged_tool(
        "GITHUB_FIND_PULL_REQUESTS",
        source,
        {
            "for_authenticated_user": True,
            "state": "all",
            "sort": "updated",
            "order": "desc",
            "per_page": _page_size("github"),
            "page": page,
            "response_detail": "minimal",
        },
        source_type="github",
        size_key="per_page",
    )
    pull_requests = [sanitize_github_pull_request(pr) for pr in (data.get("pull_requests") or [])]
    events = []
    for pr in pull_requests:
        pr_id = pr.get("id") or pr.get("node_id") or pr.get("number")
        if not pr_id:
            continue
        updated = pr.get("updated_at") or pr.get("created_at") or ""
        events.append({
            "external_event_id": f"pr:{pr_id}:{updated or 'snapshot'}",
            "payload": pr,
            "event_type": "pull_request_snapshot",
        })

    total_count = int(data.get("total_count") or 0)
    page_size = int(data.get("__inferencehq_page_size") or _page_size("github"))
    done = len(pull_requests) < page_size or (total_count > 0 and page * page_size >= total_count)
    return events, {"page": page + 1}, done


async def _fetch_github_app(source, checkpoint: dict):
    policy = _jsonb(source["sync_policy"])
    installation_id = str(policy.get("github_installation_id") or "").strip()
    if not installation_id:
        raise RuntimeError("GitHub App source is missing github_installation_id")
    allowed_resources = _jsonb(policy.get("allowed_resources"))
    repositories = allowed_resources.get("github_repositories") or []
    if not repositories:
        return [], checkpoint, True

    repo_idx = int(checkpoint.get("repo_idx") or 0)
    page = int(checkpoint.get("page") or 1)
    page_size = _page_size("github")
    if repo_idx >= len(repositories):
        return [], {"repo_idx": repo_idx, "page": page}, True

    repo = repositories[repo_idx]
    full_name = repo.get("full_name") if isinstance(repo, dict) else str(repo)
    if not full_name:
        return [], {"repo_idx": repo_idx + 1, "page": 1}, repo_idx + 1 >= len(repositories)

    token = await create_installation_token(installation_id)
    pull_requests = await fetch_pull_requests_for_repo(
        installation_token=token,
        full_name=full_name,
        page=page,
        per_page=page_size,
    )
    events = []
    for pr in pull_requests:
        pr = sanitize_github_pull_request(dict(pr))
        pr.setdefault("repository", {"full_name": full_name})
        pr_id = pr.get("id") or pr.get("node_id") or pr.get("number")
        if not pr_id:
            continue
        updated = pr.get("updated_at") or pr.get("created_at") or ""
        events.append({
            "external_event_id": f"pr:{full_name}:{pr_id}:{updated or 'snapshot'}",
            "payload": pr,
            "event_type": "pull_request_snapshot",
        })

    repo_done = len(pull_requests) < page_size
    next_checkpoint = (
        {"repo_idx": repo_idx + 1, "page": 1}
        if repo_done
        else {"repo_idx": repo_idx, "page": page + 1}
    )
    done = repo_done and next_checkpoint["repo_idx"] >= len(repositories)
    return events, next_checkpoint, done


HUBSPOT_FETCH_SEQUENCE = [
    ("contacts", "HUBSPOT_LIST_CONTACTS"),
    ("companies", "HUBSPOT_LIST_COMPANIES"),
    ("deals", "HUBSPOT_LIST_DEALS"),
    ("tickets", "HUBSPOT_LIST_TICKETS"),
    ("owners", "HUBSPOT_RETRIEVE_OWNERS"),
]


async def _fetch_hubspot(source, checkpoint: dict, *, kind: str):
    object_idx = int(checkpoint.get("object_idx") or 0)
    after = checkpoint.get("after")
    if object_idx >= len(HUBSPOT_FETCH_SEQUENCE):
        return [], checkpoint, True

    object_type, slug = HUBSPOT_FETCH_SEQUENCE[object_idx]
    args = {"limit": _page_size("hubspot")}
    if after:
        args["after"] = after
    data = await _execute_paged_tool(
        slug,
        source,
        args,
        source_type="hubspot",
        size_key="limit",
    )

    records = _hubspot_records(data)
    events = []
    for record in records:
        if not isinstance(record, dict):
            continue
        record_id = (
            record.get("id")
            or record.get("objectId")
            or record.get("object_id")
            or record.get("userId")
            or record.get("user_id")
            or record.get("email")
        )
        if not record_id:
            continue
        updated = (
            record.get("updatedAt")
            or record.get("updated_at")
            or record.get("lastmodifieddate")
            or (record.get("properties") or {}).get("lastmodifieddate")
            or record.get("createdAt")
            or record.get("created_at")
            or ""
        )
        events.append({
            "external_event_id": f"{object_type}:{record_id}:{updated or 'snapshot'}",
            "payload": {
                "event_type": "crm_object_snapshot",
                "object_type": object_type,
                "record": record,
            },
            "event_type": f"{object_type}_snapshot",
        })

    next_after = _hubspot_next_after(data)
    if next_after:
        return events, {"object_idx": object_idx, "after": next_after}, False
    next_idx = object_idx + 1
    return events, {"object_idx": next_idx, "after": None}, next_idx >= len(HUBSPOT_FETCH_SEQUENCE)


def _hubspot_records(data: dict) -> list:
    for key in ("results", "contacts", "companies", "deals", "tickets", "owners", "items"):
        value = data.get(key)
        if isinstance(value, list):
            return value
    return []


def _hubspot_next_after(data: dict) -> str | None:
    paging = data.get("paging") if isinstance(data.get("paging"), dict) else {}
    next_page = paging.get("next") if isinstance(paging.get("next"), dict) else {}
    value = (
        next_page.get("after")
        or data.get("after")
        or data.get("next_after")
        or data.get("nextAfter")
        or data.get("next_page_token")
        or data.get("nextPageToken")
    )
    return str(value) if value else None


FETCHERS = {
    "slack": _fetch_slack,
    "gmail": _fetch_gmail,
    "gcal": _fetch_gcal,
    "googledocs": _fetch_googledocs,
    "googledrive": _fetch_googledrive,
    "googlesheets": _fetch_googlesheets,
    "linear": _fetch_linear,
    "github": _fetch_github,
    "hubspot": _fetch_hubspot,
}
