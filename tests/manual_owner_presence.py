"""Opt-in LiveKit integration check; creates and removes one synthetic room.

Run with the Hermes interpreter and its server environment loaded. No real
meeting is modified, no microphone is opened, and no conversation is stored.
"""
import asyncio
import json
import os
import uuid
from unittest.mock import AsyncMock, patch

from livekit import api, rtc
from test_adapter import livekit_adapter as mod
from gateway.config import PlatformConfig


async def eventually(predicate):
    async def wait():
        while not predicate():
            await asyncio.sleep(0.05)
    await asyncio.wait_for(wait(), 10)


async def main():
    name = 'mira-behavior-check-' + uuid.uuid4().hex[:12]
    account = str(uuid.uuid4())
    url, key, secret = (os.environ[k] for k in ('LIVEKIT_URL', 'LIVEKIT_API_KEY', 'LIVEKIT_API_SECRET'))
    service = api.LiveKitAPI(url=url, api_key=key, api_secret=secret)
    supervisor = mod.MultiRoomLiveKitAdapter(PlatformConfig(enabled=True, extra={
        'url': url, 'api_key': key, 'api_secret': secret, 'room': name, 'agent_name': 'Validation',
    }))
    supervisor._running = True
    clients = []
    results = {}
    original_factory = supervisor._make_room_worker
    def factory(room_name):
        worker = original_factory(room_name)
        worker._refresh_room_agent_profile = AsyncMock(return_value=True)
        worker._record_agent_transcript = AsyncMock()
        return worker
    supervisor._make_room_worker = factory
    original_discovery = supervisor._occupied_room_names
    async def discover_only_validation_room():
        return (await original_discovery()) & {name}
    supervisor._occupied_room_names = discover_only_validation_room

    async def join(identity, owner):
        client = rtc.Room()
        token = api.AccessToken(key, secret).with_identity(identity).with_metadata(json.dumps({
            'type': 'user', 'mira_account_id': account if owner else str(uuid.uuid4()),
            'can_bring_agent': 'true',
        })).with_grants(api.VideoGrants(room_join=True, room=name, can_publish=False,
                                       can_publish_data=True, can_update_own_metadata=False)).to_jwt()
        clients.append(client)
        await client.connect(url, token)
        return client

    try:
        # No agent_runtime marker: the production gateway cannot discover this
        # room. Only this test supervisor treats it as its configured home.
        await service.room.create_room(api.CreateRoomRequest(name=name, empty_timeout=30, metadata=json.dumps({
            'agent_id': -1, 'agent_owner_account_id': account,
            'agent_video_owner_identity': 'validation-owner-first',
        })))
        guest = await join('validation-guest', False)
        await supervisor._reconcile_room_workers()
        assert not supervisor._room_workers
        results['guest_alone_does_not_join'] = True

        owner = await join('validation-owner-first', True)
        await supervisor._reconcile_room_workers()
        worker = supervisor._room_workers[name]
        await eventually(lambda: len(guest.remote_participants) == 2)
        results['owner_arrival_joins_agent'] = worker._room.isconnected()

        received = []
        guest.on('data_received', lambda p: received.append(p.data.decode()) if p.topic == 'hermes-chat' else None)
        await worker.send(name, 'Compacting conversation history now.')
        with patch.object(worker, '_decode_audio_to_pcm', side_effect=AssertionError('Unsafe audio decoded')):
            await worker.play_tts(name, 'unused.wav', caption='Context window is full.')
        await worker.send(name, 'The museum opens at nine.')
        await eventually(lambda: bool(received))
        assert received == ['The museum opens at nine.']
        results['only_conversational_text_reaches_room'] = True
        for state in ('listening', 'thinking', 'speaking'):
            await worker._set_agent_state(state, force=True)
            await eventually(lambda: any(p.attributes.get('lk.agent.state') == state for p in guest.remote_participants.values()))
        results['all_three_states_reach_remote_client'] = True

        turn = asyncio.create_task(asyncio.sleep(60))
        supervisor._room_turn_tasks[turn] = worker
        await owner.disconnect()
        await eventually(lambda: name not in supervisor._room_workers)
        await eventually(lambda: not guest.remote_participants)
        assert turn.cancelled()
        await supervisor._reconcile_room_workers()
        assert not supervisor._room_workers
        results['owner_departure_cancels_turn_and_leaves_guest'] = True
        results['guest_cannot_trigger_agent_rejoin'] = True

        await join('validation-owner-second', True)
        await supervisor._reconcile_room_workers()
        assert supervisor._room_workers[name] is not worker
        results['owner_return_creates_new_occupancy'] = True
        await guest.disconnect()
        await supervisor._reconcile_room_workers()
        assert name in supervisor._room_workers
        results['guest_departure_preserves_owner_agent'] = True
        print(json.dumps(results))
    finally:
        await supervisor.disconnect()
        for client in clients:
            await client.disconnect()
        await service.room.delete_room(api.DeleteRoomRequest(room=name))
        await service.aclose()


if __name__ == '__main__':
    asyncio.run(main())
