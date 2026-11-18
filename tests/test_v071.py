"""0.7.1 feature tests: stale-dispatch escalation in the bridge stop's ledger
context. Field provenance: three times in one deployment a dispatched lane went
idle after an interim message, nothing re-invoked anyone, and the wave stalled
invisibly until a HUMAN noticed (GCC sessions 0064/0067/0070, lesson L55, third
recurrence operator-caught). The passive `[still in fleet]` line read
identically for a working agent and a dead one; these tests pin the loud form —
and pin that it stays context, never a block."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from fleetproof import runlog
from fleetproof.cli import main
from fleetproof.config import STALE_DISPATCH_DEFAULT_MINUTES, stale_dispatch_minutes
from fleetproof.hookgate import _ledger_context, _silence_minutes, stop_gate
from fleetproof.ledger import DispatchRecord, create_dispatch, record_report

_PASS = {"id": "ok", "run": f'"{sys.executable}" -c "raise SystemExit(0)"',
         "expect": "exit0", "block": True}

_NOW = datetime(2026, 11, 17, 12, 0, 0, tzinfo=timezone.utc)


def _setup_project(tmp_path: Path, checks: list[dict], monkeypatch) -> None:
    marker = tmp_path / ".fleetproof"
    marker.mkdir(exist_ok=True)
    (marker / "checks.json").write_text(json.dumps({"checks": checks}), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    runlog.set_runs_dir(marker / "runs")


@pytest.fixture(autouse=True)
def _reset_runs():
    yield
    runlog.set_runs_dir(None)
    import os
    os.environ.pop(runlog.SESSION_ID_ENV, None)


def _record(run_id="d-1", transitions=None, started_at=None) -> DispatchRecord:
    return DispatchRecord(
        run_id=run_id, run_dir=Path("."), parent_run_id=None, session_id="s",
        prompt="p", tier="lane", tier_source="declared",
        transitions=transitions if transitions is not None else [],
        started_at=started_at)


def _iso(minutes_ago: int, now: datetime = _NOW) -> str:
    return (now - timedelta(minutes=minutes_ago)).isoformat()


def _age_on_disk(run_id: str, minutes_ago: int) -> None:
    """Rewrite every transition stamp on a stored dispatch to the given age."""
    path = runlog.runs_dir() / run_id / "dispatch.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    stamp = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()
    for t in raw["transitions"]:
        t["at"] = stamp
    path.write_text(json.dumps(raw), encoding="utf-8")


# === Age math ===

def test_silence_age_reads_the_last_transition_stamp():
    r = _record(transitions=[
        {"state": "dispatched", "at": _iso(90)},
        {"state": "reported", "at": _iso(40)},
    ])
    assert _silence_minutes(r, now=_NOW) == 40


def test_silence_age_falls_back_to_started_at_on_empty_transitions():
    # Damaged-record convention: empty transitions already read back as
    # `dispatched` for state; the age read extends the same tolerance.
    r = _record(transitions=[], started_at=_iso(25))
    assert _silence_minutes(r, now=_NOW) == 25


def test_record_with_no_stamp_at_all_has_unknown_age_and_never_escalates(tmp_runs):
    r = _record(transitions=[], started_at=None)
    assert _silence_minutes(r, now=_NOW) is None
    context = _ledger_context([], [], [r], now=_NOW)
    assert "[still in fleet] d-1" in context
    assert "STALE" not in context


def test_unparseable_stamp_reads_as_unknown_not_stale():
    r = _record(transitions=[{"state": "dispatched", "at": "not-a-date"}],
                started_at=None)
    assert _silence_minutes(r, now=_NOW) is None


def test_future_stamp_clamps_to_zero_not_negative():
    r = _record(transitions=[{"state": "dispatched", "at": _iso(-5)}])
    assert _silence_minutes(r, now=_NOW) == 0


# === Threshold boundary and the escalated line ===

def test_in_fleet_at_the_threshold_is_not_yet_stale(tmp_runs):
    r = _record(transitions=[{"state": "dispatched",
                              "at": _iso(STALE_DISPATCH_DEFAULT_MINUTES)}])
    context = _ledger_context([], [], [r], now=_NOW)
    assert "[still in fleet] d-1: dispatched, no report yet" in context
    assert "STALE" not in context


def test_in_fleet_past_the_threshold_renders_the_escalated_line(tmp_runs):
    r = _record(transitions=[{"state": "dispatched", "at": _iso(37)}])
    context = _ledger_context([], [], [r], now=_NOW)
    assert ("- [STALE in fleet] d-1: dispatched, no ledger event for 37 min "
            "(threshold 15) — check the agent now: verify deliverables on "
            "disk, nudge it, or take the work over") in context
    assert "[still in fleet]" not in context


def test_stalled_and_retry_lines_carry_the_silence_age(tmp_runs):
    stalled = _record(run_id="d-s",
                      transitions=[{"state": "reported", "at": _iso(7)}])
    retrying = _record(run_id="d-r",
                       transitions=[{"state": "contradicted", "at": _iso(3)}])
    context = _ledger_context([stalled], [retrying], [], now=_NOW)
    assert "- [stalled] d-s: reported but never terminated (silent 7 min)" in context
    assert ("- [in retry] d-r: contradicted, awaiting the agent's fixed "
            "report (silent 3 min)") in context


# === Config key: stale_dispatch_minutes ===

def test_threshold_defaults_to_fifteen_and_reads_overrides():
    assert STALE_DISPATCH_DEFAULT_MINUTES == 15
    assert stale_dispatch_minutes({}) == 15
    assert stale_dispatch_minutes({"stale_dispatch_minutes": 30}) == 30
    # Explicit 0 / negative / false = the operator turned the escalation off.
    assert stale_dispatch_minutes({"stale_dispatch_minutes": 0}) == 0
    assert stale_dispatch_minutes({"stale_dispatch_minutes": -5}) == 0
    assert stale_dispatch_minutes({"stale_dispatch_minutes": False}) == 0
    # Malformed values fall back to the default rather than crashing a hook
    # or silently disabling the one line that interrupts a stalled wave.
    assert stale_dispatch_minutes({"stale_dispatch_minutes": "soon"}) == 15
    assert stale_dispatch_minutes({"stale_dispatch_minutes": True}) == 15


def test_zero_threshold_disables_the_escalation(tmp_path, monkeypatch):
    _setup_project(tmp_path, [_PASS], monkeypatch)
    (tmp_path / ".fleetproof" / "config.json").write_text(
        json.dumps({"stale_dispatch_minutes": 0}), encoding="utf-8")
    r = _record(transitions=[{"state": "dispatched", "at": _iso(100000)}])
    context = _ledger_context([], [], [r], now=_NOW)
    assert "[still in fleet] d-1" in context
    assert "STALE" not in context


# === Through the bridge stop gate: loud, never a block ===

def test_stop_gate_escalates_a_stale_dispatch_without_blocking(tmp_path, monkeypatch):
    _setup_project(tmp_path, [_PASS], monkeypatch)
    monkeypatch.setenv(runlog.SESSION_ID_ENV, "sess-stale")
    run_id = create_dispatch("quiet lane", tier="lane")
    _age_on_disk(run_id, minutes_ago=45)

    decision, code = stop_gate()
    assert code == 0
    assert decision is not None
    # The invariant the module's own comments defend: in-fleet dispatches are
    # context, never a block — stale makes the context louder, not blocking.
    assert "decision" not in decision
    context = decision["hookSpecificOutput"]["additionalContext"]
    assert f"[STALE in fleet] {run_id}" in context
    assert "(threshold 15)" in context
    assert "check the agent now" in context


def test_stop_gate_keeps_the_plain_line_for_a_fresh_dispatch(tmp_path, monkeypatch):
    _setup_project(tmp_path, [_PASS], monkeypatch)
    monkeypatch.setenv(runlog.SESSION_ID_ENV, "sess-fresh")
    run_id = create_dispatch("busy lane", tier="lane")

    decision, code = stop_gate()
    assert code == 0
    context = decision["hookSpecificOutput"]["additionalContext"]
    assert f"[still in fleet] {run_id}: dispatched, no report yet" in context
    assert "STALE" not in context


def test_stale_dispatch_exit_semantics_match_fresh_exactly(tmp_path, monkeypatch):
    # Byte-for-byte on the decision keys: staleness changes the words in the
    # context, and nothing else about the stop.
    _setup_project(tmp_path, [_PASS], monkeypatch)
    monkeypatch.setenv(runlog.SESSION_ID_ENV, "sess-keys")
    run_id = create_dispatch("quiet lane", tier="lane")
    fresh_decision, fresh_code = stop_gate()
    _age_on_disk(run_id, minutes_ago=400)
    stale_decision, stale_code = stop_gate()
    assert (fresh_code, set(fresh_decision)) == (stale_code, set(stale_decision))
    assert "decision" not in stale_decision


# === Board: the silent column ===

def test_fleet_board_shows_silence_age_for_open_rows(tmp_path, monkeypatch, capsys):
    _setup_project(tmp_path, [_PASS], monkeypatch)
    run_id = create_dispatch("p", tier="lane", session_id="sess-board")
    _age_on_disk(run_id, minutes_ago=17)
    capsys.readouterr()
    assert main(["fleet", "--open", "--all"]) == 0
    out = capsys.readouterr().out
    assert "silent" in out
    row = next(line for line in out.splitlines() if run_id in line)
    assert "17m" in row


def test_fleet_json_nulls_silence_on_terminal_rows(tmp_path, monkeypatch, capsys):
    # A terminal row has nothing to be silent about.
    from fleetproof.ledger import close_dispatch
    _setup_project(tmp_path, [_PASS], monkeypatch)
    run_id = create_dispatch("p", tier="lane", session_id="sess-term")
    record_report(run_id, {"summary": "done"})
    close_dispatch(run_id)
    capsys.readouterr()
    assert main(["fleet", "--all", "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    record = next(d for d in payload["dispatches"] if d["run_id"] == run_id)
    assert record["silent_age"] is None


def test_fleet_json_carries_silent_age(tmp_path, monkeypatch, capsys):
    _setup_project(tmp_path, [_PASS], monkeypatch)
    create_dispatch("p", tier="lane", session_id="sess-json")
    capsys.readouterr()
    assert main(["fleet", "--all", "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["dispatches"][0]["silent_age"]
