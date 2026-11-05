"""ARQ queue: job enqueueing with a shared connection pool."""
import uuid
from urllib.parse import urlparse
from datetime import timedelta

from arq import create_pool as arq_create_pool
from arq.connections import RedisSettings

from app.core.config import get_settings
from app.core.redis import get_redis
from app.core import trace
from app.ingestion.event_identity import payload_sha256

_arq_pool = None


def get_redis_settings() -> RedisSettings:
    settings = get_settings()
    parsed = urlparse(settings.redis_url)
    return RedisSettings(
        host=parsed.hostname or "localhost",
        port=parsed.port or 6379,
        database=int(parsed.path.lstrip("/") or 0),
        password=parsed.password or None,
        username=parsed.username or None,
    )


async def _pool():
    global _arq_pool
    if _arq_pool is None:
        _arq_pool = await arq_create_pool(get_redis_settings())
    return _arq_pool


async def close_arq_pool() -> None:
    global _arq_pool
    if _arq_pool:
        await _arq_pool.aclose()
        _arq_pool = None


async def enqueue_ingest(
    *,
    tenant_id: str,
    source_id: str,
    external_event_id: str,
    payload: dict,
    event_type: str | None = None,
    is_backfill: bool = False,
    sync_run_id: str | None = None,
    pipeline_run_id: str | None = None,
    defer_seconds: float | None = None,
    force: bool = False,
) -> None:
    kwargs = {}
    if defer_seconds and defer_seconds > 0:
        from datetime import timedelta
        kwargs["_defer_by"] = timedelta(seconds=defer_seconds)

    redis = await _pool()
    payload_hash = payload_sha256(payload)
    job_id = f"ingest:{source_id}:{external_event_id}:{payload_hash}"
    if force:
        job_id = f"{job_id}:resume:{uuid.uuid4()}"
    await redis.enqueue_job(
        "ingest_event",
        tenant_id=tenant_id,
        source_id=source_id,
        external_event_id=external_event_id,
        payload=payload,
        event_type=event_type,
        is_backfill=is_backfill,
        sync_run_id=sync_run_id,
        pipeline_run_id=pipeline_run_id,
        # one ARQ job id per event: a webhook retry can't double-process
        _job_id=job_id,
        **kwargs,
    )


async def enqueue_synthesis(
    *,
    tenant_id: str,
    source_id: str | None = None,
    normalized_event_id: str,
    thread_key: str | None,
    object_ids: list[str],
    pipeline_run_id: str,
) -> str:
    """
    Debounced per thread: rapid-fire messages in one conversation collapse to
    a single synthesis run after the batch window.
    """
    settings = get_settings()
    redis = await get_redis()
    debounce_key = f"synthesis_debounce:{tenant_id}:{thread_key or normalized_event_id}"
    queued = await redis.set(
        debounce_key,
        pipeline_run_id,
        ex=settings.triage_batch_window_seconds,
        nx=True,
    )
    if not queued:
        existing = await redis.get(debounce_key)
        return existing.decode() if isinstance(existing, bytes) else str(existing or pipeline_run_id)

    arq = await _pool()
    await arq.enqueue_job(
        "run_operating_synthesis",
        tenant_id=tenant_id,
        source_id=source_id,
        normalized_event_id=normalized_event_id,
        thread_key=thread_key,
        object_ids=object_ids,
        pipeline_run_id=pipeline_run_id,
        _defer_by=timedelta(seconds=settings.triage_batch_window_seconds),
    )
    return pipeline_run_id


async def enqueue_backfill_page(*, tenant_id: str, source_id: str, sync_run_id: str) -> None:
    arq = await _pool()
    await arq.enqueue_job(
        "backfill_page", tenant_id=tenant_id, source_id=source_id, sync_run_id=sync_run_id,
    )


async def enqueue_source_synthesis(
    *,
    tenant_id: str,
    reason: str,
    source_id: str | None = None,
    sync_run_id: str | None = None,
    requested_by_user_id: str | None = None,
    defer_seconds: float | None = None,
    force: bool = False,
) -> dict:
    """
    Tenant/source-level synthesis for manual refreshes and completed backfills.
    Redis dedupe makes repeated refresh taps or duplicate completion signals no-ops.
    """
    settings = get_settings()
    scope = f"sync:{sync_run_id}" if sync_run_id else f"manual:{requested_by_user_id or tenant_id}"
    debounce_key = f"source_synthesis:{tenant_id}:{source_id or 'all'}:{scope}"
    redis = await get_redis()
    existing = await redis.get(debounce_key)
    if existing and not force:
        existing_id = existing.decode() if isinstance(existing, bytes) else str(existing)
        return {"status": "already_queued", "pipeline_run_id": existing_id}

    run_id = await trace.start_run(
        tenant_id=tenant_id,
        kind="source_synthesis",
        source_id=source_id,
    )
    queued = await redis.set(
        debounce_key,
        run_id,
        ex=settings.source_synthesis_dedupe_seconds,
        nx=not force,
    )
    if not queued:
        existing = await redis.get(debounce_key)
        existing_id = existing.decode() if isinstance(existing, bytes) else str(existing or run_id)
        await trace.finish_run(run_id, status="coalesced")
        return {"status": "already_queued", "pipeline_run_id": existing_id}

    kwargs = {}
    if defer_seconds and defer_seconds > 0:
        kwargs["_defer_by"] = timedelta(seconds=defer_seconds)

    arq = await _pool()
    await arq.enqueue_job(
        "run_source_synthesis",
        tenant_id=tenant_id,
        source_id=source_id,
        sync_run_id=sync_run_id,
        reason=reason,
        requested_by_user_id=requested_by_user_id,
        pipeline_run_id=run_id,
        drain_attempt=0,
        _job_id=f"source_synthesis:{run_id}",
        **kwargs,
    )
    return {"status": "queued", "pipeline_run_id": run_id}


async def enqueue_source_synthesis_retry(
    *,
    tenant_id: str,
    source_id: str | None,
    sync_run_id: str | None,
    reason: str,
    pipeline_run_id: str,
    drain_attempt: int,
    defer_seconds: float,
) -> None:
    arq = await _pool()
    await arq.enqueue_job(
        "run_source_synthesis",
        tenant_id=tenant_id,
        source_id=source_id,
        sync_run_id=sync_run_id,
        reason=reason,
        requested_by_user_id=None,
        pipeline_run_id=pipeline_run_id,
        drain_attempt=drain_attempt,
        _defer_by=timedelta(seconds=defer_seconds),
    )


async def enqueue_daily_brief(*, tenant_id: str, force: bool = False) -> dict:
    """Queue an on-demand daily brief generation for demos and operator refreshes.

    The brief generator itself enforces one brief per tenant-local date.
    """
    redis = await get_redis()
    debounce_key = f"daily_brief:{tenant_id}"
    existing = await redis.get(debounce_key)
    if existing:
        return {"status": "already_queued"}
    queued = await redis.set(debounce_key, "queued", ex=600, nx=True)
    if not queued:
        return {"status": "already_queued"}
    arq = await _pool()
    await arq.enqueue_job(
        "run_daily_brief_job",
        tenant_id=tenant_id,
        _job_id=f"daily_brief:{tenant_id}:{uuid.uuid4()}",
    )
    return {"status": "queued"}
