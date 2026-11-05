"""Founder-facing workstream projection helpers."""
from __future__ import annotations

from collections import OrderedDict
from datetime import datetime, timedelta, timezone
import json

from app.presentation.alerts import accountability_from_alert, project_alert, resolve_source_observed_at, workstream_from_alert


_SEVERITY_RANK = {"high": 3, "medium": 2, "low": 1}
_ATTENTION_TYPES = {
    "blocker",
    "deadline_risk",
    "customer_commitment_risk",
    "commitment_risk",
    "ownership_gap",
    "progress_gap",
}
_SEVERITY_WEIGHT = {"high": 3.0, "medium": 2.0, "low": 1.0}


def _json_dict(value) -> dict:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _metadata_labels(value) -> list[str]:
    if isinstance(value, str):
        clean = value.strip()
        return [clean] if clean else []
    if not isinstance(value, list):
        return []
    labels: list[str] = []
    for item in value:
        if isinstance(item, str):
            label = item.strip()
        elif isinstance(item, dict):
            label = str(
                item.get("title")
                or item.get("name")
                or item.get("canonical_name")
                or item.get("label")
                or item.get("key")
                or item.get("id")
                or ""
            ).strip()
        else:
            label = ""
        if label and label not in labels:
            labels.append(label)
    return labels


def _extend_unique(target: list[str], values: list[str]) -> None:
    for value in values:
        if value and value not in target:
            target.append(value)


def _iso(value) -> str | None:
    if not value:
        return None
    return value if isinstance(value, str) else value.isoformat()


def _parse_dt(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            clean = value.replace("Z", "+00:00")
            parsed = datetime.fromisoformat(clean)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _recency_decay(timestamp: str | None) -> float:
    observed = _parse_dt(timestamp)
    if not observed:
        return 0.4
    hours = max(0.0, (datetime.now(timezone.utc) - observed).total_seconds() / 3600)
    if hours <= 2:
        return 1.0
    if hours <= 8:
        return 0.85
    if hours <= 24:
        return 0.7
    if hours <= 72:
        return 0.5
    return 0.3


def score_leverage_inputs(
    *,
    severity: str,
    timestamp: str | None,
    touches_okr: bool,
    waiting_count: int,
    deadline_boost: float,
    context_boost: float,
    feedback_adjustment: float,
) -> tuple[float, dict]:
    severity_weight = _SEVERITY_WEIGHT.get(str(severity or "").lower(), 1.0)
    recency = _recency_decay(timestamp)
    okr_boost = 0.7 if touches_okr else 0.0
    waiting_boost = min(max(waiting_count, 0), 3) * 0.2
    score = (severity_weight * recency) + okr_boost + waiting_boost + deadline_boost + context_boost + feedback_adjustment
    return score, {
        "severity_weight": severity_weight,
        "recency": recency,
        "touches_okr": touches_okr,
        "waiting_boost": waiting_boost,
        "deadline_boost": deadline_boost,
        "context_boost": context_boost,
        "feedback_adjustment": feedback_adjustment,
    }


def _latest(items: list[dict], key: str) -> dict | None:
    values = [item for item in items if item.get(key)]
    if not values:
        return None
    return sorted(values, key=lambda item: str(item.get(key) or ""), reverse=True)[0]


def _workstream_key(kind: str, title: str) -> str:
    return f"{kind}:{title.lower()}"


def _brain_kind(object_type: str | None) -> str:
    if object_type in {"initiative", "project"}:
        return "project"
    if object_type in {"commitment", "deadline"}:
        return "commitment"
    if object_type in {"blocker", "risk", "decision", "open_question"}:
        return object_type
    return object_type or "workstream"


def _brain_snapshot(obj: dict) -> dict:
    return {
        "object_id": str(obj.get("id")),
        "object_type": obj.get("object_type"),
        "title": obj.get("title"),
        "status": obj.get("status"),
        "confidence": obj.get("confidence"),
        "last_seen_at": _iso(obj.get("last_seen_at")),
    }


def _status_from_alerts(alerts: list[dict]) -> tuple[str, str]:
    if any((alert.get("severity") or "").lower() == "high" for alert in alerts):
        return "at_risk", "High-severity signal is active."
    if any((alert.get("severity") or "").lower() == "medium" for alert in alerts):
        return "needs_attention", "Medium-severity signal is active."
    if any((alert.get("drift_type") or "") in _ATTENTION_TYPES for alert in alerts):
        return "watching", "Operating signal is active."
    return "watching", "No high or medium signal is active."


def _severity_rank(alert: dict) -> int:
    return _SEVERITY_RANK.get((alert.get("severity") or "").lower(), 0)


def _created_at(alert: dict) -> datetime | None:
    return _parse_dt(alert.get("created_at"))


def _delta_from_alerts(alerts: list[dict], freshness: dict) -> tuple[str, str]:
    if not alerts:
        return "no_delivered_signal", "Durable workstream exists, but no delivered signal has been attached yet."
    ordered = sorted(alerts, key=lambda alert: str(alert.get("created_at") or ""), reverse=True)
    latest = ordered[0]
    latest_created = _created_at(latest)
    if latest_created and datetime.now(timezone.utc) - latest_created <= timedelta(hours=24):
        if len(ordered) == 1:
            return "new_signal", "New delivered signal in the last 24 hours."
        previous_rank = max((_severity_rank(alert) for alert in ordered[1:]), default=0)
        if _severity_rank(latest) > previous_rank:
            return "worsened", "Latest signal is higher severity than earlier delivered signals."
        return "updated", "New delivered update in the last 24 hours."
    if freshness.get("stale_source_count"):
        return "stale_evidence", "One or more source-backed signals are older than 24 hours."
    return "still_open", "No new delivered signal in the last 24 hours."


def _source_freshness(alerts: list[dict]) -> dict:
    now = datetime.now(timezone.utc)
    stale = 0
    missing = 0
    latest = None
    for alert in alerts:
        observed = _parse_dt(alert.get("source_observed_at"))
        if not observed:
            missing += 1
            continue
        if not latest or observed > latest:
            latest = observed
        if now - observed > timedelta(hours=24):
            stale += 1
    return {
        "latest_source_observed_at": latest.isoformat() if latest else None,
        "stale_source_count": stale,
        "missing_source_time_count": missing,
    }


def _source_count(alerts: list[dict]) -> int:
    seen: set[tuple[str, str | None]] = set()
    for alert in alerts:
        for chip in alert.get("source_chips") or []:
            seen.add((chip.get("label") or "Source", chip.get("url")))
    return len(seen)


def _people_rows(alerts: list[dict]) -> list[dict]:
    rows: OrderedDict[str, dict] = OrderedDict()
    for alert in alerts:
        accountability = alert.get("accountability") or accountability_from_alert(alert)
        if not accountability:
            continue
        people = accountability.get("people") or []
        if not people and accountability.get("owner"):
            people = [accountability["owner"]]
        if not people:
            people = ["Unknown"]
        for person in people:
            row = rows.setdefault(
                person,
                {
                    "person": person,
                    "contribution": None,
                    "right_work": None,
                    "confidence_gap": None,
                    "source_chips": [],
                },
            )
            row["contribution"] = row["contribution"] or accountability.get("contribution")
            row["right_work"] = row["right_work"] or accountability.get("right_work")
            row["confidence_gap"] = row["confidence_gap"] or accountability.get("confidence_gap")
            if not row["source_chips"]:
                row["source_chips"] = (alert.get("source_chips") or [])[:2]
    return list(rows.values())


def _brain_status(workstream: dict, brain_objects: list[dict]) -> dict | None:
    titles = {
        *(workstream.get("projects") or []),
        *(workstream.get("commitments") or []),
        workstream.get("title"),
    }
    for obj in brain_objects:
        if obj.get("title") in titles:
            return _brain_snapshot(obj)
    return None


def _brain_status_from_memberships(group: dict) -> dict | None:
    brain_objects = [dict(obj) for obj in (group.get("brain_objects") or [])]
    if not brain_objects:
        return None
    preferred_types = {
        "project": {"project", "initiative"},
        "commitment": {"commitment", "deadline"},
        "milestone": {"milestone", "deadline"},
        "risk": {"risk"},
        "blocker": {"blocker"},
        "decision": {"decision"},
        "customer": {"customer"},
        "okr": {"okr"},
        "responsibility": {"person", "responsibility"},
    }.get(group.get("kind") or "", set())
    matching = [obj for obj in brain_objects if str(obj.get("object_type") or "") in preferred_types]
    candidates = matching or brain_objects
    selected = sorted(
        candidates,
        key=lambda obj: str(obj.get("last_seen_at") or ""),
        reverse=True,
    )[0]
    return _brain_snapshot(selected)


def _about(group: dict, latest: dict | None) -> str:
    commitments = group.get("commitments") or []
    customers = group.get("customers") or []
    projects = group.get("projects") or []
    if commitments and projects:
        return f"{commitments[0]} under {projects[0]}"
    if customers and projects:
        return f"{customers[0]} under {projects[0]}"
    if commitments:
        return commitments[0]
    if latest and latest.get("drift_type"):
        drift = str(latest["drift_type"]).replace("_", " ")
        return f"{drift} signal under {group['title']}"
    return group["title"]


def _brain_group_labels(group: dict, key: str) -> list[str]:
    labels: list[str] = []
    for obj in group.get("brain_objects") or []:
        object_type = str(obj.get("object_type") or "")
        title = str(obj.get("title") or "").strip()
        if not title:
            continue
        if key == "projects" and object_type in {"project", "initiative"}:
            _extend_unique(labels, [title])
        elif key == "commitments" and object_type in {"commitment", "deadline", "milestone", "okr"}:
            _extend_unique(labels, [title])
        elif key == "customers" and object_type == "customer":
            _extend_unique(labels, [title])
    return labels


def _fallback_people_rows(group: dict) -> list[dict]:
    rows = _people_rows(group.get("alerts") or [])
    if rows:
        return rows
    people = list(group.get("people") or [])
    return [
        {
            "person": person,
            "contribution": None,
            "right_work": None,
            "confidence_gap": None,
            "source_chips": [],
        }
        for person in people
    ]


def project_workstreams(alert_rows: list[dict], brain_rows: list[dict] | None = None) -> list[dict]:
    grouped: OrderedDict[str, dict] = OrderedDict()
    brain_objects = [dict(row) for row in (brain_rows or [])]
    for row in alert_rows:
        alert = project_alert(dict(row))
        workstream = workstream_from_alert(alert)
        group = grouped.setdefault(
            workstream["key"],
            {
                "key": workstream["key"],
                "title": workstream["title"],
                "kind": workstream["kind"],
                "projects": workstream.get("projects") or [],
                "commitments": workstream.get("commitments") or [],
                "customers": workstream.get("customers") or [],
                "people": workstream.get("people") or [],
                "alerts": [],
            },
        )
        group["alerts"].append(alert)

    for obj in brain_objects:
        title = obj.get("title")
        if not title:
            continue
        kind = _brain_kind(obj.get("object_type"))
        key = _workstream_key(kind, title)
        grouped.setdefault(
            key,
            {
                "key": key,
                "title": title,
                "kind": kind,
                "projects": [title] if kind == "project" else [],
                "commitments": [title] if kind == "commitment" else [],
                "customers": [],
                "people": [],
                "alerts": [],
                "brain_status": _brain_snapshot(obj),
            },
        )

    workstreams: list[dict] = []
    for group in grouped.values():
        alerts = group["alerts"]
        latest = _latest(alerts, "created_at") if alerts else None
        freshness = _source_freshness(alerts)
        delta, delta_reason = _delta_from_alerts(alerts, freshness)
        status, status_reason = _status_from_alerts(alerts) if alerts else ("watching", "Durable workstream record exists, with no delivered risk signal attached yet.")
        brain_status = group.get("brain_status") or _brain_status(group, brain_objects)
        if brain_status and alerts:
            status_basis = "brain_object_and_delivered_signals"
        elif brain_status:
            status_basis = "brain_object_only"
        else:
            status_basis = "delivered_signals"
        confidence_gap = None if brain_status else "No durable project status object has been merged yet; this is based on delivered signals."
        missing_evidence = []
        for alert in alerts:
            for gap in alert.get("missing_evidence") or []:
                if gap not in missing_evidence:
                    missing_evidence.append(gap)
        if confidence_gap and confidence_gap not in missing_evidence:
            missing_evidence.append(confidence_gap)
        workstreams.append({
            "key": group["key"],
            "title": group["title"],
            "kind": group["kind"],
            "about": _about(group, latest),
            "delta": delta,
            "delta_reason": delta_reason,
            "status": status,
            "status_reason": status_reason,
            "status_basis": status_basis,
            "brain_status": brain_status,
            "latest_change": latest.get("why_now") or latest.get("body") or latest.get("claim") if latest else "Durable workstream record exists; no delivered update is attached yet.",
            "next_action": latest.get("recommended_action") if latest else "No source-backed next action suggested yet.",
            "last_alert_at": latest.get("created_at") if latest else None,
            "last_source_observed_at": freshness["latest_source_observed_at"],
            "stale_source_count": freshness["stale_source_count"],
            "missing_source_time_count": freshness["missing_source_time_count"],
            "alert_count": len(alerts),
            "contributing_alert_ids": [str(alert.get("id")) for alert in alerts if alert.get("id")],
            "source_count": _source_count(alerts),
            "missing_evidence": missing_evidence,
            "people_rows": _people_rows(alerts),
            "source_chips": (latest.get("source_chips") or [])[:4] if latest else [],
        })

    return sorted(
        workstreams,
        key=lambda item: (
            _SEVERITY_RANK.get(str(item["status"]).replace("at_risk", "high").replace("needs_attention", "medium"), 0),
            item.get("last_alert_at") or "",
        ),
        reverse=True,
    )


def project_durable_workstreams(
    workstream_rows: list[dict],
    alert_rows: list[dict],
    brain_rows: list[dict] | None = None,
) -> list[dict]:
    grouped: OrderedDict[str, dict] = OrderedDict()
    for raw in workstream_rows:
        row = dict(raw)
        metadata = _json_dict(row.get("metadata"))
        workstream_id = str(row.get("id") or "")
        if not workstream_id or str(row.get("status") or "active") != "active":
            continue
        project_labels = _metadata_labels(metadata.get("projects"))
        display_title = project_labels[0] if project_labels else (row.get("title") or row.get("workstream_key") or workstream_id)
        display_key = f"project:{_slug(str(display_title))}" if display_title else (row.get("workstream_key") or workstream_id)
        grouped[workstream_id] = {
            "id": workstream_id,
            "key": display_key,
            "title": display_title,
            "kind": "project",
            "projects": project_labels or ([display_title] if display_title else []),
            "commitments": _metadata_labels(metadata.get("commitments")),
            "customers": _metadata_labels(metadata.get("customers")),
            "people": _metadata_labels(metadata.get("people")),
            "alerts": [],
            "brain_objects": [],
            "metadata": metadata,
        }

    for raw in alert_rows:
        row = dict(raw)
        workstream_id = str(row.get("workstream_id") or "")
        if not workstream_id:
            continue
        alert = project_alert(row)
        fallback = workstream_from_alert(alert)
        fallback_projects = fallback.get("projects") or []
        fallback_title = fallback_projects[0] if fallback_projects else fallback["title"]
        fallback_key = f"project:{_slug(str(fallback_title))}" if fallback_title else workstream_id
        group = grouped.setdefault(
            workstream_id,
            {
                "id": workstream_id,
                "key": fallback_key,
                "title": fallback_title,
                "kind": "project",
                "projects": fallback_projects or ([fallback_title] if fallback_title else []),
                "commitments": fallback.get("commitments") or [],
                "customers": fallback.get("customers") or [],
                "people": fallback.get("people") or [],
                "alerts": [],
                "brain_objects": [],
                "metadata": {},
            },
        )
        _extend_unique(group["projects"], fallback.get("projects") or [])
        _extend_unique(group["commitments"], fallback.get("commitments") or [])
        _extend_unique(group["customers"], fallback.get("customers") or [])
        _extend_unique(group["people"], fallback.get("people") or [])
        group["alerts"].append(alert)

    for raw in brain_rows or []:
        row = dict(raw)
        workstream_id = str(row.get("workstream_id") or "")
        if not workstream_id:
            continue
        fallback_kind = _brain_kind(row.get("object_type"))
        fallback_title = str(row.get("title") or workstream_id).strip() or workstream_id
        project_title = fallback_title
        group = grouped.setdefault(
            workstream_id,
            {
                "id": workstream_id,
                "key": f"project:{_slug(project_title)}" if project_title else workstream_id,
                "title": project_title,
                "kind": "project",
                "projects": [project_title] if project_title else [],
                "commitments": [],
                "customers": [],
                "people": [],
                "alerts": [],
                "brain_objects": [],
                "metadata": {},
            },
        )
        group["brain_objects"].append(row)

    workstreams: list[dict] = []
    for group in grouped.values():
        alerts = group["alerts"]
        latest = _latest(alerts, "created_at") if alerts else None
        freshness = _source_freshness(alerts)
        delta, delta_reason = _delta_from_alerts(alerts, freshness)
        status, status_reason = _status_from_alerts(alerts) if alerts else ("watching", "Durable workstream record exists, with no delivered risk signal attached yet.")
        _extend_unique(group["projects"], _brain_group_labels(group, "projects"))
        _extend_unique(group["commitments"], _brain_group_labels(group, "commitments"))
        _extend_unique(group["customers"], _brain_group_labels(group, "customers"))
        brain_status = _brain_status_from_memberships(group)
        if brain_status and alerts:
            status_basis = "brain_object_and_delivered_signals"
        elif brain_status:
            status_basis = "brain_object_only"
        else:
            status_basis = "delivered_signals"
        confidence_gap = None if brain_status else "No durable project status object has been merged yet; this is based on delivered signals."
        missing_evidence = []
        for alert in alerts:
            for gap in alert.get("missing_evidence") or []:
                if gap not in missing_evidence:
                    missing_evidence.append(gap)
        if confidence_gap and confidence_gap not in missing_evidence:
            missing_evidence.append(confidence_gap)
        workstreams.append({
            "id": group["id"],
            "key": group["key"],
            "title": group["title"],
            "kind": group["kind"],
            "about": _about(group, latest),
            "delta": delta,
            "delta_reason": delta_reason,
            "status": status,
            "status_reason": status_reason,
            "status_basis": status_basis,
            "brain_status": brain_status,
            "latest_change": latest.get("why_now") or latest.get("body") or latest.get("claim") if latest else "Durable workstream record exists; no delivered update is attached yet.",
            "next_action": latest.get("recommended_action") if latest else "No source-backed next action suggested yet.",
            "last_alert_at": latest.get("created_at") if latest else None,
            "last_source_observed_at": freshness["latest_source_observed_at"],
            "stale_source_count": freshness["stale_source_count"],
            "missing_source_time_count": freshness["missing_source_time_count"],
            "alert_count": len(alerts),
            "contributing_alert_ids": [str(alert.get("id")) for alert in alerts if alert.get("id")],
            "source_count": _source_count(alerts),
            "missing_evidence": missing_evidence,
            "people_rows": _fallback_people_rows(group),
            "source_chips": (latest.get("source_chips") or [])[:4] if latest else [],
        })

    return sorted(
        workstreams,
        key=lambda item: (
            _SEVERITY_RANK.get(str(item["status"]).replace("at_risk", "high").replace("needs_attention", "medium"), 0),
            item.get("last_alert_at") or "",
        ),
        reverse=True,
    )


async def load_company_workstreams(
    conn,
    *,
    tenant_id: str,
    principals: list[str],
    limit: int = 100,
    muted_workstream_keys: set[str] | None = None,
    muted_drift_types: set[str] | None = None,
) -> list[dict]:
    fetch_limit = min(max(limit * 5, limit), 1000)
    alert_rows = [
        dict(row)
        for row in await conn.fetch(
            """
            SELECT id, workstream_id, drift_type, severity, confidence, claim, why_now, evidence, allowed_principals,
                   missing_evidence, recommended_action, related, feedback,
                   COALESCE(last_seen_at, created_at) AS created_at,
                   first_seen_at, last_seen_at, occurrence_count, issue_fingerprint
            FROM alerts
            WHERE tenant_id = $1 AND kind = 'alert' AND status = 'delivered'
              AND workstream_id IS NOT NULL
              AND acl_state = 'stamped'
              AND acl_freshness_state = 'fresh'
              AND allowed_principals ?| $3::text[]
            ORDER BY COALESCE(last_seen_at, created_at) DESC
            LIMIT $2
            """,
            tenant_id,
            fetch_limit,
            principals,
        )
    ]
    if muted_drift_types:
        alert_rows = [row for row in alert_rows if (row.get("drift_type") or "") not in muted_drift_types]
    if not alert_rows:
        return []
    for alert in alert_rows:
        alert["source_observed_at"] = await resolve_source_observed_at(
            conn,
            tenant_id,
            alert.get("evidence"),
            principals,
        )
    workstream_ids = [str(row["workstream_id"]) for row in alert_rows if row.get("workstream_id")]
    unique_workstream_ids = list(dict.fromkeys(workstream_ids))
    workstream_rows = [
        dict(row)
        for row in await conn.fetch(
            """
            SELECT id, tenant_id, workstream_key, title, kind, status, metadata, created_at, updated_at
            FROM workstreams
            WHERE tenant_id = $1
              AND status = 'active'
              AND id = ANY($2::uuid[])
            ORDER BY updated_at DESC
            LIMIT $3
            """,
            tenant_id,
            unique_workstream_ids,
            fetch_limit,
        )
    ]
    brain_rows = [
        dict(row)
        for row in await conn.fetch(
            """
            SELECT bo.id, bo.object_type, bo.title, bo.status, bo.confidence, bo.last_seen_at, wm.workstream_id
            FROM workstream_memberships wm
            JOIN brain_objects bo
              ON bo.id = wm.member_id
             AND bo.tenant_id = wm.tenant_id
            WHERE wm.tenant_id = $1
              AND wm.member_type = 'brain_object'
              AND wm.active = true
              AND wm.workstream_id = ANY($2::uuid[])
              AND bo.status = 'active'
              AND bo.acl_state = 'stamped'
              AND bo.acl_freshness_state = 'fresh'
              AND bo.allowed_principals ?| $3::text[]
            ORDER BY bo.last_seen_at DESC NULLS LAST
            LIMIT 500
            """,
            tenant_id,
            unique_workstream_ids,
            principals,
        )
    ] if unique_workstream_ids else []
    projected = project_durable_workstreams(workstream_rows, alert_rows, brain_rows)
    muted = muted_workstream_keys or set()
    visible = [
        item for item in projected
        if item["key"] not in muted
        and item.get("status_basis") != "brain_object_only"
        and item.get("kind") == "project"
        and (int(item.get("source_count") or 0) > 0 or int(item.get("alert_count") or 0) > 0)
    ]
    return visible[:min(limit, 200)]
