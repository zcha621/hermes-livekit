# Hermes LiveKit plugin

This submodule connects Hermes to a LiveKit room as a voice-first participant.
It supports audio, typed messages, camera/screen context, interruption, and
client-provided tools without modifying Hermes core.

Hermes is configured on its host through `config.yaml`. There is no HTTP
control bridge or web-portal control page.

## Natural voice acknowledgements

LiveKit follows Hermes Discord voice behavior:

- an acknowledgement is spoken only when the first tool in a turn starts;
- ordinary conversational turns receive no holding phrase;
- only one acknowledgement is used per turn, even when several tools run;
- the phrase is selected from the same five defaults as Discord; and
- acknowledgement audio is not added to the visible chat transcript.

The default end-of-utterance silence threshold is also aligned with Discord at
1.5 seconds. Barge-in still interrupts playback immediately.

## One-click setup on Windows

After Hermes is installed, open the MiRA repository and double-click:

```text
agents\hermes-livekit\Setup-HermesLiveKit.cmd
```

The launcher installs missing FFmpeg and Python dependencies, enables gateway
auto-start, applies the LiveKit configuration, validates it, and restarts the
gateway. It reuses credentials from the existing Hermes `.env` or from
`infrastructure\livekitserver-docker\.env`; if neither contains a complete
LiveKit configuration, it prompts for the three required values. The window
stays open at the end so success or an actionable error is visible.

## Scripted install or update

Run PowerShell from the MiRA repository root:

```powershell
.\agents\hermes-livekit\Setup-HermesLiveKit.ps1 `
  -LiveKitEnvFile .\infrastructure\livekitserver-docker\.env `
  -RestartGateway
```

The PowerShell script:

- keeps one rolling backup each of the existing Hermes `.env` and
  `config.yaml`;
- installs the plugin and pinned LiveKit dependencies;
- keeps transport credentials in `.env`;
- writes behavioral settings to Hermes `config.yaml`;
- keeps its temporary plugin rollback outside the discoverable plugin folder
  and removes it after validation;
- removes old `hermes-livekit.backup-*` directories that could override the
  active plugin during Hermes discovery;
- removes legacy control-bridge environment values, scheduled task, process,
  and installed bridge script; and
- optionally restarts or installs auto-start for the Hermes gateway.

Use `-InstallHermes` for a new host, `-InstallFfmpeg` if FFmpeg is missing, and
`-InstallAutoStart` to install Hermes gateway auto-start. Re-running setup reuses
existing `LIVEKIT_*` values unless replacements are supplied.

## Hermes YAML

The relevant section of `%LOCALAPPDATA%\hermes\config.yaml` is:

```yaml
platforms:
  livekit:
    enabled: true
    group_sessions_per_user: false
    extra:
      url: ${LIVEKIT_URL}
      api_key: ${LIVEKIT_API_KEY}
      api_secret: ${LIVEKIT_API_SECRET}
      room: ${LIVEKIT_ROOM}
      agent_name: ${LIVEKIT_AGENT_NAME}
      audio:
        silence_threshold_seconds: 1.5
        min_speech_duration_seconds: 0.5
        rms_silence_floor: 50
      vision:
        auto_attach: true
        sample_interval_seconds: 1.0
        frame_max_age_seconds: 10
        image_stream_topics:
          - test
          - hermes-image
      acknowledgements:
        enabled: true
        phrases:
          - Let me look into that.
          - One moment.
          - Checking on that now.
          - Give me a sec.
          - On it.

voice:
  auto_tts: true

display:
  busy_input_mode: interrupt
  platforms:
    livekit:
      streaming: false
      long_running_notifications: false
      busy_ack_detail: false
```

Only secrets and transport values belong in `%LOCALAPPDATA%\hermes\.env`:

```dotenv
LIVEKIT_URL=ws://127.0.0.1:7880
LIVEKIT_API_KEY=replace-me
LIVEKIT_API_SECRET=replace-me
LIVEKIT_ROOM=hermes
LIVEKIT_AGENT_NAME=MiRA
LIVEKIT_ALLOW_ALL_USERS=true
```

Set `acknowledgements.enabled: false` to disable tool acknowledgements, or edit
the phrase list to match MiRA's voice. Restart the gateway after changing YAML.

## Validate

```powershell
$hermes = "$env:LOCALAPPDATA\hermes\hermes-agent\venv\Scripts\hermes.exe"
& $hermes config check
& $hermes gateway status
python -m unittest discover -s .\agents\hermes-livekit\tests -v
```

A useful call check is:

1. Ask a question that needs no tool. MiRA should answer directly.
2. Ask a question that requires a tool. MiRA should speak one short cue when
   the first tool starts, then deliver the answer.
3. Ask a multi-tool question. The cue should still occur only once.
4. Speak during playback. MiRA should stop and listen.

## Runtime files

- `adapter.py` — LiveKit transport, media, voice activity, hooks, and tools.
- `__init__.py` — Hermes plugin registration and lifecycle hooks.
- `configure_yaml.py` — atomic non-secret behavior configuration.
- `plugin.yaml` — plugin metadata.
- `Setup-HermesLiveKit.cmd` — double-click setup for a Hermes Windows host.
- `Setup-HermesLiveKit.ps1` — repeatable Windows install/update and migration.
- `tests/test_adapter.py` — adapter behavior tests.
