from functools import lru_cache

from composio import Composio

from app.core.config import get_settings


@lru_cache
def get_composio() -> Composio:
    return Composio(api_key=get_settings().composio_api_key)


# Composio v3 requires explicit toolkit versions for manual tool execution.
# Keep the defaults pinned to versions observed in production and allow env
# overrides when Composio changes a toolkit without requiring a code deploy.
DEFAULT_TOOLKIT_VERSIONS: dict[str, str] = {
    "googledocs": "20260518_00",
    "googledrive": "20260615_00",
    "slack": "20260519_01",
    "gmail": "20260515_00",
    "googlecalendar": "20260429_00",
    "linear": "20260615_00",
    "github": "20260501_01",
    "hubspot": "20260501_00",
}


def _configured_toolkit_versions() -> dict[str, str]:
    versions = dict(DEFAULT_TOOLKIT_VERSIONS)
    raw = get_settings().composio_toolkit_versions.strip()
    if not raw:
        return versions
    for part in raw.split(","):
        if "=" not in part:
            continue
        toolkit, version = [x.strip() for x in part.split("=", 1)]
        if toolkit and version:
            versions[toolkit] = version
    return versions


def toolkit_for_tool_slug(slug: str) -> str | None:
    upper = slug.upper()
    if upper.startswith("GOOGLEDOCS_"):
        return "googledocs"
    if upper.startswith("GOOGLEDRIVE_"):
        return "googledrive"
    if upper.startswith("GOOGLECALENDAR_"):
        return "googlecalendar"
    if upper.startswith("SLACK_"):
        return "slack"
    if upper.startswith("GMAIL_"):
        return "gmail"
    if upper.startswith("LINEAR_"):
        return "linear"
    if upper.startswith("GITHUB_"):
        return "github"
    if upper.startswith("HUBSPOT_"):
        return "hubspot"
    return None


def tool_version_for_slug(slug: str) -> str | None:
    toolkit = toolkit_for_tool_slug(slug)
    if not toolkit:
        return None
    return _configured_toolkit_versions().get(toolkit)


def execute_composio_tool(
    composio: Composio,
    slug: str,
    *,
    arguments: dict,
    connected_account_id: str,
    user_id: str | None = None,
):
    kwargs = {
        "arguments": arguments,
        "connected_account_id": connected_account_id,
    }
    if user_id:
        kwargs["user_id"] = user_id
    version = tool_version_for_slug(slug)
    if version:
        kwargs["version"] = version
    return composio.tools.execute(slug, **kwargs)


# Supported apps: toolkit slug -> trigger names + sync policy defaults.
# Trigger slugs: https://docs.composio.dev/docs/toolkits
# gcal has no reliable webhook trigger — poll-primary via incremental sync.
SUPPORTED_APPS: dict[str, dict] = {
    "slack": {
        "toolkit": "slack",
        "triggers": ["SLACK_RECEIVE_MESSAGE"],
        "sync_policy": {"mode": "webhook", "gap_check_hours": 6, "freshness_hours": 12},
    },
    "gmail": {
        "toolkit": "gmail",
        "triggers": ["GMAIL_NEW_GMAIL_MESSAGE"],
        "sync_policy": {"mode": "webhook", "gap_check_hours": 6, "freshness_hours": 12},
    },
    "gcal": {
        "toolkit": "googlecalendar",
        "triggers": [],
        "sync_policy": {"mode": "poll", "poll_interval_minutes": 15, "freshness_hours": 2},
    },
    "googledocs": {
        "toolkit": "googledocs",
        "triggers": [
            {
                "slug": "GOOGLEDOCS_DOCUMENT_UPDATED_TRIGGER",
                "config": {"interval": 2, "max_results": 50},
            }
        ],
        "sync_policy": {"mode": "webhook", "gap_check_hours": 1, "freshness_hours": 6},
    },
    "googledrive": {
        "toolkit": "googledrive",
        "triggers": [
            {
                "slug": "GOOGLEDRIVE_GOOGLE_DRIVE_CHANGES",
                "config": {"interval": 5, "include_items_from_all_drives": True, "spaces": "drive"},
            }
        ],
        "sync_policy": {"mode": "webhook", "gap_check_hours": 6, "freshness_hours": 12},
    },
    "linear": {
        "toolkit": "linear",
        # Composio exposes Linear trigger definitions, but production trigger
        # creation requires an app-level Linear webhook endpoint. Use polling for
        # the MVP so a connected account is enough to read Linear issues.
        "triggers": [],
        "sync_policy": {"mode": "poll", "poll_interval_minutes": 15, "freshness_hours": 24},
    },
    "github": {
        "toolkit": "github",
        "triggers": ["GITHUB_PULL_REQUEST_EVENT"],
        "sync_policy": {"mode": "webhook", "gap_check_hours": 12, "freshness_hours": 24},
    },
    "hubspot": {
        "toolkit": "hubspot",
        # Composio exposes broad HubSpot triggers, but the first CRM version is
        # intentionally poll-primary so OAuth can stay read-only.
        "triggers": [],
        "sync_policy": {"mode": "poll", "poll_interval_minutes": 30, "freshness_hours": 24},
    },
}

# Trigger slug prefix -> internal source_type
TRIGGER_PREFIXES: list[tuple[str, str]] = [
    ("SLACK_", "slack"),
    ("GMAIL_", "gmail"),
    ("GOOGLECALENDAR_", "gcal"),
    ("GOOGLEDOCS_", "googledocs"),
    ("GOOGLEDRIVE_", "googledrive"),
    ("LINEAR_", "linear"),
    ("GITHUB_", "github"),
    ("HUBSPOT_", "hubspot"),
]


def trigger_to_source(trigger_slug: str) -> str | None:
    for prefix, source in TRIGGER_PREFIXES:
        if trigger_slug.upper().startswith(prefix):
            return source
    return None
