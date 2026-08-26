"""Tests for the telemetry summary formulas and the export pipeline.

The summary tests pin every §5.3 formula's numerator and denominator over a
corpus containing all nine outcome classes plus the three census buckets —
because which class sits where changes headline rates by integer factors, and
the flattering variant is exactly the one the formulas exist to forbid. The
export tests are adversarial: operator strings injected into every field must
not survive, exact times must not survive, and two recipients' bundles must
not join.
"""

from __future__ import annotations

import json
import re
from datetime import date

import pytest

from fleetproof import runlog
from fleetproof.checker import CheckReport
from fleetproof.ledger import (
    close_dispatch,
    create_dispatch,
    load_dispatch,
    record_report,
    record_verdict,
)
from fleetproof.telemetry import (
    build_telemetry,
    chain_head,
    load_telemetry,
    record_failure_loss,
)
from fleetproof.telemetry_export import (
    VERIFIED_MEANING,
    anchor_chain,
    census,
    export_telemetry,
    salt_for_recipient,
    summarize,
)


@pytest.fixture
def era_runs(tmp_runs, monkeypatch):
    monkeypatch.delenv(runlog.PARENT_RUN_ID_ENV, raising=False)
    (tmp_runs.parent / "config.json").write_text(
        json.dumps({"telemetry_era": "2026-01-01"}), encoding="utf-8")
    return tmp_runs


def _report(summary="did the thing"):
    return {"summary": summary, "deliverables": []}


def _finished(verdicts=("verified",), reports=("claim",), close=True, agent=None):
    """A dispatch driven through reports/verdicts alternately, then closed."""
    run_id = create_dispatch("work", tier="lane", agent=agent)
    for i, verdict in enumerate(verdicts):
        record_report(run_id, _report(reports[i] if i < len(reports) else f"r{i}"))
        record_verdict(run_id, verdict)
    if close:
        close_dispatch(run_id)
    return run_id


def _full_corpus(era_runs):
    """Every §5.1 class once (verified three times, one stop-only), plus the
    telemetry_missing / pre_telemetry / in_flight census buckets."""
    ids = {}
    ids["verified_a"] = _finished()
    ids["verified_b"] = _finished()
    ids["verified_stop_only"] = _finished(agent={
        "agent_id": "a9", "agent_type": "tester", "capture": "stop-only"})
    ids["contradicted"] = _finished(verdicts=("contradicted",))
    ids["near_miss"] = _finished(verdicts=("contradicted", "verified"),
                                 reports=("broken", "fixed"))
    ids["flake"] = _finished(verdicts=("contradicted", "verified"),
                             reports=("same", "same"))
    # Reported, terminated, never graded — the gate's own failure mode.
    ids["ungraded"] = create_dispatch("work", tier="lane")
    record_report(ids["ungraded"], _report())
    close_dispatch(ids["ungraded"])
    # Graded with an empty selection.
    ids["unverifiable"] = create_dispatch("work", tier="lane")
    record_report(ids["unverifiable"], _report())
    close_dispatch(ids["unverifiable"])
    # Terminated from dispatched, three reasons.
    ids["silent_idle"] = create_dispatch("work", tier="lane")
    close_dispatch(ids["silent_idle"], reason="sweep-idle")
    ids["terminated_unreported"] = create_dispatch("work", tier="lane")
    close_dispatch(ids["terminated_unreported"], reason="operator-close")
    ids["terminated_unclassified"] = create_dispatch("work", tier="lane")
    close_dispatch(ids["terminated_unclassified"])
    # Still running.
    ids["in_flight"] = create_dispatch("work", tier="lane")
    # Pre-telemetry: created while no cutover is configured.
    config = era_runs.parent / "config.json"
    saved = config.read_text(encoding="utf-8")
    config.unlink()
    ids["pre_telemetry"] = _finished()
    config.write_text(saved, encoding="utf-8")

    for key, run_id in ids.items():
        if key in ("in_flight", "pre_telemetry"):
            continue
        if key == "unverifiable":
            build_telemetry(run_id, check_report=CheckReport(tier="lane"), checks=[])
        else:
            build_telemetry(run_id)
    # The adversarial fixture: an era run whose telemetry file is deleted
    # after the fact must surface as telemetry_missing, never re-bucket.
    ids["missing"] = _finished()
    build_telemetry(ids["missing"])
    (load_dispatch(ids["missing"]).run_dir / "telemetry.json").unlink()
    return ids


def test_census_buckets_every_dispatch_exactly_once(era_runs):
    ids = _full_corpus(era_runs)
    buckets = {row["run_id"]: row["bucket"] for row in census()}
    assert len(buckets) == len(ids)
    assert buckets[ids["verified_a"]] == "verified"
    assert buckets[ids["near_miss"]] == "near_miss"
    assert buckets[ids["flake"]] == "verifier_flake"
    assert buckets[ids["contradicted"]] == "contradicted"
    assert buckets[ids["ungraded"]] == "ungraded"
    assert buckets[ids["unverifiable"]] == "unverifiable"
    assert buckets[ids["silent_idle"]] == "silent_idle"
    assert buckets[ids["terminated_unreported"]] == "terminated_unreported"
    assert buckets[ids["terminated_unclassified"]] == "terminated_unclassified"
    assert buckets[ids["in_flight"]] == "in_flight"
    assert buckets[ids["pre_telemetry"]] == "pre_telemetry"
    assert buckets[ids["missing"]] == "telemetry_missing"


def test_summary_formulas_pin_numerators_and_denominators(era_runs):
    _full_corpus(era_runs)
    block = summarize()["windows"]["full"]
    # D = the eleven classifiable era dispatches; missing and in-flight are
    # counted in their own rates, pre-telemetry in none.
    assert block["classifiable_dispatches"] == 11
    assert block["telemetry_era_dispatches"] == 13
    assert block["total_dispatches"] == 14

    m = block["delivery_failure_rate"]
    assert (m["numerator"], m["denominator"]) == (3, 11)
    assert m["per_1k_dispatches"] == pytest.approx(3000 / 11)
    # Unobserved co-denominators are unavailable, never zero.
    assert m["per_1M_tokens"] is None and m["per_1k_tool_calls"] is None

    m = block["false_claim_rate"]
    assert (m["numerator"], m["denominator"]) == (1, 6)  # 3v + 1c + 1nm + 1fl

    assert (block["near_miss_rate"]["numerator"],
            block["near_miss_rate"]["denominator"]) == (1, 11)
    assert (block["verifier_flake_rate"]["numerator"],
            block["verifier_flake_rate"]["denominator"]) == (1, 11)
    assert (block["ungraded_rate"]["numerator"],
            block["ungraded_rate"]["denominator"]) == (1, 11)
    assert (block["unverifiable_rate"]["numerator"],
            block["unverifiable_rate"]["denominator"]) == (1, 11)
    # The plain-English companion total, with its split attached.
    m = block["no_verdict_rate"]
    assert (m["numerator"], m["denominator"]) == (2, 11)
    assert (m["ungraded"], m["unverifiable"]) == (1, 1)
    assert (block["telemetry_missing_rate"]["numerator"],
            block["telemetry_missing_rate"]["denominator"]) == (1, 13)
    assert (block["stop_only_fraction"]["numerator"],
            block["stop_only_fraction"]["denominator"]) == (1, 13)
    # Three gate blocks (contradicted, near-miss, flake retries), no overrides.
    m = block["override_rate_by_source"]["hook"]
    assert (m["numerator"], m["denominator"]) == (0, 3)


def test_escalated_rate_splits_the_laundering_faces(era_runs):
    # Decision 0012 (b): the escalated total is a bare frequency that can hide a
    # reclassified failure. The split rides alongside, additive — the total rate
    # is unchanged — so a contradicted-then-parked or never-reported escalation
    # is visible in the aggregate, not just the per-record trail.
    from fleetproof.ledger import REASON_PARKED_PREFIX
    over = create_dispatch("work", tier="lane")
    record_report(over, _report("cannot satisfy"))
    record_verdict(over, "contradicted")
    close_dispatch(over, reason=REASON_PARKED_PREFIX + "unsat",
                   park_unsatisfiable=True)
    build_telemetry(over)
    unrep = create_dispatch("work", tier="lane")
    close_dispatch(unrep, reason=REASON_PARKED_PREFIX + "unsat",
                   park_unsatisfiable=True)
    build_telemetry(unrep)
    clean = create_dispatch("work", tier="lane")
    record_report(clean, _report("cannot be satisfied from this seat"))
    close_dispatch(clean, reason=REASON_PARKED_PREFIX + "unsat",
                   park_unsatisfiable=True)
    build_telemetry(clean)

    m = summarize()["windows"]["full"]["escalated_rate"]
    # Unchanged total: 3 escalated of 3 classifiable.
    assert (m["numerator"], m["denominator"]) == (3, 3)
    assert m["escalated_over_contradiction"] == 1
    assert m["escalated_unreported"] == 1
    assert m["escalated_clean"] == 1


def test_summary_severity_distribution_counts_unanswered(era_runs):
    ids = _full_corpus(era_runs)
    record_failure_loss(ids["contradicted"], 3, "hours")  # $300 -> S2
    sev = summarize()["windows"]["full"]["severity_distribution"]
    assert sev["S2"] == 1       # the answered contradicted run
    assert sev["S0"] == 1       # the near miss, by definition
    assert sev["unanswered"] == 0


def test_empty_corpus_rates_are_none_not_zero(era_runs):
    block = summarize()["windows"]["full"]
    assert block["delivery_failure_rate"]["rate"] is None
    assert block["telemetry_missing_rate"]["rate"] is None


def test_trailing_windows_exclude_old_runs(era_runs):
    old = _finished()
    build_telemetry(old)
    record = load_dispatch(old)
    root = json.loads((record.run_dir / "_root.json").read_text(encoding="utf-8"))
    root["started_at"] = "2020-01-01T00:00:00+00:00"
    (record.run_dir / "_root.json").write_text(json.dumps(root), encoding="utf-8")
    recent = _finished()
    build_telemetry(recent)

    windows = summarize()["windows"]
    assert windows["full"]["counts"]["verified"] == 2
    assert windows["trailing_30d"]["counts"]["verified"] == 1


def test_spec_hash_timeline_shows_regime_changes(era_runs, tmp_path):
    spec_a = tmp_path / "spec-a.json"
    spec_b = tmp_path / "spec-b.json"
    spec_a.write_text('{"checks": []}', encoding="utf-8")
    spec_b.write_text('{"checks": [ ]}', encoding="utf-8")
    for spec in (spec_a, spec_b):
        run_id = create_dispatch("work", tier="lane", spec_path=spec)
        close_dispatch(run_id, reason="operator-close")
        build_telemetry(run_id)
    timeline = summarize()["spec_hash_timeline"]
    assert len(timeline) == 2
    assert timeline[0]["spec_sha256"] != timeline[1]["spec_sha256"]


# === export: the allowlist is the whole security model ===

_MARKER = "SECRET MARKER c:/users/noah/monique-case"


def _tampered_run(era_runs):
    """A verified era run whose telemetry file has operator strings injected
    into every string-capable field, plus fields that do not exist at all."""
    run_id = _finished()
    build_telemetry(run_id)
    record = load_dispatch(run_id)
    t = load_telemetry(run_id)
    for key, value in list(t.items()):
        if value is None or isinstance(value, str):
            t[key] = _MARKER
    t["prompt_dump"] = _MARKER                      # unknown field
    t["outcome.verifier"] = {"verifier": _MARKER, "checks_run": 1,
                             "check_kind_counts": {_MARKER: 1}}
    t["oversight.events"] = [{"type": _MARKER, "source": _MARKER,
                              "at": "2026-02-01T12:34:56+00:00"}]
    t["failure.root_cause"] = _MARKER
    t["agent_type"] = _MARKER
    t["eval_suite_id"] = _MARKER
    t["deployment_id"] = _MARKER
    (record.run_dir / "telemetry.json").write_text(
        json.dumps(t), encoding="utf-8")
    return run_id


def test_no_operator_string_survives_export(era_runs, tmp_path):
    _tampered_run(era_runs)
    out = export_telemetry("acme", out_dir=tmp_path / "export")
    for name in ("telemetry-export.json", "methodology.md"):
        text = (out / name).read_text(encoding="utf-8")
        assert "SECRET" not in text, name
        assert "noah" not in text.lower(), name
        assert "monique" not in text.lower(), name
    # And the tampered record still exported (as nulls), not silently dropped.
    bundle = json.loads((out / "telemetry-export.json").read_text(encoding="utf-8"))
    assert len(bundle["records"]) == 1
    assert bundle["records"][0]["outcome.class"] == "verified"  # re-derived


def test_exports_carry_no_exact_timestamps(era_runs, tmp_path):
    run_id = _finished(verdicts=("contradicted", "verified"),
                       reports=("broken", "fixed"))
    build_telemetry(run_id)
    out = export_telemetry("acme", out_dir=tmp_path / "export")
    for name in ("telemetry-export.json", "methodology.md"):
        text = (out / name).read_text(encoding="utf-8")
        assert re.search(r"\d{2}:\d{2}:\d{2}", text) is None, name
    record = json.loads(
        (out / "telemetry-export.json").read_text(encoding="utf-8"))["records"][0]
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", record["record_date"])
    # Durations leave only as bands.
    assert record["exposure.elapsed_band"] == "lt-1m"
    assert "exposure.elapsed_seconds" not in record


def test_completeness_manifest_counts_every_bucket(era_runs, tmp_path):
    _full_corpus(era_runs)
    out = export_telemetry("acme", out_dir=tmp_path / "export")
    bundle = json.loads((out / "telemetry-export.json").read_text(encoding="utf-8"))
    by_class = {}
    for entry in bundle["completeness"]:
        by_class[entry["class"]] = by_class.get(entry["class"], 0) + entry["count"]
    assert by_class["verified"] == 3
    assert by_class["telemetry_missing"] == 1
    assert by_class["pre_telemetry"] == 1
    assert by_class["in_flight"] == 1
    assert by_class["ungraded"] == 1
    assert sum(by_class.values()) == 14
    # Excluded-from-records buckets are visible, not silently gone.
    assert len(bundle["records"]) == 11


def test_two_recipients_cannot_join_their_bundles(era_runs, tmp_path):
    run_id = _finished(verdicts=("contradicted",))
    build_telemetry(run_id)
    out_a = export_telemetry("recipient-a", out_dir=tmp_path / "a")
    out_b = export_telemetry("recipient-b", out_dir=tmp_path / "b")
    rec_a = json.loads((out_a / "telemetry-export.json").read_text(encoding="utf-8"))["records"][0]
    rec_b = json.loads((out_b / "telemetry-export.json").read_text(encoding="utf-8"))["records"][0]
    assert rec_a["run_ref"] != rec_b["run_ref"]
    assert rec_a["incident_ref"] != rec_b["incident_ref"] \
        if "incident_ref" in rec_a else True
    assert rec_a["failure.incident_ref"] != rec_b["failure.incident_ref"]
    # Same recipient twice: stable salt, stable refs.
    out_a2 = export_telemetry("recipient-a", out_dir=tmp_path / "a2")
    rec_a2 = json.loads((out_a2 / "telemetry-export.json").read_text(encoding="utf-8"))["records"][0]
    assert rec_a2["run_ref"] == rec_a["run_ref"]


def test_salt_requires_a_recipient_label(era_runs):
    from fleetproof.telemetry import TelemetryError
    with pytest.raises(TelemetryError):
        salt_for_recipient("   ")


def test_methodology_note_leads_with_the_hard_disclosures(era_runs, tmp_path):
    run_id = _finished()
    build_telemetry(run_id)
    out = export_telemetry("acme", out_dir=tmp_path / "export")
    text = (out / "methodology.md").read_text(encoding="utf-8")
    assert VERIFIED_MEANING in text
    assert "identified, minimized" in text
    assert "operator-selected sample" in text
    assert "operator-attested" in text
    assert "1 operator(s), 1 harness(es)" in text


# === append-time chain + anchor ===

def test_chain_grows_at_verdict_time_and_anchors(era_runs):
    head0, count0 = chain_head()
    assert count0 == 0
    run_id = _finished()
    build_telemetry(run_id)
    head1, count1 = chain_head()
    assert count1 == 1 and head1 != head0
    run_id2 = _finished()
    build_telemetry(run_id2)
    head2, count2 = chain_head()
    assert count2 == 2 and head2 != head1

    anchor = anchor_chain()
    assert anchor["chain_head"] == head2
    assert anchor["chain_entries"] == 2
    stored = json.loads(
        (era_runs.parent / "telemetry-anchor.json").read_text(encoding="utf-8"))
    assert stored["chain_head"] == head2


def test_export_carries_the_chain_head(era_runs, tmp_path):
    run_id = _finished()
    build_telemetry(run_id)
    head, entries = chain_head()
    out = export_telemetry("acme", out_dir=tmp_path / "export")
    bundle = json.loads((out / "telemetry-export.json").read_text(encoding="utf-8"))
    assert bundle["integrity"] == {"chain_head": head, "chain_entries": entries}


# === CLI surfaces ===

def test_cli_telemetry_verbs(era_runs, monkeypatch, capsys, tmp_path):
    from fleetproof.cli import main
    run_id = _finished(verdicts=("contradicted",))
    build_telemetry(run_id)

    assert main(["telemetry", "summary"]) == 0
    out = capsys.readouterr().out
    assert "LOCAL-ONLY" in out
    assert "delivery_failure_rate: 1/1" in out

    assert main(["telemetry", "loss", run_id, "--value", "2", "--unit", "hours"]) == 0
    out = capsys.readouterr().out
    assert "S2" in out

    assert main(["telemetry", "export", "--recipient", "acme",
                 "-o", str(tmp_path / "cli-export")]) == 0
    out = capsys.readouterr().out
    assert "identified, minimized" in out

    assert main(["telemetry", "anchor"]) == 0
    assert "chain head:" in capsys.readouterr().out

    assert main(["telemetry", "export", "--recipient", "acme",
                 "--since", "not-a-date"]) == 2


def test_abandoned_is_its_own_class_and_counts_as_a_failure(era_runs):
    # Not verified, not unverifiable, never folded into contradicted — but a
    # delivery failure and a false claim in every rate that publishes those.
    from fleetproof.ledger import REASON_ABANDONED
    run_id = create_dispatch("work", tier="lane")
    for attempt in range(3):
        record_report(run_id, _report(f"attempt {attempt}"))
        record_verdict(run_id, "contradicted")
    close_dispatch(run_id, by="hook", reason=REASON_ABANDONED)
    build_telemetry(run_id)

    block = summarize()["windows"]["full"]
    assert block["counts"]["abandoned"] == 1
    assert block["counts"]["contradicted"] == 0  # never double-counted
    m = block["delivery_failure_rate"]
    assert (m["numerator"], m["denominator"]) == (1, 1)
    m = block["false_claim_rate"]
    assert (m["numerator"], m["denominator"]) == (1, 1)
    m = block["abandoned_rate"]
    assert (m["numerator"], m["denominator"]) == (1, 1)


def test_advisory_class_counts_in_d_and_exports_as_its_own_enum(era_runs):
    from fleetproof.telemetry import CLASS_ADVISORY
    run_id = create_dispatch("work", tier="coordinator")
    record_report(run_id, _report())
    record_verdict(run_id, "advisory")
    close_dispatch(run_id)
    build_telemetry(run_id)
    block = summarize()["windows"]["full"]
    assert block["counts"][CLASS_ADVISORY] == 1
    assert block["classifiable_dispatches"] == 1
    assert (block["advisory_rate"]["numerator"], block["advisory_rate"]["denominator"]) == (1, 1)
    # Not a graded claim (no false-claim denominator), not a delivery failure.
    assert block["false_claim_rate"]["denominator"] == 0
    assert block["delivery_failure_rate"]["numerator"] == 0
    out = export_telemetry("partner", out_dir=era_runs.parent / "export")
    bundle = json.loads((out / "telemetry-export.json").read_text(encoding="utf-8"))
    assert bundle["records"][0]["outcome.class"] == CLASS_ADVISORY


def test_escalated_class_is_quarantined_like_advisory(era_runs):
    # A flagged unsatisfiable park: in D, in escalated_rate, in no success
    # and no failure numerator — the advisory quarantine, applied to the
    # operator-accepted escalation exit.
    from fleetproof.ledger import REASON_PARKED_PREFIX
    from fleetproof.telemetry import CLASS_ESCALATED
    run_id = create_dispatch("work", tier="lane")
    record_report(run_id, _report())
    record_verdict(run_id, "contradicted")
    close_dispatch(run_id, reason=REASON_PARKED_PREFIX + "unsatisfiable",
                   park_unsatisfiable=True)
    build_telemetry(run_id)
    block = summarize()["windows"]["full"]
    assert block["counts"][CLASS_ESCALATED] == 1
    assert block["counts"]["contradicted"] == 0  # reclassed, not double-counted
    assert block["classifiable_dispatches"] == 1
    assert (block["escalated_rate"]["numerator"],
            block["escalated_rate"]["denominator"]) == (1, 1)
    # Not a graded claim gone wrong, not a delivery failure.
    assert block["false_claim_rate"]["numerator"] == 0
    assert block["delivery_failure_rate"]["numerator"] == 0
    out = export_telemetry("partner", out_dir=era_runs.parent / "export")
    bundle = json.loads((out / "telemetry-export.json").read_text(encoding="utf-8"))
    assert bundle["records"][0]["outcome.class"] == CLASS_ESCALATED
