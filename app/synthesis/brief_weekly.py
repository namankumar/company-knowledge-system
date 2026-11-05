"""Weekly founder brief synthesis."""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import structlog

from app.core import trace
from app.core.db import acquire, _current_tenant_id
from app.core.destination_permissions import source_visible_to_principals
from app.core.tenant_settings import autopublish_read_only_enabled, client_publishing_paused, synthesis_paused
from app.llm.client import call_llm
from app.retrieval.retrieve import _open_loop_objects, _stale_work_objects
from app.synthesis.brief import (
    _apply_destination_claim_gate,
    _autopublish_read_only,
    _brief_citation_check,
    _company_principals,
    _drop_malformed_citation_lines,
    _render_block_line,
    _render_pack,
    _loop_section_blocks,
    _stale_work_blocks,
)
from app.synthesis.okr_pace import summarize_okr_pace_items
from app.synthesis.verifier import normalize_citations

logger = structlog.get_logger()

WEEKLY_BRIEF_PROMPT_VERSION = "v1.0"

SYSTEM_WEEKLY = """You write a weekly operating brief for a startup operator.
Use ONLY the provided sources. The input contains 7 daily briefs as structured
JSON, open loops, stale work, and OKR trajectory items. Do not invent trend
lines when fewer than 7 daily briefs are available. If that happens, say so
plainly in the Trends section instead. Every ref in your output must be a
literal "kind:id" value copied from a <source ref="kind:id"> tag. Never invent
one."""

PROMPT_WEEKLY = """<context>
{context}
</context>

Write this tenant's weekly operating brief for {today} as JSON.

Return ONLY JSON matching this shape:
{{
  "sections": [
    {{"title": "Two-week arc summary", "text": "<summary of the two-week arc>", "refs": ["kind:id", ...]}},
    {{"title": "What shipped", "text": "<what shipped this week>", "refs": ["kind:id", ...]}},
    {{"title": "Trends", "text": "<velocity / blocker / feedback trend, or explicit insufficient-history note>", "refs": ["kind:id", ...]}},
    {{"title": "Next week", "text": "<ship targets, unblocks, prep>", "refs": ["kind:id", ...]}}
  ],
  "coverage": {{"text": "<which sources fed this brief and any gaps>", "refs": ["kind:id", ...]}}
}}

Rules:
- Use the 7 daily briefs as structured stories, not markdown.
- If fewer than 7 daily briefs are available, say so in the Trends section
  instead of inventing a trend.
- Every ref must be a literal "kind:id" value copied from the sources above.
- Do not write prose outside the JSON object."""


def _jsonb(value, default):
    if value is None:
        return default
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return default
        return parsed if parsed is not None else default
    return value


def _sanitize_daily_brief_payload(payload: dict, *, brief_ref: str) -> dict:
    def _rewrite(node):
        if isinstance(node, dict):
            rewritten = {}
            for key, value in node.items():
                if key == "refs" and isinstance(value, list):
                    cleaned = []
                    for ref in value:
                        ref_text = str(ref or "").strip()
                        if not ref_text:
                            continue
                        if ref_text.startswith("daily_brief:"):
                            cleaned.append(ref_text)
                        else:
                            cleaned.append(brief_ref)
                    rewritten[key] = sorted(set(cleaned)) if cleaned else []
                else:
                    rewritten[key] = _rewrite(value)
            return rewritten
        if isinstance(node, list):
            return [_rewrite(item) for item in node]
        return node

    return _rewrite(payload)


async def run_weekly_brief(ctx: dict, *, tenant_id: str, actor_user_id: str | None = None) -> str | None:
    _current_tenant_id.set(tenant_id)
    async with acquire() as conn:
        tenant_settings_raw = await conn.fetchval("SELECT settings FROM tenants WHERE id = $1", tenant_id)
    tenant_settings = json.loads(tenant_settings_raw) if isinstance(tenant_settings_raw, str) else dict(tenant_settings_raw or {})
    weekly_date = _tenant_weekly_date(tenant_settings)
    if synthesis_paused(tenant_settings):
        run_id = await trace.start_run(tenant_id=tenant_id, kind="weekly")
        async with trace.step(run_id, tenant_id, "synthesis_paused") as st:
            st.status = "skipped"
            st.detail = {"reason": "tenant pause_synthesis is enabled"}
        await trace.finish_run(run_id, status="paused")
        return None

    async with acquire() as conn:
        existing = await conn.fetchrow(
            "SELECT id, status FROM weekly_briefs WHERE tenant_id = $1 AND brief_date = $2",
            tenant_id, weekly_date,
        )
        if existing and existing["status"] not in {"rejected", "unpublished"}:
            logger.info("weekly_brief_already_exists", tenant_id=tenant_id, brief_date=str(weekly_date))
            return str(existing["id"])
    retry_weekly_brief_id = str(existing["id"]) if existing else None

    run_id = await trace.start_run(tenant_id=tenant_id, kind="weekly")
    try:
        async with trace.step(run_id, tenant_id, "context_retrieved") as st:
            daily_brief_items = await _daily_brief_items(tenant_id, _company_principals(tenant_id), limit=7)
            open_loop_items = await _open_loop_objects(tenant_id, allowed_principals=_company_principals(tenant_id))
            stale_work_items = await _stale_work_objects(tenant_id, allowed_principals=_company_principals(tenant_id))
            okr_items = await summarize_okr_pace_items(tenant_id)
            source_items = await _source_freshness_items(tenant_id, _company_principals(tenant_id))
            extra_items = [*daily_brief_items, *source_items, *open_loop_items, *stale_work_items, *okr_items]
            pack = await _create_context_pack(
                tenant_id=tenant_id,
                purpose="brief_weekly",
                items=extra_items,
            )
            st.output_ref = pack["context_pack_id"]
            st.detail = {
                "items": len(pack["items"]),
                "daily_briefs": len(daily_brief_items),
                "open_loop_items": len(open_loop_items),
                "stale_work_items": len(stale_work_items),
                "okr_items": len(okr_items),
            }

        if not pack["items"]:
            logger.info("weekly_brief_skipped_no_activity", tenant_id=tenant_id)
            await trace.finish_run(run_id, status="suppressed")
            return None

        async with trace.step(run_id, tenant_id, "synthesized", input_ref=pack["context_pack_id"]) as st:
            write_result = await call_llm(
                task="brief_weekly",
                system=SYSTEM_WEEKLY,
                prompt=PROMPT_WEEKLY.format(context=pack["text"], today=weekly_date.isoformat()),
                tenant_id=tenant_id,
                tenant_settings=tenant_settings,
                max_tokens=2500,
                expect_json=True,
                prompt_version=WEEKLY_BRIEF_PROMPT_VERSION,
                context_pack_id=pack["context_pack_id"],
                pipeline_run_id=run_id,
                validation_pack_items=pack["items"],
            )
            parsed = write_result["parsed"] or {"sections": [], "coverage": {"text": "", "refs": []}}
            if len(daily_brief_items) < 7:
                trends = _section_by_title(parsed, "Trends")
                note = f"Insufficient history: only {len(daily_brief_items)} delivered daily briefs are available."
                if trends:
                    trends["text"] = note if note not in str(trends.get("text") or "") else str(trends.get("text") or "")
                else:
                    parsed.setdefault("sections", []).append({
                        "title": "Trends",
                        "text": note,
                        "refs": [f"daily_brief:{item['id']}" for item in daily_brief_items[:1]],
                    })
            result = {
                "text": _render_weekly_markdown(
                    parsed,
                    pack["items"],
                    daily_brief_items=daily_brief_items,
                    okr_items=okr_items,
                    open_loop_items=open_loop_items,
                    stale_work_items=stale_work_items,
                ),
                "model_call_id": write_result["model_call_id"],
            }
            st.output_ref = result["model_call_id"]
            st.detail = {
                "daily_brief_count": len(daily_brief_items),
                "okrs": len(okr_items),
                "open_loops": len(open_loop_items),
            }

        async with trace.step(run_id, tenant_id, "verified", input_ref=result["model_call_id"]) as st:
            content, omitted_lines = _drop_malformed_citation_lines(
                normalize_citations(
                    result["text"].strip(),
                    pack["items"],
                )
            )
            citation_check = _brief_citation_check(content, pack["items"])
            if citation_check["valid"]:
                content, citation_check = _apply_destination_claim_gate(
                    content,
                    citation_check,
                    pack["items"],
                    _company_principals(tenant_id),
                )
            if omitted_lines:
                citation_check = {**citation_check, "omitted_malformed_citation_lines": omitted_lines}
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
            weekly_id = retry_weekly_brief_id or str(uuid.uuid4())
            async with acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO weekly_briefs
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
                    WHERE weekly_briefs.status IN ('rejected', 'unpublished')
                    """,
                    weekly_id, tenant_id, weekly_date, content,
                    json.dumps({
                        "citation_check": citation_check,
                        "weekly": parsed,
                        "daily_briefs_count": len(daily_brief_items),
                    }),
                    pack["context_pack_id"], result["model_call_id"], status, run_id,
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
                            "weekly_brief_id": weekly_id,
                            "invalid_refs": citation_check["invalid_refs"],
                            "unsupported_claims": citation_check.get("unsupported_claims", []),
                        }),
                        run_id,
                    )
            st.output_ref = weekly_id
            st.detail = {"status": status}

        if autopublish:
            receivers = await _publish_autopublished_weekly_brief(tenant_id=tenant_id, brief_id=weekly_id)
            async with acquire() as conn:
                await conn.execute(
                    """
                    UPDATE weekly_briefs
                    SET status = 'delivered',
                        delivered_via = delivered_via || $3::jsonb
                    WHERE id = $1 AND tenant_id = $2 AND status = 'publishing'
                    """,
                    weekly_id, tenant_id, json.dumps(["app_auto"]),
                )
            if run_id:
                async with trace.step(run_id, tenant_id, "app_delivery", input_ref=weekly_id) as st:
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
            "weekly_brief_created",
            tenant_id=tenant_id,
            brief_id=weekly_id,
            citations_valid=citation_check["valid"],
            pipeline_run_id=run_id,
        )
        return weekly_id
    except Exception as e:
        await trace.finish_run(run_id, status="failed", error=f"{type(e).__name__}: {e}")
        raise


def _tenant_weekly_date(tenant_settings: dict):
    tz_name = tenant_settings.get("brief_timezone") or tenant_settings.get("timezone") or "America/Los_Angeles"
    try:
        tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        tz = ZoneInfo("UTC")
    return datetime.now(tz).date()


def _section_by_title(parsed: dict, title: str) -> dict | None:
    for section in parsed.get("sections") or []:
        if isinstance(section, dict) and str(section.get("title") or "").strip().lower() == title.lower():
            return section
    return None


def _render_weekly_markdown(
    parsed: dict,
    pack_items: list[dict],
    *,
    daily_brief_items: list[dict],
    okr_items: list[dict],
    open_loop_items: list[dict],
    stale_work_items: list[dict],
) -> str:
    blocks: list[str] = []
    for title in ("Two-week arc summary", "What shipped", "Trends", "Next week"):
        section = _section_by_title(parsed, title) or {}
        if title == "Trends" and len(daily_brief_items) < 7:
            text = f"Insufficient history: only {len(daily_brief_items)} delivered daily briefs are available."
            refs = [f"daily_brief:{item['id']}" for item in daily_brief_items[:1]]
            section = {"text": text, "refs": refs}
        blocks.append(f"## {title}\n{_render_block_line(None, section, bare=True)}")

    stale_blocks = _stale_work_blocks(stale_work_items)
    if stale_blocks:
        blocks.append("\n".join(["## What Didn't Move", *stale_blocks]))

    okr_blocks = _weekly_okr_trajectory_blocks(okr_items)
    if okr_blocks:
        blocks.append("\n".join(["## OKR trajectory", *okr_blocks]))

    loop_blocks = [block.replace("## Kill list", "## Kill list carried from dailies", 1) for block in _loop_section_blocks(open_loop_items)]
    blocks.extend(loop_blocks)

    coverage = parsed.get("coverage") or {}
    blocks.append(f"## Coverage\n{_render_block_line(None, coverage, bare=True)}")
    return "\n\n".join(blocks).strip()


def _weekly_okr_trajectory_blocks(okr_items: list[dict]) -> list[str]:
    blocks: list[str] = []
    for item in okr_items:
        text = str(item.get("text") or "").strip().replace("\n", " ")
        if not text:
            continue
        blocks.append(_render_block_line(None, {"text": text, "refs": [f"{item['kind']}:{item['id']}"]}, bare=True))
    return blocks


async def _daily_brief_items(tenant_id: str, principals: list[str], *, limit: int = 7) -> list[dict]:
    async with acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, brief_date, content_md, sections, created_at
            FROM briefs
            WHERE tenant_id = $1
              AND status = 'delivered'
              AND acl_state = 'stamped'
              AND acl_freshness_state = 'fresh'
              AND allowed_principals ?| $2::text[]
            ORDER BY brief_date DESC
            LIMIT $3
            """,
            tenant_id,
            principals,
            limit,
        )
    items: list[dict] = []
    for row in rows:
        sections = _jsonb(row["sections"], {}) or {}
        brief_payload = sections.get("brief") if isinstance(sections, dict) else {}
        payload = {
            "brief_date": str(row["brief_date"]),
            "stories": (brief_payload or {}).get("stories") if isinstance(brief_payload, dict) else [],
            "coverage": (brief_payload or {}).get("coverage") if isinstance(brief_payload, dict) else None,
            "plan_actual": (brief_payload or {}).get("plan_actual") if isinstance(brief_payload, dict) else None,
        }
        item = {
            "kind": "daily_brief",
            "id": str(row["id"]),
            "title": f"Daily brief {row['brief_date']}",
            "text": json.dumps(
                _sanitize_daily_brief_payload(payload, brief_ref=f"daily_brief:{row['id']}"),
                ensure_ascii=False,
            ),
            "source_url": None,
            "source_title": "Daily brief",
            "source_type": "brief",
            "source_observed_at": row["created_at"].isoformat() if row["created_at"] else None,
            "visibility": "company",
            "allowed_principals": principals,
            "acl_state": "stamped",
            "acl_freshness_state": "fresh",
            "priority": 1,
            "score": 1.0,
        }
        if source_visible_to_principals(item, principals):
            items.append(item)
    return items


async def _source_freshness_items(tenant_id: str, destination_principals: list[str] | None = None) -> list[dict]:
    from app.synthesis.brief import _source_freshness_items as daily_source_freshness_items

    return await daily_source_freshness_items(tenant_id, destination_principals)


async def _create_context_pack(*, tenant_id: str, purpose: str, items: list[dict]) -> dict:
    visible_items = []
    seen: set[str] = set()
    for item in items:
        if not source_visible_to_principals(item, _company_principals(tenant_id)):
            continue
        ref = f"{item.get('kind')}:{item.get('id')}"
        if ref in seen:
            continue
        seen.add(ref)
        visible_items.append(item)
    items = visible_items
    text = _render_pack(items)
    token_count = sum(len(item["text"]) // 4 + 20 for item in items)
    context_pack_id = str(uuid.uuid4())
    async with acquire() as conn:
        await conn.execute(
            """
            INSERT INTO context_packs (id, tenant_id, purpose, query, items, token_count)
            VALUES ($1, $2, $3, $4, $5::jsonb, $6)
            """,
            context_pack_id,
            tenant_id,
            purpose,
            "weekly brief",
            json.dumps(items),
            token_count,
        )
    return {"context_pack_id": context_pack_id, "items": items, "text": text}


async def _publish_autopublished_weekly_brief(*, tenant_id: str, brief_id: str) -> int:
    from app.delivery.slack_dm import _publish_to_app

    async with acquire() as conn:
        brief = await conn.fetchrow(
            """
            SELECT wb.*, COALESCE(cp.items, '[]'::jsonb) AS context_items
            FROM weekly_briefs wb
            LEFT JOIN context_packs cp ON cp.id = wb.context_pack_id
            WHERE wb.id = $1 AND wb.tenant_id = $2
              AND wb.acl_state = 'stamped'
              AND wb.acl_freshness_state = 'fresh'
              AND wb.allowed_principals ?| $3::text[]
            """,
            brief_id, tenant_id, _company_principals(tenant_id),
        )
    if not brief:
        return 0
    content, _ = _apply_destination_claim_gate(
        brief["content_md"],
        {"valid": True},
        _jsonb(brief["context_items"], []) or [],
        _company_principals(tenant_id),
    )
    return await _publish_to_app(tenant_id, {
        "type": "weekly_brief",
        "brief_id": brief_id,
        "body": content,
        "brief_date": str(brief["brief_date"]),
    })
