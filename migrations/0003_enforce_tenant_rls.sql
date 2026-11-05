-- Enforce tenant isolation for every table carrying tenant data.
-- Runtime admin/auth/health paths opt into app.bypass_rls explicitly.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'inferencehq_app') THEN
        CREATE ROLE inferencehq_app NOLOGIN NOSUPERUSER NOBYPASSRLS;
    END IF;
END $$;

GRANT USAGE ON SCHEMA public TO inferencehq_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO inferencehq_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO inferencehq_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO inferencehq_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO inferencehq_app;

DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'users', 'sources', 'source_sync_runs', 'pipeline_runs', 'pipeline_steps',
        'raw_events', 'normalized_events', 'entity_registry', 'facts',
        'brain_objects', 'brain_object_versions', 'brain_edges',
        'content_chunks', 'context_packs', 'model_calls', 'alerts', 'briefs',
        'chat_messages', 'system_issues'
    ] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I', t);
        EXECUTE format(
            'CREATE POLICY tenant_isolation ON %I USING (
                tenant_id = NULLIF(current_setting(''app.tenant_id'', true), '''')::uuid
                OR current_setting(''app.bypass_rls'', true) = ''on''
            ) WITH CHECK (
                tenant_id = NULLIF(current_setting(''app.tenant_id'', true), '''')::uuid
                OR current_setting(''app.bypass_rls'', true) = ''on''
            )',
            t
        );
    END LOOP;
END $$;
