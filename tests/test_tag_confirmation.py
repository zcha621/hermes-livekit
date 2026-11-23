import json
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import Mock, patch

from test_adapter import livekit_adapter as mod
from test_itinerary_tools import tools
import context_tools


class TagConfirmationTests(IsolatedAsyncioTestCase):
    async def invoke(self, *, owner=True, spoken=True):
        worker = SimpleNamespace(
            _room_name="current-room",
            _participant_connection_metadata=lambda identity: {"mira_room_owner": "true" if owner else "false"},
        )
        event = SimpleNamespace(
            source=SimpleNamespace(user_id="actual-speaker", chat_id="actual-chat"),
            message_id="actual-voice-event", text="Yes, let's visit the gallery at two.",
            metadata={"livekit_spoken_transcript": spoken},
        )
        room_token = context_tools.active_room.set(worker)
        turn_token = context_tools.active_turn.set(event)
        try:
            return json.loads(await tools._handle_add_confirmed_tag({
                "tag_id": "chosen-tag", "starts_at": "2026-11-24T14:00:00+13:00",
                "duration_minutes": 45, "expected_revision": 2,
                "confirmation_text": "Untrusted model text", "session_id": "wrong-room",
            }))
        finally:
            context_tools.active_turn.reset(turn_token)
            context_tools.active_room.reset(room_token)

    async def test_requires_current_owner_voice_turn(self):
        with patch.object(tools, "_post_gateway_command") as post:
            self.assertIn("error", json.loads(await tools._handle_add_confirmed_tag({})))
            self.assertIn("error", await self.invoke(owner=False))
            self.assertIn("error", await self.invoke(spoken=False))
            post.assert_not_called()

    async def test_confirmation_evidence_and_room_come_from_runtime_not_tool_arguments(self):
        with patch("room_map.fetch_room_map", return_value={"session_id": "actual-session"}) as read, \
                patch.object(tools, "_post_gateway_command", return_value={"linked": True}) as post:
            self.assertEqual(await self.invoke(), {"linked": True})
        read.assert_called_once_with("current-room")
        command = post.call_args.args[0]
        self.assertEqual(command["session_id"], "actual-session")
        self.assertEqual(command["source"]["user_id"], "actual-speaker")
        self.assertEqual(command["confirmation_event_id"], "actual-voice-event")
        self.assertEqual(command["confirmation_text"], "Yes, let's visit the gallery at two.")

    async def test_turn_context_is_reset_after_background_failure(self):
        worker = mod.LiveKitAdapter(Mock(extra={"room": "room", "url": "ws://example.invalid", "api_key": "key", "api_secret": "secret"}))
        self.addCleanup(mod.LIVE_ADAPTERS.discard, worker)
        event = SimpleNamespace(source=SimpleNamespace(chat_id="room"))

        async def fail(adapter, received, key):
            self.assertIs(context_tools.active_turn.get(), event)
            raise RuntimeError("failed")

        with patch.object(mod.BasePlatformAdapter, "_process_message_background", fail):
            with self.assertRaises(RuntimeError):
                await worker._process_message_background(event, "key")
        self.assertIsNone(context_tools.active_turn.get())
