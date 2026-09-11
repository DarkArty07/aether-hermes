"""Focused unit and regression tests for #372: One effective cron script root.

Creation/update validation, lifecycle scanning, execution and user-facing diagnostics
resolve `script` and `monitor_script` through ONE effective profile-scoped script-root contract.
Relative paths stay confined to the active `HERMES_HOME/scripts`; absolute, home-relative,
traversal, symlink-escape and non-regular targets fail closed. A create accepted for an
existing script executes that same file (same inode/content). Error text names the
effective profile root instead of the default-profile `~/.hermes/scripts` shorthand.
Resolve the effective home at call time; no import-time/default-home capture.
"""

from __future__ import annotations

from pathlib import Path
import pytest


@pytest.fixture(autouse=True)
def _reset_context_vars():
    from gateway.session_context import _UNSET, _VAR_MAP
    for v in _VAR_MAP.values():
        v.set(_UNSET)
    yield
    for v in _VAR_MAP.values():
        v.set(_UNSET)


@pytest.fixture
def profile_env(tmp_path, monkeypatch):
    """Isolated environment simulating a non-default profile (e.g. morfeo)."""
    default_home = tmp_path / "default_hermes"
    default_home.mkdir()
    (default_home / "scripts").mkdir()
    (default_home / "cron").mkdir()

    profile_home = tmp_path / "profiles" / "morfeo"
    profile_home.mkdir(parents=True)
    (profile_home / "scripts").mkdir()
    (profile_home / "cron").mkdir()
    (profile_home / "cron" / "output").mkdir()

    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    monkeypatch.setenv("HERMES_PROFILE", "morfeo")
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_SESSION_KEY", raising=False)
    monkeypatch.delenv("HERMES_UI_SESSION_ID", raising=False)
    monkeypatch.delenv("HERMES_SESSION_PLATFORM", raising=False)
    monkeypatch.delenv("HERMES_SESSION_CHAT_ID", raising=False)

    import cron.jobs as jobs_mod
    monkeypatch.setattr(jobs_mod, "HERMES_DIR", profile_home)
    monkeypatch.setattr(jobs_mod, "CRON_DIR", profile_home / "cron")
    monkeypatch.setattr(jobs_mod, "JOBS_FILE", profile_home / "cron" / "jobs.json")
    monkeypatch.setattr(jobs_mod, "OUTPUT_DIR", profile_home / "cron" / "output")

    import cron.scheduler as sched_mod
    monkeypatch.setattr(sched_mod, "_hermes_home", profile_home)

    from hermes_cli import kanban_db as kb
    kb._INITIALIZED_PATHS.clear()

    return {
        "default_home": default_home,
        "profile_home": profile_home,
        "profile_scripts": profile_home / "scripts",
        "default_scripts": default_home / "scripts",
    }


class TestIssue372ScriptRoot:
    def test_validation_rejects_absolute_path_and_names_effective_profile_root(self, profile_env):
        from tools.cronjob_tools import _validate_cron_script_path

        profile_scripts = profile_env["profile_scripts"]
        err = _validate_cron_script_path("/tmp/arbitrary.py")

        assert err is not None
        # Must fail closed on absolute paths
        assert "must be relative" in err.lower()
        # Must name the effective profile root instead of ~/.hermes/scripts/
        assert str(profile_scripts) in err
        assert "~/.hermes/scripts" not in err

    def test_validation_rejects_home_relative_path(self, profile_env):
        from tools.cronjob_tools import _validate_cron_script_path

        profile_scripts = profile_env["profile_scripts"]
        err = _validate_cron_script_path("~/my_script.py")

        assert err is not None
        assert str(profile_scripts) in err
        assert "~/.hermes/scripts" not in err

    def test_validation_rejects_traversal(self, profile_env):
        from tools.cronjob_tools import _validate_cron_script_path

        err = _validate_cron_script_path("../../etc/passwd")
        assert err is not None
        assert "escapes" in err.lower() or "traversal" in err.lower()

    def test_validation_rejects_symlink_escape(self, profile_env, tmp_path):
        from tools.cronjob_tools import _validate_cron_script_path

        outside = tmp_path / "outside_evil.py"
        outside.write_text('print("evil")\n')

        symlink = profile_env["profile_scripts"] / "escaped_link.py"
        try:
            symlink.symlink_to(outside)
        except OSError:
            pytest.skip("Symlinks not supported in this environment")

        err = _validate_cron_script_path("escaped_link.py")
        assert err is not None
        assert "escapes" in err.lower() or "symlink" in err.lower() or "traversal" in err.lower()

    def test_validation_rejects_non_regular_target(self, profile_env):
        from tools.cronjob_tools import _validate_cron_script_path

        subdir = profile_env["profile_scripts"] / "a_directory"
        subdir.mkdir()

        err = _validate_cron_script_path("a_directory")
        assert err is not None
        assert "not a regular file" in err.lower() or "not a file" in err.lower()

    def test_validation_accepts_valid_relative_script(self, profile_env):
        from tools.cronjob_tools import _validate_cron_script_path

        script_file = profile_env["profile_scripts"] / "valid.py"
        script_file.write_text('print("valid")\n')

        err = _validate_cron_script_path("valid.py")
        assert err is None

    def test_create_accepted_script_executes_same_file_same_inode(self, profile_env):
        from tools.cronjob_tools import _validate_cron_script_path
        from cron.scheduler import _run_job_script

        profile_script = profile_env["profile_scripts"] / "fingerprinted.py"
        profile_script.write_text('print("profile-morfeo-unique-token-999")\n')

        default_script = profile_env["default_scripts"] / "fingerprinted.py"
        default_script.write_text('print("default-decoy-token-000")\n')

        # Validation under active morfeo profile passes
        err = _validate_cron_script_path("fingerprinted.py")
        assert err is None

        # Execution resolves under active profile, NOT default decoy
        success, output = _run_job_script("fingerprinted.py")
        assert success is True
        assert "profile-morfeo-unique-token-999" in output
        assert "default-decoy-token-000" not in output

    def test_monitor_script_shares_identical_resolver_contract(self, profile_env):
        from tools.cronjob_tools import _validate_cron_script_path
        from cron.monitor import _run_monitor_source

        profile_mon = profile_env["profile_scripts"] / "monitor_source.py"
        profile_mon.write_text('print("hash-input-value-42")\n')

        # Negative checks on monitor_script
        assert _validate_cron_script_path("/tmp/outside_mon.py") is not None
        assert _validate_cron_script_path("../../escape.sh") is not None

        # Positive check
        assert _validate_cron_script_path("monitor_source.py") is None

        job = {"monitor_script": "monitor_source.py"}
        ok, output = _run_monitor_source(job)
        assert ok is True
        assert "hash-input-value-42" in output

    def test_lifecycle_scanning_resolves_profile_scripts_root(self, profile_env):
        from cron.lifecycle_guard import check_gateway_lifecycle, GatewayLifecycleBlocked

        bad_script = profile_env["profile_scripts"] / "restart_gateway.py"
        bad_script.write_text('import os\nos.system("hermes gateway restart")\n')

        with pytest.raises(GatewayLifecycleBlocked):
            check_gateway_lifecycle(prompt="run monitor", script="restart_gateway.py")

        with pytest.raises(GatewayLifecycleBlocked):
            check_gateway_lifecycle(prompt="run monitor", monitor_script="restart_gateway.py")
