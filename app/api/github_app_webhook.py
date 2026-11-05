"""GitHub App webhook receiver for PR metadata events."""
from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from typing import Annotated

import structlog
from fastapi import APIRouter, Header, HTTPException, Request

from app.core import trace
from app.core.config import get_settings
from app.core.db import _current_tenant_id, _rls_bypass, acquire
from app.core.source_imports import import_disabled_reason, import_enabled
from app.core.source_permissions import sanitize_source_payload
from app.ingestion.event_identity import payload_sha256
from app.workers.queue import enqueue_ingest

router = APIRouter(prefix="/github-app", tags=["github-app"])
logger = structlog.get_logger()

SUPPORTED_GITHUB_EVENTS = {
    "pull_request",
    "pull_request_review",
    "issues",
}


def verify_github_signature(*, secret: str, body: bytes, signature_header: str | None) -> bool:
    if not secret or not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


@router.post("/webhook")
async def github_app_webhook(
    request: Request,
    x_github_event: Annotated[str | None, Header(alias="X-GitHub-Event")] = None,
    x_github_delivery: Annotated[str | None, Header(alias="X-GitHub-Delivery")] = None,
    x_hub_signature_256: Annotated[str | None, Header(alias="X-Hub-Signature-256")] = None,
):
    body = await request.body()
    settings = get_settings()
    if settings.github_app_webhook_secret:
        if not verify_github_signature(
            secret=settings.github_app_webhook_secret,
            body=body,
            signature_header=x_hub_signature_256,
        ):
            logger.warning("github_app_signature_rejected", delivery=x_github_delivery)
            raise HTTPException(status_code=401, detail="Invalid GitHub webhook signature")
    elif settings.is_production:
        logger.error("github_app_webhook_secret_missing_in_production")
        raise HTTPException(status_code=503, detail="GitHub webhook secret not configured")

    event_name = (x_github_event or "").strip()
    if event_name == "ping":
        return {"ok": True, "event": "ping"}
    if event_name not in SUPPORTED_GITHUB_EVENTS:
        logger.info("github_app_event_skipped", event=event_name)
        return {"ok": True, "skipped": True, "reason": "unsupported_event"}

    payload = json.loads(body)
    installation_id = str((payload.get("installation") or {}).get("id") or "")
    if not installation_id:
        raise HTTPException(status_code=400, detail="Missing GitHub installation id")

    bypass_token = _rls_bypass.set(True)
    try:
        async with acquire() as conn:
            source = await conn.fetchrow(
                """
                SELECT id, tenant_id, status, sync_policy FROM sources
                WHERE source_type = 'github' AND external_id = $1
                """,
                f"installation:{installation_id}",
            )
    finally:
        _rls_bypass.reset(bypass_token)
    if not source:
        logger.warning("github_app_unknown_installation", installation_id=installation_id, event=event_name)
        raise HTTPException(status_code=404, detail="Unknown GitHub installation")

    tenant_id = str(source["tenant_id"])
    source_id = str(source["id"])
    _current_tenant_id.set(tenant_id)
    if not import_enabled(source["sync_policy"], status=source["status"]):
        reason = import_disabled_reason(source["sync_policy"], status=source["status"]) or "source import disabled"
        logger.info(
            "github_app_event_import_disabled",
            tenant_id=tenant_id,
            source_id=source_id,
            event=event_name,
            reason=reason,
        )
        return {"ok": True, "skipped": True, "reason": reason}
    event_data = sanitize_source_payload("github", payload)
    external_event_id = _github_external_event_id(event_name, event_data, x_github_delivery)
    run_id = await trace.start_run(tenant_id=tenant_id, kind="webhook", source_id=source_id)

    async with acquire() as conn:
        await conn.execute(
            "UPDATE sources SET last_webhook_at = now(), stale_since = NULL WHERE id = $1",
            source_id,
        )
        await conn.execute(
            """
            INSERT INTO raw_events
                (tenant_id, source_id, external_event_id, payload_sha256, event_type, payload,
                 is_backfill, pipeline_run_id, processing_attempts)
            VALUES ($1, $2, $3, $4, $5, $6::jsonb, false, $7, 0)
            ON CONFLICT (tenant_id, source_id, external_event_id, payload_sha256) DO UPDATE
            SET event_type = EXCLUDED.event_type,
                pipeline_run_id = COALESCE(raw_events.pipeline_run_id, EXCLUDED.pipeline_run_id),
                last_processing_error = NULL
            """,
            tenant_id,
            source_id,
            external_event_id,
            payload_sha256(event_data),
            f"github_app.{event_name}",
            json.dumps(event_data),
            run_id,
        )

    await enqueue_ingest(
        tenant_id=tenant_id,
        source_id=source_id,
        external_event_id=external_event_id,
        payload=event_data,
        event_type=f"github_app.{event_name}",
        pipeline_run_id=run_id,
    )
    logger.info("github_app_event_enqueued", tenant_id=tenant_id, source_id=source_id, event=event_name)
    return {"ok": True}


def _github_external_event_id(event_name: str, payload: dict, delivery_id: str | None) -> str:
    if delivery_id:
        return delivery_id
    item = payload.get("pull_request") or payload.get("issue") or {}
    item_id = item.get("id") or item.get("node_id") or item.get("number")
    action = payload.get("action") or event_name
    return f"{event_name}:{item_id or uuid.uuid4()}:{action}"
