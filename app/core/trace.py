"""Pipeline trace: every output must be reconstructable back to its source."""
import json
import uuid
from contextlib import asynccontextmanager

import structlog

from app.core.db import acquire

logger = structlog.get_logger()


async def start_run(*, tenant_id: str, kind: str, source_id: str | None = None) -> str:
    run_id = str(uuid.uuid4())
    async with acquire() as conn:
        await conn.execute(
            "INSERT INTO pipeline_runs (id, tenant_id, kind, source_id) VALUES ($1, $2, $3, $4)",
            run_id, tenant_id, kind, source_id,
        )
    return run_id


async def finish_run(run_id: str, *, status: str = "completed", error: str | None = None) -> None:
    async with acquire() as conn:
        await conn.execute(
            "UPDATE pipeline_runs SET status = $2, error = $3, finished_at = now() WHERE id = $1",
            run_id, status, error,
        )


async def set_run_status(
    run_id: str,
    status: str,
    *,
    error: str | None = None,
    parent_run_id: str | None = None,
) -> None:
    """Update an in-flight run without closing it."""
    async with acquire() as conn:
        await conn.execute(
            """
            UPDATE pipeline_runs
            SET status = $2,
                error = COALESCE($3, error),
                parent_run_id = COALESCE($4, parent_run_id)
            WHERE id = $1
            """,
            run_id, status, error, parent_run_id,
        )


class StepResult:
    def __init__(self):
        self.output_ref: str | None = None
        self.detail: dict = {}
        self.status: str = "completed"

    def skip(self, reason: str) -> None:
        self.status = "skipped"
        self.detail["skip_reason"] = reason


@asynccontextmanager
async def step(run_id: str, tenant_id: str, name: str, *, input_ref: str | None = None):
    """
    One pipeline stage. Records status/timing/error. Set result.output_ref /
    result.detail inside the block to enrich the trace.
    """
    step_id = str(uuid.uuid4())
    async with acquire() as conn:
        await conn.execute(
            """
            INSERT INTO pipeline_steps (id, tenant_id, run_id, name, input_ref)
            VALUES ($1, $2, $3, $4, $5)
            """,
            step_id, tenant_id, run_id, name, input_ref,
        )

    result = StepResult()
    try:
        yield result
    except Exception as e:
        async with acquire() as conn:
            await conn.execute(
                "UPDATE pipeline_steps SET status = 'failed', error = $2, finished_at = now() WHERE id = $1",
                step_id, f"{type(e).__name__}: {e}",
            )
        raise
    else:
        async with acquire() as conn:
            await conn.execute(
                """
                UPDATE pipeline_steps
                SET status = $2, output_ref = $3, detail = $4::jsonb, finished_at = now()
                WHERE id = $1
                """,
                step_id, result.status, result.output_ref, json.dumps(result.detail),
            )
