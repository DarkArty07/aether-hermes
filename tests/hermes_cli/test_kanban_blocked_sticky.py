"""Regression tests for #28712 — kanban dispatcher must not auto-promote
worker-initiated ``kanban_block`` (sticky blocks), but must keep
auto-recovering circuit-breaker blocks.

The bug: when a worker called ``kanban_block(reason="review-required:
...")`` to hand off to a human, the dispatcher's ``recompute_ready``
would promote the task back to ``ready`` on the next tick.  The fresh
worker found nothing to do (work already applied), exited cleanly, and
got recorded as a ``protocol_violation`` → ``gave_up`` → promote → loop
until manual intervention.

These tests pin down:

* Worker / operator-initiated blocks are sticky and survive
  ``recompute_ready``.
* Circuit-breaker blocks (``gave_up`` event, status flipped via
  ``_record_task_failure``) still auto-recover — the original intent
  of #40c1decb3 is preserved.
* An explicit ``kanban_unblock`` clears the sticky state.
* The full block → promote → crash → ``gave_up`` loop is broken after
  this fix: subsequent ticks leave the task blocked.

The tangentially related schema-init ordering bug originally reported
in #28712 (``init_db`` crashing on legacy DBs that pre-dated the
``session_id`` migration) is covered separately by
``test_kanban_db.py::test_connect_migrates_legacy_db_before_optional_column_indexes``,
landed via #28754 / #28781 ahead of this fix.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _task_status(conn, task_id: str) -> str:
    task = kb.get_task(conn, task_id)
    assert task is not None
    return task.status


# ---------------------------------------------------------------------------
# Worker-initiated kanban_block must be sticky
# ---------------------------------------------------------------------------


def test_worker_block_is_not_auto_promoted_by_recompute_ready(kanban_home: Path) -> None:
    """A standalone task that a worker explicitly blocks for review
    must stay blocked across an arbitrary number of dispatcher ticks.
    Before #28712's fix, ``recompute_ready`` would silently flip it
    back to ``ready`` on the very next tick."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="needs human review")
        kb.claim_task(conn, tid)
        assert kb.block_task(
            conn, tid,
            reason="review-required: please verify ACL change",
            expected_run_id=kb.get_task(conn, tid).current_run_id,
        )
        assert _task_status(conn, tid) == "blocked"

        # Hammer the promotion code — exactly the dispatcher loop's
        # behaviour, just compressed in time.
        for _ in range(5):
            promoted = kb.recompute_ready(conn)
            assert promoted == 0, "worker-blocked task must not auto-promote"
            assert _task_status(conn, tid) == "blocked"


def test_origin_signal_revision_block_is_sticky_until_explicit_unblock(
    kanban_home: Path,
) -> None:
    """A revision signal is still a true block, not dependency-wait work."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="contract revision required")
        claimed = kb.claim_task(conn, tid)
        assert claimed is not None
        assert kb.block_task(
            conn,
            tid,
            reason="revise the canonical contract before retrying",
            kind="needs_input",
            origin_signal="revision",
            expected_run_id=claimed.current_run_id,
        )
        assert _task_status(conn, tid) == "blocked"
        assert kb._has_sticky_block(conn, tid) is True

        for _ in range(3):
            assert kb.recompute_ready(conn) == 0
            assert _task_status(conn, tid) == "blocked"

        assert kb.unblock_task(conn, tid)
        assert _task_status(conn, tid) == "ready"


# ---------------------------------------------------------------------------
# initial_status=blocked must be sticky from creation (#91178 / Aether #188)
# ---------------------------------------------------------------------------


def test_initial_status_blocked_survives_recompute_and_reconnect(
    kanban_home: Path,
) -> None:
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="gated on setup", initial_status="blocked")
        assert _task_status(conn, tid) == "blocked"
        assert kb._has_sticky_block(conn, tid) is True
        for _ in range(5):
            assert kb.recompute_ready(conn) == 0
            assert _task_status(conn, tid) == "blocked"

    with kb.connect() as conn:
        assert kb.recompute_ready(conn) == 0
        assert _task_status(conn, tid) == "blocked"


def test_initial_status_blocked_survives_parent_completion(kanban_home: Path) -> None:
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="prerequisite")
        child = kb.create_task(
            conn,
            title="human gated child",
            parents=[parent],
            initial_status="blocked",
        )
        assert _task_status(conn, child) == "blocked"

        conn.execute(
            "UPDATE tasks SET status='done', completed_at=? WHERE id=?",
            (int(time.time()), parent),
        )
        conn.commit()
        assert kb.recompute_ready(conn) == 0
        assert _task_status(conn, child) == "blocked"


def test_initial_status_blocked_unblock_promotes_explicitly(kanban_home: Path) -> None:
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="gated", initial_status="blocked")
        assert kb.unblock_task(conn, tid)
        assert _task_status(conn, tid) == "ready"


def test_default_create_keeps_normal_promotion_semantics(kanban_home: Path) -> None:
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="plain work")
        assert kb._has_sticky_block(conn, tid) is False
        assert _task_status(conn, tid) == "ready"


# ---------------------------------------------------------------------------
# Circuit-breaker blocks still auto-recover (preserve #40c1decb3 intent)
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# unblock_task clears the sticky state
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Full bug-shaped loop: block → promote → crash → gave_up → next tick
# ---------------------------------------------------------------------------


def test_protocol_violation_loop_is_broken(kanban_home: Path) -> None:
    """Reproduces the exact #28712 loop and asserts the dispatcher
    leaves the task blocked instead of cycling.

    Loop shape from the issue:

    1. Worker calls ``kanban_block`` → status='blocked',
       ``task_runs.outcome='blocked'``, ``blocked`` event.
    2. (Bug) Dispatcher promotes back to ``ready``.
    3. Fresh worker exits cleanly without terminal tool call →
       ``protocol_violation`` event.
    4. ``_record_task_failure(failure_limit=1)`` → ``gave_up`` event,
       status='blocked' again.
    5. (Bug) Dispatcher promotes again → infinite loop.

    With the fix in place, step 2 never happens — the test simulates
    one would-be loop cycle by faking the crash-then-gave_up entries
    that *would* have been written and asserts the *next* tick still
    leaves the task blocked.
    """
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="loop reproducer")
        kb.claim_task(conn, tid)
        kb.block_task(
            conn, tid,
            reason="review-required: human eyes please",
            expected_run_id=kb.get_task(conn, tid).current_run_id,
        )
        assert _task_status(conn, tid) == "blocked"

        # First dispatcher tick — must NOT promote.
        assert kb.recompute_ready(conn) == 0
        assert _task_status(conn, tid) == "blocked"

        # Simulate the (hypothetical) protocol_violation + gave_up
        # entries that the dispatcher would have written if the bug
        # were still present.  Even with those event rows in place,
        # the worker-initiated ``blocked`` event is the most recent
        # of the ``{blocked, unblocked}`` pair, so the sticky guard
        # still fires.
        now = int(time.time())
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) "
            "VALUES (?, 'protocol_violation', NULL, ?)",
            (tid, now),
        )
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) "
            "VALUES (?, 'gave_up', NULL, ?)",
            (tid, now + 1),
        )
        conn.commit()

        # Subsequent ticks must still leave it blocked.
        for _ in range(3):
            promoted = kb.recompute_ready(conn)
            assert promoted == 0
            assert _task_status(conn, tid) == "blocked"


# ---------------------------------------------------------------------------
# Archiving a parent must not resurrect a blocked child (Aether #247)
# ---------------------------------------------------------------------------


def test_archived_parent_does_not_promote_legacy_blocked_child(
    kanban_home: Path,
) -> None:
    """A ``blocked`` child whose block predates the ``initial:true`` event
    (so ``_has_sticky_block`` cannot see it) must NOT be promoted when its
    parent is archived.

    Before the fix, ``archive_task`` → ``recompute_ready`` treated
    ``archived`` as equivalent to ``done``, flipped the child to ``ready``
    and the dispatcher spawned a worker for it — resurrecting work whose
    blocking condition was never resolved, with no ``unblock`` anywhere.
    """
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="prerequisite")
        child = kb.create_task(
            conn,
            title="legacy blocked child",
            parents=[parent],
            initial_status="blocked",
        )
        # Model the pre-fix rows: status='blocked' on the row, but no
        # 'blocked' event, so the sticky guard cannot see the block.
        conn.execute(
            "DELETE FROM task_events WHERE task_id = ? AND kind = 'blocked'",
            (child,),
        )
        conn.commit()
        assert _task_status(conn, child) == "blocked"
        assert kb._has_sticky_block(conn, child) is False

        kb.archive_task(conn, parent)

        assert _task_status(conn, parent) == "archived"
        assert _task_status(conn, child) == "blocked", (
            "archiving a parent must not promote a blocked child"
        )
        for _ in range(3):
            assert kb.recompute_ready(conn) == 0
            assert _task_status(conn, child) == "blocked"


def test_archived_parent_still_promotes_todo_child(kanban_home: Path) -> None:
    """Ordinary parent-gated work is unaffected: a ``todo`` child is still
    released when its parent is archived, so cleaning up a board does not
    strand it."""
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="prerequisite")
        child = kb.create_task(conn, title="gated child", parents=[parent])
        assert _task_status(conn, child) == "todo"

        kb.archive_task(conn, parent)
        assert _task_status(conn, child) == "ready"


def test_done_parent_still_recovers_circuit_breaker_block(kanban_home: Path) -> None:
    """The fix must not break auto-recovery: a non-sticky blocked child
    whose parent genuinely completes is still promoted."""
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="prerequisite")
        child = kb.create_task(
            conn,
            title="breaker blocked child",
            parents=[parent],
            initial_status="blocked",
        )
        conn.execute(
            "DELETE FROM task_events WHERE task_id = ? AND kind = 'blocked'",
            (child,),
        )
        conn.execute(
            "UPDATE tasks SET status='done', completed_at=? WHERE id=?",
            (int(time.time()), parent),
        )
        conn.commit()
        assert kb._has_sticky_block(conn, child) is False

        assert kb.recompute_ready(conn) == 1
        assert _task_status(conn, child) == "ready"


# ---------------------------------------------------------------------------
# Schema-init recovery on legacy DBs is covered by
# tests/hermes_cli/test_kanban_db.py::test_connect_migrates_legacy_db_before_optional_column_indexes
# (landed via #28754 / #28781).  The original PR shipped a duplicate test
# here; dropped during salvage to avoid two assertions of the same contract.
# ---------------------------------------------------------------------------
