import importlib.util
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "hermes_livekit_transcript_store_test", PLUGIN_ROOT / "transcript_store.py"
)
transcript_store = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(transcript_store)


class TranscriptStoreTests(unittest.TestCase):
    def setUp(self):
        try:
            from sqlalchemy import create_engine
        except ImportError:
            self.skipTest("sqlalchemy is not installed")
        self.engine = create_engine("sqlite://")
        sessions, participants, transcript_segments = transcript_store._tables()
        sessions.metadata.create_all(
            self.engine, tables=[sessions, participants, transcript_segments]
        )
        self._sessions, self._participants, self._transcript_segments = (
            sessions,
            participants,
            transcript_segments,
        )
        self.addCleanup(transcript_store.reset_engine_for_tests)
        patcher = patch.object(transcript_store, "_engine", return_value=self.engine)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _seed_session(self, room_name="room-1"):
        from sqlalchemy import insert

        session_id = "11111111-1111-1111-1111-111111111111"
        with self.engine.begin() as connection:
            connection.execute(
                insert(self._sessions).values(
                    session_id=session_id, livekit_room_name=room_name,
                    study_id="00000000-0000-0000-0000-000000000001",
                    status="active", created_at=datetime.now(timezone.utc),
                )
            )
        return session_id

    def _entry(self, **overrides):
        entry = {
            "sequence": 1,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "role": "user",
            "identity": "alice",
            "name": "Alice",
            "text": "What's on my itinerary today?",
            "invoked": True,
            "keyterm": "MiRA",
            "kind": "speech",
        }
        entry.update(overrides)
        return entry

    def test_auto_provisions_session_when_room_has_none(self):
        """A room with no mira_session row gets one created on first write."""
        transcript_store.record_transcript_segment(
            self._entry(), room_name="fresh-room"
        )
        from sqlalchemy import select

        with self.engine.connect() as connection:
            sessions = connection.execute(
                select(self._sessions).where(
                    self._sessions.c.livekit_room_name == "fresh-room"
                )
            ).one()
            rows = connection.execute(select(self._transcript_segments)).all()
        self.assertIsNotNone(sessions.session_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].session_id, sessions.session_id)

    def test_reuses_existing_session_on_second_write(self):
        """A second write to the same room reuses the auto-provisioned session."""
        transcript_store.record_transcript_segment(
            self._entry(sequence=1), room_name="reuse-room"
        )
        transcript_store.record_transcript_segment(
            self._entry(sequence=2), room_name="reuse-room"
        )
        from sqlalchemy import select

        with self.engine.connect() as connection:
            sessions = connection.execute(
                select(self._sessions).where(
                    self._sessions.c.livekit_room_name == "reuse-room"
                )
            ).all()
            rows = connection.execute(select(self._transcript_segments)).all()
        self.assertEqual(len(sessions), 1)
        self.assertEqual(len(rows), 2)

    def test_uses_room_bound_mira_conversation_id_as_participant_id(self):
        session_id = self._seed_session("room-1")
        participant_id = "22222222-2222-2222-2222-222222222222"
        from sqlalchemy import insert
        with self.engine.begin() as connection:
            connection.execute(insert(self._participants).values(
                participant_id=participant_id, session_id=session_id, pseudonym="Alice",
                role="tourist", livekit_identity="alice", created_at=datetime.now(timezone.utc),
            ))

        transcript_store.record_transcript_segment(
            self._entry(),
            room_name="room-1",
            mira_conversation_id=participant_id,
        )

        from sqlalchemy import select

        with self.engine.connect() as connection:
            row = connection.execute(select(self._transcript_segments)).one()
        self.assertEqual(row.participant_id, participant_id)
        self.assertEqual(row.payload["text"], "What's on my itinerary today?")
        self.assertEqual(row.payload["speaker_id"], "alice")

    def test_auto_provisions_participant_for_unknown_identity(self):
        session_id = self._seed_session("room-2")

        transcript_store.record_transcript_segment(
            self._entry(identity="hermes-agent", name="Hermes", role="assistant"),
            room_name="room-2",
        )

        from sqlalchemy import select

        with self.engine.connect() as connection:
            participant = connection.execute(
                select(self._participants).where(
                    self._participants.c.session_id == session_id
                )
            ).one()
        self.assertEqual(participant.livekit_identity, "hermes-agent")
        self.assertEqual(participant.role, "agent")

    def test_reuses_provisioned_participant_on_second_call(self):
        self._seed_session("room-3")
        transcript_store.record_transcript_segment(
            self._entry(identity="bob", name="Bob", sequence=1),
            room_name="room-3",
        )
        transcript_store.record_transcript_segment(
            self._entry(identity="bob", name="Bob", sequence=2),
            room_name="room-3",
        )

        from sqlalchemy import select

        with self.engine.connect() as connection:
            participants = connection.execute(
                select(self._participants).where(
                    self._participants.c.livekit_identity == "bob"
                )
            ).all()
        self.assertEqual(len(participants), 1)

    def test_duplicate_display_names_do_not_drop_other_speaker(self):
        self._seed_session()
        for identity in ("phone-alice", "browser-alice"):
            transcript_store.record_transcript_segment(
                self._entry(identity=identity, name="Alice"), room_name="room-1")
        from sqlalchemy import select
        with self.engine.connect() as connection:
            rows = connection.execute(select(self._transcript_segments)).all()
        self.assertEqual({r.payload["speaker_id"] for r in rows}, {"phone-alice", "browser-alice"})

    def test_reconnected_stream_sequence_can_restart(self):
        self._seed_session()
        for stream in ("occupancy-one", "occupancy-two"):
            transcript_store.record_transcript_segment(
                self._entry(sequence=1, stream_id=stream), room_name="room-1")
        from sqlalchemy import select
        with self.engine.connect() as connection:
            rows = connection.execute(select(self._transcript_segments)).all()
        self.assertEqual(len(rows), 2)

    def test_session_insert_race_preserves_outer_transaction(self):
        from sqlalchemy import insert, select
        session_id = self._seed_session()
        with self.engine.begin() as connection:
            execute = connection.execute
            calls = 0
            def race(statement, *args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 1:
                    return execute(select(self._sessions).where(self._sessions.c.session_id == "missing"))
                return execute(statement, *args, **kwargs)
            with patch.object(connection, "execute", side_effect=race):
                resolved = transcript_store._resolve_session_id(connection, self._sessions, "room-1")
            self.assertEqual(resolved, session_id)
            self.assertEqual(connection.execute(select(self._sessions.c.session_id)).scalar_one(), session_id)


if __name__ == "__main__":
    unittest.main()
