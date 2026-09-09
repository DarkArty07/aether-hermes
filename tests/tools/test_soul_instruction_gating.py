"""Tests for tracked package SOUL vs live profile SOUL instruction gating.

Issue #315 / ABR-315:
- Tracked package-source SOUL.md files (e.g. in repository worktrees) are
  ordinary reversible edits that do not steer future agent sessions, and
  must proceed without false approval prompt or timeout.
- Live profile SOUL.md files (HERMES_HOME/SOUL.md, root profiles) steer
  future agent behavior and must ALWAYS require approval / fail closed.
- Project-local instruction files (AGENTS.md, CLAUDE.md, .cursorrules)
  remain protected and require approval.
"""

import json
import pytest

from tools.file_tools import write_file_tool, patch_tool
from tools.terminal_tool import set_approval_callback
import tools.file_tools as ft
import hermes_constants


@pytest.fixture(autouse=True)
def _gate_on(monkeypatch):
    monkeypatch.setattr(
        ft, "_protected_instruction_config", lambda: (True, [])
    )


@pytest.fixture
def approvals():
    """Install a CLI approval callback; record calls; scripted answers."""
    state = {"calls": [], "answer": "deny"}

    def cb(command, description, **kwargs):
        state["calls"].append(
            {"command": command, "description": description, **kwargs}
        )
        return state["answer"]

    set_approval_callback(cb)
    yield state
    set_approval_callback(None)


def _write(path, content="test content"):
    return json.loads(write_file_tool(str(path), content))


def test_tracked_package_soul_write_proceeds_without_approval(tmp_path, approvals):
    """An ordinary edit of tracked package source SOUL.md proceeds without approval prompt."""
    repo = tmp_path / "repo"
    tracked_soul = (
        repo / "src" / "aether_agents" / "resources" / "profiles" / "supervisor" / "SOUL.md"
    )
    tracked_soul.parent.mkdir(parents=True)
    tracked_soul.write_text("persona v1\n", encoding="utf-8")

    # approvals["answer"] is "deny" — if the gate prompts, this would block
    res = _write(tracked_soul, "persona v2\n")
    assert not res.get("error"), res
    assert tracked_soul.read_text(encoding="utf-8") == "persona v2\n"
    assert approvals["calls"] == [], "tracked package source must not trigger approval"


def test_tracked_package_soul_patch_proceeds_without_approval(tmp_path, approvals):
    """Patching tracked package source SOUL.md proceeds without approval prompt."""
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    tracked_soul = repo / "SOUL.md"
    tracked_soul.write_text("line1\nold persona\nline3\n", encoding="utf-8")

    res = json.loads(
        patch_tool(
            mode="replace",
            path=str(tracked_soul),
            old_string="old persona",
            new_string="new persona",
        )
    )
    assert not res.get("error"), res
    assert "new persona" in tracked_soul.read_text(encoding="utf-8")
    assert approvals["calls"] == []


def test_live_active_profile_soul_write_requires_approval_and_deny_blocks(
    tmp_path, approvals, monkeypatch
):
    """Writing to active profile SOUL.md in HERMES_HOME requires approval and blocks on deny."""
    fake_home = tmp_path / "active_profile"
    fake_home.mkdir(parents=True)
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: fake_home)
    monkeypatch.setattr(hermes_constants, "get_default_hermes_root", lambda: fake_home)
    monkeypatch.setattr(ft, "_get_real_hermes_home", lambda: str(fake_home.resolve()))
    if hasattr(ft, "_get_real_hermes_root"):
        monkeypatch.setattr(ft, "_get_real_hermes_root", lambda: str(fake_home.resolve()))

    live_soul = fake_home / "SOUL.md"
    approvals["answer"] = "deny"

    res = _write(live_soul, "injected persona")
    assert res.get("error") and "BLOCKED" in res["error"]
    assert "SOUL.md" in res["error"]
    assert not live_soul.exists()
    assert len(approvals["calls"]) == 1


def test_live_active_profile_soul_no_human_fails_closed(tmp_path, monkeypatch):
    """Writing to active profile SOUL.md without interactive human fails closed."""
    fake_home = tmp_path / "active_profile"
    fake_home.mkdir(parents=True)
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: fake_home)
    monkeypatch.setattr(hermes_constants, "get_default_hermes_root", lambda: fake_home)
    monkeypatch.setattr(ft, "_get_real_hermes_home", lambda: str(fake_home.resolve()))
    if hasattr(ft, "_get_real_hermes_root"):
        monkeypatch.setattr(ft, "_get_real_hermes_root", lambda: str(fake_home.resolve()))

    live_soul = fake_home / "SOUL.md"
    res = _write(live_soul, "injected persona")
    assert res.get("error") and "BLOCKED" in res["error"]
    assert "SOUL.md" in res["error"]
    assert not live_soul.exists()


def test_live_named_profile_soul_requires_approval(tmp_path, approvals, monkeypatch):
    """Writing to named profile SOUL.md under HERMES_ROOT/profiles/<name>/ requires approval."""
    fake_root = tmp_path / "hermes_root"
    named_profile_dir = fake_root / "profiles" / "coder"
    named_profile_dir.mkdir(parents=True)
    monkeypatch.setattr(hermes_constants, "get_default_hermes_root", lambda: fake_root)
    if hasattr(ft, "_get_real_hermes_root"):
        monkeypatch.setattr(ft, "_get_real_hermes_root", lambda: str(fake_root.resolve()))

    named_soul = named_profile_dir / "SOUL.md"
    approvals["answer"] = "deny"

    res = _write(named_soul, "injected coder persona")
    assert res.get("error") and "BLOCKED" in res["error"]
    assert "SOUL.md" in res["error"]
    assert not named_soul.exists()
    assert len(approvals["calls"]) == 1


def test_project_agents_md_still_requires_approval(tmp_path, approvals):
    """Project-local AGENTS.md remains protected across directories."""
    target = tmp_path / "AGENTS.md"
    approvals["answer"] = "deny"
    res = _write(target, "injected instructions")
    assert res.get("error") and "BLOCKED" in res["error"]
    assert not target.exists()
    assert len(approvals["calls"]) == 1
