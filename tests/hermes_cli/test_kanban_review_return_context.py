"""Review-return counts are durable task facts, not a workflow policy."""

import os
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    for name in tuple(os.environ):
        if name.startswith("HERMES_KANBAN_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.mark.parametrize("return_count", [0, 1, 2, 4])
def test_context_counts_only_recorded_returns_for_this_task(
    kanban_home: Path, return_count: int
) -> None:
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="Reviewed unit", assignee="worker")
        other = kb.create_task(conn, title="Another unit", assignee="worker")
        for _ in range(return_count):
            kb._append_event(conn, tid, "changes_requested", {"reason": "fixture"})
        kb._append_event(conn, other, "changes_requested", {"reason": "other unit"})
        for kind in ("review_requested", "blocked", "crashed", "comment"):
            kb._append_event(conn, tid, kind, {})
        conn.commit()
        before = conn.total_changes
        context = kb.build_worker_context(conn, tid)
        assert f"Previous review returns (this task): {return_count}" in context
        assert conn.total_changes == before


def test_context_keeps_older_returns_after_reopening_connection(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="Long history", assignee="worker")
        for _ in range(2):
            kb._append_event(conn, tid, "changes_requested", {"reason": "fixture"})
        for _ in range(75):
            kb._append_event(conn, tid, "comment", {})
        conn.commit()
    with kb.connect_closing() as conn:
        assert all(event.kind != "changes_requested" for event in kb.list_events(conn, tid)[-50:])
        assert "Previous review returns (this task): 2" in kb.build_worker_context(conn, tid)
