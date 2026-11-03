import asyncio
import json
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import Mock, patch

from test_adapter import livekit_adapter as mod, make_adapter, authorize_unflagged_participants
import context_tools


class ContextToolsTests(IsolatedAsyncioTestCase):
    def setUp(self):
        ctx = Mock()
        context_tools.register_tools(ctx)
        self.handlers = {call.kwargs["name"]: call.kwargs["handler"] for call in ctx.register_tool.call_args_list}

    async def test_unbound_tools_fail_closed(self):
        for handler in self.handlers.values():
            self.assertIn("error", json.loads(await handler({"room_name": "someone-elses-room"})))

    async def test_real_hermes_registry_dispatch_preserves_context(self):
        from tools.registry import ToolRegistry
        registry = ToolRegistry()
        context_tools.register_tools(SimpleNamespace(register_tool=registry.register))
        token = context_tools.active_room.set(SimpleNamespace(_room_name="registry-room"))
        try:
            with patch("room_map.fetch_room_map", return_value={"latest_location": {"longitude": 174.77}}):
                result = await asyncio.to_thread(registry.dispatch, "get_traveller_location", {})
            self.assertEqual(json.loads(result)["room_name"], "registry-room")
            self.assertEqual(json.loads(result)["latest_location"]["longitude"], 174.77)
        finally:
            context_tools.active_room.reset(token)

    async def test_runtime_dispatch_propagates_room_to_parallel_tools_and_cleans_up(self):
        base = make_adapter()
        supervisor = mod.MultiRoomLiveKitAdapter(base.config)
        self.addCleanup(mod.LIVE_ADAPTERS.discard, base)
        for name in ("room-a", "room-b"):
            worker = supervisor._make_room_worker(name)
            self.addCleanup(mod.LIVE_ADAPTERS.discard, worker)
            supervisor._room_workers[name] = worker
        observed = {}

        async def process(dispatcher, event, key):
            await asyncio.sleep(0)
            # Models cannot choose another room, even with unexpected arguments.
            observed[key] = json.loads(await self.handlers["get_traveller_location"]({"room_name": "wrong"}))

        def fetch(room):
            return {"latest_location": {"participant_name": room, "local_time": "fresh"}}

        with patch.object(mod.BasePlatformAdapter, "_process_message_background", process), patch("room_map.fetch_room_map", side_effect=fetch):
            await asyncio.gather(*(
                supervisor._process_message_background(SimpleNamespace(source=SimpleNamespace(chat_id=name)), name)
                for name in supervisor._room_workers
            ))
        for name in supervisor._room_workers:
            self.assertEqual(observed[name]["latest_location"]["participant_name"], name)
        self.assertIsNone(context_tools.active_room.get())

    async def test_reads_refresh_and_do_not_reuse_revoked_gps(self):
        worker = SimpleNamespace(_room_name="current-room", _fetch_room_transcript=Mock(return_value=[
            {"speaker": "Phone", "text": "Museum first"}, {"speaker": "Friend", "text": "Then lunch"}]))
        token = context_tools.active_room.set(worker)
        try:
            states = [
                {"latest_location": {"longitude": 174.76, "is_stale": False}},
                {"latest_location": {"longitude": 174.77, "is_stale": True}},
                {"latest_location": None},
                {"itinerary": {"title": "Saved plan", "revision": 3}},
            ]
            with patch("room_map.fetch_room_map", side_effect=states) as fetch:
                first = json.loads(await self.handlers["get_traveller_location"]())
                second = json.loads(await self.handlers["get_traveller_location"]())
                missing = json.loads(await self.handlers["get_traveller_location"]())
                plan = json.loads(await self.handlers["get_confirmed_itinerary"]())
            self.assertNotEqual(first["latest_location"], second["latest_location"])
            self.assertTrue(second["latest_location"]["is_stale"])
            self.assertFalse(missing["found"])
            self.assertEqual(plan["itinerary"]["revision"], 3)
            self.assertEqual(fetch.call_count, 4)
            transcript = json.loads(await self.handlers["get_meeting_transcript"]({"limit": 1000}))
            self.assertEqual([e["speaker"] for e in transcript["entries"]], ["Phone", "Friend"])
            worker._fetch_room_transcript.assert_called_once_with(500)
        finally:
            context_tools.active_room.reset(token)

    async def test_location_and_time_are_fetched_before_model_even_with_custom_soul(self):
        adapter = authorize_unflagged_participants(make_adapter())
        self.addCleanup(mod.LIVE_ADAPTERS.discard, adapter)
        adapter._load_agent_profile = lambda _: {"is_active": True, "display_name": "Custom", "system_prompt": "Speak like a pirate"}
        adapter._fetch_room_transcript = Mock(return_value=[])
        adapter._drain_place_tag_nudges = lambda: ""
        state = {"latest_location": {"participant_name": "Phone traveller", "longitude": 174.76,
                 "timezone": "Pacific/Auckland", "local_time": "2026-11-03T12:00:00+13:00", "is_stale": False},
                 "itinerary": {"title": "Pre-planned museum visit"}}
        with patch("room_map.fetch_room_map", return_value=state) as fetch:
            for question in ("Where am I?", "What time is it here?"):
                event = adapter._build_spoken_transcript_event(question, "phone")
                self.assertTrue(await adapter._prepare_invoked_event(event, force_invoke=True))
                for expected in ("Phone traveller", "Pacific/Auckland", "12:00:00", "Pre-planned museum visit", "independently of SOUL"):
                    self.assertIn(expected, event.channel_prompt)
            self.assertEqual(fetch.call_count, 2)
