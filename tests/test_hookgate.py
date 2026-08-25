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


# === the bridge Stop gate is tier-scoped ===

def _graded_check_ids() -> list[str]:
    """Check ids from the verdict the gate just recorded, in spec order."""
    for run in runlog.list_run_records():
        for sub in run.sub_invocations:
            if sub.tool == "fleetproof" and sub.subcmd == "check":
                payload = sub.load_output()
                if isinstance(payload, dict):
                    return [c["id"] for c in payload.get("checks", [])]
    return []


def test_bridge_gate_on_an_untiered_spec_selects_exactly_what_untiered_did(
        tmp_path, monkeypatch):
    # The bridge grades at tier="bridge", which is not a narrowing for a v0.1 spec:
    # select_checks("bridge") is bridge-tier checks PLUS every untiered check, so an
    # entirely untiered spec still selects all of them and this gate is unchanged.
    from fleetproof.checker import select_checks
    from fleetproof.checks import load_checks
    _setup_project(tmp_path, [
        _PASS,
        dict(_FAIL, id="also-bad"),
        dict(_PASS, id="advisory", block=False),
    ], monkeypatch)

    checks = load_checks()
    assert ([c.id for c in select_checks(checks, "bridge")]
            == [c.id for c in select_checks(checks, None)])

    decision, code = stop_gate()
    assert code == 0
    assert decision["decision"] == "block"
    assert "also-bad" in decision["reason"]
    # And every check in the spec really was graded, not some subset of them.
    assert _graded_check_ids() == ["ok", "also-bad", "advisory"]


def test_leaf_tier_check_does_not_gate_the_bridge_stop(tmp_path, monkeypatch):
    # A leaf-tier check describes some subagent's work, not the session's. Grading
    # the bridge on it holds the bridge responsible for a claim it never made — and
    # worse, it is unfixable from the bridge's own turn.
    _setup_project(tmp_path, [_PASS, dict(_FAIL, id="leaf-bad", tier="leaf")],
                   monkeypatch)
    decision, code = stop_gate()
    assert code == 0
    assert decision is None
    assert _graded_check_ids() == ["ok"]


def test_bridge_tier_check_still_gates_the_bridge_stop(tmp_path, monkeypatch):
    # The other half of the scoping: a check the operator declared as the bridge's
    # own must keep blocking, or tier-scoping would have quietly disarmed the gate.
    _setup_project(tmp_path, [dict(_FAIL, id="bridge-bad", tier="bridge"),
                              dict(_PASS, id="leaf-ok", tier="leaf")], monkeypatch)
    decision, code = stop_gate()
    assert decision["decision"] == "block"
    assert "bridge-bad" in decision["reason"]
    assert _graded_check_ids() == ["bridge-bad"]


# === subagent capture (SubagentStart) ===

_LANE_PASS = {"id": "lane-ok", "run": f'"{sys.executable}" -c "raise SystemExit(0)"',
              "expect": "exit0", "tier": "lane"}
_LANE_ARTIFACT = {"id": "lane-artifact", "expect": {"file_exists": "artifact.txt"},
                  "tier": "lane", "block": True}
_LEAF_ONLY = {"id": "leaf-ok", "run": f'"{sys.executable}" -c "raise SystemExit(0)"',
              "expect": "exit0", "tier": "leaf"}


def _feed(monkeypatch, payload: dict) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))


def _start_payload(agent_id="agent-1", agent_type="tester", session="sess-fleet") -> dict:
    return {
        "session_id": session,
        "transcript_path": "/tmp/t.jsonl",
        "cwd": ".",
        "hook_event_name": "SubagentStart",
        "agent_id": agent_id,
        "agent_type": agent_type,
    }


def _stop_payload(message="I did the thing.", agent_id="agent-1",
                  agent_type="tester", session="sess-fleet") -> dict:
    payload = _start_payload(agent_id, agent_type, session)
    payload["hook_event_name"] = "SubagentStop"
    payload["last_assistant_message"] = message
    return payload


def test_subagent_start_creates_lane_dispatch_with_agent_block(tmp_path, monkeypatch):
    from fleetproof.hookgate import subagent_start_main
    from fleetproof.ledger import list_dispatches
    _setup_project(tmp_path, [_LANE_PASS], monkeypatch)
    _feed(monkeypatch, _start_payload())

    assert subagent_start_main() == 0

    dispatches = list_dispatches()
    assert len(dispatches) == 1
    d = dispatches[0]
    assert d.tier == "lane"
    # No intent declared this tier — it is the capture default, and the one
    # provenance field that could reveal an intent miss must say so instead of
    # asserting the opposite (observed in a field deployment on Windows).
    assert d.tier_source == "defaulted"
    assert d.agent == {"agent_id": "agent-1", "agent_type": "tester", "capture": "start"}
    assert d.session_id == "sess-fleet"
    assert d.state == "dispatched"
    assert d.transitions[0]["by"] == "hook"
    # The prompt is not in the payload, and the record says so instead of guessing.
    assert "[uncaptured]" in d.prompt and "tester" in d.prompt


def test_subagent_start_emits_nothing_on_success(tmp_path, monkeypatch, capsys):
    from fleetproof.hookgate import subagent_start_main
    _setup_project(tmp_path, [_LANE_PASS], monkeypatch)
    _feed(monkeypatch, _start_payload())
    assert subagent_start_main() == 0
    captured = capsys.readouterr()
    assert captured.out == ""


def test_subagent_start_survives_a_garbage_payload(tmp_path, monkeypatch):
    # Context-only hook: it can neither block nor be allowed to crash the spawn.
    from fleetproof.hookgate import subagent_start_main
    _setup_project(tmp_path, [_LANE_PASS], monkeypatch)
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json at all"))
    assert subagent_start_main() == 0


# === subagent gate (SubagentStop) ===

def test_unpaired_stop_with_no_agent_type_records_an_orphan_not_a_dispatch(
        tmp_path, monkeypatch, capsys):
    # Harness-internal helper agents emit SubagentStop with no SubagentStart and
    # no agent_type. Back-filling those as graded lane dispatches manufactured
    # phantom verified verdicts (observed in a field deployment on Windows): an
    # unpaired stop is a sighting, never a graded unit of work.
    from fleetproof.hookgate import subagent_stop_main
    from fleetproof.ledger import list_dispatches, list_orphan_stops
    _setup_project(tmp_path, [_LANE_PASS], monkeypatch)
    payload = _stop_payload()
    payload["agent_type"] = None
    _feed(monkeypatch, payload)

    assert subagent_stop_main() == 0
    captured = capsys.readouterr()
    assert captured.out == ""  # the stop goes through: no block, no grading
    assert "orphan" in captured.err

    assert list_dispatches() == []
    orphans = list_orphan_stops()
    assert len(orphans) == 1
    assert orphans[0]["agent_id"] == "agent-1"
    assert orphans[0]["agent_type"] is None
    assert orphans[0]["session_id"] == "sess-fleet"
    assert orphans[0]["last_assistant_message"] == "I did the thing."


def test_unpaired_stop_with_a_real_agent_type_is_still_an_orphan(
        tmp_path, monkeypatch, capsys):
    # Same rule when the agent_type looks legitimate: without a start (or a
    # dispatch to join), there is no contract to grade against, and a verdict
    # on a manufactured record is exactly the phantom A1 exists to prevent.
    from fleetproof.hookgate import subagent_stop_main
    from fleetproof.ledger import list_dispatches, list_orphan_stops
    _setup_project(tmp_path, [_LANE_PASS], monkeypatch)
    _feed(monkeypatch, _stop_payload(message="x" * 500))

    assert subagent_stop_main() == 0
    assert capsys.readouterr().out == ""

    assert list_dispatches() == []
    orphans = list_orphan_stops()
    assert len(orphans) == 1
    assert orphans[0]["agent_type"] == "tester"
    # Only the first 200 chars of the message are kept: an orphan is a pointer
    # for the operator, not a transcript store.
    assert orphans[0]["last_assistant_message"] == "x" * 200


def test_orphan_stops_filter_by_session(tmp_path, monkeypatch):
    from fleetproof.hookgate import subagent_stop_main
    from fleetproof.ledger import list_orphan_stops
    _setup_project(tmp_path, [_LANE_PASS], monkeypatch)
    _feed(monkeypatch, _stop_payload(session="sess-one"))
    subagent_stop_main()
    _feed(monkeypatch, _stop_payload(session="sess-two"))
    subagent_stop_main()

    assert len(list_orphan_stops()) == 2
    assert len(list_orphan_stops(session_id="sess-one")) == 1
    assert list_orphan_stops(session_id="sess-one")[0]["session_id"] == "sess-one"


def test_stop_reuses_the_dispatch_its_start_created(tmp_path, monkeypatch):
    from fleetproof.hookgate import subagent_start_main, subagent_stop_main
    from fleetproof.ledger import list_dispatches
    _setup_project(tmp_path, [_LANE_PASS], monkeypatch)

    _feed(monkeypatch, _start_payload())
    subagent_start_main()
    _feed(monkeypatch, _stop_payload())
    subagent_stop_main()

    dispatches = list_dispatches()
    assert len(dispatches) == 1  # one agent, one record
    assert dispatches[0].agent["capture"] == "start"
    assert [t["state"] for t in dispatches[0].transitions] == [
        "dispatched", "reported", "verified", "terminated",
    ]


def test_empty_last_assistant_message_blocks_without_transitioning(tmp_path, monkeypatch, capsys):
    from fleetproof.hookgate import subagent_start_main, subagent_stop_main
    from fleetproof.ledger import list_dispatches
    _setup_project(tmp_path, [_LANE_PASS], monkeypatch)
    _feed(monkeypatch, _start_payload())
    subagent_start_main()

    _feed(monkeypatch, _stop_payload(message="   "))
    assert subagent_stop_main() == 0
    decision = json.loads(capsys.readouterr().out)
    assert decision["decision"] == "block"
    assert "report-before-idle" in decision["reason"]
    assert decision["hookSpecificOutput"]["hookEventName"] == "SubagentStop"

    # Silence must not be laundered into a report.
    d = list_dispatches()[0]
    assert d.state == "dispatched"
    assert d.has_report is False


def test_retry_loop_contradicted_then_reported_then_verified(tmp_path, monkeypatch, capsys):
    # The whole point of allowing contradicted -> reported: the gate blocks, the
    # same agent gets its turn back, fixes the work, and reports again.
    from fleetproof.hookgate import subagent_start_main, subagent_stop_main
    from fleetproof.ledger import list_dispatches
    _setup_project(tmp_path, [_LANE_ARTIFACT], monkeypatch)
    _feed(monkeypatch, _start_payload())
    subagent_start_main()

    # First stop: the artifact it claims to have produced does not exist.
    _feed(monkeypatch, _stop_payload(message="Shipped the artifact."))
    assert subagent_stop_main() == 0
    first = json.loads(capsys.readouterr().out)
    assert first["decision"] == "block"
    assert "lane-artifact" in first["reason"]
    d = list_dispatches()[0]
    assert d.state == "contradicted"
    assert d.is_terminal is False  # still retryable

    # The agent actually does the work, then stops again.
    (tmp_path / "artifact.txt").write_text("real", encoding="utf-8")
    _feed(monkeypatch, _stop_payload(message="Really shipped it this time."))
    assert subagent_stop_main() == 0
    assert capsys.readouterr().out == ""  # allowed to stop

    d = list_dispatches()[0]
    assert [t["state"] for t in d.transitions] == [
        "dispatched", "reported", "contradicted", "reported", "verified", "terminated",
    ]
    assert d.verdict == "verified"
    assert d.load_report()["summary"] == "Really shipped it this time."
    # The contradiction names which check failed, and the verdict is the checker's.
    contradiction = d.transitions[2]
    assert contradiction["detail"] == "lane-artifact"
    assert contradiction["by"] == "checker-via-hook"


def test_empty_tier_selection_terminates_without_a_verdict(tmp_path, monkeypatch, capsys):
    # An absent grade is not a passing grade: nothing declared for this tier means
    # no verdict at all, and the board shows the dispatch as never verified.
    from fleetproof.hookgate import subagent_start_main, subagent_stop_main
    from fleetproof.ledger import list_dispatches
    _setup_project(tmp_path, [_LEAF_ONLY], monkeypatch)
    _feed(monkeypatch, _start_payload())
    subagent_start_main()

    _feed(monkeypatch, _stop_payload())
    assert subagent_stop_main() == 0
    assert capsys.readouterr().out == ""

    d = list_dispatches()[0]
    assert d.state == "terminated"
    assert d.verdict is None
    assert [t["state"] for t in d.transitions] == ["dispatched", "reported", "terminated"]
    # And no checker verdict was recorded for it either.
    checker_runs = [
        s for r in runlog.list_run_records() for s in r.sub_invocations
        if s.tool == "fleetproof" and s.subcmd == "check"
    ]
    assert checker_runs == []


def test_no_spec_terminates_without_a_verdict(tmp_path, monkeypatch):
    from fleetproof.hookgate import subagent_start_main, subagent_stop_main
    from fleetproof.ledger import list_dispatches
    marker = tmp_path / ".fleetproof"
    marker.mkdir()
    monkeypatch.chdir(tmp_path)
    runlog.set_runs_dir(marker / "runs")

    _feed(monkeypatch, _start_payload())
    subagent_start_main()
    _feed(monkeypatch, _stop_payload())
    assert subagent_stop_main() == 0
    d = list_dispatches()[0]
    assert d.state == "terminated"
    assert d.verdict is None


def test_pin_drift_blocks_the_subagent_stop(tmp_path, monkeypatch, capsys):
    # The spec the work was dispatched against is the contract. If it changed
    # mid-flight, we refuse to grade against the new one.
    from fleetproof.checks import SPEC_DRIFT_NOTE
    from fleetproof.hookgate import subagent_start_main, subagent_stop_main
    from fleetproof.ledger import list_dispatches
    _setup_project(tmp_path, [_LANE_PASS], monkeypatch)
    _feed(monkeypatch, _start_payload())
    subagent_start_main()
    pinned = list_dispatches()[0].spec_sha256_pinned
    assert pinned is not None

    _write_checks(tmp_path, [dict(_LANE_PASS, description="weakened after dispatch")])
    _feed(monkeypatch, _stop_payload())
    assert subagent_stop_main() == 0
    decision = json.loads(capsys.readouterr().out)
    assert decision["decision"] == "block"
    assert "check spec changed after this work was dispatched" in decision["reason"]
    assert SPEC_DRIFT_NOTE in decision["reason"]

    d = list_dispatches()[0]
    assert d.state == "reported"  # the claim is on record; the grade is not
    assert d.verdict is None
    assert d.is_terminal is False


def test_pin_drift_does_not_block_a_tier_with_nothing_to_grade(
        tmp_path, monkeypatch, capsys):
    # A spec edit mid-flight wedged EVERY open dispatch, including tiers whose
    # selection is empty — unwedging one lane wedged everyone else (observed in
    # a field deployment on Windows). With nothing runnable there is nothing
    # the drift could corrupt: terminate ungraded as usual, note the drift.
    from fleetproof.hookgate import subagent_start_main, subagent_stop_main
    from fleetproof.ledger import list_dispatches, write_intent
    _setup_project(tmp_path, [_LANE_PASS], monkeypatch)
    write_intent("tester", _INTENT_PROMPT, tier="coordinator")
    _feed(monkeypatch, _start_payload())
    subagent_start_main()

    _write_checks(tmp_path, [dict(_LANE_PASS, description="edited mid-flight")])
    _feed(monkeypatch, _stop_payload(message="Coordinated; nothing gradeable here."))
    assert subagent_stop_main() == 0
    captured = capsys.readouterr()
    assert captured.out == ""  # the stop is allowed
    assert "drift" in captured.err  # but the drift is still said out loud

    d = list_dispatches()[0]
    assert d.state == "terminated"
    assert d.verdict is None  # ungraded, exactly as an undrifted empty tier


def _write_check_script(tmp_path: Path, body: str) -> Path:
    scripts = tmp_path / ".fleetproof" / "checks"
    scripts.mkdir(parents=True, exist_ok=True)
    path = scripts / "verify.py"
    path.write_text(body, encoding="utf-8")
    return path


def test_check_script_rewrite_mid_dispatch_blocks_naming_the_scripts(
        tmp_path, monkeypatch, capsys):
    # The spec pin covered checks.json alone, so the check *scripts* could be
    # rewritten mid-dispatch — including by the graded lane itself — with zero
    # drift signal (observed in a field deployment on Windows). The tree pin
    # closes that: the graders are part of the spec.
    from fleetproof.hookgate import subagent_start_main, subagent_stop_main
    from fleetproof.ledger import list_dispatches
    _setup_project(tmp_path, [_LANE_PASS], monkeypatch)
    script = _write_check_script(tmp_path, "raise SystemExit(0)\n")
    _feed(monkeypatch, _start_payload())
    subagent_start_main()
    d = list_dispatches()[0]
    assert d.tree_sha256_pinned is not None
    assert d.spec_sha256_pinned is not None

    script.write_text("raise SystemExit(0)  # weakened mid-flight\n", encoding="utf-8")
    _feed(monkeypatch, _stop_payload())
    assert subagent_stop_main() == 0
    decision = json.loads(capsys.readouterr().out)
    assert decision["decision"] == "block"
    assert decision["reason"].startswith("[FLEETPROOF GATE")
    # The block names WHICH pin drifted: the spec file did not change, the
    # scripts did, and an operator unwedging this needs to know where to look.
    assert "check scripts under .fleetproof/checks/" in decision["reason"]
    assert "check spec changed" not in decision["reason"]

    d = list_dispatches()[0]
    assert d.state == "reported"  # the claim is on record; the grade is not
    assert d.verdict is None


def test_spec_drift_block_names_the_spec_not_the_scripts(tmp_path, monkeypatch, capsys):
    from fleetproof.hookgate import subagent_start_main, subagent_stop_main
    _setup_project(tmp_path, [_LANE_PASS], monkeypatch)
    _write_check_script(tmp_path, "raise SystemExit(0)\n")
    _feed(monkeypatch, _start_payload())
    subagent_start_main()

    _write_checks(tmp_path, [dict(_LANE_PASS, description="weakened after dispatch")])
    _feed(monkeypatch, _stop_payload())
    assert subagent_stop_main() == 0
    decision = json.loads(capsys.readouterr().out)
    assert decision["decision"] == "block"
    assert "check spec changed after this work was dispatched" in decision["reason"]
    assert "check scripts under .fleetproof/checks/" not in decision["reason"]


def test_record_without_a_tree_pin_is_not_tree_tested(tmp_path, monkeypatch, capsys):
    # Additive compatibility: a dispatch written before the tree pin existed
    # carries no tree_sha256_pinned, and must not start failing drift tests it
    # never agreed to.
    from fleetproof.hookgate import subagent_start_main, subagent_stop_main
    from fleetproof.ledger import list_dispatches
    _setup_project(tmp_path, [_LANE_PASS], monkeypatch)
    script = _write_check_script(tmp_path, "raise SystemExit(0)\n")
    _feed(monkeypatch, _start_payload())
    subagent_start_main()

    d = list_dispatches()[0]
    raw = json.loads((d.run_dir / "dispatch.json").read_text(encoding="utf-8"))
    del raw["tree_sha256_pinned"]
    (d.run_dir / "dispatch.json").write_text(json.dumps(raw), encoding="utf-8")

    script.write_text("raise SystemExit(0)  # changed\n", encoding="utf-8")
    _feed(monkeypatch, _stop_payload())
    assert subagent_stop_main() == 0
    assert capsys.readouterr().out == ""  # graded normally, no drift block
    assert list_dispatches()[0].verdict == "verified"


def test_tree_drift_with_nothing_runnable_allows_the_stop_ungraded(
        tmp_path, monkeypatch, capsys):
    # Same A5 rule for the tree pin: with nothing runnable at this tier there
    # is nothing the rewritten scripts could corrupt — note it, do not wedge.
    from fleetproof.hookgate import subagent_start_main, subagent_stop_main
    from fleetproof.ledger import list_dispatches, write_intent
    _setup_project(tmp_path, [_LANE_PASS], monkeypatch)
    script = _write_check_script(tmp_path, "raise SystemExit(0)\n")
    write_intent("tester", _INTENT_PROMPT, tier="coordinator")
    _feed(monkeypatch, _start_payload())
    subagent_start_main()

    script.write_text("raise SystemExit(0)  # changed\n", encoding="utf-8")
    _feed(monkeypatch, _stop_payload(message="Coordinated; nothing gradeable here."))
    assert subagent_stop_main() == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "drift" in captured.err

    d = list_dispatches()[0]
    assert d.state == "terminated"
    assert d.verdict is None


def test_stop_gate_pass_annotates_check_script_drift(tmp_path, monkeypatch):
    # Session-baseline drift on the bridge Stop gate gains the tree hash too:
    # a script rewritten mid-session is announced, exactly like a spec edit.
    from fleetproof.checks import TREE_DRIFT_NOTE
    _setup_project(tmp_path, [_PASS], monkeypatch)
    monkeypatch.setenv(runlog.SESSION_ID_ENV, "sess-tree-drift")

    monkeypatch.setenv(runlog.RUN_ID_ENV, "20260101-000001-aaaa01")
    first, code = stop_gate()
    assert first is None  # first verdict is the baseline

    _write_check_script(tmp_path, "raise SystemExit(0)\n")
    monkeypatch.setenv(runlog.RUN_ID_ENV, "20260101-000002-aaaa02")
    second, code = stop_gate()
    assert second is not None
    assert second.get("decision") != "block"  # a pass with drift still passes
    assert TREE_DRIFT_NOTE in second["hookSpecificOutput"]["additionalContext"]


def test_subagent_gate_fails_open_but_loudly_when_it_breaks(tmp_path, monkeypatch, capsys):
    # A bug in the gate must not wedge the fleet, but must not look like a pass.
    from fleetproof import hookgate
    _setup_project(tmp_path, [_LANE_PASS], monkeypatch)

    def _boom(payload):
        raise RuntimeError("gate exploded")

    monkeypatch.setattr(hookgate, "subagent_stop", _boom)
    _feed(monkeypatch, _stop_payload())
    assert hookgate.subagent_stop_main() == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "failed open" in captured.err


def test_stop_gate_grades_from_the_project_root_not_the_shell_cwd(tmp_path, monkeypatch):
    # The artifact exists at the project root; the shell happens to be deep in
    # a subdirectory when the hook fires. Grading from the shell's cwd read
    # this as missing — a false red on work that was actually done.
    _setup_project(tmp_path, [
        {"id": "artifact", "expect": {"file_exists": "artifact.txt"}, "block": True},
    ], monkeypatch)
    (tmp_path / "artifact.txt").write_text("real", encoding="utf-8")
    sub = tmp_path / "deep" / "inside"
    sub.mkdir(parents=True)
    monkeypatch.chdir(sub)

    decision, code = stop_gate()
    assert code == 0
    assert decision is None


def test_gate_evidence_context_names_the_cwd(tmp_path, monkeypatch):
    _setup_project(tmp_path, [_FAIL], monkeypatch)
    decision, code = stop_gate()
    assert decision["decision"] == "block"
    ctx = decision["hookSpecificOutput"]["additionalContext"]
    assert any(line.startswith("cwd: ") for line in ctx.splitlines())


# === bridge Stop hook: ledger sweep ===

def _make_dispatch(tier="lane", report=False):
    from fleetproof.ledger import create_dispatch, record_report
    run_id = create_dispatch("bridge-made work", tier=tier)
    if report:
        record_report(run_id, {"summary": "claimed done"})
    return run_id


def test_stop_gate_blocks_on_a_stalled_dispatch(tmp_path, monkeypatch):
    _setup_project(tmp_path, [_PASS], monkeypatch)
    monkeypatch.setenv(runlog.SESSION_ID_ENV, "sess-sweep")
    stalled = _make_dispatch(report=True)

    decision, code = stop_gate()
    assert code == 0
    assert decision["decision"] == "block"
    assert stalled in decision["reason"]
    assert "reported but never terminated" in decision["reason"]
    assert "[stalled]" in decision["hookSpecificOutput"]["additionalContext"]


def test_stop_gate_does_not_block_on_a_running_dispatch(tmp_path, monkeypatch):
    # A background agent still working is not a fault — report it, do not gate it.
    _setup_project(tmp_path, [_PASS], monkeypatch)
    monkeypatch.setenv(runlog.SESSION_ID_ENV, "sess-sweep-2")
    running = _make_dispatch()

    decision, code = stop_gate()
    assert code == 0
    assert decision is not None
    assert "decision" not in decision
    ctx = decision["hookSpecificOutput"]["additionalContext"]
    assert f"[still in fleet] {running}" in ctx


def test_stop_gate_ignores_terminated_and_other_sessions(tmp_path, monkeypatch):
    from fleetproof.ledger import close_dispatch
    _setup_project(tmp_path, [_PASS], monkeypatch)
    monkeypatch.setenv(runlog.SESSION_ID_ENV, "sess-mine")
    done = _make_dispatch(report=True)
    close_dispatch(done)
    monkeypatch.setenv(runlog.SESSION_ID_ENV, "sess-theirs")
    _make_dispatch(report=True)

    monkeypatch.setenv(runlog.SESSION_ID_ENV, "sess-mine")
    decision, code = stop_gate()
    assert decision is None  # my session is clean; theirs is not my business


def test_stop_gate_output_unchanged_when_session_has_no_dispatches(tmp_path, monkeypatch):
    # The sweep must add nothing when there is nothing to sweep.
    _setup_project(tmp_path, [_FAIL], monkeypatch)
    monkeypatch.setenv(runlog.SESSION_ID_ENV, "sess-no-dispatches")
    decision, code = stop_gate()
    assert set(decision) == {"decision", "reason", "hookSpecificOutput"}
    assert "reported but never terminated" not in decision["reason"]
    assert "[stalled]" not in decision["hookSpecificOutput"]["additionalContext"]
    assert "[still in fleet]" not in decision["hookSpecificOutput"]["additionalContext"]


def test_stop_gate_blocks_on_both_grounds_at_once(tmp_path, monkeypatch):
    _setup_project(tmp_path, [_FAIL], monkeypatch)
    monkeypatch.setenv(runlog.SESSION_ID_ENV, "sess-both")
    stalled = _make_dispatch(report=True)
    decision, code = stop_gate()
    assert decision["decision"] == "block"
    assert "blocking check(s) failed" in decision["reason"]
    assert stalled in decision["reason"]


# === the block must not read like a chat message ===

def test_block_reason_leads_with_the_gate_marker_and_carries_everything(
        tmp_path, monkeypatch):
    # A block rendered as prose was read as conversational input and argued
    # with — one agent restated its report through 19 block/retry cycles
    # (observed in a field deployment on Windows). The reason leads with an
    # unmistakable marker and carries the full picture in one payload.
    _setup_project(tmp_path, [_FAIL, dict(_PASS, id="fine")], monkeypatch)
    decision, code = stop_gate()
    reason = decision["reason"]
    assert reason.startswith("[FLEETPROOF GATE")
    assert "AUTOMATED BLOCK, NOT A USER MESSAGE" in reason
    assert "1/2 blocking check(s) failed" in reason
    assert "Per-check:" in reason
    assert "[FAIL] bad:" in reason and "[pass] fine:" in reason
    assert "evidence run" in reason
    assert "You may not stop by restating your report." in reason
    assert "park or re-dispatch" in reason and "do not loop" in reason
    # Belt and braces: the context channel is still populated alongside.
    assert decision["hookSpecificOutput"]["additionalContext"]


def test_block_reason_is_capped_but_keeps_the_exit_instructions(
        tmp_path, monkeypatch):
    # The reason must survive as a single payload: many failing checks get
    # their per-check lines truncated, never the escape instructions.
    many = [{"id": f"artifact-{i:03d}",
             "expect": {"file_exists": f"missing-{i:03d}.txt"}, "block": True}
            for i in range(60)]
    _setup_project(tmp_path, many, monkeypatch)
    decision, code = stop_gate()
    reason = decision["reason"]
    assert len(reason) <= 1600
    assert reason.startswith("[FLEETPROOF GATE")
    assert "blocking check(s) failed" in reason
    assert "do not loop" in reason  # the escape line survives the cap


def test_pin_drift_and_no_report_blocks_carry_the_gate_marker(
        tmp_path, monkeypatch, capsys):
    from fleetproof.hookgate import subagent_start_main, subagent_stop_main
    _setup_project(tmp_path, [_LANE_PASS], monkeypatch)
    _feed(monkeypatch, _start_payload())
    subagent_start_main()

    # No-report block: same marker, so a silent-idle block is not chat either.
    _feed(monkeypatch, _stop_payload(message="   "))
    assert subagent_stop_main() == 0
    decision = json.loads(capsys.readouterr().out)
    assert decision["reason"].startswith("[FLEETPROOF GATE")

    # Pin-drift block: likewise.
    _write_checks(tmp_path, [dict(_LANE_PASS, description="drifted mid-flight")])
    _feed(monkeypatch, _stop_payload())
    assert subagent_stop_main() == 0
    decision = json.loads(capsys.readouterr().out)
    assert decision["reason"].startswith("[FLEETPROOF GATE")
    assert "check spec changed after this work was dispatched" in decision["reason"]


# === plugin wiring ===

def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def test_hooks_json_wires_both_subagent_shims():
    data = json.loads((_repo_root() / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    hooks = data["hooks"]
    for event, script in (("SubagentStart", "subagent_start.py"),
                          ("SubagentStop", "subagent_stop.py")):
        entries = hooks[event]
        assert len(entries) == 1
        # Matchers on these events filter by agent_type; we want every subagent.
        assert "matcher" not in entries[0]
        command = entries[0]["hooks"][0]["command"]
        assert script in command
        assert entries[0]["hooks"][0]["type"] == "command"
        assert (_repo_root() / "scripts" / script).exists()


def test_subagent_shims_run_via_the_plugin_root_fallback(tmp_path):
    # Run the real shim as its own OS process with site-packages disabled (-S), so
    # the installed fleetproof is invisible and only the CLAUDE_PLUGIN_ROOT/src
    # fallback can satisfy the import. This is the separate-process property and
    # the no-pip-install promise, tested together.
    import subprocess
    env = dict(os.environ)
    env["CLAUDE_PLUGIN_ROOT"] = str(_repo_root())
    env["FLEETPROOF_RUNS_DIR"] = str(tmp_path / "runs")
    env.pop("PYTHONPATH", None)
    env.pop(runlog.SESSION_ID_ENV, None)

    start = subprocess.run(
        [sys.executable, "-S", str(_repo_root() / "scripts" / "subagent_start.py")],
        input=json.dumps(_start_payload(agent_id="shim-agent")),
        capture_output=True, text=True, cwd=str(tmp_path), env=env,
    )
    assert start.returncode == 0
    assert start.stdout == ""
    run_dirs = [p for p in (tmp_path / "runs").iterdir() if (p / "dispatch.json").exists()]
    assert len(run_dirs) == 1

    stop = subprocess.run(
        [sys.executable, "-S", str(_repo_root() / "scripts" / "subagent_stop.py")],
        input=json.dumps(_stop_payload(message="", agent_id="shim-agent")),
        capture_output=True, text=True, cwd=str(tmp_path), env=env,
    )
    assert stop.returncode == 0
    decision = json.loads(stop.stdout)
    assert decision["decision"] == "block"
    assert "report-before-idle" in decision["reason"]


# === dispatch-intent sidecar (SubagentStart) ===

_INTENT_PROMPT = "Refactor the widget module and prove it with the manifest checks."


def _intent_manifest(cmd_exit=0):
    cmd = f'"{sys.executable}" -c "raise SystemExit({cmd_exit})"'
    return {
        "deliverables": ["the widget refactor"],
        "checks": [{"id": "m-widget", "cmd": cmd}],
        "check_map": {"the widget refactor": ["m-widget"]},
        "notes": "declared by the dispatcher",
    }


def test_subagent_start_consumes_intent_sidecar(tmp_path, monkeypatch):
    # The whole point of the sidecar: the real prompt, manifest, and tier land
    # on the captured dispatch, and the intent file is gone afterwards.
    from fleetproof.hookgate import subagent_start_main
    from fleetproof.ledger import intent_path, list_dispatches, write_intent
    _setup_project(tmp_path, [_LANE_PASS], monkeypatch)
    path = write_intent("tester", _INTENT_PROMPT,
                        manifest=_intent_manifest(), tier="leaf")
    assert path.exists()

    _feed(monkeypatch, _start_payload())
    assert subagent_start_main() == 0

    d = list_dispatches()[0]
    assert d.prompt == _INTENT_PROMPT
    assert d.tier == "leaf"
    assert d.tier_source == "declared"
    assert d.manifest["deliverables"] == ["the widget refactor"]
    assert d.manifest["check_map"] == {"the widget refactor": ["m-widget"]}
    assert d.agent["capture"] == "start"
    # Consumed: one intent, one spawn.
    assert not path.exists()
    assert intent_path("tester") is not None  # the name itself stays mappable


def test_second_spawn_after_consumption_gets_the_placeholder(tmp_path, monkeypatch):
    from fleetproof.hookgate import subagent_start_main
    from fleetproof.ledger import list_dispatches, write_intent
    _setup_project(tmp_path, [_LANE_PASS], monkeypatch)
    write_intent("tester", _INTENT_PROMPT)

    _feed(monkeypatch, _start_payload(agent_id="agent-1"))
    subagent_start_main()
    _feed(monkeypatch, _start_payload(agent_id="agent-2"))
    subagent_start_main()

    # Two spawns in the same second sort unpredictably; key on the agent id.
    by_agent = {d.agent_id: d for d in list_dispatches()}
    assert by_agent["agent-1"].prompt == _INTENT_PROMPT
    assert "[uncaptured]" in by_agent["agent-2"].prompt
    assert by_agent["agent-2"].tier == "lane"


def test_intent_for_a_different_agent_type_is_left_alone(tmp_path, monkeypatch):
    from fleetproof.hookgate import subagent_start_main
    from fleetproof.ledger import intent_path, list_dispatches, write_intent
    _setup_project(tmp_path, [_LANE_PASS], monkeypatch)
    write_intent("someone-else", _INTENT_PROMPT)

    _feed(monkeypatch, _start_payload(agent_type="tester"))
    assert subagent_start_main() == 0

    assert "[uncaptured]" in list_dispatches()[0].prompt
    assert intent_path("someone-else").exists()  # not consumed by the wrong spawn


def test_malformed_intent_json_degrades_to_placeholder(tmp_path, monkeypatch, capsys):
    # The capture must survive a hand-mangled sidecar: placeholder path, loud
    # stderr note, and the bad file consumed so it cannot poison the next spawn.
    from fleetproof.hookgate import subagent_start_main
    from fleetproof.ledger import intent_path, intents_dir, list_dispatches
    _setup_project(tmp_path, [_LANE_PASS], monkeypatch)
    intents_dir().mkdir(parents=True, exist_ok=True)
    (intents_dir() / "tester.json").write_text("not json at all", encoding="utf-8")

    _feed(monkeypatch, _start_payload())
    assert subagent_start_main() == 0

    d = list_dispatches()[0]
    assert "[uncaptured]" in d.prompt
    assert "intent file tester.json" in capsys.readouterr().err
    assert not intent_path("tester").exists()


def test_malformed_manifest_in_a_valid_intent_degrades_with_a_note(
        tmp_path, monkeypatch, capsys):
    # A valid intent whose manifest is unusable keeps its real prompt; the
    # manifest degrades to derive-from-prompt with the failure written into the
    # manifest notes, where it survives on the dispatch record itself.
    from fleetproof.hookgate import subagent_start_main
    from fleetproof.ledger import INTENT_MANIFEST_MALFORMED, intents_dir, list_dispatches
    _setup_project(tmp_path, [_LANE_PASS], monkeypatch)
    intents_dir().mkdir(parents=True, exist_ok=True)
    (intents_dir() / "tester.json").write_text(json.dumps({
        "agent_type": "tester",
        "prompt": _INTENT_PROMPT,
        "manifest": {"deliverables": "not-an-array"},
    }), encoding="utf-8")

    _feed(monkeypatch, _start_payload())
    assert subagent_start_main() == 0

    d = list_dispatches()[0]
    assert d.prompt == _INTENT_PROMPT
    assert d.manifest["deliverables"] == []
    assert INTENT_MANIFEST_MALFORMED in d.manifest["notes"]
    assert "malformed" in capsys.readouterr().err


def test_invalid_intent_tier_falls_back_to_the_captured_default(
        tmp_path, monkeypatch, capsys):
    from fleetproof.hookgate import subagent_start_main
    from fleetproof.ledger import intents_dir, list_dispatches
    _setup_project(tmp_path, [_LANE_PASS], monkeypatch)
    intents_dir().mkdir(parents=True, exist_ok=True)
    (intents_dir() / "tester.json").write_text(json.dumps({
        "agent_type": "tester", "prompt": _INTENT_PROMPT, "tier": "captain",
    }), encoding="utf-8")

    _feed(monkeypatch, _start_payload())
    assert subagent_start_main() == 0

    d = list_dispatches()[0]
    assert d.prompt == _INTENT_PROMPT
    assert d.tier == "lane"
    # The declared tier was unusable, so the recorded one is a default.
    assert d.tier_source == "defaulted"
    assert "captain" in capsys.readouterr().err


def test_intent_without_a_tier_records_a_defaulted_tier_source(tmp_path, monkeypatch):
    # An intent that declares a prompt but no tier still lands on the capture
    # default: the tier is defaulted, not declared, and the record must not
    # claim otherwise.
    from fleetproof.hookgate import subagent_start_main
    from fleetproof.ledger import list_dispatches, write_intent
    _setup_project(tmp_path, [_LANE_PASS], monkeypatch)
    write_intent("tester", _INTENT_PROMPT)

    _feed(monkeypatch, _start_payload())
    assert subagent_start_main() == 0

    d = list_dispatches()[0]
    assert d.prompt == _INTENT_PROMPT
    assert d.tier == "lane"
    assert d.tier_source == "defaulted"


def test_intent_miss_is_loud_on_stderr(tmp_path, monkeypatch, capsys):
    # A role-keyed intent that silently misses leaves a placeholder prompt and
    # a defaulted lane tier with nothing telling the dispatcher (observed in a
    # field deployment on Windows). A miss must name itself.
    from fleetproof.hookgate import subagent_start_main
    from fleetproof.ledger import write_intent
    _setup_project(tmp_path, [_LANE_PASS], monkeypatch)
    write_intent("someone-else", _INTENT_PROMPT)

    _feed(monkeypatch, _start_payload(agent_type="tester"))
    assert subagent_start_main() == 0

    err = capsys.readouterr().err
    assert "no intent matched spawn 'tester'" in err
    assert "defaulted tier 'lane'" in err
    assert "someone-else.json" in err  # the sidecars that WERE present, listed


def test_intent_miss_note_says_none_when_no_sidecars_exist(tmp_path, monkeypatch, capsys):
    from fleetproof.hookgate import subagent_start_main
    _setup_project(tmp_path, [_LANE_PASS], monkeypatch)
    _feed(monkeypatch, _start_payload(agent_type="tester"))
    assert subagent_start_main() == 0
    err = capsys.readouterr().err
    assert "no intent matched spawn 'tester'" in err
    assert "Sidecars present: none." in err


def test_intent_matched_by_role_field(tmp_path, monkeypatch):
    # The sidecar is keyed by the harness agent_type (the spawn *name*), so a
    # dispatcher naming spawns per-task can declare the role instead: a sidecar
    # whose "role" field equals the payload's agent_type matches. Exact string
    # equality only — prefix matching invites collisions and stays out.
    from fleetproof.hookgate import subagent_start_main
    from fleetproof.ledger import intent_path, list_dispatches, write_intent
    _setup_project(tmp_path, [_LANE_PASS], monkeypatch)
    path = write_intent("widget-refactor", _INTENT_PROMPT, tier="leaf",
                        role="tester")

    _feed(monkeypatch, _start_payload(agent_type="tester"))
    assert subagent_start_main() == 0

    d = list_dispatches()[0]
    assert d.prompt == _INTENT_PROMPT
    assert d.tier == "leaf"
    assert not path.exists()  # consumed, same as a name match
    assert intent_path("widget-refactor") is not None


def test_intent_exact_filename_beats_a_role_match(tmp_path, monkeypatch):
    from fleetproof.hookgate import subagent_start_main
    from fleetproof.ledger import intent_path, list_dispatches, write_intent
    _setup_project(tmp_path, [_LANE_PASS], monkeypatch)
    write_intent("tester", "the exact-name prompt")
    write_intent("aaa-first-by-sort", "the role-keyed prompt", role="tester")

    _feed(monkeypatch, _start_payload(agent_type="tester"))
    assert subagent_start_main() == 0

    assert list_dispatches()[0].prompt == "the exact-name prompt"
    # The role-keyed sidecar is untouched: one intent, one spawn.
    assert intent_path("aaa-first-by-sort").exists()


def test_role_prefix_does_not_match(tmp_path, monkeypatch, capsys):
    # Explicitly out: a payload name that merely STARTS with a sidecar's name
    # (or role) is no match. Prefix matching is how 'build' intents end up on
    # 'build-docs' spawns.
    from fleetproof.hookgate import subagent_start_main
    from fleetproof.ledger import intent_path, list_dispatches, write_intent
    _setup_project(tmp_path, [_LANE_PASS], monkeypatch)
    write_intent("tester", _INTENT_PROMPT, role="tester")

    _feed(monkeypatch, _start_payload(agent_type="tester-1"))
    assert subagent_start_main() == 0

    assert "[uncaptured]" in list_dispatches()[0].prompt
    assert intent_path("tester").exists()
    assert "no intent matched spawn 'tester-1'" in capsys.readouterr().err


def test_dispatch_records_intent_source_hash(tmp_path, monkeypatch):
    # Decision-0011 interlock: a consumed sidecar's name and byte hash land on
    # the dispatch, so a forged or replaced intent is attributable post-hoc.
    import hashlib
    from fleetproof.hookgate import subagent_start_main
    from fleetproof.ledger import intent_path, list_dispatches, write_intent
    _setup_project(tmp_path, [_LANE_PASS], monkeypatch)
    path = write_intent("tester", _INTENT_PROMPT)
    expected_sha = hashlib.sha256(path.read_bytes()).hexdigest()

    _feed(monkeypatch, _start_payload())
    assert subagent_start_main() == 0

    d = list_dispatches()[0]
    assert d.intent_source == {"file": "tester.json", "sha256": expected_sha}
    assert not intent_path("tester").exists()


def test_placeholder_capture_records_no_intent_source(tmp_path, monkeypatch):
    from fleetproof.hookgate import subagent_start_main
    from fleetproof.ledger import list_dispatches
    _setup_project(tmp_path, [_LANE_PASS], monkeypatch)
    _feed(monkeypatch, _start_payload())
    assert subagent_start_main() == 0
    assert list_dispatches()[0].intent_source is None


# === manifest checks in the stop gate (SubagentStop) ===

def _era_config(tmp_path: Path) -> None:
    """Telemetry-era cutover in force, so the gate builds telemetry.json."""
    (tmp_path / ".fleetproof" / "config.json").write_text(
        json.dumps({"telemetry_era": "2026-01-01"}), encoding="utf-8")


def _spawn_with_intent(tmp_path, monkeypatch, manifest, spec=None):
    from fleetproof.hookgate import subagent_start_main
    from fleetproof.ledger import write_intent
    _setup_project(tmp_path, spec if spec is not None else [_LEAF_ONLY], monkeypatch)
    _era_config(tmp_path)
    write_intent("tester", _INTENT_PROMPT, manifest=manifest, tier="lane")
    _feed(monkeypatch, _start_payload())
    subagent_start_main()


def test_manifest_checks_grade_a_dispatch_the_spec_ignores(tmp_path, monkeypatch, capsys):
    # The L21 fix, end to end: the repo spec has nothing at lane tier, so before
    # 0.3.1 this dispatch terminated ungraded. Its own manifest checks now run
    # as blocking, the verdict lands, and coverage joins through check_map.
    from fleetproof.hookgate import subagent_stop_main
    from fleetproof.ledger import list_dispatches
    from fleetproof.telemetry import load_telemetry
    _spawn_with_intent(tmp_path, monkeypatch, _intent_manifest(cmd_exit=0))

    _feed(monkeypatch, _stop_payload(message="Refactored and verified."))
    assert subagent_stop_main() == 0
    assert capsys.readouterr().out == ""  # allowed to stop

    d = list_dispatches()[0]
    assert d.verdict == "verified"
    assert d.state == "terminated"
    telemetry = load_telemetry(d.run_id)
    assert telemetry["outcome.class"] == "verified"
    assert telemetry["outcome.verifier"]["checks_run"] == 1
    assert telemetry["outcome.coverage"] > 0


def test_failing_manifest_check_contradicts_and_blocks(tmp_path, monkeypatch, capsys):
    from fleetproof.hookgate import subagent_stop_main
    from fleetproof.ledger import list_dispatches
    _spawn_with_intent(tmp_path, monkeypatch, _intent_manifest(cmd_exit=1))

    _feed(monkeypatch, _stop_payload(message="Refactored, honest."))
    assert subagent_stop_main() == 0
    decision = json.loads(capsys.readouterr().out)
    assert decision["decision"] == "block"
    assert "m-widget" in decision["reason"]

    d = list_dispatches()[0]
    assert d.state == "contradicted"
    assert d.is_terminal is False  # the retry lands on this same dispatch


def test_manifest_checks_union_with_tier_selected_repo_checks(tmp_path, monkeypatch, capsys):
    # Both sources grade the same stop: the lane-tier repo check and the
    # dispatch's own manifest check each execute exactly once.
    from fleetproof.hookgate import subagent_stop_main
    from fleetproof.ledger import list_dispatches
    _spawn_with_intent(tmp_path, monkeypatch, _intent_manifest(cmd_exit=0),
                       spec=[_LANE_PASS])

    _feed(monkeypatch, _stop_payload(message="Done, both ways."))
    assert subagent_stop_main() == 0
    assert capsys.readouterr().out == ""

    assert _graded_check_ids() == ["lane-ok", "m-widget"]
    assert list_dispatches()[0].verdict == "verified"


def test_manifest_check_colliding_with_a_repo_check_id_defers_to_the_spec(
        tmp_path, monkeypatch, capsys):
    # One id, one command: the repo spec is the more attested source, so a
    # manifest check reusing a selected repo check's id is dropped rather than
    # run as a second command under the same name.
    from fleetproof.hookgate import subagent_stop_main
    from fleetproof.ledger import list_dispatches
    manifest = {
        "checks": [{"id": "lane-ok",
                    "cmd": f'"{sys.executable}" -c "raise SystemExit(1)"'}],
    }
    _spawn_with_intent(tmp_path, monkeypatch, manifest, spec=[_LANE_PASS])

    _feed(monkeypatch, _stop_payload(message="Done."))
    assert subagent_stop_main() == 0
    assert capsys.readouterr().out == ""  # the repo's lane-ok passes; no block

    assert _graded_check_ids() == ["lane-ok"]
    assert list_dispatches()[0].verdict == "verified"


def test_malformed_manifest_check_entries_are_skipped_loudly(
        tmp_path, monkeypatch, capsys):
    # Junk entries must neither run half-parsed nor take down the gate — and a
    # skipped check never counts as executed, so it can never read as a pass.
    from fleetproof.hookgate import subagent_stop_main
    from fleetproof.ledger import list_dispatches
    manifest = _intent_manifest(cmd_exit=0)
    manifest["checks"] = [
        "not-an-object",
        {"id": "no-cmd"},
        {"id": "multi-line", "cmd": "echo a\necho b"},
    ] + manifest["checks"]
    _spawn_with_intent(tmp_path, monkeypatch, manifest)

    _feed(monkeypatch, _stop_payload(message="Done."))
    assert subagent_stop_main() == 0
    err = capsys.readouterr().err
    assert "skipped" in err

    assert _graded_check_ids() == ["m-widget"]  # only the well-formed entry ran
    assert list_dispatches()[0].verdict == "verified"


def test_read_hook_input_tolerates_utf8_bom(monkeypatch):
    # Some shells prepend a BOM when piping; losing the payload would silently
    # lose session grouping and drift detection.
    import io
    import sys as _sys
    from fleetproof.hookgate import _read_hook_input

    monkeypatch.setattr(_sys, "stdin", io.StringIO("\ufeff{\"session_id\": \"s-1\"}\n"))
    assert _read_hook_input() == {"session_id": "s-1"}


# === arming: separate "can't end a turn" from "claims done" (B2) ===

def test_default_with_no_arming_file_is_armed(tmp_path, monkeypatch):
    from fleetproof.hookgate import BRIDGE_ARMED, load_arming
    _setup_project(tmp_path, [_FAIL], monkeypatch)
    assert load_arming()["bridge"] == BRIDGE_ARMED
    decision, code = stop_gate()
    assert decision["decision"] == "block"  # armed = current behavior


def test_malformed_arming_file_reads_as_armed(tmp_path, monkeypatch):
    # The fail-safe direction is the gate ON: a corrupt arming file must not
    # read as a disarm nobody asked for.
    from fleetproof.hookgate import BRIDGE_ARMED, arming_path, load_arming
    _setup_project(tmp_path, [_FAIL], monkeypatch)
    arming_path().write_text("{not json", encoding="utf-8")
    assert load_arming()["bridge"] == BRIDGE_ARMED
    decision, code = stop_gate()
    assert decision["decision"] == "block"


def test_advisory_bridge_renders_check_failures_as_context_not_block(
        tmp_path, monkeypatch):
    # The field workaround was flipping every check to block:false at phase
    # boundaries — a spec edit, which post-B1 is drift that wedges in-flight
    # dispatches. Arming lives OUTSIDE the hashed spec: checks still run, the
    # failure is rendered with the advisory marker, the decision does not block.
    from fleetproof.hookgate import BRIDGE_ADVISORY, set_arming
    _setup_project(tmp_path, [_FAIL, dict(_PASS, id="fine")], monkeypatch)
    set_arming(BRIDGE_ADVISORY, note="publish phase")

    decision, code = stop_gate()
    assert code == 0
    assert decision is not None
    assert "decision" not in decision  # never a block on check failures
    ctx = decision["hookSpecificOutput"]["additionalContext"]
    assert "[FLEETPROOF ADVISORY" in ctx
    assert "bridge gate disarmed: publish phase" in ctx
    # The full per-check breakdown still renders — advisory is not silent.
    assert "[FAIL] bad:" in ctx and "[pass] fine:" in ctx


def test_ledger_sweep_still_blocks_while_advisory(tmp_path, monkeypatch):
    # A half-closed dispatch is bookkeeping, not phase: arming governs the
    # check gate only, and a stalled dispatch blocks the stop regardless.
    from fleetproof.hookgate import BRIDGE_ADVISORY, set_arming
    _setup_project(tmp_path, [_FAIL], monkeypatch)
    monkeypatch.setenv(runlog.SESSION_ID_ENV, "sess-advisory-sweep")
    set_arming(BRIDGE_ADVISORY, note="publish phase")
    stalled = _make_dispatch(report=True)

    decision, code = stop_gate()
    assert decision["decision"] == "block"
    assert stalled in decision["reason"]
    assert "reported but never terminated" in decision["reason"]
    # The check failure stayed advisory: it is context, not part of the block.
    assert "blocking check(s) failed" not in decision["reason"]
    assert "[FLEETPROOF ADVISORY" in decision["hookSpecificOutput"]["additionalContext"]


def test_subagent_gate_ignores_arming(tmp_path, monkeypatch, capsys):
    # Arming is the bridge's turn-end switch. A subagent's "done" claim is
    # always graded: disarming the bridge must not un-grade the fleet.
    from fleetproof.hookgate import BRIDGE_ADVISORY, set_arming, \
        subagent_start_main, subagent_stop_main
    _setup_project(tmp_path, [_LANE_ARTIFACT], monkeypatch)
    set_arming(BRIDGE_ADVISORY, note="publish phase")
    _feed(monkeypatch, _start_payload())
    subagent_start_main()

    _feed(monkeypatch, _stop_payload(message="Shipped the artifact."))
    assert subagent_stop_main() == 0
    decision = json.loads(capsys.readouterr().out)
    assert decision["decision"] == "block"


def test_flipping_arming_trips_no_drift_mechanism(tmp_path, monkeypatch):
    # The whole point of arming.json living outside the spec and outside the
    # checks tree: flipping it mid-session must not read as drift anywhere.
    from fleetproof.checks import checks_tree_hash, spec_hash
    from fleetproof.hookgate import BRIDGE_ADVISORY, BRIDGE_ARMED, set_arming
    _setup_project(tmp_path, [_PASS], monkeypatch)
    monkeypatch.setenv(runlog.SESSION_ID_ENV, "sess-arming-drift")

    monkeypatch.setenv(runlog.RUN_ID_ENV, "20260101-000001-bbbb01")
    first, code = stop_gate()
    assert first is None  # baseline

    spec_before, tree_before = spec_hash(), checks_tree_hash()
    set_arming(BRIDGE_ADVISORY, note="publish phase")
    set_arming(BRIDGE_ARMED, note="publish done")
    assert spec_hash() == spec_before
    assert checks_tree_hash() == tree_before

    monkeypatch.setenv(runlog.RUN_ID_ENV, "20260101-000002-bbbb02")
    second, code = stop_gate()
    assert second is None  # pass, no drift note, no advisory residue


# === escalation ladder: a wedge must terminate, not loop (B3) ===

def test_third_contradiction_abandons_instead_of_blocking(tmp_path, monkeypatch, capsys):
    # An unsatisfiable blocking check produced an unbounded block/retry loop
    # (19 cycles observed in a field deployment on Windows). The third
    # contradiction is terminal: verdict recorded, dispatch abandoned, the
    # agent allowed to stop, and the dispatcher told the work is NOT verified.
    from fleetproof.hookgate import subagent_start_main, subagent_stop_main
    from fleetproof.ledger import REASON_ABANDONED, list_dispatches
    _setup_project(tmp_path, [_LANE_ARTIFACT], monkeypatch)
    _feed(monkeypatch, _start_payload())
    subagent_start_main()

    for attempt in (1, 2):
        _feed(monkeypatch, _stop_payload(message=f"Shipped it (attempt {attempt})."))
        assert subagent_stop_main() == 0
        decision = json.loads(capsys.readouterr().out)
        assert decision["decision"] == "block"

    _feed(monkeypatch, _stop_payload(message="Shipped it (attempt 3)."))
    assert subagent_stop_main() == 0
    decision = json.loads(capsys.readouterr().out)
    # No block — and no internal keys leaking into the hook output either.
    assert set(decision) == {"hookSpecificOutput"}
    ctx = decision["hookSpecificOutput"]["additionalContext"]
    assert ctx.startswith("[FLEETPROOF]")
    assert "abandoned after 3 contradicted stops" in ctx
    assert "lane-artifact" in ctx
    assert "NOT verified" in ctx
    assert "Park it, fix the spec/tier, or re-dispatch." in ctx

    d = list_dispatches()[0]
    assert d.run_id in ctx
    assert d.state == "terminated"
    assert d.verdict == "contradicted"  # abandoned is never verified
    assert d.terminate_reason == REASON_ABANDONED
    assert [t["state"] for t in d.transitions] == [
        "dispatched", "reported", "contradicted", "reported", "contradicted",
        "reported", "contradicted", "terminated",
    ]


def test_a_fix_before_the_third_stop_still_verifies(tmp_path, monkeypatch, capsys):
    # The ladder counts contradictions, not stops: two failures and then a
    # real fix is the retry loop working exactly as designed.
    from fleetproof.hookgate import subagent_start_main, subagent_stop_main
    from fleetproof.ledger import list_dispatches
    _setup_project(tmp_path, [_LANE_ARTIFACT], monkeypatch)
    _feed(monkeypatch, _start_payload())
    subagent_start_main()

    for attempt in (1, 2):
        _feed(monkeypatch, _stop_payload(message=f"Shipped it (attempt {attempt})."))
        subagent_stop_main()
        capsys.readouterr()

    (tmp_path / "artifact.txt").write_text("real", encoding="utf-8")
    _feed(monkeypatch, _stop_payload(message="Really shipped it this time."))
    assert subagent_stop_main() == 0
    assert capsys.readouterr().out == ""
    d = list_dispatches()[0]
    assert d.verdict == "verified"
    assert d.state == "terminated"


def test_abandoned_agents_next_stop_is_an_orphan_never_a_regrade(
        tmp_path, monkeypatch, capsys):
    # An abandoned dispatch is terminal. If the same agent stops again, there
    # is no non-terminal dispatch to join, so the stop falls through to the
    # A1 orphan path — it must never re-open or re-grade the abandoned record.
    from fleetproof.hookgate import subagent_start_main, subagent_stop_main
    from fleetproof.ledger import list_dispatches, list_orphan_stops
    _setup_project(tmp_path, [_LANE_ARTIFACT], monkeypatch)
    _feed(monkeypatch, _start_payload())
    subagent_start_main()
    for attempt in (1, 2, 3):
        _feed(monkeypatch, _stop_payload(message=f"Shipped it (attempt {attempt})."))
        subagent_stop_main()
        capsys.readouterr()
    d = list_dispatches()[0]
    transitions_before = list(d.transitions)

    # Even with the check now passing, the abandoned dispatch stays closed.
    (tmp_path / "artifact.txt").write_text("real", encoding="utf-8")
    _feed(monkeypatch, _stop_payload(message="Fourth stop after abandonment."))
    assert subagent_stop_main() == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "orphan" in captured.err

    assert len(list_orphan_stops()) == 1
    d = list_dispatches()[0]
    assert d.transitions == transitions_before
    assert d.verdict == "contradicted"


def test_abandonment_context_survives_a_stop_hook_retry(tmp_path, monkeypatch, capsys):
    # _suppress_on_retry swallows context-only output on a forced continuation
    # — but the abandonment notice is the loop's terminus and the dispatcher's
    # only signal, so it must be emitted even when stop_hook_active is set.
    from fleetproof.hookgate import subagent_start_main, subagent_stop_main
    _setup_project(tmp_path, [_LANE_ARTIFACT], monkeypatch)
    _feed(monkeypatch, _start_payload())
    subagent_start_main()
    for attempt in (1, 2):
        _feed(monkeypatch, _stop_payload(message=f"Shipped it (attempt {attempt})."))
        subagent_stop_main()
        capsys.readouterr()

    payload = _stop_payload(message="Shipped it (attempt 3).")
    payload["stop_hook_active"] = True
    _feed(monkeypatch, payload)
    assert subagent_stop_main() == 0
    decision = json.loads(capsys.readouterr().out)
    assert "abandoned after 3 contradicted stops" in (
        decision["hookSpecificOutput"]["additionalContext"])
