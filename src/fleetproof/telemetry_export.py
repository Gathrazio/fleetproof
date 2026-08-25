"""Telemetry aggregation and the export pipeline — the only path off the machine.

Two derived artifacts, computed and never hand-written:

*Summary* (``fleetproof telemetry summary``): the per-deployment outcome and
severity distributions, integrity rates, and trailing windows. **Local-only**:
it reads the raw records with clear-text names and exact times, so nothing it
prints is shareable — any shareable form goes through the export below.

*Export* (``fleetproof telemetry export``): a time-bounded extract built by
**allowlist, not blocklist**. A field appears in an export because a rule here
knows it, validates it against a closed vocabulary or numeric shape, and emits
it — everything else (free text, operator-chosen names, prompts, paths, exact
timestamps) does not exist on the way out. New telemetry fields therefore
default to non-exported until a rule is added, which is the failure direction
that leaks nothing.

Every published rate is a closed formula over the full outcome-class census —
numerator and denominator stated explicitly, no class ever silently dropped.
Which classes sit where changes headline numbers by integer factors, so the
formulas live in one place (``_metrics``) keyed to the schema spec, not
scattered through presentation code.

Class census note: outcome classes are re-derived live from the dispatch
records under the current derivation version, rather than trusted from stored
telemetry files — one rule-set over the whole corpus, and a stale or tampered
telemetry.json cannot re-class a run. The stored file still matters: an
era-stamped, finished run *without* a readable telemetry.json is counted as
``telemetry_missing``, an integrity defect published in every summary — never
quietly reclassified into a benign bucket, because file absence is exactly
what an agent can manufacture.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .checks import VALID_TIERS
from .config import VALID_RUN_CONTEXTS
from .ledger import (
    CAPTURE_STOP_ONLY,
    DispatchRecord,
    list_dispatches,
)
from .runlog import runs_dir
from .telemetry import (
    CLASS_ABANDONED,
    CLASS_CONTRADICTED,
    CLASS_NEAR_MISS,
    CLASS_SILENT_IDLE,
    CLASS_TERMINATED_UNREPORTED,
    CLASS_UNGRADED,
    CLASS_UNVERIFIABLE,
    CLASS_VERIFIED,
    CLASS_VERIFIER_FLAKE,
    DERIVATION_VERSION,
    OUTCOME_CLASSES,
    SCHEMA_VERSION,
    SEVERITY_BANDS,
    TASK_CLASSIFIER_VERSION,
    TelemetryError,
    chain_head,
    derive_outcome_class,
    load_telemetry,
)

# Census buckets beyond the nine outcome classes. None is reclassifiable into
# another: pre-telemetry is never back-filled, telemetry_missing is never
# excused into pre-telemetry, in-flight work is not yet an outcome.
BUCKET_PRE_TELEMETRY = "pre_telemetry"
BUCKET_TELEMETRY_MISSING = "telemetry_missing"
BUCKET_IN_FLIGHT = "in_flight"

CENSUS_BUCKETS = OUTCOME_CLASSES + (
    BUCKET_PRE_TELEMETRY, BUCKET_TELEMETRY_MISSING, BUCKET_IN_FLIGHT,
)

TRAILING_WINDOW_DAYS = (30, 90, 365)

SALTS_FILENAME = "telemetry-salts.json"
ANCHOR_FILENAME = "telemetry-anchor.json"

# The one sentence every recipient must read before any rate (spec §6.3).
VERIFIED_MEANING = (
    "verified = conformance to operator-authored checks at the recorded "
    "coverage, NOT work quality"
)

# Closed vocabularies the export validates against. A value outside its
# vocabulary exports as null — enum fields are allowlisted by *value*, not
# just by name, so a tampered record cannot smuggle text through them.
_TASK_DOMAINS = frozenset({"code", "research", "document", "data", "ops",
                           "comms", "other"})
_ACTION_CLASSES = frozenset({"create", "modify", "analyze", "execute"})
_AUTONOMY_LEVELS = frozenset({"recommend", "execute_with_approval", "autonomous"})
_IRREVERSIBILITY = frozenset({"reversible", "compensable", "irreversible", "unknown"})
_CAPTURES = frozenset({"start", "stop-only"})
_HARNESSES = frozenset({"claude-code"})
# Until a harm taxonomy is adopted into code there is nothing to validate a
# harm value against, so only the no-harm marker exports.
_HARM_TYPES = frozenset({"none"})
_CHECK_KINDS = frozenset({"exit0", "exit", "regex", "file_exists"})
_OVERSIGHT_TYPES = frozenset({"intervention", "override", "correction"})
_OVERSIGHT_SOURCES = frozenset({"hook", "operator"})

_VERSION_RE = re.compile(r"^\d+(\.\d+)*([a-z0-9.+-]*)$")

# Exported durations are bands, never seconds (spec §6.2): fine-grained times
# are the strongest deanonymizer a solo operator has.
_DURATION_BANDS = (
    (60, "lt-1m"), (600, "1m-10m"), (3600, "10m-1h"), (28800, "1h-8h"),
)
DURATION_BAND_OVER = "gte-8h"


# === Census ===

def _record_date(record: DispatchRecord) -> date | None:
    if not record.started_at:
        return None
    try:
        return datetime.fromisoformat(record.started_at).date()
    except ValueError:
        return None


def _bucket(record: DispatchRecord, telemetry: dict[str, Any] | None) -> str:
    if record.telemetry_era is None:
        return BUCKET_PRE_TELEMETRY
    checks_run = None
    if telemetry is not None:
        verifier = telemetry.get("outcome.verifier")
        if isinstance(verifier, dict) and isinstance(verifier.get("checks_run"), int):
            checks_run = verifier["checks_run"]
    outcome = derive_outcome_class(record, verifier_checks_run=checks_run)
    if outcome is None:
        return BUCKET_IN_FLIGHT
    if telemetry is None:
        return BUCKET_TELEMETRY_MISSING
    return outcome


def census(
    since: date | None = None, until: date | None = None,
) -> list[dict[str, Any]]:
    """One census row per dispatch: date, bucket, capture mode, telemetry.

    Undated records (a damaged _root.json) are excluded by any date bound but
    included in unbounded scans — a run must not vanish from the full census
    because its clock field broke.
    """
    rows: list[dict[str, Any]] = []
    for record in list_dispatches():
        day = _record_date(record)
        if since is not None and (day is None or day < since):
            continue
        if until is not None and (day is None or day > until):
            continue
        telemetry = load_telemetry(record.run_id)
        agent = record.agent if isinstance(record.agent, dict) else {}
        rows.append({
            "run_id": record.run_id,
            "date": day,
            "bucket": _bucket(record, telemetry),
            "era": record.telemetry_era is not None,
            "stop_only": agent.get("capture") == CAPTURE_STOP_ONLY,
            "spec_sha256_pinned": record.spec_sha256_pinned,
            "telemetry": telemetry,
            "record": record,
        })
    return rows


# === §5.3 metrics — the only rates that may be published ===

def _rate(numerator: int, denominator: int) -> dict[str, Any]:
    return {
        "numerator": numerator,
        "denominator": denominator,
        "rate": (numerator / denominator) if denominator else None,
    }


def _frequency(numerator: int, denominator: int) -> dict[str, Any]:
    """A per-dispatch frequency, co-reported per the mandatory co-denominators.

    Token and tool-call totals are unobserved today (every record carries them
    as null), so the co-denominated forms are null with the reason stated —
    never a number computed over a denominator that was guessed.
    """
    out = _rate(numerator, denominator)
    out["per_1k_dispatches"] = (
        (numerator / denominator) * 1000 if denominator else None)
    out["per_1M_tokens"] = None
    out["per_1k_tool_calls"] = None
    out["co_denominator_note"] = (
        "token/tool-call totals unobserved (null) in this corpus; "
        "co-denominated forms unavailable, not zero")
    return out


def _metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    counts = {bucket: 0 for bucket in CENSUS_BUCKETS}
    for row in rows:
        counts[row["bucket"]] += 1

    # D: telemetry-era dispatches with a derived outcome. telemetry_missing
    # rows have an outcome too, but their record is the integrity defect —
    # they are counted in their own first-class rate, never mixed into D.
    d = sum(counts[c] for c in OUTCOME_CLASSES)
    era_total = sum(1 for row in rows if row["era"])
    # Abandoned dispatches were graded — three times, wrongly each time — so
    # they sit in graded claims and in the false-claim numerator alongside
    # contradicted, while keeping their own first-class rate below.
    graded_claims = (counts[CLASS_VERIFIED] + counts[CLASS_CONTRADICTED]
                     + counts[CLASS_NEAR_MISS] + counts[CLASS_VERIFIER_FLAKE]
                     + counts[CLASS_ABANDONED])

    events_by_source = {"hook": 0, "operator": 0}
    overrides_by_source = {"hook": 0, "operator": 0}
    for row in rows:
        telemetry = row["telemetry"]
        if not isinstance(telemetry, dict):
            continue
        for event in telemetry.get("oversight.events") or []:
            if not isinstance(event, dict):
                continue
            source = event.get("source")
            if source not in events_by_source:
                continue
            events_by_source[source] += 1
            if event.get("type") == "override":
                overrides_by_source[source] += 1

    return {
        "counts": counts,
        "classifiable_dispatches": d,
        "telemetry_era_dispatches": era_total,
        "total_dispatches": len(rows),
        # Buyer's-seat failure: work ordered, not acceptably delivered.
        # silent_idle and terminated_unreported ARE failures even though no
        # check ever fired on them.
        "delivery_failure_rate": _frequency(
            counts[CLASS_CONTRADICTED] + counts[CLASS_ABANDONED]
            + counts[CLASS_SILENT_IDLE]
            + counts[CLASS_TERMINATED_UNREPORTED], d),
        # Of graded claims, how many were wrong.
        "false_claim_rate": _rate(
            counts[CLASS_CONTRADICTED] + counts[CLASS_ABANDONED], graded_claims),
        # Published side by side: near-miss context without flake context is
        # how verifier noise gets sold as caught failures.
        "near_miss_rate": _frequency(counts[CLASS_NEAR_MISS], d),
        "verifier_flake_rate": _frequency(counts[CLASS_VERIFIER_FLAKE], d),
        # An abandoned dispatch is a wedge the ladder terminated: the rate an
        # operator tunes specs and tiers against, so it publishes first-class.
        "abandoned_rate": _frequency(counts[CLASS_ABANDONED], d),
        # Integrity rates: the corpus's own health metrics, always published.
        "ungraded_rate": _frequency(counts[CLASS_UNGRADED], d),
        "telemetry_missing_rate": _rate(counts[BUCKET_TELEMETRY_MISSING], era_total),
        "stop_only_fraction": _rate(
            sum(1 for row in rows if row["era"] and row["stop_only"]), era_total),
        # Never folded into success.
        "unverifiable_rate": _frequency(counts[CLASS_UNVERIFIABLE], d),
        "override_rate_by_source": {
            source: _rate(overrides_by_source[source], events_by_source[source])
            for source in ("hook", "operator")
        },
    }


def _severity_distribution(rows: list[dict[str, Any]]) -> dict[str, int]:
    """Band counts over failure-classed runs; no-answer-yet counted, not hidden."""
    dist = {band: 0 for band in SEVERITY_BANDS}
    dist["unanswered"] = 0
    for row in rows:
        if row["bucket"] not in (CLASS_CONTRADICTED, CLASS_NEAR_MISS, CLASS_ABANDONED):
            continue
        telemetry = row["telemetry"] if isinstance(row["telemetry"], dict) else {}
        band = telemetry.get("failure.severity_band")
        if band in dist:
            dist[band] += 1
        else:
            dist["unanswered"] += 1
    return dist


def _spec_hash_timeline(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """First-seen dates of each pinned spec hash, chronological.

    Check-regime churn is auditable because the pin exists; this makes it
    *visible*, so a near-miss burst that coincides with a spec change reads as
    what it is.
    """
    first_seen: dict[str, date] = {}
    for row in sorted(rows, key=lambda r: (r["date"] or date.max)):
        sha = row["spec_sha256_pinned"]
        if isinstance(sha, str) and sha and sha not in first_seen and row["date"]:
            first_seen[sha] = row["date"]
    return [
        {"spec_sha256": sha, "first_seen": str(day)}
        for sha, day in sorted(first_seen.items(), key=lambda item: item[1])
    ]


def summarize() -> dict[str, Any]:
    """The full local-only summary: full-corpus block plus trailing windows."""
    rows = census()
    today = datetime.now(timezone.utc).date()
    windows: dict[str, Any] = {"full": _window_block(rows)}
    for days in TRAILING_WINDOW_DAYS:
        cutoff = today - timedelta(days=days)
        in_window = [r for r in rows if r["date"] is not None and r["date"] >= cutoff]
        windows[f"trailing_{days}d"] = _window_block(in_window)
    return {
        "label": "local-only",
        "schema_version": SCHEMA_VERSION,
        "derivation_version": DERIVATION_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "windows": windows,
        "spec_hash_timeline": _spec_hash_timeline(rows),
    }


def _window_block(rows: list[dict[str, Any]]) -> dict[str, Any]:
    block = _metrics(rows)
    block["severity_distribution"] = _severity_distribution(rows)
    return block


# === Per-counterparty salts ===

def _salts_path() -> Path:
    return runs_dir().parent / SALTS_FILENAME


def salt_for_recipient(label: str) -> str:
    """The stable salt for one export counterparty, minted on first use.

    Per-recipient on purpose: two recipients of the same corpus must not be
    able to join their copies on shared hashes. The salt file never leaves
    the machine (nothing outside the allowlist does).
    """
    if not label or not label.strip():
        raise TelemetryError("An export needs a recipient label for its salt.")
    label = label.strip()
    path = _salts_path()
    try:
        salts = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(salts, dict):
            salts = {}
    except (OSError, json.JSONDecodeError):
        salts = {}
    if label not in salts:
        salts[label] = secrets.token_hex(32)
        path.write_text(json.dumps(salts, indent=2), encoding="utf-8")
    return salts[label]


def _salted(value: Any, salt: str) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return hashlib.sha256(f"{salt}:{value}".encode("utf-8")).hexdigest()


# === Allowlist guards ===

def _enum(value: Any, valid: frozenset) -> str | None:
    return value if isinstance(value, str) and value in valid else None


def _count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _fraction(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if 0.0 <= float(value) <= 1.0 else None


def _duration_band(seconds: Any) -> str | None:
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or seconds < 0:
        return None
    for limit, band in _DURATION_BANDS:
        if seconds < limit:
            return band
    return DURATION_BAND_OVER


def _version_str(value: Any) -> str | None:
    return value if isinstance(value, str) and _VERSION_RE.match(value) else None


def _known_constant(value: Any, constant: Any) -> Any:
    """A pinned-vocabulary field exports only values this build knows.

    Unknown values (a future version, a tampered string) drop to null — the
    §6.1 default of non-exported-until-allowlisted applied to field *values*.
    """
    return value if value == constant else None


def _verifier_export(verifier: Any) -> dict[str, Any] | None:
    if not isinstance(verifier, dict):
        return None
    raw = verifier.get("verifier")
    name: str | None = None
    if isinstance(raw, str) and raw.startswith("fleetproof/") \
            and _version_str(raw.split("/", 1)[1]):
        name = raw
    kind_counts_raw = verifier.get("check_kind_counts")
    kind_counts: dict[str, int] = {}
    if isinstance(kind_counts_raw, dict):
        for kind, n in kind_counts_raw.items():
            n = _count(n)
            if n is None:
                continue
            key = kind if kind in _CHECK_KINDS else "other"
            kind_counts[key] = kind_counts.get(key, 0) + n
    return {
        "verifier": name,
        "checks_run": _count(verifier.get("checks_run")),
        "check_kind_counts": kind_counts,
    }


def _claimed_export(claimed: Any) -> dict[str, Any] | None:
    if not isinstance(claimed, dict):
        return None
    evidence_raw = claimed.get("evidence_counts")
    evidence = {}
    for key in ("executed", "observed", "believed"):
        evidence[key] = _count(evidence_raw.get(key)) if isinstance(evidence_raw, dict) else None
    return {
        "deliverable_count": _count(claimed.get("deliverable_count")),
        "confidence_min": _fraction(claimed.get("confidence_min")),
        "confidence_mean": _fraction(claimed.get("confidence_mean")),
        "evidence_counts": evidence,
    }


def _oversight_export(events: Any) -> dict[str, Any]:
    counts: dict[str, Any] = {
        "events_total": 0,
        "by_source": {"hook": 0, "operator": 0},
        "overrides_by_source": {"hook": 0, "operator": 0},
    }
    for event in events if isinstance(events, list) else []:
        if not isinstance(event, dict):
            continue
        source = _enum(event.get("source"), _OVERSIGHT_SOURCES)
        etype = _enum(event.get("type"), _OVERSIGHT_TYPES)
        if source is None or etype is None:
            continue
        counts["events_total"] += 1
        counts["by_source"][source] += 1
        if etype == "override":
            counts["overrides_by_source"][source] += 1
    return counts


def _date_str(value: Any) -> str | None:
    """A date-bucketed form of any timestamp-ish string; exact times never pass."""
    if not isinstance(value, str):
        return None
    try:
        return str(datetime.fromisoformat(value).date())
    except ValueError:
        pass
    try:
        return str(date.fromisoformat(value))
    except ValueError:
        return None


def _export_record(row: dict[str, Any], salt: str) -> dict[str, Any]:
    """One telemetry record through the allowlist. Nothing else survives."""
    t = row["telemetry"] or {}
    return {
        # Identity: date bucket + salted references only. The raw run id
        # embeds a to-the-second timestamp, so it never exports even hashed
        # without the salt in front of it.
        "record_date": str(row["date"]) if row["date"] else None,
        "run_ref": _salted(row["run_id"], salt),
        "session_ref": _salted(t.get("session_id"), salt),
        "tier": _enum(t.get("tier"), VALID_TIERS),
        "deployment_ref": _salted(t.get("deployment_id"), salt),
        "operator_ref": _salted(t.get("operator_id"), salt),
        "schema_version": _known_constant(t.get("schema_version"), SCHEMA_VERSION),
        "derivation_version": _count(t.get("fleetproof.derivation_version")),
        "semconv_version": _known_constant(t.get("semconv_version"), None),
        "taxonomy_version": _known_constant(t.get("fleetproof.taxonomy_version"), None),
        "task_classifier_version": _known_constant(
            t.get("task.classifier_version"), TASK_CLASSIFIER_VERSION),
        "run_context": _enum(t.get("fleetproof.run_context"), VALID_RUN_CONTEXTS),
        "telemetry_era": _date_str(t.get("fleetproof.telemetry_era")),
        # Stack: vendor fields export null until they are machine-captured;
        # a value with no captured provenance has no business in an export.
        "gen_ai.provider.name": None,
        "gen_ai.request.model": None,
        "harness": _enum(t.get("fleetproof.harness"), _HARNESSES),
        "fleetproof_version": _version_str(t.get("fleetproof.version")),
        "agent_type_ref": _salted(t.get("agent_type"), salt),
        "capture": _enum(t.get("agent.capture"), _CAPTURES),
        "spec_ref": _salted(t.get("spec_sha256_pinned"), salt),
        "eval_suite_ref": _salted(t.get("eval_suite_id"), salt),
        # Task classification: closed enums.
        "task.domain": _enum(t.get("task.domain"), _TASK_DOMAINS),
        "task.action_class": _enum(t.get("task.action_class"), _ACTION_CLASSES),
        "autonomy_level": _enum(t.get("autonomy_level"), _AUTONOMY_LEVELS),
        "action_irreversibility": _enum(t.get("action_irreversibility"), _IRREVERSIBILITY),
        # Exposure: counts and bands. Durations leave only as bands.
        "exposure.tool_call_count": _count(t.get("exposure.tool_call_count")),
        "exposure.tokens_in": _count(t.get("exposure.tokens_in")),
        "exposure.tokens_out": _count(t.get("exposure.tokens_out")),
        "exposure.elapsed_band": _duration_band(t.get("exposure.elapsed_seconds")),
        "exposure.files_touched": _count(t.get("exposure.files_touched")),
        "exposure.retry_count": _count(t.get("exposure.retry_count")),
        # Outcome. The class is the census's re-derivation from the dispatch
        # record under the current derivation version — one rule-set over the
        # whole corpus — not whatever string the stored file happens to hold.
        "outcome.class": _enum(row["bucket"], frozenset(OUTCOME_CLASSES)),
        "outcome.claimed": _claimed_export(t.get("outcome.claimed")),
        "outcome.verifier": _verifier_export(t.get("outcome.verifier")),
        "outcome.coverage": _fraction(t.get("outcome.coverage")),
        # Failure: bands, enums, and the re-keyed incident reference. Raw and
        # estimated loss amounts stay local — bands are the exported form.
        "failure.harm_type": _enum(t.get("failure.harm_type"), _HARM_TYPES),
        "failure.severity_band": _enum(t.get("failure.severity_band"),
                                       frozenset(SEVERITY_BANDS)),
        "failure.severity_floor": _enum(t.get("failure.severity_floor"),
                                        frozenset(SEVERITY_BANDS)),
        "failure.labor_rate_assumed": _count(t.get("failure.labor_rate_assumed")),
        "failure.incident_ref": _salted(t.get("failure.incident_id"), salt),
        "oversight.event_counts": _oversight_export(t.get("oversight.events")),
        "compliance.retention_days": _count(t.get("compliance.retention_days")),
    }


# === The export itself ===

def _completeness(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Runs-per-day-per-class, every bucket included, so gaps are visible."""
    counts: dict[tuple[str, str], int] = {}
    for row in rows:
        day = str(row["date"]) if row["date"] else "undated"
        key = (day, row["bucket"])
        counts[key] = counts.get(key, 0) + 1
    return [
        {"date": day, "class": bucket, "count": n}
        for (day, bucket), n in sorted(counts.items())
    ]


def _methodology_note(rows: list[dict[str, Any]], window_label: str) -> str:
    era_rows = [r for r in rows if r["era"]]
    operators = {
        (r["telemetry"] or {}).get("operator_id") for r in era_rows
        if isinstance(r["telemetry"], dict)
    }
    harnesses = {
        (r["telemetry"] or {}).get("fleetproof.harness") for r in era_rows
        if isinstance(r["telemetry"], dict)
    }
    providers = {
        (r["telemetry"] or {}).get("gen_ai.provider.name") for r in era_rows
        if isinstance(r["telemetry"], dict)
    }
    def _n(values: set) -> int:
        named = {v for v in values if v}
        # An unnamed operator/harness/provider is still one, not zero.
        return max(1, len(named)) if era_rows else 0
    return f"""# Methodology note

## Read this before any rate

- **Corpus shape:** {_n(operators)} operator(s), {_n(harnesses)} harness(es),
  {_n(providers)} model provider(s). A single-operator corpus is one correlated
  deployment — one prompting style, one harness, one provider mix — whatever
  its run count.
- **What "verified" means:** {VERIFIED_MEANING}. Checks are operator-authored
  and un-audited; plausible-but-wrong output that satisfies the declared
  checks enters this corpus as verified.
- **Survivorship:** dispatched work is an operator-selected sample. Work the
  operator chose to do inline generates no record here.

## Trust model

Records are **operator-attested**. Checker/agent separation is process
convention on a shared filesystem, not privilege separation. Records are
hash-chained at append time (verdict time) and the chain head can be anchored
outside the record tree; the export attests "unaltered since anchoring".
Pre-anchor tampering and pre-export deletion are not cryptographically
excluded — the completeness manifest in this export makes gaps visible, it
does not make them impossible.

## Identity and minimization

This export is **identified, minimized** — not anonymized: a single-party
export is identified by construction. Operator-chosen strings are salted
hashes under a per-recipient salt (two recipients cannot join corpora);
incident references are re-keyed per recipient; timestamps are coarsened to
date buckets and durations to bands; free text never exports.
Revocation means no future exports; prior disclosures are not recallable.

## Measurement caveats

- Coverage is claim-relative until manifests are routinely non-empty; the
  zero-deliverable case is null, never 1.0.
- Token and tool-call exposure are unobserved (null) in this corpus, so
  per-1M-token / per-1k-tool-call co-denominators are unavailable, not zero.
- Window: {window_label}. Schema {SCHEMA_VERSION}, derivation
  {DERIVATION_VERSION}, exporter fleetproof {__version__}.
"""


def export_telemetry(
    recipient: str,
    out_dir: Path | None = None,
    since: date | None = None,
    until: date | None = None,
) -> Path:
    """Write the export bundle for one counterparty; returns its directory.

    The bundle is two files: ``telemetry-export.json`` (records through the
    allowlist, completeness manifest, integrity head) and ``methodology.md``.
    Records are telemetry-era runs with a readable telemetry file; every other
    bucket (pre-telemetry, telemetry_missing, in-flight) appears in the
    completeness manifest by count, so exclusions are visible rather than
    silent.
    """
    salt = salt_for_recipient(recipient)
    rows = census(since=since, until=until)
    window_label = f"{since or 'corpus start'} to {until or 'corpus end'}"

    records = [
        _export_record(row, salt) for row in rows
        if row["era"] and isinstance(row["telemetry"], dict)
        and row["bucket"] in OUTCOME_CLASSES
    ]
    head, entries = chain_head()
    bundle = {
        "export_label": "identified, minimized",
        "schema_version": SCHEMA_VERSION,
        "derivation_version": DERIVATION_VERSION,
        "exported_on": str(datetime.now(timezone.utc).date()),
        "window": {"since": str(since) if since else None,
                   "until": str(until) if until else None},
        "records": records,
        "completeness": _completeness(rows),
        "integrity": {"chain_head": head, "chain_entries": entries},
    }

    if out_dir is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
        out_dir = runs_dir().parent / f"telemetry-export-{stamp}"
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "telemetry-export.json").write_text(
        json.dumps(bundle, indent=2) + "\n", encoding="utf-8")
    (out_dir / "methodology.md").write_text(
        _methodology_note(rows, window_label), encoding="utf-8")
    return out_dir


# === Anchoring ===

def anchor_chain() -> dict[str, Any]:
    """Record the current chain head as an anchorable value.

    A plain anchor file, deliberately: writing git refs from inside a
    verification tool means mutating a repo the tool does not own, and a
    wrongly-targeted ref write is worse than no anchor. The file gives the
    operator the exact value to move outside the agent-writable tree — sign
    it as a git tag, paste it somewhere append-only — which is the step that
    actually creates the "unaltered since anchoring" property.
    """
    head, entries = chain_head()
    anchor = {
        "chain_head": head,
        "chain_entries": entries,
        "anchored_at": datetime.now(timezone.utc).isoformat(),
        "note": (
            "Move this head outside the record tree to make it an anchor: "
            "e.g. `git tag -s fleetproof-anchor-<date> -m <chain_head>` in a "
            "repo the fleet does not write, or any append-only store."
        ),
    }
    (runs_dir().parent / ANCHOR_FILENAME).write_text(
        json.dumps(anchor, indent=2) + "\n", encoding="utf-8")
    return anchor
