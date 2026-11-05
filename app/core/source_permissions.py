"""Tenant source permission policies and runtime guardrails."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from app.core.db import acquire


SLACK_ALLOWED_SCOPES = [
    "channels:read",
    "channels:history",
    "groups:read",
    "groups:history",
    "users:read",
    "users:read.email",
    "team:read",
    "reactions:read",
    "files:read",
]
SLACK_GROUP_DM_SCOPES = [
    "mpim:read",
    "mpim:history",
    "mpim:write",
]
SLACK_ONE_TO_ONE_DM_READ_SCOPES = ["im:read", "im:history"]
SLACK_DENIED_SCOPES = ["im:read", "im:history", "im:write", "mpim:read", "mpim:history", "mpim:write"]
SLACK_DM_READ_DENIED_SCOPES = ["im:write"]
SLACK_EXAMPLECO_CHANNEL_WRITE_SCOPES = ["chat:write"]
SLACK_STRICT_READ_ONLY_DENIED_SCOPES = SLACK_DENIED_SCOPES

EXAMPLECO_DOMAIN = "exampleco.ai"

GITHUB_ALLOWED_METADATA_FIELDS = {
    "id",
    "node_id",
    "number",
    "title",
    "state",
    "draft",
    "user",
    "author",
    "assignees",
    "requested_reviewers",
    "reviewers",
    "labels",
    "base",
    "head",
    "repository",
    "repo",
    "html_url",
    "url",
    "created_at",
    "updated_at",
    "closed_at",
    "merged_at",
    "additions",
    "deletions",
    "changed_files",
    "comments",
    "review_comments",
    "commits",
    "merged",
    "mergeable",
    "review_state",
    "statuses_url",
}
GITHUB_DENIED_FIELDS = {
    "body",
    "diff_url",
    "patch_url",
    "files",
    "file",
    "filename",
    "patch",
    "diff",
    "contents",
    "content",
    "raw",
    "blob",
}
GITHUB_DENIED_TOOL_MARKERS = ("FILE", "CONTENT", "DIFF", "PATCH", "BLOB", "TREE", "COMMIT")
GITHUB_ALLOWED_TOOLS = {"GITHUB_FIND_PULL_REQUESTS"}

GMAIL_ALLOWED_TOOLS = {"GMAIL_FETCH_EMAILS"}
GMAIL_DENIED_TOOL_MARKERS = (
    "ARCHIVE",
    "CREATE",
    "DELETE",
    "DRAFT",
    "FORWARD",
    "IMPORT",
    "LABEL",
    "MARK",
    "MODIFY",
    "MOVE",
    "REPLY",
    "SEND",
    "TRASH",
    "UPDATE",
)
GOOGLEDOCS_ALLOWED_TOOLS = {"GOOGLEDOCS_SEARCH_DOCUMENTS", "GOOGLEDOCS_GET_DOCUMENT_PLAINTEXT"}
GOOGLEDOCS_DENIED_TOOL_MARKERS = (
    "APPEND",
    "BATCH_UPDATE",
    "CREATE",
    "DELETE",
    "INSERT",
    "PERMISSION",
    "REPLACE",
    "SHARE",
    "UPDATE",
    "WRITE",
)
GOOGLEDRIVE_ALLOWED_TOOLS = {"GOOGLEDRIVE_FIND_FILE"}
GOOGLEDRIVE_DENIED_TOOL_MARKERS = (
    "COPY",
    "CREATE",
    "DELETE",
    "DOWNLOAD",
    "EXPORT",
    "MOVE",
    "PERMISSION",
    "SHARE",
    "UPDATE",
    "UPLOAD",
    "WRITE",
)
GOOGLECALENDAR_ALLOWED_TOOLS = {"GOOGLECALENDAR_EVENTS_LIST"}
GOOGLECALENDAR_DENIED_TOOL_MARKERS = (
    "CREATE",
    "DELETE",
    "IMPORT",
    "INSERT",
    "MOVE",
    "PATCH",
    "QUICK_ADD",
    "UPDATE",
    "WRITE",
)

HUBSPOT_ALLOWED_SCOPES = [
    "oauth",
    "crm.objects.contacts.read",
    "crm.objects.companies.read",
    "crm.objects.deals.read",
    "crm.objects.owners.read",
    "crm.schemas.contacts.read",
    "crm.schemas.companies.read",
    "crm.schemas.deals.read",
]
HUBSPOT_DENIED_SCOPE_SUFFIXES = (".write", ".sensitive.read", ".highly_sensitive.read")
HUBSPOT_DENIED_SCOPES = [
    "crm.objects.contacts.write",
    "crm.objects.companies.write",
    "crm.objects.deals.write",
    "crm.objects.tickets.write",
    "crm.import",
    "crm.export",
]
HUBSPOT_ALLOWED_TOOLS = {
    "HUBSPOT_GET_ACCOUNT_INFO",
    "HUBSPOT_LIST_GRANTED_SCOPES",
    "HUBSPOT_LIST_CONTACTS",
    "HUBSPOT_LIST_COMPANIES",
    "HUBSPOT_LIST_DEALS",
    "HUBSPOT_RETRIEVE_OWNERS",
    "HUBSPOT_RETRIEVE_PAGE_OF_CRM_OWNERS",
    "HUBSPOT_GET_CONTACT_IDS",
    "HUBSPOT_GET_CONTACT",
    "HUBSPOT_GET_COMPANY",
    "HUBSPOT_GET_DEAL",
    "HUBSPOT_GET_DEALS",
    "HUBSPOT_READ_CONTACT",
    "HUBSPOT_READ_CONTACTS",
    "HUBSPOT_READ_CRM_OBJECT_BY_ID",
    "HUBSPOT_READ_BATCH_OF_CRM_OBJECTS_BY_ID_OR_PROPERTY_VALUES",
    "HUBSPOT_READ_APAGE_OF_OBJECTS_BY_TYPE",
    "HUBSPOT_LIST_OBJECT_ASSOCIATIONS",
    "HUBSPOT_READ_ASSOCIATIONS_BATCH",
    "HUBSPOT_SEARCH_CONTACTS_BY_CRITERIA",
    "HUBSPOT_SEARCH_COMPANIES",
    "HUBSPOT_SEARCH_DEALS",
}

LINEAR_ALLOWED_SCOPES = ["read"]
LINEAR_DENIED_SCOPES = ["write"]
HUBSPOT_DENIED_TOOL_MARKERS = (
    "ADD",
    "ARCHIVE",
    "CANCEL",
    "CLONE",
    "CONFIGURE",
    "CREATE",
    "DELETE",
    "IMPORT",
    "MERGE",
    "MODIFY",
    "PARTIALLY_UPDATE",
    "PERMANENTLY_DELETE",
    "PUBLISH",
    "PURGE",
    "REMOVE",
    "REPLACE",
    "RESET",
    "RESTORE",
    "SET_",
    "START_IMPORT",
    "UPDATE",
)

GMAIL_ALLOWED_TOOLS = {"GMAIL_FETCH_EMAILS"}
GMAIL_DENIED_SCOPE_MARKERS = ("gmail.modify", "gmail.compose", "gmail.send", "mail.google.com")
GMAIL_DENIED_TOOL_MARKERS = ("CREATE", "DELETE", "DRAFT", "FORWARD", "MODIFY", "SEND", "TRASH", "UPDATE")

GCAL_ALLOWED_TOOLS = {"GOOGLECALENDAR_EVENTS_LIST"}
GCAL_DENIED_SCOPE_MARKERS = ("calendar.events", "calendar.acls", "calendar.settings", "calendar.addons")
GCAL_DENIED_TOOL_MARKERS = ("CREATE", "DELETE", "INSERT", "PATCH", "QUICK_ADD", "UPDATE", "WRITE")

GOOGLEDRIVE_ALLOWED_TOOLS = {"GOOGLEDRIVE_FIND_FILE"}
GOOGLEDRIVE_DENIED_SCOPE_MARKERS = ("drive.activity",)
GOOGLEDRIVE_DENIED_TOOL_MARKERS = (
    "COPY",
    "CREATE",
    "DELETE",
    "DOWNLOAD",
    "EXPORT",
    "MOVE",
    "PATCH",
    "PERMISSION",
    "REMOVE",
    "SHARE",
    "UPDATE",
    "UPLOAD",
    "WRITE",
)
GOOGLEDOCS_ALLOWED_TOOLS = {"GOOGLEDOCS_SEARCH_DOCUMENTS", "GOOGLEDOCS_GET_DOCUMENT_PLAINTEXT"}
GOOGLEDOCS_DENIED_SCOPE_MARKERS = ("drive.file", "documents")
GOOGLEDOCS_DENIED_TOOL_MARKERS = (
    "BATCH_UPDATE",
    "CREATE",
    "DELETE",
    "INSERT",
    "PERMISSION",
    "REPLACE",
    "SHARE",
    "UPDATE",
    "WRITE",
)
HUBSPOT_SAFE_RECORD_FIELDS = {
    "id",
    "objectId",
    "object_id",
    "userId",
    "user_id",
    "email",
    "createdAt",
    "created_at",
    "updatedAt",
    "updated_at",
    "archived",
    "portalId",
    "portal_id",
    "hubId",
    "hub_id",
    "url",
    "source_url",
    "sourceUrl",
}
HUBSPOT_SAFE_PROPERTIES = {
    "amount",
    "closedate",
    "company",
    "content",
    "createdate",
    "dealname",
    "dealstage",
    "domain",
    "email",
    "firstname",
    "firstName",
    "hs_lastmodifieddate",
    "hs_object_id",
    "hs_pipeline",
    "hs_pipeline_stage",
    "hubspot_owner_id",
    "industry",
    "jobtitle",
    "lastname",
    "lastName",
    "lastmodifieddate",
    "lifecyclestage",
    "name",
    "pipeline",
    "subject",
    "teams",
    "userId",
}


class PermissionDenied(RuntimeError):
    def __init__(self, reason: str, *, source_type: str | None = None, tool_slug: str | None = None):
        super().__init__(reason)
        self.reason = reason
        self.source_type = source_type
        self.tool_slug = tool_slug


def json_object(value: Any) -> dict:
    if value is None:
        return {}
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return dict(value)


def json_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return list(value) if isinstance(value, (list, tuple, set)) else []


def source_type_from_slug(slug: str) -> str | None:
    upper = slug.upper()
    if upper.startswith("SLACK_"):
        return "slack"
    if upper.startswith("GITHUB_"):
        return "github"
    if upper.startswith("HUBSPOT_"):
        return "hubspot"
    if upper.startswith("GOOGLEDOCS_"):
        return "googledocs"
    if upper.startswith("GOOGLEDRIVE_"):
        return "googledrive"
    if upper.startswith("LINEAR_"):
        return "linear"
    if upper.startswith("GMAIL_"):
        return "gmail"
    if upper.startswith("GOOGLECALENDAR_"):
        return "gcal"
    return None


def strict_read_only_required(source_config: dict | None = None) -> bool:
    source_config = source_config or {}
    if source_config.get("strict_read_only") is True:
        return True
    domains = json_list(source_config.get("allowed_email_domains"))
    if EXAMPLECO_DOMAIN in {str(domain).strip().lower().lstrip("@") for domain in domains}:
        return True
    tenant_domain = str(source_config.get("tenant_domain") or "").strip().lower().lstrip("@")
    tenant_name = str(source_config.get("tenant_name") or "").strip().lower()
    return (
        tenant_domain == EXAMPLECO_DOMAIN
        or tenant_name in {"exampleco", EXAMPLECO_DOMAIN}
        or re.search(r"(?<![a-z0-9])exampleco(?![a-z0-9])", tenant_name) is not None
    )


def default_permission_policy(source_type: str, source_config: dict | None = None) -> dict:
    source_config = source_config or {}
    strict_read_only = strict_read_only_required(source_config)
    if source_type == "slack":
        allowed_scopes = list(SLACK_ALLOWED_SCOPES)
        if strict_read_only:
            allowed_scopes = [*allowed_scopes, *SLACK_EXAMPLECO_CHANNEL_WRITE_SCOPES]
        else:
            allowed_scopes = [*allowed_scopes, *SLACK_GROUP_DM_SCOPES, *SLACK_ONE_TO_ONE_DM_READ_SCOPES]
        denied_scopes = SLACK_STRICT_READ_ONLY_DENIED_SCOPES if strict_read_only else SLACK_DM_READ_DENIED_SCOPES
        denied_resource_classes = ["slack_direct_messages"] if strict_read_only else ["slack_1_1_dm_writes"]
        return {
            "connector_provider": "composio",
            "strict_read_only": strict_read_only,
            "allowed_scopes": allowed_scopes,
            "denied_scopes": denied_scopes,
            "allowed_resources": {
                "slack_channel_ids": source_config.get("slack_channel_ids") or [],
                "slack_channel_types": ["public_channel", "private_channel"] if strict_read_only else ["public_channel", "private_channel", "mpim", "im"],
            },
            "denied_resource_classes": denied_resource_classes,
            "permission_summary": (
                "Reads selected public/private Slack channels and group messages. "
                "Reads 1-1 DM history for non-ExampleCo tenants. Does not write 1-1 DMs."
                if not strict_read_only else
                "Reads selected public/private Slack channels. "
                "Can post to selected Slack channels. Does not read or write direct messages."
            ),
        }
    if source_type == "github":
        access_mode = str(
            source_config.get("github_access_mode")
            or source_config.get("access_mode")
            or "pr_metadata"
        ).strip()
        if access_mode == "full_source":
            access_mode = "pr_metadata"
        return {
            "connector_provider": "github_app",
            "access_mode": "pr_metadata",
            "strict_read_only": strict_read_only,
            "allowed_scopes": ["pull_requests:read", "issues:read", "metadata:read"],
            "denied_scopes": [
                "repo",
                "contents:read",
                "contents:write",
                "actions:read",
                "checks:read",
                "statuses:read",
                "workflows:read",
            ],
            "allowed_resources": {
                "github_repositories": source_config.get("github_repositories") or [],
            },
            "denied_resource_classes": ["github_source_code", "github_diffs", "github_file_contents", "github_workflows"],
            "permission_summary": (
                "Reads GitHub PR metadata only: title, author, status, reviews, labels, timestamps, and URLs. "
                "Does not read source code, diffs, patches, file contents, secrets, or workflow files."
            ),
        }
    if source_type == "googledocs":
        return {
            "connector_provider": "composio",
            "strict_read_only": strict_read_only,
            "allowed_scopes": [
                "https://www.googleapis.com/auth/documents.readonly",
                "https://www.googleapis.com/auth/drive.metadata.readonly",
            ],
            "denied_scopes": [
                "drive",
                "drive.file",
                "drive.readonly",
                "https://www.googleapis.com/auth/drive",
                "https://www.googleapis.com/auth/drive.file",
                "https://www.googleapis.com/auth/drive.readonly",
            ],
            "allowed_resources": {
                "google_document_ids": source_config.get("google_document_ids") or [],
                "google_folder_ids": source_config.get("google_folder_ids") or [],
                "document_query": source_config.get("document_query"),
                "document_name": source_config.get("document_name"),
            },
            "denied_resource_classes": ["unshared_google_docs", "unselected_google_drive"],
            "permission_summary": (
                "Reads Google Docs that are explicitly shared, selected by document/folder, or matched by the configured filter."
            ),
        }
    if source_type == "gmail":
        return {
            "connector_provider": "composio",
            "allowed_scopes": [],
            "denied_scopes": [],
            "allowed_resources": {
                "gmail_owner": source_config.get("gmail_owner"),
                "gmail_query": source_config.get("gmail_query"),
                "gmail_label_ids": source_config.get("gmail_label_ids") or [],
                "gmail_sender_domains": source_config.get("gmail_sender_domains") or [],
            },
            "denied_resource_classes": ["gmail_sends", "gmail_drafts", "gmail_deletes", "gmail_labels", "gmail_contact_reads"],
            "permission_summary": (
                "Reads Gmail messages available to the connected account. Provider consent can be broader under managed OAuth, "
                "so Alloy allows only scoped email fetches at runtime."
            ),
            "provider_consent_warning": "managed_oauth_may_grant_broader_gmail_access_than_runtime_allows",
        }
    if source_type == "gcal":
        return {
            "connector_provider": "composio",
            "allowed_scopes": [],
            "denied_scopes": [],
            "allowed_resources": {"calendar_ids": source_config.get("calendar_ids") or ["primary"]},
            "denied_resource_classes": ["calendar_writes", "calendar_deletes", "calendar_invites"],
            "permission_summary": "Reads connected Google Calendar events. Does not create, update, delete, move, or invite.",
        }
    if source_type == "googledrive":
        return {
            "connector_provider": "composio",
            "allowed_scopes": [],
            "denied_scopes": [],
            "allowed_resources": {
                "google_file_ids": source_config.get("google_file_ids") or [],
                "google_folder_ids": source_config.get("google_folder_ids") or [],
            },
            "denied_resource_classes": ["drive_writes", "drive_deletes", "drive_shares", "drive_downloads", "drive_exports"],
            "permission_summary": (
                "Reads selected or connected Google Drive file metadata. Does not write, delete, share, download, or export files."
            ),
        }
    if source_type == "linear":
        return {
            "connector_provider": "composio",
            "strict_read_only": strict_read_only,
            "allowed_scopes": LINEAR_ALLOWED_SCOPES,
            "denied_scopes": LINEAR_DENIED_SCOPES,
            "allowed_resources": {"linear_workspace": "connected_oauth_workspace"},
            "denied_resource_classes": ["linear_writes"],
            "permission_summary": "Reads Linear work items, projects, owners, labels, and status. Does not write to Linear.",
        }
    if source_type == "hubspot":
        return {
            "connector_provider": "composio",
            "strict_read_only": strict_read_only,
            "allowed_scopes": HUBSPOT_ALLOWED_SCOPES,
            "denied_scopes": HUBSPOT_DENIED_SCOPES,
            "allowed_resources": {
                "hubspot_object_types": source_config.get("hubspot_object_types") or [
                    "contacts",
                    "companies",
                    "deals",
                    "owners",
                ],
            },
            "denied_resource_classes": [
                "hubspot_writes",
                "hubspot_deletes",
                "hubspot_merges",
                "hubspot_imports",
                "hubspot_exports",
                "hubspot_sensitive_properties",
            ],
            "permission_summary": (
                "Reads HubSpot CRM contacts, companies, deals, owners, and associations. "
                "Does not create, update, delete, merge, import, export, or read sensitive properties."
            ),
        }
    if source_type == "gmail":
        return {
            "connector_provider": "composio",
            "strict_read_only": strict_read_only,
            "allowed_scopes": [
                "https://www.googleapis.com/auth/gmail.readonly",
            ],
            "denied_scopes": [
                "gmail.modify",
                "gmail.compose",
                "gmail.send",
                "https://mail.google.com/",
                "https://www.googleapis.com/auth/gmail.modify",
                "https://www.googleapis.com/auth/gmail.compose",
                "https://www.googleapis.com/auth/gmail.send",
            ],
            "allowed_resources": {},
            "denied_resource_classes": ["gmail_writes", "gmail_sends", "gmail_deletes"],
            "permission_summary": "Reads Gmail messages. Does not send, draft, modify, trash, or delete email.",
        }
    if source_type == "gcal":
        return {
            "connector_provider": "composio",
            "strict_read_only": strict_read_only,
            "allowed_scopes": [
                "https://www.googleapis.com/auth/calendar.readonly",
            ],
            "denied_scopes": [
                "calendar",
                "calendar.events",
                "https://www.googleapis.com/auth/calendar",
                "https://www.googleapis.com/auth/calendar.events",
            ],
            "allowed_resources": {},
            "denied_resource_classes": ["calendar_writes", "calendar_event_mutations"],
            "permission_summary": "Reads Google Calendar events. Does not create, update, or delete calendar events.",
        }
    if source_type == "googledrive":
        return {
            "connector_provider": "composio",
            "strict_read_only": strict_read_only,
            "allowed_scopes": [
                "https://www.googleapis.com/auth/drive.metadata.readonly",
            ],
            "denied_scopes": [
                "drive",
                "drive.file",
                "drive.readonly",
                "https://www.googleapis.com/auth/drive",
                "https://www.googleapis.com/auth/drive.file",
                "https://www.googleapis.com/auth/drive.readonly",
            ],
            "allowed_resources": {
                "google_file_ids": source_config.get("google_file_ids") or [],
                "google_folder_ids": source_config.get("google_folder_ids") or [],
            },
            "denied_resource_classes": ["google_drive_writes", "unselected_google_drive"],
            "permission_summary": "Reads selected Google Drive file metadata. Does not create, update, delete, share, download, or export files.",
        }
    if source_type == "capture":
        return {
            "connector_provider": "internal",
            "strict_read_only": False,
            "allowed_scopes": [],
            "denied_scopes": [],
            "allowed_resources": {},
            "denied_resource_classes": ["capture_cross_user_reads"],
            "permission_summary": (
                "Ad-hoc notes a user pushes directly into the brain. Visible only to the "
                "user who wrote them, never team- or company-visible."
            ),
        }
    return {
        "connector_provider": "composio",
        "strict_read_only": strict_read_only,
        "allowed_scopes": [],
        "denied_scopes": [],
        "allowed_resources": {},
        "denied_resource_classes": [],
        "permission_summary": "Reads only the connected account data used by this source.",
    }


def apply_permission_policy(sync_policy: dict, source_type: str, source_config: dict | None = None) -> dict:
    policy = dict(sync_policy)
    permission_policy = default_permission_policy(source_type, source_config)
    existing = json_object(policy.get("permission_policy"))
    if existing:
        permission_policy = {
            **permission_policy,
            **existing,
            "allowed_resources": {
                **json_object(permission_policy.get("allowed_resources")),
                **json_object(existing.get("allowed_resources")),
            },
        }
    policy["permission_policy"] = permission_policy
    policy["connector_provider"] = permission_policy["connector_provider"]
    policy["allowed_resources"] = permission_policy["allowed_resources"]
    policy["denied_resource_classes"] = permission_policy["denied_resource_classes"]
    policy["permission_summary"] = permission_policy["permission_summary"]
    policy["last_permission_verification_at"] = datetime.now(timezone.utc).isoformat()
    return policy


def source_value(source, key: str, default=None):
    if source is None:
        return default
    if hasattr(source, "get"):
        return source.get(key, default)
    try:
        return source[key]
    except (KeyError, TypeError):
        return default


def permission_policy_from_source(source) -> dict:
    policy = json_object(source_value(source, "sync_policy", {}))
    source_type = source_value(source, "source_type", "")
    permission_policy = json_object(policy.get("permission_policy")) or default_permission_policy(source_type)
    top_level_allowed = json_object(policy.get("allowed_resources"))
    if top_level_allowed:
        permission_policy = {
            **permission_policy,
            "allowed_resources": {
                **json_object(permission_policy.get("allowed_resources")),
                **top_level_allowed,
            },
        }
    return permission_policy


def human_permission_summary(source_type: str, policy: dict | None = None) -> dict:
    policy = policy or default_permission_policy(source_type)
    denied_resource_classes = json_list(policy.get("denied_resource_classes"))
    can_see = {
        "slack": "Selected Slack channels and group messages" if "slack_direct_messages" not in denied_resource_classes else "Selected Slack channels",
        "github": "GitHub pull request metadata only",
        "googledocs": "Google Docs explicitly shared or selected for Inference HQ",
        "linear": "Linear work items, projects, owners, labels, and status",
        "gmail": "Connected Gmail messages available to the connected account",
        "gcal": "Connected Google Calendar events",
        "googledrive": "Selected or connected Google Drive files",
        "hubspot": "HubSpot CRM contacts, companies, deals, owners, and associations",
        "capture": "Notes you captured yourself",
    }.get(source_type, "Connected source data")
    cannot_see = {
        "slack": (
            "Slack direct messages"
            if "slack_direct_messages" in denied_resource_classes
            else "Slack 1-1 DMs"
            if "slack_1_1_dm" in denied_resource_classes
            else "Slack 1-1 DM writes"
        ),
        "github": "Source code, diffs, patches, file contents, secrets, or workflow files",
        "googledocs": "Unshared or unselected Google Docs",
        "gmail": "Gmail sends, drafts, deletes, labels, or contacts",
        "gcal": "Calendar writes, deletes, moves, or invites",
        "googledrive": "Drive writes, deletes, shares, downloads, or exports",
        "linear": "Linear writes or mutations",
        "hubspot": "HubSpot writes, deletes, merges, imports, exports, or sensitive properties",
        "capture": "Other users' captured notes",
    }.get(source_type, "")
    return {
        "can_see": can_see,
        "cannot_see": cannot_see,
        "summary": policy.get("permission_summary") or can_see,
        "allowed_resources": json_object(policy.get("allowed_resources")),
        "denied_resource_classes": json_list(policy.get("denied_resource_classes")),
        "permission_confidence": "high",
    }


def assert_granted_scopes_allowed(source_type: str, granted_scopes: list[str], policy: dict) -> None:
    denied = {scope.lower() for scope in json_list(policy.get("denied_scopes"))}
    granted = {str(scope).lower() for scope in granted_scopes}
    overlap = sorted(granted & denied)
    if overlap:
        raise PermissionDenied(
            f"{source_type} connection granted denied scope(s): {', '.join(overlap)}",
            source_type=source_type,
        )
    if source_type == "hubspot":
        denied_by_suffix = sorted(
            scope for scope in granted
            if scope.endswith(HUBSPOT_DENIED_SCOPE_SUFFIXES)
        )
        if denied_by_suffix:
            raise PermissionDenied(
                f"hubspot connection granted denied scope(s): {', '.join(denied_by_suffix)}",
                source_type=source_type,
            )
    if policy.get("strict_read_only"):
        denied_markers = {
            "gmail": GMAIL_DENIED_SCOPE_MARKERS,
            "gcal": GCAL_DENIED_SCOPE_MARKERS,
            "googledocs": GOOGLEDOCS_DENIED_SCOPE_MARKERS,
            "googledrive": GOOGLEDRIVE_DENIED_SCOPE_MARKERS,
        }.get(source_type, ())
        marker_hits = sorted(
            scope for scope in granted
            if any(marker in scope for marker in denied_markers)
            and "readonly" not in scope
        )
        if marker_hits:
            raise PermissionDenied(
                f"{source_type} connection granted denied scope(s): {', '.join(marker_hits)}",
                source_type=source_type,
            )


async def log_permission_denied(
    *,
    tenant_id: str | None,
    source_id: str | None,
    source_type: str | None,
    tool_slug: str | None,
    reason: str,
) -> None:
    if not tenant_id:
        return
    async with acquire() as conn:
        await conn.execute(
            """
            INSERT INTO system_issues (tenant_id, kind, severity, detail)
            VALUES ($1, 'permission_denied', 'high', $2::jsonb)
            """,
            tenant_id,
            json.dumps({
                "source_id": source_id,
                "source_type": source_type,
                "tool_slug": tool_slug,
                "reason": reason,
            }),
        )


def assert_tool_allowed(source, slug: str, arguments: dict | None = None) -> None:
    arguments = arguments or {}
    source_type = source_value(source, "source_type") or source_type_from_slug(slug)
    policy = permission_policy_from_source(source)
    granted_scopes = json_list(source_value(source, "granted_scopes", []))
    assert_granted_scopes_allowed(source_type, granted_scopes, policy)

    upper = slug.upper()
    if source_type == "slack":
        denied_resource_classes = json_list(policy.get("denied_resource_classes"))
        direct_messages_denied = "slack_direct_messages" in denied_resource_classes
        if upper in {"SLACK_LIST_ALL_CHANNELS"}:
            requested = {item.strip() for item in str(arguments.get("types") or "").split(",")}
            if "im" in requested and ("slack_1_1_dm" in denied_resource_classes or direct_messages_denied):
                raise PermissionDenied("Slack 1-1 DM listing is denied", source_type=source_type, tool_slug=slug)
            if "mpim" in requested and direct_messages_denied:
                raise PermissionDenied("Slack group DM listing is denied", source_type=source_type, tool_slug=slug)
        if upper in {"SLACK_FETCH_CONVERSATION_HISTORY", "SLACK_SEND_MESSAGE"}:
            channel = str(arguments.get("channel") or "")
            if channel.startswith("D") and ("slack_1_1_dm" in denied_resource_classes or direct_messages_denied):
                raise PermissionDenied("Slack 1-1 DM access is denied", source_type=source_type, tool_slug=slug)
            if channel.startswith("D") and upper == "SLACK_SEND_MESSAGE":
                raise PermissionDenied("Slack 1-1 DM writes are denied", source_type=source_type, tool_slug=slug)
            allowed_channels = json_object(policy.get("allowed_resources")).get("slack_channel_ids") or []
            if allowed_channels and channel and channel not in allowed_channels:
                raise PermissionDenied(f"Slack channel {channel} is not allowlisted", source_type=source_type, tool_slug=slug)

    if source_type == "github":
        if upper not in GITHUB_ALLOWED_TOOLS or any(marker in upper for marker in GITHUB_DENIED_TOOL_MARKERS):
            raise PermissionDenied(
                f"GitHub tool {slug} is outside PR metadata access",
                source_type=source_type,
                tool_slug=slug,
            )
        if str(arguments.get("response_detail") or "").lower() != "minimal":
            raise PermissionDenied("GitHub PR metadata access requires response_detail=minimal", source_type=source_type, tool_slug=slug)

    if source_type == "gmail":
        if upper not in GMAIL_ALLOWED_TOOLS or any(marker in upper for marker in GMAIL_DENIED_TOOL_MARKERS):
            raise PermissionDenied(
                f"Gmail tool {slug} is outside read-only email fetch access",
                source_type=source_type,
                tool_slug=slug,
            )
        allowed_resources = json_object(policy.get("allowed_resources"))
        configured_query = str(allowed_resources.get("gmail_query") or "").strip()
        if configured_query:
            query = str(arguments.get("query") or "")
            if configured_query.lower() not in query.lower():
                raise PermissionDenied(
                    "Gmail fetch query is missing the configured Gmail query scope",
                    source_type=source_type,
                    tool_slug=slug,
                )

    if source_type == "googledocs":
        if upper not in GOOGLEDOCS_ALLOWED_TOOLS or any(marker in upper for marker in GOOGLEDOCS_DENIED_TOOL_MARKERS):
            raise PermissionDenied(
                f"Google Docs tool {slug} is outside read-only document access",
                source_type=source_type,
                tool_slug=slug,
            )
        allowed_resources = json_object(policy.get("allowed_resources"))
        document_ids = [str(item).strip() for item in json_list(allowed_resources.get("google_document_ids")) if str(item).strip()]
        folder_ids = [str(item).strip() for item in json_list(allowed_resources.get("google_folder_ids")) if str(item).strip()]
        configured_query = str(allowed_resources.get("document_query") or "").strip()
        document_name = str(allowed_resources.get("document_name") or "").strip()
        if upper == "GOOGLEDOCS_GET_DOCUMENT_PLAINTEXT" and document_ids:
            document_id = str(arguments.get("document_id") or arguments.get("documentId") or "").strip()
            if document_id not in document_ids:
                raise PermissionDenied(
                    f"Google Docs document {document_id or '<missing>'} is not allowlisted",
                    source_type=source_type,
                    tool_slug=slug,
                )
        if upper == "GOOGLEDOCS_SEARCH_DOCUMENTS":
            query = str(arguments.get("query") or "")
            missing = []
            if configured_query and configured_query.lower() not in query.lower():
                missing.append(configured_query)
            elif document_name and document_name.lower() not in query.lower():
                missing.append(document_name)
            missing.extend(resource_id for resource_id in document_ids + folder_ids if resource_id not in query)
            if missing:
                raise PermissionDenied(
                    f"Google Docs search is missing configured document scope: {', '.join(missing)}",
                    source_type=source_type,
                    tool_slug=slug,
                )

    if source_type == "googledrive":
        if upper not in GOOGLEDRIVE_ALLOWED_TOOLS or any(marker in upper for marker in GOOGLEDRIVE_DENIED_TOOL_MARKERS):
            raise PermissionDenied(
                f"Google Drive tool {slug} is outside read-only file metadata access",
                source_type=source_type,
                tool_slug=slug,
            )
        allowed_resources = json_object(policy.get("allowed_resources"))
        query = str(arguments.get("query") or "")
        file_ids = [str(item).strip() for item in json_list(allowed_resources.get("google_file_ids")) if str(item).strip()]
        folder_ids = [str(item).strip() for item in json_list(allowed_resources.get("google_folder_ids")) if str(item).strip()]
        missing = [resource_id for resource_id in file_ids + folder_ids if resource_id not in query]
        if missing:
            raise PermissionDenied(
                f"Google Drive query is missing configured resource scope: {', '.join(missing)}",
                source_type=source_type,
                tool_slug=slug,
            )

    if source_type == "gcal":
        if upper not in GOOGLECALENDAR_ALLOWED_TOOLS or any(marker in upper for marker in GOOGLECALENDAR_DENIED_TOOL_MARKERS):
            raise PermissionDenied(
                f"Google Calendar tool {slug} is outside read-only event access",
                source_type=source_type,
                tool_slug=slug,
            )

    if source_type == "linear":
        if not upper.startswith("LINEAR_LIST_") and not upper.startswith("LINEAR_GET_"):
            raise PermissionDenied("Linear is read-only for this tenant", source_type=source_type, tool_slug=slug)

    if source_type == "hubspot":
        if upper not in HUBSPOT_ALLOWED_TOOLS or any(marker in upper for marker in HUBSPOT_DENIED_TOOL_MARKERS):
            raise PermissionDenied(
                f"HubSpot tool {slug} is outside read-only CRM access",
                source_type=source_type,
                tool_slug=slug,
            )
    if source_type == "gmail":
        if upper not in GMAIL_ALLOWED_TOOLS or any(marker in upper for marker in GMAIL_DENIED_TOOL_MARKERS):
            raise PermissionDenied("Gmail is read-only for this tenant", source_type=source_type, tool_slug=slug)
    if source_type == "gcal":
        if upper not in GCAL_ALLOWED_TOOLS or any(marker in upper for marker in GCAL_DENIED_TOOL_MARKERS):
            raise PermissionDenied("Google Calendar is read-only for this tenant", source_type=source_type, tool_slug=slug)
    if source_type == "googledrive":
        if upper not in GOOGLEDRIVE_ALLOWED_TOOLS or any(marker in upper for marker in GOOGLEDRIVE_DENIED_TOOL_MARKERS):
            raise PermissionDenied("Google Drive is read-only for this tenant", source_type=source_type, tool_slug=slug)
    if source_type == "googledocs":
        if upper not in GOOGLEDOCS_ALLOWED_TOOLS or any(marker in upper for marker in GOOGLEDOCS_DENIED_TOOL_MARKERS):
            raise PermissionDenied("Google Docs is read-only for this tenant", source_type=source_type, tool_slug=slug)


def sanitize_github_pull_request(pr: dict) -> dict:
    """Keep PR metadata and drop code, diff, patch, file, and body fields."""
    sanitized = {
        key: value
        for key, value in pr.items()
        if key in GITHUB_ALLOWED_METADATA_FIELDS and key not in GITHUB_DENIED_FIELDS
    }
    for key in list(sanitized.keys()):
        if key.lower() in GITHUB_DENIED_FIELDS:
            sanitized.pop(key, None)
    return sanitized


def sanitize_source_payload(source_type: str, payload: dict) -> dict:
    """Remove disallowed provider fields before raw storage or normalization."""
    if source_type == "github":
        return _drop_github_denied_fields(payload)
    if source_type == "hubspot":
        return _sanitize_hubspot_payload(payload)
    return payload


def denied_payload_reason(source_type: str, payload: dict, policy: dict | None = None) -> str | None:
    """Return why an incoming provider event must not be processed."""
    if source_type == "slack":
        policy = policy or default_permission_policy(source_type, {"strict_read_only": True})
        event = payload.get("event") if isinstance(payload.get("event"), dict) else payload
        channel = str(event.get("channel") or "")
        channel_type = str(event.get("channel_type") or payload.get("channel_type") or "").strip()
        denied_resource_classes = json_list(policy.get("denied_resource_classes"))
        direct_messages_denied = "slack_direct_messages" in denied_resource_classes
        if channel.startswith("D") and ("slack_1_1_dm" in denied_resource_classes or direct_messages_denied):
            return "Slack 1-1 DM event is denied"
        if channel_type == "mpim" and direct_messages_denied:
            return "Slack group DM event is denied"
    return None


def _drop_github_denied_fields(value):
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if key.lower() in GITHUB_DENIED_FIELDS:
                continue
            result[key] = _drop_github_denied_fields(item)
        return result
    if isinstance(value, list):
        return [_drop_github_denied_fields(item) for item in value]
    return value


def _argument_contains_value(value: Any, expected: str) -> bool:
    if isinstance(value, dict):
        return any(_argument_contains_value(item, expected) for item in value.values())
    if isinstance(value, list):
        return any(_argument_contains_value(item, expected) for item in value)
    return str(value).strip() == expected


def _sanitize_hubspot_payload(payload: dict) -> dict:
    result = {}
    for key, value in payload.items():
        if key == "record" and isinstance(value, dict):
            result[key] = _sanitize_hubspot_record(value)
        elif key == "properties" and isinstance(value, dict):
            result[key] = _sanitize_hubspot_properties(value)
        elif key in {"event_type", "object_type", "objectType", "type"}:
            result[key] = value
        elif key in HUBSPOT_SAFE_RECORD_FIELDS:
            result[key] = value
    return result


def _sanitize_hubspot_record(record: dict) -> dict:
    result = {
        key: value
        for key, value in record.items()
        if key in HUBSPOT_SAFE_RECORD_FIELDS
    }
    properties = record.get("properties")
    if isinstance(properties, dict):
        result["properties"] = _sanitize_hubspot_properties(properties)
    return result


def _sanitize_hubspot_properties(properties: dict) -> dict:
    return {
        key: value
        for key, value in properties.items()
        if key in HUBSPOT_SAFE_PROPERTIES and value not in (None, "")
    }
