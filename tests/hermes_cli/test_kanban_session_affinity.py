"""Behavioral coverage for generic Kanban worker session affinity."""

import json
import os
import subprocess

import pytest

from hermes_cli import kanban_db as kb


def _project(tmp_path):
    from hermes_cli import projects_db

    with projects_db.connect_closing() as project_conn:
        return projects_db.create_project(
            project_conn, name=f"Project {tmp_path.name}", primary_path=str(tmp_path)
        )


def _task(conn, project_id, *, title="work", flow="flow-7", terminal=False, workspace=None, session_id=None):
    return kb.create_task(
        conn,
        title=title,
        assignee="worker",
        project_id=project_id,
        workspace_kind="dir",
        workspace_path=str(workspace) if workspace else None,
        session_id=session_id,
        session_affinity={"flow_id": flow, "terminal": terminal},
    )


def test_normalize_session_affinity_requires_flow_and_defaults_non_terminal():
    assert kb.normalize_session_affinity({"flow_id": " flow-7 "}) == {
        "flow_id": "flow-7", "terminal": False,
    }


def test_normalize_session_affinity_rejects_missing_flow_id():
    with pytest.raises(ValueError, match="flow_id"):
        kb.normalize_session_affinity({"terminal": True})


def test_affinity_task_requires_a_canonical_project(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban"))
    conn = kb.connect()
    try:
        with pytest.raises(ValueError, match="project"):
            _task(conn, "missing-project", workspace=tmp_path)
    finally:
        conn.close()


def test_affinity_is_persisted_on_a_project_task(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    project_id = _project(tmp_path)
    conn = kb.connect()
    try:
        task_id = _task(conn, project_id, workspace=tmp_path, terminal=True)
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.project_id == project_id
        assert task.session_affinity == {"flow_id": "flow-7", "terminal": True}
    finally:
        conn.close()


def test_affinity_lease_registers_one_exact_worker_session(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    project_id = _project(tmp_path)
    conn = kb.connect()
    try:
        task_id = _task(conn, project_id, workspace=tmp_path)
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None
        lease = kb.reserve_session_affinity(conn, claimed, workspace_path=str(tmp_path))
        assert lease is not None and lease.session_id is None and lease.generation == 1
        assert kb.register_session_affinity(conn, claimed, lease, session_id="session-1")
        stored = kb.get_session_affinity(conn, claimed)
        assert stored is not None
        assert stored["session_id"] == "session-1"
        assert stored["generation"] == 1
    finally:
        conn.close()


def test_affinity_spawn_uses_exact_resume_and_workspace_pin(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    from hermes_state import SessionDB

    sessions = SessionDB(db_path=tmp_path / "profile" / "state.db")
    sessions.create_session("session-1", "kanban", profile_name="worker", cwd=str(workspace))
    sessions.close()
    project_id = _project(tmp_path)
    conn = kb.connect()
    try:
        task_id = _task(conn, project_id, workspace=workspace)
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None
        lease = kb.reserve_session_affinity(conn, claimed, workspace_path=str(workspace))
        assert lease is not None
        kb.register_session_affinity(conn, claimed, lease, session_id="session-1")
    finally:
        conn.close()

    captured = {}

    class FakeProc:
        pid = 123

    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["hermes"])
    monkeypatch.setattr(
        subprocess, "Popen",
        lambda argv, **kwargs: captured.update(argv=argv, kwargs=kwargs) or FakeProc(),
    )
    conn = kb.connect()
    try:
        task = kb.get_task(conn, task_id)
        assert task is not None
        lease = kb.AffinityLease(
            board=lease.board, project_id=lease.project_id, flow_id=lease.flow_id,
            assignee=lease.assignee, generation=lease.generation, token=lease.token,
            session_id="session-1",
        )
        kb._default_spawn(task, str(workspace), affinity=lease)
    finally:
        conn.close()
    argv = captured["argv"]
    assert argv[argv.index("--resume") + 1] == "session-1"
    assert "--no-restore-cwd" in argv
    assert argv[argv.index("--in") + 1] == str(workspace)
    child_env = captured["kwargs"]["env"]
    assert child_env["HERMES_KANBAN_FLOW_ID"] == lease.flow_id
    assert child_env["HERMES_KANBAN_AFFINITY_FLOW_ID"] == lease.flow_id


def test_kanban_worker_session_records_current_workspace(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(workspace))
    from run_agent import _launch_cwd_for_session

    assert _launch_cwd_for_session("kanban") == str(workspace)


def test_worker_resume_rejects_closed_session(tmp_path):
    from hermes_state import SessionDB

    state_db = tmp_path / "state.db"
    sessions = SessionDB(db_path=state_db)
    sessions.create_session("session-1", "kanban", cwd=str(tmp_path))
    sessions.end_session("session-1", "session_reset")
    sessions.close()
    with pytest.raises(kb.AffinityRegistrationError, match="closed"):
        kb.validate_worker_resume_session("session-1", db_path=state_db)


def test_worker_resume_rejects_worker_exit_session(tmp_path):
    from hermes_state import SessionDB

    state_db = tmp_path / "state.db"
    sessions = SessionDB(db_path=state_db)
    sessions.create_session("session-1", "kanban", cwd=str(tmp_path))
    sessions.end_session("session-1", "worker_exit")
    sessions.close()
    with pytest.raises(kb.AffinityRegistrationError, match="closed"):
        kb.validate_worker_resume_session("session-1", db_path=state_db)


def test_session_db_failure_without_affinity_is_retried_cleanly(monkeypatch):
    from run_agent import AIAgent

    class FailingSessionDB:
        def create_session(self, **_kwargs):
            raise RuntimeError("temporary session db failure")

    agent = object.__new__(AIAgent)
    agent._persist_disabled = False
    agent._session_db_created = False
    agent._session_db = FailingSessionDB()
    agent.platform = "cli"
    agent.session_id = "session-1"
    agent._session_init_model_config = None
    agent.model = "test-model"
    agent._cached_system_prompt = None
    agent._parent_session_id = None
    monkeypatch.delenv("HERMES_KANBAN_AFFINITY_TOKEN", raising=False)

    agent._ensure_db_session()

    assert agent._session_db_created is False


def test_session_db_failure_with_affinity_aborts_worker_start(monkeypatch):
    from hermes_cli.kanban_affinity import AffinityRegistrationError
    from run_agent import AIAgent

    class FailingSessionDB:
        def create_session(self, **_kwargs):
            raise RuntimeError("temporary session db failure")

    agent = object.__new__(AIAgent)
    agent._persist_disabled = False
    agent._session_db_created = False
    agent._session_db = FailingSessionDB()
    agent.platform = "kanban"
    agent.session_id = "session-1"
    agent._session_init_model_config = None
    agent.model = "test-model"
    agent._cached_system_prompt = None
    agent._parent_session_id = None
    monkeypatch.setenv("HERMES_KANBAN_AFFINITY_TOKEN", "lease-token")

    with pytest.raises(AffinityRegistrationError, match="session DB creation"):
        agent._ensure_db_session()


def test_worker_resume_rejects_wrong_profile(tmp_path):
    from hermes_state import SessionDB

    state_db = tmp_path / "state.db"
    sessions = SessionDB(db_path=state_db)
    sessions.create_session("session-1", "kanban", profile_name="worker-a", cwd=str(tmp_path))
    sessions.close()
    with pytest.raises(kb.AffinityRegistrationError, match="profile"):
        kb.validate_worker_resume_session(
            "session-1", db_path=state_db, expected_profile="worker-b"
        )


def test_worker_resume_accepts_canonical_default_profile(tmp_path):
    from hermes_state import SessionDB

    state_db = tmp_path / "state.db"
    sessions = SessionDB(db_path=state_db)
    sessions.create_session("session-1", "kanban", cwd=str(tmp_path))
    sessions.close()
    assert kb.validate_worker_resume_session(
        "session-1", db_path=state_db, expected_profile="default"
    )["id"] == "session-1"


def test_corrupt_task_affinity_is_rejected_before_dispatch(tmp_path):
    project_id = _project(tmp_path)
    conn = kb.connect()
    try:
        task_id = _task(conn, project_id, workspace=tmp_path)
        conn.execute(
            "UPDATE tasks SET session_affinity = ? WHERE id = ?",
            ("{not valid json", task_id),
        )
        conn.commit()
        task = kb.claim_task(conn, task_id)
        assert task is not None
        with pytest.raises(kb.AffinityRegistrationError, match="corrupt"):
            kb.reserve_session_affinity(conn, task)
    finally:
        conn.close()


def test_worker_resume_rejects_missing_session(tmp_path):
    with pytest.raises(kb.AffinityRegistrationError, match="missing"):
        kb.validate_worker_resume_session("missing-session", db_path=tmp_path / "state.db")


def test_worker_child_inherits_affinity_only_for_same_assignee(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban"))
    monkeypatch.setenv("HERMES_PROFILE", "worker")
    project_id = _project(tmp_path)
    conn = kb.connect()
    try:
        parent_id = _task(conn, project_id, workspace=tmp_path, session_id="origin-session")
        kb.claim_task(conn, parent_id)
        kb.add_notify_sub(conn, task_id=parent_id, platform="telegram", chat_id="origin-chat")
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", parent_id)
    from tools import kanban_tools

    same = json.loads(kanban_tools._handle_create(
        {"title": "same flow", "assignee": "worker", "parents": [parent_id]}
    ))
    other = json.loads(kanban_tools._handle_create(
        {"title": "other flow", "assignee": "reviewer", "parents": [parent_id]}
    ))
    conn = kb.connect()
    try:
        same_task = kb.get_task(conn, same["task_id"])
        other_task = kb.get_task(conn, other["task_id"])
        assert same_task is not None and other_task is not None
        assert same["session_affinity"] == {"flow_id": "flow-7", "terminal": False}
        assert same_task.session_affinity == {"flow_id": "flow-7", "terminal": False}
        assert same_task.session_id == "origin-session"
        assert same_task.workspace_path == str(tmp_path)
        assert kb.list_notify_subs(conn, task_id=same["task_id"]) == []
        assert other_task.session_affinity is None
        assert kb.list_notify_subs(conn, task_id=other["task_id"]) == []
    finally:
        conn.close()


def test_inherited_affinity_child_is_not_terminal_without_explicit_opt_in(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban"))
    monkeypatch.setenv("HERMES_PROFILE", "worker")
    project_id = _project(tmp_path)
    conn = kb.connect()
    try:
        parent_id = _task(
            conn,
            project_id,
            workspace=tmp_path,
            terminal=True,
            session_id="origin-session",
        )
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", parent_id)
    from tools import kanban_tools

    child = json.loads(
        kanban_tools._handle_create(
            {"title": "non-terminal child", "assignee": "worker", "parents": [parent_id]}
        )
    )
    assert child["session_affinity"] == {"flow_id": "flow-7", "terminal": False}


def test_terminal_child_copies_origin_subscription(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban"))
    monkeypatch.setenv("HERMES_PROFILE", "worker")
    project_id = _project(tmp_path)
    conn = kb.connect()
    try:
        parent_id = _task(conn, project_id, workspace=tmp_path, session_id="origin-session")
        kb.add_notify_sub(conn, task_id=parent_id, platform="telegram", chat_id="origin-chat")
        child_id = _task(conn, project_id, terminal=True, workspace=tmp_path, session_id="origin-session")
        conn.execute("INSERT INTO task_links(parent_id, child_id) VALUES (?, ?)", (parent_id, child_id))
        kb._inherit_notify_subs(conn, child_id, (parent_id,))
        conn.commit()
        assert len(kb.list_notify_subs(conn, task_id=child_id)) == 1
    finally:
        conn.close()


def test_terminal_grandchild_recovers_origin_subscription_after_silent_child(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban"))
    project_id = _project(tmp_path)
    conn = kb.connect()
    try:
        root = kb.create_task(
            conn,
            title="controller",
            assignee="worker",
            project_id=project_id,
            session_id="origin-session",
            workspace_kind="dir",
            workspace_path=str(tmp_path),
            session_affinity={"flow_id": "flow-7"},
        )
        kb.add_notify_sub(conn, task_id=root, platform="telegram", chat_id="origin-chat")
        silent_child = kb.create_task(
            conn,
            title="review",
            assignee="worker",
            project_id=project_id,
            parents=(root,),
            session_id="origin-session",
            workspace_kind="dir",
            workspace_path=str(tmp_path),
            session_affinity={"flow_id": "flow-7"},
        )
        assert kb.list_notify_subs(conn, task_id=silent_child) == []
        terminal = kb.create_task(
            conn,
            title="integrate",
            assignee="worker",
            project_id=project_id,
            parents=(silent_child,),
            session_id="origin-session",
            workspace_kind="dir",
            workspace_path=str(tmp_path),
            session_affinity={"flow_id": "flow-7", "terminal": True},
        )
        assert len(kb.list_notify_subs(conn, task_id=terminal)) == 1
    finally:
        conn.close()


def test_origin_signal_is_the_only_nonterminal_affinity_notification(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    project_id = _project(tmp_path)
    conn = kb.connect()
    try:
        root = kb.create_task(conn, title="controller", assignee="worker", project_id=project_id, session_id="origin-session")
        kb.add_notify_sub(conn, task_id=root, platform="telegram", chat_id="chat-1")
        child = _task(conn, project_id, workspace=tmp_path, session_id="origin-session")
        kb.claim_task(conn, child)
        assert kb.list_notify_subs(conn, task_id=child) == []
        assert kb.block_task(conn, child, reason="need a decision", kind="needs_input", origin_signal="input")
        assert len(kb.list_notify_subs(conn, task_id=child)) == 1
        _, events = kb.unseen_events_for_sub(conn, task_id=child, platform="telegram", chat_id="chat-1")
        assert [event.kind for event in events] == ["origin_signal"]
        assert events[0].payload["origin_signal"] == "input"
    finally:
        conn.close()


def test_affinity_terminal_event_reuses_origin_subscription(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    project_id = _project(tmp_path)
    conn = kb.connect()
    try:
        root = kb.create_task(conn, title="controller", assignee="worker", project_id=project_id, session_id="origin-session")
        kb.add_notify_sub(conn, task_id=root, platform="telegram", chat_id="origin-chat")
        child = _task(conn, project_id, terminal=True, workspace=tmp_path, session_id="origin-session")
        assert kb._route_affinity_terminal(conn, child, reason="session lease lost")
        assert len(kb.list_notify_subs(conn, task_id=child)) == 1
        events = kb.list_events(conn, child)
        assert events[-1].kind == "flow_terminal"
        assert events[-1].payload["reason"] == "session lease lost"
    finally:
        conn.close()


def test_terminal_affinity_completion_emits_flow_terminal(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    project_id = _project(tmp_path)
    conn = kb.connect()
    try:
        root = kb.create_task(conn, title="controller", assignee="worker", project_id=project_id, session_id="origin-session", session_affinity={"flow_id": "flow-7"})
        kb.add_notify_sub(conn, task_id=root, platform="telegram", chat_id="origin-chat")
        child = _task(conn, project_id, terminal=True, workspace=tmp_path, session_id="origin-session")
        conn.execute("INSERT INTO task_links(parent_id, child_id) VALUES (?, ?)", (root, child))
        conn.commit()
        kb.complete_task(conn, root)
        claimed = kb.claim_task(conn, child)
        assert claimed is not None
        lease = kb.reserve_session_affinity(conn, claimed)
        assert lease is not None
        assert kb.complete_task(conn, child, result="integrated")
        assert kb.get_session_affinity(conn, child)["lease_token"] is None
        assert any(event.kind == "flow_terminal" for event in kb.list_events(conn, child))
    finally:
        conn.close()


def test_missing_affinity_lease_is_rejected_after_prior_run(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban"))
    project_id = _project(tmp_path)
    conn = kb.connect()
    try:
        first_id = _task(conn, project_id, workspace=tmp_path)
        first = kb.claim_task(conn, first_id)
        assert first is not None
        lease = kb.reserve_session_affinity(conn, first)
        assert lease is not None
        kb.register_session_affinity(conn, first, lease, session_id="session-1")
        kb.complete_task(conn, first_id)
        conn.execute("DELETE FROM kanban_session_affinity")
        conn.commit()
        second_id = _task(conn, project_id, workspace=tmp_path)
        second = kb.claim_task(conn, second_id)
        assert second is not None
        with pytest.raises(kb.AffinityRegistrationError, match="missing"):
            kb.reserve_session_affinity(conn, second)
    finally:
        conn.close()


def test_affinity_busy_and_generation_fence(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban"))
    project_id = _project(tmp_path)
    conn = kb.connect()
    try:
        first_id = _task(conn, project_id, workspace=tmp_path)
        second_id = _task(conn, project_id, workspace=tmp_path)
        first = kb.claim_task(conn, first_id)
        second = kb.claim_task(conn, second_id)
        assert first is not None and second is not None
        first_lease = kb.reserve_session_affinity(conn, first)
        assert first_lease is not None
        conn.execute(
            "UPDATE kanban_session_affinity SET session_id = 'session-1'"
        )
        conn.commit()
        with pytest.raises(kb.AffinityBusy):
            kb.reserve_session_affinity(conn, second)
        kb.release_session_affinity(conn, first_lease)
        second_lease = kb.reserve_session_affinity(conn, second)
        assert second_lease is not None
        assert second_lease.generation == first_lease.generation + 1
        with pytest.raises(kb.AffinityRegistrationError, match="stale"):
            kb.register_session_affinity(conn, first, first_lease, session_id="old-session")
    finally:
        conn.close()


def test_unrecoverable_affinity_spawn_failure_routes_flow_terminal(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    project_id = _project(tmp_path)
    conn = kb.connect()
    try:
        root = kb.create_task(conn, title="controller", assignee="worker", project_id=project_id, session_id="origin-session", session_affinity={"flow_id": "flow-7"})
        kb.add_notify_sub(conn, task_id=root, platform="telegram", chat_id="origin-chat")
        task_id = _task(conn, project_id, workspace=tmp_path)
    finally:
        conn.close()
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda _: True)

    def spawn(*_args, **_kwargs):
        raise RuntimeError("session resume unavailable")

    conn = kb.connect()
    try:
        result = kb.dispatch_once(conn, spawn_fn=spawn, failure_limit=1)
        assert task_id in result.auto_blocked
        assert kb.get_task(conn, task_id).status == "blocked"
        assert any(event.kind == "flow_terminal" for event in kb.list_events(conn, task_id))
    finally:
        conn.close()
