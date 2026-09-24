"""Tests for Project provenance recovery, early refusal, and bounded review failure (#494).

Covers:
- AC1: Reproduction of null-Project scratch child on unchanged revision in disposable state.
- AC2: Supported route persists canonical Project on a fresh child worktree and enables
       same-card review claim; actionable early refusal before task/run/event insertion.
- AC3: Refusal matrix (cross-Project mismatch, missing/ambiguous board binding, traversal/symlink,
       absent parent edge, contradictory registry) while preserving non-Aether scratch creation,
       same-profile linked projects, and affinity recovery.
- AC4: Bounded failure and containment for malformed child without claim/reclaim loop or rollback.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

_WORKTREE = Path(__file__).resolve().parents[2]
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))

from hermes_cli import kanban_db as kb
from hermes_cli import projects_db as pdb
import tools.kanban_tools as kt


@pytest.fixture
def disposable_env(tmp_path, monkeypatch):
    """Set up completely isolated disposable home, board, and repo roots."""
    base = tmp_path / "disposable_root"
    base.mkdir(parents=True)

    repo_dir = base / "repo"
    repo_dir.mkdir()
    worktrees_dir = repo_dir / ".worktrees"
    worktrees_dir.mkdir()

    import subprocess

    subprocess.run(["git", "init", "-q", str(repo_dir)], check=True)
    subprocess.run(
        ["git", "-C", str(repo_dir), "config", "user.name", "Test"], check=True
    )
    subprocess.run(
        ["git", "-C", str(repo_dir), "config", "user.email", "test@test.local"],
        check=True,
    )
    (repo_dir / "README").write_text("test")
    subprocess.run(["git", "-C", str(repo_dir), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repo_dir), "commit", "-q", "-m", "init"], check=True
    )

    # Board directory and metadata
    board_slug = "oc-synth-test-v1"
    boards_root = base / "kanban" / "boards"
    board_dir = boards_root / board_slug
    board_dir.mkdir(parents=True)
    db_path = board_dir / "kanban.db"

    # Profile 1 (supervisor) has canonical project in its registry
    home_supervisor = base / "profiles" / "supervisor"
    home_supervisor.mkdir(parents=True)
    with pdb.connect_closing(db_path=home_supervisor / "projects.db") as pconn:
        canonical_pid = pdb.create_project(
            pconn,
            name="Synth Canonical Project",
            primary_path=str(repo_dir),
        )

    # Profile 2 (implementer) has clean registry without the project
    home_implementer = base / "profiles" / "implementer"
    home_implementer.mkdir(parents=True)
    with pdb.connect_closing(db_path=home_implementer / "projects.db") as pconn:
        pass  # Empty

    board_meta = {
        "slug": board_slug,
        "name": "Objective Synth Test",
        "project_id": canonical_pid,
        "default_workdir": str(repo_dir),
        "aether_project_id": "synth-aether-proj-uuid",
        "aether_contract_id": "oc_synth_contract",
        "aether_contract_version": 1,
    }
    (board_dir / "board.json").write_text(json.dumps(board_meta), encoding="utf-8")

    # Set environment to supervisor initially
    monkeypatch.setenv("HERMES_HOME", str(home_supervisor))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(base))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", board_slug)
    for var in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_SESSION_ID"):
        monkeypatch.delenv(var, raising=False)

    return {
        "base": base,
        "repo_dir": repo_dir,
        "worktrees_dir": worktrees_dir,
        "board_slug": board_slug,
        "board_dir": board_dir,
        "db_path": db_path,
        "canonical_pid": canonical_pid,
        "home_supervisor": home_supervisor,
        "home_implementer": home_implementer,
    }


def test_ac1_red_reproduction_unfixed_route_persists_null_project(
    disposable_env, monkeypatch
):
    """AC1 RED: on unchanged code, a Project-bound dir parent without affinity

    plus an explicit same-Project child with direct parent edge and omitted
    workspace persists a null-Project scratch child.
    """
    conn = kb.connect(db_path=disposable_env["db_path"])
    try:
        parent_wt = disposable_env["worktrees_dir"] / "t_parent101"
        parent_wt.mkdir()

        parent_id = kb.create_task(
            conn,
            title="Supervisor unit breakdown",
            assignee="supervisor",
            project_id=disposable_env["canonical_pid"],
            workspace_kind="dir",
            workspace_path=str(parent_wt),
            session_affinity=None,
            board=disposable_env["board_slug"],
        )
        parent = kb.get_task(conn, parent_id)
        assert parent is not None
        assert parent.project_id == disposable_env["canonical_pid"]
        assert parent.workspace_kind == "dir"
        assert parent.session_affinity is None

        # Switch worker context to implementer (different profile, project not in registry)
        monkeypatch.setenv("HERMES_HOME", str(disposable_env["home_implementer"]))
        monkeypatch.setenv("HERMES_KANBAN_TASK", parent_id)

        res_raw = kt._handle_create({
            "title": "Child implementation unit",
            "assignee": "implementer",
            "project": disposable_env["canonical_pid"],
            "parents": [parent_id],
            "board": disposable_env["board_slug"],
        })
        res = json.loads(res_raw)
        assert res.get("ok") is True
        child_id = res["task_id"]

        child = kb.get_task(conn, child_id)
        assert child is not None
        # On fixed code, this persists the canonical project on a fresh child worktree
        assert child.project_id == disposable_env["canonical_pid"], (
            f"Expected canonical project {disposable_env['canonical_pid']!r}, got {child.project_id!r}"
        )
        assert child.workspace_kind == "worktree"
        assert child.workspace_path == str(disposable_env["worktrees_dir"] / child_id)
        assert child.workspace_path != str(parent_wt)
    finally:
        conn.close()


def test_ac2_supported_route_and_same_card_review(disposable_env, monkeypatch):
    """AC2: Fixed supported route persists canonical Project on a fresh worktree,

    and a supported same-card review can claim with review source_status and no bypass.
    """
    conn = kb.connect(db_path=disposable_env["db_path"])
    try:
        parent_wt = disposable_env["worktrees_dir"] / "t_parent202"
        parent_wt.mkdir()

        # Root supervisor task opted into Aether collaboration
        root_id = kb.create_task(
            conn,
            title="Supervisor root",
            assignee="supervisor",
            project_id=disposable_env["canonical_pid"],
            workspace_kind="dir",
            workspace_path=str(parent_wt),
            session_id="origin-session-synth",
            session_affinity={"flow_id": "flow-supervisor-synth", "terminal": False},
            board=disposable_env["board_slug"],
        )
        assert kb.opt_in_collaboration(
            conn,
            root_id,
            contract_id="oc_synth_contract",
            contract_version="1",
            session_id="origin-session-synth",
            board=disposable_env["board_slug"],
        )
        claimed_root = kb.claim_task(conn, root_id)
        assert claimed_root is not None
        lease = kb.reserve_session_affinity(
            conn,
            claimed_root,
            workspace_path=str(parent_wt),
            board=disposable_env["board_slug"],
        )
        assert lease is not None
        assert kb.register_session_affinity(
            conn, claimed_root, lease, session_id="supervisor-session-1"
        )
        kb.complete_task(conn, root_id)

        # Implementer creates child
        monkeypatch.setenv("HERMES_HOME", str(disposable_env["home_implementer"]))
        monkeypatch.setenv("HERMES_KANBAN_TASK", root_id)

        res_raw = kt._handle_create({
            "title": "Unit implementation",
            "assignee": "implementer",
            "project": disposable_env["canonical_pid"],
            "parents": [root_id],
            "board": disposable_env["board_slug"],
        })
        res = json.loads(res_raw)
        assert res.get("ok") is True
        child_id = res["task_id"]

        child = kb.get_task(conn, child_id)
        assert child is not None
        assert child.project_id == disposable_env["canonical_pid"]
        assert child.workspace_kind == "worktree"
        assert child.workspace_path == str(disposable_env["worktrees_dir"] / child_id)

        # Claim child, finish unit, request same-card review
        kb.recompute_ready(conn)
        claimed_child = kb.claim_task(conn, child_id)
        assert claimed_child is not None
        assert kb.request_review(
            conn,
            child_id,
            summary="Implementation complete and verified",
            reviewer="supervisor",
            expected_run_id=claimed_child.current_run_id,
        )

        review_task = kb.get_task(conn, child_id)
        assert review_task is not None
        assert review_task.status == "review"
        assert review_task.assignee == "supervisor"

        # Dispatch review: supervisor claims review run without failure
        from hermes_cli import profiles

        monkeypatch.setattr(profiles, "profile_exists", lambda _p: True)

        spawned = []

        def fake_spawn(task, *args, **kwargs):
            spawned.append((task, args, kwargs))
            return 8888

        # Run dispatch under supervisor profile
        monkeypatch.setenv("HERMES_HOME", str(disposable_env["home_supervisor"]))
        dispatch_res = kb.dispatch_once(
            conn,
            spawn_fn=fake_spawn,
            max_spawn=1,
            failure_limit=1,
            board=disposable_env["board_slug"],
        )
        assert child_id not in dispatch_res.auto_blocked
        assert len(spawned) == 1
        spawned_task, spawn_args, spawn_kwargs = spawned[0]
        assert spawned_task.id == child_id
        assert spawned_task.assignee == "supervisor"
        assert spawned_task.status == "running"
    finally:
        conn.close()


def test_ac2_early_refusal_when_safe_recovery_fails_on_aether_board(
    disposable_env, monkeypatch
):
    """AC2: When a Project was explicitly supplied in a recognized Aether board

    and safe recovery fails, refuse actionably BEFORE any task, run, or event row
    is inserted. Never report success with a persisted null Project.
    """
    conn = kb.connect(db_path=disposable_env["db_path"])
    try:
        # Initial row counts
        initial_tasks = conn.execute("SELECT count(*) FROM tasks").fetchone()[0]
        initial_events = conn.execute("SELECT count(*) FROM task_events").fetchone()[0]
        initial_runs = conn.execute("SELECT count(*) FROM task_runs").fetchone()[0]

        # Switch to implementer profile where project is not registered
        monkeypatch.setenv("HERMES_HOME", str(disposable_env["home_implementer"]))
        # No parent task, no source task, but explicit project on Aether board
        res_raw = kt._handle_create({
            "title": "Orphan child with project",
            "assignee": "implementer",
            "project": disposable_env["canonical_pid"],
            "board": disposable_env["board_slug"],
        })
        res = json.loads(res_raw)
        # Must refuse with error, not ok: true
        assert "error" in res
        assert res.get("ok", False) is False
        assert "Cannot safely resolve project" in res.get(
            "error", ""
        ) or "safe recovery failed" in res.get("error", "")

        # Direct call to kb.create_task must also raise ValueError
        with pytest.raises(ValueError, match="Cannot safely resolve project"):
            kb.create_task(
                conn,
                title="Direct orphan task",
                assignee="implementer",
                project_id=disposable_env["canonical_pid"],
                board=disposable_env["board_slug"],
            )

        # Assert zero rows were inserted
        after_tasks = conn.execute("SELECT count(*) FROM tasks").fetchone()[0]
        after_events = conn.execute("SELECT count(*) FROM task_events").fetchone()[0]
        after_runs = conn.execute("SELECT count(*) FROM task_runs").fetchone()[0]
        assert after_tasks == initial_tasks
        assert after_events == initial_events
        assert after_runs == initial_runs
    finally:
        conn.close()


def test_ac3_mismatch_matrix(disposable_env, monkeypatch):
    """AC3: Complete mismatch matrix refuses without fallback, while generic

    scratch, same-profile linked projects, and affinity recovery keep passing.
    """
    conn = kb.connect(db_path=disposable_env["db_path"])
    try:
        parent_wt = disposable_env["worktrees_dir"] / "t_parent303"
        parent_wt.mkdir()

        parent_id = kb.create_task(
            conn,
            title="Parent task",
            assignee="supervisor",
            project_id=disposable_env["canonical_pid"],
            workspace_kind="dir",
            workspace_path=str(parent_wt),
            board=disposable_env["board_slug"],
        )

        monkeypatch.setenv("HERMES_HOME", str(disposable_env["home_implementer"]))
        monkeypatch.setenv("HERMES_KANBAN_TASK", parent_id)

        # 1. Cross-Project mismatch with parent
        with pytest.raises(ValueError, match="does not match parent"):
            kb.create_task(
                conn,
                title="Mismatch project",
                assignee="implementer",
                project_id="p_other_foreign",
                parents=[parent_id],
                board=disposable_env["board_slug"],
            )

        # 2. Cross-Project mismatch with board
        with pytest.raises(ValueError, match="Cannot safely resolve project"):
            kb.create_task(
                conn,
                title="Mismatch board project",
                assignee="implementer",
                project_id="p_mismatch_board",
                board=disposable_env["board_slug"],
            )

        # 3. Traversal in parent workspace path
        monkeypatch.setenv("HERMES_HOME", str(disposable_env["home_supervisor"]))
        traversal_path = disposable_env["worktrees_dir"] / ".." / "traversal"
        traversal_path.mkdir(exist_ok=True)
        bad_parent_1 = kb.create_task(
            conn,
            title="Bad traversal parent",
            assignee="supervisor",
            project_id=disposable_env["canonical_pid"],
            workspace_kind="dir",
            workspace_path=str(traversal_path),
            board=disposable_env["board_slug"],
        )
        monkeypatch.setenv("HERMES_HOME", str(disposable_env["home_implementer"]))
        with pytest.raises(ValueError, match="Cannot safely resolve project"):
            kb.create_task(
                conn,
                title="Child of traversal parent",
                assignee="implementer",
                project_id=disposable_env["canonical_pid"],
                parents=[bad_parent_1],
                board=disposable_env["board_slug"],
            )

        # 4. Absent parent edge: project_source_task_id points to parent, but parents is empty
        with pytest.raises(ValueError, match="Cannot safely resolve project"):
            kb.create_task(
                conn,
                title="Absent parent edge child",
                assignee="implementer",
                project_id=disposable_env["canonical_pid"],
                project_source_task_id=parent_id,
                parents=[],
                board=disposable_env["board_slug"],
            )

        # 5. Contradictory registry values
        foreign_repo = disposable_env["base"] / "foreign_repo"
        foreign_repo.mkdir()
        home_contradictory = disposable_env["base"] / "profiles" / "contradictory"
        home_contradictory.mkdir(parents=True)
        with pdb.connect_closing(db_path=home_contradictory / "projects.db") as pconn:
            with pdb.write_txn(pconn):
                pconn.execute(
                    "INSERT INTO projects (id, slug, name, primary_path, created_at, archived) "
                    "VALUES (?, ?, ?, ?, ?, 0)",
                    (
                        disposable_env["canonical_pid"],
                        "contradictory",
                        "Contradictory Project",
                        str(foreign_repo),
                        100,
                    ),
                )
        monkeypatch.setenv("HERMES_HOME", str(home_contradictory))
        with pytest.raises(ValueError, match="Cannot safely resolve project"):
            kb.create_task(
                conn,
                title="Contradictory registry child",
                assignee="implementer",
                project_id=disposable_env["canonical_pid"],
                parents=[parent_id],
                board=disposable_env["board_slug"],
            )
        monkeypatch.setenv("HERMES_HOME", str(disposable_env["home_implementer"]))

        # 6. Ordinary non-Aether scratch creation without project continues to pass
        generic_board_dir = (
            disposable_env["base"] / "kanban" / "boards" / "generic-board"
        )
        generic_board_dir.mkdir(parents=True)
        generic_db = generic_board_dir / "kanban.db"
        (generic_board_dir / "board.json").write_text(
            json.dumps({"slug": "generic-board", "name": "Generic"}), encoding="utf-8"
        )
        gconn = kb.connect(db_path=generic_db)
        try:
            gtid = kb.create_task(gconn, title="Plain scratch", board="generic-board")
            gtask = kb.get_task(gconn, gtid)
            assert gtask is not None
            assert gtask.project_id is None
            assert gtask.workspace_kind == "scratch"

            # 6b. Non-Aether board cross-project parent mismatch does not raise early (C1)
            # Both projects must be resolvable in the active registry so parent's
            # project_id is preserved on the generic board, making the mismatch constructible.
            parent_repo = disposable_env["base"] / "generic_parent_repo"
            parent_repo.mkdir(exist_ok=True)
            other_repo = disposable_env["base"] / "other_repo"
            other_repo.mkdir(exist_ok=True)
            with pdb.connect_closing(
                db_path=disposable_env["home_implementer"] / "projects.db"
            ) as pconn:
                generic_parent_pid = pdb.create_project(
                    pconn,
                    name="Generic Parent Proj",
                    primary_path=str(parent_repo),
                )
                other_pid = pdb.create_project(
                    pconn,
                    name="Other Proj",
                    primary_path=str(other_repo),
                )
            gt_parent = kb.create_task(
                gconn,
                title="Generic parent",
                project_id=generic_parent_pid,
                board="generic-board",
            )
            gt_parent_task = kb.get_task(gconn, gt_parent)
            assert gt_parent_task is not None
            assert gt_parent_task.project_id == generic_parent_pid
            gt_child = kb.create_task(
                gconn,
                title="Generic cross-project child",
                project_id=other_pid,
                parents=[gt_parent],
                board="generic-board",
            )
            gt_child_task = kb.get_task(gconn, gt_child)
            assert gt_child_task is not None
            assert gt_child_task.project_id == other_pid

            # 6c. Iterable (generator) parents with project_id links properly (C2)
            gt_gen_child = kb.create_task(
                gconn,
                title="Generic gen child",
                project_id=disposable_env["canonical_pid"],
                parents=(p for p in [gt_parent]),
                board="generic-board",
            )
            assert kb.parent_ids(gconn, gt_gen_child) == [gt_parent]
        finally:
            gconn.close()

        # 7. Same-profile linked project continues to pass
        monkeypatch.setenv("HERMES_HOME", str(disposable_env["home_supervisor"]))
        sp_tid = kb.create_task(
            conn,
            title="Supervisor local project task",
            assignee="supervisor",
            project_id=disposable_env["canonical_pid"],
            board=disposable_env["board_slug"],
        )
        sp_task = kb.get_task(conn, sp_tid)
        assert sp_task is not None
        assert sp_task.project_id == disposable_env["canonical_pid"]
        assert sp_task.workspace_kind == "worktree"
        assert sp_task.workspace_path == str(disposable_env["worktrees_dir"] / sp_tid)

        # 8. Existing affinity recovery continues to pass
        aff_parent_wt = disposable_env["worktrees_dir"] / "t_aff_parent"
        aff_parent_wt.mkdir()
        aff_parent_id = kb.create_task(
            conn,
            title="Affinity parent",
            assignee="supervisor",
            project_id=disposable_env["canonical_pid"],
            workspace_kind="dir",
            workspace_path=str(aff_parent_wt),
            session_affinity={"flow_id": "flow-aff-1", "terminal": False},
            board=disposable_env["board_slug"],
        )
        monkeypatch.setenv("HERMES_HOME", str(disposable_env["home_implementer"]))
        aff_child_id = kb.create_task(
            conn,
            title="Affinity child",
            assignee="implementer",
            project_id=disposable_env["canonical_pid"],
            project_source_task_id=aff_parent_id,
            parents=[aff_parent_id],
            workspace_kind="scratch",
            board=disposable_env["board_slug"],
        )
        aff_child = kb.get_task(conn, aff_child_id)
        assert aff_child is not None
        assert aff_child.project_id == disposable_env["canonical_pid"]
        assert aff_child.workspace_kind == "worktree"
    finally:
        conn.close()


def test_ac4_bounded_failure_containment(disposable_env, monkeypatch):
    """AC4: Pre-existing malformed child (project_id=None with affinity ancestor)

    reaches configured bounded breaker and durable blocked receipt without claim/reclaim
    loop, without rollback of kanban_block, without copying subscriptions, and without
    emitting origin_signal to unverified identity.
    """
    conn = kb.connect(db_path=disposable_env["db_path"])
    try:
        root_wt = disposable_env["worktrees_dir"] / "t_root_ws"
        root_wt.mkdir()

        # 1. Root task with session affinity and project_id
        root_id = kb.create_task(
            conn,
            title="Supervisor root",
            assignee="supervisor",
            project_id=disposable_env["canonical_pid"],
            workspace_kind="dir",
            workspace_path=str(root_wt),
            session_affinity={"flow_id": "flow-test-4", "terminal": False},
            board=disposable_env["board_slug"],
        )

        # 2. Malformed child without project_id, linked to root
        child_id = kb.create_task(
            conn,
            title="Malformed child unit",
            assignee="implementer",
            project_id=None,
            parents=[root_id],
            workspace_kind="scratch",
            board=disposable_env["board_slug"],
        )
        # Simulate pre-existing malformed child row with project_id = None
        conn.execute("UPDATE tasks SET project_id = NULL WHERE id = ?", (child_id,))
        conn.commit()

        kb.claim_task(conn, root_id)
        kb.complete_task(conn, root_id)
        kb.recompute_ready(conn)

        claimed = kb.claim_task(conn, child_id)
        assert claimed is not None
        assert claimed.status == "running"

        # Deterministic spawn failure reaches breaker (failure_limit=1)
        blocked = kb._record_task_failure(
            conn,
            child_id,
            "deterministic worker spawn failure",
            outcome="spawn_failed",
            failure_limit=1,
            release_claim=True,
            end_run=True,
        )
        assert blocked is True

        after_task = kb.get_task(conn, child_id)
        assert after_task is not None
        assert after_task.status == "blocked"
        assert after_task.consecutive_failures == 1
        assert after_task.claim_lock is None
        assert after_task.worker_pid is None

        # Check events: gave_up must be committed, no origin_signal
        events = [
            row["kind"]
            for row in conn.execute(
                "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id",
                (child_id,),
            ).fetchall()
        ]
        assert "gave_up" in events
        assert "origin_signal" not in events

        # Subscriptions: no subscription copied to unverified identity
        subs_count = conn.execute(
            "SELECT count(*) FROM kanban_notify_subs WHERE task_id = ?", (child_id,)
        ).fetchone()[0]
        assert subs_count == 0

        # Now test kanban_block / block_task on a running child with same malformed structure
        child_2_id = kb.create_task(
            conn,
            title="Second malformed child",
            assignee="implementer",
            project_id=None,
            parents=[root_id],
            workspace_kind="scratch",
            board=disposable_env["board_slug"],
        )
        # Simulate pre-existing malformed child row with project_id = None
        conn.execute("UPDATE tasks SET project_id = NULL WHERE id = ?", (child_2_id,))
        conn.commit()
        kb.recompute_ready(conn)
        claimed_2 = kb.claim_task(conn, child_2_id)
        assert claimed_2 is not None

        # block_task must succeed without raising ValueError or rolling back
        assert (
            kb.block_task(
                conn,
                child_2_id,
                reason="worker capability wall",
                kind="capability",
                expected_run_id=claimed_2.current_run_id,
            )
            is True
        )

        after_block = kb.get_task(conn, child_2_id)
        assert after_block is not None
        assert after_block.status == "blocked"
        assert after_block.claim_lock is None
        assert after_block.worker_pid is None

        # 3. Real review-lane spawn failure through dispatch_once (AC4 review lane)
        from hermes_cli import profiles as profiles_mod

        monkeypatch.setattr(profiles_mod, "profile_exists", lambda _p: True)

        review_child_id = kb.create_task(
            conn,
            title="Review child unit",
            assignee="implementer",
            project_id=disposable_env["canonical_pid"],
            parents=[root_id],
            workspace_kind="worktree",
            board=disposable_env["board_slug"],
        )
        conn.execute(
            "UPDATE tasks SET project_id = NULL WHERE id = ?", (review_child_id,)
        )
        conn.commit()

        kb.recompute_ready(conn)
        claimed_review = kb.claim_task(conn, review_child_id)
        assert claimed_review is not None
        assert claimed_review.status == "running"

        req_ok = kb.request_review(
            conn,
            review_child_id,
            summary="Review request probe",
            reviewer="supervisor",
            expected_run_id=claimed_review.current_run_id,
        )
        assert req_ok is True
        assert kb.get_task(conn, review_child_id).status == "review"

        def failing_spawn(*_a, **_k):
            raise RuntimeError("deterministic review spawn failure")

        # Tick 1: dispatch_once encounters deterministic review spawn failure
        res = kb.dispatch_once(
            conn,
            spawn_fn=failing_spawn,
            max_spawn=1,
            failure_limit=1,
            board=disposable_env["board_slug"],
            reconcile_orphans=False,
        )
        assert review_child_id in res.auto_blocked

        after_review_task = kb.get_task(conn, review_child_id)
        assert after_review_task is not None
        assert after_review_task.status == "blocked"
        assert after_review_task.consecutive_failures == 1
        assert after_review_task.claim_lock is None
        assert after_review_task.worker_pid is None

        # Check events: gave_up present, no origin_signal
        review_events = [
            row["kind"]
            for row in conn.execute(
                "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id",
                (review_child_id,),
            ).fetchall()
        ]
        assert "gave_up" in review_events
        assert "origin_signal" not in review_events

        # No inherited or copied notify subs
        review_subs_count = conn.execute(
            "SELECT count(*) FROM kanban_notify_subs WHERE task_id = ?",
            (review_child_id,),
        ).fetchone()[0]
        assert review_subs_count == 0

        # Ticks 2-3: verify stable blocked state without claim/reclaim loop
        for _ in range(2):
            res_subsequent = kb.dispatch_once(
                conn,
                spawn_fn=failing_spawn,
                max_spawn=1,
                failure_limit=1,
                board=disposable_env["board_slug"],
                reconcile_orphans=False,
            )
            assert review_child_id not in [x[0] for x in res_subsequent.spawned]
            assert review_child_id not in res_subsequent.auto_blocked
            t_subsequent = kb.get_task(conn, review_child_id)
            assert t_subsequent.status == "blocked"
            assert t_subsequent.consecutive_failures == 1
    finally:
        conn.close()
