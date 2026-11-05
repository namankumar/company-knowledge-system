from contextlib import asynccontextmanager
import json
from pathlib import Path

import pytest

from app.retrieval import retrieve
from tests.fixtures.permission_corpus import PERMISSION_PAIRWISE_CASES


def _pairwise_chunk(case: dict) -> dict:
    return {
        "kind": "chunk",
        "id": case["id"],
        "text": f"Pairwise retrieval evidence for {case['id']}.",
        "source_type": case["source_type"],
        "priority": 0,
        "score": 0.5,
        **case["expected_envelope"],
    }


def _is_fresh_stamped(envelope: dict) -> bool:
    return envelope["acl_state"] == "stamped" and envelope["acl_freshness_state"] == "fresh"


def test_audience_principals_include_actor_group_and_company_scope():
    principals = retrieve.audience_principals(
        tenant_id="tenant-1",
        actor_user_id="user-1",
        target_audience="company",
        actor_principals=["group:product"],
    )

    assert principals == ["company:tenant-1", "group:product", "user:user-1"]


def _assert_chunk_query_prefilters_before_ranking(query: str):
    assert "FROM content_chunks cc" in query
    assert "cc.allowed_principals ?| $5::text[]" in query
    assert "ne.allowed_principals ?| $5::text[]" in query
    assert "re.allowed_principals ?| $5::text[]" in query
    assert "cc.acl_state = 'stamped' AND cc.acl_freshness_state = 'fresh'" in query
    assert "ne.acl_state = 'stamped'" in query
    assert "ne.acl_freshness_state = 'fresh'" in query
    assert "re.acl_state = 'stamped'" in query
    assert "re.acl_freshness_state = 'fresh'" in query
    assert "s.source_type = ANY($4::text[])" in query
    assert "COALESCE(s.source_type, cc.source_type)" not in query
    order_index = query.index("ORDER BY")
    for fragment in (
        "cc.allowed_principals ?| $5::text[]",
        "ne.allowed_principals ?| $5::text[]",
        "re.allowed_principals ?| $5::text[]",
        "cc.acl_state = 'stamped' AND cc.acl_freshness_state = 'fresh'",
        "s.source_type = ANY($4::text[])",
    ):
        assert query.index(fragment) < order_index


@pytest.mark.asyncio
async def test_principal_filters_are_present_before_sql_and_vector_ranking(monkeypatch):
    database = FakeRetrievalDb()

    monkeypatch.setattr(retrieve, "acquire", database.acquire)
    monkeypatch.setattr(retrieve, "embeddings_enabled", lambda: True)
    monkeypatch.setattr(retrieve, "embed_text", _fake_embed_text)

    await retrieve._search_chunks(
        "tenant-1",
        "launch",
        source_types=set(),
        allowed_principals=["company:tenant-1"],
    )
    await retrieve._recent_chunks(
        "tenant-1",
        72,
        source_types=set(),
        allowed_principals=["company:tenant-1"],
    )

    chunk_queries = [query for query, _ in database.fetches if "FROM content_chunks cc" in query]
    assert len(chunk_queries) == 3
    for query in chunk_queries:
        assert "cc.allowed_principals ?| $5::text[]" in query
        assert query.index("cc.allowed_principals ?| $5::text[]") < query.index("ORDER BY")
        assert "ne.acl_state = 'stamped'" in query
        assert "ne.acl_freshness_state = 'fresh'" in query
        assert "re.acl_state = 'stamped'" in query
        assert "re.acl_freshness_state = 'fresh'" in query
        assert "ne.allowed_principals ?| $5::text[]" in query
        assert "re.allowed_principals ?| $5::text[]" in query
        assert query.index("ne.allowed_principals ?| $5::text[]") < query.index("ORDER BY")
        assert query.index("re.allowed_principals ?| $5::text[]") < query.index("ORDER BY")
        assert "s.source_type = ANY($4::text[])" in query
        assert "COALESCE(s.source_type, cc.source_type)" not in query

    assert retrieve._principal_filter("bo", 3) == "bo.allowed_principals ?| $3::text[]"
    assert retrieve._principal_filter("f", 4) == "f.allowed_principals ?| $4::text[]"
    assert retrieve._principal_filter("alerts", 3) == "alerts.allowed_principals ?| $3::text[]"


@pytest.mark.parametrize("case", PERMISSION_PAIRWISE_CASES, ids=lambda case: case["id"])
@pytest.mark.asyncio
async def test_pairwise_chunk_search_binds_acl_before_fts_and_vector_top_k(monkeypatch, case):
    database = FakeRetrievalDb()
    source_filter = [case["source_type"]]
    allowed_principals = case["expected_envelope"]["allowed_principals"]
    denied_principals = list(dict.fromkeys(case["denied_viewers"] + case["denied_groups"]))

    monkeypatch.setattr(retrieve, "acquire", database.acquire)
    monkeypatch.setattr(retrieve, "embeddings_enabled", lambda: True)
    monkeypatch.setattr(retrieve, "embed_text", _fake_embed_text)

    for principals in (allowed_principals, denied_principals):
        database.fetches.clear()

        await retrieve._search_chunks(
            "tenant-1",
            "pairwise",
            source_types={case["source_type"]},
            allowed_principals=principals,
        )

        chunk_queries = [query for query, _ in database.fetches if "FROM content_chunks cc" in query]
        assert len(chunk_queries) == 2
        assert any("ts_rank" in query for query in chunk_queries)
        assert any("cc.embedding <=>" in query for query in chunk_queries)
        for query, args in database.fetches:
            if "FROM content_chunks cc" not in query:
                continue
            _assert_chunk_query_prefilters_before_ranking(query)
            assert args[3] == source_filter
            assert args[4] == principals


def test_in_memory_authorization_guard_blocks_bad_rows_before_prompt_text():
    allowed = {
        "kind": "chunk",
        "id": "allowed",
        "text": "Company-visible launch date moved.",
        "allowed_principals": ["company:tenant-1"],
        "acl_state": "stamped",
        "acl_freshness_state": "fresh",
    }
    denied = {
        "kind": "chunk",
        "id": "denied",
        "text": "Private Gmail negotiation details.",
        "allowed_principals": ["user:private-owner"],
        "acl_state": "stamped",
        "acl_freshness_state": "fresh",
    }
    stale = {
        "kind": "chunk",
        "id": "stale",
        "text": "Stale company source.",
        "allowed_principals": ["company:tenant-1"],
        "acl_state": "stamped",
        "acl_freshness_state": "stale",
    }

    filtered = retrieve._filter_authorized_items([allowed, denied, stale], ["company:tenant-1"])
    prompt_text = retrieve._render(filtered)

    assert filtered == [allowed]
    assert "Company-visible launch date" in prompt_text
    assert "Private Gmail" not in prompt_text
    assert "Stale company source" not in prompt_text


@pytest.mark.parametrize("case", PERMISSION_PAIRWISE_CASES, ids=lambda case: case["id"])
def test_pairwise_permission_corpus_is_filtered_before_prompt_text(case):
    item = _pairwise_chunk(case)
    envelope = case["expected_envelope"]
    allowed_principals = envelope["allowed_principals"]

    filtered = retrieve._filter_authorized_items([item], allowed_principals)
    prompt_text = retrieve._render(filtered)

    if _is_fresh_stamped(envelope) and allowed_principals:
        assert filtered == [item]
        assert item["text"] in prompt_text
    else:
        assert filtered == []
        assert item["text"] not in prompt_text

    denied_principals = case["denied_viewers"] + case["denied_groups"]
    denied_filtered = retrieve._filter_authorized_items([item], denied_principals)
    denied_prompt_text = retrieve._render(denied_filtered)

    assert denied_filtered == []
    assert item["text"] not in denied_prompt_text


@pytest.mark.asyncio
async def test_authorized_context_pack_filters_pairwise_candidates_before_saving(monkeypatch):
    database = FakeContextPackDb()
    pairwise_candidates = [_pairwise_chunk(case) for case in PERMISSION_PAIRWISE_CASES]
    company_principals = ["company:exampleco"]

    async def no_matches(*args, **kwargs):
        return []

    async def chunk_results(*args, **kwargs):
        return pairwise_candidates

    monkeypatch.setattr(retrieve, "acquire", database.acquire)
    monkeypatch.setattr(retrieve, "_match_objects_by_name", no_matches)
    monkeypatch.setattr(retrieve, "_graph_neighbors", no_matches)
    monkeypatch.setattr(retrieve, "_load_objects", no_matches)
    monkeypatch.setattr(retrieve, "_open_risk_objects", no_matches)
    monkeypatch.setattr(retrieve, "_load_facts", no_matches)
    monkeypatch.setattr(retrieve, "_search_alerts", no_matches)
    monkeypatch.setattr(retrieve, "_recent_chunks", no_matches)
    monkeypatch.setattr(retrieve, "_search_chunks", chunk_results)

    async def record_permission_check(**kwargs):
        return "permission-check-1"

    monkeypatch.setattr(retrieve, "record_permission_check", record_permission_check)

    pack = await retrieve.build_authorized_context_pack(
        tenant_id="exampleco",
        actor_user_id=None,
        actor_principals=company_principals,
        target_audience="team_group",
        purpose="chat",
        query="pairwise",
    )

    saved_items = json.loads(database.executed[0][1][4])
    expected = [
        {**item, "retrieval_strategy": retrieve.ASK_ANSWER_PACK}
        for item in pairwise_candidates
        if retrieve._item_visible_to_principals(item, company_principals)
    ]
    blocked = [
        item for item in pairwise_candidates
        if not retrieve._item_visible_to_principals(item, company_principals)
    ]

    assert pack["items"] == expected
    assert saved_items == expected
    assert pack["retrieval_strategy"] == retrieve.ASK_ANSWER_PACK
    assert pack["permission"]["allowed_principals"] == company_principals
    assert pack["permission"]["retrieval_strategy"] == retrieve.ASK_ANSWER_PACK
    for item in expected:
        assert item["text"] in pack["text"]
    for item in blocked:
        assert item["text"] not in pack["text"]


@pytest.mark.asyncio
async def test_authorized_context_pack_never_saves_unauthorized_evidence(monkeypatch):
    database = FakeContextPackDb()

    async def no_matches(*args, **kwargs):
        return []

    async def chunk_results(*args, **kwargs):
        return [
            {
                "kind": "chunk",
                "id": "allowed",
                "text": "Company-visible launch date moved.",
                "source_url": "https://linear.app/company/issue/ENG-1",
                "source_type": "linear",
                "allowed_principals": ["company:tenant-1"],
                "acl_state": "stamped",
                "acl_freshness_state": "fresh",
                "priority": 0,
                "score": 0.95,
            },
            {
                "kind": "chunk",
                "id": "denied",
                "text": "Private Gmail negotiation details.",
                "source_url": "https://mail.google.com/mail/u/0/#inbox/private",
                "source_type": "gmail",
                "allowed_principals": ["user:private-owner"],
                "acl_state": "stamped",
                "acl_freshness_state": "fresh",
                "priority": 0,
                "score": 0.99,
            },
        ]

    monkeypatch.setattr(retrieve, "acquire", database.acquire)
    monkeypatch.setattr(retrieve, "_match_objects_by_name", no_matches)
    monkeypatch.setattr(retrieve, "_graph_neighbors", no_matches)
    monkeypatch.setattr(retrieve, "_load_objects", no_matches)
    monkeypatch.setattr(retrieve, "_open_risk_objects", no_matches)
    monkeypatch.setattr(retrieve, "_load_facts", no_matches)
    monkeypatch.setattr(retrieve, "_search_alerts", no_matches)
    monkeypatch.setattr(retrieve, "_recent_chunks", no_matches)
    monkeypatch.setattr(retrieve, "_search_chunks", chunk_results)
    permission_checks = []

    async def record_permission_check(**kwargs):
        permission_checks.append(kwargs)
        return "permission-check-1"

    monkeypatch.setattr(retrieve, "record_permission_check", record_permission_check)

    pack = await retrieve.build_authorized_context_pack(
        tenant_id="tenant-1",
        actor_user_id="founder-1",
        requester_user_id="founder-1",
        target_audience="company",
        destination="ask_discuss",
        purpose="chat",
        query="launch date",
    )

    saved_items = json.loads(database.executed[0][1][4])

    assert [item["id"] for item in pack["items"]] == ["allowed"]
    assert [item["id"] for item in saved_items] == ["allowed"]
    assert all(item["retrieval_strategy"] == retrieve.ASK_ANSWER_PACK for item in pack["items"])
    assert all(item["retrieval_strategy"] == retrieve.ASK_ANSWER_PACK for item in saved_items)
    assert "Private Gmail negotiation" not in pack["text"]
    assert pack["permission"]["allowed_principals"] == ["company:tenant-1", "user:founder-1"]
    assert pack["retrieval_strategy"] == retrieve.ASK_ANSWER_PACK
    assert pack["permission"] == {
        "policy_version": retrieve.POLICY_VERSION,
        "actor_user_id": "founder-1",
        "requester_user_id": "founder-1",
        "target_audience": "company",
        "destination": "ask_discuss",
        "purpose": "chat",
        "retrieval_strategy": retrieve.ASK_ANSWER_PACK,
        "allowed_principals": ["company:tenant-1", "user:founder-1"],
        "pack_policy_version": "packpolicy-v1",
    }
    assert database.executed[0][1][6] == "company"
    assert json.loads(database.executed[0][1][7]) == ["company:tenant-1", "user:founder-1"]
    assert "policy_version, actor_user_id, requester_user_id, target_audience, destination" in database.executed[0][0]
    assert database.executed[0][1][8:] == (
        retrieve.POLICY_VERSION,
        "founder-1",
        "founder-1",
        "company",
        "ask_discuss",
        "packpolicy-v1",
    )
    assert permission_checks == [{
        "tenant_id": "tenant-1",
        "actor_user_id": "founder-1",
        "target_audience": "company",
        "action": "chat.context_pack",
        "object_kind": "context_pack",
        "object_id": pack["context_pack_id"],
        "decision": "allowed",
        "reason": "audience_bound_retrieval",
        "decision_trace": {
            "policy_version": retrieve.POLICY_VERSION,
            "actor_user_id": "founder-1",
            "requester_user_id": "founder-1",
            "target_audience": "company",
            "destination": "ask_discuss",
            "purpose": "chat",
            "retrieval_strategy": retrieve.ASK_ANSWER_PACK,
            "allowed_principals": ["company:tenant-1", "user:founder-1"],
            "item_count": 1,
            "token_count": database.executed[0][1][5],
        },
    }]


# Slice 2.3: _upcoming_obligations is the forward-looking companion to
# _open_risk_objects (recency-only, ORDER BY last_seen_at DESC). Deadlines
# and calendar events must be findable by due date / start time regardless
# of when they were last touched.

@pytest.mark.asyncio
async def test_upcoming_obligations_deadline_query_is_acl_filtered_and_date_bound(monkeypatch):
    database = FakeRetrievalDb()
    monkeypatch.setattr(retrieve, "acquire", database.acquire)

    await retrieve._upcoming_obligations("tenant-1", allowed_principals=["company:tenant-1"])

    deadline_queries = [q for q, _ in database.fetches if "FROM brain_objects bo" in q]
    assert len(deadline_queries) == 1
    query = deadline_queries[0]
    assert "bo.object_type IN ('commitment', 'deadline')" in query
    assert "current_date AND current_date + INTERVAL '7 days'" in query
    assert "bo.allowed_principals ?| $3::text[]" in query
    assert query.index("bo.allowed_principals ?| $3::text[]") < query.index("ORDER BY")
    # Not recency-bound like _open_risk_objects: no last_seen_at filter/order.
    assert "ORDER BY bo.last_seen_at" not in query


@pytest.mark.asyncio
async def test_upcoming_obligations_calendar_query_excludes_recurring_events(monkeypatch):
    database = FakeRetrievalDb()
    monkeypatch.setattr(retrieve, "acquire", database.acquire)

    await retrieve._upcoming_obligations("tenant-1", allowed_principals=["company:tenant-1"])

    calendar_queries = [q for q, _ in database.fetches if "FROM content_chunks cc" in q]
    assert len(calendar_queries) == 1
    query = calendar_queries[0]
    assert "s.source_type = 'gcal'" in query
    assert "now() AND now() + INTERVAL '48 hours'" in query
    # "recurring" is set on NormalizedEvent.metadata (app/ingestion/normalizers.py),
    # not content_chunks.metadata (which only ever carries thread_key/source_url,
    # see app/workers/ingest.py) -- must read ne.metadata or the filter is a no-op.
    assert "COALESCE((ne.metadata->>'recurring')::boolean, false) = false" in query
    assert "cc.metadata->>'recurring'" not in query
    assert "cc.allowed_principals ?| $3::text[]" in query
    assert "ne.allowed_principals ?| $3::text[]" in query
    assert "re.allowed_principals ?| $3::text[]" in query


@pytest.mark.asyncio
async def test_upcoming_obligations_wired_into_brief_and_drift_but_not_chat(monkeypatch):
    database = FakeContextPackDb()
    calls = []
    stale_calls = []
    contradiction_calls = []

    async def no_matches(*args, **kwargs):
        return []

    async def record_upcoming(*args, **kwargs):
        calls.append(kwargs.get("allowed_principals"))
        return []

    async def record_stale(*args, **kwargs):
        stale_calls.append(kwargs.get("allowed_principals"))
        return []

    async def record_contradictions(*args, **kwargs):
        contradiction_calls.append(kwargs.get("allowed_principals"))
        return []

    async def record_permission_check(**kwargs):
        return "permission-check-1"

    monkeypatch.setattr(retrieve, "acquire", database.acquire)
    monkeypatch.setattr(retrieve, "_match_objects_by_name", no_matches)
    monkeypatch.setattr(retrieve, "_graph_neighbors", no_matches)
    monkeypatch.setattr(retrieve, "_load_objects", no_matches)
    monkeypatch.setattr(retrieve, "_open_risk_objects", no_matches)
    monkeypatch.setattr(retrieve, "_stale_work_objects", record_stale)
    monkeypatch.setattr(retrieve, "_load_facts", no_matches)
    monkeypatch.setattr(retrieve, "_search_alerts", no_matches)
    monkeypatch.setattr(retrieve, "_recent_chunks", no_matches)
    monkeypatch.setattr(retrieve, "_search_chunks", no_matches)
    monkeypatch.setattr(retrieve, "_upcoming_obligations", record_upcoming)
    monkeypatch.setattr(retrieve, "_recent_contradiction_facts", record_contradictions)
    monkeypatch.setattr(retrieve, "record_permission_check", record_permission_check)

    for purpose in ("brief", "drift"):
        calls.clear()
        stale_calls.clear()
        contradiction_calls.clear()
        await retrieve.build_authorized_context_pack(
            tenant_id="tenant-1",
            actor_user_id=None,
            actor_principals=["company:tenant-1"],
            target_audience="company",
            purpose=purpose,
        )
        assert calls == [["company:tenant-1"]]
        assert stale_calls == ([["company:tenant-1"]] if purpose == "brief" else [])
        assert contradiction_calls == [["company:tenant-1"]]

    calls.clear()
    stale_calls.clear()
    contradiction_calls.clear()
    await retrieve.build_authorized_context_pack(
        tenant_id="tenant-1",
        actor_user_id=None,
        actor_principals=["company:tenant-1"],
        target_audience="company",
        purpose="chat",
    )
    assert calls == []
    assert stale_calls == []
    assert contradiction_calls == []


@pytest.mark.asyncio
async def test_stale_work_query_is_acl_filtered_and_recency_bound(monkeypatch):
    database = FakeRetrievalDb()
    monkeypatch.setattr(retrieve, "acquire", database.acquire)

    await retrieve._stale_work_objects("tenant-1", allowed_principals=["company:tenant-1"])

    stale_queries = [q for q, _ in database.fetches if "FROM brain_objects bo" in q]
    assert len(stale_queries) == 1
    query = stale_queries[0]
    assert "lower(bo.status) = ANY(ARRAY['active', 'in_progress'])" in query
    assert "bo.object_type = ANY($2::text[])" in query
    assert "bo.last_seen_at < now() - INTERVAL '5 days'" in query
    assert "NOT (src.source_type = 'gcal' AND COALESCE(src.recurring, false))" in query
    assert "bo.allowed_principals ?| $4::text[]" in query
    assert query.rindex("bo.allowed_principals ?| $4::text[]") < query.rindex("ORDER BY bo.last_seen_at ASC")



@pytest.mark.asyncio
async def test_recent_contradiction_facts_filters_by_fact_kind_and_acl(monkeypatch):
    database = FakeRetrievalDb()
    monkeypatch.setattr(retrieve, "acquire", database.acquire)

    await retrieve._recent_contradiction_facts("tenant-1", allowed_principals=["company:tenant-1"])

    assert len(database.fetches) == 1
    query, args = database.fetches[0]
    assert "f.fact_kind = 'contradiction'" in query
    assert "f.acl_state = 'stamped'" in query
    assert "f.allowed_principals ?| $3::text[]" in query
    assert args[0] == "tenant-1"
    assert args[2] == ["company:tenant-1"]


@pytest.mark.asyncio
async def test_recent_contradiction_facts_omits_principal_filter_when_unscoped(monkeypatch):
    database = FakeRetrievalDb()
    monkeypatch.setattr(retrieve, "acquire", database.acquire)

    await retrieve._recent_contradiction_facts("tenant-1", allowed_principals=None)

    query, args = database.fetches[0]
    assert "allowed_principals ?|" not in query
    assert len(args) == 2


def test_pack_retrieval_strategy_distinguishes_ask_from_projection():
    assert retrieve._pack_retrieval_strategy(purpose="chat", destination=None) == retrieve.ASK_ANSWER_PACK
    assert retrieve._pack_retrieval_strategy(purpose="brief", destination="ask_discuss") == retrieve.ASK_ANSWER_PACK
    assert retrieve._pack_retrieval_strategy(purpose="brief", destination="brief") == retrieve.COVERAGE_SCAN


class FakeContextPackDb:
    def __init__(self):
        self.executed = []

    @asynccontextmanager
    async def acquire(self):
        yield self

    async def execute(self, query, *args):
        self.executed.append((query, args))


class FakeRetrievalDb:
    def __init__(self):
        self.fetches = []

    @asynccontextmanager
    async def acquire(self):
        yield self

    async def fetch(self, query, *args):
        self.fetches.append((query, args))
        return []


async def _fake_embed_text(text):
    return [0.1, 0.2, 0.3]
