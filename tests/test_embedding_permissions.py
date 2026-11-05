from contextlib import asynccontextmanager

import pytest

import app.retrieval.embed as embed_module
import app.workers.health as health_module


class FakeEmbeddingDb:
    def __init__(self, *, rows=None, update_status: str = "UPDATE 1"):
        self.fetches = []
        self.executes = []
        self.rows = rows or [{"id": "chunk-1", "text": "Company-visible launch update.", "metadata": {}, "embedding": None}]
        self.update_status = update_status

    @asynccontextmanager
    async def acquire(self):
        yield self

    async def fetch(self, query, *args):
        self.fetches.append((query, args))
        return [
            row
            for row in self.rows
            if row.get("embedding") is None and not (row.get("metadata") or {}).get("embedding_failed_at")
        ]

    async def execute(self, query, *args):
        self.executes.append((query, args))
        if "SET embedding = $2" in query:
            chunk_id, embedding, tenant_id = args
            row = next(row for row in self.rows if row["id"] == chunk_id)
            row["embedding"] = embedding
        elif "embedding_failed_at" in query:
            chunk_id, tenant_id, failed_at = args
            row = next(row for row in self.rows if row["id"] == chunk_id)
            row.setdefault("metadata", {})["embedding_failed_at"] = failed_at
        return self.update_status


@pytest.mark.asyncio
async def test_embedding_backfill_only_selects_and_updates_fresh_stamped_chunks(monkeypatch):
    database = FakeEmbeddingDb()
    fake_client = FakeEmbeddingClient()
    monkeypatch.setattr(embed_module, "acquire", database.acquire)
    monkeypatch.setattr(embed_module, "embeddings_enabled", lambda: True)
    monkeypatch.setattr(embed_module, "_get_embedding_client", lambda settings: fake_client)

    embedded = await embed_module.backfill_embeddings({}, tenant_id="tenant-1", batch=10)

    select_query, select_args = database.fetches[0]
    update_query, update_args = database.executes[0]
    assert embedded == 1
    assert select_args == ("tenant-1", 10)
    assert "acl_state = 'stamped'" in select_query
    assert "acl_freshness_state = 'fresh'" in select_query
    assert "jsonb_array_length(allowed_principals) > 0" in select_query
    assert update_args == ("chunk-1", str([0.0, 0.0, float(len("Company-visible launch update."))]), "tenant-1")
    assert len(fake_client.embeddings.calls) == 1
    assert "tenant_id = $3" in update_query
    assert "acl_state = 'stamped'" in update_query
    assert "acl_freshness_state = 'fresh'" in update_query
    assert "jsonb_array_length(allowed_principals) > 0" in update_query


@pytest.mark.asyncio
async def test_embedding_backfill_does_not_count_revoked_race_update(monkeypatch):
    database = FakeEmbeddingDb(update_status="UPDATE 0")
    fake_client = FakeEmbeddingClient()
    monkeypatch.setattr(embed_module, "acquire", database.acquire)
    monkeypatch.setattr(embed_module, "embeddings_enabled", lambda: True)
    monkeypatch.setattr(embed_module, "_get_embedding_client", lambda settings: fake_client)

    embedded = await embed_module.backfill_embeddings({}, tenant_id="tenant-1", batch=10)

    assert embedded == 0


@pytest.mark.asyncio
async def test_embedding_backfill_batches_openai_calls_at_fifty_chunks(monkeypatch):
    rows = [
        {"id": f"chunk-{index}", "text": f"Document chunk {index}"}
        for index in range(500)
    ]
    database = FakeEmbeddingDb(rows=rows)
    fake_client = FakeEmbeddingClient()
    monkeypatch.setattr(embed_module, "acquire", database.acquire)
    monkeypatch.setattr(embed_module, "embeddings_enabled", lambda: True)
    monkeypatch.setattr(embed_module, "_get_embedding_client", lambda settings: fake_client)

    embedded = await embed_module.backfill_embeddings({}, tenant_id="tenant-1", batch=500)

    assert embedded == 500
    assert len(fake_client.embeddings.calls) == 10
    assert all(len(call["input"]) == 50 for call in fake_client.embeddings.calls)
    assert database.executes[0][1] == ("chunk-0", str([0.0, 0.0, 16.0]), "tenant-1")
    assert database.executes[-1][1] == ("chunk-499", str([9.0, 49.0, 18.0]), "tenant-1")


@pytest.mark.asyncio
async def test_embedding_backfill_bisects_poison_batch_and_skips_future_runs(monkeypatch):
    rows = [
        {"id": f"chunk-{index}", "text": "poison text" if index == 17 else f"Document chunk {index}", "metadata": {}, "embedding": None}
        for index in range(50)
    ]
    database = FakeEmbeddingDb(rows=rows)
    fake_client = PoisonEmbeddingClient(poison_text="poison text")
    monkeypatch.setattr(embed_module, "acquire", database.acquire)
    monkeypatch.setattr(embed_module, "embeddings_enabled", lambda: True)
    monkeypatch.setattr(embed_module, "_get_embedding_client", lambda settings: fake_client)

    embedded_first = await embed_module.backfill_embeddings({}, tenant_id="tenant-1", batch=50)
    embedded_second = await embed_module.backfill_embeddings({}, tenant_id="tenant-1", batch=50)

    assert embedded_first == 49
    assert embedded_second == 0
    assert len(fake_client.embeddings.calls) > 1
    assert any(row["id"] == "chunk-17" and row.get("metadata", {}).get("embedding_failed_at") for row in database.rows)
    assert all(row.get("embedding") is not None or row["id"] == "chunk-17" for row in database.rows)


@pytest.mark.asyncio
async def test_embed_text_uses_shared_async_client(monkeypatch):
    fake_client = FakeEmbeddingClient()
    monkeypatch.setattr(embed_module, "embeddings_enabled", lambda: True)
    monkeypatch.setattr(embed_module, "_get_embedding_client", lambda settings: fake_client)

    vector = await embed_module.embed_text("single query")

    assert vector == [0.0, 0.0, 12.0]
    assert len(fake_client.embeddings.calls) == 1
    assert fake_client.embeddings.calls[0]["input"] == ["single query"]


class FakeTenantScanDb:
    def __init__(self):
        self.fetches = []

    @asynccontextmanager
    async def acquire(self):
        yield self

    async def fetch(self, query, *args):
        self.fetches.append((query, args))
        return []


@pytest.mark.asyncio
async def test_embedding_cycle_scans_only_fresh_stamped_chunks(monkeypatch):
    database = FakeTenantScanDb()
    monkeypatch.setattr(health_module, "acquire", database.acquire)
    monkeypatch.setattr("app.retrieval.embed.embeddings_enabled", lambda: True)

    await health_module.embedding_cycle({})

    query, args = database.fetches[0]
    assert args == ()
    assert "acl_state = 'stamped'" in query
    assert "acl_freshness_state = 'fresh'" in query
    assert "jsonb_array_length(allowed_principals) > 0" in query


async def _async_value(value):
    return value


class FakeEmbeddingClient:
    def __init__(self):
        self.embeddings = FakeEmbeddingsEndpoint()


class PoisonEmbeddingClient:
    def __init__(self, *, poison_text: str):
        self.poison_text = poison_text
        self.embeddings = PoisonEmbeddingsEndpoint(poison_text=poison_text)


class FakeEmbeddingsEndpoint:
    def __init__(self):
        self.calls = []

    async def create(self, *, model, input):
        call_index = len(self.calls)
        self.calls.append({"model": model, "input": input})
        return FakeEmbeddingResponse([
            FakeEmbeddingItem(index=index, embedding=[float(call_index), float(index), float(len(text))])
            for index, text in enumerate(input)
        ])


class PoisonEmbeddingsEndpoint:
    def __init__(self, *, poison_text: str):
        self.poison_text = poison_text
        self.calls = []

    async def create(self, *, model, input):
        self.calls.append({"model": model, "input": input})
        if any(self.poison_text in text for text in input):
            raise RuntimeError("poison batch")
        return FakeEmbeddingResponse([
            FakeEmbeddingItem(index=index, embedding=[float(len(text)), float(index), 1.0])
            for index, text in enumerate(input)
        ])


class FakeEmbeddingResponse:
    def __init__(self, data):
        self.data = data


class FakeEmbeddingItem:
    def __init__(self, *, index, embedding):
        self.index = index
        self.embedding = embedding
