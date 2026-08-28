"""hermes-livekit — LiveKit voice gateway plugin for hermes-agent.

Registers a ``livekit`` platform via the ``hermes_agent.plugins`` entry
point. No core hermes-agent edits are required — every integration touch
point uses an existing ``register_platform()`` hook.
"""

import json
import logging
import os
import re
import uuid
from pathlib import Path
from typing import Optional

try:
    from .adapter import (  # type: ignore[import-not-found]  # noqa: E402
        LIVE_ADAPTERS,
        LiveKitAdapter,
        check_livekit_requirements,
    )
except ImportError:  # Direct-file test and legacy entry-point compatibility.
    from adapter import (  # noqa: E402
        LIVE_ADAPTERS,
        LiveKitAdapter,
        check_livekit_requirements,
    )

logger = logging.getLogger("gateway.platforms.livekit")

__all__ = ["register", "LiveKitAdapter", "check_livekit_requirements"]

_PLUGIN_ROOT = Path(__file__).resolve().parent
_TOURISM_SKILL_PATH = (
    _PLUGIN_ROOT / "skills" / "mira-new-zealand-tourism" / "SKILL.md"
)

_TEXT_TOOL_CALL_RE = re.compile(
    r"<tool_call>\s*(\{.*?\})\s*</tool_call>",
    re.IGNORECASE | re.DOTALL,
)


def _qwen_realtime_request_middleware(**kwargs):
    """Put Spark's native non-thinking flag on realtime LiveKit requests."""
    if str(kwargs.get("platform") or "").lower() != "livekit":
        return None
    if "qwen3" not in str(kwargs.get("model") or "").lower():
        return None
    request = kwargs.get("request")
    if not isinstance(request, dict):
        return None
    updated = dict(request)
    current_extra_body = updated.get("extra_body")
    extra_body = (
        dict(current_extra_body) if isinstance(current_extra_body, dict) else {}
    )
    extra_body["enable_thinking"] = False
    updated["extra_body"] = extra_body

    messages = updated.get("messages")
    if isinstance(messages, list):
        rewritten_messages = list(messages)
        for index in range(len(rewritten_messages) - 1, -1, -1):
            message = rewritten_messages[index]
            if not isinstance(message, dict) or message.get("role") != "user":
                continue
            rewritten = dict(message)
            content = rewritten.get("content")
            if isinstance(content, str):
                if "/no_think" not in content:
                    rewritten["content"] = content.rstrip() + "\n\n/no_think"
            elif isinstance(content, list):
                parts = list(content)
                parts.append({"type": "text", "text": "/no_think"})
                rewritten["content"] = parts
            rewritten_messages[index] = rewritten
            break
        updated["messages"] = rewritten_messages
    return {
        "request": updated,
        "reason": "livekit_qwen_realtime_nonthinking",
    }


def _on_post_api_request_hook(**kwargs) -> None:
    """Normalize Qwen's text-encoded tool decision before Hermes dispatch.

    The Spark Qwen3-VL chat template can emit a correct ``<tool_call>`` JSON
    block while its OpenAI-compatible server leaves ``message.tool_calls``
    empty. Hermes otherwise strips the block as non-user-facing markup and
    retries the whole model request. This hook translates only that existing
    model decision into Hermes's canonical response type; tool selection,
    registry validation, approval, and execution remain owned by Hermes.
    """
    if str(kwargs.get("platform") or "").lower() not in {"livekit", "discord"}:
        return
    if "qwen3" not in str(kwargs.get("model") or "").lower():
        return
    assistant_message = kwargs.get("assistant_message")
    if assistant_message is None or getattr(assistant_message, "tool_calls", None):
        return
    content = getattr(assistant_message, "content", None)
    if not isinstance(content, str) or "<tool_call>" not in content.lower():
        return

    try:
        from agent.transports.types import ToolCall
    except Exception:
        logger.debug("Qwen text tool-call compatibility type unavailable", exc_info=True)
        return

    tool_calls = []
    valid_matches = []
    for match in _TEXT_TOOL_CALL_RE.finditer(content):
        try:
            payload = json.loads(match.group(1))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        name = str(payload.get("name") or "").strip()
        arguments = payload.get("arguments")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.:-]{0,127}", name):
            continue
        if not isinstance(arguments, dict):
            continue
        tool_calls.append(
            ToolCall(
                id=f"call_{uuid.uuid4().hex[:24]}",
                name=name,
                arguments=json.dumps(arguments, ensure_ascii=False),
            )
        )
        valid_matches.append(match)

    if not tool_calls:
        return
    visible = content
    for match in reversed(valid_matches):
        visible = visible[: match.start()] + visible[match.end() :]
    assistant_message.content = visible.strip() or None
    assistant_message.tool_calls = tool_calls
    assistant_message.finish_reason = "tool_calls"
    logger.info(
        "normalized %d Qwen text tool call(s) for %s",
        len(tool_calls),
        str(kwargs.get("platform") or "?"),
    )

def _on_session_finalize_hook(session_id="", **kwargs) -> None:
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


_LIVEKIT_PLATFORM_HINT = """This conversation arrived through LiveKit. Speech is
already transcribed, and a recent camera or shared-screen frame may be attached
when the participant explicitly refers to visual context. Traveller context —
their confirmed itinerary, current location and local time, and earlier speech
from this meeting — is available through the hermes-mira-context MCP server's
tools, alongside Hermes's normal tools, skills, and other MCP servers. Decide whether and when to use them exactly as in any other Hermes conversation.
For this realtime Qwen route, respond without extended reasoning so a selected
tool can start immediately.
/no_think"""


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
        "agent_avatar": os.getenv("LIVEKIT_AGENT_AVATAR", ""),
    }

    # Do not seed agent_name here. Hermes applies this mapping with
    # ``PlatformConfig.extra.update()``, so an environment fallback would
    # overwrite the explicit platforms.livekit.extra.agent_name saved by the
    # MiRA portal. LiveKitAdapter already falls back to LIVEKIT_AGENT_NAME when
    # no configured value exists.

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

    # Register the domain guidance as a normal Hermes skill. It is deliberately
    # not injected into every LiveKit prompt: Hermes decides when to load it.
    try:
        ctx.register_skill("mira-new-zealand-tourism", _TOURISM_SKILL_PATH)
    except Exception as exc:
        logger.debug("tourism skill registration failed: %s", exc)

    # Itinerary save/confirm as native tools, not MCP: this deployment's
    # model has never once successfully invoked an MCP-prefixed tool, while
    # natively-registered tools are called reliably (see itinerary_tools.py's
    # docstring for the evidence). Read-only lookups (get_confirmed_itinerary,
    # get_traveller_location, get_meeting_transcript) still come from the
    # hermes-mira-context MCP server — lower-stakes if occasionally skipped.
    try:
        try:
            from .itinerary_tools import register_tools
        except ImportError:
            from itinerary_tools import register_tools
        register_tools(ctx)
    except Exception:
        logger.exception("could not register the itinerary save/confirm tools")

    # No backend orchestration hooks are registered. LiveKit and Discord use
    # Hermes's normal model loop, and context/action calls occur only when
    # Hermes selects a registered tool. The post-API hook only normalizes the
    # configured Qwen server's text-encoded tool-call wire format; the lifecycle
    # hook releases in-flight futures during reset.
    try:
        ctx.register_middleware("llm_request", _qwen_realtime_request_middleware)
        ctx.register_hook("post_api_request", _on_post_api_request_hook)
        ctx.register_hook("on_session_finalize", _on_session_finalize_hook)
    except Exception as exc:
        logger.debug("hook registration failed: %s", exc)

