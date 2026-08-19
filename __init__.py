"""hermes-livekit — LiveKit voice gateway plugin for hermes-agent.

Registers a ``livekit`` platform via the ``hermes_agent.plugins`` entry
point. No core hermes-agent edits are required — every integration touch
point uses an existing ``register_platform()`` hook.
"""

import logging
import os
import sys
from pathlib import Path
from typing import Optional

_PLUGIN_DIR = str(Path(__file__).resolve().parent)
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

from adapter import (
    LIVE_ADAPTERS,
    TOOLSET_NAME,
    LiveKitAdapter,
    check_livekit_requirements,
)

logger = logging.getLogger("gateway.platforms.livekit")

__all__ = ["register", "LiveKitAdapter", "check_livekit_requirements"]

_PLUGIN_ROOT = Path(__file__).resolve().parent
_TOURISM_SKILL_PATH = (
    _PLUGIN_ROOT / "skills" / "mira-new-zealand-tourism" / "SKILL.md"
)


def _skill_body(path: Path) -> str:
    """Load a bundled skill body without its YAML frontmatter."""
    try:
        content = path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    if not content.startswith("---"):
        return content
    parts = content.split("---", 2)
    return parts[2].strip() if len(parts) == 3 else content


def _on_session_finalize_hook(**kwargs) -> None:
    """Cancel pending remote tool calls when the user resets the session.

    Hermes fires ``on_session_finalize`` from ``_handle_reset_command`` —
    i.e. when the user issues ``/new``. The adapter's proxy coroutines are
    blocked on per-call futures; without this hook they'd hang until the
    per-call timeout.
    """
    for adapter in list(LIVE_ADAPTERS):
        try:
            adapter._finish_tool_acknowledgement_turn()
            n = adapter.cancel_pending_tool_calls_for_session_reset()
            if n:
                logger.info("session finalize: cancelled %d in-flight remote tool call(s)", n)
        except Exception as exc:
            logger.debug("session-finalize cleanup failed for %s: %s", adapter, exc)


def _livekit_source(event):
    source = getattr(event, "source", None)
    platform = getattr(getattr(source, "platform", None), "value", "")
    return source if platform == "livekit" else None


def _on_pre_gateway_dispatch_hook(event=None, session_store=None, **kwargs) -> None:
    """Bind a new LiveKit turn to Hermes's canonical session ID."""
    source = _livekit_source(event)
    if source is None or session_store is None:
        return
    try:
        session_id = session_store.get_or_create_session(source).session_id
    except Exception as exc:
        logger.debug("could not bind LiveKit acknowledgement session: %s", exc)
        return

    chat_id = str(getattr(source, "chat_id", "") or "")
    for adapter in list(LIVE_ADAPTERS):
        if not chat_id or adapter._room_name == chat_id:
            adapter.bind_tool_acknowledgement_session(session_id)


def _on_pre_tool_call_hook(
    session_id="", turn_id="", tool_call_id="", **kwargs
) -> None:
    """Mirror Discord: acknowledge once, immediately before the first tool."""
    for adapter in list(LIVE_ADAPTERS):
        adapter.schedule_tool_acknowledgement(
            session_id=session_id,
            turn_id=turn_id,
            tool_call_id=tool_call_id,
        )

_LIVEKIT_PLATFORM_HINT = """You are MiRA on LiveKit, a travel companion for people on live video calls with remote family or friends.

Primary goal: help the user stay natural, helpful, and present during the call. Give practical travel guidance, conversational support, local suggestions, itinerary ideas, and help with what to say or do next.

Interaction rules:
- Do not narrate routine internal work or add a generic acknowledgement to every turn. The voice adapter supplies one brief cue only when the first tool actually starts.
- Prefer concise, spoken-friendly answers. Keep wording natural and easy to say aloud.
- Use text for quick confirmations, links, addresses, code, or details that are easier to read than hear.
- Avoid heavy markdown or long lists unless the user explicitly asks for them.
- If a tool, image, video, phone state, or other context signal is unavailable, do not pretend it exists. Be explicit about what you can and cannot verify.
- Ask one short clarifying question only when needed; otherwise make a reasonable travel-oriented recommendation.

Context-aware behavior:
- When conversation context suggests the user is on a trip, prioritize location-aware and situation-aware help.
- A recent camera or shared-screen frame may be attached to a user turn. Use it when relevant, distinguish observation from inference, and do not claim to see anything when no image is attached.
- When phone usage or call state is relevant later, favor actions that fit a live call setting: short replies, quick guidance, and minimal interruption.

Style:
- Sound warm, calm, and confident.
- Do not over-explain unless asked.
- Keep voice replies short enough to feel natural in a live conversation.
- You can send text messages alongside voice replies when that helps clarity."""

_TOURISM_GUIDANCE = _skill_body(_TOURISM_SKILL_PATH)
if _TOURISM_GUIDANCE:
    # Platform hints are a stable prompt layer. Including the read-only bundled
    # skill here gives every LiveKit session the same tourism operating guidance
    # without exposing Hermes's general skill discovery or skill-write tools.
    _LIVEKIT_PLATFORM_HINT += "\n\n" + _TOURISM_GUIDANCE


def _env_enablement() -> Optional[dict]:
    """Seed ``PlatformConfig.extra`` from env vars during gateway config load.

    Called by the platform registry BEFORE the adapter is constructed, so
    ``hermes gateway status`` reflects env-only configuration without
    instantiating the LiveKit SDK. Returns ``None`` when LiveKit isn't
    minimally configured; the caller skips auto-enabling.
    """
    url = (os.getenv("LIVEKIT_URL") or "").strip()
    api_key = (os.getenv("LIVEKIT_API_KEY") or "").strip()
    api_secret = (os.getenv("LIVEKIT_API_SECRET") or "").strip()
    if not (url and api_key and api_secret):
        return None

    room = os.getenv("LIVEKIT_ROOM", "hermes")
    seed: dict = {
        "url": url,
        "api_key": api_key,
        "api_secret": api_secret,
        "room": room,
        "agent_name": os.getenv("LIVEKIT_AGENT_NAME", "Hermes"),
        "agent_avatar": os.getenv("LIVEKIT_AGENT_AVATAR", ""),
    }

    # LiveKit's adapter only ever joins one room, so the room IS the home
    # channel by definition. Default LIVEKIT_HOME_CHANNEL to LIVEKIT_ROOM
    # unless explicitly overridden — keeps cron / cross-platform delivery
    # sensible without requiring the user to duplicate the value.
    home = (os.getenv("LIVEKIT_HOME_CHANNEL") or room).strip()
    if home:
        os.environ.setdefault("LIVEKIT_HOME_CHANNEL", home)
        seed["home_channel"] = {
            "chat_id": home,
            "name": os.getenv("LIVEKIT_HOME_CHANNEL_NAME", "Home"),
        }
    return seed


def _is_connected(cfg) -> bool:
    """True when the gateway should consider LiveKit configured.

    Mirrors the ``cfg.extra.get(

url)`` check that the kortexa branch
    inlined in ``_PLATFORM_CONNECTED_CHECKERS``. The url is the load-bearing
    field — without it, neither the SDK nor presence polling can run.
    """
    try:
        return bool((cfg.extra or {}).get("url"))
    except Exception:
        return False


def _interactive_setup() -> None:
    """Prompt the user for LiveKit credentials and persist to .env.

    Minimal first-pass setup — falls back to instructions when the
    interactive helpers aren't importable. The standalone-platform
    setup wizard in ``hermes_cli/gateway.py`` covers most env-driven
    setups; this is a plugin-side fallback for ``hermes config`` flows
    that bypass that wizard.
    """
    try:
        from hermes_cli.config import set_env_value
    except Exception:
        print("LiveKit interactive setup requires a hermes-agent install.")
        print("Set these env vars manually in your .env:")
        print("  LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET")
        print("  LIVEKIT_ROOM (default: hermes)")
        return

    print("\\nLiveKit setup (press Enter to skip a value)")
    url = input("  LIVEKIT_URL (wss://...): ").strip()
    if url:
        set_env_value("LIVEKIT_URL", url)
    api_key = input("  LIVEKIT_API_KEY: ").strip()
    if api_key:
        set_env_value("LIVEKIT_API_KEY", api_key)
    api_secret = input("  LIVEKIT_API_SECRET: ").strip()
    if api_secret:
        set_env_value("LIVEKIT_API_SECRET", api_secret)
    room = input("  LIVEKIT_ROOM (default: hermes): ").strip()
    if room:
        set_env_value("LIVEKIT_ROOM", room)
    print("LiveKit settings saved.")


def register(ctx) -> None:
    """Plugin entry point — called by the hermes-agent plugin loader.

    Registers a ``livekit`` platform that can be enabled in
    ``~/.hermes/config.yaml`` (``platforms.livekit.enabled: true``) and
    auto-configures from ``LIVEKIT_URL`` / ``LIVEKIT_API_KEY`` /
    ``LIVEKIT_API_SECRET`` env vars.
    """
    ctx.register_platform(
        name="livekit",
        label="LiveKit",
        adapter_factory=lambda cfg: LiveKitAdapter(cfg),
        check_fn=check_livekit_requirements,
        is_connected=_is_connected,
        required_env=["LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET"],
        install_hint="pip install hermes-livekit  # adds livekit + livekit-api SDKs",
        setup_fn=_interactive_setup,
        # Env-driven auto-config: seeds PlatformConfig.extra + home_channel
        # from LIVEKIT_* env vars, so env-only setups show up in
        # `hermes gateway status` without instantiating the adapter.
        env_enablement_fn=_env_enablement,
        # Cron home-channel delivery support.
        cron_deliver_env_var="LIVEKIT_HOME_CHANNEL",
        # Auth env vars
        allowed_users_env="LIVEKIT_ALLOWED_USERS",
        allow_all_env="LIVEKIT_ALLOW_ALL_USERS",
        # Display
        emoji="🤖‍♂️",
        # LiveKit identities are not phone numbers / emails
        pii_safe=False,
        # /update from a voice channel makes no sense
        allow_update_command=False,
        # LLM guidance — delivered to run_agent.py via PlatformEntry.platform_hint
        platform_hint=_LIVEKIT_PLATFORM_HINT,
    )

    # Keep the guidance available as a namespaced read-only plugin skill for
    # CLI inspection. LiveKit receives the same content in its platform hint,
    # so the voice surface does not need skill discovery or write tools.
    try:
        ctx.register_skill("mira-new-zealand-tourism", _TOURISM_SKILL_PATH)
    except Exception as exc:
        logger.debug("tourism skill registration failed: %s", exc)

    # The LiveKit platform default must be narrow. Client-offered tools are
    # registered into TOOLSET_NAME after a trusted worker joins the room. Do
    # not inherit Hermes core tools here: that would expose terminal, files,
    # browser, code execution, delegation, and global MCP servers to voice.
    try:
        from toolsets import TOOLSETS
        TOOLSETS["hermes-livekit"] = {
            "description": "MiRA LiveKit tools supplied by the trusted room worker",
            "tools": [],
            "includes": [TOOLSET_NAME],
        }
    except Exception:
        logger.exception("could not register the restricted LiveKit toolset")

    # Match Hermes Discord voice behavior: arm the LiveKit turn at gateway
    # dispatch and speak one cue only on the first actual tool invocation.
    try:
        ctx.register_hook("pre_gateway_dispatch", _on_pre_gateway_dispatch_hook)
        ctx.register_hook("pre_tool_call", _on_pre_tool_call_hook)
    except Exception as exc:
        logger.debug("acknowledgement hook registration failed: %s", exc)

    # Cancel remote-tool futures at session boundaries. Regular spoken
    # interruption is handled by Hermes's busy_input_mode=interrupt path.
    try:
        ctx.register_hook("on_session_finalize", _on_session_finalize_hook)
    except Exception as exc:
        logger.debug("hook registration failed: %s", exc)

