"""Authoritative shared map snapshot for a LiveKit room's current agent turn."""
from __future__ import annotations

import os
import time
import uuid
from pathlib import Path


def fetch_room_map(room_name: str) -> dict | None:
    import httpx
    from sqlalchemy import select
    try:
        from .transcript_store import _engine, _tables
    except ImportError:
        from transcript_store import _engine, _tables

    engine = _engine()
    if engine is None or not room_name:
        return None
    sessions, participants, _ = _tables()
    with engine.connect() as connection:
        row = connection.execute(
            select(sessions.c.session_id, participants.c.participant_id)
            .join(participants, participants.c.session_id == sessions.c.session_id)
            .where(sessions.c.livekit_room_name == room_name)
            .order_by(participants.c.participant_id).limit(1)
        ).first()
    if row is None:
        return None
    headers = {"Accept": "application/json"}
    key_path = os.getenv("MIRA_AUTH_PRIVATE_KEY_PATH", "").strip()
    key_id = os.getenv("MIRA_AUTH_KEY_ID", "").strip()
    if key_path and key_id:
        import jwt
        now = int(time.time())
        token = jwt.encode({
            "iss": os.getenv("MIRA_AUTH_ISSUER", "https://mira.local/auth"),
            "aud": os.getenv("MIRA_AUTH_AUDIENCE", "tourism-ai-backend"),
            "sub": "python-context-worker", "iat": now, "nbf": now, "exp": now + 60,
            "jti": uuid.uuid4().hex, "scope": "context:read", "session_id": row.session_id,
        }, Path(key_path).read_bytes(), algorithm="EdDSA", headers={"typ": "at+jwt", "kid": key_id})
        headers["Authorization"] = f"Bearer {token}"
    base = os.getenv("MIRA_BACKEND_URL", "http://127.0.0.1:8000").rstrip("/")
    response = httpx.get(
        f"{base}/api/v1/sessions/{row.session_id}/map-state",
        params={"participant_id": row.participant_id}, headers=headers, timeout=3.0,
    )
    response.raise_for_status()
    return response.json()
