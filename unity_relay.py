"""Private, authenticated output transport to the Unity-owned agent participant.

The processor subscribes to humans with a hidden, non-publishing LiveKit token.
Only Unity publishes agent media and room events. No UI participant filtering is
needed, and a missing renderer never falls back to a second visible agent.
"""
import asyncio
import base64
import hashlib
import hmac
import json
import time
import uuid

TOPIC = 'mira-unity-output'


def relay_key(secret, room, identity):
    return hmac.new(secret.encode(), f'mira-unity-relay-v1:{room}:{identity}'.encode(), hashlib.sha256).hexdigest()


class UnityRelay:
    def __init__(self, participant, secret, room, identity):
        self.participant = participant
        self.identity = identity
        self.key = bytes.fromhex(relay_key(secret, room, identity))
        self.epoch = uuid.uuid4().hex
        self.sequence = 0
        self.generation = 0

    async def emit(self, kind, **payload):
        self.sequence += 1
        body = json.dumps(dict(kind=kind, epoch=self.epoch, sequence=self.sequence,
                              issued_at=time.time(), generation=self.generation, **payload), separators=(',', ':')).encode()
        envelope = json.dumps({'body': base64.b64encode(body).decode(),
                               'mac': hmac.new(self.key, body, hashlib.sha256).hexdigest()}, separators=(',', ':')).encode()
        await self.participant.publish_data(envelope, reliable=True, topic=TOPIC,
                                            destination_identities=[self.identity])

    async def capture_frame(self, frame):
        await self.emit('audio', pcm=base64.b64encode(bytes(frame.data)).decode())
        # Data packets have no AudioSource capture backpressure. Pace PCM so
        # long answers cannot fill the receiver queue or delay interruption.
        await asyncio.sleep(frame.samples_per_channel / frame.sample_rate)

    def clear_queue(self):
        self.generation += 1
        asyncio.create_task(self.emit('flush'))


class UnityLocalParticipant:
    def __init__(self, actual, relay):
        self.actual = actual
        self.relay = relay
        self.identity = relay.identity

    async def publish_data(self, data, *, reliable=True, topic='', destination_identities=None):
        await self.relay.emit('data', data=base64.b64encode(data).decode(), topic=topic,
                              destinations=list(destination_identities or []))

    async def set_name(self, name):
        await self.relay.emit('name', name=name)

    async def set_attributes(self, attributes):
        await self.relay.emit('attributes', attributes=attributes)

    async def set_metadata(self, metadata):
        # Unity's server-issued agent/appearance metadata remains authoritative.
        pass

    async def publish_transcription(self, transcription):
        # The normal agent:*transcript JSON event is relayed by publish_data.
        # Avoid publishing a native transcript under the hidden service SID.
        pass

    def __getattr__(self, name):
        return getattr(self.actual, name)


class UnityRelayRoom:
    def __init__(self, room, relay):
        self.actual = room
        self.local_participant = UnityLocalParticipant(room.local_participant, relay)

    def __getattr__(self, name):
        return getattr(self.actual, name)
