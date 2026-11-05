from pathlib import Path

from app.core.permission_adapters import provider_acl_to_envelope
from tests.fixtures.permission_corpus import CORPUS_PRINCIPAL_MAPPINGS, PERMISSION_FIXTURES, PERMISSION_PAIRWISE_CASES


def test_provider_adapters_match_permission_corpus_envelopes():
    for case in PERMISSION_FIXTURES + PERMISSION_PAIRWISE_CASES:
        if "legacy_fail_closed" in case["tags"]:
            continue

        assert provider_acl_to_envelope(
            case["source_type"],
            case["provider_acl_payload"],
            principal_mappings=CORPUS_PRINCIPAL_MAPPINGS,
        ) == case["expected_envelope"], case["id"]


def test_unknown_provider_principals_fail_closed():
    cases = [
        case for case in PERMISSION_FIXTURES
        if "unknown_principal" in case["provider_principal_types"]
    ]

    assert cases
    for case in cases:
        envelope = provider_acl_to_envelope(
            case["source_type"],
            case["provider_acl_payload"],
            principal_mappings=CORPUS_PRINCIPAL_MAPPINGS,
        )
        assert envelope["visibility"] == "restricted"
        assert envelope["acl_state"] == "needs_permission_review"
        assert envelope["acl_confidence"] == "low"
        assert envelope["acl_freshness_state"] == "needs_permission_review"


def test_personal_sources_do_not_promote_participants_to_company_visibility():
    gmail_case = next(case for case in PERMISSION_FIXTURES if case["id"] == "gmail_naman_customer_thread")

    envelope = provider_acl_to_envelope("gmail", gmail_case["provider_acl_payload"])

    assert envelope["visibility"] == "private"
    assert envelope["allowed_principals"] == ["user:founder"]
    assert "buyer@example.com" not in envelope["allowed_principals"]
    assert "company:exampleco" not in envelope["allowed_principals"]


def test_drive_docs_explicit_revocation_and_freshness_are_preserved():
    base_payload = {
        "fileId": "doc-revoked",
        "permissions": [{"type": "domain", "domain": "company.com", "role": "reader"}],
    }

    for extra in (
        {"acl_freshness_state": "revoked"},
        {"access_revoked_at": "2026-06-23T00:00:00Z"},
    ):
        envelope = provider_acl_to_envelope(
            "googledocs",
            {**base_payload, **extra},
            principal_mappings=CORPUS_PRINCIPAL_MAPPINGS,
        )
        assert envelope["visibility"] == "company"
        assert envelope["allowed_principals"] == ["company:exampleco"]
        assert envelope["acl_state"] == "stamped"
        assert envelope["acl_freshness_state"] == "revoked"

    stale = provider_acl_to_envelope(
        "googledrive",
        {**base_payload, "acl_freshness_state": "stale"},
        principal_mappings=CORPUS_PRINCIPAL_MAPPINGS,
    )
    assert stale["acl_freshness_state"] == "stale"


def test_drive_domain_permission_without_domain_uses_verified_domain_hint_only():
    payload = {
        "fileId": "drive-domain-null",
        "connected_account": {"email": "naman@company.com"},
        "permissions": [
            {"id": "perm-domain", "type": "domain", "domain": None, "role": "reader"},
            {"id": "perm-owner", "type": "user", "emailAddress": None, "role": "owner"},
        ],
    }

    missing_hint = provider_acl_to_envelope(
        "googledrive",
        payload,
        principal_mappings=CORPUS_PRINCIPAL_MAPPINGS,
    )
    assert missing_hint["visibility"] == "restricted"
    assert missing_hint["acl_state"] == "needs_permission_review"

    with_hint = provider_acl_to_envelope(
        "googledrive",
        {**payload, "permission_domain_hints": {"perm-domain": "company.com"}},
        principal_mappings=CORPUS_PRINCIPAL_MAPPINGS,
    )
    assert with_hint["visibility"] == "company"
    assert with_hint["allowed_principals"] == ["company:exampleco", "user:founder"]
    assert with_hint["acl_state"] == "stamped"


def test_calendar_non_private_event_inherits_user_and_domain_acl():
    alice_only = provider_acl_to_envelope(
        "gcal",
        {
            "event": {"visibility": "default"},
            "calendar_acl": [
                {"scope": {"type": "user", "value": "c_fixture@group.calendar.google.com"}, "role": "owner"},
                {"scope": {"type": "default"}, "role": "none"},
                {"scope": {"type": "user", "value": "alice@company.com"}, "role": "reader"},
            ],
        },
        principal_mappings={
            ("google", "user", "alice@company.com"): "user:alice",
            ("google", "domain", "company.com"): "company:exampleco",
        },
    )
    assert alice_only["visibility"] == "private"
    assert alice_only["allowed_principals"] == ["user:alice"]
    assert alice_only["acl_state"] == "stamped"

    company = provider_acl_to_envelope(
        "gcal",
        {
            "event": {"visibility": "default"},
            "calendar_acl": [
                {"scope": {"type": "domain", "value": "company.com"}, "role": "reader"},
            ],
        },
        principal_mappings={
            ("google", "user", "alice@company.com"): "user:alice",
            ("google", "domain", "company.com"): "company:exampleco",
        },
    )
    assert company["visibility"] == "company"
    assert company["allowed_principals"] == ["company:exampleco"]


def test_provider_adapters_fail_closed_without_principal_mappings():
    slack_case = next(case for case in PERMISSION_FIXTURES if case["id"] == "slack_company_announcements")
    hubspot_case = next(case for case in PERMISSION_FIXTURES if case["id"] == "hubspot_all_user_company_record")

    for case in (slack_case, hubspot_case):
        envelope = provider_acl_to_envelope(case["source_type"], case["provider_acl_payload"])
        assert envelope["visibility"] == "restricted"
        assert envelope["acl_state"] == "needs_permission_review"
        assert envelope["allowed_principals"][0].startswith("unknown:")


def test_slack_missing_channel_privacy_fails_closed_even_with_workspace_mapping():
    envelope = provider_acl_to_envelope(
        "slack",
        {
            "team": {"id": "T_EXAMPLECO"},
            "channel": {"id": "C_UNKNOWN"},
        },
        principal_mappings=CORPUS_PRINCIPAL_MAPPINGS,
    )

    assert envelope["visibility"] == "restricted"
    assert envelope["acl_state"] == "needs_permission_review"
    assert envelope["acl_confidence"] == "low"


def test_principal_mapping_migration_is_rls_protected():
    sql = (Path(__file__).resolve().parents[1] / "migrations" / "0036_principal_identity_mappings.sql").read_text()

    assert "CREATE TABLE IF NOT EXISTS principal_identity_mappings" in sql
    assert "UNIQUE (tenant_id, source_type, provider_principal_type, provider_principal_id)" in sql
    assert "ALTER TABLE principal_identity_mappings ENABLE ROW LEVEL SECURITY" in sql
    assert "ALTER TABLE principal_identity_mappings FORCE ROW LEVEL SECURITY" in sql
    assert "CREATE POLICY tenant_isolation ON principal_identity_mappings" in sql
