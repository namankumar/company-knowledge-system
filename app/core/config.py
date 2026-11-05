from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from functools import lru_cache
import structlog

from app.llm.catalog import UnknownModelCostError, normalize_model_override

logger = structlog.get_logger()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # App
    app_env: str = "development"
    app_version: str = "0.2.0"
    secret_key: str = "change-me"

    # Database
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/inferencehq"
    database_pool_size: int = 10
    database_max_overflow: int = 20
    # Comma-separated DB login roles used by other services, such as the
    # worker, when they do not share the web service's DATABASE_URL role.
    database_runtime_roles: str = ""

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # Auth
    admin_token: str = ""              # bearer token for /admin endpoints; empty disables admin API
    cors_allow_origins: str = ""       # comma-separated browser origins, e.g. http://localhost:8082
    google_oauth_client_id: str = ""   # optional aud check for Google ID-token login
    auth_cookie_domain: str = ""       # optional cookie domain, e.g. .inferencehq.co

    # Anthropic + model router. Tiers map to router tasks in app/llm/router.py.
    anthropic_api_key: str = ""
    model_cheap: str = "claude-haiku-4-5-20251001"
    model_mid: str = "claude-sonnet-4-6"
    model_strong: str = "claude-opus-4-8"
    model_override_global: str = ""    # force every task to this model
    model_override_tasks: str = ""     # "triage=claude-sonnet-4-6,chat=claude-opus-4-8"
    llm_daily_budget_usd: float = 25.0
    llm_fixture_mode: bool = False
    llm_processing_enabled: bool = True

    # Embeddings (optional — retrieval falls back to FTS when unset)
    openai_api_key: str = ""
    embedding_model: str = "text-embedding-3-small"

    # Optional OpenAI-compatible LLM providers. Only used when a route policy enables them.
    groq_api_key: str = ""
    groq_base_url: str = "https://api.groq.com/openai/v1"
    deepinfra_api_key: str = ""
    deepinfra_base_url: str = "https://api.deepinfra.com/v1/openai"
    lilac_api_key: str = ""
    lilac_base_url: str = "https://api.getlilac.com/v1"
    wafer_api_key: str = ""
    wafer_base_url: str = "https://pass.wafer.ai/v1"
    wafer_zdr_required: bool = False
    baseten_api_key: str = ""
    baseten_base_url: str = "https://inference.baseten.co/v1"
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"

    # Local CLI-backed providers (development-only wedge)
    claude_cli_enabled: bool = False
    claude_cli_command: str = "claude"
    claude_cli_home: str = ""
    claude_cli_chat_first: bool = False

    # Composio
    composio_api_key: str = ""
    composio_webhook_secret: str = ""
    composio_toolkit_versions: str = ""  # "googledocs=20260518_00,slack=20260519_01"
    slack_composio_auth_config_id: str = ""  # required for ExampleCo Slack OAuth without direct-message scopes
    slack_composio_dm_auth_config_id: str = ""  # required for non-ExampleCo Slack OAuth with 1-1 DM read scopes
    hubspot_composio_auth_config_id: str = ""  # required for read-only HubSpot OAuth; do not use Composio managed HubSpot
    linear_composio_auth_config_id: str = ""  # optional pinned Composio Linear config; must be read-only
    public_url: str = "http://localhost:8000"
    founder_app_url: str = "http://localhost:8082"
    exampleco_scoreboard_password: str = ""

    # GitHub App, used for PR metadata without broad OAuth repo scopes.
    github_app_id: str = ""
    github_app_slug: str = ""
    github_app_private_key: str = ""
    github_app_webhook_secret: str = ""
    github_app_manifest_owner: str = "InferenceHQ"

    # Sentry
    sentry_dsn: str = ""

    # Pipeline behavior
    triage_batch_window_seconds: int = 120   # debounce window per thread before synthesis
    backfill_days: int = 60
    backfill_max_pages_per_run: int = 250
    backfill_ingest_spacing_seconds: float = 2.0
    source_synthesis_dedupe_seconds: int = 300
    live_drift_synthesis_dedupe_seconds: int = 300
    personal_signal_projection_dedupe_seconds: int = 300
    personal_signal_projection_max_subjects: int = 25
    backfill_synthesis_drain_retry_seconds: int = 30
    backfill_synthesis_max_drain_attempts: int = 20
    worker_max_jobs: int = 5

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @model_validator(mode="after")
    def fixture_model_is_test_only(self):
        if self.llm_fixture_mode and self.app_env != "test":
            raise ValueError("LLM_FIXTURE_MODE can only be enabled when APP_ENV=test")
        if self.model_override_global:
            try:
                self.model_override_global = normalize_model_override(self.model_override_global)
            except UnknownModelCostError as exc:
                logger.error(
                    "invalid_model_override_global",
                    model_override=self.model_override_global,
                    error=str(exc),
                )
                self.model_override_global = ""
        if self.model_override_tasks:
            valid_pairs: list[str] = []
            for raw_pair in self.model_override_tasks.split(","):
                pair = raw_pair.strip()
                if not pair or "=" not in pair:
                    if pair:
                        logger.error("invalid_model_override_task_entry", entry=pair)
                    continue
                task, value = pair.split("=", 1)
                task = task.strip()
                value = value.strip()
                if not task or not value:
                    logger.error("invalid_model_override_task_entry", entry=pair)
                    continue
                try:
                    normalized = normalize_model_override(value)
                except UnknownModelCostError as exc:
                    logger.error(
                        "invalid_model_override_task",
                        task=task,
                        model_override=value,
                        error=str(exc),
                    )
                    continue
                valid_pairs.append(f"{task}={normalized}")
            self.model_override_tasks = ",".join(valid_pairs)
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
