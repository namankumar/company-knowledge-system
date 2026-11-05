"""Provider ACL adapters for the permissioned company brain."""

from __future__ import annotations

from typing import Any

from app.core.permissions import normalize_email


DEFAULT_PRINCIPAL_MAPPINGS: dict[tuple[str, str, str], str] = {}


def provider_acl_to_envelope(
    source_type: str,
    provider_acl_payload: dict[str, Any],
    *,
    principal_mappings: dict[tuple[str, str, str], str] | None = None,
) -> dict[str, Any]:
    mappings = principal_mappings or DEFAULT_PRINCIPAL_MAPPINGS
    if source_type == "slack":
        return _slack_envelope(provider_acl_payload, mappings)
    if source_type == "github":
        return _github_envelope(provider_acl_payload, mappings)
    if source_type == "gmail":
        return _gmail_envelope(provider_acl_payload)
    if source_type in {"googledocs", "googledrive"}:
        return _drive_envelope(provider_acl_payload, mappings)
    if source_type == "gcal":
        return _calendar_envelope(provider_acl_payload, mappings)
    if source_type == "linear":
        return _linear_envelope(provider_acl_payload, mappings)
    if source_type == "hubspot":
        return _hubspot_envelope(provider_acl_payload, mappings)
    if source_type == "capture":
        return _capture_envelope(provider_acl_payload)
    return _review_envelope([f"unknown:{source_type}:unsupported"])


def _slack_envelope(payload: dict[str, Any], mappings: dict[tuple[str, str, str], str]) -> dict[str, Any]:
    channel = payload.get("channel") or {}
    team = payload.get("team") or {}
    if channel.get("is_private") is False:
        principal = _map(mappings, "slack", "workspace", team.get("id"))
        return _envelope("company", [principal] if principal else [], confidence="high") if principal else _review_envelope(["unknown:slack-workspace"])
    if "is_private" not in channel:
        return _review_envelope([f"unknown:slack-channel:{channel.get('id') or 'missing'}"])

    unknowns = [member for member in payload.get("members") or [] if not _map(mappings, "slack", "user", member)]
    if unknowns:
        return _review_envelope([f"unknown:slack:{member}" for member in unknowns])

    principals = []
    for group in payload.get("usergroups") or []:
        mapped = _map(mappings, "slack", "usergroup", group.get("id"))
        if mapped:
            principals.append(mapped)
    principals.append(_map(mappings, "slack", "channel", channel.get("id")))
    principals = _dedupe([p for p in principals if p])
    if principals:
        freshness = "revoked" if payload.get("removed_members") else "fresh"
        return _envelope("team", principals, confidence="high", freshness=freshness)
    member_principals = [_map(mappings, "slack", "user", member) for member in payload.get("members") or []]
    member_principals = [principal for principal in member_principals if principal]
    if member_principals:
        return _envelope("direct", member_principals, confidence="high")
    return _review_envelope([f"unknown:slack-channel:{channel.get('id') or 'missing'}"])


def _github_envelope(payload: dict[str, Any], mappings: dict[tuple[str, str, str], str]) -> dict[str, Any]:
    org = payload.get("organization") or {}
    repo = payload.get("repository") or {}
    if repo.get("visibility") == "internal" and org.get("mapped_to_company"):
        principal = _map(mappings, "github", "org", org.get("login"))
        return _envelope("company", [principal], confidence="high") if principal else _review_envelope(["unknown:github-org"])

    external = payload.get("outside_collaborators") or []
    if external:
        return _envelope(
            "restricted",
            [f"external:github:{collab.get('login')}" for collab in external if collab.get("login")],
            confidence="medium",
        )

    principals = []
    unknowns = []
    for team in payload.get("teams") or []:
        mapped = _map(mappings, "github", "team", team.get("slug"))
        if mapped:
            principals.append(mapped)
        else:
            unknowns.append(f"unknown:github-team:{team.get('slug')}")
    if unknowns:
        return _review_envelope(unknowns)
    if principals:
        return _envelope("team", principals, confidence="high")
    return _review_envelope([f"unknown:github-repo:{repo.get('full_name') or 'missing'}"])


def _gmail_envelope(payload: dict[str, Any]) -> dict[str, Any]:
    owner = (payload.get("connected_account") or {}).get("alloy_user_id")
    if not owner:
        return _review_envelope(["unknown:gmail-owner"])
    return _envelope("private", [owner], basis="connected_account_owner", confidence="high")


def _drive_envelope(payload: dict[str, Any], mappings: dict[tuple[str, str, str], str]) -> dict[str, Any]:
    principals = []
    unknowns = []
    for permission in payload.get("permissions") or []:
        permission_type = permission.get("type")
        if permission_type == "domain":
            principal = _map(mappings, "google", "domain", _drive_permission_domain(permission, payload))
        elif permission_type == "group":
            principal = _map(mappings, "google", "group", normalize_email(permission.get("emailAddress") or ""))
        elif permission_type == "user":
            email = _drive_permission_user_email(permission, payload)
            principal = _map(mappings, "google", "user", email)
            if not principal and email:
                principal = f"external:{email}" if not email.endswith("@company.com") else None
        else:
            principal = None
        if principal:
            principals.append(principal)
        else:
            unknowns.append(f"unknown:google:{permission_type or 'permission'}")
    if unknowns:
        return _review_envelope(unknowns)
    explicit_freshness = payload.get("acl_freshness_state")
    if payload.get("access_revoked_at") or explicit_freshness == "revoked":
        freshness = "revoked"
    elif explicit_freshness in {"stale", "needs_permission_review"}:
        freshness = explicit_freshness
    elif payload.get("last_acl_synced_at"):
        freshness = "stale"
    else:
        freshness = "fresh"
    if freshness == "needs_permission_review":
        return _review_envelope(["unknown:google-acl-freshness"])
    confidence = "medium" if any(principal.startswith("external:") for principal in principals) else "high"
    return _envelope(_visibility_from_principals(principals), principals, confidence=confidence, freshness=freshness)


def _drive_permission_domain(permission: dict[str, Any], payload: dict[str, Any]) -> str | None:
    domain = permission.get("domain")
    if domain:
        return str(domain)
    hints = payload.get("permission_domain_hints") or payload.get("domain_permission_hints") or []
    if isinstance(hints, str):
        return hints
    if isinstance(hints, list) and len(hints) == 1:
        return str(hints[0])
    if isinstance(hints, dict):
        permission_id = permission.get("id")
        if permission_id and hints.get(str(permission_id)):
            return str(hints[str(permission_id)])
        domains = [value for value in hints.values() if value]
        if len(domains) == 1:
            return str(domains[0])
    return None


def _drive_permission_user_email(permission: dict[str, Any], payload: dict[str, Any]) -> str:
    email = normalize_email(permission.get("emailAddress") or "")
    if email:
        return email
    hints = payload.get("permission_user_hints") or payload.get("user_permission_hints") or {}
    permission_id = permission.get("id")
    if isinstance(hints, dict) and permission_id and hints.get(str(permission_id)):
        return normalize_email(str(hints[str(permission_id)]))
    connected_email = normalize_email((payload.get("connected_account") or {}).get("email") or "")
    if connected_email and permission.get("role") == "owner":
        return connected_email
    return ""


def _calendar_envelope(payload: dict[str, Any], mappings: dict[tuple[str, str, str], str]) -> dict[str, Any]:
    event = payload.get("event") or {}
    if event.get("visibility") == "private":
        for acl in payload.get("calendar_acl") or []:
            scope = acl.get("scope") or {}
            if scope.get("type") == "user" and acl.get("role") == "owner":
                principal = _map(mappings, "google", "user", normalize_email(scope.get("value") or ""))
                return _envelope("private", [principal], basis="calendar_acl_event_privacy", confidence="high") if principal else _review_envelope(["unknown:gcal-owner"])
        return _review_envelope(["unknown:gcal-owner"])

    principals = []
    unknowns = []
    for acl in payload.get("calendar_acl") or []:
        scope = acl.get("scope") or {}
        if scope.get("type") == "domain":
            principal = _map(mappings, "google", "domain", scope.get("value"))
        elif scope.get("type") == "user":
            email = normalize_email(scope.get("value") or "")
            if email.endswith("@group.calendar.google.com"):
                continue
            principal = _map(mappings, "google", "user", email)
        elif scope.get("type") == "group":
            principal = _map(mappings, "google", "group", normalize_email(scope.get("value") or ""))
        elif scope.get("type") in {"default", None}:
            continue
        else:
            principal = None
        if principal:
            principals.append(principal)
        elif scope.get("type"):
            unknowns.append(f"unknown:gcal-{scope.get('type')}")
    if unknowns:
        return _review_envelope(unknowns)
    if principals:
        return _envelope(_visibility_from_principals(principals), principals, basis="calendar_acl_event_privacy", confidence="high")
    return _review_envelope(["unknown:gcal-acl"])


def _linear_envelope(payload: dict[str, Any], mappings: dict[tuple[str, str, str], str]) -> dict[str, Any]:
    if payload.get("guest"):
        email = normalize_email((payload.get("guest") or {}).get("email") or "")
        return _envelope("restricted", [f"external:{email}"], basis="derived_provider_policy", confidence="medium")

    teams = payload.get("teams") or []
    if teams and any(team.get("private") for team in teams) and any(not team.get("private") for team in teams):
        return _review_envelope(
            [_map(mappings, "linear", "team", team.get("id")) for team in teams if _map(mappings, "linear", "team", team.get("id"))],
            basis="derived_provider_policy",
        )

    team = payload.get("team") or {}
    if team.get("private"):
        principal = _map(mappings, "linear", "team", team.get("id"))
        return _envelope("team", [principal], basis="derived_provider_policy", confidence="medium") if principal else _review_envelope(["unknown:linear-team"], basis="derived_provider_policy")

    workspace = payload.get("workspace") or {}
    if workspace.get("mapped_to_company"):
        principal = _map(mappings, "linear", "workspace", workspace.get("id"))
        return _envelope("company", [principal], basis="derived_provider_policy", confidence="medium") if principal else _review_envelope(["unknown:linear-workspace"], basis="derived_provider_policy")
    return _review_envelope(["unknown:linear-visibility"], basis="derived_provider_policy")


def _hubspot_envelope(payload: dict[str, Any], mappings: dict[tuple[str, str, str], str]) -> dict[str, Any]:
    if (payload.get("policy") or {}).get("record_access") == "all_hubspot_users":
        principal = _map(mappings, "hubspot", "policy", "all_hubspot_users")
        return _envelope("company", [principal], basis="tenant_policy", confidence="medium") if principal else _review_envelope(["unknown:hubspot-policy:all_hubspot_users"], basis="tenant_policy")

    pipeline = (payload.get("pipeline_access") or {}).get("pipeline")
    if pipeline:
        principal = _map(mappings, "hubspot", "pipeline", pipeline)
        return _envelope("team", [principal], basis="derived_provider_policy", confidence="medium") if principal else _review_envelope(["unknown:hubspot-pipeline"], basis="derived_provider_policy")

    owner_id = ((payload.get("record") or {}).get("properties") or {}).get("hubspot_owner_id")
    if owner_id:
        owner = next((item for item in payload.get("owners") or [] if item.get("id") == owner_id), None)
        if not owner:
            return _review_envelope([f"unknown:hubspot-owner:{owner_id}"], basis="derived_provider_policy")
        if owner.get("email"):
            principal = _map(mappings, "hubspot", "owner", owner_id)
            return _envelope("private", [principal], basis="derived_provider_policy", confidence="medium") if principal else _review_envelope([f"unknown:hubspot-owner:{owner_id}"], basis="derived_provider_policy")
        teams = owner.get("teams") or []
        principals = [_map(mappings, "hubspot", "team", team.get("id")) for team in teams]
        principals = _dedupe([p for p in principals if p])
        if principals:
            return _envelope("team", principals, basis="derived_provider_policy", confidence="medium")
    return _review_envelope(["unknown:hubspot-owner"], basis="derived_provider_policy")


def _capture_envelope(payload: dict[str, Any]) -> dict[str, Any]:
    """Ad-hoc capture (POST /capture) is never a connected-tool grant, so
    there is no provider ACL to read. app/ingestion/acl_stamping.py has no
    per-source-type branch for "capture" -- it falls through to the generic
    {"source": {"type", "external_event_id"}} shape -- so the owner is
    recovered from the external_event_id the API endpoint mints
    (`capture:{user_id}:{token}`, app/api/capture.py) instead of from a
    provider payload field. Always owner-private: there is no path here that
    can widen visibility to team or company, by design."""
    external_event_id = str((payload.get("source") or {}).get("external_event_id") or "")
    parts = external_event_id.split(":", 2)
    if len(parts) >= 2 and parts[0] == "capture" and parts[1]:
        return _envelope("private", [f"user:{parts[1]}"], basis="capture_owner", confidence="high")
    return _review_envelope(["unknown:capture-owner"])


def _map(mappings: dict[tuple[str, str, str], str], source_type: str, principal_type: str, principal_id: Any) -> str | None:
    if principal_id is None:
        return None
    return mappings.get((source_type, principal_type, str(principal_id)))


def _visibility_from_principals(principals: list[str]) -> str:
    if any(principal.startswith("company:") for principal in principals):
        return "company"
    if any(principal.startswith("group:") for principal in principals):
        return "team"
    if any(principal.startswith("external:") for principal in principals):
        return "restricted"
    if len(principals) == 1:
        return "private"
    return "direct"


def _envelope(
    visibility: str,
    principals: list[str | None],
    *,
    basis: str = "provider_snapshot",
    confidence: str = "high",
    state: str = "stamped",
    freshness: str = "fresh",
) -> dict[str, Any]:
    return {
        "visibility": visibility,
        "allowed_principals": _dedupe([p for p in principals if p]),
        "acl_basis": basis,
        "acl_confidence": confidence,
        "acl_state": state,
        "acl_freshness_state": freshness,
    }


def _review_envelope(principals: list[str | None], *, basis: str = "provider_snapshot") -> dict[str, Any]:
    return _envelope(
        "restricted",
        [p for p in principals if p],
        basis=basis,
        confidence="low",
        state="needs_permission_review",
        freshness="needs_permission_review",
    )


def _dedupe(values: list[str]) -> list[str]:
    out = []
    seen = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out
