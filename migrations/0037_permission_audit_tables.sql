-- 0037_permission_audit_tables: durable permission checks and incidents.

CREATE TABLE IF NOT EXISTS permission_checks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    actor_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    target_audience TEXT NOT NULL,
    action TEXT NOT NULL,
    object_kind TEXT NOT NULL,
    object_id UUID,
    source_resource_id UUID REFERENCES source_resources(id) ON DELETE SET NULL,
    decision TEXT NOT NULL,
    reason TEXT,
    policy_version TEXT NOT NULL,
    decision_trace JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_permission_checks_tenant_created
    ON permission_checks (tenant_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_permission_checks_decision
    ON permission_checks (tenant_id, decision, action);

CREATE TABLE IF NOT EXISTS permission_incidents (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    reported_by UUID REFERENCES users(id) ON DELETE SET NULL,
    incident_type TEXT NOT NULL,
    object_kind TEXT NOT NULL,
    object_id UUID,
    source_resource_id UUID REFERENCES source_resources(id) ON DELETE SET NULL,
    target_audience TEXT,
    expected_decision TEXT NOT NULL,
    actual_decision TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    adapter_version TEXT,
    decision_trace JSONB NOT NULL DEFAULT '{}',
    safe_debug_context JSONB NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'open',
    root_cause TEXT,
    resolution TEXT,
    resolved_by UUID REFERENCES users(id) ON DELETE SET NULL,
    resolved_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_permission_incidents_open
    ON permission_incidents (tenant_id, status, incident_type, created_at DESC);

GRANT SELECT, INSERT, UPDATE, DELETE ON permission_checks TO inferencehq_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON permission_incidents TO inferencehq_app;

ALTER TABLE permission_checks ENABLE ROW LEVEL SECURITY;
ALTER TABLE permission_checks FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON permission_checks;
CREATE POLICY tenant_isolation ON permission_checks USING (
    tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
    OR current_setting('app.bypass_rls', true) = 'on'
) WITH CHECK (
    tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
    OR current_setting('app.bypass_rls', true) = 'on'
);

ALTER TABLE permission_incidents ENABLE ROW LEVEL SECURITY;
ALTER TABLE permission_incidents FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON permission_incidents;
CREATE POLICY tenant_isolation ON permission_incidents USING (
    tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
    OR current_setting('app.bypass_rls', true) = 'on'
) WITH CHECK (
    tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
    OR current_setting('app.bypass_rls', true) = 'on'
);
