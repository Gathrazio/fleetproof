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

import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .checks import (
    VALID_TIERS,
    Check,
    checks_tree_hash,
    default_checks_path,
    load_checks,
    short_spec_hash,
    spec_hash,
    unknown_tier_message,
)
from .runlog import list_run_records, project_root, record, runs_dir

# Per-check wall-clock ceiling. A check that hangs is a failed check, not a hung fleet.
DEFAULT_TIMEOUT_S = 600

# === Dispatch identity in the check environment ===
#
# Repo checks are selected per tier and, until this existed, received no
# context about WHICH dispatch they were grading — so a four-lane fleet in four
# repos could not be gated per lane from the spec, only from manifests
# (observed in a field deployment on Windows). Every check a gate runs now
# sees these four variables; a spec check may branch on FLEETPROOF_AGENT_TYPE
# and grade one lane differently from another. Values are the empty string
# when unknown, never absent: a check can test for emptiness without first
# testing for existence. FLEETPROOF_RUN_ID here is the *dispatch's* run id —
# the same variable name runlog uses for run-id propagation, deliberately, so
# a check that itself records lands under the dispatch it graded. The bare
# CLI (``fleetproof check``) has no dispatch, so it sets tier and session only.
CHECK_ENV_RUN_ID = "FLEETPROOF_RUN_ID"
CHECK_ENV_AGENT_TYPE = "FLEETPROOF_AGENT_TYPE"
CHECK_ENV_TIER = "FLEETPROOF_TIER"
CHECK_ENV_SESSION_ID = "FLEETPROOF_SESSION_ID"
CHECK_ENV_KEYS = (CHECK_ENV_RUN_ID, CHECK_ENV_AGENT_TYPE, CHECK_ENV_TIER,
                  CHECK_ENV_SESSION_ID)


def check_env(identity: dict[str, Any] | None) -> dict[str, str] | None:
    """The subprocess environment for a check: the parent's, plus ``identity``.

    ``identity`` maps a :data:`CHECK_ENV_KEYS` name to its value; a None value
    becomes the empty string. Keys not in ``identity`` are left as inherited.
    ``None`` in returns ``None`` out, which :func:`subprocess.run` reads as
    "inherit" — the pre-identity behaviour, byte-identical.
    """
    if identity is None:
        return None
    env = dict(os.environ)
    for key, value in identity.items():
        env[key] = "" if value is None else str(value)
    return env

# Secret shapes redacted from the persisted output tails before they reach
# output.json. Real check commands echo connection strings and tokens when
# they fail — a storage account key survived in a run record for the life of
# the record (observed in a field deployment on Windows). Case-insensitive on
# purpose, and each replacement names its label so a reviewer can tell *what
# kind* of secret was there without learning the secret. Grading always runs
# on the raw output first: redaction covers what is persisted, never what is
# graded — a verdict that changed because the evidence was masked would be a
# new bug.
REDACTION_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (label, re.compile(pattern, re.IGNORECASE))
    for label, pattern in (
        ("account-key", r"AccountKey=[^;\s]+"),
        ("sas-sig", r"sig=[A-Za-z0-9%+/=]{16,}"),
        ("private-key",
         r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
        ("jwt", r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}"),
        ("bearer-token", r"bearer\s+[A-Za-z0-9._~+/=-]{16,}"),
    )
]


def redact_output(text: str, extra_patterns: tuple[str, ...] = ()) -> str:
    """Apply the builtin redactions, then a check's own ``redact`` patterns.

    ``extra_patterns`` were validated compilable at spec load; a pattern that
    somehow fails here anyway is skipped rather than allowed to take down the
    checker — losing one custom redaction is recoverable, losing the verdict
    is not.
    """
    for label, pattern in REDACTION_PATTERNS:
        text = pattern.sub(f"[REDACTED:{label}]", text)
    for raw in extra_patterns:
        try:
            text = re.sub(raw, "[REDACTED:custom]", text)
        except re.error:
            continue
    return text


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
    # The exact command the runner executed — string form verbatim, argv form
    # as the list — because a persisted verdict whose command lives only in a
    # since-edited spec or a vanished intent sidecar is unauditable (asked for
    # from a field deployment on Windows). Redaction does NOT cover this
    # field: it masks output tails, so a secret typed into a check's command
    # line persists verbatim in the local output.json. It stays local —
    # telemetry excludes check commands for the same reason it excludes check
    # ids (operator-authored and path-like). Additive: None on older records.
    cmd: str | list[str] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CheckReport:
    results: list[CheckResult] = field(default_factory=list)
    run_id: str | None = None
    spec_sha256: str | None = None
    # Hash of the whole checks tree (spec bytes + scripts under
    # .fleetproof/checks/) that governed this verdict. Additive: None on older
    # records, and a None is never drift-tested.
    tree_sha256: str | None = None
    # Which tier's checks this verdict covers; None means "every check in the
    # spec" (the v0.1 behaviour). Additive — older records omit it entirely.
    tier: str | None = None
    # The directory every check in this report executed from — on the verdict
    # itself because a cwd surprise (a hook inheriting a shell that cd'd away)
    # produces a wall of false reds that is undiagnosable without it. Additive:
    # None on older records and on reports built without running.
    cwd: str | None = None
    # The arming state that governed this verdict, stamped by the gate that
    # ran it: {"tier": <tier graded>, "state": "armed"|"advisory"|"n/a",
    # "note": <disarm note or None>}. Every bridge run in the field wrote
    # ``advisory: false`` while the bridge gate was disarmed, because arming
    # was applied in the gate and never persisted — six months on, ``verdict:
    # fail, blocking_failed: 3`` in output.json reads as a gate that fired,
    # and only a mutable, history-less arming.json knew otherwise (observed in
    # a field deployment on Windows). ``state: "n/a"`` is the bare CLI: no
    # gate, so no arming governed anything. Additive: None on older records.
    arming: dict[str, Any] | None = None
    # Checks that were selected for this tier but retired instead of run —
    # succeeded by a check that passed (this session, or this very run), or
    # retired by an operator's `phase advance`. Each entry: {"id", "reason",
    # "succeeded_by", "note", "blocking"}. Never in ``results``, so never in
    # ``total``, ``failed``, or ``blocking_failures``: a retired check is not
    # a failed one. Additive: [] on older records. See :mod:`fleetproof.phase`.
    retired: list[dict[str, Any]] = field(default_factory=list)
    # Blocking checks whose grader control's PASS sample was still
    # pending-capture when this report ran (see
    # :func:`fleetproof.controls.pending_pass_checks`) — stamped by the
    # subagent gate so the evidence record says the capture obligation was
    # outstanding at verdict time. Never affects the verdict; the verified
    # transition warns. Additive: [] on older records and on reports built
    # without the stamp.
    controls_pending_capture: list[str] = field(default_factory=list)

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

    @property
    def all_advisory(self) -> bool:
        """True when checks ran and none of them could have failed the verdict.

        Such a verdict certifies nothing — 'pass' on it is vacuously true, and
        rendering the bare word teaches operators that PASS can mean 'nothing
        was at stake' (observed in a field deployment on Windows). The empty
        report stays outside this: zero selected checks is an absent grade,
        which is its own condition.
        """
        return self.total > 0 and not any(r.blocking for r in self.results)

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            # Sibling flag, not a third verdict value: consumers keyed on
            # pass/fail keep working, and an all-advisory pass is markable.
            # ``all_advisory`` is the current key — the bare word also names
            # an arming state and a dispatch verdict in this same record, so
            # the flag says what it flags. ``advisory`` is the 0.4.0 key,
            # dual-written for one release; remove in 0.7.0.
            "all_advisory": self.all_advisory,
            "advisory": self.all_advisory,
            # SHA-256 of the checks.json that governed this verdict. Additive:
            # records written before this field existed simply omit it, and every
            # reader treats a missing value as None (no hash on record).
            "spec_sha256": self.spec_sha256,
            "tree_sha256": self.tree_sha256,
            "tier": self.tier,
            "cwd": self.cwd,
            "arming": self.arming,
            "retired": list(self.retired),
            "controls_pending_capture": list(self.controls_pending_capture),
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


def _run_command(
    command: str | list[str], cwd: Path, timeout: int,
    env: dict[str, str] | None = None,
) -> tuple[int | None, str, str]:
    """Execute a check command in a separate process. Returns (returncode, stdout, stderr).

    String form runs through the platform shell (v0.1 compatibility — every
    existing spec is a shell line); argv form (a list) runs with ``shell=False``,
    no cmd.exe involvement, same timeout and encoding handling. A timeout or
    spawn failure yields a None returncode, which every grader treats as a
    failure — the tool never lets an unrunnable check silently pass.
    """
    try:
        proc = subprocess.run(
            command,
            shell=isinstance(command, str),
            cwd=str(cwd),
            capture_output=True,
            text=True,
            # Explicit encoding: without it, undecodable output (one emoji on a
            # cp1252 console) raised inside .communicate(), wiping the evidence
            # tail while the check still passed — and false-failing regex checks.
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
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
        # Exit code gates the match (finding H1): a failing command whose *error
        # text* happens to contain the pattern — e.g. a tool echoing
        # "ALL_TESTS_PASSED is not a flag" — must not read as a pass.
        if returncode != 0:
            return False, f"exit {returncode} (expected 0); output not consulted for /{pattern}/"
        combined = f"{stdout}\n{stderr}"
        ok = re.search(pattern, combined) is not None
        return ok, ("matched" if ok else "no match") + f" for /{pattern}/"

    if kind == "file_exists":
        raw_path = check.expect["path"]
        # The command (when there is one) must have completed and succeeded
        # (finding C2): an artifact left over from an earlier run must not pass a
        # check whose command could not even launch.
        if check.run is not None:
            if returncode is None:
                return False, f"command did not complete; not consulting {raw_path}"
            if returncode != 0:
                return False, f"exit {returncode} (expected 0); not consulting {raw_path}"
        declared = Path(raw_path)
        if declared.is_absolute():
            return False, f"absolute path not allowed: {raw_path}"
        target = (cwd / declared).resolve()
        try:
            target.relative_to(cwd.resolve())
        except ValueError:
            return False, f"path escapes the run directory: {raw_path}"
        if not target.exists():
            return False, f"missing: {raw_path}"
        if target.is_dir():
            return False, f"is a directory, expected a file: {raw_path}"
        if target.stat().st_size == 0:
            return False, f"empty file: {raw_path}"
        return True, f"present: {raw_path}"

    return False, f"unknown expectation kind: {kind}"


def run_check(check: Check, cwd: Path, timeout: int = DEFAULT_TIMEOUT_S,
              env: dict[str, str] | None = None) -> CheckResult:
    """Execute and grade a single check in its own subprocess.

    ``env`` is the full subprocess environment (see :func:`check_env`); None
    inherits the checker's own.
    """
    import time as _time
    started = _time.perf_counter()
    returncode: int | None = None
    stdout = stderr = ""
    if check.run is not None:
        returncode, stdout, stderr = _run_command(check.run, cwd, timeout, env)
    passed, detail = _grade(check, returncode, stdout, stderr, cwd)
    duration_ms = (_time.perf_counter() - started) * 1000.0
    # Redact before truncating, so a secret straddling the truncation boundary
    # cannot leave half of itself behind in the kept tail.
    return CheckResult(
        id=check.id,
        expectation=check.describe_expectation(),
        passed=passed,
        blocking=check.block,
        returncode=returncode,
        detail=detail,
        duration_ms=round(duration_ms, 3),
        stdout_tail=_tail(redact_output(stdout, check.redact)),
        stderr_tail=_tail(redact_output(stderr, check.redact)),
        cmd=check.run,
    )


def _apply_ownership(result: CheckResult, check: Check, tier: str | None) -> CheckResult:
    """Demote an owned check to advisory when graded away from its owner's seat.

    A blocking check with an ``owner`` is only a *blocking* check at the
    owner's own tier; selected anywhere else it still runs — the criterion
    stays visible — but its failure grades ``warn`` and cannot fail the
    verdict. A check only the coordinator (or the operator, who is not an
    agent tier at all) can satisfy wedged the lane it was assigned to through
    an unwinnable retry loop (observed in a field deployment on Windows).
    A run with no tier at all (the bare CLI's full-spec run) is the operator's
    own surface, and the operator is the one seat every owner answers to, so
    nothing is demoted there.
    """
    if check.owner is None or tier is None or check.owner == tier:
        return result
    result.blocking = False
    if not result.passed:
        result.detail += f"; owner: {check.owner} — advisory at tier {tier}"
    return result


def select_checks(checks: list[Check], tier: str | None) -> list[Check]:
    """The subset of ``checks`` that governs ``tier``.

    ``tier=None`` selects everything, exactly as v0.1 did — the Stop hook passes
    no tier and must keep grading the whole spec. With a tier given, a check is
    selected when its own ``tier`` matches, plus every *untiered* check at every
    tier. Untiered-fires-everywhere is the C1 fix (adversarial pass): scoping
    untiered checks to the bridge alone made the default subagent gate select
    nothing — a gate that looked alive (the bridge fired) while grading no
    subagent at all. A *tiered* check still fires only at its own rung: a
    leaf-tier check never gates the bridge, and narrowing a check to one rung
    remains an explicit act.
    """
    if tier is None:
        return list(checks)
    if tier not in VALID_TIERS:
        raise ValueError(unknown_tier_message(tier))
    return [c for c in checks if c.tier == tier or c.tier is None]


def run_checks(
    checks: list[Check] | None = None,
    *,
    cwd: Path | None = None,
    timeout: int = DEFAULT_TIMEOUT_S,
    record_to_log: bool = True,
    spec_path: Path | None = None,
    tier: str | None = None,
    identity: dict[str, Any] | None = None,
    arming: dict[str, Any] | None = None,
    retired: list[dict[str, Any]] | None = None,
    controls_pending: list[str] | None = None,
) -> CheckReport:
    """Run every selected check and (by default) append the verdict to the run log.

    ``checks`` defaults to the loaded ``.fleetproof/checks.json``. ``cwd`` defaults
    to the *project root* resolved from the current working directory — not the
    cwd itself, because the checker usually runs inside a hook that inherits
    whatever directory the working shell happened to be in, and a shell that
    cd'd into a subdirectory turned every relative path in the spec into a
    false red (observed in a field deployment on Windows). The spec's relative
    paths are written against the repo, so the repo root is what they mean.
    ``spec_path`` is the check-spec file whose bytes get hashed onto the verdict;
    when omitted it is resolved from ``cwd`` the same way the checker itself finds
    the spec, so the recorded hash always describes the spec that governed the run.
    ``tier`` narrows which checks run (see :func:`select_checks`); omitting it runs
    them all, which is what every v0.1 caller gets.

    A tier with no matching checks yields an empty report, whose verdict is "pass"
    because nothing was declared to fail — the recorded total of 0 is what makes
    that visible rather than silent.

    ``identity`` is the dispatch identity every check's subprocess sees in its
    environment (:data:`CHECK_ENV_KEYS`; see :func:`check_env`). The gates
    pass the graded dispatch's run id, agent type, tier, and session; the
    bare CLI passes tier and session only; omitting it inherits the checker's
    own environment unchanged.

    ``arming`` is stamped verbatim onto the persisted record (see
    :attr:`CheckReport.arming`): the gate that ran the checks says which
    tier's arming governed the verdict and whether it was armed or advisory,
    so the evidence record is interpretable without the arming file as it
    was at the time.

    ``retired`` is what the caller already retired before running (see
    :func:`fleetproof.phase.apply_phase`): stamped onto the report so the
    verdict lists them. On top of that, a check whose ``succeeded_by``
    successor PASSED in this very run is retired here after grading — its
    result moves out of ``results`` into ``retired`` — so the phase swap
    costs no extra blocked stop while the session evidence catches up.

    ``controls_pending`` is what the caller already read off the control
    ledger before running (see
    :func:`fleetproof.controls.pending_pass_checks`): stamped verbatim onto
    the persisted record, never consulted for the verdict.
    """
    if checks is None:
        checks = load_checks(spec_path)
    checks = select_checks(checks, tier)
    work_dir = Path(cwd) if cwd is not None else project_root(Path.cwd())
    resolved_spec = Path(spec_path) if spec_path is not None else default_checks_path(work_dir)
    sha = spec_hash(resolved_spec)

    report = CheckReport(spec_sha256=sha, tree_sha256=checks_tree_hash(resolved_spec),
                         tier=tier, cwd=str(work_dir), arming=arming,
                         retired=list(retired or []),
                         controls_pending_capture=list(controls_pending or []))
    env = check_env(identity)
    if not record_to_log:
        for check in checks:
            report.results.append(
                _apply_ownership(run_check(check, work_dir, timeout, env), check, tier))
        _retire_succeeded_this_run(report, checks)
        return report

    with record("fleetproof", "check", {"check_count": len(checks), "tier": tier}) as handle:
        report.run_id = handle.run_id
        for check in checks:
            report.results.append(
                _apply_ownership(run_check(check, work_dir, timeout, env), check, tier))
        _retire_succeeded_this_run(report, checks)
        payload = report.to_dict()
        payload["recorded_from_pid"] = _self_pid()
        handle.set_output(payload)
    return report


def _retire_succeeded_this_run(report: CheckReport, checks: list[Check]) -> None:
    """Move a predecessor's result into ``retired`` when its successor passed
    in this report. Transitive, like :func:`fleetproof.phase.apply_phase`:
    A -> B -> C with C passed retires B and then A. A successor that merely
    ran and failed retires nothing — the predecessor's own result stands."""
    by_id = {c.id: c for c in checks}
    passed = {r.id for r in report.results if r.passed}
    succeeded: set[str] = set()
    changed = True
    while changed:
        changed = False
        for check in checks:
            successor = check.succeeded_by
            if not successor or check.id in succeeded:
                continue
            if successor not in by_id:
                continue
            if successor in passed or successor in succeeded:
                succeeded.add(check.id)
                changed = True
    if not succeeded:
        return
    kept = []
    for result in report.results:
        if result.id in succeeded:
            successor = by_id[result.id].succeeded_by
            report.retired.append({
                "id": result.id,
                "reason": f"succeeded by {successor} (passed this run)",
                "succeeded_by": successor,
                "note": None,
                "blocking": result.blocking,
            })
        else:
            kept.append(result)
    report.results = kept


def _session_baseline(session_id: str | None, key: str) -> str | None:
    """The ``key`` hash of the EARLIEST recorded checker verdict in ``session_id``.

    Legacy verdicts with no hash under ``key`` are skipped, so the baseline is
    the earliest verdict that actually carries one. Returns None when there is
    no session context or no hashed verdict yet — in which case there is
    nothing to drift *from*, so callers treat it as no drift.
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
            sha = payload.get(key)
            if sha:
                order_key = sub.started_at or run.started_at or ""
                hashed.append((order_key, sha))
    if not hashed:
        return None
    hashed.sort(key=lambda item: item[0])
    return hashed[0][1]


def session_spec_baseline(session_id: str | None) -> str | None:
    """The spec hash every later verdict in the same session is compared
    against: if a verdict's own spec hash differs from this, the checks.json
    was edited mid-session (spec drift)."""
    return _session_baseline(session_id, "spec_sha256")


def session_tree_baseline(session_id: str | None) -> str | None:
    """The checks-tree sibling of :func:`session_spec_baseline`: catches a
    check *script* rewritten mid-session even when checks.json itself is
    byte-identical. Verdicts recorded before the tree hash existed carry none
    and are skipped — an old session is not retroactively drift-tested."""
    return _session_baseline(session_id, "tree_sha256")


def spec_drifted(current_hash: str | None, session_id: str | None) -> tuple[bool, str | None]:
    """Return ``(drifted, baseline_hash)`` for a verdict's hash within a session.

    ``drifted`` is True only when a session baseline exists and the current hash
    differs from it — a v0.1 pass-with-drift still passes, this just flags it.
    """
    baseline = session_spec_baseline(session_id)
    drifted = bool(baseline and current_hash and baseline != current_hash)
    return drifted, baseline


def tree_drifted(current_hash: str | None, session_id: str | None) -> tuple[bool, str | None]:
    """Return ``(drifted, baseline_hash)`` for a verdict's checks-tree hash.

    Same posture as :func:`spec_drifted`: True only when a session tree
    baseline exists and the current hash differs — either hash absent (legacy
    records, unreadable tree) means no drift verdict can honestly be made.
    """
    baseline = session_tree_baseline(session_id)
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
    # Retired checks render, and render as retired — a check that stepped
    # aside must be visible on the verdict and never look like a failure.
    for entry in report.retired:
        lines.append(f"  [retired] {entry.get('id')}: retired ({entry.get('reason')})")
    s = report.to_dict()["summary"]
    if report.total == 0 and report.retired:
        lines.append(
            f"RETIRED - 0 run; {len(report.retired)} selected check(s) retired "
            "(see [retired] lines).")
    elif report.all_advisory:
        # Never the bare word PASS here: with zero blocking checks nothing
        # could have failed this verdict, and the line must say so.
        lines.append(
            f"ADVISORY - 0 blocking; {s['total']} advisory check(s), "
            f"{s['passed']} passed."
        )
    else:
        lines.append(
            f"{report.verdict.upper()} - {s['passed']}/{s['total']} passed, "
            f"{s['blocking_failed']} blocking failure(s)."
        )
    lines.append(f"spec: {short_spec_hash(report.spec_sha256)}")
    # Where the checks ran: the one fact that separates a real red from a
    # cwd surprise. Omitted only on reports that never executed anything.
    if report.cwd:
        lines.append(f"cwd: {report.cwd}")
    # Only surfaced when scoped, so untiered (v0.1) output is byte-identical.
    if report.tier:
        lines.append(f"tier: {report.tier}")
    # Only surfaced when a gate disarmed it: an armed gate is the default and
    # the bare CLI has no gate, so neither adds a line.
    stamp = format_arming_stamp(report.arming)
    if stamp and (report.arming or {}).get("state") == "advisory":
        lines.append(stamp)
    return "\n".join(lines)


def format_arming_stamp(arming: Any) -> str | None:
    """One line rendering a persisted arming stamp, or None when absent.

    Tolerates a non-dict (a poisoned record must not crash the reader).
    """
    if not isinstance(arming, dict):
        return None
    tier = arming.get("tier") or "?"
    state = arming.get("state") or "?"
    note = arming.get("note")
    line = f"arming: {tier} gate {state}"
    if note:
        line += f" ({note})"
    return line


if __name__ == "__main__":  # pragma: no cover - manual smoke entry
    rep = run_checks()
    print(format_report_text(rep))
    sys.exit(1 if rep.verdict == "fail" else 0)
