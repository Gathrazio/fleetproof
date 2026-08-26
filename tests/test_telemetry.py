"""Tests for the verification-telemetry layer.

The outcome-class derivation gets one test per row of the closed nine-class
set, plus the adversarial transition sequences from the schema's red-team
passes as fixtures — the attacks that motivated `ungraded`, the
near-miss/flake split, and the null-never-guessed exposure rules are the
regressions this file pins.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from fleetproof import runlog
from fleetproof.checker import CheckReport, CheckResult
from fleetproof.checks import Check
from fleetproof.ledger import (
    close_dispatch,
    create_dispatch,
    load_dispatch,
    record_report,
    record_verdict,
)
from fleetproof.telemetry import (
    CLASS_CONTRADICTED,
    CLASS_NEAR_MISS,
    CLASS_SILENT_IDLE,
    CLASS_TERMINATED_UNCLASSIFIED,
    CLASS_TERMINATED_UNREPORTED,
    CLASS_UNGRADED,
    CLASS_UNVERIFIABLE,
    CLASS_VERIFIED,
    CLASS_VERIFIER_FLAKE,
    DERIVATION_VERSION,
    SCHEMA_VERSION,
    TelemetryError,
    build_telemetry,
    classify_action_class,
    classify_task_domain,
    derive_outcome_class,
    load_telemetry,
    loss_to_usd,
    record_failure_loss,
    severity_band_for_usd,
)


@pytest.fixture
def era_runs(tmp_runs, monkeypatch):
    """Isolated runs dir with a telemetry-era cutover already in force."""
    monkeypatch.delenv(runlog.PARENT_RUN_ID_ENV, raising=False)
    (tmp_runs.parent / "config.json").write_text(
        json.dumps({"telemetry_era": "2026-01-01"}), encoding="utf-8")
    return tmp_runs


def _report(summary="did the thing", deliverables=None):
    return {"summary": summary, "deliverables": deliverables or []}


def _check(check_id="c1", kind="exit0"):
    expect = {"kind": kind}
    if kind == "file_exists":
        expect = {"kind": "file_exists", "path": "out.txt"}
    return Check(id=check_id, run=None if kind == "file_exists" else "cmd",
                 expect=expect, block=True)


def _graded(check_ids=("c1",), kinds=None):
    """A CheckReport + checks list standing for a grading that executed."""
    kinds = kinds or ["exit0"] * len(check_ids)
    report = CheckReport(spec_sha256="ab" * 32, tier="lane")
    checks = []
    for cid, kind in zip(check_ids, kinds):
        checks.append(_check(cid, kind))
        report.results.append(CheckResult(
            id=cid, expectation="exit code 0", passed=True, blocking=True,
            returncode=0, detail="exit 0", duration_ms=1.0))
    return report, checks


def _empty_grading():
    """A grading that ran and selected zero checks."""
    return CheckReport(spec_sha256="ab" * 32, tier="lane"), []


# === §5.1: one test per derivation row ===

def test_class_verified(era_runs):
    run_id = create_dispatch("work", tier="lane")
    record_report(run_id, _report())
    record_verdict(run_id, "verified")
    close_dispatch(run_id)
    assert derive_outcome_class(load_dispatch(run_id)) == CLASS_VERIFIED


def test_class_near_miss_requires_a_changed_work_product(era_runs):
    run_id = create_dispatch("work", tier="lane")
    record_report(run_id, _report("broken attempt"))
    record_verdict(run_id, "contradicted")
    record_report(run_id, _report("fixed attempt"))
    record_verdict(run_id, "verified")
    close_dispatch(run_id)
    assert derive_outcome_class(load_dispatch(run_id)) == CLASS_NEAR_MISS


def test_class_verifier_flake_on_unchanged_work_product(era_runs):
    # RT-B S2-6: an unchanged retry that then passes means the contradiction,
    # not the work, was wrong — it must not contaminate near-miss frequency.
    run_id = create_dispatch("work", tier="lane")
    record_report(run_id, _report("same claim"))
    record_verdict(run_id, "contradicted")
    record_report(run_id, _report("same claim"))
    record_verdict(run_id, "verified")
    close_dispatch(run_id)
    assert derive_outcome_class(load_dispatch(run_id)) == CLASS_VERIFIER_FLAKE


def test_class_contradicted(era_runs):
    run_id = create_dispatch("work", tier="lane")
    record_report(run_id, _report())
    record_verdict(run_id, "contradicted")
    close_dispatch(run_id)
    assert derive_outcome_class(load_dispatch(run_id)) == CLASS_CONTRADICTED


def test_class_ungraded_when_grading_never_ran(era_runs):
    # RT-A S1-1 / RT-B S1-1, the convergent finding: reported, terminated, no
    # verdict, and no record that a checker ever ran — the class where the
    # gate's own failure modes land. Never bucketed into unverifiable.
    run_id = create_dispatch("work", tier="lane")
    record_report(run_id, _report())
    close_dispatch(run_id)
    assert derive_outcome_class(load_dispatch(run_id)) == CLASS_UNGRADED
    telemetry = build_telemetry(run_id)
    assert telemetry["outcome.class"] == CLASS_UNGRADED


def test_class_unverifiable_when_grading_ran_and_selected_nothing(era_runs):
    run_id = create_dispatch("work", tier="lane")
    record_report(run_id, _report())
    close_dispatch(run_id)
    report, checks = _empty_grading()
    telemetry = build_telemetry(run_id, check_report=report, checks=checks)
    assert telemetry["outcome.class"] == CLASS_UNVERIFIABLE
    assert telemetry["outcome.verifier"]["checks_run"] == 0


def test_class_silent_idle(era_runs):
    run_id = create_dispatch("work", tier="lane")
    close_dispatch(run_id, reason="sweep-idle")
    assert derive_outcome_class(load_dispatch(run_id)) == CLASS_SILENT_IDLE


def test_class_terminated_unreported(era_runs):
    for reason in ("operator-close", "session-end"):
        run_id = create_dispatch("work", tier="lane")
        close_dispatch(run_id, reason=reason)
        assert derive_outcome_class(load_dispatch(run_id)) == CLASS_TERMINATED_UNREPORTED


def test_class_terminated_unclassified_without_reason(era_runs):
    # Older records carry no reason; the split is underivable and must never
    # be guessed into one of its siblings.
    run_id = create_dispatch("work", tier="lane")
    close_dispatch(run_id)
    assert derive_outcome_class(load_dispatch(run_id)) == CLASS_TERMINATED_UNCLASSIFIED


# === derivation edges ===

def test_in_flight_dispatch_has_no_class_yet(era_runs):
    run_id = create_dispatch("work", tier="lane")
    assert derive_outcome_class(load_dispatch(run_id)) is None
    record_report(run_id, _report())
    assert derive_outcome_class(load_dispatch(run_id)) is None
    record_verdict(run_id, "contradicted")
    # A contradicted dispatch awaiting its retry is work in progress, not an
    # outcome.
    assert derive_outcome_class(load_dispatch(run_id)) is None


def test_retry_without_hashes_derives_flake_not_near_miss(era_runs):
    # A record from before per-attempt hashes cannot evidence a changed work
    # product, and near-miss is the metric with the strongest incentive to
    # inflate — the un-evidenced case lands on the self-critical side.
    run_id = create_dispatch("work", tier="lane")
    record_report(run_id, _report("first"))
    record_verdict(run_id, "contradicted")
    record_report(run_id, _report("second"))
    record_verdict(run_id, "verified")
    close_dispatch(run_id)
    record = load_dispatch(run_id)
    raw = json.loads((record.run_dir / "dispatch.json").read_text(encoding="utf-8"))
    for entry in raw["transitions"]:
        entry.pop("report_sha256", None)
    (record.run_dir / "dispatch.json").write_text(json.dumps(raw), encoding="utf-8")
    assert derive_outcome_class(load_dispatch(run_id)) == CLASS_VERIFIER_FLAKE


# === builder: era discipline, null-never-guessed, field spec ===

def test_pre_telemetry_dispatch_is_never_backfilled(tmp_runs, monkeypatch):
    monkeypatch.delenv(runlog.PARENT_RUN_ID_ENV, raising=False)
    run_id = create_dispatch("old work", tier="lane")
    record_report(run_id, _report())
    record_verdict(run_id, "verified")
    close_dispatch(run_id)
    assert build_telemetry(run_id) is None
    assert not (load_dispatch(run_id).run_dir / "telemetry.json").exists()


def test_builder_stamps_versions_and_identity(era_runs, monkeypatch):
    monkeypatch.setenv(runlog.SESSION_ID_ENV, "sess-t")
    run_id = create_dispatch("implement the fix", tier="lane")
    record_report(run_id, _report())
    record_verdict(run_id, "verified")
    close_dispatch(run_id)
    t = build_telemetry(run_id)
    assert t["schema_version"] == SCHEMA_VERSION
    assert t["fleetproof.derivation_version"] == DERIVATION_VERSION
    assert t["run_id"] == run_id
    assert t["session_id"] == "sess-t"
    assert t["tier"] == "lane"
    assert t["fleetproof.telemetry_era"] == "2026-01-01"
    assert t["fleetproof.run_context"] == "production"
    assert t["compliance.capture_mode"] == "all non-operator fields machine-captured"
    # Persisted, and identical on read-back.
    assert load_telemetry(run_id) == t


def test_run_context_comes_from_config(era_runs):
    (era_runs.parent / "config.json").write_text(json.dumps({
        "telemetry_era": "2026-01-01", "run_context": "drill",
        "deployment_id": "fleet-a", "operator_id": "op-1",
    }), encoding="utf-8")
    run_id = create_dispatch("drill work", tier="lane")
    close_dispatch(run_id, reason="operator-close")
    t = build_telemetry(run_id)
    assert t["fleetproof.run_context"] == "drill"
    assert t["deployment_id"] == "fleet-a"
    assert t["operator_id"] == "op-1"


def test_stop_only_capture_nulls_every_exposure_field(era_runs):
    # RT-A S2-4: a stop-only dispatch has no observation window; zeros there
    # are wrong-as-fact and corrupt every frequency denominator.
    run_id = create_dispatch(
        "work", tier="lane",
        agent={"agent_id": "a1", "agent_type": "tester", "capture": "stop-only"})
    record_report(run_id, _report())
    record_verdict(run_id, "verified")
    close_dispatch(run_id)
    t = build_telemetry(run_id)
    for key in ("exposure.tool_call_count", "exposure.tokens_in",
                "exposure.tokens_out", "exposure.elapsed_seconds",
                "exposure.files_touched", "exposure.retry_count",
                "exposure.value_at_stake"):
        assert t[key] is None, key
    assert t["agent.capture"] == "stop-only"
    assert t["outcome.class"] == CLASS_VERIFIED


def test_start_capture_records_elapsed_and_retries(era_runs):
    run_id = create_dispatch(
        "work", tier="lane",
        agent={"agent_id": "a1", "agent_type": "tester", "capture": "start"})
    record_report(run_id, _report("first"))
    record_verdict(run_id, "contradicted")
    record_report(run_id, _report("second"))
    record_verdict(run_id, "verified")
    close_dispatch(run_id)
    t = build_telemetry(run_id)
    assert t["exposure.retry_count"] == 1
    assert isinstance(t["exposure.elapsed_seconds"], float)
    # Unobservable exposure stays null even on a full start capture.
    assert t["exposure.tokens_in"] is None
    assert t["exposure.tool_call_count"] is None


def test_claimed_summary_reduces_the_report_to_numbers(era_runs):
    deliverables = [
        {"name": "a.py", "confidence": 0.9, "evidence": "executed"},
        {"name": "b.py", "confidence": 0.5, "evidence": "believed"},
        {"name": "c.py", "evidence": "executed"},
    ]
    run_id = create_dispatch("work", tier="lane")
    record_report(run_id, _report("claims", deliverables))
    record_verdict(run_id, "verified")
    close_dispatch(run_id)
    claimed = build_telemetry(run_id)["outcome.claimed"]
    assert claimed["deliverable_count"] == 3
    assert claimed["confidence_min"] == 0.5
    assert claimed["confidence_mean"] == pytest.approx(0.7)
    assert claimed["evidence_counts"] == {"executed": 2, "observed": 0, "believed": 1}


# === coverage (§3.5) ===

def test_coverage_over_union_of_manifest_and_claim(era_runs):
    manifest = {
        "deliverables": ["a.py", "b.py"],
        "check_map": {"a.py": ["check-a"], "b.py": ["check-b"], "c.py": ["check-c"]},
    }
    run_id = create_dispatch("work", tier="lane", manifest=manifest)
    # The agent claims a third deliverable the manifest never listed; the
    # denominator is the union, so shrinking the claim cannot ace the metric.
    record_report(run_id, _report("done", [{"name": "c.py", "evidence": "executed"}]))
    record_verdict(run_id, "verified")
    close_dispatch(run_id)
    report, checks = _graded(("check-a", "check-c"))
    t = build_telemetry(run_id, check_report=report, checks=checks)
    # a.py and c.py covered by executed checks; b.py's check never ran.
    assert t["outcome.coverage"] == pytest.approx(2 / 3)


def test_zero_deliverable_coverage_is_null_never_one(era_runs):
    # RT-A S1-5: an empty denominator is an absence of measurement, not a
    # perfect score.
    run_id = create_dispatch("work", tier="lane")
    record_report(run_id, _report("summary only, zero deliverables"))
    record_verdict(run_id, "verified")
    close_dispatch(run_id)
    report, checks = _graded(("c1",))
    t = build_telemetry(run_id, check_report=report, checks=checks)
    assert t["outcome.coverage"] is None


def test_agent_evidence_labels_play_no_role_in_coverage(era_runs):
    # RT-B S2-5: self-declared evidence: "executed" must not count as coverage.
    run_id = create_dispatch("work", tier="lane", manifest={
        "check_map": {"a.py": ["a-check-that-never-ran"]}})
    record_report(run_id, _report("done", [
        {"name": "a.py", "evidence": "executed"}]))
    record_verdict(run_id, "verified")
    close_dispatch(run_id)
    report, checks = _graded(("some-check",))
    # The map joins a.py to a check the checker never executed; the agent's
    # own "executed" label must not stand in for it.
    t = build_telemetry(run_id, check_report=report, checks=checks)
    assert t["outcome.coverage"] == 0.0


def test_unmapped_deliverables_make_coverage_null_not_zero(era_runs):
    # Deliverables exist but the manifest maps no checks to them: nothing was
    # measured, and an absent measurement is null — the same principle the
    # zero-deliverable case already followed, applied to the other half
    # (0.0 here read as "measured and found empty" in a field deployment).
    run_id = create_dispatch("work", tier="lane")
    record_report(run_id, _report("done", [
        {"name": "a.py", "evidence": "executed"}]))
    record_verdict(run_id, "verified")
    close_dispatch(run_id)
    report, checks = _graded(("some-check",))
    t = build_telemetry(run_id, check_report=report, checks=checks)
    assert t["outcome.coverage"] is None


def test_verifier_block_counts_kinds_but_never_ids(era_runs):
    run_id = create_dispatch("work", tier="lane")
    record_report(run_id, _report())
    record_verdict(run_id, "verified")
    close_dispatch(run_id)
    report, checks = _graded(("secret-path-check", "other"),
                             kinds=["exit0", "file_exists"])
    verifier = build_telemetry(run_id, check_report=report, checks=checks)["outcome.verifier"]
    assert verifier["checks_run"] == 2
    assert verifier["check_kind_counts"] == {"exit0": 1, "file_exists": 1}
    assert "secret-path-check" not in json.dumps(verifier)


# === severity (§5.2) ===

def test_band_edges_are_monotone_in_dollars():
    assert severity_band_for_usd(0) == "S0"
    assert severity_band_for_usd(99.99) == "S1"
    assert severity_band_for_usd(100) == "S2"
    assert severity_band_for_usd(999.99) == "S2"
    assert severity_band_for_usd(1000) == "S3"
    assert severity_band_for_usd(9999.99) == "S3"
    assert severity_band_for_usd(10000) == "S4"


def test_hours_convert_at_the_published_labor_rate():
    assert loss_to_usd(2, "hours") == 200.0
    assert loss_to_usd(150, "usd") == 150.0
    with pytest.raises(TelemetryError):
        loss_to_usd(1, "fortnights")


def _contradicted_run(era_runs):
    run_id = create_dispatch("work", tier="lane")
    record_report(run_id, _report())
    record_verdict(run_id, "contradicted")
    close_dispatch(run_id)
    build_telemetry(run_id)
    return run_id


def test_raw_loss_derives_band_and_estimated_loss(era_runs):
    run_id = _contradicted_run(era_runs)
    t = record_failure_loss(run_id, 3, "hours",
                            prompt_latency_seconds=4.2, default_accepted=False)
    assert t["failure.raw_loss"] == {"value": 3, "unit": "hours"}
    assert t["failure.severity_band"] == "S2"  # 3h * $100 = $300
    assert t["failure.estimated_loss"] == {
        "amount": 300.0, "currency": "USD", "method": "operator-hours@$100/hr"}
    assert t["failure.labor_rate_assumed"] == 100
    assert t["failure.prompt_latency_seconds"] == 4.2
    assert t["failure.default_accepted"] is False
    assert t["failure.incident_id"].startswith("inc-")


def test_band_stays_null_until_the_operator_answers(era_runs):
    # Null, never guessed: a contradicted run with no answer yet has no band.
    run_id = _contradicted_run(era_runs)
    t = load_telemetry(run_id)
    assert t["outcome.class"] == CLASS_CONTRADICTED
    assert t["failure.raw_loss"] is None
    assert t["failure.severity_band"] is None
    assert t["failure.severity_floor"] is not None


def test_operator_answer_cannot_undercut_the_machine_floor(era_runs):
    # RT-B S2-13: the retry chain's elapsed time is a measured lower bound on
    # rework; the one self-reported number may raise severity, never lower it.
    run_id = _contradicted_run(era_runs)
    record = load_dispatch(run_id)
    raw = json.loads((record.run_dir / "dispatch.json").read_text(encoding="utf-8"))
    # Manufacture a 2-hour retry chain: contradicted at T, "graded" at T+2h.
    base = datetime(2026, 2, 1, 12, 0, tzinfo=timezone.utc)
    for entry, offset in zip(raw["transitions"], (0, 1, 2, 7202)):
        entry["at"] = (base + timedelta(seconds=offset)).isoformat()
    raw["transitions"].insert(3, {
        "state": "reported", "at": (base + timedelta(seconds=3)).isoformat(),
        "by": "hook", "report_sha256": "x"})
    raw["transitions"].insert(4, {
        "state": "contradicted", "at": (base + timedelta(seconds=7200)).isoformat(),
        "by": "checker"})
    (record.run_dir / "dispatch.json").write_text(json.dumps(raw), encoding="utf-8")
    build_telemetry(run_id)
    # 2h * $100/hr = $200 -> floor S2. An operator answering "30 minutes"
    # ($50 -> S1) cannot pull the band below it.
    t = record_failure_loss(run_id, 0.5, "hours")
    assert t["failure.severity_floor"] == "S2"
    assert t["failure.severity_band"] == "S2"


def test_near_miss_without_loss_is_s0(era_runs):
    run_id = create_dispatch("work", tier="lane")
    record_report(run_id, _report("broken"))
    record_verdict(run_id, "contradicted")
    record_report(run_id, _report("fixed"))
    record_verdict(run_id, "verified")
    close_dispatch(run_id)
    t = build_telemetry(run_id)
    assert t["outcome.class"] == CLASS_NEAR_MISS
    assert t["failure.severity_band"] == "S0"


def test_loss_rejects_bad_values(era_runs):
    run_id = _contradicted_run(era_runs)
    with pytest.raises(TelemetryError):
        record_failure_loss(run_id, -1, "usd")
    with pytest.raises(TelemetryError):
        record_failure_loss(run_id, 1, "days")


# === oversight & preservation across rebuilds ===

def test_gate_blocks_become_hook_sourced_oversight_events(era_runs):
    run_id = create_dispatch("work", tier="lane")
    record_report(run_id, _report("first"))
    record_verdict(run_id, "contradicted")
    record_report(run_id, _report("second"))
    record_verdict(run_id, "verified")
    close_dispatch(run_id)
    events = build_telemetry(run_id)["oversight.events"]
    assert len(events) == 1
    assert events[0]["type"] == "intervention"
    assert events[0]["source"] == "hook"
    assert events[0]["accepted"] is True


def test_operator_events_and_answers_survive_a_rebuild(era_runs):
    run_id = _contradicted_run(era_runs)
    t = load_telemetry(run_id)
    t["oversight.events"].append(
        {"type": "override", "accepted": True, "at": "2026-02-01", "source": "operator"})
    t["failure.harm_type"] = "none"
    record = load_dispatch(run_id)
    (record.run_dir / "telemetry.json").write_text(json.dumps(t), encoding="utf-8")

    rebuilt = build_telemetry(run_id)
    assert {"type": "override", "accepted": True, "at": "2026-02-01",
            "source": "operator"} in rebuilt["oversight.events"]
    assert rebuilt["failure.harm_type"] == "none"


def test_verifier_block_survives_a_rebuild_without_fresh_grading(era_runs):
    run_id = create_dispatch("work", tier="lane")
    record_report(run_id, _report())
    record_verdict(run_id, "verified")
    close_dispatch(run_id)
    report, checks = _graded(("c1",))
    build_telemetry(run_id, check_report=report, checks=checks)
    rebuilt = build_telemetry(run_id)  # e.g. a later loss recording
    assert rebuilt["outcome.verifier"]["checks_run"] == 1


# === task classifiers (versioned, deliberately dumb) ===

def test_task_classifiers_are_deterministic_and_versioned(era_runs):
    assert classify_task_domain("refactor the ledger module") == "code"
    assert classify_task_domain("draft a memo for the team") == "document"
    assert classify_task_domain("juggle flaming torches") == "other"
    assert classify_action_class("fix the failing gate") == "modify"
    assert classify_action_class("juggle flaming torches") is None
    run_id = create_dispatch("refactor the ledger module", tier="lane")
    close_dispatch(run_id, reason="operator-close")
    t = build_telemetry(run_id)
    assert t["task.domain"] == "code"
    assert t["task.classifier_version"] == "fleetproof-task-heuristic/1"


# === hook-gate integration: telemetry written on the checker's side ===

def _hook_project(tmp_path, checks, monkeypatch, era=True):
    marker = tmp_path / ".fleetproof"
    marker.mkdir()
    (marker / "checks.json").write_text(json.dumps({"checks": checks}), encoding="utf-8")
    if era:
        (marker / "config.json").write_text(
            json.dumps({"telemetry_era": "2026-01-01"}), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    runlog.set_runs_dir(marker / "runs")
    monkeypatch.delenv(runlog.PARENT_RUN_ID_ENV, raising=False)
    monkeypatch.delenv(runlog.RUN_ID_ENV, raising=False)


@pytest.fixture(autouse=True)
def _reset_runs_dir():
    yield
    runlog.set_runs_dir(None)


def test_subagent_gate_writes_telemetry_on_verified_stop(tmp_path, monkeypatch):
    import sys as _sys
    from fleetproof.hookgate import capture_subagent_start, subagent_stop
    from fleetproof.ledger import list_dispatches
    _hook_project(tmp_path, [
        {"id": "ok", "run": f'"{_sys.executable}" -c "raise SystemExit(0)"',
         "expect": "exit0", "block": True},
    ], monkeypatch)
    capture_subagent_start({"agent_id": "a1", "agent_type": "tester"})
    decision, code = subagent_stop({
        "agent_id": "a1", "agent_type": "tester",
        "last_assistant_message": "Done."})
    assert decision is None and code == 0
    d = list_dispatches()[0]
    t = load_telemetry(d.run_id)
    assert t is not None
    assert t["outcome.class"] == CLASS_VERIFIED
    assert t["outcome.verifier"]["checks_run"] == 1


def test_subagent_gate_empty_selection_writes_unverifiable(tmp_path, monkeypatch):
    from fleetproof.hookgate import capture_subagent_start, subagent_stop
    from fleetproof.ledger import list_dispatches
    _hook_project(tmp_path, [
        {"id": "leaf-only", "run": None,
         "expect": {"file_exists": "x.txt"}, "block": True, "tier": "leaf"},
    ], monkeypatch)
    capture_subagent_start({"agent_id": "a1", "agent_type": "tester"})
    decision, code = subagent_stop({
        "agent_id": "a1", "agent_type": "tester",
        "last_assistant_message": "Done."})
    assert decision is None and code == 0
    d = list_dispatches()[0]
    t = load_telemetry(d.run_id)
    assert t["outcome.class"] == CLASS_UNVERIFIABLE
    assert t["outcome.verifier"]["checks_run"] == 0


def test_pre_telemetry_hook_stop_writes_no_telemetry(tmp_path, monkeypatch):
    import sys as _sys
    from fleetproof.hookgate import capture_subagent_start, subagent_stop
    from fleetproof.ledger import list_dispatches
    _hook_project(tmp_path, [
        {"id": "ok", "run": f'"{_sys.executable}" -c "raise SystemExit(0)"',
         "expect": "exit0", "block": True},
    ], monkeypatch, era=False)
    capture_subagent_start({"agent_id": "a1", "agent_type": "tester"})
    decision, code = subagent_stop({
        "agent_id": "a1", "agent_type": "tester",
        "last_assistant_message": "Done."})
    assert decision is None and code == 0
    d = list_dispatches()[0]
    assert not (d.run_dir / "telemetry.json").exists()


# === abandoned dispatches (B3): their own class, never verified ===

def test_class_abandoned(era_runs):
    from fleetproof.ledger import REASON_ABANDONED
    from fleetproof.telemetry import CLASS_ABANDONED
    run_id = create_dispatch("work", tier="lane")
    for attempt in range(3):
        record_report(run_id, _report(f"attempt {attempt}"))
        record_verdict(run_id, "contradicted")
    close_dispatch(run_id, by="hook", reason=REASON_ABANDONED)
    assert derive_outcome_class(load_dispatch(run_id)) == CLASS_ABANDONED
    # An abandoned dispatch is a failure on record: it gets the failure block
    # (incident id, machine severity floor), same as a contradicted one.
    telemetry = build_telemetry(run_id)
    assert telemetry["outcome.class"] == CLASS_ABANDONED
    assert telemetry["failure.incident_id"] is not None
    assert telemetry["failure.severity_floor"] is not None


def test_parked_before_reporting_reads_as_terminated_unreported(era_runs):
    from fleetproof.ledger import REASON_PARKED_PREFIX
    run_id = create_dispatch("work", tier="lane")
    close_dispatch(run_id, reason=REASON_PARKED_PREFIX + "blocked on operator")
    assert derive_outcome_class(load_dispatch(run_id)) == CLASS_TERMINATED_UNREPORTED


def test_parked_after_a_contradiction_stays_contradicted(era_runs):
    # Parking closes the bookkeeping; it must not soften the verdict. The one
    # deliberate exit is the structured --unsatisfiable flag (its own tests
    # below); a plain park — even one whose prose says "unsatisfiable" —
    # never reclasses, because derivation must not parse prose.
    from fleetproof.ledger import REASON_PARKED_PREFIX
    run_id = create_dispatch("work", tier="lane")
    record_report(run_id, _report())
    record_verdict(run_id, "contradicted")
    close_dispatch(run_id, reason=REASON_PARKED_PREFIX + "unsatisfiable from this seat")
    assert derive_outcome_class(load_dispatch(run_id)) == CLASS_CONTRADICTED


# === escalated (D1): the operator-flagged unsatisfiable park ===

def test_flagged_park_after_a_contradiction_classes_escalated(era_runs):
    # The lane refused work it could not satisfy, the gate blocked the stop,
    # and the operator agreed by parking with the flag. Escalating correctly
    # is neither a success nor a failure: no incident, no severity floor.
    from fleetproof.ledger import REASON_PARKED_PREFIX
    from fleetproof.telemetry import CLASS_ESCALATED
    run_id = create_dispatch("work", tier="lane")
    record_report(run_id, _report("cannot be satisfied from this seat"))
    record_verdict(run_id, "contradicted")
    close_dispatch(run_id, reason=REASON_PARKED_PREFIX + "unsatisfiable",
                   park_unsatisfiable=True)
    assert derive_outcome_class(load_dispatch(run_id)) == CLASS_ESCALATED
    telemetry = build_telemetry(run_id)
    assert telemetry["outcome.class"] == CLASS_ESCALATED
    assert telemetry["failure.incident_id"] is None
    assert telemetry["failure.severity_floor"] is None


def test_flagged_park_before_reporting_classes_escalated(era_runs):
    # The flag is the operator's structured signal, so it reclasses whether
    # or not a report ever landed — unflagged, this exact shape reads as
    # terminated_unreported (pinned above).
    from fleetproof.ledger import REASON_PARKED_PREFIX
    from fleetproof.telemetry import CLASS_ESCALATED
    run_id = create_dispatch("work", tier="lane")
    close_dispatch(run_id, reason=REASON_PARKED_PREFIX + "unsatisfiable",
                   park_unsatisfiable=True)
    assert derive_outcome_class(load_dispatch(run_id)) == CLASS_ESCALATED


def test_verified_verdict_wins_over_a_flagged_park(era_runs):
    # Work that passed its checks was satisfied, whatever the park said
    # afterwards — the flag must not be able to launder a verified outcome
    # out of the success column (or a success out of the record).
    from fleetproof.ledger import REASON_PARKED_PREFIX
    run_id = create_dispatch("work", tier="lane")
    record_report(run_id, _report())
    record_verdict(run_id, "verified")
    close_dispatch(run_id, reason=REASON_PARKED_PREFIX + "parked anyway",
                   park_unsatisfiable=True)
    assert derive_outcome_class(load_dispatch(run_id)) == CLASS_VERIFIED


def test_class_advisory_is_its_own_class_never_verified(era_runs):
    # A blocking failure under a disarmed gate: graded, failed, not blocked.
    # Not verified, not contradicted, not unverifiable; not a failure record.
    from fleetproof.telemetry import CLASS_ADVISORY
    run_id = create_dispatch("work", tier="coordinator")
    record_report(run_id, _report())
    record_verdict(run_id, "advisory", detail="1/1 blocking check(s) failed (x)")
    close_dispatch(run_id)
    record = load_dispatch(run_id)
    assert derive_outcome_class(record) == CLASS_ADVISORY
    assert record.verdict == "advisory"
    telemetry = build_telemetry(run_id, *_graded(("x",)))
    assert telemetry["outcome.class"] == CLASS_ADVISORY
    assert telemetry["failure.incident_id"] is None
    assert telemetry["fleetproof.derivation_version"] == 5
