import importlib.util
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

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


class PluginHookTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.adapter = Mock()
        self.adapter._room_name = "test-room"
        plugin.LIVE_ADAPTERS.add(self.adapter)

    def tearDown(self):
        plugin.LIVE_ADAPTERS.discard(self.adapter)

    def test_platform_hint_describes_transport_without_injecting_domain_policy(self):
        self.assertIn("Hermes conversation", plugin._LIVEKIT_PLATFORM_HINT)
        self.assertIn("Decide whether and when", plugin._LIVEKIT_PLATFORM_HINT)
        self.assertIn("MCP servers", plugin._LIVEKIT_PLATFORM_HINT)
        self.assertIn("/no_think", plugin._LIVEKIT_PLATFORM_HINT)
        self.assertNotIn("Aotearoa New Zealand", plugin._LIVEKIT_PLATFORM_HINT)
        self.assertNotIn("Never end a turn", plugin._LIVEKIT_PLATFORM_HINT)

    def test_register_exposes_bundled_skill_read_only(self):
        context = Mock()

        plugin.register(context)

        context.register_skill.assert_called_once_with(
            "mira-new-zealand-tourism", plugin._TOURISM_SKILL_PATH
        )
        context.register_platform.assert_called_once()
        self.assertEqual(context.register_tool.call_count, 3)
        tool_calls = [call.kwargs for call in context.register_tool.call_args_list]
        self.assertEqual(
            [call["name"] for call in tool_calls],
            [
                "find_local_recommendations",
                "get_current_trip_context",
                "manage_trip_itinerary",
            ],
        )
        self.assertTrue(all(call["toolset"] == "hermes-livekit" for call in tool_calls))
        self.assertTrue(all(call["is_async"] for call in tool_calls))
        hook_names = [call.args[0] for call in context.register_hook.call_args_list]
        self.assertEqual(
            hook_names, ["post_api_request", "on_session_finalize"]
        )
        context.register_middleware.assert_called_once_with(
            "llm_request", plugin._qwen_realtime_request_middleware
        )

    def test_qwen_livekit_request_middleware_sets_native_nonthinking_flag(self):
        request = {
            "messages": [{"role": "user", "content": "recommend lunch"}],
            "extra_body": {"preserved": True},
        }

        result = plugin._qwen_realtime_request_middleware(
            platform="livekit",
            model="OptimizeLLM/Qwen3-VL-30B-A3B-Thinking-NVFP4",
            request=request,
        )

        self.assertIsNotNone(result)
        self.assertEqual(
            result["request"]["extra_body"],
            {"preserved": True, "enable_thinking": False},
        )
        self.assertEqual(
            result["request"]["messages"][-1]["content"],
            "recommend lunch\n\n/no_think",
        )
        self.assertEqual(request["messages"][-1]["content"], "recommend lunch")
        self.assertEqual(request["extra_body"], {"preserved": True})

    def test_qwen_request_middleware_is_livekit_and_model_scoped(self):
        request = {"messages": []}
        self.assertIsNone(
            plugin._qwen_realtime_request_middleware(
                platform="discord", model="Qwen3", request=request
            )
        )
        self.assertIsNone(
            plugin._qwen_realtime_request_middleware(
                platform="livekit", model="gpt-5", request=request
            )
        )

    def test_plugin_defines_no_automatic_model_turn_orchestration(self):
        self.assertFalse(hasattr(plugin, "_on_pre_llm_account_context"))
        self.assertFalse(hasattr(plugin, "_on_pre_llm_live_trip_context"))
        self.assertFalse(hasattr(plugin, "_on_post_llm_account_turn"))

    def test_qwen_text_tool_decision_is_normalized_before_dispatch(self):
        response = SimpleNamespace(
            content=(
                '<tool_call>{"name":"find_local_recommendations",'
                '"arguments":{"query":"lunch","location":"Auckland"}}'
                "</tool_call>"
            ),
            tool_calls=None,
            finish_reason="stop",
        )

        plugin._on_post_api_request_hook(
            platform="livekit",
            model="OptimizeLLM/Qwen3-VL-30B-A3B-Thinking-NVFP4",
            assistant_message=response,
        )

        self.assertIsNone(response.content)
        self.assertEqual(response.finish_reason, "tool_calls")
        self.assertEqual(len(response.tool_calls), 1)
        self.assertEqual(
            response.tool_calls[0].function.name, "find_local_recommendations"
        )
        self.assertEqual(
            json.loads(response.tool_calls[0].function.arguments),
            {"query": "lunch", "location": "Auckland"},
        )

    def test_qwen_tool_normalizer_is_platform_and_format_scoped(self):
        malformed = SimpleNamespace(
            content="<tool_call>not-json</tool_call>",
            tool_calls=None,
            finish_reason="stop",
        )
        plugin._on_post_api_request_hook(
            platform="livekit",
            model="Qwen3",
            assistant_message=malformed,
        )
        self.assertIsNone(malformed.tool_calls)

        cli_response = SimpleNamespace(
            content=(
                '<tool_call>{"name":"find_local_recommendations",'
                '"arguments":{"query":"lunch"}}</tool_call>'
            ),
            tool_calls=None,
            finish_reason="stop",
        )
        plugin._on_post_api_request_hook(
            platform="cli", model="Qwen3", assistant_message=cli_response
        )
        self.assertIsNone(cli_response.tool_calls)

    async def test_itinerary_backend_runs_only_through_selected_tool(self):
        handler = AsyncMock(return_value='{"linked":true}')
        self.adapter._tool_owners = {"manage_trip_itinerary": "worker-1"}
        self.adapter._build_tool_handler.return_value = handler

        result = await plugin._route_account_planning_tool(
            {"action": "load"}, session_id="discord-session"
        )

        self.adapter._build_tool_handler.assert_called_once_with(
            "worker-1", "manage_trip_itinerary"
        )
        handler.assert_awaited_once_with(
            {"action": "load"}, session_id="discord-session"
        )
        self.assertEqual(result, '{"linked":true}')

    async def test_context_backend_runs_only_through_selected_tool(self):
        handler = AsyncMock(return_value='{"contexts":[]}')
        self.adapter._tool_owners = {"get_current_trip_context": "worker-1"}
        self.adapter._build_tool_handler.return_value = handler

        result = await plugin._route_current_trip_context_tool(
            {"transcript_limit": 8}, session_id="discord-session"
        )

        self.adapter._build_tool_handler.assert_called_once_with(
            "worker-1", "get_current_trip_context"
        )
        handler.assert_awaited_once_with(
            {"transcript_limit": 8}, session_id="discord-session"
        )
        self.assertEqual(result, '{"contexts":[]}')

    def test_manifest_declares_cross_platform_mira_tools(self):
        manifest = (PLUGIN_ROOT / "plugin.yaml").read_text(encoding="utf-8")

        self.assertIn("provides_tools:", manifest)
        self.assertIn("  - find_local_recommendations", manifest)
        self.assertIn("  - get_current_trip_context", manifest)
        self.assertIn("  - manage_trip_itinerary", manifest)
        self.assertIn("  - post_api_request", manifest)
        self.assertTrue((PLUGIN_ROOT / "tools.py").is_file())

    @patch.dict(
        "os.environ",
        {
            "LIVEKIT_URL": "wss://livekit.example.test",
            "LIVEKIT_API_KEY": "key",
            "LIVEKIT_API_SECRET": "secret",
            "LIVEKIT_AGENT_NAME": "Environment fallback",
        },
        clear=False,
    )
    def test_env_enablement_does_not_override_portal_agent_name(self):
        seed = plugin._env_enablement()

        self.assertIsNotNone(seed)
        self.assertNotIn("agent_name", seed)

if __name__ == "__main__":
    unittest.main()
