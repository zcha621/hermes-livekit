"""Cross-platform tools provided by the MiRA LiveKit plugin."""

from __future__ import annotations

from . import (
    _FIND_LOCAL_RECOMMENDATIONS_SCHEMA,
    _GET_CURRENT_TRIP_CONTEXT_SCHEMA,
    _MANAGE_TRIP_ITINERARY_SCHEMA,
    _route_account_planning_tool,
    _route_current_trip_context_tool,
    _route_local_recommendations_tool,
)


def register_tools(ctx) -> None:
    """Expose MiRA's model-selected data tools on every Hermes surface."""
    registrations = (
        (
            _FIND_LOCAL_RECOMMENDATIONS_SCHEMA,
            _route_local_recommendations_tool,
            "\U0001f50e",
        ),
        (
            _GET_CURRENT_TRIP_CONTEXT_SCHEMA,
            _route_current_trip_context_tool,
            "\U0001f9ed",
        ),
        (
            _MANAGE_TRIP_ITINERARY_SCHEMA,
            _route_account_planning_tool,
            "\U0001f5fa\ufe0f",
        ),
    )
    for schema, handler, emoji in registrations:
        ctx.register_tool(
            name=schema["name"],
            toolset="hermes-livekit",
            schema=schema,
            handler=handler,
            is_async=True,
            description=schema["description"],
            emoji=emoji,
        )
