import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
CONFIGURE_SPEC = importlib.util.spec_from_file_location(
    "hermes_livekit_configure_test", PLUGIN_ROOT / "configure_yaml.py"
)
configure_yaml = importlib.util.module_from_spec(CONFIGURE_SPEC)
assert CONFIGURE_SPEC.loader is not None
CONFIGURE_SPEC.loader.exec_module(configure_yaml)

DEFAULT_NEW_ZEALAND_VOICE = configure_yaml.DEFAULT_NEW_ZEALAND_VOICE
INVOCATION_KEYTERMS = configure_yaml.INVOCATION_KEYTERMS
LEGACY_MIRA_SYSTEM_PROMPT = configure_yaml.LEGACY_MIRA_SYSTEM_PROMPT
REMOTE_TOOL_NAMES = configure_yaml.REMOTE_TOOL_NAMES
REMOTE_TOOL_OWNER_PREFIXES = configure_yaml.REMOTE_TOOL_OWNER_PREFIXES
update_config = configure_yaml.update_config


class ConfigureYamlTests(unittest.TestCase):
    def setUp(self):
        resolver = patch.object(
            configure_yaml, "_hermes_effective_cli_toolsets", return_value=()
        )
        self.effective_cli_toolsets = resolver.start()
        self.addCleanup(resolver.stop)

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
                "agent:\n  system_prompt: Keep this prompt.\nplatforms:\n  livekit:\n    enabled: true\nplatform_toolsets:\n  cli:\n    - web\n    - skills\n    - local-mcp\n",
                encoding="utf-8",
            )

            update_config(path)

            config = yaml.safe_load(path.read_text(encoding="utf-8"))
            self.assertEqual(config["agent"]["system_prompt"], "Keep this prompt.")
            extra = config["platforms"]["livekit"]["extra"]
            self.assertEqual(extra["acknowledgements"], {"enabled": False})
            self.assertEqual(extra["audio"]["silence_threshold_seconds"], 0.7)
            self.assertEqual(extra["audio"]["min_speech_duration_seconds"], 0.3)
            self.assertEqual(
                extra["vision"]["image_stream_topics"], ["test", "hermes-image"]
            )
            self.assertEqual(extra["invocation"]["keyterms"], INVOCATION_KEYTERMS)
            self.assertEqual(
                extra["invocation"]["standalone_followup_seconds"], 5.0
            )
            self.assertNotIn("conversation_timeout_seconds", extra["invocation"])
            self.assertEqual(extra["transcription"]["history_max_entries"], 80)
            self.assertEqual(extra["transcription"]["history_max_chars"], 12000)
            self.assertEqual(extra["transcription"]["prompt_max_entries"], 12)
            self.assertEqual(extra["transcription"]["prompt_max_chars"], 3000)
            self.assertEqual(extra["remote_tools"]["allowed_names"], REMOTE_TOOL_NAMES)
            self.assertEqual(
                extra["remote_tools"]["allowed_owner_prefixes"],
                REMOTE_TOOL_OWNER_PREFIXES,
            )
            self.assertEqual(
                config["platform_toolsets"]["livekit"],
                ["web", "skills", "local-mcp", "hermes-livekit"],
            )
            self.assertIn("skills", config["platform_toolsets"]["livekit"])
            self.assertIn("web", config["platform_toolsets"]["livekit"])
            self.assertNotIn("no_mcp", config["platform_toolsets"]["livekit"])
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

    def test_update_exposes_mira_toolset_on_configured_gateways(self):
        workspace_temp = Path(__file__).resolve().parents[3] / ".tmp"
        workspace_temp.mkdir(exist_ok=True)
        path = workspace_temp / "hermes-livekit-channel-toolsets.yaml"
        try:
            path.write_text(
                yaml.safe_dump(
                    {
                        "platform_toolsets": {
                            "cli": ["browser", "web", "my-mcp"],
                            "discord": ["browser", "no_mcp"],
                            "telegram": ["web"],
                        }
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )

            update_config(path)

            config = yaml.safe_load(path.read_text(encoding="utf-8"))
            self.assertEqual(
                config["platform_toolsets"]["discord"],
                ["browser", "web", "my-mcp", "hermes-livekit"],
            )
            self.assertEqual(
                config["platform_toolsets"]["telegram"],
                ["web"],
            )
            self.assertEqual(
                config["platform_toolsets"]["livekit"].count("hermes-livekit"),
                1,
            )
        finally:
            path.unlink(missing_ok=True)

    def test_update_includes_effective_cli_additions_from_hermes(self):
        workspace_temp = Path(__file__).resolve().parents[3] / ".tmp"
        workspace_temp.mkdir(exist_ok=True)
        path = workspace_temp / "hermes-livekit-effective-toolsets.yaml"
        self.effective_cli_toolsets.return_value = (
            "bfl",
            "browser",
            "hermes-livekit",
        )
        try:
            path.write_text(
                yaml.safe_dump(
                    {
                        "platform_toolsets": {
                            "cli": ["browser", "hermes-livekit"],
                            "discord": ["browser", "hermes-livekit"],
                            "livekit": ["browser", "hermes-livekit", "no_mcp"],
                        }
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )

            update_config(path)

            config = yaml.safe_load(path.read_text(encoding="utf-8"))
            expected = ["browser", "hermes-livekit", "bfl"]
            self.assertEqual(config["platform_toolsets"]["cli"], expected)
            self.assertEqual(config["platform_toolsets"]["livekit"], expected)
            self.assertEqual(config["platform_toolsets"]["discord"], expected)
            self.assertNotIn("no_mcp", config["platform_toolsets"]["livekit"])
        finally:
            path.unlink(missing_ok=True)

    def test_optional_auxiliary_model_keeps_spark_foreground_only(self):
        workspace_temp = Path(__file__).resolve().parents[3] / ".tmp"
        workspace_temp.mkdir(exist_ok=True)
        path = workspace_temp / "hermes-livekit-auxiliary.yaml"
        try:
            path.write_text(
                yaml.safe_dump(
                    {
                        "model": {
                            "provider": "custom:custom-spark2",
                            "default": "OptimizeLLM/Qwen3-VL-30B-A3B-Thinking-NVFP4",
                        },
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )

            update_config(
                path,
                auxiliary_model="qwythos-9b-claude-mythos-5-1m",
                auxiliary_base_url="http://127.0.0.1:1234/v1/",
                auxiliary_api_key="local-key",
            )

            config = yaml.safe_load(path.read_text(encoding="utf-8"))
            self.assertEqual(config["model"]["provider"], "custom:custom-spark2")
            self.assertEqual(
                config["delegation"],
                {
                    "model": "qwythos-9b-claude-mythos-5-1m",
                    "provider": "custom",
                    "base_url": "http://127.0.0.1:1234/v1",
                    "api_key": "local-key",
                    "api_mode": "chat_completions",
                },
            )
            expected_auxiliary = {
                "model": "qwythos-9b-claude-mythos-5-1m",
                "provider": "custom",
                "base_url": "http://127.0.0.1:1234/v1",
                "api_key": "local-key",
            }
            self.assertEqual(
                config["auxiliary"]["title_generation"], expected_auxiliary
            )
            self.assertEqual(
                config["auxiliary"]["compression"], expected_auxiliary
            )
        finally:
            path.unlink(missing_ok=True)

    def test_mcp_server_not_registered_without_python_exe(self):
        workspace_temp = Path(__file__).resolve().parents[3] / ".tmp"
        workspace_temp.mkdir(exist_ok=True)
        path = workspace_temp / "hermes-livekit-no-mcp.yaml"
        try:
            path.write_text(
                yaml.safe_dump({"mcp_servers": {"other": {"url": "https://x"}}}),
                encoding="utf-8",
            )

            update_config(path)

            config = yaml.safe_load(path.read_text(encoding="utf-8"))
            # "Keep the current config": an unrelated existing MCP server is
            # untouched, and hermes-mira-context is not added when no python
            # interpreter for it was resolved.
            self.assertEqual(config["mcp_servers"], {"other": {"url": "https://x"}})
        finally:
            path.unlink(missing_ok=True)

    def test_mcp_server_registered_when_python_exe_given(self):
        workspace_temp = Path(__file__).resolve().parents[3] / ".tmp"
        workspace_temp.mkdir(exist_ok=True)
        path = workspace_temp / "hermes-livekit-mcp.yaml"
        try:
            path.write_text(
                yaml.safe_dump({"mcp_servers": {"other": {"url": "https://x"}}}),
                encoding="utf-8",
            )

            update_config(
                path,
                mcp_python_exe=r"C:\services\hermes-mcp\.venv\Scripts\python.exe",
            )

            config = yaml.safe_load(path.read_text(encoding="utf-8"))
            self.assertEqual(
                config["mcp_servers"]["other"], {"url": "https://x"}
            )
            mira_context = config["mcp_servers"]["hermes-mira-context"]
            self.assertEqual(
                mira_context["command"],
                r"C:\services\hermes-mcp\.venv\Scripts\python.exe",
            )
            self.assertEqual(mira_context["args"], ["-m", "hermes_mcp.server"])
            self.assertEqual(mira_context["env"]["MIRA_DATABASE_URL"], "${MIRA_DATABASE_URL}")
            self.assertTrue(mira_context["enabled"])
        finally:
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
