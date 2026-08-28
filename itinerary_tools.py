"""Native Hermes tools for saving and confirming a traveller's itinerary.

Registered directly (not through the hermes-mira-context MCP server) because
the model driving this plugin's realtime LiveKit route has never once
successfully invoked an MCP-prefixed tool (``mcp__hermes_mira_context__*``)
in this deployment's history, while natively-registered tools are called
reliably. The MCP server still owns the read-only lookups
(get_confirmed_itinerary, get_traveller_location, get_meeting_transcript) —
those are lower-stakes if occasionally skipped. Saving a draft is not, so
that write path gets the native tool's reliability instead.

Talks to the same tourism-ai-backend endpoint
(``POST /gateway/planning-workspace``) the hermes-mira-context MCP server
uses, via stdlib ``urllib.request`` only — no extra dependency for the
Hermes plugin environment.
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
import uuid
from datetime import datetime, time as _time, timedelta

logger = logging.getLogger("gateway.platforms.livekit")

_TIME_TOKEN = r"(\d{1,2})(?::(\d{2}))?\s*(am|pm|AM|PM)?"
_LINE_RE = re.compile(
    rf"^\s*{_TIME_TOKEN}\s*(?:[-–—]\s*{_TIME_TOKEN})?\s*[-–—:]?\s*(.+?)\s*$"
)
_DEFAULT_ITEM_DURATION_MINUTES = 60

_SAVE_ITINERARY_DRAFT_SCHEMA = {
    "name": "save_itinerary_draft",
    "description": (
        "Save a trip plan as a draft itinerary. Call this right after "
        "describing a day plan to the traveller: write the plan as normal "
        "spoken/chat text first (one activity per line, each starting with "
        "its time), then pass that same text here as plan_text. A spoken "
        "or chat description alone is not enough — if this is never "
        "called, nothing is saved and the traveller will not see it later."
    ),
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "required": ["plan_text", "plan_date"],
        "properties": {
            "plan_text": {
                "type": "string",
                "description": (
                    "The itinerary as plain text, one activity per line, "
                    "each starting with its time, e.g. '9:00 - Coffee at "
                    "Villa Martinique, Great North Rd'."
                ),
            },
            "plan_date": {
                "type": "string",
                "description": (
                    "The actual calendar date the plan is for, as "
                    "YYYY-MM-DD (work out what \"this Saturday\" etc. "
                    "means and pass the real date)."
                ),
            },
            "title": {"type": "string", "maxLength": 255},
            "timezone": {"type": "string", "maxLength": 64, "default": "Pacific/Auckland"},
            "requirements": {"type": "string", "maxLength": 5000},
        },
    },
}

_CONFIRM_ITINERARY_DRAFT_SCHEMA = {
    "name": "confirm_itinerary_draft",
    "description": (
        "Confirm and permanently save the traveller's current itinerary "
        "draft. Only call this after the traveller has unmistakably "
        "approved the draft — never infer approval from silence, thanks, "
        "or a request to see the draft."
    ),
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "required": ["expected_revision"],
        "properties": {
            "expected_revision": {
                "type": "integer",
                "minimum": 1,
                "description": "The exact draft revision the traveller approved.",
            },
        },
    },
}


def _session_source():
    """Best-effort (platform, user_id, hermes_session_id) for the current turn."""
    try:
        from gateway.session_context import get_session_env
    except Exception:
        return "livekit", "unknown", "unknown"
    platform = (get_session_env("HERMES_SESSION_PLATFORM", "") or "livekit").strip().lower()
    user_id = get_session_env("HERMES_SESSION_USER_ID", "").strip() or "unknown"
    session_id = get_session_env("HERMES_SESSION_ID", "").strip() or "unknown"
    return platform, user_id, session_id


def _to_time(hour, minute, ampm):
    if hour is None:
        return None
    try:
        h = int(hour)
    except (TypeError, ValueError):
        return None
    m = int(minute) if minute else 0
    if ampm:
        ampm = ampm.lower()
        if ampm == "pm" and h != 12:
            h += 12
        elif ampm == "am" and h == 12:
            h = 0
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    return _time(hour=h, minute=m)


def _parse_itinerary_text(plan_text: str, plan_date: str):
    base_date = datetime.fromisoformat(plan_date).date()
    parsed = []
    pending_header = ""
    for raw_line in (plan_text or "").splitlines():
        line = raw_line.strip().lstrip("*-•").strip()
        if not line:
            continue
        match = _LINE_RE.match(line)
        if not match:
            pending_header = f"{pending_header} {line}".strip()
            continue
        start_hour, start_minute, start_ampm, end_hour, end_minute, end_ampm, description = match.groups()
        start_time = _to_time(start_hour, start_minute, start_ampm)
        if start_time is None:
            pending_header = f"{pending_header} {line}".strip()
            continue
        start_dt = datetime.combine(base_date, start_time)
        end_time = _to_time(end_hour, end_minute, end_ampm) if end_hour else None
        end_dt = (
            datetime.combine(base_date, end_time)
            if end_time is not None
            else start_dt + timedelta(minutes=_DEFAULT_ITEM_DURATION_MINUTES)
        )
        if end_dt <= start_dt:
            end_dt = start_dt + timedelta(minutes=_DEFAULT_ITEM_DURATION_MINUTES)
        full_description = f"{pending_header} {description}".strip() if pending_header else description
        pending_header = ""
        parsed.append(
            {"starts_at": start_dt.isoformat(), "ends_at": end_dt.isoformat(), "description": full_description}
        )
    parsed.sort(key=lambda item: item["starts_at"])
    return parsed


def _short_location(description: str) -> str:
    first_clause = re.split(r"[,:;.\-–—]", description, maxsplit=1)[0].strip()
    return (first_clause or description)[:255] or "Unspecified location"


def _resolve_zone(timezone_name):
    if not timezone_name or not str(timezone_name).strip():
        return None
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(str(timezone_name).strip())
    except Exception:
        return None


def _build_draft(*, title, summary, timezone, plan_date, plan_text, requirements):
    zone = _resolve_zone(timezone)
    parsed_items = _parse_itinerary_text(plan_text, plan_date)
    items = []
    for item in parsed_items:
        starts_at = datetime.fromisoformat(item["starts_at"])
        ends_at = datetime.fromisoformat(item["ends_at"])
        if zone is not None:
            starts_at = starts_at.replace(tzinfo=zone)
            ends_at = ends_at.replace(tzinfo=zone)
        items.append(
            {
                "item_id": str(uuid.uuid4()),
                "starts_at": starts_at.isoformat(),
                "ends_at": ends_at.isoformat(),
                "location": {"name": _short_location(item["description"])},
                "activity": item["description"],
                "transportation": {"mode": "unspecified"},
            }
        )
    return {
        "title": title,
        "summary": summary,
        "timezone": timezone,
        "requirements": (requirements or "").strip() or "No specific requirements noted.",
        "items": items,
    }


def _backend_base_url() -> str:
    return os.getenv("MIRA_BACKEND_URL", "http://127.0.0.1:8000").rstrip("/")


def _post_gateway_command(command: dict) -> dict:
    url = f"{_backend_base_url()}/api/v1/gateway/planning-workspace"
    body = json.dumps(command).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, method="POST", headers={"Content-Type": "application/json", "Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"tourism-ai-backend rejected the request (HTTP {exc.code}): {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"tourism-ai-backend unreachable: {exc}") from exc


async def _handle_save_itinerary_draft(args=None, **kwargs):
    args = args or {}
    plan_text = str(args.get("plan_text") or "")
    plan_date = str(args.get("plan_date") or "")
    if not plan_text.strip() or not plan_date.strip():
        return json.dumps({"error": "plan_text and plan_date are required"})

    platform, user_id, hermes_session_id = _session_source()
    title = str(args.get("title") or "").strip() or f"Trip plan for {plan_date}"
    timezone = str(args.get("timezone") or "Pacific/Auckland").strip() or "Pacific/Auckland"
    requirements = str(args.get("requirements") or "")

    try:
        draft = _build_draft(
            title=title,
            summary=title,
            timezone=timezone,
            plan_date=plan_date,
            plan_text=plan_text,
            requirements=requirements,
        )
        if not draft["items"]:
            return json.dumps(
                {
                    "error": (
                        "Could not find any timed activity lines in plan_text. "
                        "Each line needs to start with a time, e.g. '9:00 - Coffee at ...'."
                    )
                }
            )
        result = _post_gateway_command(
            {
                "action": "revise",
                "source": {
                    "platform": platform,
                    "user_id": user_id,
                    "chat_id": hermes_session_id,
                    "hermes_session_id": hermes_session_id,
                },
                "draft": draft,
            }
        )
        return json.dumps(result, ensure_ascii=False, default=str)
    except Exception as exc:
        logger.warning("save_itinerary_draft failed: %s", exc)
        return json.dumps({"error": str(exc)})


async def _handle_confirm_itinerary_draft(args=None, **kwargs):
    args = args or {}
    try:
        expected_revision = int(args.get("expected_revision"))
    except (TypeError, ValueError):
        return json.dumps({"error": "expected_revision must be an integer"})

    platform, user_id, hermes_session_id = _session_source()
    try:
        result = _post_gateway_command(
            {
                "action": "confirm",
                "source": {
                    "platform": platform,
                    "user_id": user_id,
                    "chat_id": hermes_session_id,
                    "hermes_session_id": hermes_session_id,
                },
                "expected_revision": expected_revision,
            }
        )
        return json.dumps(result, ensure_ascii=False, default=str)
    except Exception as exc:
        logger.warning("confirm_itinerary_draft failed: %s", exc)
        return json.dumps({"error": str(exc)})


def register_tools(ctx) -> None:
    """Expose the itinerary save/confirm write path as native Hermes tools."""
    registrations = (
        (_SAVE_ITINERARY_DRAFT_SCHEMA, _handle_save_itinerary_draft, "\U0001f4be"),
        (_CONFIRM_ITINERARY_DRAFT_SCHEMA, _handle_confirm_itinerary_draft, "✅"),
    )
    for schema, handler, emoji in registrations:
        ctx.register_tool(
            name=schema["name"],
            toolset="hermes-livekit",
            schema=schema,
            handler=handler,
            is_async=True,
            description=schema["description"],
            emoji=emoji,
        )
