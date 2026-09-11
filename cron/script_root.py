"""Canonical profile-scoped script root resolver for cron (#372).

Shared contract across:
- API / tool validation (cronjob_create, cronjob_update)
- Gateway lifecycle scanning (cron.lifecycle_guard)
- Scheduler and monitor execution (cron.scheduler, cron.monitor)
- User-facing diagnostics

Confines scripts to the active HERMES_HOME/scripts directory resolved dynamically
at call time. Absolute, home-relative, traversal, symlink-escape, and non-regular
targets fail closed. Error text names the effective profile root.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


def get_effective_scripts_dir() -> Path:
    """Return the active profile-scoped scripts directory, resolved at call time.

    Respects context-local overrides, HERMES_HOME, and scheduler test hooks.
    """
    from hermes_constants import get_hermes_home

    try:
        from cron import scheduler
        base = scheduler._hermes_home or get_hermes_home()
    except Exception:
        base = get_hermes_home()

    scripts_dir = Path(base) / "scripts"
    try:
        scripts_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return scripts_dir


def resolve_cron_script_path(
    script_path: Optional[str],
    *,
    for_execution: bool = False,
) -> tuple[Optional[Path], Optional[str]]:
    """Resolve and validate a cron script path under the active scripts root.

    Args:
        script_path: The user-supplied or stored script string.
        for_execution: When True, requires the target to exist and be a regular file,
            and validates path confinement for execution. When False (validation mode),
            rejects all absolute/home-relative paths, traversal, symlink escapes,
            and existing non-regular targets.

    Returns:
        (resolved_path, error_message): Exactly one is non-None for a non-empty
        input. If script_path is empty or None, returns (None, None).
    """
    if not script_path or not str(script_path).strip():
        return None, None

    raw = str(script_path).strip()
    if "\0" in raw:
        return None, f"Blocked: script path is not a valid filesystem path: {script_path!r}"

    scripts_dir = get_effective_scripts_dir()
    scripts_dir_resolved = scripts_dir.resolve()

    # Detect absolute or home-relative paths
    is_abs_or_home = (
        raw.startswith(("/", "~"))
        or (len(raw) >= 2 and raw[1] == ":")
        or Path(raw).is_absolute()
    )

    if is_abs_or_home:
        if for_execution:
            try:
                expanded = Path(raw).expanduser().resolve()
            except (ValueError, RuntimeError, OSError):
                return None, f"Blocked: script path is not a valid filesystem path: {script_path!r}"

            try:
                expanded.relative_to(scripts_dir_resolved)
            except ValueError:
                return None, (
                    f"Blocked: script path resolves outside the scripts directory "
                    f"({scripts_dir_resolved}): {script_path!r}"
                )

            if not expanded.exists():
                return None, f"Script not found: {expanded}"
            if not expanded.is_file():
                return None, f"Script path is not a file: {expanded}"
            return expanded, None
        else:
            return None, (
                f"Script path must be relative to {scripts_dir}. "
                f"Got absolute or home-relative path: {raw!r}. "
                f"Place scripts in {scripts_dir} and use just the filename."
            )

    # Relative path checks
    try:
        raw_path = Path(raw)
        if ".." in raw_path.parts:
            # Explicit traversal component
            if for_execution:
                return None, (
                    f"Blocked: script path resolves outside the scripts directory "
                    f"({scripts_dir_resolved}): {script_path!r}"
                )
            return None, f"Script path escapes the scripts directory via traversal: {raw!r}"

        candidate = scripts_dir / raw_path
        resolved = candidate.resolve()
        resolved.relative_to(scripts_dir_resolved)
    except (ValueError, RuntimeError, OSError):
        if for_execution:
            return None, (
                f"Blocked: script path resolves outside the scripts directory "
                f"({scripts_dir_resolved}): {script_path!r}"
            )
        return None, f"Script path escapes the scripts directory via traversal or symlink: {raw!r}"

    # Non-regular target check (directories, devices, sockets, FIFOs)
    if candidate.exists() and not candidate.is_file():
        if for_execution:
            return None, f"Script path is not a file: {candidate}"
        return None, f"Script path is not a regular file: {raw!r}"

    if for_execution:
        if not candidate.exists():
            return None, f"Script not found: {candidate}"
        if not candidate.is_file():
            return None, f"Script path is not a file: {candidate}"
        return candidate, None

    return candidate, None


def validate_cron_script_path(script_path: Optional[str]) -> Optional[str]:
    """Validate a cron script or monitor_script path at the API boundary.

    Returns an error string if blocked, or None if valid.
    """
    _, error = resolve_cron_script_path(script_path, for_execution=False)
    return error
