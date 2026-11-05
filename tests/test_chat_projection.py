from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json

import pytest

import app.synthesis.chat as chat_module
from app.presentation.chat import _source_chips, project_chat_message, project_chat_response
from app.synthesis.chat import (
    _context_relevance_score,
    _deterministic_context_answer,
    _format_workstream_answer,
    _is_broad_operating_question,
    _is_source_health_question,
    _is_workstream_status_question,
    _requested_source_types,
    _retrieval_query_for_question,
    _select_workstream,
    _source_health_answer,
    _source_health_recovered_after_import_failure,
    _workstream_sources,
    strip_citations,
)
from app.synthesis.verifier import verify_inline_citations


COMPANY_PERMISSION = {"allowed_principals": ["company:tenant-1"]}
COMPANY_ACL = {
    "allowed_principals": ["company:tenant-1"],
    "acl_state": "stamped",
    "acl_freshness_state": "fresh",
}


def test_project_chat_response_adds_source_chips_and_missing_evidence():
    result = {
        "answer": (
            "The API launch is blocked. [ref:event:aaaaaaaa-1111-2222-3333-444444444444]\n\n"
            "Sources used:\n"
            "Missing: GitHub evidence for code completion."
        ),
        "sources": [
            {
                "ref": "event:aaaaaaaa-1111-2222-3333-444444444444",
                "source_url": "https://slack.com/archives/C1/p1",
                "source_observed_at": "2026-06-17T14:00:00+00:00",
                "preview": "API launch is blocked",
                **COMPANY_ACL,
            }
        ],
        "citations_valid": True,
        "uncited_claims": [],
        "trust_metadata": {"system_source_health": True, "permission": COMPANY_PERMISSION},
    }

    projected = project_chat_response(result)

    assert projected["display_answer"] == "The API launch is blocked."
    assert projected["source_chips"][0]["label"] == "Slack · #C1"
    assert projected["source_chips"][0]["source_observed_at"] == "2026-06-17T14:00:00+00:00"
    assert "preview" not in projected["source_chips"][0]
    assert projected["confidence_gaps"] == ["GitHub evidence for code completion"]
    assert projected["missing_evidence"] == projected["confidence_gaps"]


def test_project_chat_response_filters_raw_sources_to_destination_visible():
    result = {
        "answer": "The launch is blocked. [ref:event:company-source]",
        "sources": [
            {
                "ref": "event:company-source",
                "source_url": "https://docs.google.com/document/d/company",
                "source_title": "Company launch doc",
                **COMPANY_ACL,
            },
            {
                "ref": "event:private-source",
                "source_url": "https://mail.google.com/mail/u/0/#inbox/private",
                "source_title": "Private customer email",
                "allowed_principals": ["user:user-2"],
                "acl_state": "stamped",
                "acl_freshness_state": "fresh",
            },
        ],
        "citations_valid": True,
        "uncited_claims": [],
        "trust_metadata": {"permission": COMPANY_PERMISSION},
    }

    projected = project_chat_response(result)

    assert projected["sources"] == [result["sources"][0]]
    assert len(projected["source_chips"]) == 1
    assert projected["source_chips"][0]["label"] == "Company launch doc"
    assert "mail.google.com" not in str(projected)


def test_project_chat_response_filters_raw_answer_lines_to_destination_visible_sources():
    result = {
        "answer": (
            "Company launch is blocked. [ref:event:company-source]\n"
            "Private Gmail says the customer is angry. [ref:event:private-source]"
        ),
        "sources": [
            {
                "ref": "event:company-source",
                "source_url": "https://docs.google.com/document/d/company",
                **COMPANY_ACL,
            },
            {
                "ref": "event:private-source",
                "source_url": "https://mail.google.com/mail/u/0/#inbox/private",
                "allowed_principals": ["user:user-2"],
                "acl_state": "stamped",
                "acl_freshness_state": "fresh",
            },
        ],
        "citations_valid": True,
        "uncited_claims": [],
        "trust_metadata": {"permission": COMPANY_PERMISSION},
    }

    projected = project_chat_response(result)

    assert "Company launch is blocked" in projected["answer"]
    assert "Private Gmail" not in projected["answer"]
    assert "Private Gmail" not in projected["display_answer"]
    assert "mail.google.com" not in str(projected)


def test_project_chat_response_sanitizes_raw_trust_metadata():
    result = {
        "answer": "Company answer.",
        "sources": [],
        "citations_valid": True,
        "uncited_claims": [],
        "trust_metadata": {
            "citations_valid": True,
            "confidence_gaps": ["Private source at https://mail.google.com/mail/u/0/#inbox/private from maya@example.com"],
            "permission": COMPANY_PERMISSION,
            "debug_source_url": "https://mail.google.com/mail/u/0/#inbox/private",
            "source_identity_hints": ["maya@example.com"],
        },
    }

    projected = project_chat_response(result)

    assert "debug_source_url" not in projected["trust_metadata"]
    assert "source_identity_hints" not in projected["trust_metadata"]
    assert "[redacted-url]" in projected["trust_metadata"]["confidence_gaps"][0]
    assert "[redacted-email]" in projected["trust_metadata"]["confidence_gaps"][0]
    assert "mail.google.com" not in str(projected)
    assert "maya@example.com" not in str(projected)


def test_project_chat_response_hides_internal_permission_metadata():
    result = {
        "answer": "Company answer.",
        "sources": [],
        "citations_valid": True,
        "uncited_claims": [],
        "trust_metadata": {
            "citations_valid": True,
            "permission": {
                "policy_version": "permissioned-company-brain-v1",
                "actor_user_id": "user-secret",
                "requester_user_id": "user-secret",
                "target_audience": "company",
                "destination": "ask_discuss",
                "purpose": "chat",
                "allowed_principals": ["company:tenant-1"],
            },
        },
    }

    projected = project_chat_response(result)

    assert projected["trust_metadata"]["permission"] == {
        "policy_version": "permissioned-company-brain-v1",
        "target_audience": "company",
        "allowed_principals": ["company:tenant-1"],
    }
    assert "user-secret" not in str(projected)


def test_project_chat_message_normalizes_json_sources():
    row = {
        "id": "msg-1",
        "role": "assistant",
        "text": "No answer.\nMissing: a current source.",
        "sources": '[{"source_url":"https://linear.app/exampleco/issue/ENG-1/x","allowed_principals":["company:tenant-1"],"acl_state":"stamped","acl_freshness_state":"fresh"}]',
        "trust_metadata": '{"confidence_gaps":["stored gap"],"citations_valid":false,"permission":{"allowed_principals":["company:tenant-1"]}}',
        "created_at": "2026-06-17T12:00:00Z",
    }

    projected = project_chat_message(row)

    assert projected["display_text"] == "No answer."
    assert projected["source_chips"][0]["label"] == "Linear · ENG-1"
    assert projected["confidence_gaps"] == ["stored gap"]


def test_project_chat_message_filters_raw_sources_to_destination_visible():
    row = {
        "id": "msg-1",
        "role": "assistant",
        "text": "Company answer.",
        "sources": json.dumps([
            {
                "ref": "event:company-source",
                "source_url": "https://linear.app/exampleco/issue/ENG-1/x",
                **COMPANY_ACL,
            },
            {
                "ref": "event:private-source",
                "source_url": "https://mail.google.com/mail/u/0/#inbox/private",
                "allowed_principals": ["user:user-2"],
                "acl_state": "stamped",
                "acl_freshness_state": "fresh",
            },
        ]),
        "trust_metadata": json.dumps({
            "permission": COMPANY_PERMISSION,
            "citations_valid": True,
        }),
        "created_at": "2026-06-17T12:00:00Z",
    }

    projected = project_chat_message(row)

    assert len(projected["sources"]) == 1
    assert projected["sources"][0]["ref"] == "event:company-source"
    assert len(projected["source_chips"]) == 1
    assert "mail.google.com" not in str(projected)


def test_project_chat_message_filters_raw_text_lines_to_destination_visible_sources():
    row = {
        "id": "msg-1",
        "role": "assistant",
        "text": (
            "Company launch is blocked. [ref:event:company-source]\n"
            "Private Gmail says the customer is angry. [ref:event:private-source]"
        ),
        "sources": json.dumps([
            {
                "ref": "event:company-source",
                "source_url": "https://docs.google.com/document/d/company",
                **COMPANY_ACL,
            },
            {
                "ref": "event:private-source",
                "source_url": "https://mail.google.com/mail/u/0/#inbox/private",
                "allowed_principals": ["user:user-2"],
                "acl_state": "stamped",
                "acl_freshness_state": "fresh",
            },
        ]),
        "trust_metadata": json.dumps({
            "permission": COMPANY_PERMISSION,
            "citations_valid": True,
        }),
        "created_at": "2026-06-17T12:00:00Z",
    }

    projected = project_chat_message(row)

    assert "Company launch is blocked" in projected["text"]
    assert "Private Gmail" not in projected["text"]
    assert "Private Gmail" not in projected["display_text"]
    assert "mail.google.com" not in str(projected)


def test_project_chat_message_sanitizes_raw_trust_metadata():
    row = {
        "id": "msg-1",
        "role": "assistant",
        "text": "Company answer.",
        "sources": "[]",
        "trust_metadata": json.dumps({
            "citations_valid": True,
            "confidence_gaps": ["Private source at https://mail.google.com/mail/u/0/#inbox/private from maya@example.com"],
            "permission": COMPANY_PERMISSION,
            "debug_source_url": "https://mail.google.com/mail/u/0/#inbox/private",
            "source_identity_hints": ["maya@example.com"],
        }),
        "created_at": "2026-06-17T12:00:00Z",
    }

    projected = project_chat_message(row)

    assert "debug_source_url" not in projected["trust_metadata"]
    assert "source_identity_hints" not in projected["trust_metadata"]
    assert "[redacted-url]" in projected["trust_metadata"]["confidence_gaps"][0]
    assert "[redacted-email]" in projected["trust_metadata"]["confidence_gaps"][0]
    assert "mail.google.com" not in str(projected)
    assert "maya@example.com" not in str(projected)


def test_project_chat_message_flags_cited_source_without_url():
    row = {
        "id": "msg-1",
        "role": "assistant",
        "text": "The object exists [ref:object:obj-1].",
        "sources": '[{"ref":"object:obj-1"}]',
        "trust_metadata": "{}",
        "created_at": "2026-06-17T12:00:00Z",
    }

    projected = project_chat_message(row)

    assert projected["display_text"] == "The object exists."
    assert projected["source_chips"] == []
    assert projected["confidence_gaps"] == []


def test_project_chat_response_does_not_flag_source_health_identity_without_url():
    result = {
        "answer": "Source health\n- Slack: live.\n\nSources used:\nMissing: nothing material.",
        "sources": [
            {
                "ref": "source:source-slack",
                "source_title": "Source health · Slack",
                "source_url": None,
                "source_type": "slack",
                "source_id": "source-slack",
                "source_external_id": "T123",
                "source_identity_hints": ["T123"],
                **COMPANY_ACL,
            }
        ],
        "citations_valid": True,
        "uncited_claims": [],
        "trust_metadata": {"system_source_health": True, "permission": COMPANY_PERMISSION},
    }

    projected = project_chat_response(result)

    assert projected["source_chips"][0]["label"] == "Source health · Slack"
    assert projected["confidence_gaps"] == []


def test_project_chat_response_flags_source_health_title_without_system_trust():
    result = {
        "answer": "The launch is blocked [ref:source:source-slack].",
        "sources": [
            {
                "ref": "source:source-slack",
                "source_title": "Source health · Slack",
                "source_url": None,
                "source_type": "slack",
                "source_id": "source-slack",
                "source_external_id": "T123",
                "source_identity_hints": ["T123"],
                **COMPANY_ACL,
            }
        ],
        "citations_valid": True,
        "uncited_claims": [],
        "trust_metadata": {"permission": COMPANY_PERMISSION},
    }

    projected = project_chat_response(result)

    assert projected["source_chips"][0]["label"] == "Source health · Slack"
    assert projected["confidence_gaps"] == ["Source unavailable for source:source-slack"]


def test_project_chat_projection_strips_any_inline_ref_shape():
    result = {
        "answer": (
            "The plan changed. [ref:normalized_event:event-with-hyphen]\n"
            "The owner is unclear. [ref:brain_object:object_with_underscore]\n\n"
            "Sources used:\n"
            "Missing: nothing material."
        ),
        "sources": [
            {
                "ref": "normalized_event:event-with-hyphen",
                "source_url": "https://docs.google.com/document/d/demo",
            }
        ],
        "citations_valid": True,
        "uncited_claims": [],
    }

    projected = project_chat_response(result)

    assert "[ref:" not in projected["display_answer"]
    assert projected["display_answer"] == "The plan changed.\nThe owner is unclear."


def test_strip_citations_handles_refs_in_confidence_gap_text():
    text = "GitHub is not connected. [ref:alert:19d025a1-70af-44ac-afe5-1f7c7cd04386]"

    assert strip_citations(text) == "GitHub is not connected."


def test_project_chat_response_marks_degraded_answers():
    result = {
        "answer": (
            "Accuracy launch moved to July 8. [ref:raw:demo-slack-accuracy]\n\n"
            "Sources used:\n"
            "Missing: additional connected sources may improve this answer."
        ),
        "sources": [
            {
                "ref": "raw:demo-slack-accuracy",
                "source_url": "https://slack.com/archives/C1/p1",
            }
        ],
        "citations_valid": True,
        "uncited_claims": [],
        "trust_metadata": {
            "llm_degraded": True,
            "confidence_gaps": ["Model provider unavailable; answered from retrieved sources only."],
        },
    }

    projected = project_chat_response(result)

    assert projected["degraded"] is True
    assert projected["degraded_reason"] == "Model provider unavailable. Answered from retrieved sources only."
    assert projected["confidence_gaps"] == ["Model provider unavailable; answered from retrieved sources only."]


def test_deterministic_context_answer_is_source_cited():
    pack = [
        {
            "kind": "raw",
            "id": "demo-slack-accuracy",
            "text": "Accuracy launch moved to July 8 because API credentials are still missing.",
            "source_url": "https://slack.example.test",
        }
    ]

    answer = _deterministic_context_answer(pack)
    result = verify_inline_citations(answer, pack, require_relevance=True)

    assert result["valid"]
    assert result["cited"] == ["raw:demo-slack-accuracy"]


def test_deterministic_context_answer_hides_raw_alert_syntax():
    pack = [
        {
            "kind": "alert",
            "id": "alert-1",
            "text": (
                "[alert:deadline_risk] workstream=Accuracy Project "
                "claim=Accuracy launch is likely to slip. "
                "why_now=API credentials are still missing. "
                "next_action=Michelle should assign an owner."
            ),
        }
    ]

    answer = _deterministic_context_answer(pack)

    assert "[alert:" not in answer
    assert "workstream=" not in answer
    assert "Accuracy Project: Accuracy launch is likely to slip." in answer
    assert "Why now: API credentials are still missing." in answer


def test_context_relevance_rejects_unrelated_private_data_query():
    pack = [
        {
            "kind": "alert",
            "id": "alert-1",
            "text": "Accuracy Project launch moved to July 8 because API credentials are missing.",
        }
    ]

    score = _context_relevance_score("What private customer emails mention salaries?", pack)

    assert score == 0


def test_context_relevance_accepts_matching_project_query():
    pack = [
        {
            "kind": "alert",
            "id": "alert-1",
            "text": "Accuracy Project launch moved to July 8 because API credentials are missing.",
        }
    ]

    score = _context_relevance_score("What's latest on the Accuracy Project?", pack)

    assert score > 0.18


def test_requested_source_types_detects_missing_source_queries():
    assert _requested_source_types("Is this on track based on GitHub commits?") == {"github"}
    assert _requested_source_types("Quote the private Gmail thread about Acme") == {"gmail"}
    assert _requested_source_types("Are the Google Drive files connected?") == {"googledrive"}
    assert _requested_source_types("What does HubSpot say about the Acme renewal pipeline?") == {"hubspot"}


def test_requested_source_retrieval_query_drops_source_and_generic_terms():
    query = _retrieval_query_for_question(
        "What is the latest on Project Phoenix from Gmail?",
        broad_operating_question=False,
        requested_sources={"gmail"},
    )

    assert query == "phoenix"


def test_source_health_questions_are_detected():
    assert _is_source_health_question("Which sources are stale or missing?") is True
    assert _is_source_health_question("What's latest on Accuracy?") is False


def test_source_health_recovery_requires_real_source_freshness():
    row = {
        "status": "live",
        "sync_policy": {"identity_hints": ["ExampleCo"]},
        "last_backfill_status": "failed",
        "fresh_source_at": None,
        "last_data_at": datetime(2026, 6, 18, 9, 30, tzinfo=timezone.utc),
        "stale_since": None,
        "degraded_reason": None,
    }

    assert _source_health_recovered_after_import_failure(row) is False

    row["fresh_source_at"] = datetime(2026, 6, 18, 9, 31, tzinfo=timezone.utc)
    assert _source_health_recovered_after_import_failure(row) is True


def test_source_health_chips_keep_account_identity_without_url():
    chips = _source_chips([
        {
            "ref": "source:source-slack",
            "source_title": "Source health · Slack",
            "source_url": None,
            "source_type": "slack",
            "source_id": "source-slack",
            "source_external_id": "T123",
            "source_identity_hints": ["T123", "ExampleCo"],
            **COMPANY_ACL,
        }
    ], COMPANY_PERMISSION)

    assert chips == [
        {
            "label": "Source health · Slack",
            "url": None,
            "ref": "source:source-slack",
            "source_observed_at": None,
            "source_type": "slack",
            "source_id": "source-slack",
            "source_external_id": "T123",
            "source_identity_hints": ["T123", "ExampleCo"],
        }
    ]


class FakeSourceHealthConn:
    def __init__(self):
        self.fetches = []

    async def fetch(self, query, *args):
        self.fetches.append((query, args))
        if "FROM sources s" not in query:
            raise AssertionError(query)
        return [
            {
                "id": "source-slack",
                "source_type": "slack",
                "status": "live",
                "external_id": "T123",
                "sync_policy": {"identity_hints": ["ExampleCo"]},
                "fresh_source_at": datetime(2026, 6, 18, 11, 0, tzinfo=timezone.utc),
                "last_data_at": datetime(2026, 6, 18, 11, 0, tzinfo=timezone.utc),
                "last_backfill_status": "completed",
                "stale_since": None,
                "degraded_reason": None,
            },
            {
                "id": "source-docs",
                "source_type": "googledocs",
                "status": "live",
                "external_id": "docs-account",
                "sync_policy": {"identity_hints": ["ExampleCo"]},
                "fresh_source_at": datetime(2026, 6, 18, 9, 30, tzinfo=timezone.utc),
                "last_data_at": datetime(2026, 6, 18, 9, 30, tzinfo=timezone.utc),
                "last_backfill_status": "completed",
                "stale_since": None,
                "degraded_reason": None,
            },
            {
                "id": "source-drive",
                "source_type": "googledrive",
                "status": "live",
                "external_id": "drive-account",
                "sync_policy": {"identity_hints": ["ExampleCo"]},
                "fresh_source_at": datetime(2026, 6, 18, 8, 15, tzinfo=timezone.utc),
                "last_data_at": datetime(2026, 6, 18, 8, 15, tzinfo=timezone.utc),
                "last_backfill_status": "failed",
                "stale_since": None,
                "degraded_reason": None,
            },
            {
                "id": "source-linear",
                "source_type": "linear",
                "status": "degraded",
                "external_id": "linear-org-1",
                "sync_policy": {"identity_hints": ["ExampleCo"]},
                "fresh_source_at": datetime(2026, 6, 17, 11, 0, tzinfo=timezone.utc),
                "last_data_at": datetime(2026, 6, 17, 11, 0, tzinfo=timezone.utc),
                "last_backfill_status": "completed",
                "stale_since": datetime(2026, 6, 18, 10, 0, tzinfo=timezone.utc),
                "degraded_reason": "No recent events",
            },
        ]


@asynccontextmanager
async def fake_source_health_acquire():
    yield FakeSourceHealthConn()


@pytest.mark.asyncio
async def test_source_health_answer_uses_connection_state(monkeypatch):
    conn = FakeSourceHealthConn()

    @asynccontextmanager
    async def acquire():
        yield conn

    monkeypatch.setattr(chat_module, "acquire", acquire)

    answer, trust, sources = await _source_health_answer("tenant-1")

    query, args = conn.fetches[0]
    assert args == ("tenant-1", ["company:tenant-1"])
    assert "FROM source_resources sr" in query
    assert "sr.acl_state = 'stamped'" in query
    assert "sr.acl_freshness_state = 'fresh'" in query
    assert "sr.allowed_principals ?| $2::text[]" in query
    assert "- Slack: live, last data Jun 18, 11:00 AM UTC, backfill completed." in answer
    assert "- Google Docs: live, last data Jun 18, 9:30 AM UTC, backfill completed." in answer
    assert "- Google Drive: live, last data Jun 18, 8:15 AM UTC, backfill completed." in answer
    assert "- Gmail: missing." in answer
    assert "- Calendar: missing." in answer
    assert "Linear: degraded, last data Jun 17, 11:00 AM UTC, backfill completed, stale since Jun 18, 10:00 AM UTC, source details require source access" in answer
    assert "No recent events" not in answer
    assert "2026-06-18T" not in answer
    assert trust["system_source_health"] is True
    assert trust["permission"]["allowed_principals"] == ["company:tenant-1"]
    assert "Gmail is not connected." in trust["confidence_gaps"]
    assert sources[0]["source_id"] == "source-slack"
    assert sources[0]["source_external_id"] is None
    assert sources[0]["source_identity_hints"] == []
    assert sources[0]["allowed_principals"] == ["company:tenant-1"]
    assert sources[0]["acl_state"] == "stamped"
    assert sources[0]["acl_freshness_state"] == "fresh"
    assert "T123" not in str(sources)


def test_broad_operating_question_can_use_general_context():
    pack = [{"kind": "alert", "id": "a1", "text": "API Credentials are blocked."}]

    assert _is_broad_operating_question("What changed overnight?", pack) is True
    assert _is_broad_operating_question("Tell me about salaries", pack) is False


def test_workstream_question_selects_matching_workstream():
    workstreams = [
        {
            "key": "project:linear hygiene",
            "title": "Linear Hygiene",
            "about": "Linear issue cleanup",
            "latest_change": "Old tickets were cleaned up.",
            "next_action": "Review stale issues.",
        },
        {
            "key": "project:accuracy project",
            "title": "Accuracy Project",
            "about": "API credential launch risk",
            "latest_change": "API credentials are still missing.",
            "next_action": "Assign customer notification owner.",
        },
    ]

    assert _is_workstream_status_question("What's latest on the Accuracy Project?") is True
    selected = _select_workstream("What's latest on the Accuracy Project?", workstreams)

    assert selected["key"] == "project:accuracy project"


def test_workstream_answer_formats_status_delta_and_missing_evidence():
    answer = _format_workstream_answer({
        "title": "Accuracy Project",
        "status": "at_risk",
        "delta": "worsened",
        "delta_reason": "Latest signal is higher severity than earlier delivered signals.",
        "latest_change": "API credentials have not arrived.",
        "next_action": "Michelle should assign an owner to notify Acme.",
        "status_reason": "High-severity signal is active.",
        "last_source_observed_at": "2026-06-18T12:00:00+00:00",
        "people_rows": [
            {
                "person": "Maya",
                "contribution": "Maya is driving launch recovery.",
            }
        ],
        "missing_evidence": ["GitHub is not connected."],
    })

    assert "## Accuracy Project" in answer
    assert "Status: At risk." in answer
    assert "Delta: Worsened." in answer
    assert "What changed: API credentials have not arrived." in answer
    assert "People: Maya" in answer
    assert "Missing: GitHub is not connected." in answer


def test_workstream_sources_require_existing_destination_visible_acl():
    workstream = {
        "last_source_observed_at": "2026-06-23T12:00:00Z",
        "source_chips": [
            {
                "ref": "event:company",
                "url": "https://docs.google.com/document/d/company",
                "label": "Company Doc",
                **COMPANY_ACL,
            },
            {
                "ref": "event:legacy",
                "url": "https://mail.google.com/mail/u/0/#inbox/private",
                "label": "Private Gmail",
            },
            {
                "ref": "event:stale",
                "url": "https://docs.google.com/document/d/stale",
                "label": "Stale Doc",
                "allowed_principals": ["company:tenant-1"],
                "acl_state": "stamped",
                "acl_freshness_state": "stale",
            },
        ],
    }

    sources = _workstream_sources("tenant-1", workstream)

    assert [source["ref"] for source in sources] == ["event:company"]
    assert "mail.google.com" not in str(sources)
    assert "Stale Doc" not in str(sources)
