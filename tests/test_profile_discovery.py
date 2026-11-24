"""Exercise actual Hermes discovery, not a mock registration context."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class ProfileDiscoveryTests(unittest.TestCase):
    def test_routed_profiles_discover_tools_and_keep_transport_bindings(self):
        plugin = Path(__file__).resolve().parents[1]
        script = r'''
import importlib, json, os, shutil, sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from hermes_constants import set_hermes_home_override, reset_hermes_home_override
from agent.secret_scope import set_multiplex_active, set_secret_scope, reset_secret_scope, get_secret
from hermes_cli.plugins import discover_plugins
from tools.registry import registry
from toolsets import resolve_toolset
import context_tools, service_access

root = Path(os.environ['HERMES_HOME'])
source = Path(sys.argv[1])
with patch.object(service_access, 'get_scoped_secret', side_effect=lambda key, default=None: {'MIRA_AUTH_KEY_ID':'host-service'}.get(key, default)):
    access = service_access.MeetingServiceAccess()
worker = SimpleNamespace(_room_name='owner-room', _meeting_service_access=access,
    _participant_connection_metadata=lambda identity: {'mira_room_owner':'true' if identity=='owner' else 'false'})
event = SimpleNamespace(source=SimpleNamespace(user_id='owner',chat_id='owner-room'),
    metadata={'livekit_spoken_transcript':True},message_id='spoken-event',text='Yes, add the tagged place at 5 PM')
room_token = context_tools.active_room.set(worker)
turn_token = context_tools.active_turn.set(event)
set_multiplex_active(True)
try:
    modules=[]
    for name in ('first', 'second'):
        home=root/'profiles'/name
        (home/'plugins').mkdir(parents=True)
        shutil.copytree(source,home/'plugins/hermes-livekit',ignore=shutil.ignore_patterns('.git','__pycache__','tests'))
        (home/'config.yaml').write_text('plugins:\n  enabled: [hermes-livekit]\nplatform_toolsets:\n  livekit: [hermes-livekit]\n')
        home_token=set_hermes_home_override(home)
        secret_token=set_secret_scope({'OPENAI_API_KEY':name})
        try:
            discover_plugins()
            assert 'add_confirmed_place_to_itinerary' in resolve_toolset('hermes-livekit')
            entry=registry.get_entry('add_confirmed_place_to_itinerary')
            assert entry is not None
            module=importlib.import_module(entry.handler.__module__)
            prefix=module.__package__
            modules.append(prefix)
            profile_context=importlib.import_module(prefix+'.context_tools')
            profile_service=importlib.import_module(prefix+'.service_access')
            assert profile_context.active_room is context_tools.active_room
            assert profile_context.active_turn is context_tools.active_turn
            assert profile_service._settings is service_access._settings
            state={'session_id':'test-session','itinerary':{'revision':0}}
            def fetch(room):
                assert room=='owner-room'
                assert profile_service.service_env('MIRA_AUTH_KEY_ID')=='host-service'
                assert get_secret('OPENAI_API_KEY')==name
                return state
            def send(command):
                assert command['source']['user_id']=='owner'
                assert command['confirmation_event_id']=='spoken-event'
                assert command['confirmation_text']==event.text
                assert profile_service.service_env('MIRA_AUTH_KEY_ID')=='host-service'
                assert get_secret('OPENAI_API_KEY')==name
                return {'saved':True}
            with patch(prefix+'.room_map.fetch_room_map',side_effect=fetch), patch.object(module,'_send_gateway_command',side_effect=send) as write:
                read=json.loads(registry.dispatch('get_confirmed_itinerary',{}))
                assert read['itinerary']['revision']==0
                result=json.loads(registry.dispatch('add_confirmed_place_to_itinerary',{'tag_id':'tag','expected_revision':0,'starts_at':'2026-11-24T17:00:00+13:00','duration_minutes':30}))
                assert result['saved'] and write.call_count==1
                event.source.user_id='companion'
                denied=json.loads(registry.dispatch('add_confirmed_place_to_itinerary',{}))
                assert 'verbal confirmation' in denied['error'] and write.call_count==1
                event.source.user_id='owner'
            assert profile_service._settings.get() is None
        finally:
            reset_secret_scope(secret_token)
            reset_hermes_home_override(home_token)
    assert modules[0]!=modules[1], 'Test must cover separately imported profile plugins'
finally:
    set_multiplex_active(False)
    context_tools.active_turn.reset(turn_token)
    context_tools.active_room.reset(room_token)
print('Actual profile discovery and room-bound read/write dispatch passed')
'''
        with tempfile.TemporaryDirectory(prefix="mira-profile-discovery-") as home:
            result = subprocess.run([sys.executable, "-c", script, str(plugin)],
                env={**os.environ, "HERMES_HOME": home, "HERMES_PROFILE": "default",
                     "PYTHONPATH": str(plugin) + os.pathsep + os.environ.get("PYTHONPATH", "")},
                capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
