"""
V1 retrieval: deterministic entity/alias match -> graph hop -> recency window
-> Postgres FTS + optional pgvector -> token budget -> saved context pack.

Recall over precision for the pilot: missing the commitment that's about to
slip costs more than one extra Slack message in the pack.
"""
import json
import re
import uuid
from datetime import UTC, datetime
from typing import Any

import structlog

from app.core.db import acquire
from app.core.permission_audit import record_permission_check
from app.personalization.person_context import list_person_contexts, person_context_pack_item
from app.personalization.channel_context import list_channel_contexts, channel_context_pack_item
from app.personalization.tenant_context import normalize_tenant_context, tenant_context_pack_item
from app.presentation.alerts import workstream_from_alert
from app.retrieval.embed import embed_text, embeddings_enabled
from app.retrieval.pack_policies import get_pack_policy, register_view_loaders
from app.synthesis.work_signals import ASK_ANSWER_PACK, COVERAGE_SCAN

logger = structlog.get_logger()

TOKEN_BUDGET = 9000          # ~chars/4
CHUNK_LIMIT = 30
OBJECT_LIMIT = 25
FACT_LIMIT = 30
ARTIFACT_LIMIT = 25
# Deterministic excerpt construction for artifact pack items (Slice A.2,
# architecture-packet-artifacts-lifecycle-2026-07-11.md): collapse whitespace,
# then hard-truncate to this many characters with a trailing ellipsis if the
# content is longer. This is the binding review-verdict constraint on this
# slice -- excerpt construction must be deterministic truncation or extractive
# selection only, never an LLM summarization step, because a silent
# abstractive excerpt would reintroduce the lossy-compression path
# app/llm/validators.py's workstream-digest validator exists to prevent.
ARTIFACT_EXCERPT_CHARS = 800
STALE_WORK_DAYS = 5
STALE_WORK_OBJECT_TYPES = ("project", "initiative", "commitment", "deadline", "blocker", "risk", "open_question")
OBJECT_ACL_FILTER = "bo.acl_state = 'stamped' AND bo.acl_freshness_state = 'fresh'"
EDGE_ACL_FILTER = "acl_state = 'stamped' AND acl_freshness_state = 'fresh'"
FACT_ACL_FILTER = "f.acl_state = 'stamped' AND f.acl_freshness_state = 'fresh'"
CHUNK_ACL_FILTER = "cc.acl_state = 'stamped' AND cc.acl_freshness_state = 'fresh'"
ALERT_ACL_FILTER = "acl_state = 'stamped' AND acl_freshness_state = 'fresh'"
SOURCE_EVENT_ACL_FILTER = """
ne.acl_state = 'stamped' AND ne.acl_freshness_state = 'fresh'
AND re.acl_state = 'stamped' AND re.acl_freshness_state = 'fresh'
"""
POLICY_VERSION = "permissioned-company-brain-v1"


def audience_principals(
    *,
    tenant_id: str,
    actor_user_id: str | None,
    target_audience: str,
    actor_principals: list[str] | set[str] | None = None,
) -> list[str]:
    principals = set(str(p) for p in (actor_principals or []) if p)
    if actor_user_id:
        principals.add(f"user:{actor_user_id}")
    if target_audience == "company":
        principals.add(f"company:{tenant_id}")
        if actor_user_id:
            principals.add(f"user:{actor_user_id}")
    elif target_audience == "private_user" and actor_user_id:
        principals.add(f"user:{actor_user_id}")
    return sorted(principals)


def _principal_filter(alias: str, param_number: int) -> str:
    return f"{alias}.allowed_principals ?| ${param_number}::text[]"


async def build_context_pack(*args, **kwargs) -> dict:
    raise RuntimeError(
        "build_context_pack is disabled for permissioned retrieval; "
        "use build_authorized_context_pack with actor_user_id and target_audience."
    )


async def build_authorized_context_pack(
    *,
    tenant_id: str,
    actor_user_id: str | None,
    target_audience: str,
    purpose: str,
    requester_user_id: str | None = None,
    destination: str | None = None,
    query: str | None = None,
    object_ids: list[str] | None = None,
    recent_hours: int | None = None,
    source_types: set[str] | list[str] | None = None,
    actor_principals: list[str] | set[str] | None = None,
    token_budget: int | None = None,
) -> dict:
    """Audience-bound context pack. SQL/vector candidates must pass ACL before packing."""
    # Resolved first, unconditionally: a purpose with no registered
    # PackPolicy must fail loud even on the no-principals early-return path
    # below, not only once retrieval views start loading.
    policy = get_pack_policy(purpose)
    principals = audience_principals(
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        target_audience=target_audience,
        actor_principals=actor_principals,
    )
    if not principals:
        return await _save_authorized_pack(
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            purpose=purpose,
            query=query,
            packed=[],
            used=0,
            target_audience=target_audience,
            allowed_principals=[],
            requester_user_id=requester_user_id,
            destination=destination,
            pack_policy_version=policy.policy_version,
        )

    items: list[dict] = []
    seen: set[str] = set()
    requested_sources = {str(s).lower() for s in (source_types or []) if s}

    matched_object_ids = list(object_ids or [])
    if query:
        matched_object_ids += await _match_objects_by_name(tenant_id, query, allowed_principals=principals)

    if matched_object_ids:
        matched_object_ids += await _graph_neighbors(tenant_id, matched_object_ids, allowed_principals=principals)

    for obj in _filter_authorized_items(
        _filter_source_items(await _load_objects(tenant_id, matched_object_ids, allowed_principals=principals), requested_sources),
        principals,
    ):
        _add(items, seen, obj)

    floor_items: list[dict] = []
    floor_tokens_used = 0
    for view in policy.views:
        view_items = _filter_authorized_items(
            _filter_source_items(await view.loader(tenant_id, allowed_principals=principals), requested_sources),
            principals,
        )
        if view.item_cap is not None:
            view_items = view_items[: view.item_cap]
        view_tokens_used = 0
        for view_item in view_items:
            cost = len(view_item["text"]) // 4 + 20
            if view_tokens_used + cost > view.floor_tokens:
                # Doesn't fit this view's reserved floor: still a candidate
                # for ranked fill, just no longer guaranteed. An empty or
                # under-floor view simply yields its unused tokens back to
                # ranked fill (Tier 2) below -- there is no separate
                # "surplus pool" bookkeeping needed because floor_tokens_used
                # only ever reflects tokens actually reserved.
                _add(items, seen, view_item)
                continue
            if _add(floor_items, seen, view_item):
                view_tokens_used += cost
        floor_tokens_used += view_tokens_used

    for fact in _filter_authorized_items(
        _filter_source_items(await _load_facts(tenant_id, matched_object_ids, allowed_principals=principals), requested_sources),
        principals,
    ):
        _add(items, seen, fact)

    if query:
        if not requested_sources:
            for alert in _filter_authorized_items(await _search_alerts(tenant_id, query, allowed_principals=principals), principals):
                _add(items, seen, alert)
        for chunk in _filter_authorized_items(
            await _search_chunks(tenant_id, query, source_types=requested_sources, allowed_principals=principals),
            principals,
        ):
            _add(items, seen, chunk)

    if recent_hours:
        for chunk in _filter_authorized_items(
            await _recent_chunks(tenant_id, recent_hours, source_types=requested_sources, allowed_principals=principals),
            principals,
        ):
            _add(items, seen, chunk)

    if f"company:{tenant_id}" in principals and purpose != "search":
        tenant_context_item = await _tenant_context_item(tenant_id)
        if tenant_context_item:
            _add(items, seen, tenant_context_item)
    if target_audience == "private_user" and actor_user_id:
        for context_item in await _person_context_items(tenant_id, actor_user_id):
            _add(items, seen, context_item)
    if purpose in {"brief", "brief_weekly", "chat", "drift"}:
        for context_item in await _channel_context_items(tenant_id, principals):
            _add(items, seen, context_item)

    items.sort(key=lambda i: (-i.get("priority", 0), -i.get("score", 0.0)))
    retrieval_strategy = _pack_retrieval_strategy(purpose=purpose, destination=destination)
    budget = token_budget or policy.token_budget or TOKEN_BUDGET

    packed: list[dict] = []
    used = 0
    for floor_item in floor_items:
        # A view's floor is a reservation, not a suggestion: every item that
        # fit inside its own view's floor_tokens is included unconditionally,
        # even if that pushes total usage above `budget` on an extreme day --
        # floors protect what's declared important at ranked fill's expense
        # (see the packet's noisy-day regression: this is the guarantee under
        # test, not an accident of headroom).
        packed.append(_annotate_pack_item(floor_item, retrieval_strategy=retrieval_strategy))
        used += len(floor_item["text"]) // 4 + 20

    remaining_budget = max(0, budget - used)
    if policy.ranked_fill_enabled:
        ranked_used = 0
        for item in items:
            cost = len(item["text"]) // 4 + 20
            if ranked_used + cost > remaining_budget:
                continue
            packed.append(_annotate_pack_item(item, retrieval_strategy=retrieval_strategy))
            ranked_used += cost
        used += ranked_used

    return await _save_authorized_pack(
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        purpose=purpose,
        query=query,
        packed=packed,
        used=used,
        target_audience=target_audience,
        allowed_principals=principals,
        requester_user_id=requester_user_id,
        destination=destination,
        retrieval_strategy=retrieval_strategy,
        pack_policy_version=policy.policy_version,
    )


async def _save_authorized_pack(
    *,
    tenant_id: str,
    actor_user_id: str | None,
    purpose: str,
    query: str | None,
    packed: list[dict],
    used: int,
    target_audience: str,
    allowed_principals: list[str],
    requester_user_id: str | None = None,
    destination: str | None = None,
    retrieval_strategy: str = COVERAGE_SCAN,
    pack_policy_version: str | None = None,
) -> dict:
    pack_id = str(uuid.uuid4())
    async with acquire() as conn:
        await conn.execute(
            """
            INSERT INTO context_packs
                (id, tenant_id, purpose, query, items, token_count, visibility, acl_basis,
                 acl_confidence, allowed_principals, acl_state, acl_freshness_state,
                 policy_version, actor_user_id, requester_user_id, target_audience, destination,
                 pack_policy_version)
            VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, 'audience_bound_retrieval',
                    'high', $8::jsonb, 'stamped', 'fresh',
                    $9, $10, $11, $12, $13, $14)
            """,
            pack_id, tenant_id, purpose, query, json.dumps(packed), used,
            target_audience, json.dumps(allowed_principals), POLICY_VERSION,
            actor_user_id, requester_user_id, target_audience, destination or purpose,
            pack_policy_version,
        )
    await record_permission_check(
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        target_audience=target_audience,
        action=f"{purpose}.context_pack",
        object_kind="context_pack",
        object_id=pack_id,
        decision="allowed" if allowed_principals else "denied",
        reason="audience_bound_retrieval" if allowed_principals else "no_authorized_principals",
        decision_trace={
            "policy_version": POLICY_VERSION,
            "actor_user_id": actor_user_id,
            "requester_user_id": requester_user_id,
            "target_audience": target_audience,
            "destination": destination or purpose,
            "purpose": purpose,
            "retrieval_strategy": retrieval_strategy,
            "allowed_principals": allowed_principals,
            "item_count": len(packed),
            "token_count": used,
        },
    )
    return {
        "context_pack_id": pack_id,
        "items": packed,
        "text": _render(packed),
        "retrieval_strategy": retrieval_strategy,
        "pack_policy_version": pack_policy_version,
        "permission": {
            "policy_version": POLICY_VERSION,
            "actor_user_id": actor_user_id,
            "requester_user_id": requester_user_id,
            "target_audience": target_audience,
            "destination": destination or purpose,
            "purpose": purpose,
            "retrieval_strategy": retrieval_strategy,
            "allowed_principals": allowed_principals,
            "pack_policy_version": pack_policy_version,
        },
    }


def _pack_retrieval_strategy(*, purpose: str, destination: str | None) -> str:
    if destination == "ask_discuss" or purpose.startswith("chat"):
        return ASK_ANSWER_PACK
    return COVERAGE_SCAN


async def _tenant_context_item(tenant_id: str) -> dict | None:
    try:
        async with acquire() as conn:
            fetchrow = getattr(conn, "fetchrow", None)
            if fetchrow is None:
                return None
            row = await fetchrow(
                """
                SELECT id, tenant_id, priorities, active_narratives, cadences, current_plan, updated_by, updated_at
                FROM tenant_contexts
                WHERE tenant_id = $1
                """,
                tenant_id,
            )
    except Exception:
        return None
    context = normalize_tenant_context(dict(row) if row else None, tenant_id=tenant_id)
    return tenant_context_pack_item(context)


async def _person_context_items(tenant_id: str, owner_user_id: str) -> list[dict]:
    try:
        contexts = await list_person_contexts(tenant_id=tenant_id, owner_user_id=owner_user_id)
    except Exception:
        return []
    items: list[dict] = []
    for context in contexts:
        item = person_context_pack_item(context)
        if item:
            items.append(item)
    return items


async def _channel_context_items(tenant_id: str, allowed_principals: list[str]) -> list[dict]:
    try:
        contexts = await list_channel_contexts(tenant_id=tenant_id)
    except Exception:
        return []
    items: list[dict] = []
    allowed = set(str(principal) for principal in allowed_principals if principal)
    async with acquire() as conn:
        fetchrow = getattr(conn, "fetchrow", None)
        for context in contexts:
            current_allowed = [str(principal) for principal in (context.get("allowed_principals") or []) if principal]
            source_resource_id = context.get("source_resource_id")
            if source_resource_id and fetchrow is not None:
                source_resource = await fetchrow(
                    """
                    SELECT allowed_principals, acl_state, acl_freshness_state
                    FROM source_resources
                    WHERE tenant_id = $1
                      AND id = $2
                      AND source_type = 'slack'
                      AND resource_type = 'slack_channel'
                    """,
                    tenant_id,
                    source_resource_id,
                )
                if not source_resource:
                    continue
                if source_resource.get("acl_state") != "stamped" or source_resource.get("acl_freshness_state") != "fresh":
                    continue
                current_allowed = [
                    str(principal)
                    for principal in _json_list(source_resource.get("allowed_principals"))
                    if principal
                ]
            if not set(current_allowed) & allowed:
                continue
            item = channel_context_pack_item(context)
            if not item:
                continue
            item["allowed_principals"] = current_allowed
            if source_resource_id:
                item["source_resource_id"] = str(source_resource_id)
            if not _filter_authorized_items([item], allowed):
                continue
            items.append(item)
    return items


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple) or isinstance(value, set):
        return list(value)
    return []


def _annotate_pack_item(item: dict[str, Any], *, retrieval_strategy: str) -> dict[str, Any]:
    annotated = dict(item)
    annotated["retrieval_strategy"] = retrieval_strategy
    return annotated


def _filter_source_items(items: list[dict], source_types: set[str]) -> list[dict]:
    if not source_types:
        return items
    return [
        item for item in items
        if str(item.get("source_type") or "").lower() in source_types
    ]


def _filter_authorized_items(items: list[dict], allowed_principals: list[str]) -> list[dict]:
    return [item for item in items if _item_visible_to_principals(item, allowed_principals)]


def _item_visible_to_principals(item: dict, allowed_principals: list[str]) -> bool:
    if item.get("acl_state") != "stamped" or item.get("acl_freshness_state") != "fresh":
        return False
    item_principals = item.get("allowed_principals") or []
    if isinstance(item_principals, str):
        try:
            item_principals = json.loads(item_principals)
        except json.JSONDecodeError:
            return False
    return bool(set(str(p) for p in item_principals) & set(allowed_principals))


def _add(items: list, seen: set, item: dict | None) -> bool:
    if not item:
        return False
    key = f"{item['kind']}:{item['id']}"
    if key in seen:
        return False
    seen.add(key)
    items.append(item)
    return True


def _render(items: list[dict]) -> str:
    """Render the pack with stable source-ref tags the verifier can check."""
    blocks = []
    for item in items:
        ref = f"{item['kind']}:{item['id']}"
        url = f" url={item['source_url']}" if item.get("source_url") else ""
        blocks.append(f"<source ref=\"{ref}\"{url}>\n{item['text']}\n</source>")
    return "\n\n".join(blocks)


# ---------------------------------------------------------------------------

async def _match_objects_by_name(tenant_id: str, query: str, *, allowed_principals: list[str] | None = None) -> list[str]:
    """Entity registry names/aliases/emails appearing in the query -> their brain objects + title matches."""
    words = [w for w in re.findall(r"[\w'@.-]{3,}", query.lower())]
    if not words:
        return []
    async with acquire() as conn:
        principal_filter = ""
        args = [tenant_id, words]
        if allowed_principals is not None:
            principal_filter = " AND " + _principal_filter("bo", 3)
            args.append(allowed_principals)
        rows = await conn.fetch(
            """
            SELECT id FROM brain_objects bo
            WHERE bo.tenant_id = $1 AND status = 'active'
              AND """ + OBJECT_ACL_FILTER + """
              """ + principal_filter + """
              AND EXISTS (SELECT 1 FROM unnest($2::text[]) w WHERE lower(title) LIKE '%' || w || '%')
            LIMIT 15
            """,
            *args,
        )
        entity_rows = await conn.fetch(
            """
            SELECT canonical_name FROM entity_registry
            WHERE tenant_id = $1
              AND EXISTS (
                SELECT 1 FROM unnest($2::text[]) w
                WHERE lower(canonical_name) LIKE '%' || w || '%'
                   OR EXISTS (SELECT 1 FROM unnest(aliases) a WHERE lower(a) LIKE '%' || w || '%')
                   OR EXISTS (SELECT 1 FROM unnest(emails) e WHERE e LIKE '%' || w || '%')
              )
            LIMIT 10
            """,
            tenant_id, words,
        )
        ids = [str(r["id"]) for r in rows]
        for er in entity_rows:
            entity_principal_filter = ""
            entity_args = [tenant_id, er["canonical_name"]]
            if allowed_principals is not None:
                entity_principal_filter = " AND " + _principal_filter("bo", 3)
                entity_args.append(allowed_principals)
            obj = await conn.fetchrow(
                "SELECT id FROM brain_objects bo WHERE tenant_id = $1 AND lower(title) = lower($2) AND " + OBJECT_ACL_FILTER + entity_principal_filter,
                *entity_args,
            )
            if obj:
                ids.append(str(obj["id"]))
    return ids


async def _graph_neighbors(tenant_id: str, object_ids: list[str], *, allowed_principals: list[str] | None = None) -> list[str]:
    if not object_ids:
        return []
    async with acquire() as conn:
        principal_filter = ""
        args = [tenant_id, object_ids]
        if allowed_principals is not None:
            principal_filter = " AND " + _principal_filter("brain_edges", 3)
            args.append(allowed_principals)
        rows = await conn.fetch(
            """
            SELECT DISTINCT CASE WHEN from_object_id = ANY($2::uuid[]) THEN to_object_id ELSE from_object_id END AS id
            FROM brain_edges
            WHERE tenant_id = $1 AND (from_object_id = ANY($2::uuid[]) OR to_object_id = ANY($2::uuid[]))
              AND """ + EDGE_ACL_FILTER + """
              """ + principal_filter + """
            LIMIT 30
            """,
            *args,
        )
    return [str(r["id"]) for r in rows]


async def _load_objects(tenant_id: str, object_ids: list[str], *, allowed_principals: list[str] | None = None) -> list[dict]:
    if not object_ids:
        return []
    async with acquire() as conn:
        principal_filter = ""
        source_event_principal_filter = ""
        args = [tenant_id, list(set(object_ids)), OBJECT_LIMIT]
        if allowed_principals is not None:
            principal_filter = " AND " + _principal_filter("bo", 4)
            source_event_principal_filter = (
                " AND ne.allowed_principals ?| $4::text[]"
                " AND re.allowed_principals ?| $4::text[]"
            )
            args.append(allowed_principals)
        rows = await conn.fetch(
            """
            SELECT bo.id, bo.object_type, bo.title, bo.state, bo.status, bo.confidence,
                   bo.source_refs, bo.last_seen_at, bo.visibility, bo.allowed_principals,
                   bo.acl_state, bo.acl_freshness_state,
                   src.source_url,
                   src.source_id,
                   src.source_type,
                   src.source_external_id,
                   src.source_sync_policy
            FROM brain_objects bo
            LEFT JOIN LATERAL (
                SELECT ref.source_ref->>'url' AS source_url,
                       s.id AS source_id,
                       s.source_type,
                       s.external_id AS source_external_id,
                       s.sync_policy AS source_sync_policy
                FROM brain_object_event_refs ref
                JOIN normalized_events ne
                  ON ne.id = ref.normalized_event_id
                 AND ne.tenant_id = $1
                JOIN raw_events re
                  ON re.id = ne.raw_event_id
                 AND re.tenant_id = $1
                JOIN sources s
                  ON s.id = re.source_id
                 AND s.tenant_id = $1
                WHERE ref.tenant_id = $1
                  AND ref.object_id = bo.id
                  AND """ + SOURCE_EVENT_ACL_FILTER + """
                  """ + source_event_principal_filter + """
                ORDER BY ref.observed_at DESC NULLS LAST, ref.created_at DESC
                LIMIT 1
            ) src ON true
            WHERE bo.tenant_id = $1 AND bo.id = ANY($2::uuid[])
              AND """ + OBJECT_ACL_FILTER + """
              """ + principal_filter + """
            ORDER BY bo.last_seen_at DESC LIMIT $3
            """,
            *args,
        )
    return [_object_item(r, priority=2) for r in rows]


async def _open_risk_objects(tenant_id: str, *, allowed_principals: list[str] | None = None) -> list[dict]:
    async with acquire() as conn:
        principal_filter = ""
        source_event_principal_filter = ""
        args = [tenant_id, OBJECT_LIMIT]
        if allowed_principals is not None:
            principal_filter = " AND " + _principal_filter("bo", 3)
            source_event_principal_filter = (
                " AND ne.allowed_principals ?| $3::text[]"
                " AND re.allowed_principals ?| $3::text[]"
            )
            args.append(allowed_principals)
        rows = await conn.fetch(
            """
            SELECT bo.id, bo.object_type, bo.title, bo.state, bo.status, bo.confidence,
                   bo.source_refs, bo.last_seen_at, bo.visibility, bo.allowed_principals,
                   bo.acl_state, bo.acl_freshness_state,
                   src.source_url,
                   src.source_id,
                   src.source_type,
                   src.source_external_id,
                   src.source_sync_policy
            FROM brain_objects bo
            LEFT JOIN LATERAL (
                SELECT ref.source_ref->>'url' AS source_url,
                       s.id AS source_id,
                       s.source_type,
                       s.external_id AS source_external_id,
                       s.sync_policy AS source_sync_policy
                FROM brain_object_event_refs ref
                JOIN normalized_events ne
                  ON ne.id = ref.normalized_event_id
                 AND ne.tenant_id = $1
                JOIN raw_events re
                  ON re.id = ne.raw_event_id
                 AND re.tenant_id = $1
                JOIN sources s
                  ON s.id = re.source_id
                 AND s.tenant_id = $1
                WHERE ref.tenant_id = $1
                  AND ref.object_id = bo.id
                  AND """ + SOURCE_EVENT_ACL_FILTER + """
                  """ + source_event_principal_filter + """
                ORDER BY ref.observed_at DESC NULLS LAST, ref.created_at DESC
                LIMIT 1
            ) src ON true
            WHERE bo.tenant_id = $1 AND bo.status = 'active'
              AND bo.object_type IN ('commitment', 'deadline', 'blocker', 'risk', 'open_question')
              AND """ + OBJECT_ACL_FILTER + """
              """ + principal_filter + """
            ORDER BY bo.last_seen_at DESC LIMIT $2
            """,
            *args,
        )
    return [_object_item(r, priority=1) for r in rows]


async def _stale_work_objects(tenant_id: str, *, allowed_principals: list[str] | None = None) -> list[dict]:
    """Find active durable objects that have gone quiet for 5+ days.

    This is the backward-looking companion to _open_risk_objects: not what is
    currently risky by recency, but what did not move enough to stay fresh in
    the brief. Recurring calendar commitments are excluded so standing
    meetings do not read like stale execution.
    """
    async with acquire() as conn:
        principal_filter = ""
        source_event_principal_filter = ""
        args = [tenant_id, STALE_WORK_OBJECT_TYPES, OBJECT_LIMIT]
        if allowed_principals is not None:
            principal_filter = " AND " + _principal_filter("bo", 4)
            source_event_principal_filter = (
                " AND ne.allowed_principals ?| $4::text[]"
                " AND re.allowed_principals ?| $4::text[]"
            )
            args.append(allowed_principals)
        rows = await conn.fetch(
            """
            SELECT bo.id, bo.object_type, bo.title, bo.state, bo.status, bo.confidence,
                   bo.source_refs, bo.last_seen_at, bo.visibility, bo.allowed_principals,
                   bo.acl_state, bo.acl_freshness_state,
                   src.source_url,
                   src.source_id,
                   src.source_type,
                   src.source_external_id,
                   src.source_sync_policy,
                   src.recurring
            FROM brain_objects bo
            LEFT JOIN LATERAL (
                SELECT ref.source_ref->>'url' AS source_url,
                       s.id AS source_id,
                       s.source_type,
                       s.external_id AS source_external_id,
                       s.sync_policy AS source_sync_policy,
                       COALESCE((ne.metadata->>'recurring')::boolean, false) AS recurring
                FROM brain_object_event_refs ref
                JOIN normalized_events ne
                  ON ne.id = ref.normalized_event_id
                 AND ne.tenant_id = $1
                JOIN raw_events re
                  ON re.id = ne.raw_event_id
                 AND re.tenant_id = $1
                JOIN sources s
                  ON s.id = re.source_id
                 AND s.tenant_id = $1
                WHERE ref.tenant_id = $1
                  AND ref.object_id = bo.id
                  AND """ + SOURCE_EVENT_ACL_FILTER + """
                  """ + source_event_principal_filter + """
                ORDER BY ref.observed_at DESC NULLS LAST, ref.created_at DESC
                LIMIT 1
            ) src ON true
            WHERE bo.tenant_id = $1
              AND lower(bo.status) = ANY(ARRAY['active', 'in_progress'])
              AND bo.object_type = ANY($2::text[])
              AND bo.last_seen_at < now() - INTERVAL '5 days'
              AND NOT (src.source_type = 'gcal' AND COALESCE(src.recurring, false))
              AND """ + OBJECT_ACL_FILTER + """
              """ + principal_filter + """
            ORDER BY bo.last_seen_at ASC
            LIMIT $3
            """,
            *args,
        )
    return [_stale_work_item(r) for r in rows]


async def _open_loop_objects(tenant_id: str, *, allowed_principals: list[str] | None = None) -> list[dict]:
    async with acquire() as conn:
        principal_filter = ""
        source_event_principal_filter = ""
        args = [tenant_id, OBJECT_LIMIT]
        if allowed_principals is not None:
            principal_filter = " AND " + _principal_filter("bo", 3)
            source_event_principal_filter = (
                " AND ne.allowed_principals ?| $3::text[]"
                " AND re.allowed_principals ?| $3::text[]"
            )
            args.append(allowed_principals)
        rows = await conn.fetch(
            """
            SELECT bo.id, bo.object_type, bo.title, bo.state, bo.status, bo.confidence,
                   bo.source_refs, bo.last_seen_at, bo.visibility, bo.allowed_principals,
                   bo.acl_state, bo.acl_freshness_state, bo.loop_status, bo.loop_opened_at,
                   bo.loop_last_activity_at, bo.loop_stale_after, bo.loop_reopened_count,
                   src.source_url,
                   src.source_id,
                   src.source_type,
                   src.source_external_id,
                   src.source_sync_policy
            FROM brain_objects bo
            LEFT JOIN LATERAL (
                SELECT ref.source_ref->>'url' AS source_url,
                       s.id AS source_id,
                       s.source_type,
                       s.external_id AS source_external_id,
                       s.sync_policy AS source_sync_policy
                FROM brain_object_event_refs ref
                JOIN normalized_events ne
                  ON ne.id = ref.normalized_event_id
                 AND ne.tenant_id = $1
                JOIN raw_events re
                  ON re.id = ne.raw_event_id
                 AND re.tenant_id = $1
                JOIN sources s
                  ON s.id = re.source_id
                 AND s.tenant_id = $1
                WHERE ref.tenant_id = $1
                  AND ref.object_id = bo.id
                  AND """ + SOURCE_EVENT_ACL_FILTER + """
                  """ + source_event_principal_filter + """
                ORDER BY ref.observed_at DESC NULLS LAST, ref.created_at DESC
                LIMIT 1
            ) src ON true
            WHERE bo.tenant_id = $1 AND bo.status = 'active'
              AND bo.object_type IN ('commitment', 'deadline')
              AND bo.loop_status IN ('open', 'reopened', 'stale')
              AND """ + OBJECT_ACL_FILTER + """
              """ + principal_filter + """
            ORDER BY bo.loop_last_activity_at DESC NULLS LAST, bo.last_seen_at DESC
            LIMIT $2
            """,
            *args,
        )
    return [_open_loop_item(r, priority=2) for r in rows]


async def _upcoming_obligations(tenant_id: str, *, allowed_principals: list[str] | None = None) -> list[dict]:
    """Forward-looking companion to _open_risk_objects, which is recency-only
    (last_seen_at DESC) and structurally cannot surface a hard date with no
    recent activity. Two sources: deadline/commitment objects due in the next
    7 days regardless of recency, and calendar events in the next 48h.
    Recurring calendar events are excluded (drift-fp-01 class: a standing
    weekly meeting is not a prep-gap warning)."""
    async with acquire() as conn:
        principal_filter = ""
        args: list[Any] = [tenant_id, OBJECT_LIMIT]
        if allowed_principals is not None:
            principal_filter = " AND " + _principal_filter("bo", 3)
            args.append(allowed_principals)
        deadline_rows = await conn.fetch(
            """
            SELECT bo.id, bo.object_type, bo.title, bo.state, bo.status, bo.confidence,
                   bo.source_refs, bo.last_seen_at, bo.visibility, bo.allowed_principals,
                   bo.acl_state, bo.acl_freshness_state
            FROM brain_objects bo
            WHERE bo.tenant_id = $1 AND bo.status = 'active'
              AND bo.object_type IN ('commitment', 'deadline')
              AND (bo.state->>'due_date') IS NOT NULL
              AND (bo.state->>'due_date') ~ '^\\d{4}-\\d{2}-\\d{2}$'
              AND (bo.state->>'due_date')::date BETWEEN current_date AND current_date + INTERVAL '7 days'
              AND """ + OBJECT_ACL_FILTER + """
              """ + principal_filter + """
            ORDER BY (bo.state->>'due_date')::date ASC LIMIT $2
            """,
            *args,
        )
    items = [_object_item(r, priority=1) for r in deadline_rows]

    async with acquire() as conn:
        principal_filter = ""
        source_event_principal_filter = ""
        args = [tenant_id, CHUNK_LIMIT]
        if allowed_principals is not None:
            principal_filter = " AND " + _principal_filter("cc", 3)
            source_event_principal_filter = (
                " AND ne.allowed_principals ?| $3::text[]"
                " AND re.allowed_principals ?| $3::text[]"
            )
            args.append(allowed_principals)
        calendar_rows = await conn.fetch(
            """
            SELECT cc.id, cc.text, cc.metadata, cc.occurred_at,
                   cc.visibility, cc.allowed_principals, cc.acl_state, cc.acl_freshness_state,
                   s.id AS source_id, s.source_type, s.external_id AS source_external_id,
                   s.sync_policy AS source_sync_policy
            FROM content_chunks cc
            LEFT JOIN normalized_events ne
              ON ne.id = cc.normalized_event_id
             AND ne.tenant_id = $1
             AND ne.acl_state = 'stamped'
             AND ne.acl_freshness_state = 'fresh'
            LEFT JOIN raw_events re
              ON re.id = ne.raw_event_id
             AND re.tenant_id = $1
             AND re.acl_state = 'stamped'
             AND re.acl_freshness_state = 'fresh'
             """ + source_event_principal_filter + """
            LEFT JOIN sources s ON s.id = re.source_id AND s.tenant_id = $1
            WHERE cc.tenant_id = $1
              AND """ + CHUNK_ACL_FILTER + """
              """ + principal_filter + """
              AND s.source_type = 'gcal'
              AND cc.occurred_at BETWEEN now() AND now() + INTERVAL '48 hours'
              AND COALESCE((ne.metadata->>'recurring')::boolean, false) = false
            ORDER BY cc.occurred_at ASC LIMIT $2
            """,
            *args,
        )
    for r in calendar_rows:
        chunk = _chunk_item(r, score=0.3)
        chunk["priority"] = 1
        items.append(chunk)
    return items


async def _recent_contradiction_facts(tenant_id: str, *, allowed_principals: list[str] | None = None) -> list[dict]:
    """Slice 4.1: structured conflict signal written by brain/merge.py when a
    trackable object-state field (owner/due_date/status_detail) changes
    value. Recency-based like _open_risk_objects/_upcoming_obligations so it
    surfaces regardless of whether the object was matched by query -- drift's
    'customer_contradiction' type otherwise has no structured input to point
    at and has to infer conflicts itself from raw evidence."""
    async with acquire() as conn:
        principal_filter = ""
        source_event_principal_filter = ""
        args = [tenant_id, FACT_LIMIT]
        if allowed_principals is not None:
            principal_filter = " AND " + _principal_filter("f", 3)
            source_event_principal_filter = (
                " AND ne.allowed_principals ?| $3::text[]"
                " AND re.allowed_principals ?| $3::text[]"
            )
            args.append(allowed_principals)
        rows = await conn.fetch(
            """
            SELECT f.id, f.claim, f.label, f.confidence, f.observed_at, f.source_refs,
                   f.visibility, f.allowed_principals, f.acl_state, f.acl_freshness_state,
                   src.source_url,
                   src.source_id,
                   src.source_type,
                   src.source_external_id,
                   src.source_sync_policy
            FROM facts f
            LEFT JOIN LATERAL (
                SELECT fact_ref.source_ref->>'url' AS source_url,
                       s.id AS source_id,
                       s.source_type,
                       s.external_id AS source_external_id,
                       s.sync_policy AS source_sync_policy
                FROM (
                    SELECT f.normalized_event_id, '{}'::jsonb AS source_ref
                    WHERE f.normalized_event_id IS NOT NULL
                    UNION ALL
                    SELECT (ref->>'normalized_event_id')::uuid AS normalized_event_id, ref AS source_ref
                    FROM jsonb_array_elements(f.source_refs) ref
                    WHERE ref->>'normalized_event_id' ~*
                          '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
                ) fact_ref
                JOIN normalized_events ne
                  ON ne.id = fact_ref.normalized_event_id
                 AND ne.tenant_id = $1
                JOIN raw_events re
                  ON re.id = ne.raw_event_id
                 AND re.tenant_id = $1
                JOIN sources s
                  ON s.id = re.source_id
                 AND s.tenant_id = $1
                WHERE """ + SOURCE_EVENT_ACL_FILTER + """
                  """ + source_event_principal_filter + """
                ORDER BY ne.occurred_at DESC NULLS LAST
                LIMIT 1
            ) src ON true
            WHERE f.tenant_id = $1 AND f.fact_kind = 'contradiction'
              AND """ + FACT_ACL_FILTER + """
              """ + principal_filter + """
            ORDER BY f.observed_at DESC LIMIT $2
            """,
            *args,
        )
    return [_fact_item(r) for r in rows]


def _artifact_excerpt(content: str) -> str:
    """Deterministic truncation, not extraction by a model call. See
    ARTIFACT_EXCERPT_CHARS's comment for why this must never become an LLM
    summarization step."""
    compact = re.sub(r"\s+", " ", content or "").strip()
    if len(compact) <= ARTIFACT_EXCERPT_CHARS:
        return compact
    return compact[: ARTIFACT_EXCERPT_CHARS - 1].rstrip() + "…"


async def _artifact_pack_items(tenant_id: str, *, allowed_principals: list[str] | None = None) -> list[dict]:
    """Slice A.2 (architecture-packet-artifacts-lifecycle-2026-07-11.md):
    current-status artifacts as a pack-item view.

    Eligibility is `status = 'current'` only -- a draft or superseded
    artifact is never a candidate here, no exception even for the artifact's
    own owner. The live-editing surface for a draft is the session tool loop
    (app/sessions/tools.py), a structurally different access path, out of
    scope for retrieval. The SQL WHERE clause enforces this; the Python-side
    `artifact["status"] != "current"` check right after is a deliberate
    belt-and-suspenders re-check against a raw row (not a second
    implementation of the SQL semantics, just a redundant guard so a future
    query edit that drops the WHERE clause fails a unit test instead of
    shipping silently).

    ACL is the EXISTING `artifact_visible_to_destination` check
    (app/presentation/artifacts.py), reused rather than reimplemented -- the
    packet explicitly calls out duplicate ACL logic as the anti-pattern
    already caught and fixed twice in this build (Track 3's original PR
    review, Track C's Wave 1 review).

    Unfloored: this loader is wired into app/retrieval/pack_policies.py's
    PackPolicy registry with floor_tokens=0, so every item it returns is a
    Tier 2 ranked-fill candidate like a chunk or fact, never a
    floor-guaranteed inclusion, per the packet's Q3 binding resolution
    ("start unfloored in Tier 2, revisit on yield data").

    Fails soft (mirrors _tenant_context_item's established pattern for
    optional/additive pack-item sources): a connection stand-in without a
    `.fetch` method, or any other query-layer error, degrades to "no
    artifacts this pack build" rather than failing the whole pack build --
    this view is additive to purposes that worked fine before it existed,
    the same guarantee _tenant_context_item/_person_context_items/
    _channel_context_items already give their callers.
    """
    from app.artifacts.store import normalize_artifact
    from app.presentation.artifacts import artifact_visible_to_destination

    try:
        async with acquire() as conn:
            fetch = getattr(conn, "fetch", None)
            if fetch is None:
                return []
            rows = await fetch(
                """
                SELECT * FROM artifacts
                WHERE tenant_id = $1 AND status = 'current'
                ORDER BY updated_at DESC
                LIMIT $2
                """,
                tenant_id, ARTIFACT_LIMIT,
            )
    except Exception as e:
        logger.warning("artifact_pack_items_query_failed", tenant_id=tenant_id, error=str(e))
        return []
    items: list[dict] = []
    for row in rows:
        artifact = normalize_artifact(dict(row))
        if artifact["status"] != "current":
            continue
        if allowed_principals is not None and not artifact_visible_to_destination(artifact, allowed_principals):
            continue
        excerpt = _artifact_excerpt(artifact["content"])
        items.append({
            "kind": "artifact",
            "id": artifact["id"],
            "text": f"[artifact:{artifact['kind']}] {artifact['title']}\n{excerpt}",
            "source_url": None,
            "visibility": artifact["visibility"],
            "allowed_principals": artifact["allowed_principals"],
            "acl_state": "stamped",
            "acl_freshness_state": "fresh",
            "priority": 1,
            "score": 0.5,
            "artifact_kind": artifact["kind"],
            "artifact_version": artifact["current_version"],
        })
    return items


def _object_item(row, priority: int) -> dict:
    state = row["state"]
    state = json.loads(state) if isinstance(state, str) else dict(state or {})
    source_id = _row_get(row, "source_id")
    url = _row_get(row, "source_url") if source_id else None
    source_external_id = _row_get(row, "source_external_id") if source_id else None
    state_str = ", ".join(f"{k}={v}" for k, v in state.items() if v is not None)
    return {
        "kind": "object",
        "id": str(row["id"]),
        "text": f"[{row['object_type']}] {row['title']} ({state_str}) confidence={row['confidence']:.2f} last_seen={row['last_seen_at']:%Y-%m-%d}",
        "source_url": url,
        "source_id": str(source_id) if source_id else None,
        "source_type": _row_get(row, "source_type") if source_id else None,
        "source_external_id": source_external_id,
        "source_identity_hints": _source_identity_hints(source_external_id, _row_get(row, "source_sync_policy")) if source_id else [],
        "source_observed_at": row["last_seen_at"].isoformat() if row["last_seen_at"] else None,
        "visibility": _row_get(row, "visibility"),
        "allowed_principals": _json_list(_row_get(row, "allowed_principals")),
        "acl_state": _row_get(row, "acl_state"),
        "acl_freshness_state": _row_get(row, "acl_freshness_state"),
        "priority": priority,
        "score": float(row["confidence"]),
    }


def _open_loop_item(row, priority: int) -> dict:
    state = row["state"]
    state = json.loads(state) if isinstance(state, str) else dict(state or {})
    source_id = _row_get(row, "source_id")
    url = _row_get(row, "source_url") if source_id else None
    source_external_id = _row_get(row, "source_external_id") if source_id else None
    details = [f"loop_status={row['loop_status']}"]
    loop_opened_at = _row_get(row, "loop_opened_at")
    loop_last_activity_at = _row_get(row, "loop_last_activity_at")
    loop_stale_after = _row_get(row, "loop_stale_after")
    loop_reopened_count = int(_row_get(row, "loop_reopened_count") or 0)
    if loop_opened_at:
        details.append(f"opened={loop_opened_at:%Y-%m-%d}")
    if loop_last_activity_at:
        details.append(f"last_activity={loop_last_activity_at:%Y-%m-%d}")
    if loop_stale_after:
        details.append(f"stale_after={loop_stale_after:%Y-%m-%d}")
    if loop_reopened_count > 0:
        details.append(f"reopened_count={loop_reopened_count}")
    state_str = ", ".join(
        [*(f"{k}={v}" for k, v in state.items() if v is not None), *details]
    )
    return {
        "kind": "object",
        "id": str(row["id"]),
        "text": f"[{row['object_type']}] {row['title']} ({state_str}) confidence={row['confidence']:.2f} last_seen={row['last_seen_at']:%Y-%m-%d}",
        "source_url": url,
        "source_id": str(source_id) if source_id else None,
        "source_type": _row_get(row, "source_type") if source_id else None,
        "source_external_id": source_external_id,
        "source_identity_hints": _source_identity_hints(source_external_id, _row_get(row, "source_sync_policy")) if source_id else [],
        "source_observed_at": row["last_seen_at"].isoformat() if row["last_seen_at"] else None,
        "visibility": _row_get(row, "visibility"),
        "allowed_principals": _json_list(_row_get(row, "allowed_principals")),
        "acl_state": _row_get(row, "acl_state"),
        "acl_freshness_state": _row_get(row, "acl_freshness_state"),
        "priority": priority,
        "score": float(row["confidence"]),
    }


def _stale_work_item(row) -> dict:
    state = row["state"]
    state = json.loads(state) if isinstance(state, str) else dict(state or {})
    source_id = _row_get(row, "source_id")
    url = _row_get(row, "source_url") if source_id else None
    source_external_id = _row_get(row, "source_external_id") if source_id else None
    last_seen_at = row["last_seen_at"]
    days_silent = max(0, (datetime.now(UTC) - last_seen_at).days) if last_seen_at else None
    owner = str(state.get("owner") or "").strip() or "unknown"
    status_detail = str(state.get("status_detail") or "").strip()
    parts = [f"[{row['object_type']}] {row['title']}", f"owner={owner}"]
    if days_silent is not None:
        parts.append(f"days_silent={days_silent}")
    if status_detail:
        parts.append(f"status={status_detail}")
    if last_seen_at:
        parts.append(f"last_seen={last_seen_at:%Y-%m-%d}")
    return {
        "kind": "object",
        "id": str(row["id"]),
        "text": " ".join(parts),
        "source_url": url,
        "source_id": str(source_id) if source_id else None,
        "source_type": _row_get(row, "source_type") if source_id else None,
        "source_external_id": source_external_id,
        "source_identity_hints": _source_identity_hints(source_external_id, _row_get(row, "source_sync_policy")) if source_id else [],
        "source_observed_at": last_seen_at.isoformat() if last_seen_at else None,
        "visibility": _row_get(row, "visibility"),
        "allowed_principals": _json_list(_row_get(row, "allowed_principals")),
        "acl_state": _row_get(row, "acl_state"),
        "acl_freshness_state": _row_get(row, "acl_freshness_state"),
        "signal_type": "stale_work",
        "priority": 1,
        "score": float(days_silent or 0),
        "days_silent": days_silent,
    }


async def _load_facts(tenant_id: str, object_ids: list[str], *, allowed_principals: list[str] | None = None) -> list[dict]:
    if not object_ids:
        return []
    async with acquire() as conn:
        principal_filter = ""
        source_event_principal_filter = ""
        args = [tenant_id, list(set(object_ids)), FACT_LIMIT]
        if allowed_principals is not None:
            principal_filter = " AND " + _principal_filter("f", 4)
            source_event_principal_filter = (
                " AND ne.allowed_principals ?| $4::text[]"
                " AND re.allowed_principals ?| $4::text[]"
            )
            args.append(allowed_principals)
        rows = await conn.fetch(
            """
            SELECT f.id, f.claim, f.label, f.confidence, f.observed_at, f.source_refs,
                   f.visibility, f.allowed_principals, f.acl_state, f.acl_freshness_state,
                   src.source_url,
                   src.source_id,
                   src.source_type,
                   src.source_external_id,
                   src.source_sync_policy
            FROM facts f
            LEFT JOIN LATERAL (
                SELECT fact_ref.source_ref->>'url' AS source_url,
                       s.id AS source_id,
                       s.source_type,
                       s.external_id AS source_external_id,
                       s.sync_policy AS source_sync_policy
                FROM (
                    SELECT f.normalized_event_id, '{}'::jsonb AS source_ref
                    WHERE f.normalized_event_id IS NOT NULL
                    UNION ALL
                    SELECT (ref->>'normalized_event_id')::uuid AS normalized_event_id, ref AS source_ref
                    FROM jsonb_array_elements(f.source_refs) ref
                    WHERE ref->>'normalized_event_id' ~*
                          '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
                ) fact_ref
                JOIN normalized_events ne
                  ON ne.id = fact_ref.normalized_event_id
                 AND ne.tenant_id = $1
                JOIN raw_events re
                  ON re.id = ne.raw_event_id
                 AND re.tenant_id = $1
                JOIN sources s
                  ON s.id = re.source_id
                 AND s.tenant_id = $1
                WHERE """ + SOURCE_EVENT_ACL_FILTER + """
                  """ + source_event_principal_filter + """
                ORDER BY ne.occurred_at DESC NULLS LAST
                LIMIT 1
            ) src ON true
            WHERE f.tenant_id = $1 AND f.brain_object_id = ANY($2::uuid[])
              AND """ + FACT_ACL_FILTER + """
              """ + principal_filter + """
            ORDER BY f.observed_at DESC LIMIT $3
            """,
            *args,
        )
    out = []
    for r in rows:
        out.append(_fact_item(r))
    return out


def _fact_item(row) -> dict:
    source_id = _row_get(row, "source_id")
    url = _row_get(row, "source_url") if source_id else None
    source_external_id = _row_get(row, "source_external_id") if source_id else None
    return {
        "kind": "fact",
        "id": str(row["id"]),
        "text": f"[{row['label']}] {row['claim']} (observed {row['observed_at']:%Y-%m-%d})",
        "source_url": url,
        "source_id": str(source_id) if source_id else None,
        "source_type": _row_get(row, "source_type") if source_id else None,
        "source_external_id": source_external_id,
        "source_identity_hints": _source_identity_hints(source_external_id, _row_get(row, "source_sync_policy")) if source_id else [],
        "source_observed_at": row["observed_at"].isoformat() if row["observed_at"] else None,
        "visibility": _row_get(row, "visibility"),
        "allowed_principals": _json_list(_row_get(row, "allowed_principals")),
        "acl_state": _row_get(row, "acl_state"),
        "acl_freshness_state": _row_get(row, "acl_freshness_state"),
        "priority": 1,
        "score": float(row["confidence"]),
    }


async def _search_chunks(
    tenant_id: str,
    query: str,
    *,
    source_types: set[str] | None = None,
    allowed_principals: list[str] | None = None,
) -> list[dict]:
    items: list[dict] = []
    source_filter = sorted(source_types or [])
    async with acquire() as conn:
        principal_filter = ""
        source_event_principal_filter = ""
        args = [tenant_id, query, CHUNK_LIMIT, source_filter]
        if allowed_principals is not None:
            principal_filter = " AND " + _principal_filter("cc", 5)
            source_event_principal_filter = (
                " AND ne.allowed_principals ?| $5::text[]"
                " AND re.allowed_principals ?| $5::text[]"
            )
            args.append(allowed_principals)
        fts_rows = await conn.fetch(
            """
            SELECT cc.id, cc.text, cc.metadata, cc.occurred_at,
                   cc.visibility, cc.allowed_principals, cc.acl_state, cc.acl_freshness_state,
                   s.id AS source_id, s.source_type, s.external_id AS source_external_id,
                   s.sync_policy AS source_sync_policy,
                   ts_rank(cc.tsv, plainto_tsquery('english', $2)) AS rank
            FROM content_chunks cc
            LEFT JOIN normalized_events ne
              ON ne.id = cc.normalized_event_id
             AND ne.tenant_id = $1
             AND ne.acl_state = 'stamped'
             AND ne.acl_freshness_state = 'fresh'
            LEFT JOIN raw_events re
              ON re.id = ne.raw_event_id
             AND re.tenant_id = $1
             AND re.acl_state = 'stamped'
             AND re.acl_freshness_state = 'fresh'
             """ + source_event_principal_filter + """
            LEFT JOIN sources s ON s.id = re.source_id AND s.tenant_id = $1
            WHERE cc.tenant_id = $1
              AND """ + CHUNK_ACL_FILTER + """
              """ + principal_filter + """
              AND cc.tsv @@ plainto_tsquery('english', $2)
              AND (cardinality($4::text[]) = 0 OR s.source_type = ANY($4::text[]))
            ORDER BY rank DESC LIMIT $3
            """,
            *args,
        )
        items += [_chunk_item(r, score=float(r["rank"])) for r in fts_rows]

        if embeddings_enabled():
            vector = await embed_text(query)
            if vector is not None:
                vector_principal_filter = ""
                vector_source_event_principal_filter = ""
                vector_args = [tenant_id, str(vector), CHUNK_LIMIT, source_filter]
                if allowed_principals is not None:
                    vector_principal_filter = " AND " + _principal_filter("cc", 5)
                    vector_source_event_principal_filter = (
                        " AND ne.allowed_principals ?| $5::text[]"
                        " AND re.allowed_principals ?| $5::text[]"
                    )
                    vector_args.append(allowed_principals)
                vec_rows = await conn.fetch(
                    """
                    SELECT cc.id, cc.text, cc.metadata, cc.occurred_at,
                           cc.visibility, cc.allowed_principals, cc.acl_state, cc.acl_freshness_state,
                           s.id AS source_id, s.source_type, s.external_id AS source_external_id,
                           s.sync_policy AS source_sync_policy,
                           1 - (cc.embedding <=> $2::vector) AS sim
                    FROM content_chunks cc
                    LEFT JOIN normalized_events ne
                      ON ne.id = cc.normalized_event_id
                     AND ne.tenant_id = $1
                     AND ne.acl_state = 'stamped'
                     AND ne.acl_freshness_state = 'fresh'
                    LEFT JOIN raw_events re
                      ON re.id = ne.raw_event_id
                     AND re.tenant_id = $1
                     AND re.acl_state = 'stamped'
                     AND re.acl_freshness_state = 'fresh'
                     """ + vector_source_event_principal_filter + """
                    LEFT JOIN sources s ON s.id = re.source_id AND s.tenant_id = $1
                    WHERE cc.tenant_id = $1
                      AND """ + CHUNK_ACL_FILTER + """
                      """ + vector_principal_filter + """
                      AND cc.embedding IS NOT NULL
                      AND (cardinality($4::text[]) = 0 OR s.source_type = ANY($4::text[]))
                    ORDER BY cc.embedding <=> $2::vector LIMIT $3
                    """,
                    *vector_args,
                )
                items += [_chunk_item(r, score=float(r["sim"])) for r in vec_rows]
    return items


async def _search_alerts(tenant_id: str, query: str, *, allowed_principals: list[str] | None = None) -> list[dict]:
    words = [w for w in re.findall(r"[\w'@.-]{3,}", query.lower())]
    async with acquire() as conn:
        principal_filter = ""
        args = [tenant_id, words]
        if allowed_principals is not None:
            principal_filter = " AND " + _principal_filter("alerts", 3)
            args.append(allowed_principals)
        rows = await conn.fetch(
            """
            SELECT id, drift_type, severity, confidence, claim, why_now, evidence,
                   missing_evidence, recommended_action, related, created_at,
                   visibility, allowed_principals, acl_state, acl_freshness_state
            FROM alerts
            WHERE tenant_id = $1
              AND kind = 'alert'
              AND status = 'delivered'
              AND """ + ALERT_ACL_FILTER + """
              """ + principal_filter + """
              AND (
                cardinality($2::text[]) = 0
                OR EXISTS (
                  SELECT 1 FROM unnest($2::text[]) w
                  WHERE lower(claim) LIKE '%' || w || '%'
                     OR lower(COALESCE(why_now, '')) LIKE '%' || w || '%'
                     OR lower(COALESCE(recommended_action, '')) LIKE '%' || w || '%'
                     OR lower(related::text) LIKE '%' || w || '%'
                )
              )
            ORDER BY created_at DESC
            LIMIT 12
            """,
            *args,
        )
    return [_alert_item(row) for row in rows]


def _alert_item(row) -> dict:
    alert = dict(row)
    evidence = alert.get("evidence")
    evidence = json.loads(evidence) if isinstance(evidence, str) else list(evidence or [])
    missing = alert.get("missing_evidence")
    missing = json.loads(missing) if isinstance(missing, str) else list(missing or [])
    workstream = workstream_from_alert(alert)
    evidence_text = []
    url = None
    for item in evidence[:4]:
        if not isinstance(item, dict):
            continue
        url = url or item.get("source_url") or item.get("url")
        source = item.get("source_type") or item.get("source_ref") or "source"
        summary = item.get("quote_or_summary") or item.get("quote") or item.get("summary")
        if summary:
            evidence_text.append(f"{source}: {summary}")
    parts = [
        f"[alert:{alert['drift_type']}] workstream={workstream['title']}",
        f"claim={alert['claim']}",
    ]
    if alert.get("why_now"):
        parts.append(f"why_now={alert['why_now']}")
    if alert.get("recommended_action"):
        parts.append(f"next_action={alert['recommended_action']}")
    if evidence_text:
        parts.append("evidence=" + " | ".join(evidence_text))
    if missing:
        parts.append("missing_evidence=" + " | ".join(str(item) for item in missing[:4]))
    return {
        "kind": "alert",
        "id": str(alert["id"]),
        "text": "\n".join(parts),
        "source_url": url,
        "visibility": _row_get(row, "visibility"),
        "allowed_principals": _json_list(_row_get(row, "allowed_principals")),
        "acl_state": _row_get(row, "acl_state"),
        "acl_freshness_state": _row_get(row, "acl_freshness_state"),
        "priority": 1,
        "score": float(alert.get("confidence") or 0),
    }


async def _recent_chunks(
    tenant_id: str,
    hours: int,
    *,
    source_types: set[str] | None = None,
    allowed_principals: list[str] | None = None,
) -> list[dict]:
    source_filter = sorted(source_types or [])
    async with acquire() as conn:
        principal_filter = ""
        source_event_principal_filter = ""
        args = [tenant_id, hours, CHUNK_LIMIT * 2, source_filter]
        if allowed_principals is not None:
            principal_filter = " AND " + _principal_filter("cc", 5)
            source_event_principal_filter = (
                " AND ne.allowed_principals ?| $5::text[]"
                " AND re.allowed_principals ?| $5::text[]"
            )
            args.append(allowed_principals)
        rows = await conn.fetch(
            """
            SELECT cc.id, cc.text, cc.metadata, cc.occurred_at,
                   cc.visibility, cc.allowed_principals, cc.acl_state, cc.acl_freshness_state,
                   s.id AS source_id, s.source_type, s.external_id AS source_external_id,
                   s.sync_policy AS source_sync_policy
            FROM content_chunks cc
            LEFT JOIN normalized_events ne
              ON ne.id = cc.normalized_event_id
             AND ne.tenant_id = $1
             AND ne.acl_state = 'stamped'
             AND ne.acl_freshness_state = 'fresh'
            LEFT JOIN raw_events re
              ON re.id = ne.raw_event_id
             AND re.tenant_id = $1
             AND re.acl_state = 'stamped'
             AND re.acl_freshness_state = 'fresh'
             """ + source_event_principal_filter + """
            LEFT JOIN sources s ON s.id = re.source_id AND s.tenant_id = $1
            WHERE cc.tenant_id = $1
              AND """ + CHUNK_ACL_FILTER + """
              """ + principal_filter + """
              AND cc.occurred_at > now() - make_interval(hours => $2)
              AND (cardinality($4::text[]) = 0 OR s.source_type = ANY($4::text[]))
            ORDER BY cc.occurred_at DESC LIMIT $3
            """,
            *args,
        )
    return [_chunk_item(r, score=0.3) for r in rows]


async def load_deep_story_chunks(
    tenant_id: str,
    object_ids: list[str],
    *,
    recent_hours: int = 26,
    allowed_principals: list[str] | None = None,
) -> list[dict]:
    """Targeted deep retrieval for scout-then-dig brief runs.

    Resolve recent chunk evidence from the selected stories' object set rather
    than from the tenant-wide recency window. This keeps call 2's pack scoped
    to the workstreams call 1 selected while still rehydrating raw evidence.
    """
    if not object_ids:
        return []
    async with acquire() as conn:
        principal_filter = ""
        source_event_principal_filter = ""
        args: list[Any] = [tenant_id, list(set(object_ids)), recent_hours, allowed_principals or [], CHUNK_LIMIT]
        if allowed_principals is not None:
            principal_filter = " AND " + _principal_filter("cc", 4)
            source_event_principal_filter = (
                " AND ne.allowed_principals ?| $4::text[]"
                " AND re.allowed_principals ?| $4::text[]"
            )
        rows = await conn.fetch(
            """
            WITH scoped_events AS (
                SELECT DISTINCT ref.normalized_event_id
                FROM brain_object_event_refs ref
                JOIN normalized_events ne
                  ON ne.id = ref.normalized_event_id
                 AND ne.tenant_id = $1
                JOIN raw_events re
                  ON re.id = ne.raw_event_id
                 AND re.tenant_id = $1
                WHERE ref.tenant_id = $1
                  AND ref.object_id = ANY($2::uuid[])
                  AND ne.occurred_at >= now() - ($3::int * interval '1 hour')
                  AND """ + SOURCE_EVENT_ACL_FILTER + """
                  """ + source_event_principal_filter + """
            )
            SELECT cc.id, cc.text, cc.metadata, cc.visibility, cc.allowed_principals,
                   cc.acl_state, cc.acl_freshness_state, ne.occurred_at,
                   s.id AS source_id, s.source_type, s.external_id AS source_external_id,
                   s.sync_policy AS source_sync_policy
            FROM content_chunks cc
            JOIN normalized_events ne
              ON ne.id = cc.normalized_event_id
             AND ne.tenant_id = $1
            JOIN raw_events re
              ON re.id = ne.raw_event_id
             AND re.tenant_id = $1
            JOIN sources s
              ON s.id = re.source_id
             AND s.tenant_id = $1
            WHERE cc.tenant_id = $1
              AND cc.normalized_event_id IN (SELECT normalized_event_id FROM scoped_events)
              AND """ + CHUNK_ACL_FILTER + """
              """ + principal_filter + """
            ORDER BY ne.occurred_at DESC, cc.created_at DESC
            LIMIT $5
            """,
            *args,
        )
    return [_chunk_item(r, score=0.8) for r in rows]


def _chunk_item(row, score: float) -> dict:
    meta = row["metadata"]
    meta = json.loads(meta) if isinstance(meta, str) else dict(meta or {})
    occurred = f"{row['occurred_at']:%Y-%m-%d %H:%M}" if row["occurred_at"] else "unknown"
    source_id = _row_get(row, "source_id")
    source_external_id = _row_get(row, "source_external_id") if source_id else None
    source_url = meta.get("source_url") if source_id else None
    return {
        "kind": "chunk",
        "id": str(row["id"]),
        "text": f"[{occurred}] {row['text'][:1200]}",
        "source_url": source_url,
        "source_id": str(source_id) if source_id else None,
        "source_type": _row_get(row, "source_type") if source_id else None,
        "source_external_id": source_external_id,
        "source_identity_hints": _source_identity_hints(source_external_id, _row_get(row, "source_sync_policy")) if source_id else [],
        "source_observed_at": row["occurred_at"].isoformat() if row["occurred_at"] else None,
        "visibility": _row_get(row, "visibility"),
        "allowed_principals": _json_list(_row_get(row, "allowed_principals")),
        "acl_state": _row_get(row, "acl_state"),
        "acl_freshness_state": _row_get(row, "acl_freshness_state"),
        "priority": 0,
        "score": score,
    }


def _row_get(row, key: str):
    try:
        return row[key]
    except (KeyError, IndexError):
        return None


def _json_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return list(value) if isinstance(value, (list, tuple, set)) else []


def _source_identity_hints(external_id: str | None, sync_policy) -> list[str]:
    policy = json.loads(sync_policy) if isinstance(sync_policy, str) else dict(sync_policy or {})
    values = [external_id]
    raw = policy.get("identity_hints")
    if isinstance(raw, list):
        values.extend(str(v) for v in raw if v)
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


# Thin wrappers, not direct function references: tests monkeypatch
# retrieve._open_risk_objects / _stale_work_objects / _upcoming_obligations /
# _recent_contradiction_facts as module attributes (see e.g.
# tests/test_authorized_retrieval.py, tests/test_tenant_context_retrieval.py).
# A wrapper that calls the bare name performs a fresh module-global lookup on
# every invocation, so it picks up a monkeypatched replacement; registering
# the function objects directly would freeze in the pre-patch reference at
# import time and silently ignore the monkeypatch.
async def _view_open_risk(tenant_id: str, *, allowed_principals: list[str] | None = None) -> list[dict]:
    return await _open_risk_objects(tenant_id, allowed_principals=allowed_principals)


async def _view_stale_work(tenant_id: str, *, allowed_principals: list[str] | None = None) -> list[dict]:
    return await _stale_work_objects(tenant_id, allowed_principals=allowed_principals)


async def _view_upcoming_obligations(tenant_id: str, *, allowed_principals: list[str] | None = None) -> list[dict]:
    return await _upcoming_obligations(tenant_id, allowed_principals=allowed_principals)


async def _view_contradictions(tenant_id: str, *, allowed_principals: list[str] | None = None) -> list[dict]:
    return await _recent_contradiction_facts(tenant_id, allowed_principals=allowed_principals)


async def _view_artifacts(tenant_id: str, *, allowed_principals: list[str] | None = None) -> list[dict]:
    return await _artifact_pack_items(tenant_id, allowed_principals=allowed_principals)


# Registered once this module has finished defining its own loader
# functions -- see app/retrieval/pack_policies.py's module docstring for why
# this happens here rather than pack_policies.py importing retrieve.py
# directly (that would be circular: this module imports PackPolicy/
# ViewSpec/get_pack_policy from pack_policies at the top of the file).
register_view_loaders(
    open_risk=_view_open_risk,
    stale_work=_view_stale_work,
    upcoming_obligations=_view_upcoming_obligations,
    contradictions=_view_contradictions,
    artifact=_view_artifacts,
)
