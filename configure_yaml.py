"""Apply non-secret Hermes LiveKit behavior defaults to config.yaml."""

from __future__ import annotations

import argparse
import os
import tempfile
from pathlib import Path

import yaml


ACKNOWLEDGEMENT_PHRASES = [
    "Let me look into that.",
    "One moment.",
    "Checking on that now.",
    "Give me a sec.",
    "On it.",
]
INVOCATION_KEYTERMS = ["Hermes", "MiRA"]
LIVEKIT_TOOLSETS = ["hermes-livekit", "no_mcp"]
REMOTE_TOOL_NAMES = ["find_local_recommendations"]
REMOTE_TOOL_OWNER_PREFIXES = ["agent-mira-knowledge-worker-"]
DEFAULT_NEW_ZEALAND_VOICE = "en-NZ-MollyNeural"
STOCK_EDGE_VOICES = {"", "en-US-JennyNeural"}
LEGACY_MIRA_SYSTEM_PROMPT = (
    "You are MiRA, a warm and concise tourism companion. Respond directly and "
    "naturally. Do not announce routine internal work. Ask a brief clarifying "
    "question only when needed."
)


def update_config(config_path: Path) -> None:
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
        "silence_threshold_seconds": 1.5,
        "min_speech_duration_seconds": 0.5,
        "rms_silence_floor": 50,
    }
    extra["vision"] = {
        "auto_attach": True,
        "sample_interval_seconds": 1.0,
        "frame_max_age_seconds": 10,
        "image_stream_topics": ["test", "hermes-image"],
    }
    extra["acknowledgements"] = {
        "enabled": True,
        "phrases": list(ACKNOWLEDGEMENT_PHRASES),
    }
    extra["invocation"] = {
        "enabled": True,
        "keyterms": list(INVOCATION_KEYTERMS),
        "conversation_timeout_seconds": 120,
        "strip_keyterm": True,
    }
    extra["remote_tools"] = {
        "allowed_names": list(REMOTE_TOOL_NAMES),
        "allowed_owner_prefixes": list(REMOTE_TOOL_OWNER_PREFIXES),
    }

    # Hermes otherwise gives an unknown plugin platform its broad core tool
    # bundle and automatically adds every enabled MCP server. MiRA's voice
    # surface gets only trusted LiveKit worker tools and explicitly opts out of
    # global MCP inheritance.
    platform_toolsets = config.setdefault("platform_toolsets", {})
    if not isinstance(platform_toolsets, dict):
        raise ValueError("Hermes platform_toolsets config must be a YAML mapping")
    platform_toolsets["livekit"] = list(LIVEKIT_TOOLSETS)

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
    args = parser.parse_args()
    update_config(args.config)
    print(f"Updated Hermes LiveKit YAML behavior: {args.config}")


if __name__ == "__main__":
    main()
