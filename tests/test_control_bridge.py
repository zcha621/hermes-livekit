"""Tests for the MiRA Hermes control bridge."""

import json
import shutil
import unittest
from pathlib import Path
from uuid import uuid4

from control_bridge import BridgeError, HermesController, read_dotenv, update_dotenv, validate_config


class ControlBridgeTests(unittest.TestCase):
    def setUp(self):
        self.test_home = Path(__file__).parent / f".test-home-{uuid4().hex}"
        self.test_home.mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self.test_home, ignore_errors=True)

    def test_validate_config_accepts_safe_values(self):
        changes = validate_config(
            {
                "livekit_url": "wss://livekit.example.test",
                "room": "tour-room:1",
                "agent_name": "Hermes",
                "auto_vision": True,
                "silence_seconds": 0.8,
                "livekit_enabled": True,
            }
        )
        self.assertEqual(changes["LIVEKIT_ROOM"], "tour-room:1")
        self.assertEqual(changes["HERMES_LIVEKIT_AUTO_VISION"], "true")

    def test_validate_config_rejects_unknown_and_unsafe_values(self):
        with self.assertRaises(BridgeError):
            validate_config({"unknown": "value"})
        with self.assertRaises(BridgeError):
            validate_config({"room": "../../escape"})
        with self.assertRaises(BridgeError):
            validate_config({"livekit_url": "https://not-websocket.example"})

    def test_dotenv_update_preserves_secrets_and_comments(self):
        path = self.test_home / ".env"
        path.write_text("# retained\nLIVEKIT_API_SECRET=secret\nLIVEKIT_ROOM=old\n", encoding="utf-8")
        update_dotenv(path, {"LIVEKIT_ROOM": "new"})
        lines, values = read_dotenv(path)
        self.assertIn("# retained", lines)
        self.assertEqual(values["LIVEKIT_API_SECRET"], "secret")
        self.assertEqual(values["LIVEKIT_ROOM"], "new")
        self.assertEqual(len(list(path.parent.glob(".env.portal-*.bak"))), 1)

    def test_safe_config_never_returns_credentials(self):
        (self.test_home / ".env").write_text(
            "LIVEKIT_URL=ws://localhost:7880\n"
            "LIVEKIT_API_KEY=key-value\n"
            "LIVEKIT_API_SECRET=secret-value\n"
            "LIVEKIT_ROOM=hermes\n",
            encoding="utf-8",
        )
        (self.test_home / "config.yaml").write_text(
            "platforms:\n  livekit:\n    enabled: true\nvoice:\n  auto_tts: true\n",
            encoding="utf-8",
        )
        rendered = json.dumps(HermesController(self.test_home).safe_config())
        self.assertNotIn("key-value", rendered)
        self.assertNotIn("secret-value", rendered)
        self.assertIn('"has_api_key": true', rendered)
        self.assertIn('"has_api_secret": true', rendered)


if __name__ == "__main__":
    unittest.main()
