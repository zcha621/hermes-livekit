# Hermes LiveKit plugin

This submodule connects Hermes to a LiveKit room as a voice-first participant.
It supports audio, typed messages, camera/screen context, interruption, and
client-provided tools without modifying Hermes core.

Hermes is configured on its host through `config.yaml`; there is no HTTP
control bridge. MiRA's administrator portal stores pending LiveKit agent-name,
invocation-keyterm, and SOUL revisions in its database. Explicit short-lived
deployment tools import or apply only `config.yaml` and `SOUL.md`; the public
portal receives no Hermes host mount. Gateway lifecycle remains host-managed.

MiRA's conversational setup has three deliberate layers: `assets/SOUL.md`
defines identity and voice, the bundled `mira-new-zealand-tourism` skill is an
optional Hermes skill, and the trusted Python worker supplies bounded domain
tools. LiveKit and Discord receive the same configured Hermes tools, skills,
and MCP servers as the GUI/CLI, plus the MiRA tools. The plugin does not call
the backend before or after ordinary model turns. The complete operator guide is
[`docs/deployment/HERMES-SETUP.md`](../../docs/deployment/HERMES-SETUP.md).

## Invocation keyterms and shared meeting context

MiRA transcribes every completed room utterance but normally creates a Hermes
response turn only when that utterance contains a configured invocation keyterm.
The defaults are `Hermes` and `MiRA`. A phrase such as “MiRA, find an accessible
Rotorua walk” is logged with its speaker, then the invocation phrase is stripped
before the request reaches the LLM. Mobile endpointing can finalize a standalone
wake phrase before the question; in that one case, only the same participant's
next utterance is accepted for five seconds. There is no room-wide follow-up
window.

The Android Compose client and web meeting also expose a press-and-hold
`@Agent` button. Press publishes `push-to-talk-start`; release publishes the
matching `push-to-talk-end` on the reliable `hermes-control` topic. While held,
internal pauses do not endpoint the request. Release finalizes that complete
audio buffer and applies the same invocation gate as a spoken keyterm. The
adapter ignores any claimed identity in the JSON and binds the turn to the
authenticated LiveKit packet sender, so the speaker name, transcript, history,
and tool context all belong to the participant who held the button. If that
participant was muted, the clients enable their microphone only for the held
turn and then restore its previous state.

The adapter keeps a bounded chronological transcript containing every
participant and Hermes's own speech. It publishes finalized segments both as
participant-labeled `agent:*transcript` data events and, for speech, through
LiveKit's native transcription API. Ambient conversation is not discarded: the
recent transcript is included as quoted context whenever a participant invokes
MiRA, so the reply can understand references without answering uninvoked speech.
A standalone keyterm does not create an empty model turn.

Portal clients should set `mira_conversation_id`; Hermes then keeps history for
that explicit conversation. A participant without that metadata receives a
fresh call-scoped ID for each connection instead of inheriting permanent room
history. The bounded participant transcript above still supplies current
multi-speaker context without allowing old calls to grow every new prompt.

When location, local time, itinerary, or earlier meeting speech matters,
Hermes can select one of the hermes-mira-context MCP server's tools (see
[MiRA domain context via MCP](#mira-domain-context-via-mcp) below). No
database snapshot is fetched or injected automatically. This keeps direct
conversation on the same model path as the Hermes GUI while leaving current
context available on demand.

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
| `idle` | Agent is ready | Include a keyterm or hold `@Agent` for each request to MiRA. |
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

## Low-latency voice path

Tool acknowledgements are disabled and the plugin registers no pre-tool cue
hook, so a selected tool starts without an extra TTS request. Ordinary and
tool-using turns both stay in Hermes's normal response loop.

The realtime Spark Qwen request middleware sends its native
`extra_body.enable_thinking: false` flag and a final `/no_think` marker on the
provider payload without changing the stored participant transcript. If its
OpenAI-compatible endpoint returns the model-selected call as a
`<tool_call>` JSON block instead of the standard `tool_calls` field, a scoped
post-response compatibility hook converts that existing decision to Hermes's
canonical tool-call type. The hook does not select or execute a tool; Hermes's
registry, approval, and dispatch boundaries remain unchanged.

LiveKit's TTS boundary independently removes `<think>` blocks. If Hermes
exhausts its reasoning-only retries and returns a visual diagnostic containing
a labeled reasoning excerpt, the data-channel text remains available to the
UI, but voice playback substitutes a short retry message and never synthesizes
the excerpt.

The default end-of-utterance silence threshold is 0.7 seconds and the minimum
speech duration is 0.3 seconds. Barge-in still interrupts playback immediately.

## MiRA domain context via MCP

MiRA's itinerary, location, and meeting-transcript context is served by a
separate MCP (Model Context Protocol) server, `services/hermes-mcp`, rather
than by tools this plugin registers. Hermes connects to it like any other MCP
server (`mcp_servers.hermes-mira-context` in `config.yaml`, written by
`configure_yaml.py` when `Setup-HermesLiveKit.ps1` resolves a python
interpreter for it — see that service's README for setup). Once connected, its
tools are available on every platform Hermes is configured for, LiveKit
included, alongside Hermes's normal tools, skills, and other MCP servers —
Hermes decides whether and when to call them, same as any other tool.

The server exposes four tools:

- `get_confirmed_itinerary(mira_account_id)` — the traveller's saved
  itinerary, read from the tourism-ai-backend's `mira_account_itinerary`
  table.
- `get_traveller_location(aware_device_id)` — the traveller's latest GPS fix,
  timezone, and computed local time, read from the AWARE `locations` /
  `timezone` tables the Android client streams into.
- `get_meeting_transcript(room_name)` — earlier speech from the current
  LiveKit room, read from `mira_transcript_segment` (written by this
  plugin's `transcript_store.py` as the adapter finalizes each utterance).
- `manage_trip_itinerary(action, platform, user_id, hermes_session_id, ...)` —
  load, link, revise, or confirm the traveller's account-wide itinerary,
  proxied to the tourism-ai-backend's `POST /gateway/planning-workspace`.
  `revise` stores an editable draft; `confirm` requires the exact current
  revision and explicit traveller approval before the backend promotes it to
  the confirmed itinerary (and clears the planning conversation so the next
  session starts fresh). There is no pre/post-turn planning hook — Hermes
  calls this only when the conversation requires that action.

The adapter injects the identifiers these tools need (`room_name`,
`platform`, `user_id`, `hermes_session_id`, and — when present in the
speaker's trusted LiveKit participant metadata — `mira_account_id` and
`aware_device_id`) into the model's context on every invoked turn (see
`_mcp_identifier_context` in `adapter.py`), since an MCP tool call carries no
implicit session context the way this plugin's own framework-registered
tools do.

The `/trips` text box is a dedicated chat surface: pressing **Send** is an
explicit Hermes invocation and does not require the `MiRA` voice wake term.
The wake term still applies to ambient speech in a live room.

A generic client-offered remote-tool channel still exists in the adapter
(`client:tool-register` / `agent:tool-call` / `client:tool-result` over the
`hermes-control` topic) for any future tool a connected client wants to
offer — it just has no MiRA-specific tool names allow-listed by default any
more. Configure `platforms.livekit.extra.remote_tools.allowed_names` to opt
one in.

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

LiveKit sessions expose optional tourism evidence routes:

- `get_confirmed_itinerary` / `get_traveller_location` / `get_meeting_transcript`
  (hermes-mira-context MCP server) — saved itinerary, current location and
  local time, and earlier meeting speech;
- `manage_trip_itinerary` (same MCP server) - account linking plus
  conversational draft revision and explicit confirmation. A draft is not a
  saved itinerary;
- Hermes web and MCP search capabilities for fresh facts, local
  recommendations, or current conditions.

Setup resolves the GUI/CLI toolset surface—including Hermes additions that are
effective but not yet present in an older saved list—and persists that same
selection for CLI, LiveKit, and configured Discord. It adds `hermes-livekit`
and removes the old `no_mcp` sentinel, so globally enabled MCP servers follow
Hermes's normal platform resolution.
The tourism skill is registered for Hermes to load when useful; it is not
concatenated into every voice prompt.

## Hermes YAML

The relevant section of `%LOCALAPPDATA%\hermes\config.yaml` is:

```yaml
platform_toolsets:
  cli: &hermes-conversation-tools
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
    - hermes-livekit
  livekit: *hermes-conversation-tools
  discord: *hermes-conversation-tools

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
        silence_threshold_seconds: 0.7
        min_speech_duration_seconds: 0.3
        rms_silence_floor: 50
      vision:
        auto_attach: true
        sample_interval_seconds: 1.0
        frame_max_age_seconds: 10
        image_stream_topics:
          - test
          - hermes-image
      acknowledgements:
        enabled: false
      invocation:
        enabled: true
        keyterms:
          - Hermes
          - MiRA
        strip_keyterm: true
        standalone_followup_seconds: 5.0
      transcription:
        history_max_entries: 80
        history_max_chars: 12000
        prompt_max_entries: 12
        prompt_max_chars: 3000
      remote_tools:
        allowed_names: []   # opt in a client-offered tool by name here
        allowed_owner_prefixes:
          - agent-mira-knowledge-worker-

mcp_servers:
  hermes-mira-context:
    command: C:\path\to\services\hermes-mcp\.venv\Scripts\python.exe
    args: ["-m", "hermes_mcp.server"]
    env:
      MIRA_DATABASE_URL: "${MIRA_DATABASE_URL}"
      MIRA_AWARE_DATABASE_HOST: "${MIRA_AWARE_DATABASE_HOST}"
      MIRA_AWARE_DATABASE_PORT: "${MIRA_AWARE_DATABASE_PORT}"
      MIRA_AWARE_DATABASE_NAME: "${MIRA_AWARE_DATABASE_NAME}"
      MIRA_AWARE_DATABASE_USER: "${MIRA_AWARE_DATABASE_USER}"
      MIRA_AWARE_DATABASE_PASSWORD: "${MIRA_AWARE_DATABASE_PASSWORD}"
    enabled: true

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

The setup keeps `acknowledgements.enabled: false` for the thin transport path.
The setup changes the stock US Edge voice to the New Zealand English Molly
voice, but preserves any custom voice or provider. It does not alter STT.

To route Hermes-selected delegated subtasks and background title/compression
work to the local LM Studio model without putting it in the foreground response
path, run setup with
`-ConfigureAuxiliaryModel`. The defaults target
`qwythos-9b-claude-mythos-5-1m` at `http://127.0.0.1:1234/v1`; override them
with `-AuxiliaryBaseUrl` and `-AuxiliaryModel` when needed.

## Validate

```powershell
$hermes = "$env:LOCALAPPDATA\hermes\hermes-agent\venv\Scripts\hermes.exe"
& $hermes config check
& $hermes gateway status
& $hermes tools list --platform discord
& $hermes plugins doctor --ci .\agents\hermes-livekit
python -m unittest discover -s .\agents\hermes-livekit\tests -v
```

The Discord tool listing must show the `hermes-livekit` toolset with all three
MiRA tools enabled. To exercise the same typed-data path used by `/trips`
against a running local room:

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
2. Ask a question that requires a tool. The tool should start without a
   separate acknowledgement turn, then MiRA should deliver the answer.
3. Ask a multi-tool question. Hermes should choose and sequence the tools.
4. Speak during playback. MiRA should stop and listen.
5. While MiRA is working or speaking, use **Stop agent** and confirm playback
   stops immediately. Repeat with the microphone muted.
6. Speak without a keyterm. The utterance should appear in the labeled room
   transcript, but no Hermes response turn should be created.
7. Say “MiRA” plus a request. MiRA should answer using relevant context from the
   preceding ambient transcript.
8. Speak again without a keyterm. MiRA should remain quiet; repeat with a
   keyterm and confirm the correct participant name appears in context.
9. With camera and screen share enabled, refer explicitly to each source in
   separate turns. Confirm MiRA selects the current speaker's camera for the
   camera request and the room screen-share for the screen request, and that the
   gateway does not attempt voice transcription on a `.jpg` file.
10. Ask for the current location, local time, itinerary, and a detail from the
    earlier call. Confirm one answer can use all four bounded context sources.

## Runtime files

- `adapter.py` — LiveKit transport, media, voice activity, hooks, and tools.
- `__init__.py` — Hermes plugin registration and lifecycle hooks.
- `transcript_store.py` — persists finalized meeting speech for the
  hermes-mira-context MCP server's `get_meeting_transcript` tool.
- `configure_yaml.py` — atomic non-secret behavior configuration, including
  registering the hermes-mira-context MCP server.
- `assets/SOUL.md` — canonical MiRA conversational identity.
- `skills/mira-new-zealand-tourism/SKILL.md` — grounded Aotearoa tourism procedure.
- `plugin.yaml` — plugin metadata.
- `Setup-HermesLiveKit.cmd` — double-click setup for a Hermes Windows host.
- `Setup-HermesLiveKit.ps1` — repeatable Windows install/update and migration.
- `tests/test_adapter.py` — adapter behavior tests.
