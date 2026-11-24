"""Tool entry point for Hermes' deferred platform-plugin discovery."""


def register_tools(ctx):
    from .itinerary_tools import register_tools as register_itinerary_tools
    from .context_tools import register_tools as register_context_tools
    register_itinerary_tools(ctx)
    register_context_tools(ctx)
