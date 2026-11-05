"""Founder-facing alert projection helpers."""
import json
import re
from urllib.parse import urlparse

from app.core.destination_permissions import filter_visible_sources


_GENERIC_SOURCE_LABELS = {
    "calendar",
    "gcal",
    "github",
    "gmail",
    "google docs",
    "googledocs",
    "linear",
    "slack",
    "source",
}


def _jsonb(value, default):
    if value is None:
        return default
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return default
    return value


def _iso(value) -> str | None:
    if not value:
        return None
    return value if isinstance(value, str) else value.isoformat()


def _item_label(item) -> str | None:
    if isinstance(item, str):
        return item.strip() or None
    if isinstance(item, dict):
        for key in ("title", "name", "canonical_name", "label", "key", "id"):
            value = item.get(key)
            if value:
                return str(value).strip()
    return None


def _labels(items) -> list[str]:
    if not isinstance(items, list):
        return []
    out: list[str] = []
    for item in items:
        label = _item_label(item)
        if label and label not in out:
            out.append(label)
    return out


def _meaningful_fallback(fallback: str | None) -> str | None:
    if not fallback:
        return None
    clean = str(fallback).strip()
    if not clean:
        return None
    if clean.lower() in _GENERIC_SOURCE_LABELS:
        return None
    return clean


def _slug_label(value: str | None, *, prefix: str = "") -> str | None:
    if not value:
        return None
    clean = re.sub(r"[-_]+", " ", value).strip()
    if not clean:
        return None
    if prefix:
        return f"{prefix}{clean}"
    return clean


def _source_detail(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urlparse(url)
    path = parsed.path
    if "slack.com" in parsed.netloc:
        match = re.search(r"/archives/([^/]+)", path)
        return _slug_label(match.group(1) if match else None, prefix="#")
    if "linear.app" in parsed.netloc:
        issue = re.search(r"/issue/([^/]+)", path)
        if issue:
            return "-".join(issue.group(1).split("-")[:2]).upper()
        project = re.search(r"/project/([^/?]+)", path)
        return _slug_label(project.group(1) if project else None)
    if "mail.google.com" in parsed.netloc:
        return "thread"
    if "calendar.google.com" in parsed.netloc:
        return "event"
    if "docs.google.com" in parsed.netloc:
        return "document"
    if "github.com" in parsed.netloc:
        parts = [part for part in path.split("/") if part]
        if len(parts) >= 2:
            return "/".join(parts[:2])
    return None


def _source_label(url: str | None, fallback: str | None = None) -> str:
    meaningful = _meaningful_fallback(fallback)
    if meaningful:
        return meaningful
    if not url:
        return "Source"
    host = urlparse(url).netloc.replace("www.", "")
    detail = _source_detail(url)
    if "slack.com" in host:
        return f"Slack · {detail}" if detail else "Slack"
    if "linear.app" in host:
        return f"Linear · {detail}" if detail else "Linear"
    if "mail.google.com" in host:
        return f"Gmail · {detail}" if detail else "Gmail"
    if "calendar.google.com" in host:
        return f"Calendar · {detail}" if detail else "Calendar"
    if "docs.google.com" in host:
        return f"Google Docs · {detail}" if detail else "Google Docs"
    if "github.com" in host:
        return f"GitHub · {detail}" if detail else "GitHub"
    return host or "Source"


def workstream_from_alert(alert: dict) -> dict:
    related = _jsonb(alert.get("related"), {}) or {}
    projects = _labels(related.get("projects"))
    commitments = _labels(related.get("commitments"))
    customers = _labels(related.get("customers"))
    people = _labels(related.get("people"))

    if projects:
        title = projects[0]
        kind = "project"
    elif commitments:
        title = commitments[0]
        kind = "commitment"
    elif customers:
        title = customers[0]
        kind = "customer"
    elif people:
        title = people[0]
        kind = "person"
    else:
        drift_type = (alert.get("drift_type") or "operating_signal").replace("_", " ")
        title = drift_type[:1].upper() + drift_type[1:]
        kind = "signal"

    return {
        "key": f"{kind}:{title.lower()}",
        "title": title,
        "kind": kind,
        "projects": projects,
        "commitments": commitments,
        "customers": customers,
        "people": people,
    }


def accountability_from_alert(alert: dict) -> dict | None:
    related = _jsonb(alert.get("related"), {}) or {}
    accountability = related.get("accountability")
    people = _labels(related.get("people"))
    if isinstance(accountability, dict):
        owner = _item_label(accountability.get("owner")) or (people[0] if people else None)
        contribution = accountability.get("contribution") or accountability.get("contribution_summary")
        right_work = accountability.get("right_work") or accountability.get("right_work_status")
        gap = accountability.get("confidence_gap") or accountability.get("gap")
        return {
            "people": people,
            "owner": owner,
            "contribution": str(contribution).strip() if contribution else None,
            "right_work": str(right_work).strip() if right_work else None,
            "confidence_gap": str(gap).strip() if gap else None,
        }
    if not people:
        return None
    drift_type = alert.get("drift_type") or ""
    if drift_type in {"ownership_gap", "unclear_owner"}:
        return {
            "people": people,
            "owner": None,
            "contribution": None,
            "right_work": "Owner unclear.",
            "confidence_gap": "No source-backed owner visible for this workstream.",
        }
    return {
        "people": people,
        "owner": people[0],
        "contribution": None,
        "right_work": None,
        "confidence_gap": None,
    }


def source_chips_from_alert(alert: dict) -> list[dict]:
    evidence = _jsonb(alert.get("evidence"), []) or []
    destination_principals = _jsonb(alert.get("allowed_principals"), []) or []
    evidence = filter_visible_sources(evidence, destination_principals)
    chips: list[dict] = []
    seen: set[tuple[str, str | None]] = set()
    if not isinstance(evidence, list):
        return chips
    for item in evidence:
        if not isinstance(item, dict):
            continue
        url = item.get("source_url") or item.get("url")
        label = _source_label(url, item.get("source_title") or item.get("title") or item.get("source_type"))
        key = (url or item.get("source_ref") or label, None)
        if key in seen:
            continue
        seen.add(key)
        chip = {
            "label": label,
            "url": url,
            "ref": item.get("ref") or item.get("source_ref"),
        }
        has_source_identity = bool(item.get("source_id") or item.get("source_external_id") or item.get("source_identity_hints"))
        if has_source_identity and item.get("source_type"):
            chip["source_type"] = item.get("source_type")
        for key in ("source_id", "source_external_id"):
            if item.get(key):
                chip[key] = item.get(key)
        if has_source_identity and item.get("source_identity_hints"):
            chip["source_identity_hints"] = item.get("source_identity_hints")
        chips.append(chip)
        if len(chips) >= 4:
            break
    return chips


def source_observed_at_from_alert(alert: dict) -> str | None:
    return _iso(alert.get("source_observed_at"))


async def resolve_source_observed_at(
    conn,
    tenant_id: str,
    evidence,
    destination_principals: list[str] | None = None,
) -> str | None:
    """Resolve evidence refs to the newest source observation time.

    Raw refs should normally be `raw:<raw_event_uuid>`. Legacy/demo refs that
    use provider external ids are only resolved when the evidence item carries
    a source type or title, so same external ids from different sources cannot
    make an older claim look current.
    """
    if not destination_principals:
        return None
    items = _jsonb(evidence, []) or []
    if not isinstance(items, list):
        return None
    refs = []
    for item in items:
        if not isinstance(item, dict):
            continue
        ref = item.get("ref") or item.get("source_ref")
        if not ref or ":" not in str(ref):
            continue
        source_hint = item.get("source_type") or item.get("source_title") or item.get("title")
        refs.append({
            "kind": str(ref).split(":", 1)[0],
            "id": str(ref).split(":", 1)[1],
            "source_hint": str(source_hint).lower() if source_hint else None,
        })
    if not refs:
        return None
    rows = await conn.fetch(
        """
        WITH refs AS (
            SELECT kind, id_text, source_hint
            FROM jsonb_to_recordset($2::jsonb)
                 AS x(kind text, id_text text, source_hint text)
        )
        SELECT max(resolved.observed_at) AS source_observed_at
        FROM refs
        LEFT JOIN normalized_events ne
          ON refs.kind = 'event'
         AND refs.id_text = ne.id::text
         AND ne.tenant_id = $1
         AND ne.acl_state = 'stamped'
         AND ne.acl_freshness_state = 'fresh'
         AND ne.allowed_principals ?| $3::text[]
        LEFT JOIN facts f
          ON refs.kind = 'fact'
         AND refs.id_text = f.id::text
         AND f.tenant_id = $1
         AND f.acl_state = 'stamped'
         AND f.acl_freshness_state = 'fresh'
         AND f.allowed_principals ?| $3::text[]
        LEFT JOIN brain_objects bo
          ON refs.kind = 'object'
         AND refs.id_text = bo.id::text
         AND bo.tenant_id = $1
         AND bo.acl_state = 'stamped'
         AND bo.acl_freshness_state = 'fresh'
         AND bo.allowed_principals ?| $3::text[]
        LEFT JOIN content_chunks cc
          ON refs.kind IN ('chunk', 'event')
         AND refs.id_text = cc.id::text
         AND cc.tenant_id = $1
         AND cc.acl_state = 'stamped'
         AND cc.acl_freshness_state = 'fresh'
         AND cc.allowed_principals ?| $3::text[]
        LEFT JOIN raw_events re
          ON refs.kind = 'raw'
         AND refs.id_text = re.id::text
         AND re.tenant_id = $1
         AND re.acl_state = 'stamped'
         AND re.acl_freshness_state = 'fresh'
         AND re.allowed_principals ?| $3::text[]
        CROSS JOIN LATERAL (
            SELECT COALESCE(ne.occurred_at, f.observed_at, bo.last_seen_at, cc.occurred_at, re.occurred_at, re.received_at) AS observed_at
        ) resolved
        """,
        tenant_id,
        json.dumps([{"kind": ref["kind"], "id_text": ref["id"], "source_hint": ref["source_hint"]} for ref in refs]),
        destination_principals,
    )
    resolved = rows[0]["source_observed_at"] if rows else None
    if resolved:
        return resolved.isoformat()
    return None


async def enrich_evidence_source_identity(conn, tenant_id: str, evidence) -> list[dict]:
    """Attach safe source account identifiers to evidence refs.

    This lets client-side readiness prove that the visible evidence came from
    the same provider accounts that `/connect/me` verified, without exposing
    raw source content or OAuth secrets.
    """
    items = _jsonb(evidence, []) or []
    if not isinstance(items, list):
        return []
    refs = []
    for item in items:
        if not isinstance(item, dict):
            continue
        ref = item.get("ref") or item.get("source_ref")
        if not ref or ":" not in str(ref):
            continue
        source_hint = item.get("source_type") or item.get("source_title") or item.get("title")
        refs.append({
            "kind": str(ref).split(":", 1)[0],
            "id_text": str(ref).split(":", 1)[1],
            "source_hint": str(source_hint).lower() if source_hint else None,
        })
    if not refs:
        return items
    rows = await conn.fetch(
        """
        WITH refs AS (
            SELECT kind, id_text, source_hint
            FROM jsonb_to_recordset($2::jsonb)
                 AS x(kind text, id_text text, source_hint text)
        ),
        resolved AS (
            SELECT refs.kind, refs.id_text, s.id AS source_id, s.source_type, s.external_id, s.sync_policy
            FROM refs
            LEFT JOIN normalized_events ne
              ON refs.kind = 'event'
             AND refs.id_text = ne.id::text
             AND ne.tenant_id = $1
            LEFT JOIN content_chunks cc
              ON refs.kind IN ('chunk', 'event')
             AND refs.id_text = cc.id::text
             AND cc.tenant_id = $1
            LEFT JOIN normalized_events chunk_ne
              ON cc.normalized_event_id = chunk_ne.id
             AND chunk_ne.tenant_id = $1
            LEFT JOIN facts f
              ON refs.kind = 'fact'
             AND refs.id_text = f.id::text
             AND f.tenant_id = $1
            LEFT JOIN normalized_events fact_ne
              ON f.normalized_event_id = fact_ne.id
             AND fact_ne.tenant_id = $1
            LEFT JOIN raw_events re
              ON (
                 (
                    refs.kind = 'raw'
                AND refs.id_text = re.id::text
                 )
                 OR re.id = COALESCE(ne.raw_event_id, chunk_ne.raw_event_id, fact_ne.raw_event_id)
              )
             AND re.tenant_id = $1
            LEFT JOIN sources s
              ON s.id = re.source_id
             AND s.tenant_id = $1
        )
        SELECT DISTINCT ON (kind, id_text)
               kind, id_text, source_id, source_type, external_id, sync_policy
        FROM resolved
        WHERE source_id IS NOT NULL
        ORDER BY kind, id_text
        """,
        tenant_id,
        json.dumps(refs),
    )
    by_ref = {
        f"{row['kind']}:{row['id_text']}": {
            "source_id": str(row["source_id"]),
            "source_type": row["source_type"],
            "source_external_id": row["external_id"],
            "source_identity_hints": _source_identity_hints(row["external_id"], row["sync_policy"]),
        }
        for row in rows
    }
    enriched = []
    for item in items:
        if not isinstance(item, dict):
            enriched.append(item)
            continue
        ref = item.get("ref") or item.get("source_ref")
        enriched.append({**item, **by_ref.get(str(ref), {})})
    return enriched


def _source_identity_hints(external_id: str | None, sync_policy) -> list[str]:
    policy = _jsonb(sync_policy, {}) or {}
    values = [external_id]
    raw = policy.get("identity_hints")
    if isinstance(raw, list):
        values.extend(str(v) for v in raw if v)
    return _dedup_safe_identity_hints(values)


def _dedup_safe_identity_hints(values: list[str | None]) -> list[str]:
    out: list[str] = []
    seen = set()
    for value in values:
        clean = str(value or "").strip()
        if not clean:
            continue
        normalized = clean.lower()
        if normalized.startswith(("sk-", "xox", "ghp_", "github_pat_", "ya29.")):
            continue
        if len(clean) > 160:
            continue
        if any(ch in clean for ch in ("\n", "\r", "{", "}", "[", "]")):
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        out.append(clean)
    return out[:8]


def project_alert(row) -> dict:
    alert = dict(row)
    alert["evidence"] = _jsonb(alert.get("evidence"), [])
    alert["evidence"] = filter_visible_sources(
        alert["evidence"],
        _jsonb(alert.get("allowed_principals"), []) or [],
    )
    alert["missing_evidence"] = _jsonb(alert.get("missing_evidence"), [])
    alert["related"] = _jsonb(alert.get("related"), {})
    alert["workstream"] = workstream_from_alert(alert)
    alert["accountability"] = accountability_from_alert(alert)
    alert["source_chips"] = source_chips_from_alert(alert)
    alert["source_observed_at"] = source_observed_at_from_alert(alert)
    alert["feedback"] = alert.get("feedback")
    return alert
