import asyncio
import importlib.util
import json
import os
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import gateway.config as gateway_config
from gateway.config import PlatformConfig

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
ADAPTER_SPEC = importlib.util.spec_from_file_location(
    "hermes_livekit_adapter_test", PLUGIN_ROOT / "adapter.py"
)
livekit_adapter = importlib.util.module_from_spec(ADAPTER_SPEC)
assert ADAPTER_SPEC.loader is not None
sys.modules[ADAPTER_SPEC.name] = livekit_adapter
ADAPTER_SPEC.loader.exec_module(livekit_adapter)

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
        self.adapter._activate_invoked_turn("Hermes")

        self.adapter._interrupt_playback("phone")

        self.assertTrue(self.adapter._playback_interrupt.is_set())
        self.assertTrue(source.cleared)

    def test_barge_in_before_invocation_does_not_stop_playback(self):
        source = SimpleNamespace(clear_queue=lambda: self.fail("queue should stay live"))
        self.adapter._audio_source = source
        self.adapter._is_playing = True

        self.adapter._interrupt_playback("phone")

        self.assertFalse(self.adapter._playback_interrupt.is_set())

    def test_explicit_interrupt_flushes_audio_and_dispatches_priority_stop(self):
        source = SimpleNamespace()
        source.cleared = False
        source.clear_queue = lambda: setattr(source, "cleared", True)
        self.adapter._audio_source = source
        self.adapter._is_playing = True
        self.adapter._activate_invoked_turn("Hermes")

        with (
            patch.object(self.adapter, "_publish_agent_event", AsyncMock()) as published,
            patch.object(self.adapter, "handle_message", AsyncMock()) as dispatched,
        ):
            asyncio.run(
                self.adapter._handle_client_control(
                    {
                        "action": "interrupt",
                        "reason": "user-request",
                        "request_id": "request-123",
                    },
                    "phone",
                )
            )

        self.assertTrue(source.cleared)
        self.assertTrue(self.adapter._playback_interrupt.is_set())
        dispatched.assert_awaited_once()
        event = dispatched.await_args.args[0]
        self.assertEqual(event.message_id, "request-123")
        self.assertEqual(event.text, "/stop")
        self.assertEqual(event.source.user_id, "phone")
        self.assertTrue(getattr(event, "_livekit_invocation_checked"))
        published.assert_awaited_once_with(
            "agent:interrupted",
            {
                "identity": "phone",
                "request_id": "request-123",
                "reason": "user-request",
            },
        )

    def test_explicit_interrupt_is_ignored_without_active_response(self):
        with (
            patch.object(self.adapter, "_publish_agent_event", AsyncMock()) as published,
            patch.object(self.adapter, "handle_message", AsyncMock()) as dispatched,
        ):
            asyncio.run(
                self.adapter._handle_client_control(
                    {"action": "interrupt", "request_id": "request-123"}, "phone"
                )
            )

        dispatched.assert_not_awaited()
        published.assert_awaited_once_with(
            "agent:interrupt-ignored",
            {
                "identity": "phone",
                "request_id": "request-123",
                "reason": "no-active-response",
            },
        )

    def test_keyterm_match_is_case_insensitive_and_strips_wake_phrase(self):
        keyterm, cleaned = self.adapter._match_keyterm(
            "Hey MiRA, find walks near Rotorua"
        )

        self.assertEqual(keyterm, "MiRA")
        self.assertEqual(cleaned, "find walks near Rotorua")

    def test_short_followup_keeps_participant_topic(self):
        topic = self.adapter._topic_for_turn("yes please", "Rotorua family walks")

        self.assertEqual(topic, "Rotorua family walks")

    def test_source_chat_id_uses_bounded_call_fallback_without_metadata(self):
        with patch.object(
            self.adapter, "_participant_connection_metadata", return_value={}
        ):
            alice_first = self.adapter._source_chat_id("alice")
            alice_followup = self.adapter._source_chat_id("alice")
            bob = self.adapter._source_chat_id("bob")

        self.assertEqual(alice_first, alice_followup)
        self.assertNotEqual(alice_first, bob)
        self.assertTrue(alice_first.startswith("test-room:call:"))
        self.assertNotEqual(alice_first, "test-room")

        self.adapter._reset_room_context()
        with patch.object(
            self.adapter, "_participant_connection_metadata", return_value={}
        ):
            self.assertNotEqual(alice_first, self.adapter._source_chat_id("alice"))

    def test_source_chat_id_prefers_trusted_portal_conversation_id(self):
        with patch.object(
            self.adapter,
            "_participant_connection_metadata",
            return_value={"mira_conversation_id": "call-20260827-a"},
        ):
            result = self.adapter._source_chat_id("alice")

        self.assertEqual(
            result, "test-room:conversation:call-20260827-a"
        )

    def test_mcp_identifier_context_includes_room_account_and_device(self):
        with patch.object(
            self.adapter,
            "_participant_connection_metadata",
            return_value={
                "mira_account_id": "acct-1",
                "aware_device_id": "device-1",
            },
        ):
            result = self.adapter._mcp_identifier_context("alice")

        self.assertIn("hermes-mira-context MCP tools", result)
        self.assertIn("room_name='test-room'", result)
        self.assertIn("platform='livekit'", result)
        self.assertIn("user_id='alice'", result)
        self.assertIn("hermes_session_id=", result)
        self.assertIn("mira_account_id='acct-1'", result)
        self.assertIn("aware_device_id='device-1'", result)

    def test_mcp_identifier_context_omits_optional_ids_when_absent(self):
        with patch.object(
            self.adapter, "_participant_connection_metadata", return_value={}
        ):
            result = self.adapter._mcp_identifier_context("alice")

        self.assertIn("room_name='test-room'", result)
        self.assertNotIn("mira_account_id=", result)
        self.assertNotIn("aware_device_id=", result)

    def test_model_prompt_uses_only_bounded_recent_room_transcript(self):
        for index in range(20):
            self.adapter._append_transcript(
                role="user",
                identity="alice",
                name="Alice",
                text=f"utterance-{index}",
                invoked=index == 19,
            )

        context = self.adapter._transcript_context()

        self.assertNotIn("[8] Alice (user): utterance-7", context)
        self.assertIn("[9] Alice (user): utterance-8", context)
        self.assertIn("utterance-19", context)
        self.assertEqual(context.count("Alice (user)"), 12)

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

    def test_visual_source_cues_select_screen_or_current_speaker_camera(self):
        now = time.monotonic()
        self.adapter._video_track_meta = {
            "alice-camera": ("alice", "source_camera"),
            "alice-screen": ("alice", "source_screenshare"),
            "bob-camera": ("bob", "source_camera"),
        }
        self.adapter._latest_video_frames = {
            "alice-camera": (b"alice-camera", now, "source_camera", 640, 480),
            "alice-screen": (b"alice-screen", now - 0.2, "source_screenshare", 1280, 720),
            "bob-camera": (b"bob-camera", now + 0.2, "source_camera", 640, 480),
        }

        screen = self.adapter._latest_frame_for_identity(
            "alice", preferred_source=self.adapter._preferred_video_source(
                "What is on the shared screen?"
            )
        )
        camera = self.adapter._latest_frame_for_identity(
            "alice", preferred_source=self.adapter._preferred_video_source(
                "What can you see in my camera?"
            )
        )

        self.assertEqual(screen[0], b"alice-screen")
        self.assertEqual(camera[0], b"alice-camera")

    def test_camera_cue_never_falls_back_to_another_participant(self):
        now = time.monotonic()
        self.adapter._video_track_meta = {
            "bob-camera": ("bob", "source_camera"),
        }
        self.adapter._latest_video_frames = {
            "bob-camera": (b"bob-camera", now, "source_camera", 640, 480),
        }

        result = self.adapter._latest_frame_for_identity(
            "alice", preferred_source="camera"
        )

        self.assertIsNone(result)

    def test_video_source_name_is_stable_for_unknown_sdk_value(self):
        publication = SimpleNamespace(source="SOURCE_SCREENSHARE")
        source = livekit_adapter.LiveKitAdapter._video_source_name(publication)
        self.assertIn("screenshare", source)

    def test_auto_vision_only_triggers_for_visual_utterances(self):
        self.assertFalse(self.adapter._should_attach_video("Hello, Hermes."))
        self.assertFalse(self.adapter._should_attach_video("What is the weather today?"))
        self.assertTrue(self.adapter._should_attach_video("Can you see my face?"))
        self.assertTrue(self.adapter._should_attach_video("Which button should I tap?"))

    def test_voice_defaults_prioritize_low_latency(self):
        self.assertEqual(self.adapter._silence_threshold_seconds, 0.7)
        self.assertEqual(self.adapter._min_speech_duration_seconds, 0.3)
        self.assertFalse(self.adapter._ack_enabled)
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
        self.adapter._ack_enabled = True
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

    def test_prepare_tts_text_suppresses_leaked_function_invocation(self):
        leaked = "get_confirmed_itinerary(mira_account_id='acct-1')"
        self.assertEqual(self.adapter.prepare_tts_text(leaked), "")

    def test_prepare_tts_text_keeps_conversational_text_around_tool_leak(self):
        text = "I'll check that.\nget_confirmed_itinerary(mira_account_id='acct-1')\nI found two options."
        self.assertEqual(
            self.adapter.prepare_tts_text(text),
            "I'll check that.\n\nI found two options.",
        )

    def test_prepare_tts_text_strips_thinking_and_keeps_final_answer(self):
        text = (
            "<think>First I should inspect the available tools.</think>"
            "The Viaduct has several good lunch options."
        )

        self.assertEqual(
            self.adapter.prepare_tts_text(text),
            "The Viaduct has several good lunch options.",
        )

    def test_prepare_tts_text_does_not_speak_reasoning_only_fallback(self):
        text = (
            "\N{WARNING SIGN}\N{VARIATION SELECTOR-16} The model produced only "
            "internal reasoning and no final answer, despite retries and fallback. "
            "Its last reasoning, which may contain the answer:\n\n"
            "I should inspect the user's profile and decide which tool to call."
        )

        self.assertEqual(
            self.adapter.prepare_tts_text(text),
            livekit_adapter.REASONING_ONLY_SPOKEN_FAILURE,
        )
        self.assertNotIn(
            "inspect the user's profile", self.adapter._prepared_tts_text
        )
        self.assertEqual(self.adapter._prepared_tts_source, text)


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

    async def test_client_message_is_an_explicit_invocation_without_wake_term(self):
        self.adapter._room = SimpleNamespace(
            remote_participants={"traveller": SimpleNamespace(name="Traveller")},
            local_participant=SimpleNamespace(set_attributes=AsyncMock()),
        )

        with (
            patch.object(self.adapter, "_publish_agent_event", AsyncMock()),
            patch.object(
                livekit_adapter.BasePlatformAdapter,
                "handle_message",
                AsyncMock(),
            ) as dispatched,
        ):
            await self.adapter._handle_client_message(
                {"text": "Plan a relaxed day in Christchurch"}, "traveller"
            )

        dispatched.assert_awaited_once()
        event = dispatched.await_args.args[0]
        self.assertEqual(event.text, "Plan a relaxed day in Christchurch")
        self.assertTrue(getattr(event, "_livekit_invocation_checked"))
        self.assertTrue(self.adapter._conversation_is_active())
        self.assertTrue(self.adapter._conversation_transcript[-1]["invoked"])

    async def test_message_without_keyterm_is_transcribed_but_not_dispatched(self):
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

    async def test_each_utterance_requires_keyterm_and_ambient_speech_is_context(self):
        self.adapter._room = SimpleNamespace(
            remote_participants={
                "alice": SimpleNamespace(name="Alice"),
                "bob": SimpleNamespace(name="Bob"),
            },
            local_participant=SimpleNamespace(set_attributes=AsyncMock()),
        )
        published = AsyncMock()
        with patch.object(self.adapter, "_publish_agent_event", published):
            ambient = livekit_adapter.MessageEvent(
                text="We should find an accessible Rotorua walk",
                message_type=livekit_adapter.MessageType.VOICE,
                source=self.adapter.build_source(
                    chat_id="test-room", chat_type="group", user_id="alice"
                ),
            )
            invoked = livekit_adapter.MessageEvent(
                text="Hermes, which option would suit us?",
                message_type=livekit_adapter.MessageType.VOICE,
                source=self.adapter.build_source(
                    chat_id="test-room", chat_type="group", user_id="bob"
                ),
            )
            followup_without_keyterm = livekit_adapter.MessageEvent(
                text="And make it short",
                message_type=livekit_adapter.MessageType.VOICE,
                source=self.adapter.build_source(
                    chat_id="test-room", chat_type="group", user_id="alice"
                ),
            )

            self.assertFalse(await self.adapter._prepare_invoked_event(ambient))
            self.assertTrue(await self.adapter._prepare_invoked_event(invoked))
            self.assertTrue(self.adapter._conversation_is_active())
            self.assertFalse(
                await self.adapter._prepare_invoked_event(followup_without_keyterm)
            )

        self.assertEqual(invoked.source.user_name, "Bob")
        self.assertIn(
            "Alice (user): We should find an accessible Rotorua walk",
            invoked.channel_prompt,
        )
        self.assertIn(
            "Bob (user): Hermes, which option would suit us?",
            invoked.channel_prompt,
        )
        self.assertEqual(invoked.text, "which option would suit us")
        user_events = [
            call.args[1]
            for call in published.await_args_list
            if call.args[0] == "agent:user-transcript"
        ]
        self.assertEqual(
            [event["name"] for event in user_events], ["Alice", "Bob", "Alice"]
        )
        self.assertEqual(
            [event["invoked"] for event in user_events], [False, True, False]
        )

    async def test_native_transcripts_include_participant_and_agent_speech(self):
        publish_transcription = AsyncMock()
        self.adapter._room = SimpleNamespace(
            remote_participants={"alice": SimpleNamespace(name="Alice")},
            local_participant=SimpleNamespace(
                identity="hermes-mira",
                publish_transcription=publish_transcription,
            ),
        )
        self.adapter._audio_track_sids["alice"] = "TR-user"
        self.adapter._local_audio_track_sid = "TR-agent"

        user_entry = self.adapter._append_transcript(
            role="user",
            identity="alice",
            name="Alice",
            text="We were discussing geothermal walks",
        )
        await self.adapter._publish_transcript_entry(user_entry)
        await self.adapter._record_agent_transcript(
            "Waimangu fits that conversation.", kind="speech"
        )

        user_transcription = publish_transcription.await_args_list[0].args[0]
        agent_transcription = publish_transcription.await_args_list[1].args[0]
        self.assertEqual(user_transcription.participant_identity, "alice")
        self.assertEqual(user_transcription.track_sid, "TR-user")
        self.assertEqual(agent_transcription.participant_identity, "hermes-mira")
        self.assertEqual(agent_transcription.track_sid, "TR-agent")
        self.assertTrue(user_transcription.segments[0].final)
        self.assertIn(
            "Hermes (assistant): Waimangu fits that conversation.",
            self.adapter._transcript_context(),
        )

    async def test_spoken_visual_turn_routes_jpeg_as_image_not_voice_attachment(self):
        now = time.monotonic()
        self.adapter._video_track_meta = {
            "alice-camera": ("alice", "camera"),
        }
        self.adapter._latest_video_frames = {
            "alice-camera": (b"camera-jpeg", now, "camera", 640, 480),
        }

        event = self.adapter._build_spoken_transcript_event(
            "MiRA, what can you see in my camera?", "alice"
        )

        self.assertEqual(event.message_type, livekit_adapter.MessageType.TEXT)
        self.assertTrue(event.metadata["livekit_spoken_transcript"])
        self.assertTrue(event.metadata["media_already_transcribed"])
        self.assertEqual(event.media_types, ["image/jpeg"])
        self.assertEqual(Path(event.media_urls[0]).read_bytes(), b"camera-jpeg")
        os.unlink(event.media_urls[0])

    async def test_spoken_text_event_still_publishes_native_transcription(self):
        publish_transcription = AsyncMock()
        self.adapter._room = SimpleNamespace(
            remote_participants={"alice": SimpleNamespace(name="Alice")},
            local_participant=SimpleNamespace(
                set_attributes=AsyncMock(),
                publish_transcription=publish_transcription,
            ),
        )
        self.adapter._audio_track_sids["alice"] = "TR-user"
        event = self.adapter._build_spoken_transcript_event(
            "MiRA, use the live context", "alice"
        )

        with patch.object(
            livekit_adapter.BasePlatformAdapter, "handle_message", AsyncMock()
        ):
            await self.adapter.handle_message(event)

        publish_transcription.assert_awaited_once()
        self.assertEqual(
            publish_transcription.await_args.args[0].participant_identity, "alice"
        )

    async def test_send_voice_routes_auto_tts_to_livekit_audio_track(self):
        expected = livekit_adapter.SendResult(success=True, message_id="tts-1")

        with patch.object(
            self.adapter, "play_tts", AsyncMock(return_value=expected)
        ) as play_tts:
            result = await self.adapter.send_voice(
                "test-room",
                "reply.mp3",
                caption="The camera shows test code 7429.",
                reply_to="turn-1",
                metadata={"source": "auto-tts"},
            )

        self.assertIs(result, expected)
        play_tts.assert_awaited_once_with(
            "test-room",
            "reply.mp3",
            reply_to="turn-1",
            metadata={"source": "auto-tts"},
            caption="The camera shows test code 7429.",
        )

    async def test_tts_speech_is_logged_once_and_reused_as_future_context(self):
        publish_transcription = AsyncMock()
        local_participant = SimpleNamespace(
            identity="hermes-mira",
            publish_data=AsyncMock(),
            publish_transcription=publish_transcription,
            set_attributes=AsyncMock(),
        )
        self.adapter._room = SimpleNamespace(
            remote_participants={}, local_participant=local_participant
        )
        self.adapter._audio_source = SimpleNamespace(
            capture_frame=AsyncMock(), clear_queue=lambda: None
        )
        response = "The earlier group discussion points to Waimangu."
        self.adapter._activate_invoked_turn("MiRA")
        self.assertEqual(self.adapter.prepare_tts_text(response), response)

        with patch.object(
            self.adapter, "_decode_audio_to_pcm", return_value=b"\x00" * 1920
        ):
            played = await self.adapter.play_tts("test-room", __file__)
        sent = await self.adapter.send("test-room", response)

        self.assertTrue(played.success)
        self.assertTrue(sent.success)
        assistant_entries = [
            entry
            for entry in self.adapter._conversation_transcript
            if entry["role"] == "assistant"
        ]
        self.assertEqual(len(assistant_entries), 1)
        self.assertEqual(assistant_entries[0]["text"], response)
        self.assertEqual(publish_transcription.await_count, 1)
        self.assertIn(response, self.adapter._transcript_context())

    async def test_standalone_keyterm_arms_same_participant_followup(self):
        self.adapter._room = SimpleNamespace(
            remote_participants={"alice": SimpleNamespace(name="Alice")},
            local_participant=SimpleNamespace(set_attributes=AsyncMock()),
        )
        wake = livekit_adapter.MessageEvent(
            text="MiRA",
            message_type=livekit_adapter.MessageType.VOICE,
            source=self.adapter.build_source(
                chat_id="test-room", chat_type="group", user_id="alice"
            ),
        )
        question = livekit_adapter.MessageEvent(
            text="Can you see my face in the camera?",
            message_type=livekit_adapter.MessageType.VOICE,
            source=self.adapter.build_source(
                chat_id="test-room", chat_type="group", user_id="alice"
            ),
        )

        with patch.object(self.adapter, "_publish_agent_event", AsyncMock()) as published:
            wake_accepted = await self.adapter._prepare_invoked_event(wake)
            question_accepted = await self.adapter._prepare_invoked_event(question)

        self.assertFalse(wake_accepted)
        self.assertTrue(question_accepted)
        self.assertTrue(self.adapter._conversation_is_active())
        self.assertEqual(question.text, "Can you see my face in the camera?")
        self.assertNotIn("alice", self.adapter._armed_participant_wakes)
        published.assert_any_await(
            "agent:invoked",
            {"identity": "alice", "name": "Alice", "keyterm": "MiRA"},
        )

    async def test_standalone_keyterm_cannot_be_consumed_by_another_participant(self):
        self.adapter._room = SimpleNamespace(
            remote_participants={
                "alice": SimpleNamespace(name="Alice"),
                "bob": SimpleNamespace(name="Bob"),
            },
            local_participant=SimpleNamespace(set_attributes=AsyncMock()),
        )
        wake = livekit_adapter.MessageEvent(
            text="MiRA",
            message_type=livekit_adapter.MessageType.VOICE,
            source=self.adapter.build_source(
                chat_id="test-room", chat_type="group", user_id="alice"
            ),
        )
        bob = livekit_adapter.MessageEvent(
            text="What should we do?",
            message_type=livekit_adapter.MessageType.VOICE,
            source=self.adapter.build_source(
                chat_id="test-room", chat_type="group", user_id="bob"
            ),
        )

        with patch.object(self.adapter, "_publish_agent_event", AsyncMock()):
            self.assertFalse(await self.adapter._prepare_invoked_event(wake))
            self.assertFalse(await self.adapter._prepare_invoked_event(bob))

        self.assertIn("alice", self.adapter._armed_participant_wakes)

    async def test_standalone_keyterm_followup_expires(self):
        self.adapter._room = SimpleNamespace(
            remote_participants={"alice": SimpleNamespace(name="Alice")},
            local_participant=SimpleNamespace(set_attributes=AsyncMock()),
        )
        wake = livekit_adapter.MessageEvent(
            text="MiRA",
            message_type=livekit_adapter.MessageType.VOICE,
            source=self.adapter.build_source(
                chat_id="test-room", chat_type="group", user_id="alice"
            ),
        )
        late_question = livekit_adapter.MessageEvent(
            text="What should we do?",
            message_type=livekit_adapter.MessageType.VOICE,
            source=self.adapter.build_source(
                chat_id="test-room", chat_type="group", user_id="alice"
            ),
        )

        with patch.object(self.adapter, "_publish_agent_event", AsyncMock()):
            self.assertFalse(await self.adapter._prepare_invoked_event(wake))
            self.adapter._armed_participant_wakes["alice"] = ("MiRA", 10.0)
            with patch.object(livekit_adapter.time, "monotonic", return_value=16.0):
                self.assertFalse(
                    await self.adapter._prepare_invoked_event(late_question)
                )

        self.assertNotIn("alice", self.adapter._armed_participant_wakes)

    async def test_thinking_state_publishes_standard_and_rich_status(self):
        set_attributes = AsyncMock()
        self.adapter._room = SimpleNamespace(
            remote_participants={},
            local_participant=SimpleNamespace(set_attributes=set_attributes),
        )
        self.adapter._activate_invoked_turn("Hermes")
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
                b'{"type":"client:tool-register","name":"custom_client_tool",'
                b'"description":"Grounded tourism retrieval","input_schema":{"type":"object"}}'
            ),
            participant=SimpleNamespace(identity="mira-worker"),
        )
        with patch.object(self.adapter, "_register_client_tool", fake_register):
            self.adapter._on_data_received(packet)
            await asyncio.wait_for(registered.wait(), timeout=1)

        self.assertEqual(received["identity"], "mira-worker")
        self.assertEqual(received["message"]["name"], "custom_client_tool")

    async def test_client_interrupt_packet_routes_from_shared_control_topic(self):
        routed = asyncio.Event()
        received = {}

        async def fake_control(message, identity):
            received["message"] = message
            received["identity"] = identity
            routed.set()

        packet = SimpleNamespace(
            topic="hermes-control",
            data=(
                b'{"type":"client:control","action":"interrupt",'
                b'"reason":"user-request","request_id":"request-123"}'
            ),
            participant=SimpleNamespace(identity="phone"),
        )
        with patch.object(self.adapter, "_handle_client_control", fake_control):
            self.adapter._on_data_received(packet)
            await asyncio.wait_for(routed.wait(), timeout=1)

        self.assertEqual(received["identity"], "phone")
        self.assertEqual(received["message"]["action"], "interrupt")
        self.assertEqual(received["message"]["request_id"], "request-123")

    async def test_push_to_talk_release_invokes_only_the_authenticated_sender(self):
        bytes_per_second = (
            livekit_adapter.SAMPLE_RATE * livekit_adapter.NUM_CHANNELS * 2
        )
        self.adapter._audio_buffers["alice"] = bytearray()
        self.adapter._room = SimpleNamespace(
            remote_participants={"alice": SimpleNamespace(name="Alice")},
            local_participant=SimpleNamespace(set_attributes=AsyncMock()),
        )

        with (
            patch.object(self.adapter, "_publish_agent_event", AsyncMock()),
            patch.object(self.adapter, "_process_voice_input", AsyncMock()) as process,
            patch.object(
                livekit_adapter, "PUSH_TO_TALK_RELEASE_GRACE_SECONDS", 0
            ),
        ):
            await self.adapter._handle_client_control(
                {
                    "action": "push-to-talk-start",
                    "request_id": "press-123",
                    "identity": "spoofed-user",
                },
                "alice",
            )
            self.adapter._audio_buffers["alice"] = bytearray(bytes_per_second)
            await self.adapter._handle_client_control(
                {"action": "push-to-talk-end", "request_id": "press-123"},
                "alice",
            )
            await asyncio.sleep(0)

        process.assert_awaited_once()
        self.assertEqual(process.await_args.args[0], "alice")
        self.assertTrue(process.await_args.kwargs["force_invoke"])
        self.assertEqual(
            process.await_args.kwargs["invocation_keyterm"], "@Agent"
        )
        self.assertNotIn("alice", self.adapter._push_to_talk_sessions)

    async def test_push_to_talk_release_cannot_flush_another_participant(self):
        self.adapter._audio_buffers["alice"] = bytearray(64_000)
        self.adapter._room = SimpleNamespace(
            remote_participants={"alice": SimpleNamespace(name="Alice")},
            local_participant=SimpleNamespace(set_attributes=AsyncMock()),
        )

        with (
            patch.object(self.adapter, "_publish_agent_event", AsyncMock()) as published,
            patch.object(self.adapter, "_process_voice_input", AsyncMock()) as process,
        ):
            await self.adapter._handle_client_control(
                {"action": "push-to-talk-start", "request_id": "press-123"},
                "alice",
            )
            await self.adapter._handle_client_control(
                {"action": "push-to-talk-end", "request_id": "press-123"},
                "bob",
            )

        process.assert_not_awaited()
        self.assertEqual(
            self.adapter._push_to_talk_sessions["alice"], "press-123"
        )
        published.assert_any_await(
            "agent:push-to-talk-ignored",
            {
                "identity": "bob",
                "request_id": "press-123",
                "reason": "not-active",
            },
        )

    async def test_push_to_talk_start_barges_in_while_agent_is_speaking(self):
        source = SimpleNamespace()
        source.cleared = False
        source.clear_queue = lambda: setattr(source, "cleared", True)
        self.adapter._audio_source = source
        self.adapter._is_playing = True
        self.adapter._activate_invoked_turn("Hermes")
        self.adapter._room = SimpleNamespace(
            remote_participants={"alice": SimpleNamespace(name="Alice")},
            local_participant=SimpleNamespace(set_attributes=AsyncMock()),
        )

        with (
            patch.object(self.adapter, "_publish_agent_event", AsyncMock()),
            patch.object(self.adapter, "handle_message", AsyncMock()) as dispatched,
        ):
            await self.adapter._handle_client_control(
                {"action": "push-to-talk-start", "request_id": "press-1"},
                "alice",
            )

        # The agent's speech is flushed immediately...
        self.assertTrue(source.cleared)
        self.assertTrue(self.adapter._playback_interrupt.is_set())
        # ...and its in-flight backend work is dropped via the priority /stop.
        dispatched.assert_awaited_once()
        event = dispatched.await_args.args[0]
        self.assertEqual(event.text, "/stop")
        self.assertEqual(event.source.user_id, "alice")
        # The press itself still starts a held @Agent turn as normal.
        self.assertEqual(self.adapter._push_to_talk_sessions["alice"], "press-1")

    async def test_push_to_talk_start_skips_stop_dispatch_when_agent_is_idle(self):
        self.adapter._room = SimpleNamespace(
            remote_participants={"alice": SimpleNamespace(name="Alice")},
            local_participant=SimpleNamespace(set_attributes=AsyncMock()),
        )

        with (
            patch.object(self.adapter, "_publish_agent_event", AsyncMock()),
            patch.object(self.adapter, "handle_message", AsyncMock()) as dispatched,
        ):
            await self.adapter._handle_client_control(
                {"action": "push-to-talk-start", "request_id": "press-1"},
                "alice",
            )

        dispatched.assert_not_awaited()
        self.assertEqual(self.adapter._push_to_talk_sessions["alice"], "press-1")

    def test_can_bring_agent_defaults_true_for_legacy_connections_without_metadata(self):
        self.adapter._room = SimpleNamespace(
            remote_participants={"alice": SimpleNamespace(name="Alice", metadata="")}
        )
        self.assertTrue(self.adapter._can_bring_agent("alice"))

    def test_can_bring_agent_reads_room_owner_flag_from_participant_metadata(self):
        self.adapter._room = SimpleNamespace(
            remote_participants={
                "alice": SimpleNamespace(
                    name="Alice",
                    metadata=json.dumps({"can_bring_agent": "true"}),
                ),
                "bob": SimpleNamespace(
                    name="Bob",
                    metadata=json.dumps({"can_bring_agent": "false"}),
                ),
            }
        )
        self.assertTrue(self.adapter._can_bring_agent("alice"))
        self.assertFalse(self.adapter._can_bring_agent("bob"))

    async def test_push_to_talk_start_rejected_for_a_participant_who_did_not_create_the_room(self):
        self.adapter._room = SimpleNamespace(
            remote_participants={
                "bob": SimpleNamespace(
                    name="Bob", metadata=json.dumps({"can_bring_agent": "false"})
                )
            },
            local_participant=SimpleNamespace(set_attributes=AsyncMock()),
        )

        with (
            patch.object(self.adapter, "_publish_agent_event", AsyncMock()) as published,
            patch.object(self.adapter, "handle_message", AsyncMock()) as dispatched,
        ):
            await self.adapter._handle_client_control(
                {"action": "push-to-talk-start", "request_id": "press-1"}, "bob"
            )

        dispatched.assert_not_awaited()
        self.assertNotIn("bob", self.adapter._push_to_talk_sessions)
        published.assert_awaited_once_with(
            "agent:push-to-talk-ignored",
            {"identity": "bob", "request_id": "press-1", "reason": "not-authorized"},
        )

    async def test_explicit_interrupt_rejected_for_a_participant_who_did_not_create_the_room(self):
        self.adapter._activate_invoked_turn("Hermes")
        self.adapter._is_playing = True
        self.adapter._room = SimpleNamespace(
            remote_participants={
                "bob": SimpleNamespace(
                    name="Bob", metadata=json.dumps({"can_bring_agent": "false"})
                )
            },
            local_participant=SimpleNamespace(set_attributes=AsyncMock()),
        )

        with (
            patch.object(self.adapter, "_publish_agent_event", AsyncMock()) as published,
            patch.object(self.adapter, "handle_message", AsyncMock()) as dispatched,
        ):
            await self.adapter._handle_client_control(
                {"action": "interrupt", "request_id": "press-1"}, "bob"
            )

        dispatched.assert_not_awaited()
        self.assertFalse(self.adapter._playback_interrupt.is_set())
        published.assert_awaited_once_with(
            "agent:interrupt-ignored",
            {"identity": "bob", "request_id": "press-1", "reason": "not-authorized"},
        )

    async def test_forced_agent_button_turn_preserves_speaker_identity(self):
        self.adapter._room = SimpleNamespace(
            remote_participants={"alice": SimpleNamespace(name="Alice")},
            local_participant=SimpleNamespace(set_attributes=AsyncMock()),
        )
        event = livekit_adapter.MessageEvent(
            text="What is next on my itinerary?",
            message_type=livekit_adapter.MessageType.VOICE,
            source=self.adapter.build_source(
                chat_id="test-room",
                chat_type="group",
                user_id="alice",
                user_name="alice",
            ),
        )

        with patch.object(self.adapter, "_publish_agent_event", AsyncMock()):
            accepted = await self.adapter._prepare_invoked_event(
                event,
                force_invoke=True,
                invocation_keyterm="@Agent",
            )

        self.assertTrue(accepted)
        self.assertEqual(event.source.user_id, "alice")
        self.assertEqual(event.source.user_name, "Alice")
        self.assertEqual(self.adapter._active_speaker_identity, "alice")
        self.assertEqual(self.adapter._conversation_transcript[-1]["keyterm"], "@Agent")

    async def test_remote_tool_registration_rejects_untrusted_participant(self):
        message = {
            "name": "custom_client_tool",
            "description": "Current consented trip context",
            "input_schema": {"type": "object"},
        }
        with patch.object(self.adapter, "_publish_typed", AsyncMock()) as published:
            await self.adapter._register_client_tool(message, "phone-user")

        published.assert_awaited_once()
        self.assertEqual(
            published.await_args.args[0]["reason"], "owner-not-allowed"
        )

    async def test_remote_tool_handler_adds_trusted_session_source_and_returns_json(self):
        owner = "agent-mira-knowledge-worker-12345678"
        self.adapter._room = SimpleNamespace(remote_participants={owner: object()})
        handler = self.adapter._build_tool_handler(owner, "custom_client_tool")

        async def publish(message, **_kwargs):
            await self.adapter._handle_tool_result(
                {
                    "call_id": message["call_id"],
                    "result": {"linked": True, "draft": {"revision": 4}},
                },
                owner,
            )

        session_values = {
            "HERMES_SESSION_PLATFORM": "discord",
            "HERMES_SESSION_USER_ID": "discord-user-42",
            "HERMES_SESSION_USER_NAME": "Traveller",
            "HERMES_SESSION_CHAT_ID": "channel-1",
            "HERMES_SESSION_ID": "fallback-session",
        }
        with (
            patch.object(self.adapter, "_publish_typed", side_effect=publish) as sent,
            patch(
                "gateway.session_context.get_session_env",
                side_effect=lambda name, default="": session_values.get(name, default),
            ),
        ):
            result = await handler(
                {"action": "load", "_mira_source": {"user_id": "forged"}},
                session_id="canonical-session",
            )

        arguments = sent.await_args.args[0]["arguments"]
        self.assertEqual(
            arguments["_mira_source"],
            {
                "platform": "discord",
                "user_id": "discord-user-42",
                "user_name": "Traveller",
                "chat_id": "channel-1",
                "hermes_session_id": "canonical-session",
            },
        )
        self.assertEqual(
            json.loads(result), {"linked": True, "draft": {"revision": 4}}
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

    async def test_simulated_worker_registration_dynamically_registers_tool(self):
        identity = "simulated-agent-af292dcc9e40"
        self.adapter._room = SimpleNamespace(
            remote_participants={
                identity: SimpleNamespace(kind=SimpleNamespace(name="PARTICIPANT_KIND_AGENT"))
            }
        )
        self.adapter._allowed_remote_tool_names = frozenset({"trip_context_tool"})
        message = {
            "name": "trip_context_tool",
            "description": "Current consented trip context",
            "input_schema": {"type": "object"},
        }

        with (
            patch("tools.registry.registry.register") as register,
            patch.object(self.adapter, "_publish_typed", AsyncMock()) as published,
        ):
            await self.adapter._register_client_tool(message, identity)

        register.assert_called_once()
        self.assertEqual(self.adapter._tool_owners["trip_context_tool"], identity)
        self.assertTrue(published.await_args.args[0]["success"])

    async def test_simulated_worker_accepts_livekit_numeric_agent_kind(self):
        identity = "simulated-agent-numeric"
        self.adapter._room = SimpleNamespace(
            remote_participants={identity: SimpleNamespace(kind=4)}
        )

        self.assertTrue(self.adapter._remote_participant_is_agent(identity))

    async def test_simulated_phone_cannot_register_remote_tool(self):
        identity = "simulated-agent-phone"
        self.adapter._room = SimpleNamespace(
            remote_participants={
                identity: SimpleNamespace(kind=SimpleNamespace(name="PARTICIPANT_KIND_STANDARD"))
            }
        )
        message = {
            "name": "custom_client_tool",
            "description": "Grounded tourism retrieval",
            "input_schema": {"type": "object"},
        }

        with patch.object(self.adapter, "_publish_typed", AsyncMock()) as published:
            await self.adapter._register_client_tool(message, identity)

        self.assertEqual(
            published.await_args.args[0]["reason"], "owner-kind-not-agent"
        )

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

    async def test_tool_acknowledgement_is_spoken_with_transcript_caption(self):
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
            caption=phrase,
        )


if __name__ == "__main__":
    unittest.main()
