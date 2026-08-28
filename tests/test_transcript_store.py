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
                    session_id=session_id, livekit_room_name=room_name
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

    def test_skips_silently_without_matching_session(self):
        transcript_store.record_transcript_segment(
            self._entry(), room_name="no-such-room"
        )
        from sqlalchemy import select

        with self.engine.connect() as connection:
            rows = connection.execute(select(self._transcript_segments)).all()
        self.assertEqual(rows, [])

    def test_trusts_mira_conversation_id_as_participant_id(self):
        self._seed_session("room-1")
        participant_id = "22222222-2222-2222-2222-222222222222"

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


if __name__ == "__main__":
    unittest.main()
