"""Smoke tests for the HTML report generator."""

from __future__ import annotations

import sys
from pathlib import Path

from fleetproof.checker import run_checks
from fleetproof.checks import Check
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


def test_report_escapes_content(tmp_runs, tmp_path):
    # Ensure html.escape discipline holds — a run id with markup must not inject.
    run_checks([_check("x", run=f'"{sys.executable}" -c "print(\'<b>hi</b>\')"',
                       expect={"kind": "regex", "pattern": "hi"})],
               cwd=tmp_path, record_to_log=True)
    html = build_report()
    assert "<b>hi</b>" not in html or "&lt;b&gt;" in html
