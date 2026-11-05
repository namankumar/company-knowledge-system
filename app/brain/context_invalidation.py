"""Contradiction-triggered context invalidation (Slice 4, packpolicy-digests
packet).

`context_reference_index` is a reverse index: which durable context entries
(tenant/person/channel context, workstream digests) cite which entities.
`invalidate_referencing_context()` is the single shared hook every entity
state-change call site converges on -- brain/merge.py calls it right after
writing a contradiction fact and right after any status-transition-to-
non-active write (see module-level note below on the latter). Same hook
fires for all three trigger sources named in the packet's Pipeline section
(contradiction fact, object archival, and a future OKR-change event), so
there is exactly one place this logic lives.

Additive-only, per the packet's Trust Requirement: a missed trigger
degrades to "stale until next scheduled pass" (workstream digests) or "not
re-proposed until the next scheduled scan" (tenant/person/channel context),
never to "wrong forever" -- this hook is a freshness accelerant, not the
only path to eventual correctness.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import structlog

from app.core.db import acquire

logger = structlog.get_logger()

# Matches app/personalization/workstream_digest.py's DEBOUNCE_MINUTES --
# multiple triggers on the same digest within this window collapse into one
# refresh_queued transition + one enqueued refresh, not one per trigger.
DEBOUNCE_MINUTES = 15


def _object_entity_ids_from_refs(refs: list[str]) -> list[str]:
    """context_reference_index.entity_id targets brain_objects.id (or
    entity_registry.id) only -- a "fact:uuid" ref points at a facts row, not
    a brain_object, so it has no entity_id to record here. This mirrors the
    filter workstream_digest.py's digest-generation path already applies."""
    entity_ids: list[str] = []
    for ref in refs or []:
        if not isinstance(ref, str) or ":" not in ref:
            continue
        kind, _, entity_id = ref.partition(":")
        if kind != "object":
            continue
        entity_ids.append(entity_id)
    return list(dict.fromkeys(entity_ids))  # de-dupe, preserve order


async def replace_context_reference_index(
    conn, *, tenant_id: str, context_kind: str, context_id: str, refs: list[str]
) -> None:
    """Delete-then-reinsert this context entry's entity rows in
    context_reference_index, derived directly from its cited refs
    (evidence_refs for workstream digests, source_refs for tenant/person/
    channel context proposals). Shared by workstream_digest.py's digest
    generation and the approve_*_proposal retrofits below -- one function,
    one place this derivation logic lives, per the packet's Durable Objects
    note that this index is "safely rebuildable from evidence_refs/
    source_refs on the durable rows if it's ever dropped" (see
    rebuild_context_reference_index)."""
    entity_ids = _object_entity_ids_from_refs(refs)
    await conn.execute(
        "DELETE FROM context_reference_index WHERE tenant_id = $1 AND context_kind = $2 AND context_id = $3",
        tenant_id,
        context_kind,
        context_id,
    )
    for entity_id in entity_ids:
        await conn.execute(
            """
            INSERT INTO context_reference_index (tenant_id, context_kind, context_id, entity_id, entity_kind)
            VALUES ($1, $2, $3, $4, 'brain_object')
            ON CONFLICT (tenant_id, context_kind, context_id, entity_id) DO NOTHING
            """,
            tenant_id,
            context_kind,
            context_id,
            entity_id,
        )


async def _referencing_rows(tenant_id: str, entity_id: str, entity_kind: str) -> list[dict]:
    async with acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT DISTINCT context_kind, context_id
            FROM context_reference_index
            WHERE tenant_id = $1 AND entity_id = $2 AND entity_kind = $3
            """,
            tenant_id,
            entity_id,
            entity_kind,
        )
    return [dict(row) for row in rows]


async def _debounce_and_mark_stale(tenant_id: str, digest_id: str) -> bool:
    """Returns True if this call actually transitioned the digest (i.e. it
    was not already within the debounce window of a prior trigger) -- the
    signal the caller uses to decide whether to enqueue a refresh."""
    async with acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, workstream_key, anchor_object_id, freshness_state, last_material_change_at
            FROM workstream_digests
            WHERE tenant_id = $1 AND id = $2
            FOR UPDATE
            """,
            tenant_id,
            digest_id,
        )
        if not row:
            return False
        last_change = row["last_material_change_at"]
        now = datetime.now(timezone.utc)
        if last_change and (now - last_change) < timedelta(minutes=DEBOUNCE_MINUTES):
            return False  # a trigger already landed within the debounce window
        await conn.execute(
            """
            UPDATE workstream_digests
            SET freshness_state = 'stale', last_material_change_at = now(), updated_at = now()
            WHERE tenant_id = $1 AND id = $2
            """,
            tenant_id,
            digest_id,
        )
    return True


async def _enqueue_digest_refresh(tenant_id: str, digest_id: str) -> None:
    """The "code enqueues a workstream_digest_draft job" transition
    (stale -> refresh_queued -> fresh) from the packet's state machine.
    This codebase has no arq job wired for this yet (out of scope for this
    slice, named as a follow-on); in its place this calls
    maybe_refresh_workstream_digest directly and synchronously, which is a
    safe substitute -- it performs the exact same draft/validate/write work
    a queued job would, just inline rather than deferred to a worker.
    A test double can monkeypatch this function to assert it fires at most
    once per debounce window without doing real work (see
    tests/test_context_invalidation.py's debounce test).

    Slice 3.3 P1 fix: a thread digest (`source_kind='thread'`) has no
    `anchor_object_id` -- `maybe_refresh_workstream_digest` requires one
    (it looks up a brain_object by that id), so blindly forwarding a NULL
    anchor here would bind the literal string "None" as a UUID query
    parameter and raise at the database layer the first time a thread
    digest's cited evidence changed. Defense in depth: 3.3's write path
    (workstream_digest.py::_persist_digest) no longer indexes thread
    digests into context_reference_index at all, so this function
    shouldn't normally see one -- but this guard makes the function itself
    safe regardless of what's in the index (e.g. a stale row from before
    that fix, or a future write path that indexes them again by mistake).
    A thread-kind or anchor-less row here degrades to "not refreshed by
    this cascade" (skip + log), never a crash -- same additive-only
    trust model as every other branch in this hook."""
    from app.personalization.workstream_digest import maybe_refresh_workstream_digest

    async with acquire() as conn:
        row = await conn.fetchrow(
            "SELECT anchor_object_id, source_kind FROM workstream_digests WHERE tenant_id = $1 AND id = $2",
            tenant_id,
            digest_id,
        )
    if not row:
        return
    if row["source_kind"] != "workstream" or not row["anchor_object_id"]:
        logger.warning(
            "context_invalidation_skipped_non_workstream_digest",
            tenant_id=tenant_id,
            digest_id=digest_id,
            source_kind=row["source_kind"],
        )
        return
    await maybe_refresh_workstream_digest(
        tenant_id=tenant_id,
        anchor_object_id=str(row["anchor_object_id"]),
        trigger="contradiction",
    )


# Context kinds whose durable row is a founder/operator-reviewed proposal
# (tenant_context, person_context, channel_context) rather than a
# code-published digest. Kept as a set here (not imported from each
# personalization module) to avoid a context_invalidation -> personalization
# -> context_invalidation import cycle; the propose functions themselves are
# resolved via a deferred import in _trigger_immediate_proposal below.
_PROPOSAL_CONTEXT_KINDS = {"tenant_context", "person_context", "channel_context"}


async def _trigger_immediate_proposal(tenant_id: str, context_id: str, context_kind: str) -> None:
    """Same review gate as today (founder/operator approval), just enqueued
    immediately instead of waiting for the next scheduled
    maybe_propose_*_update() pass. Each propose function has its own
    signature (channel_context's needs only tenant_id; tenant_context's and
    person_context's need a pre-built "fresh" context_pack -- the same
    pack their normal post-brief call site already builds), so this
    resolves exactly what each one needs before calling it, rather than
    exposing a new simplified variant of each function."""
    if context_kind == "channel_context":
        from app.personalization.channel_context import maybe_propose_channel_context_update

        await maybe_propose_channel_context_update(tenant_id=tenant_id)
        return

    if context_kind == "tenant_context":
        from app.personalization.tenant_context import maybe_propose_tenant_context_update
        from app.retrieval.retrieve import build_authorized_context_pack

        pack = await build_authorized_context_pack(
            tenant_id=tenant_id,
            actor_user_id=None,
            target_audience="company",
            purpose="tenant_context_seed",
        )
        if not pack["items"]:
            return
        await maybe_propose_tenant_context_update(tenant_id=tenant_id, context_pack=pack)
        return

    if context_kind == "person_context":
        from app.personalization.person_context import maybe_propose_person_context_update
        from app.retrieval.retrieve import build_authorized_context_pack

        async with acquire() as conn:
            row = await conn.fetchrow(
                "SELECT owner_user_id, subject_entity_id FROM person_contexts WHERE tenant_id = $1 AND id = $2",
                tenant_id,
                context_id,
            )
        if not row:
            return
        owner_user_id = str(row["owner_user_id"])
        subject_entity_id = str(row["subject_entity_id"])
        pack = await build_authorized_context_pack(
            tenant_id=tenant_id,
            actor_user_id=owner_user_id,
            target_audience="private_user",
            purpose="person_context_seed",
        )
        if not pack["items"]:
            return
        await maybe_propose_person_context_update(
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            subject_entity_id=subject_entity_id,
            context_pack=pack,
        )


async def invalidate_referencing_context(
    tenant_id: str, entity_id: str, entity_kind: str = "brain_object"
) -> dict:
    """Look up every durable context entry that cites `entity_id` (via
    context_reference_index) and either mark it stale + debounced-enqueue a
    digest refresh (workstream_digest) or trigger an immediate re-propose
    (tenant/person/channel context). Returns a summary dict for callers/tests
    to assert on: {"digests_marked_stale": int, "digests_refresh_enqueued":
    int, "context_kinds_proposed": [str, ...]}."""
    rows = await _referencing_rows(tenant_id, entity_id, entity_kind)
    digests_marked_stale = 0
    digests_refresh_enqueued = 0
    context_kinds_proposed: set[str] = set()
    proposal_targets: list[tuple[str, str]] = []  # (context_kind, context_id), deduped below

    for row in rows:
        context_kind = row["context_kind"]
        context_id = str(row["context_id"])
        if context_kind == "workstream_digest":
            transitioned = await _debounce_and_mark_stale(tenant_id, context_id)
            if transitioned:
                digests_marked_stale += 1
                await _enqueue_digest_refresh(tenant_id, context_id)
                digests_refresh_enqueued += 1
        elif context_kind in _PROPOSAL_CONTEXT_KINDS:
            if context_kind not in context_kinds_proposed:
                proposal_targets.append((context_kind, context_id))
            context_kinds_proposed.add(context_kind)
        else:
            logger.warning("context_invalidation_unknown_context_kind", context_kind=context_kind)

    for context_kind, context_id in proposal_targets:
        await _trigger_immediate_proposal(tenant_id, context_id, context_kind)

    return {
        "digests_marked_stale": digests_marked_stale,
        "digests_refresh_enqueued": digests_refresh_enqueued,
        "context_kinds_proposed": sorted(context_kinds_proposed),
    }


def entry_refs(entries) -> list[str]:
    """Flattens the `refs`/`source_refs` arrays off a list of tenant/person/
    channel-context entry dicts (each entry has its own citation list --
    these tables have no single top-level evidence_refs column, unlike
    workstream_digests). Public: shared by rebuild_context_reference_index
    below and each module's approve_*_proposal retrofit."""
    refs: list[str] = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        this_entry_refs = entry.get("refs") or entry.get("source_refs")
        if isinstance(this_entry_refs, list):
            refs.extend(str(ref) for ref in this_entry_refs if ref)
    return refs


def context_refs_from_entries(context: dict, *, list_fields: tuple[str, ...], object_fields: tuple[str, ...] = ()) -> list[str]:
    """Collects every entry ref across a normalized tenant/person/channel
    context dict's list fields plus any single-object fields (e.g.
    current_plan, relationship_kind)."""
    refs: list[str] = []
    for field in list_fields:
        refs.extend(entry_refs(context.get(field)))
    for field in object_fields:
        value = context.get(field)
        if isinstance(value, dict):
            refs.extend(entry_refs([value]))
    return refs


async def rebuild_context_reference_index(tenant_id: str) -> dict:
    """Drop and rebuild context_reference_index from evidence_refs/
    source_refs on all durable context rows for this tenant -- proves the
    "it's a derived index, safely rebuildable" claim in the packet's
    Durable Objects section isn't just an assertion. Returns counts per
    context_kind rebuilt."""
    import json

    rebuilt: dict[str, int] = {"workstream_digest": 0, "tenant_context": 0, "person_context": 0, "channel_context": 0}

    async with acquire() as conn:
        # Slice 3.3 P1 fix: only source_kind='workstream' rows belong in
        # this index -- see _enqueue_digest_refresh's docstring for why a
        # thread digest (anchor_object_id NULL by construction) would crash
        # the invalidation cascade if it were re-indexed here.
        digest_rows = await conn.fetch(
            "SELECT id, evidence_refs FROM workstream_digests WHERE tenant_id = $1 AND source_kind = 'workstream'",
            tenant_id,
        )
        for row in digest_rows:
            evidence_refs = row["evidence_refs"]
            if isinstance(evidence_refs, str):
                evidence_refs = json.loads(evidence_refs)
            await replace_context_reference_index(
                conn,
                tenant_id=tenant_id,
                context_kind="workstream_digest",
                context_id=str(row["id"]),
                refs=evidence_refs or [],
            )
            rebuilt["workstream_digest"] += 1

        from app.personalization.tenant_context import get_tenant_context

        tenant_context = await get_tenant_context(tenant_id=tenant_id)
        if tenant_context and tenant_context.get("id"):
            refs = context_refs_from_entries(
                tenant_context,
                list_fields=("priorities", "active_narratives", "cadences"),
                object_fields=("current_plan",),
            )
            await replace_context_reference_index(
                conn,
                tenant_id=tenant_id,
                context_kind="tenant_context",
                context_id=str(tenant_context["id"]),
                refs=refs,
            )
            rebuilt["tenant_context"] += 1

        # list_person_contexts() is scoped to one owner_user_id (its
        # signature has no tenant-wide "all owners" mode), so a full-tenant
        # rebuild queries person_contexts directly here instead -- this file
        # is not under app/personalization/, so it is not itself subject to
        # the invariant-2 context-authoring allowlist, and this is a
        # maintenance/rebuild operation over already-durable rows, not LLM
        # context assembly.
        from app.personalization.person_context import normalize_person_context

        person_context_rows = await conn.fetch(
            "SELECT id, tenant_id, owner_user_id, subject_entity_id, relationship_kind, "
            "topic_stances, communication_preferences, sentiment_trend, sentiment_history, "
            "evidence_refs, last_confirmed_at, status, updated_by, updated_at "
            "FROM person_contexts WHERE tenant_id = $1",
            tenant_id,
        )
        for raw_row in person_context_rows:
            row = dict(raw_row)
            person_context = normalize_person_context(
                row,
                tenant_id=tenant_id,
                owner_user_id=str(row["owner_user_id"]),
                subject_entity_id=str(row["subject_entity_id"]),
            )
            if not person_context.get("id"):
                continue
            # relationship_kind/sentiment_trend are included in object_fields
            # for parity with approve_person_context_proposal's derivation
            # below, but normalize_person_context flattens both to plain
            # strings (no refs survive that normalization step today), so in
            # practice only the three list_fields contribute rows. Keeping
            # both call sites identical is what matters here -- see the
            # comment on approve_person_context_proposal's own call for the
            # underlying gap.
            refs = context_refs_from_entries(
                person_context,
                list_fields=("topic_stances", "communication_preferences", "sentiment_history"),
                object_fields=("relationship_kind", "sentiment_trend"),
            )
            await replace_context_reference_index(
                conn,
                tenant_id=tenant_id,
                context_kind="person_context",
                context_id=str(person_context["id"]),
                refs=refs,
            )
            rebuilt["person_context"] += 1

        from app.personalization.channel_context import list_channel_contexts

        for channel_context in await list_channel_contexts(tenant_id=tenant_id):
            if not channel_context.get("id"):
                continue
            refs = context_refs_from_entries(
                channel_context,
                list_fields=("dominant_concerns", "tone_norms", "participant_dynamics", "trend_history"),
            )
            await replace_context_reference_index(
                conn,
                tenant_id=tenant_id,
                context_kind="channel_context",
                context_id=str(channel_context["id"]),
                refs=refs,
            )
            rebuilt["channel_context"] += 1

    return rebuilt
