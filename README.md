# Hermes LiveKit Plugin

Hermes LiveKit is a platform plugin that connects the Hermes agent gateway to a LiveKit room over WebRTC. It lets the agent listen to speech from room participants, accept typed text from the LiveKit data channel, send the text into the Hermes LLM pipeline, and play the response back into the room as audio.

This repo is meant to live directly under your Hermes plugins folder, which matches your current setup:

`C:\Users\zcha621\AppData\Local\hermes\plugins\livekit`

## What it does

- Joins a LiveKit room as a participant.
- Captures inbound participant audio and turns it into Hermes voice messages.
- Accepts text messages from the LiveKit data channel and routes them into the same Hermes message pipeline.
- Publishes agent speech back into the room through a local LiveKit audio track.
- Emits lightweight lifecycle events for UI state such as listening, thinking, and speaking.
- Supports optional video frame capture and remote-tool registration from clients.

## Files in this plugin

- `adapter.py` - the LiveKit platform adapter implementation.
- `plugin.yaml` - plugin metadata and required environment variables.
- `__init__.py` - package entry point.

## Setup

If you already copied this folder into `C:\Users\zcha621\AppData\Local\hermes\plugins\livekit`, you do not need to move it anywhere else.

1. Keep the plugin files in the Hermes plugins directory.
2. Make sure Hermes knows about the plugin in `C:\Users\zcha621\AppData\Local\hermes\config.yaml`:

```yaml
plugins:
	enabled:
		- hermes-livekit
platforms:
	livekit:
		enabled: true
		group_sessions_per_user: false
		extra:
			url: ${LIVEKIT_URL}
			api_key: ${LIVEKIT_API_KEY}
			api_secret: ${LIVEKIT_API_SECRET}
			room: ${LIVEKIT_ROOM}
```

3. Set the LiveKit environment variables in `C:\Users\zcha621\AppData\Local\hermes\.env` or in your shell.
4. Start the gateway with the usual Hermes command, then join the LiveKit room from a browser or client.

If Hermes cannot import the LiveKit SDK packages yet, install the plugin dependencies in the same Python environment that runs Hermes. The easiest path is:

```bash
pip install hermes-livekit
```

If you prefer to keep the local folder copy and only install dependencies, you can use that package as a convenience install for the LiveKit Python SDKs.

## Requirements

Hermes must be able to load this local plugin folder and import the LiveKit runtime dependencies.

At minimum, the runtime needs the LiveKit SDKs that `adapter.py` imports, plus `ffmpeg` on `PATH` for TTS decoding.

The plugin also expects these LiveKit environment variables:

- `LIVEKIT_URL`
- `LIVEKIT_API_KEY`
- `LIVEKIT_API_SECRET`

Optional variables:

- `LIVEKIT_ROOM` - room name, defaults to `hermes`
- `LIVEKIT_AGENT_NAME` - display name for the agent, defaults to `Hermes`
- `LIVEKIT_AGENT_AVATAR` - avatar URL or local file path
- `LIVEKIT_PRESENCE_POLL_INTERVAL` - override the room presence poll interval
- `HERMES_LIVEKIT_LOG_LEVEL` - logging verbosity for this adapter
- `HERMES_LIVEKIT_TOOL_TIMEOUT_SEC` - timeout for remote tool calls

## Configuration

The plugin metadata is defined in `plugin.yaml`. It declares the required LiveKit credentials and identifies this plugin as a Hermes platform plugin.

The plugin entry point in `__init__.py` registers the `livekit` platform, seeds configuration from your `LIVEKIT_*` env vars, and exposes a small interactive setup helper for Hermes when available.

If you want to set the values manually, you can use environment variables like this:

```powershell
$env:LIVEKIT_URL = "wss://your-instance.livekit.cloud"
$env:LIVEKIT_API_KEY = "your-key"
$env:LIVEKIT_API_SECRET = "your-secret"
```

Your current `.env` already follows this pattern and only needs valid values for the LiveKit connection fields.

## How message flow works

### Speech input

1. A participant in the LiveKit room publishes an audio track.
2. The adapter subscribes to that track and buffers the PCM audio.
3. Silence detection decides when the user has finished speaking.
4. The buffered audio is written to a temporary WAV file.
5. Hermes STT transcribes the audio into text.
6. The adapter wraps the transcript in a Hermes `MessageEvent` and passes it to the gateway base handler.
7. The Hermes agent runs the LLM, tools, and response logic.
8. If the response is voice-enabled, Hermes generates TTS audio and the adapter publishes it back into LiveKit.

### Text input

1. A client sends text over the LiveKit data channel.
2. The adapter normalizes the payload into a Hermes `MessageEvent`.
3. The same gateway message pipeline runs as for speech.
4. The response is delivered back to the LiveKit room as text, audio, or both depending on the platform logic.

## TTS playback

When the gateway decides to speak a response, it calls the adapter's `play_tts()` method with an audio file path. The adapter:

1. Temporarily pauses inbound capture to reduce echo.
2. Decodes the TTS audio file to raw PCM using `ffmpeg`.
3. Feeds the PCM into a LiveKit `AudioSource` in 20 ms frames.
4. Publishes speaking lifecycle events for client UIs.
5. Resumes normal capture after playback ends.

## Troubleshooting

### The adapter does not connect

Check that the LiveKit SDK is installed and the credentials are correct.

- `LIVEKIT_URL`
- `LIVEKIT_API_KEY`
- `LIVEKIT_API_SECRET`

### Speech is not transcribed

Make sure the room participant is actually publishing audio and that silence detection is not filtering out very short or very quiet utterances.

### Audio playback is silent

Verify that `ffmpeg` is installed and available on `PATH`, because the adapter uses it to decode the TTS file before publishing audio frames.

### Browser text works but voice does not

Check that the participant has an audio track subscribed and that the adapter can connect to the room long enough to receive it.

## Notes

This plugin is designed to work on top of the upstream Hermes gateway without core patches. It follows the gateway platform contract and keeps the LiveKit-specific logic isolated in the plugin folder.

If you move this plugin to another machine, copy the whole `livekit` folder, then update that machine's Hermes config and `.env` values to match its LiveKit room and credentials.
