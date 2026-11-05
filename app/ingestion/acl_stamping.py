"""ACL stamping for live ingestion.

This module sits at the source boundary. It turns provider-shaped permission
payloads into an Alloy ACL envelope once, upserts the source resource snapshot,
and returns the exact fields downstream rows should inherit.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any

import structlog

from app.core.db import acquire
from app.core.permission_adapters import provider_acl_to_envelope
from app.core.permission_audit import report_permission_incident
from app.core.permissions import normalize_email

logger = structlog.get_logger()


@dataclass(frozen=True)
class ACLStamp:
    source_resource_id: str
    visibility: str
    acl_basis: str
    acl_confidence: str
    allowed_principals: list[str]
    acl_state: str
    acl_freshness_state: str
    provider_acl: dict[str, Any]
    resource_type: str
    external_id: str


async def stamp_source_resource_acl(
    *,
    tenant_id: str,
    source_id: str,
    source_type: str,
    external_event_id: str,
    payload: dict[str, Any],
) -> ACLStamp:
    """Resolve a source resource ACL and return fields for event row writes."""
    async with acquire() as conn:
        source = await conn.fetchrow(
            """
            SELECT id, tenant_id, source_type, external_id, sync_policy
            FROM sources
            WHERE id = $1 AND tenant_id = $2
            """,
            source_id,
            tenant_id,
        )
        if not source:
            provider_acl = {"source": {"id": source_id, "missing": True}}
            envelope = _review_envelope("source_missing")
            descriptor = ResourceDescriptor("unknown_resource", external_event_id, None, None)
        else:
            source_row = _row_dict(source)
            source_row["sync_policy"] = _json_dict(source_row.get("sync_policy"))
            provider_acl = _provider_acl_payload(source_type, payload, source_row, external_event_id)
            mappings = await _principal_mappings(conn, tenant_id)
            envelope = _safe_envelope(source_type, provider_acl, mappings, source_row=source_row, tenant_id=tenant_id)
            descriptor = _resource_descriptor(source_type, provider_acl, payload, external_event_id)

        resource_id = await conn.fetchval(
            """
            INSERT INTO source_resources
                (tenant_id, source_id, source_type, resource_type, external_id, name, owner_user_id,
                 acl_basis, acl_confidence, visibility, allowed_principals, provider_acl,
                 acl_state, acl_freshness_state, last_acl_synced_at, access_revoked_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7,
                    $8, $9, $10, $11::jsonb, $12::jsonb,
                    $13, $14, now(), CASE WHEN $14 = 'revoked' THEN now() ELSE NULL END)
            ON CONFLICT (tenant_id, source_id, resource_type, external_id) DO UPDATE
            SET name = COALESCE(EXCLUDED.name, source_resources.name),
                owner_user_id = COALESCE(EXCLUDED.owner_user_id, source_resources.owner_user_id),
                acl_basis = EXCLUDED.acl_basis,
                acl_confidence = EXCLUDED.acl_confidence,
                visibility = EXCLUDED.visibility,
                allowed_principals = EXCLUDED.allowed_principals,
                provider_acl = EXCLUDED.provider_acl,
                acl_state = EXCLUDED.acl_state,
                acl_freshness_state = EXCLUDED.acl_freshness_state,
                last_acl_synced_at = now(),
                access_revoked_at = CASE
                    WHEN EXCLUDED.acl_freshness_state = 'revoked'
                    THEN COALESCE(source_resources.access_revoked_at, now())
                    ELSE NULL
                END,
                updated_at = now()
            RETURNING id
            """,
            tenant_id,
            source_id,
            source_type,
            descriptor.resource_type,
            descriptor.external_id,
            descriptor.name,
            descriptor.owner_user_id,
            envelope["acl_basis"],
            envelope["acl_confidence"],
            envelope["visibility"],
            json.dumps(envelope["allowed_principals"]),
            json.dumps(provider_acl),
            envelope["acl_state"],
            envelope["acl_freshness_state"],
        )

    stamp = ACLStamp(
        source_resource_id=str(resource_id),
        visibility=envelope["visibility"],
        acl_basis=envelope["acl_basis"],
        acl_confidence=envelope["acl_confidence"],
        allowed_principals=list(envelope["allowed_principals"]),
        acl_state=envelope["acl_state"],
        acl_freshness_state=envelope["acl_freshness_state"],
        provider_acl=provider_acl,
        resource_type=descriptor.resource_type,
        external_id=descriptor.external_id,
    )
    await _report_if_restricted(tenant_id=tenant_id, stamp=stamp, source_type=source_type)
    return stamp


@dataclass(frozen=True)
class ResourceDescriptor:
    resource_type: str
    external_id: str
    name: str | None
    owner_user_id: str | None


async def _principal_mappings(conn, tenant_id: str) -> dict[tuple[str, str, str], str]:
    rows = await conn.fetch(
        """
        SELECT source_type, provider_principal_type, provider_principal_id, alloy_principal_key
        FROM principal_identity_mappings
        WHERE tenant_id = $1
          AND mapping_state = 'mapped'
          AND confidence IN ('medium', 'high')
          AND alloy_principal_key IS NOT NULL
        """,
        tenant_id,
    )
    return {
        (
            str(row["source_type"]),
            str(row["provider_principal_type"]),
            str(row["provider_principal_id"]),
        ): str(row["alloy_principal_key"])
        for row in rows
    }


def _safe_envelope(
    source_type: str,
    provider_acl: dict[str, Any],
    mappings: dict[tuple[str, str, str], str],
    *,
    source_row: dict[str, Any] | None = None,
    tenant_id: str | None = None,
) -> dict[str, Any]:
    envelope = provider_acl_to_envelope(source_type, provider_acl, principal_mappings=mappings)
    if envelope.get("acl_state") != "stamped" and source_row and tenant_id:
        policy_envelope = _source_policy_envelope(source_type, provider_acl, source_row, tenant_id)
        if policy_envelope:
            envelope = policy_envelope
    if envelope.get("acl_state") == "stamped" and not envelope.get("allowed_principals"):
        return _review_envelope("empty_allowed_principals")
    return {
        "visibility": str(envelope["visibility"]),
        "allowed_principals": [str(principal) for principal in envelope["allowed_principals"]],
        "acl_basis": str(envelope["acl_basis"]),
        "acl_confidence": str(envelope["acl_confidence"]),
        "acl_state": str(envelope["acl_state"]),
        "acl_freshness_state": str(envelope["acl_freshness_state"]),
    }


def _source_policy_envelope(
    source_type: str,
    provider_acl: dict[str, Any],
    source_row: dict[str, Any],
    tenant_id: str,
) -> dict[str, Any] | None:
    """Use explicit tenant source allowlists when provider ACL lacks principals.

    This is deliberately narrower than a generic fallback. It only promotes
    resources the tenant/operator already allowlisted in source sync policy.
    """
    policy = _json_dict(source_row.get("sync_policy"))
    allowed = _merged_allowed_resources(policy)
    if source_type == "slack":
        channel = provider_acl.get("channel") or {}
        channel_id = str(channel.get("id") or "")
        channel_types = {str(value) for value in _json_list(allowed.get("slack_channel_types"))}
        channel_ids = {str(value) for value in _json_list(allowed.get("slack_channel_ids"))}
        denied_classes = set(_json_list(policy.get("denied_resource_classes")))
        denied_classes.update(_json_list(_json_dict(policy.get("permission_policy")).get("denied_resource_classes")))
        if channel.get("is_private") is False and "slack_1_1_dm" in denied_classes:
            if (channel_ids and channel_id in channel_ids) or (not channel_ids and "public_channel" in channel_types):
                return _company_policy_envelope(tenant_id, confidence="medium")
    if source_type == "github":
        access_mode = str(
            policy.get("github_access_mode")
            or _json_dict(policy.get("permission_policy")).get("github_access_mode")
            or _json_dict(policy.get("permission_policy")).get("access_mode")
            or ""
        ).lower()
        repo_name = str((provider_acl.get("repository") or {}).get("full_name") or "")
        allowed_repos = {_github_allowed_repo_name(repo) for repo in _json_list(allowed.get("github_repositories"))}
        allowed_repos = {repo for repo in allowed_repos if repo}
        if access_mode == "pr_metadata" and repo_name and repo_name.lower() in allowed_repos:
            return _company_policy_envelope(tenant_id)
    if source_type == "linear":
        if provider_acl.get("guest"):
            return None
        team = provider_acl.get("team") or {}
        teams = provider_acl.get("teams") or []
        if team.get("private") or any(isinstance(item, dict) and item.get("private") for item in teams):
            return None
        project = provider_acl.get("project") or {}
        if allowed.get("linear_workspace") == "connected_oauth_workspace":
            return _company_policy_envelope(tenant_id, confidence="medium")
        allowed_team_ids = {str(value) for value in _json_list(allowed.get("linear_team_ids"))}
        allowed_project_ids = {str(value) for value in _json_list(allowed.get("linear_project_ids"))}
        team_id = team.get("id") if isinstance(team, dict) else None
        project_id = project.get("id") if isinstance(project, dict) else None
        if (team_id and str(team_id) in allowed_team_ids) or (project_id and str(project_id) in allowed_project_ids):
            return _company_policy_envelope(tenant_id, confidence="medium")
    return None


def _company_policy_envelope(tenant_id: str, *, confidence: str = "high") -> dict[str, Any]:
    return {
        "visibility": "company",
        "allowed_principals": [f"company:{tenant_id}"],
        "acl_basis": "tenant_source_policy",
        "acl_confidence": confidence,
        "acl_state": "stamped",
        "acl_freshness_state": "fresh",
    }


def _merged_allowed_resources(policy: dict[str, Any]) -> dict[str, Any]:
    permission_policy = _json_dict(policy.get("permission_policy"))
    return {
        **_json_dict(permission_policy.get("allowed_resources")),
        **_json_dict(policy.get("allowed_resources")),
    }


def _review_envelope(reason: str) -> dict[str, Any]:
    return {
        "visibility": "restricted",
        "allowed_principals": [f"unknown:acl:{reason}"],
        "acl_basis": "provider_snapshot",
        "acl_confidence": "low",
        "acl_state": "needs_permission_review",
        "acl_freshness_state": "needs_permission_review",
    }


def _provider_acl_payload(
    source_type: str,
    payload: dict[str, Any],
    source: dict[str, Any],
    external_event_id: str,
) -> dict[str, Any]:
    if source_type == "gmail":
        sync_policy = _json_dict(source.get("sync_policy"))
        connected_user = _alloy_user_principal(sync_policy.get("connected_user_id"))
        account_email = sync_policy.get("connected_email") or sync_policy.get("email") or source.get("external_id")
        thread_id = _first(payload, "thread_id", "threadId", default=None) or (
            (payload.get("thread") or {}).get("id") if isinstance(payload.get("thread"), dict) else None
        )
        return {
            "connected_account": {
                "alloy_user_id": connected_user,
                "email": account_email,
            },
            "thread": {
                "id": thread_id or external_event_id,
                "participants": _gmail_participants(payload),
            },
            "labels": _first(payload, "label_ids", "labelIds", "labels", default=[]),
        }
    if source_type == "slack":
        event = payload.get("event") if isinstance(payload.get("event"), dict) else payload
        channel = payload.get("channel") if isinstance(payload.get("channel"), dict) else {}
        channel_id = channel.get("id") or _first(event, "channel", "channel_id")
        team_id = (payload.get("team") or {}).get("id") if isinstance(payload.get("team"), dict) else None
        channel_privacy = None
        if "is_private" in channel:
            channel_privacy = channel.get("is_private")
        elif event.get("channel_type") in {"channel", "group", "im", "mpim"}:
            channel_privacy = event.get("channel_type") != "channel"
        return _without_empty({
            "team": _without_empty({"id": team_id or _first(event, "team", "team_id")}),
            "channel": _without_empty({
                "id": channel_id,
                "name": channel.get("name"),
                "is_private": channel_privacy,
            }),
            "members": payload.get("members"),
            "usergroups": payload.get("usergroups"),
            "removed_members": payload.get("removed_members"),
        })
    if source_type == "github":
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        repo = payload.get("repository") or data.get("repository") or {}
        org = payload.get("organization") or data.get("organization") or {}
        return _without_empty({
            "organization": _without_empty({
                "login": org.get("login"),
                "mapped_to_company": org.get("mapped_to_company"),
            }),
            "repository": _without_empty({
                "full_name": repo.get("full_name") or repo.get("fullName") or repo.get("name"),
                "visibility": repo.get("visibility"),
            }),
            "teams": payload.get("teams") or data.get("teams"),
            "collaborators": payload.get("collaborators") or data.get("collaborators"),
            "outside_collaborators": payload.get("outside_collaborators") or data.get("outsideCollaborators"),
        })
    if source_type in {"googledocs", "googledrive"}:
        file_data = payload.get("file") or payload.get("document") or payload.get("data") or {}
        file_id = (
            payload.get("fileId")
            or payload.get("file_id")
            or file_data.get("id")
            or file_data.get("fileId")
            or file_data.get("document_id")
        )
        key = "document" if source_type == "googledocs" else "file"
        return _without_empty({
            "fileId": file_id if source_type == "googledocs" else None,
            key: _without_empty({
                "id": file_id,
                "name": file_data.get("name") or file_data.get("title"),
                "mimeType": file_data.get("mimeType") or file_data.get("mime_type"),
            }),
            "drive": payload.get("drive"),
            "parents": payload.get("parents") or file_data.get("parents"),
            "inherited": payload.get("inherited"),
            "permissions": payload.get("permissions") or file_data.get("permissions"),
            "last_acl_synced_at": payload.get("last_acl_synced_at") or file_data.get("last_acl_synced_at"),
            "access_revoked_at": payload.get("access_revoked_at") or file_data.get("access_revoked_at"),
            "acl_freshness_state": payload.get("acl_freshness_state") or file_data.get("acl_freshness_state"),
        })
    if source_type == "gcal":
        event = payload.get("event") if isinstance(payload.get("event"), dict) else payload
        return _without_empty({
            "calendar": payload.get("calendar") or _without_empty({
                "id": _first(payload, "calendar_id", "calendarId", default=None),
                "accessRole": payload.get("accessRole"),
            }),
            "calendar_acl": payload.get("calendar_acl") or payload.get("calendarAcl"),
            "event": _without_empty({
                "id": event.get("id") or external_event_id,
                "visibility": event.get("visibility"),
                "status": event.get("status"),
            }),
        })
    if source_type == "linear":
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        issue = data.get("issue") if isinstance(data.get("issue"), dict) else data
        team = payload.get("team") or data.get("team") or issue.get("team")
        return _without_empty({
            "workspace": payload.get("workspace") or data.get("workspace"),
            "team": team,
            "teams": payload.get("teams") or data.get("teams"),
            "project": payload.get("project") or data.get("project") or issue.get("project"),
            "issue": _without_empty({
                "id": issue.get("id") or issue.get("identifier"),
                "team": issue.get("team"),
            }),
            "guest": payload.get("guest") or data.get("guest"),
        })
    if source_type == "hubspot":
        record = payload.get("record") if isinstance(payload.get("record"), dict) else payload
        properties = record.get("properties") or {}
        pipeline = properties.get("pipeline") or properties.get("hs_pipeline")
        return _without_empty({
            "object_type": payload.get("object_type") or payload.get("objectType"),
            "policy": payload.get("policy"),
            "pipeline_access": payload.get("pipeline_access") or payload.get("pipelineAccess") or _without_empty({"pipeline": pipeline}),
            "record": _without_empty({
                "id": record.get("id") or properties.get("hs_object_id"),
                "properties": _without_empty({
                    "hubspot_owner_id": properties.get("hubspot_owner_id"),
                    "pipeline": pipeline,
                }),
            }),
            "owners": payload.get("owners"),
        })
    return {"source": {"type": source_type, "external_event_id": external_event_id}}


def _resource_descriptor(
    source_type: str,
    provider_acl: dict[str, Any],
    payload: dict[str, Any],
    external_event_id: str,
) -> ResourceDescriptor:
    if source_type == "gmail":
        thread = provider_acl.get("thread") or {}
        owner = (provider_acl.get("connected_account") or {}).get("alloy_user_id")
        return ResourceDescriptor("gmail_thread", str(thread.get("id") or external_event_id), None, _principal_user_id(owner))
    if source_type == "slack":
        channel = provider_acl.get("channel") or {}
        channel_id = channel.get("id") or _first(payload.get("event", payload), "channel", "channel_id")
        return ResourceDescriptor("slack_channel", str(channel_id or external_event_id), channel.get("name"), None)
    if source_type == "github":
        repo = provider_acl.get("repository") or {}
        full_name = repo.get("full_name") or _github_repo_name(payload)
        return ResourceDescriptor("github_repo", str(full_name or external_event_id), full_name, None)
    if source_type == "googledocs":
        document = provider_acl.get("document") or payload.get("document") or {}
        file_id = provider_acl.get("fileId") or document.get("id") or document.get("document_id")
        return ResourceDescriptor("google_doc", str(file_id or external_event_id), document.get("name") or document.get("title"), None)
    if source_type == "googledrive":
        file_data = provider_acl.get("file") or payload.get("file") or payload.get("data") or payload
        return ResourceDescriptor(
            "google_drive_file",
            str(file_data.get("id") or file_data.get("file_id") or file_data.get("fileId") or external_event_id),
            file_data.get("name") or file_data.get("title"),
            None,
        )
    if source_type == "gcal":
        event = provider_acl.get("event") or payload
        calendar = provider_acl.get("calendar") or {}
        event_id = event.get("id") or external_event_id
        calendar_id = calendar.get("id") or _first(payload, "calendar_id", "calendarId", default="primary")
        return ResourceDescriptor("calendar_event", f"{calendar_id}:{event_id}", event.get("summary"), None)
    if source_type == "linear":
        issue = provider_acl.get("issue") or payload.get("issue") or payload
        issue_id = issue.get("id") or issue.get("identifier") or external_event_id
        return ResourceDescriptor("linear_issue", str(issue_id), issue.get("title"), None)
    if source_type == "hubspot":
        record = provider_acl.get("record") or payload.get("record") or payload
        properties = record.get("properties") or {}
        object_type = provider_acl.get("object_type") or payload.get("object_type") or "record"
        record_id = record.get("id") or properties.get("hs_object_id") or external_event_id
        name = properties.get("name") or properties.get("dealname") or record.get("name")
        return ResourceDescriptor(f"hubspot_{object_type}", str(record_id), name, None)
    return ResourceDescriptor(f"{source_type}_event", external_event_id, None, None)


async def _report_if_restricted(*, tenant_id: str, stamp: ACLStamp, source_type: str) -> None:
    if stamp.acl_state == "stamped" and stamp.acl_freshness_state == "fresh":
        return
    try:
        await report_permission_incident(
            tenant_id=tenant_id,
            incident_type="source_acl_needs_review",
            object_kind="source_resource",
            source_resource_id=stamp.source_resource_id,
            target_audience=stamp.visibility,
            expected_decision="deny",
            actual_decision="deny",
            decision_trace={
                "acl_state": stamp.acl_state,
                "acl_freshness_state": stamp.acl_freshness_state,
                "acl_basis": stamp.acl_basis,
                "acl_confidence": stamp.acl_confidence,
            },
            safe_debug_context={
                "source_type": source_type,
                "resource_type": stamp.resource_type,
                "external_id": stamp.external_id,
                "allowed_principal_count": len(stamp.allowed_principals),
            },
        )
    except Exception as exc:  # pragma: no cover - incident logging is non-critical to fail-closed stamping
        logger.warning("permission_incident_report_failed", source_type=source_type, error=str(exc))


def _gmail_participants(payload: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for key in ("sender", "from", "from_email", "to", "recipient", "cc", "bcc"):
        raw = payload.get(key)
        if isinstance(raw, list):
            values.extend(str(item) for item in raw)
        elif raw:
            values.append(str(raw))
    participants: list[str] = []
    for raw in values:
        for part in raw.split(","):
            email = normalize_email(part.strip().strip("<>"))
            if email and "@" in email:
                participants.append(email)
    return _dedupe(participants)


def _github_repo_name(payload: dict[str, Any]) -> str | None:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    repo = data.get("repository") if isinstance(data.get("repository"), dict) else {}
    return repo.get("full_name") or repo.get("name")


def _principal_user_id(principal: str | None) -> str | None:
    if not principal or not principal.startswith("user:"):
        return None
    return principal.removeprefix("user:")


def _alloy_user_principal(value: Any) -> str | None:
    if value in (None, ""):
        return None
    raw = str(value)
    return raw if raw.startswith("user:") else f"user:{raw}"


def _json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        if isinstance(parsed, str):
            try:
                parsed = json.loads(parsed)
            except json.JSONDecodeError:
                return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _github_allowed_repo_name(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("full_name") or value.get("fullName") or "").strip().lower()
    return str(value).strip().lower()


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return [value]
        return parsed if isinstance(parsed, list) else [parsed]
    if value in (None, ""):
        return []
    return [value]


def _row_dict(row: Any) -> dict[str, Any]:
    if isinstance(row, dict):
        return dict(row)
    return {key: row[key] for key in row.keys()}


def _first(d: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        value = d.get(key)
        if value not in (None, ""):
            return value
    return default


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _without_empty(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if item not in (None, "", [], {})}
