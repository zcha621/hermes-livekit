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

    async def test_text_revision_preserves_mapped_stop_and_confirmation_evidence(self):
        original = {
            "item_id": "saved-stop", "activity": "Visit the tagged place",
            "starts_at": "2026-08-30T17:00:00+12:00", "ends_at": "2026-08-30T17:30:00+12:00",
            "location": {"name": "Selected food court", "latitude": -36.85, "longitude": 174.76},
            "transportation": {"mode": "walking"}, "source_tag_id": "selected-tag",
            "confirmation_event_id": "actual-voice", "confirmation_text": "Yes, add that place",
            "notes": "Meet by the entrance",
        }
        calls = []
        def post(command):
            calls.append(command)
            return {"itinerary": {"items": [original]}} if command["action"] == "load" else {"saved": True}
        with patch.object(tools, "_post_gateway_command", side_effect=post):
            result = await tools._handle_save_itinerary_draft({
                "plan_text": "16:00 - 16:30 Visit the tagged place\n18:00 - Dinner",
                "plan_date": "2026-08-30",
            })
        self.assertTrue(json.loads(result)["saved"])
        self.assertEqual([c["action"] for c in calls], ["load", "revise"])
        saved = calls[1]["draft"]["items"][0]
        self.assertEqual(saved, {**original, "starts_at": "2026-08-30T16:00:00+12:00", "ends_at": "2026-08-30T16:30:00+12:00"})
        self.assertEqual(original["starts_at"], "2026-08-30T17:00:00+12:00")

    async def test_verified_location_reaches_saved_draft(self):
        location = {"activity": "Walk in the park", "name": "Selected park", "latitude": -36.85, "longitude": 174.76}
        with patch.object(tools, "_post_gateway_command", return_value={}) as post:
            result = await tools._handle_save_itinerary_draft({
                "plan_text": "10:00 - Walk in the park", "plan_date": "2026-08-30", "stop_locations": [location],
            })
        self.assertNotIn("error", json.loads(result))
        self.assertEqual(post.call_args.args[0]["draft"]["items"][0]["location"], {k:v for k,v in location.items() if k != "activity"})

    async def test_unavailable_previous_plan_prevents_destructive_rewrite(self):
        with patch.object(tools, "_post_gateway_command", side_effect=RuntimeError("database unavailable")) as post:
            result = await tools._handle_save_itinerary_draft({"plan_text": "10:00 - Walk", "plan_date": "2026-08-30"})
        self.assertIn("database unavailable", json.loads(result)["error"])
        self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args.args[0]["action"], "load")

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
    def test_registers_native_planning_and_confirmed_tag_tools(self):
        ctx = Mock()
        tools.register_tools(ctx)

        self.assertEqual(ctx.register_tool.call_count, 3)
        names = [call.kwargs["name"] for call in ctx.register_tool.call_args_list]
        self.assertEqual(names, ["save_itinerary_draft", "confirm_itinerary_draft", "add_confirmed_place_to_itinerary"])
        self.assertTrue(all(call.kwargs["toolset"] == "hermes-livekit" for call in ctx.register_tool.call_args_list))
        self.assertTrue(all(call.kwargs["is_async"] for call in ctx.register_tool.call_args_list))



class MultiDayAndClockTests(unittest.TestCase):
    PLAN = """**Day 1 – Sat 26 Sept**
- 9:30 – Brunch at Hindmarsh Market
- 12:00 – Walk along the River Torrens
- 1:00 – Lunch on Rundle Mall
- 5:30 – Dinner in the CBD
**Day 2 – Sun 27 Sept**
- 10:00 – Slow start, coffee in the CBD
- 1:00 – Lunch
- 2:30-4 – Art gallery second look"""

    def test_day_headings_move_activities_to_their_own_date(self):
        items = tools._parse_itinerary_text(self.PLAN, "2026-09-26")
        self.assertEqual(
            [(item["starts_at"], item["description"]) for item in items],
            [
                ("2026-09-26T09:30:00", "Brunch at Hindmarsh Market"),
                ("2026-09-26T12:00:00", "Walk along the River Torrens"),
                ("2026-09-26T13:00:00", "Lunch on Rundle Mall"),
                ("2026-09-26T17:30:00", "Dinner in the CBD"),
                ("2026-09-27T10:00:00", "Slow start, coffee in the CBD"),
                ("2026-09-27T13:00:00", "Lunch"),
                ("2026-09-27T14:30:00", "Art gallery second look"),
            ],
        )
        self.assertEqual(items[-1]["ends_at"], "2026-09-27T16:00:00")

    def test_heading_forms_resolve_to_dates(self):
        base = tools.datetime.fromisoformat("2026-09-26").date()
        self.assertEqual(str(tools._heading_date("Day 3", base)), "2026-09-28")
        self.assertEqual(str(tools._heading_date("Day 2 - 2026-09-27", base)), "2026-09-27")
        self.assertEqual(str(tools._heading_date("Sunday, Sept 27th:", base)), "2026-09-27")
        self.assertEqual(str(tools._heading_date("Monday", base)), "2026-09-28")
        self.assertIsNone(tools._heading_date("Sunday markets in the park", base))
        self.assertIsNone(tools._heading_date("Bring a jacket", base))

    def test_explicit_meridiem_and_24_hour_times_are_kept(self):
        items = tools._parse_itinerary_text(
            "6:00 am - Sunrise hike\n07:30 - Breakfast\n13:15 - Ferry\n8 pm - Dinner",
            "2026-09-26",
        )
        self.assertEqual(
            [item["starts_at"][11:16] for item in items],
            ["06:00", "07:30", "13:15", "20:00"],
        )

    def test_evening_times_after_inferred_afternoon_stay_in_the_evening(self):
        items = tools._parse_itinerary_text(
            "11:00 - Museum\n1:00 - Lunch\n5:30 - Beach\n8:00 - Dinner", "2026-09-26"
        )
        self.assertEqual(
            [item["starts_at"][11:16] for item in items],
            ["11:00", "13:00", "17:30", "20:00"],
        )


class BackendAuthorizationTests(unittest.TestCase):
    def test_signs_a_python_context_worker_token_when_a_key_is_configured(self):
        import tempfile

        import jwt
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        key = Ed25519PrivateKey.generate()
        pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        with tempfile.TemporaryDirectory() as directory:
            key_path = Path(directory) / "key.pem"
            key_path.write_bytes(pem)
            env = {"MIRA_AUTH_PRIVATE_KEY_PATH": str(key_path), "MIRA_AUTH_KEY_ID": "kid-1"}
            with patch.dict("os.environ", env):
                header = tools._worker_authorization_header()
        token = header.removeprefix("Bearer ")
        self.assertEqual(jwt.get_unverified_header(token)["kid"], "kid-1")
        claims = jwt.decode(
            token, key.public_key(), algorithms=["EdDSA"], audience="tourism-ai-backend"
        )
        self.assertEqual(claims["sub"], "python-context-worker")
        self.assertEqual(claims["iss"], "https://mira.local/auth")
        self.assertTrue({"accounts:read", "accounts:write"} <= set(claims["scope"].split()))

    def test_no_header_when_unconfigured_or_left_as_a_hermes_placeholder(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertIsNone(tools._worker_authorization_header())
        placeholders = {
            "MIRA_AUTH_PRIVATE_KEY_PATH": "${MIRA_AUTH_PRIVATE_KEY_PATH}",
            "MIRA_AUTH_KEY_ID": "${MIRA_AUTH_KEY_ID}",
        }
        with patch.dict("os.environ", placeholders, clear=True):
            self.assertIsNone(tools._worker_authorization_header())

if __name__ == "__main__":
    unittest.main()

class StopLocationRetentionTests(unittest.TestCase):
    def draft(self, text='10:00 - Walk', date='2026-08-30'):
        return tools._build_draft(title='Day', summary='Day', timezone='Pacific/Auckland', plan_date=date, plan_text=text, requirements='')

    def test_changed_activity_or_day_never_inherits_coordinates(self):
        previous = self.draft()
        previous['items'][0]['location'].update(latitude=-36.85, longitude=174.76)
        for draft in (self.draft('10:00 - Different walk'), self.draft(date='2026-08-31')):
            tools._retain_stop_details(draft, previous)
            self.assertNotIn('latitude', draft['items'][0]['location'])

    def test_repeated_activities_are_not_matched_arbitrarily(self):
        previous = self.draft()
        previous['items'][0]['location'].update(latitude=-36.85, longitude=174.76)
        draft = self.draft('10:00 - Walk\n13:00 - Walk')
        tools._retain_stop_details(draft, previous)
        self.assertTrue(all('latitude' not in i['location'] for i in draft['items']))
        with self.assertRaises(ValueError):
            tools._apply_stop_locations(draft, [{'activity':'Walk','name':'Park','latitude':-36.85,'longitude':174.76}])

    def test_invalid_or_unmatched_coordinates_are_rejected(self):
        for updates in ({'latitude':91}, {'longitude':float('nan')}, {'latitude':True}, {'activity':'Missing'}, {'name':''}):
            point = {'activity':'Walk','name':'Park','latitude':-36.85,'longitude':174.76, **updates}
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                tools._apply_stop_locations(self.draft(), [point])

    def test_tag_confirmation_cannot_be_reused_for_a_different_location(self):
        draft = self.draft()
        draft['items'][0]['source_tag_id'] = 'original-tag'
        with self.assertRaises(ValueError):
            tools._apply_stop_locations(draft, [{'activity':'Walk','name':'Another place','latitude':-36.85,'longitude':174.76}])
