"""Tests for the grader control ledger (.fleetproof/controls/)."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

from fleetproof import runlog
from fleetproof.checks import Check, checks_tree_hash
from fleetproof.controls import (
    CONTROL_SAMPLE_ENV,
    ControlError,
    control_path,
    control_warnings,
    controls_are_outside_the_checks_tree,
    controls_dir,
    load_control,
    record_control,
)


@pytest.fixture
def project(tmp_path, monkeypatch):
    marker = tmp_path / ".fleetproof"
    marker.mkdir()
    (marker / "checks.json").write_text(json.dumps({"checks": []}), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    runlog.set_runs_dir(marker / "runs")
    yield tmp_path
    runlog.set_runs_dir(None)


# A check that grades the file named by FLEETPROOF_CONTROL_SAMPLE: passes
# when it holds the real field name, fails otherwise.
_SAMPLE_GRADER = Check(
    id="tcn-health", block=True, expect={"kind": "exit0"},
    run=[sys.executable, "-c",
         "import json, os, sys; d = json.load(open(os.environ['" + CONTROL_SAMPLE_ENV + "']));"
         " sys.exit(0 if isinstance(d.get('days_remaining'), (int, float)) else 5)"])


def _sample(tmp_path: Path, name: str, payload: dict) -> Path:
    p = tmp_path / name
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


def test_record_control_hashes_samples_and_observes_both_directions(project):
    good = _sample(project, "captured.json", {"checked": True, "days_remaining": 67.1})
    bad = _sample(project, "expired.json", {"checked": True, "days_remaining": None})
    record = record_control(
        "tcn-health", pass_sample=good, fail_sample=bad, provenance="captured",
        note="captured from DEBTNET-DEV:4800/health 2026-08-25", check=_SAMPLE_GRADER,
        check_source="manifest", cwd=project, by="bridge")
    path = control_path("tcn-health")
    assert path == controls_dir() / "tcn-health.json" and path.exists()
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk == record
    assert record["provenance"] == "captured"
    assert record["by"] == "bridge" and record["recorded_at"]
    assert record["pass_sample"]["sha256"] == hashlib.sha256(good.read_bytes()).hexdigest()
    assert record["pass_sample"]["path"] == str(good.resolve())
    assert record["pass_sample"]["observed_exit"] == 0
    assert record["pass_sample"]["observed_pass"] is True
    assert record["pass_sample"]["agrees"] is True
    assert record["fail_sample"]["observed_exit"] == 5
    assert record["fail_sample"]["observed_pass"] is False
    assert record["fail_sample"]["agrees"] is True
    assert record["cmd"] == _SAMPLE_GRADER.run
    assert record["sample_env"] == CONTROL_SAMPLE_ENV


def test_a_disagreeing_direction_is_recorded_not_hidden(project):
    # The field's exact mistake: the "pass" sample carries the wrong field
    # name, so the grader fails it. The record says so; it does not refuse.
    wrong = _sample(project, "authored.json", {"checked": True, "days_until_expiry": 67})
    record = record_control("tcn-health", pass_sample=wrong, provenance="authored",
                            check=_SAMPLE_GRADER, cwd=project)
    assert record["pass_sample"]["observed_pass"] is False
    assert record["pass_sample"]["agrees"] is False
    assert record["fail_sample"] is None


def test_record_without_a_resolvable_check_observes_nothing(project):
    good = _sample(project, "s.json", {})
    record = record_control("unknown-check", pass_sample=good, provenance="captured",
                            check=None, cwd=project)
    assert record["pass_sample"]["observed_exit"] is None
    assert record["pass_sample"]["agrees"] is None
    assert record["cmd"] is None


def test_record_control_rejects_bad_inputs(project):
    good = _sample(project, "s.json", {})
    with pytest.raises(ControlError, match="cannot name a control file"):
        record_control("../evil", pass_sample=good, provenance="captured")
    with pytest.raises(ControlError, match="provenance must be one of"):
        record_control("ok", pass_sample=good, provenance="guessed")
    with pytest.raises(ControlError, match="is not a file"):
        record_control("ok", pass_sample=project / "absent.json", provenance="captured")
    assert control_path("a/b") is None
    assert load_control("never-recorded") is None


def test_controls_live_outside_the_hashed_checks_tree(project):
    # A control is evidence about a grader, not a grader: recording one must
    # trip no drift pin.
    assert controls_are_outside_the_checks_tree()
    before = checks_tree_hash()
    record_control("tcn-health", pass_sample=_sample(project, "s.json", {}),
                   provenance="captured")
    assert checks_tree_hash() == before


def test_control_warnings_name_uncontrolled_and_authored_blocking_checks(project):
    good = _sample(project, "s.json", {"days_remaining": 1})
    record_control("captured-ok", pass_sample=good, provenance="captured")
    record_control("authored-only", pass_sample=good, provenance="authored")
    checks = [
        Check(id="captured-ok", run="x", expect={"kind": "exit0"}, block=True),
        Check(id="authored-only", run="x", expect={"kind": "exit0"}, block=True),
        Check(id="uncontrolled", run="x", expect={"kind": "exit0"}, block=True),
        Check(id="advisory-uncontrolled", run="x", expect={"kind": "exit0"}, block=False),
    ]
    warnings = control_warnings(checks)
    assert len(warnings) == 2
    assert warnings[0].startswith("blocking check 'authored-only': its only pass sample is authored")
    assert warnings[1].startswith("blocking check 'uncontrolled' has no grader control")
    assert "fleetproof check control uncontrolled --pass-sample" in warnings[1]
