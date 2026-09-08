"""Turn-end guard for Kanban workers.

A Kanban worker may leave normally only after exactly one successful terminal
board transition. A model naming a terminal tool is not evidence: the matching
tool result must carry Hermes' durable JSON receipt for this task/run.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
import json
import os
from pathlib import Path
import sqlite3
from typing import Any, Optional


_TERMINAL_KANBAN_TOOLS = frozenset(
    {
        "kanban_complete",
        "kanban_block",
        "kanban_request_review",
        "kanban_request_changes",
    }
)

_DEFAULT_MAX_ATTEMPTS = 2


class HandoffStatus(str, Enum):
    """State of the durable terminal receipts in a worker transcript."""

    NOT_REQUIRED = "not_required"
    MISSING = "missing"
    VALID = "valid"
    CONFLICT = "conflict"


class StopAction(str, Enum):
    """Action the conversation loop must take at a candidate stop."""

    ALLOW = "allow"
    NUDGE = "nudge"
    VIOLATION = "violation"


@dataclass(frozen=True)
class HandoffAssessment:
    status: HandoffStatus
    successful_count: int
    tool_name: Optional[str] = None
    reason: str = ""


@dataclass(frozen=True)
class StopDecision:
    action: StopAction
    assessment: HandoffAssessment
    nudge: Optional[str] = None
    reason: str = ""


def kanban_stop_nudge_enabled() -> bool:
    """Return whether the Kanban stop guard is active for this process."""
    env = os.environ.get("HERMES_KANBAN_STOP_NUDGE")
    if env is not None and env.strip().lower() in {"0", "false", "no", "off"}:
        return False
    task = (os.environ.get("HERMES_KANBAN_TASK") or "").strip()
    return bool(task)


def _tool_call_name(tc: Any) -> str:
    if isinstance(tc, dict):
        fn = tc.get("function")
        if isinstance(fn, dict):
            return str(fn.get("name") or "")
        return str(tc.get("name") or "")
    fn = getattr(tc, "function", None)
    if fn is not None:
        return str(getattr(fn, "name", "") or "")
    return str(getattr(tc, "name", "") or "")


def _tool_call_id(tc: Any) -> str:
    if isinstance(tc, dict):
        return str(tc.get("id") or "")
    return str(getattr(tc, "id", "") or "")


def _receipt_payload(content: Any) -> Optional[dict]:
    if isinstance(content, dict):
        return content
    if not isinstance(content, str):
        return None
    try:
        payload = json.loads(content)
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _receipt_matches_worker(
    payload: dict,
    *,
    task_id: str,
    run_id: str,
) -> bool:
    if payload.get("ok") is not True:
        return False
    receipt_task = str(payload.get("task_id") or "").strip()
    if not receipt_task or (task_id and receipt_task != task_id):
        return False
    if run_id:
        receipt_run = str(payload.get("run_id") or "").strip()
        if receipt_run != run_id:
            return False
    return True


def assess_kanban_handoff(
    messages: Iterable[dict] | None,
    *,
    task_id: Optional[str] = None,
    run_id: Optional[str | int] = None,
) -> HandoffAssessment:
    """Validate exactly one successful terminal receipt for this worker run."""
    history = list(messages or [])
    expected_task = (
        task_id or os.environ.get("HERMES_KANBAN_TASK") or ""
    ).strip()
    expected_run = str(
        run_id if run_id is not None else os.environ.get("HERMES_KANBAN_RUN_ID") or ""
    ).strip()

    terminal_calls: dict[str, str] = {}
    duplicate_terminal_call_ids: set[str] = set()
    for msg in history:
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        for tool_call in msg.get("tool_calls") or []:
            name = _tool_call_name(tool_call)
            call_id = _tool_call_id(tool_call)
            if name in _TERMINAL_KANBAN_TOOLS and call_id:
                if call_id in terminal_calls:
                    duplicate_terminal_call_ids.add(call_id)
                terminal_calls[call_id] = name

    successful: list[tuple[str, str]] = []
    for msg in history:
        if not isinstance(msg, dict) or msg.get("role") != "tool":
            continue
        call_id = str(msg.get("tool_call_id") or "")
        called_name = terminal_calls.get(call_id)
        result_name = str(msg.get("name") or msg.get("tool_name") or "")
        if not called_name or result_name != called_name:
            continue
        payload = _receipt_payload(msg.get("content"))
        if payload is None:
            continue
        if _receipt_matches_worker(
            payload,
            task_id=expected_task,
            run_id=expected_run,
        ):
            successful.append((call_id, called_name))

    if duplicate_terminal_call_ids:
        return HandoffAssessment(
            status=HandoffStatus.CONFLICT,
            successful_count=len(successful),
            reason=(
                "duplicate terminal tool_call_id values make the handoff "
                "transcript ambiguous"
            ),
        )
    if len(successful) == 1:
        return HandoffAssessment(
            status=HandoffStatus.VALID,
            successful_count=1,
            tool_name=successful[0][1],
            reason="exactly one successful durable terminal receipt",
        )
    if len(successful) > 1:
        return HandoffAssessment(
            status=HandoffStatus.CONFLICT,
            successful_count=len(successful),
            reason=(
                "expected exactly one successful durable terminal receipt, "
                f"found {len(successful)}"
            ),
        )
    if terminal_calls:
        reason = "terminal tool call had no matching successful durable receipt"
    else:
        reason = "no terminal Kanban tool call was made"
    return HandoffAssessment(
        status=HandoffStatus.MISSING,
        successful_count=0,
        reason=reason,
    )


def session_called_kanban_terminal(messages: Iterable[dict] | None) -> bool:
    """Compatibility helper: true only for one valid durable handoff receipt."""
    return assess_kanban_handoff(messages).status is HandoffStatus.VALID


def _resolve_worker_kanban_db(
    *,
    board: Optional[str] = None,
    db_path: Optional[Path | str] = None,
) -> Optional[Path]:
    """Resolve the pinned worker kanban DB path.

    Never falls back to current board or default board unless explicitly
    pinned by the worker environment or caller parameters.
    """
    if db_path is not None:
        p = Path(db_path).expanduser()
        return p if p.exists() and p.is_file() else None

    env_db = os.environ.get("HERMES_KANBAN_DB", "").strip()
    if env_db:
        p = Path(env_db).expanduser()
        return p if p.exists() and p.is_file() else None

    pinned_board = (board or os.environ.get("HERMES_KANBAN_BOARD", "")).strip()
    if pinned_board:
        try:
            from hermes_cli.kanban_db import (
                DEFAULT_BOARD,
                _normalize_board_slug,
                board_dir,
                kanban_home,
            )

            slug = _normalize_board_slug(pinned_board)
            if slug is None:
                return None
            if slug == DEFAULT_BOARD:
                p = kanban_home() / "kanban.db"
            else:
                p = board_dir(slug) / "kanban.db"
            return p if p.exists() and p.is_file() else None
        except Exception:
            return None

    return None


def _verify_durable_completion(
    *,
    task_id: Optional[str] = None,
    run_id: Optional[str | int] = None,
    board: Optional[str] = None,
    db_path: Optional[Path | str] = None,
) -> Optional[str]:
    """Check for read-only durable completion proof in the pinned worker DB.

    Returns a truthful existing-durable-completion reason string if verified,
    or None if the proof fails.
    """
    expected_task = (
        task_id or os.environ.get("HERMES_KANBAN_TASK") or ""
    ).strip()
    raw_run = (
        run_id if run_id is not None else os.environ.get("HERMES_KANBAN_RUN_ID") or ""
    )
    try:
        expected_run = int(str(raw_run).strip())
    except (ValueError, TypeError):
        return None
    if not expected_task or expected_run <= 0:
        return None

    resolved_path = _resolve_worker_kanban_db(board=board, db_path=db_path)
    if resolved_path is None or not resolved_path.is_file():
        return None

    # Open strictly read-only using SQLite URI mode=ro
    uri = resolved_path.resolve().as_uri() + "?mode=ro"
    conn = None
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN")

        # 1. Consistent snapshot: check task row
        task_row = conn.execute(
            "SELECT id, status, current_run_id FROM tasks WHERE id = ?",
            (expected_task,),
        ).fetchone()
        if not task_row:
            return None
        if str(task_row["id"]) != expected_task:
            return None
        if str(task_row["status"]) != "done":
            return None
        if task_row["current_run_id"] is not None:
            return None

        # 2. Consistent snapshot: check run row
        run_row = conn.execute(
            "SELECT id, task_id, status, outcome, ended_at FROM task_runs WHERE id = ?",
            (expected_run,),
        ).fetchone()
        if not run_row:
            return None
        if int(run_row["id"]) != expected_run:
            return None
        if str(run_row["task_id"]) != expected_task:
            return None
        if str(run_row["status"]) != "done":
            return None
        if str(run_row["outcome"]) != "completed":
            return None
        if run_row["ended_at"] is None or int(run_row["ended_at"]) <= 0:
            return None

        # 3. Guard against reclaimed/reopened newer runs
        max_run = conn.execute(
            "SELECT MAX(id) AS max_id FROM task_runs WHERE task_id = ?",
            (expected_task,),
        ).fetchone()
        if max_run and max_run["max_id"] is not None and int(max_run["max_id"]) > expected_run:
            return None

        # 4. Check native completed event matching this task and run
        event_rows = conn.execute(
            "SELECT kind, payload FROM task_events WHERE task_id = ? AND run_id = ?",
            (expected_task, expected_run),
        ).fetchall()

        completed_events = [
            r for r in event_rows if r["kind"] in ("completed", "flow_terminal")
        ]
        if len(completed_events) != 1:
            return None

        conflicting_terminal_events = [
            r
            for r in event_rows
            if r["kind"]
            in (
                "blocked",
                "review_requested",
                "changes_requested",
                "crashed",
                "timed_out",
                "gave_up",
            )
        ]
        if conflicting_terminal_events:
            return None

        return (
            f"verified existing durable completion for task {expected_task}, "
            f"run {expected_run} on pinned board"
        )
    except (sqlite3.Error, OSError, ValueError, TypeError):
        return None
    finally:
        if conn is not None:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            conn.close()


def _nudge_text(*, task_id: str, reason: str) -> str:
    return (
        "[System: You are a Hermes Kanban worker. A plain-text reply is NOT a "
        "terminal state for the board.\n\n"
        f"Task `{task_id}` has no verified durable completion. Handoff validation failed: "
        f"{reason}.\n\n"
        "Do this immediately in your next response — do not narrate intent:\n"
        "1. Finish any remaining deliverable (write the required file(s) now).\n"
        "2. Call exactly one lifecycle tool appropriate to the task: "
        "`kanban_complete`, `kanban_block`, `kanban_request_review`, or "
        "`kanban_request_changes`.\n"
        "3. If a lifecycle tool returns an error, correct it and retry; a rejected "
        "tool call is not a handoff.\n\n"
        "Never end a turn with only a promise of future action. Exhausting this "
        "retry budget is an explicit Kanban protocol failure, not a clean exit.]"
    )


def evaluate_kanban_stop(
    *,
    messages: Iterable[dict] | None = None,
    attempts: int = 0,
    max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
    task_id: Optional[str] = None,
    run_id: Optional[str | int] = None,
    board: Optional[str] = None,
    db_path: Optional[Path | str] = None,
) -> StopDecision:
    """Return the fail-closed action for a candidate Kanban worker stop."""
    if not kanban_stop_nudge_enabled():
        assessment = HandoffAssessment(
            HandoffStatus.NOT_REQUIRED,
            successful_count=0,
            reason="Kanban stop guard disabled for this process",
        )
        return StopDecision(StopAction.ALLOW, assessment, reason=assessment.reason)

    assessment = assess_kanban_handoff(
        messages,
        task_id=task_id,
        run_id=run_id,
    )
    if assessment.status is HandoffStatus.VALID:
        return StopDecision(StopAction.ALLOW, assessment, reason=assessment.reason)
    if assessment.status is HandoffStatus.CONFLICT:
        return StopDecision(
            StopAction.VIOLATION,
            assessment,
            reason=assessment.reason,
        )

    # Transcript assessment is MISSING: check read-only durable proof.
    durable_reason = _verify_durable_completion(
        task_id=task_id,
        run_id=run_id,
        board=board,
        db_path=db_path,
    )
    if durable_reason:
        return StopDecision(
            StopAction.ALLOW,
            assessment,
            reason=durable_reason,
        )

    if attempts >= max_attempts:
        reason = f"Kanban terminal retry budget exhausted: {assessment.reason}"
        return StopDecision(StopAction.VIOLATION, assessment, reason=reason)

    tid = (
        task_id or os.environ.get("HERMES_KANBAN_TASK") or "this task"
    ).strip()
    nudge = _nudge_text(task_id=tid, reason=assessment.reason)
    return StopDecision(
        StopAction.NUDGE,
        assessment,
        nudge=nudge,
        reason=assessment.reason,
    )


def build_kanban_stop_nudge(
    *,
    messages: Iterable[dict] | None = None,
    attempts: int = 0,
    max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
    task_id: Optional[str] = None,
    run_id: Optional[str | int] = None,
    board: Optional[str] = None,
    db_path: Optional[Path | str] = None,
) -> Optional[str]:
    """Backward-compatible nudge-only view of :func:`evaluate_kanban_stop`."""
    return evaluate_kanban_stop(
        messages=messages,
        attempts=attempts,
        max_attempts=max_attempts,
        task_id=task_id,
        run_id=run_id,
        board=board,
        db_path=db_path,
    ).nudge


__all__ = [
    "HandoffAssessment",
    "HandoffStatus",
    "StopAction",
    "StopDecision",
    "assess_kanban_handoff",
    "build_kanban_stop_nudge",
    "evaluate_kanban_stop",
    "kanban_stop_nudge_enabled",
    "session_called_kanban_terminal",
]
