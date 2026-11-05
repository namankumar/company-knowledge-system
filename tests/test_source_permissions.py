import pytest

from app.core.source_permissions import (
    PermissionDenied,
    SLACK_ALLOWED_SCOPES,
    SLACK_DENIED_SCOPES,
    SLACK_GROUP_DM_SCOPES,
    SLACK_ONE_TO_ONE_DM_READ_SCOPES,
    assert_granted_scopes_allowed,
    assert_tool_allowed,
    default_permission_policy,
    sanitize_github_pull_request,
    sanitize_source_payload,
    denied_payload_reason,
    strict_read_only_required,
)
from app.core.github_app import validate_installation_permissions
from app.api.github_app_webhook import verify_github_signature


def test_exampleco_slack_policy_excludes_one_to_one_dm_scopes():
    policy = default_permission_policy("slack", {"tenant_name": "ExampleCo"})

    assert "channels:history" in SLACK_ALLOWED_SCOPES
    assert "groups:history" in SLACK_ALLOWED_SCOPES
    assert "im:read" in SLACK_DENIED_SCOPES
    assert "im:write" in SLACK_DENIED_SCOPES
    assert "mpim:history" in SLACK_DENIED_SCOPES
    assert not (set(SLACK_GROUP_DM_SCOPES) & set(policy["allowed_scopes"]))
    assert not (set(policy["allowed_scopes"]) & set(policy["denied_scopes"]))


def test_exampleco_ai_tenant_name_is_strict_no_dm():
    assert strict_read_only_required({"tenant_name": "ExampleCo AI"}) is True
    policy = default_permission_policy("slack", {"tenant_name": "ExampleCo AI"})

    assert policy["strict_read_only"] is True
    assert "im:read" not in policy["allowed_scopes"]
    assert "im:history" not in policy["allowed_scopes"]
    assert "mpim:history" not in policy["allowed_scopes"]
    assert "slack_direct_messages" in policy["denied_resource_classes"]
    assert "im" not in policy["allowed_resources"]["slack_channel_types"]
    assert "mpim" not in policy["allowed_resources"]["slack_channel_types"]


def test_non_exampleco_slack_policy_allows_one_to_one_dm_read_scopes():
    policy = default_permission_policy("slack", {"tenant_name": "InferenceHQ Demo"})

    assert set(SLACK_ONE_TO_ONE_DM_READ_SCOPES) <= set(policy["allowed_scopes"])
    assert set(SLACK_GROUP_DM_SCOPES) <= set(policy["allowed_scopes"])
    assert "im:write" in policy["denied_scopes"]
    assert "slack_1_1_dm_writes" in policy["denied_resource_classes"]
    assert "slack_direct_messages" not in policy["denied_resource_classes"]
    assert_granted_scopes_allowed("slack", ["channels:read", "im:read", "im:history"], policy)


def test_granted_slack_dm_scope_is_rejected_for_exampleco():
    policy = default_permission_policy("slack", {"tenant_name": "ExampleCo"})

    with pytest.raises(PermissionDenied, match="im:history"):
        assert_granted_scopes_allowed("slack", ["channels:read", "im:history"], policy)


def test_exampleco_slack_runtime_guard_blocks_dm_channel_ids():
    source = {"source_type": "slack", "sync_policy": {"permission_policy": default_permission_policy("slack", {"tenant_name": "ExampleCo"})}}

    with pytest.raises(PermissionDenied, match="1-1 DM"):
        assert_tool_allowed(source, "SLACK_FETCH_CONVERSATION_HISTORY", {"channel": "D123"})
    with pytest.raises(PermissionDenied, match="group DM"):
        assert_tool_allowed(source, "SLACK_LIST_ALL_CHANNELS", {"types": "public_channel,private_channel,mpim"})


def test_non_exampleco_slack_runtime_guard_allows_dm_reads_but_not_writes():
    source = {"source_type": "slack", "sync_policy": {"permission_policy": default_permission_policy("slack", {"tenant_name": "InferenceHQ Demo"})}}

    assert_tool_allowed(source, "SLACK_FETCH_CONVERSATION_HISTORY", {"channel": "D123"})
    with pytest.raises(PermissionDenied, match="1-1 DM writes"):
        assert_tool_allowed(source, "SLACK_SEND_MESSAGE", {"channel": "D123"})


def test_slack_runtime_guard_enforces_channel_allowlist():
    policy = default_permission_policy("slack", {"slack_channel_ids": ["C_ALLOWED"]})
    source = {"source_type": "slack", "sync_policy": {"permission_policy": policy}}

    assert_tool_allowed(source, "SLACK_FETCH_CONVERSATION_HISTORY", {"channel": "C_ALLOWED"})
    with pytest.raises(PermissionDenied, match="not allowlisted"):
        assert_tool_allowed(source, "SLACK_FETCH_CONVERSATION_HISTORY", {"channel": "C_OTHER"})


def test_github_runtime_guard_allows_only_minimal_pr_metadata_tool():
    source = {"source_type": "github", "sync_policy": {"permission_policy": default_permission_policy("github")}}

    assert_tool_allowed(source, "GITHUB_FIND_PULL_REQUESTS", {"response_detail": "minimal"})
    with pytest.raises(PermissionDenied, match="outside PR metadata"):
        assert_tool_allowed(source, "GITHUB_GET_FILE_CONTENTS", {"owner": "exampleco", "repo": "backend"})
    with pytest.raises(PermissionDenied, match="response_detail=minimal"):
        assert_tool_allowed(source, "GITHUB_FIND_PULL_REQUESTS", {"response_detail": "full"})


def test_github_sanitizer_drops_code_diff_patch_and_body_fields():
    sanitized = sanitize_github_pull_request({
        "id": 42,
        "number": 17,
        "title": "Add health scoring",
        "body": "Implementation details should not be ingested.",
        "diff_url": "https://github.com/acme/repo/pull/17.diff",
        "patch_url": "https://github.com/acme/repo/pull/17.patch",
        "files": [{"filename": "app.py", "patch": "+secret"}],
        "additions": 10,
        "deletions": 2,
        "changed_files": 1,
    })

    assert sanitized == {
        "id": 42,
        "number": 17,
        "title": "Add health scoring",
        "additions": 10,
        "deletions": 2,
        "changed_files": 1,
    }


def test_github_source_payload_sanitizer_drops_denied_fields_recursively():
    payload = sanitize_source_payload("github", {
        "pull_request": {
            "title": "Safe title",
            "body": "unsafe",
            "files": [{"filename": "app.py", "patch": "+secret"}],
        },
        "repository": {"full_name": "exampleco/backend"},
    })

    assert payload == {
        "pull_request": {"title": "Safe title"},
        "repository": {"full_name": "exampleco/backend"},
    }


def test_exampleco_slack_dm_payload_is_denied_even_if_provider_sends_it():
    policy = default_permission_policy("slack", {"tenant_name": "ExampleCo"})

    assert denied_payload_reason("slack", {"event": {"channel": "D123", "text": "private"}}, policy) == "Slack 1-1 DM event is denied"
    assert denied_payload_reason("slack", {"event": {"channel": "G123", "channel_type": "mpim", "text": "group dm"}}, policy) == "Slack group DM event is denied"
    assert denied_payload_reason("slack", {"event": {"channel": "C123", "text": "public"}}) is None


def test_non_exampleco_slack_dm_payload_is_allowed_by_source_policy():
    policy = default_permission_policy("slack", {"tenant_name": "InferenceHQ Demo"})

    assert denied_payload_reason("slack", {"event": {"channel": "D123", "text": "private"}}, policy) is None


def test_gmail_policy_records_broad_provider_consent_runtime_guard():
    policy = default_permission_policy("gmail")

    assert "gmail_sends" in policy["denied_resource_classes"]
    assert policy["provider_consent_warning"] == "managed_oauth_may_grant_broader_gmail_access_than_runtime_allows"


def test_gmail_runtime_guard_allows_only_fetch():
    source = {"source_type": "gmail", "sync_policy": {"permission_policy": default_permission_policy("gmail")}}

    assert_tool_allowed(source, "GMAIL_FETCH_EMAILS", {"max_results": 5})
    with pytest.raises(PermissionDenied, match="read-only email fetch"):
        assert_tool_allowed(source, "GMAIL_SEND_EMAIL", {"to": "maya@example.com"})
    with pytest.raises(PermissionDenied, match="read-only email fetch"):
        assert_tool_allowed(source, "GMAIL_DELETE_EMAIL", {"message_id": "msg-1"})


def test_gmail_runtime_guard_enforces_configured_query_scope():
    source = {
        "source_type": "gmail",
        "sync_policy": {
            "allowed_resources": {"gmail_query": "from:customer@example.com"},
        },
    }

    assert_tool_allowed(
        source,
        "GMAIL_FETCH_EMAILS",
        {"query": "newer_than:14d -in:spam (from:customer@example.com)", "max_results": 5},
    )
    with pytest.raises(PermissionDenied, match="configured Gmail query scope"):
        assert_tool_allowed(source, "GMAIL_FETCH_EMAILS", {"query": "newer_than:14d", "max_results": 5})


def test_googledocs_runtime_guard_allows_only_search_and_plaintext_read():
    source = {"source_type": "googledocs", "sync_policy": {"permission_policy": default_permission_policy("googledocs")}}

    assert_tool_allowed(source, "GOOGLEDOCS_SEARCH_DOCUMENTS", {"query": "name contains 'roadmap'"})
    assert_tool_allowed(source, "GOOGLEDOCS_GET_DOCUMENT_PLAINTEXT", {"document_id": "doc-1"})
    with pytest.raises(PermissionDenied, match="read-only document access"):
        assert_tool_allowed(source, "GOOGLEDOCS_BATCH_UPDATE_DOCUMENT", {"document_id": "doc-1"})


def test_googledocs_runtime_guard_enforces_configured_document_scope():
    policy = default_permission_policy(
        "googledocs",
        {
            "document_query": "name contains 'ExampleCo Q3'",
            "google_document_ids": ["doc-q3-goals"],
            "google_folder_ids": ["folder-q3-okrs"],
        },
    )
    source = {"source_type": "googledocs", "sync_policy": {"permission_policy": policy}}

    assert_tool_allowed(
        source,
        "GOOGLEDOCS_SEARCH_DOCUMENTS",
        {"query": "(name contains 'ExampleCo Q3') and ('folder-q3-okrs' in parents) and id = 'doc-q3-goals'"},
    )
    assert_tool_allowed(source, "GOOGLEDOCS_GET_DOCUMENT_PLAINTEXT", {"document_id": "doc-q3-goals"})
    with pytest.raises(PermissionDenied, match="configured document scope"):
        assert_tool_allowed(source, "GOOGLEDOCS_SEARCH_DOCUMENTS", {"query": "name contains 'roadmap'"})
    with pytest.raises(PermissionDenied, match="not allowlisted"):
        assert_tool_allowed(source, "GOOGLEDOCS_GET_DOCUMENT_PLAINTEXT", {"document_id": "doc-other"})


def test_googledrive_runtime_guard_allows_only_file_metadata_find():
    source = {"source_type": "googledrive", "sync_policy": {"permission_policy": default_permission_policy("googledrive")}}

    assert_tool_allowed(source, "GOOGLEDRIVE_FIND_FILE", {"query": "trashed = false"})
    with pytest.raises(PermissionDenied, match="read-only file metadata"):
        assert_tool_allowed(source, "GOOGLEDRIVE_EXPORT_FILE", {"file_id": "file-1"})
    with pytest.raises(PermissionDenied, match="read-only file metadata"):
        assert_tool_allowed(source, "GOOGLEDRIVE_CREATE_PERMISSION", {"file_id": "file-1"})


def test_googledrive_runtime_guard_enforces_configured_resource_scope():
    source = {
        "source_type": "googledrive",
        "sync_policy": {
            "allowed_resources": {
                "google_file_ids": ["file-q3-goals-pdf"],
                "google_folder_ids": ["folder-q3-okrs"],
            },
        },
    }

    assert_tool_allowed(
        source,
        "GOOGLEDRIVE_FIND_FILE",
        {"query": "trashed = false and (id = 'file-q3-goals-pdf' or 'folder-q3-okrs' in parents)"},
    )
    with pytest.raises(PermissionDenied, match="configured resource scope"):
        assert_tool_allowed(source, "GOOGLEDRIVE_FIND_FILE", {"query": "trashed = false"})


def test_google_calendar_runtime_guard_allows_only_event_listing():
    source = {"source_type": "gcal", "sync_policy": {"permission_policy": default_permission_policy("gcal")}}

    assert_tool_allowed(source, "GOOGLECALENDAR_EVENTS_LIST", {"calendar_id": "primary"})
    with pytest.raises(PermissionDenied, match="read-only event access"):
        assert_tool_allowed(source, "GOOGLECALENDAR_CREATE_EVENT", {"calendar_id": "primary"})
    with pytest.raises(PermissionDenied, match="read-only event access"):
        assert_tool_allowed(source, "GOOGLECALENDAR_DELETE_EVENT", {"calendar_id": "primary"})


def test_linear_runtime_guard_is_read_only():
    source = {"source_type": "linear", "sync_policy": {"permission_policy": default_permission_policy("linear")}}

    assert_tool_allowed(source, "LINEAR_LIST_LINEAR_ISSUES", {"first": 10})
    with pytest.raises(PermissionDenied, match="read-only"):
        assert_tool_allowed(source, "LINEAR_CREATE_LINEAR_ISSUE", {"title": "Do not write"})


def test_hubspot_policy_is_read_only_crm_scope():
    policy = default_permission_policy("hubspot")

    assert "crm.objects.contacts.read" in policy["allowed_scopes"]
    assert "crm.objects.deals.write" in policy["denied_scopes"]
    assert "hubspot_writes" in policy["denied_resource_classes"]


def test_hubspot_runtime_guard_allows_read_only_crm_tools():
    source = {"source_type": "hubspot", "sync_policy": {"permission_policy": default_permission_policy("hubspot")}}

    assert_tool_allowed(source, "HUBSPOT_LIST_CONTACTS", {"limit": 10})
    assert_tool_allowed(source, "HUBSPOT_SEARCH_DEALS", {"limit": 10})
    with pytest.raises(PermissionDenied, match="read-only CRM access"):
        assert_tool_allowed(source, "HUBSPOT_CREATE_CONTACT", {"email": "bad@example.com"})
    with pytest.raises(PermissionDenied, match="read-only CRM access"):
        assert_tool_allowed(source, "HUBSPOT_MERGE_CONTACTS", {"primaryObjectId": "1", "objectIdToMerge": "2"})


def test_hubspot_granted_write_or_sensitive_scope_is_rejected():
    policy = default_permission_policy("hubspot")

    with pytest.raises(PermissionDenied, match="contacts.write"):
        assert_granted_scopes_allowed("hubspot", ["crm.objects.contacts.read", "crm.objects.contacts.write"], policy)
    with pytest.raises(PermissionDenied, match="sensitive"):
        assert_granted_scopes_allowed("hubspot", ["crm.objects.contacts.sensitive.read"], policy)


def test_hubspot_payload_sanitizer_keeps_only_safe_crm_fields():
    payload = sanitize_source_payload("hubspot", {
        "event_type": "crm_object_snapshot",
        "object_type": "contacts",
        "record": {
            "id": "contact-1",
            "updatedAt": "2026-06-22T12:00:00Z",
            "properties": {
                "email": "maya@internal.example.invalid",
                "firstname": "Maya",
                "lastname": "Patel",
                "phone": "+1-555-0100",
                "ssn": "nope",
                "lastmodifieddate": "2026-06-22T12:00:00Z",
            },
            "associations": {"deals": ["deal-1"]},
        },
    })

    assert payload == {
        "event_type": "crm_object_snapshot",
        "object_type": "contacts",
        "record": {
            "id": "contact-1",
            "updatedAt": "2026-06-22T12:00:00Z",
            "properties": {
                "email": "maya@internal.example.invalid",
                "firstname": "Maya",
                "lastname": "Patel",
                "lastmodifieddate": "2026-06-22T12:00:00Z",
            },
        },
    }


def test_github_app_permission_validator_rejects_code_access():
    problems = validate_installation_permissions({
        "permissions": {
            "metadata": "read",
            "pull_requests": "read",
            "contents": "read",
            "actions": "read",
            "checks": "read",
        },
    })

    assert "GitHub App granted denied permission: contents:read" in problems
    assert "GitHub App granted denied permission: actions:read" in problems
    assert "GitHub App granted denied permission: checks:read" in problems


def test_github_app_permission_validator_requires_pr_metadata_access():
    problems = validate_installation_permissions({
        "permissions": {
            "metadata": "read",
            "issues": "read",
        },
    })

    assert "GitHub App permission pull_requests is required as read, got missing" in problems


def test_github_app_webhook_signature_verification():
    body = b'{"zen":"safe metadata"}'
    secret = "github-webhook-secret"
    signature = "sha256=" + __import__("hmac").new(
        secret.encode(),
        body,
        __import__("hashlib").sha256,
    ).hexdigest()

    assert verify_github_signature(secret=secret, body=body, signature_header=signature)
    assert not verify_github_signature(secret=secret, body=body, signature_header="sha256=bad")
