"""Exercise the installed Hermes runner's captionless auto-voice path."""
import asyncio
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from gateway.run_voice import GatewayVoiceMixin
from test_adapter import livekit_adapter as mod, make_adapter


class RunnerVoiceTests(IsolatedAsyncioTestCase):
    def setUp(self):
        self.worker = make_adapter()
        self.addCleanup(mod.LIVE_ADAPTERS.discard, self.worker)
        self.worker._room = SimpleNamespace()
        self.worker._audio_source = SimpleNamespace(capture_frame=AsyncMock())
        self.worker._ensure_connected_for_playback = AsyncMock()
        self.worker._record_agent_transcript = AsyncMock()
        self.worker._set_agent_state = AsyncMock(return_value=1)
        self.worker._finish_state_if_current = AsyncMock()
        self.worker._publish_agent_event = AsyncMock()
        self.event = SimpleNamespace(source=SimpleNamespace(
            platform=mod.Platform("livekit"), chat_id="test-room"))

    async def invoke(self, runner, text):
        token = mod.active_room.set(self.worker)
        try:
            await runner._send_voice_reply(self.event, text)
        finally:
            mod.active_room.reset(token)

    async def test_installed_runner_sanitizes_before_synthesis_and_plays_captionless_reply(self):
        worker = self.worker

        class Runner(GatewayVoiceMixin):
            def _delivery_adapter_for(self, source):
                return worker

            def _get_guild_id(self, event):
                return None

            def _reply_anchor_for_event(self, event):
                return None

            def _thread_metadata_for_source(self, source, anchor):
                return {}

        runner = Runner()
        mod._install_runner_voice_preparation(runner)
        wrapped = runner._send_voice_reply
        mod._install_runner_voice_preparation(runner)
        self.assertIs(runner._send_voice_reply, wrapped)
        with tempfile.TemporaryDirectory() as directory:
            audio = str(Path(directory) / "reply.wav")

            def synthesize(*, text, output_path):
                self.assertEqual(text, "The museum opens at nine.")
                # A concurrent acknowledgement must not erase this file's proof.
                worker._prepared_tts_text = ""
                Path(output_path).write_bytes(b"test")
                return json.dumps({"success": True, "file_path": output_path})

            with patch("gateway.run_voice.build_auto_tts_output_path", return_value=audio), \
                    patch("tools.tts_tool.text_to_speech_tool", side_effect=synthesize) as tts, \
                    patch.object(worker, "_decode_audio_to_pcm", return_value=b"\0" * 1920):
                source = "<think>Private plan</think>The museum opens at nine."
                await self.invoke(runner, source)
                tts.assert_called_once()
                worker._audio_source.capture_frame.assert_awaited_once()
                worker._set_agent_state.assert_awaited_once_with("speaking", force=True)
                worker._record_agent_transcript.assert_awaited_once_with(
                    "The museum opens at nine.", kind="speech")
                self.assertEqual(worker._last_spoken_source, source)
                self.assertFalse(Path(audio).exists())
        self.assertIsNone(mod._runner_voice_script.get())

    async def test_internal_notice_never_reaches_runner_synthesis(self):
        runner = SimpleNamespace(_send_voice_reply=AsyncMock())
        original = runner._send_voice_reply
        mod._install_runner_voice_preparation(runner)
        await self.invoke(runner, "Compacting conversation history now.")
        original.assert_not_awaited()

    async def test_unrelated_platform_is_unchanged_and_wrong_room_is_rejected(self):
        runner = SimpleNamespace(_send_voice_reply=AsyncMock())
        original = runner._send_voice_reply
        mod._install_runner_voice_preparation(runner)
        self.event.source.platform = "discord"
        await self.invoke(runner, "Unchanged content")
        original.assert_awaited_once_with(self.event, "Unchanged content")
        original.reset_mock()
        self.event.source.platform = "livekit"
        self.event.source.chat_id = "another-room"
        await self.invoke(runner, "A normal answer")
        original.assert_not_awaited()

    async def test_verified_scripts_are_isolated_between_concurrent_turns(self):
        observed = []

        async def original(event, text):
            await asyncio.sleep(0)
            observed.append((text, mod._runner_voice_script.get()[1]))

        runner = SimpleNamespace(_send_voice_reply=original)
        mod._install_runner_voice_preparation(runner)
        await asyncio.gather(self.invoke(runner, "First reply"), self.invoke(runner, "Second reply"))
        self.assertCountEqual(observed, [("First reply", "First reply"), ("Second reply", "Second reply")])
        self.assertIsNone(mod._runner_voice_script.get())

    async def test_unknown_captionless_audio_is_still_rejected(self):
        with patch.object(self.worker, "_decode_audio_to_pcm") as decode:
            await self.worker.play_tts("test-room", "unknown.wav")
            decode.assert_not_called()

    async def test_push_to_talk_during_synthesis_discards_old_audio_but_next_reply_can_speak(self):
        worker = self.worker
        async def original(event, text):
            worker._interrupt_playback('owner', explicit=True)
            await worker.play_tts('test-room', 'late.mp3')
        runner = SimpleNamespace(_send_voice_reply=original)
        mod._install_runner_voice_preparation(runner)
        with patch.object(worker, '_decode_audio_to_pcm') as decode:
            await self.invoke(runner, 'Old answer.')
            decode.assert_not_called()
        worker.prepare_tts_text('New answer.')
        with patch.object(worker, '_decode_audio_to_pcm', return_value=b'\0' * 1920):
            await worker.play_tts('test-room', 'new.mp3')
        worker._audio_source.capture_frame.assert_awaited_once()
