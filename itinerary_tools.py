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
uses, via stdlib ``urllib.request``. Like that server it authenticates as
the ``python-context-worker`` subject with a short-lived EdDSA token signed
by MIRA_AUTH_PRIVATE_KEY_PATH / MIRA_AUTH_KEY_ID (PyJWT ships with Hermes);
without them the backend must run with MIRA_AUTH_MODE=disabled.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
import uuid
from datetime import date as _date, datetime, time as _time, timedelta

logger = logging.getLogger("gateway.platforms.livekit")

_TIME_TOKEN = r"(\d{1,2})(?::(\d{2}))?\s*(am|pm|AM|PM)?"
_LINE_RE = re.compile(
    rf"^\s*{_TIME_TOKEN}\s*(?:[-–—]\s*{_TIME_TOKEN})?\s*[-–—:]?\s*(.+?)\s*$"
)
_DEFAULT_ITEM_DURATION_MINUTES = 60

# Day headings in multi-day plans: "Day 2", "Sun 27 Sept", "27 September",
# "Sept 27th", "2026-09-27" (optionally combined, e.g. "Day 2 - Sun 27 Sept:").
_MONTHS = {
    name: number
    for number, names in enumerate(
        (
            ("jan", "january"), ("feb", "february"), ("mar", "march"),
            ("apr", "april"), ("may",), ("jun", "june"), ("jul", "july"),
            ("aug", "august"), ("sep", "sept", "september"), ("oct", "october"),
            ("nov", "november"), ("dec", "december"),
        ),
        start=1,
    )
    for name in names
}
_WEEKDAYS = {
    name: number
    for number, names in enumerate(
        (
            ("mon", "monday"), ("tue", "tues", "tuesday"), ("wed", "wednesday"),
            ("thu", "thur", "thurs", "thursday"), ("fri", "friday"),
            ("sat", "saturday"), ("sun", "sunday"),
        )
    )
    for name in names
}
_MONTH_WORD = "|".join(sorted(_MONTHS, key=len, reverse=True))
_WEEKDAY_WORD = "|".join(sorted(_WEEKDAYS, key=len, reverse=True))
_ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_DAY_MONTH_RE = re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({_MONTH_WORD})\b\.?", re.I)
_MONTH_DAY_RE = re.compile(rf"\b({_MONTH_WORD})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b", re.I)
_DAY_NUMBER_RE = re.compile(r"\bday\s+(\d{1,2})\b", re.I)
_WEEKDAY_RE = re.compile(rf"\b({_WEEKDAY_WORD})\b\.?", re.I)
# A heading line carries nothing but date words once those are removed.
_HEADING_LEFTOVER_RE = re.compile(r"^[\s,:;.\-–—()|/]*$")

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
                    "each starting with its 24-hour time, e.g. '09:00 - Coffee "
                    "at Villa Martinique, Great North Rd' or '13:30 - Lunch'. "
                    "For a multi-day plan put a heading line with the real "
                    "date before each day's activities, e.g. 'Day 2 - "
                    "2026-09-27', and list each day in time order."
                ),
            },
            "plan_date": {
                "type": "string",
                "description": (
                    "The actual calendar date of the plan's first day, as "
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


def _date_in_year(day: int, month: int, base_date: _date):
    try:
        candidate = _date(base_date.year, month, day)
    except ValueError:
        return None
    # "5 Jan" in a plan made in December means next January.
    if (base_date - candidate).days > 180:
        try:
            candidate = _date(base_date.year + 1, month, day)
        except ValueError:
            return None
    return candidate


def _heading_date(line: str, base_date: _date):
    """The calendar date a day-heading line names, or None if it is not one."""
    remainder = line
    found = None
    match = _ISO_DATE_RE.search(line)
    if match:
        try:
            found = _date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            return None
        remainder = remainder.replace(match.group(0), " ")
    for pattern, day_group, month_group in (
        (_DAY_MONTH_RE, 1, 2),
        (_MONTH_DAY_RE, 2, 1),
    ):
        match = pattern.search(remainder)
        if match and found is None:
            found = _date_in_year(
                int(match.group(day_group)),
                _MONTHS[match.group(month_group).lower()],
                base_date,
            )
            if found is None:
                return None
            remainder = remainder.replace(match.group(0), " ")
    day_number = _DAY_NUMBER_RE.search(remainder)
    if day_number:
        if found is None:
            found = base_date + timedelta(days=max(int(day_number.group(1)) - 1, 0))
        remainder = remainder.replace(day_number.group(0), " ")
    weekday = _WEEKDAY_RE.search(remainder)
    if weekday:
        if found is None:
            ahead = (_WEEKDAYS[weekday.group(1).lower()] - base_date.weekday()) % 7
            found = base_date + timedelta(days=ahead)
        remainder = remainder.replace(weekday.group(0), " ")
    if found is None or not _HEADING_LEFTOVER_RE.match(remainder):
        return None
    return found


def _infer_afternoon(
    value: _time,
    explicit_meridiem: bool,
    previous: _time | None,
    previous_written_24h: bool = False,
) -> _time:
    """Read a 12-hour clock time without am/pm the way a day plan means it.

    Plans are written in order, so "11:00 ... 12:30 ... 1:00 lunch" means
    1 pm, and a sightseeing plan never starts an activity at 1-6 am. After a
    time written on the 24-hour clock ("14:00"), a smaller bare time is taken
    literally: that writer is not using a 12-hour clock.
    """
    if explicit_meridiem or not 1 <= value.hour <= 11:
        return value
    if value.hour <= 6:
        return value.replace(hour=value.hour + 12)
    if previous is not None and value < previous and not previous_written_24h:
        return value.replace(hour=value.hour + 12)
    return value


def _parse_itinerary_text(plan_text: str, plan_date: str):
    base_date = datetime.fromisoformat(plan_date).date()
    current_date = base_date
    previous_time = None
    previous_written_24h = False
    parsed = []
    pending_header = ""
    for raw_line in (plan_text or "").splitlines():
        line = raw_line.replace("**", "").replace("__", "").strip().lstrip("*-•#").strip()
        if not line:
            continue
        match = _LINE_RE.match(line)
        if not match:
            heading = _heading_date(line, base_date)
            if heading is not None:
                current_date = heading
                previous_time = None
                previous_written_24h = False
                pending_header = ""
            else:
                pending_header = f"{pending_header} {line}".strip()
            continue
        start_hour, start_minute, start_ampm, end_hour, end_minute, end_ampm, description = match.groups()
        start_time = _to_time(start_hour, start_minute, start_ampm)
        if start_time is None:
            pending_header = f"{pending_header} {line}".strip()
            continue
        written_24h = not start_ampm and start_time.hour >= 13
        start_time = _infer_afternoon(
            start_time, bool(start_ampm), previous_time, previous_written_24h
        )
        previous_time = start_time
        previous_written_24h = written_24h
        start_dt = datetime.combine(current_date, start_time)
        end_time = _to_time(end_hour, end_minute, end_ampm) if end_hour else None
        if end_time is not None:
            end_time = _infer_afternoon(
                end_time, bool(end_ampm), start_time, written_24h
            )
        end_dt = (
            datetime.combine(current_date, end_time)
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


# Mirrors services/hermes-mcp/src/hermes_mcp/auth.py (and the portal's
# issueWorkerAccessToken): the backend's gateway planning endpoint only
# accepts this subject with accounts:read + accounts:write.
_WORKER_SUBJECT = "python-context-worker"
_WORKER_SCOPES = ("accounts:read", "accounts:write", "itinerary:read")


def _configured(name: str) -> str:
    value = os.getenv(name, "").strip()
    # Hermes leaves an unset ${VAR} placeholder in place; treat it as unset.
    return "" if value.startswith("${") else value


def _worker_authorization_header() -> str | None:
    key_path = _configured("MIRA_AUTH_PRIVATE_KEY_PATH")
    key_id = _configured("MIRA_AUTH_KEY_ID")
    if not key_path or not key_id:
        return None
    import jwt

    with open(key_path, "rb") as handle:
        private_key = handle.read()
    try:
        ttl = int(_configured("MIRA_AUTH_TOKEN_TTL_SECONDS"))
    except ValueError:
        ttl = 300
    now = int(time.time())
    token = jwt.encode(
        {
            "iss": _configured("MIRA_AUTH_ISSUER") or "https://mira.local/auth",
            "sub": _WORKER_SUBJECT,
            "aud": _configured("MIRA_AUTH_AUDIENCE") or "tourism-ai-backend",
            "iat": now,
            "nbf": now,
            "exp": now + ttl,
            "jti": uuid.uuid4().hex,
            "scope": " ".join(sorted(_WORKER_SCOPES)),
        },
        private_key,
        algorithm="EdDSA",
        headers={"typ": "at+jwt", "kid": key_id},
    )
    return f"Bearer {token}"


def _backend_base_url() -> str:
    return os.getenv("MIRA_BACKEND_URL", "http://127.0.0.1:8000").rstrip("/")


def _post_gateway_command(command: dict) -> dict:
    url = f"{_backend_base_url()}/api/v1/gateway/planning-workspace"
    body = json.dumps(command).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    authorization = _worker_authorization_header()
    if authorization:
        headers["Authorization"] = authorization
    request = urllib.request.Request(url, data=body, method="POST", headers=headers)
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
