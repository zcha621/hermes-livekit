"""Typed meeting turns keep authorization/context but never synthesize speech."""
import asyncio
import json
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from test_adapter import livekit_adapter as mod, make_adapter


class TextChatTests(IsolatedAsyncioTestCase):
    def setUp(self):
        self.adapter = make_adapter()
        self.addCleanup(mod.LIVE_ADAPTERS.discard, self.adapter)
        self.publish = AsyncMock()
        self.adapter._room = SimpleNamespace(
            remote_participants={
                'owner': SimpleNamespace(name='Owner', metadata=json.dumps({'can_bring_agent': 'true'})),
                'guest': SimpleNamespace(name='Guest', metadata=json.dumps({'can_bring_agent': 'false'})),
            },
            local_participant=SimpleNamespace(publish_data=self.publish, set_attributes=AsyncMock()),
        )

    async def test_control_packet_reaches_llm_handler_and_returns_text_without_speech(self):
        delivered = asyncio.Event()
        runner = SimpleNamespace(_send_voice_reply=AsyncMock())
        synthesize = runner._send_voice_reply
        self.adapter.gateway_runner = runner
        self.adapter._process_voice_input = AsyncMock()
        # A repeated answer still closes a text turn even if it was spoken earlier.
        self.adapter._last_spoken_source = 'The museum opens at nine.'
        events = []

        async def llm(event):
            events.append(event)
            self.assertEqual(event.message_type, mod.MessageType.TEXT)
            self.assertTrue(event._livekit_text_only)
            self.assertFalse(self.adapter._should_auto_tts_for_chat(event.source.chat_id))
            self.assertEqual(self.adapter.prepare_tts_text('Answer'), '')
            await runner._send_voice_reply(event, 'The museum opens at nine.')
            return 'The museum opens at nine.'

        async def publish(data, **kwargs):
            if kwargs.get('topic') == 'hermes-chat':
                self.assertEqual(data.decode(), 'The museum opens at nine.')
                delivered.set()

        self.publish.side_effect = publish
        self.adapter.set_message_handler(llm)
        self.adapter._on_data_received(SimpleNamespace(
            participant=SimpleNamespace(identity='owner'), topic='hermes-control',
            data=json.dumps({'type': 'client:message', 'text': 'When does the museum open?'}).encode(),
        ))
        await asyncio.wait_for(delivered.wait(), 5)
        # Let the adapter finish transcript/status cleanup before assertions.
        await asyncio.gather(*list(self.adapter._background_tasks))
        synthesize.assert_not_awaited()
        self.adapter._process_voice_input.assert_not_awaited()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].source.user_id, 'owner')
        self.assertEqual(events[0].text, 'When does the museum open?')
        self.assertTrue(any(row['text'] == 'The museum opens at nine.' for row in self.adapter._conversation_transcript))
        self.assertEqual(self.adapter._agent_state, 'idle')
        self.assertTrue(self.adapter._should_auto_tts_for_chat(events[0].source.chat_id))
        self.assertFalse(mod._text_only_turn.get())

    async def test_guest_and_malformed_messages_do_not_consume_captures_or_dispatch(self):
        with patch.object(self.adapter, 'handle_message', AsyncMock()) as dispatch, \
                patch.object(self.adapter, '_drain_pending_captures') as drain:
            for identity, text in [('guest', 'Hello'), ('owner', {}), ('owner', '  ')]:
                await self.adapter._handle_client_message({'text': text}, identity)
            dispatch.assert_not_awaited()
            drain.assert_not_called()

    async def test_public_chat_and_agent_output_are_not_invocations(self):
        with patch.object(self.adapter, 'handle_message', AsyncMock()) as dispatch:
            for topic in ['lk-chat-topic', 'lk.chat', 'hermes-chat']:
                self.adapter._on_data_received(SimpleNamespace(
                    participant=SimpleNamespace(identity='owner'), topic=topic,
                    data=b'{"message":"Hermes, hello"}',
                ))
            await asyncio.sleep(0)
            dispatch.assert_not_awaited()

    async def test_concurrent_voice_turn_and_next_voice_turn_keep_speech_enabled(self):
        observed = []

        async def process(adapter, event, key):
            await asyncio.sleep(0)
            observed.append((key, adapter._should_auto_tts_for_chat('test-room')))
            if key == 'failed-text':
                raise RuntimeError('LLM failed')

        def event(text_only):
            return SimpleNamespace(_livekit_text_only=text_only, source=SimpleNamespace(chat_id='test-room'))

        with patch.object(mod.BasePlatformAdapter, '_process_message_background', process):
            await asyncio.gather(
                self.adapter._process_message_background(event(True), 'text'),
                self.adapter._process_message_background(event(False), 'voice'),
            )
            with self.assertRaises(RuntimeError):
                await self.adapter._process_message_background(event(True), 'failed-text')
            await self.adapter._process_message_background(event(False), 'next-voice')
        self.assertCountEqual(observed, [('text', False), ('voice', True), ('failed-text', False), ('next-voice', True)])
        self.assertFalse(mod._text_only_turn.get())
