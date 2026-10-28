import asyncio
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock, patch
from test_adapter import livekit_adapter as mod, make_adapter


class MultiRoomTests(IsolatedAsyncioTestCase):
    def setUp(self):
        old = make_adapter()
        self.adapter = mod.MultiRoomLiveKitAdapter(old.config)
        mod.LIVE_ADAPTERS.discard(old)
        self.addAsyncCleanup(self.adapter.disconnect)

    def worker(self, name):
        worker = self.adapter._make_room_worker(name)
        worker._room = SimpleNamespace(isconnected=lambda: True)
        worker.connect = AsyncMock(return_value=True)
        async def disconnect():
            mod.LIVE_ADAPTERS.discard(worker)
        worker.disconnect = AsyncMock(side_effect=disconnect)
        return worker

    async def test_two_occupied_rooms_and_independent_departure(self):
        a, b = self.worker('a'), self.worker('b')
        self.adapter._make_room_worker = Mock(side_effect=[a,b])
        self.adapter._occupied_room_names = AsyncMock(return_value={'a','b'})
        await self.adapter._reconcile_room_workers()
        self.assertEqual(set(self.adapter._room_workers), {'a','b'})
        a.connect.assert_awaited_once(); b.connect.assert_awaited_once()
        self.adapter._occupied_room_names.return_value = {'b'}
        await self.adapter._reconcile_room_workers()
        a.disconnect.assert_awaited_once(); b.disconnect.assert_not_awaited()
        self.assertIs(self.adapter._room_workers['b'], b)

    async def test_failed_discovery_keeps_connections(self):
        a = self.worker('a'); self.adapter._room_workers['a'] = a
        self.adapter._occupied_room_names = AsyncMock(side_effect=RuntimeError('offline'))
        await self.adapter._reconcile_room_workers()
        self.assertIs(self.adapter._room_workers['a'], a)
        a.disconnect.assert_not_awaited()

    async def test_replies_require_matching_room_and_occupancy(self):
        a, b = self.worker('a'), self.worker('b')
        self.adapter._room_workers.update(a=a,b=b)
        a.send = AsyncMock(return_value=mod.SendResult(success=True))
        b.send = AsyncMock(return_value=mod.SendResult(success=True))
        a.play_tts = AsyncMock(return_value=mod.SendResult(success=True))
        b.play_tts = AsyncMock(return_value=mod.SendResult(success=True))
        chat = 'a:' + a._session_epoch + ':call:user'
        await self.adapter.send(chat, 'hello')
        await self.adapter.send_voice(chat, '/tmp/speech.wav')
        a.send.assert_awaited_once(); a.play_tts.assert_awaited_once()
        b.send.assert_not_awaited(); b.play_tts.assert_not_awaited()
        self.assertFalse((await self.adapter.send('a:old:call:user','late')).success)
        self.assertFalse((await self.adapter.send('unknown','late')).success)

    async def test_cancelling_one_room_does_not_cancel_other_turn(self):
        a, b = self.worker('a'), self.worker('b')
        self.adapter._room_workers.update(a=a,b=b)
        one = asyncio.create_task(asyncio.sleep(60))
        two = asyncio.create_task(asyncio.sleep(60))
        self.adapter._room_turn_tasks.update({one:a,two:b})
        await self.adapter._remove_room_worker('a')
        self.assertTrue(one.cancelled()); self.assertFalse(two.done())
        two.cancel(); await asyncio.gather(two,return_exceptions=True)

    async def test_parallel_speech_preparation_stays_in_each_room(self):
        a,b = self.worker('a'),self.worker('b')
        self.adapter._room_workers.update(a=a,b=b)
        async def process(dispatcher,event,key):
            await asyncio.sleep(0)
            dispatcher.prepare_tts_text(event.text)
            await asyncio.sleep(0)
        events=[SimpleNamespace(source=SimpleNamespace(chat_id=name+':'+worker._session_epoch+':call:user'),text=word) for name,worker,word in [('a',a,'apple'),('b',b,'banana')]]
        with patch.object(mod.LiveKitAdapter,'_process_message_background',process):
            await asyncio.gather(*(self.adapter._process_message_background(e,str(n)) for n,e in enumerate(events)))
        self.assertEqual(a._prepared_tts_text,'apple'); self.assertEqual(b._prepared_tts_text,'banana')
        self.assertFalse(self.adapter._room_turn_tasks)

    async def test_scoped_tool_reset_preserves_other_session(self):
        a = self.worker('a'); self.adapter._room_workers['a']=a
        one=asyncio.get_running_loop().create_future(); two=asyncio.get_running_loop().create_future()
        a._pending_tool_calls.update(one=one,two=two)
        a._pending_tool_sessions.update(one='session-a',two='session-b')
        self.assertEqual(a.cancel_pending_tool_calls_for_session_reset('session-a'),1)
        self.assertIsInstance(one.exception(), RuntimeError); self.assertFalse(two.done())
        self.assertIn('two',a._pending_tool_calls)
        two.cancel()

    async def test_api_snapshot_discovers_all_human_occupied_hermes_rooms(self):
        client=SimpleNamespace(room=SimpleNamespace(list_rooms=AsyncMock(return_value=SimpleNamespace(rooms=[SimpleNamespace(name=n,metadata='{"agent_runtime":"hermes"}') for n in ['a','b','empty']])),list_participants=AsyncMock(side_effect=[SimpleNamespace(participants=[SimpleNamespace(identity='human-a',metadata='{}')]),SimpleNamespace(participants=[SimpleNamespace(identity='human-b',metadata='{}')]),SimpleNamespace(participants=[SimpleNamespace(identity='agent-appearance-empty',metadata='{"type":"agent"}')])])),aclose=AsyncMock())
        with patch.object(mod,'LiveKitAPI',return_value=client):
            self.assertEqual(await self.adapter._occupied_room_names(),{'a','b'})
        client.aclose.assert_awaited_once()
