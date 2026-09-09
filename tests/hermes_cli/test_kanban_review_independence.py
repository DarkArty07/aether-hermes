"""Regression tests for independent same-card review ownership (#362)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create a sterile board for review ownership transitions."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def test_initial_review_without_independent_reviewer_fails_closed(
    kanban_home: Path,
) -> None:
    """An implementer cannot leave its own review run claimable."""
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="requires an independent review",
            assignee="implementer",
        )
        claimed = kb.claim_task(conn, task_id, claimer="implementer:1")
        assert claimed is not None

        ok, reason = kb.request_review(
            conn,
            task_id,
            summary="Implementation is ready.",
            expected_run_id=claimed.current_run_id,
            with_reason=True,
        )

        assert ok is False
        assert reason is not None
        assert "reviewer" in reason
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "running"
        assert task.assignee == "implementer"
        assert task.current_run_id == claimed.current_run_id
        assert task.claim_lock is not None
        assert [
            event for event in kb.list_events(conn, task_id)
            if event.kind == "review_requested"
        ] == []



def test_worker_review_tool_rejects_missing_reviewer(
    kanban_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The worker surface reports the fail-closed review decision."""
    from tools import kanban_tools

    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="tool requires independent review",
            assignee="implementer",
        )
        implementation = kb.claim_task(conn, task_id, claimer="implementer:1")
        assert implementation is not None

    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(implementation.current_run_id))
    result = json.loads(
        kanban_tools._handle_request_review(
            {"summary": "Implementation is ready."}
        )
    )
    assert "error" in result
    assert "reviewer" in result["error"]
    with kb.connect() as conn:
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "running"


def test_implementer_cannot_select_itself_as_reviewer(kanban_home: Path) -> None:
    """An explicit self-review is rejected rather than routed to the implementer."""
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="reject self review",
            assignee="implementer",
        )
        claimed = kb.claim_task(conn, task_id, claimer="implementer:1")
        assert claimed is not None

        ok, reason = kb.request_review(
            conn,
            task_id,
            summary="Implementation is ready.",
            reviewer="implementer",
            expected_run_id=claimed.current_run_id,
            with_reason=True,
        )

        assert ok is False
        assert reason is not None
        assert "independent" in reason
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "running"
        assert task.assignee == "implementer"
        assert [
            event for event in kb.list_events(conn, task_id)
            if event.kind == "review_requested"
        ] == []



def test_legacy_review_without_reviewer_cannot_be_claimed(kanban_home: Path) -> None:
    """Already-parked unsafe reviews stay parked instead of self-approving."""
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="legacy unsafe review",
            assignee="implementer",
        )
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'review' WHERE id = ?", (task_id,)
            )
            kb._append_event(
                conn,
                task_id,
                "review_requested",
                {"implementer": "implementer", "reviewer": None},
            )

        assert kb.claim_review_task(conn, task_id, claimer="implementer:1") is None
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "review"
        assert task.claim_lock is None


def test_explicit_reviewer_can_claim_and_re_review_reuses_provenance(
    kanban_home: Path,
) -> None:
    """Explicit review remains available and omitted re-review is safe."""
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="preserve explicit review cycle",
            assignee="implementer",
        )
        implementation = kb.claim_task(conn, task_id, claimer="implementer:1")
        assert implementation is not None
        assert kb.request_review(
            conn,
            task_id,
            summary="First candidate is ready.",
            reviewer="supervisor",
            expected_run_id=implementation.current_run_id,
        )

        awaiting_review = kb.get_task(conn, task_id)
        assert awaiting_review is not None
        assert awaiting_review.status == "review"
        assert awaiting_review.assignee == "supervisor"
        review = kb.claim_review_task(conn, task_id, claimer="supervisor:1")
        assert review is not None
        assert kb.request_changes(
            conn,
            task_id,
            reason="Add the boundary regression.",
            expected_run_id=review.current_run_id,
        ) == (True, "implementer")

        rework = kb.claim_task(conn, task_id, claimer="implementer:2")
        assert rework is not None
        assert kb.request_review(
            conn,
            task_id,
            summary="Boundary regression is now covered.",
            expected_run_id=rework.current_run_id,
        )
        rereview = kb.get_task(conn, task_id)
        assert rereview is not None
        assert rereview.status == "review"
        assert rereview.assignee == "supervisor"
        assert kb.claim_review_task(conn, task_id, claimer="supervisor:2") is not None
