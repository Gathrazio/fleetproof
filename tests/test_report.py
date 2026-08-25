"""Smoke tests for the HTML report generator."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import json

import pytest

from fleetproof import ledger, runlog
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


def test_report_renders_an_all_advisory_verdict_as_advisory(tmp_runs, tmp_path):
    # A verdict with zero blocking checks certifies nothing; a green 'pass'
    # badge on it would be the vacuous PASS in HTML form.
    run_checks([_check("a1", run=f'"{sys.executable}" -c "raise SystemExit(0)"',
                       block=False)],
               cwd=tmp_path, record_to_log=True)
    html = build_report()
    assert ">advisory</span>" in html
    assert ">pass</span>" not in html


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


# === dispatch rows + tree nesting ===

@pytest.fixture
def clean_parent(monkeypatch):
    """No inherited parent run id, so a dispatch's parent is only what we pass."""
    monkeypatch.delenv(runlog.PARENT_RUN_ID_ENV, raising=False)


def _dispatch(prompt, *, tier=None, parent=None, agent=None, report=None,
              verdict=None, close=False) -> str:
    run_id = ledger.create_dispatch(prompt, tier=tier, parent_run_id=parent,
                                    agent=agent)
    if report is not None:
        ledger.record_report(run_id, report)
    if verdict:
        ledger.record_verdict(run_id, verdict, detail="lane-artifact")
    if close:
        ledger.close_dispatch(run_id)
    return run_id


_SECTION_RE = re.compile(r"<section class='run ([^']*)'>\s*<h2>(\S+?) ")


def _depths(html: str) -> dict[str, int]:
    """run_id -> indentation depth, read back off the rendered sections."""
    out: dict[str, int] = {}
    for classes, run_id in _SECTION_RE.findall(html):
        depth = 0
        for token in classes.split():
            if re.fullmatch(r"d\d", token):
                depth = int(token[1:])
        out[run_id] = depth
    return out


def _set_parent(runs_dir: Path, run_id: str, parent_run_id: str) -> None:
    """Rewrite a run's parent link — the only way to build a cycle on purpose."""
    root = runs_dir / run_id / "_root.json"
    data = json.loads(root.read_text(encoding="utf-8"))
    data["parent_run_id"] = parent_run_id
    root.write_text(json.dumps(data), encoding="utf-8")


def test_report_renders_a_dispatch_as_a_first_class_row(tmp_runs, clean_parent):
    _dispatch("Build the audit surface\nsecond line is not on the row",
              tier="lane",
              agent={"agent_id": "a-1", "agent_type": "tester", "capture": "start"},
              report={"summary": "Shipped it.", "source": "last_assistant_message"},
              verdict="verified", close=True)
    html = build_report()

    assert "<span class='badge kind'>dispatch</span>" in html
    # The row an operator scans: state, tier, verdict, agent, report-on-record.
    assert ">done<" in html
    assert ">lane!<" in html          # declared tier, marked as declared
    assert ">tester<" in html
    assert "class='badge ok'>verified</span>" in html
    # Prompt and claim: first line only, with provenance for the claim.
    assert "Build the audit surface" in html
    assert "second line is not on the row" not in html
    assert "Shipped it." in html
    assert "via last_assistant_message" in html
    # And the transition trail that is the provenance for the verdict.
    assert "Transitions" in html
    assert "<b>Dispatches:</b> 1" in html


def test_report_marks_an_ungraded_dispatch_explicitly(tmp_runs, clean_parent):
    # The failure this exists to prevent: a dispatch nobody graded reading as fine.
    _dispatch("never graded", tier="leaf", report={"summary": "claimed done"},
              close=True)
    html = build_report()

    assert "class='badge warn'>ungraded</span>" in html
    assert "Dispatch ungraded" in html
    assert "an absent grade is not a passing grade" in html
    # Its own status class, so it is visually distinct from a verified dispatch.
    assert "<section class='run dispatch ungraded'>" in html
    assert "run dispatch verified" not in html


def test_report_headlines_a_contradicted_dispatch(tmp_runs, clean_parent):
    _dispatch("claimed an artifact it never wrote", tier="lane",
              report={"summary": "Shipped the artifact."}, verdict="contradicted")
    html = build_report()

    assert "<section class='run dispatch contradicted'>" in html
    assert "Claim contradicted" in html
    assert "reported done, but the independent checker" in html
    # Which check contradicted it, and which process said so.
    assert "lane-artifact" in html
    assert "checker" in html
    # Not yet closed, so the board wording for a half-closed dispatch shows here too.
    assert "contradicted (stalled)" in html


def test_report_nests_dispatches_under_their_parent_run(tmp_runs, clean_parent):
    root = _dispatch("bridge work", tier="bridge")
    lane = _dispatch("lane work", tier="lane", parent=root)
    leaf = _dispatch("leaf work", tier="leaf", parent=lane)
    # A parent that is not in this report must leave its child at top level rather
    # than dropping the row.
    orphan = _dispatch("parent not in this report", tier="lane",
                       parent="20200101-000000-zzzzzz")

    depths = _depths(build_report())
    assert depths[root] == 0
    assert depths[lane] == 1
    assert depths[leaf] == 2
    assert depths[orphan] == 0


def test_report_caps_nesting_depth(tmp_runs, clean_parent):
    chain = [_dispatch("d0", tier="bridge")]
    for i in range(1, 9):
        chain.append(_dispatch(f"d{i}", tier="lane", parent=chain[-1]))
    depths = _depths(build_report())
    assert [depths[r] for r in chain] == [0, 1, 2, 3, 4, 5, 5, 5, 5]


def test_report_survives_a_parent_cycle_without_losing_a_row(tmp_runs, clean_parent):
    # A malformed parent chain costs its rows their nesting and nothing else:
    # dropping a row is not an option on a surface meant to show everything.
    a = _dispatch("a", tier="lane")
    b = _dispatch("b", tier="lane", parent=a)
    _set_parent(tmp_runs, a, b)

    depths = _depths(build_report())
    assert {a, b} <= set(depths)
    assert max(depths[a], depths[b]) <= 1


def test_report_survives_a_self_parenting_run(tmp_runs, clean_parent):
    a = _dispatch("its own parent", tier="lane")
    _set_parent(tmp_runs, a, a)
    depths = _depths(build_report())
    assert depths[a] == 0


def test_report_flags_a_dispatch_whose_record_is_unreadable(tmp_runs, clean_parent):
    # One corrupt record degrades one row; it must not take down the whole report.
    run_id = _dispatch("will be corrupted", tier="lane")
    (tmp_runs / run_id / "dispatch.json").write_text("{ not json", encoding="utf-8")
    html = build_report()
    assert run_id in html
    assert "missing or unreadable" in html
    assert "<section class='run dispatch ungraded'>" in html


def test_report_surfaces_claimed_confidence_and_says_when_it_is_absent(
        tmp_runs, clean_parent):
    _dispatch("two deliverables", tier="lane", report={
        "summary": "two deliverables",
        "deliverables": [
            {"name": "graded", "confidence": 0.9, "evidence": "executed",
             "pointer": "src/x.py"},
            {"name": "unevidenced"},
        ],
    })
    html = build_report()
    assert "Claimed deliverables" in html
    assert "0.90" in html and "executed" in html
    # An omitted confidence/evidence says so rather than defaulting to reassuring.
    assert "not stated" in html


def test_report_flags_an_uncaptured_prompt(tmp_runs, clean_parent):
    from fleetproof.hookgate import UNCAPTURED_PROMPT
    _dispatch(UNCAPTURED_PROMPT.format(agent_type="tester"), tier="lane")
    html = build_report()
    assert "prompt not captured" in html


def test_report_links_every_dispatch_to_its_on_disk_record(tmp_runs, clean_parent):
    # Drill-in has to be one click, and the literal path stays visible so it works
    # even where the link does not.
    import html as html_lib

    run_id = _dispatch("work", tier="lane")
    record_dir = tmp_runs / run_id
    html = build_report()
    assert record_dir.as_uri() in html
    assert f"<code>{html_lib.escape(str(record_dir))}</code>" in html


def test_report_with_dispatches_is_still_self_contained(tmp_runs, clean_parent):
    _dispatch("<script>alert(1)</script> injected", tier="lane",
              report={"summary": "<b>bold</b> claim"})
    html = build_report()
    assert "<script" not in html.lower()
    # No network surface: the only links a report carries are local file paths.
    assert "http://" not in html and "https://" not in html
    assert "&lt;script&gt;" in html
    assert "&lt;b&gt;bold&lt;/b&gt;" in html


def test_report_nests_a_dispatch_under_the_run_that_ordered_it(tmp_runs, clean_parent):
    # The real-world shape of the tree: an ordinary recorded run dispatches work,
    # and the ledger picks that run up as the dispatch's parent. The report has to
    # show the dispatch under the run that ordered it, not as an unrelated section.
    with record("some-tool", "do"):
        pass
    parent_run = list_run_records()[0].run_id
    child = _dispatch("a dispatch", tier="lane")

    html = build_report()
    depths = _depths(html)
    assert depths[parent_run] == 0
    assert depths[child] == 1

    classes = {run_id: cls for cls, run_id in _SECTION_RE.findall(html)}
    # The ordinary run keeps the chrome and status it has always had.
    assert "dispatch" not in classes[parent_run]
    assert "unverified" in classes[parent_run]
    assert "dispatch" in classes[child]
