"""Tests for the decomposer module + `hermes kanban decompose` CLI surface.

The auxiliary LLM client is mocked — no network calls. Tests exercise the
prompt plumbing, response parsing, DB writes (via the real DB helper),
and the assignee-fallback logic.
"""

from __future__ import annotations

import json as jsonlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_decompose as decomp


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _fake_aux_response(content: str):
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = content
    return resp


def _mock_client_returning(content: str):
    client = MagicMock()
    client.chat.completions.create = MagicMock(return_value=_fake_aux_response(content))
    return client


def _patch_aux_client(content: str, *, model: str = "test-model"):
    # decompose_task now routes through call_llm (see #35566) — mock it at
    # the source module so task config, extra_body, and retries stay out of
    # unit-test scope.
    return patch(
        "agent.auxiliary_client.call_llm",
        return_value=_fake_aux_response(content),
    )


def _patch_extra_body():
    # No-op shim retained for call-site compatibility: extra_body plumbing
    # now lives inside call_llm, which _patch_aux_client already mocks.
    return patch("agent.auxiliary_client.get_auxiliary_extra_body", return_value={})


def _patch_list_profiles(names: list[str]):
    """Pretend the named profiles exist. The decomposer uses
    profiles_mod.list_profiles() to build the roster + valid-set, and
    profiles_mod.profile_exists() to resolve orchestrator/default."""
    from types import SimpleNamespace
    fake_profiles = [
        SimpleNamespace(
            name=n, is_default=(i == 0), description=f"desc for {n}",
            description_auto=False, model="m", provider="p", skill_count=1,
        )
        for i, n in enumerate(names)
    ]
    return [
        patch("hermes_cli.profiles.list_profiles", return_value=fake_profiles),
        patch("hermes_cli.profiles.profile_exists", side_effect=lambda x: x in names),
        patch("hermes_cli.profiles.get_active_profile_name", return_value=names[0] if names else "default"),
    ]


def _escalate_via_block_loop(conn, task_id: str, *, kind: str | None = None):
    """Replay the durable block -> unblock -> same-kind block sequence."""
    assert kb.block_task(conn, task_id, reason="review-required: inspect", kind=kind)
    assert kb.unblock_task(conn, task_id)
    assert kb.block_task(conn, task_id, reason="review-required: inspect", kind=kind)
    return kb.get_task(conn, task_id)


def _task_snapshot(conn, task_id: str) -> dict:
    row = conn.execute(
        "SELECT title, body, assignee, created_by, workspace_kind, "
        "workspace_path, branch_name, status, block_kind, block_recurrences "
        "FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    assert row is not None
    return {
        "task": dict(row),
        "parents": kb.parent_ids(conn, task_id),
        "children": kb.child_ids(conn, task_id),
        "count": conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
    }


def test_decompose_with_fanout_creates_children(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="ship a feature", triage=True)

    llm_payload = jsonlib.dumps({
        "fanout": True,
        "rationale": "test split",
        "tasks": [
            {"title": "research", "body": "look it up", "assignee": "researcher", "parents": []},
            {"title": "build", "body": "code it", "assignee": "engineer", "parents": [0]},
        ],
    })

    patches = _patch_list_profiles(["orchestrator", "researcher", "engineer"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(llm_payload), _patch_extra_body():
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok, outcome.reason
    assert outcome.fanout is True
    assert outcome.child_ids and len(outcome.child_ids) == 2

    with kb.connect() as conn:
        root = kb.get_task(conn, tid)
        c0 = kb.get_task(conn, outcome.child_ids[0])
        c1 = kb.get_task(conn, outcome.child_ids[1])
    assert root.status == "todo"
    assert c0.status == "ready"
    assert c1.status == "todo"
    assert c0.assignee == "researcher"
    assert c1.assignee == "engineer"


def test_decompose_fanout_false_invalid_llm_assignee_uses_default(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="route me safely", triage=True)

    llm_payload = jsonlib.dumps({
        "fanout": False,
        "rationale": "single unit",
        "title": "Tightened title",
        "body": "Route to fallback.",
        "assignee": "made_up",
    })

    patches = _patch_list_profiles(["orchestrator", "fallback"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(llm_payload), _patch_extra_body(), patch(
            "hermes_cli.kanban_decompose._load_config",
            return_value={"kanban": {"default_assignee": "fallback"}},
        ):
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok, outcome.reason
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
    assert task is not None
    assert task.assignee == "fallback"


@pytest.mark.parametrize("kind", [None, "capability"])
def test_escalated_triage_is_excluded_and_auto_refused_before_llm(kanban_home, kind):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="review handoff", body="preserve this")
        task = _escalate_via_block_loop(conn, tid, kind=kind)
        assert task.status == "triage"
        assert task.block_kind == kind
        assert kb.is_block_loop_escalated(conn, tid)

    assert tid not in decomp.list_triage_ids()
    with patch("agent.auxiliary_client.call_llm") as call_llm:
        outcome = decomp.decompose_task(
            tid,
            author=decomp.AUTO_DECOMPOSER_AUTHOR,
        )
    assert outcome.ok is False
    assert "refusing" in outcome.reason
    call_llm.assert_not_called()

    with kb.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "triage"
        assert not any(
            event.kind == "triage_escalation_recovered"
            for event in kb.list_events(conn, tid)
        )


def test_auto_refusal_preserves_worktree_snapshot_and_graph(kanban_home, tmp_path):
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="upstream", assignee="parent")
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (parent,))
        conn.commit()
        tid = kb.create_task(
            conn,
            title="preserve title",
            body="preserve body",
            assignee="worker",
            created_by="operator",
            workspace_kind="worktree",
            workspace_path=str(tmp_path / "repo" / "checkout"),
            branch_name="feature/preserve",
        )
        child = kb.create_task(conn, title="downstream", assignee="child")
        kb.link_tasks(conn, parent_id=parent, child_id=tid)
        kb.link_tasks(conn, parent_id=tid, child_id=child)
        _escalate_via_block_loop(conn, tid, kind=None)
        before = _task_snapshot(conn, tid)

    with patch("agent.auxiliary_client.call_llm") as call_llm:
        outcome = decomp.decompose_task(
            tid,
            author=decomp.AUTO_DECOMPOSER_AUTHOR,
        )
    assert outcome.ok is False
    call_llm.assert_not_called()

    with kb.connect_closing() as conn:
        assert _task_snapshot(conn, tid) == before


def test_fresh_triage_is_still_listed_for_auto_decompose(kanban_home):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="fresh idea", triage=True)
    assert tid in decomp.list_triage_ids()


def test_escalation_predicate_uses_event_order_not_mutable_fields(kanban_home):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="event ordering", triage=True)
        conn.execute(
            "UPDATE tasks SET block_kind='capability', block_recurrences=99 "
            "WHERE id=?",
            (tid,),
        )
        conn.commit()
        assert not kb.is_block_loop_escalated(conn, tid)

        with kb.write_txn(conn):
            kb._append_event(conn, tid, "block_loop_detected")
        assert kb.is_block_loop_escalated(conn, tid)
        assert kb.specify_triage_task(
            conn,
            tid,
            body="operator-routed work",
            author="operator",
        )
        assert kb.recover_escalated_triage_task(conn, tid)
        assert not kb.is_block_loop_escalated(conn, tid)

        with kb.write_txn(conn):
            kb._append_event(conn, tid, "block_loop_detected")
        assert kb.is_block_loop_escalated(conn, tid)


def test_triage_feed_preserves_tenant_and_priority_order(kanban_home):
    with kb.connect_closing() as conn:
        low = kb.create_task(
            conn, title="low", triage=True, tenant="alpha", priority=1,
        )
        high = kb.create_task(
            conn, title="high", triage=True, tenant="alpha", priority=9,
        )
        other = kb.create_task(
            conn, title="other tenant", triage=True, tenant="beta", priority=50,
        )
        excluded = kb.create_task(
            conn, title="excluded", tenant="alpha", priority=100,
        )
        _escalate_via_block_loop(conn, excluded, kind=None)

    assert decomp.list_triage_ids(tenant="alpha") == [high, low]
    assert decomp.list_triage_ids(tenant="beta") == [other]


def test_failed_manual_decompose_keeps_escalation_and_snapshot(kanban_home):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="manual decision", body="unchanged")
        _escalate_via_block_loop(conn, tid, kind="capability")
        before = _task_snapshot(conn, tid)

    patches = _patch_list_profiles(["orchestrator", "fallback"])
    for p in patches:
        p.start()
    try:
        with patch(
            "hermes_cli.kanban_decompose._load_config",
            return_value={"kanban": {"default_assignee": "fallback"}},
        ), patch(
            "agent.auxiliary_client.call_llm",
            side_effect=RuntimeError("auxiliary unavailable"),
        ):
            outcome = decomp.decompose_task(tid, author="operator")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok is False
    with kb.connect_closing() as conn:
        assert _task_snapshot(conn, tid) == before
        assert kb.is_block_loop_escalated(conn, tid)
        assert tid not in decomp.list_triage_ids()
        assert not any(
            event.kind == "triage_escalation_recovered"
            for event in kb.list_events(conn, tid)
        )


def test_malformed_manual_decompose_keeps_escalation(kanban_home):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="malformed response")
        _escalate_via_block_loop(conn, tid, kind="capability")

    patches = _patch_list_profiles(["orchestrator", "fallback"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client("not valid json"):
            outcome = decomp.decompose_task(tid, author="operator")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok is False
    with kb.connect_closing() as conn:
        assert kb.is_block_loop_escalated(conn, tid)
        assert tid not in decomp.list_triage_ids()
        assert not any(
            event.kind == "triage_escalation_recovered"
            for event in kb.list_events(conn, tid)
        )


def test_failed_manual_db_promotion_does_not_recover(kanban_home):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="db failure")
        _escalate_via_block_loop(conn, tid, kind="capability")

    payload = jsonlib.dumps({
        "fanout": False,
        "title": "would change",
        "body": "would change",
    })
    patches = _patch_list_profiles(["orchestrator", "fallback"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(payload), patch(
            "hermes_cli.kanban_decompose._load_config",
            return_value={"kanban": {"default_assignee": "fallback"}},
        ), patch.object(kb, "specify_triage_task", return_value=False):
            outcome = decomp.decompose_task(tid, author="operator")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok is False
    with kb.connect_closing() as conn:
        assert kb.is_block_loop_escalated(conn, tid)
        assert not any(
            event.kind == "triage_escalation_recovered"
            for event in kb.list_events(conn, tid)
        )


def test_manual_fanout_recovers_only_after_atomic_graph_success(kanban_home):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="fan out manually")
        _escalate_via_block_loop(conn, tid, kind="capability")

    payload = jsonlib.dumps({
        "fanout": True,
        "tasks": [
            {"title": "research", "body": "inspect", "assignee": "researcher", "parents": []},
            {"title": "build", "body": "implement", "assignee": "engineer", "parents": [0]},
        ],
    })
    patches = _patch_list_profiles(["orchestrator", "researcher", "engineer"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(payload), _patch_extra_body():
            outcome = decomp.decompose_task(tid, author="operator")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok, outcome.reason
    assert outcome.child_ids and len(outcome.child_ids) == 2
    with kb.connect_closing() as conn:
        task = kb.get_task(conn, tid)
        assert task.status == "todo"
        assert task.block_kind is None
        assert task.block_recurrences == 0
        assert not kb.is_block_loop_escalated(conn, tid)
        assert any(
            event.kind == "triage_escalation_recovered"
            for event in kb.list_events(conn, tid)
        )


def test_decompose_returns_false_when_task_not_triage(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="x")  # ready, not triage

    patches = _patch_list_profiles(["orchestrator"])
    for p in patches:
        p.start()
    try:
        outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()
    assert outcome.ok is False
    assert "not in triage" in outcome.reason


