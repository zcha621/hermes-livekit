import importlib.util
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

room_map = load("room_map")
store = load("transcript_store")


class RoomMapTests(unittest.TestCase):
    def test_backend_request_is_bound_to_the_resolved_room(self):
        import httpx
        import jwt
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from sqlalchemy import create_engine, insert
        engine = create_engine("sqlite://")
        self.addCleanup(engine.dispose)
        sessions, participants, _ = store._tables()
        sessions.metadata.create_all(engine)
        room_id, person_id = str(uuid4()), str(uuid4())
        now = datetime.now(timezone.utc)
        with engine.begin() as conn:
            conn.execute(insert(sessions).values(session_id=room_id, livekit_room_name="map-test",
                         study_id=str(uuid4()), status="active", created_at=now))
            conn.execute(insert(participants).values(participant_id=person_id, session_id=room_id,
                         pseudonym="Phone", role="tourist", created_at=now))
        key = Ed25519PrivateKey.generate()
        state = {"session_id": room_id, "tags": [], "itinerary": None, "latest_location": None}
        fake_store = SimpleNamespace(_engine=lambda: engine, _tables=store._tables)
        with tempfile.TemporaryDirectory() as directory:
            keyfile = Path(directory) / "key.pem"
            keyfile.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
            env = {"MIRA_AUTH_PRIVATE_KEY_PATH": str(keyfile), "MIRA_AUTH_KEY_ID": "test-key"}
            with patch.dict(os.environ, env, clear=True), patch.dict(sys.modules, {"transcript_store": fake_store}):
                response = httpx.Response(200, json=state, request=httpx.Request("GET", "http://test"))
                with patch("httpx.get", return_value=response) as get:
                    self.assertIsNone(room_map.fetch_room_map("other-room"))
                    get.assert_not_called()
                    self.assertEqual(room_map.fetch_room_map("map-test"), state)
                    args, kwargs = get.call_args
                    self.assertIn(f"/sessions/{room_id}/map-state", args[0])
                    self.assertEqual(kwargs["params"], {"participant_id": person_id})
                    token = kwargs["headers"]["Authorization"].removeprefix("Bearer ")
                    claims = jwt.decode(token, key.public_key(), algorithms=["EdDSA"],
                                        audience="tourism-ai-backend", issuer="https://mira.local/auth")
                    self.assertEqual(claims["session_id"], room_id)
                    self.assertEqual(claims["scope"], "context:read")
                    self.assertEqual(claims["sub"], "python-context-worker")
