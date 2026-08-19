import os
import tempfile
import unittest
from pathlib import Path

import yaml

from configure_yaml import (
    ACKNOWLEDGEMENT_PHRASES,
    DEFAULT_NEW_ZEALAND_VOICE,
    INVOCATION_KEYTERMS,
    LEGACY_MIRA_SYSTEM_PROMPT,
    LIVEKIT_TOOLSETS,
    REMOTE_TOOL_NAMES,
    REMOTE_TOOL_OWNER_PREFIXES,
    update_config,
)


class ConfigureYamlTests(unittest.TestCase):
    def test_update_preserves_unrelated_config_and_writes_real_lists(self):
        workspace_temp = Path(__file__).resolve().parents[3] / ".tmp"
        workspace_temp.mkdir(exist_ok=True)
        descriptor, name = tempfile.mkstemp(
            prefix="hermes-livekit-config-", suffix=".yaml", dir=workspace_temp
        )
        os.close(descriptor)
        path = Path(name)
        try:
            path.write_text(
                "agent:\n  system_prompt: Keep this prompt.\nplatforms:\n  livekit:\n    enabled: true\n",
                encoding="utf-8",
            )

            update_config(path)

            config = yaml.safe_load(path.read_text(encoding="utf-8"))
            self.assertEqual(config["agent"]["system_prompt"], "Keep this prompt.")
            extra = config["platforms"]["livekit"]["extra"]
            self.assertEqual(
                extra["acknowledgements"]["phrases"], ACKNOWLEDGEMENT_PHRASES
            )
            self.assertIsInstance(extra["acknowledgements"]["phrases"], list)
            self.assertEqual(
                extra["vision"]["image_stream_topics"], ["test", "hermes-image"]
            )
            self.assertEqual(extra["invocation"]["keyterms"], INVOCATION_KEYTERMS)
            self.assertEqual(extra["invocation"]["conversation_timeout_seconds"], 120)
            self.assertEqual(extra["remote_tools"]["allowed_names"], REMOTE_TOOL_NAMES)
            self.assertEqual(
                extra["remote_tools"]["allowed_owner_prefixes"],
                REMOTE_TOOL_OWNER_PREFIXES,
            )
            self.assertEqual(config["platform_toolsets"]["livekit"], LIVEKIT_TOOLSETS)
            self.assertEqual(config["tts"]["provider"], "edge")
            self.assertEqual(
                config["tts"]["edge"]["voice"], DEFAULT_NEW_ZEALAND_VOICE
            )
        finally:
            path.unlink(missing_ok=True)

    def test_update_replaces_only_stock_voice_and_legacy_prompt(self):
        workspace_temp = Path(__file__).resolve().parents[3] / ".tmp"
        workspace_temp.mkdir(exist_ok=True)
        path = workspace_temp / "hermes-livekit-existing.yaml"
        try:
            path.write_text(
                yaml.safe_dump(
                    {
                        "agent": {"system_prompt": LEGACY_MIRA_SYSTEM_PROMPT},
                        "tts": {
                            "provider": "edge",
                            "edge": {"voice": "en-US-JennyNeural"},
                        },
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )

            update_config(path)

            config = yaml.safe_load(path.read_text(encoding="utf-8"))
            self.assertEqual(config["agent"]["system_prompt"], "")
            self.assertEqual(
                config["tts"]["edge"]["voice"], DEFAULT_NEW_ZEALAND_VOICE
            )
        finally:
            path.unlink(missing_ok=True)

    def test_update_preserves_custom_tts_provider(self):
        workspace_temp = Path(__file__).resolve().parents[3] / ".tmp"
        workspace_temp.mkdir(exist_ok=True)
        path = workspace_temp / "hermes-livekit-custom-tts.yaml"
        try:
            path.write_text(
                "tts:\n  provider: elevenlabs\n  elevenlabs:\n    voice_id: custom\n",
                encoding="utf-8",
            )

            update_config(path)

            config = yaml.safe_load(path.read_text(encoding="utf-8"))
            self.assertEqual(config["tts"]["provider"], "elevenlabs")
            self.assertEqual(config["tts"]["elevenlabs"]["voice_id"], "custom")
            self.assertNotIn("edge", config["tts"])
        finally:
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
