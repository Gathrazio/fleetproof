"""0.7.2 feature tests: the grading-spine fix pack.

Field provenance (GCC lessons L27/L34/L51/L58, sessions 0070/0079/0082/0083):
named teammate dispatches went terminal with the claim recorded and no verdict
attached — the no-contract shape, where a named spawn's agent_type is the NAME,
no sidecar matches, and the manifest-less capture has nothing runnable at stop
time; a long build's late-artifact checks minted a permanent false
`contradicted` at intermediate stops; a living interim report tripped the
bridge's verify-or-close block; and `dispatch new --agent-name` plus the real
spawn forked the ledger into two records.

These tests pin the cures AND the invariants: terminal verdicts stay immutable,
no new context blocks a stop that used to pass, and the two deliberate gate
relaxations (check-grace, interim reports) carry a composed regression test
(lesson L25).
"""

from __future__ import annotations

import io
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from fleetproof import runlog
from fleetproof.cli import main
from fleetproof.hookgate import (
    ADVISORY,
    POST_HOC_MANIFEST_FILENAME,
    _ledger_context,
    ledger_sweep,
    set_arming,
    stop_gate,
    subagent_start_main,
    subagent_stop_main,
    verify_dispatch,
)
from fleetproof.ledger import (
    LedgerError,
    close_dispatch,
    create_dispatch,
    list_dispatches,
    list_intents,
    load_dispatch,
    manifest_grace_stops,
    record_report,
    record_verdict,
    write_intent,
)
from fleetproof.report import state_label

_LEAF_ONLY = {"id": "leaf-ok", "run": f'"{sys.executable}" -c "raise SystemExit(0)"',
              "expect": "exit0", "tier": "leaf"}

# A late-artifact check: fails until artifact.txt exists in the project root —
# the L58 shape (an 8-minute render still in flight at intermediate stops).
_ARTIFACT_CMD = (f'"{sys.executable}" -c "import os,sys; '
                 "sys.exit(0 if os.path.exists('artifact.txt') else 1)\"")


def _grace_manifest(grace=2, grace_value=None):
    manifest = {
        "deliverables": ["the rendered artifact"],
        "checks": [{"id": "late-artifact", "cmd": _ARTIFACT_CMD}],
        "check_map": {"the rendered artifact": ["late-artifact"]},
        "notes": "declared by the dispatcher",
    }
    manifest["grace_stops"] = grace if grace_value is None else grace_value
    return manifest


def _setup_project(tmp_path: Path, checks: list[dict], monkeypatch) -> None:
    marker = tmp_path / ".fleetproof"
    marker.mkdir(exist_ok=True)
    (marker / "checks.json").write_text(json.dumps({"checks": checks}),
                                        encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    runlog.set_runs_dir(marker / "runs")


@pytest.fixture(autouse=True)
def _reset_runs():
    yield
    runlog.set_runs_dir(None)
    os.environ.pop(runlog.SESSION_ID_ENV, None)


def _feed(monkeypatch, payload: dict) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))


def _start_payload(agent_id="agent-1", agent_type="tester", session="sess-fleet"):
    return {"session_id": session, "transcript_path": "/tmp/t.jsonl", "cwd": ".",
            "hook_event_name": "SubagentStart", "agent_id": agent_id,
            "agent_type": agent_type}


def _stop_payload(message="I did the thing.", agent_id="agent-1",
                  agent_type="tester", session="sess-fleet"):
    payload = _start_payload(agent_id, agent_type, session)
    payload["hook_event_name"] = "SubagentStop"
    payload["last_assistant_message"] = message
    return payload


def _stop(monkeypatch, capsys, **kwargs):
    """Run one SubagentStop through the main; return (decision_or_None, err)."""
    _feed(monkeypatch, _stop_payload(**kwargs))
    assert subagent_stop_main() == 0
    captured = capsys.readouterr()
    decision = json.loads(captured.out) if captured.out else None
    return decision, captured.err


def _age_on_disk(run_id: str, minutes_ago: int) -> None:
    path = runlog.runs_dir() / run_id / "dispatch.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    stamp = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()
    for t in raw["transitions"]:
        t["at"] = stamp
    path.write_text(json.dumps(raw), encoding="utf-8")


# =====================================================================
# Candidate 2 — check-grace for long in-dispatch builds (L58)
# =====================================================================

def test_manifest_grace_stops_reads_tolerantly():
    assert manifest_grace_stops(None) == 0
    assert manifest_grace_stops({}) == 0
    assert manifest_grace_stops({"grace_stops": 3}) == 3
    assert manifest_grace_stops({"grace_stops": 1}) == 1
    # Malformed values are "no grace", never a crash and never a nuked manifest.
    assert manifest_grace_stops({"grace_stops": 0}) == 0
    assert manifest_grace_stops({"grace_stops": -2}) == 0
    assert manifest_grace_stops({"grace_stops": "3"}) == 0
    assert manifest_grace_stops({"grace_stops": True}) == 0
    assert manifest_grace_stops({"grace_stops": 2.5}) == 0


def test_grace_defers_the_verdict_then_the_ladder_runs(tmp_path, monkeypatch, capsys):
    # The L58 sequence end-to-end: two graced stops (blocked, NO verdict),
    # then contradictions grade exactly as before — bounded at grace + 3.
    _setup_project(tmp_path, [_LEAF_ONLY], monkeypatch)
    write_intent("tester", "render the artifact", manifest=_grace_manifest(2),
                 tier="lane")
    _feed(monkeypatch, _start_payload())
    subagent_start_main()
    capsys.readouterr()

    # Stops 1 and 2: grace consumed, blocked, no verdict, no contradiction.
    for n in (1, 2):
        decision, _ = _stop(monkeypatch, capsys, message=f"still rendering ({n})")
        assert decision["decision"] == "block"
        assert f"Check-grace stop {n} of 2 consumed" in decision["reason"]
        assert "NO verdict was recorded" in decision["reason"]
        d = list_dispatches()[0]
        assert d.state == "reported"
        assert d.verdict is None
        assert d.grace_blocks_used == n
        assert not any(t.get("state") == "contradicted" for t in d.transitions)

    # Stop 3: grace exhausted — the ordinary contradicted path, ladder strike 1.
    decision, _ = _stop(monkeypatch, capsys, message="still rendering (3)")
    assert decision["decision"] == "block"
    assert "Check-grace" not in decision["reason"]
    d = list_dispatches()[0]
    assert d.state == "contradicted"

    # Stops 4 and 5: strikes 2 and 3 — the third contradiction is terminal
    # (abandoned), so the whole wedge is bounded at grace_stops + 3 blocks.
    decision, _ = _stop(monkeypatch, capsys, message="still rendering (4)")
    assert decision["decision"] == "block"
    decision, _ = _stop(monkeypatch, capsys, message="still rendering (5)")
    assert "decision" not in decision  # terminal: stop allowed, dispatcher told
    assert "abandoned after 3 contradicted stops" in \
        decision["hookSpecificOutput"]["additionalContext"]
    d = list_dispatches()[0]
    assert d.state == "terminated"
    assert d.terminate_reason == "abandoned-after-3-contradictions"


def test_grace_then_artifact_lands_grades_verified(tmp_path, monkeypatch, capsys):
    # The happy L58 ending: the build finishes inside the grace window and the
    # final stop verifies — no contradiction anywhere in the trail, and the
    # final report (not the interim one) is the body on file.
    _setup_project(tmp_path, [_LEAF_ONLY], monkeypatch)
    write_intent("tester", "render the artifact", manifest=_grace_manifest(2),
                 tier="lane")
    _feed(monkeypatch, _start_payload())
    subagent_start_main()
    capsys.readouterr()

    decision, _ = _stop(monkeypatch, capsys, message="still rendering")
    assert "Check-grace stop 1 of 2" in decision["reason"]

    (tmp_path / "artifact.txt").write_text("rendered\n", encoding="utf-8")
    decision, _ = _stop(monkeypatch, capsys, message="render complete, verified on disk")
    assert decision is None  # clean verified stop
    d = list_dispatches()[0]
    assert d.verdict == "verified"
    assert d.state == "terminated"
    assert not any(t.get("state") == "contradicted" for t in d.transitions)
    # reported -> reported kept the claim fresh: the final body won.
    assert d.load_report()["summary"] == "render complete, verified on disk"
    assert len(d.load_reports()) == 2


def test_malformed_grace_declaration_is_ignored_with_a_note(
        tmp_path, monkeypatch, capsys):
    _setup_project(tmp_path, [_LEAF_ONLY], monkeypatch)
    write_intent("tester", "render", manifest=_grace_manifest(grace_value="2"),
                 tier="lane")
    _feed(monkeypatch, _start_payload())
    subagent_start_main()
    capsys.readouterr()

    decision, err = _stop(monkeypatch, capsys, message="still rendering")
    assert decision["decision"] == "block"
    assert "Check-grace" not in decision["reason"]  # no grace: graded now
    assert "'grace_stops' but the value is not an int >= 1" in err
    assert list_dispatches()[0].state == "contradicted"


def test_grace_never_blocks_a_disarmed_tier(tmp_path, monkeypatch, capsys):
    # Never-block: the advisory branch must not start blocking because a
    # manifest declares grace — a disarmed coordinator's failing stop still
    # grades `advisory` immediately, exactly as 0.7.1 did.
    _setup_project(tmp_path, [_LEAF_ONLY], monkeypatch)
    set_arming(ADVISORY, note="coordinator build phase", tier="coordinator")
    write_intent("tester", "render", manifest=_grace_manifest(2),
                 tier="coordinator")
    _feed(monkeypatch, _start_payload())
    subagent_start_main()
    capsys.readouterr()

    decision, _ = _stop(monkeypatch, capsys, message="mid-phase stop")
    assert decision is None  # advisory stop: allowed, closed, no block
    d = list_dispatches()[0]
    assert d.verdict == "advisory"
    assert d.state == "terminated"
    assert d.grace_blocks_used == 0


def test_verify_dispatch_ignores_grace(tmp_path, monkeypatch):
    # The operator's out-of-band grade is the final look by definition.
    _setup_project(tmp_path, [_LEAF_ONLY], monkeypatch)
    run_id = create_dispatch("render", tier="lane", manifest=_grace_manifest(5),
                             session_id="sess-oob")
    record_report(run_id, {"summary": "claimed done"})
    result = verify_dispatch(run_id)
    assert result["verdict"] == "contradicted"
    assert load_dispatch(run_id).grace_blocks_used == 0


# =====================================================================
# reported -> reported (the re-report rule the grace path rides on)
# =====================================================================

def test_interim_then_final_report_supersedes(tmp_runs):
    run_id = create_dispatch("work", session_id="s-rr")
    record_report(run_id, {"summary": "halfway there"}, interim=True)
    assert load_dispatch(run_id).last_report_interim is True
    record_report(run_id, {"summary": "done for real"})
    d = load_dispatch(run_id)
    assert d.last_report_interim is False
    assert d.load_report()["summary"] == "done for real"
    assert [r["summary"] for r in d.load_reports()] == [
        "halfway there", "done for real"]


# =====================================================================
# Candidate 3 — interim-report state
# =====================================================================

def test_interim_flag_rides_the_transition_and_the_dict(tmp_runs):
    run_id = create_dispatch("work", session_id="s-int")
    record_report(run_id, {"summary": "living report"}, interim=True)
    d = load_dispatch(run_id)
    assert d.transitions[-1]["interim"] is True
    assert d.to_dict()["last_report_interim"] is True
    assert state_label(d) == "reported (interim)"


def test_ledger_sweep_classifies_interim_apart_from_stalled(tmp_path, monkeypatch):
    _setup_project(tmp_path, [_LEAF_ONLY], monkeypatch)
    session = "sess-sweep"
    interim_id = create_dispatch("living", session_id=session)
    record_report(interim_id, {"summary": "interim"}, interim=True)
    stalled_id = create_dispatch("halfway", session_id=session)
    record_report(stalled_id, {"summary": "final"})
    fleet_id = create_dispatch("running", session_id=session)

    stalled, retrying, in_fleet, interim = ledger_sweep(session)
    assert [r.run_id for r in interim] == [interim_id]
    assert [r.run_id for r in stalled] == [stalled_id]
    assert [r.run_id for r in in_fleet] == [fleet_id]
    assert retrying == []


def test_bridge_stop_does_not_block_on_an_interim_report(tmp_path, monkeypatch):
    _setup_project(tmp_path, [
        {"id": "ok", "run": f'"{sys.executable}" -c "raise SystemExit(0)"',
         "expect": "exit0", "block": True}], monkeypatch)
    monkeypatch.setenv(runlog.SESSION_ID_ENV, "sess-interim")
    run_id = create_dispatch("living lane", tier="lane")
    record_report(run_id, {"summary": "interim"}, interim=True)

    decision, code = stop_gate()
    assert code == 0
    assert "decision" not in decision  # context, never a block
    context = decision["hookSpecificOutput"]["additionalContext"]
    assert f"[interim report] {run_id}" in context
    assert "verify-or-close not armed" in context


def test_graded_unclosed_still_blocks_even_after_an_interim(tmp_path, monkeypatch):
    # The flag ends at the verdict: a verified-but-never-closed dispatch is
    # the half-closed bookkeeping the sweep exists to block on, interim or no.
    _setup_project(tmp_path, [
        {"id": "ok", "run": f'"{sys.executable}" -c "raise SystemExit(0)"',
         "expect": "exit0", "block": True}], monkeypatch)
    monkeypatch.setenv(runlog.SESSION_ID_ENV, "sess-halfclosed")
    run_id = create_dispatch("lane", tier="lane")
    record_report(run_id, {"summary": "interim"}, interim=True)
    record_verdict(run_id, "verified")

    decision, code = stop_gate()
    assert decision["decision"] == "block"
    assert run_id in decision["reason"]


def test_stale_interim_escalates_like_any_silent_lane(tmp_path, monkeypatch):
    # The watchdog is not deferred: an interim lane that dies is the L55
    # shape and must not hide behind its flag.
    _setup_project(tmp_path, [
        {"id": "ok", "run": f'"{sys.executable}" -c "raise SystemExit(0)"',
         "expect": "exit0", "block": True}], monkeypatch)
    monkeypatch.setenv(runlog.SESSION_ID_ENV, "sess-stale-int")
    run_id = create_dispatch("living lane", tier="lane")
    record_report(run_id, {"summary": "interim"}, interim=True)
    _age_on_disk(run_id, minutes_ago=45)

    fresh_like, code = stop_gate()
    context = fresh_like["hookSpecificOutput"]["additionalContext"]
    assert f"[STALE interim report] {run_id}" in context
    assert "check the agent now" in context
    assert "decision" not in fresh_like  # still never a block


def test_cli_dispatch_report_interim_flag(tmp_path, monkeypatch, capsys):
    _setup_project(tmp_path, [_LEAF_ONLY], monkeypatch)
    run_id = create_dispatch("work", session_id="s-cli-int")
    report = tmp_path / "report.json"
    report.write_text(json.dumps({"summary": "interim claim"}), encoding="utf-8")
    capsys.readouterr()
    assert main(["dispatch", "report", run_id, "--report", str(report),
                 "--interim"]) == 0
    out = capsys.readouterr().out
    assert "interim" in out
    assert load_dispatch(run_id).last_report_interim is True


# =====================================================================
# Composed regression (L25): grace + interim both relax a gate
# =====================================================================

def test_grace_plus_interim_do_not_compound_into_silence(
        tmp_path, monkeypatch, capsys):
    # Both 0.7.2 relaxations in play at once: the bridge must not block on
    # the interim row, the grace window must still be bounded, and the final
    # failing stop must still grade contradicted — while a genuinely
    # half-closed dispatch STILL blocks the bridge. Neither relaxation may
    # widen the other. The untiered pass check keeps the bridge's own spec
    # gate green so the sweep is the only thing under test.
    _setup_project(tmp_path, [
        {"id": "bridge-ok", "run": f'"{sys.executable}" -c "raise SystemExit(0)"',
         "expect": "exit0", "block": True},
        _LEAF_ONLY], monkeypatch)
    write_intent("tester", "render", manifest=_grace_manifest(1), tier="lane")
    _feed(monkeypatch, _start_payload())
    subagent_start_main()
    capsys.readouterr()

    # The lane's dispatcher files an interim report on its behalf (CLI path).
    lane = list_dispatches()[0]
    record_report(lane.run_id, {"summary": "interim: render running"},
                  interim=True, by="cli")

    # Bridge stop: interim row is context, not a block.
    monkeypatch.setenv(runlog.SESSION_ID_ENV, "sess-fleet")
    decision, _ = stop_gate()
    assert "decision" not in decision
    assert f"[interim report] {lane.run_id}" in \
        decision["hookSpecificOutput"]["additionalContext"]

    # The lane's failing stop still consumes its one grace...
    decision, _ = _stop(monkeypatch, capsys, message="still rendering")
    assert decision["decision"] == "block"
    assert "Check-grace stop 1 of 1" in decision["reason"]
    # ...and the next failing stop still grades contradicted, permanently.
    decision, _ = _stop(monkeypatch, capsys, message="still rendering (2)")
    assert decision["decision"] == "block"
    assert list_dispatches()[0].state == "contradicted"

    # And a verified-unclosed sibling still blocks the bridge outright.
    half = create_dispatch("half-closed", tier="lane", session_id="sess-fleet")
    record_report(half, {"summary": "claimed"})
    record_verdict(half, "verified")
    decision, _ = stop_gate()
    assert decision["decision"] == "block"
    assert half in decision["reason"]


# =====================================================================
# Candidate 1 — the ungraded close is loud, and recoverable
# =====================================================================

def test_ungraded_close_announces_in_band_with_remedy(
        tmp_path, monkeypatch, capsys):
    # Manifest-less capture at a tier the spec leaves empty: the close is
    # honest (nothing runnable) and now the dispatcher is told AT the close,
    # in-band, with the out-of-band remedy named.
    _setup_project(tmp_path, [_LEAF_ONLY], monkeypatch)
    _feed(monkeypatch, _start_payload())
    subagent_start_main()
    capsys.readouterr()

    decision, _ = _stop(monkeypatch, capsys, message="done (so I claim)")
    assert "decision" not in decision  # the stop is allowed — context only
    context = decision["hookSpecificOutput"]["additionalContext"]
    d = list_dispatches()[0]
    assert f"dispatch {d.run_id} closed UNGRADED" in context
    assert "nothing runnable at tier lane" in context
    assert f"dispatch verify {d.run_id} --terminal --manifest <file>" in context
    assert d.state == "terminated" and d.verdict is None


def test_verify_post_hoc_manifest_grades_a_terminal_ungraded_record(
        tmp_path, monkeypatch, capsys):
    # The sanctioned recovery: the record never got a contract; the operator
    # supplies one out of band. The record's own manifest stays untouched,
    # the body lands beside it, and the verdict transition carries the hash.
    _setup_project(tmp_path, [_LEAF_ONLY], monkeypatch)
    _feed(monkeypatch, _start_payload())
    subagent_start_main()
    _stop(monkeypatch, capsys, message="done (so I claim)")
    d = list_dispatches()[0]
    assert d.state == "terminated" and d.verdict is None

    (tmp_path / "artifact.txt").write_text("real\n", encoding="utf-8")
    manifest = {"deliverables": ["artifact"],
                "checks": [{"id": "late-artifact", "cmd": _ARTIFACT_CMD}]}
    result = verify_dispatch(d.run_id, allow_terminal=True,
                             post_hoc_manifest=manifest)
    assert result["verdict"] == "verified"
    assert "post-hoc manifest" in result["detail"]

    d = load_dispatch(d.run_id)
    verdict_t = next(t for t in d.transitions if t.get("state") == "verified")
    assert verdict_t["by"] == "cli-verify"
    assert verdict_t["post_hoc_manifest_sha256"]
    assert verdict_t["post_hoc_manifest_file"] == POST_HOC_MANIFEST_FILENAME
    assert (d.run_dir / POST_HOC_MANIFEST_FILENAME).exists()
    # The record's own manifest was never rewritten.
    assert (d.manifest or {}).get("checks", []) == []


def test_verify_post_hoc_manifest_can_contradict(tmp_path, monkeypatch, capsys):
    _setup_project(tmp_path, [_LEAF_ONLY], monkeypatch)
    _feed(monkeypatch, _start_payload())
    subagent_start_main()
    _stop(monkeypatch, capsys, message="done (so I claim)")
    d = list_dispatches()[0]

    manifest = {"checks": [{"id": "late-artifact", "cmd": _ARTIFACT_CMD}]}
    result = verify_dispatch(d.run_id, allow_terminal=True,
                             post_hoc_manifest=manifest)  # no artifact on disk
    assert result["verdict"] == "contradicted"


def test_verify_post_hoc_refusals(tmp_path, monkeypatch):
    _setup_project(tmp_path, [_LEAF_ONLY], monkeypatch)
    manifest = {"checks": [{"id": "c", "cmd": _ARTIFACT_CMD}]}

    # A dispatch with ANY verdict is refused: outcomes on record stay.
    graded = create_dispatch("graded work", tier="lane", session_id="s-v")
    record_report(graded, {"summary": "done"})
    record_verdict(graded, "contradicted", detail="failed")
    close_dispatch(graded)
    with pytest.raises(LedgerError, match="outcomes on record stay"):
        verify_dispatch(graded, allow_terminal=True, post_hoc_manifest=manifest)
    # And the refusal left the record untouched.
    assert load_dispatch(graded).verdict == "contradicted"
    assert not (load_dispatch(graded).run_dir / POST_HOC_MANIFEST_FILENAME).exists()

    # A dispatch with its own contract is graded against it, never a substitute.
    own = create_dispatch("own contract", tier="lane", session_id="s-v",
                          manifest={"checks": [{"id": "own", "cmd": _ARTIFACT_CMD}]})
    record_report(own, {"summary": "done"})
    with pytest.raises(LedgerError, match="already declares manifest checks"):
        verify_dispatch(own, post_hoc_manifest=manifest)

    # A post-hoc manifest with no checks grades nothing — refused.
    bare = create_dispatch("bare", tier="lane", session_id="s-v")
    record_report(bare, {"summary": "done"})
    with pytest.raises(LedgerError, match="declares no checks"):
        verify_dispatch(bare, post_hoc_manifest={"deliverables": ["x"]})


def test_cli_dispatch_verify_manifest_flag(tmp_path, monkeypatch, capsys):
    _setup_project(tmp_path, [_LEAF_ONLY], monkeypatch)
    run_id = create_dispatch("work", tier="lane", session_id="s-cliv")
    record_report(run_id, {"summary": "claimed"})
    close_dispatch(run_id)  # terminal-ungraded

    (tmp_path / "artifact.txt").write_text("real\n", encoding="utf-8")
    mf = tmp_path / "post.json"
    mf.write_text(json.dumps(
        {"checks": [{"id": "late-artifact", "cmd": _ARTIFACT_CMD}]}),
        encoding="utf-8")
    capsys.readouterr()
    assert main(["dispatch", "verify", run_id, "--terminal",
                 "--manifest", str(mf)]) == 0
    out = capsys.readouterr().out
    assert "verified" in out
    assert load_dispatch(run_id).verdict == "verified"


# =====================================================================
# Candidate 4b — SubagentStart joins a waiting CLI dispatch (the fork fix)
# =====================================================================

def _cli_record(agent_type="tester", session="sess-fleet", manifest=None):
    return create_dispatch(
        "lane work", tier="lane", manifest=manifest, session_id=session,
        agent={"agent_type": agent_type, "agent_id": None, "capture": "cli"})


def test_start_joins_the_waiting_cli_dispatch(tmp_path, monkeypatch, capsys):
    _setup_project(tmp_path, [_LEAF_ONLY], monkeypatch)
    manifest = {"checks": [{"id": "late-artifact", "cmd": _ARTIFACT_CMD}]}
    run_id = _cli_record(manifest=manifest)
    _feed(monkeypatch, _start_payload(agent_id="agent-7"))
    assert subagent_start_main() == 0
    err = capsys.readouterr().err
    assert f"joined CLI-created dispatch {run_id}" in err

    records = list_dispatches()
    assert len(records) == 1  # no twin
    assert records[0].run_id == run_id
    assert records[0].agent_id == "agent-7"

    # And the stop grades THAT record through its manifest checks.
    (tmp_path / "artifact.txt").write_text("done\n", encoding="utf-8")
    decision, _ = _stop(monkeypatch, capsys, agent_id="agent-7",
                        message="artifact rendered")
    assert decision is None
    assert load_dispatch(run_id).verdict == "verified"


def test_ambiguous_cli_join_captures_normally_and_says_so(
        tmp_path, monkeypatch, capsys):
    _setup_project(tmp_path, [_LEAF_ONLY], monkeypatch)
    _cli_record()
    _cli_record()
    _feed(monkeypatch, _start_payload(agent_id="agent-7"))
    assert subagent_start_main() == 0
    err = capsys.readouterr().err
    assert "ambiguous CLI-dispatch join" in err
    assert len(list_dispatches()) == 3  # the 0.7.1 behavior, said out loud


def test_cross_session_cli_record_is_not_joined(tmp_path, monkeypatch, capsys):
    _setup_project(tmp_path, [_LEAF_ONLY], monkeypatch)
    _cli_record(session="some-other-session")
    _feed(monkeypatch, _start_payload(agent_id="agent-7"))
    assert subagent_start_main() == 0
    assert "joined CLI-created dispatch" not in capsys.readouterr().err
    assert len(list_dispatches()) == 2


def test_join_leaves_a_matching_sidecar_unconsumed_with_a_warning(
        tmp_path, monkeypatch, capsys):
    _setup_project(tmp_path, [_LEAF_ONLY], monkeypatch)
    _cli_record()
    path = write_intent("tester", "the sidecar prompt")
    _feed(monkeypatch, _start_payload(agent_id="agent-7"))
    assert subagent_start_main() == 0
    err = capsys.readouterr().err
    assert "joined CLI-created dispatch" in err
    assert "was NOT consumed" in err
    assert path.exists()  # left for the hygiene surfaces, not eaten silently


# =====================================================================
# Candidate 5 — name-mismatch warn (L34/L51)
# =====================================================================

def test_intent_miss_with_sidecars_is_stamped_and_explained(
        tmp_path, monkeypatch, capsys):
    _setup_project(tmp_path, [_LEAF_ONLY], monkeypatch)
    write_intent("integration", "archetype-keyed prompt")  # the wrong key
    _feed(monkeypatch, _start_payload(agent_type="lane-N1-build"))
    assert subagent_start_main() == 0
    err = capsys.readouterr().err
    assert "named spawn reports its NAME as agent_type" in err
    assert "--agent <spawn-name>" in err

    d = list_dispatches()[0]
    assert d.intent_miss == {"sidecars_present": ["integration.json"]}


def test_ungraded_close_names_the_missed_sidecars(tmp_path, monkeypatch, capsys):
    # The stamp pays off at the stop: the in-band ungraded-close context
    # names the sidecars that sat unconsumed and states the match-key rule.
    _setup_project(tmp_path, [_LEAF_ONLY], monkeypatch)
    write_intent("integration", "archetype-keyed prompt")
    _feed(monkeypatch, _start_payload(agent_type="lane-N1-build"))
    subagent_start_main()
    capsys.readouterr()

    decision, _ = _stop(monkeypatch, capsys, agent_type="lane-N1-build",
                        message="done (so I claim)")
    context = decision["hookSpecificOutput"]["additionalContext"]
    assert "closed UNGRADED" in context
    assert "integration.json" in context
    assert "named spawns report the NAME as agent_type" in context


# =====================================================================
# Candidate 4a — sidecar hygiene: board footer + sweep --intents
# =====================================================================

def test_fleet_footer_lists_unconsumed_intents(tmp_path, monkeypatch, capsys):
    _setup_project(tmp_path, [_LEAF_ONLY], monkeypatch)
    write_intent("integration", "waiting prompt")
    capsys.readouterr()
    assert main(["fleet", "--all"]) == 0
    out = capsys.readouterr().out
    assert "1 unconsumed intent sidecar(s)" in out
    assert "integration.json" in out
    assert "dispatch sweep --older-than" in out

    assert main(["fleet", "--all", "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["unconsumed_intents"][0]["file"] == "integration.json"


def test_sweep_intents_clears_stale_sidecars_only(tmp_path, monkeypatch, capsys):
    _setup_project(tmp_path, [_LEAF_ONLY], monkeypatch)
    stale = write_intent("old-agent", "stale prompt")
    fresh = write_intent("new-agent", "fresh prompt")
    raw = json.loads(stale.read_text(encoding="utf-8"))
    raw["created_at"] = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    stale.write_text(json.dumps(raw), encoding="utf-8")

    capsys.readouterr()
    assert main(["dispatch", "sweep", "--older-than", "1d",
                 "--intents", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "would remove 1 unconsumed" in out
    assert stale.exists() and fresh.exists()  # dry run writes nothing

    assert main(["dispatch", "sweep", "--older-than", "1d", "--intents"]) == 0
    out = capsys.readouterr().out
    assert "removed 1 unconsumed" in out
    assert not stale.exists()
    assert fresh.exists()
    assert [i["file"] for i in list_intents()] == ["new-agent.json"]


def test_sweep_without_intents_flag_leaves_sidecars_alone(
        tmp_path, monkeypatch, capsys):
    _setup_project(tmp_path, [_LEAF_ONLY], monkeypatch)
    stale = write_intent("old-agent", "stale prompt")
    raw = json.loads(stale.read_text(encoding="utf-8"))
    raw["created_at"] = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    stale.write_text(json.dumps(raw), encoding="utf-8")
    capsys.readouterr()
    assert main(["dispatch", "sweep", "--older-than", "1d"]) == 0
    assert stale.exists()


# =====================================================================
# Never-block pins for the new context lines
# =====================================================================

def test_interim_exit_semantics_match_fresh_exactly(tmp_path, monkeypatch):
    # Same shape as 0.7.1's stale pin: interim (fresh or stale) changes the
    # words in the context payload and nothing about the stop decision.
    _setup_project(tmp_path, [
        {"id": "ok", "run": f'"{sys.executable}" -c "raise SystemExit(0)"',
         "expect": "exit0", "block": True}], monkeypatch)
    monkeypatch.setenv(runlog.SESSION_ID_ENV, "sess-pin")
    run_id = create_dispatch("living lane", tier="lane")
    record_report(run_id, {"summary": "interim"}, interim=True)
    fresh_decision, fresh_code = stop_gate()
    _age_on_disk(run_id, minutes_ago=400)
    stale_decision, stale_code = stop_gate()
    assert (fresh_code, set(fresh_decision)) == (stale_code, set(stale_decision))
    assert "decision" not in stale_decision
