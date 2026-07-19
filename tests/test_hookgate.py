"""Tests for the Stop-hook decision logic."""

from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path

import pytest

from fleetproof import runlog
from fleetproof.hookgate import record_tool_main, stop_gate


def _setup_project(tmp_path: Path, checks: list[dict], monkeypatch) -> None:
    marker = tmp_path / ".fleetproof"
    marker.mkdir()
    (marker / "checks.json").write_text(json.dumps({"checks": checks}), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    runlog.set_runs_dir(marker / "runs")


@pytest.fixture(autouse=True)
def _reset_runs():
    yield
    runlog.set_runs_dir(None)
    # _apply_session_id writes os.environ directly (mirroring the real hook
    # process), so clear it between tests rather than relying on monkeypatch.
    os.environ.pop(runlog.SESSION_ID_ENV, None)


def test_stop_gate_blocks_on_failing_blocking_check(tmp_path, monkeypatch):
    _setup_project(tmp_path, [
        {"id": "must-pass", "run": f'"{sys.executable}" -c "raise SystemExit(1)"',
         "expect": "exit0", "block": True},
    ], monkeypatch)
    decision, code = stop_gate()
    assert code == 0
    assert decision is not None
    assert decision["decision"] == "block"
    assert "must-pass" in decision["reason"]
    assert decision["hookSpecificOutput"]["hookEventName"] == "Stop"


def test_stop_gate_allows_when_all_pass(tmp_path, monkeypatch):
    _setup_project(tmp_path, [
        {"id": "ok", "run": f'"{sys.executable}" -c "raise SystemExit(0)"',
         "expect": "exit0", "block": True},
    ], monkeypatch)
    decision, code = stop_gate()
    assert decision is None
    assert code == 0


def test_stop_gate_allows_when_only_advisory_fails(tmp_path, monkeypatch):
    _setup_project(tmp_path, [
        {"id": "advisory", "run": f'"{sys.executable}" -c "raise SystemExit(1)"',
         "expect": "exit0", "block": False},
    ], monkeypatch)
    decision, code = stop_gate()
    assert decision is None


def test_stop_gate_fails_open_without_spec(tmp_path, monkeypatch):
    marker = tmp_path / ".fleetproof"
    marker.mkdir()
    monkeypatch.chdir(tmp_path)
    runlog.set_runs_dir(marker / "runs")
    decision, code = stop_gate()
    assert decision is None
    assert code == 0


def test_record_tool_captures_session_id_from_stdin(tmp_path, monkeypatch):
    # The PostToolUse recorder must lift session_id off the hook stdin payload
    # and stamp it onto the root record, so the report can group the session.
    marker = tmp_path / ".fleetproof"
    marker.mkdir()
    monkeypatch.chdir(tmp_path)
    runlog.set_runs_dir(marker / "runs")
    monkeypatch.delenv(runlog.RUN_ID_ENV, raising=False)
    monkeypatch.delenv(runlog.SESSION_ID_ENV, raising=False)

    payload = {
        "session_id": "sess-abc-123",
        "hook_event_name": "PostToolUse",
        "tool_name": "Edit",
        "tool_input": {"file_path": "x.py"},
        "tool_response": {"ok": True},
    }
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))

    assert record_tool_main() == 0

    runs = runlog.list_run_records()
    assert len(runs) == 1
    assert runs[0].session_id == "sess-abc-123"
    assert runs[0].root_tool == "claude-tool"


def test_record_tool_without_session_id_leaves_it_absent(tmp_path, monkeypatch):
    # A payload with no session_id (older Claude Code, or a malformed hook) must
    # still record — just ungrouped. Backward-compatible with pre-fix behavior.
    marker = tmp_path / ".fleetproof"
    marker.mkdir()
    monkeypatch.chdir(tmp_path)
    runlog.set_runs_dir(marker / "runs")
    monkeypatch.delenv(runlog.RUN_ID_ENV, raising=False)
    monkeypatch.delenv(runlog.SESSION_ID_ENV, raising=False)

    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"tool_name": "Bash"})))

    assert record_tool_main() == 0
    runs = runlog.list_run_records()
    assert len(runs) == 1
    assert runs[0].session_id is None


def _write_checks(tmp_path: Path, checks: list[dict]) -> None:
    (tmp_path / ".fleetproof" / "checks.json").write_text(
        json.dumps({"checks": checks}), encoding="utf-8"
    )


_PASS = {"id": "ok", "run": f'"{sys.executable}" -c "raise SystemExit(0)"', "expect": "exit0"}
_FAIL = {"id": "bad", "run": f'"{sys.executable}" -c "raise SystemExit(1)"', "expect": "exit0"}


def test_pass_with_spec_drift_still_passes_but_is_annotated(tmp_path, monkeypatch):
    from fleetproof.checks import SPEC_DRIFT_NOTE
    _setup_project(tmp_path, [dict(_PASS, description="original")], monkeypatch)
    monkeypatch.setenv(runlog.SESSION_ID_ENV, "sess-drift")

    monkeypatch.setenv(runlog.RUN_ID_ENV, "20260101-000001-aaaaaa")
    first, code = stop_gate()
    assert code == 0
    assert first is None  # first verdict is the baseline: passes silently

    # The agent edits its own checks.json mid-session (still passing, but changed).
    monkeypatch.setenv(runlog.RUN_ID_ENV, "20260101-000002-bbbbbb")
    _write_checks(tmp_path, [dict(_PASS, description="weakened")])
    second, code = stop_gate()
    assert code == 0
    # A pass with drift must NOT block...
    assert second is not None
    assert second.get("decision") != "block"
    # ...but must carry the unmissable drift annotation.
    ctx = second["hookSpecificOutput"]["additionalContext"]
    assert SPEC_DRIFT_NOTE in ctx


def test_block_reason_carries_drift_note(tmp_path, monkeypatch):
    from fleetproof.checks import SPEC_DRIFT_NOTE
    _setup_project(tmp_path, [dict(_PASS, description="original")], monkeypatch)
    monkeypatch.setenv(runlog.SESSION_ID_ENV, "sess-drift-2")

    monkeypatch.setenv(runlog.RUN_ID_ENV, "20260101-000001-cccccc")
    stop_gate()  # baseline

    # Agent swaps the spec to a failing blocking check after the baseline was set.
    monkeypatch.setenv(runlog.RUN_ID_ENV, "20260101-000002-dddddd")
    _write_checks(tmp_path, [_FAIL])
    decision, code = stop_gate()
    assert decision is not None
    assert decision["decision"] == "block"
    assert SPEC_DRIFT_NOTE in decision["reason"]
    assert SPEC_DRIFT_NOTE in decision["hookSpecificOutput"]["additionalContext"]


def test_no_false_drift_across_different_sessions(tmp_path, monkeypatch):
    _setup_project(tmp_path, [dict(_PASS, description="original")], monkeypatch)

    monkeypatch.setenv(runlog.SESSION_ID_ENV, "sess-A")
    monkeypatch.setenv(runlog.RUN_ID_ENV, "20260101-000001-eeeeee")
    stop_gate()

    # A different session with a different spec is not drift — each session has
    # its own baseline.
    monkeypatch.setenv(runlog.SESSION_ID_ENV, "sess-B")
    monkeypatch.setenv(runlog.RUN_ID_ENV, "20260101-000002-ffffff")
    _write_checks(tmp_path, [dict(_PASS, description="different-but-own-baseline")])
    decision, code = stop_gate()
    assert decision is None  # pass, no drift => silent
