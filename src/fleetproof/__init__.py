"""FleetProof — independent, out-of-band verification for agent fleets.

The agent may author the checks; it never executes and grades its own work.
"""

from __future__ import annotations

__version__ = "0.2.1"

from .runlog import (
    RunRecord,
    SubInvocation,
    child_run_id,
    current_run_id,
    list_run_records,
    load_run,
    project_root,
    record,
    recorded,
    runs_dir,
    set_runs_dir,
)
from .checks import Check, CheckSpecError, load_checks
from .checker import CheckReport, CheckResult, run_check, run_checks

__all__ = [
    "__version__",
    "recorded",
    "record",
    "current_run_id",
    "child_run_id",
    "project_root",
    "runs_dir",
    "set_runs_dir",
    "list_run_records",
    "load_run",
    "RunRecord",
    "SubInvocation",
    "Check",
    "CheckSpecError",
    "load_checks",
    "CheckReport",
    "CheckResult",
    "run_check",
    "run_checks",
]
