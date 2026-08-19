import asyncio
import os
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import gateway.config as gateway_config
from gateway.config import PlatformConfig

import adapter as livekit_adapter

# The production plugin registry is populated before its adapter factory runs.
# Unit tests import the module directly, so seed the same dynamic enum allowlist.
gateway_config._Platform__bundled_plugin_names = (
    gateway_config._Platform__bundled_plugin_names or set()
) | {"livekit"}


def make_adapter(extra=None):
    settings = {
        "url": "ws://example.invalid",
        "api_key": "key",
        "api_secret": "secret",
        "room": "test-room",
        "agent_name": "Hermes",
    }
    settings.update(extra or {})
    return livekit_adapter.LiveKitAdapter(
        PlatformConfig(
            enabled=True,
            extra=settings,
        )
    )


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.adapter = make_adapter()

    def tearDown(self):
        livekit_adapter.LIVE_ADAPTERS.discard(self.adapter)

    def test_barge_in_sets_interrupt_and_clears_audio_queue(self):
        source = SimpleNamespace()
        source.cleared = False
        source.clear_queue = lambda: setattr(source, "cleared", True)
        self.adapter._audio_source = source
        self.adapter._is_playing = True
        self.adapter._activate_conversation("Hermes")

        self.adapter._interrupt_playback("phone")

        self.assertTrue(self.adapter._playback_interrupt.is_set())
        self.assertTrue(source.cleared)

    def test_barge_in_before_invocation_does_not_stop_playback(self):
        source = SimpleNamespace(clear_queue=lambda: self.fail("queue should stay live"))
        self.adapter._audio_source = source
        self.adapter._is_playing = True

        self.adapter._interrupt_playback("phone")

        self.assertFalse(self.adapter._playback_interrupt.is_set())

    def test_keyterm_match_is_case_insensitive_and_strips_wake_phrase(self):
        keyterm, cleaned = self.adapter._match_keyterm(
            "Hey MiRA, find walks near Rotorua"
        )

        self.assertEqual(keyterm, "MiRA")
        self.assertEqual(cleaned, "find walks near Rotorua")

    def test_short_followup_keeps_participant_topic(self):
        topic = self.adapter._topic_for_turn("yes please", "Rotorua family walks")

        self.assertEqual(topic, "Rotorua family walks")

    def test_latest_video_frame_prefers_sender_and_writes_unique_snapshot(self):
        now = time.monotonic()
        self.adapter._video_track_meta = {
            "alice-camera": ("alice", "camera"),
            "bob-screen": ("bob", "screenshare"),
        }
        self.adapter._latest_video_frames = {
            "alice-camera": (b"alice-jpeg", now, "camera", 640, 480),
            "bob-screen": (b"bob-jpeg", now + 0.1, "screenshare", 1280, 720),
        }

        capture = self.adapter._queue_latest_video_frame("alice")

        self.assertIsNotNone(capture)
        path, mime = capture
        self.assertEqual(mime, "image/jpeg")
        self.assertEqual(Path(path).read_bytes(), b"alice-jpeg")
        os.unlink(path)
        self.adapter._pending_captures.clear()

    def test_video_source_name_is_stable_for_unknown_sdk_value(self):
        publication = SimpleNamespace(source="SOURCE_SCREENSHARE")
        source = livekit_adapter.LiveKitAdapter._video_source_name(publication)
        self.assertIn("screenshare", source)

    def test_auto_vision_only_triggers_for_visual_utterances(self):
        self.assertFalse(self.adapter._should_attach_video("Hello, Hermes."))
        self.assertFalse(self.adapter._should_attach_video("What is the weather today?"))
        self.assertTrue(self.adapter._should_attach_video("Can you see my face?"))
        self.assertTrue(self.adapter._should_attach_video("Which button should I tap?"))

    def test_voice_defaults_match_discord_turn_timing(self):
        self.assertEqual(self.adapter._silence_threshold_seconds, 1.5)
        self.assertEqual(self.adapter._min_speech_duration_seconds, 0.5)
        self.assertEqual(
            self.adapter._ack_phrases,
            livekit_adapter.DEFAULT_ACK_PHRASES,
        )

    def test_behavior_is_loaded_from_hermes_yaml_extra(self):
        adapter = make_adapter(
            {
                "audio": {"silence_threshold_seconds": 1.8},
                "vision": {"auto_attach": False},
                "acknowledgements": {
                    "enabled": True,
                    "phrases": ["I'm on it."],
                },
            }
        )
        self.addCleanup(livekit_adapter.LIVE_ADAPTERS.discard, adapter)

        self.assertEqual(adapter._silence_threshold_seconds, 1.8)
        self.assertFalse(adapter._auto_vision)
        self.assertEqual(adapter._ack_phrases, ("I'm on it.",))

    def test_only_matching_session_first_tool_schedules_acknowledgement(self):
        class Loop:
            def __init__(self):
                self.calls = []

            def is_running(self):
                return True

            def call_soon_threadsafe(self, callback, *args):
                self.calls.append((callback, args))

        loop = Loop()
        self.adapter._room = object()
        self.adapter._event_loop = loop
        self.adapter._tool_ack_pending = True
        self.adapter.bind_tool_acknowledgement_session("livekit-session")

        self.assertFalse(
            self.adapter.schedule_tool_acknowledgement(session_id="other-session")
        )
        self.assertTrue(
            self.adapter.schedule_tool_acknowledgement(
                session_id="livekit-session", turn_id="turn-1", tool_call_id="tool-1"
            )
        )
        self.assertFalse(
            self.adapter.schedule_tool_acknowledgement(
                session_id="livekit-session", turn_id="turn-1", tool_call_id="tool-2"
            )
        )
        self.assertEqual(len(loop.calls), 1)


class AsyncAdapterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.adapter = make_adapter()

    def tearDown(self):
        livekit_adapter.LIVE_ADAPTERS.discard(self.adapter)

    async def test_plain_data_channel_text_is_dispatched_without_json_crash(self):
        dispatched = asyncio.Event()
        received = {}

        async def fake_handle(event):
            received["event"] = event
            dispatched.set()

        packet = SimpleNamespace(
            topic="chat",
            data=b"Hermes, hello from phone",
            participant=SimpleNamespace(identity="phone"),
        )
        with patch.object(self.adapter, "handle_message", fake_handle):
            self.adapter._on_data_received(packet)
            await asyncio.wait_for(dispatched.wait(), timeout=1)

        self.assertEqual(received["event"].text, "Hermes, hello from phone")
        self.assertEqual(received["event"].source.user_id, "phone")

    async def test_message_without_keyterm_is_ignored_until_invoked(self):
        dispatched = AsyncMock()
        self.adapter._room = SimpleNamespace(
            remote_participants={},
            local_participant=SimpleNamespace(set_attributes=AsyncMock()),
        )
        event = livekit_adapter.MessageEvent(
            text="What is the weather?",
            message_type=livekit_adapter.MessageType.VOICE,
            source=self.adapter.build_source(
                chat_id="test-room",
                chat_type="group",
                user_id="alice",
                user_name="Alice",
            ),
        )

        with (
            patch.object(self.adapter, "_publish_agent_event", AsyncMock()),
            patch.object(livekit_adapter.BasePlatformAdapter, "handle_message", dispatched),
        ):
            await self.adapter.handle_message(event)

        dispatched.assert_not_awaited()

    async def test_keyterm_opens_room_for_another_speaker_and_adds_context(self):
        self.adapter._room = SimpleNamespace(
            remote_participants={
                "alice": SimpleNamespace(name="Alice"),
                "bob": SimpleNamespace(name="Bob"),
            },
            local_participant=SimpleNamespace(set_attributes=AsyncMock()),
        )
        published = AsyncMock()
        with patch.object(self.adapter, "_publish_agent_event", published):
            first = livekit_adapter.MessageEvent(
                text="Hermes, plan a Rotorua walk for us",
                message_type=livekit_adapter.MessageType.VOICE,
                source=self.adapter.build_source(
                    chat_id="test-room", chat_type="group", user_id="alice"
                ),
            )
            second = livekit_adapter.MessageEvent(
                text="Make it accessible too",
                message_type=livekit_adapter.MessageType.VOICE,
                source=self.adapter.build_source(
                    chat_id="test-room", chat_type="group", user_id="bob"
                ),
            )

            self.assertTrue(await self.adapter._prepare_invoked_event(first))
            self.assertTrue(await self.adapter._prepare_invoked_event(second))

        self.assertEqual(first.source.user_name, "Alice")
        self.assertEqual(second.source.user_name, "Bob")
        self.assertIn("Alice: plan a Rotorua walk for us", second.channel_prompt)
        self.assertIn("Bob: Make it accessible too", second.channel_prompt)

    async def test_standalone_keyterm_invokes_without_empty_llm_turn(self):
        self.adapter._room = SimpleNamespace(
            remote_participants={"alice": SimpleNamespace(name="Alice")},
            local_participant=SimpleNamespace(set_attributes=AsyncMock()),
        )
        event = livekit_adapter.MessageEvent(
            text="MiRA",
            message_type=livekit_adapter.MessageType.VOICE,
            source=self.adapter.build_source(
                chat_id="test-room", chat_type="group", user_id="alice"
            ),
        )

        with patch.object(self.adapter, "_publish_agent_event", AsyncMock()) as published:
            accepted = await self.adapter._prepare_invoked_event(event)

        self.assertFalse(accepted)
        self.assertTrue(self.adapter._conversation_is_active())
        published.assert_any_await(
            "agent:invoked",
            {"identity": "alice", "name": "Alice", "keyterm": "MiRA"},
        )

    async def test_thinking_state_publishes_standard_and_rich_status(self):
        set_attributes = AsyncMock()
        self.adapter._room = SimpleNamespace(
            remote_participants={},
            local_participant=SimpleNamespace(set_attributes=set_attributes),
        )
        self.adapter._activate_conversation("Hermes")
        self.adapter._active_speaker_identity = "alice"
        self.adapter._active_speaker_name = "Alice"
        self.adapter._active_topic = "accessible Rotorua walks"

        with patch.object(self.adapter, "_publish_agent_event", AsyncMock()) as published:
            await self.adapter._set_agent_state("thinking", force=True)

        attributes = set_attributes.await_args.args[0]
        self.assertEqual(attributes["lk.agent.state"], "thinking")
        self.assertEqual(attributes["mira.agent.invoked"], "true")
        self.assertEqual(attributes["mira.agent.active_speaker_name"], "Alice")
        status_payload = next(
            call.args[1]
            for call in published.await_args_list
            if call.args[0] == "agent:status"
        )
        self.assertEqual(status_payload["schema"], "mira-agent-status.v1")
        self.assertEqual(status_payload["state"], "thinking")
        self.assertTrue(status_payload["can_interrupt"])
        published.assert_any_await("agent:thinking-start")

    async def test_tool_registration_control_message_does_not_require_text(self):
        registered = asyncio.Event()
        received = {}

        async def fake_register(message, identity):
            received["message"] = message
            received["identity"] = identity
            registered.set()

        packet = SimpleNamespace(
            topic="hermes-control",
            data=(
                b'{"type":"client:tool-register","name":"find_local_recommendations",'
                b'"description":"Grounded tourism retrieval","input_schema":{"type":"object"}}'
            ),
            participant=SimpleNamespace(identity="mira-worker"),
        )
        with patch.object(self.adapter, "_register_client_tool", fake_register):
            self.adapter._on_data_received(packet)
            await asyncio.wait_for(registered.wait(), timeout=1)

        self.assertEqual(received["identity"], "mira-worker")
        self.assertEqual(received["message"]["name"], "find_local_recommendations")

    async def test_remote_tool_registration_rejects_untrusted_participant(self):
        message = {
            "name": "find_local_recommendations",
            "description": "Grounded tourism retrieval",
            "input_schema": {"type": "object"},
        }
        with patch.object(self.adapter, "_publish_typed", AsyncMock()) as published:
            await self.adapter._register_client_tool(message, "phone-user")

        published.assert_awaited_once()
        self.assertEqual(
            published.await_args.args[0]["reason"], "owner-not-allowed"
        )

    async def test_remote_tool_registration_rejects_non_allowlisted_tool(self):
        message = {
            "name": "run_shell",
            "description": "Unsafe arbitrary execution",
            "input_schema": {"type": "object"},
        }
        identity = "agent-mira-knowledge-worker-12345678"
        with patch.object(self.adapter, "_publish_typed", AsyncMock()) as published:
            await self.adapter._register_client_tool(message, identity)

        published.assert_awaited_once()
        self.assertEqual(published.await_args.args[0]["reason"], "tool-not-allowed")

    async def test_senderless_text_packet_is_not_collapsed_into_client_identity(self):
        packet = SimpleNamespace(
            topic="hermes-control",
            data=b"Hermes, process this",
            participant=None,
        )

        with patch.object(self.adapter, "handle_message", AsyncMock()) as dispatched:
            self.adapter._on_data_received(packet)
            await asyncio.sleep(0)

        dispatched.assert_not_awaited()

    async def test_image_byte_stream_is_queued_for_next_turn(self):
        published = AsyncMock()

        class Reader:
            info = SimpleNamespace(mime_type="image/png")

            def __aiter__(self):
                async def chunks():
                    yield b"png-"
                    yield b"bytes"

                return chunks()

        with patch.object(self.adapter, "_publish_agent_event", published):
            await self.adapter._receive_image_stream(Reader(), "phone")

        self.assertEqual(len(self.adapter._pending_captures), 1)
        path, mime = self.adapter._pending_captures.pop()
        self.assertEqual(mime, "image/png")
        self.assertEqual(Path(path).read_bytes(), b"png-bytes")
        os.unlink(path)
        published.assert_awaited()

    async def test_tool_acknowledgement_is_spoken_without_chat_transcript(self):
        self.adapter._room = object()
        self.adapter._active_sessions["session"] = asyncio.Event()
        phrase = livekit_adapter.DEFAULT_ACK_PHRASES[0]
        self.adapter._tool_ack_audio_paths[phrase] = __file__

        with (
            patch.object(livekit_adapter.random, "choice", return_value=phrase),
            patch.object(self.adapter, "play_tts", AsyncMock()) as play,
        ):
            await self.adapter._play_tool_acknowledgement(0)

        play.assert_awaited_once_with(
            "test-room",
            __file__,
            metadata={"non_conversational": True, "tool_acknowledgement": True},
        )


if __name__ == "__main__":
    unittest.main()
