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


def test_dispatch_new_prompt_file_strips_a_windows_bom(cli_runs, tmp_path, capsys):
    # PowerShell redirection writes UTF-8 with a BOM; a plain utf-8 read embeds
    # ﻿ at the start of the recorded prompt forever.
    pf = tmp_path / "prompt.txt"
    pf.write_bytes(b"\xef\xbb\xbfbom prompt")
    assert main(["dispatch", "new", "--prompt-file", str(pf)]) == 0
    run_id = capsys.readouterr().out.strip()
    from fleetproof.ledger import load_dispatch
    assert load_dispatch(run_id).prompt == "bom prompt"


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


def test_dispatch_new_without_a_session_warns_it_is_unadoptable(cli_runs, capsys):
    # A bare CLI shell has no session id; the record stamps null, and adoption
    # needs an exact session match — so the dispatch the operator meant to be
    # joinable is a second, unjoinable row unless someone says so (observed in
    # a field deployment on Windows). stdout stays the bare run id.
    assert main(["dispatch", "new", "--prompt", "work", "--agent-name", "w"]) == 0
    out, err = capsys.readouterr()
    assert "session-less" in err
    assert "can never adopt it" in err
    assert runlog.SESSION_ID_ENV in err
    assert "--session-id" in err
    run_id = out.strip()
    from fleetproof.ledger import load_dispatch
    assert load_dispatch(run_id).session_id is None


def test_dispatch_new_session_id_flag_stamps_the_session(cli_runs, capsys):
    assert main(["dispatch", "new", "--prompt", "work", "--session-id", "sess-flag",
                 "--format", "json"]) == 0
    out, err = capsys.readouterr()
    assert "session-less" not in err
    assert json.loads(out)["session_id"] == "sess-flag"


def test_dispatch_new_session_id_flag_beats_the_environment(cli_runs, capsys,
                                                             monkeypatch):
    monkeypatch.setenv(runlog.SESSION_ID_ENV, "sess-env")
    assert main(["dispatch", "new", "--prompt", "work", "--session-id", "sess-flag",
                 "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["session_id"] == "sess-flag"


def test_dispatch_new_env_session_id_silences_the_warning(cli_runs, capsys,
                                                          monkeypatch):
    monkeypatch.setenv(runlog.SESSION_ID_ENV, "sess-env")
    assert main(["dispatch", "new", "--prompt", "work", "--format", "json"]) == 0
    out, err = capsys.readouterr()
    assert "session-less" not in err
    assert json.loads(out)["session_id"] == "sess-env"


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


def test_dispatch_intent_prompt_file_strips_a_windows_bom(cli_runs, tmp_path, capsys):
    pf = tmp_path / "prompt.md"
    pf.write_bytes(b"\xef\xbb\xbfbom prompt")
    assert main(["dispatch", "intent", "--agent", "recon",
                 "--prompt-file", str(pf)]) == 0
    path = Path(capsys.readouterr().out.strip())
    intent = json.loads(path.read_text(encoding="utf-8"))
    assert intent["prompt"] == "bom prompt"


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


def test_dispatch_park_unsatisfiable_flag_round_trips(cli_runs, capsys):
    # The structured non-satisfiability marker (D1): a real field on the
    # record, and the human line says it — the operator should see what they
    # just flagged. Without the flag, no marker.
    from fleetproof.ledger import load_dispatch
    run_id = _dispatch_new(capsys)
    assert main(["dispatch", "park", run_id,
                 "--reason", "cannot be satisfied from this seat",
                 "--unsatisfiable"]) == 0
    out = capsys.readouterr().out
    assert "parked (unsatisfiable): cannot be satisfied from this seat" in out
    assert load_dispatch(run_id).park_unsatisfiable is True
    plain = _dispatch_new(capsys)
    assert main(["dispatch", "park", plain, "--reason", "waiting"]) == 0
    assert load_dispatch(plain).park_unsatisfiable is False


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


def test_fleet_json_rows_carry_report_and_block_counts(cli_runs, capsys):
    # There is no `dispatch show` verb; the machine-readable board row is
    # where the per-dispatch report and block counts surface.
    from fleetproof.ledger import record_block, record_report
    run_id = _dispatch_new(capsys)
    record_report(run_id, {"summary": "one"})
    record_block(run_id, "blocked once")
    capsys.readouterr()
    assert main(["fleet", "--format", "json"]) == 0
    row = json.loads(capsys.readouterr().out)["dispatches"][0]
    assert row["run_id"] == run_id
    assert row["report_count"] == 1
    assert row["block_count"] == 1


def test_fleet_board_marks_an_inherited_tier_with_a_tilde(cli_runs, capsys):
    # '~' is neither '!' (nobody declared it for this spawn) nor '?' (it is
    # graded): the legend says where the tier came from.
    from fleetproof.ledger import TIER_SOURCE_INHERITED, create_dispatch
    source = create_dispatch("declared", tier="lane")
    create_dispatch("re-message", tier="lane", tier_source=TIER_SOURCE_INHERITED,
                    inherited_from=source)
    assert main(["fleet"]) == 0
    out = capsys.readouterr().out
    assert "lane~" in out
    assert "tier~ = inherited from an earlier dispatch of the same agent type" in out
    out.encode("cp1252")
    capsys.readouterr()
    assert main(["fleet", "--format", "json"]) == 0
    rows = json.loads(capsys.readouterr().out)["dispatches"]
    inherited = next(r for r in rows if r["tier_source"] == "inherited")
    assert inherited["inherited_from"] == source


def _ungraded_termination(session: str | None = None) -> str:
    from fleetproof.ledger import close_dispatch, create_dispatch, record_report
    run_id = create_dispatch("re-message", tier="lane", session_id=session)
    record_report(run_id, {"summary": "already done in my prior turn"})
    close_dispatch(run_id, by="hook")
    return run_id


def test_fleet_announces_ungraded_terminations_this_session(cli_runs, capsys,
                                                             monkeypatch):
    # The board column was honest but passive; the line arrives unprompted,
    # after the orphan count, naming the run ids.
    from fleetproof.ledger import close_dispatch, create_dispatch, record_report
    monkeypatch.setenv(runlog.SESSION_ID_ENV, "sess-x")
    ungraded = _ungraded_termination("sess-x")
    other = _ungraded_termination("sess-other")   # not this session
    graded = create_dispatch("fine", tier="lane", session_id="sess-x")
    record_report(graded, {"summary": "ok"})
    from fleetproof.ledger import record_verdict
    record_verdict(graded, "verified")
    close_dispatch(graded)
    assert main(["fleet"]) == 0
    out = capsys.readouterr().out
    assert "1 dispatch(es) terminated ungraded this session" in out
    assert ungraded in out.split("terminated ungraded this session")[1]
    assert other not in out.split("terminated ungraded this session")[1]
    assert "an absent grade is not a passing grade" in out
    # After the orphan-count line's slot: it is the last line of the board.
    assert out.rstrip().splitlines()[-1].startswith("1 dispatch(es) terminated ungraded")
    out.encode("cp1252")


def test_fleet_prints_nothing_about_ungraded_when_there_are_none(cli_runs, capsys,
                                                                 monkeypatch):
    monkeypatch.setenv(runlog.SESSION_ID_ENV, "sess-x")
    _ungraded_termination("sess-other")
    _dispatch_new(capsys)
    capsys.readouterr()
    assert main(["fleet"]) == 0
    assert "terminated ungraded" not in capsys.readouterr().out


def test_fleet_open_filter_cannot_hide_the_ungraded_line(cli_runs, capsys, monkeypatch):
    monkeypatch.setenv(runlog.SESSION_ID_ENV, "sess-x")
    _ungraded_termination("sess-x")
    assert main(["fleet", "--open"]) == 0
    out = capsys.readouterr().out
    assert "No dispatches found." in out
    assert "1 dispatch(es) terminated ungraded this session" in out


def test_fleet_without_a_session_counts_the_dispatches_shown(cli_runs, capsys):
    _ungraded_termination(None)
    assert main(["fleet"]) == 0
    out = capsys.readouterr().out
    assert "1 dispatch(es) terminated ungraded in the dispatches shown" in out


def test_fleet_json_carries_the_ungraded_terminations(cli_runs, capsys, monkeypatch):
    monkeypatch.setenv(runlog.SESSION_ID_ENV, "sess-x")
    run_id = _ungraded_termination("sess-x")
    assert main(["fleet", "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["terminated_ungraded_count"] == 1
    assert payload["terminated_ungraded"] == [run_id]
    assert payload["dispatches"][0]["terminated_ungraded"] is True


# === CLI check passes tier + session identity only (C15) ===

def test_check_cli_sets_tier_and_session_only(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    runlog.set_runs_dir(tmp_path / "runs")
    monkeypatch.setenv(runlog.SESSION_ID_ENV, "sess-cli-77")
    monkeypatch.delenv("FLEETPROOF_AGENT_TYPE", raising=False)
    spec = tmp_path / "checks.json"
    spec.write_text(json.dumps({"checks": [
        {"id": "echo", "run": [sys.executable, "-c",
                               "import os; print(os.environ['FLEETPROOF_TIER'], "
                               "os.environ['FLEETPROOF_SESSION_ID'], "
                               "os.environ.get('FLEETPROOF_AGENT_TYPE', '<absent>'))"]},
    ]}), encoding="utf-8")
    assert main(["check", "--spec", str(spec), "--tier", "lane", "--no-record",
                 "--format", "json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["checks"][0]["stdout_tail"].strip() == "lane sess-cli-77 <absent>"


# === arm/disarm --tier (C5) ===

def test_disarm_tier_lane_is_an_argparse_error_saying_lanes_are_always_graded(
        cli_runs, capsys):
    with pytest.raises(SystemExit) as exc:
        main(["disarm", "--tier", "lane", "--note", "why"])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "lanes are always graded" in err
    with pytest.raises(SystemExit):
        main(["arm", "--tier", "leaf"])
    assert "lanes are always graded" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        main(["arm", "--tier", "captain"])
    assert "armable tiers: bridge, coordinator" in capsys.readouterr().err


def test_disarm_and_arm_coordinator_roundtrip(cli_runs, capsys):
    from fleetproof.hookgate import arming_path
    assert main(["disarm", "--tier", "coordinator", "--note", "build phase"]) == 0
    out = capsys.readouterr().out
    assert "coordinator gate: ADVISORY (disarmed): build phase" in out
    assert "lanes are always graded" in out
    raw = json.loads(arming_path().read_text(encoding="utf-8"))
    assert raw["coordinator"] == "advisory"
    assert raw["bridge"] == "armed"
    assert raw["notes"]["coordinator"] == "build phase"
    assert raw["note"] == ""  # the bridge note; untouched

    assert main(["arm", "--tier", "coordinator", "--format", "json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["tier"] == "coordinator" and out["coordinator"] == "armed"
    assert json.loads(arming_path().read_text(encoding="utf-8"))["coordinator"] == "armed"


def test_fleet_board_shows_both_tiers_when_both_disarmed(cli_runs, capsys):
    assert main(["disarm", "--note", "publish phase"]) == 0
    assert main(["disarm", "--tier", "coordinator", "--note", "build phase"]) == 0
    capsys.readouterr()
    assert main(["fleet"]) == 0
    out = capsys.readouterr().out
    assert "bridge gate: ADVISORY (disarmed): publish phase" in out
    assert "coordinator gate: ADVISORY (disarmed): build phase" in out
    assert "re-arm: fleetproof arm --tier coordinator" in out

    assert main(["arm"]) == 0  # bridge only
    capsys.readouterr()
    assert main(["fleet"]) == 0
    out = capsys.readouterr().out
    assert "bridge gate: ADVISORY" not in out
    assert "coordinator gate: ADVISORY (disarmed): build phase" in out


def test_fleet_json_carries_the_full_arming_when_any_tier_is_advisory(cli_runs, capsys):
    assert main(["disarm", "--tier", "coordinator", "--note", "build phase"]) == 0
    capsys.readouterr()
    assert main(["fleet", "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["arming"]["coordinator"] == "advisory"
    assert payload["arming"]["bridge"] == "armed"


# === the CLI check records arming n/a; show renders the stamp (C6) ===

def test_check_cli_records_arming_not_applicable_and_show_renders_it(
        tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    runlog.set_runs_dir(tmp_path / "runs")
    monkeypatch.delenv(runlog.RUN_ID_ENV, raising=False)
    spec = tmp_path / "checks.json"
    spec.write_text(json.dumps({"checks": [
        {"id": "ok", "run": f'"{sys.executable}" -c "raise SystemExit(0)"'},
    ]}), encoding="utf-8")
    assert main(["check", "--spec", str(spec), "--tier", "lane", "--format", "json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["arming"] == {"tier": "lane", "state": "n/a", "note": None}

    assert main(["show", out["run_id"]]) == 0
    text = capsys.readouterr().out
    assert "arming: lane gate n/a" in text
    assert main(["show", out["run_id"], "--format", "json"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["sub_invocations"][0]["arming"]["state"] == "n/a"


def test_show_renders_a_disarmed_gate_run_with_its_note(tmp_path, monkeypatch, capsys):
    from fleetproof.hookgate import ADVISORY, set_arming, stop_gate
    monkeypatch.chdir(tmp_path)
    marker = tmp_path / ".fleetproof"
    marker.mkdir()
    (marker / "checks.json").write_text(json.dumps({"checks": [
        {"id": "bad", "run": f'"{sys.executable}" -c "raise SystemExit(1)"'},
    ]}), encoding="utf-8")
    runlog.set_runs_dir(marker / "runs")
    monkeypatch.setenv(runlog.RUN_ID_ENV, "20260101-000010-show01")
    set_arming(ADVISORY, note="publish phase")
    stop_gate()
    assert main(["show", "20260101-000010-show01"]) == 0
    text = capsys.readouterr().out
    assert "arming: bridge gate advisory (publish phase)" in text


def test_show_tolerates_records_without_an_arming_stamp(tmp_path, monkeypatch, capsys):
    # A 0.4.0 record has no arming key; show must render it as before.
    monkeypatch.chdir(tmp_path)
    rd = tmp_path / "runs"
    runlog.set_runs_dir(rd)
    sub = rd / "20260101-000011-old001" / "fleetproof-check-20260101-000011-000000"
    sub.mkdir(parents=True)
    (sub / "invocation.json").write_text(json.dumps(
        {"tool": "fleetproof", "subcmd": "check"}), encoding="utf-8")
    (sub / "result.json").write_text(json.dumps({"exit_code": 0}), encoding="utf-8")
    (sub / "output.json").write_text(json.dumps({"verdict": "pass"}), encoding="utf-8")
    assert main(["show", "20260101-000011-old001"]) == 0
    text = capsys.readouterr().out
    assert "fleetproof check" in text and "arming:" not in text


# === dispatch intent / new --preflight (C11) ===

def _preflight_manifest_file(tmp_path: Path) -> Path:
    mf = tmp_path / "manifest.json"
    mf.write_text(json.dumps({"manifest": {
        "deliverables": ["health field"],
        "checks": [
            {"id": "m-ok", "cmd": [sys.executable, "-c", "print('checked=true days_remaining=67')"],
             "expect": {"regex": "days_remaining=\\d+"}},
            {"id": "m-bad", "cmd": f'"{sys.executable}" -c "import sys; print(\'FAIL: '
                                   f'sati_cert.days_until_expiry missing\'); sys.exit(5)"'},
        ],
        "check_map": {"health field": ["m-ok", "m-bad"]},
    }}), encoding="utf-8")
    return mf


def test_dispatch_intent_preflight_renders_both_checks_and_records_nothing(
        cli_runs, tmp_path, capsys):
    # The authoring-time surface the field lacked: the exact command, its
    # exit, the expectation, and what the target emitted - before pinning.
    pf = tmp_path / "prompt.md"
    pf.write_text("ensure the health field", encoding="utf-8")
    mf = _preflight_manifest_file(tmp_path)
    assert main(["dispatch", "intent", "--agent", "tcn", "--prompt-file", str(pf),
                 "--manifest", str(mf), "--tier", "lane", "--preflight"]) == 0
    out = capsys.readouterr().out
    first, rest = out.split("\n", 1)
    assert Path(first).name == "tcn.json" and Path(first).exists()  # sidecar written
    assert "preflight: 2 manifest check(s)" in rest
    assert "[PASS] m-ok  (blocking)" in rest
    assert "argv:   [" in rest and "days_remaining=67" in rest
    assert "expect: output matches /days_remaining=\\d+/" in rest
    assert "[FAIL] m-bad  (blocking)" in rest
    assert "shell:  " in rest
    assert "exit:   5  -> exit 5 (expected 0)" in rest
    assert "stdout: | FAIL: sati_cert.days_until_expiry missing" in rest
    assert "preflight: 1 passed, 1 failed of 2 - a preview only" in rest
    assert rest.rstrip().endswith(
        "A positive control must be a captured real emission, never authored by "
        "the check's author.")
    # Nothing under runs/: the work has not happened yet.
    assert not any(p.is_dir() and not p.name.startswith(".") for p in cli_runs.iterdir()) \
        if cli_runs.exists() else True
    assert runlog.list_run_records() == []


def test_dispatch_intent_preflight_refuses_a_malformed_check_before_writing(
        cli_runs, tmp_path, capsys):
    pf = tmp_path / "prompt.md"
    pf.write_text("work", encoding="utf-8")
    mf = tmp_path / "manifest.json"
    mf.write_text(json.dumps({"checks": [
        {"id": "m-bad", "cmd": "echo a\necho b"}]}), encoding="utf-8")
    assert main(["dispatch", "intent", "--agent", "tcn", "--prompt-file", str(pf),
                 "--manifest", str(mf), "--preflight"]) == 2
    err = capsys.readouterr().err
    assert "manifest check [0] (m-bad): 'cmd' must be a single line" in err
    from fleetproof.ledger import intents_dir
    assert not (intents_dir() / "tcn.json").exists()


def test_dispatch_intent_without_preflight_still_writes_a_manifest_with_a_bad_check(
        cli_runs, tmp_path, capsys):
    # Unchanged 0.4.0 posture: the gate skips it loudly at stop time.
    pf = tmp_path / "prompt.md"
    pf.write_text("work", encoding="utf-8")
    mf = tmp_path / "manifest.json"
    mf.write_text(json.dumps({"checks": [{"id": "m-bad", "cmd": "echo a\necho b"}]}),
                  encoding="utf-8")
    assert main(["dispatch", "intent", "--agent", "tcn", "--prompt-file", str(pf),
                 "--manifest", str(mf)]) == 0


def test_dispatch_intent_preflight_json_carries_the_results(cli_runs, tmp_path, capsys):
    pf = tmp_path / "prompt.md"
    pf.write_text("work", encoding="utf-8")
    mf = _preflight_manifest_file(tmp_path)
    assert main(["dispatch", "intent", "--agent", "tcn", "--prompt-file", str(pf),
                 "--manifest", str(mf), "--preflight", "--format", "json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert [(r["id"], r["passed"], r["returncode"]) for r in out["preflight"]] == [
        ("m-ok", True, 0), ("m-bad", False, 5)]
    assert out["preflight"][1]["form"] == "shell"


def test_preflight_checks_see_the_identity_env_with_an_empty_run_id(
        cli_runs, tmp_path, capsys, monkeypatch):
    monkeypatch.setenv(runlog.SESSION_ID_ENV, "sess-pre")
    pf = tmp_path / "prompt.md"
    pf.write_text("work", encoding="utf-8")
    mf = tmp_path / "manifest.json"
    mf.write_text(json.dumps({"checks": [{"id": "echo", "cmd": [
        sys.executable, "-c",
        "import os; print(repr(os.environ['FLEETPROOF_RUN_ID']), "
        "os.environ['FLEETPROOF_AGENT_TYPE'], os.environ['FLEETPROOF_TIER'], "
        "os.environ['FLEETPROOF_SESSION_ID'])"]}]}), encoding="utf-8")
    assert main(["dispatch", "intent", "--agent", "tcn", "--prompt-file", str(pf),
                 "--manifest", str(mf), "--tier", "lane", "--preflight"]) == 0
    assert "stdout: | '' tcn lane sess-pre" in capsys.readouterr().out


def test_dispatch_new_preflight_shares_the_code_path(cli_runs, tmp_path, capsys):
    mf = _preflight_manifest_file(tmp_path)
    assert main(["dispatch", "new", "--prompt", "p", "--manifest", str(mf),
                 "--agent-name", "tcn", "--preflight", "--session-id", "s1"]) == 0
    out = capsys.readouterr().out
    run_id = out.split("\n", 1)[0]
    assert "[PASS] m-ok" in out and "[FAIL] m-bad" in out
    # The dispatch itself is recorded (that is what `new` does); no checker
    # run is.
    from fleetproof.ledger import load_dispatch
    assert load_dispatch(run_id) is not None
    assert not any(s.subcmd == "check" for r in runlog.list_run_records()
                   for s in r.sub_invocations)


def test_dispatch_new_preflight_needs_a_manifest(cli_runs, capsys):
    assert main(["dispatch", "new", "--prompt", "p", "--preflight"]) == 2
    assert "--preflight needs --manifest" in capsys.readouterr().err


# === grader control ledger at the CLI (C12) ===

def _control_manifest(tmp_path: Path) -> Path:
    grader = [sys.executable, "-c",
              "import json, os, sys; d = json.load(open(os.environ['FLEETPROOF_CONTROL_SAMPLE']));"
              " sys.exit(0 if 'days_remaining' in d else 5)"]
    mf = tmp_path / "manifest.json"
    mf.write_text(json.dumps({"manifest": {
        "deliverables": ["health field"],
        "checks": [{"id": "tcn-health", "cmd": grader},
                   {"id": "advisory-note", "cmd": "echo hi", "block": False}],
        "check_map": {"health field": ["tcn-health"]},
    }}), encoding="utf-8")
    return mf


def test_check_control_records_from_a_manifest_and_still_lets_plain_check_parse(
        cli_runs, tmp_path, capsys):
    from fleetproof.controls import load_control
    mf = _control_manifest(tmp_path)
    good = tmp_path / "captured.json"
    good.write_text(json.dumps({"days_remaining": 67.1}), encoding="utf-8")
    bad = tmp_path / "broken.json"
    bad.write_text(json.dumps({"days_until_expiry": 67}), encoding="utf-8")
    assert main(["check", "control", "tcn-health", "--pass-sample", str(good),
                 "--fail-sample", str(bad), "--provenance", "captured",
                 "--note", "captured 2026-08-25", "--manifest", str(mf)]) == 0
    out = capsys.readouterr().out
    assert "control recorded for 'tcn-health' (provenance: captured)" in out
    assert "pass_sample:" in out and "observed exit 0 -> pass (agrees)" in out
    assert "fail_sample:" in out and "observed exit 5 -> fail (agrees)" in out
    rec = load_control("tcn-health")
    assert rec["check_source"] == "manifest" and rec["note"] == "captured 2026-08-25"

    # The bare checker verb is untouched by the nested verb.
    spec = tmp_path / "checks.json"
    spec.write_text(json.dumps({"checks": [
        {"id": "ok", "run": f'"{sys.executable}" -c "raise SystemExit(0)"'}]}),
        encoding="utf-8")
    assert main(["check", "--spec", str(spec), "--no-record"]) == 0


def test_check_control_json_and_unresolvable_check(cli_runs, tmp_path, capsys):
    good = tmp_path / "s.json"
    good.write_text("{}", encoding="utf-8")
    assert main(["check", "control", "nowhere", "--pass-sample", str(good),
                 "--provenance", "authored", "--format", "json"]) == 0
    rec = json.loads(capsys.readouterr().out)
    assert rec["check_id"] == "nowhere" and rec["cmd"] is None
    assert rec["pass_sample"]["observed_exit"] is None


def test_check_control_rejects_a_bad_provenance_and_a_missing_sample(cli_runs, tmp_path, capsys):
    with pytest.raises(SystemExit):
        main(["check", "control", "x", "--pass-sample", "s", "--provenance", "guessed"])
    assert main(["check", "control", "x", "--pass-sample", str(tmp_path / "absent"),
                 "--provenance", "captured"]) == 2


def test_dispatch_intent_warns_per_uncontrolled_blocking_check(cli_runs, tmp_path, capsys):
    pf = tmp_path / "prompt.md"
    pf.write_text("work", encoding="utf-8")
    mf = _control_manifest(tmp_path)
    assert main(["dispatch", "intent", "--agent", "tcn", "--prompt-file", str(pf),
                 "--manifest", str(mf)]) == 0
    captured = capsys.readouterr()
    assert Path(captured.out.strip()).exists()  # sidecar written: a warning, not a refusal
    assert "WARNING: blocking check 'tcn-health' has no grader control" in captured.err
    assert "advisory-note" not in captured.err  # advisory checks are not warned about
    assert "fleetproof check control tcn-health --pass-sample" in captured.err


def test_dispatch_intent_strict_controls_refuses_and_writes_nothing(cli_runs, tmp_path, capsys):
    from fleetproof.ledger import intents_dir
    pf = tmp_path / "prompt.md"
    pf.write_text("work", encoding="utf-8")
    mf = _control_manifest(tmp_path)
    assert main(["dispatch", "intent", "--agent", "tcn", "--prompt-file", str(pf),
                 "--manifest", str(mf), "--strict-controls"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "REFUSED: blocking check 'tcn-health' has no grader control" in captured.err
    assert "error (uncontrolled_checks)" in captured.err
    assert not (intents_dir() / "tcn.json").exists()


def test_dispatch_intent_authored_only_pass_sample_warns(cli_runs, tmp_path, capsys):
    pf = tmp_path / "prompt.md"
    pf.write_text("work", encoding="utf-8")
    mf = _control_manifest(tmp_path)
    sample = tmp_path / "authored.json"
    sample.write_text(json.dumps({"days_remaining": 1}), encoding="utf-8")
    assert main(["check", "control", "tcn-health", "--pass-sample", str(sample),
                 "--provenance", "authored", "--manifest", str(mf)]) == 0
    capsys.readouterr()
    assert main(["dispatch", "intent", "--agent", "tcn", "--prompt-file", str(pf),
                 "--manifest", str(mf)]) == 0
    err = capsys.readouterr().err
    assert "WARNING: blocking check 'tcn-health': its only pass sample is authored" in err


def test_dispatch_intent_with_a_captured_control_is_silent(cli_runs, tmp_path, capsys):
    pf = tmp_path / "prompt.md"
    pf.write_text("work", encoding="utf-8")
    mf = _control_manifest(tmp_path)
    sample = tmp_path / "captured.json"
    sample.write_text(json.dumps({"days_remaining": 1}), encoding="utf-8")
    assert main(["check", "control", "tcn-health", "--pass-sample", str(sample),
                 "--provenance", "captured", "--manifest", str(mf)]) == 0
    capsys.readouterr()
    assert main(["dispatch", "intent", "--agent", "tcn", "--prompt-file", str(pf),
                 "--manifest", str(mf), "--strict-controls"]) == 0
    assert "WARNING" not in capsys.readouterr().err


def test_dispatch_new_with_a_manifest_warns_and_strict_refuses(cli_runs, tmp_path, capsys):
    mf = _control_manifest(tmp_path)
    assert main(["dispatch", "new", "--prompt", "p", "--manifest", str(mf),
                 "--session-id", "s1"]) == 0
    assert "WARNING: blocking check 'tcn-health' has no grader control" in capsys.readouterr().err
    assert main(["dispatch", "new", "--prompt", "p", "--manifest", str(mf),
                 "--session-id", "s1", "--strict-controls"]) == 1
    from fleetproof.ledger import list_dispatches
    assert len(list_dispatches()) == 1  # the strict attempt recorded nothing


# === telemetry summary off-state (C8) ===

def _graded_dispatch(capsys) -> str:
    """A dispatch that reported, was verified, and closed — classifiable
    only when an era stamped it at creation."""
    from fleetproof.ledger import close_dispatch, record_report, record_verdict
    run_id = _dispatch_new(capsys)
    record_report(run_id, {"summary": "done"})
    record_verdict(run_id, "verified")
    close_dispatch(run_id)
    return run_id


def test_telemetry_summary_says_off_when_no_era_is_configured(cli_runs, capsys):
    # Fifty-four dispatches, every rate n/a, "severity: no failures" — with no
    # line saying the layer was never switched on (observed in a field
    # deployment on Windows). The off-state is now the FIRST line.
    _graded_dispatch(capsys)
    capsys.readouterr()
    assert main(["telemetry", "summary"]) == 0
    out = capsys.readouterr().out
    lines = out.splitlines()
    assert lines[0].startswith("telemetry is OFF for this repo: no telemetry_era in "
                               ".fleetproof/config.json")
    assert "pre-telemetry dispatches are never back-filled" in lines[0]
    assert "severity: n/a (0 classifiable)" in out
    assert "no failures" not in out
    assert "predate telemetry_era" not in out


def test_telemetry_summary_json_carries_the_off_state(cli_runs, capsys):
    assert main(["telemetry", "summary", "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["telemetry_era"] is None


def test_telemetry_summary_names_the_era_when_every_row_predates_it(cli_runs, capsys):
    # Era configured AFTER the dispatches were created: nothing is
    # back-filled, and the summary says so with the date instead of
    # rendering four screens of n/a.
    _graded_dispatch(capsys)
    (cli_runs.parent / "config.json").write_text(
        json.dumps({"telemetry_era": "2026-01-01"}), encoding="utf-8")
    capsys.readouterr()
    assert main(["telemetry", "summary"]) == 0
    out = capsys.readouterr().out
    assert not out.startswith("telemetry is OFF")
    assert ("[full] 1 dispatch(es), 0 classifiable\n"
            "  all 1 dispatch(es) in this window predate telemetry_era 2026-01-01") in out
    assert "severity: n/a (0 classifiable)" in out
    assert "no failures" not in out


def test_telemetry_summary_with_classifiable_rows_prints_neither_line(cli_runs, capsys):
    (cli_runs.parent / "config.json").write_text(
        json.dumps({"telemetry_era": "2026-01-01"}), encoding="utf-8")
    _graded_dispatch(capsys)
    from fleetproof.telemetry import build_telemetry
    from fleetproof.ledger import list_dispatches
    build_telemetry(list_dispatches()[0].run_id)
    capsys.readouterr()
    assert main(["telemetry", "summary"]) == 0
    out = capsys.readouterr().out
    assert "telemetry is OFF" not in out
    assert "predate telemetry_era" not in out
    assert "[full] 1 dispatch(es), 1 classifiable" in out
    assert "severity: no failures" in out  # the existing wording, unchanged
    assert main(["telemetry", "summary", "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["telemetry_era"] == "2026-01-01"


def test_telemetry_summary_prints_no_verdict_split_and_escalated(cli_runs, capsys):
    # D12: the plain-English total prints WITH its split — the total alone
    # re-creates the ungraded/unverifiable word collision. D1: escalated_rate
    # prints beside the other quarantined rate (advisory).
    from fleetproof.ledger import record_report, record_verdict
    (cli_runs.parent / "config.json").write_text(
        json.dumps({"telemetry_era": "2026-01-01"}), encoding="utf-8")
    ungraded = _dispatch_new(capsys)
    record_report(ungraded, {"summary": "claimed, never graded"})
    assert main(["dispatch", "close", ungraded]) == 0
    capsys.readouterr()
    escalated = _dispatch_new(capsys)
    record_report(escalated, {"summary": "cannot satisfy"})
    record_verdict(escalated, "contradicted")
    assert main(["dispatch", "park", escalated,
                 "--reason", "unsatisfiable", "--unsatisfiable"]) == 0
    capsys.readouterr()
    assert main(["telemetry", "summary"]) == 0
    out = capsys.readouterr().out
    assert "no_verdict_rate: 1/2 = 0.500 (ungraded 1 + unverifiable 0)" in out
    assert "escalated_rate: 1/2 = 0.500" in out


# === phase verbs (C13) ===

def test_phase_advance_status_reset(cli_runs, capsys):
    assert main(["phase", "status"]) == 0
    assert capsys.readouterr().out.strip() == "phase: nothing retired."
    assert main(["phase", "advance", "--retire", "a", "--retire", "b", "--note", "over"]) == 0
    out = capsys.readouterr().out
    assert "phase advanced: retired a, b — over" in out
    assert "trips no drift pin" in out
    assert (cli_runs.parent / "phase.json").exists()
    assert main(["phase", "status", "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert set(payload["retired"]) == {"a", "b"}
    assert payload["retired"]["a"]["note"] == "over"
    assert payload["in_spec"] == []  # no spec here
    assert main(["phase", "status"]) == 0
    out = capsys.readouterr().out
    assert "2 retired check(s)" in out and "not in the current spec" in out
    assert main(["phase", "reset"]) == 0
    assert "phase reset: nothing retired" in capsys.readouterr().out
    assert main(["phase", "status", "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["retired"] == {}


def test_phase_advance_requires_a_note(cli_runs, capsys):
    with pytest.raises(SystemExit):
        main(["phase", "advance", "--retire", "a"])
    assert main(["phase", "advance", "--retire", "a", "--note", "  "]) == 2
    assert "non-empty --note" in capsys.readouterr().err


def test_check_cli_lists_a_retired_check_as_retired(cli_runs, tmp_path, capsys):
    spec = tmp_path / "checks.json"
    spec.write_text(json.dumps({"checks": [
        {"id": "bad", "run": f'"{sys.executable}" -c "raise SystemExit(1)"'},
        {"id": "ok", "run": f'"{sys.executable}" -c "raise SystemExit(0)"'},
    ]}), encoding="utf-8")
    assert main(["check", "--spec", str(spec)]) == 1
    capsys.readouterr()
    assert main(["phase", "advance", "--retire", "bad", "--note", "over"]) == 0
    capsys.readouterr()
    assert main(["check", "--spec", str(spec)]) == 0
    out = capsys.readouterr().out
    assert "[retired] bad: retired (phase advance: over)" in out
    assert "[FAIL]" not in out
    assert main(["check", "--spec", str(spec), "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"] == "pass" and payload["retired"][0]["id"] == "bad"
