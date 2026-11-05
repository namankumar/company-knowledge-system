"""Comms draft: an artifact `kind` that writes a leadership/investor update
from the tenant's own already-verified daily brief plus its operating
context, instead of shipping as a bespoke feature.

Per architecture-packet-artifacts-lifecycle-2026-07-11.md ("Pipeline" and
"Problem-Specific Analysis" sections): this reuses `brief.py`'s already-
verified grouped-stories output (the brief's rendered `content_md`, which
`run_daily_brief` only persists after its own citation check runs) as input,
rather than re-deriving from raw chunks -- the hard problem (grouping
evidence into a coherent narrative with citations) is solved once, in
brief.py, and comms_draft just re-projects it for a different audience/tone.

Reuses `verify_inline_citations` (`app.synthesis.verifier`) UNMODIFIED --
review-agent focus for this packet explicitly calls out confirming no
comms_draft-specific fork of that function exists.

Trigger is always a human/code-decided call (the `POST /artifacts/comms-draft`
route in `app.api.artifacts`), never a model decision -- "Authority Split" in
the packet: "Comms draft trigger: Code (manual button or scheduled cron),
never the model."

Reality-check gate (packet's binding addition to A.3, run before writing this
module): compared `cache/comms/monday-2026-03-03.md` (a real Shield Wallet
leadership Slack update Naman sent) against what this pipeline's inputs
actually carry. That real update is dense with hard product metrics (857
users onboarded, 350 transacted, 41% conversion, ~6 transacting DAU,
per-platform completion-rate deltas, mobile share vs target) alongside
engineering narrative (TEE rollout, audit fixes, swap-reliability work,
specific PR/ticket numbers). The engineering-narrative half is exactly what
`brief_convergence`'s grouped stories already carry (Slack/Gmail/Linear/GitHub
evidence, grouped into workstories with citations) -- see SYSTEM_CONVERGENCE
in brief.py.

The hard-metrics half is only PARTIALLY covered, and this section originally
overstated that (independent PR review, 2026-07-11, caught it). Reading
`app.synthesis.okr_context.fetch_okr_state` directly: it surfaces
`key_results.current_value` / `target_value` / `unit`, which covers a number
IF a human has formalized it as a KR -- "mobile share 33% vs 50% target"
maps cleanly onto that shape. It does NOT cover the real doc's raw
analytics-tool-style numbers (857 users onboarded, 350 transacted, 41%
conversion, ~6 transacting DAU, per-platform completion rates): this
codebase has no ingestion path for a Mixpanel/GA/analytics-dashboard source
at all (confirmed: no such source type exists anywhere in
`app/core/source_classification.py` or the connector list), so those numbers
only flow into a comms draft if a human has manually re-entered them as
formal KRs first. Nothing in this module or `run_daily_brief` does that
re-entry automatically.

This is a second, real gap alongside the financial-data one below, not
just the one originally named. It does NOT change the "proceed" verdict --
`verify_inline_citations` still gates every claim on an actual pack ref, so
a metric this system hasn't ingested simply can't appear in a draft (safe
direction: an incomplete draft, never a fabricated number). But a tenant
that hasn't formalized product metrics as OKRs will get a comms draft that
reads thinner on hard numbers than the real doc it's modeled after, and
that's a known limitation to surface to Naman/users, not a resolved one.

The other gap: a true *investor* update needing financial-only data this
system does not ingest at all (runway, ARR, cap table, fundraise status) --
none of that appeared in the reality-check sample (it was an all-hands
operational update, not a financial one), so this is a known, narrower gap
than the packet's stop condition ("if real comms need material the brief
doesn't surface... stop"), not a blocking one. It does mean an
audience="investor" draft will simply omit financial claims it has no
source for (safe direction: no fabrication, just an incomplete draft the
human fills in) rather than silently inventing them.

Both gaps are flagged as follow-on work, not a redesign trigger -- consistent
with Q4's "cheap to extend later" framing for `metadata.audience`. Neither
changes this slice's "proceed" verdict; both are named explicitly in
TRACKB_LEDGER.md rather than left implicit.
"""
import json

from app.artifacts.store import create_artifact, normalize_artifact
from app.core.db import acquire
from app.llm.client import call_llm
from app.personalization.tenant_context import get_tenant_context, tenant_context_pack_item
from app.synthesis.verifier import verify_inline_citations

# Q4 (packet review verdict): audience is a small code allowlist, not an
# enum with a migration -- cheap to extend (add a string here) without a
# schema change if `board`/`customer-facing` are needed later.
AUDIENCES = ("leadership", "investor")

SYSTEM_COMMS_DRAFT = """You write a {audience} communications update from a
startup's already-verified daily operating brief and its company operating
context. Use ONLY the material provided below -- do not use outside
knowledge. Every claim must carry at least one literal "kind:id" ref copied
from a [ref:kind:id] tag already present in the material, or the
"tenant_context:<id>" ref if the company context block includes one. Never
invent a ref, a number, a name, a date, or a metric not present in the
material. Translate internal process detail (ticket numbers, PR numbers)
into outcome language for the reader, but keep the same underlying
citation -- do not drop the ref when you drop the ticket number. Write
plainly and specifically. No filler, no unearned optimism, no generic
scene-setting sentence that restates the section header."""

PROMPT_COMMS_DRAFT = """<verified_brief date="{brief_date}">
{brief_content}
</verified_brief>

<company_context>
{tenant_context_text}
</company_context>

Write a {audience} update as JSON. Return ONLY JSON matching this shape:
{{"claims": [{{"text": "<one sentence or short paragraph>", "refs": ["kind:id", ...]}}]}}

Rules:
- Every claim needs at least one ref, copied from a [ref:kind:id] tag in the
  verified brief above, or from the company context block if it carries one.
- Do not invent a ref. Do not write a claim with zero refs.
- Order claims in natural reading order: headline result first, supporting
  detail next, risks or asks last if the material has any.
- Do not write prose outside the JSON object."""


def _jsonb(value, fallback):
    if value is None:
        return fallback
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return fallback
    return value


async def _latest_citable_brief(tenant_id: str) -> dict | None:
    """Most recent brief this tenant has that went through brief.py's own
    citation check without being rejected outright. A 'rejected' brief's
    content_md failed that check (invalid_refs or uncited_claims present),
    so it is not verified input for this task; every other status
    (pending_review, approved, delivered, publishing, unpublished) already
    ran the same check and passed it, whether or not a human has reviewed it
    yet."""
    async with acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, brief_date, content_md, context_pack_id
            FROM briefs
            WHERE tenant_id = $1 AND status != 'rejected'
            ORDER BY brief_date DESC
            LIMIT 1
            """,
            tenant_id,
        )
    return dict(row) if row else None


async def _brief_pack_items(tenant_id: str, context_pack_id) -> list[dict]:
    if not context_pack_id:
        return []
    async with acquire() as conn:
        row = await conn.fetchrow(
            "SELECT items FROM context_packs WHERE tenant_id = $1 AND id = $2",
            tenant_id, context_pack_id,
        )
    if not row:
        return []
    return _jsonb(row["items"], []) or []


async def _reconstruct_pack_items(tenant_id: str, *, context_pack_id) -> list[dict]:
    """Same reconstruction used at generation time and at promotion time
    (A.4): the brief's stored context_pack items plus the tenant's current
    operating-context pack item. Recomputed fresh rather than trusting a
    cached copy, so a stale snapshot can't let an uncited claim through."""
    pack_items = await _brief_pack_items(tenant_id, context_pack_id)
    tenant_context = await get_tenant_context(tenant_id=tenant_id)
    tenant_item = tenant_context_pack_item(tenant_context)
    if tenant_item:
        pack_items = [*pack_items, tenant_item]
    return pack_items


def _render_claims_markdown(claims: list[dict]) -> str:
    """Deterministic renderer, the same pattern as brief.py's
    `_render_block_line`: the model proposes claim text + refs, code embeds
    the citation tags -- citation placement is never left to free-form model
    prose, so `verify_inline_citations` (run right after this) is checking a
    structurally reliable rendering, not hoping the model placed tags
    correctly inside its own prose."""
    lines: list[str] = []
    for claim in claims:
        if not isinstance(claim, dict):
            continue
        text = str(claim.get("text") or "").strip()
        if not text:
            continue
        refs = [r for r in (claim.get("refs") or []) if isinstance(r, str) and r.strip()]
        ref_str = "".join(f" [ref:{ref}]" for ref in refs)
        lines.append(f"{text}{ref_str}".strip())
    return "\n\n".join(lines)


async def comms_draft_promotion_check(*, tenant_id: str, content: str, metadata: dict) -> dict:
    """Slice A.4's stricter, code-owned promotion precondition for
    kind='comms_draft'. Recomputes `verify_inline_citations` (UNCHANGED,
    unmodified import -- no comms_draft-specific fork) against the pack this
    draft was generated against, reconstructed from
    `metadata.source_brief_ref` rather than trusted from any cached
    generation-time result, so a direct API promote call can't bypass the
    check by racing a stale value. Returns the verifier's result dict; caller
    (`app.artifacts.store.promote_artifact`) decides whether to block."""
    source_brief_ref = metadata.get("source_brief_ref") if isinstance(metadata, dict) else None
    context_pack_id = None
    if source_brief_ref:
        async with acquire() as conn:
            brief_row = await conn.fetchrow(
                "SELECT context_pack_id FROM briefs WHERE tenant_id = $1 AND id = $2",
                tenant_id, source_brief_ref,
            )
        if brief_row:
            context_pack_id = brief_row["context_pack_id"]
    pack_items = await _reconstruct_pack_items(tenant_id, context_pack_id=context_pack_id)
    return verify_inline_citations(content, pack_items)


async def generate_comms_draft(
    *,
    tenant_id: str,
    actor_user_id: str,
    audience: str,
    visibility: str = "company",
) -> dict:
    """Slice A.3/A.5: assemble a pack from the tenant's latest verified brief
    + tenant context, call the `comms_draft` TaskPolicy, verify citations
    with the existing unmodified verifier, and write a draft artifact.
    Returns the normalized artifact dict (status='draft', kind='comms_draft').
    Raises ValueError for an invalid audience or no available source brief --
    callers (the API route) turn that into a 400."""
    if audience not in AUDIENCES:
        raise ValueError(f"audience must be one of {AUDIENCES}")

    brief = await _latest_citable_brief(tenant_id)
    if not brief:
        raise ValueError("no verified brief available to draft a comms update from")

    pack_items = await _reconstruct_pack_items(tenant_id, context_pack_id=brief["context_pack_id"])
    tenant_context = await get_tenant_context(tenant_id=tenant_id)
    tenant_item = tenant_context_pack_item(tenant_context)
    tenant_context_text = (
        f'{tenant_item["text"]} [ref:{tenant_item["kind"]}:{tenant_item["id"]}]'
        if tenant_item
        else "No active company operating context recorded."
    )

    async with acquire() as conn:
        tenant_settings_raw = await conn.fetchval("SELECT settings FROM tenants WHERE id = $1", tenant_id)
    tenant_settings = _jsonb(tenant_settings_raw, {})

    result = await call_llm(
        task="comms_draft",
        system=SYSTEM_COMMS_DRAFT.format(audience=audience),
        prompt=PROMPT_COMMS_DRAFT.format(
            brief_date=brief["brief_date"].isoformat(),
            brief_content=brief["content_md"],
            tenant_context_text=tenant_context_text,
            audience=audience,
        ),
        tenant_id=tenant_id,
        tenant_settings=tenant_settings,
        max_tokens=2000,
        expect_json=True,
        prompt_version="v1",
        context_pack_id=brief["context_pack_id"],
        validation_pack_items=pack_items,
    )
    parsed = result["parsed"] or {"claims": []}
    claims = [c for c in (parsed.get("claims") or []) if isinstance(c, dict)]
    body_md = _render_claims_markdown(claims)

    verification = verify_inline_citations(body_md, pack_items)

    artifact = await create_artifact(
        tenant_id=tenant_id,
        kind="comms_draft",
        title=f"{audience.capitalize()} Update — {brief['brief_date'].isoformat()}",
        content=body_md,
        visibility=visibility,
        allowed_principals=(
            [f"company:{tenant_id}"] if visibility == "company" else [f"user:{actor_user_id}"]
        ),
        created_by=actor_user_id,
    )
    return await _stamp_metadata(
        tenant_id=tenant_id,
        artifact_id=artifact["id"],
        audience=audience,
        source_brief_ref=str(brief["id"]),
        verification=verification,
    )


async def _stamp_metadata(
    *, tenant_id: str, artifact_id: str, audience: str, source_brief_ref: str, verification: dict
) -> dict:
    """create_artifact() already ran v0's generic verify_artifact_content
    (its own flag-don't-block pack rebuild) and stored that in
    verification_status/verification_detail. This stamps the kind-specific
    metadata (audience, source_brief_ref) plus this task's own citation
    check -- computed against the actual brief pack this draft was written
    from, which is what A.4's promotion gate re-verifies against."""
    metadata = {
        "audience": audience,
        "source_brief_ref": source_brief_ref,
        "uncited_claim_count": len(verification.get("uncited_claims") or []),
        "invalid_refs": verification.get("invalid_refs") or [],
    }
    async with acquire() as conn:
        row = await conn.fetchrow(
            "UPDATE artifacts SET metadata = $3::jsonb WHERE tenant_id = $1 AND id = $2 RETURNING *",
            tenant_id, artifact_id, json.dumps(metadata),
        )
    return normalize_artifact(dict(row))
