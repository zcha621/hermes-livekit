"""Cross-platform tools provided by the MiRA LiveKit plugin."""

from __future__ import annotations

from . import _MANAGE_TRIP_ITINERARY_SCHEMA, _route_account_planning_tool


def register_tools(ctx) -> None:
    """Expose account itinerary planning to every configured Hermes surface."""
    ctx.register_tool(
        name="manage_trip_itinerary",
        toolset="hermes-livekit",
        schema=_MANAGE_TRIP_ITINERARY_SCHEMA,
        handler=_route_account_planning_tool,
        is_async=True,
        description=_MANAGE_TRIP_ITINERARY_SCHEMA["description"],
        emoji="\U0001f5fa\ufe0f",  # world map
    )
