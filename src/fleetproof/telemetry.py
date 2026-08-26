"""Verification telemetry — the per-run record an operator can reason about fleets from.

Every FleetProof-verified dispatch already leaves a claim (report.json, the
agent's own words) and a verdict trail (dispatch.json transitions, appended by
the checker). This module condenses the two into ``telemetry.json`` — one flat
record per run, in a closed vocabulary, that aggregates and exports can be
computed from without re-reading anyone's prose.

Three rules shape everything here:

*Written by the checker's side, never the agent's.* ``build_telemetry`` runs at
verdict/close time, from the hook gate or the operator's CLI — the same process
boundary the verdict itself crosses. The reporting agent's code path never
touches this file.

*Null, never guessed.* A field whose value was not observed is ``null``. A
stop-only capture (the start hook never fired) has no observation window, so
every exposure field is null — zeros there would be wrong-as-fact and would
corrupt every frequency denominator downstream. Same posture for anything the
harness does not expose (model, tokens, permission mode): absent is recorded
as absent.

*Pre-telemetry is never back-filled.* A dispatch without the era stamp predates
the cutover; building telemetry for it would be inventing history, so
``build_telemetry`` refuses. Conversely an era-stamped dispatch that *lacks*
telemetry.json is an integrity defect (``telemetry_missing``), surfaced by the
summary — never silently reclassified as pre-telemetry, because file absence
is exactly what an agent can manufacture.

Field names follow the schema spec verbatim as flat dotted keys (the OTel
attribute convention): ``outcome.class``, ``exposure.retry_count``,
``fleetproof.run_context``, and so on. The derivation of ``outcome.class`` is
code and will change, so it is versioned per record (``derivation_version``);
an unversioned derivation would silently mix class semantics across a corpus.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from . import __version__
from .config import (
    deployment_id,
    eval_suite_id,
    load_config,
    operator_id,
    run_context,
)
from .ledger import (
    CAPTURE_STOP_ONLY,
    REASON_ABANDONED,
    REASON_OPERATOR_CLOSE,
    REASON_SESSION_END,
    REASON_SWEEP_IDLE,
    STATE_ADVISORY,
    STATE_CONTRADICTED,
    STATE_REPORTED,
    STATE_VERIFIED,
    DispatchRecord,
    is_parked_reason,
    load_dispatch,
)
from .runlog import runs_dir

TELEMETRY_FILENAME = "telemetry.json"

SCHEMA_VERSION = "fleetproof-telemetry/1.1"

# The outcome-class derivation below implements the spec's nine-row table
# (its derivation_version 2; version 1 being the pre-red-team draft that
# lacked ungraded and the near-miss/flake split) plus the ``abandoned`` class
# the escalation ladder added — a tenth row, hence version 3 — plus the
# ``advisory`` class per-tier arming added: an eleventh row, version 4.
# Purely additive each time: no record written under version 3 can carry an
# advisory verdict transition, so re-deriving an older corpus under this
# version classes every record exactly as before.
# Version 5 makes two moves at once. The ``escalated`` class (a park carrying
# the --unsatisfiable marker): additive, because no earlier record carries the
# marker, so older corpora re-derive unchanged. And ``_coverage`` returning
# null instead of 0.0 when deliverables exist but the manifest maps no checks
# to them: NOT class-additive — a rebuilt record shifts 0.0 -> null for that
# situation — which is exactly why the version moves instead of the shift
# passing silently.
DERIVATION_VERSION = 5

# Every vocabulary a record leans on is pinned per record. The two nulls are
# honest, not lazy: no OTel-named field carries a non-null value yet (the
# harness exposes no model/provider to hooks), so no gen-ai semconv release is
# actually being interpreted; and no failure-mode taxonomy has been adopted
# into code yet, so there is no taxonomy version to claim. Both get real values
# the release that first populates the fields they govern.
SEMCONV_VERSION = None
TAXONOMY_VERSION = None
TASK_CLASSIFIER_VERSION = "fleetproof-task-heuristic/1"

# Published labor-rate assumption for converting hour-denominated losses to
# dollars, recorded on every failure record so a future recalibration can
# re-derive rather than orphan. Dollars per hour.
LABOR_RATE_USD_PER_HOUR = 100

# === The closed outcome-class set (spec §5.1, derivation_version 2) ===

CLASS_VERIFIED = "verified"
CLASS_NEAR_MISS = "near_miss"
CLASS_VERIFIER_FLAKE = "verifier_flake"
CLASS_CONTRADICTED = "contradicted"
# The escalation ladder's terminal outcome: three contradicted stops on one
# dispatch, abandoned by the gate. Its own class — not folded into
# contradicted (a single wrong claim and an agent that wedged for three
# rounds are different fleet problems), and by construction never verified.
CLASS_ABANDONED = "abandoned"
# A blocking check failed and the tier's gate was disarmed, so the stop was
# allowed on an operator's recorded switch. Its own class: not verified (a
# check failed), not contradicted (nothing blocked and the ladder did not
# strike), not unverifiable (something checkable was checked — and failed).
# Never counts as success anywhere.
CLASS_ADVISORY = "advisory"
# A park carrying the operator's --unsatisfiable marker: the agent reported
# the work cannot be satisfied from its seat and the operator agreed, on
# record. Its own class — not contradicted (correctly refusing unsatisfiable
# work is not a false claim), and never a success (nothing was delivered).
# Quarantined exactly like ``advisory``: in the denominator, in no success
# and no failure numerator, no incident attached — a lane must never learn
# that escalating scores worse than guessing.
CLASS_ESCALATED = "escalated"
CLASS_UNGRADED = "ungraded"
CLASS_UNVERIFIABLE = "unverifiable"
CLASS_SILENT_IDLE = "silent_idle"
CLASS_TERMINATED_UNREPORTED = "terminated_unreported"
CLASS_TERMINATED_UNCLASSIFIED = "terminated_unclassified"

OUTCOME_CLASSES = (
    CLASS_VERIFIED,
    CLASS_NEAR_MISS,
    CLASS_VERIFIER_FLAKE,
    CLASS_CONTRADICTED,
    CLASS_ABANDONED,
    CLASS_ADVISORY,
    CLASS_ESCALATED,
    CLASS_UNGRADED,
    CLASS_UNVERIFIABLE,
    CLASS_SILENT_IDLE,
    CLASS_TERMINATED_UNREPORTED,
    CLASS_TERMINATED_UNCLASSIFIED,
)

# === Severity bands (spec §5.2) — derived, monotone in dollars ===

SEVERITY_BANDS = ("S0", "S1", "S2", "S3", "S4")

LOSS_UNIT_HOURS = "hours"
LOSS_UNIT_USD = "usd"
VALID_LOSS_UNITS = frozenset({LOSS_UNIT_HOURS, LOSS_UNIT_USD})

CAPTURE_MODE = "all non-operator fields machine-captured"


class TelemetryError(Exception):
    """Raised on an unusable telemetry operation (bad loss unit, unknown run)."""


# === Task classification heuristics (deliberately dumb, versioned) ===
#
# Same posture as manifest derivation v1: no inference beyond keyword matching,
# no model call, and a pinned classifier version so a smarter successor can be
# told apart in the corpus instead of silently shifting the frequency tables.
# First matching category in a fixed order wins; ties are resolved by that
# order, which is part of what the version pins.

_DOMAIN_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("code", ("code", "refactor", "bug", "compile", "implement", "module",
              "function", "script", "repo", "test", "lint")),
    ("data", ("data", "dataset", "csv", "database", "sql", "etl", "schema")),
    ("document", ("document", "draft", "memo", "report", "summary", "doc",
                  "write-up", "chapter")),
    ("research", ("research", "investigate", "survey", "recon", "compare",
                  "evaluate", "benchmark")),
    ("ops", ("deploy", "install", "configure", "server", "backup", "monitor",
             "release", "provision")),
    ("comms", ("email", "message", "notify", "post", "reply", "announce")),
)

_ACTION_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("modify", ("fix", "modify", "update", "refactor", "edit", "change",
                "rename", "patch", "migrate")),
    ("create", ("create", "build", "write", "add", "implement", "draft",
                "new", "generate")),
    ("analyze", ("analyze", "analyse", "investigate", "review", "compare",
                 "audit", "research", "survey", "evaluate")),
    ("execute", ("run", "execute", "deploy", "install", "launch")),
)


def _keyword_match(prompt: str, table: tuple[tuple[str, tuple[str, ...]], ...]) -> str | None:
    text = (prompt or "").lower()
    for label, keywords in table:
        for word in keywords:
            if re.search(rf"\b{re.escape(word)}\b", text):
                return label
    return None


def classify_task_domain(prompt: str) -> str:
    """The spec's task.domain enum; ``other`` when nothing matches."""
    return _keyword_match(prompt, _DOMAIN_KEYWORDS) or "other"


def classify_action_class(prompt: str) -> str | None:
    """The spec's task.action_class enum; None when nothing matches.

    The enum has no ``other``, and a guessed action class is worse than an
    absent one, so an unmatched prompt classifies as null.
    """
    return _keyword_match(prompt, _ACTION_KEYWORDS)


# === Severity derivation ===

def loss_to_usd(value: float, unit: str) -> float:
    """A raw loss quantity in dollars, at the published labor-rate assumption."""
    if unit == LOSS_UNIT_USD:
        return float(value)
    if unit == LOSS_UNIT_HOURS:
        return float(value) * LABOR_RATE_USD_PER_HOUR
    raise TelemetryError(f"Unknown loss unit {unit!r}; expected one of {sorted(VALID_LOSS_UNITS)}.")


def severity_band_for_usd(usd: float) -> str:
    """The §5.2 band for a dollar loss. Monotone; edges belong to the upper band."""
    if usd <= 0:
        return "S0"
    if usd < 100:
        return "S1"
    if usd < 1000:
        return "S2"
    if usd < 10000:
        return "S3"
    return "S4"


def _escalate_band(band: str, steps: int = 1) -> str:
    idx = SEVERITY_BANDS.index(band)
    return SEVERITY_BANDS[min(idx + steps, len(SEVERITY_BANDS) - 1)]


def _max_band(a: str | None, b: str | None) -> str | None:
    bands = [x for x in (a, b) if x in SEVERITY_BANDS]
    if not bands:
        return None
    return max(bands, key=SEVERITY_BANDS.index)


# === Outcome-class derivation (spec §5.1) ===

def _parse_at(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _report_hashes_around_last_contradiction(
    transitions: list[dict[str, Any]],
) -> tuple[str | None, str | None]:
    """``(contradicted_hash, verified_hash)`` for the final retry chain.

    The contradicted hash is the one on the reported transition immediately
    preceding the *last* contradicted transition; the verified hash is the one
    on the last reported transition overall (the claim the final verdict
    graded). Either can be None on records written before hashes existed.
    """
    last_contra_idx = None
    for i, entry in enumerate(transitions):
        if entry.get("state") == STATE_CONTRADICTED:
            last_contra_idx = i
    if last_contra_idx is None:
        return None, None
    contradicted_hash = None
    for entry in reversed(transitions[:last_contra_idx]):
        if entry.get("state") == STATE_REPORTED:
            contradicted_hash = entry.get("report_sha256")
            break
    verified_hash = None
    for entry in reversed(transitions):
        if entry.get("state") == STATE_REPORTED:
            verified_hash = entry.get("report_sha256")
            break
    return contradicted_hash, verified_hash


def derive_outcome_class(
    record: DispatchRecord, verifier_checks_run: int | None = None,
) -> str | None:
    """The §5.1 class for a dispatch, or None while it is still in flight.

    Classes describe finished lifecycles, so nothing is classified before the
    terminate transition — a contradicted dispatch awaiting its retry is not
    yet a ``contradicted`` outcome, it is work in progress.

    ``verifier_checks_run`` is what separates ``unverifiable`` from
    ``ungraded`` when no verdict exists: a checker that ran and selected zero
    checks recorded that fact (checks_run == 0 → unverifiable, it graded
    nothing on purpose); no verifier record at all means grading never
    happened (ungraded — the class where the gate's own failure modes land).
    """
    if not record.is_terminal:
        return None

    transitions = record.transitions
    states = [t.get("state") for t in transitions]
    reported = STATE_REPORTED in states
    had_contradiction = STATE_CONTRADICTED in states
    final_verdict = record.verdict

    if final_verdict == STATE_VERIFIED:
        if not had_contradiction:
            return CLASS_VERIFIED
        contradicted_hash, verified_hash = _report_hashes_around_last_contradiction(transitions)
        if contradicted_hash is None or verified_hash is None:
            # Records from before per-attempt hashes existed cannot evidence a
            # changed work product. Claiming near_miss without evidence would
            # inflate the one metric with the strongest incentive to inflate,
            # so the un-evidenced case lands on the self-critical side.
            return CLASS_VERIFIER_FLAKE
        return CLASS_NEAR_MISS if contradicted_hash != verified_hash else CLASS_VERIFIER_FLAKE

    # The structured non-satisfiability exit: parked with the --unsatisfiable
    # marker. It outranks every branch below — an accepted escalation is the
    # outcome whatever verdict preceded the park — but never a verified
    # verdict (handled above): work that passed its checks was satisfied,
    # whatever the park said afterwards.
    if record.park_unsatisfiable:
        return CLASS_ESCALATED

    if final_verdict == STATE_CONTRADICTED:
        # The abandonment terminate reason splits a wedged agent (three
        # contradicted stops, gate gave up) from a single wrong claim. A
        # plain *parked* contradicted dispatch stays contradicted: parking
        # closes the bookkeeping, it does not soften the verdict. The one
        # deliberate exit is the --unsatisfiable marker, handled above.
        if record.terminate_reason == REASON_ABANDONED:
            return CLASS_ABANDONED
        return CLASS_CONTRADICTED

    if final_verdict == STATE_ADVISORY:
        return CLASS_ADVISORY

    # No verdict ever recorded.
    if reported:
        if verifier_checks_run == 0:
            return CLASS_UNVERIFIABLE
        return CLASS_UNGRADED

    # Terminated straight from dispatched: the reason is the only derivable
    # split. A parked reason is an operator's deliberate close, so it lands
    # beside operator-close rather than in the unclassified bucket.
    reason = record.terminate_reason
    if reason == REASON_SWEEP_IDLE:
        return CLASS_SILENT_IDLE
    if reason in (REASON_OPERATOR_CLOSE, REASON_SESSION_END) or is_parked_reason(reason):
        return CLASS_TERMINATED_UNREPORTED
    return CLASS_TERMINATED_UNCLASSIFIED


# === Building the record ===

def telemetry_path(record: DispatchRecord) -> Path:
    return record.run_dir / TELEMETRY_FILENAME


def load_telemetry(run_id: str) -> dict[str, Any] | None:
    """The stored telemetry record, or None when absent/unreadable."""
    record = load_dispatch(run_id)
    if record is None:
        return None
    try:
        data = json.loads(telemetry_path(record).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _elapsed_seconds(transitions: list[dict[str, Any]]) -> float | None:
    """Elapsed (not "autonomy") seconds from dispatch to the last transition.

    Elapsed on purpose: this window includes gate-waits and operator-away time,
    and pretending otherwise would misname the quantity.
    """
    stamps = [_parse_at(t.get("at")) for t in transitions]
    stamps = [s for s in stamps if s is not None]
    if len(stamps) < 2:
        return None
    return max(0.0, (max(stamps) - min(stamps)).total_seconds())


def _retry_chain_elapsed_seconds(transitions: list[dict[str, Any]]) -> float:
    """Seconds from the first contradiction to the last verdict — measured rework."""
    first_contra = None
    last_verdict = None
    for entry in transitions:
        at = _parse_at(entry.get("at"))
        if at is None:
            continue
        state = entry.get("state")
        if state == STATE_CONTRADICTED and first_contra is None:
            first_contra = at
        if state in (STATE_CONTRADICTED, STATE_VERIFIED, STATE_ADVISORY):
            last_verdict = at
    if first_contra is None or last_verdict is None:
        return 0.0
    return max(0.0, (last_verdict - first_contra).total_seconds())


def _claimed_summary(report: dict[str, Any] | None) -> dict[str, Any] | None:
    """The claim half of the claim-vs-verdict diff, reduced to numbers."""
    if not isinstance(report, dict):
        return None
    deliverables = report.get("deliverables")
    if not isinstance(deliverables, list):
        deliverables = []
    confidences = [
        float(d.get("confidence")) for d in deliverables
        if isinstance(d, dict) and isinstance(d.get("confidence"), (int, float))
        and not isinstance(d.get("confidence"), bool)
    ]
    evidence_counts = {"executed": 0, "observed": 0, "believed": 0}
    for d in deliverables:
        if isinstance(d, dict) and d.get("evidence") in evidence_counts:
            evidence_counts[d["evidence"]] += 1
    return {
        "deliverable_count": len(deliverables),
        "confidence_min": min(confidences) if confidences else None,
        "confidence_mean": (sum(confidences) / len(confidences)) if confidences else None,
        "evidence_counts": evidence_counts,
    }


def _coverage(
    record: DispatchRecord,
    report: dict[str, Any] | None,
    executed_check_ids: set[str] | None,
) -> float | None:
    """Deliverable coverage per §3.5: checker-executed checks over the union
    of manifest deliverables and claimed deliverables.

    The agent's self-declared evidence labels play no role — coverage counts
    only checks the checker actually executed, joined through the manifest's
    deliverable->check mapping. The zero-deliverable case is null, never 1.0:
    an empty denominator is an absence of measurement, not a perfect score.
    Null likewise when no checker execution is on record to compute against.
    """
    if executed_check_ids is None:
        return None
    manifest = record.manifest or {}
    deliverables: set[str] = set()
    for item in manifest.get("deliverables") or []:
        if isinstance(item, str) and item.strip():
            deliverables.add(item)
    if isinstance(report, dict):
        for item in report.get("deliverables") or []:
            if isinstance(item, dict) and isinstance(item.get("name"), str) and item["name"].strip():
                deliverables.add(item["name"])
    if not deliverables:
        return None
    check_map = manifest.get("check_map")
    if not isinstance(check_map, dict) or not check_map:
        # Deliverables with no deliverable->check mapping: no check was ever
        # joined to any of them, so nothing was measured — the docstring's
        # own principle applies, and the answer is null, not a zero score.
        return None
    covered = sum(
        1 for d in deliverables
        if any(cid in executed_check_ids for cid in check_map.get(d, []) if isinstance(cid, str))
    )
    return covered / len(deliverables)


def _verifier_block(check_report: Any, checks: list[Any] | None) -> dict[str, Any]:
    """Verdict provenance + stringency: who graded, how many checks, of what kinds.

    Check *ids* are deliberately not stored here — they are operator-authored
    and path-like, and this block is the part of the record the export carries.
    Check *commands* (the per-check ``cmd`` persisted in ``output.json`` as of
    0.6.0) are excluded for the same reason, more so: an argv is exactly the
    kind of operator-authored, secret-bearing string an export must never
    carry.
    """
    kind_counts: dict[str, int] = {}
    for check in checks or []:
        kind = check.expect.get("kind") if isinstance(getattr(check, "expect", None), dict) else None
        if kind:
            kind_counts[kind] = kind_counts.get(kind, 0) + 1
    return {
        "verifier": f"fleetproof/{__version__}",
        "checks_run": len(getattr(check_report, "results", []) or []),
        "check_kind_counts": kind_counts,
    }


def _machine_oversight_events(transitions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Gate blocks as oversight events. Machine-captured, so source is ``hook``.

    Each contradiction is an intervention that took effect (the stop was
    refused), which is what ``accepted`` records. Operator-added events carry
    ``source: operator`` and are preserved across rebuilds, so override rates
    stay weightable by provenance.
    """
    return [
        {"type": "intervention", "accepted": True, "at": t.get("at"), "source": "hook"}
        for t in transitions if t.get("state") == STATE_CONTRADICTED
    ]


def _incident_id(run_id: str) -> str:
    """Stable local incident id. Never exported as-is — re-keyed per recipient."""
    return "inc-" + hashlib.sha256(f"fleetproof-incident:{run_id}".encode("utf-8")).hexdigest()[:16]


# Operator-supplied and verdict-time fields that a rebuild must carry forward:
# a later close must not erase what the operator answered or what the checker
# recorded when it actually ran.
_PRESERVED_KEYS = (
    "outcome.verifier",
    "outcome.coverage",
    "failure.raw_loss",
    "failure.harm_type",
    "failure.mode",
    "failure.root_cause",
    "failure.remediation",
    "failure.prompt_latency_seconds",
    "failure.default_accepted",
    "exposure.value_at_stake",
)


def build_telemetry(
    run_id: str,
    check_report: Any = None,
    checks: list[Any] | None = None,
    report_content: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Build or update ``telemetry.json`` for a telemetry-era dispatch.

    Called from the checker's side at verdict and close time. Returns the
    record, or None for a pre-telemetry dispatch — those are never back-filled.

    ``check_report``/``checks`` carry the grading that just happened (including
    a deliberately empty grading: a report with zero selected checks records
    ``checks_run: 0``, which is what lets ``unverifiable`` be told apart from
    ``ungraded`` later). Omitting them preserves whatever verifier block an
    earlier build recorded. ``report_content`` overrides reading report.json
    off disk (tests).
    """
    record = load_dispatch(run_id)
    if record is None:
        raise TelemetryError(f"No dispatch record for run {run_id!r}.")
    if record.telemetry_era is None:
        return None

    existing = load_telemetry(run_id) or {}
    config = load_config()
    report = report_content if report_content is not None else record.load_report()

    agent = record.agent if isinstance(record.agent, dict) else {}
    capture = agent.get("capture")
    stop_only = capture == CAPTURE_STOP_ONLY

    # Verifier: freshly graded wins; otherwise what an earlier build recorded.
    if check_report is not None:
        verifier: dict[str, Any] | None = _verifier_block(check_report, checks)
        executed_ids: set[str] | None = {
            r.id for r in (getattr(check_report, "results", []) or [])
        }
        coverage = _coverage(record, report, executed_ids)
    else:
        verifier = existing.get("outcome.verifier") if isinstance(
            existing.get("outcome.verifier"), dict) else None
        coverage = existing.get("outcome.coverage")
        if not isinstance(coverage, (int, float)):
            coverage = None

    checks_run = verifier.get("checks_run") if verifier else None
    outcome_class = derive_outcome_class(record, verifier_checks_run=checks_run)

    retry_count = max(0, sum(1 for t in record.transitions
                             if t.get("state") == STATE_REPORTED) - 1)

    telemetry: dict[str, Any] = {
        # --- identity & versioning ---
        "schema_version": SCHEMA_VERSION,
        "fleetproof.derivation_version": DERIVATION_VERSION,
        "semconv_version": SEMCONV_VERSION,
        "fleetproof.taxonomy_version": TAXONOMY_VERSION,
        "task.classifier_version": TASK_CLASSIFIER_VERSION,
        "run_id": record.run_id,
        "session_id": record.session_id,
        "tier": record.tier,
        "deployment_id": deployment_id(config),
        "operator_id": operator_id(config),
        "fleetproof.run_context": run_context(config),
        "fleetproof.telemetry_era": record.telemetry_era,
        "started_at": record.started_at,
        # --- stack ---
        "gen_ai.provider.name": None,
        "gen_ai.request.model": None,
        "fleetproof.harness": "claude-code" if record.agent is not None else None,
        "fleetproof.version": __version__,
        "agent_type": agent.get("agent_type"),
        "agent.capture": capture,
        "spec_sha256_pinned": record.spec_sha256_pinned,
        "eval_suite_id": eval_suite_id(config),
        # --- task classification ---
        "task.domain": classify_task_domain(record.prompt),
        "task.action_class": classify_action_class(record.prompt),
        # Neither is observable from the hook payloads today; a guessed value
        # would be worse than an honest null / unknown, and unknown is the
        # spec-mandated irreversibility default.
        "autonomy_level": None,
        "action_irreversibility": "unknown",
        # --- exposure (all null on stop-only: no observation window) ---
        "exposure.tool_call_count": None,
        "exposure.tokens_in": None,
        "exposure.tokens_out": None,
        "exposure.elapsed_seconds": None if stop_only else _elapsed_seconds(record.transitions),
        "exposure.files_touched": None,
        "exposure.retry_count": None if stop_only else retry_count,
        "exposure.value_at_stake": existing.get("exposure.value_at_stake"),
        # --- outcome ---
        "outcome.claimed": _claimed_summary(report),
        "outcome.class": outcome_class,
        "outcome.verifier": verifier,
        "outcome.coverage": coverage,
        # --- compliance ---
        "compliance.retention_days": None,
        "compliance.capture_mode": CAPTURE_MODE,
    }

    # Failure detail exists only where there is (or was) a failure on record.
    # Abandoned counts: three contradicted claims that never converged is a
    # failure with a measured rework floor, not a neutral termination.
    is_failure = outcome_class in (CLASS_CONTRADICTED, CLASS_NEAR_MISS, CLASS_ABANDONED)
    raw_loss = existing.get("failure.raw_loss")
    telemetry.update(_failure_block(record, outcome_class, is_failure, raw_loss, existing))

    telemetry["oversight.events"] = _machine_oversight_events(record.transitions) + [
        e for e in (existing.get("oversight.events") or [])
        if isinstance(e, dict) and e.get("source") == "operator"
    ]

    _write_telemetry(record, telemetry)
    return telemetry


def _failure_block(
    record: DispatchRecord,
    outcome_class: str | None,
    is_failure: bool,
    raw_loss: Any,
    existing: dict[str, Any],
) -> dict[str, Any]:
    block: dict[str, Any] = {
        "failure.mode": existing.get("failure.mode"),
        "failure.harm_type": existing.get("failure.harm_type"),
        "failure.raw_loss": raw_loss if isinstance(raw_loss, dict) else None,
        "failure.severity_band": None,
        "failure.severity_floor": None,
        "failure.labor_rate_assumed": None,
        "failure.estimated_loss": None,
        "failure.root_cause": existing.get("failure.root_cause"),
        "failure.remediation": existing.get("failure.remediation"),
        "failure.incident_id": None,
        "failure.prompt_latency_seconds": existing.get("failure.prompt_latency_seconds"),
        "failure.default_accepted": existing.get("failure.default_accepted"),
    }
    if not is_failure:
        return block

    block["failure.incident_id"] = _incident_id(record.run_id)
    block["failure.labor_rate_assumed"] = LABOR_RATE_USD_PER_HOUR

    # Machine floor from observables: the retry chain's elapsed time is a
    # *measured* lower bound on rework, priced at the published labor rate.
    # Irreversible actions escalate the floor one band — the observables can
    # justify raising a lower bound, never lowering one. A near miss floors at
    # S0 by the band table's own definition ("caught pre-acceptance"): its
    # retry chain happened *inside* the gate, before anything was accepted, so
    # pricing that time as delivered loss would contradict the S0 row.
    if outcome_class == CLASS_NEAR_MISS:
        floor = "S0"
    else:
        floor_usd = (_retry_chain_elapsed_seconds(record.transitions) / 3600.0) \
            * LABOR_RATE_USD_PER_HOUR
        floor = severity_band_for_usd(floor_usd)
    if _irreversibility(existing) == "irreversible":
        floor = _escalate_band(floor)
    block["failure.severity_floor"] = floor

    band: str | None = None
    if isinstance(block["failure.raw_loss"], dict):
        try:
            usd = loss_to_usd(block["failure.raw_loss"].get("value"),
                              block["failure.raw_loss"].get("unit"))
        except (TelemetryError, TypeError, ValueError):
            usd = None
        if usd is not None:
            band = severity_band_for_usd(usd)
            method = ("operator-hours@$%d/hr" % LABOR_RATE_USD_PER_HOUR
                      if block["failure.raw_loss"].get("unit") == LOSS_UNIT_HOURS
                      else "operator-usd")
            block["failure.estimated_loss"] = {
                "amount": round(usd, 2), "currency": "USD", "method": method,
            }
    elif outcome_class == CLASS_NEAR_MISS:
        # Caught pre-acceptance with no operator-reported loss: S0 by the band
        # table's own definition, not a guess.
        band = "S0"

    # The operator's answer may raise the severity; it can never undercut the
    # machine floor. No answer yet (a contradicted run awaiting its one
    # question) leaves the band null — never guessed — while the floor stands.
    if band is not None:
        block["failure.severity_band"] = _max_band(band, block["failure.severity_floor"])
    return block


def _irreversibility(existing: dict[str, Any]) -> str:
    value = existing.get("action_irreversibility")
    return value if isinstance(value, str) else "unknown"


def _write_telemetry(record: DispatchRecord, telemetry: dict[str, Any]) -> None:
    telemetry_path(record).write_text(
        json.dumps(telemetry, indent=2) + "\n", encoding="utf-8")
    _append_chain(record.run_id, telemetry)


# === Append-time integrity chain ===
#
# Every telemetry write appends a link to a rolling hash chain, so the corpus
# is chained at *append* time — an export-time chain would only prove the
# export file itself was not altered after generation, saying nothing about
# the months the records sat as editable JSON. The residual is stated plainly
# where it matters (the methodology note): the chain lives in the same
# writable tree as the records until its head is anchored outside it, which
# is what `fleetproof telemetry anchor` exists for.

CHAIN_FILENAME = "telemetry-chain.jsonl"
_CHAIN_GENESIS = "0" * 64


def chain_path() -> Path:
    return runs_dir().parent / CHAIN_FILENAME


def _content_hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def chain_head() -> tuple[str, int]:
    """``(head_hash, entry_count)`` of the chain; genesis when empty/unreadable."""
    head = _CHAIN_GENESIS
    count = 0
    try:
        lines = chain_path().read_text(encoding="utf-8").splitlines()
    except OSError:
        return head, 0
    for line in lines:
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict) and isinstance(entry.get("head"), str):
            head = entry["head"]
            count += 1
    return head, count


def _append_chain(run_id: str, telemetry: dict[str, Any]) -> None:
    """Best-effort chain append. A chain failure must not fail the write it
    describes — the anchor command and export surface a short chain loudly."""
    try:
        prev, _ = chain_head()
        content = _content_hash(telemetry)
        head = hashlib.sha256(f"{prev}:{content}".encode("utf-8")).hexdigest()
        entry = {"run_id": run_id, "telemetry_sha256": content,
                 "prev": prev, "head": head}
        with chain_path().open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except OSError:
        pass


# === The one operator prompt per failure: a raw quantity ===

def record_failure_loss(
    run_id: str,
    value: float,
    unit: str,
    *,
    prompt_latency_seconds: float | None = None,
    default_accepted: bool | None = None,
) -> dict[str, Any]:
    """Record the operator's raw loss answer for a failed run and re-derive.

    The prompt asks for one raw quantity — hours or dollars — and the band is
    *derived*, monotone in dollars at the published labor rate. Raw stays
    priceable across every future band recalibration; a band alone would be
    orphaned by the next edge change. The answer can raise the severity above
    the machine floor, never lower it below.

    ``prompt_latency_seconds`` and ``default_accepted`` are honesty telemetry
    on the honesty telemetry: how long the answer took, and whether a suggested
    default was accepted unchanged.
    """
    if unit not in VALID_LOSS_UNITS:
        raise TelemetryError(
            f"Loss unit must be one of {sorted(VALID_LOSS_UNITS)}; got {unit!r}.")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise TelemetryError(f"Loss value must be a non-negative number; got {value!r}.")

    existing = load_telemetry(run_id)
    if existing is None:
        raise TelemetryError(
            f"No telemetry record for run {run_id!r}; a loss can only be "
            "recorded against a built telemetry record.")
    existing["failure.raw_loss"] = {"value": value, "unit": unit}
    if prompt_latency_seconds is not None:
        existing["failure.prompt_latency_seconds"] = float(prompt_latency_seconds)
    if default_accepted is not None:
        existing["failure.default_accepted"] = bool(default_accepted)

    record = load_dispatch(run_id)
    if record is None:
        raise TelemetryError(f"No dispatch record for run {run_id!r}.")
    _write_telemetry(record, existing)
    # Rebuild so the band, floor, and estimated loss re-derive from the raw
    # answer under the current rules.
    rebuilt = build_telemetry(run_id)
    if rebuilt is None:  # pragma: no cover - era-stamped by construction here
        raise TelemetryError(f"Run {run_id!r} is pre-telemetry; nothing to record against.")
    return rebuilt
