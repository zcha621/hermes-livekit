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


def make_adapter():
    return livekit_adapter.LiveKitAdapter(
        PlatformConfig(
            enabled=True,
            extra={
                "url": "ws://example.invalid",
                "api_key": "key",
                "api_secret": "secret",
                "room": "test-room",
                "agent_name": "Hermes",
            },
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

        self.adapter._interrupt_playback("phone")

        self.assertTrue(self.adapter._playback_interrupt.is_set())
        self.assertTrue(source.cleared)

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
            data=b"hello from phone",
            participant=SimpleNamespace(identity="phone"),
        )
        with patch.object(self.adapter, "handle_message", fake_handle):
            self.adapter._on_data_received(packet)
            await asyncio.wait_for(dispatched.wait(), timeout=1)

        self.assertEqual(received["event"].text, "hello from phone")
        self.assertEqual(received["event"].source.user_id, "phone")

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

    async def test_slow_turn_defaults_to_status_only_acknowledgement(self):
        self.adapter._room = object()
        self.adapter._active_sessions["session"] = asyncio.Event()
        self.adapter._work_ack_audio_path = __file__

        with (
            patch.object(livekit_adapter, "WORK_ACK_DELAY", 0),
            patch.object(livekit_adapter, "WORK_ACK_MODE", "status"),
            patch.object(self.adapter, "_publish_agent_event", AsyncMock()) as publish,
            patch.object(self.adapter, "send", AsyncMock()) as send,
            patch.object(self.adapter, "play_tts", AsyncMock()) as play,
        ):
            await self.adapter._send_work_ack_if_needed("session", "room")

        publish.assert_awaited()
        send.assert_not_awaited()
        play.assert_not_awaited()

    async def test_spoken_acknowledgement_remains_opt_in(self):
        self.adapter._room = object()
        self.adapter._active_sessions["session"] = asyncio.Event()
        self.adapter._work_ack_audio_path = __file__

        with (
            patch.object(livekit_adapter, "WORK_ACK_DELAY", 0),
            patch.object(livekit_adapter, "WORK_ACK_MODE", "spoken"),
            patch.object(self.adapter, "_publish_agent_event", AsyncMock()),
            patch.object(self.adapter, "send", AsyncMock()) as send,
            patch.object(self.adapter, "play_tts", AsyncMock()) as play,
        ):
            await self.adapter._send_work_ack_if_needed("session", "room")

        send.assert_awaited_once()
        play.assert_awaited_once_with("room", __file__)


if __name__ == "__main__":
    unittest.main()
