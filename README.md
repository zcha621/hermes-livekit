# Hermes LiveKit plugin

This submodule connects Hermes to a LiveKit room as a voice-first participant.
It supports audio, typed messages, camera/screen context, interruption, and
client-provided tools without modifying Hermes core.

Hermes is configured on its host through `config.yaml`. There is no HTTP
control bridge or web-portal control page.

MiRA's conversational setup has three deliberate layers: `assets/SOUL.md`
defines identity and voice, the bundled `mira-new-zealand-tourism` skill defines
tourism procedure and grounding, and the trusted Python worker supplies the one
implemented domain tool. LiveKit receives no Hermes core tools and explicitly
opts out of global MCP servers. The complete operator guide is
[`docs/deployment/HERMES-SETUP.md`](../../docs/deployment/HERMES-SETUP.md).

## Invocation keyterms and shared meeting context

MiRA listens for speech activity but does not send ordinary room conversation
to Hermes until someone says a configured invocation keyterm. The defaults are
`Hermes` and `MiRA`. A phrase such as “MiRA, find an accessible Rotorua walk”
opens a room-wide conversation window and strips the invocation phrase before
the request reaches the LLM.

While the window is open:

- any participant can continue the conversation without repeating a keyterm;
- LiveKit identity and display name are attached to every turn;
- the latest substantive topic/request is retained separately for each speaker;
- short follow-ups such as “yes please” retain that speaker's prior topic; and
- speech from any participant interrupts TTS immediately and becomes the next
  Hermes turn through the gateway's `busy_input_mode: interrupt` behavior.

The window closes after 120 seconds without an accepted turn. Saying a keyterm
again opens or refreshes it. A keyterm spoken on its own opens the window but
does not create an empty LLM request.

This is application-level invocation policy built on LiveKit participant audio
and Hermes STT. LiveKit itself supplies per-participant identity, synchronized
attributes, data packets, and interruption-capable media; it does not manage
MiRA's wake phrases.

## Agent status in meeting clients

The adapter publishes LiveKit's standard `lk.agent.state` participant attribute
and mirrors richer context in an `agent:status` data envelope using schema
`mira-agent-status.v1`.

| LiveKit state | Meeting UI | Meaning |
| --- | --- | --- |
| `initializing` | Agent is starting | The adapter is joining and publishing media. |
| `idle` | Agent is ready | Say a keyterm, or continue while already invoked. |
| `listening` | Listening to _name_ | A participant is currently talking. |
| `thinking` | Working for _name_ | STT has completed and the Hermes backend is working. |
| `speaking` | Agent is speaking | TTS is playing; any participant may interrupt. |

The durable participant attributes also expose:

- `mira.agent.invoked`
- `mira.agent.active_speaker`
- `mira.agent.active_speaker_name`
- `mira.agent.topic`
- `mira.agent.keyterms`
- `mira.agent.status_schema`

The web meeting at `/meet`, the Android Compose meeting, and the classic
Android meeting all render the same status contract. Participant attributes
let late joiners receive current state; reliable status data packets provide
the matching realtime event stream.

## Natural voice acknowledgements

LiveKit follows Hermes Discord voice behavior:

- an acknowledgement is spoken only when the first tool in a turn starts;
- ordinary conversational turns receive no holding phrase;
- only one acknowledgement is used per turn, even when several tools run;
- the phrase is selected from the same five defaults as Discord; and
- acknowledgement audio is not added to the visible chat transcript.

The default end-of-utterance silence threshold is also aligned with Discord at
1.5 seconds. Barge-in still interrupts playback immediately.

## MiRA domain tools over LiveKit

A trusted room participant such as MiRA's silent Python worker can register a
bounded tool by publishing a `client:tool-register` JSON envelope on the
`hermes-control` topic. Hermes validates the name and object input schema, adds
the tool to its registry, targets `agent:tool-call` envelopes back to the owner,
and waits for a matching `client:tool-result`. Registrations and results are
control messages and do not need a fake text/content field.

Registration is fail-closed: the participant identity must begin with
`agent-mira-knowledge-worker-`, and the tool name must be exactly
`find_local_recommendations`. All other identities and names are rejected.

The MiRA worker currently includes a harmless `content` compatibility marker in
these envelopes so an installed pre-fix adapter can also route them. Updated
adapters dispatch on `type` and ignore that marker.

MiRA currently uses this protocol for `find_local_recommendations`. The worker,
not the LLM, injects the Tourism AI session ID and holds the short-lived backend
credential. Do not expose arbitrary HTTP, SQL, Cypher, shell, or filesystem
tools through this protocol.

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
- migrates Hermes configuration before applying current settings;
- installs the plugin, MiRA tourism skill, and pinned LiveKit dependencies;
- installs the canonical MiRA SOUL when the file is missing or still the
  generic Hermes starter, while preserving customized SOUL files;
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
existing `LIVEKIT_*` values unless replacements are supplied. Use
`-ReplaceSoul` only after reviewing a customized SOUL; setup saves the previous
file as `SOUL.md.mira.bak`.

## Hermes YAML

The relevant section of `%LOCALAPPDATA%\hermes\config.yaml` is:

```yaml
platform_toolsets:
  livekit:
    - hermes-livekit
    - no_mcp

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
      invocation:
        enabled: true
        keyterms:
          - Hermes
          - MiRA
        conversation_timeout_seconds: 120
        strip_keyterm: true
      remote_tools:
        allowed_names:
          - find_local_recommendations
        allowed_owner_prefixes:
          - agent-mira-knowledge-worker-

tts:
  provider: edge
  edge:
    voice: en-NZ-MollyNeural

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
LIVEKIT_ALLOW_ALL_USERS=false
```

New installs deny unpaired users by default. If every participant in an
isolated research room must be accepted, rerun setup with `-AllowAllUsers` after
reviewing the room-token and participant-admission policy. Upgrades preserve an
existing explicit `true` or `false` value.

Set `acknowledgements.enabled: false` to disable tool acknowledgements, or edit
the phrase list to match MiRA's voice. Restart the gateway after changing YAML.
The setup changes the stock US Edge voice to the New Zealand English Molly
voice, but preserves any custom voice or provider. It does not alter STT.

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
5. Before invocation, speak without a keyterm. The agent should return to ready
   without sending the transcript to Hermes.
6. Say “MiRA” plus a request as one participant, then continue as a second
   participant without repeating it. The status should show the correct names
   and latest topics.
7. Wait for the invocation timeout and confirm the UI returns to “say MiRA to
   begin”.

## Runtime files

- `adapter.py` — LiveKit transport, media, voice activity, hooks, and tools.
- `__init__.py` — Hermes plugin registration and lifecycle hooks.
- `configure_yaml.py` — atomic non-secret behavior configuration.
- `assets/SOUL.md` — canonical MiRA conversational identity.
- `skills/mira-new-zealand-tourism/SKILL.md` — grounded Aotearoa tourism procedure.
- `plugin.yaml` — plugin metadata.
- `Setup-HermesLiveKit.cmd` — double-click setup for a Hermes Windows host.
- `Setup-HermesLiveKit.ps1` — repeatable Windows install/update and migration.
- `tests/test_adapter.py` — adapter behavior tests.
