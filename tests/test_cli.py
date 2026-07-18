"""End-to-end tests for the argparse CLI."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from fleetproof import runlog
from fleetproof.cli import main


@pytest.fixture(autouse=True)
def _reset_runs():
    yield
    runlog.set_runs_dir(None)


def test_init_writes_spec(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    spec = tmp_path / "checks.json"
    assert main(["init", "--path", str(spec)]) == 0
    assert spec.exists()
    data = json.loads(spec.read_text(encoding="utf-8"))
    assert "checks" in data


def test_init_refuses_overwrite(tmp_path, capsys):
    spec = tmp_path / "checks.json"
    spec.write_text("{}", encoding="utf-8")
    assert main(["init", "--path", str(spec)]) == 1


def test_check_passing_spec_exits_zero(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    runlog.set_runs_dir(tmp_path / "runs")
    spec = tmp_path / "checks.json"
    spec.write_text(json.dumps({"checks": [
        {"id": "ok", "run": f'"{sys.executable}" -c "raise SystemExit(0)"', "expect": "exit0"},
    ]}), encoding="utf-8")
    rc = main(["check", "--spec", str(spec), "--format", "json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["verdict"] == "pass"


def test_check_failing_spec_exits_one(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    runlog.set_runs_dir(tmp_path / "runs")
    spec = tmp_path / "checks.json"
    spec.write_text(json.dumps({"checks": [
        {"id": "bad", "run": f'"{sys.executable}" -c "raise SystemExit(1)"', "expect": "exit0"},
    ]}), encoding="utf-8")
    assert main(["check", "--spec", str(spec)]) == 1


def test_report_command_writes_html(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    runlog.set_runs_dir(tmp_path / "runs")
    out = tmp_path / "r.html"
    assert main(["report", "-o", str(out)]) == 0
    assert out.exists()
