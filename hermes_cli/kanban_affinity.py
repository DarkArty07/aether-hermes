"""Session-affinity value validation shared by the Kanban kernel.

The database lifecycle remains in :mod:`hermes_cli.kanban_db`; this module
contains only value-level rules so they can be tested without opening a board.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


SESSION_AFFINITY_KEYS = frozenset({"flow_id", "terminal"})


class AffinityBusy(RuntimeError):
    """The logical flow already has a live worker lease."""


class AffinityRegistrationError(RuntimeError):
    """A worker tried to register an invalid or fenced affinity lease."""


@dataclass(frozen=True)
class AffinityLease:
    """Opaque lease data passed from the dispatcher to a worker."""

    board: str
    project_id: str
    flow_id: str
    assignee: str
    generation: int
    token: str
    session_id: str | None = None
    terminal: bool = False


def normalize_session_affinity(value: Any) -> dict[str, Any] | None:
    """Validate and canonicalize a task's optional session-affinity value.

    ``flow_id`` is the caller-owned logical flow identifier.  ``terminal`` is
    explicit opt-in for the one task that may emit a terminal flow event.
    Unknown keys are rejected so a typo cannot silently weaken routing.
    """
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("session_affinity must be an object")
    unknown = set(value) - SESSION_AFFINITY_KEYS
    if unknown:
        raise ValueError(
            "session_affinity has unknown field(s): "
            + ", ".join(sorted(str(key) for key in unknown))
        )
    flow_id = value.get("flow_id")
    if not isinstance(flow_id, str) or not flow_id.strip():
        raise ValueError("session_affinity.flow_id is required")
    terminal = value.get("terminal", False)
    if not isinstance(terminal, bool):
        raise ValueError("session_affinity.terminal must be a boolean")
    return {"flow_id": flow_id.strip(), "terminal": terminal}
