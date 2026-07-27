"""Authenticated control plane for a MiRA-managed Hermes gateway.

The Next.js portal talks to this service from its server-side API routes.  The
bridge intentionally exposes only an allow-listed set of Hermes/LiveKit
settings and never returns credential values.
"""

from __future__ import annotations

import argparse
import ctypes
import hmac
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

try:
    import yaml
except ImportError:  # pragma: no cover - setup installs PyYAML.
    yaml = None

LOG = logging.getLogger("mira.hermes_control")
ROOM_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
AGENT_RE = re.compile(r"^[^\r\n]{1,80}$")

ENV_FIELDS = {
    "livekit_url": "LIVEKIT_URL",
    "livekit_api_key": "LIVEKIT_API_KEY",
    "livekit_api_secret": "LIVEKIT_API_SECRET",
    "room": "LIVEKIT_ROOM",
    "agent_name": "LIVEKIT_AGENT_NAME",
    "allow_all_users": "LIVEKIT_ALLOW_ALL_USERS",
    "auto_vision": "HERMES_LIVEKIT_AUTO_VISION",
    "video_sample_seconds": "HERMES_LIVEKIT_VIDEO_SAMPLE_SECONDS",
    "video_max_age_seconds": "HERMES_LIVEKIT_VIDEO_MAX_AGE_SECONDS",
    "silence_seconds": "HERMES_LIVEKIT_SILENCE_SECONDS",
    "work_ack_seconds": "HERMES_LIVEKIT_WORK_ACK_SECONDS",
    "notify_interval_seconds": "HERMES_AGENT_NOTIFY_INTERVAL",
}

PUBLIC_ENV_FIELDS = {
    "livekit_url",
    "room",
    "agent_name",
    "allow_all_users",
    "auto_vision",
    "video_sample_seconds",
    "video_max_age_seconds",
    "silence_seconds",
    "work_ack_seconds",
    "notify_interval_seconds",
}

BOOL_FIELDS = {"allow_all_users", "auto_vision"}
NUMBER_RANGES = {
    "video_sample_seconds": (0.1, 30.0),
    "video_max_age_seconds": (1.0, 120.0),
    "silence_seconds": (0.2, 5.0),
    "work_ack_seconds": (0.0, 60.0),
    "notify_interval_seconds": (5.0, 3600.0),
}


def pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        process_query_limited_information = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            process_query_limited_information, False, pid
        )
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return ctypes.windll.kernel32.GetLastError() == 5  # Access denied proves it exists.
    try:  # pragma: no cover - Windows is the supported host.
        os.kill(pid, 0)
        return True
    except OSError:
        return False


class BridgeError(RuntimeError):
    def __init__(self, message: str, status: int = HTTPStatus.BAD_REQUEST):
        super().__init__(message)
        self.status = int(status)


def read_dotenv(path: Path) -> tuple[list[str], dict[str, str]]:
    lines = path.read_text(encoding="utf-8-sig").splitlines() if path.exists() else []
    values: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return lines, values


def update_dotenv(path: Path, changes: dict[str, str]) -> None:
    lines, _ = read_dotenv(path)
    positions: dict[str, int] = {}
    for index, line in enumerate(lines):
        if "=" in line and not line.lstrip().startswith("#"):
            positions[line.split("=", 1)[0].strip()] = index
    for key, value in changes.items():
        rendered = f"{key}={value}"
        if key in positions:
            lines[positions[key]] = rendered
        else:
            positions[key] = len(lines)
            lines.append(rendered)

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
        shutil.copy2(path, path.with_name(f"{path.name}.portal-{stamp}.bak"))
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", newline="\n", dir=path.parent, delete=False
    ) as handle:
        handle.write("\n".join(lines))
        handle.write("\n")
        temporary = Path(handle.name)
    os.replace(temporary, path)


def parse_bool(value: Any, field: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
        return value.strip().lower() == "true"
    raise BridgeError(f"{field} must be true or false")


def validate_url(value: Any) -> str:
    if not isinstance(value, str):
        raise BridgeError("livekit_url must be a string")
    rendered = value.strip()
    parsed = urlparse(rendered)
    if parsed.scheme not in {"ws", "wss"} or not parsed.hostname:
        raise BridgeError("livekit_url must be an absolute ws:// or wss:// URL")
    if parsed.username or parsed.password:
        raise BridgeError("livekit_url must not contain embedded credentials")
    return rendered


def validate_config(payload: Any) -> dict[str, str]:
    if not isinstance(payload, dict):
        raise BridgeError("JSON object required")
    unknown = set(payload) - (set(ENV_FIELDS) | {"livekit_enabled", "auto_tts"})
    if unknown:
        raise BridgeError(f"Unsupported configuration field(s): {', '.join(sorted(unknown))}")

    changes: dict[str, str] = {}
    for field, value in payload.items():
        if value is None or value == "":
            if field in {"livekit_api_key", "livekit_api_secret"}:
                continue
            raise BridgeError(f"{field} cannot be empty")
        if field == "livekit_url":
            changes[ENV_FIELDS[field]] = validate_url(value)
        elif field == "room":
            if not isinstance(value, str) or not ROOM_RE.fullmatch(value.strip()):
                raise BridgeError("room contains unsupported characters or is too long")
            changes[ENV_FIELDS[field]] = value.strip()
        elif field == "agent_name":
            if not isinstance(value, str) or not AGENT_RE.fullmatch(value.strip()):
                raise BridgeError("agent_name must be 1-80 characters without newlines")
            changes[ENV_FIELDS[field]] = value.strip()
        elif field in {"livekit_api_key", "livekit_api_secret"}:
            if not isinstance(value, str) or len(value.strip()) < 3 or "\n" in value:
                raise BridgeError(f"{field} is invalid")
            changes[ENV_FIELDS[field]] = value.strip()
        elif field in BOOL_FIELDS:
            changes[ENV_FIELDS[field]] = str(parse_bool(value, field)).lower()
        elif field in NUMBER_RANGES:
            try:
                number = float(value)
            except (TypeError, ValueError):
                raise BridgeError(f"{field} must be a number") from None
            minimum, maximum = NUMBER_RANGES[field]
            if not minimum <= number <= maximum:
                raise BridgeError(f"{field} must be between {minimum:g} and {maximum:g}")
            changes[ENV_FIELDS[field]] = f"{number:g}"
        elif field in {"livekit_enabled", "auto_tts"}:
            parse_bool(value, field)
    return changes


@dataclass
class HermesController:
    home: Path

    @property
    def env_path(self) -> Path:
        return self.home / ".env"

    @property
    def config_path(self) -> Path:
        return self.home / "config.yaml"

    @property
    def state_path(self) -> Path:
        return self.home / "gateway_state.json"

    @property
    def hermes_exe(self) -> Path:
        candidate = self.home / "hermes-agent" / "venv" / "Scripts" / "hermes.exe"
        return candidate if candidate.exists() else Path("hermes")

    def _environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        environment["HERMES_HOME"] = str(self.home)
        _, values = read_dotenv(self.env_path)
        environment.update(values)
        return environment

    def command(self, *arguments: str, check: bool = True, timeout: float = 30) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(
                [str(self.hermes_exe), *arguments],
                env=self._environment(),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise BridgeError(f"Hermes command failed: {exc}", HTTPStatus.BAD_GATEWAY) from exc
        if check and result.returncode:
            detail = (result.stderr or result.stdout or "unknown Hermes error").strip()
            raise BridgeError(f"Hermes command failed: {detail}", HTTPStatus.BAD_GATEWAY)
        return result

    def _config(self) -> dict[str, Any]:
        if not self.config_path.exists() or yaml is None:
            return {}
        try:
            parsed = yaml.safe_load(self.config_path.read_text(encoding="utf-8-sig"))
            return parsed if isinstance(parsed, dict) else {}
        except Exception as exc:
            LOG.warning("Could not read Hermes config: %s", exc)
            return {}

    def safe_config(self) -> dict[str, Any]:
        _, values = read_dotenv(self.env_path)
        config = self._config()
        livekit = ((config.get("platforms") or {}).get("livekit") or {})
        voice = config.get("voice") or {}
        result: dict[str, Any] = {
            field: values.get(environment_name, "")
            for field, environment_name in ENV_FIELDS.items()
            if field in PUBLIC_ENV_FIELDS
        }
        for field in BOOL_FIELDS:
            result[field] = str(result.get(field, "")).lower() == "true"
        for field in NUMBER_RANGES:
            raw = result.get(field)
            try:
                result[field] = float(raw)
            except (TypeError, ValueError):
                result[field] = None
        result.update(
            {
                "livekit_enabled": bool(livekit.get("enabled", False)),
                "auto_tts": bool(voice.get("auto_tts", False)),
                "has_api_key": bool(values.get("LIVEKIT_API_KEY")),
                "has_api_secret": bool(values.get("LIVEKIT_API_SECRET")),
            }
        )
        return result

    def status(self) -> dict[str, Any]:
        state: dict[str, Any] = {}
        if self.state_path.exists():
            try:
                parsed = json.loads(self.state_path.read_text(encoding="utf-8-sig"))
                state = parsed if isinstance(parsed, dict) else {}
            except (OSError, json.JSONDecodeError):
                state = {}
        pid = state.get("pid")
        process_alive = isinstance(pid, int) and pid_exists(pid)
        safe = self.safe_config()
        return {
            "service": "mira-hermes-control",
            "bridge_version": "1.0.0",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "hermes_home": str(self.home),
            "plugin_installed": (self.home / "plugins" / "hermes-livekit" / "plugin.yaml").exists(),
            "configured": bool(
                safe.get("livekit_url")
                and safe.get("has_api_key")
                and safe.get("has_api_secret")
                and safe.get("room")
            ),
            "gateway_running": process_alive and state.get("gateway_state") == "running",
            "pid": pid if process_alive else None,
            "gateway_state": state.get("gateway_state", "unknown"),
            "platforms": state.get("platforms", {}),
            "config": safe,
        }

    def configure(self, payload: Any) -> dict[str, Any]:
        changes = validate_config(payload)
        if changes:
            update_dotenv(self.env_path, changes)
        self.command("plugins", "enable", "hermes-livekit")
        if "livekit_enabled" in payload:
            enabled = parse_bool(payload["livekit_enabled"], "livekit_enabled")
            self.command("config", "set", "platforms.livekit.enabled", str(enabled).lower())
        if "auto_tts" in payload:
            enabled = parse_bool(payload["auto_tts"], "auto_tts")
            self.command("config", "set", "voice.auto_tts", str(enabled).lower())
        self.command("config", "set", "platforms.livekit.group_sessions_per_user", "false")
        self.command("config", "set", "platforms.livekit.extra.url", "${LIVEKIT_URL}")
        self.command("config", "set", "platforms.livekit.extra.api_key", "${LIVEKIT_API_KEY}")
        self.command("config", "set", "platforms.livekit.extra.api_secret", "${LIVEKIT_API_SECRET}")
        self.command("config", "set", "platforms.livekit.extra.room", "${LIVEKIT_ROOM}")
        self.command("config", "check")
        return self.safe_config()

    def start(self) -> None:
        if self.status()["gateway_running"]:
            return
        creationflags = 0
        kwargs: dict[str, Any] = {}
        if os.name == "nt":
            creationflags = (
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "DETACHED_PROCESS", 0)
            )
        else:  # pragma: no cover - Windows is the supported host.
            kwargs["start_new_session"] = True
        subprocess.Popen(
            [str(self.hermes_exe), "gateway", "run"],
            env=self._environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
            **kwargs,
        )
        self._wait_for_running(True)

    def stop(self) -> None:
        if not self.status()["gateway_running"]:
            return
        self.command("gateway", "stop", timeout=45)
        self._wait_for_running(False)

    def restart(self) -> None:
        self.stop()
        self.start()

    def _wait_for_running(self, expected: bool, timeout: float = 15) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if bool(self.status()["gateway_running"]) is expected:
                return
            time.sleep(0.25)
        raise BridgeError("Timed out waiting for the Hermes gateway", HTTPStatus.GATEWAY_TIMEOUT)

    def action(self, action: str) -> dict[str, Any]:
        if action == "start":
            self.start()
        elif action == "stop":
            self.stop()
        elif action == "restart":
            self.restart()
        else:
            raise BridgeError("action must be start, stop, or restart")
        return self.status()

    def runtime(self, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise BridgeError("JSON object required")
        runtime = payload.get("runtime")
        room = payload.get("room")
        if runtime not in {"hermes", "livekit"}:
            raise BridgeError("runtime must be hermes or livekit")
        update: dict[str, Any] = {"livekit_enabled": runtime == "hermes"}
        if room is not None:
            update["room"] = room
        self.configure(update)
        self.restart()
        return self.status()


class ControlServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], controller: HermesController, token: str):
        super().__init__(address, ControlHandler)
        self.controller = controller
        self.token = token


class ControlHandler(BaseHTTPRequestHandler):
    server: ControlServer

    def log_message(self, format: str, *args: Any) -> None:
        LOG.info("%s - %s", self.address_string(), format % args)

    def _json(self, status: int, body: Any) -> None:
        encoded = json.dumps(body, separators=(",", ":"), default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(encoded)

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        supplied = header[7:] if header.startswith("Bearer ") else ""
        if supplied and hmac.compare_digest(supplied, self.server.token):
            return True
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "Unauthorized"})
        return False

    def _payload(self) -> Any:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise BridgeError("Invalid Content-Length") from None
        if length <= 0 or length > 64 * 1024:
            raise BridgeError("A JSON body between 1 byte and 64 KiB is required")
        try:
            return json.loads(self.rfile.read(length))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise BridgeError("Invalid JSON body") from None

    def do_GET(self) -> None:
        if self.path == "/health":
            self._json(HTTPStatus.OK, {"ok": True, "service": "mira-hermes-control"})
            return
        if not self._authorized():
            return
        try:
            if self.path == "/v1/status":
                self._json(HTTPStatus.OK, self.server.controller.status())
            elif self.path == "/v1/config":
                self._json(HTTPStatus.OK, self.server.controller.safe_config())
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
        except BridgeError as exc:
            self._json(exc.status, {"error": str(exc)})
        except Exception:
            LOG.exception("Unhandled control request")
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "Internal control error"})

    def do_PUT(self) -> None:
        if not self._authorized():
            return
        try:
            if self.path != "/v1/config":
                self._json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
                return
            self._json(HTTPStatus.OK, self.server.controller.configure(self._payload()))
        except BridgeError as exc:
            self._json(exc.status, {"error": str(exc)})
        except Exception:
            LOG.exception("Unhandled control request")
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "Internal control error"})

    def do_POST(self) -> None:
        if not self._authorized():
            return
        try:
            payload = self._payload()
            if self.path == "/v1/actions":
                if not isinstance(payload, dict):
                    raise BridgeError("JSON object required")
                result = self.server.controller.action(str(payload.get("action", "")))
            elif self.path == "/v1/runtime":
                result = self.server.controller.runtime(payload)
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
                return
            self._json(HTTPStatus.OK, result)
        except BridgeError as exc:
            self._json(exc.status, {"error": str(exc)})
        except Exception:
            LOG.exception("Unhandled control request")
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "Internal control error"})


def token_from(home: Path, explicit: str | None) -> str:
    if explicit:
        token = explicit
    else:
        _, values = read_dotenv(home / ".env")
        token = os.getenv("HERMES_CONTROL_TOKEN") or values.get("HERMES_CONTROL_TOKEN", "")
    if len(token) < 32:
        raise SystemExit("HERMES_CONTROL_TOKEN must contain at least 32 characters")
    return token


def main() -> None:
    default_home = Path(os.getenv("HERMES_HOME") or Path.home() / "AppData/Local/hermes")
    parser = argparse.ArgumentParser(description="MiRA Hermes control bridge")
    parser.add_argument("--hermes-home", type=Path, default=default_home)
    parser.add_argument("--host", default=os.getenv("HERMES_CONTROL_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("HERMES_CONTROL_PORT", "8790")))
    parser.add_argument("--token")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    controller = HermesController(args.hermes_home.resolve())
    server = ControlServer((args.host, args.port), controller, token_from(controller.home, args.token))
    LOG.info("Hermes control bridge listening on http://%s:%s", args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
