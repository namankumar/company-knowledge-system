-- 0001_baseline: Inference HQ V1 company brain schema.
-- Replaces the v0 schema entirely (deployed tables held no real data).

-- ============================================================
-- Drop v0 tables
-- ============================================================
DROP TABLE IF EXISTS insight_feedback, push_log, insight_log, scan_log,
    prompt_versions, knowledge_pages, events, webhook_log, scope_routing,
    webhook_registrations, team_members, teams, companies CASCADE;

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS vector;

-- ============================================================
-- Tenant and user layer
-- ============================================================
CREATE TABLE IF NOT EXISTS tenants (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL,
    region TEXT NOT NULL DEFAULT 'us',
    plan TEXT NOT NULL DEFAULT 'pilot',
    settings JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    email TEXT NOT NULL,
    name TEXT,
    role TEXT NOT NULL DEFAULT 'founder',          -- founder | operator | member
    api_token_hash TEXT UNIQUE,                    -- sha256 of bearer token
    slack_user_id TEXT,                            -- for DM delivery + chat identity
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, email)
);

-- ============================================================
-- Source layer
-- ============================================================
CREATE TABLE IF NOT EXISTS sources (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    source_type TEXT NOT NULL,                     -- slack | gmail | gcal | transcript | linear | github
    external_id TEXT NOT NULL,                     -- workspace / account id at the provider
    composio_connection_id TEXT,
    signing_secret TEXT,
    status TEXT NOT NULL DEFAULT 'not_connected',  -- not_connected|connecting|connected|backfilling|live|degraded|expired|failed
    granted_scopes JSONB NOT NULL DEFAULT '[]',
    sync_policy JSONB NOT NULL DEFAULT '{}',       -- {mode: webhook|poll, poll_interval_minutes, gap_check_hours, freshness_hours}
    checkpoint JSONB NOT NULL DEFAULT '{}',        -- per-source cursor / page token / date window
    last_webhook_at TIMESTAMPTZ,
    last_synced_at TIMESTAMPTZ,
    last_probe_at TIMESTAMPTZ,
    stale_since TIMESTAMPTZ,
    degraded_reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, source_type, external_id)
);

CREATE TABLE IF NOT EXISTS source_sync_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    source_id UUID NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,                            -- backfill | incremental | replay | gap_check
    status TEXT NOT NULL DEFAULT 'running',        -- running | completed | failed | cancelled
    checkpoint JSONB NOT NULL DEFAULT '{}',        -- last completed checkpoint for resume
    stats JSONB NOT NULL DEFAULT '{}',             -- {fetched, ingested, duplicates}
    error TEXT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_sync_runs_source ON source_sync_runs (source_id, started_at DESC);

-- ============================================================
-- Pipeline / trace layer
-- ============================================================
CREATE TABLE IF NOT EXISTS pipeline_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,                            -- webhook | backfill | replay | daily | chat | manual
    source_id UUID REFERENCES sources(id) ON DELETE SET NULL,
    status TEXT NOT NULL DEFAULT 'running',        -- running | completed | failed
    error TEXT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_pipeline_runs_tenant ON pipeline_runs (tenant_id, started_at DESC);

CREATE TABLE IF NOT EXISTS pipeline_steps (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    run_id UUID NOT NULL REFERENCES pipeline_runs(id) ON DELETE CASCADE,
    name TEXT NOT NULL,                            -- stored_raw | normalized | triaged | resolved | extracted | merged | retrieved | synthesized | verified | delivered
    status TEXT NOT NULL DEFAULT 'running',        -- running | completed | failed | skipped
    input_ref TEXT,                                -- id of the row this step consumed
    output_ref TEXT,                               -- id of the row this step produced
    detail JSONB NOT NULL DEFAULT '{}',
    error TEXT,
    retries INT NOT NULL DEFAULT 0,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_pipeline_steps_run ON pipeline_steps (run_id, started_at);

-- ============================================================
-- Event layer
-- ============================================================
CREATE TABLE IF NOT EXISTS raw_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    source_id UUID NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    external_event_id TEXT NOT NULL,
    event_type TEXT,
    payload JSONB NOT NULL,
    occurred_at TIMESTAMPTZ,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    is_backfill BOOLEAN NOT NULL DEFAULT false,
    pipeline_run_id UUID REFERENCES pipeline_runs(id) ON DELETE SET NULL,
    UNIQUE (tenant_id, source_id, external_event_id)
);
CREATE INDEX IF NOT EXISTS idx_raw_events_tenant_time ON raw_events (tenant_id, received_at DESC);

CREATE TABLE IF NOT EXISTS normalized_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    raw_event_id UUID NOT NULL REFERENCES raw_events(id) ON DELETE CASCADE,
    source_type TEXT NOT NULL,
    actor_entity_id UUID,                          -- resolved person, when known
    actor_label TEXT,                              -- raw actor string (slack id, email)
    occurred_at TIMESTAMPTZ NOT NULL,
    text TEXT NOT NULL,
    entities JSONB NOT NULL DEFAULT '[]',          -- candidate mentions before resolution
    thread_key TEXT,                               -- groups messages in one conversation
    channel_key TEXT,                              -- slack channel, gmail label, calendar id
    source_url TEXT,
    metadata JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (raw_event_id)
);
CREATE INDEX IF NOT EXISTS idx_normalized_tenant_time ON normalized_events (tenant_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_normalized_thread ON normalized_events (tenant_id, thread_key, occurred_at);

-- ============================================================
-- Brain layer
-- ============================================================
CREATE TABLE IF NOT EXISTS entity_registry (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    entity_type TEXT NOT NULL,                     -- person | team | project | customer
    canonical_name TEXT NOT NULL,
    aliases TEXT[] NOT NULL DEFAULT '{}',
    emails TEXT[] NOT NULL DEFAULT '{}',
    handles JSONB NOT NULL DEFAULT '{}',           -- {slack: U123, github: octocat}
    external_ids JSONB NOT NULL DEFAULT '{}',
    metadata JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, entity_type, canonical_name)
);
CREATE INDEX IF NOT EXISTS idx_entity_emails ON entity_registry USING GIN (emails);
CREATE INDEX IF NOT EXISTS idx_entity_aliases ON entity_registry USING GIN (aliases);

CREATE TABLE IF NOT EXISTS brain_objects (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    object_type TEXT NOT NULL,                     -- person|team|project|initiative|customer|commitment|deadline|decision|blocker|risk|open_question
    canonical_key TEXT NOT NULL,                   -- stable dedup key within (tenant, type)
    title TEXT NOT NULL,
    state JSONB NOT NULL DEFAULT '{}',             -- structured fields per type (owner, due_date, status detail, ...)
    status TEXT NOT NULL DEFAULT 'active',         -- active | resolved | stale | archived
    confidence REAL NOT NULL DEFAULT 0.5,
    source_refs JSONB NOT NULL DEFAULT '[]',       -- [{raw_event_id, normalized_event_id, url, quote}]
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    version INT NOT NULL DEFAULT 1,
    last_trace_id UUID,                            -- pipeline_run that last touched this
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, object_type, canonical_key)
);
CREATE INDEX IF NOT EXISTS idx_brain_objects_type ON brain_objects (tenant_id, object_type, status);

CREATE TABLE IF NOT EXISTS brain_object_versions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    object_id UUID NOT NULL REFERENCES brain_objects(id) ON DELETE CASCADE,
    version INT NOT NULL,
    previous JSONB,
    updated JSONB NOT NULL,
    change_summary TEXT,
    source_refs JSONB NOT NULL DEFAULT '[]',
    model TEXT,
    prompt_version TEXT,
    trace_id UUID,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_object_versions ON brain_object_versions (object_id, version DESC);

CREATE TABLE IF NOT EXISTS brain_edges (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    from_object_id UUID NOT NULL REFERENCES brain_objects(id) ON DELETE CASCADE,
    to_object_id UUID NOT NULL REFERENCES brain_objects(id) ON DELETE CASCADE,
    edge_type TEXT NOT NULL,                       -- owns | has_deadline | depends_on | blocks | belongs_to | evidences | contradicts | decided
    confidence REAL NOT NULL DEFAULT 0.5,
    source_refs JSONB NOT NULL DEFAULT '[]',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, from_object_id, to_object_id, edge_type)
);
CREATE INDEX IF NOT EXISTS idx_edges_from ON brain_edges (from_object_id);
CREATE INDEX IF NOT EXISTS idx_edges_to ON brain_edges (to_object_id);

CREATE TABLE IF NOT EXISTS facts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    brain_object_id UUID REFERENCES brain_objects(id) ON DELETE SET NULL,
    entity_id UUID REFERENCES entity_registry(id) ON DELETE SET NULL,
    claim TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT 'observed',        -- stated | observed | inferred
    confidence REAL NOT NULL DEFAULT 0.5,
    observed_at TIMESTAMPTZ NOT NULL,
    source_refs JSONB NOT NULL DEFAULT '[]',
    normalized_event_id UUID REFERENCES normalized_events(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_facts_object ON facts (brain_object_id, observed_at DESC);

-- ============================================================
-- Retrieval layer
-- ============================================================
CREATE TABLE IF NOT EXISTS content_chunks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    normalized_event_id UUID REFERENCES normalized_events(id) ON DELETE CASCADE,
    brain_object_id UUID REFERENCES brain_objects(id) ON DELETE CASCADE,
    source_type TEXT,
    text TEXT NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}',
    embedding vector(1536),
    tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED,
    occurred_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_chunks_tsv ON content_chunks USING GIN (tsv);
CREATE INDEX IF NOT EXISTS idx_chunks_tenant_time ON content_chunks (tenant_id, occurred_at DESC);

CREATE TABLE IF NOT EXISTS context_packs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    purpose TEXT NOT NULL,                         -- chat | brief | drift
    query TEXT,
    items JSONB NOT NULL DEFAULT '[]',             -- [{kind, id, text, source_url, score}]
    token_count INT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============================================================
-- Synthesis and quality layer
-- ============================================================
CREATE TABLE IF NOT EXISTS model_calls (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID REFERENCES tenants(id) ON DELETE CASCADE,
    task TEXT NOT NULL,                            -- triage | extraction | merge | drift | brief | chat | verify
    provider TEXT NOT NULL DEFAULT 'anthropic',
    model TEXT NOT NULL,
    override_source TEXT NOT NULL DEFAULT 'default', -- default | task | tenant | global
    prompt_version TEXT,
    context_pack_id UUID REFERENCES context_packs(id) ON DELETE SET NULL,
    request_body TEXT,                             -- full prompt sent (admin provenance view)
    response_body TEXT,                            -- full raw model output
    parsed JSONB,
    validation_status TEXT,                        -- valid | invalid | unparsed
    input_tokens INT,
    output_tokens INT,
    cost_usd NUMERIC(10, 6),
    latency_ms INT,
    pipeline_run_id UUID REFERENCES pipeline_runs(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_model_calls_tenant ON model_calls (tenant_id, created_at DESC);

CREATE TABLE IF NOT EXISTS alerts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,                            -- alert | suppression | open_question
    drift_type TEXT,                               -- deadline_risk | commitment_risk | unclear_owner | repeated_blocker | plan_mismatch | progress_gap | customer_contradiction
    severity TEXT,                                 -- high | medium | low
    confidence REAL,
    claim TEXT NOT NULL,
    why_now TEXT,
    evidence JSONB NOT NULL DEFAULT '[]',          -- [{source_ref, quote}]
    missing_evidence JSONB NOT NULL DEFAULT '[]',
    recommended_action TEXT,
    related JSONB NOT NULL DEFAULT '{}',           -- {people: [], projects: [], commitments: []}
    context_pack_id UUID REFERENCES context_packs(id) ON DELETE SET NULL,
    model_call_id UUID REFERENCES model_calls(id) ON DELETE SET NULL,
    status TEXT NOT NULL DEFAULT 'pending_review', -- pending_review | approved | rejected | delivered | suppressed | dismissed
    reviewed_by TEXT,                              -- admin who approved/rejected
    reviewed_at TIMESTAMPTZ,
    review_note TEXT,
    delivered_via JSONB NOT NULL DEFAULT '[]',
    feedback TEXT,                                 -- useful | wrong | already_knew | missing_context | too_noisy | should_have_alerted_earlier
    feedback_note TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_alerts_tenant ON alerts (tenant_id, created_at DESC);

CREATE TABLE IF NOT EXISTS briefs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    brief_date DATE NOT NULL,
    content_md TEXT NOT NULL,
    sections JSONB NOT NULL DEFAULT '{}',
    context_pack_id UUID REFERENCES context_packs(id) ON DELETE SET NULL,
    model_call_id UUID REFERENCES model_calls(id) ON DELETE SET NULL,
    status TEXT NOT NULL DEFAULT 'pending_review', -- pending_review | approved | rejected | delivered
    reviewed_by TEXT,
    reviewed_at TIMESTAMPTZ,
    delivered_via JSONB NOT NULL DEFAULT '[]',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, brief_date)
);

CREATE TABLE IF NOT EXISTS chat_messages (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    channel TEXT NOT NULL DEFAULT 'api',           -- api | slack_dm | app
    role TEXT NOT NULL,                            -- user | assistant
    text TEXT NOT NULL,
    sources JSONB NOT NULL DEFAULT '[]',
    context_pack_id UUID REFERENCES context_packs(id) ON DELETE SET NULL,
    model_call_id UUID REFERENCES model_calls(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_chat_tenant_time ON chat_messages (tenant_id, created_at DESC);

CREATE TABLE IF NOT EXISTS system_issues (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID REFERENCES tenants(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,                            -- stale_source | failed_step | unsupported_claim | sync_gap | budget_exhausted
    severity TEXT NOT NULL DEFAULT 'medium',
    detail JSONB NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'open',           -- open | resolved
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at TIMESTAMPTZ
);

-- ============================================================
-- Row-level security on tenant-scoped data tables
-- ============================================================
DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'raw_events', 'normalized_events', 'entity_registry', 'facts',
        'brain_objects', 'brain_object_versions', 'brain_edges',
        'content_chunks', 'context_packs', 'alerts', 'briefs', 'chat_messages'
    ] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format(
            'DROP POLICY IF EXISTS tenant_isolation ON %I', t
        );
        EXECUTE format(
            'CREATE POLICY tenant_isolation ON %I USING (tenant_id = current_setting(''app.tenant_id'', true)::uuid)', t
        );
    END LOOP;
END $$;
