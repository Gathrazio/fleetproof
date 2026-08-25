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


def test_dispatch_new_agent_name_writes_a_cli_capture(cli_runs, tmp_path, capsys,
                                                      monkeypatch):
    # The joinable form: the record awaits its agent_id (adopted at the first
    # matching SubagentStop) and carries the session so the stop can find it.
    monkeypatch.setenv(runlog.SESSION_ID_ENV, "sess-cli")
    assert main(["dispatch", "new", "--prompt", "work", "--agent-name", "worker",
                 "--format", "json"]) == 0
    record = json.loads(capsys.readouterr().out)
    assert record["agent"] == {"agent_type": "worker", "agent_id": None,
                               "capture": "cli"}
    assert record["session_id"] == "sess-cli"


def test_dispatch_new_without_agent_name_writes_no_agent_block(cli_runs, capsys):
    assert main(["dispatch", "new", "--prompt", "work", "--format", "json"]) == 0
    record = json.loads(capsys.readouterr().out)
    assert record["agent"] is None


def test_dispatch_new_with_manifest_file(cli_runs, tmp_path, capsys):
    mf = tmp_path / "manifest.json"
    mf.write_text(json.dumps({"deliverables": ["a"], "notes": "n"}), encoding="utf-8")
    assert main(["dispatch", "new", "--prompt", "work", "--manifest", str(mf),
                 "--format", "json"]) == 0
    record = json.loads(capsys.readouterr().out)
    assert record["manifest"]["deliverables"] == ["a"]
    assert record["manifest"]["notes"] == "n"


def test_dispatch_new_manifest_with_a_bom_is_accepted(cli_runs, tmp_path, capsys):
    # PowerShell redirection writes UTF-8 with a BOM; a BOM-prefixed manifest
    # must not be rejected as invalid JSON (same rationale as the spec
    # loader's utf-8-sig read of checks.json).
    mf = tmp_path / "manifest.json"
    mf.write_bytes(b"\xef\xbb\xbf" + json.dumps({"deliverables": ["a"]}).encode("utf-8"))
    assert main(["dispatch", "new", "--prompt", "work", "--manifest", str(mf),
                 "--format", "json"]) == 0
    record = json.loads(capsys.readouterr().out)
    assert record["manifest"]["deliverables"] == ["a"]


def test_dispatch_report_with_a_bom_is_accepted(cli_runs, tmp_path, capsys):
    run_id = _dispatch_new(capsys)
    rf = tmp_path / "report.json"
    rf.write_bytes(b"\xef\xbb\xbf" + json.dumps({"summary": "done"}).encode("utf-8"))
    assert main(["dispatch", "report", run_id, "--report", str(rf)]) == 0


def test_dispatch_new_bad_manifest_file(cli_runs, tmp_path, capsys):
    mf = tmp_path / "manifest.json"
    mf.write_text("{not json", encoding="utf-8")
    assert main(["dispatch", "new", "--prompt", "work", "--manifest", str(mf)]) == 2
    assert main(["dispatch", "new", "--prompt", "work", "--manifest",
                 str(tmp_path / "absent.json")]) == 2


def test_dispatch_intent_writes_sidecar_and_prints_path(cli_runs, tmp_path, capsys):
    pf = tmp_path / "prompt.md"
    pf.write_text("do the real work\nwith a real prompt", encoding="utf-8")
    mf = tmp_path / "manifest.json"
    mf.write_text(json.dumps({"manifest": {
        "deliverables": ["a"],
        "checks": [{"id": "c1", "cmd": "true"}],
        "check_map": {"a": ["c1"]},
    }}), encoding="utf-8")

    assert main(["dispatch", "intent", "--agent", "recon", "--prompt-file", str(pf),
                 "--manifest", str(mf), "--tier", "lane"]) == 0
    path = Path(capsys.readouterr().out.strip())
    assert path.exists()
    assert path.name == "recon.json"

    intent = json.loads(path.read_text(encoding="utf-8"))
    assert intent["agent_type"] == "recon"
    assert intent["prompt"] == "do the real work\nwith a real prompt"
    assert intent["manifest"]["check_map"] == {"a": ["c1"]}
    assert intent["tier"] == "lane"
    assert intent["created_at"]


def test_dispatch_intent_role_flag_writes_the_role_field(cli_runs, tmp_path, capsys):
    # --role keys the sidecar to what the spawn is FOR, so a per-task spawn
    # name still finds its intent (match = filename OR role, exact equality).
    pf = tmp_path / "prompt.md"
    pf.write_text("work", encoding="utf-8")
    assert main(["dispatch", "intent", "--agent", "widget-refactor",
                 "--prompt-file", str(pf), "--role", "tester"]) == 0
    path = Path(capsys.readouterr().out.strip())
    intent = json.loads(path.read_text(encoding="utf-8"))
    assert intent["role"] == "tester"
    assert intent["agent_type"] == "widget-refactor"


def test_dispatch_intent_json_format(cli_runs, tmp_path, capsys):
    pf = tmp_path / "prompt.md"
    pf.write_text("work", encoding="utf-8")
    assert main(["dispatch", "intent", "--agent", "recon", "--prompt-file", str(pf),
                 "--format", "json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert Path(out["intent_path"]).exists()


def test_dispatch_intent_rejects_bad_inputs(cli_runs, tmp_path, capsys):
    pf = tmp_path / "prompt.md"
    pf.write_text("work", encoding="utf-8")
    # Missing prompt file: an intent with no prompt is the placeholder problem
    # the sidecar exists to fix, so it fails at write time.
    assert main(["dispatch", "intent", "--agent", "recon",
                 "--prompt-file", str(tmp_path / "absent.md")]) == 2
    # Unparseable manifest file vs. parseable-but-unusable manifest shape.
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert main(["dispatch", "intent", "--agent", "recon", "--prompt-file", str(pf),
                 "--manifest", str(bad)]) == 2
    unusable = tmp_path / "unusable.json"
    unusable.write_text(json.dumps({"deliverables": "not-an-array"}), encoding="utf-8")
    assert main(["dispatch", "intent", "--agent", "recon", "--prompt-file", str(pf),
                 "--manifest", str(unusable)]) == 1
    # A path-shaped agent type must never become a path.
    assert main(["dispatch", "intent", "--agent", "../evil",
                 "--prompt-file", str(pf)]) == 1


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
    # Explicit labels, not raw state names.
    assert "in-fleet (awaiting report)" in out
    assert "done (no report)" in out
    # cp1252 consoles: the board must encode without a UnicodeEncodeError.
    out.encode("cp1252")

    assert main(["fleet", "--open"]) == 0
    open_out = capsys.readouterr().out
    assert open_id in open_out and dead_id not in open_out


def test_fleet_board_never_shows_a_bare_dash_for_a_verdict(cli_runs, capsys):
    # The one thing this board must not do: let an ungraded dispatch look fine.
    _dispatch_new(capsys)
    capsys.readouterr()
    assert main(["fleet"]) == 0
    out = capsys.readouterr().out
    assert "ungraded" in out
    header, row = out.splitlines()[0], out.splitlines()[1]
    verdict_col = header.index("verdict")
    assert row[verdict_col:verdict_col + len("ungraded")] == "ungraded"
    # And the footer counts it, so the operator sees the total without scanning.
    assert "1 ungraded." in out


def test_fleet_board_labels_state_tier_agent_and_verdict(cli_runs, capsys):
    from fleetproof.ledger import (
        close_dispatch,
        create_dispatch,
        record_report,
        record_verdict,
    )

    # Declared tier + a graded, closed, agent-backed dispatch.
    graded = create_dispatch(
        "declared work",
        tier="lane",
        agent={"agent_id": "a-1", "agent_type": "tester", "capture": "stop-only"},
    )
    record_report(graded, {"summary": "done"})
    record_verdict(graded, "contradicted", detail="lane-artifact")
    # An inferred-tier dispatch that reported and was then abandoned half-closed.
    stalled = create_dispatch("inferred work")
    record_report(stalled, {"summary": "done"})
    closed_clean = create_dispatch("finished work", tier="leaf")
    record_report(closed_clean, {"summary": "done"})
    record_verdict(closed_clean, "verified")
    close_dispatch(closed_clean)

    assert main(["fleet"]) == 0
    out = capsys.readouterr().out
    # Declared tiers carry the '!' marker; an inferred one does not.
    assert "lane!" in out and "leaf!" in out
    assert "bridge " in out  # inferred: no marker (no parent run -> bridge)
    assert "reported (stalled)" in out
    assert "done " in out
    assert "contradicted" in out and "verified" in out
    # A back-filled agent is flagged: the start hook never fired for it.
    assert "tester (stop-only)" in out
    # The legend explains every marker it uses.
    assert "tier! = declared, not inferred." in out
    out.encode("cp1252")


def test_fleet_board_marks_a_defaulted_tier_with_a_question_mark(cli_runs, capsys):
    # 'lane!' claims the dispatcher declared the tier; a capture that fell back
    # to the default must read as 'lane?' — a defaulted tier is a hint that an
    # intent went missing, and the ! marker asserted the opposite.
    from fleetproof.ledger import TIER_SOURCE_DEFAULTED, create_dispatch
    create_dispatch("captured with no intent", tier="lane",
                    tier_source=TIER_SOURCE_DEFAULTED)
    assert main(["fleet"]) == 0
    out = capsys.readouterr().out
    assert "lane?" in out
    assert "lane!" not in out
    assert "tier? = defaulted (no intent matched)" in out
    out.encode("cp1252")


def test_fleet_json_still_emits_the_full_records(cli_runs, capsys):
    # The human board changed shape; the machine-readable form must not.
    run_id = _dispatch_new(capsys)
    capsys.readouterr()
    assert main(["fleet", "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    record = payload["dispatches"][0]
    assert record["run_id"] == run_id
    for key in ("prompt", "tier", "tier_source", "manifest", "transitions",
                "spec_sha256_pinned", "state", "verdict", "has_report", "age"):
        assert key in record


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


def test_fleet_counts_orphans_without_listing_them_as_dispatches(cli_runs, capsys):
    # An orphan stop is a sighting, not a dispatch: the default board mentions
    # the count only, and no orphan row appears among the dispatch rows.
    from fleetproof.ledger import record_orphan_stop
    record_orphan_stop(agent_id="a-1", agent_type=None, session_id="sess-x",
                       last_assistant_message="a helper agent's summary")
    assert main(["fleet"]) == 0
    out = capsys.readouterr().out
    assert "No dispatches found." in out
    assert "1 orphan stop(s)" in out
    assert "not graded" in out
    out.encode("cp1252")


def test_fleet_orphans_flag_lists_the_orphans(cli_runs, capsys):
    from fleetproof.ledger import record_orphan_stop
    record_orphan_stop(agent_id="a-1", agent_type="helper", session_id="sess-x",
                       last_assistant_message="orphaned summary text")
    assert main(["fleet", "--orphans"]) == 0
    out = capsys.readouterr().out
    assert "helper" in out and "orphaned summary text" in out
    out.encode("cp1252")

    assert main(["fleet", "--orphans", "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(payload["orphans"]) == 1
    assert payload["orphans"][0]["agent_id"] == "a-1"

    # Session filter applies to the orphan view the same way it does the board.
    assert main(["fleet", "--orphans", "--session", "sess-other",
                 "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["orphans"] == []


# === liveness beacon ===

def _passing_spec(tmp_path: Path) -> Path:
    spec = tmp_path / "checks.json"
    spec.write_text(json.dumps({"checks": [
        {"id": "ok", "run": f'"{sys.executable}" -c "raise SystemExit(0)"',
         "expect": "exit0"},
    ]}), encoding="utf-8")
    return spec


def test_liveness_warning_when_session_has_no_hook_record(
        cli_runs, tmp_path, monkeypatch, capsys):
    # A plugin installed mid-session registers no hooks until restart: the gate
    # looks installed and grades nothing. Each read surface must say so.
    monkeypatch.setenv(runlog.SESSION_ID_ENV, "sess-inert")
    spec = _passing_spec(tmp_path)

    assert main(["fleet"]) == 0
    assert "no hook has fired for this session" in capsys.readouterr().err
    assert main(["check", "--spec", str(spec), "--no-record"]) == 0
    err = capsys.readouterr().err
    assert "no hook has fired for this session" in err and "INERT" in err
    assert main(["report", "-o", str(tmp_path / "r.html")]) == 0
    assert "no hook has fired for this session" in capsys.readouterr().err


def test_liveness_warning_absent_once_a_hook_record_exists(
        cli_runs, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(runlog.SESSION_ID_ENV, "sess-alive")
    monkeypatch.setenv(runlog.RUN_ID_ENV, "20260101-000001-abcdef")
    with runlog.record("claude-tool", "Bash", {}):
        pass

    assert main(["fleet"]) == 0
    assert "no hook has fired" not in capsys.readouterr().err
    assert main(["check", "--spec", str(_passing_spec(tmp_path)),
                 "--no-record"]) == 0
    assert "no hook has fired" not in capsys.readouterr().err


def test_liveness_warning_absent_with_a_hook_created_dispatch(
        cli_runs, monkeypatch, capsys):
    # A dispatch whose transitions carry by="hook" is hook evidence too — the
    # subagent hooks can fire in a session before any PostToolUse record lands.
    from fleetproof.ledger import create_dispatch
    monkeypatch.setenv(runlog.SESSION_ID_ENV, "sess-alive-2")
    create_dispatch("hook-captured work", tier="lane", by="hook")

    assert main(["fleet"]) == 0
    assert "no hook has fired" not in capsys.readouterr().err


def test_liveness_unknown_line_only_in_fleet_without_a_session(
        cli_runs, tmp_path, capsys):
    # No session id in env means liveness is unanswerable, not bad: only the
    # board says so, softly; check and report stay silent.
    assert main(["fleet"]) == 0
    assert "hooks-liveness unknown" in capsys.readouterr().err
    assert main(["report", "-o", str(tmp_path / "r.html")]) == 0
    err = capsys.readouterr().err
    assert "hooks-liveness" not in err and "no hook has fired" not in err


# === hook entry subcommands + tier-scoped check ===

def test_subagent_hook_subcommands_are_wired(cli_runs, monkeypatch, capsys):
    # Same shape as the existing stop-gate/record entries: the CLI is another way
    # into the hook mains, which is what the plugin shims call.
    import io
    import os

    from fleetproof.ledger import list_dispatches

    payload = {"session_id": "sess-cli", "hook_event_name": "SubagentStart",
               "agent_id": "cli-agent", "agent_type": "tester"}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    assert main(["subagent-start"]) == 0
    assert len(list_dispatches()) == 1

    payload["hook_event_name"] = "SubagentStop"
    payload["last_assistant_message"] = ""
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    assert main(["subagent-stop"]) == 0
    decision = json.loads(capsys.readouterr().out)
    assert decision["decision"] == "block"
    # Blocked for want of a report, so the dispatch stays where it was.
    assert list_dispatches()[0].state == "dispatched"
    # _apply_session_id writes os.environ directly, mirroring the real hook process.
    os.environ.pop(runlog.SESSION_ID_ENV, None)


def test_check_tier_flag_scopes_the_run(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    runlog.set_runs_dir(tmp_path / "runs")
    spec = tmp_path / "checks.json"
    spec.write_text(json.dumps({"checks": [
        {"id": "leaf-ok", "run": f'"{sys.executable}" -c "raise SystemExit(0)"',
         "tier": "leaf"},
        {"id": "bridge-bad", "run": f'"{sys.executable}" -c "raise SystemExit(1)"'},
    ]}), encoding="utf-8")

    # Untiered fires at every tier (the C1 fix), so the leaf run includes the
    # untiered failure and fails with it.
    assert main(["check", "--spec", str(spec), "--tier", "leaf", "--format", "json"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["tier"] == "leaf"
    assert [c["id"] for c in out["checks"]] == ["leaf-ok", "bridge-bad"]

    # An untiered check gates the bridge too, so the bridge tier still fails.
    assert main(["check", "--spec", str(spec), "--tier", "bridge"]) == 1
    assert "tier: bridge" in capsys.readouterr().out

    # No tier: v0.1 behaviour, every check runs.
    assert main(["check", "--spec", str(spec), "--format", "json"]) == 1
    every = json.loads(capsys.readouterr().out)
    assert every["tier"] is None
    assert [c["id"] for c in every["checks"]] == ["leaf-ok", "bridge-bad"]


def test_check_rejects_unknown_tier(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    spec = tmp_path / "checks.json"
    spec.write_text(json.dumps({"checks": []}), encoding="utf-8")
    # argparse choices reject it before the checker ever runs.
    with pytest.raises(SystemExit):
        main(["check", "--spec", str(spec), "--tier", "middle-management"])


def test_dispatch_close_reason_flag(cli_runs, capsys):
    # The CLI is the operator's close path: no flag means operator-close, and a
    # scripted sweeper can say what it actually is.
    from fleetproof.ledger import load_dispatch

    assert main(["dispatch", "new", "--prompt", "default close"]) == 0
    default_id = capsys.readouterr().out.strip()
    assert main(["dispatch", "close", default_id]) == 0
    capsys.readouterr()
    assert load_dispatch(default_id).terminate_reason == "operator-close"

    assert main(["dispatch", "new", "--prompt", "swept close"]) == 0
    swept_id = capsys.readouterr().out.strip()
    assert main(["dispatch", "close", swept_id, "--reason", "sweep-idle"]) == 0
    capsys.readouterr()
    assert load_dispatch(swept_id).terminate_reason == "sweep-idle"


# === arm / disarm (B2) ===

def test_disarm_and_arm_roundtrip(cli_runs, capsys):
    import json as _json
    from fleetproof.hookgate import arming_path
    assert main(["disarm", "--note", "publish phase"]) == 0
    raw = _json.loads(arming_path().read_text(encoding="utf-8"))
    assert raw["bridge"] == "advisory"
    assert raw["note"] == "publish phase"
    assert raw["set_at"] and raw["by"]

    assert main(["arm"]) == 0
    raw = _json.loads(arming_path().read_text(encoding="utf-8"))
    assert raw["bridge"] == "armed"


def test_disarm_requires_a_note(cli_runs, capsys):
    # Asymmetric on purpose: switching the gate OFF requires a reason.
    with pytest.raises(SystemExit):
        main(["disarm"])
    assert main(["disarm", "--note", "   "]) == 2  # a blank note is no note


def test_fleet_echoes_the_advisory_state(cli_runs, capsys):
    assert main(["disarm", "--note", "publish phase"]) == 0
    capsys.readouterr()
    _dispatch_new(capsys)
    assert main(["fleet"]) == 0
    out = capsys.readouterr().out
    assert "ADVISORY" in out
    assert "publish phase" in out

    assert main(["arm"]) == 0
    capsys.readouterr()
    assert main(["fleet"]) == 0
    out = capsys.readouterr().out
    assert "ADVISORY" not in out  # armed is the default; the board stays quiet


# === dispatch park (B3) ===

def test_dispatch_park_terminates_with_the_reason(cli_runs, capsys):
    from fleetproof.ledger import load_dispatch
    run_id = _dispatch_new(capsys)
    assert main(["dispatch", "park", run_id,
                 "--reason", "blocked on operator approval"]) == 0
    d = load_dispatch(run_id)
    assert d.state == "terminated"
    assert d.terminate_reason == "parked: blocked on operator approval"


def test_dispatch_park_requires_a_reason(cli_runs, capsys):
    run_id = _dispatch_new(capsys)
    with pytest.raises(SystemExit):
        main(["dispatch", "park", run_id])
    assert main(["dispatch", "park", run_id, "--reason", "   "]) == 2


def test_dispatch_park_refuses_a_terminated_dispatch(cli_runs, capsys):
    run_id = _dispatch_new(capsys)
    assert main(["dispatch", "close", run_id]) == 0
    capsys.readouterr()
    assert main(["dispatch", "park", run_id, "--reason", "again"]) == 1


def test_fleet_labels_abandoned_and_parked(cli_runs, capsys):
    from fleetproof.ledger import (
        REASON_ABANDONED, close_dispatch, record_report, record_verdict,
    )
    abandoned = _dispatch_new(capsys)
    record_report(abandoned, {"summary": "claimed done"})
    record_verdict(abandoned, "contradicted")
    close_dispatch(abandoned, by="hook", reason=REASON_ABANDONED)
    parked = _dispatch_new(capsys)
    assert main(["dispatch", "park", parked, "--reason", "waiting"]) == 0
    capsys.readouterr()

    assert main(["fleet"]) == 0
    out = capsys.readouterr().out
    abandoned_row = next(line for line in out.splitlines() if abandoned in line)
    # Terminal, loud, and never mistakable for a clean "done" — and the
    # verdict column keeps saying contradicted, never verified.
    assert "abandoned!" in abandoned_row
    assert "contradicted" in abandoned_row
    parked_row = next(line for line in out.splitlines() if parked in line)
    assert "parked" in parked_row
    assert "ungraded" in parked_row
