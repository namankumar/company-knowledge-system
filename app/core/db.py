import contextvars
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Any

import structlog

from app.core.config import get_settings

logger = structlog.get_logger()

_pool: Any = None

# Context var for tenant isolation — set at the top of every request
_current_tenant_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "current_tenant_id", default=None
)
_rls_bypass: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "rls_bypass", default=False
)


async def create_pool() -> Any:
    import asyncpg  # lazy import — C extension, skip at module load time
    settings = get_settings()
    dsn = settings.database_url.replace("postgresql+asyncpg://", "postgresql://")
    return await asyncpg.create_pool(
        dsn,
        min_size=2,
        max_size=settings.database_pool_size,
        max_inactive_connection_lifetime=300,
        command_timeout=30,
    )


async def get_pool() -> Any:
    global _pool
    if _pool is None:
        _pool = await create_pool()
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool:
        try:
            await _pool.close()
        except RuntimeError as e:
            # Test-only defensive branch: pytest-asyncio gives each async
            # test its own event loop, but this pool is a module-level
            # singleton, so a pool created under a prior test's loop cannot
            # be gracefully closed once that loop is gone. There is nothing
            # left to flush in that case -- just drop the reference so the
            # next get_pool() call creates a fresh pool under the current
            # loop. Gated to APP_ENV=test so production/worker/script
            # behavior of close_pool() is unchanged: any other RuntimeError,
            # and any RuntimeError at all outside tests, still raises.
            settings = get_settings()
            if settings.app_env == "test" and "event loop is closed" in str(e).lower():
                logger.warning(
                    "close_pool_swallowed_closed_loop",
                    app_env=settings.app_env,
                    error=str(e),
                )
            else:
                raise
        _pool = None


@asynccontextmanager
async def acquire(*, use_runtime_role: bool = True) -> AsyncGenerator[Any, None]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            if use_runtime_role:
                await conn.execute("SET LOCAL ROLE inferencehq_app")
            tenant_id = _current_tenant_id.get()
            if tenant_id:
                await conn.execute("SELECT set_config('app.tenant_id', $1, true)", tenant_id)
            if _rls_bypass.get():
                await conn.execute("SELECT set_config('app.bypass_rls', 'on', true)")
            yield conn
