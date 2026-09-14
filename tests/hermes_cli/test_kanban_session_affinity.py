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


def _opt_in_aether_review(conn, root_id, project_id, *, contract_id="oc_review"):
    """Mark the default test board/root as one exact Aether contract flow."""
    meta_path = kb.board_metadata_path()
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(
        json.dumps(
            {
                "aether_contract_id": contract_id,
                "aether_contract_version": 1,
                "aether_project_id": "aether-project-test",
                "project_id": project_id,
            }
        ),
        encoding="utf-8",
    )
    assert kb.opt_in_collaboration(
        conn,
        root_id,
        contract_id=contract_id,
        contract_version="1",
        session_id="origin-session",
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


@pytest.mark.parametrize("persisted_affinity", [None, "{not valid json"])
def test_standard_affinity_registration_rejects_removed_or_corrupt_card_affinity(
    tmp_path, monkeypatch, persisted_affinity
):
    """A normal affinity lease cannot be reinterpreted as derived review."""
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban"))
    project_id = _project(tmp_path)
    conn = kb.connect()
    try:
        task_id = _task(conn, project_id, workspace=tmp_path)
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None
        lease = kb.reserve_session_affinity(conn, claimed, workspace_path=str(tmp_path))
        assert lease is not None
        conn.execute(
            "UPDATE tasks SET session_affinity=? WHERE id=?",
            (persisted_affinity, task_id),
        )
        conn.commit()
        changed = kb.get_task(conn, task_id)
        assert changed is not None
        with pytest.raises(kb.AffinityRegistrationError, match="identity does not match"):
            kb.register_session_affinity(
                conn,
                changed,
                lease,
                session_id="unexpected-session",
            )
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


def test_affinity_review_spawn_separates_session_workspace_from_candidate_workspace(
    tmp_path, monkeypatch
):
    """A resumed reviewer keeps its flow cwd while tools target the candidate tree."""
    canonical = tmp_path / "supervisor-root"
    candidate = tmp_path / "implementer-candidate"
    profile_home = tmp_path / "supervisor-profile"
    canonical.mkdir()
    candidate.mkdir()
    profile_home.mkdir()

    task = kb.Task(
        id="t_review",
        title="review implementation",
        body=None,
        assignee="supervisor",
        status="running",
        priority=0,
        created_by="implementer",
        created_at=1,
        started_at=1,
        completed_at=None,
        workspace_kind="worktree",
        workspace_path=str(candidate),
        claim_lock="claim",
        claim_expires=None,
        tenant=None,
        branch_name="project/t_review",
        project_id="p_review",
        current_run_id=7,
        session_affinity=None,
    )
    lease = kb.AffinityLease(
        board="review-board",
        project_id="p_review",
        flow_id="flow-review",
        assignee="supervisor",
        generation=2,
        token="lease-token",
        session_id="supervisor-session",
    )
    captured = {}

    class FakeProc:
        pid = 456

    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["hermes"])
    monkeypatch.setattr(kb, "_resolve_worker_cli_toolsets", lambda _home: None)
    monkeypatch.setattr(
        kb,
        "validate_worker_resume_session",
        lambda session_id, **kwargs: captured.update(
            validated_session=session_id, validated_kwargs=kwargs
        )
        or {"id": session_id},
    )
    from hermes_cli import profiles

    monkeypatch.setattr(profiles, "resolve_profile_env", lambda _profile: str(profile_home))
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda argv, **kwargs: captured.update(argv=argv, kwargs=kwargs) or FakeProc(),
    )

    kb._default_spawn(
        task,
        str(candidate),
        affinity=lease,
        session_workspace=str(canonical),
    )

    assert captured["validated_session"] == "supervisor-session"
    assert captured["validated_kwargs"]["workspace_path"] == str(canonical)
    argv = captured["argv"]
    assert argv[argv.index("--resume") + 1] == "supervisor-session"
    assert argv[argv.index("--in") + 1] == str(canonical)
    child_env = captured["kwargs"]["env"]
    assert child_env["HERMES_KANBAN_SESSION_WORKSPACE"] == str(canonical)
    assert child_env["HERMES_KANBAN_REVIEW_AFFINITY"] == "1"
    assert child_env["HERMES_KANBAN_WORKSPACE"] == str(candidate)
    assert child_env["TERMINAL_CWD"] == str(candidate)
    assert captured["kwargs"]["cwd"] == str(canonical)


def test_review_dispatch_reuses_supervisor_flow_session_without_rewriting_unit_workspace(
    tmp_path, monkeypatch
):
    """Same-card review resumes the flow Supervisor but leaves the unit worktree intact."""
    kanban_home = tmp_path / "kanban"
    profile_home = tmp_path / "profile"
    canonical = tmp_path / "supervisor-root"
    candidate = tmp_path / "implementer-candidate"
    canonical.mkdir()
    candidate.mkdir()
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(kanban_home))
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    project_id = _project(tmp_path)

    conn = kb.connect()
    try:
        root = kb.create_task(
            conn,
            title="decompose",
            assignee="supervisor",
            project_id=project_id,
            workspace_kind="dir",
            workspace_path=str(canonical),
            session_affinity={"flow_id": "flow-review", "terminal": False},
        )
        _opt_in_aether_review(conn, root, project_id)
        claimed_root = kb.claim_task(conn, root)
        assert claimed_root is not None
        root_lease = kb.reserve_session_affinity(
            conn, claimed_root, workspace_path=str(canonical)
        )
        assert root_lease is not None
        assert kb.register_session_affinity(
            conn, claimed_root, root_lease, session_id="supervisor-session"
        )
        assert kb.complete_task(conn, root, result="decomposed")

        unit = kb.create_task(
            conn,
            title="implement",
            assignee="implementer",
            project_id=project_id,
            parents=(root,),
            workspace_kind="dir",
            workspace_path=str(candidate),
            skills=("implementation-evidence",),
            model_override="implementer-model",
            provider_override="custom:implementer-provider",
            reasoning_effort="high",
        )
        claimed_unit = kb.claim_task(conn, unit)
        assert claimed_unit is not None
        assert kb.request_review(
            conn,
            unit,
            summary="candidate ready",
            reviewer="supervisor",
            expected_run_id=claimed_unit.current_run_id,
        )
    finally:
        conn.close()

    from hermes_cli import profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda _profile: True)
    captured = {}

    def fake_spawn(task, workspace, *, board=None, affinity=None, session_workspace=None):
        captured.update(
            task=task,
            workspace=workspace,
            board=board,
            affinity=affinity,
            session_workspace=session_workspace,
        )
        return 999

    conn = kb.connect()
    try:
        result = kb.dispatch_once(conn, spawn_fn=fake_spawn, max_spawn=1)
        assert [row[0] for row in result.spawned] == [unit]
        assert captured["workspace"] == str(candidate)
        assert captured["session_workspace"] == str(canonical)
        assert captured["affinity"] is not None
        assert captured["affinity"].session_id == "supervisor-session"
        assert captured["affinity"].flow_id == "flow-review"
        assert captured["task"].skills == []
        assert captured["task"].model_override is None
        assert captured["task"].provider_override is None
        assert captured["task"].reasoning_effort is None

        persisted = kb.get_task(conn, unit)
        assert persisted is not None
        assert persisted.workspace_path == str(candidate)
        assert persisted.session_affinity is None
        assert persisted.skills == ["implementation-evidence"]
        assert persisted.model_override == "implementer-model"
        assert persisted.provider_override == "custom:implementer-provider"
        assert persisted.reasoning_effort == "high"
        lease_state = conn.execute(
            "SELECT session_id, workspace_path, owner_task_id FROM kanban_session_affinity "
            "WHERE project_id = ? AND flow_id = ? AND assignee = ?",
            (project_id, "flow-review", "supervisor"),
        ).fetchone()
        assert lease_state is not None
        assert lease_state["session_id"] == "supervisor-session"
        assert lease_state["workspace_path"] == str(canonical)
        assert lease_state["owner_task_id"] == unit
    finally:
        conn.close()


def test_review_rework_rereview_and_terminal_reuse_one_supervisor_session(
    tmp_path, monkeypatch
):
    """One Aether flow keeps the exact Supervisor session across all phases."""
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban"))
    canonical = tmp_path / "supervisor-root"
    candidate = tmp_path / "candidate"
    canonical.mkdir()
    candidate.mkdir()
    project_id = _project(tmp_path)

    conn = kb.connect()
    try:
        root = kb.create_task(
            conn,
            title="decompose",
            assignee="supervisor",
            project_id=project_id,
            workspace_kind="dir",
            workspace_path=str(canonical),
            session_id="origin-session",
            session_affinity={"flow_id": "flow-review", "terminal": False},
        )
        _opt_in_aether_review(conn, root, project_id)
        root_run = kb.claim_task(conn, root)
        assert root_run is not None
        root_lease = kb.reserve_session_affinity(
            conn, root_run, workspace_path=str(canonical)
        )
        assert root_lease is not None
        assert kb.register_session_affinity(
            conn, root_run, root_lease, session_id="supervisor-session"
        )
        assert kb.complete_task(conn, root, expected_run_id=root_run.current_run_id)

        unit = kb.create_task(
            conn,
            title="implement",
            assignee="implementer",
            project_id=project_id,
            parents=(root,),
            workspace_kind="dir",
            workspace_path=str(candidate),
            skills=("implementation-evidence",),
        )
        terminal = kb.create_task(
            conn,
            title="integrate",
            assignee="supervisor",
            project_id=project_id,
            parents=(root, unit),
            workspace_kind="dir",
            workspace_path=str(canonical),
            session_id="origin-session",
            session_affinity={"flow_id": "flow-review", "terminal": True},
        )
        impl_run = kb.claim_task(conn, unit)
        assert impl_run is not None
        assert kb.request_review(
            conn,
            unit,
            summary="candidate one",
            reviewer="supervisor",
            expected_run_id=impl_run.current_run_id,
        )
    finally:
        conn.close()

    from hermes_cli import profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda _profile: True)
    spawns = []

    def fake_spawn(
        task,
        workspace,
        *,
        board=None,
        affinity=None,
        session_workspace=None,
    ):
        spawns.append(
            {
                "task": task.id,
                "workspace": workspace,
                "session_workspace": session_workspace,
                "session_id": (
                    affinity.session_id
                    if affinity is not None
                    else None
                ),
                "generation": (
                    affinity.generation
                    if affinity is not None
                    else None
                ),
            }
        )
        return 3000 + len(spawns)

    conn = kb.connect()
    try:
        first = kb.dispatch_once(conn, spawn_fn=fake_spawn, max_spawn=1)
        assert [row[0] for row in first.spawned] == [unit]
        first_review = kb.get_task(conn, unit)
        assert first_review is not None and first_review.status == "running"
        assert spawns[-1]["session_id"] == "supervisor-session"
        assert spawns[-1]["workspace"] == str(candidate)
        assert spawns[-1]["session_workspace"] == str(canonical)
        first_generation = spawns[-1]["generation"]

        ok, returned = kb.request_changes(
            conn,
            unit,
            reason="bounded correction",
            expected_run_id=first_review.current_run_id,
        )
        assert ok and returned == "implementer"
        lease_row = conn.execute(
            "SELECT lease_token, session_id FROM kanban_session_affinity "
            "WHERE project_id=? AND flow_id='flow-review' AND assignee='supervisor'",
            (project_id,),
        ).fetchone()
        assert lease_row is not None
        assert lease_row["lease_token"] is None
        assert lease_row["session_id"] == "supervisor-session"

        second_impl = kb.claim_task(conn, unit)
        assert second_impl is not None and second_impl.assignee == "implementer"
        assert kb.request_review(
            conn,
            unit,
            summary="candidate two",
            expected_run_id=second_impl.current_run_id,
        )
        second = kb.dispatch_once(conn, spawn_fn=fake_spawn, max_spawn=1)
        assert [row[0] for row in second.spawned] == [unit]
        second_review = kb.get_task(conn, unit)
        assert second_review is not None and second_review.status == "running"
        assert spawns[-1]["session_id"] == "supervisor-session"
        assert spawns[-1]["generation"] > first_generation
        assert kb.complete_task(
            conn,
            unit,
            summary="reviewed and approved",
            expected_run_id=second_review.current_run_id,
        )

        final_dispatch = kb.dispatch_once(conn, spawn_fn=fake_spawn, max_spawn=1)
        assert [row[0] for row in final_dispatch.spawned] == [terminal]
        assert spawns[-1]["session_id"] == "supervisor-session"
        assert spawns[-1]["workspace"] == str(canonical)
        persisted_unit = kb.get_task(conn, unit)
        assert persisted_unit is not None and persisted_unit.session_affinity is None
    finally:
        conn.close()


def test_derived_review_keeps_two_argument_spawn_workspace_contract(tmp_path, monkeypatch):
    """Legacy/test spawn callbacks still receive the candidate as arg two."""
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban"))
    canonical = tmp_path / "supervisor"
    candidate = tmp_path / "candidate"
    canonical.mkdir()
    candidate.mkdir()
    project_id = _project(tmp_path)
    conn = kb.connect()
    try:
        root = kb.create_task(
            conn,
            title="root",
            assignee="supervisor",
            project_id=project_id,
            workspace_kind="dir",
            workspace_path=str(canonical),
            session_affinity={"flow_id": "flow-review"},
        )
        _opt_in_aether_review(conn, root, project_id)
        root_run = kb.claim_task(conn, root)
        assert root_run is not None
        lease = kb.reserve_session_affinity(conn, root_run)
        assert lease is not None
        assert kb.register_session_affinity(
            conn, root_run, lease, session_id="supervisor-session"
        )
        assert kb.complete_task(conn, root)
        unit = kb.create_task(
            conn,
            title="unit",
            assignee="implementer",
            project_id=project_id,
            parents=(root,),
            workspace_kind="dir",
            workspace_path=str(candidate),
        )
        impl = kb.claim_task(conn, unit)
        assert impl is not None
        assert kb.request_review(
            conn,
            unit,
            reviewer="supervisor",
            expected_run_id=impl.current_run_id,
        )
    finally:
        conn.close()

    from hermes_cli import profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda _profile: True)
    captured = {}

    def old_spawn(task, workspace):
        captured.update(task=task.id, workspace=workspace)
        return 8123

    conn = kb.connect()
    try:
        result = kb.dispatch_once(conn, spawn_fn=old_spawn, max_spawn=1)
        assert [row[0] for row in result.spawned] == [unit]
        assert captured == {"task": unit, "workspace": str(candidate)}
    finally:
        conn.close()


def test_derived_review_affinity_busy_returns_card_to_review_without_failure(
    tmp_path, monkeypatch
):
    """A busy Supervisor session defers review without charging an attempt."""
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban"))
    canonical = tmp_path / "supervisor"
    candidate = tmp_path / "candidate"
    canonical.mkdir()
    candidate.mkdir()
    project_id = _project(tmp_path)
    conn = kb.connect()
    try:
        root = kb.create_task(
            conn,
            title="root",
            assignee="supervisor",
            project_id=project_id,
            workspace_kind="dir",
            workspace_path=str(canonical),
            session_affinity={"flow_id": "flow-review"},
        )
        _opt_in_aether_review(conn, root, project_id)
        root_run = kb.claim_task(conn, root)
        assert root_run is not None
        lease = kb.reserve_session_affinity(conn, root_run)
        assert lease is not None
        assert kb.register_session_affinity(
            conn, root_run, lease, session_id="supervisor-session"
        )
        assert kb.complete_task(conn, root)

        active_controller = kb.create_task(
            conn,
            title="active supervisor phase",
            assignee="supervisor",
            project_id=project_id,
            parents=(root,),
            workspace_kind="dir",
            workspace_path=str(canonical),
            session_affinity={"flow_id": "flow-review"},
        )
        active = kb.claim_task(conn, active_controller)
        assert active is not None
        active_lease = kb.reserve_session_affinity(conn, active)
        assert active_lease is not None
        assert active_lease.session_id == "supervisor-session"

        unit = kb.create_task(
            conn,
            title="unit",
            assignee="implementer",
            project_id=project_id,
            parents=(root,),
            workspace_kind="dir",
            workspace_path=str(candidate),
        )
        impl = kb.claim_task(conn, unit)
        assert impl is not None
        assert kb.request_review(
            conn,
            unit,
            reviewer="supervisor",
            expected_run_id=impl.current_run_id,
        )
    finally:
        conn.close()

    from hermes_cli import profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda _profile: True)
    spawned = []

    def should_not_spawn(task, workspace, **kwargs):
        spawned.append((task.id, workspace, kwargs))
        return 9999

    conn = kb.connect()
    try:
        result = kb.dispatch_once(conn, spawn_fn=should_not_spawn, max_spawn=5)
        assert spawned == []
        assert [task_id for task_id, _reason in result.affinity_deferred] == [unit]
        deferred = kb.get_task(conn, unit)
        assert deferred is not None
        assert deferred.status == "review"
        assert deferred.current_run_id is None
        assert deferred.consecutive_failures == 0
        owner = conn.execute(
            "SELECT owner_task_id, session_id FROM kanban_session_affinity "
            "WHERE project_id=? AND flow_id='flow-review' AND assignee='supervisor'",
            (project_id,),
        ).fetchone()
        assert owner is not None
        assert owner["owner_task_id"] == active_controller
        assert owner["session_id"] == "supervisor-session"
    finally:
        conn.close()


@pytest.mark.parametrize("failure_mode", ["spawn", "crash"])
def test_derived_review_failure_releases_only_lease_and_preserves_session(
    tmp_path, monkeypatch, failure_mode
):
    """Review worker failure must free ownership without losing flow identity."""
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban"))
    canonical = tmp_path / "supervisor"
    candidate = tmp_path / "candidate"
    canonical.mkdir()
    candidate.mkdir()
    project_id = _project(tmp_path)
    conn = kb.connect()
    try:
        root = kb.create_task(
            conn,
            title="root",
            assignee="supervisor",
            project_id=project_id,
            workspace_kind="dir",
            workspace_path=str(canonical),
            session_affinity={"flow_id": "flow-review"},
        )
        _opt_in_aether_review(conn, root, project_id)
        root_run = kb.claim_task(conn, root)
        assert root_run is not None
        root_lease = kb.reserve_session_affinity(conn, root_run)
        assert root_lease is not None
        assert kb.register_session_affinity(
            conn, root_run, root_lease, session_id="supervisor-session"
        )
        assert kb.complete_task(conn, root)

        unit = kb.create_task(
            conn,
            title="unit",
            assignee="implementer",
            project_id=project_id,
            parents=(root,),
            workspace_kind="dir",
            workspace_path=str(candidate),
        )
        impl = kb.claim_task(conn, unit)
        assert impl is not None
        assert kb.request_review(
            conn,
            unit,
            reviewer="supervisor",
            expected_run_id=impl.current_run_id,
        )
    finally:
        conn.close()

    from hermes_cli import profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda _profile: True)

    def spawn(task, workspace, *, affinity=None, session_workspace=None, board=None):
        assert affinity is not None
        assert affinity.session_id == "supervisor-session"
        assert workspace == str(candidate)
        assert session_workspace == str(canonical)
        if failure_mode == "spawn":
            raise RuntimeError("synthetic review spawn failure")
        return 77881

    conn = kb.connect()
    try:
        result = kb.dispatch_once(
            conn,
            spawn_fn=spawn,
            max_spawn=1,
            failure_limit=3,
        )
        if failure_mode == "spawn":
            assert result.spawned == []
        else:
            assert [row[0] for row in result.spawned] == [unit]
            monkeypatch.setattr(kb, "_resolve_crash_grace_seconds", lambda: 0)
            monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
            monkeypatch.setattr(
                kb, "_classify_worker_exit", lambda _pid: ("nonzero_exit", 1)
            )
            assert kb.detect_crashed_workers(conn) == [unit]

        task = kb.get_task(conn, unit)
        assert task is not None
        assert task.status == "review"
        assert task.current_run_id is None
        assert task.session_affinity is None
        row = conn.execute(
            "SELECT session_id, lease_token, owner_task_id, owner_run_id "
            "FROM kanban_session_affinity WHERE project_id=? AND flow_id=? "
            "AND assignee='supervisor'",
            (project_id, "flow-review"),
        ).fetchone()
        assert row is not None
        assert row["session_id"] == "supervisor-session"
        assert row["lease_token"] is None
        assert row["owner_task_id"] is None
        assert row["owner_run_id"] is None
    finally:
        conn.close()


def test_derived_review_affinity_registers_existing_supervisor_session(
    tmp_path, monkeypatch
):
    """A derived review lease may bind only to the flow's existing session."""
    kanban_home = tmp_path / "kanban"
    profile_home = tmp_path / "supervisor-profile"
    canonical = tmp_path / "supervisor-root"
    candidate = tmp_path / "implementer-candidate"
    canonical.mkdir()
    candidate.mkdir()
    profile_home.mkdir()
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(kanban_home))
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    project_id = _project(tmp_path)

    from hermes_state import SessionDB

    sessions = SessionDB(db_path=profile_home / "state.db")
    sessions.create_session(
        "supervisor-session",
        "kanban",
        profile_name="supervisor",
        cwd=str(canonical),
    )
    sessions.close()

    conn = kb.connect()
    try:
        root = kb.create_task(
            conn,
            title="decompose",
            assignee="supervisor",
            project_id=project_id,
            workspace_kind="dir",
            workspace_path=str(canonical),
            session_affinity={"flow_id": "flow-review", "terminal": False},
        )
        _opt_in_aether_review(conn, root, project_id)
        claimed_root = kb.claim_task(conn, root)
        assert claimed_root is not None
        root_lease = kb.reserve_session_affinity(
            conn, claimed_root, workspace_path=str(canonical)
        )
        assert root_lease is not None
        assert kb.register_session_affinity(
            conn, claimed_root, root_lease, session_id="supervisor-session"
        )
        assert kb.complete_task(conn, root)

        unit = kb.create_task(
            conn,
            title="implement",
            assignee="implementer",
            project_id=project_id,
            parents=(root,),
            workspace_kind="dir",
            workspace_path=str(candidate),
        )
        claimed_unit = kb.claim_task(conn, unit)
        assert claimed_unit is not None
        assert kb.request_review(
            conn,
            unit,
            summary="candidate ready",
            reviewer="supervisor",
            expected_run_id=claimed_unit.current_run_id,
        )
        review = kb.claim_review_task(conn, unit)
        assert review is not None
        reserved = kb._reserve_review_flow_session(conn, review)
        assert reserved is not None
        lease, session_workspace = reserved
        assert session_workspace == str(canonical)
        assert lease is not None and lease.session_id == "supervisor-session"
    finally:
        conn.close()

    from hermes_cli import profiles

    monkeypatch.setattr(profiles, "resolve_profile_env", lambda _profile: str(profile_home))
    monkeypatch.setenv("HERMES_KANBAN_TASK", unit)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(review.current_run_id))
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", str(review.claim_lock))
    monkeypatch.setenv("HERMES_KANBAN_AFFINITY_TOKEN", lease.token)
    monkeypatch.setenv("HERMES_KANBAN_AFFINITY_GENERATION", str(lease.generation))
    monkeypatch.setenv("HERMES_KANBAN_AFFINITY_FLOW_ID", lease.flow_id)
    monkeypatch.setenv("HERMES_KANBAN_AFFINITY_PROJECT_ID", lease.project_id)
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(candidate))
    monkeypatch.setenv("HERMES_KANBAN_SESSION_WORKSPACE", str(canonical))
    monkeypatch.setenv("HERMES_KANBAN_REVIEW_AFFINITY", "1")

    assert kb.register_worker_session_from_env("supervisor-session")
    conn = kb.connect()
    try:
        persisted = kb.get_task(conn, unit)
        assert persisted is not None and persisted.session_affinity is None
        row = conn.execute(
            "SELECT session_id, workspace_path, owner_task_id, owner_run_id "
            "FROM kanban_session_affinity WHERE project_id = ? AND flow_id = ?",
            (project_id, "flow-review"),
        ).fetchone()
        assert row is not None
        assert row["session_id"] == "supervisor-session"
        assert row["workspace_path"] == str(canonical)
        assert row["owner_task_id"] == unit
        assert row["owner_run_id"] == review.current_run_id
    finally:
        conn.close()


@pytest.mark.parametrize("drift", ["contract_version", "ancestor_profile"])
def test_derived_review_registration_revalidates_aether_context(
    tmp_path, monkeypatch, drift
):
    """Reservation-time Aether identity must still hold at worker registration."""
    kanban_home = tmp_path / "kanban"
    profile_home = tmp_path / "supervisor-profile"
    canonical = tmp_path / "supervisor-root"
    candidate = tmp_path / "implementer-candidate"
    canonical.mkdir()
    candidate.mkdir()
    profile_home.mkdir()
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(kanban_home))
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    project_id = _project(tmp_path)

    from hermes_state import SessionDB

    sessions = SessionDB(db_path=profile_home / "state.db")
    sessions.create_session(
        "supervisor-session",
        "kanban",
        profile_name="supervisor",
        cwd=str(canonical),
    )
    sessions.close()

    conn = kb.connect()
    try:
        root = kb.create_task(
            conn,
            title="decompose",
            assignee="supervisor",
            project_id=project_id,
            workspace_kind="dir",
            workspace_path=str(canonical),
            session_affinity={"flow_id": "flow-review"},
        )
        _opt_in_aether_review(conn, root, project_id)
        root_run = kb.claim_task(conn, root)
        assert root_run is not None
        root_lease = kb.reserve_session_affinity(conn, root_run)
        assert root_lease is not None
        assert kb.register_session_affinity(
            conn, root_run, root_lease, session_id="supervisor-session"
        )
        assert kb.complete_task(conn, root)

        unit = kb.create_task(
            conn,
            title="implement",
            assignee="implementer",
            project_id=project_id,
            parents=(root,),
            workspace_kind="dir",
            workspace_path=str(candidate),
        )
        impl = kb.claim_task(conn, unit)
        assert impl is not None
        assert kb.request_review(
            conn,
            unit,
            reviewer="supervisor",
            expected_run_id=impl.current_run_id,
        )
        review = kb.claim_review_task(conn, unit)
        assert review is not None
        reserved = kb._reserve_review_flow_session(conn, review)
        assert reserved is not None
        lease, session_workspace = reserved

        if drift == "contract_version":
            event = conn.execute(
                "SELECT id, payload FROM task_events "
                "WHERE task_id=? AND kind='collaboration_opted_in' "
                "ORDER BY id DESC LIMIT 1",
                (root,),
            ).fetchone()
            payload = json.loads(event["payload"])
            payload["contract_version"] = "2"
            conn.execute(
                "UPDATE task_events SET payload=? WHERE id=?",
                (json.dumps(payload), event["id"]),
            )
        else:
            conn.execute("UPDATE tasks SET assignee='morfeo' WHERE id=?", (root,))
        conn.commit()
    finally:
        conn.close()

    from hermes_cli import profiles

    monkeypatch.setattr(profiles, "resolve_profile_env", lambda _profile: str(profile_home))
    monkeypatch.setenv("HERMES_KANBAN_TASK", unit)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(review.current_run_id))
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", str(review.claim_lock))
    monkeypatch.setenv("HERMES_KANBAN_AFFINITY_TOKEN", lease.token)
    monkeypatch.setenv("HERMES_KANBAN_AFFINITY_GENERATION", str(lease.generation))
    monkeypatch.setenv("HERMES_KANBAN_AFFINITY_FLOW_ID", lease.flow_id)
    monkeypatch.setenv("HERMES_KANBAN_AFFINITY_PROJECT_ID", lease.project_id)
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(candidate))
    monkeypatch.setenv("HERMES_KANBAN_SESSION_WORKSPACE", session_workspace)
    monkeypatch.setenv("HERMES_KANBAN_REVIEW_AFFINITY", "1")

    assert not kb.register_worker_session_from_env("supervisor-session")


def test_derived_review_requires_existing_flow_binding(tmp_path, monkeypatch):
    """Aether review may reuse an existing Supervisor binding, never create one."""
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban"))
    project_id = _project(tmp_path)
    canonical = tmp_path / "supervisor-root"
    candidate = tmp_path / "candidate"
    canonical.mkdir()
    candidate.mkdir()
    conn = kb.connect()
    try:
        root = kb.create_task(
            conn,
            title="decompose",
            assignee="supervisor",
            project_id=project_id,
            workspace_kind="dir",
            workspace_path=str(canonical),
            session_affinity={"flow_id": "flow-review", "terminal": False},
        )
        _opt_in_aether_review(conn, root, project_id)
        unit = kb.create_task(
            conn,
            title="implement",
            assignee="implementer",
            project_id=project_id,
            parents=(root,),
            workspace_kind="dir",
            workspace_path=str(candidate),
        )
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (root,))
        conn.execute("UPDATE tasks SET status='review', assignee='supervisor' WHERE id=?", (unit,))
        conn.commit()
        review = kb.claim_review_task(conn, unit)
        assert review is not None
        with pytest.raises(
            kb.AffinityRegistrationError,
            match="resumable session|existing registered session",
        ):
            kb._reserve_review_flow_session(conn, review)
        assert conn.execute("SELECT 1 FROM kanban_session_affinity").fetchone() is None
    finally:
        conn.close()


def test_generic_review_without_flow_affinity_keeps_bundled_review_skill(
    tmp_path, monkeypatch
):
    """The generic Hermes review lane remains unchanged outside an affinity flow."""
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban"))
    project_id = _project(tmp_path)
    canonical = tmp_path / "supervisor-root"
    workspace = tmp_path / "candidate"
    canonical.mkdir()
    workspace.mkdir()
    conn = kb.connect()
    try:
        root = kb.create_task(
            conn,
            title="generic parent",
            assignee="supervisor",
            project_id=project_id,
            workspace_kind="dir",
            workspace_path=str(canonical),
            session_affinity={"flow_id": "flow-generic", "terminal": False},
        )
        root_claim = kb.claim_task(conn, root)
        assert root_claim is not None
        lease = kb.reserve_session_affinity(conn, root_claim, workspace_path=str(canonical))
        assert lease is not None
        assert kb.register_session_affinity(
            conn, root_claim, lease, session_id="generic-supervisor-session"
        )
        assert kb.complete_task(conn, root)
        task_id = kb.create_task(
            conn,
            title="plain implementation",
            assignee="implementer",
            project_id=project_id,
            parents=(root,),
            workspace_kind="dir",
            workspace_path=str(workspace),
        )
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None
        assert kb.request_review(
            conn,
            task_id,
            summary="ready",
            reviewer="supervisor",
            expected_run_id=claimed.current_run_id,
        )
    finally:
        conn.close()


def test_aether_review_without_supervisor_affinity_fails_closed(
    tmp_path, monkeypatch
):
    """An opted-in Aether review must not silently fall back to a fresh reviewer."""
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban"))
    project_id = _project(tmp_path)
    canonical = tmp_path / "supervisor-root"
    candidate = tmp_path / "candidate"
    canonical.mkdir()
    candidate.mkdir()

    conn = kb.connect()
    try:
        root = kb.create_task(
            conn,
            title="decompose",
            assignee="supervisor",
            project_id=project_id,
            workspace_kind="dir",
            workspace_path=str(canonical),
            session_id="origin-session",
        )
        _opt_in_aether_review(conn, root, project_id)
        assert kb.claim_task(conn, root) is not None
        assert kb.complete_task(conn, root)

        unit = kb.create_task(
            conn,
            title="implement",
            assignee="implementer",
            project_id=project_id,
            parents=(root,),
            workspace_kind="dir",
            workspace_path=str(candidate),
        )
        claimed = kb.claim_task(conn, unit)
        assert claimed is not None
        assert kb.request_review(
            conn,
            unit,
            summary="candidate ready",
            reviewer="supervisor",
            expected_run_id=claimed.current_run_id,
        )
    finally:
        conn.close()

    from hermes_cli import profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda _profile: True)
    spawned = []

    def fake_spawn(*args, **kwargs):
        spawned.append((args, kwargs))
        return 999

    conn = kb.connect()
    try:
        result = kb.dispatch_once(
            conn,
            spawn_fn=fake_spawn,
            max_spawn=1,
            failure_limit=1,
        )
        assert spawned == []
        assert unit in result.auto_blocked
        task = kb.get_task(conn, unit)
        assert task is not None and task.status == "blocked"
        assert "review flow has no same-profile affinity ancestor" in (
            task.last_failure_error or ""
        )
    finally:
        conn.close()

    from hermes_cli import profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda _profile: True)
    captured = {}

    def fake_spawn(task, workspace, **kwargs):
        captured.update(task=task, workspace=workspace, kwargs=kwargs)
        return 1001

    conn = kb.connect()
    try:
        result = kb.dispatch_once(conn, spawn_fn=fake_spawn, max_spawn=1)
        assert [row[0] for row in result.spawned] == [task_id]
        assert captured["workspace"] == str(workspace)
        assert "sdlc-review" in (captured["task"].skills or [])
        assert captured["kwargs"].get("affinity") is None
        assert captured["kwargs"].get("session_workspace") is None
    finally:
        conn.close()


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_contract_id",
        "mismatched_contract_id",
        "mismatched_contract_version",
        "missing_event_project",
        "mismatched_event_project",
        "mismatched_board_project",
        "missing_aether_project",
    ],
)
def test_nonmatching_aether_identity_uses_generic_review(
    tmp_path, monkeypatch, mutation
):
    """Partial/mismatched Aether identity must never borrow Supervisor affinity."""
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban"))
    project_id = _project(tmp_path)
    canonical = tmp_path / "supervisor-root"
    candidate = tmp_path / "candidate"
    canonical.mkdir()
    candidate.mkdir()
    conn = kb.connect()
    try:
        root = kb.create_task(
            conn,
            title="root",
            assignee="supervisor",
            project_id=project_id,
            workspace_kind="dir",
            workspace_path=str(canonical),
            session_affinity={"flow_id": "flow-review"},
        )
        _opt_in_aether_review(conn, root, project_id)
        root_run = kb.claim_task(conn, root)
        assert root_run is not None
        lease = kb.reserve_session_affinity(conn, root_run)
        assert lease is not None
        assert kb.register_session_affinity(
            conn, root_run, lease, session_id="supervisor-session"
        )
        assert kb.complete_task(conn, root)

        meta_path = kb.board_metadata_path()
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        event = conn.execute(
            "SELECT id, payload FROM task_events "
            "WHERE task_id=? AND kind='collaboration_opted_in' "
            "ORDER BY id DESC LIMIT 1",
            (root,),
        ).fetchone()
        assert event is not None
        payload = json.loads(event["payload"])
        if mutation == "missing_contract_id":
            meta.pop("aether_contract_id", None)
        elif mutation == "mismatched_contract_id":
            payload["contract_id"] = "oc_other"
        elif mutation == "mismatched_contract_version":
            payload["contract_version"] = "2"
        elif mutation == "missing_event_project":
            payload["project_id"] = None
        elif mutation == "mismatched_event_project":
            payload["project_id"] = "p_other"
        elif mutation == "mismatched_board_project":
            meta["project_id"] = "p_other"
        elif mutation == "missing_aether_project":
            meta.pop("aether_project_id", None)
        else:  # pragma: no cover - parametrization is closed above
            raise AssertionError(mutation)
        meta_path.write_text(json.dumps(meta), encoding="utf-8")
        conn.execute(
            "UPDATE task_events SET payload=? WHERE id=?",
            (json.dumps(payload), event["id"]),
        )
        conn.commit()

        unit = kb.create_task(
            conn,
            title="unit",
            assignee="implementer",
            project_id=project_id,
            parents=(root,),
            workspace_kind="dir",
            workspace_path=str(candidate),
            skills=("implementation-evidence",),
            model_override="implementer-model",
            provider_override="custom:implementer-provider",
            reasoning_effort="high",
        )
        impl = kb.claim_task(conn, unit)
        assert impl is not None
        assert kb.request_review(
            conn,
            unit,
            reviewer="supervisor",
            expected_run_id=impl.current_run_id,
        )
    finally:
        conn.close()

    from hermes_cli import profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda _profile: True)
    captured = {}

    def fake_spawn(task, workspace, **kwargs):
        captured.update(task=task, workspace=workspace, kwargs=kwargs)
        return 9010

    conn = kb.connect()
    try:
        result = kb.dispatch_once(conn, spawn_fn=fake_spawn, max_spawn=1)
        assert [row[0] for row in result.spawned] == [unit]
        assert captured["workspace"] == str(candidate)
        assert captured["kwargs"].get("affinity") is None
        assert captured["kwargs"].get("session_workspace") is None
        assert captured["task"].skills == ["implementation-evidence", "sdlc-review"]
        assert captured["task"].model_override == "implementer-model"
        assert captured["task"].provider_override == "custom:implementer-provider"
        assert captured["task"].reasoning_effort == "high"
    finally:
        conn.close()


def test_kanban_worker_session_records_current_workspace(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(workspace))
    from run_agent import _launch_cwd_for_session

    assert _launch_cwd_for_session("kanban") == str(workspace)


def test_affinity_review_session_row_keeps_canonical_workspace(tmp_path, monkeypatch):
    canonical = tmp_path / "supervisor"
    candidate = tmp_path / "candidate"
    canonical.mkdir()
    candidate.mkdir()
    monkeypatch.setenv("HERMES_KANBAN_AFFINITY_TOKEN", "lease")
    monkeypatch.setenv("HERMES_KANBAN_SESSION_WORKSPACE", str(canonical))
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(candidate))
    monkeypatch.setenv("TERMINAL_CWD", str(candidate))

    from run_agent import _launch_cwd_for_session

    assert _launch_cwd_for_session("kanban") == str(canonical)


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


def test_aether_review_without_supervisor_affinity_fails_closed(tmp_path, monkeypatch):
    """Verified Aether review must not silently fall back to a fresh reviewer."""
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban"))
    workspace = tmp_path / "candidate"
    workspace.mkdir()
    project_id = _project(tmp_path)
    conn = kb.connect()
    try:
        root = kb.create_task(
            conn,
            title="Aether root without Supervisor affinity",
            assignee="supervisor",
            project_id=project_id,
            workspace_kind="dir",
            workspace_path=str(tmp_path),
        )
        _opt_in_aether_review(conn, root, project_id)
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (root,))
        unit = kb.create_task(
            conn,
            title="unit",
            assignee="implementer",
            project_id=project_id,
            parents=(root,),
            workspace_kind="dir",
            workspace_path=str(workspace),
        )
        impl = kb.claim_task(conn, unit)
        assert impl is not None
        assert kb.request_review(
            conn,
            unit,
            reviewer="supervisor",
            expected_run_id=impl.current_run_id,
        )
    finally:
        conn.close()

    from hermes_cli import profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda _profile: True)
    spawned = []

    def should_not_spawn(task, workspace, **kwargs):
        spawned.append((task.id, workspace, kwargs))
        return 1234

    conn = kb.connect()
    try:
        kb.dispatch_once(
            conn,
            spawn_fn=should_not_spawn,
            max_spawn=1,
            failure_limit=3,
        )
        assert spawned == []
        reviewed = kb.get_task(conn, unit)
        assert reviewed is not None
        assert reviewed.status == "review"
        assert reviewed.current_run_id is None
        assert reviewed.consecutive_failures == 1
        assert "no same-profile flow affinity ancestor" in (
            reviewed.last_failure_error or ""
        )
    finally:
        conn.close()


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


def test_explicit_affinity_child_normalizes_shared_workspace_to_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban"))
    monkeypatch.setenv("HERMES_PROFILE", "worker")
    project_id = _project(tmp_path)
    conn = kb.connect()
    try:
        parent_id = _task(conn, project_id, workspace=tmp_path, session_id="origin-session")
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", parent_id)
    from tools import kanban_tools

    child = json.loads(
        kanban_tools._handle_create(
            {
                "title": "terminal shared workspace",
                "assignee": "worker",
                "parents": [parent_id],
                "workspace_kind": "worktree",
                "workspace_path": str(tmp_path),
                "project": project_id,
                "session_affinity": {"flow_id": "flow-7", "terminal": True},
            }
        )
    )
    assert "task_id" in child, child
    conn = kb.connect()
    try:
        task = kb.get_task(conn, child["task_id"])
        assert task is not None
        assert task.workspace_kind == "dir"
        assert task.workspace_path == str(tmp_path)
    finally:
        conn.close()


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


def _blocked_flow_with_terminal_controller(
    conn,
    project_id,
    workspace,
    *,
    controller_goal_mode=False,
):
    root = kb.create_task(
        conn,
        title="decompose",
        assignee="supervisor",
        project_id=project_id,
        session_id="origin-session",
        workspace_kind="dir",
        workspace_path=str(workspace),
        session_affinity={"flow_id": "flow-7"},
    )
    assert kb.claim_task(conn, root) is not None
    assert kb.complete_task(conn, root)
    unit = kb.create_task(
        conn,
        title="implement",
        assignee="implementer",
        project_id=project_id,
        parents=(root,),
        session_id="origin-session",
        workspace_kind="dir",
        workspace_path=str(workspace),
    )
    controller = kb.create_task(
        conn,
        title="integrate",
        assignee="supervisor",
        project_id=project_id,
        parents=(root, unit),
        session_id="origin-session",
        workspace_kind="dir",
        workspace_path=str(workspace),
        session_affinity={"flow_id": "flow-7", "terminal": True},
        goal_mode=controller_goal_mode,
    )
    return unit, controller


def test_blocked_unit_wakes_terminal_flow_controller(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban"))
    project_id = _project(tmp_path)
    conn = kb.connect()
    try:
        unit, controller = _blocked_flow_with_terminal_controller(
            conn, project_id, tmp_path
        )
        assert kb.claim_task(conn, unit) is not None
        assert kb.block_task(conn, unit, reason="local guard regression", kind="capability")
        blocked = kb.get_task(conn, unit)
        ready_controller = kb.get_task(conn, controller)
        assert blocked is not None and blocked.status == "blocked"
        assert ready_controller is not None and ready_controller.status == "ready"
        attention = [
            event for event in kb.list_events(conn, controller)
            if event.kind == "flow_attention"
        ]
        assert len(attention) == 1
        assert attention[0].payload is not None
        assert attention[0].payload["blocked_task_id"] == unit
        context = kb.build_worker_context(conn, controller)
        assert "## Flow recovery attention" in context
        assert f"Blocked task: `{unit}`" in context
        assert "origin_signal=\"recovery\"" in context
        assert kb.claim_task(conn, controller) is not None
    finally:
        conn.close()


def test_goal_mode_controller_accepts_exact_recovery_signal(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban"))
    project_id = _project(tmp_path)
    conn = kb.connect()
    try:
        unit, controller = _blocked_flow_with_terminal_controller(
            conn,
            project_id,
            tmp_path,
            controller_goal_mode=True,
        )
        kb.add_notify_sub(
            conn,
            task_id=controller,
            platform="tui",
            chat_id="origin-session",
        )
        assert kb.claim_task(conn, unit) is not None
        assert kb.block_task(
            conn,
            unit,
            reason="local guard regression",
            kind="capability",
        )
        controller_run = kb.claim_task(conn, controller)
        assert controller_run is not None
    finally:
        conn.close()

    monkeypatch.setenv("HERMES_PROFILE", "supervisor")
    monkeypatch.setenv("HERMES_KANBAN_TASK", controller)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(controller_run.current_run_id))
    from tools import kanban_tools

    output = json.loads(
        kanban_tools._handle_block({
            "reason": "runtime recovery failed",
            "kind": "capability",
            "origin_signal": "recovery",
        })
    )
    assert output["ok"] is True
    assert output["status"] == "blocked"

    conn = kb.connect()
    try:
        controller_after = kb.get_task(conn, controller)
        blocked_unit = kb.get_task(conn, unit)
        assert controller_after is not None and controller_after.status == "blocked"
        assert blocked_unit is not None and blocked_unit.status == "blocked"
        assert len(kb._pending_flow_attentions(conn, controller)) == 1
        _, events = kb.unseen_events_for_sub(
            conn,
            task_id=controller,
            platform="tui",
            chat_id="origin-session",
            kinds=("origin_signal", "flow_terminal"),
        )
        assert [event.kind for event in events] == ["origin_signal"]
        assert events[0].payload is not None
        assert events[0].payload["origin_signal"] == "recovery"
        context = kb.build_worker_context(conn, controller)
        assert "## Flow recovery attention" in context
    finally:
        conn.close()


def test_controller_dependency_block_resolves_attention_and_retries_unit(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban"))
    project_id = _project(tmp_path)
    conn = kb.connect()
    try:
        unit, controller = _blocked_flow_with_terminal_controller(
            conn, project_id, tmp_path
        )
        assert kb.claim_task(conn, unit) is not None
        assert kb.block_task(conn, unit, reason="local guard regression", kind="capability")
        assert kb.claim_task(conn, controller) is not None
        assert kb.block_task(
            conn,
            controller,
            reason="runtime recovered; resume implementation",
            kind="dependency",
        )
        resumed = kb.get_task(conn, unit)
        waiting_controller = kb.get_task(conn, controller)
        assert resumed is not None and resumed.status == "ready"
        assert waiting_controller is not None and waiting_controller.status == "todo"
        assert any(
            event.kind == "flow_attention_resolved"
            and event.payload is not None
            and event.payload["blocked_task_id"] == unit
            for event in kb.list_events(conn, controller)
        )
    finally:
        conn.close()


def test_controller_can_escalate_recovery_to_origin(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban"))
    project_id = _project(tmp_path)
    conn = kb.connect()
    try:
        unit, controller = _blocked_flow_with_terminal_controller(
            conn, project_id, tmp_path
        )
        kb.add_notify_sub(
            conn, task_id=controller, platform="tui", chat_id="origin-session"
        )
        assert kb.claim_task(conn, unit) is not None
        assert kb.block_task(conn, unit, reason="local guard regression", kind="capability")
        assert kb.claim_task(conn, controller) is not None
        assert kb.block_task(
            conn,
            controller,
            reason="runtime recovery failed",
            kind="capability",
            origin_signal="recovery",
        )
        _, events = kb.unseen_events_for_sub(
            conn,
            task_id=controller,
            platform="tui",
            chat_id="origin-session",
            kinds=("origin_signal", "flow_terminal"),
        )
        assert [event.kind for event in events] == ["origin_signal"]
        assert events[0].payload is not None
        assert events[0].payload["origin_signal"] == "recovery"
        blocked = kb.get_task(conn, unit)
        assert blocked is not None and blocked.status == "blocked"
    finally:
        conn.close()


def test_controller_requeues_when_a_second_attention_is_pending(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban"))
    project_id = _project(tmp_path)
    conn = kb.connect()
    try:
        first, controller = _blocked_flow_with_terminal_controller(conn, project_id, tmp_path)
        second = kb.create_task(conn, title="second implementer", assignee="implementer", project_id=project_id, workspace_kind="dir", workspace_path=str(tmp_path))
        kb.link_tasks(conn, parent_id=second, child_id=controller)
        assert kb.claim_task(conn, first) is not None
        assert kb.claim_task(conn, second) is not None
        assert kb.block_task(conn, first, reason="first", kind="capability")
        assert kb.block_task(conn, second, reason="second", kind="capability")
        assert kb.claim_task(conn, controller) is not None
        assert kb.block_task(conn, controller, reason="first fixed", kind="dependency")
        requeued = kb.get_task(conn, controller)
        assert requeued is not None and requeued.status == "ready"
        assert len(kb._pending_flow_attentions(conn, controller)) == 1
        assert any(event.kind == "flow_attention_requeued" for event in kb.list_events(conn, controller))
    finally:
        conn.close()
