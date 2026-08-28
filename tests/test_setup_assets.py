import unittest
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]


class SetupAssetTests(unittest.TestCase):
    def test_double_click_launcher_runs_full_host_setup(self):
        launcher = (PLUGIN_ROOT / "Setup-HermesLiveKit.cmd").read_text(
            encoding="utf-8"
        )

        self.assertIn("Setup-HermesLiveKit.ps1", launcher)
        self.assertIn("-InstallFfmpeg", launcher)
        self.assertIn("-InstallAutoStart", launcher)
        self.assertIn("-RestartGateway", launcher)

    def test_setup_never_leaves_discoverable_plugin_backups(self):
        setup = (PLUGIN_ROOT / "Setup-HermesLiveKit.ps1").read_text(
            encoding="utf-8-sig"
        )

        self.assertIn("[System.IO.Path]::GetTempPath()", setup)
        self.assertIn('$_.Name -like "hermes-livekit.backup-*"', setup)
        self.assertNotIn('$targetPlugin.backup-', setup)
        self.assertIn(
            "Remove-Item -LiteralPath $targetPlugin -Recurse -Force", setup
        )

    def test_setup_deletes_legacy_control_bridge_artifacts(self):
        setup = (PLUGIN_ROOT / "Setup-HermesLiveKit.ps1").read_text(
            encoding="utf-8-sig"
        )

        self.assertIn('$_.Name -like "control_bridge.py.retired-*"', setup)
        self.assertNotIn("Move-Item -LiteralPath $bridgePath", setup)

    def test_setup_does_not_force_allow_all_users_on_new_installs(self):
        setup = (PLUGIN_ROOT / "Setup-HermesLiveKit.ps1").read_text(
            encoding="utf-8-sig"
        )

        self.assertIn("[switch]$AllowAllUsers", setup)
        self.assertIn('$allowAllUsersValue = "false"', setup)
        self.assertNotIn(
            'Set-DotEnvValue $envPath "LIVEKIT_ALLOW_ALL_USERS" "true"',
            setup,
        )

    def test_readme_leads_with_one_click_setup(self):
        readme = (PLUGIN_ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn("## One-click setup on Windows", readme)
        self.assertIn("Setup-HermesLiveKit.cmd", readme)

    def test_setup_installs_identity_skill_and_task_capable_tool_surface(self):
        setup = (PLUGIN_ROOT / "Setup-HermesLiveKit.ps1").read_text(
            encoding="utf-8-sig"
        )
        soul = PLUGIN_ROOT / "assets" / "SOUL.md"
        skill = PLUGIN_ROOT / "skills" / "mira-new-zealand-tourism" / "SKILL.md"

        self.assertTrue(soul.is_file())
        self.assertTrue(skill.is_file())
        self.assertIn("[switch]$ReplaceSoul", setup)
        self.assertIn("Install-MiraSoul", setup)
        self.assertIn('Join-Path $sourcePlugin "skills"', setup)
        self.assertIn('"adapter.py", "__init__.py", "transcript_store.py"', setup)

        configure = (PLUGIN_ROOT / "configure_yaml.py").read_text(encoding="utf-8")
        self.assertIn('"hermes-livekit",', configure)
        self.assertIn('"skills",', configure)
        self.assertIn('"terminal",', configure)
        self.assertIn('"web",', configure)
        self.assertIn('name.strip() != "no_mcp"', configure)
        self.assertIn("plugins doctor --ci", setup)
        self.assertIn('"--hermes-root"', setup)
        self.assertIn("[switch]$ConfigureAuxiliaryModel", setup)
        self.assertIn('"--auxiliary-model"', setup)
        # Itinerary/location/transcript context is served by the
        # hermes-mira-context MCP server, registered when the setup script
        # resolves a python interpreter for services/hermes-mcp.
        self.assertIn("MCP_SERVER_NAME = \"hermes-mira-context\"", configure)
        self.assertIn('"--mcp-python-exe"', setup)
        self.assertIn(
            "plugins enable hermes-livekit --no-allow-tool-override", setup
        )
        self.assertNotIn(
            "config set platforms.livekit.extra.agent_name", setup
        )


if __name__ == "__main__":
    unittest.main()
