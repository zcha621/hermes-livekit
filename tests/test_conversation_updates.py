"""Conversation delivery stays live while tools run, without ending the task."""
import asyncio
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from test_adapter import livekit_adapter as mod, make_adapter


class ConversationUpdateTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.worker = make_adapter()
        self.addCleanup(mod.LIVE_ADAPTERS.discard, self.worker)
        self.worker._running = True
        self.publish = AsyncMock()
        self.worker._room = SimpleNamespace(local_participant=SimpleNamespace(
            publish_data=self.publish, set_attributes=AsyncMock()))
        self.worker._record_agent_transcript = AsyncMock()
        self.worker._activate_invoked_turn('MiRA')
        self.worker._arm_tool_acknowledgement()

    async def test_typed_progress_arrives_before_blocked_tool_finishes_then_final(self):
        release = asyncio.Event()
        tool = asyncio.create_task(release.wait())
        token = mod._text_only_turn.set(True)
        try:
            with patch.object(self.worker, '_speak_conversation_update', AsyncMock()) as speak:
                await self.worker.send('test-room', "I'll check the opening hours.", metadata={'_interim_send': True})
                self.assertFalse(tool.done())
                self.assertTrue(self.worker._invoked_turn_active)
                self.assertEqual(self.publish.call_args.args[0], b"I'll check the opening hours.")
                self.assertFalse(self.worker._tool_ack_pending)
                speak.assert_not_awaited()
                release.set()
                await tool
                await self.worker.send('test-room', 'The museum opens at nine.')
                self.assertFalse(self.worker._invoked_turn_active)
                self.assertEqual(self.worker._agent_state, 'idle')
                self.assertEqual([c.args[0] for c in self.publish.call_args_list if c.kwargs.get('topic') == 'hermes-chat'],
                                 [b"I'll check the opening hours.", b'The museum opens at nine.'])
        finally:
            release.set()
            await tool
            mod._text_only_turn.reset(token)

    async def test_voice_update_does_not_block_tool_or_close_turn(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def speech(text, generation):
            started.set()
            await release.wait()
        with patch.object(self.worker, '_speak_conversation_update', side_effect=speech):
            await asyncio.wait_for(self.worker.send('test-room', 'I found two options.', metadata={'_interim_send': True}), 1)
            await asyncio.wait_for(started.wait(), 1)
            self.assertTrue(self.worker._invoked_turn_active)
            release.set()
            await asyncio.gather(*list(self.worker._tool_ack_tasks))

    async def test_natural_update_suppresses_already_scheduled_generic_ack(self):
        token = mod._text_only_turn.set(True)
        try:
            generation = self.worker._tool_ack_generation
            await self.worker.send('test-room', "I'll check the weather.", metadata={'_interim_send': True})
            await self.worker._play_tool_acknowledgement(generation)
            self.assertEqual(self.publish.await_count, 1)
        finally:
            mod._text_only_turn.reset(token)

    async def test_first_tool_cue_is_visible_for_typed_chat_and_only_once(self):
        token = mod._text_only_turn.set(True)
        try:
            self.assertTrue(self.worker.schedule_tool_acknowledgement())
            self.assertFalse(self.worker.schedule_tool_acknowledgement())
            await asyncio.sleep(0)
            await asyncio.gather(*list(self.worker._tool_ack_tasks))
            self.assertEqual(self.publish.await_count, 1)
            self.assertIn(self.publish.call_args.args[0].decode(), mod.DEFAULT_ACK_PHRASES)
            self.assertTrue(self.worker._invoked_turn_active)
        finally:
            mod._text_only_turn.reset(token)

    async def test_final_response_invalidates_late_update_synthesis(self):
        generation = self.worker._tool_ack_generation
        def synthesize(*, text, output_path):
            Path(output_path).write_bytes(b'test')
            self.worker._finish_tool_acknowledgement_turn()
            return json.dumps({'success': True, 'file_path': output_path})
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(mod.tempfile, 'gettempdir', return_value=directory), \
                patch('tools.tts_tool.check_tts_requirements', return_value=True), \
                patch('tools.tts_tool.text_to_speech_tool', side_effect=synthesize), \
                patch.object(self.worker, 'play_tts', AsyncMock()) as play:
            await self.worker._speak_conversation_update('Still checking.', generation)
            play.assert_not_awaited()
            self.assertEqual(list(Path(directory).rglob('*.mp3')), [])

    async def test_interim_audio_returns_to_thinking_without_finishing_task(self):
        self.worker._audio_source = SimpleNamespace(capture_frame=AsyncMock())
        self.worker._ensure_connected_for_playback = AsyncMock()
        self.worker._publish_agent_event = AsyncMock()
        with patch.object(self.worker, '_decode_audio_to_pcm', return_value=b'\0' * 1920):
            await self.worker.play_tts('test-room', 'update.mp3', caption='I found two options.', metadata={
                '_interim_send': True, '_livekit_update_generation': self.worker._tool_ack_generation,
                '_livekit_transcript_recorded': True,
            })
        self.assertTrue(self.worker._invoked_turn_active)
        self.assertEqual(self.worker._agent_state, 'thinking')
        self.worker._record_agent_transcript.assert_not_awaited()

    async def test_stale_audio_waiting_for_playback_lock_is_discarded(self):
        self.worker._audio_source = SimpleNamespace(capture_frame=AsyncMock())
        await self.worker._playback_lock.acquire()
        task = asyncio.create_task(self.worker.play_tts('test-room', 'stale.mp3', caption='Checking.', metadata={
            '_interim_send': True, '_livekit_update_generation': self.worker._tool_ack_generation,
        }))
        await asyncio.sleep(0)
        self.worker._finish_tool_acknowledgement_turn()
        self.worker._playback_lock.release()
        with patch.object(self.worker, '_decode_audio_to_pcm') as decode:
            await task
            decode.assert_not_called()


    async def test_native_background_completion_bypasses_wake_word_and_keeps_typed_modality(self):
        from gateway.wake import admit_internal_event
        received = []
        self.worker._reply_modalities['test-room'] = True
        event = mod.MessageEvent(text='Background task completed: museum opens at nine.',
            message_type=mod.MessageType.TEXT,
            source=self.worker.build_source(chat_id='test-room', user_id='owner', chat_type='group'),
            internal=True)
        async def handler(message):
            received.append(message)
            self.assertTrue(mod._text_only_turn.get())
            return 'The museum opens at nine.'
        self.worker.set_message_handler(handler)
        with patch.object(self.worker, '_prepare_invoked_event', AsyncMock()) as wake_gate, \
                patch.object(self.worker, '_speak_conversation_update', AsyncMock()) as speak:
            await admit_internal_event(self.worker, event)
            await asyncio.gather(*list(self.worker._background_tasks))
            wake_gate.assert_not_awaited()
            speak.assert_not_awaited()
        self.assertEqual(received, [event])
        self.assertTrue(event._gateway_accepted)
        self.assertEqual([c.args[0] for c in self.publish.call_args_list if c.kwargs.get('topic') == 'hermes-chat'], [b'The museum opens at nine.'])

    async def test_background_completion_for_previous_occupancy_is_not_admitted(self):
        from gateway.wake import admit_internal_event, WakeNotAccepted
        self.worker._reply_modalities['test-room:old:conversation:user'] = True
        self.worker._reset_room_context()
        event = mod.MessageEvent(text='Old result', message_type=mod.MessageType.TEXT,
            source=self.worker.build_source(chat_id='test-room:old:conversation:user', user_id='owner'),
            internal=True)
        with patch.object(self.worker, '_prepare_invoked_event', AsyncMock()) as wake_gate:
            with self.assertRaises(WakeNotAccepted):
                await admit_internal_event(self.worker, event)
            wake_gate.assert_not_awaited()
        self.publish.assert_not_awaited()

    async def test_real_gateway_commentary_metadata_reaches_chat_without_sealing_turn(self):
        from gateway.stream_consumer import GatewayStreamConsumer
        consumer = GatewayStreamConsumer(self.worker, 'test-room')
        token = mod._text_only_turn.set(True)
        try:
            self.assertTrue(await consumer._send_commentary("I'll compare the options."))
            self.assertTrue(self.worker._invoked_turn_active)
            self.assertEqual(self.publish.call_args.args[0], b"I'll compare the options.")
            self.assertFalse(consumer._already_sent)
        finally:
            mod._text_only_turn.reset(token)

    async def test_voice_updates_preserve_message_order_without_blocking_chat(self):
        entered, release = asyncio.Event(), asyncio.Event()
        heard = []
        async def synthesize(text, generation):
            if text == 'First update.':
                entered.set()
                await release.wait()
            heard.append(text)
        with patch.object(self.worker, '_synthesize_conversation_update', side_effect=synthesize):
            await self.worker.send('test-room', 'First update.', metadata={'_interim_send': True})
            await entered.wait()
            await self.worker.send('test-room', 'Second update.', metadata={'_interim_send': True})
            await asyncio.sleep(0)
            self.assertEqual(heard, [])
            release.set()
            await asyncio.gather(*list(self.worker._tool_ack_tasks))
        self.assertEqual(heard, ['First update.', 'Second update.'])
