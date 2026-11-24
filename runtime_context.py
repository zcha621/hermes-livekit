"""Task-local meeting bindings shared by separately imported profile plugins.

Hermes gives each profile its own plugin module namespace. The transport and
the routed tool handler must nevertheless read the same ContextVars. Store
only those variables under a stable process module, never credentials or a
process-global current room. Values remain isolated across concurrent tasks.
"""
from contextvars import ContextVar
import sys
from types import ModuleType

_candidate = ModuleType("_mira_livekit_runtime_context_v1")
_candidate.active_room = ContextVar("mira_context_room", default=None)
_candidate.active_turn = ContextVar("mira_context_turn", default=None)
_candidate.service_settings = ContextVar("mira_meeting_service_settings", default=None)
_shared = sys.modules.setdefault(_candidate.__name__, _candidate)

active_room = _shared.active_room
active_turn = _shared.active_turn
service_settings = _shared.service_settings
