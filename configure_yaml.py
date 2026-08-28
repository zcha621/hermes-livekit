"""Apply non-secret Hermes LiveKit behavior defaults to config.yaml."""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

import yaml


INVOCATION_KEYTERMS = ["Hermes", "MiRA"]
# Fallback only for a config that has no CLI/Discord selection to copy. Normal
# setup derives LiveKit and Discord from the user's CLI (Hermes GUI) toolsets.
DEFAULT_HERMES_CONVERSATION_TOOLSETS = [
    "hermes-livekit",
    "browser",
    "clarify",
    "code_execution",
    "computer_use",
    "cronjob",
    "delegation",
    "file",
    "image_gen",
    "memory",
    "session_search",
    "skills",
    "terminal",
    "todo",
    "tts",
    "vision",
    "web",
]
# No tool names are host-managed by default any more — MiRA's itinerary,
# location, and meeting-transcript context is served by the
# hermes-mira-context MCP server (see MCP_SERVER_NAME below) instead of
# LiveKit remote tools. This stays configurable/non-empty for operators who
# still want a client to offer its own tool over the data channel.
REMOTE_TOOL_NAMES: list[str] = []
REMOTE_TOOL_OWNER_PREFIXES = [
    "agent-mira-knowledge-worker-",
    # LiveKit Agents 1.2.x uses this identity in ``connect --room`` mode,
    # which is how MiRA's room-bound worker is launched locally.  The adapter
    # additionally requires LiveKit's participant kind to be AGENT for this
    # compatibility prefix.
    "simulated-agent-",
]
MCP_SERVER_NAME = "hermes-mira-context"
MCP_SERVER_ENV_VARS = (
    "MIRA_DATABASE_URL",
    "MIRA_AWARE_DATABASE_HOST",
    "MIRA_AWARE_DATABASE_PORT",
    "MIRA_AWARE_DATABASE_NAME",
    "MIRA_AWARE_DATABASE_USER",
    "MIRA_AWARE_DATABASE_PASSWORD",
)
DEFAULT_NEW_ZEALAND_VOICE = "en-NZ-MollyNeural"
STOCK_EDGE_VOICES = {"", "en-US-JennyNeural"}
LEGACY_MIRA_SYSTEM_PROMPT = (
    "You are MiRA, a warm and concise tourism companion. Respond directly and "
    "naturally. Do not announce routine internal work. Ask a brief clarifying "
    "question only when needed."
)


def _hermes_effective_cli_toolsets(
    config: dict, hermes_root: Path | None = None
) -> tuple[str, ...]:
    """Ask Hermes for effective GUI toolsets when its runtime is available."""
    inserted_path = ""
    if hermes_root is not None:
        inserted_path = str(hermes_root.resolve())
        if inserted_path not in sys.path:
            sys.path.insert(0, inserted_path)
    try:
        from hermes_cli.tools_config import _get_platform_tools

        return tuple(
            sorted(
                _get_platform_tools(
                    config, "cli", include_default_mcp_servers=False
                )
            )
        )
    except Exception as exc:
        if hermes_root is not None:
            raise RuntimeError(
                f"Could not resolve effective Hermes CLI toolsets from {hermes_root}"
            ) from exc
        return ()
    finally:
        if inserted_path and sys.path and sys.path[0] == inserted_path:
            sys.path.pop(0)


def _effective_gui_toolsets(
    config: dict, reference: list, hermes_root: Path | None = None
) -> list[str]:
    """Return the saved GUI selection plus Hermes's effective CLI additions.

    Hermes can automatically enable newly shipped toolsets even when they are
    absent from an older saved ``platform_toolsets.cli`` list. Querying its
    resolver during host setup prevents LiveKit from silently missing those
    capabilities. The import is optional so this file remains independently
    testable and usable before Hermes is installed.
    """
    names = [
        name.strip()
        for name in reference
        if isinstance(name, str) and name.strip() and name.strip() != "no_mcp"
    ]
    names.extend(
        name.strip()
        for name in _hermes_effective_cli_toolsets(config, hermes_root)
        if isinstance(name, str) and name.strip() and name.strip() != "no_mcp"
    )
    return list(dict.fromkeys(names))


def update_config(
    config_path: Path,
    *,
    auxiliary_model: str = "",
    auxiliary_base_url: str = "",
    auxiliary_api_key: str = "",
    hermes_root: Path | None = None,
    mcp_python_exe: str = "",
) -> None:
    if config_path.exists():
        with config_path.open("r", encoding="utf-8-sig") as stream:
            config = yaml.safe_load(stream) or {}
    else:
        config = {}
    if not isinstance(config, dict):
        raise ValueError("Hermes config root must be a YAML mapping")

    platforms = config.setdefault("platforms", {})
    if not isinstance(platforms, dict):
        raise ValueError("Hermes platforms config must be a YAML mapping")
    livekit = platforms.setdefault("livekit", {})
    if not isinstance(livekit, dict):
        raise ValueError("Hermes LiveKit config must be a YAML mapping")
    extra = livekit.setdefault("extra", {})
    if not isinstance(extra, dict):
        raise ValueError("Hermes LiveKit extra config must be a YAML mapping")
    extra["audio"] = {
        "silence_threshold_seconds": 0.7,
        "min_speech_duration_seconds": 0.3,
        "rms_silence_floor": 50,
    }
    extra["vision"] = {
        "auto_attach": True,
        "sample_interval_seconds": 1.0,
        "frame_max_age_seconds": 10,
        "image_stream_topics": ["test", "hermes-image"],
    }
    extra["acknowledgements"] = {"enabled": False}
    extra["invocation"] = {
        "enabled": True,
        "keyterms": list(INVOCATION_KEYTERMS),
        "strip_keyterm": True,
        "standalone_followup_seconds": 5.0,
    }
    extra["transcription"] = {
        "history_max_entries": 80,
        "history_max_chars": 12000,
        "prompt_max_entries": 12,
        "prompt_max_chars": 3000,
    }
    extra["remote_tools"] = {
        "allowed_names": list(REMOTE_TOOL_NAMES),
        "allowed_owner_prefixes": list(REMOTE_TOOL_OWNER_PREFIXES),
    }

    # Register the itinerary/location/meeting-transcript MCP server. Only
    # touched when the caller resolved a python interpreter for it (Setup-
    # HermesLiveKit.ps1 passes --mcp-python-exe when services/hermes-mcp has
    # a venv) so a host without that service installed keeps whatever
    # mcp_servers it already had — "keep the current config" applies here.
    if mcp_python_exe.strip():
        mcp_servers = config.setdefault("mcp_servers", {})
        if not isinstance(mcp_servers, dict):
            raise ValueError("Hermes mcp_servers config must be a YAML mapping")
        mcp_servers[MCP_SERVER_NAME] = {
            "command": mcp_python_exe.strip(),
            "args": ["-m", "hermes_mcp.server"],
            "env": {name: f"${{{name}}}" for name in MCP_SERVER_ENV_VARS},
            "enabled": True,
        }

    # Treat LiveKit and Discord as ordinary Hermes conversation surfaces. Copy
    # the GUI/CLI selection, retain any explicitly selected MCP server names,
    # and add only MiRA's domain bridge. Never add the ``no_mcp`` sentinel.
    platform_toolsets = config.setdefault("platform_toolsets", {})
    if not isinstance(platform_toolsets, dict):
        raise ValueError("Hermes platform_toolsets config must be a YAML mapping")
    cli_configured = isinstance(platform_toolsets.get("cli"), list)
    reference = platform_toolsets.get("cli")
    if not isinstance(reference, list) or not reference:
        reference = platform_toolsets.get("discord")
    if not isinstance(reference, list) or not reference:
        reference = DEFAULT_HERMES_CONVERSATION_TOOLSETS
    conversation_toolsets = _effective_gui_toolsets(
        config, reference, hermes_root
    )
    if "hermes-livekit" not in conversation_toolsets:
        conversation_toolsets.append("hermes-livekit")
    if cli_configured:
        # Persist any resolver-added GUI toolsets so parity is explicit and
        # stable across future Hermes versions and setup reruns.
        platform_toolsets["cli"] = list(conversation_toolsets)
    platform_toolsets["livekit"] = list(conversation_toolsets)
    discord_configured = isinstance(platform_toolsets.get("discord"), list)
    raw_discord = platforms.get("discord")
    if isinstance(raw_discord, dict) and raw_discord.get("enabled") is True:
        discord_configured = True
    if discord_configured:
        platform_toolsets["discord"] = list(conversation_toolsets)

    # Keep the Spark-hosted model as the sole foreground conversational model.
    # The optional local model handles explicit delegation plus background
    # title/compression work, preventing those auxiliary requests from
    # contending with a realtime Spark turn.
    if auxiliary_model.strip() and auxiliary_base_url.strip():
        auxiliary_endpoint = auxiliary_base_url.strip().rstrip("/")
        auxiliary_key = auxiliary_api_key or "lm-studio"
        delegation = config.setdefault("delegation", {})
        if not isinstance(delegation, dict):
            raise ValueError("Hermes delegation config must be a YAML mapping")
        delegation.update(
            {
                "model": auxiliary_model.strip(),
                "provider": "custom",
                "base_url": auxiliary_endpoint,
                "api_key": auxiliary_key,
                "api_mode": "chat_completions",
            }
        )
        auxiliary = config.setdefault("auxiliary", {})
        if not isinstance(auxiliary, dict):
            raise ValueError("Hermes auxiliary config must be a YAML mapping")
        for task_name in ("title_generation", "compression"):
            task = auxiliary.setdefault(task_name, {})
            if not isinstance(task, dict):
                raise ValueError(
                    f"Hermes auxiliary.{task_name} config must be a YAML mapping"
                )
            task.update(
                {
                    "model": auxiliary_model.strip(),
                    "provider": "custom",
                    "base_url": auxiliary_endpoint,
                    "api_key": auxiliary_key,
                }
            )

    # SOUL.md is the primary identity. Remove only the previous MiRA setup's
    # known overlay; preserve any operator-authored system prompt.
    agent = config.get("agent")
    if isinstance(agent, dict) and agent.get("system_prompt") == LEGACY_MIRA_SYSTEM_PROMPT:
        agent["system_prompt"] = ""

    # Prefer a New Zealand English voice when the host is new or still uses
    # Hermes's stock US Edge voice. Never replace a custom provider or voice.
    tts = config.setdefault("tts", {})
    if not isinstance(tts, dict):
        raise ValueError("Hermes TTS config must be a YAML mapping")
    provider = str(tts.get("provider") or "edge")
    tts.setdefault("provider", provider)
    if provider == "edge":
        edge = tts.setdefault("edge", {})
        if not isinstance(edge, dict):
            raise ValueError("Hermes Edge TTS config must be a YAML mapping")
        if str(edge.get("voice") or "") in STOCK_EDGE_VOICES:
            edge["voice"] = DEFAULT_NEW_ZEALAND_VOICE

    config_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{config_path.name}.", suffix=".tmp", dir=config_path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            yaml.safe_dump(config, stream, sort_keys=False, allow_unicode=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, config_path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--auxiliary-model", default="")
    parser.add_argument("--auxiliary-base-url", default="")
    parser.add_argument("--auxiliary-api-key", default="")
    parser.add_argument("--hermes-root", type=Path)
    parser.add_argument("--mcp-python-exe", default="")
    args = parser.parse_args()
    update_config(
        args.config,
        auxiliary_model=args.auxiliary_model,
        auxiliary_base_url=args.auxiliary_base_url,
        auxiliary_api_key=args.auxiliary_api_key,
        hermes_root=args.hermes_root,
        mcp_python_exe=args.mcp_python_exe,
    )
    print(f"Updated Hermes LiveKit YAML behavior: {args.config}")


if __name__ == "__main__":
    main()
