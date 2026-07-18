"""The independent checker.

This is the part of FleetProof that no agent runs against itself. The agent that
did the work authored ``checks.json``; *this* module executes those checks — each
declared command in its own OS subprocess — grades the results deterministically,
and appends the verdict to the run log. It is invoked from the Stop hook, i.e.
from a process the working agent does not control.

The separation is load-bearing:
    - the working agent writes the run records and the check spec;
    - the checker (a different process) executes and grades them.

No LLM is in this path. Grading is pure comparison.
"""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .checks import Check, load_checks
from .runlog import record, runs_dir

# Per-check wall-clock ceiling. A check that hangs is a failed check, not a hung fleet.
DEFAULT_TIMEOUT_S = 600


@dataclass
class CheckResult:
    id: str
    expectation: str
    passed: bool
    blocking: bool
    returncode: int | None
    detail: str
    duration_ms: float
    stdout_tail: str = ""
    stderr_tail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CheckReport:
    results: list[CheckResult] = field(default_factory=list)
    run_id: str | None = None

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if not r.passed)

    @property
    def blocking_failures(self) -> list[CheckResult]:
        return [r for r in self.results if not r.passed and r.blocking]

    @property
    def verdict(self) -> str:
        """'pass' unless a blocking check failed. Non-blocking failures do not gate."""
        return "fail" if self.blocking_failures else "pass"

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "summary": {
                "total": self.total,
                "passed": self.passed,
                "failed": self.failed,
                "blocking_failed": len(self.blocking_failures),
            },
            "checks": [r.to_dict() for r in self.results],
        }


def _tail(text: str, limit: int = 2000) -> str:
    if len(text) <= limit:
        return text
    return "…(truncated)…\n" + text[-limit:]


def _run_command(command: str, cwd: Path, timeout: int) -> tuple[int | None, str, str]:
    """Execute a shell command in a separate process. Returns (returncode, stdout, stderr).

    A timeout or spawn failure yields a None returncode, which every grader treats
    as a failure — the tool never lets an unrunnable check silently pass.
    """
    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except subprocess.TimeoutExpired as e:
        out = e.stdout or ""
        err = e.stderr or ""
        if isinstance(out, bytes):
            out = out.decode(errors="replace")
        if isinstance(err, bytes):
            err = err.decode(errors="replace")
        return None, out, (err + f"\n[fleetproof] command timed out after {timeout}s")
    except OSError as e:
        return None, "", f"[fleetproof] could not launch command: {e}"


def _grade(check: Check, returncode: int | None, stdout: str, stderr: str, cwd: Path) -> tuple[bool, str]:
    """Return (passed, human-readable detail) for one check. Pure comparison."""
    kind = check.expect["kind"]

    if kind in ("exit0", "exit"):
        want = 0 if kind == "exit0" else check.expect["code"]
        if returncode is None:
            return False, f"command did not complete (expected exit {want})"
        ok = returncode == want
        return ok, f"exit {returncode} (expected {want})"

    if kind == "regex":
        if returncode is None:
            return False, "command did not complete; cannot match output"
        pattern = check.expect["pattern"]
        combined = f"{stdout}\n{stderr}"
        ok = re.search(pattern, combined) is not None
        return ok, ("matched" if ok else "no match") + f" for /{pattern}/"

    if kind == "file_exists":
        target = (cwd / check.expect["path"]).resolve()
        ok = target.exists()
        return ok, ("present" if ok else "missing") + f": {check.expect['path']}"

    return False, f"unknown expectation kind: {kind}"


def run_check(check: Check, cwd: Path, timeout: int = DEFAULT_TIMEOUT_S) -> CheckResult:
    """Execute and grade a single check in its own subprocess."""
    import time as _time
    started = _time.perf_counter()
    returncode: int | None = None
    stdout = stderr = ""
    if check.run is not None:
        returncode, stdout, stderr = _run_command(check.run, cwd, timeout)
    passed, detail = _grade(check, returncode, stdout, stderr, cwd)
    duration_ms = (_time.perf_counter() - started) * 1000.0
    return CheckResult(
        id=check.id,
        expectation=check.describe_expectation(),
        passed=passed,
        blocking=check.block,
        returncode=returncode,
        detail=detail,
        duration_ms=round(duration_ms, 3),
        stdout_tail=_tail(stdout),
        stderr_tail=_tail(stderr),
    )


def run_checks(
    checks: list[Check] | None = None,
    *,
    cwd: Path | None = None,
    timeout: int = DEFAULT_TIMEOUT_S,
    record_to_log: bool = True,
) -> CheckReport:
    """Run every check and (by default) append the verdict to the run log.

    ``checks`` defaults to the loaded ``.fleetproof/checks.json``. ``cwd`` defaults
    to the current working directory — the directory the fleet actually worked in.
    """
    if checks is None:
        checks = load_checks()
    work_dir = Path(cwd) if cwd is not None else Path.cwd()

    report = CheckReport()
    if not record_to_log:
        for check in checks:
            report.results.append(run_check(check, work_dir, timeout))
        return report

    with record("fleetproof", "check", {"check_count": len(checks)}) as handle:
        report.run_id = handle.run_id
        for check in checks:
            report.results.append(run_check(check, work_dir, timeout))
        payload = report.to_dict()
        payload["recorded_from_pid"] = _self_pid()
        handle.set_output(payload)
    return report


def _self_pid() -> int:
    import os
    return os.getpid()


def format_report_text(report: CheckReport) -> str:
    """A compact plain-text rendering for the CLI and hook feedback."""
    lines = []
    for r in report.results:
        mark = "PASS" if r.passed else ("FAIL" if r.blocking else "warn")
        lines.append(f"  [{mark}] {r.id}: {r.detail}")
    s = report.to_dict()["summary"]
    lines.append(
        f"{report.verdict.upper()} - {s['passed']}/{s['total']} passed, "
        f"{s['blocking_failed']} blocking failure(s)."
    )
    return "\n".join(lines)


if __name__ == "__main__":  # pragma: no cover - manual smoke entry
    rep = run_checks()
    print(format_report_text(rep))
    sys.exit(1 if rep.verdict == "fail" else 0)
