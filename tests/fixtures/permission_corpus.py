"""Seed corpus for Alloy permission behavior.

These fixtures are provider-shaped on input and Alloy-shaped on expectation.
Slice 0 keeps the corpus executable before production permission code exists.
Later slices should wire these cases into adapter, retrieval, vector, surface,
audit, and console tests instead of replacing them.
"""

from __future__ import annotations

from itertools import combinations, product
from typing import Any


SUPPORTED_SOURCES = {
    "slack",
    "github",
    "gmail",
    "googledocs",
    "googledrive",
    "gcal",
    "linear",
    "hubspot",
}

REQUIRED_LEAK_PATHS = {
    "sql_candidates",
    "vector_top_k",
    "reranker_input",
    "context_pack",
    "prompt_text",
    "model_trace",
    "source_chips",
    "audit_view",
    "console_view",
    "destination_surface",
}

REQUIRED_ENVELOPE_FIELDS = {
    "visibility",
    "allowed_principals",
    "acl_basis",
    "acl_confidence",
    "acl_state",
    "acl_freshness_state",
}

REQUIRED_PROVIDER_PAYLOAD_KEYS = {
    "slack": {"channel"},
    "github": {"repository"},
    "gmail": {"connected_account", "thread"},
    "googledocs": {"fileId", "permissions"},
    "googledrive": {"file", "permissions"},
    "gcal": {"calendar", "calendar_acl", "event"},
    "linear": {"workspace", "team", "project", "issue", "teams", "guest"},
    "hubspot": {"object_type", "record"},
}

CORPUS_PRINCIPAL_MAPPINGS = {
    ("slack", "channel", "C_PRODUCT"): "group:product",
    ("slack", "user", "U_MAYA"): "user:maya",
    ("slack", "user", "U_SHRUTI"): "user:shruti",
    ("slack", "user", "U_JAMES"): "user:james",
    ("slack", "usergroup", "S_PRODUCT"): "group:product",
    ("slack", "workspace", "T_EXAMPLECO"): "company:exampleco",
    ("github", "org", "company"): "company:exampleco",
    ("github", "team", "engineering"): "group:engineering",
    ("google", "group", "product@company.com"): "group:product",
    ("google", "group", "sales@company.com"): "group:sales",
    ("google", "domain", "company.com"): "company:exampleco",
    ("google", "user", "maya@company.com"): "user:maya",
    ("google", "user", "naman@company.com"): "user:founder",
    ("linear", "workspace", "lin_ws"): "company:exampleco",
    ("linear", "team", "ENG"): "group:engineering",
    ("linear", "team", "EXEC"): "group:leadership",
    ("hubspot", "owner", "owner_123"): "user:maya",
    ("hubspot", "team", "team_sales_a"): "group:sales-a",
    ("hubspot", "pipeline", "enterprise"): "group:sales-a",
    ("hubspot", "policy", "all_hubspot_users"): "company:exampleco",
}

REQUIRED_AXIS_VALUES = {
    "actor_category": {
        "owner",
        "direct_share_recipient",
        "same_group_member",
        "different_group_member",
        "company_member",
        "admin_operator",
        "external_guest",
        "unknown_unmapped_user",
    },
    "actor_group_state": {
        "in_allowed_group",
        "removed_from_allowed_group",
        "in_unrelated_group",
        "in_multiple_groups",
        "guest_not_alloy_company",
    },
    "source_visibility": {
        "private",
        "direct_user_share",
        "group_team_share",
        "company_workspace_org_visible",
        "inherited_from_parent",
        "external_shared",
        "mixed_public_private",
        "unclear",
    },
    "acl_freshness_state": {"fresh", "stale", "revoked", "needs_permission_review"},
    "retrieval_audience": {
        "private_user",
        "direct_user_set",
        "team_group",
        "company",
        "operator",
        "external_destination",
    },
    "destination": {
        "ask_discuss",
        "brief",
        "alert",
        "slack_channel",
        "generated_google_doc",
        "email_draft",
        "csv_export",
        "dashboard",
        "source_chip",
        "audit_view",
    },
}

REQUIRED_SOURCE_PRINCIPAL_PAIRS = {
    ("slack", "channel"),
    ("slack", "user"),
    ("slack", "group"),
    ("slack", "workspace"),
    ("slack", "unknown_principal"),
    ("github", "repo"),
    ("github", "team"),
    ("github", "org"),
    ("github", "external_collaborator"),
    ("github", "unknown_principal"),
    ("gmail", "owner"),
    ("googledocs", "user"),
    ("googledocs", "group"),
    ("googledocs", "domain"),
    ("googledocs", "folder"),
    ("googledrive", "user"),
    ("googledrive", "group"),
    ("googledrive", "folder"),
    ("googledrive", "shared_drive"),
    ("googledrive", "external_collaborator"),
    ("gcal", "calendar"),
    ("gcal", "user"),
    ("gcal", "domain"),
    ("linear", "workspace"),
    ("linear", "team"),
    ("linear", "project"),
    ("linear", "external_guest"),
    ("hubspot", "owner"),
    ("hubspot", "team"),
    ("hubspot", "pipeline"),
    ("hubspot", "unknown_principal"),
}

PAIRWISE_AXES = {
    "source_type": SUPPORTED_SOURCES,
    "actor_category": REQUIRED_AXIS_VALUES["actor_category"],
    "actor_group_state": REQUIRED_AXIS_VALUES["actor_group_state"],
    "provider_principal_type": {principal_type for _, principal_type in REQUIRED_SOURCE_PRINCIPAL_PAIRS},
}

LEAK_CRITICAL_COVERAGE = {
    ("gmail", "owner_thread", "ask_discuss", "company_member"),
    ("gmail", "owner_thread", "shared_chat_answer", "company_member"),
    ("googledrive", "direct_user_share", "ask_discuss", "direct_share_recipient"),
    ("googledrive", "group_share", "brief", "same_group_member"),
    ("googledrive", "inherited_folder", "dashboard", "admin_operator"),
    ("googledrive", "shared_drive", "generated_google_doc", "same_group_member"),
    ("googledrive", "external_share", "csv_export", "external_guest"),
    ("slack", "private_channel", "ask_discuss", "same_group_member"),
    ("slack", "public_channel", "source_chip", "company_member"),
    ("slack", "removed_member", "slack_channel", "different_group_member"),
    ("slack", "unmapped_member", "audit_view", "unknown_unmapped_user"),
    ("github", "team_repo", "ask_discuss", "same_group_member"),
    ("github", "org_repo", "brief", "company_member"),
    ("github", "outside_collaborator", "audit_view", "external_guest"),
    ("github", "missing_team_mapping", "source_chip", "unknown_unmapped_user"),
    ("linear", "workspace_issue", "brief", "company_member"),
    ("linear", "private_team_issue", "ask_discuss", "same_group_member"),
    ("linear", "guest_issue", "dashboard", "external_guest"),
    ("linear", "mixed_project", "alert", "different_group_member"),
    ("hubspot", "owner_record", "ask_discuss", "owner"),
    ("hubspot", "team_record", "dashboard", "same_group_member"),
    ("hubspot", "pipeline_record", "brief", "same_group_member"),
    ("hubspot", "all_user_record", "source_chip", "company_member"),
    ("hubspot", "unclear_owner_mapping", "audit_view", "unknown_unmapped_user"),
}


def envelope(
    *,
    visibility: str,
    principals: list[str],
    basis: str = "provider_snapshot",
    confidence: str = "high",
    state: str = "stamped",
    freshness: str = "fresh",
) -> dict[str, Any]:
    return {
        "visibility": visibility,
        "allowed_principals": principals,
        "acl_basis": basis,
        "acl_confidence": confidence,
        "acl_state": state,
        "acl_freshness_state": freshness,
    }


def fixture(
    *,
    case_id: str,
    source_type: str,
    scenario: str,
    actor_category: str,
    actor_group_state: str,
    provider_principal_types: list[str],
    source_visibility: str,
    retrieval_audience: str,
    destination: str,
    provider_acl_payload: dict[str, Any],
    expected_envelope: dict[str, Any],
    allowed_viewers: list[str],
    denied_viewers: list[str],
    denied_groups: list[str],
    purpose: str = "permission_corpus",
    tags: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "id": case_id,
        "source_type": source_type,
        "scenario": scenario,
        "actor": {
            "user_id": f"user_{actor_category}",
            "category": actor_category,
            "groups": [actor_group_state],
        },
        "actor_group_state": actor_group_state,
        "provider_principal_types": provider_principal_types,
        "source_visibility": source_visibility,
        "acl_freshness_state": expected_envelope["acl_freshness_state"],
        "target_audience": retrieval_audience,
        "purpose": purpose,
        "destination": destination,
        "provider_acl_payload": provider_acl_payload,
        "expected_envelope": expected_envelope,
        "allowed_viewers": allowed_viewers,
        "denied_viewers": denied_viewers,
        "denied_groups": denied_groups,
        "expected_absent_from": sorted(REQUIRED_LEAK_PATHS),
        "tags": tags or [],
    }


PERMISSION_FIXTURES: list[dict[str, Any]] = [
    fixture(
        case_id="slack_private_product_channel",
        source_type="slack",
        scenario="private_channel",
        actor_category="same_group_member",
        actor_group_state="in_allowed_group",
        provider_principal_types=["channel", "user", "group"],
        source_visibility="group_team_share",
        retrieval_audience="team_group",
        destination="ask_discuss",
        provider_acl_payload={
            "channel": {"id": "C_PRODUCT", "name": "product", "is_private": True},
            "members": ["U_MAYA", "U_SHRUTI", "U_JAMES"],
            "usergroups": [{"id": "S_PRODUCT", "users": ["U_MAYA", "U_SHRUTI", "U_JAMES"]}],
        },
        expected_envelope=envelope(visibility="team", principals=["group:product"]),
        allowed_viewers=["user:maya", "user:shruti", "user:james"],
        denied_viewers=["user:founder"],
        denied_groups=["group:sales"],
    ),
    fixture(
        case_id="slack_company_announcements",
        source_type="slack",
        scenario="public_channel",
        actor_category="company_member",
        actor_group_state="in_multiple_groups",
        provider_principal_types=["channel", "workspace"],
        source_visibility="company_workspace_org_visible",
        retrieval_audience="company",
        destination="source_chip",
        provider_acl_payload={
            "team": {"id": "T_EXAMPLECO", "domain": "exampleco"},
            "channel": {"id": "C_ANNOUNCE", "name": "company-announcements", "is_private": False},
        },
        expected_envelope=envelope(visibility="company", principals=["company:exampleco"]),
        allowed_viewers=["user:maya", "user:founder"],
        denied_viewers=["external:vendor"],
        denied_groups=["external"],
    ),
    fixture(
        case_id="slack_removed_member_revoked",
        source_type="slack",
        scenario="removed_member",
        actor_category="different_group_member",
        actor_group_state="removed_from_allowed_group",
        provider_principal_types=["channel", "user"],
        source_visibility="group_team_share",
        retrieval_audience="external_destination",
        destination="slack_channel",
        provider_acl_payload={
            "channel": {"id": "C_PRODUCT", "name": "product", "is_private": True},
            "members": ["U_MAYA", "U_SHRUTI"],
            "removed_members": ["U_JAMES"],
        },
        expected_envelope=envelope(visibility="team", principals=["group:product"], freshness="revoked"),
        allowed_viewers=["user:maya", "user:shruti"],
        denied_viewers=["user:james", "user:founder"],
        denied_groups=["group:company"],
    ),
    fixture(
        case_id="slack_unmapped_private_member",
        source_type="slack",
        scenario="unmapped_member",
        actor_category="unknown_unmapped_user",
        actor_group_state="in_unrelated_group",
        provider_principal_types=["channel", "unknown_principal"],
        source_visibility="unclear",
        retrieval_audience="operator",
        destination="audit_view",
        provider_acl_payload={
            "channel": {"id": "C_EXEC", "name": "exec-private", "is_private": True},
            "members": ["U_UNKNOWN"],
        },
        expected_envelope=envelope(
            visibility="restricted",
            principals=["unknown:slack:U_UNKNOWN"],
            confidence="low",
            state="needs_permission_review",
            freshness="needs_permission_review",
        ),
        allowed_viewers=[],
        denied_viewers=["user:operator", "user:founder"],
        denied_groups=["group:company"],
    ),
    fixture(
        case_id="github_engineering_repo",
        source_type="github",
        scenario="team_repo",
        actor_category="same_group_member",
        actor_group_state="in_allowed_group",
        provider_principal_types=["repo", "team"],
        source_visibility="group_team_share",
        retrieval_audience="team_group",
        destination="ask_discuss",
        provider_acl_payload={
            "repository": {"full_name": "inferencehq/backend", "visibility": "private"},
            "teams": [{"slug": "engineering", "permission": "pull"}],
            "collaborators": [],
        },
        expected_envelope=envelope(visibility="team", principals=["group:engineering"]),
        allowed_viewers=["user:martin"],
        denied_viewers=["user:sales"],
        denied_groups=["group:sales"],
    ),
    fixture(
        case_id="github_company_handbook_org_repo",
        source_type="github",
        scenario="org_repo",
        actor_category="company_member",
        actor_group_state="in_multiple_groups",
        provider_principal_types=["repo", "org"],
        source_visibility="company_workspace_org_visible",
        retrieval_audience="company",
        destination="brief",
        provider_acl_payload={
            "organization": {"login": "company", "mapped_to_company": True},
            "repository": {"full_name": "company/handbook", "visibility": "internal"},
        },
        expected_envelope=envelope(visibility="company", principals=["company:exampleco"]),
        allowed_viewers=["user:founder", "user:maya"],
        denied_viewers=["external:contractor"],
        denied_groups=["external"],
    ),
    fixture(
        case_id="github_outside_collaborator",
        source_type="github",
        scenario="outside_collaborator",
        actor_category="external_guest",
        actor_group_state="guest_not_alloy_company",
        provider_principal_types=["repo", "external_collaborator"],
        source_visibility="external_shared",
        retrieval_audience="external_destination",
        destination="audit_view",
        provider_acl_payload={
            "repository": {"full_name": "company/partner-portal", "visibility": "private"},
            "outside_collaborators": [{"login": "partner-dev", "permission": "pull"}],
        },
        expected_envelope=envelope(visibility="restricted", principals=["external:github:partner-dev"], confidence="medium"),
        allowed_viewers=["external:github:partner-dev"],
        denied_viewers=["user:founder"],
        denied_groups=["group:company"],
    ),
    fixture(
        case_id="github_missing_team_mapping",
        source_type="github",
        scenario="missing_team_mapping",
        actor_category="unknown_unmapped_user",
        actor_group_state="in_unrelated_group",
        provider_principal_types=["repo", "unknown_principal"],
        source_visibility="unclear",
        retrieval_audience="operator",
        destination="source_chip",
        provider_acl_payload={
            "repository": {"full_name": "company/secret", "visibility": "private"},
            "teams": [{"slug": "unknown-team", "permission": "pull"}],
        },
        expected_envelope=envelope(
            visibility="restricted",
            principals=["unknown:github-team:unknown-team"],
            confidence="low",
            state="needs_permission_review",
            freshness="needs_permission_review",
        ),
        allowed_viewers=[],
        denied_viewers=["user:operator", "user:founder"],
        denied_groups=["group:company"],
    ),
    fixture(
        case_id="gmail_naman_customer_thread",
        source_type="gmail",
        scenario="owner_thread",
        actor_category="owner",
        actor_group_state="in_multiple_groups",
        provider_principal_types=["owner"],
        source_visibility="private",
        retrieval_audience="private_user",
        destination="ask_discuss",
        provider_acl_payload={
            "connected_account": {"alloy_user_id": "user:founder", "email": "naman@company.com"},
            "thread": {"id": "thread_customer_1", "participants": ["naman@company.com", "buyer@example.com"]},
            "labels": ["INBOX"],
        },
        expected_envelope=envelope(visibility="private", principals=["user:founder"], basis="connected_account_owner"),
        allowed_viewers=["user:founder"],
        denied_viewers=["user:maya", "user:operator"],
        denied_groups=["group:company"],
    ),
    fixture(
        case_id="gmail_shared_answer_recheck",
        source_type="gmail",
        scenario="owner_thread",
        actor_category="company_member",
        actor_group_state="in_unrelated_group",
        provider_principal_types=["owner"],
        source_visibility="private",
        retrieval_audience="company",
        destination="shared_chat_answer",
        provider_acl_payload={
            "connected_account": {"alloy_user_id": "user:founder", "email": "naman@company.com"},
            "thread": {"id": "thread_customer_1", "participants": ["naman@company.com", "buyer@example.com"]},
        },
        expected_envelope=envelope(visibility="private", principals=["user:founder"], basis="connected_account_owner"),
        allowed_viewers=["user:founder"],
        denied_viewers=["user:maya", "user:operator"],
        denied_groups=["group:company"],
    ),
    fixture(
        case_id="gmail_private_connection_console_view",
        source_type="gmail",
        scenario="owner_connection_console_view",
        actor_category="admin_operator",
        actor_group_state="in_multiple_groups",
        provider_principal_types=["owner"],
        source_visibility="private",
        retrieval_audience="operator",
        destination="console_view",
        provider_acl_payload={
            "connected_account": {"alloy_user_id": "user:maya", "email": "maya@company.com"},
            "thread": {"id": "gmail-thread-private", "participants": ["maya@company.com", "buyer@example.com"]},
        },
        expected_envelope=envelope(visibility="private", principals=["user:maya"], basis="connected_account_owner"),
        allowed_viewers=["user:maya"],
        denied_viewers=["user:founder", "user:operator"],
        denied_groups=["group:company"],
        tags=["console_privacy"],
    ),
    fixture(
        case_id="gmail_owner_thread_non_owner_ask",
        source_type="gmail",
        scenario="owner_thread",
        actor_category="company_member",
        actor_group_state="in_unrelated_group",
        provider_principal_types=["owner"],
        source_visibility="private",
        retrieval_audience="company",
        destination="ask_discuss",
        provider_acl_payload={
            "connected_account": {"alloy_user_id": "user:founder", "email": "naman@company.com"},
            "thread": {"id": "thread_customer_1", "participants": ["naman@company.com", "buyer@example.com"]},
        },
        expected_envelope=envelope(visibility="private", principals=["user:founder"], basis="connected_account_owner"),
        allowed_viewers=["user:founder"],
        denied_viewers=["user:maya", "user:operator"],
        denied_groups=["group:company"],
    ),
    fixture(
        case_id="gmail_owner_thread_email_draft_block",
        source_type="gmail",
        scenario="owner_thread",
        actor_category="owner",
        actor_group_state="in_multiple_groups",
        provider_principal_types=["owner"],
        source_visibility="private",
        retrieval_audience="external_destination",
        destination="email_draft",
        provider_acl_payload={
            "connected_account": {"alloy_user_id": "user:founder", "email": "naman@company.com"},
            "thread": {"id": "thread_customer_1", "participants": ["naman@company.com", "buyer@example.com"]},
            "draft_destination": {"to": ["team@company.com"]},
        },
        expected_envelope=envelope(visibility="private", principals=["user:founder"], basis="connected_account_owner"),
        allowed_viewers=["user:founder"],
        denied_viewers=["user:maya", "user:operator"],
        denied_groups=["group:company"],
    ),
    fixture(
        case_id="googledocs_product_roadmap_group",
        source_type="googledocs",
        scenario="group_doc",
        actor_category="same_group_member",
        actor_group_state="in_allowed_group",
        provider_principal_types=["user", "group"],
        source_visibility="group_team_share",
        retrieval_audience="team_group",
        destination="ask_discuss",
        provider_acl_payload={
            "fileId": "doc_roadmap",
            "permissions": [
                {"type": "group", "emailAddress": "product@company.com", "role": "reader"},
                {"type": "user", "emailAddress": "maya@company.com", "role": "writer"},
            ],
        },
        expected_envelope=envelope(visibility="team", principals=["group:product", "user:maya"]),
        allowed_viewers=["user:maya", "user:shruti"],
        denied_viewers=["user:sales"],
        denied_groups=["group:sales"],
    ),
    fixture(
        case_id="googledocs_company_okr_domain",
        source_type="googledocs",
        scenario="company_folder_doc",
        actor_category="company_member",
        actor_group_state="in_multiple_groups",
        provider_principal_types=["domain", "folder"],
        source_visibility="inherited_from_parent",
        retrieval_audience="company",
        destination="brief",
        provider_acl_payload={
            "fileId": "doc_okr",
            "inherited": True,
            "parents": [{"id": "folder_company", "name": "Company OKRs"}],
            "permissions": [{"type": "domain", "domain": "company.com", "role": "reader"}],
        },
        expected_envelope=envelope(visibility="company", principals=["company:exampleco"]),
        allowed_viewers=["user:founder", "user:maya"],
        denied_viewers=["external:vendor"],
        denied_groups=["external"],
    ),
    fixture(
        case_id="googledrive_direct_private_file",
        source_type="googledrive",
        scenario="direct_user_share",
        actor_category="direct_share_recipient",
        actor_group_state="in_multiple_groups",
        provider_principal_types=["user"],
        source_visibility="direct_user_share",
        retrieval_audience="direct_user_set",
        destination="ask_discuss",
        provider_acl_payload={
            "file": {"id": "file_private_plan", "name": "Private plan"},
            "permissions": [
                {"type": "user", "emailAddress": "maya@company.com", "role": "owner"},
                {"type": "user", "emailAddress": "naman@company.com", "role": "reader"},
            ],
        },
        expected_envelope=envelope(visibility="direct", principals=["user:maya", "user:founder"]),
        allowed_viewers=["user:maya", "user:founder"],
        denied_viewers=["user:shruti"],
        denied_groups=["group:company"],
    ),
    fixture(
        case_id="googledrive_product_group_share",
        source_type="googledrive",
        scenario="group_share",
        actor_category="same_group_member",
        actor_group_state="in_allowed_group",
        provider_principal_types=["group"],
        source_visibility="group_team_share",
        retrieval_audience="team_group",
        destination="brief",
        provider_acl_payload={
            "file": {"id": "file_product_plan", "name": "Product plan"},
            "permissions": [{"type": "group", "emailAddress": "product@company.com", "role": "reader"}],
        },
        expected_envelope=envelope(visibility="team", principals=["group:product"]),
        allowed_viewers=["user:maya", "user:shruti"],
        denied_viewers=["user:sales"],
        denied_groups=["group:sales"],
    ),
    fixture(
        case_id="googledrive_group_resource_console_view",
        source_type="googledrive",
        scenario="group_resource_console_view",
        actor_category="same_group_member",
        actor_group_state="in_allowed_group",
        provider_principal_types=["group", "folder"],
        source_visibility="group_team_share",
        retrieval_audience="team_group",
        destination="console_view",
        provider_acl_payload={
            "file": {"id": "drive-resource-product", "name": "Product Readiness Notes"},
            "permissions": [{"type": "group", "emailAddress": "product@company.com", "role": "reader"}],
        },
        expected_envelope=envelope(visibility="team", principals=["group:product"]),
        allowed_viewers=["user:maya"],
        denied_viewers=["user:sales", "user:operator"],
        denied_groups=["group:sales"],
        tags=["console_privacy"],
    ),
    fixture(
        case_id="googledrive_sales_inherited_folder",
        source_type="googledrive",
        scenario="inherited_folder",
        actor_category="admin_operator",
        actor_group_state="in_allowed_group",
        provider_principal_types=["folder", "group"],
        source_visibility="inherited_from_parent",
        retrieval_audience="operator",
        destination="dashboard",
        provider_acl_payload={
            "file": {"id": "deck_sales_forecast", "name": "Sales forecast"},
            "parents": [{"id": "folder_sales", "name": "Sales"}],
            "permissions": [{"type": "group", "emailAddress": "sales@company.com", "role": "reader", "inherited": True}],
        },
        expected_envelope=envelope(visibility="team", principals=["group:sales"]),
        allowed_viewers=["user:sales-lead"],
        denied_viewers=["user:operator"],
        denied_groups=["group:engineering"],
    ),
    fixture(
        case_id="googledrive_sales_shared_drive",
        source_type="googledrive",
        scenario="shared_drive",
        actor_category="same_group_member",
        actor_group_state="in_allowed_group",
        provider_principal_types=["shared_drive", "group"],
        source_visibility="inherited_from_parent",
        retrieval_audience="team_group",
        destination="generated_google_doc",
        provider_acl_payload={
            "drive": {"id": "drive_sales", "name": "Sales shared drive"},
            "file": {"id": "deck_qbr", "name": "QBR"},
            "permissions": [{"type": "group", "emailAddress": "sales@company.com", "role": "reader"}],
        },
        expected_envelope=envelope(visibility="team", principals=["group:sales"]),
        allowed_viewers=["user:sales-lead"],
        denied_viewers=["user:maya"],
        denied_groups=["group:product"],
    ),
    fixture(
        case_id="googledrive_external_share",
        source_type="googledrive",
        scenario="external_share",
        actor_category="external_guest",
        actor_group_state="guest_not_alloy_company",
        provider_principal_types=["external_collaborator", "user"],
        source_visibility="external_shared",
        retrieval_audience="external_destination",
        destination="csv_export",
        provider_acl_payload={
            "file": {"id": "file_partner", "name": "Partner notes"},
            "permissions": [{"type": "user", "emailAddress": "partner@example.com", "role": "reader"}],
        },
        expected_envelope=envelope(visibility="restricted", principals=["external:partner@example.com"], confidence="medium"),
        allowed_viewers=["external:partner@example.com"],
        denied_viewers=["user:founder"],
        denied_groups=["group:company"],
    ),
    fixture(
        case_id="gcal_private_interview_event",
        source_type="gcal",
        scenario="private_event",
        actor_category="owner",
        actor_group_state="in_multiple_groups",
        provider_principal_types=["calendar", "user"],
        source_visibility="private",
        retrieval_audience="private_user",
        destination="ask_discuss",
        provider_acl_payload={
            "calendar": {"id": "maya@company.com", "accessRole": "owner"},
            "calendar_acl": [{"scope": {"type": "user", "value": "maya@company.com"}, "role": "owner"}],
            "event": {"id": "event_interview", "visibility": "private", "summary": "Interview candidate"},
            "attendees": [{"email": "naman@company.com"}],
        },
        expected_envelope=envelope(visibility="private", principals=["user:maya"], basis="calendar_acl_event_privacy"),
        allowed_viewers=["user:maya"],
        denied_viewers=["user:founder", "user:operator"],
        denied_groups=["group:company"],
    ),
    fixture(
        case_id="gcal_company_all_hands",
        source_type="gcal",
        scenario="company_event",
        actor_category="company_member",
        actor_group_state="in_multiple_groups",
        provider_principal_types=["calendar", "domain"],
        source_visibility="company_workspace_org_visible",
        retrieval_audience="company",
        destination="brief",
        provider_acl_payload={
            "calendar": {"id": "company@company.com", "accessRole": "reader"},
            "calendar_acl": [{"scope": {"type": "domain", "value": "company.com"}, "role": "reader"}],
            "event": {"id": "event_all_hands", "visibility": "public", "summary": "All hands"},
        },
        expected_envelope=envelope(visibility="company", principals=["company:exampleco"], basis="calendar_acl_event_privacy"),
        allowed_viewers=["user:founder", "user:maya"],
        denied_viewers=["external:vendor"],
        denied_groups=["external"],
    ),
    fixture(
        case_id="linear_workspace_issue",
        source_type="linear",
        scenario="workspace_issue",
        actor_category="company_member",
        actor_group_state="in_multiple_groups",
        provider_principal_types=["workspace", "team"],
        source_visibility="company_workspace_org_visible",
        retrieval_audience="company",
        destination="brief",
        provider_acl_payload={
            "workspace": {"id": "lin_ws", "mapped_to_company": True},
            "team": {"id": "ENG", "private": False},
            "issue": {"id": "ENG-123", "team": {"id": "ENG"}},
        },
        expected_envelope=envelope(visibility="company", principals=["company:exampleco"], basis="derived_provider_policy", confidence="medium"),
        allowed_viewers=["user:founder", "user:martin"],
        denied_viewers=["external:contractor"],
        denied_groups=["external"],
    ),
    fixture(
        case_id="linear_private_leadership_issue",
        source_type="linear",
        scenario="private_team_issue",
        actor_category="same_group_member",
        actor_group_state="in_allowed_group",
        provider_principal_types=["team", "project"],
        source_visibility="group_team_share",
        retrieval_audience="team_group",
        destination="ask_discuss",
        provider_acl_payload={
            "team": {"id": "EXEC", "private": True},
            "project": {"id": "exec-planning", "teamIds": ["EXEC"]},
            "issue": {"id": "EXEC-7", "team": {"id": "EXEC"}},
        },
        expected_envelope=envelope(visibility="team", principals=["group:leadership"], basis="derived_provider_policy", confidence="medium"),
        allowed_viewers=["user:howard", "user:collin"],
        denied_viewers=["user:maya"],
        denied_groups=["group:company"],
    ),
    fixture(
        case_id="linear_guest_issue",
        source_type="linear",
        scenario="guest_issue",
        actor_category="external_guest",
        actor_group_state="guest_not_alloy_company",
        provider_principal_types=["external_guest", "team"],
        source_visibility="external_shared",
        retrieval_audience="external_destination",
        destination="dashboard",
        provider_acl_payload={
            "team": {"id": "PARTNER", "private": True},
            "guest": {"email": "partner@example.com", "teamIds": ["PARTNER"]},
            "issue": {"id": "PARTNER-9", "team": {"id": "PARTNER"}},
        },
        expected_envelope=envelope(visibility="restricted", principals=["external:partner@example.com"], basis="derived_provider_policy", confidence="medium"),
        allowed_viewers=["external:partner@example.com"],
        denied_viewers=["user:founder"],
        denied_groups=["group:company"],
    ),
    fixture(
        case_id="linear_mixed_project_review",
        source_type="linear",
        scenario="mixed_project",
        actor_category="different_group_member",
        actor_group_state="in_unrelated_group",
        provider_principal_types=["project", "team"],
        source_visibility="mixed_public_private",
        retrieval_audience="team_group",
        destination="alert",
        provider_acl_payload={
            "project": {"id": "mixed-roadmap", "teamIds": ["ENG", "EXEC"]},
            "teams": [{"id": "ENG", "private": False}, {"id": "EXEC", "private": True}],
        },
        expected_envelope=envelope(
            visibility="restricted",
            principals=["group:engineering", "group:leadership"],
            basis="derived_provider_policy",
            confidence="low",
            state="needs_permission_review",
            freshness="needs_permission_review",
        ),
        allowed_viewers=[],
        denied_viewers=["user:maya", "user:operator"],
        denied_groups=["group:company"],
    ),
    fixture(
        case_id="hubspot_owner_contact",
        source_type="hubspot",
        scenario="owner_record",
        actor_category="owner",
        actor_group_state="in_multiple_groups",
        provider_principal_types=["owner"],
        source_visibility="private",
        retrieval_audience="private_user",
        destination="ask_discuss",
        provider_acl_payload={
            "object_type": "contact",
            "record": {"id": "contact-1", "properties": {"hubspot_owner_id": "owner_123"}},
            "owners": [{"id": "owner_123", "email": "maya@company.com", "teams": [{"id": "team_sales_a"}]}],
        },
        expected_envelope=envelope(visibility="private", principals=["user:maya"], basis="derived_provider_policy", confidence="medium"),
        allowed_viewers=["user:maya"],
        denied_viewers=["user:founder"],
        denied_groups=["group:company"],
    ),
    fixture(
        case_id="hubspot_sales_team_deal",
        source_type="hubspot",
        scenario="team_record",
        actor_category="same_group_member",
        actor_group_state="in_allowed_group",
        provider_principal_types=["team", "owner"],
        source_visibility="group_team_share",
        retrieval_audience="team_group",
        destination="dashboard",
        provider_acl_payload={
            "object_type": "deal",
            "record": {"id": "deal-1", "properties": {"hubspot_owner_id": "owner_456", "pipeline": "sales_a"}},
            "owners": [{"id": "owner_456", "teams": [{"id": "team_sales_a", "primary": True}]}],
        },
        expected_envelope=envelope(visibility="team", principals=["group:sales-a"], basis="derived_provider_policy", confidence="medium"),
        allowed_viewers=["user:sales-a"],
        denied_viewers=["user:sales-b", "user:maya"],
        denied_groups=["group:product"],
    ),
    fixture(
        case_id="hubspot_pipeline_restricted_deal",
        source_type="hubspot",
        scenario="pipeline_record",
        actor_category="same_group_member",
        actor_group_state="in_allowed_group",
        provider_principal_types=["pipeline", "team"],
        source_visibility="group_team_share",
        retrieval_audience="team_group",
        destination="brief",
        provider_acl_payload={
            "object_type": "deal",
            "record": {"id": "deal-2", "properties": {"pipeline": "enterprise"}},
            "pipeline_access": {"pipeline": "enterprise", "teams": ["team_sales_a"]},
        },
        expected_envelope=envelope(visibility="team", principals=["group:sales-a"], basis="derived_provider_policy", confidence="medium"),
        allowed_viewers=["user:sales-a"],
        denied_viewers=["user:sales-b"],
        denied_groups=["group:product"],
    ),
    fixture(
        case_id="hubspot_all_user_company_record",
        source_type="hubspot",
        scenario="all_user_record",
        actor_category="company_member",
        actor_group_state="in_multiple_groups",
        provider_principal_types=["team", "owner"],
        source_visibility="company_workspace_org_visible",
        retrieval_audience="company",
        destination="source_chip",
        provider_acl_payload={
            "object_type": "company",
            "record": {"id": "company-1", "properties": {"domain": "example.com"}},
            "policy": {"record_access": "all_hubspot_users"},
        },
        expected_envelope=envelope(visibility="company", principals=["company:exampleco"], basis="tenant_policy", confidence="medium"),
        allowed_viewers=["user:founder", "user:maya"],
        denied_viewers=["external:vendor"],
        denied_groups=["external"],
    ),
    fixture(
        case_id="hubspot_unclear_owner_mapping",
        source_type="hubspot",
        scenario="unclear_owner_mapping",
        actor_category="unknown_unmapped_user",
        actor_group_state="in_unrelated_group",
        provider_principal_types=["owner", "unknown_principal"],
        source_visibility="unclear",
        retrieval_audience="operator",
        destination="audit_view",
        provider_acl_payload={
            "object_type": "deal",
            "record": {"id": "deal-unknown", "properties": {"hubspot_owner_id": "owner_missing"}},
            "owners": [],
        },
        expected_envelope=envelope(
            visibility="restricted",
            principals=["unknown:hubspot-owner:owner_missing"],
            basis="derived_provider_policy",
            confidence="low",
            state="needs_permission_review",
            freshness="needs_permission_review",
        ),
        allowed_viewers=[],
        denied_viewers=["user:operator", "user:founder"],
        denied_groups=["group:company"],
    ),
    fixture(
        case_id="legacy_unstamped_chunk",
        source_type="gmail",
        scenario="legacy_unstamped",
        actor_category="admin_operator",
        actor_group_state="in_multiple_groups",
        provider_principal_types=["owner"],
        source_visibility="unclear",
        retrieval_audience="operator",
        destination="audit_view",
        provider_acl_payload={"legacy_row": {"table": "content_chunks", "has_acl_envelope": False}},
        expected_envelope=envelope(
            visibility="restricted",
            principals=[],
            basis="legacy_missing_acl",
            confidence="low",
            state="legacy_unstamped",
            freshness="needs_permission_review",
        ),
        allowed_viewers=[],
        denied_viewers=["user:operator", "user:founder"],
        denied_groups=["group:company"],
        tags=["legacy_fail_closed"],
    ),
    fixture(
        case_id="stale_acl_company_synthesis_block",
        source_type="googledocs",
        scenario="company_folder_doc",
        actor_category="company_member",
        actor_group_state="in_multiple_groups",
        provider_principal_types=["domain", "folder"],
        source_visibility="company_workspace_org_visible",
        retrieval_audience="company",
        destination="brief",
        provider_acl_payload={
            "fileId": "doc_stale_okr",
            "permissions": [{"type": "domain", "domain": "company.com", "role": "reader"}],
            "last_acl_synced_at": "2026-06-01T00:00:00Z",
        },
        expected_envelope=envelope(visibility="company", principals=["company:exampleco"], freshness="stale"),
        allowed_viewers=["user:founder"],
        denied_viewers=["user:maya"],
        denied_groups=["group:company"],
        tags=["stale_blocks_company_synthesis"],
    ),
]


def source_principal_pairs(fixtures: list[dict[str, Any]] | None = None) -> set[tuple[str, str]]:
    corpus = fixtures or PERMISSION_FIXTURES
    return {
        (case["source_type"], principal_type)
        for case in corpus
        for principal_type in case["provider_principal_types"]
    }


def leak_coverage_cases(fixtures: list[dict[str, Any]] | None = None) -> set[tuple[str, str, str, str]]:
    corpus = fixtures or PERMISSION_FIXTURES
    return {
        (case["source_type"], case["scenario"], case["destination"], case["actor"]["category"])
        for case in corpus
    }


def axis_values(axis: str, fixtures: list[dict[str, Any]] | None = None) -> set[str]:
    corpus = fixtures or PERMISSION_FIXTURES
    if axis == "actor_category":
        return {case["actor"]["category"] for case in corpus}
    if axis == "retrieval_audience":
        return {case["target_audience"] for case in corpus}
    return {case[axis] for case in corpus}


def pairwise_axis_value(case: dict[str, Any], axis: str) -> str:
    if axis == "source_type":
        return case["source_type"]
    if axis == "actor_category":
        return case["actor"]["category"]
    if axis == "actor_group_state":
        return case["actor_group_state"]
    if axis == "provider_principal_type":
        principal_types = case.get("provider_principal_types") or []
        return principal_types[0] if principal_types else "unknown_principal"
    raise KeyError(axis)


def pairwise_cases(fixtures: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    corpus = fixtures or (PERMISSION_FIXTURES + PERMISSION_PAIRWISE_CASES)
    return [
        {
            "id": case["id"],
            **{axis: pairwise_axis_value(case, axis) for axis in PAIRWISE_AXES},
        }
        for case in corpus
    ]


def required_pairwise_pairs() -> set[tuple[str, str, str, str]]:
    required: set[tuple[str, str, str, str]] = set()
    axes = list(PAIRWISE_AXES)
    for left, right in combinations(axes, 2):
        if {left, right} == {"source_type", "provider_principal_type"}:
            for source_type, principal_type in REQUIRED_SOURCE_PRINCIPAL_PAIRS:
                required.add((left, source_type, right, principal_type))
            continue
        for left_value, right_value in product(PAIRWISE_AXES[left], PAIRWISE_AXES[right]):
            required.add((left, left_value, right, right_value))
    return required


def covered_pairwise_pairs(fixtures: list[dict[str, Any]] | None = None) -> set[tuple[str, str, str, str]]:
    cases = pairwise_cases(fixtures)
    covered: set[tuple[str, str, str, str]] = set()
    axes = list(PAIRWISE_AXES)
    for case in cases:
        for left, right in combinations(axes, 2):
            covered.add((left, case[left], right, case[right]))
    return covered


def _pairwise_base_fixtures() -> dict[tuple[str, str], dict[str, Any]]:
    bases: dict[tuple[str, str], dict[str, Any]] = {}
    for case in PERMISSION_FIXTURES:
        if "legacy_fail_closed" in case["tags"]:
            continue
        for principal_type in case["provider_principal_types"]:
            bases.setdefault((case["source_type"], principal_type), case)
    return bases


def _pairwise_candidate_cases() -> list[dict[str, Any]]:
    bases = _pairwise_base_fixtures()
    candidates: list[dict[str, Any]] = []
    for source_type, principal_type in sorted(REQUIRED_SOURCE_PRINCIPAL_PAIRS):
        base = bases[(source_type, principal_type)]
        for actor_category in sorted(REQUIRED_AXIS_VALUES["actor_category"]):
            for actor_group_state in sorted(REQUIRED_AXIS_VALUES["actor_group_state"]):
                candidates.append(
                    {
                        "id": f"pairwise_{source_type}_{principal_type}_{actor_category}_{actor_group_state}",
                        "source_type": source_type,
                        "scenario": "pairwise_matrix",
                        "actor": {
                            "user_id": f"user_{actor_category}",
                            "category": actor_category,
                            "groups": [actor_group_state],
                        },
                        "actor_group_state": actor_group_state,
                        "provider_principal_types": [principal_type],
                        "source_visibility": base["source_visibility"],
                        "acl_freshness_state": base["acl_freshness_state"],
                        "target_audience": base["target_audience"],
                        "purpose": "permission_pairwise_matrix",
                        "destination": base["destination"],
                        "provider_acl_payload": base["provider_acl_payload"],
                        "expected_envelope": base["expected_envelope"],
                        "allowed_viewers": base["allowed_viewers"],
                        "denied_viewers": base["denied_viewers"],
                        "denied_groups": base["denied_groups"],
                        "expected_absent_from": base["expected_absent_from"],
                        "tags": ["pairwise_matrix", f"base:{base['id']}"],
                    }
                )
    return candidates


def _case_pairs(case: dict[str, Any]) -> set[tuple[str, str, str, str]]:
    axes = list(PAIRWISE_AXES)
    values = {axis: pairwise_axis_value(case, axis) for axis in axes}
    return {
        (left, values[left], right, values[right])
        for left, right in combinations(axes, 2)
    }


def _generate_pairwise_cases() -> list[dict[str, Any]]:
    uncovered = required_pairwise_pairs() - covered_pairwise_pairs(PERMISSION_FIXTURES)
    candidates = _pairwise_candidate_cases()
    selected: list[dict[str, Any]] = []
    while uncovered:
        best = max(candidates, key=lambda case: (len(_case_pairs(case) & uncovered), case["id"]))
        best_cover = _case_pairs(best) & uncovered
        if not best_cover:
            missing = sorted(uncovered)[:5]
            raise AssertionError(f"permission pairwise matrix cannot cover required pairs: {missing}")
        selected.append(best)
        uncovered -= best_cover
        candidates.remove(best)
    return selected


PERMISSION_PAIRWISE_CASES = _generate_pairwise_cases()
