"""
Founder chat: question in, source-cited answer out. Pull-based, so no approval
gate — but every claim cites the pack, invalid citations are flagged, and the
answer names its missing evidence.
"""
import json
import re
import uuid

from anthropic import AnthropicError
import structlog

from app.core.db import acquire
from app.core import trace
from app.core.tenant_settings import synthesis_paused
from app.ingestion.noise import muted_drift_types, muted_workstream_keys
from app.llm.client import BudgetExhausted, call_llm
from app.llm.failures import ProviderCallError
from app.presentation.workstreams import load_company_workstreams
from app.retrieval.retrieve import (
    POLICY_VERSION as RETRIEVAL_POLICY_VERSION,
    build_authorized_context_pack as build_context_pack,
    _source_identity_hints,
)
from app.core.destination_permissions import (
    filter_claim_text_for_destination,
    source_visible_to_principals,
    verify_destination_visibility,
)
from app.synthesis.okr_context import fetch_okr_state, is_okr_question, okr_status_answer
from app.synthesis.verifier import normalize_citations, verify_inline_citations

logger = structlog.get_logger()

CHAT_PROMPT_VERSION = "v1.3"
MIN_RELEVANT_CONTEXT_SCORE = 0.18
_STOPWORDS = {
    "about",
    "after",
    "again",
    "against",
    "all",
    "also",
    "and",
    "answer",
    "any",
    "are",
    "can",
    "company",
    "customer",
    "customers",
    "does",
    "doing",
    "for",
    "from",
    "have",
    "how",
    "into",
    "latest",
    "mention",
    "mentions",
    "private",
    "project",
    "status",
    "tell",
    "that",
    "the",
    "their",
    "there",
    "this",
    "what",
    "when",
    "where",
    "which",
    "with",
}
_SOURCE_ALIASES = {
    "github": "github",
    "git": "github",
    "prs": "github",
    "gmail": "gmail",
    "email": "gmail",
    "emails": "gmail",
    "calendar": "gcal",
    "meeting": "gcal",
    "meetings": "gcal",
    "slack": "slack",
    "linear": "linear",
    "docs": "googledocs",
    "doc": "googledocs",
    "document": "googledocs",
    "drive": "googledrive",
    "file": "googledrive",
    "files": "googledrive",
    "crm": "hubspot",
    "hubspot": "hubspot",
    "pipeline": "hubspot",
}
_MVP_SOURCE_LABELS = {
    "slack": "Slack",
    "gmail": "Gmail",
    "gcal": "Calendar",
    "googledocs": "Google Docs",
    "googledrive": "Google Drive",
    "linear": "Linear",
    "github": "GitHub",
    "hubspot": "HubSpot",
}

SYSTEM = """You answer a startup founder's questions about her company using
ONLY the provided sources. Cite every factual claim, heading, and bullet with
[ref:kind:id]. Do not write uncited summary headings, section labels, or bold
labels. Distinguish stated facts from inferences — mark inferences with
(inferred). When the sources don't
contain the answer, say exactly what's missing instead of guessing. Be direct
and brief; she is busy. Citations must use the literal bracket format
[ref:kind:id] copied from <source ref="kind:id">. Do not use markdown links,
bare ids, URLs, footnotes, or a separate sources list as a substitute for
inline citations. Never write [object:id], [event:id], [alert:id], or any
other kind:id citation without the ref: prefix.

For task-planning questions, recommend a read-only plan from the sources:
why now, priority, dependencies, execution steps, draft artifact shape, approval
handoff, and explicit actions that require approval. Never imply that you wrote
to Linear, Drive, Slack, email, or any source system unless a provided source
proves that write already happened."""

PROMPT = """<context>
{context}
</context>

<recent_conversation>
{history}
</recent_conversation>

Question: {question}

Answer using only the context. End with:
- "Sources used:" — nothing extra, citations are inline
- "Missing:" one line on evidence that would improve this answer, or "nothing material"."""

REPAIR_PROMPT = """<context>
{context}
</context>

The previous answer contained uncited or unsupported claims:
{findings}

Rewrite the answer to this question using ONLY the context:
{question}

Every factual claim, heading, and bullet must include an inline [ref:kind:id]
citation. If a claim cannot be cited, remove it and put it in Missing instead.

End with:
- "Sources used:" — nothing extra, citations are inline
- "Missing:" one line on evidence that would improve this answer, or "nothing material"."""


async def answer_question(
    *,
    tenant_id: str,
    user_id: str | None,
    question: str,
    user_role: str | None = None,
    channel: str = "api",
) -> dict:
    """Returns {answer, sources, citations_valid, chat_message_id}."""
    if user_role == "member":
        return {
            "answer": (
                "Discuss is currently available only to company-wide operators. "
                "Employee-scoped retrieval is not enabled yet.\n\n"
                "Missing: permission-scoped retrieval for employee views."
            ),
            "sources": [],
            "citations_valid": True,
            "uncited_claims": [],
            "trust_metadata": {
                "confidence_gaps": ["Employee-scoped retrieval is not enabled yet."],
                "permission_denied": True,
            },
        }
    run_id = await trace.start_run(tenant_id=tenant_id, kind="chat")
    history_principals = _chat_message_principals(tenant_id, user_id, {"allowed_principals": [f"company:{tenant_id}"]})
    async with acquire() as conn:
        tenant_settings_raw = await conn.fetchval("SELECT settings FROM tenants WHERE id = $1", tenant_id)
        history_rows = await conn.fetch(
            """
            SELECT role, text FROM chat_messages
            WHERE tenant_id = $1 AND (user_id = $2 OR $2 IS NULL)
              AND acl_state = 'stamped'
              AND acl_freshness_state = 'fresh'
              AND allowed_principals ?| $3::text[]
            ORDER BY created_at DESC LIMIT 6
            """,
            tenant_id, user_id, history_principals,
        )
    tenant_settings = json.loads(tenant_settings_raw) if isinstance(tenant_settings_raw, str) else dict(tenant_settings_raw or {})
    if synthesis_paused(tenant_settings):
        async with trace.step(run_id, tenant_id, "synthesis_paused") as st:
            st.status = "skipped"
            st.detail = {"reason": "tenant pause_synthesis is enabled"}
        await trace.finish_run(run_id, status="paused")
        return {
            "answer": (
                "Discuss is paused for this workspace right now.\n\n"
                "Missing: synthesis must be unpaused before I can answer from company memory."
            ),
            "sources": [],
            "citations_valid": True,
            "uncited_claims": [],
            "trust_metadata": {
                "confidence_gaps": ["Tenant synthesis is paused."],
                "paused": True,
            },
            "pipeline_run_id": run_id,
        }
    history = "\n".join(f"{r['role']}: {r['text'][:400]}" for r in reversed(history_rows)) or "(none)"

    try:
        if _is_source_health_question(question):
            answer, trust_metadata, sources = await _source_health_answer(tenant_id)
            async with acquire() as conn:
                await _insert_chat_message(
                    conn,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    channel=channel,
                    role="user",
                    text=question,
                    pipeline_run_id=run_id,
                )
                message_id = str(uuid.uuid4())
                await _insert_chat_message(
                    conn,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    channel=channel,
                    role="assistant",
                    text=answer,
                    sources=sources,
                    pipeline_run_id=run_id,
                    trust_metadata=trust_metadata,
                    permission=trust_metadata.get("permission"),
                    message_id=message_id,
                )
            await trace.finish_run(run_id, status="source_health")
            return {
                "answer": answer,
                "sources": sources,
                "citations_valid": True,
                "uncited_claims": [],
                "unsupported_claims": [],
                "trust_metadata": trust_metadata,
                "chat_message_id": message_id,
                "pipeline_run_id": run_id,
            }

        broad_operating_question = _is_broad_operating_question_text(question)
        requested_sources = _requested_source_types(question)
        missing_sources = await _unavailable_requested_sources(
            tenant_id,
            requested_sources,
            _company_principals(tenant_id),
        )
        if missing_sources:
            source_list = ", ".join(sorted(missing_sources))
            answer = (
                f"I cannot verify that from {source_list} because the source is not connected or fresh enough.\n\n"
                f"Missing: connected, fresh {source_list} evidence for this question."
            )
            trust_metadata = {
                "citations_valid": True,
                "invalid_refs": [],
                "uncited_claims": [],
                "unsupported_claims": [],
                "confidence_gaps": [f"{source_list} is not connected or fresh enough."],
                "missing_requested_sources": sorted(missing_sources),
            }
            async with acquire() as conn:
                await _insert_chat_message(
                    conn,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    channel=channel,
                    role="user",
                    text=question,
                    pipeline_run_id=run_id,
                )
                message_id = str(uuid.uuid4())
                await _insert_chat_message(
                    conn,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    channel=channel,
                    role="assistant",
                    text=answer,
                    sources=[],
                    pipeline_run_id=run_id,
                    trust_metadata=trust_metadata,
                    permission=trust_metadata.get("permission"),
                    message_id=message_id,
                )
            await trace.finish_run(run_id, status="open_question")
            return {
                "answer": answer,
                "sources": [],
                "citations_valid": True,
                "uncited_claims": [],
                "unsupported_claims": [],
                "trust_metadata": trust_metadata,
                "chat_message_id": message_id,
                "pipeline_run_id": run_id,
            }

        okr_result = None
        if not requested_sources and is_okr_question(question):
            okr_result = okr_status_answer(
                question,
                await fetch_okr_state(tenant_id, include_private_contributions=False),
            )
        if okr_result:
            okr_result = _prepare_okr_chat_result(tenant_id, user_id, okr_result)
            async with acquire() as conn:
                await _insert_chat_message(
                    conn,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    channel=channel,
                    role="user",
                    text=question,
                    pipeline_run_id=run_id,
                )
                message_id = str(uuid.uuid4())
                await _insert_chat_message(
                    conn,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    channel=channel,
                    role="assistant",
                    text=okr_result["answer"],
                    sources=okr_result["sources"],
                    pipeline_run_id=run_id,
                    trust_metadata=okr_result["trust_metadata"],
                    permission=okr_result["trust_metadata"].get("permission"),
                    message_id=message_id,
                )
            await trace.finish_run(run_id, status="okr_status")
            return {
                "answer": okr_result["answer"],
                "sources": okr_result["sources"],
                "citations_valid": okr_result["trust_metadata"].get("citations_valid", False),
                "uncited_claims": okr_result["trust_metadata"].get("uncited_claims", []),
                "unsupported_claims": okr_result["trust_metadata"].get("unsupported_claims", []),
                "trust_metadata": okr_result["trust_metadata"],
                "chat_message_id": message_id,
                "pipeline_run_id": run_id,
            }

        workstream_result = None if requested_sources else await _workstream_status_answer(tenant_id, question)
        if workstream_result:
            async with acquire() as conn:
                await _insert_chat_message(
                    conn,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    channel=channel,
                    role="user",
                    text=question,
                    pipeline_run_id=run_id,
                )
                message_id = str(uuid.uuid4())
                await _insert_chat_message(
                    conn,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    channel=channel,
                    role="assistant",
                    text=workstream_result["answer"],
                    sources=workstream_result["sources"],
                    pipeline_run_id=run_id,
                    trust_metadata=workstream_result["trust_metadata"],
                    permission=workstream_result["trust_metadata"].get("permission"),
                    message_id=message_id,
                )
            await trace.finish_run(run_id, status="workstream_status")
            return {
                "answer": workstream_result["answer"],
                "sources": workstream_result["sources"],
                "citations_valid": True,
                "uncited_claims": [],
                "unsupported_claims": [],
                "trust_metadata": workstream_result["trust_metadata"],
                "chat_message_id": message_id,
                "pipeline_run_id": run_id,
            }

        async with trace.step(run_id, tenant_id, "context_retrieved") as st:
            retrieval_query = _retrieval_query_for_question(
                question,
                broad_operating_question=broad_operating_question,
                requested_sources=requested_sources,
            )
            pack = await build_context_pack(
                tenant_id=tenant_id,
                actor_user_id=user_id,
                target_audience="company",
                purpose="chat",
                requester_user_id=user_id,
                destination="ask_discuss",
                query=retrieval_query,
                recent_hours=72 if (not requested_sources or broad_operating_question) else None,
                source_types=requested_sources,
            )
            st.output_ref = pack["context_pack_id"]
            relevance_score = _context_relevance_score(question, pack["items"])
            st.detail = {
                "items": len(pack["items"]),
                "relevance_score": relevance_score,
                "broad_operating_question": broad_operating_question,
                "retrieval_query": retrieval_query,
                "requested_source_types": sorted(requested_sources),
                "permission": pack.get("permission"),
            }

        async with acquire() as conn:
            await _insert_chat_message(
                conn,
                tenant_id=tenant_id,
                user_id=user_id,
                channel=channel,
                role="user",
                text=question,
                pipeline_run_id=run_id,
            )

        if relevance_score < MIN_RELEVANT_CONTEXT_SCORE and not (broad_operating_question and pack["items"]):
            source_phrase = (
                " from " + ", ".join(sorted(requested_sources))
                if requested_sources else ""
            )
            answer = (
                f"I couldn't find company sources{source_phrase} relevant to that question.\n\n"
                f"Missing: source-backed{source_phrase} evidence that matches this question."
            )
            trust_metadata = {
                "citations_valid": True,
                "invalid_refs": [],
                "uncited_claims": [],
                "unsupported_claims": [],
                "confidence_gaps": ["No retrieved source was relevant enough to answer this question."],
                "low_relevance": True,
                "relevance_score": relevance_score,
                "requested_source_types": sorted(requested_sources),
                "permission": _pack_permission(pack),
            }
            async with trace.step(run_id, tenant_id, "output_created", input_ref=pack["context_pack_id"]) as st:
                message_id = str(uuid.uuid4())
                async with acquire() as conn:
                    await _insert_chat_message(
                        conn,
                        tenant_id=tenant_id,
                        user_id=user_id,
                        channel=channel,
                        role="assistant",
                        text=answer,
                        sources=[],
                        context_pack_id=pack["context_pack_id"],
                        pipeline_run_id=run_id,
                        trust_metadata=trust_metadata,
                        permission=trust_metadata.get("permission"),
                        message_id=message_id,
                    )
                st.status = "skipped"
                st.detail = {"reason": "low_relevance_context", "relevance_score": relevance_score}
                st.output_ref = message_id
            await trace.finish_run(run_id, status="open_question")
            return {
                "answer": answer,
                "sources": [],
                "citations_valid": True,
                "uncited_claims": [],
                "unsupported_claims": [],
                "trust_metadata": trust_metadata,
                "chat_message_id": message_id,
                "context_pack_id": pack["context_pack_id"],
                "pipeline_run_id": run_id,
            }

        if requested_sources:
            answer = _deterministic_context_answer(pack["items"])
            citation_check = verify_inline_citations(answer, pack["items"], require_relevance=True)
            answer, citation_check, sources, claim_gate = _apply_destination_claim_gate(answer, citation_check, pack)
            if not sources:
                answer = (
                    f"I found {', '.join(sorted(requested_sources))} connected, but could not verify an answer "
                    "from source-specific evidence for this question.\n\n"
                    f"Missing: source-backed {', '.join(sorted(requested_sources))} evidence that matches this question."
                )
                citation_check = {
                    "valid": True,
                    "cited": [],
                    "invalid_refs": [],
                    "uncited_claims": [],
                    "unsupported_claims": [],
                }
            trust_metadata = {
                "citations_valid": citation_check["valid"],
                "invalid_refs": citation_check["invalid_refs"],
                "uncited_claims": citation_check["uncited_claims"],
                "unsupported_claims": citation_check.get("unsupported_claims", []),
                "confidence_gaps": _confidence_gaps(answer, citation_check),
                "requested_source_types": sorted(requested_sources),
                "source_specific_answer": True,
                "blocked_permission_claims": claim_gate["blocked_claims"],
                "permission": _pack_permission(pack),
            }
            async with trace.step(run_id, tenant_id, "output_created", input_ref=pack["context_pack_id"]) as st:
                message_id = str(uuid.uuid4())
                async with acquire() as conn:
                    await _insert_chat_message(
                        conn,
                        tenant_id=tenant_id,
                        user_id=user_id,
                        channel=channel,
                        role="assistant",
                        text=answer,
                        sources=sources,
                        context_pack_id=pack["context_pack_id"],
                        pipeline_run_id=run_id,
                        trust_metadata=trust_metadata,
                        permission=trust_metadata.get("permission"),
                        message_id=message_id,
                    )
                st.detail = {"source_specific_answer": True, **citation_check}
                st.output_ref = message_id
            await trace.finish_run(run_id, status="source_specific")
            return {
                "answer": answer,
                "sources": sources,
                "citations_valid": citation_check["valid"],
                "uncited_claims": citation_check["uncited_claims"],
                "unsupported_claims": citation_check.get("unsupported_claims", []),
                "trust_metadata": trust_metadata,
                "chat_message_id": message_id,
                "context_pack_id": pack["context_pack_id"],
                "pipeline_run_id": run_id,
            }

        task_items = _task_planning_items(question, pack["items"])
        if _is_task_planning_question(question) and task_items:
            answer = _deterministic_task_planning_answer(task_items)
            citation_check = verify_inline_citations(answer, pack["items"], require_relevance=True)
            answer, citation_check, sources, claim_gate = _apply_destination_claim_gate(answer, citation_check, pack)
            if not citation_check["valid"] or _needs_repair(citation_check):
                answer = (
                    "I couldn't verify a task plan from the available company sources. "
                    "The source-backed task plan was withheld because one or more claims were missing usable citations.\n\n"
                    "Missing: concrete Linear task evidence and approval-path evidence for this question."
                )
                sources = []
            trust_metadata = {
                "citations_valid": citation_check["valid"],
                "invalid_refs": citation_check["invalid_refs"],
                "uncited_claims": citation_check["uncited_claims"],
                "unsupported_claims": citation_check.get("unsupported_claims", []),
                "confidence_gaps": _confidence_gaps(answer, citation_check),
                "read_only_task_plan": True,
                "blocked_permission_claims": claim_gate["blocked_claims"],
                "permission": _pack_permission(pack),
            }
            async with trace.step(run_id, tenant_id, "output_created", input_ref=pack["context_pack_id"]) as st:
                message_id = str(uuid.uuid4())
                async with acquire() as conn:
                    await _insert_chat_message(
                        conn,
                        tenant_id=tenant_id,
                        user_id=user_id,
                        channel=channel,
                        role="assistant",
                        text=answer,
                        sources=sources,
                        context_pack_id=pack["context_pack_id"],
                        pipeline_run_id=run_id,
                        trust_metadata=trust_metadata,
                        permission=trust_metadata.get("permission"),
                        message_id=message_id,
                    )
                st.detail = {"read_only_task_plan": True, **citation_check}
                st.output_ref = message_id
            await trace.finish_run(run_id, status="task_plan")
            return {
                "answer": answer,
                "sources": sources,
                "citations_valid": citation_check["valid"],
                "uncited_claims": citation_check["uncited_claims"],
                "unsupported_claims": citation_check.get("unsupported_claims", []),
                "trust_metadata": trust_metadata,
                "chat_message_id": message_id,
                "context_pack_id": pack["context_pack_id"],
                "pipeline_run_id": run_id,
            }

        async with trace.step(run_id, tenant_id, "synthesized", input_ref=pack["context_pack_id"]) as st:
            try:
                result = await call_llm(
                    task="chat",
                    system=SYSTEM,
                    prompt=PROMPT.format(context=pack["text"], history=history, question=question),
                    tenant_id=tenant_id,
                    tenant_settings=tenant_settings,
                    max_tokens=1500,
                    expect_json=False,
                    prompt_version=CHAT_PROMPT_VERSION,
                    context_pack_id=pack["context_pack_id"],
                    pipeline_run_id=run_id,
                )
                st.output_ref = result["model_call_id"]
            except (AnthropicError, ProviderCallError, BudgetExhausted) as e:
                result = None
                st.status = "degraded"
                st.detail = {
                    "reason": "llm_unavailable",
                    "failure_reason": getattr(e, "reason", None),
                    "error_type": type(e).__name__,
                    "error": str(e)[:500],
                }

        if result is None:
            answer = _deterministic_context_answer(pack["items"])
            citation_check = verify_inline_citations(answer, pack["items"], require_relevance=True)
            if citation_check["valid"]:
                answer, citation_check, sources, claim_gate = _apply_destination_claim_gate(answer, citation_check, pack)
            else:
                answer = (
                    "I couldn't verify an answer from the available company sources. "
                    "The model provider was unavailable and the source fallback did not produce usable citations.\n\n"
                    "Missing: a working model provider or stronger source-backed evidence for this question."
                )
                sources = []
                citation_check = {
                    "valid": False,
                    "cited": [],
                    "invalid_refs": citation_check.get("invalid_refs", []),
                    "uncited_claims": citation_check.get("uncited_claims", []),
                    "unsupported_claims": citation_check.get("unsupported_claims", []),
                }
                claim_gate = {"blocked_claims": []}
            trust_metadata = {
                "citations_valid": citation_check["valid"],
                "invalid_refs": citation_check["invalid_refs"],
                "uncited_claims": citation_check["uncited_claims"],
                "unsupported_claims": citation_check.get("unsupported_claims", []),
                "confidence_gaps": [
                    *_confidence_gaps(answer, citation_check),
                    "Model provider unavailable; answered from retrieved sources only.",
                ][:8],
                "llm_degraded": True,
                "blocked_permission_claims": claim_gate["blocked_claims"],
                "permission": _pack_permission(pack),
            }
            async with trace.step(run_id, tenant_id, "output_created", input_ref=pack["context_pack_id"]) as st:
                message_id = str(uuid.uuid4())
                async with acquire() as conn:
                    await _insert_chat_message(
                        conn,
                        tenant_id=tenant_id,
                        user_id=user_id,
                        channel=channel,
                        role="assistant",
                        text=answer,
                        sources=sources,
                        context_pack_id=pack["context_pack_id"],
                        pipeline_run_id=run_id,
                        trust_metadata=trust_metadata,
                        permission=trust_metadata.get("permission"),
                        message_id=message_id,
                    )
                st.status = "degraded"
                st.detail = {"reason": "llm_unavailable_deterministic_fallback"}
                st.output_ref = message_id
            await trace.finish_run(run_id, status="degraded")
            return {
                "answer": answer,
                "sources": sources,
                "citations_valid": citation_check["valid"],
                "uncited_claims": citation_check["uncited_claims"],
                "unsupported_claims": citation_check.get("unsupported_claims", []),
                "trust_metadata": trust_metadata,
                "chat_message_id": message_id,
                "context_pack_id": pack["context_pack_id"],
                "pipeline_run_id": run_id,
            }

        async with trace.step(run_id, tenant_id, "verified", input_ref=result["model_call_id"]) as st:
            answer = normalize_citations(result["text"].strip(), pack["items"])
            citation_check = verify_inline_citations(answer, pack["items"], require_relevance=True)
            if _needs_repair(citation_check):
                initial_check = citation_check
                repair = await call_llm(
                    task="chat",
                    system=SYSTEM,
                    prompt=REPAIR_PROMPT.format(
                        context=pack["text"],
                        findings=_repair_findings(citation_check),
                        question=question,
                    ),
                    tenant_id=tenant_id,
                    tenant_settings=tenant_settings,
                    max_tokens=1500,
                    expect_json=False,
                    prompt_version=f"{CHAT_PROMPT_VERSION}-repair",
                    context_pack_id=pack["context_pack_id"],
                    pipeline_run_id=run_id,
                )
                repaired_answer = normalize_citations(repair["text"].strip(), pack["items"])
                repaired_check = verify_inline_citations(repaired_answer, pack["items"], require_relevance=True)
                if not _needs_repair(repaired_check):
                    answer = repaired_answer
                    citation_check = repaired_check
                    st.detail = {"repaired": True, **repaired_check}
                    st.output_ref = repair["model_call_id"]
                else:
                    deterministic_answer = _deterministic_context_answer(pack["items"])
                    deterministic_check = verify_inline_citations(
                        deterministic_answer,
                        pack["items"],
                        require_relevance=True,
                    )
                    if not _needs_repair(deterministic_check):
                        answer = deterministic_answer
                        citation_check = deterministic_check
                        st.detail = {
                            "repaired": False,
                            "deterministic_fallback": True,
                            "initial": initial_check,
                            "repair": repaired_check,
                            "fallback": deterministic_check,
                        }
                    else:
                        st.detail = {"repaired": False, "initial": initial_check, "repair": repaired_check, "fallback": deterministic_check}
            else:
                st.detail = citation_check
            answer, citation_check, sources, claim_gate = _apply_destination_claim_gate(answer, citation_check, pack)

        if not citation_check["valid"] or _needs_repair(citation_check):
            answer = (
                "I couldn't verify an answer from the available company sources. "
                "The model output was withheld because one or more claims were missing usable citations.\n\n"
                "Missing: stronger source-backed evidence for this question."
            )
            sources = []
            citation_check = {
                "valid": False,
                "cited": [],
                "invalid_refs": [],
                "uncited_claims": [],
                "unsupported_claims": [],
            }
            claim_gate = {"blocked_claims": []}

        trust_metadata = {
            "citations_valid": citation_check["valid"],
            "invalid_refs": citation_check["invalid_refs"],
            "uncited_claims": citation_check["uncited_claims"],
            "unsupported_claims": citation_check.get("unsupported_claims", []),
            "confidence_gaps": _confidence_gaps(answer, citation_check),
            "blocked_permission_claims": claim_gate["blocked_claims"],
            "permission": _pack_permission(pack),
        }

        async with trace.step(run_id, tenant_id, "output_created", input_ref=result["model_call_id"]) as st:
            message_id = str(uuid.uuid4())
            async with acquire() as conn:
                await _insert_chat_message(
                    conn,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    channel=channel,
                    role="assistant",
                    text=answer,
                    sources=sources,
                    context_pack_id=pack["context_pack_id"],
                    model_call_id=result["model_call_id"],
                    pipeline_run_id=run_id,
                    trust_metadata=trust_metadata,
                    permission=trust_metadata.get("permission"),
                    message_id=message_id,
                )
            st.output_ref = message_id

        await trace.finish_run(run_id)
        return {
            "answer": answer,
            "sources": sources,
            "citations_valid": citation_check["valid"],
            "uncited_claims": citation_check["uncited_claims"],
            "unsupported_claims": citation_check.get("unsupported_claims", []),
            "trust_metadata": trust_metadata,
            "chat_message_id": message_id,
            "context_pack_id": pack["context_pack_id"],
            "pipeline_run_id": run_id,
        }
    except Exception as e:
        await trace.finish_run(run_id, status="failed", error=f"{type(e).__name__}: {e}")
        raise


def _chat_message_principals(
    tenant_id: str,
    user_id: str | None,
    permission: dict | None = None,
) -> list[str]:
    principals = [
        str(principal)
        for principal in (permission or {}).get("allowed_principals", [])
        if principal
    ]
    if user_id:
        principals.append(f"user:{user_id}")
    if not principals:
        principals.append(f"company:{tenant_id}")
    return list(dict.fromkeys(principals))


async def _insert_chat_message(
    conn,
    *,
    tenant_id: str,
    user_id: str | None,
    channel: str,
    role: str,
    text: str,
    pipeline_run_id: str | None,
    message_id: str | None = None,
    sources: list[dict] | None = None,
    context_pack_id: str | None = None,
    model_call_id: str | None = None,
    trust_metadata: dict | None = None,
    permission: dict | None = None,
) -> str:
    message_id = message_id or str(uuid.uuid4())
    principals = _chat_message_principals(tenant_id, user_id, permission)
    visibility = "company" if f"company:{tenant_id}" in principals else "private"
    await conn.execute(
        """
        INSERT INTO chat_messages
            (id, tenant_id, user_id, channel, role, text, sources,
             context_pack_id, model_call_id, pipeline_run_id, trust_metadata,
             visibility, acl_basis, acl_confidence, allowed_principals,
             acl_state, acl_freshness_state)
        VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb,
                $8, $9, $10, $11::jsonb,
                $12, 'chat_audience', 'medium', $13::jsonb,
                'stamped', 'fresh')
        """,
        message_id,
        tenant_id,
        user_id,
        channel,
        role,
        text,
        json.dumps(sources or []),
        context_pack_id,
        model_call_id,
        pipeline_run_id,
        json.dumps(trust_metadata or {}),
        visibility,
        json.dumps(principals),
    )
    return message_id


def _resolve_sources(cited_refs: list[str], pack_items: list[dict], *, pack: dict | None = None) -> list[dict]:
    by_ref = {f"{i['kind']}:{i['id']}": i for i in pack_items}
    out, seen = [], set()
    for ref in cited_refs:
        if ref in seen or ref not in by_ref:
            continue
        seen.add(ref)
        item = by_ref[ref]
        source = {
            "ref": ref,
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
        out.append(source)
    if not pack:
        return out
    permission = pack.get("permission") or {}
    destination_principals = permission.get("allowed_principals")
    check = verify_destination_visibility(sources=out, destination_principals=destination_principals)
    if check["ok"]:
        return out
    blocked = set(check["blocked_refs"])
    return [
        source for source in out
        if (source.get("ref") or source.get("source_url") or source.get("source_id") or "(unknown)") not in blocked
    ]


def _prepare_okr_chat_result(tenant_id: str, user_id: str | None, okr_result: dict) -> dict:
    permission = {
        "policy_version": RETRIEVAL_POLICY_VERSION,
        "actor_user_id": user_id,
        "requester_user_id": user_id,
        "target_audience": "company",
        "destination": "ask_discuss",
        "purpose": "chat",
        "allowed_principals": _company_principals(tenant_id),
    }
    pack = {
        "items": [_okr_context_item_with_acl(item, permission["allowed_principals"]) for item in okr_result.get("sources") or []],
        "permission": permission,
    }
    citation_check = verify_inline_citations(okr_result.get("answer") or "", pack["items"], require_relevance=True)
    claim_gate = {"blocked_claims": []}
    answer = okr_result.get("answer") or ""
    sources: list[dict] = []
    if citation_check.get("valid") and not _needs_repair(citation_check):
        answer, citation_check, sources, claim_gate = _apply_destination_claim_gate(answer, citation_check, pack)
    if not citation_check.get("valid") or _needs_repair(citation_check):
        answer = (
            "I couldn't verify an OKR answer from sources visible to this destination.\n\n"
            "Missing: destination-visible OKR evidence for this answer."
        )
        sources = []

    trust_metadata = {
        **(okr_result.get("trust_metadata") or {}),
        "citations_valid": citation_check.get("valid", False),
        "invalid_refs": citation_check.get("invalid_refs", []),
        "uncited_claims": citation_check.get("uncited_claims", []),
        "unsupported_claims": citation_check.get("unsupported_claims", []),
        "blocked_permission_claims": claim_gate["blocked_claims"],
        "permission": _pack_permission(pack),
    }
    return {
        **okr_result,
        "answer": answer,
        "sources": sources,
        "trust_metadata": trust_metadata,
    }


def _okr_context_item_with_acl(item: dict, allowed_principals: list[str]) -> dict:
    stamped = dict(item)
    stamped.setdefault("visibility", "company")
    stamped.setdefault("allowed_principals", allowed_principals)
    stamped.setdefault("acl_state", "stamped")
    stamped.setdefault("acl_freshness_state", "fresh")
    return stamped


def _apply_destination_claim_gate(answer: str, citation_check: dict, pack: dict) -> tuple[str, dict, list[dict], dict]:
    if not citation_check.get("valid") or _needs_repair(citation_check):
        return answer, citation_check, [], {"blocked_claims": []}

    permission = pack.get("permission") or {}
    all_sources = _resolve_sources(citation_check.get("cited") or [], pack["items"], pack=None)
    claim_gate = filter_claim_text_for_destination(
        answer,
        all_sources,
        permission.get("allowed_principals"),
    )
    if not claim_gate["blocked_claims"]:
        return answer, citation_check, _resolve_sources(citation_check.get("cited") or [], pack["items"], pack=pack), claim_gate

    if claim_gate["text"]:
        answer = claim_gate["text"]
        citation_check = verify_inline_citations(answer, pack["items"], require_relevance=True)
        if citation_check.get("valid") and not _needs_repair(citation_check):
            sources = _resolve_sources(citation_check.get("cited") or [], pack["items"], pack=pack)
            return answer, citation_check, sources, claim_gate

    answer = (
        "I couldn't verify an answer from sources visible to this destination.\n\n"
        "Missing: destination-visible evidence for this answer."
    )
    citation_check = {
        "valid": True,
        "cited": [],
        "invalid_refs": [],
        "uncited_claims": [],
        "unsupported_claims": [],
    }
    return answer, citation_check, [], claim_gate


def _pack_permission(pack: dict) -> dict:
    permission = pack.get("permission") or {}
    return {
        "policy_version": permission.get("policy_version"),
        "actor_user_id": permission.get("actor_user_id"),
        "requester_user_id": permission.get("requester_user_id"),
        "target_audience": permission.get("target_audience"),
        "destination": permission.get("destination"),
        "purpose": permission.get("purpose"),
        "allowed_principals": permission.get("allowed_principals") or [],
    }


def _company_principals(tenant_id: str) -> list[str]:
    return [f"company:{tenant_id}"]


def _needs_repair(citation_check: dict) -> bool:
    return bool(
        citation_check.get("uncited_claims")
        or citation_check.get("unsupported_claims")
        or citation_check.get("invalid_refs")
        or not citation_check.get("valid")
    )


def _repair_findings(citation_check: dict) -> str:
    findings: list[str] = []
    for key in ("uncited_claims", "unsupported_claims", "invalid_refs"):
        values = citation_check.get(key) or []
        for value in values[:5]:
            findings.append(f"- {key}: {strip_citations(str(value))}")
    return "\n".join(findings) or "- citation validation failed"


def _deterministic_context_answer(pack_items: list[dict]) -> str:
    lines: list[str] = []
    for item in pack_items[:4]:
        kind = item.get("kind")
        item_id = item.get("id")
        text = _compact_source_text(item.get("text") or "")
        if not kind or not item_id or not text:
            continue
        lines.append(f"- {text} [ref:{kind}:{item_id}]")
    if not lines:
        return (
            "I couldn't verify an answer from the available company sources.\n\n"
            "Missing: source-backed company activity relevant to this question."
        )
    return "\n".join(lines) + "\n\nSources used:\nMissing: additional connected sources may improve this answer."


def _is_task_planning_question(question: str) -> bool:
    q = question.lower()
    has_task_key = bool(re.search(r"\b[A-Z]{1,8}-\d+\b", question, flags=re.IGNORECASE))
    task_terms = ("wg-", "linear", "issue", "task", "ticket", "poc", "playbook")
    planning_terms = ("tackle", "plan", "execute", "draft", "what should", "how should", "next move", "what to do")
    return (has_task_key or any(term in q for term in task_terms)) and any(term in q for term in planning_terms)


def _requested_task_keys(question: str) -> set[str]:
    keys = {match.lower() for match in re.findall(r"\b[A-Z]{1,8}-\d+\b", question, flags=re.IGNORECASE)}
    q = question.lower()
    if "poc" in q or "playbook" in q:
        keys.update({"poc", "playbook"})
    return keys


def _task_planning_items(question: str, pack_items: list[dict]) -> list[dict]:
    keys = _requested_task_keys(question)
    if not keys:
        return []
    direct = []
    for item in pack_items:
        text = (item.get("text") or "").lower()
        source_type = str(item.get("source_type") or "").lower()
        source_url = str(item.get("source_url") or "").lower()
        is_linear_issue = (
            "linear issue" in text
            or source_type == "linear"
            or "linear.app" in source_url
            or any(f"/issue/{key}" in source_url for key in keys)
        )
        if any(key in text or f"/issue/{key}" in source_url for key in keys) and is_linear_issue:
            direct.append(item)
    if not direct:
        return []
    out = list(direct)
    for item in pack_items:
        if item in out:
            continue
        text = (item.get("text") or "").lower()
        source_url = str(item.get("source_url") or "").lower()
        if any(key in text or f"/issue/{key}" in source_url for key in keys):
            out.append(item)
    return out


def _task_ref(item: dict) -> str:
    return f"[ref:{item['kind']}:{item['id']}]"


def _first_task_item(pack_items: list[dict], *needles: str) -> dict | None:
    for item in pack_items:
        text = (item.get("text") or "").lower()
        if any(needle.lower() in text for needle in needles):
            return item
    return None


def _deterministic_task_planning_answer(pack_items: list[dict]) -> str:
    linear = _linear_task_item(pack_items) or pack_items[0]
    fields = _linear_task_fields(linear)
    related = [item for item in pack_items if item is not linear]
    approval = _first_task_item(related, "shruti", "approval", "approved", "comment") or linear
    artifact = _first_task_item(related, "drive", "doc", "artifact", "draft", "playbook") or linear
    dependency = _first_task_item(related, "dependency", "blocked", "blocker", "customer", "pilot", "poc") or linear

    key = fields.get("identifier") or _task_key_from_text(linear.get("text") or "") or "the Linear issue"
    title = fields.get("title") or "the work described in Linear"
    state = fields.get("state") or "unknown"
    assignee = fields.get("assignee") or "unknown"
    project = fields.get("project") or "unknown"
    priority = fields.get("priority") or "unknown"
    due_date = fields.get("due_date") or "unknown"
    description = fields.get("description")

    linear_ref = _task_ref(linear)
    approval_ref = _task_ref(approval)
    artifact_ref = _task_ref(artifact)
    dependency_ref = _task_ref(dependency)
    missing = _linear_task_missing_fields(fields)
    enough_detail_for_steps = bool(description) or artifact is not linear
    approval_has_source = approval is not linear
    if description:
        why_now = f"the task description says {description[:180].rstrip('.')}."
    elif priority != "unknown" or due_date != "unknown":
        why_now = f"priority is {priority} and due date is {due_date}."
    else:
        why_now = "the sources do not include priority or due-date evidence."

    if description:
        plan_steps = _task_plan_steps(title, description)
        draft_shape = "not enough evidence to recommend an artifact shape; ask Michelle what format she wants"
    elif artifact is not linear:
        plan_steps = _compact_source_text(artifact.get("text") or "")
        draft_shape = "not enough evidence to recommend an artifact shape; ask Michelle what format she wants"
    elif enough_detail_for_steps:
        plan_steps = _task_plan_steps(title, description)
        draft_shape = "not enough evidence to recommend an artifact shape; ask Michelle what format she wants"
    else:
        plan_steps = "the sources do not include enough detail to recommend execution steps"
        draft_shape = "the sources do not include enough detail to recommend a draft artifact"
    execution_ref = linear_ref if description else artifact_ref
    related_summary = _compact_source_text(dependency.get("text") or "")
    missing_line = "; ".join(missing) if missing else "nothing material"
    if approval_has_source:
        approval_line = f"Approval boundary: {_compact_source_text(approval.get('text') or '')} {approval_ref}"
    else:
        approval_line = "Approval boundary: no approval-path source was retrieved; keep this read-only until Michelle approves."
    known_parts = [f"{key} is a Linear task titled {title.rstrip('.')}"]
    if state != "unknown":
        known_parts.append(f"state is {state}")
    if assignee != "unknown":
        known_parts.append(f"assignee is {assignee}")
    if project != "unknown":
        known_parts.append(f"project is {project}")
    if priority != "unknown":
        known_parts.append(f"priority is {priority}")
    if due_date != "unknown":
        known_parts.append(f"due date is {due_date}")
    summary = ", ".join(known_parts)
    if why_now == "the sources do not include priority or due-date evidence.":
        why_now_line = f"- Why now: {why_now}"
    else:
        why_now_line = f"- Why now: {why_now} {linear_ref}"
    if plan_steps == "the sources do not include enough detail to recommend execution steps":
        plan_steps_line = f"- Execution steps: {plan_steps}"
    else:
        plan_steps_line = f"- Execution steps: {plan_steps} {execution_ref}"
    return (
        f"{summary}. {linear_ref}\n"
        f"{why_now_line}\n"
        f"- Related context: {related_summary} {dependency_ref}\n"
        f"{plan_steps_line}\n"
        f"- Draft artifact: {draft_shape}.\n"
        f"- {approval_line}\n\n"
        "Sources used:\n"
        f"Missing: {missing_line}."
    )


def _linear_task_item(pack_items: list[dict]) -> dict | None:
    for item in pack_items:
        text = (item.get("text") or "").lower()
        source_type = str(item.get("source_type") or "").lower()
        source_url = str(item.get("source_url") or "").lower()
        if "linear issue" in text or source_type == "linear" or "linear.app" in source_url:
            return item
    return None


def _linear_task_fields(item: dict) -> dict[str, str]:
    text = item.get("text") or ""
    fields: dict[str, str] = {}
    first_line = text.splitlines()[0] if text.splitlines() else text
    match = re.search(r"Linear issue\s+([A-Z]{1,8}-\d+)\s+is titled\s+(.+?)(?:\.|$)", text, re.IGNORECASE)
    if not match:
        match = re.search(r"Linear issue:?\s+([A-Z]{1,8}-\d+)\s+(.+)", first_line, re.IGNORECASE)
    if match:
        fields["identifier"] = match.group(1).upper()
        fields["title"] = match.group(2).strip().rstrip(".")
    elif key := _task_key_from_text(text):
        fields["identifier"] = key

    patterns = {
        "state": r"^State:\s*(.+)$",
        "assignee": r"^Assignee:\s*(.+)$",
        "team": r"^Team:\s*(.+)$",
        "project": r"^Project:\s*(.+)$",
        "priority": r"^Priority:\s*(.+)$",
        "due_date": r"^Due date:\s*(.+)$",
        "description": r"^Description:\s*(.+)$",
    }
    for key, pattern in patterns.items():
        value = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
        if value:
            fields[key] = value.group(1).strip()
    return fields


def _task_key_from_text(text: str) -> str | None:
    match = re.search(r"\b([A-Z]{1,8}-\d+)\b", text or "", re.IGNORECASE)
    return match.group(1).upper() if match else None


def _linear_task_missing_fields(fields: dict[str, str]) -> list[str]:
    labels = {
        "state": "Linear state",
        "assignee": "assignee",
        "priority": "priority",
        "due_date": "due date",
        "project": "project/workstream",
        "description": "task description",
    }
    return [f"{label} is missing from the retrieved Linear source" for key, label in labels.items() if not fields.get(key)]


def _task_plan_steps(title: str, description: str | None) -> str:
    if description:
        return description.rstrip(".")
    lower = f"{title} {description or ''}".lower()
    if "playbook" in lower or "poc" in lower:
        return (
            "draft the POC stages, write success criteria, list owner handoffs, add cadence and escalation paths, "
            "then prepare the smallest reviewable version for Shruti"
        )
    if "customer" in lower or "pilot" in lower:
        return (
            "identify the customer commitment, confirm the owner, list blockers, draft the customer-safe update, "
            "then ask for approval before sending or changing source systems"
        )
    return (
        "confirm the desired outcome, identify missing owner/deadline/blocker evidence, draft the next action, "
        "then ask for approval before changing any source system"
    )


def _task_draft_shape(title: str, description: str | None) -> str:
    lower = f"{title} {description or ''}".lower()
    if "playbook" in lower or "poc" in lower:
        return "sections for entry criteria, kickoff, customer setup, weekly cadence, success metrics, escalation paths, and closeout"
    if "customer" in lower or "pilot" in lower:
        return "a short status note with current state, blocker, owner, customer impact, decision needed, and proposed customer message"
    return "a concise task note with source-backed goal, current state, blocker, owner, next step, and approval request"


def _meaningful_terms(text: str) -> set[str]:
    terms = set()
    for token in re.findall(r"[a-zA-Z][a-zA-Z0-9_'-]{2,}", text.lower()):
        token = token.strip("'")
        if token.endswith("'s"):
            token = token[:-2]
        if token in _STOPWORDS:
            continue
        if token.endswith("s") and len(token) > 4:
            token = token[:-1]
        terms.add(token)
    return terms


def _context_relevance_score(question: str, pack_items: list[dict]) -> float:
    question_terms = _meaningful_terms(question)
    if not question_terms:
        return 1.0 if pack_items else 0.0
    context_terms: set[str] = set()
    for item in pack_items[:12]:
        context_terms.update(_meaningful_terms(item.get("text") or ""))
    if not context_terms:
        return 0.0
    overlap = question_terms & context_terms
    return len(overlap) / len(question_terms)


def _retrieval_query_for_question(
    question: str,
    *,
    broad_operating_question: bool,
    requested_sources: set[str],
) -> str:
    if broad_operating_question and not requested_sources:
        return "project commitment blocker risk changed update focus today"
    if requested_sources:
        source_words = {word for word, source in _SOURCE_ALIASES.items() if source in requested_sources}
        terms = [
            term for term in sorted(_meaningful_terms(question))
            if term not in source_words and term not in requested_sources
        ]
        if terms:
            return " ".join(terms)
    return question


def _requested_source_types(question: str) -> set[str]:
    words = set(re.findall(r"[a-zA-Z][a-zA-Z0-9_-]+", question.lower()))
    return {source for word, source in _SOURCE_ALIASES.items() if word in words}


def _is_source_health_question(question: str) -> bool:
    q = question.lower()
    return any(word in q for word in ("source", "sources", "connected", "connection", "fresh", "stale", "missing")) and any(
        word in q for word in ("stale", "missing", "fresh", "connected", "coverage", "health", "sync", "synced")
    )


def _source_health_time(value) -> str:
    if not value:
        return "no data yet"
    return value.strftime("%b %-d, %-I:%M %p %Z")


def _source_health_policy(row) -> dict:
    raw = row["sync_policy"]
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:
            return {}
    return dict(raw or {})


def _source_health_recovered_after_import_failure(row) -> bool:
    if row["last_backfill_status"] != "failed":
        return False
    if str(row["status"]) != "live":
        return False
    if row["stale_since"] or row["degraded_reason"]:
        return False
    policy = _source_health_policy(row)
    if policy.get("trigger_errors"):
        return False
    return bool(row["fresh_source_at"])


async def _source_health_answer(tenant_id: str) -> tuple[str, dict, list[dict]]:
    principals = _company_principals(tenant_id)
    async with acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT s.id, s.source_type, s.status, s.sync_policy,
                   GREATEST(s.last_webhook_at, s.last_synced_at) AS fresh_source_at,
                   GREATEST(s.last_webhook_at, s.last_synced_at, backfill.finished_at) AS last_data_at,
                   backfill.status AS last_backfill_status, s.stale_since, s.degraded_reason
            FROM sources s
            LEFT JOIN LATERAL (
                SELECT status, finished_at
                FROM source_sync_runs
                WHERE source_id = s.id AND kind = 'backfill'
                ORDER BY started_at DESC LIMIT 1
            ) backfill ON true
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
    by_type = {str(r["source_type"]): r for r in rows}
    lines = ["Source health"]
    gaps: list[str] = []
    sources: list[dict] = []
    for source_type, label in _MVP_SOURCE_LABELS.items():
        row = by_type.get(source_type)
        if not row:
            lines.append(f"- {label}: missing.")
            gaps.append(f"{label} is not connected.")
            continue
        status = str(row["status"])
        last_data = _source_health_time(row["last_data_at"])
        recovered_import = _source_health_recovered_after_import_failure(row)
        backfill = "completed" if recovered_import else (row["last_backfill_status"] or "not started")
        stale = f", stale since {_source_health_time(row['stale_since'])}" if row["stale_since"] else ""
        reason = ", source details require source access" if row["degraded_reason"] else ""
        lines.append(f"- {label}: {status}, last data {last_data}, backfill {backfill}{stale}{reason}.")
        sources.append({
            "ref": f"source:{row['id']}",
            "source_url": None,
            "source_title": f"Source health · {label}",
            "source_type": row["source_type"],
            "source_id": str(row["id"]),
            "source_external_id": None,
            "source_identity_hints": [],
            "source_observed_at": row["last_data_at"].isoformat() if row["last_data_at"] else None,
            "visibility": "company",
            "allowed_principals": principals,
            "acl_state": "stamped",
            "acl_freshness_state": "fresh",
        })
        if row["stale_since"] or status in {"degraded", "expired", "failed"}:
            gaps.append(f"{label} is {status}.")
    return "\n".join(lines) + "\n\nSources used:\nMissing: nothing material.", {
        "citations_valid": True,
        "invalid_refs": [],
        "uncited_claims": [],
        "unsupported_claims": [],
        "confidence_gaps": gaps[:8],
        "system_source_health": True,
        "permission": {
            "policy_version": "permissioned-company-brain-v1",
            "actor_user_id": None,
            "requester_user_id": None,
            "target_audience": "company",
            "destination": "ask_discuss",
            "purpose": "chat_source_health",
            "allowed_principals": principals,
        },
    }, sources


async def _workstream_status_answer(tenant_id: str, question: str) -> dict | None:
    if not _is_workstream_status_question(question):
        return None
    workstreams = await _tenant_workstreams(tenant_id)
    if not workstreams:
        return None
    selected = _select_workstream(question, workstreams)
    if not selected:
        return None
    sources = _workstream_sources(tenant_id, selected)
    answer = _format_workstream_answer(selected)
    gaps = list(selected.get("missing_evidence") or [])
    if selected.get("missing_source_time_count"):
        gaps.append(f"{selected['missing_source_time_count']} update is missing an evidence timestamp.")
    if selected.get("stale_source_count"):
        gaps.append(f"{selected['stale_source_count']} source-backed signal is stale.")
    if not sources:
        gaps.append("No openable source link is attached to this workstream yet.")
    return {
        "answer": answer,
        "sources": sources,
        "trust_metadata": {
            "citations_valid": True,
            "invalid_refs": [],
            "uncited_claims": [],
            "unsupported_claims": [],
            "confidence_gaps": _dedupe(gaps)[:8],
            "system_workstream_answer": True,
            "workstream_key": selected.get("key"),
            "workstream_delta": selected.get("delta"),
            "workstream_status": selected.get("status"),
            "permission": {
                "policy_version": "permissioned-company-brain-v1",
                "actor_user_id": None,
                "requester_user_id": None,
                "target_audience": "company",
                "destination": "ask_discuss",
                "purpose": "chat_workstream_status",
                "allowed_principals": _company_principals(tenant_id),
            },
        },
    }


async def _tenant_workstreams(tenant_id: str) -> list[dict]:
    principals = _company_principals(tenant_id)
    muted = await muted_workstream_keys(tenant_id)
    muted_types = await muted_drift_types(tenant_id)
    async with acquire() as conn:
        return await load_company_workstreams(
            conn,
            tenant_id=tenant_id,
            principals=principals,
            limit=100,
            muted_workstream_keys=muted,
            muted_drift_types=muted_types,
        )


def _is_workstream_status_question(question: str) -> bool:
    q = question.lower()
    status_terms = (
        "latest",
        "status",
        "blocked",
        "blocker",
        "risk",
        "at risk",
        "on track",
        "changed",
        "what happened",
        "what's up",
        "whats up",
    )
    work_terms = ("project", "commitment", "workstream", "launch", "accuracy", "api credentials", "customer")
    return any(term in q for term in status_terms) and any(term in q for term in work_terms)


def _select_workstream(question: str, workstreams: list[dict]) -> dict | None:
    question_terms = _meaningful_terms(question)
    best: tuple[float, dict] | None = None
    for workstream in workstreams:
        haystack = " ".join(
            str(value or "")
            for value in (
                workstream.get("title"),
                workstream.get("about"),
                workstream.get("kind"),
                workstream.get("latest_change"),
                workstream.get("next_action"),
            )
        )
        terms = _meaningful_terms(haystack)
        if not terms:
            continue
        overlap = question_terms & terms
        title_hit = str(workstream.get("title") or "").lower() in question.lower()
        score = len(overlap) / max(len(question_terms), 1)
        if title_hit:
            score += 1.0
        if best is None or score > best[0]:
            best = (score, workstream)
    if not best or best[0] < 0.2:
        return None
    return best[1]


def _workstream_sources(tenant_id: str, workstream: dict) -> list[dict]:
    sources = []
    allowed_principals = _company_principals(tenant_id)
    for chip in workstream.get("source_chips") or []:
        if not chip.get("url"):
            continue
        source = {
            "ref": chip.get("ref"),
            "source_url": chip.get("url"),
            "source_title": chip.get("label"),
            "source_type": chip.get("label"),
            "source_observed_at": workstream.get("last_source_observed_at"),
            "visibility": chip.get("visibility"),
            "allowed_principals": chip.get("allowed_principals") or [],
            "acl_state": chip.get("acl_state"),
            "acl_freshness_state": chip.get("acl_freshness_state"),
        }
        if not source_visible_to_principals(source, allowed_principals):
            continue
        sources.append(source)
        if len(sources) >= 6:
            break
    return sources


def _format_workstream_answer(workstream: dict) -> str:
    status = _human_label(workstream.get("status") or "watching")
    delta = _human_label(workstream.get("delta") or "still_open")
    lines = [
        f"## {workstream.get('title') or 'Workstream'}",
        f"- Status: {status}.",
        f"- Delta: {delta}. {workstream.get('delta_reason') or 'No delta reason available.'}",
        f"- What changed: {workstream.get('latest_change') or 'No recent source-backed change text yet.'}",
        f"- Next action: {workstream.get('next_action') or 'No source-backed next action suggested yet.'}",
    ]
    if workstream.get("status_reason"):
        lines.append(f"- Why this status: {workstream['status_reason']}")
    if workstream.get("last_source_observed_at"):
        lines.append(f"- Evidence freshness: latest source observed at {workstream['last_source_observed_at']}.")
    else:
        lines.append("- Evidence freshness: no source timestamp is available.")
    people = workstream.get("people_rows") or []
    if people:
        first = people[0]
        lines.append(
            f"- People: {first.get('person') or 'Unknown'} — "
            f"{first.get('contribution') or 'no source-backed contribution yet'}"
        )
    missing = workstream.get("missing_evidence") or []
    if missing:
        lines.append(f"Missing: {missing[0]}")
    else:
        lines.append("Missing: nothing material.")
    return "\n".join(lines)


def _human_label(value: str) -> str:
    return str(value or "").replace("_", " ").capitalize()


def _dedupe(values: list[str]) -> list[str]:
    out = []
    for value in values:
        if value and value not in out:
            out.append(value)
    return out


async def _unavailable_requested_sources(
    tenant_id: str,
    requested: set[str],
    destination_principals: list[str] | None = None,
) -> set[str]:
    if not requested:
        return set()
    principals = destination_principals or _company_principals(tenant_id)
    async with acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT DISTINCT s.source_type
            FROM sources s
            WHERE s.tenant_id = $1
              AND s.source_type = ANY($2::text[])
              AND s.status IN ('live', 'connected')
              AND s.stale_since IS NULL
              AND EXISTS (
                  SELECT 1
                  FROM source_resources sr
                  WHERE sr.tenant_id = s.tenant_id
                    AND sr.source_id = s.id
                    AND sr.source_type = s.source_type
                    AND sr.acl_state = 'stamped'
                    AND sr.acl_freshness_state = 'fresh'
                    AND sr.allowed_principals ?| $3::text[]
              )
            """,
            tenant_id,
            sorted(requested),
            principals,
        )
    available = {str(r["source_type"]) for r in rows}
    return set(requested) - available


def _is_broad_operating_question_text(question: str) -> bool:
    q = question.lower()
    broad_phrases = [
        "what changed",
        "changed overnight",
        "overnight",
        "what's new",
        "what is new",
        "what should i focus",
        "focus today",
        "what is blocked",
        "what's blocked",
        "at risk",
    ]
    return any(phrase in q for phrase in broad_phrases)


def _is_broad_operating_question(question: str, pack_items: list[dict]) -> bool:
    return bool(pack_items) and _is_broad_operating_question_text(question)


def _compact_source_text(text: str) -> str:
    clean = " ".join((text or "").replace("\n", " ").split())
    clean = re.sub(r"\[ref:[a-z]+:[A-Za-z0-9_.:-]+\]", "", clean).strip()
    clean = _humanize_context_text(clean)
    if len(clean) <= 220:
        return clean
    return clean[:217].rstrip() + "..."


def _humanize_context_text(text: str) -> str:
    if not text.startswith("[alert:"):
        return text
    fields = {
        key: value.strip()
        for key, value in re.findall(
            r"\b(workstream|claim|why_now|next_action)=([^=]+?)(?=\s+\b(?:workstream|claim|why_now|next_action)=|$)",
            text,
        )
    }
    pieces: list[str] = []
    if fields.get("workstream") and fields.get("claim"):
        pieces.append(f"{fields['workstream']}: {fields['claim']}")
    elif fields.get("claim"):
        pieces.append(fields["claim"])
    if fields.get("why_now"):
        pieces.append(f"Why now: {fields['why_now']}")
    if fields.get("next_action"):
        pieces.append(f"Next action: {fields['next_action']}")
    return " ".join(pieces) or text


def _confidence_gaps(answer: str, citation_check: dict) -> list[str]:
    gaps: list[str] = []
    missing = re.search(r"^[-*]?\s*Missing:\s*(.+)$", answer or "", re.IGNORECASE | re.MULTILINE)
    if missing:
        value = missing.group(1).strip().rstrip(".")
        if value and value.lower() not in {"nothing", "nothing material"}:
            gaps.append(value)
    for claim in citation_check.get("unsupported_claims", []) or []:
        gaps.append(f"Unsupported by cited source: {strip_citations(claim)}")
    for claim in citation_check.get("uncited_claims", []) or []:
        gaps.append(f"Uncited claim: {strip_citations(claim)}")
    if citation_check.get("invalid_refs"):
        gaps.append("One or more citations did not match retrieved sources.")
    return gaps[:8]


def strip_citations(text: str) -> str:
    """Human-friendly rendering for Slack: drop [ref:...] tags."""
    return re.sub(r"\s*\[ref:[^\]]+\]", "", text)
