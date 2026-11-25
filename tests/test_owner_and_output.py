import asyncio
import json
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, patch

from test_adapter import livekit_adapter as mod, make_adapter


def person(identity, account, **metadata):
    return SimpleNamespace(identity=identity, metadata=json.dumps({"mira_account_id": account, **metadata}))


OWNER_ROOM = json.dumps({"agent_id": 42, "agent_owner_account_id": "owner-account"})


class OwnerPresenceTests(TestCase):
    def test_owner_leaves_while_shared_user_and_renderer_remain(self):
        owner = person("alice-phone", "owner-account")
        guest = person("bob", "guest-account", can_bring_agent="true")
        renderer = person("agent-appearance-room", "owner-account", type="agent")
        self.assertTrue(mod._room_has_required_presence(OWNER_ROOM, [owner, guest, renderer]))
        self.assertFalse(mod._room_has_required_presence(OWNER_ROOM, [guest, renderer]))
        self.assertTrue(mod._room_has_required_presence(OWNER_ROOM, [person("alice-browser", "owner-account")]))

    def test_missing_binding_fails_closed_and_legacy_rooms_work(self):
        guest = person("bob", "guest-account")
        self.assertFalse(mod._room_has_required_presence('{"agent_id":42}', [guest]))
        self.assertFalse(mod._room_has_required_presence('invalid', [guest]))
        self.assertTrue(mod._room_has_required_presence('{}', [guest]))
        self.assertTrue(mod._room_has_required_presence('{"agent_video_owner_identity":"bob"}', [guest]))


class OwnerDepartureTests(IsolatedAsyncioTestCase):
    async def test_departure_cancels_only_owned_turn_and_stops_audio(self):
        supervisor = mod.MultiRoomLiveKitAdapter(make_adapter().config)
        worker = supervisor._make_room_worker('room')
        supervisor._room_workers['room'] = worker
        owner = person('alice', 'owner-account')
        guest = person('bob', 'guest-account')
        worker._room = SimpleNamespace(metadata=OWNER_ROOM, remote_participants={'bob': guest})
        worker._audio_source = SimpleNamespace(clear_queue=lambda: None)
        worker.disconnect = AsyncMock()
        task = asyncio.create_task(asyncio.sleep(60))
        supervisor._room_turn_tasks[task] = worker
        worker._on_participant_disconnected(owner)
        for _ in range(10):
            await asyncio.sleep(0)
        self.assertTrue(worker._playback_interrupt.is_set())
        self.assertTrue(task.cancelled())
        self.assertNotIn('room', supervisor._room_workers)
        worker.disconnect.assert_awaited_once()
        mod.LIVE_ADAPTERS.discard(worker)

    async def test_stale_response_never_recreates_departed_room(self):
        adapter = make_adapter()
        adapter._running = True
        adapter._room = None
        adapter._join_room = AsyncMock()
        await adapter._ensure_connected_for_playback()
        adapter._join_room.assert_not_awaited()
        mod.LIVE_ADAPTERS.discard(adapter)


class OutputBoundaryTests(IsolatedAsyncioTestCase):
    def setUp(self):
        self.adapter = make_adapter()
        self.adapter._room = SimpleNamespace(local_participant=SimpleNamespace(publish_data=AsyncMock()))
        self.adapter._audio_source = SimpleNamespace(capture_frame=AsyncMock())

    def tearDown(self):
        mod.LIVE_ADAPTERS.discard(self.adapter)

    async def test_runtime_notices_never_publish_or_play(self):
        messages = [
            'The context should be compacted before continuing.',
            'Compacting conversation history now.',
            'Context window is full. Retrying in 3 seconds.',
            'I need to call the get_location tool.',
            'Analysis: inspect the tool result before answering.',
            '{"tool_calls": [{"name": "get_location"}]}',
            '<think>Private plan</think>',
        ]
        with patch.object(self.adapter, '_decode_audio_to_pcm') as decode:
            for message in messages:
                self.assertEqual(self.adapter.prepare_tts_text(message), '', message)
                await self.adapter.send('room', message)
                await self.adapter.play_tts('room', 'unused.wav', caption=message)
            await self.adapter.send('room', 'Connecting runtime', metadata={'non_conversational': True})
            await self.adapter.play_tts('room', 'unused.wav', caption='Connecting runtime', metadata={'non_conversational': True})
            await self.adapter.send('room', 'Background maintenance started', metadata={'_interim_send': True, 'non_conversational': True})
            await self.adapter.play_tts('room', 'unused.wav', caption='Background maintenance started', metadata={'_interim_send': True, 'non_conversational': True})
            await self.adapter.send_or_update_status('room', 'compressing', 'Background maintenance started')
            decode.assert_not_called()
        self.adapter._room.local_participant.publish_data.assert_not_awaited()
        self.adapter._audio_source.capture_frame.assert_not_awaited()

    async def test_raw_audio_with_hidden_reasoning_is_rejected_even_with_a_safe_answer(self):
        text = '<think>Private plan</think>The museum opens at nine.'
        self.assertEqual(self.adapter.prepare_tts_text(text), 'The museum opens at nine.')
        with patch.object(self.adapter, '_decode_audio_to_pcm') as decode:
            await self.adapter.play_tts('room', 'unsafe.wav', caption=text)
            decode.assert_not_called()

    def test_final_answer_survives_tagged_thinking_and_travel_language(self):
        self.assertEqual(self.adapter.prepare_tts_text('<analysis>call tool</analysis>A compact car is suitable.'), 'A compact car is suitable.')
        self.assertEqual(self.adapter.prepare_tts_text('I am thinking of visiting the museum.'), 'I am thinking of visiting the museum.')

    def test_old_internal_speech_does_not_reenter_conversation_context(self):
        context = self.adapter._merge_db_transcript_into_context([
            {'sequence': 1, 'speaker': 'Agent', 'role': 'assistant', 'text': 'Compacting conversation history now.'},
            {'sequence': 2, 'speaker': 'Traveller', 'role': 'user', 'text': 'Why did you say context compaction?'},
            {'sequence': 3, 'speaker': 'Agent', 'role': 'assistant', 'text': 'The museum opens at nine.'},
        ])
        self.assertNotIn('Compacting conversation history now.', context)
        self.assertIn('Why did you say context compaction?', context)
        self.assertIn('The museum opens at nine.', context)
