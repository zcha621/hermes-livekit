"""LiveKit voice platform adapter using WebRTC.

Joins a LiveKit room as a participant, transcribes inbound audio via
Hermes's STT pipeline, feeds transcripts into the agent loop, and publishes
TTS replies back as audio.

Carved out of hermes-agent's kortexa/gateway-livekit branch so it can be
installed as a pip plugin on top of upstream main without core patches.

Requires:
    pip install hermes-livekit
    LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET env vars
"""

import asyncio
import hashlib
import io
import json
import logging
import math
import os
import random
import re
import struct
import subprocess
import tempfile
import time
import uuid
import wave
from datetime import datetime, timezone
from typing import Any, Dict, Optional

try:
    from livekit import rtc
    LIVEKIT_AVAILABLE = True
except ImportError:
    LIVEKIT_AVAILABLE = False
    rtc = None  # type: ignore[assignment]

try:
    from livekit.api import AccessToken, VideoGrants, LiveKitAPI
    from livekit.protocol.room import ListParticipantsRequest
    LIVEKIT_API_AVAILABLE = True
except ImportError:
    LIVEKIT_API_AVAILABLE = False
    AccessToken = None  # type: ignore[assignment,misc]
    VideoGrants = None  # type: ignore[assignment,misc]
    LiveKitAPI = None  # type: ignore[assignment,misc]
    ListParticipantsRequest = None  # type: ignore[assignment,misc]

# Pillow is used to JPEG-encode sampled video frames before handing them to
# hermes's vision pipeline. The plugin still loads (and voice still works)
# if Pillow is missing; only frame capture is disabled.
try:
    from io import BytesIO
    from PIL import Image
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False
    Image = None  # type: ignore[assignment,misc]
    BytesIO = None  # type: ignore[assignment,misc]

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)

# Use the ``gateway.platforms.livekit`` namespace rather than ``__name__``.
# Hermes core's gateway.log handler installs a component filter that only
# admits records from loggers whose name starts with one of the component
# prefixes (``gateway`` is one of them — see ``hermes_logging.py``
# ``COMPONENT_PREFIXES``). Loggers outside that allowlist get dropped at
# the handler stage regardless of their own level. Adopting the
# ``gateway.platforms.<adapter>`` convention is also what the kortexa
# branch's core-resident version does, so the log output is
# byte-identical whether the LiveKit platform lives in core or here.
logger = logging.getLogger("gateway.platforms.livekit")


# Allow operators to dial verbosity without editing code:
#   HERMES_LIVEKIT_LOG_LEVEL=DEBUG    # noisy
#   HERMES_LIVEKIT_LOG_LEVEL=WARNING  # quiet
#   HERMES_LIVEKIT_LOG_LEVEL=20       # numeric also accepted
# Unset → inherit from hermes's root logger config (INFO under the standard
# gateway setup), matching every other built-in adapter.
def _apply_env_log_level() -> None:
    raw = os.getenv("HERMES_LIVEKIT_LOG_LEVEL", "").strip()
    if not raw:
        return
    try:
        logger.setLevel(int(raw))
        return
    except ValueError:
        pass
    level = logging.getLevelName(raw.upper())
    if isinstance(level, int):
        logger.setLevel(level)


_apply_env_log_level()

# Voice defaults. Behavioral settings are resolved from
# ``platforms.livekit.extra`` in Hermes config.yaml for each adapter instance.
# Keep .env for transport credentials only.
DEFAULT_SILENCE_THRESHOLD_SECONDS = 1.5  # Discord voice-channel parity
DEFAULT_MIN_SPEECH_DURATION_SECONDS = 0.5
DEFAULT_RMS_SILENCE_FLOOR = 50
POLL_INTERVAL = 0.2               # silence check interval when active
IDLE_POLL_INTERVAL = 2.0          # silence check interval when no remote participants
DEFAULT_VIDEO_SAMPLE_INTERVAL_SECONDS = 1.0
DEFAULT_VIDEO_FRAME_MAX_AGE_SECONDS = 10.0
DEFAULT_IMAGE_STREAM_TOPICS = ("test", "hermes-image")
DEFAULT_ACK_PHRASES = (
    "Let me look into that.",
    "One moment.",
    "Checking on that now.",
    "Give me a sec.",
    "On it.",
)
DEFAULT_INVOCATION_KEYTERMS = ("Hermes", "MiRA")
DEFAULT_CONVERSATION_TIMEOUT_SECONDS = 120.0
AGENT_STATUS_SCHEMA = "mira-agent-status.v1"
AGENT_STATES = frozenset({"initializing", "idle", "listening", "thinking", "speaking"})
TOPIC_MAX_LENGTH = 180
MAX_IMAGE_STREAM_BYTES = 12 * 1024 * 1024
VISUAL_UTTERANCE_RE = re.compile(
    r"\b("
    r"see|look|watch|show|visible|visual|camera|face|screen|share|"
    r"image|photo|picture|video|frame|read|sign|menu|page|app|"
    r"button|click|tap|wearing|colour|color|object|view|"
    r"this|that|these|those|here|there"
    r")\b",
    re.IGNORECASE,
)

# LiveKit audio defaults
SAMPLE_RATE = 48000
NUM_CHANNELS = 1

# Reconnection
RECONNECT_BACKOFF = [2, 5, 10, 30, 60]
MAX_RECONNECT_ATTEMPTS = 10       # give up after this many consecutive failures

# Presence polling (when no humans in room, we stay out and poll).
# Defaults differ by deployment:
#   - LiveKit Cloud has real API rate limits and we pay per-minute, so
#     30s keeps headroom while still waking fast enough for normal UX.
#   - Self-hosted LiveKit has no limits and no cost pressure, so we poll
#     aggressively enough that the first speaker doesn't wait noticeably.
# Override with LIVEKIT_PRESENCE_POLL_INTERVAL (seconds) in the env if
# neither default fits.
PRESENCE_POLL_INTERVAL_CLOUD = 30.0
PRESENCE_POLL_INTERVAL_LOCAL = 5.0

# Remote tools (client-offered, callable by the agent). See
# docs/remote-tools-design.md. v0.3.0 ships protocol + desktop_notify-style
# small-result tools; large/binary results are Phase 1.5.
TOOL_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$")
TOOL_CALL_TIMEOUT_DEFAULT = 30.0
TOOLSET_NAME = "hermes-livekit-tools"

# Live adapter instances — used by the plugin's session-finalize hook to find
# the adapter(s) whose pending remote-tool calls need cancellation when the
# user issues /new. Set membership is managed by __init__ / disconnect.
LIVE_ADAPTERS: "set[LiveKitAdapter]" = set()


def check_livekit_requirements() -> bool:
    """Check if LiveKit dependencies are available and configured."""
    if not LIVEKIT_AVAILABLE or not LIVEKIT_API_AVAILABLE:
        return False
    if not os.getenv("LIVEKIT_URL") or not os.getenv("LIVEKIT_API_KEY") or not os.getenv("LIVEKIT_API_SECRET"):
        return False
    return True


def _compute_rms(pcm_data: bytes) -> float:
    """Compute RMS energy of 16-bit PCM samples."""
    if len(pcm_data) < 2:
        return 0.0
    n_samples = len(pcm_data) // 2
    samples = struct.unpack(f"<{n_samples}h", pcm_data[:n_samples * 2])
    if not samples:
        return 0.0
    return math.sqrt(sum(s * s for s in samples) / n_samples)


def _pcm_to_wav(pcm_data: bytes, sample_rate: int, channels: int) -> bytes:
    """Wrap raw 16-bit PCM in a WAV container."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)  # 16-bit
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_data)
    return buf.getvalue()


class LiveKitAdapter(BasePlatformAdapter):
    """LiveKit voice adapter using WebRTC.

    Joins a LiveKit room, captures participant audio, transcribes to text,
    and sends TTS replies back to the room.
    """

    def __init__(self, config: PlatformConfig):
        # Use Platform("livekit") instead of Platform.LIVEKIT — the plugin
        # registers the platform name dynamically and Platform._missing_
        # creates a pseudo-member on first lookup.
        super().__init__(config, Platform("livekit"))

        extra = config.extra or {}
        self._url: str = extra.get("url") or os.getenv("LIVEKIT_URL", "")
        self._api_key: str = extra.get("api_key") or os.getenv("LIVEKIT_API_KEY", "")
        self._api_secret: str = extra.get("api_secret") or os.getenv("LIVEKIT_API_SECRET", "")
        self._room_name: str = extra.get("room") or os.getenv("LIVEKIT_ROOM", "hermes")
        self._agent_name: str = extra.get("agent_name") or os.getenv("LIVEKIT_AGENT_NAME", "Hermes")
        self._agent_avatar: str = extra.get("agent_avatar") or os.getenv("LIVEKIT_AGENT_AVATAR", "") or self._find_default_avatar()

        audio_config = extra.get("audio") if isinstance(extra.get("audio"), dict) else {}
        vision_config = extra.get("vision") if isinstance(extra.get("vision"), dict) else {}
        ack_config = (
            extra.get("acknowledgements")
            if isinstance(extra.get("acknowledgements"), dict)
            else {}
        )
        invocation_config = (
            extra.get("invocation")
            if isinstance(extra.get("invocation"), dict)
            else {}
        )
        self._silence_threshold_seconds = self._positive_float(
            audio_config.get("silence_threshold_seconds"),
            DEFAULT_SILENCE_THRESHOLD_SECONDS,
        )
        self._min_speech_duration_seconds = self._positive_float(
            audio_config.get("min_speech_duration_seconds"),
            DEFAULT_MIN_SPEECH_DURATION_SECONDS,
        )
        self._rms_silence_floor = self._nonnegative_float(
            audio_config.get("rms_silence_floor"), DEFAULT_RMS_SILENCE_FLOOR
        )
        self._video_sample_interval_seconds = self._positive_float(
            vision_config.get("sample_interval_seconds"),
            DEFAULT_VIDEO_SAMPLE_INTERVAL_SECONDS,
        )
        self._video_frame_max_age_seconds = self._positive_float(
            vision_config.get("frame_max_age_seconds"),
            DEFAULT_VIDEO_FRAME_MAX_AGE_SECONDS,
        )
        self._auto_vision = self._config_bool(vision_config.get("auto_attach"), True)
        topics = vision_config.get("image_stream_topics", DEFAULT_IMAGE_STREAM_TOPICS)
        if isinstance(topics, str):
            topics = topics.split(",")
        self._image_stream_topics = tuple(
            str(topic).strip() for topic in topics if str(topic).strip()
        ) if isinstance(topics, (list, tuple)) else DEFAULT_IMAGE_STREAM_TOPICS
        self._ack_enabled = self._config_bool(ack_config.get("enabled"), True)
        phrases = ack_config.get("phrases", DEFAULT_ACK_PHRASES)
        if isinstance(phrases, str):
            phrases = [phrases]
        self._ack_phrases = tuple(
            str(phrase).strip() for phrase in phrases if str(phrase).strip()
        ) if isinstance(phrases, (list, tuple)) else DEFAULT_ACK_PHRASES
        if not self._ack_phrases:
            self._ack_enabled = False

        keyterms = invocation_config.get("keyterms", DEFAULT_INVOCATION_KEYTERMS)
        if isinstance(keyterms, str):
            keyterms = keyterms.split(",")
        self._keyterms = tuple(
            dict.fromkeys(
                str(keyterm).strip()
                for keyterm in keyterms
                if str(keyterm).strip()
            )
        ) if isinstance(keyterms, (list, tuple)) else DEFAULT_INVOCATION_KEYTERMS
        self._invocation_enabled = self._config_bool(
            invocation_config.get("enabled"), True
        ) and bool(self._keyterms)
        self._strip_keyterm = self._config_bool(
            invocation_config.get("strip_keyterm"), True
        )
        self._conversation_timeout_seconds = self._positive_float(
            invocation_config.get("conversation_timeout_seconds"),
            DEFAULT_CONVERSATION_TIMEOUT_SECONDS,
        )
        self._keyterm_patterns = tuple(
            (
                keyterm,
                re.compile(
                    r"(?<!\w)"
                    + r"\s+".join(re.escape(part) for part in keyterm.split())
                    + r"(?!\w)",
                    re.IGNORECASE,
                ),
            )
            for keyterm in self._keyterms
        )

        self._room: Optional["rtc.Room"] = None
        self._audio_source: Optional["rtc.AudioSource"] = None
        self._local_track: Optional["rtc.LocalAudioTrack"] = None
        self._silence_task: Optional[asyncio.Task] = None
        self._connect_task: Optional[asyncio.Task] = None
        self._presence_task: Optional[asyncio.Task] = None
        self._graceful_leave: bool = False  # set while intentionally leaving

        # Per-participant audio buffers: identity -> (pcm bytearray, last_audio_time)
        self._audio_buffers: Dict[str, bytearray] = {}
        self._last_audio_time: Dict[str, float] = {}
        self._audio_streams: Dict[str, asyncio.Task] = {}
        self._audio_stream_handles: Dict[str, Any] = {}

        # Explicit client-side input pause. TTS never toggles this flag:
        # remote microphone tracks are independent of our published audio and
        # must remain live so users can barge in while Hermes is speaking.
        self._paused = False
        self._is_playing = False
        self._playback_interrupt = asyncio.Event()
        self._playback_lock = asyncio.Lock()
        self._stt_lock = asyncio.Lock()
        self._event_loop: Optional[asyncio.AbstractEventLoop] = None
        self._tool_ack_generation = 0
        self._tool_ack_pending = False
        self._tool_ack_session_id = ""
        self._tool_ack_tasks: set[asyncio.Task] = set()
        self._tool_ack_audio_paths: Dict[str, str] = {}

        # Per-participant speech state (for listening-start/stop events)
        self._speaking_participants: set[str] = set()

        # A keyterm opens a room-wide conversation window. Hermes already
        # supports shared group sessions; this state adds explicit invocation,
        # current-speaker, and per-speaker topic tracking to that session.
        self._conversation_active_until = 0.0
        self._conversation_expiry_task: Optional[asyncio.Task] = None
        self._participant_topics: Dict[str, str] = {}
        self._active_speaker_identity = ""
        self._active_speaker_name = ""
        self._active_topic = ""
        self._last_keyterm = ""
        self._agent_state = "initializing"
        self._agent_state_generation = 0
        self._agent_state_lock = asyncio.Lock()

        # Video streams are continuously drained to avoid native queue
        # overflows. Only a throttled latest frame is JPEG-encoded in memory.
        # Track SID keys allow camera and screen share to coexist.
        self._video_streams: Dict[str, "rtc.VideoStream"] = {}
        self._video_tasks: Dict[str, asyncio.Task] = {}
        self._video_track_meta: Dict[str, tuple[str, str]] = {}
        self._latest_video_frames: Dict[str, tuple[bytes, float, str, int, int]] = {}

        # Frames captured-but-not-yet-dispatched. Drained into the next
        # MessageEvent built by _process_voice_input or _handle_client_message.
        # Each entry is a (path, mime_type) tuple. Paths are temp files written
        # under <tempdir>/hermes_livekit/; cleanup happens on disconnect (the
        # agent loop reads the file after handle_message returns, so we can't
        # unlink at dispatch time).
        self._pending_captures: list[tuple[str, str]] = []

        # Remote tools registered by connected clients over the data channel.
        # See docs/remote-tools-design.md. Single-client v1: we track per
        # identity so participant-disconnect cleanup is uniform, but only one
        # client is expected to register at a time.
        self._client_tools: Dict[str, set[str]] = {}        # identity -> tool names
        self._tool_owners: Dict[str, str] = {}              # tool name -> owner identity
        self._pending_tool_calls: Dict[str, asyncio.Future] = {}  # call_id -> future
        self._pending_tool_owners: Dict[str, str] = {}      # call_id -> owner identity

        self._presence_poll_interval: float = self._resolve_presence_poll_interval()
        self._tool_call_timeout: float = self._resolve_tool_call_timeout()

        # Register in module-level set so the plugin's session-finalize hook
        # can reach us. (Multi-room would mean multiple adapters; v1 is one.)
        LIVE_ADAPTERS.add(self)

    @staticmethod
    def _config_bool(value: Any, default: bool) -> bool:
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"1", "true", "yes", "on"}:
                return True
            if normalized in {"0", "false", "no", "off"}:
                return False
        return bool(value)

    @staticmethod
    def _positive_float(value: Any, default: float) -> float:
        try:
            parsed = float(value)
            return parsed if parsed > 0 else default
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _nonnegative_float(value: Any, default: float) -> float:
        try:
            parsed = float(value)
            return parsed if parsed >= 0 else default
        except (TypeError, ValueError):
            return default

    def _conversation_is_active(self) -> bool:
        if not self._invocation_enabled:
            return True
        return time.monotonic() < self._conversation_active_until

    def _participant_display_name(self, identity: str) -> str:
        if self._room is not None:
            participant = self._room.remote_participants.get(identity)
            name = str(getattr(participant, "name", "") or "").strip()
            if name:
                return name
        return identity or "Participant"

    def _match_keyterm(self, transcript: str) -> tuple[str, str]:
        """Return ``(matched keyterm, optionally stripped transcript)``."""
        for keyterm, pattern in self._keyterm_patterns:
            match = pattern.search(transcript)
            if match is None:
                continue
            cleaned = transcript
            if self._strip_keyterm:
                cleaned = (transcript[:match.start()] + " " + transcript[match.end():])
                cleaned = re.sub(r"^[\s,.:;!?\-–—]+|[\s,.:;!?\-–—]+$", "", cleaned)
                cleaned = re.sub(r"^(?:hey|hi|okay|ok)\b[\s,.:;!?\-–—]*", "", cleaned, flags=re.IGNORECASE)
                cleaned = re.sub(r"\s{2,}", " ", cleaned)
            return keyterm, cleaned.strip()
        return "", transcript.strip()

    @staticmethod
    def _topic_for_turn(text: str, previous: str = "") -> str:
        compact = re.sub(r"\s+", " ", text).strip()
        meaningful_words = re.findall(r"[\w'-]+", compact, flags=re.UNICODE)
        if len(meaningful_words) < 4 and previous:
            return previous
        if len(compact) <= TOPIC_MAX_LENGTH:
            return compact
        return compact[: TOPIC_MAX_LENGTH - 1].rstrip() + "…"

    def _activate_conversation(self, keyterm: str = "") -> None:
        self._conversation_active_until = (
            time.monotonic() + self._conversation_timeout_seconds
        )
        if keyterm:
            self._last_keyterm = keyterm
        if self._conversation_expiry_task is not None:
            self._conversation_expiry_task.cancel()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._conversation_expiry_task = None
        else:
            self._conversation_expiry_task = loop.create_task(
                self._expire_conversation_after_idle()
            )

    async def _expire_conversation_after_idle(self) -> None:
        try:
            while self._running:
                remaining = self._conversation_active_until - time.monotonic()
                if remaining > 0:
                    await asyncio.sleep(remaining)
                    continue
                if self._active_sessions or self._is_playing or self._speaking_participants:
                    self._conversation_active_until = time.monotonic() + 5.0
                    continue
                self._active_speaker_identity = ""
                self._active_speaker_name = ""
                self._active_topic = ""
                await self._set_agent_state("idle", force=True)
                return
        except asyncio.CancelledError:
            return

    async def _prepare_invoked_event(self, event: MessageEvent) -> bool:
        """Apply room invocation policy and speaker/topic context to a turn."""
        identity = str(getattr(event.source, "user_id", "") or "client")
        display_name = self._participant_display_name(identity)
        matched_keyterm, cleaned = self._match_keyterm(event.text or "")
        was_active = self._conversation_is_active()

        if matched_keyterm:
            self._activate_conversation(matched_keyterm)
        elif not was_active:
            await self._publish_agent_event(
                "agent:invocation-required",
                {"identity": identity, "keyterms": list(self._keyterms)},
            )
            await self._set_agent_state("idle", force=True)
            return False
        else:
            self._activate_conversation()

        self._active_speaker_identity = identity
        self._active_speaker_name = display_name
        self._active_topic = self._participant_topics.get(identity, "")
        if cleaned:
            topic = self._topic_for_turn(
                cleaned, self._participant_topics.get(identity, "")
            )
            if topic:
                self._participant_topics[identity] = topic
                self._active_topic = topic

        # A standalone wake phrase opens the follow-up window without sending a
        # content-free LLM turn. The UI still receives the invocation status.
        if not cleaned:
            await self._publish_agent_event(
                "agent:invoked",
                {"identity": identity, "name": display_name, "keyterm": matched_keyterm},
            )
            await self._set_agent_state("idle", force=True)
            return False

        event.text = cleaned
        event.source.user_name = display_name
        event.source.chat_type = "group"
        ledger = "; ".join(
            f"{self._participant_display_name(person)}: {topic}"
            for person, topic in self._participant_topics.items()
        )
        meeting_context = (
            "LiveKit meeting context: the current speaker is "
            f"{display_name} (identity {identity}). "
            "Address the correct speaker and distinguish participants. "
            f"Latest participant topics/requests: {ledger}."
        )
        existing_prompt = str(getattr(event, "channel_prompt", "") or "").strip()
        event.channel_prompt = (
            f"{existing_prompt}\n\n{meeting_context}" if existing_prompt else meeting_context
        )
        setattr(event, "_livekit_invocation_checked", True)
        return True

    def _agent_status_payload(self) -> Dict[str, Any]:
        invoked = self._conversation_is_active()
        return {
            "schema": AGENT_STATUS_SCHEMA,
            "state": self._agent_state,
            "invoked": invoked,
            "can_interrupt": invoked,
            "active_speaker": {
                "identity": self._active_speaker_identity,
                "name": self._active_speaker_name,
            },
            "topic": self._active_topic,
            "keyterms": list(self._keyterms),
            "last_keyterm": self._last_keyterm,
            "updated_at": datetime.now(tz=timezone.utc).isoformat(),
        }

    async def _set_agent_state(self, state: str, *, force: bool = False) -> int:
        """Synchronize LiveKit's standard state attribute and MiRA context."""
        if state not in AGENT_STATES:
            raise ValueError(f"Unsupported LiveKit agent state: {state}")
        async with self._agent_state_lock:
            if not force and state == self._agent_state:
                return self._agent_state_generation
            previous_state = self._agent_state
            self._agent_state = state
            self._agent_state_generation += 1
            generation = self._agent_state_generation
            payload = self._agent_status_payload()
            if self._room is not None:
                attributes = {
                    "lk.agent.state": state,
                    "lk.agent.inputs": '["audio","text"]',
                    "lk.agent.outputs": '["audio","transcription"]',
                    "mira.agent.status_schema": AGENT_STATUS_SCHEMA,
                    "mira.agent.invoked": "true" if payload["invoked"] else "false",
                    "mira.agent.active_speaker": self._active_speaker_identity,
                    "mira.agent.active_speaker_name": self._active_speaker_name,
                    "mira.agent.topic": self._active_topic,
                    "mira.agent.keyterms": json.dumps(self._keyterms),
                }
                try:
                    await self._room.local_participant.set_attributes(attributes)
                except Exception as exc:
                    logger.debug("[%s] agent attributes update failed: %s", self.name, exc)
                await self._publish_agent_event("agent:status", payload)
                if previous_state == "thinking" and state != "thinking":
                    await self._publish_agent_event("agent:thinking-stop")
                if state == "thinking" and previous_state != "thinking":
                    await self._publish_agent_event("agent:thinking-start")
            return generation

    async def _finish_state_if_current(self, generation: int) -> None:
        if generation == self._agent_state_generation:
            await self._set_agent_state("idle")

    def _arm_tool_acknowledgement(self) -> None:
        """Arm one acknowledgement for the next real tool call in this turn."""
        self._event_loop = asyncio.get_running_loop()
        self._tool_ack_generation += 1
        self._tool_ack_pending = self._ack_enabled
        self._tool_ack_session_id = ""

    def bind_tool_acknowledgement_session(self, session_id: str) -> None:
        """Bind the armed LiveKit turn to Hermes's persisted session ID."""
        if self._tool_ack_pending:
            self._tool_ack_session_id = str(session_id or "")

    def schedule_tool_acknowledgement(
        self,
        *,
        session_id: str = "",
        turn_id: str = "",
        tool_call_id: str = "",
    ) -> bool:
        """Schedule the Discord-style first-tool acknowledgement thread-safely.

        Hermes tool hooks may execute on a worker thread. The actual TTS and
        LiveKit work is therefore handed back to the adapter's event loop.
        """
        if not self._tool_ack_pending or not self._ack_enabled:
            return False
        if self._tool_ack_session_id and str(session_id or "") != self._tool_ack_session_id:
            return False
        loop = self._event_loop
        if loop is None or not loop.is_running() or self._room is None:
            return False

        self._tool_ack_pending = False
        generation = self._tool_ack_generation
        loop.call_soon_threadsafe(self._start_tool_ack_task, generation)
        logger.debug(
            "[%s] tool acknowledgement armed for turn=%s tool_call=%s",
            self.name,
            turn_id or "?",
            tool_call_id or "?",
        )
        return True

    def _start_tool_ack_task(self, generation: int) -> None:
        if generation != self._tool_ack_generation or not self._running:
            return
        task = asyncio.create_task(self._play_tool_acknowledgement(generation))
        self._tool_ack_tasks.add(task)
        task.add_done_callback(self._tool_ack_tasks.discard)

    async def _play_tool_acknowledgement(self, generation: int) -> None:
        """Speak one short phrase only while the originating turn is active."""
        phrase = random.choice(self._ack_phrases)
        try:
            audio_path = self._tool_ack_audio_paths.get(phrase, "")
            if not audio_path or not os.path.isfile(audio_path):
                from tools.tts_tool import check_tts_requirements, text_to_speech_tool

                if not check_tts_requirements():
                    return
                digest = hashlib.sha256(phrase.encode("utf-8")).hexdigest()[:12]
                output_path = os.path.join(
                    tempfile.gettempdir(), "hermes_livekit", f"tool_ack_{digest}.mp3"
                )
                os.makedirs(os.path.dirname(output_path), exist_ok=True)
                raw_result = await asyncio.to_thread(
                    text_to_speech_tool,
                    text=phrase,
                    output_path=output_path,
                )
                result = json.loads(raw_result)
                audio_path = str(result.get("file_path") or output_path)
                if not result.get("success", True) or not os.path.isfile(audio_path):
                    return
                self._tool_ack_audio_paths[phrase] = audio_path

            # Do not let a late TTS synthesis talk over a response that has
            # already completed or a newer user turn.
            if generation != self._tool_ack_generation or not self._active_sessions:
                return
            await self.play_tts(
                self._room_name,
                audio_path,
                metadata={"non_conversational": True, "tool_acknowledgement": True},
            )
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.debug("[%s] tool acknowledgement failed: %s", self.name, exc)

    def _finish_tool_acknowledgement_turn(self) -> None:
        self._tool_ack_generation += 1
        self._tool_ack_pending = False
        self._tool_ack_session_id = ""

    def _should_auto_tts_for_chat(self, chat_id: str) -> bool:
        """LiveKit is voice-first — always auto-TTS unless the chat opted out.

        On text platforms the default is gated by ``voice.auto_tts`` (off by
        default). On LiveKit the channel itself is audio, so a typed-only
        reply gives the user nothing. Per-chat ``/voice off`` still wins.
        """
        if chat_id in self._auto_tts_disabled_chats:
            return False
        return True

    def _resolve_presence_poll_interval(self) -> float:
        """Pick the presence-poll interval: env override > cloud/local default.

        LiveKit Cloud hosts on ``*.livekit.cloud``; anything else is treated
        as a self-hosted deployment and gets the faster default.
        """
        override = os.getenv("LIVEKIT_PRESENCE_POLL_INTERVAL", "").strip()
        if override:
            try:
                parsed = float(override)
                if parsed > 0:
                    logger.info("[%s] presence poll interval=%.1fs (LIVEKIT_PRESENCE_POLL_INTERVAL)", self.name, parsed)
                    return parsed
            except ValueError:
                logger.warning("[%s] LIVEKIT_PRESENCE_POLL_INTERVAL=%r is not a number; using default", self.name, override)

        is_cloud = ".livekit.cloud" in self._url.lower()
        interval = PRESENCE_POLL_INTERVAL_CLOUD if is_cloud else PRESENCE_POLL_INTERVAL_LOCAL
        logger.info("[%s] presence poll interval=%.1fs (%s default)", self.name, interval, "cloud" if is_cloud else "local")
        return interval

    def _resolve_tool_call_timeout(self) -> float:
        """Per-call timeout for remote tools. HERMES_LIVEKIT_TOOL_TIMEOUT_SEC overrides."""
        raw = os.getenv("HERMES_LIVEKIT_TOOL_TIMEOUT_SEC", "").strip()
        if raw:
            try:
                parsed = float(raw)
                if parsed > 0:
                    return parsed
            except ValueError:
                logger.warning("[%s] HERMES_LIVEKIT_TOOL_TIMEOUT_SEC=%r is not a number; using default", self.name, raw)
        return TOOL_CALL_TIMEOUT_DEFAULT

    @staticmethod
    def _find_default_avatar() -> str:
        """Look for a default avatar image in ~/.hermes/."""
        from pathlib import Path
        hermes_home = Path.home() / ".hermes"
        for name in ("agent.png", "agent.jpg"):
            path = hermes_home / name
            if path.is_file():
                return str(path)
        return ""

    def _resolve_avatar_url(self) -> str:
        """Convert avatar to a URL suitable for LiveKit metadata.

        If it's already a URL, use as-is. If it's a local file, encode
        as a data URI so it works without a web server.
        """
        avatar = self._agent_avatar
        if not avatar:
            return ""
        if avatar.startswith(("http://", "https://", "data:")):
            return avatar
        # Local file — base64 encode as data URI
        try:
            import base64
            from pathlib import Path
            path = Path(avatar).expanduser()
            if not path.is_file():
                return ""
            suffix = path.suffix.lower()
            mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}.get(suffix, "image/png")
            data = base64.b64encode(path.read_bytes()).decode("ascii")
            return f"data:{mime};base64,{data}"
        except Exception:
            return ""

    # -- Connection lifecycle -----------------------------------------------

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        """Start the LiveKit adapter.

        Presence-aware: if the room already has at least one remote
        participant, join immediately. Otherwise stay out and run a
        presence watcher that joins as soon as someone arrives. Either
        way the adapter is "connected" from the gateway's point of view.

        ``is_reconnect`` is part of the BasePlatformAdapter.connect
        contract (the gateway's reconnection watcher passes it); this
        adapter has no cold-boot vs reconnect distinction, so it is
        accepted and ignored.
        """
        self._event_loop = asyncio.get_running_loop()
        if not LIVEKIT_AVAILABLE:
            logger.warning("[%s] livekit SDK not installed. Run: pip install hermes-livekit", self.name)
            return False
        if not LIVEKIT_API_AVAILABLE:
            logger.warning("[%s] livekit-api not installed. Run: pip install hermes-livekit", self.name)
            return False
        if not self._url or not self._api_key or not self._api_secret:
            logger.warning("[%s] LIVEKIT_URL, LIVEKIT_API_KEY, and LIVEKIT_API_SECRET required", self.name)
            return False

        self._running = True

        # Check if anyone is in the room already. If not, don't consume a
        # participant slot — just watch.
        count = await self._count_remote_participants()
        if count > 0:
            logger.info("[%s] %d participant(s) already in '%s', joining", self.name, count, self._room_name)
            return await self._join_room()

        logger.info("[%s] Room '%s' empty, watching for participants (poll %.1fs)", self.name, self._room_name, self._presence_poll_interval)
        self._mark_connected()
        self._presence_task = asyncio.create_task(self._presence_watch_loop())
        return True

    async def _count_remote_participants(self) -> int:
        """Count non-local participants currently in the room via the Server API.

        Returns 0 on any error (room missing, network blip, etc.) — callers
        treat that as "nobody here, keep polling".
        """
        try:
            # Server API expects http(s):// scheme; convert from ws(s)://.
            http_url = self._url
            if http_url.startswith("wss://"):
                http_url = "https://" + http_url[6:]
            elif http_url.startswith("ws://"):
                http_url = "http://" + http_url[5:]
            http_url = http_url.rstrip("/")

            client = LiveKitAPI(url=http_url, api_key=self._api_key, api_secret=self._api_secret)
            try:
                resp = await client.room.list_participants(
                    ListParticipantsRequest(room=self._room_name)
                )
                return len(resp.participants)
            finally:
                await client.aclose()
        except Exception as e:
            logger.debug("[%s] presence check failed: %s", self.name, e)
            return 0

    async def _presence_watch_loop(self) -> None:
        """Poll the room; join as soon as a remote participant appears."""
        try:
            while self._running:
                await asyncio.sleep(self._presence_poll_interval)
                if not self._running:
                    return
                if self._room is not None:
                    # Something else joined us (manual reconnect?); stop polling.
                    return
                count = await self._count_remote_participants()
                if count > 0:
                    logger.info("[%s] Participant detected in '%s', joining", self.name, self._room_name)
                    if await self._join_room():
                        return  # joined — done polling
        except asyncio.CancelledError:
            return

    async def _join_room(self) -> bool:
        """Actually establish the LiveKit room connection and start audio I/O."""
        try:
            self._room = rtc.Room()

            # Register event handlers
            self._room.on("track_subscribed", self._on_track_subscribed)
            self._room.on("track_unsubscribed", self._on_track_unsubscribed)
            self._room.on("participant_disconnected", self._on_participant_disconnected)
            self._room.on("disconnected", self._on_disconnected)
            # Inbound data-channel: clients send control messages (capture-frame,
            # typed text, runtime control hooks) on the hermes-control topic.
            self._room.on("data_received", self._on_data_received)
            for topic in self._image_stream_topics:
                try:
                    self._room.register_byte_stream_handler(topic, self._on_image_stream)
                except Exception as exc:
                    logger.debug("[%s] image stream topic %s unavailable: %s", self.name, topic, exc)

            # Create access token
            import json as _json
            token = (
                AccessToken(api_key=self._api_key, api_secret=self._api_secret)
                .with_identity(f"hermes-{self._agent_name.lower()}")
                .with_name(self._agent_name)
                .with_grants(VideoGrants(
                    room_join=True,
                    room=self._room_name,
                    can_publish=True,
                    can_subscribe=True,
                    can_update_own_metadata=True,
                ))
            )
            jwt_token = token.to_jwt()

            # Connect to room
            await self._room.connect(self._url, jwt_token)
            await self._set_agent_state("initializing", force=True)

            # Set metadata (including avatar) after connecting — avoids JWT size limits
            metadata = {}
            avatar_url = self._resolve_avatar_url()
            if avatar_url:
                metadata["avatar"] = avatar_url
            if metadata:
                await self._room.local_participant.set_metadata(_json.dumps(metadata))

            # Publish a local audio track for TTS playback
            self._audio_source = rtc.AudioSource(SAMPLE_RATE, NUM_CHANNELS)
            self._local_track = rtc.LocalAudioTrack.create_audio_track(
                "hermes-voice", self._audio_source
            )
            options = rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
            await self._room.local_participant.publish_track(self._local_track, options)

            # Start silence detection loop
            self._silence_task = asyncio.create_task(self._check_silence_loop())
            await self._set_agent_state("idle", force=True)

            self._mark_connected()
            logger.info("[%s] Connected to room '%s' at %s", self.name, self._room_name, self._url)

            # If no explicit agent name was configured, ask the LLM and reconnect
            if not os.getenv("LIVEKIT_AGENT_NAME") and not (self.config.extra or {}).get("agent_name"):
                asyncio.create_task(self._resolve_agent_name())

            return True
        except Exception as e:
            logger.error("[%s] Failed to connect: %s", self.name, e)
            return False

    async def disconnect(self) -> None:
        """Disconnect from LiveKit room."""
        self._running = False
        self._mark_disconnected()

        if self._presence_task:
            self._presence_task.cancel()
            try:
                await self._presence_task
            except asyncio.CancelledError:
                pass
            self._presence_task = None

        if self._silence_task:
            self._silence_task.cancel()
            try:
                await self._silence_task
            except asyncio.CancelledError:
                pass
            self._silence_task = None

        if self._conversation_expiry_task:
            self._conversation_expiry_task.cancel()
            try:
                await self._conversation_expiry_task
            except asyncio.CancelledError:
                pass
            self._conversation_expiry_task = None

        await self._close_all_audio_streams()

        # Cancel all audio stream tasks
        for task in self._audio_streams.values():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._audio_streams.clear()

        for task in self._video_tasks.values():
            task.cancel()
        if self._video_tasks:
            await asyncio.gather(*self._video_tasks.values(), return_exceptions=True)
        self._video_tasks.clear()

        # Close video streams after their consumer tasks have exited.
        for stream in self._video_streams.values():
            try:
                await stream.aclose()
            except Exception:
                pass
        self._video_streams.clear()
        self._video_track_meta.clear()
        self._latest_video_frames.clear()

        for task in list(self._tool_ack_tasks):
            task.cancel()
        self._tool_ack_tasks.clear()
        self._tool_ack_pending = False

        for path in set(self._tool_ack_audio_paths.values()):
            try:
                os.unlink(path)
            except OSError:
                pass
        self._tool_ack_audio_paths.clear()

        if self._room:
            self._graceful_leave = True
            try:
                await self._room.disconnect()
            finally:
                self._graceful_leave = False
            self._room = None

        self._audio_source = None
        self._local_track = None
        self._audio_buffers.clear()
        self._last_audio_time.clear()
        self._speaking_participants.clear()
        self._participant_topics.clear()
        self._conversation_active_until = 0.0
        self._active_speaker_identity = ""
        self._active_speaker_name = ""
        self._active_topic = ""

        # Unlink any frame files that were captured but never dispatched
        # (no MessageEvent ever drained them). Dispatched-but-not-yet-read
        # files live on — the agent loop may still be processing.
        for path, _mime in self._pending_captures:
            try:
                os.unlink(path)
            except OSError:
                pass
        self._pending_captures.clear()

        # Drop every client-registered tool from the hermes registry.
        self._cleanup_all_client_tools()

        LIVE_ADAPTERS.discard(self)
        self._event_loop = None

        logger.info("[%s] Disconnected", self.name)

    async def handle_message(self, event) -> None:
        """Process incoming text messages (from voice STT or direct browser chatbox).
        
        Accepts either a LiveKit DataPacket (voice input path) or a Hermes MessageEvent 
        (direct text message from browser chatbox).
        """
        # Determine if this is a DataPacket (voice path) or MessageEvent (text path).
        # The base platform adapter owns actual message dispatch; this method
        # only normalizes LiveKit packets into MessageEvent objects.
        if hasattr(event, "data") and hasattr(event, "topic"):
            # This is a LiveKit DataPacket - voice input path
            packet = event
            participant = getattr(packet, "participant", None)
            identity = str(getattr(participant, "identity", "") or "").strip()
            if not identity:
                logger.warning("[%s] ignoring transcript packet without sender identity", self.name)
                return
            
            try:
                import json as _json
                msg = _json.loads(packet.data.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                logger.debug("[%s] handle_message: undecodable DataPacket payload", self.name)
                return
            
            transcript = msg.get("transcript", "")
            if not transcript.strip():
                logger.debug("[%s] handle_message: empty transcript", self.name)
                return

            msg_event = MessageEvent(
                text=transcript,
                message_type=MessageType.VOICE,
                source=self.build_source(
                    chat_id=self._room_name,
                    chat_name=self._room_name,
                    chat_type="group",
                    user_id=identity,
                    user_name=identity,
                ),
                message_id=getattr(packet, "id", uuid.uuid4().hex[:12]),
                media_urls=[],
                media_types=[],
                timestamp=datetime.now(tz=timezone.utc),
            )
            await self.handle_message(msg_event)
            return

        if not isinstance(event, MessageEvent):
            await super().handle_message(event)
            return

        if not getattr(event, "_livekit_invocation_checked", False):
            if not await self._prepare_invoked_event(event):
                return

        identity = str(getattr(event.source, "user_id", "") or "client")
        await self._publish_agent_event(
            "agent:user-transcript",
            {
                "transcript": event.text,
                "final": True,
                "identity": identity,
                "name": getattr(event.source, "user_name", identity),
                "topic": self._participant_topics.get(identity, ""),
            },
        )
        await self._set_agent_state("thinking", force=True)
        self._arm_tool_acknowledgement()
        await super().handle_message(event)

    # -- LiveKit event handlers ---------------------------------------------

    def _on_track_subscribed(
        self,
        track: "rtc.Track",
        publication: "rtc.RemoteTrackPublication",
        participant: "rtc.RemoteParticipant",
    ):
        """Start capturing media when a participant's track is subscribed.

        Audio tracks are buffered continuously for VAD/STT. Video tracks are
        stored but NOT iterated eagerly — frames are pulled on demand when a
        client sends a ``client:capture-frame`` message on the
        ``hermes-control`` data-channel topic.
        """
        identity = participant.identity

        if track.kind == rtc.TrackKind.KIND_AUDIO:
            logger.info("[%s] Audio track subscribed: %s", self.name, identity)
            self._audio_buffers[identity] = bytearray()
            # Deliberately do NOT seed _last_audio_time here. _check_silence_loop
            # sets it on the first chunk above RMS_SILENCE_FLOOR, and treats a
            # missing entry as "this participant has never spoken" — discarding
            # accumulated noise instead of feeding silence to STT. Seeding it on
            # subscribe defeats that guard: a participant who only ever publishes
            # silence would accrue a stale timestamp and eventually trip an
            # utterance.
            stream = rtc.AudioStream(track)
            self._audio_stream_handles[identity] = stream
            task = asyncio.create_task(self._audio_receive_loop(stream, identity))
            self._audio_streams[identity] = task
            return

        if track.kind == rtc.TrackKind.KIND_VIDEO:
            if not PIL_AVAILABLE:
                logger.warning(
                    "[%s] Video track from %s ignored — Pillow not installed",
                    self.name, identity,
                )
                return
            track_key = self._video_track_key(track, publication, identity)
            source_name = self._video_source_name(publication)
            old = self._video_streams.pop(track_key, None)
            if old is not None:
                try:
                    asyncio.create_task(old.aclose())
                except Exception:
                    pass
            old_task = self._video_tasks.pop(track_key, None)
            if old_task is not None:
                old_task.cancel()
            stream = rtc.VideoStream(track, capacity=1)
            self._video_streams[track_key] = stream
            self._video_track_meta[track_key] = (identity, source_name)
            self._video_tasks[track_key] = asyncio.create_task(
                self._video_receive_loop(track_key, stream)
            )
            logger.info(
                "[%s] Video track subscribed: %s source=%s (continuous latest-frame sampling)",
                self.name, identity, source_name,
            )
            return

    def _on_track_unsubscribed(
        self,
        track: "rtc.Track",
        publication: "rtc.RemoteTrackPublication",
        participant: "rtc.RemoteParticipant",
    ):
        """Clean up when a participant's track is unsubscribed."""
        identity = participant.identity

        if track.kind == rtc.TrackKind.KIND_AUDIO:
            logger.debug("[%s] Audio track unsubscribed: %s", self.name, identity)
            self._cleanup_participant(identity)
            return

        if track.kind == rtc.TrackKind.KIND_VIDEO:
            logger.debug("[%s] Video track unsubscribed: %s", self.name, identity)
            track_key = self._video_track_key(track, publication, identity)
            task = self._video_tasks.pop(track_key, None)
            if task is not None:
                task.cancel()
            stream = self._video_streams.pop(track_key, None)
            self._video_track_meta.pop(track_key, None)
            if stream is not None:
                try:
                    asyncio.create_task(stream.aclose())
                except Exception:
                    pass
            return

    def _on_participant_disconnected(self, participant: "rtc.RemoteParticipant"):
        """Clean up when a participant leaves the room.

        If we're now alone in the room, drop the connection and go back
        to presence polling — no need to consume a participant slot while
        nobody's here to talk to.
        """
        identity = participant.identity
        logger.info("[%s] Participant disconnected: %s", self.name, identity)
        self._cleanup_participant(identity)
        self._cleanup_participant_video(identity)
        # Drop any tools this client had registered + fail their pending calls.
        self._cleanup_client_tools(identity)

        if self._room and not self._room.remote_participants:
            logger.info("[%s] Last participant left '%s', leaving room", self.name, self._room_name)
            asyncio.create_task(self._leave_and_watch())

    async def _leave_and_watch(self) -> None:
        """Tear down the room connection and resume presence polling."""
        # Stop silence detection and audio streams, but keep self._running
        # so the presence loop can resume us later.
        if self._silence_task:
            self._silence_task.cancel()
            try:
                await self._silence_task
            except asyncio.CancelledError:
                pass
            self._silence_task = None

        await self._close_all_audio_streams()

        for task in self._audio_streams.values():
            task.cancel()
        self._audio_streams.clear()
        self._audio_buffers.clear()
        self._last_audio_time.clear()
        self._speaking_participants.clear()

        for task in self._video_tasks.values():
            task.cancel()
        self._video_tasks.clear()
        for stream in self._video_streams.values():
            try:
                await stream.aclose()
            except Exception:
                pass
        self._video_streams.clear()
        self._video_track_meta.clear()
        self._latest_video_frames.clear()

        # Clients will be gone after we drop the room — clear their tools.
        self._cleanup_all_client_tools()

        if self._room:
            self._graceful_leave = True
            try:
                await self._room.disconnect()
            except Exception as e:
                logger.debug("[%s] leave error: %s", self.name, e)
            finally:
                self._graceful_leave = False
            self._room = None
        self._audio_source = None
        self._local_track = None

        if self._running and (self._presence_task is None or self._presence_task.done()):
            self._presence_task = asyncio.create_task(self._presence_watch_loop())

    def _on_disconnected(self, reason: str = ""):
        """Handle unexpected room disconnection — schedule reconnection.

        Graceful leaves (empty room, full teardown) set ``_graceful_leave``
        so we don't fight with ``_leave_and_watch`` / ``disconnect``.
        """
        if not self._running or self._graceful_leave:
            return
        logger.warning("[%s] Disconnected from room: %s. Will reconnect.", self.name, reason)
        self._connect_task = asyncio.create_task(self._reconnect_loop())

    async def _reconnect_loop(self):
        """Reconnect to LiveKit with exponential backoff.

        Caps at MAX_RECONNECT_ATTEMPTS consecutive failures — beyond that the
        adapter stays disconnected rather than spamming a misconfigured URL
        forever. The user can restart the gateway to retry.
        """
        backoff_idx = 0
        attempts = 0
        while self._running:
            if attempts >= MAX_RECONNECT_ATTEMPTS:
                logger.error(
                    "[%s] Giving up after %d reconnect attempts. Restart the gateway to try again.",
                    self.name, attempts,
                )
                self._running = False
                self._mark_disconnected()
                return
            delay = RECONNECT_BACKOFF[min(backoff_idx, len(RECONNECT_BACKOFF) - 1)]
            logger.info("[%s] Reconnecting in %ds (attempt %d/%d)...", self.name, delay, attempts + 1, MAX_RECONNECT_ATTEMPTS)
            await asyncio.sleep(delay)
            if not self._running:
                return
            try:
                if await self._join_room():
                    logger.info("[%s] Reconnected successfully", self.name)
                    return
            except Exception as e:
                logger.warning("[%s] Reconnect attempt failed: %s", self.name, e)
            backoff_idx += 1
            attempts += 1

    async def _resolve_agent_name(self):
        """Ask the LLM for the agent's name, then update the display name in-place."""
        try:
            from openai import AsyncOpenAI
            from hermes_cli.config import load_config

            config = load_config()
            model_config = config.get("model", {})
            provider = model_config.get("provider", "")
            model = model_config.get("default", "")

            # Use the runtime provider resolution to get the right client
            from hermes_cli.runtime_provider import resolve_requested_provider
            resolved = resolve_requested_provider(provider, model)
            if not resolved or not resolved.get("api_key"):
                return

            client = AsyncOpenAI(
                api_key=resolved["api_key"],
                base_url=resolved.get("base_url"),
            )
            resp = await client.chat.completions.create(
                model=resolved.get("model", model),
                messages=[{"role": "user", "content": "What is your name? Reply with ONLY your first name — no quotes, no punctuation, no explanation. It will be used as your on-screen display label in a video call."}],
                max_tokens=20,
            )
            name = resp.choices[0].message.content.strip().strip('"').strip("'").split()[0] if resp.choices else ""
            if not name or name.lower() == "hermes" or len(name) > 30:
                return

            logger.info("[%s] LLM says agent name is '%s', updating display name", self.name, name)
            self._agent_name = name
            await self._room.local_participant.set_name(name)
        except Exception as e:
            logger.debug("[%s] Could not resolve agent name from LLM: %s", self.name, e)

    def _cleanup_participant(self, identity: str):
        """Remove buffers and cancel audio stream for a participant.

        If the participant was mid-utterance when the track went away
        (e.g. their mic dropped or — for file-based publishers — the
        clip ended), flush whatever speech has been buffered before
        discarding it, so the user's last words still reach STT.
        """
        # Flush a pending utterance, if any, before tearing buffers down.
        buf = self._audio_buffers.get(identity)
        if buf is not None and identity in self._speaking_participants and len(buf) > 0:
            # Use the whole buffer: the track just ended, so there is no
            # "ongoing silence" to trim — the buffer ends at (or very close
            # to) the last spoken word. The steady-state path in
            # _check_silence_loop trims trailing silence because the
            # participant is still connected; here, subtracting a fixed
            # silence window would chop real speech (or zero the flush
            # entirely) when the track ends right after a word. Trailing
            # silence in the audio handed to STT is harmless; lost words
            # are not.
            speech_end = len(buf)
            duration = speech_end / (SAMPLE_RATE * NUM_CHANNELS * 2)
            if duration >= self._min_speech_duration_seconds:
                pcm_data = bytes(buf[:speech_end])
                logger.info(
                    "[%s] Utterance from %s: %.1fs audio (flushed on track end)",
                    self.name, identity, duration,
                )
                try:
                    asyncio.create_task(
                        self._publish_agent_event(
                            "agent:listening-stop", {"identity": identity}
                        )
                    )
                    asyncio.create_task(self._process_voice_input(identity, pcm_data))
                except RuntimeError:
                    # No running event loop (e.g. during disconnect path) — skip flush.
                    pass

        task = self._audio_streams.pop(identity, None)
        if task:
            task.cancel()
        stream = self._audio_stream_handles.pop(identity, None)
        if stream is not None:
            try:
                asyncio.create_task(stream.aclose())
            except RuntimeError:
                pass
        self._audio_buffers.pop(identity, None)
        self._last_audio_time.pop(identity, None)
        self._speaking_participants.discard(identity)
        try:
            asyncio.create_task(self._finish_listening_state())
        except RuntimeError:
            pass

    async def _close_all_audio_streams(self) -> None:
        """Close all subscribed LiveKit audio streams."""
        for identity, stream in list(self._audio_stream_handles.items()):
            try:
                await stream.aclose()
            except Exception as e:
                logger.debug("[%s] audio stream close failed for %s: %s", self.name, identity, e)
        self._audio_stream_handles.clear()

    # -- Audio capture and processing ---------------------------------------

    async def _audio_receive_loop(
        self,
        stream: "rtc.AudioStream",
        identity: str,
    ):
        """Receive audio frames from a participant and buffer them.

        This loop must drain the SDK's internal queue as fast as possible
        to avoid 'native audio stream queue overflow' warnings.  All
        heavy processing (RMS, silence detection) happens in
        _check_silence_loop instead.
        """
        try:
            async for event in stream:
                if self._paused:
                    continue
                if identity not in self._audio_buffers:
                    break

                self._audio_buffers[identity].extend(event.frame.data.tobytes())
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.warning("[%s] Audio receive error for %s: %s", self.name, identity, e)

    @staticmethod
    def _video_track_key(track: Any, publication: Any, identity: str) -> str:
        return str(
            getattr(publication, "sid", "")
            or getattr(track, "sid", "")
            or f"{identity}:{id(track)}"
        )

    @staticmethod
    def _video_source_name(publication: Any) -> str:
        source = getattr(publication, "source", None)
        try:
            return str(rtc.TrackSource.Name(source)).lower()
        except Exception:
            return str(source or "video").lower()

    def _encode_video_frame(self, frame: Any) -> bytes:
        from livekit.rtc import VideoBufferType

        rgba = frame.convert(VideoBufferType.RGBA)
        img = Image.frombytes("RGBA", (rgba.width, rgba.height), bytes(rgba.data))
        img = img.convert("RGB")
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=82)
        return buf.getvalue()

    async def _video_receive_loop(
        self, track_key: str, stream: "rtc.VideoStream"
    ) -> None:
        """Continuously drain one video track and retain a throttled latest frame."""
        last_sample = 0.0
        try:
            async for frame_event in stream:
                now = time.monotonic()
                if now - last_sample < self._video_sample_interval_seconds:
                    continue
                last_sample = now
                meta = self._video_track_meta.get(track_key)
                if meta is None:
                    return
                identity, source_name = meta
                try:
                    frame = frame_event.frame
                    jpeg = self._encode_video_frame(frame)
                    self._latest_video_frames[track_key] = (
                        jpeg, now, source_name, frame.width, frame.height
                    )
                    logger.debug(
                        "[%s] sampled %dx%d %s frame from %s",
                        self.name, frame.width, frame.height, source_name, identity,
                    )
                except Exception as exc:
                    logger.debug("[%s] video sample encode failed: %s", self.name, exc)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.warning("[%s] Video receive error for %s: %s", self.name, track_key, exc)

    def _latest_frame_for_identity(
        self, identity: str
    ) -> Optional[tuple[bytes, float, str, int, int]]:
        candidates = []
        for key, frame_info in self._latest_video_frames.items():
            meta = self._video_track_meta.get(key)
            if meta is not None and (not identity or meta[0] == identity):
                candidates.append(frame_info)
        if not candidates and identity:
            # A text sender may not be the participant publishing the shared
            # screen. Fall back to the freshest room video.
            candidates = list(self._latest_video_frames.values())
        if not candidates:
            return None
        return max(candidates, key=lambda item: item[1])

    def _queue_latest_video_frame(self, identity: str) -> Optional[tuple[str, str]]:
        latest = self._latest_frame_for_identity(identity)
        if latest is None:
            return None
        jpeg, captured_at, source_name, _width, _height = latest
        if time.monotonic() - captured_at > self._video_frame_max_age_seconds:
            return None
        tmp_dir = os.path.join(tempfile.gettempdir(), "hermes_livekit")
        os.makedirs(tmp_dir, exist_ok=True)
        safe_source = re.sub(r"[^a-z0-9_-]+", "-", source_name.lower()).strip("-") or "video"
        path = os.path.join(
            tmp_dir, f"{safe_source}_{uuid.uuid4().hex[:12]}.jpg"
        )
        with open(path, "wb") as file_handle:
            file_handle.write(jpeg)
        capture = (path, "image/jpeg")
        self._pending_captures.append(capture)
        return capture

    @staticmethod
    def _should_attach_video(text: str) -> bool:
        """Avoid making ordinary voice turns pay the vision-analysis latency."""
        return bool(VISUAL_UTTERANCE_RE.search(text or ""))

    def _cleanup_participant_video(self, identity: str) -> None:
        keys = [
            key for key, meta in self._video_track_meta.items()
            if meta[0] == identity
        ]
        for key in keys:
            task = self._video_tasks.pop(key, None)
            if task is not None:
                task.cancel()
            stream = self._video_streams.pop(key, None)
            if stream is not None:
                try:
                    asyncio.create_task(stream.aclose())
                except Exception:
                    pass
            self._video_track_meta.pop(key, None)
            self._latest_video_frames.pop(key, None)

    def _interrupt_playback(self, identity: str) -> None:
        if not self._is_playing or not self._conversation_is_active():
            return
        logger.info("[%s] Barge-in detected from %s; stopping TTS", self.name, identity)
        self._playback_interrupt.set()
        if self._audio_source is not None:
            try:
                self._audio_source.clear_queue()
            except Exception:
                pass

    async def _set_listening_state(self, identity: str) -> None:
        self._active_speaker_identity = identity
        self._active_speaker_name = self._participant_display_name(identity)
        self._active_topic = self._participant_topics.get(identity, "")
        await self._set_agent_state("listening", force=True)

    async def _finish_listening_state(self) -> None:
        if self._speaking_participants:
            identity = next(iter(self._speaking_participants))
            await self._set_listening_state(identity)
        elif self._agent_state == "listening":
            await self._set_agent_state("idle")

    async def _check_silence_loop(self):
        """Periodically check for completed utterances (silence after speech).

        Each tick, we look at the tail of every participant's buffer to
        decide whether they are currently speaking or silent.  When
        silence exceeds the threshold, we extract the utterance and
        send it for transcription.

        Drops to a slower poll when no participants are buffered — saves
        CPU without delaying utterance detection (a joining participant
        will trigger ``_on_track_subscribed`` immediately, not on the
        next loop tick).
        """
        # bytes per poll interval (how much audio one tick represents)
        bytes_per_tick = int(SAMPLE_RATE * NUM_CHANNELS * 2 * POLL_INTERVAL)

        try:
            while self._running:
                # No one to listen to — sleep longer.
                if not self._audio_buffers:
                    await asyncio.sleep(IDLE_POLL_INTERVAL)
                    continue
                await asyncio.sleep(POLL_INTERVAL)

                for identity in list(self._audio_buffers.keys()):
                    buf = self._audio_buffers.get(identity)
                    if buf is None:
                        continue

                    buf_len = len(buf)
                    if buf_len == 0:
                        continue

                    # Check RMS of the most recent chunk to detect speech/silence
                    tail = bytes(buf[-bytes_per_tick:]) if buf_len >= bytes_per_tick else bytes(buf)
                    rms = _compute_rms(tail)

                    if rms > self._rms_silence_floor:
                        # Active speech — update timestamp
                        self._last_audio_time[identity] = time.monotonic()
                        # Emit listening-start on first loud chunk of an utterance
                        if identity not in self._speaking_participants:
                            self._speaking_participants.add(identity)
                            self._interrupt_playback(identity)
                            asyncio.create_task(
                                self._publish_agent_event(
                                    "agent:listening-start", {"identity": identity}
                                )
                            )
                            asyncio.create_task(self._set_listening_state(identity))
                        continue

                    # Silent — check if silence has lasted long enough
                    last_time = self._last_audio_time.get(identity)
                    if last_time is None:
                        # Never spoke — discard accumulated noise
                        self._audio_buffers[identity] = bytearray()
                        continue

                    elapsed_silence = time.monotonic() - last_time
                    if elapsed_silence < self._silence_threshold_seconds:
                        continue

                    # Trim trailing silence from the buffer (keep only up to
                    # SILENCE_THRESHOLD worth of trailing audio)
                    silence_bytes = int(self._silence_threshold_seconds * SAMPLE_RATE * NUM_CHANNELS * 2)
                    speech_end = max(0, buf_len - silence_bytes)

                    duration = speech_end / (SAMPLE_RATE * NUM_CHANNELS * 2)
                    if duration < self._min_speech_duration_seconds:
                        # Too short — discard as noise
                        self._audio_buffers[identity] = bytearray()
                        self._last_audio_time.pop(identity, None)
                        # False alarm — revert the listening-start we sent
                        if identity in self._speaking_participants:
                            self._speaking_participants.discard(identity)
                            asyncio.create_task(
                                self._publish_agent_event(
                                    "agent:listening-stop", {"identity": identity}
                                )
                            )
                            asyncio.create_task(self._finish_listening_state())
                        continue

                    # Extract the utterance (speech portion only) and reset
                    pcm_data = bytes(buf[:speech_end])
                    self._audio_buffers[identity] = bytearray()
                    self._last_audio_time.pop(identity, None)
                    self._speaking_participants.discard(identity)
                    asyncio.create_task(
                        self._publish_agent_event(
                            "agent:listening-stop", {"identity": identity}
                        )
                    )
                    asyncio.create_task(self._finish_listening_state())

                    logger.info("[%s] Utterance from %s: %.1fs audio", self.name, identity, duration)
                    asyncio.create_task(self._process_voice_input(identity, pcm_data))
        except asyncio.CancelledError:
            return

    async def _process_voice_input(self, identity: str, pcm_data: bytes):
        """Transcribe audio and feed into the agent loop."""
        try:
            # Write PCM to WAV temp file
            wav_data = _pcm_to_wav(pcm_data, SAMPLE_RATE, NUM_CHANNELS)
            tmp_dir = os.path.join(tempfile.gettempdir(), "hermes_livekit")
            os.makedirs(tmp_dir, exist_ok=True)
            wav_path = os.path.join(tmp_dir, f"utterance_{uuid.uuid4().hex[:8]}.wav")
            with open(wav_path, "wb") as f:
                f.write(wav_data)

            # Transcribe using hermes STT pipeline. transcribe_audio resolves
            # the model from stt config internally when called with no model
            # arg — same pattern other gateway adapters use.
            from tools.transcription_tools import transcribe_audio
            # The local Whisper backend is not safe or fast under overlapping
            # calls; serialize utterances to avoid the 20s+ stalls seen in logs.
            async with self._stt_lock:
                result = await asyncio.to_thread(transcribe_audio, wav_path)

            # Clean up temp file
            try:
                os.unlink(wav_path)
            except OSError:
                pass

            logger.info("[%s] STT result from %s: %s", self.name, identity, result)
            transcript = (result.get("transcript") or result.get("text") or "").strip() if isinstance(result, dict) else ""
            if not transcript:
                logger.info("[%s] Empty transcript from %s, skipping", self.name, identity)
                return

            logger.info("[%s] Transcript from %s: %s", self.name, identity, transcript[:80])

            # Drain any captured frames into this message so the agent's
            # vision pipeline sees them alongside the transcript.
            if self._auto_vision and self._should_attach_video(transcript):
                self._queue_latest_video_frame(identity)
            media_urls, media_types = self._drain_pending_captures()

            # Build message event
            source = self.build_source(
                chat_id=self._room_name,
                chat_name=self._room_name,
                chat_type="group",
                user_id=identity,
                user_name=identity,
            )

            event = MessageEvent(
                text=transcript,
                message_type=MessageType.VOICE,
                source=source,
                message_id=uuid.uuid4().hex[:12],
                media_urls=media_urls,
                media_types=media_types,
                timestamp=datetime.now(tz=timezone.utc),
            )

            await self.handle_message(event)
        except Exception as e:
            logger.error("[%s] Error processing voice from %s: %s", self.name, identity, e)

    # -- Inbound data channel + frame capture -------------------------------

    # Topic clients send control messages on. Outbound topics (hermes-chat,
    # untopic-ed agent:* lifecycle events) are unchanged.
    DATA_CHANNEL_CONTROL_TOPIC = "hermes-control"

    def _on_data_received(self, packet) -> None:
        """Route inbound data-channel packets.
        Called synchronously by the SDK's event thread; heavy work is
        kicked off as asyncio tasks. JSON payloads on the
        ``hermes-control`` topic are dispatched by their ``type`` field;
        anything else is ignored (silently — keeps the protocol open for
        unrelated apps sharing the same data channel without spamming logs).
        """
        topic = getattr(packet, "topic", None) or ""
        # Handle all topics - browser chatbox sends text on any topic
        participant = getattr(packet, "participant", None)
        participant_identity = str(
            getattr(participant, "identity", "") if participant is not None else ""
        ).strip()

        raw_payload = b""
        try:
            raw_payload = bytes(packet.data)
        except Exception:
            raw_payload = b""
        raw_text = raw_payload.decode("utf-8", errors="replace").strip()

        try:
            import json as _json
            msg = _json.loads(raw_text) if raw_text.startswith(("{", "[")) else None
        except (UnicodeDecodeError, ValueError):
            msg = None

        # Check if this is a text message (from browser chatbox) vs control data.
        # Support both JSON envelopes and plain string payloads because browser
        # textbox clients may publish raw text directly on the data channel.
        msg_type = msg.get("type", "") if isinstance(msg, dict) else ""
        text_content = ""
        if isinstance(msg, dict):
            text_content = (
                str(msg.get("content") or msg.get("text") or msg.get("message") or "")
            ).strip()
        elif raw_text:
            text_content = raw_text

        is_text_message = bool(text_content) or msg_type == "text"
        if not msg_type and text_content:
            msg_type = "text"

        # A sender-less packet can occur if a newly connected participant
        # publishes before its participant update reaches this client. Never
        # collapse such turns into a shared synthetic "client" identity: that
        # would corrupt multi-speaker context and weaken authorization. The
        # sender can retry once room synchronization completes.
        if (msg_type or text_content) and not participant_identity:
            logger.warning("[%s] dropping data packet without participant identity", self.name)
            return

        # Dispatch table. Keep additions here so adding new client:* types
        # is a single line.
        handlers = {
            "client:capture-frame": lambda: self._capture_next_frame(participant_identity),
            "client:message": lambda: self._handle_client_message(msg, participant_identity),
            "client:control": lambda: self._handle_client_control(msg, participant_identity),
            "client:tool-register": lambda: self._register_client_tool(msg, participant_identity),
            "client:tool-unregister": lambda: self._unregister_client_tool(msg, participant_identity),
            "client:tool-result": lambda: self._handle_tool_result(msg, participant_identity),
        }

        handler = handlers.get(msg_type)
        if handler is not None:
            try:
                asyncio.create_task(handler())
            except RuntimeError:
                # No running loop (callback fired during teardown). Drop quietly.
                pass
            return

        # Handle direct text messages (browser chatbox input).
        if is_text_message and msg_type == "text":
            if self._auto_vision and self._should_attach_video(text_content):
                self._queue_latest_video_frame(participant_identity)
            media_urls, media_types = self._drain_pending_captures()
            asyncio.create_task(self.handle_message(MessageEvent(
                text=text_content,
                message_type=MessageType.TEXT,
                source=self.build_source(
                    chat_id=self._room_name,
                    chat_name=self._room_name,
                    chat_type="direct",
                    user_id=participant_identity,
                    user_name=participant_identity,
                ),
                message_id=(
                    msg.get("id", uuid.uuid4().hex[:12])
                    if isinstance(msg, dict)
                    else uuid.uuid4().hex[:12]
                ),
                media_urls=media_urls,
                media_types=media_types,
                timestamp=datetime.now(tz=timezone.utc),
            )))
            return

        logger.debug(
            "[%s] unknown control type %r from %s",
            self.name,
            msg_type,
            participant_identity or "?",
        )

    async def _capture_next_frame(self, identity: str) -> None:
        """Sample the very next video frame from ``identity`` and queue it.

        Only one frame per call — option C semantics (no continuous
        decoding). If the participant has no video track subscribed yet,
        emit ``agent:frame-capture-failed`` so the client knows.
        """
        if not PIL_AVAILABLE:
            await self._publish_agent_event(
                "agent:frame-capture-failed",
                {"reason": "pillow-not-installed"},
            )
            return

        latest = self._latest_frame_for_identity(identity)
        if latest is None:
            logger.info("[%s] capture-frame from %s but no video track subscribed", self.name, identity)
            await self._publish_agent_event(
                "agent:frame-capture-failed",
                {"reason": "no-video-track", "identity": identity},
            )
            return

        queued = self._queue_latest_video_frame(identity)
        if queued is None:
            await self._publish_agent_event(
                "agent:frame-capture-failed",
                {"reason": "stale-frame", "identity": identity},
            )
            return
        path, _mime = queued
        jpeg_bytes, _captured_at, source_name, width, height = latest
        logger.info(
            "[%s] captured latest %dx%d %s frame from %s (%d bytes) — pending=%d",
            self.name, width, height, source_name, identity,
            len(jpeg_bytes), len(self._pending_captures),
        )
        await self._publish_agent_event(
            "agent:frame-captured",
            {
                "identity": identity,
                "width": width,
                "height": height,
                "source": source_name,
                "path": path,
                "bytes": len(jpeg_bytes),
                "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            },
        )

    def _on_image_stream(self, reader: Any, participant_identity: str) -> None:
        """Accept still images using the same byte-stream pattern as MiRA's sample."""
        task = asyncio.create_task(
            self._receive_image_stream(reader, str(participant_identity or "client"))
        )
        self._tool_ack_tasks.add(task)
        task.add_done_callback(self._tool_ack_tasks.discard)

    async def _receive_image_stream(self, reader: Any, identity: str) -> None:
        data = bytearray()
        try:
            async for chunk in reader:
                data.extend(bytes(chunk))
                if len(data) > MAX_IMAGE_STREAM_BYTES:
                    raise ValueError("image stream exceeds 12 MiB limit")
            if not data:
                return
            info = getattr(reader, "info", None)
            mime = str(getattr(info, "mime_type", "") or "image/png")
            suffix = ".jpg" if "jpeg" in mime or "jpg" in mime else ".png"
            tmp_dir = os.path.join(tempfile.gettempdir(), "hermes_livekit")
            os.makedirs(tmp_dir, exist_ok=True)
            path = os.path.join(tmp_dir, f"stream_{uuid.uuid4().hex[:12]}{suffix}")
            with open(path, "wb") as file_handle:
                file_handle.write(data)
            self._pending_captures.append((path, mime))
            logger.info(
                "[%s] image byte stream received from %s (%d bytes, %s)",
                self.name, identity, len(data), mime,
            )
            await self._publish_agent_event(
                "agent:frame-captured",
                {"identity": identity, "bytes": len(data), "source": "byte-stream"},
            )
        except Exception as exc:
            logger.warning("[%s] image byte stream failed from %s: %s", self.name, identity, exc)
            await self._publish_agent_event(
                "agent:frame-capture-failed",
                {"reason": "byte-stream-error", "identity": identity, "detail": str(exc)},
            )

    async def _handle_client_message(self, msg: Dict[str, Any], identity: str) -> None:
        """Inject a typed text message as if it were a transcribed voice utterance.

        Useful for clients that want to text-chat with the agent over the
        LiveKit data channel (no STT needed). Any pending captures attach
        to this message, same as the voice path.
        """
        text = (msg.get("text") or "").strip()
        if not text:
            return

        if self._auto_vision and self._should_attach_video(text):
            self._queue_latest_video_frame(identity)
        media_urls, media_types = self._drain_pending_captures()

        source = self.build_source(
            chat_id=self._room_name,
            chat_name=self._room_name,
            chat_type="group",
            user_id=identity or "client",
            user_name=identity or "client",
        )
        event = MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            source=source,
            message_id=uuid.uuid4().hex[:12],
            media_urls=media_urls,
            media_types=media_types,
            timestamp=datetime.now(tz=timezone.utc),
        )

        await self.handle_message(event)

    async def _handle_client_control(self, msg: Dict[str, Any], identity: str) -> None:
        """Runtime control hooks from the client. Placeholder for now.

        Currently recognized actions:
          - ``pause``  — stop sampling inbound audio (already used internally
            during TTS playback); kept here as an explicit client-facing hook
            for future "mute me" UX.
          - ``resume`` — re-enable audio sampling.
        """
        action = (msg.get("action") or "").strip().lower()
        if action == "pause":
            self._paused = True
            logger.info("[%s] paused by client %s", self.name, identity)
        elif action == "resume":
            self._paused = False
            logger.info("[%s] resumed by client %s", self.name, identity)
        else:
            logger.debug("[%s] unknown client:control action %r", self.name, action)

    # -- Remote tools (client-registered) -----------------------------------

    async def _publish_typed(
        self,
        msg: Dict[str, Any],
        *,
        identity: Optional[str] = None,
        topic: str = "",
    ) -> None:
        """Publish a flat-envelope JSON message; optionally target one participant.

        Unlike _publish_agent_event (which wraps payload in {type, payload}),
        the remote-tool protocol is flat by spec — every field at top level.
        """
        if not self._room:
            return
        import json as _json
        try:
            data = _json.dumps(msg).encode("utf-8")
            dest = [identity] if identity else []
            await self._room.local_participant.publish_data(
                data, reliable=True, topic=topic, destination_identities=dest,
            )
        except Exception as exc:
            logger.debug("[%s] typed publish failed (%s): %s", self.name, msg.get("type"), exc)

    async def _register_client_tool(self, msg: Dict[str, Any], identity: str) -> None:
        name = (msg.get("name") or "").strip()
        description = msg.get("description") or ""
        input_schema = msg.get("input_schema")

        if not identity:
            return

        if not TOOL_NAME_RE.match(name):
            await self._publish_typed(
                {"type": "agent:tool-registered", "name": name, "success": False, "reason": "name-invalid"},
                identity=identity,
            )
            return

        if not isinstance(input_schema, dict) or input_schema.get("type") != "object":
            await self._publish_typed(
                {"type": "agent:tool-registered", "name": name, "success": False, "reason": "schema-invalid"},
                identity=identity,
            )
            return

        try:
            from tools.registry import registry
        except Exception as exc:
            logger.error("[%s] tool registry unavailable: %s", self.name, exc)
            await self._publish_typed(
                {"type": "agent:tool-registered", "name": name, "success": False, "reason": "registry-unavailable"},
                identity=identity,
            )
            return

        handler = self._build_tool_handler(identity, name)
        # The registry's `schema` is the OpenAI function-envelope shape
        # (`{name, description, parameters}`), not a bare JSON Schema. Wrap
        # the client-supplied input_schema accordingly.
        registry_schema = {
            "name": name,
            "description": description,
            "parameters": input_schema,
        }
        try:
            # override=True so a reconnecting client can re-register without
            # an explicit unregister round-trip. Single-client v1 — collisions
            # between distinct clients are undefined per design doc.
            registry.register(
                name=name,
                toolset=TOOLSET_NAME,
                schema=registry_schema,
                handler=handler,
                is_async=True,
                description=description,
                override=True,
            )
        except Exception as exc:
            logger.warning("[%s] tool register %r failed: %s", self.name, name, exc)
            await self._publish_typed(
                {
                    "type": "agent:tool-registered",
                    "name": name,
                    "success": False,
                    "reason": "register-failed",
                    "detail": str(exc),
                },
                identity=identity,
            )
            return

        self._client_tools.setdefault(identity, set()).add(name)
        self._tool_owners[name] = identity
        logger.info("[%s] client %s registered tool %r", self.name, identity, name)
        await self._publish_typed(
            {"type": "agent:tool-registered", "name": name, "success": True},
            identity=identity,
        )

    async def _unregister_client_tool(self, msg: Dict[str, Any], identity: str) -> None:
        name = (msg.get("name") or "").strip()
        owner = self._tool_owners.get(name)
        if owner != identity:
            await self._publish_typed(
                {"type": "agent:tool-unregistered", "name": name, "success": False, "reason": "not-owned-by-you"},
                identity=identity,
            )
            return
        self._deregister_tool(name, identity)
        await self._publish_typed(
            {"type": "agent:tool-unregistered", "name": name, "success": True},
            identity=identity,
        )

    async def _handle_tool_result(self, msg: Dict[str, Any], identity: str) -> None:
        call_id = (msg.get("call_id") or "").strip()
        if not call_id:
            return
        future = self._pending_tool_calls.pop(call_id, None)
        self._pending_tool_owners.pop(call_id, None)
        if future is None or future.done():
            # Late result for a cancelled/timed-out call — ignore.
            logger.debug("[%s] tool-result for unknown call_id %r", self.name, call_id)
            return
        if "error" in msg:
            future.set_exception(RuntimeError(str(msg.get("error") or "tool reported error")))
        else:
            future.set_result(msg.get("result"))

    def _build_tool_handler(self, owner_identity: str, registered_name: str):
        """Return an async fn that hermes will call when the LLM picks this tool.

        Signature matches the hermes ToolRegistry contract:
        ``handler(args_dict, **kwargs)`` — first positional is the LLM-supplied
        arguments object, kwargs are framework extras we pass through.
        """

        async def proxy(args: Optional[Dict[str, Any]] = None, **_kwargs: Any) -> Any:
            arguments: Dict[str, Any] = dict(args or {})
            if not self._room or owner_identity not in self._room.remote_participants:
                raise RuntimeError(
                    f"client {owner_identity!r} who registered {registered_name!r} is not connected"
                )
            call_id = f"tc_{uuid.uuid4().hex[:12]}"
            loop = asyncio.get_running_loop()
            future = loop.create_future()
            self._pending_tool_calls[call_id] = future
            self._pending_tool_owners[call_id] = owner_identity
            try:
                await self._publish_typed(
                    {
                        "type": "agent:tool-call",
                        "call_id": call_id,
                        "name": registered_name,
                        "arguments": arguments,
                    },
                    identity=owner_identity,
                )
                return await asyncio.wait_for(future, timeout=self._tool_call_timeout)
            except asyncio.TimeoutError:
                await self._publish_typed(
                    {"type": "agent:tool-call-timeout", "call_id": call_id, "name": registered_name},
                    identity=owner_identity,
                )
                raise RuntimeError(
                    f"remote tool {registered_name!r} timed out after {self._tool_call_timeout:.0f}s"
                )
            except asyncio.CancelledError:
                # Agent loop is unwinding — let the client abort.
                await self._publish_typed(
                    {"type": "agent:tool-call-cancelled", "call_id": call_id, "name": registered_name},
                    identity=owner_identity,
                )
                raise
            finally:
                self._pending_tool_calls.pop(call_id, None)
                self._pending_tool_owners.pop(call_id, None)

        return proxy

    def _deregister_tool(self, name: str, identity: str) -> None:
        """Remove a single tool from the hermes registry and our maps."""
        try:
            from tools.registry import registry
            registry.deregister(name)
        except Exception as exc:
            logger.debug("[%s] tool deregister %r failed: %s", self.name, name, exc)
        self._tool_owners.pop(name, None)
        tools = self._client_tools.get(identity)
        if tools:
            tools.discard(name)
            if not tools:
                self._client_tools.pop(identity, None)

    def _cleanup_client_tools(self, identity: str) -> None:
        """Deregister all tools owned by ``identity`` and fail their pending calls."""
        for name in list(self._client_tools.get(identity, set())):
            self._deregister_tool(name, identity)
        for call_id in list(self._pending_tool_owners.keys()):
            if self._pending_tool_owners.get(call_id) != identity:
                continue
            future = self._pending_tool_calls.pop(call_id, None)
            self._pending_tool_owners.pop(call_id, None)
            if future is not None and not future.done():
                future.set_exception(
                    RuntimeError(f"client {identity!r} disconnected mid-call")
                )

    def _cleanup_all_client_tools(self) -> None:
        """Deregister every client-offered tool and fail every pending call."""
        for identity in list(self._client_tools.keys()):
            self._cleanup_client_tools(identity)
        # Belt + braces — anything not keyed by a tracked identity.
        for call_id, future in list(self._pending_tool_calls.items()):
            if not future.done():
                future.set_exception(RuntimeError("livekit adapter shutting down"))
        self._pending_tool_calls.clear()
        self._pending_tool_owners.clear()
        self._client_tools.clear()
        self._tool_owners.clear()

    def cancel_pending_tool_calls_for_session_reset(self) -> int:
        """Fail in-flight remote tool calls and tell the owning clients.

        Called from the plugin's ``on_session_finalize`` hook (and the
        upstream ``agent_loop_stopped`` hook, once that lands). The agent
        loop is gone but our proxy coroutines are blocked on the result
        future — without this, they'd hang until the call's timeout (or
        until the client responds to a call the agent no longer cares
        about). Tool *registrations* stay intact — only the in-flight
        invocations are cancelled.

        Returns the number of calls that were cancelled.
        """
        if not self._pending_tool_calls:
            return 0
        cancelled = 0
        for call_id, future in list(self._pending_tool_calls.items()):
            owner = self._pending_tool_owners.get(call_id, "")
            if owner:
                # Best-effort notification — schedule on the same loop the
                # adapter runs on. If no loop is running, the publish just
                # gets skipped (the future-failure path still runs).
                try:
                    asyncio.create_task(
                        self._publish_typed(
                            {
                                "type": "agent:tool-call-cancelled",
                                "call_id": call_id,
                                "reason": "session-reset",
                            },
                            identity=owner,
                        )
                    )
                except RuntimeError:
                    pass  # no running loop
            if not future.done():
                future.set_exception(
                    RuntimeError("agent session reset; tool call abandoned")
                )
            cancelled += 1
        self._pending_tool_calls.clear()
        self._pending_tool_owners.clear()
        return cancelled

    def _drain_pending_captures(self) -> tuple[list[str], list[str]]:
        """Pop all buffered frame paths into parallel (urls, types) lists.

        Temp files are NOT unlinked here — the hermes agent loop reads them
        after handle_message returns (the dispatch is fire-and-forget). The
        files live under <tempdir>/hermes_livekit/ and are cleaned up on
        disconnect; OS tempdir housekeeping handles anything we miss.
        """
        urls: list[str] = []
        types: list[str] = []
        while self._pending_captures:
            path, mime = self._pending_captures.pop(0)
            urls.append(path)
            types.append(mime)
        return urls, types

    # -- Outbound messaging -------------------------------------------------

    async def _publish_agent_event(
        self, event_type: str, payload: Optional[Dict[str, Any]] = None
    ) -> None:
        """Publish an agent:* lifecycle event as JSON on the default data topic.

        Consumed by voice-agent.desktop (and any compatible client) to drive
        UI state — listening/thinking/speaking indicators and live transcript
        display. Topic is deliberately unset: the desktop client routes
        messages with no topic (or any topic other than "hermes-chat") to its
        JSON/event handler.
        """
        if not self._room:
            return
        try:
            import json as _json
            msg = {"type": event_type, "payload": payload or {}}
            await self._room.local_participant.publish_data(
                _json.dumps(msg).encode("utf-8"), reliable=True
            )
        except Exception as e:
            # Never let UI telemetry break the voice flow.
            logger.debug("[%s] agent event publish failed (%s): %s", self.name, event_type, e)

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send text via data channel (best-effort for connected web clients)."""
        if not self._room:
            return SendResult(success=False, error="Not connected to room")

        # A normal response closes the acknowledgement window. Telemetry and
        # tool cues are explicitly non-conversational and must not do so.
        if not (metadata or {}).get("non_conversational"):
            self._finish_tool_acknowledgement_turn()

        try:
            data = content.encode("utf-8")
            await self._room.local_participant.publish_data(
                data, reliable=True, topic="hermes-chat"
            )
            # Mirror the content as an agent-transcript event so clients that
            # render a conversation log can add an assistant message.
            await self._publish_agent_event(
                "agent:agent-transcript", {"transcript": content, "final": True}
            )
            return SendResult(success=True, message_id=uuid.uuid4().hex[:12])
        except Exception as e:
            logger.debug("[%s] Data channel send failed (non-critical): %s", self.name, e)
            # Not a failure — voice is the primary channel
            return SendResult(success=True, message_id=uuid.uuid4().hex[:12])

    async def play_tts(
        self,
        chat_id: str,
        audio_path: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        caption: Optional[str] = None,
    ) -> SendResult:
        """Play TTS audio into the LiveKit room via the published audio track."""
        if not self._audio_source or not self._room:
            return SendResult(success=False, error="Not connected to room")

        async with self._playback_lock:
            speaking_generation = 0
            try:
                self._playback_interrupt.clear()
                self._is_playing = True

                if caption:
                    await self._publish_agent_event(
                        "agent:agent-transcript",
                        {"transcript": caption, "final": True},
                    )

                pcm_data = await asyncio.to_thread(
                    self._decode_audio_to_pcm, audio_path
                )
                if not pcm_data:
                    return SendResult(success=False, error="Failed to decode audio")

                samples_per_frame = SAMPLE_RATE // 50  # 20ms frames
                bytes_per_frame = samples_per_frame * NUM_CHANNELS * 2
                speaking_generation = await self._set_agent_state("speaking", force=True)
                await self._publish_agent_event("agent:speaking-start")

                offset = 0
                while offset < len(pcm_data):
                    if self._playback_interrupt.is_set():
                        try:
                            self._audio_source.clear_queue()
                        except Exception:
                            pass
                        await self._publish_agent_event(
                            "agent:speech-interrupted", {"reason": "user-barge-in"}
                        )
                        break
                    chunk = pcm_data[offset:offset + bytes_per_frame]
                    if len(chunk) < bytes_per_frame:
                        chunk += b"\x00" * (bytes_per_frame - len(chunk))
                    frame = rtc.AudioFrame(
                        data=chunk,
                        sample_rate=SAMPLE_RATE,
                        num_channels=NUM_CHANNELS,
                        samples_per_channel=samples_per_frame,
                    )
                    await self._audio_source.capture_frame(frame)
                    offset += bytes_per_frame

                return SendResult(success=True, message_id=uuid.uuid4().hex[:12])
            except Exception as e:
                logger.error("[%s] TTS playback error: %s", self.name, e)
                return SendResult(success=False, error=str(e))
            finally:
                self._is_playing = False
                await self._publish_agent_event("agent:speaking-stop")
                if speaking_generation:
                    await self._finish_state_if_current(speaking_generation)

    @staticmethod
    def _decode_audio_to_pcm(audio_path: str) -> Optional[bytes]:
        """Decode an audio file to raw 16-bit PCM using ffmpeg."""
        try:
            result = subprocess.run(
                [
                    "ffmpeg", "-i", audio_path,
                    "-f", "s16le",        # raw 16-bit little-endian PCM
                    "-acodec", "pcm_s16le",
                    "-ar", str(SAMPLE_RATE),
                    "-ac", str(NUM_CHANNELS),
                    "-loglevel", "error",
                    "pipe:1",
                ],
                capture_output=True,
                timeout=30,
            )
            if result.returncode != 0:
                logger.warning("ffmpeg decode failed: %s", result.stderr.decode()[:200])
                return None
            return result.stdout
        except FileNotFoundError:
            logger.warning("ffmpeg not found — required for LiveKit TTS playback")
            return None
        except Exception as e:
            logger.warning("Audio decode error: %s", e)
            return None

    def prepare_tts_text(self, text: str) -> str:
        """Strip tool output, code blocks, URLs, and file paths for voice.

        The full response is already sent via data channel — TTS should
        only speak the conversational parts.

        Overrides ``BasePlatformAdapter.prepare_tts_text``, which upstream
        calls in the auto-TTS path (the hook landed via NousResearch/
        hermes-agent#27308). The base default does a basic markdown strip;
        this override additionally removes code fences, inline code, URLs,
        file paths, and MEDIA: tags.
        """
        import re as _re

        # Remove fenced code blocks (```...```)
        text = _re.sub(r'```[\s\S]*?```', '', text)

        # Remove inline code (`...`)
        text = _re.sub(r'`[^`]+`', '', text)

        # Remove URLs
        text = _re.sub(r'https?://\S+', '', text)

        # Remove file paths (/foo/bar, ~/foo, C:\foo)
        text = _re.sub(r'(?:~|/|[A-Z]:\\)[\w./\\-]+', '', text)

        # Remove MEDIA: tags
        text = _re.sub(r'MEDIA:\S+', '', text)

        # Remove markdown formatting
        text = _re.sub(r'[*_`#\[\]()]', '', text)

        # Collapse whitespace
        text = _re.sub(r'\n{3,}', '\n\n', text)
        text = _re.sub(r'  +', ' ', text)

        return text[:4000].strip()

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        """Mirror Discord's working indicator through LiveKit agent state."""
        await self._set_agent_state("thinking")

    async def stop_typing(self, chat_id: str) -> None:
        """Clear the working state after Hermes completes the turn."""
        if self._agent_state == "thinking" and not self._is_playing:
            await self._set_agent_state("idle")

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """Return info about the LiveKit room."""
        participants = []
        if self._room:
            for p in self._room.remote_participants.values():
                participants.append(p.identity)
        return {
            "name": self._room_name,
            "type": "group",
            "chat_id": chat_id,
            "participants": participants,
        }

