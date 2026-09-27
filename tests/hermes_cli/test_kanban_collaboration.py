"""Focused unit and behavioral tests for native Kanban collaboration storage.

Covers CE-01..04, CE-07..08, plan D1..D4, D7:
- Adjunct table schema and idempotent migrations
- Root opt-in binding and ancestor inheritance
- Request, response, ack, resolve lifecycle
- Deduplication and idempotency
- Bounded delivery leases and recovery on process loss
- Proactive lifecycle notices and coalescing
- Controller flow attention tagged as collaboration advisory
- Root-done preservation vs terminal-flow expiration
- Stale/unavailable handling on ended flows
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

import pytest

from hermes_cli import kanban_db as kb


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


@pytest.fixture
def board_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, sqlite3.Connection]:
    """Isolated temporary board environment with explicit env scrubbing and path assertion."""
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
    db_path = kb.kanban_db_path(board="test_board")
    assert_isolated_db_path(db_path, tmp_path)
    conn = kb.connect(board="test_board")
    return kanban_home, conn


def test_board_isolation_and_env_pin_rejection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fixture/isolation check fails when HERMES_KANBAN_DB is pinned outside the tmp root."""
    kanban_home = tmp_path / "kanban"
    kanban_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(kanban_home))

    # Negative control: HERMES_KANBAN_DB pin outside tmp root
    fake_live_db = tmp_path.parent / "live_board.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(fake_live_db))
    pinned_path = kb.kanban_db_path(board="test_board")
    assert pinned_path == fake_live_db
    with pytest.raises(AssertionError, match="Isolation check failed"):
        assert_isolated_db_path(pinned_path, kanban_home)

    # Positive control: after proper scrubbing, resolves inside tmp root
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    scrubbed_path = kb.kanban_db_path(board="test_board")
    assert_isolated_db_path(scrubbed_path, kanban_home)


def test_schema_initialization_and_idempotence(
    board_db: tuple[Path, sqlite3.Connection],
) -> None:
    """Case 9: fresh DB creates kanban_collaboration table and repeated init is idempotent."""
    _, conn = board_db
    # Check table existence
    tables = {
        r["name"]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "kanban_collaboration" in tables

    # Check columns
    cols = {
        r["name"]: r["type"].upper()
        for r in conn.execute("PRAGMA table_info(kanban_collaboration)").fetchall()
    }
    required_cols = [
        "id",
        "root_task_id",
        "task_id",
        "source_kind",
        "source_id",
        "source_run_id",
        "source_event_id",
        "source_comment_id",
        "recipient_kind",
        "recipient_id",
        "contract_id",
        "contract_version",
        "request_id",
        "action",
        "disposition",
        "evidence_refs",
        "delivery_state",
        "resolution",
        "enqueued_at",
        "acknowledged_at",
        "resolved_at",
        "created_at",
        "lease_token",
        "lease_expires",
        "dedup_key",
        "summary",
        "body",
    ]
    for col in required_cols:
        assert col in cols, f"missing column {col} in kanban_collaboration"

    # Repeated initialization is idempotent
    conn2 = kb.connect(board="test_board")
    tables2 = {
        r["name"]
        for r in conn2.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "kanban_collaboration" in tables2
    conn2.close()


def test_opt_in_binding_and_ancestor_inheritance(
    board_db: tuple[Path, sqlite3.Connection],
) -> None:
    """Case 1: opted-in root binds collaboration; descendants inherit; normal root stays legacy."""
    _, conn = board_db
    # 1. Normal root without opt-in
    legacy_root = kb.create_task(conn, title="Legacy root", assignee="worker")
    legacy_child = kb.create_task(
        conn, title="Legacy child", assignee="worker", parents=[legacy_root]
    )
    assert kb.get_collaboration_root(conn, legacy_root) is None
    assert kb.get_collaboration_root(conn, legacy_child) is None

    # 2. Opted-in root
    opted_root = kb.create_task(conn, title="Opted root", assignee="worker")
    kb.opt_in_collaboration(
        conn, opted_root, mode="advisory", session_id="sess-origin-1"
    )

    # Verify opt-in event recorded
    events = kb.list_events(conn, opted_root)
    opt_events = [e for e in events if e.kind == "collaboration_opted_in"]
    assert len(opt_events) == 1
    payload = (
        opt_events[0].payload
        if isinstance(opt_events[0].payload, dict)
        else json.loads(opt_events[0].payload or "{}")
    )
    assert payload.get("mode") == "advisory"
    assert payload.get("origin_session_id") == "sess-origin-1"

    # Create descendants
    child1 = kb.create_task(
        conn, title="Child 1", assignee="worker", parents=[opted_root]
    )
    grandchild = kb.create_task(
        conn, title="Grandchild", assignee="worker", parents=[child1]
    )

    assert kb.get_collaboration_root(conn, opted_root) == opted_root
    assert kb.get_collaboration_root(conn, child1) == opted_root
    assert kb.get_collaboration_root(conn, grandchild) == opted_root


def test_mixed_root_and_unrelated_project_rejection(
    board_db: tuple[Path, sqlite3.Connection],
) -> None:
    """Case 1: child with multiple distinct roots rejects as mixed root; foreign project fails closed."""
    kanban_home, conn = board_db
    root_a = kb.create_task(conn, title="Root A", assignee="worker")
    kb.opt_in_collaboration(conn, root_a, mode="advisory", session_id="sess-a")

    root_b = kb.create_task(conn, title="Root B", assignee="worker")
    # root_b not opted in or different root

    mixed_child = kb.create_task(
        conn, title="Mixed Child", assignee="worker", parents=[root_a, root_b]
    )
    # Mixed root ancestry fails closed (returns None)
    assert kb.get_collaboration_root(conn, mixed_child) is None

    # Foreign project ancestry check
    from hermes_cli import projects_db

    with projects_db.connect_closing() as pconn:
        proj_a = projects_db.create_project(
            pconn, name="Proj A", primary_path=str(kanban_home / "proj_a")
        )
        proj_b = projects_db.create_project(
            pconn, name="Proj B", primary_path=str(kanban_home / "proj_b")
        )

    root_proj = kb.create_task(
        conn, title="Proj Root", assignee="worker", project_id=proj_a
    )
    kb.opt_in_collaboration(conn, root_proj, mode="advisory", session_id="sess-proj")
    child_foreign = kb.create_task(
        conn,
        title="Foreign Child",
        assignee="worker",
        project_id=proj_b,
        parents=[root_proj],
    )
    assert kb.get_collaboration_root(conn, child_foreign) is None


def test_request_respond_ack_resolve_lifecycle(
    board_db: tuple[Path, sqlite3.Connection],
) -> None:
    """Case 2: full request -> enqueue -> ack -> respond -> resolve lifecycle."""
    _, conn = board_db
    root = kb.create_task(conn, title="Root", assignee="worker")
    kb.opt_in_collaboration(conn, root, mode="advisory", session_id="sess-origin")
    # Record an origin notify subscription on the root
    conn.execute(
        """
        INSERT INTO kanban_notify_subs
            (task_id, platform, chat_id, thread_id, user_id, delivery_mode, created_at, last_event_id)
        VALUES (?, 'tui', 'chat-1', '', 'user-1', 'notify', ?, 0)
        """,
        (root, int(time.time())),
    )

    task = kb.create_task(
        conn, title="Implementation unit", assignee="implementer", parents=[root]
    )
    # Complete root task so children become ready
    kb.claim_task(conn, root)
    kb.complete_task(conn, root)
    conn.execute("DELETE FROM kanban_collaboration")
    # Transition task to running
    claimed_task = kb.claim_task(conn, task)
    assert claimed_task is not None
    assert claimed_task.status == "running"

    sibling = kb.create_task(
        conn, title="Sibling unit", assignee="reviewer", parents=[root]
    )

    # 1. Explicit request before completion
    collab_req = kb.create_collaboration_request(
        conn,
        task_id=task,
        author="implementer",
        body="Does interface foo require bar?",
        recipient="origin",
        evidence_refs=["specs/contract.md:15"],
    )
    assert collab_req["status"] == "pending"
    assert collab_req["recipient_kind"] == "origin"
    req_id = collab_req["collaboration_id"]

    # Verify source task status and claim are preserved
    t_row = kb.get_task(conn, task)
    assert t_row is not None
    assert t_row.status == "running"
    # Sibling task is independent and can be claimed
    claimed_sibling = kb.claim_task(conn, sibling)
    assert claimed_sibling is not None
    assert claimed_sibling.status == "running"

    # 2. Recipient claims pending message
    claimed_msgs = kb.claim_collaboration_messages(
        conn, recipient_kind="origin", lease_token="lease-1", lease_seconds=60
    )
    assert len(claimed_msgs) == 1
    assert claimed_msgs[0]["id"] == req_id
    assert claimed_msgs[0]["delivery_state"] == "queued"

    # 3. Explicit acknowledgment
    ack_ok = kb.advance_collaboration_message(
        conn, collaboration_id=req_id, delivery_state="acknowledged"
    )
    assert ack_ok is True
    msg_after_ack = kb.get_collaboration_message(conn, req_id)
    assert msg_after_ack is not None
    assert msg_after_ack["delivery_state"] == "acknowledged"
    assert msg_after_ack["acknowledged_at"] is not None

    # 4. Attributable response
    collab_resp = kb.create_collaboration_response(
        conn,
        request_id=req_id,
        author="morfeo",
        body="Yes, foo requires bar per spec.",
        disposition="advice",
        evidence_refs=["specs/contract.md:20"],
    )
    assert collab_resp["status"] == "responded"
    resp_id = collab_resp["collaboration_id"]

    # Original request resolution is 'responded'
    req_after_resp = kb.get_collaboration_message(conn, req_id)
    assert req_after_resp is not None
    assert req_after_resp["resolution"] == "responded"

    # Verify source task status still unchanged (no completion inferred)
    t_row = kb.get_task(conn, task)
    assert t_row is not None
    assert t_row.status == "running"

    # 5. Requester resolves
    resolve_ok = kb.resolve_collaboration_request(
        conn,
        request_id=req_id,
        author="implementer",
        body="Applied clarification to unit tests.",
        disposition="applied",
    )
    assert resolve_ok["status"] == "resolved"
    req_after_resolve = kb.get_collaboration_message(conn, req_id)
    assert req_after_resolve is not None
    assert req_after_resolve["resolution"] == "resolved"
    assert req_after_resolve["resolved_at"] is not None
    assert req_after_resolve["disposition"] == "applied"

    # Source task status still running
    t_row = kb.get_task(conn, task)
    assert t_row is not None
    assert t_row.status == "running"


def test_idempotency_and_dedup(board_db: tuple[Path, sqlite3.Connection]) -> None:
    """Case 2: duplicate requests with same idempotency_key return existing row."""
    _, conn = board_db
    root = kb.create_task(conn, title="Root", assignee="worker")
    kb.opt_in_collaboration(conn, root, mode="advisory", session_id="sess-origin")
    conn.execute(
        """
        INSERT INTO kanban_notify_subs
            (task_id, platform, chat_id, thread_id, user_id, delivery_mode, created_at, last_event_id)
        VALUES (?, 'tui', 'chat-1', '', 'user-1', 'notify', ?, 0)
        """,
        (root, int(time.time())),
    )
    task = kb.create_task(conn, title="Task", assignee="worker", parents=[root])

    req1 = kb.create_collaboration_request(
        conn,
        task_id=task,
        author="worker",
        body="Question 1",
        recipient="origin",
        idempotency_key="key-123",
    )
    req2 = kb.create_collaboration_request(
        conn,
        task_id=task,
        author="worker",
        body="Question 1 duplicate retry",
        recipient="origin",
        idempotency_key="key-123",
    )
    assert req1["collaboration_id"] == req2["collaboration_id"]
    # Exactly one row in table
    rows = conn.execute(
        "SELECT COUNT(*) FROM kanban_collaboration WHERE task_id = ?", (task,)
    ).fetchone()
    assert rows[0] == 1


def test_claim_lease_expiration_and_recovery(
    board_db: tuple[Path, sqlite3.Connection],
) -> None:
    """Case 3: expired lease on queued record recovers back to pending on next claim."""
    _, conn = board_db
    root = kb.create_task(conn, title="Root", assignee="worker")
    kb.opt_in_collaboration(conn, root, mode="advisory", session_id="sess-origin")
    conn.execute(
        """
        INSERT INTO kanban_notify_subs
            (task_id, platform, chat_id, thread_id, user_id, delivery_mode, created_at, last_event_id)
        VALUES (?, 'tui', 'chat-1', '', 'user-1', 'notify', ?, 0)
        """,
        (root, int(time.time())),
    )
    task = kb.create_task(conn, title="Task", assignee="worker", parents=[root])

    req = kb.create_collaboration_request(
        conn,
        task_id=task,
        author="worker",
        body="Question",
        recipient="origin",
    )
    req_id = req["collaboration_id"]

    # Claim with 1-second lease
    claimed = kb.claim_collaboration_messages(
        conn, recipient_kind="origin", lease_token="lease-temp", lease_seconds=1
    )
    assert len(claimed) == 1
    assert claimed[0]["delivery_state"] == "queued"

    # Fast-forward time / simulate expired lease
    conn.execute(
        "UPDATE kanban_collaboration SET lease_expires = ? WHERE id = ?",
        (int(time.time()) - 10, req_id),
    )

    # Next claim should reclaim the expired message
    claimed_again = kb.claim_collaboration_messages(
        conn, recipient_kind="origin", lease_token="lease-new", lease_seconds=60
    )
    assert len(claimed_again) == 1
    assert claimed_again[0]["id"] == req_id
    assert claimed_again[0]["delivery_state"] == "queued"
    assert claimed_again[0]["lease_token"] == "lease-new"


def test_proactive_lifecycle_notices_and_coalescing(
    board_db: tuple[Path, sqlite3.Connection],
) -> None:
    """Case 4: review_requested, changes_requested, blocked enqueue notices and coalesce."""
    _, conn = board_db
    root = kb.create_task(conn, title="Root", assignee="worker")
    kb.opt_in_collaboration(conn, root, mode="advisory", session_id="sess-origin")
    task = kb.create_task(conn, title="Task", assignee="implementer", parents=[root])
    kb.claim_task(conn, root)
    kb.complete_task(conn, root)
    # Clear unconsumed notice from root decomposition completion
    conn.execute("DELETE FROM kanban_collaboration")
    kb.claim_task(conn, task)

    # 1. request_review enqueues notice
    ok = kb.request_review(
        conn, task, summary="Completed feature X", reviewer="reviewer", force=True
    )
    assert ok is True
    msgs = kb.list_pending_collaboration(
        conn, recipient_kind="origin", root_task_id=root
    )
    assert len(msgs) == 1
    first_notice = msgs[0]
    assert first_notice["action"] == "notice"
    assert first_notice["source_kind"] == "lifecycle"
    assert "Completed feature X" in (first_notice["summary"] or "")

    # 2. changes_requested coalesces into existing unconsumed notice
    # Claim review task
    claimed_rev = kb.claim_review_task(conn, task, claimer="reviewer")
    assert claimed_rev is not None
    ok, _ = kb.request_changes(conn, task, reason="Missing tests for edge case")
    assert ok is True
    msgs_after_changes = kb.list_pending_collaboration(
        conn, recipient_kind="origin", root_task_id=root
    )
    assert len(msgs_after_changes) == 1  # Coalesced!
    coalesced = msgs_after_changes[0]
    assert coalesced["id"] == first_notice["id"]
    assert "Missing tests for edge case" in (coalesced["summary"] or "")

    # 3. Explicit request is NOT coalesced into automatic notice
    kb.create_collaboration_request(
        conn,
        task_id=task,
        author="implementer",
        body="Clarification question",
        recipient="origin",
    )
    all_msgs = kb.list_pending_collaboration(
        conn, recipient_kind="origin", root_task_id=root
    )
    assert len(all_msgs) == 2  # 1 notice + 1 explicit request
    assert {m["action"] for m in all_msgs} == {"notice", "request"}


def test_controller_flow_attention_collaboration_advisory(
    board_db: tuple[Path, sqlite3.Connection],
) -> None:
    """Case 7: controller-recipient request creates flow_attention tagged as advisory and does not unblock parents."""
    kanban_home, conn = board_db
    from hermes_cli import projects_db

    with projects_db.connect_closing() as pconn:
        project_id = projects_db.create_project(
            pconn, name="Test Project", primary_path=str(kanban_home)
        )

    root = kb.create_task(conn, title="Root", assignee="worker", project_id=project_id)
    kb.opt_in_collaboration(conn, root, mode="advisory", session_id="sess-origin")

    parent_task = kb.create_task(
        conn,
        title="Parent blocked",
        assignee="worker",
        project_id=project_id,
        parents=[root],
    )
    kb.claim_task(conn, root)
    kb.complete_task(conn, root)
    # Block parent task
    kb.claim_task(conn, parent_task)
    kb.block_task(conn, parent_task, reason="Need help", kind="needs_input")
    p_status = kb.get_task(conn, parent_task).status
    assert p_status == "blocked"

    # Create terminal controller task
    controller_task = kb.create_task(
        conn,
        title="Supervisor controller",
        assignee="supervisor",
        project_id=project_id,
        parents=[root, parent_task],
        session_affinity={"flow_id": "flow-1", "terminal": True},
    )

    # Make request to "controller"
    req = kb.create_collaboration_request(
        conn,
        task_id=parent_task,
        author="worker",
        body="Controller advisory request",
        recipient="controller",
    )
    assert req["recipient_kind"] == "controller"

    # Flow attention was enqueued on controller
    attentions = kb._pending_flow_attentions(conn, controller_task)
    assert len(attentions) == 1
    att = attentions[0]
    assert att.get("collaboration_advisory") is True
    assert att.get("blocked_task_id") == parent_task

    # Resolving flow attention must NOT unblock parent_task
    resolved = kb._resolve_flow_attention(conn, controller_task)
    assert resolved is True

    # Check parent status: STILL BLOCKED!
    parent_after = kb.get_task(conn, parent_task)
    assert parent_after.status == "blocked"


def test_root_done_preserves_collaboration_vs_terminal_flow_expires(
    board_db: tuple[Path, sqlite3.Connection],
) -> None:
    """Case 7 & 8: decomposition root done keeps collaboration active; flow_terminal marks it stale."""
    _, conn = board_db
    root = kb.create_task(
        conn, title="Decomp root", assignee="worker", project_id="p_test"
    )
    kb.opt_in_collaboration(conn, root, mode="advisory", session_id="sess-origin")
    conn.execute(
        """
        INSERT INTO kanban_notify_subs
            (task_id, platform, chat_id, thread_id, user_id, delivery_mode, created_at, last_event_id)
        VALUES (?, 'tui', 'chat-1', '', 'user-1', 'notify', ?, 0)
        """,
        (root, int(time.time())),
    )
    child = kb.create_task(
        conn, title="Child task", assignee="worker", project_id="p_test", parents=[root]
    )

    # Decomposition root completes
    kb.claim_task(conn, root)
    kb.complete_task(conn, root, summary="Decomposition complete")
    assert kb.get_task(conn, root).status == "done"

    # Child can still make collaboration requests!
    req = kb.create_collaboration_request(
        conn,
        task_id=child,
        author="worker",
        body="Child asking question",
        recipient="origin",
    )
    req_id = req["collaboration_id"]
    msg = kb.get_collaboration_message(conn, req_id)
    assert msg is not None
    assert msg["resolution"] == "open"

    # Terminal flow event expires collaboration
    kb._expire_collaboration_for_flow(conn, child)
    msg_after_expire = kb.get_collaboration_message(conn, req_id)
    assert msg_after_expire is not None
    assert msg_after_expire["resolution"] == "stale"


def test_archive_task_expires_collaboration_to_stale(
    board_db: tuple[Path, sqlite3.Connection],
) -> None:
    """Case 8: archive_task on an opted-in root stales unresolved collaboration messages."""
    _, conn = board_db
    root = kb.create_task(
        conn, title="Archive Root", assignee="worker", project_id="p_test"
    )
    kb.opt_in_collaboration(conn, root, mode="advisory", session_id="sess-origin")
    conn.execute(
        """
        INSERT INTO kanban_notify_subs
            (task_id, platform, chat_id, thread_id, user_id, delivery_mode, created_at, last_event_id)
        VALUES (?, 'tui', 'chat-1', '', 'user-1', 'notify', ?, 0)
        """,
        (root, int(time.time())),
    )
    child = kb.create_task(
        conn, title="Child Task", assignee="worker", project_id="p_test", parents=[root]
    )

    req = kb.create_collaboration_request(
        conn, task_id=child, author="worker", body="Pending inquiry", recipient="origin"
    )
    req_id = req["collaboration_id"]
    msg = kb.get_collaboration_message(conn, req_id)
    assert msg is not None
    assert msg["resolution"] == "open"

    # Public archive_task call on root
    archived = kb.archive_task(conn, root)
    assert archived is True
    root_task = kb.get_task(conn, root)
    assert root_task is not None and root_task.status == "archived"

    # Collaboration root fails closed for archived root and descendants
    assert kb.get_collaboration_root(conn, root) is None
    assert kb.get_collaboration_root(conn, child) is None

    # Unresolved request must be marked stale with record preserved
    msg_after = kb.get_collaboration_message(conn, req_id)
    assert msg_after is not None
    assert msg_after["resolution"] == "stale"
    assert msg_after["resolved_at"] is not None


def test_controller_worker_context_collaboration_advisory(
    board_db: tuple[Path, sqlite3.Connection],
) -> None:
    """Case 7: build_worker_context labels collaboration advisory and omits parent-recovery recipe."""
    kanban_home, conn = board_db
    from hermes_cli import projects_db

    with projects_db.connect_closing() as pconn:
        project_id = projects_db.create_project(
            pconn, name="Context Project", primary_path=str(kanban_home)
        )

    root = kb.create_task(conn, title="Root", assignee="worker", project_id=project_id)
    kb.opt_in_collaboration(conn, root, mode="advisory", session_id="sess-origin")

    child = kb.create_task(
        conn, title="Child", assignee="worker", project_id=project_id, parents=[root]
    )
    ctrl = kb.create_task(
        conn,
        title="Supervisor",
        assignee="supervisor",
        project_id=project_id,
        parents=[root],
        session_affinity={"flow_id": "flow-ctx", "terminal": True},
    )

    kb.create_collaboration_request(
        conn,
        task_id=child,
        author="worker",
        body="Advisory query",
        recipient="controller",
    )

    ctx = kb.build_worker_context(conn, ctrl)
    assert "## Flow collaboration advisory" in ctx
    assert "requested collaboration advisory" in ctx
    # Verify absence of recovery recipes
    assert "## Flow recovery attention" not in ctx
    assert 'kanban_block(kind="dependency")' not in ctx
    assert 'origin_signal="recovery"' not in ctx


def test_opt_in_persists_contract_binding_from_board_metadata(
    board_db: tuple[Path, sqlite3.Connection],
) -> None:
    """Case 1: opt_in_collaboration populates contract_id and contract_version from board.json metadata."""
    kanban_home, conn = board_db
    # Write board.json with contract metadata in the board directory
    meta_path = kb.board_metadata_path("test_board")
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    board_meta = {
        "slug": "test_board",
        "aether_contract_id": "oc_test_contract_abc",
        "aether_contract_version": 1,
        "aether_project_id": "proj_aether_uuid_99",
    }
    meta_path.write_text(json.dumps(board_meta), encoding="utf-8")

    root = kb.create_task(conn, title="Contract Root", assignee="worker")
    kb.opt_in_collaboration(
        conn, root, mode="advisory", session_id="sess-orig", board="test_board"
    )

    events = kb.list_events(conn, root)
    opt_events = [e for e in events if e.kind == "collaboration_opted_in"]
    assert len(opt_events) == 1
    raw_payload = opt_events[0].payload
    pl = (
        raw_payload
        if isinstance(raw_payload, dict)
        else json.loads(str(raw_payload or "{}"))
    )
    assert pl.get("contract_id") == "oc_test_contract_abc"
    assert pl.get("contract_version") == "1"
    assert pl.get("project_id") == "proj_aether_uuid_99"

    # Sibling request inherits contract_id and version
    conn.execute(
        """
        INSERT INTO kanban_notify_subs
            (task_id, platform, chat_id, thread_id, user_id, delivery_mode, created_at, last_event_id)
        VALUES (?, 'tui', 'chat-1', '', 'user-1', 'notify', ?, 0)
        """,
        (root, int(time.time())),
    )
    req = kb.create_collaboration_request(
        conn, task_id=root, author="worker", body="Check contract", recipient="origin"
    )
    msg = kb.get_collaboration_message(conn, req["collaboration_id"])
    assert msg is not None
    assert msg["contract_id"] == "oc_test_contract_abc"
    assert msg["contract_version"] == "1"


def test_source_run_id_persistence(
    board_db: tuple[Path, sqlite3.Connection], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Case 4: source_run_id is persisted on requests, responses, and lifecycle notices when run exists."""
    _, conn = board_db
    root = kb.create_task(
        conn, title="Run Root", assignee="worker", project_id="p_test"
    )
    kb.opt_in_collaboration(conn, root, mode="advisory", session_id="sess-origin")
    conn.execute(
        """
        INSERT INTO kanban_notify_subs
            (task_id, platform, chat_id, thread_id, user_id, delivery_mode, created_at, last_event_id)
        VALUES (?, 'tui', 'chat-1', '', 'user-1', 'notify', ?, 0)
        """,
        (root, int(time.time())),
    )
    child = kb.create_task(
        conn, title="Run Child", assignee="worker", project_id="p_test", parents=[root]
    )

    # 1. Explicit run_id on request
    req = kb.create_collaboration_request(
        conn,
        task_id=child,
        author="worker",
        body="Request with run",
        recipient="origin",
        run_id=123,
    )
    req_row = kb.get_collaboration_message(conn, req["collaboration_id"])
    assert req_row is not None
    assert req_row["source_run_id"] == 123

    # 2. Explicit run_id on response
    resp = kb.create_collaboration_response(
        conn,
        request_id=req["collaboration_id"],
        author="supervisor",
        body="Response with run",
        disposition="advice",
        run_id=124,
    )
    resp_row = kb.get_collaboration_message(conn, resp["collaboration_id"])
    assert resp_row is not None
    assert resp_row["source_run_id"] == 124

    # 3. Environment-derived run_id
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "777")
    req2 = kb.create_collaboration_request(
        conn, task_id=child, author="worker", body="Env run request", recipient="origin"
    )
    req2_row = kb.get_collaboration_message(conn, req2["collaboration_id"])
    assert req2_row is not None
    assert req2_row["source_run_id"] == 777

    # 4. Lifecycle notice with run_id from claimed task
    kb.complete_task(conn, root)
    claimed = kb.claim_task(conn, child)
    assert claimed is not None and claimed.current_run_id is not None
    run_id_val = claimed.current_run_id
    kb.block_task(conn, child, reason="Blocked notice test", kind="needs_input")
    notices = conn.execute(
        "SELECT * FROM kanban_collaboration WHERE action = 'notice' AND source_run_id = ?",
        (run_id_val,),
    ).fetchall()
    assert len(notices) >= 1


def test_opt_in_origin_route_persist_match_and_refusal(
    board_db: tuple[Path, sqlite3.Connection],
) -> None:
    """origin_route is persisted distinctly from SessionDB id and matches one notify sub."""
    _, conn = board_db
    root = kb.create_task(conn, title="Route Root", assignee="worker")
    with pytest.raises(ValueError, match="origin_route"):
        kb.opt_in_collaboration(
            conn,
            root,
            mode="advisory",
            session_id="sess-raw",
            origin_route={"platform": "telegram"},
        )
    kb.opt_in_collaboration(
        conn,
        root,
        mode="advisory",
        session_id="sess-raw",
        origin_route={
            "platform": "telegram",
            "chat_id": "chat-100",
            "thread_id": None,
            "notifier_profile": "default",
            "origin_session_id": "sess-raw",
        },
    )
    route = kb.get_collaboration_origin_route(conn, root)
    assert route is not None
    assert route["platform"] == "telegram"
    assert route["chat_id"] == "chat-100"
    assert route["origin_session_id"] == "sess-raw"
    assert route["chat_id"] != route["origin_session_id"]
    assert kb.collaboration_origin_route_matches_sub(
        route, {"platform": "telegram", "chat_id": "chat-100", "thread_id": ""}
    )
    assert not kb.collaboration_origin_route_matches_sub(
        route, {"platform": "tui", "chat_id": "chat-100"}
    )
    assert not kb.collaboration_origin_route_matches_sub(
        route, {"platform": "telegram", "chat_id": "chat-other"}
    )


def _aether_parentage_fixture(
    tmp_path: Path, conn: sqlite3.Connection
) -> tuple[str, str, str]:
    """Install one exact Aether board identity and return Project/workspace paths."""
    from hermes_cli import projects_db

    with projects_db.connect_closing() as pconn:
        project_id = projects_db.create_project(
            pconn, name=f"HF-460 {tmp_path.name}", primary_path=str(tmp_path)
        )
    board = "test_board"
    metadata_path = kb.board_metadata_path(board)
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path.write_text(
        json.dumps({
            "slug": board,
            "aether_contract_id": "oc_hf460",
            "aether_contract_version": 1,
            "aether_project_id": "aether-project-hf460",
            "project_id": project_id,
        }),
        encoding="utf-8",
    )
    assert kb._read_bound_board_metadata(conn)["project_id"] == project_id
    return project_id, str(tmp_path / "canonical"), str(tmp_path / "candidate")


def _parentage_snapshot(conn: sqlite3.Connection) -> tuple:
    return (
        tuple(
            tuple(row)
            for row in conn.execute(
                "SELECT id, status, workspace_path FROM tasks ORDER BY id"
            )
        ),
        tuple(
            tuple(row)
            for row in conn.execute(
                "SELECT parent_id, child_id FROM task_links ORDER BY parent_id, child_id"
            )
        ),
        tuple(
            tuple(row)
            for row in conn.execute(
                "SELECT id, task_id, kind, payload FROM task_events ORDER BY id"
            )
        ),
        tuple(
            tuple(row)
            for row in conn.execute(
                "SELECT * FROM kanban_notify_subs ORDER BY task_id, platform, chat_id"
            )
        ),
    )


def test_aether_create_task_preserves_one_corroborated_root_atomically(
    board_db: tuple[Path, sqlite3.Connection],
) -> None:
    """New Aether children cannot join an independent root or leave write residue."""
    kanban_home, conn = board_db
    project_id, canonical, candidate = _aether_parentage_fixture(kanban_home, conn)
    Path(canonical).mkdir()
    Path(candidate).mkdir()
    root = kb.create_task(
        conn,
        title="canonical root",
        assignee="supervisor",
        project_id=project_id,
        workspace_kind="dir",
        workspace_path=canonical,
    )
    assert kb.opt_in_collaboration(
        conn,
        root,
        contract_id="oc_hf460",
        contract_version="1",
        session_id="origin-session",
        board="test_board",
    )
    decision = kb.create_task(
        conn,
        title="decision",
        assignee="supervisor",
        project_id=project_id,
        parents=(root,),
        workspace_kind="dir",
        workspace_path=canonical,
    )
    unit = kb.create_task(
        conn,
        title="unit",
        assignee="implementer",
        project_id=project_id,
        parents=(decision,),
        workspace_kind="dir",
        workspace_path=candidate,
    )
    assert kb.get_collaboration_root(conn, unit) == root

    # An unrelated root is a valid generic task until a caller tries to join it
    # to this exact persisted Aether flow.
    independent = kb.create_task(
        conn,
        title="independent decision",
        assignee="supervisor",
        project_id=project_id,
        workspace_kind="dir",
        workspace_path=canonical,
    )
    conn.execute(
        "INSERT INTO kanban_notify_subs "
        "(task_id, platform, chat_id, thread_id, user_id, delivery_mode, created_at, last_event_id) "
        "VALUES (?, 'tui', 'origin-chat', '', 'user-1', 'notify', ?, 0)",
        (root, int(time.time())),
    )
    conn.commit()

    before = _parentage_snapshot(conn)
    rejected_workspace = Path(candidate) / "must-not-exist"
    with pytest.raises(ValueError, match="canonical Aether collaboration root"):
        kb.create_task(
            conn,
            title="ambiguous unit",
            assignee="implementer",
            project_id=project_id,
            parents=(root, independent),
            workspace_kind="dir",
            workspace_path=str(rejected_workspace),
        )
    assert _parentage_snapshot(conn) == before
    assert not rejected_workspace.exists()
    assert kb.get_collaboration_root(conn, unit) == root


@pytest.mark.parametrize("link_to_ancestor", [False, True])
def test_aether_link_tasks_preserves_same_root_across_descendants_atomically(
    board_db: tuple[Path, sqlite3.Connection],
    link_to_ancestor: bool,
) -> None:
    """link_tasks rejects root replacement before links/status/events/subscriptions change."""
    kanban_home, conn = board_db
    project_id, canonical, candidate = _aether_parentage_fixture(kanban_home, conn)
    Path(canonical).mkdir()
    Path(candidate).mkdir()
    root = kb.create_task(
        conn,
        title="canonical root",
        assignee="supervisor",
        project_id=project_id,
        workspace_kind="dir",
        workspace_path=canonical,
    )
    assert kb.opt_in_collaboration(
        conn,
        root,
        contract_id="oc_hf460",
        contract_version="1",
        session_id="origin-session",
        board="test_board",
    )
    decision = kb.create_task(
        conn,
        title="decision",
        assignee="supervisor",
        project_id=project_id,
        parents=(root,),
        workspace_kind="dir",
        workspace_path=canonical,
    )
    unit = kb.create_task(
        conn,
        title="unit",
        assignee="implementer",
        project_id=project_id,
        parents=(decision,),
        workspace_kind="dir",
        workspace_path=candidate,
    )
    independent = kb.create_task(
        conn,
        title="independent",
        assignee="supervisor",
        project_id=project_id,
        workspace_kind="dir",
        workspace_path=canonical,
    )
    conn.execute("UPDATE tasks SET status='running' WHERE id=?", (independent,))
    conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (unit,))
    conn.execute(
        "INSERT INTO kanban_notify_subs "
        "(task_id, platform, chat_id, thread_id, user_id, delivery_mode, created_at, last_event_id) "
        "VALUES (?, 'tui', 'independent-chat', '', 'user-2', 'notify', ?, 0)",
        (independent, int(time.time())),
    )
    conn.commit()

    target = decision if link_to_ancestor else unit
    before = _parentage_snapshot(conn)
    with pytest.raises(ValueError, match="canonical Aether collaboration root"):
        kb.link_tasks(conn, independent, target)
    assert _parentage_snapshot(conn) == before
    assert kb.get_collaboration_root(conn, decision) == root
    assert kb.get_collaboration_root(conn, unit) == root


def test_aether_link_tasks_allows_new_rootless_decision_chain(
    board_db: tuple[Path, sqlite3.Connection],
) -> None:
    """Linking fresh rootless nodes beneath the canonical root preserves that root."""
    kanban_home, conn = board_db
    project_id, canonical, _ = _aether_parentage_fixture(kanban_home, conn)
    root = kb.create_task(
        conn,
        title="canonical root",
        assignee="supervisor",
        project_id=project_id,
        workspace_kind="dir",
        workspace_path=canonical,
    )
    assert kb.opt_in_collaboration(
        conn,
        root,
        contract_id="oc_hf460",
        contract_version="1",
        session_id="origin-session",
        board="test_board",
    )
    decision = kb.create_task(
        conn, title="rootless decision", assignee="supervisor", project_id=project_id
    )
    existing_unit = kb.create_task(
        conn,
        title="existing rootless descendant",
        assignee="implementer",
        project_id=project_id,
        parents=(decision,),
    )

    kb.link_tasks(conn, root, decision)
    assert kb.get_collaboration_root(conn, decision) == root
    assert kb.get_collaboration_root(conn, existing_unit) == root

    later_unit = kb.create_task(
        conn, title="later rootless unit", assignee="implementer", project_id=project_id
    )
    kb.link_tasks(conn, decision, later_unit)
    assert kb.get_collaboration_root(conn, later_unit) == root

    # Aether identity on the board must not restrict an unrelated generic DAG.
    generic_a = kb.create_task(conn, title="generic root A", assignee="worker")
    generic_b = kb.create_task(conn, title="generic root B", assignee="worker")
    generic_child = kb.create_task(conn, title="generic child", assignee="worker")
    kb.link_tasks(conn, generic_a, generic_child)
    kb.link_tasks(conn, generic_b, generic_child)
    assert kb.get_collaboration_root(conn, generic_child) is None


def test_aether_link_rejects_project_mismatch_before_side_effects(
    board_db: tuple[Path, sqlite3.Connection],
    tmp_path: Path,
) -> None:
    """A same-root edge cannot attach a foreign-Project descendant."""
    from hermes_cli import projects_db

    kanban_home, conn = board_db
    project_id, canonical, _ = _aether_parentage_fixture(kanban_home, conn)
    with projects_db.connect_closing() as pconn:
        foreign_project = projects_db.create_project(
            pconn,
            name=f"HF-460 foreign {tmp_path.name}",
            primary_path=str(tmp_path / "foreign"),
        )
    Path(canonical).mkdir()
    root = kb.create_task(
        conn,
        title="canonical root",
        assignee="supervisor",
        project_id=project_id,
        workspace_kind="dir",
        workspace_path=canonical,
    )
    assert kb.opt_in_collaboration(
        conn,
        root,
        contract_id="oc_hf460",
        contract_version="1",
        session_id="origin-session",
        board="test_board",
    )
    child = kb.create_task(
        conn,
        title="foreign child",
        assignee="implementer",
        project_id=foreign_project,
        workspace_kind="dir",
        workspace_path=str(tmp_path / "foreign-child"),
    )

    before = _parentage_snapshot(conn)
    with pytest.raises(ValueError, match="canonical Aether collaboration Project"):
        kb.link_tasks(conn, root, child)
    assert _parentage_snapshot(conn) == before


def test_aether_parentage_rejects_incomplete_project_identity_claim_atomically(
    board_db: tuple[Path, sqlite3.Connection],
) -> None:
    """A persisted exact opt-in cannot be extended after its Aether Project binding is lost."""
    kanban_home, conn = board_db
    project_id, canonical, candidate = _aether_parentage_fixture(kanban_home, conn)
    Path(canonical).mkdir()
    Path(candidate).mkdir()
    root = kb.create_task(
        conn,
        title="canonical root",
        assignee="supervisor",
        project_id=project_id,
        workspace_kind="dir",
        workspace_path=canonical,
    )
    assert kb.opt_in_collaboration(
        conn,
        root,
        contract_id="oc_hf460",
        contract_version="1",
        session_id="origin-session",
        board="test_board",
    )

    metadata_path = kb.board_metadata_path("test_board")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata.pop("aether_project_id")
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    before = _parentage_snapshot(conn)
    with pytest.raises(
        ValueError, match="malformed or mismatched Aether collaboration claim"
    ):
        kb.create_task(
            conn,
            title="unreviewable child",
            assignee="implementer",
            project_id=project_id,
            parents=(root,),
            workspace_kind="dir",
            workspace_path=str(Path(candidate) / "must-not-exist"),
        )
    assert _parentage_snapshot(conn) == before
    assert kb.get_collaboration_root(conn, root) == root


@pytest.mark.parametrize(
    "damage", ["missing_contract", "changed_project", "missing_metadata"]
)
def test_aether_link_rejects_damaged_board_identity_atomically(
    board_db: tuple[Path, sqlite3.Connection], damage: str
) -> None:
    """Persisted Aether opt-in snapshots stay guarded if board identity is damaged."""
    kanban_home, conn = board_db
    project_id, canonical, candidate = _aether_parentage_fixture(kanban_home, conn)
    root = kb.create_task(
        conn,
        title="canonical root",
        assignee="supervisor",
        project_id=project_id,
        workspace_kind="dir",
        workspace_path=canonical,
    )
    assert kb.opt_in_collaboration(
        conn,
        root,
        contract_id="oc_hf460",
        contract_version="1",
        session_id="origin-session",
        board="test_board",
    )
    decision = kb.create_task(
        conn,
        title="decision",
        assignee="supervisor",
        project_id=project_id,
        parents=(root,),
        workspace_kind="dir",
        workspace_path=canonical,
    )
    unit = kb.create_task(
        conn,
        title="unit",
        assignee="implementer",
        project_id=project_id,
        parents=(decision,),
        workspace_kind="dir",
        workspace_path=candidate,
    )
    unrelated = kb.create_task(
        conn,
        title="unrelated root",
        assignee="supervisor",
        project_id=project_id,
    )
    kb.add_notify_sub(
        conn,
        task_id=unrelated,
        platform="tui",
        chat_id="unrelated-chat",
        delivery_mode="notify",
    )

    metadata_path = kb.board_metadata_path("test_board")
    if damage == "missing_metadata":
        metadata_path.unlink()
    else:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if damage == "missing_contract":
            metadata.pop("aether_contract_id")
        else:
            metadata["project_id"] = "different-project"
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    before = _parentage_snapshot(conn)
    with pytest.raises(
        ValueError, match="malformed or mismatched Aether collaboration claim"
    ):
        kb.link_tasks(conn, unrelated, unit)
    assert _parentage_snapshot(conn) == before
    assert kb.get_collaboration_root(conn, decision) == root
    assert kb.get_collaboration_root(conn, unit) == root


def test_generic_multiroot_create_is_unchanged_by_aether_parentage_guard(
    board_db: tuple[Path, sqlite3.Connection],
) -> None:
    """The opt-in and exact connection-board identity are required for this guard."""
    _, conn = board_db
    root_a = kb.create_task(conn, title="generic root A", assignee="worker")
    root_b = kb.create_task(conn, title="generic root B", assignee="worker")
    child = kb.create_task(
        conn,
        title="generic multi-root child",
        assignee="worker",
        parents=(root_a, root_b),
    )
    assert kb.get_collaboration_root(conn, child) is None


def test_isolated_aether_key_and_ambient_default_board_do_not_corroborate(
    board_db: tuple[Path, sqlite3.Connection],
    tmp_path: Path,
) -> None:
    """Only the actual DB board metadata plus event identity enable the guard."""
    from hermes_cli import projects_db

    kanban_home, conn = board_db
    with projects_db.connect_closing() as pconn:
        project_id = projects_db.create_project(
            pconn, name=f"HF-460 isolated {tmp_path.name}", primary_path=str(tmp_path)
        )

    # Put a fully Aether-looking identity on the ambient default board, but the
    # actual connection is for test_board. An isolated key on that actual board
    # is still insufficient, even with a persisted generic opt-in event.
    default_meta_path = kb.board_metadata_path("default")
    default_meta_path.parent.mkdir(parents=True, exist_ok=True)
    default_meta_path.write_text(
        json.dumps({
            "aether_contract_id": "oc_ambient",
            "aether_contract_version": 1,
            "aether_project_id": "ambient-aether-project",
            "project_id": project_id,
        }),
        encoding="utf-8",
    )
    actual_meta_path = kb.board_metadata_path("test_board")
    actual_meta_path.parent.mkdir(parents=True, exist_ok=True)
    actual_meta_path.write_text(
        json.dumps({
            "slug": "test_board",
            "project_id": project_id,
            "aether_project_id": "isolated-label",
        }),
        encoding="utf-8",
    )

    root = kb.create_task(
        conn,
        title="generic opted-in root",
        assignee="supervisor",
        project_id=project_id,
    )
    assert kb.opt_in_collaboration(
        conn, root, session_id="origin-session", board="test_board"
    )
    independent = kb.create_task(
        conn, title="generic independent root", assignee="worker", project_id=project_id
    )
    child = kb.create_task(
        conn,
        title="generic multi-root child",
        assignee="worker",
        project_id=project_id,
        parents=(root, independent),
    )
    assert kb.get_collaboration_root(conn, child) is None


@pytest.mark.parametrize("attach_second_aether_root", [False, True])
def test_aether_link_tasks_refuses_root_substitution_atomically(
    board_db: tuple[Path, sqlite3.Connection],
    attach_second_aether_root: bool,
) -> None:
    """An opted-in root keeps its exact canonical root under every new edge.

    ``attach_second_aether_root=False`` attaches a generic root above the
    corroborated root; ``True`` attaches a second corroborated root. Both are
    root substitution: they would give existing descendants a different
    collaboration root and leave the flow pre-damaged for review (#475).
    """
    kanban_home, conn = board_db
    project_id, _canonical, candidate = _aether_parentage_fixture(kanban_home, conn)
    Path(candidate).mkdir()
    canonical_root = kb.create_task(
        conn,
        title="canonical root",
        assignee="supervisor",
        project_id=project_id,
        workspace_kind="dir",
        workspace_path=str(Path(candidate) / "canonical"),
    )
    assert kb.opt_in_collaboration(
        conn,
        canonical_root,
        contract_id="oc_hf460",
        contract_version="1",
        session_id="origin-session",
        board="test_board",
    )
    decision = kb.create_task(
        conn,
        title="decision",
        assignee="supervisor",
        project_id=project_id,
        parents=(canonical_root,),
    )
    unit = kb.create_task(
        conn,
        title="unit",
        assignee="implementer",
        project_id=project_id,
        parents=(decision,),
        workspace_kind="dir",
        workspace_path=candidate,
    )

    if attach_second_aether_root:
        intruder = kb.create_task(
            conn,
            title="second corroborated root",
            assignee="supervisor",
            project_id=project_id,
        )
        assert kb.opt_in_collaboration(
            conn,
            intruder,
            contract_id="oc_hf460",
            contract_version="1",
            session_id="other-session",
            board="test_board",
        )
    else:
        intruder = kb.create_task(
            conn,
            title="generic independent root",
            assignee="supervisor",
            project_id=project_id,
        )
    unit_2 = kb.create_task(
        conn,
        title="intruder unit",
        assignee="implementer",
        project_id=project_id,
        parents=(intruder,),
    )
    kb.add_notify_sub(
        conn,
        task_id=unit_2,
        platform="tui",
        chat_id="intruder-chat",
        delivery_mode="notify",
    )
    conn.execute("UPDATE tasks SET status='done' WHERE id=?", (intruder,))
    conn.execute("UPDATE tasks SET status='running' WHERE id=?", (unit_2,))
    conn.commit()

    before = _parentage_snapshot(conn)
    with pytest.raises(ValueError, match="canonical Aether collaboration root"):
        kb.link_tasks(conn, intruder, canonical_root)
    assert _parentage_snapshot(conn) == before
    assert kb.get_collaboration_root(conn, decision) == canonical_root
    assert kb.get_collaboration_root(conn, unit) == canonical_root
    assert kb.get_collaboration_root(conn, canonical_root) == canonical_root


def test_aether_create_task_refuses_second_corroborated_root_atomically(
    board_db: tuple[Path, sqlite3.Connection],
) -> None:
    """A new child cannot be born under two corroborated or two mixed roots."""
    kanban_home, conn = board_db
    project_id, canonical, candidate = _aether_parentage_fixture(kanban_home, conn)
    Path(canonical).mkdir()
    Path(candidate).mkdir()
    canonical_root = kb.create_task(
        conn,
        title="canonical root",
        assignee="supervisor",
        project_id=project_id,
        workspace_kind="dir",
        workspace_path=canonical,
    )
    assert kb.opt_in_collaboration(
        conn,
        canonical_root,
        contract_id="oc_hf460",
        contract_version="1",
        session_id="origin-session",
        board="test_board",
    )
    second_root = kb.create_task(
        conn,
        title="second corroborated root",
        assignee="supervisor",
        project_id=project_id,
    )
    assert kb.opt_in_collaboration(
        conn,
        second_root,
        contract_id="oc_hf460",
        contract_version="1",
        session_id="other-session",
        board="test_board",
    )
    unit = kb.create_task(
        conn,
        title="unit",
        assignee="implementer",
        project_id=project_id,
        parents=(canonical_root,),
        workspace_kind="dir",
        workspace_path=candidate,
    )
    generic_root = kb.create_task(
        conn,
        title="generic root",
        assignee="worker",
        project_id=project_id,
    )

    before = _parentage_snapshot(conn)
    for parents in ((canonical_root, second_root), (canonical_root, generic_root)):
        rejected_workspace = Path(candidate) / f"must-not-exist-{len(parents)}"
        with pytest.raises(ValueError, match="canonical Aether collaboration root"):
            kb.create_task(
                conn,
                title="ambiguous child",
                assignee="implementer",
                project_id=project_id,
                parents=parents,
                workspace_kind="dir",
                workspace_path=str(rejected_workspace),
            )
        assert not rejected_workspace.exists()
    assert _parentage_snapshot(conn) == before
    assert kb.get_collaboration_root(conn, unit) == canonical_root
    assert kb.get_collaboration_root(conn, canonical_root) == canonical_root
    assert kb.get_collaboration_root(conn, second_root) == second_root
