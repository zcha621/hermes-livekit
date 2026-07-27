# MiRA Hermes + LiveKit

This repository is the source of MiRA's Hermes LiveKit integration. MiRA pins
it as the Git submodule `agents/hermes-livekit`. It contains:

- `adapter.py`, `__init__.py`, and `plugin.yaml` — the Hermes platform plugin
  for audio, text, barge-in, camera and screen-share vision, work
  acknowledgements, and progress notifications.
- `Setup-HermesLiveKit.ps1` — repeatable Windows installation and configuration.
- `control_bridge.py` — the authenticated, allow-listed control plane used by
  the MiRA web portal.
- `tests/` — control-plane and adapter tests.

The plugin runs beside Hermes on a Windows host. The portal never reads or
writes that host's files directly:

```text
browser -> authenticated Next.js API -> bearer-authenticated control bridge
                                             |
                                             +-> Hermes .env/config/gateway
                                             +-> LiveKit room
```

## Prerequisites

- Windows PowerShell 5.1 or PowerShell 7
- A LiveKit URL, API key, and API secret
- A configured Hermes model/provider
- Network reachability from the portal server to the control bridge

The setup script can install Hermes and ffmpeg when the explicit
`-InstallHermes` and `-InstallFfmpeg` switches are supplied. Hermes itself is
downloaded from the official Nous Research Windows installer. Run `hermes
setup` afterward if a model provider has not yet been configured.

## Developer quick start

Clone MiRA with its submodules:

```powershell
git clone --recurse-submodules https://github.com/zcha621/MiRA.git
Set-Location MiRA
```

From the MiRA repository root on the Windows host:

```powershell
.\agents\hermes-livekit\Setup-HermesLiveKit.ps1 `
  -InstallHermes `
  -InstallFfmpeg `
  -LiveKitUrl "ws://livekit-host:7880" `
  -LiveKitApiKey "development-key" `
  -LiveKitApiSecret "development-secret" `
  -Room "hermes" `
  -StartControlBridge `
  -RestartGateway
```

For login auto-start, add `-InstallAutoStart`. This installs Hermes's gateway
task and a current-user scheduled task called `MiRA Hermes Control Bridge`.

The script:

1. backs up existing Hermes `.env` and `config.yaml`;
2. copies the runtime plugin files to
   `%LOCALAPPDATA%\hermes\plugins\hermes-livekit`;
3. copies the control bridge to `%LOCALAPPDATA%\hermes\control`;
4. installs pinned LiveKit SDKs, Pillow, and PyYAML in Hermes's virtualenv;
5. enables the plugin and applies the MiRA voice/vision/interrupt settings;
6. creates a random 256-bit control token unless one was supplied;
7. validates imports and `hermes config check`;
8. optionally starts the bridge and gateway.

At the end it prints `HERMES_CONTROL_TOKEN`. Treat this value like a password.
Copy it to the portal server's ignored `.env.local`; do not commit it.

To update an existing developer host after pulling repository changes, rerun
the same command. The old plugin and configuration are retained as
timestamped backups.

## Pair the web portal

Add these server-only values to `apps/web-portal/.env.local`:

```env
HERMES_CONTROL_URL=http://127.0.0.1:8790
HERMES_CONTROL_TOKEN=replace-with-the-token-printed-by-setup
HERMES_CONTROL_TIMEOUT_MS=10000
```

If the portal and Hermes are on separate machines, terminate TLS at a private
reverse proxy and use an `https://` URL. Plain remote HTTP is rejected by
default because it would expose the bearer token and write-only LiveKit
credentials. For an isolated development network only:

```env
HERMES_CONTROL_ALLOW_INSECURE=true
```

Start the bridge on an externally reachable interface only when the network
and firewall are appropriately restricted:

```powershell
.\agents\hermes-livekit\Setup-HermesLiveKit.ps1 `
  -LiveKitUrl "ws://livekit-host:7880" `
  -LiveKitApiKey "development-key" `
  -LiveKitApiSecret "development-secret" `
  -ControlHost "0.0.0.0" `
  -StartControlBridge
```

Grant a portal user `hermes_manage` in **User Management**. Administrators have
the permission implicitly. The **Hermes Control** dashboard then provides:

- bridge, gateway, and platform monitoring every five seconds;
- start, stop, and restart actions;
- setup/configuration for URL, write-only credentials, room, participant name,
  TTS, silence, work acknowledgement, progress, and vision;
- room-scoped switching between Hermes and a MiRA Python LiveKit agent.

## Agent switching semantics

Hermes currently watches one configured room at a time. The portal therefore
treats Hermes ownership as global while the MiRA Python agent remains
room-scoped.

- Switching to **Hermes** enables the Hermes LiveKit platform, assigns the
  selected room, restarts the gateway, and then deactivates any active MiRA
  Python agent in that room.
- Switching to **MiRA LiveKit agent** disables the Hermes LiveKit platform,
  restarts the gateway, and activates the selected database-backed Python
  agent for the supplied Tourism AI session UUID.
- If Python activation fails, the API attempts to restore the previous Hermes
  room and enabled state.
- Switch requests are serialized within each portal server process to prevent
  overlapping lifecycle mutations.

The gateway restart briefly reconnects other configured Hermes platforms.

## Security boundary

The bridge:

- requires `Authorization: Bearer ...` for every `/v1/*` route;
- uses constant-time token comparison and a minimum 32-character token;
- accepts JSON bodies of at most 64 KiB;
- validates all rooms, URLs, booleans, and numeric ranges;
- updates only an explicit environment/config allow-list;
- never returns the LiveKit API key, API secret, or control token;
- creates a timestamped `.env` backup before portal updates;
- binds to loopback by default;
- sends no permissive browser CORS headers.

The browser talks only to authenticated same-origin Next.js routes. Portal
routes enforce `hermes_manage`; the control token exists only on the portal
server and Hermes host.

## Plugin behavior

Required environment variables:

- `LIVEKIT_URL`
- `LIVEKIT_API_KEY`
- `LIVEKIT_API_SECRET`

Important optional settings:

- `LIVEKIT_ROOM` (default `hermes`)
- `LIVEKIT_AGENT_NAME` (default `Hermes`)
- `HERMES_LIVEKIT_SILENCE_SECONDS` (default `0.8`)
- `HERMES_LIVEKIT_AUTO_VISION` (default `true`)
- `HERMES_LIVEKIT_VIDEO_SAMPLE_SECONDS` (default `1.0`)
- `HERMES_LIVEKIT_VIDEO_MAX_AGE_SECONDS` (default `10`)
- `HERMES_LIVEKIT_WORK_ACK_SECONDS` (default `6`)
- `HERMES_LIVEKIT_WORK_ACK_MODE` (`off`, `status` (default), `text`, or `spoken`)
- `HERMES_LIVEKIT_WORK_ACK_TEXT` (default `Let me check that.`)
- `HERMES_AGENT_NOTIFY_INTERVAL` (default `20`)

Camera and screen-share tracks are sampled continuously and the freshest
relevant frame is attached to visual turns. Speech barge-in clears active TTS.
The adapter also accepts text/data messages and MiRA-compatible image streams.

## Validation

From this directory:

```powershell
& "$env:LOCALAPPDATA\hermes\hermes-agent\venv\Scripts\python.exe" `
  -m unittest discover -s tests -v
```

From MiRA's `apps/web-portal`:

```powershell
npm install
npm run test:auth
npm run typecheck
npm run build
```

Operational checks:

```powershell
hermes gateway status
Invoke-RestMethod http://127.0.0.1:8790/health
```

An authenticated status check can be made from the Hermes host without
printing the token:

```powershell
$headers = @{ Authorization = "Bearer $env:HERMES_CONTROL_TOKEN" }
Invoke-RestMethod http://127.0.0.1:8790/v1/status -Headers $headers
```
