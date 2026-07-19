"""Smoke tests for the HTML report generator."""

from __future__ import annotations

import sys
from pathlib import Path

import json

from fleetproof import runlog
from fleetproof.checker import run_checks
from fleetproof.checks import Check, load_checks
from fleetproof.report import build_report, write_report
from fleetproof.runlog import list_run_records, record


def _check(cid, run, expect=None, block=True):
    return Check(id=cid, run=run, expect=expect or {"kind": "exit0"}, block=block)


def test_report_empty_is_self_contained(tmp_runs):
    html = build_report([])
    assert html.startswith("<!DOCTYPE html>")
    assert "No runs recorded yet." in html
    # Self-contained: no external asset references.
    assert "http://" not in html and "https://" not in html
    assert "<script" not in html.lower()


def test_report_marks_contradicted_run(tmp_runs, tmp_path):
    # A run whose independent checker verdict is fail must render as 'contradicted'.
    run_checks([_check("bad", run=f'"{sys.executable}" -c "raise SystemExit(1)"')],
               cwd=tmp_path, record_to_log=True)
    html = build_report()
    assert "contradicted" in html
    assert "Claim contradicted" in html


def test_report_marks_verified_run(tmp_runs, tmp_path):
    run_checks([_check("good", run=f'"{sys.executable}" -c "raise SystemExit(0)"')],
               cwd=tmp_path, record_to_log=True)
    html = build_report()
    assert "verified" in html


def test_report_marks_unverified_run(tmp_runs):
    with record("some-tool", "do"):
        pass
    html = build_report()
    assert "unverified" in html


def test_write_report_creates_file(tmp_runs, tmp_path):
    out = tmp_path / "out" / "report.html"
    written = write_report(out, [])
    assert written.exists()
    assert written.read_text(encoding="utf-8").startswith("<!DOCTYPE html>")


def test_report_groups_runs_by_session(tmp_runs, monkeypatch):
    # Two runs share one session id; a third (legacy-style) has none. The report
    # must collapse the first two into a single session block and leave the third
    # rendering standalone, exactly as pre-fix records do.
    monkeypatch.setenv(runlog.SESSION_ID_ENV, "sess-1")
    monkeypatch.setenv(runlog.RUN_ID_ENV, "20260101-000001-aaaaaa")
    with record("claude-tool", "Edit"):
        pass
    monkeypatch.setenv(runlog.RUN_ID_ENV, "20260101-000002-bbbbbb")
    with record("claude-tool", "Write"):
        pass

    # Third run: no session id (mimics a record written before this field).
    monkeypatch.delenv(runlog.SESSION_ID_ENV, raising=False)
    monkeypatch.setenv(runlog.RUN_ID_ENV, "20260101-000003-cccccc")
    with record("some-tool", "do"):
        pass

    html = build_report()

    # The session appears once as a grouped header, not once per run inside it.
    assert html.count("Session <code>sess-1</code>") == 1
    assert "runs in session:</b> 2" in html
    assert "<b>Sessions:</b> 1" in html
    # The two session runs and the lone legacy run are all still present.
    for rid in ("aaaaaa", "bbbbbb", "cccccc"):
        assert rid in html


def test_report_without_sessions_omits_session_chrome(tmp_runs):
    # Pure legacy records (no session id) must render as before: no "Sessions"
    # count, no session container — just per-run sections.
    with record("some-tool", "do"):
        pass
    html = build_report()
    assert "Sessions:" not in html
    assert "class='session" not in html
    assert "unverified" in html


def test_report_marks_spec_drift(tmp_runs, tmp_path, monkeypatch):
    # Two same-session verdicts with different spec hashes: the report must render
    # a spec-drift marker on the second (drifted) verdict, and show the short hash.
    spec = tmp_path / "checks.json"
    monkeypatch.setenv(runlog.SESSION_ID_ENV, "sess-report-drift")

    monkeypatch.setenv(runlog.RUN_ID_ENV, "20260101-000001-aaaaaa")
    spec.write_text(json.dumps({"checks": [
        {"id": "ok", "run": f'"{sys.executable}" -c "raise SystemExit(0)"',
         "expect": "exit0", "description": "original"}]}), encoding="utf-8")
    run_checks(load_checks(spec), cwd=tmp_path, record_to_log=True, spec_path=spec)

    monkeypatch.setenv(runlog.RUN_ID_ENV, "20260101-000002-bbbbbb")
    spec.write_text(json.dumps({"checks": [
        {"id": "ok", "run": f'"{sys.executable}" -c "raise SystemExit(0)"',
         "expect": "exit0", "description": "weakened"}]}), encoding="utf-8")
    run_checks(load_checks(spec), cwd=tmp_path, record_to_log=True, spec_path=spec)

    html = build_report()
    assert "spec drift" in html
    assert "drift-note" in html
    # The per-verdict short spec hash is shown.
    assert "spec " in html


def test_report_escapes_content(tmp_runs, tmp_path):
    # Ensure html.escape discipline holds — a run id with markup must not inject.
    run_checks([_check("x", run=f'"{sys.executable}" -c "print(\'<b>hi</b>\')"',
                       expect={"kind": "regex", "pattern": "hi"})],
               cwd=tmp_path, record_to_log=True)
    html = build_report()
    assert "<b>hi</b>" not in html or "&lt;b&gt;" in html
