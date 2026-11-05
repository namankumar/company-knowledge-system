"""
Drift synthesis: bounded context pack in, alert/suppression/open question out.

An alert without verified evidence is invalid — it becomes a suppression plus
a system issue, never a delivered alert. Alerts that pass verification land in
pending_review for the operator's approval before anything reaches the founder.
"""
import json
import hashlib
import re
import uuid
from math import sqrt

import structlog

from app.core.db import acquire
from app.core import trace
from app.core.tenant_settings import autopublish_read_only_enabled, client_publishing_paused, synthesis_paused
from app.llm.client import call_llm
from app.presentation.alerts import project_alert, resolve_source_observed_at
from app.retrieval.embed import embed_text, embeddings_enabled
from app.retrieval.retrieve import build_authorized_context_pack as build_context_pack, audience_principals
from app.synthesis.feedback_tuning import (
    DEFAULT_PUBLISH_BAR,
    min_confidence_to_publish,
    recent_rejected_claims,
    render_recent_rejected_block,
)
from app.workstreams.attach import resolve_alert_attachment, sync_persisted_member_attachment
from app.synthesis.verifier import verify_evidence

logger = structlog.get_logger()

DRIFT_PROMPT_VERSION = "v1.5"
MERGEABLE_ALERT_STATUSES = ("pending_review", "approved", "delivered", "publishing", "delivery_paused")
MERGE_SCORE_THRESHOLD = 0.42
EMBEDDING_ONLY_THRESHOLD = 0.78
EMBEDDING_LABEL_THRESHOLD = 0.65
MERGE_WINDOW_DAYS = 14
SEVERITY_RANK = {"low": 1, "medium": 2, "high": 3}
STOPWORDS = {
    "about", "after", "again", "already", "around", "because", "before", "being",
    "between", "could", "customer", "customers", "date", "does", "done", "earliest",
    "from", "have", "into", "launch", "needs", "next", "pilot", "project", "realistic",
    "should", "shows", "still", "that", "their", "there", "this", "today", "until",
    "what", "when", "where", "which", "while", "will", "with", "work",
}

SYSTEM = """You are an operating intelligence system for a startup founder or manager.
Detect places where reality is diverging from a stated or inferred plan,
deadline, customer commitment, owner expectation, or operating cadence.
Use ONLY the provided sources. Mark inferred claims as inferred.
If evidence is insufficient, return suppress or open_question — never invent.
This output is rendered in HTML. Write plain text fields only. Do not use
markdown bullets, headings, tables, or citation syntax inside prose fields.
Every evidence ref must be copied exactly from a <source ref="..."> attribute
in the context. Never use new_activity, trigger, context, source, or a URL as
an evidence ref."""

PROMPT = """<context>
{context}
</context>

<recently_rejected>
{recently_rejected}
</recently_rejected>

<new_activity>
{trigger}
</new_activity>

Does the new activity, combined with the context, materially change the risk picture?

Publish only if there is an actionable change, risk, blocker, ownership gap,
decision need, customer contradiction, or important open question.

Look for: deadline risk, customer commitment risk, unclear owner, repeated blocker,
plan mismatch, progress claimed without evidence, customer signal contradicting
project status, important open question.

Return JSON:
{{
  "decision": "alert|suppress|open_question",
  "drift_type": "deadline_risk|commitment_risk|unclear_owner|repeated_blocker|plan_mismatch|progress_gap|customer_contradiction|other",
  "severity": "high|medium|low",
  "confidence": 0.0,
  "claim": "one sentence: what is drifting and why it matters",
  "why_now": "one compact, information-dense paragraph explaining what changed, the expected plan or commitment, the contradiction, why it matters now, and what is still missing",
  "evidence": [{{"ref": "<exact source ref attribute copied from context, e.g. event:uuid or object:uuid>", "quote": "supporting text copied or closely paraphrased from that source"}}],
  "missing_evidence": ["what would confirm or refute this"],
  "recommended_action": "one concrete next step a named human can take today, including what artifact/status/customer message should be updated",
  "related": {{"people": [], "projects": [], "commitments": []}}
}}

Good why_now paragraphs include specific names, dates, commitments, prior context,
and the cost of waiting. Example style: "Acme reaffirmed the June 30 pilot
expectation while the internal launch date moved to July 8 and no named owner
has been assigned to notify them. The new activity repeats the same unresolved
state already visible in the planning sources: customer date unchanged,
internal date slipped, and customer communication still ownerless. Waiting
until Friday increases surprise risk because Acme has already asked for the
pilot timing and no source shows a decision."

Avoid thin generic wording like "the plan artifact has not caught up" or
"the customer commitment remains active" when sources contain richer facts.
If evidence is sparse, say exactly what is missing instead of becoming vague.

Use the recently_rejected block only when the rejected example matches the same
drift_type you are considering. Never suppress one drift_type because a
different drift_type was rejected before. If the current signal is
substantively the same rejected claim without new contradictory evidence, prefer
suppress and say the evidence is already known or resolved.

Suppress when: routine progress, already-known state, no material change, gossip.
Open question when: signal is real but evidence is one-sided or ambiguous."""


async def run_drift_synthesis(
    ctx: dict,
    *,
    tenant_id: str,
    normalized_event_id: str | None = None,
    thread_key: str | None = None,
    object_ids: list[str] | None = None,
    pipeline_run_id: str | None = None,
    source_id: str | None = None,
    trigger_override: str | None = None,
    recent_hours: int = 48,
) -> None:
    from app.core.db import _current_tenant_id
    _current_tenant_id.set(tenant_id)
    run_id = pipeline_run_id or await trace.start_run(
        tenant_id=tenant_id,
        kind="synthesis",
    )

    try:
        input_ref = normalized_event_id or source_id or "manual"
        async with trace.step(run_id, tenant_id, "context_retrieved", input_ref=input_ref) as st:
            allowed_principals = _company_principals(tenant_id)
            async with acquire() as conn:
                if trigger_override is not None:
                    trigger_rows = []
                elif source_id:
                    trigger_rows = await conn.fetch(
                        """
                        SELECT ne.actor_label, ne.occurred_at, ne.text, ne.source_url
                        FROM normalized_events ne
                        JOIN raw_events re ON re.id = ne.raw_event_id
                        WHERE ne.tenant_id = $1 AND re.source_id = $2
                          AND ne.acl_state = 'stamped'
                          AND ne.acl_freshness_state = 'fresh'
                          AND ne.allowed_principals ?| $3::text[]
                        ORDER BY ne.occurred_at DESC LIMIT 20
                        """,
                        tenant_id, source_id, allowed_principals,
                    )
                elif normalized_event_id:
                    trigger_rows = await conn.fetch(
                        """
                        SELECT actor_label, occurred_at, text, source_url FROM normalized_events
                        WHERE tenant_id = $1 AND (id = $2 OR (thread_key = $3 AND $3 IS NOT NULL))
                          AND acl_state = 'stamped'
                          AND acl_freshness_state = 'fresh'
                          AND allowed_principals ?| $4::text[]
                        ORDER BY occurred_at DESC LIMIT 10
                        """,
                        tenant_id, normalized_event_id, thread_key, allowed_principals,
                    )
                else:
                    trigger_rows = []
                tenant_settings_raw = await conn.fetchval(
                    "SELECT settings FROM tenants WHERE id = $1",
                    tenant_id,
                )
            tenant_settings = (
                json.loads(tenant_settings_raw)
                if isinstance(tenant_settings_raw, str)
                else dict(tenant_settings_raw or {})
            )
            if synthesis_paused(tenant_settings):
                st.skip("tenant pause_synthesis is enabled")
                await trace.finish_run(run_id, status="paused")
                return
            if trigger_override is not None:
                trigger = trigger_override
            else:
                trigger = "\n".join(
                    f"[{r['occurred_at']:%Y-%m-%d %H:%M}] {r['actor_label'] or 'unknown'}: {r['text'][:800]}"
                    for r in reversed(trigger_rows)
                )
            if not trigger:
                trigger = "Manual synthesis requested. Re-check the current brain and recent source activity."
            pack = await _build_company_context_pack(
                tenant_id=tenant_id,
                purpose="drift",
                object_ids=object_ids,
                recent_hours=recent_hours,
            )
            try:
                rejected_claims = await recent_rejected_claims(tenant_id=tenant_id, limit=6)
            except Exception as e:
                logger.warning("recent_rejected_claims_unavailable", tenant_id=tenant_id, error=str(e))
                await _record_issue(
                    tenant_id,
                    "feedback_tuning_unavailable",
                    {"stage": "recent_rejected_claims", "error": str(e)[:500]},
                    pipeline_run_id=run_id,
                )
                rejected_claims = []
            rejected_block = render_recent_rejected_block(rejected_claims)
            st.output_ref = pack["context_pack_id"]
            st.detail = {"items": len(pack["items"]), "recently_rejected": rejected_block != "None."}

        async with trace.step(run_id, tenant_id, "synthesized", input_ref=pack["context_pack_id"]) as st:
            result = await call_llm(
                task="drift",
                system=SYSTEM,
                prompt=PROMPT.format(context=pack["text"], recently_rejected=rejected_block, trigger=trigger),
                tenant_id=tenant_id,
                tenant_settings=tenant_settings,
                max_tokens=1500,
                prompt_version=DRIFT_PROMPT_VERSION,
                context_pack_id=pack["context_pack_id"],
                pipeline_run_id=run_id,
            )
            st.output_ref = result["model_call_id"]

        parsed = result["parsed"]
        if not isinstance(parsed, dict) or parsed.get("decision") not in ("alert", "suppress", "open_question"):
            logger.warning("drift_output_invalid", tenant_id=tenant_id, model_call_id=result["model_call_id"])
            await _record_issue(
                tenant_id,
                "invalid_synthesis_output",
                {"model_call_id": result["model_call_id"]},
                pipeline_run_id=run_id,
            )
            await trace.finish_run(run_id, status="failed", error="invalid synthesis output")
            return

        async with trace.step(run_id, tenant_id, "verified", input_ref=result["model_call_id"]) as st:
            decision = parsed["decision"]
            verification = verify_evidence(parsed.get("evidence", []), pack["items"])
            st.detail = {
                "valid": verification["valid"],
                "invalid_refs": verification["invalid_refs"],
                "unmatched_quotes": verification["unmatched_quotes"],
            }
            if decision == "alert" and not verification["valid"]:
                await _record_issue(
                    tenant_id,
                    "unsupported_claim",
                    {"invalid_refs": verification["invalid_refs"], "model_call_id": result["model_call_id"]},
                    pipeline_run_id=run_id,
                )
                decision = "suppress"
                parsed["why_now"] = (
                    (parsed.get("why_now") or "")
                    + " [auto-suppressed: evidence failed verification]"
                )
            if decision == "alert" and verification["valid"]:
                try:
                    publish_bar = await min_confidence_to_publish(
                        tenant_id=tenant_id,
                        drift_type=parsed.get("drift_type"),
                    )
                except Exception as e:
                    logger.warning(
                        "drift_publish_bar_unavailable",
                        tenant_id=tenant_id,
                        drift_type=parsed.get("drift_type"),
                        error=str(e),
                    )
                    await _record_issue(
                        tenant_id,
                        "feedback_tuning_unavailable",
                        {
                            "stage": "min_confidence_to_publish",
                            "drift_type": parsed.get("drift_type"),
                            "error": str(e)[:500],
                        },
                        pipeline_run_id=run_id,
                    )
                    publish_bar = DEFAULT_PUBLISH_BAR
                confidence = float(parsed.get("confidence") or 0.0)
                if confidence < publish_bar:
                    await _record_issue(
                        tenant_id,
                        "tuned_suppression",
                        {
                            "drift_type": parsed.get("drift_type"),
                            "confidence": confidence,
                            "publish_bar": publish_bar,
                            "claim": parsed.get("claim"),
                            "model_call_id": result["model_call_id"],
                        },
                        pipeline_run_id=run_id,
                    )
                    decision = "suppress"
                    parsed["why_now"] = (
                        (parsed.get("why_now") or "")
                        + f" [auto-suppressed: confidence {confidence:.2f} below tenant publish bar {publish_bar:.2f}]"
                    )

        kind = {"alert": "alert", "suppress": "suppression", "open_question": "open_question"}[decision]
        autopublish = (
            kind == "alert"
            and _autopublish_read_only(tenant_settings)
            and not client_publishing_paused(tenant_settings)
        )
        status = "publishing" if autopublish else ("pending_review" if kind != "suppression" else "suppressed")
        evidence_out = verification["verified"] + verification["flagged"]
        if not evidence_out:
            evidence_out = [
                {**e, "quote_verified": False}
                for e in parsed.get("evidence", [])
                if isinstance(e, dict)
            ]

        async with trace.step(run_id, tenant_id, "output_created", input_ref=result["model_call_id"]) as st:
            async with acquire() as conn:
                output = await _upsert_alert_issue(
                    conn,
                    tenant_id=tenant_id,
                    kind=kind,
                    status=status,
                    parsed=parsed,
                    evidence=evidence_out,
                    context_pack_id=pack["context_pack_id"],
                    model_call_id=result["model_call_id"],
                    pipeline_run_id=run_id,
                )
            alert_id = output["alert_id"]
            st.output_ref = alert_id
            st.detail = {
                "kind": kind,
                "status": output["status"],
                "merged": output["merged"],
                "issue_fingerprint": output["issue_fingerprint"],
                "occurrence_count": output["occurrence_count"],
            }

        if autopublish and (not output["merged"] or output["status"] == "delivered"):
            receivers = await _publish_autopublished_alert(tenant_id=tenant_id, alert_id=alert_id)
            if receivers is None:
                if output["status"] == "publishing":
                    async with acquire() as conn:
                        await conn.execute(
                            """
                            UPDATE alerts
                            SET status = 'delivery_blocked'
                            WHERE id = $1 AND tenant_id = $2 AND status = 'publishing'
                            """,
                            alert_id, tenant_id,
                        )
                if run_id:
                    async with trace.step(run_id, tenant_id, "app_delivery", input_ref=alert_id) as st:
                        st.status = "skipped"
                        st.detail = {
                            "channel": f"deliveries:{tenant_id}",
                            "autopublished": True,
                            "blocked": True,
                            "redis_receivers": 0,
                            "pull_api_visible": False,
                        }
                    await trace.finish_run(run_id, status="delivery_blocked")
            elif output["status"] == "publishing":
                async with acquire() as conn:
                    await conn.execute(
                        """
                        UPDATE alerts
                        SET status = 'delivered',
                            delivered_via = delivered_via || $3::jsonb
                        WHERE id = $1 AND tenant_id = $2 AND status = 'publishing'
                        """,
                        alert_id, tenant_id, json.dumps(["app_auto"]),
                    )
            if receivers is not None and run_id:
                async with trace.step(run_id, tenant_id, "app_delivery", input_ref=alert_id) as st:
                    st.detail = {
                        "channel": f"deliveries:{tenant_id}",
                        "autopublished": True,
                        "redis_receivers": receivers,
                        "pull_api_visible": True,
                    }
                await trace.finish_run(run_id, status="delivered")
        elif output["status"] == "pending_review":
            await trace.set_run_status(run_id, "pending_review")
        elif output["status"] == "delivered":
            await trace.finish_run(run_id, status="delivered")
        else:
            await trace.finish_run(run_id, status=output["status"])

        logger.info(
            "drift_synthesis_complete", tenant_id=tenant_id, alert_id=alert_id,
            decision=decision, drift_type=parsed.get("drift_type"), severity=parsed.get("severity"),
            verified=verification["valid"], pipeline_run_id=run_id,
        )
    except Exception as e:
        await trace.finish_run(run_id, status="failed", error=f"{type(e).__name__}: {e}")
        raise


def _autopublish_read_only(tenant_settings: dict) -> bool:
    return autopublish_read_only_enabled(tenant_settings)


def _company_principals(tenant_id: str) -> list[str]:
    return audience_principals(
        tenant_id=tenant_id,
        actor_user_id=None,
        target_audience="company",
    )


async def _build_company_context_pack(*, tenant_id: str, purpose: str, **kwargs) -> dict:
    return await build_context_pack(
        tenant_id=tenant_id,
        actor_user_id=None,
        requester_user_id=None,
        target_audience="company",
        destination="alert",
        purpose=purpose,
        **kwargs,
    )


async def _upsert_alert_issue(
    conn,
    *,
    tenant_id: str,
    kind: str,
    status: str,
    parsed: dict,
    evidence: list[dict],
    context_pack_id: str,
    model_call_id: str,
    pipeline_run_id: str,
    visibility: str = "company",
    allowed_principals: list[str] | None = None,
    acl_basis: str = "derived_context_pack",
    acl_confidence: str = "medium",
) -> dict:
    normalized_allowed_principals = allowed_principals or _company_principals(tenant_id)
    fingerprint = _issue_fingerprint(
        parsed,
        visibility=visibility,
        allowed_principals=normalized_allowed_principals,
    )
    candidate = None
    claim_embedding = None
    claim_text = str(parsed.get("claim") or "").strip()
    if kind == "alert":
        if claim_text and embeddings_enabled():
            claim_embedding = await embed_text(claim_text)
        candidate_query = """
            SELECT id, issue_fingerprint, kind, drift_type, severity, confidence, claim, claim_embedding, why_now,
                   evidence, missing_evidence, recommended_action, related, status, occurrence_count,
                   merged_run_ids, merged_context_pack_ids, merged_model_call_ids, allowed_principals
            FROM alerts
            WHERE tenant_id = $1
              AND kind = 'alert'
              AND status = ANY($2::text[])
              AND visibility = $3
              AND drift_type IS NOT DISTINCT FROM $4
              AND COALESCE(last_seen_at, created_at) >= now() - ($5::text)::interval
        """
        query_args: list[object] = [
            tenant_id,
            list(MERGEABLE_ALERT_STATUSES),
            visibility,
            parsed.get("drift_type"),
            f"{MERGE_WINDOW_DAYS} days",
        ]
        if visibility == "private":
            candidate_query += "\n              AND allowed_principals = $6::jsonb"
            query_args.append(json.dumps(normalized_allowed_principals))
        candidate_query += "\n            ORDER BY COALESCE(last_seen_at, created_at) DESC\n            LIMIT 50\n            "
        candidates = await conn.fetch(
            candidate_query,
            *query_args,
        )
        candidate = _best_merge_candidate(
            parsed,
            fingerprint,
            [dict(row) for row in candidates],
            claim_embedding=claim_embedding,
            visibility=visibility,
            allowed_principals=normalized_allowed_principals,
        )

    if candidate:
        merged = _merged_alert_payload(candidate, parsed, evidence, fingerprint)
        stored_claim_embedding = await _resolve_claim_embedding(
            stored_claim=merged.get("claim"),
            parsed_claim=claim_text,
            parsed_embedding=claim_embedding,
            candidate_claim=candidate.get("claim"),
            candidate_embedding=candidate.get("claim_embedding"),
        )
        merged_alert_row = {
            **candidate,
            "drift_type": parsed.get("drift_type"),
            "related": merged["related"],
        }
        workstream_id = await resolve_alert_attachment(
            conn,
            tenant_id=tenant_id,
            alert_row=merged_alert_row,
        )
        await conn.execute(
            """
            UPDATE alerts
            SET issue_fingerprint = $3,
                severity = $4,
                confidence = $5,
                claim = $6,
                why_now = $7,
                evidence = $8::jsonb,
                missing_evidence = $9::jsonb,
                recommended_action = $10,
                related = $11::jsonb,
                first_seen_at = COALESCE(first_seen_at, created_at),
                last_seen_at = now(),
                occurrence_count = COALESCE(occurrence_count, 1) + 1,
                merged_run_ids = $12::jsonb,
                merged_context_pack_ids = $13::jsonb,
                merged_model_call_ids = $14::jsonb,
                visibility = $15,
                acl_basis = $16,
                acl_confidence = $17,
                allowed_principals = $18::jsonb,
                acl_state = 'stamped',
                acl_freshness_state = 'fresh',
                workstream_id = $19,
                claim_embedding = $20::vector
            WHERE id = $1 AND tenant_id = $2
            """,
            candidate["id"],
            tenant_id,
            fingerprint,
            merged["severity"],
            merged["confidence"],
            merged["claim"],
            merged["why_now"],
            json.dumps(merged["evidence"]),
            json.dumps(merged["missing_evidence"]),
            merged["recommended_action"],
            json.dumps(merged["related"]),
            json.dumps(_append_unique_json(candidate.get("merged_run_ids"), pipeline_run_id)),
            json.dumps(_append_unique_json(candidate.get("merged_context_pack_ids"), context_pack_id)),
            json.dumps(_append_unique_json(candidate.get("merged_model_call_ids"), model_call_id)),
            visibility,
            acl_basis,
            acl_confidence,
            json.dumps(allowed_principals or _company_principals(tenant_id)),
            workstream_id,
            str(stored_claim_embedding) if stored_claim_embedding is not None else None,
        )
        await sync_persisted_member_attachment(
            conn,
            tenant_id=tenant_id,
            workstream_id=workstream_id,
            member_type="alert",
            member_id=str(candidate["id"]),
            resolution_method="alert_projection",
            reason="attached alert to durable workstream during drift synthesis merge",
            metadata={
                "drift_type": parsed.get("drift_type"),
                "severity": merged["severity"],
                "issue_fingerprint": fingerprint,
            },
        )
        return {
            "alert_id": str(candidate["id"]),
            "merged": True,
            "status": candidate["status"],
            "issue_fingerprint": fingerprint,
            "occurrence_count": int(candidate.get("occurrence_count") or 1) + 1,
        }

    alert_id = str(uuid.uuid4())
    workstream_id = await resolve_alert_attachment(
        conn,
        tenant_id=tenant_id,
        alert_row={
            "id": alert_id,
            "kind": kind,
            "drift_type": parsed.get("drift_type"),
            "related": parsed.get("related", {}),
        },
    )
    await conn.execute(
        """
        INSERT INTO alerts
            (id, tenant_id, kind, drift_type, severity, confidence, claim, why_now,
             evidence, missing_evidence, recommended_action, related,
             context_pack_id, model_call_id, status, pipeline_run_id, delivered_via,
             issue_fingerprint, first_seen_at, last_seen_at, occurrence_count,
             visibility, acl_basis, acl_confidence, allowed_principals, acl_state, acl_freshness_state, workstream_id, claim_embedding)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb,$10::jsonb,$11,$12::jsonb,$13,$14,$15,$16,$17::jsonb,$18,now(),now(),1,
                $19, $20, $21, $22::jsonb, 'stamped', 'fresh', $23, $24::vector)
        """,
        alert_id,
        tenant_id,
        kind,
        parsed.get("drift_type"),
        parsed.get("severity"),
        float(parsed.get("confidence") or 0.5),
        parsed.get("claim", ""),
        parsed.get("why_now"),
        json.dumps(evidence),
        json.dumps(parsed.get("missing_evidence", [])),
        parsed.get("recommended_action"),
        json.dumps(parsed.get("related", {})),
        context_pack_id,
        model_call_id,
        status,
        pipeline_run_id,
        json.dumps([]),
        fingerprint,
        visibility,
        acl_basis,
        acl_confidence,
        json.dumps(allowed_principals or _company_principals(tenant_id)),
        workstream_id,
        str(claim_embedding) if claim_embedding is not None else None,
    )
    await sync_persisted_member_attachment(
        conn,
        tenant_id=tenant_id,
        workstream_id=workstream_id,
        member_type="alert",
        member_id=alert_id,
        resolution_method="alert_projection",
        reason="attached alert to durable workstream during drift synthesis insert",
        metadata={
            "drift_type": parsed.get("drift_type"),
            "severity": parsed.get("severity"),
            "issue_fingerprint": fingerprint,
        },
    )
    return {
        "alert_id": alert_id,
        "merged": False,
        "status": status,
        "issue_fingerprint": fingerprint,
        "occurrence_count": 1,
    }


def _best_merge_candidate(
    parsed: dict,
    fingerprint: str,
    candidates: list[dict],
    *,
    claim_embedding=None,
    visibility: str | None = None,
    allowed_principals: list[str] | None = None,
) -> dict | None:
    best: tuple[float, dict] | None = None
    for candidate in candidates:
        if visibility == "private" and not _private_allowed_principals_match(candidate, allowed_principals):
            continue
        if candidate.get("issue_fingerprint") == fingerprint:
            return candidate
        score = _merge_score(parsed, candidate, claim_embedding=claim_embedding)
        if score >= MERGE_SCORE_THRESHOLD and (best is None or score > best[0]):
            best = (score, candidate)
    return best[1] if best else None


def _merge_score(parsed: dict, candidate: dict, *, claim_embedding=None) -> float:
    new_labels = set(_related_labels(parsed.get("related")))
    old_labels = set(_related_labels(candidate.get("related")))
    label_overlap = len(new_labels & old_labels)
    if new_labels or old_labels:
        label_score = label_overlap / max(min(len(new_labels), len(old_labels)), 1)
    else:
        label_score = 0.0
    new_tokens = _issue_tokens(parsed)
    old_tokens = _issue_tokens(candidate)
    if not new_tokens or not old_tokens:
        token_score = 0.0
    else:
        token_score = len(new_tokens & old_tokens) / max(min(len(new_tokens), len(old_tokens)), 1)
    embedding_score = _embedding_similarity(claim_embedding, candidate.get("claim_embedding"))
    if (new_labels or old_labels) and not label_overlap:
        return 0.0
    if not (new_labels or old_labels):
        if embedding_score is not None and embedding_score >= EMBEDDING_ONLY_THRESHOLD:
            return embedding_score
        return token_score if token_score >= 0.55 else 0.0
    if embedding_score is not None and embedding_score >= EMBEDDING_LABEL_THRESHOLD:
        return (label_score * 0.35) + (embedding_score * 0.65)
    if embedding_score is not None:
        return (label_score * 0.55) + (token_score * 0.45) if token_score >= 0.20 else 0.0
    if token_score >= 0.20:
        return (label_score * 0.55) + (token_score * 0.45)
    return 0.0


def _issue_fingerprint(parsed: dict, *, visibility: str | None = None, allowed_principals: list[str] | None = None) -> str:
    drift_type = _normalize_text(parsed.get("drift_type") or "alert")
    labels = _related_labels(parsed.get("related"))
    label_part = "|".join(labels[:5]) or "no-related-label"
    token_part = "|".join(sorted(_issue_tokens(parsed))[:12])
    principal_part = ""
    if visibility == "private":
        principal_part = "|".join(sorted(_normalize_text(principal) for principal in (allowed_principals or []))) or "no-private-principal"
    raw = f"{drift_type}|{label_part}|{token_part}|{visibility or 'company'}|{principal_part}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _private_allowed_principals_match(candidate: dict, allowed_principals: list[str] | None) -> bool:
    if not allowed_principals:
        return False
    candidate_principals = _json_list(candidate.get("allowed_principals"))
    return sorted(candidate_principals) == sorted(allowed_principals)


def _merged_alert_payload(candidate: dict, parsed: dict, evidence: list[dict], fingerprint: str) -> dict:
    existing_evidence = _json_list(candidate.get("evidence"))
    existing_missing = _json_list(candidate.get("missing_evidence"))
    existing_related = _json_dict(candidate.get("related"))
    new_related = _json_dict(parsed.get("related"))
    old_confidence = float(candidate.get("confidence") or 0.0)
    new_confidence = float(parsed.get("confidence") or 0.5)
    return {
        "issue_fingerprint": fingerprint,
        "severity": _max_severity(candidate.get("severity"), parsed.get("severity")),
        "confidence": max(old_confidence, new_confidence),
        "claim": _prefer_richer_text(parsed.get("claim"), candidate.get("claim")),
        "why_now": _prefer_richer_text(parsed.get("why_now"), candidate.get("why_now")),
        "evidence": _merge_json_items(existing_evidence, evidence, limit=24),
        "missing_evidence": _merge_json_items(existing_missing, parsed.get("missing_evidence", []), limit=12),
        "recommended_action": _prefer_richer_text(parsed.get("recommended_action"), candidate.get("recommended_action")),
        "related": _merge_related(existing_related, new_related),
    }


def _max_severity(old: str | None, new: str | None) -> str | None:
    old_key = str(old or "").lower()
    new_key = str(new or "").lower()
    return old if SEVERITY_RANK.get(old_key, 0) >= SEVERITY_RANK.get(new_key, 0) else new


def _prefer_richer_text(new: str | None, old: str | None) -> str | None:
    new_text = str(new or "").strip()
    old_text = str(old or "").strip()
    if not old_text:
        return new_text
    if not new_text:
        return old_text
    if len(new_text) >= len(old_text) * 0.8:
        return new_text
    return old_text


def _merge_related(existing: dict, new: dict) -> dict:
    merged = dict(existing or {})
    for key, value in (new or {}).items():
        if isinstance(value, list):
            merged[key] = _merge_json_items(_json_list(merged.get(key)), value, limit=30)
        elif isinstance(value, dict):
            prior = merged.get(key)
            merged[key] = {**(prior if isinstance(prior, dict) else {}), **value}
        elif value not in (None, "", []):
            merged[key] = value
    return merged


def _merge_json_items(existing, new, *, limit: int) -> list:
    output = []
    seen = set()
    for item in [*_json_list(existing), *_json_list(new)]:
        key = _json_key(item)
        if key in seen:
            continue
        seen.add(key)
        output.append(item)
        if len(output) >= limit:
            break
    return output


def _append_unique_json(existing, value: str | None, *, limit: int = 50) -> list[str]:
    values = [str(item) for item in _json_list(existing) if item]
    if value and value not in values:
        values.append(value)
    return values[-limit:]


def _json_key(item) -> str:
    if isinstance(item, dict):
        parts = [item.get("ref"), item.get("source_ref"), item.get("url"), item.get("quote"), item.get("quote_or_summary")]
        if any(parts):
            return "|".join(str(part or "") for part in parts)
    return json.dumps(item, sort_keys=True, default=str)


def _json_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []
    return value if isinstance(value, list) else []


def _json_dict(value) -> dict:
    if value is None:
        return {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return {}
    return value if isinstance(value, dict) else {}


def _related_labels(related) -> list[str]:
    related = _json_dict(related)
    labels = []
    for key in ("projects", "commitments", "customers", "accounts", "people"):
        for item in _json_list(related.get(key)):
            label = _item_label(item)
            if label:
                normalized = _normalize_text(label)
                if normalized and normalized not in labels:
                    labels.append(normalized)
    return labels


def _item_label(item) -> str | None:
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        for key in ("title", "name", "canonical_name", "label", "key", "id"):
            value = item.get(key)
            if value:
                return str(value)
    return None


def _issue_tokens(value) -> set[str]:
    if isinstance(value, dict):
        text = " ".join(
            str(part or "")
            for part in [
                value.get("claim"),
                value.get("why_now"),
                value.get("recommended_action"),
                " ".join(_related_labels(value.get("related"))),
            ]
        )
    else:
        text = str(value or "")
    tokens = set()
    for token in re.findall(r"[a-z0-9]+", _normalize_text(text)):
        if len(token) >= 3 and token not in STOPWORDS:
            tokens.add(token)
    return tokens


def _normalize_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", str(value or "").lower()).strip()


def _embedding_similarity(current_embedding, candidate_embedding) -> float | None:
    left = _parse_embedding(current_embedding)
    right = _parse_embedding(candidate_embedding)
    if not left or not right or len(left) != len(right):
        return None
    left_norm = sqrt(sum(value * value for value in left))
    right_norm = sqrt(sum(value * value for value in right))
    if left_norm == 0 or right_norm == 0:
        return None
    dot = sum(left_value * right_value for left_value, right_value in zip(left, right))
    return dot / (left_norm * right_norm)


async def _resolve_claim_embedding(
    *,
    stored_claim,
    parsed_claim,
    parsed_embedding,
    candidate_claim=None,
    candidate_embedding=None,
) -> list[float] | None:
    stored_text = str(stored_claim or "").strip()
    if not stored_text:
        return None
    parsed_text = str(parsed_claim or "").strip()
    candidate_text = str(candidate_claim or "").strip()
    candidate_vector = _parse_embedding(candidate_embedding)
    if stored_text == parsed_text:
        if parsed_embedding is not None:
            return parsed_embedding
        if stored_text == candidate_text:
            return candidate_vector
        return None
    if stored_text == candidate_text and candidate_vector is not None:
        return candidate_vector
    if not embeddings_enabled():
        return None
    return await embed_text(stored_text)


def _parse_embedding(value) -> list[float] | None:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            cleaned = value.strip().strip("[]")
            if not cleaned:
                return None
            value = [part.strip() for part in cleaned.split(",")]
    if not isinstance(value, list):
        return None
    try:
        return [float(item) for item in value]
    except (TypeError, ValueError):
        return None


async def _publish_autopublished_alert(*, tenant_id: str, alert_id: str) -> int | None:
    from app.delivery.slack_dm import (
        _alert_destination_visibility,
        _destination_visibility_issue_detail,
        _publish_to_app,
    )

    async with acquire() as conn:
        alert = await conn.fetchrow(
            """
            SELECT * FROM alerts
            WHERE id = $1 AND tenant_id = $2
              AND acl_state = 'stamped'
              AND acl_freshness_state = 'fresh'
              AND allowed_principals ?| $3::text[]
            """,
            alert_id, tenant_id, _company_principals(tenant_id),
        )
        if alert:
            alert = dict(alert)
            alert["source_observed_at"] = await resolve_source_observed_at(
                conn, tenant_id, alert.get("evidence"), _company_principals(tenant_id)
            )
    if not alert:
        return 0
    visibility_check = _alert_destination_visibility(alert, _company_principals(tenant_id))
    if not visibility_check["ok"]:
        await _record_issue(
            tenant_id,
            "destination_visibility_blocked",
            _destination_visibility_issue_detail(alert_id, visibility_check, autopublish=True),
            pipeline_run_id=str(alert["pipeline_run_id"]) if alert.get("pipeline_run_id") else None,
        )
        return None
    projected = project_alert(alert)
    return await _publish_to_app(tenant_id, {
        "type": "alert",
        "alert_id": alert_id,
        "claim": alert["claim"],
        "severity": alert["severity"],
        "confidence": alert["confidence"],
        "drift_type": alert["drift_type"],
        "why_now": alert["why_now"],
        "recommended_action": alert["recommended_action"],
        "workstream": projected["workstream"],
        "source_chips": projected["source_chips"],
        "source_observed_at": projected["source_observed_at"],
        "missing_evidence": projected["missing_evidence"],
    })


async def _record_issue(
    tenant_id: str,
    kind: str,
    detail: dict,
    *,
    pipeline_run_id: str | None = None,
) -> None:
    async with acquire() as conn:
        await conn.execute(
            """
            INSERT INTO system_issues (tenant_id, kind, detail, pipeline_run_id)
            VALUES ($1, $2, $3::jsonb, $4)
            """,
            tenant_id, kind, json.dumps(detail), pipeline_run_id,
        )
