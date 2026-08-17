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


def update_config(config_path: Path) -> None:
    if config_path.exists():
        with config_path.open("r", encoding="utf-8-sig") as stream:
            config = yaml.safe_load(stream) or {}
    else:
        config = {}
    if not isinstance(config, dict):
        raise ValueError("Hermes config root must be a YAML mapping")

    platforms = config.setdefault("platforms", {})
    livekit = platforms.setdefault("livekit", {})
    extra = livekit.setdefault("extra", {})
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
