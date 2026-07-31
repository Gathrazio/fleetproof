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

from .checks import (
    VALID_TIERS,
    Check,
    default_checks_path,
    load_checks,
    short_spec_hash,
    spec_hash,
)
from .runlog import list_run_records, record, runs_dir

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
    spec_sha256: str | None = None
    # Which tier's checks this verdict covers; None means "every check in the
    # spec" (the v0.1 behaviour). Additive — older records omit it entirely.
    tier: str | None = None

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
            # SHA-256 of the checks.json that governed this verdict. Additive:
            # records written before this field existed simply omit it, and every
            # reader treats a missing value as None (no hash on record).
            "spec_sha256": self.spec_sha256,
            "tier": self.tier,
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


def select_checks(checks: list[Check], tier: str | None) -> list[Check]:
    """The subset of ``checks`` that governs ``tier``.

    ``tier=None`` selects everything, exactly as v0.1 did — the Stop hook passes
    no tier and must keep grading the whole spec. With a tier given, a check is
    selected when its own ``tier`` matches, plus every *untiered* check when the
    tier is ``bridge``: a spec written before tiers existed describes the session
    as a whole, which is the bridge's job. So an untiered check never fires for a
    leaf, and a leaf-tier check never gates the bridge.
    """
    if tier is None:
        return list(checks)
    if tier not in VALID_TIERS:
        raise ValueError(f"Unknown tier {tier!r}; expected one of {sorted(VALID_TIERS)}.")
    return [
        c for c in checks
        if c.tier == tier or (c.tier is None and tier == "bridge")
    ]


def run_checks(
    checks: list[Check] | None = None,
    *,
    cwd: Path | None = None,
    timeout: int = DEFAULT_TIMEOUT_S,
    record_to_log: bool = True,
    spec_path: Path | None = None,
    tier: str | None = None,
) -> CheckReport:
    """Run every selected check and (by default) append the verdict to the run log.

    ``checks`` defaults to the loaded ``.fleetproof/checks.json``. ``cwd`` defaults
    to the current working directory — the directory the fleet actually worked in.
    ``spec_path`` is the check-spec file whose bytes get hashed onto the verdict;
    when omitted it is resolved from ``cwd`` the same way the checker itself finds
    the spec, so the recorded hash always describes the spec that governed the run.
    ``tier`` narrows which checks run (see :func:`select_checks`); omitting it runs
    them all, which is what every v0.1 caller gets.

    A tier with no matching checks yields an empty report, whose verdict is "pass"
    because nothing was declared to fail — the recorded total of 0 is what makes
    that visible rather than silent.
    """
    if checks is None:
        checks = load_checks(spec_path)
    checks = select_checks(checks, tier)
    work_dir = Path(cwd) if cwd is not None else Path.cwd()
    resolved_spec = Path(spec_path) if spec_path is not None else default_checks_path(work_dir)
    sha = spec_hash(resolved_spec)

    report = CheckReport(spec_sha256=sha, tier=tier)
    if not record_to_log:
        for check in checks:
            report.results.append(run_check(check, work_dir, timeout))
        return report

    with record("fleetproof", "check", {"check_count": len(checks), "tier": tier}) as handle:
        report.run_id = handle.run_id
        for check in checks:
            report.results.append(run_check(check, work_dir, timeout))
        payload = report.to_dict()
        payload["recorded_from_pid"] = _self_pid()
        handle.set_output(payload)
    return report


def session_spec_baseline(session_id: str | None) -> str | None:
    """The spec hash of the EARLIEST recorded checker verdict in ``session_id``.

    This is the baseline every later verdict in the same session is compared
    against: if a verdict's own spec hash differs from this, the checks.json was
    edited mid-session (spec drift). Legacy verdicts with no hash on record are
    skipped, so the baseline is the earliest verdict that actually carries one.
    Returns None when there is no session context or no hashed verdict yet — in
    which case there is nothing to drift *from*, so callers treat it as no drift.
    """
    if not session_id:
        return None
    hashed: list[tuple[str, str]] = []
    for run in list_run_records():
        if (run.session_id or None) != session_id:
            continue
        for sub in run.sub_invocations:
            if sub.tool != "fleetproof" or sub.subcmd != "check":
                continue
            payload = sub.load_output()
            if not isinstance(payload, dict):
                continue
            sha = payload.get("spec_sha256")
            if sha:
                order_key = sub.started_at or run.started_at or ""
                hashed.append((order_key, sha))
    if not hashed:
        return None
    hashed.sort(key=lambda item: item[0])
    return hashed[0][1]


def spec_drifted(current_hash: str | None, session_id: str | None) -> tuple[bool, str | None]:
    """Return ``(drifted, baseline_hash)`` for a verdict's hash within a session.

    ``drifted`` is True only when a session baseline exists and the current hash
    differs from it — a v0.1 pass-with-drift still passes, this just flags it.
    """
    baseline = session_spec_baseline(session_id)
    drifted = bool(baseline and current_hash and baseline != current_hash)
    return drifted, baseline


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
    lines.append(f"spec: {short_spec_hash(report.spec_sha256)}")
    # Only surfaced when scoped, so untiered (v0.1) output is byte-identical.
    if report.tier:
        lines.append(f"tier: {report.tier}")
    return "\n".join(lines)


if __name__ == "__main__":  # pragma: no cover - manual smoke entry
    rep = run_checks()
    print(format_report_text(rep))
    sys.exit(1 if rep.verdict == "fail" else 0)
