"""
Daily founder brief: broad coverage from the same brain as chat. Catches the
accumulated weak signals no single event justifies alerting on. Lands in
pending_review for operator approval before delivery.
"""
import json
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import structlog

from app.core.db import acquire
from app.core import trace
from app.core.destination_permissions import filter_claim_text_for_destination, source_visible_to_principals
from app.core.tenant_settings import autopublish_read_only_enabled, client_publishing_paused, synthesis_paused
from app.llm.client import call_llm
from app.personalization.tenant_context import maybe_propose_tenant_context_update
from app.personalization.user_context import get_user_context, user_context_pack_item
from app.retrieval.retrieve import (
    build_authorized_context_pack as build_context_pack,
    _open_loop_objects,
    _source_identity_hints,
    load_deep_story_chunks,
)
from app.synthesis.open_loops import open_loop_payloads
from app.synthesis.okr_context import fetch_okr_state, okr_attention_brief_section, okr_attention_items
from app.synthesis.verifier import normalize_citations, verify_block_citations

logger = structlog.get_logger()

BRIEF_PROMPT_VERSION = "v2.2"

# Slice 2.1: two-call structured synthesis replaces the single markdown call.
# 9K tokens (the shared TOKEN_BUDGET) was the binding constraint on grouping
# cross-source evidence into one story; the brief pack alone gets a larger
# budget so convergence has enough context to work with.
BRIEF_PACK_TOKEN_BUDGET = 30_000

SYSTEM_CONVERGENCE = """You group evidence for a startup operator's daily
brief into distinct operating stories. A story is a project, commitment,
blocker, risk, or open question. Evidence about the SAME underlying situation
from different sources (e.g. the same commitment mentioned in Slack and
Gmail) belongs in ONE story, not split across stories. Use ONLY the provided
sources. Every evidence_refs entry must be a literal "kind:id" value copied
from a <source ref="kind:id"> tag. Never invent one.

One source may be tagged <source ref="prior_brief:...">: yesterday's
delivered brief. Use it only to recognize which of today's stories continue
a story from yesterday. If a today's-evidence story is a continuation, include
the prior_brief ref in that story's evidence_refs alongside today's refs.
Never group prior_brief content as its own story, and never treat it as new
evidence."""

PROMPT_CONVERGENCE = """<context>
{context}
</context>

Group the sources above into distinct operating stories for today's brief ({today}).

Return ONLY JSON matching this shape:
{{"stories": [{{"title": "<workstream name>", "evidence_refs": ["kind:id", ...], "notes": "<optional grouping rationale, not shown to the user>"}}]}}

Rules:
- Prefer source-backed names from the context. If a story is unclear, title it "Open Questions".
- evidence_refs must be literal "kind:id" values copied from the <source ref="kind:id"> tags above. Never invent one.
- Every source with brief-relevant content should be referenced by at least one story. If nothing groups, one story per source is fine.
- If a <source ref="prior_brief:..."> is present, do not create a story for it. If today's evidence continues a story it describes, add the prior_brief ref to that story's evidence_refs.
- Do not write prose outside the JSON object."""

SYSTEM = """You write a daily operating brief for a startup operator from
pre-grouped operating stories. She is overloaded and wants to know which
projects, commitments, blockers, risks, and open questions need attention
today. Use ONLY the provided sources. Every ref in your output must be a
literal "kind:id" value copied from a <source ref="kind:id"> tag. Never
invent a ref. Mark inferred claims with "(inferred)" in the block text. Never
invent numbers, dates, owners, or completion status.

Only write a TL;DR when the grouped stories contain 3 or more high/medium
stories. Keep it to 3 sentences max. On quiet days, omit the TL;DR entirely.

Write 1 to 3 moves total. Deduplicate repeated next actions across stories.
Each move must name an action, what it creates, and a deadline. If there is
nothing actionable, omit moves entirely.

If a <source ref="prior_brief:..."> is present, it is yesterday's delivered
brief. Compare each of today's stories against it: if a story is the same
unresolved item as yesterday, say so explicitly ("Carried over from
yesterday: ...") citing the prior_brief ref; if something yesterday's brief
raised is now resolved, say so and close it out; if a story has no material
update since yesterday, mark it "unchanged" and write what_changed's text as
EXACTLY "No change since yesterday." (nothing else), citing the prior_brief
ref. Only mark a story unchanged if it is genuinely the same story
yesterday's brief covered with no new evidence today; if there is anything
new or specific to say, write it normally and do not mark the story
unchanged. Lead with what changed: order stories with material updates
before unchanged ones.

The sources may include deadlines, commitments, or calendar events that are
still in the future (a due date within the next 7 days, or a meeting within
the next 48 hours) rather than recent activity. When one of those appears
with no visible preparation elsewhere in the sources, write a coming_up
entry naming the date, what is due, and what prep is missing, citing the
source. Do not write a coming_up entry for a recurring standing meeting with
no other signal, or when the deadline already has real prep visible in a
story (a plain mention there is enough; do not duplicate it here)."""

PROMPT = """<context>
{context}
</context>

<grouped_stories>
{grouped_stories}
</grouped_stories>

Write today's operating brief ({today}) as JSON. Write exactly one entry per
grouped story above (do not add or drop stories).

Return ONLY JSON matching this shape:
{{
  "tldr": {{"text": "optional 1-3 sentence summary for busy days only", "refs": ["kind:id", ...]}},
  "moves": [
    {{
      "action": "one next action from a story next_action block",
      "creates": "what this creates or changes",
      "deadline": "deadline or timebox",
      "refs": ["kind:id", ...]
    }}
  ],
  "stories": [
    {{
      "title": "<workstream name from the grouped story>",
      "leverage": "high|medium|low",
      "status": "new|continuing|resolved|unchanged (optional: omit entirely if there is no prior_brief source, i.e. this tenant's first brief)",
      "focus_today": {{"text": "one specific outcome or decision to pay attention to today", "refs": ["kind:id", ...]}},
      "what_changed": {{"text": "material source-backed update: names, dates, commitments, why it matters now", "refs": ["kind:id", ...]}},
      "blocked_or_at_risk": {{"text": "deadline, customer commitment, owner gap, stale work, or 'No risk evidence found in the provided sources.'", "refs": ["kind:id", ...]}},
      "needs_decision": {{"text": "decision or open question, or 'No decision evidence found in the provided sources.'", "refs": ["kind:id", ...]}},
      "next_action": {{"text": "specific next step for a named human, including what artifact/status/customer message should change, or 'No action recommended.'", "refs": ["kind:id", ...]}},
      "implication": {{"text": "optional: what assumption this challenges, cost of ignoring it another day", "refs": ["kind:id", ...]}},
      "who_needs_to_know": {{"text": "optional: who should be looped in on this", "refs": ["kind:id", ...]}}
    }}
  ],
  "coverage": {{"text": "which sources fed this brief and any gaps (sources stale or missing)", "refs": ["kind:id", ...]}},
  "coming_up": {{"text": "optional: a near-term deadline or calendar event with no visible prep, naming the date and what is missing", "refs": ["kind:id", ...]}}
}}

Only write tldr when there are 3 or more high/medium stories. Omit the key
entirely on quiet days or when the source health gate has replaced it.

Write 1 to 3 moves total. Deduplicate repeated actions across stories. Each
move must carry a deadline and should come from a story next_action.

leverage ranks this story's priority for today: "high" if someone is blocked
waiting on it, it unblocks other work, or delay compounds; "medium" for real
but non-urgent attention; "low" for FYI, no action needed.

implication, who_needs_to_know, and coming_up are optional; omit the key
entirely (not null, not empty string) when there is nothing to say.

Rules: every ref must be a literal "kind:id" value copied from <source
ref="kind:id"> tags. No claim without a ref in its block's refs list. Do not
write a generic status report; preserve who, what changed, expected plan,
contradiction, timing, and missing evidence. If evidence is missing, say what
cannot be verified in the relevant block's text. Do not write prose outside
the JSON object."""

# Slice 2.4: independent third call over the same pack (not call 1's grouped
# stories) -- which stated plans have no matching activity, and which
# activities match no plan. No new retrieval: the pack already holds OKR
# state, open-risk objects, and fresh chunks; slice 4.1's contradiction facts
# (fact_kind='contradiction') show up as normal fact sources and are strong
# evidence when present.
SYSTEM_PLAN_ACTUAL = """You compare stated plans against observed activity
for a startup operator's daily brief. Use ONLY the provided sources. Find
two kinds of mismatch: a stated plan, commitment, or deadline with no
matching activity evidence ("plan_no_activity"); and activity with no stated
plan or commitment behind it ("activity_no_plan"). A recurring or routine
event with no explicit plan attached is not a mismatch. If a source is a
structured contradiction fact (an existing claim on an object overwritten by
a conflicting one), treat it as strong evidence of a mismatch. Every ref in
your output must be a literal "kind:id" value copied from a <source
ref="kind:id"> tag. Never invent one. If the sources are fully consistent,
return an empty list. Do not manufacture a mismatch to fill space.

Use these monitor labels for each mismatch:
- commitments: explicit promises, follow-ups, or owner/deadline commitments with no matching follow-through
- deadlines: upcoming deadlines or prep windows with no visible preparation
- strategy: stated priorities or OKR mix versus actual activity mix. Use this
  for a priority list even when the priority names a work item; do not
  collapse priority-vs-activity into commitments unless the source is clearly
  an explicit promise or owner/deadline commitment.
- teammates: teammate commitments versus follow-through
- roadmap: roadmap/build work, including Linear or GitHub when connected
- cross_monitor: a contradiction that spans two or more monitors in the same
  mismatch; rank these ahead of any single-monitor mismatch. Do not use
  cross_monitor just because multiple mismatches exist in the same pack."""

PROMPT_PLAN_ACTUAL = """<context>
{context}
</context>

Compare stated plans against observed activity as of {today}.

Use these monitor families and source pairings:
- commitments: open loops, post-4.2, asks and commitments waiting on activity
- deadlines: upcoming deadlines and prep gaps from the forward-looking pack
- strategy: stated priorities and OKR refs versus the observed activity mix
- teammates: teammate commitments versus follow-through
- roadmap: roadmap items versus Linear / GitHub build activity when connected

Return ONLY JSON matching this shape:
{{"mismatches": [{{"type": "plan_no_activity|activity_no_plan", "monitor": "commitments|deadlines|strategy|teammates|roadmap|cross_monitor", "text": "<one sentence: what plan or activity, what's missing, why it matters>", "refs": ["kind:id", ...]}}]}}

Rules:
- Only include a mismatch when the sources actually show a stated
  plan/commitment/deadline with no matching activity, or activity with no
  stated plan behind it.
- Use "monitor" to name the mismatch class. Pick "cross_monitor" when the
  mismatch spans two or more monitors. Put any cross-monitor mismatch first.
- Prefer "strategy" for priority-vs-activity cases, and "commitments" only for
  explicit promises, follow-ups, or owner/deadline commitments.
- Every ref must be a literal "kind:id" value copied from <source
  ref="kind:id"> tags. No mismatch without at least one ref.
- Set monitor to the source pair family that best explains the mismatch:
  commitments, deadlines, strategy, teammates, or roadmap. If the same item
  is supported by more than one monitor, choose the strongest one and rank
  that mismatch above single-monitor misses when ordering the list.
- If the pack is fully consistent, return {{"mismatches": []}}. Do not write
  filler. Do not write prose outside the JSON object."""


async def run_daily_brief(ctx: dict, *, tenant_id: str, actor_user_id: str | None = None) -> str | None:
    from app.core.db import _current_tenant_id
    _current_tenant_id.set(tenant_id)

    async with acquire() as conn:
        tenant_settings_raw = await conn.fetchval("SELECT settings FROM tenants WHERE id = $1", tenant_id)
    tenant_settings = json.loads(tenant_settings_raw) if isinstance(tenant_settings_raw, str) else dict(tenant_settings_raw or {})
    brief_date = _tenant_today(tenant_settings)
    if synthesis_paused(tenant_settings):
        run_id = await trace.start_run(tenant_id=tenant_id, kind="daily")
        async with trace.step(run_id, tenant_id, "synthesis_paused") as st:
            st.status = "skipped"
            st.detail = {"reason": "tenant pause_synthesis is enabled"}
        await trace.finish_run(run_id, status="paused")
        return None

    retry_brief_id = None
    async with acquire() as conn:
        existing = await conn.fetchrow(
            "SELECT id, status FROM briefs WHERE tenant_id = $1 AND brief_date = $2",
            tenant_id, brief_date,
        )
        if existing and existing["status"] not in {"rejected", "unpublished"}:
            logger.info("brief_already_exists", tenant_id=tenant_id, brief_date=str(brief_date))
            return str(existing["id"])
        if existing:
            retry_brief_id = str(existing["id"])
    run_id = await trace.start_run(tenant_id=tenant_id, kind="daily")

    try:
        async with trace.step(run_id, tenant_id, "context_retrieved") as st:
            breadth_pack = await _build_company_context_pack(
                tenant_id=tenant_id, purpose="brief_convergence", token_budget=BRIEF_PACK_TOKEN_BUDGET,
            )
            source_items = await _source_freshness_items(tenant_id, _company_principals(tenant_id))
            source_health_items = await _stale_source_health_items(tenant_id, _company_principals(tenant_id))
            try:
                open_loop_items = await _open_loop_objects(tenant_id, allowed_principals=_company_principals(tenant_id))
            except Exception as e:
                logger.info("open_loop_retrieval_failed", tenant_id=tenant_id, error=f"{type(e).__name__}: {e}")
                open_loop_items = []
            okr_state = await fetch_okr_state(tenant_id, include_private_contributions=False)
            okr_items = okr_attention_items(okr_state)
            user_context_item = None
            if actor_user_id:
                user_context = await get_user_context(tenant_id=tenant_id, user_id=actor_user_id)
                user_context_item = user_context_pack_item(user_context)
            prior_brief_item = await _prior_brief_pack_item(
                tenant_id, brief_date, _company_principals(tenant_id),
            )
            extra_items = [*source_items, *open_loop_items, *okr_items]
            if prior_brief_item:
                extra_items.append(prior_brief_item)
            if user_context_item:
                extra_items.append(user_context_item)
            extra_items.extend(source_health_items)
            if extra_items:
                breadth_pack = await _augment_context_pack(tenant_id, breadth_pack, extra_items)
            user_context_included = any(item.get("kind") == "user_context" for item in breadth_pack["items"])
            st.output_ref = breadth_pack["context_pack_id"]
            st.detail = {
                "breadth_items": len(breadth_pack["items"]),
                "open_loop_items": len(open_loop_items),
                "source_health_items": len(source_health_items),
                "okr_objects": len(okr_state),
                "okr_attention_items": len(okr_items),
                "user_context_included": user_context_included,
                "prior_brief_included": prior_brief_item is not None,
            }
        if not breadth_pack["items"]:
            logger.info("brief_skipped_no_activity", tenant_id=tenant_id)
            await trace.finish_run(run_id, status="suppressed")
            return None

        feedback_line = await _feedback_rules_line(tenant_id)

        # OKR attention is a prepended section on the full synthesized brief
        # (see _prepend_okr_attention in the verify step below), never a
        # standalone deterministic report. A tenant with OKR state gets the
        # same synthesized brief as anyone else; healthy OKRs simply prepend
        # nothing.
        async with trace.step(run_id, tenant_id, "synthesized", input_ref=breadth_pack["context_pack_id"]) as st:
            breadth_context_text = (
                breadth_pack["text"]
                + f"\n\n<feedback_rules>\n{feedback_line}\n</feedback_rules>"
            )
            convergence_result = await call_llm(
                task="brief_convergence",
                system=SYSTEM_CONVERGENCE,
                prompt=PROMPT_CONVERGENCE.format(context=breadth_context_text, today=brief_date.isoformat()),
                tenant_id=tenant_id,
                tenant_settings=tenant_settings,
                max_tokens=1500,
                expect_json=True,
                prompt_version=BRIEF_PROMPT_VERSION,
                context_pack_id=breadth_pack["context_pack_id"],
                pipeline_run_id=run_id,
                validation_pack_items=breadth_pack["items"],
            )
            # `or {"stories": []}` only matters in fixture mode (no validator runs
            # there); the routed/production path never returns here with parsed=None
            # since validate_brief_convergence already rejected anything malformed.
            grouped_stories = convergence_result["parsed"] or {"stories": []}
            selected_object_ids = await _selected_story_object_ids(
                tenant_id=tenant_id,
                grouped_stories=grouped_stories,
            )
            deep_pack = await _build_company_context_pack(
                tenant_id=tenant_id,
                purpose="brief_write",
                object_ids=selected_object_ids,
                token_budget=BRIEF_PACK_TOKEN_BUDGET,
            )
            deep_extra_items = list(extra_items)
            deep_chunk_items = await load_deep_story_chunks(
                tenant_id,
                selected_object_ids,
                recent_hours=26,
                allowed_principals=_company_principals(tenant_id),
            )
            if deep_chunk_items:
                deep_extra_items.extend(deep_chunk_items)
            if deep_extra_items:
                deep_pack = await _augment_context_pack(tenant_id, deep_pack, deep_extra_items)
            context_text = (
                deep_pack["text"]
                + f"\n\n<feedback_rules>\n{feedback_line}\n</feedback_rules>"
            )
            write_result = await call_llm(
                task="brief",
                system=SYSTEM,
                prompt=PROMPT.format(
                    context=context_text,
                    grouped_stories=json.dumps(grouped_stories, ensure_ascii=False),
                    today=brief_date.isoformat(),
                ),
                tenant_id=tenant_id,
                tenant_settings=tenant_settings,
                max_tokens=3000,
                expect_json=True,
                prompt_version=BRIEF_PROMPT_VERSION,
                context_pack_id=deep_pack["context_pack_id"],
                pipeline_run_id=run_id,
                validation_pack_items=deep_pack["items"],
            )
            # Slice 2.4: independent third call over context_text (not
            # grouped_stories) -- plan-vs-actual doesn't need story grouping,
            # it compares the raw pack against itself.
            plan_actual_result = await call_llm(
                task="plan_actual",
                system=SYSTEM_PLAN_ACTUAL,
                prompt=PROMPT_PLAN_ACTUAL.format(context=context_text, today=brief_date.isoformat()),
                tenant_id=tenant_id,
                tenant_settings=tenant_settings,
                max_tokens=1500,
                expect_json=True,
                prompt_version=BRIEF_PROMPT_VERSION,
                context_pack_id=deep_pack["context_pack_id"],
                pipeline_run_id=run_id,
                validation_pack_items=deep_pack["items"],
            )
            # Same as above: unreachable with parsed=None outside fixture mode.
            brief_parsed = write_result["parsed"] or {"stories": []}
            brief_parsed["moves"] = _dedupe_moves(brief_parsed.get("moves"))
            if not _brief_has_enough_high_severity_stories(brief_parsed):
                brief_parsed["tldr"] = None
            if source_health_items:
                stale_warning = _stale_source_warning(source_health_items)
                if stale_warning:
                    brief_parsed["tldr"] = stale_warning
            brief_parsed["plan_actual"] = plan_actual_result["parsed"] or {"mismatches": []}
            result = {
                "text": _render_brief_markdown(brief_parsed, deep_pack["items"]),
                "model_call_id": write_result["model_call_id"],
            }
            st.output_ref = result["model_call_id"]
            st.detail = {
                "breadth_context_pack_id": breadth_pack["context_pack_id"],
                "deep_context_pack_id": deep_pack["context_pack_id"],
                "convergence_model_call_id": convergence_result["model_call_id"],
                "plan_actual_model_call_id": plan_actual_result["model_call_id"],
                "story_count": len(grouped_stories.get("stories") or []),
                "selected_object_count": len(selected_object_ids),
                "deep_chunk_count": len(deep_chunk_items),
                "plan_actual_mismatch_count": len(brief_parsed["plan_actual"].get("mismatches") or []),
            }

        async with trace.step(run_id, tenant_id, "verified", input_ref=result["model_call_id"]) as st:
            content, omitted_lines = _drop_malformed_citation_lines(
                normalize_citations(
                    _prepend_okr_attention(result["text"].strip(), okr_items),
                    deep_pack["items"],
                )
            )
            citation_check = _brief_citation_check(content, deep_pack["items"])
            if citation_check["valid"]:
                content, citation_check = _apply_destination_claim_gate(
                    content,
                    citation_check,
                    deep_pack["items"],
                    _company_principals(tenant_id),
                )
            if omitted_lines:
                citation_check = {
                    **citation_check,
                    "omitted_malformed_citation_lines": omitted_lines,
                }
            st.detail = citation_check
        unsupported_claims = citation_check.get("unsupported_claims", [])
        autopublish = (
            citation_check["valid"]
            and not unsupported_claims
            and _autopublish_read_only(tenant_settings)
            and not client_publishing_paused(tenant_settings)
        )
        if citation_check.get("invalid_refs") or citation_check.get("uncited_claims"):
            status = "rejected"
        elif autopublish:
            status = "publishing"
        else:
            status = "pending_review"

        async with trace.step(run_id, tenant_id, "output_created", input_ref=result["model_call_id"]) as st:
            brief_id = retry_brief_id or str(uuid.uuid4())
            async with acquire() as conn:
                await conn.execute(
                    """
                    WITH prior AS (
                        SELECT tenant_id, id AS brief_id, brief_date, content_md, sections,
                               status, context_pack_id, model_call_id
                        FROM briefs
                        WHERE tenant_id = $2 AND brief_date = $3 AND status IN ('rejected', 'unpublished')
                    ), archived AS (
                        INSERT INTO brief_revisions
                            (tenant_id, brief_id, brief_date, content_md, sections,
                             status, context_pack_id, model_call_id)
                        SELECT tenant_id, brief_id, brief_date, content_md, sections,
                               status, context_pack_id, model_call_id
                        FROM prior
                        RETURNING id
                    )
                    INSERT INTO briefs
                        (id, tenant_id, brief_date, content_md, sections, context_pack_id,
                         model_call_id, status, pipeline_run_id, delivered_via,
                         visibility, acl_basis, acl_confidence, allowed_principals,
                         acl_state, acl_freshness_state)
                    VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $8, $9, $10::jsonb,
                            'company', 'derived_context_pack', 'medium', $11::jsonb,
                            'stamped', 'fresh')
                    ON CONFLICT (tenant_id, brief_date) DO UPDATE
                    SET content_md = EXCLUDED.content_md,
                        sections = EXCLUDED.sections,
                        context_pack_id = EXCLUDED.context_pack_id,
                        model_call_id = EXCLUDED.model_call_id,
                        status = EXCLUDED.status,
                        pipeline_run_id = EXCLUDED.pipeline_run_id,
                        delivered_via = EXCLUDED.delivered_via,
                        visibility = EXCLUDED.visibility,
                        acl_basis = EXCLUDED.acl_basis,
                        acl_confidence = EXCLUDED.acl_confidence,
                        allowed_principals = EXCLUDED.allowed_principals,
                        acl_state = EXCLUDED.acl_state,
                        acl_freshness_state = EXCLUDED.acl_freshness_state,
                        reviewed_by = NULL,
                        reviewed_at = NULL,
                        created_at = now()
                    WHERE briefs.status IN ('rejected', 'unpublished')
                    """,
                    brief_id, tenant_id, brief_date, content,
                    json.dumps({
                        "citation_check": citation_check,
                        "brief": brief_parsed,
                        "grouped_stories": grouped_stories,
                    }),
                    deep_pack["context_pack_id"], result["model_call_id"], status, run_id,
                    json.dumps([]), json.dumps(_company_principals(tenant_id)),
                )
                if not citation_check["valid"]:
                    await conn.execute(
                        """
                        INSERT INTO system_issues
                            (tenant_id, kind, detail, pipeline_run_id)
                        VALUES ($1, 'unsupported_claim', $2::jsonb, $3)
                        """,
                        tenant_id,
                        json.dumps({
                            "brief_id": brief_id,
                            "invalid_refs": citation_check["invalid_refs"],
                            "unsupported_claims": citation_check.get("unsupported_claims", []),
                        }),
                        run_id,
                    )
            st.output_ref = brief_id
            st.detail = {"status": status}

        if citation_check["valid"]:
            async with trace.step(run_id, tenant_id, "tenant_context_update_attempted", input_ref=brief_id) as st:
                try:
                    proposal = await maybe_propose_tenant_context_update(
                        tenant_id=tenant_id,
                        context_pack=deep_pack,
                        pipeline_run_id=run_id,
                    )
                except Exception as e:
                    st.status = "failed"
                    st.detail = {"error": f"{type(e).__name__}: {e}"}
                else:
                    st.detail = {"proposed": proposal is not None}

        if autopublish:
            receivers = await _publish_autopublished_brief(tenant_id=tenant_id, brief_id=brief_id)
            async with acquire() as conn:
                await conn.execute(
                    """
                    UPDATE briefs
                    SET status = 'delivered',
                        delivered_via = delivered_via || $3::jsonb
                    WHERE id = $1 AND tenant_id = $2 AND status = 'publishing'
                    """,
                    brief_id, tenant_id, json.dumps(["app_auto"]),
                )
            if run_id:
                async with trace.step(run_id, tenant_id, "app_delivery", input_ref=brief_id) as st:
                    st.detail = {
                        "channel": f"deliveries:{tenant_id}",
                        "autopublished": True,
                        "redis_receivers": receivers,
                        "pull_api_visible": True,
                    }
                await trace.finish_run(run_id, status="delivered")
        elif status == "pending_review":
            await trace.set_run_status(run_id, "pending_review")
        else:
            await trace.finish_run(run_id, status="rejected")

        logger.info(
            "brief_created",
            tenant_id=tenant_id,
            brief_id=brief_id,
            citations_valid=citation_check["valid"],
            pipeline_run_id=run_id,
        )
        return brief_id
    except Exception as e:
        await trace.finish_run(run_id, status="failed", error=f"{type(e).__name__}: {e}")
        raise


def _tenant_today(tenant_settings: dict):
    tz_name = tenant_settings.get("brief_timezone") or tenant_settings.get("timezone") or "America/Los_Angeles"
    try:
        tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        tz = ZoneInfo("UTC")
    return datetime.now(tz).date()


# Leverage rank the model assigns per story (validated to one of "high",
# "medium", "low" by validate_brief_structured). Proxies slice 1.2's ranking
# score function, which does not exist yet; swap this ordering key for that
# score once 1.2 lands, per the build plan's shared-score-inputs intent.
_LEVERAGE_ORDER = {"high": 0, "medium": 1, "low": 2}

# Slice 2.2: stories the model marks "unchanged" since yesterday's brief
# render as one collapsed line and sort after every story with a material
# update, so the brief leads with what changed.
_UNCHANGED_STATUS = "unchanged"

_STORY_LINE_LABELS = (
    ("focus_today", "Focus today"),
    ("what_changed", "What changed"),
    ("blocked_or_at_risk", "Blocked or at risk"),
    ("needs_decision", "Needs decision"),
    ("next_action", "Next action"),
    ("implication", "Implication"),
    ("who_needs_to_know", "Who needs to know"),
)

_PLAN_ACTUAL_MONITORS = {"commitments", "deadlines", "strategy", "teammates", "roadmap"}


def _loop_section_blocks(pack_items: list[dict] | None) -> list[str]:
    payloads = open_loop_payloads(pack_items or [])
    if not payloads:
        return []

    blocks: list[str] = []
    kill_lines = ["## Kill list"]
    for payload in payloads:
        ref = payload["source_ref"]
        kill_lines.append(f"- {payload['line']} [ref:{ref}]")
    blocks.append("\n".join(kill_lines))

    delegated = [payload for payload in payloads if (payload.get("days_open") or 0) >= 3]
    if delegated:
        lines = ["## Drop or delegate"]
        for payload in delegated:
            owner = f" owner={payload['owner']}" if payload.get("owner") else ""
            ref = payload["source_ref"]
            lines.append(
                f"- {payload['title']}{owner} has been open {int(payload.get('days_open') or 0)} days and should be delegated or dropped. [ref:{ref}]"
            )
        blocks.append("\n".join(lines))

    old_count = len(delegated)
    if old_count >= 3 or len(payloads) > 5:
        refs = " ".join(f"[ref:{payload['source_ref']}]" for payload in delegated[:3]) or ""
        blocks.append(
            "## Energy Audit\n"
            f"- {old_count} open loops have carried 3+ days and {len(payloads)} loops are active overall. {refs}".strip()
        )

    return blocks


_TLDR_SECTION_TITLE = "TL;DR"
_MOVES_SECTION_TITLE = "Moves"


def _render_brief_markdown(parsed: dict, pack_items: list[dict] | None = None) -> str:
    """Deterministic renderer for slice 2.1's structured brief JSON. Produces
    the SAME markdown shape the single-call prompt used to write directly, so
    every downstream step (OKR prepend, citation normalization, block-citation
    verification, presentation parsing) stays unchanged."""
    stories = [s for s in (parsed.get("stories") or []) if isinstance(s, dict)]
    blocks = []
    tldr = parsed.get("tldr")
    if isinstance(tldr, dict) and str(tldr.get("text") or "").strip():
        blocks.append(f"## {_TLDR_SECTION_TITLE}\n{_render_block_line(None, tldr, bare=True)}")
    moves = [m for m in (parsed.get("moves") or []) if isinstance(m, dict)]
    if moves:
        move_lines = [f"## {_MOVES_SECTION_TITLE}"]
        move_lines.extend(_render_move_line(move) for move in moves)
        blocks.append("\n".join(move_lines))
    ordered = sorted(
        enumerate(stories),
        key=lambda pair: (
            1 if pair[1].get("status") == _UNCHANGED_STATUS else 0,
            _LEVERAGE_ORDER.get(pair[1].get("leverage"), 1),
            pair[0],
        ),
    )
    for _, story in ordered:
        title = str(story.get("title") or "Open Questions").strip() or "Open Questions"
        if story.get("status") == _UNCHANGED_STATUS:
            lines = [f"## {title}", _render_block_line("What changed", story.get("what_changed"))]
            blocks.append("\n".join(lines))
            continue
        lines = [f"## {title}"]
        for field_name, label in _STORY_LINE_LABELS:
            block = story.get(field_name)
            if block is None and field_name in ("implication", "who_needs_to_know"):
                continue
            lines.append(_render_block_line(label, block))
        blocks.append("\n".join(lines))
    stale_work_items = _stale_work_blocks(pack_items or [])
    if stale_work_items:
        lines = ["## What Didn't Move"]
        for item in stale_work_items:
            lines.append(_render_block_line(None, item, bare=True))
        blocks.append("\n".join(lines))
    room_shift_items = _room_shift_blocks(pack_items or [])
    if room_shift_items:
        lines = ["## Room Shifts"]
        for item in room_shift_items:
            lines.append(_render_block_line(None, item, bare=True))
        blocks.append("\n".join(lines))
    coming_up = parsed.get("coming_up")
    if isinstance(coming_up, dict) and str(coming_up.get("text") or "").strip():
        blocks.append(f"## Coming Up\n{_render_block_line(None, coming_up, bare=True)}")
    mismatches = [
        m for m in ((parsed.get("plan_actual") or {}).get("mismatches") or []) if isinstance(m, dict)
    ]
    if mismatches:
        lines = ["## Plan vs Actual"]
        ordered_mismatches = sorted(
            enumerate(mismatches),
            key=lambda item: (0 if str(item[1].get("monitor") or "") == "cross_monitor" else 1, item[0]),
        )
        for _, mismatch in ordered_mismatches:
            label = _plan_actual_monitor_label(mismatch)
            lines.append(_render_block_line(label, mismatch))
        blocks.append("\n".join(lines))
    blocks.extend(_loop_section_blocks(pack_items or []))
    coverage = parsed.get("coverage") or {}
    blocks.append(f"## Coverage\n{_render_block_line(None, coverage, bare=True)}")
    return "\n\n".join(blocks).strip()


def _render_move_line(move: dict) -> str:
    action = str(move.get("action") or "").strip()
    creates = str(move.get("creates") or "").strip()
    deadline = str(move.get("deadline") or "").strip()
    refs = move.get("refs") or []
    ref_str = "".join(f" [ref:{ref}]" for ref in refs if isinstance(ref, str) and ref.strip())
    body = " | ".join((
        f"Action: {action}" if action else "Action: Missing action",
        f"Creates: {creates}" if creates else "Creates: Missing outcome",
        f"Deadline: {deadline}" if deadline else "Deadline: Missing deadline",
    ))
    return f"- {body}{ref_str}"


def _render_block_line(label: str | None, block: dict | None, *, bare: bool = False) -> str:
    block = block if isinstance(block, dict) else {}
    text = str(block.get("text") or "").strip()
    refs = block.get("refs") or []
    ref_str = "".join(f" [ref:{ref}]" for ref in refs if isinstance(ref, str) and ref.strip())
    body = f"{text}{ref_str}".strip()
    if bare:
        return f"- {body}" if body else "- Missing: nothing captured."
    return f"- {label}: {body}"


_PLAN_ACTUAL_MONITOR_LABELS = {
    "commitments": "Commitments",
    "deadlines": "Deadlines",
    "strategy": "Strategy",
    "teammates": "Teammates",
    "roadmap": "Roadmap",
    "cross_monitor": "Cross-monitor",
}


def _plan_actual_monitor_label(mismatch: dict) -> str:
    monitor = str(mismatch.get("monitor") or "").strip()
    return _PLAN_ACTUAL_MONITOR_LABELS.get(monitor, monitor.replace("_", " ").title() if monitor else "Plan")


def _stale_work_blocks(pack_items: list[dict]) -> list[dict]:
    blocks: list[dict] = []
    for item in pack_items:
        if not isinstance(item, dict):
            continue
        if item.get("signal_type") != "stale_work":
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        ref = item.get("kind") and item.get("id")
        block: dict[str, object] = {"text": text}
        if ref:
            block["refs"] = [f"{item['kind']}:{item['id']}"]
        blocks.append(block)
    return blocks


def _room_shift_blocks(pack_items: list[dict]) -> list[dict]:
    blocks: list[dict] = []
    for item in pack_items:
        if not isinstance(item, dict) or item.get("kind") != "channel_context":
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        block: dict[str, object] = {"text": text}
        if item.get("kind") and item.get("id"):
            block["refs"] = [f"{item['kind']}:{item['id']}"]
        blocks.append(block)
    return blocks


def _brief_citation_check(content: str, items: list[dict]) -> dict:
    citation_check = verify_block_citations(
        content,
        items,
        require_relevance=True,
    )
    uncited_claims = [
        claim for claim in citation_check.get("uncited_claims", [])
        if _requires_brief_citation(claim)
    ]
    return {
        **citation_check,
        "uncited_claims": uncited_claims,
        # Briefs contain recommended actions and synthesized focus lines, so
        # relevance warnings are review signals. Hallucinated refs and uncited
        # substantive claims still fail closed.
        "valid": not citation_check.get("invalid_refs") and not uncited_claims,
    }


def _brief_has_enough_high_severity_stories(parsed: dict) -> bool:
    stories = [s for s in (parsed.get("stories") or []) if isinstance(s, dict)]
    return sum(1 for story in stories if story.get("leverage") in {"high", "medium"}) >= 3


def _dedupe_moves(moves: object) -> list[dict]:
    if not isinstance(moves, list):
        return []
    deduped: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for move in moves:
        if not isinstance(move, dict):
            continue
        action = str(move.get("action") or "").strip()
        creates = str(move.get("creates") or "").strip()
        deadline = str(move.get("deadline") or "").strip()
        if not action or not creates or not deadline:
            continue
        key = (action.lower(), creates.lower(), deadline.lower())
        if key in seen:
            continue
        refs = [ref for ref in (move.get("refs") or []) if isinstance(ref, str) and ref.strip()]
        deduped.append({
            "action": action,
            "creates": creates,
            "deadline": deadline,
            "refs": list(dict.fromkeys(refs)),
        })
        seen.add(key)
        if len(deduped) == 3:
            break
    return deduped


def _normalize_source_health_row(row: dict, principals: list[str]) -> dict | None:
    sync_policy = row.get("sync_policy") or {}
    if isinstance(sync_policy, str):
        try:
            sync_policy = json.loads(sync_policy)
        except json.JSONDecodeError:
            sync_policy = {}
    freshest = row.get("freshest")
    freshness_hours = int(sync_policy.get("freshness_hours", 24) or 24)
    now = datetime.now(timezone.utc)
    is_stale = freshest is None or now - freshest > timedelta(hours=freshness_hours)
    if not is_stale and row.get("status") == "live" and not row.get("stale_since") and not row.get("degraded_reason"):
        return None
    source_type = str(row.get("source_type") or "source")
    freshest_text = f"{freshest:%Y-%m-%d %H:%M}" if freshest else "never"
    stale_since = row.get("stale_since")
    stale_text = f", stale since {stale_since:%Y-%m-%d %H:%M}" if stale_since else ""
    reason = ", source details require source access" if row.get("degraded_reason") else ""
    return {
        "kind": "source",
        "id": str(row["id"]),
        "text": f"Source freshness: {source_type} is {row['status']}, last data {freshest_text}{stale_text}{reason}.",
        "source_id": str(row["id"]),
        "source_type": source_type,
        "source_external_id": None,
        "source_identity_hints": [],
        "source_url": None,
        "priority": 2,
        "score": 1.0,
        "visibility": "company",
        "allowed_principals": principals,
        "acl_state": "stamped",
        "acl_freshness_state": "fresh",
    }


async def _stale_source_health_items(tenant_id: str, destination_principals: list[str] | None = None) -> list[dict]:
    principals = destination_principals or _company_principals(tenant_id)
    async with acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT s.id, s.source_type, s.status, s.stale_since, s.degraded_reason, s.sync_policy,
                   GREATEST(s.last_webhook_at, s.last_synced_at) AS freshest
            FROM sources s
            WHERE s.tenant_id = $1
              AND EXISTS (
                  SELECT 1
                  FROM source_resources sr
                  WHERE sr.tenant_id = s.tenant_id
                    AND sr.source_id = s.id
                    AND sr.acl_state = 'stamped'
              )
            ORDER BY s.source_type
            """,
            tenant_id,
        )
    items: list[dict] = []
    for row in rows:
        item = _normalize_source_health_row(dict(row), principals)
        if item and source_visible_to_principals(item, principals):
            items.append(item)
    return items


def _stale_source_warning(source_health_items: list[dict]) -> dict | None:
    if not source_health_items:
        return None
    first = source_health_items[0]
    text = str(first.get("text") or "").strip()
    if not text:
        return None
    return {
        "text": f"Stale-data warning: {text}",
        "refs": [f"source:{first['id']}"],
    }


def _apply_destination_claim_gate(
    content: str,
    citation_check: dict,
    items: list[dict],
    destination_principals: list[str],
) -> tuple[str, dict]:
    claim_gate = filter_claim_text_for_destination(
        content,
        _sources_from_pack_items(items),
        destination_principals,
    )
    if not claim_gate["blocked_claims"]:
        return content, citation_check
    if claim_gate["text"] and "[ref:" in claim_gate["text"]:
        content = claim_gate["text"]
        citation_check = _brief_citation_check(content, items)
    else:
        content = "## Coverage\n- Missing: destination-visible evidence for this brief."
        citation_check = {
            **_brief_citation_check(content, items),
            "valid": False,
        }
    return content, {
        **citation_check,
        "blocked_permission_claims": claim_gate["blocked_claims"],
    }


def _sources_from_pack_items(items: list[dict]) -> list[dict]:
    sources: list[dict] = []
    for item in items:
        kind = item.get("kind")
        item_id = item.get("id")
        if not kind or not item_id:
            continue
        source = {
            "ref": f"{kind}:{item_id}",
            "source_url": item.get("source_url"),
            "source_title": item.get("source_title") or item.get("title"),
            "source_type": item.get("source_type"),
            "source_id": item.get("source_id"),
            "source_external_id": item.get("source_external_id"),
            "source_identity_hints": item.get("source_identity_hints") or [],
            "source_observed_at": item.get("source_observed_at"),
        }
        if any(key in item for key in ("visibility", "allowed_principals", "acl_state", "acl_freshness_state")):
            source.update({
                "visibility": item.get("visibility"),
                "allowed_principals": item.get("allowed_principals") or [],
                "acl_state": item.get("acl_state"),
                "acl_freshness_state": item.get("acl_freshness_state"),
            })
        sources.append(source)
    return sources


def _json_array(value) -> list:
    if value is None:
        return []
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return list(value) if isinstance(value, (list, tuple, set)) else []


def _requires_brief_citation(claim: str) -> bool:
    stripped = (claim or "").strip()
    if not stripped:
        return False
    if not stripped.strip("-—_*"):
        return False
    return True


def _drop_malformed_citation_lines(content: str) -> tuple[str, list[str]]:
    kept = []
    omitted = []
    for line in content.splitlines():
        if _has_malformed_ref(line):
            omitted.append(line)
            continue
        kept.append(line)
    return "\n".join(kept).strip(), omitted


def _prepend_okr_attention(content: str, okr_items: list[dict]) -> str:
    okr_section = okr_attention_brief_section(okr_items)
    if not okr_section:
        return content
    if "## OKRs needing attention" in content:
        return content
    return f"{okr_section}\n\n{content}".strip()


def _okr_only_brief(okr_items: list[dict]) -> str:
    section = okr_attention_brief_section(okr_items)
    if not section:
        return ""
    first = okr_items[0]
    ref = f"[ref:{first['kind']}:{first['id']}]"
    return f"{section}\n\n## Coverage\n- Focus today: OKR brief limited to OKR items needing attention. {ref}"


def _has_malformed_ref(line: str) -> bool:
    marker = "[ref:"
    idx = line.find(marker)
    while idx != -1:
        end = line.find("]", idx)
        if end == -1:
            return True
        idx = line.find(marker, end + 1)
    return False


def _autopublish_read_only(tenant_settings: dict) -> bool:
    return autopublish_read_only_enabled(tenant_settings)


def _company_principals(tenant_id: str) -> list[str]:
    return [f"company:{tenant_id}"]


async def _build_company_context_pack(*, tenant_id: str, purpose: str, **kwargs) -> dict:
    return await build_context_pack(
        tenant_id=tenant_id,
        actor_user_id=None,
        requester_user_id=None,
        target_audience="company",
        destination="brief",
        purpose=purpose,
        **kwargs,
    )


async def _publish_autopublished_brief(*, tenant_id: str, brief_id: str) -> int:
    from app.delivery.slack_dm import _publish_to_app

    async with acquire() as conn:
        brief = await conn.fetchrow(
            """
            SELECT b.*, COALESCE(cp.items, '[]'::jsonb) AS context_items
            FROM briefs b
            LEFT JOIN context_packs cp ON cp.id = b.context_pack_id
            WHERE b.id = $1 AND b.tenant_id = $2
              AND b.acl_state = 'stamped'
              AND b.acl_freshness_state = 'fresh'
              AND b.allowed_principals ?| $3::text[]
            """,
            brief_id, tenant_id, _company_principals(tenant_id),
        )
    if not brief:
        return 0
    content, _ = _apply_destination_claim_gate(
        brief["content_md"],
        {"valid": True},
        _json_array(brief["context_items"]),
        _company_principals(tenant_id),
    )
    return await _publish_to_app(tenant_id, {
        "type": "brief",
        "brief_id": brief_id,
        "body": content,
        "brief_date": str(brief["brief_date"]),
    })


async def _source_freshness_items(tenant_id: str, destination_principals: list[str] | None = None) -> list[dict]:
    principals = destination_principals or _company_principals(tenant_id)
    async with acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT s.id, s.source_type, s.status,
                   GREATEST(s.last_webhook_at, s.last_synced_at) AS freshest,
                   s.stale_since, s.degraded_reason
            FROM sources s
            WHERE s.tenant_id = $1
              AND EXISTS (
                  SELECT 1
                  FROM source_resources sr
                  WHERE sr.tenant_id = s.tenant_id
                    AND sr.source_id = s.id
                    AND sr.acl_state = 'stamped'
                    AND sr.acl_freshness_state = 'fresh'
                    AND sr.allowed_principals ?| $2::text[]
              )
            ORDER BY s.source_type
            """,
            tenant_id,
            principals,
        )
    items: list[dict] = []
    for r in rows:
        freshest = f"{r['freshest']:%Y-%m-%d %H:%M}" if r["freshest"] else "never"
        stale = f", stale since {r['stale_since']:%Y-%m-%d %H:%M}" if r["stale_since"] else ""
        reason = ", source details require source access" if r["degraded_reason"] else ""
        item = {
            "kind": "source",
            "id": str(r["id"]),
            "text": f"Source freshness: {r['source_type']} is {r['status']}, last data {freshest}{stale}{reason}.",
            "source_id": str(r["id"]),
            "source_type": r["source_type"],
            "source_external_id": None,
            "source_identity_hints": [],
            "source_url": None,
            "priority": 2,
            "score": 1.0,
            "visibility": "company",
            "allowed_principals": principals,
            "acl_state": "stamped",
            "acl_freshness_state": "fresh",
        }
        if source_visible_to_principals(item, principals):
            items.append(item)
    return items


async def _prior_brief_pack_item(tenant_id: str, brief_date, principals: list[str]) -> dict | None:
    """Slice 2.2: the last brief actually shown to the tenant (delivered or
    awaiting review), so the write call can carry unresolved items forward
    and collapse unchanged workstreams instead of re-deriving the day from
    scratch."""
    async with acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, brief_date, content_md
            FROM briefs
            WHERE tenant_id = $1
              AND brief_date < $2
              AND status IN ('delivered', 'pending_review')
              AND allowed_principals ?| $3::text[]
            ORDER BY brief_date DESC
            LIMIT 1
            """,
            tenant_id, brief_date, principals,
        )
    if not row or not str(row["content_md"] or "").strip():
        return None
    return {
        "kind": "prior_brief",
        "id": str(row["id"]),
        "text": f"Brief delivered {row['brief_date']}:\n{row['content_md']}",
        "priority": 3,
        "score": 1.0,
        "visibility": "company",
        "allowed_principals": principals,
        "acl_state": "stamped",
        "acl_freshness_state": "fresh",
    }


async def _augment_context_pack(tenant_id: str, pack: dict, extra_items: list[dict]) -> dict:
    items = list(pack["items"])
    seen = {f"{item['kind']}:{item['id']}" for item in items}
    for item in extra_items:
        if not source_visible_to_principals(item, _company_principals(tenant_id)):
            continue
        key = f"{item['kind']}:{item['id']}"
        if key not in seen:
            seen.add(key)
            items.append(item)
    text = _render_pack(items)
    token_count = sum(len(item["text"]) // 4 + 20 for item in items)
    async with acquire() as conn:
        await conn.execute(
            """
            UPDATE context_packs
            SET items = $3::jsonb, token_count = $4
            WHERE id = $1 AND tenant_id = $2
            """,
            pack["context_pack_id"],
            tenant_id,
            json.dumps(items),
            token_count,
        )
    return {**pack, "items": items, "text": text}


async def _selected_story_object_ids(*, tenant_id: str, grouped_stories: dict) -> list[str]:
    refs: set[str] = set()
    for story in grouped_stories.get("stories") or []:
        for ref in story.get("evidence_refs") or []:
            if isinstance(ref, str) and ":" in ref:
                refs.add(ref)
    object_ids = {ref.split(":", 1)[1] for ref in refs if ref.startswith("object:")}
    digest_ids = [ref.split(":", 1)[1] for ref in refs if ref.startswith("workstream_digest:")]
    fact_ids = [ref.split(":", 1)[1] for ref in refs if ref.startswith("fact:")]

    if digest_ids:
        async with acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT anchor_object_id, evidence_refs
                FROM workstream_digests
                WHERE tenant_id = $1 AND id = ANY($2::uuid[])
                """,
                tenant_id,
                digest_ids,
            )
        for row in rows:
            if row["anchor_object_id"]:
                object_ids.add(str(row["anchor_object_id"]))
            for evidence_ref in _json_array(row["evidence_refs"]):
                if isinstance(evidence_ref, str) and evidence_ref.startswith("object:"):
                    object_ids.add(evidence_ref.split(":", 1)[1])
                if isinstance(evidence_ref, str) and evidence_ref.startswith("fact:"):
                    fact_ids.append(evidence_ref.split(":", 1)[1])

    if fact_ids:
        async with acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT brain_object_id
                FROM facts
                WHERE tenant_id = $1 AND id = ANY($2::uuid[])
                  AND brain_object_id IS NOT NULL
                """,
                tenant_id,
                list(set(fact_ids)),
            )
        for row in rows:
            object_ids.add(str(row["brain_object_id"]))

    return sorted(object_ids)


def _render_pack(items: list[dict]) -> str:
    blocks = []
    for item in items:
        ref = f"{item['kind']}:{item['id']}"
        url = f" url={item['source_url']}" if item.get("source_url") else ""
        blocks.append(f"<source ref=\"{ref}\"{url}>\n{item['text']}\n</source>")
    return "\n\n".join(blocks)


async def _feedback_rules_line(tenant_id: str) -> str:
    async with acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT rule_type, value, reason
            FROM feedback_rules
            WHERE tenant_id = $1 AND active
            ORDER BY created_at DESC
            LIMIT 20
            """,
            tenant_id,
        )
    if not rows:
        return "No active feedback rules."
    return "\n".join(
        f"- {row['rule_type']}: {row['value']} ({row['reason'] or 'no reason'})"
        for row in rows
    )
