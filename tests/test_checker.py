"""Tests for the independent checker runner — including the separate-process property."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from fleetproof.checker import (
    run_check,
    run_checks,
    select_checks,
    session_spec_baseline,
    spec_drifted,
)
from fleetproof.checks import Check, load_checks, spec_hash
from fleetproof.runlog import list_run_records


def _check(cid, run=None, expect=None, block=True, tier=None):
    return Check(id=cid, run=run, expect=expect or {"kind": "exit0"}, block=block, tier=tier)


def test_exit0_pass_and_fail(tmp_project):
    ok = run_check(_check("ok", run=f'"{sys.executable}" -c "raise SystemExit(0)"'), tmp_project)
    bad = run_check(_check("bad", run=f'"{sys.executable}" -c "raise SystemExit(0)"',
                           expect={"kind": "exit", "code": 5}), tmp_project)
    assert ok.passed is True
    assert bad.passed is False


def test_exit_specific_code(tmp_project):
    r = run_check(
        _check("e3", run=f'"{sys.executable}" -c "raise SystemExit(3)"',
               expect={"kind": "exit", "code": 3}),
        tmp_project,
    )
    assert r.passed is True and r.returncode == 3


def test_regex_match(tmp_project):
    r = run_check(
        _check("re", run=f'"{sys.executable}" -c "print(\'hello world\')"',
               expect={"kind": "regex", "pattern": "hello"}),
        tmp_project,
    )
    assert r.passed is True


def test_file_exists(tmp_project):
    (tmp_project / "artifact.txt").write_text("x", encoding="utf-8")
    present = run_check(_check("f1", expect={"kind": "file_exists", "path": "artifact.txt"}), tmp_project)
    absent = run_check(_check("f2", expect={"kind": "file_exists", "path": "nope.txt"}), tmp_project)
    assert present.passed is True
    assert absent.passed is False


def test_unrunnable_check_is_a_failure_not_a_pass(tmp_project):
    # A command that cannot complete must never be graded as passing.
    r = run_check(_check("hang", run=f'"{sys.executable}" -c "import time; time.sleep(5)"'),
                  tmp_project, timeout=1)
    assert r.passed is False
    assert r.returncode is None


def test_checker_runs_command_in_a_separate_process(tmp_project):
    # The independence guarantee, made observable: the check command runs in a
    # different OS process than the runner/test process.
    script = tmp_project / "emit_pid.py"
    script.write_text(
        "import os, pathlib; pathlib.Path('child_pid.txt').write_text(str(os.getpid()))",
        encoding="utf-8",
    )
    check = _check("pid", run=f'"{sys.executable}" emit_pid.py')
    report = run_checks([check], cwd=tmp_project, record_to_log=False)
    assert report.verdict == "pass"
    child_pid = int((tmp_project / "child_pid.txt").read_text(encoding="utf-8"))
    assert child_pid != os.getpid()


def test_verdict_blocking_vs_nonblocking(tmp_project):
    checks = [
        _check("blocking-fail", run=f'"{sys.executable}" -c "raise SystemExit(1)"', block=True),
        _check("advisory-fail", run=f'"{sys.executable}" -c "raise SystemExit(1)"', block=False),
    ]
    report = run_checks(checks, cwd=tmp_project, record_to_log=False)
    assert report.verdict == "fail"
    assert len(report.blocking_failures) == 1

    only_advisory = run_checks(
        [_check("advisory", run=f'"{sys.executable}" -c "raise SystemExit(1)"', block=False)],
        cwd=tmp_project, record_to_log=False,
    )
    # A non-blocking failure does not gate "done".
    assert only_advisory.verdict == "pass"
    assert only_advisory.failed == 1


def test_run_checks_records_verdict_to_log(tmp_runs, tmp_project):
    checks = [_check("ok", run=f'"{sys.executable}" -c "raise SystemExit(0)"')]
    report = run_checks(checks, cwd=tmp_project, record_to_log=True)
    runs = list_run_records()
    assert len(runs) == 1
    check_subs = [s for s in runs[0].sub_invocations if s.tool == "fleetproof" and s.subcmd == "check"]
    assert len(check_subs) == 1
    payload = check_subs[0].load_output()
    assert payload["verdict"] == "pass"
    assert payload["checks"][0]["id"] == "ok"
    # The recorder captured the checker's own pid — evidence it recorded from a real process.
    assert "recorded_from_pid" in payload


def _write_spec(path: Path, description: str = "") -> None:
    path.write_text(json.dumps({"checks": [
        {"id": "ok", "run": f'"{sys.executable}" -c "raise SystemExit(0)"',
         "expect": "exit0", "description": description},
    ]}), encoding="utf-8")


def test_run_checks_records_spec_hash(tmp_runs, tmp_path):
    # The verdict must carry the SHA-256 of the spec bytes that governed it, both
    # on the in-memory report and in the recorded output.json.
    spec = tmp_path / "checks.json"
    _write_spec(spec)
    report = run_checks(load_checks(spec), cwd=tmp_path, record_to_log=True, spec_path=spec)
    assert report.spec_sha256 == spec_hash(spec)
    payload = list_run_records()[0].sub_invocations[0].load_output()
    assert payload["spec_sha256"] == spec_hash(spec)


def test_spec_baseline_and_drift_within_session(tmp_runs, tmp_path, monkeypatch):
    # Two verdicts in one session, spec edited between them: the baseline is the
    # first verdict's hash, and the second is flagged as drifted against it.
    from fleetproof import runlog
    spec = tmp_path / "checks.json"
    monkeypatch.setenv(runlog.SESSION_ID_ENV, "sess-1")
    monkeypatch.setenv(runlog.RUN_ID_ENV, "20260101-000001-aaaaaa")
    _write_spec(spec, description="original")
    first = run_checks(load_checks(spec), cwd=tmp_path, record_to_log=True, spec_path=spec)

    monkeypatch.setenv(runlog.RUN_ID_ENV, "20260101-000002-bbbbbb")
    _write_spec(spec, description="weakened")
    second = run_checks(load_checks(spec), cwd=tmp_path, record_to_log=True, spec_path=spec)

    assert first.spec_sha256 != second.spec_sha256
    assert session_spec_baseline("sess-1") == first.spec_sha256
    drifted, baseline = spec_drifted(second.spec_sha256, "sess-1")
    assert drifted is True
    assert baseline == first.spec_sha256
    # The first verdict is the baseline, so it is not itself drift.
    assert spec_drifted(first.spec_sha256, "sess-1")[0] is False


# === tier scoping (additive; v0.1 callers pass no tier) ===

def _tiered_set():
    return [
        _check("untiered"),
        _check("leaf-check", tier="leaf"),
        _check("lane-check", tier="lane"),
        _check("coordinator-check", tier="coordinator"),
        _check("bridge-check", tier="bridge"),
    ]


# === advisory verdict rendering ===

_OK = f'"{sys.executable}" -c "raise SystemExit(0)"'
_BAD = f'"{sys.executable}" -c "raise SystemExit(1)"'


def test_all_advisory_report_renders_advisory_not_pass(tmp_project):
    # 'PASS - 0/8 passed' when every check is block:false is vacuous: nothing
    # could have failed the verdict, so the word PASS certifies nothing
    # (observed in a field deployment on Windows). Say what it is.
    from fleetproof.checker import format_report_text
    report = run_checks(
        [_check("a1", run=_OK, block=False), _check("a2", run=_BAD, block=False)],
        cwd=tmp_project, record_to_log=False)
    assert report.all_advisory is True
    # JSON compatibility: the verdict field itself does not change vocabulary.
    payload = report.to_dict()
    assert payload["verdict"] == "pass"
    assert payload["advisory"] is True
    text = format_report_text(report)
    assert "ADVISORY - 0 blocking; 2 advisory check(s), 1 passed" in text
    assert "PASS -" not in text


def test_report_with_any_blocking_check_is_not_advisory(tmp_project):
    from fleetproof.checker import format_report_text
    report = run_checks(
        [_check("b1", run=_OK, block=True), _check("a1", run=_OK, block=False)],
        cwd=tmp_project, record_to_log=False)
    assert report.all_advisory is False
    assert report.to_dict()["advisory"] is False
    assert "PASS -" in format_report_text(report)


def test_empty_report_is_not_advisory(tmp_project):
    # Zero selected checks is its own condition (an absent grade), not an
    # advisory one; the 0-total PASS line stays as the visible trace of it.
    report = run_checks([], cwd=tmp_project, record_to_log=False)
    assert report.all_advisory is False


def test_no_tier_selects_everything_v01_behaviour():
    assert [c.id for c in select_checks(_tiered_set(), None)] == [
        "untiered", "leaf-check", "lane-check", "coordinator-check", "bridge-check",
    ]


def test_bridge_tier_includes_untiered_checks():
    # An untiered spec is a bridge-tier spec by definition, so a v0.1 spec keeps
    # gating the bridge once tiers exist.
    assert [c.id for c in select_checks(_tiered_set(), "bridge")] == [
        "untiered", "bridge-check",
    ]


def test_untiered_checks_fire_at_every_tier():
    # The C1 fix: scoping untiered checks to the bridge alone made the default
    # subagent gate select nothing. Untiered now fires everywhere; a *tiered*
    # check still fires only at its own rung.
    assert [c.id for c in select_checks(_tiered_set(), "leaf")] == [
        "untiered", "leaf-check"]
    assert [c.id for c in select_checks(_tiered_set(), "lane")] == [
        "untiered", "lane-check"]
    assert [c.id for c in select_checks(_tiered_set(), "coordinator")] == [
        "untiered", "coordinator-check"]


def test_unknown_tier_rejected():
    with pytest.raises(ValueError):
        select_checks(_tiered_set(), "middle-management")


def test_run_checks_tier_filters_and_records_tier(tmp_runs, tmp_project):
    ok = f'"{sys.executable}" -c "raise SystemExit(0)"'
    bad = f'"{sys.executable}" -c "raise SystemExit(1)"'
    checks = [_check("leaf-ok", run=ok, tier="leaf"), _check("bridge-bad", run=bad)]

    # bridge-bad is untiered, so it fires at leaf too (the C1 fix) — the leaf
    # verdict fails on it alongside the leaf's own passing check.
    leaf = run_checks(checks, cwd=tmp_project, record_to_log=False, tier="leaf")
    assert [r.id for r in leaf.results] == ["leaf-ok", "bridge-bad"]
    assert leaf.verdict == "fail"
    assert leaf.tier == "leaf"

    bridge = run_checks(checks, cwd=tmp_project, record_to_log=False, tier="bridge")
    assert [r.id for r in bridge.results] == ["bridge-bad"]
    assert bridge.verdict == "fail"

    # Backward compatibility: no tier runs both, exactly as v0.1 did.
    every = run_checks(checks, cwd=tmp_project, record_to_log=False)
    assert [r.id for r in every.results] == ["leaf-ok", "bridge-bad"]
    assert every.tier is None

    recorded = run_checks(checks, cwd=tmp_project, record_to_log=True, tier="leaf")
    payload = list_run_records()[0].sub_invocations[0].load_output()
    assert payload["tier"] == "leaf"
    assert payload["summary"]["total"] == 2
    assert recorded.tier == "leaf"


def test_tier_with_no_matching_checks_is_an_empty_report(tmp_project):
    # Nothing declared for this tier: an empty report, whose zero total is what
    # makes "nothing was verified" visible rather than silent. (Requires an
    # explicitly tiered check — untiered would fire everywhere post-C1.)
    report = run_checks([_check("bridge-only", run="true", tier="bridge")],
                        cwd=tmp_project, record_to_log=False, tier="leaf")
    assert report.total == 0
    assert report.verdict == "pass"


def test_no_drift_without_session_context(tmp_runs, tmp_path):
    # No session id => no baseline to drift from => never flagged.
    spec = tmp_path / "checks.json"
    _write_spec(spec)
    report = run_checks(load_checks(spec), cwd=tmp_path, record_to_log=True, spec_path=spec)
    assert session_spec_baseline(None) is None
    assert spec_drifted(report.spec_sha256, None) == (False, None)
