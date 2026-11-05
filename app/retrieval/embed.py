"""
Optional embeddings via OpenAI. Retrieval degrades to FTS + recency when no
OPENAI_API_KEY is set — never a hard dependency.
"""
from datetime import datetime, timezone
from typing import Any

import structlog

from app.core.config import get_settings
from app.core.db import acquire

logger = structlog.get_logger()

EMBEDDING_BATCH_SIZE = 50
_embedding_client: Any | None = None
_embedding_client_api_key: str | None = None


def embeddings_enabled() -> bool:
    settings = get_settings()
    return bool(settings.openai_api_key) and not settings.llm_fixture_mode


def _get_embedding_client(settings) -> Any:
    global _embedding_client, _embedding_client_api_key
    if _embedding_client is None or _embedding_client_api_key != settings.openai_api_key:
        from openai import AsyncOpenAI  # transitive dep via composio

        _embedding_client = AsyncOpenAI(api_key=settings.openai_api_key)
        _embedding_client_api_key = settings.openai_api_key
    return _embedding_client


async def _embed_texts_raw(texts: list[str]) -> list[list[float] | None]:
    settings = get_settings()
    client = _get_embedding_client(settings)

    response = await client.embeddings.create(
        model=settings.embedding_model,
        input=[text[:8000] for text in texts],
    )
    embeddings_by_index = {
        int(getattr(item, "index", index)): item.embedding
        for index, item in enumerate(response.data)
    }
    return [embeddings_by_index.get(index) for index in range(len(texts))]


async def embed_texts(texts: list[str]) -> list[list[float] | None]:
    if not embeddings_enabled():
        return [None for _ in texts]
    if not texts:
        return []
    try:
        return await _embed_texts_raw(texts)
    except Exception as e:
        logger.warning("embed_batch_failed", error=str(e), count=len(texts))
        return [None for _ in texts]


async def _mark_embedding_failed(chunk_id: str, *, tenant_id: str, reason: str) -> None:
    failed_at = datetime.now(timezone.utc).isoformat()
    async with acquire() as conn:
        await conn.execute(
            """
            UPDATE content_chunks
            SET metadata = jsonb_set(
                COALESCE(metadata, '{}'::jsonb),
                '{embedding_failed_at}',
                to_jsonb($3::text),
                true
            )
            WHERE id = $1 AND tenant_id = $2
            """,
            chunk_id, tenant_id, failed_at,
        )
    logger.warning("embed_chunk_skipped", chunk_id=chunk_id, tenant_id=tenant_id, reason=reason)


async def _embed_rows_with_bisect(rows: list[dict], *, tenant_id: str) -> list[list[float] | None]:
    if not rows:
        return []
    try:
        return await _embed_texts_raw([row["text"] for row in rows])
    except Exception as e:
        if len(rows) == 1:
            await _mark_embedding_failed(str(rows[0]["id"]), tenant_id=tenant_id, reason=str(e))
            return [None]
        mid = max(1, len(rows) // 2)
        left = await _embed_rows_with_bisect(rows[:mid], tenant_id=tenant_id)
        right = await _embed_rows_with_bisect(rows[mid:], tenant_id=tenant_id)
        return left + right


async def embed_text(text: str) -> list[float] | None:
    embeddings = await embed_texts([text])
    return embeddings[0] if embeddings else None


async def backfill_embeddings(ctx: dict, *, tenant_id: str, batch: int = 50) -> int:
    """Cron job: embed chunks that don't have embeddings yet."""
    if not embeddings_enabled():
        return 0
    from app.core.db import _current_tenant_id
    _current_tenant_id.set(tenant_id)

    async with acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, text FROM content_chunks
            WHERE tenant_id = $1
              AND embedding IS NULL
              AND COALESCE(metadata->>'embedding_failed_at', '') = ''
              AND acl_state = 'stamped'
              AND acl_freshness_state = 'fresh'
              AND jsonb_array_length(allowed_principals) > 0
            ORDER BY created_at DESC LIMIT $2
            """,
            tenant_id, batch,
        )
    embedded = 0
    for start in range(0, len(rows), EMBEDDING_BATCH_SIZE):
        batch_rows = rows[start:start + EMBEDDING_BATCH_SIZE]
        vectors = await _embed_rows_with_bisect(batch_rows, tenant_id=tenant_id)
        for row, vector in zip(batch_rows, vectors):
            if vector is None:
                continue
            async with acquire() as conn:
                status = await conn.execute(
                    """
                    UPDATE content_chunks
                    SET embedding = $2
                    WHERE id = $1
                      AND tenant_id = $3
                      AND acl_state = 'stamped'
                      AND acl_freshness_state = 'fresh'
                      AND jsonb_array_length(allowed_principals) > 0
                    """,
                    row["id"], str(vector), tenant_id,
                )
            if str(status).endswith(" 1"):
                embedded += 1
    if embedded:
        logger.info("embeddings_backfilled", tenant_id=tenant_id, count=embedded)
    return embedded
