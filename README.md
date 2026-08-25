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

MiRA transcribes every completed room utterance but creates a Hermes response
turn only when that same utterance contains a configured invocation keyterm.
The defaults are `Hermes` and `MiRA`. A phrase such as “MiRA, find an accessible
Rotorua walk” is logged with its speaker, then the invocation phrase is stripped
before the request reaches the LLM. The next utterance requires its own keyterm;
there is no room-wide follow-up window.

The adapter keeps a bounded chronological transcript containing every
participant and Hermes's own speech. It publishes finalized segments both as
participant-labeled `agent:*transcript` data events and, for speech, through
LiveKit's native transcription API. Ambient conversation is not discarded: the
recent transcript is included as quoted context whenever a participant invokes
MiRA, so the reply can understand references without answering uninvoked speech.
A standalone keyterm is logged but does not arm the next utterance.

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
| `idle` | Agent is ready | Include a keyterm in each request to MiRA. |
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

## Interruption and audio processing

Meeting microphones remain published during Hermes playback. Web and Android
enable WebRTC echo cancellation, noise suppression, and automatic gain control;
the Android client additionally enables its high-pass and typing-noise filters.
The adapter detects inbound speech independently of its outbound track and
flushes queued TTS audio on voice barge-in.

The meeting clients also expose **Stop agent** while the state is `thinking` or
`speaking`. The control is independent of microphone mute and uses a reliable
packet on the `hermes-control` topic:

```json
{
  "type": "client:control",
  "action": "interrupt",
  "reason": "user-request",
  "request_id": "client-generated-id"
}
```

Hermes immediately clears queued audio, publishes `agent:interrupted`, and
dispatches its canonical `/stop` command. The gateway cancels in-flight model,
tool, and child-agent work regardless of the configured busy-input mode.

## Natural voice acknowledgements

LiveKit follows Hermes Discord voice behavior:

- an acknowledgement is spoken only when the first tool in a turn starts;
- ordinary conversational turns receive no holding phrase;
- only one acknowledgement is used per turn, even when several tools run;
- the phrase is selected from the same five defaults as Discord; and
- acknowledgement audio is labeled and added to the room transcript.

The default end-of-utterance silence threshold is also aligned with Discord at
1.5 seconds. Barge-in still interrupts playback immediately.

## MiRA domain tools over LiveKit

A trusted room participant such as MiRA's silent Python worker can register a
bounded tool by publishing a `client:tool-register` JSON envelope on the
`hermes-control` topic. Hermes validates the name and object input schema, adds
the tool to its registry, targets `agent:tool-call` envelopes back to the owner,
and waits for a matching `client:tool-result`. Registrations and results are
control messages and do not need a fake text/content field.

Registration is fail-closed: the production participant identity must begin
with `agent-mira-knowledge-worker-`. LiveKit Agents 1.2.x local
`connect --room` workers use `simulated-agent-`; that compatibility identity is
accepted only when LiveKit marks the participant kind as `AGENT`. The tool name
must be `find_local_recommendations`, `get_current_trip_context`, or
`manage_trip_itinerary`. All other identities and names are rejected.

The MiRA worker currently includes a harmless `content` compatibility marker in
these envelopes so an installed pre-fix adapter can also route them. Updated
adapters dispatch on `type` and ignore that marker.

MiRA uses this protocol for grounded recommendation retrieval and for a bounded
current-context read. The latter returns current server time, an optional saved
itinerary, latest consented Android context, and recent participant-labelled
transcripts. The worker, not the LLM, injects the Tourism AI session ID and
holds the short-lived backend credential. Do not expose arbitrary HTTP, SQL,
Cypher, shell, or filesystem tools through this protocol.

`manage_trip_itinerary` is the account-planning route. Hermes receives trusted
gateway platform/user/session context from the adapter, never an account ID
from the model. `revise` stores an editable draft; `confirm` requires the exact
current revision and explicit traveller approval before the backend promotes it
to the confirmed itinerary. The plugin loads the linked workspace before each
turn and records the completed user/assistant turn afterward, allowing the
draft, confirmed plan, and recent conversation to continue across Hermes
sessions and explicitly configured gateway channels. An unlinked channel can
be attached with the portal's 15-minute one-time code.

The `/trips` text box is a dedicated chat surface: pressing **Send** is an
explicit Hermes invocation and does not require the `MiRA` voice wake term.
The wake term still applies to ambient speech in a live room. The plugin
declares `manage_trip_itinerary` in `plugin.yaml` and pre-registers it from
`tools.py`, so Discord and other configured Hermes channels can discover the
same tool even while the LiveKit adapter itself is deferred.

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

## Evidence routes

LiveKit sessions expose three preferred tourism evidence routes:

- `get_current_trip_context` - current time, optional itinerary, consented
  location/device/social context, and recent room conversation;

- `find_local_recommendations` — the authenticated MiRA graph/RAG route for
  curated, session-scoped tourism evidence;
- `manage_trip_itinerary` - account linking plus conversational draft revision
  and explicit confirmation. A draft is not a saved itinerary;
- `web_search` — Hermes's read-only online-search fallback for fresh facts or
  locations not covered by the pilot graph.

LiveKit also receives the same normal Hermes task and skill toolsets as Discord,
so a spoken request can be completed rather than acknowledged and abandoned.
Setup also adds the narrow `hermes-livekit` toolset to every explicitly
configured Hermes gateway surface, allowing linked Discord, Telegram, or other
channels to continue account planning while the trusted LiveKit worker remains
connected.
`no_mcp` remains explicit, preventing globally enabled MCP servers from
silently entering a room. The tourism skill tells Hermes when to prefer the
graph and when to fall back to online search, and requires source URLs for
web-derived claims.

## Hermes YAML

The relevant section of `%LOCALAPPDATA%\hermes\config.yaml` is:

```yaml
platform_toolsets:
  livekit:
    - hermes-livekit
    - browser
    - clarify
    - code_execution
    - computer_use
    - cronjob
    - delegation
    - file
    - image_gen
    - memory
    - session_search
    - skills
    - terminal
    - todo
    - tts
    - vision
    - web
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
        strip_keyterm: true
      transcription:
        history_max_entries: 80
        history_max_chars: 12000
      remote_tools:
        allowed_names:
          - find_local_recommendations
          - get_current_trip_context
          - manage_trip_itinerary
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
& $hermes tools list --platform discord
python -m unittest discover -s .\agents\hermes-livekit\tests -v
```

The Discord tool listing must show the `hermes-livekit` toolset as enabled;
`plugin.yaml` declares its `manage_trip_itinerary` tool. To exercise the same
typed-data path used by `/trips` against a running local room:

```powershell
$python = "$env:LOCALAPPDATA\hermes\hermes-agent\venv\Scripts\python.exe"
& $python .\agents\python-agent\scripts\verify_livekit_chat.py `
  --env-file "$env:LOCALAPPDATA\hermes\.env" `
  --room ECL `
  --message "Plan a relaxed one-day visit to Christchurch"
```

The command prints the Hermes reply as JSON. A transcript entry with no
subsequent Hermes turn usually means the installed adapter predates explicit
typed-chat invocation handling; rerun the setup command and restart the
gateway.

A useful call check is:

1. Ask a question that needs no tool. MiRA should answer directly.
2. Ask a question that requires a tool. MiRA should speak one short cue when
   the first tool starts, then deliver the answer.
3. Ask a multi-tool question. The cue should still occur only once.
4. Speak during playback. MiRA should stop and listen.
5. While MiRA is working or speaking, use **Stop agent** and confirm playback
   stops immediately. Repeat with the microphone muted.
6. Speak without a keyterm. The utterance should appear in the labeled room
   transcript, but no Hermes response turn should be created.
7. Say “MiRA” plus a request. MiRA should answer using relevant context from the
   preceding ambient transcript.
8. Speak again without a keyterm. MiRA should remain quiet; repeat with a
   keyterm and confirm the correct participant name appears in context.

## Runtime files

- `adapter.py` — LiveKit transport, media, voice activity, hooks, and tools.
- `__init__.py` — Hermes plugin registration and lifecycle hooks.
- `tools.py` — discovery-time cross-platform itinerary tool registration.
- `configure_yaml.py` — atomic non-secret behavior configuration.
- `assets/SOUL.md` — canonical MiRA conversational identity.
- `skills/mira-new-zealand-tourism/SKILL.md` — grounded Aotearoa tourism procedure.
- `plugin.yaml` — plugin metadata.
- `Setup-HermesLiveKit.cmd` — double-click setup for a Hermes Windows host.
- `Setup-HermesLiveKit.ps1` — repeatable Windows install/update and migration.
- `tests/test_adapter.py` — adapter behavior tests.
