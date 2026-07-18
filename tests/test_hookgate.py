"""Tests for the Stop-hook decision logic."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from fleetproof import runlog
from fleetproof.hookgate import stop_gate


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
