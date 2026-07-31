"""The Dispatch Ledger — who was told to do what, and what actually happened.

v0.1 could verify one agent's "done" claim against a check spec. It could not
answer the question a fleet operator actually asks: *of everything I dispatched,
what came back, and did any of it check out?* A subagent that was launched, wrote
nothing, and died leaves no trace in a run log that only records tool calls.

So a **dispatch** is recorded at launch, before any work happens, as its own
top-level run:

    <runs-dir>/<run-id>/
        _root.json      root_tool="dispatch", parent_run_id -> the dispatching run
        dispatch.json   the prompt, the tier, the manifest, the transitions
        report.json     what the dispatched agent claimed (written on report)

Two properties matter more than convenience:

*Nothing grades itself.* The dispatching agent writes ``dispatch.json``; the
report is the dispatched agent's own claim; the ``verified``/``contradicted``
transition is appended by the checker, in a different process. This module only
does bookkeeping — pure comparison, no LLM, no judgement.

*Reality over tidiness.* The lifecycle allows ``terminated`` from any state,
including straight from ``dispatched``. An agent killed before it ever reported
is a thing that happens; a ledger that refused to record it would be lying to
keep its state machine pretty.

Lifecycle::

    dispatched -> reported -> verified | contradicted -> terminated
         |            |            (and each of those) -> terminated
         +------------+----------------------------------> terminated

The current state of a dispatch is its last transition. ``terminated`` is the
only terminal state. "Open" means not yet reported.

Public API:
    create_dispatch(prompt, *, tier, manifest, parent_run_id, by) -> run_id
    infer_tier(parent_run_id) -> str
    record_report(run_id, report, by=...)
    record_verdict(run_id, verdict, detail=..., by=...)
    close_dispatch(run_id, by=...)
    load_dispatch(run_id) -> DispatchRecord | None
    list_dispatches(*, session_id, open_only, non_terminal_only) -> list[DispatchRecord]
    derive_manifest(prompt) -> dict
"""

from __future__ import annotations

import json
import os
import re
import socket
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .checks import VALID_TIERS, spec_hash
from .runlog import (
    PARENT_RUN_ID_ENV,
    RUN_ID_ENV,
    SESSION_ID_ENV,
    _generate_run_id,
    runs_dir,
)

# === Constants ===

DISPATCH_FILENAME = "dispatch.json"
REPORT_FILENAME = "report.json"
ROOT_FILENAME = "_root.json"

# The root_tool every dispatch run carries, so runlog readers (list/show/report)
# can tell a dispatch apart from an ordinary recorded invocation.
DISPATCH_ROOT_TOOL = "dispatch"

STATE_DISPATCHED = "dispatched"
STATE_REPORTED = "reported"
STATE_VERIFIED = "verified"
STATE_CONTRADICTED = "contradicted"
STATE_TERMINATED = "terminated"

# Legal successors per state. ``terminated`` is reachable from everywhere on
# purpose (see the module docstring): the ledger records what happened, it does
# not enforce that agents die politely.
_ALLOWED_NEXT: dict[str, frozenset[str]] = {
    STATE_DISPATCHED: frozenset({STATE_REPORTED, STATE_TERMINATED}),
    STATE_REPORTED: frozenset({STATE_VERIFIED, STATE_CONTRADICTED, STATE_TERMINATED}),
    STATE_VERIFIED: frozenset({STATE_TERMINATED}),
    STATE_CONTRADICTED: frozenset({STATE_TERMINATED}),
    STATE_TERMINATED: frozenset(),
}

TERMINAL_STATES = frozenset({STATE_TERMINATED})
VERDICT_STATES = frozenset({STATE_VERIFIED, STATE_CONTRADICTED})

# Tier vocabulary is defined in :mod:`fleetproof.checks` (the lower-level module,
# which a check spec's optional "tier" field also validates against) and
# re-exported here so callers can import it from whichever layer they already use.
TIER_LEAF = "leaf"
TIER_LANE = "lane"
TIER_COORDINATOR = "coordinator"
TIER_BRIDGE = "bridge"
TIERS = VALID_TIERS

TIER_SOURCE_INFERRED = "inferred"
TIER_SOURCE_DECLARED = "declared"

# Evidence kinds a reported deliverable may claim, weakest last. "executed" means
# a command ran and its result is on record; "believed" means nobody checked.
VALID_EVIDENCE = frozenset({"executed", "observed", "believed"})

# Ceiling on an ancestor walk, so a malformed or circular parent chain costs a
# bounded amount of work instead of hanging a hook.
MAX_CHAIN_WALK = 64

# Run ids embed a timestamp plus a hash of pid+perf-counter, so a collision needs
# two dispatches in the same second on the same pid. A handful of retries is more
# than enough; failing loudly beats silently reusing a run directory.
_RUN_ID_ATTEMPTS = 8

EMPTY_MANIFEST_KEYS = ("deliverables", "allowed_paths", "checks")

DERIVED_V1_NO_MANIFEST = "derived-v1: no structured manifest in prompt"
DERIVED_V1_MALFORMED = "derived-v1: embedded manifest was malformed and ignored"


class LedgerError(Exception):
    """Raised on an illegal state transition or an unusable dispatch/report."""


# === Reading a dispatch back ===

@dataclass
class DispatchRecord:
    """One dispatch, as read back off disk. Absent fields read back as None."""
    run_id: str
    run_dir: Path
    parent_run_id: str | None
    session_id: str | None
    prompt: str
    tier: str | None
    tier_source: str | None
    manifest: dict[str, Any] = field(default_factory=dict)
    spec_sha256_pinned: str | None = None
    transitions: list[dict[str, Any]] = field(default_factory=list)
    started_at: str | None = None

    @property
    def state(self) -> str:
        """The last recorded transition's state.

        A dispatch.json with an empty transitions list is a damaged record; it
        reads back as ``dispatched`` rather than raising, so one bad file cannot
        take out the whole board.
        """
        if not self.transitions:
            return STATE_DISPATCHED
        return str(self.transitions[-1].get("state") or STATE_DISPATCHED)

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    @property
    def is_open(self) -> bool:
        """True while this dispatch has not reported yet.

        A dispatch terminated before it ever reported is *not* open — it is dead.
        Use :attr:`is_terminal` to ask "is it still running?".
        """
        return self.state == STATE_DISPATCHED

    @property
    def verdict(self) -> str | None:
        """The last verified/contradicted transition's state, or None if never graded.

        Kept separate from :attr:`state` because a graded dispatch that was then
        terminated still has a verdict worth showing on the board.
        """
        for entry in reversed(self.transitions):
            state = entry.get("state")
            if state in VERDICT_STATES:
                return str(state)
        return None

    @property
    def has_report(self) -> bool:
        return (self.run_dir / REPORT_FILENAME).exists()

    def load_report(self) -> dict[str, Any] | None:
        """Load report.json, or None when absent/unreadable."""
        return _read_json(self.run_dir / REPORT_FILENAME)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "parent_run_id": self.parent_run_id,
            "session_id": self.session_id,
            "prompt": self.prompt,
            "tier": self.tier,
            "tier_source": self.tier_source,
            "manifest": self.manifest,
            "spec_sha256_pinned": self.spec_sha256_pinned,
            "transitions": self.transitions,
            "started_at": self.started_at,
            "state": self.state,
            "verdict": self.verdict,
            "has_report": self.has_report,
        }


def _read_json(path: Path) -> dict[str, Any] | None:
    """Read a JSON object, or None if missing/unreadable/not an object."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def load_dispatch(run_id: str) -> DispatchRecord | None:
    """Load one dispatch by run id, or None if that run is not a dispatch."""
    run_dir = runs_dir() / run_id
    dispatch = _read_json(run_dir / DISPATCH_FILENAME)
    if dispatch is None:
        return None
    root = _read_json(run_dir / ROOT_FILENAME) or {}
    transitions = dispatch.get("transitions")
    manifest = dispatch.get("manifest")
    return DispatchRecord(
        run_id=run_id,
        run_dir=run_dir,
        parent_run_id=root.get("parent_run_id"),
        session_id=root.get("session_id"),
        prompt=str(dispatch.get("prompt") or ""),
        tier=dispatch.get("tier"),
        tier_source=dispatch.get("tier_source"),
        manifest=manifest if isinstance(manifest, dict) else {},
        spec_sha256_pinned=dispatch.get("spec_sha256_pinned"),
        transitions=transitions if isinstance(transitions, list) else [],
        started_at=root.get("started_at"),
    )


def list_dispatches(
    *,
    session_id: str | None = None,
    open_only: bool = False,
    non_terminal_only: bool = False,
) -> list[DispatchRecord]:
    """Every dispatch in the runs directory, newest first.

    ``open_only`` keeps dispatches that have not reported yet; ``non_terminal_only``
    keeps everything not terminated (i.e. "still in the fleet"). Run ids are
    timestamp-prefixed, so reverse directory order is newest-first.
    """
    rd = runs_dir()
    if not rd.exists():
        return []
    out: list[DispatchRecord] = []
    for run_dir in sorted(rd.iterdir(), reverse=True):
        if not run_dir.is_dir() or run_dir.name.startswith("."):
            continue
        if not (run_dir / DISPATCH_FILENAME).exists():
            continue
        record = load_dispatch(run_dir.name)
        if record is None:
            continue
        if session_id is not None and (record.session_id or None) != session_id:
            continue
        if open_only and not record.is_open:
            continue
        if non_terminal_only and record.is_terminal:
            continue
        out.append(record)
    return out


# === Tier inference ===

def _parent_of(run_id: str) -> str | None:
    """The parent run id on a run's _root.json, or None if absent/unreadable."""
    root = _read_json(runs_dir() / run_id / ROOT_FILENAME)
    if not root:
        return None
    parent = root.get("parent_run_id")
    return str(parent) if parent else None


def _declared_tier(run_id: str) -> str | None:
    dispatch = _read_json(runs_dir() / run_id / DISPATCH_FILENAME)
    if not dispatch:
        return None
    tier = dispatch.get("tier")
    return str(tier) if tier else None


def _is_chain_root(run_id: str) -> bool:
    """True when ``run_id`` tops its own chain: no parent, or declared bridge tier."""
    if _declared_tier(run_id) == TIER_BRIDGE:
        return True
    return _parent_of(run_id) is None


def ancestor_chain(run_id: str | None) -> list[str]:
    """Run ids from ``run_id`` up to its chain root, inclusive, nearest first.

    Walks ``_root.json`` parent links. Guarded against cycles (a run that is its
    own ancestor) and against unbounded chains, because this runs inside a hook
    and a malformed run tree must cost bounded time, not a hang.
    """
    if not run_id:
        return []
    chain: list[str] = [run_id]
    seen = {run_id}
    current = run_id
    while len(chain) < MAX_CHAIN_WALK:
        if _is_chain_root(current):
            break
        parent = _parent_of(current)
        if parent is None or parent in seen:
            break
        chain.append(parent)
        seen.add(parent)
        current = parent
    return chain


def infer_tier(parent_run_id: str | None) -> str:
    """Infer a dispatch's tier from how deep its parent chain goes.

    - no parent at all               -> ``bridge`` (nobody dispatched us)
    - parent tops its own chain      -> ``lane``   (a bridge dispatched us)
    - parent is itself dispatched    -> ``leaf``

    ``coordinator`` is never inferred: it is a role the operator assigns, not a
    shape visible in the run tree, so it only ever arrives declared.
    A parent whose record is missing or unreadable is treated as a chain root —
    the guess degrades to ``lane`` rather than failing the dispatch.
    """
    if not parent_run_id:
        return TIER_BRIDGE
    return TIER_LANE if len(ancestor_chain(parent_run_id)) <= 1 else TIER_LEAF


def _validate_tier(tier: str) -> str:
    if tier not in VALID_TIERS:
        raise LedgerError(
            f"Unknown tier {tier!r}; expected one of {sorted(VALID_TIERS)}."
        )
    return tier


# === Manifests ===

_JSON_FENCE_RE = re.compile(r"```json\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def empty_manifest(notes: str = "") -> dict[str, Any]:
    """The enrichable manifest shape: three empty lists plus free-text notes."""
    manifest: dict[str, Any] = {key: [] for key in EMPTY_MANIFEST_KEYS}
    manifest["notes"] = notes
    return manifest


def _coerce_manifest(raw: Any) -> dict[str, Any]:
    """Normalize a manifest to the canonical shape. Raises ValueError if unusable.

    Unknown keys are preserved: the manifest is meant to be enriched over time,
    and this module is not the arbiter of what a future field means.
    """
    if not isinstance(raw, dict):
        raise ValueError("manifest must be an object")
    out = empty_manifest()
    for key in EMPTY_MANIFEST_KEYS:
        val = raw.get(key, [])
        if val is None:
            val = []
        if not isinstance(val, list):
            raise ValueError(f"manifest.{key} must be an array")
        out[key] = list(val)
    notes = raw.get("notes", "")
    if notes is None:
        notes = ""
    if not isinstance(notes, str):
        raise ValueError("manifest.notes must be a string")
    out["notes"] = notes
    for key, val in raw.items():
        if key not in out:
            out[key] = val
    return out


def derive_manifest(prompt: str) -> dict[str, Any]:
    """Derive a manifest from a dispatch prompt. Derivation template v1.

    v1 is deliberately dumb, and versioned so a later template can be told apart
    in the ledger: if the prompt contains a fenced ```json block whose object has
    a top-level "manifest" key, that manifest is used. Otherwise the empty
    enrichable manifest is returned, with a note saying so. No inference, no
    keyword scraping, no model call — a manifest the operator did not write is
    not evidence of anything.

    A fenced block that *claims* a manifest but is malformed degrades to the
    empty manifest with a different note, so the failure is visible in the record
    rather than swallowed.
    """
    saw_malformed = False
    for match in _JSON_FENCE_RE.finditer(prompt or ""):
        body = match.group(1).strip()
        if not body:
            continue
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            continue
        if not isinstance(parsed, dict) or "manifest" not in parsed:
            continue
        try:
            return _coerce_manifest(parsed["manifest"])
        except ValueError:
            saw_malformed = True
    return empty_manifest(DERIVED_V1_MALFORMED if saw_malformed else DERIVED_V1_NO_MANIFEST)


# === Creating a dispatch ===

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _transition(state: str, by: str, detail: str = "") -> dict[str, Any]:
    entry: dict[str, Any] = {"state": state, "at": _now_iso(), "by": by or "cli"}
    if detail:
        entry["detail"] = detail
    return entry


def _default_parent_run_id() -> str | None:
    """The run this dispatch hangs off: the dispatching process's own run id.

    Prefers FLEETPROOF_RUN_ID (the run the dispatching process is executing as)
    and falls back to FLEETPROOF_PARENT_RUN_ID, which is what a hook process gets
    handed when it has no run of its own yet.
    """
    return os.environ.get(RUN_ID_ENV) or os.environ.get(PARENT_RUN_ID_ENV) or None


def _fresh_run_dir() -> tuple[str, Path]:
    """Allocate an unused run id + its directory."""
    rd = runs_dir()
    for _ in range(_RUN_ID_ATTEMPTS):
        run_id = _generate_run_id()
        run_dir = rd / run_id
        if not run_dir.exists():
            run_dir.mkdir(parents=True)
            return run_id, run_dir
    raise LedgerError("Could not allocate a unique dispatch run id.")


def create_dispatch(
    prompt: str,
    *,
    tier: str | None = None,
    manifest: dict[str, Any] | None = None,
    parent_run_id: str | None = None,
    by: str = "cli",
    spec_path: Path | None = None,
) -> str:
    """Record a dispatch at launch and return its run id.

    Creates a new top-level run directory with a ``_root.json`` whose
    ``parent_run_id`` points at the dispatching run, plus the ``dispatch.json``
    that makes it a dispatch. The current check spec's hash is pinned onto the
    record so a later verdict can be told whether it was graded against the spec
    that was in force when the work was ordered.

    ``tier`` omitted means infer it (recorded as ``tier_source="inferred"``);
    passing one records ``"declared"``. ``manifest`` omitted means derive one from
    the prompt. ``spec_path`` overrides which spec file gets hashed (hooks/tests);
    by default the project's ``.fleetproof/checks.json`` is used, and an
    unreadable spec pins ``null`` rather than failing the dispatch.
    """
    if not isinstance(prompt, str) or not prompt.strip():
        raise LedgerError("A dispatch needs a prompt; refusing to record an empty one.")

    resolved_parent = parent_run_id if parent_run_id is not None else _default_parent_run_id()

    if tier is None:
        resolved_tier = infer_tier(resolved_parent)
        tier_source = TIER_SOURCE_INFERRED
    else:
        resolved_tier = _validate_tier(tier)
        tier_source = TIER_SOURCE_DECLARED

    if manifest is None:
        resolved_manifest = derive_manifest(prompt)
    else:
        try:
            resolved_manifest = _coerce_manifest(manifest)
        except ValueError as e:
            raise LedgerError(f"Unusable manifest: {e}") from e

    run_id, run_dir = _fresh_run_dir()
    session_id = os.environ.get(SESSION_ID_ENV) or None

    # Same shape runlog writes for a root record, so `fleetproof list`/`show` and
    # the HTML report read dispatch runs back without knowing about the ledger.
    _write_json(run_dir / ROOT_FILENAME, {
        "run_id": run_id,
        "parent_run_id": resolved_parent,
        "session_id": session_id,
        "root_tool": DISPATCH_ROOT_TOOL,
        "started_at": _now_iso(),
        "host": socket.gethostname(),
        "user": os.environ.get("USER") or os.environ.get("USERNAME") or "unknown",
        "pid": os.getpid(),
    })
    _write_json(run_dir / DISPATCH_FILENAME, {
        "prompt": prompt,
        "tier": resolved_tier,
        "tier_source": tier_source,
        "manifest": resolved_manifest,
        "spec_sha256_pinned": spec_hash(spec_path),
        "transitions": [_transition(STATE_DISPATCHED, by)],
    })
    return run_id


# === State transitions ===

def _require_dispatch(run_id: str) -> DispatchRecord:
    record = load_dispatch(run_id)
    if record is None:
        raise LedgerError(f"No dispatch record for run {run_id!r}.")
    return record


def _append_transition(run_id: str, state: str, by: str, detail: str = "") -> DispatchRecord:
    """Append a transition after checking it is legal from the current state."""
    record = _require_dispatch(run_id)
    current = record.state
    allowed = _ALLOWED_NEXT.get(current, frozenset())
    if state not in allowed:
        raise LedgerError(
            f"Illegal transition {current!r} -> {state!r} for dispatch {run_id}; "
            f"legal next states: {sorted(allowed) or 'none (terminal)'}."
        )
    raw = _read_json(record.run_dir / DISPATCH_FILENAME) or {}
    transitions = raw.get("transitions")
    if not isinstance(transitions, list):
        transitions = []
    transitions.append(_transition(state, by, detail))
    raw["transitions"] = transitions
    _write_json(record.run_dir / DISPATCH_FILENAME, raw)
    record.transitions = transitions
    return record


def _validate_report(report: Any) -> dict[str, Any]:
    """Loosely validate a dispatched agent's report. Additive: unknown keys pass.

    The one hard rule: a report with neither a summary nor any deliverable is not
    a report, and accepting it would let "I did nothing" register as "I reported".
    """
    if not isinstance(report, dict):
        raise LedgerError("A report must be a JSON object.")
    summary = report.get("summary")
    if summary is not None and not isinstance(summary, str):
        raise LedgerError("report.summary must be a string.")
    deliverables = report.get("deliverables", [])
    if deliverables is None:
        deliverables = []
    if not isinstance(deliverables, list):
        raise LedgerError("report.deliverables must be an array.")
    if not (summary or "").strip() and not deliverables:
        raise LedgerError(
            "Empty report: needs a summary or at least one deliverable."
        )
    for i, item in enumerate(deliverables):
        if not isinstance(item, dict):
            raise LedgerError(f"report.deliverables[{i}] must be an object.")
        confidence = item.get("confidence")
        if confidence is not None:
            if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
                raise LedgerError(
                    f"report.deliverables[{i}].confidence must be a number 0.0-1.0."
                )
            if not 0.0 <= float(confidence) <= 1.0:
                raise LedgerError(
                    f"report.deliverables[{i}].confidence {confidence} is outside 0.0-1.0."
                )
        evidence = item.get("evidence")
        if evidence is not None and evidence not in VALID_EVIDENCE:
            raise LedgerError(
                f"report.deliverables[{i}].evidence must be one of "
                f"{sorted(VALID_EVIDENCE)}."
            )
    return report


def record_report(run_id: str, report: dict[str, Any], by: str = "cli") -> DispatchRecord:
    """Store what the dispatched agent claimed, then mark it ``reported``.

    The report is written verbatim: it is the agent's own claim, and the whole
    point of keeping it is being able to diff a claim against a verdict later.
    """
    record = _require_dispatch(run_id)
    validated = _validate_report(report)
    # Check the transition is legal *before* writing report.json, so a report
    # against a terminated dispatch cannot leave a claim on disk with no
    # transition explaining where it came from.
    allowed = _ALLOWED_NEXT.get(record.state, frozenset())
    if STATE_REPORTED not in allowed:
        raise LedgerError(
            f"Illegal transition {record.state!r} -> {STATE_REPORTED!r} for "
            f"dispatch {run_id}; legal next states: {sorted(allowed) or 'none (terminal)'}."
        )
    _write_json(record.run_dir / REPORT_FILENAME, validated)
    return _append_transition(run_id, STATE_REPORTED, by)


def record_verdict(
    run_id: str,
    verdict: str,
    detail: str = "",
    by: str = "checker",
) -> DispatchRecord:
    """Append the checker's verdict on a reported dispatch.

    Only ``verified`` or ``contradicted``, and only after a report — grading a
    dispatch that never reported anything would be grading nothing.
    """
    if verdict not in VERDICT_STATES:
        raise LedgerError(
            f"Verdict must be one of {sorted(VERDICT_STATES)}; got {verdict!r}."
        )
    return _append_transition(run_id, verdict, by, detail)


def close_dispatch(run_id: str, by: str = "cli") -> DispatchRecord:
    """Terminate a dispatch. Legal from any non-terminal state."""
    return _append_transition(run_id, STATE_TERMINATED, by)
