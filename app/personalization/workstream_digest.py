"""Workstream digests: rolling, per-audience extractive summaries.

A workstream digest is a 100-150 token extractive summary of an active
workstream (a project/initiative/commitment brain_object plus its graph
neighbors), one row per `(workstream_key, audience_scope)` pair. Unlike
tenant_context/person_context/channel_context, digests have no
proposal/approve gate -- see the Authority Split section of
architecture-packet-packpolicy-digests-2026-07-11.md for the rationale
(digests are a narrower, mechanically-verifiable claim: "these are the
extractive facts already in brain_objects/facts, compressed", not a
judgment call about priorities or relationships). Code selects and
ACL-intersects evidence per audience_scope before a cheap LLM task drafts
`summary_text`; a code-owned extractive validator (see
app/llm/validators.py::validate_workstream_digest_draft) is the sole gate,
same trust model as brief.py's call 2 output.

Workstream/entity resolution and per-scope evidence selection query
brain_objects/brain_edges/facts directly rather than through
build_authorized_context_pack -- this is the named invariant-2
context-authoring exception (ARCHITECTURE.md), the same category of
operation as app/personalization/channel_context.py's
_channel_event_rows() and app/personalization/person_context.py's
_recent_person_context_event_rows(). Enforced by
tests/test_context_authoring_allowlist.py; the functions below that query
brain tables directly (_workstream_anchor_candidates, _workstream_anchor_row,
_workstream_graph_neighbor_ids, _scope_evidence_rows) are on that allowlist.

The actual LLM-facing step (drafting summary_text from the pre-filtered
evidence bundle) still goes through build_authorized_context_pack --
invariant 2 stays intact for that call, only evidence *selection* upstream
of it is context-authoring code.
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any

import structlog

from app.brain.context_invalidation import replace_context_reference_index
from app.core.db import acquire
from app.core import trace
from app.llm.client import call_llm
from app.llm.validators import normalize_workstream_digest_draft_output, validate_workstream_digest_draft
from app.retrieval.retrieve import build_authorized_context_pack

logger = structlog.get_logger()

DIGESTABLE_OBJECT_TYPES = ("project", "initiative", "commitment")
DIGEST_TOKEN_MIN = 100
DIGEST_TOKEN_MAX = 150
DIGEST_PROMPT_VERSION = "workstream-digest-v2"

# Debounce window for material-merge/contradiction triggers on the same
# workstream_key, matching source_health_cycle's cadence (per the packet's
# State Machine section).
DEBOUNCE_MINUTES = 15
REFRESH_QUEUED_TTL_MINUTES = 30

SYSTEM_DIGEST = (
    "You write short, extractive workstream digests for an internal company "
    "brain. You are given a bounded set of pre-filtered evidence, each tagged "
    'with a <source ref="kind:id"> tag. Write a 100-150 token summary: what '
    "the workstream is, its current state, open items, and the last material "
    "change. Preserve concrete names, dates, and numbers verbatim from the "
    "evidence -- do not paraphrase them away. Only cite refs that appear in "
    "the evidence you were given. Return one flat JSON object with keys "
    '{"summary_text": str, "evidence_refs": [str, ...]}. Do not wrap it '
    "inside a workstream_digest object."
)

PROMPT_DIGEST = (
    "Evidence:\n{context}\n\n"
    "Write the workstream digest as one flat JSON object with exactly the "
    'keys "summary_text" and "evidence_refs". Do not nest it under '
    '"workstream_digest".'
)


def workstream_key_for_object(object_type: str, title: str) -> str:
    return f"{object_type}:{(title or '').strip().lower()}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _company_principal(tenant_id: str) -> str:
    return f"company:{tenant_id}"


def _owner_principal(owner_user_id: str) -> str:
    return f"user:{owner_user_id}"


async def _workstream_anchor_candidates(tenant_id: str) -> list[dict]:
    """Active project/initiative/commitment brain_objects eligible to anchor
    a workstream digest. Direct brain-table query -- invariant-2
    context-authoring exception, see module docstring."""
    async with acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, object_type, title, status
            FROM brain_objects
            WHERE tenant_id = $1
              AND status = 'active'
              AND object_type = ANY($2::text[])
              AND acl_state = 'stamped' AND acl_freshness_state = 'fresh'
            ORDER BY last_seen_at DESC
            """,
            tenant_id,
            list(DIGESTABLE_OBJECT_TYPES),
        )
    return [dict(row) for row in rows]


async def _workstream_anchor_row(tenant_id: str, anchor_object_id: str) -> dict | None:
    """Single anchor-object lookup by id, used by
    maybe_refresh_workstream_digest to resolve workstream_key/status for a
    trigger-driven refresh (as opposed to _workstream_anchor_candidates'
    scheduled-sweep enumeration). Direct brain-table query -- invariant-2
    context-authoring exception, see module docstring."""
    async with acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, object_type, title, status FROM brain_objects WHERE tenant_id = $1 AND id = $2",
            tenant_id,
            anchor_object_id,
        )
    return dict(row) if row else None


async def _workstream_graph_neighbor_ids(tenant_id: str, anchor_object_id: str) -> list[str]:
    """Anchor object + one graph hop, same grouping rule
    workstream_from_alert() applies to a delivered alert's `related` payload,
    computed here directly from brain_edges. Direct brain-table query --
    invariant-2 context-authoring exception, see module docstring."""
    async with acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT DISTINCT CASE WHEN from_object_id = $2 THEN to_object_id ELSE from_object_id END AS id
            FROM brain_edges
            WHERE tenant_id = $1 AND (from_object_id = $2 OR to_object_id = $2)
              AND acl_state = 'stamped' AND acl_freshness_state = 'fresh'
            LIMIT 30
            """,
            tenant_id,
            anchor_object_id,
        )
    neighbor_ids = [str(row["id"]) for row in rows]
    return [anchor_object_id, *neighbor_ids]


def _principals_overlap(allowed_principals: Any, scope_principals: set[str]) -> bool:
    if isinstance(allowed_principals, str):
        try:
            allowed_principals = json.loads(allowed_principals)
        except json.JSONDecodeError:
            allowed_principals = []
    values = {str(p) for p in (allowed_principals or [])}
    return bool(values & scope_principals)


async def _scope_evidence_rows(tenant_id: str, entity_ids: list[str]) -> tuple[list[dict], list[dict]]:
    """All ACL-stamped brain_objects/facts rows for this entity set,
    unfiltered by scope -- scope filtering happens in Python via
    _principals_overlap so this module can compute both the company bundle
    and every distinct owner-private bundle from one fetch instead of one
    query per scope. Direct brain-table query -- invariant-2
    context-authoring exception, see module docstring."""
    if not entity_ids:
        return [], []
    async with acquire() as conn:
        object_rows = await conn.fetch(
            """
            SELECT id, object_type, title, state, confidence, last_seen_at,
                   allowed_principals, acl_state, acl_freshness_state
            FROM brain_objects
            WHERE tenant_id = $1 AND id = ANY($2::uuid[])
              AND acl_state = 'stamped' AND acl_freshness_state = 'fresh'
            """,
            tenant_id,
            entity_ids,
        )
        fact_rows = await conn.fetch(
            """
            SELECT id, claim, label, confidence, observed_at, allowed_principals,
                   acl_state, acl_freshness_state
            FROM facts
            WHERE tenant_id = $1 AND brain_object_id = ANY($2::uuid[])
              AND acl_state = 'stamped' AND acl_freshness_state = 'fresh'
            ORDER BY observed_at DESC
            LIMIT 20
            """,
            tenant_id,
            entity_ids,
        )
    return [dict(row) for row in object_rows], [dict(row) for row in fact_rows]


def _candidate_owner_ids(
    object_rows: list[dict], fact_rows: list[dict], *, company_principal: str
) -> set[str]:
    """Owners with at least one piece of evidence NOT visible to the company
    principal -- i.e. audience-differentiated evidence exists for this
    workstream. Per the packet's Q4 resolution: generate an owner-scope row
    whenever this condition holds, from day one, no manual activation gate."""
    owners: set[str] = set()
    for row in (*object_rows, *fact_rows):
        allowed = row.get("allowed_principals")
        if isinstance(allowed, str):
            try:
                allowed = json.loads(allowed)
            except json.JSONDecodeError:
                allowed = []
        allowed = {str(p) for p in (allowed or [])}
        if company_principal in allowed:
            continue
        for principal in allowed:
            if principal.startswith("user:"):
                owners.add(principal.split(":", 1)[1])
    return owners


def _evidence_refs_for_scope(
    object_rows: list[dict], fact_rows: list[dict], *, scope_principals: set[str]
) -> tuple[list[str], list[str]]:
    """Returns (object_ids, evidence_refs) visible to this scope."""
    object_ids: list[str] = []
    refs: list[str] = []
    for row in object_rows:
        if _principals_overlap(row.get("allowed_principals"), scope_principals):
            object_ids.append(str(row["id"]))
            refs.append(f"object:{row['id']}")
    for row in fact_rows:
        if _principals_overlap(row.get("allowed_principals"), scope_principals):
            refs.append(f"fact:{row['id']}")
    return object_ids, refs


async def _fetch_digest_row(conn, *, tenant_id: str, workstream_key: str, audience_scope: str):
    return await conn.fetchrow(
        """
        SELECT id, tenant_id, workstream_key, anchor_object_id, thread_id,
               source_kind, audience_scope,
               summary_text, evidence_refs, allowed_principals, acl_state,
               acl_freshness_state, freshness_state, last_material_change_at,
               version, model_call_id, context_pack_id, created_at, updated_at
        FROM workstream_digests
        WHERE tenant_id = $1 AND workstream_key = $2 AND audience_scope = $3
        """,
        tenant_id,
        workstream_key,
        audience_scope,
    )


def normalize_workstream_digest(row: dict | None) -> dict | None:
    if not row:
        return None
    row = dict(row)
    evidence_refs = row.get("evidence_refs")
    if isinstance(evidence_refs, str):
        evidence_refs = json.loads(evidence_refs)
    allowed_principals = row.get("allowed_principals")
    if isinstance(allowed_principals, str):
        allowed_principals = json.loads(allowed_principals)
    return {
        "id": str(row["id"]),
        "tenant_id": str(row["tenant_id"]),
        "workstream_key": row["workstream_key"],
        "anchor_object_id": str(row["anchor_object_id"]) if row.get("anchor_object_id") else None,
        "thread_id": str(row["thread_id"]) if row.get("thread_id") else None,
        "source_kind": row.get("source_kind") or "workstream",
        "audience_scope": row["audience_scope"],
        "summary_text": row.get("summary_text") or "",
        "evidence_refs": evidence_refs or [],
        "allowed_principals": allowed_principals or [],
        "acl_state": row.get("acl_state") or "stamped",
        "acl_freshness_state": row.get("acl_freshness_state") or "fresh",
        "freshness_state": row.get("freshness_state") or "fresh",
        "last_material_change_at": row.get("last_material_change_at"),
        "version": int(row.get("version") or 1),
        "model_call_id": str(row["model_call_id"]) if row.get("model_call_id") else None,
        "context_pack_id": str(row["context_pack_id"]) if row.get("context_pack_id") else None,
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


async def get_workstream_digest(*, tenant_id: str, workstream_key: str, audience_scope: str) -> dict | None:
    async with acquire() as conn:
        row = await _fetch_digest_row(conn, tenant_id=tenant_id, workstream_key=workstream_key, audience_scope=audience_scope)
    return normalize_workstream_digest(dict(row) if row else None)


async def mark_workstream_digest_stale(*, tenant_id: str, workstream_key: str, audience_scope: str) -> None:
    async with acquire() as conn:
        await conn.execute(
            """
            UPDATE workstream_digests
            SET freshness_state = 'stale', last_material_change_at = now(), updated_at = now()
            WHERE tenant_id = $1 AND workstream_key = $2 AND audience_scope = $3
              AND freshness_state != 'stale'
            """,
            tenant_id,
            workstream_key,
            audience_scope,
        )


async def _persist_digest(
    *,
    tenant_id: str,
    workstream_key: str,
    anchor_object_id: str | None,
    audience_scope: str,
    summary_text: str,
    evidence_refs: list[str],
    allowed_principals: list[str],
    model_call_id: str | None,
    context_pack_id: str | None,
    trigger: str,
    thread_id: str | None = None,
    source_kind: str = "workstream",
) -> dict:
    async with acquire() as conn:
        current_row = await _fetch_digest_row(conn, tenant_id=tenant_id, workstream_key=workstream_key, audience_scope=audience_scope)
        current = normalize_workstream_digest(dict(current_row) if current_row else None)
        next_version = (current["version"] + 1) if current else 1
        previous_summary = current["summary_text"] if current else None
        row = await conn.fetchrow(
            """
            INSERT INTO workstream_digests (
                tenant_id, workstream_key, anchor_object_id, thread_id, source_kind,
                audience_scope, summary_text, evidence_refs, allowed_principals,
                acl_state, acl_freshness_state, freshness_state,
                last_material_change_at, version, model_call_id, context_pack_id
            )
            VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9::jsonb, 'stamped', 'fresh',
                'fresh', now(), $10, $11, $12
            )
            ON CONFLICT (tenant_id, workstream_key, audience_scope) DO UPDATE
            SET summary_text = EXCLUDED.summary_text,
                evidence_refs = EXCLUDED.evidence_refs,
                allowed_principals = EXCLUDED.allowed_principals,
                acl_state = 'stamped',
                acl_freshness_state = 'fresh',
                freshness_state = 'fresh',
                last_material_change_at = now(),
                version = $10,
                model_call_id = $11,
                context_pack_id = $12,
                updated_at = now()
            RETURNING id, tenant_id, workstream_key, anchor_object_id, thread_id,
                      source_kind, audience_scope,
                      summary_text, evidence_refs, allowed_principals, acl_state,
                      acl_freshness_state, freshness_state, last_material_change_at,
                      version, model_call_id, context_pack_id, created_at, updated_at
            """,
            tenant_id,
            workstream_key,
            anchor_object_id,
            thread_id,
            source_kind,
            audience_scope,
            summary_text,
            json.dumps(evidence_refs),
            json.dumps(allowed_principals),
            next_version,
            model_call_id,
            context_pack_id,
        )
        persisted = normalize_workstream_digest(dict(row))
        await conn.execute(
            """
            INSERT INTO workstream_digest_versions (
                tenant_id, digest_id, version, previous_summary, new_summary,
                evidence_refs, trigger, model_call_id
            )
            VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8)
            """,
            tenant_id,
            persisted["id"],
            next_version,
            previous_summary,
            summary_text,
            json.dumps(evidence_refs),
            trigger,
            model_call_id,
        )
        # P1 fix (Slice 3.3 review): the contradiction-triggered invalidation
        # cascade (app.brain.context_invalidation.invalidate_referencing_
        # context -> _enqueue_digest_refresh) re-derives a refresh by
        # calling maybe_refresh_workstream_digest(anchor_object_id=...) --
        # a thread digest has no anchor_object_id (that's exactly why
        # migration 0077 made the column nullable), so indexing it here
        # would hand that cascade a NULL anchor and crash on the next
        # evidence change touching this digest's refs (str(None) bound as
        # a UUID query param). Thread digests are not wired into this
        # cascade yet -- a missed trigger degrades to "stale until the next
        # explicit maybe_refresh_thread_digest call", the same
        # additive-only trust model this hook already applies to
        # workstream digests, not a correctness regression. Revisit once a
        # thread-appropriate re-derivation path exists.
        if source_kind == "workstream":
            await replace_context_reference_index(
                conn,
                tenant_id=tenant_id,
                context_kind="workstream_digest",
                context_id=persisted["id"],
                refs=evidence_refs,
            )
    return persisted


def _extractive_signal_tokens(object_rows: list[dict], fact_rows: list[dict]) -> list[str]:
    """Named entities/dates/numbers pulled straight from the evidence text,
    used by the validator to check the draft preserved them verbatim rather
    than gisting them away."""
    tokens: set[str] = set()
    date_pattern = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
    number_pattern = re.compile(r"\b\d+(?:\.\d+)?\b")
    for row in object_rows:
        title = str(row.get("title") or "")
        for word in title.split():
            cleaned = word.strip(".,()")
            if cleaned and cleaned[0].isupper():
                tokens.add(cleaned)
        state = row.get("state")
        if isinstance(state, str):
            try:
                state = json.loads(state)
            except json.JSONDecodeError:
                state = {}
        for value in (state or {}).values():
            if not value:
                continue
            for match in date_pattern.findall(str(value)):
                tokens.add(match)
    for row in fact_rows:
        claim = str(row.get("claim") or "")
        tokens.update(date_pattern.findall(claim))
        tokens.update(number_pattern.findall(claim))
    return sorted(tokens)


async def _draft_and_write_scope(
    *,
    tenant_id: str,
    workstream_key: str,
    anchor_object_id: str | None,
    audience_scope: str,
    allowed_principals: list[str],
    object_ids: list[str],
    evidence_refs: list[str],
    object_rows: list[dict],
    fact_rows: list[dict],
    actor_user_id: str | None,
    target_audience: str,
    actor_principals: list[str],
    trigger: str,
    pipeline_run_id: str | None,
    run_id: str,
    thread_id: str | None = None,
    source_kind: str = "workstream",
) -> dict | None:
    if not evidence_refs:
        return None

    # The LLM-facing pack build for the digest draft itself: this DOES go
    # through build_authorized_context_pack (invariant 2 intact for the
    # LLM-facing step), scoped to exactly this workstream's entity set via
    # object_ids -- evidence *selection* (which scope, which entities) is
    # the context-authoring code above; pack *assembly* for the model call
    # is the sanctioned path.
    async with trace.step(run_id, tenant_id, "digest_pack_built"):
        pack = await build_authorized_context_pack(
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            target_audience=target_audience,
            purpose="workstream_digest_draft",
            object_ids=object_ids,
            actor_principals=actor_principals,
        )

    if not pack["items"]:
        return None

    async with trace.step(run_id, tenant_id, "digest_drafted", input_ref=pack["context_pack_id"]):
        result = await call_llm(
            task="workstream_digest_draft",
            system=SYSTEM_DIGEST,
            prompt=PROMPT_DIGEST.format(context=pack["text"]),
            tenant_id=tenant_id,
            max_tokens=400,
            expect_json=True,
            prompt_version=DIGEST_PROMPT_VERSION,
            context_pack_id=pack["context_pack_id"],
            pipeline_run_id=pipeline_run_id,
            validation_pack_items=pack["items"],
        )

    parsed = result.get("parsed") or {}
    normalized = normalize_workstream_digest_draft_output(parsed, pack_items=pack["items"])
    signal_tokens = _extractive_signal_tokens(object_rows, fact_rows)
    check = validate_workstream_digest_draft(
        parsed,
        pack_items=pack["items"],
        signal_tokens=signal_tokens,
    )
    async with trace.step(run_id, tenant_id, "digest_validated", input_ref=result.get("model_call_id")) as st:
        st.detail["valid"] = check.valid
    if not check.valid:
            st.skip(str(check.reason) if check.reason else "validation_failed")
            logger.warning(
                "workstream_digest_rejected_invalid_output",
                tenant_id=tenant_id,
                workstream_key=workstream_key,
                audience_scope=audience_scope,
                reason=check.reason,
                details=check.details,
            )
            return None
    if not normalized:
        logger.warning(
            "workstream_digest_rejected_unparseable_normalization",
            tenant_id=tenant_id,
            workstream_key=workstream_key,
            audience_scope=audience_scope,
        )
        return None

    async with trace.step(run_id, tenant_id, "digest_written", input_ref=result.get("model_call_id")):
        persisted = await _persist_digest(
            tenant_id=tenant_id,
            workstream_key=workstream_key,
            anchor_object_id=anchor_object_id,
            audience_scope=audience_scope,
            summary_text=str(normalized.get("summary_text") or ""),
            evidence_refs=[ref for ref in (normalized.get("evidence_refs") or []) if ref in evidence_refs] or evidence_refs,
            allowed_principals=allowed_principals,
            model_call_id=result.get("model_call_id"),
            context_pack_id=pack["context_pack_id"],
            trigger=trigger,
            thread_id=thread_id,
            source_kind=source_kind,
        )
    return persisted


async def maybe_refresh_workstream_digest(
    *,
    tenant_id: str,
    anchor_object_id: str,
    trigger: str = "scheduled",
    pipeline_run_id: str | None = None,
    run_id: str | None = None,
) -> list[dict]:
    """Generate/refresh workstream_digests rows for every audience_scope
    that has visible, audience-differentiated evidence for this workstream.
    Company scope is generated whenever any company-visible evidence
    exists; an owner:{id} scope is generated whenever that owner has at
    least one piece of evidence not visible to the company (per the
    packet's Q4 resolution -- no manual activation gate, the condition IS
    the observation). Returns the list of persisted digest rows.

    Owns its own trace.step run when the caller doesn't already have one in
    flight (run_id not passed): every pipeline stage from trigger to write
    must appear in pipeline_runs/steps, per invariant 5."""
    owns_run = run_id is None
    if owns_run:
        run_id = await trace.start_run(tenant_id=tenant_id, kind="workstream_digest")

    async with trace.step(run_id, tenant_id, "workstream_resolved", input_ref=anchor_object_id) as resolve_step:
        anchor_row = await _workstream_anchor_row(tenant_id, anchor_object_id)
        if not anchor_row:
            resolve_step.skip("anchor_object_not_found")
            if owns_run:
                await trace.finish_run(run_id, status="skipped")
            return []
        anchor = dict(anchor_row)
        workstream_key = workstream_key_for_object(anchor["object_type"], anchor["title"])
        resolve_step.detail["workstream_key"] = workstream_key

    if anchor.get("status") != "active":
        async with trace.step(run_id, tenant_id, "workstream_retired", input_ref=anchor_object_id):
            await _retire_workstream_digests(tenant_id=tenant_id, workstream_key=workstream_key)
        if owns_run:
            await trace.finish_run(run_id, status="completed")
        return []

    async with trace.step(run_id, tenant_id, "evidence_resolved", input_ref=anchor_object_id) as evidence_step:
        entity_ids = await _workstream_graph_neighbor_ids(tenant_id, anchor_object_id)
        object_rows, fact_rows = await _scope_evidence_rows(tenant_id, entity_ids)
        evidence_step.detail["entity_count"] = len(entity_ids)

    company_principal = _company_principal(tenant_id)
    persisted: list[dict] = []

    company_object_ids, company_refs = _evidence_refs_for_scope(
        object_rows, fact_rows, scope_principals={company_principal}
    )
    if company_refs:
        result = await _draft_and_write_scope(
            tenant_id=tenant_id,
            workstream_key=workstream_key,
            anchor_object_id=anchor_object_id,
            audience_scope="company",
            allowed_principals=[company_principal],
            object_ids=company_object_ids,
            evidence_refs=company_refs,
            object_rows=object_rows,
            fact_rows=fact_rows,
            actor_user_id=None,
            target_audience="company",
            actor_principals=[],
            trigger=trigger,
            pipeline_run_id=pipeline_run_id,
            run_id=run_id,
        )
        if result:
            persisted.append(result)

    owner_ids = _candidate_owner_ids(object_rows, fact_rows, company_principal=company_principal)
    for owner_id in sorted(owner_ids):
        owner_principal = _owner_principal(owner_id)
        owner_object_ids, owner_refs = _evidence_refs_for_scope(
            object_rows, fact_rows, scope_principals={company_principal, owner_principal}
        )
        if not owner_refs:
            continue
        result = await _draft_and_write_scope(
            tenant_id=tenant_id,
            workstream_key=workstream_key,
            anchor_object_id=anchor_object_id,
            audience_scope=f"owner:{owner_id}",
            allowed_principals=[owner_principal],
            object_ids=owner_object_ids,
            evidence_refs=owner_refs,
            object_rows=object_rows,
            fact_rows=fact_rows,
            actor_user_id=owner_id,
            target_audience="private_user",
            actor_principals=[company_principal],
            trigger=trigger,
            pipeline_run_id=pipeline_run_id,
            run_id=run_id,
        )
        if result:
            persisted.append(result)

    if owns_run:
        await trace.finish_run(run_id, status="completed" if persisted else "skipped")
    return persisted


def workstream_key_for_thread(thread_id: str) -> str:
    """Never collides with a real workstream_key (those are always
    `object_type:title`, and no object_type is literally "thread")."""
    return f"thread:{thread_id}"


async def _thread_referenced_object_ids(tenant_id: str, thread_id: str) -> list[str]:
    """Entity set for a thread digest: brain_object ids referenced by the
    thread's own tool_call/tool_result session_messages -- see
    app/sessions/tools.py's _tool_search/_tool_dig result shape,
    `items: [{"kind": "object"|"fact", "id": ..., "text": ...}]`, stored in
    session_messages.payload["result"]["items"] by app/sessions/loop.py.
    This queries session_messages, not a brain table (BRAIN_TABLES in
    tests/test_context_authoring_allowlist.py doesn't include it), so it
    doesn't need an invariant-2 context-authoring allowlist entry -- it's
    resolving *which entities this thread touched*, the same category of
    lookup _workstream_graph_neighbor_ids does for a brain_object anchor,
    just sourced from the thread's own tool-call history instead of
    brain_edges."""
    async with acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT payload FROM session_messages
            WHERE tenant_id = $1 AND thread_id = $2 AND role = 'tool_result'
            ORDER BY created_at DESC
            LIMIT 200
            """,
            tenant_id,
            thread_id,
        )
    object_ids: list[str] = []
    seen: set[str] = set()
    for row in rows:
        payload = row["payload"]
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                payload = {}
        result = (payload or {}).get("result") or {}
        for item in result.get("items") or []:
            if not isinstance(item, dict) or item.get("kind") != "object":
                continue
            object_id = item.get("id")
            if object_id and object_id not in seen:
                seen.add(object_id)
                object_ids.append(object_id)
    return object_ids


async def maybe_refresh_thread_digest(
    *,
    tenant_id: str,
    thread_id: str,
    trigger: str = "scheduled",
    pipeline_run_id: str | None = None,
    run_id: str | None = None,
) -> list[dict]:
    """Thread digest generation (Slice 3.3, per the packet's "no new table,
    no new maintenance task" design): reuses
    maybe_refresh_workstream_digest's exact machinery -- same extractive
    validator, same per-audience ACL evidence selection, same versioning,
    same context_reference_index write via _persist_digest -- scoped to a
    thread's own referenced entity set (see _thread_referenced_object_ids)
    instead of a brain_object's graph neighborhood. workstream_key is
    `thread:{thread_id}` and source_kind='thread' distinguishes these rows
    from ordinary workstream digests for @thread resolution and retrieval
    filtering.

    A thread with no resolved entities yet (no search/dig tool call has
    surfaced any brain_object) produces no digest row -- same "no evidence,
    no digest" behavior as maybe_refresh_workstream_digest, not an error."""
    owns_run = run_id is None
    if owns_run:
        run_id = await trace.start_run(tenant_id=tenant_id, kind="thread_digest")

    workstream_key = workstream_key_for_thread(thread_id)

    async with trace.step(run_id, tenant_id, "thread_entity_set_resolved", input_ref=thread_id) as resolve_step:
        entity_ids = await _thread_referenced_object_ids(tenant_id, thread_id)
        resolve_step.detail["entity_count"] = len(entity_ids)

    if not entity_ids:
        if owns_run:
            await trace.finish_run(run_id, status="skipped")
        return []

    async with trace.step(run_id, tenant_id, "thread_evidence_resolved", input_ref=thread_id) as evidence_step:
        object_rows, fact_rows = await _scope_evidence_rows(tenant_id, entity_ids)
        evidence_step.detail["entity_count"] = len(entity_ids)

    company_principal = _company_principal(tenant_id)
    persisted: list[dict] = []

    company_object_ids, company_refs = _evidence_refs_for_scope(
        object_rows, fact_rows, scope_principals={company_principal}
    )
    if company_refs:
        result = await _draft_and_write_scope(
            tenant_id=tenant_id,
            workstream_key=workstream_key,
            anchor_object_id=None,
            audience_scope="company",
            allowed_principals=[company_principal],
            object_ids=company_object_ids,
            evidence_refs=company_refs,
            object_rows=object_rows,
            fact_rows=fact_rows,
            actor_user_id=None,
            target_audience="company",
            actor_principals=[],
            trigger=trigger,
            pipeline_run_id=pipeline_run_id,
            run_id=run_id,
            thread_id=thread_id,
            source_kind="thread",
        )
        if result:
            persisted.append(result)

    owner_ids = _candidate_owner_ids(object_rows, fact_rows, company_principal=company_principal)
    for owner_id in sorted(owner_ids):
        owner_principal = _owner_principal(owner_id)
        owner_object_ids, owner_refs = _evidence_refs_for_scope(
            object_rows, fact_rows, scope_principals={company_principal, owner_principal}
        )
        if not owner_refs:
            continue
        result = await _draft_and_write_scope(
            tenant_id=tenant_id,
            workstream_key=workstream_key,
            anchor_object_id=None,
            audience_scope=f"owner:{owner_id}",
            allowed_principals=[owner_principal],
            object_ids=owner_object_ids,
            evidence_refs=owner_refs,
            object_rows=object_rows,
            fact_rows=fact_rows,
            actor_user_id=owner_id,
            target_audience="private_user",
            actor_principals=[company_principal],
            trigger=trigger,
            pipeline_run_id=pipeline_run_id,
            run_id=run_id,
            thread_id=thread_id,
            source_kind="thread",
        )
        if result:
            persisted.append(result)

    if owns_run:
        await trace.finish_run(run_id, status="completed" if persisted else "skipped")
    return persisted


async def get_thread_digest(*, tenant_id: str, thread_id: str, audience_scope: str = "company") -> dict | None:
    return await get_workstream_digest(
        tenant_id=tenant_id,
        workstream_key=workstream_key_for_thread(thread_id),
        audience_scope=audience_scope,
    )


async def list_thread_digests(*, tenant_id: str, thread_id: str) -> list[dict]:
    """Every non-retired audience-scope digest row for one thread (company
    plus any owner-private rows), used by @thread resolution and idea
    filing to build a match candidate from whichever scope the requester
    can see."""
    workstream_key = workstream_key_for_thread(thread_id)
    async with acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, tenant_id, workstream_key, anchor_object_id, thread_id,
                   source_kind, audience_scope,
                   summary_text, evidence_refs, allowed_principals, acl_state,
                   acl_freshness_state, freshness_state, last_material_change_at,
                   version, model_call_id, context_pack_id, created_at, updated_at
            FROM workstream_digests
            WHERE tenant_id = $1 AND workstream_key = $2 AND freshness_state != 'retired'
            """,
            tenant_id,
            workstream_key,
        )
    return [d for d in (normalize_workstream_digest(dict(r)) for r in rows) if d]


async def list_all_thread_digests(*, tenant_id: str) -> list[dict]:
    """Every non-retired thread-digest row for a tenant, across all threads
    -- the candidate pool for @thread resolution (app.sessions.
    thread_resolution.resolve_thread_reference)."""
    async with acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, tenant_id, workstream_key, anchor_object_id, thread_id,
                   source_kind, audience_scope,
                   summary_text, evidence_refs, allowed_principals, acl_state,
                   acl_freshness_state, freshness_state, last_material_change_at,
                   version, model_call_id, context_pack_id, created_at, updated_at
            FROM workstream_digests
            WHERE tenant_id = $1 AND source_kind = 'thread' AND freshness_state != 'retired'
            """,
            tenant_id,
        )
    return [d for d in (normalize_workstream_digest(dict(r)) for r in rows) if d]


async def sweep_workstream_digests(*, tenant_id: str) -> list[dict]:
    """Scheduled-sweep trigger (per the packet's Pipeline section): refresh
    every active project/initiative/commitment workstream that doesn't yet
    have a fresh digest, or whose digest has gone stale. Cron wiring itself
    (a `workstream_digest_cycle` alongside `daily_brief_cycle`/
    `okr_pace_cycle`) is a follow-on named here, not scheduled in this
    slice -- this function is the callable body a future cron entry point
    would invoke, kept independently callable/testable now."""
    candidates = await _workstream_anchor_candidates(tenant_id)
    persisted: list[dict] = []
    for candidate in candidates:
        result = await maybe_refresh_workstream_digest(
            tenant_id=tenant_id,
            anchor_object_id=str(candidate["id"]),
            trigger="scheduled",
        )
        persisted.extend(result)
    return persisted


async def _retire_workstream_digests(*, tenant_id: str, workstream_key: str) -> None:
    async with acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, evidence_refs FROM workstream_digests WHERE tenant_id = $1 AND workstream_key = $2",
            tenant_id,
            workstream_key,
        )
        for row in rows:
            await conn.execute(
                """
                UPDATE workstream_digests
                SET freshness_state = 'retired', updated_at = now()
                WHERE tenant_id = $1 AND id = $2
                """,
                tenant_id,
                row["id"],
            )


def workstream_digest_pack_item(digest: dict) -> dict | None:
    """Renders an approved workstream_digests row into a pack item, for any
    PackPolicy that opts a "digests" ViewSpec into its `views` tuple (not
    activated broadly yet -- see app/retrieval/pack_policies.py). A stale
    digest is still returned (staleness is a quality signal, not an ACL
    gate) but annotated so downstream synthesis/verification can weight it
    lower; a retired digest is excluded from all views."""
    if not digest or digest.get("freshness_state") == "retired":
        return None
    summary = digest.get("summary_text") or ""
    if not summary:
        return None
    return {
        "kind": "workstream_digest",
        "id": digest["id"],
        "text": f"Workstream digest ({digest['workstream_key']}): {summary}",
        "priority": 3,
        "score": 1.0,
        "visibility": "company" if digest.get("audience_scope") == "company" else "restricted",
        "allowed_principals": digest.get("allowed_principals") or [],
        "acl_state": digest.get("acl_state") or "stamped",
        "acl_freshness_state": digest.get("acl_freshness_state") or "fresh",
        "freshness_state": digest.get("freshness_state") or "fresh",
    }


async def _workstream_digest_items(tenant_id: str, *, allowed_principals: list[str] | None = None) -> list[dict]:
    """The "digests" ViewSpec loader (app/retrieval/pack_policies.py),
    registered by this module at import time. Returns fresh/stale (never
    retired) digest pack items visible to the given principals."""
    allowed = set(allowed_principals or [])
    async with acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, tenant_id, workstream_key, anchor_object_id, audience_scope,
                   summary_text, evidence_refs, allowed_principals, acl_state,
                   acl_freshness_state, freshness_state, last_material_change_at,
                   version, model_call_id, context_pack_id, created_at, updated_at
            FROM workstream_digests
            WHERE tenant_id = $1 AND freshness_state != 'retired'
            """,
            tenant_id,
        )
    items = []
    for row in rows:
        digest = normalize_workstream_digest(dict(row))
        if not digest:
            continue
        if not (set(digest.get("allowed_principals") or []) & allowed):
            continue
        item = workstream_digest_pack_item(digest)
        if item:
            items.append(item)
    return items


try:  # pragma: no cover - defensive against import-order issues in isolated unit tests
    from app.retrieval.pack_policies import register_view_loaders

    register_view_loaders(digests=_workstream_digest_items)
except Exception:  # pragma: no cover
    logger.warning("workstream_digest_view_loader_registration_failed")
