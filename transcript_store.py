"""Persists finalized LiveKit meeting speech into the tourism-ai-backend's
``mira_transcript_segment`` table so the hermes-mira-context MCP server's
``get_meeting_transcript`` tool can recall it later.

Writes directly to the same MySQL domain database the backend and the MCP
server use (``MIRA_DATABASE_URL``), rather than going through the backend's
``POST /transcript-segments`` endpoint — that endpoint requires a
session-bound worker JWT this voice process doesn't hold, and a direct write
keeps this on the same trust boundary the read-side MCP server already uses.
Best-effort throughout: any failure (misconfigured env, unknown room,
connectivity) is logged and swallowed so a DB hiccup never interrupts a live
conversation.
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime
from functools import lru_cache
from typing import Any, Dict, Optional

logger = logging.getLogger("gateway.platforms.livekit")

_SCHEMA_NAME = "livekit-transcript.v1"


@lru_cache(maxsize=1)
def _engine():
    """Lazily build the SQLAlchemy engine — import cost paid only if used."""
    database_url = os.getenv("MIRA_DATABASE_URL", "").strip()
    if not database_url:
        return None
    try:
        from sqlalchemy import create_engine
    except ImportError:
        logger.debug("transcript persistence disabled: sqlalchemy not installed")
        return None
    return create_engine(database_url, pool_pre_ping=True, future=True)


def _tables():
    from sqlalchemy import (
        BigInteger,
        Column,
        DateTime,
        JSON,
        MetaData,
        String,
        Table,
    )

    metadata = MetaData()
    sessions = Table(
        "mira_session",
        metadata,
        Column("session_id", String(36), primary_key=True),
        Column("livekit_room_name", String(255), nullable=False, unique=True),
    )
    participants = Table(
        "mira_participant",
        metadata,
        Column("participant_id", String(36), primary_key=True),
        Column("session_id", String(36), nullable=False),
        Column("pseudonym", String(128), nullable=False),
        Column("role", String(32), nullable=False),
        Column("livekit_identity", String(255), nullable=True),
        Column("account_id", String(36), nullable=True),
        Column("created_at", DateTime(timezone=True), nullable=False),
    )
    transcript_segments = Table(
        "mira_transcript_segment",
        metadata,
        Column("event_id", String(36), primary_key=True),
        Column("segment_id", String(36), nullable=False),
        Column("session_id", String(36), nullable=False),
        Column("participant_id", String(36), nullable=False),
        Column("schema_name", String(64), nullable=False),
        Column("occurred_at", DateTime(timezone=True), nullable=False),
        Column("received_at", DateTime(timezone=True), nullable=False),
        Column("device_id", String(128), nullable=False),
        Column("sequence", BigInteger, nullable=False),
        Column("payload", JSON, nullable=False),
        Column("source_kind", String(16), nullable=False),
        Column("correlation_id", String(128), nullable=False),
    )
    return sessions, participants, transcript_segments


def _resolve_session_id(connection, sessions_table, room_name: str) -> Optional[str]:
    from sqlalchemy import select

    row = connection.execute(
        select(sessions_table.c.session_id).where(
            sessions_table.c.livekit_room_name == room_name
        )
    ).first()
    return row.session_id if row is not None else None


def _resolve_participant_id(
    connection,
    participants_table,
    *,
    session_id: str,
    identity: str,
    display_name: str,
    role: str,
    mira_conversation_id: str,
) -> Optional[str]:
    from sqlalchemy import select
    from sqlalchemy.exc import IntegrityError
    from sqlalchemy import insert

    # A human participant's connection metadata already carries their stable
    # mira_participant.participant_id (see adapter.py _source_chat_id) — trust
    # it directly rather than looking it up.
    if mira_conversation_id:
        return mira_conversation_id

    existing = connection.execute(
        select(participants_table.c.participant_id).where(
            (participants_table.c.session_id == session_id)
            & (participants_table.c.livekit_identity == identity)
        )
    ).first()
    if existing is not None:
        return existing.participant_id

    # Not registered through the normal session/participant flow (e.g. the
    # agent's own identity, or a guest who joined without a portal token).
    # Provision a lightweight row so the transcript still has a speaker.
    new_id = str(uuid.uuid4())
    try:
        connection.execute(
            insert(participants_table).values(
                participant_id=new_id,
                session_id=session_id,
                pseudonym=display_name[:128] or identity[:128] or "Participant",
                role=role,
                livekit_identity=identity,
                account_id=None,
                created_at=datetime.utcnow(),
            )
        )
        return new_id
    except IntegrityError:
        # Lost a race with another concurrent insert for this identity.
        connection.rollback()
        existing = connection.execute(
            select(participants_table.c.participant_id).where(
                (participants_table.c.session_id == session_id)
                & (participants_table.c.livekit_identity == identity)
            )
        ).first()
        return existing.participant_id if existing is not None else None


def record_transcript_segment(entry: Dict[str, Any], *, room_name: str, mira_conversation_id: str = "") -> None:
    """Persist one finalized transcript entry. Synchronous — call via a thread.

    ``entry`` is one item produced by ``LiveKitAdapter._append_transcript``:
    ``{sequence, timestamp, role, identity, name, text, invoked, keyterm, kind}``.
    """
    engine = _engine()
    if engine is None:
        return

    from sqlalchemy.exc import IntegrityError, SQLAlchemyError

    identity = str(entry.get("identity") or "")
    text = str(entry.get("text") or "")
    if not identity or not text:
        return

    try:
        occurred_at = datetime.fromisoformat(str(entry.get("timestamp")))
    except (TypeError, ValueError):
        occurred_at = datetime.utcnow()

    sessions_table, participants_table, transcript_segments = _tables()
    role = str(entry.get("role") or "user")
    participant_role_hint = "agent" if role == "assistant" else "tourist"

    try:
        with engine.begin() as connection:
            session_id = _resolve_session_id(connection, sessions_table, room_name)
            if session_id is None:
                logger.debug(
                    "transcript persistence skipped: no mira_session for room %r",
                    room_name,
                )
                return

            participant_id = _resolve_participant_id(
                connection,
                participants_table,
                session_id=session_id,
                identity=identity,
                display_name=str(entry.get("name") or identity),
                role=participant_role_hint,
                mira_conversation_id=mira_conversation_id,
            )
            if participant_id is None:
                logger.debug(
                    "transcript persistence skipped: could not resolve participant for %r",
                    identity,
                )
                return

            now = datetime.utcnow()
            from sqlalchemy import insert

            connection.execute(
                insert(transcript_segments).values(
                    event_id=str(uuid.uuid4()),
                    segment_id=str(uuid.uuid4()),
                    session_id=session_id,
                    participant_id=participant_id,
                    schema_name=_SCHEMA_NAME,
                    occurred_at=occurred_at,
                    received_at=now,
                    device_id=identity[:128] or "livekit-voice",
                    sequence=int(entry.get("sequence") or 0),
                    payload={
                        "text": text,
                        "is_final": True,
                        "redacted": False,
                        "speaker_id": identity,
                        "role": role,
                        "name": str(entry.get("name") or ""),
                        "kind": str(entry.get("kind") or "speech"),
                    },
                    source_kind="worker",
                    correlation_id=str(uuid.uuid4()),
                )
            )
    except IntegrityError:
        # Duplicate (participant_id, device_id, sequence) — a reconnect reset
        # the room-scoped sequence counter. Not worth retrying; the entry
        # already exists once for this speaker under a different sequence
        # generation, which is an acceptable gap for a best-effort store.
        logger.debug("transcript persistence: duplicate segment for %r seq=%s", identity, entry.get("sequence"))
    except SQLAlchemyError as exc:
        logger.warning("transcript persistence failed: %s", exc)


def reset_engine_for_tests() -> None:
    _engine.cache_clear()
