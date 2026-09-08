"""Tests for Kanban worker same-run durable completion recovery (B304)."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import pytest

from agent.kanban_stop import (
    HandoffStatus,
    StopAction,
    assess_kanban_handoff,
    build_kanban_stop_nudge,
    evaluate_kanban_stop,
)
from hermes_cli.kanban_db import (
    block_task,
    claim_task,
    connect,
    create_task,
    complete_task,
)


@pytest.fixture
def clear_kanban_env(monkeypatch):
    for var in (
        "HERMES_KANBAN_TASK",
        "HERMES_KANBAN_RUN_ID",
        "HERMES_KANBAN_STOP_NUDGE",
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_BOARD",
    ):
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


@pytest.fixture
def temp_kanban_db(tmp_path):
    db_file = tmp_path / "kanban.db"
    conn = connect(db_file)
    try:
        yield conn, db_file
    finally:
        conn.close()


def _conflicting_messages(task_id: str, run_id: int) -> list[dict]:
    ok_receipt = {"ok": True, "task_id": task_id, "run_id": run_id}
    return [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "kanban_complete", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "name": "kanban_complete",
            "tool_call_id": "c1",
            "content": json.dumps(ok_receipt),
        },
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "c2",
                    "type": "function",
                    "function": {"name": "kanban_block", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "name": "kanban_block",
            "tool_call_id": "c2",
            "content": json.dumps(ok_receipt),
        },
    ]


def test_durable_recovery_positive_and_idempotent_reads(clear_kanban_env, temp_kanban_db):
    conn, db_path = temp_kanban_db
    tid = create_task(conn, title="B304 Unit Task", assignee="implementer")
    claimed = claim_task(conn, tid)
    assert claimed is not None
    run_id = claimed.current_run_id
    assert run_id is not None

    ok = complete_task(conn, tid, expected_run_id=run_id, summary="Delivered unit changes")
    assert ok is True

    # Pinned worker context
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", tid)
    clear_kanban_env.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    clear_kanban_env.setenv("HERMES_KANBAN_DB", str(db_path))

    # In conversation history, tool call/result are absent
    messages = []
    assessment = assess_kanban_handoff(messages)
    assert assessment.status is HandoffStatus.MISSING

    # Take snapshot counts before evaluate_kanban_stop
    tasks_count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    runs_count = conn.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0]
    events_count = conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0]
    subs_count = conn.execute("SELECT COUNT(*) FROM kanban_notify_subs").fetchone()[0]

    # Evaluate stop
    decision = evaluate_kanban_stop(messages=messages)
    assert decision.action is StopAction.ALLOW
    assert decision.nudge is None
    assert decision.assessment.status is HandoffStatus.MISSING
    assert "durable" in decision.reason.lower() or "completed" in decision.reason.lower()
    assert build_kanban_stop_nudge(messages=messages) is None

    # Repeated reads must not add rows, events, runs, or notifications
    for _ in range(3):
        repeat_decision = evaluate_kanban_stop(messages=messages)
        assert repeat_decision.action is StopAction.ALLOW

    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == tasks_count
    assert conn.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0] == runs_count
    assert conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0] == events_count
    assert conn.execute("SELECT COUNT(*) FROM kanban_notify_subs").fetchone()[0] == subs_count


def test_durable_recovery_negative_wrong_board(clear_kanban_env, temp_kanban_db, tmp_path):
    conn, db_path = temp_kanban_db
    tid = create_task(conn, title="Task on board A", assignee="implementer")
    claimed = claim_task(conn, tid)
    assert claimed is not None and claimed.current_run_id is not None
    run_id = claimed.current_run_id
    complete_task(conn, tid, expected_run_id=run_id, summary="Done on A")

    other_db = tmp_path / "other_board.db"
    other_conn = connect(other_db)
    other_conn.close()

    clear_kanban_env.setenv("HERMES_KANBAN_TASK", tid)
    clear_kanban_env.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    clear_kanban_env.setenv("HERMES_KANBAN_DB", str(other_db))

    decision = evaluate_kanban_stop(messages=[])
    assert decision.action is StopAction.NUDGE
    assert decision.nudge is not None


def test_durable_recovery_negative_wrong_task(clear_kanban_env, temp_kanban_db):
    conn, db_path = temp_kanban_db
    tid = create_task(conn, title="Task A", assignee="implementer")
    claimed = claim_task(conn, tid)
    assert claimed is not None and claimed.current_run_id is not None
    run_id = claimed.current_run_id
    complete_task(conn, tid, expected_run_id=run_id, summary="Done")

    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_different_task")
    clear_kanban_env.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    clear_kanban_env.setenv("HERMES_KANBAN_DB", str(db_path))

    decision = evaluate_kanban_stop(messages=[])
    assert decision.action is StopAction.NUDGE


def test_durable_recovery_negative_wrong_run(clear_kanban_env, temp_kanban_db):
    conn, db_path = temp_kanban_db
    tid = create_task(conn, title="Task A", assignee="implementer")
    claimed = claim_task(conn, tid)
    assert claimed is not None and claimed.current_run_id is not None
    run_id = claimed.current_run_id
    complete_task(conn, tid, expected_run_id=run_id, summary="Done")

    clear_kanban_env.setenv("HERMES_KANBAN_TASK", tid)
    clear_kanban_env.setenv("HERMES_KANBAN_RUN_ID", str(run_id + 10))
    clear_kanban_env.setenv("HERMES_KANBAN_DB", str(db_path))

    decision = evaluate_kanban_stop(messages=[])
    assert decision.action is StopAction.NUDGE


def test_durable_recovery_negative_unrelated_terminal_event(clear_kanban_env, temp_kanban_db):
    conn, db_path = temp_kanban_db
    tid = create_task(conn, title="Blocked Task", assignee="implementer")
    claimed = claim_task(conn, tid)
    assert claimed is not None and claimed.current_run_id is not None
    run_id = claimed.current_run_id
    block_task(conn, tid, reason="Waiting on dependency", kind="dependency")

    clear_kanban_env.setenv("HERMES_KANBAN_TASK", tid)
    clear_kanban_env.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    clear_kanban_env.setenv("HERMES_KANBAN_DB", str(db_path))

    decision = evaluate_kanban_stop(messages=[])
    assert decision.action is StopAction.NUDGE


def test_durable_recovery_negative_missing_completed_event(clear_kanban_env, temp_kanban_db):
    conn, db_path = temp_kanban_db
    tid = create_task(conn, title="Corrupted Event Task", assignee="implementer")
    claimed = claim_task(conn, tid)
    assert claimed is not None and claimed.current_run_id is not None
    run_id = claimed.current_run_id
    complete_task(conn, tid, expected_run_id=run_id, summary="Done")

    # Corrupt: remove completed event
    conn.execute(
        "DELETE FROM task_events WHERE task_id = ? AND run_id = ? AND kind IN ('completed', 'flow_terminal')",
        (tid, run_id),
    )
    conn.commit()

    clear_kanban_env.setenv("HERMES_KANBAN_TASK", tid)
    clear_kanban_env.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    clear_kanban_env.setenv("HERMES_KANBAN_DB", str(db_path))

    decision = evaluate_kanban_stop(messages=[])
    assert decision.action is StopAction.NUDGE


def test_durable_recovery_negative_reopened_newer_run(clear_kanban_env, temp_kanban_db):
    conn, db_path = temp_kanban_db
    tid = create_task(conn, title="Reopened Task", assignee="implementer")
    claimed1 = claim_task(conn, tid)
    assert claimed1 is not None and claimed1.current_run_id is not None
    run_id1 = claimed1.current_run_id
    complete_task(conn, tid, expected_run_id=run_id1, summary="First finish")

    # Reopen into a newer run (e.g. status='ready' then claimed again)
    conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
    conn.commit()
    claimed2 = claim_task(conn, tid)
    assert claimed2 is not None and claimed2.current_run_id is not None
    run_id2 = claimed2.current_run_id
    assert run_id2 > run_id1

    # Worker still running run 1
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", tid)
    clear_kanban_env.setenv("HERMES_KANBAN_RUN_ID", str(run_id1))
    clear_kanban_env.setenv("HERMES_KANBAN_DB", str(db_path))

    decision = evaluate_kanban_stop(messages=[])
    assert decision.action is StopAction.NUDGE


def test_durable_recovery_negative_conflicting_transcript(clear_kanban_env, temp_kanban_db):
    conn, db_path = temp_kanban_db
    tid = create_task(conn, title="Conflict Task", assignee="implementer")
    claimed = claim_task(conn, tid)
    assert claimed is not None and claimed.current_run_id is not None
    run_id = claimed.current_run_id
    complete_task(conn, tid, expected_run_id=run_id, summary="Done")

    clear_kanban_env.setenv("HERMES_KANBAN_TASK", tid)
    clear_kanban_env.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    clear_kanban_env.setenv("HERMES_KANBAN_DB", str(db_path))

    messages = _conflicting_messages(tid, run_id)
    assert assess_kanban_handoff(messages).status is HandoffStatus.CONFLICT

    decision = evaluate_kanban_stop(messages=messages)
    # Conflict is FAIL-CLOSED and must NEVER be upgraded by durable recovery
    assert decision.action is StopAction.VIOLATION


def test_durable_recovery_negative_unreadable_db(clear_kanban_env, tmp_path):
    bad_db = tmp_path / "corrupt.db"
    bad_db.write_bytes(b"not a valid sqlite file contents\n")

    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_task")
    clear_kanban_env.setenv("HERMES_KANBAN_RUN_ID", "1")
    clear_kanban_env.setenv("HERMES_KANBAN_DB", str(bad_db))

    decision = evaluate_kanban_stop(messages=[])
    assert decision.action is StopAction.NUDGE


def test_durable_recovery_negative_unpinned_board(clear_kanban_env, temp_kanban_db):
    conn, db_path = temp_kanban_db
    tid = create_task(conn, title="Unpinned Task", assignee="implementer")
    claimed = claim_task(conn, tid)
    assert claimed is not None and claimed.current_run_id is not None
    run_id = claimed.current_run_id
    complete_task(conn, tid, expected_run_id=run_id, summary="Done")

    clear_kanban_env.setenv("HERMES_KANBAN_TASK", tid)
    clear_kanban_env.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    # HERMES_KANBAN_DB and HERMES_KANBAN_BOARD are deliberately UNSET

    decision = evaluate_kanban_stop(messages=[])
    # Must NOT fall back to current/default
    assert decision.action is StopAction.NUDGE


def test_durable_recovery_budget_exhaustion(clear_kanban_env, temp_kanban_db):
    conn, db_path = temp_kanban_db
    tid = create_task(conn, title="Budget Exhaustion Task", assignee="implementer")
    claimed = claim_task(conn, tid)
    assert claimed is not None and claimed.current_run_id is not None
    run_id = claimed.current_run_id

    clear_kanban_env.setenv("HERMES_KANBAN_TASK", tid)
    clear_kanban_env.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    clear_kanban_env.setenv("HERMES_KANBAN_DB", str(db_path))

    # Proof fails because task is running, not done.
    # Attempts >= max_attempts -> VIOLATION
    decision = evaluate_kanban_stop(messages=[], attempts=2, max_attempts=2)
    assert decision.action is StopAction.VIOLATION
    assert "exhausted" in decision.reason.lower()


def test_durable_recovery_consistent_read_snapshot_isolation(
    clear_kanban_env, temp_kanban_db, monkeypatch
):
    conn, db_path = temp_kanban_db
    tid = create_task(conn, title="Snapshot Test Task", assignee="implementer")
    claimed = claim_task(conn, tid)
    assert claimed is not None and claimed.current_run_id is not None
    run_id = claimed.current_run_id
    complete_task(conn, tid, expected_run_id=run_id, summary="Completed run 1")

    clear_kanban_env.setenv("HERMES_KANBAN_TASK", tid)
    clear_kanban_env.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    clear_kanban_env.setenv("HERMES_KANBAN_DB", str(db_path))

    orig_connect = sqlite3.connect
    hook_ran = [False]
    probed_states = []

    def mocked_connect(database, **kwargs):
        real_conn = orig_connect(database, **kwargs)
        if "mode=ro" not in str(database):
            return real_conn

        class SnapshotHookConnection:
            def __init__(self, c):
                self._c = c

            def __getattr__(self, name):
                return getattr(self._c, name)

            def __setattr__(self, name, value):
                if name == "_c":
                    super().__setattr__(name, value)
                else:
                    setattr(self._c, name, value)

            def execute(self, sql, *args, **kwargs):
                # When Query 2 on task_runs is reached, Query 1 on tasks has completed
                if "FROM task_runs WHERE id = ?" in sql and not hook_ran[0]:
                    hook_ran[0] = True
                    # Concurrent writer commits a modification after Query 1 finished
                    w_conn = connect(db_path)
                    w_conn.execute(
                        "UPDATE tasks SET status = 'running', current_run_id = 2 WHERE id = ?",
                        (tid,),
                    )
                    w_conn.execute(
                        "INSERT INTO task_runs (id, task_id, status, outcome, started_at) "
                        "VALUES (2, ?, 'running', NULL, 12345)",
                        (tid,),
                    )
                    w_conn.commit()
                    w_conn.close()

                    # Probe the task status on this same read-only connection
                    probe_cursor = self._c.execute(
                        "SELECT status FROM tasks WHERE id = ?", (tid,)
                    )
                    probed_states.append(probe_cursor.fetchone()[0])

                return self._c.execute(sql, *args, **kwargs)

        return SnapshotHookConnection(real_conn)

    monkeypatch.setattr(sqlite3, "connect", mocked_connect)

    decision = evaluate_kanban_stop(messages=[])
    assert hook_ran[0] is True
    # The proof connection must observe the snapshot prior to the concurrent commit
    assert probed_states == ["done"]
    assert decision.action is StopAction.ALLOW
    assert decision.nudge is None
    assert decision.assessment.status is HandoffStatus.MISSING
    assert "durable" in decision.reason.lower() or "completed" in decision.reason.lower()
