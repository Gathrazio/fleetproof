"""Tests for the dispatch ledger — records, lifecycle, tiers, manifests.

The ledger is bookkeeping, so these tests are mostly about the two properties
that make it trustworthy: a dispatch is on record before any work happens, and
the lifecycle records what actually occurred (including an agent killed before it
ever reported) instead of only what a tidy state machine would allow.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fleetproof import runlog
from fleetproof.checks import spec_hash
from fleetproof.ledger import (
    DERIVED_V1_MALFORMED,
    DERIVED_V1_NO_MANIFEST,
    STATE_CONTRADICTED,
    STATE_DISPATCHED,
    STATE_REPORTED,
    STATE_TERMINATED,
    STATE_VERIFIED,
    LedgerError,
    ancestor_chain,
    close_dispatch,
    create_dispatch,
    derive_manifest,
    find_dispatch_by_agent,
    infer_tier,
    list_dispatches,
    load_dispatch,
    record_report,
    record_verdict,
    report_content_hash,
)


@pytest.fixture
def ledger_runs(tmp_runs, monkeypatch):
    """Isolated runs dir with every inherited run-id env var cleared.

    tmp_runs clears RUN_ID/NO_RECORD/SESSION_ID; a dispatch also reads
    PARENT_RUN_ID, so a real parent run in the ambient environment would
    otherwise leak into the inferred tier.
    """
    monkeypatch.delenv(runlog.PARENT_RUN_ID_ENV, raising=False)
    return tmp_runs


def _ok_report(summary: str = "did the thing") -> dict:
    return {
        "summary": summary,
        "deliverables": [
            {"name": "ledger.py", "confidence": 0.9, "evidence": "executed",
             "pointer": "src/fleetproof/ledger.py"},
        ],
    }


# === create / load roundtrip ===

def test_create_and_load_roundtrip(ledger_runs):
    run_id = create_dispatch("build the thing", tier="lane")
    record = load_dispatch(run_id)
    assert record is not None
    assert record.run_id == run_id
    assert record.prompt == "build the thing"
    assert record.tier == "lane"
    assert record.tier_source == "declared"
    assert record.state == STATE_DISPATCHED
    assert record.is_open is True
    assert record.is_terminal is False
    assert record.has_report is False
    assert record.verdict is None
    assert record.started_at is not None
    assert [t["state"] for t in record.transitions] == [STATE_DISPATCHED]
    assert record.transitions[0]["by"] == "cli"


def test_create_writes_root_record_readable_by_runlog(ledger_runs):
    # A dispatch run must read back through the ordinary run-log reader, so
    # `fleetproof list`/`show`/report see dispatches without ledger knowledge.
    parent = create_dispatch("bridge work")
    child = create_dispatch("lane work", parent_run_id=parent)
    run = runlog.load_run(child)
    assert run is not None
    assert run.root_tool == "dispatch"
    root = json.loads((ledger_runs / child / "_root.json").read_text(encoding="utf-8"))
    assert root["parent_run_id"] == parent


def test_create_records_session_id_from_env(ledger_runs, monkeypatch):
    monkeypatch.setenv(runlog.SESSION_ID_ENV, "sess-fleet")
    record = load_dispatch(create_dispatch("scoped work"))
    assert record.session_id == "sess-fleet"


def test_create_dispatch_tier_source_override(ledger_runs):
    # The caller can say explicitly HOW the tier was arrived at — a capture
    # that fell back to the lane default records "defaulted", never the
    # "declared" its non-None tier argument would otherwise imply.
    from fleetproof.ledger import TIER_SOURCE_DEFAULTED
    record = load_dispatch(create_dispatch(
        "captured work", tier="lane", tier_source=TIER_SOURCE_DEFAULTED))
    assert record.tier == "lane"
    assert record.tier_source == "defaulted"


def test_create_dispatch_rejects_an_unknown_tier_source(ledger_runs):
    with pytest.raises(LedgerError):
        create_dispatch("work", tier="lane", tier_source="guessed")


def test_create_defaults_parent_to_current_run(ledger_runs, monkeypatch):
    parent = create_dispatch("bridge work")
    monkeypatch.setenv(runlog.RUN_ID_ENV, parent)
    child = load_dispatch(create_dispatch("dispatched from within a run"))
    assert child.parent_run_id == parent
    assert child.tier == "lane"
    assert child.tier_source == "inferred"


def test_empty_prompt_rejected(ledger_runs):
    with pytest.raises(LedgerError):
        create_dispatch("   ")


def test_unknown_declared_tier_rejected(ledger_runs):
    with pytest.raises(LedgerError):
        create_dispatch("work", tier="middle-management")


def test_load_dispatch_ignores_non_dispatch_runs(ledger_runs):
    with runlog.record("some-tool", "do"):
        pass
    plain_run = runlog.list_run_records()[0].run_id
    assert load_dispatch(plain_run) is None
    assert load_dispatch("no-such-run") is None


# === tier inference ===

def test_tier_inference_at_three_depths(ledger_runs):
    bridge = create_dispatch("top-level work")
    lane = create_dispatch("mid work", parent_run_id=bridge)
    leaf = create_dispatch("deep work", parent_run_id=lane)
    assert load_dispatch(bridge).tier == "bridge"
    assert load_dispatch(lane).tier == "lane"
    assert load_dispatch(leaf).tier == "leaf"
    assert all(load_dispatch(r).tier_source == "inferred" for r in (bridge, lane, leaf))


def test_declared_tier_overrides_inference_and_is_recorded(ledger_runs):
    bridge = create_dispatch("top-level work")
    # Depth says "lane"; the operator says "coordinator" — which is the one tier
    # inference can never produce on its own.
    declared = create_dispatch("coordinate", tier="coordinator", parent_run_id=bridge)
    record = load_dispatch(declared)
    assert record.tier == "coordinator"
    assert record.tier_source == "declared"
    # And a declared coordinator still counts as a real link in the chain.
    assert load_dispatch(create_dispatch("under it", parent_run_id=declared)).tier == "leaf"


def test_infer_tier_without_parent_is_bridge(ledger_runs):
    assert infer_tier(None) == "bridge"
    assert infer_tier("") == "bridge"


def test_infer_tier_degrades_to_lane_for_unknown_parent(ledger_runs):
    # An unreadable parent record must not fail the dispatch; it degrades to the
    # shallowest non-bridge guess.
    assert infer_tier("ghost-run-id") == "lane"


def test_ancestor_walk_survives_a_cycle(ledger_runs):
    # Two runs that claim each other as parent. The walk must terminate.
    for name, parent in (("run-a", "run-b"), ("run-b", "run-a")):
        d = ledger_runs / name
        d.mkdir(parents=True)
        (d / "_root.json").write_text(
            json.dumps({"run_id": name, "parent_run_id": parent}), encoding="utf-8")
    assert ancestor_chain("run-a") == ["run-a", "run-b"]
    assert infer_tier("run-a") == "leaf"


# === lifecycle state machine ===

def test_full_happy_path_transitions(ledger_runs):
    run_id = create_dispatch("work")
    record_report(run_id, _ok_report())
    assert load_dispatch(run_id).state == STATE_REPORTED
    record_verdict(run_id, STATE_VERIFIED, detail="all checks passed")
    record = load_dispatch(run_id)
    assert record.state == STATE_VERIFIED
    assert record.verdict == STATE_VERIFIED
    assert record.transitions[-1]["by"] == "checker"
    assert record.transitions[-1]["detail"] == "all checks passed"
    close_dispatch(run_id)
    final = load_dispatch(run_id)
    assert final.state == STATE_TERMINATED
    assert final.is_terminal is True
    # The verdict survives termination — the board still shows how it was graded.
    assert final.verdict == STATE_VERIFIED
    assert [t["state"] for t in final.transitions] == [
        STATE_DISPATCHED, STATE_REPORTED, STATE_VERIFIED, STATE_TERMINATED,
    ]


def test_illegal_jump_from_dispatched_to_verified_raises(ledger_runs):
    run_id = create_dispatch("work")
    with pytest.raises(LedgerError):
        record_verdict(run_id, STATE_VERIFIED)
    assert load_dispatch(run_id).state == STATE_DISPATCHED


def test_kill_from_dispatched_is_allowed(ledger_runs):
    # An agent killed before it ever reported. Record reality.
    run_id = create_dispatch("work that never came back")
    close_dispatch(run_id, by="hook")
    record = load_dispatch(run_id)
    assert record.state == STATE_TERMINATED
    assert record.is_open is False
    assert record.verdict is None
    assert record.transitions[-1]["by"] == "hook"


def test_kill_from_reported_is_allowed(ledger_runs):
    run_id = create_dispatch("work")
    record_report(run_id, _ok_report())
    close_dispatch(run_id)
    assert load_dispatch(run_id).state == STATE_TERMINATED


def test_nothing_follows_terminated(ledger_runs):
    run_id = create_dispatch("work")
    close_dispatch(run_id)
    with pytest.raises(LedgerError):
        record_report(run_id, _ok_report())
    with pytest.raises(LedgerError):
        close_dispatch(run_id)
    # The rejected report left no claim on disk.
    assert load_dispatch(run_id).has_report is False


def test_second_report_rejected(ledger_runs):
    run_id = create_dispatch("work")
    record_report(run_id, _ok_report("first"))
    with pytest.raises(LedgerError):
        record_report(run_id, _ok_report("revised"))
    assert load_dispatch(run_id).load_report()["summary"] == "first"


def test_contradicted_dispatch_may_report_again(ledger_runs):
    # The gate-blocked retry lands on the same dispatch: the harness hands the same
    # agent its turn back, so re-reporting is reality the ledger has to record.
    run_id = create_dispatch("work")
    record_report(run_id, _ok_report("first attempt"))
    record_verdict(run_id, STATE_CONTRADICTED, detail="tests-pass failed")
    record_report(run_id, _ok_report("fixed it"))
    record_verdict(run_id, STATE_VERIFIED)
    close_dispatch(run_id)

    record = load_dispatch(run_id)
    assert [t["state"] for t in record.transitions] == [
        STATE_DISPATCHED, STATE_REPORTED, STATE_CONTRADICTED,
        STATE_REPORTED, STATE_VERIFIED, STATE_TERMINATED,
    ]
    # The newest claim wins on disk; the attempt history lives in the transitions.
    assert record.load_report()["summary"] == "fixed it"


def test_verified_dispatch_may_not_report_again(ledger_runs):
    # Only a contradiction re-opens a dispatch. A passed one is done.
    run_id = create_dispatch("work")
    record_report(run_id, _ok_report())
    record_verdict(run_id, STATE_VERIFIED)
    with pytest.raises(LedgerError):
        record_report(run_id, _ok_report("second thoughts"))


def test_contradicted_verdict_recorded(ledger_runs):
    run_id = create_dispatch("work")
    record_report(run_id, _ok_report())
    record_verdict(run_id, STATE_CONTRADICTED, detail="tests-pass failed")
    record = load_dispatch(run_id)
    assert record.state == STATE_CONTRADICTED
    assert record.verdict == STATE_CONTRADICTED


def test_bogus_verdict_rejected(ledger_runs):
    run_id = create_dispatch("work")
    record_report(run_id, _ok_report())
    with pytest.raises(LedgerError):
        record_verdict(run_id, "probably-fine")


def test_transitions_on_missing_dispatch_raise(ledger_runs):
    with pytest.raises(LedgerError):
        close_dispatch("no-such-run")


# === reports ===

def test_report_written_verbatim_and_read_back(ledger_runs):
    run_id = create_dispatch("work")
    report = _ok_report()
    report["extra_field"] = {"additive": True}
    record_report(run_id, report)
    record = load_dispatch(run_id)
    assert record.has_report is True
    assert record.load_report() == report


def test_empty_report_rejected(ledger_runs):
    run_id = create_dispatch("work")
    for bad in ({}, {"summary": "", "deliverables": []}, {"summary": "   "},
                {"deliverables": []}):
        with pytest.raises(LedgerError):
            record_report(run_id, bad)
    assert load_dispatch(run_id).state == STATE_DISPATCHED
    assert load_dispatch(run_id).has_report is False


def test_report_with_only_deliverables_accepted(ledger_runs):
    run_id = create_dispatch("work")
    record_report(run_id, {"deliverables": [{"name": "x", "evidence": "observed"}]})
    assert load_dispatch(run_id).state == STATE_REPORTED


def test_report_with_only_summary_accepted(ledger_runs):
    run_id = create_dispatch("work")
    record_report(run_id, {"summary": "explained why nothing shipped"})
    assert load_dispatch(run_id).state == STATE_REPORTED


def test_malformed_report_fields_rejected(ledger_runs):
    run_id = create_dispatch("work")
    bad_reports = [
        {"summary": "s", "deliverables": "not-a-list"},
        {"summary": "s", "deliverables": ["not-an-object"]},
        {"summary": "s", "deliverables": [{"name": "x", "confidence": 1.5}]},
        {"summary": "s", "deliverables": [{"name": "x", "confidence": "high"}]},
        {"summary": "s", "deliverables": [{"name": "x", "evidence": "vibes"}]},
        {"summary": 42},
    ]
    for bad in bad_reports:
        with pytest.raises(LedgerError):
            record_report(run_id, bad)
    with pytest.raises(LedgerError):
        record_report(run_id, ["not", "an", "object"])


# === spec pinning ===

def test_spec_hash_pinned_at_dispatch(ledger_runs, tmp_path):
    spec = tmp_path / "checks.json"
    spec.write_text(json.dumps({"checks": [{"id": "ok", "run": "true"}]}), encoding="utf-8")
    run_id = create_dispatch("work", spec_path=spec)
    assert load_dispatch(run_id).spec_sha256_pinned == spec_hash(spec)


def test_missing_spec_pins_null(ledger_runs, tmp_path):
    run_id = create_dispatch("work", spec_path=tmp_path / "absent.json")
    assert load_dispatch(run_id).spec_sha256_pinned is None


def test_tree_hash_pinned_at_dispatch(ledger_runs, tmp_path):
    # The graders are part of the spec: the pin covers the check scripts too,
    # or a rewritten grader mid-dispatch leaves zero drift signal.
    from fleetproof.checks import checks_tree_hash
    spec = tmp_path / "checks.json"
    spec.write_text(json.dumps({"checks": [{"id": "ok", "run": "true"}]}), encoding="utf-8")
    scripts = tmp_path / "checks"
    scripts.mkdir()
    (scripts / "verify.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
    run_id = create_dispatch("work", spec_path=spec)
    record = load_dispatch(run_id)
    assert record.tree_sha256_pinned == checks_tree_hash(spec)
    assert record.tree_sha256_pinned != record.spec_sha256_pinned


def test_missing_spec_pins_null_tree_hash(ledger_runs, tmp_path):
    run_id = create_dispatch("work", spec_path=tmp_path / "absent.json")
    assert load_dispatch(run_id).tree_sha256_pinned is None


# === listing and filters ===

def test_list_dispatches_newest_first_and_filters(ledger_runs, monkeypatch):
    monkeypatch.setenv(runlog.SESSION_ID_ENV, "sess-1")
    still_open = create_dispatch("open work")
    reported = create_dispatch("reported work")
    record_report(reported, _ok_report())
    dead = create_dispatch("dead work")
    close_dispatch(dead)

    monkeypatch.setenv(runlog.SESSION_ID_ENV, "sess-2")
    other_session = create_dispatch("other session work")

    all_ids = [r.run_id for r in list_dispatches()]
    assert set(all_ids) == {still_open, reported, dead, other_session}
    assert all_ids == sorted(all_ids, reverse=True)  # newest first

    assert [r.run_id for r in list_dispatches(open_only=True)] == sorted(
        [still_open, other_session], reverse=True)
    assert [r.run_id for r in list_dispatches(non_terminal_only=True)] == sorted(
        [still_open, reported, other_session], reverse=True)
    assert [r.run_id for r in list_dispatches(session_id="sess-2")] == [other_session]
    assert [r.run_id for r in list_dispatches(session_id="sess-1", open_only=True)] == [
        still_open]


def test_list_dispatches_skips_plain_runs_and_missing_dir(ledger_runs):
    assert list_dispatches() == []
    with runlog.record("some-tool", "do"):
        pass
    assert list_dispatches() == []
    create_dispatch("real dispatch")
    assert len(list_dispatches()) == 1


def test_damaged_dispatch_reads_back_as_dispatched(ledger_runs):
    # One corrupt record must not take out the board.
    run_id = create_dispatch("work")
    path = ledger_runs / run_id / "dispatch.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["transitions"] = []
    path.write_text(json.dumps(raw), encoding="utf-8")
    assert load_dispatch(run_id).state == STATE_DISPATCHED
    assert len(list_dispatches()) == 1


# === manifests ===

def test_derive_manifest_without_structured_block(ledger_runs):
    manifest = derive_manifest("just prose, no json anywhere")
    assert manifest == {
        "deliverables": [], "allowed_paths": [], "checks": [],
        "notes": DERIVED_V1_NO_MANIFEST,
    }


def test_derive_manifest_from_fenced_json_block():
    prompt = (
        "Do the work.\n\n"
        "```json\n"
        '{"manifest": {"deliverables": ["ledger.py"], '
        '"allowed_paths": ["src/"], "checks": ["tests-pass"], "notes": "phase A"}}\n'
        "```\n"
        "Thanks.\n"
    )
    manifest = derive_manifest(prompt)
    assert manifest["deliverables"] == ["ledger.py"]
    assert manifest["allowed_paths"] == ["src/"]
    assert manifest["checks"] == ["tests-pass"]
    assert manifest["notes"] == "phase A"


def test_derive_manifest_ignores_fenced_json_without_manifest_key():
    prompt = "```json\n{\"something_else\": 1}\n```"
    assert derive_manifest(prompt)["notes"] == DERIVED_V1_NO_MANIFEST


def test_derive_manifest_flags_malformed_manifest():
    # A block that claims a manifest but is unusable must say so, not pretend
    # there was nothing there.
    prompt = "```json\n{\"manifest\": {\"deliverables\": \"not-a-list\"}}\n```"
    assert derive_manifest(prompt)["notes"] == DERIVED_V1_MALFORMED


def test_derive_manifest_tolerates_unparseable_fence():
    prompt = "```json\n{not json at all\n```"
    assert derive_manifest(prompt)["notes"] == DERIVED_V1_NO_MANIFEST


def test_create_dispatch_derives_manifest_from_prompt(ledger_runs):
    prompt = "Work.\n```json\n{\"manifest\": {\"deliverables\": [\"a\"]}}\n```"
    record = load_dispatch(create_dispatch(prompt))
    assert record.manifest["deliverables"] == ["a"]
    assert record.manifest["notes"] == ""


def test_explicit_manifest_is_normalized_and_preserves_unknown_keys(ledger_runs):
    record = load_dispatch(create_dispatch(
        "work", manifest={"deliverables": ["x"], "owner": "integration"}))
    assert record.manifest["deliverables"] == ["x"]
    assert record.manifest["allowed_paths"] == []
    assert record.manifest["checks"] == []
    assert record.manifest["notes"] == ""
    assert record.manifest["owner"] == "integration"


# === harness-agent block + lookup ===

def _agent(agent_id="agent-1", agent_type="tester", capture="start") -> dict:
    return {"agent_id": agent_id, "agent_type": agent_type, "capture": capture}


def test_agent_block_roundtrip(ledger_runs):
    run_id = create_dispatch("subagent work", tier="lane", agent=_agent())
    record = load_dispatch(run_id)
    assert record.agent == _agent()
    assert record.agent_id == "agent-1"
    assert record.to_dict()["agent"]["capture"] == "start"


def test_agent_absent_reads_back_as_none(ledger_runs):
    # A Phase A dispatch has no agent block at all; the key is omitted, not null.
    run_id = create_dispatch("hand-made work")
    raw = json.loads((ledger_runs / run_id / "dispatch.json").read_text(encoding="utf-8"))
    assert "agent" not in raw
    record = load_dispatch(run_id)
    assert record.agent is None
    assert record.agent_id is None


def test_agent_block_preserves_unknown_keys(ledger_runs):
    run_id = create_dispatch("work", agent=dict(_agent(), transcript="/tmp/t.jsonl"))
    assert load_dispatch(run_id).agent["transcript"] == "/tmp/t.jsonl"


def test_malformed_agent_block_raises(ledger_runs):
    for bad in ("not-an-object", {"capture": "sometimes"}, {"agent_id": 7},
                {"agent_type": []}):
        with pytest.raises(LedgerError):
            create_dispatch("work", agent=bad)


def test_find_dispatch_by_agent(ledger_runs, monkeypatch):
    monkeypatch.setenv(runlog.SESSION_ID_ENV, "sess-1")
    mine = create_dispatch("work", tier="lane", agent=_agent("agent-1"))
    create_dispatch("other work", tier="lane", agent=_agent("agent-2"))
    found = find_dispatch_by_agent("sess-1", "agent-1")
    assert found is not None and found.run_id == mine
    assert find_dispatch_by_agent("sess-1", "agent-3") is None
    assert find_dispatch_by_agent("sess-1", None) is None


def test_find_dispatch_by_agent_skips_terminated_and_other_sessions(ledger_runs, monkeypatch):
    # An agent id can come back around after its dispatch closed out, so a
    # terminated record must never be the one a later stop reports onto.
    monkeypatch.setenv(runlog.SESSION_ID_ENV, "sess-1")
    old = create_dispatch("finished work", tier="lane", agent=_agent("agent-1"))
    close_dispatch(old)
    assert find_dispatch_by_agent("sess-1", "agent-1") is None

    fresh = create_dispatch("new work", tier="lane", agent=_agent("agent-1"))
    assert find_dispatch_by_agent("sess-1", "agent-1").run_id == fresh
    # Same agent id, different session: not a match.
    assert find_dispatch_by_agent("sess-other", "agent-1") is None


def test_find_dispatch_by_agent_without_a_session_matches_on_agent_alone(ledger_runs, monkeypatch):
    # A payload with no session_id (older harness, or a malformed hook) leaves the
    # agent id as the only join key available. Documented fallback, not an accident.
    monkeypatch.setenv(runlog.SESSION_ID_ENV, "sess-1")
    run_id = create_dispatch("work", tier="lane", agent=_agent("agent-1"))
    found = find_dispatch_by_agent(None, "agent-1")
    assert found is not None and found.run_id == run_id


def test_find_dispatch_by_agent_returns_newest_match(ledger_runs, monkeypatch):
    monkeypatch.setenv(runlog.SESSION_ID_ENV, "sess-1")
    first = create_dispatch("older", tier="lane", agent=_agent("agent-1"))
    second = create_dispatch("newer", tier="lane", agent=_agent("agent-1"))
    # Two live records for one agent id is already a bug upstream; the lookup at
    # least has to be deterministic. Newest-first is run-id order (see
    # list_dispatches), so the greater id wins.
    assert find_dispatch_by_agent("sess-1", "agent-1").run_id == max(first, second)


# === CLI/hook join unification (B7) ===

def _cli_agent(agent_type="worker"):
    return {"agent_type": agent_type, "agent_id": None, "capture": "cli"}


def test_capture_cli_is_a_valid_agent_block(ledger_runs):
    run_id = create_dispatch("cli-declared work", tier="lane", agent=_cli_agent())
    record = load_dispatch(run_id)
    assert record.agent == _cli_agent()
    assert record.agent_id is None


def test_find_for_stop_prefers_the_agent_id_key(ledger_runs, monkeypatch):
    from fleetproof.ledger import find_dispatch_for_stop
    monkeypatch.setenv(runlog.SESSION_ID_ENV, "sess-1")
    create_dispatch("awaiting adoption", tier="lane", agent=_cli_agent("worker"))
    hooked = create_dispatch("hook-captured", tier="lane",
                             agent=_agent("agent-1", agent_type="worker"))
    found, notes = find_dispatch_for_stop("sess-1", "agent-1", "worker")
    assert found.run_id == hooked
    assert notes == []


def test_unique_name_match_adopts_the_agent_id(ledger_runs, monkeypatch):
    from fleetproof.ledger import find_dispatch_for_stop
    monkeypatch.setenv(runlog.SESSION_ID_ENV, "sess-1")
    run_id = create_dispatch("cli-declared work", tier="lane", agent=_cli_agent())
    found, notes = find_dispatch_for_stop("sess-1", "agent-9", "worker")
    assert found is not None and found.run_id == run_id
    assert notes == []
    # The adoption is durable and audited: the id is on the record, the
    # transitions note it, and the state did not move.
    reloaded = load_dispatch(run_id)
    assert reloaded.agent_id == "agent-9"
    assert reloaded.state == "dispatched"
    assert "adopted agent_id agent-9" in reloaded.transitions[-1]["detail"]


def test_ambiguous_name_match_adopts_nothing_and_says_so(ledger_runs, monkeypatch):
    from fleetproof.ledger import find_dispatch_for_stop
    monkeypatch.setenv(runlog.SESSION_ID_ENV, "sess-1")
    first = create_dispatch("one", tier="lane", agent=_cli_agent())
    second = create_dispatch("two", tier="lane", agent=_cli_agent())
    found, notes = find_dispatch_for_stop("sess-1", "agent-9", "worker")
    assert found is None
    assert len(notes) == 1 and "ambiguous" in notes[0]
    assert first in notes[0] and second in notes[0]
    for run_id in (first, second):
        assert load_dispatch(run_id).agent_id is None


def test_name_match_requires_an_open_dispatch(ledger_runs, monkeypatch):
    from fleetproof.ledger import find_dispatch_for_stop
    monkeypatch.setenv(runlog.SESSION_ID_ENV, "sess-1")
    run_id = create_dispatch("already reported", tier="lane", agent=_cli_agent())
    record_report(run_id, _ok_report())
    found, notes = find_dispatch_for_stop("sess-1", "agent-9", "worker")
    assert found is None and notes == []


def test_name_match_requires_the_same_session(ledger_runs, monkeypatch):
    from fleetproof.ledger import find_dispatch_for_stop
    monkeypatch.setenv(runlog.SESSION_ID_ENV, "sess-1")
    create_dispatch("session-scoped", tier="lane", agent=_cli_agent())
    found, notes = find_dispatch_for_stop("sess-2", "agent-9", "worker")
    assert found is None and notes == []


def test_name_match_never_steals_a_record_that_has_an_agent_id(ledger_runs, monkeypatch):
    from fleetproof.ledger import find_dispatch_for_stop
    monkeypatch.setenv(runlog.SESSION_ID_ENV, "sess-1")
    create_dispatch("someone else's", tier="lane",
                    agent=_agent("agent-1", agent_type="worker"))
    found, notes = find_dispatch_for_stop("sess-1", "agent-9", "worker")
    assert found is None and notes == []


def test_malformed_explicit_manifest_raises(ledger_runs):
    with pytest.raises(LedgerError):
        create_dispatch("work", manifest={"checks": "tests-pass"})
    with pytest.raises(LedgerError):
        create_dispatch("work", manifest=["not", "an", "object"])


# === terminate reasons (telemetry preconditions) ===

def test_close_records_machine_reason_on_the_terminate_transition(ledger_runs):
    run_id = create_dispatch("idle work", tier="lane")
    record = close_dispatch(run_id, reason="sweep-idle")
    assert record.state == STATE_TERMINATED
    assert record.transitions[-1]["reason"] == "sweep-idle"
    assert load_dispatch(run_id).terminate_reason == "sweep-idle"


def test_close_without_reason_stays_legal_and_reads_unclassified(ledger_runs):
    # Older callers pass no reason; the record must stay writable and the
    # absence must read back as None, never as a guessed reason.
    run_id = create_dispatch("legacy close", tier="lane")
    record = close_dispatch(run_id)
    assert record.state == STATE_TERMINATED
    assert "reason" not in record.transitions[-1]
    assert load_dispatch(run_id).terminate_reason is None


def test_close_rejects_out_of_vocabulary_reason(ledger_runs):
    # A free-text reason would be underivable in exactly the way the closed
    # vocabulary exists to prevent.
    run_id = create_dispatch("work", tier="lane")
    with pytest.raises(LedgerError):
        close_dispatch(run_id, reason="felt-like-it")
    assert load_dispatch(run_id).state == STATE_DISPATCHED


def test_terminate_reason_is_none_while_not_terminated(ledger_runs):
    run_id = create_dispatch("work", tier="lane")
    assert load_dispatch(run_id).terminate_reason is None


# === work-product hash across retries ===

def test_report_transition_carries_content_hash(ledger_runs):
    run_id = create_dispatch("hash me", tier="lane")
    record = record_report(run_id, _ok_report())
    entry = record.transitions[-1]
    assert entry["state"] == STATE_REPORTED
    assert entry["report_sha256"] == report_content_hash(_ok_report())


def test_retry_hashes_split_changed_from_unchanged_work(ledger_runs):
    # The near-miss/flake split downstream rests on this: report.json only ever
    # holds the latest claim, so the per-attempt hash must live on the
    # transitions or an unchanged retry is indistinguishable from a fixed one.
    unchanged = create_dispatch("retry, same work", tier="lane")
    record_report(unchanged, _ok_report())
    record_verdict(unchanged, STATE_CONTRADICTED)
    record_report(unchanged, _ok_report())
    hashes = [t["report_sha256"] for t in load_dispatch(unchanged).transitions
              if t["state"] == STATE_REPORTED]
    assert len(hashes) == 2 and hashes[0] == hashes[1]

    changed = create_dispatch("retry, fixed work", tier="lane")
    record_report(changed, _ok_report("first attempt"))
    record_verdict(changed, STATE_CONTRADICTED)
    record_report(changed, _ok_report("second attempt, actually fixed"))
    hashes = [t["report_sha256"] for t in load_dispatch(changed).transitions
              if t["state"] == STATE_REPORTED]
    assert len(hashes) == 2 and hashes[0] != hashes[1]


def test_report_content_hash_is_key_order_independent(ledger_runs):
    a = {"summary": "s", "deliverables": []}
    b = {"deliverables": [], "summary": "s"}
    assert report_content_hash(a) == report_content_hash(b)


# === telemetry-era cutover stamp ===

def _write_config(ledger_runs, payload):
    (ledger_runs.parent / "config.json").write_text(
        json.dumps(payload), encoding="utf-8")


def test_dispatch_created_after_cutover_carries_era_stamp(ledger_runs):
    _write_config(ledger_runs, {"telemetry_era": "2026-01-01"})
    record = load_dispatch(create_dispatch("in-era work", tier="lane"))
    assert record.telemetry_era == "2026-01-01"
    raw = json.loads((record.run_dir / "dispatch.json").read_text(encoding="utf-8"))
    assert raw["telemetry_era"] == "2026-01-01"


def test_dispatch_without_config_stays_pre_telemetry_shaped(ledger_runs):
    record = load_dispatch(create_dispatch("pre-era work", tier="lane"))
    assert record.telemetry_era is None
    raw = json.loads((record.run_dir / "dispatch.json").read_text(encoding="utf-8"))
    assert "telemetry_era" not in raw


def test_future_dated_cutover_does_not_stamp(ledger_runs):
    _write_config(ledger_runs, {"telemetry_era": "2999-01-01"})
    assert load_dispatch(create_dispatch("work", tier="lane")).telemetry_era is None


def test_malformed_cutover_reads_as_unconfigured(ledger_runs):
    _write_config(ledger_runs, {"telemetry_era": "someday"})
    assert load_dispatch(create_dispatch("work", tier="lane")).telemetry_era is None


# === manifest deliverable<->check mapping ===

def test_manifest_check_map_roundtrips(ledger_runs):
    manifest = {
        "deliverables": ["report.html"],
        "checks": ["html-exists"],
        "check_map": {"report.html": ["html-exists"]},
    }
    record = load_dispatch(create_dispatch("mapped work", manifest=manifest))
    assert record.manifest["check_map"] == {"report.html": ["html-exists"]}


def test_manifest_without_check_map_keeps_its_shape(ledger_runs):
    # Additive: absent must stay absent on disk, and readers treat it as empty.
    record = load_dispatch(create_dispatch("unmapped work",
                                           manifest={"deliverables": ["x"]}))
    assert "check_map" not in record.manifest


def test_malformed_check_map_rejected(ledger_runs):
    for bad in ("not-an-object", {"d": "not-a-list"}, {"d": [1, 2]}):
        with pytest.raises(LedgerError):
            create_dispatch("work", manifest={"check_map": bad})


# === terminate reasons: the ladder's machine reason and the parked namespace ===

def test_close_accepts_the_machine_abandoned_reason(ledger_runs):
    from fleetproof.ledger import REASON_ABANDONED
    run_id = create_dispatch("work", tier="lane")
    record_report(run_id, _ok_report())
    record_verdict(run_id, STATE_CONTRADICTED)
    record = close_dispatch(run_id, reason=REASON_ABANDONED)
    assert record.terminate_reason == REASON_ABANDONED


def test_close_accepts_a_parked_reason_with_text(ledger_runs):
    from fleetproof.ledger import REASON_PARKED_PREFIX
    run_id = create_dispatch("work", tier="lane")
    record = close_dispatch(run_id, reason=REASON_PARKED_PREFIX + "waiting on operator")
    assert record.terminate_reason == "parked: waiting on operator"


def test_close_rejects_a_blank_parked_reason(ledger_runs):
    # The prefix namespaces the one open-ended reason; an empty remainder is
    # a reason-shaped non-reason, and the vocabulary stays closed otherwise.
    run_id = create_dispatch("work", tier="lane")
    with pytest.raises(LedgerError):
        close_dispatch(run_id, reason="parked: ")
    with pytest.raises(LedgerError):
        close_dispatch(run_id, reason="parked:   ")


# === invented tiers name the legal values ===

def test_invented_tier_error_names_the_legal_tiers(ledger_runs):
    # "unknown tier 'lead'" alone sends the operator to the docs for the
    # vocabulary; the rejection carries it (asked for from a field deployment
    # on Windows).
    with pytest.raises(LedgerError) as excinfo:
        create_dispatch("work", tier="lead")
    msg = str(excinfo.value)
    assert "unknown tier 'lead'" in msg
    assert "legal tiers: bridge, coordinator, lane, leaf" in msg


def test_invented_tier_in_write_intent_names_the_legal_tiers(ledger_runs, tmp_path):
    from fleetproof.ledger import write_intent
    with pytest.raises(LedgerError) as excinfo:
        write_intent("tester", "work", tier="captain")
    assert "unknown tier 'captain'" in str(excinfo.value)
    assert "legal tiers: bridge, coordinator, lane, leaf" in str(excinfo.value)


def test_invented_tier_in_a_sidecar_note_names_the_legal_tiers(ledger_runs, tmp_path):
    # The consume side degrades instead of raising (it runs inside a hook),
    # but its degradation note must carry the same vocabulary.
    from fleetproof.ledger import consume_intent, intents_dir
    intents_dir().mkdir(parents=True, exist_ok=True)
    (intents_dir() / "tester.json").write_text(json.dumps({
        "agent_type": "tester", "prompt": "work", "tier": "captain",
    }), encoding="utf-8")
    fields, notes = consume_intent("tester")
    assert fields is not None and fields["tier"] is None
    assert any("unknown tier 'captain'" in n
               and "legal tiers: bridge, coordinator, lane, leaf" in n
               for n in notes)


# === every report and every block is kept ===

def test_every_report_is_kept_as_a_numbered_file(ledger_runs):
    # report.json is latest-wins; a retry used to erase the prior claim's
    # body (two of three gone — observed in a field deployment on Windows).
    from fleetproof.ledger import record_block
    run_id = create_dispatch("work", tier="lane")
    record_report(run_id, _ok_report("first claim"))
    record_verdict(run_id, STATE_CONTRADICTED, detail="x")
    record_report(run_id, _ok_report("second claim"))
    record_verdict(run_id, STATE_CONTRADICTED, detail="x")
    record_report(run_id, _ok_report("third claim"))

    record = load_dispatch(run_id)
    reports_dir = record.run_dir / "reports"
    assert sorted(p.name for p in reports_dir.iterdir()) == [
        "001.json", "002.json", "003.json"]
    assert record.report_count == 3
    assert [r["summary"] for r in record.load_reports()] == [
        "first claim", "second claim", "third claim"]
    # report.json still holds the latest, byte-for-byte the same payload.
    assert record.load_report() == json.loads((reports_dir / "003.json").read_text())
    assert record.to_dict()["report_count"] == 3
    assert record.to_dict()["block_count"] == 0


def test_record_block_keeps_the_verbatim_text_and_the_checker_run(ledger_runs):
    from fleetproof.ledger import record_block
    run_id = create_dispatch("work", tier="lane")
    text = "[GATE]\nline two: exact bytes, with a trailing newline\n"
    path = record_block(run_id, text, checker_run_id="20260825-000000-abcdef")
    record_block(run_id, "second block", checker_run_id=None)

    record = load_dispatch(run_id)
    assert path.name == "001.txt"
    assert path.read_text(encoding="utf-8") == text
    assert record.block_count == 2
    blocks = record.load_blocks()
    assert [b["seq"] for b in blocks] == ["001", "002"]
    assert blocks[0]["text"] == text
    assert blocks[0]["checker_run_id"] == "20260825-000000-abcdef"
    assert blocks[0]["at"]
    assert blocks[1]["checker_run_id"] is None


def test_sequence_numbers_skip_over_a_gap(ledger_runs):
    # Derived from the highest stem, not the count: a deleted 001 must not
    # let a later attempt be written as a second 002.
    from fleetproof.ledger import record_block
    run_id = create_dispatch("work", tier="lane")
    record_block(run_id, "a")
    record_block(run_id, "b")
    (load_dispatch(run_id).run_dir / "blocks" / "001.txt").unlink()
    path = record_block(run_id, "c")
    assert path.name == "003.txt"


def test_old_layout_dispatch_dir_without_reports_or_blocks_still_loads(ledger_runs):
    # A 0.4.0 dispatch dir: report.json alone, no reports/ and no blocks/.
    run_id = create_dispatch("work", tier="lane")
    record_report(run_id, _ok_report("only claim"))
    run_dir = load_dispatch(run_id).run_dir
    import shutil
    shutil.rmtree(run_dir / "reports")
    assert not (run_dir / "reports").exists()
    assert not (run_dir / "blocks").exists()

    record = load_dispatch(run_id)
    assert record is not None
    assert record.has_report
    assert record.report_count == 1
    assert record.load_reports() == [record.load_report()]
    assert record.block_count == 0
    assert record.load_blocks() == []
    d = record.to_dict()
    assert d["report_count"] == 1 and d["block_count"] == 0


# === inherited tier_source ===

def test_inherited_tier_source_requires_its_source_and_vice_versa(ledger_runs):
    from fleetproof.ledger import TIER_SOURCE_INHERITED
    with pytest.raises(LedgerError):
        create_dispatch("x", tier="lane", tier_source=TIER_SOURCE_INHERITED)
    with pytest.raises(LedgerError):
        create_dispatch("x", tier="lane", inherited_from="20260101-000000-aaaaaa")
    source = create_dispatch("declared", tier="lane")
    run_id = create_dispatch("x", tier="lane", tier_source=TIER_SOURCE_INHERITED,
                             inherited_from=source)
    record = load_dispatch(run_id)
    assert record.tier_source == "inherited"
    assert record.inherited_from == source
    assert record.to_dict()["inherited_from"] == source
    # A 0.4.0 record has no such field; it reads back as None.
    assert load_dispatch(source).inherited_from is None

