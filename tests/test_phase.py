"""Tests for phase succession: succeeded_by evidence, phase.json, and the
retired rendering. The gate-level behaviour is in test_hookgate.py."""

from __future__ import annotations

import json
import sys

import pytest

from fleetproof import runlog
from fleetproof.checker import format_report_text, run_checks
from fleetproof.checks import Check
from fleetproof.phase import (
    apply_phase,
    load_phase,
    phase_path,
    reset_phase,
    retire_checks,
    session_passed_check_ids,
)


def _check(cid, exit_code=0, succeeded_by=None, tier=None, block=True):
    return Check(id=cid, run=[sys.executable, "-c", f"raise SystemExit({exit_code})"],
                 expect={"kind": "exit0"}, block=block, tier=tier,
                 succeeded_by=succeeded_by)


def test_phase_file_absent_reads_as_nothing_retired(tmp_runs):
    assert load_phase() == {"retired": {}, "set_at": None, "by": None}


def test_retire_and_reset_round_trip_with_attribution(tmp_runs):
    path = retire_checks(["worktree-ahead"], "merge landed at 14:20", by="bridge")
    assert path == phase_path() == tmp_runs.parent / "phase.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert set(raw) == {"retired", "set_at", "by"}
    assert raw["by"] == "bridge"
    assert set(raw["retired"]["worktree-ahead"]) == {"at", "by", "note"}
    assert raw["retired"]["worktree-ahead"]["note"] == "merge landed at 14:20"
    # Additive over what is already there.
    retire_checks(["second"], "also over", by="bridge")
    assert set(load_phase()["retired"]) == {"worktree-ahead", "second"}
    reset_phase(by="bridge")
    state = load_phase()
    assert state["retired"] == {}
    assert state["by"] == "bridge"  # the reset itself is attributed


def test_retire_requires_a_note_and_an_id(tmp_runs):
    with pytest.raises(ValueError, match="non-empty --note"):
        retire_checks(["a"], "   ")
    with pytest.raises(ValueError, match="at least one --retire"):
        retire_checks([], "why")


def test_malformed_phase_file_retires_nothing(tmp_runs):
    phase_path().parent.mkdir(parents=True, exist_ok=True)
    phase_path().write_text("{not json", encoding="utf-8")
    assert load_phase()["retired"] == {}
    phase_path().write_text(json.dumps({"retired": {"a": "yes"}}), encoding="utf-8")
    assert load_phase()["retired"] == {}  # a non-object entry is not a retirement


def test_session_evidence_is_a_persisted_pass_in_the_same_session(tmp_runs, monkeypatch):
    monkeypatch.setenv(runlog.SESSION_ID_ENV, "sess-a")
    run_checks([_check("landed", 0), _check("other", 1)], cwd=tmp_runs.parent)
    assert session_passed_check_ids("sess-a") == {"landed"}
    assert session_passed_check_ids("sess-b") == set()
    assert session_passed_check_ids(None) == set()
    # An unrecorded run is not evidence.
    run_checks([_check("quiet", 0)], cwd=tmp_runs.parent, record_to_log=False)
    assert "quiet" not in session_passed_check_ids("sess-a")


def test_apply_phase_retires_by_succession_and_by_operator(tmp_runs):
    a = _check("a", 1, succeeded_by="b")
    b = _check("b", 0)
    c = _check("c", 1)
    # Nothing passed, nothing retired.
    active, retired = apply_phase([a, b, c], "s", phase={"retired": {}}, passed_ids=set())
    assert [x.id for x in active] == ["a", "b", "c"] and retired == []
    # Successor passed this session: the predecessor steps aside, in order.
    active, retired = apply_phase([a, b, c], "s", phase={"retired": {}}, passed_ids={"b"})
    assert [x.id for x in active] == ["b", "c"]
    assert retired == [{"id": "a", "reason": "succeeded by b", "succeeded_by": "b",
                        "note": None, "blocking": True}]
    # Operator retirement, by id, with the note carried.
    phase = {"retired": {"c": {"at": "t", "by": "ops", "note": "phase over"}}}
    active, retired = apply_phase([a, b, c], "s", phase=phase, passed_ids=set())
    assert [x.id for x in active] == ["a", "b"]
    assert retired[0]["reason"] == "phase advance: phase over"
    assert retired[0]["succeeded_by"] is None


def test_succession_is_transitive_but_operator_retirement_is_not(tmp_runs):
    a = _check("a", 1, succeeded_by="b")
    b = _check("b", 1, succeeded_by="c")
    c = _check("c", 0)
    active, retired = apply_phase([a, b, c], "s", phase={"retired": {}}, passed_ids={"c"})
    assert [x.id for x in active] == ["c"]
    assert [e["id"] for e in retired] == ["a", "b"]
    assert all(e["succeeded_by"] for e in retired)
    # b retired only by the operator: says nothing about the work a asserts.
    phase = {"retired": {"b": {"at": "t", "by": "ops", "note": "n"}}}
    active, retired = apply_phase([a, b, c], "s", phase=phase, passed_ids=set())
    assert [x.id for x in active] == ["a", "c"]
    assert retired[0]["succeeded_by"] is None


def test_successor_passing_in_the_same_run_retires_the_predecessor(tmp_runs):
    # No extra blocked stop: a FAIL on the predecessor becomes a retirement
    # when its successor passed in the report being graded.
    report = run_checks([_check("worktree-ahead", 1, succeeded_by="merge-landed"),
                         _check("merge-landed", 0)], cwd=tmp_runs.parent)
    assert report.verdict == "pass"
    assert [r.id for r in report.results] == ["merge-landed"]
    assert report.retired == [{"id": "worktree-ahead",
                               "reason": "succeeded by merge-landed (passed this run)",
                               "succeeded_by": "merge-landed", "note": None,
                               "blocking": True}]
    text = format_report_text(report)
    assert ("[retired] worktree-ahead: retired (succeeded by merge-landed "
            "(passed this run))") in text
    assert "FAIL" not in text
    assert "PASS - 1/1 passed, 0 blocking failure(s)." in text
    payload = report.to_dict()
    assert payload["summary"] == {"total": 1, "passed": 1, "failed": 0, "blocking_failed": 0}
    assert payload["retired"][0]["id"] == "worktree-ahead"


def test_successor_failing_in_the_same_run_retires_nothing(tmp_runs):
    report = run_checks([_check("a", 1, succeeded_by="b"), _check("b", 1)],
                        cwd=tmp_runs.parent)
    assert report.verdict == "fail"
    assert [r.id for r in report.blocking_failures] == ["a", "b"]
    assert report.retired == []


def test_all_retired_report_renders_retired_not_pass(tmp_runs):
    a = _check("a", 1)
    _, retired = apply_phase([a], "s", phase={"retired": {"a": {"note": "over"}}},
                             passed_ids=set())
    report = run_checks([], cwd=tmp_runs.parent, retired=retired)
    text = format_report_text(report)
    assert "[retired] a: retired (phase advance: over)" in text
    assert "RETIRED - 0 run; 1 selected check(s) retired" in text
    assert "PASS" not in text
