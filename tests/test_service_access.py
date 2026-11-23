import asyncio
import json
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import Mock, patch

from agent.secret_scope import get_secret, set_secret_scope, reset_secret_scope
import context_tools
import itinerary_tools
import service_access
import transcript_store
from adapter import LiveKitAdapter


class ServiceAccessTests(IsolatedAsyncioTestCase):
    async def test_routed_read_and_write_use_host_service_settings_without_exposing_them(self):
        host = {"MIRA_DATABASE_URL": "sqlite://", "MIRA_AUTH_KEY_ID": "host-service",
                "OPENAI_API_KEY": "host-model-key"}
        with patch.object(service_access, "get_scoped_secret", side_effect=lambda key, default=None: host.get(key, default)):
            access = service_access.MeetingServiceAccess()
        worker = SimpleNamespace(_room_name="owner-room", _meeting_service_access=access)
        room_token = context_tools.active_room.set(worker)
        scope_token = set_secret_scope({"OPENAI_API_KEY": "profile-model-key", "MIRA_AUTH_KEY_ID": "profile-unrelated"})
        try:
            def fetch(room):
                self.assertEqual(room, "owner-room")
                self.assertEqual(service_access.service_env("MIRA_AUTH_KEY_ID"), "host-service")
                # The general secret scope stays the profile's, even inside an operation.
                self.assertEqual(get_secret("MIRA_AUTH_KEY_ID"), "profile-unrelated")
                self.assertEqual(service_access.service_env("OPENAI_API_KEY"), "profile-model-key")
                return {"itinerary": {"revision": 2}}

            registry = Mock()
            context_tools.register_tools(registry)
            handler = next(call.kwargs['handler'] for call in registry.register_tool.call_args_list
                           if call.kwargs['name'] == 'get_confirmed_itinerary')
            with patch('room_map.fetch_room_map', side_effect=fetch):
                result = json.loads(await handler({'room_name': 'other-room'}))
            self.assertEqual(result['itinerary']['revision'], 2)
            self.assertNotIn('host-service', json.dumps(result))
            with patch('room_map.fetch_room_map', side_effect=fetch):
                prompt = await asyncio.to_thread(LiveKitAdapter._shared_map_context, worker)
            self.assertIn('"revision": 2', prompt)
            self.assertNotIn('host-service', prompt)

            def send(command):
                self.assertEqual(service_access.service_env("MIRA_AUTH_KEY_ID"), "host-service")
                self.assertEqual(command, {"action": "confirm", "expected_revision": 2})
                return {"linked": True}

            with patch.object(itinerary_tools, '_send_gateway_command', side_effect=send):
                result = await asyncio.to_thread(itinerary_tools._post_gateway_command,
                                                {"action": "confirm", "expected_revision": 2})
            self.assertTrue(result['linked'])
            self.assertEqual(service_access.service_env("MIRA_AUTH_KEY_ID"), "profile-unrelated")
            self.assertEqual(get_secret("OPENAI_API_KEY"), "profile-model-key")
        finally:
            reset_secret_scope(scope_token)
            context_tools.active_room.reset(room_token)

    async def test_service_context_is_restored_after_failure_and_parallel_rooms_do_not_mix(self):
        workers = []
        for name in ('a', 'b'):
            with patch.object(service_access, 'get_scoped_secret', side_effect=lambda key, default=None, name=name: name if key == 'MIRA_AUTH_KEY_ID' else default):
                workers.append(service_access.MeetingServiceAccess())
        def read(access):
            return access.run(service_access.service_env, 'MIRA_AUTH_KEY_ID')
        self.assertEqual(await asyncio.gather(*(asyncio.to_thread(read, access) for access in workers)), ['a', 'b'])
        def fail():
            raise RuntimeError('operation failed')
        with self.assertRaises(RuntimeError):
            workers[0].run(fail)
        self.assertIsNone(service_access._settings.get())

    async def test_missing_profile_configuration_does_not_poison_host_engine_cache(self):
        transcript_store.reset_engine_for_tests()
        self.addCleanup(transcript_store.reset_engine_for_tests)
        with patch.object(transcript_store, '_getenv', return_value=''):
            self.assertIsNone(transcript_store._engine())
        with patch.object(transcript_store, '_getenv', return_value='sqlite://'):
            engine = transcript_store._engine()
            self.assertIsNotNone(engine)
            self.addCleanup(engine.dispose)
