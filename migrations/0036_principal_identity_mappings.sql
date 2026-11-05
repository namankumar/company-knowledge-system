-- 0036_principal_identity_mappings: map provider identities into Alloy principals.

CREATE TABLE IF NOT EXISTS principal_identity_mappings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    source_type TEXT NOT NULL,
    provider_principal_type TEXT NOT NULL,
    provider_principal_id TEXT NOT NULL,
    provider_principal_display TEXT,
    alloy_principal_type TEXT NOT NULL,
    alloy_principal_id UUID,
    alloy_principal_key TEXT,
    mapping_state TEXT NOT NULL DEFAULT 'unmapped',
    confidence TEXT NOT NULL DEFAULT 'low',
    last_seen_at TIMESTAMPTZ,
    last_resolved_at TIMESTAMPTZ,
    raw_provider_identity JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, source_type, provider_principal_type, provider_principal_id)
);

CREATE INDEX IF NOT EXISTS idx_principal_identity_mappings_lookup
    ON principal_identity_mappings (tenant_id, source_type, provider_principal_type, provider_principal_id);

CREATE INDEX IF NOT EXISTS idx_principal_identity_mappings_state
    ON principal_identity_mappings (tenant_id, mapping_state, confidence);

GRANT SELECT, INSERT, UPDATE, DELETE ON principal_identity_mappings TO inferencehq_app;
ALTER TABLE principal_identity_mappings ENABLE ROW LEVEL SECURITY;
ALTER TABLE principal_identity_mappings FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON principal_identity_mappings;
CREATE POLICY tenant_isolation ON principal_identity_mappings USING (
    tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
    OR current_setting('app.bypass_rls', true) = 'on'
) WITH CHECK (
    tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
    OR current_setting('app.bypass_rls', true) = 'on'
);
