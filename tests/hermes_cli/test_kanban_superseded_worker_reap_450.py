"""Regression: a terminal non-current worker must not outlive its run (#450).

Defect (Aether Agents #450): during recovery of an RC lane, a task whose run
had been durably recorded terminal (``status=blocked``, ``outcome=blocked``)
still had its already-spawned Hermes worker process alive — parented by the
gateway, sleeping with no children, cwd equal to the task worktree. The task
was later resumed as a successor run with a different pid, so two workers
coexisted against one worktree.

Mechanism: the run was terminalized from *outside* the worker (an
out-of-band ``kanban_block`` that the block-loop breaker routed to
``triage``). That transition clears ``tasks.worker_pid`` /
``task_runs.worker_pid`` and closes the run, but nothing signals the OS
process, and nothing checked for it before the successor was spawned.

These tests pin the acceptance invariant:

* a worker whose run has ended and is no longer current is terminated before
  the dispatch tick can spawn a successor (reaped or exited);
* a long-running worker whose run is still current is never a candidate, so
  elapsed time alone never kills live work;
* a reused pid is never mistaken for the worker.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb

# Captured before any test patches ``os.kill`` so cleanup always really kills.
_REAL_OS_KILL = os.kill


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _spawn_workerish_child(seconds: int = 120) -> subprocess.Popen:
    """A real, long-lived child process standing in for a worker.

    ``start_new_session=True`` mirrors ``_default_spawn``, so the process has
    the same session-leader shape the dispatcher actually creates.
    """
    return subprocess.Popen(
        [sys.executable, "-c", f"import time; time.sleep({seconds})"],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _reap_child(proc: subprocess.Popen) -> None:
    """Clean up a stand-in worker, bypassing a test's patched ``os.kill``."""
    if proc.poll() is None:
        try:
            _REAL_OS_KILL(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass
    proc.wait(timeout=10)


def _running_task(conn, title: str = "superseded worker") -> str:
    tid = kb.create_task(conn, title=title, assignee="worker")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
    assert kb.claim_task(conn, tid) is not None
    return tid


def _events(conn, task_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT kind, payload FROM task_events WHERE task_id = ? ORDER BY id",
        (task_id,),
    ).fetchall()
    out = []
    for row in rows:
        try:
            payload = json.loads(row["payload"] or "{}")
        except (TypeError, ValueError):
            payload = {}
        out.append({"kind": row["kind"], "payload": payload})
    return out


def _kinds(conn, task_id: str) -> list[str]:
    return [e["kind"] for e in _events(conn, task_id)]


def _write_spawn_event(conn, task_id: str, payload: dict) -> None:
    """Write a raw ``spawned`` event (used to emulate older spawn records)."""
    with kb.write_txn(conn):
        run_id = kb._current_run_id(conn, task_id)
        kb._append_event(conn, task_id, "spawned", payload, run_id=run_id)


# ---------------------------------------------------------------------------
# The defect: terminal run, live process, successor about to be spawned
# ---------------------------------------------------------------------------


def test_out_of_band_block_leaves_no_identity_then_reap_terminates_worker(
    kanban_home: Path,
) -> None:
    """The exact #450 shape, end to end on the board surface.

    A controller/operator ``kanban_block`` ends a live worker's run; the reap
    then terminates the process and records the action durably.
    """
    with kb.connect_closing() as conn:
        proc = _spawn_workerish_child()
        try:
            tid = _running_task(conn)
            kb._set_worker_pid(conn, tid, proc.pid)

            # Terminalized from outside the worker (block-loop breaker path).
            assert kb.block_task(
                conn, tid, reason="controller block", kind="needs_input",
            )
            task = kb.get_task(conn, tid)
            assert task is not None
            # No identity left on the board: the fix cannot rely on it.
            assert task.worker_pid is None
            run = conn.execute(
                "SELECT worker_pid, ended_at FROM task_runs WHERE task_id = ?",
                (tid,),
            ).fetchone()
            assert run is not None and run["ended_at"] is not None
            assert run["worker_pid"] is None
            assert kb._pid_alive(proc.pid), "precondition: worker still alive"

            reaped = kb.reap_superseded_workers(conn)

            assert [entry["prev_pid"] for entry in reaped] == [proc.pid]
            assert reaped[0]["terminated"] is True
            assert reaped[0]["termination_attempted"] is True
            assert proc.wait(timeout=10) == -signal.SIGTERM
            assert not kb._pid_alive(proc.pid)

            terminations = [
                e for e in _events(conn, tid)
                if e["kind"] == "superseded_worker_termination"
            ]
            assert len(terminations) == 1
            assert terminations[0]["payload"]["prev_pid"] == proc.pid
            assert terminations[0]["payload"]["terminated"] is True
        finally:
            _reap_child(proc)


def test_dispatch_tick_reaps_before_it_spawns_the_successor(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The invariant at the successor boundary.

    The tick that spawns the successor must not start it while the previous
    worker process is alive: the spawn observes the reap's effect.
    """
    with kb.connect_closing() as conn:
        proc = _spawn_workerish_child()
        try:
            tid = _running_task(conn)
            kb._set_worker_pid(conn, tid, proc.pid)
            assert kb.block_task(
                conn, tid, reason="controller block", kind="needs_input",
            )
            # Re-queued for a successor.
            assert kb.unblock_task(conn, tid)
            unblocked = kb.get_task(conn, tid)
            assert unblocked is not None and unblocked.status == "ready"

            observed: list[dict] = []

            def _stub_spawn(task, workspace, **kwargs):
                # Evidence for ordering: what was true when the successor
                # actually started.
                observed.append({
                    "task": task.id,
                    "predecessor_alive": kb._pid_alive(proc.pid),
                })
                return None

            monkeypatch.setattr(
                "hermes_cli.profiles.profile_exists", lambda _name: True,
            )
            result = kb._dispatch_once_locked(
                conn,
                max_spawn=1,
                reconcile_orphans=False,
                spawn_fn=_stub_spawn,
                failure_limit=5,
            )

            assert [entry["prev_pid"] for entry in result.superseded_reaped] == [proc.pid]
            assert result.superseded_held == []
            assert [entry["task"] for entry in observed] == [tid]
            assert observed[0]["predecessor_alive"] is False
            assert proc.wait(timeout=10) == -signal.SIGTERM
        finally:
            _reap_child(proc)


def test_dispatch_tick_withholds_spawn_when_the_worker_survives_termination(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A survivor must not get a successor spawned beside it.

    SIGKILL cannot be ignored by a user-space process, so survival is
    emulated by making the signal a no-op for this pid — the point of the
    test is the hold path, not the kill.
    """
    with kb.connect_closing() as conn:
        proc = _spawn_workerish_child()
        try:
            tid = _running_task(conn)
            kb._set_worker_pid(conn, tid, proc.pid)
            assert kb.block_task(
                conn, tid, reason="controller block", kind="needs_input",
            )
            assert kb.unblock_task(conn, tid)

            real_kill = os.kill

            def _unkillable(pid, sig):
                if int(pid) == proc.pid:
                    return None
                return real_kill(pid, sig)

            monkeypatch.setattr(os, "kill", _unkillable)
            monkeypatch.setattr(
                "hermes_cli.profiles.profile_exists", lambda _name: True,
            )

            observed: list[str] = []

            def _stub_spawn(task, workspace, **kwargs):
                observed.append(task.id)
                return None

            result = kb._dispatch_once_locked(
                conn,
                max_spawn=1,
                reconcile_orphans=False,
                spawn_fn=_stub_spawn,
                failure_limit=5,
            )

            assert [entry["prev_pid"] for entry in result.superseded_reaped] == [proc.pid]
            assert result.superseded_reaped[0]["terminated"] is False
            assert result.superseded_held == [tid]
            assert observed == [], "no successor may start beside the survivor"
            assert tid not in [spawned[0] for spawned in result.spawned]
            assert "superseded_worker_hold" in _kinds(conn, tid)
            assert proc.poll() is None
        finally:
            _reap_child(proc)


# ---------------------------------------------------------------------------
# Counter-cases: what must NOT be reaped
# ---------------------------------------------------------------------------


def test_current_running_worker_is_never_a_candidate_regardless_of_age(
    kanban_home: Path,
) -> None:
    """Duration alone never kills live work.

    The run is current and its worked is alive after eight hours of wall
    clock plus a stale heartbeat: still not a candidate.
    """
    with kb.connect_closing() as conn:
        proc = _spawn_workerish_child()
        try:
            tid = _running_task(conn)
            kb._set_worker_pid(conn, tid, proc.pid)
            stale = int(time.time()) - 8 * 3600
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE task_runs SET started_at = ?, last_heartbeat_at = ? "
                    "WHERE task_id = ?",
                    (stale, stale, tid),
                )
                conn.execute(
                    "UPDATE tasks SET last_heartbeat_at = ? WHERE id = ?",
                    (stale, tid),
                )

            assert kb.reap_superseded_workers(conn) == []

            assert kb._pid_alive(proc.pid)
            assert proc.poll() is None
            assert "superseded_worker_termination" not in _kinds(conn, tid)
        finally:
            _reap_child(proc)


def test_ready_task_without_any_run_is_not_a_candidate(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="fresh", assignee="worker")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
        assert kb.reap_superseded_workers(conn) == []
        assert "superseded_worker_termination" not in _kinds(conn, tid)


def test_recycled_pid_is_not_mistaken_for_the_worker(kanban_home: Path) -> None:
    """The recorded fingerprint guards against a reused pid.

    The spawn record names a live process, but with a start-time fingerprint
    that does not match it — i.e. the pid was recycled into something else.
    """
    with kb.connect_closing() as conn:
        proc = _spawn_workerish_child()
        try:
            tid = _running_task(conn)
            live_start = kb._process_start_time(proc.pid)
            assert live_start is not None
            _write_spawn_event(conn, tid, {
                "pid": proc.pid,
                "start_time": int(live_start) + 12345,
                "claimer_host": kb._claimer_id().split(":", 1)[0],
            })
            assert kb.block_task(conn, tid, reason="controller block")

            assert kb.reap_superseded_workers(conn) == []

            assert kb._pid_alive(proc.pid)
            assert proc.poll() is None
        finally:
            _reap_child(proc)


def test_spawn_record_without_a_fingerprint_is_left_alone(kanban_home: Path) -> None:
    """Older spawn records are not guessed at — they carry no fingerprint."""
    with kb.connect_closing() as conn:
        proc = _spawn_workerish_child()
        try:
            tid = _running_task(conn)
            _write_spawn_event(conn, tid, {"pid": proc.pid})
            assert kb.block_task(conn, tid, reason="controller block")

            assert kb.reap_superseded_workers(conn) == []
            assert kb._pid_alive(proc.pid)
        finally:
            _reap_child(proc)


def test_spawn_record_from_another_host_is_not_signalled(
    kanban_home: Path,
) -> None:
    with kb.connect_closing() as conn:
        proc = _spawn_workerish_child()
        try:
            tid = _running_task(conn)
            _write_spawn_event(conn, tid, {
                "pid": proc.pid,
                "start_time": kb._process_start_time(proc.pid),
                "claimer_host": "some-other-host",
            })
            assert kb.block_task(conn, tid, reason="controller block")

            assert kb.reap_superseded_workers(conn) == []
            assert proc.poll() is None
        finally:
            _reap_child(proc)


def test_exited_worker_leaves_nothing_to_reap(kanban_home: Path) -> None:
    """A worker that exits on its own is simply not a candidate."""
    with kb.connect_closing() as conn:
        proc = _spawn_workerish_child(seconds=0)
        tid = _running_task(conn)
        kb._set_worker_pid(conn, tid, proc.pid)
        assert kb.block_task(conn, tid, reason="controller block")
        proc.wait(timeout=10)

        assert kb.reap_superseded_workers(conn) == []
        assert "superseded_worker_termination" not in _kinds(conn, tid)


def test_reap_is_idempotent(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        proc = _spawn_workerish_child()
        try:
            tid = _running_task(conn)
            kb._set_worker_pid(conn, tid, proc.pid)
            assert kb.block_task(conn, tid, reason="controller block")

            first = kb.reap_superseded_workers(conn)
            second = kb.reap_superseded_workers(conn)

            assert len(first) == 1
            assert second == []
            assert len([
                e for e in _events(conn, tid)
                if e["kind"] == "superseded_worker_termination"
            ]) == 1
        finally:
            _reap_child(proc)


def test_dry_run_tick_does_not_signal_anything(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    with kb.connect_closing() as conn:
        proc = _spawn_workerish_child()
        try:
            tid = _running_task(conn)
            kb._set_worker_pid(conn, tid, proc.pid)
            assert kb.block_task(conn, tid, reason="controller block")

            result = kb._dispatch_once_locked(
                conn, max_spawn=1, reconcile_orphans=False, dry_run=True,
            )

            assert result.superseded_reaped == []
            assert kb._pid_alive(proc.pid)
            assert proc.poll() is None
        finally:
            _reap_child(proc)
