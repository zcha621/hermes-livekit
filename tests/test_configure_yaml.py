import os
import tempfile
import unittest
from pathlib import Path

import yaml

from configure_yaml import ACKNOWLEDGEMENT_PHRASES, update_config


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
        finally:
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
