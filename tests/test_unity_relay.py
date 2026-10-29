import asyncio, base64, hashlib, hmac, json, sys
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch
from test_adapter import livekit_adapter as mod, make_adapter, PLUGIN_ROOT
sys.path.insert(0,str(PLUGIN_ROOT))
from unity_relay import UnityRelay, relay_key


class UnityRelayTests(IsolatedAsyncioTestCase):
    async def test_output_is_signed_and_only_addressed_to_unity(self):
        participant=SimpleNamespace(publish_data=AsyncMock())
        relay=UnityRelay(participant,'secret','room','agent-appearance-one')
        await relay.emit('data',data='aGVsbG8=',topic='hermes-chat')
        args=participant.publish_data.call_args
        self.assertEqual(args.kwargs['destination_identities'],['agent-appearance-one'])
        self.assertEqual(args.kwargs['topic'],'mira-unity-output')
        envelope=json.loads(args.args[0]);body=base64.b64decode(envelope['body'])
        key=bytes.fromhex(relay_key('secret','room','agent-appearance-one'))
        self.assertEqual(envelope['mac'],hmac.new(key,body,hashlib.sha256).hexdigest())
        self.assertNotEqual(relay_key('secret','room','agent-appearance-one'),relay_key('secret','other','agent-appearance-one'))
        self.assertEqual(json.loads(body)['kind'],'data')

    async def test_unity_mode_joins_hidden_without_publishing_voice(self):
        adapter=make_adapter({'unity_renderer_identity':'agent-appearance-one'})
        participant=SimpleNamespace(publish_data=AsyncMock(),publish_track=AsyncMock())
        room=SimpleNamespace(on=lambda *a:None,register_byte_stream_handler=lambda *a:None,
            connect=AsyncMock(),disconnect=AsyncMock(),local_participant=participant)
        adapter._set_agent_state=AsyncMock()
        adapter._refresh_room_agent_profile=AsyncMock(return_value=True)
        with patch.object(mod.rtc,'Room',return_value=room):
            self.assertTrue(await adapter._join_room())
        adapter._silence_task.cancel();await asyncio.gather(adapter._silence_task,return_exceptions=True)
        token=room.connect.call_args.args[1];raw=token.split('.')[1];claims=json.loads(base64.urlsafe_b64decode(raw+'='*(-len(raw)%4)))
        self.assertTrue(claims['video']['hidden']);self.assertFalse(claims['video']['canPublish'])
        self.assertTrue(claims['video']['canPublishData'])
        self.assertTrue(claims['sub'].startswith('system-unity-'))
        self.assertEqual(adapter._room.local_participant.identity,'agent-appearance-one')
        participant.publish_track.assert_not_awaited()
        mod.LIVE_ADAPTERS.discard(adapter)

    async def test_unity_output_cannot_trigger_another_agent_turn(self):
        adapter=make_adapter({'unity_renderer_identity':'agent-appearance-one'})
        adapter.handle_message=AsyncMock()
        adapter._on_data_received(SimpleNamespace(participant=SimpleNamespace(identity='agent-appearance-one'),data=b'Sarah do it again',topic='hermes-chat'))
        adapter._on_track_subscribed(SimpleNamespace(kind=mod.rtc.TrackKind.KIND_AUDIO),SimpleNamespace(),SimpleNamespace(identity='agent-appearance-one'))
        self.assertFalse(adapter._audio_buffers);adapter.handle_message.assert_not_awaited()
        mod.LIVE_ADAPTERS.discard(adapter)

    async def test_appearance_waits_for_unity_and_processor_is_not_human(self):
        adapter=mod.MultiRoomLiveKitAdapter(make_adapter().config)
        room=SimpleNamespace(name='room',metadata=json.dumps({'agent_runtime':'hermes','agent_transport':'unity','unity_renderer_identity':'agent-appearance-one'}))
        humans=SimpleNamespace(participants=[SimpleNamespace(identity='alice')])
        client=SimpleNamespace(room=SimpleNamespace(list_rooms=AsyncMock(return_value=SimpleNamespace(rooms=[room])),list_participants=AsyncMock(return_value=humans)),aclose=AsyncMock())
        with patch.object(mod,'LiveKitAPI',return_value=client):
            self.assertEqual(await adapter._occupied_room_names(),set())
            humans.participants.append(SimpleNamespace(identity='agent-appearance-one'))
            self.assertEqual(await adapter._occupied_room_names(),{'room'})
            self.assertEqual(adapter._room_transports['room'],'agent-appearance-one')
        self.assertFalse(mod._is_human_participant(SimpleNamespace(identity='system-unity-test')))

    async def test_transport_change_replaces_direct_worker(self):
        adapter=mod.MultiRoomLiveKitAdapter(make_adapter().config)
        old=adapter._make_room_worker('room');old._room=SimpleNamespace(isconnected=lambda:True);old.disconnect=AsyncMock()
        adapter._room_workers['room']=old
        adapter._room_transports['room']='agent-appearance-one'
        adapter._occupied_room_names=AsyncMock(return_value={'room'})
        new=adapter._make_room_worker('room');new.connect=AsyncMock(return_value=True)
        adapter._make_room_worker=lambda name:new
        await adapter._reconcile_room_workers()
        old.disconnect.assert_awaited_once();new.connect.assert_awaited_once()
        self.assertIs(adapter._room_workers['room'],new)
        for worker in (old,new,adapter):mod.LIVE_ADAPTERS.discard(worker)
