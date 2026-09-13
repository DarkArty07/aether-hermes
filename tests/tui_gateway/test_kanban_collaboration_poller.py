"""Tests for TUI collaboration consumer poller (CE-HF-DELIVER, #334).

Covers CE-03 (origin reach), CE-06 (exact origin, no passive human ping),
CE-07 (no duplicate supervisor), plan D4-D5:
- Independent processing of collaboration records vs legacy terminal-notification cursor
- Exact origin session matching (platform='tui', chat_id=session_key)
- Internal collaboration wake without passive status.update emission
- Labeled native peer-evidence block (never operator out-of-band wrapper)
- Busy session non-concurrency (no concurrent agent turns, flushes when idle)
- Cross-board origin isolation (identical display names / keys across boards never cross-deliver)
- Closed/finalized origin non-rerouting
- Coexistence with ordinary terminal notifications
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
import tui_gateway.server as server


def assert_isolated_db_path(db_path: Path, expected_root: Path) -> None:
    try:
        resolved_path = db_path.resolve()
        resolved_root = expected_root.resolve()
        assert resolved_path.is_relative_to(resolved_root), (
            f"Isolation check failed: db_path {resolved_path} is not under expected root {resolved_root}"
        )
    except (ValueError, AttributeError):
        assert str(db_path.resolve()).startswith(str(expected_root.resolve())), (
            f"Isolation check failed: db_path {db_path} is not under expected root {expected_root}"
        )


@pytest.fixture(autouse=True)
def scrub_kanban_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _hermetic_environment,
):
    """Scrub ambient kanban env vars and isolate kanban home under tmp_path."""
    for var in (
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_BOARD",
        "HERMES_KANBAN_TASK",
        "HERMES_KANBAN_RUN_ID",
        "HERMES_KANBAN_WORKSPACES_ROOT",
    ):
        monkeypatch.delenv(var, raising=False)
    kanban_home = tmp_path / "kanban"
    kanban_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(kanban_home))
    db_path = kb.kanban_db_path("default")
    assert_isolated_db_path(db_path, tmp_path)


def _isolated_connect(board: str = "default"):
    """Open only after asserting the resolved board DB stays under this fixture."""
    home = Path(os.environ["HERMES_KANBAN_HOME"]).expanduser()
    db_path = kb.kanban_db_path(board)
    assert_isolated_db_path(db_path, home.parent)
    return kb.connect(board=board)


SESSION_KEY = "tui-origin-session-42"


def _session(
    key: str = SESSION_KEY, *, running: bool = False, finalized: bool = False
) -> dict:
    return {
        "session_key": key,
        "history_lock": threading.Lock(),
        "running": running,
        "_finalized": finalized,
    }


def _setup_collab_tree(
    *,
    board: str = "default",
    chat_id: str = SESSION_KEY,
    assignee: str = "worker-1",
) -> tuple[str, str]:
    conn = _isolated_connect(board)
    try:
        root = kb.create_task(conn, title="collaboration root", assignee="morfeo")
        kb.opt_in_collaboration(conn, root, mode="advisory", session_id=chat_id)
        kb.add_notify_sub(conn, task_id=root, platform="tui", chat_id=chat_id)
        child = kb.create_task(
            conn, title="child implementation", assignee=assignee, parents=[root]
        )
        kb.claim_task(conn, root)
        kb.complete_task(conn, root)
        conn.execute(
            "UPDATE kanban_notify_subs SET last_event_id = "
            "(SELECT COALESCE(MAX(id), 0) FROM task_events) WHERE task_id = ?",
            (root,),
        )
        conn.execute("DELETE FROM kanban_collaboration")
        kb.claim_task(conn, child)
        return root, child
    finally:
        conn.close()


def _wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


class TestTuiCollaborationPoller:
    def test_collect_kanban_collaboration_claims_and_formats_peer_block(self):
        """Claiming collaboration returns formatted peer block and leaves notify cursor untouched."""
        root_id, child_id = _setup_collab_tree(assignee="worker-1")

        # Enqueue an origin notice via lifecycle transition
        conn = _isolated_connect()
        try:
            ok = kb.request_review(
                conn,
                child_id,
                summary="ready for peer inspection",
                reviewer="reviewer",
                force=True,
            )
            assert ok is True
            sub_before = kb.list_notify_subs(conn, task_id=root_id)[0]
            pre_cursor = sub_before["last_event_id"]
        finally:
            conn.close()

        session = _session()
        items = server._collect_kanban_collaboration(session)

        assert len(items) == 1
        collab_dict, text = items[0]
        assert collab_dict["recipient_kind"] == "origin"
        assert collab_dict["delivery_state"] == "queued"
        assert "[PEER COLLABORATION EVIDENCE - Role: worker-1]" in text
        assert "ready for peer inspection" in text
        assert "[/PEER COLLABORATION EVIDENCE]" in text
        # Must never use operator wrapper
        assert "[OUT-OF-BAND USER MESSAGE" not in text
        assert "from the operator" not in text

        # Notify cursor is untouched (cursor independence)
        conn = _isolated_connect()
        try:
            sub_after = kb.list_notify_subs(conn, task_id=root_id)[0]
            assert sub_after["last_event_id"] == pre_cursor
            # Collaboration row is queued in DB
            row = kb.get_collaboration_message(conn, collab_dict["id"])
            assert row is not None
            assert row["delivery_state"] == "queued"
        finally:
            conn.close()

    def test_poller_loop_dispatches_collaboration_without_passive_status_update(
        self, monkeypatch
    ):
        """Collaboration dispatches agent turn but emits NO status.update (no passive human ping)."""
        root_id, child_id = _setup_collab_tree(assignee="implementer")

        conn = _isolated_connect()
        try:
            res = kb.create_collaboration_request(
                conn,
                task_id=child_id,
                author="implementer",
                body="ambiguous specification detail",
                recipient="origin",
            )
            assert res["ok"] is True
        finally:
            conn.close()

        session = _session(running=False)
        emits: list = []
        submits: list = []

        monkeypatch.setattr(server, "_KANBAN_POLL_SECONDS", 0.01)
        monkeypatch.setattr(
            server,
            "_emit",
            lambda event, sid, payload=None: emits.append((event, payload)),
        )
        monkeypatch.setattr(
            server,
            "_run_prompt_submit",
            lambda rid, sid, sess, text: submits.append(text),
        )

        stop = threading.Event()
        thread = threading.Thread(
            target=server._notification_poller_loop,
            args=(stop, "sid-tui-collab", session),
            daemon=True,
        )
        thread.start()
        try:
            assert _wait_for(lambda: submits), "agent turn was never dispatched"
        finally:
            stop.set()
            thread.join(timeout=5)

        # Agent turn dispatched with peer evidence block
        assert len(submits) == 1
        assert "PEER COLLABORATION EVIDENCE" in submits[0]
        assert "ambiguous specification detail" in submits[0]

        # Critical: NO status.update emitted for collaboration!
        status_updates = [p for e, p in emits if e == "status.update"]
        assert len(status_updates) == 0, (
            f"Expected 0 status.updates for collaboration, got {status_updates}"
        )

        # Session running state was set during dispatch
        assert session["running"] is True

    def test_busy_session_does_not_run_concurrently(self, monkeypatch):
        """Busy session defers collaboration dispatch until idle without concurrent turn."""
        root_id, child_id = _setup_collab_tree(assignee="implementer")

        conn = _isolated_connect()
        try:
            res = kb.create_collaboration_request(
                conn,
                task_id=child_id,
                author="implementer",
                body="Which interface schema should be used?",
                recipient="origin",
            )
            assert res["ok"] is True
        finally:
            conn.close()

        session = _session(running=True)
        emits: list = []
        submits: list = []

        monkeypatch.setattr(server, "_KANBAN_POLL_SECONDS", 0.01)
        monkeypatch.setattr(
            server,
            "_emit",
            lambda event, sid, payload=None: emits.append((event, payload)),
        )
        monkeypatch.setattr(
            server,
            "_run_prompt_submit",
            lambda rid, sid, sess, text: submits.append(text),
        )

        stop = threading.Event()
        thread = threading.Thread(
            target=server._notification_poller_loop,
            args=(stop, "sid-tui-busy", session),
            daemon=True,
        )
        thread.start()
        try:
            # While busy: no submissions should occur
            time.sleep(0.1)
            assert submits == [], "Should not dispatch while session is busy"

            # Transition to idle
            with session["history_lock"]:
                session["running"] = False

            assert _wait_for(lambda: submits), (
                "collaboration batch never flushed after idle"
            )
            assert "Which interface schema should be used?" in submits[0]
        finally:
            stop.set()
            thread.join(timeout=5)

    def test_two_origins_separate_boards_never_receive_each_others_notices(self):
        """Boards with identical session keys or display names never cross-deliver notices."""
        board_a = "board-a"
        board_b = "board-b"
        conn_a = _isolated_connect(board_a)
        conn_a.close()
        conn_b = _isolated_connect(board_b)
        conn_b.close()

        # Both boards use the same session_key
        root_a, child_a = _setup_collab_tree(
            board=board_a, chat_id="shared-key", assignee="worker-a"
        )
        root_b, child_b = _setup_collab_tree(
            board=board_b, chat_id="shared-key", assignee="worker-b"
        )

        # Enqueue notice ONLY on Board A
        conn = _isolated_connect(board_a)
        try:
            ok = kb.request_review(
                conn,
                child_a,
                summary="board A notice only",
                reviewer="reviewer",
                force=True,
            )
            assert ok is True
        finally:
            conn.close()

        session_a = _session(key="shared-key")
        items = server._collect_kanban_collaboration(session_a)

        assert len(items) == 1
        collab_dict, text = items[0]
        assert collab_dict["root_task_id"] == root_a
        assert "board A notice only" in text
        assert root_b not in text

        # Re-polling sees no remaining items
        assert server._collect_kanban_collaboration(session_a) == []

    def test_finalized_or_mismatched_origin_never_reroutes(self):
        """Finalized origin session or mismatched key does not claim or reroute."""
        root_id, child_id = _setup_collab_tree(chat_id="origin-key-1")

        conn = _isolated_connect()
        try:
            ok = kb.request_review(
                conn,
                child_id,
                summary="origin is gone",
                reviewer="reviewer",
                force=True,
            )
            assert ok is True
        finally:
            conn.close()

        # Session with different key
        other_session = _session(key="other-key")
        items = server._collect_kanban_collaboration(other_session)
        assert items == []

        # Session with matching key but finalized
        finalized_session = _session(key="origin-key-1", finalized=True)
        items_fin = server._collect_kanban_collaboration(finalized_session)
        assert items_fin == []

        # Collaboration remains in DB (not lost, not acknowledged, not rerouted)
        conn = _isolated_connect()
        try:
            rows = kb.list_pending_collaboration(conn, root_task_id=root_id)
            assert len(rows) == 1
            assert rows[0]["delivery_state"] == "pending"
            assert rows[0]["resolution"] == "open"
        finally:
            conn.close()

    def test_ordinary_terminal_event_and_collaboration_coexist(self):
        """Ordinary terminal notification emits status text, collaboration does not; cursors independent."""
        root_id, child_id = _setup_collab_tree(assignee="worker")

        # Subscribe TUI session to child_id for terminal notifications
        conn = _isolated_connect()
        try:
            kb.add_notify_sub(
                conn, task_id=child_id, platform="tui", chat_id=SESSION_KEY
            )
            # Create collaboration notice for origin (root)
            ok = kb.request_review(
                conn,
                child_id,
                summary="collaboration review notice",
                reviewer="reviewer",
                force=True,
            )
            assert ok is True
            # Complete child_id (terminal event)
            ok2 = kb.complete_task(conn, child_id, summary="child terminal done")
            assert ok2 is True
        finally:
            conn.close()

        session = _session()
        terminal_texts = server._collect_kanban_notifications(session)
        collab_items = server._collect_kanban_collaboration(session)

        # Terminal notifications returned text
        assert len(terminal_texts) >= 1
        assert any("done" in t for t in terminal_texts)

        # Collaboration returned distinct item
        assert len(collab_items) == 1
        assert "PEER COLLABORATION EVIDENCE" in collab_items[0][1]
