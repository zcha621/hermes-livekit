import importlib.util
import json
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "hermes_livekit_itinerary_tools_test", PLUGIN_ROOT / "itinerary_tools.py"
)
tools = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(tools)


class ParseItineraryTextTests(unittest.TestCase):
    def test_parses_one_activity_per_timed_line(self):
        text = (
            "9:00 - Coffee at Villa Martinique, Great North Rd\n"
            "10:00 - Walk through Viaduct Harbour\n"
            "12:00 - Lunch at Mountain Goat, Saunders St"
        )
        items = tools._parse_itinerary_text(text, "2026-08-30")
        self.assertEqual(len(items), 3)
        self.assertEqual(items[0]["starts_at"], "2026-08-30T09:00:00")
        self.assertEqual(
            items[0]["description"], "Coffee at Villa Martinique, Great North Rd"
        )

    def test_default_duration_when_no_end_time_given(self):
        items = tools._parse_itinerary_text("9:00 - Coffee\n10:30 - Walk", "2026-08-30")
        self.assertEqual(items[0]["ends_at"], "2026-08-30T10:00:00")

    def test_explicit_time_range_sets_end_time(self):
        items = tools._parse_itinerary_text("9:00 - 10:30 Coffee and a walk", "2026-08-30")
        self.assertEqual(items[0]["starts_at"], "2026-08-30T09:00:00")
        self.assertEqual(items[0]["ends_at"], "2026-08-30T10:30:00")

    def test_am_pm_suffix_handled(self):
        items = tools._parse_itinerary_text("9am - Breakfast\n2pm - Museum", "2026-08-30")
        self.assertEqual(items[0]["starts_at"], "2026-08-30T09:00:00")
        self.assertEqual(items[1]["starts_at"], "2026-08-30T14:00:00")

    def test_section_header_folds_into_next_item(self):
        items = tools._parse_itinerary_text(
            "Morning - inner city walks\n9:00 - Coffee at Villa Martinique", "2026-08-30"
        )
        self.assertEqual(len(items), 1)
        self.assertIn("Morning - inner city walks", items[0]["description"])
        self.assertIn("Coffee at Villa Martinique", items[0]["description"])

    def test_items_sorted_chronologically(self):
        items = tools._parse_itinerary_text(
            "14:00 - Afternoon activity\n9:00 - Morning activity", "2026-08-30"
        )
        self.assertEqual(
            [item["description"] for item in items],
            ["Morning activity", "Afternoon activity"],
        )


class BuildDraftTests(unittest.TestCase):
    def test_build_draft_produces_backend_ready_shape(self):
        draft = tools._build_draft(
            title="A Rotorua day",
            summary="A Rotorua day",
            timezone="Pacific/Auckland",
            plan_date="2026-08-30",
            plan_text="9:00 - Te Puia geothermal valley\n13:00 - Lunch at the Barn Cafe",
            requirements="",
        )
        self.assertEqual(draft["title"], "A Rotorua day")
        self.assertEqual(len(draft["items"]), 2)
        first = draft["items"][0]
        self.assertEqual(first["activity"], "Te Puia geothermal valley")
        self.assertEqual(first["location"], {"name": "Te Puia geothermal valley"})
        self.assertEqual(first["transportation"], {"mode": "unspecified"})
        import uuid

        uuid.UUID(first["item_id"])
        self.assertTrue(first["starts_at"].endswith("+12:00") or first["starts_at"].endswith("+13:00"))
        self.assertEqual(draft["requirements"], "No specific requirements noted.")

    def test_build_draft_with_no_timed_lines_is_empty(self):
        draft = tools._build_draft(
            title="Empty",
            summary="Empty",
            timezone="Pacific/Auckland",
            plan_date="2026-08-30",
            plan_text="Just prose, no times here.",
            requirements="",
        )
        self.assertEqual(draft["items"], [])


class HandlerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        session_patcher = patch.object(
            tools, "_session_source", return_value=("livekit", "alice", "session-1")
        )
        self.session_source = session_patcher.start()
        self.addCleanup(session_patcher.stop)

    async def test_save_itinerary_draft_posts_normalized_command(self):
        captured = {}

        def fake_post(command):
            captured.update(command)
            return {"linked": True, "draft": {"revision": 1}}

        with patch.object(tools, "_post_gateway_command", side_effect=fake_post):
            result = await tools._handle_save_itinerary_draft(
                {
                    "plan_text": "9:00 - Coffee at Villa Martinique",
                    "plan_date": "2026-08-30",
                    "title": "A day out",
                }
            )

        self.assertEqual(captured["action"], "revise")
        self.assertEqual(
            captured["source"],
            {
                "platform": "livekit",
                "user_id": "alice",
                "chat_id": "session-1",
                "hermes_session_id": "session-1",
            },
        )
        self.assertEqual(len(captured["draft"]["items"]), 1)
        self.assertEqual(json.loads(result), {"linked": True, "draft": {"revision": 1}})

    async def test_save_itinerary_draft_rejects_missing_required_fields(self):
        result = await tools._handle_save_itinerary_draft({"plan_text": ""})
        self.assertIn("error", json.loads(result))

    async def test_save_itinerary_draft_reports_when_no_items_parsed(self):
        result = await tools._handle_save_itinerary_draft(
            {"plan_text": "just some prose", "plan_date": "2026-08-30"}
        )
        self.assertIn("error", json.loads(result))

    async def test_save_itinerary_draft_surfaces_backend_errors(self):
        with patch.object(
            tools, "_post_gateway_command", side_effect=RuntimeError("backend rejected it")
        ):
            result = await tools._handle_save_itinerary_draft(
                {"plan_text": "9:00 - Coffee", "plan_date": "2026-08-30"}
            )
        self.assertEqual(json.loads(result), {"error": "backend rejected it"})

    async def test_confirm_itinerary_draft_posts_confirm_command(self):
        captured = {}

        def fake_post(command):
            captured.update(command)
            return {"linked": True, "itinerary": {"revision": 2}}

        with patch.object(tools, "_post_gateway_command", side_effect=fake_post):
            result = await tools._handle_confirm_itinerary_draft({"expected_revision": 2})

        self.assertEqual(captured["action"], "confirm")
        self.assertEqual(captured["expected_revision"], 2)
        self.assertEqual(json.loads(result), {"linked": True, "itinerary": {"revision": 2}})

    async def test_confirm_itinerary_draft_rejects_non_integer_revision(self):
        result = await tools._handle_confirm_itinerary_draft({"expected_revision": "not-a-number"})
        self.assertIn("error", json.loads(result))


class RegisterToolsTests(unittest.TestCase):
    def test_registers_both_native_tools(self):
        ctx = Mock()
        tools.register_tools(ctx)

        self.assertEqual(ctx.register_tool.call_count, 2)
        names = [call.kwargs["name"] for call in ctx.register_tool.call_args_list]
        self.assertEqual(names, ["save_itinerary_draft", "confirm_itinerary_draft"])
        self.assertTrue(all(call.kwargs["toolset"] == "hermes-livekit" for call in ctx.register_tool.call_args_list))
        self.assertTrue(all(call.kwargs["is_async"] for call in ctx.register_tool.call_args_list))


if __name__ == "__main__":
    unittest.main()
