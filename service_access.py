"""Host-owned meeting service access, separate from routed model credentials.

Capture at adapter construction. Only synchronous trusted database/backend
operations enter this scope; the model loop, subprocesses and profile files do
not receive these settings. Room and speaker authorization stays with callers.
"""
from contextvars import ContextVar
from functools import wraps
from types import MappingProxyType
import os

try:
    from gateway.platforms._shared import get_scoped_secret
except ImportError:
    get_scoped_secret = os.getenv

_NAMES = (
    "MIRA_DATABASE_URL", "MIRA_BACKEND_URL", "MIRA_AUTH_PRIVATE_KEY_PATH",
    "MIRA_AUTH_KEY_ID", "MIRA_AUTH_ISSUER", "MIRA_AUTH_AUDIENCE",
    "MIRA_AUTH_TOKEN_TTL_SECONDS",
)
_settings = ContextVar("mira_meeting_service_settings", default=None)


def service_env(name, default=None):
    settings = _settings.get()
    if settings is not None and name in _NAMES:
        return settings.get(name, default)
    return get_scoped_secret(name, default)


class MeetingServiceAccess:
    def __init__(self):
        self.__settings = MappingProxyType({
            name: value for name in _NAMES
            if (value := get_scoped_secret(name, None)) is not None
        })

    def run(self, operation, *args, **kwargs):
        token = _settings.set(self.__settings)
        try:
            return operation(*args, **kwargs)
        finally:
            _settings.reset(token)


def room_service_call(worker, operation, *args, **kwargs):
    access = getattr(worker, "_meeting_service_access", None)
    if access is None:
        return operation(*args, **kwargs)
    return access.run(operation, *args, **kwargs)


def meeting_service_method(operation):
    @wraps(operation)
    def scoped(worker, *args, **kwargs):
        return room_service_call(worker, operation, worker, *args, **kwargs)
    return scoped
