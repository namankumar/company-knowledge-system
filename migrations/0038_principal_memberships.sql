-- 0038_principal_memberships: effective Alloy principal memberships for actors.

CREATE TABLE IF NOT EXISTS principal_memberships (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    member_principal_key TEXT NOT NULL,
    principal_key TEXT NOT NULL,
    principal_type TEXT NOT NULL,
    source_type TEXT,
    provider_principal_type TEXT,
    provider_principal_id TEXT,
    basis TEXT NOT NULL DEFAULT 'principal_identity_mapping',
    confidence TEXT NOT NULL DEFAULT 'medium',
    membership_state TEXT NOT NULL DEFAULT 'active',
    raw_provider_membership JSONB NOT NULL DEFAULT '{}',
    last_seen_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_principal_memberships_unique
    ON principal_memberships (
        tenant_id,
        member_principal_key,
        principal_key,
        COALESCE(source_type, ''),
        COALESCE(provider_principal_type, ''),
        COALESCE(provider_principal_id, '')
    );

CREATE INDEX IF NOT EXISTS idx_principal_memberships_member
    ON principal_memberships (tenant_id, member_principal_key, membership_state, confidence);

CREATE INDEX IF NOT EXISTS idx_principal_memberships_principal
    ON principal_memberships (tenant_id, principal_key, membership_state);

GRANT SELECT, INSERT, UPDATE, DELETE ON principal_memberships TO inferencehq_app;
ALTER TABLE principal_memberships ENABLE ROW LEVEL SECURITY;
ALTER TABLE principal_memberships FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON principal_memberships;
CREATE POLICY tenant_isolation ON principal_memberships USING (
    tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
    OR current_setting('app.bypass_rls', true) = 'on'
) WITH CHECK (
    tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
    OR current_setting('app.bypass_rls', true) = 'on'
);
