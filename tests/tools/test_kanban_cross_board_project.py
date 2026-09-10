"""HLP-226c — board-bound Project recovery for a shared cross-board ``dir``.

A project/affinity worker may deliberately share a canonical worktree that was
created for an **earlier** board
(``<repo>/.worktrees/<prior-board-leaf>``).  The leaf therefore names no task
in the current board, and the creating profile may not register the Project at
all.  Before HLP-226c the shared-``dir`` source fallback required the leaf to
resolve to a root task in this board, discarded the already-persisted Project
and the same-flow terminal failed closed with
``session-affinity tasks require a canonical project_id``.

The corrected behavior recovers Project/repository identity from the current
board's own Project binding (``project_id`` + ``default_workdir``) when, and
only when, the source row carries that exact Project and affinity and the
shared path resolves to exactly one opaque leaf under the board repository's
``.worktrees`` directory.  The leaf itself stays opaque: it is never looked up
for the prior-board case and never trusted for authority.

Coverage:

* the recurrence RED/GREEN regression for the same-flow terminal;
* the two-step current-board terminal -> cross-profile Implementer E2E with a
  real worktree materialized through the native dispatcher resolver;
* the complete H226C-FR-004 fail-closed matrix (board Project, board
  repository, path shape, source Project, flow/assignee, explicit conflict);
* unchanged HLP-226 / HLP-226b / scratch / non-affinity behavior.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import projects_db as pdb
from tools import kanban_tools as kt

BOARD = "hlp226c-board"
PROJECT_ID = "p_226c0ffee"
OTHER_PROJECT_ID = "p_otherboard"
FLOW_ID = "hlp226c-flow-1"
OTHER_FLOW_ID = "hlp226c-flow-2"
PRIOR_BOARD_LEAF = "t_priorboard01"
SESSION_ID = "sess-origin-226c"

RED_ERROR = "kanban_create: session-affinity tasks require a canonical project_id"


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _assert_ok(out: dict) -> dict:
    assert out.get("ok") is True, out
    return out


def _assert_failed(out: dict, needle: str) -> None:
    assert out.get("ok") is not True, out
    assert needle in str(out.get("error", "")), out


class CrossBoard:
    """Disposable repository, board and two profile homes.

    ``supervisor`` and ``implementer`` both start with zero registered
    Projects, and the shared ``dir`` worktree leaf belongs to a prior board —
    exactly the reported recurrence.
    """

    def __init__(self, tmp_path: Path, repo: Path, shared: Path, profiles: dict):
        self.tmp_path = tmp_path
        self.repo = repo
        self.shared = shared
        self.profiles = profiles

    # -- worker impersonation ------------------------------------------------
    def as_worker(self, monkeypatch, profile: str, task_id: str) -> None:
        monkeypatch.setenv("HERMES_HOME", str(self.profiles[profile]))
        monkeypatch.setenv("HERMES_PROFILE", profile)
        monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
        monkeypatch.setenv("HERMES_KANBAN_BOARD", BOARD)

    def registered_project_ids(self, monkeypatch, profile: str) -> list[str]:
        monkeypatch.setenv("HERMES_HOME", str(self.profiles[profile]))
        with pdb.connect_closing() as conn:
            return [project.id for project in pdb.list_projects(conn)]

    # -- row helpers ---------------------------------------------------------
    def seed_shared_dir_task(
        self,
        *,
        project_id=PROJECT_ID,
        workspace=None,
        flow=FLOW_ID,
        affinity=True,
        assignee="supervisor",
        session_id=SESSION_ID,
        title="root supervisor",
    ) -> str:
        """Persist the current-board project/affinity root row.

        The row is written directly because no profile registers the Project:
        this is the durable state a prior board left behind.
        """
        conn = kb.connect()
        try:
            task_id = kb.create_task(conn, title=title, assignee=assignee)
            conn.execute(
                "UPDATE tasks SET project_id=?, workspace_kind=?, workspace_path=?, "
                "session_affinity=?, session_id=? WHERE id=?",
                (
                    project_id,
                    "dir",
                    str(workspace) if workspace is not None else str(self.shared),
                    (
                        json.dumps({"flow_id": flow, "terminal": False})
                        if affinity
                        else None
                    ),
                    session_id,
                    task_id,
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return task_id

    def set_workspace(self, task_id: str, workspace) -> None:
        conn = kb.connect()
        try:
            conn.execute(
                "UPDATE tasks SET workspace_path=? WHERE id=?",
                (str(workspace), task_id),
            )
            conn.commit()
        finally:
            conn.close()

    def rows_for_title(self, title: str) -> list[dict]:
        conn = kb.connect()
        try:
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT id, project_id, workspace_kind, workspace_path "
                    "FROM tasks WHERE title = ?",
                    (title,),
                ).fetchall()
            ]
        finally:
            conn.close()

    def get_task(self, task_id: str):
        conn = kb.connect()
        try:
            return kb.get_task(conn, task_id)
        finally:
            conn.close()

    # -- tool calls ----------------------------------------------------------
    def attempt_terminal(
        self,
        monkeypatch,
        root_id: str,
        *,
        title: str,
        flow=FLOW_ID,
        assignee="supervisor",
        project_id=None,
    ) -> dict:
        self.as_worker(monkeypatch, "supervisor", root_id)
        args = {
            "title": title,
            "assignee": assignee,
            "parents": [root_id],
            "session_affinity": {"flow_id": flow, "terminal": True},
        }
        if project_id is not None:
            args["project_id"] = project_id
        return json.loads(kt._handle_create(args))

    def create_terminal(self, monkeypatch, root_id: str) -> str:
        out = _assert_ok(
            self.attempt_terminal(monkeypatch, root_id, title="terminal supervisor")
        )
        return out["task_id"]


@pytest.fixture
def cross_board(tmp_path, monkeypatch):
    """Disposable git repo + project-scoped board + two empty profile homes."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "hlp226c@example.invalid")
    _git(repo, "config", "user.name", "HLP-226c fixture")
    (repo / "README.md").write_text("hlp226c fixture\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "fixture root")

    # The prior board's worktree: present on disk, absent from this board.
    shared = repo / ".worktrees" / PRIOR_BOARD_LEAF
    shared.mkdir(parents=True)

    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "shared-kanban.db"))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", BOARD)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)
    kb._INITIALIZED_PATHS.clear()

    kb.create_board(
        BOARD,
        name="HLP-226c board",
        default_workdir=str(repo),
        project_id=PROJECT_ID,
    )

    profiles = {
        "supervisor": tmp_path / "profiles" / "supervisor",
        "implementer": tmp_path / "profiles" / "implementer",
    }
    for path in profiles.values():
        path.mkdir(parents=True)

    return CrossBoard(tmp_path, repo, shared, profiles)


# ---------------------------------------------------------------------------
# H226C-FR-001/002 — the recurrence: same-flow terminal from a shared dir
# ---------------------------------------------------------------------------


def test_cross_board_shared_dir_terminal_recovers_board_project(
    cross_board, monkeypatch
):
    """The reported recurrence now yields the same-flow terminal with P."""
    root_id = cross_board.seed_shared_dir_task()

    out = cross_board.attempt_terminal(
        monkeypatch, root_id, title="terminal supervisor"
    )
    assert out.get("ok") is True, out

    terminal = cross_board.get_task(out["task_id"])
    assert terminal is not None
    # Exact Project recovered from the current board's own binding.
    assert terminal.project_id == PROJECT_ID
    # Exact shared workspace, persisted as an explicit directory.
    assert terminal.workspace_kind == "dir"
    assert terminal.workspace_path == str(cross_board.shared)
    # Exact flow, terminal flag and origin provenance.
    assert terminal.session_affinity == {"flow_id": FLOW_ID, "terminal": True}
    assert terminal.session_id == SESSION_ID
    # Native parent dependency gate: the terminal waits for its root.
    assert terminal.status == "todo"

    # No Project was registered in either profile registry.
    assert cross_board.registered_project_ids(monkeypatch, "supervisor") == []
    assert cross_board.registered_project_ids(monkeypatch, "implementer") == []


# ---------------------------------------------------------------------------
# H226C-FR-003 — terminal -> fresh cross-profile Implementer worktree (E2E)
# ---------------------------------------------------------------------------


def test_terminal_to_cross_profile_implementer_materializes_worktree(
    cross_board, monkeypatch
):
    """Terminal Supervisor -> Implementer child, real materialized checkout."""
    root_id = cross_board.seed_shared_dir_task()
    terminal_id = cross_board.create_terminal(monkeypatch, root_id)

    cross_board.as_worker(monkeypatch, "supervisor", terminal_id)
    out = json.loads(
        kt._handle_create({
            "title": "fresh implementer rework",
            "assignee": "implementer",
            "parents": [terminal_id],
            "workspace_kind": "worktree",
        })
    )
    assert out.get("ok") is True, out
    child_id = out["task_id"]

    child = cross_board.get_task(child_id)
    assert child is not None
    assert child.project_id == PROJECT_ID
    assert child.workspace_kind == "worktree"
    # Its own canonical task-id-keyed worktree, never the shared parent dir.
    assert child.workspace_path == str(cross_board.repo / ".worktrees" / child_id)
    assert child.workspace_path != str(cross_board.shared)
    # Deterministic Project branch convention: <project-slug>/<task-id>[-title]
    assert child.branch_name == (
        f"{pdb.normalize_slug(PROJECT_ID)}/{child_id}-fresh-implementer-rework"
    )
    assert child.status == "todo"  # dependency gate on the terminal

    # Native dispatcher resolver materializes a real, usable checkout.
    conn = kb.connect()
    try:
        fresh = kb.get_task(conn, child_id)
        assert fresh is not None
        workspace = kb.resolve_workspace(fresh, board=BOARD)
    finally:
        conn.close()
    assert workspace.is_dir()
    assert workspace == cross_board.repo / ".worktrees" / child_id
    assert _git(workspace, "rev-parse", "--abbrev-ref", "HEAD") == child.branch_name
    assert (workspace / "README.md").read_text(encoding="utf-8") == "hlp226c fixture\n"
    # Read-only Git inspection succeeds in the materialized checkout.
    assert _git(workspace, "status", "--porcelain") == ""

    # The shared parent workspace is untouched by the child materialization.
    assert cross_board.shared.is_dir()
    assert list(cross_board.shared.iterdir()) == []

    for profile in cross_board.profiles:
        assert cross_board.registered_project_ids(monkeypatch, profile) == []


# ---------------------------------------------------------------------------
# H226C-FR-004 — fail-closed matrix
# ---------------------------------------------------------------------------


def test_board_project_mismatch_fails_closed(cross_board, monkeypatch):
    kb.write_board_metadata(BOARD, project_id=OTHER_PROJECT_ID)
    root_id = cross_board.seed_shared_dir_task()

    out = cross_board.attempt_terminal(
        monkeypatch, root_id, title="must-not-persist-board-project-mismatch"
    )
    _assert_failed(out, "canonical project_id")
    assert cross_board.rows_for_title("must-not-persist-board-project-mismatch") == []


def test_board_without_project_binding_fails_closed(cross_board, monkeypatch):
    kb.write_board_metadata(BOARD, project_id="")
    root_id = cross_board.seed_shared_dir_task()

    out = cross_board.attempt_terminal(
        monkeypatch, root_id, title="must-not-persist-board-without-project"
    )
    _assert_failed(out, RED_ERROR)
    assert cross_board.rows_for_title("must-not-persist-board-without-project") == []


def test_board_repository_mismatch_fails_closed(cross_board, monkeypatch):
    elsewhere = cross_board.tmp_path / "elsewhere"
    elsewhere.mkdir()
    kb.write_board_metadata(BOARD, default_workdir=str(elsewhere))
    root_id = cross_board.seed_shared_dir_task()

    out = cross_board.attempt_terminal(
        monkeypatch, root_id, title="must-not-persist-board-repo-mismatch"
    )
    _assert_failed(out, RED_ERROR)
    assert cross_board.rows_for_title("must-not-persist-board-repo-mismatch") == []


@pytest.mark.parametrize("default_workdir", ["relative/repo", ""])
def test_board_default_workdir_must_be_absolute(
    cross_board, monkeypatch, default_workdir
):
    kb.write_board_metadata(BOARD, default_workdir=default_workdir)
    root_id = cross_board.seed_shared_dir_task()

    out = cross_board.attempt_terminal(
        monkeypatch, root_id, title="must-not-persist-bad-board-workdir"
    )
    _assert_failed(out, RED_ERROR)
    assert cross_board.rows_for_title("must-not-persist-bad-board-workdir") == []


def _noncanonical_dir(cross_board, case: str) -> str:
    repo = cross_board.repo
    if case == "outside-worktrees":
        outside = repo / "shared"
        outside.mkdir()
        return str(outside)
    if case == "nested-leaf":
        nested = repo / ".worktrees" / "prior" / "nested"
        nested.mkdir(parents=True)
        return str(nested)
    if case == "relative":
        return os.path.join(".worktrees", PRIOR_BOARD_LEAF)
    if case == "traversal":
        return str(repo / ".worktrees" / ".." / "escape")
    if case == "symlink-escape":
        outside = cross_board.tmp_path / "outside"
        outside.mkdir()
        link = repo / ".worktrees" / "leaked"
        link.symlink_to(outside, target_is_directory=True)
        return str(link)
    if case == "prefix-collision-repo":
        other = cross_board.tmp_path / "repo-other"
        (other / ".worktrees" / PRIOR_BOARD_LEAF).mkdir(parents=True)
        return str(other / ".worktrees" / PRIOR_BOARD_LEAF)
    raise AssertionError(f"unknown case {case!r}")


@pytest.mark.parametrize(
    "case",
    [
        "outside-worktrees",
        "nested-leaf",
        "relative",
        "traversal",
        "symlink-escape",
        "prefix-collision-repo",
    ],
)
def test_noncanonical_shared_dir_fails_closed(cross_board, monkeypatch, case):
    """A path-shaped string alone is never Project authority."""
    root_id = cross_board.seed_shared_dir_task(
        workspace=_noncanonical_dir(cross_board, case)
    )

    out = cross_board.attempt_terminal(
        monkeypatch, root_id, title="must-not-persist-noncanonical-dir"
    )
    _assert_failed(out, RED_ERROR)
    assert cross_board.rows_for_title("must-not-persist-noncanonical-dir") == []


def test_in_board_non_root_leaf_does_not_authorize_recovery(cross_board, monkeypatch):
    """A leaf that resolves in this board but is not the canonical root fails."""
    root_id = cross_board.seed_shared_dir_task()
    cross_board.set_workspace(root_id, cross_board.repo / ".worktrees" / root_id)

    out = cross_board.attempt_terminal(
        monkeypatch, root_id, title="must-not-persist-leaf-conflict"
    )
    _assert_failed(out, RED_ERROR)
    assert cross_board.rows_for_title("must-not-persist-leaf-conflict") == []


@pytest.mark.parametrize(
    "source_project", [None, OTHER_PROJECT_ID], ids=["absent", "conflicting"]
)
def test_source_project_absent_or_conflicting_fails_closed(
    cross_board, monkeypatch, source_project
):
    root_id = cross_board.seed_shared_dir_task(project_id=source_project)
    cross_board.as_worker(monkeypatch, "supervisor", root_id)

    conn = kb.connect()
    try:
        with pytest.raises(ValueError, match="canonical project_id"):
            kb.create_task(
                conn,
                title="must-not-persist-source-project",
                assignee="supervisor",
                project_id=PROJECT_ID,
                project_source_task_id=root_id,
                session_affinity={"flow_id": FLOW_ID, "terminal": True},
            )
    finally:
        conn.close()
    assert cross_board.rows_for_title("must-not-persist-source-project") == []


def test_explicit_conflicting_project_is_rejected(cross_board, monkeypatch):
    root_id = cross_board.seed_shared_dir_task()

    out = cross_board.attempt_terminal(
        monkeypatch,
        root_id,
        title="must-not-persist-explicit-project",
        project_id=OTHER_PROJECT_ID,
    )
    _assert_failed(out, "does not match the worker task's canonical project")
    assert cross_board.rows_for_title("must-not-persist-explicit-project") == []


@pytest.mark.parametrize(
    "flow,assignee",
    [(OTHER_FLOW_ID, "supervisor"), (FLOW_ID, "implementer")],
    ids=["flow", "assignee"],
)
def test_affinity_flow_or_assignee_mismatch_is_rejected(
    cross_board, monkeypatch, flow, assignee
):
    root_id = cross_board.seed_shared_dir_task()

    out = cross_board.attempt_terminal(
        monkeypatch,
        root_id,
        title="must-not-persist-affinity-mismatch",
        flow=flow,
        assignee=assignee,
    )
    _assert_failed(out, "only match the current worker's flow and assignee")
    assert cross_board.rows_for_title("must-not-persist-affinity-mismatch") == []


# ---------------------------------------------------------------------------
# Preservation controls
# ---------------------------------------------------------------------------


def test_non_affinity_shared_dir_source_keeps_legacy_behavior(cross_board, monkeypatch):
    """Only affinity recovery changed; a plain dir source still drops P."""
    root_id = cross_board.seed_shared_dir_task(affinity=False)
    cross_board.as_worker(monkeypatch, "supervisor", root_id)

    out = json.loads(
        kt._handle_create({
            "title": "legacy non-affinity child",
            "assignee": "implementer",
            "parents": [root_id],
            "workspace_kind": "worktree",
        })
    )
    assert out.get("ok") is True, out
    child = cross_board.get_task(out["task_id"])
    assert child is not None
    # Unchanged legacy downgrade: no Project/repository recovery without
    # session affinity, so no canonical `.worktrees/<task-id>` path is
    # derived. The persistent board `default_workdir` remains the only
    # native fallback (unchanged pre-existing behavior).
    assert child.project_id is None
    assert child.branch_name is None
    assert child.workspace_path == str(cross_board.repo)
