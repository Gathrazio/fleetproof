"""0.7.0 feature tests: block budgets, false-terminal verify, advisory
visibility, board/ledger hygiene, the self-gitignoring ledger, and the regex
consult opt-in. Each test names the field finding that earned the behavior."""

from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path

import pytest

from fleetproof import runlog
from fleetproof.cli import main
from fleetproof.hookgate import (
    GATE_EXHAUSTED_MARKER,
    MAX_BRIDGE_BLOCKS,
    MAX_NO_REPORT_BLOCKS,
    stop_block_status,
    stop_gate,
    stop_gate_main,
    subagent_start_main,
    subagent_stop_main,
    verify_dispatch,
)
from fleetproof.ledger import (
    REASON_NO_REPORT,
    LedgerError,
    close_dispatch,
    create_dispatch,
    list_dispatches,
    load_dispatch,
    record_report,
    reopen_for_verify,
)

_PASS = {"id": "ok", "run": f'"{sys.executable}" -c "raise SystemExit(0)"',
         "expect": "exit0", "block": True}
_FAIL = {"id": "bad", "run": f'"{sys.executable}" -c "raise SystemExit(1)"',
         "expect": "exit0", "block": True}
_LANE_PASS = {"id": "lane-ok", "run": f'"{sys.executable}" -c "raise SystemExit(0)"',
              "expect": "exit0", "tier": "lane", "block": True}
_LANE_WARN_FAIL = {"id": "lane-warn", "run": f'"{sys.executable}" -c "raise SystemExit(1)"',
                   "expect": "exit0", "tier": "lane", "block": False}


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
    os.environ.pop(runlog.SESSION_ID_ENV, None)


def _feed(monkeypatch, payload: dict) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))


def _start_payload(agent_id="agent-1", agent_type="tester", session="sess-v070") -> dict:
    return {"session_id": session, "transcript_path": "/tmp/t.jsonl", "cwd": ".",
            "hook_event_name": "SubagentStart", "agent_id": agent_id,
            "agent_type": agent_type}


def _stop_payload(message="Done.", agent_id="agent-1", agent_type="tester",
                  session="sess-v070") -> dict:
    payload = _start_payload(agent_id, agent_type, session)
    payload["hook_event_name"] = "SubagentStop"
    payload["last_assistant_message"] = message
    return payload


# === Bridge stop-block budget (trial rows T6/H4; MCC unbounded-wedge) ===

def test_bridge_block_budget_exhausts_to_context(tmp_path, monkeypatch):
    _setup_project(tmp_path, [_FAIL], monkeypatch)
    monkeypatch.setenv(runlog.SESSION_ID_ENV, "sess-budget")
    # The first MAX_BRIDGE_BLOCKS stops block; the next stands down.
    for i in range(MAX_BRIDGE_BLOCKS):
        decision, code = stop_gate()
        assert code == 0
        assert decision is not None and decision.get("decision") == "block", i
    decision, code = stop_gate()
    assert code == 0
    assert decision is not None
    assert "decision" not in decision  # no block
    context = decision["hookSpecificOutput"]["additionalContext"]
    assert GATE_EXHAUSTED_MARKER in context
    assert "bad" in context  # the original failure text still renders
    status = stop_block_status("sess-budget")
    assert status is not None and status["exhausted"] is True


def test_bridge_block_budget_resets_on_clean_stop(tmp_path, monkeypatch):
    _setup_project(tmp_path, [_FAIL], monkeypatch)
    monkeypatch.setenv(runlog.SESSION_ID_ENV, "sess-reset")
    for _ in range(3):
        decision, _ = stop_gate()
        assert decision.get("decision") == "block"
    # The failure is fixed; a clean stop re-arms the budget. (The spec edit
    # legitimately produces drift CONTEXT — what matters is that nothing
    # blocks and the counter clears.)
    _setup_project(tmp_path, [_PASS], monkeypatch)
    decision, _ = stop_gate()
    assert decision is None or "decision" not in decision
    assert stop_block_status("sess-reset") is None
    # A fresh wedge starts its count from zero.
    _setup_project(tmp_path, [_FAIL], monkeypatch)
    decision, _ = stop_gate()
    assert decision.get("decision") == "block"
    assert stop_block_status("sess-reset")["blocks"] == 1


def test_block_budget_configurable_via_config(tmp_path, monkeypatch):
    _setup_project(tmp_path, [_FAIL], monkeypatch)
    (tmp_path / ".fleetproof" / "config.json").write_text(
        json.dumps({"stop_block_budget": 1}), encoding="utf-8")
    monkeypatch.setenv(runlog.SESSION_ID_ENV, "sess-cfg")
    decision, _ = stop_gate()
    assert decision.get("decision") == "block"
    decision, _ = stop_gate()
    assert decision is not None and "decision" not in decision
    assert GATE_EXHAUSTED_MARKER in decision["hookSpecificOutput"]["additionalContext"]


def test_exhaustion_context_survives_stop_hook_retry(tmp_path, monkeypatch, capsys):
    # The stand-down announcement is the wedge's terminus; _suppress_on_retry
    # must not swallow it even with stop_hook_active true (same posture as the
    # abandonment notice).
    _setup_project(tmp_path, [_FAIL], monkeypatch)
    (tmp_path / ".fleetproof" / "config.json").write_text(
        json.dumps({"stop_block_budget": 1}), encoding="utf-8")
    for payload in ({"session_id": "sess-nsp", "stop_hook_active": True},) * 2:
        _feed(monkeypatch, payload)
        assert stop_gate_main() == 0
    out = capsys.readouterr().out
    # json.dumps escapes the marker's em dash, so match its ASCII tail.
    assert "HUMAN ATTENTION REQUIRED]" in out
    assert "_fleetproof_never_suppress" not in out


def test_no_session_means_no_budget_and_blocks_keep_blocking(tmp_path, monkeypatch):
    # Session-less stops cannot be budgeted per session; the gate keeps its
    # old always-block behavior rather than inventing a global counter.
    _setup_project(tmp_path, [_FAIL], monkeypatch)
    for _ in range(MAX_BRIDGE_BLOCKS + 2):
        decision, _ = stop_gate()
        assert decision.get("decision") == "block"


# === Report-before-idle ladder (trial row 4c) ===

def test_third_no_report_block_is_terminal(tmp_path, monkeypatch, capsys):
    _setup_project(tmp_path, [_LANE_PASS], monkeypatch)
    _feed(monkeypatch, _start_payload())
    assert subagent_start_main() == 0
    run_id = list_dispatches()[0].run_id

    for i in range(MAX_NO_REPORT_BLOCKS - 1):
        _feed(monkeypatch, _stop_payload(message=""))
        assert subagent_stop_main() == 0
        out = capsys.readouterr().out
        assert "report-before-idle" in out, i
    # The third empty stop closes the dispatch instead of blocking again.
    _feed(monkeypatch, _stop_payload(message=""))
    assert subagent_stop_main() == 0
    out = capsys.readouterr().out
    assert "report-before-idle" not in out
    assert "NOT verified" in out
    d = load_dispatch(run_id)
    assert d.is_terminal
    assert d.verdict is None
    assert d.terminate_reason == REASON_NO_REPORT


def test_no_report_close_renders_loud_on_the_board(tmp_path, monkeypatch):
    from fleetproof.report import state_label
    _setup_project(tmp_path, [_LANE_PASS], monkeypatch)
    run_id = create_dispatch("p", tier="lane", session_id="s")
    close_dispatch(run_id, reason=REASON_NO_REPORT)
    assert state_label(load_dispatch(run_id)) == "no-report!"


def test_agent_that_reports_after_blocks_still_grades(tmp_path, monkeypatch, capsys):
    # The ladder must not penalize an agent that recovers before the terminus.
    _setup_project(tmp_path, [_LANE_PASS], monkeypatch)
    _feed(monkeypatch, _start_payload())
    assert subagent_start_main() == 0
    _feed(monkeypatch, _stop_payload(message=""))
    assert subagent_stop_main() == 0
    capsys.readouterr()
    _feed(monkeypatch, _stop_payload(message="Done, wrote the file."))
    assert subagent_stop_main() == 0
    d = list_dispatches()[0]
    assert d.verdict == "verified"
    assert d.is_terminal


# === A retry is not a stall (trial row 1c) ===

def test_contradicted_dispatch_renders_as_retry_context_not_block(tmp_path, monkeypatch):
    _setup_project(tmp_path, [_PASS], monkeypatch)
    monkeypatch.setenv(runlog.SESSION_ID_ENV, "sess-retry")
    run_id = create_dispatch("p", tier="lane", session_id="sess-retry")
    record_report(run_id, {"summary": "done"})
    from fleetproof.ledger import record_verdict
    record_verdict(run_id, "contradicted", detail="bad-check")
    decision, code = stop_gate()
    assert code == 0
    assert decision is not None and "decision" not in decision
    context = decision["hookSpecificOutput"]["additionalContext"]
    assert "[in retry]" in context and run_id in context


def test_graded_but_unclosed_dispatch_still_blocks_the_bridge(tmp_path, monkeypatch):
    _setup_project(tmp_path, [_PASS], monkeypatch)
    monkeypatch.setenv(runlog.SESSION_ID_ENV, "sess-stall")
    run_id = create_dispatch("p", tier="lane", session_id="sess-stall")
    record_report(run_id, {"summary": "done"})
    from fleetproof.ledger import record_verdict
    record_verdict(run_id, "verified", detail="1/1")
    decision, _ = stop_gate()
    assert decision.get("decision") == "block"
    assert run_id in decision["reason"]


# === False-terminal verify (trial finding H2) ===

def test_verify_terminal_regrades_an_ungraded_terminal(tmp_path, monkeypatch):
    _setup_project(tmp_path, [_LANE_PASS], monkeypatch)
    run_id = create_dispatch("p", tier="lane", session_id="s")
    record_report(run_id, {"summary": "interim: waiting for the sleep"})
    close_dispatch(run_id)  # the false terminal
    result = verify_dispatch(run_id, note="real DONE arrived by hand-back",
                             allow_terminal=True)
    assert result["verdict"] == "verified"
    d = load_dispatch(run_id)
    assert d.is_terminal and d.verdict == "verified"
    reported = [t for t in d.transitions if t.get("state") == "reported"]
    assert any(t.get("reopened") for t in reported)
    assert all(t.get("by") == "cli-verify"
               for t in d.transitions if t.get("reopened"))


def test_verify_terminal_refuses_a_graded_terminal(tmp_path, monkeypatch):
    _setup_project(tmp_path, [_LANE_PASS], monkeypatch)
    run_id = create_dispatch("p", tier="lane", session_id="s")
    record_report(run_id, {"summary": "done"})
    from fleetproof.ledger import record_verdict
    record_verdict(run_id, "verified", detail="1/1")
    close_dispatch(run_id)
    with pytest.raises(LedgerError, match="verdict"):
        verify_dispatch(run_id, allow_terminal=True)


def test_plain_verify_refusal_names_the_terminal_exit(tmp_path, monkeypatch):
    _setup_project(tmp_path, [_LANE_PASS], monkeypatch)
    run_id = create_dispatch("p", tier="lane", session_id="s")
    record_report(run_id, {"summary": "interim"})
    close_dispatch(run_id)
    with pytest.raises(LedgerError, match="--terminal"):
        verify_dispatch(run_id)


def test_reopen_refuses_a_live_dispatch(tmp_path, monkeypatch):
    _setup_project(tmp_path, [_LANE_PASS], monkeypatch)
    run_id = create_dispatch("p", tier="lane", session_id="s")
    with pytest.raises(LedgerError, match="not terminal"):
        reopen_for_verify(run_id, "summary")


def test_cli_verify_terminal_flag_is_wired(tmp_path, monkeypatch, capsys):
    _setup_project(tmp_path, [_LANE_PASS], monkeypatch)
    run_id = create_dispatch("p", tier="lane", session_id="s")
    record_report(run_id, {"summary": "interim"})
    close_dispatch(run_id)
    assert main(["dispatch", "verify", run_id, "--terminal",
                 "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"] == "verified"


# === Advisory-failure visibility (trial finding 1a) ===

def test_verified_verdict_carries_failing_advisory_ids(tmp_path, monkeypatch, capsys):
    _setup_project(tmp_path, [_LANE_PASS, _LANE_WARN_FAIL], monkeypatch)
    _feed(monkeypatch, _start_payload())
    assert subagent_start_main() == 0
    _feed(monkeypatch, _stop_payload(message="Done, all tests pass."))
    assert subagent_stop_main() == 0
    d = list_dispatches()[0]
    assert d.verdict == "verified"
    assert d.advisory_failures == ["lane-warn"]
    assert "lane-warn" in (d.verdict_detail or "")
    assert "lane-warn" in capsys.readouterr().err


def test_verified_star_label_and_board_footer(tmp_path, monkeypatch, capsys):
    from fleetproof.report import verdict_label
    _setup_project(tmp_path, [_LANE_PASS, _LANE_WARN_FAIL], monkeypatch)
    _feed(monkeypatch, _start_payload())
    assert subagent_start_main() == 0
    _feed(monkeypatch, _stop_payload(message="Done."))
    assert subagent_stop_main() == 0
    capsys.readouterr()
    d = list_dispatches()[0]
    assert verdict_label(d) == "verified*"
    assert main(["fleet", "--all"]) == 0
    out = capsys.readouterr().out
    assert "verified*" in out
    assert "FAILING advisory" in out


def test_bridge_stop_announces_advisory_failures(tmp_path, monkeypatch, capsys):
    _setup_project(tmp_path, [_LANE_PASS, _LANE_WARN_FAIL], monkeypatch)
    _feed(monkeypatch, _start_payload())
    assert subagent_start_main() == 0
    _feed(monkeypatch, _stop_payload(message="Done."))
    assert subagent_stop_main() == 0
    capsys.readouterr()
    decision, _ = stop_gate()  # session env set by the hooks above
    err = capsys.readouterr().err
    assert "FAILING advisory" in err and "lane-warn" in err


def test_bridge_own_pass_with_warn_failure_says_so_on_stderr(tmp_path, monkeypatch, capsys):
    _setup_project(tmp_path, [_PASS, dict(_FAIL, id="warn-only", block=False)],
                   monkeypatch)
    decision, _ = stop_gate()
    assert decision is None  # still a pass
    assert "warn-only" in capsys.readouterr().err


# === Board and ledger hygiene ===

def test_dispatch_sweep_closes_stale_rows(tmp_path, monkeypatch, capsys):
    _setup_project(tmp_path, [_PASS], monkeypatch)
    stale = create_dispatch("old", tier="lane", session_id="s")
    # Backdate by rewriting _root.json's started_at.
    root_path = runlog.runs_dir() / stale / "_root.json"
    root = json.loads(root_path.read_text(encoding="utf-8"))
    root["started_at"] = "2026-01-01T00:00:00+00:00"
    root_path.write_text(json.dumps(root), encoding="utf-8")
    fresh = create_dispatch("new", tier="lane", session_id="s")

    assert main(["dispatch", "sweep", "--older-than", "2d", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert stale in out and fresh not in out
    assert not load_dispatch(stale).is_terminal  # dry run closed nothing

    assert main(["dispatch", "sweep", "--older-than", "2d",
                 "--reason", "stale August rows"]) == 0
    capsys.readouterr()
    assert load_dispatch(stale).is_terminal
    assert load_dispatch(stale).terminate_reason == "parked: stale August rows"
    assert not load_dispatch(fresh).is_terminal


def test_fleet_scopes_to_hook_session_by_default(tmp_path, monkeypatch, capsys):
    _setup_project(tmp_path, [_PASS], monkeypatch)
    mine = create_dispatch("mine", tier="lane", session_id="sess-a")
    other = create_dispatch("other", tier="lane", session_id="sess-b")
    monkeypatch.setenv(runlog.SESSION_ID_ENV, "sess-a")
    assert main(["fleet"]) == 0
    out = capsys.readouterr().out
    assert mine in out and other not in out
    assert "scoped to session" in out
    assert main(["fleet", "--all"]) == 0
    out = capsys.readouterr().out
    assert mine in out and other in out


def test_fleet_without_session_lists_everything_unchanged(tmp_path, monkeypatch, capsys):
    _setup_project(tmp_path, [_PASS], monkeypatch)
    a = create_dispatch("a", tier="lane", session_id="sess-a")
    b = create_dispatch("b", tier="lane", session_id="sess-b")
    assert main(["fleet"]) == 0
    out = capsys.readouterr().out
    assert a in out and b in out and "scoped to session" not in out


def test_footer_id_lists_are_capped(tmp_path, monkeypatch):
    from fleetproof.ledger import (FOOTER_ID_CAP, list_ungraded_terminations,
                                   ungraded_termination_line)
    _setup_project(tmp_path, [_PASS], monkeypatch)
    for i in range(FOOTER_ID_CAP + 3):
        run_id = create_dispatch(f"p{i}", tier="lane", session_id="s")
        record_report(run_id, {"summary": "x"})
        close_dispatch(run_id)
    line = ungraded_termination_line(list_ungraded_terminations("s"), scope_known=True)
    assert "+3 more" in line


def test_cleanup_max_runs_bounds_the_ledger(tmp_path, monkeypatch, capsys):
    _setup_project(tmp_path, [_PASS], monkeypatch)
    for i in range(5):
        create_dispatch(f"p{i}", tier="lane", session_id="s")
    # Distinct run-id timestamps are not guaranteed within a second; count dirs.
    total = len(list(runlog.runs_dir().iterdir()))
    assert main(["cleanup", "--older-than-days", "9999", "--max-runs", "2"]) == 0
    capsys.readouterr()
    kept = [d for d in runlog.runs_dir().iterdir() if d.is_dir()]
    assert len(kept) == min(2, total)


def test_ledger_writes_its_own_gitignore(tmp_path, monkeypatch):
    _setup_project(tmp_path, [_PASS], monkeypatch)
    create_dispatch("p", tier="lane", session_id="s")
    gitignore = tmp_path / ".fleetproof" / ".gitignore"
    assert gitignore.exists()
    assert "*" in gitignore.read_text(encoding="utf-8")


def test_existing_gitignore_is_never_touched(tmp_path, monkeypatch):
    _setup_project(tmp_path, [_PASS], monkeypatch)
    gitignore = tmp_path / ".fleetproof" / ".gitignore"
    gitignore.write_text("runs/\n", encoding="utf-8")
    create_dispatch("p", tier="lane", session_id="s")
    assert gitignore.read_text(encoding="utf-8") == "runs/\n"


def test_version_stamped_in_root_records(tmp_path, monkeypatch):
    import fleetproof
    _setup_project(tmp_path, [_PASS], monkeypatch)
    run_id = create_dispatch("p", tier="lane", session_id="s")
    root = json.loads((runlog.runs_dir() / run_id / "_root.json")
                      .read_text(encoding="utf-8"))
    assert root["fleetproof_version"] == fleetproof.__version__


# === consult_output_on_nonzero (trial side finding) ===

_PRINT_FAIL0_EXIT1 = (f'"{sys.executable}" -c "print(\'PASS=3 FAIL=0\'); '
                      'raise SystemExit(1)"')


def test_regex_still_gated_on_exit_by_default(tmp_path, monkeypatch):
    _setup_project(tmp_path, [
        {"id": "rx", "run": _PRINT_FAIL0_EXIT1,
         "expect": {"regex": "FAIL=0"}, "block": True},
    ], monkeypatch)
    decision, _ = stop_gate()
    assert decision.get("decision") == "block"
    assert "output not consulted" in decision["reason"]


def test_regex_consult_opt_in_lets_output_decide(tmp_path, monkeypatch):
    _setup_project(tmp_path, [
        {"id": "rx", "run": _PRINT_FAIL0_EXIT1,
         "expect": {"regex": "FAIL=0"}, "block": True,
         "consult_output_on_nonzero": True},
    ], monkeypatch)
    decision, _ = stop_gate()
    assert decision is None


def test_consult_flag_refused_on_non_regex_checks(tmp_path, monkeypatch):
    from fleetproof.checks import CheckSpecError, load_checks
    _setup_project(tmp_path, [
        {"id": "x", "run": "true", "expect": "exit0",
         "consult_output_on_nonzero": True},
    ], monkeypatch)
    with pytest.raises(CheckSpecError, match="regex"):
        load_checks()
