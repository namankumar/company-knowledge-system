"""Pure projection: artifact DB rows -> API payloads.

Follows invariant 4: every user-visible artifact payload passes destination
permission filtering (`app/core/destination_permissions.py`) via
`artifact_visible_to_destination`. This is the single ACL check for
"can this principal see this artifact" -- `app/api/artifacts.py`'s
`_can_view`/`list_all` call straight into this module rather than
re-implementing the check, so there is one place this logic can drift.
"""
from typing import Any

from app.core.destination_permissions import source_visible_to_principals


def _as_source(artifact: dict[str, Any]) -> dict[str, Any]:
    return {
        "ref": f"artifact:{artifact['id']}",
        "allowed_principals": artifact.get("allowed_principals") or [],
        "acl_state": "stamped",
        "acl_freshness_state": "fresh",
    }


def artifact_visible_to_destination(artifact: dict[str, Any], destination_principals: list[str] | None) -> bool:
    return source_visible_to_principals(_as_source(artifact), destination_principals)


def project_artifact(artifact: dict[str, Any]) -> dict[str, Any]:
    """Single-artifact payload for a caller already authorized to view it
    (owner or company-scope member, checked by the API route)."""
    return {
        "id": artifact["id"],
        "kind": artifact["kind"],
        "title": artifact["title"],
        "content": artifact["content"],
        "current_version": artifact["current_version"],
        "status": artifact["status"],
        "visibility": artifact["visibility"],
        "verification_status": artifact["verification_status"],
        "verification_detail": artifact["verification_detail"],
        "metadata": artifact.get("metadata") or {},
        "created_by": artifact["created_by"],
        "updated_by": artifact["updated_by"],
        "created_at": artifact["created_at"],
        "updated_at": artifact["updated_at"],
    }


def project_artifact_list(
    artifacts: list[dict[str, Any]],
    *,
    destination_principals: list[str] | None = None,
) -> list[dict[str, Any]]:
    """List projection for a destination context (e.g. a company-wide surface).

    Filters out any artifact whose `allowed_principals` does not overlap the
    destination's principals -- an owner-private artifact never appears in a
    company-wide destination, even if it was somehow included in the input
    list.
    """
    return [
        project_artifact(artifact)
        for artifact in artifacts
        if artifact_visible_to_destination(artifact, destination_principals)
    ]


def project_artifact_version(version: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": version["id"],
        "artifact_id": version["artifact_id"],
        "version_number": version["version_number"],
        "content": version["content"],
        "citations": version["citations"],
        "verification_status": version["verification_status"],
        "verification_detail": version["verification_detail"],
        "created_by": version["created_by"],
        "created_at": version["created_at"],
    }
