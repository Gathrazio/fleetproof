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
    infer_tier,
    list_dispatches,
    load_dispatch,
    record_report,
    record_verdict,
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


def test_malformed_explicit_manifest_raises(ledger_runs):
    with pytest.raises(LedgerError):
        create_dispatch("work", manifest={"checks": "tests-pass"})
    with pytest.raises(LedgerError):
        create_dispatch("work", manifest=["not", "an", "object"])
