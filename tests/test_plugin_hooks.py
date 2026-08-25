import importlib.util
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

PLUGIN_ROOT = Path(__file__).resolve().parents[1]

spec = importlib.util.spec_from_file_location(
    "hermes_livekit_plugin_test",
    PLUGIN_ROOT / "__init__.py",
    submodule_search_locations=[str(PLUGIN_ROOT)],
)
plugin = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = plugin
spec.loader.exec_module(plugin)


class PluginHookTests(unittest.TestCase):
    def setUp(self):
        self.adapter = Mock()
        self.adapter._room_name = "test-room"
        plugin.LIVE_ADAPTERS.add(self.adapter)

    def tearDown(self):
        plugin.LIVE_ADAPTERS.discard(self.adapter)

    def test_gateway_hook_binds_livekit_turn_to_persisted_session(self):
        source = SimpleNamespace(
            platform=SimpleNamespace(value="livekit"),
            chat_id="test-room",
        )
        event = SimpleNamespace(source=source)
        store = SimpleNamespace(
            get_or_create_session=Mock(
                return_value=SimpleNamespace(session_id="session-123")
            )
        )

        plugin._on_pre_gateway_dispatch_hook(event=event, session_store=store)

        store.get_or_create_session.assert_called_once_with(source)
        self.adapter.bind_tool_acknowledgement_session.assert_called_once_with(
            "session-123"
        )

    def test_non_livekit_turn_is_ignored(self):
        event = SimpleNamespace(
            source=SimpleNamespace(
                platform=SimpleNamespace(value="discord"), chat_id="test-room"
            )
        )
        store = SimpleNamespace(get_or_create_session=Mock())

        plugin._on_pre_gateway_dispatch_hook(event=event, session_store=store)

        store.get_or_create_session.assert_not_called()
        self.adapter.bind_tool_acknowledgement_session.assert_not_called()

    def test_tool_hook_forwards_session_and_turn_identity(self):
        plugin._on_pre_tool_call_hook(
            session_id="session-123",
            turn_id="turn-1",
            tool_call_id="tool-1",
            tool_name="web_search",
        )

        self.adapter.schedule_tool_acknowledgement.assert_called_once_with(
            session_id="session-123",
            turn_id="turn-1",
            tool_call_id="tool-1",
        )

    def test_tourism_guidance_is_always_in_livekit_platform_hint(self):
        self.assertIn("Aotearoa New Zealand", plugin._LIVEKIT_PLATFORM_HINT)
        self.assertIn("find_local_recommendations", plugin._LIVEKIT_PLATFORM_HINT)
        self.assertIn("get_current_trip_context", plugin._LIVEKIT_PLATFORM_HINT)
        self.assertIn("manage_trip_itinerary", plugin._LIVEKIT_PLATFORM_HINT)
        self.assertIn("explicitly approves", plugin._LIVEKIT_PLATFORM_HINT)
        self.assertIn(
            "Never end a turn with a holding sentence", plugin._LIVEKIT_PLATFORM_HINT
        )
        self.assertIn("normal task and skill tools", plugin._LIVEKIT_PLATFORM_HINT)

    def test_register_exposes_bundled_skill_read_only(self):
        context = Mock()

        plugin.register(context)

        context.register_skill.assert_called_once_with(
            "mira-new-zealand-tourism", plugin._TOURISM_SKILL_PATH
        )
        context.register_platform.assert_called_once()
        context.register_tool.assert_called_once()
        tool_call = context.register_tool.call_args.kwargs
        self.assertEqual(tool_call["name"], "manage_trip_itinerary")
        self.assertEqual(tool_call["toolset"], "hermes-livekit")
        self.assertTrue(tool_call["is_async"])
        hook_names = [call.args[0] for call in context.register_hook.call_args_list]
        self.assertIn("pre_llm_call", hook_names)
        self.assertIn("post_llm_call", hook_names)

    def test_manifest_declares_cross_platform_itinerary_tool(self):
        manifest = (PLUGIN_ROOT / "plugin.yaml").read_text(encoding="utf-8")

        self.assertIn("provides_tools:", manifest)
        self.assertIn("  - manage_trip_itinerary", manifest)
        self.assertTrue((PLUGIN_ROOT / "tools.py").is_file())

    def test_pre_llm_hook_injects_linked_cross_channel_workspace(self):
        workspace = {
            "linked": True,
            "draft": {"revision": 2, "title": "Auckland"},
            "itinerary": None,
            "conversation": [{"role": "user", "content": "Keep it relaxed"}],
        }
        with patch.object(
            plugin, "_dispatch_account_workspace", return_value=workspace
        ) as dispatch:
            result = plugin._on_pre_llm_account_context(
                session_id="discord-session", user_message="Can we continue my plan?"
            )

        dispatch.assert_called_once_with({"action": "load"}, session_id="discord-session")
        self.assertIn('"revision":2', result["context"])
        self.assertIn("NOT saved/confirmed", result["context"])

    def test_post_llm_hook_records_full_turn_for_linked_account(self):
        with patch.object(plugin, "_dispatch_account_workspace") as dispatch:
            plugin._on_post_llm_account_turn(
                session_id="livekit-session",
                turn_id="turn-7",
                user_message="Move lunch later",
                assistant_response="I moved lunch to 1 pm.",
            )

        dispatch.assert_called_once_with(
            {
                "action": "record_turn",
                "turn_id": "turn-7",
                "user_message": "Move lunch later",
                "assistant_message": "I moved lunch to 1 pm.",
            },
            session_id="livekit-session",
        )

    def test_pre_llm_hook_executes_explicit_account_link_before_model(self):
        unlinked = {"linked": False}
        linked = {
            "linked": True,
            "draft": None,
            "itinerary": None,
            "conversation": [],
        }
        with patch.object(
            plugin,
            "_dispatch_account_workspace",
            side_effect=[unlinked, linked],
        ) as dispatch:
            result = plugin._on_pre_llm_account_context(
                session_id="discord-session",
                user_message="link my account ABCDEFGHIJKLMNOPQRSTUVWX",
            )

        self.assertEqual(dispatch.call_count, 2)
        dispatch.assert_any_call({"action": "load"}, session_id="discord-session")
        dispatch.assert_any_call(
            {"action": "link", "link_code": "ABCDEFGHIJKLMNOPQRSTUVWX"},
            session_id="discord-session",
        )
        self.assertIn("registered-account planning context", result["context"])


if __name__ == "__main__":
    unittest.main()
