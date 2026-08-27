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

    async def test_gui_tool_uses_cross_process_livekit_relay_without_adapter(self):
        plugin.LIVE_ADAPTERS.discard(self.adapter)
        relay = AsyncMock(return_value='{"linked":true}')

        with patch.object(plugin, "_route_remote_tool_via_livekit", relay):
            result = await plugin._route_remote_tool(
                "manage_trip_itinerary",
                {"action": "load"},
                session_id="20260827_172156_c71fef",
            )

        relay.assert_awaited_once_with(
            "manage_trip_itinerary",
            {"action": "load"},
            session_id="20260827_172156_c71fef",
        )
        self.assertEqual(result, '{"linked":true}')

    async def test_cross_process_relay_uses_worker_protocol_and_source(self):
        worker = SimpleNamespace(identity="simulated-agent-worker")

        class FakeAccessToken:
            def __init__(self, *_args):
                pass

            def with_identity(self, _identity):
                return self

            def with_name(self, _name):
                return self

            def with_grants(self, _grants):
                return self

            def to_jwt(self):
                return "token"

        class FakeLocalParticipant:
            def __init__(self, room):
                self.room = room
                self.published = []

            async def publish_data(self, data, **kwargs):
                message = json.loads(data)
                self.published.append((message, kwargs))
                result = {
                    "type": "client:tool-result",
                    "call_id": message["call_id"],
                    "result": {"linked": False},
                }
                self.room.handlers["data_received"](
                    SimpleNamespace(
                        topic="hermes-control",
                        participant=worker,
                        data=json.dumps(result).encode(),
                    )
                )

        class FakeRoom:
            latest = None

            def __init__(self):
                FakeRoom.latest = self
                self.handlers = {}
                self.local_participant = FakeLocalParticipant(self)
                self.disconnected = False

            def on(self, name):
                def register(callback):
                    self.handlers[name] = callback
                    return callback

                return register

            async def connect(self, _url, _token):
                registration = {
                    "type": "client:tool-register",
                    "name": "manage_trip_itinerary",
                }
                self.handlers["data_received"](
                    SimpleNamespace(
                        topic="hermes-control",
                        participant=worker,
                        data=json.dumps(registration).encode(),
                    )
                )

            async def disconnect(self):
                self.disconnected = True

        fake_livekit = SimpleNamespace(
            api=SimpleNamespace(
                AccessToken=FakeAccessToken,
                VideoGrants=lambda **kwargs: kwargs,
            ),
            rtc=SimpleNamespace(Room=FakeRoom),
        )
        source = {
            "platform": "desktop",
            "user_id": "hermes-install:test",
            "user_name": "Local Hermes user",
            "chat_id": "gui-1",
            "hermes_session_id": "session-1",
        }

        with (
            patch.dict(sys.modules, {"livekit": fake_livekit}),
            patch.object(
                plugin,
                "_livekit_setting",
                side_effect=lambda name, default="": {
                    "LIVEKIT_URL": "ws://livekit.test:7880",
                    "LIVEKIT_API_KEY": "key",
                    "LIVEKIT_API_SECRET": "secret",
                    "LIVEKIT_ROOM": "ECL",
                }.get(name, default),
            ),
            patch.object(plugin, "_mira_source_context", return_value=source),
        ):
            result = await plugin._route_remote_tool_via_livekit(
                "manage_trip_itinerary", {"action": "load"}
            )

        self.assertEqual(json.loads(result), {"linked": False})
        published, publish_kwargs = FakeRoom.latest.local_participant.published[0]
        self.assertEqual(published["name"], "manage_trip_itinerary")
        self.assertEqual(published["arguments"]["_mira_source"], source)
        self.assertEqual(
            publish_kwargs["destination_identities"],
            ["simulated-agent-worker"],
        )
        self.assertTrue(FakeRoom.latest.disconnected)

    def test_desktop_source_uses_stable_install_identity(self):
        values = {
            "HERMES_SESSION_PLATFORM": "",
            "HERMES_SESSION_SOURCE": "desktop",
            "HERMES_SESSION_ID": "20260827_172156_c71fef",
            "HERMES_UI_SESSION_ID": "4967c651",
        }

        with (
            patch(
                "gateway.session_context.get_session_env",
                side_effect=lambda name, default="": values.get(name, default),
            ),
            patch.object(
                plugin,
                "_stable_local_user_id",
                return_value="hermes-install:test-install",
            ),
        ):
            source = plugin._mira_source_context({})

        self.assertEqual(
            source,
            {
                "platform": "desktop",
                "user_id": "hermes-install:test-install",
                "user_name": "Local Hermes user",
                "chat_id": "4967c651",
                "hermes_session_id": "20260827_172156_c71fef",
            },
        )

    def test_gateway_source_preserves_bound_platform_identity(self):
        values = {
            "HERMES_SESSION_PLATFORM": "discord",
            "HERMES_SESSION_SOURCE": "discord",
            "HERMES_SESSION_USER_ID": "discord-user-42",
            "HERMES_SESSION_USER_NAME": "Traveller",
            "HERMES_SESSION_CHAT_ID": "channel-1",
            "HERMES_SESSION_ID": "discord-session",
        }

        with patch(
            "gateway.session_context.get_session_env",
            side_effect=lambda name, default="": values.get(name, default),
        ):
            source = plugin._mira_source_context({})

        self.assertEqual(source["platform"], "discord")
        self.assertEqual(source["user_id"], "discord-user-42")
        self.assertEqual(source["hermes_session_id"], "discord-session")

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
