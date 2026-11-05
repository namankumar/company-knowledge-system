-- 0033_source_permission_policies: persist least-privilege source policies.

UPDATE sources
SET sync_policy = sync_policy || jsonb_build_object(
    'connector_provider', 'composio',
    'allowed_resources', jsonb_build_object(
        'slack_channel_ids', COALESCE(sync_policy->'allowed_resources'->'slack_channel_ids', '[]'::jsonb),
        'slack_channel_types', '["public_channel","private_channel","mpim"]'::jsonb
    ),
    'denied_resource_classes', '["slack_1_1_dm"]'::jsonb,
    'permission_summary', 'Reads selected public/private Slack channels and group messages. Does not read or write 1-1 DMs.',
    'last_permission_verification_at', now()
)
WHERE source_type = 'slack';

UPDATE sources
SET granted_scopes = COALESCE(granted_scopes, '[]'::jsonb) - 'im:read' - 'im:history' - 'im:write',
    sync_policy = sync_policy || jsonb_build_object(
        'permission_policy',
        jsonb_build_object(
            'connector_provider', 'composio',
            'allowed_scopes', '["channels:read","channels:history","groups:read","groups:history","users:read","users:read.email","team:read","reactions:read","files:read","mpim:read","mpim:history","mpim:write"]'::jsonb,
            'denied_scopes', '["im:read","im:history","im:write"]'::jsonb,
            'allowed_resources', jsonb_build_object(
                'slack_channel_ids', COALESCE(sync_policy->'allowed_resources'->'slack_channel_ids', '[]'::jsonb),
                'slack_channel_types', '["public_channel","private_channel","mpim"]'::jsonb
            ),
            'denied_resource_classes', '["slack_1_1_dm"]'::jsonb,
            'permission_summary', 'Reads selected public/private Slack channels and group messages. Does not read or write 1-1 DMs.'
        )
    )
WHERE source_type = 'slack';

UPDATE sources
SET sync_policy = sync_policy || jsonb_build_object(
    'github_access_mode', 'pr_metadata',
    'connector_provider', 'github_app',
    'allowed_resources', jsonb_build_object(
        'github_repositories', COALESCE(sync_policy->'allowed_resources'->'github_repositories', '[]'::jsonb)
    ),
    'denied_resource_classes', '["github_source_code","github_diffs","github_file_contents","github_workflows"]'::jsonb,
    'permission_summary', 'Reads GitHub PR metadata only: title, author, status, reviews, labels, timestamps, and URLs. Does not read source code, diffs, patches, file contents, secrets, or workflow files.',
    'last_permission_verification_at', now(),
    'permission_policy',
    jsonb_build_object(
        'connector_provider', 'github_app',
        'allowed_scopes', '["pull_requests:read","issues:read","metadata:read"]'::jsonb,
        'denied_scopes', '["repo","contents:read","contents:write","actions:read","workflows:read"]'::jsonb,
        'allowed_resources', jsonb_build_object(
            'github_repositories', COALESCE(sync_policy->'allowed_resources'->'github_repositories', '[]'::jsonb)
        ),
        'denied_resource_classes', '["github_source_code","github_diffs","github_file_contents","github_workflows"]'::jsonb,
        'permission_summary', 'Reads GitHub PR metadata only: title, author, status, reviews, labels, timestamps, and URLs. Does not read source code, diffs, patches, file contents, secrets, or workflow files.'
    )
)
WHERE source_type = 'github';

UPDATE sources
SET sync_policy = sync_policy || jsonb_build_object(
    'document_scope', CASE
        WHEN sync_policy ? 'document_query' THEN COALESCE(sync_policy->>'document_scope', 'configured_document_query')
        ELSE 'explicitly_shared_documents'
    END,
    'connector_provider', 'composio',
    'allowed_resources', jsonb_build_object(
        'google_document_ids', COALESCE(sync_policy->'allowed_resources'->'google_document_ids', '[]'::jsonb),
        'google_folder_ids', COALESCE(sync_policy->'allowed_resources'->'google_folder_ids', '[]'::jsonb),
        'document_query', sync_policy->>'document_query',
        'document_name', sync_policy->>'document_name'
    ),
    'denied_resource_classes', '["unshared_google_docs","unselected_google_drive"]'::jsonb,
    'permission_summary', 'Reads Google Docs that are explicitly shared, selected by document/folder, or matched by the configured pilot filter.',
    'last_permission_verification_at', now(),
    'permission_policy',
    jsonb_build_object(
        'connector_provider', 'composio',
        'allowed_scopes', '["documents.readonly","drive.file"]'::jsonb,
        'denied_scopes', '["drive","drive.readonly"]'::jsonb,
        'allowed_resources', jsonb_build_object(
            'google_document_ids', COALESCE(sync_policy->'allowed_resources'->'google_document_ids', '[]'::jsonb),
            'google_folder_ids', COALESCE(sync_policy->'allowed_resources'->'google_folder_ids', '[]'::jsonb),
            'document_query', sync_policy->>'document_query',
            'document_name', sync_policy->>'document_name'
        ),
        'denied_resource_classes', '["unshared_google_docs","unselected_google_drive"]'::jsonb,
        'permission_summary', 'Reads Google Docs that are explicitly shared, selected by document/folder, or matched by the configured pilot filter.'
    )
)
WHERE source_type = 'googledocs';

UPDATE sources
SET sync_policy = sync_policy || jsonb_build_object(
    'connector_provider', 'composio',
    'allowed_resources', jsonb_build_object(
        'linear_team_ids', COALESCE(sync_policy->'allowed_resources'->'linear_team_ids', '[]'::jsonb),
        'linear_project_ids', COALESCE(sync_policy->'allowed_resources'->'linear_project_ids', '[]'::jsonb)
    ),
    'denied_resource_classes', '["linear_writes"]'::jsonb,
    'permission_summary', 'Reads Linear work items, projects, owners, labels, and status. Does not write to Linear.',
    'last_permission_verification_at', now(),
    'permission_policy',
    jsonb_build_object(
        'connector_provider', 'composio',
        'allowed_scopes', '["read"]'::jsonb,
        'denied_scopes', '["write"]'::jsonb,
        'allowed_resources', jsonb_build_object(
            'linear_team_ids', COALESCE(sync_policy->'allowed_resources'->'linear_team_ids', '[]'::jsonb),
            'linear_project_ids', COALESCE(sync_policy->'allowed_resources'->'linear_project_ids', '[]'::jsonb)
        ),
        'denied_resource_classes', '["linear_writes"]'::jsonb,
        'permission_summary', 'Reads Linear work items, projects, owners, labels, and status. Does not write to Linear.'
    )
)
WHERE source_type = 'linear';
