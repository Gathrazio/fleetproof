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


# === dispatch ledger verbs ===

@pytest.fixture
def cli_runs(tmp_path, monkeypatch):
    """Isolated runs dir + no inherited run-id env, for the dispatch verbs."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(runlog.RUN_ID_ENV, raising=False)
    monkeypatch.delenv(runlog.PARENT_RUN_ID_ENV, raising=False)
    monkeypatch.delenv(runlog.SESSION_ID_ENV, raising=False)
    rd = tmp_path / "runs"
    runlog.set_runs_dir(rd)
    return rd


def _dispatch_new(capsys, *extra) -> str:
    assert main(["dispatch", "new", "--prompt", "do the work", *extra]) == 0
    return capsys.readouterr().out.strip()


def test_dispatch_new_prints_run_id(cli_runs, capsys):
    run_id = _dispatch_new(capsys)
    assert (cli_runs / run_id / "dispatch.json").exists()


def test_dispatch_new_json_prints_full_record(cli_runs, capsys):
    assert main(["dispatch", "new", "--prompt", "do the work", "--tier", "leaf",
                 "--format", "json"]) == 0
    record = json.loads(capsys.readouterr().out)
    assert record["tier"] == "leaf"
    assert record["tier_source"] == "declared"
    assert record["state"] == "dispatched"


def test_dispatch_new_requires_exactly_one_prompt_source(cli_runs, tmp_path, capsys):
    assert main(["dispatch", "new"]) == 2
    pf = tmp_path / "prompt.txt"
    pf.write_text("from a file", encoding="utf-8")
    assert main(["dispatch", "new", "--prompt", "x", "--prompt-file", str(pf)]) == 2


def test_dispatch_new_reads_prompt_file(cli_runs, tmp_path, capsys):
    pf = tmp_path / "prompt.txt"
    pf.write_text("multi\nline\nprompt", encoding="utf-8")
    assert main(["dispatch", "new", "--prompt-file", str(pf)]) == 0
    run_id = capsys.readouterr().out.strip()
    from fleetproof.ledger import load_dispatch
    assert load_dispatch(run_id).prompt == "multi\nline\nprompt"


def test_dispatch_new_with_manifest_file(cli_runs, tmp_path, capsys):
    mf = tmp_path / "manifest.json"
    mf.write_text(json.dumps({"deliverables": ["a"], "notes": "n"}), encoding="utf-8")
    assert main(["dispatch", "new", "--prompt", "work", "--manifest", str(mf),
                 "--format", "json"]) == 0
    record = json.loads(capsys.readouterr().out)
    assert record["manifest"]["deliverables"] == ["a"]
    assert record["manifest"]["notes"] == "n"


def test_dispatch_new_bad_manifest_file(cli_runs, tmp_path, capsys):
    mf = tmp_path / "manifest.json"
    mf.write_text("{not json", encoding="utf-8")
    assert main(["dispatch", "new", "--prompt", "work", "--manifest", str(mf)]) == 2
    assert main(["dispatch", "new", "--prompt", "work", "--manifest",
                 str(tmp_path / "absent.json")]) == 2


def test_dispatch_report_and_close(cli_runs, tmp_path, capsys):
    run_id = _dispatch_new(capsys)
    rf = tmp_path / "report.json"
    rf.write_text(json.dumps({
        "summary": "done",
        "deliverables": [{"name": "x", "confidence": 0.8, "evidence": "executed",
                          "pointer": None}],
    }), encoding="utf-8")
    assert main(["dispatch", "report", run_id, "--report", str(rf)]) == 0
    assert "reported" in capsys.readouterr().out
    assert (cli_runs / run_id / "report.json").exists()
    assert main(["dispatch", "close", run_id]) == 0
    assert "terminated" in capsys.readouterr().out
    # Nothing follows termination, and the CLI says so instead of pretending.
    assert main(["dispatch", "close", run_id]) == 1


def test_dispatch_report_rejects_empty_report(cli_runs, tmp_path, capsys):
    run_id = _dispatch_new(capsys)
    rf = tmp_path / "report.json"
    rf.write_text(json.dumps({"summary": "", "deliverables": []}), encoding="utf-8")
    assert main(["dispatch", "report", run_id, "--report", str(rf)]) == 1


def test_dispatch_report_unknown_run(cli_runs, tmp_path, capsys):
    rf = tmp_path / "report.json"
    rf.write_text(json.dumps({"summary": "done"}), encoding="utf-8")
    assert main(["dispatch", "report", "no-such-run", "--report", str(rf)]) == 1
    assert main(["dispatch", "report", "no-such-run", "--report",
                 str(tmp_path / "absent.json")]) == 2


def test_fleet_board_is_plain_ascii(cli_runs, capsys):
    open_id = _dispatch_new(capsys)
    dead_id = _dispatch_new(capsys)
    assert main(["dispatch", "close", dead_id]) == 0
    capsys.readouterr()

    assert main(["fleet"]) == 0
    out = capsys.readouterr().out
    assert open_id in out and dead_id in out
    assert "dispatched" in out and "terminated" in out
    # cp1252 consoles: the board must encode without a UnicodeEncodeError.
    out.encode("cp1252")

    assert main(["fleet", "--open"]) == 0
    open_out = capsys.readouterr().out
    assert open_id in open_out and dead_id not in open_out


def test_fleet_empty_and_json_and_session_filter(cli_runs, monkeypatch, capsys):
    assert main(["fleet"]) == 0
    assert "No dispatches found." in capsys.readouterr().out

    monkeypatch.setenv(runlog.SESSION_ID_ENV, "sess-a")
    mine = _dispatch_new(capsys)
    monkeypatch.setenv(runlog.SESSION_ID_ENV, "sess-b")
    theirs = _dispatch_new(capsys)

    assert main(["fleet", "--session", "sess-a", "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [d["run_id"] for d in payload["dispatches"]] == [mine]
    assert payload["dispatches"][0]["age"]
    assert theirs != mine
