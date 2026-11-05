"""
Source-specific normalization: raw payload -> NormalizedEvent or None.

None means the event carries no content worth processing (joins, typing
indicators, empty bodies). The raw event is still retained either way.

Composio payload shapes vary between trigger versions, so every field read
is defensive with fallbacks — same approach that survived the V3 migration.
"""
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone

import structlog

from app.core.source_permissions import sanitize_github_pull_request

logger = structlog.get_logger()

SLACK_MENTION_RE = re.compile(r"<@([A-Z0-9]+)>")


@dataclass
class NormalizedEvent:
    source_type: str
    actor_label: str | None
    occurred_at: datetime
    text: str
    thread_key: str | None = None
    channel_key: str | None = None
    source_url: str | None = None
    entities: list[dict] = field(default_factory=list)   # candidate mentions, pre-resolution
    metadata: dict = field(default_factory=dict)


def normalize(source_type: str, payload: dict) -> NormalizedEvent | None:
    handler = {
        "slack": _normalize_slack,
        "gmail": _normalize_gmail,
        "gcal": _normalize_gcal,
        "googledocs": _normalize_googledocs,
        "googlesheets": _normalize_googlesheets,
        "googledrive": _normalize_googledrive,
        "linear": _normalize_linear,
        "github": _normalize_github,
        "hubspot": _normalize_hubspot,
        "transcript": _normalize_transcript,
        "meeting": _normalize_meeting,
        "capture": _normalize_capture,
    }.get(source_type)
    if not handler:
        logger.warning("no_normalizer", source_type=source_type)
        return None
    try:
        return handler(payload)
    except Exception as e:
        logger.error("normalize_failed", source_type=source_type, error=str(e), exc_info=True)
        raise


def _first(d: dict, *keys, default=None):
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return v
    return default


def _ts_to_dt(value) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    s = str(value)
    if re.fullmatch(r"\d{10,13}(\.\d+)?", s):
        ts = float(s)
        if ts > 1e12:  # milliseconds
            ts /= 1000
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)


def _normalize_slack(payload: dict) -> NormalizedEvent | None:
    # Composio SLACK_RECEIVE_MESSAGE data, or a native Slack event wrapper
    event = payload.get("event", payload)
    channel_payload = payload.get("channel") if isinstance(payload.get("channel"), dict) else {}
    team_payload = payload.get("team") if isinstance(payload.get("team"), dict) else {}
    subtype = event.get("subtype", "")
    if subtype in ("channel_join", "channel_leave", "bot_add", "bot_remove"):
        return None
    text = _first(event, "text", default="")
    if not text.strip():
        return None

    channel = _first(event, "channel", "channel_id", default="")
    ts = _first(event, "ts", "event_ts", "timestamp")
    thread_ts = event.get("thread_ts") or ts
    user = _first(event, "user", "user_id", "bot_id")
    team = _first(event, "team", "team_id", default="")

    mentions = [{"type": "person", "handle_type": "slack", "handle": m} for m in SLACK_MENTION_RE.findall(text)]
    if user:
        mentions.append({"type": "person", "handle_type": "slack", "handle": user, "is_actor": True})

    permalink = None
    if channel and ts:
        permalink = f"https://app.slack.com/client/{team}/{channel}" if team else None
        slack_ts = str(ts).replace(".", "")
        permalink = f"https://slack.com/archives/{channel}/p{slack_ts}"

    return NormalizedEvent(
        source_type="slack",
        actor_label=f"slack:{user}" if user else None,
        occurred_at=_ts_to_dt(ts),
        text=text,
        thread_key=f"{channel}:{thread_ts}" if channel else None,
        channel_key=channel or None,
        source_url=permalink,
        entities=mentions,
        metadata={
            "channel_type": event.get("channel_type"),
            "subtype": subtype or None,
            "channel_id": channel or channel_payload.get("id"),
            "channel_name": channel_payload.get("name"),
            "channel_is_private": channel_payload.get("is_private"),
            "team_id": team or team_payload.get("id"),
        },
    )


def _normalize_gmail(payload: dict) -> NormalizedEvent | None:
    # Composio GMAIL_NEW_GMAIL_MESSAGE data
    text = _first(payload, "message_text", "messageText", "snippet", "body", default="")
    subject = _first(payload, "subject", default="")
    if not text.strip() and not subject.strip():
        return None

    sender = _first(payload, "sender", "from", "from_email", default="")
    to = _first(payload, "to", "recipient", default="") or ""
    thread_id = _first(payload, "thread_id", "threadId", default="")
    message_id = _first(payload, "message_id", "messageId", "id", default="")
    ts = _first(payload, "message_timestamp", "messageTimestamp", "internalDate", "date")

    entities = []
    for raw_addr in _split_addresses(sender) + _split_addresses(to):
        email = _extract_email(raw_addr)
        if email:
            entities.append({
                "type": "person", "handle_type": "email", "handle": email,
                "is_actor": raw_addr in _split_addresses(sender),
            })

    body = f"Subject: {subject}\n\n{text}" if subject else text
    return NormalizedEvent(
        source_type="gmail",
        actor_label=f"email:{_extract_email(sender)}" if sender else None,
        occurred_at=_ts_to_dt(ts),
        text=body,
        thread_key=f"gmail:{thread_id}" if thread_id else None,
        channel_key=_first(payload, "label_ids", "labelIds", default=None) and "inbox" or None,
        source_url=f"https://mail.google.com/mail/u/0/#all/{thread_id or message_id}" if (thread_id or message_id) else None,
        entities=entities,
        metadata={"subject": subject or None, "to": to or None},
    )


def _normalize_gcal(payload: dict) -> NormalizedEvent | None:
    # Google Calendar event resource (from polling sync)
    if payload.get("status") == "cancelled":
        summary = payload.get("summary") or "(cancelled event)"
        text = f"Calendar event cancelled: {summary}"
    else:
        summary = _first(payload, "summary", default="")
        if not summary:
            return None
        description = _first(payload, "description", default="")
        text = f"Calendar event: {summary}"
        if description:
            text += f"\n{description}"

    start = payload.get("start", {})
    start_val = _first(start, "dateTime", "date") if isinstance(start, dict) else start
    attendees = payload.get("attendees") or []
    organizer = (payload.get("organizer") or {}).get("email", "")

    entities = []
    if organizer:
        entities.append({"type": "person", "handle_type": "email", "handle": organizer, "is_actor": True})
    for a in attendees:
        email = a.get("email") if isinstance(a, dict) else None
        if email:
            entities.append({"type": "person", "handle_type": "email", "handle": email})
            text_name = a.get("displayName")
            if text_name:
                entities[-1]["name"] = text_name

    attendee_list = ", ".join(e["handle"] for e in entities if e.get("handle_type") == "email")
    if attendee_list:
        text += f"\nAttendees: {attendee_list}"

    return NormalizedEvent(
        source_type="gcal",
        actor_label=f"email:{organizer}" if organizer else None,
        occurred_at=_ts_to_dt(start_val),
        text=text,
        thread_key=f"gcal:{payload.get('recurringEventId') or payload.get('id')}",
        channel_key=_first(payload, "calendar_id", "calendarId", default="primary"),
        source_url=payload.get("htmlLink"),
        entities=entities,
        metadata={"event_status": payload.get("status"), "recurring": bool(payload.get("recurringEventId"))},
    )


def _normalize_googledocs(payload: dict) -> NormalizedEvent | None:
    document = payload.get("document") or {}
    content = payload.get("document_content") or {}
    text = _first(content, "plain_text", "plainText", default="")
    title = _first(content, "title", default="") or _first(document, "name", default="")
    if not text.strip():
        return None

    document_id = _first(content, "document_id", "documentId", default="") or _first(document, "id", default="")
    modified = _first(document, "modifiedTime", "modified_time", default=None)
    source_url = (
        _first(content, "display_url", "displayUrl", default="")
        or _first(document, "webViewLink", "web_view_link", default="")
        or (f"https://docs.google.com/document/d/{document_id}/edit" if document_id else None)
    )
    modifier = document.get("lastModifyingUser") or document.get("last_modifying_user") or {}
    modifier_email = modifier.get("emailAddress") or modifier.get("email")
    entities = []
    if modifier_email:
        entities.append({
            "type": "person",
            "handle_type": "email",
            "handle": modifier_email.lower(),
            "name": modifier.get("displayName") or modifier.get("display_name"),
            "is_actor": True,
        })

    return NormalizedEvent(
        source_type="googledocs",
        actor_label=f"email:{modifier_email.lower()}" if modifier_email else None,
        occurred_at=_ts_to_dt(modified),
        text=f"Google Doc: {title}\n\n{text}",
        thread_key=f"googledocs:{document_id}" if document_id else None,
        channel_key=document_id or None,
        source_url=source_url,
        entities=entities,
        metadata={
            "title": title,
            "document_id": document_id or None,
            "modified_time": modified,
            "event_type": payload.get("event_type"),
        },
    )


def _normalize_googlesheets(payload: dict) -> NormalizedEvent | None:
    sheet = (
        payload.get("spreadsheet")
        or payload.get("sheet")
        or payload.get("file")
        or payload.get("metadata")
        or {}
    )
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    rows = (
        payload.get("rows")
        or data.get("rows")
        or payload.get("values")
        or data.get("values")
        or []
    )
    workbook_text = _sheet_workbook_text(
        payload.get("sheets")
        or payload.get("tabs")
        or payload.get("worksheets")
        or data.get("sheets")
        or data.get("tabs")
        or data.get("worksheets")
    )
    title = (
        _first(payload, "title", "name", default="")
        or _first(sheet, "title", "name", default="")
        or "Google Sheet"
    )
    text = _first(payload, "text", "plain_text", "plainText", "summary", default="")
    if not str(text or "").strip() and rows:
        text = _sheet_rows_text(rows)
    if not str(text or "").strip() and workbook_text:
        text = workbook_text
    if not str(text or "").strip() and not title:
        return None

    spreadsheet_id = (
        _first(payload, "spreadsheet_id", "spreadsheetId", "id", default="")
        or _first(sheet, "spreadsheet_id", "spreadsheetId", "id", default="")
    )
    modified = (
        _first(payload, "modifiedTime", "modified_time", "updated_at", "updatedAt", "occurred_at", default=None)
        or _first(sheet, "modifiedTime", "modified_time", "updated_at", "updatedAt", default=None)
    )
    source_url = (
        _first(payload, "url", "source_url", "sourceUrl", "webViewLink", "web_view_link", default="")
        or _first(sheet, "url", "source_url", "sourceUrl", "webViewLink", "web_view_link", default="")
        or (f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit" if spreadsheet_id else None)
    )
    modifier = payload.get("lastModifyingUser") or payload.get("last_modifying_user") or sheet.get("lastModifyingUser") or sheet.get("last_modifying_user") or {}
    modifier_email = None
    if isinstance(modifier, dict):
        modifier_email = modifier.get("emailAddress") or modifier.get("email")
    entities = []
    if modifier_email:
        entities.append({
            "type": "person",
            "handle_type": "email",
            "handle": modifier_email.lower(),
            "name": modifier.get("displayName") or modifier.get("display_name"),
            "is_actor": True,
        })

    body = str(text or "").strip()
    if title and title.lower() not in body[:200].lower():
        body = f"Google Sheet: {title}\n\n{body}" if body else f"Google Sheet: {title}"

    return NormalizedEvent(
        source_type="googlesheets",
        actor_label=f"email:{modifier_email.lower()}" if modifier_email else None,
        occurred_at=_ts_to_dt(modified),
        text=body,
        thread_key=f"googlesheets:{spreadsheet_id}" if spreadsheet_id else None,
        channel_key=spreadsheet_id or None,
        source_url=source_url,
        entities=entities,
        metadata={
            "title": title or None,
            "spreadsheet_id": spreadsheet_id or None,
            "row_count": len(rows) if isinstance(rows, list) else None,
            "modified_time": modified,
            "event_type": payload.get("event_type") or payload.get("type"),
        },
    )


def _sheet_rows_text(rows) -> str:
    if not isinstance(rows, list):
        return ""
    header_index = _sheet_header_index(rows)
    if header_index is not None:
        header = [str(cell or "").strip() for cell in rows[header_index]]
        mapped_rows = []
        for row in rows[header_index + 1:header_index + 81]:
            if not isinstance(row, list):
                continue
            mapped = {
                header[index]: row[index] if index < len(row) else ""
                for index in range(len(header))
                if header[index]
            }
            if any(value not in (None, "", []) for value in mapped.values()):
                mapped_rows.append(mapped)
        if mapped_rows:
            return _sheet_rows_text(mapped_rows)
    lines = []
    for index, row in enumerate(rows[:80], start=1):
        if isinstance(row, dict):
            normalized = {_sheet_key(key): value for key, value in row.items()}
            parts = []
            for label, keys in [
                ("Objective", ("objective_title", "objective", "okr")),
                ("Key Result", ("key_result_title", "key_result", "kr_title", "kr")),
                ("Milestone", ("milestone", "milestone_whats_due", "milestone_what_s_due", "title")),
                ("Owner", ("owner",)),
                ("Due", ("due_date", "date")),
                ("Measure", ("how_we_measure_it", "measurement_rule", "measure")),
                ("Status", ("status", "on_track")),
                ("Notes", ("notes_actual", "notes", "actual")),
            ]:
                value = _first_non_empty(normalized, keys)
                if value not in (None, "", []):
                    parts.append(f"{label}: {value}")
            if not parts:
                parts = [f"{key}: {value}" for key, value in list(row.items())[:8] if value not in (None, "", [])]
            if parts:
                lines.append(f"Row {index}. " + ". ".join(str(part)[:300] for part in parts))
            continue
        if isinstance(row, list):
            cells = [str(cell).strip() for cell in row[:12] if str(cell).strip()]
            if cells:
                lines.append(f"Row {index}. " + " | ".join(cells)[:600])
    return "\n".join(lines)[:12000]


def _sheet_workbook_text(workbook) -> str:
    tabs = _sheet_workbook_tabs(workbook)
    if not tabs:
        return ""
    lines = []
    for tab_name, rows in tabs[:12]:
        tab_text = _sheet_rows_text(rows)
        if not tab_text:
            continue
        lines.append(f"Tab: {tab_name}\n{tab_text}")
    return "\n\n".join(lines)[:12000]


def _sheet_workbook_tabs(workbook) -> list[tuple[str, list]]:
    if isinstance(workbook, dict):
        return [
            (str(name), rows)
            for name, rows in workbook.items()
            if isinstance(rows, list)
        ]
    if not isinstance(workbook, list):
        return []
    tabs = []
    for index, item in enumerate(workbook, start=1):
        if isinstance(item, dict):
            name = item.get("name") or item.get("title") or item.get("sheet_name") or item.get("tab") or f"Sheet {index}"
            rows = item.get("values") or item.get("rows") or item.get("data")
            if isinstance(rows, list):
                tabs.append((str(name), rows))
    return tabs


def _sheet_header_index(rows: list) -> int | None:
    for index, row in enumerate(rows[:10]):
        if not isinstance(row, list):
            continue
        normalized = {_sheet_key(cell) for cell in row if str(cell or "").strip()}
        has_milestone = bool(normalized & {"milestone", "milestone_whats_due", "milestone_what_s_due"})
        if has_milestone and ("date" in normalized or "owner" in normalized or "status" in normalized):
            return index
        if "key_result" in normalized and ("objective_title" in normalized or "objective" in normalized):
            return index
    return None


def _sheet_key(value) -> str:
    clean = str(value or "").strip().lower()
    clean = clean.replace("?", "").replace("/", " ")
    clean = clean.replace("(", " ").replace(")", " ").replace("'", " ").replace("’", " ")
    return "_".join(part for part in clean.replace("-", " ").split() if part)


def _first_non_empty(row: dict, keys: tuple[str, ...]):
    for key in keys:
        value = row.get(key)
        if value not in (None, "", []):
            return value
    return None


def _normalize_googledrive(payload: dict) -> NormalizedEvent | None:
    file_data = (
        payload.get("file")
        or payload.get("data")
        or payload.get("metadata")
        or payload
    )
    if not isinstance(file_data, dict):
        return None
    file_id = _first(file_data, "id", "file_id", "fileId", default="")
    name = _first(file_data, "name", "title", default="")
    mime_type = _first(file_data, "mimeType", "mime_type", default="")
    modified = _first(file_data, "modifiedTime", "modified_time", "modifiedDate", "updated_at", "updatedAt", default=None)
    description = _first(file_data, "description", default="")
    owners = file_data.get("owners") or []
    owner_labels = []
    entities = []
    for owner in owners:
        if not isinstance(owner, dict):
            continue
        email = owner.get("emailAddress") or owner.get("email")
        name_label = owner.get("displayName") or owner.get("name")
        if email:
            entities.append({
                "type": "person",
                "handle_type": "email",
                "handle": email.lower(),
                "name": name_label,
                "is_actor": True,
            })
        owner_labels.append(name_label or email or "")
    if not name and not description and not file_id:
        return None
    text = f"Google Drive file: {name or file_id}"
    if mime_type:
        text += f"\nType: {mime_type}"
    if description:
        text += f"\nDescription: {description}"
    if owner_labels:
        text += f"\nOwners: {', '.join(label for label in owner_labels if label)}"
    return NormalizedEvent(
        source_type="googledrive",
        actor_label=f"email:{entities[0]['handle']}" if entities else None,
        occurred_at=_ts_to_dt(modified),
        text=text,
        thread_key=f"googledrive:{file_id}" if file_id else None,
        channel_key=_first(file_data, "driveId", "drive_id", default="my-drive"),
        source_url=(
            _first(file_data, "webViewLink", "web_view_link", "alternateLink", default="")
            or (f"https://drive.google.com/file/d/{file_id}/view" if file_id else None)
        ),
        entities=entities,
        metadata={
            "file_id": file_id or None,
            "name": name or None,
            "mime_type": mime_type or None,
            "modified_time": modified,
            "event_type": payload.get("event_type") or payload.get("type"),
        },
    )


def _normalize_linear(payload: dict) -> NormalizedEvent | None:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    issue = data.get("issue") if isinstance(data.get("issue"), dict) else data
    if not isinstance(issue, dict):
        return None

    title = _first(issue, "title", "name", default="")
    description = _first(issue, "description", "body", "content", default="")
    if not title.strip() and not description.strip():
        return None

    issue_id = _first(issue, "id", "issue_id", "issueId", default="")
    identifier = _first(issue, "identifier", "number", default="")
    state = _linear_name(issue.get("state")) or _first(issue, "state_name", "stateName", "status", default="")
    assignee = _linear_person(issue.get("assignee") or issue.get("assigneeUser"))
    creator = _linear_person(issue.get("creator") or issue.get("createdBy"))
    team = _linear_name(issue.get("team")) or _first(issue, "team_name", "teamName", default="")
    project = _linear_name(issue.get("project")) or _first(issue, "project_name", "projectName", default="")
    priority = _first(issue, "priorityLabel", "priority_label", "priority", default="")
    due_date = _first(issue, "dueDate", "due_date", "deadline", default="")
    updated_at = _first(issue, "updatedAt", "updated_at", "createdAt", "created_at")
    url = _first(issue, "url", "webUrl", "html_url", default=None)

    lines = [f"Linear issue: {identifier} {title}".strip()]
    if state:
        lines.append(f"State: {state}")
    if assignee.get("label"):
        lines.append(f"Assignee: {assignee['label']}")
    if creator.get("label"):
        lines.append(f"Creator: {creator['label']}")
    if team:
        lines.append(f"Team: {team}")
    if project:
        lines.append(f"Project: {project}")
    if priority:
        lines.append(f"Priority: {priority}")
    if due_date:
        lines.append(f"Due date: {due_date}")
    if description:
        lines.append(f"Description: {description}")

    comments = issue.get("comments") or data.get("comments") or []
    for comment in comments[:10]:
        if not isinstance(comment, dict):
            continue
        body = _first(comment, "body", "text", "content", default="")
        if not body.strip():
            continue
        author = _linear_person(comment.get("user") or comment.get("author"))
        prefix = f"Comment by {author['label']}: " if author.get("label") else "Comment: "
        lines.append(prefix + body)

    entities = []
    for person, is_actor in ((creator, True), (assignee, False)):
        if person.get("email"):
            entities.append({
                "type": "person",
                "handle_type": "email",
                "handle": person["email"].lower(),
                "name": person.get("name"),
                "is_actor": is_actor,
            })
        elif person.get("name"):
            entities.append({"type": "person", "name": person["name"], "is_actor": is_actor})

    return NormalizedEvent(
        source_type="linear",
        actor_label=(
            f"email:{creator['email'].lower()}" if creator.get("email")
            else (f"linear:{creator['id']}" if creator.get("id") else None)
        ),
        occurred_at=_ts_to_dt(updated_at),
        text="\n".join(lines),
        thread_key=f"linear:{issue_id or identifier}" if (issue_id or identifier) else None,
        channel_key=team or None,
        source_url=url,
        entities=entities,
        metadata={
            "issue_id": issue_id or None,
            "identifier": str(identifier) if identifier else None,
            "state": state or None,
            "project": project or None,
            "team": team or None,
            "due_date": due_date or None,
            "event_type": payload.get("event_type") or data.get("type") or payload.get("type"),
        },
    )


def _linear_name(value) -> str:
    if isinstance(value, dict):
        return _first(value, "name", "title", "key", default="") or ""
    return str(value) if value not in (None, "") else ""


def _linear_person(value) -> dict:
    if not isinstance(value, dict):
        return {}
    name = _first(value, "name", "displayName", "display_name", default="")
    email = _first(value, "email", "emailAddress", "email_address", default="")
    person_id = _first(value, "id", default="")
    label = name or email or person_id
    return {"id": person_id, "name": name, "email": email, "label": label}


def _normalize_github(payload: dict) -> NormalizedEvent | None:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    pull_request = data.get("pull_request") if isinstance(data.get("pull_request"), dict) else None
    issue = data.get("issue") if isinstance(data.get("issue"), dict) else None
    commit = data.get("commit") if isinstance(data.get("commit"), dict) else None

    if pull_request:
        return _normalize_github_pull_request(payload, data, pull_request)
    if issue:
        return _normalize_github_issue(payload, data, issue)
    if commit:
        return None

    # Search/backfill results are bare pull request objects.
    if data.get("number") and data.get("title") and (data.get("html_url") or data.get("url")):
        return _normalize_github_pull_request(payload, data, sanitize_github_pull_request(data))
    return None


def _normalize_github_pull_request(payload: dict, data: dict, pr: dict) -> NormalizedEvent:
    repository = _github_repository(data, pr)
    author = _github_person(pr.get("user") or pr.get("author"))
    assignees = [_github_person(value) for value in (pr.get("assignees") or [])]
    reviewers = [_github_person(value) for value in (pr.get("requested_reviewers") or [])]
    number = _first(pr, "number", default="")
    title = _first(pr, "title", default="Untitled pull request")
    state = _first(pr, "state", default="")
    merged_at = _first(pr, "merged_at", "mergedAt", default="")
    base = _github_branch(pr.get("base"), pr.get("base_branch"))
    head = _github_branch(pr.get("head"), pr.get("head_branch"))

    lines = [f"GitHub pull request: {repository}#{number} {title}".strip()]
    lines.append(f"State: {'merged' if merged_at else state or 'unknown'}")
    if pr.get("draft"):
        lines.append("Draft: yes")
    if author.get("label"):
        lines.append(f"Author: {author['label']}")
    if assignees:
        lines.append("Assignees: " + ", ".join(p["label"] for p in assignees if p.get("label")))
    if reviewers:
        lines.append("Review requested from: " + ", ".join(p["label"] for p in reviewers if p.get("label")))
    if base or head:
        lines.append(f"Branches: {head or 'unknown'} -> {base or 'unknown'}")
    labels = [_github_label(value) for value in (pr.get("labels") or [])]
    if labels := [value for value in labels if value]:
        lines.append("Labels: " + ", ".join(labels))

    people = [(author, True)] + [(person, False) for person in assignees + reviewers]
    return NormalizedEvent(
        source_type="github",
        actor_label=f"github:{author['login']}" if author.get("login") else None,
        occurred_at=_ts_to_dt(_first(pr, "updated_at", "updatedAt", "created_at", "createdAt")),
        text="\n".join(lines),
        thread_key=f"github:{repository}:pr:{number or pr.get('id')}",
        channel_key=repository or None,
        source_url=_first(pr, "html_url", "htmlUrl", "url", default=None),
        entities=_github_entities(people),
        metadata={
            "kind": "pull_request",
            "repository": repository or None,
            "number": number or None,
            "state": "merged" if merged_at else (state or None),
            "draft": bool(pr.get("draft")),
            "base_branch": base or None,
            "head_branch": head or None,
            "event_type": payload.get("event_type") or data.get("action") or payload.get("type"),
        },
    )


def _normalize_github_issue(payload: dict, data: dict, issue: dict) -> NormalizedEvent:
    repository = _github_repository(data, issue)
    author = _github_person(issue.get("user") or issue.get("author"))
    assignees = [_github_person(value) for value in (issue.get("assignees") or [])]
    number = _first(issue, "number", default="")
    title = _first(issue, "title", default="Untitled issue")
    state = _first(issue, "state", default="")
    lines = [f"GitHub issue: {repository}#{number} {title}".strip(), f"State: {state or 'unknown'}"]
    if author.get("label"):
        lines.append(f"Author: {author['label']}")
    if assignees:
        lines.append("Assignees: " + ", ".join(p["label"] for p in assignees if p.get("label")))
    return NormalizedEvent(
        source_type="github",
        actor_label=f"github:{author['login']}" if author.get("login") else None,
        occurred_at=_ts_to_dt(_first(issue, "updated_at", "updatedAt", "created_at", "createdAt")),
        text="\n".join(lines),
        thread_key=f"github:{repository}:issue:{number or issue.get('id')}",
        channel_key=repository or None,
        source_url=_first(issue, "html_url", "htmlUrl", "url", default=None),
        entities=_github_entities([(author, True)] + [(person, False) for person in assignees]),
        metadata={
            "kind": "issue",
            "repository": repository or None,
            "number": number or None,
            "state": state or None,
            "event_type": payload.get("event_type") or data.get("action") or payload.get("type"),
        },
    )


def _normalize_github_commit(payload: dict, data: dict, commit: dict) -> NormalizedEvent | None:
    repository = _github_repository(data, commit)
    sha = _first(commit, "sha", "id", default="")
    message = _first(commit, "message", default="")
    if not sha and not message:
        return None
    author = _github_person(commit.get("author") or commit.get("committer"))
    lines = [f"GitHub commit: {repository} {str(sha)[:12]}".strip()]
    if author.get("label"):
        lines.append(f"Author: {author['label']}")
    if message:
        lines.append(f"Message: {message}")
    return NormalizedEvent(
        source_type="github",
        actor_label=f"github:{author['login']}" if author.get("login") else None,
        occurred_at=_ts_to_dt(
            _first(commit, "timestamp", "date", "committed_at", "committedAt")
            or (commit.get("author") or {}).get("date")
        ),
        text="\n".join(lines),
        thread_key=f"github:{repository}:commit:{sha}" if sha else None,
        channel_key=repository or None,
        source_url=_first(commit, "html_url", "htmlUrl", "url", default=None),
        entities=_github_entities([(author, True)]),
        metadata={
            "kind": "commit",
            "repository": repository or None,
            "sha": sha or None,
            "event_type": payload.get("event_type") or data.get("action") or payload.get("type"),
        },
    )


def _github_repository(data: dict, item: dict) -> str:
    repository = data.get("repository") or item.get("repository") or {}
    if isinstance(repository, str):
        return repository
    return _first(repository, "full_name", "fullName", "name", default="") or ""


def _github_person(value) -> dict:
    if not isinstance(value, dict):
        return {}
    login = _first(value, "login", "username", default="")
    name = _first(value, "name", "display_name", default="")
    email = _first(value, "email", default="")
    return {
        "login": login,
        "name": name,
        "email": email,
        "label": name or login or email,
    }


def _github_entities(people: list[tuple[dict, bool]]) -> list[dict]:
    entities = []
    seen = set()
    for person, is_actor in people:
        handle = person.get("email") or person.get("login")
        if not handle or handle in seen:
            continue
        seen.add(handle)
        entities.append({
            "type": "person",
            "handle_type": "email" if person.get("email") else "github",
            "handle": handle.lower() if person.get("email") else handle,
            "name": person.get("name") or None,
            "is_actor": is_actor,
        })
    return entities


def _github_branch(value, fallback) -> str:
    if isinstance(value, dict):
        return _first(value, "ref", "name", "label", default="") or ""
    return str(fallback) if fallback not in (None, "") else ""


def _github_label(value) -> str:
    if isinstance(value, dict):
        return _first(value, "name", "title", default="") or ""
    return str(value) if value not in (None, "") else ""


HUBSPOT_OBJECT_LABELS = {
    "contacts": "contact",
    "companies": "company",
    "deals": "deal",
    "tickets": "ticket",
    "owners": "owner",
}
HUBSPOT_OBJECT_PATH_IDS = {
    "contacts": "0-1",
    "companies": "0-2",
    "deals": "0-3",
    "tickets": "0-5",
}
HUBSPOT_DISPLAY_PROPERTIES = {
    "contacts": ("email", "firstname", "lastname", "jobtitle", "company", "lifecyclestage", "hubspot_owner_id", "lastmodifieddate"),
    "companies": ("name", "domain", "industry", "lifecyclestage", "hubspot_owner_id", "lastmodifieddate"),
    "deals": ("dealname", "dealstage", "pipeline", "amount", "closedate", "hubspot_owner_id", "lastmodifieddate"),
    "tickets": ("subject", "content", "hs_pipeline_stage", "hubspot_owner_id", "lastmodifieddate"),
    "owners": ("email", "firstName", "lastName", "userId", "teams"),
}


def _normalize_hubspot(payload: dict) -> NormalizedEvent | None:
    object_type = str(payload.get("object_type") or payload.get("objectType") or "").lower()
    record = payload.get("record") if isinstance(payload.get("record"), dict) else payload
    if not object_type:
        object_type = str(_first(record, "objectType", "object_type", default="")).lower()
    if object_type not in HUBSPOT_OBJECT_LABELS:
        return None

    properties = record.get("properties") if isinstance(record.get("properties"), dict) else record
    record_id = str(
        _first(record, "id", "objectId", "object_id", "userId", "user_id", default="")
        or _first(properties, "hs_object_id", "email", default="")
    )
    title = _hubspot_title(object_type, properties, record_id)
    if not title:
        return None

    lines = [f"HubSpot {HUBSPOT_OBJECT_LABELS[object_type]}: {title}"]
    for key in HUBSPOT_DISPLAY_PROPERTIES.get(object_type, ()):
        value = properties.get(key)
        if value in (None, "", []):
            continue
        label = key.replace("hs_", "").replace("_", " ").replace("hubspot owner id", "owner id")
        lines.append(f"{label.title()}: {_hubspot_value(value)}")

    updated_at = (
        _first(record, "updatedAt", "updated_at", "createdAt", "created_at")
        or _first(properties, "lastmodifieddate", "hs_lastmodifieddate", "createdate")
    )
    email = _first(properties, "email", default="")
    actor_label = _hubspot_actor_label(properties)
    entities = []
    if email:
        entities.append({
            "type": "person",
            "handle_type": "email",
            "handle": str(email).lower(),
            "name": _hubspot_person_name(properties) or None,
            "is_actor": object_type in {"contacts", "owners"},
        })

    source_url = _first(record, "url", "source_url", "sourceUrl", "archivedUrl", default=None)
    portal_id = _first(record, "portalId", "portal_id", "hubId", "hub_id", default=None)
    if not source_url and record_id and object_type in HUBSPOT_OBJECT_PATH_IDS:
        prefix = f"/contacts/{portal_id}" if portal_id else "/contacts"
        source_url = f"https://app.hubspot.com{prefix}/record/{HUBSPOT_OBJECT_PATH_IDS[object_type]}/{record_id}"

    return NormalizedEvent(
        source_type="hubspot",
        actor_label=actor_label,
        occurred_at=_ts_to_dt(updated_at),
        text="\n".join(lines),
        thread_key=f"hubspot:{object_type}:{record_id}" if record_id else None,
        channel_key=object_type,
        source_url=source_url,
        entities=entities,
        metadata={
            "object_type": object_type,
            "record_id": record_id or None,
            "event_type": payload.get("event_type") or payload.get("type"),
        },
    )


def _hubspot_title(object_type: str, properties: dict, record_id: str) -> str:
    if object_type == "contacts":
        return _hubspot_person_name(properties) or str(_first(properties, "email", default="") or record_id)
    if object_type == "companies":
        return str(_first(properties, "name", "domain", default="") or record_id)
    if object_type == "deals":
        return str(_first(properties, "dealname", "name", default="") or record_id)
    if object_type == "tickets":
        return str(_first(properties, "subject", "content", default="") or record_id)
    if object_type == "owners":
        return _hubspot_person_name(properties) or str(_first(properties, "email", default="") or record_id)
    return record_id


def _hubspot_actor_label(properties: dict) -> str | None:
    email = _first(properties, "email", "hubspot_owner_email", "owner_email", default="")
    if email:
        return f"email:{str(email).lower()}"
    owner_id = _first(
        properties,
        "hubspot_owner_id",
        "owner_id",
        "ownerId",
        "hs_owner_id",
        "userId",
        "user_id",
        default="",
    )
    if owner_id:
        return f"hubspot_owner:{str(owner_id).strip()}"
    return None


def _hubspot_person_name(properties: dict) -> str:
    first = str(_first(properties, "firstname", "firstName", "first_name", default="") or "").strip()
    last = str(_first(properties, "lastname", "lastName", "last_name", default="") or "").strip()
    return " ".join(part for part in (first, last) if part).strip()


def _hubspot_value(value) -> str:
    if isinstance(value, list):
        return ", ".join(_hubspot_value(item) for item in value[:5])
    if isinstance(value, dict):
        label = _first(value, "name", "label", "email", "id", default="")
        return str(label or "object")
    return str(value)


def _normalize_transcript(payload: dict) -> NormalizedEvent | None:
    """
    Our own push format:
    {title, occurred_at, text? , segments?: [{speaker, text, ts?}], attendees?: [email|name], source?: granola|manual}
    """
    title = _first(payload, "title", default="Meeting")
    segments = payload.get("segments") or []
    if segments:
        lines = [f"{s.get('speaker', 'Unknown')}: {s.get('text', '')}" for s in segments if s.get("text")]
        text = "\n".join(lines)
    else:
        text = _first(payload, "text", "transcript", default="")
    if not text.strip():
        return None

    entities = []
    speakers = {s.get("speaker") for s in segments if s.get("speaker")}
    for name in speakers:
        entities.append({"type": "person", "name": name})
    for a in payload.get("attendees") or []:
        if "@" in str(a):
            entities.append({"type": "person", "handle_type": "email", "handle": a})
        else:
            entities.append({"type": "person", "name": a})

    return NormalizedEvent(
        source_type="transcript",
        actor_label=None,
        occurred_at=_ts_to_dt(payload.get("occurred_at")),
        text=f"Meeting: {title}\n\n{text}",
        thread_key=f"transcript:{title}:{payload.get('occurred_at', '')}"[:200],
        channel_key=payload.get("source") or "manual",
        source_url=payload.get("source_url"),
        entities=entities,
        metadata={"title": title, "segment_count": len(segments)},
    )


def _normalize_meeting(payload: dict) -> NormalizedEvent | None:
    event = _normalize_transcript(payload)
    if event is None:
        return None
    return replace(
        event,
        source_type="meeting",
        thread_key=event.thread_key.replace("transcript:", "meeting:", 1) if event.thread_key else None,
    )


def _normalize_capture(payload: dict) -> NormalizedEvent | None:
    """
    Ad-hoc capture: a free-text note a user pushes directly into the brain
    (POST /capture, app/api/capture.py), outside any connected source.

    Payload shape is our own: {text, captured_by_user_id, occurred_at}. ACL
    is not derived here -- acl_stamping.py has no per-source-type branch for
    "capture" so it falls through to the generic provider_acl shape, and
    app/core/permission_adapters.py::_capture_envelope recovers the owner
    from the external_event_id the API endpoint mints instead. This
    normalizer only has to turn the payload into pipeline text.
    """
    text = _first(payload, "text", default="")
    if not text.strip():
        return None

    user_id = _first(payload, "captured_by_user_id", default="")
    return NormalizedEvent(
        source_type="capture",
        actor_label=f"user:{user_id}" if user_id else None,
        occurred_at=_ts_to_dt(payload.get("occurred_at")),
        text=text.strip(),
        thread_key=None,
        channel_key="capture",
        source_url=None,
        entities=[],
        metadata={"captured_by_user_id": user_id or None},
    )


def _split_addresses(raw: str) -> list[str]:
    return [a.strip() for a in str(raw).split(",") if a.strip()]


def _extract_email(raw: str) -> str | None:
    match = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", str(raw))
    return match.group(0).lower() if match else None
