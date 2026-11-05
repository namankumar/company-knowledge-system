-- 0035_acl_envelope_fail_closed: add ACL envelopes and default legacy data to restricted.

CREATE TABLE IF NOT EXISTS source_resources (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    source_id UUID NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    source_type TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    external_id TEXT NOT NULL,
    name TEXT,
    parent_resource_id UUID REFERENCES source_resources(id) ON DELETE SET NULL,
    owner_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    acl_basis TEXT NOT NULL DEFAULT 'legacy_missing_acl',
    acl_confidence TEXT NOT NULL DEFAULT 'low',
    visibility TEXT NOT NULL DEFAULT 'restricted',
    allowed_principals JSONB NOT NULL DEFAULT '[]',
    provider_acl JSONB NOT NULL DEFAULT '{}',
    acl_state TEXT NOT NULL DEFAULT 'legacy_unstamped',
    acl_freshness_state TEXT NOT NULL DEFAULT 'needs_permission_review',
    last_acl_synced_at TIMESTAMPTZ,
    access_revoked_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, source_id, resource_type, external_id)
);

CREATE INDEX IF NOT EXISTS idx_source_resources_tenant_acl
    ON source_resources (tenant_id, source_type, visibility, acl_state, acl_freshness_state);

CREATE INDEX IF NOT EXISTS idx_source_resources_parent
    ON source_resources (parent_resource_id);

ALTER TABLE raw_events
    ADD COLUMN IF NOT EXISTS source_resource_id UUID REFERENCES source_resources(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS visibility TEXT NOT NULL DEFAULT 'restricted',
    ADD COLUMN IF NOT EXISTS acl_basis TEXT NOT NULL DEFAULT 'legacy_missing_acl',
    ADD COLUMN IF NOT EXISTS acl_confidence TEXT NOT NULL DEFAULT 'low',
    ADD COLUMN IF NOT EXISTS allowed_principals JSONB NOT NULL DEFAULT '[]',
    ADD COLUMN IF NOT EXISTS acl_state TEXT NOT NULL DEFAULT 'legacy_unstamped',
    ADD COLUMN IF NOT EXISTS acl_freshness_state TEXT NOT NULL DEFAULT 'needs_permission_review';

ALTER TABLE normalized_events
    ADD COLUMN IF NOT EXISTS source_resource_id UUID REFERENCES source_resources(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS visibility TEXT NOT NULL DEFAULT 'restricted',
    ADD COLUMN IF NOT EXISTS acl_basis TEXT NOT NULL DEFAULT 'legacy_missing_acl',
    ADD COLUMN IF NOT EXISTS acl_confidence TEXT NOT NULL DEFAULT 'low',
    ADD COLUMN IF NOT EXISTS allowed_principals JSONB NOT NULL DEFAULT '[]',
    ADD COLUMN IF NOT EXISTS acl_state TEXT NOT NULL DEFAULT 'legacy_unstamped',
    ADD COLUMN IF NOT EXISTS acl_freshness_state TEXT NOT NULL DEFAULT 'needs_permission_review';

ALTER TABLE content_chunks
    ADD COLUMN IF NOT EXISTS source_resource_id UUID REFERENCES source_resources(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS visibility TEXT NOT NULL DEFAULT 'restricted',
    ADD COLUMN IF NOT EXISTS acl_basis TEXT NOT NULL DEFAULT 'legacy_missing_acl',
    ADD COLUMN IF NOT EXISTS acl_confidence TEXT NOT NULL DEFAULT 'low',
    ADD COLUMN IF NOT EXISTS allowed_principals JSONB NOT NULL DEFAULT '[]',
    ADD COLUMN IF NOT EXISTS acl_state TEXT NOT NULL DEFAULT 'legacy_unstamped',
    ADD COLUMN IF NOT EXISTS acl_freshness_state TEXT NOT NULL DEFAULT 'needs_permission_review';

ALTER TABLE facts
    ADD COLUMN IF NOT EXISTS source_resource_id UUID REFERENCES source_resources(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS visibility TEXT NOT NULL DEFAULT 'restricted',
    ADD COLUMN IF NOT EXISTS acl_basis TEXT NOT NULL DEFAULT 'legacy_missing_acl',
    ADD COLUMN IF NOT EXISTS acl_confidence TEXT NOT NULL DEFAULT 'low',
    ADD COLUMN IF NOT EXISTS allowed_principals JSONB NOT NULL DEFAULT '[]',
    ADD COLUMN IF NOT EXISTS acl_state TEXT NOT NULL DEFAULT 'legacy_unstamped',
    ADD COLUMN IF NOT EXISTS acl_freshness_state TEXT NOT NULL DEFAULT 'needs_permission_review';

ALTER TABLE brain_objects
    ADD COLUMN IF NOT EXISTS source_resource_id UUID REFERENCES source_resources(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS visibility TEXT NOT NULL DEFAULT 'restricted',
    ADD COLUMN IF NOT EXISTS acl_basis TEXT NOT NULL DEFAULT 'legacy_missing_acl',
    ADD COLUMN IF NOT EXISTS acl_confidence TEXT NOT NULL DEFAULT 'low',
    ADD COLUMN IF NOT EXISTS allowed_principals JSONB NOT NULL DEFAULT '[]',
    ADD COLUMN IF NOT EXISTS acl_state TEXT NOT NULL DEFAULT 'legacy_unstamped',
    ADD COLUMN IF NOT EXISTS acl_freshness_state TEXT NOT NULL DEFAULT 'needs_permission_review';

ALTER TABLE brain_edges
    ADD COLUMN IF NOT EXISTS source_resource_id UUID REFERENCES source_resources(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS visibility TEXT NOT NULL DEFAULT 'restricted',
    ADD COLUMN IF NOT EXISTS acl_basis TEXT NOT NULL DEFAULT 'legacy_missing_acl',
    ADD COLUMN IF NOT EXISTS acl_confidence TEXT NOT NULL DEFAULT 'low',
    ADD COLUMN IF NOT EXISTS allowed_principals JSONB NOT NULL DEFAULT '[]',
    ADD COLUMN IF NOT EXISTS acl_state TEXT NOT NULL DEFAULT 'legacy_unstamped',
    ADD COLUMN IF NOT EXISTS acl_freshness_state TEXT NOT NULL DEFAULT 'needs_permission_review';

ALTER TABLE context_packs
    ADD COLUMN IF NOT EXISTS visibility TEXT NOT NULL DEFAULT 'restricted',
    ADD COLUMN IF NOT EXISTS acl_basis TEXT NOT NULL DEFAULT 'legacy_missing_acl',
    ADD COLUMN IF NOT EXISTS acl_confidence TEXT NOT NULL DEFAULT 'low',
    ADD COLUMN IF NOT EXISTS allowed_principals JSONB NOT NULL DEFAULT '[]',
    ADD COLUMN IF NOT EXISTS acl_state TEXT NOT NULL DEFAULT 'legacy_unstamped',
    ADD COLUMN IF NOT EXISTS acl_freshness_state TEXT NOT NULL DEFAULT 'needs_permission_review';

ALTER TABLE alerts
    ADD COLUMN IF NOT EXISTS visibility TEXT NOT NULL DEFAULT 'restricted',
    ADD COLUMN IF NOT EXISTS acl_basis TEXT NOT NULL DEFAULT 'legacy_missing_acl',
    ADD COLUMN IF NOT EXISTS acl_confidence TEXT NOT NULL DEFAULT 'low',
    ADD COLUMN IF NOT EXISTS allowed_principals JSONB NOT NULL DEFAULT '[]',
    ADD COLUMN IF NOT EXISTS acl_state TEXT NOT NULL DEFAULT 'legacy_unstamped',
    ADD COLUMN IF NOT EXISTS acl_freshness_state TEXT NOT NULL DEFAULT 'needs_permission_review';

ALTER TABLE briefs
    ADD COLUMN IF NOT EXISTS visibility TEXT NOT NULL DEFAULT 'restricted',
    ADD COLUMN IF NOT EXISTS acl_basis TEXT NOT NULL DEFAULT 'legacy_missing_acl',
    ADD COLUMN IF NOT EXISTS acl_confidence TEXT NOT NULL DEFAULT 'low',
    ADD COLUMN IF NOT EXISTS allowed_principals JSONB NOT NULL DEFAULT '[]',
    ADD COLUMN IF NOT EXISTS acl_state TEXT NOT NULL DEFAULT 'legacy_unstamped',
    ADD COLUMN IF NOT EXISTS acl_freshness_state TEXT NOT NULL DEFAULT 'needs_permission_review';

ALTER TABLE chat_messages
    ADD COLUMN IF NOT EXISTS visibility TEXT NOT NULL DEFAULT 'restricted',
    ADD COLUMN IF NOT EXISTS acl_basis TEXT NOT NULL DEFAULT 'legacy_missing_acl',
    ADD COLUMN IF NOT EXISTS acl_confidence TEXT NOT NULL DEFAULT 'low',
    ADD COLUMN IF NOT EXISTS allowed_principals JSONB NOT NULL DEFAULT '[]',
    ADD COLUMN IF NOT EXISTS acl_state TEXT NOT NULL DEFAULT 'legacy_unstamped',
    ADD COLUMN IF NOT EXISTS acl_freshness_state TEXT NOT NULL DEFAULT 'needs_permission_review';

CREATE INDEX IF NOT EXISTS idx_raw_events_acl
    ON raw_events (tenant_id, acl_state, acl_freshness_state, visibility);
CREATE INDEX IF NOT EXISTS idx_normalized_events_acl
    ON normalized_events (tenant_id, acl_state, acl_freshness_state, visibility);
CREATE INDEX IF NOT EXISTS idx_content_chunks_acl
    ON content_chunks (tenant_id, acl_state, acl_freshness_state, visibility);
CREATE INDEX IF NOT EXISTS idx_facts_acl
    ON facts (tenant_id, acl_state, acl_freshness_state, visibility);
CREATE INDEX IF NOT EXISTS idx_brain_objects_acl
    ON brain_objects (tenant_id, acl_state, acl_freshness_state, visibility);
CREATE INDEX IF NOT EXISTS idx_alerts_acl
    ON alerts (tenant_id, acl_state, acl_freshness_state, visibility);

GRANT SELECT, INSERT, UPDATE, DELETE ON source_resources TO inferencehq_app;
ALTER TABLE source_resources ENABLE ROW LEVEL SECURITY;
ALTER TABLE source_resources FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON source_resources;
CREATE POLICY tenant_isolation ON source_resources USING (
    tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
    OR current_setting('app.bypass_rls', true) = 'on'
) WITH CHECK (
    tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
    OR current_setting('app.bypass_rls', true) = 'on'
);
